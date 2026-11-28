# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A running turn shows a breathing dot under the transcript while it waits with nothing on screen moving."""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from textual.geometry import Size
from textual.screen import Screen

from chrys.app.tui.widgets.chat.chrome import ChatBottomSpacer
from chrys.app.tui.widgets.chat.compaction_card import CompactionCard
from chrys.app.tui.widgets.chat.context_fold import ContextFoldWidget
from chrys.app.tui.widgets.chat.live_process_row import LiveProcessRow
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    ErrorMessage,
    SystemMessage,
    ThinkingMessage,
    UserMessage,
)
from chrys.app.tui.widgets.chat.panel import _WAITING_ROW_DELAY_SECONDS, ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.chat.waiting_row import WaitingRow, waits_after
from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_READ
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import ChatPanelApp, click_when_settled
from tests.support.waiting import wait_for, wait_until

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.timer import Timer, TimerCallback
    from textual.widget import Widget


def _compaction(status: str) -> CompactionCard:
    card = CompactionCard("c-1")
    card.status = status
    return card


@pytest.mark.parametrize(
    ("make", "waits"),
    [
        pytest.param(lambda: None, False, id="nothing"),
        pytest.param(lambda: UserMessage("prompt"), True, id="prompt"),
        pytest.param(lambda: SystemMessage("notice"), True, id="notice"),
        pytest.param(lambda: ContextFoldWidget("ctx-1", "Summary"), True, id="context-fold"),
        pytest.param(lambda: ThinkingMessage("Look.", is_final=False), False, id="thinking-streams"),
        pytest.param(lambda: ThinkingMessage("Look."), True, id="thinking-complete"),
        pytest.param(lambda: AgentMessage("Done.", profile_name="Agent"), False, id="answer"),
        pytest.param(lambda: AgentMessage("Next.", is_intermediate=True), True, id="text-between-tool-calls"),
        pytest.param(ToolGroup, False, id="tool-group-running"),
        pytest.param(lambda: ToolGroup(live=False), True, id="tool-group-done"),
        pytest.param(LiveProcessRow, False, id="live-process"),
        pytest.param(lambda: _compaction("running"), False, id="compaction-running"),
        pytest.param(lambda: _compaction("ok"), True, id="compaction-done"),
        pytest.param(lambda: ErrorMessage("failed"), False, id="error"),
    ],
)
def test_a_turn_waits_after_what_says_it_goes_on_and_not_after_what_still_moves_or_says_why(
    make: Callable[[], Widget | None], waits: bool
) -> None:
    assert waits_after(make()) is waits


def _row(panel: ChatPanel) -> WaitingRow:
    return panel.query_one(WaitingRow)


def _hidden_with_none_due(panel: ChatPanel) -> bool:
    return not _row(panel).display and panel._waiting_row_timer is None


def _hold_each_moment(monkeypatch: pytest.MonkeyPatch, panel: ChatPanel) -> list[TimerCallback]:
    """Start each moment the row waits for paused, so it passes only once the test starts it, however slow the run;
    return what each moment's timer was handed to run as it passes.

    Pausing the timer after a step is too late: the moment may have passed while the step ran, and Textual's timer
    hands its callback to the panel as it passes, which then runs it whatever the timer does.
    """
    set_timer = panel.set_timer
    handed: list[TimerCallback] = []

    def held(
        delay: float, callback: TimerCallback | None = None, *, name: str | None = None, pause: bool = False
    ) -> Timer:
        moment = isinstance(callback, partial) and callback.func == panel._show_waiting_row
        if moment:
            handed.append(callback)
        return set_timer(delay, callback, name=name, pause=pause or moment)

    monkeypatch.setattr(panel, "set_timer", held)
    return handed


def _start_the_moment(panel: ChatPanel) -> None:
    timer = panel._waiting_row_timer
    assert timer is not None
    timer.resume()


