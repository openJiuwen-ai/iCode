# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lifecycle tests for main-screen cleanup delegation."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable
from functools import partial
from types import SimpleNamespace
from typing import cast

import pytest
from textual.app import App, ComposeResult
from textual.worker import Worker, WorkerCancelled

from chrys.app.tui.screens.main.diff_controller import LiveDiffTracker
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.app.tui.screens.main.tool_action_bridge import ToolActionBridge
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.ask_user import AskUserAnswer
from tests.support.tui_helpers import make_live_mutation, stale_file_cache
from tests.support.waiting import wait_for


def test_unmount_shuts_down_buddy_before_flushing_notifications() -> None:
    calls: list[str] = []

    class _Subscriptions:
        async def unsubscribe_all(self) -> None:
            calls.append("unsubscribe")

    class _BuddyCommand:
        async def shutdown(self) -> None:
            calls.append("buddy_shutdown")

    async def flush_notifications() -> None:
        calls.append("flush_notifications")

    async def close_workflow() -> None:
        calls.append("workflow_close")

    async def close_workspace_branch() -> None:
        calls.append("workspace_branch_close")

    screen = SimpleNamespace(
        _events=SimpleNamespace(close_approval_waits=lambda: calls.append("approval_waits_close")),
        _workflow_timer=None,
        _workflow=SimpleNamespace(close=close_workflow),
        _subscriptions=_Subscriptions(),
        _suggestions=SimpleNamespace(buddy_command=_BuddyCommand()),
        _session_title=SimpleNamespace(stop_activity=lambda: calls.append("session_title_stop")),
        _workspace_branch=SimpleNamespace(close=close_workspace_branch),
        _flush_settings_save=flush_notifications,
        _locale_controller=None,
    )

    asyncio.run(MainScreen.on_unmount(screen))

    assert calls == [
        "approval_waits_close",
        "workflow_close",
        "unsubscribe",
        "session_title_stop",
        "workspace_branch_close",
        "buddy_shutdown",
        "flush_notifications",
    ]


# ──────────── _set_agent_running file-cache invalidation ───────────────


def _make_screen_for_running_toggle() -> SimpleNamespace:
    """Mock screen with the attributes ``_set_agent_running`` reads/writes."""
    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from chrys.app.tui.widgets.chrome.input_bar import InputBar

    input_bar = SimpleNamespace(
        agent_running=False,
        locked=False,
        unlock_and_keep=lambda: None,
    )
    chat_panel = SimpleNamespace(agent_running=False)
    engine = SimpleNamespace(session_generation=1)
    workspace_actions = SimpleNamespace(after_turn_checks=0)

    def check_after_turn() -> None:
        workspace_actions.after_turn_checks += 1

    workspace_actions.check_after_turn = check_after_turn

    def query_one(cls):
        if cls is InputBar:
            return input_bar
        if cls is ChatPanel:
            return chat_panel
        raise AssertionError(f"unexpected query_one({cls})")

    return SimpleNamespace(
        _state=MainScreenState(),
        _session_title=SimpleNamespace(run_started=lambda: None, sync_activity=lambda: None),
        _live_diff=LiveDiffTracker(),
        _suggestions=SimpleNamespace(file_cache=None),
        _workflow=SimpleNamespace(workflow_mode=False),
        _workspace_actions=workspace_actions,
        _navigation=SimpleNamespace(dismiss_interrupt_confirm=lambda: None),
        _services=MainScreenServices(bus=EventBus(), engine_provider=lambda: engine),
        _view_adapter=SimpleNamespace(current_chat_session_id=lambda: "session-1"),
        query_one=query_one,
        refresh_bindings=lambda: None,
    )


