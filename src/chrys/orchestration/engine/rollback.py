# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Engine-internal companion module for rollback orchestration.

Pure rollback-file helpers live in :mod:`chrys.service.mutations.snapshot_files`.
This module owns the engine-facing workflow: validate the requested
target, optionally revert file mutations, promote a saved snapshot, and
reload the session while suppressing stale in-memory saves.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.foundation.events.types import Error, RollbackResult, SessionRestore, UserRollback, Warning
from chrys.foundation.i18n import DisplayBlock, msg
from chrys.foundation.util.async_tasks import await_task_quiescence
from chrys.foundation.util.lock import FileLock
from chrys.orchestration.engine.state.machine import EngineState
from chrys.service.mutations.snapshot_files import (
    rollback_snapshot_for_target,
    rollback_snapshot_paths,
    snapshot_target_turn,
    snapshot_target_turns,
    write_rollback_snapshot,
)
from chrys.service.mutations.snapshot_files import (
    turn_prompt_previews as collect_turn_prompt_previews,
)
from chrys.service.mutations.workspace_changes import format_partial_revert_notice, format_retained_changes_notice
from chrys.service.state.store import SESSION_BACKUP_FILE_NAME, SESSION_RECOVERY_FILE_NAME, recorded_surface
from chrys.service.trajectory.state import TurnRecord, turn_records

if TYPE_CHECKING:
    from chrys.foundation.config.settings_store import SettingsHandle
    from chrys.foundation.events.bus import EventBus
    from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
    from chrys.orchestration.engine.session_lifecycle import SessionLifecycle
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.orchestration.engine.state.current_agent import CurrentAgent
    from chrys.orchestration.engine.state.lifecycle_permits import LifecyclePermits
    from chrys.orchestration.engine.state.machine import EngineStateMachine
    from chrys.orchestration.engine.state.session_writer import SessionWriter
    from chrys.orchestration.engine.trajectory import TrajectoryRecorder
    from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker
    from chrys.service.session.history import SessionHistoryManager


logger = logging.getLogger(__name__)

_ROLLBACK_NO_SESSION = msg(
    "rollback.no_session",
    fallback="No active session to discard turns from.",
)
_ROLLBACK_CONVERSATION_CHANGED = msg(
    "rollback.conversation_changed",
    fallback="Discard cancelled because the conversation changed after the picker was loaded.",
)
_ROLLBACK_CONVERSATION_ADVANCED = msg(
    "rollback.conversation_advanced",
    fallback="Discard cancelled because the conversation advanced from turn {expected_turn} to turn {current_turn}.",
)
_ROLLBACK_RUNTIME_CHANGED = msg(
    "rollback.runtime_changed",
    fallback="Discard cancelled because the workspace or runtime changed after the picker was loaded.",
)
_ROLLBACK_RELATIVE_TURNS_INVALID = msg(
    "rollback.relative_turns_invalid",
    fallback="relative_turns must be positive.",
)
_ROLLBACK_TURNS_UNAVAILABLE = msg(
    "rollback.turns_unavailable",
    fallback="Cannot discard {requested_turns} turns; the session currently has {current_turns}.",
)
_ROLLBACK_TARGET_TURN_INVALID = msg(
    "rollback.target_turn_invalid",
    fallback="target_turn must be >= 0.",
)
_ROLLBACK_TURN_UNAVAILABLE = msg(
    "rollback.turn_unavailable",
    fallback="Cannot discard turns after turn {target_turn}; available targets: {available}",
)
_ROLLBACK_RESET_FAILED = msg(
    "rollback.reset_failed",
    fallback="Could not discard all turns because the session state is busy.",
)
_ROLLBACK_SNAPSHOT_MISSING = msg(
    "rollback.snapshot_missing",
    fallback="Snapshot for turn {target_turn} is missing.",
)
_ROLLBACK_SWAP_LOCKED = msg(
    "rollback.swap_locked",
    fallback="Timed out waiting for session lock: {detail}",
    multiline=True,
)
_ROLLBACK_SWAP_FAILED = msg(
    "rollback.swap_failed",
    fallback="Failed to restore snapshot: {detail}",
    multiline=True,
)
_ROLLBACK_SESSION_CHANGED = msg(
    "rollback.session_changed",
    fallback="Discard cancelled because the active session changed.",
)
_ROLLBACK_REFUSED = msg(
    "rollback.refused",
    fallback="Cannot discard turns in state {state}.",
)


