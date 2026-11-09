# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Discard entry points as a user meets them: visible controls and real clicks."""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Button, Select

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.diff.rollback_modal import RollbackModal
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    ErrorMessage,
    InterruptedMessage,
    MessageCopyButton,
    MessageRollbackButton,
    UserMessage,
)
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import BaseToolCard, ToolGroup
from chrys.app.tui.widgets.welcome import WelcomeWidget
from chrys.foundation.config.settings import Settings
from chrys.foundation.events import types as ev
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import Message
from chrys.service.context.providers.history import CompressedBlock
from chrys.service.mutations.types import MutationOp, MutationSource
from tests.app.tui.screens.test_prompt_shortcut_send import hold_next, notices, running_session
from tests.support.event_capture import capture_events
from tests.support.rollback_spec import drain
from tests.support.tui_helpers import ChatPanelApp, click_when_settled
from tests.support.waiting import wait_for

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


@pytest.fixture(autouse=True)
def no_native_git_watcher(monkeypatch):
    """The rollback contract does not involve macOS native Git watching."""
    monkeypatch.setattr(
        "chrys.app.tui.util.git_branch.GitBranchMonitor._start_observer", lambda _monitor, _target: None
    )


def visible_button_texts(widget):
    return [str(b.label) for b in widget.query(Button) if b.display and b.parent.display]


@pytest.mark.parametrize("locale,label", [("en", "Discard This Turn"), ("zh-Hans", "撤回本轮")])
@pytest.mark.parametrize("source", ["user", "session_close", "crash"])
async def test_interruption_card_offers_rollback_but_error_card_does_not(locale, label, source):
    app = ChatPanelApp()
    app.locale_controller = LocaleController(Settings(locale=locale))
    async with app.run_test(size=(60, 25)) as pilot:
        panel = app.query_one(ChatPanel)
        panel.set_rollback_targets({0})
        await panel.add_user_message("recover this prompt")
        await panel.add_interrupted(source=source)
        await pilot.pause()
        card = panel.query_one(InterruptedMessage)
        assert label in visible_button_texts(card)
        header = panel.query_one(UserMessage).query_one(MessageRollbackButton)
        assert str(header.content) == ("↺ Discard this turn and later" if locale == "en" else "↺ 撤回本轮及之后")
        buttons = [b for b in card.query(Button) if b.display]
        assert len(buttons) == 2
        assert buttons[0].region.y == buttons[1].region.y
        await panel.add_error("provider error")
        await pilot.pause()
        assert label not in visible_button_texts(panel.query_one(ErrorMessage))


async def test_only_turn_start_has_visible_rollback_link():
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.set_rollback_targets({0})
        await panel.add_user_message("opening prompt")
        panel.apply_turn_started(1)  # the backend's authoritative ordinal
        await panel.add_agent_message("partial answer")
        await panel.add_interrupted()
        await panel.prepare_retry()
        await panel.add_user_message("continue with this note", is_injection=True)
        await pilot.pause()
        opening, note = list(panel.query(UserMessage))
        assert opening.query_one(MessageRollbackButton).display
        assert not any(b.display and b.parent.display for b in note.query(MessageRollbackButton))


