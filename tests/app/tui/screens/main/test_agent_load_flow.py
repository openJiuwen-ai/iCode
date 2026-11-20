# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the agent-load event flow: dialogs, failures and restore reuse."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest

from chrys.app.tui.screens.diff import RollbackProgressModal
from chrys.app.tui.screens.main.ports import StatusTrail
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState, RuntimeState
from chrys.app.tui.screens.main.view_adapter import MainScreenViewAdapter
from chrys.app.tui.support.gc_freeze import (
    GcAbsorbReason,
    GcAbsorbRequested,
    GcReclaimReason,
    GcReclaimRequested,
)
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentRuntimeDetails,
    ProfileSwitched,
    RuntimeHookDetails,
    RuntimeHookSourceDetails,
    RuntimeModelDetails,
)
from chrys.foundation.i18n import MessageRef
from chrys.foundation.i18n.formatting import format_message
from tests.support.tui_helpers import (
    main_screen_state_at,
    make_backend_handler,
    make_session_handler,
    status_text,
    status_trail,
)


def test_agent_load_events_lock_input_and_wait_for_switch_event() -> None:
    from chrys.foundation.events.types import AgentLoadFinished, AgentLoadProgress, AgentLoadStarted

    load_states: list[bool] = []
    pushed: list[object] = []
    status_calls: list[str] = []
    clipboard_dirs: list[object] = []

    class _FakeStatusBar:
        def snapshot(self) -> dict[str, object]:
            return {"visible": False, "flash": None, "status": ""}

        def start_run(self) -> None:
            status_calls.append("start")

        def show(self, msg: MessageRef | str) -> None:
            status_calls.append(status_text(msg))

    class _FakeApp:
        def push_screen(self, screen: object, _callback=None) -> None:
            pushed.append(screen)

    class _FakeInputBar:
        def set_clipboard_image_dir(self, directory: object) -> None:
            clipboard_dirs.append(directory)

        def focus_input(self) -> None:
            return

    class _FakeStateStore:
        def session_dir(self, session_id: str) -> Path:
            return Path("/sessions") / session_id

    def _query_one(cls):
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        if cls.__name__ == "InputBar":
            return _FakeInputBar()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    def _set_agent_loading(value: bool) -> None:
        load_states.append(value)

    def _debug(*_args: object) -> None:
        return

    screen = SimpleNamespace(
        app=_FakeApp(),
        _services=MainScreenServices(bus=EventBus(), state_store=_FakeStateStore()),
        _gc_messages=[],
        query_one=_query_one,
        _set_agent_loading=_set_agent_loading,
        _debug=_debug,
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None
    handler._agent_load_status_snapshot = None

    asyncio.run(
        handler.on_agent_load_started(
            AgentLoadStarted(
                operation="switch",
                to_profile="Code",
                to_display_name="Code Agent",
                session_id="session-new",
            )
        )
    )
    assert load_states == [True]
    assert status_calls[:2] == ["start", "Switching Agent"]
    assert clipboard_dirs == [Path("/sessions/session-new/attachments/clipboard")]
    assert len(pushed) == 1

    dialog = pushed[0]
    asyncio.run(
        handler.on_agent_load_progress(
            AgentLoadProgress(phase="mcp", message="Connecting MCP server fs", current=0, total=2)
        )
    )
    assert dialog._message == "Connecting MCP server fs"

    asyncio.run(
        handler.on_agent_load_progress(
            AgentLoadProgress(phase="mcp", message="Connecting MCP server fs", current=1, total=2)
        )
    )
    assert dialog._message == "Connecting MCP server fs"
    assert dialog._messages == ["Connecting MCP servers: 1/2"]

    asyncio.run(
        handler.on_agent_load_finished(
            AgentLoadFinished(operation="switch", agent_profile="Code", display_name="Code Agent")
        )
    )
    assert load_states == [True]
    assert dialog._dismiss_pending is False
    assert dialog._message == "Applying agent changes"
    assert dialog._messages[-1] == "Applying agent changes"
    assert len(screen._gc_messages) == 1
    assert isinstance(screen._gc_messages[0], GcReclaimRequested)
    assert screen._gc_messages[0].reason is GcReclaimReason.AGENT_REBUILT
    assert screen._gc_messages[0].prompt is False

    handler.finish_agent_load("Profile switched: QA -> Code")
    assert load_states == [True, False]
    assert dialog._dismiss_pending is True
    assert dialog._messages[-1] == "Profile switched: QA -> Code"
    assert "Applying agent changes" not in dialog._messages


def test_agent_load_dialog_replaces_active_rollback_progress_modal() -> None:
    calls: list[tuple[str, object]] = []
    restore_dialog = object()

    class _RollbackProgress:
        def handoff_to_session_restore(self) -> None:
            calls.append(("rollback-dismiss", self))

    class _FakeApp:
        def push_screen(self, screen: object, _callback=None) -> None:
            calls.append(("restore-push", screen))

    rollback_progress = _RollbackProgress()
    screen = SimpleNamespace(app=_FakeApp())
    adapter = MainScreenViewAdapter(screen, state=MainScreenState())  # type: ignore[arg-type]
    adapter._rollback_progress_modal = rollback_progress  # type: ignore[assignment]

    asyncio.run(adapter.push_agent_load_dialog(restore_dialog))

    assert calls == [("rollback-dismiss", rollback_progress), ("restore-push", restore_dialog)]
    assert adapter._rollback_progress_modal is None


def test_rollback_progress_worker_does_not_cancel_prior_handoff_tail() -> None:
    worker_calls: list[tuple[object, bool, str]] = []
    pushed: list[object] = []

    class _FakeScreen:
        app = SimpleNamespace(push_screen=lambda modal, _callback=None: pushed.append(modal))

        def run_worker(self, awaitable: object, *, exclusive: bool, group: str) -> object:
            worker_calls.append((awaitable, exclusive, group))
            return object()

    async def operation() -> None:
        return

    adapter = MainScreenViewAdapter(_FakeScreen(), state=MainScreenState())  # type: ignore[arg-type]
    adapter.open_rollback_progress_modal(operation)
    modal = pushed[0]
    assert isinstance(modal, RollbackProgressModal)
    awaitable = modal._run_operation()
    try:
        assert modal._start_worker is not None
        modal._start_worker(awaitable)
    finally:
        awaitable.close()

    assert worker_calls == [(awaitable, False, "rollback-progress")]


def test_nonstartup_agent_load_failure_requests_conservative_idle_reclaim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.foundation.events.types import AgentLoadFailed

    gc_messages: list[object] = []
    screen = SimpleNamespace(
        _state=MainScreenState(runtime=RuntimeState(profile="Code")),
        _gc_messages=gc_messages,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)
    monkeypatch.setattr(handler._agent_load(), "on_failed", lambda _event, *, display=None: None)

    asyncio.run(handler.on_agent_load_failed(AgentLoadFailed(operation="startup", agent_profile="Code")))
    assert gc_messages == []

    asyncio.run(handler.on_agent_load_failed(AgentLoadFailed(operation="switch", agent_profile="QA")))
    assert len(gc_messages) == 1
    assert isinstance(gc_messages[0], GcReclaimRequested)
    assert gc_messages[0].reason is GcReclaimReason.AGENT_REBUILD_FAILED
    assert gc_messages[0].prompt is False


def test_agent_load_failed_preserves_failed_profile_label() -> None:
    from chrys.foundation.events.types import AgentLoadFailed

    loading: list[bool] = []
    flashes: list[str] = []
    subtitles: list[str] = []

    class _FakeStatusBar:
        def flash(self, message: MessageRef | str, **_kwargs: object) -> None:
            flashes.append(message if isinstance(message, str) else format_message(message))

    def _query_one(cls):
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    state = MainScreenState()
    profiles: list[str] = []
    screen = SimpleNamespace(
        _state=state,
        _set_profile_display=profiles.append,
        _set_agent_loading=loading.append,
        query_one=_query_one,
        _debug=lambda *_args: None,
        _update_subtitle=lambda: subtitles.append("updated"),
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None

    asyncio.run(
        handler.on_agent_load_failed(
            AgentLoadFailed(agent_profile="Code", display_name="Code", message="missing api key")
        )
    )

    assert state.runtime.profile == "Code"
    assert profiles == ["Code"]
    assert subtitles == ["updated"]
    assert loading == [False]
    assert flashes == ["Agent load failed: missing api key"]


def test_finish_agent_load_clears_loading_if_dialog_finish_fails() -> None:
    """A stale successful-load dialog must not prevent input-bar unlock."""
    loading: list[bool] = []

    class _BrokenDialog:
        def finish(self, *_args: object, **_kwargs: object) -> None:
            raise RuntimeError("dialog already gone")

    screen = SimpleNamespace(_set_agent_loading=loading.append)
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = _BrokenDialog()

    handler.finish_agent_load("Session ready: Code")

    assert handler._agent_load_dialog is None
    assert loading == [False]


def test_agent_load_restore_waits_for_session_restored() -> None:
    from chrys.foundation.events.types import AgentLoadFinished, AgentLoadStarted

    load_states: list[bool] = []
    pushed: list[object] = []
    clipboard_dirs: list[object] = []

    class _FakeStatusBar:
        def snapshot(self) -> dict[str, object]:
            return {"visible": False, "flash": None, "status": ""}

        def start_run(self) -> None:
            return

        def show(self, _msg: str) -> None:
            return

    class _FakeApp:
        def push_screen(self, screen: object, _callback=None) -> None:
            pushed.append(screen)

    class _FakeInputBar:
        def set_clipboard_image_dir(self, directory: object) -> None:
            clipboard_dirs.append(directory)

        def focus_input(self) -> None:
            return

    class _FakeStateStore:
        def session_dir(self, session_id: str) -> Path:
            return Path("/sessions") / session_id

    def _query_one(cls):
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        if cls.__name__ == "InputBar":
            return _FakeInputBar()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    screen = SimpleNamespace(
        app=_FakeApp(),
        _services=MainScreenServices(bus=EventBus(), state_store=_FakeStateStore()),
        query_one=_query_one,
        _set_agent_loading=load_states.append,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None
    handler._agent_load_status_snapshot = None

    asyncio.run(
        handler.on_agent_load_started(
            AgentLoadStarted(operation="restore", to_profile="Code", session_id="session-restore")
        )
    )
    dialog = pushed[0]
    assert clipboard_dirs == [Path("/sessions/session-restore/attachments/clipboard")]

    asyncio.run(handler.on_agent_load_finished(AgentLoadFinished(operation="restore", agent_profile="Code")))
    assert load_states == [True]
    assert dialog._message == "Restoring session history"

    handler.finish_agent_load()
    assert load_states == [True, False]
    assert dialog._dismiss_pending is True


def test_begin_session_restore_load_shows_availability_check() -> None:
    load_states: list[bool] = []
    pushed: list[object] = []
    status_calls: list[str] = []
    clipboard_dirs: list[object] = []

    class _FakeStatusBar:
        def snapshot(self) -> dict[str, object]:
            return {"visible": False, "flash": None, "status": ""}

        def start_run(self) -> None:
            status_calls.append("start")

        def show(self, msg: MessageRef | str) -> None:
            status_calls.append(status_text(msg))

    class _FakeApp:
        def push_screen(self, screen: object, _callback=None) -> None:
            pushed.append(screen)

    class _FakeInputBar:
        def set_clipboard_image_dir(self, directory: object) -> None:
            clipboard_dirs.append(directory)

        def focus_input(self) -> None:
            return

    class _FakeStateStore:
        def session_dir(self, session_id: str) -> Path:
            return Path("/sessions") / session_id

    def _query_one(cls):
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        if cls.__name__ == "InputBar":
            return _FakeInputBar()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    screen = SimpleNamespace(
        app=_FakeApp(),
        _services=MainScreenServices(bus=EventBus(), state_store=_FakeStateStore()),
        query_one=_query_one,
        _set_agent_loading=load_states.append,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None
    handler._agent_load_status_snapshot = None

    asyncio.run(handler.begin_session_restore_load("40d9a048-3e08-4cff-a0e0-ce8c09d3e011"))

    assert load_states == [True]
    assert status_calls[:2] == ["start", "Restoring Session"]
    assert clipboard_dirs == []
    assert len(pushed) == 1
    dialog = pushed[0]
    assert dialog._message == "Checking session availability"
    assert dialog._messages == ["Checking session availability"]


def test_begin_session_restore_load_lookup_then_resolved_id_reuses_dialog() -> None:
    """``/resume`` opens the modal with an empty id, then fills in the resolved id."""
    pushed: list[object] = []
    snapshots: list[None] = []

    class _FakeStatusBar:
        def snapshot(self) -> dict[str, object]:
            snapshots.append(None)
            return {"visible": False, "flash": None, "status": ""}

        def start_run(self) -> None:
            return

        def show(self, _msg: object) -> None:
            return

    class _FakeApp:
        def push_screen(self, screen: object, _callback=None) -> None:
            pushed.append(screen)

    class _FakeInputBar:
        def set_clipboard_image_dir(self, directory: object) -> None:
            raise AssertionError(f"restore must not touch clipboard dir: {directory}")

        def focus_input(self) -> None:
            return

    def _query_one(cls):
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        if cls.__name__ == "InputBar":
            return _FakeInputBar()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    screen = SimpleNamespace(
        app=_FakeApp(),
        query_one=_query_one,
        _set_agent_loading=lambda _value: None,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None
    handler._agent_load_status_snapshot = None

    asyncio.run(handler.begin_session_restore_load(""))
    assert len(pushed) == 1
    dialog = pushed[0]
    assert dialog._subtitle == ""
    assert dialog._message == "Checking session availability"

    asyncio.run(handler.begin_session_restore_load("40d9a048-3e08-4cff-a0e0-ce8c09d3e011"))

    assert pushed == [dialog]
    assert handler._agent_load_dialog is dialog
    assert dialog._subtitle == "40d9a0483e08"
    assert dialog._message == "Checking session availability"
    assert snapshots == [None]


def test_restore_agent_load_started_reuses_availability_dialog() -> None:
    from chrys.foundation.events.types import AgentLoadStarted

    pushed: list[object] = []
    clipboard_dirs: list[object] = []

    class _FakeStatusBar:
        def snapshot(self) -> dict[str, object]:
            return {"visible": False, "flash": None, "status": ""}

        def start_run(self) -> None:
            return

        def show(self, _msg: str) -> None:
            return

    class _FakeApp:
        def push_screen(self, screen: object, _callback=None) -> None:
            pushed.append(screen)

    class _FakeInputBar:
        def set_clipboard_image_dir(self, directory: object) -> None:
            clipboard_dirs.append(directory)

        def focus_input(self) -> None:
            return

    class _FakeStateStore:
        def session_dir(self, session_id: str) -> Path:
            return Path("/sessions") / session_id

    def _query_one(cls):
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        if cls.__name__ == "InputBar":
            return _FakeInputBar()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    screen = SimpleNamespace(
        app=_FakeApp(),
        _services=MainScreenServices(bus=EventBus(), state_store=_FakeStateStore()),
        query_one=_query_one,
        _set_agent_loading=lambda _value: None,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None
    handler._agent_load_status_snapshot = None

    asyncio.run(handler.begin_session_restore_load("restore"))
    dialog = pushed[0]
    asyncio.run(
        handler.on_agent_load_started(AgentLoadStarted(operation="restore", to_profile="Code", session_id="restore"))
    )

    assert pushed == [dialog]
    assert clipboard_dirs == [Path("/sessions/restore/attachments/clipboard")]
    assert handler._agent_load_dialog is dialog
    assert dialog._message == "Preparing agent"
    assert dialog._messages == ["Session availability checked"]


def test_agent_load_restore_final_message_replaces_pending_message() -> None:
    from chrys.foundation.events.types import AgentLoadFinished, AgentLoadStarted

    load_states: list[bool] = []
    pushed: list[object] = []
    clipboard_dirs: list[object] = []

    class _FakeStatusBar:
        def snapshot(self) -> dict[str, object]:
            return {"visible": False, "flash": None, "status": ""}

        def start_run(self) -> None:
            return

        def show(self, _msg: str) -> None:
            return

    class _FakeApp:
        def push_screen(self, screen: object, _callback=None) -> None:
            pushed.append(screen)

    class _FakeInputBar:
        def set_clipboard_image_dir(self, directory: object) -> None:
            clipboard_dirs.append(directory)

        def focus_input(self) -> None:
            return

    class _FakeStateStore:
        def session_dir(self, session_id: str) -> Path:
            return Path("/sessions") / session_id

    def _query_one(cls):
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        if cls.__name__ == "InputBar":
            return _FakeInputBar()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    screen = SimpleNamespace(
        app=_FakeApp(),
        _services=MainScreenServices(bus=EventBus(), state_store=_FakeStateStore()),
        query_one=_query_one,
        _set_agent_loading=load_states.append,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None
    handler._agent_load_status_snapshot = None

    asyncio.run(
        handler.on_agent_load_started(AgentLoadStarted(operation="restore", to_profile="Code", session_id="restore"))
    )
    dialog = pushed[0]
    assert clipboard_dirs == [Path("/sessions/restore/attachments/clipboard")]

    asyncio.run(handler.on_agent_load_finished(AgentLoadFinished(operation="restore", agent_profile="Code")))
    handler.finish_agent_load("Session restored: abc12345")

    assert dialog._messages[-1] == "Session restored: abc12345"
    assert "Restoring session history" not in dialog._messages


def test_profile_switched_finishes_agent_load_after_final_event() -> None:
    finish_messages: list[str] = []
    flashes: list[str] = []
    status_clears: list[None] = []
    tool_trails: list[StatusTrail] = []
    welcome_updates: list[tuple[str, str]] = []

    class _FakeEvents:
        def format_tool_info(
            self,
            tool_names: list[str],
            skill_names: list[str],
            *,
            memory_files: list[str] | None = None,
            runtime_details: AgentRuntimeDetails | None = None,
        ) -> str:
            del tool_names, skill_names, memory_files, runtime_details
            return "tools"

        def get_profile_description(self, _profile_name: str) -> str:
            return "description"

        def finish_agent_load(self, message: MessageRef | str = "") -> None:
            finish_messages.append(status_text(message))

    class _FakePanel:
        def set_profile(self, _profile: str) -> None:
            return

        def update_welcome(self, *, profile: str = "", cwd: str = "") -> None:
            welcome_updates.append((profile, cwd))

    class _FakeInputBar:
        def set_clipboard_image_dir(self, _directory: object) -> None:
            return

    class _FakeStatusBar:
        def set_profile(self, _profile: str, *, description: str = "") -> None:
            return

        def set_tool_info(self, trail: StatusTrail) -> None:
            tool_trails.append(trail)

        def clear_status(self) -> None:
            status_clears.append(None)

        def flash(self, message: str, **_kwargs: object) -> None:
            flashes.append(message)

    panel = _FakePanel()
    input_bar = _FakeInputBar()
    status = _FakeStatusBar()
    # The Hooks tab is told when the number of configured hooks changes.
    hook_runs: list[None] = []
    sidebar = SimpleNamespace(hooks_panel=SimpleNamespace(runs_changed=lambda: hook_runs.append(None)))

    def _query_one(cls):
        if cls.__name__ == "ChatPanel":
            return panel
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "StatusBar":
            return status
        if cls.__name__ == "SidebarPanel":
            return sidebar
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    state = main_screen_state_at("/repo/current")
    state.runtime.profile = "Code Agent"
    screen = SimpleNamespace(
        _state=state,
        _events=_FakeEvents(),
        _gc_messages=[],
        query_one=_query_one,
        _update_subtitle=lambda: None,
        _debug=lambda *_args: None,
    )
    handler = make_session_handler(screen)
    runtime_details = AgentRuntimeDetails(
        hook_sources=[
            RuntimeHookSourceDetails(
                scope="global",
                hooks=[RuntimeHookDetails(id="notify", enabled=True)],
            )
        ]
    )

    asyncio.run(
        handler.on_profile_switched(
            ProfileSwitched(
                from_profile="QA",
                to_profile="Code",
                from_display_name="QA Agent",
                to_display_name="Code Agent",
                runtime_details=runtime_details,
            )
        )
    )

    assert flashes == []
    assert status_clears == [None]
    assert finish_messages == ["Profile switched: QA Agent -> Code Agent"]
    assert [status_trail(trail) for trail in tool_trails] == ["1 hook"]
    assert hook_runs == [None]
    assert welcome_updates == [("Code Agent", "/repo/current")]
    assert len(screen._gc_messages) == 1
    assert isinstance(screen._gc_messages[0], GcAbsorbRequested)
    assert screen._gc_messages[0].reason is GcAbsorbReason.PROFILE_UI_UPDATED
    assert screen._gc_messages[0].terminal_boundary is False


@pytest.mark.parametrize(
    ("selection_source", "expected"),
    [("active", "new-model"), ("agent", "old-model")],
)
def test_profile_switched_syncs_model_cache_only_for_active_selection(
    selection_source: Literal["active", "agent"],
    expected: str,
) -> None:
    tool_trails: list[StatusTrail] = []

    class _FakeEvents:
        def format_tool_info(
            self,
            tool_names: list[str],
            skill_names: list[str],
            *,
            memory_files: list[str] | None = None,
            runtime_details: AgentRuntimeDetails | None = None,
        ) -> str:
            del tool_names, skill_names, memory_files, runtime_details
            return "tools"

        def finish_agent_load(self, _message: MessageRef | str = "") -> None:
            return

    class _FakeStatusBar:
        def set_tool_info(self, trail: StatusTrail) -> None:
            tool_trails.append(trail)

        def clear_status(self) -> None:
            return

        def flash(self, _message: str, **_kwargs: object) -> None:
            return

    # The Hooks tab is told when the number of configured hooks changes.
    hook_runs: list[None] = []
    sidebar = SimpleNamespace(hooks_panel=SimpleNamespace(runs_changed=lambda: hook_runs.append(None)))

    def query_one(cls: type):
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        if cls.__name__ == "SidebarPanel":
            return sidebar
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    services = MainScreenServices(bus=EventBus(), active_model_profile_id="old-model")
    screen = SimpleNamespace(
        _state=MainScreenState(runtime=RuntimeState(profile="Code")),
        _services=services,
        _events=_FakeEvents(),
        _gc_messages=[],
        query_one=query_one,
        _debug=lambda *_args: None,
    )
    handler = make_session_handler(screen)
    details = AgentRuntimeDetails(
        model=RuntimeModelDetails(profile_id="new-model", selection_source=selection_source),
        hook_sources=[
            RuntimeHookSourceDetails(
                scope="workdir",
                hooks=[RuntimeHookDetails(id="guard", enabled=True)],
            )
        ],
    )

    asyncio.run(
        handler.on_profile_switched(
            ProfileSwitched(
                from_profile="Code",
                to_profile="Code",
                runtime_details=details,
            )
        )
    )

    assert services.active_model_profile_id == expected
    assert [status_trail(trail) for trail in tool_trails] == ["1 hook"]
    assert hook_runs == [None]
