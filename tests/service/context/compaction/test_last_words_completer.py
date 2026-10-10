# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The cache-safe completer side call: success, usage reporting, instruction composition, demotion and slice budgets."""

from __future__ import annotations

import math
from typing import ClassVar

import pytest

from chrys.foundation.errors import ProviderResponseError
from chrys.foundation.retry import RetryAttemptInfo
from chrys.kernel import Content, LastWordsToolCallError, Message
from chrys.service.context.compaction import last_words as last_words_mod
from chrys.service.context.compaction.last_words import (
    _BASE_GUIDANCE,
    _FORMAT_CONTRACT,
    _SLICE_SAFETY_MARGIN_TOKENS,
    _SUPPLEMENT_LABEL,
    LastWordsGenerationError,
    LastWordsGenerator,
)
from chrys.service.context.compaction.scoped import ScopedGroup
from chrys.service.profiles.models.schema import ModelProfile
from tests.service.context.compaction._last_words_helpers import (
    CharacterTokenizer,
    FailingFallbackClient,
    FakeCompleter,
    FallbackClient,
    generate,
    make_generator,
    retry_collector,
    structured_note,
    user,
)
from tests.support.provider_errors import anthropic_status, openai_status

pytestmark = pytest.mark.usefixtures("no_note_floor")


@pytest.fixture
def slice_budget_capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Record the slice budget the generator solves for, keeping the real slicing behavior.

    ``prepare_scoped_slice`` is patched on the ``last_words`` module because the
    production code imports it by name — patching ``scoped`` would not take.
    """
    captured: dict[str, int] = {}
    original_prepare = last_words_mod.prepare_scoped_slice

    def _capture_budget(groups, *, tokenizer, slice_budget):  # type: ignore[no-untyped-def]
        captured["slice_budget"] = slice_budget
        return original_prepare(groups, tokenizer=tokenizer, slice_budget=slice_budget)

    monkeypatch.setattr(last_words_mod, "prepare_scoped_slice", _capture_budget)
    return captured


async def test_completer_path_success_bypasses_reconstruction(tmp_path):
    """A successful side call returns the note without touching the fallback client."""
    gen = make_generator(tmp_path, template="TEMPLATE TEXT", max_output_tokens=1234)
    note = structured_note()
    completer = FakeCompleter([note])

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
    )

    assert out == note
    # The fallback client (and its route_kind="last-words" session) was never created.
    assert gen._client is None
    call = completer.calls[0]
    assert "TEMPLATE TEXT" in call["instruction"]
    assert call["max_output_tokens"] == 1234
    assert [message.text for message in call["base_messages"]] == ["do X"]


# ---------------------------------------------------------------------------
# Side-call usage reporting (Token Usage panel accounting)
# ---------------------------------------------------------------------------


async def test_completer_path_passes_usage_reporter_to_side_call(tmp_path):
    """The generator hands its usage hook to the completer, and the hook
    forwards raw provider usage to the constructor's ``report_usage``."""
    reported: list = []
    gen = make_generator(tmp_path, report_usage=reported.append)
    completer = FakeCompleter([structured_note()])

    await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
    )

    on_usage = completer.calls[0]["on_usage"]
    assert on_usage is not None
    usage = {"input_token_count": 9, "output_token_count": 4, "total_token_count": 13}
    on_usage(usage)
    assert reported == [usage]


async def test_fallback_path_reports_side_call_usage(tmp_path):
    """The reconstruction fallback reports provider usage from its response."""
    reported: list = []
    gen = make_generator(tmp_path, report_usage=reported.append)
    note = structured_note()
    usage = {"input_token_count": 7, "output_token_count": 3, "total_token_count": 10}

    class _UsageResponse:
        raw_text = note
        usage_details = usage
        additional_properties: ClassVar[dict[str, object]] = {}
        messages: ClassVar[tuple[Message, ...]] = ()

    class _UsageClient:
        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            return _UsageResponse()

    gen._client = _UsageClient()  # type: ignore[assignment]

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
    )

    assert out == note
    assert reported == [usage]


