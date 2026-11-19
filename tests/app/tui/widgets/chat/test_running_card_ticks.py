# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Running tool cards tick in a populated main screen without laying the transcript out.

Shown cards repaint their spinner and elapsed labels in place; cards outside the visible cut skip
the repaint, which would make the next geometry read rebuild the compositor map for the whole
transcript, and paint their current state on the first tick after they show.

The cards' clocks are frozen so each simulated second changes their elapsed labels exactly once;
the cards' own timers do the ticking.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import ModuleType
from unittest.mock import create_autospec

import pytest
from rich.text import Text
from textual.app import App
from textual.content import Content
from textual.geometry import Size
from textual.widgets import Static

from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.chat import compaction_card as compaction_card_module
from chrys.app.tui.widgets.chat.compaction_card import CompactionCard
from chrys.app.tui.widgets.chat.live_process_row import LiveProcessRow
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.renderers import execute as execute_module
from chrys.app.tui.widgets.chat.renderers import sub_agent as sub_agent_module
from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_READ, KIND_SHELL, KIND_SUB_AGENT
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for

pytestmark = pytest.mark.asyncio

_SIZE = (120, 50)
_TAIL = [f"test_case_{index} PASSED" for index in range(12)]


class _Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def monotonic(self) -> float:
        return self.now


def _freeze_card_clocks(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    for module in (execute_module, sub_agent_module, compaction_card_module):
        shadow = ModuleType("time")
        shadow.__dict__.update(vars(time))
        shadow.monotonic = clock.monotonic
        monkeypatch.setattr(module, "time", shadow)
    return clock


async def _populate(panel: ChatPanel) -> None:
    for index in range(12):
        await panel.add_user_message(f"Question {index}\nwith a second line")
        await panel.add_tool_start(
            f"read-{index}", "read_file", KIND_FILESYSTEM_READ, args={"path": f"src/mod_{index}.py"}
        )
        await panel.add_tool_result(f"read-{index}", "read_file", "1: line\n2: line\n", 12)
        await panel.add_agent_message(f"## Answer {index}\n\nA paragraph with `code` and **emphasis**.")
    await panel.add_user_message("Investigate everything in parallel.")


async def _start_live_cards(
    panel: ChatPanel, *, tail: list[str] = _TAIL
) -> tuple[SubAgentToolCall, ExecuteToolCall, ToolGroup]:
    await panel.add_tool_start(
        "agent", "explore_agent", KIND_SUB_AGENT, args={"prompt": "Map the modules.", "profile": "Explore"}
    )
    await panel.add_tool_start("shell", "shell", KIND_SHELL, args={"command": "make test"})
    panel.update_tool_progress("shell", tail)
    group = panel.query(ToolGroup).last()
    agent, shell = group.get_tool("agent"), group.get_tool("shell")
    assert isinstance(agent, SubAgentToolCall) and isinstance(shell, ExecuteToolCall)
    return agent, shell, group


def _toggle_live_process(panel: ChatPanel) -> None:
    panel.on_live_process_row_toggle_requested(LiveProcessRow.ToggleRequested(panel.query_one(LiveProcessRow)))


def _plain(widget: Static) -> str:
    content = widget.content
    return content.plain if isinstance(content, Text | Content) else str(content)


def _agent_label(card: SubAgentToolCall) -> str:
    return _plain(card.query_one("#sa-label", Static))


def _shell_label(card: ExecuteToolCall) -> str:
    return _plain(card.query_one("#exec-label", Static))


def _frame_text(app: App[None]) -> str:
    return "\n".join(strip.text for strip in app.screen._compositor.render_strips())


async def _settle(app: App[None], screen: MainScreen) -> None:
    """Wait until nothing reaches *screen*'s layout and its queued repaints have been flushed.

    ``screen_is_settled`` does not cover a repaint queued for the screen's next frame. When that
    repaint includes a widget outside the visible cut (a card in a collapsed group), the frame
    marks the full map stale again, and the next geometry read rebuilds it inside the window a
    test watches.
    """
    await wait_for(
        lambda: screen_is_settled(app, screen) and not (screen._repaint_required or screen._dirty_widgets),
        description="main screen settled with its queued repaints flushed",
    )


async def _tick_to(second: int, agent: SubAgentToolCall, shell: ExecuteToolCall) -> None:
    await wait_for(
        lambda: _agent_label(agent).endswith(f"({second}s)") and f"({second}s •" in _shell_label(shell),
        description=f"cards ticked to {second}s",
    )


async def test_shown_running_cards_tick_without_laying_out_a_populated_main_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _freeze_card_clocks(monkeypatch)
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=_SIZE):
        main = app.screen
        assert isinstance(main, MainScreen)
        panel = main.query_one(ChatPanel)
        await _populate(panel)
        agent, shell, group = await _start_live_cards(panel)
        _toggle_live_process(panel)
        group.collapsed = False
        await _settle(app, main)
        layout = create_autospec(main._refresh_layout, side_effect=main._refresh_layout)
        monkeypatch.setattr(main, "_refresh_layout", layout)

        for second in (1, 2, 3):
            clock.now += 1.0
            panel.update_tool_progress("shell", [f"tick {second} PASSED"])
            await _tick_to(second, agent, shell)
        tail = shell.query_one("#exec-panel", Static)
        painted_tail = tail.content
        clock.now += 1.0
        await _tick_to(4, agent, shell)
        await _settle(app, main)

        layout.assert_not_called()
        # Ticks animate the spinner and the elapsed label; only new output repaints the tail.
        assert tail.content is painted_tail
        assert _plain(tail).splitlines()[-1] == "tick 3 PASSED"
        frame = _frame_text(app)
        assert "SubAgent (4s)" in frame
        assert "(4s •" in frame
        assert "tick 3 PASSED" in frame


