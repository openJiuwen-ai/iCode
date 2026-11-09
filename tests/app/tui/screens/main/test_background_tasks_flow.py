# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The main screen shows the session's background commands and asks before an action stops them."""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import time
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from textual.app import App
from textual.geometry import Region
from textual.widgets import Button, Static

from chrys.app.tui.screens.dialogs.background_task import BackgroundTaskModal
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.terminal.panel import ShellPanel
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.chrome.status_bar import StatusBar
from chrys.app.tui.widgets.sidebar.background_tasks import BackgroundTaskList
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    BackgroundTasksChanged,
    SessionNew,
    SessionReady,
    SessionRestore,
    ShellToBackground,
)
from chrys.foundation.models.background_tasks import BackgroundTaskView
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled, status_trail
from tests.support.waiting import wait_for

_SESSION = "s1"


def _task(task_id: str = "t1", *, running: bool = True) -> BackgroundTaskView:
    started = time.time() - 30
    return BackgroundTaskView(
        id=task_id,
        command=f"serve {task_id}",
        reason="",
        cwd="/repo",
        trigger="timeout",
        turn=1,
        pids=(4242,),
        log_path="/logs/x.log",
        started_at=started,
        ended_at=None if running else started + 10,
        exit_code=None if running else 0,
    )


async def _ready(bus: EventBus, main: MainScreen, pilot: object, *tasks: BackgroundTaskView) -> None:
    await bus.publish(SessionReady(session_id=_SESSION, agent_profile="Code"), raise_handler_errors=True)
    await bus.publish(BackgroundTasksChanged(session_id=_SESSION, tasks=tasks), raise_handler_errors=True)
    await wait_for(lambda: main.background_tasks_state == tasks, pilot=pilot, description="tasks shown")


def _row_texts(rows: Static) -> list[str]:
    width, height = rows.size
    return [strip.text.rstrip() for strip in rows.render_lines(Region(0, 0, width, height)) if strip.text.strip()]


def _trail(main: MainScreen) -> str:
    return status_trail(main.query_one(StatusBar)._tool_trail)


@pytest.mark.asyncio
async def test_the_current_sessions_tasks_show_in_the_sidebar_and_status_bar(tmp_path: Path) -> None:
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", event_bus=bus)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await _ready(bus, main, pilot, _task("t1"), _task("t2"), _task("t3", running=False))
        assert main.query_one(BackgroundTaskList).tasks == main.background_tasks_state
        assert "2 background tasks" in _trail(main)

        # Another session's list (a stale event) changes nothing.
        await bus.publish(BackgroundTasksChanged(session_id="other", tasks=()), raise_handler_errors=True)
        assert len(main.background_tasks_state) == 3

        await bus.publish(BackgroundTasksChanged(session_id=_SESSION, tasks=(_task("t1"),)), raise_handler_errors=True)
        await wait_for(lambda: "1 background task" in _trail(main), pilot=pilot, description="count updated")

        # The same session made ready again (a rebuild) keeps them.
        await bus.publish(SessionReady(session_id=_SESSION, agent_profile="QA"), raise_handler_errors=True)
        assert [task.id for task in main.background_tasks_state] == ["t1"]

        # The next session starts with none.
        await bus.publish(SessionReady(session_id="s2", agent_profile="Code"), raise_handler_errors=True)
        await wait_for(lambda: main.background_tasks_state == (), pilot=pilot, description="none for s2")
        assert "background" not in _trail(main)

        # Back in the first one, whose commands ended with it: none either.
        await bus.publish(SessionReady(session_id=_SESSION, agent_profile="Code"), raise_handler_errors=True)
        await wait_for(
            lambda: main._state.runtime.main_usage_source_id == _SESSION, pilot=pilot, description="back in s1"
        )
        assert main.background_tasks_state == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("answered", [False, True], ids=["idle", "after-an-answer"])
async def test_a_count_that_changed_in_shell_mode_shows_once_it_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answered: bool
) -> None:
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", event_bus=bus)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await _ready(bus, main, pilot, _task("t1"))
        status = main.query_one(StatusBar)
        if answered:
            status.start_run()
            status.flash_completed()

        def shown() -> str:
            return status_trail(status._flash_display_trail() if answered else status._tool_trail)

        # A count that changes under the run's "completed" flash becomes the flash's own.
        await bus.publish(
            BackgroundTasksChanged(session_id=_SESSION, tasks=(_task("t1"), _task("t2"))), raise_handler_errors=True
        )
        await wait_for(lambda: "2 background tasks" in shown(), pilot=pilot, description="the count")
        assert status.visible
        monkeypatch.setattr(main.query_one(ShellPanel), "_start_shell", lambda _cwd: None)

        main.shell_mode_state = True
        tasks = (_task("t1"), _task("t2"), _task("t3"))
        await bus.publish(BackgroundTasksChanged(session_id=_SESSION, tasks=tasks), raise_handler_errors=True)
        await wait_for(lambda: main.background_tasks_state == tasks, pilot=pilot, description="taken in shell mode")
        main.shell_mode_state = False
        await wait_for(lambda: "3 background tasks" in shown(), pilot=pilot, description="the new count")
        assert status.visible


