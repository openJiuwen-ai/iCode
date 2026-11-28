# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-container drag selection while scrolling restored chat history."""

from __future__ import annotations

from asyncio import Event

import pytest
from textual import events
from textual.geometry import Offset
from textual.screen import Screen
from textual.selection import Selection
from textual.widget import Widget
from textual.widgets import Static, TextArea

import chrys.app.tui.behaviors.right_click_copy as right_click_copy
from chrys.app.tui.widgets.chat.messages import RetryMessage, UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.app_header import AppHeader
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from tests.app.tui.widgets.chat.quote_popup_fixtures import _MainScreenApp, _open_history_turn
from tests.app.tui.widgets.chat.replay_fixtures import _turns
from tests.support.waiting import wait_for


def _sidebar_gap(screen: Screen, sidebar: Widget, row: int) -> Offset:
    for x in range(sidebar.region.x, sidebar.region.right):
        point = Offset(x, row)
        hit, offset = screen.get_widget_and_offset_at(*point)
        if hit is not None and hit.allow_select and sidebar in hit.ancestors_with_self and offset is None:
            return point
    pytest.fail("No selectable gap in the sidebar at the requested row.")


@pytest.mark.parametrize(
    "destination_kind", ["to-header", "to-input", "to-input-from-padding", "to-input-from-panel-gap"]
)
async def test_copy_preserves_a_drag_start_after_its_history_page_scrolls_out(
    monkeypatch: pytest.MonkeyPatch, destination_kind: str
) -> None:
    app = _MainScreenApp()
    copied: list[str] = []
    monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, text: copied.append(text) or True)
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(120))
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        towards_header = destination_kind == "to-header"
        from_padding = destination_kind == "to-input-from-padding"
        from_panel_gap = destination_kind == "to-input-from-panel-gap"
        turn = 80 if towards_header else 40
        body = await _open_history_turn(pilot, panel, turn)
        prompt = panel.query_one(f"#turn-{turn}", UserMessage)
        leaf = prompt.query_one(".message-header", Static) if towards_header else body
        if from_padding:
            origin = Offset(prompt.region.right - 1, prompt.region.y)
            assert screen.get_widget_and_offset_at(*origin) == (prompt, None)
        elif from_panel_gap:
            origin = Offset(panel.content_region.x, leaf.content_region.y + 1)
            assert screen.get_widget_and_offset_at(*origin) == (panel, None)
        else:
            origin = leaf.content_region.offset + Offset(2, 0)
        await pilot.mouse_down(offset=origin)
        await pilot.hover(offset=origin + (Offset(0, 1) if from_padding or from_panel_gap else Offset(1, 0)))
        state = screen._select_state
        assert screen._selecting and state is not None
        if from_padding or from_panel_gap:
            assert state.start.container is (prompt if from_padding else panel)
            assert state.start.content_widget is None and state.start.content_offset is None
        else:
            assert state.start.content_widget is leaf and state.start.content_offset == Offset(2, 0)

        await _open_history_turn(pilot, panel, 1 if towards_header else 120)
        if from_panel_gap:
            assert panel.scroll_offset != state.start.container_initial_scroll_offset
        else:
            await wait_for(lambda: not state.start.container.region, pilot=pilot)
        assert leaf.is_attached and not panel.query("#turn-60")
        destination = (
            screen.query_one(AppHeader).region.offset + Offset(10, 0)
            if towards_header
            else screen.query_one("#chat-input", TextArea).content_region.offset
        )
        await pilot.hover(offset=destination)
        assert screen._select_state is not None and screen._select_state.screen_offset == destination
        if towards_header:
            first = panel.query_one("#turn-1").query_one(VirtualizedMarkdown)
            assert first in screen.selections
        await pilot.mouse_up(offset=destination)
        screen.action_copy_text()

        await wait_for(lambda: bool(copied), pilot=pilot, description="copy includes the selected cold history")
        lines = [line for line in copied[0].splitlines() if line.startswith(("question ", "answer ", "estion "))]
        if towards_header:
            expected = [line for index in range(turn - 1) for line in (f"question {index}", f"answer {index}")]
            assert lines == expected
            assert copied[0].splitlines()[-1] == "[You]"
            assert not panel.query("#turn-99")
        else:
            first_line = "question" if from_padding else "estion"
            expected = [f"answer {turn - 1}"]
            if not from_panel_gap:
                expected.insert(0, f"{first_line} {turn - 1}")
            expected.extend(line for index in range(turn, 120) for line in (f"question {index}", f"answer {index}"))
            assert lines == expected
            assert not panel.query("#turn-1")


