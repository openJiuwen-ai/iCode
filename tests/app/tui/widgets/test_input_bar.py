# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the TUI input bar."""

from __future__ import annotations

import asyncio

import pytest
from rich.cells import cell_len
from rich.text import Text
from textual.app import App, ComposeResult
from textual.content import Content
from textual.selection import SELECT_ALL
from textual.widgets import Button, TextArea

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.i18n import LocaleController, LocaleSwitchStatus
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.screens.main.state import MainScreenState
from chrys.app.tui.screens.main.view_adapter import MainScreenViewAdapter
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome import input_bar as input_bar_module
from chrys.app.tui.widgets.chrome.input_bar import (
    INPUT_RETRY,
    INPUT_SEND,
    InputBar,
    _ChatTextArea,
    _is_file_trigger_boundary,
)
from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList
from chrys.app.tui.widgets.loading import ChrysLoadingIndicator
from chrys.app.tui.widgets.sidebar.context import ContextPanel, ContextUsageState
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.i18n import MessageRef
from tests.support.waiting import wait_for


class _InputBarApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.suggestion_directions: list[str] = []
        self.file_trigger_count = 0
        self.model_trigger_count = 0
        self.shell_request_count = 0
        self.editor_request_count = 0
        self.editor_event_order: list[str] = []
        self.submitted: list[str] = []

    def compose(self) -> ComposeResult:
        yield InputBar()

    def on_input_bar_user_submitted(self, event: InputBar.UserSubmitted) -> None:
        self.submitted.append(event.text)

    def on_input_bar_suggestion_navigate(self, event: InputBar.SuggestionNavigate) -> None:
        self.suggestion_directions.append(event.direction)

    def on_input_bar_file_triggered(self, _event: InputBar.FileTriggered) -> None:
        self.file_trigger_count += 1

    def on_input_bar_model_triggered(self, _event: InputBar.ModelTriggered) -> None:
        self.model_trigger_count += 1

    def on_input_bar_shell_mode_requested(self, _event: InputBar.ShellModeRequested) -> None:
        self.shell_request_count += 1

    def on_input_bar_suggestion_dismiss(self, _event: InputBar.SuggestionDismiss) -> None:
        self.editor_event_order.append("dismiss")

    def on_input_bar_editor_requested(self, _event: InputBar.EditorRequested) -> None:
        self.editor_request_count += 1
        self.editor_event_order.append("editor")


class _LocalizedInputBarApp(App[None]):
    def __init__(self, controller: LocaleController) -> None:
        super().__init__()
        self._controller = controller

    def compose(self) -> ComposeResult:
        yield InputBar(locale_controller=self._controller)


@pytest.mark.parametrize("submit", ["button", "enter"])
async def test_pending_retry_disables_submission_without_consuming_draft(submit: str) -> None:
    retries: list[str] = []

    class RetryApp(_InputBarApp):
        def on_input_bar_retry_requested(self, event: InputBar.RetryRequested) -> None:
            retries.append(event.text)

    async with RetryApp().run_test() as pilot:
        input_bar = pilot.app.query_one(InputBar)
        input_bar.retry_mode = True
        input_bar.value = "next draft"
        input_bar.retry_pending = True
        button = input_bar.query_one("#send-btn", Button)
        assert button.disabled

        def attempt() -> None:
            if submit == "button":
                input_bar.on_button_pressed(Button.Pressed(button))
            else:
                input_bar.on__chat_text_area_submitted(_ChatTextArea.Submitted(input_bar.value))

        attempt()
        await pilot.pause()
        assert input_bar.value == "next draft"
        assert not retries
        assert not pilot.app.submitted
        input_bar.retry_pending = False
        assert not button.disabled
        attempt()
        await pilot.pause()
        assert retries == ["next draft"]
        assert input_bar.value == ""


class _MainScreenInputBindingApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.main_screen = MainScreen(EventBus(), engine_provider=None)

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self.main_screen)


async def test_pending_retry_visibly_disables_new_session_but_keeps_editor_available() -> None:
    new_sessions = []

    class RetryApp(_InputBarApp):
        def on_input_bar_new_session_requested(self, event: InputBar.NewSessionRequested) -> None:
            new_sessions.append(event)

    async with RetryApp().run_test() as pilot:
        input_bar = pilot.app.query_one(InputBar)
        input_bar.has_messages = True
        input_bar.retry_pending = True
        new_button = input_bar.query_one("#new-btn", Button)
        editor_button = input_bar.query_one("#editor-btn", Button)
        assert new_button.visible and new_button.disabled
        assert not editor_button.disabled
        input_bar.on_button_pressed(Button.Pressed(new_button))
        input_bar.on_button_pressed(Button.Pressed(editor_button))
        await wait_for(lambda: pilot.app.editor_request_count == 1)
        assert not new_sessions

        input_bar.retry_pending = False
        assert new_button.visible and not new_button.disabled
        input_bar.on_button_pressed(Button.Pressed(new_button))
        await wait_for(lambda: len(new_sessions) == 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_loading", [False, True], ids=["idle", "loading"])
@pytest.mark.parametrize("retry_pending", [False, True], ids=["no-submit", "retry-admission"])
async def test_ctrl_r_opens_prompt_history_and_enter_restores_original_prompt(
    monkeypatch: pytest.MonkeyPatch,
    agent_loading: bool,
    retry_pending: bool,
) -> None:
    markup_shaped_prompt = "failure [type=missing, input_value={}, input_type=dict])"

    async def load_prompt_history(_input_bar: InputBar, *, max_entries: int) -> list[str]:
        assert max_entries == 100
        return ["older", "middle\nwith newline", markup_shaped_prompt]

    monkeypatch.setattr(InputBar, "load_prompt_history", load_prompt_history)
    app = _MainScreenInputBindingApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.focus_input()
        await wait_for(
            lambda: input_bar.query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )
        app.main_screen._set_agent_loading(agent_loading)
        input_bar.retry_pending = retry_pending
        await pilot.pause()

        await pilot.press("ctrl+r")
        await pilot.pause()

        suggestions = app.main_screen.query_one(SuggestionList)
        assert suggestions.mode == "history"
        assert suggestions._values == [markup_shaped_prompt, "middle\nwith newline", "older"]
        assert [content.plain for content in suggestions._contents] == [
            markup_shaped_prompt,
            "middle ↵ with newline",
            "older",
        ]
        assert app.focused is input_bar.query_one("#chat-input", _ChatTextArea)

        await pilot.press("enter")
        await pilot.pause()

        assert input_bar.value == markup_shaped_prompt
        assert suggestions.mode == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["shell", "session-json"])
