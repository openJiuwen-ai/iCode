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
outside directory is only the process cwd. A mount of the filesystem root
``/`` raises ``SandboxError`` before any sandbox is created: the sandbox
builds its own directories at its root, which a bind of the host root
would make host directories. A workspace root of ``/`` is refused before
the bundled server is started.

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
``.docker``, ``.netrc``, and ``.chrys``.
``host_path`` and ``sandbox_path`` are that path under the mount, not another
spelling of its real path. A symlink is not followed. A path whose real
location falls outside the mount is not bound; the comparison is
``os.path.realpath`` components, so ``work-extra`` is not inside ``work``.
``.config`` as a whole stays writable, and so does a project ``.git/hooks``.
The legacy single-cwd writable create uses the same covers.

The iCode config directory (``get_platform().config_dir``) is also covered
when an ancestor is a writable mount. When it exists, its realpath is inside
the mount, and it is not reached through a symlink that leaves the mount, it
is appended as a read-only bind of that path. This covers
``/home/user/.chrys`` when the writable mount is ``/home``.

Hidden directories
------------------
Every create, writable or read-only, also appends
``filesystem_policy.hidden_host_paths``: the real paths of the iCode config
directory and the session store. Wherever a bind shows one of them, the
server mounts an empty read-only tmpfs over it, so a mount of the account
home or one of its parents cannot read settings, keys or transcripts. The
server hides its own home the same way, so this client adds no cover for
it. A directory stays visible only below a mount that starts strictly
inside it. A command cwd that the server would hide raises
``SandboxError`` before a sandbox is created or reused. A server lists
``hidden_host_paths`` in the ``features`` of its ``/health`` answer when it
honours the field. A configured ``sandbox.url`` that does not raises
``SandboxError`` when a workspace mount or the command cwd contains one of
those directories; the bundled server always honours it.

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

The cache belongs to one server: a configured ``sandbox_url``, or the
bundled server. A command for another server parks the current entries
and uses that server's own, so a switch to another URL and back reuses the
sandboxes the first server still holds. A command with a configured URL
also retires the bundled server this process started; it stops once no
command leases it. When the last runtime in the process releases, every
sandbox still cached for a configured URL is deleted on that service
(``release_external_sandboxes``), so restarting iCode does not leave them
behind.

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
completed exec do not change the cache.

Create
------
Each create names its sandbox: the id is generated here and sent with the
POST, so it is known before the server answers. A cancel, or any failure
before the sandbox is ``ready`` (an error response, a sandbox that did not
start), DELETEs that id before the call ends; the server may have created
it after the client stopped waiting, and a DELETE of an id it never created
is a harmless 404. The delete is shielded, so a second cancellation does
not skip it. A 409 means the id names a sandbox this call did not create,
so it is not deleted. An id already cached or counted in flight is not
deleted either. This path does not POST ``/stop``.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import posixpath
import re
import secrets
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath
from typing import TYPE_CHECKING, Any

from chrys.foundation.util.once_close import finish_close
from chrys.service.sandbox.types import SandboxSettings

if TYPE_CHECKING:
    import httpx

_HTTP_TIMEOUT_SLACK_SECONDS = 15.0
_STOP_TIMEOUT_SECONDS = 2.0
# Process exit waits at most this long for configured services to delete their sandboxes.
_EXIT_DELETE_SECONDS = 5.0
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
_HIDDEN_WORKDIR = (
    "Sandbox command working directory is inside the iCode config or session directory, which the sandbox hides."
)
_ROOT_MOUNT = "The sandbox cannot mount the filesystem root (/). Use a project directory as the workspace instead."
_HIDDEN_HOST_PATHS_FEATURE = "hidden_host_paths"
_SERVER_CANNOT_HIDE = (
    "The sandbox service at sandbox.url cannot hide the iCode config and session directories, and this "
    "workspace contains one of them. Update the sandbox service, or use a workspace that does not contain them."
)
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
# The slots above belong to the server ``_server_key`` names. A command for
# another server parks them in ``_parked`` under that key and takes its own
# server's slots, so switching ``sandbox.url`` away and back reuses the
# sandboxes the first server still holds. ``_in_flight`` and ``_retiring``
# are shared: ids are minted by this client and never repeat.
_sandbox_id: str | None = None
_writable_root: str | None = None
_writable_by_root: dict[str, str] = {}
_writable_mounts: dict[str, tuple[str, ...]] = {}
_readonly_id: str | None = None
_readonly_root: str | None = None
_readonly_physical: bool = False
_in_flight: dict[str, int] = {}
_retiring: set[str] = set()
_server_key: str | None = None
_parked: dict[str, _ServerCache] = {}
# Each configured ``sandbox_url`` a command used, with the token last sent there.
_external_servers: dict[str, str] = {}
_lock: asyncio.Lock | None = None
# Every bundled server shares one key: a sandbox on a replaced one answers 404 and is recreated.
_BUNDLED_SERVER_KEY = ""


