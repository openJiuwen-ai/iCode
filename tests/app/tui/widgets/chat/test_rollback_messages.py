# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rollback entry affordances, retained turn numbering and archived tool replay."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from textual.widgets import Button

from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    InterruptedMessage,
    MessageCopyButton,
    MessageRollbackButton,
    ThinkingMessage,
    UserMessage,
)
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.replay import HistoryReplayPlanner, ReplayAgentMessage, ReplayToolCall
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.patches.textual_precompose import precompose_tree
from chrys.foundation.trajectory.metadata import ANALYTICS_ITEM_ID_KEY, OPERATION_ID_KEY
from chrys.kernel import Content, Message
from chrys.service.context.providers.history import CompressedBlock
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import ChatPanelApp, WidgetApp, click_when_settled
from tests.support.waiting import wait_for


@pytest.mark.parametrize("compressed", [False, True])
async def test_opener_link_targets_before_its_turn_and_disables_immediately(compressed: bool) -> None:
    message = UserMessage("Edit me", compressed=compressed, turn_number=4, rollback_available=True)
    requests: list[UserMessage.RollbackRequested] = []

    class App(WidgetApp):
        def on_user_message_rollback_requested(self, event: UserMessage.RollbackRequested) -> None:
            requests.append(event)

    async with App(lambda: message).run_test(size=(60, 20)) as pilot:
        button = message.query_one(MessageRollbackButton)
        assert button.display
        copy = message.query_one(MessageCopyButton)
        assert copy.display
        assert copy.render().plain == "Copy"
        assert button.render().plain == "↺ Discard this turn and later"
        assert button.region.right == message.query_one("MessageHeaderRow").content_region.right - 1
        await click_when_settled(pilot, button)
        assert [(request.message, request.target_turn) for request in requests] == [(message, 3)]
        assert button.disabled
        await click_when_settled(pilot, button)
        assert len(requests) == 1
        message.set_rollback_disabled(False)
        await click_when_settled(pilot, button)
        assert len(requests) == 2
        assert message.query_one(MessageRollbackButton).region.height == 1
        await message.remove()
        # A successful rollback removes the source before its completion callback runs.
        message.set_rollback_disabled(False)


@pytest.mark.parametrize("source", ["user", "session_closed", "error", "new_source"])
@pytest.mark.parametrize("action", ["Continue", "Retry", None])
async def test_interrupt_rollback_shares_action_row_and_hides_with_continue(source: str, action: str | None) -> None:
    message = InterruptedMessage("Stopped", source, action_label=action, offers_discard=action == "Continue")
    requests: list[InterruptedMessage.RollbackRequested] = []

    class App(WidgetApp):
        def on_interrupted_message_rollback_requested(self, event: InterruptedMessage.RollbackRequested) -> None:
            requests.append(event)

    async with App(lambda: message).run_test(size=(60, 20)) as pilot:
        rollback = list(message.query(".status-rollback-btn").results(Button))
        assert len(rollback) == (1 if action == "Continue" else 0)
        if rollback:
            action = message.query_one(".status-action-btn", Button)
            assert rollback[0].region.y == action.region.y
            assert rollback[0].region.x == action.region.right + 1
            await click_when_settled(pilot, rollback[0])
            assert len(requests) == 1
            assert rollback[0].disabled
            message.set_rollback_disabled(False)
            assert not rollback[0].disabled
        message.hide_action()
        if action is not None:
            assert message.query_one(".status-action-row").display is False


async def test_non_openers_and_unavailable_targets_have_no_link() -> None:
    messages = [
        UserMessage("Legacy"),
        UserMessage("Pruned", turn_number=2),
        UserMessage("Continue", turn_number=3, rollback_available=True, is_injection=True),
    ]
    async with WidgetApp(lambda: messages).run_test():
        assert all(not message.query_one(MessageRollbackButton).display for message in messages)


