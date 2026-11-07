# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ChatPanel: run-state reactives, replay markers, TOC/turn index, inline status actions, stream finalization, chrome, and prepare_retry."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.events import Click
from textual.widgets import Button, Static, TextArea

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.i18n import LocaleController, LocaleSwitchStatus
from chrys.app.tui.widgets.chat.compaction_card import CompactionCard
from chrys.app.tui.widgets.chat.context_fold import ContextFoldWidget
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    ErrorMessage,
    InterruptedMessage,
    SystemMessage,
    UserMessage,
    _UserHeader,
    format_message_created_at,
)
from chrys.app.tui.widgets.chat.panel import ChatPanel, _ScrollToBottomButton
from chrys.app.tui.widgets.chat.renderers.ask_user import AskUserToolCall
from chrys.app.tui.widgets.chat.session_json import SessionJsonPanel
from chrys.app.tui.widgets.chat.tool_call import (
    ToolCall,
    ToolGroup,
)
from chrys.foundation.config.settings import Settings
from chrys.foundation.i18n import Localizer
from chrys.foundation.models.history_markers import ANSWER_SEGMENT_KEY, HistoryMarkerKind
from chrys.foundation.tool_kinds import (
    KIND_ASK_USER,
)
from chrys.foundation.trajectory_timing import TRAJECTORY_TIMING_KEY
from chrys.kernel import Content
from chrys.service.session.message_metadata import MESSAGE_CREATED_AT_KEY
from tests.support.tui_helpers import (
    ChatPanelApp,
    WidgetApp,
    chat_content_children,
)
from tests.support.waiting import wait_for


def test_chat_panel_agent_running_reactive_resets_final_response_gate() -> None:
    panel = ChatPanel()
    panel._final_response_started = True

    panel.agent_running = True

    assert panel._agent_running is True
    assert panel._final_response_started is False


async def test_chat_panel_clear_resets_defensive_agent_running_cache() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.agent_running = True

        await panel.clear()

        assert panel.agent_running is True
        assert panel._agent_running is False


async def test_chat_panel_clear_preserves_runtime_metadata_for_replay_and_live_headers() -> None:
    raw_messages = [
        {
            "role": "assistant",
            "contents": [
                {
                    "type": "function_call",
                    "call_id": "ask-1",
                    "name": "ask_user",
                    "arguments": {"question": "Continue?"},
                }
            ],
        },
        {
            "role": "tool",
            "contents": [
                {
                    "type": "function_result",
                    "call_id": "ask-1",
                    "result": "User response: yes",
                }
            ],
        },
    ]

    async with ChatPanelApp().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.set_profile("Code Agent")
        panel.set_tool_kinds({"ask_user": KIND_ASK_USER})
        await panel.add_user_message("before clear")

        await panel.clear()
        await panel.replay_history(raw_messages)
        await pilot.pause()

        group = panel.query_one(ToolGroup)
        assert group._tool_records["ask-1#0"].tool_kind == KIND_ASK_USER

        group.collapsed = False
        await wait_for(
            lambda: group._content_mounted and group._tools,
            pilot=pilot,
            description="group._content_mounted and group._tools",
        )

        assert isinstance(next(iter(group._tools.values())), AskUserToolCall)

        await panel.add_agent_message("live response")
        await pilot.pause()

        assert panel.query_one(AgentMessage)._copy_label() == "Code Agent"


async def test_chat_panel_replay_uses_created_at_metadata() -> None:
    created_at = datetime.now(UTC).replace(second=0, microsecond=0)
    expected = format_message_created_at(created_at)

    raw_messages = [
        {
            "role": "user",
            "contents": [{"type": "text", "text": "hello"}],
            "additional_properties": {
                MESSAGE_CREATED_AT_KEY: created_at.isoformat(),
                TRAJECTORY_TIMING_KEY: {
                    "started_at": created_at.isoformat(),
                    "finished_at": created_at.isoformat(),
                    "duration_ms": 0,
                },
            },
        },
        {
            "role": "assistant",
            "contents": [{"type": "text", "text": "hi"}],
            "additional_properties": {
                MESSAGE_CREATED_AT_KEY: (created_at + timedelta(milliseconds=2345)).isoformat(),
                TRAJECTORY_TIMING_KEY: {
                    "started_at": created_at.isoformat(),
                    "finished_at": (created_at + timedelta(milliseconds=2345)).isoformat(),
                    "duration_ms": 2345,
                },
            },
        },
        {
            "role": "assistant",
            "contents": [{"type": "text", "text": ""}],
            "additional_properties": {HistoryMarkerKind.KEY: HistoryMarkerKind.TURN},
        },
    ]

    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.set_profile("Code Agent")
        await panel.replay_history(raw_messages, initial_profile="Code Agent")
        await pilot.pause()

        user = panel.query_one(UserMessage)
        agent = panel.query_one(AgentMessage)
        assert user._ts == expected
        assert "(0ms)" not in str(user.query_one(_UserHeader).content)
        assert agent._ts == expected
        assert agent._duration_ms == 2345
        assert agent._header_text().plain == f"▾ Code Agent {expected} (2s)"


async def test_chat_panel_replay_adds_action_only_for_trailing_interruption() -> None:
    raw_messages = [
        {
            "role": "assistant",
            "contents": [{"type": "text", "text": "Execution interrupted"}],
            "additional_properties": {
                HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED,
                "_interrupted_by": "user",
            },
        },
        {
            "role": "user",
            "contents": [{"type": "text", "text": "later prompt"}],
        },
        {
            "role": "assistant",
            "contents": [{"type": "text", "text": "later answer"}],
        },
    ]

    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(raw_messages)
        await pilot.pause()

        assert not list(panel.query(Button))


async def test_chat_panel_replay_adds_action_for_current_interruption() -> None:
    raw_messages = [
        {
            "role": "assistant",
            "contents": [{"type": "text", "text": "Execution interrupted"}],
            "additional_properties": {
                HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED,
                "_interrupted_by": "error",
            },
        }
    ]

    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(raw_messages)
        await pilot.pause()

        action = panel.query_one(InterruptedMessage)
        assert str(action.query_one(Button).label) == "Retry"


