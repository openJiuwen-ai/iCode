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
The create body is ``policy_mode`` ``append`` plus one bind mount whose
``host_path`` and ``sandbox_path`` are both the command cwd. The mount is
``rw`` for the writable sandbox and ``ro`` when this cwd is outside that
sandbox's writable root.

The writable sandbox is cached with the cwd it mounted. A later command
reuses that id while ``GET`` reports ``phase=ready`` and the new cwd is
that root or a directory inside it. Paths are compared as normalized POSIX
components, so ``/work`` does not contain ``/work-extra``. A cwd outside
that root does not replace the writable sandbox and does not add a second
``rw`` mount; it runs in a separate read-only sandbox. A 404 for either
cached id drops that entry and creates one replacement. Any other HTTP
error, and a second 404, still fail the call.

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
Cancelling the task that is awaiting an exec POSTs ``/stop`` for that
sandbox id, drops the id from the cache, and reraises ``CancelledError``.
The stop is shielded, so the cancellation does not skip it, and it is
bounded. A stop failure does not replace the cancellation. A cancel before
this call has started an exec, a ``SandboxError``, and a completed exec do
not stop the sandbox.
"""

from __future__ import annotations

import asyncio
import posixpath
import time
from collections.abc import Sequence
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from chrys.foundation.util.once_close import finish_close
from chrys.service.sandbox.types import SandboxSettings

if TYPE_CHECKING:
    import httpx

_HTTP_TIMEOUT_SLACK_SECONDS = 15.0
_STOP_TIMEOUT_SECONDS = 2.0
_MAX_READY_ERROR_CHARS = 200
# create_task plus wait_for yields once even when the lock is free. A gap
# smaller than this is that hop, not a command queued behind another.
_LOCK_SCHEDULING_SLACK_SECONDS = 0.25
_READY = "ready"
_READ_WRITE = "rw"
_READ_ONLY = "ro"
_LOCK_WAIT_EXPIRED = "Sandbox command timed out waiting for the sandbox."

# Process cache. ``_sandbox_id`` mounts ``_writable_root`` read-write.
# ``_readonly_id`` mounts ``_readonly_root`` read-only and never replaces
# the writable entry.
_sandbox_id: str | None = None
_writable_root: str | None = None
_readonly_id: str | None = None
_readonly_root: str | None = None
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
) -> SandboxExec:
    """Run ``argv`` in the sandbox and return its exit code and output.

    Called only when the sandbox is enabled. ``linux=False`` raises
    immediately and does not open a connection or start a server. An empty
    ``sandbox_url`` asks the supervisor to start the bundled server. That
    server authenticates with a token the supervisor creates and keeps in
    memory; ``sandbox_token`` is not sent to it. A non-empty URL is used
    as-is, does not start a process, and is the only place ``sandbox_token``
    is sent. Waiting for the cache lock consumes ``timeout_seconds``.
    Cancellation during exec stops that sandbox and then propagates.
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
    # request replaces it with the time left after the wait.
    http_timeout = float(timeout_seconds) + _HTTP_TIMEOUT_SLACK_SECONDS
    async with connect(base_url, token, http_timeout) as client:
        await _require_health(client)
        sandbox_id, remaining = await _sandbox_for_command(client, cwd, timeout_seconds)
        try:
            return await _exec(client, sandbox_id, argv, cwd, timeout_seconds, remaining)
        except asyncio.CancelledError:
            await _stop_on_cancel(client, sandbox_id)
            raise


async def _require_health(client: httpx.AsyncClient) -> None:
    payload = _json_object(await client.get("/health"))
    if payload.get("status") != "ok":
        raise SandboxError("Sandbox server health is not ok.")


async def _ready_sandbox(client: httpx.AsyncClient, cwd: str) -> str:
    mode, current = _select_cache(cwd)
    if current is not None:
        try:
            payload = _json_object(await client.get(_sandbox_path(current)))
        except SandboxError as error:
            if error.status_code != 404:
                raise
            # The cached id is gone after a server restart or a URL change.
            # Drop that one entry and create a single replacement.
            _drop_cache(mode)
        else:
            if payload.get("phase") == _READY:
                return current
    created = _json_object(await client.post("/api/v1/sandboxes", json=_create_body(cwd, mode)))
    sandbox_id = _require_id(created)
    if created.get("phase") != _READY:
        started = _json_object(await client.post(f"{_sandbox_path(sandbox_id)}/start"))
        sandbox_id = _require_id(started)
        if started.get("phase") != _READY:
            raise _not_ready(started)
    _store_cache(mode, sandbox_id, cwd)
    return sandbox_id


