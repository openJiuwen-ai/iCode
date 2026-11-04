# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One cancellable side question alongside a session's main execution lease."""

from __future__ import annotations

import asyncio
import copy
import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from chrys.foundation.events.types import (
    InvocationSideQuestionEnded,
    InvocationSideQuestionFailed,
    RollbackResult,
    SideQuestionCancelled,
    SideQuestionHistoryRequested,
    SideQuestionRequested,
    SideQuestionsReset,
)
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.models.side_questions import SideQuestionEntry
from chrys.foundation.trajectory.context import side_call_scope
from chrys.foundation.trajectory.envelope import ActorRole
from chrys.orchestration.invoker.content_projection import ContentProjector
from chrys.orchestration.invoker.origin import BoundEmitter
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import check_operation
from chrys.service.hooks.operation import HookOperation
from chrys.service.llm.side_question import answer_side_question, conversation_snapshot
from chrys.service.session.side_questions import SideQuestionSessionSeed, load_side_questions, order_side_questions

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.trajectory.context import TrajectoryContext
    from chrys.orchestration.engine.execution import ExecutionLease
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.orchestration.engine.state.current_agent import CurrentAgent
    from chrys.orchestration.engine.state.lifecycle_permits import LifecyclePermits
    from chrys.orchestration.engine.trajectory import TrajectoryRecorder
    from chrys.service.profiles.models.schema import ModelProfile
    from chrys.service.session.history import SessionHistoryManager
    from chrys.service.state.store import StateStore

logger = logging.getLogger(__name__)
_CLEANUP_TIMEOUT_SECONDS = 0.25


@dataclass
class _Question:
    emitter: BoundEmitter
    created_at: str
    seed: SideQuestionSessionSeed
    operation: HookOperation
    writes_cancelled: threading.Event
    task: asyncio.Task[None] | None = None
    cancelled: bool = False


