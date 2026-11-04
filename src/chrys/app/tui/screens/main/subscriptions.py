# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""EventBus subscription ownership for the main screen."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chrys.foundation.events.types import (
    AgentLoadFailed,
    AgentLoadFinished,
    AgentLoadProgress,
    AgentLoadStarted,
    AgentRuntimeUpdated,
    AgentThinking,
    ApprovalCancelled,
    ApprovalModeUpdated,
    ApprovalRequest,
    ApprovalReviewed,
    AskUserTimedOut,
    BackgroundTasksChanged,
    CompactionFinished,
    CompactionStarted,
    ContextCompressed,
    Error,
    Event,
    ExecutionChanged,
    HookConfirmationRequest,
    HookConfirmationWithdrawn,
    HookExecutionsChanged,
    HookRunsChanged,
    ImageAttachmentCompressionFinished,
    ImageAttachmentCompressionStarted,
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
    ProfileSwitched,
    PromptCommandsEnded,
    PromptCommandsRunning,
    QuestionToUser,
    RollbackResult,
    SessionForked,
    SessionReady,
    SessionRestored,
    SessionSaved,
    SessionTitleUpdated,
    SettingsReloaded,
    TodoListUpdated,
    ToolCompacted,
    UsageUpdate,
    UserInjectResult,
    Warning,
    WorkspaceUpdated,
)

if TYPE_CHECKING:
    from chrys.app.tui.screens.main.event_handlers import BackendEventHandler
    from chrys.app.tui.screens.main.session_handlers import SessionHandler
    from chrys.foundation.events.bus import EventBus


type EventHandler[E: Event] = Callable[[E], Awaitable[None]]


@dataclass(frozen=True)
class EventSubscription[E: Event]:
    """One EventBus subscription owned by the main screen."""

    event_type: type[E]
    handler: EventHandler[E]


def _sub[E: Event](event_type: type[E], handler: EventHandler[E]) -> EventSubscription[Any]:
    """Create a subscription while checking its event/handler pairing."""
    return EventSubscription(event_type, handler)


class MainScreenSubscriptions:
    """Subscribe and unsubscribe the main screen's backend event handlers."""

    def __init__(
        self,
        *,
        bus: EventBus,
        events: BackendEventHandler,
        sessions: SessionHandler,
        rollback_result_handler: EventHandler[RollbackResult],
        execution_handler: EventHandler[ExecutionChanged],
        prompt_commands_running_handler: EventHandler[PromptCommandsRunning],
        prompt_commands_ended_handler: EventHandler[PromptCommandsEnded],
    ) -> None:
        self._bus = bus
        self._subscriptions: tuple[EventSubscription[Any], ...] = (
            _sub(ExecutionChanged, execution_handler),
            _sub(SessionReady, events.on_session_ready),
            _sub(AgentLoadStarted, events.on_agent_load_started),
            _sub(AgentLoadProgress, events.on_agent_load_progress),
            _sub(AgentLoadFinished, events.on_agent_load_finished),
            _sub(AgentLoadFailed, events.on_agent_load_failed),
            _sub(AgentRuntimeUpdated, events.on_agent_runtime_updated),
            _sub(ImageAttachmentCompressionStarted, events.on_image_attachment_compression_started),
            _sub(ImageAttachmentCompressionFinished, events.on_image_attachment_compression_finished),
            _sub(PromptCommandsRunning, prompt_commands_running_handler),
            _sub(PromptCommandsEnded, prompt_commands_ended_handler),
            _sub(AgentThinking, events.on_agent_thinking),
            _sub(InvocationMessage, events.on_agent_message),
            _sub(InvocationContentDelta, events.on_agent_content),
            _sub(InvocationContentFinished, events.on_agent_content),
            _sub(InvocationContentAbandoned, events.on_agent_content),
            _sub(InvocationToolCallStart, events.on_tool_start),
            _sub(InvocationToolCallArgsUpdated, events.on_tool_args_updated),
            _sub(InvocationToolCallStatusUpdated, events.on_tool_status_updated),
            _sub(InvocationToolCallProgress, events.on_tool_progress),
            _sub(InvocationToolCallResult, events.on_tool_result),
            _sub(InvocationStarted, events.on_sub_agent_invocation_start),
            _sub(InvocationProgress, events.on_sub_agent_progress),
            _sub(InvocationCompactionStarted, events.on_sub_agent_compaction_started),
            _sub(InvocationCompactionFinished, events.on_sub_agent_compaction_finished),
            _sub(InvocationCompactionCommitted, events.on_sub_agent_compaction_committed),
            _sub(InvocationPaused, events.on_sub_agent_paused),
            _sub(InvocationResumed, events.on_sub_agent_resumed_after_pause),
            _sub(InvocationAnswerWithdrawn, events.on_answer_withdrawn),
            _sub(InvocationCascadeAborted, events.on_sub_agent_cascade_aborted),
            _sub(InvocationAborted, events.on_sub_agent_aborted),
            _sub(ApprovalRequest, events.on_approval_request),
            _sub(ApprovalCancelled, events.on_approval_cancelled),
            _sub(ApprovalReviewed, events.on_approval_reviewed),
            _sub(ApprovalModeUpdated, events.on_approval_mode_updated),
            _sub(QuestionToUser, events.on_question_to_user),
            _sub(HookConfirmationRequest, events.on_hook_confirmation_request),
            _sub(HookConfirmationWithdrawn, events.on_hook_confirmation_withdrawn),
            _sub(AskUserTimedOut, events.on_ask_user_timed_out),
            _sub(UsageUpdate, events.on_usage_update),
            _sub(TodoListUpdated, events.on_todo_list_updated),
            _sub(BackgroundTasksChanged, events.on_background_tasks_changed),
            _sub(HookExecutionsChanged, events.on_hook_executions_changed),
            _sub(HookRunsChanged, events.on_hook_runs_changed),
            _sub(ToolCompacted, events.on_tool_compacted),
            _sub(CompactionStarted, events.on_compaction_started),
            _sub(CompactionFinished, events.on_compaction_finished),
            _sub(ContextCompressed, events.on_context_compressed),
            _sub(InvocationContextPressure, events.on_context_pressure),
            _sub(UserInjectResult, events.on_injection_outcome),
            _sub(Error, events.on_error),
            _sub(InvocationRetryAttempt, events.on_retry_attempt),
            _sub(Warning, events.on_warning),
            _sub(SettingsReloaded, events.on_settings_reloaded),
            _sub(SessionRestored, sessions.on_session_restored),
            _sub(SessionSaved, sessions.on_session_saved),
            _sub(SessionTitleUpdated, sessions.on_session_title_updated),
            _sub(SessionForked, sessions.on_session_forked),
            _sub(ProfileSwitched, sessions.on_profile_switched),
            _sub(WorkspaceUpdated, sessions.on_workspace_updated),
            _sub(RollbackResult, rollback_result_handler),
        )
        self._subscribed = False

    @property
    def subscriptions(self) -> tuple[EventSubscription[Any], ...]:
        """Return the owned subscription table."""
        return self._subscriptions

    async def subscribe_all(self) -> None:
        """Register all handlers, once."""
        if self._subscribed:
            return
        for subscription in self._subscriptions:
            await self._bus.subscribe(subscription.event_type, subscription.handler)
        self._subscribed = True

    async def unsubscribe_all(self) -> None:
        """Remove all registered handlers, if currently subscribed."""
        if not self._subscribed:
            return
        for subscription in reversed(self._subscriptions):
            await self._bus.unsubscribe(subscription.event_type, subscription.handler)
        self._subscribed = False
