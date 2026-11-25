# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the fork-session dialog widget."""

from __future__ import annotations

from types import SimpleNamespace


def test_fork_session_dialog_only_allows_dismiss_after_result() -> None:
    from chrys.app.tui.screens.dialogs.fork_session import ForkSessionDialog

    dialog = ForkSessionDialog()
    assert dialog._allow_click_outside_dismiss() is False
    assert dialog._default_dismiss_result() is None

    dialog.set_success("fork-1234")
    assert dialog._allow_click_outside_dismiss() is True
    assert dialog._default_dismiss_result() == "stay"

    dialog = ForkSessionDialog()
    dialog.set_error("Fork failed")
    assert dialog._allow_click_outside_dismiss() is True
    assert dialog._default_dismiss_result() is None


async def test_fork_session_dialog_omits_new_window_button_when_unavailable() -> None:
    from textual.app import App

    from chrys.app.tui.screens.dialogs.fork_session import ForkSessionDialog

    app = App()
    dialog = ForkSessionDialog("fork-1234", show_new_window=False)

    async with app.run_test():
        await app.push_screen(dialog)

        assert list(dialog.query("#fork-session-open")) == []
        assert list(dialog.query("#fork-session-switch"))
        assert list(dialog.query("#fork-session-stay"))


def test_fork_session_focus_skips_buttons_in_hidden_groups() -> None:
    from chrys.app.tui.screens.dialogs.fork_session import ForkSessionDialog

    class _FakeParent:
        def __init__(self, *, display: bool) -> None:
            self.display = display

    class _FakeButton:
        def __init__(self, *, parent: _FakeParent, has_focus: bool = False) -> None:
            self.display = True
            self.parent = parent
            self.has_focus = has_focus
            self.focused = False

        def focus(self) -> None:
            self.focused = True

    visible_group = _FakeParent(display=True)
    hidden_group = _FakeParent(display=False)
    switch = _FakeButton(parent=visible_group)
    stay = _FakeButton(parent=visible_group, has_focus=True)
    ok = _FakeButton(parent=hidden_group)
    dialog = SimpleNamespace(
        query=lambda _cls: [switch, stay, ok],
        _button_is_visible=ForkSessionDialog._button_is_visible,
    )

    ForkSessionDialog._focus_relative(dialog, 1)

    assert switch.focused is True
    assert ok.focused is False
    assert ForkSessionDialog._button_is_visible(ok) is False


async def test_tab_failure_keeps_full_command_and_replaces_the_open_action(monkeypatch) -> None:
    from textual.app import App
    from textual.widgets import Button, Static

    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.screens.dialogs import fork_session
    from chrys.app.tui.terminal.fork_tab import ForkTabTarget
    from chrys.foundation.config.settings import Settings
    from tests.support.tui_helpers import click_when_settled, rich_plain
    from tests.support.waiting import wait_for

    fork_id = "edd10c91-fcc5-44d4-9f7e-aa1dd55e2282"
    cwd = "/workspace/project"
    copied = []
    attempts = []
    target = ForkTabTarget("Ghostty", "/dev/ttys010")

    async def open_tab(self, session_id, directory):
        assert self is target
        attempts.append(("tab", session_id, directory))
        from chrys.app.tui.terminal.fork_tab import _TAB_DENIED

        return _TAB_DENIED.bind()

    async def open_window(session_id, *, cwd):
        attempts.append(("window", session_id, cwd))
        raise fork_session.TerminalLaunchError("window launch failed")

    def copy_command(app, command):
        copied.append(command)
        return True

    monkeypatch.setattr(ForkTabTarget, "open", open_tab)
    monkeypatch.setattr(fork_session, "launch_new_chrys_window", open_window)
    monkeypatch.setattr(fork_session, "copy_text_to_clipboards", copy_command)
    app = App()
    async with app.run_test() as pilot:
        dialog = fork_session.ForkSessionDialog(
            tab_target=target, cwd=cwd, locale_controller=LocaleController(Settings(locale="zh-Hans"))
        )
        await app.push_screen(dialog)
        dialog.set_success(fork_id)
        button = dialog.query_one("#fork-session-open", Button)
        assert button.label.plain == "在新标签页打开"
        await click_when_settled(pilot, button)
        await wait_for(lambda: button.label.plain == "在新窗口中打开", pilot=pilot)
        reason = dialog.query_one("#fork-session-tab-reason", Static)
        assert "macOS 拒绝" in rich_plain(reason.render())
        assert app.screen is dialog
        await click_when_settled(pilot, button)
        await wait_for(lambda: "window launch failed" in rich_plain(reason.render()), pilot=pilot)
        assert rich_plain(dialog.query_one("#fork-session-command", Static).render()) == f"icode -s {fork_id}"
        await click_when_settled(pilot, "#fork-session-copy")
        assert copied == [f"icode -s {fork_id}"]
        assert attempts == [("tab", fork_id, cwd), ("window", fork_id, cwd)]
        assert app.screen is dialog


async def test_open_action_dismisses_only_after_host_success(monkeypatch) -> None:
    from textual.app import App
    from textual.widgets import Button

    from chrys.app.tui.screens.dialogs import fork_session
    from chrys.app.tui.terminal.fork_tab import ForkTabTarget
    from tests.support.tui_helpers import click_when_settled
    from tests.support.waiting import wait_for

    target = ForkTabTarget("Terminal", "/dev/ttys001")
    calls = []

    async def open_tab(self, session, cwd):
        calls.append((self, session, cwd))

    async def open_window(session, *, cwd):
        calls.append((None, session, cwd))

    monkeypatch.setattr(ForkTabTarget, "open", open_tab)
    monkeypatch.setattr(fork_session, "launch_new_chrys_window", open_window)
    app = App()
    async with app.run_test() as pilot:
        for host, label, result in ((target, "Open in New Tab", "new_tab"), (None, "Open in New Window", "new_window")):
            results = []
            dialog = fork_session.ForkSessionDialog("fork-id", tab_target=host, cwd="/workspace")
            await app.push_screen(dialog, results.append)
            button = dialog.query_one("#fork-session-open", Button)
            assert button.label.plain == label
            await click_when_settled(pilot, button)
            await wait_for(results.__len__, pilot=pilot)
            assert results == [result]
        assert calls == [(target, "fork-id", "/workspace"), (None, "fork-id", "/workspace")]
