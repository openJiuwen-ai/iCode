# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The side question card itself: its one page, where it scrolls, what it copies, and its own update work."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
import textual._compositor as compositor_module
from textual.scrollbar import ScrollBar
from textual.selection import Selection
from textual.widget import Widget
from textual.widgets import Button, Static, TextArea

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.widgets.btw import BtwAnswer, BtwPage, BtwPanel, BtwQuestion, BtwThinking, ChatColumn
from chrys.app.tui.widgets.chat.messages import AgentMessage, MessageCopyButton, UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.config.settings import Settings
from chrys.foundation.models.side_questions import SideQuestionEntry
from chrys.foundation.patches import textual_reflow_reuse
from tests.support.pilot_barrier import screen_is_settled
from tests.support.side_question_card import CLOSE_GLYPH, shown_answer, shown_question
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import (
    BusyWidget,
    assert_app_handles_messages,
    click_when_settled,
    interrupt_removal,
    resize_when_settled,
    rich_plain,
)
from tests.support.waiting import (
    wait_for,
)

if TYPE_CHECKING:
    from pathlib import Path

    from textual._arrange import DockArrangeResult
    from textual.geometry import Size

    from chrys.app.tui.app import ChrysApp

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


async def test_thinking_card_takes_half_of_the_column(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        panel = app.screen.query_one(BtwPanel)
        panel.begin("short", "Why?")
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        assert panel.running
        assert app.screen.query_one(ChatColumn).region.height == 34
        assert panel.region.height == 17
        assert app.screen.query_one(ChatPanel).region.height == 17


_BTW_THEMES = ("chrys", "monokai", "textual-light", "ansi-dark", "ansi-light")


async def _assert_card_paints_in_theme(panel: BtwPanel) -> None:
    """What the theme decides on the card: the close button's look, and the page drawn on the card's own background."""
    close = panel.query_one("#btw-close", Button)
    assert close.rich_style.bold
    assert close.styles.background.a > 0
    assert close.rich_style.bgcolor != panel.rich_style.bgcolor
    page = panel.query_one(BtwPage)
    assert page.styles.background.a == 0
    assert page.rich_style.bgcolor == panel.rich_style.bgcolor


def _shown_rows(app: ChrysApp, widget: Widget) -> str:
    """The screen rows *widget* paints, as text."""
    strips = app.screen._compositor.render_strips()
    shown = app.screen.find_widget(widget).visible_region
    return "\n".join(strips[y].crop(shown.x, shown.right).text for y in range(shown.y, shown.bottom))


def _in_view(page: BtwPage, widget: Widget) -> bool:
    """Whether *widget*'s first row is inside the page's visible rows."""
    return page.scroll_y <= widget.virtual_region.y < page.scroll_y + page.scrollable_content_region.height


@pytest.mark.parametrize("size", [(60, 24), (120, 36)])
async def test_one_page_shows_question_over_answer_as_chat_messages_and_keeps_close_visible(
    tmp_path: Path, size: tuple[int, int]
) -> None:
    """Each theme only colors the card: its paints are checked under every theme in one app per size, and
    the story the layout and the localized text decide runs once, under the last theme."""
    app = make_chrys_app(tmp_path, settings=Settings(locale="zh-Hans"))
    async with app.run_test(size=size) as pilot:
        panel = app.screen.query_one(BtwPanel)
        page = panel.query_one(BtwPage)
        question = "这个仓库的 list[int] 是什么?\n" + "请结合 Python 类型声明解释。\n" * 20
        panel.begin("question", question)
        close = panel.query_one("#btw-close", Button)
        await wait_for(
            lambda: panel.display and close.content_size.height == 1 and shown_question(panel) == question,
            pilot=pilot,
            description="side question close button has a visible text row",
        )
        for theme in _BTW_THEMES:
            app.theme = theme
            await wait_for(
                lambda theme=theme: app.current_theme.name == theme and screen_is_settled(app, app.screen),
                pilot=pilot,
                description=f"the card restyled for {theme}",
            )
            await _assert_card_paints_in_theme(panel)
        assert CLOSE_GLYPH in app.screen._compositor.render_strips()[close.region.y].text
        assert panel.border_title == "顺便问"
        assert not panel.border_subtitle
        assert str(close.tooltip) == "关闭（Esc）"  # noqa: RUF001 - localized punctuation
        assert close.region.width == 3
        assert close.region.right == panel.region.right - 2
        counter = panel.query_one("#btw-position", Static)
        assert counter.region.x == panel.query_one("#btw-previous").region.right
        assert panel.query_one("#btw-next").region.x == counter.region.right
        # The controls sit over the page's first row, beside the question's header: no row of their own.
        assert close.region.y == page.region.y == panel.content_region.y
        assert page.region.height == panel.content_region.height
        # One blank row between the card's title and the question, as between the question and the answer.
        assert page.region.y == panel.region.y + 2
        gap = app.screen._compositor.render_strips()[panel.region.y + 1].text
        assert gap[panel.region.x + 1 : panel.region.right - 1].strip() == ""

        # A question too long for the page leaves the thinking row under it in view.
        thinking = panel.query_one(BtwThinking)
        await wait_for(lambda: page.scroll_y > 0 and _in_view(page, thinking), pilot=pilot)
        # Scrolled, the controls stay where they were, over the page's first visible row.
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        assert close.region.y == page.region.y
        assert CLOSE_GLYPH in app.screen._compositor.render_strips()[close.region.y].text
        # The page's scrollbar still shows beside them.
        assert isinstance(app.screen.get_widget_at(page.region.right - 1, close.region.y)[0], ScrollBar)
        message = panel.query_one(BtwQuestion)
        page.scroll_home(animate=False)
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        header = _shown_rows(app, message).splitlines()[0]
        assert message.region.y == page.region.y
        # A plain label on the card's own background, as the answer's.
        assert header.split()[:3] == ["█", "问题", "复制"]
        assert "\u276f" not in header
        assert message.styles.background.a == 0
        assert "撤回" not in header
        assert "PM" not in header and "AM" not in header
        assert "list[int]" in _shown_rows(app, page)

        await panel.finish("question", "答案: 主要使用 **Python**。")
        assert shown_question(panel) == question
        assert shown_answer(panel) == "答案: 主要使用 **Python**。"
        answer = panel.query_one(BtwAnswer)
        # Read from the top while it waited, the page still brings the answer that lands into view once, as the
        # chat does for an answer that lands after its reader scrolled up; the question stays the same widget.
        await wait_for(lambda: _in_view(page, answer) and screen_is_settled(app, app.screen), pilot=pilot)
        assert page.scroll_y > 0
        assert panel.query_one(BtwQuestion) is message
        # As many blank rows between the question and the answer as between the card's title and the question.
        assert answer.virtual_region.y - message.virtual_region.bottom == page.region.y - panel.region.y - 1 == 1
        shown = _shown_rows(app, answer)
        header = shown.splitlines()[0]
        # The card's one answer never folds: no arrow, and clicking its header leaves it open.
        assert header.split()[:3] == ["█", "回答", "复制"]
        assert "答案: 主要使用 Python" in shown
        await click_when_settled(pilot, answer.query_one(".agent-header"))
        assert not answer.collapsed
        assert "答案: 主要使用 Python" in _shown_rows(app, answer)
        assert not thinking.display
        assert CLOSE_GLYPH in app.screen._compositor.render_strips()[close.region.y].text

        # A short question starts the page from its top, its answer still to come.
        panel.begin("next", "另一个问题")
        await wait_for(lambda: shown_question(panel) == "另一个问题" and shown_answer(panel) is None, pilot=pilot)
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        assert page.scroll_y == 0
        assert thinking.display
        await click_when_settled(pilot, "#btw-close")
        await wait_for(lambda: not panel.display, pilot=pilot, description="close works from the controls row")
        assert shown_question(panel) == "" and shown_answer(panel) is None


@pytest.mark.parametrize("long_question", [False, True])
async def test_reopened_side_question_starts_at_its_top_unless_the_question_hides_its_answer(
    tmp_path: Path, long_question: bool
) -> None:
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(120, 36)) as pilot:
        panel = app.screen.query_one(BtwPanel)
        question = "\n\n".join(f"Question line {index}" for index in range(30)) if long_question else "Short?"
        # Shown as it opens, as /btw reopens the latest answer: the page is placed after the answer arrives.
        await panel.show_record("reopened", SideQuestionEntry("2026-11-10T10:00:00+00:00", question, "The answer."))
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        page = panel.query_one(BtwPage)
        assert _in_view(page, panel.query_one(BtwAnswer))
        assert (page.scroll_y > 0) == long_question