@pytest.mark.parametrize("from_padding", [False, True], ids=["body", "padding"])
async def test_copy_from_offscreen_start_with_active_geometry(
    monkeypatch: pytest.MonkeyPatch, from_padding: bool
) -> None:
    app = _MainScreenApp()
    copied: list[str] = []
    monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, text: copied.append(text) or True)
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(120))
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        leaf = await _open_history_turn(pilot, panel, 8)
        prompt = panel.query_one("#turn-8")
        origin = (
            Offset(prompt.region.right - 1, prompt.region.y)
            if from_padding
            else leaf.content_region.offset + Offset(2, 0)
        )
        await pilot.mouse_down(offset=origin)
        state = screen._select_state
        assert screen._selecting and state is not None
        assert state.start.content_widget is (None if from_padding else leaf)

        await _open_history_turn(pilot, panel, 1)
        assert state.start.container.region and state.start.pointer_start_offset.y >= panel.region.bottom
        assert not panel.query("#turn-60")
        destination = screen.query_one(AppHeader).region.offset + Offset(10, 0)
        await pilot.hover(offset=destination)
        tail = panel.query_one("#turn-120").query_one(VirtualizedMarkdown)
        tail_selected = tail in screen.selections
        await pilot.mouse_up(offset=destination)
        screen.action_copy_text()

        await wait_for(lambda: bool(copied), pilot=pilot)
        assert not panel.query("#turn-60")
        assert "question 0" in copied[0] and "answer 6" in copied[0]
        assert "question 119" not in copied[0]
        assert copied[0].splitlines()[-1] == ("[You]" if from_padding else "qu")
        assert not tail_selected


@pytest.mark.parametrize("from_padding", [False, True], ids=["panel-gap", "message-padding"])
@pytest.mark.parametrize("turns", [20, 120], ids=["fully-mounted", "windowed"])
async def test_drag_reentering_chat_keeps_the_original_gap(
    monkeypatch: pytest.MonkeyPatch, from_padding: bool, turns: int
) -> None:
    app = _MainScreenApp()
    copied: list[str] = []
    monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, text: copied.append(text) or True)
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(turns))
        if turns == 20:
            await panel.wait_replay_complete()
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        start_turn = 2 if turns == 20 else 40
        leaf = await _open_history_turn(pilot, panel, start_turn)
        prompt = panel.query_one(f"#turn-{start_turn}")
        if from_padding:
            origin = Offset(prompt.region.right - 1, prompt.region.y)
        else:
            origin = Offset(panel.content_region.x, leaf.content_region.y + 1)
        await pilot.mouse_down(offset=origin)
        await pilot.hover(offset=origin + Offset(0, 1))
        start = screen._select_state
        assert start is not None and start.start.content_widget is None
        last = await _open_history_turn(pilot, panel, turns)

        editor = screen.query_one("#chat-input", TextArea)
        await pilot.hover(offset=(origin.x if not from_padding else editor.content_region.x, editor.content_region.y))
        assert screen._fallback_select_state is not None
        last_question = f"question {turns - 1}"
        end = last.content_region.offset + Offset(len(last_question), 0)
        await pilot.hover(offset=end)
        assert screen._chat_selection.active
        await pilot.mouse_up(offset=end)
        screen.action_copy_text()

        await wait_for(lambda: bool(copied), pilot=pilot)
        lines = copied[0].splitlines()
        assert (f"question {start_turn - 1}" in lines) is from_padding
        assert f"answer {start_turn - 1}" in lines and last_question in lines
        assert f"question {start_turn - 2}" not in lines
        if turns == 120:
            assert not panel.query("#turn-1")


