# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ShellModeController layout ownership and focus routing."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest

from chrys.app.tui.screens.main.shell_mode import ShellModeController
from chrys.app.tui.screens.main.state import MainScreenState
from chrys.app.tui.screens.main.view_adapter import MainScreenViewAdapter


class _FakeShellModeView:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def enter_shell_mode(self) -> None:
        self.calls.append("enter")

    def exit_shell_mode(self) -> None:
        self.calls.append("exit")

    def set_alternate_screen_active(self, active: bool) -> None:
        self.calls.append(("alternate", active))


class _FakeFocusView:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def focus_input(self) -> None:
        self.calls.append("focus_input")

    def insert_paste_payload(self, text: str) -> bool:
        self.calls.append(("paste", text))
        return True


def _make_shell_mode_controller(
    *,
    state: MainScreenState | None = None,
    shell_view: _FakeShellModeView | None = None,
    focus_view: _FakeFocusView | None = None,
    shell_mode_states: list[bool] | None = None,
    dismiss_suggestions: Callable[[], None] | None = None,
    panel_focus_changed: Callable[[], None] | None = None,
) -> tuple[ShellModeController, MainScreenState, _FakeShellModeView, _FakeFocusView, list[bool]]:
    state = state or MainScreenState()
    shell_view = shell_view or _FakeShellModeView()
    focus_view = focus_view or _FakeFocusView()
    shell_mode_states = shell_mode_states if shell_mode_states is not None else []

    controller = ShellModeController(
        state=state,
        shell_view=shell_view,
        focus_view=focus_view,
        set_shell_mode_state=shell_mode_states.append,
        panel_focus_changed=panel_focus_changed or (lambda: None),
        dismiss_suggestions=dismiss_suggestions or (lambda: None),
        debug=lambda *_args: None,
    )
    return controller, state, shell_view, focus_view, shell_mode_states


def test_focus_guard_allows_inline_status_action_button_focus() -> None:
    class _FakeStatusButton:
        def __init__(self) -> None:
            self.ancestors_with_self: list[object] = []

        def has_class(self, name: str) -> bool:
            return name == "status-action-btn"

    controller, _state, _shell_view, focus_view, _shell_mode_states = _make_shell_mode_controller()

    controller.on_descendant_focus(_FakeStatusButton())

    assert focus_view.calls == []


def test_focus_guard_still_redirects_display_only_chat_descendants() -> None:
    from chrys.app.tui.widgets.chat.panel import ChatPanel

    class _FakeChatChild:
        def __init__(self) -> None:
            self.ancestors_with_self = [self, ChatPanel()]

        def has_class(self, _name: str) -> bool:
            return False

    controller, _state, _shell_view, focus_view, _shell_mode_states = _make_shell_mode_controller()

    controller.on_descendant_focus(_FakeChatChild())

    assert focus_view.calls == ["focus_input"]


def test_focus_guard_allows_ask_user_inline_controls_to_keep_focus() -> None:
    from chrys.app.tui.widgets.ask_user_controls import AskUserResponseFooter

    controller, _state, _shell_view, focus_view, _shell_mode_states = _make_shell_mode_controller()

    controller.on_descendant_focus(AskUserResponseFooter("req-1"))

    assert focus_view.calls == []


@pytest.mark.parametrize(
    ("active", "fullscreen_terminal"),
    [(False, False), (True, False), (False, True), (True, True)],
    ids=["chat", "shell", "fullscreen", "shell-fullscreen"],
)
def test_panel_focus_is_kept_exactly_where_the_focus_guard_leaves_it(active: bool, fullscreen_terminal: bool) -> None:
    """MainScreen makes the sidebar tab strip focusable from ``keeps_panel_focus``."""
    from chrys.app.tui.widgets.chat.panel import ChatPanel

    class _FakeChatChild:
        def __init__(self) -> None:
            self.ancestors_with_self = [self, ChatPanel()]

        def has_class(self, _name: str) -> bool:
            return False

    controller, state, _shell_view, focus_view, _shell_mode_states = _make_shell_mode_controller()
    state.shell.active = active
    state.shell.fullscreen_terminal = fullscreen_terminal

    controller.on_descendant_focus(_FakeChatChild())

    assert controller.keeps_panel_focus() is (active or fullscreen_terminal)
    assert (focus_view.calls == []) is controller.keeps_panel_focus()


