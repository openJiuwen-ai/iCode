# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for UsageTrackingMiddleware integration."""

from __future__ import annotations

import pytest

from tests.service.context.compaction._compaction_helpers import _build_single_turn, _make_strategy

# ---------------------------------------------------------------------------
# Integration: middleware calibrates strategy overhead
# ---------------------------------------------------------------------------


async def test_middleware_calibrates_strategy():
    """UsageTrackingMiddleware calls calibrate() on the strategy."""
    from unittest.mock import MagicMock

    from chrys.kernel import ChatResponse
    from chrys.service.context.middleware.usage import UsageTrackingMiddleware

    strategy = _make_strategy(max_context_tokens=2_000_000)
    messages = _build_single_turn(4, result_size=4000)
    await strategy(messages)
    our_estimate = strategy.last_included_tokens

    mw = UsageTrackingMiddleware(
        max_context_tokens=2_000_000,
        compaction_strategy=strategy,
    )

    # First call: middleware calibrates — learns overhead
    overhead = 5000
    api_tokens = our_estimate + overhead
    context = MagicMock()
    context.messages = []
    context.stream = False
    mock_result = MagicMock(spec=ChatResponse)
    mock_result.usage_details = {
        "input_token_count": api_tokens,
        "output_token_count": 5_000,
        "total_token_count": api_tokens + 5_000,
    }
    context.result = mock_result

    async def call_next():
        pass

    await mw.process(context, call_next)

    # First call learns overhead, ratio stays 1.0
    assert strategy.system_overhead_tokens == overhead
    assert strategy.calibration_ratio == 1.0

    # Second call: learns ratio
    mock_result.usage_details = {
        "input_token_count": int((our_estimate + overhead) * 1.1),
        "output_token_count": 5_000,
        "total_token_count": int((our_estimate + overhead) * 1.1) + 5_000,
    }
    await mw.process(context, call_next)
    assert abs(strategy.calibration_ratio - 1.1) < 0.01


@pytest.mark.asyncio
async def test_responses_hosted_usage_keeps_billing_but_uses_local_context_estimate() -> None:
    """Responses providers report hosted execution as aggregate input usage."""
    from unittest.mock import MagicMock

    from chrys.kernel import ChatResponse, Content, Message
    from chrys.service.context.compaction import UnifiedContextStrategy
    from chrys.service.context.middleware.usage import UsageTrackingMiddleware

    strategy = UnifiedContextStrategy(max_context_tokens=350_000)
    strategy._last_included_tokens = 419
    strategy._request_overhead_floor = 19_821
    reported: list[tuple] = []
    middleware = UsageTrackingMiddleware(
        max_context_tokens=350_000,
        on_usage=lambda *args: reported.append(args),
        compaction_strategy=strategy,
        use_local_context_estimate_for_hosted_usage=True,
    )
    response = ChatResponse(
        messages=[
            Message(
                "assistant",
                [
                    Content.from_search_tool_call("ws1", tool_name="web_search", arguments={"query": "docs"}),
                    Content.from_search_tool_result(
                        "ws1", tool_name="web_search", result={"type": "computer_initialize_state"}
                    ),
                    Content.from_text("Answer"),
                ],
            )
        ],
        usage_details={
            "input_token_count": 273_399,
            "output_token_count": 2_187,
            "total_token_count": 275_586,
        },
    )
    context = MagicMock()
    context.stream = False
    context.result = response

    async def call_next() -> None:
        return None

    await middleware.process(context, call_next)

    assert len(reported) == 1
    assert reported[0][:4] == (275_586, 273_399, 2_187, 20_240)
    assert reported[0][7:] == (False, True)
    assert strategy.system_overhead_tokens == 0
    assert not strategy.calibration_initialized


