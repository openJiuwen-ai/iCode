# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Harness tests for the quote pill, the input chip, and quote bubble rendering."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.events import MouseDown, MouseUp
from textual.geometry import Offset
from textual.pilot import Pilot
from textual.selection import SelectEnd, SelectStart, SelectState
from textual.widgets import Static

from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.chat.messages import UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.quote_format import (
    MAX_PENDING_QUOTES,
    SOURCE_ASSISTANT,
    Quote,
    compose_outgoing_text,
)
from chrys.app.tui.widgets.chat.quote_popup import QuoteSelectionPopup
from chrys.app.tui.widgets.chrome.input_bar import InputBar, QuoteChip
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.events.bus import EventBus
from tests.support.waiting import wait_for


class _MainScreenApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.main_screen = MainScreen(EventBus(), engine_provider=None)

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self.main_screen)


def _chat_drag_state(start: VirtualizedMarkdown, end: VirtualizedMarkdown, *, end_offset: Offset) -> SelectState:
    assert start.parent is not None
    assert end.parent is not None
    return SelectState(
        start.region.offset,
        start=SelectStart(
            start.parent,
            start.region.offset - start.parent.region.offset,
            start.parent.region.offset,
            start.parent.scroll_offset,
            content_widget=start,
            content_offset=Offset(0, 0),
        ),
    ).update_end(
        end.region.offset + end_offset,
        SelectEnd(end.parent, end, end_offset),
    )


async def _panel_with_agent_message(app: _MainScreenApp, pilot: Pilot[None], text: str) -> VirtualizedMarkdown:
    panel = app.main_screen.query_one(ChatPanel)
    await panel.add_agent_message(text, is_final=True)
    await pilot.pause()
    body = app.main_screen.query_one(VirtualizedMarkdown)
    await wait_for(
        lambda: body.region.height >= 1,
        pilot=pilot,
        description="the agent message body has rendered",
    )
    return body


async def _finish_drag_over(app: _MainScreenApp, body: VirtualizedMarkdown, text: str) -> QuoteSelectionPopup:
    """Drive a chat drag across *body* and end it, returning the quote pill."""
    app.main_screen._watch__select_state(_chat_drag_state(body, body, end_offset=Offset(len(text), 0)))
    app.main_screen._watch__selecting(False)
    popup = app.main_screen.query_one(QuoteSelectionPopup)
    await wait_for(
        lambda: popup.is_shown and popup.region.width > 0,
        pilot=None,
        description="the quote pill is shown after the drag ends",
    )
    return popup


def _post_mouse(app: _MainScreenApp, event_type: type[MouseDown] | type[MouseUp], x: int, y: int) -> None:
    widget = app.main_screen.query_one(QuoteSelectionPopup)
    event = event_type(
        widget,
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
    )
    app.post_message(event)


@pytest.mark.asyncio
async def test_finished_drag_shows_quote_pill_and_click_quotes_the_selection() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 24)) as pilot:
        text = "quotable agent answer"
        body = await _panel_with_agent_message(app, pilot, text)
        popup = await _finish_drag_over(app, body, text)

        assert popup.is_shown
        assert app.main_screen.get_selected_text() == text
        click_x = popup.region.x + 1
        click_y = popup.region.y
        _post_mouse(app, MouseDown, click_x, click_y)
        # The in-place mouse-up clears the selection first (stock Textual);
        # the pressed pill must survive it so its click still lands.
        await pilot.pause()
        assert popup.pressed
        _post_mouse(app, MouseUp, click_x, click_y)
        await pilot.pause()
        await wait_for(
            lambda: not popup.is_shown,
            pilot=pilot,
            description="the quote pill withdraws after quoting",
        )

        quotes = app.main_screen._quotes.quotes
        assert quotes == (Quote(text=text, source=SOURCE_ASSISTANT),)
        assert app.main_screen.get_selected_text() is None
        chip = app.main_screen.query_one(InputBar).query_one(QuoteChip)
        assert chip.display is True
        assert chip.count == 1