def test_shell_mode_state_watcher_owns_layout() -> None:
    from textual.widgets import Footer

    from chrys.app.tui.terminal.panel import ShellPanel
    from chrys.app.tui.terminal.widget import Terminal
    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from chrys.app.tui.widgets.chat.session_json import SessionJsonPanel
    from chrys.app.tui.widgets.chrome.hook_indicator import HookIndicator
    from chrys.app.tui.widgets.chrome.input_bar import InputBar
    from chrys.app.tui.widgets.chrome.prompt_card import PromptCard
    from chrys.app.tui.widgets.chrome.status_bar import StatusBar
    from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
    from chrys.app.tui.widgets.trajectory import TrajectoryDashboard

    calls: list[tuple[str, object]] = []

    class _FakeChat:
        display = True

    class _FakeSessionJson:
        display = False

        def suspend_for_shell_mode(self) -> None:
            calls.append(("session_json_suspend", None))
            self.display = False

        def finish_shell_mode(self, *, restore: bool) -> None:
            calls.append(("session_json_finish", restore))
            self.display = restore

        def hide_session_json(self) -> None:
            self.display = False

    class _FakeDashboard:
        display = False
        foreground = False
        session_json_visible = False

        def suspend_for_shell_mode(self) -> bool:
            self.display = False
            return False

        def finish_shell_mode(self) -> bool:
            return False

    class _FakeTerminal:
        def focus(self) -> None:
            calls.append(("terminal_focus", None))

    class _FakeShell:
        def show(self) -> None:
            calls.append(("shell_show", None))

        def hide(self) -> None:
            calls.append(("shell_hide", None))

        def query_one(self, cls: type) -> object:
            if cls is Terminal:
                return _FakeTerminal()
            raise AssertionError(f"unexpected shell query_one({cls.__name__})")

    class _FakeStatus:
        def snapshot(self) -> dict[str, object]:
            calls.append(("status_snapshot", None))
            return {"visible": True, "status": "Thinking"}

        def remove_class(self, class_name: str) -> None:
            calls.append(("status_remove", class_name))

        def flash(self, message: str, **kwargs: object) -> None:
            calls.append(("status_flash", (message, kwargs)))

        def restore(self, state: dict[str, object], *, tool_trail: object = None) -> None:
            calls.append(("status_restore", {**state, "tool_trail": tool_trail}))

    class _FakeInput:
        display = True

        def focus_input(self) -> None:
            calls.append(("input_focus", None))

    class _FakePromptCard:
        display = True

    class _FakeFooter:
        display = True

    class _FakeHookIndicator:
        display = True

    class _FakeSidebar:
        def add_class(self, class_name: str) -> None:
            calls.append(("sidebar_add", class_name))

        def remove_class(self, class_name: str) -> None:
            calls.append(("sidebar_remove", class_name))

    chat = _FakeChat()
    chat_column = _FakeChat()
    session_json = _FakeSessionJson()
    dashboard = _FakeDashboard()
    shell = _FakeShell()
    status = _FakeStatus()
    input_bar = _FakeInput()
    prompt_card = _FakePromptCard()
    footer = _FakeFooter()
    hook_indicator = _FakeHookIndicator()
    sidebar = _FakeSidebar()

    def query_one(cls: type | str) -> object:
        # The footer row holds the Footer and the hook indicator; shell mode hides the row.
        if cls == "#footer-row":
            return footer
        if cls == "#chat-column":
            return chat_column
        if cls is ChatPanel:
            return chat
        if cls is SessionJsonPanel:
            return session_json
        if cls is TrajectoryDashboard:
            return dashboard
        if cls is ShellPanel:
            return shell
        if cls is StatusBar:
            return status
        if cls is InputBar:
            return input_bar
        # The prompt shortcut card belongs to the input area: a surface without the input hides it too.
        if cls is PromptCard:
            return prompt_card
        if cls is Footer:
            return footer
        if cls is HookIndicator:
            return hook_indicator
        if cls is SidebarPanel:
            return sidebar
        raise AssertionError(f"unexpected query_one({cls!r})")

    screen = SimpleNamespace(
        _workflow=SimpleNamespace(workflow_mode=False),
        _workflow_panel=SimpleNamespace(display=False),
        _sync_workflow_timer=lambda: None,
        query_one=query_one,
        _debug=lambda *_args: None,
    )
    state = MainScreenState()
    adapter = MainScreenViewAdapter(screen, state=state)
    controller = ShellModeController(
        state=state,
        shell_view=adapter,
        focus_view=_FakeFocusView(),
        set_shell_mode_state=lambda active: setattr(screen, "shell_mode_state", active),
        panel_focus_changed=lambda: None,
        dismiss_suggestions=lambda: None,
        debug=lambda *_args: None,
    )

    controller.apply(True)

    assert state.shell.active is True
    assert chat.display is False
    assert input_bar.display is False
    assert prompt_card.display is False
    assert footer.display is False
    # Hidden with its row; its own display follows the app mode only.
    assert hook_indicator.display is True
    assert ("shell_show", None) in calls
    assert ("terminal_focus", None) in calls

    controller.apply(False)

    assert state.shell.active is False
    assert chat.display is True
    assert input_bar.display is True
    assert prompt_card.display is True
    assert footer.display is True
    assert hook_indicator.display is True
    assert ("shell_hide", None) in calls
    assert ("session_json_finish", False) in calls
    assert ("input_focus", None) in calls
    # Restored with the trail as it is now, which may have changed while the shell showed.
    assert ("status_restore", {"visible": True, "status": "Thinking", "tool_trail": ""}) in calls


def test_panel_focus_hears_each_shell_and_fullscreen_change_after_the_state_lands() -> None:
    state = MainScreenState()
    observed: list[tuple[bool, bool]] = []
    controller, _state, _shell_view, _focus_view, _shell_mode_states = _make_shell_mode_controller(
        state=state,
        panel_focus_changed=lambda: observed.append((state.shell.active, state.shell.fullscreen_terminal)),
    )

    controller.apply(True)
    controller.set_alternate_screen_active(True)
    controller.set_alternate_screen_active(False)
    controller.apply(False)

    assert observed == [(True, False), (True, True), (True, False), (False, False)]


def test_terminal_exit_events_write_screen_shell_source() -> None:
    state = MainScreenState()
    state.shell.active = True
    controller, _state, _shell_view, _focus_view, shell_mode_states = _make_shell_mode_controller(state=state)

    controller.exit_on_escape()
    assert shell_mode_states == [False]

    shell_mode_states.clear()
    state.shell.active = True
    controller.exit_on_shell_closed()
    assert shell_mode_states == [False]