@pytest.mark.asyncio
async def test_anthropic_hosted_usage_keeps_billing_but_uses_provider_context_occupancy() -> None:
    """Anthropic billing remains aggregate while context tracks the retained window."""
    from unittest.mock import MagicMock

    from chrys.kernel import ChatResponse, Content, Message
    from chrys.service.context.manager import ContextManager
    from chrys.service.profiles.models.resolver import default_profile

    profile = default_profile()
    profile.provider = "anthropic"
    reported: list[tuple] = []
    manager = ContextManager(profile, on_usage=lambda *args: reported.append(args))
    strategy = manager.compaction_strategy
    strategy._last_included_tokens = 419
    strategy._request_overhead_floor = 19_821
    context = MagicMock()
    context.stream = False
    context.result = ChatResponse(
        messages=[
            Message(
                "assistant",
                [Content.from_code_interpreter_tool_call(call_id="code_1", hosted_provider="anthropic")],
            )
        ],
        usage_details={
            "input_token_count": 152_357,
            "output_token_count": 1_657,
            "total_token_count": 154_014,
            "context_input_token_count": 41_182,
            "anthropic.cache_creation_input_tokens": 41_173,
            "anthropic.cache_read_input_tokens": 111_175,
        },
    )

    async def call_next() -> None:
        return None

    await manager.usage_middleware.process(context, call_next)

    assert reported[0][:4] == (154_014, 152_357, 1_657, 41_182)
    assert reported[0][6] == 111_175
    assert reported[0][7:] == (False, True)
    assert not strategy.calibration_initialized


@pytest.mark.asyncio
async def test_anthropic_blocking_hosted_usage_uses_provider_context_estimate() -> None:
    """Blocking cache hits recover their retained prefix from aggregate reads."""
    from unittest.mock import MagicMock

    from chrys.kernel import ChatResponse, Content, Message
    from chrys.service.context.manager import ContextManager
    from chrys.service.profiles.models.resolver import default_profile

    profile = default_profile()
    profile.provider = "anthropic"
    reported: list[tuple] = []
    manager = ContextManager(profile, on_usage=lambda *args: reported.append(args))
    strategy = manager.compaction_strategy
    strategy._last_included_tokens = 419
    strategy._request_overhead_floor = 19_390
    context = MagicMock()
    context.stream = False
    context.result = ChatResponse(
        messages=[
            Message(
                "assistant",
                [Content.from_code_interpreter_tool_call(call_id="code_1", hosted_provider="anthropic")],
            )
        ],
        usage_details={
            "input_token_count": 151_408,
            "output_token_count": 1_539,
            "total_token_count": 152_947,
            "context_input_token_estimate": 40_826,
            "context_input_token_floor": 3_965,
            "anthropic.cache_creation_input_tokens": 3_956,
            "anthropic.cache_read_input_tokens": 147_443,
        },
    )

    async def call_next() -> None:
        return None

    await manager.usage_middleware.process(context, call_next)

    assert reported[0][:4] == (152_947, 151_408, 1_539, 40_826)
    assert reported[0][6] == 147_443
    assert reported[0][7:] == (False, True)
    assert not strategy.calibration_initialized


@pytest.mark.asyncio
async def test_responses_non_hosted_usage_still_calibrates_normally() -> None:
    """The workaround is scoped to responses that actually ran hosted tools."""
    from unittest.mock import MagicMock

    from chrys.kernel import ChatResponse, Content, Message
    from chrys.service.context.compaction import UnifiedContextStrategy
    from chrys.service.context.middleware.usage import UsageTrackingMiddleware

    strategy = UnifiedContextStrategy(max_context_tokens=350_000)
    strategy._last_included_tokens = 400
    reported: list[tuple] = []
    middleware = UsageTrackingMiddleware(
        on_usage=lambda *args: reported.append(args),
        compaction_strategy=strategy,
        use_local_context_estimate_for_hosted_usage=True,
    )
    context = MagicMock()
    context.stream = False
    context.result = ChatResponse(
        messages=[Message("assistant", [Content.from_text("Answer")])],
        usage_details={"input_token_count": 900, "output_token_count": 100, "total_token_count": 1_000},
    )

    async def call_next() -> None:
        return None

    await middleware.process(context, call_next)

    assert strategy.system_overhead_tokens == 500
    assert strategy.calibration_initialized
    assert reported[0][7:] == (True, False)


