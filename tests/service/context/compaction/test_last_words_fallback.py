# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The reconstruction fallback: prompt composition, option allowlist, output budgets and the admission ladder."""

from __future__ import annotations

import asyncio
import base64
import json
from types import ModuleType
from typing import Any, ClassVar

import pytest
from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice
from openai.types.chat.chat_completion_message import ChatCompletionMessage

from chrys.foundation.errors import ProviderResponseError
from chrys.foundation.util.chrys_headers import X_SESSION_ID_HEADER
from chrys.kernel import Content, Message
from chrys.kernel.client import _ClientLastWordsCompleter, start_with_wire_progress
from chrys.service.context.compaction.last_words import (
    _BASE_GUIDANCE,
    _FORMAT_CONTRACT,
    _SUPPLEMENT_LABEL,
    LastWordsGenerationError,
    LastWordsGenerator,
    LastWordsSpendBudgetExceeded,
)
from chrys.service.llm import one_shot
from chrys.service.llm.chat_completions import ChatCompletionsClient
from chrys.service.llm.chat_completions import client as chat_completions_client
from chrys.service.profiles.agents.schema import DEFAULT_LAST_WORDS_MAX_OUTPUT_TOKENS
from chrys.service.profiles.models.options import (
    AUTO_INTERLEAVED_THINKING_OPTION,
    STREAM_REQUIRES_FINISH_REASON_OPTION,
    THINKING_BLOCK_BINDING_OPTION,
)
from chrys.service.profiles.models.schema import API_STYLE_RESPONSES, ModelProfile
from tests.service.context.compaction._compaction_helpers import _anthropic_fetched_pdf_exchange
from tests.service.context.compaction._last_words_helpers import (
    FailingFallbackClient,
    FakeCompleter,
    FallbackClient,
    generate,
    long_structured_note,
    make_generator,
    retry_collector,
    structured_note,
    user,
)
from tests.support.openai_chat_wire import ChatReply, scripted_openai
from tests.support.provider_errors import openai_status
from tests.support.scripted_wire import ScriptedWire, pin_wire_inputs, route_clients_to
from tests.support.wire_cases._kit import anth_replies, anth_text, resp_message, resp_replies, resp_response

pytestmark = pytest.mark.usefixtures("no_note_floor")


def _note_chunk(text: str, finish_reason: str | None = None) -> ChatCompletionChunk:
    delta = ChoiceDelta.model_construct(role="assistant", content=text)
    return ChatCompletionChunk.model_construct(
        id="chunk-1",
        object="chat.completion.chunk",
        created=1_717_171_717,
        model="model",
        choices=[ChunkChoice.model_construct(index=0, delta=delta, finish_reason=finish_reason)],
        usage=None,
    )


def _note_asking_for_a_tool(text: str, name: str | None) -> ChatCompletionChunk:
    """A chunk with *text* and a whole call to *name* (none when None), finishing its choice."""
    function = ChoiceDeltaToolCallFunction.model_construct(arguments='{"path": "x"}', name=name)
    call = ChoiceDeltaToolCall.model_construct(index=0, id="call_1", type="function", function=function)
    delta = ChoiceDelta.model_construct(role="assistant", content=text, tool_calls=[call])
    return ChatCompletionChunk.model_construct(
        id="chunk-1",
        object="chat.completion.chunk",
        created=1_717_171_717,
        model="model",
        choices=[ChunkChoice.model_construct(index=0, delta=delta, finish_reason="tool_calls")],
        usage=None,
    )


def _note_completion(text: str, finish_reason: str) -> ChatCompletion:
    message = ChatCompletionMessage.model_construct(role="assistant", content=text)
    return ChatCompletion.model_construct(
        id="completion-1",
        object="chat.completion",
        created=1_717_171_717,
        model="model",
        choices=[Choice.model_construct(index=0, message=message, finish_reason=finish_reason)],
        usage=None,
    )


@pytest.mark.parametrize("template", ["", " \n\t", "TEMPLATE TEXT"])
async def test_fallback_always_has_contract_and_base_with_optional_labeled_supplement(tmp_path, template):
    from chrys.service.context.compaction.last_words import (
        _MIN_NOTE_TOKENS,
        _NO_TOOLS_FINAL,
        _NO_TOOLS_OPENER,
        _NO_TOOLS_REMINDER,
    )

    gen = make_generator(tmp_path, template=template)
    fallback = FallbackClient(structured_note())
    gen._client = fallback  # type: ignore[assignment]

    await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    instruction = fallback.messages[0][0].text
    opener = _NO_TOOLS_OPENER.format(min_note_tokens=_MIN_NOTE_TOKENS, max_output_tokens=20_000)
    final = _NO_TOOLS_FINAL.format(min_note_tokens=_MIN_NOTE_TOKENS, max_output_tokens=20_000)
    assert instruction.index(opener) < instruction.index("Everything in this conversation")
    assert instruction.index("<previous_progress_note>") < instruction.index(_FORMAT_CONTRACT)
    assert instruction.index(_FORMAT_CONTRACT) < instruction.index(_BASE_GUIDANCE)
    if template.strip():
        assert instruction.index(_BASE_GUIDANCE) < instruction.index(_SUPPLEMENT_LABEL)
        assert instruction.index(_SUPPLEMENT_LABEL) < instruction.index(template)
        assert instruction.index(template) < instruction.index(_NO_TOOLS_REMINDER)
    else:
        assert _SUPPLEMENT_LABEL not in instruction
        assert instruction.index(_BASE_GUIDANCE) < instruction.index(_NO_TOOLS_REMINDER)
    assert instruction.index(_NO_TOOLS_REMINDER) < instruction.index(final)


