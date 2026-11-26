# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Characterization tests for ChatPanel's temporary migration ports."""

from __future__ import annotations

import pytest
from rich.text import Text
from textual.app import App, ComposeResult

from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.welcome import WelcomeWidget
from chrys.foundation.util.session_ids import session_short_id


class _PanelApp(App):
    def compose(self) -> ComposeResult:
        yield ChatPanel()


@pytest.mark.asyncio
async def test_clearing_the_session_id_removes_the_rendered_border_title() -> None:
    async with _PanelApp().run_test() as pilot:
        screen = pilot.app.screen
        panel = screen.query_one(ChatPanel)
        session_id = "0123456789abcdef"
        short_id = session_short_id(session_id)
        panel.set_session_id(session_id)
        panel.set_session_title("Original first prompt")
        await pilot.pause()
        border = screen._compositor.render_strips()[panel.region.y].text
        assert f"{short_id} · Original first prompt" in border

        panel.set_session_id("")
        await pilot.pause()

        border = screen._compositor.render_strips()[panel.region.y].text
        assert short_id not in border
        assert "Original first prompt" not in border


@pytest.mark.asyncio
async def test_chat_panel_transcript_ui_adapter_matches_current_contract() -> None:
    async with _PanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await pilot.pause()

        assert panel.query_one(WelcomeWidget) is not None
        panel._auto_scroll_paused_by_user = True
        panel._final_response_started = True

        panel.on_replay_started()

        assert panel._auto_scroll_paused_by_user is False
        assert panel._final_response_started is False
        assert panel.query_one(WelcomeWidget) is not None

        await panel.dismiss_welcome_for_content()
        await pilot.pause()

        assert not list(panel.query(WelcomeWidget))

        panel._anchor_released = True
        panel._auto_scroll_paused_by_user = True
        panel.on_status_message_mounted()

        assert panel._auto_scroll_paused_by_user is False
        assert panel._anchor_released is False

        panel.set_border_title(Text("Session: test"))
        panel.set_border_subtitle(Text("/tmp/project"))

        assert str(panel.border_title) == "Session: test"
        assert str(panel.border_subtitle) == "/tmp/project"
        assert panel.has_border_subtitle() is True
        assert panel.border_region() == panel.region

        assert panel.transcript_content_children() == []