async def test_fallback_path_reports_the_usage_of_a_response_the_adapter_failed(tmp_path):
    """A response the adapter fails (here a refusal that asks for calls)
    consumed provider tokens too."""
    reported: list = []
    gen = make_generator(tmp_path, report_usage=reported.append)
    usage = {"input_token_count": 7, "output_token_count": 3, "total_token_count": 10}
    error = ProviderResponseError("content_filter", "Refused.", retryable=False, usage_details=usage)
    gen._client = FailingFallbackClient(error)  # type: ignore[assignment]

    with pytest.raises(LastWordsGenerationError):
        await generate(
            gen,
            user_request="do X",
            previous_last_words=None,
            dropped_messages=[],
        )

    assert reported == [usage]


def test_report_side_call_usage_swallows_hook_errors(tmp_path):
    """A raising usage sink must never break note generation."""

    def _boom(_usage):  # type: ignore[no-untyped-def]
        raise RuntimeError("panel gone")

    gen = make_generator(tmp_path, report_usage=_boom)

    gen._report_side_call_usage({"total_token_count": 5})  # must not raise


def test_report_side_call_usage_skips_empty_and_unset(tmp_path):
    """Empty payloads are dropped, and a hook-less generator is a no-op."""
    reported: list = []
    gen = make_generator(tmp_path, report_usage=reported.append)
    gen._report_side_call_usage({})
    assert reported == []

    gen_no_hook = make_generator(tmp_path)
    gen_no_hook._report_side_call_usage({"total_token_count": 5})  # no hook: no-op


async def test_completer_instruction_wraps_guidance_in_triple_no_tools_directive(tmp_path):
    """The client layer forces ``tool_choice="none"``, but that cannot stop
    tool-call markup emitted as plain text, so the instruction still
    suppresses tool use in prose: once before and twice after the guidance,
    with the supplement intact between.  The opener and the final check both
    state the output-token cap so the model sizes the note to avoid
    ``finish_reason:"length"`` truncation."""
    from chrys.service.context.compaction.last_words import (
        _MIN_NOTE_TOKENS,
        _NO_TOOLS_FINAL,
        _NO_TOOLS_OPENER,
        _NO_TOOLS_REMINDER,
    )

    gen = make_generator(tmp_path, template="TEMPLATE TEXT", max_output_tokens=1234)
    completer = FakeCompleter([structured_note()])

    await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
    )

    instruction = completer.calls[0]["instruction"]
    opener = _NO_TOOLS_OPENER.format(min_note_tokens=_MIN_NOTE_TOKENS, max_output_tokens=1234)
    final = _NO_TOOLS_FINAL.format(min_note_tokens=_MIN_NOTE_TOKENS, max_output_tokens=1234)
    for directive in (opener, _NO_TOOLS_REMINDER, final):
        assert directive in instruction
        assert "do not call any tools" in directive.lower()
    assert "Scope — what to summarise:" not in instruction
    assert "Everything in this conversation is the current, in-progress task." in instruction
    assert "inherited state, not user input" in instruction
    # Anti-restart guard: "## Next" must never claim completion unless the
    # user-facing reply was actually delivered.
    assert "this note itself delivers nothing to the user" in _BASE_GUIDANCE
    # Section order: opener < scoped body < contract < base < supplement < reminder < final.
    assert instruction.index(opener) < instruction.index("Everything in this conversation")
    assert instruction.index("Everything in this conversation") < instruction.index(_FORMAT_CONTRACT)
    assert instruction.index(_FORMAT_CONTRACT) < instruction.index(_BASE_GUIDANCE)
    assert instruction.index(_BASE_GUIDANCE) < instruction.index(_SUPPLEMENT_LABEL)
    assert instruction.index(_SUPPLEMENT_LABEL) < instruction.index("TEMPLATE TEXT")
    assert instruction.index("TEMPLATE TEXT") < instruction.index(_NO_TOOLS_REMINDER)
    assert instruction.index(_NO_TOOLS_REMINDER) < instruction.index(final)
    # The cap is stated before the guidance and again in the final check,
    # alongside the 500-token minimum and the no-deliberation hint.
    assert "1234-token output cap" in opener
    assert "1234-token output cap" in final
    assert "at least 500 tokens" in opener
    assert "at least 500 tokens" in final
    assert "do not deliberate" in opener.lower()
    assert "deliberation" in final.lower()


