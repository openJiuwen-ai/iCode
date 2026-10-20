# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session saves and ordered recovery writes bound to their captured session identity."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypedDict

from chrys.foundation.recovery import RecoveryPersistOutcome
from chrys.service.agent_middleware.reminders.archive_pointer import CATALOG_POINTER_RECORD_COUNT_STATE_KEY
from chrys.service.profiles.models.schema import API_STYLE_RESPONSES
from chrys.service.session import checkpoint as session_checkpoint
from chrys.service.session.persistence import has_real_messages
from chrys.service.trajectory.items import ensure_history_item_ids

if TYPE_CHECKING:
    from chrys.foundation.models.session_surface import SessionSurface
    from chrys.foundation.models.workspace import Workspace
    from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.orchestration.engine.state.current_agent import CurrentAgent
    from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker
    from chrys.service.profiles.models.schema import ModelProfile
    from chrys.service.session.persistence import SessionPersistence

logger = logging.getLogger(__name__)


class SessionMetadata(TypedDict):
    """The persistence metadata captured alongside a session snapshot."""

    agent_profile_name: str
    agent_display_name: str
    agent_profile_id: str
    agent_profile_fingerprint: str
    model_profile_fingerprint: str | None
    workspace: Workspace | None
    model_profile: ModelProfile | None
    last_surface: SessionSurface | None


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    """A session id paired with the metadata captured for its write."""

    session_id: str | None
    metadata: SessionMetadata


class RecoveryCheckpoints(Protocol):
    """Ordered checkpoint writes available to an agent build."""

    async def save_checkpoint(self) -> None: ...
    async def persist_now(self) -> bool: ...
    async def persist_barrier(self) -> RecoveryPersistOutcome: ...
    async def flush(self) -> None: ...


