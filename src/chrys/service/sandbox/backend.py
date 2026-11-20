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
cwd is the command cwd resolved the same way (exec ``workdir``): only
physical paths are mounted, so a symlink spelling of the cwd may not exist
inside the sandbox. A cwd inside that root, or inside a ``workspace_dirs``
entry that is not already under it, reuses the writable sandbox. Those
extra directories are further read-write binds on the same sandbox. The
cache entry is the physical primary root together with the whole mount
set, so two workspaces do not share one mount, and a command whose mount
set differs from the cached one (a directory added or removed) gets a new
sandbox, which supersedes the cached one for that root. A cwd outside that
set does not add a bind and does not get a sandbox of its own. The command
reuses the writable sandbox built from the workspace mounts, and the
outside directory is only the process cwd. Binding ``/`` is not special:
the base policy remounts ``/`` read-only.

A physical cwd that is outside every workspace mount and lies under a
protected tree is rejected with ``SandboxError`` before a sandbox is
created. Protected trees are ``/run``, ``/tmp``, ``/var/run``, ``/var/tmp``,
``/root``, and the account home directory. Comparison uses
``os.path.realpath`` components, so a symlink into one of those trees is
protected too. A cwd inside a workspace mount stays on the writable sandbox
even when that mount itself is under a protected tree.

Paths are compared as components after ``os.path.realpath``, so a sibling
such as ``work-extra`` is not inside ``work``. Two symlink spellings of one
directory are one sandbox and one read-write mount of the physical path.

A writable mount stays read-write, including when that mount is the home
directory. The fragment also appends a read-only bind for each account path
that exists inside the mount and is not a symlink: ``.ssh``, ``.bashrc``,
``.bash_profile``, ``.profile``, ``.zshrc``, ``.zprofile``, ``.zshenv``,
``.zlogin``, ``.gitconfig``, ``.git-credentials``, ``.gnupg``, ``.pypirc``,
``.npmrc``, ``.wgetrc``, ``.curlrc``, ``.azure``, ``.config/gh``,
``.config/git``, ``.config/gcloud``, ``.config/glab``,
``.config/configstore``, ``.local/share/keyrings``, ``.aws``, ``.kube``,
``.docker``, ``.netrc``, ``.chrys``, and the sandbox engine home directory.
``host_path`` and ``sandbox_path`` are that path under the mount, not another
spelling of its real path. A symlink is not followed. A path whose real
location falls outside the mount is not bound; the comparison is
``os.path.realpath`` components, so ``work-extra`` is not inside ``work``.
``.config`` as a whole stays writable, and so does a project ``.git/hooks``.
The legacy single-cwd writable create uses the same covers.

The account control directories are also covered when an ancestor is a
writable mount. The iCode directory is ``Path.home() / ".chrys"``. The
sandbox engine home is the password-database home
(``pwd.getpwuid(os.geteuid()).pw_dir``, or ``Path.home()`` when that is
unavailable) joined with the engine home directory name. Each existing
directory whose realpath is inside the mount, and which is not reached
through a symlink that leaves the mount, is appended as a read-only bind of
that path. A missing directory is skipped. This covers ``/home/user/.chrys``
when the writable mount is ``/home``.

When ``workspace_root`` is omitted, the first command cwd remains the
writable root of a single legacy slot. That slot is separate from the
workspace-keyed cache. Its mounts are the command cwd as given, and so is
its exec ``workdir``. A later cwd does not bind a new host directory. A
protected outside cwd raises ``SandboxError``. Any other outside cwd reuses
the existing sandbox and passes that cwd as ``workdir``.

