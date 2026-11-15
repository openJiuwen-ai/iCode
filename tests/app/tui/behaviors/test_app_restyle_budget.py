# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the app's restyle and relayout budget."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from rich.cells import cell_len
from textual.screen import Screen
from textual.widgets import Static, TextArea

from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.config.settings import Settings
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, wait_for, wait_until_quiet, with_wait_deadline


@pytest.mark.parametrize("hidden_by", ["self", "ancestor", "overlay"])
async def test_inactive_loading_ticks_do_not_rebuild_populated_transcript(tmp_path: Path, hidden_by: str) -> None:
    """A hidden or covered spinner must not arrange or refresh its owning screen."""
    from textual.containers import VerticalGroup

    from chrys.app.tui.screens.dialogs.tool_view import ToolDetailModal
    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from chrys.app.tui.widgets.loading import ChrysLoadingIndicator

    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(120, 32)) as pilot:
        underlay = app.screen
        chat = underlay.query_one(ChatPanel)
        indicator = ChrysLoadingIndicator()
        container = VerticalGroup(indicator)
        container.styles.dock = "top"
        container.styles.height = 1
        indicator.display = hidden_by != "self"
        container.display = hidden_by != "ancestor"
        await chat.mount(*(Static(f"Transcript row {index}") for index in range(64)))
        await underlay.mount(container)
        await pilot.pause()
        if hidden_by == "overlay":
            await app.push_screen(ToolDetailModal(title="Details", input_widgets=[], output_widgets=[Static("Output")]))
            await pilot.pause()
        compositor = underlay._compositor
        with (
            patch.object(compositor, "_arrange_root", autospec=True, side_effect=compositor._arrange_root) as arrange,
            patch.object(underlay, "_refresh_layout", autospec=True, side_effect=underlay._refresh_layout) as layout,
            patch.object(indicator, "refresh", autospec=True, side_effect=indicator.refresh) as refresh,
        ):
            # Drive the timer callback with a cold map on every tick: cached
            # geometry would hide an accidental is_on_screen lookup.
            for _ in range(3):
                compositor._full_map_invalidated = True
                indicator.automatic_refresh()
            arrange.assert_not_called()
            layout.assert_not_called()
            refresh.assert_not_called()

        if hidden_by == "overlay":
            await app.pop_screen()
        indicator.display = container.display = True
        await wait_for(
            lambda: indicator in compositor.visible_widgets,
            pilot=pilot,
            description="uncovered loading indicator is composited",
        )
        with patch.object(indicator, "refresh", autospec=True, side_effect=indicator.refresh) as refresh:
            indicator.automatic_refresh()
            refresh.assert_called_once()


async def test_overlay_close_preserves_keyboard_only_mouse_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Removing an overlay must not invent a pointer at the default position."""

    app = make_chrys_app(tmp_path)

    async with app.run_test() as pilot:
        underlay = app.screen
        assert app.mouse_over is None

        await app.push_screen(Screen())
        await pilot.pause()
        assert app.mouse_over is None

        hover_hit_tests: list[tuple[int, int]] = []
        get_hover_widgets_at = underlay.get_hover_widgets_at

        def record_hover_hit_test(x: int, y: int):
            hover_hit_tests.append((x, y))
            return get_hover_widgets_at(x, y)

        monkeypatch.setattr(underlay, "get_hover_widgets_at", record_hover_hit_test)

        await app.pop_screen()
        await pilot.pause()

        assert app.screen is underlay
        assert app.mouse_over is None
        assert hover_hit_tests == []


async def test_native_modal_css_first_load_updates_only_modal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A new modal stylesheet must not invalidate a populated MainScreen."""
    from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    app = make_chrys_app(tmp_path)

    async with app.run_test() as pilot:
        underlay = VirtualizedMarkdown("# Populated underlay")
        await app.screen.mount(underlay)
        await pilot.pause()

        underlay_style_updates: list[None] = []
        monkeypatch.setattr(underlay, "notify_style_update", lambda: underlay_style_updates.append(None))
        stylesheet_update_roots: list[object] = []
        original_update = app.stylesheet.update

        def _record_update(root: object, animate: bool = False) -> None:
            stylesheet_update_roots.append(root)
            original_update(root, animate=animate)  # type: ignore[arg-type]

        monkeypatch.setattr(app.stylesheet, "update", _record_update)

        first = ConfirmDialog(title="Exit", message="Exit Chrys?", confirm_label="Exit")
        await app.push_screen(first)
        await pilot.pause()

        assert stylesheet_update_roots == [first]
        assert underlay_style_updates == []
        assert first.query_one("#confirm-container").outer_size.width == 43

        first.dismiss(False)
        await pilot.pause()
        stylesheet_update_roots.clear()

        second = ConfirmDialog(title="Exit", message="Exit Chrys?", confirm_label="Exit")
        await app.push_screen(second)
        await pilot.pause()

        assert stylesheet_update_roots == []
        assert second.query_one("#confirm-container").outer_size.width == 43


