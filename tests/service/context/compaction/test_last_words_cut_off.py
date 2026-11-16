# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A note the provider cut off is asked for again, shorter, and is marked when nothing better can be had.

The answer's text alone cannot tell: a note that stopped inside its last
section has every heading the format asks for.  The side call says whether the
provider stopped it, and that is what the generator acts on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from chrys.foundation.errors import ProviderResponseError
from chrys.kernel import ContextWindowFilledError, LastWordsCompletion, Message
from chrys.service.context.compaction.last_words import (
    _CUT_OFF_CORRECTION,
    _CUT_OFF_NOTICE,
    _CUT_OFF_VIOLATION,
    _MIN_NOTE_TOKENS,
    LastWordsGenerator,
    LastWordsSpendBudgetExceeded,
    _shorter_note_budget,
)
from chrys.service.profiles.models.schema import ModelProfile
from tests.service.context.compaction._compaction_helpers import _assistant_text, _build_tool_group
from tests.service.context.compaction._last_words_helpers import (
    FailingFallbackClient,
    FakeCompleter,
    generate,
    long_structured_note,
    make_generator,
    status_collector,
    user,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

_BUDGET = 4000
_OPENER = "Refactor the parser"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))


def _cut_off(text: str) -> LastWordsCompletion:
    return LastWordsCompletion(text, cut_off=True)


def _states_cap(instruction: str, cap: int) -> bool:
    return f"under the {cap}-token output cap" in instruction


def _entry_request() -> list[Message]:
    return [
        user("What does the lexer do?"),
        _assistant_text("It splits the source into tokens."),
        user(_OPENER),
        *_build_tool_group("call_0", "read_file", "parser source"),
    ]


class _ScriptedClient:
    """Reconstruction-path client: each answer is ``(text, finish_reason)``; the last one repeats."""

    def __init__(self, answers: list[tuple[str, str | None]]) -> None:
        self._answers = list(answers)
        self.prompts: list[str] = []
        self.sent_max_tokens: list[int] = []

    async def get_response(self, messages: Sequence[Message], *, stream: bool, options: Mapping[str, Any]):  # type: ignore[no-untyped-def]
        self.prompts.append("\n".join(message.text for message in messages))
        self.sent_max_tokens.append(options["max_tokens"])
        text, reason = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]

        class _Response:
            usage_details = None
            additional_properties: ClassVar[dict[str, object]] = {}
            messages: ClassVar[tuple[Message, ...]] = ()
            finish_reason = reason
            raw_text = text

        return _Response()


def test_the_shorter_budget_is_half_and_never_under_the_minimum_the_prompt_asks_for() -> None:
    assert _shorter_note_budget(8192) == 4096
    assert _shorter_note_budget(800) == 500
    # A budget already under the minimum cannot be raised to it.
    assert _shorter_note_budget(300) == 300


