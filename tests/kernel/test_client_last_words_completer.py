# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the client-owned LAST_WORDS completer (kernel side).

Covers the ``BaseChatClient.get_response`` → ``CompactionCallContext``
threading and the ``_ClientLastWordsCompleter`` contract:

* the compaction strategy receives a per-call context carrying the completer —
  except in effective Responses store mode, which is gated off pending the
  required live smoke validation;
* the side call goes through ``_inner_get_response`` (no recursive compaction);
* side-call options are a shallow copy with top-level-only overrides
  (``max_tokens``, ``store=False``) and all
  continuation/output-shaping/execution-mode keys stripped
  (``_SIDE_CALL_CONTINUATION_KEYS``, ``_SIDE_CALL_OUTPUT_SHAPING_KEYS``,
  ``_SIDE_CALL_EXECUTION_MODE_KEYS``) — the live options dict and the shared
  tools list are never mutated; ``tool_choice`` is forced to ``"none"``
  whenever tools ride along (and never invented without them) — the typed
  override is the primary tool suppression, backed by the strategy-layer
  instruction plus the response guard;
* streaming side calls are drained internally;
* the broad response guard rejects all six recognized tool-call content types;
* provider usage is reported through ``on_usage`` for every observed
  response — including guard-rejected ones — and skipped when absent;
* the completer returns only the note text — the side-call ``ChatResponse``
  (and its ``conversation_id``) never escapes.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

import chrys.kernel.client as kernel_client
import chrys.kernel.compaction as kernel_compaction
from chrys.foundation.errors import ProviderResponseError, is_retryable, may_be_context_overflow
from chrys.foundation.text.tokenizer import MixedLanguageTokenizer
from chrys.foundation.trajectory.context import (
    TRAJECTORY_EXCHANGE_KWARG,
    ExchangeTrace,
    TrajectoryContext,
    main_actor,
)
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.kernel import (
    CONTEXT_WINDOW_FILLED_KEY,
    EXHAUSTION_TAIL_KWARG,
    BaseChatClient,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    LastWordsToolCallError,
    Message,
    ResponseStream,
    in_internal_side_call,
)

_SIDE_CALL_CONTINUATION_KEYS = kernel_client._SIDE_CALL_CONTINUATION_KEYS
_SIDE_CALL_EXECUTION_MODE_KEYS = kernel_client._SIDE_CALL_EXECUTION_MODE_KEYS
_SIDE_CALL_OUTPUT_SHAPING_KEYS = kernel_client._SIDE_CALL_OUTPUT_SHAPING_KEYS
_ClientLastWordsCompleter = kernel_client._ClientLastWordsCompleter
_clamp_output_cap_for_context = kernel_client._clamp_output_cap_for_context
TOOL_CALL_CONTENT_TYPES = kernel_compaction.TOOL_CALL_CONTENT_TYPES
CompactionCallContext = kernel_compaction.CompactionCallContext


