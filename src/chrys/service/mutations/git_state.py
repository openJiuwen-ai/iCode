# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded Git state helpers for workspace and shell mutation tracking."""

from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from io import BufferedReader
from pathlib import PurePath

logger = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS = 10.0
GIT_PATH_LIMIT = 500


@dataclass(frozen=True, slots=True)
class GitHeadState:
    """One repository HEAD and branch snapshot."""

    head: str | None
    branch: str | None


@dataclass(frozen=True, slots=True)
class GitStatusResult:
    """Path statuses returned by porcelain v1."""

    statuses: dict[str, tuple[str, ...]]
    truncated: bool = False
    failed: bool = False


@dataclass(frozen=True, slots=True)
class GitDeltaEntry:
    """One name-status delta record."""

    status: str
    path: str
    old_path: str | None = None


@dataclass(frozen=True, slots=True)
class GitDeltaResult:
    """A bounded scoped delta between two HEAD endpoints."""

    entries: tuple[GitDeltaEntry, ...]
    truncated: bool = False
    failed: bool = False


@dataclass(frozen=True, slots=True)
class _StreamResult:
    returncode: int
    stopped: bool
    malformed: bool


def canonical_path(path: str) -> str:
    """Return the comparison identity for a filesystem path."""
    return os.path.normcase(os.path.realpath(path))


def lexical_path(path: str) -> str:
    """Return a comparison identity that keeps symlinks unresolved.

    Baseline entries derive from paths Git/the scanner report relative
    to an already-canonical root; the entry itself may be a tracked
    symlink whose identity is the link, not its destination —
    ``canonical_path`` would resolve the final component to the
    destination (the wrong entity, possibly outside the root, and two
    entries can collide on one destination). Neither Git nor the
    scanner descends symlinked directories, so intermediate components
    never need resolving.
    """
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def lexical_display_path(path: str) -> str:
    """Return the case-preserving spelling of ``lexical_path``.

    Baseline entries are stored in this spelling so notices can render
    the on-disk case (``normcase`` folds it away on Windows); every
    comparison against them re-folds through ``lexical_path`` or
    ``entry_path``.
    """
    return os.path.normpath(os.path.abspath(path))


def entry_path(path: str) -> str:
    """Return a comparison identity that resolves the route, not the entry.

    ``canonical_path`` also resolves the final component, so a symlink
    and the file it points at collide on one identity and correlate as
    one entry; ``lexical_path`` resolves nothing, so two routes to one
    file through an aliased directory read as two entries. Resolving
    only the directory keeps aliased routes merged while a link and its
    destination stay distinct.
    """
    parent, name = os.path.split(os.path.normpath(os.path.abspath(path)))
    return os.path.normcase(os.path.join(canonical_path(parent), name))


def entry_display_path(path: str) -> str:
    """Return the case-preserving spelling of ``entry_path``.

    Resolves the route like ``entry_path`` but keeps the on-disk case
    (``realpath`` reports real component spelling, ``normcase`` is never
    applied), so display rebasing can take a relative tail that renders
    faithfully on case-folding platforms.
    """
    norm = os.path.normpath(os.path.abspath(path))
    parent, name = os.path.split(norm)
    try:
        return os.path.join(os.path.realpath(parent), name)
    except ValueError:
        # Invalid identities (e.g. NUL in a parent) cannot name a file.
        # Keep their lexical spelling so endpoint probes can report absence.
        return norm


def git_subprocess_env() -> dict[str, str]:
    """Return an environment where derived pathspecs are always literal."""
    env = os.environ.copy()
    env["GIT_LITERAL_PATHSPECS"] = "1"
    return env


def canonical_git_pathspecs(pathspecs: Iterable[str]) -> tuple[str, ...]:
    """Stable-deduplicate pathspecs and remove segment-covered descendants."""
    result: list[str] = []
    for raw in pathspecs:
        # Backslash is a legal filename byte on POSIX; rewrite separators
        # only where backslash IS the platform separator.
        normalized = raw.replace(os.sep, "/") if os.sep != "/" else raw
        value = normalized.strip("/") or "."
        if value == ".":
            return (".",)
        if value in result or any(value.startswith(f"{parent}/") for parent in result):
            continue
        result = [existing for existing in result if not existing.startswith(f"{value}/")]
        result.append(value)
    return tuple(result)