@pytest.mark.asyncio
async def test_responses_streaming_hosted_usage_uses_finalized_response_shape() -> None:
    """Streaming detection runs after assembly, when hosted items are complete."""
    from unittest.mock import MagicMock

    from chrys.kernel import ChatResponse, ChatResponseUpdate, Content, Message, ResponseStream
    from chrys.service.context.compaction import UnifiedContextStrategy
    from chrys.service.context.middleware.usage import UsageTrackingMiddleware

    strategy = UnifiedContextStrategy(max_context_tokens=350_000)
    strategy._last_included_tokens = 1_000
    strategy._request_overhead_floor = 9_000
    reported: list[tuple] = []
    middleware = UsageTrackingMiddleware(
        on_usage=lambda *args: reported.append(args),
        compaction_strategy=strategy,
        use_local_context_estimate_for_hosted_usage=True,
    )
    usage = {"input_token_count": 80_000, "output_token_count": 500, "total_token_count": 80_500}

    async def updates():
        yield ChatResponseUpdate(
            role="assistant",
            contents=[Content.from_usage(usage_details=usage)],
            model="deepseek",
        )

    def finalize(_updates) -> ChatResponse:
        return ChatResponse(
            messages=[
                Message(
                    "assistant",
                    [Content.from_search_tool_call("ws1", tool_name="web_search", arguments={})],
                )
            ],
            usage_details=usage,
        )

    context = MagicMock()
    context.stream = True
    context.result = ResponseStream(updates(), finalizer=finalize)

    async def call_next() -> None:
        return None

    await middleware.process(context, call_next)
    async for _ in context.result:
        pass
    await context.result.get_final_response()

    assert reported[0][:4] == (80_500, 80_000, 500, 10_000)
    assert reported[0][-1] is True
    assert not strategy.calibration_initialized


@pytest.mark.asyncio
async def test_responses_failed_response_cannot_poison_context_calibration() -> None:
    """A failed response only exposes usage, so the dialect gate is conservative."""
    from unittest.mock import MagicMock

    from chrys.foundation.errors import ProviderResponseError
    from chrys.service.context.compaction import UnifiedContextStrategy
    from chrys.service.context.middleware.usage import UsageTrackingMiddleware

    strategy = UnifiedContextStrategy(max_context_tokens=350_000)
    strategy._last_included_tokens = 1_000
    strategy._request_overhead_floor = 9_000
    reported: list[tuple] = []
    middleware = UsageTrackingMiddleware(
        on_usage=lambda *args: reported.append(args),
        compaction_strategy=strategy,
        use_local_context_estimate_for_hosted_usage=True,
    )
    context = MagicMock()
    context.stream = False

    async def call_next() -> None:
        raise ProviderResponseError(
            "server_error",
            "The model failed.",
            retryable=True,
            usage_details={
                "input_token_count": 80_000,
                "output_token_count": 500,
                "total_token_count": 80_500,
            },
        )

    with pytest.raises(ProviderResponseError):
        await middleware.process(context, call_next)

    assert reported[0][:4] == (80_500, 80_000, 500, 10_000)
    assert reported[0][-1] is True
    assert not strategy.calibration_initialized


@pytest.mark.asyncio
async def test_streaming_responses_failed_response_cannot_poison_context_calibration() -> None:
    """Lazy stream failures retain the same hosted-aggregate guard as blocking failures."""
    from unittest.mock import MagicMock

    from chrys.foundation.errors import ProviderResponseError
    from chrys.kernel import ChatResponse, ChatResponseUpdate, ResponseStream
    from chrys.service.context.compaction import UnifiedContextStrategy
    from chrys.service.context.middleware.usage import UsageTrackingMiddleware

    strategy = UnifiedContextStrategy(max_context_tokens=350_000)
    strategy._last_included_tokens = 1_000
    strategy._request_overhead_floor = 9_000
    reported: list[tuple] = []
    middleware = UsageTrackingMiddleware(
        on_usage=lambda *args: reported.append(args),
        compaction_strategy=strategy,
        use_local_context_estimate_for_hosted_usage=True,
    )
    provider_error = ProviderResponseError(
        "server_error",
        "The model failed.",
        retryable=True,
        usage_details={
            "input_token_count": 80_000,
            "output_token_count": 500,
            "total_token_count": 80_500,
        },
    )

    async def updates():
        yield ChatResponseUpdate(role="assistant", contents=[])
        raise provider_error

    context = MagicMock()
    context.stream = True
    context.result = ResponseStream(updates(), finalizer=ChatResponse.from_updates)

    async def call_next() -> None:
        return None

    await middleware.process(context, call_next)
    with pytest.raises(ProviderResponseError):
        _ = [update async for update in context.result]

    assert reported[0][:4] == (80_500, 80_000, 500, 10_000)
    assert reported[0][-1] is True
    assert not strategy.calibration_initialized


