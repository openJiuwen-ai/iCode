# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for a ChatPanel transcript that fills its view: no frame painted shows a blank row under its last line, as
its entries fold, give rows up or grow, and the view that follows the bottom moves with them."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager

import pytest
from textual._arrange import DockArrangeResult
from textual.geometry import Region, Size
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Static

from chrys.app.tui.widgets.chat.compaction_card import CompactionCard, CompactionCardTitle
from chrys.app.tui.widgets.chat.fold_gap import FOLD_GAP_ROWS
from chrys.app.tui.widgets.chat.live_process_row import LiveProcessRow
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    MessageHeaderRow,
    StreamedMarkdownMessage,
    ThinkingMessage,
    UserMessage,
    _FoldRowHeader,
)
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolGroup, ToolGroupTitle
from chrys.app.tui.widgets.chat.waiting_row import WaitingRow
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.app.tui.widgets.markdown.blocks import MarkdownBlock
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_READ
from tests.app.tui.widgets._chat_scroll import scrolled_to_bottom
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import ChatPanelApp, click_when_settled
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for


def _shows(body: VirtualizedMarkdown, text: str) -> bool:
    """The markdown body was handed *text* and no parse is left to install it: its rows are the text's."""
    return body.source == text and body._pending_parse is None and not body.lock.is_locked


def _shows_preview(answer: AgentMessage, text: str) -> bool:
    return answer.text == text and answer.query_one(_FoldRowHeader).render().plain.startswith("▸ Replying...")


def _text_rows(answer: AgentMessage) -> int:
    """The rows of the answer's header and text, and the blank row below text between tool calls."""
    body = answer.query_one(VirtualizedMarkdown)
    return answer.query_one(MessageHeaderRow).outer_size.height + body.outer_size.height + body.styles.margin.bottom


def _blank_rows(cp: ChatPanel) -> int | None:
    """The rows the panel shows blank under the transcript's last line, as its screen last placed them; negative while
    the line is below the bottom border, as a view that grew has not followed it yet.

    Its last line is the last entry's, or the waiting row's below it. None while the screen placed that row nowhere it
    shows: Textual leaves a widget out of a layout that ran before it mounted, and paints its rows blank in their place
    until the next layout places it.
    """
    rows = [
        child
        for child in cp.children
        if child.display and (isinstance(child, WaitingRow) or not cp.is_infrastructure(child))
    ]
    placed = cp.screen._compositor.visible_widgets
    if not rows or rows[-1] not in placed:
        return None
    return cp.content_region.bottom - placed[rows[-1]][0].bottom


def _record_blank_rows(monkeypatch: pytest.MonkeyPatch, cp: ChatPanel) -> list[int]:
    """For each frame the screen paints while the transcript fills the view, but one whose last row Textual left out of
    the layout it paints: the blank rows under its last line."""
    rows: list[int] = []
    paint = Screen._compositor_refresh

    def record(screen: Screen) -> None:
        if screen is cp.screen and cp.max_scroll_y > 0 and (blank := _blank_rows(cp)) is not None:
            rows.append(blank)
        paint(screen)

    monkeypatch.setattr(Screen, "_compositor_refresh", record)
    return rows


def _ends_on_the_border(cp: ChatPanel) -> bool:
    """The transcript fills the view, which shows its end, with no blank row under its last line."""
    return cp.max_scroll_y > 0 and cp.scroll_y == cp.max_scroll_y and _blank_rows(cp) == 0


async def _following_a_tool_group(cp: ChatPanel, pilot: object, tools: int) -> ToolGroup:
    """Settle a view that follows the bottom below a running group of *tools* finished tool calls."""
    app = cp.app
    await scrolled_to_bottom(cp, pilot)
    cp.agent_running = True
    for index in range(tools):
        await cp.add_tool_start(
            f"c-{index}", "read_file", KIND_FILESYSTEM_READ, f"f{index}.py", {"path": f"f{index}.py"}
        )
        await cp.add_tool_result(f"c-{index}", "read_file", "line\n" * 3, 10)
    cp._toggle_live_process_row(cp.query_one(LiveProcessRow))
    cp.query(ToolGroup).last().collapsed = False
    await wait_for(
        lambda: cp.scroll_y == cp.max_scroll_y and screen_is_settled(app, cp.screen),
        pilot=pilot,
        description="the tool calls laid out at the bottom",
    )
    return cp.query(ToolGroup).last()