@pytest.mark.parametrize("available", [True, False])
async def test_link_change_before_a_composed_replay_entry_mounts_is_kept(available: bool) -> None:
    """Replay composes a batch, then mounts it after an await; a target refresh may land in between."""
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        message = UserMessage("old turn", turn_number=3, rollback_available=not available)
        precompose_tree([message])
        message.set_rollback_available(available)
        await panel.mount(message)
        await wait_for(lambda: message.is_mounted, pilot=pilot, description="replayed opener mounted")
        assert message.query_one(MessageRollbackButton).display is available


async def test_archived_tool_group_uses_the_thinking_gray_guide_without_opacity() -> None:
    group = ToolGroup(live=False, is_compressed=True)
    thinking = ThinkingMessage("Archived thinking", is_compressed=True)
    async with WidgetApp(lambda: [group, thinking]).run_test():
        assert group.has_class("--compressed")
        assert group.styles.opacity == 1.0
        assert group.styles.border_left == thinking.styles.border_left
        assert group.query_one("#tg-content").styles.border_left == thinking.styles.border_left
        assert group.styles.color == thinking.styles.color


@pytest.mark.parametrize("with_output", [False, True])
async def test_fresh_turn_after_unanswered_continuation_keeps_only_real_output(with_output: bool) -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("Original")
        panel.apply_turn_started(1)
        if with_output:
            await panel.add_agent_message("Partial answer")
        await panel.add_user_message("Continue with this", is_injection=True, item_id="note")
        await panel.add_error("Failed without further output")
        await panel.add_user_message("New question")
        await panel.apply_fresh_turn_cleanup(
            retired_previous_turn=not with_output, removed_item_ids=("note",) if with_output else ("opener", "note")
        )
        panel.apply_turn_started(2 if with_output else 1)
        assert [message.text for message in panel.query(UserMessage)] == (
            ["Original", "New question"] if with_output else ["New question"]
        )
        assert [item.turn_index for item in panel.toc_items] == ([1, 2] if with_output else [1])


async def test_fresh_turn_removes_all_unanswered_continuations_and_their_thinking() -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("Original")
        panel.apply_turn_started(1)
        await panel.add_agent_message("Partial answer")
        await panel.add_thinking_delta("kept", "reasoning", "Earlier thinking")
        panel.finish_thinking_segment("kept")
        await panel.add_user_message("First continuation", is_injection=True, item_id="first")
        await panel.add_thinking_delta("first", "reasoning", "First unlanded thought")
        panel.finish_thinking_segment("first")
        await panel.add_user_message("Second continuation", is_injection=True, item_id="second")
        await panel.add_thinking_delta("second", "reasoning", "Second unlanded thought")
        panel.finish_thinking_segment("second")
        await panel.add_error("Failed without further output")

        await panel.add_user_message("New question")
        await panel.apply_fresh_turn_cleanup(retired_previous_turn=False, removed_item_ids=("first", "second"))
        panel.apply_turn_started(2)

        assert [message.text for message in panel.query(UserMessage)] == ["Original", "New question"]
        assert [message.text for message in panel.query(ThinkingMessage)] == ["Earlier thinking"]
        assert [item.turn_index for item in panel.toc_items] == [1, 2]


def _saved_user(text: str, item_id: str, *, injected: bool = False) -> dict[str, object]:
    properties: dict[str, object] = {ANALYTICS_ITEM_ID_KEY: item_id}
    if injected:
        properties[HistoryMarkerKind.INJECTED_KEY] = True
    return Message("user", [text], additional_properties=properties).to_dict()


