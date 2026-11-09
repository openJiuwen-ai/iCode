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
The create body is ``policy_mode`` ``append`` plus one ``rw`` bind mount
whose ``host_path`` and ``sandbox_path`` are both the command cwd.

One sandbox id is cached on this module for the process. The next call
reuses it while ``GET`` still reports ``phase=ready``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from chrys.service.sandbox.types import SandboxSettings

if TYPE_CHECKING:
    import httpx

_HTTP_TIMEOUT_SLACK_SECONDS = 15.0
_READY = "ready"

_sandbox_id: str | None = None
_lock: asyncio.Lock | None = None


class SandboxError(Exception):
    """The sandbox call failed closed.

    ``str(self)`` is written by chrys and is what the shell tool passes to
    ``tool_error``. It must not carry a token, URL userinfo, or the text of
    an HTTP or OS exception.
    """

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class SandboxExec:
    """One completed remote command. ``exit_code`` may be non-zero."""

    __slots__ = ("exit_code", "stderr", "stdout")

    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


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
    ``sandbox_url`` asks the supervisor to start the bundled server. A
    non-empty URL is used as-is and does not start a process.
    """
    if not linux:
        raise SandboxError("Sandbox execution is supported only on Linux.")

    # Imported here so a non-Linux call does not touch the client, and so this
    # module can define SandboxError before the client imports it back.
    from chrys.service.sandbox.client import connect
    from chrys.service.sandbox.supervisor import ensure_started

    token = settings.sandbox_token
    configured = settings.sandbox_url
    http_timeout = float(timeout_seconds) + _HTTP_TIMEOUT_SLACK_SECONDS
    async with _gate():
        base_url = configured or await ensure_started(token)
        async with connect(base_url, token, http_timeout) as client:
            await _require_health(client)
            sandbox_id = await _ready_sandbox(client, cwd)
            return await _exec(client, sandbox_id, argv, cwd, timeout_seconds)


async def _require_health(client: httpx.AsyncClient) -> None:
    payload = _json_object(await client.get("/health"))
    if payload.get("status") != "ok":
        raise SandboxError("Sandbox server health is not ok.")


async def _ready_sandbox(client: httpx.AsyncClient, cwd: str) -> str:
    global _sandbox_id
    current = _sandbox_id
    if current is not None:
        payload = _json_object(await client.get(_sandbox_path(current)))
        if payload.get("phase") == _READY:
            return current
    created = _json_object(await client.post("/api/v1/sandboxes", json=_create_body(cwd)))
    sandbox_id = _require_id(created)
    if created.get("phase") != _READY:
        started = _json_object(await client.post(f"{_sandbox_path(sandbox_id)}/start"))
        sandbox_id = _require_id(started)
        if started.get("phase") != _READY:
            raise SandboxError("Sandbox did not become ready.")
    _sandbox_id = sandbox_id
    return sandbox_id


async def _exec(
    client: httpx.AsyncClient,
    sandbox_id: str,
    argv: Sequence[str],
    cwd: str,
    timeout_seconds: int,
) -> SandboxExec:
    payload = _json_object(
        await client.post(
            f"{_sandbox_path(sandbox_id)}/exec",
            json={
                "command": list(argv),
                "workdir": cwd,
                "timeout_seconds": timeout_seconds,
            },
        )
    )
    exit_code = payload.get("exit_code")
    if type(exit_code) is not int:
        raise SandboxError("Sandbox exec response did not include an exit code.")
    return SandboxExec(exit_code, _stream_text(payload, "stdout"), _stream_text(payload, "stderr"))


def _create_body(cwd: str) -> dict[str, Any]:
    return {
        "policy_mode": "append",
        "policy": {
            "filesystem_policy": {
                "bind_mounts": [
                    {"host_path": cwd, "sandbox_path": cwd, "mode": "rw"},
                ]
            }
        },
    }


def _sandbox_path(sandbox_id: str) -> str:
    return f"/api/v1/sandboxes/{sandbox_id}"


def _require_id(payload: dict[str, Any]) -> str:
    sandbox_id = payload.get("id")
    if not isinstance(sandbox_id, str) or not sandbox_id or any(char in sandbox_id for char in "/\\?#\x00"):
        raise SandboxError("Sandbox server returned an unusable sandbox id.")
    return sandbox_id


def _json_object(response: httpx.Response) -> dict[str, Any]:
    if response.status_code < 200 or response.status_code >= 300:
        raise SandboxError(f"Sandbox server returned HTTP {response.status_code}.")
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
