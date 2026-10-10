# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the session title on the chat border and the terminal tab."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from chrys.app.tui.screens.dialogs.session_title import SessionTitleDialog
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.screens.main.session_title import SessionTitleController
from chrys.app.tui.screens.main.state import MainScreenState, RunState
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.foundation.config.settings import Settings
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for


class _Driver:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.writes.append(data)


class _App:
    def __init__(self) -> None:
        self._driver = _Driver()

    @property
    def titles(self) -> list[str]:
        """The titles written, decoded from their OSC 0 + OSC 2 pairs."""
        titles: list[str] = []
        for write in self._driver.writes:
            osc0, _sep, _osc2 = write.partition("\x07")
            titles.append(osc0.removeprefix("\x1b]0;"))
        return titles


class _Timer:
    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _Harness:
    def __init__(self, *, cwd: str = "/repo", session_id: str = "session-1") -> None:
        self.run = RunState()
        self.app = _App()
        self.cwd = cwd
        self.session_id = session_id
        self.border_titles: list[str] = []
        self.timers: list[_Timer] = []
        self.pushed: list[tuple[object, Callable[[str | None], None]]] = []
        self.saves: list[tuple[str, str]] = []
        self.controller = SessionTitleController(
            run=self.run,
            app=lambda: self.app,
            workspace_cwd=lambda: self.cwd,
            show_display_title=self.border_titles.append,
            set_interval=self._set_interval,
            current_session_id=lambda: self.session_id,
            push_screen=lambda screen, callback: self.pushed.append((screen, callback)),
            start_custom_title_save=lambda title, session_id: self.saves.append((title, session_id)),
            session_dir=lambda session_id: Path("/sessions") / session_id,
        )

    def _set_interval(self, _interval: float, callback: Callable[[], None]) -> _Timer:
        timer = _Timer(callback)
        self.timers.append(timer)
        return timer


@pytest.fixture(autouse=True)
def _native_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)


def test_display_title_prefers_custom_then_generated_then_fallback() -> None:
    harness = _Harness()
    controller = harness.controller

    controller.set_session_title_state(fallback="First prompt")
    controller.set_session_title_state(generated="Summary")
    controller.set_session_title_state(custom="Pinned")
    controller.set_session_title_state(custom="")

    assert harness.border_titles == ["First prompt", "Summary", "Pinned", "Summary"]
    assert controller.custom_title == ""
    assert controller.display_title == "Summary"


def test_reset_clears_every_title_and_the_result_mark(tmp_path: Path) -> None:
    harness = _Harness(cwd=str(tmp_path))
    controller = harness.controller
    controller.set_session_title_state(custom="Pinned", generated="Summary", fallback="First")
    controller.mark_terminal_title_completed()

    controller.reset_session_title_state()

    assert controller.display_title == ""
    assert harness.border_titles[-1] == ""
    assert harness.app.titles[-1] == str(tmp_path)


def test_cwd_title_yields_to_pinned_session_title(tmp_path: Path) -> None:
    """cwd updates (workspace changes, restores) must not unpin a session title."""
    pinned = _Harness()
    pinned.controller.set_session_title_state(generated="Login bug fix")
    pinned.app._driver.writes.clear()

    pinned.controller.set_terminal_title_for_cwd(str(tmp_path))

    assert pinned.app.titles == ["Login bug fix"]

    unpinned = _Harness()
    unpinned.run.agent_running = True

    unpinned.controller.set_terminal_title_for_cwd(str(tmp_path))

    assert unpinned.app.titles == [f"◇ {tmp_path}"]


def test_unseeded_cwd_title_falls_back_to_workspace_cwd(tmp_path: Path) -> None:
    harness = _Harness(cwd=str(tmp_path))

    harness.controller.clear_terminal_title_result()
    harness.controller.mark_terminal_title_completed()

    assert harness.app.titles == [f"✓ {tmp_path}"]


def test_first_prompt_seeds_the_fallback_title_and_previews_on_the_tab() -> None:
    harness = _Harness()

    harness.controller.set_terminal_title_for_user_message("  fix \n the  login bug ")
    harness.controller.set_terminal_title_for_user_message("second prompt")

    assert harness.border_titles == ["fix the login bug"]
    assert harness.app.titles[-1] == "second prompt"


def test_custom_title_keeps_the_tab_pinned_over_prompt_previews() -> None:
    harness = _Harness()
    harness.controller.set_session_title_state(custom="Pinned")

    harness.controller.set_terminal_title_for_user_message("a prompt")

    assert harness.app.titles[-1] == "Pinned"


def test_running_animation_preserves_current_prompt_preview() -> None:
    harness = _Harness()
    harness.controller.set_session_title_state(fallback="First task", generated="Old generated title")
    harness.app._driver.writes.clear()

    harness.controller.set_terminal_title_for_user_message("Second task")
    harness.run.agent_running = True
    harness.controller.run_started()
    harness.controller.sync_activity()
    harness.timers[0].callback()

    assert harness.app.titles == ["Second task", "◇ Second task", "◈ Second task"]


