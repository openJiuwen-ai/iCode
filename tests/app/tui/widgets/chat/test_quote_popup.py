# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The drag-selection actions for copying text or quoting it in a message."""

from __future__ import annotations

from asyncio import Event, create_task
from pathlib import Path

import pytest
from textual.events import MouseDown, MouseUp
from textual.geometry import Offset, Region
from textual.pilot import Pilot
from textual.scrollbar import ScrollTo
from textual.selection import SELECT_ALL, SelectEnd, Selection, SelectStart, SelectState
from textual.widget import Widget
from textual.widgets import Static, TextArea

import chrys.app.tui.behaviors.right_click_copy as right_click_copy
from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.clipboard import ClipboardApp
from chrys.app.tui.widgets.btw import BtwAnswer, BtwPage, BtwPanel, BtwQuestion
from chrys.app.tui.widgets.chat.context_fold import ContextFoldWidget
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    MessageCopyButton,
    SystemMessage,
    ThinkingMessage,
    UserMessage,
)
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.quote_popup import QuoteSelectionPopup
from chrys.app.tui.widgets.chrome.app_header import AppHeader
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.models.side_questions import SideQuestionEntry
from chrys.foundation.text.draft_references import (
    EMPTY_REFERENCES,
    DraftReference,
    expand_references,
)
from tests.app.tui.widgets.chat.quote_popup_fixtures import _MainScreenApp, _open_history_turn
from tests.app.tui.widgets.chat.replay_fixtures import _turns
from tests.support.pilot_barrier import screen_is_settled
from tests.support.toasts import shown_toasts
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled, rich_plain
from tests.support.waiting import wait_for

_LINE_START = Offset(0, 0)


async def test_drag_selection_does_not_extend_into_hidden_history_during_a_cold_scroll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(120))
        user = panel.query_one("#turn-120", UserMessage).query_one(VirtualizedMarkdown)
        answer = panel.query(AgentMessage).last().query_one(VirtualizedMarkdown)
        await wait_for(lambda: user.region.overlaps(panel.content_region), pilot=pilot)
        state = _chat_drag_state(user, answer, end_offset=Offset(len("answer 119"), 0))
        screen._select_state = state
        await pilot.pause()
        selected = screen.get_selected_text()
        assert screen._selecting and selected

        monkeypatch.setattr(panel._history_scroll, "request_widgets", lambda *_: None)
        panel.release_anchor()
        panel.vertical_scrollbar.post_message(ScrollTo(y=panel.scroll_y / 2, animate=False))
        await wait_for(lambda: panel._history_scroll.pending is not None, pilot=pilot)
        assert user.region.overlaps(panel.content_region)
        assert answer.region.overlaps(panel.content_region)

        assert screen._select_state is state
        assert screen.get_selected_text() == selected


async def test_selecting_the_visible_tail_keeps_the_archive_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot, description="chat panel has a viewport")
        await panel.replay_history(_turns(120))
        user = panel.query_one("#turn-120", UserMessage).query_one(VirtualizedMarkdown)
        answer = panel.query(AgentMessage).last().query_one(VirtualizedMarkdown)
        await wait_for(
            lambda: user.region.overlaps(panel.content_region) and answer.region.overlaps(panel.content_region),
            pilot=pilot,
            description="both selection endpoints are visible",
        )
        assert panel.pending_transcript_widgets()
        request = panel._replay_mount.request_window
        transcript_seq = panel.transcript_seq
        cold = set(panel.pending_transcript_widgets())
        cold_order_reads = 0
        mount_requests = []

        def record_request(widgets, ready, **kwargs):
            mount_requests.append(len(widgets))
            request(widgets, ready, **kwargs)

        def record_transcript_seq(widget):
            nonlocal cold_order_reads
            if widget in cold:
                cold_order_reads += 1
            return transcript_seq(widget)

        monkeypatch.setattr(panel._replay_mount, "request_window", record_request)
        monkeypatch.setattr(panel, "transcript_seq", record_transcript_seq)
        screen._watch__select_state(_chat_drag_state(user, answer, end_offset=Offset(len("answer 119"), 0)))
        await pilot.pause()
        screen._watch__selecting(False)
        await pilot.pause()

        selected = screen.get_selected_text()
        assert selected is not None and "question 119" in selected and "answer 119" in selected
        assert mount_requests == []
        assert cold_order_reads == 0
        assert panel.pending_transcript_widgets()


@pytest.mark.parametrize("turn", [1, 120])
async def test_copying_from_chat_into_the_input_includes_only_the_selected_history(
    monkeypatch: pytest.MonkeyPatch, turn: int
) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot, description="chat panel has a viewport")
        await panel.replay_history(_turns(120))
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y,
            pilot=pilot,
            description="indexed tail has settled",
        )
        selector = f"#turn-{turn}"
        if turn == 1:
            panel.scroll_to_turn(selector[1:])
        await wait_for(
            lambda: bool(panel.query(selector)) and panel.query_one(selector).region.overlaps(panel.content_region),
            pilot=pilot,
            description="selection start mounted and visible",
        )
        first = panel.query_one(selector).query_one(VirtualizedMarkdown)
        await wait_for(
            lambda: first.region.overlaps(panel.content_region) and screen_is_settled(app, screen),
            pilot=pilot,
            description="selection start text has layout",
        )
        assert not panel.query("#turn-60")
        input_bar = screen.query_one(InputBar)
        end = Offset(first.region.x, input_bar.region.y)
        assert end.y >= panel.region.bottom
        state = _chat_drag_state(first, first, end_offset=Offset(1, 0))
        screen._watch__select_state(state.update_end(end, SelectEnd(input_bar, None, None)))
        screen._watch__selecting(False)

        copied: list[str] = []

        def record_copy(_app: ClipboardApp, text: str) -> bool:
            copied.append(text)
            return True

        monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", record_copy)
        screen.action_copy_text()
        await wait_for(lambda: bool(copied), pilot=pilot, description="copy of the cross-panel drag")
        assert f"question {turn - 1}" in copied[0]
        assert "answer 119" in copied[0]
        if turn == 120:
            assert not panel.query("#turn-60")
            return
        assert "question 59" in copied[0]