async def _ended_by(cp: ChatPanel, pilot: object, end: str) -> Widget:
    """Fill the view with a transcript that *end* ends; return the row whose last line ends it.

    A running turn waits after a tool group or compaction that ended: those end a turn that ended, which shows no
    waiting row below them.
    """
    if end == "tool group":
        group = await _following_a_tool_group(cp, pilot, tools=1)
        cp.agent_running = False
        return group
    await scrolled_to_bottom(cp, pilot)
    if end == "answer":
        return cp.mounted_agent_messages()[-1]
    if end == "user message":
        await cp.add_user_message("Another question")
        return cp.query(UserMessage).last()
    if end == "compaction":
        await cp.add_compaction_start("k1")
        cp.complete_compaction("k1", outcome="ok", last_words="A note.")
        return cp.query_one(CompactionCard)
    cp.agent_running = True
    await cp.add_thinking_delta("r1", "reasoning", "Think one.\n\nThink two.")
    cp._toggle_live_process_row(cp.query_one(LiveProcessRow))
    cp.query_one(ThinkingMessage).collapsed = False
    if end == "thinking":
        # Still streaming: the turn moves.
        return cp.query_one(ThinkingMessage)
    cp.finish_thinking_segment("r1")
    # Thinking that ended waits for what comes next: the row shows after a moment.
    return cp.query_one(WaitingRow)


@pytest.mark.parametrize("end", ["answer", "user message", "thinking", "tool group", "compaction", "waiting row"])
async def test_a_transcript_that_fills_the_view_shows_its_last_line_on_the_bottom_border(end: str) -> None:
    """No blank row stands under the last line, whatever ends the transcript: an entry's bottom margin parts it from
    the entry after it, and the last has none."""
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test(size=(80, 24)) as pilot:
        app = pilot.app
        cp = app.query_one(ChatPanel)
        last = await _ended_by(cp, pilot, end)
        await wait_for(
            lambda: last.display and screen_is_settled(app, cp.screen) and cp.scroll_y == cp.max_scroll_y,
            pilot=pilot,
            description="the transcript laid out at the bottom",
        )
        assert _ends_on_the_border(cp)
        assert last.region.bottom == cp.content_region.bottom


async def test_an_answer_keeps_its_blank_row_below_once_an_entry_follows_it() -> None:
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        app = pilot.app
        cp = app.query_one(ChatPanel)
        await scrolled_to_bottom(cp, pilot)
        answer = cp.mounted_agent_messages()[-1]
        assert answer.region.bottom == cp.content_region.bottom

        await cp.add_user_message("Another question")
        message = cp.query(UserMessage).last()
        await wait_for(
            lambda: screen_is_settled(app, cp.screen) and _ends_on_the_border(cp),
            pilot=pilot,
            description="the message laid out at the bottom",
        )
        assert message.region.y - answer.region.bottom == 1


def _grown_to(cp: ChatPanel, entry: Static, lines: int) -> Callable[[], bool]:
    return lambda: (
        entry.region.height == lines and screen_is_settled(cp.app, cp.screen) and cp.scroll_y == cp.max_scroll_y
    )


async def test_a_transcript_scrolls_only_once_its_lines_pass_the_bottom_border() -> None:
    """Shorter than the view, or with its last line on the bottom border, the transcript fits the view; past that, the
    view scrolls by the rows its lines take, its last entry's margin not one."""
    async with ChatPanelApp().run_test(size=(80, 20)) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("hello")
        entry = Static()
        entry.styles.margin = (0, 0, 1, 0)
        await cp.mount(entry)
        rows_past_the_border: list[int] = []

        for lines in range(8, 26):
            entry.update("\n".join(f"line {index}" for index in range(lines)))
            await wait_for(
                _grown_to(cp, entry, lines), pilot=pilot, description=f"{lines} lines laid out at the bottom"
            )
            rows_past = entry.virtual_region.bottom - cp.scrollable_content_region.height
            rows_past_the_border.append(rows_past)
            assert cp.max_scroll_y == max(0, rows_past), lines

        assert min(rows_past_the_border) < 0 < max(rows_past_the_border)
        assert 0 in rows_past_the_border


