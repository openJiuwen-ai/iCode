# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""While a turn runs, its process folds behind one row that shows the latest step; a click opens it.

Each process segment's thinking, text between tool calls and tool groups hide behind one temporary
row as they arrive. The row reads the latest member's latest status, and opens to the members row by
row. The answer that streams is not a member. When the turn ends the row goes and the turn takes the
shape a finished turn has always had.
"""

from __future__ import annotations

import re
from types import ModuleType
from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec

import pytest
from textual.geometry import Region
from textual.widgets import Static

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.widgets.ask_user_controls import AskUserOptions
from chrys.app.tui.widgets.chat import tool_call, tool_renderers
from chrys.app.tui.widgets.chat.agent_transcript_surface import (
    AgentTranscriptJournal,
    AgentTranscriptSurface,
    TranscriptAssistantOp,
    TranscriptToolResultOp,
    TranscriptToolStartOp,
    TranscriptUserOp,
)
from chrys.app.tui.widgets.chat.compaction_card import CompactionCard
from chrys.app.tui.widgets.chat.live_process_row import LiveProcessRow
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    MessageRollbackButton,
    SystemMessage,
    ThinkingMessage,
    UserMessage,
    _FoldRowHeader,
)
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.process_block import ProcessBlock
from chrys.app.tui.widgets.chat.selection_controller import ChatSelectionController
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.chat.waiting_row import WaitingRow
from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown
from chrys.foundation.config.settings import Settings
from chrys.foundation.models.ask_user import AskUserOption, AskUserQuestion
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.tool_kinds import (
    KIND_ASK_USER,
    KIND_FILESYSTEM_READ,
    KIND_SHELL,
    KIND_SLEEP,
    KIND_SUB_AGENT,
    TOOL_CALL_KIND_METADATA_KEY,
)
from chrys.kernel import Content, Message
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import ChatPanelApp, LocalizedWidgetApp, chat_content_children, click_when_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import App
    from textual.pilot import Pilot
    from textual.widget import Widget

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


def _on() -> bool:
    return True


# ---------------------------------------------------------------------------
# The row and what it shows
# ---------------------------------------------------------------------------


def _rows(panel: Widget) -> list[LiveProcessRow]:
    return list(panel.query(LiveProcessRow))


def _row(panel: Widget) -> LiveProcessRow:
    rows = _rows(panel)
    assert len(rows) == 1, rows
    return rows[0]


def _painted(widget: Widget) -> str:
    """The row as it paints on screen, at the width layout gave it."""
    width = widget.size.width
    if not width or not widget.display:
        return ""
    strips = widget.render_lines(Region(0, 0, width, 1))
    return strips[0].text.rstrip() if strips else ""


_LEADS = ("▸ ", "▾ ", "• ", "◦ ")
"""What a row paints before its words: the fold arrow, or the running dot that may take its place."""


def _status(row: Widget) -> str:
    text = _painted(row).strip()
    for lead in _LEADS:
        if text.startswith(lead):
            return text[len(lead) :]
    return text


async def _settled(pilot: Pilot[Any]) -> None:
    await wait_for(lambda: screen_is_settled(pilot.app, pilot.app.screen), pilot=pilot, description="screen settled")


async def _status_shows(pilot: Pilot[Any], panel: ChatPanel, words: str) -> None:
    await wait_for(
        lambda: len(_rows(panel)) == 1 and words in _status(_rows(panel)[0]),
        pilot=pilot,
        description=f"the live row to show {words!r}",
    )


def _members(panel: ChatPanel) -> list[Widget]:
    """Thinking, text between tool calls and tool groups, in document order."""
    return [
        child
        for child in chat_content_children(panel)
        if isinstance(child, (ThinkingMessage, ToolGroup)) or (isinstance(child, AgentMessage) and child.foldable)
    ]


def _shown(widgets: list[Widget]) -> list[bool]:
    return [widget.display for widget in widgets]


# ---------------------------------------------------------------------------
# Driving a turn through the panel's live API, as the screen does
# ---------------------------------------------------------------------------


async def _think(panel: ChatPanel, segment_id: str, *texts: str, finish: bool = True) -> None:
    for text in texts:
        await panel.add_thinking_delta(segment_id, "reasoning", text)
    if finish:
        panel.finish_thinking_segment(segment_id)


async def _say(panel: ChatPanel, segment_id: str, text: str) -> None:
    """Text that streams and completes: an answer until a tool call follows it."""
    await panel.add_answer_delta(segment_id, text)
    panel.finish_answer_segment(segment_id, completed=True)


async def _read(panel: ChatPanel, call_id: str, path: str, *, done: bool = True) -> None:
    await panel.add_tool_start(call_id, "read_file", KIND_FILESYSTEM_READ, args={"path": path})
    if done:
        await panel.add_tool_result(call_id, "read_file", "ok")


async def _finish(panel: ChatPanel, text: str = "Done.") -> None:
    await _say(panel, "final", text)
    await panel.add_agent_message(text)


def _summary(panel: ChatPanel) -> list[tuple[int, int, bool, str]]:
    return [
        (block.segment_index, block.tool_count, block.collapsed, _painted(block)) for block in panel.query(ProcessBlock)
    ]


class _FrameLog:
    """What *probe* read at each frame the app put on screen."""

    def __init__(self, app: App[Any], probe: Callable[[], object]) -> None:
        self.frames: list[object] = []
        display = app._display

        def recording(screen: Any, renderable: Any) -> None:
            if renderable is not None and not app._batch_count:
                self.frames.append(probe())
            display(screen, renderable)

        app._display = recording  # type: ignore[method-assign]


def _questions() -> tuple[AskUserQuestion, ...]:
    return (AskUserQuestion("Pick?", options=(AskUserOption("Python"), AskUserOption("Rust"))),)


# ---------------------------------------------------------------------------
# §5 state: before and at the first member
# ---------------------------------------------------------------------------


async def test_no_row_before_the_first_member_and_a_streaming_answer_is_not_one() -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _settled(pilot)
        assert not _rows(panel)
        await panel.add_answer_delta("a", "Streaming the answer")
        await _settled(pilot)
        assert not _rows(panel)
        assert panel.query(AgentMessage).last().display


@pytest.mark.parametrize("first", ["thinking", "tool"])
async def test_the_first_member_is_hidden_and_folded_from_its_first_frame(first: str) -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _settled(pilot)
        log = _FrameLog(
            pilot.app,
            lambda: (
                _shown(_members(panel)),
                len(_rows(panel)),
                sum(len(thinking.query(VirtualizedMarkdown)) for thinking in panel.query(ThinkingMessage)),
            ),
        )
        if first == "thinking":
            await _think(panel, "r1", "Let me look ", "at the retry path first.", finish=False)
        else:
            await _read(panel, "c1", "/tmp/a.py", done=False)
        await _settled(pilot)

        row = _row(panel)
        assert row.collapsed
        (member,) = _members(panel)
        assert not member.display
        assert member.collapsed, "a member hidden behind the row is folded, whatever the default-open settings say"
        assert not panel.query(ThinkingMessage) or not panel.query_one(ThinkingMessage).query(VirtualizedMarkdown)
        assert log.frames, "the test saw frames"
        for shown, rows, bodies in log.frames:  # type: ignore[misc]
            assert not any(shown), "a member was on screen for a frame"
            assert bodies == 0, "hidden thinking laid out its body"
            if shown:
                # A member is mounted: its row came in the same frame.
                assert rows == 1, "a member arrived without its row"


# ---------------------------------------------------------------------------
# §4.2 the latest status
# ---------------------------------------------------------------------------


async def test_streaming_thinking_shows_a_growing_token_count_in_the_row() -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "word " * 60, finish=False)
        await _status_shows(pilot, panel, "Thinking")
        pattern = re.compile(r"Thinking \((\S+) tokens?")
        await wait_for(
            lambda: pattern.search(_status(_row(panel))) is not None,
            pilot=pilot,
            description="the row counts the thinking's tokens",
        )
        first = pattern.search(_status(_row(panel))).group(1)  # type: ignore[union-attr]
        await _think(panel, "r1", "word " * 600, finish=False)
        await wait_for(
            lambda: (match := pattern.search(_status(_row(panel)))) is not None and match.group(1) != first,
            pilot=pilot,
            description="the token count follows the thinking as it streams on",
        )


async def test_finished_thinking_shows_its_opening_sentence() -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "Look at the retry path. Then the tests.")
        await _status_shows(pilot, panel, "Look at the retry path.")
        assert "Thought" in _status(_row(panel))
        assert _painted(_row(panel)).startswith("▸ ")


async def test_text_settled_after_its_tool_group_mounted_is_not_the_latest_member() -> None:
    """The group mounts before the text above it settles: document order, not arrival, names the latest."""
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py")
        await _say(panel, "a1", "Now the config file.")
        await _read(panel, "c2", "/tmp/b.py", done=False)
        await _status_shows(pilot, panel, "Reading /tmp/b.py")
        await panel.add_tool_result("c2", "read_file", "ok")
        # The tool group is still the latest member; the text before it is not newer.
        await _settled(pilot)
        assert "Now the config file." not in _status(_row(panel))


async def test_the_row_follows_each_running_call_of_its_group() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py", done=False)
        await _status_shows(pilot, panel, "Reading /tmp/a.py")
        await panel.add_tool_start("c2", "zsh", KIND_SHELL, args={"command": "sleep 5", "reason": "Wait five seconds"})
        await _status_shows(pilot, panel, "Wait five seconds")
        # The later call ends first: the one still running is the activity again.
        await panel.add_tool_result("c2", "zsh", "ok")
        await _status_shows(pilot, panel, "Reading /tmp/a.py")
        # An approval edits the running call's arguments.
        panel.update_tool_args("c1", {"path": "/tmp/edited.py"})
        await _status_shows(pilot, panel, "Reading /tmp/edited.py")


async def test_a_running_sleep_counts_down_in_the_row() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await panel.add_tool_start("z", "sleep", KIND_SLEEP, args={"seconds": 20, "reason": "CI"})
        await _status_shows(pilot, panel, "Sleeping: CI")
        await wait_for(
            lambda: re.search(r"\d+s left", _status(_row(panel))) is not None,
            pilot=pilot,
            description="the row shows the time the sleep has left",
        )


@pytest.mark.parametrize("count", [1, 64])
@pytest.mark.parametrize("channel", ["answer", "thinking"])
async def test_a_stream_delta_does_not_visit_folded_steps(
    count: int, channel: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        panel.agent_running = True
        if channel == "answer":
            await panel.add_answer_delta("a1", "Answer")
        for index in range(count):
            await _think(panel, f"r{index}", f"Step {index}.")
        if channel == "thinking":
            await _think(panel, "next", "A new", finish=False)
        await _settled(pilot)
        visits = 0
        display = ThinkingMessage.display

        def visit(member: ThinkingMessage) -> bool:
            nonlocal visits
            visits += 1
            return display.__get__(member)

        with monkeypatch.context() as patch:
            patch.setattr(ThinkingMessage, "display", property(visit, display.fset))
            if channel == "answer":
                await panel.add_answer_delta("a1", " grows.")
            else:
                await _think(panel, "next", " thought.", finish=False)
        await _settled(pilot)
        assert not panel.query_one(WaitingRow).display
        if channel == "answer":
            assert panel.query_one(AgentMessage).text == "Answer grows."
        else:
            assert panel.query(ThinkingMessage).last().text == "A new thought."
        assert visits == 0


@pytest.mark.parametrize("count", [1, 64])
async def test_sleep_animation_frames_do_not_revisit_tool_records(count: int, monkeypatch: pytest.MonkeyPatch) -> None:
    now = 100.0
    clock = ModuleType("time")
    monkeypatch.setattr(clock, "monotonic", lambda: now, raising=False)
    monkeypatch.setattr(tool_call, "time", clock)

    visits = 0

    class CountedRecords(dict):
        def values(self):
            nonlocal visits
            for record in super().values():
                visits += 1
                yield record

    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        for index in range(count - 1):
            await _read(panel, f"c{index}", f"{index}.py")
        await panel.add_tool_start("sleep", "sleep", KIND_SLEEP, args={"seconds": 20, "reason": "CI"})
        await _status_shows(pilot, panel, "20s left")
        group = panel.query_one(ToolGroup)
        monkeypatch.setattr(group, "_tool_records", CountedRecords(group._tool_records))
        now += 3
        row = _row(panel)
        row.refresh(layout=False)
        assert _painted(row) == "▸ Sleeping: CI · 17s left"
        assert visits == 0


async def test_automatic_retries_keep_the_latest_step_in_one_row() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        panel.agent_running = True
        for attempt in (1, 2):
            await _read(panel, f"c{attempt}", f"attempt-{attempt}.py", done=False)
            await panel.prepare_retry()
            await panel.add_retry("Retrying", attempt, 3, 30)
        await _read(panel, "c3", "attempt-3.py", done=False)
        await _settled(pilot)
        assert [_status(row) for row in _rows(panel) if row.display] == ["Reading attempt-3.py"]


async def test_steering_after_the_answer_starts_restores_the_latest_step() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "original", "original.py")
        await panel.add_answer_delta("a1", "Partial answer.")
        await _status_shows(pilot, panel, "1 tool call: 1 read")
        row = _row(panel)

        preview = panel.query_one(AgentMessage)
        await panel.end_steered_pass()
        assert preview.is_provisional and not preview.query(VirtualizedMarkdown)
        await panel.withdraw_answer(("a1",))
        await _settled(pilot)
        withdrawn = _painted(row)
        await panel.add_user_message("Try this instead", is_injection=True)
        await _settled(pilot)

        assert _painted(row) == withdrawn == "▸ Reading original.py"


async def test_a_boundary_without_withdrawal_also_freezes_the_latest_step() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "original", "original.py")
        await panel.add_answer_delta("a1", "Partial answer.")
        await _status_shows(pilot, panel, "1 tool call: 1 read")
        await panel.add_user_message("Also check this", is_injection=True)
        await _settled(pilot)
        assert _painted(_row(panel)) == "▸ Reading original.py"


@pytest.mark.parametrize("surface", ["live", "finished", "replayed"])
async def test_a_process_title_stays_above_the_thinking_and_answer_on_every_surface(surface: str) -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 40)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        if surface == "replayed":
            await panel.replay_history(
                [
                    Message("user", [Content.from_text("prompt")]).to_dict(),
                    Message("assistant", [Content.from_text("The answer.")]).to_dict(),
                    Message("assistant", [Content.from_text_reasoning(text="First thought.")]).to_dict(),
                    Message("assistant", [Content.from_text_reasoning(text="Second thought.")]).to_dict(),
                ]
            )
        else:
            await panel.add_user_message("prompt")
            await _say(panel, "a1", "The answer.")
            await _think(panel, "r1", "First thought.")
            await _think(panel, "r2", "Second thought.")
            if surface == "finished":
                await panel.add_agent_message("The answer.")
        await _settled(pilot)
        title = panel.query_one(LiveProcessRow if surface == "live" else ProcessBlock)
        user = panel.query_one(UserMessage)
        answer = panel.query_one(AgentMessage)
        thoughts = list(panel.query(ThinkingMessage))

        def visible_entries() -> list[object]:
            return [
                child
                for child in panel.displayed_children
                if isinstance(child, (UserMessage, AgentMessage, ThinkingMessage, LiveProcessRow, ProcessBlock))
            ]

        assert len(thoughts) == 2
        assert visible_entries() == [user, title, answer]
        assert _painted(title) == "▸ Activity"
        await click_when_settled(pilot, title)
        await _settled(pilot)
        ordered = [answer, *thoughts] if surface == "live" else [*thoughts, answer]
        assert visible_entries() == [user, title, *ordered]
        assert [entry.region.y for entry in ordered] == sorted(entry.region.y for entry in ordered)
        assert _painted(title) == "▾ Activity"
        assert panel.transcript_entry_after(ordered[-2]) is ordered[-1]
        assert panel.toggle_fold_all()
        await _settled(pilot)
        assert visible_entries() == [user, title, answer]
        assert _painted(title) == "▸ Activity"


async def test_preview_keeps_its_place_until_the_final_reply_moves_below_later_activity() -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 40)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.agent_running = True
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "First thought.")
        title = _row(panel)
        await click_when_settled(pilot, title)
        await panel.add_answer_delta("a1", "The answer begins.")
        answer = panel.query_one(AgentMessage)
        first = panel.query_one(ThinkingMessage)
        await _settled(pilot)
        original_y = title.region.y
        selection = ChatSelectionController(pilot.app.screen)
        selection._panel = panel
        user_body = panel.query_one(UserMessage).query_one(VirtualizedMarkdown)
        preview = answer.query_one(_FoldRowHeader)
        start = selection._leaf_anchor(panel, user_body, None)
        end = selection._leaf_anchor(panel, preview, None)
        assert start is not None and end is not None
        assert selection._commit_range(panel, start, end)

        await _think(panel, "r2", "Another thought.", finish=False)
        second = panel.query(ThinkingMessage).last()
        await panel.add_answer_delta("a1", " It keeps streaming.")
        await _settled(pilot)
        assert title.region.y == original_y
        assert [child for child in panel.displayed_children if child in (title, first, second, answer)] == [
            title,
            first,
            answer,
            second,
        ]
        assert first.region.bottom <= answer.region.y < second.region.y
        assert panel.query_one(AgentMessage) is answer
        assert preview.render().plain.startswith("▸ Replying...")
        assert answer.text == "The answer begins. It keeps streaming."
        assert "Replying..." in (selection.extract_text() or "")
        assert not panel.query_one(WaitingRow).display

        panel.finish_answer_segment("a1", completed=True)
        await panel.add_system("A notice after the preview")
        await panel.add_agent_message("The answer begins. It keeps streaming.")
        await _settled(pilot)
        final = panel.query_one(AgentMessage)
        assert not answer.is_attached
        assert panel.current_turn_children()[-1] is final
        assert final.query_one(VirtualizedMarkdown).shown_source == "The answer begins. It keeps streaming."


async def test_text_followed_by_tools_keeps_arrival_order_without_scanning_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "first", "first.py")
        scan = create_autospec(panel.current_turn_children, side_effect=panel.current_turn_children)
        monkeypatch.setattr(panel, "current_turn_children", scan)
        for index in range(4):
            await _say(panel, f"a{index}", "Checking another file.")
            await _read(panel, f"c{index}", f"{index}.py")
        scan.assert_not_called()


async def test_finishing_an_open_thought_hides_it_under_its_folded_process() -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "Working.", finish=False)
        row = _row(panel)
        await click_when_settled(pilot, row)
        thought = panel.query_one(ThinkingMessage)
        await click_when_settled(pilot, thought.query_one(".fold-row-header"))
        await click_when_settled(pilot, row)
        assert row.collapsed and thought.display and not thought.collapsed
        panel.finish_thinking_segment("r1")
        await panel.add_answer_delta("a1", "Done.")
        await _settled(pilot)
        assert row.collapsed and not thought.display
        await click_when_settled(pilot, row)
        assert thought.display


async def test_paused_sub_agent_reusing_a_previous_turns_id_has_its_own_card() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("first turn")
        await _read(panel, "same", "first.py")
        await _finish(panel)
        await panel.add_user_message("second turn")
        await panel.add_tool_start("same", "Explore", KIND_SUB_AGENT, args={"task": "inspect"})
        panel.link_sub_agent_invocation("same", "sub", "Explore", tool_name="Explore")
        panel.sub_agent_paused("sub", "acp_transport", "connection closed", 2)
        await _settled(pilot)
        first, second = panel.query(ToolGroup)
        assert first.all_complete and first._tool_records["same"].result == "ok"
        assert second.get_tool("same") is not None and second.collapse_locked and second.display


@pytest.mark.parametrize("finished", [False, True])
async def test_the_segment_title_leaves_a_notice_where_it_appeared(finished: bool) -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 40)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _say(panel, "a1", "The answer.")
        await panel.add_system("A notice.")
        await _think(panel, "r1", "First thought.")
        await _think(panel, "r2", "Second thought.")
        if finished:
            await panel.add_agent_message("The answer.")
        await _settled(pilot)
        title = panel.query_one(ProcessBlock if finished else LiveProcessRow)
        await click_when_settled(pilot, title)
        await _settled(pilot)
        activity = [panel.query_one(SystemMessage), *panel.query(ThinkingMessage)]
        answer = panel.query_one(AgentMessage)
        expected = [*activity, answer] if finished else [answer, *activity]
        assert [
            child
            for child in panel.displayed_children
            if isinstance(
                child, (UserMessage, LiveProcessRow, ProcessBlock, SystemMessage, ThinkingMessage, AgentMessage)
            )
        ] == [panel.query_one(UserMessage), title, *expected]


async def test_a_tool_call_keeps_the_title_in_place_when_the_answer_becomes_a_member() -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        panel.apply_turn_started(1)
        await _say(panel, "a1", "I can check the file.")
        await _settled(pilot)
        answer = panel.query_one(AgentMessage)
        rollback = panel.query_one(MessageRollbackButton)
        original = answer.styles.border_left[1]
        await pilot.hover(rollback)
        await _settled(pilot)
        highlighted = answer.styles.border_left[1]
        assert highlighted != original

        await _think(panel, "r1", "Check the implementation.")
        await _settled(pilot)
        row = _row(panel)
        thought = panel.query_one(ThinkingMessage)
        original_y = row.region.y
        assert row.region.bottom <= answer.region.y
        assert answer.styles.border_left[1] == highlighted

        await _read(panel, "c1", "implementation.py")
        await _settled(pilot)
        assert answer.styles.border_left[1] == highlighted
        await click_when_settled(pilot, row)
        await _settled(pilot)
        assert row.region.y == original_y
        group = panel.query_one(ToolGroup)
        assert [child for child in panel.displayed_children if child in (row, thought, answer, group)] == [
            row,
            answer,
            thought,
            group,
        ]
        assert row.region.y < answer.region.y < thought.region.y < group.region.y


async def test_a_sub_agent_s_own_activity_reaches_the_row() -> None:
    async with ChatPanelApp().run_test(size=(120, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await panel.add_tool_start("parent", "Explore", KIND_SUB_AGENT, args={"task": "inspect"})
        panel.link_sub_agent_invocation("parent", "invocation", "Explore Agent")
        await _status_shows(pilot, panel, "Running Explore Agent")
        panel.add_sub_agent_answer("Explore Agent", "invocation", "segment", "Let me inspect the notes.")
        await _status_shows(pilot, panel, "Let me inspect the notes.")
        panel.sub_agent_answer_withdrawn("invocation", ("segment",))
        await _status_shows(pilot, panel, "Running Explore Agent")
        await panel.add_sub_agent_tool_start(
            "Explore", "invocation", "read_file", {"path": "notes.txt"}, "inner", tool_kind=KIND_FILESYSTEM_READ
        )
        await _status_shows(pilot, panel, "Reading notes.txt")


async def test_new_reply_and_late_result_do_not_rescan_previous_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("inspect")
        await _read(panel, "first", "first.py", done=False)
        for index in range(16):
            await _say(panel, f"a{index}", "Checking another file.")
            await _read(panel, f"c{index}", f"{index}.py")
        await _settled(pilot)
        row = _row(panel)
        visits = 0

        class CountedMembers(list):
            def __iter__(self):
                nonlocal visits
                for member in super().__iter__():
                    visits += 1
                    yield member

        with monkeypatch.context() as patch:
            patch.setattr(row, "members", CountedMembers(row.members))
            await panel.add_answer_delta("final", "Here is the answer.")
            assert not row.summarized
            await panel.add_tool_result("first", "read_file", "Error: cannot read", canonical_status="failed")
            await _settled(pilot)
            assert row.summarized and row.tally.total == 17 and row.tally.failed == 1
        assert visits == 0


@pytest.mark.parametrize("read_still_running", [False, True], ids=["tools-finished", "last-tool-finishes-under-answer"])
async def test_the_row_summarizes_while_an_answer_streams_once_the_tools_finish(
    monkeypatch: pytest.MonkeyPatch,
    read_still_running: bool,
) -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py", done=not read_still_running)
        if not read_still_running:
            await _read(panel, "c2", "/tmp/b.py")
        await _settled(pilot)
        row = _row(panel)
        repaint = create_autospec(row.refresh, side_effect=row.refresh)
        monkeypatch.setattr(row, "refresh", repaint)
        if read_still_running:
            await panel.add_answer_delta("final", "Answering while the read runs.")
            await _settled(pilot)
            assert not row.summarized
            await panel.add_tool_result("c1", "read_file", "ok")
            await panel.add_answer_delta("final", " Still answering.")
            await wait_for(lambda: row.summarized, pilot=pilot, description="the idle row to summarize")
        else:
            await panel.add_answer_delta("final", " ")
            assert not row.summarized
            await panel.add_answer_delta("final", "Both files agree.")
        await _settled(pilot)
        expected = "▸ 1 tool call: 1 read" if read_still_running else "▸ 2 tool calls: 2 reads"
        assert _painted(row) == expected
        assert not row._work_running
        if read_still_running:
            return

        assert row.region.height == 1
        answer = panel.query(AgentMessage).last()
        assert answer.display and answer.text == "Both files agree."
        children = chat_content_children(panel)
        assert children.index(row) < children.index(answer)

        await panel.add_answer_delta("final", " Here is why.")
        assert _painted(row) == expected
        panel.finish_answer_segment("final", completed=True)
        await _read(panel, "c3", "/tmp/c.py", done=False)
        await _status_shows(pilot, panel, "Reading /tmp/c.py")
        await wait_for(lambda: row._work_running, pilot=pilot, description="the new tool resumes activity animation")
        assert row._work_running and row.latest is panel.query(ToolGroup).last()
        assert not answer.display and answer.collapsed
        assert repaint.call_count > 0
        assert all(not call.kwargs.get("layout", False) for call in repaint.call_args_list)


async def test_a_streaming_answer_joins_the_process_only_when_a_tool_call_follows_it() -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "Plan the change.")
        await panel.add_answer_delta("a1", "First I will look at the file.")
        await _settled(pilot)
        text = panel.query(AgentMessage).last()
        assert text.display, "the streaming text may be the answer: it stays in view"
        assert _painted(_row(panel)) == "▸ Activity"
        panel.finish_answer_segment("a1", completed=True)
        await _read(panel, "c1", "/tmp/a.py", done=False)
        await _status_shows(pilot, panel, "Reading /tmp/a.py")
        assert not text.display
        assert text.collapsed


def _label_style(message: AgentMessage) -> str:
    """The style of the reply header's arrow and label."""
    header = message._header_text()
    return str(header.spans[0].style) if header.spans else str(header.style)


