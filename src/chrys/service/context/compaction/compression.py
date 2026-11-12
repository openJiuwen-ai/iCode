# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-turn compression state and fold execution toolkit."""

from __future__ import annotations

import uuid
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import EXCLUDED_KEY, Message, annotate_token_counts, set_excluded
from chrys.kernel.compaction import _token_count, included_token_count
from chrys.kernel.identity import WeakIdentityRegistry
from chrys.service.context.providers.archive_index import merge_fold_summaries, project_archive
from chrys.service.context.providers.history import PRE_OUTPUT_HISTORY_LEN_STATE_KEY

from .events import (
    _REASON_COMPRESSION,
    CachedCompressedContextSummary,
    CompressInfo,
    PendingCompression,
    _compressed_turn_range,
)
from .exclusions import ExclusionLedger
from .groups import (
    _carries_only,
    _content_object_ids,
    _fold_summary_ids,
    _matches_summary_id,
    _message_present,
)
from .turns import _resolve_turns

if TYPE_CHECKING:
    from chrys.service.context.providers.history import CompressedBlock

    from .strategy import UnifiedContextStrategy


@dataclass(frozen=True, slots=True)
class _FoldMatch:
    """Recognizes a wire message as the per-call copy of a folded state message.

    Tiers: the same contents list (legacy same-object lists), only the fold's
    content objects (the kernel's per-call wire views: fresh wrapper and
    contents list), the same ``additional_properties`` (the reminder
    middleware's enriched user messages: fresh contents, the original's dict)
    or a summary of a folded occurrence.  Every fold site matches through
    this one predicate so a new copy shape can't reach only some of them.
    """

    contents: WeakIdentityRegistry | set[int]
    content_objects: WeakIdentityRegistry | set[int]
    properties_ids: set[int]
    summary_ids: set[str] | frozenset[str]

    @classmethod
    def of_range(cls, fold_range: list[Message], summary_ids: set[str] | frozenset[str]) -> _FoldMatch:
        """Frame-local match for a fold range still held by the caller.

        The ``additional_properties`` ids are safe keys: the fold flags these
        very dicts excluded afterwards, so a wire message sharing one leaves
        the request either way; matching it only places the summary where the
        range starts.
        """
        return cls(
            contents={id(message.contents) for message in fold_range},
            content_objects=_content_object_ids(fold_range),
            properties_ids={id(message.additional_properties) for message in fold_range},
            summary_ids=summary_ids,
        )

    @classmethod
    def of_cached(cls, cached: CachedCompressedContextSummary) -> _FoldMatch:
        """Match for a cached fold whose originals may have been collected.

        The ``additional_properties`` ids are read now from the originals
        still alive, so none is the recycled id of a collected dict.
        """
        return cls(
            contents=cached.folded_contents,
            content_objects=cached.folded_content_objects,
            properties_ids={
                id(message.additional_properties) for ref in cached.folded_messages if (message := ref()) is not None
            },
            summary_ids=cached.folded_summary_ids,
        )

    def matches(self, message: Message) -> bool:
        contents = self.contents
        same_contents = (
            message.contents in contents
            if isinstance(contents, WeakIdentityRegistry)
            else id(message.contents) in contents
        )
        return (
            same_contents
            or _carries_only(message, self.content_objects)
            or id(message.additional_properties) in self.properties_ids
            or (bool(self.summary_ids) and _matches_summary_id(message, self.summary_ids))
        )


def _fold_tokens(fold_range: list[Message]) -> int:
    """Local token estimate of a fold range; untokenized messages count as 0."""
    total = 0
    for message in fold_range:
        token_count = _token_count(message)
        if token_count is not None:
            total += token_count
    return total