async def test_a_streamed_answer_gives_back_at_once_the_row_its_closing_markup_frees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Until its ``**`` closes, a line wraps that then fits again: the row goes in the frame that fits it."""
    async with ChatPanelApp().run_test(size=(80, 20)) as pilot:
        app = pilot.app
        cp = app.query_one(ChatPanel)
        await scrolled_to_bottom(cp, pilot)
        cp.agent_running = True
        answer = AgentMessage("start", is_final=False)
        await cp.mount_transcript_widget(answer)
        body = answer.query_one(VirtualizedMarkdown)
        await wait_for(
            lambda: _shows(body, "start") and screen_is_settled(app, cp.screen),
            pilot=pilot,
            description="the answer started",
        )
        # One word short of the width: the unclosed ``**b`` wraps, the closed bold ``b`` fits.
        text = "x" * (body.content_size.width - 3) + " **b"
        answer.stream_update(text)
        await wait_for(
            lambda: body.virtual_size.height == 2 and screen_is_settled(app, cp.screen) and _ends_on_the_border(cp),
            pilot=pilot,
            description="the unclosed markup wrapped",
        )
        height = answer.outer_size.height
        blank_rows = _record_blank_rows(monkeypatch, cp)

        answer.stream_update(text + "**")
        await wait_for(
            lambda: body.virtual_size.height == 1 and screen_is_settled(app, cp.screen),
            pilot=pilot,
            description="the closed markup fits one row",
        )
        assert answer.outer_size.height == height - 1
        assert _ends_on_the_border(cp)
        assert blank_rows
        assert max(blank_rows) == 0, blank_rows


@asynccontextmanager
async def _laid_out_before(
    entry: Widget, monkeypatch: pytest.MonkeyPatch, shown: Callable[[], bool]
) -> AsyncIterator[list[int]]:
    """Hold *entry* and every widget in it until its screen lays out what *shown* says the block showed: none of them
    runs what the block asked of it before that layout, as on a machine loaded enough that a layout another widget
    asked for runs first. Yields the height each such layout gave *entry*, while held."""
    screen = entry.screen
    release = asyncio.Event()
    heights: list[int] = []
    refresh_layout = Screen._refresh_layout

    def spy(self: Screen, size: Size | None = None, scroll: bool = False) -> None:
        refresh_layout(self, size, scroll)
        if self is screen and not release.is_set() and shown():
            heights.append(entry.outer_size.height)

    monkeypatch.setattr(Screen, "_refresh_layout", spy)
    parts = list(entry.walk_children(with_self=True))
    held: list[Widget] = []

    async def hold(part: Widget) -> None:
        held.append(part)
        await release.wait()

    for part in parts:
        part.call_next(hold, part)
    try:
        # No pilot: it would wait for the held widgets to run what they were asked.
        await wait_for(lambda: len(held) == len(parts), description="every widget in the entry is held")
        yield heights
        await wait_for(lambda: bool(heights), description="the screen laid out what the block showed")
    finally:
        release.set()


def _record_paints(
    monkeypatch: pytest.MonkeyPatch, cp: ChatPanel, folded: Widget, row: Widget
) -> list[tuple[bool, Region | None, bool]]:
    """For each frame the screen paints: whether *folded* was folded when the transcript was laid out as it paints, the
    screen region it paints *row* in, and whether the view shows the end of the transcript.

    A widget's repaint can reach the screen a frame before the layout another widget asked for: that frame paints the
    transcript as it was laid out before, so it is judged by the fold it was laid out with, not the one asked for.
    """
    paints: list[tuple[bool, Region | None, bool]] = []
    laid_out: dict[str, object] = {"arrangement": None, "collapsed": folded.collapsed}
    paint = Screen._compositor_refresh
    arrange = ChatPanel.arrange

    def arranged(panel: ChatPanel, size: Size, optimal: bool = False) -> DockArrangeResult:
        result = arrange(panel, size, optimal=optimal)
        # A cached arrangement comes back as the same result: only a new one reads the fold as it is now.
        if panel is cp and not optimal and result is not laid_out["arrangement"]:
            laid_out.update(arrangement=result, collapsed=folded.collapsed)
        return result

    def record(screen: Screen) -> None:
        painted = screen._compositor.visible_widgets.get(row)
        at_end = cp.scroll_y == cp.max_scroll_y
        paints.append((laid_out["collapsed"] is True, None if painted is None else painted[0], at_end))
        paint(screen)

    monkeypatch.setattr(ChatPanel, "arrange", arranged)
    monkeypatch.setattr(Screen, "_compositor_refresh", record)
    return paints


async def test_an_answer_shows_its_tool_group_folded_above_it_in_the_frame_that_shows_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answer's start folds the tool group above it: the rows the group gives up go in that frame, and the view
    comes up to the transcript's new end, the answer's last line on the bottom border.

    Measured open in that frame, the group stood blank under its title, and the content moved in the next.
    """
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        app = pilot.app
        cp = app.query_one(ChatPanel)
        group = await _following_a_tool_group(cp, pilot, tools=1)
        bottom = cp.virtual_size.height
        blank_rows = _record_blank_rows(monkeypatch, cp)

        # The group folds as the answer starts: the layout that shows the answer must measure it folded, though
        # nothing in it has run since.
        async with _laid_out_before(
            group, monkeypatch, lambda: group.collapsed and bool(cp.mounted_agent_messages())
        ) as heights:
            await cp.add_answer_delta("segment-1", "The answer starts")
        answer = cp.mounted_agent_messages()[-1]
        await wait_for(
            lambda: (
                group.collapsed and _shows_preview(answer, "The answer starts") and screen_is_settled(app, cp.screen)
            ),
            pilot=pilot,
            description="the answer started below the folded group",
        )
        assert set(heights) == {group.outer_size.height}, heights
        assert cp.virtual_size.height < bottom
        assert _ends_on_the_border(cp)
        assert answer.region.bottom == cp.content_region.bottom
        assert blank_rows
        assert max(blank_rows) == 0, blank_rows


