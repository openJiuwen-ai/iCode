# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The LAST_WORDS note format contract: validation, canonicalization, corrective retries and the length floor."""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

import pytest

from chrys.foundation.retry import RetryAttemptInfo
from chrys.kernel import Content, Message
from chrys.service.context.compaction.last_words import (
    _BASE_GUIDANCE,
    _FORMAT_CONTRACT,
    _FORMAT_CORRECTION_MAX_CHARS,
    _SUPPLEMENT_LABEL,
    LastWordsGenerator,
    _format_correction,
    _note_format_violation,
    _validate_note_format,
)
from tests.service.context.compaction._last_words_helpers import (
    FakeCompleter,
    FallbackClient,
    SequenceFallbackClient,
    generate,
    long_structured_note,
    make_generator,
    retry_collector,
    status_collector,
    structured_note,
)

pytestmark = pytest.mark.usefixtures("no_note_floor")


@pytest.mark.parametrize(
    ("text", "violation"),
    [
        ("## Progress\nDone\n\n## Next\nContinue", 'missing required heading "## Task"'),
        (
            "## Task\nOne\n\n## Progress\nDone\n\n## Task\nTwo\n\n## Next\nContinue",
            'required heading "## Task" appears more than once',
        ),
        (
            "## Task\n\n## Progress\nDone\n\n## Next\nContinue",
            'section "## Task" has no non-blank body content',
        ),
        (
            "## Task\nOne\n\n## Progress\nDone\n\n## Next",
            'section "## Next" has no non-blank body content',
        ),
        (
            "## Task extra\nOne\n\n## Progress\nDone\n\n## Next\nContinue",
            'missing required heading "## Task"',
        ),
        (
            "## Task\nOne\n\n## Progress\nDone\n\n## Next\nContinue\n\n## Key facts\nFact\n\n## Task\nDuplicate",
            'required heading "## Task" appears more than once',
        ),
    ],
)
def test_note_format_violation_reports_structured_grammar_errors(text: str, violation: str) -> None:
    assert _note_format_violation(text) == violation


def test_note_format_violation_accepts_required_prefix_and_optional_tail() -> None:
    text = structured_note() + "\n\n## Key facts\nA fact\n\n## Constraints\nA constraint"

    assert _note_format_violation(text) is None


@pytest.mark.parametrize("fence", ["```", "~~~"])
def test_note_format_violation_ignores_headings_inside_fenced_code_blocks(fence: str) -> None:
    text = (
        "## Task\nDo it\n\n"
        f"{fence}markdown\n## Task\nnot a section\n## Next\nnot a section\n{fence}\n\n"
        "## Progress\nDone\n\n## Next\nContinue"
    )

    assert _note_format_violation(text) is None


@pytest.mark.parametrize(
    ("opening", "closing"),
    [("   ```python", "  ````"), ("  ~~~~ markdown", "   ~~~~~")],
)
def test_note_format_violation_honors_indented_fences_with_info_strings(opening: str, closing: str) -> None:
    text = (
        f"## Task\nDo it\n\n{opening}\n## Task\nignored duplicate\n{closing}\n\n## Progress\nDone\n\n## Next\nContinue"
    )

    assert _note_format_violation(text) is None


def test_note_format_violation_accepts_crlf_input() -> None:
    assert _note_format_violation(structured_note().replace("\n", "\r\n")) is None


@pytest.mark.parametrize("indent", ["", " ", "  ", "   "])
def test_note_format_violation_accepts_commonmark_heading_indent_and_trailing_whitespace(indent: str) -> None:
    text = f"{indent}## Task  \t\nDo it\n\n{indent}## Progress\t\nDone\n\n{indent}## Next   \nContinue"

    assert _note_format_violation(text) is None


@pytest.mark.parametrize("heading", ["##Task", "    ## Task", "## Task extra"])
def test_note_format_violation_rejects_non_heading_or_wrong_title_task_line(heading: str) -> None:
    """No space after the hashes (not a heading), 4-space indent (code block),
    and a different title all still fail — only level and order are relaxed."""
    text = f"{heading}\nDo it\n\n## Progress\nDone\n\n## Next\nContinue"

    assert _note_format_violation(text) == 'missing required heading "## Task"'