async def test_live_text_starts_muted_then_uses_the_intermediate_or_final_style() -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        panel.agent_running = True
        await panel.add_answer_delta("a1", "First I will look at the file.")
        await _settled(pilot)
        text = panel.query(AgentMessage).last()
        header = text.query_one(_FoldRowHeader)
        preview = header.render()
        assert preview.plain.startswith("▸ Replying...")
        assert text.collapsed and not text.query(VirtualizedMarkdown)
        assert not text.query_one("MessageCopyButton").display
        panel.finish_answer_segment("a1", completed=True)
        await _read(panel, "c1", "/tmp/a.py")
        await click_when_settled(pilot, _row(panel))
        await _settled(pilot)
        assert panel.query_one(AgentMessage) is text
        assert text.has_class("--intermediate") and not text.has_class("--provisional")
        assert header.render().plain == "▸ First I will look at the file."
        assert header.render().spans != preview.spans
        await panel.add_answer_delta("final", "Done.")
        await _settled(pilot)
        pending = panel.query(AgentMessage).last()
        assert pending.query_one(_FoldRowHeader).render().plain.startswith("▸ Replying...")
        assert pending.styles.border_left[0] == "solid"
        panel.finish_answer_segment("final", completed=True)
        await panel.add_agent_message("Done.")
        await _settled(pilot)
        answer = panel.query(AgentMessage).last()
        assert not pending.is_attached
        assert answer.styles.border_left[0] == "thick"
        assert "green" in _label_style(answer)