async def test_prompt_history_loading_is_dismissed_before_main_view_transition(
    monkeypatch: pytest.MonkeyPatch,
    transition: str,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def load_prompt_history(_input_bar: InputBar, *, max_entries: int) -> list[str]:
        assert max_entries == 100
        started.set()
        await release.wait()
        return ["stale prompt"]

    monkeypatch.setattr(InputBar, "load_prompt_history", load_prompt_history)
    app = _MainScreenInputBindingApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.focus_input()
        await wait_for(
            lambda: input_bar.query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )
        await pilot.press("ctrl+r")
        await started.wait()

        suggestions = app.main_screen.query_one(SuggestionList)
        loading = suggestions.query_one(ChrysLoadingIndicator)
        assert suggestions.mode == "history"
        assert suggestions.is_loading is True
        assert loading.display is True

        try:
            if transition == "shell":
                # "!" is query text inside the history search, so the shell request
                # arrives the way any other entry point would deliver it.
                input_bar.post_message(InputBar.ShellModeRequested())
            else:
                await pilot.press("f12")
            await pilot.pause()

            assert suggestions.mode == ""
            assert suggestions.is_visible is False
            assert suggestions.is_loading is False
            if transition == "shell":
                assert app.main_screen._state.shell.active is True
            else:
                assert app.main_screen._dashboard_visible() is True
        finally:
            release.set()

        await pilot.pause()
        assert suggestions.mode == ""
        assert suggestions.is_visible is False


@pytest.mark.asyncio
async def test_prompt_history_search_takes_bang_as_query_until_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    async def load_prompt_history(_input_bar: InputBar, *, max_entries: int) -> list[str]:
        assert max_entries == 100
        return ["git status", "!ls -la"]

    monkeypatch.setattr(InputBar, "load_prompt_history", load_prompt_history)
    app = _MainScreenInputBindingApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.focus_input()
        await wait_for(
            lambda: input_bar.query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )
        suggestions = app.main_screen.query_one(SuggestionList)

        await pilot.press("ctrl+r")
        await wait_for(
            lambda: suggestions.mode == "history" and not suggestions.is_loading,
            pilot=pilot,
            description="prompt history rows",
        )
        await pilot.press("!")
        await wait_for(
            lambda: suggestions._values == ["!ls -la"],
            pilot=pilot,
            description="history filtered by the bang query",
        )
        assert input_bar.value == "!"
        assert app.main_screen._state.shell.active is False

        await pilot.press("escape")
        await pilot.pause()
        assert suggestions.is_visible is False
        input_bar.value = ""
        await pilot.press("!")
        await wait_for(
            lambda: app.main_screen._state.shell.active,
            pilot=pilot,
            description="shell mode after leaving history search",
        )


@pytest.mark.asyncio
async def test_prompt_history_ignored_while_another_suggestion_list_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    loads: list[int] = []

    async def load_prompt_history(_input_bar: InputBar, *, max_entries: int) -> list[str]:
        loads.append(max_entries)
        return ["/resume earlier session"]

    monkeypatch.setattr(InputBar, "load_prompt_history", load_prompt_history)
    app = _MainScreenInputBindingApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.focus_input()
        await wait_for(
            lambda: input_bar.query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )
        suggestions = app.main_screen.query_one(SuggestionList)

        await pilot.press("/")
        await wait_for(lambda: suggestions.mode == "commands", pilot=pilot, description="command list")

        await pilot.press("ctrl+r")
        await pilot.pause()
        assert suggestions.mode == "commands"
        assert input_bar.value == "/"
        assert loads == []

        await pilot.press("escape")
        await pilot.pause()
        assert suggestions.is_visible is False
        await pilot.press("ctrl+r")
        await wait_for(
            lambda: suggestions.mode == "history" and suggestions._values == ["/resume earlier session"],
            pilot=pilot,
            description="prompt history after closing the command list",
        )
        assert loads == [100]


@pytest.mark.asyncio
async def test_prompt_history_search_swaps_input_placeholder_until_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def load_prompt_history(_input_bar: InputBar, *, max_entries: int) -> list[str]:
        assert max_entries == 100
        return ["older prompt"]

    monkeypatch.setattr(InputBar, "load_prompt_history", load_prompt_history)
    app = _MainScreenInputBindingApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.main_screen.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        input_bar.focus_input()
        await wait_for(
            lambda: text_area.has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )
        default_placeholder = text_area.placeholder
        default_right_hint = text_area._right_placeholder
        assert default_right_hint

        await pilot.press("ctrl+r")
        await wait_for(
            lambda: app.main_screen.query_one(SuggestionList).mode == "history",
            pilot=pilot,
            description="prompt history popup",
        )
        placeholder = text_area.placeholder
        assert isinstance(placeholder, Content)
        assert placeholder.plain == "Search prompt history   Enter  Select  Esc  Close"
        assert text_area._right_placeholder == ""

        await pilot.press("escape")
        await pilot.pause()
        assert text_area.placeholder == default_placeholder
        assert text_area._right_placeholder == default_right_hint


@pytest.mark.asyncio
async def test_prompt_history_selection_resets_up_down_browsing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def load_prompt_history(_input_bar: InputBar, *, max_entries: int) -> list[str]:
        assert max_entries == 100
        return ["selected prompt"]

    monkeypatch.setattr(InputBar, "load_prompt_history", load_prompt_history)
    app = _MainScreenInputBindingApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.main_screen.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        input_bar.add_to_history("selected")
        input_bar.value = "earlier draft"
        input_bar.focus_input()
        await wait_for(
            lambda: input_bar.query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )

        await pilot.press("up")
        assert input_bar.value == "selected"
        assert text_area._history.index == 0

        await pilot.press("ctrl+r")
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()

        assert input_bar.value == "selected prompt"
        assert text_area._history.index == -1
        assert text_area._history_browsing is False

        await pilot.press("down")
        assert input_bar.value == "selected prompt"


@pytest.mark.asyncio
async def test_main_screen_binds_one_way_child_flags() -> None:
    app = _MainScreenInputBindingApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        chat_panel = app.main_screen.query_one(ChatPanel)
        context_panel = app.main_screen.query_one(ContextPanel)
        input_bar = app.main_screen.query_one(InputBar)

        app.main_screen._set_agent_running(True)
        app.main_screen._set_agent_loading(True)
        app.main_screen._set_has_messages(True)
        app.main_screen.chat_profile_name = "Code"
        app.main_screen.chat_session_id = "12345678-1234-1234-1234-123456789abc"
        app.main_screen.chat_session_title = "Fix login bug"
        app.main_screen.chat_workspace_cwd = "/repo/chrys"
        app.main_screen.context_usage_state = ContextUsageState.with_window(
            used_tokens=57_700,
            max_context_tokens=200_000,
            total_session_tokens=236_100,
        )
        await pilot.pause()

        assert chat_panel.agent_running is True
        assert chat_panel._profile_name == "Code"
        assert chat_panel.session_id == "12345678-1234-1234-1234-123456789abc"
        assert str(chat_panel.border_subtitle) == "/repo/chrys"
        assert str(chat_panel.border_title) == "Session: 123456781234 \u00b7 Fix login bug"
        assert context_panel._current_used == 57_700
        assert context_panel._current_max == 200_000
        assert context_panel._total_session_tokens == 236_100
        assert input_bar.agent_running is True
        assert input_bar.agent_loading is True
        assert input_bar.has_messages is True

        app.main_screen._set_agent_running(False)
        app.main_screen._set_agent_loading(False)
        app.main_screen._set_has_messages(False)
        await pilot.pause()

        assert chat_panel.agent_running is False
        assert input_bar.agent_running is False
        assert input_bar.agent_loading is False
        assert input_bar.has_messages is False


@pytest.mark.asyncio
async def test_main_screen_reactive_sources_apply_existing_side_effects() -> None:
    app = _MainScreenInputBindingApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.main_screen.query_one(InputBar)
        app.main_screen._suggestions.file_cache = {"stale": object()}
        app.main_screen._live_diff.call_paths["call"] = "src/app.py"
        app.main_screen._live_diff.file_mutations["src/app.py"] = object()

        app.main_screen.agent_running_state = True
        await pilot.pause()

        assert app.main_screen._state.run.agent_running is True
        assert app.main_screen._live_diff.call_paths == {}
        assert app.main_screen._live_diff.file_mutations == {}

        input_bar.lock_with_text()
        app.main_screen.agent_running_state = False
        app.main_screen.agent_loading_state = False
        app.main_screen.has_messages_state = True
        await pilot.pause()

        assert app.main_screen._state.run.agent_running is False
        assert app.main_screen._state.run.agent_loading is False
        assert app.main_screen._state.run.has_messages is True
        assert app.main_screen._suggestions.file_cache is None
        assert input_bar.locked is False


@pytest.mark.parametrize(
    "text",
    ["@", "some text @", "some text\t@", "some text \uff20", "你好@", "你好\uff20", "こんにちは@", "한글@"],
)
def test_file_trigger_boundary_accepts_start_whitespace_and_cjk(text: str) -> None:
    assert _is_file_trigger_boundary(text, len(text) - 1) is True


@pytest.mark.parametrize("text", ["some text@", "user@web.com", "user\uff20web.com", "path/to@file", "abc_@"])
def test_file_trigger_boundary_rejects_ascii_word_boundaries(text: str) -> None:
    at_pos = next(i for i, char in enumerate(text) if char in ("@", "\uff20"))
    assert _is_file_trigger_boundary(text, at_pos) is False


@pytest.mark.asyncio
async def test_input_bar_requests_shell_mode_without_owning_state() -> None:
    app = _InputBarApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        input_bar.query_one("#chat-input", TextArea).focus()
        await wait_for(
            lambda: input_bar.query_one("#chat-input", TextArea).has_focus,
            pilot=pilot,
            description="control focus before interaction",
        )

        await pilot.press("!")
        await pilot.pause()

        assert app.shell_request_count == 1
        assert input_bar.shell_mode is False


@pytest.mark.asyncio
async def test_prompt_history_search_types_trigger_characters_as_query_text() -> None:
    app = _InputBarApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        input_bar.set_suggestions_active(True, mode="history")
        input_bar.query_one("#chat-input", TextArea).focus()
        await wait_for(
            lambda: input_bar.query_one("#chat-input", TextArea).has_focus,
            pilot=pilot,
            description="control focus before interaction",
        )

        await pilot.press("!", "$", "@")
        await pilot.pause()

        assert input_bar.value == "!$@"
        assert app.shell_request_count == 0
        assert app.file_trigger_count == 0
        assert app.model_trigger_count == 0

        # Leaving history search restores the trigger characters.
        input_bar.set_suggestions_active(False)
        input_bar.value = ""
        await pilot.press("!")
        await pilot.pause()
        assert app.shell_request_count == 1


@pytest.mark.parametrize(
    ("prefix", "expected_count"),
    [
        ("", 1),
        ("some text ", 1),
        ("你好", 1),
        ("user", 0),
    ],
)
@pytest.mark.asyncio
async def test_input_bar_posts_file_trigger_only_at_file_boundaries(prefix: str, expected_count: int) -> None:
    app = _InputBarApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        input_bar.value = prefix
        input_bar.query_one("#chat-input", TextArea).focus()
        await wait_for(
            lambda: input_bar.query_one("#chat-input", TextArea).has_focus,
            pilot=pilot,
            description="control focus before interaction",
        )
        await pilot.press("@")
        await pilot.pause()

    assert app.file_trigger_count == expected_count


@pytest.mark.asyncio
async def test_input_bar_dollar_character_triggers_model_suggestions() -> None:
    app = _InputBarApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        input_bar.query_one("#chat-input", TextArea).focus()
        await wait_for(
            lambda: input_bar.query_one("#chat-input", TextArea).has_focus,
            pilot=pilot,
            description="control focus before interaction",
        )

        await pilot.press("$")
        await pilot.pause()

        assert app.model_trigger_count == 1
        assert app.editor_request_count == 0
        assert input_bar.value == "$"


@pytest.mark.parametrize("draft", ["", "existing draft"])
@pytest.mark.asyncio
async def test_input_bar_ctrl_o_opens_editor_without_changing_draft(draft: str) -> None:
    app = _InputBarApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        input_bar.value = draft
        input_bar.query_one("#chat-input", TextArea).focus()
        await wait_for(
            lambda: input_bar.query_one("#chat-input", TextArea).has_focus,
            pilot=pilot,
            description="control focus before interaction",
        )

        await pilot.press("ctrl+o")
        await pilot.pause()

        assert app.editor_request_count == 1
        assert input_bar.value == draft


@pytest.mark.asyncio
async def test_input_bar_prompt_button_opens_editor_without_changing_draft() -> None:
    app = _InputBarApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        input_bar.value = "existing draft"
        editor_button = input_bar.query_one("#editor-btn", Button)
        text_area = input_bar.query_one("#chat-input", TextArea)

        assert editor_button.label.plain == ">"
        assert editor_button.disabled is False

        await pilot.click("#editor-btn")
        await pilot.pause()

        assert app.editor_request_count == 1
        assert input_bar.value == "existing draft"
        assert text_area.has_focus


@pytest.mark.asyncio
async def test_input_bar_ctrl_e_keeps_textual_line_end_behavior() -> None:
    app = _InputBarApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", TextArea)
        input_bar.value = "draft"
        text_area.move_cursor((0, 1))
        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="control focus before interaction")

        await pilot.press("ctrl+e")
        await pilot.pause()

        assert text_area.cursor_location == (0, 5)
        assert app.editor_request_count == 0