async def _foldable_entry(
    cp: ChatPanel, pilot: object, entry: str
) -> tuple[ToolGroup | StreamedMarkdownMessage | CompactionCard, Widget]:
    """Settle a view that follows the bottom of a running turn at *entry*; return it and the row a click folds it by."""
    if entry == "tool group":
        group = await _following_a_tool_group(cp, pilot, tools=1)
        return group, group.query_one(ToolGroupTitle)
    await scrolled_to_bottom(cp, pilot)
    cp.agent_running = True
    if entry == "thinking":
        await cp.add_thinking_delta("r1", "reasoning", "Think one.\n\nThink two.\n\nThink three.")
        cp.finish_thinking_segment("r1")
        cp._toggle_live_process_row(cp.query_one(LiveProcessRow))
        message: StreamedMarkdownMessage = cp.query_one(ThinkingMessage)
        return message, message.query_one(_FoldRowHeader)
    if entry == "text between tool calls":
        await cp.add_answer_delta("a1", "Let me look.\n\nAt both files.")
        cp.finish_answer_segment("a1", completed=True)
        await cp.add_tool_start("c1", "read_file", KIND_FILESYSTEM_READ, "a.py", {"path": "a.py"})
        cp._toggle_live_process_row(cp.query_one(LiveProcessRow))
        message = cp.mounted_agent_messages()[-1]
        return message, message.query_one(_FoldRowHeader)
    await cp.add_compaction_start("k1")
    cp.complete_compaction("k1", outcome="ok", last_words="A note.\n\nIt goes on.")
    card = cp.query_one(CompactionCard)
    return card, card.query_one(CompactionCardTitle)


@pytest.mark.parametrize(
    ("entry", "how"),
    [
        ("tool group", "click"),
        ("tool group", "fold all"),
        ("tool group", "click once the turn ended"),
        ("thinking", "click"),
        ("text between tool calls", "click"),
        ("compaction", "click"),
    ],
)
async def test_an_entry_the_user_folds_gives_its_rows_up_at_the_bottom_in_the_layout_that_folds_it(
    monkeypatch: pytest.MonkeyPatch, entry: str, how: str
) -> None:
    """A view that follows the bottom shows no blank row under the transcript's end in any frame painted with the fold,
    the content below the fold coming up in none of them."""
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test(size=(80, 24)) as pilot:
        app = pilot.app
        cp = app.query_one(ChatPanel)
        folded, title = await _foldable_entry(cp, pilot, entry)
        if how == "fold all":
            folded = cp.query_one(LiveProcessRow)
            title = folded
        if how == "click once the turn ended":
            cp.agent_running = False
        # Thinking and a compaction that ended wait for what comes next: the waiting row shows after a moment.
        waits = cp.agent_running and entry in {"thinking", "compaction"}

        def at_bottom(collapsed: bool) -> bool:
            return (
                folded.collapsed is collapsed
                and cp.query_one(WaitingRow).display is waits
                and all(
                    body._pending_parse is None and not body.lock.is_locked
                    for body in folded.query(VirtualizedMarkdown)
                )
                and cp.scroll_y == cp.max_scroll_y
                and screen_is_settled(app, cp.screen)
            )

        if folded.collapsed:
            # The turn folded it: the user opens it first, once the row has come, or the view that follows it down
            # would move the title away from a click already aimed.
            if waits:
                await wait_for(
                    lambda: cp.query_one(WaitingRow).display and screen_is_settled(app, cp.screen),
                    pilot=pilot,
                    description="the waiting row shows under the folded entry",
                )
            await click_when_settled(pilot, title)
        await wait_for(lambda: at_bottom(False), pilot=pilot, description="the entry shows open at the bottom")
        bottom = cp.virtual_size.height
        paints = _record_paints(monkeypatch, cp, folded, title)
        blank_rows = _record_blank_rows(monkeypatch, cp)

        if how == "fold all":
            assert cp.toggle_fold_all()
        else:
            await click_when_settled(pilot, title)
        await wait_for(lambda: at_bottom(True), pilot=pilot, description="the user folded the entry")
        assert cp.virtual_size.height < bottom
        assert _ends_on_the_border(cp)
        folded_paints = [(row, at_end) for collapsed, row, at_end in paints if collapsed]
        assert folded_paints
        assert set(folded_paints) == {(title.region, True)}, paints
        assert blank_rows
        assert max(blank_rows) == 0, blank_rows


