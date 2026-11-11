# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Materialize a read-only workdir seed into a case-private directory.

The source path is never opened in-place for agent edits. Strategies:

- ``copy`` — recursive copy (default; required for non-git trees)
- ``git_clone`` — ``git clone --no-local`` into the destination
- ``git_worktree`` — ``git worktree add --detach`` (fast; shares the source
  ``.git``, so isolation is weaker — see module docs in the user guide)

Isolation rules for ``copy`` and ``git_clone``:

- No source git metadata is copied. Every repository found in the source tree
  (root, nested, submodule or linked worktree; any ``.git`` dir, file or
  symlink) is rebuilt in the copy as a fresh private repository with
  ``git clone --no-local``, so no ``core.worktree``, ``commondir``,
  ``worktrees/``, ``modules/``, ``include.path`` or gitfile can point back at
  the caller's repositories.
- Every rebuilt repository has its remotes removed; one left behind fails the
  materialization.
- Symlinks resolving outside the source or the copy are refused.
- Git runs without inherited repository-selecting ``GIT_*`` variables and
  with ``GIT_CEILING_DIRECTORIES`` set, so neither the caller's environment
  nor an enclosing repository is picked up.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs
from chrys.service.rsi.errors import RsiBadRequest

logger = logging.getLogger(__name__)

WorkdirStrategy = Literal["copy", "git_clone", "git_worktree"]
WORKDIR_STRATEGIES: frozenset[str] = frozenset({"copy", "git_clone", "git_worktree"})
_SEED_MARKER = ".chrys_rsi_workdir_seed.json"
RESPONSE_FILE_NAME = ".chrys_rsi_response.txt"
SESSIONS_DIR_NAME = ".chrys_rsi_sessions"
_RSI_PRIVATE_FILES = (_SEED_MARKER, RESPONSE_FILE_NAME, SESSIONS_DIR_NAME)


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


def isolated_git_env(ceiling: Path | None) -> dict[str, str]:
    """Process env without inherited repository-selecting git variables, bounded at *ceiling*.

    Drops what ``git rev-parse --local-env-vars`` lists (``GIT_DIR``,
    ``GIT_WORK_TREE``, ``GIT_INDEX_FILE``, ``GIT_COMMON_DIR``, config injection
    via ``GIT_CONFIG_PARAMETERS`` / ``GIT_CONFIG_COUNT`` …) plus discovery
    knobs; user-level settings such as ``GIT_CONFIG_GLOBAL`` or author identity
    stay. With *ceiling* set, git run below it never discovers a repository
    above it.
    """
    env = {key: value for key, value in os.environ.items() if not _is_repo_selecting_git_var(key)}
    if ceiling is not None:
        env["GIT_CEILING_DIRECTORIES"] = str(ceiling)
    return env


_REPO_SELECTING_GIT_VARS = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_DIR",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_GRAFT_FILE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_NAMESPACE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_PREFIX",
        "GIT_REPLACE_REF_BASE",
        "GIT_SHALLOW_FILE",
        "GIT_WORK_TREE",
    }
)


def _is_repo_selecting_git_var(name: str) -> bool:
    upper = name.upper()
    return upper in _REPO_SELECTING_GIT_VARS or upper.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))


def enclosing_git_work_tree(path: Path) -> Path | None:
    """Top level of the git work tree holding *path* (or its nearest existing parent), if any.

    Case workdirs without their own repository would let git fall through to
    that enclosing repository.
    """
    probe = path.expanduser().absolute()
    while not probe.is_dir():
        if probe.parent == probe:
            return None
        probe = probe.parent
    try:
        result = _run_git(["rev-parse", "--show-toplevel"], cwd=probe, check=False)
    except RsiBadRequest:
        return None
    top = result.stdout.strip()
    if result.returncode != 0 or not top:
        return None
    return Path(top)


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
    if dest.exists() or dest.is_symlink():
        raise RsiBadRequest(f"workdir destination already exists: {dest}")
    source = resolve_seed_source(seed, dataset_dir=dataset_dir)
    if not source.is_dir():
        raise RsiBadRequest(f"workdir.from must be a directory: {seed.source}")
    dest_resolved = dest.resolve()
    if dest_resolved == source or dest_resolved.is_relative_to(source):
        raise RsiBadRequest(
            f"workdir destination {dest} is inside workdir.from {source}; choose an output directory outside it"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)

    strategy = seed.strategy
    if strategy == "copy":
        _copy_tree(source, dest)
    elif strategy == "git_clone":
        _git_clone(source, dest, ref=seed.ref)
    elif strategy == "git_worktree":
        _git_worktree(source, dest, ref=seed.ref)
    else:
        raise RsiBadRequest(f"unknown workdir.strategy: {strategy}")
    _write_marker(dest, strategy=strategy, source=source)
    _exclude_rsi_files(dest)
    return dest