@pytest.mark.asyncio
async def test_input_bar_ctrl_o_remains_available_while_agent_running() -> None:
    app = _InputBarApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", TextArea)
        input_bar.agent_running = True
        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()

        await pilot.press("ctrl+o")
        await pilot.pause()

        assert app.editor_request_count == 1
        assert text_area.placeholder.plain == "Inject a message  /  Commands  @  Files"


@pytest.mark.parametrize("guard", ["locked", "shell_mode"])
@pytest.mark.asyncio
async def test_input_bar_guards_editor_forwarder(guard: str) -> None:
    app = _InputBarApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        if guard == "locked":
            input_bar.locked = True
        else:
            input_bar.shell_mode = True
        await pilot.pause()

        assert input_bar.query_one("#editor-btn", Button).disabled is True

        text_area.post_message(_ChatTextArea.EditorRequested())
        await pilot.pause()

        assert app.editor_request_count == 0


@pytest.mark.asyncio
async def test_input_bar_dismisses_active_suggestions_before_editor_request() -> None:
    app = _InputBarApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        input_bar.set_suggestions_active(True, mode="files")
        input_bar.query_one("#chat-input", TextArea).focus()
        await wait_for(
            lambda: input_bar.query_one("#chat-input", TextArea).has_focus,
            pilot=pilot,
            description="control focus before interaction",
        )

        await pilot.press("ctrl+o")
        await pilot.pause()

        assert app.editor_event_order == ["dismiss", "editor"]


