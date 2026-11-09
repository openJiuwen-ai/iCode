# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The prompt editor shows the sending form and commits the whole draft once."""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.widgets import Button, TextArea
from textual.widgets.text_area import Document

from chrys.app.tui.screens.dialogs.editor import EditorDialog
from chrys.app.tui.screens.dialogs.reference_preview import ReferencePreviewDialog
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.util.git_branch import GitBranchMonitor
from chrys.app.tui.widgets.chrome.draft_editor import DraftEditor
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft, ShortcutIntent
from chrys.app.tui.widgets.editor import MESSAGE_EDITOR_MAX_CHARACTERS, EditorMode, VimKeymap, VimState
from chrys.foundation.events.bus import EventBus
from chrys.foundation.text.draft_references import DraftReferences, expand_references
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.asyncio


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


def _draft(*, fields: bool = True, owner: bool = True, kind: str = "mixed") -> ChatDraft:
    references = DraftReferences()
    labels: list[str] = []
    if kind != "none":
        references, reference = references.with_new("paste", "  pasted first\npasted last\n")
        labels.append(reference.label)
    if kind == "mixed":
        for reference_kind, content in [
            ("paste", "second pasted\nchunk"),
            ("quote", "@single !`echo no`"),
            ("quote", "first\nsecond"),
            ("image", "/tmp/shot.png"),
        ]:
            references, reference = references.with_new(reference_kind, content)
            labels.append(reference.label)
    text = "prefix " + " ".join(labels) + " FIELD suffix"
    start = text.index("FIELD")
    return ChatDraft(
        text,
        ((start, start + 5),) if fields else (),
        ShortcutIntent("shortcut", "Code", "model") if owner else None,
        references,
    )


async def _open(
    app: _App, pilot, draft: ChatDraft, *, entry: str = "keyboard", cursor: int = 0
) -> tuple[InputBar, TextArea, DraftEditor]:
    bar = app.main.query_one(InputBar)
    bar.set_draft(draft, cursor_location=Document(draft.text).get_location_from_index(cursor))
    area = bar.query_one("#chat-input", TextArea)
    bar.focus_input()
    await wait_for(lambda: area.has_focus, pilot=pilot, description="composer focus")
    if entry == "button":
        await click_when_settled(pilot, "#editor-btn")
    else:
        await pilot.press("ctrl+o")
    await wait_for(
        lambda: isinstance(app.screen, EditorDialog) and app.screen.is_mounted and screen_is_settled(app, app.screen),
        pilot=pilot,
        description="expanded editor ready",
    )
    return bar, area, app.screen.query_one(DraftEditor)


def _empty_composer(app: _App) -> None:
    """Leave the composer as a new app has it: no draft, and no step to undo or redo.

    Several cases share one app run (a MainScreen costs more to build than a case costs to run);
    each starts from here, so no draft or undo step of an earlier case reaches it.
    """
    bar = app.main.query_one(InputBar)
    area = bar.query_one("#chat-input", TextArea)
    bar.set_draft(ChatDraft(""))
    area.history.clear()
    assert bar.draft() == ChatDraft("")
    assert not area.history.undo_stack and not area.history.redo_stack


@contextlib.contextmanager
def _case(**values: object) -> Iterator[None]:
    """Name the case of a shared app run that failed."""
    try:
        yield
    except BaseException as error:
        error.add_note("case: " + ", ".join(f"{name}={value!r}" for name, value in values.items()))
        raise


_DRAFT_SHAPES = [(False, False), (True, False), (False, True), (True, True)]


