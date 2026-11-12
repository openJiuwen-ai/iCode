# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The note is asked for once as an extension of the live request, and from the scoped slice when that fails."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import pytest

from chrys.foundation.errors import ProviderResponseError
from chrys.kernel import EXCLUDED_KEY, LastWordsToolCallError, Message, project_included_messages
from chrys.kernel.compaction import CompactionCallContext, estimate_message_tokens
from chrys.service.context.compaction.last_words import _SLICE_SAFETY_MARGIN_TOKENS, LastWordsGenerator
from chrys.service.context.compaction.scoped import ScopedGroup
from chrys.service.context.providers.history import CompressibleHistoryProvider
from chrys.service.profiles.models.schema import ModelProfile
from tests.service.context.compaction._compaction_helpers import (
    _assistant_text,
    _build_multi_turn,
    _build_single_turn,
    _build_tool_group,
    _estimate_tokens,
    _forced_phase4,
    _make_strategy,
    _user,
)
from tests.service.context.compaction._last_words_helpers import (
    CharacterTokenizer,
    FakeCompleter,
    FallbackClient,
    generate,
    make_generator,
    structured_note,
    user,
)
from tests.support.phase4_stubs import StubLastWordsGenerator

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("no_note_floor")

_OPENER = "Refactor the parser"


def _entry_request() -> list[Message]:
    """A live request: one finished turn, then the current one up to its newest tool result."""
    return [
        user("What does the lexer do?"),
        _assistant_text("It splits the source into tokens."),
        user(_OPENER),
        *_build_tool_group("call_0", "read_file", "old parser source"),
        *_build_tool_group("call_1", "read_file", "new parser source"),
    ]


def _current_task_work(entry: list[Message]) -> list[Message]:
    """The current turn's first tool group: the scoped harness takes one group of tool work."""
    return entry[3:5]


def _small_window_generator(tmp_path: Path) -> LastWordsGenerator:
    profile = ModelProfile(
        id="small", name="small", model_id="small", max_context_tokens=20_000, max_output_tokens=1_000
    )
    return LastWordsGenerator(profile=profile, template="TEMPLATE", max_output_tokens=300, log_dir=tmp_path)


async def test_the_note_is_asked_for_as_an_extension_of_the_live_request(tmp_path: Path) -> None:
    gen = make_generator(tmp_path, template="TEMPLATE TEXT", max_output_tokens=1234)
    note = structured_note()
    completer = FakeCompleter([note])
    entry = _entry_request()

    out = await generate(
        gen,
        user_request=_OPENER,
        previous_last_words=None,
        dropped_messages=_current_task_work(entry),
        completer=completer,
        entry_messages=entry,
    )

    assert out == note
    [call] = completer.calls
    # The provider has exactly these messages cached, under the live tool choice.
    assert len(call["base_messages"]) == len(entry)
    assert all(sent is live for sent, live in zip(call["base_messages"], entry, strict=True))
    assert call["keep_tool_choice"] is True
    assert call["max_output_tokens"] == 1234
    # The conversation holds an earlier turn too: the instruction says which task the note is for.
    assert f"<current_task_request>\n{_OPENER}\n</current_task_request>" in call["instruction"]
    assert "TEMPLATE TEXT" in call["instruction"]
    assert gen._client is None


async def test_without_the_live_request_the_note_comes_from_the_slice_alone(tmp_path: Path) -> None:
    """The control for the test above: the same call without ``entry_messages``."""
    gen = make_generator(tmp_path)
    completer = FakeCompleter([structured_note()])

    await generate(
        gen,
        user_request=_OPENER,
        previous_last_words=None,
        dropped_messages=_current_task_work(_entry_request()),
        completer=completer,
    )

    [call] = completer.calls
    assert call["keep_tool_choice"] is False
    assert [message.text for message in call["base_messages"] if message.role == "user"] == [_OPENER]
    assert "<current_task_request>" not in call["instruction"]


