# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The Quote pill at a drag's end in the chat or the side question card, the label it puts in the draft, and quotes in a sent bubble."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from textual.app import App, ComposeResult
from textual.events import MouseDown, MouseUp
from textual.geometry import Offset
from textual.pilot import Pilot
from textual.selection import SELECT_ALL, SelectEnd, Selection, SelectStart, SelectState
from textual.widgets import Static

import chrys.app.tui as tui_module
from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.app import ChrysApp
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.theme import TuiVariableDefaultsMixin
from chrys.app.tui.widgets.btw import BtwAnswer, BtwPage, BtwPanel, BtwQuestion
from chrys.app.tui.widgets.chat.messages import MessageCopyButton, UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.quote_popup import QuoteSelectionPopup
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.side_questions import SideQuestionEntry
from chrys.foundation.text.draft_references import (
    EMPTY_REFERENCES,
    DraftReference,
    expand_references,
)
from tests.support.pilot_barrier import screen_is_settled
from tests.support.toasts import shown_toasts
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import rich_plain
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
        notifications_before = len(shown_toasts(app))

        await _click_pill(app, pilot, popup)

        assert input_bar.value == "queued note"
        assert len(shown_toasts(app)) == notifications_before + 1


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


async def _side_card(app: _MainScreenApp, pilot: Pilot[None], question: str, answer: str) -> BtwPanel:
    """A side question card showing *question* and its *answer*, both rendered."""
    card = app.main_screen.query_one(BtwPanel)
    card.begin("side", question)
    await card.finish("side", answer)
    await wait_for(
        lambda: (
            screen_is_settled(app, app.main_screen)
            and all(body.region.height >= 1 for body in card.query(VirtualizedMarkdown))
        ),
        pilot=pilot,
        description="the side question and answer have rendered and settled",
    )
    return card


def _side_body(card: BtwPanel, part: str) -> VirtualizedMarkdown:
    return card.query_one(BtwAnswer if part == "answer" else BtwQuestion).query_one(VirtualizedMarkdown)


@pytest.mark.asyncio
@pytest.mark.parametrize("part", ["question", "answer"])
async def test_a_drag_in_the_side_card_quotes_it_from_inside_the_card(part: str) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 30)) as pilot:
        question, answer = "why retry at all", "a bounded retry keeps failures visible"
        card = await _side_card(app, pilot, question, answer)
        text = question if part == "question" else answer
        input_bar = app.main_screen.query_one(InputBar)

        popup = await _finish_drag_over(app, _side_body(card, part), text)

        assert card.query_one(BtwPage).content_region.contains_region(popup.region)
        await _click_pill(app, pilot, popup)
        assert input_bar.value == "[Quote #1] "
        assert input_bar.draft().references.get("quote", 1) == DraftReference("quote", 1, text)
        assert card.display


@pytest.mark.asyncio
@pytest.mark.parametrize("from_chat", [True, False])
async def test_a_drag_from_one_transcript_into_the_other_offers_no_quote(from_chat: bool) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 30)) as pilot:
        chat_body = await _panel_with_agent_message(app, pilot, "the chat's answer")
        card = await _side_card(app, pilot, "a side question", "the side answer")
        side_body = _side_body(card, "answer")
        start, end = (chat_body, side_body) if from_chat else (side_body, chat_body)

        app.main_screen._watch__select_state(_chat_drag_state(start, end, end_offset=Offset(4, 0)))
        app.main_screen._watch__selecting(False)
        await pilot.pause()

        assert not app.main_screen.query_one(QuoteSelectionPopup).is_shown


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["chat", "card"])
async def test_a_drag_from_a_transcript_out_onto_the_sidebar_still_quotes_it(source: str) -> None:
    """The sidebar's own text rides along, as it did before the card could be quoted."""
    app = _MainScreenApp()
    async with app.run_test(size=(100, 30)) as pilot:
        text = "the chat's answer"
        body = await _panel_with_agent_message(app, pilot, text)
        if source == "card":
            text = "the side answer"
            body = _side_body(await _side_card(app, pilot, "a side question", text), "answer")
        label = app.main_screen.query_one("#ctx-usage-label", Static)

        app.main_screen._watch__select_state(_chat_drag_state(body, body, end_offset=Offset(4, 0)))
        # What the drag picked up on its way out: the transcript's text, and the sidebar label it ended on.
        app.main_screen.selections = {body: SELECT_ALL, label: SELECT_ALL}
        app.main_screen._watch__selecting(False)

        popup = app.main_screen.query_one(QuoteSelectionPopup)
        await wait_for(lambda: popup.is_shown, pilot=pilot, description="the quote pill is offered")
        assert app.main_screen.get_selected_text() == f"{text}\nContext Usage"


