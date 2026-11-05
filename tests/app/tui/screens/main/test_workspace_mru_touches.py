# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for workspace MRU touch scheduling from session and workspace events."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from chrys.app.tui.screens.main.state import MainScreenState, SessionViewState
from chrys.foundation.events.types import (
    SessionReady,
    SessionRestored,
    WorkspaceUpdated,
)
from tests.support.tui_helpers import (
    fake_session_title,
    main_screen_state_at,
    make_backend_handler,
    make_session_handler,
)

# ──────────── workspace MRU touch scheduling ───────────────────────────


def _capture_mru_touches(captured: list[dict[str, object]]):
    def _capture(paths, *, max_entries, session_id="", used_at=None) -> None:
        captured.append(
            {"paths": list(paths), "max_entries": max_entries, "session_id": session_id, "used_at": used_at}
        )

    return _capture


def _make_mru_workspace_screen(calls: list[tuple[str, object]]) -> SimpleNamespace:
    """Minimal screen fake for the no-messages on_workspace_updated path."""

    class _FakePanel:
        border_subtitle = None

        def update_welcome(self, *, profile: str = "", cwd: str = "") -> None:
            calls.append(("welcome", (profile, cwd)))

        def set_workspace_cwd(self, cwd: str) -> None:
            calls.append(("workspace_cwd", cwd))

    class _FakeShellPanel:
        async def change_directory(self, cwd: str) -> None:
            calls.append(("shell_cwd", cwd))

    class _FakeInputBar:
        def set_paste_cwd(self, cwd: str) -> None:
            calls.append(("paste_cwd", cwd))

        def set_clipboard_image_dir(self, directory: object) -> None:
            calls.append(("clipboard_image_dir", directory))

    panel = _FakePanel()
    shell = _FakeShellPanel()
    input_bar = _FakeInputBar()

    def query_one(cls: type):
        if cls.__name__ == "ChatPanel":
            return panel
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "ShellPanel":
            return shell
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    state = main_screen_state_at("/repo/current")
    state.runtime.profile = "Code Agent"
    return SimpleNamespace(
        _state=state,
        _suggestions=SimpleNamespace(discard_file_index=lambda: None),
        query_one=query_one,
        _session_title=fake_session_title(),
        _debug=lambda *_args: None,
    )


async def _drain_workspace_mru_tasks() -> None:
    from chrys.app.tui.support import workspace_mru

    while workspace_mru._BACKGROUND_TASKS:
        await asyncio.gather(*list(workspace_mru._BACKGROUND_TASKS), return_exceptions=True)


def test_session_ready_schedules_mru_touches_for_all_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        "chrys.app.tui.screens.main.event_handlers.schedule_workspace_mru_touches",
        _capture_mru_touches(captured),
    )

    class _FakePanel:
        def set_profile(self, _profile: str) -> None:
            return

        def set_tool_kinds(self, _tool_kinds: dict[str, str]) -> None:
            return

    class _FakeInputBar:
        def set_clipboard_image_dir(self, _directory: object) -> None:
            return

    class _FakeStatusBar:
        def set_profile(self, _profile: str, *, description: str = "") -> None:
            return

        def set_tool_info(self, _trail: str) -> None:
            return

    def _query_one(cls):
        if cls.__name__ == "ChatPanel":
            return _FakePanel()
        if cls.__name__ == "InputBar":
            return _FakeInputBar()
        if cls.__name__ == "StatusBar":
            return _FakeStatusBar()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    screen = SimpleNamespace(
        _state=MainScreenState(session=SessionViewState(restoring_session=True)),
        query_one=_query_one,
        _update_subtitle=lambda: None,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)
    handler._services.workspace_mru_max_entries = 7
    event = SessionReady(
        agent_profile="Code",
        session_id="session-1",
        primary_cwd="/repo/primary",
        working_dirs=["/repo/primary", "/repo/extra"],
    )

    asyncio.run(handler.on_session_ready(event))

    assert captured == [
        {
            "paths": ["/repo/primary", "/repo/primary", "/repo/extra"],
            "max_entries": 7,
            "session_id": "session-1",
            "used_at": event.timestamp,
        }
    ]


