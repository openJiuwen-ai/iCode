# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Materialize a read-only workdir seed into a case-private directory.

The source path is never opened in-place for agent edits. Strategies:

- ``copy`` — recursive copy (default; required for non-git trees)
- ``git_clone`` — ``git clone --local --no-hardlinks`` into the destination
- ``git_worktree`` — ``git worktree add --detach`` (fast; shares the source
  ``.git``, so isolation is weaker — see module docs in the user guide)

Symlinks in a ``copy`` source that resolve outside the source tree are refused.
After copy/clone, real ``.git`` directories are kept; gitfiles that point outside
the private tree are disconnected; remotes are stripped so ``git push`` cannot
reach the caller's origin.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs
from chrys.service.rsi.errors import RsiBadRequest

logger = logging.getLogger(__name__)

WorkdirStrategy = Literal["copy", "git_clone", "git_worktree"]
WORKDIR_STRATEGIES: frozenset[str] = frozenset({"copy", "git_clone", "git_worktree"})
_SEED_MARKER = ".chrys_rsi_workdir_seed.json"


@dataclass(slots=True, frozen=True)
class WorkdirSeed:
    """Case-level instruction to materialize a private workdir from a source tree."""

    source: str
    strategy: WorkdirStrategy = "copy"
    ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"from": self.source, "strategy": self.strategy}
        if self.ref:
            payload["ref"] = self.ref
        return payload


def resolve_seed_source(seed: WorkdirSeed, *, dataset_dir: Path) -> Path:
    """Resolve ``seed.source`` against *dataset_dir* when relative."""
    raw = Path(seed.source).expanduser()
    path = raw if raw.is_absolute() else (dataset_dir / raw)
    try:
        return path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RsiBadRequest(f"workdir.from not found: {seed.source}") from exc
    except OSError as exc:
        raise RsiBadRequest(f"workdir.from is not readable: {seed.source}") from exc


def materialize_workdir(
    seed: WorkdirSeed,
    *,
    dest: Path,
    dataset_dir: Path,
) -> Path:
    """Materialize *seed* into *dest* (must not already exist). Returns *dest*."""
    if dest.exists():
        raise RsiBadRequest(f"workdir destination already exists: {dest}")
    source = resolve_seed_source(seed, dataset_dir=dataset_dir)
    if not source.is_dir():
        raise RsiBadRequest(f"workdir.from must be a directory: {seed.source}")
    dest.parent.mkdir(parents=True, exist_ok=True)

    strategy = seed.strategy
    if strategy == "copy":
        _copy_tree(source, dest)
        _write_marker(dest, strategy=strategy, source=source)
        return dest
    if strategy == "git_clone":
        _git_clone(source, dest, ref=seed.ref)
        _write_marker(dest, strategy=strategy, source=source)
        return dest
    if strategy == "git_worktree":
        _git_worktree(source, dest, ref=seed.ref)
        _write_marker(dest, strategy=strategy, source=source)
        return dest
    raise RsiBadRequest(f"unknown workdir.strategy: {strategy}")


def snapshot_seed_for_task(
    seed: WorkdirSeed,
    *,
    dest: Path,
    dataset_dir: Path,
) -> WorkdirSeed:
    """Materialize a task-local seed and return a rewritten ``copy`` seed pointing at it.

    ``git_worktree`` is upgraded to ``git_clone`` so the task ``input/`` tree does
    not keep a live link into the caller's repository. Real ``.git`` directories from
    clone are kept so later task-path copies still have a private git history.
    """
    strategy: WorkdirStrategy = seed.strategy
    if strategy == "git_worktree":
        strategy = "git_clone"
    materialize_workdir(
        WorkdirSeed(source=seed.source, strategy=strategy, ref=seed.ref),
        dest=dest,
        dataset_dir=dataset_dir,
    )
    # Destination is absolute under the task; store as absolute so later loads
    # (dataset_dir = input/) still resolve even if relative layout shifts.
    return WorkdirSeed(source=str(dest.resolve()), strategy="copy", ref=None)