async def test_fallback_prompt_states_length_contract(tmp_path):
    """The reconstruction prompt states the 500-token minimum and the output cap.

    The completer instruction carries this contract in its no-tools wrapper;
    the fallback prompt must state it too, or the base guidance's "keep the
    note tight" direction would steer compliant models under the note floor."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    captured: dict = {}

    class _CapturingClient:
        async def get_response(self, messages, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            captured["user_prompt"] = messages[1].text

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = structured_note()

            return _Response()

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path)
    gen._client = _CapturingClient()  # type: ignore[assignment]

    await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    prompt = captured["user_prompt"]
    assert "at least 500 tokens" in prompt
    assert "20000-token output cap" in prompt  # DEFAULT_LAST_WORDS_MAX_OUTPUT_TOKENS


@pytest.mark.parametrize(
    (
        "profile",
        "generator_kwargs",
        "expected_side_call_max",
        "expected_stated_cap",
        "expected_wire_max",
        "expected_thinking",
    ),
    [
        pytest.param(
            ModelProfile(id="t", name="t", model_id="deepseek-chat", max_output_tokens=8192, stream=False),
            {},
            8192,
            "8192-token output cap",
            8192,
            None,
            id="model_output_cap_clamps_both_note_paths",
        ),
        pytest.param(
            ModelProfile(
                id="t",
                name="t",
                provider="anthropic",
                model_id="claude-test",
                max_output_tokens=0,  # unknown cap — this case targets the unclamped math
                chat_options='{"thinking": {"type": "enabled", "budget_tokens": 16000}}',
                stream=False,
            ),
            {"max_output_tokens": 12000},
            12000 + 16000,
            "12000-token output cap",
            12000 + 16000,
            {"type": "enabled", "budget_tokens": 16000},
            id="thinking_budget_added_to_wire_max_tokens",
        ),
        pytest.param(
            ModelProfile(
                id="t",
                name="t",
                model_id="deepseek-chat",
                max_output_tokens=8192,
                chat_options='{"max_tokens": 20000}',
                stream=False,
            ),
            {},
            8192,
            None,
            8192,
            None,
            id="profile_max_tokens_cannot_override_note_call_clamp",
        ),
    ],
)
async def test_note_call_output_budget_is_clamped_on_both_paths(
    tmp_path,
    profile: ModelProfile,
    generator_kwargs: dict,
    expected_side_call_max: int,
    expected_stated_cap: str | None,
    expected_wire_max: int,
    expected_thinking: dict | None,
) -> None:
    """The wire ``max_tokens`` and the stated note budget both respect the model cap.

    DeepSeek rejects ``max_tokens`` above 8192 outright, on the side call and on
    the reconstruction fallback alike, and a profile ``chat_options`` value must
    not undo the computed clamp.  Anthropic goes the other way: extended
    thinking spends from ``max_tokens``, so the wire value adds the thinking
    budget on top while the instruction keeps stating only the visible-note
    share.
    """
    gen = LastWordsGenerator(profile=profile, log_dir=tmp_path, **generator_kwargs)
    completer = FakeCompleter([structured_note()])
    captured: dict = {}

    class _CapturingClient:
        async def get_response(self, messages, *_args, **kwargs):  # type: ignore[no-untyped-def]
            captured["options"] = kwargs.get("options")

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = structured_note()

            return _Response()

    gen._client = _CapturingClient()  # type: ignore[assignment]

    await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
    )
    # Side call: clamped wire value, clamped stated budget.
    call = completer.calls[0]
    assert call["max_output_tokens"] == expected_side_call_max
    if expected_stated_cap is not None:
        assert expected_stated_cap in call["instruction"]

    # Fallback: same clamp via the reconstruction client options.
    await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])
    assert captured["options"]["max_tokens"] == expected_wire_max
    if expected_thinking is not None:
        # The thinking config itself rides through to the fallback call untouched.
        assert captured["options"]["thinking"] == expected_thinking


def test_output_budgets_math(tmp_path):
    """Budget corner cases: disabled thinking, cap-only, thinking re-clamped to cap."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.schema import ModelProfile

    def budgets(*, cap: int = 0, chat_options: str = "", configured: int = 12000) -> tuple[int, int]:
        profile = ModelProfile(id="t", name="t", max_output_tokens=cap, chat_options=chat_options)
        return LastWordsGenerator(profile=profile, log_dir=tmp_path, max_output_tokens=configured)._output_budgets()

    # No cap, no thinking: both values are the configured budget.
    assert budgets() == (12000, 12000)
    # Disabled thinking is ignored.
    assert budgets(chat_options='{"thinking": {"type": "disabled", "budget_tokens": 16000}}') == (12000, 12000)
    # Cap alone clamps both.
    assert budgets(cap=8192) == (8192, 8192)
    # Thinking budget rides on top of the wire value only.
    assert budgets(chat_options='{"thinking": {"type": "enabled", "budget_tokens": 16000}}') == (12000, 28000)
    # Cap re-clamps the combined wire value; the stated note share shrinks.
    assert budgets(cap=20000, chat_options='{"thinking": {"type": "enabled", "budget_tokens": 16000}}') == (
        4000,
        20000,
    )
    # No explicit cap: a user-set profile max_tokens is the ceiling instead
    # (provider-validated by every live call).
    assert budgets(chat_options='{"max_tokens": 8000}') == (8000, 8000)
    # A generous profile max_tokens does not inflate the note budget.
    assert budgets(chat_options='{"max_tokens": 20000}') == (12000, 12000)
    # The explicit field wins over the chat-options fallback.
    assert budgets(cap=8192, chat_options='{"max_tokens": 20000}') == (8192, 8192)
    # Non-integer / non-positive max_tokens values are ignored, not crashes.
    assert budgets(chat_options='{"max_tokens": "8000"}') == (12000, 12000)
    assert budgets(chat_options='{"max_tokens": 0}') == (12000, 12000)
    # Provider-native output-cap spellings serve as the ceiling too
    # (programmatic profiles bypass the loader migration).
    assert budgets(chat_options='{"max_output_tokens": 4096}') == (4096, 4096)
    assert budgets(chat_options='{"max_completion_tokens": 4096}') == (4096, 4096)
    # A bool budget_tokens is malformed, not a 1-token thinking budget.
    assert budgets(chat_options='{"thinking": {"type": "enabled", "budget_tokens": true}}') == (12000, 12000)