@pytest.mark.asyncio
async def test_input_bar_editor_placeholders_are_exact_and_ordered() -> None:
    app = _InputBarApp()

    async with app.run_test(size=(160, 15)) as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", TextArea)

        assert text_area.placeholder.plain == (
            "Type a message (Ctrl+O for Editor)   #  Agents  $  Models  /  Commands  @  Files  !  Shell"
        )
        newline_hint = "Ctrl+J for newline"
        rendered_line = text_area.render_line(0)
        assert rendered_line.text.rstrip().endswith(newline_hint)
        assert rendered_line.text.endswith(" ")
        assert rendered_line.cell_length == text_area.content_size.width

        input_bar.value = "draft"
        await pilot.pause()
        assert newline_hint not in text_area.render_line(0).text
        input_bar.value = ""
        await pilot.pause()

        input_bar.agent_running = True
        await pilot.pause()
        assert text_area.placeholder.plain == "Inject a message  /  Commands  @  Files"


@pytest.mark.asyncio
async def test_input_bar_relocalizes_semantic_state_and_cell_width_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = LocaleController(Settings(locale="en"))
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    input_bar: InputBar | None = None

    async with _LocalizedInputBarApp(controller).run_test(size=(120, 15)) as pilot:
        input_bar = pilot.app.query_one(InputBar)
        send = input_bar.query_one("#send-btn", Button)
        new = input_bar.query_one("#new-btn", Button)
        text_area = input_bar.query_one("#chat-input", TextArea)
        assert input_bar in controller._surfaces
        assert str(send.label) == "Send"
        assert str(new.label) == "New"

        label_writes: list[tuple[str, str]] = []
        original_set_label = input_bar._set_btn_label

        def record_label(label: str, *, button_id: str = "send-btn", defer_geometry: bool = False) -> None:
            label_writes.append((button_id, label))
            original_set_label(label, button_id=button_id, defer_geometry=defer_geometry)

        monkeypatch.setattr(input_bar, "_set_btn_label", record_label)
        assert controller.switch_locale("zh-Hans").status is LocaleSwitchStatus.EFFECTIVE_CHANGED

        assert ("send-btn", "发送") in label_writes
        assert str(send.label) == "发送"
        assert str(new.label) == "新建"
        assert send.styles.width is not None
        assert send.styles.width.value == cell_len("发送") + 4
        assert text_area.placeholder.plain == (
            "输入消息（Ctrl+O 打开编辑器）   #  智能体  $  模型  /  命令  @  文件  !  终端"  # noqa: RUF001
        )
        assert text_area.render_line(0).text.rstrip().endswith("Ctrl+J 换行")

        signature_after_switch = input_bar._interaction_signature
        input_bar.agent_running = True
        await pilot.pause()
        assert input_bar._interaction_signature != signature_after_switch
        assert str(send.label) == "中断"
        assert text_area.placeholder.plain == "注入消息  /  命令  @  文件"

        input_bar.agent_running = False
        adapter_screen = type("_AdapterScreen", (), {"query_one": lambda _self, _type: input_bar})()
        adapter = MainScreenViewAdapter(adapter_screen, state=MainScreenState())  # type: ignore[arg-type]
        adapter.set_retry_mode(True, label=INPUT_RETRY.bind())
        assert str(send.label) == "重试"
        assert controller.switch_locale("en").status is LocaleSwitchStatus.EFFECTIVE_CHANGED
        assert str(send.label) == "Retry"

        assert controller.switch_locale("zh-Hans").status is LocaleSwitchStatus.EFFECTIVE_CHANGED
        input_bar.retry_mode = False
        input_bar.agent_running = True
        input_bar.lock_with_text()
        assert str(send.label) == "已排队"

    assert input_bar is not None
    assert input_bar not in controller._surfaces


