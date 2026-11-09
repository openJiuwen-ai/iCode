# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for :class:`KernelSubAgentPolicy`.

The controller is the heart of per-sub-agent pause/retry semantics, so
we exercise it against a stub ``Agent`` that lets each test drive the
exact failure sequence it needs (stall exhaustion, framework exception,
user retry, user abort, global cascade abort, concurrency).

The :class:`StreamRetryLoop` itself is covered in
``tests/foundation/test_retry.py`` — here we focus on controller-level
behaviour (pause transitions, event emission, decision routing).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import openai
import pytest

from chrys.foundation.errors import ROUTE_EXTENSION_KEY, ErrorKind, RouteFacts, failure_facts, origin_of
from chrys.foundation.errors.display import display_fields
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    InvocationAborted,
    InvocationCascadeAborted,
    InvocationPaused,
    InvocationResumed,
    InvocationRetryAttempt,
    InvocationToolCallResult,
)
from chrys.foundation.models.failures import FailureFacts
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.retry import StreamStall
from chrys.foundation.tool_invocation_order import TOOL_INVOCATION_ORDER_KEY
from chrys.foundation.tool_kinds import KIND_SHELL, KIND_SLEEP
from chrys.foundation.tool_result_metadata import (
    SHELL_EXIT_CODE_METADATA_KEY,
    TOOL_ERROR_KIND_METADATA_KEY,
    TOOL_ERROR_MESSAGE_METADATA_KEY,
    TOOL_ERRORED_METADATA_KEY,
    TOOL_FAILED_METADATA_KEY,
)
from chrys.foundation.trajectory.event_types import EventType, RetryMode, RetryReason
from chrys.foundation.trajectory.ids import is_valid_analytics_id, new_analytics_id
from chrys.foundation.trajectory.metadata import read_analytics_item_id
from chrys.kernel import (
    EXCLUDE_REASON_KEY,
    EXCLUDED_KEY,
    GROUP_ANNOTATION_KEY,
    SUMMARIZED_BY_SUMMARY_ID_KEY,
    AgentResponse,
    AgentResponseUpdate,
    AgentSession,
    Content,
    LoopRecorder,
    Message,
    ResponseStream,
)
from chrys.orchestration.invoker.contracts import SubAgentFailureReason, SubAgentStatus
from chrys.orchestration.invoker.resources import Conversation
from chrys.service.agent_middleware._metadata_keys import (
    _APPROVAL_REJECTED_KEY,
    _REJECTION_MESSAGE_KEY,
    _REJECTION_SOURCE_KEY,
)
from chrys.service.agent_middleware.control.sleep import SleepMiddleware
from chrys.service.agent_middleware.events.hook_dispatch import set_call_id
from chrys.service.agent_middleware.events.sub_agent_events import SubAgentEventMiddleware
from chrys.service.context.compaction import _REASON_COMPRESSION
from chrys.service.context.providers.history import PRE_OUTPUT_HISTORY_LEN_STATE_KEY
from chrys.service.llm.mock import MockChatClient
from chrys.service.session.message_metadata import TOOL_RESULT_METADATA_KEY
from chrys.service.trajectory.retries import RetryBackoffTrace
from tests.orchestration.sub_agents._controller_fixtures import (
    _collect,
    _controller_runtime,
    _make_controller,
    _ScriptedAgent,
    _StubResponse,
    kernel_shell,
)
from tests.service.trajectory._fakes import FakeSink, make_context
from tests.support.provider_errors import API_HOST, openai_status
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

# --- Stub agent so tests can script the run outcome ----------------------


def _input_texts(run_input: list[Any]) -> list[str]:
    """Plain text of each run-input item; the seed prompt arrives as a user Message."""
    return [item.text if isinstance(item, Message) else item for item in run_input]


def _history_messages(session: AgentSession) -> list[Message]:
    state = session.state.setdefault("chrys_history", {})
    return state.setdefault("messages", [])


def _append_completed_exchange(session: AgentSession, *, prompt: str = "do the thing") -> None:
    messages = _history_messages(session)
    messages.append(Message("user", [prompt]))
    messages.append(Message("assistant", [Content.from_function_call("c1", "read_file", arguments={})]))
    messages.append(Message("tool", [Content.from_function_result("c1", result="contents")]))


# --- Tests ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_runs_once_and_returns_text():
    bus = EventBus()
    agent = _ScriptedAgent(outcomes=["hello"])
    ctrl = _make_controller(agent, bus)

    result = await ctrl.run()

    assert result == "hello"
    assert ctrl.status == SubAgentStatus.COMPLETED
    assert len(agent.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("structured_output", "expected"),
    [
        (
            Content.from_uri("data:image/png;base64,QUJD", media_type="image/png"),
            "Sub-agent returned image output.",
        ),
        (
            Content.from_hosted_file("file-1", media_type="text/csv", name="report.csv"),
            "Sub-agent returned artifact output.",
        ),
    ],
)
async def test_hosted_structured_only_output_is_success(structured_output: Content, expected: str) -> None:
    call = Content.from_hosted_tool_call(
        "hosted-1",
        tool_name="code_interpreter",
        hosted_family="code",
        hosted_provider="openai",
    )
    result = Content.from_hosted_tool_result(
        "hosted-1",
        tool_name="code_interpreter",
        items=[structured_output],
        hosted_family="code",
        hosted_provider="openai",
        provider_phase="terminal",
        provider_status="completed",
    )
    response = _StubResponse(messages=[Message("assistant", [call, result])])
    controller = _make_controller(_ScriptedAgent(outcomes=[response]), EventBus())

    output = await controller.run()

    assert output == expected
    assert controller.status == SubAgentStatus.COMPLETED


@pytest.mark.asyncio
async def test_failed_hosted_structured_output_is_not_promoted_to_success() -> None:
    result = Content.from_image_generation_tool_result(
        image_id="image-1",
        outputs=[Content.from_uri("data:image/png;base64,QUJD", media_type="image/png")],
        hosted_provider="openai",
        provider_phase="terminal",
        provider_status="failed",
    )
    response = _StubResponse(messages=[Message("assistant", [result])])
    controller = _make_controller(_ScriptedAgent(outcomes=[response]), EventBus())

    output = await controller.run()

    assert output.startswith("Error: ")
    assert "returned no output" in output
    assert output != "Sub-agent returned image output."


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_status", ["generating", None])
async def test_terminal_hosted_image_output_overrides_stale_status(provider_status: str | None) -> None:
    result = Content.from_image_generation_tool_result(
        image_id="image-1",
        outputs=[Content.from_uri("data:image/png;base64,QUJD", media_type="image/png")],
        hosted_provider="openai",
        provider_phase="terminal",
        provider_status=provider_status,
    )
    response = _StubResponse(messages=[Message("assistant", [result])])
    controller = _make_controller(_ScriptedAgent(outcomes=[response]), EventBus())

    output = await controller.run()

    assert output == "Sub-agent returned image output."


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "options",
    [
        {},
        {"store": True},
        {"store": False},
        {"extra_body": {"store": True}},
        {"extra_body": {"store": False}},
        {"extra_body": {"store": None}},
        {"store": True, "extra_body": {"store": False}},
        {"store": False, "extra_body": {"store": True}},
        {"previous_response_id": "resp_1"},
        {"extra_body": {"conversation_id": "conv_1"}},
    ],
)
def test_forced_stateless_sub_agent_never_selects_service_storage(
    stream: bool,
    options: dict[str, Any],
) -> None:
    agent = _ScriptedAgent(outcomes=["unused"])
    agent.client = SimpleNamespace(STORES_BY_DEFAULT=True, FORCES_STATELESS=True)  # type: ignore[attr-defined]

    controller = _make_controller(
        agent,
        EventBus(),
        run_kwargs={"options": options},
        stream=stream,
    )

    assert controller.policy._service_storage is False


def test_forced_stateless_sub_agent_restore_site_receives_capability_flag() -> None:
    agent = _ScriptedAgent(outcomes=["unused"])
    agent.client = SimpleNamespace(STORES_BY_DEFAULT=True, FORCES_STATELESS=True)  # type: ignore[attr-defined]
    controller = _make_controller(
        agent,
        EventBus(),
        run_kwargs={
            "options": {
                "store": True,
                "continuation_token": {"response_id": "pending"},
                "background": True,
                "extra_body": {"conversation_id": "nested", "background": True},
            }
        },
    )

    assert controller.policy._service_storage is False
    controller.policy._session.service_session_id = "stale-child-session"
    controller.policy._attempts._restore_service_retry_inputs(controller.policy._run_kwargs)

    assert controller.policy._session.service_session_id is None
    assert controller.policy._run_kwargs["options"] == {"store": True, "extra_body": {}}