async def test_a_new_intermediate_member_returns_the_summary_to_its_latest_text() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py")
        await panel.add_answer_delta("a1", "Checking another case.")
        await _settled(pilot)
        row = _row(panel)
        assert _painted(row) == "▸ 1 tool call: 1 read"

        panel.finish_answer_segment("a1", completed=True)
        await panel.add_answer_delta("a2", "The next part.")
        await _settled(pilot)
        assert _painted(row) == "▸ Checking another case."
        assert row.latest is panel.query(AgentMessage).first()
        assert not row.summarized and not row._work_running
        await panel.add_answer_delta("a2", " More.")
        await _settled(pilot)
        assert _painted(row) == "▸ Checking another case."


@pytest.mark.parametrize("locale, expected", [("en", "▸ Activity"), ("zh-Hans", "▸ 执行过程")])
async def test_thinking_only_summarizes_in_the_current_language_and_new_thinking_resumes_activity(
    locale: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    controller = LocaleController(Settings(locale=locale))
    app = LocalizedWidgetApp(
        lambda: ChatPanel(
            locale_controller=controller,
            thinking_expanded=_on,
        )
    )
    app.locale_controller = controller
    async with app.run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "Plan the change.")
        await _settled(pilot)
        row = _row(panel)
        repaint = create_autospec(row.refresh, side_effect=row.refresh)
        monkeypatch.setattr(row, "refresh", repaint)
        await panel.add_answer_delta("a1", "The answer.")
        await _settled(pilot)
        assert _painted(row) == expected and not row._work_running
        assert row.region.height == 1

        panel.finish_answer_segment("a1", completed=True)
        await _think(panel, "r2", "Another thought.", finish=False)
        await _settled(pilot)
        assert row.latest is panel.query(ThinkingMessage).last() and row._work_running
        assert _painted(row) != expected
        assert all(not call.kwargs.get("layout", False) for call in repaint.call_args_list)


