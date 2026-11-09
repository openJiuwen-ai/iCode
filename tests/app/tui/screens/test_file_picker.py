# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the file picker dialog and its MRU-backed recent-paths provider."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList, Static

from chrys.app.tui.screens.dialogs import file_picker as file_picker_module
from chrys.app.tui.screens.dialogs.file_picker import (
    FilePicker,
    FilePickerMode,
    _FilteredDirectoryTree,
    _nearest_existing_dir,
)
from chrys.app.tui.screens.main.recent_dirs import WorkspaceMruRecentDirs
from chrys.app.tui.support import workspace_mru
from chrys.app.tui.support.path_shortcuts import format_recent_path_label
from chrys.app.tui.support.workspace_mru import (
    ensure_workspace_mru_index,
    schedule_workspace_mru_touches,
    session_root_key,
    workspace_mru_exists,
)
from chrys.foundation.platform import safe_getcwd
from chrys.kernel import Message
from chrys.service.state.store import JsonFileStateStore
from tests.support.waiting import wait_for


class _FilePickerApp(App):
    def compose(self) -> ComposeResult:
        yield Static("placeholder")


class _BlockingRecentPaths:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self._release = asyncio.Event()

    async def get_recent_paths(self) -> list[tuple[str, str]]:
        self.started.set()
        try:
            await self._release.wait()
            return []
        finally:
            self.finished.set()

    def release(self) -> None:
        self._release.set()


class _FakeStateStore:
    def __init__(self, sessions_dir: Path, metas: list[object] | None = None) -> None:
        self._sessions_dir = sessions_dir
        self._metas = metas or []
        self.list_sessions_calls = 0

    def session_dir(self, session_id: str) -> Path:
        return self._sessions_dir / session_id

    async def list_sessions(self) -> list[object]:
        self.list_sessions_calls += 1
        return list(self._metas)


def _meta(primary_cwd: str, *, working_dirs: list[str] | None = None, offset_seconds: float = 0) -> SimpleNamespace:
    return SimpleNamespace(
        primary_cwd=primary_cwd,
        working_dirs=working_dirs or [],
        updated_at=datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=offset_seconds),
    )


@pytest.fixture
def mru_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "mru-config"
    fake_platform = type("P", (), {"config_dir": target})()
    monkeypatch.setattr(workspace_mru, "get_platform", lambda: fake_platform)
    return target


@pytest.mark.asyncio
async def test_file_picker_mount_does_not_wait_for_recent_paths(tmp_path) -> None:
    app = _FilePickerApp()
    provider = _BlockingRecentPaths()

    async with app.run_test() as pilot:
        screen = FilePicker(initial_path=str(tmp_path), recent_paths=provider)

        async def mount_screen() -> None:
            await app.push_screen(screen)

        mount_task = asyncio.create_task(mount_screen())
        try:
            await wait_for(
                provider.started.is_set,
                pilot=pilot,
                timeout=30,
                description="file picker recent paths background task",
            )
            assert not provider.finished.is_set()
            await wait_for(
                mount_task.done,
                pilot=pilot,
                timeout=30,
                description="file picker mount while recent paths are blocked",
            )
            await mount_task

            assert not provider.finished.is_set()
            assert screen.query_one("#fsd-favorites", OptionList).option_count > 0
        finally:
            provider.release()
            await asyncio.wait_for(mount_task, timeout=10)


@pytest.mark.asyncio
async def test_file_picker_parent_entry_navigates_to_parent(tmp_path) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)

    app = _FilePickerApp()
    async with app.run_test() as pilot:
        screen = FilePicker(mode=FilePickerMode.FOLDER, initial_path=str(child))
        await app.push_screen(screen)
        tree = screen.query_one("#fsd-tree", _FilteredDirectoryTree)
        await tree.reload()
        await pilot.pause()

        parent_entry = tree.root.children[0]
        assert parent_entry.label.plain == ".."
        assert tree.is_parent_navigation_node(parent_entry)

        tree.select_node(parent_entry)
        await wait_for(
            lambda: tree.path == parent and screen._selected_path == str(parent),
            timeout=2.0,
            interval=0.05,
            pilot=pilot,
            description="parent navigation selection",
        )

        assert tree.path == parent
        assert screen._selected_path == str(parent)


