# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Observer flags, lazy structured output and isolated audit snapshots."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import Error
from chrys.kernel import AgentResponse, Content, Message
from chrys.orchestration.engine.run.turn_hooks import TurnHookDispatcher
from chrys.orchestration.invoker.contracts import Aborted, Failed, Ok, RunIntent, RunRequest, StaleContinuation
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.llm.mock import MockChatClient, MockResponse
from tests.orchestration.invoker._main_pass import fresh_pass
from tests.support.loaded_agents import make_manifest


async def test_error_then_stop_preserves_old_dual_flags(executor):
    backend = executor.backend
    backend._attempts.run = create_autospec(backend._attempts.run, side_effect=ValueError("failed first"))

    async def stop_on_error(event):
        await executor.interrupt()

    await executor._bus.subscribe(Error, stop_on_error)
    try:
        await fresh_pass(executor, ["work"])
        assert executor.state.was_interrupted is True
        assert executor.state.run_failed is True
        assert executor.state.last_error == "failed first"
        hooks = create_autospec(HookManager, instance=True)
        hooks.has_hooks_for.return_value = True
        host = SimpleNamespace(
            session=SimpleNamespace(
                hook_manager=hooks,
                agent_profile=None,
                session_id="session",
                workspace=None,
                turn_number=1,
                runtime_meta=None,
            ),
            current=SimpleNamespace(loaded=SimpleNamespace(bindings=executor), manifest=make_manifest()),
        )
        await TurnHookDispatcher(session=host.session, current=host.current).fire_after_turn(failed=True)
        hooks.fire.assert_awaited_once()
        assert hooks.fire.call_args.args[0] is HookEvent.AFTER_TURN
        assert hooks.fire.call_args.args[1]["status"] == "failed"
        assert hooks.fire.call_args.args[1]["failed"] is True
    finally:
        await executor._bus.unsubscribe(Error, stop_on_error)


async def test_interrupt_before_error_suppresses_error_event(executor):
    events = []

    async def capture(event):
        events.append(event)

    async def attempt(*args, **kwargs):
        await executor.interrupt()
        raise ValueError("already interrupted")

    executor.backend._attempts.run = create_autospec(executor.backend._attempts.run, side_effect=attempt)
    await executor._bus.subscribe(Error, capture)
    try:
        await fresh_pass(executor, ["work"])
        assert isinstance(executor.inputs.outcome, Aborted)
        assert executor.state.was_interrupted is True
        assert executor.state.run_failed is False
        assert executor.state.last_error == ""
        assert events == []
    finally:
        await executor._bus.unsubscribe(Error, capture)


@pytest.mark.parametrize("shell", ["turn", "child"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("text", ['{"answer": 42}', "not JSON"])
async def test_structured_format_does_not_add_eager_validation(executor, stream, text, shell):
    from chrys.foundation.events.bus import EventBus
    from chrys.kernel import Agent
    from tests.orchestration.sub_agents._controller_fixtures import _make_controller

    client = MockChatClient(responses=[MockResponse(text=text, chunk_delay=0)])
    options = {"response_format": {"type": "json_object"}}
    controller = None
    try:
        if shell == "turn":
            executor._stream = stream
            executor._chat_options = options
            executor._agent.client = client
            await fresh_pass(executor, ["work"])
            outcome = executor.inputs.outcome
        else:
            controller = _make_controller(
                Agent(client=client), EventBus(), stream=stream, run_kwargs={"options": options}
            )
            outcome = await controller.policy.backend.run(
                RunRequest([Message("user", ["work"])], RunIntent.FRESH, controller.origin)
            )
        assert client.call_count == 1
        assert isinstance(outcome, Ok)
        assert outcome.structured_output is None
        assert isinstance(outcome.backend_payload, AgentResponse)
        assert outcome.backend_payload.text == text
    finally:
        if controller is not None:
            await controller.policy.backend.owner.aclose()


async def test_restore_rejects_previously_live_ticket_without_effects(executor):
    backend = executor.backend
    backend.history_state = {"messages": [Message("user", ["kept"])]}
    snapshot = backend.checkpoint()
    backend._attempts.run = create_autospec(backend._attempts.run, side_effect=ValueError("failed"))
    await fresh_pass(executor, ["work"])
    failed = executor.inputs.outcome
    assert isinstance(failed, Failed)
    ticket = failed.continuation
    assert ticket is not None
    assert backend.continuation_is_live(ticket, executor.inputs.origin) is True
    backend.restore(snapshot)
    assert backend.continuation_is_live(ticket, executor.inputs.origin) is False
    with pytest.raises(StaleContinuation):
        await backend.run(RunRequest([], RunIntent.RETRY, executor.inputs.origin, ticket))
    assert backend._attempts.run.await_count == 1
    assert backend.history_state["messages"][0].text == "kept"


async def test_export_audit_isolated_from_live_history_and_ticket(executor):
    backend = executor.backend
    live = Message("user", ["kept"])
    backend.history_state = {"messages": [live]}
    backend._attempts.run = create_autospec(backend._attempts.run, side_effect=ValueError("failed"))
    await fresh_pass(executor, ["work"])
    failed = executor.inputs.outcome
    assert isinstance(failed, Failed)
    ticket = failed.continuation
    generation = backend.state_generation
    exported = backend.export_audit()
    messages = exported["state"]["chrys_history"]["messages"]
    assert messages[0] is not live
    assert messages[0].contents[0] is not live.contents[0]
    messages[0].contents[0].text = "changed"
    messages.clear()
    messages.append(Message("user", ["foreign"]))
    assert backend.history_state["messages"] == [live]
    assert live.text == "kept"
    assert backend.state_generation == generation
    assert backend.continuation_is_live(ticket, executor.inputs.origin) is True


async def test_audit_nested_metadata_and_live_identity(executor):
    backend = executor.backend
    content = Content.from_text("kept")
    content.additional_properties["nested"] = {"values": [1]}
    message = Message("user", [content], additional_properties={"nested": {"values": [2]}})
    backend.history_state = {"messages": [message], "nested": {"values": [3]}}
    state = backend.session.state
    history = backend.history_state
    messages = history["messages"]
    backend._attempts.run = create_autospec(backend._attempts.run, side_effect=ValueError("failure"))
    await fresh_pass(executor, ["work"])
    assert isinstance(executor.inputs.outcome, Failed)
    ticket = executor.inputs.outcome.continuation
    exported = backend.export_audit()["state"]["chrys_history"]
    exported["messages"][0].contents[0].additional_properties["nested"]["values"].append(7)
    exported["messages"][0].additional_properties["nested"]["values"].append(8)
    exported["nested"]["values"].append(9)
    assert content.additional_properties["nested"]["values"] == [1]
    assert message.additional_properties["nested"]["values"] == [2]
    assert history["nested"]["values"] == [3]
    assert backend.session.state is state
    assert backend.history_state is history
    assert backend.history_state["messages"] is messages
    assert messages[0] is message
    assert messages[0].contents[0] is content
    assert backend.continuation_is_live(ticket, executor.inputs.origin)
