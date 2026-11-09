# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for @ file suggestions: cold scans, index builds, debounced queries, and their races."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from textual.worker import WorkerCancelled

from chrys.app.tui.screens.main import suggestions as suggestions_module
from chrys.app.tui.screens.main.suggestions import SuggestionHandler
from chrys.app.tui.widgets.chrome.file_index import ProjectPathIndex, WorkspacePathIndex
from chrys.app.tui.widgets.chrome.file_scanner import ProjectPathScanResult, ProjectPathSuggestion
from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionItem, SuggestionList
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import (
    DeferredWorker,
    SuggestionScreen,
    make_suggestion_handler,
    make_suggestion_screen,
    scan_result,
    suggestion_values,
    wait_for_file_query,
)
from tests.support.waiting import MAIN_SCREEN_BUILD_TIMEOUT, wait_for

if TYPE_CHECKING:
    from textual.pilot import Pilot

    from chrys.app.tui.app import ChrysApp
    from chrys.app.tui.screens.main.screen import MainScreen

_SCAN_TARGET = "chrys.app.tui.widgets.chrome.file_scanner.scan_project_paths"

type _ScanFake = Callable[[str], Awaitable[ProjectPathScanResult]]


def _install_file_rows(
    handler: SuggestionHandler,
    root: str,
    rows: Sequence[ProjectPathSuggestion],
) -> WorkspacePathIndex:
    """Install a snapshot for *root* so ``@`` needs no scan to answer."""
    handler._state.workspace_marker.current_cwd = root
    index = handler._file_index_for(root)
    index.install(scan_result(root, list(rows)))
    return index


def _file_rows(handler: SuggestionHandler, *, limit: int = 30) -> list[str]:
    """Read the rows the resident snapshot would answer the empty query with."""
    snapshot = handler._file_snapshot()
    return [] if snapshot is None else [suggestion.path for suggestion in snapshot.head(limit=limit)]


def _patch_scan(
    monkeypatch: pytest.MonkeyPatch,
    source: Mapping[str, Sequence[str]] | _ScanFake,
    *,
    roots: list[str] | None = None,
) -> None:
    """Point the project-path scanner at a fake.

    ``source`` is either a coroutine function taking the scan root, or a
    mapping from root to the file paths that root yields. Every scanned root
    is appended to ``roots`` when one is supplied.
    """
    if isinstance(source, Mapping):
        paths_by_root = source

        async def scan(root: str) -> ProjectPathScanResult:
            return scan_result(root, [ProjectPathSuggestion(path=path, kind="file") for path in paths_by_root[root]])
    else:
        scan = source

    async def record(root: str) -> ProjectPathScanResult:
        if roots is not None:
            roots.append(root)
        return await scan(root)

    monkeypatch.setattr(_SCAN_TARGET, record)


@dataclass(slots=True)
class _ColdScanGate:
    """A cold scan held open on the event loop until the test releases it."""

    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)
    roots: list[str] = field(default_factory=list)


def _hold_cold_scan(
    monkeypatch: pytest.MonkeyPatch,
    *,
    paths: Sequence[str] = ("stale.py",),
) -> _ColdScanGate:
    """Block the cold scan until ``gate.release`` is set."""
    gate = _ColdScanGate()

    async def scan(root: str) -> ProjectPathScanResult:
        gate.started.set()
        await gate.release.wait()
        return scan_result(root, [ProjectPathSuggestion(path=path, kind="file") for path in paths])

    _patch_scan(monkeypatch, scan, roots=gate.roots)
    return gate


@dataclass(slots=True)
class _IndexBuildGate:
    """An index build paused on its worker thread until the test releases it."""

    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)
    builds: int = 0


def _hold_index_build(
    monkeypatch: pytest.MonkeyPatch,
    handler: SuggestionHandler,
    *,
    first_build_only: bool = False,
) -> _IndexBuildGate:
    """Block the off-loop index build until ``gate.release`` is set."""
    gate = _IndexBuildGate()
    original_build = ProjectPathIndex.build

    def build(scan: ProjectPathScanResult) -> ProjectPathIndex:
        gate.builds += 1
        if not first_build_only or gate.builds == 1:
            gate.started.set()
            gate.release.wait(timeout=1)
        return original_build(scan)

    monkeypatch.setattr(ProjectPathIndex, "build", build)
    return gate


def test_file_suggestions_rescan_when_cwd_changes_without_explicit_invalidation(monkeypatch) -> None:
    """The @ file cache is scoped by cwd, not just by prior scan existence."""
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    screen._set_workspace_cwd("/repo-a")
    scans: list[str] = []

    def fake_getcwd() -> str:
        raise AssertionError("tracked workspace cwd should be used before process cwd")

    monkeypatch.setattr("chrys.foundation.platform.safe_getcwd", fake_getcwd)
    _patch_scan(monkeypatch, {"/repo-a": ["a.py"], "/repo-b": ["b.py"]}, roots=scans)

    handler._suggestion_mode = "files"
    asyncio.run(handler.show_file_suggestions_async())
    assert suggestion_values(screen.suggestion_list.last_items) == ["a.py"]

    screen._set_workspace_cwd("/repo-b")
    asyncio.run(handler.show_file_suggestions_async())

    assert scans == ["/repo-a", "/repo-b"]
    assert suggestion_values(screen.suggestion_list.last_items) == ["b.py"]


