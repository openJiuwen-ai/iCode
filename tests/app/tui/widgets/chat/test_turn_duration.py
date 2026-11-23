# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The answer's header tells how long its turn took, live and replayed alike.

A turn's time runs from when its pass began, as the user sent the message or
pressed Continue or Retry, to when the answer finished: every model call, tool
run and wait for the user in between counts, and a message sent mid-turn does
not restart it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from textual.geometry import Region

from chrys.app.tui.widgets.chat.messages import AgentMessage, _AgentReplyHeader, turn_duration_ms
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.replay import (
    HistoryReplayPlanner,
    ReplayAgentMessage,
    ReplayStructuredCompletion,
)
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.trajectory_timing import TRAJECTORY_TIMING_KEY
from chrys.kernel import Content
from chrys.service.context.providers.history import TURN_INDEX_KEY, TURN_STARTED_AT_KEY
from chrys.service.session.message_metadata import MESSAGE_CREATED_AT_KEY
from tests.support.tui_helpers import ChatPanelApp

_SENT = datetime(2026, 8, 19, 1, 58, tzinfo=UTC)


def _at(seconds: float) -> str:
    return (_SENT + timedelta(seconds=seconds)).isoformat()


def _user(text: str, at: float, **extra: Any) -> dict[str, Any]:
    return {
        "role": "user",
        "contents": [{"type": "text", "text": text}],
        "additional_properties": {MESSAGE_CREATED_AT_KEY: _at(at), **extra},
    }


def _assistant(contents: list[dict[str, Any]], at: float, *, took_ms: int) -> dict[str, Any]:
    """A model response that finished *at*, after *took_ms* of its own."""
    timing = {"started_at": _at(at - took_ms / 1000), "finished_at": _at(at), "duration_ms": took_ms}
    return {
        "role": "assistant",
        "contents": contents,
        "additional_properties": {MESSAGE_CREATED_AT_KEY: _at(at), TRAJECTORY_TIMING_KEY: timing},
    }


def _answer(text: str, at: float, *, took_ms: int = 19_000) -> dict[str, Any]:
    return _assistant([{"type": "text", "text": text}], at, took_ms=took_ms)


def _command(call_id: str, at: float) -> list[dict[str, Any]]:
    """A response that ran one command, and its result."""
    call = {"type": "function_call", "call_id": call_id, "name": "zsh", "arguments": '{"command": "make test"}'}
    result = {"type": "function_result", "call_id": call_id, "result": "ok"}
    return [_assistant([call], at, took_ms=2_000), {"role": "tool", "contents": [result]}]


def _turn(index: int, *, started_at: float | None = None) -> dict[str, Any]:
    extra: dict[str, Any] = {HistoryMarkerKind.KEY: HistoryMarkerKind.TURN, TURN_INDEX_KEY: index}
    if started_at is not None:
        extra[TURN_STARTED_AT_KEY] = _at(started_at)
    return {"role": "assistant", "contents": [{"type": "text", "text": ""}], "additional_properties": extra}


def _interrupted() -> dict[str, Any]:
    return {
        "role": "assistant",
        "contents": [{"type": "text", "text": "Execution interrupted"}],
        "additional_properties": {HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED, "_interrupted_by": "user"},
    }


def _told(messages: list[dict[str, Any]]) -> list[int | None]:
    """The time each replayed agent message tells, in order."""
    entries = HistoryReplayPlanner().build_plan(messages).entries
    return [entry.duration_ms for entry in entries if isinstance(entry, ReplayAgentMessage)]


@pytest.mark.parametrize("marker_keeps_start", [True, False], ids=["marker", "older-marker"])
def test_the_answer_tells_its_whole_turn_not_its_last_response(marker_keeps_start: bool) -> None:
    """The model's last call took 19 s; the turn, with its commands, took over six minutes.

    A turn marker written before turns kept when their pass began times it from the opening message.
    """
    messages = [
        _user("Fix the tests", 0),
        *_command("c1", 60),
        *_command("c2", 200),
        _answer("Fixed.", 372),
        _turn(1, started_at=0 if marker_keeps_start else None),
    ]

    # The two commands replay as one group.
    assert _told(messages) == [None, 372_000]


def test_continue_times_the_turn_from_when_it_was_pressed() -> None:
    """Continue left the interrupted pass's work in place; its marker says when the new pass began."""
    messages = [
        _user("Fix the tests", 0),
        *_command("c1", 60),
        _answer("Half done", 90),
        *_command("c2", 3_620),
        _answer("Fixed.", 3_700),
        _turn(1, started_at=3_600),
    ]

    assert _told(messages) == [None, None, None, 100_000]