@pytest.mark.parametrize("template", ["", " \n\t", "TEMPLATE TEXT"])
async def test_instruction_always_has_contract_and_base_with_optional_labeled_supplement(tmp_path, template):
    gen = make_generator(tmp_path, template=template)
    completer = FakeCompleter([structured_note()])

    await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
    )

    instruction = completer.calls[0]["instruction"]
    assert instruction.index("inherited state, not user input") < instruction.index(_FORMAT_CONTRACT)
    assert instruction.index(_FORMAT_CONTRACT) < instruction.index(_BASE_GUIDANCE)
    if template.strip():
        assert instruction.index(_BASE_GUIDANCE) < instruction.index(_SUPPLEMENT_LABEL)
        assert instruction.index(_SUPPLEMENT_LABEL) < instruction.index(template)
    else:
        assert _SUPPLEMENT_LABEL not in instruction


async def test_completer_fixed_guidance_survives_supplement_truncation(tmp_path):
    from chrys.service.context.compaction.last_words import (
        _FALLBACK_TEMPLATE_MAX_CHARS,
        _FALLBACK_TEMPLATE_TRUNCATION_MARKER,
    )

    template = "T" * (_FALLBACK_TEMPLATE_MAX_CHARS * 2)
    gen = make_generator(tmp_path, template=template)
    completer = FakeCompleter([structured_note()])

    await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
    )

    instruction = completer.calls[0]["instruction"]
    assert instruction.index(_FORMAT_CONTRACT) < instruction.index(_BASE_GUIDANCE)
    assert instruction.index(_BASE_GUIDANCE) < instruction.index(_SUPPLEMENT_LABEL)
    assert _FALLBACK_TEMPLATE_TRUNCATION_MARKER.strip() in instruction
    assert template not in instruction