@pytest.mark.asyncio
async def test_transient_error_auto_retries_then_succeeds():
    """A retryable exception triggers auto-retry inside StreamRetryLoop;
    the controller stays in RUNNING and never pauses."""
    bus = EventBus()
    paused_events = await _collect(bus, InvocationPaused)
    retry_events = await _collect(bus, InvocationRetryAttempt)

    class TransientError(ConnectionError):
        """Retryable through the classifier's builtin transient layer."""

    agent = _ScriptedAgent(outcomes=[TransientError("blip"), "done"])
    ctrl = _make_controller(agent, bus, run_kwargs={"options": {"store": True}})
    result = await ctrl.run()

    assert result == "done"
    assert ctrl.status == SubAgentStatus.COMPLETED
    assert paused_events == []
    assert len(retry_events) == 1
    assert retry_events[0].origin.invocation_id == "inv-1"


@pytest.mark.asyncio
async def test_a_stall_retry_notice_names_the_stall_as_the_main_turn_does():
    bus = EventBus()
    retry_events = await _collect(bus, InvocationRetryAttempt)

    agent = _ScriptedAgent(outcomes=[StreamStall("no streaming updates received for 60s"), "done"])
    ctrl = _make_controller(agent, bus, run_kwargs={"options": {"store": True}})

    assert await ctrl.run() == "done"
    [retry] = retry_events
    assert retry.message == "Stream stalled"
    assert retry.display_message is not None
    assert (retry.display_message.definition.key, retry.display_hint) == ("retry.stream_stalled", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "expected_reason"),
    [
        (StreamStall("provider idle"), RetryReason.STREAM_STALL),
        (ConnectionError("reset"), RetryReason.TRANSIENT_ERROR),
    ],
)
async def test_service_retry_trajectory_preserves_failure_reason(
    exc: BaseException,
    expected_reason: str,
) -> None:
    sink = FakeSink()
    controller = kernel_shell(
        conversation=Conversation(),
        parent_origin=None,
        invocation_id="inv-1",
        tool_name="Explore",
        agent_name="Explore",
        agent=_ScriptedAgent(),
        **_controller_runtime(),
        prompt="do the thing",
        run_kwargs={},
        event_bus=None,
        session_id="s-1",
        trajectory_context=make_context(sink),
        trajectory_boundary_operation_id=new_analytics_id(),
    )

    await controller.policy._publish_service_retry_attempt("retry", 1, 3, 7, exc)

    scheduled = sink.only(EventType.RETRY_SCHEDULED)
    assert scheduled.payload["reason_code"] == expected_reason
    assert scheduled.payload["retry_mode"] == RetryMode.RUN


@pytest.mark.asyncio
async def test_interrupted_service_retry_sleep_keeps_retry_scheduled() -> None:
    sink = FakeSink()
    trace = RetryBackoffTrace.open(
        context=make_context(sink),
        parent_operation_id=new_analytics_id(),
        retry_mode=RetryMode.RUN,
    )
    assert trace is not None
    await trace.scheduled(reason_code=RetryReason.TRANSIENT_ERROR, delay_seconds=7)
    controller = _make_controller(_ScriptedAgent(), None)
    controller.policy._service_retry_trace = trace

    with patch.object(controller.policy, "_interruptible_sleep", new=AsyncMock(return_value=True)):
        assert await controller.policy._sleep_for_service_retry(7) is True

    assert sink.event_types == [EventType.RETRY_SCHEDULED]


@pytest.mark.asyncio
@pytest.mark.parametrize("decides", [True, False], ids=["pauses", "ends"])
async def test_a_failed_child_reports_the_verdict_on_its_retry_pause_and_end(tmp_path: Path, *, decides: bool):
    """A provider's failure carries the classifier's verdict on every event a frontend reports it by."""
    bus = EventBus()
    retries = await _collect(bus, InvocationRetryAttempt)
    paused = await _collect(bus, InvocationPaused)
    aborted = await _collect(bus, InvocationAborted)
    limited = await openai_status(429, {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}})
    assert isinstance(limited, openai.APIStatusError)
    # Every model request carries its route, so the model service answered.
    route = RouteFacts(origin_of(f"https://{API_HOST}"), None, first_hop_reached=True)
    limited.response.request.extensions[ROUTE_EXTENSION_KEY] = route
    ctrl = kernel_shell(
        conversation=Conversation(),
        parent_origin=None,
        invocation_id="inv-1",
        tool_name="Explore",
        agent_name="Explore",
        agent=_ScriptedAgent(outcomes=[limited, limited]),
        **_controller_runtime(),
        prompt="do the thing",
        # Service-side storage: the policy's whole-run lane retries the 429 itself.
        run_kwargs={"options": {"store": True}},
        event_bus=bus,
        session_id="s-1",
        max_retries=1,
        backoff_schedule=(0,),
        persist_dir=tmp_path,
        human_failure_decisions=decides,
    )

    run = asyncio.create_task(ctrl.run())
    if decides:
        await wait_for(lambda: paused or run.done(), description="the child paused")
        assert ctrl.request_abort() is True
    await asyncio.wait_for(run, ENGINE_TURN_TIMEOUT)

    facts = failure_facts(limited)
    display, hint = display_fields(limited)
    assert facts == FailureFacts(ErrorKind.RATE_LIMITED, retryable=True, status_code=429)
    assert display is not None
    assert [event.facts for event in retries] == [facts]
    assert [event.last_error_facts for event in paused] == ([facts] if decides else [])
    assert [(event.last_error_facts, event.last_error_display, event.last_error_hint) for event in aborted] == [
        (facts, display, hint)
    ]


@pytest.mark.asyncio
async def test_a_failure_ends_the_child_where_no_card_could_pause_it(tmp_path: Path):
    """Without a decision surface the failed child ends at once: no pause, no control record, its error
    as the tool result and one aborted event."""
    bus = EventBus()
    paused_events = await _collect(bus, InvocationPaused)
    aborted_events = await _collect(bus, InvocationAborted)
    queued: list[Path] = []
    agent = _ScriptedAgent(outcomes=[RuntimeError("401 auth failed")])
    ctrl = kernel_shell(
        conversation=Conversation(),
        parent_origin=None,
        invocation_id="inv-1",
        tool_name="Explore",
        agent_name="Explore",
        agent=agent,
        **_controller_runtime(),
        prompt="do the thing",
        run_kwargs={},
        event_bus=bus,
        session_id="s-1",
        max_retries=0,
        backoff_schedule=(0,),
        persist_dir=tmp_path,
        pending_record_finalizer=queued.append,
        human_failure_decisions=False,
    )

    result = await ctrl.run()

    assert result.startswith("Error: sub-agent 'Explore' failed — ")
    assert "401 auth failed" in result
    assert ctrl.status == SubAgentStatus.ABORTED
    assert ctrl.policy.failure_reason == SubAgentFailureReason.FRAMEWORK_EXC
    assert len(agent.calls) == 1
    assert paused_events == []
    assert [event.last_error for event in aborted_events] == [ctrl.policy.last_error]
    assert ctrl.request_retry() is False
    # The queued path is the record a pause would have written; nothing paused, so none exists.
    assert [path.parent for path in queued] == [tmp_path]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_framework_exception_pauses_immediately():
    """Non-retryable exception → pause on first failure."""
    bus = EventBus()
    paused_events = await _collect(bus, InvocationPaused)

    agent = _ScriptedAgent(outcomes=[RuntimeError("401 auth failed")])
    ctrl = _make_controller(agent, bus)

    run_task = asyncio.create_task(ctrl.run())

    # Wait until the controller is paused.
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)
    assert ctrl.policy.failure_reason == SubAgentFailureReason.FRAMEWORK_EXC
    assert "401 auth failed" in ctrl.policy.last_error
    assert len(paused_events) == 1
    assert paused_events[0].reason == "framework_exc"

    # Abort it so the task can exit.
    assert ctrl.request_abort() is True
    result = await run_task
    assert result.startswith("Error: sub-agent 'Explore' aborted by user")
    assert ctrl.status == SubAgentStatus.ABORTED