async def test_chat_panel_replay_localizes_recognized_status_and_frame() -> None:
    raw_messages = [
        {
            "role": "assistant",
            "contents": [{"type": "text", "text": "stale English literal"}],
            "additional_properties": {
                HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED,
                HistoryMarkerKind.STATUS_CODE_KEY: HistoryMarkerKind.STATUS_EXECUTION_INTERRUPTED,
                "_interrupted_by": "user",
            },
        }
    ]

    async with ChatPanelApp(Localizer("zh-Hans")).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(raw_messages)
        await pilot.pause()

        interruption = panel.query_one(InterruptedMessage)
        rendered = interruption._render_text().plain
        assert rendered == "⚠ 已中断\n用户中断：执行已中断"  # noqa: RUF001
        assert "Interrupted" not in rendered
        assert "by user" not in rendered
        assert "stale English literal" not in rendered
        assert str(interruption.query_one(Button).label) == "继续"


async def test_chat_panel_replay_unknown_status_keeps_literal() -> None:
    raw_messages = [
        {
            "role": "assistant",
            "contents": [{"type": "text", "text": "future marker literal"}],
            "additional_properties": {
                HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED,
                HistoryMarkerKind.STATUS_CODE_KEY: "future_status",
                "_interrupted_by": "",
            },
        }
    ]

    async with ChatPanelApp(Localizer("zh-Hans")).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(raw_messages)
        await pilot.pause()

        assert panel.query_one(InterruptedMessage)._render_text().plain == "⚠ 已中断\nfuture marker literal"


async def test_chat_panel_replay_sanitizes_legacy_marker_controls() -> None:
    raw_literal = "first\x1b[31mred\nsecond"
    raw_messages = [
        {
            "role": "assistant",
            "contents": [{"type": "text", "text": raw_literal}],
            "additional_properties": {
                HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED,
                "_interrupted_by": "",
            },
        }
    ]

    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(raw_messages)
        await pilot.pause()

        rendered = panel.query_one(InterruptedMessage)._render_text().plain
        assert rendered == "⚠ Interrupted\nfirst�[31mred\nsecond"
        assert "\x1b" not in rendered
        assert raw_messages[0]["contents"][0]["text"] == raw_literal


@pytest.mark.parametrize(
    ("structured_ids", "expected_text"),
    [
        pytest.param({"_invocation_ids": ["first", "second"]}, "正在等待 2 个子智能体", id="structured-count"),
        pytest.param({}, "Awaiting 99 sub-agent(s)", id="literal-without-ids"),
    ],
)
async def test_chat_panel_replay_awaiting_status(structured_ids: dict[str, object], expected_text: str) -> None:
    """Structured invocation ids localize the count; without them the literal text is kept verbatim."""
    raw_messages = [
        {
            "role": "assistant",
            "contents": [{"type": "text", "text": "Awaiting 99 sub-agent(s)"}],
            "additional_properties": {
                HistoryMarkerKind.KEY: HistoryMarkerKind.AWAITING_SUB_AGENTS,
                HistoryMarkerKind.STATUS_CODE_KEY: HistoryMarkerKind.STATUS_AWAITING_SUB_AGENTS,
                **structured_ids,
            },
        }
    ]

    async with ChatPanelApp(Localizer("zh-Hans")).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(raw_messages)
        await pilot.pause()

        assert panel.query_one(AgentMessage).text == expected_text
        assert raw_messages[0]["contents"][0]["text"] == "Awaiting 99 sub-agent(s)"


async def test_chat_panel_replay_downgraded_first_injection_makes_following_user_injection() -> None:
    raw_messages = [
        {
            "role": "user",
            "contents": [{"type": "text", "text": "first"}],
            "additional_properties": {"_injected": True},
        },
        {
            "role": "user",
            "contents": [{"type": "text", "text": "second"}],
        },
    ]

    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(raw_messages)
        await pilot.pause()

        user_messages = list(panel.query(UserMessage))
        assert [(message.id, message.is_injection, message._text) for message in user_messages] == [
            ("turn-1", False, "first"),
            ("inj-1", True, "second"),
        ]


async def test_chat_panel_timestamps_only_final_agent_response() -> None:
    first_at = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
    final_at = datetime(2026, 5, 15, 12, 1, 0, tzinfo=UTC)
    expected = format_message_created_at(final_at)

    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.set_profile("Code Agent")
        await panel.add_user_message("hello", created_at=first_at)
        await panel.add_answer_delta("seg-1", "working")
        await panel.add_tool_start("read-1", "read_file", "filesystem.read", "path=/tmp/file")
        await panel.add_tool_result("read-1", "read_file", "tool output")
        await panel.add_answer_delta("seg-2", "done")
        await pilot.pause()

        agent_messages = list(panel.query(AgentMessage))
        assert [message._ts for message in agent_messages] == ["", ""]

        await panel.add_agent_message("done", created_at=final_at)
        await pilot.pause()

        agent_messages = list(panel.query(AgentMessage))
        assert [message.text for message in agent_messages] == ["working", "done"]
        assert [message._is_intermediate for message in agent_messages] == [True, False]
        assert [message._ts for message in agent_messages] == ["", expected]


async def test_chat_panel_toggle_fold_all_leaves_agent_messages() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        tool_group = ToolGroup()
        agent_message = AgentMessage("Done.")

        await panel.mount(tool_group)
        await panel.mount(agent_message)
        await pilot.pause()

        tool_group.collapsed = False
        agent_message.collapsed = False

        assert panel.toggle_fold_all() is True
        assert tool_group.collapsed is True
        assert agent_message.collapsed is False

        assert panel.toggle_fold_all() is False
        assert tool_group.collapsed is False
        assert agent_message.collapsed is False


async def test_chat_panel_toggle_fold_all_affects_replayed_tool_groups() -> None:
    raw_messages = [
        {"role": "user", "contents": [{"type": "text", "text": "run"}]},
        {
            "role": "assistant",
            "contents": [{"type": "function_call", "call_id": "call-1", "name": "zsh", "arguments": "{}"}],
        },
        {"role": "tool", "contents": [{"type": "function_result", "call_id": "call-1", "result": "ok"}]},
    ]

    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(raw_messages)
        await pilot.pause()

        group = panel.query_one(ToolGroup)
        assert group.collapsed is True

        assert panel.toggle_fold_all() is False
        assert group.collapsed is False

        assert panel.toggle_fold_all() is True
        assert group.collapsed is True