async def _sandbox_for_command(
    client: httpx.AsyncClient,
    cwd: str,
    timeout_seconds: int,
) -> tuple[str, float]:
    """Return a ready sandbox id and the command budget left after the lock wait.

    The lock is held only while the cache is read or updated and while a
    sandbox is created. The caller execs after this returns.
    """
    remaining = await _acquire_cache_lock(float(timeout_seconds))
    try:
        return await _ready_sandbox(client, cwd), remaining
    finally:
        _gate().release()


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


async def _stop_on_cancel(client: httpx.AsyncClient, sandbox_id: str) -> None:
    """Stop ``sandbox_id`` after exec was cancelled, then forget that id.

    ``finish_close`` keeps the stop running when the caller is cancelled
    again. Failures inside the stop are discarded so they cannot replace the
    caller's ``CancelledError``.
    """
    task = asyncio.create_task(_stop_and_forget(client, sandbox_id))
    try:
        await finish_close(task)
    except asyncio.CancelledError:
        raise
    except Exception:
        return


async def _stop_and_forget(client: httpx.AsyncClient, sandbox_id: str) -> None:
    try:
        await client.post(f"{_sandbox_path(sandbox_id)}/stop", timeout=_STOP_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        # Drop the id whether or not the stop request succeeded, so the next
        # command does not reuse a sandbox this call tried to stop.
        await _forget_cached_id(sandbox_id)


async def _forget_cached_id(sandbox_id: str) -> None:
    """Drop ``sandbox_id`` from the cache if it is still the stored one."""
    lock = _gate()
    await lock.acquire()
    try:
        _clear_cached_id(sandbox_id)
    finally:
        lock.release()


def _clear_cached_id(sandbox_id: str) -> None:
    global _sandbox_id, _writable_root, _readonly_id, _readonly_root
    if _sandbox_id == sandbox_id:
        _sandbox_id = None
        _writable_root = None
    if _readonly_id == sandbox_id:
        _readonly_id = None
        _readonly_root = None


def _not_ready(payload: dict[str, Any]) -> SandboxError:
    """The start response was not ``ready``.

    A 2xx body is a sandbox ref. The engine failure is ``error_message`` on
    that ref. The ``error`` key belongs to the HTTP error envelope, which
    this path does not receive. One short line of ``error_message`` is safe
    to show. Anything else stays the generic sentence.
    """
    detail = _short_error_line(payload.get("error_message"))
    if detail is None:
        return SandboxError("Sandbox did not become ready.")
    return SandboxError(f"Sandbox did not become ready: {detail}")


def _short_error_line(value: object) -> str | None:
    """One short server error line, with URLs and control characters left out."""
    if not isinstance(value, str):
        return None
    line = value.strip()
    if not line or len(line) > _MAX_READY_ERROR_CHARS:
        return None
    if any(ord(char) < 32 for char in line):
        return None
    if "://" in line:
        return None
    return line


def _create_body(cwd: str, mode: str) -> dict[str, Any]:
    return {
        "policy_mode": "append",
        "policy": {
            "filesystem_policy": {
                "bind_mounts": [
                    {"host_path": cwd, "sandbox_path": cwd, "mode": mode},
                ]
            }
        },
    }


def _select_cache(cwd: str) -> tuple[str, str | None]:
    """Return the mount mode and a cached id to reuse, if one covers ``cwd``.

    The first sandbox is writable at the cwd of its create. A later cwd
    outside that root is read-only and must not replace the writable entry.
    """
    if _sandbox_id is not None and _writable_root is not None and _same_or_inside(_writable_root, cwd):
        return _READ_WRITE, _sandbox_id
    if _sandbox_id is None or _writable_root is None:
        return _READ_WRITE, None
    if _readonly_id is not None and _readonly_root is not None and _same_or_inside(_readonly_root, cwd):
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


def _posix_parts(path: str) -> tuple[str, ...]:
    return PurePosixPath(posixpath.normpath(path)).parts


def _drop_cache(mode: str) -> None:
    global _sandbox_id, _writable_root, _readonly_id, _readonly_root
    if mode == _READ_WRITE:
        _sandbox_id = None
        _writable_root = None
        return
    _readonly_id = None
    _readonly_root = None


def _store_cache(mode: str, sandbox_id: str, cwd: str) -> None:
    """Remember the cwd this create mounted. That path is the cached root."""
    global _sandbox_id, _writable_root, _readonly_id, _readonly_root
    if mode == _READ_WRITE:
        _sandbox_id = sandbox_id
        _writable_root = cwd
        return
    _readonly_id = sandbox_id
    _readonly_root = cwd


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