async def test_file_trigger_opens_loading_popup_before_cold_scan_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen = make_suggestion_screen()
    screen._set_workspace_cwd("/repo")
    handler = make_suggestion_handler(screen)
    gate = _hold_cold_scan(monkeypatch, paths=["ready.py"])

    handler.on_file_triggered()
    assert screen.suggestion_list.last_mode == "files"
    assert screen.suggestion_list.last_title == "Files under /repo"
    assert screen.suggestion_list.is_loading is True
    assert screen.suggestion_list.last_items == []

    task = asyncio.create_task(handler.show_file_suggestions_async())
    await gate.started.wait()
    assert screen.suggestion_list.is_loading is True

    gate.release.set()
    await task

    assert gate.roots == ["/repo"]
    assert screen.suggestion_list.is_loading is False
    assert suggestion_values(screen.suggestion_list.last_items) == ["ready.py"]


def test_file_suggestions_show_disabled_truncation_row(monkeypatch) -> None:
    """A bounded scan advertises truncation instead of silently omitting files."""
    screen = make_suggestion_screen()
    screen._set_workspace_cwd("/repo")
    handler = make_suggestion_handler(screen)

    async def truncated_scan(root: str) -> ProjectPathScanResult:
        return scan_result(
            root,
            [ProjectPathSuggestion(path="a.py", kind="file")],
            truncated=True,
            file_budget=1,
            suggestion_budget=1,
            source_truncations={"rg": True},
        )

    _patch_scan(monkeypatch, truncated_scan)

    handler._suggestion_mode = "files"
    asyncio.run(handler.show_file_suggestions_async())

    assert suggestion_values(screen.suggestion_list.last_items) == ["a.py", "__chrys_file_results_truncated__"]
    truncation_row = screen.suggestion_list.last_items[-1]
    assert isinstance(truncation_row, SuggestionItem)
    assert truncation_row.disabled is True
    assert truncation_row.kind == "status"
    assert truncation_row.disabled_reason == "1 files / 1 rows indexed"


def test_file_suggestions_show_no_file_rows_for_empty_scan(monkeypatch) -> None:
    """A session from another machine may point at a workspace with no local files."""
    screen = make_suggestion_screen()
    screen._set_workspace_cwd("Z:\\Fake\\MissingWorkspace")
    handler = make_suggestion_handler(screen)

    _patch_scan(monkeypatch, {"Z:\\Fake\\MissingWorkspace": []})

    handler._suggestion_mode = "files"
    asyncio.run(handler.show_file_suggestions_async())

    assert screen.suggestion_list.mode == "files"
    assert screen.suggestion_list.last_items == []
    assert _file_rows(handler) == []


def test_file_suggestion_enter_without_selection_does_not_submit_text() -> None:
    screen = make_suggestion_screen()
    screen.input_bar.value = "@missing"
    screen.suggestion_list.is_visible = True
    handler = make_suggestion_handler(screen)
    handler._suggestion_mode = "files"

    assert handler.on_suggestion_select(execute=True) is True

    assert screen.input_bar.value == "@missing"
    assert screen.submitted == []
    assert screen.suggestion_list.is_visible is True


def test_file_suggestions_discard_scan_when_cwd_changes_mid_scan(monkeypatch) -> None:
    """A scan result is cached only if its root is still current when it returns."""
    screen = make_suggestion_screen()
    screen._set_workspace_cwd("/repo-a")
    handler = make_suggestion_handler(screen)
    scans: list[str] = []

    def fake_getcwd() -> str:
        raise AssertionError("tracked workspace cwd should be used before process cwd")

    async def scan_and_chdir(root: str) -> ProjectPathScanResult:
        if root == "/repo-a":
            screen._set_workspace_cwd("/repo-b")
            return scan_result(root, [ProjectPathSuggestion(path="stale.py", kind="file")])
        return scan_result(root, [ProjectPathSuggestion(path="fresh.py", kind="file")])

    monkeypatch.setattr("chrys.foundation.platform.safe_getcwd", fake_getcwd)
    _patch_scan(monkeypatch, scan_and_chdir, roots=scans)

    handler._suggestion_mode = "files"
    asyncio.run(handler.show_file_suggestions_async())

    assert scans == ["/repo-a", "/repo-b"]
    assert suggestion_values(screen.suggestion_list.last_items) == ["fresh.py"]


async def test_file_suggestions_filter_directory_query() -> None:
    """A directory-shaped query should keep the matching directory entry."""
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    _install_file_rows(
        handler,
        "/repo",
        [
            ProjectPathSuggestion(path="src/chrys/app/tui/", kind="directory"),
            ProjectPathSuggestion(path="src/chrys/app/tui/widgets/chrome/file_scanner.py", kind="file"),
            ProjectPathSuggestion(path="src/chrys/orchestration/engine/engine.py", kind="file"),
        ],
    )
    handler._suggestion_mode = "files"

    handler.on_text_changed("@src/chrys/app/tui/")
    await wait_for_file_query(handler)

    assert suggestion_values(screen.suggestion_list.last_items)[0] == "src/chrys/app/tui/"
    assert isinstance(screen.suggestion_list.last_items[0], SuggestionItem)
    assert screen.suggestion_list.last_items[0].kind == "directory"