@pytest.mark.parametrize(
    "first_answer",
    [
        LastWordsToolCallError("the answer called a tool"),
        ProviderResponseError("server_error", "Upstream failed.", retryable=True),
        "",
        "Here is what happened so far, without the headings the note needs.",
    ],
    ids=["tool-call", "provider-error", "empty", "format-violation"],
)
async def test_a_failed_extension_is_not_repeated_and_the_slice_writes_the_note(
    tmp_path: Path, first_answer: str | Exception
) -> None:
    gen = make_generator(tmp_path)
    note = structured_note()
    completer = FakeCompleter([first_answer, note])
    entry = _entry_request()

    out = await generate(
        gen,
        user_request=_OPENER,
        previous_last_words=None,
        dropped_messages=_current_task_work(entry),
        completer=completer,
        entry_messages=entry,
    )

    assert out == note
    assert [call["keep_tool_choice"] for call in completer.calls] == [True, False]
    # The second request holds the current task only.
    assert [message.text for message in completer.calls[1]["base_messages"] if message.role == "user"] == [_OPENER]
    assert gen._client is None


async def test_an_extension_that_fits_exactly_is_sent_and_one_token_more_is_not(tmp_path: Path) -> None:
    tokenizer = CharacterTokenizer()
    groups = [ScopedGroup("opener", "user", (user(_OPENER),), True)]
    overhead = 456
    calibration = 1.5
    usable = math.floor((20_000 - 300 - _SLICE_SAFETY_MARGIN_TOKENS) / calibration)

    async def _extension_calls(entry: list[Message]) -> list[dict]:
        completer = FakeCompleter([structured_note()])
        await _small_window_generator(tmp_path).generate(
            groups,
            None,
            degraded_opener=False,
            has_continuation_nudges=False,
            completer=completer,
            tokenizer=tokenizer,
            request_overhead_tokens=overhead,
            calibration_ratio=calibration,
            entry_messages=entry,
        )
        return [call for call in completer.calls if call["keep_tool_choice"]]

    [probe] = await _extension_calls([user(_OPENER)])
    room = usable - overhead - tokenizer.count_tokens(probe["instruction"])
    fixed = estimate_message_tokens(user(""), tokenizer)
    fitting = [user("x" * (room - fixed))]
    assert estimate_message_tokens(fitting[0], tokenizer) == room

    assert len(await _extension_calls(fitting)) == 1
    assert await _extension_calls([user("x" * (room - fixed + 1))]) == []


async def test_an_extension_is_charged_its_whole_request_and_a_refused_one_is_never_sent(tmp_path: Path) -> None:
    gen = make_generator(tmp_path)
    completer = FakeCompleter([structured_note()])
    entry = _entry_request()
    asked: list[int] = []

    def _spend(tokens: int) -> bool:
        asked.append(tokens)
        return len(asked) > 1

    await generate(
        gen,
        user_request=_OPENER,
        previous_last_words=None,
        dropped_messages=_current_task_work(entry),
        completer=completer,
        spend_side_call_tokens=_spend,
        entry_messages=entry,
    )

    # The refused request was the whole conversation; the one sent is the current task's slice.
    assert [call["keep_tool_choice"] for call in completer.calls] == [False]
    assert len(asked) == 2
    assert asked[0] > asked[1]


async def test_a_task_resumed_without_its_request_is_never_asked_for_as_an_extension(tmp_path: Path) -> None:
    """The instruction names the task by its opening request; a resumed task has none to name."""
    gen = make_generator(tmp_path)
    completer = FakeCompleter([structured_note()])
    entry = _entry_request()

    await generate(
        gen,
        user_request=_OPENER,
        previous_last_words=None,
        dropped_messages=_current_task_work(entry),
        degraded_opener=True,
        completer=completer,
        entry_messages=entry,
    )

    assert [call["keep_tool_choice"] for call in completer.calls] == [False]


async def test_without_a_side_call_the_live_request_is_not_used(tmp_path: Path) -> None:
    gen = make_generator(tmp_path)
    fallback = FallbackClient()
    gen._client = fallback
    entry = _entry_request()

    out = await generate(
        gen,
        user_request=_OPENER,
        previous_last_words=None,
        dropped_messages=_current_task_work(entry),
        entry_messages=entry,
    )

    assert out == fallback.text
    assert fallback.calls == 1
    assert "What does the lexer do?" not in fallback.messages[0][0].text


# ---------------------------------------------------------------------------
# Which request a compaction pass hands the generator
# ---------------------------------------------------------------------------