async def test_text_and_the_tool_call_after_it_fold_as_what_follows_them_starts_and_the_view_shows_the_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text a tool call follows folds to its header row as the call starts, and the call's group folds as the text
    after it starts: no frame shows a blank row under the transcript's last line."""
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        app = pilot.app
        cp = app.query_one(ChatPanel)
        group = await _following_a_tool_group(cp, pilot, tools=1)
        await cp.add_answer_delta("segment-1", "A short note")
        note = cp.mounted_agent_messages()[-1]
        await wait_for(
            lambda: group.collapsed and _shows_preview(note, "A short note") and screen_is_settled(app, cp.screen),
            pilot=pilot,
            description="the note started below the folded group",
        )
        cp.finish_answer_segment("segment-1", completed=True)
        blank_rows = _record_blank_rows(monkeypatch, cp)

        await cp.add_tool_start("c-next", "read_file", KIND_FILESYSTEM_READ, "next.py", {"path": "next.py"})
        following = cp.query(ToolGroup).last()
        await wait_for(
            lambda: (
                following is not group
                and note.has_class("--intermediate")
                and note.collapsed
                and screen_is_settled(app, cp.screen)
            ),
            pilot=pilot,
            description="the next tool call folded the note",
        )
        # Its header, and the gap that parts it from the folded group above it.
        assert note.gap_above
        assert note.outer_size.height == 1 + FOLD_GAP_ROWS
        assert _ends_on_the_border(cp)

        await cp.add_tool_result("c-next", "read_file", "line\n" * 3, 10)
        await cp.add_answer_delta("segment-2", "The answer")
        answer = cp.mounted_agent_messages()[-1]
        await wait_for(
            lambda: following.collapsed and _shows_preview(answer, "The answer") and screen_is_settled(app, cp.screen),
            pilot=pilot,
            description="the answer started below the folded group",
        )
        assert _ends_on_the_border(cp)
        assert blank_rows
        assert max(blank_rows) == 0, blank_rows


@pytest.mark.parametrize("thinking_expanded", [False, True], ids=["folded-thinking", "open-thinking"])
async def test_a_turn_with_its_tool_groups_folded_shows_its_end_on_the_bottom_border(
    monkeypatch: pytest.MonkeyPatch, thinking_expanded: bool
) -> None:
    """Long text, a tool call that folds it, its result, then thinking that ends the call's group: as the display
    settings have it by default, no frame shows a blank row under the transcript's last line."""
    text = "\n\n".join(f"Paragraph {index} of the text between calls." for index in range(8))
    async with ChatPanelApp(thinking_expanded=lambda: thinking_expanded).run_test(size=(100, 40)) as pilot:
        app = pilot.app
        cp = app.query_one(ChatPanel)
        await scrolled_to_bottom(cp, pilot)
        await cp.add_user_message("prompt")
        cp.agent_running = True
        await cp.add_answer_delta("segment-1", text)
        cp.finish_answer_segment("segment-1", completed=True)
        note = cp.mounted_agent_messages()[-1]
        await wait_for(
            lambda: (
                note.query_one(_FoldRowHeader).render().plain.startswith("▸ Replying...")
                and cp.scroll_y == cp.max_scroll_y
                and screen_is_settled(app, cp.screen)
            ),
            pilot=pilot,
            description="the text laid out at the bottom",
        )
        blank_rows = _record_blank_rows(monkeypatch, cp)

        await cp.add_tool_start("c1", "read_file", KIND_FILESYSTEM_READ, "a.py", {"path": "a.py"})
        cp._toggle_live_process_row(cp.query_one(LiveProcessRow))
        await cp.add_tool_result("c1", "read_file", "ok", 5)
        await cp.add_thinking_delta("r1", "reasoning", "Think one.\n\nThink two.")
        cp.finish_thinking_segment("r1")
        group = cp.query_one(ToolGroup)
        await wait_for(
            lambda: note.collapsed and cp.query(ThinkingMessage) and group.region and screen_is_settled(app, cp.screen),
            pilot=pilot,
            description="the thinking started below the folded text and tool call",
        )
        assert _ends_on_the_border(cp)
        assert blank_rows
        assert max(blank_rows) == 0, blank_rows