@pytest.mark.asyncio
@pytest.mark.parametrize("onto", ["counter", "chat"])
async def test_a_drag_from_the_side_card_onto_other_text_of_the_two_transcripts_offers_no_quote(onto: str) -> None:
    """A selection that reaches the card's own counter, or the chat, would mix that text into the quote."""
    app = _MainScreenApp()
    async with app.run_test(size=(100, 30)) as pilot:
        chat_body = await _panel_with_agent_message(app, pilot, "the chat's answer")
        card = await _side_card(app, pilot, "a side question", "the side answer")
        card.set_history_position(0, 1, "2026-11-10T10:00:00+00:00")
        counter = card.query_one("#btw-position", Static)
        await wait_for(lambda: counter.region.width > 0, pilot=pilot, description="the counter shows")
        body = _side_body(card, "answer")
        other, other_text = (counter, "1/1") if onto == "counter" else (chat_body, "the chat's answer")

        app.main_screen._watch__select_state(_chat_drag_state(body, body, end_offset=Offset(4, 0)))
        # What the drag picked up on its way out of the page: the answer, and the text it ended on.
        app.main_screen.selections = {body: SELECT_ALL, other: SELECT_ALL}
        app.main_screen._watch__selecting(False)
        await pilot.pause()

        assert other_text in (app.main_screen.get_selected_text() or "")
        assert not app.main_screen.query_one(QuoteSelectionPopup).is_shown


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["chat", "card"])
async def test_a_scrollbar_press_still_lets_the_scroll_withdraw_the_pill(source: str) -> None:
    """Pressing a scrollbar ends no drag but forgets it; the scroll that follows still moves the text away."""
    app = _MainScreenApp()
    async with app.run_test(size=(100, 30)) as pilot:
        text = "the chat's answer"
        body = await _panel_with_agent_message(app, pilot, text)
        if source == "card":
            text = "the side answer"
            card = await _side_card(app, pilot, "a side question", text)
            body = _side_body(card, "answer")
        popup = await _finish_drag_over(app, body, text)

        # What Textual does on a press that starts no selection, the scrollbar's included.
        app.main_screen._watch__select_state(None)
        if source == "chat":
            app.main_screen.schedule_chat_selection_reprojection(app.main_screen.query_one(ChatPanel))
        else:
            card.post_message(BtwPanel.Scrolled())

        await wait_for(lambda: not popup.is_shown, pilot=pilot, description="the scroll withdrew the pill")


@pytest.mark.asyncio
async def test_scrolling_the_side_card_withdraws_the_pill_and_keeps_the_selection() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 30)) as pilot:
        answer = "\n\n".join(f"paragraph {index}" for index in range(40))
        card = await _side_card(app, pilot, "a long answer?", answer)
        page = card.query_one(BtwPage)
        await wait_for(lambda: page.max_scroll_y > page.scroll_y + 3, pilot=pilot, description="the answer overflows")
        popup = await _finish_drag_over(app, _side_body(card, "question"), "a long answer?")

        page.scroll_to(y=page.scroll_y + 3, animate=False)

        await wait_for(lambda: not popup.is_shown, pilot=pilot, description="the scroll withdrew the pill")
        assert app.main_screen.get_selected_text() == "a long answer?"