@pytest.mark.parametrize("shown", ["notice", "question", "answered"])
async def test_side_card_takes_part_in_gc_freeze_whatever_it_shows(tmp_path: Path, shown: str) -> None:
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(120, 36)) as pilot:
        panel = app.screen.query_one(BtwPanel)
        if shown == "notice":
            panel.show_history_notice("notice", loading=True)
        elif shown == "question":
            panel.begin("question", "Why?")
        else:
            await panel.show_record("answered", SideQuestionEntry("2026-11-10T10:00:00+00:00", "Why?", "Because."))
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        bodies = list(panel.query(VirtualizedMarkdown))
        assert len(bodies) == {"notice": 0, "question": 1, "answered": 2}[shown]
        prepared: list[VirtualizedMarkdown] = []
        renewed: list[VirtualizedMarkdown] = []
        with (
            patch.object(VirtualizedMarkdown, "prepare_for_gc_freeze", lambda body: prepared.append(body)),  # noqa: PLW0108 - a function binds to the body
            patch.object(VirtualizedMarkdown, "after_gc_freeze", lambda body: renewed.append(body)),  # noqa: PLW0108 - a function binds to the body
        ):
            panel.prepare_for_gc_freeze()
            panel.after_gc_freeze()
        assert prepared == renewed == bodies
        assert (panel.gc_freeze_block_reason() is not None) == (shown == "question")