@pytest.mark.asyncio
async def test_stream_stall_pause_message_uses_readable_cause():
    """Streaming stalls should not leak placeholder exception args into the pause banner."""
    bus = EventBus()
    paused_events = await _collect(bus, InvocationPaused)

    class _NeverYieldStream:
        def __init__(self) -> None:
            self.cleanup_calls = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(10)
            raise AssertionError("unreachable after timeout")

        async def get_final_response(self):
            raise AssertionError("final response should not be reached")

        async def aclose(self) -> None:
            self.cleanup_calls += 1

    class _StreamingAgent:
        client = MockChatClient()

        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []
            self.streams: list[_NeverYieldStream] = []

        def run(self, _prompt, **kwargs):
            self.calls.append(dict(kwargs))
            stream = _NeverYieldStream()
            self.streams.append(stream)
            return stream

    agent = _StreamingAgent()
    ctrl = kernel_shell(
        conversation=Conversation(),
        parent_origin=None,
        invocation_id="inv-1",
        tool_name="Explore",
        agent_name="Explore",
        agent=agent,
        **_controller_runtime(),
        prompt="do the thing",
        run_kwargs={"options": {"store": True}},
        event_bus=bus,
        session_id="s-1",
        max_retries=1,
        backoff_schedule=(0,),
        stream=True,
        stream_attempt_timeout=0.01,
    )

    run_task = asyncio.create_task(ctrl.run())
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)

    assert ctrl.policy.failure_reason == SubAgentFailureReason.STREAM_STALL
    assert ctrl.policy.last_error == "Stream stalled after 1 retries: no streaming updates received for 0.01s"
    assert ": 0" not in ctrl.policy.last_error
    assert len(paused_events) == 1
    assert paused_events[0].last_error == ctrl.policy.last_error
    assert len(agent.calls) == 2
    assert all(call.get("stream") is True for call in agent.calls)
    assert [stream.cleanup_calls for stream in agent.streams] == [1, 1]

    assert ctrl.request_abort() is True
    result = await run_task
    assert result.startswith("Error: sub-agent 'Explore' aborted by user")
    assert ctrl.status == SubAgentStatus.ABORTED


@pytest.mark.asyncio
async def test_informational_function_call_keeps_sub_agent_watchdog_armed():
    """A hosted call has no local result await, so an idle next chunk must still stall."""
    bus = EventBus()

    class _InformationalThenIdleStream:
        def __init__(self) -> None:
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.sent:
                self.sent = True
                return SimpleNamespace(
                    contents=[
                        Content.from_function_call(
                            "hosted-1",
                            "web_search",
                            arguments={"query": "chrys"},
                            informational_only=True,
                        )
                    ]
                )
            await asyncio.sleep(30)
            raise AssertionError("unreachable after timeout")

        async def get_final_response(self):
            raise AssertionError("final response should not be reached")

        async def aclose(self) -> None:
            return None

    class _StreamingAgent:
        client = MockChatClient()

        def run(self, _prompt, **_kwargs):
            return _InformationalThenIdleStream()

    ctrl = kernel_shell(
        conversation=Conversation(),
        parent_origin=None,
        invocation_id="inv-1",
        tool_name="Explore",
        agent_name="Explore",
        agent=_StreamingAgent(),
        **_controller_runtime(),
        prompt="do the thing",
        run_kwargs={"options": {"store": True}},
        event_bus=bus,
        session_id="s-1",
        max_retries=1,
        backoff_schedule=(0,),
        stream=True,
        stream_attempt_timeout=0.01,
    )

    run_task = asyncio.create_task(ctrl.run())
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)

    assert ctrl.policy.failure_reason == SubAgentFailureReason.STREAM_STALL
    assert ctrl.request_abort() is True
    await run_task


@pytest.mark.asyncio
async def test_retry_from_pause_runs_again_and_succeeds():
    """After pause, user-triggered Retry restarts agent.run() from the prompt."""
    bus = EventBus()
    resumed_events = await _collect(bus, InvocationResumed)

    agent = _ScriptedAgent(outcomes=[RuntimeError("fail once"), "recovered"])
    ctrl = _make_controller(agent, bus)

    run_task = asyncio.create_task(ctrl.run())
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)
    assert ctrl.request_retry() is True

    result = await run_task
    assert result == "recovered"
    assert ctrl.status == SubAgentStatus.COMPLETED
    assert len(resumed_events) == 1
    assert len(agent.calls) == 2  # original + retry


@pytest.mark.asyncio
async def test_pass_start_hooks_fire_once_per_pass():
    """Each pass — the initial run and every user Retry after a pause — fires
    the pass-start hooks exactly once, so state carried across a pass's
    whole-run retry attempts (the validation middleware's budget) cannot leak
    from an aborted pass into the next one."""
    bus = EventBus()
    calls: list[int] = []

    agent = _ScriptedAgent(outcomes=[RuntimeError("fail once"), "recovered"])
    ctrl = _make_controller(agent, bus, pass_start_hooks=(lambda: calls.append(1),))

    run_task = asyncio.create_task(ctrl.run())
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)
    assert len(calls) == 1
    assert ctrl.request_retry() is True

    result = await run_task
    assert result == "recovered"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_raising_pass_start_hook_pauses_instead_of_escaping():
    """A raising hook takes the normal framework-exception pause path — no
    exception may escape the run loop after ``InvocationResumed`` was already
    published for a retry pass."""
    bus = EventBus()

    def _bad_hook() -> None:
        raise RuntimeError("hook exploded")

    agent = _ScriptedAgent(outcomes=["unused"])
    ctrl = _make_controller(agent, bus, pass_start_hooks=(_bad_hook,))

    run_task = asyncio.create_task(ctrl.run())
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)

    assert agent.calls == []
    assert "hook exploded" in ctrl.policy.last_error
    assert ctrl.request_abort() is True
    await run_task


@pytest.mark.asyncio
async def test_retry_from_pause_continues_after_completed_tool_work():
    """After completed inner tool work, user Retry continues with empty input."""
    bus = EventBus()
    session = AgentSession()

    def _fail_after_completed_work(_prompt, kwargs):
        assert kwargs["session"] is session
        _append_completed_exchange(session)
        _history_messages(session).append(
            Message("assistant", [Content.from_function_call("dangling", "read_file", arguments={})])
        )
        return RuntimeError("failed after useful work")

    def _succeed_from_history(prompt, kwargs):
        assert kwargs["session"] is session
        assert prompt == []
        _history_messages(session).append(Message("assistant", ["recovered"]))
        return "recovered"

    agent = _ScriptedAgent(outcomes=[_fail_after_completed_work, _succeed_from_history])
    ctrl = _make_controller(agent, bus, session=session)

    run_task = asyncio.create_task(ctrl.run())
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)
    assert ctrl.request_retry() is True

    result = await run_task

    assert result == "recovered"
    assert ctrl.status == SubAgentStatus.COMPLETED
    assert _input_texts(agent.calls[0]) == ["do the thing"]
    assert len(agent.calls) == 2
    assert agent.calls[1] == []

    messages = _history_messages(session)
    call_ids = [c.call_id for m in messages for c in m.contents if c.type == "function_call"]
    assert call_ids == ["c1"]
    assert [m.text for m in messages if m.role == "user"] == ["do the thing"]


@pytest.mark.asyncio
async def test_retry_from_pause_replays_prompt_when_no_work_completed():
    """If a sub-agent fails before completed work, Retry removes the orphan prompt and replays it."""
    bus = EventBus()
    session = AgentSession()

    def _fail_after_prompt(_prompt, kwargs):
        assert kwargs["session"] is session
        _history_messages(session).append(Message("user", ["do the thing"]))
        return RuntimeError("failed before any work")

    def _succeed_after_replay(prompt, kwargs):
        assert kwargs["session"] is session
        assert _input_texts(prompt) == ["do the thing"]
        _history_messages(session).append(prompt[0])
        _history_messages(session).append(Message("assistant", ["replayed"]))
        return "replayed"

    agent = _ScriptedAgent(outcomes=[_fail_after_prompt, _succeed_after_replay])
    ctrl = _make_controller(agent, bus, session=session)

    run_task = asyncio.create_task(ctrl.run())
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)
    assert ctrl.request_retry() is True

    result = await run_task

    assert result == "replayed"
    assert [_input_texts(call) for call in agent.calls] == [["do the thing"], ["do the thing"]]
    assert [m.text for m in _history_messages(session) if m.role == "user"] == ["do the thing"]
    # The replayed prompt is the same persisted item: its analytics identity
    # survives the retry instead of minting a second anonymous user message.
    seed_ids = {read_analytics_item_id(call[0].additional_properties) for call in agent.calls}
    assert len(seed_ids) == 1 and None not in seed_ids