@pytest.mark.asyncio
async def test_middleware_calibrates_strategy_non_streaming():
    """Non-streaming: UsageTrackingMiddleware calibrates strategy overhead."""
    from unittest.mock import MagicMock

    from chrys.kernel import ChatResponse
    from chrys.service.context.compaction import MixedLanguageTokenizer, UnifiedContextStrategy
    from chrys.service.context.middleware.usage import UsageTrackingMiddleware

    strategy = UnifiedContextStrategy(
        max_context_tokens=200_000,
        trigger_pct=0.85,
        target_pct=0.50,
        tokenizer=MixedLanguageTokenizer(),
    )
    # Simulate that the strategy has measured conversation tokens
    strategy._last_included_tokens = 50_000

    mw = UsageTrackingMiddleware(
        max_context_tokens=200_000,
        compaction_strategy=strategy,
    )

    context = MagicMock()
    context.messages = []
    context.stream = False

    async def call_next():
        pass

    # First call: learns overhead (65k - 50k = 15k)
    mock_result = MagicMock(spec=ChatResponse)
    mock_result.usage_details = {
        "input_token_count": 65_000,  # 50k conversation + 15k overhead
        "output_token_count": 5_000,
        "total_token_count": 70_000,
    }
    context.result = mock_result
    await mw.process(context, call_next)

    assert strategy.system_overhead_tokens == 15_000
    assert strategy.calibration_ratio == 1.0  # first call skips ratio
    assert mw._last_usage is not None
    assert mw._last_usage["input_token_count"] == 65_000

    # Second call: learns ratio
    mock_result.usage_details = {
        "input_token_count": 71_500,  # (50k + 15k) * 1.1
        "output_token_count": 5_000,
        "total_token_count": 76_500,
    }
    await mw.process(context, call_next)
    assert abs(strategy.calibration_ratio - 1.1) < 0.01


@pytest.mark.asyncio
async def test_middleware_streaming_uses_hooks():
    """Streaming: middleware publishes one usage event after stream finalization."""
    from unittest.mock import MagicMock

    from chrys.kernel import ResponseStream
    from chrys.service.context.compaction import MixedLanguageTokenizer, UnifiedContextStrategy
    from chrys.service.context.middleware.usage import UsageTrackingMiddleware

    strategy = UnifiedContextStrategy(
        max_context_tokens=200_000,
        trigger_pct=0.85,
        target_pct=0.50,
        tokenizer=MixedLanguageTokenizer(),
    )
    strategy._last_included_tokens = 50_000

    usage_calls: list[int] = []

    mw = UsageTrackingMiddleware(
        max_context_tokens=200_000,
        on_usage=lambda total, *_: usage_calls.append(total),
        compaction_strategy=strategy,
    )

    from chrys.kernel import ChatResponse, ChatResponseUpdate, Content

    usage_content = Content.from_usage(
        usage_details={"input_token_count": 100_000, "output_token_count": 3_000, "total_token_count": 103_000}
    )
    text_content = Content.from_text("Hello")

    async def _stream():
        yield ChatResponseUpdate(contents=[text_content], role="assistant", model="mock")
        yield ChatResponseUpdate(contents=[usage_content], role="assistant", model="mock")
        yield ChatResponseUpdate(contents=[], role="assistant", model="mock", finish_reason="stop")

    stream = ResponseStream(
        _stream(),
        finalizer=ChatResponse.from_updates,
    )

    context = MagicMock()
    context.messages = []
    context.stream = True
    context.result = stream

    async def call_next():
        pass

    await mw.process(context, call_next)

    updates = []
    async for update in context.result:
        updates.append(update)
    await context.result.get_final_response()

    assert usage_calls == [103_000]
    # First call learns overhead (100k - 50k = 50k), ratio stays 1.0
    assert strategy.system_overhead_tokens == 50_000
    assert strategy.calibration_ratio == 1.0
    assert mw._last_usage is not None
    assert mw._last_usage["input_token_count"] == 100_000