async def test_browsing_side_questions_does_not_arrange_a_populated_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    textual_reflow_reuse.apply_runtime_patch()
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app.screen
        chat = main.query_one(ChatPanel)
        for index in range(24):
            await chat.mount(UserMessage(f"Question {index}"), AgentMessage(f"Answer {index} with **emphasis**."))
        panel = main.query_one(BtwPanel)
        await panel.show_record("first", SideQuestionEntry("2026-11-10T10:00:00+00:00", "First?", "First answer"))
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="populated chat and card settled")
        transcript = {widget for entry in chat.children for widget in entry.walk_children(Widget, with_self=True)}
        chat_region, chat_scroll = chat.region, chat.scroll_y
        arranged: list[Widget] = []
        arrange = Widget.arrange

        def record_arrange(self: Widget, size: Size, optimal: bool = False) -> DockArrangeResult:
            arranged.append(self)
            return arrange(self, size, optimal)

        # The oracle's own from-scratch arrangement would show up in the spy.
        monkeypatch.setattr(compositor_module, "_VERIFY_REUSE", False)
        monkeypatch.setattr(Widget, "arrange", record_arrange)
        await panel.show_record(
            "second", SideQuestionEntry("2026-11-10T11:00:00+00:00", "Second?", "Second answer with **emphasis**.")
        )
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="next side question shown")
        assert "Second answer with emphasis" in _shown_rows(app, panel)
        assert [widget for widget in arranged if widget in transcript] == []
        assert (chat.region, chat.scroll_y) == (chat_region, chat_scroll)

        # Positive control: the spy sees the transcript a from-scratch reflow arranges.
        arranged.clear()
        main._compositor.reflow(main, main.outer_size)
        assert len({widget for widget in arranged if widget in transcript}) >= 24