@pytest.mark.parametrize("towards_header", [True, False], ids=["to-header", "from-header"])
async def test_cross_panel_copy_keeps_the_bottom_selection_boundary_during_history_shrink(
    monkeypatch: pytest.MonkeyPatch,
    towards_header: bool,
) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot, description="chat panel has a viewport")
        messages = _turns(120)
        answers = [message for message in messages if message["role"] == "assistant"]
        url = "https://example.com/" + "a" * 550
        for index, message in enumerate(answers):
            label = "final reply outside the selection" if index == 119 else "short link"
            message["contents"] = [{"type": "text", "text": f"[{label}]({url})"}]
        await panel.replay_history(messages)
        leaf = panel.query_one("#turn-120").query_one(VirtualizedMarkdown)
        await wait_for(
            lambda: (
                not panel._history_scroll.restoring_tail
                and panel.scroll_y == panel.max_scroll_y
                and leaf.region.overlaps(panel.content_region)
                and screen_is_settled(app, screen)
            ),
            pilot=pilot,
            description="the last turn is visible at the anchored bottom",
        )
        assert panel._anchored and not panel._anchor_released
        assert not panel.query("#turn-1")
        header = screen.query_one(AppHeader)
        top = header.region.offset
        assert top.y < panel.region.y
        if towards_header:
            start = _chat_drag_state(leaf, leaf, start_offset=Offset(2, 0), end_offset=Offset(2, 0))
            state = start.update_end(top, SelectEnd(header, None, None))
        else:
            end = Offset(panel.region.x, leaf.content_region.y)
            state = SelectState(
                end,
                SelectStart(header, _LINE_START, top, header.scroll_offset, None, None),
                SelectEnd(panel, None, None),
            )
        copied: list[str] = []
        monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, text: copied.append(text) or True)
        screen._select_state = state
        screen._selecting = False
        screen.action_copy_text()

        await wait_for(lambda: bool(copied), pilot=pilot, description="cross-panel selection copied")
        assert "question 0" in copied[0] and "question 118" in copied[0]
        assert copied[0].splitlines()[-1] == ("qu" if towards_header else "question 119")
        assert "final reply outside the selection" not in copied[0]


@pytest.mark.parametrize("copy_first", [False, True], ids=["before-first-copy", "before-repeat-copy"])
@pytest.mark.parametrize("on_message", [False, True], ids=["panel-gap", "message-gap"])
async def test_copy_of_chat_gap_selection_survives_scroll(
    monkeypatch: pytest.MonkeyPatch, copy_first: bool, on_message: bool
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
        leaf = await _open_history_turn(pilot, panel, 20)
        header = screen.query_one(AppHeader)
        recorded: list[str] = []
        monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, s: recorded.append(s) or True)
        if on_message:
            prompt = panel.query_one("#turn-20", UserMessage)
            point = Offset(prompt.region.right - 1, prompt.region.y)
            assert screen.get_widget_and_offset_at(*point) == (prompt, None)
            end = SelectEnd(prompt, None, None)
        else:
            point = Offset(panel.region.x, leaf.content_region.y)
            end = SelectEnd(panel, None, None)
        state = SelectState(
            point,
            SelectStart(header, Offset(), header.region.offset, header.scroll_offset, None, None),
            end,
        )
        screen._select_state = state
        screen._selecting = False
        if copy_first:
            screen.action_copy_text()
            await wait_for(lambda: len(recorded) == 1, pilot=pilot)
        # Textual retires its drag state when the scrollbar is pressed.
        screen._select_state = None
        panel.jump_to_bottom()
        await wait_for(
            lambda: (
                panel.scroll_y == panel.max_scroll_y
                and not leaf.region.overlaps(panel.content_region)
                and screen._history_selection_panel is None
            ),
            pilot=pilot,
        )
        screen.action_copy_text()
        await wait_for(lambda: len(recorded) == (2 if copy_first else 1), pilot=pilot)
        assert "question 0" in recorded[-1] and "question 18" in recorded[-1] and "question 20" not in recorded[-1]
        assert "question 119" not in recorded[-1]
        assert ("question 19" in recorded[-1]) is not on_message
        assert recorded[0] == recorded[-1]


async def test_copy_offscreen_markdown_after_theme_change() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(120))
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        leaf = await _open_history_turn(pilot, panel, 20)
        screen._select_state = _chat_drag_state(leaf, leaf, end_offset=Offset(8, 0))
        screen._selecting = False
        original = screen.get_selected_text()
        assert original and "question" in original, original
        panel.jump_to_bottom()
        await wait_for(
            lambda: (
                panel.scroll_y == panel.max_scroll_y
                and not leaf.region.overlaps(panel.content_region)
                and screen_is_settled(app, screen)
            ),
            pilot=pilot,
        )
        assert screen.get_selected_text() == original
        app.theme = "textual-light"
        await pilot.pause()
        await wait_for(lambda: screen_is_settled(app, screen), pilot=pilot)
        copied = screen.get_selected_text()
        assert copied == original