def resolve_git_root(path: str, *, timeout: float = GIT_TIMEOUT_SECONDS) -> str | None:
    """Resolve the canonical Git worktree root containing *path*."""
    result = _run_git(
        path,
        ["rev-parse", "--show-toplevel"],
        timeout=timeout,
    )
    if result is None or result.returncode != 0:
        return None
    value = os.fsdecode(result.stdout).strip()
    return canonical_path(value) if value else None


def read_git_head(root: str, *, timeout: float = GIT_TIMEOUT_SECONDS) -> GitHeadState:
    """Read HEAD and its symbolic branch; unborn HEAD is represented by ``None``."""
    deadline = time.monotonic() + timeout
    head = read_git_head_oid(root, timeout=max(0.0, deadline - time.monotonic()))

    branch_result = _run_git(
        root,
        ["symbolic-ref", "--quiet", "--short", "HEAD"],
        timeout=max(0.0, deadline - time.monotonic()),
    )
    branch = (
        os.fsdecode(branch_result.stdout).strip() or None
        if branch_result is not None and branch_result.returncode == 0
        else None
    )
    return GitHeadState(head=head, branch=branch)


def read_git_head_oid(root: str, *, timeout: float = GIT_TIMEOUT_SECONDS) -> str | None:
    """Read HEAD's commit id; return ``None`` only for a confirmed unborn branch.

    Exit 128 also covers unreadable repositories and broken references. The
    empty-tree delta is safe only after Git reports an initial branch matching
    HEAD's symbolic reference. All probes share one deadline.
    """
    deadline = time.monotonic() + timeout
    result = _run_git(root, ["rev-parse", "--verify", "HEAD^{commit}"], timeout=timeout)
    if result is None:
        raise OSError("git rev-parse HEAD failed")
    if result.returncode == 0:
        head = os.fsdecode(result.stdout).strip()
        if head:
            return head
        raise OSError("git rev-parse HEAD returned no commit id")
    logger.debug(
        "Git HEAD probe failed for %s (exit %s): %s", root, result.returncode, os.fsdecode(result.stderr).strip()
    )
    if result.returncode == 128:
        symbolic = _run_git(root, ["symbolic-ref", "--quiet", "HEAD"], timeout=max(0.0, deadline - time.monotonic()))
        if symbolic is not None and symbolic.returncode == 0:
            ref = os.fsdecode(symbolic.stdout).strip()
            if ref.startswith("refs/heads/"):
                # Porcelain v2 works on older Git too. A broken ref can
                # report (initial), but its branch.head is (unknown); require
                # both headers so corruption is never mistaken for absence.
                status = _run_git(
                    root,
                    ["status", "--porcelain=v2", "--branch", "--untracked-files=no"],
                    timeout=max(0.0, deadline - time.monotonic()),
                )
                if status is not None and status.returncode == 0:
                    headers = status.stdout.splitlines()
                    branch_header = b"# branch.head " + os.fsencode(ref.removeprefix("refs/heads/"))
                    if b"# branch.oid (initial)" in headers and branch_header in headers:
                        return None
    raise OSError("git rev-parse HEAD failed; unborn HEAD could not be confirmed")


