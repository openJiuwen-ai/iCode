# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-logical-wire-call retry and stream-boundary behavior."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from chrys.foundation.errors import ProviderResponseError
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.retry import StreamStall
from chrys.kernel import (
    AgentSession,
    ChatClientException,
    ChatResponse,
    ChatResponseUpdate,
    ConsumedInjectionMessageProbe,
    Content,
    FunctionTool,
    LoopRecorder,
    Message,
    ResponseStream,
    StallExhaustedAction,
    is_retry_boundary_update,
    report_wire_progress,
    tool,
    wire_progress_scope,
)
from chrys.kernel import loop as loop_module
from chrys.kernel.middleware import ChatMiddleware, ChatMiddlewareLayer
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.agent_middleware.response_validation import ResponseValidationMiddleware
from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware
from chrys.service.agent_middleware.system_reminder import wrap_system_reminder as _wrap
from tests.support.transcript_invariants import InvariantCheckedToolLoopLayer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable


@pytest.fixture(autouse=True)
def _fast_continuation_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop_module, "CONTINUATION_POLL_INTERVAL_SECONDS", 0.0)


def _user(text: str = "go") -> Message:
    return Message("user", [text])


def _text_response(text: str, **kwargs: Any) -> ChatResponse:
    return ChatResponse(messages=[Message("assistant", [text])], **kwargs)


def _call_response(call_id: str, name: str, **arguments: Any) -> ChatResponse:
    return ChatResponse(
        messages=[Message("assistant", [Content.from_function_call(call_id, name, arguments=arguments)])]
    )


def _text_update(text: str, **kwargs: Any) -> ChatResponseUpdate:
    return ChatResponseUpdate(contents=[Content.from_text(text)], role="assistant", **kwargs)


def _silent_update(**kwargs: Any) -> ChatResponseUpdate:
    """Stream progress without readable text: a replay cannot repeat anything shown."""
    return ChatResponseUpdate(contents=[], role="assistant", **kwargs)


def _call_update(call_id: str, name: str, **arguments: Any) -> ChatResponseUpdate:
    return ChatResponseUpdate(
        contents=[Content.from_function_call(call_id, name, arguments=arguments)],
        role="assistant",
    )


@dataclass
class _Policy:
    max_retries: int = 2
    stall_timeout_seconds: float | None = None
    stall_max_retries: int = 0
    stall_exhausted_action: StallExhaustedAction = StallExhaustedAction.BLOCKING_FALLBACK
    hosted_commits_in_flight: Callable[[], tuple[str, ...]] | None = None
    answer_shown: Callable[[], bool] | None = None
    events: list[tuple[str, int, int, int, BaseException]] = field(default_factory=list)
    before_retry_calls: int = 0
    before_retry_hook: Any = None
    sleep_hook: Any = None

    def backoff_seconds(self, _attempt: int) -> int:
        return 0

    def is_retryable(self, exc: BaseException) -> bool:
        return isinstance(exc, ConnectionError | StreamStall)

    def is_interrupted(self) -> bool:
        return False

    async def sleep(self, seconds: int) -> bool:
        if self.sleep_hook is not None:
            self.sleep_hook()
        return False

    async def on_retry(
        self,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
        exc: BaseException,
    ) -> None:
        self.events.append((message, attempt, max_attempts, delay_seconds, exc))

    def before_retry(self) -> None:
        self.before_retry_calls += 1
        if self.before_retry_hook is not None:
            self.before_retry_hook()


_STALL_TIMEOUT = 0.01


async def _silent_then_hung(
    policy: _Policy, on_close: Callable[[], None] | None = None
) -> AsyncIterator[ChatResponseUpdate]:
    """One silent update, then a pull that never answers: the only pull the stall watchdog times.

    The watchdog times idle wall-clock gaps, and a starved runner can hold a healthy pull longer
    than a test-sized timeout; the stream the script meant to succeed then stalls, and the loop
    asks the wire for a call the script never planned. Armed as this stream hands over its update,
    for the pull that follows, and disarmed when the stream closes, it fires on that pull alone.
    """
    policy.stall_timeout_seconds = _STALL_TIMEOUT
    try:
        yield _silent_update()
        await asyncio.Event().wait()
    finally:
        policy.stall_timeout_seconds = None
        if on_close is not None:
            on_close()


class _ScriptedWire:
    STORES_BY_DEFAULT = False

    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []

    def get_response(
        self,
        messages: list[Message],
        *,
        stream: bool = False,
        options: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        self.calls.append(
            {
                "messages": list(messages),
                "options": dict(options or {}),
                "stream": stream,
                "kwargs": dict(kwargs),
            }
        )
        outcome = self.outcomes.pop(0)
        if callable(outcome):
            outcome = outcome()

        if not stream:

            async def _blocking() -> ChatResponse:
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

            return _blocking()

        if isinstance(outcome, ResponseStream):
            return outcome

        async def _updates():
            if isinstance(outcome, BaseException):
                raise outcome
            for update in outcome:
                yield update

        return ResponseStream(_updates(), finalizer=ChatResponse.from_updates)


def _wrapped_network_error() -> ChatClientException:
    cause = ConnectionError("peer closed connection")
    try:
        raise ChatClientException(
            "Chat Completions request failed: peer closed connection", inner_exception=cause
        ) from cause
    except ChatClientException as exc:
        return exc


class _ChatClientRetryPolicy(_Policy):
    def is_retryable(self, exc: BaseException) -> bool:
        return isinstance(exc, ChatClientException)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_wire_retry_publishes_clean_provider_error(stream: bool) -> None:
    success: Any = [_text_update("done")] if stream else _text_response("done")
    wire = _ScriptedWire([_wrapped_network_error(), success])
    policy = _ChatClientRetryPolicy()
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire))

    response = layer.get_response(
        [_user()],
        stream=stream,
        client_kwargs={"wire_retry_policy": policy},
    )
    if stream:
        assert isinstance(response, ResponseStream)
        await response.get_final_response()
    else:
        await response

    assert policy.events[0][0] == "peer closed connection"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_forced_stateless_store_true_still_uses_local_wire_retry(stream: bool) -> None:
    success: Any = [_text_update("done")] if stream else _text_response("done")
    wire = _ScriptedWire([ConnectionError("retry me"), success])
    wire.STORES_BY_DEFAULT = True
    wire.FORCES_STATELESS = True
    policy = _Policy()
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire))

    response = layer.get_response(
        [_user()],
        stream=stream,
        options={"store": True},
        client_kwargs={"wire_retry_policy": policy},
    )
    if stream:
        assert isinstance(response, ResponseStream)
        await response.get_final_response()
    else:
        await response

    assert len(wire.calls) == 2
    assert len(policy.events) == 1


def _layer(wire: _ScriptedWire, *, validation: bool = False) -> InvariantCheckedToolLoopLayer:
    middleware = [ResponseValidationMiddleware()] if validation else None
    return InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire, middleware=middleware))