async def test_restored_note_leaves_by_the_history_item_it_was_replayed_with() -> None:
    """A restored session's notes carry their saved items, so the next prompt's cleanup finds them too."""
    messages = [
        _saved_user("Original", "opener"),
        Message("assistant", ["Partial answer"]).to_dict(),
        _saved_user("Answered note", "answered", injected=True),
        Message("assistant", ["Answer to the note"]).to_dict(),
        _saved_user("Unanswered note", "unanswered", injected=True),
        Message(
            "assistant", ["Stopped"], additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED}
        ).to_dict(),
        _turn_marker(1).to_dict(),
    ]
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(messages)
        await panel.wait_replay_complete()
        assert [(message.text, message.history_item_id) for message in panel.query(UserMessage)] == [
            ("Original", "opener"),
            ("Answered note", "answered"),
            ("Unanswered note", "unanswered"),
        ]

        await panel.add_user_message("New question")
        await panel.apply_fresh_turn_cleanup(retired_previous_turn=False, removed_item_ids=("unanswered",))
        panel.apply_turn_started(2)

        assert [message.text for message in panel.query(UserMessage)] == ["Original", "Answered note", "New question"]


def _turn_marker(number: int) -> Message:
    return Message(
        "assistant", [], additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.TURN, "_turn": number}
    )


def test_modern_call_without_matching_diff_cannot_consume_legacy_reused_id() -> None:
    modern = Content.from_function_call(call_id="reused", name="edit_file", arguments={})
    modern.additional_properties[OPERATION_ID_KEY] = "missing-operation"
    legacy = Content.from_function_call(call_id="reused", name="edit_file", arguments={})
    messages = [
        Message("assistant", [modern]).to_dict(),
        _turn_marker(1).to_dict(),
        Message("assistant", [legacy]).to_dict(),
    ]
    plan = HistoryReplayPlanner().build_plan(messages, file_snapshots={"reused": [("before", "after")]})
    calls = [
        content
        for entry in plan.entries
        if isinstance(entry, ReplayAgentMessage)
        for content in entry.contents
        if isinstance(content, ReplayToolCall)
    ]
    assert [call.file_snapshot for call in calls] == [None, ("before", "after")]


@pytest.mark.parametrize("legacy", [False, True])
async def test_archived_tools_copy_and_diff_pair_by_operation_not_reused_provider_id(legacy: bool) -> None:
    def edit(operation: str) -> Content:
        call = Content.from_function_call(call_id="reused", name="edit_file", arguments={"path": "a.txt"})
        if not legacy:
            call.additional_properties[OPERATION_ID_KEY] = operation
        return call

    first = edit("old-op")
    second = edit("new-op")
    block = CompressedBlock(
        compressed_context_id="archive",
        messages=[
            Message("user", [Content.from_text("Old question")]),
            Message("assistant", [first]),
            Message("tool", [Content.from_function_result(call_id="reused", result="Edited")]),
            Message("assistant", [Content.from_text("Old answer")]),
            _turn_marker(1),
        ],
    )
    messages = [
        {
            "role": "assistant",
            "contents": [],
            "additional_properties": {HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY, "_block_id": "archive"},
        },
        Message("user", [Content.from_text("New question")]).to_dict(),
        Message("assistant", [second]).to_dict(),
        Message("tool", [Content.from_function_result(call_id="reused", result="Edited")]).to_dict(),
        _turn_marker(2).to_dict(),
    ]
    snapshots = (
        {"reused": [("new before", "new after")]}
        if legacy
        else {
            ("operation", "old-op"): [("old before", "old after")],
            ("operation", "new-op"): [("new before", "new after")],
            ("operation", "child-op"): [("child before", "child after")],
        }
    )
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(
            messages, compressed_blocks={"archive": block}, file_snapshots=snapshots, rollback_targets={0, 1}
        )
        await panel.wait_replay_complete()
        old, new = panel.query(UserMessage)
        assert [old.turn_number, new.turn_number] == [1, 2]
        assert old.query_one(MessageCopyButton).display
        assert old.query_one(MessageRollbackButton).display
        old_answer = next(message for message in panel.query(AgentMessage) if message.text == "Old answer")
        assert old_answer.query_one(MessageCopyButton).display
        groups = list(panel.query(ToolGroup))
        assert len(groups) == 2
        assert groups[0].has_class("--compressed") and not groups[1].has_class("--compressed")
        records = [next(iter(group._tool_records.values())) for group in groups]
        assert records[0].file_snapshot == (None if legacy else ("old before", "old after"))
        assert records[1].file_snapshot == ("new before", "new after")