@pytest.mark.asyncio
async def test_middleware_streaming_publishes_latest_cumulative_usage_once() -> None:
    """Streaming: multiple cumulative usage snapshots do not double-count."""
    from unittest.mock import MagicMock

    from chrys.kernel import ChatResponse, ChatResponseUpdate, Content, ResponseStream
    from chrys.service.context.compaction import MixedLanguageTokenizer, UnifiedContextStrategy
    from chrys.service.context.middleware.usage import UsageTrackingMiddleware

    strategy = UnifiedContextStrategy(
        max_context_tokens=200_000,
        trigger_pct=0.85,
        target_pct=0.50,
        tokenizer=MixedLanguageTokenizer(),
    )
    strategy._last_included_tokens = 50_000

    usage_calls: list[int] = []
    mw = UsageTrackingMiddleware(
        max_context_tokens=200_000,
        on_usage=lambda total, *_: usage_calls.append(total),
        compaction_strategy=strategy,
    )

    first_usage = {"input_token_count": 100_000, "output_token_count": 10, "total_token_count": 100_010}
    final_usage = {"input_token_count": 100_000, "output_token_count": 300, "total_token_count": 100_300}

    async def _stream():
        yield ChatResponseUpdate(contents=[Content.from_text("Hello")], role="assistant", model="mock")
        yield ChatResponseUpdate(
            contents=[Content.from_usage(usage_details=first_usage)], role="assistant", model="mock"
        )
        yield ChatResponseUpdate(
            contents=[Content.from_usage(usage_details=final_usage)], role="assistant", model="mock"
        )
        yield ChatResponseUpdate(contents=[], role="assistant", model="mock", finish_reason="stop")

    stream = ResponseStream(_stream(), finalizer=ChatResponse.from_updates)
    context = MagicMock()
    context.messages = []
    context.stream = True
    context.result = stream

    async def call_next():
        pass

    await mw.process(context, call_next)
    async for _ in context.result:
        pass
    final_response = await context.result.get_final_response()

    assert usage_calls == [100_300]
    assert final_response.usage_details == final_usage
    assert strategy.system_overhead_tokens == 50_000
    assert mw._last_usage == final_usage


@pytest.mark.asyncio
async def test_middleware_streaming_anthropic_shape_keeps_latest_per_key() -> None:
    """Anthropic emits message_start (input + small initial output) then
    message_delta (cumulative output only). Last-write-wins per key must
    keep the input from chunk 1 and adopt the final cumulative output
    from chunk 2 — summing would over-count by the initial output value.
    """
    from unittest.mock import MagicMock

    from chrys.kernel import ChatResponse, ChatResponseUpdate, Content, ResponseStream
    from chrys.service.context.middleware.usage import UsageTrackingMiddleware

    usage_calls: list[int] = []
    mw = UsageTrackingMiddleware(
        max_context_tokens=200_000,
        on_usage=lambda total, *_: usage_calls.append(total),
    )

    # Anthropic streaming usage shape: message_start carries input + initial
    # output, message_delta carries only the final cumulative output (no input,
    # no total).
    message_start_usage = {"input_token_count": 100_000, "output_token_count": 3}
    message_delta_usage = {"output_token_count": 290}
    # Last-write-wins on output_token_count, input carried over from chunk 1.
    expected_usage = {"input_token_count": 100_000, "output_token_count": 290}

    async def _stream():
        yield ChatResponseUpdate(
            contents=[Content.from_usage(usage_details=message_start_usage)], role="assistant", model="mock"
        )
        yield ChatResponseUpdate(
            contents=[Content.from_usage(usage_details=message_delta_usage)], role="assistant", model="mock"
        )
        yield ChatResponseUpdate(contents=[], role="assistant", model="mock", finish_reason="stop")

    stream = ResponseStream(_stream(), finalizer=ChatResponse.from_updates)
    context = MagicMock()
    context.messages = []
    context.stream = True
    context.result = stream

    async def call_next():
        pass

    await mw.process(context, call_next)
    async for _ in context.result:
        pass
    final_response = await context.result.get_final_response()

    # Total falls back to input + output in _fire_callback when total_token_count
    # is absent (Anthropic doesn't populate it).
    assert usage_calls == [100_290]
    assert final_response.usage_details == expected_usage
    assert mw._last_usage == expected_usage