A 404 for a cached id drops that one entry and creates one replacement.
Any other HTTP error, and a second 404, still fail the call. Replacing a
cached sandbox (a read-only one for another root, a superseded workspace
mount set, or one that is not ready) deletes the replaced sandbox once no
command is executing in it: each exec counts as one in-flight use from the
moment its id is chosen under the lock until the exec returns, raises or
is cancelled, and the last use to end performs the delete. That delete is
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
A read-only id for the cancelled command is forgotten and retired like a
replaced one. That forget is shielded, so a second cancellation does not
skip it. The command's in-flight use ends as on any other exit; when its
sandbox was forgotten or already replaced and this was the last use, the
sandbox is deleted, and that delete is shielded the same way. Another exec
still running in that read-only sandbox keeps it until that exec ends.
A cancel before this call has started an exec, a ``SandboxError``, and a
completed exec do not change the cache. A cancel during create, after the
server has returned a sandbox id and before that id is cached or counted
in flight, DELETEs the id. The delete is shielded, so a second cancellation
does not skip it. A writable sandbox that was stored in the cache is not
deleted. This path does not POST ``/stop``. A cancel before any id is known
has nothing to delete.
"""

from __future__ import annotations

import asyncio
import functools
import os
import posixpath
import re
import time
from collections.abc import Sequence
from pathlib import Path, PurePath, PurePosixPath
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
_OUTSIDE_WORKDIR = "Sandbox command working directory is outside the workspace and was not mounted."
_UNENCODABLE_PATH = "Sandbox path could not be encoded for the sandbox request."
# Host trees that must not be bind-mounted merely because the process cwd
# is there. The account home is added at the call. Comparison is realpath
# components, so a symlink into one of these trees is protected too.
_PROTECTED_DIRECTORIES = ("/run", "/tmp", "/var/run", "/var/tmp", "/root")

# Process cache.
# ``_sandbox_id`` / ``_writable_root`` are the legacy slot used when the
# caller omits ``workspace_root``: the first command cwd is the writable root.
# ``_writable_by_root`` maps a physical workspace root to its read-write
# sandbox, and ``_writable_mounts`` that root to the mount set the sandbox
# was created with; the two change together. Cancel does not drop either
# writable entry.
# ``_readonly_id`` mounts ``_readonly_root`` read-only and never replaces a
# writable entry.
# ``_in_flight`` counts the execs using each id. A replaced id, or a
# read-only id forgotten on cancel, waits in ``_retiring`` until its count
# reaches zero, then is deleted.
_sandbox_id: str | None = None
_writable_root: str | None = None
_writable_by_root: dict[str, str] = {}
_writable_mounts: dict[str, tuple[str, ...]] = {}
_readonly_id: str | None = None
_readonly_root: str | None = None
_readonly_physical: bool = False
_in_flight: dict[str, int] = {}
_retiring: set[str] = set()
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
    """One completed remote command. ``exit_code`` may be non-zero.

    ``timed_out`` is true only when the exec JSON says ``timed_out: true``.
    An exit code of 124 by itself is not a timeout.
    """

    __slots__ = ("exit_code", "stderr", "stdout", "timed_out")

    def __init__(self, exit_code: int, stdout: str, stderr: str, timed_out: bool = False) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


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
    is sent. The bundled server runs with ``sandbox_network`` and is leased
    until the command is done. Waiting for the cache lock consumes ``timeout_seconds``.
    Cancellation during exec does not stop the sandbox, and then propagates.
    The writable cache is kept across that cancellation; a read-only
    sandbox it ran in is deleted once no other exec is using it.

    When ``workspace_root`` is provided, the read-write mount is that
    session workspace (plus ``workspace_dirs`` outside it), not ``cwd``.
    When it is omitted, the first command cwd remains the writable root.
    """
    if not linux:
        raise SandboxError("Sandbox execution is supported only on Linux.")

    # Imported here so a non-Linux call does not touch the client, and so this
    # module can define SandboxError before the client imports it back.
    from chrys.service.sandbox.supervisor import leased_server

    run = functools.partial(
        _run_on_server,
        argv=argv,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        workspace_root=workspace_root,
        workspace_dirs=workspace_dirs,
    )
    if settings.sandbox_url:
        return await run(settings.sandbox_url, settings.sandbox_token, trust_env=True)
    # Held until the command is done: a server replaced by a network change
    # is stopped only after the last command on it.
    async with leased_server(settings.sandbox_network) as (base_url, token):
        return await run(base_url, token, trust_env=False)