def _echo_tool(runs: list[str]) -> FunctionTool:
    @tool(name="echo")
    async def echo(text: str) -> str:
        runs.append(text)
        return f"echo:{text}"

    return echo


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_retry_after_committed_tool_work_reissues_only_failed_logical_call(stream: bool) -> None:
    runs: list[str] = []
    policy = _Policy()
    first = [_call_update("c1", "echo", text="once")] if stream else _call_response("c1", "echo", text="once")
    final = [_text_update("done")] if stream else _text_response("done")
    wire = _ScriptedWire([first, ConnectionError("wire dropped"), final])
    layer = _layer(wire)

    pending = layer.get_response(
        [_user()],
        stream=stream,
        options={"tools": [_echo_tool(runs)]},
        client_kwargs={"wire_retry_policy": policy},
    )
    if stream:
        updates = [update async for update in pending]
        response = await pending.get_final_response()
        assert sum(is_retry_boundary_update(update) for update in updates) == 1
    else:
        response = await pending

    assert runs == ["once"]
    assert len(wire.calls) == 3
    assert response.messages[-1].text == "done"
    assert [event[1] for event in policy.events] == [1]
    assert policy.before_retry_calls == 1
    assert sum(content.type == "function_call" for message in response.messages for content in message.contents) == 1
    assert sum(content.type == "function_result" for message in response.messages for content in message.contents) == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_continuation_token_is_polled_to_terminal_inside_logical_call(stream: bool) -> None:
    token = {"response_id": "pending-1"}
    if stream:
        pending = [_silent_update(continuation_token=token)]
        terminal = [_text_update("terminal")]
    else:
        pending = _text_response("not-terminal", continuation_token=token)
        terminal = _text_response("terminal")
    wire = _ScriptedWire([pending, terminal])
    layer = _layer(wire, validation=True)

    result = layer.get_response([_user()], stream=stream)
    if stream:
        _ = [update async for update in result]
        response = await result.get_final_response()
    else:
        response = await result

    assert response.text == "terminal"
    assert response.continuation_token is None
    assert wire.calls[0]["options"].get("continuation_token") is None
    assert wire.calls[1]["options"]["continuation_token"] == token


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_nonretryable_failure_after_tool_commit_preserves_exchange(stream: bool) -> None:
    runs: list[str] = []
    recorder = LoopRecorder()
    policy = _Policy()
    first = [_call_update("c1", "echo", text="kept")] if stream else _call_response("c1", "echo", text="kept")
    wire = _ScriptedWire([first, RuntimeError("terminal failure")])
    layer = _layer(wire)

    pending = layer.get_response(
        [_user()],
        stream=stream,
        options={"tools": [_echo_tool(runs)]},
        client_kwargs={"loop_recorder": recorder, "wire_retry_policy": policy},
    )
    with pytest.raises(RuntimeError, match="terminal failure"):
        if stream:
            _ = [update async for update in pending]
        else:
            await pending

    assert runs == ["kept"]
    assert policy.events == []
    assert recorder.committed_count == 1
    assert recorder.loop_messages is not None
    assert [message.role for message in recorder.loop_messages] == ["assistant", "tool"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_exhausted_wire_retry_after_tool_commit_preserves_exchange(stream: bool) -> None:
    runs: list[str] = []
    recorder = LoopRecorder()
    policy = _Policy(max_retries=1)
    first = [_call_update("c1", "echo", text="kept")] if stream else _call_response("c1", "echo", text="kept")
    wire = _ScriptedWire(
        [
            first,
            ConnectionError("first failure"),
            ConnectionError("exhausted failure"),
        ]
    )
    layer = _layer(wire)

    pending = layer.get_response(
        [_user()],
        stream=stream,
        options={"tools": [_echo_tool(runs)]},
        client_kwargs={"loop_recorder": recorder, "wire_retry_policy": policy},
    )
    with pytest.raises(ConnectionError, match="exhausted failure"):
        if stream:
            _ = [update async for update in pending]
        else:
            await pending

    assert runs == ["kept"]
    assert len(policy.events) == 1
    assert recorder.committed_count == 1
    assert recorder.loop_messages is not None
    assert [message.role for message in recorder.loop_messages] == ["assistant", "tool"]


@pytest.mark.asyncio
async def test_stall_fallback_closes_nested_stream_and_continues_tool_loop() -> None:
    runs: list[str] = []
    closed = 0
    cleanups = 0
    policy = _Policy()

    def _closed() -> None:
        nonlocal closed
        closed += 1

    async def _cleanup() -> None:
        nonlocal cleanups
        cleanups += 1

    inner = ResponseStream(_silent_then_hung(policy, _closed), finalizer=ChatResponse.from_updates)
    inner.with_cleanup_hook(_cleanup)

    async def _resolve_inner() -> ResponseStream[ChatResponseUpdate, ChatResponse]:
        return inner

    stalled = ResponseStream.from_awaitable(_resolve_inner())
    stalled.with_cleanup_hook(_cleanup)
    wire = _ScriptedWire(
        [
            [_call_update("c1", "echo", text="first")],
            stalled,
            _call_response("c2", "echo", text="fallback"),
            [_text_update("done")],
        ]
    )
    layer = _layer(wire)
    stream = layer.get_response(
        [_user()],
        stream=True,
        options={"tools": [_echo_tool(runs)]},
        client_kwargs={"wire_retry_policy": policy},
    )

    updates = [update async for update in stream]
    response = await stream.get_final_response()

    assert runs == ["first", "fallback"]
    assert [call["stream"] for call in wire.calls] == [True, True, False, True]
    assert sum(is_retry_boundary_update(update) for update in updates) == 1
    assert len(policy.events) == 1
    assert "blocking" in policy.events[0][0].lower()
    assert response.messages[-1].text == "done"
    assert "discard-me" not in response.text
    assert closed == 1
    assert cleanups == 2


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_zero_budget_transport_failure_does_not_retry(stream: bool) -> None:
    """A zero transient budget permits only the initial wire attempt."""
    success: Any = [_text_update("must stay unused")] if stream else _text_response("must stay unused")
    wire = _ScriptedWire([ConnectionError("wire dropped"), success])
    policy = _Policy(max_retries=0)
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire))

    pending = layer.get_response(
        [_user()],
        stream=stream,
        client_kwargs={"wire_retry_policy": policy},
    )
    with pytest.raises(ConnectionError, match="wire dropped"):
        if stream:
            _ = [update async for update in pending]
        else:
            await pending

    assert len(wire.calls) == 1
    assert policy.events == []
    assert policy.before_retry_calls == 0


@pytest.mark.parametrize("budget,expected_calls", [(1, 5), (2, 8)])
@pytest.mark.asyncio
async def test_local_streaming_wire_lanes_compose_to_3n_plus_2_worst_case(budget: int, expected_calls: int) -> None:
    """With transient budget N, the local-streaming wire layer can issue
    3N+2 provider calls: N stream transient restarts, N stall restarts, one
    stall-exhausted blocking fallback, then N fallback transient retries before
    the final success. The transient and stall lanes are independent counters,
    and the blocking fallback carries its own transient lane."""

    policy = _Policy(max_retries=budget, stall_max_retries=budget)

    def _stalled_stream() -> ResponseStream:
        return ResponseStream(_silent_then_hung(policy), finalizer=ChatResponse.from_updates)

    outcomes: list[Any] = []
    outcomes.extend(ConnectionError(f"stream transient {i}") for i in range(budget))
    outcomes.extend(_stalled_stream for _ in range(budget))
    outcomes.append(_stalled_stream)  # exhausts the stall lane -> blocking fallback
    outcomes.extend(ConnectionError(f"fallback transient {i}") for i in range(budget))
    outcomes.append(_text_response("done"))

    wire = _ScriptedWire(outcomes)
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire))

    stream = layer.get_response(
        [_user()],
        stream=True,
        client_kwargs={"wire_retry_policy": policy},
    )
    updates = [update async for update in stream]
    response = await stream.get_final_response()

    assert len(wire.calls) == expected_calls  # 3N + 2 provider calls
    # 2N+1 streaming attempts (N transient + N stall + 1 exhausted) then N+1
    # blocking attempts (N fallback transient retries + 1 success).
    assert [call["stream"] for call in wire.calls] == [True] * (2 * budget + 1) + [False] * (budget + 1)
    assert response.messages[-1].text == "done"
    # One retry boundary per restart: N stream transient + N stall + exhaustion.
    assert sum(is_retry_boundary_update(update) for update in updates) == 2 * budget + 1
    transient = [
        (attempt, max_attempts)
        for _message, attempt, max_attempts, _delay, exc in policy.events
        if isinstance(exc, ConnectionError)
    ]
    stall = [
        (attempt, max_attempts)
        for _message, attempt, max_attempts, _delay, exc in policy.events
        if isinstance(exc, StreamStall)
    ]
    # N transient retries on the stream lane + N on the blocking fallback lane.
    assert transient == [(i, budget) for i in range(1, budget + 1)] * 2
    # N stall retries, then the exhaustion event that triggers the fallback.
    assert stall == [(i, budget) for i in range(1, budget + 1)] + [(budget + 1, budget + 1)]


