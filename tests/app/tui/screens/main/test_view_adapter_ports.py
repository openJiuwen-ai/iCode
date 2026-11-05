# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Port conformance checks for the main-screen Textual adapter."""

from __future__ import annotations

import inspect
from typing import Protocol

import pytest
from textual.widgets import Static

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.i18n import LocaleController, LocaleSwitchStatus
from chrys.app.tui.screens.main import ports
from chrys.app.tui.screens.main.state import MainScreenState
from chrys.app.tui.screens.main.view_adapter import MainScreenViewAdapter
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.status_bar import StatusBar
from chrys.foundation.config.settings import Settings
from chrys.foundation.i18n import MessageRef
from tests.support.tui_helpers import WidgetApp, fake_session_title


def _protocol_member_names(protocol: type[Protocol]) -> set[str]:
    names: set[str] = set()
    for base in reversed(protocol.__mro__):
        if base in {Protocol, object}:
            continue
        for name, value in base.__dict__.items():
            if name.startswith("_"):
                continue
            if inspect.isfunction(value) or isinstance(value, property):
                names.add(name)
    return names


@pytest.mark.parametrize(
    "protocol",
    [
        ports.InputFlowView,
        ports.ShellModeView,
        ports.InputFocusView,
        ports.SuggestionPopupView,
        ports.BuddyCommandView,
        ports.SessionLifecycleView,
        ports.RuntimeConfigView,
        ports.WorkspaceView,
        ports.CopyActionView,
        ports.DiffView,
        ports.RollbackView,
        ports.NavigationView,
        ports.ToolActionView,
        ports.DialogGatewayView,
        ports.MainScreenPresentationView,
        ports.BackendEventView,
    ],
)
def test_main_screen_view_adapter_implements_view_ports(protocol: type[Protocol]) -> None:
    missing = sorted(name for name in _protocol_member_names(protocol) if not hasattr(MainScreenViewAdapter, name))

    assert missing == []


async def test_main_view_status_callers_preserve_message_refs_for_live_retranslation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from textual.widgets import Footer

    from chrys.app.tui.screens.main.view_adapter import MainScreenViewAdapter
    from chrys.app.tui.terminal.panel import ShellPanel
    from chrys.app.tui.widgets.chrome import status_bar as status_bar_module
    from chrys.app.tui.widgets.sidebar.panel import SidebarPanel

    controller = LocaleController(Settings(locale="en"))
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    now = [100.0]
    monkeypatch.setattr(status_bar_module, "time", SimpleNamespace(monotonic=lambda: now[0]))

    class _RetryPanel:
        async def prepare_retry(self) -> None:
            return

        async def add_retry(self, *_args: object) -> None:
            return

    class _Terminal:
        def focus(self) -> None:
            return

    class _Shell:
        def query_one(self, _widget_type: type) -> _Terminal:
            return _Terminal()

    class _Sidebar:
        def suppress_visibility(self, _owner: str, _active: bool) -> None:
            return

    class _Footer:
        display = True

    class _Screen:
        def __init__(self, status_bar: StatusBar) -> None:
            self.status_bar = status_bar
            self.retry_panel = _RetryPanel()
            self.shell = _Shell()
            self.sidebar = _Sidebar()
            self.footer = _Footer()
            self.terminal_title_result = ""
            self._session_title = fake_session_title(
                mark_terminal_title_completed=lambda: setattr(self, "terminal_title_result", "✓"),
                mark_terminal_title_failed=lambda: setattr(self, "terminal_title_result", "✗"),
            )

        def query_one(self, widget_type: type | str):
            # The footer row holds the Footer and the hook indicator; shell mode hides the row.
            if widget_type == "#footer-row":
                return self.footer
            if widget_type is StatusBar:
                return self.status_bar
            if widget_type is ChatPanel:
                return self.retry_panel
            if widget_type is ShellPanel:
                return self.shell
            if widget_type is SidebarPanel:
                return self.sidebar
            if widget_type is Footer:
                return self.footer
            raise AssertionError(widget_type)

    async with WidgetApp(lambda: StatusBar(locale_controller=controller)).run_test() as pilot:
        status_bar = pilot.app.query_one(StatusBar)
        screen = _Screen(status_bar)
        state = MainScreenState()
        state.shell.active = True
        adapter = MainScreenViewAdapter(screen, state=state)  # type: ignore[arg-type]

        adapter.flash_interrupted()
        assert status_bar._flash is not None
        assert isinstance(status_bar._flash.text, MessageRef)
        assert controller.switch_locale("zh-Hans").status is LocaleSwitchStatus.EFFECTIVE_CHANGED
        assert status_bar.query_one("#status-flash", Static).render().plain == "已由用户中断"

        adapter.flash_turn_complete()
        adapter.mark_terminal_title_completed()
        assert screen.terminal_title_result == "✓"
        assert status_bar._flash is not None
        assert isinstance(status_bar._flash.text, MessageRef)
        assert status_bar.query_one("#status-flash", Static).render().plain.startswith("已完成")

        adapter.flash_status(status_bar._flash.text, error=True)
        adapter.mark_terminal_title_failed()
        assert screen.terminal_title_result == "✗"

        adapter.start_tool_status("read_file")
        assert isinstance(status_bar.status, MessageRef)
        assert status_bar.query_one("#status-text", Static).render().plain == "正在运行：read_file"  # noqa: RUF001

        now[0] += 1
        await adapter.show_retry_attempt("temporary", 2, 4, 1)
        assert isinstance(status_bar.status, MessageRef)
        assert status_bar.query_one("#status-text", Static).render().plain == "正在重试（2/4）..."  # noqa: RUF001

        adapter.set_alternate_screen_active(False)
        assert status_bar._flash is not None
        assert isinstance(status_bar._flash.text, MessageRef)
        assert status_bar.query_one("#status-flash", Static).render().plain == (
            "终端模式 — 连按两次 Esc 或输入 exit 退出"
        )

        state.shell.active = False
        status_bar.set_profile("Code Agent")
        status_bar.flash("Interactive terminal")
        adapter.set_alternate_screen_active(False)
        assert status_bar.visible is True
        assert status_bar._flash is None
        assert status_bar.query_one("#profile-tag", Static).render().plain == "Code Agent"