async def _run_on_server(
    base_url: str,
    token: str,
    *,
    trust_env: bool,
    argv: Sequence[str],
    cwd: str,
    timeout_seconds: int,
    workspace_root: str | None,
    workspace_dirs: Sequence[str],
) -> SandboxExec:
    from chrys.service.sandbox.client import connect

    # Opened before the lock wait. Health and create use this budget; the exec
    # request replaces it with the time left after the wait. trust_env stays on
    # for a configured URL and is off for the bundled server this process started.
    http_timeout = float(timeout_seconds) + _HTTP_TIMEOUT_SLACK_SECONDS
    async with connect(base_url, token, http_timeout, trust_env=trust_env) as client:
        await _require_health(client)
        sandbox_id, workdir, remaining = await _sandbox_for_command(
            client,
            cwd,
            timeout_seconds,
            workspace_root=workspace_root,
            workspace_dirs=workspace_dirs,
        )
        # The in-flight use taken under the lock is released on every exit,
        # with no await before the release.
        try:
            try:
                return await _exec(client, sandbox_id, argv, workdir, timeout_seconds, remaining)
            except asyncio.CancelledError:
                await _stop_on_cancel(sandbox_id)
                raise
        finally:
            if _release_in_flight(sandbox_id):
                await _delete_sandbox(client, sandbox_id)


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
) -> tuple[str, str, float]:
    """Return a ready sandbox id, its exec workdir, and the budget left after the lock wait.

    The lock is held only while the cache is read or updated and while a
    sandbox is created. The returned id carries one in-flight use, taken
    before the lock is released; the caller execs, then releases it.
    """
    remaining = await _acquire_cache_lock(float(timeout_seconds))
    try:
        sandbox_id, workdir = await _ready_sandbox(
            client,
            cwd,
            workspace_root=workspace_root,
            workspace_dirs=workspace_dirs,
        )
        _in_flight[sandbox_id] = _in_flight.get(sandbox_id, 0) + 1
        return sandbox_id, workdir, remaining
    finally:
        _gate().release()


async def _ready_sandbox(
    client: httpx.AsyncClient,
    cwd: str,
    *,
    workspace_root: str | None,
    workspace_dirs: Sequence[str],
) -> tuple[str, str]:
    """Return a ready sandbox id and the workdir that exists inside it."""
    if workspace_root is None:
        return await _ready_legacy_sandbox(client, cwd), cwd
    return await _ready_workspace_sandbox(client, cwd, workspace_root, workspace_dirs)


async def _ready_legacy_sandbox(client: httpx.AsyncClient, cwd: str) -> str:
    """Writable root is the first command cwd. Used when no workspace is passed.

    A later cwd does not bind a new host directory. One under a protected
    tree raises. Any other later cwd reuses the sandbox created for the
    first cwd.
    """
    if _legacy_inside(cwd):
        return await _reuse_legacy_writable(client)
    if _sandbox_id is not None and _writable_root is not None:
        _reject_unmounted_protected(_physical_dir(cwd))
        return await _reuse_legacy_writable(client)
    return await _create_legacy_writable(client, cwd)


def _legacy_inside(cwd: str) -> bool:
    """Whether ``cwd`` is the legacy writable root or a directory inside it."""
    if _sandbox_id is None or _writable_root is None:
        return False
    if _same_or_inside(_writable_root, cwd):
        return True
    return _same_or_inside_physical(_physical_dir(_writable_root), _physical_dir(cwd))


async def _reuse_legacy_writable(client: httpx.AsyncClient) -> str:
    """Return the legacy writable sandbox, recreating it at the same root.

    A 404 drops the slot and creates one replacement of the original root.
    The command cwd is not mounted.
    """
    root = _writable_root
    current = _sandbox_id
    if root is None or current is None:
        raise SandboxError(_OUTSIDE_WORKDIR)
    state = await _cached_phase(client, current)
    if state == _READY:
        return current
    if state == _CACHE_MISSING:
        _drop_cache(_READ_WRITE)
    sandbox_id = await _create_ready(client, _create_body(root, _READ_WRITE))
    _store_cache(sandbox_id, root)
    return sandbox_id


async def _create_legacy_writable(client: httpx.AsyncClient, cwd: str) -> str:
    """Create the legacy writable sandbox. ``cwd`` is its only mount."""
    sandbox_id = await _create_ready(client, _create_body(cwd, _READ_WRITE))
    _store_cache(sandbox_id, cwd)
    return sandbox_id