@dataclass(slots=True)
class _ServerCache:
    """The cache slots of one server while commands use another."""

    sandbox_id: str | None = None
    writable_root: str | None = None
    writable_by_root: dict[str, str] = field(default_factory=dict)
    writable_mounts: dict[str, tuple[str, ...]] = field(default_factory=dict)
    readonly_id: str | None = None
    readonly_root: str | None = None
    readonly_physical: bool = False

    def holds(self, sandbox_id: str) -> bool:
        return sandbox_id in (self.sandbox_id, self.readonly_id) or sandbox_id in self.writable_by_root.values()


def _use_server_cache(key: str) -> None:
    """Make ``key``'s slots current, parking the current server's. Called with the lock held; no await."""
    global _server_key, _sandbox_id, _writable_root, _readonly_id, _readonly_root, _readonly_physical
    if key == _server_key:
        return
    if _server_key is not None:
        _parked[_server_key] = _ServerCache(
            sandbox_id=_sandbox_id,
            writable_root=_writable_root,
            writable_by_root=dict(_writable_by_root),
            writable_mounts=dict(_writable_mounts),
            readonly_id=_readonly_id,
            readonly_root=_readonly_root,
            readonly_physical=_readonly_physical,
        )
        restored = _parked.pop(key, None) or _ServerCache()
        _sandbox_id = restored.sandbox_id
        _writable_root = restored.writable_root
        _writable_by_root.clear()
        _writable_by_root.update(restored.writable_by_root)
        _writable_mounts.clear()
        _writable_mounts.update(restored.writable_mounts)
        _readonly_id = restored.readonly_id
        _readonly_root = restored.readonly_root
        _readonly_physical = restored.readonly_physical
    _server_key = key


async def release_external_sandboxes() -> None:
    """DELETE every sandbox this process keeps on a configured ``sandbox_url``.

    The last runtime to release calls this at exit. No later command can reuse
    those sandboxes, and a service with idle reaping off would keep each one
    for good. The bundled server is stopped instead, which removes its own.
    The whole release is bounded by ``_EXIT_DELETE_SECONDS``; a service that
    does not answer keeps its sandboxes, and nothing here fails the exit.
    """
    servers = dict(_external_servers)
    _external_servers.clear()
    owned = {url: ids for url in servers if (ids := _take_server_ids(url))}
    if not owned:
        return
    from chrys.service.sandbox.client import connect

    async def delete_on(url: str, ids: tuple[str, ...]) -> None:
        async with connect(url, servers[url], _STOP_TIMEOUT_SECONDS, trust_env=True) as client:
            for sandbox_id in ids:
                await _delete_quietly(client, sandbox_id)

    async def delete_all() -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(_EXIT_DELETE_SECONDS):
                await asyncio.gather(*(delete_on(url, ids) for url, ids in owned.items()), return_exceptions=True)

    await finish_close(asyncio.create_task(delete_all()))


def _take_server_ids(key: str) -> tuple[str, ...]:
    """Empty ``key``'s cache slots and return the sandbox ids they held. No await."""
    global _sandbox_id, _writable_root
    if key == _server_key:
        held = [_sandbox_id, _readonly_id, *_writable_by_root.values()]
        _sandbox_id = None
        _writable_root = None
        _writable_by_root.clear()
        _writable_mounts.clear()
        _drop_readonly()
    else:
        parked = _parked.pop(key, None)
        if parked is None:
            return ()
        held = [parked.sandbox_id, parked.readonly_id, *parked.writable_by_root.values()]
    return tuple(dict.fromkeys(sandbox_id for sandbox_id in held if sandbox_id is not None))


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
    if workspace_root is not None:
        _reject_root_mounts(_workspace_mounts(workspace_root, workspace_dirs))

    # Imported here so a non-Linux call does not touch the client, and so this
    # module can define SandboxError before the client imports it back.
    from chrys.service.sandbox.runtime_hold import note_external_server_used, retire_bundled_server
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
        _external_servers[settings.sandbox_url] = settings.sandbox_token
        note_external_server_used()
        await retire_bundled_server()
        return await run(settings.sandbox_url, settings.sandbox_token, trust_env=True, server_key=settings.sandbox_url)
    # Held until the command is done: a server replaced by a network change
    # is stopped only after the last command on it.
    async with leased_server(settings.sandbox_network) as (base_url, token):
        return await run(base_url, token, trust_env=False, server_key=_BUNDLED_SERVER_KEY)


