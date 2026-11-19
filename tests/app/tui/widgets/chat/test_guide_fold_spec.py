# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""An open fold closes from the thin guide down its left, as from its header row.

Columns 4-5 (the guide under a thinking, text or tool group's ``▾`` and the column right of it)
fold that member; columns 2-3 (the guide under the process's ``▾``) fold the whole process, the
running row or a finished process header, when that is allowed. A drag along the guide folds
nothing, and a double click on it selects nothing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec

import pytest
from textual.events import Click, MouseDown, MouseUp
from textual.geometry import Offset
from textual.widgets import Static

from chrys.app.tui.widgets.chat.fold_gap import FoldGap
from chrys.app.tui.widgets.chat.guide_fold import GuideFoldRequested
from chrys.app.tui.widgets.chat.live_process_row import LiveProcessRow
from chrys.app.tui.widgets.chat.messages import AgentMessage, ThinkingMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.process_block import ProcessBlock
from chrys.app.tui.widgets.chat.tool_call import ToolGroup, ToolGroupTitle
from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_READ
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import ChatPanelApp, click_when_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from textual.pilot import Pilot
    from textual.widget import Widget

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


def _on() -> bool:
    return True


async def _settled(pilot: Pilot[Any]) -> None:
    await wait_for(lambda: screen_is_settled(pilot.app, pilot.app.screen), pilot=pilot, description="screen settled")


def _mouse(event_type: type[Any], screen: Widget, x: int, y: int, **extra: Any) -> Any:
    return event_type(
        widget=screen,
        x=x,
        y=y,
        delta_x=0,
        delta_y=0,
        button=1,
        shift=False,
        meta=False,
        ctrl=False,
        screen_x=x,
        screen_y=y,
        **extra,
    )


async def _press(pilot: Pilot[Any], down: tuple[int, int], up: tuple[int, int], *, chain: int = 1) -> None:
    """Press at *down*, let go at *up*: the terminal reports a click where the button came up."""
    await _settled(pilot)
    screen = pilot.app.screen
    pilot.app.mouse_position = Offset(*down)
    screen._forward_event(_mouse(MouseDown, screen, *down))
    await pilot.pause()
    pilot.app.mouse_position = Offset(*up)
    screen._forward_event(_mouse(MouseUp, screen, *up))
    screen._forward_event(_mouse(Click, screen, *up, chain=chain))
    await pilot.pause()
    await _settled(pilot)


async def _click_cell(pilot: Pilot[Any], x: int, y: int) -> None:
    await _press(pilot, (x, y), (x, y))


async def _turn(panel: ChatPanel, *, finish: bool) -> None:
    await panel.add_user_message("prompt")
    await panel.add_thinking_delta("r1", "reasoning", "Look at the retry path. It has two branches to compare.")
    panel.finish_thinking_segment("r1")
    await panel.add_answer_delta("a1", "Checking.")
    panel.finish_answer_segment("a1", completed=True)
    await panel.add_tool_start("c1", "read_file", KIND_FILESYSTEM_READ, args={"path": "/tmp/a.py"})
    await panel.add_tool_result("c1", "read_file", "ok")
    await panel.add_tool_start("c2", "read_file", KIND_FILESYSTEM_READ, args={"path": "/tmp/b.py"})
    await panel.add_tool_result("c2", "read_file", "ok")
    if finish:
        await panel.add_answer_delta("final", "Done.")
        panel.finish_answer_segment("final", completed=True)
        await panel.add_agent_message("Done.")


async def _open_process(pilot: Pilot[Any], panel: ChatPanel, container: str) -> LiveProcessRow | ProcessBlock:
    """Run a turn whose process is open, with its thinking and its tool group open too; return the process."""
    await _turn(panel, finish=container == "block")
    await _settled(pilot)
    process = panel.query_one(ProcessBlock) if container == "block" else panel.query_one(LiveProcessRow)
    assert process.collapsed
    await click_when_settled(pilot, process)
    await wait_for(lambda: not process.collapsed, pilot=pilot, description="the process opens")
    await _open_members(pilot, panel)
    return process


async def _open_members(pilot: Pilot[Any], panel: ChatPanel) -> None:
    thinking = panel.query_one(ThinkingMessage)
    group = panel.query_one(ToolGroup)
    thinking.collapsed = False
    group.collapsed = False
    await wait_for(
        lambda: bool(thinking.query(VirtualizedMarkdown)) and group._content_mounted and bool(group._tools),
        pilot=pilot,
        description="the open thinking and tool group laid out",
    )
    await _settled(pilot)


def _inner_guide(member: Widget) -> Widget:
    """What draws a member's own guide, under its ``▾``: an open thinking's body, an open group's calls."""
    if isinstance(member, (ThinkingMessage, AgentMessage)):
        return member.query_one(VirtualizedMarkdown)
    return member.query_one("#tg-content")


# ---------------------------------------------------------------------------
# Columns 4-5: the member's own guide
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("column", [0, 1], ids=["guide", "beside-guide"])
@pytest.mark.parametrize("member_type", [ThinkingMessage, ToolGroup], ids=["thinking", "tool-group"])
@pytest.mark.parametrize("container", ["live", "block"])
async def test_clicking_a_member_s_guide_folds_that_member(container: str, member_type: type, column: int) -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        process = await _open_process(pilot, panel, container)
        member = panel.query_one(member_type)
        guide = _inner_guide(member)
        assert guide.region.height > 1
        await _click_cell(pilot, guide.region.x + column, guide.region.y + 1)
        await wait_for(lambda: member.collapsed, pilot=pilot, description="the member folds")
        assert not process.collapsed, "only the member folds"
        assert member.display

        await _settled(pilot)
        await _click_cell(pilot, member.region.x + 1, member.region.y)
        await wait_for(lambda: process.collapsed, pilot=pilot, description="the outer guide folds the process")
        assert member.process_hidden


# ---------------------------------------------------------------------------
# Columns 2-3: the process's guide
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("column", [0, 1], ids=["guide", "beside-guide"])
async def test_clicking_the_process_guide_folds_the_running_row_and_keeps_following(column: int) -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        row = await _open_process(pilot, panel, "live")
        assert not panel.is_anchor_released()
        member = panel.query_one(ToolGroup)
        await _click_cell(pilot, member.region.x + column, member.region.y + 1)
        await wait_for(lambda: row.collapsed, pilot=pilot, description="the running row folds")
        await _settled(pilot)
        assert not member.display and not panel.query_one(ThinkingMessage).display
        assert not panel.is_anchor_released(), "folding the running process keeps following the bottom (Q9)"


@pytest.mark.parametrize("column", [0, 1], ids=["guide", "beside-guide"])
async def test_clicking_the_process_guide_folds_a_finished_process(column: int) -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        block = await _open_process(pilot, panel, "block")
        thinking = panel.query_one(ThinkingMessage)
        await _click_cell(pilot, thinking.region.x + column, thinking.region.y + 1)
        await wait_for(lambda: block.collapsed, pilot=pilot, description="the process folds")
        assert not any(member.display for member in block.members)


async def test_the_guide_of_a_process_that_cannot_fold_does_nothing() -> None:
    async with ChatPanelApp().run_test(size=(100, 40)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await panel.add_answer_delta("a1", "Checking.")
        panel.finish_answer_segment("a1", completed=True)
        await panel.add_tool_start("c1", "read_file", KIND_FILESYSTEM_READ, args={"path": "/tmp/a.py"})
        await panel.add_tool_result("c1", "read_file", "ok")
        group = panel.query_one(ToolGroup)
        # The group waits for the user: folding the process would hide it.
        group.lock_collapse_for("question")
        await panel.add_answer_delta("final", "Done.")
        panel.finish_answer_segment("final", completed=True)
        await panel.add_agent_message("Done.")
        await _settled(pilot)
        block = panel.query_one(ProcessBlock)
        assert block.requested_collapsed and not block.collapsed
        borders = [member.styles.border_left for member in block.members]

        text = block.members[0]
        await pilot.hover(offset=(text.region.x, text.region.y))
        await _settled(pilot)
        assert [member.styles.border_left for member in block.members] == borders, "it does not light up"

        await _click_cell(pilot, text.region.x, text.region.y)
        assert not block.collapsed
        assert block.requested_collapsed, "no toggle was asked for"
        assert group.display


# ---------------------------------------------------------------------------
# Hover, drag and double click
# ---------------------------------------------------------------------------


async def test_hovering_a_guide_that_folds_lights_it_and_leaving_restores_it() -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await _open_process(pilot, panel, "block")
        guide = _inner_guide(panel.query_one(ToolGroup))
        before = guide.styles.border_left
        await pilot.hover(offset=(guide.region.x, guide.region.y + 1))
        await _settled(pilot)
        lit = guide.styles.border_left
        assert lit[0] == before[0], "it stays the same thin line"
        assert lit[1] != before[1], "it takes the colour of the fold arrow"
        await pilot.hover(offset=(guide.region.x + 30, guide.region.y + 1))
        await _settled(pilot)
        assert guide.styles.border_left == before


@pytest.mark.parametrize("container", ["row", "block"])
async def test_hovering_the_process_guide_lights_all_of_it_down_every_member(container: str) -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        process = await _open_process(pilot, panel, container)
        members = [member for member in process.members if member.display]
        assert len(members) > 1
        before = [member.styles.border_left for member in members]
        first, last = members[0], members[-1]

        await pilot.hover(offset=(first.region.x, first.region.y + 1))
        await _settled(pilot)
        lit = [member.styles.border_left for member in members]
        assert all(after[1] != old[1] for after, old in zip(lit, before, strict=True)), "the whole line lights"
        assert len({colour for _, colour in lit}) == 1

        await pilot.hover(offset=(last.region.x, last.region.y + last.region.height - 1))
        await _settled(pilot)
        assert [member.styles.border_left for member in members] == lit, "moving along it keeps all of it lit"

        await pilot.hover(offset=(last.region.x + 30, last.region.y))
        await _settled(pilot)
        assert [member.styles.border_left for member in members] == before


@pytest.mark.parametrize("process_type", [LiveProcessRow, ProcessBlock], ids=["live", "finished"])
async def test_dragging_across_a_header_or_along_a_guide_folds_nothing(
    process_type: type[LiveProcessRow | ProcessBlock],
) -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await _turn(panel, finish=process_type is ProcessBlock)
        await _settled(pilot)
        process = panel.query_one(process_type)
        assert process.collapsed
        y = process.region.y
        await _press(pilot, (process.region.x + 2, y), (process.region.x + 12, y))
        assert process.collapsed
        await click_when_settled(pilot, process, offset=(4, 0))
        await wait_for(lambda: not process.collapsed, pilot=pilot, description="a click in one cell still opens it")
        await _settled(pilot)
        y = process.region.y
        await _press(pilot, (process.region.x + 2, y), (process.region.x + 12, y))
        assert not process.collapsed
        await _open_members(pilot, panel)

        group = panel.query_one(ToolGroup)
        guide = _inner_guide(group)
        assert guide.region.height > 2
        await _press(pilot, (guide.region.x, guide.region.y), (guide.region.x, guide.region.y + 2))
        assert not group.collapsed
        thinking = panel.query_one(ThinkingMessage)
        await _press(pilot, (thinking.region.x, thinking.region.y + 1), (thinking.region.x, thinking.region.y + 2))
        assert not process.collapsed


async def test_double_clicking_a_guide_selects_nothing() -> None:
    async with ChatPanelApp(
        thinking_expanded=_on,
    ).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await _open_process(pilot, panel, "block")
        body = _inner_guide(panel.query_one(ThinkingMessage))
        screen = pilot.app.screen
        # A double click on the text itself still selects, as it always has.
        screen.clear_selection()
        await _press(pilot, (body.region.x + 6, body.region.y), (body.region.x + 6, body.region.y), chain=2)
        assert screen.selections
        screen.clear_selection()
        await _settled(pilot)
        # The second click of a double click on the guide.
        await _press(pilot, (body.region.x, body.region.y), (body.region.x, body.region.y), chain=2)
        assert not screen.selections


def _member_title(member: Widget) -> Widget:
    return member.query_one(ToolGroupTitle) if isinstance(member, ToolGroup) else member.query_one(".agent-header")


def _short_line(panel: ChatPanel, member: FoldGap) -> FoldGap:
    if member.gap_below:
        return member
    shown = panel.displayed_children
    following = shown[shown.index(member) + 1]
    assert isinstance(following, FoldGap) and following.gap_above
    return following


def _short_cell(gap: FoldGap) -> tuple[int, int]:
    return gap.content_region.x, gap.content_region.y if gap.gap_above else gap.content_region.bottom - 1


@pytest.mark.parametrize("container", ["live", "block"])
async def test_process_line_and_title_light_the_outer_guide_without_lighting_short_guides(container: str) -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        process = await _open_process(pilot, panel, container)
        thinking = panel.query_one(ThinkingMessage)
        thinking.collapsed = True
        await _settled(pilot)
        gap = _short_line(panel, thinking)
        base_gap = gap.gap_text().style
        members = [member for member in process.members if member.display]
        base = [member.styles.border_left for member in members]
        first = members[0]

        await pilot.hover(offset=(first.region.x, first.region.y + 1))
        await _settled(pilot)
        assert all(member.styles.border_left != old for member, old in zip(members, base, strict=True))
        assert gap.gap_text().style == base_gap

        await pilot.hover(offset=(process.region.x + 3, process.region.y))
        await _settled(pilot)
        assert all(member.styles.border_left != old for member, old in zip(members, base, strict=True))
        assert gap.gap_text().style == base_gap

        await pilot.hover(offset=(panel.region.right - 3, panel.region.bottom - 2))
        await _settled(pilot)
        assert [member.styles.border_left for member in members] == base
        assert gap.gap_text().style == base_gap


@pytest.mark.parametrize("container", ["live", "block"])
@pytest.mark.parametrize("member_type", [ThinkingMessage, AgentMessage, ToolGroup])
@pytest.mark.parametrize("collapsed", [True, False], ids=["folded", "open"])
async def test_member_title_lights_only_its_own_short_or_inner_guide(
    container: str, member_type: type[ThinkingMessage | AgentMessage | ToolGroup], collapsed: bool
) -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        process = await _open_process(pilot, panel, container)
        member = next(entry for entry in process.members if isinstance(entry, member_type))
        member.collapsed = collapsed
        await _settled(pilot)
        if not collapsed:
            await wait_for(lambda: member.guide_surface() is not None, pilot=pilot, description="member body is shown")
        outer = [entry.styles.border_left for entry in process.members]
        if collapsed and member_type is ToolGroup and container == "block":
            # The final answer follows this row without a fold gap.
            title = _member_title(member)
            await pilot.hover(offset=(title.region.x + 1, title.region.y))
            await _settled(pilot)
            assert [entry.styles.border_left for entry in process.members] == outer
            assert member._gap_below_hover_color is None
            return
        painter = _short_line(panel, member) if collapsed else _inner_guide(member)
        below = painter is member and collapsed
        before = painter.gap_text(below=below).style if collapsed else painter.styles.border_left
        title = _member_title(member)
        await pilot.hover(offset=(title.region.x + 1, title.region.y))
        await _settled(pilot)
        after = painter.gap_text(below=below).style if collapsed else painter.styles.border_left
        assert after != before
        assert [entry.styles.border_left for entry in process.members] == outer
        await pilot.hover(offset=(panel.region.right - 3, panel.region.bottom - 2))
        await _settled(pilot)
        assert (painter.gap_text(below=below).style if collapsed else painter.styles.border_left) == before


@pytest.mark.parametrize("container", ["live", "block"])
@pytest.mark.parametrize("trailing", [False, True], ids=["next-entry-gap", "trailing-gap"])
async def test_short_guide_lights_and_opens_the_member_above_it(container: str, trailing: bool) -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        process = await _open_process(pilot, panel, container)
        member = panel.query_one(ToolGroup) if trailing else panel.query_one(ThinkingMessage)
        if trailing and container == "block":
            await panel.query(AgentMessage).last().remove()
        member.collapsed = True
        await _settled(pilot)
        gap = _short_line(panel, member)
        base = gap.gap_text(below=trailing).style
        cell = (gap.content_region.x, gap.content_region.bottom - 1) if trailing else _short_cell(gap)
        await pilot.hover(offset=cell)
        await _settled(pilot)
        assert gap.gap_text(below=trailing).style != base
        assert gap.styles.pointer == "pointer"
        await _click_cell(pilot, *cell)
        await wait_for(lambda: not member.collapsed, pilot=pilot, description="short guide opens its member")
        assert not process.collapsed
        assert gap.gap_text(below=trailing).style == base


@pytest.mark.parametrize("container", ["live", "block"])
async def test_locked_member_and_process_titles_do_not_light_any_guides(container: str) -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        process = await _open_process(pilot, panel, container)
        group = panel.query_one(ToolGroup)
        group.lock_collapse_for("question")
        await _settled(pilot)
        members = list(process.members)
        before = [member.styles.border_left for member in members]
        body = _inner_guide(group)
        inner = body.styles.border_left
        title = _member_title(group)
        for cell in ((process.region.x + 3, process.region.y), (title.region.x + 1, title.region.y)):
            await pilot.hover(offset=cell)
            await _settled(pilot)
            assert [member.styles.border_left for member in members] == before
            assert body.styles.border_left == inner


async def test_a_short_guide_above_a_live_title_opens_the_preceding_member() -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await panel.add_tool_start("old", "read_file", KIND_FILESYSTEM_READ, args={"path": "old.py"})
        await panel.add_tool_result("old", "read_file", "ok")
        await panel.add_interrupted()
        previous = panel.query_one(ToolGroup)
        await panel.add_thinking_delta("next", "reasoning", "Continue.")
        row = panel.query_one(LiveProcessRow)
        panel._toggle_live_process_row(row)
        await _settled(pilot)
        assert previous.collapsed and row.gap_above and not row.collapsed
        before = [member.styles.border_left for member in row.members]
        x = previous.query_one(ToolGroupTitle).content_region.x
        y = row.content_region.y

        def guide_cell():
            strip = row.screen._compositor.render_strips()[y].crop(x, x + 1)
            return strip.text, next(iter(strip)).style

        char, base = guide_cell()
        assert char == "│"
        assert row.screen._compositor.render_strips()[y].text[row.content_region.x] == " "

        await pilot.hover(offset=(row.content_region.x, y))
        await _settled(pilot)
        assert [member.styles.border_left for member in row.members] == before
        assert guide_cell() == ("│", base)
        await _click_cell(pilot, row.content_region.x, y)
        assert not row.collapsed and previous.collapsed

        await pilot.hover(offset=(x, y))
        await _settled(pilot)
        assert guide_cell()[1] != base
        assert [member.styles.border_left for member in row.members] == before
        await _click_cell(pilot, x, y)
        assert not previous.collapsed and not row.collapsed


@pytest.mark.parametrize("member_type", [ThinkingMessage, ToolGroup])
async def test_hover_stays_on_the_members_own_guide_as_its_title_toggles(member_type: type) -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await _open_process(pilot, panel, "live")
        member = panel.query_one(member_type)
        member.collapsed = True
        await _settled(pilot)
        gap = _short_line(panel, member)
        below = gap is member
        base = gap.gap_text(below=below).style
        title = _member_title(member)
        await pilot.hover(offset=(title.region.x + 1, title.region.y))
        await _settled(pilot)
        assert gap.gap_text(below=below).style != base

        await click_when_settled(pilot, title, offset=(1, 0))
        await _settled(pilot)
        assert not member.collapsed
        assert gap.gap_text(below=below).style == base
        body = _inner_guide(member)
        assert body in member._guide_hover.painters

        await click_when_settled(pilot, title, offset=(1, 0))
        await _settled(pilot)
        assert member.collapsed
        assert gap.gap_text(below=below).style != base


@pytest.mark.parametrize("container", ["live", "block"])
async def test_folding_from_a_lit_process_title_restores_the_members_guides(container: str) -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        process = await _open_process(pilot, panel, container)
        before = [member.styles.border_left for member in process.members]
        cell = process.region.x + 3, process.region.y
        await pilot.hover(offset=cell)
        await _settled(pilot)
        assert [member.styles.border_left for member in process.members] != before
        await _click_cell(pilot, *cell)
        await _settled(pilot)
        assert process.collapsed
        assert [member.styles.border_left for member in process.members] == before
        await pilot.hover(offset=cell)
        await _settled(pilot)
        assert [member.styles.border_left for member in process.members] == before


async def test_fold_all_clears_the_hover_on_the_finished_title_under_the_mouse() -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        process = await _open_process(pilot, panel, "block")
        before = [member.styles.border_left for member in process.members]
        await pilot.hover(offset=(process.content_region.x + 3, process.content_region.y))
        await _settled(pilot)
        assert process.styles.pointer == "pointer"
        assert [member.styles.border_left for member in process.members] != before

        assert panel.toggle_fold_all()
        await _settled(pilot)
        assert process.styles.pointer != "pointer"
        assert [member.styles.border_left for member in process.members] == before


async def test_fold_all_clears_the_hover_on_the_live_title_under_the_mouse() -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        process = await _open_process(pilot, panel, "live")
        before = [member.styles.border_left for member in process.members]
        await pilot.hover(offset=(process.content_region.x + 3, process.content_region.y))
        await _settled(pilot)
        assert process.styles.pointer == "pointer"
        assert [member.styles.border_left for member in process.members] != before

        assert panel.toggle_fold_all()
        await _settled(pilot)
        assert process.styles.pointer != "pointer"
        assert [member.styles.border_left for member in process.members] == before


@pytest.mark.parametrize("guide", ["activity", "short-gap"])
async def test_theme_change_recolors_a_guide_under_the_stationary_pointer(guide: str) -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 50)) as pilot:
        app = pilot.app
        panel = app.query_one(ChatPanel)
        process = await _open_process(pilot, panel, "live")
        thinking = panel.query_one(ThinkingMessage)
        if guide == "short-gap":
            thinking.collapsed = True
            await _settled(pilot)
            cell = _short_cell(_short_line(panel, thinking))
            pointer = cell
            title = _member_title(thinking)
        else:
            cell = thinking.region.x, thinking.region.y
            pointer = process.content_region.x + 3, process.content_region.y
            title = process

        await pilot.hover(offset=pointer)
        await _settled(pilot)
        compositor = app.screen._compositor
        before = compositor.get_style_at(*cell).color
        title_color = title.visual_style.foreground
        assert before is not None
        app.theme = "textual-light"
        await _settled(pilot)
        assert app.mouse_position == Offset(*pointer)
        assert title.visual_style.foreground != title_color
        stationary = compositor.get_style_at(*cell).color
        assert stationary != before

        await pilot.hover(offset=(0, 0))
        await pilot.hover(offset=pointer)
        await _settled(pilot)
        assert stationary == compositor.get_style_at(*cell).color


@pytest.mark.parametrize("title_visible", [True, False], ids=["stays-in-view", "lands-under-pointer"])
async def test_a_guide_fold_uses_the_view_after_prepend(title_visible: bool) -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 14)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.mount(Static("\n".join(["above"] * 20)))
        await _turn(panel, finish=True)
        process = panel.query_one(ProcessBlock)
        panel._toggle_process_block(process)
        notice = Static("Still visible when the process folds.")
        later_member = panel.query_one(ToolGroup)
        await panel.mount(notice, before=later_member)
        await panel.mount(Static("\n".join(["below"] * 30)))
        await _settled(pilot)

        # In the reverse case the first visible entry must survive the fold
        # so the view hold can still track it through the prepend.
        member = panel.query_one(ThinkingMessage) if title_visible else later_member
        top = panel.content_region.y
        row = panel.content_region.height - 3 if title_visible else process.virtual_region.y - notice.virtual_region.y
        panel.release_anchor()
        panel.scroll_to(y=process.virtual_region.y - row, animate=False, immediate=True)
        await _settled(pilot)
        assert process.region.y == top + row
        edge = min(panel.content_region.bottom, member.region.bottom) - 1
        assert top <= edge and edge >= member.region.y
        older = Static("\n".join(["older"] * max(4, 2 - row)))

        with pilot.app.batch_update():
            # The prepend can be the first arrangement after the hold starts.
            panel.begin_view_hold()
            await panel.mount_replay_batch([older], before=panel.transcript_content_children()[0])
            panel.on_guide_fold_requested(GuideFoldRequested(member, process=True, screen_y=edge))
        await _settled(pilot)
        assert process.collapsed
        assert process.region.y == (top + row if title_visible else edge)