@pytest.mark.asyncio
async def test_stall_downgraded_exhaustion_final_is_stripped() -> None:
    """The stall-downgrade blocking product also passes the exhaustion strip.

    The downgrade assigns an unfiltered blocking response inside the streaming
    retry layer; the tail must still strip its unexecutable calls and
    synthesize the fallback. Wire retries only exist under client-side
    storage (service-side storage nulls the retry policy), so the strip must
    not invalidate anything here: the client transcript is canonical and the
    next request cannot replay the stripped calls. The downgrade product's
    conversation handle never reaches the outer assembly in the first place —
    the blocking response is assigned as the wire-layer final without its
    response-level metadata being re-yielded as updates.
    """
    runs: list[str] = []
    policy = _Policy()
    stalled = ResponseStream(_silent_then_hung(policy), finalizer=ChatResponse.from_updates)
    session = AgentSession()
    downgraded_final = ChatResponse(
        messages=[Message("assistant", [Content.from_function_call("c2", "echo", arguments={"text": "late"})])],
        conversation_id="conv-svc",
    )
    wire = _ScriptedWire(
        [
            [_call_update("c1", "echo", text="first")],
            stalled,
            downgraded_final,
        ]
    )
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire), max_iterations=1)
    stream = layer.get_response(
        [_user()],
        stream=True,
        options={"tools": [_echo_tool(runs)]},
        client_kwargs={"wire_retry_policy": policy, "session": session},
    )
    updates = [update async for update in stream]
    response = await stream.get_final_response()

    assert runs == ["first"]
    assert [call["stream"] for call in wire.calls] == [True, True, False]
    assert sum(is_retry_boundary_update(update) for update in updates) == 1
    call_ids = [
        content.call_id
        for message in response.messages
        for content in message.contents
        if content.type == "function_call"
    ]
    assert call_ids == ["c1"], "the downgraded product's call must not persist"
    assert response.messages[-1].text == loop_module._MAX_ITERATIONS_FALLBACK_TEXT
    assert response.conversation_id is None
    # Client-side storage: the strip leaves the session's ordinary
    # continuation tracking alone.
    assert session.service_session_id == "conv-svc"


@pytest.mark.asyncio
async def test_retry_boundary_marker_is_not_assembled_into_response() -> None:
    marker = ChatResponseUpdate.retry_boundary()
    response = ChatResponse.from_updates([_text_update("before"), marker, _text_update("after")])

    assert response.text == "beforeafter"
    assert is_retry_boundary_update(marker)
    assert marker.contents == []


@pytest.mark.asyncio
async def test_a_retry_before_any_text_emits_a_boundary_before_the_fresh_attempt() -> None:
    async def _failed_updates():
        yield _call_update("c1", "echo", text="partial call")
        raise ConnectionError("stream reset")

    failed = ResponseStream(_failed_updates(), finalizer=ChatResponse.from_updates)
    policy = _Policy()
    wire = _ScriptedWire([failed, [_text_update("fresh answer")]])
    layer = _layer(wire)
    stream = layer.get_response(
        [_user()],
        stream=True,
        client_kwargs={"wire_retry_policy": policy},
    )

    updates = [update async for update in stream]
    response = await stream.get_final_response()
    marker_index = next(index for index, update in enumerate(updates) if is_retry_boundary_update(update))

    assert updates[marker_index - 1].contents[0].call_id == "c1"
    assert updates[marker_index + 1].text == "fresh answer"
    assert response.text == "fresh answer"
    assert len(policy.events) == 1


@pytest.mark.asyncio
async def test_hosted_commit_evidence_vetoes_blocking_wire_retry() -> None:
    # A valid blocking response can land hosted side effects and STILL fail
    # the attempt when an outer middleware raises after validation observed
    # the response; retrying would re-create the request and re-run the
    # hosted work server-side.
    class _OuterRaiser(ChatMiddleware):
        async def process(self, context: Any, call_next: Any) -> None:
            await call_next()
            raise ConnectionError("outer probe failed")

    hosted = ChatResponse(
        messages=[
            Message(
                "assistant",
                [
                    Content.from_text("issue filed"),
                    Content.from_mcp_server_tool_result("mc1", output=[Content.from_text("done")]),
                ],
            )
        ]
    )
    validation = ResponseValidationMiddleware()
    policy = _Policy()
    policy.hosted_commits_in_flight = validation.hosted_commits_in_flight
    wire = _ScriptedWire([hosted, _text_response("must stay unused")])
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire, middleware=[_OuterRaiser(), validation]))

    with pytest.raises(ConnectionError, match="outer probe failed"):
        await layer.get_response(
            [_user()],
            stream=False,
            client_kwargs={"wire_retry_policy": policy},
        )

    assert len(wire.calls) == 1
    assert policy.events == []


@pytest.mark.asyncio
async def test_hosted_free_blocking_wire_retry_still_replays() -> None:
    # The veto must not over-block: a transport failure with no hosted work
    # observed in the attempt retries as before, even with the probe wired.
    validation = ResponseValidationMiddleware()
    policy = _Policy()
    policy.hosted_commits_in_flight = validation.hosted_commits_in_flight
    wire = _ScriptedWire([ConnectionError("wire dropped"), _text_response("recovered")])
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire, middleware=[validation]))

    response = await layer.get_response(
        [_user()],
        stream=False,
        client_kwargs={"wire_retry_policy": policy},
    )

    assert len(wire.calls) == 2
    assert response.messages[-1].text == "recovered"
    assert len(policy.events) == 1


@pytest.mark.asyncio
async def test_hosted_commit_evidence_vetoes_mid_stream_wire_replay() -> None:
    # A hosted MCP result streamed before the drop proves the provider ran
    # side effects server-side; replaying the create would run them again.
    # The validation middleware observes the update as it lands and the
    # policy surfaces that evidence to the kernel, which must re-raise
    # instead of re-issuing the request.
    async def _failed_updates():
        yield ChatResponseUpdate(
            contents=[Content.from_mcp_server_tool_result("mc1", output=[Content.from_text("done")])],
            role="assistant",
        )
        raise ConnectionError("stream reset")

    failed = ResponseStream(_failed_updates(), finalizer=ChatResponse.from_updates)
    validation = ResponseValidationMiddleware()
    policy = _Policy()
    policy.hosted_commits_in_flight = validation.hosted_commits_in_flight
    wire = _ScriptedWire([failed, [_text_update("must stay unused")]])
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire, middleware=[validation]))
    stream = layer.get_response(
        [_user()],
        stream=True,
        client_kwargs={"wire_retry_policy": policy},
    )

    with pytest.raises(ConnectionError, match="stream reset"):
        _ = [update async for update in stream]

    assert len(wire.calls) == 1
    assert policy.events == []


