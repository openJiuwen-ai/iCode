# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Headless session host for running Chrys agents outside the TUI."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentLoadFailed,
    AgentLoadFinished,
    AgentLoadProgress,
    AgentLoadStarted,
    AgentRuntimeUpdated,
    ApprovalModeUpdated,
    ApprovalRequest,
    ApprovalReviewed,
    CompactionFinished,
    CompactionStarted,
    ContextCompressed,
    Error,
    Event,
    InvocationAborted,
    InvocationAnswerWithdrawn,
    InvocationCascadeAborted,
    InvocationCompactionCommitted,
    InvocationCompactionFinished,
    InvocationCompactionStarted,
    InvocationContentAbandoned,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationContextPressure,
    InvocationEvent,
    InvocationMessage,
    InvocationPaused,
    InvocationProgress,
    InvocationResumed,
    InvocationRetryAttempt,
    InvocationStarted,
    InvocationToolCallArgsUpdated,
    InvocationToolCallProgress,
    InvocationToolCallResult,
    InvocationToolCallStart,
    InvocationToolCallStatusUpdated,
    QuestionToUser,
    SessionRestore,
    SessionRestored,
    SessionSaved,
    StateChanged,
    TodoListUpdated,
    ToolCompacted,
    TurnStarted,
    UsageUpdate,
    UserInjectResult,
    UserInterrupt,
    UserMessage,
    UserRetry,
    Warning,
    WorkflowCancelRequest,
    WorkflowNodeStateChanged,
    WorkflowRunAccepted,
    WorkflowRunFinished,
    WorkflowRunRejected,
    WorkflowRunRequest,
)
from chrys.foundation.events.workflow import WORKFLOW_RUN_EVENTS
from chrys.foundation.i18n import DisplayBlock, DisplaySequence, MessageRef, msg
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workflow_session import (
    WorkflowDraft,
    WorkflowSessionSelection,
    WorkflowTarget,
    WorkspaceSnapshot,
)
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform import get_platform
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.util.session_ids import SESSION_SHORT_ID_LEN, session_short_id
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.engine import AgentEngine
from chrys.orchestration.workflows.catalog import WorkflowCatalog
from chrys.orchestration.workflows.preview import PreparedWorkflow
from chrys.orchestration.workflows.runner import WorkflowRunResult
from chrys.service.approval.policy import ApprovalMode
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile, MCPServerConfig
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.state.store import JsonFileStateStore, StateStore
from chrys.service.workflows.discovery import Discovery
from chrys.service.workflows.outcomes import REASON_DEADLINE_EXCEEDED

_AGENT_PROFILE_NOT_FOUND = msg(
    "session_host.agent_profile_not_found",
    fallback="Agent profile not found: {name}",
)
_AGENT_PROFILE_NOT_FOUND_WITH_AVAILABLE = msg(
    "session_host.agent_profile_not_found_with_available",
    fallback="Agent profile not found: {name}. Available profiles: {available}",
)
_SESSION_NOT_FOUND = msg(
    "session_host.session_not_found",
    fallback="Session not found: {session_id}",
)
_SESSION_NOT_FOUND_WITH_RECENT = msg(
    "session_host.session_not_found_with_recent",
    fallback="Session not found: {session_id}. Recent sessions: {recent}",
)
_SESSION_ID_AMBIGUOUS = msg(
    "session_host.session_id_ambiguous",
    fallback="Session id '{session_id}' is ambiguous.",
)
_NO_FINAL_RESPONSE = msg(
    "session_host.no_final_response",
    fallback="Agent run ended without a final response.",
)
_HEADLESS_INTERACTION_REQUIRED = msg(
    "session_host.headless_interaction_required",
    fallback="Agent requested user input in headless mode: {detail}",
    multiline=True,
)

_HEADLESS_RUN_EVENT_TYPES = (
    AgentLoadFailed,
    AgentLoadFinished,
    AgentLoadProgress,
    AgentLoadStarted,
    InvocationMessage,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationAnswerWithdrawn,
    InvocationContentAbandoned,
    AgentRuntimeUpdated,
    TurnStarted,
    ApprovalModeUpdated,
    ApprovalRequest,
    ApprovalReviewed,
    CompactionFinished,
    CompactionStarted,
    ContextCompressed,
    InvocationContextPressure,
    Error,
    QuestionToUser,
    InvocationRetryAttempt,
    SessionRestored,
    SessionSaved,
    StateChanged,
    InvocationAborted,
    InvocationCascadeAborted,
    InvocationCompactionCommitted,
    InvocationCompactionFinished,
    InvocationCompactionStarted,
    InvocationStarted,
    InvocationPaused,
    InvocationProgress,
    InvocationResumed,
    InvocationToolCallArgsUpdated,
    InvocationToolCallProgress,
    InvocationToolCallResult,
    InvocationToolCallStart,
    InvocationToolCallStatusUpdated,
    TodoListUpdated,
    ToolCompacted,
    UsageUpdate,
    UserInjectResult,
    Warning,
    WorkflowRunAccepted,
    WorkflowRunRejected,
    *WORKFLOW_RUN_EVENTS,
)