def test_output_budgets_warns_when_thinking_squeezes_note_below_prompt_minimum(tmp_path, caplog):
    """A thinking budget just under the cap silently starved the note before.

    The prompt asks for at least 500 tokens of note content; when the
    clamped note share falls below that, every attempt is truncated under
    the note floor and retried — warn instead of failing silently.  When
    the thinking budget meets/exceeds the cap the provider rejects the call
    outright, which has its own (mutually exclusive) warning.
    """
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.schema import ModelProfile

    def budgets(chat_options: str) -> tuple[int, int]:
        profile = ModelProfile(id="t", name="t", max_output_tokens=8192, chat_options=chat_options)
        return LastWordsGenerator(profile=profile, log_dir=tmp_path, max_output_tokens=12000)._output_budgets()

    with caplog.at_level("WARNING"):
        assert budgets('{"thinking": {"type": "enabled", "budget_tokens": 8100}}') == (92, 8192)
    assert "below the 500-token minimum" in caplog.text
    assert "meets or exceeds" not in caplog.text

    caplog.clear()
    with caplog.at_level("WARNING"):
        budgets('{"thinking": {"type": "enabled", "budget_tokens": 8192}}')
    assert "meets or exceeds" in caplog.text
    assert "below the 500-token minimum" not in caplog.text


async def test_fallback_uses_three_scoped_blocks_and_interleaves_followups(tmp_path):
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    captured: dict = {}

    class _Client:
        async def get_response(self, messages, *_args, **kwargs):  # type: ignore[no-untyped-def]
            captured["prompt"] = messages[1].text
            captured["options"] = kwargs["options"]

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = structured_note()

            return _Response()

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path)
    gen._client = _Client()  # type: ignore[assignment]
    await generate(
        gen,
        user_request="fix it",
        previous_last_words="previous",
        dropped_messages=[Message("assistant", ["working"])],
        followup_texts=["also test it"],
    )

    prompt = captured["prompt"]
    assert all(f"<{tag}>" in prompt for tag in ("user_request", "previous_progress_note", "work_done_since"))
    assert "<prior_conversation>" not in prompt
    assert "<injected_followups>" not in prompt
    assert "- user said: also test it" in prompt


async def test_fallback_renders_an_image_result_as_a_placeholder_not_base64(tmp_path):
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    captured: dict = {}

    class _Client:
        async def get_response(self, messages, *_args, **kwargs):  # type: ignore[no-untyped-def]
            captured["prompt"] = messages[1].text

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = structured_note()

            return _Response()

    image = Content.from_data(data=b"\x89PNG" + b"\x00" * 3000, media_type="image/png")
    dropped = [
        Message("assistant", [Content.from_function_call("call-1", "read_file", arguments={"path": "shot.png"})]),
        Message(
            "tool",
            [Content.from_function_result("call-1", result=[Content.from_text("Read shot.png"), image])],
        ),
    ]
    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path)
    gen._client = _Client()  # type: ignore[assignment]
    await generate(gen, user_request="look at it", previous_last_words=None, dropped_messages=dropped)

    prompt = captured["prompt"]
    assert "  result[read_file]: Read shot.png\n[image/png image]" in prompt
    assert image.uri is not None
    assert image.uri.split(",", 1)[1][:64] not in prompt


@pytest.mark.parametrize("restored", [False, True], ids=["live", "restored"])
async def test_fallback_renders_a_hosted_base64_document_as_a_placeholder(tmp_path, restored):
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    captured: dict = {}

    class _Client:
        async def get_response(self, messages, *_args, **kwargs):  # type: ignore[no-untyped-def]
            captured["prompt"] = messages[1].text

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = structured_note()

            return _Response()

    payload = base64.b64encode(b"%PDF-1.7\n" + b"binary-payload" * 150).decode()
    dropped = _anthropic_fetched_pdf_exchange(payload)
    if restored:
        dropped = [Message.from_dict(message.to_dict()) for message in dropped]
    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path)
    gen._client = _Client()  # type: ignore[assignment]
    await generate(gen, user_request="read the paper", previous_last_words=None, dropped_messages=dropped)

    prompt = captured["prompt"]
    assert "[application/pdf artifact]" in prompt
    assert payload[:64] not in prompt


async def test_fallback_option_allowlist_drops_all_input_shaping_fields(tmp_path):
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.schema import ModelProfile

    captured: dict = {}

    class _Client:
        async def get_response(self, _messages: list[Message], *, stream: bool, options: dict[str, object]) -> object:
            # Every setting reaches the client inside the options, never as a keyword of its own.
            assert stream is False
            captured.update(options)

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = structured_note()

            return _Response()

    profile = ModelProfile(
        id="bounded",
        name="bounded",
        model_id="model",
        chat_options=(
            '{"model":"safe","temperature":0.2,"reasoning_effort":"low",'
            '"instructions":"huge","tools":[{"type":"function"}],"tool_choice":"required",'
            '"response_format":{"type":"json_schema"},"schema":{"huge":true},"store":true,'
            '"previous_response_id":"resp","conversation_id":"conv","unknown_input":"drop"}'
        ),
        stream=False,
    )
    gen = LastWordsGenerator(profile=profile, log_dir=tmp_path)
    gen._client = _Client()  # type: ignore[assignment]
    await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert captured == {
        "model": "safe",
        "temperature": 0.2,
        "reasoning_effort": "low",
        "max_tokens": DEFAULT_LAST_WORDS_MAX_OUTPUT_TOKENS,
    }