@pytest.mark.parametrize("running", ["tool", "thinking", "earlier-tool"])
async def test_an_answer_does_not_summarize_a_segment_with_running_members(running: str) -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        if running == "thinking":
            await panel.add_answer_delta("a1", "Beginning the answer.")
            await _think(panel, "r1", "Still thinking.", finish=False)
        else:
            await _read(panel, "c1", "/tmp/a.py", done=False)
            if running == "earlier-tool":
                await _think(panel, "r1", "The later thought is done.")
        row = _row(panel)
        await panel.add_answer_delta("a1", "Answering while the work runs.")
        await _settled(pilot)
        assert not row.summarized
        assert "tool call" not in _painted(row) and _painted(row) != "▸ Activity"


async def test_reply_stream_keeps_an_open_row_and_sealed_segments_as_they_are() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py")
        first = _row(panel)
        await panel.add_user_message("also this", is_injection=True)
        await _read(panel, "c2", "/tmp/b.py")
        second = _rows(panel)[1]
        await click_when_settled(pilot, second)
        await _settled(pilot)
        before = (first.render().plain, second.render().plain)
        await panel.add_answer_delta("a1", "Done.")
        await _settled(pilot)
        assert (first.render().plain, second.render().plain) == before
        assert first.settled and not first.summarized
        assert not second.collapsed and not second.summarized


async def test_a_member_taken_away_leaves_the_one_before_it_and_the_last_one_takes_the_row() -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "First idea.")
        await _think(panel, "r2", "Second idea.")
        await _status_shows(pilot, panel, "Second idea.")
        await panel.abandon_thinking(("r2",))
        await _status_shows(pilot, panel, "First idea.")
        await panel.abandon_thinking(("r1",))
        await wait_for(lambda: not _rows(panel), pilot=pilot, description="a row with no members left goes")