async def test_copy_from_a_bottom_gap_includes_the_cold_page_below(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(120, 32)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(120))
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        await _open_history_turn(pilot, panel, 20)
        viewport = panel._history_viewport
        assert viewport is not None and panel._history_window is not None
        stop = panel._history_window[1]
        before, after = stop * 4 - 1, stop * 4
        assert not panel.query(f"#turn-{after + 1}")
        panel.scroll_to(
            y=viewport._document_top + viewport._bounds.start(stop) - panel.content_region.height,
            animate=False,
            immediate=True,
        )
        await wait_for(lambda: screen_is_settled(app, screen), pilot=pilot)
        assert not panel.query(f"#turn-{after + 1}")
        point = Offset(panel.content_region.x, panel.content_region.bottom - 1)
        assert screen.get_widget_and_offset_at(*point) == (panel, None)
        input_bar = screen.query_one(InputBar)
        screen._select_state = SelectState(
            point,
            SelectStart(input_bar, Offset(), input_bar.region.offset, input_bar.scroll_offset, None, None),
            SelectEnd(panel, None, None),
        )
        screen._selecting = False
        screen._select_state = None
        panel.jump_to_bottom()
        await wait_for(lambda: panel.scroll_y == panel.max_scroll_y, pilot=pilot)

        copied: list[str] = []
        monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, text: copied.append(text) or True)
        screen.action_copy_text()
        await wait_for(lambda: bool(copied), pilot=pilot, description="copy includes the cold page below the gap")
        assert f"question {before}" not in copied[0]
        assert f"question {after}" in copied[0] and "question 119" in copied[0]


@pytest.mark.parametrize("start_on_notice", [False, True], ids=["gap", "notice"])
async def test_copy_from_chat_keeps_its_boundary_when_replay_heights_shrink(
    monkeypatch: pytest.MonkeyPatch,
    start_on_notice: bool,
) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot, description="chat panel has a viewport")
        messages = _turns(120)
        url = "https://example.com/" + "a" * 550
        for message in messages:
            if message["role"] == "assistant":
                message["contents"] = [{"type": "text", "text": f"[short link]({url})"}]
        notice = SystemMessage("archive boundary notice") if start_on_notice else None
        if notice is not None:
            mount = panel.mount_transcript_widgets

            async def mount_with_notice(widgets, **kwargs):
                opener = next(i for i, widget in enumerate(widgets) if widget.id == "turn-80")
                widgets.insert(opener + 1, notice)
                await mount(widgets, **kwargs)

            monkeypatch.setattr(panel, "mount_transcript_widgets", mount_with_notice)
        await panel.replay_history(messages)
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y,
            pilot=pilot,
            description="indexed tail has settled",
        )
        panel.scroll_to_turn("turn-80")
        await wait_for(
            lambda: (
                bool(panel.query("#turn-80"))
                and panel.query_one("#turn-80").region.overlaps(panel.content_region)
                and screen_is_settled(app, screen)
            ),
            pilot=pilot,
            description="the first selected turn has layout",
        )
        first_leaf = notice if notice is not None else panel.query_one("#turn-80").query_one(VirtualizedMarkdown)
        start_point = (
            first_leaf.content_region.offset
            if notice is not None
            else Offset(panel.region.x, first_leaf.content_region.y)
        )
        assert start_point.y >= panel.region.y
        input_bar = screen.query_one(InputBar)
        end_point = Offset(start_point.x, input_bar.region.y)
        state = SelectState(
            end_point,
            SelectStart(
                panel,
                start_point - panel.region.offset,
                panel.region.offset,
                panel.scroll_offset,
                notice,
                _LINE_START if notice is not None else None,
            ),
            SelectEnd(input_bar, None, None),
        )
        before_height = panel.virtual_size.height
        assert not panel.query("#turn-1")
        screen._watch__select_state(state)
        assert first_leaf in screen.selections
        screen._watch__selecting(False)

        copied: list[str] = []

        def record_copy(_app: ClipboardApp, text: str) -> bool:
            copied.append(text)
            return True

        monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", record_copy)
        screen.action_copy_text()
        await wait_for(lambda: bool(copied), pilot=pilot, description="copy of a chat-to-input drag")
        assert panel.virtual_size.height < before_height
        if start_on_notice:
            assert copied[0].splitlines()[0].endswith("archive boundary notice")
            assert "question 79" not in copied[0]
        else:
            assert copied[0].splitlines()[0] == "question 79"
        assert "question 78" not in copied[0]
        assert "question 119" in copied[0]

        panel.scroll_to_turn("turn-20")
        await wait_for(
            lambda: panel._history_window is not None and not first_leaf.region.overlaps(panel.content_region),
            pilot=pilot,
            description="exact layout released and the drag's start has scrolled out",
        )
        screen.action_copy_text()
        await wait_for(lambda: len(copied) == 2, pilot=pilot, description="second copy of the same selection")
        assert copied[1] == copied[0]


