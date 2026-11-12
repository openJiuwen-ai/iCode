# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Git branch discovery and HEAD-file monitoring for TUI workspace chrome."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from rich.cells import cell_len
from rich.text import Text

from chrys.app.tui.util.paths import middle_cut_path
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar

logger = logging.getLogger(__name__)

GIT_BRANCH_POLL_INTERVAL_SECONDS = 7.0

_GIT_DIR_PREFIX = "gitdir:"
_REFS_HEADS_PREFIX = "refs/heads/"
_HEAD_NAME = "HEAD"


@dataclass(frozen=True, slots=True)
class GitBranchSnapshot:
    """Current git branch metadata for a workspace cwd."""

    cwd: str
    branch: str = ""
    head_path: Path | None = None
    git_marker_path: Path | None = None
    repo_root: Path | None = None


@dataclass(frozen=True, slots=True)
class _GitPaths:
    repo_root: Path
    git_dir: Path
    git_marker_path: Path


@dataclass(frozen=True, slots=True)
class _WatchTarget:
    watch_dirs: tuple[Path, ...]
    head_path: Path | None
    git_marker_path: Path | None
    pending_head_parent: Path | None = None
    non_git_cwd: Path | None = None


def format_workspace_subtitle(cwd: str, branch: str, *, width: int | None) -> Text:
    """Fit the workspace path to the border while retaining its final directory and branch."""
    if not cwd:
        return Text()
    cwd = sanitize_legacy_scalar(cwd)
    windows_path = PureWindowsPath(cwd)
    windows_style = bool(windows_path.drive) or ("\\" in cwd and "/" not in cwd)
    path = windows_path if windows_style else PurePosixPath(cwd)
    if not path.anchor or len(path.parts) > 1:
        cwd = cwd.rstrip("/\\" if windows_style else "/")
    branch = sanitize_legacy_scalar(branch)
    suffix = f" ({branch})" if branch else ""
    if width is None:
        return Text(cwd + suffix)
    separator = "\\" if "\\" in cwd and "/" not in cwd else "/"
    name = cwd.rsplit(separator, 1)[-1]
    minimum = min(cell_len(cwd), cell_len(name) + 2) if name else cell_len(cwd)
    path_width = max(min(minimum, max(0, width)), width - cell_len(suffix))
    return Text(middle_cut_path(cwd, path_width) + suffix)


def read_git_branch_snapshot(cwd: str) -> GitBranchSnapshot:
    """Read git branch metadata for *cwd* without invoking git."""
    start = _path_from_cwd(cwd)
    if start is None:
        return GitBranchSnapshot(cwd=cwd)
    paths = _find_git_paths(start)
    if paths is None:
        return GitBranchSnapshot(cwd=cwd)
    head_path = paths.git_dir / _HEAD_NAME
    branch = _read_head_branch(head_path)
    return GitBranchSnapshot(
        cwd=cwd,
        branch=branch,
        head_path=head_path,
        git_marker_path=paths.git_marker_path,
        repo_root=paths.repo_root,
    )


class GitBranchMonitor:
    """Watch the git HEAD file for a workspace cwd.

    The monitor uses watchdog file notifications. Callers should still refresh on
    semantic UI events such as tool completion because those updates are cheap
    and cover environments where native file notifications are unavailable.
    """

    def __init__(self, on_change: Callable[[], None]) -> None:
        self._on_change = on_change
        self._cwd = ""
        self._active = False
        self._observer: Any | None = None
        self._target: _WatchTarget | None = None

    @property
    def watching(self) -> bool:
        """Return whether a native filesystem watcher is currently active."""
        return self._observer is not None

    @property
    def active(self) -> bool:
        """Return whether monitoring has been started."""
        return self._active

    def start(self, cwd: str) -> GitBranchSnapshot:
        """Activate monitoring for *cwd* and return the current snapshot."""
        self._active = True
        return self.configure(cwd)

    def configure(self, cwd: str) -> GitBranchSnapshot:
        """Switch monitoring to *cwd* and return the current snapshot."""
        self._cwd = cwd
        snapshot = read_git_branch_snapshot(cwd)
        self._sync_watch_target(snapshot)
        return snapshot

    def refresh(self) -> GitBranchSnapshot:
        """Re-read current branch metadata and adjust watcher paths if needed."""
        snapshot = read_git_branch_snapshot(self._cwd)
        self._sync_watch_target(snapshot)
        return snapshot

    def stop(self) -> None:
        """Stop native file monitoring."""
        self._active = False
        self._target = None
        self._stop_observer()

    def _sync_watch_target(self, snapshot: GitBranchSnapshot) -> None:
        target = _watch_target_for_snapshot(snapshot)
        if target == self._target:
            if self._active and self._observer is None and target is not None:
                self._start_observer(target)
            return
        self._target = target
        self._stop_observer()
        if not self._active or target is None:
            return
        self._start_observer(target)

    def _start_observer(self, target: _WatchTarget) -> None:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        on_change = self._on_change

        class _HeadEventHandler(FileSystemEventHandler):
            def on_any_event(self, event: object) -> None:
                if _event_matches_watch_target(event, target):
                    on_change()

        observer = Observer()
        try:
            for watch_dir in target.watch_dirs:
                observer.schedule(_HeadEventHandler(), str(watch_dir), recursive=False)
            observer.start()
        except OSError, RuntimeError:
            logger.debug("failed to start git branch file monitor", exc_info=True)
            with contextlib.suppress(RuntimeError):
                observer.stop()
            return
        self._observer = observer

    def _stop_observer(self) -> None:
        observer = self._observer
        if observer is None:
            return
        self._observer = None
        with contextlib.suppress(RuntimeError):
            observer.stop()
        with contextlib.suppress(RuntimeError):
            observer.join(timeout=1.0)


