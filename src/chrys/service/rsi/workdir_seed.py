# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Materialize a read-only workdir seed into a case-private directory.

The source path is never opened in-place for agent edits. Strategies:

- ``copy`` — recursive copy (default; required for non-git trees)
- ``git_clone`` — ``git clone --local --no-hardlinks`` into the destination
- ``git_worktree`` — ``git worktree add --detach`` (fast; shares the source
  ``.git``, so isolation is weaker — see module docs in the user guide)

Symlinks in a ``copy`` source that resolve outside the source tree are refused.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
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
    not keep a live link into the caller's repository.
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
    if not marker_path.is_file():
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
    (dest / _SEED_MARKER).write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _copy_tree(source: Path, dest: Path) -> None:
    _refuse_escaping_symlinks(source)
    try:
        shutil.copytree(source, dest, symlinks=True, ignore_dangling_symlinks=True)
    except OSError as exc:
        raise RsiBadRequest(f"workdir copy failed: {exc}") from exc
    _refuse_escaping_symlinks(dest, label="copied workdir")


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
    args = ["clone", "--local", "--no-hardlinks", str(source), str(dest)]
    _run_git(args, cwd=None, check=True)
    if ref:
        _run_git(["checkout", "--force", ref], cwd=dest, check=True)


def _git_worktree(source: Path, dest: Path, *, ref: str | None) -> None:
    _require_git_repo(source)
    target = ref or "HEAD"
    _run_git(["worktree", "add", "--detach", str(dest), target], cwd=source, check=True)


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