def _draft_restore_adapter(input_bar: InputBar) -> MainScreenViewAdapter:
    screen = type("_AdapterScreen", (), {"query_one": lambda _self, _type: input_bar})()
    return MainScreenViewAdapter(screen, state=MainScreenState())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_restore_draft_defers_to_a_draft_that_landed_while_the_hand_back_travelled() -> None:
    """The composer hands a consumed prompt back only into an empty draft."""
    async with _InputBarApp().run_test(size=(120, 15)) as pilot:
        input_bar = pilot.app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)

        assert input_bar.restore_draft("rejected prompt") is True
        assert input_bar.value == "rejected prompt"

        # A draft typed before the hand-back landed owns the composer: replacing
        # it would destroy it and move the cursor, which also breaks an
        # in-progress IME composition.
        text_area.load_text("")
        text_area.focus()
        text_area.insert("\u4f60\u597d")
        cursor = text_area.cursor_location
        assert input_bar.restore_draft("rejected prompt") is False
        assert input_bar.value == "\u4f60\u597d"
        assert text_area.cursor_location == cursor

        # An indent or a stray newline is a draft too: no consume path leaves
        # whitespace behind, so it can only be something the user just typed.
        text_area.load_text("  \n ")
        assert input_bar.restore_draft("rejected prompt") is False
        assert input_bar.value == "  \n "

        # An empty hand-back writes nothing, even into an empty composer.
        text_area.load_text("")
        assert input_bar.restore_draft("") is False
        assert input_bar.value == ""
        assert input_bar.restore_draft("rejected prompt") is True
        assert input_bar.value == "rejected prompt"


@pytest.mark.asyncio
async def test_model_unconfigured_submit_hand_backs_never_overwrite_a_new_draft() -> None:
    """Both blocked-submit hand-backs are one message hop, which shortens the race, not closes it."""
    async with _InputBarApp().run_test(size=(120, 15)) as pilot:
        input_bar = pilot.app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)

        # InputBar cleared the composer when it posted UserSubmitted/RetryRequested;
        # the user starts the next message before MainScreen handles the message.
        text_area.load_text("the next thing I want to say")
        assert input_bar.restore_draft("blocked submit") is False
        assert input_bar.restore_draft("blocked retry note") is False
        assert input_bar.value == "the next thing I want to say"


@pytest.mark.asyncio
async def test_restore_input_text_issues_a_credential_only_when_it_wrote() -> None:
    """The rewrite follow-up is authorized by that restore, never by text equality."""
    async with _InputBarApp().run_test(size=(120, 15)) as pilot:
        input_bar = pilot.app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        adapter = _draft_restore_adapter(input_bar)

        restore = adapter.restore_input_text("describe @shot.png")
        assert restore is not None
        assert input_bar.value == "describe @shot.png"

        # The image-rejection dialog's "use paths" action rewrites what that
        # restore put there.
        assert adapter.rewrite_restored_input(restore, "describe /abs/shot.png") is True
        assert input_bar.value == "describe /abs/shot.png"

        # The credential is spent, so the same one cannot be replayed later.
        assert adapter.rewrite_restored_input(restore, "describe /other.png") is False
        assert input_bar.value == "describe /abs/shot.png"

        # A restore that declined issues nothing, so its follow-up is refused too.
        text_area.load_text("typed in the publish window")
        assert adapter.restore_input_text("describe @shot.png") is None
        assert adapter.rewrite_restored_input(None, "describe /abs/shot.png") is False
        assert input_bar.value == "typed in the publish window"


@pytest.mark.asyncio
async def test_stale_credential_cannot_reclaim_a_draft_that_reads_like_its_text() -> None:
    """Ownership is the credential: identical text typed later is still the user's."""
    async with _InputBarApp().run_test(size=(120, 15)) as pilot:
        input_bar = pilot.app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        adapter = _draft_restore_adapter(input_bar)

        stale = adapter.restore_input_text("describe @shot.png")
        assert stale is not None

        # That prompt is submitted, and the user later types the very same text
        # again — from history, or by hand.
        text_area.load_text("")
        text_area.focus()
        text_area.insert("describe @shot.png")
        cursor = text_area.cursor_location

        # A rollback or rejection arriving now must not mistake it for ours,
        # and neither may the old credential.
        assert adapter.restore_input_text("rolled back text") is None
        assert adapter.rewrite_restored_input(stale, "describe /abs/shot.png") is False
        assert input_bar.value == "describe @shot.png"
        assert text_area.cursor_location == cursor


@pytest.mark.asyncio
async def test_rewrite_restored_input_leaves_identical_text_and_its_cursor_alone() -> None:
    """Rewriting to the same text would still reset the cursor and break IME composition."""
    async with _InputBarApp().run_test(size=(120, 15)) as pilot:
        input_bar = pilot.app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        adapter = _draft_restore_adapter(input_bar)

        restore = adapter.restore_input_text("describe shot.png")
        assert restore is not None
        text_area.focus()
        text_area.move_cursor((0, 0))

        assert adapter.rewrite_restored_input(restore, "describe shot.png") is True
        assert input_bar.value == "describe shot.png"
        assert text_area.cursor_location == (0, 0)


@pytest.mark.asyncio
async def test_restore_input_text_hands_back_a_refused_queued_message() -> None:
    """A queued message's composer stays read-only, so unlocking it restores that message."""
    async with _InputBarApp().run_test(size=(120, 15)) as pilot:
        input_bar = pilot.app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        adapter = _draft_restore_adapter(input_bar)

        # Enter while the agent runs keeps the text, locked, and sends it stripped.
        text_area.load_text("describe @shot.png\n")
        input_bar.lock_with_text()

        restore = adapter.restore_input_text("describe @shot.png")

        assert restore is not None
        assert not input_bar.locked
        # The image-rejection dialog's "use paths" action rewrites it in place.
        assert adapter.rewrite_restored_input(restore, "describe /abs/shot.png") is True
        assert input_bar.value == "describe /abs/shot.png"