class _ProviderRetryPolicy(_Policy):
    def is_retryable(self, exc: BaseException) -> bool:
        return isinstance(exc, ProviderResponseError) and exc.retryable


def _failed_after_hosted_work(*, invalidates: bool, hosted: bool = True) -> ProviderResponseError:
    # What an adapter raises when the response failed after the provider ran
    # hosted work it never yielded.
    observed = (
        Content.from_mcp_server_tool_call("mc1", "create_issue", server_name="github"),
        Content.from_mcp_server_tool_result("mc1", output=[Content.from_text("created #42")]),
    )
    return ProviderResponseError(
        "stream_truncated",
        "the response failed",
        retryable=True,
        invalidates_continuation_token=invalidates,
        observed_contents=observed if hosted else (),
    )


def _validated_layer(wire: _ScriptedWire) -> tuple[InvariantCheckedToolLoopLayer, _ProviderRetryPolicy]:
    validation = ResponseValidationMiddleware()
    policy = _ProviderRetryPolicy()
    policy.hosted_commits_in_flight = validation.hosted_commits_in_flight
    return InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire, middleware=[validation])), policy


async def _drive(layer: InvariantCheckedToolLoopLayer, policy: _Policy, *, stream: bool) -> ChatResponse:
    result = layer.get_response([_user()], stream=stream, client_kwargs={"wire_retry_policy": policy})
    if stream:
        assert isinstance(result, ResponseStream)
        _ = [update async for update in result]
        return await result.get_final_response()
    return await result