def snapshot_seed_for_task(
    seed: WorkdirSeed,
    *,
    dest: Path,
    dataset_dir: Path,
) -> WorkdirSeed:
    """Materialize a task-local seed and return a rewritten ``copy`` seed pointing at it.

    ``git_worktree`` is upgraded to ``git_clone`` so the task ``input/`` tree does
    not keep a live link into the caller's repository. Later case copies rebuild
    the snapshot's private repositories again.
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


def write_text_nofollow(path: Path, text: str, *, what: str) -> None:
    """Write *text* to *path* without following a symlink planted there."""
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
        raise RsiBadRequest(f"{what} refused (symlink or unwritable): {path}") from exc
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


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
    write_text_nofollow(
        dest / _SEED_MARKER,
        json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
        what="workdir marker",
    )


def _exclude_rsi_files(dest: Path) -> None:
    """Keep RSI's own files out of ``git status`` in a private root repository."""
    git_dir = dest / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        return
    info = git_dir / "info"
    exclude = info / "exclude"
    if info.is_symlink() or exclude.is_symlink():
        raise RsiBadRequest(f"workdir git metadata has a symlinked info/exclude: {exclude}")
    try:
        info.mkdir(exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    except OSError as exc:
        raise RsiBadRequest(f"workdir git exclude unreadable: {exc}") from exc
    lines = existing.splitlines()
    missing = [f"/{name}" for name in _RSI_PRIVATE_FILES if f"/{name}" not in lines]
    if not missing:
        return
    prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
    write_text_nofollow(exclude, prefix + "\n".join(missing) + "\n", what="workdir git exclude")


def _copy_tree(source: Path, dest: Path) -> None:
    _refuse_escaping_symlinks(source)
    repo_roots = _find_repo_roots(source)
    try:
        shutil.copytree(
            source,
            dest,
            symlinks=True,
            ignore_dangling_symlinks=True,
            ignore=_ignore_git_metadata,
        )
    except OSError as exc:
        raise RsiBadRequest(f"workdir copy failed: {exc}") from exc
    _refuse_escaping_symlinks(dest, label="copied workdir")
    for relative in repo_roots:
        _rebuild_private_repo(source / relative, dest / relative)


def _ignore_git_metadata(_directory: str, names: list[str]) -> list[str]:
    return [".git"] if ".git" in names else []


def _find_repo_roots(source: Path) -> list[Path]:
    """Relative paths of every directory holding a ``.git`` entry, outermost first."""
    roots: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        if ".git" in dirnames or ".git" in filenames:
            roots.append(Path(dirpath).relative_to(source))
        if ".git" in dirnames:
            dirnames.remove(".git")
    return roots


def _rebuild_private_repo(source_repo: Path, dest_repo: Path) -> None:
    """Give *dest_repo* (copied files, no metadata) a fresh clone of *source_repo*'s history.

    The index is reset to the source's HEAD, so uncommitted source edits show up
    as modifications, as they do in the source.
    """
    if not _is_work_tree_root(source_repo):
        raise RsiBadRequest(
            f"workdir.from contains a .git entry git cannot open: {source_repo / '.git'}; "
            "fix or remove it, or use a git_clone seed"
        )
    head = _run_git(["rev-parse", "--verify", "--quiet", "HEAD"], cwd=source_repo, check=False)
    if head.returncode != 0:
        _run_git(["init", "--quiet"], cwd=dest_repo, check=True, ceiling=dest_repo.parent)
        return
    scratch = Path(tempfile.mkdtemp(prefix=".rsi-clone-", dir=dest_repo.parent))
    try:
        clone = scratch / "repo"
        _run_git(
            ["clone", "--quiet", "--no-local", "--no-checkout", str(source_repo), str(clone)],
            cwd=None,
            check=True,
        )
        os.replace(clone / ".git", dest_repo / ".git")
    except OSError as exc:
        raise RsiBadRequest(f"workdir git rebuild failed for {source_repo}: {exc}") from exc
    finally:
        _remove_tree(scratch)
    _run_git(["reset", "--quiet", "--mixed", "HEAD"], cwd=dest_repo, check=True, ceiling=dest_repo.parent)
    _strip_git_remotes(dest_repo)


def _is_work_tree_root(path: Path) -> bool:
    result = _run_git(["rev-parse", "--show-toplevel"], cwd=path, check=False, ceiling=path.parent)
    if result.returncode != 0:
        return False
    try:
        return Path(result.stdout.strip()).resolve() == path.resolve()
    except OSError:
        return False


def _strip_git_remotes(repo: Path) -> None:
    """Remove every remote; fail closed if any survives."""
    git_dir = repo / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir() or (git_dir / "config").is_symlink():
        raise RsiBadRequest(f"workdir git metadata is not a private directory: {git_dir}")
    listed = _run_git(["remote"], cwd=repo, check=True, ceiling=repo.parent)
    for name in listed.stdout.split():
        _run_git(["remote", "remove", name], cwd=repo, check=False, ceiling=repo.parent)
    remaining = _run_git(["remote"], cwd=repo, check=True, ceiling=repo.parent).stdout.split()
    if remaining:
        raise RsiBadRequest(f"workdir git remotes could not be removed in {repo}: {', '.join(remaining)}")


def _remove_tree(path: Path) -> None:
    def _on_error(func: Any, failed: str, _exc: BaseException) -> None:
        failed_path = Path(failed)
        if failed_path.is_symlink():
            return
        os.chmod(failed_path, stat.S_IWRITE)
        func(failed)

    shutil.rmtree(path, onexc=_on_error)


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
    _run_git(["clone", "--quiet", "--no-local", str(source.resolve()), str(dest_abs)], cwd=None, check=True)
    if ref:
        _run_git(["checkout", "--quiet", "--force", ref], cwd=dest_abs, check=True, ceiling=dest_abs.parent)
    _strip_git_remotes(dest_abs)
    _refuse_escaping_symlinks(dest_abs, label="cloned workdir")


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
    ceiling: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = ["git", *args]
    try:
        result = subprocess.run(  # noqa: S603
            command,
            cwd=str(cwd) if cwd is not None else None,
            env=isolated_git_env(ceiling),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **windows_hidden_subprocess_kwargs(),
        )
    except FileNotFoundError as exc:
        raise RsiBadRequest("git is required for workdir seeds that hold git repositories") from exc
    except OSError as exc:
        raise RsiBadRequest(f"git invocation failed: {exc}") from exc
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
        raise RsiBadRequest(f"git {' '.join(args)} failed: {detail}")
    return result
