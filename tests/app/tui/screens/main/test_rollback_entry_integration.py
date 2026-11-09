# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Main-screen gestures, translated restored cards and bounded archived replay."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest
from textual.widget import Widget
from textual.widgets import Button, TextArea

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.chat.messages import (
    InterruptedMessage,
    MessageCopyButton,
    MessageRollbackButton,
    UserMessage,
)
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.replay_mount import REPLAY_MOUNT_BATCH_SIZE
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.welcome import WelcomeWidget
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import RollbackResult, UserRetry, UserRollback
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import Content, Message
from chrys.service.context.providers.history import CompressedBlock
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from tests.support.tui_app_harness import ShutdownOnlyEngine, make_chrys_app
from tests.support.tui_helpers import ChatPanelApp, click_when_settled, rich_plain
from tests.support.waiting import wait_for

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


@pytest.fixture(autouse=True)
def no_native_branch_observer(monkeypatch: pytest.MonkeyPatch) -> None:
    """These interaction tests do not need a native filesystem observer."""
    monkeypatch.setattr(
        "chrys.app.tui.util.git_branch.GitBranchMonitor._start_observer",
        lambda _monitor, _target: None,
    )


class _FirstTurnEngine(ShutdownOnlyEngine):
    """Real mutation projection with a deterministic first-turn rollback response."""

    current_turn_number = 1
    conversation_revision = 7
    build_generation = 1

    def __init__(self, root: Path) -> None:
        self.workspace_primary_cwd = str(root)
        self.mutation_tracker = MutationTracker(SnapshotStore(root))
        self.mutation_tracker.start_turn(1)
        self.fenced = False

    def available_rollback_turns(self) -> list[int]:
        return [0]

    def turn_prompt_previews(self) -> dict[int, str]:
        return {1: "Edit this prompt"}

    async def begin_rollback_projection(self, *, session_id: str | None, session_generation: int) -> str:
        assert session_id is None
        assert session_generation == self.session_generation
        assert not self.fenced
        self.fenced = True
        return "first-turn-projection"

    def finish_rollback_projection(self, owner: str) -> None:
        assert owner == "first-turn-projection"
        assert self.fenced
        self.fenced = False


@pytest.mark.parametrize("entry", ["interrupted", "message"])
async def test_main_screen_mouse_rollback_needs_no_model_and_restores_first_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    bus = EventBus()
    engine = _FirstTurnEngine(tmp_path / "mutations")
    app = make_chrys_app(tmp_path / "state", engine=engine, event_bus=bus)
    requests: list[UserRollback] = []

    async def restore(event: UserRollback) -> None:
        requests.append(event)
        await bus.publish(
            RollbackResult(session_id="restored", target_turn=0, rolled_back_user_text="Edit this prompt")
        )

    await bus.subscribe(UserRollback, restore)
    async with app.run_test(size=(80, 32)) as pilot:
        screen = app.screen
        assert isinstance(screen, MainScreen)

        def forbidden_model_check() -> bool:
            raise AssertionError("Rollback must not consult model configuration")

        monkeypatch.setattr(screen, "_model_unconfigured", forbidden_model_check)
        panel = screen.query_one(ChatPanel)
        await panel.add_user_message("Edit this prompt")
        panel.apply_turn_started(1)
        await panel.add_interrupted("Execution interrupted by user")
        screen.query_one(InputBar).retry_mode = True
        if entry == "interrupted":
            button = panel.query_one(InterruptedMessage).query_one(".status-rollback-btn", Button)
        else:
            button = panel.query_one(UserMessage).query_one(MessageRollbackButton)
        await click_when_settled(pilot, button)
        # Both entries ask even for the latest turn without file changes.
        await wait_for(lambda: isinstance(app.screen, ConfirmDialog), pilot=pilot)
        assert requests == []
        await click_when_settled(pilot, "#confirm-yes")
        await wait_for(
            lambda: (
                len(requests) == 1 and screen.query_one(InputBar).value == "Edit this prompt" and app.screen is screen
            ),
            pilot=pilot,
            description="first-turn rollback restores the welcome composer",
        )
        assert requests[0].expected_conversation_revision == 7
        assert requests[0].relative_turns == (1 if entry == "interrupted" else None)
        assert requests[0].target_turn == 0
        assert not list(panel.query(UserMessage))
        assert not list(panel.query(InterruptedMessage))
        assert panel.query_one(WelcomeWidget).display
        text_area = screen.query_one(InputBar).query_one("#chat-input", TextArea)
        assert text_area.has_focus
        assert text_area.cursor_location == (0, len("Edit this prompt"))


@pytest.mark.parametrize("entry", ["interrupted", "message"])
async def test_loading_rollback_entry_restores_button_and_shows_running_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
) -> None:
    app = make_chrys_app(tmp_path / "state", engine=_FirstTurnEngine(tmp_path / "mutations"))
    async with app.run_test(size=(80, 32)) as pilot:
        screen = app.screen
        assert isinstance(screen, MainScreen)
        panel = screen.query_one(ChatPanel)
        await panel.add_user_message("Edit this prompt")
        panel.apply_turn_started(1)
        await panel.add_interrupted("Execution interrupted by user")
        if entry == "interrupted":
            button = panel.query_one(InterruptedMessage).query_one(".status-rollback-btn", Button)
        else:
            button = panel.query_one(UserMessage).query_one(MessageRollbackButton)
        notify = Mock()
        monkeypatch.setattr(screen, "notify", notify)
        screen._state.run.agent_loading = True

        await click_when_settled(pilot, button)

        await wait_for(
            lambda: notify.call_count == 1 and not button.disabled,
            pilot=pilot,
            description="loading rollback gives the running notice and restores its source button",
        )
        notify.assert_called_once_with(
            "Cannot discard while the agent is running.",
            title="Discard",
            severity="warning",
            timeout=3,
            markup=False,
        )
        assert app.screen is screen


