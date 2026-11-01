# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for @ file suggestions: cold scans, index builds, debounced queries, and their races."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from chrys.app.tui.screens.main import suggestions as suggestions_module
from chrys.app.tui.screens.main.suggestions import SuggestionHandler
from chrys.app.tui.widgets.chrome.file_index import ProjectPathIndex, WorkspacePathIndex
from chrys.app.tui.widgets.chrome.file_scanner import ProjectPathScanResult, ProjectPathSuggestion
from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionItem
from chrys.service.mutations.types import MutationProvenance, MutationSource
from tests.support.tui_helpers import (
    DeferredWorker,
    SuggestionScreen,
    make_suggestion_handler,
    make_suggestion_screen,
    scan_result,
    suggestion_values,
    wait_for_file_query,
)

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
    assert old.snapshot is None


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
    assert handler._file_warmup_requested_root is None
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
# Index lifecycle: staleness after a turn, background rebuild, incremental apply
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _PeriodFake:
    mutations: tuple[SimpleNamespace, ...]
    detection_truncated: bool = False


def _engine(
    *,
    mutated: Sequence[tuple[MutationSource, MutationProvenance]] = (),
    changed: Sequence[str] = (),
    detection_truncated: bool = False,
) -> object:
    period = _PeriodFake(
        mutations=tuple(SimpleNamespace(source=source, provenance=provenance) for source, provenance in mutated),
        detection_truncated=detection_truncated,
    )
    return SimpleNamespace(
        mutation_tracker=SimpleNamespace(
            current_period=period,
            get_changed_files_set=lambda: set(changed),
        )
    )


def _no_engine() -> object:
    return SimpleNamespace(mutation_tracker=None)


def _screen_at(root: str) -> SuggestionScreen:
    screen = make_suggestion_screen()
    screen._set_workspace_cwd(root)
    return screen


async def test_the_first_at_after_a_turn_answers_without_scanning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn end schedules the rebuild; the ``@`` that follows only reads the snapshot.

    ``scan_project_paths`` is the only subprocess caller under ``@``, so counting
    it is the "no rg/git subprocess on the input path" assertion (no wall clock).
    """
    screen = _screen_at("/repo")
    handler = make_suggestion_handler(screen)
    scans: list[str] = []

    async def scan(root: str) -> ProjectPathScanResult:
        scans.append(root)
        return scan_result(root, [ProjectPathSuggestion(path="after.py", kind="file")])

    _patch_scan(monkeypatch, scan, roots=None)
    _install_file_rows(handler, "/repo", [ProjectPathSuggestion(path="before.py", kind="file")])
    screen.services.engine_provider = _no_engine

    handler.mark_file_index_dirty()
    assert handler._file_rebuild_worker is not None
    await handler._file_rebuild_worker.wait()
    scans_after_the_turn = list(scans)

    handler._suggestion_mode = "files"
    handler.on_file_triggered()
    await handler.show_file_suggestions_async()

    assert scans == scans_after_the_turn == ["/repo"]
    assert screen.suggestion_list.is_loading is False
    assert suggestion_values(screen.suggestion_list.last_items) == ["after.py"]


async def test_a_turn_end_serves_the_stale_snapshot_while_the_rebuild_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STALE: the trigger answers from the installed snapshot, then the rebuild hot-swaps."""
    screen = _screen_at("/repo")
    handler = make_suggestion_handler(screen)
    gate = _ColdScanGate()

    async def gated_scan(root: str) -> ProjectPathScanResult:
        gate.started.set()
        await gate.release.wait()
        return scan_result(root, [ProjectPathSuggestion(path="after.py", kind="file")])

    _patch_scan(monkeypatch, gated_scan, roots=gate.roots)
    _install_file_rows(handler, "/repo", [ProjectPathSuggestion(path="before.py", kind="file")])
    screen.services.engine_provider = _no_engine

    handler.mark_file_index_dirty()
    await gate.started.wait()

    handler._suggestion_mode = "files"
    handler.on_file_triggered()

    assert screen.suggestion_list.is_loading is False
    assert suggestion_values(screen.suggestion_list.last_items) == ["before.py"]

    gate.release.set()
    await handler._file_rebuild_worker.wait()

    assert suggestion_values(screen.suggestion_list.last_items) == ["after.py"]