@pytest.mark.asyncio
async def test_seed_prompt_carries_a_stable_analytics_item_id():
    """The sub-agent's first user message is a persisted context item with analytics identity."""
    bus = EventBus()
    session = AgentSession()
    agent = _ScriptedAgent(outcomes=["done"])
    ctrl = _make_controller(agent, bus, session=session)

    assert await ctrl.run() == "done"

    (call,) = agent.calls
    (seed,) = call
    assert isinstance(seed, Message)
    assert seed.role == "user"
    assert seed.text == "do the thing"
    item_id = read_analytics_item_id(seed.additional_properties)
    assert item_id is not None
    assert is_valid_analytics_id(item_id)


def test_snapshot_restore_rolls_back_sub_agent_history_and_compressions():
    """Sub-agent transient retry rollback mirrors the main executor's history rollback."""
    bus = EventBus()
    session = AgentSession()
    ctrl = _make_controller(_ScriptedAgent(outcomes=[]), bus, session=session)
    base = Message("assistant", ["base"])
    state = session.state.setdefault("chrys_history", {})
    state["messages"] = [base]
    state["compressed_msgs"] = ["base-block"]

    snapshot = ctrl.policy._rollback.snapshot()

    base.additional_properties[EXCLUDED_KEY] = True
    base.additional_properties[EXCLUDE_REASON_KEY] = _REASON_COMPRESSION
    state["messages"].append(Message("assistant", ["rolled back"]))
    state["compressed_msgs"].append("rolled-back-block")
    state[PRE_OUTPUT_HISTORY_LEN_STATE_KEY] = 99
    session.service_session_id = "resp_rolled_back"

    ctrl.policy._rollback.restore(snapshot)

    assert state["messages"] == [base]
    assert state["compressed_msgs"] == ["base-block"]
    assert session.service_session_id is None
    assert PRE_OUTPUT_HISTORY_LEN_STATE_KEY not in state
    # Exact restore: marks set during the rolled-back attempt vanish entirely
    # (the pre-attempt additional_properties had neither key).
    assert EXCLUDED_KEY not in base.additional_properties
    assert EXCLUDE_REASON_KEY not in base.additional_properties


def test_snapshot_restore_rolls_back_compaction_anchor_state():
    """Exclusion anchors ride the sub-agent retry snapshot with history.

    A Phase-4 commit inside the rolled-back attempt records anchors for
    messages the rollback then removes; without restoring the anchor list
    the after_run persist would bind the successful retry's structural
    twins and silently exclude live messages.
    """
    bus = EventBus()
    session = AgentSession()
    anchors_snapshot = ("pre-attempt-anchor",)
    restored_anchor_snapshots: list[tuple[str, ...]] = []
    strategy = SimpleNamespace(
        snapshot_retry_state=lambda: anchors_snapshot,
        restore_retry_state=restored_anchor_snapshots.append,
    )
    ctrl = _make_controller(
        _ScriptedAgent(outcomes=[]),
        bus,
        session=session,
        run_kwargs={"compaction_strategy": strategy},
    )
    state = session.state.setdefault("chrys_history", {})
    state["messages"] = []
    state["compressed_msgs"] = []

    snapshot = ctrl.policy._rollback.snapshot()
    ctrl.policy._rollback.restore(snapshot)

    assert restored_anchor_snapshots == [anchors_snapshot]


def test_snapshot_restore_preserves_pre_existing_marks_and_reverts_annotations():
    """Exact restore keeps legitimate pre-attempt marks and undoes annotation edits.

    The reason-based clearing this replaced could only clear; it would have
    wiped marks persisted by earlier runs and left a rolled-back attempt's
    ``_group`` summarized-by marker behind (orphan summary re-injection bait
    for ``UnifiedContextStrategy._reinject_cached_summaries``).
    """
    bus = EventBus()
    session = AgentSession()
    ctrl = _make_controller(_ScriptedAgent(outcomes=[]), bus, session=session)
    kept = Message("assistant", ["legitimately excluded earlier"])
    kept.additional_properties[EXCLUDED_KEY] = True
    kept.additional_properties[EXCLUDE_REASON_KEY] = _REASON_COMPRESSION
    grouped = Message("assistant", ["grouped"])
    grouped.additional_properties[GROUP_ANNOTATION_KEY] = {"group_id": "g1"}
    state = session.state.setdefault("chrys_history", {})
    state["messages"] = [kept, grouped]
    state["compressed_msgs"] = []

    snapshot = ctrl.policy._rollback.snapshot()

    # Failed attempt: automatic compaction excludes `grouped` and stamps the
    # summarized-by marker into the (snapshot-shared) _group annotation.
    grouped.additional_properties[EXCLUDED_KEY] = True
    grouped.additional_properties[EXCLUDE_REASON_KEY] = "budget_tool_compaction"
    grouped.additional_properties[GROUP_ANNOTATION_KEY][SUMMARIZED_BY_SUMMARY_ID_KEY] = "summary-1"

    ctrl.policy._rollback.restore(snapshot)

    assert kept.additional_properties[EXCLUDED_KEY] is True
    assert kept.additional_properties[EXCLUDE_REASON_KEY] == _REASON_COMPRESSION
    assert EXCLUDED_KEY not in grouped.additional_properties
    assert grouped.additional_properties[GROUP_ANNOTATION_KEY] == {"group_id": "g1"}

    # Copy-on-restore: a second rollback from the same snapshot still works.
    grouped.additional_properties[EXCLUDED_KEY] = True
    ctrl.policy._rollback.restore(snapshot)
    assert EXCLUDED_KEY not in grouped.additional_properties
    assert kept.additional_properties[EXCLUDED_KEY] is True


def test_snapshot_restore_preserves_existing_sub_agent_pre_output_floor():
    bus = EventBus()
    session = AgentSession()
    ctrl = _make_controller(_ScriptedAgent(outcomes=[]), bus, session=session)
    state = session.state.setdefault("chrys_history", {})
    state["messages"] = [Message("assistant", ["base"])]
    state["compressed_msgs"] = []
    state[PRE_OUTPUT_HISTORY_LEN_STATE_KEY] = 1

    snapshot = ctrl.policy._rollback.snapshot()
    state[PRE_OUTPUT_HISTORY_LEN_STATE_KEY] = 99

    ctrl.policy._rollback.restore(snapshot)

    assert state[PRE_OUTPUT_HISTORY_LEN_STATE_KEY] == 1


def test_repair_history_keeps_legacy_nudge_without_writing_a_new_one():
    """A persisted legacy nudge stays readable while an empty resume adds no user."""
    bus = EventBus()
    session = AgentSession()
    loop_recorder = LoopRecorder()
    ctrl = _make_controller(
        _ScriptedAgent(outcomes=[]),
        bus,
        session=session,
        loop_recorder=loop_recorder,
        prompt="continue",
    )

    prompt_msg = Message("user", ["continue"])
    first_call = Message("assistant", [Content.from_function_call("sleep-1", "sleep", arguments={})])
    first_result = Message("tool", [Content.from_function_result("sleep-1", result="slept once")])
    messages = _history_messages(session)
    messages.extend([prompt_msg, first_call, first_result])

    legacy_nudge = Message("user", ["continue"])
    legacy_nudge.additional_properties[HistoryMarkerKind.CONTINUATION_KEY] = True
    messages.append(legacy_nudge)
    second_call = Message("assistant", [Content.from_function_call("sleep-2", "sleep", arguments={})])
    second_result = Message("tool", [Content.from_function_result("sleep-2", result="slept twice")])
    ctrl.policy._pass_start_index = len(messages)
    loop_recorder._initial_count = 0
    loop_recorder._captured = [second_call, second_result]
    ctrl.policy._active_run_input = []

    ctrl.policy._repair_paused_history()

    assert messages == [prompt_msg, first_call, first_result, legacy_nudge, second_call, second_result]
    assert [
        message for message in messages if message.additional_properties.get(HistoryMarkerKind.CONTINUATION_KEY)
    ] == [legacy_nudge]
    result_call_ids = [
        content.call_id for message in messages for content in message.contents if content.type == "function_result"
    ]
    assert result_call_ids == ["sleep-1", "sleep-2"]


