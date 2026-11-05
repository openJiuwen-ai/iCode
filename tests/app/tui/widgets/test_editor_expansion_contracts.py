# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Whole-text editor ownership and the text actually delivered to a mock model."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from textual.app import App, ComposeResult

from chrys.app.tui.screens.dialogs.editor import EditorDialog
from chrys.app.tui.screens.dialogs.vision_unsupported import VisionUnsupportedDialog
from chrys.app.tui.util.git_branch import GitBranchMonitor
from chrys.app.tui.widgets.chrome.draft_editor import DraftEditor
from chrys.app.tui.widgets.chrome.input_bar import InputBar, _ChatTextArea
from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft, ShortcutIntent
from chrys.foundation.events import types as ev
from chrys.foundation.text.draft_references import EMPTY_REFERENCES
from chrys.foundation.text.mentions import format_file_mention
from tests.app.tui.screens.test_prompt_shortcut_send import MAIN_MODEL, mock_model, running_session, turn_finished
from tests.support.engines import AgentEngineFactory
from tests.support.secure_files import plant_owner_only_bytes
from tests.support.tui_helpers import click_when_settled, png_bytes
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for


@pytest.fixture(autouse=True)
def _no_preferences_or_branch_observers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chrys.app.tui.screens.main.screen.persist_editor_keymap", lambda _mode: None)
    monkeypatch.setattr(GitBranchMonitor, "_start_observer", lambda _monitor, _target: None)


class _OwnershipApp(App[None]):
    def compose(self) -> ComposeResult:
        yield InputBar()
        yield DraftEditor(
            draft=ChatDraft("original", owner=ShortcutIntent("fix", "Code", "model")),
            paste_cwd=str(Path.cwd()),
            clipboard_image_dir=None,
        )


@pytest.mark.asyncio
async def test_clearing_editor_then_typing_drops_the_shortcut_owner_without_reviving_it_on_undo() -> None:
    app = _OwnershipApp()
    async with app.run_test():
        bar = app.query_one(InputBar)
        editor = app.query_one(DraftEditor)
        area = bar.query_one(_ChatTextArea)
        bar.set_draft(editor.chat_draft())
        editor.clear()
        editor.insert("new text")
        assert editor.chat_draft().owner is None
        area.replace_draft(editor.chat_draft())

        assert area.draft_owner is None
        area.undo()
        assert area.text == "original"
        assert area.draft_owner is None
        area.redo()
        assert area.text == "new text"
        assert area.draft_owner is None


@pytest.mark.asyncio
async def test_empty_editor_result_undo_restores_fields_and_labels_but_keeps_owner_ended() -> None:
    app = _OwnershipApp()
    async with app.run_test():
        bar = app.query_one(InputBar)
        area = bar.query_one(_ChatTextArea)
        references, entry = EMPTY_REFERENCES.with_new("paste", "first\nsecond")
        original = ChatDraft(f"FIELD {entry.label}", ((0, 5),), ShortcutIntent("fix", "Code", "model"), references)
        bar.set_draft(original)
        area.replace_draft(ChatDraft(""))
        assert area.draft_owner is None

        area.undo()
        assert area.chat_draft() == replace(original, owner=None)
        area.redo()
        assert area.text == ""
        assert area.prompt_fields == ()
        assert area.draft_owner is None


@pytest.mark.asyncio
@pytest.mark.parametrize("vision", [False, True])
async def test_edited_expanded_quotes_and_image_reach_the_mock_model_in_their_send_form(
    agent_engine: AgentEngineFactory, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, vision: bool
) -> None:
    from chrys.service.profiles.models.registry import ModelProfileRegistry

    image = tmp_path / "shot.png"
    await asyncio.to_thread(plant_owner_only_bytes, image, png_bytes((20, 30, 40)))
    models = ModelProfileRegistry()
    models.register(replace(mock_model(MAIN_MODEL, "Main"), vision=vision))
    references = EMPTY_REFERENCES
    references, single = references.with_new("quote", "@do-not-attach.py !`do-not-run`")
    references, multi = references.with_new("quote", "first\n@also-inert.py !`still-inert`")
    references, picture = references.with_new("image", str(image))
    draft = ChatDraft(f"Review {single.label} {multi.label} {picture.label}", references=references)
    sending = (
        "Review \n> \\@do-not-attach.py \\!`do-not-run`\n\n \n"
        "> first\n> \\@also-inert.py \\!`still-inert`\n\n " + format_file_mention(str(image))
    )
    async with running_session(agent_engine, tmp_path, monkeypatch, models=models) as session:
        session.bar.set_draft(draft)
        await session.pilot.press("ctrl+o")
        await wait_for(
            lambda: isinstance(session.app.screen, EditorDialog) and session.app.screen.is_mounted,
            pilot=session.pilot,
            description="whole send text editor mounted",
        )
        editor = session.app.screen.query_one(DraftEditor)
        assert editor.text == sending
        editor.insert("!", editor.document.end)
        await session.pilot.press("ctrl+o")
        await wait_for(
            lambda: session.app.screen is session.main, pilot=session.pilot, description="edited send text saved"
        )
        assert session.bar.draft() == ChatDraft(sending + "!")
        await session.pilot.press("enter")
        if not vision:
            await wait_for(
                lambda: isinstance(session.app.screen, VisionUnsupportedDialog),
                pilot=session.pilot,
                timeout=ENGINE_TURN_TIMEOUT,
                description="text-only model image fallback",
            )
            await click_when_settled(session.pilot, "#vision-unsupported-use-paths")
            await wait_for(
                lambda: session.app.screen is session.main,
                pilot=session.pilot,
                description="image path fallback returned",
            )
            sending = sending.replace(format_file_mention(str(image)), str(image))
            assert session.bar.value == sending + "!"
            await session.pilot.press("enter")
        await wait_for(
            lambda: bool(session.model.call_history),
            pilot=session.pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="expanded draft reached the model",
        )
        await turn_finished(session)
        submitted = session.seen.of(ev.UserMessage)[-1]
        assert submitted.text == sending + "!"
        messages, _options = session.model.call_history[-1]
        user = next(message for message in reversed(messages) if message.role == "user")
        assert user.contents[0].text == sending + "!"
        image_contents = [content for content in user.contents if content.type == "data"]
        assert len(image_contents) == (1 if vision else 0)