class CompressionEngine:
    """Own provider compression state and compose site-specific folds."""

    def __init__(self, strategy: UnifiedContextStrategy, ledger: ExclusionLedger) -> None:
        self._strategy = strategy
        self._ledger = ledger
        self._state: dict[str, Any] | None = None
        self._pending_compressions: list[PendingCompression] = []
        self._compressed_context_cache: dict[str, CachedCompressedContextSummary] = {}
        # Strong references to folds whose on_compress publication completed.
        # Retry snapshots retain only an index into this monotonic journal;
        # rollback can then replay every later fold with its original block
        # identity/metadata and without publishing a duplicate UI event.
        self._published_folds: list[CompressedBlock] = []

    def bind_state(self, state: dict[str, Any]) -> None:
        """Bind the provider state and clear only the same-run summary cache."""
        self._compressed_context_cache.clear()
        self._state = state

    def queue_compression(self, marker_id: str, summary_text: str) -> tuple[str, int]:
        """Validate and queue one agent-requested compression."""
        if self._state is None:
            raise RuntimeError("Strategy not bound — call bind_state() first.")

        from chrys.service.context.providers.history import _compress_state_validate

        freed_count = _compress_state_validate(self._state, marker_id)
        ctx_id = f"ctx_{uuid.uuid4().hex[:8]}"
        self._pending_compressions.append(
            PendingCompression(
                compressed_context_id=ctx_id,
                marker_id=marker_id,
                summary_text=summary_text,
                freed_count=freed_count,
            )
        )
        return ctx_id, freed_count

    def list_compressed(self) -> dict[str, Any]:
        """List compressed blocks and markers not consumed by queued work."""
        if self._state is None:
            return {"blocks": [], "markers": []}

        from chrys.service.context.providers.history import CompressibleHistoryProvider

        info = CompressibleHistoryProvider.list_compressed(self._state)
        pending_markers = {pending.marker_id for pending in self._pending_compressions}
        if pending_markers:
            info["markers"] = [marker for marker in info["markers"] if marker["marker_id"] not in pending_markers]
        return info

    def execute_fold(
        self,
        marker_id: str,
        summary: str,
        compressed_context_id: str | None = None,
    ) -> tuple[str, list[Message]]:
        """Apply one provider-state fold; callers own exception boundaries."""
        if self._state is None:
            raise RuntimeError("Strategy not bound — call bind_state() first.")

        from chrys.service.context.providers.history import _compress_state

        if compressed_context_id is None:
            return _compress_state(self._state, marker_id, summary)
        return _compress_state(self._state, marker_id, summary, compressed_context_id)

    def published_fold_count(self) -> int:
        """Return the monotonic fold-journal boundary for a retry snapshot."""
        return len(self._published_folds)

    def replay_published_folds(self, start_index: int) -> None:
        """Silently reapply published folds created after *start_index*.

        The executor restores the pre-attempt message list and truncates new
        compressed blocks before calling this method.  Replaying in journal
        order is well-defined because Phase 3 folds only completed turns;
        each block's marker is therefore present in that restored baseline.
        The existing block object is put back after ``_compress_state`` does
        the list rewrite so its id, archived messages, turn range, and
        timestamp remain byte-for-byte stable.
        """
        if self._state is None:
            return

        from chrys.service.context.providers.history import _compress_state

        for block in self._published_folds[start_index:]:
            if self._state_has_compressed_block(block.compressed_context_id):
                continue
            _compress_state(
                self._state,
                block.marker_id,
                block.summary_text,
                block.compressed_context_id,
            )
            compressed_blocks = self._state.get("compressed_msgs", [])
            if not isinstance(compressed_blocks, list) or not compressed_blocks:
                raise RuntimeError("Replayed compression did not append its block.")
            compressed_blocks[-1] = block
            self._record_pre_output_history_len()

    def _record_published_fold(self, compressed_context_id: str) -> None:
        """Append the committed provider block after on_compress returns."""
        if self._state is None:
            return
        compressed_blocks = self._state.get("compressed_msgs", [])
        if not isinstance(compressed_blocks, list):
            return

        from chrys.service.context.providers.history import CompressedBlock

        for block in reversed(compressed_blocks):
            if isinstance(block, CompressedBlock) and block.compressed_context_id == compressed_context_id:
                if not self._published_folds or self._published_folds[-1] is not block:
                    self._published_folds.append(block)
                return

    def exclude_wire_matches(
        self,
        messages: list[Message],
        fold_range: list[Message],
        fold_summary_ids: set[str],
        *,
        match_summary_refs: bool,
        skip_already_excluded: bool,
        insertion_index: int | None = None,
    ) -> int | None:
        """Exclude the wire projection for a fold under explicit site policy.

        Match wire-list entries against the fold range (state objects) with
        :class:`_FoldMatch`; summary references count only when
        *match_summary_refs* is set.
        """
        fold = _FoldMatch.of_range(fold_range, fold_summary_ids if match_summary_refs else frozenset())
        for idx, message in enumerate(messages):
            if skip_already_excluded and message.additional_properties.get(EXCLUDED_KEY, False):
                continue
            if not fold.matches(message):
                continue
            if insertion_index is None:
                insertion_index = idx
            set_excluded(message, excluded=True, reason=_REASON_COMPRESSION)
        return insertion_index

    def insert_and_cache_summary(
        self,
        messages: list[Message],
        compressed_context_id: str,
        fold_range: list[Message],
        fold_summary_ids: set[str],
        insertion_index: int | None,
    ) -> None:
        """Keep a state-fold summary visible on this and later wire views."""
        summary_msg = self._find_state_summary(compressed_context_id)
        if summary_msg is None:
            return
        if insertion_index is not None and not _message_present(messages, summary_msg):
            messages.insert(insertion_index, summary_msg)
        folded_contents = WeakIdentityRegistry()
        folded_content_objects = WeakIdentityRegistry()
        for folded in fold_range:
            folded_contents.register(folded.contents)
            for content in folded.contents:
                folded_content_objects.register(content)
        self._compressed_context_cache[compressed_context_id] = CachedCompressedContextSummary(
            summary_message=summary_msg,
            folded_contents=folded_contents,
            folded_summary_ids=frozenset(fold_summary_ids),
            folded_content_objects=folded_content_objects,
            folded_messages=tuple(weakref.ref(folded) for folded in fold_range),
        )

    async def run_queued(self, messages: list[Message]) -> bool:
        """Execute queued folds against provider state and the live wire list."""
        # ---- Step 0: Process pending cross-turn compressions ----
        # Compressions modify self._state["messages"] (has turn markers)
        # and set _excluded on the wire message list (the `messages`
        # parameter).  History messages reach the wire un-copied (kernel
        # SessionContext.extend_messages), so the contents-identity match is
        # a direct hit for them; it stays a *match* rather than an identity
        # walk because streaming finalizers rebuild current-turn messages
        # into fresh objects with fresh contents lists.
        compressions_processed = False
        if self._pending_compressions and self._state is not None:
            for pending in self._pending_compressions:
                try:
                    _ctx_id, fold_range = self.execute_fold(
                        pending.marker_id,
                        pending.summary_text,
                        pending.compressed_context_id,
                    )
                except ValueError:
                    continue
                # Both figures of the fold's event are counts of this list,
                # like every phase's: the newest messages get theirs first.
                annotate_token_counts(messages, tokenizer=self._strategy.tokenizer)
                tokens_before = included_token_count(messages)
                self._record_pre_output_history_len()
                fold_summary_ids = _fold_summary_ids(fold_range)
                insertion_index = self.exclude_wire_matches(
                    messages,
                    fold_range,
                    fold_summary_ids,
                    match_summary_refs=False,
                    skip_already_excluded=False,
                )
                self.insert_and_cache_summary(
                    messages,
                    pending.compressed_context_id,
                    fold_range,
                    fold_summary_ids,
                    insertion_index,
                )
                # The state-side fold range is re-flagged even when absent
                # from the wire projection.
                for message in fold_range:
                    set_excluded(message, excluded=True, reason=_REASON_COMPRESSION)
                self._ledger.invalidate_for_fold(fold_range)
                self._strategy._note_prefix_rewrite(head=True)
                compressions_processed = True
                # Before the fold is reported: what the merge moves into the
                # index is part of what this fold freed.
                self._merge_archive(messages)
                annotate_token_counts(messages, tokenizer=self._strategy.tokenizer)
                await self._strategy._notify_compress(
                    CompressInfo(
                        compressed_context_id=pending.compressed_context_id,
                        summary=pending.summary_text,
                        freed_messages=len(fold_range),
                        turn_range=_compressed_turn_range(self._state, pending.compressed_context_id),
                        source="agent",
                    ),
                    trigger="agent_request",
                    tokens_before=tokens_before,
                    tokens_after=included_token_count(messages),
                )
                self._record_published_fold(pending.compressed_context_id)
            self._pending_compressions.clear()

        return compressions_processed

    async def flush_pending_compressions(self) -> bool:
        """Execute queued folds after a run, without a wire projection."""
        if not self._pending_compressions or self._state is None:
            return False

        any_processed = False
        for pending in self._pending_compressions:
            try:
                _ctx_id, fold_range = self.execute_fold(
                    pending.marker_id,
                    pending.summary_text,
                    pending.compressed_context_id,
                )
            except ValueError:
                continue
            any_processed = True
            self._ledger.invalidate_for_fold(fold_range)
            self._strategy._note_prefix_rewrite(head=True, before_next_request=True)
            # No request list to count here: the last request's count, less
            # what the same tokenizer counted for the folded messages.
            tokens_before = self._strategy.last_included_tokens
            await self._strategy._notify_compress(
                CompressInfo(
                    compressed_context_id=pending.compressed_context_id,
                    summary=pending.summary_text,
                    freed_messages=len(fold_range),
                    turn_range=_compressed_turn_range(self._state, pending.compressed_context_id),
                    source="agent",
                ),
                trigger="agent_request",
                tokens_before=tokens_before,
                tokens_after=max(0, tokens_before - _fold_tokens(fold_range)),
            )
            self._record_published_fold(pending.compressed_context_id)
        self._pending_compressions.clear()
        if any_processed:
            self._merge_archive(None)
        return any_processed

    async def emergency_compress_oldest_turn(
        self,
        messages: list[Message],
        *,
        usage_pct: float,
        tokens_before: int,
        protect_newest_turn: bool = False,
    ) -> tuple[bool, bool]:
        """Fold the oldest eligible completed turn into a visible summary (Phase 3).

        With *protect_newest_turn* the newest completed turn's marker is never
        the fold point, so that turn stays as it is.

        Returns ``(progressed, compressed)``.  Empty markers can be consumed
        without creating a summary, so ``progressed`` may be true while
        ``compressed`` is false.
        """
        if self._state is None:
            return False, False

        from chrys.service.context.providers.history import CompressibleHistoryProvider, _auto_summary

        info = CompressibleHistoryProvider.list_compressed(self._state)
        markers = info.get("markers", [])
        if not markers:
            return False, False

        # §4.1 defensive eligibility clamp: never fold at or past the
        # current real turn's start.  Resolved over the LIVE WIRE list (the
        # same list the phases compact, in-flight input included) with the
        # provider state supplying marker positions; the resolver locates
        # the current span's boundary message in state and returns it as
        # ``state_boundary`` — the clamp reads that field rather than
        # re-projecting, so projection and clamp cannot drift.  ``None``
        # means the input is not stored yet (fresh prompt) — inputs are
        # stored before outputs, so state holds no current-turn work and
        # there is nothing to protect.  The clamp is re-evaluated on every
        # call, and the Phase 3 loop calls this method again after each
        # progressed fold, so the boundary is never cached across a
        # ``state["messages"]`` replacement.  In mainline resume flows
        # dispatch stripping removes interior markers before this can
        # trigger; the clamp turns "P3 never folds current-task work" into
        # a local guarantee (crash-leftover interior markers included).
        resolved = _resolve_turns(messages, self._state)
        current_span = resolved.current
        state_boundary = current_span.state_boundary if current_span is not None else None

        protected_marker_id: str | None = None
        if protect_newest_turn:
            completed = [
                candidate
                for candidate in markers
                if state_boundary is None
                or (isinstance(position := candidate.get("position"), int) and position < state_boundary)
            ]
            if completed:
                protected_marker_id = completed[-1].get("marker_id")

        marker: dict[str, Any] | None = None
        marker_id = ""
        marker_position = 0
        insertion_index: int | None = None

        for candidate in markers:
            candidate_id = candidate.get("marker_id")
            candidate_position = candidate.get("position")
            if not isinstance(candidate_id, str) or not candidate_id:
                return False, False
            if not isinstance(candidate_position, int):
                return False, False
            if state_boundary is not None and candidate_position >= state_boundary:
                # Folding up to this marker would swallow the current real
                # turn's work (fold ranges are inclusive of the marker).
                continue
            if protected_marker_id is not None and candidate_id == protected_marker_id:
                continue

            fold_range = self._state_fold_range(candidate_position)
            if not fold_range or not self._fold_range_has_content(fold_range):
                return self._discard_state_marker(candidate), False

            candidate_insertion_index = self._visible_fold_insertion_index(
                messages, _FoldMatch.of_range(fold_range, _fold_summary_ids(fold_range))
            )
            if candidate_insertion_index is None:
                continue

            marker = candidate
            marker_id = candidate_id
            marker_position = candidate_position
            insertion_index = candidate_insertion_index
            break

        if marker is None:
            return False, False

        summary = _auto_summary(self._state, marker_position, max_context_tokens=self._strategy.max_context_tokens)

        try:
            await self._strategy._fire_pre_compact("phase3", usage_pct=usage_pct, tokens_before=tokens_before)
            compressed_context_id, fold_range = self.execute_fold(marker_id, summary)
        except ValueError:
            return False, False
        self._record_pre_output_history_len()

        fold_summary_ids = _fold_summary_ids(fold_range)
        insertion_index = self.exclude_wire_matches(
            messages,
            fold_range,
            fold_summary_ids,
            match_summary_refs=True,
            skip_already_excluded=True,
            insertion_index=insertion_index,
        )
        self.insert_and_cache_summary(
            messages,
            compressed_context_id,
            fold_range,
            fold_summary_ids,
            insertion_index,
        )

        for message in fold_range:
            set_excluded(message, excluded=True, reason=_REASON_COMPRESSION)

        self._ledger.invalidate_for_fold(fold_range)
        self._merge_archive(messages)
        self._strategy._note_prefix_rewrite(head=True)

        await self._strategy._notify_compress(
            CompressInfo(
                compressed_context_id=compressed_context_id,
                summary=summary,
                freed_messages=len(fold_range),
                turn_range=_compressed_turn_range(self._state, compressed_context_id),
                source="auto",
            ),
            trigger="usage_threshold",
            tokens_before=tokens_before,
            tokens_after=included_token_count(messages),
        )
        self._record_published_fold(compressed_context_id)

        return True, True

    def _merge_archive(self, messages: list[Message] | None) -> None:
        """Keep the fold summaries within their share of the window, right after a fold.

        Only here: the fold has just rewritten the request from the last
        summary on, so moving the oldest summaries into the archive index
        costs the cached summaries before it and no second rewrite later.
        *messages* is the live request list, or None after a run, where
        only provider state is left to change.
        """
        if self._state is None:
            return
        merge = merge_fold_summaries(
            self._state,
            max_context_tokens=self._strategy.max_context_tokens,
            count_tokens=self._strategy.tokenizer.count_tokens,
        )
        if merge is None or messages is None:
            return
        # A summary the index now lists is flagged excluded on the object the
        # request shares with state, so only the index itself is missing here.
        self._record_pre_output_history_len()
        project_archive(self._state, messages)

    def _record_pre_output_history_len(self) -> None:
        """Remember the provider-state length after request-time compression."""
        if self._state is None:
            return
        state_messages = self._state.get("messages", [])
        if isinstance(state_messages, list):
            self._state[PRE_OUTPUT_HISTORY_LEN_STATE_KEY] = len(state_messages)

    def _state_fold_range(self, marker_position: int) -> list[Message]:
        """Return the pending state fold range ending at *marker_position*."""
        if self._state is None:
            return []
        state_messages = self._state.get("messages", [])
        if not isinstance(state_messages, list) or marker_position < 0 or marker_position >= len(state_messages):
            return []
        # Compaction state is bound to the kernel history provider's Message list.
        typed_state_messages = cast("list[Message]", state_messages)

        fold_start = 0
        for idx, message in enumerate(typed_state_messages):
            if message.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.SUMMARY:
                fold_start = idx + 1
        if fold_start > marker_position:
            return []
        return typed_state_messages[fold_start : marker_position + 1]

    @staticmethod
    def _fold_range_has_content(fold_range: list[Message]) -> bool:
        """Return whether *fold_range* contains something other than turn markers."""
        return any(
            message.additional_properties.get(HistoryMarkerKind.KEY) != HistoryMarkerKind.TURN for message in fold_range
        )

    def _visible_fold_insertion_index(self, messages: list[Message], fold: _FoldMatch) -> int | None:
        """Return where a visible compressed summary should be inserted."""
        for idx, message in enumerate(messages):
            if message.additional_properties.get(EXCLUDED_KEY, False):
                continue
            if fold.matches(message):
                return idx
        return None

    def _discard_state_marker(self, marker: dict[str, Any]) -> bool:
        """Remove an empty state marker without creating a compressed block."""
        if self._state is None:
            return False
        state_messages = self._state.get("messages", [])
        if not isinstance(state_messages, list):
            return False
        # Compaction state is bound to the kernel history provider's Message list.
        typed_state_messages = cast("list[Message]", state_messages)

        position = marker.get("position")
        if (
            isinstance(position, int)
            and 0 <= position < len(state_messages)
            and typed_state_messages[position].additional_properties.get(HistoryMarkerKind.KEY)
            == HistoryMarkerKind.TURN
        ):
            del typed_state_messages[position]
            self._record_pre_output_history_len()
            return True

        marker_id = marker.get("marker_id")
        if not isinstance(marker_id, str) or not marker_id:
            return False
        for idx, message in enumerate(typed_state_messages):
            if message.additional_properties.get("_turn_id") == marker_id:
                del typed_state_messages[idx]
                self._record_pre_output_history_len()
                return True
        return False

    def _find_state_summary(self, compressed_context_id: str) -> Message | None:
        """Return the state summary message for a compressed block."""
        if self._state is None:
            return None
        state_messages = self._state.get("messages", [])
        if not isinstance(state_messages, list):
            return None
        for message in state_messages:
            if (
                isinstance(message, Message)
                and message.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.SUMMARY
                and message.additional_properties.get("_block_id") == compressed_context_id
            ):
                return message
        return None

    def _state_has_compressed_block(self, compressed_context_id: str) -> bool:
        """Return whether the provider state still owns *compressed_context_id*."""
        if self._state is None:
            return False
        compressed_blocks = self._state.get("compressed_msgs", [])
        if not isinstance(compressed_blocks, list):
            return False

        from chrys.service.context.providers.history import CompressedBlock

        for block in compressed_blocks:
            if isinstance(block, CompressedBlock) and block.compressed_context_id == compressed_context_id:
                return True
            if isinstance(block, dict) and block.get("compressed_context_id") == compressed_context_id:
                return True
        return False

    def _prune_stale_compressed_context_cache(self) -> None:
        """Drop cached summaries that no longer match provider state."""
        stale_block_ids = [
            block_id
            for block_id in self._compressed_context_cache
            if self._find_state_summary(block_id) is None or not self._state_has_compressed_block(block_id)
        ]
        for block_id in stale_block_ids:
            self._compressed_context_cache.pop(block_id, None)

    def reinject_summaries(self, messages: list[Message]) -> None:
        """Re-inject emergency compressed summaries into per-call copies."""
        self._reinject_cached_summaries(messages)
        if self._state is not None:
            # After the summaries: the archive index goes in front of them.
            project_archive(self._state, messages)

    def _reinject_cached_summaries(self, messages: list[Message]) -> None:
        if not self._compressed_context_cache:
            return
        self._prune_stale_compressed_context_cache()
        if not self._compressed_context_cache:
            return

        needed: dict[str, int] = {}
        for block_id, cached in self._compressed_context_cache.items():
            if _message_present(messages, cached.summary_message):
                continue

            insertion_index: int | None = None
            fold = _FoldMatch.of_cached(cached)
            for idx, message in enumerate(messages):
                if fold.matches(message):
                    insertion_index = idx
                    break

            if insertion_index is not None:
                needed[block_id] = insertion_index

        for block_id in sorted(needed, key=lambda item: needed[item], reverse=True):
            messages.insert(needed[block_id], self._compressed_context_cache[block_id].summary_message)