async def test_chat_panel_working_dir_click_posts_compatible_message() -> None:
    messages: list[ChatPanel.WorkingDirClicked] = []

    class WorkingDirApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

        def on_chat_panel_working_dir_clicked(self, event: ChatPanel.WorkingDirClicked) -> None:
            event.stop()
            messages.append(event)

    class _Click:
        def __init__(self, widget: ChatPanel, screen_y: int) -> None:
            self.widget = widget
            self.screen_y = screen_y

        def stop(self) -> None:
            return

        def prevent_default(self) -> None:
            return

    async with WorkingDirApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.border_subtitle = Text("/tmp/project")
        await pilot.pause()

        panel.on_click(_Click(panel, panel.region.y + panel.region.height - 1))  # type: ignore[arg-type]
        await pilot.pause()

    assert len(messages) == 1
    assert isinstance(messages[0], ChatPanel.WorkingDirClicked)


async def test_chat_panel_replay_omits_intermediate_agent_timestamps() -> None:
    created_at = datetime(2026, 5, 15, 12, 1, 0, tzinfo=UTC)
    expected = format_message_created_at(created_at)

    raw_messages = [
        {
            "role": "assistant",
            "contents": [
                {"type": "text", "text": "I'll inspect the files."},
                {"type": "function_call", "call_id": "call_1", "name": "zsh", "arguments": "{}"},
            ],
            "additional_properties": {MESSAGE_CREATED_AT_KEY: created_at.isoformat()},
        },
        {
            "role": "assistant",
            "contents": [{"type": "text", "text": "Done."}],
            "additional_properties": {MESSAGE_CREATED_AT_KEY: created_at.isoformat()},
        },
    ]

    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.set_profile("Code Agent")
        await panel.replay_history(raw_messages, initial_profile="Code Agent")
        await pilot.pause()

        agent_messages = list(panel.query(AgentMessage))
        assert [message._is_intermediate for message in agent_messages] == [True, False]
        assert [message._ts for message in agent_messages] == ["", expected]


async def test_chat_panel_replay_settles_an_answer_segment_a_later_one_followed() -> None:
    """Live, a later segment settles the earlier one as intermediate text; replay shows them the same way."""
    created_at = datetime(2026, 5, 15, 12, 1, 0, tzinfo=UTC)
    contents = [
        Content.from_text("A first look.", additional_properties={ANSWER_SEGMENT_KEY: "seg-1"}),
        Content.from_text_reasoning(text="thinking again"),
        Content.from_text("The answer.", additional_properties={ANSWER_SEGMENT_KEY: "seg-2"}),
    ]
    raw_messages = [
        {"role": "user", "contents": [{"type": "text", "text": "ask"}]},
        {
            "role": "assistant",
            "contents": [content.to_dict() for content in contents],
            "additional_properties": {MESSAGE_CREATED_AT_KEY: created_at.isoformat()},
        },
    ]

    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history(raw_messages)

        agent_messages = list(panel.query(AgentMessage))
        assert [message.text for message in agent_messages] == ["A first look.", "The answer."]
        assert [message._is_intermediate for message in agent_messages] == [True, False]
        assert [message._ts for message in agent_messages] == ["", format_message_created_at(created_at)]


@pytest.mark.parametrize(
    ("turn_range", "expected_title"),
    [
        ((3, 5), "  ═══ Compressed (Turn 3-5) ═══"),
        ((7, 7), "  ═══ Compressed (Turn 7) ═══"),
        ((0, 0), "  ═══ Compressed (5,000 messages) ═══"),
    ],
)
def test_context_fold_title_includes_turn_range(
    turn_range: tuple[int, int],
    expected_title: str,
) -> None:
    widget = ContextFoldWidget("ctx_abc", "summary", 5000, turn_range)

    assert widget.render().plain.splitlines()[0] == expected_title


async def test_chat_panel_add_messages() -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("hello")
        await cp.add_agent_message("hi there")
        await cp.add_tool_start("c1", "read_file", "path=/foo")
        await cp.add_tool_result("c1", "read_file", "file contents...", 89)
        await cp.add_error("something went wrong")
        await cp.add_system("session started")
        await cp.add_context_fold("rw_abc", "summary of folded messages", 5000, (3, 5))
        # The compaction is retry output, so it removes the stale error even
        # when an informational system row was appended in between.
        assert len(chat_content_children(cp)) == 5
        assert not list(cp.query(ErrorMessage))
        assert len(list(cp.query(SystemMessage))) == 1
        fold = cp.query_one(ContextFoldWidget)
        assert fold.render().plain.splitlines()[0] == "  ═══ Compressed (Turn 3-5) ═══"


async def test_chat_panel_removes_failed_prompt_from_toc() -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("bad prompt")
        cp.apply_turn_started(1)
        await cp.add_error("failed before output")

        await cp.add_user_message("replacement prompt")
        await cp.apply_fresh_turn_cleanup(retired_previous_turn=True, removed_item_ids=())
        cp.apply_turn_started(1)
        await pilot.pause()

        items = cp.toc_items
        assert [(item.turn_id, item.summary, item.turn_index) for item in items] == [
            ("turn-2", "replacement prompt", 1)
        ]
        assert [message._text for message in cp.query(UserMessage)] == ["replacement prompt"]


@pytest.mark.parametrize("status_type", [ErrorMessage, InterruptedMessage])
@pytest.mark.parametrize("output", ["none", "assistant", "tool"])
async def test_fresh_submit_cleans_failed_turn_across_context_notices(status_type, output: str) -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("older prompt")
        cp.apply_turn_started(1)
        await cp.add_agent_message("older response")
        await cp.add_user_message("failed prompt")
        cp.apply_turn_started(2)
        if output == "assistant":
            await cp.add_answer_delta("seg-partial", "partial response")
        elif output == "tool":
            await cp.add_tool_start("read-1", "read_file", "filesystem.read", "path=/tmp/file")
            await cp.add_tool_result("read-1", "read_file", "tool output")
        if status_type is ErrorMessage:
            await cp.add_error("failed")
        else:
            await cp.add_interrupted()
        await cp.add_system("Workspace changed")
        await cp.add_system("Agent profile switched")
        notices = list(cp.query(SystemMessage))
        responses = list(cp.query(AgentMessage))
        tools = list(cp.query(ToolGroup))

        await cp.add_user_message("replacement prompt")
        await cp.apply_fresh_turn_cleanup(retired_previous_turn=output == "none", removed_item_ids=())
        cp.apply_turn_started(2 if output == "none" else 3)

        assert not list(cp.query(status_type))
        expected_users = ["older prompt", *(["failed prompt"] if output != "none" else []), "replacement prompt"]
        assert [message._text for message in cp.query(UserMessage)] == expected_users
        assert [item.summary for item in cp.toc_items] == expected_users
        assert [item.turn_index for item in cp.toc_items] == ([1, 2] if output == "none" else [1, 2, 3])
        assert list(cp.query(SystemMessage)) == notices
        assert list(cp.query(AgentMessage)) == responses
        assert list(cp.query(ToolGroup)) == tools