@pytest.mark.parametrize("hosted", [True, False], ids=["hosted_work", "no_hosted_work"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_hosted_work_on_a_provider_failure_vetoes_wire_retry(stream: bool, hosted: bool) -> None:
    success: Any = [_text_update("recovered")] if stream else _text_response("recovered")
    wire = _ScriptedWire([_failed_after_hosted_work(invalidates=False, hosted=hosted), success])
    layer, policy = _validated_layer(wire)

    if hosted:
        with pytest.raises(ProviderResponseError, match="the response failed"):
            await _drive(layer, policy, stream=stream)
        assert len(wire.calls) == 1
        assert policy.events == []
    else:
        response = await _drive(layer, policy, stream=stream)
        assert response.text == "recovered"
        assert len(wire.calls) == 2
        assert len(policy.events) == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_hosted_work_on_a_provider_failure_still_polls_a_live_token(stream: bool) -> None:
    token = {"response_id": "pending-hosted"}
    if stream:
        pending: Any = [_silent_update(continuation_token=token)]
        terminal: Any = [_text_update("terminal")]
    else:
        pending = _text_response("not-terminal", continuation_token=token)
        terminal = _text_response("terminal")
    wire = _ScriptedWire([pending, _failed_after_hosted_work(invalidates=False), terminal])
    layer, policy = _validated_layer(wire)

    response = await _drive(layer, policy, stream=stream)

    # Polling resumes the response that ran the work; it never re-creates it.
    assert response.text == "terminal"
    assert [call["options"].get("continuation_token") for call in wire.calls] == [None, token, token]
    assert len(policy.events) == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_an_invalidated_token_with_hosted_work_never_re_creates_the_request(stream: bool) -> None:
    token = {"response_id": "pending-hosted"}
    pending: Any = (
        [_text_update("not-terminal", continuation_token=token)]
        if stream
        else _text_response("not-terminal", continuation_token=token)
    )
    unused: Any = [_text_update("must stay unused")] if stream else _text_response("must stay unused")
    wire = _ScriptedWire([pending, _failed_after_hosted_work(invalidates=True), unused])
    layer, policy = _validated_layer(wire)

    with pytest.raises(ProviderResponseError, match="the response failed"):
        await _drive(layer, policy, stream=stream)

    assert len(wire.calls) == 2
    assert policy.events == []


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_hosted_work_a_poll_showed_still_vetoes_a_later_poll_that_ends_the_response(stream: bool) -> None:
    # One poll reports the hosted work and keeps the token; the next poll
    # ends the response without repeating it. The work belongs to the
    # response, not to the poll that reported it.
    token = {"response_id": "pending-hosted"}
    pending: Any = (
        [_silent_update(continuation_token=token)]
        if stream
        else _text_response("not-terminal", continuation_token=token)
    )
    ended = ProviderResponseError(
        "server_error", "the response ended", retryable=True, invalidates_continuation_token=True
    )
    unused: Any = [_text_update("must stay unused")] if stream else _text_response("must stay unused")
    wire = _ScriptedWire([pending, _failed_after_hosted_work(invalidates=False), ended, unused])
    layer, policy = _validated_layer(wire)

    with pytest.raises(ProviderResponseError, match="the response ended"):
        await _drive(layer, policy, stream=stream)

    assert [call["options"].get("continuation_token") for call in wire.calls] == [None, token, token]
    assert len(policy.events) == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_hosted_work_of_an_earlier_response_does_not_veto_a_new_request(stream: bool) -> None:
    # The first response ran hosted work and finished; the tool loop's next
    # request starts another response, whose failure may be retried.
    hosted = [
        Content.from_mcp_server_tool_call("mc1", "create_issue", server_name="github"),
        Content.from_mcp_server_tool_result("mc1", output=[Content.from_text("created #42")]),
    ]
    call = Content.from_function_call("c1", "echo", arguments={"text": "x"})
    first: Any = (
        [ChatResponseUpdate(role="assistant", contents=[*hosted, call])]
        if stream
        else ChatResponse(messages=[Message("assistant", [*hosted, call])])
    )
    final: Any = [_text_update("done")] if stream else _text_response("done")
    failure = ProviderResponseError("server_error", "dropped", retryable=True)
    wire = _ScriptedWire([first, failure, final])
    layer, policy = _validated_layer(wire)

    runs: list[str] = []
    result = layer.get_response(
        [_user()], stream=stream, options={"tools": [_echo_tool(runs)]}, client_kwargs={"wire_retry_policy": policy}
    )
    if stream:
        assert isinstance(result, ResponseStream)
        response = await result.get_final_response()
    else:
        response = await result

    assert response.text == "done"
    assert runs == ["x"]
    assert len(wire.calls) == 3
    assert len(policy.events) == 1


@pytest.mark.asyncio
async def test_response_stream_close_is_recursive_idempotent_and_skips_finalization() -> None:
    cleanups = 0
    finalized = 0
    transport_closed = 0

    async def _updates():
        nonlocal transport_closed
        try:
            await asyncio.Event().wait()
            yield _text_update("unreachable")
        finally:
            transport_closed += 1

    def _finalize(updates: list[ChatResponseUpdate]) -> ChatResponse:
        nonlocal finalized
        finalized += 1
        return ChatResponse.from_updates(updates)

    async def _cleanup() -> None:
        nonlocal cleanups
        cleanups += 1

    inner = ResponseStream(_updates(), finalizer=_finalize)
    inner.with_cleanup_hook(_cleanup)

    async def _resolve() -> ResponseStream[ChatResponseUpdate, ChatResponse]:
        return inner

    outer = ResponseStream.from_awaitable(_resolve())
    outer.with_cleanup_hook(_cleanup)
    pull = asyncio.create_task(outer.__anext__())
    await asyncio.sleep(0)
    pull.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pull
    await outer.aclose()

    assert transport_closed == 1
    assert cleanups == 2
    assert finalized == 0


def test_storage_resolver_strips_all_handles_but_keeps_continuation_token() -> None:
    from chrys.kernel import resolve_storage_mode_and_handles

    options = {
        "store": True,
        "conversation_id": "top",
        "continuation_token": {"response_id": "pending"},
        "extra_body": {
            "previous_response_id": "nested",
            "conversation": "conv",
            "continuation_token": "nested-pending",
        },
    }
    client_kwargs = {
        "previous_response_id": "kwarg",
        "extra_body": {"conversation_id": "kwarg-nested"},
    }

    resolved = resolve_storage_mode_and_handles(
        options,
        stores_by_default=False,
        client_kwargs=client_kwargs,
    )

    assert resolved.service_side is True
    assert resolved.handles_present is True
    assert resolved.options_without_handles == {
        "store": True,
        "continuation_token": {"response_id": "pending"},
        "extra_body": {"continuation_token": "nested-pending"},
    }
    assert resolved.client_kwargs_without_handles == {"extra_body": {}}
    assert options["conversation_id"] == "top"
    assert client_kwargs["previous_response_id"] == "kwarg"


def test_storage_resolver_follows_wire_truth_and_kwargs_only_escalate() -> None:
    from chrys.kernel import resolve_storage_mode_and_handles

    # options are the wire truth: no current client reads ``store`` from
    # kwargs, so a kwargs-side store=False must not reclassify a stored
    # request as local — in-place re-creates would duplicate hosted work.
    stored = resolve_storage_mode_and_handles(
        {"store": True},
        stores_by_default=False,
        client_kwargs={"store": False},
    )
    assert stored.service_side is True

    # Within options, extra_body overrides the named parameter — matching
    # how providers merge the request body.
    local = resolve_storage_mode_and_handles(
        {"store": True, "extra_body": {"store": False}},
        stores_by_default=False,
    )
    assert local.service_side is False

    # A kwargs-side store may escalate to service-side: over-conservative
    # retry semantics are safe, forced-local ones are not.
    escalated = resolve_storage_mode_and_handles(
        {"store": False},
        stores_by_default=False,
        client_kwargs={"extra_body": {"store": True}},
    )
    assert escalated.service_side is True

    # A *present* ``extra_body`` null still overrides the named parameter:
    # the merged body carries ``"store": null`` and the provider applies
    # its storage default, so the resolver must too — never fall through
    # to the named value.
    null_over_false = resolve_storage_mode_and_handles(
        {"store": False, "extra_body": {"store": None}},
        stores_by_default=True,
    )
    assert null_over_false.service_side is True

    null_over_true = resolve_storage_mode_and_handles(
        {"store": True, "extra_body": {"store": None}},
        stores_by_default=False,
    )
    assert null_over_true.service_side is False


@pytest.mark.parametrize(
    ("options", "client_kwargs"),
    [
        ({"store": True}, {}),
        ({"extra_body": {"store": True}}, {}),
        ({"store": False}, {"store": True}),
        ({"store": False}, {"extra_body": {"store": True}}),
    ],
)
def test_storage_resolver_force_stateless_vetoes_every_store_injection_point(
    options: dict[str, Any],
    client_kwargs: dict[str, Any],
) -> None:
    from chrys.kernel import resolve_storage_mode_and_handles

    resolved = resolve_storage_mode_and_handles(
        options,
        stores_by_default=True,
        client_kwargs=client_kwargs,
        force_stateless=True,
    )

    assert resolved.service_side is False


def test_storage_resolver_force_stateless_strips_every_stateful_spelling_without_mutation() -> None:
    from chrys.kernel import resolve_storage_mode_and_handles

    options = {
        "store": True,
        "conversation_id": "conv",
        "previous_response_id": "resp",
        "conversation": {"id": "thread"},
        "continuation_token": {"response_id": "pending"},
        "background": True,
        "extra_body": {
            "conversation_id": "nested-conv",
            "previous_response_id": "nested-resp",
            "conversation": {"id": "nested-thread"},
            "continuation_token": "nested-pending",
            "background": True,
            "kept": "value",
        },
    }
    client_kwargs = {
        "store": True,
        "continuation_token": {"response_id": "kwarg-pending"},
        "background": True,
        "extra_body": {"conversation_id": "kwarg-conv", "background": True, "kept": "kwarg"},
    }

    resolved = resolve_storage_mode_and_handles(
        options,
        stores_by_default=True,
        client_kwargs=client_kwargs,
        force_stateless=True,
    )

    assert resolved.service_side is False
    assert resolved.handles_present is True
    assert resolved.options_without_handles == {"store": True, "extra_body": {"kept": "value"}}
    assert resolved.client_kwargs_without_handles == {"store": True, "extra_body": {"kept": "kwarg"}}
    assert options["background"] is True
    assert client_kwargs["continuation_token"] == {"response_id": "kwarg-pending"}


def test_conversation_handle_normalization_and_collection() -> None:
    from chrys.kernel.client import collect_conversation_handles, conversation_handle_value

    # Normalization: direct string, mapping form, and everything foreign.
    assert conversation_handle_value("conv-1") == "conv-1"
    assert conversation_handle_value({"id": "conv-2"}) == "conv-2"
    assert conversation_handle_value("") is None
    assert conversation_handle_value(None) is None
    assert conversation_handle_value(7) is None
    assert conversation_handle_value({"id": 7}) is None
    assert conversation_handle_value({"name": "no-id"}) is None

    # Collection covers every spelling, top-level and nested in extra_body.
    assert collect_conversation_handles(None) == []
    assert collect_conversation_handles({}) == []
    assert collect_conversation_handles({"conversation_id": "a"}) == ["a"]
    assert collect_conversation_handles({"previous_response_id": "b"}) == ["b"]
    assert collect_conversation_handles({"conversation": {"id": "c"}}) == ["c"]
    assert collect_conversation_handles(
        {
            "conversation_id": "top",
            "extra_body": {"previous_response_id": "nested", "conversation": "conv"},
        }
    ) == ["top", "nested", "conv"]
    # A non-mapping extra_body is foreign state, not a handle source.
    assert collect_conversation_handles({"extra_body": ["not-a-mapping"]}) == []


def test_continuation_token_participates_in_handle_collection() -> None:
    from chrys.kernel.client import collect_conversation_handles, continuation_token_response_id

    # Normalization: only the response_id-shaped token is interpretable.
    assert continuation_token_response_id({"response_id": "bg-1"}) == "bg-1"
    assert continuation_token_response_id({"response_id": ""}) is None
    assert continuation_token_response_id({"response_id": 7}) is None
    assert continuation_token_response_id({"other": "opaque"}) is None
    assert continuation_token_response_id("bg-1") is None
    assert continuation_token_response_id(None) is None

    # The token's response id is continuation state like previous_response_id.
    assert collect_conversation_handles({"continuation_token": {"response_id": "bg-1"}}) == ["bg-1"]
    assert collect_conversation_handles({"conversation_id": "top", "continuation_token": {"response_id": "bg-1"}}) == [
        "top",
        "bg-1",
    ]
    # Only the top-level token participates: a nested extra_body copy never
    # reaches the retrieve short-circuit.
    assert collect_conversation_handles({"extra_body": {"continuation_token": {"response_id": "bg-1"}}}) == []


@pytest.mark.asyncio
async def test_service_storage_ignores_kernel_policy_on_first_request() -> None:
    policy = _Policy()
    wire = _ScriptedWire([ConnectionError("outer owner")])
    wire.STORES_BY_DEFAULT = True
    layer = _layer(wire)
    session = AgentSession(service_session_id="service-id")

    with pytest.raises(ConnectionError, match="outer owner"):
        await layer.get_response(
            [_user()],
            options={"extra_body": {"store": True}},
            client_kwargs={"session": session, "wire_retry_policy": policy},
        )

    assert len(wire.calls) == 1
    assert policy.events == []


def _injected_texts(call: dict[str, Any]) -> list[str]:
    return [
        message.text
        for message in call["messages"]
        if message.additional_properties.get(HistoryMarkerKind.INJECTED_KEY) is True
    ]


@pytest.mark.asyncio
async def test_failed_call_injection_replays_once_and_persists_once() -> None:
    injection = InjectionMiddleware()
    persisted: dict[str, str] = {}

    async def _persist(consumed: Any) -> None:
        persisted.setdefault(consumed.consumption_id, consumed.text)

    injection.set_on_consumed(_persist)
    injection.queue("retry-note", injection_id="note-1")
    policy = _Policy(before_retry_hook=injection.restore_for_retry)
    probe = ConsumedInjectionMessageProbe(
        injection.drain_consumed_injection_messages,
        injection.commit_logical_call,
    )
    wire = _ScriptedWire([ConnectionError("retry"), _text_response("done")])
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire, middleware=[injection]))

    injection.begin_retry()
    try:
        response = await layer.get_response(
            [_user()],
            client_kwargs={
                "wire_retry_policy": policy,
                "consumed_injection_message_probe": probe,
            },
        )
    finally:
        injection.end_retry()

    assert response.text == "done"
    assert [_injected_texts(call) for call in wire.calls] == [["retry-note"], ["retry-note"]]
    assert persisted == {"note-1": "retry-note"}


