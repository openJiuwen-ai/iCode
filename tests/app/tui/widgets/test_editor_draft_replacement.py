# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Saving a full-text editor draft is exactly one reversible composer edit."""

from __future__ import annotations

from dataclasses import replace

import pytest
from textual.app import App, ComposeResult
from textual.events import Paste

from chrys.app.tui.widgets.chrome.input_bar import InputBar, _ChatTextArea
from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft, ShortcutIntent
from chrys.foundation.text.draft_references import EMPTY_REFERENCES, iter_reference_labels


class _App(App[None]):
    def compose(self) -> ComposeResult:
        yield InputBar()


def _original() -> ChatDraft:
    table, entry = EMPTY_REFERENCES.with_new("paste", "one\ntwo\nthree\nfour\nfive\nsix")
    text = f"before {entry.label} field after"
    start = text.index("field")
    return ChatDraft(text, ((start, start + 5),), ShortcutIntent("fix", "Code", "model-a"), table)


def _parts(app: _App) -> tuple[InputBar, _ChatTextArea]:
    bar = app.query_one(InputBar)
    return bar, bar.query_one(_ChatTextArea)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_fields", [False, True])
async def test_editor_replacement_and_later_paste_round_trip_both_metadata_tables(with_fields: bool) -> None:
    app = _App()
    async with app.run_test(size=(100, 30)):
        bar, area = _parts(app)
        before = _original()
        if not with_fields:
            before = replace(before, fields=())
        bar.set_draft(before)
        area.history.clear()
        text = before.sent_text() + "!"
        start = text.index("field")
        edited = ChatDraft(text, ((start, start + 5),) if with_fields else (), before.owner)

        area.replace_draft(edited)
        assert bar.draft() == edited
        area.move_cursor((0, 0))
        pasted = "alpha\nbeta\ngamma\ndelta\nepsilon\nzeta"
        await area._on_paste(Paste(pasted))
        after_paste = bar.draft()
        labels = iter_reference_labels(after_paste.text, after_paste.references)
        assert len(labels) == 1
        assert labels[0].reference.content == pasted

        area.action_undo()
        assert bar.value == edited.text
        assert bar.draft().fields == edited.fields
        area.action_undo()
        assert bar.draft() == before
        area.action_redo()
        assert bar.value == edited.text
        assert bar.draft().fields == edited.fields
        area.action_redo()
        assert bar.draft() == after_paste
        labels = iter_reference_labels(area.text, area.references)
        assert len(labels) == 1
        assert labels[0].reference.content == pasted
        if with_fields:
            assert [area.text[a:b] for a, b in area.prompt_fields] == ["field"]


@pytest.mark.asyncio
async def test_editor_replacement_undo_does_not_restore_a_cancelled_shortcut_switch() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)):
        bar, area = _parts(app)
        before = _original()
        bar.set_draft(before)
        edited = ChatDraft(before.sent_text() + "!", owner=before.owner)
        area.replace_draft(edited)
        cancelled = replace(before.owner, switch_dropped=True)
        area.set_draft_owner(cancelled)

        area.action_undo()
        assert bar.value == before.text
        assert area.draft_owner == cancelled
        area.action_redo()
        assert bar.value == edited.text
        assert area.draft_owner == cancelled


@pytest.mark.asyncio
async def test_editor_replacement_undo_does_not_revive_an_owner_dropped_on_send_refusal() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)):
        bar, area = _parts(app)
        before = _original()
        bar.set_draft(before)
        edited = ChatDraft(before.sent_text() + "!", owner=before.owner)
        area.replace_draft(edited)
        area.leave_draft()
        assert bar.restore_draft(replace(edited, owner=None))
        assert area.draft_owner is None

        area.action_undo()
        assert bar.value == before.text
        assert bar.draft().references == before.references
        assert bar.draft().fields == before.fields
        assert area.draft_owner is None
        area.action_redo()
        assert bar.value == edited.text
        assert area.draft_owner is None


@pytest.mark.asyncio
async def test_editor_replacement_of_plain_text_has_no_intermediate_empty_undo() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)):
        bar, area = _parts(app)
        bar.set_draft(ChatDraft("before"))
        area.history.clear()
        area.replace_draft(ChatDraft("after"))
        area.action_undo()
        assert bar.value == "before"
        area.action_redo()
        assert bar.value == "after"