async def test_fresh_cleanup_keeps_prompt_separated_from_error_by_notice() -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("keep this prompt")
        await cp.add_system("Agent configuration changed")
        await cp.add_error("configuration failed", action_label=None)
        await cp.add_system("Workspace changed")

        await cp.add_user_message("new prompt")

        assert not list(cp.query(ErrorMessage))
        assert [message._text for message in cp.query(UserMessage)] == ["keep this prompt", "new prompt"]
        assert len(list(cp.query(SystemMessage))) == 2


async def test_fresh_cleanup_does_not_cross_a_pending_retry_note() -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("original prompt")
        await cp.add_error("failed")
        await cp.add_user_message("pending retry note", is_injection=True)
        await cp.add_system("Workspace changed")

        await cp.add_user_message("new prompt")

        assert len(list(cp.query(ErrorMessage))) == 1
        assert [message._text for message in cp.query(UserMessage)] == [
            "original prompt",
            "pending retry note",
            "new prompt",
        ]


async def test_status_lookup_and_fresh_cleanup_use_chrome_infrastructure_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import create_autospec

    from textual.widget import Widget

    from chrys.app.tui.widgets.chat.chrome import ChatPanelChrome

    infrastructure = Static("additional chrome")
    original = ChatPanelChrome.is_infrastructure

    def is_infrastructure(chrome: ChatPanelChrome, widget: Widget) -> bool:
        return widget is infrastructure or original(chrome, widget)

    monkeypatch.setattr(ChatPanelChrome, "is_infrastructure", create_autospec(original, side_effect=is_infrastructure))
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("failed prompt")
        cp.apply_turn_started(1)
        await cp.add_error("failed")
        await cp.mount(infrastructure)
        button = cp.query_one(ErrorMessage).query_one(Button)
        cp.set_trailing_status_action_disabled(True)
        assert button.disabled
        cp.hide_trailing_status_action()
        assert button.parent is not None and not button.parent.display

        await cp.add_user_message("replacement prompt")
        await cp.apply_fresh_turn_cleanup(retired_previous_turn=True, removed_item_ids=())
        cp.apply_turn_started(1)

        assert not list(cp.query(ErrorMessage))
        assert [message._text for message in cp.query(UserMessage)] == ["replacement prompt"]
        assert infrastructure.is_mounted


async def test_chat_panel_marks_compressed_toc_by_backend_turn_index_after_failed_prompt_cleanup() -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("bad prompt")
        cp.apply_turn_started(1)
        await cp.add_error("failed before output")
        await cp.add_user_message("first kept")
        await cp.apply_fresh_turn_cleanup(retired_previous_turn=True, removed_item_ids=())
        cp.apply_turn_started(1)
        await cp.add_user_message("second kept")
        cp.apply_turn_started(2)
        await cp.add_user_message("second kept detail", is_injection=True)
        await cp.add_user_message("third kept")
        cp.apply_turn_started(3)

        changed = cp.mark_turn_range_compressed((2, 2))
        await pilot.pause()

        items = cp.toc_items
        assert changed is True
        assert [item.turn_index for item in items] == [1, 2, 3]
        assert [item.compressed for item in items] == [False, True, False]
        assert len(items[1].children) == 1
        assert items[1].children[0].compressed is True


async def test_chat_panel_failed_turn_with_output_keeps_its_own_backend_turn_index() -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("failed with partial output")
        cp.apply_turn_started(1)
        await cp.add_agent_message("partial output")
        await cp.add_error("failed after output")
        await cp.add_user_message("continuation")
        await cp.apply_fresh_turn_cleanup(retired_previous_turn=False, removed_item_ids=())
        cp.apply_turn_started(2)
        await cp.add_user_message("next fresh turn")
        cp.apply_turn_started(3)

        changed = cp.mark_turn_range_compressed((1, 1))
        await pilot.pause()

        items = cp.toc_items
        assert changed is True
        assert [item.turn_index for item in items] == [1, 2, 3]
        assert [item.compressed for item in items] == [True, False, False]


async def test_chat_panel_replay_assigns_turn_index_from_trailing_markers() -> None:
    messages = [
        {"role": "user", "contents": [{"type": "text", "text": "first"}], "additional_properties": {}},
        {"role": "assistant", "contents": [{"type": "text", "text": "one"}], "additional_properties": {}},
        {
            "role": "assistant",
            "contents": [""],
            "additional_properties": {HistoryMarkerKind.KEY: HistoryMarkerKind.TURN, "_turn_id": "turn_1", "_turn": 1},
        },
        {"role": "user", "contents": [{"type": "text", "text": "second"}], "additional_properties": {}},
        {"role": "assistant", "contents": [{"type": "text", "text": "two"}], "additional_properties": {}},
        {
            "role": "assistant",
            "contents": [""],
            "additional_properties": {HistoryMarkerKind.KEY: HistoryMarkerKind.TURN, "_turn_id": "turn_2", "_turn": 2},
        },
    ]

    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(messages)

        changed = cp.mark_turn_range_compressed((2, 2))
        await pilot.pause()

        items = cp.toc_items
        assert changed is True
        assert [item.turn_index for item in items] == [1, 2]
        assert [item.compressed for item in items] == [False, True]


async def test_chat_panel_adds_inline_retry_action_for_error() -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_error("something went wrong")
        await pilot.pause()

        action = cp.query_one(ErrorMessage)
        button = action.query_one(Button)
        assert str(button.label) == "Retry"


async def test_chat_panel_can_render_error_without_inline_action() -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_error("not retryable", action_label=None)
        await pilot.pause()

        assert cp.query_one(ErrorMessage)
        assert not list(cp.query(Button))


async def test_chat_panel_adds_inline_continue_action_for_interrupt() -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_interrupted()
        await pilot.pause()

        action = cp.query_one(InterruptedMessage)
        button = action.query_one(Button)
        assert str(button.label) == "Continue"


