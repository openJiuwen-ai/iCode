# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Current-turn drop round execution and cancellation-drained spilling."""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.foundation.models.history_markers import read_last_words_record
from chrys.foundation.models.turns import is_continuation_message
from chrys.foundation.trajectory.metadata import read_analytics_item_id
from chrys.kernel import EXCLUDED_KEY, Message, annotate_token_counts, included_token_count, set_excluded

from .breaker import _DROP_MIN_PROGRESS_PCT, DropBreakerController
from .events import _REASON_CURRENT_TURN_DROP, CompactionInfo
from .exclusions import ExclusionLedger
from .groups import _tool_call_name
from .last_words import LastWordsSpendBudgetExceeded
from .last_words_state import LastWordsState, ManifestEntry, serialized_text_tokens
from .scoped import ScopedGroup, build_scoped_group_timeline
from .spill import (
    SpillBatchResult,
    SpillManifestItem,
    SpillQuota,
    group_result_texts,
    turn_directory_name,
    write_spill_batch,
)
from .summaries import _result_excerpt
from .turns import _resolve_turns

if TYPE_CHECKING:
    from collections.abc import Sequence

    from chrys.kernel import CompactionCallContext

    from .strategy import LastWordsGeneratorLike, UnifiedContextStrategy

_log = logging.getLogger(__name__)

# The newest work of the turn stays as it is: results the model has not read
# yet, and the calls it would otherwise have to repeat to see them again.
_KEEP_RECENT_MAX_TOKENS = 20_000
_KEEP_RECENT_WINDOW_SHARE = 0.10
# What the LAST_WORDS reminder adds besides the note itself: its heading,
# affordance and opening lines, and the dropped-call list.  The list is capped
# at six thousand characters; Han text is the densest the tokenizer measures
# at 0.6 tokens per character, so the cap is under 3,600 tokens, and text the
# serialized message escapes (quotes, backslashes, control characters) doubles
# at most, which for ASCII stays under 3,000.  The rest of the block is a few
# hundred.  The todo list it carries is measured separately in that serialized
# form: it has no cap of its own.  A pass whose only remaining limit is the
# window keeps the tail under both, since a request over the window is refused
# however small the excess.
_NOTE_REMINDER_RESERVE_TOKENS = 4_096


@dataclass(frozen=True, slots=True)
class _DropPlan:
    """Which of the turn's groups one round drops; the groups after them stay."""

    drop: tuple[ScopedGroup, ...]
    drop_tokens: int
    oversized: ScopedGroup | None
    """The newest tool group, when it is dropped only for being larger than what may stay.

    Its manifest lines carry both ends of each result, so the newest work is
    not dropped without a trace in the request.
    """


def _recorded_note(messages: list[Message], span: tuple[int, int]) -> str | None:
    """The note the turn's carrier records, for a turn whose live note is gone."""
    for message in reversed(messages[span[0] : span[1]]):
        if message.role != "user" or message.additional_properties.get(EXCLUDED_KEY, False):
            continue
        record = read_last_words_record(message.additional_properties)
        if record is not None:
            return record.note or None
    return None


def _consumed_item_ids(groups: Sequence[ScopedGroup]) -> list[str]:
    """Return the ordered unique stable ids represented by Phase-4 groups."""
    seen: set[str] = set()
    ordered: list[str] = []
    for group in groups:
        for message in group.messages:
            candidates = [
                read_analytics_item_id(message.additional_properties),
                *(read_analytics_item_id(content.additional_properties) for content in message.contents),
            ]
            for item_id in candidates:
                if item_id is not None and item_id not in seen:
                    seen.add(item_id)
                    ordered.append(item_id)
    return ordered


