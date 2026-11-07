# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Editing folded pasted text keeps the draft, live labels and undo in step."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.events import Paste
from textual.notifications import SeverityLevel

from chrys.app.tui.screens.dialogs.reference_preview import ReferencePreviewDialog
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.theme import TuiVariableDefaultsMixin
from chrys.app.tui.util.git_branch import GitBranchMonitor
from chrys.app.tui.widgets.chrome.input_bar import ChatDraft, InputBar, _ChatTextArea
from chrys.app.tui.widgets.editor import MessageEditor
from chrys.foundation.events.bus import EventBus
from chrys.foundation.text.draft_references import EMPTY_REFERENCES, DraftReference, iter_reference_labels
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

_OLD = "one\ntwo\nthree\nfour"
_NEW = "new\ntext\nwith\nfive\nlines"
_OLD_LABEL = "[Pasted text #1 +3 lines]"
_NEW_LABEL = "[Pasted text #1 +4 lines]"


class _InputApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str] = []

    def compose(self) -> ComposeResult:
        yield InputBar()

    def on_input_bar_user_submitted(self, event: InputBar.UserSubmitted) -> None:
        self.submitted.append(event.text)


class _MainApp(TuiVariableDefaultsMixin, App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.main_screen = MainScreen(EventBus(), engine_provider=None)
        self.notices: list[str] = []

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self.main_screen)

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.notices.append(message)
        super().notify(message, title=title, severity=severity, timeout=timeout, markup=markup)


@pytest.fixture(autouse=True)
def _no_branch_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(GitBranchMonitor, "_start_observer", lambda _monitor, _target: None)


async def _draft(app: App[None], pilot, *, text: str | None = None) -> tuple[InputBar, _ChatTextArea, DraftReference]:
    if isinstance(app, _MainApp):
        await wait_for(
            lambda: app.screen is app.main_screen and app.main_screen.is_mounted,
            pilot=pilot,
            description="main screen mounted",
        )
    input_bar = app.main_screen.query_one(InputBar) if isinstance(app, _MainApp) else app.query_one(InputBar)
    input_bar.focus_input()
    chat = input_bar.query_one(_ChatTextArea)
    await wait_for(lambda: chat.has_focus, pilot=pilot, description="chat input focused")
    references, reference = EMPTY_REFERENCES.with_new("paste", _OLD)
    input_bar.set_draft(ChatDraft(text if text is not None else f"x{_OLD_LABEL}y", references=references))
    return input_bar, chat, reference


def _live(chat: _ChatTextArea) -> list[str]:
    return [chat.text[label.start : label.end] for label in iter_reference_labels(chat.text, chat.references)]


async def _open_paste(app: _MainApp, pilot, chat: _ChatTextArea) -> MessageEditor:
    chat.move_cursor((0, 0))
    await click_when_settled(pilot, chat, offset=(4, 0))
    await wait_for(lambda: isinstance(app.screen, ReferencePreviewDialog), pilot=pilot, description="paste editor open")
    editor = app.screen.query_one(MessageEditor)
    await wait_for(lambda: editor.has_focus, pilot=pilot, description="paste editor focused")
    return editor


@pytest.mark.asyncio
async def test_a_changed_paste_keeps_its_number_and_one_undo_restores_its_old_content() -> None:
    app = _InputApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, chat, reference = await _draft(app, pilot)

        assert chat.edit_reference(reference, _NEW)
        assert chat.text == f"x{_NEW_LABEL}y"
        assert _live(chat) == [_NEW_LABEL]
        assert input_bar.draft().sent_text() == f"x{_NEW}y"

        await pilot.press("ctrl+z")
        assert chat.text == f"x{_OLD_LABEL}y"
        assert _live(chat) == [_OLD_LABEL]
        assert input_bar.draft().sent_text() == f"x{_OLD}y"

        await pilot.press("ctrl+y")
        assert chat.text == f"x{_NEW_LABEL}y"
        assert _live(chat) == [_NEW_LABEL]
        assert input_bar.draft().sent_text() == f"x{_NEW}y"


@pytest.mark.asyncio
@pytest.mark.parametrize(("content", "expected"), [("short", "xshorty"), ("", "xy"), ("a\r\nb", "xa\nby")])
async def test_a_shortened_or_empty_paste_becomes_plain_text(content: str, expected: str) -> None:
    app = _InputApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, chat, reference = await _draft(app, pilot)

        assert chat.edit_reference(reference, content)
        assert chat.text == expected
        assert _live(chat) == []
        assert input_bar.draft().sent_text() == expected

        await pilot.press("ctrl+z")
        assert chat.text == f"x{_OLD_LABEL}y"
        assert _live(chat) == [_OLD_LABEL]
        assert input_bar.draft().sent_text() == f"x{_OLD}y"


@pytest.mark.asyncio
async def test_unchanged_content_adds_no_edit_between_the_draft_and_its_previous_undo() -> None:
    app = _InputApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, chat, reference = await _draft(app, pilot)
        await pilot.press("X")
        before = input_bar.draft()

        assert chat.edit_reference(reference, _OLD)
        assert input_bar.draft() == before
        await pilot.press("ctrl+z")
        assert chat.text == f"x{_OLD_LABEL}y"
        assert _live(chat) == [_OLD_LABEL]