@pytest.mark.parametrize(
    ("chat_options", "expected_key"),
    [
        ({}, "note-session"),
        ({"prompt_cache_key": "mine"}, "mine"),
        ({"extra_body": {"prompt_cache_key": "nested", "user_tag": "drop"}}, "nested"),
        ({"prompt_cache_key": None}, None),
        ({"extra_body": {"prompt_cache_key": None}}, None),
    ],
    ids=["automatic", "top_level", "extra_body", "top_level_null", "extra_body_null"],
)
async def test_a_fallback_note_keeps_the_prompt_cache_key_the_profile_sets(
    tmp_path, monkeypatch: pytest.MonkeyPatch, chat_options: dict[str, object], expected_key: str | None
) -> None:
    pin_wire_inputs(monkeypatch)
    note = resp_response(response_id="resp_note", output=[resp_message("msg_note", structured_note())])
    wire = ScriptedWire(resp_replies([note], stream=False))
    route_clients_to(wire.transport, monkeypatch)
    profile = ModelProfile(
        id="cache",
        name="cache",
        provider="openai",
        api_style=API_STYLE_RESPONSES,
        model_id="model",
        api_key="sk-cache",
        chat_options=json.dumps(chat_options),
        stream=False,
    )
    gen = LastWordsGenerator(profile=profile, log_dir=tmp_path, session_id="note-session")
    try:
        await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])
    finally:
        await gen.aclose()

    [request] = wire.requests
    body = json.loads(request.content)
    assert request.headers[X_SESSION_ID_HEADER] == "note-session"
    assert body.get("prompt_cache_key") == expected_key
    assert ("prompt_cache_key" in body) is (expected_key is not None)
    assert "user_tag" not in body


@pytest.mark.parametrize(
    ("binding", "interleaved", "expected"),
    [
        ("error", True, {THINKING_BLOCK_BINDING_OPTION: "error"}),
        ("off", True, {THINKING_BLOCK_BINDING_OPTION: "off"}),
        ("auto", False, {AUTO_INTERLEAVED_THINKING_OPTION: False}),
    ],
    ids=["error", "off", "interleaved_off"],
)
async def test_a_fallback_note_keeps_the_profile_thinking_settings(
    tmp_path, binding: Any, interleaved: bool, expected: dict[str, object]
) -> None:
    captured: dict = {}

    class _Client:
        async def get_response(self, _messages: list[Message], *, stream: bool, options: dict[str, object]) -> object:
            # Every setting reaches the client inside the options, never as a keyword of its own.
            assert stream is False
            captured.update(options)

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = structured_note()

            return _Response()

    profile = ModelProfile(
        id="claude",
        name="claude",
        provider="anthropic",
        model_id="claude-opus-5-5",
        stream=False,
        thinking_block_binding=binding,
        auto_interleaved_thinking=interleaved,
    )
    gen = LastWordsGenerator(profile=profile, log_dir=tmp_path)
    gen._client = _Client()  # type: ignore[assignment]
    await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert captured == {"max_tokens": DEFAULT_LAST_WORDS_MAX_OUTPUT_TOKENS, **expected}


@pytest.mark.parametrize("interleaved", [True, False], ids=["automatic", "turned_off"])
async def test_a_streamed_fallback_note_sends_the_interleaved_beta_as_the_profile_says(
    tmp_path, monkeypatch: pytest.MonkeyPatch, interleaved: bool
) -> None:
    pin_wire_inputs(monkeypatch)
    wire = ScriptedWire(anth_replies([anth_text(structured_note(), message_id="msg_note")], stream=True))
    route_clients_to(wire.transport, monkeypatch)
    profile = ModelProfile(
        id="claude",
        name="claude",
        provider="anthropic",
        model_id="claude-sonnet-4-5",
        api_key="sk-ant-note",
        chat_options=json.dumps({"thinking": {"type": "enabled", "budget_tokens": 1024}}),
        stream=True,
        auto_interleaved_thinking=interleaved,
    )
    gen = LastWordsGenerator(profile=profile, log_dir=tmp_path)
    try:
        await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])
    finally:
        await gen.aclose()

    [request] = wire.requests
    [betas] = request.headers.get_list("anthropic-beta")
    assert ("interleaved-thinking-2025-05-14" in betas.split(",")) is interleaved
    assert AUTO_INTERLEAVED_THINKING_OPTION not in json.loads(request.content)


@pytest.mark.parametrize("requires_finish_reason", [True, False], ids=["requires_a_finish_reason", "lenient"])
async def test_a_fallback_note_streamed_without_a_finish_reason_is_judged_as_the_profile_says(
    tmp_path, requires_finish_reason: bool
) -> None:
    note = structured_note()
    profile = ModelProfile(
        id="cut",
        name="cut",
        model_id="model",
        stream=True,
        stream_requires_finish_reason=requires_finish_reason,
    )
    gen = LastWordsGenerator(profile=profile, log_dir=tmp_path, max_transient_retries=0)
    async with scripted_openai([[_note_chunk(note)]]) as wire:
        gen._client = ChatCompletionsClient(model="model", sdk_client=wire.client)  # type: ignore[assignment]
        if requires_finish_reason:
            with pytest.raises(LastWordsGenerationError) as raised:
                await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])
            cause = raised.value.__cause__
            assert isinstance(cause, ProviderResponseError)
            assert cause.code == "stream_truncated"
        else:
            assert await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[]) == note

    [request] = wire.requests
    assert STREAM_REQUIRES_FINISH_REASON_OPTION not in request