async def test_a_failed_rebuild_keeps_the_old_snapshot_and_the_popup_usable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screen = _screen_at("/repo")
    handler = make_suggestion_handler(screen)

    async def failing_scan(root: str) -> ProjectPathScanResult:
        raise OSError("rg disappeared")

    _patch_scan(monkeypatch, failing_scan)
    _install_file_rows(handler, "/repo", [ProjectPathSuggestion(path="before.py", kind="file")])
    screen.services.engine_provider = _no_engine

    handler.mark_file_index_dirty()
    await handler._file_rebuild_worker.wait()

    handler._suggestion_mode = "files"
    handler.on_file_triggered()

    assert suggestion_values(screen.suggestion_list.last_items) == ["before.py"]
    assert handler._file_index is not None
    assert handler._file_index.dirty is True


async def test_a_turn_end_applies_tracker_deltas_without_a_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Deterministic mutations fold into the snapshot in memory: no scan, no rebuild."""
    root = str(tmp_path)
    created = tmp_path / "src" / "new.py"
    created.parent.mkdir(parents=True, exist_ok=True)
    created.write_text("", encoding="utf-8")
    screen = _screen_at(root)
    handler = make_suggestion_handler(screen)

    async def unexpected_scan(scan_root: str) -> ProjectPathScanResult:
        raise AssertionError("an incremental turn end must not scan")

    _patch_scan(monkeypatch, unexpected_scan)
    _install_file_rows(handler, root, [ProjectPathSuggestion(path="src/old.py", kind="file")])
    screen.services.engine_provider = lambda: _engine(
        mutated=[(MutationSource.WRITE_FILE, MutationProvenance.PROVEN)],
        changed=[str(created)],
    )

    handler.mark_file_index_dirty()

    # The turn end only accepts the change; the fold lands in the worker.
    worker = handler._file_rebuild_worker
    assert worker is not None
    await worker.wait()

    assert handler._file_index is not None
    assert handler._file_index.dirty is False
    # A folded row is appended, so only the visible set is contractual here.
    assert set(_file_rows(handler)) == {"src/", "src/old.py", "src/new.py"}


async def test_a_pending_fold_reports_dirty_until_the_worker_publishes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Between accepting the change and installing it, ``@`` answers stale.

    The turn end returns before the fold is derived, so the window is the only
    place the old snapshot could be reported current; nothing scans in it, and
    the worker publishes the delta without a subprocess.
    """
    root = str(tmp_path)
    created = tmp_path / "src" / "new.py"
    created.parent.mkdir(parents=True, exist_ok=True)
    created.write_text("", encoding="utf-8")
    screen = _screen_at(root)
    handler = make_suggestion_handler(screen)

    async def unexpected_scan(scan_root: str) -> ProjectPathScanResult:
        raise AssertionError("a foldable turn end must not scan")

    _patch_scan(monkeypatch, unexpected_scan)
    _install_file_rows(handler, root, [ProjectPathSuggestion(path="src/old.py", kind="file")])
    screen.services.engine_provider = lambda: _engine(
        mutated=[(MutationSource.WRITE_FILE, MutationProvenance.PROVEN)],
        changed=[str(created)],
    )

    handler.mark_file_index_dirty()
    worker = handler._file_rebuild_worker
    index = handler._file_index
    assert worker is not None
    assert index is not None
    assert index.dirty is True
    assert _file_rows(handler) == ["src/old.py"]

    await worker.wait()

    assert index.dirty is False
    assert set(_file_rows(handler)) == {"src/", "src/old.py", "src/new.py"}