def test_approval_dialog_tool_name_is_the_raw_name_or_empty() -> None:
    from types import SimpleNamespace

    from chrys.app.tui.screens.dialogs.approval import ApprovalDialog
    from chrys.app.tui.screens.main.view_adapter import MainScreenViewAdapter

    adapter = MainScreenViewAdapter(SimpleNamespace(), state=MainScreenState())  # type: ignore[arg-type]
    acp_name = "acp: `fs/write_text_file`"

    assert adapter.approval_dialog_tool_name(ApprovalDialog(caller_name="", tool_name=acp_name)) == acp_name
    assert adapter.approval_dialog_tool_name(SimpleNamespace(tool_name="shell")) == ""  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("draft", "locked", "expected"),
    [
        ("queued text", True, ("", False, ["unlock_and_clear"])),
        ("queued text", False, ("", False, ["unlock_and_clear"])),
        # Enter sends the composer's text stripped.
        ("  queued text\n", False, ("", False, ["unlock_and_clear"])),
        ("edited draft", False, ("edited draft", False, [])),
    ],
    ids=["still_queued", "handed_back_unchanged", "handed_back_with_whitespace", "edited_since"],
)
def test_unlock_input_clearing_keeps_a_draft_edited_since(
    draft: str, locked: bool, expected: tuple[str, bool, list[str]]
) -> None:
    from types import SimpleNamespace

    from chrys.app.tui.widgets.chrome.input_bar import InputBar

    calls: list[str] = []
    input_bar = SimpleNamespace(value=draft, locked=locked)

    def unlock_and_clear() -> None:
        calls.append("unlock_and_clear")
        input_bar.value = ""
        input_bar.locked = False

    def unlock_and_keep() -> None:
        calls.append("unlock_and_keep")
        input_bar.locked = False

    input_bar.unlock_and_clear = unlock_and_clear
    input_bar.unlock_and_keep = unlock_and_keep

    def query_one(widget_type: type) -> object:
        assert widget_type is InputBar
        return input_bar

    adapter = MainScreenViewAdapter(SimpleNamespace(query_one=query_one), state=MainScreenState())  # type: ignore[arg-type]
    adapter.unlock_input_clearing("queued text")

    assert (input_bar.value, input_bar.locked, calls) == expected