@pytest.mark.parametrize(
    "heading",
    [
        "# Task",
        "### Task",
        "#### Task",
        "## Task #",
        "## Task ##",
        # CommonMark permits spaces/tabs AFTER the closing hash run too.
        "## Task ##   ",
        "## Task ##\t",
        "### Task ### \t ",
    ],
)
def test_required_heading_level_and_closing_sequence_are_relaxed_and_normalized(heading: str) -> None:
    """Required titles are recognized at any ATX level (and with CommonMark
    closing sequences, including trailing whitespace after the hash run)
    and normalized back to the canonical ``##`` form."""
    from chrys.service.context.compaction.last_words import _validate_note_format

    text = f"{heading}\nDo it\n\n## Progress\nDone\n\n## Next\nContinue"

    violation, canonical = _validate_note_format(text)
    assert violation is None
    assert canonical == "## Task\nDo it\n\n## Progress\nDone\n\n## Next\nContinue"


def test_note_format_violation_allows_outer_whitespace_but_rejects_provider_wrapper() -> None:
    assert _note_format_violation(f" \t\n\n{structured_note()}\n\t") is None

    wrapped = f"<summary>\n{structured_note()}\n</summary>"
    assert _note_format_violation(wrapped) == 'note must begin with required heading "## Task"'


def test_note_format_violation_ignores_nested_markers_and_unterminated_fence_tail() -> None:
    nested = "## Task\nDo it\n\n````markdown\n~~~\n## Task\n~~~\n```\n````\n\n## Progress\nDone\n\n## Next\nContinue"
    unterminated = structured_note() + "\n\n~~~markdown\n## Task\nignored duplicate"

    assert _note_format_violation(nested) is None
    assert _note_format_violation(unterminated) is None


def test_note_format_violation_does_not_treat_backtick_info_containing_backtick_as_fence() -> None:
    text = "## Task\nDo it\n\n```bad`info\n## Task\nduplicate\n```\n\n## Progress\nDone\n\n## Next\nContinue"

    assert _note_format_violation(text) == 'required heading "## Task" appears more than once'


@pytest.mark.parametrize("next_heading", ["# Detail", "## Detail"])
def test_note_format_violation_requires_body_content_before_next_section_heading(next_heading: str) -> None:
    """A level-1/2 heading starts a new section, so required content cannot
    be satisfied by the following section's body."""
    text = f"## Task\n{next_heading}\nNested text\n\n## Progress\nDone\n\n## Next\nContinue"

    assert _note_format_violation(text) == 'section "## Task" has no non-blank body content'


@pytest.mark.parametrize("sub_heading", ["### Detail", "###### Detail"])
def test_subsection_headings_count_as_required_section_body(sub_heading: str) -> None:
    """Level-3+ non-required headings are subsections of the enclosing
    required section — they and their text are body content, and they ride
    along when sections are reordered."""
    from chrys.service.context.compaction.last_words import _validate_note_format

    text = f"## Task\n{sub_heading}\nNested text\n\n## Progress\nDone\n\n## Next\nContinue"

    violation, canonical = _validate_note_format(text)
    assert violation is None
    assert canonical == text


def test_note_format_violation_rejects_whitespace_only_required_body() -> None:
    text = "## Task\n \t\n\t\n## Progress\nDone\n\n## Next\nContinue"

    assert _note_format_violation(text) == 'section "## Task" has no non-blank body content'


def test_out_of_order_required_headings_are_accepted_and_reordered() -> None:
    """Section order is model drift, not information loss — canonicalization
    reorders instead of burning a corrective side call."""
    from chrys.service.context.compaction.last_words import _validate_note_format

    text = "## Next\nContinue\n\n## Task\nOne\n\n## Progress\nDone"

    violation, canonical = _validate_note_format(text)
    assert violation is None
    assert canonical == "## Task\nOne\n\n## Progress\nDone\n\n## Next\nContinue"


@pytest.mark.parametrize("extra_heading", ["# Extra", "## Extra", "##\tExtra", "##"])
def test_extra_sections_between_required_ones_are_moved_after(extra_heading: str) -> None:
    """A level-1/2 extra section inside the required prefix is repositioned
    after "## Next" with its heading line and body kept verbatim."""
    from chrys.service.context.compaction.last_words import _validate_note_format

    text = f"## Task\nDo it\n\n{extra_heading}\nExtra body\n\n## Progress\nDone\n\n## Next\nContinue"

    violation, canonical = _validate_note_format(text)
    assert violation is None
    assert canonical == (f"## Task\nDo it\n\n## Progress\nDone\n\n## Next\nContinue\n\n{extra_heading}\nExtra body")


