# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fresh prompts preserve completed boundaries and retire only no-work turns."""

from __future__ import annotations

import pytest

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import Content, Message
from chrys.service.session.history import SessionHistoryManager


def _history(contents: list[Message]) -> SessionHistoryManager:
    manager = SessionHistoryManager()
    manager.bind({"messages": [Message("user", ["before"]), Message("assistant", ["done"])], "turn_counter": 0})
    manager.insert_turn_marker()
    manager.messages.extend(contents)
    manager.insert_interrupted_marker()
    manager.insert_turn_marker()
    return manager


def _guidance() -> Message:
    note = Message("user", ["try differently"])
    note.additional_properties[HistoryMarkerKind.INJECTED_KEY] = True
    return note


@pytest.mark.parametrize("thought", [False, True])
@pytest.mark.parametrize("guidance", [False, True])
def test_no_work_retires_the_entire_turn_including_its_guidance(thought: bool, guidance: bool) -> None:
    contents = [Message("user", ["failed opener"])]
    if thought:
        contents.append(Message("assistant", [Content.from_text_reasoning(text="private thought")]))
    if guidance:
        contents.append(_guidance())
    manager = _history(contents)

    assert manager.prepare_fresh_turn().retired_previous_turn is True

    assert manager.state["turn_counter"] == 1
    assert [message.text for message in manager.messages[:2]] == ["before", "done"]
    assert len(manager.messages) == 3
    assert manager.messages[-1].additional_properties["_turn"] == 1


@pytest.mark.parametrize("work", [Message("assistant", ["partial answer"]), Message("tool", ["tool result"])])
def test_retained_work_keeps_its_marker_and_drops_only_unanswered_guidance(work: Message) -> None:
    manager = _history([Message("user", ["retained opener"]), work, _guidance()])
    marker = manager.messages[-1]

    assert manager.prepare_fresh_turn().retired_previous_turn is False

    assert manager.state["turn_counter"] == 2
    assert manager.messages[-1] is marker
    assert manager.messages[-2] is work
    assert [message.text for message in manager.messages if message.role == "user"] == ["before", "retained opener"]
    assert manager.trailing_status_marker() is None


def test_tracker_work_keeps_a_turn_without_model_output() -> None:
    manager = _history([Message("user", ["retained opener"]), _guidance()])
    marker = manager.messages[-1]

    assert manager.prepare_fresh_turn(other_records_present=True).retired_previous_turn is False

    assert manager.state["turn_counter"] == 2
    assert manager.messages[-1] is marker
    assert [message.text for message in manager.messages if message.role == "user"] == ["before", "retained opener"]
    assert manager.trailing_status_marker() is None


@pytest.mark.parametrize("awaiting", [False, True])
def test_restored_unfinished_turn_gets_exactly_one_terminal_boundary(awaiting: bool) -> None:
    manager = SessionHistoryManager()
    manager.bind({"messages": [Message("user", ["unfinished"]), Message("assistant", ["partial"])], "turn_counter": 0})
    if awaiting:
        manager.upsert_awaiting_sub_agents_marker(["child"])

    assert manager.close_unfinished_turn() is True
    assert manager.close_unfinished_turn() is False

    assert manager.state["turn_counter"] == 1
    assert manager.messages[-1].additional_properties[HistoryMarkerKind.KEY] == HistoryMarkerKind.TURN
    assert manager.trailing_status_marker() is not None


@pytest.mark.parametrize("other_records", [False, True])
def test_summary_survives_empty_failed_turn_cleanup(other_records: bool) -> None:
    summary = Message(
        "assistant", ["Completed context"], additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY}
    )
    manager = SessionHistoryManager()
    manager.bind({"messages": [summary, Message("user", ["empty opener"])], "turn_counter": 4})
    manager.insert_interrupted_marker()
    manager.insert_turn_marker()

    result = manager.prepare_fresh_turn(other_records_present=other_records)

    assert result.retired_previous_turn is (not other_records)
    assert manager.messages[0] is summary
    assert manager.state["turn_counter"] == (5 if other_records else 4)
    assert [message.text for message in manager.messages if message.role == "user"] == (
        ["empty opener"] if other_records else []
    )


def test_retained_turn_reports_all_unanswered_notes_and_removes_their_thinking() -> None:
    answer = Message("assistant", ["Partial answer"])
    earlier_thought = Message("assistant", [Content.from_text_reasoning(text="Earlier thought")])
    manager = _history(
        [
            Message("user", ["opener"]),
            answer,
            earlier_thought,
            _guidance(),
            Message("assistant", [Content.from_text_reasoning(text="First unlanded thought")]),
            _guidance(),
            Message("assistant", [Content.from_text_reasoning(text="Second unlanded thought")]),
        ]
    )

    result = manager.prepare_fresh_turn()

    assert result.retired_previous_turn is False
    assert result.removed_followups == 2
    assert manager.messages[-2] is earlier_thought
    assert manager.messages[-1].additional_properties[HistoryMarkerKind.KEY] == HistoryMarkerKind.TURN


def test_restoring_summary_alone_does_not_create_an_unfinished_turn() -> None:
    summary = Message(
        "assistant", ["Completed context"], additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY}
    )
    manager = SessionHistoryManager()
    manager.bind({"messages": [summary], "turn_counter": 4})
    assert manager.close_unfinished_turn() is False
    assert manager.messages == [summary]
    assert manager.state["turn_counter"] == 4