def release_workdir_seeds_under(root: Path) -> None:
    """Detach any ``git_worktree`` seeds under *root* before deleting the tree."""
    if not root.is_dir():
        return
    for dirpath, _dirnames, filenames in os.walk(root, topdown=False, followlinks=False):
        if _SEED_MARKER not in filenames:
            continue
        workdir = Path(dirpath)
        try:
            _release_one_workdir(workdir)
        except RsiBadRequest as exc:
            logger.warning("RSI workdir seed release failed for %s: %s", workdir, exc.message)


def _release_one_workdir(workdir: Path) -> None:
    marker_path = workdir / _SEED_MARKER
    if marker_path.is_symlink() or not marker_path.is_file():
        return
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except OSError, UnicodeError, json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    if payload.get("strategy") != "git_worktree":
        return
    source = str(payload.get("source") or "").strip()
    if not source:
        return
    source_path = Path(source)
    resolved = str(workdir.resolve())
    result = _run_git(
        ["worktree", "remove", "--force", resolved],
        cwd=source_path,
        check=False,
    )
    if result.returncode == 0:
        return
    _run_git(["worktree", "prune"], cwd=source_path, check=False)


def _write_marker(
    dest: Path,
    *,
    strategy: str,
    source: Path,
) -> None:
    payload: dict[str, Any] = {
        "strategy": strategy,
        "source": str(source),
    }
    _write_text_nofollow(
        dest / _SEED_MARKER,
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
    )