@pytest.mark.parametrize("from_gap", [False, True], ids=["body", "gap"])
async def test_sidebar_drag_preserves_the_direction_of_an_offscreen_origin(
    monkeypatch: pytest.MonkeyPatch, from_gap: bool
) -> None:
    app = _MainScreenApp()
    copied: list[str] = []
    monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, text: copied.append(text) or True)
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(120))
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        leaf = await _open_history_turn(pilot, panel, 80)
        origin = (
            Offset(panel.content_region.x, leaf.content_region.y + 1)
            if from_gap
            else leaf.content_region.offset + Offset(2, 0)
        )
        await pilot.mouse_down(offset=origin)
        await pilot.hover(offset=origin + (Offset(0, 1) if from_gap else Offset(1, 0)))
        state = screen._select_state
        assert state is not None and state.start.content_widget is (None if from_gap else leaf)
        await _open_history_turn(pilot, panel, 1)
        await wait_for(lambda: not leaf.region, pilot=pilot)
        sidebar = screen.query_one("SidebarPanel")
        target = Offset(sidebar.region.x + 3, panel.region.bottom - 1)
        hit, _ = screen.get_widget_and_offset_at(*target)
        assert hit is not None and sidebar in hit.ancestors_with_self

        await pilot.hover(offset=target)
        if not from_gap:
            assert screen.selections.get(leaf) == Selection(None, Offset(2, 0))
        await pilot.mouse_up(offset=target)
        screen.action_copy_text()
        await wait_for(lambda: bool(copied), pilot=pilot)
        assert "question 59" in copied[0]
        if from_gap:
            assert "question 79" in copied[0] and "question 80" not in copied[0]
        else:
            assert copied[0].splitlines()[-1] == "qu"
        assert "estion 79" not in copied[0].splitlines() and not panel.query("#turn-99")


@pytest.mark.parametrize("upwards", [True, False], ids=["upwards", "downwards"])
@pytest.mark.parametrize("origin_kind", ["cold-body", "measured-body", "measured-gap"])
async def test_crossing_into_the_sidebar_copies_a_contiguous_chat_range(
    monkeypatch: pytest.MonkeyPatch, upwards: bool, origin_kind: str
) -> None:
    app = _MainScreenApp()
    copied: list[str] = []
    monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, text: copied.append(text) or True)
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(120))
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        cold = origin_kind == "cold-body"
        from_gap = origin_kind == "measured-gap"
        if cold:
            origin_turn, crossed_turn = (80, 21) if upwards else (40, 101)
        else:
            origin_turn, crossed_turn = (8, 2) if upwards else (1, 8)
        origin = await _open_history_turn(pilot, panel, origin_turn)
        start = (
            Offset(panel.content_region.x, origin.content_region.y + 1)
            if from_gap
            else origin.content_region.offset + Offset(2, 0)
        )
        await pilot.mouse_down(offset=start)
        await pilot.hover(offset=start + (Offset(0, 1) if from_gap else Offset(1, 0)))
        state = screen._select_state
        assert state is not None and state.start.content_widget is (None if from_gap else origin)

        await _open_history_turn(pilot, panel, crossed_turn - 1)
        visible = panel.query_one(f"#turn-{crossed_turn}").query_one(VirtualizedMarkdown)
        if cold:
            await wait_for(lambda: not origin.region, pilot=pilot)
        else:
            assert origin.region and not origin.region.overlaps(panel.content_region)
        sidebar = screen.query_one("SidebarPanel")
        destination = Offset(sidebar.region.x + 3, visible.content_region.y)
        hit, _ = screen.get_widget_and_offset_at(*destination)
        assert hit is not None and sidebar in hit.ancestors_with_self
        assert not panel.query("#turn-60")
        unrelated = ("#turn-95" if upwards else "#turn-10") if cold else "#turn-60"
        assert not panel.query(unrelated)
        await pilot.hover(offset=destination)
        if not from_gap:
            assert screen.selections.get(origin) == (
                Selection(None, Offset(2, 0)) if upwards else Selection(Offset(2, 0), None)
            )
        assert (visible in screen.selections) is not upwards
        await pilot.mouse_up(offset=destination)
        screen.action_copy_text()

        await wait_for(lambda: bool(copied), pilot=pilot)
        questions = [
            line for line in copied[0].splitlines() if line.startswith(("question ", "estion ")) or line == "qu"
        ]
        if upwards:
            expected = [f"question {index}" for index in range(crossed_turn, origin_turn - 1)]
            expected.append(f"question {origin_turn - 1}" if from_gap else "qu")
        else:
            expected = [f"question {index}" for index in range(origin_turn, crossed_turn)]
            if not from_gap:
                expected.insert(0, f"estion {origin_turn - 1}")
        assert questions == expected
        assert not panel.query(unrelated)
        assert screen._pending_quote_pick is not None and screen._pending_quote_pick[0] == copied[0]