def test_two_pause_repairs_keep_each_resumed_exchange_after_the_previous_pass() -> None:
    bus = EventBus()
    session = AgentSession()
    recorder = LoopRecorder()
    ctrl = _make_controller(
        _ScriptedAgent(),
        bus,
        session=session,
        loop_recorder=recorder,
    )
    prompt = Message("user", ["do the thing"])
    messages = _history_messages(session)
    messages.append(prompt)

    first_call = Message("assistant", [Content.from_function_call("first", "read_file", arguments={})])
    first_result = Message("tool", [Content.from_function_result("first", result="one")])
    ctrl.policy._pass_start_index = len(messages)
    recorder._initial_count = 0
    recorder._captured = [first_call, first_result]
    ctrl.policy._active_run_input = []
    ctrl.policy._repair_paused_history()

    legacy_nudge = Message("user", ["continue"])
    legacy_nudge.additional_properties[HistoryMarkerKind.CONTINUATION_KEY] = True
    messages.append(legacy_nudge)

    second_call = Message("assistant", [Content.from_function_call("second", "read_file", arguments={})])
    second_result = Message("tool", [Content.from_function_result("second", result="two")])
    recorder.reset()
    ctrl.policy._pass_start_index = len(messages)
    recorder._initial_count = 0
    recorder._captured = [second_call, second_result]
    ctrl.policy._active_run_input = []
    ctrl.policy._repair_paused_history()

    assert messages == [prompt, first_call, first_result, legacy_nudge, second_call, second_result]
    assert sum(bool(message.additional_properties.get(HistoryMarkerKind.CONTINUATION_KEY)) for message in messages) == 1


def test_repair_history_clears_service_session_id_on_pause():
    """Pause-time repair must drop service_session_id so OpenAI Responses
    profiles with ``store: true`` don't have ``CompressibleHistoryProvider``
    skip the locally-merged history on the next user Retry."""
    bus = EventBus()
    session = AgentSession()
    ctrl = _make_controller(_ScriptedAgent(outcomes=[]), bus, session=session)
    _history_messages(session).append(Message("user", ["do the thing"]))
    session.service_session_id = "resp_partial_attempt_1"

    ctrl.policy._repair_paused_history()

    assert session.service_session_id is None


@pytest.mark.asyncio
async def test_abort_from_pause_returns_error_string_to_parent():
    """User Abort delivers an Error: string to the parent tool call."""
    bus = EventBus()

    agent = _ScriptedAgent(outcomes=[RuntimeError("nope")])
    ctrl = _make_controller(agent, bus)

    run_task = asyncio.create_task(ctrl.run())
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)
    ctrl.request_abort()

    result = await run_task
    assert result.startswith("Error: sub-agent 'Explore' aborted by user")
    assert "nope" in result
    assert ctrl.status == SubAgentStatus.ABORTED


@pytest.mark.asyncio
async def test_function_result_rearms_service_sub_agent_watchdog():
    async def _updates():
        yield AgentResponseUpdate(
            contents=[Content.from_function_call("c1", "echo", arguments={})],
            role="assistant",
        )
        yield AgentResponseUpdate(
            contents=[Content.from_function_result("c1", result="done")],
            role="tool",
        )
        await asyncio.sleep(30)

    class _StreamingAgent:
        client = MockChatClient()

        def run(self, _prompt, **_kwargs):
            return ResponseStream(_updates(), finalizer=AgentResponse.from_updates)

    ctrl = kernel_shell(
        conversation=Conversation(),
        parent_origin=None,
        invocation_id="inv-1",
        tool_name="Explore",
        agent_name="Explore",
        agent=_StreamingAgent(),
        **_controller_runtime(),
        prompt="do the thing",
        run_kwargs={"options": {"store": True}},
        event_bus=EventBus(),
        session_id="s-1",
        stream=True,
        stream_attempt_timeout=0.01,
    )

    with pytest.raises(StreamStall):
        await asyncio.wait_for(
            ctrl.policy._attempts._stream_single_attempt(ctrl.policy._active_run_input, ctrl.policy._run_kwargs),
            timeout=5.0,
        )


@pytest.mark.asyncio
async def test_local_wire_stall_raises_directly_into_pause_flow():
    bus = EventBus()
    paused_events = await _collect(bus, InvocationPaused)
    agent = _ScriptedAgent(outcomes=[StreamStall("provider idle")])
    ctrl = _make_controller(agent, bus, run_kwargs={"options": {"store": False}})

    run_task = asyncio.create_task(ctrl.run())
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)

    assert ctrl.policy.failure_reason == SubAgentFailureReason.STREAM_STALL
    assert ctrl.policy.last_error == "provider idle"
    assert len(paused_events) == 1
    assert len(agent.calls) == 1

    assert ctrl.request_abort() is True
    result = await run_task
    assert result.startswith("Error: sub-agent 'Explore' aborted by user")


@pytest.mark.asyncio
async def test_abort_publishes_sub_agent_aborted_event():
    """User abort should emit a ``InvocationAborted`` event so the engine can
    decrement its paused-invocation set and the TUI card can reflect the
    terminal state."""
    bus = EventBus()
    aborted_events = await _collect(bus, InvocationAborted)

    agent = _ScriptedAgent(outcomes=[RuntimeError("bad creds")])
    ctrl = _make_controller(agent, bus)

    run_task = asyncio.create_task(ctrl.run())
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)
    ctrl.request_abort()
    await run_task

    assert len(aborted_events) == 1
    ev = aborted_events[0]
    assert ev.origin.invocation_id == "inv-1"
    assert ev.agent_name == "Explore"
    assert "bad creds" in ev.last_error


@pytest.mark.asyncio
async def test_cascade_abort_from_pause_raises_cancelled_error():
    """Global interrupt: cascade_abort resolves the pending decision and
    the controller raises CancelledError so the parent cleans up."""
    bus = EventBus()
    cascade_events = await _collect(bus, InvocationCascadeAborted)

    agent = _ScriptedAgent(outcomes=[RuntimeError("framework boom")])
    ctrl = _make_controller(agent, bus)

    run_task = asyncio.create_task(ctrl.run())
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)
    await ctrl.cascade_abort()

    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert ctrl.status == SubAgentStatus.CASCADE_ABORTED
    assert len(cascade_events) == 1
    assert cascade_events[0].origin.invocation_id == "inv-1"


@pytest.mark.asyncio
async def test_paused_cascade_commits_parent_before_merging_history_and_writing_terminal_log() -> None:
    bus = EventBus()
    session = AgentSession()
    recorder = LoopRecorder()
    order: list[str] = []

    def commit_inner_exchange(_prompt: object, _kwargs: object) -> RuntimeError:
        call = Content.from_function_call("inner-call", "read_file", arguments={})
        call.additional_properties[TOOL_INVOCATION_ORDER_KEY] = 0
        commit = recorder.stage_exchange([Message("assistant", [call])], [call], result_carrier_item_id="a" * 32)[0]
        commit.commit_final(Content.from_function_result("inner-call", result="inner-result"))
        return RuntimeError("pause after committed inner work")

    agent = _ScriptedAgent(outcomes=[commit_inner_exchange])
    ctrl = _make_controller(
        agent,
        bus,
        session=session,
        loop_recorder=recorder,
        max_retries=0,
        parent_interrupted_result_commit=lambda: order.append("parent-committed"),
    )

    async def write_log(*, status: str, **_fields: object) -> None:
        order.append(f"log:{status}")

    ctrl.policy._write_log = write_log  # type: ignore[method-assign]
    run_task = asyncio.create_task(ctrl.run())
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)

    await ctrl.cascade_abort()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    messages = _history_messages(session)
    assert [
        content.call_id for message in messages for content in message.contents if content.type == "function_result"
    ] == ["inner-call"]
    assert order.count("parent-committed") == 1
    assert order.index("parent-committed") < order.index("log:cascade_aborted")


@pytest.mark.asyncio
async def test_committed_inner_work_disables_automatic_sub_agent_retry() -> None:
    bus = EventBus()
    recorder = LoopRecorder()

    class TransientError(ConnectionError):
        """Retryable through the classifier's builtin transient layer."""

    def commit_then_fail(_prompt: object, _kwargs: object) -> TransientError:
        call = Content.from_function_call("inner-call", "write_file", arguments={})
        call.additional_properties[TOOL_INVOCATION_ORDER_KEY] = 0
        commit = recorder.stage_exchange([Message("assistant", [call])], [call], result_carrier_item_id="a" * 32)[0]
        commit.commit_final(Content.from_function_result("inner-call", result="written"))
        return TransientError("provider failed after commit")

    agent = _ScriptedAgent(outcomes=[commit_then_fail, "must not auto-retry"])
    ctrl = _make_controller(agent, bus, loop_recorder=recorder)
    run_task = asyncio.create_task(ctrl.run())
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)

    assert len(agent.calls) == 1
    assert recorder.committed_count == 1
    ctrl.request_abort()
    await run_task