async def test_a_long_status_is_cut_to_the_width_and_a_new_one_repaints_only_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp().run_test(size=(40, 16)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        reason = "Print every line of the configuration file in the project root"
        await panel.add_tool_start("c1", "zsh", KIND_SHELL, args={"command": "cat x", "reason": reason})
        await _status_shows(pilot, panel, "Print every")
        row = _row(panel)
        assert row.region.height == 2
        painted = _painted(row)
        assert painted.endswith("…")
        assert len(painted) <= row.size.width
        await _settled(pilot)

        layouts: list[bool] = []
        repaint = row.refresh

        def recording(*args: Any, **kwargs: Any) -> Any:
            layouts.append(bool(kwargs.get("layout", False)))
            return repaint(*args, **kwargs)

        monkeypatch.setattr(row, "refresh", recording)
        await panel.add_tool_start("c2", "zsh", KIND_SHELL, args={"command": "ls", "reason": "List the files"})
        await _status_shows(pilot, panel, "List the files")
        assert layouts and not any(layouts), "a new status repaints the row without laying out the transcript"
        assert row.region.height == 2


# ---------------------------------------------------------------------------
# §4.2 position and order
# ---------------------------------------------------------------------------


async def test_text_then_a_tool_call_puts_the_row_before_the_text_and_opens_in_finished_order() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _say(panel, "a1", "Let me check the file.")
        await _settled(pilot)
        assert not _rows(panel), "the text may still be the answer"
        await _read(panel, "c1", "/tmp/a.py", done=False)
        await _settled(pilot)
        row = _row(panel)
        text = panel.query(AgentMessage).last()
        group = panel.query_one(ToolGroup)
        children = chat_content_children(panel)
        assert [type(child) for child in children] == [UserMessage, type(row), AgentMessage, ToolGroup]
        assert not text.display and not group.display
        assert text.process_hidden and group.process_hidden
        assert row.collapsed and row.latest is group

        await panel.add_tool_result("c1", "read_file", "ok")
        await panel.add_answer_delta("final", "Done.")
        assert _painted(row) == "▸ 1 tool call: 1 read"

        await click_when_settled(pilot, row)
        await wait_for(lambda: not row.collapsed, pilot=pilot, description="the row opens")
        await _settled(pilot)
        assert text.display and group.display
        assert row.region.y < text.region.y < group.region.y
        assert "1 tool call" in _painted(row)

        panel.finish_answer_segment("final", completed=True)
        await panel.add_agent_message("Done.")
        await _settled(pilot)
        assert not _rows(panel)
        children = chat_content_children(panel)
        assert [type(child) for child in children] == [UserMessage, ProcessBlock, AgentMessage, ToolGroup, AgentMessage]
        assert panel.query_one(ProcessBlock).collapsed


async def test_dozens_of_calls_stay_one_row_and_open_row_by_row() -> None:
    async with ChatPanelApp().run_test(size=(100, 60)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        for step in range(10):
            await _say(panel, f"a{step}", f"Step {step}.")
            for call in range(3):
                await _read(panel, f"c{step}-{call}", f"/tmp/{step}-{call}.py")
        await _settled(pilot)
        row = _row(panel)
        assert row.region.height == 2
        assert not any(_shown(_members(panel)))
        await click_when_settled(pilot, row)
        await wait_for(lambda: not row.collapsed, pilot=pilot, description="the row opens")
        await _settled(pilot)
        assert _painted(row).startswith("▾ 30 tool calls: 30 reads")
        members = _members(panel)
        assert len(members) == 20
        assert all(_shown(members))
        assert all(member.collapsed for member in members)


async def test_hidden_sequential_calls_build_only_the_new_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    factory = create_autospec(tool_renderers.create_tool_widget, side_effect=tool_renderers.create_tool_widget)
    monkeypatch.setattr(tool_renderers, "create_tool_widget", factory)
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        count = 24
        for index in range(count):
            call_id = f"c{index}"
            await _read(panel, call_id, f"/tmp/{index}.py", done=False)
            group = panel.query_one(ToolGroup)
            assert set(group._tools) == {call_id}
            await panel.add_tool_result(call_id, "read_file", f"result {index}")
            await wait_for(lambda group=group: not group._tools, pilot=pilot, description="completed cards pruned")
        assert factory.call_count <= 2 * count
        group = panel.query_one(ToolGroup)
        assert group.process_hidden and group.all_complete
        assert not group._activity_header_finalized
        assert not group.query(tool_call.BaseToolCard)

        built = factory.call_count
        await _read(panel, "c0", "/tmp/new.py", done=False)
        assert group.all_complete and not group._tools
        await panel.add_tool_result("c0", "read_file", "later result")
        assert group.all_complete and not group._tools
        assert factory.call_count == built
        await _read(panel, "live", "/tmp/live.py", done=False)
        live = group.get_tool("live")
        assert live is not None
        await _read(panel, "live", "/tmp/updated.py", done=False)
        assert group.get_tool("live") is live

        group.collapsed = False
        await _settled(pilot)
        assert set(group._tools) == {"live"}, "expanding a still-hidden group does not restore its cards"
        await click_when_settled(pilot, _row(panel))
        await wait_for(lambda: group._content_mounted, pilot=pilot, description="missing cards restored")
        restored = list(group.query(tool_call.BaseToolCard))
        assert restored[-1] is live
        assert [card.call_id for card in restored[:-1]] == [f"c{index}" for index in range(count)]
        assert [card.result_text for card in restored[:-1]] == [f"result {index}" for index in range(count)]
        assert restored[0].args == {"path": "/tmp/0.py"}
        assert restored[0].status == "complete"
        assert factory.call_count <= 3 * count


@pytest.mark.parametrize("kind", [KIND_ASK_USER, KIND_SUB_AGENT], ids=["question", "sub-agent"])
async def test_a_waiting_call_keeps_its_card_when_revealing_pruned_siblings(kind: str) -> None:
    async with ChatPanelApp().run_test(size=(100, 40)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "old", "/tmp/old.py")
        group = panel.query_one(ToolGroup)
        await wait_for(lambda: not group._tools, pilot=pilot, description="earlier card pruned")

        if kind == KIND_ASK_USER:
            await panel.add_tool_start("current", "ask_user", kind, args={"question": "Pick?"})
        else:
            await panel.add_tool_start("current", "Explore", kind, args={"task": "inspect"})
        card = group.get_tool("current")
        assert card is not None and set(group._tools) == {"current"}
        if kind == KIND_ASK_USER:
            assert panel.show_ask_user_inline("current", "question", _questions())
            await _focused_options(pilot, panel)
        else:
            panel.link_sub_agent_invocation("current", "child", "Explore Agent")
            panel.sub_agent_paused("child", "acp_transport", "connection closed", 2)

        await wait_for(lambda: group._content_mounted, pilot=pilot, description="missing sibling restored")
        assert group.display and group.collapse_locked
        assert group.get_tool("current") is card
        assert [item.call_id for item in group.query(tool_call.BaseToolCard)] == ["old", "current"]


# ---------------------------------------------------------------------------
# §5 interaction: clicking the row
# ---------------------------------------------------------------------------


async def test_clicking_the_row_opens_its_members_folded_and_clicking_again_hides_them() -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "Look first.")
        await _say(panel, "a1", "Checking.")
        await _read(panel, "c1", "/tmp/a.py", done=False)
        await _settled(pilot)
        row = _row(panel)
        members = _members(panel)
        assert [type(member) for member in members] == [ThinkingMessage, AgentMessage, ToolGroup]

        await click_when_settled(pilot, row)
        await wait_for(lambda: not row.collapsed, pilot=pilot, description="the row opens")
        await _settled(pilot)
        assert all(_shown(members))
        assert all(member.collapsed for member in members), "opening the row keeps its members folded"
        assert _painted(row).startswith("▾ 1 tool call: 1 read")

        await click_when_settled(pilot, row)
        await wait_for(lambda: row.collapsed, pilot=pilot, description="the row closes")
        await _settled(pilot)
        assert not any(_shown(members))
        assert "Reading /tmp/a.py" in _status(row)
        assert not _painted(row).startswith("▾")
        assert not panel.is_anchor_released(), "opening and closing the running process keeps following (Q9)"


async def test_an_open_row_without_tool_calls_reads_activity() -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "Look first.")
        row = _row(panel)
        await click_when_settled(pilot, row)
        await wait_for(lambda: not row.collapsed, pilot=pilot, description="the row opens")
        await _settled(pilot)
        assert _painted(row) == "▾ Activity"


async def test_thinking_uses_its_open_default_when_it_arrives_in_open_activity() -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "Look first.")
        row = _row(panel)
        await click_when_settled(pilot, row)
        await wait_for(lambda: not row.collapsed, pilot=pilot, description="the row opens")
        await _think(panel, "r2", "Now the details.", finish=False)
        await _settled(pilot)
        first, second = _members(panel)
        assert first.collapsed and first.display
        assert second.display and not second.collapsed, "thinking shown open by default streams open"


async def test_opening_the_row_keeps_following_the_bottom() -> None:
    async with ChatPanelApp().run_test(size=(80, 22)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        for step in range(12):
            await _say(panel, f"a{step}", f"Step {step}.")
            await _read(panel, f"c{step}", f"/tmp/{step}.py")
        await panel.add_answer_delta("final", "Writing the answer.\n" * 4)
        await _settled(pilot)
        await wait_for(
            lambda: not panel.is_anchor_released() and panel.scroll_y == panel.max_scroll_y,
            pilot=pilot,
            description="the view follows the bottom before the click",
        )
        row = _row(panel)
        await click_when_settled(pilot, row)
        await wait_for(lambda: not row.collapsed, pilot=pilot, description="the row opens")
        await _settled(pilot)
        assert panel.max_scroll_y > 0, "the open process is taller than the view"
        assert not panel.is_anchor_released()
        await wait_for(
            lambda: panel.scroll_y == panel.max_scroll_y,
            pilot=pilot,
            description="the view stays at the bottom after the row opens",
        )
        # The answer streams on: the view still follows it.
        await panel.add_answer_delta("final", "More of the answer.\n" * 4)
        await _settled(pilot)
        await wait_for(lambda: panel.scroll_y == panel.max_scroll_y, pilot=pilot, description="still following")
        assert not panel.is_anchor_released()


async def test_fold_all_includes_the_live_row() -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "Look first.")
        await _settled(pilot)
        row = _row(panel)
        assert row.collapsed
        assert panel.toggle_fold_all() is False
        await _settled(pilot)
        assert not row.collapsed
        assert all(_shown(_members(panel)))
        assert panel.toggle_fold_all() is True
        await _settled(pilot)
        assert row.collapsed
        assert not any(_shown(_members(panel)))