async def test_file_suggestions_filter_full_relative_path_from_injected_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct file-cache injection remains a cheap test path for file suggestions."""
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    injected = [
        ProjectPathSuggestion(path="src/chrys/app/tui/widgets/chrome/", kind="directory"),
        ProjectPathSuggestion(path="src/chrys/app/tui/widgets/chrome/file_scanner.py", kind="file"),
        ProjectPathSuggestion(path="src/chrys/orchestration/engine/engine.py", kind="file"),
    ]
    installed = _install_file_rows(handler, "/repo", injected)
    handler._suggestion_mode = "files"

    def fail_fuzzy_filter(*_args, **_kwargs) -> list[str]:
        raise AssertionError("file suggestions should query ProjectPathIndex")

    monkeypatch.setattr("chrys.app.tui.widgets.chrome.file_scanner.fuzzy_filter", fail_fuzzy_filter)

    handler.on_text_changed("@chrome")
    await wait_for_file_query(handler)

    assert handler._file_snapshot() is installed.snapshot
    assert suggestion_values(screen.suggestion_list.last_items) == [
        "src/chrys/app/tui/widgets/chrome/",
        "src/chrys/app/tui/widgets/chrome/file_scanner.py",
    ]
    assert isinstance(screen.suggestion_list.last_items[0], SuggestionItem)
    assert screen.suggestion_list.last_items[0].kind == "directory"


async def test_discard_file_index_drops_the_resident_snapshot() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    _install_file_rows(handler, "/repo", [ProjectPathSuggestion(path="src/chrome/file.py", kind="file")])
    handler._suggestion_mode = "files"

    handler.on_text_changed("@chrome")
    await wait_for_file_query(handler)
    assert handler._file_snapshot() is not None

    handler.discard_file_index()

    assert handler._file_snapshot() is None
    assert handler._file_index is None


async def test_a_workspace_switch_replaces_the_resident_snapshot() -> None:
    """The snapshot belongs to one root: another root starts cold instead of reusing it."""
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    old = _install_file_rows(handler, "/repo-a", [ProjectPathSuggestion(path="a.py", kind="file")])

    screen._set_workspace_cwd("/repo-b")

    assert handler._file_snapshot() is None
    replacement = handler._file_index_for("/repo-b")

    assert replacement is not old
    assert replacement.snapshot is None


async def test_dismissed_suggestions_do_not_receive_stale_file_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(suggestions_module, "_FILE_QUERY_DEBOUNCE_SECONDS", 0)
    screen = make_suggestion_screen()
    screen._set_workspace_cwd("/repo")
    handler = make_suggestion_handler(screen)
    _install_file_rows(handler, "/repo", [ProjectPathSuggestion(path="src/chrome/file.py", kind="file")])
    handler._suggestion_mode = "files"

    handler.on_text_changed("@chrome")
    handler.dismiss_suggestions()
    await wait_for_file_query(handler)

    assert screen.suggestion_list.is_visible is False
    assert screen.suggestion_list.last_items == []


async def test_switching_modes_invalidates_pending_file_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(suggestions_module, "_FILE_QUERY_DEBOUNCE_SECONDS", 0)
    screen = make_suggestion_screen()
    screen._set_workspace_cwd("/repo")
    handler = make_suggestion_handler(screen)
    _install_file_rows(handler, "/repo", [ProjectPathSuggestion(path="src/chrome/file.py", kind="file")])
    handler._suggestion_mode = "files"

    handler.on_text_changed("@chrome")
    handler._show_suggestions("commands", [])
    await wait_for_file_query(handler)

    assert screen.suggestion_list.mode == "commands"
    assert suggestion_values(screen.suggestion_list.last_items) == []


async def test_rapid_file_typing_keeps_one_active_query_and_runs_latest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(suggestions_module, "_FILE_QUERY_DEBOUNCE_SECONDS", 0)
    screen = make_suggestion_screen()
    screen._set_workspace_cwd("/repo")
    handler = make_suggestion_handler(screen)
    handler._suggestion_mode = "files"
    _install_file_rows(handler, "/repo", [ProjectPathSuggestion(path="placeholder.py", kind="file")])

    lock = threading.Lock()
    active = 0
    max_active = 0
    calls: list[str] = []

    class SlowIndex(ProjectPathIndex):
        def query(self, query: str, *, limit: int = 30) -> list[ProjectPathSuggestion]:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                calls.append(query)
                time.sleep(0.03)
                return [ProjectPathSuggestion(path=f"{query}.py", kind="file")]
            finally:
                with lock:
                    active -= 1

    index = handler._file_index_for("/repo")
    index.snapshot = SlowIndex(scan_result("/repo", []))

    handler.on_text_changed("@alpha")
    await asyncio.sleep(0.01)
    handler.on_text_changed("@beta")
    handler.on_text_changed("@gamma")
    await wait_for_file_query(handler)

    assert max_active == 1
    assert calls[-1] == "gamma"
    assert "beta" not in calls
    assert suggestion_values(screen.suggestion_list.last_items) == ["gamma.py"]


async def test_typing_file_query_before_cold_scan_completion_replays_latest_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(suggestions_module, "_FILE_QUERY_DEBOUNCE_SECONDS", 0)
    screen = make_suggestion_screen()
    screen._set_workspace_cwd("/repo")
    handler = make_suggestion_handler(screen)
    gate = _hold_cold_scan(monkeypatch, paths=["alpha.py", "src/foo.py"])

    handler._suggestion_mode = "files"
    warmup = asyncio.create_task(handler.show_file_suggestions_async())
    await gate.started.wait()
    handler.on_text_changed("@foo")
    gate.release.set()
    await warmup
    await wait_for_file_query(handler)

    assert gate.roots == ["/repo"]
    assert suggestion_values(screen.suggestion_list.last_items) == ["src/foo.py"]


async def test_cache_invalidation_during_index_build_discards_stale_warmup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen = make_suggestion_screen()
    screen._set_workspace_cwd("/repo")
    handler = make_suggestion_handler(screen)
    scans: list[str] = []

    async def stale_then_fresh_scan(root: str) -> ProjectPathScanResult:
        path = "stale.py" if len(scans) == 1 else "fresh.py"
        return scan_result(root, [ProjectPathSuggestion(path=path, kind="file")])

    _patch_scan(monkeypatch, stale_then_fresh_scan, roots=scans)
    gate = _hold_index_build(monkeypatch, handler, first_build_only=True)

    handler._suggestion_mode = "files"
    warmup = asyncio.create_task(handler.show_file_suggestions_async())
    await asyncio.to_thread(gate.started.wait, 1)
    handler.discard_file_index()
    gate.release.set()
    await warmup

    assert scans == ["/repo", "/repo"]
    assert _file_rows(handler) == ["fresh.py"]
    assert suggestion_values(screen.suggestion_list.last_items) == ["fresh.py"]


@dataclass(frozen=True, slots=True)
class _WarmupHold:
    """A warmup paused at one point of the cold-scan then index-build pipeline."""

    wait_until_held: Callable[[], Awaitable[None]]
    release: Callable[[], None]
    roots: list[str]


def _hold_at_cold_scan(monkeypatch: pytest.MonkeyPatch, _handler: SuggestionHandler) -> _WarmupHold:
    gate = _hold_cold_scan(monkeypatch)

    async def wait_until_held() -> None:
        await gate.started.wait()

    return _WarmupHold(wait_until_held=wait_until_held, release=gate.release.set, roots=gate.roots)


def _hold_at_index_build(monkeypatch: pytest.MonkeyPatch, handler: SuggestionHandler) -> _WarmupHold:
    roots: list[str] = []
    _patch_scan(monkeypatch, {"/repo": ["stale.py"]}, roots=roots)
    gate = _hold_index_build(monkeypatch, handler)

    async def wait_until_held() -> None:
        await asyncio.to_thread(gate.started.wait, 1)

    return _WarmupHold(wait_until_held=wait_until_held, release=gate.release.set, roots=roots)


def _dismiss_suggestions(_screen: SuggestionScreen, handler: SuggestionHandler) -> None:
    handler.dismiss_suggestions()


def _switch_to_commands(_screen: SuggestionScreen, handler: SuggestionHandler) -> None:
    handler._show_suggestions("commands", [])


def _detach_screen(screen: SuggestionScreen, _handler: SuggestionHandler) -> None:
    screen.is_attached = False


@dataclass(frozen=True, slots=True)
class _Interruption:
    """One way a warmup is cut short, and the popup state that must survive it."""

    hold: Callable[[pytest.MonkeyPatch, SuggestionHandler], _WarmupHold]
    interrupt: Callable[[SuggestionScreen, SuggestionHandler], None]
    expected_mode: str
    expected_visible: bool


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            _Interruption(_hold_at_cold_scan, _dismiss_suggestions, "", False),
            id="dismiss-during-cold-scan",
        ),
        pytest.param(
            _Interruption(_hold_at_index_build, _switch_to_commands, "commands", True),
            id="mode-switch-during-index-build",
        ),
        pytest.param(
            _Interruption(_hold_at_cold_scan, _detach_screen, "", False),
            id="detach-during-cold-scan",
        ),
    ],
)
async def test_interrupting_a_file_warmup_never_reopens_file_suggestions(
    monkeypatch: pytest.MonkeyPatch,
    case: _Interruption,
) -> None:
    """Dismiss, mode switch, and detach all cut the cold build short: no follow-up
    scan is queued and the popup is not reopened.  A scan that already finished
    still installs its snapshot — that is what makes the next ``@`` free.
    """
    screen = make_suggestion_screen()
    screen._set_workspace_cwd("/repo")
    handler = make_suggestion_handler(screen)
    hold = case.hold(monkeypatch, handler)

    handler._suggestion_mode = "files"
    warmup = asyncio.create_task(handler.show_file_suggestions_async())
    await hold.wait_until_held()
    case.interrupt(screen, handler)
    hold.release()
    await warmup

    assert hold.roots == ["/repo"]
    assert handler._file_rescan_requested is False
    assert screen.suggestion_list.last_items == []
    assert screen.suggestion_list.mode == case.expected_mode
    assert screen.suggestion_list.is_visible is case.expected_visible


async def test_dismiss_before_warmup_starts_does_not_restore_file_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen = make_suggestion_screen()
    screen._set_workspace_cwd("/repo")
    handler = make_suggestion_handler(screen)
    scans: list[str] = []
    _patch_scan(monkeypatch, {"/repo": ["stale.py"]}, roots=scans)

    handler._suggestion_mode = "files"
    warmup = asyncio.create_task(handler.show_file_suggestions_async())
    handler.dismiss_suggestions()
    await warmup

    assert scans == []
    assert handler.suggestion_mode is None
    assert screen.suggestion_list.is_visible is False


async def test_mode_switch_before_warmup_worker_scans_skips_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen = make_suggestion_screen()
    screen._set_workspace_cwd("/repo")
    handler = make_suggestion_handler(screen)
    scans: list[str] = []
    deferred_workers: list[DeferredWorker] = []

    def run_worker(work, **_kwargs) -> DeferredWorker:
        worker = DeferredWorker(work)
        deferred_workers.append(worker)
        return worker

    monkeypatch.setattr(screen, "run_worker", run_worker)
    _patch_scan(monkeypatch, {"/repo": ["stale.py"]}, roots=scans)

    handler._suggestion_mode = "files"
    warmup = asyncio.create_task(handler.show_file_suggestions_async())
    await asyncio.sleep(0)
    assert deferred_workers

    handler._show_suggestions("commands", [])
    await warmup

    assert scans == []
    assert handler._file_snapshot() is None
    assert screen.suggestion_list.mode == "commands"


async def test_detached_screen_does_not_receive_file_query_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(suggestions_module, "_FILE_QUERY_DEBOUNCE_SECONDS", 0)
    screen = make_suggestion_screen()
    screen._set_workspace_cwd("/repo")
    handler = make_suggestion_handler(screen)
    _install_file_rows(handler, "/repo", [ProjectPathSuggestion(path="src/chrome/file.py", kind="file")])
    handler._suggestion_mode = "files"

    handler.on_text_changed("@chrome")
    screen.is_attached = False
    await wait_for_file_query(handler)

    assert screen.suggestion_list.last_items == []


async def test_index_warmup_build_does_not_starve_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    screen = make_suggestion_screen()
    screen._set_workspace_cwd("/repo")
    handler = make_suggestion_handler(screen)
    original_build = ProjectPathIndex.build

    async def large_scan(root: str) -> ProjectPathScanResult:
        return scan_result(
            root,
            [ProjectPathSuggestion(path=f"src/file_{index:04d}.py", kind="file") for index in range(1000)],
        )

    loop_kept_ticking = threading.Event()

    def slow_build_file_index(scan: ProjectPathScanResult):
        # Block the worker until the event loop has demonstrably kept
        # ticking underneath the build.  If the build ran on the loop
        # thread instead, this could never be set — the timeout path
        # finishes the warmup with the tick count still at ~0 and the
        # assertion fails.  A handshake instead of a fixed sleep: tick
        # counting against wall-clock flakes on Windows' coarse timer.
        loop_kept_ticking.wait(timeout=5.0)
        return original_build(scan)

    _patch_scan(monkeypatch, large_scan)
    monkeypatch.setattr(ProjectPathIndex, "build", slow_build_file_index)

    handler._suggestion_mode = "files"
    warmup = asyncio.create_task(handler.show_file_suggestions_async())
    ticks = 0
    while not warmup.done():
        ticks += 1
        if ticks > 3:
            loop_kept_ticking.set()
        await asyncio.sleep(0.005)
    await warmup

    assert ticks > 3
    assert suggestion_values(screen.suggestion_list.last_items)[:1] == ["src/file_0000.py"]


def test_selecting_directory_suggestion_inserts_trailing_slash() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)

    handler.on_suggestion_selected("files", "src/chrys", execute=False, kind="directory")

    assert screen.input_bar.replacements == [("@", "@src/chrys/ ")]


def test_selecting_file_status_suggestion_is_ignored() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)

    handler.on_suggestion_selected("files", "__chrys_file_results_truncated__", execute=False, kind="status")

    assert screen.input_bar.replacements == []
    assert screen.suggestion_list.is_visible is False


# --------------------------------------------------------------------------- #
# Resident snapshot: answered at once, rescanned in the background when stale
# --------------------------------------------------------------------------- #


def _screen_at(root: str) -> SuggestionScreen:
    screen = make_suggestion_screen()
    screen._set_workspace_cwd(root)
    return screen


def _rows(*paths: str) -> list[ProjectPathSuggestion]:
    return [ProjectPathSuggestion(path=path, kind="file") for path in paths]


@dataclass(slots=True)
class _ScanSequence:
    """A fake scanner answering each scan with the next row set, optionally held open."""

    results: list[list[str] | BaseException]
    roots: list[str] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event | None = None

    async def __call__(self, root: str) -> ProjectPathScanResult:
        self.roots.append(root)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return scan_result(root, _rows(*result))


async def test_a_current_snapshot_answers_the_trigger_without_scanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen = _screen_at("/repo")
    handler = make_suggestion_handler(screen)
    scans = _ScanSequence(results=[])
    _patch_scan(monkeypatch, scans)
    _install_file_rows(handler, "/repo", _rows("a.py"))

    started: list[Callable[[], Awaitable[object]]] = []
    handler._callbacks = replace(handler._callbacks, start_worker=started.append)

    handler.on_file_triggered()
    await handler.show_file_suggestions_async()

    assert started == []
    assert scans.roots == []
    assert screen.suggestion_list.is_loading is False
    assert suggestion_values(screen.suggestion_list.last_items) == ["a.py"]


async def test_after_a_turn_the_stale_snapshot_answers_while_the_rescan_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn end rescans even when the agent changed nothing it recorded.

    The file here was created outside the agent (an editor, a script), which the
    mutation tracker never sees: only a rescan can list it.
    """
    screen = _screen_at("/repo")
    handler = make_suggestion_handler(screen)
    scans = _ScanSequence(results=[["a.py", "notes.md"]], release=asyncio.Event())
    _patch_scan(monkeypatch, scans)
    _install_file_rows(handler, "/repo", _rows("a.py"))

    started: list[Callable[[], Awaitable[object]]] = []
    handler._callbacks = replace(handler._callbacks, start_worker=started.append)

    handler.mark_file_index_stale()
    handler.on_file_triggered()

    assert screen.suggestion_list.is_loading is False
    assert suggestion_values(screen.suggestion_list.last_items) == ["a.py"]
    assert started == [handler.show_file_suggestions_async]

    shown = asyncio.create_task(started[0]())
    await wait_for(lambda: scans.started.is_set() or shown.done(), description="background rescan started")
    assert not shown.done()
    assert suggestion_values(screen.suggestion_list.last_items) == ["a.py"]

    scans.release.set()
    await shown

    assert scans.roots == ["/repo"]
    assert suggestion_values(screen.suggestion_list.last_items) == ["a.py", "notes.md"]
    assert handler._file_index is not None and handler._file_index.current