@pytest.mark.asyncio
async def test_restore_input_text_unlocks_before_deferring_to_a_live_draft() -> None:
    """Abandoned injections still unlock the bar even when the restore is skipped."""
    async with _InputBarApp().run_test(size=(120, 15)) as pilot:
        input_bar = pilot.app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        adapter = _draft_restore_adapter(input_bar)

        text_area.load_text("typed while queued")
        input_bar.lock_with_text()
        assert input_bar.locked

        assert adapter.restore_input_text("rolled back text") is None

        assert not input_bar.locked
        assert input_bar.value == "typed while queued"


@pytest.mark.asyncio
async def test_input_bar_button_labels_treat_translation_markup_as_literal() -> None:
    class MarkupLocalizer:
        effective_locale = "zh-Hans"

        def render(self, reference: MessageRef) -> str:
            if reference.definition is INPUT_SEND:
                return "[red]发送[/red]"
            return reference.definition.fallback

    controller = LocaleController(
        Settings(locale="zh-Hans"),
        localizer=MarkupLocalizer(),  # type: ignore[arg-type]
    )

    async with _LocalizedInputBarApp(controller).run_test(size=(120, 15)) as pilot:
        input_bar = pilot.app.query_one(InputBar)
        send = input_bar.query_one("#send-btn", Button)
        # Markup-looking translation text stays literal (never parsed as
        # Textual markup) and the pinned width matches the literal cells.
        assert send.label.plain == "[red]发送[/red]"
        assert not send.label.spans
        assert send.styles.width is not None
        assert send.styles.width.value == cell_len("[red]发送[/red]") + 4


@pytest.mark.asyncio
async def test_input_bar_snapshot_and_replace_draft_cursor_semantics() -> None:
    app = _InputBarApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", TextArea)
        input_bar.value = "before"
        text_area.move_cursor((0, 2))

        snapshot = input_bar.snapshot_draft()

        assert snapshot.text == "before"
        assert snapshot.cursor_location == (0, 2)
        assert snapshot.revision == input_bar.draft_revision
        assert input_bar.value == "before"
        assert text_area.cursor_location == (0, 2)

        input_bar.replace_draft("first\nsecond")
        await pilot.pause()

        assert input_bar.value == "first\nsecond"
        assert text_area.cursor_location == (1, 6)
        assert input_bar.query_one("#send-btn", Button).disabled is False
        assert input_bar.draft_revision > snapshot.revision


@pytest.mark.asyncio
async def test_input_bar_ctrl_j_keeps_cursor_visible_after_height_cap() -> None:
    app = _InputBarApp()

    async with app.run_test(size=(130, 15)) as pilot:
        await pilot.pause()
        text_area = app.query_one(InputBar).query_one("#chat-input", TextArea)
        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="control focus before interaction")

        await pilot.press("1")
        for line in range(2, 9):
            await pilot.press("ctrl+j")
            await pilot.press(str(line))
        await pilot.pause()

        assert text_area.document.line_count == 8
        assert text_area.content_size.height == 7

        await pilot.press("ctrl+j")
        await pilot.pause()

        cursor_y = text_area.cursor_location[0]
        scroll_y = round(text_area.scroll_y)
        assert scroll_y <= cursor_y < scroll_y + text_area.content_size.height


@pytest.mark.asyncio
async def test_input_bar_keeps_arrow_keys_while_agent_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Running drafts should still support normal history and cursor movement."""
    monkeypatch.setattr(
        input_bar_module,
        "append_history",
        lambda text, *, session_id=None, instance_id=None, cwd=None: None,
    )
    app = _InputBarApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", TextArea)
        input_bar.add_to_history("old prompt")
        input_bar.agent_running = True
        input_bar.value = "draft"
        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()

        await pilot.press("up")
        assert input_bar.value == "old prompt"

        text_area.move_cursor((0, 2))
        await pilot.press("left")
        assert text_area.cursor_location == (0, 1)
        await pilot.press("right")
        assert text_area.cursor_location == (0, 2)


_WRAPPED_LINES = {"latin": " ".join(["word"] * 40), "cjk": "很长的一句话没有换行符" * 8}


@pytest.mark.asyncio
@pytest.mark.parametrize("script", list(_WRAPPED_LINES))
async def test_up_moves_through_a_wrapped_line_before_recalling_history(
    monkeypatch: pytest.MonkeyPatch, script: str
) -> None:
    """A line too long for the box wraps onto several rows but is one line:
    up moves row by row and recalls the previous prompt only from the first."""
    monkeypatch.setattr(
        input_bar_module,
        "append_history",
        lambda text, *, session_id=None, instance_id=None, cwd=None: None,
    )
    draft = _WRAPPED_LINES[script]
    app = _InputBarApp()

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", TextArea)
        input_bar.add_to_history("old prompt")
        input_bar.value = draft
        text_area.focus()
        await wait_for(
            lambda: text_area.has_focus and text_area.wrapped_document.height >= 3,
            pilot=pilot,
            description="a focused draft wrapped onto three rows or more",
        )
        rows = text_area.wrapped_document.height

        for _ in range(rows - 1):
            await pilot.press("up")
        assert input_bar.value == draft
        assert text_area.navigator.is_first_wrapped_line(text_area.cursor_location)

        await pilot.press("up")
        assert input_bar.value == "old prompt"


@pytest.mark.asyncio
async def test_down_moves_through_a_wrapped_history_entry_before_the_next_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        input_bar_module,
        "append_history",
        lambda text, *, session_id=None, instance_id=None, cwd=None: None,
    )
    entry = _WRAPPED_LINES["latin"]
    app = _InputBarApp()

    async with app.run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", TextArea)
        input_bar.add_to_history(entry)
        input_bar.add_to_history("newer prompt")
        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="composer focus before interaction")
        await pilot.press("up")
        await pilot.press("up")
        assert input_bar.value == entry
        await wait_for(
            lambda: text_area.wrapped_document.height >= 3,
            pilot=pilot,
            description="the recalled prompt wrapped onto three rows or more",
        )
        rows = text_area.wrapped_document.height
        for _ in range(rows - 1):
            await pilot.press("up")
        assert text_area.navigator.is_first_wrapped_line(text_area.cursor_location)

        for _ in range(rows - 1):
            await pilot.press("down")
        assert input_bar.value == entry
        assert text_area.navigator.is_last_wrapped_line(text_area.cursor_location)

        await pilot.press("down")
        assert input_bar.value == "newer prompt"


@pytest.mark.asyncio
async def test_input_bar_suppresses_arrow_keys_for_queued_injection(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once an injection is queued, disabling the input must block arrow edits."""
    monkeypatch.setattr(
        input_bar_module,
        "append_history",
        lambda text, *, session_id=None, instance_id=None, cwd=None: None,
    )
    app = _InputBarApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", TextArea)
        input_bar.add_to_history("old prompt")
        input_bar.agent_running = True
        input_bar.value = "draft"
        input_bar.lock_with_text()
        text_area.focus()
        await pilot.pause()

        assert text_area.disabled is True

        await pilot.press("up")
        await pilot.press("down")

        assert input_bar.value == "draft"

        text_area.move_cursor((0, 2))
        await pilot.press("left")
        assert text_area.cursor_location == (0, 2)
        await pilot.press("right")
        assert text_area.cursor_location == (0, 2)