def test_interrupted_message_default_copy_is_byte_identical() -> None:
    assert InterruptedMessage("Execution interrupted", "user")._render_text().plain == (
        "⚠ Interrupted\nExecution interrupted by user"
    )
    assert InterruptedMessage("Execution failed", "error")._render_text().plain == "✗ Error\nExecution failed"
    assert InterruptedMessage("Checkpoint restored", "")._render_text().plain == ("⚠ Interrupted\nCheckpoint restored")


async def test_chat_panel_live_interruption_uses_localized_frame() -> None:
    async with ChatPanelApp(Localizer("zh-Hans")).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_interrupted("操作\x1b停止", "user")
        await pilot.pause()

        interruption = panel.query_one(InterruptedMessage)
        assert interruption._render_text().plain == "⚠ 已中断\n用户中断：操作�停止"  # noqa: RUF001
        assert str(interruption.query_one(Button).label) == "继续"

        await panel.add_interrupted("失败详情", "error")
        await pilot.pause()

        interruption = panel.query_one(InterruptedMessage)
        assert interruption._render_text().plain == "✗ 错误\n失败详情"
        assert str(interruption.query_one(Button).label) == "重试"


async def test_chat_panel_live_interruption_defaults_to_the_localized_shared_reason() -> None:
    async with ChatPanelApp(Localizer("zh-Hans")).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_interrupted()
        await pilot.pause()

        interruption = panel.query_one(InterruptedMessage)
        assert interruption._render_text().plain == "⚠ 已中断\n用户中断：执行已中断"  # noqa: RUF001
        assert str(interruption.query_one(".status-action-btn", Button).label) == "继续"


async def test_chat_panel_live_interruption_keeps_the_english_default_copy() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_interrupted()
        await pilot.pause()

        interruption = panel.query_one(InterruptedMessage)
        assert interruption._render_text().plain == "⚠ Interrupted\nExecution interrupted by user"


async def test_chat_panel_live_error_card_localizes_its_default_action() -> None:
    async with ChatPanelApp(Localizer("zh-Hans")).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_error("出错了")
        await pilot.pause()

        button = panel.query_one(ErrorMessage).query_one(Button)
        assert str(button.label) == "重试"

        await panel.add_error("出错了", action_label="自定义")
        await pilot.pause()

        assert str(panel.query_one(ErrorMessage).query_one(Button).label) == "自定义"


async def test_chat_panel_removes_inline_status_action_with_status() -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_error("something went wrong")
        await cp.add_agent_message("recovered")
        await pilot.pause()

        assert not list(cp.query(ErrorMessage))
        assert not list(cp.query(Button))


async def test_chat_panel_context_fold_replaces_trailing_error_on_retry() -> None:
    """A retry-time fold is run output, so it supersedes the prior error."""
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_error("first attempt failed")

        await cp.add_context_fold("ctx-retry", "durable summary", 3, (1, 1))
        await pilot.pause()

        assert not list(cp.query(ErrorMessage))
        fold = cp.query_one(ContextFoldWidget)
        assert "Compressed (Turn 1)" in fold.render().plain


async def test_chat_panel_context_fold_breaks_stale_stream_cursor() -> None:
    """Recovered output mounts after a carried fold, not into failed partial text."""
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_answer_delta("seg-partial", "failed partial")
        await cp.add_error("stream failed")

        await cp.add_context_fold("ctx-retry", "durable summary", 3, (1, 1))
        await cp.add_answer_delta("seg-retry", "retry recovered")
        await cp.add_agent_message("retry recovered")
        await pilot.pause()

        transcript = chat_content_children(cp)
        agent_messages = list(cp.query(AgentMessage))
        fold = cp.query_one(ContextFoldWidget)
        assert [message.text for message in agent_messages] == ["failed partial", "retry recovered"]
        assert [message._is_final for message in agent_messages] == [True, True]
        assert [message.has_class("--streaming") for message in agent_messages] == [False, False]
        assert transcript.index(agent_messages[0]) < transcript.index(fold) < transcript.index(agent_messages[1])


async def _break_stream_with_error(cp: ChatPanel) -> None:
    await cp.add_error("stream failed")


async def _break_stream_with_retry_cleanup(cp: ChatPanel) -> None:
    await cp.prepare_retry()
    await cp.add_retry("Stream stalled", 1, 5, 0)


@pytest.mark.parametrize(
    "break_stream",
    [
        pytest.param(_break_stream_with_error, id="error-then-compaction"),
        pytest.param(_break_stream_with_retry_cleanup, id="retry-cleanup-then-compaction"),
    ],
)
async def test_chat_panel_compaction_after_partial_stream_leaves_no_stale_cursor(
    break_stream: Callable[[ChatPanel], Awaitable[None]],
) -> None:
    """Recovered output mounts after Phase 4 with no orphan stream cursor.

    Covers both a plain stream error and the real RetryAttempt cleanup sequence
    ahead of the compaction card.
    """
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_answer_delta("seg-partial", "failed partial")
        await break_stream(cp)

        await cp.add_compaction_start("comp-retry")
        cp.complete_compaction("comp-retry", outcome="ok", last_words="durable note")
        await cp.add_answer_delta("seg-retry", "retry recovered")
        await cp.add_agent_message("retry recovered")
        await pilot.pause()

        transcript = chat_content_children(cp)
        agent_messages = list(cp.query(AgentMessage))
        card = cp.query_one(CompactionCard)
        assert [message.text for message in agent_messages] == ["failed partial", "retry recovered"]
        assert [message._is_final for message in agent_messages] == [True, True]
        assert [message.has_class("--streaming") for message in agent_messages] == [False, False]
        assert transcript.index(agent_messages[0]) < transcript.index(card) < transcript.index(agent_messages[1])


async def test_chat_panel_tool_group_open_finalizes_failed_partial_stream() -> None:
    """A retry starting with a tool call cannot reuse failed streamed prose."""
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_answer_delta("seg-partial", "failed partial")
        await cp.add_error("stream failed")

        await cp.add_tool_start("call-retry", "read_file", "filesystem.read", "path=/tmp/retry")
        await cp.add_tool_result("call-retry", "read_file", "recovered tool output")
        await cp.add_answer_delta("seg-retry", "retry recovered")
        await cp.add_agent_message("retry recovered")
        await pilot.pause()

        transcript = chat_content_children(cp)
        agent_messages = list(cp.query(AgentMessage))
        group = cp.query_one(ToolGroup)
        assert [message.text for message in agent_messages] == ["failed partial", "retry recovered"]
        assert [message._is_final for message in agent_messages] == [True, True]
        assert [message.has_class("--streaming") for message in agent_messages] == [False, False]
        assert transcript.index(agent_messages[0]) < transcript.index(group) < transcript.index(agent_messages[1])


