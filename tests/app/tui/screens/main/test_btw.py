# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real-engine /btw interaction: independent execution, cancellation, and the two-column layout."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, ContentSwitcher, Static, Tab, Tabs

from chrys.app.tui.widgets.btw import BtwPanel, BtwQuestion, BtwThinking, ChatColumn
from chrys.app.tui.widgets.chat.messages import AgentMessage, UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
from chrys.foundation.config.settings import Settings
from chrys.foundation.events import types as ev
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.orchestration.engine import side_questions
from chrys.service.session.side_questions import BTW_FILE_NAME, load_side_questions
from chrys.service.state.store import JsonFileStateStore
from tests.app.tui.screens.test_prompt_shortcut_send import (
    ALPHA,
    BETA,
    FAST_MODEL,
    MAIN_MODEL,
    make_agent,
    mock_models,
    notices,
    running_session,
    turn_finished,
)
from tests.support.pilot_barrier import screen_is_settled
from tests.support.secure_files import plant_owner_only_bytes
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled, make_wheel
from tests.support.waiting import (
    ENGINE_TEST_WAIT_TIMEOUT,
    ENGINE_TURN_TIMEOUT,
    await_run_task_chain,
    wait_for,
    with_wait_deadline,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from chrys.service.profiles.models.schema import ModelProfile
    from tests.support.engines import AgentEngineFactory

_CLOSE_GLYPH = "✕"
_SAVE_WARNING = ("warning", "This side answer could not be saved. It stays available until you leave this session.")
_READ_WARNING = ("warning", "Side question history could not be read. Your saved file was kept.")


@dataclass
class SideAnswer:
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup_release: asyncio.Event | None = None
    calls: list[tuple[str, str]] = field(default_factory=list)
    reminders: list[tuple[str, ...]] = field(default_factory=list)
    answer: str = "A finite retry limit keeps failures visible."
    fail: bool = False

    async def __call__(
        self,
        profile: ModelProfile,
        *,
        session_id: str,
        session_dir: Path | None,
        question: str,
        context: str,
        reminders: Sequence[str] = (),
    ) -> str:
        self.calls.append((question, context))
        self.reminders.append(tuple(reminders))
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            if self.cleanup_release is not None:
                await self.cleanup_release.wait()
            raise
        if self.fail:
            raise RuntimeError("private provider detail must not reach the card")
        return self.answer


@pytest.mark.parametrize("main_finishes_first", [False, True])
async def test_close_cancels_only_side_question_and_ignores_late_answer(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, main_finishes_first: bool
) -> None:
    side = SideAnswer()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    async with running_session(agent_engine, tmp_path, monkeypatch, size=(120, 36)) as session:
        hold = session.hold()
        session.model.hold = hold
        session.chat.load_text("Fix retries.")
        await session.pilot.press("enter")
        await wait_for(hold.entered.is_set, pilot=session.pilot, description="main model held")
        panel = session.main.query_one(BtwPanel)
        chat = session.main.query_one(ChatPanel)
        sidebar = session.main.query_one(SidebarPanel)
        sidebar_region = sidebar.region
        chat_height = chat.region.height
        question = "为什么不能无限重试?" * 20
        session.chat.load_text("/btw " + question)
        await wait_for(
            lambda: str(session.bar.query_one("#send-btn", Button).label) == "Send side question",
            pilot=session.pilot,
            description="send button targets side question",
        )
        await click_when_settled(session.pilot, "#send-btn")
        await wait_for(side.entered.is_set, pilot=session.pilot, description="side request started")
        await wait_for(lambda: panel.region.height > 0, pilot=session.pilot, description="side card laid out")
        assert panel.running
        assert panel in session.main._gc_freeze_participants
        assert panel.gc_freeze_block_reason() is not None
        assert panel.border_title == "Side Questions"
        assert not panel.border_subtitle
        assert panel.query_one(BtwQuestion).question == question
        assert panel.query_one(ContentSwitcher).current == "btw-output-page"
        close = panel.query_one("#btw-close", Button)
        assert close.content_size.height == 1
        assert _CLOSE_GLYPH in session.main._compositor.render_strips()[close.region.y].text
        assert str(close.tooltip) == "Close (Esc)"
        assert panel.region.y >= chat.region.bottom
        assert panel.region.right == chat.region.right
        assert sidebar.region == sidebar_region
        assert chat.region.height < chat_height
        assert session.chat.text == ""
        assert "Fix retries." in side.calls[0][1]
        assert not any(question in message.text for message in session.engine.history_messages)

        if main_finishes_first:
            hold.release()
            await await_run_task_chain(session.engine, turn_state=session.engine.turns.turn_state)
            assert panel.running and panel.display

        session.chat.load_text("/btw another question")
        await session.pilot.press("enter")
        assert session.chat.text == "/btw another question"
        assert len(side.calls) == 1
        request_id = panel.request_id
        session.main._suggestions.dismiss_suggestions()
        await session.pilot.press("escape")
        await wait_for(side.cancelled.is_set, pilot=session.pilot, description="side request cancelled")
        await wait_for(
            lambda: chat.region.height == chat_height, pilot=session.pilot, description="chat height restored"
        )
        assert not panel.display
        assert session.engine.is_turn_active is not main_finishes_first
        assert session.seen.of(ev.UserInterrupt) == []
        assert sidebar.region == sidebar_region
        assert not panel.query_one(BtwThinking)._animating

        origin = InvocationOrigin(
            kind="side_question", session_id=session.session_id(), invocation_id=request_id, parent=None
        )
        await session.bus.publish(
            ev.InvocationContentDelta(origin=origin, segment_id="late", channel="answer", text="Late answer")
        )
        await session.bus.publish(
            ev.InvocationContentFinished(origin=origin, segment_id="late", channel="answer", status="completed")
        )
        assert not panel.display
        assert panel.query_one(VirtualizedMarkdown).source == ""
        hold.release()
        await await_run_task_chain(session.engine, turn_state=session.engine.turns.turn_state)


async def test_closing_a_question_allows_another_while_cancelled_request_is_cleaning_up(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleanup_release = asyncio.Event()
    side = SideAnswer(cleanup_release=cleanup_release, answer="Second answer")
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        panel = session.main.query_one(BtwPanel)
        try:
            session.chat.load_text("/btw First?")
            session.bar.submit_input()
            await wait_for(side.entered.is_set, pilot=session.pilot, description="first side request started")
            first_task = next(iter(session.engine._side_questions._run_tasks))
            first_request_id = panel.request_id

            await click_when_settled(session.pilot, "#btw-close")
            await wait_for(side.cancelled.is_set, pilot=session.pilot, description="cancelled request cleanup held")
            assert not first_task.done()
            assert not panel.display

            session.chat.load_text("/btw Second?")
            session.bar.submit_input()
            await wait_for(
                lambda: (
                    bool(panel.request_id)
                    and panel.request_id != first_request_id
                    and (len(side.calls) == 2 or not panel.running)
                ),
                pilot=session.pilot,
                description="replacement side question admitted or rejected",
            )
            assert [question for question, _context in side.calls] == ["First?", "Second?"]
            assert not first_task.done()
            assert panel.running
            assert not panel.query_one("#btw-error").display

            cleanup_release.set()
            await asyncio.gather(first_task, return_exceptions=True)
            assert first_task.cancelled()
            assert panel.running
            assert panel.query_one(BtwQuestion).question == "Second?"

            side.release.set()
            await wait_for(
                lambda: str(panel.query_one("#btw-position", Static).content) == "1/1",
                pilot=session.pilot,
                description="replacement side answer shown",
            )
            assert panel.query_one(VirtualizedMarkdown).source == "Second answer"
            store = JsonFileStateStore(tmp_path / "sessions")
            await wait_for(
                lambda: (
                    [
                        entry.question
                        for entry in load_side_questions(store.session_dir(session.session_id()), session.session_id())
                    ]
                    == ["Second?"]
                ),
                pilot=session.pilot,
                description="replacement side answer saved",
            )
        finally:
            cleanup_release.set()
            side.release.set()
            await asyncio.gather(
                *tuple(session.engine._side_questions._run_tasks | session.engine._side_questions._commit_tasks),
                return_exceptions=True,
            )


@pytest.mark.parametrize("target", ["answer", "question_tab", "previous"])
async def test_card_click_keeps_composer_typing_and_sending(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    side = SideAnswer()
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    async with running_session(agent_engine, tmp_path, monkeypatch, size=(120, 40)) as session:
        panel = session.main.query_one(BtwPanel)
        for index, (question, answer) in enumerate((("One?", "Answer one"), ("Two?", "Answer two")), 1):
            side.answer = answer
            session.bar.focus_input()
            session.chat.load_text(f"/btw {question}")
            session.bar.submit_input()
            await wait_for(
                lambda index=index: str(panel.query_one("#btw-position", Static).content) == f"{index}/{index}",
                pilot=session.pilot,
                description="side answer shown",
            )
            owner = session.engine._side_questions
            await asyncio.gather(*tuple(owner._run_tasks | owner._commit_tasks))

        if target == "answer":
            await click_when_settled(session.pilot, panel.query_one(VirtualizedMarkdown), offset=(3, 0))
        elif target == "question_tab":
            await click_when_settled(session.pilot, "#btw-question-tab")
            assert panel.query_one(ContentSwitcher).current == "btw-question-page"
        else:
            await click_when_settled(session.pilot, "#btw-previous")
            await wait_for(
                lambda: str(panel.query_one("#btw-position", Static).content) == "1/2",
                pilot=session.pilot,
                description="previous side answer shown",
            )
            assert panel.query_one(VirtualizedMarkdown).source == "Answer one"

        await session.pilot.press(*"fix it")
        assert session.chat.text == "fix it"
        await session.pilot.press("enter")
        await wait_for(
            lambda: session.model.call_count == 1,
            pilot=session.pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="typed message reaches the main model",
        )
        await turn_finished(session)
        assert [event.text for event in session.seen.of(ev.UserMessage)] == ["fix it"]
        assert "fix it" in session.model.last_user_text()


@pytest.mark.parametrize("long_answer", [False, True])
async def test_final_answer_stays_in_card_and_scrolls_without_shortening_sidebar(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, long_answer: bool
) -> None:
    side = SideAnswer(
        answer="\n\n".join(f"Paragraph {index}: bounded retry." for index in range(60))
        if long_answer
        else "Bounded retry."
    )
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    async with running_session(agent_engine, tmp_path, monkeypatch, size=(120, 40)) as session:
        panel = session.main.query_one(BtwPanel)
        sidebar = session.main.query_one(SidebarPanel)
        sidebar_region = sidebar.region
        session.chat.load_text("/btw Explain retry.\nUse an example.")
        await session.pilot.press("enter")
        markdown = panel.query_one(VirtualizedMarkdown)
        await wait_for(
            lambda: panel.display and not panel.running and markdown.display,
            pilot=session.pilot,
            description="final side answer shown",
        )
        assert markdown.source == side.answer
        assert panel.gc_freeze_block_reason() is None
        assert panel.query_one(BtwQuestion).question == "Explain retry.\nUse an example."
        assert sidebar.region == sidebar_region
        column = session.main.query_one(ChatColumn)
        assert column.region.height == 34
        assert panel.region.height == 17
        assert session.main.query_one(ChatPanel).region.height == 17
        output = panel.query_one("#btw-output-page")
        assert output.max_scroll_y == 0
        assert not output.show_vertical_scrollbar
        pages = panel.query_one(ContentSwitcher)
        assert markdown.region.height == pages.content_size.height
        assert markdown.region.bottom == panel.content_region.bottom
        assert session.seen.of(ev.UserMessage) == []
        if long_answer:
            assert markdown.max_scroll_y > 0
            assert markdown.vertical_scrollbar.region.right == panel.region.right - 1
            assert markdown.vertical_scrollbar.region.height == pages.content_size.height
            markdown.focus()
            await session.pilot.press("down")
            await wait_for(lambda: markdown.scroll_y > 0, pilot=session.pilot, description="side answer scrolls")
        await click_when_settled(session.pilot, "#btw-close")
        await wait_for(lambda: not panel.display, pilot=session.pilot, description="completed side card closed")
        assert session.chat.has_focus


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


async def test_browsing_long_and_short_saved_answers_keeps_card_height(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    side = SideAnswer(answer="Short answer.")
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    async with running_session(agent_engine, tmp_path, monkeypatch, size=(120, 40)) as session:
        panel = session.main.query_one(BtwPanel)
        for question, answer in (("Short?", "Short answer."), ("Long?", "\n\n".join(["Long answer."] * 60))):
            side.answer = answer
            session.chat.load_text(f"/btw {question}")
            session.bar.submit_input()
            await wait_for(
                lambda expected=answer: panel.query_one(VirtualizedMarkdown).source == expected, pilot=session.pilot
            )
            await asyncio.gather(*tuple(session.engine._side_questions._run_tasks))
            await asyncio.gather(*tuple(session.engine._side_questions._commit_tasks))
        await wait_for(lambda: screen_is_settled(session.app, session.main), pilot=session.pilot)
        chat = session.main.query_one(ChatPanel)
        card_region, chat_region = panel.region, chat.region
        assert card_region.height == chat_region.height == 17
        await click_when_settled(session.pilot, "#btw-previous")
        await wait_for(lambda: panel.query_one(BtwQuestion).question == "Short?", pilot=session.pilot)
        await wait_for(lambda: screen_is_settled(session.app, session.main), pilot=session.pilot)
        assert (panel.region, chat.region) == (card_region, chat_region)
        await click_when_settled(session.pilot, "#btw-next")
        await wait_for(lambda: panel.query_one(BtwQuestion).question == "Long?", pilot=session.pilot)
        await wait_for(lambda: screen_is_settled(session.app, session.main), pilot=session.pilot)
        assert (panel.region, chat.region) == (card_region, chat_region)
        assert panel.query_one(VirtualizedMarkdown).max_scroll_y > 0


async def test_failure_and_session_change_do_not_keep_a_side_request_alive(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    side = SideAnswer(fail=True)
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        panel = session.main.query_one(BtwPanel)
        session.chat.load_text("/btw")
        session.bar.submit_input()
        assert session.chat.text == ""
        assert not panel.display
        await wait_for(
            lambda: ("warning", "No side questions yet. Use /btw <question> to ask one.") in notices(session.app),
            pilot=session.pilot,
        )
        toast = next(item for item in session.app._notifications if item.message.startswith("No side questions"))
        assert (toast.title, toast.timeout, toast.markup) == ("Side Questions", 3, False)
        assert not panel.display
        assert not panel.request_id
        assert session.chat.has_focus
        assert not side.calls
        session.chat.load_text("/btw Fail safely.")
        session.bar.submit_input()
        await wait_for(lambda: panel.display and not panel.running, pilot=session.pilot, description="side error shown")
        error = panel.query_one("#btw-error", Static)
        assert error.display
        assert "private provider" not in str(error.content)
        assert session.seen.of(ev.Error) == []

        side.fail = False
        side.release.clear()
        side.entered.clear()
        session.chat.load_text("/btw Wait here.")
        session.bar.submit_input()
        await wait_for(side.entered.is_set, pilot=session.pilot, description="replacement side request started")
        await session.bus.publish(ev.SessionNew())
        await wait_for(side.cancelled.is_set, pilot=session.pilot, description="session switch cancels side request")
        assert not panel.display


@pytest.mark.parametrize("theme", ["chrys", "monokai", "textual-light", "ansi-dark", "ansi-light"])
@pytest.mark.parametrize("size", [(60, 24), (120, 36)])
async def test_tabs_separate_full_question_from_output_and_keep_close_visible(
    tmp_path: Path, theme: str, size: tuple[int, int]
) -> None:
    app = make_chrys_app(tmp_path, settings=Settings(locale="zh-Hans"))
    async with app.run_test(size=size) as pilot:
        app.theme = theme
        panel = app.screen.query_one(BtwPanel)
        question = "这个仓库的 list[int] 是什么?\n" + "请结合 Python 类型声明解释。\n" * 20
        panel.begin("question", question)
        switcher = panel.query_one(ContentSwitcher)
        tabs = panel.query_one(Tabs)
        close = panel.query_one("#btw-close", Button)
        await wait_for(
            lambda: panel.display and close.content_size.height == 1,
            pilot=pilot,
            description="side question close button has a visible text row",
        )
        assert switcher.current == "btw-output-page"
        assert _CLOSE_GLYPH in app.screen._compositor.render_strips()[close.region.y].text
        assert panel.border_title == "顺便问"
        assert not panel.border_subtitle
        assert str(close.tooltip) == "关闭（Esc）"  # noqa: RUF001 - localized punctuation
        assert close.region.width == 3
        assert tabs.region.width >= 12
        assert close.rich_style.bold
        assert close.styles.background.a > 0
        assert close.rich_style.bgcolor != panel.rich_style.bgcolor
        counter = panel.query_one("#btw-position", Static)
        assert counter.content_region.x == panel.query_one("#btw-previous").region.right + 1
        assert panel.query_one("#btw-next").region.x == counter.content_region.right + 1
        assert str(panel.query_one("#btw-output-tab", Tab).label) == "回答"
        underline = app.screen._compositor.render_strips()[close.region.bottom]
        assert underline.crop(close.region.x, close.region.right).text == "━" * close.region.width
        if not theme.startswith("ansi"):
            # The native tab underline continues through the gap and under every toolbar control.
            assert underline.crop(tabs.region.right - 1, close.region.right).text == "━" * (
                close.region.right - tabs.region.right + 1
            )
            tab_style = next(iter(underline.crop(tabs.region.right - 1, tabs.region.right))).style
            control_style = next(iter(underline.crop(close.region.x, close.region.right))).style
            assert tab_style is not None and control_style is not None
            assert tab_style.color == control_style.color
        assert "list[int]" not in "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        card_region = panel.region
        close_region = close.region

        await click_when_settled(pilot, "#btw-question-tab")
        await wait_for(lambda: switcher.current == "btw-question-page", pilot=pilot)
        question_page = panel.query_one(VerticalScroll)
        assert panel.query_one(BtwQuestion).question == question
        assert question_page.styles.background.a == 0
        assert question_page.rich_style.bgcolor == panel.rich_style.bgcolor
        assert question_page.max_scroll_y > 0
        assert question_page.region == switcher.content_region
        assert question_page.vertical_scrollbar.region.right == panel.region.right - 1
        assert question_page.vertical_scrollbar.region.height == switcher.content_size.height
        assert "list[int]" in "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        assert panel.region == card_region
        assert close.region == close_region
        assert _CLOSE_GLYPH in app.screen._compositor.render_strips()[close.region.y].text

        await panel.finish("question", "答案: 主要使用 **Python**。")
        assert switcher.current == "btw-question-page"
        tabs.focus()
        await pilot.press("right")
        await wait_for(lambda: switcher.current == "btw-output-page", pilot=pilot)
        assert "答案: 主要使用 Python" in "\n".join(strip.text for strip in app.screen._compositor.render_strips())
        assert panel.query_one(VirtualizedMarkdown).source == "答案: 主要使用 **Python**。"
        assert panel.query_one(VirtualizedMarkdown).region.height == switcher.content_size.height
        assert _CLOSE_GLYPH in app.screen._compositor.render_strips()[close.region.y].text

        # A new question always opens Answer, even after reading the previous question.
        tabs.active = "btw-question-tab"
        panel.begin("next", "另一个问题")
        await wait_for(lambda: switcher.current == "btw-output-page", pilot=pilot)
        assert tabs.active == "btw-output-tab"
        await click_when_settled(pilot, "#btw-close")
        await wait_for(lambda: not panel.display, pilot=pilot, description="close works from the fixed tab bar")


async def test_switching_btw_tabs_does_not_relayout_populated_main_screen(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app.screen
        chat = main.query_one(ChatPanel)
        for index in range(24):
            await chat.mount(UserMessage(f"Question {index}"), AgentMessage(f"Answer {index} with **emphasis**."))
        panel = main.query_one(BtwPanel)
        panel.begin("question", "A full question")
        await panel.finish("question", "A complete answer")
        await wait_for(
            lambda: screen_is_settled(app, main),
            pilot=pilot,
            description="populated chat and side card settled",
        )
        footer = main.query_one(ChrysFooter)
        with (
            patch.object(main, "_refresh_layout", wraps=main._refresh_layout) as layouts,
            patch.object(main, "update_node_styles", wraps=main.update_node_styles) as restyles,
            patch.object(footer, "recompose", wraps=footer.recompose) as recomposes,
        ):
            await click_when_settled(pilot, "#btw-question-tab")
            assert "A full question" in "\n".join(strip.text for strip in main._compositor.render_strips())
            await click_when_settled(pilot, "#btw-output-tab")
            assert "A complete answer" in "\n".join(strip.text for strip in main._compositor.render_strips())
            layouts.assert_not_called()
            restyles.assert_not_called()
            recomposes.assert_not_called()


@pytest.mark.parametrize("first_fails", [False, True])
async def test_next_question_can_start_while_previous_result_is_being_delivered(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_fails: bool
) -> None:
    side = SideAnswer(answer="First answer", fail=first_fails)
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    entered, release = asyncio.Event(), asyncio.Event()
    first_task: asyncio.Task | None = None

    async def hold_first_result(_event: ev.SideQuestionHistoryRequested | ev.InvocationSideQuestionFailed) -> None:
        nonlocal first_task
        if not entered.is_set():
            first_task = asyncio.current_task()
            entered.set()
            await release.wait()

    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        held_event = ev.InvocationSideQuestionFailed if first_fails else ev.SideQuestionHistoryRequested
        await session.bus.subscribe(held_event, hold_first_result)
        panel = session.main.query_one(BtwPanel)
        try:
            session.chat.load_text("/btw First?")
            session.bar.submit_input()
            await wait_for(entered.is_set, pilot=session.pilot, description="previous result delivery held")
            assert first_task is not None
            first_request_id = panel.request_id
            assert not first_task.done()
            assert not panel.running
            if first_fails:
                assert panel.query_one("#btw-error").display
            else:
                assert panel.query_one(VirtualizedMarkdown).source == "First answer"

            side.answer = "Second answer"
            side.fail = False
            session.chat.load_text("/btw Second?")
            session.bar.submit_input()
            await wait_for(
                lambda: panel.request_id != first_request_id and (len(side.calls) == 2 or not panel.running),
                pilot=session.pilot,
                description="next question admitted or rejected",
            )
            assert len(side.calls) == 2
        finally:
            release.set()
            await session.bus.unsubscribe(held_event, hold_first_result)
            await asyncio.gather(
                *tuple(session.engine._side_questions._run_tasks | session.engine._side_questions._commit_tasks)
            )
        await wait_for(
            lambda: str(panel.query_one("#btw-position", Static).content) == ("1/1" if first_fails else "2/2"),
            pilot=session.pilot,
        )
        assert panel.query_one(BtwQuestion).question == "Second?"
        assert panel.query_one(VirtualizedMarkdown).source == "Second answer"
        assert not panel.query_one("#btw-error").display


async def test_saved_history_reopens_navigates_and_restores_a_btw_only_session(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    side = SideAnswer()
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        panel = session.main.query_one(BtwPanel)
        counter = panel.query_one("#btw-position", Static)
        tabs = panel.query_one(Tabs)
        pages = panel.query_one(ContentSwitcher)
        for question, answer, position in (("First?", "First answer", "1/1"), ("Second?", "Second answer", "2/2")):
            side.answer = answer
            session.chat.load_text(f"/btw {question}")
            session.bar.submit_input()
            await wait_for(
                lambda expected=position: str(counter.content) == expected,
                pilot=session.pilot,
                description="saved answer indexed",
            )
            await asyncio.gather(
                *tuple(session.engine._side_questions._run_tasks | session.engine._side_questions._commit_tasks)
            )
        assert session.seen.of(ev.UserMessage) == []
        original = session.session_id()
        store = JsonFileStateStore(tmp_path / "sessions")
        path = store.session_dir(original) / BTW_FILE_NAME
        data = json.loads(path.read_text(encoding="utf-8"))
        assert list(data) == ["session_id", "items"]
        assert data["session_id"] == original
        assert [entry["question"] for entry in data["items"]] == ["First?", "Second?"]
        assert all(set(entry) == {"created_at", "question", "answer"} for entry in data["items"])
        assert len(await store.list_sessions()) == 1
        assert await store.load_session(original) == {"messages": [], "compressed_msgs": [], "turn_counter": 0}

        await click_when_settled(session.pilot, "#btw-close")
        with monkeypatch.context() as patcher:
            patcher.setattr(session.main, "_model_unconfigured", lambda: True)
            session.chat.load_text("/btw")
            await wait_for(
                lambda: str(session.bar.query_one("#send-btn", Button).label) == "Open side questions",
                pilot=session.pilot,
            )
            session.bar.submit_input()
            await wait_for(lambda: panel.display and str(counter.content) == "2/2", pilot=session.pilot)
        assert panel.query_one(VirtualizedMarkdown).source == "Second answer"
        assert str(panel.query_one("#btw-output-tab", Tab).label) == "Answer"
        assert panel.query_one("#btw-next", Button).disabled
        assert counter.tooltip
        await click_when_settled(session.pilot, "#btw-previous")
        await wait_for(lambda: str(counter.content) == "1/2", pilot=session.pilot)
        assert panel.query_one(VirtualizedMarkdown).source == "First answer"
        assert panel.query_one(BtwQuestion).question == "First?"
        assert panel.query_one("#btw-previous", Button).disabled
        assert tabs.active == "btw-output-tab"
        assert pages.current == "btw-output-page"
        await click_when_settled(session.pilot, "#btw-question-tab")
        await click_when_settled(session.pilot, "#btw-next")
        await wait_for(lambda: str(counter.content) == "2/2", pilot=session.pilot)
        assert tabs.active == "btw-question-tab"
        assert pages.current == "btw-question-page"
        assert panel.query_one(BtwQuestion).question == "Second?"
        await click_when_settled(session.pilot, "#btw-previous")
        await wait_for(lambda: str(counter.content) == "1/2", pilot=session.pilot)
        assert tabs.active == "btw-question-tab"
        assert pages.current == "btw-question-page"
        assert panel.query_one(BtwQuestion).question == "First?"
        await click_when_settled(session.pilot, "#btw-output-tab")
        await click_when_settled(session.pilot, "#btw-next")
        await wait_for(lambda: str(counter.content) == "2/2", pilot=session.pilot)
        assert tabs.active == "btw-output-tab"
        assert pages.current == "btw-output-page"
        assert len(side.calls) == 2

        await session.bus.publish(ev.SessionNew())
        assert not panel.display
        await session.bus.publish(ev.SessionRestore(session_id=original))
        assert session.engine.session.session_id == original
        session.chat.load_text("/btw")
        session.bar.submit_input()
        await wait_for(lambda: panel.display and str(counter.content) == "2/2", pilot=session.pilot)
        assert tabs.active == "btw-output-tab"
        assert pages.current == "btw-output-page"
        assert len(side.calls) == 2
        assert [entry.answer for entry in load_side_questions(path.parent, original)] == [
            "First answer",
            "Second answer",
        ]
        await session.bus.publish(ev.SessionClear(session_id=original))
        assert not path.exists()
        assert not panel.display


@pytest.mark.parametrize("operation", ["clear", "rollback"])
async def test_session_reset_does_not_wait_for_queued_side_write_or_resurrect_it(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_write = JsonFileStateStore._save_side_question_sync

    def blocked_write(self, session_id, entry, seed, *, cancelled=None):
        entered.set()
        try:
            if not release.wait(ENGINE_TURN_TIMEOUT):
                raise TimeoutError("test commit gate was not released")
            original_write(self, session_id, entry, seed, cancelled=cancelled)
        finally:
            finished.set()

    side = SideAnswer()
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    monkeypatch.setattr(JsonFileStateStore, "_save_side_question_sync", blocked_write)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        original_session = session.session_id()
        try:
            session.chat.load_text("Main turn")
            session.bar.submit_input()
            await wait_for(lambda: session.engine.session.turn_number == 1, pilot=session.pilot)
            await await_run_task_chain(session.engine, turn_state=session.engine.turns.turn_state)
            session.chat.load_text("/btw Save then reset.")
            session.bar.submit_input()
            await wait_for(entered.is_set, pilot=session.pilot, description="side write queued before lock")
            panel = session.main.query_one(BtwPanel)
            await wait_for(lambda: not panel.running, pilot=session.pilot)
            assert panel.query_one(VirtualizedMarkdown).source == side.answer
            event = (
                ev.SessionClear(session_id=original_session) if operation == "clear" else ev.UserRollback(target_turn=0)
            )
            await asyncio.wait_for(session.bus.publish(event), timeout=ENGINE_TURN_TIMEOUT)
            assert not finished.is_set()
            assert not panel.display
        finally:
            release.set()
            await wait_for(finished.is_set, pilot=session.pilot, description="invalidated worker returned")
        directory = JsonFileStateStore(tmp_path / "sessions").session_dir(original_session)
        assert not (directory / BTW_FILE_NAME).exists()
        assert not (directory / "session.json").exists()


async def test_answer_does_not_wait_for_saves_in_completion_order(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()
    original_write = JsonFileStateStore._save_side_question_sync

    def hold_first(self, session_id, entry, seed, *, cancelled=None):
        if entry.question == "First?":
            entered.set()
            if not release.wait(ENGINE_TURN_TIMEOUT):
                raise TimeoutError("test commit gate was not released")
        original_write(self, session_id, entry, seed, cancelled=cancelled)

    side = SideAnswer(answer="First answer")
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    monkeypatch.setattr(JsonFileStateStore, "_save_side_question_sync", hold_first)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        panel = session.main.query_one(BtwPanel)
        store = JsonFileStateStore(tmp_path / "sessions")
        directory = store.session_dir(session.session_id())
        try:
            session.chat.load_text("/btw First?")
            session.bar.submit_input()
            await wait_for(entered.is_set, pilot=session.pilot)
            await wait_for(lambda: not panel.running, pilot=session.pilot)
            assert panel.query_one(VirtualizedMarkdown).source == "First answer"
            await click_when_settled(session.pilot, "#btw-close")
            side.answer = "Second answer"
            session.chat.load_text("/btw Second?")
            session.bar.submit_input()
            await wait_for(lambda: len(load_side_questions(directory, session.session_id())) == 1, pilot=session.pilot)
            assert load_side_questions(directory, session.session_id())[0].question == "Second?"
        finally:
            release.set()
            await asyncio.gather(
                *tuple(session.engine._side_questions._run_tasks | session.engine._side_questions._commit_tasks)
            )
        assert [entry.question for entry in load_side_questions(directory, session.session_id())] == [
            "First?",
            "Second?",
        ]
        session.chat.load_text("/btw")
        session.bar.submit_input()
        await wait_for(lambda: str(panel.query_one("#btw-position", Static).content) == "2/2", pilot=session.pilot)
        assert panel.query_one(BtwQuestion).question == "Second?"


async def test_cleanup_timeout_does_not_block_session_operations_or_new_questions(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_cleanup(*args, **kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            # Model the cancellation-resistant cleanup used by client owners.
            from chrys.foundation.util.once_close import finish_close

            await finish_close(asyncio.create_task(release.wait()))

    monkeypatch.setattr(side_questions, "answer_side_question", slow_cleanup)
    monkeypatch.setattr(side_questions, "_CLEANUP_TIMEOUT_SECONDS", 0.01)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        try:
            session.chat.load_text("/btw First?")
            session.bar.submit_input()
            await wait_for(entered.is_set, pilot=session.pilot)
            await asyncio.wait_for(session.engine._side_questions.quiesce(), timeout=1)
            assert "cleanup exceeded its grace period" in caplog.text
            assert not session.main.query_one(BtwPanel).running
            side = SideAnswer(answer="Second answer")
            side.release.set()
            monkeypatch.setattr(side_questions, "answer_side_question", side)
            session.chat.load_text("/btw Second?")
            session.bar.submit_input()
            await wait_for(side.entered.is_set, pilot=session.pilot)
        finally:
            release.set()
            await asyncio.gather(
                *tuple(session.engine._side_questions._run_tasks | session.engine._side_questions._commit_tasks),
                return_exceptions=True,
            )


@pytest.mark.parametrize("rebuild", ["none", "model", "agent", "restore"])
async def test_save_failure_warns_and_keeps_answer_available_in_current_session(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rebuild: str
) -> None:
    async def fail_save(self, session_id, entry, seed, *, cancelled=None):
        raise OSError("storage unavailable")

    side = SideAnswer(answer="Still useful without disk.")
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    monkeypatch.setattr(JsonFileStateStore, "save_side_question", fail_save)
    async with running_session(agent_engine, tmp_path, monkeypatch, models=mock_models()) as session:
        panel = session.main.query_one(BtwPanel)
        if rebuild == "restore":
            session.chat.load_text("Main task")
            session.bar.submit_input()
            await wait_for(lambda: session.model.call_count == 1, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT)
            await turn_finished(session)
        session.chat.load_text("/btw Keep this answer")
        session.bar.submit_input()
        await wait_for(
            lambda: _SAVE_WARNING in notices(session.app),
            pilot=session.pilot,
        )
        await wait_for(lambda: not panel.running, pilot=session.pilot)
        assert panel.query_one(VirtualizedMarkdown).source == side.answer
        await click_when_settled(session.pilot, "#btw-close")
        original = session.session_id()
        if rebuild == "restore":
            await session.bus.publish(ev.SessionRestore(session_id=original))
            assert len(session.builds.profiles) == 2
            assert not panel.display
        elif rebuild != "none":
            event = (
                ev.SetModelProfile(profile_id=FAST_MODEL)
                if rebuild == "model"
                else ev.AgentProfileSwitch(profile_name=BETA)
            )
            await session.bus.publish(event)
            await wait_for(
                lambda: not session.engine._permits.agent_loading, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT
            )
            assert session.session_id() == original
        session.chat.load_text("/btw")
        session.bar.submit_input()
        await wait_for(lambda: panel.display, pilot=session.pilot)
        assert panel.query_one(BtwQuestion).question == "Keep this answer"
        assert panel.query_one(VirtualizedMarkdown).source == side.answer


@pytest.mark.parametrize("card_action", ["close", "navigate"])
async def test_save_failure_warns_after_card_moves_on(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, card_action: str
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    original_save = JsonFileStateStore.save_side_question

    async def held_save(self, session_id, entry, seed, *, cancelled=None):
        if entry.question == "First?":
            entered.set()
            await release.wait()
            raise OSError("storage unavailable")
        await original_save(self, session_id, entry, seed, cancelled=cancelled)

    side = SideAnswer(answer="First answer")
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    monkeypatch.setattr(JsonFileStateStore, "save_side_question", held_save)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        panel = session.main.query_one(BtwPanel)
        try:
            session.chat.load_text("/btw First?")
            session.bar.submit_input()
            await wait_for(entered.is_set, pilot=session.pilot)
            commit = next(iter(session.engine._side_questions._commit_tasks))
            await asyncio.gather(*tuple(session.engine._side_questions._run_tasks))
            assert not commit.done()
            first_request = panel.request_id
            await click_when_settled(session.pilot, "#btw-close")
            if card_action == "navigate":
                side.answer = "Second answer"
                session.chat.load_text("/btw Second?")
                session.bar.submit_input()
                await wait_for(
                    lambda: str(panel.query_one("#btw-position", Static).content) == "2/2", pilot=session.pilot
                )
                await click_when_settled(session.pilot, "#btw-previous")
                await wait_for(
                    lambda: str(panel.query_one("#btw-position", Static).content) == "1/2", pilot=session.pilot
                )
                assert panel.query_one(VirtualizedMarkdown).source == "First answer"
            assert panel.request_id != first_request
            assert _SAVE_WARNING not in notices(session.app)
            release.set()
            await commit
            await wait_for(lambda: _SAVE_WARNING in notices(session.app), pilot=session.pilot)
            assert panel.display is (card_action == "navigate")
            assert not panel.running
        finally:
            release.set()
            await asyncio.gather(*tuple(session.engine._side_questions._run_tasks))
            await asyncio.gather(*tuple(session.engine._side_questions._commit_tasks))


async def test_unreadable_history_still_reopens_memory_answer(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    side = SideAnswer(answer="Only copy of the answer")
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        directory = session.engine.session.session_dir
        assert directory is not None
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / BTW_FILE_NAME
        await asyncio.to_thread(plant_owner_only_bytes, path, b"{broken json")
        panel = session.main.query_one(BtwPanel)
        session.chat.load_text("/btw Keep this")
        session.bar.submit_input()
        await wait_for(lambda: _SAVE_WARNING in notices(session.app), pilot=session.pilot)
        await asyncio.gather(*tuple(session.engine._side_questions._run_tasks))
        assert panel.query_one(VirtualizedMarkdown).source == side.answer
        assert str(panel.query_one("#btw-position", Static).content) == "1/1"
        assert _READ_WARNING not in notices(session.app)
        await click_when_settled(session.pilot, "#btw-close")
        session.chat.load_text("/btw")
        session.bar.submit_input()
        await wait_for(lambda: _READ_WARNING in notices(session.app), pilot=session.pilot)
        await wait_for(lambda: panel.display and panel.query_one(VirtualizedMarkdown).display, pilot=session.pilot)
        assert panel.query_one(BtwQuestion).question == "Keep this"
        assert panel.query_one(VirtualizedMarkdown).source == side.answer
        assert str(panel.query_one("#btw-position", Static).content) == "1/1"
        assert path.read_bytes() == b"{broken json"


@pytest.mark.parametrize("rebuild", ["model", "agent", "restore"])
async def test_running_side_question_obeys_session_rebuild(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rebuild: str
) -> None:
    side = SideAnswer()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    async with running_session(
        agent_engine, tmp_path, monkeypatch, agents=(make_agent(ALPHA, model=MAIN_MODEL), make_agent(BETA))
    ) as session:
        panel = session.main.query_one(BtwPanel)
        if rebuild == "restore":
            session.chat.load_text("Main task")
            session.bar.submit_input()
            await wait_for(lambda: session.model.call_count == 1, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT)
            await turn_finished(session)
        session.chat.load_text("/btw Keep running")
        session.bar.submit_input()
        await wait_for(side.entered.is_set, pilot=session.pilot)
        task = next(iter(session.engine._side_questions._run_tasks))
        request_id = panel.request_id
        original = session.session_id()
        event = (
            ev.SetModelProfile(profile_id=FAST_MODEL)
            if rebuild == "model"
            else ev.AgentProfileSwitch(profile_name=BETA)
        )
        if rebuild == "restore":
            event = ev.SessionRestore(session_id=original)
        await session.bus.publish(event)
        await wait_for(
            lambda: not session.engine._permits.agent_loading and not session.bar.agent_loading,
            pilot=session.pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        assert session.session_id() == original
        assert len(session.builds.profiles) == 2
        if rebuild == "restore":
            assert task.cancelled() and side.cancelled.is_set()
            assert not panel.display and not panel.running
            assert not panel.query_one("#btw-error").display
            assert not session.main._model_unconfigured()
            session.chat.load_text("/btw Following question")
            session.bar.submit_input()
            await wait_for(lambda: len(side.calls) == 2, pilot=session.pilot)
            task = next(iter(session.engine._side_questions._run_tasks))
            request_id = panel.request_id
        assert not task.done()
        assert panel.running and panel.display and panel.request_id == request_id
        side.release.set()
        await task
        await asyncio.gather(*tuple(session.engine._side_questions._commit_tasks))
        assert panel.query_one(VirtualizedMarkdown).source == side.answer
        assert not panel.running and panel.gc_freeze_block_reason() is None
        directory = session.engine.session.session_dir
        assert directory is not None
        assert [entry.answer for entry in load_side_questions(directory, original)] == [side.answer]


@pytest.mark.parametrize("rebuild", ["model", "agent", "workspace"])
async def test_side_question_spanning_rebuild_answers_and_records_usage(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rebuild: str
) -> None:
    from chrys.foundation.trajectory.envelope import ActorRole
    from chrys.kernel import UsageDetails
    from chrys.service.analytics.usage_report import load_usage_report
    from chrys.service.llm import side_question
    from chrys.service.llm.mock import MockResponse
    from tests.app.tui.screens.test_prompt_shortcut_send import Hold, ScriptedModel

    class SideModel(ScriptedModel):
        def _next_response(self) -> MockResponse:
            self._call_index += 1
            return MockResponse(
                text="Recorded answer.", usage_details=UsageDetails(input_token_count=23, output_token_count=7)
            )

    client = SideModel()
    held = Hold()
    client.hold = held

    @asynccontextmanager
    async def scoped(profile, *, session_id, parent_session_id, session_dir):
        try:
            yield client
        finally:
            await client.aclose()

    monkeypatch.setattr(side_question, "scoped_client", scoped)
    other = tmp_path / "other-workspace"
    await asyncio.to_thread(other.mkdir)
    async with running_session(
        agent_engine, tmp_path, monkeypatch, agents=(make_agent(ALPHA, model=MAIN_MODEL), make_agent(BETA))
    ) as session:
        panel = session.main.query_one(BtwPanel)
        session.chat.load_text("/btw Count this answer")
        session.bar.submit_input()
        try:
            await wait_for(held.entered.is_set, pilot=session.pilot)
            task = next(iter(session.engine._side_questions._run_tasks))
            assert not task.done() and client.call_count == 1
            if rebuild == "model":
                event = ev.SetModelProfile(profile_id=FAST_MODEL)
            elif rebuild == "agent":
                event = ev.AgentProfileSwitch(profile_name=BETA)
            else:
                event = ev.WorkspaceChange(primary_cwd=str(other))
            await session.bus.publish(event)
            assert len(session.builds.profiles) == 2
            assert not task.done() and panel.running
            held.release()
            await task
            await asyncio.gather(*tuple(session.engine._side_questions._commit_tasks))
            assert panel.query_one(VirtualizedMarkdown).source == "Recorded answer."
            assert not panel.running
            await session.engine._trajectory_recorder.checkpoint()
            report = await asyncio.to_thread(load_usage_report, JsonFileStateStore(tmp_path / "sessions"))
            calls = [call for call in report.calls if call.source == ActorRole.SIDE_QUESTION]
            assert len(calls) == 1
            assert calls[0].reported and not calls[0].failed
            assert (calls[0].new_input, calls[0].output) == (23, 7)
        finally:
            held.release()


@pytest.mark.parametrize("transition", ["model", "rollback", "restore"])
@pytest.mark.parametrize("save_fails", [False, True])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_session_transition_preserves_pending_side_save(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: str, save_fails: bool
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    original_save = JsonFileStateStore.save_side_question

    async def held_save(self, session_id, entry, seed, *, cancelled=None):
        entered.set()
        await release.wait()
        if save_fails:
            raise OSError("storage unavailable")
        await original_save(self, session_id, entry, seed, cancelled=cancelled)

    side = SideAnswer()
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    monkeypatch.setattr(JsonFileStateStore, "save_side_question", held_save)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        if transition in {"rollback", "restore"}:
            prompts = ("one", "two", "three") if transition == "rollback" else ("one",)
            for turn_number, prompt in enumerate(prompts, 1):
                session.chat.load_text(prompt)
                await session.pilot.press("enter")
                await wait_for(
                    lambda expected=turn_number: session.engine.session.turn_number == expected,
                    pilot=session.pilot,
                    timeout=ENGINE_TURN_TIMEOUT,
                    description="main turn admitted before draining its save",
                )
                await turn_finished(session)
        panel = session.main.query_one(BtwPanel)
        try:
            session.chat.load_text("/btw Keep the save")
            session.bar.submit_input()
            await wait_for(entered.is_set, pilot=session.pilot)
            commit = next(iter(session.engine._side_questions._commit_tasks))
            await asyncio.gather(*tuple(session.engine._side_questions._run_tasks))
            event = (
                ev.SetModelProfile(profile_id=FAST_MODEL)
                if transition == "model"
                else ev.UserRollback(target_turn=1, revert_changes=False)
            )
            if transition == "restore":
                event = ev.SessionRestore(session_id=session.session_id())
            await session.bus.publish(event)
            assert not commit.done()
            if transition == "restore":
                assert len(session.builds.profiles) == 2
                assert not panel.display
            if transition == "rollback":
                assert session.engine.session.turn_number == 1
                assert not panel.display
            release.set()
            await commit
            if save_fails:
                await wait_for(lambda: _SAVE_WARNING in notices(session.app), pilot=session.pilot)
            else:
                directory = session.engine.session.session_dir
                assert directory is not None
                assert [entry.answer for entry in load_side_questions(directory, session.session_id())] == [side.answer]
            session.main._btw.close_question()
            session.chat.load_text("/btw")
            session.bar.submit_input()
            await wait_for(lambda: panel.display and panel.query_one(VirtualizedMarkdown).display, pilot=session.pilot)
            assert panel.query_one(VirtualizedMarkdown).source == side.answer
        finally:
            release.set()
            await asyncio.gather(*tuple(session.engine._side_questions._commit_tasks))


@pytest.mark.parametrize("rollback", ["failure", "success", "generation_only"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_rollback_only_cancels_side_question_on_success(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollback: str
) -> None:
    import chrys.orchestration.engine.engine as rollback_module

    side = SideAnswer()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    original_copy = rollback_module.atomic_copy_file

    def fail_primary(source: Path, destination: Path) -> None:
        if destination.name == "session.json":
            raise OSError("simulated swap failure")
        original_copy(source, destination)

    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        for turn_number, prompt in enumerate(("one", "two", "three"), 1):
            session.chat.load_text(prompt)
            await session.pilot.press("enter")
            await wait_for(
                lambda expected=turn_number: session.engine.session.turn_number == expected,
                pilot=session.pilot,
                timeout=ENGINE_TURN_TIMEOUT,
                description="main turn admitted before draining its save",
            )
            await turn_finished(session)
        panel = session.main.query_one(BtwPanel)
        session.chat.load_text("/btw Keep running")
        session.bar.submit_input()
        await wait_for(side.entered.is_set, pilot=session.pilot)
        task = next(iter(session.engine._side_questions._run_tasks))
        request_id = panel.request_id
        generation = session.engine._permits.session_generation
        if rollback == "generation_only":
            session.engine._permits.advance_session_generation()
        else:
            if rollback == "failure":
                monkeypatch.setattr(rollback_module, "atomic_copy_file", fail_primary)
            await session.bus.publish(ev.UserRollback(target_turn=1, revert_changes=False))
        assert session.engine._permits.session_generation > generation
        if rollback == "success":
            await asyncio.gather(task, return_exceptions=True)
            assert task.cancelled() and side.cancelled.is_set()
            assert not panel.display and not panel.running
            assert panel.gc_freeze_block_reason() is None
        else:
            if rollback == "failure":
                assert any(error.code == "rollback_swap_failed" for error in session.seen.of(ev.Error))
            assert not task.done()
            assert panel.running and panel.request_id == request_id
            side.release.set()
            await task
            await asyncio.gather(*tuple(session.engine._side_questions._commit_tasks))
            assert not panel.running and panel.gc_freeze_block_reason() is None
            assert panel.query_one(VirtualizedMarkdown).source == side.answer
            directory = session.engine.session.session_dir
            assert directory is not None
            assert [entry.answer for entry in load_side_questions(directory, session.session_id())] == [side.answer]
            session.chat.load_text("/btw Next question")
            session.bar.submit_input()
            await wait_for(lambda: len(side.calls) == 2, pilot=session.pilot)


async def test_refused_fork_leaves_side_question_running(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    side = SideAnswer()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        panel = session.main.query_one(BtwPanel)
        session.chat.load_text("/btw No completed history yet")
        session.bar.submit_input()
        await wait_for(side.entered.is_set, pilot=session.pilot)
        task = next(iter(session.engine._side_questions._run_tasks))
        request_id = panel.request_id
        await session.bus.publish(ev.SessionFork(session_id=session.session_id()))
        assert any(error.code == "session_fork_empty" for error in session.seen.of(ev.Error))
        assert not task.done()
        assert panel.running and panel.request_id == request_id
        side.release.set()
        await task
        await asyncio.gather(*tuple(session.engine._side_questions._commit_tasks))
        assert panel.query_one(VirtualizedMarkdown).source == side.answer
        assert not panel.running


async def test_cancel_during_completion_delivery_stops_thinking(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    side = SideAnswer()
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_completion(event: ev.InvocationContentFinished) -> None:
        if event.origin.kind == "side_question":
            entered.set()
            await release.wait()

    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        controller = session.main._btw
        await session.bus.unsubscribe(ev.InvocationContentFinished, controller.on_finished)
        await session.bus.subscribe(ev.InvocationContentFinished, hold_completion)
        await session.bus.subscribe(ev.InvocationContentFinished, controller.on_finished)
        try:
            session.chat.load_text("/btw Complete this")
            session.bar.submit_input()
            await wait_for(entered.is_set, pilot=session.pilot)
            assert controller.panel.running
            assert session.engine._side_questions._active is None
            await session.engine._side_questions.quiesce()
            assert not controller.panel.running
            assert not controller.panel.display
        finally:
            release.set()
            await session.bus.unsubscribe(ev.InvocationContentFinished, hold_completion)


async def test_btw_only_session_can_fork_and_reset_without_leaking_history(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    side = SideAnswer()
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        panel = session.main.query_one(BtwPanel)
        session.chat.load_text("/btw Keep in the fork.")
        session.bar.submit_input()
        await wait_for(lambda: str(panel.query_one("#btw-position", Static).content) == "1/1", pilot=session.pilot)
        await asyncio.gather(
            *tuple(session.engine._side_questions._run_tasks | session.engine._side_questions._commit_tasks)
        )
        original = session.session_id()
        session.chat.load_text("/fork")
        session.bar.submit_input()
        await wait_for(
            lambda: (
                bool(session.app.screen.query("#fork-session-stay"))
                and session.app.screen.query_one("#fork-session-stay").display
                and not session.main._btw.panel.running
            ),
            pilot=session.pilot,
            description="side-question-only session forked from the chat command",
        )
        await click_when_settled(session.pilot, "#fork-session-stay")
        store = JsonFileStateStore(tmp_path / "sessions")
        sessions = await store.list_sessions()
        assert len(sessions) == 2
        fork = next(meta.session_id for meta in sessions if meta.session_id != original)
        assert load_side_questions(store.session_dir(fork), fork)[0].question == "Keep in the fork."
        assert await session.engine._lifecycle.reset_session_to_welcome(original)
        assert not (store.session_dir(original) / BTW_FILE_NAME).exists()
        assert len(load_side_questions(store.session_dir(fork), fork)) == 1


@pytest.mark.parametrize("switch", [False, True])
async def test_fork_only_cancels_running_side_question_when_switching(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, switch: bool
) -> None:
    side = SideAnswer()
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        panel = session.main.query_one(BtwPanel)
        session.chat.load_text("/btw Saved answer")
        session.bar.submit_input()
        await wait_for(lambda: str(panel.query_one("#btw-position", Static).content) == "1/1", pilot=session.pilot)
        await asyncio.gather(
            *tuple(session.engine._side_questions._run_tasks | session.engine._side_questions._commit_tasks)
        )
        side.release.clear()
        side.entered.clear()
        session.chat.load_text("/btw Running answer")
        session.bar.submit_input()
        await wait_for(side.entered.is_set, pilot=session.pilot)
        task = next(iter(session.engine._side_questions._run_tasks))
        request_id = panel.request_id
        original = session.session_id()
        session.chat.load_text("/fork")
        session.bar.submit_input()
        await wait_for(
            lambda: (
                bool(session.app.screen.query("#fork-session-stay"))
                and session.app.screen.query_one("#fork-session-stay").visible
            ),
            pilot=session.pilot,
        )
        assert not task.done()
        assert panel.running and panel.request_id == request_id
        await click_when_settled(session.pilot, "#fork-session-switch" if switch else "#fork-session-stay")
        if switch:
            await wait_for(lambda: session.session_id() != original, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT)
            await asyncio.gather(task, return_exceptions=True)
            assert side.cancelled.is_set() and task.cancelled()
            assert not panel.running and not panel.display
            assert panel.gc_freeze_block_reason() is None
        else:
            assert session.session_id() == original
            assert not task.done()
            assert panel.running and panel.request_id == request_id
            side.release.set()
            await task
            await asyncio.gather(*tuple(session.engine._side_questions._commit_tasks))
            assert str(panel.query_one("#btw-position", Static).content) == "2/2"
            assert panel.query_one(VirtualizedMarkdown).source == side.answer
            assert not side.cancelled.is_set()


@pytest.mark.parametrize("long_reason", [False, True])
async def test_side_prompt_hooks_block_provider_and_keep_reminders_local(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, long_reason: bool
) -> None:
    import sys

    from chrys.foundation.platform import get_platform

    reason = "\n".join(f"side rejected line {index}" for index in range(31)) if long_reason else "side rejected"

    def write_hook() -> None:
        directory = get_platform().config_dir / "hooks"
        directory.mkdir(parents=True, exist_ok=True)
        script = (
            "import json,os; "
            "p=json.load(open(os.environ['CHRYS_HOOK_PAYLOAD_FILE'],encoding='utf-8')); "
            f"r={{'action':'block','reason':{reason!r}}} if 'deny' in p['text'] else {{'system_reminder':'side-only note'}}; "
            "json.dump(r,open(os.environ['CHRYS_HOOK_RESULT'],'w',encoding='utf-8'))"
        )
        document = {
            "version": 1,
            "hooks": [
                {
                    "id": "side-gate",
                    "event": "user_prompt_submit",
                    "run": {"type": "command", "argv": [sys.executable, "-c", script]},
                    "execution": {"mode": "blocking", "timeout_seconds": 20},
                }
            ],
        }
        (directory / "hooks.json").write_text(json.dumps(document), encoding="utf-8")

    await asyncio.to_thread(write_hook)
    side = SideAnswer()
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    async with running_session(agent_engine, tmp_path, monkeypatch, size=(120, 24)) as session:
        panel = session.main.query_one(BtwPanel)
        session.chat.load_text("/btw deny this prompt")
        session.bar.submit_input()
        await wait_for(
            lambda: panel.display and panel.query_one("#btw-error").display,
            pilot=session.pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        assert not side.calls
        assert not panel.running
        assert "side rejected" in str(panel.query_one("#btw-error", Static).content)
        if long_reason:
            output = panel.query_one("#btw-output-page")
            error = panel.query_one("#btw-error")
            await wait_for(lambda: screen_is_settled(session.app, session.main), pilot=session.pilot)
            assert output.max_scroll_y > 0
            assert "side rejected line 30" not in "\n".join(
                strip.text for strip in session.main._compositor.render_strips()
            )
            # Wheel events on the text bubble to the output scroll container.
            for _ in range(20):
                error.post_message(make_wheel(error, up=False))
            await wait_for(lambda: output.scroll_y == output.max_scroll_y, pilot=session.pilot)
            assert "side rejected line 30" in "\n".join(
                strip.text for strip in session.main._compositor.render_strips()
            )
            output.focus()
            await session.pilot.press("home")
            await wait_for(lambda: output.scroll_y == 0, pilot=session.pilot)
            await session.pilot.press("end")
            await wait_for(lambda: output.scroll_y == output.max_scroll_y, pilot=session.pilot)
            assert "side rejected line 30" in "\n".join(
                strip.text for strip in session.main._compositor.render_strips()
            )
        session.chat.load_text("/btw allowed prompt")
        session.bar.submit_input()
        await wait_for(
            lambda: str(panel.query_one("#btw-position", Static).content) == "1/1",
            pilot=session.pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        assert len(side.calls) == 1
        assert any("side-only note" in note for note in side.reminders[0])
        assert session.seen.of(ev.UserMessage) == []
        assert session.engine.history_messages == []


async def test_render_failure_releases_side_card_running_state(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    side = SideAnswer(answer="broken render")
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        panel = session.main.query_one(BtwPanel)
        markdown = panel.query_one(VirtualizedMarkdown)
        update = markdown.update

        def fail_answer(text: str):
            if text == "broken render":
                raise ValueError("render failed")
            return update(text)

        monkeypatch.setattr(markdown, "update", fail_answer)
        session.chat.load_text("/btw first")
        session.bar.submit_input()
        await wait_for(lambda: panel.display and panel.query_one("#btw-error").display, pilot=session.pilot)
        assert not panel.running
        assert not panel.query_one(BtwThinking)._animating
        assert panel.gc_freeze_block_reason() is None
        side.answer = "rendered answer"
        session.chat.load_text("/btw second")
        session.bar.submit_input()
        await wait_for(lambda: markdown.display and markdown.source == "rendered answer", pilot=session.pilot)
        assert len(side.calls) == 2


async def test_history_request_without_backend_uses_unavailable_notice(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path)
    async with app.run_test(notifications=True) as pilot:
        panel = app.screen.query_one(BtwPanel)
        app.screen._btw.open_history("not-ready")
        await wait_for(lambda: any("unavailable" in message for _, message in notices(app)), pilot=pilot)
        assert not panel.display
        assert not any("could not be read" in message for _, message in notices(app))


@pytest.mark.parametrize("change", ["unloaded", "running"])
async def test_fork_rechecks_state_after_side_history_wait(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()

    async def held_history() -> bool:
        entered.set()
        await release.wait()
        return True

    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        monkeypatch.setattr(session.engine._side_questions, "has_history", held_history)
        fork = asyncio.create_task(session.bus.publish(ev.SessionFork(session_id=session.session_id())))
        try:
            await wait_for(lambda: entered.is_set() or fork.done(), pilot=session.pilot)
            assert entered.is_set()
            assert not fork.done()
            with monkeypatch.context() as patcher:
                if change == "unloaded":
                    patcher.setattr(session.engine.current, "loaded", None)
                else:
                    patcher.setattr(session.engine._lifecycle._fsm, "is_running", lambda: True)
                release.set()
                await fork
        finally:
            release.set()
            await asyncio.gather(fork, return_exceptions=True)
        expected = "session_fork_not_ready" if change == "unloaded" else "session_fork_busy"
        assert any(error.code == expected for error in session.seen.of(ev.Error))
        assert len(await JsonFileStateStore(tmp_path / "sessions").list_sessions()) == 0


@pytest.mark.parametrize("decision", ["continue", "decline", "close"])
async def test_side_question_hook_confirmation_and_cancellation(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    from chrys.app.tui.screens.main.hook_confirmation_dialog import HookConfirmationDialog
    from tests.orchestration.test_session_host_hook_ask import _write_ask_hook

    await asyncio.to_thread(_write_ask_hook)
    side = SideAnswer()
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        panel = session.main.query_one(BtwPanel)
        session.chat.load_text("/btw Check first")
        session.bar.submit_input()
        await wait_for(
            lambda: isinstance(session.app.screen, HookConfirmationDialog),
            pilot=session.pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        assert not side.calls
        if decision == "continue":
            await click_when_settled(session.pilot, "#confirm-yes")
            await wait_for(lambda: str(panel.query_one("#btw-position", Static).content) == "1/1", pilot=session.pilot)
            assert len(side.calls) == 1
        elif decision == "decline":
            await click_when_settled(session.pilot, "#confirm-no")
            await wait_for(lambda: panel.query_one("#btw-error").display, pilot=session.pilot)
            assert not side.calls and not panel.running
        else:
            session.main._btw.close_question(focus=False)
            await wait_for(lambda: session.app.screen is session.main, pilot=session.pilot)
            await asyncio.gather(
                *tuple(session.engine._side_questions._run_tasks | session.engine._side_questions._commit_tasks)
            )
            assert not side.calls and not panel.running and not panel.display


def _write_held_side_hook(entered: Path, release: Path) -> None:
    import sys
    import textwrap

    from chrys.foundation.platform import get_platform
    from tests.service.hooks.test_run_log import _held

    script = textwrap.dedent(
        f"""
        import json, os
        from pathlib import Path
        with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"], encoding="utf-8") as f:
            payload = json.load(f)
        if payload.get("side_question"):
            entered = Path({str(entered)!r})
            temporary = entered.with_name(entered.name + ".tmp")
            temporary.write_text(str(os.getpid()), encoding="utf-8")
            os.replace(temporary, entered)
        """
    ) + textwrap.indent(_held(release), "    ")
    directory = get_platform().config_dir / "hooks"
    directory.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 1,
        "hooks": [
            {
                "id": "held-side-question",
                "event": "user_prompt_submit",
                "run": {"type": "command", "argv": [sys.executable, "-c", script]},
                "execution": {"mode": "blocking", "timeout_seconds": 30},
            }
        ],
    }
    (directory / "hooks.json").write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize("transition", ["typed_resume", "ask_restore", "workspace"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_hook_drain_fails_side_question_without_blocking_or_calling_model(
    agent_engine: AgentEngineFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, transition: str
) -> None:
    import psutil

    from chrys.app.tui.screens.main.hook_confirmation_dialog import HookConfirmationDialog
    from tests.orchestration.test_session_host_hook_ask import _write_ask_hook

    for name in ("HOME", "USERPROFILE", "APPDATA"):
        monkeypatch.setenv(name, str(tmp_path))
    entered, release = tmp_path / "hook-entered", tmp_path / "hook-release"
    other = tmp_path / "other-workspace"
    await asyncio.to_thread(other.mkdir)
    if transition == "ask_restore":
        await asyncio.to_thread(_write_ask_hook)
    else:
        await asyncio.to_thread(_write_held_side_hook, entered, release)
    side = SideAnswer()
    side.release.set()
    monkeypatch.setattr(side_questions, "answer_side_question", side)
    # Bind the test model to the agent so settings reloads keep it configured.
    async with running_session(
        agent_engine, tmp_path, monkeypatch, agents=(make_agent(ALPHA, model=MAIN_MODEL),)
    ) as session:
        session.chat.load_text("Main task")
        session.bar.submit_input()
        if transition == "ask_restore":
            await wait_for(
                lambda: isinstance(session.app.screen, HookConfirmationDialog) and session.app.screen.is_mounted,
                pilot=session.pilot,
                timeout=ENGINE_TURN_TIMEOUT,
            )
            await click_when_settled(session.pilot, "#confirm-yes")
        await wait_for(lambda: session.model.call_count == 1, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT)
        await turn_finished(session)
        session_id = session.session_id()
        owner = session.engine._side_questions
        manager = session.engine.session.hook_manager
        assert manager is not None
        panel = session.main.query_one(BtwPanel)
        session.chat.load_text("/btw Check first")
        session.bar.submit_input()
        transition_task = None
        try:
            await wait_for(lambda: owner._active is not None, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT)
            active = owner._active
            assert active is not None and active.task is not None
            task = active.task

            def hook_waiting() -> bool:
                if transition == "ask_restore":
                    return isinstance(session.app.screen, HookConfirmationDialog) and session.app.screen.is_mounted
                return entered.exists()

            await wait_for(lambda: hook_waiting() or task.done(), pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT)
            if task.done():
                await task
            assert hook_waiting()
            assert not task.done()
            assert panel.display and panel.running
            assert not side.calls
            process = None
            if transition != "ask_restore":
                process = psutil.Process(int(await asyncio.to_thread(entered.read_text, encoding="utf-8")))

            if transition == "typed_resume":
                session.chat.load_text("/resume")
                session.bar.submit_input()
            else:
                event = (
                    ev.SessionRestore(session_id=session_id)
                    if transition == "ask_restore"
                    else ev.WorkspaceChange(primary_cwd=str(other))
                )
                transition_task = asyncio.create_task(session.bus.publish(event))
            await wait_for(
                lambda: active.operation.state == "invalidated" or task.done(),
                pilot=session.pilot,
                timeout=ENGINE_TURN_TIMEOUT,
            )
            assert active.operation.state == "invalidated"
            await asyncio.to_thread(release.touch)
            await wait_for(task.done, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT)
            if transition == "workspace":
                await task
            else:
                with pytest.raises(asyncio.CancelledError):
                    await task
            if transition_task is not None:
                await wait_for(transition_task.done, pilot=session.pilot, timeout=ENGINE_TURN_TIMEOUT)
                await transition_task
            await wait_for(
                lambda: (
                    session.app.screen is session.main
                    and not session.bar.agent_loading
                    and not session.engine._permits.agent_loading
                    and session.engine.session.hook_manager is not None
                    and session.engine.session.hook_manager is not manager
                ),
                pilot=session.pilot,
                timeout=ENGINE_TURN_TIMEOUT,
            )
            if process is not None:
                assert not process.is_running()
            assert session.engine.session_id == session_id
            assert session.model.call_count == 1 and not side.calls
            assert not panel.running
            error = panel.query_one("#btw-error", Static)
            if transition == "workspace":
                assert panel.display and error.display
                assert str(error.content) == "The side question could not finish. Close it and ask again."
            else:
                assert not panel.display and not error.display
            history = ev.SideQuestionHistoryRequested(session_id=session_id)
            await session.bus.publish(history)
            assert history.accepted and history.entries == ()
            directory = JsonFileStateStore(tmp_path / "sessions").session_dir(session_id)
            assert not (directory / BTW_FILE_NAME).exists()

            session.chat.load_text("/btw Ask again")
            session.bar.submit_input()
            assert session.chat.text == ""
            if transition == "ask_restore":
                await wait_for(
                    lambda: isinstance(session.app.screen, HookConfirmationDialog) and session.app.screen.is_mounted,
                    pilot=session.pilot,
                    timeout=ENGINE_TURN_TIMEOUT,
                )
                await click_when_settled(session.pilot, "#confirm-yes")
            await wait_for(
                lambda: str(panel.query_one("#btw-position", Static).content) == "1/1",
                pilot=session.pilot,
                timeout=ENGINE_TURN_TIMEOUT,
            )
            await asyncio.gather(*tuple(owner._run_tasks | owner._commit_tasks))
            assert [question for question, _ in side.calls] == ["Ask again"]
            assert not panel.running and not error.display
            assert panel.query_one(VirtualizedMarkdown).source == side.answer
        finally:
            await asyncio.to_thread(release.touch)
            if transition_task is not None:
                await asyncio.gather(transition_task, return_exceptions=True)