@pytest.mark.asyncio
@pytest.mark.parametrize("move_on", ["close", "ask", "browse"])
async def test_the_side_card_moving_on_drops_its_selection_and_its_quote(move_on: str) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 30)) as pilot:
        card = await _side_card(app, pilot, "first question", "first answer")
        input_bar = app.main_screen.query_one(InputBar)
        popup = await _finish_drag_over(app, _side_body(card, "answer"), "first answer")

        if move_on == "close":
            card.close_question()
        elif move_on == "ask":
            card.begin("next", "second question")
        else:
            await card.show_record("next", SideQuestionEntry("2026-11-10T10:00:00+00:00", "second?", "second answer"))

        await wait_for(lambda: not popup.is_shown, pilot=pilot, description="the pill went with its text")
        assert app.main_screen.get_selected_text() is None
        # Pressed where it stood, nothing old reaches the draft.
        _post_mouse(app, MouseDown, popup.region.x + 1, popup.region.y)
        await pilot.pause()
        assert input_bar.value == ""


@pytest.mark.asyncio
async def test_the_side_card_moving_on_keeps_a_chat_selection_and_its_quote() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 30)) as pilot:
        text = "the chat's answer"
        chat_body = await _panel_with_agent_message(app, pilot, text)
        card = await _side_card(app, pilot, "a side question", "the side answer")
        popup = await _finish_drag_over(app, chat_body, text)

        card.close_question()
        await pilot.pause()

        assert popup.is_shown
        assert app.main_screen.get_selected_text() == text


@pytest.mark.asyncio
async def test_the_chat_scrolling_keeps_a_quote_offer_for_the_side_card() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await _panel_with_agent_message(app, pilot, "the chat's answer")
        card = await _side_card(app, pilot, "a side question", "the side answer")
        popup = await _finish_drag_over(app, _side_body(card, "answer"), "the side answer")

        # The main task's output scrolls the chat while the side answer stays put.
        app.main_screen.schedule_chat_selection_reprojection(app.main_screen.query_one(ChatPanel))
        await pilot.pause()

        assert popup.is_shown


@pytest.mark.asyncio
async def test_answer_arrival_preserves_selected_question_and_its_widget() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 30)) as pilot:
        card = app.main_screen.query_one(BtwPanel)
        card.begin("side", "Selected question")
        await wait_for(lambda: bool(card.query(BtwQuestion)) and screen_is_settled(app, app.main_screen), pilot=pilot)
        question = card.query_one(BtwQuestion)
        await _finish_drag_over(app, question.query_one(VirtualizedMarkdown), "Selected question")
        selection = dict(app.main_screen.selections)
        page = card.query_one(BtwPage)
        position = page.scroll_y

        await card.finish("side", "The answer arrives")
        await wait_for(lambda: screen_is_settled(app, app.main_screen), pilot=pilot)

        assert card.query_one(BtwQuestion) is question
        assert app.main_screen.selections == selection
        assert app.main_screen.get_selected_text() == "Selected question"
        assert page.scroll_y == position


@pytest.mark.asyncio
@pytest.mark.parametrize("move_on", ["close", "ask", "browse", "notice", "hide", "answer again"])
async def test_queued_quote_request_cannot_quote_a_retired_side_page(move_on: str) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 30)) as pilot:
        card = await _side_card(app, pilot, "First question", "First answer")
        input_bar = app.main_screen.query_one(InputBar)
        await _finish_drag_over(app, _side_body(card, "answer"), "First answer")
        # Textual clears the selection on pressing the pill, before delivering Requested.
        app.main_screen.clear_selection()
        if move_on == "close":
            card.close_question()
        elif move_on == "ask":
            card.begin("new", "Second question")
        elif move_on == "browse":
            await card.show_record("new", SideQuestionEntry("2026-11-10T10:00:00+00:00", "Second?", "Second answer"))
        elif move_on == "notice":
            card.show_history_notice("new", loading=True)
        elif move_on == "answer again":
            # The same question completing again: its answer is replaced, the page stays.
            await card.finish("side", "Second answer")
        else:
            card.display = False
        app.main_screen.post_message(QuoteSelectionPopup.Requested())
        await pilot.pause()
        assert input_bar.value == ""
        assert not input_bar.draft().references