async def test_chat_panel_new_user_finalizes_failed_partial_stream() -> None:
    """Submitting a new prompt after failure leaves no orphan stream cursor."""
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("first prompt")
        await cp.add_answer_delta("seg-partial", "failed partial")
        await cp.add_error("stream failed")

        await cp.add_user_message("replacement prompt")
        await cp.add_answer_delta("seg-replacement", "replacement response")
        await cp.add_agent_message("replacement response")
        await pilot.pause()

        transcript = chat_content_children(cp)
        agent_messages = list(cp.query(AgentMessage))
        replacement = list(cp.query(UserMessage))[-1]
        assert [message.text for message in agent_messages] == ["failed partial", "replacement response"]
        assert [message._is_final for message in agent_messages] == [True, True]
        assert [message.has_class("--streaming") for message in agent_messages] == [False, False]
        assert transcript.index(agent_messages[0]) < transcript.index(replacement) < transcript.index(agent_messages[1])


async def test_chat_panel_each_answer_segment_is_its_own_message() -> None:
    """A failed segment ends as it was shown; text the turn went on past, by a tool call or another segment, turns intermediate."""
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_answer_delta("seg-1", "prior ")
        await cp.add_answer_delta("seg-1", "partial")
        cp.finish_answer_segment("seg-1", completed=False)

        await cp.add_answer_delta("seg-2", "Checking the tools.")
        await cp.add_tool_start("read-1", "read_file", "filesystem.read", "path=/tmp/file")
        await cp.add_tool_result("read-1", "read_file", "tool output")
        # Text before a call that opens no card, such as one naming an unknown tool.
        await cp.add_answer_delta("seg-3", "Trying another tool.")
        cp.finish_answer_segment("seg-3", completed=True)
        await cp.add_answer_delta("seg-4", "finished")
        await cp.add_agent_message("finished")
        await pilot.pause()

        agent_messages = list(cp.query(AgentMessage))
        assert [message.text for message in agent_messages] == [
            "prior partial",
            "Checking the tools.",
            "Trying another tool.",
            "finished",
        ]
        assert [message._is_intermediate for message in agent_messages] == [False, True, True, False]
        assert [message._is_final for message in agent_messages] == [True, True, True, True]
        assert [message.has_class("--streaming") for message in agent_messages] == [False, False, False, False]


async def test_chat_panel_final_message_does_not_remount_an_answer_a_tool_call_settled() -> None:
    """The final response's text already streamed: after a tool call settled its message, it mounts no second copy."""
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await cp.add_answer_delta("seg-1", "Let me check.")
        cp.finish_answer_segment("seg-1", completed=True)
        await cp.add_tool_start("read-1", "read_file", "filesystem.read", "path=/tmp/file")
        await cp.add_tool_result("read-1", "read_file", "tool output")
        # The tool call settled the streamed message: no current message is left to finalize.
        assert cp._current_agent_msg is None

        await cp.add_agent_message("Let me check.")

        assert [message.text for message in cp.query(AgentMessage)] == ["Let me check."]


@pytest.mark.parametrize("status_type", [ErrorMessage, InterruptedMessage])
async def test_chat_panel_withdraws_the_answer_segments_a_bare_continue_asks_for_again(status_type) -> None:
    """A bare Continue or Retry drops the failed pass's trailing answer from history, so the transcript drops it too."""
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await cp.add_answer_delta("seg-1", "Checking the tools.")
        await cp.add_tool_start("read-1", "read_file", "filesystem.read", "path=/tmp/file")
        await cp.add_tool_result("read-1", "read_file", "tool output")
        await cp.add_answer_delta("seg-2", "Here is the first ")
        cp.finish_answer_segment("seg-2", completed=True)
        await cp.add_answer_delta("seg-3", "half of the answer")
        cp.finish_answer_segment("seg-3", completed=False)
        if status_type is ErrorMessage:
            await cp.add_error("failed")
        else:
            await cp.add_interrupted()
        status = cp.query_one(status_type)

        await cp.withdraw_answer(("seg-2", "seg-3"))

        assert [message.text for message in cp.query(AgentMessage)] == ["Checking the tools."]
        assert cp.query_one(ToolGroup).is_mounted
        assert cp.query_one(status_type) is status


async def test_a_start_reusing_a_finished_call_id_leaves_that_card_showing_the_call_its_result_answered() -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await cp.add_tool_start("c1", "alpha", "", '{"x": 1}', args={"x": 1})
        await cp.add_tool_result("c1", "alpha", "alpha result")
        await cp.add_tool_start("c1", "beta", "", '{"y": 2}', args={"y": 2})

        [card] = cp.query(ToolCall)
        assert (card.tool_name, card.args, card.status) == ("alpha", {"x": 1}, "complete")


async def test_chat_panel_keeps_every_answer_a_withdrawal_does_not_name() -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await cp.add_answer_delta("seg-1", "An earlier answer")
        cp.finish_answer_segment("seg-1", completed=True)
        await cp.add_answer_delta("seg-2", "The dropped answer")
        cp.finish_answer_segment("seg-2", completed=False)

        await cp.withdraw_answer(("seg-2", "another-turn-segment"))

        assert [message.text for message in cp.query(AgentMessage)] == ["An earlier answer"]


async def test_chat_panel_replays_each_answer_segment_as_its_own_message_so_a_withdrawal_takes_only_its_own() -> None:
    """Text on either side of a hosted result is two segments: a bare Continue drops the trailing one only."""
    call = Content.from_hosted_tool_call(
        "search-1", tool_name="web_search", status="running", hosted_family="search", hosted_provider="openai"
    )
    result = Content.from_hosted_tool_result(
        "search-1", tool_name="web_search", result="found", status="completed", hosted_family="search"
    )
    contents = [
        call,
        Content.from_text("Searching", additional_properties={ANSWER_SEGMENT_KEY: "intro"}),
        # An empty fragment shows nothing, so it splits no segment.
        Content.from_text(""),
        Content.from_text(" the web.", additional_properties={ANSWER_SEGMENT_KEY: "intro"}),
        result,
        Content.from_text("Found Chrys.", additional_properties={ANSWER_SEGMENT_KEY: "answer"}),
    ]
    raw_messages = [
        {"role": "user", "contents": [{"type": "text", "text": "search"}]},
        {"role": "assistant", "contents": [content.to_dict() for content in contents]},
    ]

    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.replay_history(raw_messages)
        assert [message.text for message in cp.query(AgentMessage)] == ["Searching the web.", "Found Chrys."]

        await cp.withdraw_answer(("answer",))

        assert [message.text for message in cp.query(AgentMessage)] == ["Searching the web."]