@pytest.mark.asyncio
async def test_file_picker_parent_entry_clears_file_selection(tmp_path) -> None:
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    selected_file = child / "selected.txt"
    selected_file.write_text("selected", encoding="utf-8")

    app = _FilePickerApp()
    async with app.run_test() as pilot:
        screen = FilePicker(mode=FilePickerMode.FILE, initial_path=str(child))
        await app.push_screen(screen)
        tree = screen.query_one("#fsd-tree", _FilteredDirectoryTree)
        await tree.reload()
        await pilot.pause()
        screen._selected_path = str(selected_file)

        tree.select_node(tree.root.children[0])
        await wait_for(
            lambda: tree.path == parent and screen._selected_path is None,
            timeout=2.0,
            interval=0.05,
            pilot=pilot,
            description="file-mode parent navigation clears selection",
        )

        assert tree.path == parent
        assert screen._selected_path is None


@pytest.mark.asyncio
async def test_file_picker_parent_entry_does_not_block_normal_directory_expansion(tmp_path) -> None:
    child = tmp_path / "child"
    nested = child / "nested"
    leaf = nested / "leaf"
    leaf.mkdir(parents=True)

    app = _FilePickerApp()
    async with app.run_test() as pilot:
        screen = FilePicker(mode=FilePickerMode.FOLDER, initial_path=str(child))
        await app.push_screen(screen)
        tree = screen.query_one("#fsd-tree", _FilteredDirectoryTree)
        await tree.reload()
        await pilot.pause()

        nested_node = next(node for node in tree.root.children if node.data is not None and node.data.path == nested)
        nested_node.expand()
        await pilot.pause()

        assert [node.data.path for node in nested_node.children if node.data is not None] == [leaf]


# ──────────── deleted initial folder ───────────────────────────────────


def test_nearest_existing_dir_walks_up_to_a_folder_that_exists(tmp_path: Path) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    file = folder / "notes.txt"
    file.write_text("notes", encoding="utf-8")

    assert _nearest_existing_dir(str(folder)) == str(folder)
    assert _nearest_existing_dir(str(folder / "deleted" / "nested")) == str(folder)
    # A file is not a folder to open at.
    assert _nearest_existing_dir(str(file)) == str(folder)
    assert _nearest_existing_dir(str(file / "child")) == str(folder)
    # Relative paths are taken from the process cwd, as before.
    assert _nearest_existing_dir(os.path.join("deleted", "nested")) == os.getcwd()


def test_nearest_existing_dir_is_none_when_no_ancestor_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # As on Windows for a drive that is no longer there: not even the root is a folder.
    shadow = ModuleType("os")
    shadow.path = SimpleNamespace(  # type: ignore[attr-defined]
        abspath=os.path.abspath, expanduser=os.path.expanduser, dirname=os.path.dirname, isdir=lambda _path: False
    )
    monkeypatch.setattr(file_picker_module, "os", shadow)

    assert _nearest_existing_dir(str(tmp_path / "gone")) is None
    # The picker then opens where the app runs.
    assert FilePicker(initial_path=str(tmp_path / "gone"))._initial_path == safe_getcwd()