@pytest.mark.parametrize("count", [1, 64])
async def test_hover_along_one_guide_does_not_revisit_process_members(
    count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 160)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        for index in range(count):
            await panel.add_thinking_delta(f"r{index}", "reasoning", f"Step {index}.")
            panel.finish_thinking_segment(f"r{index}")
        row = panel.query_one(LiveProcessRow)
        await click_when_settled(pilot, row)
        first = panel.query(ThinkingMessage).first()
        first.collapsed = False
        await _settled(pilot)
        x, y = first.region.offset
        await pilot.hover(offset=(x, y))
        await _settled(pilot)
        lit = first.styles.border_left
        visits = 0

        class CountedMembers(list):
            def __iter__(self):
                nonlocal visits
                for member in super().__iter__():
                    visits += 1
                    yield member

        monkeypatch.setattr(row, "members", CountedMembers(row.members))
        await pilot.hover(offset=(x, y + 1))
        await _settled(pilot)
        assert first.styles.pointer == "pointer"
        assert first.styles.border_left == lit
        assert visits == 0


async def test_cross_member_hover_restores_all_lines_without_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        process = await _open_process(pilot, panel, "live")
        first, last = process.members[0], process.members[-1]
        before = [entry.styles.border_left for entry in process.members]
        layout = create_autospec(pilot.app.screen._refresh_layout, side_effect=pilot.app.screen._refresh_layout)
        monkeypatch.setattr(pilot.app.screen, "_refresh_layout", layout)
        await pilot.hover(offset=(first.region.x, first.region.y + 1))
        await _settled(pilot)
        lit = [entry.styles.border_left for entry in process.members]
        await pilot.hover(offset=(last.region.x, last.region.bottom - 1))
        await _settled(pilot)
        assert [entry.styles.border_left for entry in process.members] == lit
        await pilot.hover(offset=(panel.region.right - 3, panel.region.bottom - 2))
        await _settled(pilot)
        assert [entry.styles.border_left for entry in process.members] == before
        layout.assert_not_called()