async def test_a_discard_during_a_pending_fold_drops_the_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A fold that straddles a workspace switch must not publish.

    ``discard()`` bumps the epoch, so the derived rows are dropped: the snapshot
    stays empty instead of being resurrected from the root that was left behind,
    and the dropped fold does not fall back to scanning.
    """
    root = str(tmp_path)
    created = tmp_path / "new.py"
    created.write_text("", encoding="utf-8")
    screen = _screen_at(root)
    handler = make_suggestion_handler(screen)

    async def unexpected_scan(scan_root: str) -> ProjectPathScanResult:
        raise AssertionError("a fold that lost its epoch must not fall back to a scan")

    _patch_scan(monkeypatch, unexpected_scan)
    index = _install_file_rows(handler, root, [ProjectPathSuggestion(path="old.py", kind="file")])
    screen.services.engine_provider = lambda: _engine(
        mutated=[(MutationSource.WRITE_FILE, MutationProvenance.PROVEN)],
        changed=[str(created)],
    )

    fold_started = threading.Event()
    release_fold = threading.Event()
    derive_fold = WorkspacePathIndex._derive_fold

    def gated_derive(self, removals, additions):
        fold_started.set()
        release_fold.wait(timeout=10)
        return derive_fold(self, removals, additions)

    monkeypatch.setattr(WorkspacePathIndex, "_derive_fold", gated_derive)

    handler.mark_file_index_dirty()
    worker = handler._file_rebuild_worker
    assert worker is not None
    assert await asyncio.to_thread(fold_started.wait, 5) is True

    handler.discard_file_index()
    release_fold.set()
    await worker.wait()

    assert index.snapshot is None
    assert index.installed is False
    assert index.query("old") == []
    assert index.delta == set()


@pytest.mark.parametrize(
    "engine_factory",
    [
        pytest.param(_no_engine, id="no-tracker"),
        pytest.param(
            lambda: _engine(mutated=[(MutationSource.SHELL, MutationProvenance.PROVEN)], changed=["/repo/x.py"]),
            id="shell-source",
        ),
        pytest.param(
            lambda: _engine(
                mutated=[(MutationSource.WRITE_FILE, MutationProvenance.PROVEN)],
                changed=["/repo/x.py"],
                detection_truncated=True,
            ),
            id="detection-truncated",
        ),
    ],
)
async def test_an_unknowable_turn_end_marks_the_snapshot_dirty_and_rebuilds(
    monkeypatch: pytest.MonkeyPatch,
    engine_factory: Callable[[], object],
) -> None:
    """Shell tools and truncated detection can create files silently, so they rescan."""
    screen = _screen_at("/repo")
    handler = make_suggestion_handler(screen)
    scans: list[str] = []

    _patch_scan(monkeypatch, {"/repo": ["rebuilt.py"]}, roots=scans)
    _install_file_rows(handler, "/repo", [ProjectPathSuggestion(path="before.py", kind="file")])
    screen.services.engine_provider = engine_factory

    handler.mark_file_index_dirty()
    await handler._file_rebuild_worker.wait()

    assert scans == ["/repo"]
    assert _file_rows(handler) == ["rebuilt.py"]


def test_file_query_debounce_is_one_frame() -> None:
    """AC5: the keystroke debounce is 16ms, i.e. one 60Hz frame."""
    assert pytest.approx(0.016) == suggestions_module._FILE_QUERY_DEBOUNCE_SECONDS


async def test_typing_without_an_installed_snapshot_starts_no_query_worker() -> None:
    """The 16ms fast path is only armed by an installed snapshot."""
    screen = _screen_at("/repo")
    handler = make_suggestion_handler(screen)
    handler._suggestion_mode = "files"

    handler.on_text_changed("@repo")

    assert handler._file_query_pending is None
    assert handler._file_query_worker is None


async def test_a_read_only_turn_needs_no_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty mutation set folds to nothing, so the snapshot stays clean."""
    screen = _screen_at("/repo")
    handler = make_suggestion_handler(screen)

    async def unexpected_scan(scan_root: str) -> ProjectPathScanResult:
        raise AssertionError("a read-only turn must not scan")

    _patch_scan(monkeypatch, unexpected_scan)
    _install_file_rows(handler, "/repo", [ProjectPathSuggestion(path="before.py", kind="file")])
    screen.services.engine_provider = _engine

    handler.mark_file_index_dirty()

    assert handler._file_rebuild_worker is None
    assert handler._file_index is not None
    assert handler._file_index.dirty is False
    assert _file_rows(handler) == ["before.py"]


