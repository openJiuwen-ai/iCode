# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A turn's fold rows stand one blank row apart: a gap above a row that follows a folded one, its guide under the arrows."""

from __future__ import annotations

from itertools import pairwise

import pytest
from textual.geometry import Region
from textual.widget import Widget

from chrys.app.tui.widgets.chat.agent_transcript_surface import (
    AgentTranscriptJournal,
    AgentTranscriptSurface,
    TranscriptAssistantOp,
    TranscriptToolResultOp,
    TranscriptToolStartOp,
)
from chrys.app.tui.widgets.chat.fold_gap import FOLD_GAP_ROWS, FoldGap
from chrys.app.tui.widgets.chat.live_process_row import LiveProcessRow
from chrys.app.tui.widgets.chat.messages import AgentMessage, ThinkingMessage, UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.process_block import ProcessBlock
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.chat.waiting_row import WaitingRow
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_READ, KIND_SHELL
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import ChatPanelApp, LocalizedWidgetApp, click_when_settled
from tests.support.waiting import wait_for


async def _steps(panel: ChatPanel, steps: int) -> None:
    """Thinking, then a command, *steps* times: the turn moving on folds each."""
    for index in range(steps):
        await panel.add_thinking_delta(f"r{index}", "reasoning", f"Step {index}.")
        panel.finish_thinking_segment(f"r{index}")
        await panel.add_tool_start(f"s{index}", "zsh", KIND_SHELL, args={"command": "make"})
        await panel.add_tool_result(f"s{index}", "zsh", "ok")


def _rows(panel: ChatPanel) -> list[Widget]:
    """The fold rows shown, in order."""
    return [child for child in panel.displayed_children if isinstance(child, (ThinkingMessage, ToolGroup))]


def _top_row(widget: Widget) -> str:
    """The row *widget* draws at its top, under what it shows."""
    return widget.render_lines(Region(0, 0, widget.outer_size.width, 1))[0].text


def _header(row: Widget) -> Widget:
    return row.children[0]


async def test_rows_under_an_open_summary_stand_one_gap_apart_and_the_last_has_none_below_it() -> None:
    app = ChatPanelApp(
        thinking_expanded=lambda: True,
    )
    async with app.run_test(size=(90, 40)) as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _steps(panel, 3)
        await panel.add_answer_delta("a", "Done.")
        panel.finish_answer_segment("a", completed=True)
        await panel.add_agent_message("Done.")
        block = panel.query_one(ProcessBlock)
        block.action_toggle_process()
        await wait_for(
            lambda: len(_rows(panel)) == 6 and screen_is_settled(app, panel.screen),
            pilot=pilot,
            description="the summary opened to its rows",
        )
        rows = _rows(panel)
        assert all(isinstance(row, FoldGap) and row.ends_folded() for row in rows)
        # The first follows the summary row, each next one the row before it: a gap above each.
        assert rows[0].region.y == block.region.bottom
        for above, row in pairwise(rows):
            assert row.region.y == above.region.bottom
        for i, row in enumerate(rows):
            assert row.gap_above
            assert row.outer_size.height == FOLD_GAP_ROWS + 1
            assert _header(row).region.y == row.region.y + FOLD_GAP_ROWS
            # The summary's arrow lines up with the outer guide. A member's
            # arrow adds a short guide two columns in.
            assert _top_row(row)[:3] == ("│  " if i == 0 else "│ │")
            assert _header(row).region.x == row.region.x + 2
        # Nothing follows the last row but the margin before the answer: it does not end the transcript.
        answer = panel.query(AgentMessage).last()
        assert not rows[-1].gap_below
        assert answer.region.y == rows[-1].region.bottom + 1
        assert not answer.gap_above


async def test_an_open_row_s_own_blank_row_parts_it_from_the_row_below_until_it_folds_again() -> None:
    app = ChatPanelApp(
        thinking_expanded=lambda: True,
    )
    async with app.run_test(size=(90, 40)) as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _steps(panel, 2)
        await panel.add_agent_message("Done.")
        panel.query_one(ProcessBlock).action_toggle_process()
        await wait_for(lambda: len(_rows(panel)) == 4, pilot=pilot, description="the summary opened")
        thinking, group = _rows(panel)[2:]
        assert isinstance(thinking, ThinkingMessage)

        thinking.collapsed = False
        await wait_for(
            lambda: not group.gap_above and screen_is_settled(app, panel.screen),
            pilot=pilot,
            description="the row below the open thinking gave up its gap",
        )
        assert thinking.gap_above
        assert group.region.y == thinking.region.bottom
        assert group.outer_size.height == 1
        assert _header(group).region.y == group.region.y

        thinking.collapsed = True
        await wait_for(
            lambda: group.gap_above and screen_is_settled(app, panel.screen),
            pilot=pilot,
            description="the row below the folded thinking took its gap back",
        )
        assert group.region.y == thinking.region.bottom
        assert group.outer_size.height == FOLD_GAP_ROWS + 1