def test_session_restored_schedules_mru_touches_for_all_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        "chrys.app.tui.screens.main.session_handlers.schedule_workspace_mru_touches",
        _capture_mru_touches(captured),
    )
    calls: list[tuple[str, object]] = []

    class _FakePanel:
        border_subtitle = None

        def update_welcome(self, *, profile: str = "", cwd: str = "") -> None:
            self.welcome_info = (profile, cwd)

        async def clear(self) -> None:
            return

        def set_session_id(self, _session_id: str) -> None:
            return

        def set_workspace_cwd(self, _cwd: str) -> None:
            return

        def update_usage(self, _tokens: int, _total_session_tokens: int = 0) -> None:
            return

    class _FakeInputBar:
        retry_mode = True

        def set_paste_cwd(self, cwd: str) -> None:
            calls.append(("paste_cwd", cwd))

        def set_clipboard_image_dir(self, _directory: object) -> None:
            return

    class _FakeStatusBar:
        def flash(self, _text: str) -> None:
            return

    panel = _FakePanel()
    input_bar = _FakeInputBar()
    status = _FakeStatusBar()

    def query_one(cls: type):
        if cls.__name__ == "ChatPanel":
            return panel
        if cls.__name__ == "InputBar":
            return input_bar
        if cls.__name__ == "StatusBar":
            return status
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    screen = SimpleNamespace(
        _state=MainScreenState(session=SessionViewState(restoring_session=True)),
        query_one=query_one,
        _set_has_messages=lambda _value: None,
        _session_title=fake_session_title(),
        _events=SimpleNamespace(finish_agent_load=lambda _message: None),
        _debug=lambda *_args: None,
    )
    event = SessionRestored(
        session_id="session-old",
        agent_profile="Code",
        display_name="Code Agent",
        message_count=3,
        primary_cwd="/repo/restored",
        working_dirs=["/repo/restored", "/repo/extra"],
    )

    asyncio.run(make_session_handler(screen).on_session_restored(event))

    assert captured == [
        {
            "paths": ["/repo/restored", "/repo/restored", "/repo/extra"],
            "max_entries": 20,
            "session_id": "session-old",
            "used_at": event.timestamp,
        }
    ]


def test_workspace_updated_schedules_mru_touches_with_event_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[dict[str, object]] = []
    monkeypatch.setattr(
        "chrys.app.tui.screens.main.session_handlers.schedule_workspace_mru_touches",
        _capture_mru_touches(captured),
    )
    screen = _make_mru_workspace_screen([])
    event = WorkspaceUpdated(
        primary_cwd="/repo/next",
        working_dirs=["/repo/next", "/repo/extra"],
        session_id="session-1",
    )

    asyncio.run(make_session_handler(screen).on_workspace_updated(event))

    assert captured == [
        {
            "paths": ["/repo/next", "/repo/next", "/repo/extra"],
            "max_entries": 20,
            "session_id": "session-1",
            "used_at": event.timestamp,
        }
    ]


def test_workspace_updated_records_mru_end_to_end(tmp_path: Path) -> None:
    """The detached touch task lands in the index; missing dirs never do."""
    from chrys.app.tui.support.workspace_mru import ensure_workspace_mru_index, load_workspace_mru

    primary = tmp_path / "primary"
    extra = tmp_path / "extra"
    primary.mkdir()
    extra.mkdir()
    missing = tmp_path / "deleted"
    ensure_workspace_mru_index([], max_entries=20, root_key="sha256:test-root")
    screen = _make_mru_workspace_screen([])
    event = WorkspaceUpdated(primary_cwd=str(primary), working_dirs=[str(extra), str(missing)])

    async def run() -> None:
        await make_session_handler(screen).on_workspace_updated(event)
        await _drain_workspace_mru_tasks()

    asyncio.run(run())

    assert load_workspace_mru(20) == [str(primary), str(extra)]


def test_workspace_updated_skips_mru_touch_when_index_missing(tmp_path: Path) -> None:
    """Event touches must not create the index ahead of the one-time backfill."""
    from chrys.app.tui.support.workspace_mru import workspace_mru_exists

    primary = tmp_path / "primary"
    primary.mkdir()
    calls: list[tuple[str, object]] = []
    screen = _make_mru_workspace_screen(calls)

    async def run() -> None:
        await make_session_handler(screen).on_workspace_updated(WorkspaceUpdated(primary_cwd=str(primary)))
        await _drain_workspace_mru_tasks()

    asyncio.run(run())

    assert not workspace_mru_exists()
    # The skipped touch is not an error — the UI refresh completed normally.
    assert ("workspace_cwd", str(primary)) in calls


def test_workspace_updated_mru_failure_does_not_break_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.app.tui.support import workspace_mru
    from chrys.app.tui.support.workspace_mru import ensure_workspace_mru_index

    primary = tmp_path / "primary"
    primary.mkdir()
    ensure_workspace_mru_index([], max_entries=20, root_key="sha256:test-root")

    def _boom(*_args: object, **_kwargs: object) -> bool:
        raise OSError("disk full")

    monkeypatch.setattr(workspace_mru, "record_workspace_mru_uses", _boom)
    calls: list[tuple[str, object]] = []
    screen = _make_mru_workspace_screen(calls)

    async def run() -> None:
        await make_session_handler(screen).on_workspace_updated(WorkspaceUpdated(primary_cwd=str(primary)))
        await _drain_workspace_mru_tasks()

    asyncio.run(run())  # must not raise

    assert ("workspace_cwd", str(primary)) in calls