async def test_running_cards_in_a_collapsed_group_skip_their_repaints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _freeze_card_clocks(monkeypatch)
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=_SIZE):
        main = app.screen
        assert isinstance(main, MainScreen)
        panel = main.query_one(ChatPanel)
        await _populate(panel)
        agent, shell, group = await _start_live_cards(panel)
        assert group.collapsed
        await _settle(app, main)
        labels = {
            label: label.content
            for label in (agent.query_one("#sa-label", Static), shell.query_one("#exec-label", Static))
        }
        layout = create_autospec(main._refresh_layout, side_effect=main._refresh_layout)
        monkeypatch.setattr(main, "_refresh_layout", layout)
        compositor = main._compositor
        # The hidden shell card's streamed output painted during setup; unless a shown widget
        # repainted since, the full map rebuild that paint owes is still pending, and the
        # window's first geometry read would pay it. Settle it before watching the ticks.
        _ = compositor.full_map
        assert not compositor._full_map_invalidated
        arrange = create_autospec(compositor._arrange_root, side_effect=compositor._arrange_root)
        monkeypatch.setattr(compositor, "_arrange_root", arrange)

        clock.now += 3.0
        for _ in range(3):
            frames = (agent._spin_idx, shell._spin_idx)
            await wait_for(
                lambda frames=frames: agent._spin_idx != frames[0] and shell._spin_idx != frames[1],
                description="both cards ticked",
            )
        await _settle(app, main)

        layout.assert_not_called()
        assert [call for call in arrange.call_args_list if call.kwargs.get("visible_only") is False] == []
        for label, content in labels.items():
            assert label.content is content


async def test_running_cards_ticked_while_collapsed_show_current_text_when_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _freeze_card_clocks(monkeypatch)
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=_SIZE):
        main = app.screen
        assert isinstance(main, MainScreen)
        panel = main.query_one(ChatPanel)
        await _populate(panel)
        agent, shell, group = await _start_live_cards(panel, tail=["initial shell output"])
        assert group.collapsed
        _toggle_live_process(panel)
        group.collapsed = False
        await _settle(app, main)
        tail = shell.query_one("#exec-panel", Static)
        assert tail.outer_size.height == 1 + tail.styles.gutter.height
        assert "initial shell output" in _frame_text(app)

        _toggle_live_process(panel)
        assert group.collapsed and group.process_hidden
        await _settle(app, main)
        layout_heights: list[int] = []
        refresh_layout = main._refresh_layout

        def record_reveal(size: Size | None = None, scroll: bool = False) -> None:
            refresh_layout(size, scroll)
            if group.display and not group.collapsed and not scroll:
                layout_heights.append(tail.outer_size.height)

        layout = create_autospec(refresh_layout, side_effect=record_reveal)
        monkeypatch.setattr(main, "_refresh_layout", layout)

        clock.now += 5.0
        panel.update_tool_progress("shell", [f"hidden line {index}" for index in range(3)])
        frame_before = agent._spin_idx
        await wait_for(lambda: agent._spin_idx != frame_before, description="collapsed card ticked")
        await _settle(app, main)
        layout.assert_not_called()

        _toggle_live_process(panel)
        group.collapsed = False
        await _settle(app, main)

        # The same layout that reveals the card measures the output which arrived while it was hidden.
        expected_height = 4 + tail.styles.gutter.height
        assert layout_heights and layout_heights[0] == expected_height
        assert tail.outer_size.height == expected_height
        assert "hidden line 2" in _frame_text(app)
        # The first tick after the cards show paints the elapsed time they reached while hidden.
        await wait_for(
            lambda: "SubAgent (5s)" in _frame_text(app) and "(5s •" in _frame_text(app),
            description="shown cards paint their current elapsed labels",
        )


async def test_compaction_elapsed_label_that_widens_still_gets_its_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _freeze_card_clocks(monkeypatch)
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=_SIZE):
        main = app.screen
        assert isinstance(main, MainScreen)
        panel = main.query_one(ChatPanel)
        await _populate(panel)
        await panel.add_compaction_start("compaction")
        label = panel.query_one(CompactionCard).query_one("#compaction-label", Static)
        clock.now += 9.0
        await wait_for(lambda: _plain(label).endswith("(9s)"), description="label shows 9s")
        await _settle(app, main)
        width = label.outer_size.width
        layout = create_autospec(main._refresh_layout, side_effect=main._refresh_layout)
        monkeypatch.setattr(main, "_refresh_layout", layout)

        clock.now += 1.0
        await wait_for(lambda: _plain(label).endswith("(10s)"), description="label shows 10s")
        await _settle(app, main)

        assert layout.call_count >= 1
        assert label.outer_size.width == width + 1
        assert "Compacting conversation... (10s)" in _frame_text(app)