@pytest.mark.asyncio
@pytest.mark.usefixtures("frozen_earlier_heap")
async def test_side_card_messages_follow_a_language_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        panel = app.screen.query_one(BtwPanel)
        answer = "\n\n".join(f"Paragraph {index}" for index in range(40))
        await panel.show_record("shown", SideQuestionEntry("2026-11-10T10:00:00+00:00", "Why?", answer))
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        question, reply = panel.query_one(BtwQuestion), panel.query_one(BtwAnswer)
        page = panel.query_one(BtwPage)
        page.scroll_to(y=page.max_scroll_y // 2, animate=False)
        await wait_for(
            lambda: 0 < page.scroll_y < page.max_scroll_y and screen_is_settled(app, app.screen), pilot=pilot
        )
        body = reply.query_one(VirtualizedMarkdown)
        app.screen.selections = {body: Selection(Offset(0, 0), Offset(7, 0))}
        selection = dict(app.screen.selections)
        position = page.scroll_y

        app.locale_controller.switch_locale("zh-Hans")

        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        assert page.scroll_y == position
        assert app.screen.selections == selection
        assert panel.query_one(BtwQuestion) is question
        assert panel.query_one(BtwAnswer) is reply
        for message, label in [(question, "问题"), (reply, "回答")]:
            header = message.query_one(".message-header", Static)
            assert rich_plain(header.render()) == label
            assert header.get_selection(Selection(None, None)) == (f"[{label}]", "\n")
        # The copy actions, new on the card, speak the new language too.
        for message, tooltip in [(question, "复制你的原始消息"), (reply, "复制原始智能体回复")]:
            copy = message.query_one(MessageCopyButton)
            assert rich_plain(copy.render()) == "复制"
            assert str(copy.tooltip) == tooltip
        assert (question.text, reply.text) == ("Why?", answer)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["chat", "card"])
async def test_resizing_withdraws_the_pill_and_keeps_the_selection(source: str) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 30)) as pilot:
        text = "the chat's answer"
        body = await _panel_with_agent_message(app, pilot, text)
        if source == "card":
            text = "the side answer"
            card = await _side_card(app, pilot, "a side question", text)
            body = _side_body(card, "answer")
        popup = await _finish_drag_over(app, body, text)

        await pilot.resize_terminal(80, 50)

        await wait_for(lambda: not popup.is_shown, pilot=pilot, description="the new layout withdrew the pill")
        assert app.main_screen.get_selected_text() == text


@pytest.mark.asyncio
async def test_a_drag_over_the_side_question_and_answer_quotes_both_with_their_labels() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 30)) as pilot:
        card = await _side_card(app, pilot, "why retry at all", "a bounded retry keeps failures visible")
        question, answer = _side_body(card, "question"), _side_body(card, "answer")
        input_bar = app.main_screen.query_one(InputBar)

        app.main_screen._watch__select_state(_chat_drag_state(question, answer, end_offset=Offset(len("a bounded"), 0)))
        app.main_screen._watch__selecting(False)
        popup = app.main_screen.query_one(QuoteSelectionPopup)
        await wait_for(lambda: popup.is_shown and popup.region.width > 0, pilot=pilot, description="quote pill shown")
        await _click_pill(app, pilot, popup)

        assert input_bar.draft().references.get("quote", 1) == DraftReference(
            "quote", 1, "why retry at all\n[Answer]\na bounded"
        )
