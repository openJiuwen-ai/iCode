# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The loop's model cycle is closed on every exit, interrupts included.

A cycle whose start marker landed and whose terminal one never did reads as a
cycle still running, so the abort path has to keep its handle until the
terminal is on its way.
"""

from __future__ import annotations

import asyncio
import errno
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, NoReturn

import pytest

from chrys.foundation.errors.kinds import ErrorKind
from chrys.foundation.errors.provider import ProviderResponseError
from chrys.foundation.trajectory.context import TRAJECTORY_CONTEXT_KWARG
from chrys.foundation.trajectory.envelope import MeasurementSource
from chrys.foundation.trajectory.event_types import EventType, ExchangeOutcome, ToolOutcome
from chrys.kernel import ChatMiddlewareLayer, ChatResponse, Content, Message, tool
from chrys.kernel import loop as loop_module
from chrys.kernel.middleware import FunctionInvocationContext, FunctionMiddleware
from chrys.service.llm.observer import WireCallObserver
from chrys.service.llm.wire_client import WireClient
from tests.service.trajectory._fakes import CancelAckSink, FakeSink, make_context
from tests.support.transcript_invariants import InvariantCheckedToolLoopLayer


class _HostedCallClient:
    """Client whose single blocking response carries one provider-hosted call, then ``tail``."""

    def __init__(self, tail: Content) -> None:
        self._tail = tail

    def get_response(
        self,
        messages: Sequence[Message],
        *,
        stream: bool,
        options: Mapping[str, Any] | None,
        function_invocation_kwargs: Mapping[str, Any] | None,
        compaction_strategy: object,
        tokenizer: object,
        client_kwargs: Mapping[str, Any] | None,
    ) -> Awaitable[ChatResponse]:
        del messages, options, function_invocation_kwargs, compaction_strategy, tokenizer, client_kwargs
        assert stream is False

        async def _resolve() -> ChatResponse:
            return ChatResponse(
                messages=[
                    Message(
                        "assistant",
                        [
                            Content.from_hosted_tool_call(
                                "hosted-1",
                                tool_name="web_search",
                                status="completed",
                                hosted_family="search",
                                hosted_provider="openai",
                            ),
                            self._tail,
                        ],
                    )
                ]
            )

        return _resolve()


@pytest.mark.asyncio
async def test_a_cycle_interrupted_while_recording_hosted_calls_is_still_closed() -> None:
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(_HostedCallClient(Content.from_text("done"))))
    # 1 = the cycle's start marker, 2 = the hosted call the response carried
    # (a client that is not a wire client reports no exchange of its own).
    sink = CancelAckSink(at=2)
    context = make_context(sink)

    with pytest.raises(asyncio.CancelledError):
        await layer.get_response(
            [Message("user", ["hi"])],
            client_kwargs={TRAJECTORY_CONTEXT_KWARG: context},
        )

    assert sink.drafts[1].event_type == EventType.HOSTED_CALL_OBSERVED, "the cancel must land on the hosted call"
    started = sink.only(EventType.MODEL_CYCLE_STARTED)
    finished = sink.only(EventType.MODEL_CYCLE_FINISHED)
    assert finished.operation_id == started.operation_id


@pytest.mark.asyncio
async def test_a_landed_call_is_settled_when_the_hosted_call_records_are_interrupted() -> None:
    """Closing the cycle hands the landed call's operation to the loop before
    the hosted-call records await, so an interrupt there still settles it."""

    @tool(name="echo")
    async def echo(text: str) -> str:
        return f"echo:{text}"

    layer = InvariantCheckedToolLoopLayer(
        ChatMiddlewareLayer(_HostedCallClient(Content.from_function_call("c1", "echo", arguments={"text": "x"})))
    )
    # 1 = the cycle's start marker, 2 = the hosted call the response carried
    # (a client that is not a wire client reports no exchange of its own).
    sink = CancelAckSink(at=2)

    with pytest.raises(asyncio.CancelledError):
        await layer.get_response(
            [Message("user", ["hi"])],
            options={"tools": [echo]},
            client_kwargs={TRAJECTORY_CONTEXT_KWARG: make_context(sink)},
        )

    assert sink.drafts[1].event_type == EventType.HOSTED_CALL_OBSERVED, "the cancel must land on the hosted call"
    assert sink.only(EventType.TOOL_OPERATION_FINISHED).payload["outcome"] == ToolOutcome.FILTERED
    sink.assert_operations_settled()


class _ReportingWireClient(WireClient):
    """The real preparation path: compaction runs before the request is sent,
    and the production observer reports the exchange start just before sending it."""

    OTEL_PROVIDER_NAME = "test"

    def __init__(self) -> None:
        super().__init__(observer=WireCallObserver())

    def _send(
        self, *, messages: Sequence[Message], options: Mapping[str, Any], **kwargs: Any
    ) -> Awaitable[ChatResponse]:
        del messages, options, kwargs

        async def _resolve() -> ChatResponse:
            return ChatResponse(messages=[Message("assistant", ["hi"])])

        return _resolve()

    def _open_stream(self, *, messages: Sequence[Message], options: Mapping[str, Any], **kwargs: Any) -> NoReturn:
        raise NotImplementedError("these tests send blocking requests")


class _SuspendingCompaction:
    """A compaction pass that awaits, the way a summarization side call does."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def __call__(self, messages: object, context: object = None) -> None:
        del messages, context
        self.entered.set()
        await asyncio.Event().wait()