def read_git_status(
    root: str,
    pathspecs: tuple[str, ...],
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
    max_paths: int = GIT_PATH_LIMIT,
) -> GitStatusResult:
    """Read pathspec-scoped porcelain status without buffering unbounded output."""
    statuses: dict[str, list[str]] = {}
    pending_code: str | None = None
    truncated = False

    def _add(path_bytes: bytes, code: str) -> bool:
        nonlocal truncated
        path = os.path.normpath(os.fsdecode(path_bytes))
        if path not in statuses and len(statuses) >= max_paths:
            truncated = True
            return False
        statuses.setdefault(path, []).append(code)
        return True

    def _consume(field: bytes) -> bool:
        nonlocal pending_code
        if pending_code is not None:
            code = pending_code
            pending_code = None
            if code.startswith("R"):
                return _add(field, "D ")
            return True
        if len(field) < 4 or field[2:3] != b" ":
            raise ValueError("malformed git status record")
        code = field[:2].decode("ascii")
        path = field[3:]
        if not _add(path, code):
            return False
        if "R" in code or "C" in code:
            pending_code = code
        return True

    args = ["status", "--porcelain=v1", "-z", "--untracked-files=all"]
    if pathspecs:
        args.extend(["--", *pathspecs])
    try:
        stream = _run_git_nul_stream(root, args, timeout=timeout, consume=_consume)
    except OSError, ValueError:
        logger.debug("Git status capture failed for %s", root, exc_info=True)
        return GitStatusResult(statuses={}, failed=True)
    failed = (
        (stream.returncode != 0 and not stream.stopped)
        or (stream.malformed and not stream.stopped)
        or pending_code is not None
    )
    return GitStatusResult(
        statuses={path: tuple(codes) for path, codes in statuses.items()} if not failed else {},
        truncated=truncated or stream.stopped,
        failed=failed,
    )


def read_git_head_delta(
    root: str,
    old_head: str | None,
    new_head: str | None,
    pathspecs: tuple[str, ...],
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
    max_paths: int = GIT_PATH_LIMIT,
) -> GitDeltaResult:
    """Read a bounded scoped name-status delta, including unborn endpoints."""
    if old_head == new_head:
        return GitDeltaResult(entries=())
    if old_head is None or new_head is None:
        existing = new_head if old_head is None else old_head
        if existing is None:
            return GitDeltaResult(entries=())
        return _read_unborn_delta(
            root,
            existing,
            created=old_head is None,
            pathspecs=pathspecs,
            timeout=timeout,
            max_paths=max_paths,
        )

    entries: list[GitDeltaEntry] = []
    seen: set[str] = set()
    pending_status: str | None = None
    pending_old: str | None = None
    truncated = False

    def _reserve(*paths: str) -> bool:
        nonlocal truncated
        additions = {os.path.normpath(path) for path in paths} - seen
        if len(seen) + len(additions) > max_paths:
            truncated = True
            return False
        seen.update(additions)
        return True

    def _consume(field: bytes) -> bool:
        nonlocal pending_status, pending_old
        value = os.fsdecode(field)
        if pending_status is None:
            pending_status = value
            return True
        if pending_status.startswith(("R", "C")) and pending_old is None:
            pending_old = os.path.normpath(value)
            return True
        path = os.path.normpath(value)
        status = pending_status
        old_path = pending_old
        pending_status = None
        pending_old = None
        counted = (old_path, path) if status.startswith("R") and old_path is not None else (path,)
        if not _reserve(*counted):
            return False
        entries.append(GitDeltaEntry(status=status, path=path, old_path=old_path))
        return True

    args = ["diff", "--name-status", "-z", old_head, new_head]
    if pathspecs:
        args.extend(["--", *pathspecs])
    try:
        stream = _run_git_nul_stream(root, args, timeout=timeout, consume=_consume)
    except OSError, ValueError:
        logger.debug("Git HEAD delta failed for %s", root, exc_info=True)
        return GitDeltaResult(entries=(), failed=True)
    failed = (
        (stream.returncode != 0 and not stream.stopped)
        or (stream.malformed and not stream.stopped)
        or pending_status is not None
        or pending_old is not None
    )
    return GitDeltaResult(
        entries=tuple(entries) if not failed else (),
        truncated=truncated or stream.stopped,
        failed=failed,
    )


@dataclass(frozen=True, slots=True)
class GitEntryMeta:
    """Blob size and entry kind for one tree entry at a HEAD endpoint."""

    size: int | None
    symlink: bool