async def test_a_rescan_that_finished_before_the_queued_worker_started_swaps_in_keeping_the_selection() -> None:
    """Reopening ``@`` while an earlier rescan finishes shows the stale rows; the user may choose from them."""
    screen = _screen_at("/repo")
    handler = make_suggestion_handler(screen)
    index = _install_file_rows(handler, "/repo", _rows("a.py", "b.py"))
    started: list[Callable[[], Awaitable[object]]] = []
    handler._callbacks = replace(handler._callbacks, start_worker=started.append)
    handler.mark_file_index_stale()
    handler.on_file_triggered()
    assert started == [handler.show_file_suggestions_async]

    index.install(scan_result("/repo", _rows("a.py", "b.py", "c.py")))
    await started[0]()

    assert suggestion_values(screen.suggestion_list.last_items) == ["a.py", "b.py", "c.py"]
    assert screen.suggestion_list.last_keep_selection is True


@pytest.mark.parametrize("files_shown", [False, True], ids=["popup-closed", "popup-open"])
async def test_a_turn_end_only_marks_the_snapshot_stale(monkeypatch: pytest.MonkeyPatch, files_shown: bool) -> None:
    """The rescan waits for the next ``@``: the turn's after-turn hooks may still be changing files."""
    screen = _screen_at("/repo")
    handler = make_suggestion_handler(screen)
    scans = _ScanSequence(results=[])
    _patch_scan(monkeypatch, scans)
    index = _install_file_rows(handler, "/repo", _rows("a.py"))
    if files_shown:
        handler.on_file_triggered()

    handler.mark_file_index_stale()

    assert handler._file_warmup_worker is None
    assert scans.roots == []
    assert index.stale is True