def test_set_agent_running_false_invalidates_file_cache() -> None:
    """When the agent stops, the ``@`` file cache must be dropped.

    Agent tool calls (``write_file``/``edit_file``/shell) can create or
    delete files during a turn; without invalidation the next ``@``
    trigger would show a stale list.  ``_set_agent_running`` is the
    single chokepoint for all stop transitions (normal completion,
    error, user interrupt).
    """
    from chrys.app.tui.screens.main.screen import MainScreen

    screen = _make_screen_for_running_toggle()
    screen._suggestions.file_cache = stale_file_cache("src/a.py", "src/b.py")  # prior @ scan
    screen._state.run.agent_running = True

    MainScreen._set_agent_running(screen, False)

    assert screen._suggestions.file_cache is None
    assert screen._state.run.agent_running is False


@pytest.mark.parametrize("stopped_by", ["screen", "backend_handler"])
def test_a_stopped_run_checks_the_working_folder_once(stopped_by: str) -> None:
    from chrys.app.tui.screens.main.screen import MainScreen

    screen = _make_screen_for_running_toggle()
    MainScreen._set_agent_running(screen, True)
    assert screen._workspace_actions.after_turn_checks == 0
    if stopped_by == "backend_handler":
        # ``BackendEventHandler.set_agent_running`` clears the shared flag before it calls the screen.
        screen._state.run.agent_running = False

    MainScreen._set_agent_running(screen, False)
    assert screen._workspace_actions.after_turn_checks == 1

    MainScreen._set_agent_running(screen, False)
    assert screen._workspace_actions.after_turn_checks == 1


def test_set_agent_running_true_preserves_file_cache() -> None:
    """Cache is invalidated only on stop — starting a turn keeps it intact.

    The cache is per-turn staleness: we don't want to rebuild on every
    user prompt, only after the agent has had a chance to mutate the
    filesystem.
    """
    from chrys.app.tui.screens.main.screen import MainScreen

    screen = _make_screen_for_running_toggle()
    cached = stale_file_cache("src/a.py", "src/b.py")
    screen._suggestions.file_cache = cached

    MainScreen._set_agent_running(screen, True)

    assert screen._suggestions.file_cache is cached
    assert screen._state.run.agent_running is True


def test_running_generation_changes_only_when_a_new_turn_starts() -> None:
    from chrys.app.tui.screens.main.screen import MainScreen

    screen = _make_screen_for_running_toggle()
    MainScreen._set_agent_running(screen, True)
    assert screen._state.run.generation == 1
    screen._live_diff.file_mutations["/repo/live.py"] = make_live_mutation("before", "after", "modify")
    cached = stale_file_cache("src/warm.py")
    screen._suggestions.file_cache = cached

    MainScreen._set_agent_running(screen, True)
    assert screen._state.run.generation == 1
    assert "/repo/live.py" in screen._live_diff.file_mutations
    assert screen._suggestions.file_cache is cached

    MainScreen._set_agent_running(screen, False)
    MainScreen._set_agent_running(screen, True)
    assert screen._state.run.generation == 2
    assert screen._live_diff.file_mutations == {}


def test_inline_ask_user_submit_publishes_response() -> None:
    from chrys.app.tui.widgets.chat.renderers.ask_user import AskUserInlineSubmitted
    from chrys.foundation.events.types import AskUserResponse

    bus = EventBus()
    responses: list[AskUserResponse] = []
    screen = object.__new__(MainScreen)
    screen._workflow = SimpleNamespace(workflow_mode=False)
    screen._services = MainScreenServices(bus=EventBus())
    screen._debug = lambda *_args: None
    screen._tool_actions = ToolActionBridge(publisher=bus, debug=screen._debug)

    async def _collect(event: AskUserResponse) -> None:
        responses.append(event)

    async def _run() -> None:
        await bus.subscribe(AskUserResponse, _collect)
        await screen.on_ask_user_inline_submitted(
            AskUserInlineSubmitted("c1", "q1", (AskUserAnswer(values=("Python",)),))
        )

    asyncio.run(_run())

    assert len(responses) == 1
    assert responses[0].request_id == "q1"
    assert responses[0].answers == (AskUserAnswer(values=("Python",)),)