@pytest.mark.asyncio
async def test_new_drag_and_scroll_withdraw_the_quote_pill() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 24)) as pilot:
        text = "some longer agent answer that stays put"
        body = await _panel_with_agent_message(app, pilot, text)
        popup = await _finish_drag_over(app, body, text)
        assert popup.is_shown

        # A new drag starting hides the pill again.
        app.main_screen._watch__selecting(True)
        assert not popup.is_shown

        popup = await _finish_drag_over(app, body, text)
        assert popup.is_shown
        # Any transcript scroll or relayout withdraws it too.
        app.main_screen.schedule_chat_selection_reprojection(app.main_screen.query_one(ChatPanel))
        assert not popup.is_shown


@pytest.mark.asyncio
async def test_quote_click_beyond_limits_notifies_and_keeps_the_buffer() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 24)) as pilot:
        for index in range(MAX_PENDING_QUOTES):
            assert app.main_screen._quotes.add(f"existing {index}", None)
        text = "one quote too many"
        body = await _panel_with_agent_message(app, pilot, text)
        popup = await _finish_drag_over(app, body, text)

        notifications_before = len(app._notifications)
        click_x = popup.region.x + 1
        click_y = popup.region.y
        _post_mouse(app, MouseDown, click_x, click_y)
        await pilot.pause()
        _post_mouse(app, MouseUp, click_x, click_y)
        await wait_for(
            lambda: not popup.is_shown,
            pilot=pilot,
            description="the quote pill withdraws after the rejected add",
        )

        assert len(app.main_screen._quotes) == MAX_PENDING_QUOTES
        assert len(app._notifications) == notifications_before + 1


@pytest.mark.asyncio
async def test_session_change_clears_pending_quotes_and_chip() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 24)) as pilot:
        assert app.main_screen._quotes.add("carried quote", None)
        input_bar = app.main_screen.query_one(InputBar)
        chip = input_bar.query_one(QuoteChip)
        assert chip.display is True

        app.main_screen.chat_session_id = "another-session"
        await pilot.pause()

        assert not app.main_screen._quotes.quotes
        assert chip.display is False


@pytest.mark.asyncio
async def test_set_pending_quotes_drives_the_chip_label() -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        input_bar = app.main_screen.query_one(InputBar)
        chip = input_bar.query_one(QuoteChip)

        assert chip.display is False
        input_bar.set_pending_quotes(2)
        await pilot.pause()
        assert chip.display is True
        assert chip.count == 2
        input_bar.set_pending_quotes(2)
        assert chip.count == 2
        input_bar.set_pending_quotes(0)
        await pilot.pause()
        assert chip.display is False


@pytest.mark.asyncio
async def test_user_message_with_quote_block_renders_quote_view_and_copies_raw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        wire = compose_outgoing_text("explain please", [Quote(text="quoted line", source=SOURCE_ASSISTANT)])
        panel = app.main_screen.query_one(ChatPanel)
        await panel.add_user_message(wire)
        await pilot.pause()

        message = app.main_screen.query_one(UserMessage)
        body = message.query_one(VirtualizedMarkdown)
        await wait_for(
            lambda: body.region.height >= 1,
            pilot=pilot,
            description="the user message body has rendered",
        )

        # The bubble shows a quoted-conversation view, never the wire block.
        assert "chrys-conversation-selections" not in body.source
        assert "> **Assistant message**" in body.source
        assert "explain please" in body.source

        # Copy keeps the raw message exactly as sent.
        message.copy_user_message()
        assert app.clipboard == wire
        assert copied[-1] == wire


@pytest.mark.asyncio
async def test_plain_user_message_unaffected_by_quote_view() -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        panel = app.main_screen.query_one(ChatPanel)
        await panel.add_user_message("plain message")
        await pilot.pause()

        body = app.main_screen.query_one(UserMessage).query_one(VirtualizedMarkdown)
        await wait_for(
            lambda: body.region.height >= 1,
            pilot=pilot,
            description="the user message body has rendered",
        )
        assert body.source == "plain message"
        assert app.main_screen.query_one(UserMessage).text == "plain message"


def test_quote_chip_is_not_selectable_chrome() -> None:
    assert QuoteChip.ALLOW_SELECT is False
    assert QuoteSelectionPopup.ALLOW_SELECT is False
    assert issubclass(QuoteChip, Static)