@dataclass(slots=True)
class _EngineInAfterTurnHooks:
    """The engine surface the turn end reads: the turn's lifecycle task, still running its hooks."""

    turn_lifecycle_task: asyncio.Task[bool]


@pytest.mark.parametrize("resident", [True, False], ids=["resident-snapshot", "first-at"])
async def test_a_scan_that_ran_before_the_after_turn_hooks_finished_is_stale_again(
    monkeypatch: pytest.MonkeyPatch,
    resident: bool,
) -> None:
    """After-turn hooks run once the turn's UI stops and may create or delete files.

    The first ``@`` of a session may come while they run, with no snapshot yet.
    """
    screen = _screen_at("/repo")
    handler = make_suggestion_handler(screen)
    scans = _ScanSequence(results=[["a.py"], ["a.py", "hook.log"]])
    _patch_scan(monkeypatch, scans)
    if resident:
        _install_file_rows(handler, "/repo", _rows("a.py"))
    hooks_done = asyncio.Event()
    lifecycle = asyncio.create_task(hooks_done.wait())
    screen.services.engine_provider = lambda: _EngineInAfterTurnHooks(lifecycle)
    try:
        handler.mark_file_index_stale()
        handler.on_file_triggered()
        await handler.show_file_suggestions_async()
        index = handler._file_index
        assert index is not None
        assert index.current is True

        hooks_done.set()
        await lifecycle
        assert index.stale is True

        handler.on_file_triggered()
        await handler.show_file_suggestions_async()
    finally:
        hooks_done.set()
        await lifecycle

    assert scans.roots == ["/repo", "/repo"]
    assert suggestion_values(screen.suggestion_list.last_items) == ["a.py", "hook.log"]


