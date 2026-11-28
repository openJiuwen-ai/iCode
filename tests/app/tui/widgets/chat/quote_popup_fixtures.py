# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared main screen harness for transcript drag, copy and quote tests."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.pilot import Pilot

import chrys.app.tui as tui_module
from chrys.app.tui.app import ChrysApp
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.theme import TuiVariableDefaultsMixin
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.events.bus import EventBus
from tests.support.pilot_barrier import screen_is_settled
from tests.support.waiting import wait_for


class _MainScreenApp(TuiVariableDefaultsMixin, App):
    # The app's own stylesheet lays the main screen out as iCode does: the side card gets its share of the column.
    CSS_PATH: ClassVar = [Path(tui_module.__file__).parent / path for path in ChrysApp.CSS_PATH]

    def __init__(self) -> None:
        super().__init__()
        self.main_screen = MainScreen(EventBus(), engine_provider=None)

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self.main_screen)


async def _open_history_turn(pilot: Pilot[None], panel: ChatPanel, turn: int) -> VirtualizedMarkdown:
    panel.scroll_to_turn(f"turn-{turn}")
    await wait_for(
        lambda: (
            bool(panel.query(f"#turn-{turn}"))
            and panel.query_one(f"#turn-{turn}").region.overlaps(panel.content_region)
            and screen_is_settled(pilot.app, pilot.app.screen)
        ),
        pilot=pilot,
    )
    return panel.query_one(f"#turn-{turn}").query_one(VirtualizedMarkdown)