@pytest.mark.asyncio
async def test_changed_content_with_the_same_label_still_has_its_own_undo_step() -> None:
    app = _InputApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, chat, reference = await _draft(app, pilot)
        changed = "changed\nbut\nstill\nfour"

        assert chat.edit_reference(reference, changed)
        assert chat.text == f"x{_OLD_LABEL}y"
        assert input_bar.draft().sent_text() == f"x{changed}y"
        chat.undo()
        assert input_bar.draft().sent_text() == f"x{_OLD}y"
        assert _live(chat) == [_OLD_LABEL]
        chat.redo()
        assert input_bar.draft().sent_text() == f"x{changed}y"


@pytest.mark.asyncio
async def test_undo_past_a_paste_edit_keeps_the_earlier_label_live() -> None:
    app = _InputApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, chat, reference = await _draft(app, pilot)
        await pilot.press("X")
        assert chat.edit_reference(reference, _NEW)

        chat.undo()
        assert chat.text == f"x{_OLD_LABEL}yX"
        chat.undo()
        assert chat.text == f"x{_OLD_LABEL}y"
        assert _live(chat) == [_OLD_LABEL]
        assert input_bar.draft().sent_text() == f"x{_OLD}y"


@pytest.mark.asyncio
async def test_interleaved_quote_registration_survives_paste_edit_undo_and_redo() -> None:
    app = _InputApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, chat, reference = await _draft(app, pilot)
        assert chat.edit_reference(reference, _NEW)
        chat.move_cursor(chat.document.end)
        assert chat.insert_reference("quote", "said")
        after = input_bar.draft()

        chat.undo()
        assert _live(chat) == [_NEW_LABEL]
        chat.undo()
        assert _live(chat) == [_OLD_LABEL]
        assert input_bar.draft().sent_text() == f"x{_OLD}y"
        chat.redo()
        chat.redo()
        assert input_bar.draft() == after
        assert _live(chat) == [_NEW_LABEL, "[Quote #1]"]
        assert input_bar.draft().sent_text() == f"x{_NEW}y “said”"


@pytest.mark.asyncio
async def test_two_occurrences_update_and_undo_together() -> None:
    app = _InputApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, chat, reference = await _draft(app, pilot, text=f"x{_OLD_LABEL} middle {_OLD_LABEL}y")

        assert chat.edit_reference(reference, _NEW)
        assert chat.text == f"x{_NEW_LABEL} middle {_NEW_LABEL}y"
        assert _live(chat) == [_NEW_LABEL, _NEW_LABEL]
        assert input_bar.draft().sent_text() == f"x{_NEW} middle {_NEW}y"
        chat.undo()
        assert chat.text == f"x{_OLD_LABEL} middle {_OLD_LABEL}y"
        assert _live(chat) == [_OLD_LABEL, _OLD_LABEL]
        assert input_bar.draft().sent_text() == f"x{_OLD} middle {_OLD}y"


@pytest.mark.asyncio
@pytest.mark.parametrize("same_line_count", [False, True], ids=["label-changes", "label-unchanged"])
async def test_updating_two_paste_labels_preserves_and_shifts_the_field_between_them(same_line_count: bool) -> None:
    app = _InputApp()
    async with app.run_test(size=(100, 30)) as pilot:
        original = f"x{_OLD_LABEL} field {_OLD_LABEL}y"
        input_bar, chat, reference = await _draft(app, pilot, text=original)
        start = original.index("field")
        fields = ((start, start + 5),)
        chat.set_prompt_fields(fields, select=False)
        changed = (
            "changed\nbut\nstill\nfour" if same_line_count else "\n".join(f"line {number}" for number in range(12))
        )
        label = _OLD_LABEL if same_line_count else "[Pasted text #1 +11 lines]"
        expected = f"x{label} field {label}y"
        moved = expected.index("field")
        moved_fields = ((moved, moved + 5),)

        assert chat.edit_reference(reference, changed)
        assert chat.text == expected
        assert chat.prompt_fields == moved_fields
        assert chat.text[moved : moved + 5] == "field"
        assert input_bar.draft().sent_text() == f"x{changed} field {changed}y"
        await pilot.press("ctrl+z")
        assert chat.text == original
        assert chat.prompt_fields == fields
        assert chat.text[start : start + 5] == "field"
        assert input_bar.draft().sent_text() == f"x{_OLD} field {_OLD}y"
        await pilot.press("ctrl+y")
        assert chat.text == expected
        assert chat.prompt_fields == moved_fields
        assert chat.text[moved : moved + 5] == "field"
        assert input_bar.draft().sent_text() == f"x{changed} field {changed}y"