async def test_a_keystroke_racing_the_rescan_still_resets_the_highlight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new query's rows start at its best match, even when a rescan lands while its search runs."""
    screen = _screen_at("/repo")
    handler = make_suggestion_handler(screen)
    _patch_scan(monkeypatch, _ScanSequence(results=[["b.py", "x/b2.py", "ab.py"]]))
    _install_file_rows(handler, "/repo", _rows("b.py", "x/b2.py"))
    handler.mark_file_index_stale()
    searching = threading.Event()
    release_search = threading.Event()
    original_query = ProjectPathIndex.query

    def query(index: ProjectPathIndex, text: str, *, limit: int = 30) -> list[ProjectPathSuggestion]:
        if not searching.is_set():
            searching.set()
            release_search.wait(timeout=5)
        return original_query(index, text, limit=limit)

    monkeypatch.setattr(ProjectPathIndex, "query", query)
    handler._callbacks = replace(handler._callbacks, start_worker=lambda _work: None)
    handler.on_file_triggered()
    try:
        handler.on_text_changed("@b")
        await wait_for(searching.is_set, description="the keystroke's search runs off the loop")

        await handler.show_file_suggestions_async()
    finally:
        release_search.set()
    await wait_for_file_query(handler)

    assert suggestion_values(screen.suggestion_list.last_items) == ["b.py", "ab.py", "x/b2.py"]
    assert screen.suggestion_list.last_keep_selection is False


