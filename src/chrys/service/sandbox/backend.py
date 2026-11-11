# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fail-closed sandbox shell execution.

``run_shell`` is the only entry Agent H calls. It never starts a host shell.
A command that exits non-zero is a ``SandboxExec``. Anything else that is
not a completed exec — transport loss, timeout, non-2xx, health other than
``ok``, a sandbox that does not become ``ready``, a missing ``exit_code`` —
raises ``SandboxError``. ``str`` of that error is safe for ``tool_error``:
it is text this module wrote, not ``str()`` of an HTTP or OS exception, and
it never includes the bearer token or URL userinfo.

Create body and append
----------------------
The vendored server manager resolves ``policy_mode=append`` by copying the
server base policy and merging the request fragment. Append merge walks
mappings key by key, so a fragment that sets only
``filesystem_policy.bind_mounts`` keeps every other filesystem field from
the base policy (read-only paths, bind-root entries, devices, and so on).
When both sides of a key are lists, items in the fragment are appended when
an equal item is not already present. Scalars in a fragment would replace
the base value; this client does not send any.

Append therefore does not replace ``filesystem_policy`` as a whole, and this
client does not read the current policy back to build a merged document.

Writable root
-------------
When ``workspace_root`` is provided, it is the session workspace. The
read-write mount and the cache key are that root, resolved with
``os.path.realpath`` before either use, not the command cwd. The process
cwd stays the command cwd (exec ``workdir``). A cwd inside that root, or
inside a ``workspace_dirs`` entry that is not already under it, reuses the
writable sandbox. Those extra directories are further read-write binds on
the same sandbox. The cache key is the physical primary root, so two
workspaces do not share one mount. A cwd outside that set does not replace
the writable sandbox and is not mounted read-write; it runs in a read-only
sandbox. Binding ``/`` is not special: the base policy remounts ``/``
read-only.

Paths are compared as components after ``os.path.realpath``, so a sibling
such as ``work-extra`` is not inside ``work``. Two symlink spellings of one
directory are one sandbox and one read-write mount of the physical path.

When ``workspace_root`` is omitted, the first command cwd remains the
writable root of a single legacy slot. That slot is separate from the
workspace-keyed cache.

A 404 for a cached id drops that one entry and creates one replacement.
Any other HTTP error, and a second 404, still fail the call. Replacing a
read-only sandbox deletes the previous read-only sandbox. That delete is
not cancellation.

Lock and command budget
-----------------------
The process lock covers reading or updating that cache and creating a
sandbox. It does not cover the exec request, so two commands that already
have sandbox ids run their execs together. Time spent waiting to acquire
the lock counts against ``timeout_seconds``. When that wait uses up the
budget, the call raises ``SandboxError`` and does not exec. The exec
request's HTTP timeout is the time still left plus the usual slack, not
the original budget added on after the wait. The server's whole-second
timeout is that same remainder, ignoring only the brief scheduling gap of
an uncontended acquire.