@pytest.mark.parametrize("entry", ["keyboard", "button"])
@pytest.mark.parametrize("kind", ["none", "paste", "mixed"])
async def test_open_editor_expands_sending_forms_and_shifts_fields(entry: str, kind: str) -> None:
    app = _App()
    async with app.run_test(size=(120, 36)) as pilot:
        # Every (fields, owner) shape, each written whole into the composer and opened in a new editor.
        for fields, owner in _DRAFT_SHAPES:
            with _case(fields=fields, owner=owner):
                draft = _draft(fields=fields, owner=owner, kind=kind)
                _empty_composer(app)
                _bar, _area, editor = await _open(app, pilot, draft, entry=entry, cursor=len(draft.text))
                expected = expand_references(draft.text, draft.references)
                assert editor.text == expected
                assert not editor.references
                expected_start = expected.index("FIELD")
                assert editor.chat_draft().fields == (((expected_start, expected_start + 5),) if fields else ())
                assert editor.chat_draft().owner == draft.owner
                assert editor.cursor_location == Document(expected).end
                # Unchanged, so Escape closes it at once.
                await pilot.press("escape")
                await wait_for(lambda: app.screen is app.main, pilot=pilot, description="clean editor closed")


@pytest.mark.parametrize("entry", ["keyboard", "button"])
async def test_editor_cursor_maps_both_label_edges(entry: str) -> None:
    app = _App()
    references, reference = DraftReferences().with_new("paste", "first\nlast")
    draft = ChatDraft(f"a{reference.label}z", references=references)
    async with app.run_test(size=(120, 36)) as pilot:
        for edge in ("before", "after"):
            with _case(edge=edge):
                cursor = 1 if edge == "before" else 1 + len(reference.label)
                _empty_composer(app)
                _bar, _area, editor = await _open(app, pilot, draft, entry=entry, cursor=cursor)
                assert editor.text == "afirst\nlastz"
                assert editor.cursor_location == ((0, 1) if edge == "before" else (1, 4))
                # Unchanged, so Escape closes it at once.
                await pilot.press("escape")
                await wait_for(lambda: app.screen is app.main, pilot=pilot, description="clean editor closed")


@pytest.mark.parametrize("mode", list(EditorMode))
async def test_clean_editor_round_trip_preserves_cursor_at_adjacent_label_edges(mode: EditorMode) -> None:
    app = _App()
    app.main._editor_mode = mode
    references, first = DraftReferences().with_new(
        "paste", "first line\nsecond line\nthird line\nfourth line\nfifth line\nlast line"
    )
    references, second = references.with_new("paste", "x\ny\nz\nq\nr\ns")
    draft = ChatDraft(f"{first.label}{second.label} tail", references=references)
    async with app.run_test(size=(120, 36)) as pilot:
        bar = app.main.query_one(InputBar)
        area = bar.query_one("#chat-input", TextArea)
        for edge in ("before", "between", "after"):
            with _case(edge=edge):
                cursor = {"before": 0, "between": len(first.label), "after": len(first.label) + len(second.label)}[edge]
                expected_cursor = Document(draft.text).get_location_from_index(cursor)
                _empty_composer(app)
                _bar, _area, editor = await _open(app, pilot, draft, cursor=cursor)
                assert editor.text == first.content + second.content + " tail"
                assert not editor.editor_is_dirty
                undo_before = len(area.history.undo_stack)

                await pilot.press("ctrl+o")

                await wait_for(
                    lambda: app.screen is app.main and area.has_focus, pilot=pilot, description="clean save returned"
                )
                assert bar.draft() == draft
                assert area.cursor_location == expected_cursor
                assert len(area.history.undo_stack) == undo_before
                if edge == "before":
                    await pilot.press("backspace")
                    assert bar.draft() == draft


@pytest.mark.parametrize("mode", list(EditorMode))
@pytest.mark.parametrize("click_chain", [1, 2])
async def test_click_paste_opens_only_its_text_titled_by_its_label_in_initial_mode(
    mode: EditorMode, click_chain: int
) -> None:
    app = _App()
    app.main._editor_mode = mode
    references, reference = DraftReferences().with_new("paste", "first\nsecond")
    draft = ChatDraft(f"{reference.label} and {reference.label}", references=references)
    async with app.run_test(size=(120, 36)) as pilot:
        bar = app.main.query_one(InputBar)
        bar.set_draft(draft)
        area = bar.query_one("#chat-input", TextArea)
        await wait_for(lambda: screen_is_settled(app, app.main), pilot=pilot, description="label ready")
        offset = (
            area.content_region.x - area.region.x + len(reference.label) + 6,
            area.content_region.y - area.region.y,
        )
        await click_when_settled(pilot, area, offset=offset, times=click_chain)
        await wait_for(
            lambda: isinstance(app.screen, EditorDialog) and app.screen.is_mounted,
            pilot=pilot,
            description="paste opens editor",
        )
        editor = app.screen.query_one(DraftEditor)
        assert editor.text == "first\nsecond"
        assert editor.cursor_location == (0, 0)
        assert str(app.screen.query_one("#editor-dialog").border_title) == reference.label
        assert editor.mode is mode
        if mode is EditorMode.VIM:
            assert isinstance(editor.active_keymap, VimKeymap)
            assert editor.active_keymap.state is VimState.NORMAL