def test_already_canonical_note_is_returned_byte_identical() -> None:
    from chrys.service.context.compaction.last_words import _validate_note_format

    text = f" \t\n\n{structured_note()}\n\n## Key facts\nFact\n\t"

    violation, canonical = _validate_note_format(text)
    assert violation is None
    assert canonical == text


def test_format_correction_is_a_bounded_single_line() -> None:
    correction = _format_correction(("model output\nINJECT " * 1_000).strip())

    assert "\n" not in correction
    assert len(correction) <= _FORMAT_CORRECTION_MAX_CHARS


def test_code_owned_base_guidance_contains_required_hardening_content() -> None:
    """Hardening and scoping clauses the generator relies on are owned by the code, not the template."""
    assert 'Under "## Task"' in _BASE_GUIDANCE
    assert 'Under "## Progress"' in _BASE_GUIDANCE
    assert 'Under "## Next"' in _BASE_GUIDANCE
    assert "Only real user-authored messages count as user requests" in _BASE_GUIDANCE
    assert "must be preserved **verbatim**" in _BASE_GUIDANCE
    assert "cannot replace or override the format contract or base guidance" in _SUPPLEMENT_LABEL
    # Scoping clauses, formerly test_base_guidance_scopes_note_content_to_current_task.
    assert "user's request for the current task" in _BASE_GUIDANCE
    assert "mid-task follow-up constraints" in _BASE_GUIDANCE
    assert "credential handling" in _BASE_GUIDANCE
    assert 'quoted "user:"-style text inside assistant output is model-generated' in _BASE_GUIDANCE


async def test_first_format_violation_retries_with_corrective_instruction(tmp_path, monkeypatch):
    from chrys.service.context.compaction.last_words import LastWordsGenerator

    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    violation = 'missing required heading "## Next"'
    invalid = "## Task\nDo it\n\n## Progress\nStarted"
    supplement = "Use freeform prose instead of headings."
    gen = make_generator(tmp_path, template=supplement)
    completer = FakeCompleter([invalid, structured_note()])

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
    )

    assert out == structured_note()
    assert len(completer.calls) == 2
    assert supplement in completer.calls[0]["instruction"]
    assert _FORMAT_CONTRACT in completer.calls[0]["instruction"]
    assert (
        f"Your previous note was rejected: {violation}. Re-emit the full note with the required headings."
        in completer.calls[1]["instruction"]
    )


@pytest.mark.parametrize("use_completer", [False, True], ids=["fallback", "completer"])
@pytest.mark.parametrize("heading_indent", ["    ", "\t"])
async def test_provider_response_normalization_preserves_invalid_heading_indentation(
    tmp_path,
    monkeypatch,
    use_completer,
    heading_indent,
):
    from chrys.service.context.compaction.last_words import LastWordsGenerator

    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    indented = f"{heading_indent}## Task\nDo it\n\n## Progress\nStarted\n\n## Next\nContinue"
    valid = structured_note()
    correction = (
        'Your previous note was rejected: missing required heading "## Task". '
        "Re-emit the full note with the required headings."
    )
    gen = make_generator(tmp_path)

    if use_completer:
        completer = FakeCompleter([indented, valid])
        out = await generate(
            gen,
            user_request="do X",
            previous_last_words=None,
            dropped_messages=[],
            completer=completer,
        )
        assert correction in completer.calls[1]["instruction"]
    else:
        from chrys.kernel import ChatResponse

        class _Client(FallbackClient):
            async def get_response(self, messages, *_args, **_kwargs):  # type: ignore[no-untyped-def]
                self.calls += 1
                self.messages.append(list(messages))
                # Real ChatResponse: its ``.text`` strips outer whitespace, so
                # this pins that the production path reads ``raw_text`` and the
                # indented heading actually reaches the validator.
                text = indented if self.calls == 1 else valid
                return ChatResponse(messages=[Message("assistant", [Content.from_text(text)])])

        client = _Client()
        gen._client = client  # type: ignore[assignment]
        out = await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])
        assert correction in client.messages[1][0].text

    assert out == valid