@pytest.mark.asyncio
async def test_middleware_streaming_uses_final_usage_without_usage_chunk():
    """Streaming: finalized usage_details is handled when no usage chunk appears."""
    from unittest.mock import MagicMock

    from chrys.kernel import ChatResponse, ChatResponseUpdate, Content, Message, ResponseStream
    from chrys.service.context.compaction import MixedLanguageTokenizer, UnifiedContextStrategy
    from chrys.service.context.middleware.usage import UsageTrackingMiddleware

    strategy = UnifiedContextStrategy(
        max_context_tokens=200_000,
        trigger_pct=0.85,
        target_pct=0.50,
        tokenizer=MixedLanguageTokenizer(),
    )
    strategy._last_included_tokens = 50_000

    usage_calls: list[int] = []
    mw = UsageTrackingMiddleware(
        max_context_tokens=200_000,
        on_usage=lambda total, *_: usage_calls.append(total),
        compaction_strategy=strategy,
    )

    usage_details = {"input_token_count": 80_000, "output_token_count": 4_000, "total_token_count": 84_000}

    async def _stream():
        yield ChatResponseUpdate(contents=[Content.from_text("Hello")], role="assistant", model="mock")
        yield ChatResponseUpdate(contents=[], role="assistant", model="mock", finish_reason="stop")

    def _finalizer(_updates):
        return ChatResponse(
            messages=[Message(role="assistant", contents=[Content.from_text("Hello")])],
            usage_details=usage_details,
        )

    stream = ResponseStream(_stream(), finalizer=_finalizer)
    context = MagicMock()
    context.messages = []
    context.stream = True
    context.result = stream

    async def call_next():
        pass

    await mw.process(context, call_next)
    async for _ in context.result:
        pass
    await context.result.get_final_response()

    assert usage_calls == [84_000]
    assert strategy.system_overhead_tokens == 30_000
    assert mw._last_usage == usage_details


@pytest.mark.asyncio
async def test_middleware_streaming_repeated_final_usage_without_usage_chunk() -> None:
    """Final-only streaming usage is handled per stream, even when values repeat."""
    from unittest.mock import MagicMock

    from chrys.kernel import ChatResponse, ChatResponseUpdate, Content, Message, ResponseStream
    from chrys.service.context.middleware.usage import UsageTrackingMiddleware

    strategy = MagicMock()
    strategy.last_included_tokens = 0
    strategy.calibration_ratio = 1.0
    strategy.system_overhead_tokens = 0

    usage_calls: list[int] = []
    mw = UsageTrackingMiddleware(
        max_context_tokens=200_000,
        on_usage=lambda total, *_: usage_calls.append(total),
        compaction_strategy=strategy,
    )
    usage_details = {"input_token_count": 80_000, "output_token_count": 4_000, "total_token_count": 84_000}

    async def run_once() -> None:
        async def _stream():
            yield ChatResponseUpdate(contents=[Content.from_text("Hello")], role="assistant", model="mock")
            yield ChatResponseUpdate(contents=[], role="assistant", model="mock", finish_reason="stop")

        def _finalizer(_updates):
            return ChatResponse(
                messages=[Message(role="assistant", contents=[Content.from_text("Hello")])],
                usage_details=usage_details,
            )

        context = MagicMock()
        context.messages = []
        context.stream = True
        context.result = ResponseStream(_stream(), finalizer=_finalizer)

        async def call_next():
            pass

        await mw.process(context, call_next)
        async for _ in context.result:
            pass
        await context.result.get_final_response()

    await run_once()
    await run_once()

    assert usage_calls == [84_000, 84_000]
    assert strategy.calibrate.call_count == 2