async def test_the_row_shows_below_the_transcript_a_moment_after_the_turn_last_moved_and_hides_as_it_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        _hold_each_moment(monkeypatch, panel)
        panel.agent_running = True
        await panel.add_user_message("prompt")
        # The moment keeps it from flashing up between one row ending and the next starting.
        assert not _row(panel).display and panel._waiting_row_timer is not None
        _start_the_moment(panel)
        await wait_for(lambda: _row(panel).display, pilot=pilot, description="the waiting row shows")
        prompt = panel.query_one(UserMessage)
        children = list(panel.children)
        assert children.index(_row(panel)) == children.index(prompt) + 1
        assert isinstance(children[children.index(_row(panel)) + 1], ChatBottomSpacer)

        # New output takes its place at once.
        await panel.add_answer_delta("a1", "Let me look.")
        assert _hidden_with_none_due(panel)
        # An answer may end the turn: no row comes after it.
        panel.finish_answer_segment("a1", completed=True)
        assert _hidden_with_none_due(panel)

        await panel.add_tool_start("c1", "read_file", KIND_FILESYSTEM_READ, args={})
        await panel.add_tool_result("c1", "read_file", "ok")
        # The group's title still runs through its tool phase.
        assert _hidden_with_none_due(panel)

        await panel.add_thinking_delta("r1", "reasoning", "The file says yes.")
        assert _hidden_with_none_due(panel)
        panel.finish_thinking_segment("r1")
        await click_when_settled(pilot, panel.query_one(LiveProcessRow))
        _start_the_moment(panel)
        await wait_for(lambda: _row(panel).display, pilot=pilot, description="the row after complete thinking")
        children = list(panel.children)
        assert children.index(_row(panel)) == children.index(panel.query(ThinkingMessage).last()) + 1

        panel.agent_running = False
        assert _hidden_with_none_due(panel)