Cancellation
------------
Cancelling the task that is awaiting an exec does not POST ``/stop``.
Stopping the sandbox would kill every other command in it. ``CancelledError``
is reraised. The writable cache entry is kept, so the next command reuses
that workspace's sandbox instead of adopting a new cwd as the writable root.
A read-only id for the cancelled command is forgotten. That forget is
shielded, so a second cancellation does not skip it. A cancel before this
call has started an exec, a ``SandboxError``, and a completed exec do not
change the cache.
"""

from __future__ import annotations

import asyncio
import os
import posixpath
import re
import time
from collections.abc import Sequence
from pathlib import PurePath, PurePosixPath
from typing import TYPE_CHECKING, Any

from chrys.foundation.util.once_close import finish_close
from chrys.service.sandbox.types import SandboxSettings

if TYPE_CHECKING:
    import httpx

_HTTP_TIMEOUT_SLACK_SECONDS = 15.0
_STOP_TIMEOUT_SECONDS = 2.0
_MAX_READY_ERROR_CHARS = 200
# Scheme, "://", and the rest of the token, userinfo and path included.
_READY_ERROR_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://\S+")
# create_task plus wait_for yields once even when the lock is free. A gap
# smaller than this is that hop, not a command queued behind another.
_LOCK_SCHEDULING_SLACK_SECONDS = 0.25
_READY = "ready"
_READ_WRITE = "rw"
_READ_ONLY = "ro"
_CACHE_MISSING = "missing"
_CACHE_REPLACE = "replace"
_LOCK_WAIT_EXPIRED = "Sandbox command timed out waiting for the sandbox."

# Process cache.
# ``_sandbox_id`` / ``_writable_root`` are the legacy slot used when the
# caller omits ``workspace_root``: the first command cwd is the writable root.
# ``_writable_by_root`` maps a physical workspace root to its read-write
# sandbox. Cancel does not drop either writable entry.
# ``_readonly_id`` mounts ``_readonly_root`` read-only and never replaces a
# writable entry. Switching it deletes the sandbox being replaced.
_sandbox_id: str | None = None
_writable_root: str | None = None
_writable_by_root: dict[str, str] = {}
_readonly_id: str | None = None
_readonly_root: str | None = None
_readonly_physical: bool = False
_lock: asyncio.Lock | None = None


class SandboxError(Exception):
    """The sandbox call failed closed.

    ``str(self)`` is written by chrys and is what the shell tool passes to
    ``tool_error``. It must not carry a token, URL userinfo, or the text of
    an HTTP or OS exception.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class SandboxExec:
    """One completed remote command. ``exit_code`` may be non-zero."""

    __slots__ = ("exit_code", "stderr", "stdout")

    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def _monotonic() -> float:
    """Clock for the cache-lock budget. Tests replace this, not ``time.monotonic``."""
    return time.monotonic()


def _gate() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def run_shell(
    settings: SandboxSettings,
    *,
    argv: Sequence[str],
    cwd: str,
    timeout_seconds: int,
    linux: bool,
    workspace_root: str | None = None,
    workspace_dirs: Sequence[str] = (),
) -> SandboxExec:
    """Run ``argv`` in the sandbox and return its exit code and output.

    Called only when the sandbox is enabled. ``linux=False`` raises
    immediately and does not open a connection or start a server. An empty
    ``sandbox_url`` asks the supervisor to start the bundled server. That
    server authenticates with a token the supervisor creates and keeps in
    memory; ``sandbox_token`` is not sent to it. A non-empty URL is used
    as-is, does not start a process, and is the only place ``sandbox_token``
    is sent. Waiting for the cache lock consumes ``timeout_seconds``.
    Cancellation during exec does not stop the sandbox, and then propagates.
    The writable cache is kept across that cancellation.

    When ``workspace_root`` is provided, the read-write mount is that
    session workspace (plus ``workspace_dirs`` outside it), not ``cwd``.
    When it is omitted, the first command cwd remains the writable root.
    """
    if not linux:
        raise SandboxError("Sandbox execution is supported only on Linux.")

    # Imported here so a non-Linux call does not touch the client, and so this
    # module can define SandboxError before the client imports it back.
    from chrys.service.sandbox.client import connect
    from chrys.service.sandbox.supervisor import ensure_started

    configured = settings.sandbox_url
    if configured:
        base_url = configured
        token = settings.sandbox_token
    else:
        base_url, token = await ensure_started()
    # Opened before the lock wait. Health and create use this budget; the exec
    # request replaces it with the time left after the wait. trust_env stays on
    # for a configured URL and is off for the bundled server this process started.
    http_timeout = float(timeout_seconds) + _HTTP_TIMEOUT_SLACK_SECONDS
    async with connect(base_url, token, http_timeout, trust_env=bool(configured)) as client:
        await _require_health(client)
        sandbox_id, remaining = await _sandbox_for_command(
            client,
            cwd,
            timeout_seconds,
            workspace_root=workspace_root,
            workspace_dirs=workspace_dirs,
        )
        try:
            return await _exec(client, sandbox_id, argv, cwd, timeout_seconds, remaining)
        except asyncio.CancelledError:
            await _stop_on_cancel(sandbox_id)
            raise


async def _require_health(client: httpx.AsyncClient) -> None:
    payload = _json_object(await client.get("/health"))
    if payload.get("status") != "ok":
        raise SandboxError("Sandbox server health is not ok.")