@pytest.mark.asyncio
async def test_committed_inner_work_vetoes_stored_mode_retry_at_non_default_budget() -> None:
    """The commit gate wins over a positive injected budget inside the automatic retry loop.

    Stored mode is what routes failures through the L0 retry loop at all;
    the retryable error would let the loop replay the run were the gate
    not consulted."""
    bus = EventBus()
    retry_events = await _collect(bus, InvocationRetryAttempt)
    recorder = LoopRecorder()

    class TransientError(ConnectionError):
        """Retryable through the classifier's builtin transient layer."""

    def commit_then_fail(_prompt: object, _kwargs: object) -> TransientError:
        call = Content.from_function_call("inner-call", "write_file", arguments={})
        call.additional_properties[TOOL_INVOCATION_ORDER_KEY] = 0
        commit = recorder.stage_exchange([Message("assistant", [call])], [call], result_carrier_item_id="a" * 32)[0]
        commit.commit_final(Content.from_function_result("inner-call", result="written"))
        return TransientError("provider failed after commit")

    agent = _ScriptedAgent(outcomes=[commit_then_fail, "must not auto-retry"])
    ctrl = _make_controller(
        agent,
        bus,
        loop_recorder=recorder,
        max_retries=3,
        run_kwargs={"options": {"store": True}},
    )
    assert ctrl.policy._service_storage is True
    run_task = asyncio.create_task(ctrl.run())
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)

    assert len(agent.calls) == 1
    assert recorder.committed_count == 1
    assert retry_events == []
    ctrl.request_abort()
    await run_task


def test_observed_hosted_commits_disable_automatic_sub_agent_retry() -> None:
    # Hosted calls run inside the failed exchange and never reach the loop
    # recorder: only the run-scoped probe can veto their replay.
    ctrl = _make_controller(_ScriptedAgent(), None)
    assert ctrl.policy._attempts._may_retry_attempt(ctrl.policy._run_kwargs) is True
    ctrl = _make_controller(
        _ScriptedAgent(),
        None,
        hosted_commits_probe=lambda: ("shell",),
    )

    assert ctrl.policy._attempts._may_retry_attempt(ctrl.policy._run_kwargs) is False
    # A live continuation token resumes the background response instead of
    # re-creating it, so nothing hosted runs twice.
    live = {"options": {"continuation_token": {"response_id": "pending"}}}
    assert ctrl.policy._attempts._may_retry_attempt(live) is True


@pytest.mark.asyncio
async def test_cascade_abort_mid_run_cancels_live_task_and_publishes_event():
    """User-initiated cascade_abort() on a running controller should:

    1. Cancel the live ``agent.run()`` task directly (so long-running
       sub-agents stop immediately without waiting for parent task
       cancellation to propagate — the parent has no enclosing task in
       the streaming executor path).
    2. Publish :class:`InvocationCascadeAborted` (because the cancel WAS
       user-driven via ``_cascade_requested``)."""
    bus = EventBus()
    cascade_events = await _collect(bus, InvocationCascadeAborted)

    started = asyncio.Event()

    async def _long_run(prompt, **kwargs):
        started.set()
        await asyncio.sleep(10)
        return _StubResponse(text="never")

    agent = _ScriptedAgent()
    agent.run = _long_run  # type: ignore[assignment]

    ctrl = _make_controller(agent, bus)
    run_task = asyncio.create_task(ctrl.run())

    # Wait for the agent.run() to actually be awaiting inside _attempt.
    await started.wait()
    # Cascade abort must tear the task down directly — not rely on
    # parent task cancel (which only exists in the blocking executor path).
    await ctrl.cascade_abort()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert ctrl.status == SubAgentStatus.CASCADE_ABORTED
    assert len(cascade_events) == 1


@pytest.mark.asyncio
async def test_cascade_abort_mid_sleep_publishes_interrupted_inner_tool_result():
    """Cascade abort lets an active inner sleep publish its interrupted result before task cancel."""
    bus = EventBus()
    sleep_mw = SleepMiddleware(bus, session_id="s-1")
    sub_events = SubAgentEventMiddleware(
        bus, "Explore", "inv-1", session_id="s-1", origin=InvocationOrigin("sub_agent", "s-1", "inv-1", None)
    )
    results = await _collect(bus, InvocationToolCallResult)
    ctx = SimpleNamespace(
        function=SimpleNamespace(name="sleep", chrys_kind=KIND_SLEEP),
        arguments={"seconds": 30, "reason": "test wait"},
        result=None,
        metadata={},
    )
    set_call_id(ctx, "sleep-call")

    async def _next() -> None:
        async def _underlying() -> None:
            raise AssertionError("sleep middleware should not call the underlying tool")

        await sleep_mw.process(ctx, _underlying)

    task = asyncio.create_task(sub_events.process(ctx, _next))
    await asyncio.sleep(0)
    assert sleep_mw.active_call_ids == ("sleep-call",)

    ctrl = _make_controller(_ScriptedAgent(outcomes=["never"]), bus, sleep_middleware=sleep_mw)
    ctrl.policy._attempt_handle.task = task
    await ctrl.cascade_abort()
    await asyncio.wait_for(task, timeout=5.0)

    assert results[-1].call_id == "sleep-call"
    assert results[-1].result == "Sleep interrupted after 0 seconds (requested 30 seconds)."
    assert results[-1].metadata["sleep_interrupted"] is True


@pytest.mark.asyncio
async def test_sub_agent_event_middleware_publishes_shell_result_metadata():
    """Inner shell tools should expose structured shell metadata to the parent card."""
    bus = EventBus()
    sub_events = SubAgentEventMiddleware(
        bus, "Explore", "inv-1", session_id="s-1", origin=InvocationOrigin("sub_agent", "s-1", "inv-1", None)
    )
    results = await _collect(bus, InvocationToolCallResult)
    ctx = SimpleNamespace(
        function=SimpleNamespace(name="bash", chrys_kind=KIND_SHELL),
        arguments={"command": "false"},
        result=None,
        metadata={"call_id": "provider-shell-call"},
    )
    set_call_id(ctx, "shell-call")

    async def _next() -> None:
        from chrys.service.tools.builtins.shell import shell_result_metadata

        metadata = shell_result_metadata.get()
        assert metadata is not None
        metadata[SHELL_EXIT_CODE_METADATA_KEY] = 1
        ctx.result = "boom\n[exit_code: 1]"

    await sub_events.process(ctx, _next)

    assert results[-1].call_id == "shell-call"
    assert results[-1].metadata[SHELL_EXIT_CODE_METADATA_KEY] == 1
    assert ctx.metadata[TOOL_RESULT_METADATA_KEY] == {SHELL_EXIT_CODE_METADATA_KEY: 1}


@pytest.mark.asyncio
async def test_sub_agent_event_middleware_user_rejection_metadata_split():
    """Inner user rejections should not look like hook denials."""
    bus = EventBus()
    sub_events = SubAgentEventMiddleware(
        bus, "Explore", "inv-1", session_id="s-1", origin=InvocationOrigin("sub_agent", "s-1", "inv-1", None)
    )
    results = await _collect(bus, InvocationToolCallResult)
    ctx = SimpleNamespace(
        function=SimpleNamespace(name="write_file", chrys_kind="filesystem.write"),
        arguments={"path": "a.txt", "content": "x"},
        result=None,
        metadata={
            "call_id": "provider-user-call",
            _APPROVAL_REJECTED_KEY: True,
            _REJECTION_SOURCE_KEY: "user",
            _REJECTION_MESSAGE_KEY: "Denied by user",
        },
    )
    set_call_id(ctx, "inner-user-call")

    async def _next() -> None:
        ctx.result = "Error: Denied by user"

    await sub_events.process(ctx, _next)

    assert results[-1].call_id == "inner-user-call"
    assert results[-1].metadata[TOOL_FAILED_METADATA_KEY] is True
    assert results[-1].metadata["approval"] == "user_rejected"
    assert results[-1].metadata[TOOL_ERROR_KIND_METADATA_KEY] == "approval_rejected"
    assert results[-1].metadata[TOOL_ERROR_MESSAGE_METADATA_KEY] == "Denied by user"
    assert ctx.metadata[TOOL_RESULT_METADATA_KEY] == {
        TOOL_FAILED_METADATA_KEY: True,
        "approval": "user_rejected",
        TOOL_ERROR_KIND_METADATA_KEY: "approval_rejected",
        TOOL_ERROR_MESSAGE_METADATA_KEY: "Denied by user",
    }