@pytest.mark.asyncio
async def test_a_command_ending_as_it_moves_to_the_background_updates_the_shown_list(tmp_path: Path) -> None:
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", event_bus=bus)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await _ready(bus, main, pilot, _task("t1"))
        sidebar = main.query_one(SidebarPanel)
        if not sidebar.display:
            main.action_toggle_sidebar()
        sidebar.focus_tab("tab-tasks")
        await wait_for(
            lambda: is_widget_shown_on_active_screen(main.query_one(BackgroundTaskList)),
            pilot=pilot,
            description="the list shown",
        )
        loop = asyncio.get_running_loop()

        async def publish(*tasks: BackgroundTaskView) -> None:
            # As the registry does: from a task with an empty context, outside the app's.
            event = BackgroundTasksChanged(session_id=_SESSION, tasks=tasks)
            await loop.create_task(bus.publish(event, raise_handler_errors=True), context=contextvars.Context())

        # Its end arrives before the list laid out the row its start added.
        await publish(_task("t1"), _task("t2"))
        await publish(_task("t1"), _task("t2", running=False))
        await wait_for(lambda: len(main.background_tasks_state) == 2, pilot=pilot, description="the end shown")
        await wait_for(lambda: "1 background task" in _trail(main), pilot=pilot, description="the count")


@pytest.mark.asyncio
async def test_ctrl_t_moves_running_shells_unless_the_background_is_full(tmp_path: Path) -> None:
    bus = EventBus()
    moves: list[ShellToBackground] = []

    async def record(event: ShellToBackground) -> None:
        moves.append(event)

    await bus.subscribe(ShellToBackground, record)
    app = make_chrys_app(tmp_path / "sessions", event_bus=bus)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await _ready(bus, main, pilot, _task())
        await pilot.press("ctrl+t")
        assert moves == []  # Only while the agent runs.

        main._state.run.agent_running = True
        await pilot.press("ctrl+t")
        await wait_for(lambda: len(moves) == 1, pilot=pilot, description="moved to the background")

        await bus.publish(BackgroundTasksChanged(session_id=_SESSION, tasks=(_task(),), full=True))
        notified: list[str] = []
        app.notify = lambda message, **_kwargs: notified.append(str(message))  # type: ignore[method-assign]
        await pilot.press("ctrl+t")
        await wait_for(lambda: notified, pilot=pilot, description="the full notice")
        assert len(moves) == 1


@pytest.mark.asyncio
async def test_a_new_session_asks_before_stopping_running_tasks(tmp_path: Path) -> None:
    bus = EventBus()
    news: list[SessionNew] = []

    async def record(event: SessionNew) -> None:
        news.append(event)

    await bus.subscribe(SessionNew, record)
    app = make_chrys_app(tmp_path / "sessions", settings=Settings(locale="zh-Hans"), event_bus=bus)
    # As many as can run, each with its reason, in a short terminal: the list scrolls, the buttons stay.
    running = [dataclasses.replace(_task(f"t{index}"), reason="keeps the server up") for index in range(1, 9)]
    async with app.run_test(size=(100, 24)) as pilot:
        main = app._main_screen
        assert main is not None
        await _ready(bus, main, pilot, *running, _task("t9", running=False))

        main.post_message(InputBar.NewSessionRequested())
        await wait_for(
            lambda: (
                isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted and screen_is_settled(app, app.screen)
            ),
            pilot=pilot,
            description="all background task previews laid out",
        )
        message = "\n".join(str(row.content) for row in app.screen.query(Static).filter(".task-command"))
        assert "serve t1" in message
        assert "serve t8" in message
        assert "serve t9" not in message
        assert str(app.screen.query_one("#confirm-yes", Button).label) == "停止并继续"
        assert str(app.screen.query_one("#confirm-no", Button).label) == "取消"
        await click_when_settled(pilot, "#confirm-no")
        await wait_for(lambda: app.screen is main, pilot=pilot, description="back to chat")
        assert news == []

        main.post_message(InputBar.NewSessionRequested())
        await wait_for(lambda: isinstance(app.screen, ConfirmDialog), pilot=pilot, description="asked again")
        await click_when_settled(pilot, "#confirm-yes")
        await wait_for(lambda: len(news) == 1, pilot=pilot, description="the new session")