# ---------------------------------------------------------------------------
# §4.4 / Q5 a tool group waiting for the user
# ---------------------------------------------------------------------------


async def _focused_options(pilot: Pilot[Any], panel: ChatPanel) -> AskUserOptions:
    await wait_for(
        lambda: bool(panel.query(AskUserOptions)) and panel.query_one(AskUserOptions).has_focus,
        pilot=pilot,
        description="the question takes focus",
    )
    return panel.query_one(AskUserOptions)


async def _turn_waiting_on_a_question(pilot: Pilot[Any], panel: ChatPanel) -> ToolGroup:
    await panel.add_user_message("prompt")
    await _read(panel, "c1", "/tmp/a.py")
    await _say(panel, "a1", "I need to ask.")
    await panel.add_tool_start("q", "ask_user", KIND_ASK_USER, args={"question": "Pick?"})
    await _settled(pilot)
    ask = panel.query(ToolGroup).last()
    assert not ask.display
    assert panel.show_ask_user_inline("q", "req-1", _questions())
    await wait_for(lambda: ask.display, pilot=pilot, description="the waiting group shows")
    return ask


async def test_a_waiting_question_shows_only_its_group_and_hides_it_again_once_answered() -> None:
    async with ChatPanelApp().run_test(size=(100, 40)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        ask = await _turn_waiting_on_a_question(pilot, panel)
        row = _row(panel)
        assert row.collapsed
        others = [member for member in _members(panel) if member is not ask]
        assert others and not any(_shown(others))
        children = chat_content_children(panel)
        assert children.index(row) < children.index(ask), "it shows where it is, below the row"
        options = await _focused_options(pilot, panel)
        await _settled(pilot)
        assert panel.region.contains_region(options.region), "the question is scrolled into view"

        assert panel.clear_ask_user_inline("q", "req-1")
        await wait_for(lambda: not ask.display, pilot=pilot, description="the answered group hides again")
        assert row.collapsed


async def test_closing_the_row_keeps_focus_in_a_question_that_stays_shown() -> None:
    async with ChatPanelApp().run_test(size=(100, 40)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        ask = await _turn_waiting_on_a_question(pilot, panel)
        options = await _focused_options(pilot, panel)
        row = _row(panel)
        await click_when_settled(pilot, row)
        await wait_for(lambda: not row.collapsed, pilot=pilot, description="the row opens")
        await click_when_settled(pilot, row)
        await wait_for(lambda: row.collapsed, pilot=pilot, description="the row closes")
        await _settled(pilot)
        assert ask.display
        assert options.has_focus


async def test_releasing_one_of_two_waiting_groups_keeps_the_other_shown() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py")
        await panel.add_user_message("also this", is_injection=True)
        await _read(panel, "c2", "/tmp/b.py")
        await _settled(pilot)
        first, second = panel.query(ToolGroup)
        assert len(_rows(panel)) == 2
        first.lock_collapse_for("ask-1")
        second.lock_collapse_for("ask-2")
        await wait_for(lambda: first.display and second.display, pilot=pilot, description="both waiting groups show")
        first.unlock_collapse_for("ask-1")
        await wait_for(lambda: not first.display, pilot=pilot, description="the released group hides")
        await _settled(pilot)
        assert second.display
        assert all(row.collapsed for row in _rows(panel))


async def test_a_paused_sub_agent_shows_its_group_until_it_resumes() -> None:
    async with ChatPanelApp().run_test(size=(120, 40)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py")
        await _say(panel, "a1", "Delegating.")
        await panel.add_tool_start("parent", "Explore", KIND_SUB_AGENT, args={"task": "inspect"})
        panel.link_sub_agent_invocation("parent", "invocation", "Explore Agent")
        await _settled(pilot)
        group = panel.query(ToolGroup).last()
        assert not group.display
        panel.sub_agent_paused("invocation", "acp_transport", "connection closed", 2)
        await wait_for(lambda: group.display, pilot=pilot, description="the paused agent's group shows")
        assert _row(panel).collapsed
        assert not panel.query(ToolGroup).first().display
        panel.sub_agent_resumed_after_pause("invocation")
        await wait_for(lambda: not group.display, pilot=pilot, description="the resumed agent's group hides again")


# ---------------------------------------------------------------------------
# §4.5 the end of the turn
# ---------------------------------------------------------------------------


_READ_KIND = {TOOL_CALL_KIND_METADATA_KEY: KIND_FILESYSTEM_READ}


def _history() -> list[dict[str, Any]]:
    """The same turn as :func:`_live`, as a saved session holds it."""
    return [
        Message("user", [Content.from_text("prompt")]).to_dict(),
        Message(
            "assistant",
            [
                Content.from_text_reasoning(text="Look."),
                Content.from_text("Checking."),
                Content.from_function_call(
                    call_id="c1", name="read_file", arguments="{}", additional_properties=_READ_KIND
                ),
            ],
        ).to_dict(),
        Message("tool", [Content.from_function_result(call_id="c1", result="ok")]).to_dict(),
        Message(
            "user", [Content.from_text("also this")], additional_properties={HistoryMarkerKind.INJECTED_KEY: True}
        ).to_dict(),
        Message(
            "assistant",
            [
                Content.from_text("One more."),
                Content.from_function_call(
                    call_id="c2", name="read_file", arguments="{}", additional_properties=_READ_KIND
                ),
            ],
        ).to_dict(),
        Message("tool", [Content.from_function_result(call_id="c2", result="ok")]).to_dict(),
        Message("assistant", [Content.from_text("Done.")]).to_dict(),
    ]


async def _live(panel: ChatPanel, *, before_end: Callable[[], None] | None = None) -> None:
    await panel.add_user_message("prompt")
    await panel.add_thinking_delta("r1", "reasoning", "Look.")
    panel.finish_thinking_segment("r1")
    for call_id, text in [("c1", "Checking."), ("c2", "One more.")]:
        if call_id == "c2":
            await panel.add_user_message("also this", is_injection=True)
        await panel.add_answer_delta(call_id, text)
        panel.finish_answer_segment(call_id, completed=True)
        await panel.add_tool_start(call_id, "read_file", "filesystem.read", args={})
        await panel.add_tool_result(call_id, "read_file", "ok")
        await panel.add_system("notice")
    if before_end is not None:
        before_end()
    await panel.add_answer_delta("final", "Done.")
    panel.finish_answer_segment("final", completed=True)
    await panel.add_agent_message("Done.")


async def test_success_ends_as_the_same_process_blocks_a_restored_session_shows() -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        seen_rows: list[int] = []
        await _live(panel, before_end=lambda: seen_rows.append(len(_rows(panel))))
        await _settled(pilot)
        assert seen_rows == [2], "each segment had its row while the turn ran"
        assert not _rows(panel)
        live = _summary(panel)
        assert live == [
            (0, 1, True, "▸ 1 tool call: 1 read"),
            (1, 1, True, "▸ 1 tool call: 1 read"),
        ]
        assert [message.text for message in panel.query(AgentMessage) if message.display] == ["Done."]
        assert all(member.collapsed for member in panel.query(ThinkingMessage))
        await panel.clear()
        await panel.replay_history(_history())
        await panel.wait_replay_complete()
        await _settled(pilot)
        assert not _rows(panel)
        assert _summary(panel) == live


async def test_the_turn_folds_at_its_end_even_after_the_user_opened_the_row() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _say(panel, "a1", "Checking.")
        await _read(panel, "c1", "/tmp/a.py")
        row = _row(panel)
        await click_when_settled(pilot, row)
        await wait_for(lambda: not row.collapsed, pilot=pilot, description="the row opens")
        await _finish(panel)
        await _settled(pilot)
        assert not _rows(panel)
        block = panel.query_one(ProcessBlock)
        assert block.collapsed
        assert not any(_shown(block.members))


async def test_a_lone_thinking_turn_ends_as_that_thinking_s_row_in_the_row_s_place() -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "Simple question.")
        await _settled(pilot)
        place = _row(panel).region.y
        await panel.add_agent_message("Yes.")
        await _settled(pilot)
        assert not _rows(panel)
        assert not panel.query(ProcessBlock)
        thinking = panel.query_one(ThinkingMessage)
        assert thinking.display and thinking.collapsed
        assert thinking.region.y == place


async def test_a_structured_output_success_shows_its_steps_row_by_row() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _say(panel, "a1", "Checking.")
        await _read(panel, "c1", "/tmp/a.py")
        await _settled(pilot)
        assert _rows(panel)
        await panel.add_agent_message("", structured_output_completed=True)
        await _settled(pilot)
        assert not _rows(panel)
        assert not panel.query(ProcessBlock)
        assert all(_shown(_members(panel)))


@pytest.mark.parametrize("ending", ["error", "interrupted"])
async def test_a_failed_or_interrupted_turn_shows_its_steps_row_by_row_still_folded(ending: str) -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "Look first.")
        await _say(panel, "a1", "Checking.")
        await _read(panel, "c1", "/tmp/a.py")
        await _settled(pilot)
        assert _rows(panel)
        if ending == "error":
            await panel.add_error("failed")
        else:
            await panel.add_interrupted()
        await _settled(pilot)
        assert not _rows(panel)
        assert not panel.query(ProcessBlock)
        members = _members(panel)
        assert len(members) == 3
        assert all(_shown(members))
        thinking, text, _group = members
        assert thinking.collapsed, "a step folded behind the row stays folded (Q4)"
        assert text.collapsed


@pytest.mark.parametrize("ending", ["error", "interrupted"])
async def test_thinking_ended_by_a_failure_shows_as_its_folded_row(ending: str) -> None:
    """The cost Q4 accepts: with thinking shown open by default, it was folded behind the row and stays folded."""
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "Look at the retry path.", finish=False)
        await _settled(pilot)
        assert _rows(panel)
        panel.finish_thinking_segment("r1")
        if ending == "error":
            await panel.add_error("failed")
        else:
            await panel.add_interrupted()
        await _settled(pilot)
        assert not _rows(panel)
        thinking = panel.query_one(ThinkingMessage)
        assert thinking.display and thinking.collapsed
        assert not thinking.query(VirtualizedMarkdown)
        # It opens on a click, as any folded thinking does.
        thinking.collapsed = False
        await wait_for(lambda: bool(thinking.query(VirtualizedMarkdown)), pilot=pilot, description="it opens")