async def test_fallback_caps_supplement_and_middle_truncates_previous_note(tmp_path):
    from chrys.service.context.compaction.last_words import (
        _FALLBACK_PREV_NOTE_MAX_CHARS,
        _FALLBACK_TEMPLATE_MAX_CHARS,
        LastWordsGenerator,
    )
    from chrys.service.profiles.models.resolver import default_profile

    captured: dict = {}

    class _Client:
        async def get_response(self, messages, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            captured["system"] = messages[0].text
            captured["prompt"] = messages[1].text

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = structured_note()

            return _Response()

    template = "T" * (_FALLBACK_TEMPLATE_MAX_CHARS * 20)
    previous = "START" + "p" * (_FALLBACK_PREV_NOTE_MAX_CHARS * 4) + "FRESHEST"
    gen = LastWordsGenerator(profile=default_profile(), template=template, log_dir=tmp_path)
    gen._client = _Client()  # type: ignore[assignment]
    await generate(gen, user_request="do X", previous_last_words=previous, dropped_messages=[])

    system_instruction = captured["system"]
    supplement = system_instruction.split(f"{_SUPPLEMENT_LABEL}\n", 1)[1].split("\n\nReminder:", 1)[0]
    assert _FORMAT_CONTRACT in system_instruction
    assert _BASE_GUIDANCE in system_instruction
    assert len(supplement) <= _FALLBACK_TEMPLATE_MAX_CHARS
    assert "template truncated" in supplement
    assert "START" in captured["prompt"]
    assert "FRESHEST" in captured["prompt"]
    assert "older note content truncated" in captured["prompt"]


def test_fallback_admission_counter_is_utf8_byte_conservative() -> None:
    from chrys.service.context.compaction.last_words import _fallback_admission_tokens

    ascii_request = (Message("system", ["a"]), Message("user", ["plain"] * 4))
    adversarial = (Message("system", ["🙂"]), Message("user", ['é\\"🙂'] * 4))
    assert _fallback_admission_tokens(adversarial, output_reserve=0) > _fallback_admission_tokens(
        ascii_request,
        output_reserve=0,
    )


async def test_fallback_shrinks_timeline_without_truncating_fixed_guidance(tmp_path):
    from chrys.service.context.compaction.last_words import (
        _FALLBACK_TEMPLATE_COMPACT_CHARS,
        LastWordsGenerator,
        _fallback_admission_tokens,
    )
    from chrys.service.profiles.models.schema import ModelProfile

    captured: dict = {}

    class _Client:
        async def get_response(self, messages, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            captured["messages"] = tuple(messages)

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = structured_note()

            return _Response()

    profile = ModelProfile(
        id="small",
        name="small",
        model_id="small",
        max_context_tokens=12_000,
        max_output_tokens=500,
        stream=False,
    )
    gen = LastWordsGenerator(profile=profile, template="T" * 100_000, log_dir=tmp_path)
    gen._client = _Client()  # type: ignore[assignment]
    previous = "START" + "p" * 100_000 + "LATEST"
    await generate(
        gen,
        user_request="do X",
        previous_last_words=previous,
        dropped_messages=[Message("assistant", ["work " * 20_000])],
    )

    messages = captured["messages"]
    assert _FORMAT_CONTRACT in messages[0].text
    assert _BASE_GUIDANCE in messages[0].text
    assert _SUPPLEMENT_LABEL in messages[0].text
    assert "template truncated" in messages[0].text
    supplement = messages[0].text.split(f"{_SUPPLEMENT_LABEL}\n", 1)[1].split("\n\nReminder:", 1)[0]
    assert len(supplement) <= _FALLBACK_TEMPLATE_COMPACT_CHARS
    assert _fallback_admission_tokens(messages, output_reserve=500) <= profile.max_context_tokens


async def test_fallback_format_correction_is_readmitted_and_shrunk(tmp_path, monkeypatch):
    from chrys.service.context.compaction.last_words import (
        LastWordsGenerator,
        _fallback_admission_tokens,
    )
    from chrys.service.profiles.models.schema import ModelProfile

    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    invalid = "## Task\nDo it\n\n## Progress\n" + "work " * 100
    valid = long_structured_note()

    class _Client:
        def __init__(self) -> None:
            self.messages: list[tuple[Message, ...]] = []

        async def get_response(self, messages, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.messages.append(tuple(messages))

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = invalid if len(self.messages) == 1 else valid

            return _Response()

    profile = ModelProfile(
        id="correction-budget",
        name="correction-budget",
        model_id="correction-budget",
        # Calibrated between the first attempt's admission estimate and the
        # correction attempt's (larger, rejection-notice-bearing) one; growing
        # the shared guidance/contract text shifts both and moves this line.
        max_context_tokens=9_560,
        max_output_tokens=500,
        stream=False,
    )
    client = _Client()
    gen = LastWordsGenerator(profile=profile, log_dir=tmp_path)
    gen._client = client  # type: ignore[assignment]

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[Message("assistant", ["work " * 9_000])],
    )

    assert out == valid
    assert len(client.messages) == 2
    first, corrected = client.messages
    assert "Your previous note was rejected:" not in first[0].text
    assert 'Your previous note was rejected: missing required heading "## Next".' in corrected[0].text
    assert len(corrected[1].text) < len(first[1].text)
    assert _FORMAT_CONTRACT in corrected[0].text
    assert _BASE_GUIDANCE in corrected[0].text
    assert _fallback_admission_tokens(corrected, output_reserve=500) <= profile.max_context_tokens


async def test_fallback_tiny_window_sends_final_candidate_before_failing(tmp_path):
    """Local admission never dead-ends Phase 4: the terminal candidate is sent
    even when the byte-conservative estimate says it cannot fit, and only a
    genuine provider context rejection ends the shrink ladder."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.schema import ModelProfile

    class _Client:
        calls = 0

        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise RuntimeError("prompt is too long for this model")

    profile = ModelProfile(id="tiny", name="tiny", model_id="tiny", max_context_tokens=32, max_output_tokens=32)
    client = _Client()
    gen = LastWordsGenerator(profile=profile, log_dir=tmp_path)
    gen._client = client  # type: ignore[assignment]
    with pytest.raises(LastWordsGenerationError) as excinfo:
        await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])
    assert client.calls == 1
    assert isinstance(excinfo.value.__cause__, RuntimeError)


async def test_fallback_small_window_final_candidate_is_provider_authoritative(tmp_path):
    """A realistic small-context profile succeeds via the terminal candidate even
    though every candidate exceeds the byte-conservative admission estimate."""
    from chrys.service.context.compaction.last_words import (
        _MIN_NOTE_TOKENS,
        LastWordsGenerator,
        _fallback_admission_tokens,
    )
    from chrys.service.profiles.models.schema import ModelProfile

    captured: dict = {}

    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        async def get_response(self, messages, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            captured["messages"] = tuple(messages)
            captured["options"] = dict(_kwargs.get("options") or {})

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = structured_note()

            return _Response()

    profile = ModelProfile(
        id="small-window",
        name="small-window",
        model_id="small-window",
        max_context_tokens=9_000,
        max_output_tokens=8_192,
        stream=False,
    )
    client = _Client()
    gen = LastWordsGenerator(profile=profile, log_dir=tmp_path)
    gen._client = client  # type: ignore[assignment]

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[Message("assistant", ["work " * 5_000])],
    )

    assert out == structured_note()
    assert client.calls == 1
    messages = captured["messages"]
    assert _fallback_admission_tokens(messages, output_reserve=8_192) > profile.max_context_tokens
    # The output reserve is exact, so it must be clamped to the room the
    # conservative input estimate leaves — a provider enforcing
    # input + max_tokens <= context would reject the full 8_192 reserve.
    sent_max_tokens = captured["options"]["max_tokens"]
    input_estimate = _fallback_admission_tokens(messages, output_reserve=0)
    assert sent_max_tokens >= _MIN_NOTE_TOKENS
    assert input_estimate + sent_max_tokens <= profile.max_context_tokens
    # The prompt's length directive must advertise the clamped budget, not the
    # original one — otherwise the model writes past max_tokens and the note
    # truncates mid-section.
    assert f"{sent_max_tokens}-token" in messages[1].text
    assert "20000-token" not in messages[1].text


async def test_fallback_bypass_never_raises_max_tokens_above_model_cap(tmp_path):
    """The note floor must not push the bypass request above the configured
    output ceiling — providers enforcing the hard cap would reject it."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.schema import ModelProfile

    captured: dict = {}

    class _Client:
        def __init__(self) -> None:
            self.calls = 0

        async def get_response(self, messages, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            captured["options"] = dict(_kwargs.get("options") or {})

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = structured_note()

            return _Response()

    profile = ModelProfile(
        id="tiny-cap",
        name="tiny-cap",
        model_id="tiny-cap",
        max_context_tokens=2_000,
        max_output_tokens=256,
        stream=False,
    )
    client = _Client()
    gen = LastWordsGenerator(profile=profile, log_dir=tmp_path, max_output_tokens=256)
    gen._client = client  # type: ignore[assignment]

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[Message("assistant", ["work " * 2_000])],
    )

    assert out == structured_note()
    assert client.calls == 1
    assert captured["options"]["max_tokens"] == 256


async def test_fallback_short_note_acceptance_still_canonicalizes(tmp_path, monkeypatch):
    """A structurally valid note accepted below the length floor must still be
    canonicalized (heading levels normalized, sections ordered)."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator

    monkeypatch.setattr(LastWordsGenerator, "_MAX_CORRECTIVE_RETRIES", 0)
    short_relaxed = "# Task\nDo it\n\n### Progress\nStarted\n\n## Next\nFinish"

    class _Client:
        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = short_relaxed

            return _Response()

    gen = make_generator(tmp_path)
    gen._client = _Client()  # type: ignore[assignment]

    out = await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert out == "## Task\nDo it\n\n## Progress\nStarted\n\n## Next\nFinish"


async def test_provider_context_rejection_advances_fallback_shrink_sequence(tmp_path):
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    prompt_lengths: list[int] = []

    class _Client:
        async def get_response(self, messages, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            prompt_lengths.append(len(messages[1].text))
            if len(prompt_lengths) == 1:
                raise RuntimeError("maximum context length exceeded")

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = structured_note()

            return _Response()

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path)
    gen._client = _Client()  # type: ignore[assignment]
    await generate(
        gen,
        user_request="do X",
        previous_last_words="previous",
        dropped_messages=[Message("assistant", [f"work-{index} " * 1_000]) for index in range(20)],
    )
    assert len(prompt_lengths) == 2
    assert prompt_lengths[1] < prompt_lengths[0]


async def test_provider_context_rejection_traverses_shrink_ladder_at_zero_transient_budget(tmp_path):
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    class _Client:
        calls = 0

        async def get_response(self, _messages, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise RuntimeError("maximum context length exceeded")

    client = _Client()
    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path, max_transient_retries=0)
    gen._client = client  # type: ignore[assignment]

    with pytest.raises(LastWordsGenerationError) as exc_info:
        await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert client.calls == 5
    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert "maximum context length exceeded" in str(exc_info.value.__cause__)


async def _prompt_lengths_after(tmp_path, monkeypatch, rejection: BaseException) -> tuple[list[int], int]:  # type: ignore[no-untyped-def]
    """Fail the first fallback call with *rejection*; return each call's prompt length and the retries announced."""
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    retry_events, publish_retry = retry_collector()
    prompt_lengths: list[int] = []

    class _Client:
        async def get_response(self, messages, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            prompt_lengths.append(len(messages[1].text))
            if len(prompt_lengths) == 1:
                raise rejection

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = structured_note()

            return _Response()

    gen = make_generator(tmp_path, publish_retry=publish_retry)
    gen._client = _Client()  # type: ignore[assignment]
    await generate(
        gen,
        user_request="do X",
        previous_last_words="previous",
        dropped_messages=[Message("assistant", [f"work-{index} " * 1_000]) for index in range(20)],
    )
    return prompt_lengths, len(retry_events)


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (
            500,
            "This model's maximum context length is 128000 tokens. However, your messages resulted in 130000 tokens.",
        ),
        (503, "maximum context length is temporarily reduced"),
    ],
)
async def test_a_server_error_naming_the_context_window_shrinks_the_fallback(
    tmp_path, monkeypatch, status: int, message: str
) -> None:
    """A gateway may wrap an overflow in a 5xx: shrinking costs one smaller candidate, retrying the whole budget."""
    rejection = await openai_status(status, {"error": {"type": "server_error", "message": message}})

    prompt_lengths, retries = await _prompt_lengths_after(tmp_path, monkeypatch, rejection)

    assert len(prompt_lengths) == 2
    assert prompt_lengths[1] < prompt_lengths[0]
    assert retries == 0


async def test_a_candidate_that_overflows_as_its_read_timeout_runs_out_still_shrinks_the_fallback(
    tmp_path, monkeypatch
) -> None:
    """The chunk that ends a candidate's stream on a context overflow restarts
    the side call's read timeout and the stall watchdog waiting on it: reading
    on for its usage ends on the overflow, and a smaller candidate follows
    instead of the timeout cutting the stream off and sending it again."""
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    monkeypatch.setattr(chat_completions_client, "_ENDED_STREAM_WAIT_SECONDS", 1.0)
    read_timeouts: list[asyncio.Timeout] = []

    def timeout(delay: float | None) -> asyncio.Timeout:
        read_timeouts.append(asyncio.timeout(delay))
        return read_timeouts[-1]

    shadow = ModuleType("asyncio")
    shadow.__dict__.update(vars(asyncio), timeout=timeout)
    monkeypatch.setattr(one_shot, "asyncio", shadow)
    loop = asyncio.get_running_loop()
    events = 0

    async def pace() -> None:
        nonlocal events
        events += 1
        if events == 2:
            # The overflow comes as the read timeout of its pull runs out.
            read_timeouts[-1].reschedule(loop.time() + 0.5)
        elif events == 3:
            # Then the connection stays open, sending no usage.
            await asyncio.Event().wait()

    reported_at: list[int] = []
    retry_events, publish_retry = retry_collector()
    profile = ModelProfile(id="streamed", name="streamed", model_id="model", stream=True)
    gen = LastWordsGenerator(profile=profile, log_dir=tmp_path, publish_retry=publish_retry)
    overflow = [_note_chunk("## Task"), _note_chunk("", finish_reason="model_context_window_exceeded")]
    async with scripted_openai([overflow, [_note_chunk(structured_note(), finish_reason="stop")]], pace=pace) as wire:
        gen._client = ChatCompletionsClient(model="model", sdk_client=wire.client)  # type: ignore[assignment]
        note = await start_with_wire_progress(
            generate(
                gen,
                user_request="do X",
                previous_last_words="previous",
                dropped_messages=[Message("assistant", [f"work-{index} " * 1_000]) for index in range(20)],
            ),
            lambda: reported_at.append(events),
        )

    assert note == structured_note()
    first, second = (len(json.dumps(request["messages"])) for request in wire.requests)
    assert second < first
    assert retry_events == []
    # Before the first candidate, after its first chunk, at its overflow.
    assert reported_at[:3] == [0, 1, 2]


@pytest.mark.parametrize("partial", ["## Task\nDo", "## Task\n" + "Done so far. " * 60], ids=["short", "long"])
@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_a_note_that_filled_the_context_window_part_way_shrinks_the_fallback(
    tmp_path, monkeypatch, stream: bool, partial: str
) -> None:
    """The main turn keeps an answer the full window cut off; a note is asked for again, with a smaller prompt."""
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    reason = "model_context_window_exceeded"
    filled: ChatReply = (
        [_note_chunk(partial), _note_chunk("", finish_reason=reason)] if stream else _note_completion(partial, reason)
    )
    note: ChatReply = (
        [_note_chunk(structured_note(), finish_reason="stop")]
        if stream
        else _note_completion(structured_note(), "stop")
    )
    retry_events, publish_retry = retry_collector()
    profile = ModelProfile(id="model", name="model", model_id="model", stream=stream)
    gen = LastWordsGenerator(profile=profile, log_dir=tmp_path, publish_retry=publish_retry)
    async with scripted_openai([filled, note]) as wire:
        gen._client = ChatCompletionsClient(model="model", sdk_client=wire.client)  # type: ignore[assignment]
        written = await generate(
            gen,
            user_request="do X",
            previous_last_words="previous",
            dropped_messages=[Message("assistant", [f"work-{index} " * 1_000]) for index in range(20)],
        )

    assert written == structured_note()
    first, second = (len(json.dumps(request["messages"])) for request in wire.requests)
    assert second < first
    assert retry_events == []


@pytest.mark.parametrize("name", [None, "read_file"], ids=["nameless", "named"])
async def test_a_fallback_reply_that_asks_for_a_tool_is_asked_for_again_and_never_kept(
    tmp_path, monkeypatch, name: str | None
) -> None:
    # The fallback offers no tools: a reply with a call is no note, whatever text came with it.
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    retry_events, publish_retry = retry_collector()
    profile = ModelProfile(id="model", name="model", model_id="model", stream=True)
    gen = LastWordsGenerator(profile=profile, log_dir=tmp_path, publish_retry=publish_retry)
    asking = [_note_asking_for_a_tool(structured_note(task="Asked for a tool"), name)]
    note = [_note_chunk(structured_note(), finish_reason="stop")]
    async with scripted_openai([asking, note]) as wire:
        gen._client = ChatCompletionsClient(model="model", sdk_client=wire.client)  # type: ignore[assignment]
        written = await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert written == structured_note()
    assert len(wire.requests) == 2
    assert [(event.attempt, event.reason) for event in retry_events] == [
        (1, "last-words fallback call returned tool-call content")
    ]


@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_a_note_the_completer_wrote_until_the_context_window_filled_falls_back(
    tmp_path, monkeypatch, stream: bool
) -> None:
    """The completer sends the whole conversation, the call most likely to fill the window.

    A note it cut off is not asked for again with that prompt: LAST_WORDS
    falls back to the smaller one.
    """
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    reason = "model_context_window_exceeded"
    filled: ChatReply = (
        [_note_chunk("## Task\nDo"), _note_chunk("", finish_reason=reason)]
        if stream
        else _note_completion("## Task\nDo", reason)
    )
    note: ChatReply = (
        [_note_chunk(long_structured_note(), finish_reason="stop")]
        if stream
        else _note_completion(long_structured_note(), "stop")
    )
    retry_events, publish_retry = retry_collector()
    profile = ModelProfile(id="model", name="model", model_id="model", stream=stream)
    gen = LastWordsGenerator(profile=profile, log_dir=tmp_path, publish_retry=publish_retry)
    async with scripted_openai([filled, note]) as wire:
        client = ChatCompletionsClient(model="model", sdk_client=wire.client)
        gen._client = client  # type: ignore[assignment]
        written = await generate(
            gen,
            user_request="do X",
            previous_last_words="previous",
            dropped_messages=[Message("assistant", [f"work-{index} " * 1_000]) for index in range(20)],
            completer=_ClientLastWordsCompleter(client, stream=stream, options={}, client_kwargs={}),
        )

    assert written == long_structured_note()
    completer_request, fallback_request = (len(json.dumps(request["messages"])) for request in wire.requests)
    assert fallback_request < completer_request
    assert retry_events == []


async def test_a_rate_limit_naming_tokens_retries_the_same_fallback_candidate(tmp_path, monkeypatch) -> None:
    """A 429 that mentions tokens is throttling: a smaller candidate would not help, the backoff does."""
    rejection = await openai_status(
        429,
        {
            "error": {
                "type": "tokens",
                "code": "rate_limit_exceeded",
                "message": "Too many tokens, please wait before trying again.",
            }
        },
    )

    prompt_lengths, retries = await _prompt_lengths_after(tmp_path, monkeypatch, rejection)

    assert len(prompt_lengths) == 2
    assert prompt_lengths[1] == prompt_lengths[0]
    assert retries == 1


@pytest.mark.parametrize("use_completer", [False, True], ids=["fallback", "completer"])
async def test_mid_round_spend_exhaustion_aborts_before_retry(tmp_path, monkeypatch, use_completer: bool) -> None:
    """A spend gate that refuses the retry charge aborts the round before the second provider call."""
    charges: list[int] = []

    def spend(estimated_tokens: int) -> bool:
        charges.append(estimated_tokens)
        return len(charges) == 1

    completer = FakeCompleter([ConnectionError("retry me"), "must not run"]) if use_completer else None
    client = None
    if use_completer:
        monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
        gen = make_generator(tmp_path)
    else:
        monkeypatch.setattr(LastWordsGenerator, "_MAX_RETRIES", 2)
        monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0, 0))
        gen = make_generator(tmp_path)
        client = FailingFallbackClient(ConnectionError("retry me"))
        gen._client = client  # type: ignore[assignment]

    with pytest.raises(LastWordsSpendBudgetExceeded):
        await generate(
            gen,
            user_request="do X",
            previous_last_words=None,
            dropped_messages=[],
            completer=completer,
            spend_side_call_tokens=spend,
        )

    assert len(charges) == 2
    assert all(charge > 0 for charge in charges)
    if completer is not None:
        assert len(completer.calls) == 1
    else:
        assert client is not None
        assert client.calls == 1