async def test_legacy_provider_id_cannot_alias_a_modern_operation_snapshot_in_an_archive() -> None:
    provider_id = "operation:abc"

    def edit(*, modern: bool) -> Content:
        call = Content.from_function_call(call_id=provider_id, name="edit_file", arguments={"path": "a.txt"})
        if modern:
            call.additional_properties[OPERATION_ID_KEY] = "abc"
        return call

    def turn(number: int, *, modern: bool) -> list[Message]:
        return [
            Message("user", [Content.from_text(f"Question {number}")]),
            Message("assistant", [edit(modern=modern)]),
            Message("tool", [Content.from_function_result(call_id=provider_id, result="Edited")]),
            _turn_marker(number),
        ]

    block = CompressedBlock(compressed_context_id="collision", messages=turn(1, modern=False))
    summary = Message(
        "assistant",
        [],
        additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY, "_block_id": "collision"},
    )
    messages = [summary, *turn(2, modern=True), *turn(3, modern=False)]
    snapshots = {
        ("operation", "abc"): [("modern before", "modern after")],
        provider_id: [("legacy before", "legacy after")],
    }
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(
            [message.to_dict() for message in messages],
            compressed_blocks={"collision": block},
            file_snapshots=snapshots,
        )
        await panel.wait_replay_complete()
        records = [next(iter(group._tool_records.values())) for group in panel.query(ToolGroup)]
        # The archived legacy call never gets a guessed diff; neither independently keyed live diff is consumed by it.
        assert [record.file_snapshot for record in records] == [
            None,
            ("modern before", "modern after"),
            ("legacy before", "legacy after"),
        ]


async def test_pruning_one_target_repaints_only_its_header_on_populated_main_screen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(100, 32)) as pilot:
        screen = app.screen
        assert isinstance(screen, MainScreen)
        panel = screen.query_one(ChatPanel)
        messages = [UserMessage(f"Question {i}", turn_number=i, rollback_available=True) for i in range(2, 32)]
        await panel.mount(*messages)
        panel.set_rollback_targets(set(range(31)))
        await wait_for(
            lambda: screen_is_settled(app, screen),
            pilot=pilot,
            description="populated transcript settled",
        )
        layouts = Mock()
        styles = Mock()
        footer = Mock()
        monkeypatch.setattr(screen, "_refresh_layout", layouts)
        monkeypatch.setattr(screen, "update_node_styles", styles)
        monkeypatch.setattr(screen.query_one(ChrysFooter), "recompose", footer)
        untouched = Mock(wraps=messages[10].refresh)
        monkeypatch.setattr(messages[10], "refresh", untouched)
        panel.set_rollback_targets({0, *range(2, 31)})
        await pilot.pause()
        assert not messages[0].query_one(MessageRollbackButton).display
        assert all(message.query_one(MessageRollbackButton).display for message in messages[1:])
        layouts.assert_not_called()
        styles.assert_not_called()
        footer.assert_not_called()
        untouched.assert_not_called()


@pytest.mark.parametrize(
    ("locale", "label", "continue_label"),
    [("en", "Discard This Turn", "Continue"), ("zh-Hans", "撤回本轮", "继续"), ("zh-Hans", "撤回本轮", "Resume now")],
)
async def test_rollback_entries_use_their_context_specific_localized_labels(
    locale: str, label: str, continue_label: str
) -> None:
    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.config.settings import Settings

    opener = UserMessage("Question", turn_number=1, rollback_available=True)
    interrupted = InterruptedMessage("Stopped", "new_source", action_label=continue_label, offers_discard=True)
    app = WidgetApp(lambda: [opener, interrupted])
    app.locale_controller = LocaleController(Settings(locale=locale))
    async with app.run_test():
        assert opener.query_one(MessageRollbackButton).render().plain == (
            "↺ Discard this turn and later" if locale == "en" else "↺ 撤回本轮及之后"
        )
        assert str(interrupted.query_one(".status-rollback-btn", Button).label) == label
