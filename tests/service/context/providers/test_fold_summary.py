# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What a fold made without the model keeps of each turn it replaces."""

from __future__ import annotations

import pytest

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_WRITE, TOOL_CALL_KIND_METADATA_KEY
from chrys.kernel import Content, Message
from chrys.service.context.providers.fold_summary import (
    EMPTY_FOLD_SUMMARY,
    build_fold_summary,
    first_user_text,
    fold_summary_turn_chars,
)


def _user(text: str) -> Message:
    return Message("user", [text])


def _assistant(text: str) -> Message:
    return Message("assistant", [text])


def _call(call_id: str, name: str, arguments: dict | str | None = None, *, kind: str | None = None) -> Message:
    content = Content.from_function_call(call_id, name, arguments=arguments or {})
    if kind is not None:
        content.additional_properties[TOOL_CALL_KIND_METADATA_KEY] = kind
    return Message("assistant", [content])


def _result(call_id: str, text: str = "ok") -> Message:
    return Message("tool", [Content.from_function_result(call_id, result=text)])


def _write(call_id: str, path: str, name: str = "edit_file") -> list[Message]:
    return [_call(call_id, name, {"path": path, "content": "..."}, kind=KIND_FILESYSTEM_WRITE), _result(call_id)]


def _marker(turn: int) -> Message:
    message = Message("assistant", [""])
    message.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    message.additional_properties["_turn_id"] = f"turn_{turn}"
    message.additional_properties["_turn"] = turn
    return message


@pytest.mark.parametrize(
    ("window", "expected"),
    [(0, 2000), (60_000, 600), (128_000, 640), (200_000, 1000), (400_000, 2000), (1_000_000, 2000)],
)
def test_a_turn_gets_half_a_percent_of_the_window_within_fixed_bounds(window: int, expected: int) -> None:
    assert fold_summary_turn_chars(window) == expected


def test_the_final_answer_is_the_text_after_the_last_tool_work() -> None:
    turn = [
        _user("Fix the login bug"),
        _assistant("Let me look at the code first."),
        _call("c1", "read_file", {"path": "auth.py"}),
        _result("c1", "def login(): ..."),
        _assistant("The cookie was read before it was set; fixed and tests pass."),
    ]

    summary = build_fold_summary(turn)

    assert "Assistant: The cookie was read before it was set; fixed and tests pass." in summary
    assert "Let me look at the code first" not in summary


def test_a_turn_that_ended_on_tool_work_has_no_answer_line() -> None:
    turn = [_user("Run it"), _assistant("Starting."), _call("c1", "shell", {"command": "make"}), _result("c1")]

    summary = build_fold_summary(turn)

    assert "Assistant:" not in summary
    assert summary.splitlines() == ["User: Run it", "Tools: shell x1"]


def test_every_real_user_message_is_kept_and_the_continue_nudge_is_not() -> None:
    injected = _user("also keep the old cookie name")
    injected.additional_properties[HistoryMarkerKind.INJECTED_KEY] = True
    nudge = _user("continue")
    nudge.additional_properties[HistoryMarkerKind.CONTINUATION_KEY] = True
    turn = [_user("Fix the login bug"), _assistant("On it."), injected, nudge, _assistant("Done.")]

    lines = build_fold_summary(turn).splitlines()

    assert lines == ["User: Fix the login bug", "User: also keep the old cookie name", "Assistant: Done."]


def test_changed_files_come_from_write_calls_once_each_in_first_use_order() -> None:
    turn = [
        _user("Refactor"),
        _call("r1", "read_file", {"path": "src/only_read.py"}),
        _result("r1"),
        *_write("w1", "src/b.py"),
        *_write("w2", "src/a.py", name="write_file"),
        *_write("w3", "src/b.py"),
        _assistant("Refactored."),
    ]

    summary = build_fold_summary(turn)

    assert "Files changed: src/b.py, src/a.py" in summary.splitlines()
    assert "only_read.py" not in summary


def test_write_call_arguments_sent_as_json_text_name_the_file_too() -> None:
    turn = [
        _user("Write it"),
        _call("w1", "write_file", '{"path": "notes.md", "content": "x"}', kind=KIND_FILESYSTEM_WRITE),
        _result("w1"),
    ]

    assert "Files changed: notes.md" in build_fold_summary(turn)