@pytest.mark.parametrize("phase", ["parse", "remove"])
async def test_cancelled_finish_leaves_card_responsive_and_no_orphan_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """Cancel the publisher while DOM work is in flight, rather than before or after it."""
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        panel = app.screen.query_one(BtwPanel)
        panel.begin("old", "Old question")
        await wait_for(
            lambda: shown_question(panel) == "Old question" and screen_is_settled(app, app.screen), pilot=pilot
        )
        old = panel.query_one(BtwQuestion)
        entered, release = asyncio.Event(), asyncio.Event()
        show_text = BtwAnswer.show_text

        async def hold_parse(reply: BtwAnswer, text: str) -> None:
            entered.set()
            await release.wait()
            await show_text(reply, text)

        task: asyncio.Task[None] | None = None
        if phase == "parse":
            monkeypatch.setattr(BtwAnswer, "show_text", hold_parse)
            task = asyncio.create_task(panel.finish("old", "Complete answer"))
        else:
            busy = BusyWidget()
            await old.mount(busy)

        try:
            if phase == "parse":
                parse = task
                assert parse is not None
                await wait_for(lambda: entered.is_set() or parse.done(), pilot=pilot)
                assert entered.is_set() and not parse.done()
                # While it parses, the answer is not shown at all: no header over an empty body.
                assert not panel.query_one(BtwAnswer).display
                parse.cancel()
                # The cancelled wait still owns completion; the card's parse must continue.
                await asyncio.sleep(0)
                assert not parse.done()
            else:

                def start_replacement() -> None:
                    nonlocal task
                    panel.begin("next", "Next question")
                    task = asyncio.create_task(panel.finish("next", "Complete answer"))

                def cancel_finish() -> None:
                    assert task is not None and not task.done()
                    task.cancel()

                await interrupt_removal(busy, start_replacement, cancel_finish)
        finally:
            release.set()
            if task is not None:
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert not panel.running
        assert len(panel.query(BtwQuestion)) == len(panel.query(BtwAnswer)) == 1
        assert shown_answer(panel) == "Complete answer"
        if phase == "parse":
            assert panel.query_one(BtwQuestion) is old
        else:
            assert not old.is_attached and not busy.is_attached
        await assert_app_handles_messages(app)
        panel.begin("again", "Still responsive")
        await wait_for(lambda: shown_question(panel) == "Still responsive" and shown_answer(panel) is None, pilot=pilot)
        await click_when_settled(pilot, "#btw-close")
        await wait_for(
            lambda: not panel.display and not panel.query(BtwQuestion) and not panel.query(BtwAnswer), pilot=pilot
        )
        await assert_app_handles_messages(app)


@pytest.mark.parametrize("move_on", ["ask", "close"])
async def test_content_replacement_retires_an_answer_still_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, move_on: str
) -> None:
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        panel = app.screen.query_one(BtwPanel)
        panel.begin("old", "Old question")
        entered, release = asyncio.Event(), asyncio.Event()
        show_text = BtwAnswer.show_text

        async def hold_parse(reply: BtwAnswer, text: str) -> None:
            entered.set()
            await release.wait()
            await show_text(reply, text)

        monkeypatch.setattr(BtwAnswer, "show_text", hold_parse)
        task = asyncio.create_task(panel.finish("old", "Old answer"))
        try:
            await wait_for(lambda: entered.is_set() or task.done(), pilot=pilot)
            assert entered.is_set() and not task.done()
            if move_on == "ask":
                panel.begin("next", "New question")
            else:
                panel.close_question()
        finally:
            release.set()
            await task
        await wait_for(
            lambda: (
                shown_question(panel) == ("New question" if move_on == "ask" else "") and shown_answer(panel) is None
            ),
            pilot=pilot,
        )
        assert panel.running == (move_on == "ask")
        assert not panel.query_one("#btw-error").display
        await assert_app_handles_messages(app)


@pytest.mark.parametrize("size", [(60, 24), (80, 24)])
@pytest.mark.parametrize("outcome", ["waiting", "failed", "answer", "record"])
@pytest.mark.parametrize("long_question", [False, True])
async def test_wrapped_chinese_question_reveals_output_after_layout_settles(
    tmp_path: Path, size: tuple[int, int], outcome: str, long_question: bool
) -> None:
    app = make_chrys_app(tmp_path, settings=Settings(locale="zh-Hans"))
    initial_size = (size[0] + 20, size[1]) if outcome == "failed" else size
    async with app.run_test(size=initial_size) as pilot:
        panel = app.screen.query_one(BtwPanel)
        page = panel.query_one(BtwPage)
        question = "请结合仓库中的类型声明解释这个问题以及它的使用方式。" * (12 if long_question else 1)
        if outcome == "record":
            await panel.show_record("shown", SideQuestionEntry("2026-11-10T10:00:00+00:00", question, "完整回答。"))
        else:
            panel.begin("shown", question)
            await wait_for(
                lambda: shown_question(panel) == question and screen_is_settled(app, app.screen), pilot=pilot
            )
            if outcome == "failed":
                panel.fail("shown")
                await resize_when_settled(pilot, *size)
            elif outcome == "answer":
                await panel.finish("shown", "完整回答。")
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        target = panel.query_one(
            BtwThinking if outcome == "waiting" else "#btw-error" if outcome == "failed" else BtwAnswer
        )
        assert _in_view(page, target), (page.scroll_y, target.virtual_region, page.scrollable_content_region)
        assert (page.scroll_y > 0) == long_question