@pytest.mark.asyncio
async def test_sub_agent_event_middleware_hook_denial_metadata_split():
    """Inner hook denials should not look like user approval rejections."""
    from chrys.service.hooks.events import HookEvent
    from chrys.service.hooks.schema import HookDecision

    class FakeHookManager:
        def __init__(self) -> None:
            self.after_payloads: list[dict[str, object]] = []

        def has_hooks_for(self, event: HookEvent) -> bool:
            return event in {HookEvent.BEFORE_TOOL_CALL, HookEvent.AFTER_TOOL_CALL}

        async def fire(self, event: HookEvent, payload: dict[str, object], **_kwargs: object) -> HookDecision:
            if event == HookEvent.BEFORE_TOOL_CALL:
                return HookDecision(blocked=True, block_reason="blocked by inner policy")
            self.after_payloads.append(payload)
            return HookDecision()

    bus = EventBus()
    results = await _collect(bus, InvocationToolCallResult)
    manager = FakeHookManager()
    sub_events = SubAgentEventMiddleware(
        bus,
        "Explore",
        "inv-1",
        hook_manager=manager,
        profile_name="Code",
        session_id="s-1",
        origin=InvocationOrigin("sub_agent", "s-1", "inv-1", None),
    )
    ctx = SimpleNamespace(
        function=SimpleNamespace(name="write_file", chrys_kind="filesystem.write"),
        arguments={"path": "a.txt", "content": "x"},
        result=None,
        metadata={"call_id": "provider-hook-call"},
    )
    set_call_id(ctx, "inner-hook-call")

    async def _next() -> None:
        raise AssertionError("blocked hook should prevent the inner tool body")

    await sub_events.process(ctx, _next)

    assert results[-1].call_id == "inner-hook-call"
    assert results[-1].result == "Error: blocked by inner policy"
    assert results[-1].metadata[TOOL_FAILED_METADATA_KEY] is True
    assert results[-1].metadata[TOOL_ERROR_KIND_METADATA_KEY] == "hook_denied"
    assert results[-1].metadata[TOOL_ERROR_MESSAGE_METADATA_KEY] == "blocked by inner policy"
    assert "approval" not in results[-1].metadata
    assert manager.after_payloads[0]["result"] == {
        "text": "Error: blocked by inner policy",
        "duration_ms": 0,
        "error": False,
        "failed": True,
        "approval_rejected": True,
        "rejection_source": "hook",
        "hook_denied": True,
    }
    assert ctx.metadata[TOOL_RESULT_METADATA_KEY] == {
        TOOL_FAILED_METADATA_KEY: True,
        TOOL_ERROR_KIND_METADATA_KEY: "hook_denied",
        TOOL_ERROR_MESSAGE_METADATA_KEY: "blocked by inner policy",
    }


@pytest.mark.asyncio
async def test_sub_agent_event_middleware_marks_exceptions_errored():
    """Inner tool exceptions should set structured errored metadata."""
    bus = EventBus()
    sub_events = SubAgentEventMiddleware(
        bus, "Explore", "inv-1", session_id="s-1", origin=InvocationOrigin("sub_agent", "s-1", "inv-1", None)
    )
    results = await _collect(bus, InvocationToolCallResult)
    ctx = SimpleNamespace(
        function=SimpleNamespace(name="read_file", chrys_kind="filesystem.read"),
        arguments={"path": "missing.txt"},
        result=None,
        metadata={},
    )
    set_call_id(ctx, "read-call")

    async def _next() -> None:
        raise ValueError("missing")

    with pytest.raises(ValueError, match="missing"):
        await sub_events.process(ctx, _next)

    assert results[-1].call_id == "read-call"
    assert results[-1].result == "Error: missing"
    assert results[-1].metadata[TOOL_ERRORED_METADATA_KEY] is True


@pytest.mark.asyncio
async def test_external_cancel_without_cascade_request_does_not_publish():
    """When the controller's run task is cancelled externally WITHOUT
    ``_cascade_requested`` being set (e.g. main-agent stream retry
    cleanup tearing down in-flight tool tasks), the controller must NOT
    publish :class:`InvocationCascadeAborted`.

    Emitting that event on the old invocation would stamp
    "cancelled (global interrupt)" on the TUI card even though the user
    never interrupted — the main agent is merely retrying.  The fresh
    invocation gets its own id + card on retry."""
    bus = EventBus()
    cascade_events = await _collect(bus, InvocationCascadeAborted)

    started = asyncio.Event()

    async def _long_run(prompt, **kwargs):
        started.set()
        await asyncio.sleep(10)
        return _StubResponse(text="never")

    agent = _ScriptedAgent()
    agent.run = _long_run  # type: ignore[assignment]

    ctrl = _make_controller(agent, bus)
    run_task = asyncio.create_task(ctrl.run())
    await started.wait()

    # Simulate external cancellation (e.g. stream cleanup) — direct task
    # cancel, no ``cascade_abort()`` call so ``_cascade_requested`` stays False.
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert cascade_events == []


@pytest.mark.asyncio
async def test_retry_then_pause_then_abort_multi_cycle():
    """Retry, stall again, abort — exercises the outer while-loop cycle."""
    bus = EventBus()

    agent = _ScriptedAgent(
        outcomes=[
            RuntimeError("first fail"),
            RuntimeError("second fail"),
        ]
    )
    ctrl = _make_controller(agent, bus)
    run_task = asyncio.create_task(ctrl.run())

    # First pause — retry
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)
    assert "first fail" in ctrl.policy.last_error
    ctrl.request_retry()

    # Second pause — abort
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED, exclude_first=True)
    assert "second fail" in ctrl.policy.last_error
    ctrl.request_abort()

    result = await run_task
    assert "second fail" in result
    assert ctrl.status == SubAgentStatus.ABORTED


@pytest.mark.asyncio
async def test_concurrent_controllers_pause_independently():
    """Two controllers failing simultaneously should each receive their own
    decision. Retrying A must not affect B; aborting B must not disturb A."""
    bus = EventBus()
    paused_events = await _collect(bus, InvocationPaused)

    agent_a = _ScriptedAgent(outcomes=[RuntimeError("A fails"), "A succeeds"])
    agent_b = _ScriptedAgent(outcomes=[RuntimeError("B fails")])

    ctrl_a = kernel_shell(
        conversation=Conversation(),
        parent_origin=None,
        invocation_id="a",
        tool_name="Explore",
        agent_name="Explore",
        agent=agent_a,
        **_controller_runtime(),
        prompt="A",
        run_kwargs={},
        event_bus=bus,
        max_retries=2,
        backoff_schedule=(0, 0, 0),
    )
    ctrl_b = kernel_shell(
        conversation=Conversation(),
        parent_origin=None,
        invocation_id="b",
        tool_name="Plan",
        agent_name="Plan",
        agent=agent_b,
        **_controller_runtime(),
        prompt="B",
        run_kwargs={},
        event_bus=bus,
        max_retries=2,
        backoff_schedule=(0, 0, 0),
    )

    task_a = asyncio.create_task(ctrl_a.run())
    task_b = asyncio.create_task(ctrl_b.run())

    # Both should pause.
    await _wait_until(lambda: ctrl_a.status == SubAgentStatus.PAUSED and ctrl_b.status == SubAgentStatus.PAUSED)

    ids = sorted(ev.origin.invocation_id for ev in paused_events)
    assert ids == ["a", "b"]

    # Retry A, abort B — decisions routed by controller identity.
    ctrl_a.request_retry()
    ctrl_b.request_abort()

    result_a = await task_a
    result_b = await task_b

    assert result_a == "A succeeds"
    assert ctrl_a.status == SubAgentStatus.COMPLETED

    assert result_b.startswith("Error: sub-agent 'Plan' aborted by user")
    assert "B fails" in result_b
    assert ctrl_b.status == SubAgentStatus.ABORTED


@pytest.mark.asyncio
async def test_request_retry_ignored_when_not_paused():
    """Retry on a controller that's not paused should be a no-op (returns False)."""
    bus = EventBus()
    agent = _ScriptedAgent(outcomes=["ok"])
    ctrl = _make_controller(agent, bus)

    # Before run — IDLE.
    assert ctrl.request_retry() is False
    result = await ctrl.run()
    assert result == "ok"
    # After completion.
    assert ctrl.request_retry() is False


