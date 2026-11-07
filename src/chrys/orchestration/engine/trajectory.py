# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Engine-side trajectory recorder: session, turn, branch and fork anchors.

The engine owns one :class:`TrajectoryRecorder` for its lifetime; the
recorder owns one :class:`SessionTrajectory` per current session and the
turn it is currently tracing. Every lifecycle anchor calls in here —
``start()`` binds the session, ``shutdown()`` closes it before the empty
directory cleanup, the runner opens turns, the finalizer closes them,
rollback opens a new branch, fork writes the child's prelude. Everything
is best-effort: a disabled, degraded or missing recorder never changes what
the engine does.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.foundation.trajectory.context import TrajectoryContext
from chrys.foundation.trajectory.envelope import (
    EventDraft,
    MeasurementSource,
    measurement,
    monotonic_now_ns,
    utc_now_rfc3339,
)
from chrys.foundation.trajectory.event_types import (
    EventType,
    ProfileKind,
    RuntimeFinishReason,
    SourceRefKind,
    TurnEndReason,
    TurnSuspendReason,
)
from chrys.foundation.trajectory.fingerprint import DOMAIN_WORKSPACE, fingerprint_text
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.service.trajectory.segmented import emit_segmented, emit_segmented_soon
from chrys.service.trajectory.session import SessionStartInfo, SessionTrajectory
from chrys.service.trajectory.state import TurnRecord, mark_turn_sequence

if TYPE_CHECKING:
    from chrys.service.mutations.types import FileHashDiff
    from chrys.service.profiles.models.schema import ModelProfile
    from chrys.service.state.store import SessionCheckpoint

logger = logging.getLogger(__name__)

ROLLBACK_REASON_USER = "user_rollback"
ROLLBACK_REASON_FRESH_TURN_RETIRED = "fresh_turn_retired"


def exchange_facts(
    *,
    agent_profile_name: str,
    agent_profile_fingerprint: str,
    model_profile: ModelProfile | None,
    model_profile_fingerprint: str,
) -> dict[str, Any]:
    """Static ``model.exchange.*`` facts of one pass: which profiles the model was acquired under."""
    facts: dict[str, Any] = {
        "agent_profile_fingerprint": agent_profile_fingerprint,
        "model_profile_fingerprint": model_profile_fingerprint,
    }
    # An unnamed profile (an engine built before one resolved) has no id to
    # record: an empty id is not a valid identifier, and a rejected event
    # would cost the whole exchange rather than one field.
    if agent_profile_name:
        facts["agent_profile_id"] = agent_profile_name
    if model_profile is not None:
        if model_profile.id:
            facts["model_profile_id"] = model_profile.id
        facts["provider"] = model_profile.provider
        facts["api_style"] = model_profile.api_style
        facts["request_model"] = model_profile.model_id
    return facts


@dataclass(slots=True)
class ActiveTurnTrace:
    """The turn the recorder is currently tracing."""

    turn_id: str
    """The pass id: names this pass's own events in the log."""
    logical_turn_id: str
    """The turn's identity, shared by every pass of the turn (``turn_id`` for a fresh pass)."""
    turn_number: int
    started_monotonic_ns: int
    opened_at: str
    suspended: bool = False
    finished: bool = False
    start_committed: bool = False
    """Set when the opening line took its sequence — the log owns it from then on."""
    start_settlement: asyncio.Task[None] | None = None
    """The emit that writes the opening line; ``start_committed`` is only final once it is done."""
    response_settled: bool = False
    """The post-finalizer fence was queued; prevents duplicate tail markers."""