_InvocationProse = (
    InvocationMessage
    | InvocationContentDelta
    | InvocationContentFinished
    | InvocationContentAbandoned
    | InvocationAnswerWithdrawn
)
"""Model output a sub-agent keeps private: only its own invocation's stream shows it."""


def allows_headless_event(event: Event) -> bool:
    """Keep the existing headless boundary after execution families converge."""
    if not isinstance(event, _HEADLESS_RUN_EVENT_TYPES):
        return False
    if not isinstance(event, InvocationEvent):
        return True
    if event.origin.root.kind in {"workflow_node", "side_question"}:
        return False
    return event.origin.kind == "turn" or not isinstance(event, _InvocationProse)


def allows_workflow_node_event(event: Event) -> bool:
    """Whether a workflow agent node's own activity may reach a headless workflow stream that asked for it.

    The complement of what :func:`allows_headless_event` drops for workflow-rooted invocations: tool calls,
    retries, compaction and sub-agent lifecycle, and the node's own prose. A node's sub-agents keep their prose
    private, as a chat turn's do.
    """
    if not isinstance(event, InvocationEvent) or not isinstance(event, _HEADLESS_RUN_EVENT_TYPES):
        return False
    if event.origin.root.kind != "workflow_node":
        return False
    return event.origin.kind == "workflow_node" or not isinstance(event, _InvocationProse)


@dataclass(frozen=True)
class HeadlessRunResult:
    """Final response from one headless agent turn; ``events`` stays empty unless the caller asked to keep them."""

    text: str
    session_id: str
    events: list[Event] = field(default_factory=list)


class HeadlessRunError(RuntimeError):
    """Raised when a headless agent turn ends with an engine error."""

    def __init__(self, event: Error) -> None:
        self.event = event
        super().__init__(event.message or event.code or "Headless agent run failed")


@dataclass(frozen=True)
class EndTurn:
    """A turn completed normally."""

    kind: Literal["end_turn"] = "end_turn"
    final_text: str = ""


@dataclass(frozen=True)
class Cancelled:
    """A turn was cancelled by the caller."""

    kind: Literal["cancelled"] = "cancelled"
    reason: str = ""


@dataclass(frozen=True)
class Errored:
    """A turn ended with an engine error."""

    kind: Literal["error"] = "error"
    error: Error = field(default_factory=Error)


@dataclass(frozen=True)
class Refused:
    """A gate refused the user input that would have started or resumed the turn; nothing ran.

    The input's own refusal, not an engine failure: the session goes on as before.
    """

    kind: Literal["refused"] = "refused"
    error: Error = field(default_factory=Error)


TurnOutcome = EndTurn | Cancelled | Errored | Refused


class WorkflowRunRejectedError(RuntimeError):
    """The engine refused the workflow run request; ``event`` carries the reason code."""

    def __init__(self, event: WorkflowRunRejected) -> None:
        super().__init__(f"{event.error}: {event.message}")
        self.event = event


class WorkflowRunTimeoutError(TimeoutError):
    """The workflow deadline expired before a run could be accepted."""


class AgentProfileNotFoundError(KeyError):
    """Raised when an agent profile selector can't be resolved against the registry."""

    def __init__(self, message: str, *, display_message: MessageRef | None = None) -> None:
        self.display_message = display_message
        super().__init__(message)


class SessionNotFoundError(KeyError):
    """Raised when a session id (full or short) doesn't match any stored session."""

    def __init__(self, message: str, *, display_message: MessageRef | None = None) -> None:
        self.display_message = display_message
        super().__init__(message)


class AmbiguousSessionIdError(ValueError):
    """Raised when a session-id prefix matches more than one stored session."""

    def __init__(self, message: str, *, display_message: MessageRef | None = None) -> None:
        self.display_message = display_message
        super().__init__(message)


def _normalize_cwd(cwd: str | None) -> str:
    raw = (cwd or "").strip()
    if not raw:
        return ""
    return os.path.abspath(os.path.expanduser(raw))