async def test_a_live_turn_s_first_row_follows_its_prompt_with_no_gap() -> None:
    async with ChatPanelApp(thinking_expanded=lambda: False).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await _steps(panel, 1)
        process = panel.query_one(LiveProcessRow)
        prompt = panel.query_one(UserMessage)
        assert not process.gap_above
        await click_when_settled(pilot, process)
        await wait_for(
            lambda: len(_rows(panel)) == 2 and screen_is_settled(pilot.app, panel.screen),
            pilot=pilot,
            description="the turn's rows laid out",
        )
        first, second = _rows(panel)
        assert first.gap_above
        assert process.region.y == prompt.region.bottom + prompt.styles.margin.bottom
        assert first.region.y == process.region.bottom
        assert second.gap_above


async def test_the_waiting_row_below_a_folded_row_keeps_its_gap_so_the_next_row_s_arrow_comes_in_at_its_dot() -> None:
    async with ChatPanelApp(thinking_expanded=lambda: False).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.agent_running = True
        await panel.add_user_message("prompt")
        await panel.add_thinking_delta("r", "reasoning", "Look at the file.")
        panel.finish_thinking_segment("r")
        await click_when_settled(pilot, panel.query_one(LiveProcessRow))
        row = panel.query_one(WaitingRow)
        await wait_for(
            lambda: row.display and row.gap_above and screen_is_settled(pilot.app, panel.screen),
            pilot=pilot,
            description="the turn waits below its folded thinking",
        )
        assert row.outer_size.height == FOLD_GAP_ROWS + 1
        assert row.render().plain.split("\n")[0] == "│"
        dot_y = row.region.y + FOLD_GAP_ROWS

        await panel.add_tool_start("c", "read_file", KIND_FILESYSTEM_READ, args={"path": "a.py"})
        group = panel.query_one(ToolGroup)
        await wait_for(
            lambda: not row.display and group.gap_above and screen_is_settled(pilot.app, panel.screen),
            pilot=pilot,
            description="the tool call took the waiting row's place",
        )
        assert _header(group).region.y == dot_y


def _painted_row(widget: Widget, y: int) -> str:
    """The row the screen paints at *y* across *widget*, children over it included."""
    strip = widget.screen._compositor.render_strips()[y]
    return strip.text[widget.region.x : widget.region.right].rstrip()


@pytest.mark.parametrize(
    ("kind", "follows"), [("thinking", "tool call"), ("thinking", "waiting row"), ("tool group", "thinking")]
)
async def test_a_folded_row_that_ends_the_chat_keeps_a_blank_row_below_it_until_a_row_follows(
    kind: str, follows: str
) -> None:
    """An open row ends with its own blank row; a folded row that ends the transcript keeps one below it as well.
    Only its own guide runs on through it: no fold row follows for the guide under its arrow to lead to."""
    app = ChatPanelApp(thinking_expanded=lambda: False)
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        panel.agent_running = True
        await panel.add_user_message("prompt")
        if kind == "thinking":
            await panel.add_thinking_delta("r", "reasoning", "Look at the file.")
            row: FoldGap = panel.query_one(ThinkingMessage)
        else:
            await panel.add_tool_start("c0", "read_file", KIND_FILESYSTEM_READ, args={"path": "a.py"})
            row = panel.query_one(ToolGroup)
        await click_when_settled(pilot, panel.query_one(LiveProcessRow))
        await wait_for(
            lambda: row.gap_below and screen_is_settled(app, panel.screen),
            pilot=pilot,
            description="the folded row laid out with a blank row below it",
        )
        assert row.ends_folded() and row.gap_above
        assert row.outer_size.height == 1 + 2 * FOLD_GAP_ROWS
        assert _header(row).region.y == row.region.y + FOLD_GAP_ROWS
        assert _painted_row(row, row.region.bottom - 1) == "│ │"

        if follows == "tool call":
            await panel.add_tool_start("c1", "read_file", KIND_FILESYSTEM_READ, args={"path": "b.py"})
            below: FoldGap = panel.query_one(ToolGroup)
        elif follows == "thinking":
            await panel.add_thinking_delta("r", "reasoning", "Look at the next file.")
            below = panel.query_one(ThinkingMessage)
        else:
            panel.finish_thinking_segment("r")
            below = panel.query_one(WaitingRow)
        await wait_for(
            lambda: below.display and below.gap_above and screen_is_settled(app, panel.screen),
            pilot=pilot,
            description=f"a {follows} below the folded row",
        )
        # The gap above the row that follows parts them; the one below the folded row went in the same layout.
        assert not row.gap_below
        assert row.outer_size.height == 1 + FOLD_GAP_ROWS
        assert below.region.y == row.region.bottom
        # A row that folds ends the transcript in its place, the waiting row as it is.
        assert below.gap_below is (follows != "waiting row")


