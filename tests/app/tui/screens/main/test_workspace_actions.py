# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the workspace controller's directory-picker construction."""

from __future__ import annotations

from pathlib import Path

from chrys.app.tui.screens.dialogs.file_picker import FilePicker
from chrys.app.tui.screens.main.recent_dirs import WorkspaceMruRecentDirs
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.app.tui.screens.main.workspace_actions import _CHANGE_DIRECTORY, WorkspaceCallbacks, WorkspaceController
from chrys.foundation.events.bus import EventBus
from chrys.foundation.i18n import Localizer, MessageRef
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.workspace import Workspace
from chrys.service.state.store import JsonFileStateStore
from tests.support.tui_helpers import discard_worker


class _RecordingView:
    def __init__(self) -> None:
        self.is_attached = True
        self.pushed: list[object] = []

    def push_screen(self, screen: object, _callback: object | None = None) -> None:
        self.pushed.append(screen)

    def notify(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("unexpected notify")


def _make_controller(
    view: _RecordingView,
    services: MainScreenServices,
    cwd: str,
    *,
    workspace_id: str | None = None,
    callbacks: WorkspaceCallbacks | None = None,
) -> WorkspaceController:
    state = MainScreenState()
    state.workspace.current_cwd = cwd
    state.workspace_marker.current_cwd = cwd
    if workspace_id is not None:
        state.workspace.membership = Workspace(primary_cwd=cwd, id=workspace_id)
    return WorkspaceController(
        state=state,
        services=services,
        view=view,  # type: ignore[arg-type]
        callbacks=callbacks or WorkspaceCallbacks(start_worker=discard_worker, debug=lambda *_args: None),
    )


def test_directory_picker_uses_configured_mru_provider(tmp_path: Path) -> None:
    state_store = JsonFileStateStore(tmp_path / "sessions")
    services = MainScreenServices(bus=EventBus(), state_store=state_store, workspace_mru_max_entries=7)
    view = _RecordingView()

    _make_controller(view, services, str(tmp_path)).open_working_dir_picker()

    (picker,) = view.pushed
    assert isinstance(picker, FilePicker)
    assert isinstance(picker._title, MessageRef)
    assert format_message(picker._title) == "Change Directory"
    assert Localizer("zh-Hans").render(picker._title) == "更改目录"
    provider = picker._recent_paths
    assert isinstance(provider, WorkspaceMruRecentDirs)
    assert provider._max_entries == 7
    assert provider._state_store is state_store


def test_directory_picker_provider_tolerates_missing_state_store(tmp_path: Path) -> None:
    """Without a store the picker still gets a provider (index read-only mode)."""
    services = MainScreenServices(bus=EventBus(), state_store=None)
    view = _RecordingView()

    _make_controller(view, services, str(tmp_path)).open_working_dir_picker()

    (picker,) = view.pushed
    provider = picker._recent_paths
    assert isinstance(provider, WorkspaceMruRecentDirs)
    assert provider._state_store is None


def test_change_directory_title_definition_keeps_english_and_localizes_chinese() -> None:
    assert format_message(_CHANGE_DIRECTORY.bind()) == "Change Directory"
    assert Localizer("zh-Hans").render(_CHANGE_DIRECTORY.bind()) == "更改目录"