@pytest.mark.asyncio
async def test_persists_pause_record_and_cleans_up_on_abort(tmp_path):
    """A paused controller should write a json record under persist_dir
    and queue it for post-save cleanup on terminal exit."""
    bus = EventBus()
    agent = _ScriptedAgent(outcomes=[RuntimeError("borked")])
    queued: list[Path | None] = []

    ctrl = kernel_shell(
        conversation=Conversation(),
        parent_origin=None,
        invocation_id="persist-1",
        tool_name="Explore",
        agent_name="Explore",
        agent=agent,
        **_controller_runtime(),
        prompt="what is life",
        run_kwargs={},
        event_bus=bus,
        session_id="s-1",
        max_retries=0,
        backoff_schedule=(0,),
        persist_dir=tmp_path,
        pending_record_finalizer=queued.append,
    )

    run_task = asyncio.create_task(ctrl.run())
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)

    # File exists while paused.
    persist_file = tmp_path / "persist-1.json"
    assert persist_file.is_file()

    import json as _json

    data = _json.loads(persist_file.read_text(encoding="utf-8"))
    assert data["invocation_id"] == "persist-1"
    assert data["tool_name"] == "Explore"
    assert data["prompt_preview"] == "what is life"
    assert "prompt" not in data
    assert data["failure_reason"] == "framework_exc"
    assert "borked" in data["last_error"]
    # Default-empty parent_call_id when the controller is built
    # without one (still serialized so downstream code doesn't have
    # to branch on missing keys).
    assert data["parent_call_id"] == ""
    assert data["schema_version"] == 1

    ctrl.request_abort()
    await run_task

    assert queued == [persist_file]
    assert persist_file.exists()

    persist_file.unlink()
    assert not persist_file.exists()


@pytest.mark.asyncio
async def test_persists_parent_call_id_when_provided(tmp_path):
    """``parent_call_id`` round-trips through ``_serialize_state`` so
    reload-recovery can pair the record back to its assistant
    function_call by id rather than by name+order."""
    bus = EventBus()
    agent = _ScriptedAgent(outcomes=[RuntimeError("boom")])

    ctrl = kernel_shell(
        conversation=Conversation(),
        parent_origin=None,
        invocation_id="persist-pcid",
        tool_name="Explore",
        agent_name="Explore",
        agent=agent,
        **_controller_runtime(),
        prompt="p",
        run_kwargs={},
        event_bus=bus,
        session_id="s-1",
        parent_call_id="assistant-call-xyz",
        max_retries=0,
        backoff_schedule=(0,),
        persist_dir=tmp_path,
    )

    run_task = asyncio.create_task(ctrl.run())
    await _wait_until(lambda: ctrl.status == SubAgentStatus.PAUSED)

    import json as _json

    data = _json.loads((tmp_path / "persist-pcid.json").read_text(encoding="utf-8"))
    assert data["parent_call_id"] == "assistant-call-xyz"

    ctrl.request_abort()
    await run_task


@pytest.mark.asyncio
async def test_persist_file_cleaned_up_on_success(tmp_path):
    """Successful run should still call the cleanup path — no stale file."""
    bus = EventBus()
    agent = _ScriptedAgent(outcomes=["done"])
    ctrl = kernel_shell(
        conversation=Conversation(),
        parent_origin=None,
        invocation_id="persist-2",
        tool_name="Plan",
        agent_name="Plan",
        agent=agent,
        **_controller_runtime(),
        prompt="p",
        run_kwargs={},
        event_bus=bus,
        max_retries=0,
        backoff_schedule=(0,),
        persist_dir=tmp_path,
    )
    await ctrl.run()
    assert not (tmp_path / "persist-2.json").exists()


@pytest.mark.asyncio
async def test_empty_text_response_returns_error_string():
    """An empty-string agent response is rejected as an 'Error:' result."""
    bus = EventBus()
    agent = _ScriptedAgent(outcomes=[""])
    ctrl = _make_controller(agent, bus)
    result = await ctrl.run()
    assert result.startswith("Error: sub-agent '")
    assert "returned no output" in result
    assert ctrl.status == SubAgentStatus.COMPLETED


@pytest.mark.asyncio
async def test_terminal_log_write_attempted_after_failed_initial_write(tmp_path) -> None:
    """A writer whose initial write failed (``active`` False) must still get
    the terminal record — it is the only durable copy of the inner history."""
    ctrl = _make_controller(_ScriptedAgent(), None)
    calls: list[dict] = []
    terminal_callbacks: list[bool] = []

    class _Writer:
        active = False
        path = tmp_path / "log.json"

        async def write(self, **kwargs):
            calls.append(kwargs)
            return True

    ctrl.policy._log_writer = _Writer()
    ctrl.policy._terminal_audit_callback = lambda: terminal_callbacks.append(True)

    assert await ctrl.policy._write_log(status="running") is False
    assert calls == []

    assert await ctrl.policy._write_log(status="cancelled", result="cancelled", ended=True) is True
    assert len(calls) == 1
    assert calls[0]["ended"] is True
    assert ctrl.policy.terminal_audit_persisted is True
    assert terminal_callbacks == [True]


@pytest.mark.asyncio
async def test_failed_terminal_log_write_is_not_reported_as_durable(tmp_path) -> None:
    ctrl = _make_controller(_ScriptedAgent(), None)
    calls: list[dict] = []

    class _Writer:
        active = True
        path = tmp_path / "log.json"

        async def write(self, **kwargs):
            calls.append(kwargs)
            return False

    ctrl.policy._log_writer = _Writer()

    assert await ctrl.policy._write_log(status="failed", result="failed", ended=True) is False
    assert len(calls) == 2
    assert ctrl.policy.terminal_audit_persisted is False


def test_terminal_audit_upgrades_an_early_interrupted_parent_result() -> None:
    from chrys.foundation.util.sub_agent_context import SubAgentParentResultMetadata
    from chrys.orchestration.sub_agents.tools import (
        _commit_interrupted_parent_result,
        _upgrade_interrupted_parent_result,
    )

    snapshots: list[dict[str, object]] = []
    metadata = SubAgentParentResultMetadata(
        sub_agent_invocation_id="inv-1",
        commit_interrupted_result=lambda update: snapshots.append(dict(update)),
    )

    _commit_interrupted_parent_result(metadata)
    metadata.sub_agent_log_file = "child.json"
    metadata.sub_agent_audit_complete = True
    _upgrade_interrupted_parent_result(metadata)

    assert snapshots == [
        {"sub_agent_invocation_id": "inv-1"},
        {
            "sub_agent_invocation_id": "inv-1",
            "sub_agent_log_file": "child.json",
            "sub_agent_audit_complete": True,
        },
    ]


# --- helpers -------------------------------------------------------------


# Deliberately local: exclude_first waits for a false-to-true transition.
async def _wait_until(predicate, *, timeout: float = 2.0, exclude_first: bool = False):
    """Poll until ``predicate()`` is True or ``timeout`` elapses.

    ``exclude_first`` flips the predicate to wait for a transition out
    of and back into the state (used for "second pause" tests where the
    first pause was already observed).

    An ``asyncio.Event`` would be a cleaner signal, but the controller
    already exposes the state via properties — this helper is a thin
    poll used only in tests, so we deliberately suppress ASYNC110 here.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    if exclude_first:
        # Wait for predicate to become False first, then True.
        while predicate() and asyncio.get_running_loop().time() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("predicate never became True")
        await asyncio.sleep(0.01)


def test_continuation_token_observer_survives_sub_agent_service_restore():
    ctrl = _make_controller(
        _ScriptedAgent(),
        None,
        run_kwargs={"options": {"store": True, "conversation_id": "handle"}},
    )

    ctrl.policy.observe_continuation_token({"response_id": "pending"})
    assert ctrl.policy._run_kwargs["options"]["continuation_token"] == {"response_id": "pending"}

    ctrl.policy._session.service_session_id = "stale-child-session"
    ctrl.policy._attempts._restore_service_retry_inputs(ctrl.policy._run_kwargs)
    assert ctrl.policy._session.service_session_id is None
    assert ctrl.policy._run_kwargs["options"]["continuation_token"] == {"response_id": "pending"}
    assert "conversation_id" not in ctrl.policy._run_kwargs["options"]

    # The restore replaced the options dict; the observer must keep
    # targeting the live one.
    ctrl.policy.observe_continuation_token({"response_id": "renewed"})
    assert ctrl.policy._run_kwargs["options"]["continuation_token"] == {"response_id": "renewed"}
    ctrl.policy.observe_continuation_token(None)
    assert "continuation_token" not in ctrl.policy._run_kwargs["options"]