async def test_hovering_a_large_process_repaints_only_its_visible_guide_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 43)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        for index in range(80):
            await panel.add_thinking_delta(f"r{index}", "reasoning", f"Step {index}.")
            panel.finish_thinking_segment(f"r{index}")
        row = panel.query_one(LiveProcessRow)
        await click_when_settled(pilot, row)
        await pilot.hover(offset=(0, 0))
        await wait_for(
            lambda: screen_is_settled(pilot.app, pilot.app.screen) and not pilot.app.screen._dirty_widgets,
            pilot=pilot,
            description="the initial process frame has painted",
        )
        members = list(row.members)
        shown = [member for member in members if member.region.overlaps(panel.content_region)]
        assert 0 < len(shown) < len(members) == 80
        renders = [create_autospec(member.render_lines, side_effect=member.render_lines) for member in members]
        for member, render in zip(members, renders, strict=True):
            monkeypatch.setattr(member, "render_lines", render)
        painted_frames: list[int] = []
        display = pilot.app._display

        def record_frame(screen: Any, renderable: Any) -> None:
            if renderable is not None:
                painted_frames.append(sum(call.args[0].height for render in renders for call in render.call_args_list))
                for render in renders:
                    render.reset_mock()
            display(screen, renderable)

        monkeypatch.setattr(pilot.app, "_display", record_frame)
        layout = create_autospec(pilot.app.screen._refresh_layout, side_effect=pilot.app.screen._refresh_layout)
        monkeypatch.setattr(pilot.app.screen, "_refresh_layout", layout)
        first = shown[0]
        await pilot.hover(offset=(first.region.x, max(first.region.y, panel.content_region.y)))
        await wait_for(
            lambda: screen_is_settled(pilot.app, pilot.app.screen) and not pilot.app.screen._dirty_widgets,
            pilot=pilot,
            description="the lit process frame has painted",
        )
        assert painted_frames and 0 < max(painted_frames) <= 43, painted_frames
        layout.assert_not_called()