def test_agent_loading_does_not_hide_footer_bindings() -> None:
    """Loading modal blocks interaction; footer bindings should stay visually stable."""
    from chrys.app.tui.screens.main.screen import MainScreen

    screen = object.__new__(MainScreen)
    screen._workflow = SimpleNamespace(workflow_mode=False)
    screen._services = MainScreenServices(bus=EventBus())
    screen._state = MainScreenState()
    screen._state.run.agent_loading = True
    screen._dashboard_visible = lambda: False

    assert MainScreen.check_action(screen, "sessions", ()) is True
    assert MainScreen.check_action(screen, "agents_config", ()) is True
    assert MainScreen.check_action(screen, "models_config", ()) is True
    assert MainScreen.check_action(screen, "show_log_viewer", ()) is True
    assert MainScreen.check_action(screen, "pick_theme", ()) is True
    assert MainScreen.check_action(screen, "settings", ()) is True


def test_history_scope_footer_binding_removed() -> None:
    """Prompt history should not reserve Ctrl+H because it collides with Backspace."""
    from chrys.app.tui.screens.main.screen import MainScreen
    from chrys.app.tui.widgets.chrome.input_bar import _ChatTextArea
    from chrys.foundation.events.bus import EventBus

    screen = MainScreen(EventBus(), engine_provider=None)

    assert all(binding.key != "ctrl+h" for binding in _ChatTextArea.BINDINGS)
    assert "ctrl+h" not in screen._bindings.key_to_bindings


def test_prompt_history_uses_hidden_ctrl_r_binding() -> None:
    """Ctrl+R opens prompt history without adding another footer item."""
    from chrys.app.tui.screens.main.screen import MainScreen
    from chrys.foundation.events.bus import EventBus

    screen = MainScreen(EventBus(), engine_provider=None)
    binding = next(binding for binding in MainScreen.BINDINGS if binding.key == "ctrl+r")

    assert binding.action == "prompt_history"
    assert binding.show is False
    assert binding.priority is True
    assert "ctrl+r" in screen._bindings.key_to_bindings
    assert all(binding.key != "ctrl+t" for binding in MainScreen.BINDINGS)
    assert "ctrl+t" not in screen._bindings.key_to_bindings
    assert "action_toggle_toc" not in MainScreen.__dict__


@pytest.mark.parametrize(
    ("fullscreen_terminal", "shell_mode", "dashboard_visible"),
    [(True, False, False), (False, True, False), (False, False, True)],
    ids=["fullscreen-terminal", "shell-mode", "trajectory-dashboard"],
)
def test_prompt_history_action_enforces_hidden_binding_availability(
    fullscreen_terminal: bool,
    shell_mode: bool,
    dashboard_visible: bool,
) -> None:
    """Hidden bindings still dispatch, so the action must enforce overlay availability."""
    from chrys.app.tui.screens.main.screen import MainScreen

    screen = object.__new__(MainScreen)
    screen._workflow = SimpleNamespace(workflow_mode=False)
    screen._services = MainScreenServices(bus=EventBus())
    screen._state = MainScreenState()
    screen._state.shell.fullscreen_terminal = fullscreen_terminal
    screen._state.shell.active = shell_mode
    screen._dashboard_visible = lambda: dashboard_visible
    screen.query_one = lambda _widget: (_ for _ in ()).throw(AssertionError("input bar must not be queried"))

    MainScreen.action_prompt_history(screen)


async def test_start_worker_makes_its_work_only_when_the_worker_starts() -> None:
    calls: list[str] = []

    async def finish() -> str:
        return "done"

    def work() -> Awaitable[object]:
        calls.append("work")
        return finish()

    async with App().run_test() as pilot:
        host = SimpleNamespace(run_worker=pilot.app.run_worker)

        cancelled = MainScreen._start_worker(host, work)
        assert isinstance(cancelled, Worker)
        cancelled.cancel()
        with contextlib.suppress(WorkerCancelled):
            await cancelled.wait()
        # Cancelled before its first step, the worker never made a coroutine
        # that would be left unawaited.
        assert calls == []

        started = MainScreen._start_worker(host, work)
        assert isinstance(started, Worker)
        assert await started.wait() == "done"
        assert calls == ["work"]