async def test_copy_from_leading_summary_through_cold_history(monkeypatch: pytest.MonkeyPatch) -> None:
    summary = "Summary of the earlier conversation"
    app = _MainScreenApp()
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot, description="chat panel has a viewport")
        await panel.replay_history(_turns(104, leading_summary=summary))
        await wait_for(lambda: not panel._history_scroll.restoring_tail, pilot=pilot)
        assert panel._replay_mount.windowed
        fold = panel.query_one(ContextFoldWidget)
        panel.action_scroll_home()
        fold.collapsed = False
        first = fold.query_one("#summary", Static)
        await wait_for(lambda: first.region.overlaps(panel.content_region), pilot=pilot)

        panel.scroll_to_turn("turn-61")
        await wait_for(
            lambda: bool(panel.query("#turn-61")) and panel.query_one("#turn-61").region.overlaps(panel.content_region),
            pilot=pilot,
        )
        last = panel.query_one("#turn-61").query_one(VirtualizedMarkdown)
        assert not panel.query("#turn-40")
        copied: list[str] = []

        def record_copy(_app: ClipboardApp, text: str) -> bool:
            copied.append(text)
            return True

        monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", record_copy)
        screen._select_state = _chat_drag_state(first, last, end_offset=Offset(len("question 60"), 0))
        assert screen._selecting
        screen._selecting = False
        screen.action_copy_text()
        await wait_for(lambda: bool(copied), pilot=pilot, description="copy including the leading summary")
        text = copied[0]
        assert text.index(summary) < text.index("question 0") < text.index("question 39") < text.index("question 60")
        assert "question 61" not in text
        assert not panel.query("#turn-80")


@pytest.mark.parametrize(
    "action", ["clear", "scroll", "copy", "copy_then_scroll", "repeat_copy", "scrollbar", "retry", "detach"]
)
async def test_history_selection_preparation_follows_the_remaining_actions(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot, description="chat panel has a viewport")
        await panel.replay_history(_turns(120))
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y,
            pilot=pilot,
            description="indexed tail has settled",
        )
        leaves = []
        for turn in ("turn-17", "turn-61"):
            panel.scroll_to_turn(turn)
            await wait_for(
                lambda turn=turn: (
                    bool(panel.query(f"#{turn}")) and panel.query_one(f"#{turn}").region.overlaps(panel.content_region)
                ),
                pilot=pilot,
                description=f"{turn} is mounted and visible",
            )
            leaves.append(panel.query_one(f"#{turn}").query_one(VirtualizedMarkdown))
        first, last = leaves
        assert not panel.query("#turn-90")

        entered, release = Event(), Event()
        mount = panel.mount_replay_batch
        prepare = panel.prepare_history_selection
        mounted_batches = 0
        preparations = 0

        async def gated_mount(batch, *, before):
            nonlocal mounted_batches
            mounted_batches += 1
            entered.set()
            await release.wait()
            if action == "retry":
                raise RuntimeError("test history mount interrupted")
            await mount(batch, before=before)

        async def record_preparation() -> None:
            nonlocal preparations
            preparations += 1
            await prepare()

        copied: list[str] = []

        def record_copy(_app: ClipboardApp, text: str) -> bool:
            copied.append(text)
            return True

        monkeypatch.setattr(panel, "mount_replay_batch", gated_mount)
        monkeypatch.setattr(panel, "prepare_history_selection", record_preparation)
        monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", record_copy)
        if action == "copy_then_scroll":
            popup = screen.query_one(QuoteSelectionPopup)
            place_near = popup.place_near

            def place_while_layout_is_dirty(pointer: Offset, *, within: Region | None = None) -> None:
                # The real popup reflows the screen while completing the queued actions.
                panel.refresh(layout=True)
                place_near(pointer, within=within)

            monkeypatch.setattr(popup, "place_near", place_while_layout_is_dirty)
        try:
            screen._select_state = _chat_drag_state(first, last, end_offset=Offset(len("question 60"), 0))
            assert screen._selecting
            screen._selecting = False
            if action == "scrollbar":
                screen._select_state = None
            if action in ("copy", "copy_then_scroll", "repeat_copy", "scrollbar", "detach"):
                screen.action_copy_text()
            await wait_for(entered.is_set, pilot=pilot, description="preparing the selection's cold history")
            if action == "repeat_copy":
                screen.action_copy_text()
            if action == "clear":
                screen.clear_selection()
            elif action == "detach":
                await first.remove()
                assert not first.is_attached
            elif action not in ("copy_then_scroll", "retry"):
                panel.jump_to_bottom()
                await wait_for(
                    lambda: not last.region.overlaps(panel.content_region),
                    pilot=pilot,
                    description="the selection is outside the viewport",
                )
            await pilot.pause()
        finally:
            release.set()
        await wait_for(
            lambda: not panel._replay_mount.in_progress,
            pilot=pilot,
            description="history preparation has ended",
        )
        if action == "retry":
            await wait_for(
                lambda: not panel._history_selection,
                pilot=pilot,
                description="a failed preparation released exact layout",
            )
            assert mounted_batches == 1 and panel._replay_mount.has_pending
            monkeypatch.setattr(panel, "mount_replay_batch", mount)
            screen.action_copy_text()
        if action in ("copy", "copy_then_scroll", "repeat_copy", "scrollbar", "retry"):
            await wait_for(lambda: bool(copied), pilot=pilot, description="clipboard request finished")
            assert preparations == (2 if action == "retry" else 1)
            assert len(copied) == 1
            assert "question 36" in copied[0] and "question 119" not in copied[0]
            assert not panel.query("#turn-90")
            if action == "copy_then_scroll":
                assert panel._history_selection
                assert last.region.overlaps(panel.content_region)
                assert screen.query_one(QuoteSelectionPopup).is_shown
                panel.jump_to_bottom()
                await wait_for(
                    lambda: not last.region.overlaps(panel.content_region),
                    pilot=pilot,
                    description="the completed selection scrolled out of the viewport",
                )
            if action in ("copy", "copy_then_scroll", "repeat_copy", "scrollbar"):
                await wait_for(
                    lambda: not panel._history_selection and panel._history_window is not None,
                    pilot=pilot,
                    description="offscreen copy released the full layout",
                )
        else:
            assert mounted_batches == 1
            assert panel._replay_mount.has_pending
            assert not panel.query("#turn-36")
            assert copied == []
        assert not screen.query_one(QuoteSelectionPopup).is_shown