async def test_chat_panel_retry_output_removes_error_across_system_message() -> None:
    """Profile/workspace indicators do not pin a retried error in history."""
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_error("first attempt failed")
        await cp.add_system("Agent profile switched: Code → QA", key="profile-switch-1")

        await cp.add_agent_message("retry recovered")
        await pilot.pause()

        assert not list(cp.query(ErrorMessage))
        assert len(list(cp.query(SystemMessage))) == 1
        assert cp.query_one(AgentMessage).text == "retry recovered"


async def test_chat_panel_removes_status_before_retry_note_on_agent_recovery() -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("original prompt")
        await cp.add_error("something went wrong")
        await cp.add_user_message("extra context", is_injection=True)
        await cp.add_agent_message("recovered")
        await pilot.pause()

        assert not list(cp.query(ErrorMessage))
        assert len(list(cp.query(UserMessage))) == 2
        assert len(cp.get_agent_responses()) == 1


async def test_chat_panel_transcript_labels_agent_messages_with_their_profile() -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("hi")
        await cp.add_agent_message("unnamed")
        cp.profile_name = "QA [bold]"
        await cp.add_user_message("again")
        await cp.add_agent_message("named")
        await pilot.pause()

        assert cp.get_agent_responses() == [("Agent", "unnamed"), ("QA [bold]", "named")]
        assert cp.get_all_messages() == [("You", "hi"), ("Agent", "unnamed"), ("You", "again"), ("QA [bold]", "named")]


@pytest.mark.parametrize("status_type", [ErrorMessage, InterruptedMessage])
@pytest.mark.parametrize("suffix", ["none", "system", "note", "mixed"])
async def test_chat_panel_retry_actions_and_removal_find_the_same_status(status_type, suffix: str) -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("original prompt")
        if status_type is ErrorMessage:
            await cp.add_error("failed")
        else:
            await cp.add_interrupted()
        if suffix in {"system", "mixed"}:
            await cp.add_system("Workspace changed")
        if suffix in {"note", "mixed"}:
            await cp.add_user_message("additional context", is_injection=True)
        if suffix == "mixed":
            await cp.add_system("Agent profile switched")

        action = cp.query_one(status_type)
        row = action.query_one(".status-action-row")
        button = action.query_one(Button)
        system_messages = list(cp.query(SystemMessage))
        user_messages = list(cp.query(UserMessage))
        assert row.display and not button.disabled

        cp.set_trailing_status_action_disabled(True)
        assert row.display and button.disabled
        cp.set_trailing_status_action_disabled(False)
        assert row.display and not button.disabled
        cp.hide_trailing_status_action()
        assert not row.display
        assert cp.query_one(status_type) is action
        await cp.remove_trailing_status()
        assert not list(cp.query(status_type))
        assert list(cp.query(SystemMessage)) == system_messages
        assert list(cp.query(UserMessage)) == user_messages


async def test_chat_panel_status_actions_do_not_cross_a_new_user_turn() -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("first turn")
        await cp.add_error("old failure")
        await cp.add_system("Workspace changed")
        # Build an older transcript boundary without invoking fresh-submit cleanup.
        await cp.mount_transcript_widget(UserMessage("new turn"))
        await cp.add_system("Agent profile switched")
        old_status = cp.query_one(ErrorMessage)

        cp.set_trailing_status_action_disabled(True)
        cp.hide_trailing_status_action()
        await cp.remove_trailing_status()

        assert cp.query_one(ErrorMessage) is old_status
        assert old_status.query_one(".status-action-row").display
        assert not old_status.query_one(Button).disabled


async def test_chat_panel_border_title_click_posts_title_clicked(monkeypatch: pytest.MonkeyPatch) -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        session_id = "12345678-1234-1234-1234-123456789abc"
        posted: list[object] = []
        original_post = cp.post_message

        def capture(message: object) -> bool:
            posted.append(message)
            return original_post(message)

        monkeypatch.setattr(cp, "post_message", capture)

        cp.set_session_id(session_id)
        cp.on_click(
            Click(
                cp,
                x=0,
                y=0,
                delta_x=0,
                delta_y=0,
                button=1,
                shift=False,
                meta=False,
                ctrl=False,
                screen_x=cp.region.x,
                screen_y=cp.region.y,
            )
        )
        await pilot.pause()

        assert any(isinstance(message, ChatPanel.TitleClicked) for message in posted)


async def test_chat_panel_passive_click_preserves_existing_input_focus() -> None:
    class PassiveClickApp(App):
        def compose(self) -> ComposeResult:
            yield TextArea(id="input")
            yield ChatPanel()

    async with PassiveClickApp().run_test() as pilot:
        text_area = pilot.app.query_one("#input", TextArea)
        text_area.focus()
        await pilot.pause()
        assert pilot.app.focused is text_area

        await pilot.click(ChatPanel, offset=(1, 1))
        await pilot.pause()

        assert pilot.app.focused is text_area


def test_chat_panel_border_title_shows_session_title() -> None:
    cp = ChatPanel()

    cp.set_session_id("12345678-1234-1234-1234-123456789abc")
    assert str(cp.border_title) == "Session: 123456781234"

    cp.set_session_title("Fix login bug")
    assert str(cp.border_title) == "Session: 123456781234 \u00b7 Fix login bug"

    cp.set_session_title("")
    assert str(cp.border_title) == "Session: 123456781234"


def test_chat_panel_border_title_truncates_long_session_title() -> None:
    cp = ChatPanel()

    cp.set_session_id("12345678-1234-1234-1234-123456789abc")
    cp.set_session_title("t" * 200)

    title = str(cp.border_title)
    assert title.startswith("Session: 123456781234 \u00b7 ")
    assert title.endswith("\u2026")
    assert len(title) <= len("Session: 123456781234 \u00b7 ") + 48