async def test_short_malformed_completer_response_gets_correction_without_early_acceptance(tmp_path, monkeypatch):
    from chrys.service.context.compaction.last_words import LastWordsGenerator

    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    monkeypatch.setattr(LastWordsGenerator, "_MIN_NOTE_CHARS", 300)
    invalid_short = "missing every required heading"
    valid = long_structured_note()
    correction = (
        'Your previous note was rejected: missing required heading "## Task". '
        "Re-emit the full note with the required headings."
    )
    gen = make_generator(tmp_path)
    fallback = FallbackClient(valid)
    gen._client = fallback  # type: ignore[assignment]
    completer = FakeCompleter([invalid_short, invalid_short])

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
    )

    assert out == valid
    assert len(completer.calls) == 3
    assert fallback.calls == 1
    assert correction in completer.calls[1]["instruction"]
    assert correction in fallback.messages[0][0].text


async def test_short_malformed_fallback_response_gets_format_correction(tmp_path, monkeypatch):
    from chrys.service.context.compaction.last_words import LastWordsGenerator

    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    monkeypatch.setattr(LastWordsGenerator, "_MIN_NOTE_CHARS", 300)
    invalid_short = "## Task\nDo it"
    valid = long_structured_note()

    class _Client(FallbackClient):
        async def get_response(self, messages, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            self.messages.append(list(messages))

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = invalid_short if self.calls == 1 else valid

            return _Response()

    client = _Client()
    gen = make_generator(tmp_path)
    gen._client = client  # type: ignore[assignment]

    out = await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert out == valid
    assert client.calls == 2
    assert (
        'Your previous note was rejected: missing required heading "## Progress". '
        "Re-emit the full note with the required headings." in client.messages[1][0].text
    )


_PADDED_OUTER_WHITESPACE = f"{structured_note()}\n" + " " * 500
# Mis-levelled headings force the canonical rebuild, which pops the padded
# blank lines; raw length still exceeds the 300-char floor.
_PADDED_INTERNAL_BLANK_LINES = "### Task\nx\n" + "\n" * 400 + "### Progress\ny\n\n### Next\nz"
_PADDED_HEADING_CLOSING_SEQUENCE = "## Task " + "#" * 300 + "\nx\n\n## Progress\ny\n\n## Next\nz"


def _no_precondition() -> None:
    """The outer-whitespace case needs no pre-assertion about the raw note."""


def _internal_blank_lines_clear_the_raw_floor() -> None:
    assert len(_PADDED_INTERNAL_BLANK_LINES) > 300


def _heading_closing_sequence_canonicalizes_below_the_floor() -> None:
    violation, canonical = _validate_note_format(_PADDED_HEADING_CLOSING_SEQUENCE)
    assert violation is None
    assert len("".join(_PADDED_HEADING_CLOSING_SEQUENCE.split())) > 300
    assert len("".join(canonical.split())) < 300


@pytest.mark.parametrize(
    ("padded", "precondition"),
    [
        pytest.param(_PADDED_OUTER_WHITESPACE, _no_precondition, id="outer_whitespace"),
        pytest.param(
            _PADDED_INTERNAL_BLANK_LINES,
            _internal_blank_lines_clear_the_raw_floor,
            id="internal_blank_lines",
        ),
        pytest.param(
            _PADDED_HEADING_CLOSING_SEQUENCE,
            _heading_closing_sequence_canonicalizes_below_the_floor,
            id="heading_closing_sequence",
        ),
    ],
)
async def test_padding_does_not_satisfy_note_length_floor(
    tmp_path,
    monkeypatch,
    padded: str,
    precondition: Callable[[], None],
) -> None:
    """The floor applies to the canonical note, not the raw response.

    Whitespace, blank-line and closing-sequence padding all clear the raw
    300-char floor yet canonicalize away, so accepting them would authorize the
    drop with a hollow note.
    """
    precondition()
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    monkeypatch.setattr(LastWordsGenerator, "_MIN_NOTE_CHARS", 300)
    valid = long_structured_note()
    client = SequenceFallbackClient([padded, valid])
    gen = make_generator(tmp_path)
    gen._client = client  # type: ignore[assignment]

    out = await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert out == valid
    assert client.calls == 2


async def test_repeated_format_violation_is_accepted_and_surfaced(tmp_path, monkeypatch):
    from chrys.service.context.compaction.last_words import LastWordsGenerator

    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    invalid = "## Task\nDo it\n\n## Progress\nStarted"
    violation = 'missing required heading "## Next"'
    statuses, publish_status = status_collector()

    gen = make_generator(
        tmp_path,
        publish_status=publish_status,
    )
    completer = FakeCompleter([invalid])

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
    )

    assert out == invalid
    assert len(completer.calls) == 2
    assert [status.stage for status in statuses] == ["started", "finished"]
    assert statuses[-1].outcome == "ok"
    assert statuses[-1].format_violation == violation
    assert (
        sum(
            f"format violation accepted: {violation}" in path.read_text(encoding="utf-8")
            for path in tmp_path.glob("last_words_*.log")
        )
        == 1
    )