class SideQuestions:
    """Own disposable side work without making main-session operations wait for it."""

    def __init__(
        self,
        *,
        bus: EventBus,
        session: ActiveSession,
        current: CurrentAgent,
        permits: LifecyclePermits,
        history: SessionHistoryManager,
        recorder: TrajectoryRecorder,
        state_store: StateStore | None,
        lease: ExecutionLease,
    ) -> None:
        self._bus = bus
        self._session = session
        self._current = current
        self._permits = permits
        self._history = history
        self._recorder = recorder
        self._store = state_store
        self._lease = lease
        self._memory_session = ""
        self._memory_entries: tuple[SideQuestionEntry, ...] = ()
        self._active: _Question | None = None
        self._run_tasks: set[asyncio.Task[None]] = set()
        self._commit_tasks: set[asyncio.Task[None]] = set()
        self._closed = False
        self._writes_cancelled = threading.Event()

    async def subscribe(self) -> None:
        await self._bus.subscribe(SideQuestionRequested, self.request)
        await self._bus.subscribe(SideQuestionCancelled, self.cancel)
        await self._bus.subscribe(SideQuestionHistoryRequested, self.read_history)
        await self._bus.subscribe(RollbackResult, self.reset)

    async def request(self, event: SideQuestionRequested) -> None:
        session_id = self._session.session_id
        profile = self._current.manifest.active_profile
        if (
            self._closed
            or self._session.shutting_down
            or self._permits.agent_loading
            or self._lease.session_operations
            or self._lease.workflow is not None
            or not session_id
            or event.session_id != session_id
            or not event.request_id
            or not event.question.strip()
            or profile is None
            or self._current.loaded is None
            or self._active is not None
            or (self._store is not None and not self._session.guard.owns(session_id))
        ):
            return
        origin = InvocationOrigin(
            kind="side_question", session_id=session_id, invocation_id=event.request_id, parent=None
        )
        agent = self._session.agent_profile
        workspace = self._session.workspace
        seed = SideQuestionSessionSeed(
            agent_profile=agent.name if agent else "",
            agent_display_name=agent.display_name if agent else "",
            agent_profile_id=agent.id if agent else "",
            primary_cwd=workspace.primary_cwd if workspace else "",
            working_dirs=tuple(directory.path for directory in workspace.working_dirs) if workspace else (),
            model_provider=profile.provider,
            model_api_style=profile.api_style or "",
            model_id=profile.model_id,
            model_profile_id=profile.id,
            last_surface=self._session.surface,
        )
        active = _Question(
            BoundEmitter(self._bus, origin),
            event.timestamp.isoformat(),
            seed,
            HookOperation(subject="input", session_id=session_id, origin=origin, input_text=event.question),
            self._writes_cancelled,
        )
        messages = list(self._history.messages)
        loaded = self._current.require_loaded()
        if loaded.bindings.backend.active_handle is not None:
            # The history provider commits a run at its end. The recorder already holds the current
            # input and landed exchanges, but never the response still arriving on the wire.
            recorder = loaded.loop_recorder
            messages.extend(recorder.snapshot().captured or ())
            messages.extend(recorder.loop_messages or ())
            messages.extend(recorder.landed_response or ())
        context = conversation_snapshot(messages)
        profile = copy.deepcopy(profile)
        self._active = active
        event.accepted = True
        task = asyncio.create_task(
            self._run(active, profile, event.question, context, self._session.session_dir, self._recorder.context()),
            name=f"chrys.side-question.{event.request_id}",
        )
        active.task = task
        self._run_tasks.add(task)
        task.add_done_callback(self._run_tasks.discard)

    def _is_current(self, active: _Question) -> bool:
        return (
            self._active is active
            and not active.cancelled
            and not active.writes_cancelled.is_set()
            and not self._closed
            and active.emitter.origin.session_id == self._session.session_id
        )

    async def _run(
        self,
        active: _Question,
        profile: ModelProfile,
        question: str,
        context: str,
        session_dir: Path | None,
        trajectory: TrajectoryContext | None,
    ) -> None:
        delivered = False
        try:
            if not self._is_current(active):
                return
            decision = await check_operation(
                self._session.hook_manager,
                HookEvent.USER_PROMPT_SUBMIT,
                active.operation,
                lambda: {
                    "session_id": active.emitter.origin.session_id,
                    "profile": active.seed.agent_profile,
                    "cwd": active.seed.primary_cwd,
                    "text": question,
                    "injected": False,
                    "side_question": True,
                },
                scope="session",
                trajectory_context=trajectory,
                origin=active.emitter.origin,
            )
            if not self._is_current(active):
                return
            failure = None
            if decision.blocked:
                failure = InvocationSideQuestionFailed(
                    origin=active.emitter.origin, kind="blocked", reason=decision.block_reason
                )
            elif decision.invalidated or active.operation.commit_input(question) is not None:
                failure = InvocationSideQuestionFailed(origin=active.emitter.origin)
            if failure is not None:
                self._active = None
                await active.emitter.publish(failure)
                delivered = True
                return
            with side_call_scope(ActorRole.SIDE_QUESTION, context=trajectory):
                answer = await answer_side_question(
                    profile,
                    session_id=active.emitter.origin.session_id,
                    session_dir=session_dir,
                    question=question,
                    context=context,
                    reminders=decision.system_reminders,
                )
            if self._is_current(active):
                entry = SideQuestionEntry(active.created_at, question, answer)
                session_id = active.emitter.origin.session_id
                if self._memory_session != session_id:
                    self._memory_session, self._memory_entries = session_id, ()
                self._memory_entries = order_side_questions((*self._memory_entries, entry))
                # Present the answer independently of disk I/O. Completed answers remain available
                # in this session even if best-effort persistence fails.
                commit = asyncio.create_task(self._persist(active, entry))
                self._commit_tasks.add(commit)
                commit.add_done_callback(self._commit_tasks.discard)
                projector = ContentProjector(active.emitter, shows_whole_segments=True)
                await projector.publish_text("answer", answer)
                if self._is_current(active):
                    # A completion handler may show the answer and then await history I/O.
                    # Admit the next question as soon as the frontend observes completion.
                    self._active = None
                    await projector.finish("completed")
                    delivered = True
        except Exception:
            logger.debug("Side question failed", exc_info=True)
            if self._is_current(active):
                self._active = None
                await active.emitter.publish(
                    InvocationSideQuestionFailed(
                        origin=active.emitter.origin, session_id=active.emitter.origin.session_id
                    )
                )
                delivered = True
        finally:
            active.operation.invalidate("side_question_ended")
            if self._active is active:
                self._active = None
            if not delivered:
                await active.emitter.publish(InvocationSideQuestionEnded(origin=active.emitter.origin))

    async def _persist(self, active: _Question, entry: SideQuestionEntry) -> None:
        if self._store is None or active.writes_cancelled.is_set():
            return
        try:
            await self._store.save_side_question(
                active.emitter.origin.session_id, entry, active.seed, cancelled=active.writes_cancelled
            )
        except Exception:
            logger.warning("Failed to save side question history", exc_info=True)
            if not active.writes_cancelled.is_set():
                await active.emitter.publish(InvocationSideQuestionFailed(origin=active.emitter.origin, kind="save"))

    async def read_history(self, event: SideQuestionHistoryRequested) -> None:
        session_id = self._session.session_id
        if self._closed or self._session.shutting_down or not session_id or event.session_id != session_id:
            return
        event.accepted = True
        generation = self._writes_cancelled
        entries: tuple[SideQuestionEntry, ...] = ()
        try:
            if self._store is not None:
                directory = self._store.session_dir(session_id)
                entries = await asyncio.to_thread(load_side_questions, directory, session_id)
        except Exception:
            logger.warning("Failed to read side question history", exc_info=True)
            event.failed = True
        if generation.is_set():
            event.accepted = False
            return
        memory = self._memory_entries if self._memory_session == session_id else ()
        event.entries = order_side_questions((*entries, *(entry for entry in memory if entry not in entries)))

    async def has_history(self) -> bool:
        event = SideQuestionHistoryRequested(session_id=self._session.session_id)
        await self.read_history(event)
        return bool(event.entries)

    def _cancel_active(self) -> _Question | None:
        active, self._active = self._active, None
        if active is not None and not active.cancelled:
            active.cancelled = True
            active.operation.invalidate("side_question_cancelled")
            if active.task is not None and not active.task.done():
                active.task.cancel()
            return active
        return None

    async def cancel(self, event: SideQuestionCancelled) -> None:
        active = self._active
        if active is not None and (
            event.session_id == active.emitter.origin.session_id
            and event.request_id == active.emitter.origin.invocation_id
        ):
            # Keep cleanup owned for session teardown, while immediately admitting a replacement.
            self._cancel_active()

    async def reset(self, event: RollbackResult) -> None:
        if event.session_id == self._session.session_id:
            await self.stop_running()

    async def stop_running(self) -> None:
        """Stop running questions and close the card, keeping answers and queued writes."""
        await self._stop(tuple(self._run_tasks))

    async def quiesce(self) -> None:
        """End this session's side work, forgetting answers and invalidating queued writes.

        Store workers check the invalidation under the existing session write lock. A worker
        already holding it finishes before deletion; a queued one cannot recreate a deleted session.
        Cancel run and commit tasks with a short cleanup grace period. Session operations never
        await the store workers themselves, which may be waiting for the session write lock.
        """
        self._writes_cancelled.set()
        self._writes_cancelled = threading.Event()
        self._memory_session, self._memory_entries = "", ()
        await self._stop(tuple(self._run_tasks | self._commit_tasks))

    async def _stop(self, tasks: tuple[asyncio.Task[None], ...]) -> None:
        cancelled = self._cancel_active()
        for task in tasks:
            task.cancel()
        if cancelled is not None:
            # A task cancelled before its first step never enters _run's terminal-event finally.
            await cancelled.emitter.publish(InvocationSideQuestionEnded(origin=cancelled.emitter.origin))
        await self._bus.publish(SideQuestionsReset(session_id=self._session.session_id))
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=_CLEANUP_TIMEOUT_SECONDS)
            for task in pending:
                task.cancel()
            if pending:
                logger.warning("Side question cleanup exceeded its grace period; continuing session operation")

    async def close(self) -> None:
        self._closed = True
        await self.quiesce()