@pytest.mark.parametrize("kind", ["quote", "image"])
async def test_quote_and_image_click_keep_read_only_preview(kind: str) -> None:
    app = _App()
    async with app.run_test(size=(120, 36)) as pilot:
        bar = app.main.query_one(InputBar)
        assert bar.insert_reference(kind, "quoted" if kind == "quote" else "/tmp/missing.png")
        area = bar.query_one("#chat-input", TextArea)
        await wait_for(lambda: screen_is_settled(app, app.main), pilot=pilot, description="preview label ready")
        await click_when_settled(
            pilot, area, offset=(area.content_region.x - area.region.x + 3, area.content_region.y - area.region.y)
        )
        await wait_for(
            lambda: isinstance(app.screen, ReferencePreviewDialog), pilot=pilot, description="read only preview"
        )


@pytest.mark.parametrize("state", ["locked", "shell_mode"])
async def test_all_editor_entries_respect_input_state(state: str) -> None:
    app = _App()
    async with app.run_test(size=(120, 36)) as pilot:
        bar = app.main.query_one(InputBar)
        bar.set_draft(_draft(kind="paste"))
        area = bar.query_one("#chat-input", TextArea)
        # A refused entry leaves the app as it found it, so the entries take turns in one run. The
        # keyboard goes first: only it relies on the composer's focus, taken before the state is set.
        for entry in ("keyboard", "button", "paste"):
            with _case(entry=entry):
                area.focus()
                setattr(bar, state, True)
                await pilot.pause()
                if entry == "keyboard":
                    await pilot.press("ctrl+o")
                elif entry == "button":
                    await click_when_settled(pilot, "#editor-btn")
                else:
                    reference = bar.draft().references.entries[0]
                    bar.on_draft_text_area_reference_clicked(
                        area.ReferenceClicked(reference, area.text.index(reference.label))
                    )
                    await pilot.pause()
                assert app.screen is app.main


_SAVE_PATHS = [
    (EditorMode.STANDARD, "ctrl+o"),
    (EditorMode.STANDARD, "ctrl+enter"),
    (EditorMode.STANDARD, "button"),
    (EditorMode.EMACS, "ctrl+o"),
    (EditorMode.EMACS, "ctrl+enter"),
    (EditorMode.EMACS, "button"),
    (EditorMode.VIM, "ZZ"),
    (EditorMode.VIM, ":wq"),
    (EditorMode.VIM, ":x"),
    (EditorMode.VIM, "ctrl+o"),
    (EditorMode.VIM, "button"),
]


async def _save(pilot, path: str) -> None:
    if path == "button":
        await click_when_settled(pilot, "#editor-commit")
    elif path in ("ZZ", ":wq", ":x"):
        await pilot.press(*path, *(("enter",) if path.startswith(":") else ()))
    else:
        await pilot.press(path)