async def test_main_screen_continue_hides_both_interrupted_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = EventBus()
    app = make_chrys_app(tmp_path, engine=_FirstTurnEngine(tmp_path / "mutations"), event_bus=bus)
    retries: list[UserRetry] = []

    async def resumed(event: UserRetry) -> None:
        retries.append(event)

    await bus.subscribe(UserRetry, resumed)
    async with app.run_test(size=(80, 28)) as pilot:
        screen = app.screen
        assert isinstance(screen, MainScreen)
        monkeypatch.setattr(screen, "_model_unconfigured", lambda: False)
        panel = screen.query_one(ChatPanel)
        await panel.add_user_message("Original prompt")
        await panel.add_interrupted()
        composer = screen.query_one(InputBar)
        composer.retry_mode = True
        status = panel.query_one(InterruptedMessage)
        assert status.query_one(".status-rollback-btn", Button).display
        await click_when_settled(pilot, status.query_one(".status-action-btn", Button))
        await wait_for(
            lambda: len(retries) == 1 and not status.query_one(".status-action-row").display,
            pilot=pilot,
            description="Continue consumes the whole interruption action row",
        )
        assert retries[0].text == ""
        assert not status.query_one(".status-action-row").display


class _ChinesePanelApp(ChatPanelApp):
    def __init__(self) -> None:
        controller = LocaleController(Settings(locale="zh-Hans"))
        super().__init__(localization=controller.localizer)
        self.locale_controller = controller


@pytest.mark.parametrize("source", ["user", "session_closed"])
async def test_restored_interruption_has_translated_actions_on_one_row_at_60_columns(source: str) -> None:
    messages = [
        Message("user", [Content.from_text("请修改这条消息")]).to_dict(),
        Message(
            "assistant",
            [Content.from_text("Execution interrupted")],
            additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED, "_interrupted_by": source},
        ).to_dict(),
        _marker(1).to_dict(),
    ]
    async with _ChinesePanelApp().run_test(size=(60, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(messages, rollback_targets={0})
        await panel.wait_replay_complete()
        await pilot.pause()
        user = panel.query_one(UserMessage)
        status = panel.query_one(InterruptedMessage)
        rollback = status.query_one(".status-rollback-btn", Button)
        resume = status.query_one(".status-action-btn", Button)
        assert str(rollback.label) == "撤回本轮"
        assert str(resume.label) == "继续"
        assert rich_plain(user.query_one(MessageRollbackButton).render()) == "↺ 撤回本轮及之后"
        assert rich_plain(user.query_one(MessageCopyButton).render()) == "复制"
        assert rollback.region.y == resume.region.y
        assert rollback.region.right <= 60
        assert user.query_one(MessageRollbackButton).region.height == 1


def _marker(turn: int) -> Message:
    return Message(
        "assistant", [], additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.TURN, "_turn": turn}
    )


async def test_long_archived_tool_history_keeps_tail_first_bounded_mounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    count = 48
    archived: list[Message] = []
    for turn in range(1, count + 1):
        call_id = f"read-{turn}"
        archived.extend(
            (
                Message("user", [Content.from_text(f"Question {turn}")]),
                Message(
                    "assistant",
                    [Content.from_function_call(call_id=call_id, name="read_file", arguments={"path": "a.txt"})],
                ),
                Message("tool", [Content.from_function_result(call_id=call_id, result="text")]),
                Message("assistant", [Content.from_text(f"Answer {turn}\n\nMore details")]),
                _marker(turn),
            )
        )
    block = CompressedBlock(compressed_context_id="long-archive", messages=archived)
    summary = Message(
        "assistant",
        [Content.from_text("Archive summary")],
        additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY, "_block_id": "long-archive"},
    )
    async with ChatPanelApp().run_test(size=(80, 24)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        original_mount = panel.mount_replay_batch
        release = asyncio.Event()
        prepend_started = asyncio.Event()
        batch_sizes: list[int] = []

        async def mount_batch(batch: list[Widget], *, before: Widget | None) -> None:
            batch_sizes.append(len(batch))
            await original_mount(batch, before=before)
            if before is not None and not prepend_started.is_set():
                prepend_started.set()
                await release.wait()

        monkeypatch.setattr(panel, "mount_replay_batch", mount_batch)
        try:
            await panel.replay_history(
                [summary.to_dict()],
                compressed_blocks={"long-archive": block},
                rollback_targets=set(range(count)),
            )
            assert panel.replay_in_progress
            assert any(isinstance(widget, ToolGroup) for widget in panel.pending_transcript_widgets())
            assert len(list(panel.query(UserMessage))) < count
            await wait_for(prepend_started.is_set, pilot=pilot, description="archived history prepend held")
            assert panel.pending_transcript_widgets()
        finally:
            release.set()
            await panel.wait_replay_complete()
        assert all(0 < size <= REPLAY_MOUNT_BATCH_SIZE for size in batch_sizes)
        assert len(batch_sizes) > 1
        assert len(list(panel.query(UserMessage))) == count
        groups = list(panel.query(ToolGroup))
        assert len(groups) == count
        assert all(group.has_class("--compressed") for group in groups)
        assert all(message.query_one(MessageCopyButton).display for message in panel.query(UserMessage))
        assert panel.pending_transcript_widgets() == []