def test_running_title_cycles_activity_frames_and_settles_on_the_result_mark() -> None:
    harness = _Harness()
    harness.controller.set_session_title_state(custom="Login bug fix")
    harness.app._driver.writes.clear()

    harness.run.agent_running = True
    harness.controller.run_started()
    harness.controller.sync_activity()
    timer = harness.timers[0]
    for _ in range(4):
        timer.callback()
    harness.controller.mark_terminal_title_completed()
    harness.run.agent_running = False
    harness.controller.sync_activity()

    assert harness.app.titles == [
        "◇ Login bug fix",
        "◈ Login bug fix",
        "◆ Login bug fix",
        "◈ Login bug fix",
        "◇ Login bug fix",
        "✓ Login bug fix",
    ]
    assert timer.stopped is True
    assert len(harness.timers) == 1

    harness.run.agent_running = True
    harness.controller.sync_activity()
    harness.run.agent_running = False
    harness.controller.sync_activity()

    assert [timer.stopped for timer in harness.timers] == [True, True]

    harness.controller.mark_terminal_title_failed()
    harness.controller.clear_terminal_title_result()
    harness.controller.clear_terminal_title_result()

    assert harness.app.titles[-2:] == ["✗ Login bug fix", "Login bug fix"]


def test_a_new_run_drops_the_last_result_mark() -> None:
    harness = _Harness()
    harness.controller.set_session_title_state(custom="Task")
    harness.controller.mark_terminal_title_failed()

    harness.run.agent_running = True
    harness.controller.run_started()
    harness.controller.sync_activity()
    harness.run.agent_running = False
    harness.controller.sync_activity()

    assert harness.app.titles[-2:] == ["◇ Task", "Task"]


def test_activity_tick_after_the_run_stops_is_ignored() -> None:
    harness = _Harness()
    harness.run.agent_running = True
    harness.controller.sync_activity()
    timer = harness.timers[0]
    harness.run.agent_running = False
    writes = len(harness.app.titles)

    timer.callback()

    assert len(harness.app.titles) == writes


def test_editor_saves_against_the_session_it_was_opened_for() -> None:
    harness = _Harness(session_id="session-a")
    harness.controller.set_session_title_state(custom="Pinned", generated="Summary", fallback="First prompt")

    harness.controller.open_editor()
    harness.session_id = "session-b"
    dialog, on_result = harness.pushed[0]
    on_result("Renamed")
    on_result(None)

    assert isinstance(dialog, SessionTitleDialog)
    assert (dialog._custom_title, dialog._auto_title) == ("Pinned", "Summary")
    assert (dialog._session_id, dialog._session_folder) == ("sessiona", Path("/sessions/session-a"))
    assert harness.saves == [("Renamed", "session-a")]


def test_editor_and_rename_need_a_session() -> None:
    harness = _Harness(session_id="")

    harness.controller.open_editor()
    harness.controller.apply_custom_title("Renamed")

    assert harness.pushed == []
    assert harness.saves == []


def test_rename_command_saves_against_the_current_session() -> None:
    harness = _Harness(session_id="session-a")

    harness.controller.apply_custom_title("Renamed")

    assert harness.saves == [("Renamed", "session-a")]


def test_new_clear_and_restored_sessions_clear_terminal_title_result() -> None:
    cleared: list[str] = []
    screen = SimpleNamespace(
        _state=MainScreenState(),
        _session_title=SimpleNamespace(clear_terminal_title_result=lambda: cleared.append("clear")),
    )

    # Both /new and confirmed /clear enter the shared creating-new-session path.
    MainScreen._set_creating_new_session(screen, True)
    MainScreen._set_restoring_session(screen, True)
    MainScreen._set_creating_new_session(screen, False)
    MainScreen._set_restoring_session(screen, False)

    assert cleared == ["clear", "clear"]
    assert screen._state.session.creating_new_session is False
    assert screen._state.session.restoring_session is False


@pytest.mark.asyncio
async def test_border_click_opens_the_editor_on_the_store_folder_of_the_full_session_id(tmp_path: Path) -> None:
    session_id = "e9796bf5-2747-4a1b-9c3d-5e6f7a8b9c0d"
    app = make_chrys_app(tmp_path, settings=Settings(locale="en"))
    async with app.run_test(size=(120, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        main.chat_session_id = session_id
        chat_panel = main.query_one(ChatPanel)
        await wait_for(lambda: chat_panel.session_id == session_id, pilot=pilot, description="session id bound")

        await click_when_settled(pilot, chat_panel, offset=(4, 0))
        await wait_for(
            lambda: isinstance(app.screen, SessionTitleDialog) and app.screen.is_mounted,
            pilot=pilot,
            description="session title dialog open",
        )

        dialog = app.screen
        assert isinstance(dialog, SessionTitleDialog)
        assert dialog._session_id == "e9796bf52747"
        assert dialog._session_folder == app._state_store.session_dir(session_id)
        assert dialog._session_folder.name == "e9796bf52747"