async def test_a_repeated_completion_shows_one_answer(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        panel = app.screen.query_one(BtwPanel)
        panel.begin("same", "Why?")
        await panel.finish("same", "First completion")
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        # Text selected in the first answer goes with it: nothing left on the page to quote.
        app.screen.selections = {panel.query_one(BtwAnswer).query_one(VirtualizedMarkdown): Selection(None, None)}
        await panel.finish("same", "Second completion")
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        assert [answer.text for answer in panel.query(BtwAnswer) if not answer._pruning] == ["Second completion"]
        assert shown_question(panel) == "Why?"
        assert not app.screen.selections


async def test_card_leaving_mid_parse_lets_its_finish_return(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The card cancels its own work as it unmounts; whoever awaited it just stops waiting.

    Calls the unmount handler itself: the contract under test is that cancellation, which a real removal
    reaches only through the screen's teardown.
    """
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        panel = app.screen.query_one(BtwPanel)
        panel.begin("leaving", "Why?")
        await wait_for(lambda: shown_question(panel) == "Why?", pilot=pilot)
        entered = asyncio.Event()

        async def hold_parse(_reply: BtwAnswer, _text: str) -> None:
            entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(BtwAnswer, "show_text", hold_parse)
        finish = asyncio.create_task(panel.finish("leaving", "Answer"))
        await wait_for(entered.is_set, pilot=pilot)

        panel.on_unmount()

        await asyncio.wait_for(finish, timeout=5)
        assert finish.exception() is None


async def test_copying_the_answer_gives_it_as_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copied: list[str] = []
    monkeypatch.setattr(
        "chrys.app.tui.widgets.chat.messages.copy_text_to_clipboards", lambda _app, text: copied.append(text)
    )
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        panel = app.screen.query_one(BtwPanel)
        written = "```make\nall:\n\t@echo ok\n```\n"
        await panel.show_record("tabs", SideQuestionEntry("2026-11-10T10:00:00+00:00", "How?", written))
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        answer = panel.query_one(BtwAnswer)
        await click_when_settled(pilot, answer.query_one(MessageCopyButton))
        await wait_for(lambda: bool(copied), pilot=pilot)
        assert copied == [written]
        # Shown made safe for the terminal: the tab is spaced out on the page.
        assert "\t" not in answer.text


@pytest.mark.parametrize("outcome", ["answer", "failed"])
async def test_what_lands_after_the_reader_scrolled_up_is_brought_into_view(tmp_path: Path, outcome: str) -> None:
    """Scrolled back up to the question while it waited, the reader still sees the answer or the failure land."""
    app = make_chrys_app(tmp_path, settings=Settings(locale="zh-Hans"))
    async with app.run_test(size=(80, 24)) as pilot:
        panel = app.screen.query_one(BtwPanel)
        page = panel.query_one(BtwPage)
        question = "请结合仓库中的类型声明解释这个问题以及它的使用方式。" * 12
        panel.begin("waiting", question)
        await wait_for(lambda: page.scroll_y > 0 and screen_is_settled(app, app.screen), pilot=pilot)
        page.scroll_to(y=0, animate=False)
        await wait_for(lambda: page.scroll_y == 0 and screen_is_settled(app, app.screen), pilot=pilot)

        if outcome == "answer":
            await panel.finish("waiting", "完整回答。")
        else:
            panel.fail("waiting")

        target = panel.query_one(BtwAnswer if outcome == "answer" else "#btw-error")
        await wait_for(lambda: _in_view(page, target) and screen_is_settled(app, app.screen), pilot=pilot)
        # Brought in once: scrolling away afterwards is left alone.
        page.scroll_to(y=0, animate=False)
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        assert page.scroll_y == 0


async def test_labels_switched_to_a_wider_language_paint_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opened in Chinese, the labels are two cells wide; in English each takes its own width, painted whole."""
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    app = make_chrys_app(tmp_path, settings=Settings(locale="zh-Hans"))
    async with app.run_test(size=(80, 24)) as pilot:
        panel = app.screen.query_one(BtwPanel)
        await panel.show_record("shown", SideQuestionEntry("2026-11-10T10:00:00+00:00", "为什么?", "因为。"))
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        # Chinese first, so the switch below is a real one.
        assert rich_plain(panel.query_one(BtwQuestion).query_one(".message-header", Static).render()) == "问题"

        app.locale_controller.switch_locale("en")

        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        rows = [strip.text for strip in app.screen._compositor.render_strips()]
        for message, label in [(panel.query_one(BtwQuestion), "Question"), (panel.query_one(BtwAnswer), "Answer")]:
            assert rows[message.region.y].split("█", 1)[1].split()[:2] == [label, "Copy"]


@pytest.mark.parametrize(
    ("size", "locale", "position", "labels"),
    [
        ((76, 26), "en", (0, 1), ["Question", "Copy"]),
        ((80, 24), "en", (122, 456), ["Question", "Copy"]),
        ((80, 24), "zh-Hans", (122, 456), ["问题", "复制"]),
        ((60, 24), "zh-Hans", (0, 1), ["问题", "复制"]),
    ],
)
async def test_the_question_header_paints_whole_beside_the_controls(
    tmp_path: Path, size: tuple[int, int], locale: str, position: tuple[int, int], labels: list[str]
) -> None:
    """The controls sit at the right of the question's header row; the header keeps out from under them."""
    app = make_chrys_app(tmp_path, settings=Settings(locale=locale))
    async with app.run_test(size=size) as pilot:
        panel = app.screen.query_one(BtwPanel)
        await panel.show_record("shown", SideQuestionEntry("2026-11-10T10:00:00+00:00", "Why?", "Because."))
        panel.set_history_position(*position, "2026-11-10T10:00:00+00:00")
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        row = app.screen._compositor.render_strips()[panel.query_one(BtwQuestion).region.y].text
        assert row.split("█", 1)[1].split()[:2] == labels
        assert f"{position[0] + 1}/{position[1]}" in row
        assert CLOSE_GLYPH in row


@pytest.mark.parametrize(("height", "draft_lines"), [(12, 1), (18, 7)])
async def test_a_card_of_one_row_gives_up_the_blank_row_and_keeps_its_controls(
    tmp_path: Path, height: int, draft_lines: int
) -> None:
    """A short terminal, or a tall draft, leaves the card one row inside its border: the row under the title goes,
    the controls stay."""
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(80, height)) as pilot:
        app.screen.query_one("#chat-input", TextArea).load_text("\n".join(["draft"] * draft_lines))
        panel = app.screen.query_one(BtwPanel)
        page = panel.query_one(BtwPage)
        close = panel.query_one("#btw-close", Button)
        await panel.show_record("shown", SideQuestionEntry("2026-11-10T10:00:00+00:00", "Why?", "Because."))
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        assert panel.region.height == 3
        assert close.region.y == page.region.y == panel.region.y + 1
        assert app.screen.get_widget_at(close.region.x + 1, close.region.y)[0] is close
        row = app.screen._compositor.render_strips()[close.region.y].text
        # Too short to show the question over its answer, the card brings the answer's text in, beside the controls.
        assert row.split("█", 1)[1].split()[0] == "Because."
        assert CLOSE_GLYPH in row

        await resize_when_settled(pilot, 80, height + 12)
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        assert panel.region.height >= 4
        assert close.region.y == page.region.y == panel.region.y + 2

        await resize_when_settled(pilot, 80, height)
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        assert close.region.y == page.region.y == panel.region.y + 1
        await click_when_settled(pilot, close)
        await wait_for(lambda: not panel.display, pilot=pilot, description="close works on a card of one row")


@pytest.mark.parametrize("move_on", ["ask", "notice"])
async def test_the_pager_shows_only_while_the_card_shows_a_saved_record(tmp_path: Path, move_on: str) -> None:
    """A question still on its way, or a notice, is no saved record: no pager, only the close button."""
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        panel = app.screen.query_one(BtwPanel)
        pager = [panel.query_one(selector) for selector in ("#btw-previous", "#btw-position", "#btw-next")]
        close = panel.query_one("#btw-close", Button)
        await panel.show_record("shown", SideQuestionEntry("2026-11-10T10:00:00+00:00", "Why?", "Because."))
        panel.set_history_position(0, 1, "2026-11-10T10:00:00+00:00")
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        assert all(widget.region.width for widget in pager)
        assert "‹ 1/1 ›" in app.screen._compositor.render_strips()[close.region.y].text  # noqa: RUF001 - navigation glyph

        if move_on == "ask":
            panel.begin("next", "And then?")
        else:
            panel.show_history_notice("next", loading=True, reveal=False)
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        assert not any(widget.region.width for widget in pager)
        row = app.screen._compositor.render_strips()[close.region.y].text
        assert "0/0" not in row and "‹" not in row  # noqa: RUF001 - navigation glyph
        assert CLOSE_GLYPH in row

        panel.set_history_position(1, 2, "2026-11-10T11:00:00+00:00")
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        assert "‹ 2/2 ›" in app.screen._compositor.render_strips()[close.region.y].text  # noqa: RUF001 - navigation glyph


@pytest.mark.parametrize("outcome", ["answer", "failed"])
@pytest.mark.parametrize("lines", [4, 5, 6, 7, 8])
async def test_what_lands_under_a_long_question_shows_its_text_without_a_jump(
    tmp_path: Path, outcome: str, lines: int
) -> None:
    """The page moves straight down to what landed, never back to its top on the way, and its text shows."""
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(100, 30)) as pilot:
        panel = app.screen.query_one(BtwPanel)
        page = panel.query_one(BtwPage)
        panel.begin("long", "\n".join(f"question line {index}" for index in range(lines)))
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        waited_at = page.scroll_y
        painted: list[float] = []
        watch_scroll_y = page.watch_scroll_y

        def record(old: float, new: float) -> None:
            painted.append(new)
            watch_scroll_y(old, new)

        monkeypatch_target = page
        monkeypatch_target.watch_scroll_y = record  # type: ignore[method-assign]
        if outcome == "answer":
            await panel.finish("long", "the answer text")
            landed = "the answer text"
        else:
            panel.fail("long")
            landed = rich_plain(panel.query_one("#btw-error", Static).render()).split()[0]
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)

        assert all(position >= waited_at for position in painted), (waited_at, painted)
        assert landed in _shown_rows(app, page)


async def test_tab_reaches_the_card_controls_and_clicking_its_text_keeps_typing_in_the_input(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(100, 24)) as pilot:
        panel = app.screen.query_one(BtwPanel)
        await panel.show_record("shown", SideQuestionEntry("2026-11-10T10:00:00+00:00", "Why?", "Because."))
        panel.set_history_position(0, 1, "2026-11-10T10:00:00+00:00")
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        page = panel.query_one(BtwPage)
        close = panel.query_one("#btw-close", Button)
        chat_input = app.screen.query_one("#chat-input", TextArea)

        visited: list[Widget | None] = []
        for _ in range(len(app.screen.focus_chain) + 1):
            await pilot.press("tab")
            visited.append(app.focused)
            if app.focused is close:
                break
        assert close in visited
        # The page is a stop, to scroll it from the keyboard; its text is not.
        assert page in visited
        assert not [widget for widget in visited if widget is not None and page in widget.ancestors]

        await click_when_settled(pilot, panel.query_one(BtwAnswer).query_one(VirtualizedMarkdown), offset=(1, 0))
        await wait_for(lambda: app.focused is chat_input, pilot=pilot, description="typing stays in the input")