class _FailingWireClient(WireClient):
    """The same preparation path, with a provider that refuses the connection."""

    OTEL_PROVIDER_NAME = "test"

    def __init__(self) -> None:
        super().__init__(observer=WireCallObserver())

    def _send(
        self, *, messages: Sequence[Message], options: Mapping[str, Any], **kwargs: Any
    ) -> Awaitable[ChatResponse]:
        del messages, options, kwargs

        async def _resolve() -> ChatResponse:
            raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")

        return _resolve()

    def _open_stream(self, *, messages: Sequence[Message], options: Mapping[str, Any], **kwargs: Any) -> NoReturn:
        raise NotImplementedError("these tests send blocking requests")


@pytest.mark.asyncio
async def test_a_failed_exchange_records_what_the_error_was() -> None:
    """The wire attempt that dies is closed with an error kind, not just a type name."""
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(_FailingWireClient()))
    sink = FakeSink()

    with pytest.raises(ConnectionRefusedError):
        await layer.get_response(
            [Message("user", ["hi"])],
            client_kwargs={TRAJECTORY_CONTEXT_KWARG: make_context(sink)},
        )

    finished = sink.only(EventType.MODEL_EXCHANGE_FINISHED)
    assert finished.payload["outcome"] == ExchangeOutcome.ERROR
    assert finished.payload["error_code"] == "ConnectionRefusedError"
    assert finished.payload["error_kind"] == ErrorKind.CONNECTION_REFUSED


class _FailedResponseWireClient(_FailingWireClient):
    """A provider that answered with a failure, and reported what the attempt spent."""

    def _send(
        self, *, messages: Sequence[Message], options: Mapping[str, Any], **kwargs: Any
    ) -> Awaitable[ChatResponse]:
        del messages, options, kwargs

        async def _resolve() -> ChatResponse:
            raise ProviderResponseError(
                "server_error",
                "failed midway",
                retryable=False,
                usage_details={"input_token_count": 100, "cache_read_input_token_count": 40, "output_token_count": 7},
            )

        return _resolve()


@pytest.mark.asyncio
async def test_a_failed_response_keeps_the_tokens_it_spent_on_its_exchange() -> None:
    """The session totals count a failed response's usage; the exchange must say the same."""
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(_FailedResponseWireClient()))
    sink = FakeSink()

    with pytest.raises(ProviderResponseError):
        await layer.get_response(
            [Message("user", ["hi"])],
            client_kwargs={TRAJECTORY_CONTEXT_KWARG: make_context(sink)},
        )

    finished = sink.only(EventType.MODEL_EXCHANGE_FINISHED)
    assert finished.payload["outcome"] == ExchangeOutcome.ERROR
    normalized = finished.payload["usage"]["normalized"]
    assert (normalized["input_total"], normalized["cache_read"], normalized["output_total"]) == (100, 40, 7)
    assert finished.measurements["/payload/usage/normalized/input_total"]["source"] == MeasurementSource.PROVIDER
    assert finished.measurements["/payload/usage/normalized/output_total"]["source"] == MeasurementSource.PROVIDER