async def test_completer_transient_failure_retries_then_succeeds(tmp_path, monkeypatch):
    """Transient side-call errors burn the local retry budget, not the fallback."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator

    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0, 0))

    retry_events, publish_retry = retry_collector()

    gen = make_generator(tmp_path, publish_retry=publish_retry)
    completer = FakeCompleter([ConnectionError("connection dropped"), structured_note()])

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
    )

    assert out == structured_note()
    assert len(completer.calls) == 2
    assert gen._client is None
    assert retry_events == [RetryAttemptInfo(reason="connection dropped", attempt=1, max_attempts=2, delay_seconds=0)]


@pytest.mark.parametrize(
    ("completer_result", "expected_completer_calls"),
    [
        pytest.param(ConnectionError("still down"), 3, id="retry_budget_exhausted"),
        pytest.param(
            RuntimeError("400: prompt is too long: 210000 tokens > 200000 maximum"),
            1,
            id="context_window_rejection",
        ),
        pytest.param(LastWordsToolCallError("tool call in summarization response"), 3, id="illegal_tool_calls"),
        pytest.param(ValueError("malformed request"), 1, id="non_retryable_error"),
        pytest.param("   ", 3, id="empty_responses"),
    ],
)
async def test_completer_failure_demotes_to_fallback(
    tmp_path,
    monkeypatch,
    completer_result: object,
    expected_completer_calls: int,
) -> None:
    """Every side-call failure mode ends on the reconstruction fallback.

    Retryable outcomes (transport failure, illegal tool calls, empty responses)
    burn the fixed 2-retry budget first; a provider context-window rejection and
    a non-retryable error demote immediately, because the same snapshot cannot
    be made to fit by retrying.
    """
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))

    gen = make_generator(tmp_path)
    fallback = FallbackClient()
    gen._client = fallback  # type: ignore[assignment]
    completer = FakeCompleter([completer_result])

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
    )

    assert out == structured_note()
    assert len(completer.calls) == expected_completer_calls
    assert fallback.calls == 1


async def test_last_words_backs_off_on_429_too_many_tokens(tmp_path, monkeypatch) -> None:
    """A 429 that mentions tokens is throttling: back off and retry, never demote."""
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0, 0))
    retry_events, publish_retry = retry_collector()
    gen = make_generator(tmp_path, publish_retry=publish_retry)
    fallback = FallbackClient()
    gen._client = fallback  # type: ignore[assignment]
    throttled = await openai_status(
        429,
        {
            "error": {
                "type": "tokens",
                "code": "rate_limit_exceeded",
                "message": "Too many tokens, please wait before trying again.",
            }
        },
    )
    completer = FakeCompleter([throttled, structured_note()])

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
    )

    assert out == structured_note()
    assert len(completer.calls) == 2
    assert fallback.calls == 0
    assert len(retry_events) == 1


async def test_last_words_demotes_on_prompt_too_long(tmp_path, monkeypatch) -> None:
    """A provider context-window rejection demotes to the fallback without retrying."""
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    gen = make_generator(tmp_path)
    fallback = FallbackClient()
    gen._client = fallback  # type: ignore[assignment]
    rejected = await anthropic_status(
        400,
        {
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "prompt is too long: 210000 tokens > 200000 maximum"},
        },
    )
    completer = FakeCompleter([rejected])

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
    )

    assert out == structured_note()
    assert len(completer.calls) == 1
    assert fallback.calls == 1


async def test_last_words_demotes_on_a_server_error_naming_the_context_window(tmp_path, monkeypatch) -> None:
    """A gateway may wrap an overflow in a 5xx: demote at once rather than resend the same snapshot."""
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    gen = make_generator(tmp_path)
    fallback = FallbackClient()
    gen._client = fallback  # type: ignore[assignment]
    rejected = await openai_status(
        500, {"error": {"type": "server_error", "message": "This model's maximum context length is 128000 tokens."}}
    )
    completer = FakeCompleter([rejected])

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
    )

    assert out == structured_note()
    assert len(completer.calls) == 1
    assert fallback.calls == 1


async def test_completer_absent_uses_reconstruction_directly(tmp_path):
    """Without a completer the reconstruction path is used as before."""
    gen = make_generator(tmp_path)
    fallback = FallbackClient()
    gen._client = fallback  # type: ignore[assignment]

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
    )

    assert out == structured_note()
    assert fallback.calls == 1


async def test_invalid_hosted_tool_group_demotes_without_omitting_result(tmp_path):
    """A non-portable provider tool layout remains visible to the fallback."""
    gen = make_generator(tmp_path)
    fallback = FallbackClient()
    gen._client = fallback  # type: ignore[assignment]
    completer = FakeCompleter([structured_note()])
    hosted = Message(
        "assistant",
        [
            Content.from_search_tool_call("search-1", tool_name="web_search", arguments={"query": "x"}),
            Content.from_search_tool_result(
                "search-1",
                tool_name="web_search",
                result={"answer": "critical hosted result"},
            ),
        ],
    )
    groups = [
        ScopedGroup("opener", "user", (user("research this"),), True),
        ScopedGroup("hosted", "tool_call", (hosted,), False),
    ]

    out = await gen.generate(
        groups,
        None,
        degraded_opener=False,
        has_continuation_nudges=False,
        completer=completer,
    )

    assert out == structured_note()
    assert completer.calls == []
    assert fallback.calls == 1
    assert "critical hosted result" in fallback.messages[0][1].text


# ---------------------------------------------------------------------------
# Scoped instruction + bounded fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("use_completer", [False, True], ids=["fallback", "completer"])
async def test_instruction_nudge_line_is_conditional(tmp_path, use_completer: bool) -> None:
    """The resume-nudge line appears only when the scoped slice carried nudges, on both note paths."""
    if use_completer:
        gen = make_generator(tmp_path, template="TEMPLATE")
        with_nudge = FakeCompleter([structured_note()])
        without_nudge = FakeCompleter([structured_note()])
        await generate(
            gen,
            user_request="do X",
            previous_last_words=None,
            dropped_messages=[],
            has_continuation_nudges=True,
            completer=with_nudge,
        )
        await generate(
            gen,
            user_request="do X",
            previous_last_words=None,
            dropped_messages=[],
            completer=without_nudge,
        )
        instructions = [with_nudge.calls[0]["instruction"], without_nudge.calls[0]["instruction"]]
    else:
        gen = make_generator(tmp_path)
        fallback = FallbackClient(structured_note())
        gen._client = fallback  # type: ignore[assignment]
        await generate(
            gen,
            user_request="do X",
            previous_last_words=None,
            dropped_messages=[],
            has_continuation_nudges=True,
        )
        with_nudge_prompt = fallback.messages[0][0].text
        fallback.messages.clear()
        await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])
        instructions = [with_nudge_prompt, fallback.messages[0][0].text]

    assert "automatic resume nudges" in instructions[0]
    assert "automatic resume nudges" not in instructions[1]


async def test_completer_slice_budget_does_not_double_count_calibrated_tool_overhead(
    tmp_path,
    slice_budget_capture: dict[str, int],
) -> None:
    profile = ModelProfile(
        id="budget",
        name="budget",
        model_id="budget",
        max_context_tokens=20_000,
        max_output_tokens=1_000,
    )
    gen = LastWordsGenerator(profile=profile, template="TEMPLATE", max_output_tokens=300, log_dir=tmp_path)
    completer = FakeCompleter([structured_note()])
    tokenizer = CharacterTokenizer()
    groups = [ScopedGroup("opener", "user", (user("do X"),), True)]

    await gen.generate(
        groups,
        None,
        degraded_opener=False,
        has_continuation_nudges=False,
        completer=completer,
        tokenizer=tokenizer,
        system_overhead_tokens=123,
        tool_definition_tokens=456,
    )

    instruction_tokens = tokenizer.count_tokens(completer.calls[0]["instruction"])
    assert slice_budget_capture["slice_budget"] == (
        profile.max_context_tokens - 456 - instruction_tokens - 300 - _SLICE_SAFETY_MARGIN_TOKENS
    )


async def test_completer_slice_budget_is_solved_in_calibrated_space(
    tmp_path,
    slice_budget_capture: dict[str, int],
) -> None:
    profile = ModelProfile(
        id="calibrated-budget",
        name="calibrated-budget",
        model_id="calibrated-budget",
        max_context_tokens=20_000,
        max_output_tokens=1_000,
    )
    gen = LastWordsGenerator(profile=profile, template="TEMPLATE", max_output_tokens=300, log_dir=tmp_path)
    completer = FakeCompleter([structured_note()])
    tokenizer = CharacterTokenizer()

    await gen.generate(
        [ScopedGroup("opener", "user", (user("do X"),), True)],
        None,
        degraded_opener=False,
        has_continuation_nudges=False,
        completer=completer,
        tokenizer=tokenizer,
        request_overhead_tokens=456,
        calibration_ratio=1.5,
    )

    instruction_tokens = tokenizer.count_tokens(completer.calls[0]["instruction"])
    usable_raw = math.floor((20_000 - 300 - _SLICE_SAFETY_MARGIN_TOKENS) / 1.5)
    assert slice_budget_capture["slice_budget"] == usable_raw - 456 - instruction_tokens