async def test_compacted_originals_keep_copy_rollback_and_tool_rows():
    marker = Message("assistant", [""])
    marker.additional_properties.update(
        {HistoryMarkerKind.KEY: HistoryMarkerKind.TURN, "_turn": 1, "_turn_id": "turn_1"}
    )
    originals = [
        Message("user", ["original compacted question"]),
        Message.from_dict(
            {
                "role": "assistant",
                "contents": [
                    {
                        "type": "function_call",
                        "call_id": "read-1",
                        "name": "read_file",
                        "arguments": {"path": "work.txt"},
                    }
                ],
            }
        ),
        Message.from_dict(
            {"role": "tool", "contents": [{"type": "function_result", "call_id": "read-1", "result": "file contents"}]}
        ),
        Message("assistant", ["original compacted reply"]),
        marker,
    ]
    block = CompressedBlock(
        compressed_context_id="spec-compressed",
        messages=originals,
        summary_text="summary",
        marker_id="turn_1",
        turn_range=(1, 1),
    )
    summary = {
        "role": "assistant",
        "contents": [{"type": "text", "text": "summary"}],
        "additional_properties": {HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY, "_block_id": "spec-compressed"},
    }
    async with ChatPanelApp(tool_groups_expanded=lambda: True).run_test(size=(100, 35)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.set_tool_kinds({"read_file": "read_file"})
        await panel.replay_history([summary], compressed_blocks={"spec-compressed": block}, rollback_targets={0})
        await panel.wait_replay_complete()
        await pilot.pause()
        user = panel.query_one(UserMessage)
        assert user.text == "original compacted question"
        assert user.is_compressed
        assert user.query_one(MessageCopyButton).display
        assert user.query_one(MessageRollbackButton).display
        reply = next(m for m in panel.query(AgentMessage) if m.text == "original compacted reply")
        assert reply.query_one(MessageCopyButton).display
        group = panel.query_one(ToolGroup)
        group.collapsed = False
        await wait_for(
            lambda: bool(list(group.query(BaseToolCard))), pilot=pilot, description="compacted tool row mounted"
        )
        assert len(group.query(BaseToolCard)) == 1


async def interrupt_session(session):
    await session.pilot.press("escape")
    await wait_for(lambda: isinstance(session.app.screen, ConfirmDialog), pilot=session.pilot)
    await click_when_settled(session.pilot, "#confirm-yes")


def bound_openers(session, text):
    return {m.history_item_id for m in session.main.query(UserMessage) if m.text == text and m.history_item_id}


async def submit(session, text):
    earlier = bound_openers(session, text)
    session.chat.load_text(text)
    await session.pilot.press("enter")
    # The run task exists only once the submission is admitted; draining before
    # then finishes at once, with this turn not yet started.
    await wait_for(
        lambda: bool(bound_openers(session, text) - earlier),
        pilot=session.pilot,
        description=f"turn {text!r} started",
    )
    await drain(session.engine)
    await wait_for(lambda: not session.bar.agent_running, pilot=session.pilot)


def opening_message(session, text):
    return next(m for m in session.main.query(UserMessage) if m.text == text)


async def click_message_rollback(session, text):
    button = opening_message(session, text).query_one(MessageRollbackButton)
    button.scroll_visible(animate=False, immediate=True)
    await click_when_settled(session.pilot, button)


async def confirm_message_rollback(session, text):
    """A header link always asks first; the latest turn without file changes asks with a plain confirm."""
    await click_message_rollback(session, text)
    await wait_for(lambda: isinstance(session.app.screen, ConfirmDialog), pilot=session.pilot)
    await click_when_settled(session.pilot, "#confirm-yes")


@pytest.mark.parametrize("interrupted", [False, True], ids=["completed", "interrupted"])
async def test_last_turn_without_files_one_click_restores_text_and_focus(
    agent_engine, tmp_path, monkeypatch, interrupted
):
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        await submit(session, "keep this turn")
        if interrupted:
            hold = session.hold()
            session.model.hold = hold
            session.chat.load_text("recover my second prompt")
            await session.pilot.press("enter")
            await wait_for(hold.entered.is_set, pilot=session.pilot)
            await interrupt_session(session)
            await drain(session.engine)
            await wait_for(lambda: bool(list(session.main.query(InterruptedMessage))), pilot=session.pilot)
            card = session.main.query_one(InterruptedMessage)
            button = next(b for b in card.query(Button) if str(b.label) == "Discard This Turn")
            button.scroll_visible(animate=False, immediate=True)
            await click_when_settled(session.pilot, button)
            # The card's Discard asks first too, like the header link.
            await wait_for(lambda: isinstance(session.app.screen, ConfirmDialog), pilot=session.pilot)
            await click_when_settled(session.pilot, "#confirm-yes")
        else:
            await submit(session, "recover my second prompt")
            await confirm_message_rollback(session, "recover my second prompt")
        await wait_for(lambda: session.chat.text == "recover my second prompt", pilot=session.pilot)
        await wait_for(lambda: session.app.screen is session.main, pilot=session.pilot)
        assert session.chat.has_focus
        assert [m.text for m in session.main.query(UserMessage)] == ["keep this turn"]
        assert session.engine.current_turn_number == 1
        assert session.chat.cursor_location == session.chat.document.end


async def test_first_turn_one_click_returns_welcome(agent_engine, tmp_path, monkeypatch):
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        await submit(session, "mis-sent first message")
        directory = session.engine.session_dir
        await confirm_message_rollback(session, "mis-sent first message")
        await wait_for(lambda: session.chat.text == "mis-sent first message", pilot=session.pilot)
        await wait_for(lambda: session.app.screen is session.main, pilot=session.pilot)
        assert session.chat.has_focus
        assert list(session.main.query(WelcomeWidget))
        assert not list(session.main.query(UserMessage))
        assert not (directory / "session.json").exists()


async def test_cancelled_header_confirm_keeps_the_turn(agent_engine, tmp_path, monkeypatch):
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        await submit(session, "first")
        await submit(session, "kept by cancel")
        results = await capture_events(session.bus, ev.RollbackResult)
        await click_message_rollback(session, "kept by cancel")
        await wait_for(lambda: isinstance(session.app.screen, ConfirmDialog), pilot=session.pilot)
        await session.pilot.press("escape")
        await wait_for(lambda: session.app.screen is session.main, pilot=session.pilot)
        button = opening_message(session, "kept by cancel").query_one(MessageRollbackButton)
        await wait_for(lambda: not button.disabled, pilot=session.pilot)
        assert not results
        assert session.engine.current_turn_number == 2
        assert [m.text for m in session.main.query(UserMessage)] == ["first", "kept by cancel"]


@pytest.mark.parametrize("with_file", [False, True], ids=["multiple-turns", "last-turn-with-file"])
async def test_destructive_selection_opens_picker_at_clicked_turn(agent_engine, tmp_path, monkeypatch, with_file):
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        for text in ("first", "second", "third"):
            await submit(session, text)
        if with_file:
            file = tmp_path / "test.txt"
            file.write_text("before", encoding="utf-8")
            tracker = session.engine.mutation_tracker
            mutation = tracker.record(str(file), MutationOp.MODIFY, MutationSource.EDIT_FILE, "spec-edit")
            assert mutation is not None
            file.write_text("after", encoding="utf-8")
            tracker.record_after(mutation)
            selected, target = "third", 2
        else:
            selected, target = "second", 1
        await click_message_rollback(session, selected)
        await wait_for(lambda: isinstance(session.app.screen, RollbackModal), pilot=session.pilot)
        modal = session.app.screen
        # The picker joins the DOM hidden, before its Mount applies the
        # selection (Select.NULL until then): wait for the revealed content.
        await wait_for(lambda: modal._content_ready, pilot=session.pilot, description="rollback picker ready")
        assert modal.query_one("#rollback-turn-select", Select).value == target
        assert session.engine.current_turn_number == 3
        await session.pilot.press("escape")
        await wait_for(lambda: session.app.screen is session.main, pilot=session.pilot)
        assert not opening_message(session, selected).query_one(MessageRollbackButton).disabled


async def test_running_click_is_refused_and_link_remains_visible(agent_engine, tmp_path, monkeypatch):
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        await submit(session, "first")
        hold = session.hold()
        session.model.hold = hold
        session.chat.load_text("still running")
        await session.pilot.press("enter")
        await wait_for(hold.entered.is_set, pilot=session.pilot)
        results = await capture_events(session.bus, ev.RollbackResult)
        await click_message_rollback(session, "first")
        await wait_for(lambda: bool(notices(session.app)), pilot=session.pilot)
        assert any("running" in str(notice).lower() for notice in notices(session.app))
        assert not results
        assert opening_message(session, "first").query_one(MessageRollbackButton).display
        assert session.app.screen is session.main
        hold.release()
        await drain(session.engine)


@pytest.mark.parametrize("settle", [False, True], ids=["timeout", "finishes-in-time"])
async def test_interrupted_final_save_wait_is_bounded_and_preserves_turn(agent_engine, tmp_path, monkeypatch, settle):
    """Keep the real final SessionSaved handler busy beyond the allowed wait."""
    from chrys.app.tui.screens.main import rollback_controller

    assert rollback_controller.ROLLBACK_WAIT_SECONDS == 10
    # Exercise the bound with a short window; its length is the constant above.
    wait_seconds = 0.5
    monkeypatch.setattr(rollback_controller, "ROLLBACK_WAIT_SECONDS", wait_seconds)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        await submit(session, "baseline")
        hold = session.hold()
        session.model.hold = hold
        session.chat.load_text("interrupt me")
        await session.pilot.press("enter")
        await wait_for(hold.entered.is_set, pilot=session.pilot)
        save = session.hold()
        await hold_next(session.bus, ev.SessionSaved, save)
        await interrupt_session(session)
        await wait_for(save.entered.is_set, pilot=session.pilot)
        lifecycle = session.engine.turn_lifecycle_task
        await wait_for(lambda: bool(list(session.main.query(InterruptedMessage))), pilot=session.pilot)
        card = session.main.query_one(InterruptedMessage)
        button = next(b for b in card.query(Button) if str(b.label) == "Discard This Turn")
        button.scroll_visible(animate=False, immediate=True)
        results = await capture_events(session.bus, ev.RollbackResult)
        started = asyncio.get_running_loop().time()
        await click_when_settled(session.pilot, button)
        assert button.disabled
        assert not results
        if settle:
            save.release()
            # The click waits for the save, then asks before it discards.
            await wait_for(lambda: isinstance(session.app.screen, ConfirmDialog), pilot=session.pilot)
            assert not results
            await click_when_settled(session.pilot, "#confirm-yes")
            await wait_for(lambda: bool(results), pilot=session.pilot)
            assert results[0].rolled_back_user_text == "interrupt me"
            assert session.engine.current_turn_number == 1
            return
        await wait_for(
            lambda: bool(notices(session.app)),
            pilot=session.pilot,
            timeout=wait_seconds + 5,
            description="bounded rollback wait limit",
        )
        elapsed = asyncio.get_running_loop().time() - started
        assert wait_seconds * 0.8 <= elapsed <= wait_seconds + 5
        # The previous turn is still finishing, not running.
        assert any("finishing" in str(notice).lower() for notice in notices(session.app))
        assert lifecycle is not None and not lifecycle.done() and not lifecycle.cancelled()
        assert not button.disabled
        save.release()
        await drain(session.engine)


@pytest.mark.parametrize("command", ["/rollback 1", "/rollback to 1"])
async def test_slash_command_uses_the_same_rollback_and_refills_input(agent_engine, tmp_path, monkeypatch, command):
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        await submit(session, "baseline")
        await submit(session, "second prompt")
        session.chat.load_text(command)
        await session.pilot.press("enter")
        await wait_for(
            lambda: session.chat.text == "second prompt" and session.app.screen is session.main, pilot=session.pilot
        )
        assert session.engine.current_turn_number == 1
        assert [m.text for m in session.main.query(UserMessage)] == ["baseline"]
        assert session.chat.has_focus


async def test_unavailable_target_hides_just_its_link():
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        raw = []
        for n in range(1, 4):
            raw += [
                {"role": "user", "contents": [{"type": "text", "text": f"prompt {n}"}]},
                {"role": "assistant", "contents": [{"type": "text", "text": f"reply {n}"}]},
                {
                    "role": "assistant",
                    "contents": [""],
                    "additional_properties": {
                        HistoryMarkerKind.KEY: HistoryMarkerKind.TURN,
                        "_turn": n,
                        "_turn_id": f"turn_{n}",
                    },
                },
            ]
        await panel.replay_history(raw, rollback_targets={0, 1, 2})
        await panel.wait_replay_complete()
        messages = list(panel.query(UserMessage))
        assert all(m.query_one(MessageRollbackButton).display for m in messages)
        # Drop a middle target: turn 1's link follows the pruning rule instead.
        panel.set_rollback_targets({0, 1})
        await pilot.pause()
        assert [m.query_one(MessageRollbackButton).display for m in messages] == [True, True, False]


async def test_existing_input_is_preserved_on_one_click_rollback(agent_engine, tmp_path, monkeypatch):
    """Discarding must preserve a draft the user already typed."""
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        await submit(session, "first")
        await submit(session, "second")
        session.chat.load_text("my existing draft")
        results = await capture_events(session.bus, ev.RollbackResult)
        await confirm_message_rollback(session, "second")
        await wait_for(lambda: bool(results) and session.app.screen is session.main, pilot=session.pilot)
        assert session.chat.text == "my existing draft"
        assert session.engine.current_turn_number == 1