async def test_sidebar_drag_from_gap_survives_estimated_rows_rebasing(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MainScreenApp()
    copied: list[str] = []
    monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, text: copied.append(text) or True)
    messages = _turns(100, with_tools=True)
    output = "\n".join(f"line {index}: output content from tool execution" for index in range(25))
    for index in range(100):
        call, result = messages[index * 5 + 1 : index * 5 + 3]
        call["contents"][0].update(name="execute_shell", arguments={"command": f"echo {index}"})
        result["contents"][0]["result"] = output
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(messages)
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        panel.toggle_fold_all()
        await pilot.pause()

        prompt = await _open_history_turn(pilot, panel, 97)
        origin = Offset(panel.content_region.x, prompt.content_region.y + 1)
        assert screen.get_widget_and_offset_at(*origin) == (panel, None)
        await pilot.mouse_down(offset=origin)
        await pilot.hover(offset=origin + Offset(0, 1))
        state = screen._select_state
        assert state is not None and state.start.container is panel and state.start.content_widget is None

        for direction in [-1] * 10 + [1] * 6:
            panel.user_scroll_page(direction)
            await wait_for(
                lambda: not panel.history_layout_is_held() and panel._history_scroll.relative is None, pilot=pilot
            )
            await pilot.pause()
        visible_turns = [child.id for child in panel.query(UserMessage) if child.region.overlaps(panel.content_region)]
        assert visible_turns == ["turn-92", "turn-93"]
        assert panel.scroll_offset.y > state.start.container_initial_scroll_offset.y

        sidebar = screen.query_one("SidebarPanel")
        destination = Offset(sidebar.region.x + 3, panel.region.y + panel.region.height // 2)
        await pilot.hover(offset=destination)
        await pilot.mouse_up(offset=destination)
        screen.action_copy_text()

        await wait_for(lambda: bool(copied), pilot=pilot)
        questions = [line for line in copied[0].splitlines() if line.startswith("question ")]
        assert questions == [f"question {index}" for index in range(92, 97)]


async def test_cross_panel_copy_ignores_mounted_messages_outside_the_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(120))
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        await _open_history_turn(pilot, panel, 1)
        leaf = await _open_history_turn(pilot, panel, 6)
        other = panel.query_one("#turn-113")
        assert not other.region

        copied: list[str] = []
        monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, text: copied.append(text) or True)
        start = leaf.content_region.offset + Offset(5, 0)
        end = screen.query_one(AppHeader).region.offset + Offset(10, 0)
        await pilot.mouse_down(offset=start)
        await pilot.hover(offset=end)
        await pilot.mouse_up(offset=end)
        assert screen._pending_selection_history() is None
        screen.action_copy_text()
        await wait_for(lambda: bool(copied), pilot=pilot)
        assert "question 0" in copied[0] and "answer 4" in copied[0]
        assert "question 112" not in copied[0]
        assert screen._pending_quote_pick is not None and screen._pending_quote_pick[0] == copied[0]


@pytest.mark.parametrize("retry", [False, True], ids=["new-turn", "retry-resumes"])
@pytest.mark.parametrize("reverse", [False, True], ids=["header-to-sidebar", "sidebar-to-header"])
async def test_drag_between_header_and_sidebar_copies_the_full_transcript(
    monkeypatch: pytest.MonkeyPatch, reverse: bool, retry: bool
) -> None:
    app = _MainScreenApp()
    copied: list[str] = []
    monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, text: copied.append(text) or True)
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(120))
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        if retry:
            await panel.add_retry("Retrying", 1, 3, 30)
            await pilot.pause()
            assert panel.query(RetryMessage)
        header = screen.query_one(AppHeader)
        sidebar = screen.query_one("SidebarPanel")
        upper, side = header.region.offset, sidebar.region.offset + Offset(0, 1)
        widget, offset = screen.get_widget_and_offset_at(*side)
        assert widget is not None and sidebar in widget.ancestors_with_self and offset is not None

        start, end = (side, upper) if reverse else (upper, side)
        await pilot.mouse_down(offset=start)
        await pilot.hover(offset=end)
        assert screen._select_state is not None and screen._select_state.selection_bounds.overlaps(panel.region)
        await pilot.mouse_up(offset=end)
        assert not panel.query("#turn-60")
        if not retry:
            await panel.add_user_message("question appended after release")
        await panel.add_answer_delta("late-answer", "answer appended after release")
        assert not panel.query(RetryMessage)
        screen.action_copy_text()

        await wait_for(lambda: bool(copied), pilot=pilot)
        assert len(copied) == 1
        messages = [line for line in copied[0].splitlines() if line.startswith(("question ", "answer "))]
        assert messages == [line for i in range(120) for line in (f"question {i}", f"answer {i}")]