async def test_a_turn_that_ends_before_the_moment_passes_never_shows_the_row(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        _hold_each_moment(monkeypatch, panel)
        panel.agent_running = True
        await panel.add_user_message("prompt")
        _start_the_moment(panel)
        panel.agent_running = False
        assert _hidden_with_none_due(panel)
        # Past the moment it would have shown at.
        assert not await wait_until(lambda: _row(panel).display, timeout=_WAITING_ROW_DELAY_SECONDS * 2, pilot=pilot)


@pytest.mark.parametrize("member", ["thinking", "tool"])
async def test_waiting_uses_the_last_shown_entry_after_activity_finishes(member: str) -> None:
    async with ChatPanelApp(
        thinking_expanded=lambda: True,
    ).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.agent_running = True
        await panel.add_user_message("prompt")
        if member == "thinking":
            await panel.add_thinking_delta("r1", "reasoning", "Look at the file.")
            panel.finish_thinking_segment("r1")
            last = panel.query_one(ThinkingMessage)
        else:
            await panel.add_tool_start("c1", "read_file", KIND_FILESYSTEM_READ, args={"path": "/tmp/a.py"})
            await panel.add_tool_result("c1", "read_file", "ok")
            panel._end_tool_group()
            last = panel.query_one(ToolGroup)
        process = panel.query_one(LiveProcessRow)
        assert process.collapsed and not last.display
        assert _hidden_with_none_due(panel)
        assert not await wait_until(lambda: _row(panel).display, timeout=_WAITING_ROW_DELAY_SECONDS * 2, pilot=pilot)
        await click_when_settled(pilot, process)
        assert last.display

        await wait_for(
            lambda: _row(panel).display and _row(panel).region.height > 0,
            pilot=pilot,
            description="shown completed activity has a waiting dot",
        )
        assert last.region.bottom <= _row(panel).region.y
        panel.toggle_fold_all()
        assert process.collapsed and _hidden_with_none_due(panel)
        assert not await wait_until(lambda: _row(panel).display, timeout=_WAITING_ROW_DELAY_SECONDS * 2, pilot=pilot)


async def test_a_blank_new_segment_still_leaves_a_waiting_row_after_thinking() -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.agent_running = True
        await panel.add_user_message("prompt")
        await panel.add_thinking_delta("r1", "reasoning", "Thinking aloud.")
        await click_when_settled(pilot, panel.query_one(LiveProcessRow))
        assert not _row(panel).display

        await panel.add_thinking_delta("r2", "reasoning", " ")
        await wait_for(lambda: _row(panel).display, pilot=pilot, description="the previous thought has ended")
        assert len(panel.query(ThinkingMessage)) == 1


async def test_a_moment_that_passed_as_the_turn_moved_on_leaves_the_row_to_the_newer_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its timer hands the panel what shows the row as the moment passes, and stopping the timer then does not take it
    back: the turn may move on before the panel runs it, and wait a newer moment, whose own timer shows the row."""
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        handed = _hold_each_moment(monkeypatch, panel)
        panel.agent_running = True
        await panel.add_user_message("prompt")
        (passed,) = handed
        # What the timer does as its moment passes (MessagePump.set_timer). Before the panel runs it, the turn ends and
        # the next one starts waiting.
        panel.call_next(passed)
        panel.agent_running = False
        panel.agent_running = True
        newer = panel._waiting_row_timer
        assert newer is not None and len(handed) == 2
        ran = asyncio.Event()
        panel.call_next(ran.set)
        await wait_for(ran.is_set, pilot=pilot, description="the panel ran what the passed moment handed it")
        assert not _row(panel).display and panel._waiting_row_timer is newer

        _start_the_moment(panel)
        await wait_for(lambda: _row(panel).display, pilot=pilot, description="the row shows as the newer moment passes")


async def test_the_row_showing_lays_the_transcript_out_once(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        _hold_each_moment(monkeypatch, panel)
        for index in range(20):
            await panel.add_user_message(f"prompt {index}")
        panel.agent_running = True
        await panel.add_user_message("prompt")
        screen = pilot.app.screen
        prompts = list(panel.query(UserMessage))
        await wait_for(
            lambda: (
                all(prompt.query_one(VirtualizedMarkdown).shown_source for prompt in prompts)
                and screen_is_settled(pilot.app, screen)
            ),
            pilot=pilot,
            description="the prompts are laid out",
        )
        # Taller than the view: the transcript's own rows, not the spacer under a short one, give the row its place.
        assert panel.max_scroll_y > 0
        row = _row(panel)
        refresh_layout = screen._refresh_layout
        shown_layouts = 0

        def count_shown_layout(size: Size | None = None, scroll: bool = False) -> None:
            nonlocal shown_layouts
            # The timer can still be waiting when the screen handles a queued layout.
            if row.display:
                shown_layouts += 1
            refresh_layout(size, scroll)

        with patch.object(screen, "_refresh_layout", autospec=True, side_effect=count_shown_layout):
            _start_the_moment(panel)
            await wait_for(
                lambda: row.display and screen_is_settled(pilot.app, screen),
                pilot=pilot,
                description="the row shows",
            )
        # The layout that shows it gives it its rows: Textual's own size update would ask for another.
        assert shown_layouts == 1


async def test_a_turn_that_ends_as_the_row_shows_moves_the_view_in_the_frame_that_hides_the_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Held a frame as the transcript shrinks, its rows would stay blank, and the view move down only after them."""
    async with ChatPanelApp(thinking_expanded=lambda: False).run_test(size=(80, 24)) as pilot:
        app = pilot.app
        panel = app.query_one(ChatPanel)
        for index in range(4):
            await panel.add_user_message(f"message {index}")
            await panel.add_agent_message("\n".join(f"line {line}" for line in range(8)))
        await panel.add_user_message("prompt")
        panel.agent_running = True
        await panel.add_thinking_delta("r1", "reasoning", "Think one.\n\nThink two.")
        panel.finish_thinking_segment("r1")
        thinking = panel.query_one(ThinkingMessage)
        row = _row(panel)
        await click_when_settled(pilot, panel.query_one(LiveProcessRow))
        await wait_for(
            lambda: row.display and panel.scroll_y == panel.max_scroll_y and screen_is_settled(app, panel.screen),
            pilot=pilot,
            description="the row shows at the bottom of a filled view",
        )
        painted: list[tuple[int | None, bool]] = []
        paint = Screen._compositor_refresh

        def record(screen: Screen) -> None:
            visible = screen._compositor.visible_widgets
            painted.append((visible[thinking][0].y if thinking in visible else None, row in visible))
            paint(screen)

        monkeypatch.setattr(Screen, "_compositor_refresh", record)
        panel.agent_running = False
        await wait_for(
            lambda: not row.display and screen_is_settled(app, panel.screen),
            pilot=pilot,
            description="the turn ended",
        )
        assert panel.scroll_y == panel.max_scroll_y
        assert set(painted) == {(thinking.region.y, False)}, painted


async def test_the_row_showing_under_a_tall_prompt_keeps_the_view_on_its_top(monkeypatch: pytest.MonkeyPatch) -> None:
    """The row says the turn goes on; it is no reply, which alone takes the view down from where the prompt begins."""
    async with ChatPanelApp().run_test(size=(80, 20)) as pilot:
        app = pilot.app
        panel = app.query_one(ChatPanel)
        _hold_each_moment(monkeypatch, panel)
        for index in range(8):
            await panel.add_user_message(f"message {index}")
        panel.agent_running = True
        await panel.add_user_message("\n".join(f"line {index}" for index in range(30)))
        prompt = panel.query(UserMessage).last()
        row = _row(panel)
        await wait_for(
            lambda: (
                prompt.outer_size
                and panel.scroll_y == prompt.virtual_region_with_margin.y
                and screen_is_settled(app, panel.screen)
            ),
            pilot=pilot,
            description="the view shows the prompt's top",
        )
        assert panel._anchor_released is True and not row.display

        _start_the_moment(panel)
        await wait_for(
            lambda: row.display and screen_is_settled(app, panel.screen), pilot=pilot, description="the row shows"
        )
        assert panel.scroll_y == prompt.virtual_region_with_margin.y < panel.max_scroll_y
        assert panel._anchor_released is True

        await panel.add_answer_delta("a1", "\n\n".join(f"answer {index}" for index in range(10)))
        await wait_for(
            lambda: (
                panel.scroll_y == panel.max_scroll_y
                and panel._anchor_released is False
                and screen_is_settled(app, panel.screen)
            ),
            pilot=pilot,
            description="the reply takes the view down",
        )


@pytest.mark.parametrize("entry", ["thinking", "compaction"])
async def test_an_entry_in_the_rows_place_under_a_tall_prompt_takes_the_view_down(entry: str) -> None:
    """Folded thinking leaves space underneath as it takes the waiting row's place."""
    async with ChatPanelApp(thinking_expanded=lambda: False).run_test(size=(80, 20)) as pilot:
        app = pilot.app
        panel = app.query_one(ChatPanel)
        for index in range(8):
            await panel.add_user_message(f"message {index}")
        panel.agent_running = True
        await panel.add_user_message("\n".join(f"line {index}" for index in range(30)))
        prompt = panel.query(UserMessage).last()
        row = _row(panel)
        await wait_for(
            lambda: (
                row.display
                and prompt.outer_size
                and panel.scroll_y == prompt.virtual_region_with_margin.y
                and screen_is_settled(app, panel.screen)
            ),
            pilot=pilot,
            description="the row shows under the prompt's top",
        )
        height = panel.virtual_size.height

        reply: Widget
        if entry == "thinking":
            await panel.add_thinking_delta("r1", "reasoning", "Looking at the file.")
            reply = panel.query_one(LiveProcessRow)
        else:
            await panel.add_compaction_start("k1")
            reply = panel.query_one(CompactionCard)
        await wait_for(
            lambda: reply.outer_size and not row.display and screen_is_settled(app, panel.screen),
            pilot=pilot,
            description=f"{entry} shows in the row's place",
        )
        assert panel.virtual_size.height == height + (1 if entry == "thinking" else 0)
        assert panel._anchor_released is False
        assert panel.scroll_y == panel.max_scroll_y > prompt.virtual_region_with_margin.y


async def test_open_activity_waits_after_thinking_that_followed_the_displayed_answer() -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.agent_running = True
        await panel.add_user_message("prompt")
        await panel.add_answer_delta("a1", "Answer already shown.")
        panel.finish_answer_segment("a1", completed=True)
        await panel.add_thinking_delta("r1", "reasoning", "Preparing a tool.")
        title = panel.query_one(LiveProcessRow)
        await click_when_settled(pilot, title)
        panel.finish_thinking_segment("r1")
        await wait_for(
            lambda: _row(panel).display and _row(panel).region.height > 0,
            pilot=pilot,
            description="waiting for the next tool",
        )
        assert panel.mounted_agent_messages()[-1].region.bottom <= _row(panel).region.y
        await click_when_settled(pilot, title)
        assert not _row(panel).display


async def test_waiting_sync_and_live_member_mount_ignore_earlier_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def measure(earlier_turns: int) -> tuple[int, int]:
        async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
            panel = pilot.app.query_one(ChatPanel)
            await panel.mount(
                *(
                    entry
                    for index in range(earlier_turns)
                    for entry in (UserMessage(f"Earlier prompt {index}"), AgentMessage("Done.", profile_name="Agent"))
                )
            )
            panel.agent_running = True
            await panel.add_user_message("Current prompt")
            await panel.add_thinking_delta("r1", "reasoning", "First thought.")
            panel.finish_thinking_segment("r1")
            await panel.add_answer_delta("a1", "Visible answer.")
            await click_when_settled(pilot, panel.query_one(LiveProcessRow))

            display = UserMessage.display
            visits = 0

            def read_display(message: UserMessage) -> bool:
                nonlocal visits
                visits += 1
                return display.__get__(message)

            with monkeypatch.context() as patcher:
                patcher.setattr(UserMessage, "display", property(read_display, display.fset))
                panel._sync_waiting_row()
            with patch.object(panel, "transcript_occurrence", wraps=panel.transcript_occurrence) as occurrence:
                await panel.add_thinking_delta("r2", "reasoning", "Next thought.")

            thought = panel.query(ThinkingMessage).last()
            answer = panel.query(AgentMessage).last()
            await wait_for(
                lambda: thought.region.height > 0 and answer.region.y < thought.region.y,
                pilot=pilot,
                description="the new thought appears below the visible preview",
            )
            assert not _row(panel).display
            return visits, occurrence.call_count

    assert await measure(1) == await measure(64)


async def test_waiting_checks_the_same_number_of_entries_in_short_and_long_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def measure(entries: int) -> int:
        async with ChatPanelApp().run_test() as pilot:
            panel = pilot.app.query_one(ChatPanel)
            await panel.add_user_message("Prompt")
            await panel.mount(*(SystemMessage("Notice.") for _ in range(entries)))
            panel.agent_running = True
            display = SystemMessage.display
            visits = 0

            def read_display(notice: SystemMessage) -> bool:
                nonlocal visits
                visits += 1
                return display.__get__(notice)

            with monkeypatch.context() as patcher:
                patcher.setattr(SystemMessage, "display", property(read_display, display.fset))
                assert panel._turn_waits()
            return visits

    assert await measure(16) == await measure(4096)


@pytest.mark.parametrize("notice", [False, True], ids=["without-notice", "with-notice"])
async def test_folded_activity_suppresses_waiting_after_a_new_thought(notice: bool) -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.agent_running = True
        await panel.add_user_message("Prompt")
        await panel.add_thinking_delta("r1", "reasoning", "First thought.")
        panel.finish_thinking_segment("r1")
        if notice:
            await panel.add_system("A notice.")
            await wait_for(lambda: _row(panel).display, pilot=pilot, description="waiting after the notice")

        await panel.add_thinking_delta("r2", "reasoning", "Next thought.")
        panel.finish_thinking_segment("r2")
        await pilot.pause()
        assert panel.query_one(LiveProcessRow).display
        assert not panel.query(ThinkingMessage).last().display
        assert not _row(panel).display