async def test_sibling_call_run_admission_charges_one_merged_timeline(tmp_path):
    """Budget-boundary pin for the merged exchange unit: the annotation
    pipeline charges the fallback candidate as ONE group carrying both
    sibling calls — the same amount as a manually merged-annotation
    reference — and with the side-call budget set to exactly that merged
    estimate the strict ``<`` spend gate refuses the first attempt —
    Phase 4 raises and the strategy retains all current-turn groups
    instead of proceeding toward spill/exclusion."""
    from chrys.kernel import annotate_message_groups
    from chrys.kernel.compaction import (
        GROUP_ANNOTATION_KEY,
        GROUP_HAS_REASONING_KEY,
        GROUP_ID_KEY,
        GROUP_INDEX_KEY,
        GROUP_KIND_KEY,
    )
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.context.compaction.scoped import build_scoped_group_timeline
    from chrys.service.profiles.models.resolver import default_profile

    def _shape() -> list[Message]:
        return [
            user("do X"),
            Message(
                role="assistant",
                contents=[Content.from_function_call("call_a", "tool_a", arguments={"value": "a"})],
            ),
            Message(
                role="assistant",
                contents=[Content.from_function_call("call_b", "tool_b", arguments={"value": "b"})],
            ),
            Message(
                role="tool",
                contents=[
                    Content.from_function_result("call_a", result="alpha outcome"),
                    Content.from_function_result("call_b", result="beta outcome"),
                ],
            ),
        ]

    def _pipeline_groups():  # type: ignore[no-untyped-def]
        messages = _shape()
        annotate_message_groups(messages, force_reannotate=True)
        timeline = build_scoped_group_timeline(messages, span_start=0, span_end=len(messages), degraded=False)
        return timeline.groups

    def _merged_reference_groups():  # type: ignore[no-untyped-def]
        messages = _shape()
        annotate_message_groups(messages, force_reannotate=True)
        for message in messages[1:]:
            message.additional_properties[GROUP_ANNOTATION_KEY] = {
                GROUP_ID_KEY: "group_merged",
                GROUP_KIND_KEY: "tool_call",
                GROUP_INDEX_KEY: 1,
                GROUP_HAS_REASONING_KEY: False,
            }
        timeline = build_scoped_group_timeline(messages, span_start=0, span_end=len(messages), degraded=False)
        return timeline.groups

    class _NoteClient:
        async def get_response(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = structured_note()

            return _Response()

    async def _first_charge(groups) -> int:  # type: ignore[no-untyped-def]
        charges: list[int] = []

        def refuse(estimated_tokens: int) -> bool:
            charges.append(estimated_tokens)
            return False

        gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path)
        gen._client = _NoteClient()  # type: ignore[assignment]
        with pytest.raises(LastWordsSpendBudgetExceeded):
            await gen.generate(
                list(groups),
                None,
                degraded_opener=False,
                has_continuation_nudges=False,
                completer=None,
                spend_side_call_tokens=refuse,
            )
        return charges[0]

    charge_pipeline = await _first_charge(_pipeline_groups())
    charge_merged = await _first_charge(_merged_reference_groups())
    assert charge_pipeline == charge_merged

    spent = 0

    def strict_gate(estimated_tokens: int) -> bool:
        nonlocal spent
        spent += estimated_tokens
        return spent < charge_merged

    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path)
    gen._client = _NoteClient()  # type: ignore[assignment]
    with pytest.raises(LastWordsSpendBudgetExceeded):
        await gen.generate(
            list(_pipeline_groups()),
            None,
            degraded_opener=False,
            has_continuation_nudges=False,
            completer=None,
            spend_side_call_tokens=strict_gate,
        )