@pytest.mark.parametrize("reverse", [False, True], ids=["header-to-gap", "gap-to-header"])
async def test_drag_between_header_and_sidebar_gap_copies_only_the_chat_prefix(
    monkeypatch: pytest.MonkeyPatch, reverse: bool
) -> None:
    app = _MainScreenApp()
    copied: list[str] = []
    monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, text: copied.append(text) or True)
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(80))
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        tail = panel.query_one("#turn-79").query_one(VirtualizedMarkdown)
        prompt = await _open_history_turn(pilot, panel, 1)
        assert tail.is_attached and not tail.region

        header = screen.query_one("#header-title", Static)
        upper = header.content_region.offset
        sidebar = screen.query_one("SidebarPanel")
        side = _sidebar_gap(screen, sidebar, prompt.content_region.y)

        start, end = (side, upper) if reverse else (upper, side)
        await pilot.mouse_down(offset=start)
        await pilot.hover(offset=end)
        assert screen._select_state is not None and screen._select_state.selection_bounds.overlaps(panel.region)
        await pilot.mouse_up(offset=end)
        screen.action_copy_text()

        await wait_for(lambda: bool(copied), pilot=pilot)
        assert tail not in screen.selections
        lines = [line for line in copied[0].splitlines() if line.startswith(("question ", "answer "))]
        assert lines == ["question 0"]
        assert not panel.query("#turn-40")


@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
@pytest.mark.parametrize("suffix", [False, True], ids=["prefix", "suffix"])
@pytest.mark.parametrize("reverse", [False, True], ids=["from-chrome", "from-sidebar"])
async def test_sidebar_gap_selection_keeps_its_boundary_after_wheel_scroll(
    monkeypatch: pytest.MonkeyPatch, suffix: bool, reverse: bool, warm: bool
) -> None:
    app = _MainScreenApp()
    copied: list[str] = []
    monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, text: copied.append(text) or True)
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(100))
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        if warm:
            await panel.wait_replay_complete()
            await pilot.pause()
        prompt = await _open_history_turn(pilot, panel, 30)
        chrome_id = "#status-text" if suffix else "#header-title"
        chrome = screen.query_one(chrome_id, Static).content_region.offset
        side = _sidebar_gap(screen, screen.query_one("SidebarPanel"), prompt.content_region.y)
        start, end = (side, chrome) if reverse else (chrome, side)
        await pilot.mouse_down(offset=start)
        await pilot.hover(offset=end)
        await pilot.mouse_up(offset=end)
        assert screen._pending_selection_history() is not None

        row = panel.scroll_y
        await pilot._post_mouse_events([events.MouseScrollDown] * 4, offset=prompt.content_region.offset, button=0)
        await wait_for(lambda: panel.scroll_y > row and not panel.history_layout_is_held(), pilot=pilot)
        screen.action_copy_text()

        await wait_for(lambda: bool(copied), pilot=pilot)
        questions = [line for line in copied[0].splitlines() if line.startswith("question ")]
        indices = range(29, 100) if suffix else range(30)
        assert questions == [f"question {index}" for index in indices]