@pytest.mark.parametrize(
    "page_away, tall_estimate, detach_anchor",
    [(False, False, False), (True, False, False), (False, True, False), (False, False, True)],
)
async def test_copy_and_quote_wait_for_history_between_two_mounted_pages(
    monkeypatch: pytest.MonkeyPatch, page_away: bool, tall_estimate: bool, detach_anchor: bool
) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        cp = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(cp.size), pilot=pilot, description="chat panel has a viewport")
        messages = _turns(120)
        if tall_estimate:
            url = "https://example.com/" + "a" * 550
            for message in messages:
                if message["role"] == "assistant":
                    message["contents"] = [{"type": "text", "text": f"[short link]({url})"}]
        await cp.replay_history(messages)
        assert cp._replay_mount.windowed
        await wait_for(
            lambda: not cp._history_scroll.restoring_tail and cp.scroll_y == cp.max_scroll_y,
            pilot=pilot,
            description="indexed tail has settled",
        )
        cp.scroll_to_turn("turn-1")
        await wait_for(
            lambda: bool(cp.query("#turn-1")) and cp.query_one("#turn-1").region.overlaps(cp.content_region),
            pilot=pilot,
            description="oldest turn mounted",
        )
        first = cp.query_one("#turn-1").query_one(VirtualizedMarkdown)
        cp.jump_to_bottom()
        await wait_for(
            lambda: cp.query_one("#turn-120").region.overlaps(cp.content_region),
            pilot=pilot,
            description="newest turn visible",
        )
        last = cp.query_one("#turn-120").query_one(VirtualizedMarkdown)
        assert not cp.query("#turn-60")
        estimated_height = cp.virtual_size.height
        original_mount = cp.mount_replay_batch
        ready = Event()
        entered = Event()
        copied: list[str] = []

        async def delay_mount(batch, *, before):
            entered.set()
            await ready.wait()
            await original_mount(batch, before=before)

        monkeypatch.setattr(cp, "mount_replay_batch", delay_mount)

        def record_copy(_app: ClipboardApp, text: str) -> bool:
            copied.append(text)
            return True

        monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", record_copy)
        monkeypatch.setattr("chrys.app.tui.screens.main.view_adapter.copy_text_to_clipboards", record_copy)
        monkeypatch.setattr("chrys.app.tui.clipboard.uses_native_windows_clipboard", lambda: False)

        try:
            screen._watch__select_state(_chat_drag_state(first, last, end_offset=Offset(len("question 119"), 0)))
            screen._watch__selecting(False)
            screen.action_copy_text()
            await wait_for(entered.is_set, pilot=pilot, description="cold pages are still loading")
            assert screen.get_selected_text() is None
            assert screen._pending_quote_pick is None
            assert copied == []
            if page_away:
                original_y = cp.scroll_y
                cp.user_scroll_page(-1)
                await wait_for(lambda: cp.scroll_y < original_y, pilot=pilot, description="scrolled while loading")
        finally:
            ready.set()

        await wait_for(
            lambda: bool(copied) and (page_away or screen._pending_quote_pick is not None),
            pilot=pilot,
            description="whole range available to clipboard and the quote pill",
        )
        assert "question 60" in copied[0]
        if tall_estimate:
            assert cp.virtual_size.height < estimated_height
        popup = screen.query_one(QuoteSelectionPopup)
        if page_away:
            assert screen._pending_quote_pick is None
            assert not popup.is_shown
            return
        text = copied[0]
        assert screen._pending_quote_pick is not None
        assert screen._pending_quote_pick[0] == text
        await wait_for(lambda: popup.is_shown and popup.region == popup.target_region, pilot=pilot)
        if detach_anchor:
            # A renderer collapse can remove the last selected body before
            # the screen receives the click that clears its highlight.
            screen.selections = {first: SELECT_ALL}
            await pilot.pause()
            await first.remove()
            assert not first.is_attached
            assert cp._history_selection
            screen.clear_selection()
            assert not cp._history_selection
            return
        await _click_pill(app, pilot, popup, action="copy")
        await wait_for(lambda: len(copied) == 2, pilot=pilot, description="the popup copied the full range")
        assert copied == [text, text]


async def test_copy_from_chat_into_input_leaves_unselected_history_cold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot, description="chat panel has a viewport")
        await panel.replay_history(_turns(120, with_tools=True))
        await wait_for(
            lambda: (
                not panel._history_scroll.restoring_tail
                and panel.scroll_y == panel.max_scroll_y
                and screen_is_settled(app, screen)
            ),
            pilot=pilot,
            description="indexed tail has settled",
        )
        first = await _open_history_turn(pilot, panel, 72)
        assert not panel.query("#turn-1")
        assert not panel.query("#turn-90")

        editor = screen.query_one("#chat-input", TextArea)
        start = (first.content_region.x, first.content_region.y)
        end = (start[0], editor.content_region.y)
        assert end[1] >= panel.region.bottom

        copied: list[str] = []

        def record_copy(_app: ClipboardApp, text: str) -> bool:
            copied.append(text)
            return True

        monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", record_copy)
        await pilot.mouse_down(offset=start)
        await pilot.hover(offset=end)
        await pilot.mouse_up(offset=end)
        popup = screen.query_one(QuoteSelectionPopup)
        await wait_for(lambda: popup.is_shown, pilot=pilot, description="the selected range is ready to quote")

        screen.action_copy_text()
        await wait_for(lambda: bool(copied), pilot=pilot, description="the selected range is copied")
        copied_lines = copied[0].splitlines()
        assert "question 71" in copied_lines
        assert "question 89" in copied_lines
        assert "answer 119" in copied_lines
        assert "question 70" not in copied_lines
        assert not panel.query("#turn-1")


