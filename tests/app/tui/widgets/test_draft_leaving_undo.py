# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Undo after a draft leaves the input box: the whole draft comes back, whatever it holds."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.events import Paste

from chrys.app.tui.widgets.chrome.input_bar import InputBar, _ChatTextArea
from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft, ShortcutIntent
from chrys.foundation.text.draft_references import iter_reference_labels
from tests.support.waiting import wait_for

_FOUR_LINES = "one\ntwo\nthree\nfour"
_DEPLOY = ShortcutIntent("deploy", "", "")


class _App(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[InputBar.UserSubmitted] = []
        self.place = "here"

    def compose(self) -> ComposeResult:
        input_bar = InputBar()
        input_bar.draft_place = lambda: self.place
        yield input_bar

    def on_input_bar_user_submitted(self, event: InputBar.UserSubmitted) -> None:
        self.submitted.append(event)


def _parts(app: _App) -> tuple[InputBar, _ChatTextArea]:
    input_bar = app.query_one(InputBar)
    return input_bar, input_bar.query_one("#chat-input", _ChatTextArea)


def _live(text_area: _ChatTextArea) -> list[str]:
    return [
        text_area.text[label.start : label.end] for label in iter_reference_labels(text_area.text, text_area.references)
    ]


async def _send(app: _App, pilot, *, presses: int = 1) -> InputBar.UserSubmitted:
    count = len(app.submitted)
    for _ in range(presses):
        await pilot.press("enter")
    await wait_for(lambda: len(app.submitted) > count, pilot=pilot, description="the draft was sent")
    input_bar, _text_area = _parts(app)
    assert input_bar.value == ""
    return app.submitted[-1]


async def _labelled(text_area: _ChatTextArea) -> None:
    text_area.insert("see ")
    await text_area._on_paste(Paste(_FOUR_LINES))
    assert text_area.insert_reference("quote", "said")


@pytest.mark.asyncio
async def test_plain_text_comes_back_after_it_is_sent() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        await pilot.press(*"hello")
        await _send(app, pilot)

        text_area.action_undo()

        assert input_bar.value == "hello"


@pytest.mark.asyncio
async def test_a_sent_draft_with_labels_comes_back_with_its_labels_live() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        await _labelled(text_area)
        sent = await _send(app, pilot)

        text_area.action_undo()

        assert _live(text_area) == ["[Pasted text #1 +3 lines]", "[Quote #1]"]
        assert input_bar.draft().sent_text() == sent.text


@pytest.mark.asyncio
async def test_the_next_draft_after_a_send_numbers_its_labels_from_one() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        _input_bar, text_area = _parts(app)
        text_area.focus()
        await _labelled(text_area)
        await _send(app, pilot)

        assert text_area.insert_reference("quote", "next")

        assert _live(text_area) == ["[Quote #1]"]


@pytest.mark.asyncio
async def test_a_sent_shortcut_draft_comes_back_with_its_fields_and_owner() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        input_bar.set_draft(ChatDraft("run !`git diff` on {path}", ((19, 25),), _DEPLOY))
        await pilot.pause()
        # Unfilled fields: the first Enter warns, the second sends.
        await _send(app, pilot, presses=2)
        assert text_area.draft_owner is None

        text_area.action_undo()

        assert input_bar.draft() == ChatDraft("run !`git diff` on {path}", ((19, 25),), _DEPLOY)


@pytest.mark.asyncio
async def test_a_sent_shortcut_draft_with_labels_comes_back_whole() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        input_bar.set_draft(ChatDraft("check ", (), _DEPLOY))
        text_area.move_cursor(text_area.document.end)
        assert text_area.insert_reference("quote", "said")
        before = input_bar.draft()
        await _send(app, pilot)

        text_area.action_undo()

        assert input_bar.draft() == before
        assert _live(text_area) == ["[Quote #1]"]


@pytest.mark.asyncio
async def test_undo_takes_back_what_was_typed_after_the_send_first() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        await _labelled(text_area)
        sent = await _send(app, pilot)
        await pilot.press(*"new")

        text_area.action_undo()
        assert input_bar.value == ""

        text_area.action_undo()
        assert input_bar.draft().sent_text() == sent.text


@pytest.mark.asyncio
async def test_redo_back_over_a_send_keeps_the_next_drafts_labels_live() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        await _labelled(text_area)
        await _send(app, pilot)
        await text_area._on_paste(Paste("a\nb\nc\nd"))
        next_draft = input_bar.draft()

        text_area.action_undo()  # the paste
        text_area.action_undo()  # the send
        assert _live(text_area) == ["[Pasted text #1 +3 lines]", "[Quote #1]"]
        text_area.action_redo()  # the send
        assert input_bar.value == ""
        text_area.action_redo()  # the paste

        assert input_bar.draft() == next_draft
        assert _live(text_area) == ["[Pasted text #1 +3 lines]"]
        assert input_bar.draft().sent_text() == "a\nb\nc\nd"


@pytest.mark.asyncio
async def test_an_owner_does_not_come_back_once_the_session_or_folder_changed() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        input_bar.set_draft(ChatDraft("run !`git diff`", (), _DEPLOY))
        text_area.move_cursor(text_area.document.end)
        assert text_area.insert_reference("quote", "said")
        await _send(app, pilot)
        app.place = "elsewhere"

        text_area.action_undo()

        assert input_bar.value.startswith("run !`git diff`")
        assert _live(text_area) == ["[Quote #1]"]
        assert text_area.draft_owner is None


@pytest.mark.asyncio
async def test_a_message_taken_in_by_the_running_turn_comes_back_on_undo() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)):
        input_bar, text_area = _parts(app)
        text_area.focus()
        await _labelled(text_area)
        before = input_bar.draft()

        input_bar.unlock_and_clear()
        assert input_bar.value == ""
        text_area.action_undo()

        assert input_bar.draft() == before