async def test_copy_survives_removal_of_an_excluded_gap_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MainScreenApp()
    copied: list[str] = []
    monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, text: copied.append(text) or True)
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(100))
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        await _open_history_turn(pilot, panel, 30)
        last_visible_row = max(leaf.content_region.y for leaf, _ in screen._chat_selection._visible_leaves(panel))
        side = _sidebar_gap(screen, screen.query_one("SidebarPanel"), last_visible_row + 1)
        status = screen.query_one("#status-text", Static).content_region.offset
        await pilot.mouse_down(offset=side)
        await pilot.hover(offset=status)
        await pilot.mouse_up(offset=status)

        snapshot = screen._fallback_select_snapshot
        assert snapshot is not None and snapshot.anchors is not None
        excluded = snapshot.anchors.excluded_leaf
        assert excluded is not None and excluded not in screen.selections
        owner = next(widget for widget in excluded.ancestors_with_self if widget.parent is panel)
        boundary = panel.transcript_seq(owner)
        assert boundary is not None
        viewport = panel._history_viewport
        assert viewport is not None
        expected = [
            f"question {index}"
            for index in range(100)
            if (widget := viewport.turn(f"turn-{index + 1}")) is not None
            and (stamp := panel.transcript_seq(widget)) is not None
            and stamp > boundary
        ]
        expected.append("question 99")
        await panel.remove_transcript_widget(owner)
        screen.action_copy_text()

        await wait_for(lambda: bool(copied), pilot=pilot)
        questions = [line for line in copied[0].splitlines() if line.startswith("question ")]
        assert questions == expected


async def test_drag_from_status_to_chat_excludes_a_turn_appended_before_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MainScreenApp()
    copied: list[str] = []
    monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, text: copied.append(text) or True)
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(100))
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        prompt = await _open_history_turn(pilot, panel, 30)
        status = screen.query_one("#status-text", Static).content_region.offset
        await pilot.mouse_down(offset=status)
        await pilot.hover(offset=prompt.content_region.offset)
        await pilot.mouse_up(offset=prompt.content_region.offset)
        assert screen._pending_selection_history() is not None
        await panel.add_user_message("question appended after release")
        await panel.add_answer_delta("late-answer", "answer appended after release")
        screen.action_copy_text()

        await wait_for(lambda: bool(copied), pilot=pilot)
        lines = copied[0].splitlines()
        assert [line for line in lines if line.startswith("question ")] == [f"question {i}" for i in range(29, 100)]
        assert "answer appended after release" not in lines


@pytest.mark.parametrize("reverse", [False, True], ids=["sidebar-to-status", "status-to-sidebar"])
async def test_drag_between_sidebar_and_status_keeps_chat_history_cold(
    monkeypatch: pytest.MonkeyPatch, reverse: bool
) -> None:
    app = _MainScreenApp()
    copied: list[str] = []
    monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, text: copied.append(text) or True)
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(120))
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        sidebar = screen.query_one("SidebarPanel")
        side = sidebar.region.offset + Offset(0, 1)
        widget, offset = screen.get_widget_and_offset_at(*side)
        assert widget is not None and sidebar in widget.ancestors_with_self and offset is not None
        lower = screen.query_one("#status-text", Static).content_region.offset

        start, end = (lower, side) if reverse else (side, lower)
        await pilot.mouse_down(offset=start)
        await pilot.hover(offset=end)
        assert screen._select_state is not None and screen._select_state.selection_bounds.overlaps(panel.region)
        await pilot.mouse_up(offset=end)
        handled = right_click_copy.copy_screen_selection(screen)

        assert handled is bool(copied)
        assert all("question " not in text for text in copied)
        assert screen._history_selection_preparation is None
        assert not panel.query("#turn-60")


async def test_right_click_in_input_waits_for_the_selected_cold_history(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MainScreenApp()
    copied: list[str] = []
    monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, text: copied.append(text) or True)
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(120))
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        first = await _open_history_turn(pilot, panel, 1)
        editor = screen.query_one("#chat-input", TextArea)
        assert not editor.selected_text
        assert not panel.query("#turn-60")
        start, end = first.content_region.offset, editor.content_region.offset

        entered, release = Event(), Event()
        mount = panel.mount_replay_batch

        async def hold_mount(batch: list[Widget], *, before: Widget | None) -> None:
            if before is not None:
                entered.set()
                await release.wait()
            await mount(batch, before=before)

        monkeypatch.setattr(panel, "mount_replay_batch", hold_mount)
        try:
            await pilot.mouse_down(offset=start)
            await pilot.hover(offset=end)
            await pilot.mouse_up(offset=end)
            await wait_for(entered.is_set, pilot=pilot, description="selection started mounting cold history")
            await pilot.click(offset=end, button=3)
            assert not copied
        finally:
            release.set()

        await wait_for(lambda: bool(copied), pilot=pilot, description="right-click copied the complete selection")
        assert len(copied) == 1
        lines = copied[0].splitlines()
        assert [line for line in lines if line.startswith("question ")] == [f"question {i}" for i in range(120)]
        assert "answer 119" in lines
        assert not screen.selections
