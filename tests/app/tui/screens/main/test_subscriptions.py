# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for main-screen EventBus subscription ownership."""

from __future__ import annotations

from chrys.app.tui.screens.main.subscriptions import MainScreenSubscriptions
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    InvocationContextPressure,
    InvocationToolCallArgsUpdated,
    InvocationToolCallProgress,
    InvocationToolCallStatusUpdated,
    RollbackResult,
    SessionReady,
    SettingsReloaded,
    TodoListUpdated,
    Warning,
)
from chrys.foundation.models.invocations import InvocationOrigin


class _Handlers:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __getattr__(self, name: str):
        async def _handler(_event: object) -> None:
            self.calls.append(name)

        setattr(self, name, _handler)
        return _handler


async def _rollback_handler(_event: object) -> None:
    return


async def test_subscribe_all_registers_each_handler_once() -> None:
    bus = EventBus()
    events = _Handlers()
    sessions = _Handlers()
    subscriptions = MainScreenSubscriptions(
        bus=bus,
        events=events,  # type: ignore[arg-type]
        sessions=sessions,  # type: ignore[arg-type]
        rollback_result_handler=_rollback_handler,
        execution_handler=_rollback_handler,
        prompt_commands_running_handler=_rollback_handler,
        prompt_commands_ended_handler=_rollback_handler,
    )

    await subscriptions.subscribe_all()
    await subscriptions.subscribe_all()

    assert len(subscriptions.subscriptions) == len({sub.event_type for sub in subscriptions.subscriptions})
    assert {sub.event_type for sub in subscriptions.subscriptions} >= {
        SessionReady,
        SettingsReloaded,
        Warning,
        InvocationContextPressure,
        RollbackResult,
        TodoListUpdated,
        InvocationToolCallStatusUpdated,
        InvocationToolCallArgsUpdated,
        InvocationToolCallStatusUpdated,
        InvocationToolCallProgress,
    }
    for subscription in subscriptions.subscriptions:
        assert bus._handlers[subscription.event_type].count(subscription.handler) == 1


async def test_unsubscribe_all_is_idempotent_and_remount_does_not_duplicate_handlers() -> None:
    bus = EventBus()
    events = _Handlers()
    sessions = _Handlers()
    subscriptions = MainScreenSubscriptions(
        bus=bus,
        events=events,  # type: ignore[arg-type]
        sessions=sessions,  # type: ignore[arg-type]
        rollback_result_handler=_rollback_handler,
        execution_handler=_rollback_handler,
        prompt_commands_running_handler=_rollback_handler,
        prompt_commands_ended_handler=_rollback_handler,
    )

    await subscriptions.subscribe_all()
    await subscriptions.unsubscribe_all()
    await subscriptions.unsubscribe_all()
    await subscriptions.subscribe_all()
    await subscriptions.subscribe_all()

    await bus.publish(Warning(message="heads up"))
    await bus.publish(
        InvocationContextPressure(origin=InvocationOrigin("turn", "", "test-turn", None), reason="round_limit")
    )
    await bus.publish(SessionReady(session_id="session-1", agent_profile="Code"))
    await bus.publish(TodoListUpdated(session_id="session-1"))

    assert events.calls.count("on_warning") == 1
    assert events.calls.count("on_context_pressure") == 1
    assert events.calls.count("on_session_ready") == 1
    assert events.calls.count("on_todo_list_updated") == 1

    await subscriptions.unsubscribe_all()
    for subscription in subscriptions.subscriptions:
        assert subscription.handler not in bus._handlers.get(subscription.event_type, [])


async def test_tui_routes_child_prose_activity_and_control() -> None:
    from chrys.foundation.events.types import (
        InvocationContentDelta,
        InvocationContentFinished,
        InvocationMessage,
        InvocationPaused,
        InvocationProgress,
    )

    bus = EventBus()
    events = _Handlers()
    subscriptions = MainScreenSubscriptions(
        bus=bus,
        events=events,  # type: ignore[arg-type]
        sessions=_Handlers(),  # type: ignore[arg-type]
        rollback_result_handler=_rollback_handler,
        execution_handler=_rollback_handler,
        prompt_commands_running_handler=_rollback_handler,
        prompt_commands_ended_handler=_rollback_handler,
    )
    routes = {
        InvocationMessage: "on_agent_message",
        InvocationContentDelta: "on_agent_content",
        InvocationContentFinished: "on_agent_content",
        InvocationProgress: "on_sub_agent_progress",
        InvocationPaused: "on_sub_agent_paused",
    }
    await subscriptions.subscribe_all()
    try:
        registered = {item.event_type: item.handler for item in subscriptions.subscriptions}
        assert routes.keys() <= registered.keys()
        for event_type, handler_name in routes.items():
            # Dispatch the original event object; only subscription routing is under test.
            event = event_type(agent_name="Explore", origin=InvocationOrigin("sub_agent", "", "child-1", None))
            await bus.publish(event, raise_handler_errors=True)
            assert events.calls[-1] == handler_name
        assert events.calls == list(routes.values())
    finally:
        await subscriptions.unsubscribe_all()