class _RecordingClient(BaseChatClient):
    """Minimal concrete client that records every ``_inner_get_response`` call."""

    def __init__(self, *, response: ChatResponse[Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.inner_calls: list[dict[str, Any]] = []
        self.stream_drain_side_call_flags: list[bool] = []
        self._response = response

    def _make_response(self) -> ChatResponse[Any]:
        if self._response is not None:
            return self._response
        return ChatResponse(messages=[Message("assistant", [Content.from_text("note text")])])

    def _inner_get_response(
        self,
        *,
        messages: Any,
        options: Any,
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:
        self.inner_calls.append(
            {
                "messages": list(messages),
                "options": options,
                "stream": stream,
                "kwargs": kwargs,
                "side_call": in_internal_side_call(),
            }
        )
        if stream:

            async def _updates() -> Any:
                self.stream_drain_side_call_flags.append(in_internal_side_call())
                yield ChatResponseUpdate(contents=[Content.from_text("note ")], role="assistant")
                self.stream_drain_side_call_flags.append(in_internal_side_call())
                yield ChatResponseUpdate(contents=[Content.from_text("text")], role="assistant")

            return ResponseStream(_updates(), finalizer=ChatResponse.from_updates)

        async def _response() -> Any:
            return self._make_response()

        return _response()


class _FailingClient(_RecordingClient):
    """A client whose adapter fails the response with *error*."""

    def __init__(self, *, error: ProviderResponseError, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._error = error

    def _make_response(self) -> ChatResponse[Any]:
        raise self._error


class _StoringClient(_RecordingClient):
    STORES_BY_DEFAULT = True  # type: ignore[misc]


class _ForcedStatelessStoringClient(_StoringClient):
    FORCES_STATELESS = True


class _CapturingStrategy:
    """Compaction-strategy fake that records the per-call context."""

    def __init__(self) -> None:
        self.invocations = 0
        self.contexts: list[Any] = []

    async def __call__(self, messages: list[Any], context: Any = None) -> bool:
        self.invocations += 1
        self.contexts.append(context)
        return False


class _AdmissionStrategy(_CapturingStrategy):
    def __init__(
        self,
        *,
        max_context_tokens: int = 100,
        last_included_tokens: int = 50,
        system_overhead_tokens: int = 0,
        calibration_ratio: float = 1.0,
    ) -> None:
        super().__init__()
        self.max_context_tokens = max_context_tokens
        self._last_included_tokens = last_included_tokens
        self._system_overhead_tokens = system_overhead_tokens
        self._calibration_ratio = calibration_ratio

    @property
    def last_included_tokens(self) -> int:
        return self._last_included_tokens

    @property
    def system_overhead_tokens(self) -> int:
        return self._system_overhead_tokens

    @property
    def calibration_ratio(self) -> float:
        return self._calibration_ratio


class _LenTokenizer:
    def count_tokens(self, text: str) -> int:
        return len(text)


class _CountingTokenizer(_LenTokenizer):
    def __init__(self) -> None:
        self.texts: list[str] = []

    def count_tokens(self, text: str) -> int:
        self.texts.append(text)
        return super().count_tokens(text)


@pytest.mark.asyncio
async def test_get_response_passes_completer_context_to_strategy() -> None:
    client = _RecordingClient()
    strategy = _CapturingStrategy()

    await client.get_response(
        [Message("user", ["hi"])],
        options={"model": "m"},
        compaction_strategy=strategy,
    )

    assert strategy.invocations == 1
    context = strategy.contexts[0]
    assert isinstance(context, CompactionCallContext)
    assert context.last_words_completer is not None


@pytest.mark.asyncio
async def test_get_response_passes_the_reserved_exchange_id_to_compaction() -> None:
    client = _RecordingClient()
    strategy = _CapturingStrategy()
    session_id = "12345678-1234-1234-1234-123456789abc"
    exchange_operation_id = new_analytics_id()
    trace = ExchangeTrace(
        TrajectoryContext(
            sink=cast("Any", object()),
            session_id=session_id,
            actor=main_actor(session_id),
            exchange_operation_id=exchange_operation_id,
        )
    )

    await client.get_response(
        [Message("user", ["hi"])],
        options={"model": "m"},
        client_kwargs={TRAJECTORY_EXCHANGE_KWARG: trace},
        compaction_strategy=strategy,
    )

    assert strategy.contexts[0].next_exchange_operation_id == exchange_operation_id


@pytest.mark.asyncio
async def test_get_response_gates_completer_off_in_store_mode() -> None:
    """Responses server-side storage is pending live smoke validation
    (doc §4.3/§7): effective store mode gets no completer, so Phase 4 keeps
    the reconstruction fallback."""
    # Store-by-default client (OpenAI Responses shape), no explicit option.
    storing_client = _StoringClient()
    strategy = _CapturingStrategy()
    await storing_client.get_response([Message("user", ["hi"])], options={"model": "m"}, compaction_strategy=strategy)
    assert isinstance(strategy.contexts[0], CompactionCallContext)
    assert strategy.contexts[0].last_words_completer is None

    # Explicit store=True on a client that does not store by default.
    plain_client = _RecordingClient()
    strategy = _CapturingStrategy()
    await plain_client.get_response(
        [Message("user", ["hi"])], options={"model": "m", "store": True}, compaction_strategy=strategy
    )
    assert strategy.contexts[0].last_words_completer is None

    # Explicit store=False overrides the client default: completer offered.
    storing_client = _StoringClient()
    strategy = _CapturingStrategy()
    await storing_client.get_response(
        [Message("user", ["hi"])], options={"model": "m", "store": False}, compaction_strategy=strategy
    )
    assert strategy.contexts[0].last_words_completer is not None


@pytest.mark.asyncio
async def test_force_stateless_keeps_last_words_completer_available_despite_store_true() -> None:
    client = _ForcedStatelessStoringClient()
    strategy = _CapturingStrategy()

    await client.get_response(
        [Message("user", ["hi"])],
        options={"model": "m", "store": True, "previous_response_id": "resp_1"},
        compaction_strategy=strategy,
    )

    assert strategy.contexts[0].last_words_completer is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_choice",
    [
        "required",
        "any",
        {"mode": "required"},
        {"type": "function", "function": {"name": "read_file"}},
        {"type": "tool", "name": "read_file"},
    ],
)
async def test_get_response_gates_completer_off_for_forced_tool_choice(tool_choice: object) -> None:
    client = _RecordingClient()
    strategy = _CapturingStrategy()
    await client.get_response(
        [Message("user", ["hi"])],
        options={"model": "m", "tools": [{"name": "read_file"}], "tool_choice": tool_choice},
        compaction_strategy=strategy,
    )
    assert strategy.contexts[0].last_words_completer is None


@pytest.mark.asyncio
async def test_get_response_offers_completer_for_auto_choice_and_estimates_tool_definitions() -> None:
    client = _RecordingClient(tokenizer=_LenTokenizer())
    strategy = _CapturingStrategy()
    await client.get_response(
        [Message("user", ["hi"])],
        options={"model": "m", "tools": [{"name": "read_file", "description": "read a file"}], "tool_choice": "auto"},
        compaction_strategy=strategy,
    )
    context = strategy.contexts[0]
    assert context.last_words_completer is not None
    assert context.tool_definition_tokens > 0
    assert context.request_overhead_tokens == context.tool_definition_tokens


def test_request_overhead_estimates_response_format_class_via_json_schema() -> None:
    """A Pydantic ``response_format`` class is costed as its wire schema.

    ``make_json_safe`` on the class walks the class ``__dict__`` (~100k
    serialized characters for a one-field model, vs ~124 for its actual JSON
    schema), which would trigger compaction immediately on small contexts.
    """
    from pydantic import BaseModel

    class _Tiny(BaseModel):
        answer: str

    overhead = kernel_client._estimate_fixed_request_overhead_tokens({"response_format": _Tiny}, {}, _LenTokenizer())
    assert 0 < overhead < 1_000


def test_tool_definition_estimate_counts_the_schema_of_a_schema_supplied_tool() -> None:
    """MCP and skill tools carry a JSON schema, not a model class; the estimate still counts it."""
    from chrys.kernel import FunctionTool

    schema = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "What to look for. " * 100}},
        "required": ["query"],
    }
    schema_tool = FunctionTool(name="search", description="Search.", func=lambda **_: "", input_model=schema)

    estimate = kernel_client._estimate_tool_definition_tokens({"tools": [schema_tool]}, _LenTokenizer())

    assert estimate >= len(json.dumps(schema, separators=(",", ":")))