async def _ready_workspace_sandbox(
    client: httpx.AsyncClient,
    cwd: str,
    workspace_root: str,
    workspace_dirs: Sequence[str],
) -> tuple[str, str]:
    """Read-write mounts are the workspace, never the command cwd.

    The workdir is the physical cwd. A physical cwd outside every workspace
    mount does not add a bind. One that also lies under a protected tree
    raises instead of running.
    """
    mounts = _workspace_mounts(workspace_root, workspace_dirs)
    physical_cwd = _physical_dir(cwd)
    if not _cwd_uses_writable(physical_cwd, mounts):
        _reject_unmounted_protected(physical_cwd)
    return await _ready_keyed_writable(client, mounts), physical_cwd


async def _ready_keyed_writable(client: httpx.AsyncClient, mounts: tuple[str, ...]) -> str:
    """Reuse or create the read-write sandbox for the primary root ``mounts[0]``.

    The cached id is reused only when it was created with exactly ``mounts``.
    A different set creates a sandbox that supersedes it for this root; the
    superseded one is retired. A 404 drops only this root's id. Other
    workspace roots stay.
    """
    primary = mounts[0]
    cached = _writable_by_root.get(primary)
    if cached is not None and _writable_mounts.get(primary) == mounts:
        state = await _cached_phase(client, cached)
        if state == _READY:
            return cached
        if state == _CACHE_MISSING:
            _drop_writable(primary)
    sandbox_id = await _create_ready(client, _bind_body(mounts, _READ_WRITE))
    previous = _writable_by_root.get(primary)
    _store_writable(primary, mounts, sandbox_id)
    if previous is not None and previous != sandbox_id:
        await _retire(client, previous)
    return sandbox_id


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
    """Create a sandbox and return its id once the phase is ``ready``.

    A cancellation after an id is known, and before the caller stores it,
    DELETEs that id when nothing has cached it or is executing in it. The
    delete is shielded. A cancel before the create response has no id to
    delete. This does not POST ``/stop``.
    """
    known: list[str] = []
    try:
        created = _json_object(await _post_json(client, "/api/v1/sandboxes", body))
        sandbox_id = _require_id(created)
        known.append(sandbox_id)
        if created.get("phase") != _READY:
            started = _json_object(await client.post(f"{_sandbox_path(sandbox_id)}/start"))
            started_id = _require_id(started)
            if started_id not in known:
                known.append(started_id)
            sandbox_id = started_id
            if started.get("phase") != _READY:
                raise _not_ready(started)
        return sandbox_id
    except asyncio.CancelledError:
        for sandbox_id in known:
            await _delete_unowned(client, sandbox_id)
        raise


async def _delete_unowned(client: httpx.AsyncClient, sandbox_id: str) -> None:
    """DELETE ``sandbox_id`` when no cache entry and no exec holds it.

    A writable sandbox already stored in the cache is left in place. The
    delete itself is shielded by ``_delete_sandbox``.
    """
    if _is_cached(sandbox_id) or _in_flight.get(sandbox_id, 0) > 0:
        return
    await _delete_sandbox(client, sandbox_id)


async def _post_json(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, Any],
    **kwargs: Any,
) -> httpx.Response:
    """POST JSON. A path that cannot be encoded becomes ``SandboxError``.

    httpx encodes JSON as UTF-8. A POSIX path carrying surrogate-escaped
    bytes cannot be encoded, and the error text can include that path. The
    replacement message does not.
    """
    try:
        return await client.post(url, json=body, **kwargs)
    except UnicodeEncodeError:
        raise SandboxError(_UNENCODABLE_PATH) from None


async def _retire(client: httpx.AsyncClient, sandbox_id: str) -> None:
    """Delete a sandbox the cache no longer points at, once nothing runs in it.

    Called under the cache lock after the replacement is stored. An id with
    an exec still in flight is only marked; the last of those execs deletes
    it (``_release_in_flight``). An id another cache entry still holds is
    not deleted.
    """
    if _is_cached(sandbox_id):
        return
    if _in_flight.get(sandbox_id, 0) > 0:
        _retiring.add(sandbox_id)
        return
    await _delete_sandbox(client, sandbox_id)


def _release_in_flight(sandbox_id: str) -> bool:
    """Drop one in-flight use. True when the caller must now delete the sandbox.

    No await, so a cancellation cannot skip the release.
    """
    count = _in_flight.get(sandbox_id, 0) - 1
    if count > 0:
        _in_flight[sandbox_id] = count
        return False
    _in_flight.pop(sandbox_id, None)
    if sandbox_id not in _retiring:
        return False
    _retiring.discard(sandbox_id)
    return not _is_cached(sandbox_id)