async def test_copy_is_complete_while_other_replay_batches_are_pending(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(120, 36)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await wait_for(lambda: bool(panel.size), pilot=pilot)
        await panel.replay_history(_turns(120, with_tools=True))
        await wait_for(
            lambda: not panel._history_scroll.restoring_tail and panel.scroll_y == panel.max_scroll_y, pilot=pilot
        )
        first = await _open_history_turn(pilot, panel, 72)
        page_start = panel.transcript_seq(panel.query_one("#turn-69"))
        assert page_start is not None
        assert not panel.query("#turn-90")

        selection_entered, selection_release = Event(), Event()
        drain_started, drain_entered, drain_release = Event(), Event(), Event()
        mount = panel.mount_replay_batch

        async def gate_batch(batch: list[Widget], *, before: Widget | None) -> None:
            if not selection_entered.is_set():
                selection_entered.set()
                await selection_release.wait()
            last = panel.transcript_seq(batch[-1])
            assert last is not None
            if last < page_start:
                drain_entered.set()
                await drain_release.wait()
            await mount(batch, before=before)

        async def drain_other_history() -> None:
            drain_started.set()
            await panel.wait_replay_complete()

        copied: list[str] = []
        monkeypatch.setattr(panel, "mount_replay_batch", gate_batch)
        monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", lambda _app, text: copied.append(text) or True)

        drain = None
        try:
            editor = screen.query_one("#chat-input", TextArea)
            start = (first.content_region.x, first.content_region.y)
            end = (start[0], editor.content_region.y)
            await pilot.mouse_down(offset=start)
            await pilot.hover(offset=end)
            await pilot.mouse_up(offset=end)
            await wait_for(selection_entered.is_set, pilot=pilot)
            drain = create_task(drain_other_history())
            await drain_started.wait()
            selection_release.set()

            await wait_for(drain_entered.is_set, pilot=pilot)
            await wait_for(lambda: not panel._history_selection_needs_layout, pilot=pilot)
            screen.action_copy_text()
            await wait_for(lambda: bool(copied), pilot=pilot)
            assert "question 89" in copied[0].splitlines()
            assert "answer 119" in copied[0].splitlines()
            assert not panel.query("#turn-1")
        finally:
            selection_release.set()
            drain_release.set()
            if drain is not None:
                await drain


def _chat_drag_state(
    start: Widget, end: VirtualizedMarkdown, *, end_offset: Offset, start_offset: Offset = _LINE_START
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


async def _click_pill(
    app: _MainScreenApp, pilot: Pilot[None], popup: QuoteSelectionPopup, *, action: str = "quote"
) -> None:
    button = popup.query_one(f".-{action}")
    click_x = button.region.x + 1
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
@pytest.mark.parametrize("source", ["chat", "card"])
async def test_copy_preserves_the_selected_text_and_draft(source: str, monkeypatch: pytest.MonkeyPatch) -> None:
    copied: list[str] = []

    def record_copy(_app: ClipboardApp, text: str) -> bool:
        copied.append(text)
        return True

    monkeypatch.setattr("chrys.app.tui.screens.main.view_adapter.copy_text_to_clipboards", record_copy)
    monkeypatch.setattr("chrys.app.tui.clipboard.uses_native_windows_clipboard", lambda: False)
    app = _MainScreenApp()
    async with app.run_test(size=(100, 30)) as pilot:
        screen = app.main_screen
        text = "Before copy this after"
        if source == "chat":
            body = await _panel_with_agent_message(app, pilot, text)
        else:
            body = _side_body(await _side_card(app, pilot, "Question", text), "answer")
        input_bar = screen.query_one(InputBar)
        input_bar.value = "queued note"
        input_bar.lock_with_text()
        screen._watch__select_state(_chat_drag_state(body, body, start_offset=Offset(6, 0), end_offset=Offset(16, 0)))
        screen._watch__selecting(False)
        popup = screen.query_one(QuoteSelectionPopup)
        await wait_for(lambda: popup.is_shown and popup.region == popup.target_region, pilot=pilot)
        assert screen.get_selected_text() == " copy this "

        await _click_pill(app, pilot, popup, action="copy")
        await wait_for(lambda: any(toast.message == "Copied" for toast in shown_toasts(app)), pilot=pilot)

        assert copied == [" copy this "]
        assert screen.get_selected_text() is None
        assert input_bar.value == "queued note"
        assert not input_bar.draft().references


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


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [10, 18])
async def test_the_pill_keeps_both_labels_and_separate_hit_areas(width: int) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 24)) as pilot:
        popup = app.main_screen.query_one(QuoteSelectionPopup)
        await wait_for(lambda: screen_is_settled(app, app.main_screen), pilot=pilot)
        assert str(popup.styles.height) == "1"
        area = Region(1, 4, width, 3)
        popup.place_near(Offset(area.right - 1, area.bottom - 1), within=area)
        await wait_for(lambda: popup.is_shown and popup.region == popup.target_region, pilot=pilot)
        assert popup.region == Region(area.x, area.bottom - 2, width, 1)

        quote, divider, copy = popup.query(Static)
        assert tuple(rich_plain(widget.render()) for widget in (quote, divider, copy)) == ("Quote", "│", "Copy")
        assert tuple(widget.region.width for widget in (quote, divider, copy)) == (
            (5, 1, 4) if width == 10 else (9, 1, 8)
        )
        for widget in (quote, divider, copy):
            assert app.main_screen.get_widget_at(*widget.region.offset)[0] is widget
            assert not widget.allow_select

        await pilot.hover(offset=tuple(copy.region.offset))
        assert copy.mouse_hover
        assert not quote.mouse_hover
        assert copy.styles.color != quote.styles.color
        assert copy.styles.background != quote.styles.background