@pytest.mark.parametrize("mode,path", _SAVE_PATHS)
async def test_every_save_path_preserves_clean_labels_or_commits_once(mode: EditorMode, path: str) -> None:
    app = _App()
    app.main._editor_mode = mode
    draft = _draft()
    async with app.run_test(size=(120, 36)) as pilot:
        bar = app.main.query_one(InputBar)
        area = bar.query_one("#chat-input", TextArea)
        for change in ("unchanged", "restored", "text", "fields"):
            with _case(change=change):
                _empty_composer(app)
                _bar, _area, editor = await _open(app, pilot, draft)
                expected = expand_references(draft.text, draft.references)
                assert editor.text == expected
                undo_before = len(area.history.undo_stack)
                if change in ("restored", "text"):
                    editor.insert("!", editor.document.end)
                    if change == "restored":
                        editor.undo()
                elif change == "fields":
                    editor.set_prompt_fields(())
                edited = editor.chat_draft()
                # Exercise the inverse cursor conversion inside an expanded paste.
                if change in ("unchanged", "restored"):
                    editor.move_cursor((0, 10))
                await _save(pilot, path)
                await wait_for(
                    lambda: app.screen is app.main and area.has_focus, pilot=pilot, description="save returned"
                )
                if change in ("unchanged", "restored"):
                    assert bar.draft() == draft
                    assert len(area.history.undo_stack) == undo_before
                    assert area.cursor_location == (0, 7 + len(draft.references.entries[0].label))
                    await pilot.press("ctrl+z")
                    assert bar.value == ""
                else:
                    assert bar.draft() == edited
                    await pilot.press("ctrl+z")
                    assert bar.draft() == draft
                    await pilot.press("ctrl+y")
                    assert bar.draft() == edited


@pytest.mark.parametrize("mode,path", _SAVE_PATHS)
async def test_confirmed_overwrite_is_one_step_even_for_clean_editor(mode: EditorMode, path: str) -> None:
    app = _App()
    app.main._editor_mode = mode
    draft = _draft()
    async with app.run_test(size=(120, 36)) as pilot:
        for editor_changed in (False, True):
            with _case(editor_changed=editor_changed):
                _empty_composer(app)
                bar, _area, editor = await _open(app, pilot, draft)
                assert not editor.references
                if editor_changed:
                    editor.insert("!", editor.document.end)
                edited = editor.chat_draft()
                overwritten = ChatDraft("external FIELD", ((9, 14),), draft.owner)
                bar.set_draft(overwritten)
                if path == "button":
                    # A pressed Button ignores clicks until its press effect ends, and the second
                    # click (``click_when_settled``) waits that out; the effect's length (0.2 s) is not
                    # what this tests.
                    app.screen.query_one("#editor-commit", Button).active_effect_duration = 0.02
                await _save(pilot, path)
                assert isinstance(app.screen, EditorDialog)
                if path in (":wq", ":x"):
                    # A refused Vim command remains on its command line; Enter submits it again.
                    await pilot.press("enter")
                else:
                    await _save(pilot, path)
                await wait_for(lambda: app.screen is app.main, pilot=pilot, description="confirmed overwrite")
                wanted = edited if editor_changed else draft
                assert bar.draft() == wanted
                await pilot.press("ctrl+z")
                assert bar.draft() == overwritten
                await pilot.press("ctrl+y")
                assert bar.draft() == wanted


@pytest.mark.parametrize(
    "mode,path,dirty_cases",
    [
        (EditorMode.STANDARD, "escape", (False, True)),
        (EditorMode.EMACS, "escape", (False, True)),
        (EditorMode.STANDARD, "button", (False, True)),
        (EditorMode.EMACS, "button", (False, True)),
        (EditorMode.VIM, "ZQ", (False, True)),
        (EditorMode.VIM, ":q!", (False, True)),
        (EditorMode.VIM, ":q", (False,)),
        (EditorMode.VIM, "button", (False, True)),
    ],
)
async def test_discard_paths_preserve_folded_draft(mode: EditorMode, path: str, dirty_cases: tuple[bool, ...]) -> None:
    app = _App()
    app.main._editor_mode = mode
    draft = _draft()
    async with app.run_test(size=(120, 36)) as pilot:
        for dirty in dirty_cases:
            with _case(dirty=dirty):
                _empty_composer(app)
                bar, _area, editor = await _open(app, pilot, draft)
                assert not editor.references
                if dirty:
                    editor.insert("!", editor.document.end)
                if path == "button":
                    await click_when_settled(pilot, "#editor-cancel")
                    await click_when_settled(pilot, "#confirm-yes")
                elif path == "escape":
                    await pilot.press("escape")
                    if dirty:
                        assert isinstance(app.screen, EditorDialog)
                        assert app.screen.discard_armed
                        await pilot.press("escape")
                else:
                    await pilot.press(*path, *(("enter",) if path.startswith(":") else ()))
                await wait_for(lambda: app.screen is app.main, pilot=pilot, description="discard returned")
                assert bar.draft() == draft