def test_a_message_sent_mid_turn_does_not_restart_its_time() -> None:
    """Timed from the opening message, as by an older marker: the message sent mid-turn does not open one."""
    messages = [
        _user("Fix the tests", 0),
        *_command("c1", 60),
        _user("Also the docs", 90, **{HistoryMarkerKind.INJECTED_KEY: True}),
        _answer("Fixed both.", 150),
        _turn(1),
    ]

    assert _told(messages) == [None, 150_000]


def test_a_message_sent_after_a_stopped_pass_opens_its_own_turn() -> None:
    """Sending it took the stopped pass's markers away; its flags, not its place, say it opens a turn."""
    messages = [
        _user("Fix the tests", 0),
        _answer("Half done", 30),
        _user("Fix the docs instead", 3_600),
        _answer("Fixed.", 3_610),
        _turn(1),
    ]

    assert _told(messages) == [None, 10_000]


def test_a_message_history_placed_after_the_answer_leaves_the_time_on_it() -> None:
    """A message sent mid-turn whose place history could not find lands at the turn's end, after the answer."""
    messages = [
        _user("Fix the tests", 0),
        _answer("Fixed.", 40),
        _user("Also the docs", 20, **{HistoryMarkerKind.INJECTED_KEY: True}),
        _turn(1, started_at=0),
    ]

    assert _told(messages) == [40_000]


def test_thinking_after_the_answer_leaves_the_time_on_it() -> None:
    thinking = {"type": "text_reasoning", "text": "All done."}
    messages = [_user("Fix", 0), _answer("Fixed.", 40), _assistant([thinking], 41, took_ms=500), _turn(1, started_at=0)]

    assert _told(messages) == [40_000, None]


def test_every_turn_is_timed_from_its_own_start() -> None:
    messages = [
        _user("First", 0),
        _answer("One.", 30),
        _turn(1),
        _user("Second", 600),
        _answer("Two.", 645),
        _turn(2),
    ]

    assert _told(messages) == [30_000, 45_000]


@pytest.mark.parametrize(
    "messages",
    [
        pytest.param([_user("Fix", 0), _answer("Half done", 30), _interrupted(), _turn(1, started_at=0)], id="stopped"),
        pytest.param([_user("Fix", 0), _answer("Working on it", 30)], id="still-running"),
        pytest.param([_user("Fix", 60), _answer("Fixed.", 90), _turn(1, started_at=120)], id="started-after"),
        pytest.param([_answer("Fixed.", 90), _turn(1)], id="no-start-known"),
    ],
)
def test_an_answer_with_no_finished_turn_to_time_tells_none(messages: list[dict[str, Any]]) -> None:
    """A pass that ended otherwise did not answer, as live; nor can a turn be timed with no start before its end."""
    assert set(_told(messages)) == {None}


@pytest.mark.parametrize("show_reasoning", [True, False], ids=["thinking-shown", "thinking-hidden"])
@pytest.mark.parametrize("thinking_after", [False, True], ids=["alone", "thinking-after"])
def test_a_structured_completion_tells_its_turn(thinking_after: bool, show_reasoning: bool) -> None:
    """The ✓ follows the thinking after the output it completes, and tells the turn's time, as live."""
    hosted = {"hosted_family": "generic", "hosted_provider": "openai"}
    call = Content.from_hosted_tool_call(
        "hosted-1", tool_name="server_task", status="running", provider_phase="start", **hosted
    ).to_dict()
    result = Content.from_hosted_tool_result(
        "hosted-1", tool_name="server_task", result="done", status="completed", provider_phase="terminal", **hosted
    ).to_dict()
    thinking = [_assistant([{"type": "text_reasoning", "text": "Done."}], 76, took_ms=500)] if thinking_after else []
    messages = [
        _user("Run the task", 0),
        _assistant([call, result], 75, took_ms=999),
        *thinking,
        _turn(1, started_at=0),
    ]

    entries = HistoryReplayPlanner().build_plan(messages, show_reasoning=show_reasoning).entries

    completions = [entry for entry in entries if isinstance(entry, ReplayStructuredCompletion)]
    assert completions == [ReplayStructuredCompletion(_at(75), 75_000)]
    assert {entry.duration_ms for entry in entries if isinstance(entry, ReplayAgentMessage)} == {None}