async def test_chat_and_session_json_chrome_relocalize_without_touching_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = LocaleController(Settings(locale="en"))
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    chat: ChatPanel | None = None
    session_json: SessionJsonPanel | None = None

    class LocalizedChatChromeApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel(locale_controller=controller)
            yield SessionJsonPanel(locale_controller=controller)

    async with LocalizedChatChromeApp().run_test() as pilot:
        chat = pilot.app.query_one(ChatPanel)
        session_json = pilot.app.query_one(SessionJsonPanel)
        session_id = "12345678-1234-1234-1234-123456789abc"
        chat.set_session_id(session_id)
        chat.set_session_title("Fix login bug")
        monkeypatch.setattr(session_json, "_resolve_session_path", lambda _session_id: None)
        session_json.load_session(session_id)
        transcript = Static(Text("literal transcript [red]payload"))
        await chat.mount(transcript)
        await pilot.pause()
        children_before = tuple(chat.children)
        button = chat.query_one(_ScrollToBottomButton)
        visibility_before = button.styles.visibility
        walk_calls = 0
        original_walk = chat.walk_children

        def record_walk(*args: object, **kwargs: object):
            nonlocal walk_calls
            walk_calls += 1
            return original_walk(*args, **kwargs)

        monkeypatch.setattr(chat, "walk_children", record_walk)
        assert controller.switch_locale("zh-Hans").status is LocaleSwitchStatus.EFFECTIVE_CHANGED

        assert walk_calls == 0
        assert tuple(chat.children) == children_before
        assert chat.children[-1] is button
        assert transcript.render().plain == "literal transcript [red]payload"
        assert str(chat.border_title) == "会话：123456781234 · Fix login bug"  # noqa: RUF001
        assert button.render().plain == "滚动到底部 ↓"
        assert button.tooltip is not None
        assert button.tooltip.plain == "跳转到对话底部（Ctrl+End）"  # noqa: RUF001
        assert button.styles.visibility == visibility_before
        assert str(session_json.border_title) == "会话 JSON：123456781234"  # noqa: RUF001

        chat.set_session_title("")
        assert str(chat.border_title) == "会话：123456781234"  # noqa: RUF001

    assert chat is not None and session_json is not None
    assert chat not in controller._surfaces
    assert session_json not in controller._surfaces


async def test_chat_scroll_chrome_reserves_width_for_locale_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = LocaleController(Settings(locale="zh-Hans"))
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)

    async with WidgetApp(lambda: ChatPanel(locale_controller=controller)).run_test() as pilot:
        button = pilot.app.query_one(_ScrollToBottomButton)
        assert button.render().plain == "滚动到底部 ↓"

        assert controller.switch_locale("en").status is LocaleSwitchStatus.EFFECTIVE_CHANGED

        assert button.render().plain == "Scroll to bottom ↓"
        assert button.styles.min_width is not None
        assert button.styles.min_width.value >= button.render().cell_length + 2


def test_chat_panel_border_subtitle_includes_git_branch() -> None:
    cp = ChatPanel()

    cp.set_workspace_cwd("/tmp/project")
    cp.set_workspace_branch("feat/abc")

    assert str(cp.border_subtitle) == "/tmp/project (feat/abc)"


def test_chat_panel_border_subtitle_omits_empty_git_branch() -> None:
    cp = ChatPanel()

    cp.set_workspace_cwd("/tmp/project")
    cp.set_workspace_branch("main")
    cp.set_workspace_branch("")

    assert str(cp.border_subtitle) == "/tmp/project"


async def test_prepare_retry_cancels_running_tools_and_opens_fresh_group() -> None:
    """Regression: after a main-agent stream retry, new tool calls must
    attach to a NEW tool group under the re-emitted assistant block —
    not the stale group from the failed attempt.

    Without ``prepare_retry()`` the panel would carry ``_current_tool_group``
    across the retry boundary, so the next ``add_tool_start`` after the
    re-emitted intermediate text would merge into the old group, mounting
    new sub-agent cards under the OLD assistant message.
    """
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("count the lines")

        # Attempt 1: intermediate text + two parallel sub-agent tool calls.
        await cp.add_answer_delta("seg-1", "I'll launch two sub-agents in parallel.")
        await cp.add_tool_start("call_A", "general_agent", "sub_agent")
        await cp.add_tool_start("call_B", "general_agent", "sub_agent")
        old_group = cp._current_tool_group
        assert old_group is not None
        assert len(old_group._tools) == 2
        assert all(tc.status == "running" for tc in old_group._tools.values())

        # Stream stall fires → event handler calls prepare_retry() then add_retry().
        await cp.prepare_retry()
        await cp.add_retry("Stream stalled", 1, 5, 3)

        # All previously-running tools should be marked cancelled so the
        # TUI shows a terminal state for the stale invocations.
        assert all(tc.status != "running" for tc in old_group._tools.values())
        # Group state is cleared so the next ToolCallStart opens a fresh group.
        assert cp._current_tool_group is None
        assert cp._current_agent_msg is None

        # Attempt 2: the re-emitted intermediate text mounts a new assistant
        # widget, and new tool calls open a fresh group beneath it.
        await cp.add_answer_delta("seg-2", "I'll launch two sub-agents in parallel.")
        await cp.add_tool_start("call_C", "general_agent", "sub_agent")
        await cp.add_tool_start("call_D", "general_agent", "sub_agent")
        new_group = cp._current_tool_group
        assert new_group is not None
        assert new_group is not old_group, "retry-spawned tools must open a fresh group"
        assert len(new_group._tools) == 2
        assert "call_C" in new_group._tools
        assert "call_D" in new_group._tools
        # And the OLD group still holds only the original calls — the new
        # ones did NOT leak into it.
        assert "call_C" not in old_group._tools
        assert "call_D" not in old_group._tools


async def test_prepare_retry_preserves_completed_tool_results() -> None:
    """Tools that finished BEFORE the retry fires (e.g. one of two parallel
    sub-agents that completed while the other was still running) should
    retain their final results — only the still-running ones transition
    to the cancelled state."""
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("go")
        await cp.add_answer_delta("seg-1", "starting")
        await cp.add_tool_start("done_call", "general_agent", "sub_agent")
        await cp.add_tool_start("running_call", "general_agent", "sub_agent")

        # First sub-agent finishes, second still running.
        await cp.add_tool_result("done_call", "general_agent", "all good", 1200)

        group = cp._current_tool_group
        assert group is not None
        assert group._tools["done_call"].status != "running"
        assert group._tools["running_call"].status == "running"

        await cp.prepare_retry()

        # Completed result preserved, running one is now terminal.
        assert group._tools["done_call"].status != "running"
        assert group._tools["running_call"].status != "running"