def test_tool_definition_estimate_keeps_cjk_literal() -> None:
    tokenizer = MixedLanguageTokenizer()
    tools = [{"name": "翻译", "description": "中文说明" * 100}]
    literal = json.dumps(tools, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    escaped = json.dumps(tools, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    estimate = kernel_client._estimate_tool_definition_tokens({"tools": tools}, tokenizer)

    assert estimate == tokenizer.count_tokens(literal)
    assert estimate < tokenizer.count_tokens(escaped)


def test_non_string_value_estimate_keeps_cjk_literal() -> None:
    tokenizer = MixedLanguageTokenizer()
    value = {"description": "応答形式" * 100}
    literal = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    escaped = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    estimate = kernel_client._estimate_value_tokens(value, tokenizer)

    assert estimate == tokenizer.count_tokens(literal)
    assert estimate < tokenizer.count_tokens(escaped)


def test_request_overhead_counts_larger_of_conflicting_options_and_kwargs_values() -> None:
    """When options and kwargs both set an overhead key, the larger one counts.

    Anthropic ultimately wires ``options["instructions"]`` even when a kwargs
    ``instructions`` is present, so a kwargs-wins merged view would undercount
    and admission could overflow the context.
    """
    big = "x" * 400
    small = "y" * 20
    overhead = kernel_client._estimate_fixed_request_overhead_tokens(
        {"instructions": big}, {"instructions": small}, _LenTokenizer()
    )
    assert overhead >= 400


@pytest.mark.parametrize("key", ["text_format", "output_format"])
def test_request_overhead_counts_provider_native_shaping_keys(key: str) -> None:
    """Every spelling the side call scrubs as wire-affecting is also costed."""
    overhead = kernel_client._estimate_fixed_request_overhead_tokens({key: "x" * 120}, {}, _LenTokenizer())
    assert overhead >= 120


@pytest.mark.parametrize("carrier", ["options", "client_kwargs"])
def test_request_overhead_counts_extra_body_nested_shaping_values(carrier: str) -> None:
    """Provider SDKs merge ``extra_body`` into the request body last, so a
    nested schema ships on the wire and must count toward admission."""
    payload = {"extra_body": {"text": "x" * 300}}
    options = payload if carrier == "options" else {}
    kwargs = payload if carrier == "client_kwargs" else {}
    overhead = kernel_client._estimate_fixed_request_overhead_tokens(options, kwargs, _LenTokenizer())
    assert overhead >= 300


@pytest.mark.asyncio
async def test_tool_definitions_are_tokenized_once_per_context_build() -> None:
    """The context build reuses the tool estimate instead of recomputing it
    inside the fixed-overhead estimator (tools lists can be tens of KB)."""
    tokenizer = _CountingTokenizer()
    client = _RecordingClient(tokenizer=tokenizer)
    strategy = _CapturingStrategy()

    await client.get_response(
        [Message("user", ["hi"])],
        options={"model": "m", "tools": [{"name": "t", "description": "d"}]},
        compaction_strategy=strategy,
    )

    context = strategy.contexts[0]
    assert context.request_overhead_tokens == context.tool_definition_tokens > 0
    serialized_tools = json.dumps(
        [{"description": "d", "name": "t"}], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert tokenizer.texts.count(serialized_tools) == 1


@pytest.mark.asyncio
async def test_store_gate_honors_extra_body_store() -> None:
    """A nested ``extra_body.store`` is the value that actually ships (the
    SDK merges ``extra_body`` last), so the completer gate must read it."""
    # extra_body.store=True defeats a top-level store=False on the wire.
    client = _RecordingClient()
    strategy = _CapturingStrategy()
    await client.get_response(
        [Message("user", ["hi"])],
        options={"model": "m", "store": False, "extra_body": {"store": True}},
        compaction_strategy=strategy,
    )
    assert strategy.contexts[0].last_words_completer is None

    # extra_body.store=False re-enables the completer on a storing client.
    storing = _StoringClient()
    strategy = _CapturingStrategy()
    await storing.get_response(
        [Message("user", ["hi"])],
        options={"model": "m", "extra_body": {"store": False}},
        compaction_strategy=strategy,
    )
    assert strategy.contexts[0].last_words_completer is not None

    # A kwargs extra_body does NOT mask the options one: the OpenAI adapters
    # ignore request kwargs entirely, so the options nested value still ships
    # there. Explicit False with no truthy spelling anywhere → completer on.
    storing = _StoringClient()
    strategy = _CapturingStrategy()
    await storing.get_response(
        [Message("user", ["hi"])],
        options={"model": "m", "extra_body": {"store": False}},
        compaction_strategy=strategy,
        client_kwargs={"extra_body": {"sentinel": 1}},
    )
    assert strategy.contexts[0].last_words_completer is not None

    # Conversely a truthy options spelling gates off even when a kwargs
    # extra_body rides along — on adapters that drop kwargs it still ships.
    client = _RecordingClient()
    strategy = _CapturingStrategy()
    await client.get_response(
        [Message("user", ["hi"])],
        options={"model": "m", "extra_body": {"store": True}},
        compaction_strategy=strategy,
        client_kwargs={"extra_body": {"sentinel": 1}},
    )
    assert strategy.contexts[0].last_words_completer is None


@pytest.mark.asyncio
async def test_completer_scrubs_wire_overriding_keys_inside_extra_body() -> None:
    """Nested continuation/shaping/execution/cap/``store``/``tool_choice``
    spellings would override the side call's top-level sanitization when the
    SDK merges ``extra_body`` last — scrub them from a copied mapping and
    leave the live values untouched."""
    live_extra = {
        "store": True,
        "text": {"format": {"type": "json_schema"}},
        "conversation_id": "conv",
        "max_tokens": 5,
        "tool_choice": "any",
        "background": True,
        "input": [{"role": "user", "content": "hijack"}],
        "instructions": "evil override",
        "tools": [{"name": "t"}],
        "stream": True,
        "cache_control": {"type": "ephemeral"},
    }
    live_options: dict[str, Any] = {"model": "m", "extra_body": live_extra}
    client = _RecordingClient()
    completer = _ClientLastWordsCompleter(
        client,
        stream=False,
        options=live_options,
        client_kwargs={"extra_body": {"store": True, "sentinel": "kept"}},
    )

    await completer.complete_last_words([Message("user", ["q"])], "I", max_output_tokens=10)

    call = client.inner_calls[-1]
    assert call["options"]["extra_body"] == {"cache_control": {"type": "ephemeral"}}
    assert call["kwargs"]["extra_body"] == {"sentinel": "kept"}
    assert live_options["extra_body"] is live_extra
    assert live_extra["store"] is True


@pytest.mark.parametrize("alias", ("max_tokens", "max_output_tokens", "max_completion_tokens"))
def test_output_cap_clamp_uses_calibrated_room_and_never_mutates_or_raises(alias: str) -> None:
    strategy = _AdmissionStrategy(
        max_context_tokens=100,
        last_included_tokens=50,
        system_overhead_tokens=10,
        calibration_ratio=1.2,
    )
    original = {alias: 50}

    clamped = _clamp_output_cap_for_context(
        original,
        strategy=strategy,
        request_overhead_tokens=20,
        client_kwargs={},
    )

    assert clamped[alias] == 16
    assert original[alias] == 50
    assert (
        _clamp_output_cap_for_context(
            {alias: 10},
            strategy=strategy,
            request_overhead_tokens=20,
            client_kwargs={},
        )[alias]
        == 10
    )


def _admitted_cap(*, room: int, requested: int = 32_000, max_context_tokens: int = 200_000) -> int:
    strategy = _AdmissionStrategy(max_context_tokens=max_context_tokens, last_included_tokens=max_context_tokens - room)
    clamped = _clamp_output_cap_for_context(
        {"max_tokens": requested},
        strategy=strategy,
        request_overhead_tokens=0,
        client_kwargs={},
    )
    return clamped["max_tokens"]


def test_output_cap_clamp_keeps_a_safety_margin_inside_a_wide_room() -> None:
    """Estimated 196,008 input tokens leave 3,992 in a 200k window; a provider
    counting one more token rejects 196,009 + 3,992 = 200,001 outright."""
    assert _admitted_cap(room=3_992) == 3_992 - 2_000  # 1% of the window
    assert _admitted_cap(room=100_000, requested=150_000) == 98_000
    # A cap already inside the guarded room is left alone.
    assert _admitted_cap(room=100_000, requested=32_000) == 32_000
    # Small windows keep a floor of margin.
    assert _admitted_cap(room=2_000, requested=4_000, max_context_tokens=8_000) == 2_000 - 256


def test_output_cap_margin_yields_to_a_useful_output_but_never_past_the_room() -> None:
    assert _admitted_cap(room=1_500) == 1_024
    assert _admitted_cap(room=800) == 800
    for room in range(1, 6_000, 37):
        admitted = _admitted_cap(room=room)
        assert min(room, 1_024) <= admitted <= room, room


def test_output_cap_clamp_handles_multiple_aliases_and_minimum_legal_caps() -> None:
    strategy = _AdmissionStrategy(max_context_tokens=100, last_included_tokens=99)
    aliases = ("max_tokens", "max_output_tokens", "max_completion_tokens")
    options: dict[str, Any] = dict.fromkeys(aliases, 100)
    options["thinking"] = {"type": "enabled", "budget_tokens": 20}

    thinking_clamped = _clamp_output_cap_for_context(
        options,
        strategy=strategy,
        request_overhead_tokens=0,
        client_kwargs={},
        provider_min_output_cap_tokens=16,
    )

    assert all(thinking_clamped[alias] == 21 for alias in aliases)
    provider_clamped = _clamp_output_cap_for_context(
        {"max_tokens": 100},
        strategy=strategy,
        request_overhead_tokens=0,
        client_kwargs={},
        provider_min_output_cap_tokens=16,
    )
    assert provider_clamped["max_tokens"] == 16


def test_output_cap_clamp_noops_without_admission_protocol_or_cap(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("DEBUG"):
        assert _clamp_output_cap_for_context(
            {"temperature": 0.5},
            strategy=_AdmissionStrategy(),
            request_overhead_tokens=0,
            client_kwargs={},
        ) == {"temperature": 0.5}
    assert "no cap alias" in caplog.text
    assert (
        _clamp_output_cap_for_context(
            {"max_tokens": 50},
            strategy=_CapturingStrategy(),
            request_overhead_tokens=0,
            client_kwargs={},
        )["max_tokens"]
        == 50
    )
    assert (
        _clamp_output_cap_for_context(
            {"max_tokens": 50},
            strategy=None,
            request_overhead_tokens=0,
            client_kwargs={},
        )["max_tokens"]
        == 50
    )


@pytest.mark.asyncio
async def test_wire_call_folds_kwargs_cap_and_uses_merged_admission_view() -> None:
    client = _RecordingClient(tokenizer=_LenTokenizer())
    strategy = _AdmissionStrategy(max_context_tokens=100, last_included_tokens=70)

    await client.get_response(
        [Message("user", ["hi"])],
        options={"max_tokens": 80, "thinking": {"type": "enabled", "budget_tokens": 2}},
        compaction_strategy=strategy,
        client_kwargs={
            "max_tokens": 60,
            "thinking": {"type": "enabled", "budget_tokens": 40},
            "response_format": {"type": "json_schema", "schema": {"type": "object"}},
            "sentinel": "kept",
        },
    )

    call = client.inner_calls[-1]
    assert call["options"]["max_tokens"] == 41
    assert "max_tokens" not in call["kwargs"]
    assert call["kwargs"]["sentinel"] == "kept"
    context = strategy.contexts[0]
    assert context.request_overhead_tokens > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("compacting", [False, True], ids=["direct", "compaction"])
async def test_the_exhaustion_tail_flag_never_reaches_the_provider(compacting: bool) -> None:
    client = _RecordingClient(tokenizer=_LenTokenizer())
    strategy = _AdmissionStrategy(max_context_tokens=1_000, last_included_tokens=1) if compacting else None

    await client.get_response(
        [Message("user", ["hi"])],
        options={},
        compaction_strategy=strategy,
        client_kwargs={EXHAUSTION_TAIL_KWARG: True, "sentinel": "kept"},
    )

    sent = client.inner_calls[-1]["kwargs"]
    assert EXHAUSTION_TAIL_KWARG not in sent
    assert sent["sentinel"] == "kept"


@pytest.mark.asyncio
async def test_kwargs_cap_overrides_options_cap_across_alias_spellings() -> None:
    """A kwargs cap wins even when the options cap uses a different alias.

    Provider serializers resolve alias conflicts toward canonical
    ``max_tokens``, so the fold must drop the options spelling instead of
    leaving both present.
    """
    client = _RecordingClient(tokenizer=_LenTokenizer())
    strategy = _AdmissionStrategy(max_context_tokens=1_000, last_included_tokens=1)

    await client.get_response(
        [Message("user", ["hi"])],
        options={"max_tokens": 80},
        compaction_strategy=strategy,
        client_kwargs={"max_output_tokens": 40},
    )

    sent = client.inner_calls[-1]["options"]
    assert sent["max_output_tokens"] == 40
    assert "max_tokens" not in sent
    assert "max_output_tokens" not in client.inner_calls[-1]["kwargs"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_options", [[], ["not", "a", "mapping"]], ids=["falsey-list", "truthy-list"])
async def test_non_mapping_options_bypass_compaction_and_reach_provider_verbatim(invalid_options: list[Any]) -> None:
    """Invalid options skip the compaction path instead of being coerced.

    The compaction context build uses ``options or {}``, which would silently
    replace a falsey non-Mapping (e.g. ``[]``) with ``{}`` — the provider must
    instead receive the value unchanged and reject it like every other path.
    """
    client = _RecordingClient(tokenizer=_LenTokenizer())
    strategy = _AdmissionStrategy(max_context_tokens=1_000, last_included_tokens=1)

    await client.get_response(
        [Message("user", ["hi"])],
        options=invalid_options,
        compaction_strategy=strategy,
    )

    assert strategy.invocations == 0
    assert client.inner_calls[-1]["options"] is invalid_options


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_get_response_snapshots_top_level_tools_and_thinking_before_await(stream: bool) -> None:
    client = _RecordingClient()
    strategy = _AdmissionStrategy(max_context_tokens=1_000, last_included_tokens=1)
    tools = [{"name": "first"}]
    thinking = {"type": "enabled", "budget_tokens": 10}
    options: dict[str, Any] = {"max_tokens": 100, "tools": tools, "thinking": thinking}

    pending = client.get_response(
        [Message("user", ["hi"])],
        stream=stream,
        options=options,
        compaction_strategy=strategy,
    )
    options["max_tokens"] = 999
    tools.append({"name": "late"})
    thinking["budget_tokens"] = 999
    if isinstance(pending, ResponseStream):
        await pending.get_final_response()
    else:
        await pending

    sent = client.inner_calls[-1]["options"]
    assert sent["max_tokens"] == 100
    assert sent["tools"] == [{"name": "first"}]
    assert sent["thinking"]["budget_tokens"] == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("option_key", ["instructions", "system", "response_format"])
async def test_fixed_request_overhead_counts_non_message_payloads(option_key: str) -> None:
    client = _RecordingClient(tokenizer=_LenTokenizer())
    strategy = _CapturingStrategy()

    await client.get_response(
        [Message("user", ["hi"])],
        options={option_key: {"payload": "fixed"} if option_key == "response_format" else "fixed"},
        compaction_strategy=strategy,
    )

    assert strategy.contexts[0].request_overhead_tokens > 0


@pytest.mark.asyncio
async def test_completer_side_call_shape_options_and_containment() -> None:
    class _LiveSchema:
        pass

    tools = [{"name": "some_tool"}]
    live_options: dict[str, Any] = {
        "model": "m",
        "tools": tools,
        "tool_choice": "auto",
        "max_tokens": 111,
        "temperature": 0.5,
        "response_format": _LiveSchema,
        "conversation_id": "conv_live",
    }
    live_snapshot = dict(live_options)
    response = ChatResponse(
        messages=[Message("assistant", [Content.from_text("  note text  ")])],
        conversation_id="resp_side_call_must_not_escape",
    )
    client = _RecordingClient(response=response)
    completer = _ClientLastWordsCompleter(client, stream=False, options=live_options, client_kwargs={"extra": 1})
    base_messages = [Message("user", ["question"]), Message("assistant", ["answer"])]

    note = await completer.complete_last_words(base_messages, "INSTRUCTION", max_output_tokens=777)

    # Returns only the note text, verbatim (outer whitespace preserved —
    # the note validator treats indentation as meaningful) — the
    # ChatResponse (with its conversation_id) never escapes, so it cannot
    # reach hooks or continuation state, and the live options gained no state.
    assert note == "  note text  "
    assert live_options == live_snapshot
    assert live_options["tools"] is tools

    side_call = client.inner_calls[-1]
    assert side_call["stream"] is False
    assert side_call["kwargs"] == {"extra": 1}
    # Marked as an internal side call for downstream instrumentation, and the
    # marker does not leak past the completer.
    assert side_call["side_call"] is True
    assert not in_internal_side_call()
    side_options = side_call["options"]
    assert side_options is not live_options
    # tool_choice is forced to "none" whenever tools ride along — the typed
    # override is the primary tool suppression; the no-tools instruction and
    # the response guard cover plain-text tool markup it cannot stop.
    assert side_options["tool_choice"] == "none"
    assert side_options["max_tokens"] == 777
    # Tools shared by reference (prefix identity), never copied or mutated.
    assert side_options["tools"] is tools
    # Non-storing client without a store option: no store key is invented.
    assert "store" not in side_options
    # Output shaping and conversation chaining are stripped — a structured
    # live call must not force the note into schema JSON, and the side call
    # must not join a stored conversation.
    assert "response_format" not in side_options
    assert "conversation_id" not in side_options
    # Untouched top-level options carry through.
    assert side_options["temperature"] == 0.5

    side_messages = side_call["messages"]
    for sent, source in zip(side_messages[:-1], base_messages, strict=True):
        assert sent is not source
        assert sent.additional_properties is source.additional_properties
        assert all(x is y for x, y in zip(sent.contents, source.contents, strict=True))
    assert side_messages[-1].role == "user"
    assert side_messages[-1].text == "INSTRUCTION"


@pytest.mark.asyncio
async def test_completer_sanitizes_forwarded_kwargs_and_preserves_kwargs_cap_ceiling() -> None:
    client = _RecordingClient()
    completer = _ClientLastWordsCompleter(
        client,
        stream=False,
        options={"model": "m", "store": True},
        client_kwargs={
            "response_format": {"type": "json_object"},
            "output_config": {"format": {"type": "json_schema"}},
            "conversation_id": "conv",
            "background": True,
            "store": True,
            "sentinel": "kept",
        },
        kwargs_cap_ceiling=25,
    )

    await completer.complete_last_words([Message("user", ["q"])], "I", max_output_tokens=100)

    call = client.inner_calls[-1]
    assert call["options"]["max_tokens"] == 25
    assert call["options"]["store"] is False
    assert call["kwargs"] == {"sentinel": "kept"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key",
    sorted((*_SIDE_CALL_CONTINUATION_KEYS, *_SIDE_CALL_OUTPUT_SHAPING_KEYS, *_SIDE_CALL_EXECUTION_MODE_KEYS)),
)
async def test_completer_scrubs_continuation_and_output_shaping_keys(key: str) -> None:
    """Raw provider passthroughs survive serialization (Responses honors
    ``previous_response_id``/``conversation``/``text``/``text_format``;
    Anthropic forwards ``output_format``) and Responses execution state can
    hijack the request outright (``continuation_token`` polls an existing
    background response instead of sending messages; ``background=True``
    launches never-polled orphans), so every continuation, output-shaping,
    and execution-mode key must be scrubbed from the side call — and stay in
    the live options."""
    live_options: dict[str, Any] = {"model": "m", key: "sentinel-value"}
    client = _RecordingClient()
    completer = _ClientLastWordsCompleter(client, stream=False, options=live_options, client_kwargs={})

    await completer.complete_last_words([Message("user", ["q"])], "I", max_output_tokens=10)

    assert key not in client.inner_calls[-1]["options"]
    assert live_options[key] == "sentinel-value"


@pytest.mark.asyncio
async def test_completer_forces_tool_choice_none_when_live_call_has_tools() -> None:
    """A live call with tools but no ``tool_choice`` still gets the "none"
    override — instruction-only suppression is not enough in practice."""
    client = _RecordingClient()
    completer = _ClientLastWordsCompleter(
        client, stream=False, options={"model": "m", "tools": [{"name": "t"}]}, client_kwargs={}
    )

    await completer.complete_last_words([Message("user", ["q"])], "I", max_output_tokens=10)

    assert client.inner_calls[-1]["options"]["tool_choice"] == "none"


@pytest.mark.asyncio
async def test_completer_never_invents_tool_choice_without_tools() -> None:
    """A tool-less live call yields a side call without ``tool_choice`` —
    "none" is meaningless there and providers differ on rejecting the
    combination."""
    client = _RecordingClient()
    completer = _ClientLastWordsCompleter(client, stream=False, options={"model": "m"}, client_kwargs={})

    await completer.complete_last_words([Message("user", ["q"])], "I", max_output_tokens=10)

    assert "tool_choice" not in client.inner_calls[-1]["options"]


@pytest.mark.asyncio
async def test_completer_store_overrides() -> None:
    # Client that stores by default (OpenAI Responses): store=False forced.
    storing_client = _StoringClient()
    completer = _ClientLastWordsCompleter(storing_client, stream=False, options={"model": "m"}, client_kwargs={})
    await completer.complete_last_words([Message("user", ["q"])], "I", max_output_tokens=10)
    assert storing_client.inner_calls[-1]["options"]["store"] is False

    # Live options explicitly storing: side call flips its copy only.
    live_options: dict[str, Any] = {"model": "m", "store": True}
    plain_client = _RecordingClient()
    completer = _ClientLastWordsCompleter(plain_client, stream=False, options=live_options, client_kwargs={})
    await completer.complete_last_words([Message("user", ["q"])], "I", max_output_tokens=10)
    assert plain_client.inner_calls[-1]["options"]["store"] is False
    assert live_options["store"] is True


@pytest.mark.asyncio
async def test_completer_drains_streaming_side_call() -> None:
    client = _RecordingClient()
    completer = _ClientLastWordsCompleter(client, stream=True, options={"model": "m"}, client_kwargs={})

    note = await completer.complete_last_words([Message("user", ["q"])], "I", max_output_tokens=10)

    assert note == "note text"
    assert client.inner_calls[-1]["stream"] is True
    # The side-call marker covers the stream drain (where result hooks fire),
    # not just the issuing call, and resets afterwards.
    assert client.stream_drain_side_call_flags == [True, True]
    assert not in_internal_side_call()


@pytest.mark.asyncio
async def test_completer_preserves_leading_indentation_of_response_text() -> None:
    """A code-block-indented heading must reach the note validator intact:
    ``ChatResponse.text`` strips outer whitespace, so the completer reads
    ``raw_text`` — stripping here would legitimize an invalid note."""
    indented = "    ## Task\nDo it\n\n## Progress\nDone\n\n## Next\nContinue\n"
    response = ChatResponse(messages=[Message("assistant", [Content.from_text(indented)])])
    assert response.text == indented.strip()  # the stripping view the completer must NOT use
    assert response.raw_text == indented
    client = _RecordingClient(response=response)
    completer = _ClientLastWordsCompleter(client, stream=False, options={"model": "m"}, client_kwargs={})

    note = await completer.complete_last_words([Message("user", ["q"])], "I", max_output_tokens=10)

    assert note == indented


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", sorted(TOOL_CALL_CONTENT_TYPES))
async def test_completer_guard_rejects_every_tool_call_content_type(content_type: str) -> None:
    response = ChatResponse(
        messages=[Message("assistant", [Content.from_text("text"), Content(content_type)])]  # type: ignore[arg-type]
    )
    client = _RecordingClient(response=response)
    completer = _ClientLastWordsCompleter(client, stream=False, options={"model": "m"}, client_kwargs={})

    with pytest.raises(LastWordsToolCallError):
        await completer.complete_last_words([Message("user", ["q"])], "I", max_output_tokens=10)
    # The marker resets even when the guard rejects the response.
    assert not in_internal_side_call()


@pytest.mark.asyncio
async def test_completer_reports_usage_to_on_usage_callback() -> None:
    """Provider-reported usage from an accepted side call reaches ``on_usage``
    verbatim — side-call responses never traverse the middleware chain, so
    this hook is the only path that spend can take to session accounting."""
    usage = {"input_token_count": 120, "output_token_count": 30, "total_token_count": 150}
    response = ChatResponse(
        messages=[Message("assistant", [Content.from_text("note text")])],
        usage_details=usage,  # type: ignore[arg-type]
    )
    client = _RecordingClient(response=response)
    completer = _ClientLastWordsCompleter(client, stream=False, options={"model": "m"}, client_kwargs={})
    reported: list[Any] = []

    note = await completer.complete_last_words(
        [Message("user", ["q"])], "I", max_output_tokens=10, on_usage=reported.append
    )

    assert note == "note text"
    assert reported == [usage]


@pytest.mark.asyncio
async def test_completer_reports_usage_even_when_guard_rejects() -> None:
    """A response the tool-call guard rejects still consumed real provider
    tokens — usage is reported before the acceptance decision, so failed
    attempts count too."""
    usage = {"input_token_count": 80, "output_token_count": 15, "total_token_count": 95}
    response = ChatResponse(
        messages=[Message("assistant", [Content("function_call")])],  # type: ignore[arg-type]
        usage_details=usage,  # type: ignore[arg-type]
    )
    client = _RecordingClient(response=response)
    completer = _ClientLastWordsCompleter(client, stream=False, options={"model": "m"}, client_kwargs={})
    reported: list[Any] = []

    with pytest.raises(LastWordsToolCallError):
        await completer.complete_last_words(
            [Message("user", ["q"])], "I", max_output_tokens=10, on_usage=reported.append
        )

    assert reported == [usage]


@pytest.mark.asyncio
async def test_completer_fails_a_note_that_filled_the_context_window() -> None:
    """The completer sends the whole conversation, the call most likely to fill
    the window: a note it cut off fails as an overflow that is not retried, so
    the caller sends a smaller prompt. Its usage is reported first."""
    usage = {"input_token_count": 90, "output_token_count": 4, "total_token_count": 94}
    response = ChatResponse(
        messages=[Message("assistant", [Content.from_text("## Task\nPartial")])],
        usage_details=usage,  # type: ignore[arg-type]
        additional_properties={CONTEXT_WINDOW_FILLED_KEY: True},
    )
    completer = _ClientLastWordsCompleter(
        _RecordingClient(response=response), stream=False, options={"model": "m"}, client_kwargs={}
    )
    reported: list[Any] = []

    with pytest.raises(ProviderResponseError) as raised:
        await completer.complete_last_words(
            [Message("user", ["q"])], "I", max_output_tokens=10, on_usage=reported.append
        )

    assert raised.value.code == "model_context_window_exceeded"
    assert may_be_context_overflow(raised.value)
    assert not is_retryable(raised.value)
    assert reported == [usage]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [{"input_token_count": 70, "output_token_count": 9, "total_token_count": 79}, None],
    ids=["usage", "no_usage"],
)
async def test_completer_reports_the_usage_of_a_response_the_adapter_failed(usage: dict[str, int] | None) -> None:
    """A response the adapter fails (a failure finish reason, a refusal that
    asks for calls) consumed provider tokens too; without usage nothing is
    reported."""
    error = ProviderResponseError("network_error", "It broke.", retryable=True, usage_details=usage)
    completer = _ClientLastWordsCompleter(
        _FailingClient(error=error), stream=False, options={"model": "m"}, client_kwargs={}
    )
    reported: list[Any] = []

    with pytest.raises(ProviderResponseError) as raised:
        await completer.complete_last_words(
            [Message("user", ["q"])], "I", max_output_tokens=10, on_usage=reported.append
        )

    assert raised.value is error
    assert reported == ([] if usage is None else [usage])
    assert not in_internal_side_call()


@pytest.mark.asyncio
async def test_completer_skips_on_usage_when_response_has_no_usage_details() -> None:
    """No provider usage on the response (mocks, providers that omit it) —
    the callback is simply not invoked rather than fed an empty payload."""
    client = _RecordingClient()
    completer = _ClientLastWordsCompleter(client, stream=False, options={"model": "m"}, client_kwargs={})
    reported: list[Any] = []

    note = await completer.complete_last_words(
        [Message("user", ["q"])], "I", max_output_tokens=10, on_usage=reported.append
    )

    assert note == "note text"
    assert reported == []


@pytest.mark.asyncio
async def test_completer_side_call_bypasses_compaction() -> None:
    """The side call goes through ``_inner_get_response`` — the strategy is
    never re-entered from within its own Phase 4 side call."""

    class _Phase4LikeStrategy(_CapturingStrategy):
        def __init__(self) -> None:
            super().__init__()
            self.note: str | None = None

        async def __call__(self, messages: list[Any], context: Any = None) -> bool:
            await super().__call__(messages, context)
            self.note = await context.last_words_completer.complete_last_words(
                list(messages), "SUMMARIZE", max_output_tokens=50
            )
            return False

    client = _RecordingClient()
    strategy = _Phase4LikeStrategy()

    await client.get_response(
        [Message("user", ["hi"])],
        options={"model": "m"},
        compaction_strategy=strategy,
    )

    assert strategy.invocations == 1
    assert strategy.note == "note text"
    # Exactly two provider calls: the side call, then the real call.
    assert len(client.inner_calls) == 2
    assert client.inner_calls[0]["options"].get("max_tokens") == 50
    assert "max_tokens" not in client.inner_calls[1]["options"]
    # Only the side call carries the internal-side-call marker.
    assert client.inner_calls[0]["side_call"] is True
    assert client.inner_calls[1]["side_call"] is False


@pytest.mark.parametrize(("stream", "expected"), [(True, [1, 4]), (False, [1, 2])])
@pytest.mark.asyncio
async def test_completer_reports_wire_progress_on_dispatch_each_chunk_and_completion(
    stream: bool, expected: list[int]
) -> None:
    """The side call runs inside the live pull's stall watchdog: every report
    restarts its idle timer, so a long note that keeps streaming never stalls
    the pull.  A streamed note reports per chunk; a blocking one only around
    the request."""
    reports = 0

    def _on_progress() -> None:
        nonlocal reports
        reports += 1

    at_dispatch: list[int] = []

    class _Client(_RecordingClient):
        def _inner_get_response(self, **kwargs: Any) -> Any:
            at_dispatch.append(reports)
            return super()._inner_get_response(**kwargs)

    completer = _ClientLastWordsCompleter(_Client(), stream=stream, options={"model": "m"}, client_kwargs={})
    note = await kernel_client.start_with_wire_progress(
        completer.complete_last_words([Message("user", ["q"])], "I", max_output_tokens=10), _on_progress
    )

    assert note == "note text"
    assert [at_dispatch[0], reports] == expected
    # The callback belongs to the task's context only.
    kernel_client.report_wire_progress()
    assert reports == expected[1]


@pytest.mark.asyncio
async def test_wire_preparation_reports_progress_once_compaction_finishes() -> None:
    """The provider's time to first byte gets a full stall window after compaction."""
    reports = 0

    def _on_progress() -> None:
        nonlocal reports
        reports += 1

    observed: list[tuple[str, int]] = []

    class _Strategy(_CapturingStrategy):
        async def __call__(self, messages: list[Any], context: Any = None) -> bool:
            observed.append(("compaction", reports))
            return await super().__call__(messages, context)

    class _Client(_RecordingClient):
        def _inner_get_response(self, **kwargs: Any) -> Any:
            observed.append(("dispatch", reports))
            return super()._inner_get_response(**kwargs)

    await kernel_client.start_with_wire_progress(
        _Client().get_response([Message("user", ["hi"])], options={"model": "m"}, compaction_strategy=_Strategy()),
        _on_progress,
    )

    assert observed == [("compaction", 0), ("dispatch", 1)]