async def test_plain_editor_save_undo_never_shows_intermediate_empty_draft() -> None:
    app = _App()
    async with app.run_test(size=(120, 36)) as pilot:
        bar, _area, editor = await _open(app, pilot, ChatDraft("original"))
        editor.insert("!", editor.document.end)
        await pilot.press("ctrl+o")
        await wait_for(lambda: app.screen is app.main, pilot=pilot, description="plain editor returned")
        assert bar.value == "original!"
        await pilot.press("ctrl+z")
        assert bar.value == "original"


@pytest.mark.parametrize("entry", ["keyboard", "button", "paste"])
async def test_editor_limit_is_checked_after_expansion(entry: str, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _App()
    notices: list[str] = []
    monkeypatch.setattr(app.main, "notify", lambda message, **_kwargs: notices.append(str(message)))
    references, reference = DraftReferences().with_new("paste", "x" * (MESSAGE_EDITOR_MAX_CHARACTERS + 1))
    async with app.run_test(size=(120, 36)) as pilot:
        bar = app.main.query_one(InputBar)
        bar.set_draft(ChatDraft(reference.label, references=references))
        area = bar.query_one("#chat-input", TextArea)
        area.focus()
        await wait_for(lambda: area.has_focus, pilot=pilot, description="oversize composer focus")
        if entry == "keyboard":
            await pilot.press("ctrl+o")
        elif entry == "button":
            await click_when_settled(pilot, "#editor-btn")
        else:
            await click_when_settled(
                pilot, area, offset=(area.content_region.x - area.region.x + 3, area.content_region.y - area.region.y)
            )
        await pilot.pause()
        assert app.screen is app.main
        assert notices == ["Editor supports drafts up to 500,000 characters."]


async def test_drag_across_paste_selects_without_opening_and_next_click_opens() -> None:
    app = _App()
    references, reference = DraftReferences().with_new("paste", "first\nlast")
    async with app.run_test(size=(120, 36)) as pilot:
        bar = app.main.query_one(InputBar)
        bar.set_draft(ChatDraft(f"a {reference.label} z", references=references))
        area = bar.query_one("#chat-input", TextArea)
        await wait_for(lambda: screen_is_settled(app, app.main), pilot=pilot, description="drag label ready")
        left = area.content_region.x - area.region.x
        row = area.content_region.y - area.region.y
        await pilot.mouse_down(area, offset=(left, row))
        await pilot._post_mouse_events(
            [events.MouseMove], area, offset=(left + 3 + len(reference.label), row), button=1
        )
        await pilot._post_mouse_events(
            [events.MouseUp, events.Click], area, offset=(left + 3 + len(reference.label), row), button=1
        )
        assert app.screen is app.main
        assert reference.label in area.selected_text
        await pilot.press("left")
        await click_when_settled(pilot, area, offset=(left + 4, row))
        await wait_for(lambda: isinstance(app.screen, EditorDialog), pilot=pilot, description="next click opens editor")


async def test_a_changed_save_strips_the_message_ends_like_typed_text() -> None:
    # Accepted (plan section 6): while a paste that starts the message is a label, its
    # indentation is sent; once a changed save writes the text back, the send strips it.
    app = _App()
    references, reference = DraftReferences().with_new("paste", "    indented\nline 2\n")
    draft = ChatDraft(f"{reference.label} tail", references=references)
    async with app.run_test(size=(120, 36)) as pilot:
        bar, area, editor = await _open(app, pilot, draft)
        assert bar.draft().sent_text() == "    indented\nline 2\n tail"
        editor.insert("!", editor.document.end)
        await _save(pilot, "ctrl+o")
        await wait_for(lambda: app.screen is app.main and area.has_focus, pilot=pilot, description="save returned")

        assert bar.draft().sent_text() == "indented\nline 2\n tail!"