@pytest.mark.asyncio
async def test_input_bar_keeps_running_suggestion_arrow_navigation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlocked running input should still route up/down to active suggestions."""
    monkeypatch.setattr(input_bar_module, "load_history", list)
    app = _InputBarApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.query_one(InputBar)
        input_bar.agent_running = True
        input_bar.set_suggestions_active(True, mode="files")
        input_bar.query_one("#chat-input", TextArea).focus()
        await wait_for(
            lambda: input_bar.query_one("#chat-input", TextArea).has_focus,
            pilot=pilot,
            description="control focus before interaction",
        )
        await pilot.pause()

        await pilot.press("up")
        await pilot.press("down")

        assert app.suggestion_directions == ["up", "down"]


@pytest.mark.parametrize("width", [24, 40, 80])
async def test_narrow_editor_button_preserves_cells_and_adjacent_frame(width: int) -> None:
    """The three-cell button must not emit hidden overflow into adjacent cells."""
    async with _InputBarApp().run_test(size=(width, 10)) as pilot:
        bar = pilot.app.query_one(InputBar)
        editor = bar.query_one("#editor-btn", Button)
        await wait_for(lambda: editor.region.width == 3, pilot=pilot, description="compact editor button layout")
        strips = editor.render_lines(editor.size.region)
        assert strips
        for strip in strips:
            assert strip.cell_length == sum(cell_len(segment.text) for segment in strip)
            assert strip.cell_length == editor.region.width
        frame = pilot.app.screen._compositor.render_strips()
        row = frame[editor.region.y]
        assert row.cell_length == sum(cell_len(segment.text) for segment in row) == width
        assert row.crop(editor.region.x - 1, editor.region.x).text == "│"
        assert row.crop(bar.region.right - 1, bar.region.right).text == "│"


async def test_input_bar_buttons_hug_labels_without_side_gaps() -> None:
    """Localized labels drive button and group width without dead space."""
    async with _InputBarApp().run_test(size=(80, 10)) as pilot:
        bar = pilot.app.query_one(InputBar)
        group = bar.query_one("#btn-group")
        editor = bar.query_one("#editor-btn", Button)
        send = bar.query_one("#send-btn", Button)
        new = bar.query_one("#new-btn", Button)
        text_area = bar.query_one("#chat-input", TextArea)
        assert text_area.styles.background.a == pytest.approx(0.04)
        assert editor.region.width == 3
        assert editor.allow_select is False
        assert text_area.allow_select is True
        input_prompt = bar.query_one("#editor-btn", Button)
        pilot.app.screen.selections = {input_prompt: SELECT_ALL}
        await pilot.pause()
        assert input_prompt.text_selection is None
        assert pilot.app.screen.get_selected_text() == ""
        assert send.allow_select is False
        assert new.allow_select is False
        pilot.app.screen.selections = {send: SELECT_ALL, new: SELECT_ALL}
        await pilot.pause()
        assert send.text_selection is None
        assert new.text_selection is None
        assert pilot.app.screen.get_selected_text() == ""

        # Idle without messages: collapsed New slot and label-hugging Send.
        assert new.region.width == 0
        assert send.region.width == len("Send") + 4
        assert send.region.x == group.content_region.x
        assert send.region.right == group.content_region.right

        bar.value = "\n".join(f"line {index}" for index in range(12))
        await pilot.pause()
        assert text_area.show_vertical_scrollbar is True
        assert text_area.styles.scrollbar_background.a == pytest.approx(0.12)
        assert text_area.styles.scrollbar_background_hover.a == pytest.approx(0.16)
        assert text_area.styles.scrollbar_background_active.a == pytest.approx(0.20)
        assert text_area.vertical_scrollbar.region.right + 1 == send.region.x

        # A longer localized-style label expands naturally without side gaps.
        localized_label = "发送消息"
        bar._set_btn_label(localized_label)
        await pilot.pause()
        assert send.region.width == Text(localized_label).cell_len + 4
        assert text_area.vertical_scrollbar.region.right + 1 == send.region.x
        assert send.region.right == group.content_region.right
        bar._set_btn_label("Send")
        await pilot.pause()

        # Running: Interrupt tracks its own label width.
        bar.agent_running = True
        await pilot.pause()
        assert str(send.label) == "Interrupt"
        assert new.region.width == 0
        assert send.region.width == len("Interrupt") + 4
        assert send.region.x == group.content_region.x
        assert send.region.right == group.content_region.right
        strips = pilot.app.screen._compositor.render_strips()
        row = next(strip.text for strip in strips if "Interrupt" in strip.text)
        after_label = row[row.index("Interrupt") + len("Interrupt") :]
        # Only the button's own padding, the group padding, and the border
        # may follow the label — no dead reserved slot.
        assert after_label.strip("│ ") == ""

        # Retry mode also hugs its label.
        bar.agent_running = False
        bar._retry_label = "Continue"
        bar.retry_mode = True
        await pilot.pause()
        assert send.region.width == len("Continue") + 4
        strips = pilot.app.screen._compositor.render_strips()
        row = next(strip.text for strip in strips if "Continue" in strip.text)
        assert "  Continue  " in row

        # Idle with messages: New appears beside Send, flush right.
        bar.retry_mode = False
        bar.has_messages = True
        await pilot.pause()
        assert send.region.width == len("Send") + 4
        assert new.region.width == len("New") + 4
        assert new.region.right == group.content_region.right
        assert new.region.x == send.region.right + 1

        # The reserved group fits the widest real combination plus its
        # inter-button margin and two one-cell edge paddings.
        assert group.region.width == len("Send") + 4 + 1 + len("New") + 4 + 2


@pytest.mark.parametrize(
    ("value", "expected_submitted", "expected_value"),
    [
        ("hello world", ["hello world"], ""),
        ("   ", [], "   "),
    ],
    ids=["text-submits-and-clears", "whitespace-only-is-ignored"],
)
async def test_input_bar_submit(value: str, expected_submitted: list[str], expected_value: str) -> None:
    """Submitting text fires UserSubmitted and clears the draft; whitespace-only input fires nothing."""
    app = _InputBarApp()
    async with app.run_test() as pilot:
        ib = pilot.app.query_one(InputBar)
        ib.value = value
        await ib.action_submit()
        await pilot.pause()
        assert app.submitted == expected_submitted
        assert ib.value == expected_value


async def test_input_bar_send_button_disabled_for_empty_input() -> None:
    """Idle Send should only be clickable when there is non-empty text."""
    app = _InputBarApp()
    async with app.run_test() as pilot:
        ib = pilot.app.query_one(InputBar)
        send_btn = ib.query_one("#send-btn", Button)

        assert send_btn.disabled is True
        send_btn.press()
        await pilot.pause()
        assert app.submitted == []

        ib.value = "hello world"
        await pilot.pause()
        assert send_btn.disabled is False

        send_btn.press()
        await pilot.pause()
        assert app.submitted == ["hello world"]
        assert ib.value == ""
        assert send_btn.disabled is True


async def test_input_bar_loading_disables_submit_but_keeps_input_editable() -> None:
    """Agent loading disables Send without making the draft read-only."""
    app = _InputBarApp()
    async with app.run_test() as pilot:
        ib = pilot.app.query_one(InputBar)
        send_btn = ib.query_one("#send-btn", Button)
        text_area = ib.query_one("#chat-input", TextArea)

        ib.value = "queued text"
        await pilot.pause()
        assert send_btn.disabled is False

        ib.agent_loading = True
        await pilot.pause()
        assert send_btn.disabled is True
        assert str(send_btn.label) == "Send"
        assert ib.query_one("#editor-btn", Button).render().plain == ">"
        assert text_area.read_only is False

        send_btn.press()
        await ib.action_submit()
        await pilot.pause()
        assert app.submitted == []
        assert ib.value == "queued text"

        ib.agent_loading = False
        await pilot.pause()
        assert send_btn.disabled is False


async def test_input_bar_locked_queue_disables_text_area_until_unlock() -> None:
    """Queued mid-run injection disables text-area interaction until it is consumed or abandoned."""

    async with _InputBarApp().run_test() as pilot:
        ib = pilot.app.query_one(InputBar)
        send_btn = ib.query_one("#send-btn", Button)
        text_area = ib.query_one("#chat-input", TextArea)

        ib.value = "queued injection"
        ib.lock_with_text()
        await pilot.pause()

        assert ib.locked is True
        assert text_area.read_only is True
        assert text_area.disabled is True
        assert send_btn.disabled is True

        ib.unlock_and_keep()
        await pilot.pause()

        assert ib.locked is False
        assert text_area.read_only is False
        assert text_area.disabled is False


async def test_input_bar_consume_retry_text_returns_and_clears_note() -> None:
    async with _InputBarApp().run_test() as pilot:
        ib = pilot.app.query_one(InputBar)

        ib.value = "  retry note  "
        await pilot.pause()

        assert ib.consume_retry_text() == "retry note"
        assert ib.value == ""


class _MainScreenUserSubmitRecordingApp(_MainScreenInputBindingApp):
    """Pilot host that records UserSubmitted messages for no-submit assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.user_submitted: list[str] = []

    def on_input_bar_user_submitted(self, event: InputBar.UserSubmitted) -> None:
        self.user_submitted.append(event.text)