class TrajectoryRecorder:
    """Best-effort session/turn-level trajectory recording for one engine."""

    def __init__(self) -> None:
        self._trajectory: SessionTrajectory | None = None
        self._turn: ActiveTurnTrace | None = None
        self._close_task: asyncio.Task[None] | None = None
        # Monotonic timestamp of the pending user interrupt request, consumed
        # by the turn close that observes it.
        self._interrupt_requested_ns: int | None = None

    # ------------------------------------------------------------ inspection

    @property
    def trajectory(self) -> SessionTrajectory | None:
        return self._trajectory

    @property
    def current_turn(self) -> ActiveTurnTrace | None:
        return self._turn

    @property
    def current_turn_id(self) -> str | None:
        turn = self._turn
        return turn.turn_id if turn is not None and not turn.finished else None

    def context(self) -> TrajectoryContext | None:
        """The ambient context a model run of the current turn binds."""
        trajectory = self._open()
        if trajectory is None:
            return None
        return trajectory.context(turn_id=self.current_turn_id)

    def finished_turn_context(self) -> TrajectoryContext | None:
        """Context retaining the just-finished turn for its finalizer tail hooks."""
        trajectory = self._open()
        if trajectory is None:
            return None
        turn = self._turn
        return trajectory.context(turn_id=turn.turn_id if turn is not None else None)

    # ------------------------------------------------------------- lifecycle

    def bind_session(
        self,
        *,
        session_id: str,
        session_dir: Path | None,
        write_lock_path: Path | None,
        session_start_info: Callable[[], SessionStartInfo | None],
        on_activation_failed: Callable[[str], None] | None = None,
    ) -> SessionTrajectory:
        """Bind the recorder to *session_id* (idempotent for the same open session).

        Activation — opening the log, taking the writer lease, writing the
        runtime prelude — happens on the first event, so a session that
        never records anything never creates ``trajectory/``.
        """
        existing = self._trajectory
        if existing is not None and existing.session_id == session_id and not existing.is_closed:
            return existing
        self._trajectory = SessionTrajectory(
            session_id=session_id,
            session_dir=session_dir,
            write_lock_path=write_lock_path,
            session_start_info=session_start_info,
            on_activation_failed=on_activation_failed,
        )
        self._turn = None
        self._close_task = None
        self._interrupt_requested_ns = None
        return self._trajectory

    async def close(self, *, reason: str = RuntimeFinishReason.GRACEFUL_SHUTDOWN) -> None:
        """Close the current session's recorder (idempotent)."""
        trajectory = self._trajectory
        if trajectory is None:
            return
        task = self._close_task
        if task is None:
            task = asyncio.ensure_future(self._close_runtime(trajectory, reason=reason))
            self._close_task = task
        # One task for the open turn's terminal and the close behind it: the
        # close is what stops the worker and hands its descriptor and this
        # session's writer lease back, so a caller that stops waiting part way
        # through must not be able to leave the runtime open holding them.
        await asyncio.shield(task)

    async def _close_runtime(self, trajectory: SessionTrajectory, *, reason: str) -> None:
        if not trajectory.is_closed:
            turn = self._turn
            if turn is not None and not turn.finished:
                # A turn still open when the runtime closes is ended by the
                # close itself (the finalizer did not get to run — e.g. the
                # run task was cancelled during shutdown); a hard crash leaves
                # it dangling on purpose, which is how a later runtime tells
                # the two apart.
                end_reason = (
                    TurnEndReason.PROCESS_EXIT
                    if reason == RuntimeFinishReason.GRACEFUL_SHUTDOWN
                    else TurnEndReason.CANCELLED
                )
                await self.turn_finished(end_reason=end_reason)
            try:
                await trajectory.close(reason=reason)
            except Exception:
                logger.warning("Trajectory close failed for session %s", trajectory.session_id, exc_info=True)
        if self._trajectory is not trajectory:
            # A new session bound while this close was still running (its
            # caller was cancelled and moved on): that recorder is live, and
            # clearing it here would unbind a runtime nobody asked to close.
            return
        self._trajectory = None
        self._turn = None
        self._interrupt_requested_ns = None

    # ----------------------------------------------------------------- turns

    async def turn_started(
        self,
        *,
        turn_id: str,
        logical_turn_id: str,
        turn_number: int,
        is_retry: bool,
        agent_profile_fingerprint: str,
        model_profile_fingerprint: str,
        primary_cwd: str,
        history_state: dict[str, Any] | None,
        opening_item_id: str | None = None,
        preparation_scope_operation_id: str | None = None,
    ) -> str | None:
        """Open the pass about to run under *turn_id*, recording that it belongs to *logical_turn_id*.

        The ids come from the caller: a turn's identity has to exist before
        the log can take it, so a runtime whose trajectory never activates
        still registers and publishes the same turn. A retry pass mints a
        pass id of its own while the logical turn — the id a client holds
        and a rollback supersedes — stays the first pass's. ``workspace_revision``
        is the keyed fingerprint of *primary_cwd* — the same value
        ``session.started`` carries — so a mid-session workspace change shows
        up as a different revision without the path itself ever reaching the
        log.
        """
        trajectory = self._open()
        if trajectory is None:
            return None
        trace = ActiveTurnTrace(
            turn_id=turn_id,
            logical_turn_id=logical_turn_id,
            turn_number=turn_number,
            started_monotonic_ns=monotonic_now_ns(),
            opened_at=utc_now_rfc3339(),
        )
        self._turn = trace
        payload: dict[str, Any] = {
            "turn_id": trace.turn_id,
            "turn_number": turn_number,
            "opened_at": trace.opened_at,
            "agent_profile_fingerprint": agent_profile_fingerprint,
            "model_profile_fingerprint": model_profile_fingerprint,
            "is_retry": is_retry,
            # Every pass names the logical turn it belongs to, the pass's own id
            # included: a consumer reads one field and never has to infer the turn
            # from the pass id's presence or absence.
            "logical_turn_id": logical_turn_id,
        }
        if opening_item_id is not None:
            payload["opening_item_id"] = opening_item_id
        if preparation_scope_operation_id is not None:
            payload["preparation_scope_operation_id"] = preparation_scope_operation_id
        draft = EventDraft(
            event_type=EventType.TURN_STARTED,
            actor=trajectory.main_actor,
            turn_id=trace.turn_id,
            payload=payload,
            occurred_at=trace.opened_at,
            monotonic_ns=trace.started_monotonic_ns,
        )

        def _payload(sequence: int) -> Mapping[str, Any]:
            # Resolved at write time: the fingerprint key is loaded by the
            # activation this very emit may have triggered, and the registry
            # names the sequence the turn.started line actually landed at.
            # Reaching here is also what makes the turn real to the log: the
            # writer takes the sequence and queues the line in one locked step.
            trace.start_committed = True
            key = trajectory.fingerprint_key
            payload["workspace_revision"] = (
                fingerprint_text(key, DOMAIN_WORKSPACE, primary_cwd) if key is not None and primary_cwd else ""
            )
            if history_state is not None:
                mark_turn_sequence(history_state, logical_turn_id, sequence)
            return payload

        # The opening line rides a task of the recorder's own, shielded from
        # this caller: an interrupt here unwinds the caller while the emit it
        # started runs on — a cancelled ``to_thread`` activation goes on
        # opening the log and writing the draft that triggered it anyway.
        # Whether the line took a sequence therefore has no answer while this
        # task is alive, only once it settles, which is what the terminal
        # waits for.
        settlement = asyncio.create_task(_emit(trajectory, draft, payload_factory=_payload))
        trace.start_settlement = settlement
        await asyncio.shield(settlement)
        return trace.turn_id

    async def turn_finished(self, *, end_reason: str) -> None:
        """Close the current turn with a terminal ``end_reason`` (once)."""
        trajectory = self._open()
        turn = self._turn
        if trajectory is None or turn is None or turn.finished:
            return
        if not turn.start_committed and turn.start_settlement is not None:
            # An opening line that has not taken a sequence *yet* and one that
            # never will read the same from here, and only the emit itself
            # tells them apart — by settling. A close that guessed would be
            # racing a thread that is still writing, so it waits instead.
            await asyncio.wait([turn.start_settlement])
        if not turn.start_committed:
            # Settled without a sequence: nothing is left that could still
            # give the start one. A terminal against a start nothing recorded
            # is not a shape readers handle; an unclosed span is one they
            # already do, so the turn is dropped rather than closed. Only the
            # terminal is held to this — the turn's other events name their
            # turn, they do not close it.
            self._turn = None
            return
        now_ns = monotonic_now_ns()
        closed_at = utc_now_rfc3339()
        requested_ns, self._interrupt_requested_ns = self._interrupt_requested_ns, None
        if end_reason == TurnEndReason.INTERRUPTED and requested_ns is not None:
            # "User pressed interrupt" and "the run actually stopped" are two
            # moments; the second is observed here, at the turn's close.
            await _emit(
                trajectory,
                EventDraft(
                    event_type=EventType.INTERRUPT_OBSERVED,
                    actor=trajectory.main_actor,
                    turn_id=turn.turn_id,
                    payload={
                        "target_operation_id": None,
                        "target_turn_id": turn.turn_id,
                        "observed_at": closed_at,
                        "observed_after_ms": _elapsed_ms(requested_ns, now_ns),
                    },
                    measurements={
                        "/payload/observed_after_ms": measurement(MeasurementSource.MONOTONIC_CLOCK, method_version=1)
                    },
                    occurred_at=closed_at,
                    monotonic_ns=now_ns,
                ),
            )
        # Marked closed only once nothing is left to await before the terminal
        # line: an interrupt in the observation above would otherwise leave a
        # turn the shutdown repair then skips as already finished.
        turn.finished = True
        await _emit(
            trajectory,
            EventDraft(
                event_type=EventType.TURN_FINISHED,
                actor=trajectory.main_actor,
                turn_id=turn.turn_id,
                payload={
                    "turn_id": turn.turn_id,
                    "closed_at": closed_at,
                    "duration_ms": _elapsed_ms(turn.started_monotonic_ns, now_ns),
                    "end_reason": end_reason,
                },
                measurements={"/payload/duration_ms": measurement(MeasurementSource.MONOTONIC_CLOCK, method_version=1)},
                occurred_at=closed_at,
                monotonic_ns=now_ns,
            ),
        )

    async def turn_response_settled(
        self,
        *,
        outcome: str,
        drained_scopes: list[str],
        waited_hook_operation_ids: list[str],
    ) -> None:
        """Write the zero-weight response fence after every finalizer tail step."""
        prepared = self._prepare_turn_response_settled(
            outcome=outcome,
            drained_scopes=drained_scopes,
            waited_hook_operation_ids=waited_hook_operation_ids,
        )
        if prepared is not None:
            context, payload = prepared
            await emit_segmented(
                context,
                event_type=EventType.TURN_RESPONSE_SETTLED,
                payload=payload,
                array_fields={"/payload/waited_hook_operation_ids": waited_hook_operation_ids},
            )

    def turn_response_settled_soon(
        self,
        *,
        outcome: str,
        drained_scopes: list[str],
        waited_hook_operation_ids: list[str],
    ) -> None:
        """Queue the response fence while unwinding from cancellation."""
        prepared = self._prepare_turn_response_settled(
            outcome=outcome,
            drained_scopes=drained_scopes,
            waited_hook_operation_ids=waited_hook_operation_ids,
        )
        if prepared is not None:
            context, payload = prepared
            emit_segmented_soon(
                context,
                event_type=EventType.TURN_RESPONSE_SETTLED,
                payload=payload,
                array_fields={"/payload/waited_hook_operation_ids": waited_hook_operation_ids},
            )

    def _prepare_turn_response_settled(
        self,
        *,
        outcome: str,
        drained_scopes: list[str],
        waited_hook_operation_ids: list[str],
    ) -> tuple[TrajectoryContext, dict[str, Any]] | None:
        trajectory = self._open()
        turn = self._turn
        if trajectory is None or turn is None or not turn.finished or turn.response_settled:
            return None
        turn.response_settled = True
        return (
            trajectory.context(turn_id=turn.turn_id),
            {
                "outcome": outcome,
                "drained_scopes": list(drained_scopes),
                "waited_hook_operation_count": len(waited_hook_operation_ids),
            },
        )

    def interrupt_requested_soon(self, *, source: str = "user", scope: str = "turn") -> None:
        """Record a user interrupt against the open turn (queued in order, ack not awaited).

        The line takes its sequence here, ahead of the terminals the interrupt
        is about to produce, so the log still reads in the order things
        happened. The acknowledgement is not waited on: everything this
        interrupt has to reach — the executor, every live sub-agent — comes
        after the call, and a backend that has stopped acknowledging writes
        must not keep tools running while it is waited out.
        """
        trajectory = self._open()
        turn = self._turn
        if trajectory is None or turn is None or turn.finished:
            return
        now_ns = monotonic_now_ns()
        if self._interrupt_requested_ns is None:
            self._interrupt_requested_ns = now_ns
        requested_at = utc_now_rfc3339()
        _emit_soon(
            trajectory,
            EventDraft(
                event_type=EventType.INTERRUPT_REQUESTED,
                actor=trajectory.main_actor,
                turn_id=turn.turn_id,
                payload={
                    "source": source,
                    "scope": scope,
                    "target_operation_id": None,
                    "target_turn_id": turn.turn_id,
                    "reason_code": "user_interrupt",
                    "requested_at": requested_at,
                },
                occurred_at=requested_at,
                monotonic_ns=now_ns,
            ),
        )

    async def turn_suspended(self) -> None:
        trajectory = self._open()
        turn = self._turn
        if trajectory is None or turn is None or turn.finished or turn.suspended:
            return
        turn.suspended = True
        await _emit(
            trajectory,
            EventDraft(
                event_type=EventType.TURN_SUSPENDED,
                actor=trajectory.main_actor,
                turn_id=turn.turn_id,
                payload={"turn_id": turn.turn_id, "reason": TurnSuspendReason.AWAITING_SUB_AGENTS},
            ),
        )

    async def turn_resumed(self) -> None:
        trajectory = self._open()
        turn = self._turn
        if trajectory is None or turn is None or turn.finished or not turn.suspended:
            return
        turn.suspended = False
        await _emit(
            trajectory,
            EventDraft(
                event_type=EventType.TURN_RESUMED,
                actor=trajectory.main_actor,
                turn_id=turn.turn_id,
                payload={"turn_id": turn.turn_id},
            ),
        )

    async def checkpoint(self) -> None:
        """Write a durable ``trajectory.checkpoint`` (after a turn-end save)."""
        trajectory = self._open()
        if trajectory is None or not trajectory.is_active:
            return
        try:
            await trajectory.checkpoint()
        except Exception:
            logger.debug("Trajectory checkpoint failed", exc_info=True)

    # --------------------------------------------------------- derived facts

    async def mutation_summary(
        self,
        summary: Mapping[str, FileHashDiff],
        *,
        checkpoint: SessionCheckpoint | None,
    ) -> None:
        """Emit the per-turn ``tool.mutation_batch.summary`` (counts only)."""
        trajectory = self._open()
        turn = self._turn
        if trajectory is None or turn is None or not summary:
            return
        created = modified = deleted = net_zero = proven = assumed = 0
        for diff in summary.values():
            if diff.is_net_zero:
                net_zero += 1
            if not diff.before_exists and diff.after_exists:
                created += 1
            elif diff.before_exists and not diff.after_exists:
                deleted += 1
            else:
                modified += 1
            if diff.inferred or diff.content_unavailable:
                assumed += 1
            else:
                proven += 1
        payload: dict[str, Any] = {
            "turn_id": turn.turn_id,
            "files_touched": len(summary),
            "create": created,
            "modify": modified,
            "delete": deleted,
            "net_zero_count": net_zero,
            "proven_count": proven,
            "assumed_count": assumed,
            "derived_at": utc_now_rfc3339(),
        }
        if checkpoint is not None and checkpoint.session_checkpoint_id:
            payload["source_ref"] = {
                "kind": SourceRefKind.SESSION_CHECKPOINT,
                "id": checkpoint.session_checkpoint_id,
                "hash": checkpoint.content_hash,
            }
        await _emit(
            trajectory,
            EventDraft(
                event_type=EventType.TOOL_MUTATION_BATCH_SUMMARY,
                actor=trajectory.main_actor,
                turn_id=turn.turn_id,
                operation_id=new_analytics_id(),
                payload=payload,
                measurements={
                    "/payload/files_touched": measurement(MeasurementSource.DERIVED_FROM_SESSION, method_version=1)
                },
            ),
        )

    # ----------------------------------------------- profile / branch / fork

    async def profile_switched(self, *, kind: str, from_fingerprint: str, to_fingerprint: str) -> None:
        trajectory = self._open()
        if trajectory is None or from_fingerprint == to_fingerprint:
            return
        if kind not in {ProfileKind.AGENT, ProfileKind.MODEL}:
            return
        await _emit(
            trajectory,
            EventDraft(
                event_type=EventType.PROFILE_SWITCHED,
                actor=trajectory.main_actor,
                turn_id=self.current_turn_id,
                payload={"kind": kind, "from_fingerprint": from_fingerprint, "to_fingerprint": to_fingerprint},
            ),
        )

    async def ensure_active(self) -> None:
        """Open the log now, before the caller takes the session write lock.

        Activation recovers a torn tail under that same lock and does it from
        a worker thread, so a log first opened *inside* the lock waits for the
        holder that is waiting for it: the wait ends at the activation timeout
        and the log gives up on recording. The flows that reset a session hold
        the lock and record across it, so they open it first.
        """
        trajectory = self._open()
        if trajectory is None:
            return
        await trajectory.ensure_active()

    async def rollback(
        self,
        *,
        superseded_turns: list[TurnRecord],
        target_turns: list[TurnRecord],
    ) -> None:
        """Open a new branch: ``session.rollback`` then ``branch.superseded``.

        A snapshot rollback calls this after its atomic swap and before
        restore; a welcome rollback calls it after the reset has committed and
        rebound the recorder. In both cases the session write lock is released
        first. The two lists are the registry as it reads *before* the
        rollback — the turns it supersedes and the turns it keeps — so the
        range is computed from ids rather than inferred from ordinals, which
        drift apart from the log after a failed or cancelled turn.
        """
        await self._supersede_branch(
            superseded_turns=superseded_turns,
            target_turns=target_turns,
            reason_code=ROLLBACK_REASON_USER,
        )

    async def retire_turn(self, *, retired_turn: TurnRecord, target_turns: list[TurnRecord]) -> None:
        """Retire an empty turn without attributing the branch change to the user."""
        if retired_turn.started_sequence is None:
            # Inherited from a fork and never run here: this log holds nothing to supersede.
            return
        await self._supersede_branch(
            superseded_turns=[retired_turn],
            target_turns=target_turns,
            reason_code=ROLLBACK_REASON_FRESH_TURN_RETIRED,
        )

    async def _supersede_branch(
        self,
        *,
        superseded_turns: list[TurnRecord],
        target_turns: list[TurnRecord],
        reason_code: str,
    ) -> None:
        """Keep range and branch events paired for the trajectory projection."""
        trajectory = self._open()
        if trajectory is None:
            return
        # The branch being superseded is only known once the log is open, and
        # a rollback can be the first thing a runtime records: activating
        # lazily on the emit below would name no old branch at all and let
        # activation overwrite the new one.
        await trajectory.ensure_active()
        old_branch = trajectory.branch_id
        new_branch = new_analytics_id()
        # The superseded turns are a suffix of the registry, so the earliest
        # one that ran here opens the range that covers them all. A turn
        # inherited from a fork and never re-run locally has no sequence and
        # is named in the result only — the log holds nothing to supersede.
        superseded_from_sequence = _earliest_sequence(superseded_turns)
        target = target_turns[-1] if target_turns else None
        payload: dict[str, Any] = {"new_branch_id": new_branch, "reason_code": reason_code}
        if old_branch is not None:
            payload["old_branch_id"] = old_branch
        if superseded_from_sequence is not None:
            payload["superseded_from_sequence"] = superseded_from_sequence
        if target is not None:
            payload["target_turn_id"] = target.turn_id

        def _payload(sequence: int) -> Mapping[str, Any]:
            if superseded_from_sequence is not None:
                payload["superseded_to_sequence"] = sequence - 1
            return payload

        # The turn being traced (if any) belongs to the superseded branch.
        self._turn = None
        self._interrupt_requested_ns = None
        trajectory.set_branch_id(new_branch)
        await _emit(
            trajectory,
            EventDraft(event_type=EventType.SESSION_ROLLBACK, actor=trajectory.main_actor),
            payload_factory=_payload,
        )
        if old_branch is not None:
            await _emit(
                trajectory,
                EventDraft(
                    event_type=EventType.BRANCH_SUPERSEDED,
                    actor=trajectory.main_actor,
                    payload={"branch_id": old_branch, "superseded_by": new_branch},
                ),
            )

    async def fork(
        self,
        *,
        origin_session_id: str,
        fork_session_id: str,
        fork_session_dir: Path,
        fork_write_lock_path: Path | None,
        session_start_info: SessionStartInfo | None,
    ) -> None:
        """Write the fork's own opening runtime (``session.forked``).

        The directory arrives resolved: the state store owns the layout, so
        nothing here may derive one session's path from another's.
        """
        from chrys.service.trajectory.fork import record_fork

        trajectory = self._trajectory
        forked_at = trajectory.last_assigned_sequence() if trajectory is not None else 0
        try:
            await record_fork(
                fork_session_id=fork_session_id,
                fork_session_dir=fork_session_dir,
                fork_write_lock_path=fork_write_lock_path,
                origin_session_id=origin_session_id,
                forked_at_sequence=forked_at,
                session_start_info=session_start_info,
            )
        except Exception:
            logger.warning("Failed to record trajectory fork for %s", fork_session_id, exc_info=True)

    # ------------------------------------------------------------- internals

    def _open(self) -> SessionTrajectory | None:
        trajectory = self._trajectory
        if trajectory is None or trajectory.is_closed:
            return None
        return trajectory


def _earliest_sequence(records: list[TurnRecord]) -> int | None:
    """The lowest sequence any of *records* started at, if one ran in this session."""
    sequences = [record.started_sequence for record in records if record.started_sequence is not None]
    return min(sequences) if sequences else None


def _elapsed_ms(start_ns: int, end_ns: int) -> int:
    return max(0, (end_ns - start_ns) // 1_000_000)


async def _emit(
    trajectory: SessionTrajectory,
    draft: EventDraft,
    *,
    payload_factory: Callable[[int], Mapping[str, Any]] | None = None,
) -> None:
    try:
        await trajectory.emit(draft, payload_factory=payload_factory)
    except Exception:
        logger.debug("Trajectory emit failed (%s)", draft.event_type, exc_info=True)


def _emit_soon(trajectory: SessionTrajectory, draft: EventDraft) -> None:
    """Queue *draft* in sequence order without waiting for its acknowledgement."""
    try:
        trajectory.emit_soon(draft)
    except Exception:
        logger.debug("Trajectory emit failed (%s)", draft.event_type, exc_info=True)