async def test_different_fresh_format_violations_continue_into_fallback(tmp_path, monkeypatch):
    from chrys.service.context.compaction.last_words import LastWordsGenerator

    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    first_invalid = "## Task\nDo it\n\n## Progress\nStarted"
    second_invalid = "## Task\nDo it\n\n## Progress\nStarted\n\n## Next"
    third_invalid = "## Task\nDo it\n\n## Progress\n\n## Next\nContinue"
    third_violation = 'section "## Progress" has no non-blank body content'
    valid = structured_note()
    retry_events, publish_retry = retry_collector()

    gen = make_generator(tmp_path, publish_retry=publish_retry)
    fallback = FallbackClient(valid)
    gen._client = fallback  # type: ignore[assignment]
    completer = FakeCompleter([first_invalid, second_invalid, third_invalid])

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
    )

    assert out == valid
    assert len(completer.calls) == 3
    assert fallback.calls == 1
    assert [event.reason for event in retry_events] == [
        'missing required heading "## Next"',
        'section "## Next" has no non-blank body content',
    ]
    assert (
        f"Your previous note was rejected: {third_violation}. "
        "Re-emit the full note with the required headings." in fallback.messages[0][0].text
    )


async def test_second_identical_violation_is_accepted_after_different_violation(tmp_path, monkeypatch):
    from chrys.service.context.compaction.last_words import LastWordsGenerator

    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    first_invalid = "## Task\nDo it\n\n## Progress\nStarted"
    second_invalid = "## Task\nDo it\n\n## Progress\nStarted\n\n## Next"
    third_invalid = "## Task\nDo it\n\n## Progress\n\n## Next\nContinue"
    first_violation = 'missing required heading "## Next"'
    statuses, publish_status = status_collector()

    gen = make_generator(tmp_path, publish_status=publish_status)
    fallback = FallbackClient(first_invalid)
    gen._client = fallback  # type: ignore[assignment]
    completer = FakeCompleter([first_invalid, second_invalid, third_invalid])

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
    )

    assert out == first_invalid
    assert len(completer.calls) == 3
    assert fallback.calls == 1
    assert statuses[-1].format_violation == first_violation


_RETRY_LIMIT_VIOLATION = 'missing required heading "## Task"'


@pytest.mark.parametrize(
    ("note", "under_floor"),
    [
        pytest.param("short malformed note", True, id="short_note_under_floor"),
        pytest.param(("Malformed but long enough. " * 30).rstrip(), False, id="fresh_violation_over_floor"),
    ],
)
async def test_format_violation_is_accepted_when_retry_budget_is_exhausted(
    tmp_path,
    monkeypatch,
    note: str,
    under_floor: bool,
) -> None:
    """With no corrective retries left the invalid note is accepted and the violation surfaced."""
    monkeypatch.setattr(LastWordsGenerator, "_MAX_CORRECTIVE_RETRIES", 0)
    if under_floor:
        monkeypatch.setattr(LastWordsGenerator, "_MIN_NOTE_CHARS", 300)
    statuses, publish_status = status_collector()
    gen = make_generator(tmp_path, publish_status=publish_status)
    fallback = FallbackClient(note)
    gen._client = fallback  # type: ignore[assignment]

    out = await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert out == note
    assert statuses[-1].format_violation == _RETRY_LIMIT_VIOLATION
    if under_floor:
        assert (
            sum(
                f"format violation accepted: {_RETRY_LIMIT_VIOLATION}" in path.read_text(encoding="utf-8")
                for path in tmp_path.glob("last_words_*.log")
            )
            == 1
        )
    else:
        assert fallback.calls == 1
        assert statuses[-1].outcome == "ok"