@pytest.mark.asyncio
async def test_restoring_another_session_asks_but_reopening_this_one_does_not(tmp_path: Path) -> None:
    bus = EventBus()
    restores: list[SessionRestore] = []

    async def record(event: SessionRestore) -> None:
        restores.append(event)

    await bus.subscribe(SessionRestore, record)
    app = make_chrys_app(tmp_path / "sessions", event_bus=bus)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await _ready(bus, main, pilot, _task())

        main.run_worker(main._sessions.do_session_restore("other"), exit_on_error=False)
        await wait_for(lambda: isinstance(app.screen, ConfirmDialog), pilot=pilot, description="the confirmation")
        await click_when_settled(pilot, "#confirm-no")
        await wait_for(lambda: app.screen is main, pilot=pilot, description="back to chat")
        assert restores == []

        # Its own commands keep running through a restore of the current session.
        main.run_worker(main._sessions.do_session_restore(_SESSION), exit_on_error=False)
        await wait_for(lambda: len(restores) == 1, pilot=pilot, description="restored without asking")
        assert restores[0].session_id == _SESSION
        assert not isinstance(app.screen, ConfirmDialog)


@pytest.mark.asyncio
async def test_quit_asks_while_tasks_run_and_a_second_quit_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", event_bus=bus)
    exits: list[object] = []
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await _ready(bus, main, pilot, _task())
        monkeypatch.setattr(app, "exit", lambda *args, **_kwargs: exits.append(args))

        await pilot.press("ctrl+q")
        await wait_for(
            lambda: (
                isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted and screen_is_settled(app, app.screen)
            ),
            pilot=pilot,
            description="quit confirmation controls laid out",
        )
        assert str(app.screen.query_one("#confirm-yes", Button).label) == "Stop and exit"
        await click_when_settled(pilot, "#confirm-no")
        await wait_for(lambda: app.screen is main, pilot=pilot, description="back to chat")
        assert exits == []

        await pilot.press("ctrl+q")
        await wait_for(lambda: isinstance(app.screen, ConfirmDialog), pilot=pilot, description="asked again")
        await pilot.press("ctrl+q")
        await wait_for(lambda: exits, pilot=pilot, description="quit at once")


async def _settle(app: App[None], screen: MainScreen) -> None:
    await wait_for(
        lambda: screen_is_settled(app, screen) and not (screen._repaint_required or screen._dirty_widgets),
        description="main screen settled with its queued repaints flushed",
    )


@pytest.mark.asyncio
async def test_the_list_ticks_and_its_details_open_without_restyling_a_populated_main_screen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", event_bus=bus)
    async with app.run_test(size=(140, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        for index in range(12):
            await panel.add_user_message(f"Question {index}\nwith a second line")
            await panel.add_agent_message(f"## Answer {index}\n\nA paragraph with `code` and **emphasis**.")
        await _ready(bus, main, pilot, _task("t1"), _task("t2"))
        sidebar = main.query_one(SidebarPanel)
        if not sidebar.display:
            main.action_toggle_sidebar()
        sidebar.focus_tab("tab-tasks")
        background = main.query_one(BackgroundTaskList)
        timer = background._timer
        assert timer is not None
        await wait_for(
            lambda: is_widget_shown_on_active_screen(background) and timer._active.is_set(),
            pilot=pilot,
            description="the list shown and ticking",
        )
        await _settle(app, main)

        layout = create_autospec(main._refresh_layout, side_effect=main._refresh_layout)
        monkeypatch.setattr(main, "_refresh_layout", layout)
        styles: list[bool] = []
        monkeypatch.setattr(main, "update_node_styles", lambda animate=True: styles.append(animate))
        recomposes: list[None] = []

        async def record_recompose() -> None:
            recomposes.append(None)

        monkeypatch.setattr(main.query_one(ChrysFooter), "recompose", record_recompose)

        rows = background.query_one("#background-tasks-rows", Static)
        for _ in range(2):
            painted = rows.content
            await wait_for(lambda painted=painted: rows.content is not painted, description="a tick repainted")
        stopping = dataclasses.replace(_task("t2"), stopped_by="user")
        await bus.publish(BackgroundTasksChanged(session_id=_SESSION, tasks=(_task("t1"), stopping)))
        await wait_for(lambda: "stopping" in _row_texts(rows)[1], description="the stopping row")
        # It ends: "Clear ended" comes onto the box's bottom border without laying the screen out.
        ended = dataclasses.replace(_task("t2", running=False), stopped_by="user")
        await bus.publish(BackgroundTasksChanged(session_id=_SESSION, tasks=(_task("t1"), ended)))
        await wait_for(lambda: _row_texts(rows)[1].startswith("■"), description="the stopped row")
        width, height = background.outer_size
        await wait_for(
            lambda: background.render_lines(Region(0, height - 1, width, 1))[0].text.endswith(" Clear ended ─╯"),
            description="clear ended on the bottom border",
        )

        await click_when_settled(pilot, rows, offset=(2, 0))
        await wait_for(
            lambda: isinstance(app.screen, BackgroundTaskModal) and app.screen.is_mounted, description="details"
        )
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, description="back to chat")
        await _settle(app, main)

        layout.assert_not_called()
        assert styles == []
        assert recomposes == []