@pytest.mark.asyncio
async def test_a_refused_draft_handed_back_does_not_come_back_a_second_time() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        await text_area._on_paste(Paste(_FOUR_LINES))
        before = input_bar.draft()
        await _send(app, pilot)

        assert input_bar.restore_draft(before)
        assert input_bar.draft() == before
        assert _live(text_area) == ["[Pasted text #1 +3 lines]"]

        # The send did not happen after all: no redo sends it away again.
        text_area.action_redo()
        assert input_bar.draft() == before
        # Undo now takes back the paste, as before the send; nothing brings the draft back again.
        text_area.action_undo()
        text_area.action_undo()
        assert input_bar.value == ""


@pytest.mark.asyncio
async def test_a_draft_handed_back_changed_is_written_as_given() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        input_bar.set_draft(ChatDraft("run !`git diff`", (), _DEPLOY))
        await _send(app, pilot)

        # Handed back without its owner (another session): a new write, not the undo.
        assert input_bar.restore_draft(ChatDraft("run !`git diff`"))
        assert input_bar.draft() == ChatDraft("run !`git diff`")


@pytest.mark.asyncio
async def test_an_empty_composer_leaves_no_step_behind() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        _input_bar, text_area = _parts(app)
        text_area.focus()
        await pilot.press(*"hi")
        text_area.select_all()
        text_area.action_cut()
        steps = len(text_area.history.undo_stack)

        text_area.leave_draft()

        assert len(text_area.history.undo_stack) == steps


@pytest.mark.asyncio
async def test_undo_never_brings_back_an_owner_the_hand_back_dropped() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        input_bar.set_draft(ChatDraft("run !`git diff`", (), _DEPLOY))
        await _send(app, pilot)

        # Its shortcut no longer loads: it comes back as plain text, in the same place.
        assert input_bar.restore_draft(ChatDraft("run !`git diff`"))
        for _ in range(3):
            text_area.action_undo()
            assert text_area.draft_owner is None


@pytest.mark.asyncio
async def test_a_hand_back_with_rewritten_text_leaves_no_undo_to_the_draft_it_replaced() -> None:
    app = _App()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        input_bar.set_draft(ChatDraft("look ", (), _DEPLOY))
        text_area.move_cursor(text_area.document.end)
        assert text_area.insert_reference("image", "/tmp/shot.png")
        await _send(app, pilot)

        # Handed back with its image as a path (the model takes no images).
        assert input_bar.restore_draft(ChatDraft("look @/tmp/shot.png "))
        assert input_bar.draft() == ChatDraft("look @/tmp/shot.png ")
        for _ in range(3):
            text_area.action_undo()
            assert text_area.draft_owner is None
            assert _live(text_area) == []