async def test_fallback_accepts_fresh_invalid_note_when_correction_cannot_fit(tmp_path, monkeypatch):
    import chrys.service.context.compaction.last_words as last_words_module
    from chrys.service.context.compaction.last_words import LastWordsGenerator

    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    invalid = ("## Task\nDo it\n\n## Progress\n" + "work " * 100).rstrip()
    statuses, publish_status = status_collector()

    def _admission_tokens(messages, *, output_reserve):  # type: ignore[no-untyped-def]
        del output_reserve
        if "Your previous note was rejected:" in messages[0].text:
            return 10**9
        return 0

    monkeypatch.setattr(last_words_module, "_fallback_admission_tokens", _admission_tokens)
    gen = make_generator(tmp_path, publish_status=publish_status)
    fallback = FallbackClient(invalid)
    gen._client = fallback  # type: ignore[assignment]

    out = await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert out == invalid
    assert fallback.calls == 1
    assert statuses[-1].format_violation == 'missing required heading "## Next"'


@pytest.mark.parametrize("use_completer", [False, True], ids=["fallback", "completer"])
async def test_pending_invalid_note_survives_correction_spend_exhaustion(tmp_path, monkeypatch, use_completer):
    from chrys.service.context.compaction.last_words import CompactionStatus, LastWordsGenerator

    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    invalid = ("## Task\nDo it\n\n## Progress\n" + "work " * 100).rstrip()
    statuses: list[CompactionStatus] = []
    spend_calls = 0

    async def _publish_status(status: CompactionStatus) -> None:
        statuses.append(status)

    def _spend(_estimated_tokens: int) -> bool:
        nonlocal spend_calls
        spend_calls += 1
        return spend_calls == 1

    gen = make_generator(tmp_path, publish_status=_publish_status)
    fallback = FallbackClient(invalid)
    gen._client = fallback  # type: ignore[assignment]
    completer = FakeCompleter([invalid]) if use_completer else None

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
        spend_side_call_tokens=_spend,
    )

    assert out == invalid
    assert spend_calls == 2
    assert fallback.calls == (0 if use_completer else 1)
    if completer is not None:
        assert len(completer.calls) == 1
    assert statuses[-1].format_violation == 'missing required heading "## Next"'


async def test_transport_and_format_failures_use_independent_fallback_retry_budgets(tmp_path, monkeypatch):
    from chrys.service.context.compaction.last_words import LastWordsGenerator

    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (3, 7))
    monkeypatch.setattr(LastWordsGenerator, "_MAX_CORRECTIVE_RETRIES", 1)
    invalid = ("## Task\nDo it\n\n## Progress\n" + "work " * 100).rstrip()
    retry_events: list[RetryAttemptInfo] = []
    sleep_delays: list[float] = []

    async def _publish_retry(info: RetryAttemptInfo) -> None:
        retry_events.append(info)

    async def _sleep(delay: float) -> None:
        sleep_delays.append(delay)

    monkeypatch.setattr("chrys.service.context.compaction.last_words.asyncio.sleep", _sleep)

    class _Client(FallbackClient):
        async def get_response(self, messages, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            self.messages.append(list(messages))
            if self.calls <= 2:
                raise ConnectionError("connection dropped")

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = invalid

            return _Response()

    gen = make_generator(tmp_path, publish_retry=_publish_retry, max_transient_retries=2)
    client = _Client()
    gen._client = client  # type: ignore[assignment]

    out = await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert out == invalid
    assert client.calls == 4
    assert sleep_delays == [3, 7, 3]
    assert [(event.attempt, event.max_attempts, event.delay_seconds) for event in retry_events] == [
        (1, 2, 3),
        (2, 2, 7),
        (1, 1, 3),
    ]


async def test_short_terminal_retry_does_not_replace_adequate_invalid_note(tmp_path, monkeypatch):
    from chrys.service.context.compaction.last_words import LastWordsGenerator

    monkeypatch.setattr(LastWordsGenerator, "_MAX_CORRECTIVE_RETRIES", 1)
    monkeypatch.setattr(LastWordsGenerator, "_MIN_NOTE_CHARS", 300)
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    adequate_invalid = ("## Task\nDo it\n\n## Progress\n" + "work " * 100).rstrip()
    short_valid = structured_note()
    statuses, publish_status = status_collector()

    class _Client(FallbackClient):
        async def get_response(self, messages, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            self.calls += 1
            self.messages.append(list(messages))

            class _Response:
                usage_details = None
                additional_properties: ClassVar[dict[str, object]] = {}
                messages: ClassVar[tuple[Message, ...]] = ()
                raw_text = adequate_invalid if self.calls == 1 else short_valid

            return _Response()

    gen = make_generator(tmp_path, publish_status=publish_status)
    client = _Client()
    gen._client = client  # type: ignore[assignment]

    out = await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])

    assert out == adequate_invalid
    assert client.calls == 2
    assert statuses[-1].format_violation == 'missing required heading "## Next"'