async def test_text_that_starts_while_the_turn_waits_shows_the_thinking_folded_and_the_waiting_row_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answer's start folds the thinking above it and hides the waiting row below it: both go in the frame that
    shows the answer, its last line on the bottom border."""
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test(size=(80, 24)) as pilot:
        app = pilot.app
        cp = app.query_one(ChatPanel)
        await scrolled_to_bottom(cp, pilot)
        cp.agent_running = True
        await cp.add_thinking_delta("r1", "reasoning", "Line one.\n\nLine two.")
        cp._toggle_live_process_row(cp.query_one(LiveProcessRow))
        cp.query_one(ThinkingMessage).collapsed = False
        cp.finish_thinking_segment("r1")
        thinking = cp.query_one(ThinkingMessage)
        row = cp.query_one(WaitingRow)
        await wait_for(
            lambda: (
                row.display
                and _shows(thinking.query_one(VirtualizedMarkdown), "Line one.\n\nLine two.")
                and cp.scroll_y == cp.max_scroll_y
                and screen_is_settled(app, cp.screen)
            ),
            pilot=pilot,
            description="the turn waits below its complete thinking",
        )
        blank_rows = _record_blank_rows(monkeypatch, cp)

        # The thinking folds as the answer starts: the layout that shows the answer must measure it folded, though
        # nothing in it has run since.
        async with _laid_out_before(
            thinking, monkeypatch, lambda: thinking.collapsed and bool(cp.mounted_agent_messages())
        ) as heights:
            await cp.add_answer_delta("segment-1", "The answer")
        answer = cp.mounted_agent_messages()[-1]
        await wait_for(
            lambda: (
                thinking.collapsed
                and not row.display
                and _shows_preview(answer, "The answer")
                and screen_is_settled(app, cp.screen)
            ),
            pilot=pilot,
            description="the answer started below the folded thinking",
        )
        assert set(heights) == {thinking.outer_size.height}, heights
        assert _ends_on_the_border(cp)
        assert blank_rows
        assert max(blank_rows) == 0, blank_rows


async def test_a_tool_call_that_starts_while_the_turn_waits_shows_the_thinking_folded_and_the_row_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its group mounts hidden until its first call shows: the waiting row stays until the group starts and folds the
    thinking above, and the frame that shows the group shows both gone."""
    text = "\n\n".join(f"Line {index}." for index in range(5))
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test(size=(80, 40)) as pilot:
        app = pilot.app
        cp = app.query_one(ChatPanel)
        await scrolled_to_bottom(cp, pilot)
        cp.agent_running = True
        await cp.add_thinking_delta("r1", "reasoning", text)
        cp._toggle_live_process_row(cp.query_one(LiveProcessRow))
        cp.query_one(ThinkingMessage).collapsed = False
        cp.finish_thinking_segment("r1")
        thinking = cp.query_one(ThinkingMessage)
        row = cp.query_one(WaitingRow)
        await wait_for(
            lambda: (
                row.display
                and _shows(thinking.query_one(VirtualizedMarkdown), text)
                and cp.scroll_y == cp.max_scroll_y
                and screen_is_settled(app, cp.screen)
            ),
            pilot=pilot,
            description="the turn waits below its complete thinking",
        )
        blank_rows = _record_blank_rows(monkeypatch, cp)

        # The thinking folds as the group starts: the layout that shows the group must measure it folded, though
        # nothing in it has run since.
        async with _laid_out_before(
            thinking, monkeypatch, lambda: thinking.collapsed and any(group.display for group in cp.query(ToolGroup))
        ) as heights:
            await cp.add_tool_start("c-next", "read_file", KIND_FILESYSTEM_READ, "next.py", {"path": "next.py"})
        group = cp.query_one(ToolGroup)
        await wait_for(
            lambda: thinking.collapsed and not row.display and group.display and screen_is_settled(app, cp.screen),
            pilot=pilot,
            description="the tool call started below the folded thinking",
        )
        assert set(heights) == {thinking.outer_size.height}, heights
        assert _ends_on_the_border(cp)
        assert blank_rows
        assert max(blank_rows) == 0, blank_rows