@dataclass(slots=True)
class _RollbackLockLease:
    """Idempotent owner for a pre-commit rollback write lock."""

    lock: FileLock | None = None

    @property
    def held(self) -> bool:
        """Return whether the lease still owns its lock."""
        return self.lock is not None

    def release(self) -> None:
        """Release the lock once, tolerating defensive cleanup paths."""
        lock = self.lock
        self.lock = None
        if lock is not None:
            lock.release()


def _read_envelope_meta(path: Path) -> dict[str, Any]:
    """The envelope's ``meta``; empty when the file is missing or unreadable."""
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return {}
    meta = envelope.get("meta") if isinstance(envelope, dict) else None
    return meta if isinstance(meta, dict) else {}


def _read_session_overlays(session_file: Path) -> dict[str, str]:
    """Capture session-scoped meta before a snapshot overwrites ``session.json``.

    These describe the session, not the turns it holds, so the current
    value wins over whatever the snapshot carried: ``custom_title`` is the
    user's pin (or explicit clear — an empty string must survive too, or a
    pre-clear snapshot would resurrect the pin), and ``last_surface`` names
    the surface whose turn last worked in the session, which a rollback,
    not being a turn, never changes.  That turn may be one a crash cut
    short, recorded only in a newer sidecar the restore after the swap
    discards, so the surface is read as the store would read it.
    ``generated_title`` is deliberately NOT preserved: it summarizes the
    conversation, and after a rollback the snapshot's own value is the one
    that describes the restored turns — the newer title describes history
    that no longer exists.  Each value is captured verbatim; one the files
    never recorded (or an unreadable envelope) yields nothing for it.
    """
    meta = _read_envelope_meta(session_file)
    overlays = {"custom_title": value} if isinstance(value := meta.get("custom_title"), str) else {}
    surface = recorded_surface(meta, _read_envelope_meta(session_file.with_name(SESSION_RECOVERY_FILE_NAME)))
    if surface is not None:
        overlays["last_surface"] = surface
    return overlays


def _reapply_session_overlays(session_file: Path, overlays: dict[str, str]) -> None:
    """Merge preserved session-scoped meta back into the restored snapshot, best-effort."""
    if not overlays:
        return
    from chrys.foundation.platform.files import atomic_write_text

    try:
        envelope = json.loads(session_file.read_text(encoding="utf-8"))
        meta = envelope.get("meta") if isinstance(envelope, dict) else None
        if not isinstance(meta, dict):
            return
        meta.update(overlays)
        atomic_write_text(session_file, json.dumps(envelope, indent=2, ensure_ascii=False))
    except OSError, ValueError:
        logger.warning("Failed to preserve session metadata across rollback for %s", session_file, exc_info=True)


def capture_snapshot_writer(session: ActiveSession, settings_handle: SettingsHandle) -> Callable[[], None]:
    """Freeze snapshot metadata while deferring path resolution and file I/O."""
    session_id = session.session_id
    turn_number = session.turn_number
    keep = settings_handle.settings.rollback_snapshots_keep
    session_dir_for = session.session_dir_for
    lock_path_for = session.session_write_lock_path

    def write() -> None:
        session_dir = session_dir_for(session_id) if session_id else None
        lock_path = lock_path_for(session_id) if session_id else None
        write_rollback_snapshot(
            session_dir=session_dir,
            session_id=session_id,
            turn_number=turn_number,
            keep=keep,
            lock_path=lock_path,
        )

    return write