async def test_start_worker_names_the_worker_after_its_work() -> None:
    class _Sessions:
        async def restore(self, session_id: str, *, quiet: bool) -> str:
            return session_id

        async def refresh(self) -> str:
            return "refreshed"

    async def poll() -> str:
        return "polled"

    sessions = _Sessions()
    async with App().run_test() as pilot:
        host = SimpleNamespace(run_worker=pilot.app.run_worker)
        workers = [
            MainScreen._start_worker(host, partial(sessions.restore, "abc", quiet=True)),
            MainScreen._start_worker(host, sessions.refresh),
            MainScreen._start_worker(host, poll),
        ]
        assert all(isinstance(worker, Worker) for worker in workers)
        named = cast(list[Worker[object]], workers)

        assert [(worker.name, worker.description) for worker in named] == [
            ("restore", "restore('abc', quiet=True)"),
            ("refresh", "refresh()"),
            ("poll", "poll()"),
        ]
        assert [await worker.wait() for worker in named] == ["abc", "refreshed", "polled"]

        prompt = "x" * 100_000
        long = MainScreen._start_worker(host, partial(sessions.restore, prompt, quiet=True))
        assert isinstance(long, Worker)
        # A whole prompt is abbreviated, not copied into the description.
        assert long.description.startswith("restore('xxx")
        assert long.description.endswith("', quiet=True)")
        assert len(long.description) < 100
        assert await long.wait() == prompt


def test_main_screen_feeds_the_branch_controller_its_cwds_and_backend_refresh() -> None:
    screen = MainScreen(EventBus(), engine_provider=None)
    branch = screen._workspace_branch
    screen._state.workspace_marker.current_cwd = "/workspace"

    assert branch._workspace_cwd() == "/workspace"
    screen.set_reactive(MainScreen.chat_workspace_cwd, "")
    # Before the border shows a cwd, the displayed cwd is the workspace cwd.
    assert branch._displayed_cwd() == "/workspace"
    screen.set_reactive(MainScreen.chat_workspace_cwd, "/shown")
    assert branch._displayed_cwd() == "/shown"
    assert screen._events._callbacks.refresh_git_branch == branch.schedule_refresh


class _RecordingBranch:
    """Records what MainScreen asks of its git branch controller."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def start(self, cwd: str) -> None:
        self.calls.append(("start", cwd))

    def configure(self, cwd: str) -> None:
        self.calls.append(("configure", cwd))

    def displayed_cwd_changed(self, old_cwd: str, cwd: str) -> None:
        self.calls.append(("displayed_cwd_changed", old_cwd, cwd))

    async def close(self) -> None:
        self.calls.append(("close",))


class _MainScreenApp(App[None]):
    def __init__(self, screen: MainScreen) -> None:
        super().__init__()
        self.main_screen = screen

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self.main_screen)


async def test_main_screen_drives_the_branch_controller_from_mount_to_unmount() -> None:
    screen = MainScreen(EventBus(), engine_provider=None)
    branch = _RecordingBranch()
    screen._workspace_branch = branch  # type: ignore[assignment]
    screen._state.workspace_marker.current_cwd = "/repo-a"
    shown_cwd = screen.chat_workspace_cwd

    async with _MainScreenApp(screen).run_test():
        await wait_for(lambda: branch.calls, description="the branch controller started")
        assert branch.calls == [("start", "/repo-a")]

        screen._set_workspace_cwd("/repo-b")
        screen.chat_workspace_cwd = "/repo-b"

        assert branch.calls[1:] == [("configure", "/repo-b"), ("displayed_cwd_changed", shown_cwd, "/repo-b")]

    assert branch.calls[-1] == ("close",)