@pytest.mark.asyncio
async def test_successful_call_injection_is_not_reanchored_on_later_retry() -> None:
    injection = InjectionMiddleware()
    injection.queue("first-call-only", injection_id="first")
    policy = _Policy(before_retry_hook=injection.restore_for_retry)
    probe = ConsumedInjectionMessageProbe(
        injection.drain_consumed_injection_messages,
        injection.commit_logical_call,
    )
    runs: list[str] = []
    wire = _ScriptedWire(
        [
            _call_response("c1", "echo", text="once"),
            ConnectionError("retry second call"),
            _text_response("done"),
        ]
    )
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire, middleware=[injection]))

    injection.begin_retry()
    try:
        await layer.get_response(
            [_user()],
            options={"tools": [_echo_tool(runs)]},
            client_kwargs={
                "wire_retry_policy": policy,
                "consumed_injection_message_probe": probe,
            },
        )
    finally:
        injection.end_retry()

    assert runs == ["once"]
    assert [_injected_texts(call) for call in wire.calls] == [
        ["first-call-only"],
        ["first-call-only"],
        ["first-call-only"],
    ]


@pytest.mark.asyncio
async def test_backoff_queued_injection_joins_retry_while_cancelled_one_stays_absent() -> None:
    injection = InjectionMiddleware()
    injection.queue("original", injection_id="original")

    def _queue_during_backoff() -> None:
        injection.queue("joined", injection_id="joined")
        injection.queue("cancelled", injection_id="cancelled")
        assert injection.cancel("cancelled") is not None

    policy = _Policy(
        before_retry_hook=injection.restore_for_retry,
        sleep_hook=_queue_during_backoff,
    )
    probe = ConsumedInjectionMessageProbe(
        injection.drain_consumed_injection_messages,
        injection.commit_logical_call,
    )
    wire = _ScriptedWire([ConnectionError("retry"), _text_response("done")])
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire, middleware=[injection]))

    injection.begin_retry()
    try:
        await layer.get_response(
            [_user()],
            client_kwargs={
                "wire_retry_policy": policy,
                "consumed_injection_message_probe": probe,
            },
        )
    finally:
        injection.end_retry()

    assert _injected_texts(wire.calls[0]) == ["original"]
    assert _injected_texts(wire.calls[1]) == ["original", "joined"]