@pytest.mark.parametrize("use_completer", [False, True], ids=["fallback", "completer"])
async def test_short_note_retries_then_accepts_adequate(tmp_path, monkeypatch, use_completer: bool) -> None:
    """A non-empty but sub-floor note is retried like an empty response, on both note paths.

    Observed live on Responses-API reasoning models: thinking consumed the
    whole output budget and the visible note collapsed to an 85-char
    mid-sentence fragment that passed the old non-empty check.
    """
    monkeypatch.setattr(LastWordsGenerator, "_MIN_NOTE_CHARS", 300)
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0, 0))
    retry_events, publish_retry = retry_collector()
    gen = make_generator(tmp_path, publish_retry=publish_retry)

    if use_completer:
        full_note = long_structured_note("xx")
        completer = FakeCompleter(["truncated mid-sentence fragment of a note", full_note])
        out = await generate(
            gen,
            user_request="do X",
            previous_last_words=None,
            dropped_messages=[],
            completer=completer,
        )
        assert out == full_note
        assert len(completer.calls) == 2
        assert gen._client is None  # fallback never touched
        assert len(retry_events) == 1
        assert "note too short" in retry_events[0].reason
        assert "< 300" in retry_events[0].reason
    else:
        monkeypatch.setattr(LastWordsGenerator, "_MAX_CORRECTIVE_RETRIES", 2)
        full_note = long_structured_note("gg")
        client = SequenceFallbackClient(["short", full_note])
        gen._client = client  # type: ignore[assignment]
        out = await generate(gen, user_request="do X", previous_last_words=None, dropped_messages=[])
        assert out == full_note
        assert client.calls == 2


@pytest.mark.parametrize("transient_budget", [0, 50])
async def test_completer_retry_budget_is_fixed_across_transient_budgets(tmp_path, monkeypatch, transient_budget):
    """The scoped completer is deliberately not env-wired: its fixed 2-retry
    budget holds at every CHRYS_MAX_TRANSIENT_RETRIES value (0 and 50)."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator

    monkeypatch.setattr(LastWordsGenerator, "_MIN_NOTE_CHARS", 300)
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))
    retry_events, publish_retry = retry_collector()

    gen = make_generator(
        tmp_path,
        max_transient_retries=transient_budget,
        publish_retry=publish_retry,
    )
    fallback_note = long_structured_note("ff")
    fallback = FallbackClient(fallback_note)
    gen._client = fallback  # type: ignore[assignment]
    completer = FakeCompleter(["tiny fragment"])

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
        completer=completer,
    )

    assert out == fallback_note
    # Initial attempt + exactly two retries, then demote — identical at both
    # budgets, proving the env value never stacks onto the completer lane.
    assert len(completer.calls) == 3
    assert fallback.calls == 1
    assert [(event.attempt, event.max_attempts) for event in retry_events] == [(1, 2), (2, 2)]


async def test_fallback_terminal_short_note_accepted_over_failing_live_call(tmp_path, monkeypatch):
    """Exhausting retries on a non-empty sub-floor note accepts it instead of raising.

    The code-owned base guidance encourages brevity ("keep the note tight"), so a
    compliant model can persistently answer below the floor; a degraded short
    note must not escalate into failing the user's in-flight live call.
    Empty responses keep the terminal raise (see
    ``test_last_words.py::test_last_words_generator_retries_empty_response_then_raises``)."""
    from chrys.service.context.compaction.last_words import LastWordsGenerator
    from chrys.service.profiles.models.resolver import default_profile

    monkeypatch.setattr(LastWordsGenerator, "_MIN_NOTE_CHARS", 300)
    monkeypatch.setattr(LastWordsGenerator, "_MAX_CORRECTIVE_RETRIES", 1)
    monkeypatch.setattr(LastWordsGenerator, "_BACKOFF_SCHEDULE", (0,))

    client = FallbackClient("persistently short")
    gen = LastWordsGenerator(profile=default_profile(), log_dir=tmp_path)
    gen._client = client  # type: ignore[assignment]

    out = await generate(
        gen,
        user_request="do X",
        previous_last_words=None,
        dropped_messages=[],
    )

    assert out == "persistently short"
    # The retry budget is still spent pushing for a floor-compliant note first.
    assert client.calls == 2