async def test_a_turn_end_that_lands_during_a_rebuild_still_reaches_the_next_at(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A create recorded while a rebuild is in flight must survive that rebuild.

    Installing the scan as a clean baseline would drop the create: the next turn
    folds nothing, reports the snapshot current, and the file never shows up.
    """
    root = str(tmp_path)
    created = tmp_path / "src" / "created.py"
    created.parent.mkdir(parents=True, exist_ok=True)
    created.write_text("", encoding="utf-8")
    screen = _screen_at(root)
    handler = make_suggestion_handler(screen)
    scans: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def gated_scan(scan_root: str) -> ProjectPathScanResult:
        scans.append(scan_root)
        if len(scans) == 1:
            started.set()
            await release.wait()
        return scan_result(scan_root, [ProjectPathSuggestion(path="src/old.py", kind="file")])

    _patch_scan(monkeypatch, gated_scan)
    _install_file_rows(handler, root, [ProjectPathSuggestion(path="src/old.py", kind="file")])
    assert handler._file_index is not None

    # Turn 1: an unknowable change set starts a background rebuild.
    screen.services.engine_provider = _no_engine
    handler.mark_file_index_dirty()
    await started.wait()

    # Turn 2 lands while that rebuild is still scanning; its create is tracked.
    screen.services.engine_provider = lambda: _engine(
        mutated=[(MutationSource.WRITE_FILE, MutationProvenance.PROVEN)],
        changed=[str(created)],
    )
    handler.mark_file_index_dirty()
    assert handler._file_index.delta, "the create must stay pending across the rebuild"

    release.set()
    await handler._file_rebuild_worker.wait()
    scans_after_rebuild = list(scans)

    # Turn 3 changed nothing and left nothing pending: the worker that settled the
    # create is reused, and settling it still costs no scan.
    screen.services.engine_provider = _engine
    handler.mark_file_index_dirty()
    await handler._file_rebuild_worker.wait()

    assert scans == scans_after_rebuild == [root]
    assert handler._file_index.dirty is False
    assert "src/created.py" in _file_rows(handler)


async def test_a_rollback_invalidation_drops_the_path_the_undo_restored_away(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """``/undo`` deletes the file a turn created; a fold-clean snapshot must stop listing it.

    The rolled-back periods are gone from the tracker, so the restored paths
    never reach ``mark_file_index_dirty``: only the rollback invalidating the
    snapshot gets the ``@`` rows back to the restored truth.
    """
    root = str(tmp_path)
    generated = tmp_path / "generated.py"
    generated.write_text("", encoding="utf-8")
    screen = _screen_at(root)
    handler = make_suggestion_handler(screen)
    scans: list[str] = []

    async def restored_scan(scan_root: str) -> ProjectPathScanResult:
        return scan_result(scan_root, [])

    _patch_scan(monkeypatch, restored_scan, roots=scans)
    _install_file_rows(handler, root, [ProjectPathSuggestion(path="generated.py", kind="file")])
    screen.services.engine_provider = _engine

    handler.mark_file_index_dirty()
    assert handler._file_index is not None
    assert handler._file_index.dirty is False
    assert _file_rows(handler) == ["generated.py"]

    generated.unlink()
    handler.invalidate_file_index()
    assert handler._file_rebuild_worker is not None
    await handler._file_rebuild_worker.wait()

    assert scans == [root]
    assert _file_rows(handler) == []

    # A later turn that changed nothing must not resurrect the rolled-back path.
    handler.mark_file_index_dirty()

    assert handler._file_index.dirty is False
    assert _file_rows(handler) == []
    assert scans == [root]


async def test_a_rebuild_that_installs_an_unknowable_change_scans_again(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """P3-3: a scan that straddled an unknowable change cannot be the last one."""
    root = str(tmp_path)
    screen = _screen_at(root)
    handler = make_suggestion_handler(screen)
    scans: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def gated_scan(scan_root: str) -> ProjectPathScanResult:
        scans.append(scan_root)
        if len(scans) == 1:
            started.set()
            await release.wait()
            return scan_result(scan_root, [ProjectPathSuggestion(path="stale.py", kind="file")])
        return scan_result(scan_root, [ProjectPathSuggestion(path="fresh.py", kind="file")])

    _patch_scan(monkeypatch, gated_scan)
    _install_file_rows(handler, root, [ProjectPathSuggestion(path="stale.py", kind="file")])
    screen.services.engine_provider = _no_engine

    # Turn 1 starts the rebuild; turn 2 changes something unknowable mid-scan.
    handler.mark_file_index_dirty()
    await started.wait()
    handler.mark_file_index_dirty()
    release.set()
    await handler._file_rebuild_worker.wait()

    assert scans == [root, root]
    assert handler._file_index is not None
    assert handler._file_index.dirty is False
    assert _file_rows(handler) == ["fresh.py"]


async def test_a_rebuild_chains_a_bounded_number_of_scans(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """A workspace that never stops changing must not re-scan forever."""
    root = str(tmp_path)
    screen = _screen_at(root)
    handler = make_suggestion_handler(screen)
    scans: list[str] = []

    async def always_changing_scan(scan_root: str) -> ProjectPathScanResult:
        scans.append(scan_root)
        index = handler._file_index
        if index is not None:
            index.mark_dirty()
        return scan_result(scan_root, [ProjectPathSuggestion(path="busy.py", kind="file")])

    _patch_scan(monkeypatch, always_changing_scan)
    _install_file_rows(handler, root, [ProjectPathSuggestion(path="before.py", kind="file")])
    screen.services.engine_provider = _no_engine

    handler.mark_file_index_dirty()
    await handler._file_rebuild_worker.wait()

    assert len(scans) == suggestions_module._FILE_INDEX_REBUILD_SCANS
    assert handler._file_index is not None
    assert handler._file_index.dirty is True