class ChrysSessionHost:
    """Owns one event bus, one engine, and one session for non-TUI callers.

    ``start()`` is an explicit, idempotent lifecycle boundary.  Callers that
    need startup/restore events should use ``iter_run_events()``, which
    auto-starts before publishing the user turn.  ``run_until_final()`` is only
    a convenience wrapper over that streaming primitive.
    """

    def __init__(
        self,
        *,
        profile_name: str = "",
        session_id: str | None = None,
        settings: Settings | None = None,
        loaded_settings: LoadedSettings | None = None,
        event_bus: EventBus | None = None,
        agent_registry: AgentProfileRegistry | None = None,
        model_registry: ModelProfileRegistry | None = None,
        state_store: StateStore | None = None,
        approval_mode: ApprovalMode | None = ApprovalMode.BYPASS,
        cwd: str | None = None,
        workspace: Workspace | None = None,
        mcp_overlay: list[MCPServerConfig] | None = None,
        allow_user_interaction: bool = False,
        on_successful_turn: Callable[[], None] | None = None,
        on_turn_started: Callable[[], None] | None = None,
        surface: SessionSurface | None = None,
    ) -> None:
        self._profile_name = profile_name.strip()
        self._restore_session_id = (session_id or "").strip() or None
        if not self._profile_name and not self._restore_session_id:
            error_message = "profile_name is required."
            raise ValueError(error_message)
        self._bus = event_bus or EventBus()
        self._agent_registry = agent_registry or AgentProfileRegistry()
        self._model_registry = model_registry or ModelProfileRegistry()
        self._state_store = state_store or JsonFileStateStore()
        self._cwd = _normalize_cwd(cwd)
        self._workflow_catalog: WorkflowCatalog | None = None
        self._workflow_selection: WorkflowSessionSelection | None = None
        self._workspace = workspace
        self._mcp_overlay = list(mcp_overlay or [])
        self._allow_user_interaction = allow_user_interaction
        self._started = False
        self._run_lock = asyncio.Lock()
        self._cancel_requested = False
        self._last_turn_outcome: TurnOutcome | None = None
        self._engine = assemble_agent_engine(
            self._bus,
            settings,
            loaded_settings=loaded_settings,
            agent_registry=self._agent_registry,
            model_registry=self._model_registry,
            state_store=self._state_store,
            initial_approval_mode=approval_mode,
            mcp_overlay=self._mcp_overlay,
            initial_workspace=workspace or (Workspace.from_cwd(self._cwd) if self._cwd else None),
            on_successful_turn=on_successful_turn,
            on_turn_started=on_turn_started,
            allow_user_interaction=allow_user_interaction,
            surface=surface,
        )
        self._approval_mode = self._engine.session.approval_mode

    @property
    def event_bus(self) -> EventBus:
        """Event bus used by this host."""
        return self._bus

    @property
    def engine(self) -> AgentEngine:
        """Engine owned by this host."""
        return self._engine

    @property
    def _settings(self) -> Settings:
        """The engine's current settings, not a copy taken at construction.

        Held nowhere here on purpose: the engine owns the handle, and a second
        field would answer with whatever was true when this host was built —
        stale from the first reload or session restore onwards.
        """
        return self._engine.settings

    @property
    def session_id(self) -> str | None:
        """Current canonical session id, or ``None`` before start."""
        return self._engine.session_id

    @property
    def approval_mode(self) -> ApprovalMode:
        """Initial approval mode used when the engine was built."""
        return self._approval_mode

    @property
    def vision_enabled(self) -> bool:
        """Whether the currently built runtime model accepts image input."""
        return self._engine.runtime_details.model.vision

    @property
    def image_send_limit_bytes(self) -> int:
        """The most bytes an image sent with a prompt takes; a larger one is compressed."""
        return self._engine.settings.image_send_limit_bytes()

    @property
    def running_background_tasks(self) -> int:
        """How many of the session's shell commands run in the background; shutting down stops them."""
        tasks = self._engine.session.background_tasks
        return len(tasks.running) if tasks is not None else 0

    @property
    def last_turn_outcome(self) -> TurnOutcome | None:
        """Structured outcome for the most recently completed streamed turn."""
        return self._last_turn_outcome

    async def start(self) -> None:
        """Start a fresh session or restore the configured existing session.

        Idempotent. Callers that need to observe startup/restore events through
        ``iter_run_events()`` should let the first run drive startup so the
        event stream is already subscribed before load events are published.
        """
        if self._started:
            return
        self._load_registries_if_needed()
        if self._restore_session_id:
            await self.restore_session(self._restore_session_id)
        else:
            if not self._profile_name:
                error_message = "profile_name is required to start a new session."
                raise ValueError(error_message)
            profile = self._resolve_profile(self._profile_name)
            self._profile_name = profile.name
            await self._engine.start(profile)
            self._started = True

    async def restore_session(self, session_id: str) -> None:
        """Restore a saved session into this host."""
        target_session_id = await self._resolve_restore_session_id(session_id)
        if not target_session_id:
            error_message = "session_id is required to restore a session."
            raise ValueError(error_message)
        self._load_registries_if_needed()
        if self._profile_name:
            self._profile_name = self._resolve_profile(self._profile_name).name
        await self._engine.prepare()
        # Promote a requested short id to the canonical id used by restore events and future idempotent starts.
        self._restore_session_id = target_session_id
        loop = asyncio.get_running_loop()
        restored: asyncio.Future[SessionRestored] = loop.create_future()

        async def _on_restored(event: SessionRestored) -> None:
            if event.session_id == target_session_id and not restored.done():
                restored.set_result(event)

        async def _on_error(event: Error) -> None:
            if event.session_id == target_session_id and not restored.done():
                restored.set_exception(HeadlessRunError(event))

        await self._bus.subscribe(SessionRestored, _on_restored)
        await self._bus.subscribe(Error, _on_error)
        try:
            await self._bus.publish(
                SessionRestore(
                    session_id=target_session_id,
                    primary_cwd=self._cwd,
                    profile_name=self._profile_name,
                    working_dirs=(
                        [d.path for d in self._workspace.working_dirs if not d.is_primary]
                        if self._workspace is not None
                        else None
                    ),
                ),
                raise_handler_errors=True,
            )
            await restored
        finally:
            await self._bus.unsubscribe(SessionRestored, _on_restored)
            await self._bus.unsubscribe(Error, _on_error)
            if restored.done() and not restored.cancelled():
                # A failing restore publishes its Error *and* re-raises out of
                # the publish above, so the future's copy of the failure goes
                # unawaited; retrieve it or asyncio logs it as never-retrieved.
                restored.exception()
        self._started = True

    async def iter_run_events(self, message: str | UserMessage) -> AsyncIterator[Event]:
        """Run one user turn and yield backend events until the engine run finishes."""
        async for event in self._iter_turn_events(message, raise_for_outcome=True):
            yield event

    async def iter_turn_events(self, message: str | UserMessage) -> AsyncIterator[Event]:
        """Run one user turn and yield events, recording a structured terminal outcome."""
        async for event in self._iter_turn_events(message, raise_for_outcome=False):
            yield event

    async def iter_retry_events(self, text: str | None = None) -> AsyncIterator[Event]:
        """Retry the pending turn and yield events, recording a structured terminal outcome.

        *text* is optional guidance for the retry, not a new prompt: it is
        persisted inside the turn being retried. A retry is admitted only when
        the engine is willing (:attr:`AgentEngine.can_retry`); otherwise this
        runs nothing and resolves every caller the same way a turn that
        produced no answer does.
        """
        async for event in self._iter_run_events(
            UserRetry(text=text or "", session_id=self.session_id), raise_for_outcome=False
        ):
            yield event

    async def _iter_turn_events(self, message: str | UserMessage, *, raise_for_outcome: bool) -> AsyncIterator[Event]:
        async for event in self._iter_run_events(
            self._coerce_user_message(message), raise_for_outcome=raise_for_outcome
        ):
            yield event

    async def _iter_run_events(
        self, event: UserMessage | UserRetry, *, raise_for_outcome: bool
    ) -> AsyncIterator[Event]:
        if self._run_lock.locked():
            error_message = "Concurrent turns are not supported for a ChrysSessionHost."
            raise RuntimeError(error_message)
        async with self._run_lock:
            self._last_turn_outcome = None
            self._cancel_requested = False
            async for event_out in self._iter_run_events_locked(event, raise_for_outcome=raise_for_outcome):
                yield event_out

    async def cancel_current_turn(self) -> None:
        """Request cancellation for the active turn, if any."""
        self._cancel_requested = True
        await self._bus.publish(UserInterrupt(session_id=self.session_id))

    async def _iter_run_events_locked(
        self,
        turn_event: UserMessage | UserRetry,
        *,
        raise_for_outcome: bool,
    ) -> AsyncIterator[Event]:
        """Run one user turn while the host run lock is held.

        *turn_event* is the one event this turn publishes to start it: a user
        message for a fresh turn, a retry for a turn being resumed.
        """
        final: InvocationMessage | None = None
        error: Error | None = None
        question: QuestionToUser | None = None
        next_event_task: asyncio.Task[Event] | None = None
        start_task: asyncio.Task[None] | None = None
        run_task: asyncio.Task[None] | None = None

        def _record_event(event: Event) -> None:
            nonlocal final, error, question
            # Only the terminal state is kept: a caller that wants the turn's
            # events reads them from this iterator as they stream.
            if isinstance(event, InvocationMessage) and event.origin.kind == "turn" and event.is_final:
                final = event
            elif isinstance(event, Error) and event.refused_input != "injection":
                # A refused injection refuses only that message: the turn it was sent into goes on,
                # and its own outcome (a real error included) stays what the turn makes it.
                error = event
            elif isinstance(event, QuestionToUser):
                question = event

        async with self._bus.stream(*_HEADLESS_RUN_EVENT_TYPES) as stream:
            ask_user_interrupted = False

            async def _publish_turn_event() -> None:
                nonlocal run_task
                await self._bus.publish(turn_event)
                run_task = asyncio.create_task(self._engine.wait_for_run_task())

            async def _event_task_ready_after_checkpoint() -> bool:
                if next_event_task is not None and next_event_task.done():
                    return True
                # EventBus.publish queues stream events synchronously; this checkpoint lets those tasks win first.
                # Revisit if the bus later grows asynchronous fan-out.
                await asyncio.sleep(0)
                return next_event_task is not None and next_event_task.done()

            next_event_task = asyncio.create_task(stream.__anext__())
            if self._started:
                await _publish_turn_event()
            else:
                start_task = asyncio.create_task(self.start())

            try:
                while True:
                    wait_tasks: set[asyncio.Task[object]] = {next_event_task}
                    if start_task is not None:
                        wait_tasks.add(start_task)
                    if run_task is not None:
                        wait_tasks.add(run_task)
                    done, _pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)

                    if next_event_task in done:
                        event = next_event_task.result()
                        next_event_task = asyncio.create_task(stream.__anext__())
                        if not self._event_belongs_to_session(event, self.session_id, self._restore_session_id):
                            continue
                        if not allows_headless_event(event):
                            continue
                        _record_event(event)
                        yield event
                        if (
                            isinstance(event, QuestionToUser)
                            and not self._allow_user_interaction
                            and not ask_user_interrupted
                        ):
                            ask_user_interrupted = True
                            await self._bus.publish(UserInterrupt(session_id=self.session_id))
                        continue

                    if start_task is not None and start_task in done:
                        if await _event_task_ready_after_checkpoint():
                            continue
                        await start_task
                        start_task = None
                        await _publish_turn_event()
                        continue

                    if run_task is not None and run_task in done:
                        if await _event_task_ready_after_checkpoint():
                            continue
                        await run_task
                        outcome = self._resolve_run_outcome(final=final, error=error, question=question)
                        self._last_turn_outcome = outcome
                        if raise_for_outcome:
                            self._raise_for_run_outcome(outcome)
                        return
            finally:
                if start_task is not None:
                    if start_task.done():
                        self._observe_task_exception(start_task)
                    else:
                        start_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await start_task
                if run_task is not None:
                    if run_task.done():
                        self._observe_task_exception(run_task)
                    else:
                        try:
                            self._cancel_requested = True
                            await self._bus.publish(UserInterrupt(session_id=self.session_id))
                            await asyncio.shield(run_task)
                        finally:
                            self._observe_or_defer_task_exception(run_task)
                if next_event_task is not None and not next_event_task.done():
                    next_event_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await next_event_task

    async def run_until_final(
        self,
        message: str | UserMessage,
        *,
        timeout: float | None = None,
        keep_events: bool = False,
        on_event: Callable[[Event], None] | None = None,
    ) -> HeadlessRunResult:
        """Run one user turn and return the final assistant response.

        The turn's streamed events are retained in ``HeadlessRunResult.events``
        only with *keep_events*: a long turn streams many deltas nobody reads.
        *on_event* sees every event as it streams, before the next one is read.
        """

        async def _run() -> HeadlessRunResult:
            events: list[Event] = []
            final: InvocationMessage | None = None
            async for event in self.iter_run_events(message):
                if on_event is not None:
                    on_event(event)
                if keep_events:
                    events.append(event)
                if isinstance(event, InvocationMessage) and event.origin.kind == "turn" and event.is_final:
                    final = event
            if final is None:
                error_message = "Agent run ended without a final response."
                raise RuntimeError(error_message)
            return HeadlessRunResult(text=final.text, session_id=self.session_id or "", events=events)

        if timeout is None:
            return await _run()
        return await asyncio.wait_for(_run(), timeout=timeout)

    # -- workflows -----------------------------------------------------------------------

    def _project_workspace(self) -> Workspace:
        """The workspace inherited by a new workflow session, copied at admission."""
        return self._engine.workspace or self._workspace or Workspace.from_cwd(self._cwd)

    @property
    def workflow_session_id(self) -> str:
        return self._workflow_selection.session_id if self._workflow_selection is not None else ""

    @property
    def workflow_catalog(self) -> WorkflowCatalog:
        config_dir, project_cwd = (
            get_platform().config_dir,
            Path(
                self._workflow_selection.workspace.primary_cwd
                if self._workflow_selection is not None
                else self._project_workspace().primary_cwd
            ),
        )
        catalog = self._workflow_catalog
        if catalog is None or (catalog.config_dir, catalog.project_cwd) != (config_dir, project_cwd):
            catalog = self._workflow_catalog = WorkflowCatalog(config_dir=config_dir, project_cwd=project_cwd)
        return catalog

    @property
    def workflow_session_dir(self) -> Path | None:
        return self._state_store.session_dir(self.workflow_session_id) if self.workflow_session_id else None

    async def load_workflow_session(self, session_id: str) -> None:
        """Select workflow history and workspace without constructing a chat agent."""
        normalized = session_id.strip().replace("-", "").lower()
        meta = await self._state_store.load_session_meta(normalized, strict=True)
        if meta is None or normalized != (
            session_short_id(meta.session_id).lower()
            if len(normalized) <= SESSION_SHORT_ID_LEN
            else meta.session_id.replace("-", "").lower()
        ):
            raise ValueError(f"No workflow session {session_id!r} exists.")
        if meta.kind != "workflow":
            raise ValueError(f"Session {session_id!r} is a Chat session, not a Workflow session.")
        if not meta.run_count:
            raise ValueError(f"Workflow session {session_id!r} has no runs.")
        state = await self._state_store.load_workflow_session(meta.session_id)
        if state is None:
            raise ValueError(f"No workflow session {session_id!r} exists.")
        self._workflow_selection = state.selection(meta.session_id)

    def list_workflows(self) -> Discovery:
        """Active and shadowed workflows, plus unreadable files."""
        return self.workflow_catalog.discover()

    def workflow_target(
        self, workflow_id: str, *, new_session: bool = False, workspace: Workspace | None = None
    ) -> WorkflowTarget:
        """Capture a draft or the selected session; subsequent browsing cannot retarget it."""
        if not new_session and self._workflow_selection is not None:
            if workspace is not None or workflow_id != self._workflow_selection.workflow_id:
                raise WorkflowRunRejectedError(
                    WorkflowRunRejected(
                        error="spec_changed",
                        message="Use a new workflow draft to select another definition or workspace.",
                    )
                )
            return self._workflow_selection
        return WorkflowDraft(workflow_id, WorkspaceSnapshot.capture(workspace or self._project_workspace()))

    async def preview_workflow(
        self, target: WorkflowTarget, *, timeout: float | None = None, trust: bool = False
    ) -> PreparedWorkflow:
        """Prepare the exact target; ``trust`` explicitly authorizes execution of unconfirmed source."""
        catalog = WorkflowCatalog(config_dir=get_platform().config_dir, project_cwd=Path(target.workspace.primary_cwd))
        preview = await catalog.preview(
            target.workflow_id,
            timeout=timeout,
            trust=trust,
            expected_identity=target.identity if isinstance(target, WorkflowSessionSelection) else None,
        )
        return PreparedWorkflow(target, preview)

    def confirm_workflow(self, prepared: PreparedWorkflow) -> None:
        """Confirm a prepared definition independently of the current browsing selection."""
        catalog = WorkflowCatalog(
            config_dir=get_platform().config_dir, project_cwd=Path(prepared.target.workspace.primary_cwd)
        )
        catalog.confirm(prepared.preview)

    async def iter_workflow_events(
        self,
        target: WorkflowTarget | PreparedWorkflow,
        *,
        input_text: str = "",
        timeout: float = 0.0,
        include_node_activity: bool = False,
    ) -> AsyncIterator[Event]:
        """Run one workflow and yield backend events until its ``WorkflowRunFinished``.

        Raises :class:`WorkflowRunRejectedError` when the engine refuses the
        request. Replies to other requests (a retransmitted request id is
        answered with its original reply) are not this run's and are not
        yielded. Closing the iterator early (``contextlib.aclosing``) cancels
        the run and waits for it; so does a cancellation that lands while the
        request is still being published.

        ``timeout`` is in seconds and covers admission and execution. Expiry before acceptance
        raises :class:`WorkflowRunTimeoutError`; an accepted run finishes as
        cancelled with reason ``deadline_exceeded``. Both paths await cleanup.

        With ``include_node_activity`` the stream also carries this run's agent-node activity
        (:func:`allows_workflow_node_event`), admitted only for invocations one of this run's
        ``WorkflowNodeStateChanged`` events announced: the runner binds an activation's invocation id before
        publishing ``running``, so a same-session invocation from any other run is never yielded.
        """
        if self._run_lock.locked():
            error_message = "Concurrent runs are not supported for a ChrysSessionHost."
            raise RuntimeError(error_message)
        async with self._run_lock, self._bus.stream(*_HEADLESS_RUN_EVENT_TYPES) as stream:
            self._load_registries_if_needed()
            await self._engine.prepare()
            request_id = new_analytics_id()
            request = (
                target.run_request(input_text=input_text, request_id=request_id, timeout=timeout)
                if isinstance(target, PreparedWorkflow)
                else WorkflowRunRequest(target=target, input_text=input_text, request_id=request_id, timeout=timeout)
            )
            run_id: str | None = None
            execution_session_id: str | None = None
            owned_invocations: set[str] = set()
            rejected = False
            finished = False
            timed_out = False

            async def enforce_deadline() -> None:
                nonlocal timed_out
                await asyncio.sleep(timeout)
                workflows = self._engine.workflows
                if workflows.active_request_id == request_id:
                    timed_out = workflows.cancel_active(reason=REASON_DEADLINE_EXCEEDED)

            # Own a timer independently of iterator consumption: never put a timeout context across yield.
            deadline_task = (
                asyncio.create_task(enforce_deadline(), name=f"chrys.workflow.deadline.{request_id}")
                if timeout > 0
                else None
            )
            try:
                # Published inside the try: a subscriber behind the coordinator can hold the publish open after
                # the run was admitted, and a cancellation landing there must still give the run up below.
                await self._bus.publish(request)
                async for event in stream:
                    node_activity = include_node_activity and allows_workflow_node_event(event)
                    if not node_activity and not allows_headless_event(event):
                        continue
                    if isinstance(event, WorkflowRunAccepted | WorkflowRunRejected) and event.request_id != request_id:
                        continue  # the reply to someone else's request, a replayed acceptance included
                    if isinstance(event, WorkflowRunRejected):
                        rejected = True
                        if timed_out:
                            raise WorkflowRunTimeoutError("Workflow run timed out during admission.")
                        raise WorkflowRunRejectedError(event)
                    if isinstance(event, WorkflowRunAccepted):
                        run_id = event.run_id
                        execution_session_id = event.session_id
                        self._workflow_selection = event.selection
                    elif isinstance(event, Warning) and event.request_id:
                        if event.request_id != request_id:
                            continue
                    elif run_id is None or event.session_id != execution_session_id:
                        continue
                    if (
                        isinstance(
                            event,
                            WORKFLOW_RUN_EVENTS,
                        )
                        and event.run_id != run_id
                    ):
                        continue
                    if isinstance(event, WorkflowNodeStateChanged) and event.invocation_id:
                        owned_invocations.add(event.invocation_id)
                    if (
                        node_activity
                        and isinstance(event, InvocationEvent)
                        and event.origin.root.invocation_id not in owned_invocations
                    ):
                        continue
                    yield event
                    if isinstance(event, WorkflowRunFinished):
                        finished = True
                        return
            finally:
                if deadline_task is not None:
                    deadline_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await deadline_task
                # Only this request's run is ours to give up; a refused request leaves the active run alone.
                workflows = self._engine.workflows
                if workflows.active_request_id == request_id:
                    if not finished and not rejected:
                        await asyncio.shield(workflows.abandon_active())
                    await asyncio.shield(workflows.wait_idle())

    async def run_workflow_until_final(
        self,
        target: WorkflowTarget | PreparedWorkflow,
        *,
        input_text: str = "",
        timeout: float = 0.0,
    ) -> WorkflowRunResult:
        """Run one workflow to its terminal outcome and return the full result (outputs included)."""
        run_id = ""
        async for event in self.iter_workflow_events(
            target,
            input_text=input_text,
            timeout=timeout,
        ):
            if isinstance(event, WorkflowRunAccepted):
                run_id = event.run_id
        result = self._engine.workflows.result(run_id)
        if result is None:
            error_message = "Workflow run ended without a recorded result."
            raise RuntimeError(error_message)
        return result

    async def cancel_workflow(self) -> None:
        """Request cancellation of the active workflow run, if any."""
        run_id = self._engine.workflows.active_run_id
        if run_id is not None:
            await self._bus.publish(WorkflowCancelRequest(run_id=run_id, session_id=self.workflow_session_id or None))

    async def shutdown(self) -> None:
        """Shutdown the owned engine and release session resources."""
        await self._engine.shutdown()
        self._started = False

    def _load_registries_if_needed(self) -> None:
        if not self._agent_registry.list_names():
            self._agent_registry.load_all()
        if not self._model_registry.list_ids():
            self._model_registry.load_all()

    def _resolve_profile(self, name: str) -> AgentProfile:
        profile = self._agent_registry.resolve_selector(name)
        if profile is not None:
            return profile
        available_names = sorted(self._agent_registry.list_names())
        available = ", ".join(available_names)
        error_message = f"Agent profile not found: {name}"
        if available_names:
            error_message = f"{error_message}. Available profiles: {available}"
            display_message = _AGENT_PROFILE_NOT_FOUND_WITH_AVAILABLE.bind(
                name=name,
                available=DisplaySequence(available_names),
            )
        else:
            display_message = _AGENT_PROFILE_NOT_FOUND.bind(name=name)
        raise AgentProfileNotFoundError(error_message, display_message=display_message)

    async def _resolve_restore_session_id(self, session_id: str) -> str:
        raw = (session_id or "").strip()
        if not raw:
            return ""
        sessions = await self._state_store.list_sessions()
        normalized = raw.replace("-", "").lower()
        if len(normalized) <= SESSION_SHORT_ID_LEN:
            matches = [meta.session_id for meta in sessions if session_short_id(meta.session_id).lower() == normalized]
        else:
            matches = [
                meta.session_id
                for meta in sessions
                if meta.session_id == raw or meta.session_id.replace("-", "").lower() == normalized
            ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            error_message = f"Session id '{raw}' is ambiguous."
            raise AmbiguousSessionIdError(
                error_message,
                display_message=_SESSION_ID_AMBIGUOUS.bind(session_id=raw),
            )
        recent_ids = [session_short_id(meta.session_id) for meta in sessions[:5]]
        recent = ", ".join(recent_ids)
        error_message = f"Session not found: {raw}"
        if recent_ids:
            error_message = f"{error_message}. Recent sessions: {recent}"
            display_message = _SESSION_NOT_FOUND_WITH_RECENT.bind(
                session_id=raw,
                recent=DisplaySequence(recent_ids),
            )
        else:
            display_message = _SESSION_NOT_FOUND.bind(session_id=raw)
        raise SessionNotFoundError(error_message, display_message=display_message)

    @staticmethod
    def _event_belongs_to_session(
        event: Event,
        session_id: str | None,
        restore_session_id: str | None = None,
    ) -> bool:
        if not event.session_id:
            # Headless streams ignore global/sessionless events so unrelated errors cannot terminate this run.
            return False
        return event.session_id == session_id or (
            restore_session_id is not None and event.session_id == restore_session_id
        )

    @staticmethod
    def _observe_task_exception(task: asyncio.Task[object]) -> None:
        """Mark a completed helper task's exception as retrieved."""
        if task.done() and not task.cancelled():
            task.exception()

    @classmethod
    def _observe_or_defer_task_exception(cls, task: asyncio.Task[object]) -> None:
        """Observe a helper task now, or once it finishes after caller cancellation."""
        if task.done():
            cls._observe_task_exception(task)
            return
        task.add_done_callback(cls._observe_task_exception)

    def _raise_for_run_outcome(self, outcome: TurnOutcome) -> None:
        if isinstance(outcome, Errored | Refused):
            raise HeadlessRunError(outcome.error)
        if isinstance(outcome, Cancelled):
            error_message = outcome.reason or "Agent run was cancelled."
            raise RuntimeError(error_message)

    def _resolve_run_outcome(
        self,
        *,
        final: InvocationMessage | None,
        error: Error | None,
        question: QuestionToUser | None,
    ) -> TurnOutcome:
        if question is not None and not self._allow_user_interaction:
            # Headless runs have no one to answer, so a question terminates the turn.
            # Interactive hosts (e.g. ACP) answer ask_user inline, so the run continues
            # to a real final/error outcome and the question must not fail the turn.
            return Errored(error=self._question_to_error(question))
        if self._cancel_requested:
            return Cancelled(reason="Agent run was cancelled.")
        if error is not None and error.refused_input in ("prompt", "retry_note"):
            return Refused(error=error)
        if error is not None:
            return Errored(error=error)
        if final is not None:
            return EndTurn(final_text=final.text)
        return Errored(
            error=Error(
                code="no_final_response",
                message="Agent run ended without a final response.",
                display_message=_NO_FINAL_RESPONSE.bind(),
                session_id=self.session_id,
            )
        )

    def _coerce_user_message(self, message: str | UserMessage) -> UserMessage:
        if isinstance(message, UserMessage):
            return replace(message, session_id=self.session_id)
        return UserMessage(text=message, session_id=self.session_id)

    @staticmethod
    def _question_to_error(event: QuestionToUser) -> Error:
        details = [question.question.strip() for question in event.questions if question.question.strip()]
        detail = (
            " ".join(f"{index}. {question}" for index, question in enumerate(details, start=1))
            if details
            else "The agent requested user input."
        )
        return Error(
            code="headless_interaction_required",
            message=f"Agent requested user input in headless mode: {detail}",
            display_message=_HEADLESS_INTERACTION_REQUIRED.bind(detail=DisplayBlock(detail)),
            session_id=event.session_id,
        )