@pytest.mark.asyncio
async def test_middleware_streaming_no_usage_from_non_stream_result():
    """Non-streaming result with no usage_details doesn't crash."""
    from unittest.mock import MagicMock

    from chrys.kernel import ChatResponse
    from chrys.service.context.middleware.usage import UsageTrackingMiddleware

    mw = UsageTrackingMiddleware(max_context_tokens=200_000)
    usage_calls: list = []
    mw.on_usage = lambda total, *_: usage_calls.append(total)

    context = MagicMock()
    context.messages = []
    context.stream = False
    mock_result = MagicMock(spec=ChatResponse)
    mock_result.usage_details = None
    context.result = mock_result

    async def call_next():
        pass

    await mw.process(context, call_next)

    assert len(usage_calls) == 0
    assert mw._last_usage is None


# ---------------------------------------------------------------------------
# Integration: usage provider injects hint
# ---------------------------------------------------------------------------


def test_format_usage_line_basic_context_line() -> None:
    from chrys.service.agent_middleware.reminders.context_usage import format_usage_line

    line = format_usage_line({"total_token_count": 12_500}, max_context_tokens=100_000)

    assert line == "[Context Usage] current: 12.5% (12,500/100,000)"


def test_context_usage_warning_starts_at_the_threshold() -> None:
    from chrys.service.agent_middleware.reminders.context_usage import CONTEXT_USAGE_WARNING
    from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware

    def warns(total: int) -> bool:
        middleware = SystemReminderMiddleware(max_context_tokens=1_000, warn_threshold_pct=0.5)
        middleware.prepare_turn(usage={"total_token_count": total})
        return CONTEXT_USAGE_WARNING in middleware._build_reminders()

    assert not warns(499)
    assert warns(500)
    assert warns(501)


def test_context_usage_warning_reports_the_state_and_names_no_way_to_fold() -> None:
    """Even an offer to fold makes a model rewrite the prompt head while the platform is compacting."""
    from chrys.service.agent_middleware.reminders.context_usage import CONTEXT_USAGE_WARNING

    assert "for your information only" in CONTEXT_USAGE_WARNING
    assert "no action is needed" in CONTEXT_USAGE_WARNING
    for word in ("compress_context", "list_compressed_contexts", "fold", "WARNING"):
        assert word not in CONTEXT_USAGE_WARNING


def test_context_usage_warning_says_what_frees_context_where_nothing_compacts_on_its_own() -> None:
    """With automatic compaction off, only a fold by the model keeps the conversation inside the window."""
    from chrys.service.agent_middleware.reminders.context_usage import (
        CONTEXT_USAGE_WARNING,
        CONTEXT_USAGE_WARNING_MANUAL,
    )
    from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware

    middleware = SystemReminderMiddleware(max_context_tokens=1_000, warn_threshold_pct=0.5, automatic_compaction=False)
    middleware.prepare_turn(usage={"total_token_count": 600})
    reminders = middleware._build_reminders()

    assert CONTEXT_USAGE_WARNING_MANUAL in reminders
    assert CONTEXT_USAGE_WARNING not in reminders
    assert "automatic compaction is turned off" in CONTEXT_USAGE_WARNING_MANUAL
    assert "`compress_context` with a marker_id" in CONTEXT_USAGE_WARNING_MANUAL
    assert "no action is needed" not in CONTEXT_USAGE_WARNING_MANUAL


def test_sub_agent_tip_lists_sub_agents_only_when_available() -> None:
    from chrys.service.agent_middleware.reminders.sub_agents import SubAgentsSource

    tip = SubAgentsSource(["Explore", "General"]).snapshot()

    assert tip is not None
    assert tip.startswith("TIP: Sub-agents are available (`Explore`, `General`).")
    assert SubAgentsSource([]).snapshot() is None
    assert SubAgentsSource(None).snapshot() is None


def test_format_usage_line_falls_back_to_input_plus_output() -> None:
    from chrys.service.agent_middleware.reminders.context_usage import format_usage_line

    line = format_usage_line({"input_token_count": 2_000, "output_token_count": 500}, max_context_tokens=10_000)

    assert line == "[Context Usage] current: 25.0% (2,500/10,000)"