async def _run_on_server(
    base_url: str,
    token: str,
    *,
    trust_env: bool,
    server_key: str,
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
        hides = await _require_health(client)
        if not hides and server_key != _BUNDLED_SERVER_KEY:
            _reject_unhidden_private_dirs(cwd, workspace_root, workspace_dirs)
        sandbox_id, workdir, remaining = await _sandbox_for_command(
            client,
            cwd,
            timeout_seconds,
            server_key=server_key,
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


async def _require_health(client: httpx.AsyncClient) -> bool:
    """Raise unless the server is healthy; return whether it hides ``hidden_host_paths``."""
    payload = _json_object(await client.get("/health"))
    if payload.get("status") != "ok":
        raise SandboxError("Sandbox server health is not ok.")
    features = payload.get("features")
    return isinstance(features, list) and _HIDDEN_HOST_PATHS_FEATURE in features


def _reject_unhidden_private_dirs(cwd: str, workspace_root: str | None, workspace_dirs: Sequence[str]) -> None:
    """Raise when a server that cannot hide directories would mount the config or session directory.

    The mounts considered are the workspace mounts, or the cwd for the
    legacy slot, and the cwd itself.
    """
    physical_cwd = _physical_dir(cwd)
    mounts = _workspace_mounts(workspace_root, workspace_dirs) if workspace_root is not None else ()
    for hidden in _hidden_host_paths():
        if any(_same_or_inside_physical(mount, hidden) for mount in (*mounts, physical_cwd)):
            raise SandboxError(_SERVER_CANNOT_HIDE)


async def _sandbox_for_command(
    client: httpx.AsyncClient,
    cwd: str,
    timeout_seconds: int,
    *,
    server_key: str,
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
        _use_server_cache(server_key)
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
    physical_cwd = _physical_dir(cwd)
    if _sandbox_id is None or _writable_root is None:
        _reject_hidden_cwd(physical_cwd, (physical_cwd,))
        return await _create_legacy_writable(client, cwd)
    if not _legacy_inside(cwd):
        _reject_unmounted_protected(physical_cwd)
    _reject_hidden_cwd(physical_cwd, (_physical_dir(_writable_root),))
    return await _reuse_legacy_writable(client)


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
    _reject_hidden_cwd(physical_cwd, mounts)
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
    """Create a sandbox under a fresh id and return its id once the phase is ``ready``.

    Every exit other than that return DELETEs each id this call may own
    (see "Create" in the module docstring). This does not POST ``/stop``.
    """
    requested = _new_sandbox_id()
    known = [requested]
    try:
        response = await _post_json(client, "/api/v1/sandboxes", {**body, "sandbox_id": requested})
        if response.status_code == 409:
            known.clear()
        created = _json_object(response)
        sandbox_id = _require_id(created)
        if sandbox_id not in known:
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
    except BaseException:
        for sandbox_id in known:
            await _delete_unowned(client, sandbox_id)
        raise


def _new_sandbox_id() -> str:
    """A fresh id in the server's custom-id alphabet (``[0-9a-z_-]``, at most 40)."""
    return f"icode-{secrets.token_hex(12)}"


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
    """Drop and retire a cancelled read-only id. The writable cache is not touched.

    A read-only slot only ever takes an id it creates, so an id no slot holds
    now never will: a cancelled writable command returns without waiting
    for the lock.
    """
    if not _holds_readonly(sandbox_id):
        return
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
    marked. A parked server's read-only slot is cleared the same way.
    """
    if _readonly_id == sandbox_id:
        _drop_readonly()
    else:
        parked = next((cache for cache in _parked.values() if cache.readonly_id == sandbox_id), None)
        if parked is None:
            return
        parked.readonly_id = None
        parked.readonly_root = None
        parked.readonly_physical = False
    if not _is_cached(sandbox_id):
        _retiring.add(sandbox_id)


def _holds_readonly(sandbox_id: str) -> bool:
    return _readonly_id == sandbox_id or any(cache.readonly_id == sandbox_id for cache in _parked.values())


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
    are left as the caller passed them. Either mode names the directories the
    server must hide wherever a bind would show them.
    """
    _reject_root_mounts(mounts)
    binds: list[dict[str, str]] = [{"host_path": path, "sandbox_path": path, "mode": mode} for path in mounts]
    if mode == _READ_WRITE:
        binds.extend(_sensitive_readonly_binds(mounts))
    return {
        "policy_mode": "append",
        "policy": {
            "filesystem_policy": {
                "bind_mounts": binds,
                "hidden_host_paths": list(_hidden_host_paths()),
            }
        },
    }


def _reject_root_mounts(mounts: Sequence[str]) -> None:
    """Raise when a mount is the filesystem root.

    The sandbox creates its own directory at its root after the binds.
    Under a bind of the host root that is a directory on the host: an
    ordinary account cannot create it, so the sandbox would never start,
    and an account that can would leave it on the host.
    """
    if any(posixpath.normpath(path).strip("/") == "" for path in mounts):
        raise SandboxError(_ROOT_MOUNT)


def _hidden_host_paths() -> tuple[str, ...]:
    """Real paths of the iCode config directory and the session store.

    The server mounts an empty read-only tmpfs wherever a bind shows one of
    them, so a mount of the account home or one of its parents cannot read
    settings, keys or transcripts. The bundled server's own home lives in the
    config directory. A configured server that does not honour the field is
    refused when a mount would show one of them (``_reject_unhidden_private_dirs``).
    """
    from chrys.foundation.config.settings import resolve_sessions_dir

    paths: list[str] = []
    for path in (_icode_config_dir(), resolve_sessions_dir(create=False)):
        physical = os.path.realpath(path)
        if not any(_same_or_inside_physical(kept, physical) for kept in paths):
            paths.append(physical)
    return tuple(paths)


def _sensitive_readonly_binds(mounts: Sequence[str]) -> list[dict[str, str]]:
    """Read-only covers for account paths that exist inside ``mounts``.

    Relative names are covered at the mount root. The real iCode config
    directory is covered as well when an ancestor of it is the mount, which
    keeps ``~/.chrys`` unwritable when the writable mount is the parent of the
    account home, even on a server that cannot hide it.
    """
    covers: list[dict[str, str]] = []
    seen: set[str] = set()
    for mount in mounts:
        physical_mount = os.path.realpath(mount)
        for parts in _SENSITIVE_RELATIVE_PARTS:
            _append_cover(covers, seen, _sensitive_cover_path(mount, physical_mount, parts))
        for absolute in _account_control_dirs():
            _append_cover(covers, seen, _account_control_cover(mount, physical_mount, absolute))
    return covers


def _append_cover(covers: list[dict[str, str]], seen: set[str], candidate: str | None) -> None:
    if candidate is None or candidate in seen:
        return
    seen.add(candidate)
    covers.append({"host_path": candidate, "sandbox_path": candidate, "mode": _READ_ONLY})


def _account_control_dirs() -> tuple[str, ...]:
    """The iCode config directory."""
    return (_icode_config_dir(),)


def _icode_config_dir() -> str:
    """iCode config directory, by its real path."""
    from chrys.foundation.platform import get_platform

    return str(get_platform().config_dir)


def _password_database_home() -> str:
    """``pwd`` home for this uid, or ``Path.home()`` when that is unavailable."""
    directory = ""
    if sys.platform != "win32":
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


def _reject_hidden_cwd(physical_cwd: str, physical_mounts: Sequence[str]) -> None:
    """Raise when the server would hide ``physical_cwd`` behind an empty tmpfs.

    A hidden directory is covered wherever a bind of it or of an ancestor
    shows it. It stays visible only below a mount that starts strictly inside
    it, so a cwd anywhere else under it would not exist in the sandbox.
    """
    for hidden in _hidden_host_paths():
        if not _same_or_inside_physical(hidden, physical_cwd):
            continue
        if not any(
            mount != hidden
            and _same_or_inside_physical(hidden, mount)
            and _same_or_inside_physical(mount, physical_cwd)
            for mount in physical_mounts
        ):
            raise SandboxError(_HIDDEN_WORKDIR)


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
    """Whether any cache entry, current or parked, still points at ``sandbox_id``."""
    if sandbox_id in (_sandbox_id, _readonly_id) or sandbox_id in _writable_by_root.values():
        return True
    return any(parked.holds(sandbox_id) for parked in _parked.values())


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