async def _delete_sandbox(client: httpx.AsyncClient, sandbox_id: str) -> None:
    """DELETE a retired sandbox. A cancelled caller still waits for the delete.

    This is not cancellation. Cancellation does not POST ``/stop`` and does
    not delete a writable sandbox it ran in. This function DELETEs
    ``/api/v1/sandboxes/{id}`` and runs only for an id no cache entry holds
    and no exec is using. A failed delete does not fail the command.
    """
    await finish_close(asyncio.create_task(_delete_quietly(client, sandbox_id)))


async def _delete_quietly(client: httpx.AsyncClient, sandbox_id: str) -> None:
    try:
        await client.delete(_sandbox_path(sandbox_id), timeout=_STOP_TIMEOUT_SECONDS)
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
    workdir: str,
    timeout_seconds: int,
    remaining: float,
) -> SandboxExec:
    command_timeout = _exec_timeout_seconds(timeout_seconds, remaining)
    payload = _json_object(
        await _post_json(
            client,
            f"{_sandbox_path(sandbox_id)}/exec",
            {
                "command": list(argv),
                "workdir": workdir,
                "timeout_seconds": command_timeout,
            },
            timeout=remaining + _HTTP_TIMEOUT_SLACK_SECONDS,
        )
    )
    exit_code = payload.get("exit_code")
    if type(exit_code) is not int:
        raise SandboxError("Sandbox exec response did not include an exit code.")
    # Only a JSON true is a timeout. Exit code 124 alone is a normal exit.
    return SandboxExec(
        exit_code,
        _stream_text(payload, "stdout"),
        _stream_text(payload, "stderr"),
        timed_out=payload.get("timed_out") is True,
    )


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
    is forgotten and retired.
    """
    task = asyncio.create_task(_stop_and_forget(sandbox_id))
    try:
        await finish_close(task)
    except asyncio.CancelledError:
        raise
    except Exception:
        return


async def _stop_and_forget(sandbox_id: str) -> None:
    """Forget and retire a cancelled read-only id. The writable cache stays.

    Dropping the writable root would make the next command's cwd the new
    writable root. This function does not POST ``/stop``.
    """
    await _forget_cached_id(sandbox_id)


async def _forget_cached_id(sandbox_id: str) -> None:
    """Drop and retire a cancelled read-only id. The writable cache is not touched."""
    lock = _gate()
    await lock.acquire()
    try:
        _clear_cached_id(sandbox_id)
    finally:
        lock.release()


def _clear_cached_id(sandbox_id: str) -> None:
    """Forget a cancelled read-only sandbox id and mark it for deletion.

    Writable entries are left in place. Clearing the writable root would
    make the next command's cwd the new writable root. The forgotten id is
    retired like a replaced one: the cancelled command still holds an
    in-flight use, so the last exec using the id deletes it
    (``_release_in_flight``). An id another cache entry still holds is not
    marked.
    """
    if _readonly_id != sandbox_id:
        return
    _drop_readonly()
    if not _is_cached(sandbox_id):
        _retiring.add(sandbox_id)


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


# Account paths covered read-only when they exist inside a writable mount.
# The ``.config`` directory itself stays writable; only the named children
# are covered. Project ``.git/hooks`` is ordinary workspace write and is
# not listed.
_SENSITIVE_RELATIVE_PARTS: tuple[tuple[str, ...], ...] = (
    (".ssh",),
    (".bashrc",),
    (".bash_profile",),
    (".profile",),
    (".zshrc",),
    (".zprofile",),
    (".zshenv",),
    (".zlogin",),
    (".gitconfig",),
    (".git-credentials",),
    (".gnupg",),
    (".pypirc",),
    (".npmrc",),
    (".wgetrc",),
    (".curlrc",),
    (".azure",),
    (".config", "gh"),
    (".config", "git"),
    (".config", "gcloud"),
    (".config", "glab"),
    (".config", "configstore"),
    (".local", "share", "keyrings"),
    (".aws",),
    (".kube",),
    (".docker",),
    (".netrc",),
    (".chrys",),
)


def _bind_body(mounts: Sequence[str], mode: str) -> dict[str, Any]:
    """Append bind mounts. ``host_path`` and ``sandbox_path`` are the same path.

    Writable mounts stay read-write. Account paths that exist inside them are
    appended afterwards as read-only binds of that same path. Read-only mounts
    are left as the caller passed them.
    """
    binds: list[dict[str, str]] = [{"host_path": path, "sandbox_path": path, "mode": mode} for path in mounts]
    if mode == _READ_WRITE:
        binds.extend(_sensitive_readonly_binds(mounts))
    return {
        "policy_mode": "append",
        "policy": {
            "filesystem_policy": {
                "bind_mounts": binds,
            }
        },
    }


def _sensitive_readonly_binds(mounts: Sequence[str]) -> list[dict[str, str]]:
    """Read-only covers for account paths that exist inside ``mounts``.

    Relative names are covered at the mount root. The real iCode config
    directory and the sandbox engine home are covered as well when an
    ancestor of those directories is the mount, which is what protects
    ``~/.chrys`` when the writable mount is the parent of the account home.
    """
    covers: list[dict[str, str]] = []
    seen: set[str] = set()
    for mount in mounts:
        physical_mount = os.path.realpath(mount)
        for parts in _sensitive_relative_parts():
            _append_cover(covers, seen, _sensitive_cover_path(mount, physical_mount, parts))
        for absolute in _account_control_dirs():
            _append_cover(covers, seen, _account_control_cover(mount, physical_mount, absolute))
    return covers


def _sensitive_relative_parts() -> tuple[tuple[str, ...], ...]:
    """Account paths, including the sandbox engine home directory name."""
    from chrys.service.sandbox.supervisor import sandbox_engine_home_dirname

    return (*_SENSITIVE_RELATIVE_PARTS, (sandbox_engine_home_dirname(),))


def _append_cover(covers: list[dict[str, str]], seen: set[str], candidate: str | None) -> None:
    if candidate is None or candidate in seen:
        return
    seen.add(candidate)
    covers.append({"host_path": candidate, "sandbox_path": candidate, "mode": _READ_ONLY})


def _account_control_dirs() -> tuple[str, str]:
    """iCode config directory and the sandbox engine home directory."""
    return (_icode_config_dir(), _sandbox_engine_home_dir())


def _icode_config_dir() -> str:
    """Linux iCode config directory: ``Path.home() / ".chrys"``."""
    return str(Path.home() / ".chrys")


def _sandbox_engine_home_dir() -> str:
    """Sandbox engine home under the password-database home."""
    from chrys.service.sandbox.supervisor import sandbox_engine_home_dirname

    return os.path.join(_password_database_home(), sandbox_engine_home_dirname())


def _password_database_home() -> str:
    """``pwd`` home for this uid, or ``Path.home()`` when that is unavailable."""
    try:
        import pwd

        directory = pwd.getpwuid(os.geteuid()).pw_dir
    except ImportError, KeyError, AttributeError, OSError:
        directory = ""
    if isinstance(directory, str) and directory:
        return directory
    return str(Path.home())


def _account_control_cover(mount: str, physical_mount: str, absolute: str) -> str | None:
    """Read-only bind of ``absolute`` when a writable ancestor mount contains it.

    A missing directory is skipped. A symlink component is not followed, so a
    target outside the mount is not bound. The bind path is ``absolute``
    itself when that spelling is not the mount's own relative path.
    """
    if not absolute or not os.path.lexists(absolute):
        return None
    if not _same_or_inside_physical(physical_mount, os.path.realpath(absolute)):
        return None
    relative = _relative_parts(mount, absolute)
    if relative is not None:
        return _sensitive_cover_path(mount, physical_mount, relative)
    if _symlink_inside_mount(absolute, physical_mount):
        return None
    return absolute


def _relative_parts(root: str, path: str) -> tuple[str, ...] | None:
    """Components of ``path`` strictly inside ``root``, or None when it is not."""
    root_parts = PurePath(root).parts
    path_parts = PurePath(path).parts
    if len(path_parts) <= len(root_parts):
        return None
    if path_parts[: len(root_parts)] != root_parts:
        return None
    return path_parts[len(root_parts) :]


def _symlink_inside_mount(absolute: str, physical_mount: str) -> bool:
    """Whether a symlink on ``absolute`` sits inside ``physical_mount``.

    The mount boundary itself is not treated as a symlink to follow. A symlink
    anywhere inside the mount is not followed.
    """
    current = absolute
    mount_length = len(PurePath(physical_mount).parts)
    while True:
        if os.path.islink(current):
            return True
        parent = os.path.dirname(current)
        if parent == current or not os.path.lexists(parent):
            return False
        parent_real = os.path.realpath(parent)
        if not _same_or_inside_physical(physical_mount, parent_real):
            return False
        if len(PurePath(parent_real).parts) <= mount_length:
            return False
        current = parent


def _sensitive_cover_path(mount: str, physical_mount: str, parts: tuple[str, ...]) -> str | None:
    """Path under ``mount`` to bind read-only, or None when it must stay uncovered.

    ``physical_mount`` is ``os.path.realpath`` of ``mount``. A missing path is
    skipped. A symlink component is skipped without following it, so the
    outside target is not bound. A real file or directory is bound at the
    path under ``mount``, not at a different realpath spelling. That real
    location must still be the mount or a directory inside it.
    """
    candidate = mount
    for part in parts:
        candidate = os.path.join(candidate, part)
        if os.path.islink(candidate):
            return None
    if not os.path.lexists(candidate):
        return None
    if not _same_or_inside_physical(physical_mount, os.path.realpath(candidate)):
        return None
    return candidate


def _create_body(cwd: str, mode: str) -> dict[str, Any]:
    """Legacy single mount. ``cwd`` is the command cwd, not a workspace root.

    Writable mode appends the same read-only account covers as a workspace mount.
    """
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


def _reject_unmounted_protected(physical_cwd: str) -> None:
    """Raise when ``physical_cwd`` is outside the mounts and under a protected tree.

    The caller has already decided the path is not inside a workspace mount
    or the legacy writable root. Ordinary directories such as ``/usr`` are
    not protected and do not raise.
    """
    if _path_is_protected(physical_cwd):
        raise SandboxError(_OUTSIDE_WORKDIR)


def _path_is_protected(physical_path: str) -> bool:
    """Whether ``physical_path`` is a protected tree or a directory inside one.

    ``physical_path`` is already an ``os.path.realpath`` result. The protected
    roots are resolved the same way, so a symlink such as ``/var/run`` →
    ``/run`` protects both spellings.
    """
    return any(_same_or_inside_physical(root, physical_path) for root in _protected_roots())


def _protected_roots() -> tuple[str, ...]:
    """Real paths of the protected trees, including the account home."""
    roots: list[str] = []
    seen: set[str] = set()
    for path in (*_PROTECTED_DIRECTORIES, *_account_homes()):
        physical = _physical_dir(path)
        if physical in seen:
            continue
        seen.add(physical)
        roots.append(physical)
    return tuple(roots)


def _account_homes() -> tuple[str, ...]:
    """Password-database home and ``Path.home()``, without duplicates."""
    homes: list[str] = []
    password_home = _password_database_home()
    if password_home:
        homes.append(password_home)
    login_home = str(Path.home())
    if login_home and login_home not in homes:
        homes.append(login_home)
    return tuple(homes)


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


def _drop_writable(primary: str) -> None:
    _writable_by_root.pop(primary, None)
    _writable_mounts.pop(primary, None)


def _store_cache(sandbox_id: str, cwd: str) -> None:
    """Legacy writable slot. The mounted root is this command cwd."""
    global _sandbox_id, _writable_root
    _sandbox_id = sandbox_id
    _writable_root = cwd
    _retiring.discard(sandbox_id)


def _store_writable(primary: str, mounts: tuple[str, ...], sandbox_id: str) -> None:
    """Workspace writable entry: ``sandbox_id`` was created with exactly ``mounts``."""
    _writable_by_root[primary] = sandbox_id
    _writable_mounts[primary] = mounts
    _retiring.discard(sandbox_id)


def _is_cached(sandbox_id: str) -> bool:
    """Whether any cache entry still points at ``sandbox_id``."""
    return sandbox_id in (_sandbox_id, _readonly_id) or sandbox_id in _writable_by_root.values()


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