class RollbackController:
    """Validates rollback requests and restores session and workspace state."""

    def __init__(
        self,
        *,
        session: ActiveSession,
        current: CurrentAgent,
        permits: LifecyclePermits,
        writer: SessionWriter,
        turn_state: TurnRuntimeState,
        lifecycle: SessionLifecycle,
        bus: EventBus,
        history: SessionHistoryManager,
        fsm: EngineStateMachine,
        workspace_change_tracker: WorkspaceChangeTracker,
        trajectory_recorder: TrajectoryRecorder,
        settings_handle: SettingsHandle,
    ) -> None:
        self._session = session
        self._current = current
        self._permits = permits
        self._writer = writer
        self._turn_state = turn_state
        self._lifecycle = lifecycle
        self._bus = bus
        self._history = history
        self._fsm = fsm
        self._workspace_change_tracker = workspace_change_tracker
        self._trajectory_recorder = trajectory_recorder
        self._settings_handle = settings_handle

    async def begin_rollback_projection(
        self,
        *,
        session_id: str | None,
        session_generation: int,
    ) -> str | None:
        """Fence prompt admission while the TUI projects live rollback state."""
        if self._permits.session_generation != session_generation:
            return None
        if session_id is not None and self._session.session_id != session_id:
            return None
        return await self._permits.prepare_session_transition_if_current(
            "rollback_projection",
            session_id=self._session.session_id,
            session_generation=session_generation,
        )

    def finish_rollback_projection(self, owner: str) -> None:
        """Release a read-only rollback projection fence without committing it."""
        self._permits.finish_session_transition(owner)

    def write_snapshot(self) -> None:
        """Copy the current ``session.json`` to ``snapshots/turn_N.json``."""
        capture_snapshot_writer(self._session, self._settings_handle)()

    def turn_prompt_previews(self) -> dict[int, str]:
        """Return ``{turn_number: first_user_prompt}`` for every known turn."""
        if not self._history.is_bound:
            return {}
        blocks = self._history.state.get("compressed_msgs", []) or []
        return collect_turn_prompt_previews(list(self._history.messages), blocks)

    def first_rolled_back_user_text(self, target_turn: int) -> str:
        """Return the first user prompt that rollback to ``target_turn`` discards."""
        previews = self.turn_prompt_previews()
        discarded_turns = [turn for turn in previews if turn > target_turn]
        return previews[min(discarded_turns)] if discarded_turns else ""

    def available_turns(self) -> list[int]:
        """Return turn-counts that can be targeted by a rollback."""
        session_dir = self._session.session_dir
        if session_dir is None:
            return []
        turns = snapshot_target_turns(session_dir)
        current_turn = self._session.turn_number
        if current_turn > 0:
            turns = {turn for turn in turns if turn < current_turn}
        has_history = bool(
            (self._session.mutation_tracker is not None and self._session.mutation_tracker.get_all_turns())
            or (self._history.is_bound and self._history.messages)
        )
        if not has_history:
            return []
        # Back to the session start needs no snapshot: it stays reachable after pruning.
        turns.add(0)
        return sorted(turns)

    async def on_user_rollback(
        self,
        event: UserRollback,
        *,
        atomic_copy_file: Callable[[Path, Path], None],
        lock_timeout_seconds: float,
    ) -> None:
        """Handle rollback under an exclusive prompt/session-transition boundary."""
        sid = self._session.session_id
        session_generation = self._permits.session_generation
        if sid is None:
            await self._bus.publish(
                Warning(
                    code="rollback_no_session",
                    message="No active session to roll back.",
                    display_message=_ROLLBACK_NO_SESSION.bind(),
                ),
            )
            return
        if await self._cancel_if_rollback_owner_changed(event, sid, session_generation):
            return
        if await self._refuse_if_rollback_state_invalid(sid):
            return

        transition_owner = await self._permits.prepare_session_transition_if_current(
            "rollback",
            session_id=sid,
            session_generation=session_generation,
        )
        if transition_owner is None:
            await self._cancel_if_rollback_owner_changed(event, sid, session_generation)
            return
        try:
            prepared = await self._prepare_user_rollback_with_permit(
                event,
                sid=sid,
                session_generation=session_generation,
            )
            if prepared is None:
                return
            target_turn, rolled_back_user_text = prepared
            await self._execute_user_rollback_with_permit(
                event,
                sid=sid,
                session_generation=session_generation,
                transition_owner=transition_owner,
                target_turn=target_turn,
                rolled_back_user_text=rolled_back_user_text,
                atomic_copy_file=atomic_copy_file,
                lock_timeout_seconds=lock_timeout_seconds,
            )
        finally:
            self._permits.finish_session_transition(transition_owner)

    async def _prepare_user_rollback_with_permit(
        self,
        event: UserRollback,
        *,
        sid: str,
        session_generation: int,
    ) -> tuple[int, str] | None:
        """Quiesce and validate rollback without invalidating the current generation."""
        lifecycle_task = self._turn_state.lease.run_task
        await self._permits.wait_for_agent_load_idle()
        target_turn = event.target_turn
        if await self._cancel_if_rollback_owner_changed(event, sid, session_generation):
            return None
        if await self._refuse_if_rollback_state_invalid(sid):
            return None

        # IDLE is reached before final persistence and after-turn hooks finish.
        # New admission is fenced and older reserved admissions have drained, so
        # this task cannot be replaced while rollback waits for durable quiescence.
        await await_task_quiescence(lifecycle_task)
        if await self._cancel_if_rollback_owner_changed(event, sid, session_generation):
            return None
        if await self._refuse_if_rollback_state_invalid(sid):
            return None

        if (
            event.expected_conversation_revision is not None
            and self._turn_state.lease.conversation_revision != event.expected_conversation_revision
        ):
            await self._bus.publish(
                Warning(
                    code="rollback_conversation_changed",
                    message="Rollback cancelled because the conversation changed after the picker was loaded.",
                    display_message=_ROLLBACK_CONVERSATION_CHANGED.bind(),
                    session_id=sid,
                ),
            )
            return None

        if event.expected_current_turn is not None and self._session.turn_number != event.expected_current_turn:
            await self._bus.publish(
                Warning(
                    code="rollback_conversation_changed",
                    message=(
                        "Rollback cancelled because the conversation advanced "
                        f"from turn {event.expected_current_turn} to turn {self._session.turn_number}."
                    ),
                    display_message=_ROLLBACK_CONVERSATION_ADVANCED.bind(
                        expected_turn=event.expected_current_turn,
                        current_turn=self._session.turn_number,
                    ),
                    session_id=sid,
                ),
            )
            return None

        if (
            event.expected_build_generation is not None
            and self._permits.build_generation != event.expected_build_generation
        ) or (
            event.expected_workspace_cwd is not None and self._session.workspace_cwd() != event.expected_workspace_cwd
        ):
            await self._bus.publish(
                Warning(
                    code="rollback_runtime_changed",
                    message="Rollback cancelled because the workspace or runtime changed after the picker was loaded.",
                    display_message=_ROLLBACK_RUNTIME_CHANGED.bind(),
                    session_id=sid,
                ),
            )
            return None

        if event.relative_turns is not None:
            if event.relative_turns <= 0:
                await self._bus.publish(
                    Warning(
                        code="rollback_invalid_turn",
                        message="relative_turns must be positive.",
                        display_message=_ROLLBACK_RELATIVE_TURNS_INVALID.bind(),
                        session_id=sid,
                    ),
                )
                return None
            target_turn = self._session.turn_number - event.relative_turns
            if target_turn < 0:
                await self._bus.publish(
                    Warning(
                        code="rollback_unavailable",
                        message=(
                            f"Cannot roll back {event.relative_turns} turns; "
                            f"the session currently has {self._session.turn_number}."
                        ),
                        display_message=_ROLLBACK_TURNS_UNAVAILABLE.bind(
                            requested_turns=event.relative_turns,
                            current_turns=self._session.turn_number,
                        ),
                        session_id=sid,
                    ),
                )
                return None

        if target_turn < 0:
            await self._bus.publish(
                Warning(
                    code="rollback_invalid_turn",
                    message="target_turn must be >= 0.",
                    display_message=_ROLLBACK_TARGET_TURN_INVALID.bind(),
                    session_id=sid,
                ),
            )
            return None

        available = self.available_turns()
        if target_turn not in available:
            await self._bus.publish(
                Warning(
                    code="rollback_unavailable",
                    message=f"Cannot roll back to turn {target_turn}; available turns: {available}",
                    display_message=_ROLLBACK_TURN_UNAVAILABLE.bind(
                        target_turn=target_turn,
                        available=str(available),
                    ),
                    session_id=sid,
                ),
            )
            return None

        return target_turn, self.first_rolled_back_user_text(target_turn)

    async def _execute_user_rollback_with_permit(
        self,
        event: UserRollback,
        *,
        sid: str,
        session_generation: int,
        transition_owner: str,
        target_turn: int,
        rolled_back_user_text: str,
        atomic_copy_file: Callable[[Path, Path], None],
        lock_timeout_seconds: float,
    ) -> None:
        """Secure rollback resources, then commit immediately before mutation."""

        welcome_reset_lock: FileLock | None = None
        if target_turn == 0:
            await self._writer.flush()
            reset_lock_acquired, welcome_reset_lock = self._acquire_welcome_reset_write_lock(sid, lock_timeout_seconds)
            if not reset_lock_acquired:
                await self._bus.publish(
                    Error(
                        code="rollback_reset_failed",
                        message="Rollback to welcome could not reset the session because the session state is busy.",
                        display_message=_ROLLBACK_RESET_FAILED.bind(),
                        session_id=sid,
                    ),
                )
                return

        welcome_lock_lease = _RollbackLockLease(welcome_reset_lock)
        try:
            await self._execute_user_rollback_after_resource_preflight(
                event,
                sid=sid,
                session_generation=session_generation,
                transition_owner=transition_owner,
                target_turn=target_turn,
                rolled_back_user_text=rolled_back_user_text,
                atomic_copy_file=atomic_copy_file,
                lock_timeout_seconds=lock_timeout_seconds,
                welcome_lock_lease=welcome_lock_lease,
            )
        finally:
            welcome_lock_lease.release()

    async def _execute_user_rollback_after_resource_preflight(
        self,
        event: UserRollback,
        *,
        sid: str,
        session_generation: int,
        transition_owner: str,
        target_turn: int,
        rolled_back_user_text: str,
        atomic_copy_file: Callable[[Path, Path], None],
        lock_timeout_seconds: float,
        welcome_lock_lease: _RollbackLockLease,
    ) -> None:
        """Build the rollback plan and mutate only after secured preflight resources."""

        discarded_turn_count = self._session.turn_number - target_turn

        tracker = self._session.mutation_tracker
        tracker_turn_ids: list[int] = sorted(t.turn_id for t in tracker.get_all_turns()) if tracker is not None else []
        retained_notice = (
            format_retained_changes_notice(
                tracker,
                cwd=self._session.workspace_cwd(),
                max_entries=self._settings_handle.settings.workspace_change_notice_max_entries,
            )
            if target_turn == 0 and not event.revert_changes
            else None
        )

        restore_results: list = []
        plan_exclusions: list[tuple[str, str]] = []
        plan_warnings: list[str] = []
        plan_candidates: list[str] = []
        revert_detection_truncated = False
        welcome_file_rollback: Callable[[], Awaitable[None]] | None = None
        post_swap_file_rollback: Callable[[], None] | None = None
        if event.revert_changes and tracker is not None and tracker_turn_ids:
            rollback_turn_ids = {tid for tid in tracker_turn_ids if tid > target_turn}
            only_paths: set[str] | None = set(event.selected_paths) if event.selected_paths else None
            if rollback_turn_ids:
                # Truncated detection means the rolled-back turns may hold
                # writes that never became plan candidates; the flag lives on
                # the turns rollback_turns removes, so capture it here.
                revert_detection_truncated = any(
                    turn.detection_truncated for turn in tracker.get_all_turns() if turn.turn_id in rollback_turn_ids
                )
                try:
                    # Authoritative peer re-check before the only destructive
                    # consumer acts: force-reclassify against the coordination
                    # registry (a peer's claim may have landed after our
                    # finalize), then let the coordinator exclude paths a peer
                    # modified since and warn about in-flight peer commands.
                    # Coordination failures must not break rollback itself.
                    coordinator = self._session.mutation_coordinator
                    loop = asyncio.get_running_loop()
                    # The workspace cwd doubles as the registry fallback root:
                    # outside a git repo, claims were published under it, and
                    # the destructive rollback-time check must rediscover the
                    # same peer files.
                    workspace_cwd = self._session.workspace_cwd()
                    if coordinator is not None:
                        try:
                            await loop.run_in_executor(
                                None,
                                lambda: coordinator.reclassify(tracker, force=True, fallback_root=workspace_cwd),
                            )
                        except Exception:
                            logger.debug("Pre-rollback attribution re-check failed", exc_info=True)
                    # Build the plan FIRST: rollback_turns removes the turns
                    # it rolls back, so exclusions/warnings cannot be
                    # reconstructed afterwards — execute from the pre-built
                    # plan and thread its report into the result event.
                    plan = tracker.get_rollback_plan_for_turns(rollback_turn_ids)
                    if coordinator is not None:
                        try:
                            plan = await loop.run_in_executor(
                                None,
                                lambda: coordinator.augment_rollback_plan(
                                    tracker,
                                    plan,
                                    scope_paths=[workspace_cwd],
                                    fallback_root=workspace_cwd,
                                ),
                            )
                        except Exception:
                            logger.debug("Rollback plan peer augmentation failed", exc_info=True)
                    plan_exclusions = [(path, reason.value) for path, reason in plan.exclusions]
                    plan_warnings = [w.message for w in plan.warnings]
                    # Every path the rolled-back turns touched — entries the
                    # restore will attempt plus exclusions it never will. The
                    # turns are removed during execution, so the retained set
                    # can only be derived from this pre-built list afterwards.
                    plan_candidates = [path for path, _ in plan.entries] + [path for path, _ in plan.exclusions]

                    def _apply_file_rollback() -> None:
                        nonlocal restore_results
                        try:
                            restore_results = tracker.rollback_turns(
                                rollback_turn_ids,
                                only_paths=only_paths,
                                plan=plan,
                            )
                        except Exception:
                            logger.exception("Rollback file-restore failed (target_turn=%d)", target_turn)
                            restore_results = []

                    if target_turn == 0:

                        async def _apply_welcome_file_rollback() -> None:
                            _apply_file_rollback()

                        welcome_file_rollback = _apply_welcome_file_rollback
                    else:
                        post_swap_file_rollback = _apply_file_rollback
                except Exception:
                    logger.exception("Rollback file-restore failed (target_turn=%d)", target_turn)
                    restore_results = []

        if await self._cancel_if_rollback_owner_changed(event, sid, session_generation):
            return

        session_dir = self._session.session_dir
        # Which turns this rollback supersedes is a matter of identity, not of
        # ordinals: the registry is append-ordered, and a snapshot's is an
        # earlier state of it, so the superseded turns are the tail it does not
        # name. Ordinals cannot stand in for them — after a failed or cancelled
        # turn the running counter is ahead of the saved one, and subtraction
        # would then retire a turn that never ran.
        superseded_turn_ids: list[str] = []
        if target_turn == 0:
            # Every turn goes, and every command the turns started: before their
            # files come back, which those commands could write again.
            if (tasks := self._session.background_tasks) is not None:
                await tasks.stop_turns_after(0)
            # Keep the old registry for the audit record: a successful reset
            # replaces the live history before the rollback event is committed.
            rollback_history_state = self._history.state if self._history.is_bound else None
            rollback_turns = turn_records(rollback_history_state)
            superseded_turn_ids = [record.turn_id for record in rollback_turns]
            self._permits.commit_session_transition(transition_owner)

            try:
                reset_succeeded = await self._lifecycle.reset_session_to_welcome(
                    sid,
                    write_lock_held=welcome_lock_lease.held,
                    after_delete=welcome_file_rollback,
                    before_restart=welcome_lock_lease.release,
                )
            finally:
                welcome_lock_lease.release()
            if not reset_succeeded:
                await self._bus.publish(
                    Error(
                        code="rollback_reset_failed",
                        message="Rollback to welcome could not reset the session because the session state is busy.",
                        display_message=_ROLLBACK_RESET_FAILED.bind(),
                        session_id=sid,
                    ),
                )
                return
            # The reset has committed and released the welcome write lock. Its
            # restarted recorder is bound lazily to the same log, so activation
            # recovers the old branch and this event atomically opens the new one.
            # A failed reset never reaches here and therefore never supersedes the
            # branch of the session that was restored.
            await self._trajectory_recorder.rollback(
                superseded_turns=rollback_turns,
                target_turns=[],
            )
            if retained_notice:
                self._workspace_change_tracker.queue_safety_notice(retained_notice, cwd=self._session.workspace_cwd())
            self._queue_partial_revert_notice(
                plan_candidates, restore_results, detection_truncated=revert_detection_truncated
            )
            files_reverted = sum(1 for r in restore_results if r.changed)
            await self._bus.publish(
                RollbackResult(
                    session_id=sid,
                    target_turn=0,
                    rolled_back_user_text=rolled_back_user_text,
                    files_reverted=files_reverted,
                    restore_results=restore_results,
                    exclusions=plan_exclusions,
                    warnings=plan_warnings,
                    superseded_turn_ids=superseded_turn_ids,
                    discarded_turn_count=discarded_turn_count,
                ),
            )
            return

        snapshot = rollback_snapshot_for_target(session_dir, target_turn)
        session_file = session_dir / "session.json" if session_dir else None
        if snapshot is None or session_file is None or not snapshot.path.exists():
            await self._bus.publish(
                Warning(
                    code="rollback_snapshot_missing",
                    message=f"Snapshot for turn {target_turn} is missing.",
                    display_message=_ROLLBACK_SNAPSHOT_MISSING.bind(target_turn=target_turn),
                    session_id=sid,
                ),
            )
            return

        # Before the files come back: a command the removed turns started could write them again.
        if (tasks := self._session.background_tasks) is not None:
            await tasks.stop_turns_after(target_turn)

        live_turns = turn_records(self._history.state if self._history.is_bound else None)
        # What the audit record is told the snapshot keeps: it is written before
        # the swap, while the outgoing runtime still holds the pre-rollback
        # registry, so the restored registry is not available yet. A snapshot
        # whose payload does not parse names no turn, and superseding everything
        # on the strength of it would mark history the restore is about to keep
        # as retired — it names nothing instead.
        superseded_for_record: list[TurnRecord] = []
        if snapshot.readable:
            kept_turn_ids = {record.turn_id for record in snapshot.turns}
            superseded_for_record = [record for record in live_turns if record.turn_id not in kept_turn_ids]

        snapshot_missing_after_lock = False
        try:
            lock_path = self._session.session_write_lock_path(sid)
            if lock_path is None:
                raise OSError("session state store is unavailable")
            with FileLock(lock_path, timeout=lock_timeout_seconds):
                if not snapshot.path.exists():
                    snapshot_missing_after_lock = True
                else:
                    overlays = _read_session_overlays(session_file)
                    self._permits.commit_session_transition(transition_owner)
                    atomic_copy_file(snapshot.path, session_file)
                    _reapply_session_overlays(session_file, overlays)
                    backup_file = session_file.with_name(SESSION_BACKUP_FILE_NAME)
                    try:
                        # Copy the restored (overlay-patched) primary so both files
                        # stay identical.
                        atomic_copy_file(session_file, backup_file)
                    except OSError:
                        with contextlib.suppress(OSError):
                            backup_file.unlink()
                        logger.warning("Failed to update rollback backup %s", backup_file, exc_info=True)
        except TimeoutError as exc:
            await self._bus.publish(
                Error(
                    code="rollback_swap_locked",
                    message=f"Timed out waiting for session lock: {exc}",
                    display_message=_ROLLBACK_SWAP_LOCKED.bind(detail=DisplayBlock(str(exc))),
                    session_id=sid,
                ),
            )
            return
        except OSError as exc:
            await self._bus.publish(
                Error(
                    code="rollback_swap_failed",
                    message=f"Failed to restore snapshot: {exc}",
                    display_message=_ROLLBACK_SWAP_FAILED.bind(detail=DisplayBlock(str(exc))),
                    session_id=sid,
                ),
            )
            return

        if snapshot_missing_after_lock:
            await self._bus.publish(
                Warning(
                    code="rollback_snapshot_missing",
                    message=f"Snapshot for turn {target_turn} is missing.",
                    display_message=_ROLLBACK_SNAPSHOT_MISSING.bind(target_turn=target_turn),
                    session_id=sid,
                ),
            )
            return

        if post_swap_file_rollback is not None:
            post_swap_file_rollback()

        if session_dir is not None:
            snap_dir = session_dir / "snapshots"
            if snap_dir.is_dir():
                for path in rollback_snapshot_paths(session_dir):
                    turn = snapshot_target_turn(path)
                    if turn is None:
                        continue
                    if turn <= target_turn:
                        continue
                    try:
                        path.unlink()
                    except OSError:
                        logger.debug("Failed to prune stale snapshot %s", path, exc_info=True)

        # The rollback opens a new trajectory branch: recorded on the outgoing
        # runtime (the restore below closes it and the restored session resumes
        # on the new branch), after the swap lock is released — activation
        # takes the same lock — and while the live registry still names the
        # first superseded turn.
        record_cancelled = False
        try:
            await self._trajectory_recorder.rollback(
                superseded_turns=superseded_for_record,
                target_turns=snapshot.turns,
            )
        except asyncio.CancelledError:
            # The swap has committed but the engine still holds the pre-rollback
            # history: leaving here would let the next save write that history
            # back over the file the rollback just restored. The audit record is
            # the only part that can be dropped, so the cancellation waits for the
            # restore below and completes right after it.
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()
            record_cancelled = True

        with self._session.saves_suppressed():
            await self._lifecycle.on_session_restore(SessionRestore(session_id=sid, ignore_recovery=True))
        if record_cancelled:
            raise asyncio.CancelledError

        # What the rollback actually retired, read from the registry the restored
        # session came back with: the snapshot names a state, but the restore may
        # read another source (a save the crash sidecar won, a backup) or leave
        # the session untouched, and a turn is superseded only when it is gone
        # from the session that is live now.
        retained_ids = {
            record.turn_id for record in turn_records(self._history.state if self._history.is_bound else None)
        }
        superseded_turns = [record for record in live_turns if record.turn_id not in retained_ids]
        superseded_turn_ids = [record.turn_id for record in superseded_turns]

        if event.revert_changes:
            self._workspace_change_tracker.invalidate()
            # After the invalidate: the stale baseline can never rediscover
            # files the revert retained, so they must be reported directly.
            self._queue_partial_revert_notice(
                plan_candidates, restore_results, detection_truncated=revert_detection_truncated
            )
            await self._writer.save_current_session()

        files_reverted = sum(1 for r in restore_results if r.changed)
        await self._bus.publish(
            RollbackResult(
                session_id=sid,
                target_turn=target_turn,
                rolled_back_user_text=rolled_back_user_text,
                files_reverted=files_reverted,
                restore_results=restore_results,
                exclusions=plan_exclusions,
                warnings=plan_warnings,
                superseded_turn_ids=superseded_turn_ids,
                discarded_turn_count=discarded_turn_count,
            ),
        )

    def _queue_partial_revert_notice(
        self,
        plan_candidates: list[str],
        restore_results: list,
        *,
        detection_truncated: bool = False,
    ) -> None:
        """Report rollback candidates the revert did not actually restore.

        Covers a partial ``selected_paths`` selection (unselected entries are
        never attempted), plan exclusions, per-file restore failures, and a
        restore pass that failed wholesale (empty results). Truncated
        detection on a rolled-back turn means writes may exist that never
        became candidates, so it warns even when every known candidate
        restored. A fully-applied revert with complete detection queues
        nothing.
        """
        if not plan_candidates and not detection_truncated:
            return
        restored = {result.path for result in restore_results if result.ok}
        retained = [path for path in plan_candidates if path not in restored]
        cwd = self._session.workspace_cwd()
        notice = format_partial_revert_notice(
            retained,
            cwd=cwd,
            max_entries=self._settings_handle.settings.workspace_change_notice_max_entries,
            detection_incomplete=detection_truncated,
        )
        if notice:
            self._workspace_change_tracker.queue_safety_notice(notice, cwd=cwd)

    async def _cancel_if_rollback_owner_changed(
        self,
        event: UserRollback,
        session_id: str,
        session_generation: int,
    ) -> bool:
        """Reject a deferred rollback that no longer belongs to its requester."""
        if (
            self._session.session_id == session_id
            and self._permits.session_generation == session_generation
            and (event.session_id is None or event.session_id == session_id)
        ):
            return False
        await self._bus.publish(
            Warning(
                code="rollback_session_changed",
                message="Rollback cancelled because the active session changed.",
                display_message=_ROLLBACK_SESSION_CHANGED.bind(),
                session_id=session_id,
            )
        )
        return True

    async def _refuse_if_rollback_state_invalid(self, session_id: str) -> bool:
        """Preserve the immediate refusal contract for a genuinely active turn."""
        if self._fsm.state in (EngineState.IDLE, EngineState.INTERRUPTED, EngineState.FAILED):
            return False
        await self._bus.publish(
            Warning(
                code="rollback_refused",
                message=f"Rollback is not allowed in state {self._fsm.state.name}.",
                display_message=_ROLLBACK_REFUSED.bind(state=self._fsm.state.name),
                session_id=session_id,
            ),
        )
        return True

    def _acquire_welcome_reset_write_lock(
        self,
        session_id: str,
        timeout: float,
    ) -> tuple[bool, FileLock | None]:
        lock_path = self._session.session_write_lock_path(session_id)
        if lock_path is None:
            return True, None
        lock = FileLock(lock_path, timeout=timeout)
        try:
            lock.acquire()
            return True, lock
        except TimeoutError:
            logger.warning("Timed out acquiring session lock for welcome rollback %s", session_id, exc_info=True)
            return False, None
        except OSError:
            logger.warning("Failed to acquire session lock for welcome rollback %s", session_id, exc_info=True)
            return False, None