async def test_a_cut_off_note_is_asked_for_again_shorter(tmp_path: Path) -> None:
    """Its headings are all there: only the side call knows the end is missing."""
    statuses, publish_status = status_collector()
    gen = make_generator(tmp_path, max_output_tokens=_BUDGET, publish_status=publish_status)
    complete = long_structured_note("second")
    completer = FakeCompleter([_cut_off(long_structured_note("first")), complete])

    out = await generate(gen, user_request=_OPENER, previous_last_words=None, dropped_messages=[], completer=completer)

    assert out == complete
    first, second = (call["instruction"] for call in completer.calls)
    assert _states_cap(first, _BUDGET)
    assert _CUT_OFF_CORRECTION not in first
    assert _states_cap(second, _BUDGET // 2)
    assert _CUT_OFF_CORRECTION in second
    # What the provider may send is unchanged: only the size asked for is lower.
    assert [call["max_output_tokens"] for call in completer.calls] == [_BUDGET, _BUDGET]
    assert statuses[-1].format_violation == ""


async def test_a_note_cut_off_twice_is_accepted_and_says_so(tmp_path: Path) -> None:
    """The same shorter request a third time would be cut off the same way."""
    statuses, publish_status = status_collector()
    gen = make_generator(tmp_path, max_output_tokens=_BUDGET, publish_status=publish_status)
    second = long_structured_note("second")
    completer = FakeCompleter([_cut_off(long_structured_note("first")), _cut_off(second)])

    out = await generate(gen, user_request=_OPENER, previous_last_words=None, dropped_messages=[], completer=completer)

    assert out == f"{second}\n\n{_CUT_OFF_NOTICE}"
    assert len(completer.calls) == 2
    assert statuses[-1].outcome == "ok"
    assert statuses[-1].format_violation == _CUT_OFF_VIOLATION
    assert statuses[-1].last_words == out
    assert gen._client is None


async def test_a_complete_note_is_not_marked(tmp_path: Path) -> None:
    """The control for the test above."""
    gen = make_generator(tmp_path, max_output_tokens=_BUDGET)
    note = long_structured_note()

    out = await generate(
        gen, user_request=_OPENER, previous_last_words=None, dropped_messages=[], completer=FakeCompleter([note])
    )

    assert out == note


async def test_a_cut_off_extension_makes_the_slice_ask_for_a_shorter_note_at_once(tmp_path: Path) -> None:
    """The slice has the same output budget: asking it for the same size would be cut off the same way."""
    gen = make_generator(tmp_path, max_output_tokens=_BUDGET)
    complete = long_structured_note("slice")
    completer = FakeCompleter([_cut_off(long_structured_note("extension")), complete])
    entry = _entry_request()

    out = await generate(
        gen,
        user_request=_OPENER,
        previous_last_words=None,
        dropped_messages=entry[3:],
        completer=completer,
        entry_messages=entry,
    )

    assert out == complete
    extension, scoped = completer.calls
    assert (extension["keep_tool_choice"], scoped["keep_tool_choice"]) == (True, False)
    assert _states_cap(extension["instruction"], _BUDGET)
    assert _states_cap(scoped["instruction"], _BUDGET // 2)
    assert _CUT_OFF_CORRECTION in scoped["instruction"]


async def test_a_slice_cut_off_after_a_cut_off_extension_is_accepted_without_a_third_request(tmp_path: Path) -> None:
    gen = make_generator(tmp_path, max_output_tokens=_BUDGET)
    scoped_note = long_structured_note("slice")
    completer = FakeCompleter([_cut_off(long_structured_note("extension")), _cut_off(scoped_note)])
    entry = _entry_request()

    out = await generate(
        gen,
        user_request=_OPENER,
        previous_last_words=None,
        dropped_messages=entry[3:],
        completer=completer,
        entry_messages=entry,
    )

    assert out == f"{scoped_note}\n\n{_CUT_OFF_NOTICE}"
    assert len(completer.calls) == 2
    assert gen._client is None


async def test_a_cut_off_extension_is_used_when_the_slice_cannot_be_paid_for(tmp_path: Path) -> None:
    """The note is the same one a slice would have been accepted with: which path wrote it changes nothing."""
    statuses, publish_status = status_collector()
    gen = make_generator(tmp_path, max_output_tokens=_BUDGET, publish_status=publish_status)
    extension_note = long_structured_note("extension")
    completer = FakeCompleter([_cut_off(extension_note)])
    entry = _entry_request()
    charges: list[int] = []

    def spend(tokens: int) -> bool:
        charges.append(tokens)
        return len(charges) == 1

    out = await generate(
        gen,
        user_request=_OPENER,
        previous_last_words=None,
        dropped_messages=entry[3:],
        completer=completer,
        spend_side_call_tokens=spend,
        entry_messages=entry,
    )

    assert out == f"{extension_note}\n\n{_CUT_OFF_NOTICE}"
    assert len(completer.calls) == 1
    assert len(charges) == 2
    assert statuses[-1].format_violation == _CUT_OFF_VIOLATION


async def test_a_cut_off_extension_too_short_to_use_is_not_kept(tmp_path: Path) -> None:
    """The control for the test above: with nothing usable in hand, a refused slice still fails the round."""
    gen = make_generator(tmp_path, max_output_tokens=_BUDGET)
    completer = FakeCompleter([_cut_off("## Goal\nx")])
    entry = _entry_request()
    charges: list[int] = []

    def spend(tokens: int) -> bool:
        charges.append(tokens)
        return len(charges) == 1

    with pytest.raises(LastWordsSpendBudgetExceeded):
        await generate(
            gen,
            user_request=_OPENER,
            previous_last_words=None,
            dropped_messages=entry[3:],
            completer=completer,
            spend_side_call_tokens=spend,
            entry_messages=entry,
        )

    assert len(completer.calls) == 1


async def test_a_cut_off_reconstruction_is_asked_for_again_shorter(tmp_path: Path) -> None:
    gen = make_generator(tmp_path, max_output_tokens=_BUDGET)
    complete = long_structured_note("second")
    client = _ScriptedClient([(long_structured_note("first"), "length"), (complete, "stop")])
    gen._client = client  # type: ignore[assignment]

    out = await generate(gen, user_request=_OPENER, previous_last_words=None, dropped_messages=[])

    assert out == complete
    first, second = client.prompts
    assert _states_cap(first, _BUDGET)
    assert _CUT_OFF_CORRECTION not in first
    assert _states_cap(second, _BUDGET // 2)
    assert _CUT_OFF_CORRECTION in second


async def test_a_reconstruction_cut_off_twice_is_accepted_and_says_so(tmp_path: Path) -> None:
    statuses, publish_status = status_collector()
    gen = make_generator(tmp_path, max_output_tokens=_BUDGET, publish_status=publish_status)
    note = long_structured_note()
    client = _ScriptedClient([(note, "length")])
    gen._client = client  # type: ignore[assignment]

    out = await generate(gen, user_request=_OPENER, previous_last_words=None, dropped_messages=[])

    assert out == f"{note}\n\n{_CUT_OFF_NOTICE}"
    assert len(client.prompts) == 2
    assert statuses[-1].format_violation == _CUT_OFF_VIOLATION


async def test_a_note_that_filled_the_window_in_the_extension_is_kept_for_the_slice(tmp_path: Path) -> None:
    """The window was full, so the prompt shrinks — but the note it did write is a cut-off note, not nothing."""
    statuses, publish_status = status_collector()
    gen = make_generator(tmp_path, max_output_tokens=_BUDGET, publish_status=publish_status)
    extension_note = long_structured_note("extension")
    completer = FakeCompleter([ContextWindowFilledError(extension_note)])
    entry = _entry_request()
    charges: list[int] = []

    def spend(tokens: int) -> bool:
        charges.append(tokens)
        return len(charges) == 1

    out = await generate(
        gen,
        user_request=_OPENER,
        previous_last_words=None,
        dropped_messages=entry[3:],
        completer=completer,
        spend_side_call_tokens=spend,
        entry_messages=entry,
    )

    assert out == f"{extension_note}\n\n{_CUT_OFF_NOTICE}"
    assert len(completer.calls) == 1
    assert statuses[-1].format_violation == _CUT_OFF_VIOLATION


async def test_a_filled_window_in_the_slice_is_kept_when_the_reconstruction_cannot_fit(
    tmp_path: Path,
) -> None:
    """The fallback shrinks to nothing: the note the slice wrote before the window filled is better than none."""
    scoped_note = long_structured_note("slice")
    gen = make_generator(tmp_path, max_output_tokens=_BUDGET)
    client = FailingFallbackClient(ProviderResponseError("context_length_exceeded", "too long", retryable=False))
    gen._client = client  # type: ignore[assignment]
    completer = FakeCompleter([ContextWindowFilledError(scoped_note)])

    out = await generate(gen, user_request=_OPENER, previous_last_words=None, dropped_messages=[], completer=completer)

    assert out == f"{scoped_note}\n\n{_CUT_OFF_NOTICE}"
    assert client.calls >= 1


async def test_a_filled_window_too_short_to_use_is_not_kept(tmp_path: Path) -> None:
    """The control for the tests above: a fragment would replace the whole dropped turn."""
    gen = make_generator(tmp_path, max_output_tokens=_BUDGET)
    completer = FakeCompleter([ContextWindowFilledError("## Task\nDo")])
    entry = _entry_request()
    charges: list[int] = []

    def spend(tokens: int) -> bool:
        charges.append(tokens)
        return len(charges) == 1

    with pytest.raises(LastWordsSpendBudgetExceeded):
        await generate(
            gen,
            user_request=_OPENER,
            previous_last_words=None,
            dropped_messages=entry[3:],
            completer=completer,
            spend_side_call_tokens=spend,
            entry_messages=entry,
        )

    assert len(completer.calls) == 1


async def test_a_cut_off_note_handed_to_the_reconstruction_is_corrected_there(tmp_path: Path) -> None:
    """The slice gave up on a provider error after a cut-off note: the diagnosis crosses over with the pending note."""
    gen = make_generator(tmp_path, max_output_tokens=_BUDGET)
    complete = long_structured_note("reconstruction")
    client = _ScriptedClient([(complete, "stop")])
    gen._client = client  # type: ignore[assignment]
    completer = FakeCompleter([_cut_off(long_structured_note("slice")), ValueError("not retryable")])

    out = await generate(gen, user_request=_OPENER, previous_last_words=None, dropped_messages=[], completer=completer)

    assert out == complete
    [prompt] = client.prompts
    assert _states_cap(prompt, _BUDGET // 2)
    assert _CUT_OFF_CORRECTION in prompt


async def test_the_shorter_size_is_taken_from_what_the_request_can_really_send(tmp_path: Path) -> None:
    """A window too small for the usual output: the retry halves the lowered cap, not the usual one.

    The first answer is too short to keep, so the request is made again; with
    a usable note in hand a request that does not fit is not repeated.
    """
    profile = ModelProfile(
        id="small-window",
        name="small-window",
        model_id="small-window",
        max_context_tokens=9_000,
        max_output_tokens=8_192,
        stream=False,
    )
    gen = LastWordsGenerator(profile=profile, log_dir=tmp_path)
    complete = long_structured_note("second")
    client = _ScriptedClient([("## Task\nRefactor the", "length"), (complete, "stop")])
    gen._client = client  # type: ignore[assignment]

    out = await generate(
        gen,
        user_request=_OPENER,
        previous_last_words=None,
        dropped_messages=[Message("assistant", ["work " * 5_000])],
    )

    assert out == complete
    first, second = client.prompts
    first_sent, second_sent = client.sent_max_tokens
    assert _MIN_NOTE_TOKENS <= first_sent < 8_192
    assert _states_cap(first, first_sent)
    # The correction is a little longer than no correction, so the second
    # request has a little less room to send.
    assert _MIN_NOTE_TOKENS <= second_sent <= first_sent
    assert _states_cap(second, _shorter_note_budget(second_sent))
    assert _shorter_note_budget(second_sent) < second_sent
    assert _CUT_OFF_CORRECTION in second