async def _start(cp: ChatPanel, follows: str) -> Callable[[], bool]:
    """Start more text, or a tool call, as *follows* says; whether it shows."""
    if follows == "text":
        await cp.add_answer_delta("segment-2", "The answer")
        answer = cp.mounted_agent_messages()[-1]
        return lambda: _shows_preview(answer, "The answer")
    await cp.add_tool_start("c-next", "read_file", KIND_FILESYSTEM_READ, "next.py", {"path": "next.py"})
    group = cp.query_one(ToolGroup)
    return lambda: group.display


@pytest.mark.parametrize("thinking_expanded", [False, True], ids=["folded-thinking", "open-thinking"])
@pytest.mark.parametrize("follows", ["tool-call", "text"])
async def test_text_stays_above_later_thinking_and_keeps_the_end_on_the_bottom_border(
    monkeypatch: pytest.MonkeyPatch, thinking_expanded: bool, follows: str
) -> None:
    """When the preview above later thinking joins the process, its row stays in place and fits the viewport."""
    async with ChatPanelApp(thinking_expanded=lambda: thinking_expanded).run_test(size=(80, 40)) as pilot:
        app = pilot.app
        cp = app.query_one(ChatPanel)
        await scrolled_to_bottom(cp, pilot)
        cp.agent_running = True
        await cp.add_answer_delta("segment-1", "A note\n\nthat goes on\n\nand on")
        cp.finish_answer_segment("segment-1", completed=True)
        note = cp.mounted_agent_messages()[-1]
        await cp.add_thinking_delta("r1", "reasoning", "Think one.\n\nThink two.")
        cp.finish_thinking_segment("r1")
        thinking = cp.query_one(ThinkingMessage)
        cp._toggle_live_process_row(cp.query_one(LiveProcessRow))
        thinking.collapsed = not thinking_expanded
        await wait_for(
            lambda: cp.scroll_y == cp.max_scroll_y and screen_is_settled(app, cp.screen),
            pilot=pilot,
            description="the thinking laid out below the note",
        )
        title = cp.query_one(LiveProcessRow)
        assert title.region.y < note.region.y < thinking.region.y
        assert thinking.gap_above_from is note
        blank_rows = _record_blank_rows(monkeypatch, cp)

        started = await _start(cp, follows)
        await wait_for(
            lambda: note.collapsed and thinking.collapsed and started() and screen_is_settled(app, cp.screen),
            pilot=pilot,
            description="what follows started below the folded thinking and note",
        )
        assert title.region.y < note.region.y < thinking.region.y
        assert thinking.gap_above
        assert _ends_on_the_border(cp)
        assert blank_rows
        assert max(blank_rows) == 0, blank_rows


@contextmanager
def _held_parses(monkeypatch: pytest.MonkeyPatch, *sources: str) -> Iterator[dict[str, threading.Event]]:
    """Hold each markdown parse of one of *sources* until its event is set; leaving the block sets them all."""
    gates = {source: threading.Event() for source in sources}
    build_blocks = VirtualizedMarkdown._build_blocks

    def held_build_blocks(markdown: VirtualizedMarkdown, source: str) -> list[MarkdownBlock]:
        gate = gates.get(source)
        if gate is not None:
            gate.wait(timeout=ENGINE_TURN_TIMEOUT)
        return build_blocks(markdown, source)

    monkeypatch.setattr(VirtualizedMarkdown, "_build_blocks", held_build_blocks)
    try:
        yield gates
    finally:
        for gate in gates.values():
            gate.set()


async def test_the_final_reply_replaces_the_preview_while_its_markdown_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    final = "A complete reply.\n\nWith a second paragraph."
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("Prompt")
        await panel.add_answer_delta("answer", final)
        preview = panel.query_one(AgentMessage)
        panel.finish_answer_segment("answer", completed=True)
        with _held_parses(monkeypatch, final):
            completed = asyncio.create_task(panel.add_agent_message(final))
            await wait_for(completed.done, description="the turn completes while the Markdown parse is held")
            await completed
            assert not preview.is_attached
            answer = panel.query_one(AgentMessage)
            assert answer.text == final
            body = answer.query_one(VirtualizedMarkdown)
            assert body.shown_source == ""
        await wait_for(lambda: body.shown_source == final, pilot=pilot, description="the full reply appears")