class SessionWriter:
    """Own primary saves and the tasks, ordering lock, and stamps of recovery writes."""

    def __init__(
        self,
        *,
        persistence: SessionPersistence,
        session: ActiveSession,
        current: CurrentAgent,
        turn_state: TurnRuntimeState,
        workspace_change_tracker: WorkspaceChangeTracker,
    ) -> None:
        self._persistence = persistence
        self._session = session
        self._current = current
        self._turn_state = turn_state
        self._workspace_change_tracker = workspace_change_tracker
        # Crash-recovery checkpoint writes are dispatched to a background task
        # so the per-round-trip LLM call never blocks on the sidecar's fsync +
        # file lock (slow on Windows under load).  ``_pending_recovery_state``
        # holds the latest snapshot to write (newest wins); the single in-flight
        # ``_recovery_write_task`` is drained at save/shutdown boundaries so the
        # sidecar stays durable and is never resurrected after a clean turn.
        self._recovery_write_task: asyncio.Task[None] | None = None
        self._pending_recovery_state: tuple[int, SessionIdentity, dict[str, Any]] | None = None
        self._recovery_persistence_lock = asyncio.Lock()
        self._strict_recovery_write_tasks: set[asyncio.Task[bool]] = set()
        # Monotonic snapshot stamps: a queued background write whose snapshot
        # predates the last persisted one is skipped, so a coalesced writer
        # that froze state before a strict barrier can never downgrade the
        # sidecar to a snapshot missing the barrier's committed exchanges.
        self._recovery_snapshot_seq = 0
        self._recovery_persisted_seq = 0

    @property
    def write_task(self) -> asyncio.Task[None] | None:
        """Read the current write task."""
        return self._recovery_write_task

    @property
    def pending(self) -> tuple[int, SessionIdentity, dict[str, Any]] | None:
        """Read the current pending."""
        return self._pending_recovery_state

    @property
    def strict_write_tasks(self) -> set[asyncio.Task[bool]]:
        """Read the current strict write tasks."""
        return self._strict_recovery_write_tasks

    @property
    def snapshot_seq(self) -> int:
        """Read the current snapshot seq."""
        return self._recovery_snapshot_seq

    @property
    def persisted_seq(self) -> int:
        """Read the current persisted seq."""
        return self._recovery_persisted_seq

    def session_identity(self) -> SessionIdentity:
        """Capture the persistence metadata fields without yielding.

        The surface mark is read with the session id, so a queued recovery
        snapshot keeps the value of the moment it was captured.
        """
        return SessionIdentity(
            session_id=self._session.session_id,
            metadata=SessionMetadata(
                agent_profile_name=self._session.agent_profile.name if self._session.agent_profile else "",
                agent_display_name=self._session.agent_profile.display_name if self._session.agent_profile else "",
                agent_profile_id=self._session.agent_profile.id if self._session.agent_profile else "",
                agent_profile_fingerprint=self._current.manifest.agent_profile_fingerprint,
                model_profile_fingerprint=self._current.manifest.model_profile_fingerprint,
                workspace=self._session.workspace,
                model_profile=self._current.manifest.active_profile,
                last_surface=self._session.marked_surface(),
            ),
        )

    async def save_current_session(self, *, raise_on_error: bool = False, retain_empty: bool = False) -> bool:
        """Auto-save the current chat session state to disk."""
        # Drain any in-flight recovery write first.  ``save_session`` deletes the
        # sidecar structurally; flushing first guarantees that delete is the last
        # write (no stale sidecar resurrected after a clean turn) and makes the
        # final checkpoint durable even when the trailing save is suppressed.
        await self.flush()
        if self._current.loaded is None or self._session.suppress_save:
            return False
        # Serialize mutation tracker into session state
        if self._session.mutation_tracker is not None:
            self._current.loaded.bindings.backend.history_state["chrys_mutations"] = (
                self._session.mutation_tracker.serialize()
            )
        baseline = self._workspace_change_tracker.serialize()
        if baseline is not None:
            self._current.loaded.bindings.backend.history_state["chrys_workspace_baseline"] = baseline
        else:
            self._current.loaded.bindings.backend.history_state.pop("chrys_workspace_baseline", None)
        # Session todo list — reads the TRACKER (not prior state): set when
        # non-empty, pop otherwise (empty ≡ absent).
        todos = self._session.todo_tracker.serialize() if self._session.todo_tracker is not None else []
        if todos:
            self._current.loaded.bindings.backend.history_state["chrys_todos"] = todos
        else:
            self._current.loaded.bindings.backend.history_state.pop("chrys_todos", None)
        self._current.loaded.bindings.backend.history_state.update(self._session.runtime_meta.to_state_dict())
        # Persist the Phase 4 LAST_WORDS note so a post-restart resume can
        # re-inject it — the compacted turn's tool-call history is already
        # dropped from ``messages``, and the note is its only replacement.
        if self._current.loaded is not None:
            last_words = self._current.loaded.last_words.get_last_words()
            if last_words:
                self._current.loaded.bindings.backend.history_state["last_words"] = last_words
            else:
                self._current.loaded.bindings.backend.history_state.pop("last_words", None)
            manifest = self._current.loaded.last_words.get_last_words_manifest()
            if manifest:
                self._current.loaded.bindings.backend.history_state["last_words_manifest"] = manifest
            else:
                self._current.loaded.bindings.backend.history_state.pop("last_words_manifest", None)
            breaker = self._current.loaded.last_words.get_last_words_breaker_state()
            if breaker:
                self._current.loaded.bindings.backend.history_state["last_words_breaker"] = breaker
            else:
                self._current.loaded.bindings.backend.history_state.pop("last_words_breaker", None)
            catalog_pointer_record_count = (
                self._current.loaded.reminder_middleware.sources.archive_pointer.record_count_state()
            )
            if catalog_pointer_record_count is not None:
                self._current.loaded.bindings.backend.history_state[CATALOG_POINTER_RECORD_COUNT_STATE_KEY] = (
                    catalog_pointer_record_count
                )
            else:
                self._current.loaded.bindings.backend.history_state.pop(CATALOG_POINTER_RECORD_COUNT_STATE_KEY, None)
        else:
            # Note + manifest are erasure-protected while middleware is
            # temporarily unavailable. The breaker is deliberately not:
            # failed-attempt truth must be written unconditionally.
            self._current.loaded.bindings.backend.history_state.pop("last_words_breaker", None)
        if self._current.manifest.active_profile is None:
            service_session_id = None
        elif (
            self._current.manifest.active_profile.provider == "openai"
            and self._current.manifest.active_profile.api_style == API_STYLE_RESPONSES
        ):
            service_session_id = (
                self._current.loaded.bindings.backend.service_session_id
                if self._current.loaded.bindings.backend.service_session_storage_enabled
                and not self._current.loaded.bindings.state.run_failed
                and not self._current.loaded.bindings.state.was_interrupted
                else ""
            )
        else:
            service_session_id = ""
        # Every persisted item carries its analytics id before the save that
        # first persists it; items created on the normal paths already do.
        ensure_history_item_ids(self._current.loaded.bindings.backend.history_state.get("messages", ()))
        identity = self.session_identity()
        saved = await self._persistence.save_session(
            identity.session_id,
            self._current.loaded.bindings.backend.history_state,
            **identity.metadata,
            service_session_id=service_session_id,
            raise_on_error=raise_on_error,
            retain_empty=retain_empty,
        )
        if saved and self._session.session_id is not None:
            from chrys.foundation.observability.sink import get_otel_sink

            otel_sink = get_otel_sink()
            if otel_sink is not None:
                otel_sink.flush_pending(self._session.session_id)
        if saved and (self._current.loaded is not None and self._current.loaded.sub_agent_tools is not None):
            self._current.loaded.sub_agent_tools.finalize_pending_cleanups()
        if self._session.session_id is not None:
            recovered_from_sidecar = await self._persistence.recovery_session_wins(self._session.session_id)
            self._session.mark_recovered_from_sidecar(recovered_from_sidecar)
        return saved

    async def save_checkpoint(self) -> None:
        """Snapshot an interrupted-form sidecar for crash recovery at an LLM boundary.

        The snapshot is built synchronously (an atomic view of live state), but the
        disk write is handed to a background task so the agent's LLM round trip never
        waits on the sidecar's fsync + file lock — that I/O is slow on Windows and
        would otherwise stall every tool-loop iteration.
        """
        if self._persistence.state_store is None:
            return
        try:
            state = self.build_recovery_snapshot()
        except Exception:
            logger.warning("Failed to build recovery checkpoint for %s", self._session.session_id, exc_info=True)
            return
        if state is None or not has_real_messages(state):
            return
        # Coalesce bursts: keep only the newest snapshot and run a single writer.
        self._recovery_snapshot_seq += 1
        self._pending_recovery_state = (self._recovery_snapshot_seq, self.session_identity(), state)
        if self._recovery_write_task is None or self._recovery_write_task.done():
            self._recovery_write_task = asyncio.create_task(self._drain())

    def build_recovery_snapshot(self) -> dict[str, Any] | None:
        """Build the current interrupted-form snapshot from engine-owned state."""
        if (
            self._session.session_id is None
            or self._current.loaded is None
            or self._current.loaded.loop_recorder is None
        ):
            return None
        current_input = self._turn_state.current_input
        # The reminder record lands on the input when its request is
        # established, after the pre-call and injection checkpoints were taken;
        # the next checkpoint (a tool result) carries it. A hard crash in
        # between recovers the input without one: the retry rebuilds the turn's
        # reminders (one prompt-cache miss at the tail), while an injection's
        # hook reminders, which lived only in the lost process, are not re-sent.
        return session_checkpoint.build_recovery_state(
            self._current.loaded.bindings.backend.history_state,
            self._current.loaded.loop_recorder,
            mutation_tracker=self._session.mutation_tracker,
            runtime_meta=self._session.runtime_meta,
            user_text=current_input.text,
            user_contents=current_input.contents,
            user_created_at=current_input.created_at,
            user_kind=current_input.kind,
            user_reminder_source=self._current.loaded.bindings.inputs.input_properties,
            consumed_injections=list(self._current.loaded.consumed_injections),
            insert_index=self._turn_state.history_start_index,
            last_words=self._current.loaded.last_words.get_last_words() if self._current.loaded is not None else None,
            last_words_manifest=(
                self._current.loaded.last_words.get_last_words_manifest() if self._current.loaded is not None else None
            ),
            last_words_breaker=(
                self._current.loaded.last_words.get_last_words_breaker_state()
                if self._current.loaded is not None
                else None
            ),
            catalog_pointer_record_count=(
                self._current.loaded.reminder_middleware.sources.archive_pointer.record_count_state()
                if self._current.loaded is not None
                else None
            ),
            todos=self._session.todo_tracker.serialize() if self._session.todo_tracker is not None else None,
        )

    async def persist_now(self) -> bool:
        """Strictly persist the newest recovery snapshot after draining older writes."""
        if self._persistence.state_store is None:
            return False
        await self.flush()
        state = self.build_recovery_snapshot()
        if state is None or not has_real_messages(state):
            return False
        self._recovery_snapshot_seq += 1
        seq = self._recovery_snapshot_seq
        identity = self.session_identity()
        # Acquire before spawning the writer so no newer background checkpoint
        # can overtake this snapshot. The child task owns release: shielding it
        # keeps the underlying ``to_thread`` write ordered even if the Phase-4
        # caller is cancelled while persistence is in flight.
        await self._recovery_persistence_lock.acquire()
        try:
            task = asyncio.create_task(self._persist_strict(seq, identity, state))
        except BaseException:
            self._recovery_persistence_lock.release()
            raise
        self._strict_recovery_write_tasks.add(task)
        task.add_done_callback(self._strict_write_done)
        return await asyncio.shield(task)

    async def persist_barrier(self) -> RecoveryPersistOutcome:
        """Strictly persist the current recovery snapshot with a typed outcome."""
        if self._persistence.state_store is None:
            return RecoveryPersistOutcome.UNCONFIGURED
        try:
            state = self.build_recovery_snapshot()
        except Exception:
            logger.debug("Failed to build strict recovery barrier for %s", self._session.session_id, exc_info=True)
            return RecoveryPersistOutcome.FAILED
        if state is None or not has_real_messages(state):
            return RecoveryPersistOutcome.NOTHING_TO_PERSIST

        self._recovery_snapshot_seq += 1
        seq = self._recovery_snapshot_seq
        identity = self.session_identity()
        await self._recovery_persistence_lock.acquire()
        try:
            task = asyncio.create_task(self._persist_strict(seq, identity, state))
        except BaseException:
            self._recovery_persistence_lock.release()
            raise
        self._strict_recovery_write_tasks.add(task)
        task.add_done_callback(self._strict_write_done)
        try:
            persisted = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Strict recovery barrier failed for %s", self._session.session_id, exc_info=True)
            return RecoveryPersistOutcome.FAILED
        return RecoveryPersistOutcome.PERSISTED if persisted else RecoveryPersistOutcome.NOTHING_TO_PERSIST

    async def _persist_strict(self, seq: int, identity: SessionIdentity, state: dict[str, Any]) -> bool:
        """Write one strict snapshot while owning the pre-acquired ordering lock."""
        try:
            if seq <= self._recovery_persisted_seq:
                return True
            persisted = await self._persistence.save_recovery_session_strict(
                identity.session_id,
                state,
                **identity.metadata,
            )
            if persisted:
                self._recovery_persisted_seq = seq
            return persisted
        finally:
            self._recovery_persistence_lock.release()

    def _strict_write_done(self, task: asyncio.Task[bool]) -> None:
        """Retire a strict writer while observing failures after caller cancellation."""
        self._strict_recovery_write_tasks.discard(task)
        if task.cancelled():
            return
        with contextlib.suppress(Exception):
            task.exception()

    async def _drain(self) -> None:
        """Write queued recovery snapshots to disk, newest-wins, one at a time.

        Runs independently of the run task so a cancelled/hung turn (graceful
        shutdown timeout) still flushes its last checkpoint to disk.
        """
        while self._pending_recovery_state is not None:
            seq, identity, state = self._pending_recovery_state
            self._pending_recovery_state = None
            try:
                async with self._recovery_persistence_lock:
                    if seq <= self._recovery_persisted_seq:
                        continue
                    await self._persistence.save_recovery_session(
                        identity.session_id,
                        state,
                        **identity.metadata,
                    )
                    self._recovery_persisted_seq = seq
            except Exception:
                logger.warning("Failed to save recovery checkpoint for %s", self._session.session_id, exc_info=True)

    async def flush(self) -> None:
        """Await all in-flight recovery writes so the sidecar is durable and ordered.

        Called where the run loop has ended (post-run save, shutdown) and —
        mid-run — after an injection is consumed (its only durable copy until
        finalization is the sidecar).  A checkpoint queued concurrently while
        we drain extends the writer loop, never strands it: the task exits
        only when nothing is pending, and newest-wins coalescing keeps the
        await sound for the injection caller (every later snapshot also
        carries the consumed injection).

        ``asyncio.shield`` keeps run-task cancellation (the graceful-shutdown
        timeout cancels the run task while it is parked here) from propagating
        into the writer and aborting it mid-write: the run task unwinds, but the
        writer survives so the trailing shutdown flush can still drain it.
        """
        while True:
            tasks: list[asyncio.Task[Any]] = [task for task in self._strict_recovery_write_tasks if not task.done()]
            background = self._recovery_write_task
            if background is not None and not background.done():
                tasks.append(background)
            if not tasks:
                return
            await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