async def _write_spill_batch_in_worker(
    session_root: Path | None,
    quota: SpillQuota | None,
    groups: Sequence[ScopedGroup],
    *,
    record_dir: Path,
    absolute_turn: int,
    round_number: int,
    session_id: str,
    superseded_note: str | None = None,
) -> SpillBatchResult:
    """Run one spill batch without letting cancellation orphan its writes.

    ``asyncio.to_thread`` cannot stop a thread when its awaiting task is
    cancelled. Drain the owned worker before propagating cancellation so a
    session reset/restore cannot replace the quota lock or mutate the session
    directory while the old batch is still running.
    """
    worker = asyncio.create_task(
        asyncio.to_thread(
            write_spill_batch,
            session_root,
            quota,
            groups,
            record_dir=record_dir,
            absolute_turn=absolute_turn,
            round_number=round_number,
            session_id=session_id,
            superseded_note=superseded_note,
        )
    )
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Repeated interrupts must not detach the same worker. Each shield is
        # a fresh waiter; the underlying task remains owned until it settles.
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            try:
                worker.result()
            except Exception:
                _log.debug("Spill worker failed while its caller was cancelled", exc_info=True)
        raise


class CurrentTurnDropRound:
    """Execute the final current-turn drop stage against live strategy state."""

    def __init__(
        self,
        strategy: UnifiedContextStrategy,
        breaker: DropBreakerController,
        ledger: ExclusionLedger,
    ) -> None:
        self._strategy = strategy
        self._breaker = breaker
        self._ledger = ledger

    async def _generate_last_words(
        self,
        timeline_groups: list[ScopedGroup],
        previous: str | None,
        *,
        degraded_opener: bool,
        has_continuation_nudges: bool,
        context: CompactionCallContext | None,
        generator: LastWordsGeneratorLike,
        last_words: LastWordsState,
    ) -> str:
        # max() folds the retained legacy ``tool_definition_tokens`` spelling.
        request_overhead_tokens = (
            max(context.request_overhead_tokens, context.tool_definition_tokens) if context is not None else 0
        )
        failure_recorded = False
        try:
            new_last_words = await generator.generate(
                timeline_groups,
                previous,
                degraded_opener=degraded_opener,
                has_continuation_nudges=has_continuation_nudges,
                completer=context.last_words_completer if context is not None else None,
                tokenizer=self._strategy.tokenizer,
                system_overhead_tokens=self._strategy.system_overhead_tokens,
                request_overhead_tokens=request_overhead_tokens,
                calibration_ratio=self._strategy.calibration_ratio,
                spend_side_call_tokens=self._breaker.spend_side_call_tokens,
                affords_side_call_tokens=self._breaker.affords_side_call_tokens,
                entry_messages=self._strategy._entry_request,
            )
        except LastWordsSpendBudgetExceeded:
            breaker = self._breaker.record_no_progress("side_call_budget")
            if breaker is not None and not breaker.disabled:
                disabled = replace(breaker, disabled=True)
                last_words.set_drop_round_breaker(disabled)
                self._breaker.emit_context_pressure("side_call_budget", disabled)
            failure_recorded = True
            _log.warning("Phase 4 side-call spend budget exhausted; retaining current-turn groups")
            new_last_words = ""
        except Exception:
            self._breaker.record_no_progress("generation_failure")
            raise
        if not new_last_words and not failure_recorded:
            self._breaker.record_no_progress("generation_failure")
        return new_last_words

    async def _spill_drop_groups(
        self,
        drop_groups: list[ScopedGroup],
        *,
        absolute_turn: int,
        round_number: int,
        previous: str | None,
    ) -> tuple[SpillBatchResult, bool]:
        record_dir = self._strategy._spill_record_dir / turn_directory_name(absolute_turn)
        spill_result = await _write_spill_batch_in_worker(
            self._strategy._spill_root,
            self._strategy._spill_quota,
            drop_groups,
            record_dir=record_dir,
            absolute_turn=absolute_turn,
            round_number=round_number,
            session_id=self._strategy._spill_session_id,
            # The merge that authorized this drop replaces
            # ``previous``; archive that pre-merge original
            # verbatim alongside the round's group records.
            superseded_note=previous or None,
        )
        spill_permitted = True
        if spill_result.unexpected_io_failure:
            spill_permitted = False
            if self._strategy._persist_recovery_now is not None:
                try:
                    spill_permitted = (await self._strategy._persist_recovery_now()) is True
                except Exception:
                    _log.warning(
                        "Phase 4 recovery persistence failed after spill I/O error",
                        exc_info=True,
                    )
            if not spill_permitted:
                self._breaker.record_no_progress("spill_abort")
                _log.warning("Phase 4 spill I/O failure was not durably checkpointed; retaining groups")
        return spill_result, spill_permitted

    def _plan_drop(self, groups: list[ScopedGroup], *, tokens_before: int, note_tokens: int = 0) -> _DropPlan:
        """Split the turn's work into what this round drops and the recent tail that stays.

        The tail is whole groups, newest first, while they fit what may stay,
        and starts at a tool group: assistant text ahead of the first kept
        call belongs to the dropped work.  An overflow pass gives the tail up
        group by group, oldest first, until the pass's stop point is reached;
        every other pass gives it up the same way only while the request after
        the drop would otherwise exceed the window, so the work kept is work
        the request can still carry.  *note_tokens* is what the round's note
        adds to the request, known once the note is written.
        """
        strategy = self._strategy
        plan = strategy._plan
        keep_budget = min(_KEEP_RECENT_MAX_TOKENS, int(strategy.max_context_tokens * _KEEP_RECENT_WINDOW_SHARE))
        sizes = [included_token_count(list(group.messages)) for group in groups]
        kept_from = len(groups)
        kept_tokens = 0
        while kept_from > 0 and kept_tokens + sizes[kept_from - 1] <= keep_budget:
            kept_from -= 1
            kept_tokens += sizes[kept_from]

        def start_at_tool_group(index: int) -> int:
            while index < len(groups) and groups[index].kind != "tool_call":
                index += 1
            return index

        kept_from = start_at_tool_group(kept_from)
        # An overflow pass keeps nothing the pass's stop point does not allow.
        # Any other pass keeps its tail while the request that goes out still
        # fits the window, and gives it up group by group past it: the tail is
        # work the turn has just done, and room above the trigger is the
        # booking's business, but a request the provider will refuse is not.
        # The reminder the drop leaves adds text around the note: a reserve for
        # the list and its chrome, plus the todo section as it will be captured.
        # The reserve is local tokens, and usage is counted after calibration,
        # so the room it needs is scaled by the same ratio the request is.
        reserve = _NOTE_REMINDER_RESERVE_TOKENS
        last_words_state = strategy._last_words_state
        if last_words_state is not None and not plan.spend_recent_tail:
            reserve += last_words_state.todo_reminder_tokens(strategy.tokenizer.count_tokens)
        reserved = math.ceil(reserve * strategy.calibration_ratio)
        gate = (
            plan.stop_pct
            if plan.spend_recent_tail
            else (strategy.max_context_tokens - reserved) / strategy.max_context_tokens
        )
        while (
            kept_from < len(groups) and strategy._usage_pct(tokens_before + note_tokens - sum(sizes[:kept_from])) > gate
        ):
            kept_from = start_at_tool_group(kept_from + 1)
        oversized: ScopedGroup | None = None
        if kept_from == len(groups):
            newest = next((index for index in range(len(groups) - 1, -1, -1) if groups[index].kind == "tool_call"), -1)
            if newest >= 0 and sizes[newest] > keep_budget:
                oversized = groups[newest]
        return _DropPlan(tuple(groups[:kept_from]), sum(sizes[:kept_from]), oversized)

    async def run(
        self,
        messages: list[Message],
        context: CompactionCallContext | None,
    ) -> bool:
        """Run one Phase-4 pipeline stage."""
        changed = False

        # ---- Phase 4: drop the current turn's older tool calls + inline agent text ----
        # One shot: collect every current-turn group that is still included
        # except the recent tail, hand the turn to the LAST_WORDS LLM
        # summariser, exclude the collected groups, and record the updated
        # note on the turn's carrier message, which every later call sends
        # with it.
        # Generator failures (SDK error, empty response) are retried inside
        # the generator against the same LAST_WORDS prompt — we never drop
        # current-turn work without a note in hand.
        p4_count = 0
        p4_tool_names: list[str] = []
        p4_consumed_item_ids: list[str] = []
        p4_last_words_generated = False
        p4_tokens_before = included_token_count(messages)

        # The pass's plan decides whether dropping current-turn work is on the
        # table at all: never in a turn-boundary pass, and in a hard pass only
        # while usage is still above the force line.
        phase4_pct = self._strategy._plan.phase4_pct
        if phase4_pct is not None and self._strategy._usage_pct(p4_tokens_before) > phase4_pct:
            # Resolve fresh at this call site against the post-P3 lists.
            resolved = _resolve_turns(messages, self._strategy._state)
            current_span = resolved.current
            current_turn = (current_span.start, current_span.end) if current_span is not None else (0, len(messages))
            timeline = build_scoped_group_timeline(
                messages,
                span_start=current_turn[0],
                span_end=current_turn[1],
                degraded=resolved.degraded,
            )
            turn_work = [group for group in timeline.groups if group.kind not in ("user", "system")]
            drop_plan = self._plan_drop(turn_work, tokens_before=p4_tokens_before)
            drop_groups = list(drop_plan.drop)

            # Phase 4 only fires when there is at least one tool-call group
            # to drop — otherwise there is nothing meaningful to summarise
            # and dropping plain assistant text alone would lose content
            # without replacing it — and when the drop is worth a round: one
            # that frees less than a round has to would only spend a side
            # call and a breaker attempt on a prefix rewrite.  An overflow
            # pass takes what it can get: the provider refused the request,
            # and a little room may be all the resend needs.
            has_tool_call = any(group.kind == "tool_call" for group in drop_groups)
            freed_pct = self._strategy._usage_pct(p4_tokens_before) - self._strategy._usage_pct(
                p4_tokens_before - drop_plan.drop_tokens
            )
            worth_a_round = self._strategy._plan.spend_recent_tail or freed_pct >= _DROP_MIN_PROGRESS_PCT

            if has_tool_call and worth_a_round:
                reminder_middleware = self._strategy._reminder_middleware
                last_words_state = self._strategy._last_words_state
                last_words_generator = self._strategy._last_words_generator
                if last_words_generator is None or reminder_middleware is None or last_words_state is None:
                    _log.warning(
                        "Phase 4 SKIPPED drop: fresh-note collaborators unbound "
                        "(reminder_middleware=%s, last_words_state=%s, last_words_generator=%s)",
                        "bound" if reminder_middleware is not None else "None",
                        "bound" if last_words_state is not None else "None",
                        "bound" if last_words_generator is not None else "None",
                    )
                else:
                    # Step 1: synchronous entry check and attempt count, before
                    # hooks, preflight, or either side-call path.
                    breaker_at_entry, trip_reason = self._breaker.enter_round()
                    if breaker_at_entry is None:
                        await self._breaker.publish_trip(trip_reason)
                    else:
                        entry_usage_pct = self._strategy._usage_pct(p4_tokens_before)
                        await self._strategy._fire_pre_compact(
                            "phase4",
                            usage_pct=entry_usage_pct,
                            tokens_before=p4_tokens_before,
                        )
                        # Step 2: only a non-empty note produced by THIS round
                        # authorizes the later drop. A stale note is inherited
                        # input, never authorization for a new delta.
                        previous = last_words_state.get_last_words() or _recorded_note(messages, current_turn)
                        has_continuation_nudges = any(
                            is_continuation_message(message) for group in timeline.groups for message in group.messages
                        )
                        new_last_words = await self._generate_last_words(
                            list(timeline.groups),
                            previous,
                            degraded_opener=timeline.degraded_opener,
                            has_continuation_nudges=has_continuation_nudges,
                            context=context,
                            generator=last_words_generator,
                            last_words=last_words_state,
                        )

                        if new_last_words:
                            # The plan is for the request that goes out, note
                            # included: the one made before the note existed
                            # left room for the work alone.  The note was
                            # written from the whole turn, so the groups given
                            # up here are in it too.  What the reminder adds
                            # around the note is covered by the plan's reserve
                            # rather than counted, since it depends on records
                            # this drop has not written yet.  The note counts in
                            # the form the request carries it (escaped in the
                            # serialized message), not as its rendered text.
                            tokenizer = self._strategy.tokenizer
                            drop_plan = self._plan_drop(
                                turn_work,
                                tokens_before=p4_tokens_before,
                                note_tokens=max(
                                    0,
                                    serialized_text_tokens(new_last_words, tokenizer.count_tokens)
                                    - serialized_text_tokens(previous, tokenizer.count_tokens),
                                ),
                            )
                            drop_groups = list(drop_plan.drop)
                        p4_consumed_item_ids = _consumed_item_ids(drop_groups)
                        for group in drop_groups:
                            if group.kind == "tool_call":
                                p4_tool_names.extend(
                                    tool_name
                                    for message in group.messages
                                    for content in message.contents
                                    if (tool_name := _tool_call_name(content))
                                )

                        if new_last_words:
                            # Step 3: worker-thread spill, including catalog
                            # append+flush and catalog-only manifest projection.
                            absolute_turn = current_span.absolute_number if current_span is not None else 1
                            spill_result, spill_permitted = await self._spill_drop_groups(
                                drop_groups,
                                absolute_turn=absolute_turn,
                                round_number=breaker_at_entry.attempts,
                                previous=previous,
                            )

                            if spill_permitted:
                                manifest_entries = self._manifest_entries(spill_result, drop_plan.oversized)

                                # Steps 4-5: synchronous commit through
                                # exclusion and the post-round usage sample.
                                # Do not insert an await anywhere in this block.
                                last_words_state.mark_manifest_records_unavailable(spill_result.evicted_relative_paths)
                                last_words_state.set_last_words(new_last_words)
                                last_words_state.append_manifest(manifest_entries)
                                p4_last_words_generated = True
                                reminder_middleware.refresh_last_words_reminder(messages)
                                annotate_token_counts(
                                    messages,
                                    tokenizer=self._strategy.tokenizer,
                                    force_retokenize=True,
                                )
                                changed = True

                                for group in drop_groups:
                                    any_changed = False
                                    for message in group.messages:
                                        any_changed = (
                                            set_excluded(
                                                message,
                                                excluded=True,
                                                reason=_REASON_CURRENT_TURN_DROP,
                                            )
                                            or any_changed
                                        )
                                    if any_changed:
                                        self._ledger._removed_group_ids.add(group.group_id)
                                        p4_count += 1
                                post_p4_tokens = included_token_count(messages)
                                self._breaker.record_progress(
                                    entry_usage_pct,
                                    self._strategy._usage_pct(post_p4_tokens),
                                    low_yield_counts=not self._strategy._plan.spend_recent_tail,
                                )
                                # Anchor/token bookkeeping must also land
                                # inside the synchronous commit: the awaits
                                # that follow (committed publish,
                                # on_compaction) honor cancellation, and an
                                # interrupt that observed exclusions without
                                # anchors would resume retaining both the
                                # original history and the note.
                                self._strategy._last_included_tokens = post_p4_tokens
                                self._ledger.snapshot_excluded_anchors(messages)
                                self._strategy._note_prefix_rewrite(head=False)
                                # The round is now durably applied; tell
                                # observers that count real compactions
                                # (the finished-ok signal fired pre-commit
                                # and can belong to an abandoned round).
                                try:
                                    await last_words_generator.publish_committed()
                                except asyncio.CancelledError:
                                    # The commit above is durable, but the
                                    # end-of-pass on_compaction notification
                                    # (ToolCompacted for the main agent)
                                    # would be skipped by the cancellation —
                                    # deliver it on a detached task before
                                    # propagating, covering the rounds
                                    # committed so far this pass.
                                    self._schedule_detached_on_compaction(
                                        CompactionInfo(
                                            compacted_groups=p4_count,
                                            phase="phase4",
                                            turn_numbers=[current_span.absolute_number]
                                            if current_span is not None
                                            else [],
                                            tool_names=p4_tool_names,
                                            tokens_before=p4_tokens_before,
                                            tokens_after=post_p4_tokens,
                                            last_words_generated=True,
                                            consumed_item_ids=p4_consumed_item_ids,
                                        )
                                    )
                                    raise

            if p4_count > 0 or p4_last_words_generated:
                # The rounds this reports are already durably committed, so
                # the delivery itself must survive cancellation for the
                # whole await window, not just inside publish_committed
                # above: run it as an anchored task behind a shield —
                # cancellation still propagates to the caller, but the
                # ToolCompacted delivery finishes detached.
                delivery = self._schedule_detached_on_compaction(
                    CompactionInfo(
                        compacted_groups=p4_count,
                        phase="phase4",
                        turn_numbers=[current_span.absolute_number] if current_span is not None else [],
                        tool_names=p4_tool_names,
                        tokens_before=p4_tokens_before,
                        tokens_after=included_token_count(messages),
                        last_words_generated=p4_last_words_generated,
                        consumed_item_ids=p4_consumed_item_ids,
                    )
                )
                await asyncio.shield(delivery)

        return changed

    @classmethod
    def _manifest_entries(cls, spill_result: SpillBatchResult, oversized: ScopedGroup | None) -> list[ManifestEntry]:
        """The round's manifest lines; those of *oversized* carry both ends of their results."""
        excerpts = iter(group_result_texts(oversized)) if oversized is not None else iter(())
        entries: list[ManifestEntry] = []
        for item in spill_result.entries:
            excerpt = ""
            if oversized is not None and item.group_id == oversized.group_id and not item.assistant_text:
                excerpt = _result_excerpt(next(excerpts, ""))
            entries.append(cls._manifest_entry(item, excerpt=excerpt))
        return entries

    @staticmethod
    def _manifest_entry(item: SpillManifestItem, *, excerpt: str = "") -> ManifestEntry:
        return ManifestEntry(
            record_id=item.record_id,
            group_id=item.group_id,
            record_dir=item.record_dir,
            relative_path=item.relative_path,
            turn=item.turn,
            round=item.round,
            sequence=item.sequence,
            tool=item.tool,
            display_argument=item.display_argument,
            outcome=item.outcome,
            size_chars=item.size_chars,
            assistant_text=item.assistant_text,
            available=item.available,
            no_record_reason=item.no_record_reason,
            excerpt=excerpt,
        )

    def _schedule_detached_on_compaction(self, info: CompactionInfo) -> asyncio.Future:
        """Record the phase now; hand the observer callback to a detached task.

        The pass around this call can close its run before a task started here
        gets to write — it may not have run at all yet, or be waiting on the
        ack of the phase's opening line while the pass unwinds — and the phase
        (or the tail of a segmented one) would land behind the run's own
        terminal marker. Queued here it cannot.
        """
        run = self._strategy._trajectory_run
        if run is not None:
            run.phase_finished_soon(
                phase=info.phase,
                groups_compacted=info.compacted_groups,
                turn_numbers=info.turn_numbers,
                tool_names=info.tool_names,
                tokens_before=info.tokens_before,
                tokens_after=info.tokens_after,
                last_words_generated=info.last_words_generated,
                consumed_item_ids=info.consumed_item_ids,
            )
        # No run: the phase above already carries it, and only the observer
        # callback is left to deliver.
        task = asyncio.ensure_future(self._strategy._notify_compaction_for_run(info, None))
        self._strategy._detached_deliveries.add(task)
        task.add_done_callback(self._discard_detached_delivery)
        return task

    def _discard_detached_delivery(self, task: asyncio.Future) -> None:
        self._strategy._detached_deliveries.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            _log.debug("Detached on_compaction delivery failed", exc_info=exc)