@pytest.mark.asyncio
async def test_retry_replays_an_injection_with_the_reminders_it_carried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A replayed injection keeps its reminder even when a later injection becomes the last user message."""
    injection = InjectionMiddleware()
    reminder = SystemReminderMiddleware(runtime=MagicMock())
    monkeypatch.setattr(reminder.sources.runtime_env, "snapshot", lambda: "runtime")
    monkeypatch.setattr(reminder.sources.turn_line, "clock", lambda: "clock")
    reminder.prepare_turn()
    reminder.queue_hook_reminders(["hook note"])
    opener = _user()
    # The opener carried the turn line and the runtime environment on an earlier call.
    opener.additional_properties[HistoryMarkerKind.SYSTEM_REMINDERS_KEY] = [
        {"kind": "turn", "text": "clock"},
        {"kind": "catalog", "text": "runtime", "name": "runtime"},
    ]
    injection.queue("first note", injection_id="first")
    policy = _Policy(
        before_retry_hook=injection.restore_for_retry,
        sleep_hook=lambda: injection.queue("second note", injection_id="second"),
    )
    probe = ConsumedInjectionMessageProbe(
        injection.drain_consumed_injection_messages,
        injection.commit_logical_call,
    )
    wire = _ScriptedWire([ConnectionError("retry"), _text_response("done")])
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire, middleware=[injection, reminder]))

    injection.begin_retry()
    try:
        await layer.get_response(
            [opener],
            client_kwargs={
                "wire_retry_policy": policy,
                "consumed_injection_message_probe": probe,
            },
        )
    finally:
        injection.end_retry()

    def _injected_blocks(call: dict[str, Any]) -> list[list[str | None]]:
        return [
            [content.text for content in message.contents]
            for message in call["messages"]
            if message.additional_properties.get(HistoryMarkerKind.INJECTED_KEY) is True
        ]

    carried = ["first note", _wrap("hook note")]
    assert _injected_blocks(wire.calls[0]) == [carried]
    assert _injected_blocks(wire.calls[1]) == [carried, ["second note"]]


@pytest.mark.asyncio
async def test_abandoned_close_never_finalizes_partial_updates_via_cleanup_hooks() -> None:
    result_hook_runs = 0
    hook_outcomes: list[str] = []

    async def _updates():
        yield _text_update("partial")
        await asyncio.Event().wait()

    def _after(response: ChatResponse) -> ChatResponse:
        nonlocal result_hook_runs
        result_hook_runs += 1
        return response

    stream = ResponseStream(_updates(), finalizer=ChatResponse.from_updates)
    stream.with_result_hook(_after)

    async def _telemetry_shaped_cleanup() -> None:
        # Mirrors the wire client's cleanup hook: on a non-errored stream it
        # requests the final response, which must refuse on abandonment.
        try:
            await stream.get_final_response()
        except RuntimeError:
            hook_outcomes.append("refused")
        else:
            hook_outcomes.append("finalized")

    stream.with_cleanup_hook(_telemetry_shaped_cleanup)
    assert (await stream.__anext__()).text == "partial"

    await stream.aclose()

    assert hook_outcomes == ["refused"]
    assert result_hook_runs == 0
    with pytest.raises(RuntimeError, match="closed before completion"):
        await stream.get_final_response()


@pytest.mark.asyncio
async def test_abandoned_close_finishes_teardown_when_a_child_close_fails() -> None:
    events: list[str] = []

    class _Iterator:
        def __init__(self) -> None:
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return _text_update("partial")

        async def aclose(self) -> None:
            events.append("iterator")

    iterator = _Iterator()

    class _FailingStream:
        def __aiter__(self):
            return iterator

        async def aclose(self) -> None:
            events.append("stream")
            raise RuntimeError("transport close failed")

    async def _cleanup() -> None:
        events.append("cleanup")

    stream = ResponseStream(_FailingStream(), finalizer=ChatResponse.from_updates, cleanup_hooks=[_cleanup])
    assert (await stream.__anext__()).text == "partial"

    with pytest.raises(RuntimeError, match="transport close failed"):
        await stream.aclose()

    assert events == ["stream", "iterator", "cleanup"]
    await stream.aclose()
    assert events == ["stream", "iterator", "cleanup"]


@pytest.mark.asyncio
async def test_abandoned_close_prioritizes_cancellation_after_an_earlier_close_failure() -> None:
    events: list[str] = []

    class _CancellingIterator:
        def __init__(self) -> None:
            self.sent = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return _text_update("partial")

        async def aclose(self) -> None:
            events.append("iterator")
            raise asyncio.CancelledError

    iterator = _CancellingIterator()

    class _FailingStream:
        def __aiter__(self):
            return iterator

        async def aclose(self) -> None:
            events.append("stream")
            raise RuntimeError("transport close failed")

    async def _cleanup() -> None:
        events.append("cleanup")

    stream = ResponseStream(_FailingStream(), finalizer=ChatResponse.from_updates, cleanup_hooks=[_cleanup])
    assert (await stream.__anext__()).text == "partial"

    with pytest.raises(asyncio.CancelledError):
        await stream.aclose()

    assert events == ["stream", "iterator", "cleanup"]


@pytest.mark.asyncio
async def test_abandoned_close_runs_later_cleanup_hooks_after_a_hook_failure() -> None:
    events: list[str] = []

    async def _updates():
        yield _text_update("unused")

    async def _failing_cleanup() -> None:
        events.append("failing")
        raise RuntimeError("cleanup failed")

    async def _later_cleanup() -> None:
        events.append("later")

    stream = ResponseStream(_updates(), cleanup_hooks=[_failing_cleanup, _later_cleanup])

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await stream.aclose()

    assert events == ["failing", "later"]
    await stream.aclose()
    assert events == ["failing", "later"]


@pytest.mark.asyncio
async def test_cleanup_hooks_prioritize_cancellation_and_still_finish_later_hooks() -> None:
    events: list[str] = []

    async def _updates():
        yield _text_update("unused")

    async def _failing_cleanup() -> None:
        events.append("failing")
        raise RuntimeError("cleanup failed")

    async def _cancelling_cleanup() -> None:
        events.append("cancelling")
        raise asyncio.CancelledError

    async def _later_cleanup() -> None:
        events.append("later")

    stream = ResponseStream(
        _updates(),
        cleanup_hooks=[_failing_cleanup, _cancelling_cleanup, _later_cleanup],
    )

    with pytest.raises(asyncio.CancelledError):
        await stream.aclose()

    assert events == ["failing", "cancelling", "later"]


@pytest.mark.asyncio
async def test_cancel_during_watchdog_wait_unwinds_cleanly_and_closes_transport() -> None:
    transport_closed = 0
    started = asyncio.Event()

    async def _hung_updates():
        nonlocal transport_closed
        try:
            started.set()
            yield _text_update("early")
            await asyncio.Event().wait()
        finally:
            transport_closed += 1

    policy = _Policy(stall_timeout_seconds=30.0)
    hung = ResponseStream(_hung_updates(), finalizer=ChatResponse.from_updates)
    wire = _ScriptedWire([hung])
    layer = _layer(wire)
    stream = layer.get_response(
        [_user()],
        stream=True,
        client_kwargs={"wire_retry_policy": policy},
    )

    async def _consume() -> None:
        async for _ in stream:
            pass

    consumer = asyncio.create_task(_consume())
    await started.wait()
    await asyncio.sleep(0.01)
    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer

    assert transport_closed == 1


def _first_byte_after(prepare: Any) -> ResponseStream[ChatResponseUpdate, ChatResponse]:
    """A stream whose first pull runs *prepare* (compaction's slot) before answering."""

    async def _answer():
        yield _text_update("done")

    async def _resolve() -> ResponseStream[ChatResponseUpdate, ChatResponse]:
        await prepare()
        return ResponseStream(_answer(), finalizer=ChatResponse.from_updates)

    return ResponseStream.from_awaitable(_resolve())


@pytest.mark.asyncio
async def test_stall_watchdog_times_idle_gaps_not_reported_preparation() -> None:
    """Preparation that outlasts the stall timeout but reports progress more
    often than that (a LAST_WORDS side call streaming its note) keeps the pull
    alive instead of being killed and redone on every wire retry."""
    timeout = 1.0

    async def _reporting_preparation() -> None:
        # 1.2 s in total, always past the timeout; each gap leaves 0.9 s of slack.
        for _ in range(12):
            await asyncio.sleep(timeout / 10)
            report_wire_progress()

    policy = _Policy(stall_timeout_seconds=timeout, stall_exhausted_action=StallExhaustedAction.RAISE)
    wire = _ScriptedWire([_first_byte_after(_reporting_preparation)])
    stream = _layer(wire).get_response([_user()], stream=True, client_kwargs={"wire_retry_policy": policy})

    response = await stream.get_final_response()

    assert response.text == "done"
    assert policy.events == []
    assert len(wire.calls) == 1


@pytest.mark.asyncio
async def test_stall_watchdog_still_fires_once_preparation_goes_idle() -> None:
    async def _idle_after_progress() -> None:
        report_wire_progress()
        await asyncio.Event().wait()

    policy = _Policy(stall_timeout_seconds=0.05, stall_exhausted_action=StallExhaustedAction.RAISE)
    wire = _ScriptedWire([_first_byte_after(_idle_after_progress)])
    stream = _layer(wire).get_response([_user()], stream=True, client_kwargs={"wire_retry_policy": policy})

    with pytest.raises(StreamStall):
        await stream.get_final_response()
    assert len(wire.calls) == 1


def test_a_report_reaches_every_enclosing_progress_scope() -> None:
    """A side call's own read timeout, nested in the watchdog of the pull
    waiting on it, never hides its progress from that watchdog."""
    reports: list[str] = []
    with wire_progress_scope(lambda: reports.append("watchdog")):
        with wire_progress_scope(lambda: reports.append("read timeout")):
            report_wire_progress()
        report_wire_progress()
    report_wire_progress()

    assert reports == ["read timeout", "watchdog", "watchdog"]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_learned_continuation_token_survives_failed_poll_for_the_retry_owner(stream: bool) -> None:
    token = {"response_id": "pending-9"}
    observed: list[Any] = []
    if stream:
        pending = [_silent_update(continuation_token=token)]
        terminal = [_text_update("terminal")]
    else:
        pending = _text_response("not-terminal", continuation_token=token)
        terminal = _text_response("terminal")
    policy = _Policy()
    wire = _ScriptedWire([pending, ConnectionError("poll dropped"), terminal])
    layer = _layer(wire)

    result = layer.get_response(
        [_user()],
        stream=stream,
        client_kwargs={
            "wire_retry_policy": policy,
            "continuation_token_observer": observed.append,
        },
    )
    if stream:
        async for _ in result:
            pass
        response = await result.get_final_response()
    else:
        response = await result

    assert response.text == "terminal"
    assert observed == [token, None]
    assert wire.calls[1]["options"]["continuation_token"] == token
    assert wire.calls[2]["options"]["continuation_token"] == token


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_invalid_terminal_completion_clears_token_before_outer_retry(stream: bool) -> None:
    from chrys.service.agent_middleware.response_validation import TerminalResponseValidationError

    token = {"response_id": "pending-invalid"}
    observed: list[Any] = []
    if stream:
        pending = [_text_update("not-terminal", continuation_token=token)]
        invalid_terminal = [ChatResponseUpdate(contents=[], role="assistant")]
    else:
        pending = _text_response("not-terminal", continuation_token=token)
        invalid_terminal = ChatResponse(messages=[Message("assistant", [])])
    wire = _ScriptedWire([pending, invalid_terminal])
    layer = _layer(wire, validation=True)

    result = layer.get_response(
        [_user()],
        stream=stream,
        options={"store": True},
        client_kwargs={"continuation_token_observer": observed.append},
    )
    with pytest.raises(TerminalResponseValidationError):
        if stream:
            async for _ in result:
                pass
        else:
            await result

    # The completed response is immutable: a later turn must issue a
    # fresh request instead of re-polling the same invalid completion.
    assert observed == [token, None]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_wrapped_invalidating_error_clears_token_before_retry(stream: bool) -> None:
    token = {"response_id": "pending-wrapped"}
    observed: list[Any] = []
    if stream:
        pending = [_text_update("not-terminal", continuation_token=token)]
    else:
        pending = _text_response("not-terminal", continuation_token=token)
    inner = ProviderResponseError(
        "stream_truncated", "stream ended early", retryable=True, invalidates_continuation_token=True
    )
    try:
        raise ChatClientException("Chat Completions request failed", inner_exception=inner) from inner
    except ChatClientException as exc:
        wrapped = exc
    wire = _ScriptedWire([pending, wrapped])

    result = _layer(wire).get_response(
        [_user()], stream=stream, client_kwargs={"continuation_token_observer": observed.append}
    )
    with pytest.raises(ChatClientException):
        if stream:
            async for _ in result:
                pass
        else:
            await result

    # The flag sits on the wrapped provider error, not on the wrapper.
    assert observed == [token, None]


@pytest.mark.asyncio
async def test_mid_stream_token_update_reaches_retry_owner_before_disconnect() -> None:
    token = {"response_id": "bg-announced"}
    observed: list[Any] = []

    async def _dying_updates():
        yield ChatResponseUpdate(contents=[], role="assistant", continuation_token=token)
        raise ConnectionError("stream dropped")

    dying = ResponseStream(_dying_updates(), finalizer=ChatResponse.from_updates)
    wire = _ScriptedWire([dying])
    layer = _layer(wire, validation=True)

    result = layer.get_response(
        [_user()],
        stream=True,
        options={"store": True},
        client_kwargs={"continuation_token_observer": observed.append},
    )
    with pytest.raises(ConnectionError, match="stream dropped"):
        async for _ in result:
            pass

    # The retry owner learned the announced response id before the
    # disconnect: the whole-run retry retrieves it instead of re-creating.
    assert observed == [token]


@pytest.mark.asyncio
async def test_continuation_poll_does_not_consume_injections() -> None:
    injection = InjectionMiddleware()
    token = {"response_id": "bg-running"}

    def _pending_and_queue():
        injection.queue("mid-poll note", injection_id="mid")
        return _text_response("not-terminal", continuation_token=token)

    wire = _ScriptedWire([_pending_and_queue, _text_response("terminal")])
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire, middleware=[injection]))
    probe = ConsumedInjectionMessageProbe(
        injection.drain_consumed_injection_messages,
        injection.commit_logical_call,
    )

    response = await layer.get_response(
        [_user()],
        client_kwargs={"consumed_injection_message_probe": probe},
    )

    assert response.text == "terminal"
    # The poll retrieves an existing response; the provider never sees
    # request messages, so nothing may be recorded as consumed by it.
    assert _injected_texts(wire.calls[1]) == []
    assert [queued.text for queued in injection.drain_pending()] == ["mid-poll note"]