def _write_text_nofollow(path: Path, text: str) -> None:
    """Write *text* without following a pre-existing symlink at *path*."""
    data = text.encode("utf-8")
    # unlink() on a symlink removes the link, never the target.
    if path.is_symlink():
        path.unlink(missing_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    try:
        fd = os.open(os.fspath(path), flags, 0o644)
    except OSError as exc:
        # ELOOP / EMLINK when a symlink raced back in; refuse rather than follow.
        raise RsiBadRequest(f"workdir marker refused (symlink or unwritable): {path}") from exc
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _copy_tree(source: Path, dest: Path) -> None:
    _refuse_escaping_symlinks(source)
    try:
        shutil.copytree(source, dest, symlinks=True, ignore_dangling_symlinks=True)
    except OSError as exc:
        raise RsiBadRequest(f"workdir copy failed: {exc}") from exc
    _sanitize_copied_git_metadata(dest)
    _refuse_escaping_symlinks(dest, label="copied workdir")


def _sanitize_copied_git_metadata(dest: Path) -> None:
    """Keep real ``.git`` dirs; disconnect gitfiles / symlinks that escape *dest*.

    Linked worktrees store a gitfile (``gitdir: …``) or symlink that points at
    the caller's repository. Leaving those in a private copy lets BYPASS agents
    ``commit`` / ``checkout`` against the user's real branch. Nested gitfiles
    (for example under ``.worktrees/``) are walked too. Directory ``.git``
    object stores are preserved so task-path ``git_clone`` seeds still have git
    after being rewritten to ``copy``.
    """
    root_resolved = dest.resolve()
    for dirpath, dirnames, _filenames in os.walk(dest, topdown=True, followlinks=False):
        git_path = Path(dirpath) / ".git"
        if ".git" in dirnames:
            # Never walk into object stores; handle the entry itself below.
            dirnames.remove(".git")
        if not git_path.exists() and not git_path.is_symlink():
            continue
        if git_path.is_dir() and not git_path.is_symlink():
            # Real repository metadata — keep it.
            continue
        _disconnect_external_git_pointer(git_path, root_resolved)
    _strip_git_remotes(dest)


def _disconnect_external_git_pointer(git_path: Path, root_resolved: Path) -> None:
    """Unlink a gitfile/symlink whose target lies outside *root_resolved*."""
    try:
        if git_path.is_symlink():
            try:
                target = git_path.resolve()
            except OSError:
                _unlink_path(git_path)
                return
            try:
                target.relative_to(root_resolved)
            except ValueError:
                _unlink_path(git_path)
            return

        text = git_path.read_text(encoding="utf-8", errors="replace").strip()
        if not text.lower().startswith("gitdir:"):
            # Unknown non-directory .git — refuse to leave an opaque pointer.
            _unlink_path(git_path)
            return
        raw = text.split(":", 1)[1].strip()
        target = Path(raw)
        target = (git_path.parent / target).resolve() if not target.is_absolute() else target.resolve()
        try:
            target.relative_to(root_resolved)
        except ValueError:
            _unlink_path(git_path)
    except OSError as exc:
        raise RsiBadRequest(f"workdir copy could not detach external git pointer: {exc}") from exc


def _unlink_path(path: Path) -> None:
    """Remove a file or symlink; clear the Windows read-only bit when needed.

    Never ``chmod`` through a symlink (that would change the link target).
    """
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    try:
        path.unlink(missing_ok=True)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        path.unlink(missing_ok=True)


def _strip_git_remotes(repo: Path) -> None:
    """Remove every remote so ``git push`` cannot rewrite the caller's branches."""
    git_dir = repo / ".git"
    if not git_dir.is_dir() or git_dir.is_symlink():
        return
    listed = _run_git(["remote"], cwd=repo, check=False)
    if listed.returncode != 0:
        return
    for name in listed.stdout.split():
        _run_git(["remote", "remove", name], cwd=repo, check=False)


def _refuse_escaping_symlinks(root: Path, *, label: str = "workdir.from") -> None:
    root_resolved = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in (*dirnames, *filenames):
            path = Path(dirpath) / name
            if not path.is_symlink():
                continue
            try:
                target = path.resolve()
            except OSError as exc:
                raise RsiBadRequest(f"{label} has an unreadable symlink: {path}") from exc
            try:
                target.relative_to(root_resolved)
            except ValueError as exc:
                raise RsiBadRequest(f"{label} refuses symlink escaping the tree: {path} -> {target}") from exc


def _git_clone(source: Path, dest: Path, *, ref: str | None) -> None:
    _require_git_repo(source)
    dest_abs = dest.resolve()
    args = ["clone", "--local", "--no-hardlinks", str(source.resolve()), str(dest_abs)]
    _run_git(args, cwd=None, check=True)
    if ref:
        _run_git(["checkout", "--force", ref], cwd=dest_abs, check=True)
    _strip_git_remotes(dest_abs)
    _sanitize_copied_git_metadata(dest_abs)


def _git_worktree(source: Path, dest: Path, *, ref: str | None) -> None:
    _require_git_repo(source)
    # Always absolute: relative dest would resolve inside the source repo cwd.
    dest_abs = dest if dest.is_absolute() else dest.resolve()
    dest_abs.parent.mkdir(parents=True, exist_ok=True)
    target = ref or "HEAD"
    _run_git(["worktree", "add", "--detach", str(dest_abs), target], cwd=source, check=True)


def _require_git_repo(path: Path) -> None:
    result = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=path, check=False)
    if result.returncode != 0 or result.stdout.strip() != "true":
        # Also allow bare repos as clone sources.
        bare = _run_git(["rev-parse", "--is-bare-repository"], cwd=path, check=False)
        if bare.returncode == 0 and bare.stdout.strip() == "true":
            return
        raise RsiBadRequest(f"workdir strategy requires a git repository at {path}; use strategy=copy for plain trees")


def _run_git(
    args: list[str],
    *,
    cwd: Path | None,
    check: bool,
) -> subprocess.CompletedProcess[str]:
    command = ["git", *args]
    try:
        result = subprocess.run(  # noqa: S603
            command,
            cwd=str(cwd) if cwd is not None else None,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **windows_hidden_subprocess_kwargs(),
        )
    except FileNotFoundError as exc:
        raise RsiBadRequest("git is required for workdir strategy git_clone/git_worktree") from exc
    except OSError as exc:
        raise RsiBadRequest(f"git invocation failed: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        raise RsiBadRequest(f"git {' '.join(args)} failed: {detail}")
    return result
