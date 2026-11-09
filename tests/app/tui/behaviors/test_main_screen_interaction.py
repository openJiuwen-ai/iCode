# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for main-screen key routing and shell mode inside the real app shell."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import Button, TextArea

from chrys.app.tui.app import ChrysApp
from chrys.foundation.events.bus import EventBus
from chrys.service.state.store import JsonFileStateStore
from tests.support.tui_app_harness import (
    EmptyAgentRegistry,
    SessionGenerationEngine,
    ShutdownOnlyEngine,
    make_chrys_app,
)
from tests.support.waiting import wait_for


async def test_escape_collapses_suggestion_popup_before_interrupt_and_exit_prompts(tmp_path: Path) -> None:
    """Esc on an open /, @, # popup collapses it (text kept) — no confirm modal."""
    from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
    from chrys.app.tui.widgets.chrome.input_bar import InputBar
    from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList

    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine())

    async with app.run_test() as pilot:
        assert app._main_screen is not None
        main_screen = app._main_screen
        suggestions = main_screen.query_one(SuggestionList)
        input_bar = main_screen.query_one(InputBar)
        input_bar.focus_input()
        await wait_for(
            lambda: input_bar.query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )
        await pilot.pause()

        await pilot.press("/")
        await pilot.pause()
        assert suggestions.is_visible is True
        assert input_bar.suggestions_active is True

        await pilot.press("escape")
        await pilot.pause()
        assert suggestions.is_visible is False
        assert input_bar.suggestions_active is False
        assert input_bar.query_one("#chat-input", TextArea).text == "/"
        assert app.screen is main_screen  # no interrupt/exit confirm pushed

        # With the popup gone and a run active, Esc prompts Interrupt again.
        main_screen._set_agent_running(True)
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmDialog)


async def test_main_screen_chat_scroll_keys_work_with_input_and_chat_focus(tmp_path: Path) -> None:
    """The transcript should keep ScrollView keyboard scrolling even with chat-input focus."""
    from chrys.app.tui.screens.main.screen import MainScreen
    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from chrys.app.tui.widgets.chrome.input_bar import InputBar

    app = ChrysApp(
        EventBus(),
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        state_store=JsonFileStateStore(tmp_path),
        agent_registry=EmptyAgentRegistry(),  # type: ignore[arg-type]
    )

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        screen = next(screen for screen in app.screen_stack if isinstance(screen, MainScreen))
        panel = screen.query_one(ChatPanel)
        input_bar = screen.query_one(InputBar)

        for index in range(30):
            await panel.add_user_message(f"chat line {index}\n" * 3)
        await pilot.pause()
        assert panel.max_scroll_y > 0

        panel.scroll_end(animate=False)
        input_bar.focus_input()
        await wait_for(
            lambda: input_bar.query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )
        await pilot.pause()
        assert input_bar.query_one(TextArea).has_focus

        bottom = panel.scroll_y
        await pilot.press("pageup")
        await pilot.pause()
        assert panel.scroll_y < bottom

        page_up_position = panel.scroll_y
        await pilot.press("pagedown")
        await pilot.pause()
        assert panel.scroll_y > page_up_position

        panel.scroll_end(animate=False)
        panel.focus()
        await wait_for(lambda: panel.has_focus, pilot=pilot, description="chat panel focus before scrolling")
        await pilot.pause()
        assert panel.has_focus

        bottom = panel.scroll_y
        await pilot.press("up")
        await pilot.wait_for_scheduled_animations()
        assert panel.scroll_y < bottom

        up_position = panel.scroll_y
        await pilot.press("down")
        await pilot.wait_for_scheduled_animations()
        assert panel.scroll_y > up_position


async def test_main_screen_chat_page_keys_disabled_for_trajectory_dashboard() -> None:
    from chrys.app.tui.screens.main.screen import MainScreen

    screen = MainScreen(EventBus(), engine_provider=None)
    screen._dashboard_visible = lambda: True  # type: ignore[method-assign]

    assert screen.check_action("chat_page_up", ()) is False
    assert screen.check_action("chat_page_down", ()) is False
    assert screen.check_action("chat_scroll_bottom", ()) is False