@pytest.mark.asyncio
async def test_a_failure_with_no_usage_reports_none_on_its_exchange() -> None:
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(_FailingWireClient()))
    sink = FakeSink()

    with pytest.raises(ConnectionRefusedError):
        await layer.get_response(
            [Message("user", ["hi"])],
            client_kwargs={TRAJECTORY_CONTEXT_KWARG: make_context(sink)},
        )

    assert "usage" not in sink.only(EventType.MODEL_EXCHANGE_FINISHED).payload


@pytest.mark.asyncio
async def test_an_interrupt_before_the_request_is_sent_records_no_exchange() -> None:
    """The exchange handle is minted before the call, and the client reports
    the start immediately before sending. An interrupt in between — compaction
    is the window that actually suspends there — ends a trace that never
    reached the provider, so it must leave no terminal behind."""
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(_ReportingWireClient()))
    sink = FakeSink()
    compaction = _SuspendingCompaction()

    task = asyncio.create_task(
        layer.get_response(
            [Message("user", ["hi"])],
            client_kwargs={TRAJECTORY_CONTEXT_KWARG: make_context(sink)},
            compaction_strategy=compaction,
        )
    )
    await asyncio.wait_for(compaction.entered.wait(), timeout=5.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert EventType.MODEL_EXCHANGE_FINISHED not in sink.event_types
    assert EventType.MODEL_EXCHANGE_STARTED not in sink.event_types
    # The cycle around it is still closed: that span did open.
    assert sink.only(EventType.MODEL_CYCLE_FINISHED)


@pytest.mark.asyncio
async def test_a_request_that_is_sent_reports_its_exchange() -> None:
    """Control for the interrupt case: the same client, reached, starts its exchange."""
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(_ReportingWireClient()))
    sink = FakeSink()

    await layer.get_response(
        [Message("user", ["hi"])],
        client_kwargs={TRAJECTORY_CONTEXT_KWARG: make_context(sink)},
    )

    assert sink.only(EventType.MODEL_EXCHANGE_STARTED)


class _AlwaysCallsClient:
    """A provider that answers every request with a function call.

    Including the exhaustion tail's, where ``tool_choice="none"`` asked it
    not to.
    """

    def __init__(self) -> None:
        self.calls = 0

    def get_response(self, messages: object, **kwargs: object) -> object:
        del messages, kwargs
        self.calls += 1
        call_id = f"c{self.calls}"

        async def _resolve() -> ChatResponse:
            return ChatResponse(
                messages=[Message("assistant", [Content.from_function_call(call_id, "echo", arguments={"text": "x"})])]
            )

        return _resolve()


@pytest.mark.asyncio
async def test_the_exhaustion_tail_counts_the_calls_it_refused() -> None:
    """A tail reporting no calls would read as a provider that complied."""

    @tool(name="echo")
    async def echo(text: str) -> str:
        return f"echo:{text}"

    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(_AlwaysCallsClient()), max_iterations=1)
    sink = FakeSink()

    await layer.get_response(
        [Message("user", ["hi"])],
        options={"tools": [echo]},
        client_kwargs={TRAJECTORY_CONTEXT_KWARG: make_context(sink)},
    )

    # The exhausted cycle, then the tail that asked for text and got a call.
    counts = [event.payload["function_call_count"] for event in sink.of_type(EventType.MODEL_CYCLE_FINISHED)]
    assert counts == [1, 1]


class _TwoCallClient:
    """Wire client whose single response asks for two calls in one batch."""

    def get_response(self, messages: object, **kwargs: object) -> object:
        del messages, kwargs

        async def _resolve() -> ChatResponse:
            return ChatResponse(
                messages=[
                    Message(
                        "assistant",
                        [
                            Content.from_function_call("c1", "echo", arguments={"text": "a"}),
                            Content.from_function_call("c2", "echo", arguments={"text": "b"}),
                        ],
                    )
                ]
            )

        return _resolve()