@pytest.mark.asyncio
async def test_shortening_three_paste_labels_preserves_both_fields_and_is_one_undo() -> None:
    original = f"x{_OLD_LABEL} first {_OLD_LABEL} second {_OLD_LABEL}y"
    expected = "xshort first short second shorty"
    fields = tuple((original.index(word), original.index(word) + len(word)) for word in ("first", "second"))
    moved_fields = tuple((expected.index(word), expected.index(word) + len(word)) for word in ("first", "second"))
    app = _InputApp()
    async with app.run_test(size=(120, 30)) as pilot:
        input_bar, chat, reference = await _draft(app, pilot, text=original)
        chat.set_prompt_fields(fields, select=False)

        assert chat.edit_reference(reference, "short")
        assert chat.text == expected
        assert chat.prompt_fields == moved_fields
        assert [chat.text[start:end] for start, end in chat.prompt_fields] == ["first", "second"]
        assert input_bar.draft().sent_text() == expected
        await pilot.press("tab")
        assert chat.selected_text == "first"
        await pilot.press("tab")
        assert chat.selected_text == "second"

        await pilot.press("ctrl+z")
        assert chat.text == original
        assert chat.prompt_fields == fields
        assert input_bar.draft().sent_text() == f"x{_OLD} first {_OLD} second {_OLD}y"
        await pilot.press("ctrl+y")
        assert chat.text == expected
        assert chat.prompt_fields == moved_fields
        assert [chat.text[start:end] for start, end in chat.prompt_fields] == ["first", "second"]
        assert input_bar.draft().sent_text() == expected


@pytest.mark.asyncio
async def test_shortening_to_another_live_label_behaves_like_pasting_that_text() -> None:
    app = _InputApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, chat, reference = await _draft(app, pilot)
        assert chat.insert_reference("image", "/tmp/shot.png")

        assert chat.edit_reference(reference, "[Image #1]")

        assert _live(chat) == ["[Image #1]", "[Image #1]"]
        assert input_bar.draft().sent_text() == 'x @"/tmp/shot.png" y @"/tmp/shot.png"'


@pytest.mark.asyncio
async def test_a_changed_paste_is_sent_and_returns_after_the_draft_leaves() -> None:
    app = _InputApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, chat, reference = await _draft(app, pilot)
        assert chat.edit_reference(reference, _NEW)

        await pilot.press("enter")
        assert app.submitted == [f"x{_NEW}y"]
        assert chat.text == ""
        await pilot.press("ctrl+z")
        assert chat.text == f"x{_NEW_LABEL}y"
        assert input_bar.draft().sent_text() == f"x{_NEW}y"
        assert _live(chat) == [_NEW_LABEL]


@pytest.mark.asyncio
async def test_clicking_a_paste_edits_the_input_draft_and_updates_the_label_on_close() -> None:
    app = _MainApp()
    async with app.run_test(size=(120, 36)) as pilot:
        input_bar, chat, _reference = await _draft(app, pilot)
        editor = await _open_paste(app, pilot, chat)
        editor.select_all()
        editor.post_message(Paste(_NEW))
        await wait_for(lambda: editor.text == _NEW, pilot=pilot, description="paste content changed")

        await pilot.press("escape")
        await wait_for(lambda: app.screen is app.main_screen, pilot=pilot, description="paste editor closed")

        assert chat.text == f"x{_NEW_LABEL}y"
        assert _live(chat) == [_NEW_LABEL]
        assert input_bar.draft().sent_text() == f"x{_NEW}y"


@pytest.mark.asyncio
async def test_closing_an_unchanged_paste_preview_leaves_the_previous_edit_next_to_undo() -> None:
    app = _MainApp()
    async with app.run_test(size=(120, 36)) as pilot:
        input_bar, chat, _reference = await _draft(app, pilot)
        await pilot.press("X")
        before = input_bar.draft()
        await _open_paste(app, pilot, chat)

        await pilot.press("escape")
        await wait_for(lambda: app.screen is app.main_screen, pilot=pilot, description="unchanged paste editor closed")
        assert input_bar.draft() == before
        await pilot.press("ctrl+z")
        assert chat.text == f"x{_OLD_LABEL}y"
        assert _live(chat) == [_OLD_LABEL]


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["entry", "label", "both"])
async def test_a_paste_edit_is_not_applied_after_its_draft_is_replaced(missing: str) -> None:
    app = _MainApp()
    async with app.run_test(size=(120, 36)) as pilot:
        input_bar, chat, _reference = await _draft(app, pilot)
        editor = await _open_paste(app, pilot, chat)
        editor.select_all()
        editor.post_message(Paste(_NEW))
        await wait_for(lambda: editor.text == _NEW, pilot=pilot, description="paste content changed")
        replacement = ChatDraft(
            f"x{_OLD_LABEL}y" if missing == "entry" else "replacement draft",
            references=chat.references if missing == "label" else EMPTY_REFERENCES,
        )
        chat.write_draft(replacement)

        await pilot.press("escape")
        await wait_for(lambda: app.screen is app.main_screen, pilot=pilot, description="stale paste editor closed")

        assert input_bar.value == replacement.text
        assert input_bar.draft().references == replacement.references
        assert "The label is no longer in the draft; changes were not saved." in app.notices