@pytest.mark.asyncio
@pytest.mark.parametrize(("width", "source", "popup_width"), [(9, "chat", 0), (12, "chat", 10), (15, "card", 13)])
async def test_selection_actions_fit_the_terminal_viewport(width: int, source: str, popup_width: int) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(width, 18)) as pilot:
        screen = app.main_screen
        text = "select this text"
        if source == "chat":
            body = await _panel_with_agent_message(app, pilot, text)
        else:
            body = _side_body(await _side_card(app, pilot, "Question", text), "answer")
        start = tuple(body.region.offset)
        end = (start[0] + 3, start[1])
        await pilot.mouse_down(offset=start)
        await pilot.hover(offset=end)
        await pilot.mouse_up(offset=end)
        await wait_for(lambda: screen.get_selected_text() == "sele" and not screen._selecting, pilot=pilot)

        popup = screen.query_one(QuoteSelectionPopup)
        if popup_width == 0:
            assert not popup.is_shown
        else:
            assert popup.is_shown
            assert popup.region.width == popup_width
            assert Region(0, 0, *screen.size).contains_region(popup.region)
            copy = popup.query_one(".-copy")
            assert screen.get_widget_at(copy.region.right - 1, copy.region.y)[0] is copy


@pytest.mark.asyncio
async def test_ctrl_r_hides_selection_actions_before_history_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    release_history = Event()
    prompts = [f"Previously entered prompt {index} for this test." for index in range(20)]

    async def load_history(_input_bar: InputBar, *, max_entries: int) -> list[str]:
        await release_history.wait()
        return prompts

    copied: list[str] = []

    def record_copy(_app: ClipboardApp, text: str) -> bool:
        copied.append(text)
        return True

    monkeypatch.setattr(InputBar, "load_prompt_history", load_history)
    monkeypatch.setattr("chrys.app.tui.screens.main.view_adapter.copy_text_to_clipboards", record_copy)
    app = _MainScreenApp()
    async with app.run_test(size=(100, 30)) as pilot:
        screen = app.main_screen
        message = "\n\n".join(f"Select some transcript text on line {index}." for index in range(18))
        body = await _panel_with_agent_message(app, pilot, message)
        input_bar = screen.query_one(InputBar)
        await wait_for(lambda: screen_is_settled(app, screen), pilot=pilot)
        x = body.region.x + 1
        y = next(
            row
            for row in range(input_bar.region.y - 9, input_bar.region.y - 5)
            if screen.get_widget_and_offset_at(x, row)[1] is not None
        )
        await pilot.mouse_down(offset=(x, y))
        await pilot.hover(offset=(x + 5, y))
        await pilot.mouse_up(offset=(x + 5, y))
        popup = screen.query_one(QuoteSelectionPopup)
        await wait_for(lambda: popup.is_shown and popup.region == popup.target_region, pilot=pilot)
        selected_text = screen.get_selected_text()
        assert selected_text
        selection = dict(screen.selections)
        former_copy_cell = popup.query_one(".-copy").region.offset

        suggestions = screen.query_one(SuggestionList)
        try:
            await pilot.press("ctrl+r")
            await wait_for(lambda: suggestions.is_visible and suggestions.is_loading, pilot=pilot)
            assert not popup.is_shown
            assert screen.get_selected_text() == selected_text
            assert screen.selections == selection
        finally:
            release_history.set()
        await wait_for(
            lambda: suggestions.is_visible and not suggestions.is_loading and suggestions._values, pilot=pilot
        )
        assert former_copy_cell in suggestions.content_region
        assert screen.get_widget_at(*former_copy_cell)[0] is suggestions
        assert screen.get_selected_text() == selected_text

        await click_when_settled(pilot, suggestions, offset=tuple(former_copy_cell - suggestions.region.offset))
        await wait_for(lambda: input_bar.value in prompts, pilot=pilot)
        assert copied == []


@pytest.mark.asyncio
@pytest.mark.parametrize("menu", ["suggestions", "subcommands"])
async def test_input_menus_hide_selection_actions_and_keep_them_hidden(menu: str) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 30)) as pilot:
        screen = app.main_screen
        text = "selected transcript text"
        body = await _panel_with_agent_message(app, pilot, text)
        popup = await _finish_drag_over(app, body, text)

        if menu == "subcommands":
            screen._view_adapter.show_subcommand_suggestions([("show", "Show")])
        else:
            screen._view_adapter.show_suggestions("commands", [("help", "Help")])

        assert not popup.is_shown
        assert screen.get_selected_text() == text

        screen._watch__select_state(_chat_drag_state(body, body, end_offset=Offset(3, 0)))
        screen._watch__selecting(False)

        assert not popup.is_shown
        assert screen.get_selected_text() == "sele"

        screen._view_adapter.hide_suggestions()
        await _finish_drag_over(app, body, text)


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
            card.post_message(BtwPanel.Scrolled(card.query_one(BtwPage)))

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
        popup = app.screen.query_one(QuoteSelectionPopup)
        assert tuple(rich_plain(w.render()) for w in popup.query(Static)) == ("引用", "│", "复制")
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