def read_git_entry_meta(
    root: str,
    head: str,
    repo_relative_path: str,
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> GitEntryMeta | None:
    """Return one tree entry's blob size and symlink-ness at *head*.

    A single ``ls-tree -l`` query replaces separate size and mode
    probes without materializing the blob.  Returns ``None`` when the
    entry is absent, is not a blob (tree/submodule), or Git could not
    answer; ``size=None`` means the entry is a blob whose size column
    was unavailable.
    """
    result = _run_git(root, ["ls-tree", "-l", "-z", head, "--", repo_relative_path], timeout=timeout)
    if result is None or result.returncode != 0:
        return None
    record = result.stdout.split(b"\0", 1)[0]
    if not record:
        return None
    fields = record.split(b"\t", 1)[0].split()
    if len(fields) < 4 or fields[1] != b"blob":
        return None
    size_field = fields[3]
    size = int(size_field) if size_field.isdigit() else None
    return GitEntryMeta(size=size, symlink=fields[0] == b"120000")


def read_git_target_kind(
    root: str,
    head: str,
    repo_relative_path: str,
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> bool | None:
    """Whether *head*'s entry at the path was a directory.

    Evidence for a symlink target's pre-command kind: the old tree is
    the only witness that predates the command under calibration — the
    working tree may have been rewritten by that very command.  True
    for a tree or submodule (a directory on disk), False for a regular
    blob, None when the entry is absent, is itself a symlink (the
    chain's end kind stays unproven), or Git could not answer.  The
    repository root (``.``) is a directory on any head that answers.
    """
    result = _run_git(root, ["ls-tree", "-z", head, "--", repo_relative_path], timeout=timeout)
    if result is None or result.returncode != 0:
        return None
    if repo_relative_path == ".":
        # "." makes ls-tree enumerate the top-level entries rather than
        # describe the root itself; the first record would be an
        # arbitrary sibling.  The root of an answering tree is a
        # directory by definition.
        return True
    record = result.stdout.split(b"\0", 1)[0]
    if not record:
        return None
    fields = record.split(b"\t", 1)[0].split()
    if len(fields) < 3:
        return None
    if fields[1] in (b"tree", b"commit"):
        return True
    if fields[1] == b"blob":
        return None if fields[0] == b"120000" else False
    return None


def read_git_blob(
    root: str,
    head: str,
    repo_relative_path: str,
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> bytes | None:
    """Read one blob from a specific HEAD endpoint."""
    result = _run_git(root, ["show", f"{head}:{repo_relative_path}"], timeout=timeout)
    return result.stdout if result is not None and result.returncode == 0 else None


def git_path_exists(
    root: str,
    head: str,
    repo_relative_path: str,
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
) -> bool | None:
    """Return path existence at *head*, or ``None`` when Git could not answer."""
    # A failed cat-file -e cannot distinguish an absent entry from an
    # unreadable tree. A successful literal ls-tree query can: empty output
    # means absent, while every command failure leaves existence unknown.
    result = _run_git(root, ["ls-tree", "-z", head, "--", repo_relative_path], timeout=timeout)
    if result is None or result.returncode != 0:
        return None
    return bool(result.stdout)


def repo_relative_git_path(root: str, path: str) -> str | None:
    """Return a literal Git-style path for *path* when it is inside *root*.

    Containment resolves only the parent route (``entry_path``): the final
    component may be a tracked symlink whose target lives outside the
    repository, and Git addresses the entry itself, never its target.
    """
    try:
        if os.path.commonpath([canonical_path(root), entry_path(path)]) != canonical_path(root):
            return None
    except ValueError:
        return None
    return PurePath(os.path.relpath(path, root)).as_posix()


def _read_unborn_delta(
    root: str,
    head: str,
    *,
    created: bool,
    pathspecs: tuple[str, ...],
    timeout: float,
    max_paths: int,
) -> GitDeltaResult:
    entries: list[GitDeltaEntry] = []
    seen: set[str] = set()
    truncated = False

    def _consume(field: bytes) -> bool:
        nonlocal truncated
        path = os.path.normpath(os.fsdecode(field))
        if path not in seen and len(seen) >= max_paths:
            truncated = True
            return False
        seen.add(path)
        entries.append(GitDeltaEntry(status="A" if created else "D", path=path))
        return True

    args = ["ls-tree", "-r", "-z", "--name-only", head]
    if pathspecs:
        args.extend(["--", *pathspecs])
    try:
        stream = _run_git_nul_stream(root, args, timeout=timeout, consume=_consume)
    except OSError, ValueError:
        logger.debug("Git unborn HEAD delta failed for %s", root, exc_info=True)
        return GitDeltaResult(entries=(), failed=True)
    failed = (stream.returncode != 0 and not stream.stopped) or (stream.malformed and not stream.stopped)
    return GitDeltaResult(
        entries=tuple(entries) if not failed else (),
        truncated=truncated or stream.stopped,
        failed=failed,
    )


def _run_git(root: str, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes] | None:
    from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs

    if timeout <= 0:
        return None
    executable = shutil.which("git")
    if executable is None:
        return None
    try:
        return subprocess.run(  # noqa: S603
            [executable, *args],
            cwd=root,
            env=git_subprocess_env(),
            # Non-interactive probe: never hand the child the process stdin.
            # Under ACP that stream is the JSON-RPC pipe — a child that reads
            # it stalls on the protocol traffic (and swallows what it reads).
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
            **windows_hidden_subprocess_kwargs(),
        )
    except OSError, subprocess.TimeoutExpired:
        return None


def _run_git_nul_stream(
    root: str,
    args: list[str],
    *,
    timeout: float,
    consume: Callable[[bytes], bool],
) -> _StreamResult:
    """Stream NUL fields under a monotonic deadline and reap the child."""
    from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs

    if timeout <= 0:
        raise OSError("git deadline expired")
    executable = shutil.which("git")
    if executable is None:
        raise OSError("git executable unavailable")
    proc = subprocess.Popen(  # noqa: S603
        [executable, *args],
        cwd=root,
        env=git_subprocess_env(),
        # Same contract as ``_run_git``: no inherited (protocol) stdin.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        **windows_hidden_subprocess_kwargs(),
    )
    stdout = proc.stdout
    if not isinstance(stdout, BufferedReader):
        proc.kill()
        proc.wait()
        raise OSError("git stdout pipe unavailable")

    chunks: queue.Queue[bytes | BaseException | None] = queue.Queue(maxsize=2)

    def _reader() -> None:
        try:
            while data := stdout.read1(65_536):
                chunks.put(data)
        except BaseException as exc:
            chunks.put(exc)
        finally:
            chunks.put(None)

    reader = threading.Thread(target=_reader, name="chrys-git-stream", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    buffer = bytearray()
    stopped = False
    malformed = False
    reader_error: BaseException | None = None
    saw_eof = False
    try:
        while not saw_eof:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("git stream deadline expired")
            try:
                item = chunks.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError("git stream deadline expired") from exc
            if item is None:
                saw_eof = True
                break
            if isinstance(item, BaseException):
                reader_error = item
                saw_eof = True
                break
            if stopped:
                continue
            buffer.extend(item)
            while (separator := buffer.find(0)) >= 0:
                field = bytes(buffer[:separator])
                del buffer[: separator + 1]
                if not consume(field):
                    stopped = True
                    proc.terminate()
                    break
        malformed = bool(buffer)
    except TimeoutError, ValueError:
        proc.terminate()
        raise
    finally:
        remaining = max(0.05, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        drain_deadline = time.monotonic() + 1.0
        while reader.is_alive() and time.monotonic() < drain_deadline:
            try:
                chunks.get(timeout=min(0.05, drain_deadline - time.monotonic()))
            except queue.Empty:
                continue
        reader.join(timeout=1.0)
        stdout.close()
    if reader_error is not None:
        raise OSError("git stream read failed") from reader_error
    return _StreamResult(returncode=proc.returncode, stopped=stopped, malformed=malformed)