async def _sandbox_for_command(
    client: httpx.AsyncClient,
    cwd: str,
    timeout_seconds: int,
    *,
    workspace_root: str | None,
    workspace_dirs: Sequence[str],
) -> tuple[str, float]:
    """Return a ready sandbox id and the command budget left after the lock wait.

    The lock is held only while the cache is read or updated and while a
    sandbox is created. The caller execs after this returns.
    """
    remaining = await _acquire_cache_lock(float(timeout_seconds))
    try:
        sandbox_id = await _ready_sandbox(
            client,
            cwd,
            workspace_root=workspace_root,
            workspace_dirs=workspace_dirs,
        )
        return sandbox_id, remaining
    finally:
        _gate().release()


async def _ready_sandbox(
    client: httpx.AsyncClient,
    cwd: str,
    *,
    workspace_root: str | None,
    workspace_dirs: Sequence[str],
) -> str:
    if workspace_root is None:
        return await _ready_legacy_sandbox(client, cwd)
    return await _ready_workspace_sandbox(client, cwd, workspace_root, workspace_dirs)


async def _ready_legacy_sandbox(client: httpx.AsyncClient, cwd: str) -> str:
    """Writable root is the first command cwd. Used when no workspace is passed."""
    mode, current = _select_cache(cwd)
    if current is not None:
        state = await _cached_phase(client, current)
        if state == _READY:
            return current
        if state == _CACHE_MISSING:
            _drop_cache(mode)
    sandbox_id = await _create_ready(client, _create_body(cwd, mode))
    if mode == _READ_ONLY:
        await _replace_readonly(client, sandbox_id, cwd, physical=False)
    else:
        _store_cache(sandbox_id, cwd)
    return sandbox_id


async def _ready_workspace_sandbox(
    client: httpx.AsyncClient,
    cwd: str,
    workspace_root: str,
    workspace_dirs: Sequence[str],
) -> str:
    """Read-write mount is the physical workspace, not ``cwd``."""
    mounts = _workspace_mounts(workspace_root, workspace_dirs)
    physical_cwd = _physical_dir(cwd)
    if _cwd_uses_writable(physical_cwd, mounts):
        return await _ready_keyed_writable(client, mounts[0], mounts)
    return await _ready_physical_readonly(client, physical_cwd)


async def _ready_keyed_writable(
    client: httpx.AsyncClient,
    primary: str,
    mounts: tuple[str, ...],
) -> str:
    """Reuse or create the read-write sandbox keyed by ``primary``.

    A 404 drops only this root's id. Other workspace roots stay.
    """
    cached = _writable_by_root.get(primary)
    if cached is not None:
        state = await _cached_phase(client, cached)
        if state == _READY:
            return cached
        if state == _CACHE_MISSING:
            _writable_by_root.pop(primary, None)
    sandbox_id = await _create_ready(client, _bind_body(mounts, _READ_WRITE))
    _writable_by_root[primary] = sandbox_id
    return sandbox_id


async def _ready_physical_readonly(client: httpx.AsyncClient, physical_cwd: str) -> str:
    """Run ``physical_cwd`` in a read-only sandbox without touching the writable one."""
    cached = _readonly_id
    if cached is not None and _readonly_covers(physical_cwd, physical=True):
        state = await _cached_phase(client, cached)
        if state == _READY:
            return cached
        if state == _CACHE_MISSING:
            _drop_readonly()
    sandbox_id = await _create_ready(client, _bind_body((physical_cwd,), _READ_ONLY))
    await _replace_readonly(client, sandbox_id, physical_cwd, physical=True)
    return sandbox_id


def _readonly_covers(cwd_key: str, *, physical: bool) -> bool:
    """Whether the cached read-only sandbox already contains ``cwd_key``."""
    if _readonly_id is None or _readonly_root is None or _readonly_physical != physical:
        return False
    if physical:
        return _same_or_inside_physical(_readonly_root, cwd_key)
    return _same_or_inside(_readonly_root, cwd_key)


async def _cached_phase(client: httpx.AsyncClient, sandbox_id: str) -> str:
    """``ready``, ``missing`` (HTTP 404), or ``replace`` (any other phase).

    Any HTTP error other than 404 propagates. The caller drops the one
    cache entry that 404'd and creates a single replacement.
    """
    try:
        payload = _json_object(await client.get(_sandbox_path(sandbox_id)))
    except SandboxError as error:
        if error.status_code != 404:
            raise
        return _CACHE_MISSING
    if payload.get("phase") == _READY:
        return _READY
    return _CACHE_REPLACE