@pytest.mark.asyncio
async def test_a_side_card_quote_outlives_the_sidebar_text_its_drag_picked_up() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 30)) as pilot:
        card = await _side_card(app, pilot, "a side question", "the side answer")
        body = _side_body(card, "answer")
        label = app.main_screen.query_one("#ctx-usage-label", Static)
        input_bar = app.main_screen.query_one(InputBar)

        app.main_screen._watch__select_state(_chat_drag_state(body, body, end_offset=Offset(4, 0)))
        app.main_screen.selections = {body: SELECT_ALL, label: SELECT_ALL}
        app.main_screen._watch__selecting(False)
        popup = app.main_screen.query_one(QuoteSelectionPopup)
        await wait_for(lambda: popup.is_shown, pilot=pilot, description="the quote pill is offered")
        # The sidebar rebuilds its rows before the click.
        await label.remove()
        app.main_screen.clear_selection()
        app.main_screen.post_message(QuoteSelectionPopup.Requested())
        await pilot.pause()

        assert input_bar.draft().references.get("quote", 1) == DraftReference(
            "quote", 1, "the side answer\nContext Usage"
        )


@pytest.mark.parametrize("selection", ["preview", "across-messages", "dragging"])
async def test_replacing_a_selected_preview_withdraws_its_selection_and_quote(selection: str) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 32)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        text = "Select this pending answer."
        await panel.add_user_message("Prompt to select.")
        await panel.add_answer_delta("a1", text)
        preview = panel.query_one(AgentMessage)
        header = preview.query_one(".fold-row-header")
        user = panel.query_one(UserMessage).query_one(VirtualizedMarkdown)
        await wait_for(lambda: bool(header.region and user.shown_source), pilot=pilot, description="selection targets")
        start = user if selection == "across-messages" else header
        end = (header.region.x + 17, header.region.y)
        await pilot.mouse_down(start, offset=(2, 0))
        await pilot.hover(offset=end)
        await wait_for(lambda: header in screen.selections, pilot=pilot, description="the preview is selected")
        popup = screen.query_one(QuoteSelectionPopup)
        if selection != "dragging":
            _post_mouse(app, MouseUp, *end)
            await wait_for(lambda: popup.is_shown, pilot=pilot, description="the preview is offered as a quote")

        panel.finish_answer_segment("a1", completed=True)
        await panel.add_agent_message(text)
        assert not preview.is_attached
        assert not screen.selections and not popup.is_shown
        assert not screen.get_selected_text()
        if selection == "dragging":
            await pilot.hover(offset=(user.region.x + 5, user.region.y))
            _post_mouse(app, MouseUp, user.region.x + 5, user.region.y)
            await pilot.pause()
            assert not screen.selections and not popup.is_shown


@pytest.mark.asyncio
async def test_releasing_a_drag_after_activity_arrives_updates_highlight_copy_and_quote() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 32)) as pilot:
        screen = app.main_screen
        panel = screen.query_one(ChatPanel)
        await panel.add_user_message("Select this prompt.")
        await panel.add_thinking_delta("r1", "reasoning", "First thought.")
        panel.finish_thinking_segment("r1")
        await panel.add_answer_delta("a1", "Select this answer.")
        await click_when_settled(pilot, panel.query_one("LiveProcessRow"))
        user = panel.query_one(UserMessage).query_one(VirtualizedMarkdown)
        answer = panel.query_one(AgentMessage).query_one(".fold-row-header")
        await wait_for(
            lambda: bool(answer.region.height and user.shown_source), pilot=pilot, description="drag endpoints"
        )
        end = (answer.region.x + 13, answer.region.y)
        await pilot.mouse_down(user, offset=(1, 0))
        await pilot.hover(offset=end)
        await wait_for(lambda: answer in screen.selections, pilot=pilot, description="drag reaches answer")

        await panel.add_thinking_delta("r2", "reasoning", "New thought while dragging.")
        panel.finish_thinking_segment("r2")
        thought = panel.query(ThinkingMessage).last().query_one(".fold-row-header")
        await wait_for(lambda: bool(thought.region.height), pilot=pilot, description="new thought is laid out")
        before_move = screen.get_selected_text()
        assert before_move and "Replying..." in before_move and "New thought while dragging." not in before_move
        end = (thought.region.x + len(thought.render().plain), thought.region.y)
        await pilot.hover(offset=end)
        await wait_for(lambda: thought in screen.selections, pilot=pilot, description="drag extends to the new thought")
        # Pilot.mouse_up also sends a MouseMove; release at the stationary pointer.
        _post_mouse(app, MouseUp, *end)
        popup = screen.query_one(QuoteSelectionPopup)
        await wait_for(lambda: popup.is_shown, pilot=pilot, description="quote offered on release")
        copied = screen.get_selected_text()
        assert copied and "New thought while dragging." in copied
        highlighted = "".join(
            picked[0]
            for widget, selection in screen.selections.items()
            if (picked := widget.get_selection(selection)) is not None
        )
        assert "New thought while dragging." in highlighted
        selection = dict(screen.selections)
        divider = popup.query_one(".selection-divider")
        _post_mouse(app, MouseDown, *divider.region.offset)
        _post_mouse(app, MouseUp, *divider.region.offset)
        await pilot.pause()
        assert popup.is_shown
        assert screen._chat_selection.active
        assert screen.get_selected_text() == copied
        assert screen.selections == selection

        await _click_pill(app, pilot, popup)
        assert screen.query_one(InputBar).draft().references.get("quote", 1) == DraftReference(
            "quote", 1, copied.strip()
        )