@pytest.mark.parametrize(
    ("started_at", "finished_at", "told"),
    [
        (_SENT, _SENT + timedelta(minutes=6, seconds=5), 365_000),
        (_at(0), _at(1.5), 1_500),
        ("2026-08-19T01:58:00", _at(2), 2_000),
        (_at(0), _at(0), 0),
        (_at(5), _at(0), None),
        (None, _at(0), None),
        ("not a time", _at(0), None),
    ],
    ids=["datetimes", "strings", "no-zone-is-utc", "at-once", "out-of-order", "no-start", "not-a-time"],
)
def test_turn_duration_ms(started_at: Any, finished_at: Any, told: int | None) -> None:
    assert turn_duration_ms(started_at, finished_at) == told


async def _replayed_header(messages: list[dict[str, Any]]) -> str:
    app = ChatPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        await panel.replay_history(messages, initial_profile="Code Agent")
        await panel.wait_replay_complete()
        await pilot.pause()
        return panel.query_one(AgentMessage)._header_text().plain


@pytest.mark.parametrize("streamed", [True, False], ids=["streamed", "not-streamed"])
async def test_live_and_replayed_answers_tell_the_same_time(streamed: bool) -> None:
    finished = _SENT + timedelta(minutes=6, seconds=5)
    app = ChatPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        panel.set_profile("Code Agent")
        await panel.add_user_message("Fix the tests", created_at=_SENT)
        if streamed:
            await panel.add_answer_delta("seg-1", "Fixed.")
            panel.finish_answer_segment("seg-1", completed=True)
        await panel.add_agent_message("Fixed.", created_at=finished, turn_started_at=_SENT)
        await pilot.pause()
        live = panel.query_one(AgentMessage)._header_text().plain

    replayed = await _replayed_header([_user("Fix the tests", 0), _answer("Fixed.", 365), _turn(1, started_at=0)])

    assert live.endswith("(6m 5s)")
    assert live == replayed


async def test_a_live_answer_with_no_known_start_tells_no_time() -> None:
    app = ChatPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_answer_delta("seg-1", "Fixed.")
        panel.finish_answer_segment("seg-1", completed=True)
        await panel.add_agent_message("Fixed.", created_at=_SENT)
        await pilot.pause()

        assert panel.query_one(AgentMessage)._duration_ms is None


@pytest.mark.parametrize("boundary", ["context-fold", "message-sent-mid-turn"])
async def test_the_streamed_answer_keeps_its_time_past_a_boundary_before_the_turn_completes(boundary: str) -> None:
    """A fold or a message sent mid-turn lands after the answer streamed: the turn's completion still times it."""
    finished = _SENT + timedelta(minutes=6, seconds=5)
    app = ChatPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_user_message("Fix the tests", created_at=_SENT)
        await panel.add_answer_delta("seg-1", "Fixed.")
        panel.finish_answer_segment("seg-1", completed=True)
        if boundary == "context-fold":
            await panel.add_context_fold("ctx-1", "Earlier work", freed_messages=3)
        else:
            await panel.add_user_message("Also the docs", is_injection=True, created_at=_SENT + timedelta(minutes=6))
        await panel.add_agent_message("Fixed.", created_at=finished, turn_started_at=_SENT)
        await pilot.pause()

        answer = panel.query_one(AgentMessage)
        assert answer._header_text().plain.endswith("(6m 5s)")
        assert answer.text == "Fixed."


async def test_text_tools_followed_tells_no_time_though_a_boundary_let_it_go() -> None:
    app = ChatPanelApp()
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_user_message("Fix the tests", created_at=_SENT)
        await panel.add_answer_delta("seg-1", "Let me look.")
        panel.finish_answer_segment("seg-1", completed=True)
        await panel.add_context_fold("ctx-1", "Earlier work", freed_messages=3)
        await panel.add_tool_start("read-1", "read_file", "filesystem.read", "path=/tmp/file")
        await panel.add_agent_message("", created_at=_SENT + timedelta(minutes=1), turn_started_at=_SENT)
        await pilot.pause()

        assert [message._duration_ms for message in panel.query(AgentMessage)] == [None]


async def test_a_streamed_answer_shows_its_time_as_the_turn_completes_it() -> None:
    """The header widens for the time it learns at the end: nothing of it is cut, live as replayed."""
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("hello")
        await panel.add_answer_delta("a", "Hello!")
        await pilot.pause()
        panel.finish_answer_segment("a", completed=True)
        await panel.add_agent_message("Hello!", created_at=_at(2), turn_started_at=_SENT.isoformat())
        await pilot.pause()
        answer = panel.query(AgentMessage).last()
        header = answer.query_one(_AgentReplyHeader)
        painted = header.render_lines(Region(0, 0, header.size.width, 1))[0].text
        assert painted.rstrip() == answer._header_text().plain
        assert "(2s)" in painted