class _UnusedCompleter:
    """Stands for the client's side call; the stub generator never invokes it."""

    async def complete_last_words(  # type: ignore[no-untyped-def]
        self, base_messages, instruction, *, max_output_tokens, on_usage=None, keep_tool_choice=False
    ):
        raise AssertionError("the stub generator must not invoke the completer")


def _extending_context() -> CompactionCallContext:
    return CompactionCallContext(last_words_completer=_UnusedCompleter(), last_words_extends_request=True)


def _shape(messages: Sequence[Message]) -> list[tuple[str, tuple[int, ...]]]:
    """Each message's role and the identity of the content objects it holds."""
    return [(message.role, tuple(id(content) for content in message.contents)) for message in messages]


async def test_a_pass_hands_over_the_request_as_it_found_it_before_any_phase_changed_it() -> None:
    generator = StubLastWordsGenerator(text="[note]")
    messages = _build_multi_turn(2, groups_per_turn=3, result_size=1000)
    messages.pop()  # the current turn is still at its newest tool result
    strategy = _forced_phase4(messages, last_words_generator=generator)
    found = list(messages)
    found_shape = _shape(found)

    assert await strategy(messages, _extending_context())

    entry = generator.calls[0]["entry_messages"]
    assert _shape(entry) == found_shape
    # Earlier phases rewrote the previous turn before the note was asked for.
    assert _shape(project_included_messages(messages)) != found_shape
    # The pass keeps marking the live messages; the copies it handed over stay as they were.
    assert all(copy is not live for copy, live in zip(entry, found, strict=True))
    assert not any(message.additional_properties.get(EXCLUDED_KEY, False) for message in entry)
    assert any(message.additional_properties.get(EXCLUDED_KEY, False) for message in found)


@pytest.mark.parametrize(
    "context",
    [
        None,
        CompactionCallContext(),
        CompactionCallContext(last_words_completer=_UnusedCompleter()),
        CompactionCallContext(last_words_extends_request=True),
    ],
    ids=["no-context", "no-side-call", "request-does-not-extend", "extends-without-side-call"],
)
async def test_a_request_that_may_not_be_extended_is_not_handed_over(context: CompactionCallContext | None) -> None:
    generator = StubLastWordsGenerator(text="[note]")
    messages = _build_single_turn(4, result_size=1000)
    strategy = _forced_phase4(messages, last_words_generator=generator)

    assert await strategy(messages, context)

    assert generator.calls[0]["entry_messages"] is None


async def test_an_overflow_pass_hands_over_no_request() -> None:
    """The window refused that request: one more message on top of it cannot fit."""
    generator = StubLastWordsGenerator(text="[note]")
    messages = _build_single_turn(6, result_size=8_000)
    messages.pop()
    lines = {"trigger_pct": 0.90, "target_pct": 0.20, "satisfied_pct": 0.60, "force_pct": 0.70}
    strategy = _make_strategy(
        max_context_tokens=4 * _estimate_tokens(messages), last_words_generator=generator, **lines
    )
    assert strategy.note_context_overflow()

    assert await strategy(messages, _extending_context())

    assert generator.calls[0]["entry_messages"] is None


@pytest.mark.parametrize("fold_queued", [False, True], ids=["no-fold", "fold-queued"])
async def test_a_pass_that_first_folded_a_queued_turn_hands_over_no_request(fold_queued: bool) -> None:
    """The fold rewrote the head of the request: the provider no longer has it cached."""
    generator = StubLastWordsGenerator(text="[note]")
    first_user, first_answer = _user("Turn one request"), _assistant_text("Turn one answer")
    state: dict = {"messages": [first_user, first_answer], "compressed_msgs": [], "turn_counter": 0}
    marker_id = CompressibleHistoryProvider.insert_marker(state, 1)
    messages = [first_user, first_answer, _user("Turn two request")]
    for index in range(4):
        messages.extend(_build_tool_group(f"call_{index}", "read_file", "x" * 1000))
    strategy = _forced_phase4(messages, last_words_generator=generator)
    strategy.bind_state(state)
    if fold_queued:
        strategy.queue_compression(marker_id, "Turn one summary")

    assert await strategy(messages, _extending_context())

    assert (generator.calls[0]["entry_messages"] is None) is fold_queued