@pytest.mark.asyncio
async def test_continuation_polls_are_paced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(loop_module, "CONTINUATION_POLL_INTERVAL_SECONDS", 0.05)
    token = {"response_id": "bg-slow"}
    wire = _ScriptedWire(
        [
            _text_response("still running", continuation_token=token),
            _text_response("still running", continuation_token=token),
            _text_response("terminal"),
        ]
    )
    layer = _layer(wire)

    start = asyncio.get_running_loop().time()
    response = await layer.get_response([_user()])
    elapsed = asyncio.get_running_loop().time() - start

    assert response.text == "terminal"
    assert len(wire.calls) == 3
    assert elapsed >= 0.1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.asyncio
async def test_preseeded_token_cleared_on_first_terminal_so_tool_results_post_fresh(stream: bool) -> None:
    token = {"response_id": "resumed-after-outer-retry"}
    observed: list[Any] = []
    runs: list[str] = []
    if stream:
        resumed_terminal: Any = [_call_update("c1", "echo", text="resume")]
        post_tool: Any = [_text_update("done")]
    else:
        resumed_terminal = _call_response("c1", "echo", text="resume")
        post_tool = _text_response("done")
    wire = _ScriptedWire([resumed_terminal, post_tool])
    layer = _layer(wire)

    result = layer.get_response(
        [_user()],
        stream=stream,
        options={"tools": [_echo_tool(runs)], "continuation_token": token},
        client_kwargs={"continuation_token_observer": observed.append},
    )
    if stream:
        async for _ in result:
            pass
        response = await result.get_final_response()
    else:
        response = await result

    assert response.text == "done"
    assert runs == ["resume"]
    # The resumed retrieval was terminal; even though this logical call
    # never ANNOUNCED the token itself, it must be cleared — otherwise the
    # post-tool request retrieves the same completed response and the tool
    # executes twice.
    assert wire.calls[0]["options"]["continuation_token"] == token
    assert "continuation_token" not in wire.calls[1]["options"]
    assert observed == [None]


@pytest.mark.asyncio
async def test_poll_retry_preserves_consumed_injection_for_the_next_create() -> None:
    injection = InjectionMiddleware()
    injection.queue("pre-create note", injection_id="pre")
    token = {"response_id": "bg-injected"}
    runs: list[str] = []
    policy = _Policy(before_retry_hook=injection.restore_for_retry)
    wire = _ScriptedWire(
        [
            _text_response("accepted", continuation_token=token),
            ConnectionError("poll dropped"),
            _call_response("c1", "echo", text="from-background"),
            _text_response("done"),
        ]
    )
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire, middleware=[injection]))
    probe = ConsumedInjectionMessageProbe(
        injection.drain_consumed_injection_messages,
        injection.commit_logical_call,
    )

    response = await layer.get_response(
        [_user()],
        options={"tools": [_echo_tool(runs)]},
        client_kwargs={
            "wire_retry_policy": policy,
            "consumed_injection_message_probe": probe,
        },
    )

    assert response.text == "done"
    assert runs == ["from-background"]
    # The create (call 0) delivered the injection to the provider; the poll
    # retry resumes that same response, so no replay may run — a poll skips
    # consumption, and the terminal commit would then destroy the stranded
    # batch and its retained-message mirror.
    assert policy.before_retry_calls == 0
    assert _injected_texts(wire.calls[0]) == ["pre-create note"]
    assert _injected_texts(wire.calls[1]) == []
    assert _injected_texts(wire.calls[2]) == []
    # The post-tool create still carries the injection in loop history.
    assert _injected_texts(wire.calls[3]) == ["pre-create note"]