def test_at_most_twenty_changed_files_are_named_and_the_rest_counted() -> None:
    turn = [_user("Rename everything")]
    for index in range(25):
        turn.extend(_write(f"w{index}", f"f{index}.py"))

    files_line = next(line for line in build_fold_summary(turn).splitlines() if line.startswith("Files changed:"))

    assert "f19.py" in files_line
    assert "f20.py" not in files_line
    assert files_line.endswith("(+5 more)")


def test_tools_are_listed_with_how_often_each_was_called() -> None:
    turn = [_user("Look around")]
    for index in range(12):
        turn.extend([_call(f"r{index}", "read_file", {"path": f"f{index}.py"}), _result(f"r{index}")])
    for index in range(3):
        turn.extend(_write(f"e{index}", f"f{index}.py"))

    assert "Tools: edit_file x3, read_file x12" in build_fold_summary(turn).splitlines()


def test_a_long_answer_keeps_its_start_and_its_end() -> None:
    answer = "Root cause: the cache key ignored the locale. " + "detail " * 600 + "All 214 tests pass."
    turn = [_user("Why is the page stale?"), _assistant(answer)]

    line = next(line for line in build_fold_summary(turn).splitlines() if line.startswith("Assistant:"))

    assert line.startswith("Assistant: Root cause: the cache key ignored the locale.")
    assert line.endswith("All 214 tests pass.")
    assert "chars omitted" in line
    assert len(line) <= len("Assistant: ") + 1500


@pytest.mark.parametrize("turn_chars", [600, 640, 1000, 2000])
def test_a_turn_never_takes_more_than_its_share(turn_chars: int) -> None:
    turn = [_user("Request " + "context " * 300)]
    for index in range(30):
        turn.extend(_write(f"w{index}", f"src/package/module_{index}/implementation_file.py"))
    for index in range(40):
        turn.extend([_call(f"t{index}", f"tool_number_{index}"), _result(f"t{index}")])
    turn.extend([_user("Second request " + "more " * 300), _assistant("Answer " + "word " * 900 + "end.")])

    summary = build_fold_summary(turn, turn_chars=turn_chars)

    assert len(summary) <= turn_chars
    for label in ("User: ", "Assistant: ", "Files changed: ", "Tools: "):
        assert any(line.startswith(label) for line in summary.splitlines())


def test_the_request_keeps_room_beside_a_long_answer_in_a_small_window() -> None:
    turn = [_user("Fix the login bug in auth.py " + "detail " * 200), _assistant("Fixed. " * 500 + "Tests pass.")]

    lines = build_fold_summary(turn, turn_chars=600).splitlines()

    assert lines[0].startswith("User: Fix the login bug in auth.py")
    assert len(lines[0]) > 150
    assert lines[1].endswith("Tests pass.")


def test_user_messages_that_do_not_fit_are_counted() -> None:
    turn = [_user(f"Instruction {index}: " + "text " * 100) for index in range(30)]

    lines = build_fold_summary(turn, turn_chars=600).splitlines()

    assert lines[0].startswith("User: Instruction 0:")
    shown = sum(line.startswith("User: ") for line in lines)
    assert lines[-1] == f"(+{30 - shown} more user messages)"
    assert len("\n".join(lines)) <= 600


def test_a_fold_of_several_turns_summarises_each_in_order() -> None:
    fold_range = [
        _user("First request"),
        _assistant("First answer."),
        _marker(1),
        _user("Second request"),
        _assistant("Second answer."),
        _marker(2),
    ]

    assert build_fold_summary(fold_range).splitlines() == [
        "User: First request",
        "Assistant: First answer.",
        "User: Second request",
        "Assistant: Second answer.",
    ]


def test_a_fold_with_nothing_to_extract_says_so() -> None:
    assert build_fold_summary([_marker(1)]) == EMPTY_FOLD_SUMMARY
    assert build_fold_summary([]) == EMPTY_FOLD_SUMMARY


def test_the_same_history_always_folds_into_the_same_text() -> None:
    turn = [_user("Do it"), *_write("w1", "a.py"), _assistant("Done.")]

    assert build_fold_summary(turn) == build_fold_summary(list(turn))


def test_first_user_text_is_one_clipped_line_of_the_first_real_user_message() -> None:
    nudge = _user("continue")
    nudge.additional_properties[HistoryMarkerKind.CONTINUATION_KEY] = True
    messages = [nudge, _assistant("ok"), _user("Fix the\nlogin   bug " + "x" * 200)]

    label = first_user_text(messages, 80)

    assert label.startswith("Fix the login bug x")
    assert len(label) == 80
    assert label.endswith("…")
    assert first_user_text([_assistant("only an answer")], 80) == ""