async def test_a_turn_that_ends_during_the_first_scan_scans_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first scan may have read the folder before the turn wrote to it."""
    screen = _screen_at("/repo")
    handler = make_suggestion_handler(screen)
    scans = _ScanSequence(results=[["a.py"], ["a.py", "new.py"]], release=asyncio.Event())
    _patch_scan(monkeypatch, scans)
    handler.on_file_triggered()
    shown = asyncio.create_task(handler.show_file_suggestions_async())
    await wait_for(lambda: scans.started.is_set() or shown.done(), description="first scan started")
    assert not shown.done()

    handler.mark_file_index_stale()
    scans.release.set()
    await shown

    assert scans.roots == ["/repo", "/repo"]
    assert suggestion_values(screen.suggestion_list.last_items) == ["a.py", "new.py"]
    assert handler._file_index is not None and handler._file_index.current


async def test_a_rescan_for_a_replaced_workspace_hands_over_to_the_new_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one warmup worker that outlives a workspace switch scans the new root next."""
    screen = _screen_at("/repo-a")
    handler = make_suggestion_handler(screen)
    scans = _ScanSequence(results=[["a.py"], ["b.py"]], release=asyncio.Event())
    _patch_scan(monkeypatch, scans)
    handler.on_file_triggered()
    first = asyncio.create_task(handler.show_file_suggestions_async())
    await wait_for(lambda: scans.started.is_set() or first.done(), description="old root scan started")
    assert not first.done()

    handler.discard_file_index()
    screen._set_workspace_cwd("/repo-b")
    handler.on_file_triggered()
    second = asyncio.create_task(handler.show_file_suggestions_async())
    scans.release.set()
    await asyncio.gather(first, second)

    assert scans.roots == ["/repo-a", "/repo-b"]
    assert suggestion_values(screen.suggestion_list.last_items) == ["b.py"]
    assert handler._file_index is not None and handler._file_index.root == "/repo-b"
    assert handler._file_index.current


async def test_a_failed_rescan_keeps_the_stale_snapshot_and_the_next_trigger_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen = _screen_at("/repo")
    handler = make_suggestion_handler(screen)
    scans = _ScanSequence(results=[OSError("scan failed"), ["a.py", "b.py"]])
    _patch_scan(monkeypatch, scans)
    index = _install_file_rows(handler, "/repo", _rows("a.py"))
    handler.mark_file_index_stale()

    handler.on_file_triggered()
    await handler.show_file_suggestions_async()

    assert suggestion_values(screen.suggestion_list.last_items) == ["a.py"]
    assert index.stale is True

    handler.on_file_triggered()
    await handler.show_file_suggestions_async()

    assert scans.roots == ["/repo", "/repo"]
    assert suggestion_values(screen.suggestion_list.last_items) == ["a.py", "b.py"]


async def test_a_failed_first_scan_ends_the_loading_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen = _screen_at("/repo")
    handler = make_suggestion_handler(screen)
    _patch_scan(monkeypatch, _ScanSequence(results=[OSError("scan failed")]))

    handler.on_file_triggered()
    assert screen.suggestion_list.is_loading is True
    await handler.show_file_suggestions_async()

    assert screen.suggestion_list.is_loading is False
    assert screen.suggestion_list.last_items == []


async def test_a_workspace_switch_before_the_queued_rescan_starts_scans_the_new_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A switch between queuing the worker and its first step must not drop the request."""
    screen = _screen_at("/repo-a")
    handler = make_suggestion_handler(screen)
    scans = _ScanSequence(results=[["b.py"]])
    _patch_scan(monkeypatch, scans)
    monkeypatch.setattr(screen, "run_worker", lambda work, **_kwargs: DeferredWorker(work))
    handler.on_file_triggered()
    queued = handler._request_file_rescan()

    handler.discard_file_index()
    screen._set_workspace_cwd("/repo-b")
    await queued.wait()

    assert scans.roots == ["/repo-b"]
    assert screen.suggestion_list.is_loading is False
    assert suggestion_values(screen.suggestion_list.last_items) == ["b.py"]


async def test_a_rescan_cancelled_by_unmounting_does_not_fail_the_waiting_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quitting mid-scan cancels the rescan worker, whose ``wait()`` then raises ``WorkerCancelled``."""
    screen = _screen_at("/repo")
    handler = make_suggestion_handler(screen)

    class _CancelledWorker:
        is_finished = False

        async def wait(self) -> None:
            raise WorkerCancelled("Worker was cancelled")

    def cancelled_before_it_ran(work: Coroutine[object, object, None], **_kwargs: object) -> _CancelledWorker:
        work.close()
        return _CancelledWorker()

    monkeypatch.setattr(screen, "run_worker", cancelled_before_it_ran)
    handler.on_file_triggered()

    await handler.show_file_suggestions_async()

    assert isinstance(handler._file_warmup_worker, _CancelledWorker)