async def _an_answer_below_a_folded_group(cp: ChatPanel, pilot: object) -> tuple[AgentMessage, VirtualizedMarkdown]:
    """Start an answer below the tool group it folds, in a view that follows the bottom."""
    app = cp.app
    group = await _following_a_tool_group(cp, pilot, tools=1)
    group.collapsed = True
    answer = AgentMessage("The answer starts", is_final=False, segment_ids=("segment-1",))
    await cp.mount_transcript_widget(answer)
    body = answer.query_one(VirtualizedMarkdown)
    await wait_for(
        lambda: group.collapsed and _shows(body, "The answer starts") and screen_is_settled(app, cp.screen),
        pilot=pilot,
        description="the answer started below the folded group",
    )
    return answer, body


async def test_a_finished_answer_is_as_tall_as_the_text_it_shows_while_its_final_text_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The body parses the final text off the event loop: until it shows, the answer is as tall as the text it shows,
    the text streamed before included when that parse lands after the final update."""
    tall = "- The answer starts\n- and fills\n- the rows"
    streamed = "The answer starts and"
    final = "- The answer starts\n- and fills\n- the rows at last"
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        app = pilot.app
        cp = app.query_one(ChatPanel)
        answer, body = await _an_answer_below_a_folded_group(cp, pilot)
        answer.stream_update(tall)
        await wait_for(
            lambda: _shows(body, tall) and screen_is_settled(app, cp.screen),
            pilot=pilot,
            description="the answer streamed tall",
        )
        tall_rows = answer.content_size.height
        with _held_parses(monkeypatch, streamed, final) as gates:
            answer.stream_update(streamed)
            await wait_for(
                lambda: body.source == streamed and body.lock.is_locked,
                pilot=pilot,
                description="the streamed text parses",
            )
            answer.stream_update(final, is_final=True)
            gates[streamed].set()
            await wait_for(
                lambda: (
                    body.shown_source == streamed and body._pending_parse is None and screen_is_settled(app, cp.screen)
                ),
                pilot=pilot,
                description="the streamed text showed while the final text parsed",
            )
            assert body.lock.is_locked
            assert answer.content_size.height == _text_rows(answer) < tall_rows
            assert _ends_on_the_border(cp)
        await wait_for(
            lambda: _shows(body, final) and screen_is_settled(app, cp.screen),
            pilot=pilot,
            description="the final text showed",
        )
        assert answer.content_size.height == _text_rows(answer) == tall_rows
        assert _ends_on_the_border(cp)


async def test_a_retry_removes_an_answer_whose_final_text_still_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing the message runs waits for its body's parse: a wait there held off its removal until the parse ended."""
    final = "The answer starts\n\nand fills the rows"
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        answer, body = await _an_answer_below_a_folded_group(cp, pilot)
        with _held_parses(monkeypatch, final):
            answer.stream_update(final, is_final=True)
            await wait_for(lambda: body._pending_parse is None, description="the final text parses")
            removal = asyncio.create_task(cp.withdraw_answer(("segment-1",)))
            await wait_for(removal.done, description="the answer was removed")
            await removal
            assert body.lock.is_locked
        assert answer not in cp.mounted_agent_messages()
        assert not answer.is_attached


async def test_a_final_text_that_fails_to_parse_leaves_the_text_shown_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure is the body's: a wait for the parse in the message's handlers raised it there, and the App exited."""
    final = "The answer starts\n\nand cannot be parsed"
    build_blocks = VirtualizedMarkdown._build_blocks

    def failing_build_blocks(markdown: VirtualizedMarkdown, source: str) -> list[MarkdownBlock]:
        if source == final:
            raise RuntimeError("the parser failed")
        return build_blocks(markdown, source)

    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        app = pilot.app
        cp = app.query_one(ChatPanel)
        answer, body = await _an_answer_below_a_folded_group(cp, pilot)
        monkeypatch.setattr(VirtualizedMarkdown, "_build_blocks", failing_build_blocks)
        answer.stream_update(final, is_final=True)
        await wait_for(
            lambda: body._pending_parse is None and not body.lock.is_locked and screen_is_settled(app, cp.screen),
            pilot=pilot,
            description="the final parse failed",
        )
        assert app.is_running
        assert body.shown_source == "The answer starts"