async def test_startup_enter_spam_does_not_send_or_show_input_loading(tmp_path) -> None:
    """Rapid Enter during startup should leave the empty composer visually idle."""

    from chrys.app.tui.screens.main.screen import MainScreen
    from chrys.app.tui.widgets.chrome.input_bar import InputBar
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import AgentLoadStarted, SessionReady, UserMessage
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.schema import ModelProfile

    profile = AgentProfile(name="Code", display_name="Code", description="Test profile")

    class _Registry:
        def list_profiles(self) -> list[AgentProfile]:
            return [profile]

        def load_all(self) -> None:
            return

        def get(self, name: str) -> AgentProfile | None:
            return profile if name == profile.name else None

    class _Engine(SessionGenerationEngine):
        def __init__(self, bus: EventBus) -> None:
            self._bus = bus
            self.session_generation = 0
            self.load_started = asyncio.Event()
            self.release = asyncio.Event()
            self.ready = asyncio.Event()

        async def start(self, _profile: AgentProfile) -> None:
            await self._bus.publish(
                AgentLoadStarted(operation="startup", to_profile=profile.name, to_display_name=profile.display_name)
            )
            self.load_started.set()
            await self.release.wait()
            await self._bus.publish(
                SessionReady(agent_profile=profile.name, display_name=profile.display_name, session_id="test-session")
            )
            self.ready.set()

        async def shutdown(self) -> None:
            self.release.set()

    bus = EventBus()
    messages: list[str] = []

    async def _record_message(event: UserMessage) -> None:
        messages.append(event.text)

    await bus.subscribe(UserMessage, _record_message)
    engine = _Engine(bus)
    # A selectable model profile keeps the unconfigured-model send guard out
    # of this test's way — its subject is composer state during startup.
    model_registry = ModelProfileRegistry()
    model_registry.register(ModelProfile(id="model-profile", name="Configured Model", model_id="test-model"))
    app = make_chrys_app(
        tmp_path,
        engine=engine,
        event_bus=bus,
        agent_registry=_Registry(),
        model_registry=model_registry,
        gc_freeze_enabled=None,
    )

    async with app.run_test() as pilot:
        await wait_for(engine.load_started.is_set, pilot=pilot, description="the agent started loading")
        await pilot.pause()
        main_screen = next(screen for screen in pilot.app.screen_stack if isinstance(screen, MainScreen))
        ib = main_screen.query_one(InputBar)
        send_btn = ib.query_one("#send-btn", Button)
        text_area = ib.query_one("#chat-input", TextArea)

        assert send_btn.disabled is True
        assert str(send_btn.label) == "Send"
        assert text_area.read_only is False

        for _ in range(5):
            await pilot.press("enter")
        await pilot.pause()

        assert messages == []
        assert send_btn.disabled is True
        assert str(send_btn.label) == "Send"
        assert text_area.read_only is False

        ib.value = "draft while loading"
        await ib.action_submit()
        await pilot.pause()

        assert messages == []
        assert ib.value == "draft while loading"
        assert send_btn.disabled is True
        assert text_area.read_only is False

        engine.release.set()
        await wait_for(engine.ready.is_set, pilot=pilot, description="the session is ready")
        await pilot.pause()

        assert messages == []
        assert send_btn.disabled is False
        assert str(send_btn.label) == "Send"
        assert text_area.read_only is False

        await pilot.press("enter")
        await pilot.pause()

        assert messages == ["draft while loading"]


