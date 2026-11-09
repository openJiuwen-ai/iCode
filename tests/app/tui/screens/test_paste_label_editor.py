# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Clicking a pasted-text label edits that paste alone; saving is what pasting it now would give, in one undo step."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import TextArea

from chrys.app.tui.screens.dialogs.editor import EditorDialog
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.util.git_branch import GitBranchMonitor
from chrys.app.tui.widgets.chrome.draft_editor import DraftEditor
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft
from chrys.foundation.events.bus import EventBus
from chrys.foundation.text.draft_references import DraftReferences
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

pytestmark = pytest.mark.asyncio

_SIX_LINES = "l1\nl2\nl3\nl4\nl5\nl6"


class _App(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.main = MainScreen(EventBus(), engine_provider=None)

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self.main)


@pytest.fixture(autouse=True)
def _no_disk_or_observers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chrys.app.tui.screens.main.screen.persist_editor_keymap", lambda _mode: None)
    monkeypatch.setattr(GitBranchMonitor, "_start_observer", lambda _monitor, _target: None)


def _two_copies() -> ChatDraft:
    """The same paste twice and a field after both: ``[Pasted text #1 +5 lines] and [Pasted text #1 +5 lines] FIELD``."""
    references, entry = DraftReferences().with_new("paste", _SIX_LINES)
    text = f"{entry.label} and {entry.label} FIELD"
    start = text.index("FIELD")
    return ChatDraft(text, ((start, start + 5),), references=references)


async def _click_label(app: _App, pilot, draft: ChatDraft, column: int) -> tuple[InputBar, TextArea, DraftEditor]:
    bar = app.main.query_one(InputBar)
    bar.set_draft(draft)
    area = bar.query_one("#chat-input", TextArea)
    await wait_for(lambda: screen_is_settled(app, app.main), pilot=pilot, description="labels ready")
    offset = (area.content_region.x - area.region.x + column, area.content_region.y - area.region.y)
    await click_when_settled(pilot, area, offset=offset)
    await wait_for(
        lambda: isinstance(app.screen, EditorDialog) and app.screen.is_mounted and screen_is_settled(app, app.screen),
        pilot=pilot,
        description="paste editor ready",
    )
    return bar, area, app.screen.query_one(DraftEditor)


async def _save(app: _App, pilot) -> None:
    await pilot.press("ctrl+o")
    await wait_for(lambda: app.screen is app.main, pilot=pilot, description="paste editor closed")


async def test_a_longer_paste_keeps_its_numbered_label_everywhere_in_one_undo_step() -> None:
    app = _App()
    draft = _two_copies()
    label = draft.references.entries[0].label
    async with app.run_test(size=(120, 36)) as pilot:
        bar, area, editor = await _click_label(app, pilot, draft, len(label) + 6)
        editor.move_cursor(editor.document.end)
        await pilot.press("enter", "l", "7")
        await _save(app, pilot)

        new_label = "[Pasted text #1 +6 lines]"
        saved = bar.draft()
        assert saved.text == f"{new_label} and {new_label} FIELD"
        assert saved.sent_text() == f"{_SIX_LINES}\nl7 and {_SIX_LINES}\nl7 FIELD"
        field = saved.text.index("FIELD")
        assert saved.fields == ((field, field + 5),)
        assert area.cursor_location == (0, len(f"{new_label} and {new_label}"))

        await pilot.press("ctrl+z")
        assert bar.draft().text == draft.text
        assert bar.draft().sent_text() == draft.sent_text()
        assert bar.draft().fields == draft.fields

        await pilot.press("ctrl+y")
        assert bar.draft().text == saved.text
        assert bar.draft().sent_text() == saved.sent_text()
        assert bar.draft().fields == saved.fields


async def test_a_paste_short_enough_not_to_fold_turns_back_into_text_everywhere() -> None:
    app = _App()
    draft = _two_copies()
    async with app.run_test(size=(120, 36)) as pilot:
        bar, area, editor = await _click_label(app, pilot, draft, 2)
        editor.select_all()
        await pilot.press(*"short")
        await _save(app, pilot)

        saved = bar.draft()
        assert saved.text == "short and short FIELD"
        assert saved.fields == ((16, 21),)
        assert area.cursor_location == (0, 5)

        await pilot.press("ctrl+z")
        assert bar.draft().text == draft.text
        assert bar.draft().fields == draft.fields


async def test_an_emptied_paste_removes_its_labels() -> None:
    app = _App()
    draft = _two_copies()
    async with app.run_test(size=(120, 36)) as pilot:
        bar, _area, editor = await _click_label(app, pilot, draft, 2)
        editor.select_all()
        await pilot.press("backspace")
        await _save(app, pilot)

        assert bar.draft().text == " and  FIELD"
        assert bar.draft().fields == ((6, 11),)


async def test_saving_an_unchanged_paste_leaves_the_draft_as_it_was() -> None:
    app = _App()
    draft = _two_copies()
    async with app.run_test(size=(120, 36)) as pilot:
        bar, _area, _editor = await _click_label(app, pilot, draft, 2)
        await _save(app, pilot)

        assert bar.draft() == draft


async def test_discarding_a_changed_paste_leaves_the_draft_as_it_was() -> None:
    app = _App()
    draft = _two_copies()
    async with app.run_test(size=(120, 36)) as pilot:
        bar, _area, _editor = await _click_label(app, pilot, draft, 2)
        await pilot.press("x", "escape", "escape")
        await wait_for(lambda: app.screen is app.main, pilot=pilot, description="paste editor discarded")

        assert bar.draft() == draft


async def test_saving_a_paste_whose_label_is_gone_changes_nothing() -> None:
    app = _App()
    draft = _two_copies()
    entry = draft.references.entries[0]
    async with app.run_test(size=(120, 36)):
        bar = app.main.query_one(InputBar)
        bar.set_draft(ChatDraft("no labels here", references=draft.references))

        bar.edit_paste(entry, "changed", start=0)
        assert bar.draft().text == "no labels here"


async def test_label_text_left_by_a_short_save_reads_as_that_label_as_a_paste_would() -> None:
    # Accepted for now (backlog "标签按文字识别"): labels are known by their text, so a
    # short save that leaves a live label's exact text behaves like pasting that text.
    references, quote = DraftReferences().with_new("quote", "original quotation")
    references, paste = references.with_new("paste", _SIX_LINES)
    draft = ChatDraft(f"{quote.label} and {paste.label}", references=references)
    app = _App()
    async with app.run_test(size=(120, 36)) as pilot:
        bar, _area, editor = await _click_label(app, pilot, draft, len(quote.label) + 7)
        editor.select_all()
        await pilot.press(*"See [Quote #1]")
        await _save(app, pilot)

        assert bar.draft().text == f"{quote.label} and See [Quote #1]"
        assert bar.draft().sent_text() == "> original quotation\n\n and See \n> original quotation"


async def test_a_draft_changed_while_the_paste_editor_is_open_asks_for_a_second_commit() -> None:
    # The same guard as Ctrl+O: the first commit only warns, the second saves into the draft as it is now.
    app = _App()
    draft = _two_copies()
    label = draft.references.entries[0].label
    async with app.run_test(size=(120, 36)) as pilot:
        bar, _area, editor = await _click_label(app, pilot, draft, 2)
        changed = ChatDraft(f"{label} only", references=draft.references)
        bar.set_draft(changed)
        editor.select_all()
        await pilot.press(*"short")

        await pilot.press("ctrl+o")
        await pilot.pause()
        assert isinstance(app.screen, EditorDialog)
        assert bar.draft() == changed

        await _save(app, pilot)
        assert bar.draft().text == "short only"