async def test_web_modal_css_first_load_retains_full_app_update(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Web modal CSS keeps Textual's global App:focus/App:blur semantics."""
    from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

    monkeypatch.setenv("TEXTUAL_DRIVER", "textual.drivers.web_driver:WebDriver")
    app = make_chrys_app(tmp_path)

    async with app.run_test() as pilot:
        await pilot.pause()
        stylesheet_update_roots: list[object] = []
        original_update = app.stylesheet.update

        def _record_update(root: object, animate: bool = False) -> None:
            stylesheet_update_roots.append(root)
            original_update(root, animate=animate)  # type: ignore[arg-type]

        monkeypatch.setattr(app.stylesheet, "update", _record_update)
        dialog = ConfirmDialog(title="Exit", message="Exit Chrys?", confirm_label="Exit")

        await app.push_screen(dialog)
        await pilot.pause()

        assert stylesheet_update_roots == [app]


async def test_native_diff_screen_css_first_load_updates_only_diff_screen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A secondary full screen must not restyle the suspended transcript."""
    from chrys.app.tui.screens.diff.screen import DiffScreen

    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    app = make_chrys_app(tmp_path)

    async with app.run_test() as pilot:
        await pilot.pause()
        stylesheet_update_roots: list[object] = []
        original_update = app.stylesheet.update

        def _record_update(root: object, animate: bool = False) -> None:
            stylesheet_update_roots.append(root)
            original_update(root, animate=animate)  # type: ignore[arg-type]

        monkeypatch.setattr(app.stylesheet, "update", _record_update)
        diff_screen = DiffScreen({}, cwd=str(tmp_path))

        await app.push_screen(diff_screen)
        await pilot.pause()

        assert stylesheet_update_roots == [diff_screen]
        assert diff_screen.query_one("#diff-container").styles.border.top[0] == "round"


async def test_theme_chrys_classes_join_single_required_global_refresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Theme selector classes must not trigger a redundant transcript restyle."""

    monkeypatch.setattr("chrys.app.tui.app.persist_theme", lambda _theme: None)
    app = make_chrys_app(tmp_path, settings=Settings(theme="chrys"))

    async with app.run_test() as pilot:
        await app.screen.mount(*(Static("") for _ in range(256)))
        await pilot.pause()
        redundant_screen_updates: list[bool] = []
        monkeypatch.setattr(
            app.screen,
            "update_node_styles",
            lambda animate=True: redundant_screen_updates.append(animate),
        )
        stylesheet_update_roots: list[object] = []
        original_update = app.stylesheet.update

        def _record_update(root: object, animate: bool = False) -> None:
            stylesheet_update_roots.append(root)
            original_update(root, animate=animate)  # type: ignore[arg-type]

        monkeypatch.setattr(app.stylesheet, "update", _record_update)

        app.theme = "textual-dark"

        assert "-chrys" not in app.classes
        assert "-chrys-ansi" not in app.classes
        assert redundant_screen_updates == []

        await pilot.pause()

        assert stylesheet_update_roots[0] is app
        assert sum(root is app for root in stylesheet_update_roots) == 1
        assert redundant_screen_updates == []


async def test_trajectory_dashboard_visibility_does_not_restyle_chat_descendants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """F12 visibility changes should perform layout without recursive CSS work."""
    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from chrys.app.tui.widgets.chat.session_json import SessionJsonPanel
    from chrys.app.tui.widgets.chrome.input_bar import InputBar
    from chrys.app.tui.widgets.trajectory import DashboardTab, TrajectoryDashboard

    app = make_chrys_app(tmp_path)

    async with app.run_test() as pilot:
        assert app._main_screen is not None
        chat = app._main_screen.query_one(ChatPanel)
        await chat.mount(*(Static("") for _ in range(256)))
        await pilot.pause()
        recursive_style_updates: list[bool] = []
        monkeypatch.setattr(
            chat,
            "update_node_styles",
            lambda animate=True: recursive_style_updates.append(animate),
        )

        await pilot.press("f12")
        await pilot.pause()
        dashboard = app._main_screen.query_one(TrajectoryDashboard)
        session_json = app._main_screen.query_one(SessionJsonPanel)
        assert chat.display is False
        assert dashboard.foreground is True
        assert dashboard.display is True

        await pilot.click("#session-data")
        await pilot.pause()
        assert dashboard.active_tab is DashboardTab.SESSION_DATA
        assert session_json.display is True
        assert dashboard.display is True

        await pilot.press("escape")
        await pilot.pause()
        assert chat.display is True
        assert dashboard.foreground is False
        assert session_json.display is False
        assert app._main_screen.query_one(InputBar).query_one(TextArea).has_focus

        chat.set_session_id("old-session")
        await pilot.press("f12")
        await pilot.pause()
        assert dashboard.foreground is True
        app._main_screen._view_adapter.set_chat_session_id("new-session")
        assert dashboard.foreground is False
        assert chat.session_id == "new-session"
        assert chat.display is True
        assert recursive_style_updates == []


async def test_trajectory_dashboard_foreground_shows_only_the_footer_chrome(tmp_path: Path) -> None:
    """A foreground F12 dashboard hides the status and input bars; Esc restores them."""
    from textual.widgets import Footer

    from chrys.app.tui.widgets.chrome.input_bar import InputBar
    from chrys.app.tui.widgets.chrome.status_bar import StatusBar
    from chrys.app.tui.widgets.trajectory import TrajectoryDashboard

    app = make_chrys_app(tmp_path)

    async with app.run_test() as pilot:
        assert app._main_screen is not None
        status_bar = app._main_screen.query_one(StatusBar)
        input_bar = app._main_screen.query_one(InputBar)
        footer = app._main_screen.query_one(Footer)

        await pilot.press("f12")
        await pilot.pause()
        dashboard = app._main_screen.query_one(TrajectoryDashboard)
        assert dashboard.foreground is True
        assert status_bar.display is False
        assert input_bar.display is False
        assert footer.display is True

        await pilot.press("escape")
        await pilot.pause()
        assert dashboard.foreground is False
        assert status_bar.display is True
        assert input_bar.display is True
        assert footer.display is True


async def test_visibility_only_widgets_do_not_restyle_descendants(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Sidebar, shell, and suggestions should toggle layout without CSS walks."""
    from chrys.app.tui.terminal.panel import ShellPanel
    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from chrys.app.tui.widgets.chrome.input_bar import InputBar
    from chrys.app.tui.widgets.chrome.status_bar import StatusBar
    from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionItem, SuggestionList
    from chrys.app.tui.widgets.sidebar.panel import SidebarPanel

    app = make_chrys_app(tmp_path)

    async with app.run_test() as pilot:
        sidebar = app.screen.query_one(SidebarPanel)
        shell = app.screen.query_one(ShellPanel)
        suggestions = app.screen.query_one(SuggestionList)
        chat = app.screen.query_one(ChatPanel)
        status = app.screen.query_one(StatusBar)
        input_bar = app.screen.query_one(InputBar)
        recursive_style_updates: list[str] = []
        for name, widget in (("sidebar", sidebar), ("shell", shell), ("suggestions", suggestions)):
            monkeypatch.setattr(
                widget,
                "update_node_styles",
                lambda animate=True, widget_name=name: recursive_style_updates.append(widget_name),
            )
        monkeypatch.setattr(shell, "call_after_refresh", lambda _callback, *args: None)
        monkeypatch.setattr(shell, "_start_shell", lambda _cwd: None)

        sidebar.toggle()
        assert sidebar.is_visible is False
        sidebar.toggle()
        assert sidebar.is_visible is True

        shell.show()
        assert shell.is_visible is True
        shell.hide()
        assert shell.is_visible is False

        status.show("Ready")
        await pilot.pause()
        chat_region = chat.region
        status_region = status.region
        input_region = input_bar.region
        layout_refreshes: list[None] = []
        monkeypatch.setattr(app.screen, "_refresh_layout", lambda *_args, **_kwargs: layout_refreshes.append(None))

        # Drain straggler layout work from setup so anything recorded below is
        # caused by the popup operations under test. Style updates are probed
        # but deliberately NOT cleared: a late restyle from the toggles above
        # is a real contract violation the final assert must still catch.
        await wait_until_quiet(
            lambda: (len(recursive_style_updates), len(layout_refreshes)),
            description="popup style and layout counters",
            pilot=pilot,
        )
        layout_refreshes.clear()

        suggestions.show("commands", [SuggestionItem("/help", "Help")])
        assert suggestions.is_visible is True
        await pilot.pause()
        assert chat.region == chat_region
        assert status.region == status_region
        assert input_bar.region == input_region
        assert suggestions.region.bottom == input_bar.region.y
        assert suggestions.region.overlaps(status.region)

        suggestions.update([SuggestionItem("/help", "Help"), SuggestionItem("/new", "New")])
        await pilot.pause()
        assert chat.region == chat_region
        assert status.region == status_region
        assert input_bar.region == input_region
        assert suggestions.region.bottom == input_bar.region.y
        assert suggestions.region.overlaps(status.region)

        suggestions.hide()
        assert suggestions.is_visible is False
        await pilot.pause()

        assert recursive_style_updates == []
        assert layout_refreshes == []


async def test_input_and_status_state_changes_do_not_relayout_large_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fixed chrome slots must not schedule transcript-wide layouts."""
    from chrys.app.tui.widgets.chrome.input_bar import InputBar
    from chrys.app.tui.widgets.chrome.status_bar import StatusBar

    app = make_chrys_app(tmp_path)

    async with app.run_test() as pilot:
        await app.screen.mount(*(Static("") for _ in range(64)))
        await pilot.pause()
        status = app.screen.query_one(StatusBar)
        input_bar = app.screen.query_one(InputBar)
        status.set_profile("Code Agent")
        status.set_tool_info("3 tools")
        # The 64-widget mount overflows the screen, which grows its vertical
        # scrollbar on a later layout pass; on loaded CI workers that pass can
        # land after any single pause. Snapshot (and patch the layout hook)
        # only once geometry has settled, or the pending scrollbar gutter is
        # applied mid-test by the chrome flips' compositor resync and shows
        # up as a phantom region change.
        await wait_until_quiet(
            lambda: (status.region, input_bar.region),
            description="status and input geometry",
            pilot=pilot,
        )
        status_region = status.region
        input_region = input_bar.region
        layout_refreshes: list[None] = []
        monkeypatch.setattr(app.screen, "_refresh_layout", lambda *_args, **_kwargs: layout_refreshes.append(None))

        # Drain straggler layout work from the 64-widget mount so anything
        # recorded below is caused by the chrome state changes under test.
        await wait_until_quiet(
            lambda: len(layout_refreshes),
            description="post-mount layout refresh count",
            pilot=pilot,
        )
        layout_refreshes.clear()

        status.show("Thinking")
        await pilot.pause()
        status.show("Still thinking")
        await pilot.pause()
        status.flash("Ready", caution=True)
        await pilot.pause()
        status.clear_status()
        await pilot.pause()

        input_bar.agent_running = True
        await pilot.pause()
        input_bar.agent_running = False
        input_bar.has_messages = True
        await pilot.pause()
        input_bar.agent_loading = True
        await pilot.pause()
        input_bar.agent_loading = False
        await pilot.pause()
        input_bar.lock_with_text()
        await pilot.pause()
        input_bar.injection_phase = "steering"
        await pilot.pause()
        input_bar.unlock_and_keep()
        await pilot.pause()

        assert status.region == status_region
        assert input_bar.region == input_region
        assert layout_refreshes == []


async def test_chat_sidebar_tab_clicks_do_not_bounce_focus_over_a_populated_transcript(tmp_path: Path) -> None:
    """Sidebar tab clicks must not bounce focus through the tab strip.

    Chat mode hands sidebar focus straight back to the input bar, so a focusable tab strip
    costs every click a focus/blur pair: whole-screen repaints and binding refreshes.
    """
    from textual.widget import Widget
    from textual.widgets import TabbedContent, Tabs

    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from chrys.app.tui.widgets.chrome.input_bar import InputBar
    from chrys.app.tui.widgets.sidebar.panel import SidebarPanel

    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        await main.query_one(ChatPanel).mount(*(Static(f"Transcript row {index}") for index in range(256)))
        chat_input = main.query_one(InputBar).query_one("#chat-input")
        await wait_for(lambda: main.focused is chat_input, pilot=pilot, description="chat input owns focus")
        tabs = main.query_one(SidebarPanel).query_one(TabbedContent)
        targets = ("tab-debug", "tab-tasks")
        focus_changes: list[Widget | None] = []

        def record_focus(focused: Widget | None) -> None:
            focus_changes.append(focused)

        main.watch(main, "focused", record_focus, init=False)
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="settled main screen")
        with (
            patch.object(main, "refresh", autospec=True, side_effect=main.refresh) as refresh,
            patch.object(main, "refresh_bindings", autospec=True, side_effect=main.refresh_bindings) as bindings,
        ):
            for target in targets:
                await click_when_settled(pilot, tabs.get_tab(target))
                await wait_for(lambda target=target: tabs.active == target, pilot=pilot)
            await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="settled main screen")

        refresh.assert_not_called()
        # Each activation refreshes the bindings once; a focus bounce adds several per click.
        assert bindings.call_count <= len(targets)
        assert focus_changes == []
        assert main.focused is chat_input
        assert not tabs.query_one(Tabs).can_focus


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_calls_under_auto_review_never_relayout_restyle_or_recompose_the_main_screen(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every judged call of a turn and every spinner frame changes the header: only it may be remapped."""
    from chrys.app.tui.screens.dialogs.approval.dialog import ApprovalDialog
    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from chrys.app.tui.widgets.chrome.app_header import AppHeader
    from chrys.app.tui.widgets.chrome.footer import ChrysFooter
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import ApprovalCancelled, ApprovalRequest, ApprovalReviewed
    from chrys.service.approval.policy import ApprovalMode

    def judged_call(request_id: str) -> ApprovalRequest:
        return ApprovalRequest(
            request_id=request_id,
            call_id=f"call-{request_id}",
            tool_name="run_command",
            tool_kind="shell",
            args={"command": "icode --version"},
            judging=True,
        )

    bus = EventBus()
    app = make_chrys_app(tmp_path, event_bus=bus)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        chat = main.query_one(ChatPanel)
        await chat.mount(*(Static(f"Transcript row {index}") for index in range(256)))
        header = main.query_one(AppHeader)
        header.approval_mode = ApprovalMode.AUTO
        badge = header.query_one("#approval-badge", Static)
        reviewing = header.query_one("#approval-reviewing", Static)
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="settled main screen")
        badge_region = badge.region
        badge_text = badge.render().plain
        chat_region = chat.region
        layout_refreshes: list[None] = []
        style_updates: list[str] = []
        footer_recomposes: list[None] = []
        monkeypatch.setattr(main, "_refresh_layout", lambda *_args, **_kwargs: layout_refreshes.append(None))
        for name, widget in (("main", main), ("chat", chat)):
            monkeypatch.setattr(
                widget,
                "update_node_styles",
                lambda animate=True, widget_name=name: style_updates.append(widget_name),
            )

        async def record_footer_recompose() -> None:
            footer_recomposes.append(None)

        monkeypatch.setattr(main.query_one(ChrysFooter), "recompose", record_footer_recompose)

        def reviewing_shows(count: int) -> bool:
            if not count:
                return not reviewing.visible
            text = reviewing.render().plain
            return (
                reviewing.visible
                and text.rstrip().endswith(" Reviewing")
                and reviewing.region.width == cell_len(text)
                and reviewing.region.right == badge.region.x
            )

        async def wait_spinner_turn() -> None:
            frame = reviewing.render().plain[1]
            await wait_for(
                lambda: reviewing.render().plain[1] != frame,
                pilot=pilot,
                description="the review spinner turning",
            )

        steps = (
            (judged_call("first"), 1),
            (judged_call("second"), 2),
            (ApprovalReviewed(request_id="first", approved=True, reason="in scope"), 1),
            (ApprovalCancelled(request_id="second"), 0),
        )
        for event, count in steps:
            await bus.publish(event, raise_handler_errors=True)
            await wait_for(
                lambda count=count: reviewing_shows(count),
                pilot=pilot,
                description=f"the review label for {count} calls left of the badge",
            )
            if count:
                await wait_spinner_turn()
            assert badge.region == badge_region
            assert badge.render().plain == badge_text

        # A mode switch mid-review resizes the badge: the review label moves with it.
        await bus.publish(judged_call("third"), raise_handler_errors=True)
        await wait_for(lambda: reviewing_shows(1), pilot=pilot, description="a call under review again")
        header.approval_mode = ApprovalMode.MANUAL
        await wait_for(
            lambda: (
                badge.render().plain == " APPROVAL MODE: MANUAL "
                and badge.region.width == cell_len(badge.render().plain)
                and reviewing_shows(1)
            ),
            pilot=pilot,
            description="the review label left of the resized badge",
        )
        await wait_spinner_turn()
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="settled main screen")

        assert app.screen is main
        assert not any(isinstance(screen, ApprovalDialog) for screen in app.screen_stack)
        assert chat.region == chat_region
        assert layout_refreshes == []
        assert style_updates == []
        assert footer_recomposes == []