async def test_shell_mode_temporarily_replaces_trajectory_session_data_view(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Shell mode must be the sole main pane and restore the Session Data tab on exit."""
    from chrys.app.tui.terminal.panel import ShellPanel
    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from chrys.app.tui.widgets.chat.session_json import SessionJsonPanel

    app = make_chrys_app(tmp_path)

    async with app.run_test(size=(120, 32)) as pilot:
        chat = app.screen.query_one(ChatPanel)
        session_json = app.screen.query_one(SessionJsonPanel)
        shell = app.screen.query_one(ShellPanel)
        monkeypatch.setattr(shell, "_start_shell", lambda _cwd: None)

        async def wait_for_shell_state(expected: bool) -> None:
            await wait_for(
                lambda: app.screen.shell_mode_state is expected,
                pilot=pilot,
                description="shell mode reaches the requested state",
            )

        await pilot.press("f12")
        await pilot.pause()
        await pilot.click("#session-data")
        await pilot.pause()
        assert session_json.display is True
        assert chat.display is False
        retained_status = session_json._status
        retained_generation = session_json._content_generation
        assert retained_status == "No active session."

        app.screen.shell_mode_state = True
        await wait_for_shell_state(True)
        assert app.screen.shell_mode_state is True
        assert shell.display is True
        assert session_json.display is False
        assert session_json._shell_mode_suspended is True
        assert chat.display is False
        assert session_json._status == retained_status
        assert session_json._content_generation == retained_generation

        app.screen.shell_mode_state = False
        await wait_for_shell_state(False)
        assert shell.display is False
        assert session_json.display is True
        assert session_json._shell_mode_suspended is False
        assert chat.display is False
        assert session_json._status == retained_status
        assert session_json._content_generation == retained_generation


async def test_shell_mode_sidebar_tab_strip_keeps_focus_for_keyboard_navigation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Shell mode lets sidebar focus stay, so a clicked tab strip takes the arrow keys until the shell exits."""
    from textual.widgets import TabbedContent, Tabs

    from chrys.app.tui.terminal.panel import ShellPanel
    from chrys.app.tui.terminal.widget import Terminal
    from chrys.app.tui.widgets.chrome.input_bar import InputBar
    from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
    from tests.support.tui_helpers import click_when_settled

    app = make_chrys_app(tmp_path)

    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        shell = main.query_one(ShellPanel)
        monkeypatch.setattr(shell, "_start_shell", lambda _cwd: None)
        tabs = main.query_one(SidebarPanel).query_one(TabbedContent)
        strip = tabs.query_one(Tabs)
        chat_input = main.query_one(InputBar).query_one("#chat-input")

        main.shell_mode_state = True
        terminal = shell.query_one(Terminal)
        await wait_for(lambda: main.focused is terminal, pilot=pilot, description="the terminal owns focus")

        await click_when_settled(pilot, tabs.get_tab("tab-tasks"))
        await wait_for(
            lambda: tabs.active == "tab-tasks" and main.focused is strip,
            pilot=pilot,
            description="the clicked tab strip keeps focus",
        )
        await pilot.press("left")
        await wait_for(lambda: tabs.active == "tab-toc", pilot=pilot, description="Left selects the previous tab")
        assert main.focused is strip

        main.shell_mode_state = False
        await wait_for(
            lambda: main.focused is chat_input, pilot=pilot, description="leaving shell mode hands focus to the input"
        )
        assert not strip.can_focus


async def test_shell_mode_transitions_never_paint_half_applied_layout(tmp_path: Path) -> None:
    """Entering/exiting shell mode must never flush a mid-transition frame.

    The status-bar face flip inside enter/exit_shell_mode rebuilds the
    compositor map synchronously; a widget repaint flushed before the screen's
    deferred relayout paints from that map. If the flip runs before the
    display/class changes are complete, the sidebar flashes alone at the left
    edge for one frame.
    """

    from chrys.app.tui.widgets.chrome.status_bar import StatusBar

    app = make_chrys_app(tmp_path)

    frames: list[list[str]] = []

    async with app.run_test(size=(120, 32)) as pilot:
        await pilot.pause()
        await pilot.pause()

        orig_display = app._display

        def spy_display(screen_arg, renderable) -> None:
            try:
                frames.append([strip.text for strip in screen_arg._compositor.render_strips()])
            except Exception:
                frames.append([])
            orig_display(screen_arg, renderable)

        app._display = spy_display  # type: ignore[method-assign]
        try:
            enter_frame_count = len(frames)
            await pilot.press("!")
            entered_refresh = asyncio.Event()
            assert app.screen.call_after_refresh(entered_refresh.set)
            await wait_for(
                lambda: entered_refresh.is_set() and len(frames) > enter_frame_count,
                pilot=pilot,
                description="shell mode transition completes its refresh",
            )
            assert app.screen.shell_mode_state is True
            assert app.screen.query_one(StatusBar).query_one(".status-selectors").display is False

            exit_frame_count = len(frames)
            app.screen.shell_mode_state = False
            exited_refresh = asyncio.Event()
            assert app.screen.call_after_refresh(exited_refresh.set)
            await wait_for(
                lambda: exited_refresh.is_set() and len(frames) > exit_frame_count,
                pilot=pilot,
                description="leaving shell mode completes its refresh",
            )
            assert app.screen.shell_mode_state is False
            assert app.screen.query_one(StatusBar).query_one(".status-selectors").display is False
        finally:
            del app._display

    assert frames
    for index, frame in enumerate(frames):
        tab_columns = [row.find("Tasks") for row in frame if "Tasks" in row]
        assert tab_columns, f"sidebar tabs missing from painted frame {index}"
        assert min(tab_columns) > 60, f"frame {index} painted the sidebar in the left half (x={min(tab_columns)})"