async def test_a_new_message_after_a_failure_leaves_nothing_hidden() -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "Look first.")
        await _read(panel, "c1", "/tmp/a.py")
        await panel.add_error("failed")
        earlier = _members(panel)
        await panel.add_user_message("try again")
        await _read(panel, "c2", "/tmp/b.py", done=False)
        await _settled(pilot)
        assert all(_shown(earlier))
        row = _row(panel)
        assert row.collapsed
        children = chat_content_children(panel)
        assert children.index(row) > children.index(panel.query(UserMessage).last())


async def test_continuing_after_an_interrupt_starts_a_new_folded_row_and_success_folds_the_whole_turn() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py")
        row = _row(panel)
        await click_when_settled(pilot, row)
        await wait_for(lambda: not row.collapsed, pilot=pilot, description="the row opens")
        await panel.add_interrupted()
        await _settled(pilot)
        assert not _rows(panel)
        before = panel.query_one(ToolGroup)
        assert before.display

        # Continue: the same turn goes on, with no new user message; the run starts again.
        panel.hide_trailing_status_action()
        panel.agent_running = True
        await _read(panel, "c2", "/tmp/b.py", done=False)
        await _settled(pilot)
        again = _row(panel)
        assert again.collapsed, "every new row starts folded"
        assert before.display, "steps from before the interrupt are not this row's members"
        assert not panel.query(ToolGroup).last().display
        assert again.gap_above and again.region.height == 3
        assert [line.text.rstrip() for line in again.render_lines(Region(0, 0, again.size.width, 2))] == [
            "  │",
            "▸ Reading /tmp/b.py",
        ]

        await panel.add_tool_result("c2", "read_file", "ok")
        await _finish(panel)
        await _settled(pilot)
        assert not _rows(panel)
        (block,) = panel.query(ProcessBlock)
        assert block.collapsed
        assert all(group in block.members for group in panel.query(ToolGroup))
        assert not any(group.display for group in panel.query(ToolGroup))


async def test_continuing_a_recovered_turn_folds_only_new_steps_until_it_succeeds() -> None:
    history = [
        Message("user", [Content.from_text("prompt")]).to_dict(),
        Message(
            "assistant",
            [
                Content.from_function_call(
                    call_id="c1", name="read_file", arguments="{}", additional_properties=_READ_KIND
                )
            ],
        ).to_dict(),
        Message("tool", [Content.from_function_result(call_id="c1", result="ok")]).to_dict(),
    ]
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(history, recovered_from_sidecar=True)
        await panel.wait_replay_complete()
        recovered = panel.query_one(ToolGroup)
        panel.agent_running = True  # continue the recovered turn
        await _read(panel, "c2", "/tmp/b.py", done=False)
        await _settled(pilot)
        row = _row(panel)
        assert recovered.display
        children = chat_content_children(panel)
        assert children.index(recovered) < children.index(row)
        await panel.add_tool_result("c2", "read_file", "ok")
        await _finish(panel)
        await _settled(pilot)
        assert not _rows(panel)
        (block,) = panel.query(ProcessBlock)
        assert block.collapsed
        assert not any(group.display for group in panel.query(ToolGroup))


# ---------------------------------------------------------------------------
# §4.5 segment boundaries
# ---------------------------------------------------------------------------


async def test_a_message_sent_mid_turn_freezes_the_row_before_it_and_starts_a_new_one() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py")
        await _status_shows(pilot, panel, "Reading /tmp/a.py")
        first = _row(panel)
        await panel.add_user_message("also this", is_injection=True)
        await _read(panel, "c2", "/tmp/b.py", done=False)
        await _settled(pilot)
        rows = _rows(panel)
        assert len(rows) == 2 and rows[0] is first
        second = rows[1]
        assert second.collapsed
        assert not first.gap_above and not second.gap_above
        await wait_for(lambda: "Reading /tmp/b.py" in _status(second), pilot=pilot, description="the new row's status")
        assert "Reading /tmp/a.py" in _status(first), "the earlier row stays as it ended"
        injection = panel.query(UserMessage).last()
        children = chat_content_children(panel)
        assert children.index(first) < children.index(injection) < children.index(second)
        first_group, second_group = panel.query(ToolGroup)
        assert not first_group.display and not second_group.display

        await panel.add_tool_result("c2", "read_file", "ok")
        await _finish(panel)
        await _settled(pilot)
        assert not _rows(panel)
        assert [block.segment_index for block in panel.query(ProcessBlock)] == [0, 1]
        assert all(block.collapsed for block in panel.query(ProcessBlock))


async def test_open_sealed_activity_counts_a_failed_result_that_arrived_after_sealing() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("Prompt")
        await _read(panel, "c1", "original.py", done=False)
        row = _row(panel)
        await panel.add_user_message("Also check this.", is_injection=True)
        await _settled(pilot)
        preview = _painted(row)
        assert "Reading original.py" in preview

        await panel.add_tool_result("c1", "read_file", "Error: cannot read", canonical_status="failed")
        await _settled(pilot)
        assert _painted(row) == preview
        await click_when_settled(pilot, row)
        await _settled(pilot)
        assert "1 failed" in _painted(row)


async def test_sealed_sleep_keeps_its_remaining_time_when_both_rows_change_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    clock = ModuleType("time")
    monkeypatch.setattr(clock, "monotonic", lambda: now, raising=False)
    monkeypatch.setattr(tool_call, "time", clock)
    locale = LocaleController(Settings(locale="en"))
    app = LocalizedWidgetApp(
        lambda: ChatPanel(
            locale_controller=locale,
        )
    )
    app.locale_controller = locale
    async with app.run_test(size=(100, 30)) as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await panel.add_tool_start("sleep", "sleep", KIND_SLEEP, args={"seconds": 20, "reason": "CI"})
        await _status_shows(pilot, panel, "Sleeping: CI · 20s left")
        await panel.add_user_message("also this", is_injection=True)
        await _read(panel, "c2", "/tmp/b.py", done=False)
        await _settled(pilot)
        first, second = _rows(panel)
        assert "Sleeping: CI · 20s left" in _status(first)
        assert "Reading /tmp/b.py" in _status(second)

        now += 8.0
        locale.switch_locale("zh-Hans")
        await wait_for(
            lambda: (
                "休眠中" in _status(first)
                and "CI · 还剩 20 秒" in _status(first)
                and "正在读取 /tmp/b.py" in _status(second)
            ),
            pilot=pilot,
            description="both previews retranslated with the sealed countdown unchanged",
        )