def _path_from_cwd(cwd: str) -> Path | None:
    raw = cwd.strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve(strict=False)
    except OSError, RuntimeError:
        logger.debug("failed to resolve workspace cwd for git branch lookup: %s", cwd, exc_info=True)
        return None


def _find_git_paths(start: Path) -> _GitPaths | None:
    for candidate in (start, *start.parents):
        marker = candidate / ".git"
        if marker.is_dir():
            return _GitPaths(repo_root=candidate, git_dir=marker, git_marker_path=marker)
        if marker.is_file():
            git_dir = _read_gitdir_file(marker, candidate)
            if git_dir is not None:
                return _GitPaths(repo_root=candidate, git_dir=git_dir, git_marker_path=marker)
        if _looks_like_bare_git_dir(candidate):
            return _GitPaths(repo_root=candidate, git_dir=candidate, git_marker_path=candidate / _HEAD_NAME)
    return None


def _read_gitdir_file(marker: Path, repo_root: Path) -> Path | None:
    text = _read_first_line(marker)
    if not text.lower().startswith(_GIT_DIR_PREFIX):
        return None
    raw_git_dir = text[len(_GIT_DIR_PREFIX) :].strip()
    if not raw_git_dir:
        return None
    git_dir = Path(raw_git_dir)
    if not git_dir.is_absolute():
        git_dir = repo_root / git_dir
    try:
        return git_dir.expanduser().resolve(strict=False)
    except OSError, RuntimeError:
        logger.debug("failed to resolve gitdir from %s", marker, exc_info=True)
        return None


def _looks_like_bare_git_dir(candidate: Path) -> bool:
    return (candidate / _HEAD_NAME).is_file() and (candidate / "objects").is_dir() and (candidate / "refs").is_dir()


def _read_head_branch(head_path: Path) -> str:
    text = _read_first_line(head_path)
    if not text:
        return ""
    if text.startswith("ref:"):
        ref = text[4:].strip()
        if ref.startswith(_REFS_HEADS_PREFIX):
            return ref[len(_REFS_HEADS_PREFIX) :]
        return ref
    if len(text) >= 7 and all(char in "0123456789abcdefABCDEF" for char in text):
        return f"detached {text[:7]}"
    return ""


def _read_first_line(path: Path) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace").splitlines()[0].strip() if raw else ""


def _watch_target_for_snapshot(snapshot: GitBranchSnapshot) -> _WatchTarget | None:
    watch_dirs: list[Path] = []
    pending_head_parent = None
    if snapshot.head_path is not None:
        head_parent = snapshot.head_path.parent
        if head_parent.is_dir():
            _append_existing_dir(watch_dirs, head_parent)
        else:
            pending_head_parent = head_parent
            _append_existing_dir(watch_dirs, _nearest_existing_parent(head_parent))
    if snapshot.git_marker_path is not None:
        _append_existing_dir(watch_dirs, snapshot.git_marker_path.parent)
    non_git_cwd = None
    if snapshot.head_path is None:
        non_git_cwd = _path_from_cwd(snapshot.cwd)
        if non_git_cwd is not None:
            _append_existing_dir(watch_dirs, non_git_cwd)
    if not watch_dirs:
        return None
    return _WatchTarget(
        watch_dirs=tuple(watch_dirs),
        head_path=snapshot.head_path,
        git_marker_path=snapshot.git_marker_path,
        pending_head_parent=pending_head_parent,
        non_git_cwd=non_git_cwd,
    )


def _nearest_existing_parent(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if candidate.is_dir() and candidate.parent != candidate:
            return candidate
    return path


def _append_existing_dir(paths: list[Path], path: Path) -> None:
    if not path.is_dir():
        return
    if path not in paths:
        paths.append(path)


def _event_matches_watch_target(event: object, target: _WatchTarget) -> bool:
    for raw_path in _event_paths(event):
        path = _event_path(raw_path)
        if path is None:
            continue
        if target.head_path is not None and _path_matches_head(path, target.head_path):
            return True
        if target.git_marker_path is not None and _path_matches_git_marker(path, target.git_marker_path):
            return True
        if target.pending_head_parent is not None and _path_touches_pending_head_parent(
            path,
            target.pending_head_parent,
        ):
            return True
        if target.non_git_cwd is not None and path.parent == target.non_git_cwd and path.name == ".git":
            return True
    return False


def _event_paths(event: object) -> tuple[object, object]:
    return (getattr(event, "src_path", ""), getattr(event, "dest_path", ""))


def _event_path(raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return Path(raw).expanduser().resolve(strict=False)
    except OSError, RuntimeError:
        return None


def _path_matches_head(path: Path, head_path: Path) -> bool:
    if path == head_path:
        return True
    return path.parent == head_path.parent and path.name.startswith(_HEAD_NAME)


def _path_matches_git_marker(path: Path, marker: Path) -> bool:
    return path == marker


def _path_touches_pending_head_parent(path: Path, pending_head_parent: Path) -> bool:
    return path == pending_head_parent or path in pending_head_parent.parents