@pytest.mark.parametrize("chain", [1, 2], ids=["drag", "double-click"])
async def test_short_guides_consume_multi_cell_and_double_clicks_without_toggling(chain: int) -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 50)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await _open_process(pilot, panel, "live")
        thinking = panel.query_one(ThinkingMessage)
        thinking.collapsed = True
        await _settled(pilot)
        cell = _short_cell(_short_line(panel, thinking))
        end = (cell[0] + 1, cell[1]) if chain == 1 else cell
        pilot.app.screen.clear_selection()
        await _press(pilot, cell, end, chain=chain)
        assert thinking.collapsed and not pilot.app.screen.selections


async def test_hovered_live_title_lights_a_new_member_without_mouse_movement() -> None:
    async with ChatPanelApp(thinking_expanded=_on).run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        await panel.add_thinking_delta("r1", "reasoning", "First thought.")
        panel.finish_thinking_segment("r1")
        title = panel.query_one(LiveProcessRow)
        await click_when_settled(pilot, title)
        await panel.add_thinking_delta("r2", "reasoning", "Second thought.")
        panel.finish_thinking_segment("r2")
        first = panel.query(ThinkingMessage).first()
        await _settled(pilot)
        original = first.styles.border_left
        await pilot.hover(offset=(title.region.x + 3, title.region.y))
        await _settled(pilot)
        assert first.styles.border_left != original
        await panel.add_thinking_delta("r3", "reasoning", "New thought.")
        await _settled(pilot)
        assert panel.query(ThinkingMessage).last().styles.border_left == first.styles.border_left