def test_file_picker_without_a_process_cwd_opens_home_for_a_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process cwd can be the deleted working directory; only a relative path needs it."""
    home = tmp_path / "home"
    (home / "deleted").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    gone = tmp_path / "gone"
    gone.mkdir()
    monkeypatch.chdir(gone)
    try:
        gone.rmdir()
    except OSError:
        pytest.skip("this OS cannot remove the current directory")

    assert _nearest_existing_dir("sessions") is None
    assert FilePicker(initial_path="sessions")._initial_path == str(home)
    # "~" is expanded first, so it never needs the process cwd.
    assert FilePicker(initial_path=os.path.join("~", "deleted", "nested"))._initial_path == str(home / "deleted")


@pytest.mark.asyncio
async def test_file_picker_opens_a_deleted_folder_at_its_nearest_existing_parent(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    sibling = parent / "sibling"
    sibling.mkdir(parents=True)

    app = _FilePickerApp()
    async with app.run_test() as pilot:
        screen = FilePicker(mode=FilePickerMode.FOLDER, initial_path=str(parent / "deleted" / "nested"))
        await app.push_screen(screen)
        tree = screen.query_one("#fsd-tree", _FilteredDirectoryTree)
        await wait_for(
            lambda: any(node.data is not None and node.data.path == sibling for node in tree.root.children),
            pilot=pilot,
            description="the parent's folders listed",
        )

        assert screen._initial_path == str(parent)
        assert tree.path == parent


# ──────────── WorkspaceMruRecentDirs provider ──────────────────────────


@pytest.mark.asyncio
async def test_provider_missing_index_seeds_once_from_sessions(mru_config_dir: Path, tmp_path: Path) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    extra = tmp_path / "extra"
    for d in (repo_a, repo_b, extra):
        d.mkdir()
    store = _FakeStateStore(
        tmp_path / "sessions",
        [
            _meta(str(repo_a), working_dirs=[str(extra)], offset_seconds=20),
            _meta(str(repo_b), offset_seconds=10),
        ],
    )
    provider = WorkspaceMruRecentDirs(store, max_entries=20)

    paths = await provider.get_recent_paths()

    assert paths == [
        (format_recent_path_label(str(repo_a)), str(repo_a)),
        (format_recent_path_label(str(extra)), str(extra)),
        (format_recent_path_label(str(repo_b)), str(repo_b)),
    ]
    assert store.list_sessions_calls == 1
    assert workspace_mru_exists()

    # Seeded once — the second open reads the index without another scan.
    assert await provider.get_recent_paths() == paths
    assert store.list_sessions_calls == 1


@pytest.mark.asyncio
async def test_provider_seed_reads_naive_session_timestamps_as_utc(mru_config_dir: Path, tmp_path: Path) -> None:
    """One session saved without an offset must not cost the picker its whole seed."""
    older = tmp_path / "repo-older"
    newer = tmp_path / "repo-naive"
    for directory in (older, newer):
        directory.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    for session_id, directory, stamp in (
        ("older", older, "2026-09-01T09:00:00+00:00"),
        ("naive", newer, "2026-09-02T09:30:00"),
    ):
        await store.save_session(session_id, {"messages": [Message("user", ["hi"])]}, primary_cwd=str(directory))
        path = store.session_dir(session_id) / "session.json"
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["meta"]["updated_at"] = stamp
        path.write_text(json.dumps(envelope), encoding="utf-8")

    paths = await WorkspaceMruRecentDirs(store, max_entries=20).get_recent_paths()

    assert [path for _, path in paths] == [str(newer), str(older)]


@pytest.mark.asyncio
async def test_provider_missing_index_without_state_store_returns_empty(mru_config_dir: Path) -> None:
    provider = WorkspaceMruRecentDirs(None, max_entries=20)

    assert await provider.get_recent_paths() == []
    # Creating an empty index here would block the first real backfill.
    assert not workspace_mru_exists()


@pytest.mark.asyncio
async def test_provider_without_state_store_reads_existing_index(mru_config_dir: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    ensure_workspace_mru_index([(str(repo), datetime.now(UTC))], max_entries=20, root_key="sha256:elsewhere")

    provider = WorkspaceMruRecentDirs(None, max_entries=20)

    assert await provider.get_recent_paths() == [(format_recent_path_label(str(repo)), str(repo))]


@pytest.mark.asyncio
async def test_provider_backfilled_root_never_scans_sessions(mru_config_dir: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = _FakeStateStore(tmp_path / "sessions", [_meta(str(repo))])
    root_key = session_root_key(store.session_dir("__workspace_mru_probe__").parent)
    # Root already backfilled — even with an EMPTY index no scan may run.
    ensure_workspace_mru_index([], max_entries=20, root_key=root_key)

    provider = WorkspaceMruRecentDirs(store, max_entries=20)

    assert await provider.get_recent_paths() == []
    assert store.list_sessions_calls == 0


@pytest.mark.asyncio
async def test_provider_new_session_root_backfills_once_into_shared_index(mru_config_dir: Path, tmp_path: Path) -> None:
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    store_a = _FakeStateStore(tmp_path / "sessions-a", [_meta(str(repo_a), offset_seconds=10)])
    store_b = _FakeStateStore(tmp_path / "sessions-b", [_meta(str(repo_b), offset_seconds=20)])

    await WorkspaceMruRecentDirs(store_a, max_entries=20).get_recent_paths()
    provider_b = WorkspaceMruRecentDirs(store_b, max_entries=20)
    merged = await provider_b.get_recent_paths()

    # Root B seeds once and merges into the same global index.
    assert store_b.list_sessions_calls == 1
    assert [path for _, path in merged] == [str(repo_b), str(repo_a)]

    await provider_b.get_recent_paths()
    assert store_b.list_sessions_calls == 1


@pytest.mark.asyncio
async def test_provider_event_touch_before_seed_does_not_create_index(mru_config_dir: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    store = _FakeStateStore(tmp_path / "sessions", [_meta(str(repo))])

    # Event-driven touch fires before any picker open: it must not create the
    # index, and the later provider call must still run the session seed.
    schedule_workspace_mru_touches([str(repo)], max_entries=20, used_at=datetime.now(UTC))
    while workspace_mru._BACKGROUND_TASKS:
        await asyncio.gather(*list(workspace_mru._BACKGROUND_TASKS), return_exceptions=True)
    assert not workspace_mru_exists()

    paths = await WorkspaceMruRecentDirs(store, max_entries=20).get_recent_paths()

    assert [path for _, path in paths] == [str(repo)]
    assert store.list_sessions_calls == 1


@pytest.mark.asyncio
async def test_provider_disabled_max_entries_never_scans_or_creates(mru_config_dir: Path, tmp_path: Path) -> None:
    store = _FakeStateStore(tmp_path / "sessions", [_meta(str(tmp_path))])

    provider = WorkspaceMruRecentDirs(store, max_entries=0)

    assert await provider.get_recent_paths() == []
    assert store.list_sessions_calls == 0
    assert not workspace_mru_exists()


@pytest.mark.asyncio
async def test_provider_does_not_rescan_after_corrupt_index_repair(mru_config_dir: Path, tmp_path: Path) -> None:
    """A touch that repairs a corrupt index must not reopen the backfill.

    The rebuilt file carries the wildcard backfill marker; without it the
    repaired (now valid) index would report the root as never backfilled and
    the next picker open would re-run the full session scan.
    """
    repo = tmp_path / "repo"
    touched = tmp_path / "touched"
    repo.mkdir()
    touched.mkdir()
    store = _FakeStateStore(tmp_path / "sessions", [_meta(str(repo))])
    root_key = session_root_key(store.session_dir("__workspace_mru_probe__").parent)
    ensure_workspace_mru_index([], max_entries=20, root_key=root_key)

    workspace_mru.workspace_mru_path().write_text("{not json", encoding="utf-8")
    assert workspace_mru.record_workspace_mru_uses([str(touched)], max_entries=20, used_at=datetime.now(UTC))

    paths = await WorkspaceMruRecentDirs(store, max_entries=20).get_recent_paths()

    assert [path for _, path in paths] == [str(touched)]
    assert store.list_sessions_calls == 0


@pytest.mark.asyncio
async def test_provider_marks_root_backfilled_even_when_scan_fails(mru_config_dir: Path, tmp_path: Path) -> None:
    class _BrokenStateStore(_FakeStateStore):
        async def list_sessions(self) -> list[object]:
            self.list_sessions_calls += 1
            raise OSError("sessions dir unreadable")

    store = _BrokenStateStore(tmp_path / "sessions")
    provider = WorkspaceMruRecentDirs(store, max_entries=20)

    assert await provider.get_recent_paths() == []
    assert workspace_mru_exists()

    # The failed scan is not retried on every picker open.
    assert await provider.get_recent_paths() == []
    assert store.list_sessions_calls == 1