async def test_a_compaction_mid_turn_starts_a_new_row_below_its_card() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py")
        await panel.add_compaction_start("compact-1")
        panel.complete_compaction("compact-1", outcome="ok", duration_ms=1000, last_words="## Progress")
        await _read(panel, "c2", "/tmp/b.py")
        await _settled(pilot)
        first, second = _rows(panel)
        card = panel.query_one(CompactionCard)
        assert card.display
        children = chat_content_children(panel)
        assert children.index(first) < children.index(card) < children.index(second)
        await _finish(panel)
        await _settled(pilot)
        assert not _rows(panel)
        assert [block.segment_index for block in panel.query(ProcessBlock)] == [0, 1]


async def test_text_written_before_a_mid_turn_message_stays_with_the_segment_before_it() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _read(panel, "c1", "/tmp/a.py")
        await _say(panel, "a1", "Half of an answer.")
        await panel.add_user_message("also this", is_injection=True)
        await _read(panel, "c2", "/tmp/b.py")
        await _settled(pilot)
        text = next(message for message in panel.query(AgentMessage) if message.text == "Half of an answer.")
        assert text.display
        assert all(text not in row.members for row in _rows(panel))
        await _finish(panel)
        await _settled(pilot)
        blocks = list(panel.query(ProcessBlock))
        assert [block.segment_index for block in blocks] == [0, 1]
        assert text in blocks[0].members and not text.display
        await click_when_settled(pilot, blocks[0])
        await wait_for(lambda: text.display, pilot=pilot, description="the half answer is visible in the first block")
        assert ("Agent", "Half of an answer.") in panel.get_agent_responses()


# ---------------------------------------------------------------------------
# §5 lifecycle
# ---------------------------------------------------------------------------


async def test_clear_mid_run_folds_new_tools_into_a_fresh_row() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.agent_running = True
        await panel.add_user_message("prompt")
        await _read(panel, "before-clear", "/tmp/before.py", done=False)
        old_row = _row(panel)

        await panel.clear()
        assert panel.agent_running and not _rows(panel)
        await _read(panel, "after-clear", "/tmp/after.py", done=False)
        await _status_shows(pilot, panel, "/tmp/after.py")
        row = _row(panel)
        group = panel.query_one(ToolGroup)
        assert row is not old_row and row.collapsed
        assert row.members == [group] and group.process_hidden and not group.display


async def test_clear_and_a_reload_mid_run_leave_no_row_and_the_next_turn_starts_fresh() -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _think(panel, "r1", "Look first.")
        await _read(panel, "c1", "/tmp/a.py", done=False)
        await _settled(pilot)
        assert _rows(panel)
        await panel.clear()
        await _settled(pilot)
        assert not _rows(panel)

        # A rollback reloads what is left of the session.
        await panel.replay_history(_history())
        await panel.wait_replay_complete()
        await _settled(pilot)
        assert not _rows(panel)

        await panel.add_user_message("again")
        await _read(panel, "c9", "/tmp/z.py", done=False)
        await _settled(pilot)
        row = _row(panel)
        assert row.collapsed
        assert not panel.query(ToolGroup).last().display
        await panel.add_tool_result("c9", "read_file", "ok")
        await _finish(panel)
        await _settled(pilot)
        assert not _rows(panel)


# ---------------------------------------------------------------------------
# §4.3 output that grows while hidden
# ---------------------------------------------------------------------------


async def _open(pilot: Pilot[Any], row: Any) -> None:
    await click_when_settled(pilot, row)
    await wait_for(lambda: not row.collapsed, pilot=pilot, description="the row opens")


async def _close(pilot: Pilot[Any], row: Any) -> None:
    await click_when_settled(pilot, row)
    await wait_for(lambda: row.collapsed, pilot=pilot, description="the row closes")


async def test_output_seen_then_hidden_grows_without_layout_and_shows_its_new_height(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp().run_test(size=(100, 40)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await panel.add_tool_start("s", "zsh", KIND_SHELL, args={"command": "make", "reason": "Build it"})
        panel.update_tool_progress("s", ["line one"])
        row = _row(panel)
        await _open(pilot, row)
        group = panel.query_one(ToolGroup)
        group.collapsed = False
        await _settled(pilot)
        output = panel.query_one("#exec-panel", Static)
        await wait_for(lambda: output.outer_size.height == 3, pilot=pilot, description="one line of output shows")

        await _close(pilot, row)
        await _settled(pilot)
        assert not group.display
        layouts: list[bool] = []
        repaint = output.refresh

        def recording(*args: Any, **kwargs: Any) -> Any:
            layouts.append(bool(kwargs.get("layout", False)))
            return repaint(*args, **kwargs)

        monkeypatch.setattr(output, "refresh", recording)
        panel.update_tool_progress("s", ["line two"])
        panel.update_tool_progress("s", ["line three"])
        await _settled(pilot)
        assert not any(layouts), "hidden output asked for a layout"

        await _open(pilot, row)
        if group.collapsed:
            group.collapsed = False
        await _settled(pilot)
        output = panel.query_one("#exec-panel", Static)
        assert output.outer_size.height == 5
        painted = "\n".join(strip.text for strip in pilot.app.screen._compositor.render_strips())
        assert "line three" in painted


async def test_output_of_a_closed_tool_group_grows_without_layout_and_shows_its_new_height(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp().run_test(size=(100, 40)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await panel.add_tool_start("s", "zsh", KIND_SHELL, args={"command": "make", "reason": "Build it"})
        panel.update_tool_progress("s", ["line one"])
        await _open(pilot, _row(panel))
        await _settled(pilot)
        group = panel.query_one(ToolGroup)
        group.collapsed = False
        output = panel.query_one("#exec-panel", Static)
        assert not group.collapsed
        await wait_for(lambda: output.outer_size.height == 3, pilot=pilot, description="one line of output shows")
        group.collapsed = True
        await _settled(pilot)

        layouts: list[bool] = []
        repaint = output.refresh

        def recording(*args: Any, **kwargs: Any) -> Any:
            layouts.append(bool(kwargs.get("layout", False)))
            return repaint(*args, **kwargs)

        monkeypatch.setattr(output, "refresh", recording)
        panel.update_tool_progress("s", ["line two"])
        panel.update_tool_progress("s", ["line three"])
        await _settled(pilot)
        assert not any(layouts), "hidden output asked for a layout"

        group.collapsed = False
        await _settled(pilot)
        assert output.outer_size.height == 5


# ---------------------------------------------------------------------------
# Q6 detail views
# ---------------------------------------------------------------------------


async def test_a_sub_agent_detail_view_shows_its_running_steps_row_by_row() -> None:
    journal = AgentTranscriptJournal()
    surface = AgentTranscriptSurface(journal)
    async with LocalizedWidgetApp(lambda: surface).run_test() as pilot:
        journal.record(TranscriptUserOp("Input"))
        journal.record(TranscriptAssistantOp("Looking.", segment_id="s1"))
        journal.record(TranscriptToolStartOp("c1", "read_file", KIND_FILESYSTEM_READ, {"path": "README.md"}))
        journal.record(TranscriptToolResultOp("c1", "read_file", "contents"))
        journal.record(TranscriptToolStartOp("c2", "read_file", KIND_FILESYSTEM_READ, {"path": "notes.md"}))
        await wait_for(lambda: len(surface.query(ToolGroup)) == 1, pilot=pilot, description="the steps show")
        await wait_for(lambda: screen_is_settled(pilot.app, pilot.app.screen), pilot=pilot, description="settled")
        assert not surface.query(LiveProcessRow)
        assert all(child.display for child in surface.direct_children())


async def test_summarized_activity_updates_when_authoritative_hosted_result_succeeds() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await panel.add_tool_start("c1", "web_search", "", provider_hosted=True, hosted_family="search")
        panel.update_tool_status("c1", "failed")
        await panel.add_answer_delta("a1", "The answer.")
        await _status_shows(pilot, panel, "1 failed")
        await panel.add_tool_result("c1", "web_search", "Found it.")
        await _settled(pilot)
        assert "1 failed" not in _status(_row(panel))