async def _create_ready(client: httpx.AsyncClient, body: dict[str, Any]) -> str:
    created = _json_object(await client.post("/api/v1/sandboxes", json=body))
    sandbox_id = _require_id(created)
    if created.get("phase") != _READY:
        started = _json_object(await client.post(f"{_sandbox_path(sandbox_id)}/start"))
        sandbox_id = _require_id(started)
        if started.get("phase") != _READY:
            raise _not_ready(started)
    return sandbox_id


async def _replace_readonly(
    client: httpx.AsyncClient,
    sandbox_id: str,
    root: str,
    *,
    physical: bool,
) -> None:
    """Point the read-only cache at ``sandbox_id`` and delete a different previous id."""
    previous = _readonly_id
    if previous is not None and previous != sandbox_id:
        await _retire_replaced_readonly(client, previous)
    _store_readonly(sandbox_id, root, physical=physical)


async def _retire_replaced_readonly(client: httpx.AsyncClient, sandbox_id: str) -> None:
    """Delete a read-only sandbox that a different root is replacing.

    This is not cancellation. Cancellation does not POST ``/stop`` and does
    not delete the sandbox. It only forgets a read-only id and leaves the
    writable cache in place. This function DELETEs
    ``/api/v1/sandboxes/{id}`` and runs only when the read-only cache is
    about to point at a different sandbox. A failed delete does not fail
    the command; the caller still stores the replacement.
    """
    try:
        await client.delete(_sandbox_path(sandbox_id), timeout=_STOP_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:
        return


async def _acquire_cache_lock(budget: float) -> float:
    """Take the cache lock and return the command budget still left.

    A free lock does not reduce the budget. A busy lock is waited on only
    until ``budget`` runs out, and that wait is subtracted. An exhausted
    budget raises ``SandboxError`` and does not leave the lock held.
    """
    if budget <= 0:
        raise SandboxError(_LOCK_WAIT_EXPIRED)
    lock = _gate()
    # A free lock is taken without suspending, so the caller's budget stays
    # exact. The timed path below is only for a lock another command holds.
    if not lock.locked():
        await lock.acquire()
        return budget
    started = _monotonic()
    pending = asyncio.create_task(lock.acquire())
    try:
        await asyncio.wait_for(asyncio.shield(pending), budget)
    except TimeoutError:
        await _drop_acquire(lock, pending)
        raise SandboxError(_LOCK_WAIT_EXPIRED) from None
    except asyncio.CancelledError:
        # The caller's cancellation must not leave the lock acquired.
        await finish_close(asyncio.create_task(_drop_acquire(lock, pending)))
        raise
    remaining = budget - (_monotonic() - started)
    if remaining <= 0:
        lock.release()
        raise SandboxError(_LOCK_WAIT_EXPIRED)
    return remaining


async def _drop_acquire(lock: asyncio.Lock, pending: asyncio.Task[bool]) -> None:
    """Finish a lock acquire that this call is giving up, and release if it won."""
    if not pending.done():
        pending.cancel()
    took_lock = False
    try:
        took_lock = await pending
    except asyncio.CancelledError:
        took_lock = False
    if took_lock:
        lock.release()


async def _exec(
    client: httpx.AsyncClient,
    sandbox_id: str,
    argv: Sequence[str],
    cwd: str,
    timeout_seconds: int,
    remaining: float,
) -> SandboxExec:
    command_timeout = _exec_timeout_seconds(timeout_seconds, remaining)
    payload = _json_object(
        await client.post(
            f"{_sandbox_path(sandbox_id)}/exec",
            json={
                "command": list(argv),
                "workdir": cwd,
                "timeout_seconds": command_timeout,
            },
            timeout=remaining + _HTTP_TIMEOUT_SLACK_SECONDS,
        )
    )
    exit_code = payload.get("exit_code")
    if type(exit_code) is not int:
        raise SandboxError("Sandbox exec response did not include an exit code.")
    return SandboxExec(exit_code, _stream_text(payload, "stdout"), _stream_text(payload, "stderr"))


def _exec_timeout_seconds(timeout_seconds: int, remaining: float) -> int:
    """Whole seconds the remote exec may run.

    The server field is an int. The scheduling gap of an uncontended acquire
    keeps the caller's value. A longer wait drops the whole seconds already
    spent. A leftover fraction of a second still sends one second rather
    than zero, which the server treats as an immediate timeout.
    """
    if remaining > float(timeout_seconds) - _LOCK_SCHEDULING_SLACK_SECONDS:
        return timeout_seconds
    whole = int(remaining)
    if whole < 1:
        return 1
    return whole


async def _stop_on_cancel(sandbox_id: str) -> None:
    """After exec was cancelled, keep the writable cache.

    ``finish_close`` keeps the cache update running when the caller is
    cancelled again. This does not POST ``/stop``: that would kill every
    command in the sandbox. The writable root stays cached. A read-only id
    is forgotten.
    """
    task = asyncio.create_task(_stop_and_forget(sandbox_id))
    try:
        await finish_close(task)
    except asyncio.CancelledError:
        raise
    except Exception:
        return


async def _stop_and_forget(sandbox_id: str) -> None:
    """Forget a cancelled read-only id. The writable cache stays.

    Dropping the writable root would make the next command's cwd the new
    writable root. This function does not POST ``/stop``.
    """
    await _forget_cached_id(sandbox_id)


async def _forget_cached_id(sandbox_id: str) -> None:
    """Drop a cancelled read-only id. The writable cache is not touched."""
    lock = _gate()
    await lock.acquire()
    try:
        _clear_cached_id(sandbox_id)
    finally:
        lock.release()


def _clear_cached_id(sandbox_id: str) -> None:
    """Forget a cancelled read-only sandbox id.

    Writable entries are left in place. Clearing the writable root would
    make the next command's cwd the new writable root.
    """
    if _readonly_id == sandbox_id:
        _drop_readonly()


def _not_ready(payload: dict[str, Any]) -> SandboxError:
    """The start response was not ``ready``.

    A 2xx body is a sandbox ref. The engine failure is ``error_message`` on
    that ref. The ``error`` key belongs to the HTTP error envelope, which
    this path does not receive. The detail is the first usable line of
    ``error_message``: URL tokens removed, a long line shortened, and a bad
    first line does not hide a later usable line. When no line survives, the
    message stays the generic sentence.
    """
    detail = _short_error_line(payload.get("error_message"))
    if detail is None:
        return SandboxError("Sandbox did not become ready.")
    return SandboxError(f"Sandbox did not become ready: {detail}")


def _short_error_line(value: object) -> str | None:
    """The first usable line, with URLs removed and a long line shortened.

    Lines are walked in order. An empty line, a line that contains a
    control character, and a line that is empty once URL tokens are removed
    are skipped. A bad first line does not hide a later usable line. The
    surviving line is the whole detail: later lines are not appended. A line
    longer than ``_MAX_READY_ERROR_CHARS`` keeps a prefix of that limit and
    ends with ``…``.
    """
    if not isinstance(value, str):
        return None
    for raw in value.splitlines():
        line = raw.strip()
        if not line:
            continue
        # A control character disqualifies the whole line. It is never copied.
        if any(ord(char) < 32 for char in line):
            continue
        line = " ".join(_READY_ERROR_URL_RE.sub("", line).split())
        if not line:
            continue
        if len(line) > _MAX_READY_ERROR_CHARS:
            line = line[: _MAX_READY_ERROR_CHARS - 1] + "…"
        return line
    return None


def _bind_body(mounts: Sequence[str], mode: str) -> dict[str, Any]:
    """Append bind mounts. ``host_path`` and ``sandbox_path`` are the same path."""
    return {
        "policy_mode": "append",
        "policy": {
            "filesystem_policy": {
                "bind_mounts": [{"host_path": path, "sandbox_path": path, "mode": mode} for path in mounts],
            }
        },
    }


def _create_body(cwd: str, mode: str) -> dict[str, Any]:
    """Legacy single mount. ``cwd`` is the command cwd, not a workspace root."""
    return _bind_body((cwd,), mode)


def _workspace_mounts(workspace_root: str, workspace_dirs: Sequence[str]) -> tuple[str, ...]:
    """Physical read-write binds for one session workspace.

    The primary root is first. ``workspace_dirs`` entries already inside it
    are not repeated. The command cwd is not a mount.
    """
    primary = _physical_dir(workspace_root)
    mounts = [primary]
    for raw in workspace_dirs:
        physical = _physical_dir(raw)
        if _same_or_inside_physical(primary, physical) or physical in mounts:
            continue
        mounts.append(physical)
    return tuple(mounts)


def _cwd_uses_writable(physical_cwd: str, mounts: Sequence[str]) -> bool:
    """Whether ``physical_cwd`` is one of the workspace mounts or inside it."""
    return any(_same_or_inside_physical(mount, physical_cwd) for mount in mounts)


def _physical_dir(path: str) -> str:
    """Real directory path, not case-folded.

    ``os.path.realpath`` before the path is a mount or a cache key.
    """
    return os.path.realpath(path)


def _select_cache(cwd: str) -> tuple[str, str | None]:
    """Return the legacy mount mode and a cached id to reuse, if one covers ``cwd``.

    The first sandbox is writable at the cwd of its create. A later cwd
    outside that root is read-only and must not replace the writable entry.
    This slot is used only when the caller omits ``workspace_root``.
    """
    if _sandbox_id is not None and _writable_root is not None and _same_or_inside(_writable_root, cwd):
        return _READ_WRITE, _sandbox_id
    if _sandbox_id is None or _writable_root is None:
        return _READ_WRITE, None
    if _readonly_covers(cwd, physical=False):
        return _READ_ONLY, _readonly_id
    return _READ_ONLY, None


def _same_or_inside(root: str, cwd: str) -> bool:
    """Whether ``cwd`` is ``root`` or a directory inside it.

    Compared as normalized POSIX components. ``/work-extra`` is not inside
    ``/work``, and ``/work`` is not inside ``/work/sub``.
    """
    root_parts = _posix_parts(root)
    cwd_parts = _posix_parts(cwd)
    if len(cwd_parts) < len(root_parts):
        return False
    return cwd_parts[: len(root_parts)] == root_parts


def _same_or_inside_physical(root: str, path: str) -> bool:
    """Whether ``path`` is ``root`` or a directory inside it.

    Both arguments are already ``os.path.realpath`` results. Compared as
    native path components so ``work-extra`` is not inside ``work``.
    """
    root_parts = PurePath(root).parts
    path_parts = PurePath(path).parts
    if len(path_parts) < len(root_parts):
        return False
    return path_parts[: len(root_parts)] == root_parts


def _posix_parts(path: str) -> tuple[str, ...]:
    return PurePosixPath(posixpath.normpath(path)).parts


def _drop_cache(mode: str) -> None:
    """Drop the legacy writable slot or the read-only slot, not both."""
    global _sandbox_id, _writable_root
    if mode == _READ_WRITE:
        _sandbox_id = None
        _writable_root = None
        return
    _drop_readonly()


def _drop_readonly() -> None:
    global _readonly_id, _readonly_root, _readonly_physical
    _readonly_id = None
    _readonly_root = None
    _readonly_physical = False


def _store_cache(sandbox_id: str, cwd: str) -> None:
    """Legacy writable slot. The mounted root is this command cwd."""
    global _sandbox_id, _writable_root
    _sandbox_id = sandbox_id
    _writable_root = cwd


def _store_readonly(sandbox_id: str, root: str, *, physical: bool) -> None:
    """Remember the read-only root. ``physical`` selects the comparison rule."""
    global _readonly_id, _readonly_root, _readonly_physical
    _readonly_id = sandbox_id
    _readonly_root = root
    _readonly_physical = physical


def _sandbox_path(sandbox_id: str) -> str:
    return f"/api/v1/sandboxes/{sandbox_id}"


def _require_id(payload: dict[str, Any]) -> str:
    sandbox_id = payload.get("id")
    if not isinstance(sandbox_id, str) or not sandbox_id or any(char in sandbox_id for char in "/\\?#\x00"):
        raise SandboxError("Sandbox server returned an unusable sandbox id.")
    return sandbox_id


def _json_object(response: httpx.Response) -> dict[str, Any]:
    if response.status_code < 200 or response.status_code >= 300:
        raise SandboxError(
            f"Sandbox server returned HTTP {response.status_code}.",
            status_code=response.status_code,
        )
    try:
        payload = response.json()
    except ValueError:
        raise SandboxError("Sandbox server returned a response that was not JSON.") from None
    if not isinstance(payload, dict):
        raise SandboxError("Sandbox server returned a response that was not a JSON object.")
    return payload


def _stream_text(payload: dict[str, Any], key: str) -> str:
    if key not in payload or payload[key] is None:
        return ""
    value = payload[key]
    if isinstance(value, str):
        return value
    raise SandboxError("Sandbox exec response was not usable.")
