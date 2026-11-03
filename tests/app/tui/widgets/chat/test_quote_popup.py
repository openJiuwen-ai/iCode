# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The Quote pill at a transcript drag's end, the label it puts in the draft, and quotes in a sent bubble."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.events import MouseDown, MouseUp
from textual.geometry import Offset
from textual.pilot import Pilot
from textual.selection import SelectEnd, SelectStart, SelectState

from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.chat.messages import UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.quote_popup import QuoteSelectionPopup
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.events.bus import EventBus
from chrys.foundation.text.draft_references import (
    EMPTY_REFERENCES,
    DraftReference,
    expand_references,
)
from tests.support.tui_helpers import rich_plain
from tests.support.waiting import wait_for


class _MainScreenApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.main_screen = MainScreen(EventBus(), engine_provider=None)

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self.main_screen)


_LINE_START = Offset(0, 0)


def _chat_drag_state(
    start: VirtualizedMarkdown, end: VirtualizedMarkdown, *, end_offset: Offset, start_offset: Offset = _LINE_START
) -> SelectState:
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
            content_offset=start_offset,
        ),
    ).update_end(
        end.region.offset + end_offset,
        SelectEnd(end.parent, end, end_offset),
    )


async def _panel_with_agent_message(app: _MainScreenApp, pilot: Pilot[None], text: str) -> VirtualizedMarkdown:
    panel = app.main_screen.query_one(ChatPanel)
    await panel.add_agent_message(text)
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
    # Shown where its drag ended: the screen's arrange pins it to its target cells.
    await wait_for(
        lambda: popup.is_shown and popup.target_region is not None and popup.region == popup.target_region,
        pilot=None,
        description="the quote pill is shown at the drag's end",
    )
    return popup


def _post_mouse(
    app: _MainScreenApp, event_type: type[MouseDown] | type[MouseUp], x: int, y: int, *, button: int = 1
) -> None:
    widget = app.main_screen.query_one(QuoteSelectionPopup)
    event = event_type(
        widget,
        x=x,
        y=y,
        delta_x=0,
        delta_y=0,
        button=button,
        shift=False,
        meta=False,
        ctrl=False,
        screen_x=x,
        screen_y=y,
    )
    app.post_message(event)


async def _click_pill(app: _MainScreenApp, pilot: Pilot[None], popup: QuoteSelectionPopup) -> None:
    click_x = popup.region.x + 1
    click_y = popup.region.y
    _post_mouse(app, MouseDown, click_x, click_y)
    await pilot.pause()
    _post_mouse(app, MouseUp, click_x, click_y)
    await pilot.pause()
    await wait_for(lambda: not popup.is_shown, pilot=pilot, description="the quote pill withdraws after its click")


@pytest.mark.asyncio
async def test_clicking_the_pill_puts_a_quote_label_for_the_selection_into_the_draft() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 24)) as pilot:
        text = "quotable agent answer"
        body = await _panel_with_agent_message(app, pilot, text)
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.value = "explain"
        popup = await _finish_drag_over(app, body, text)
        assert app.main_screen.get_selected_text() == text

        await _click_pill(app, pilot, popup)

        assert input_bar.value == "explain [Quote #1] "
        assert input_bar.draft().references.get("quote", 1) == DraftReference("quote", 1, text)
        assert input_bar.draft_has_focus
        assert app.main_screen.get_selected_text() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("button", [2, 3])
async def test_only_the_left_button_quotes_from_the_pill(button: int) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 24)) as pilot:
        text = "quotable agent answer"
        body = await _panel_with_agent_message(app, pilot, text)
        input_bar = app.main_screen.query_one(InputBar)
        popup = await _finish_drag_over(app, body, text)

        _post_mouse(app, MouseDown, popup.region.x + 1, popup.region.y, button=button)
        await pilot.pause()

        assert input_bar.value == ""
        assert not input_bar.draft().references


@pytest.mark.asyncio
async def test_sliding_off_the_pressed_pill_leaves_it_withdrawn_and_the_next_drag_places_it() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 24)) as pilot:
        text = "quotable agent answer"
        body = await _panel_with_agent_message(app, pilot, text)
        input_bar = app.main_screen.query_one(InputBar)
        popup = await _finish_drag_over(app, body, text)

        _post_mouse(app, MouseDown, popup.region.x + 1, popup.region.y)
        await pilot.pause()
        _post_mouse(app, MouseUp, 0, 0)
        await wait_for(lambda: not popup.is_shown, pilot=pilot, description="the pressed pill withdrew")
        assert input_bar.value == "[Quote #1] "

        popup = await _finish_drag_over(app, body, text)
        app.main_screen.clear_selection()
        assert not popup.is_shown


@pytest.mark.asyncio
async def test_a_drag_over_part_of_a_message_quotes_just_that_part() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 24)) as pilot:
        text = "the quick brown fox jumps"
        body = await _panel_with_agent_message(app, pilot, text)
        input_bar = app.main_screen.query_one(InputBar)
        app.main_screen._watch__select_state(
            _chat_drag_state(body, body, start_offset=Offset(4, 0), end_offset=Offset(15, 0))
        )
        app.main_screen._watch__selecting(False)
        popup = app.main_screen.query_one(QuoteSelectionPopup)
        await wait_for(lambda: popup.is_shown and popup.region.width > 0, pilot=None, description="quote pill shown")

        await _click_pill(app, pilot, popup)

        assert input_bar.draft().references.get("quote", 1) == DraftReference("quote", 1, "quick brown")


@pytest.mark.asyncio
async def test_the_pill_stays_inside_the_transcript_at_its_right_edge() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(80, 24)) as pilot:
        text = "an answer wide enough to reach the transcript's right edge when selected"
        body = await _panel_with_agent_message(app, pilot, text)
        panel = app.main_screen.query_one(ChatPanel)
        app.main_screen._watch__select_state(_chat_drag_state(body, body, end_offset=Offset(body.region.width, 0)))
        app.main_screen._watch__selecting(False)
        popup = app.main_screen.query_one(QuoteSelectionPopup)
        await wait_for(
            lambda: popup.is_shown and popup.region == popup.target_region, pilot=pilot, description="quote pill placed"
        )

        assert panel.content_region.contains_region(popup.region)


@pytest.mark.asyncio
async def test_a_locked_draft_refuses_the_quote_and_says_so() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 24)) as pilot:
        text = "an answer"
        body = await _panel_with_agent_message(app, pilot, text)
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.value = "queued note"
        input_bar.lock_with_text()
        await pilot.pause()
        popup = await _finish_drag_over(app, body, text)
        notifications_before = len(app._notifications)

        await _click_pill(app, pilot, popup)

        assert input_bar.value == "queued note"
        assert len(app._notifications) == notifications_before + 1


def test_the_pill_reads_quote_alone() -> None:
    assert rich_plain(QuoteSelectionPopup()._render_label(None)) == "Quote"


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
async def test_a_sent_quote_reads_in_the_bubble_as_the_text_the_model_got() -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        references, _entry = EMPTY_REFERENCES.with_new("quote", "quoted line\n# not a heading")
        sent = expand_references("explain [Quote #1] please", references)
        panel = app.main_screen.query_one(ChatPanel)
        await panel.add_user_message(sent)
        await pilot.pause()

        message = app.main_screen.query_one(UserMessage)
        body = message.query_one(VirtualizedMarkdown)
        await wait_for(lambda: body.region.height >= 1, pilot=pilot, description="the user message body has rendered")

        assert sent == "explain \n> quoted line\n> # not a heading\n\n please"
        assert body.source == sent
        assert panel.get_user_messages() == [("You", sent)]