def _highlighted(suggestions: SuggestionList) -> str | None:
    row = suggestions._highlighted
    return None if row is None else suggestions._values[row]


async def _ready_main_screen(app: ChrysApp, pilot: Pilot[None]) -> MainScreen:
    def ready() -> bool:
        main = app._main_screen
        return (
            main is not None
            and app.screen is main
            and not main._state.run.agent_loading
            and main.query_one("#chat-input").has_focus
        )

    await wait_for(ready, pilot=pilot, timeout=MAIN_SCREEN_BUILD_TIMEOUT, description="chat input ready")
    main = app._main_screen
    assert main is not None
    return main


@pytest.mark.parametrize("query", ["", "py"], ids=["head", "query"])
async def test_a_rescan_swapped_into_the_open_list_keeps_the_highlighted_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    """The user may already be choosing from the stale list when the rescan lands."""
    scans = _ScanSequence(results=[["a.py", "aa.py", "b.py", "c.py"]], release=asyncio.Event())
    _patch_scan(monkeypatch, scans)
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main = await _ready_main_screen(app, pilot)
        handler = main._suggestions
        index = handler._file_index_for(handler._current_file_root())
        index.install(scan_result(index.root, _rows("a.py", "b.py", "c.py")))
        index.mark_stale()
        suggestions = main.query_one(SuggestionList)

        await pilot.press("@", *query)
        await wait_for(scans.started.is_set, pilot=pilot, description="background rescan started")
        if query:
            await wait_for(
                lambda: (
                    handler._file_query_pending is None
                    and handler._file_query_worker is not None
                    and handler._file_query_worker.is_finished
                ),
                pilot=pilot,
                description="typed query answered from the stale snapshot",
            )
        assert suggestions._values == ["a.py", "b.py", "c.py"]
        await pilot.press("down", "down")
        assert _highlighted(suggestions) == "c.py"

        scans.release.set()
        await wait_for(lambda: "aa.py" in suggestions._values, pilot=pilot, description="rescan swapped in")

        assert _highlighted(suggestions) == "c.py"


async def test_a_rescan_that_pushes_the_highlighted_row_out_leaves_the_open_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Swapping rows that no longer hold the highlighted file would move Enter onto a file nobody chose."""
    stale = [f"z{n:02}.py" for n in range(30)]
    scans = _ScanSequence(results=[[f"a{n:02}.py" for n in range(30)] + stale], release=asyncio.Event())
    _patch_scan(monkeypatch, scans)
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main = await _ready_main_screen(app, pilot)
        handler = main._suggestions
        index = handler._file_index_for(handler._current_file_root())
        index.install(scan_result(index.root, _rows(*stale)))
        index.mark_stale()
        suggestions = main.query_one(SuggestionList)

        await pilot.press("@")
        await wait_for(scans.started.is_set, pilot=pilot, description="background rescan started")
        await pilot.press("down")
        assert _highlighted(suggestions) == "z01.py"

        scans.release.set()
        worker = handler._file_warmup_worker
        assert worker is not None
        await wait_for(lambda: worker.is_finished, pilot=pilot, description="rescan installed")

        assert index.current
        assert suggestions._values[:2] == ["z00.py", "z01.py"]
        assert _highlighted(suggestions) == "z01.py"
        await pilot.press("enter")
        await wait_for(
            lambda: "@z01.py" in handler._view.input_value(), pilot=pilot, description="highlighted file inserted"
        )


async def test_a_rescan_replaces_the_open_list_when_the_highlight_was_never_moved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Left on the first row, the highlight picked nothing: a file the rescan dropped must not stay offered."""
    scans = _ScanSequence(results=[["b.py", "new.py"]], release=asyncio.Event())
    _patch_scan(monkeypatch, scans)
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main = await _ready_main_screen(app, pilot)
        handler = main._suggestions
        index = handler._file_index_for(handler._current_file_root())
        index.install(scan_result(index.root, _rows("deleted.py", "b.py")))
        index.mark_stale()
        suggestions = main.query_one(SuggestionList)

        await pilot.press("@")
        await wait_for(scans.started.is_set, pilot=pilot, description="background rescan started")
        assert _highlighted(suggestions) == "deleted.py"

        scans.release.set()
        await wait_for(lambda: "new.py" in suggestions._values, pilot=pilot, description="rescan swapped in")

        assert suggestions._values == ["b.py", "new.py"]
        assert _highlighted(suggestions) == "b.py"


async def test_typing_without_an_installed_snapshot_starts_no_query_worker() -> None:
    """Without a snapshot a query waits for the first scan instead of running."""
    screen = _screen_at("/repo")
    handler = make_suggestion_handler(screen)
    handler._suggestion_mode = "files"

    handler.on_text_changed("@repo")

    assert handler._file_query_pending is None
    assert handler._file_query_worker is None