async def test_an_open_row_that_ends_the_chat_shows_its_own_blank_row_and_no_gap_below_it() -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.agent_running = True
        await panel.add_user_message("prompt")
        await panel.add_thinking_delta("r", "reasoning", "Look at the file.")
        thinking = panel.query_one(ThinkingMessage)
        await click_when_settled(pilot, panel.query_one(LiveProcessRow))
        thinking.collapsed = False
        await wait_for(
            lambda: not thinking.ends_folded() and screen_is_settled(pilot.app, panel.screen),
            pilot=pilot,
            description="the open thinking laid out",
        )
        assert not thinking.gap_below
        # Its own guide, and its body's, run through its own blank row.
        assert _painted_row(thinking, thinking.region.bottom - 1) == "│ │"


async def test_text_below_a_folded_row_folds_with_its_gap_and_parts_the_tool_call_after_it_by_one() -> None:
    """The text settles and folds as the call starts: a folded row below a folded row, then the call's below it."""
    async with ChatPanelApp().run_test(size=(80, 40)) as pilot:
        app = pilot.app
        panel = app.query_one(ChatPanel)
        panel.agent_running = True
        await panel.add_user_message("prompt")
        await panel.add_tool_start("c1", "read_file", KIND_FILESYSTEM_READ, args={"path": "a.py"})
        await panel.add_tool_result("c1", "read_file", "ok")
        await click_when_settled(pilot, panel.query_one(LiveProcessRow))
        text = "\n\n".join(f"Line {index}." for index in range(5))
        await panel.add_answer_delta("a", text)
        panel.finish_answer_segment("a", completed=True)
        group = panel.query_one(ToolGroup)
        note = panel.query(AgentMessage).last()
        await wait_for(
            lambda: (
                group.collapsed
                and note.query_one(VirtualizedMarkdown).source == text
                and screen_is_settled(app, panel.screen)
            ),
            pilot=pilot,
            description="the note laid out below the folded group",
        )
        assert not note.gap_above

        await panel.add_tool_start("c2", "read_file", KIND_FILESYSTEM_READ, args={"path": "b.py"})
        following = panel.query(ToolGroup).last()
        await wait_for(
            lambda: following is not group and note.collapsed and screen_is_settled(app, panel.screen),
            pilot=pilot,
            description="the next tool call folded the note",
        )
        assert note.gap_above and following.gap_above
        assert note.outer_size.height == FOLD_GAP_ROWS + 1
        assert following.region.y == note.region.bottom
        assert _header(following).region.y == note.region.bottom + FOLD_GAP_ROWS


async def test_a_nested_transcript_parts_text_from_the_folded_tool_group_above_it() -> None:
    journal = AgentTranscriptJournal()
    surface = AgentTranscriptSurface(journal)
    async with LocalizedWidgetApp(lambda: surface).run_test() as pilot:
        journal.record(TranscriptToolStartOp("c1", "read_file", KIND_FILESYSTEM_READ, {"path": "a.py"}))
        journal.record(TranscriptToolResultOp("c1", "read_file", "ok"))
        journal.record(TranscriptAssistantOp("Next, the tests."))
        journal.record(TranscriptToolStartOp("c2", "read_file", KIND_FILESYSTEM_READ, {"path": "b.py"}))
        await wait_for(
            lambda: len(surface.query(ToolGroup)) == 2 and screen_is_settled(pilot.app, surface.screen),
            pilot=pilot,
            description="the text settled between the two tool groups",
        )
        above, below = surface.query(ToolGroup)
        text = surface.query_one(AgentMessage)
        assert above.collapsed and not above.gap_above
        assert text.gap_above
        assert text.region.y == above.region.bottom
        # Open text ends with its own blank row.
        assert not text.collapsed and not below.gap_above
        # Only the chat keeps a blank row below the folded row that ends it.
        assert below.ends_folded() and not below.gap_below
        assert below.outer_size.height == 1