@pytest.mark.asyncio
async def test_enter_tab_during_history_loading_do_not_mis_select(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enter/Tab pressed against the *loading* history popup select nothing
    and never submit the draft: rows do not exist yet, and the unselected
    Enter consume-fallback must not fall through to submit_user_text (adding
    "history" to SuggestionsController.on_suggestion_select's mode allow-list
    would clear the input and submit the draft here)."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def load_prompt_history(_input_bar: InputBar, *, max_entries: int) -> list[str]:
        assert max_entries == 100
        started.set()
        await release.wait()
        return ["ready prompt"]

    monkeypatch.setattr(InputBar, "load_prompt_history", load_prompt_history)
    app = _MainScreenUserSubmitRecordingApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.value = "ready"
        input_bar.focus_input()
        await wait_for(
            lambda: input_bar.query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )

        await pilot.press("ctrl+r")
        await started.wait()

        suggestions = app.main_screen.query_one(SuggestionList)
        assert suggestions.mode == "history"
        assert suggestions.is_visible is True
        assert suggestions.is_loading is True
        assert suggestions._values == []

        try:
            # Enter while loading: no row to select and the draft must not be
            # submitted; the popup stays in its loading state.
            await pilot.press("enter")
            await pilot.pause()
            assert app.user_submitted == []
            assert input_bar.value == "ready"
            assert suggestions.is_visible is True
            assert suggestions.is_loading is True
            assert suggestions._values == []

            # Tab while loading: consumed by the suggestion layer, selects
            # nothing, submits nothing.
            await pilot.press("tab")
            await pilot.pause()
            assert app.user_submitted == []
            assert input_bar.value == "ready"
            assert suggestions.is_visible is True
            assert suggestions.is_loading is True
            assert suggestions._values == []
        finally:
            release.set()

        # Once the load resolves, the popup swaps to content in one step and
        # the same keys drive a real selection.
        await wait_for(
            lambda: not suggestions.is_loading,
            pilot=pilot,
            description="history rows to appear after release",
        )
        assert suggestions._values == ["ready prompt"]

        await pilot.press("enter")
        await pilot.pause()
        assert input_bar.value == "ready prompt"
        assert suggestions.mode == ""
        assert app.user_submitted == []