@pytest.mark.asyncio
async def test_a_sibling_cancelled_before_it_ever_ran_still_settles_its_operation() -> None:
    """A task cancelled before its first step runs no line of its own, so the
    loop has to still own that operation when reconciliation comes round."""

    @tool(name="echo")
    async def echo(text: str) -> str:
        return f"echo:{text}"

    outer: asyncio.Task[object] | None = None

    class _CancelTheRun(FunctionMiddleware):
        async def process(
            self,
            context: FunctionInvocationContext,
            call_next: Callable[[], Awaitable[None]],
        ) -> None:
            del context
            # Synchronous, inside the first sibling's very first step: the
            # second sibling's task is queued and has not run yet, so it is
            # cancelled before its body executes at all.
            assert outer is not None
            outer.cancel()
            await call_next()

    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(_TwoCallClient()))
    sink = FakeSink()
    outer = asyncio.create_task(
        layer.get_response(
            [Message("user", ["hi"])],
            options={"tools": [echo]},
            middleware=[_CancelTheRun()],
            client_kwargs={TRAJECTORY_CONTEXT_KWARG: make_context(sink)},
        )
    )
    with pytest.raises(asyncio.CancelledError):
        await outer

    assert sink.only(EventType.TOOL_OPERATION_FINISHED).payload["outcome"] == ToolOutcome.FILTERED
    sink.assert_operations_settled()


class _UnnamedCallClient:
    """Wire client whose response carries a tool call the provider left unnamed."""

    def get_response(self, messages: object, **kwargs: object) -> object:
        del messages, kwargs

        async def _resolve() -> ChatResponse:
            return ChatResponse(
                messages=[Message("assistant", [Content.from_function_call(None, "echo", arguments={"text": "x"})])]
            )

        return _resolve()


@pytest.mark.asyncio
async def test_a_call_the_provider_left_unnamed_still_settles_its_operation() -> None:
    """Landing mints the operation and the batch counts it dispatched, so the
    refusal at the pipeline door is what owes it a terminal."""

    @tool(name="echo")
    async def echo(text: str) -> str:
        return f"echo:{text}"

    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(_UnnamedCallClient()))
    sink = FakeSink()

    with pytest.raises(KeyError, match="missing call_id"):
        await layer.get_response(
            [Message("user", ["hi"])],
            options={"tools": [echo]},
            client_kwargs={TRAJECTORY_CONTEXT_KWARG: make_context(sink)},
        )

    assert sink.only(EventType.TOOL_OPERATION_FINISHED).payload["outcome"] == ToolOutcome.FILTERED
    sink.assert_operations_settled()


@pytest.mark.asyncio
async def test_a_wall_clock_jump_does_not_stretch_the_cycle_it_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    """Durations come from the monotonic clock, which no clock correction moves."""
    hour_ns = 3_600_000_000_000
    calls = 0

    def jumping_time_ns() -> int:
        nonlocal calls
        calls += 1
        # The step lands between the two reads a duration would have used.
        return 1_700_000_000_000_000_000 + (hour_ns if calls > 1 else 0)

    # The loop module imports no wall clock today; ``raising=False`` still
    # catches one imported later to time the cycle.
    monkeypatch.setattr(loop_module, "time_ns", jumping_time_ns, raising=False)

    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(_HostedCallClient(Content.from_text("done"))))
    sink = FakeSink()

    await layer.get_response(
        [Message("user", ["hi"])],
        client_kwargs={TRAJECTORY_CONTEXT_KWARG: make_context(sink)},
    )

    finished = sink.only(EventType.MODEL_CYCLE_FINISHED)
    assert finished.payload["duration_ms"] < 60_000  # an hour of skew, none of it recorded
    assert finished.measurements["/payload/duration_ms"]["source"] == MeasurementSource.MONOTONIC_CLOCK
