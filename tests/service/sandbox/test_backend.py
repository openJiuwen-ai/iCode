# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fail-closed run_shell. HTTP is mocked; nothing listens and nothing is spawned."""

from __future__ import annotations

import ast
import asyncio
import builtins
import contextlib
import json
import os
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx
import pytest

from chrys.service.sandbox import backend
from chrys.service.sandbox.backend import (
    _HTTP_TIMEOUT_SLACK_SECONDS,
    SandboxError,
    SandboxExec,
    run_shell,
)
from tests.support.symlinks import symlink_or_skip
from tests.support.waiting import wait_for

_SPAWN_CALLS: list[str] = []


@dataclass(frozen=True, slots=True)
class _Settings:
    """Stand-in with the four sandbox fields. Not the Settings class."""

    sandbox_enabled: bool = True
    sandbox_url: str = "http://sandbox.test"
    sandbox_token: str = ""
    sandbox_network: str = "guarded"


class _Script:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, object, str | None, str]] = []
        self.health_status = 200
        self.health: dict[str, object] = {"status": "ok"}
        self.create_phase = "ready"
        self.start_phase = "ready"
        self.start_status = 200
        self.get_phase = "ready"
        self.get_status = 200
        self.exec_status = 200
        self.exec_payload: dict[str, object] = {"exit_code": 0, "stdout": "out", "stderr": "err"}
        self.error: Exception | None = None
        self.malformed_path: str | None = None
        self.fail_path: str | None = None
        self.fail_error: Exception | None = None
        self.distinct_ids = False
        self.create_status = 201
        self.not_found_gets = 0
        self.phase_error: str | None = None
        self.phase_error_field = "error_message"
        self.read_timeouts: list[tuple[str, float | None]] = []
        self._issued = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.error is not None:
            raise self.error
        body: object = json.loads(request.content) if request.content else None
        self.events.append(
            (request.method, request.url.path, body, request.headers.get("authorization"), request.url.host)
        )
        path = request.url.path
        recorded = request.extensions.get("timeout")
        read_timeout = recorded.get("read") if isinstance(recorded, dict) else None
        self.read_timeouts.append((path, read_timeout if isinstance(read_timeout, int | float) else None))
        if self.fail_path is not None and path == self.fail_path:
            if self.fail_error is None:
                raise AssertionError("fail_path set without fail_error")
            raise self.fail_error
        if self.malformed_path is not None and path == self.malformed_path:
            return httpx.Response(200, content=b"{not-json")
        if path == "/health":
            return httpx.Response(self.health_status, json=self.health)
        if request.method == "POST" and path == "/api/v1/sandboxes":
            if self.create_status < 200 or self.create_status >= 300:
                return httpx.Response(self.create_status, json={"error": "unavailable"})
            sandbox_id = self._allocate_id()
            return httpx.Response(self.create_status, json=self._phase_body(sandbox_id, self.create_phase))
        route = _sandbox_route(path)
        if route is None:
            return httpx.Response(500, json={"error": "unexpected"})
        sandbox_id, action = route
        if request.method == "POST" and action == "start":
            return httpx.Response(self.start_status, json=self._phase_body(sandbox_id, self.start_phase))
        if request.method == "POST" and action == "stop":
            return httpx.Response(200, json={"id": sandbox_id, "phase": "stopped"})
        if request.method == "DELETE" and action == "":
            return httpx.Response(204)
        if request.method == "GET" and action == "":
            status = self.get_status
            if self.not_found_gets > 0:
                self.not_found_gets -= 1
                status = 404
            return httpx.Response(status, json={"id": sandbox_id, "phase": self.get_phase})
        if request.method == "POST" and action == "exec":
            return httpx.Response(self.exec_status, json=self.exec_payload)
        return httpx.Response(500, json={"error": "unexpected"})

    def _allocate_id(self) -> str:
        if not self.distinct_ids:
            return "sb-1"
        self._issued += 1
        return f"sb-{self._issued}"

    def _phase_body(self, sandbox_id: str, phase: str) -> dict[str, object]:
        body: dict[str, object] = {"id": sandbox_id, "phase": phase}
        if self.phase_error is not None:
            # SandboxRef serializes the engine failure as error_message.
            # The HTTP error envelope's "error" key is a different response.
            body[self.phase_error_field] = self.phase_error
        return body


class _ClosingClient(httpx.AsyncClient):
    """Spies ``aclose`` and ``__aexit__``.

    ``async with`` on httpx 0.28 closes through ``__aexit__`` and does not call
    ``aclose``. Either path sets ``is_closed``.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.close_events: list[str] = []

    async def aclose(self) -> None:
        self.close_events.append("aclose")
        await super().aclose()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        self.close_events.append("aexit")
        await super().__aexit__(exc_type, exc_value, traceback)


class _CountingLock(asyncio.Lock):
    def __init__(self) -> None:
        super().__init__()
        self.waiting = 0
        self.acquires = 0

    async def acquire(self) -> bool:
        self.waiting += 1
        try:
            acquired = await super().acquire()
        finally:
            self.waiting -= 1
        self.acquires += 1
        return acquired


class _PausingTransport(httpx.AsyncBaseTransport):
    """Holds the first matching request until ``release``.

    ``pause_when`` selects that request. The default holds the first request,
    which is how a test parks a call inside create or exec.
    """

    def __init__(
        self,
        script: _Script,
        entered: asyncio.Event,
        release: asyncio.Event,
        *,
        pause_when: Callable[[httpx.Request], bool] | None = None,
    ) -> None:
        self._script = script
        self._entered = entered
        self._release = release
        self._pause_when = pause_when
        self.calls = 0
        self._paused = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        should_pause = not self._paused and (
            self._pause_when(request) if self._pause_when is not None else self.calls == 1
        )
        if should_pause:
            self._paused = True
            self._entered.set()
            await self._release.wait()
        return self._script.handler(request)


class _Clock:
    """Manual ``time.monotonic`` so a lock wait can be longer than real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


_SECRET = _Settings(
    sandbox_token="bearer-token",
    sandbox_url="http://user:userinfo-secret@sandbox.test",
)

_SESSION_MARKERS = (
    "save_session",
    "save_recovery_session",
    "session.json",
    "session_dir",
    "SessionHistoryManager",
    "chrys.service.session",
    "chrys.service.state",
)


def _paths(script: _Script) -> list[tuple[str, str]]:
    return [(method, path) for method, path, *_rest in script.events]


def _sandbox_route(path: str) -> tuple[str, str] | None:
    prefix = "/api/v1/sandboxes/"
    if not path.startswith(prefix):
        return None
    sandbox_id, _separator, action = path.removeprefix(prefix).partition("/")
    if not sandbox_id:
        return None
    return sandbox_id, action


def _is_create(request: httpx.Request) -> bool:
    return request.method == "POST" and request.url.path == "/api/v1/sandboxes"


def _is_exec(request: httpx.Request) -> bool:
    return request.method == "POST" and request.url.path.endswith("/exec")


def _install_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport) -> None:
    def new_client(**kwargs: Any) -> httpx.AsyncClient:
        built = dict(kwargs)
        built["transport"] = transport
        return httpx.AsyncClient(**built)

    monkeypatch.setattr("chrys.service.sandbox.client._new_client", new_client)


async def _cancel_pending(*tasks: asyncio.Task[Any] | None) -> None:
    pending = [task for task in tasks if task is not None and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


def _exec_ids(script: _Script) -> list[str]:
    ids: list[str] = []
    for method, path in _paths(script):
        if method != "POST":
            continue
        route = _sandbox_route(path)
        if route is not None and route[1] == "exec":
            ids.append(route[0])
    return ids


def _mount_body_paths(paths: tuple[str, ...], mode: str) -> dict[str, object]:
    return {
        "policy_mode": "append",
        "policy": {
            "filesystem_policy": {
                "bind_mounts": [{"host_path": path, "sandbox_path": path, "mode": mode} for path in paths],
            }
        },
    }


def _mount_body(cwd: str, mode: str) -> dict[str, object]:
    return _mount_body_paths((cwd,), mode)


def _physical(path: Path) -> str:
    """The path ``os.path.realpath`` produces, which is what the backend mounts."""
    return os.path.realpath(path)


def _exec_workdirs(script: _Script) -> list[object]:
    workdirs: list[object] = []
    for event in script.events:
        body = event[2]
        if event[1].endswith("/exec") and isinstance(body, dict):
            workdirs.append(body.get("workdir"))
    return workdirs


async def _run_workspace(
    settings: _Settings,
    *,
    cwd: str,
    workspace_root: str,
    argv: tuple[str, ...] = ("echo", "hi"),
    workspace_dirs: tuple[str, ...] = (),
) -> SandboxExec:
    """Run one command with an explicit session workspace.

    Callers pass ``workspace_root``. Mounting ``cwd`` instead fails these tests.
    """
    return await run_shell(
        settings,
        argv=argv,
        cwd=cwd,
        timeout_seconds=12,
        linux=True,
        workspace_root=workspace_root,
        workspace_dirs=workspace_dirs,
    )


def _create_bodies(script: _Script) -> list[object]:
    return [event[2] for event in script.events if event[0] == "POST" and event[1] == "/api/v1/sandboxes"]


def _assert_no_secret(text: str) -> None:
    assert "bearer-token" not in text
    assert "userinfo-secret" not in text


def _shown(result: object) -> str:
    if isinstance(result, SandboxExec):
        return f"SandboxExec(exit_code={result.exit_code!r}, stdout={result.stdout!r}, stderr={result.stderr!r})"
    return repr(result)


def _install_closing(
    monkeypatch: pytest.MonkeyPatch,
    script: _Script,
) -> tuple[list[httpx.Timeout], list[_ClosingClient]]:
    timeouts: list[httpx.Timeout] = []
    clients: list[_ClosingClient] = []

    def new_client(**kwargs: Any) -> httpx.AsyncClient:
        timeout = kwargs["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        timeouts.append(timeout)
        built = dict(kwargs)
        built["transport"] = httpx.MockTransport(script.handler)
        http = _ClosingClient(**built)
        clients.append(http)
        return http

    monkeypatch.setattr("chrys.service.sandbox.client._new_client", new_client)
    return timeouts, clients


def _assert_client_closed(clients: list[_ClosingClient]) -> None:
    assert len(clients) == 1
    assert clients[0].is_closed
    # httpx's context manager closes in ``__aexit__``; ``aclose`` is the explicit method.
    assert clients[0].close_events == ["aexit"]


def _function_source(text: str, name: str) -> str:
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(text, node) or ""
    return ""


def _append_filesystem_semantics() -> str:
    """Classify vendored ``policy_mode=append`` for ``filesystem_policy``.

    ``SandboxManager._resolve_effective_policy`` sends ``PolicyMode.APPEND``
    through ``PolicyEngine.merge_policy``. Append mode calls ``_merge_value``,
    which merges mappings key by key and appends list items that are not
    already present. Override mode is the path that replaces a list wholesale.
    """
    root = Path(__file__).resolve().parents[3]
    manager = (root / "third_party/jiuwenbox/src/jiuwenbox/server/sandbox_manager.py").read_text(encoding="utf-8")
    engine = (root / "third_party/jiuwenbox/src/jiuwenbox/server/policy_engine.py").read_text(encoding="utf-8")
    resolve = _function_source(manager, "_resolve_effective_policy")
    merge = _function_source(engine, "merge_policy")
    value = _function_source(engine, "_merge_value")
    append_merges = "PolicyMode.APPEND" in resolve and "merge_policy(" in resolve
    append_mode_uses_value_merge = "_merge_value(" in merge and 'mode == "override"' in merge
    lists_append = "isinstance(base, list) and isinstance(extra, list)" in value and "merged.append(item)" in value
    dicts_merge = "isinstance(base, dict) and isinstance(extra, Mapping)" in value
    if append_merges and append_mode_uses_value_merge and lists_append and dicts_merge:
        return "list-merge"
    return "whole-replace"


def _assert_backend_has_no_session_writer() -> None:
    source = Path(backend.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
    assert all("session" not in module for module in modules)
    for marker in _SESSION_MARKERS:
        assert marker not in source


@pytest.fixture(autouse=True)
def _reset_sandbox_mount_caches():
    """Clear mount roots the process-wide id reset does not name."""
    backend._writable_root = None
    backend._writable_by_root.clear()
    backend._readonly_id = None
    backend._readonly_root = None
    backend._readonly_physical = False
    yield
    backend._writable_root = None
    backend._writable_by_root.clear()
    backend._readonly_id = None
    backend._readonly_root = None
    backend._readonly_physical = False


@pytest.fixture(autouse=True)
def _forbid_supervisor_and_host_spawn(monkeypatch: pytest.MonkeyPatch):
    """A call into the supervisor or a managed child is a host-side path."""
    _SPAWN_CALLS.clear()

    async def forbid(*_args: object, **_kwargs: object) -> tuple[str, str]:
        _SPAWN_CALLS.append("ensure-started")
        raise SandboxError("supervisor was started")

    def forbid_lease(*_args: object, **_kwargs: object) -> object:
        _SPAWN_CALLS.append("leased-server")
        raise SandboxError("supervisor was leased")

    def forbid_managed(*_args: object, **_kwargs: object) -> object:
        _SPAWN_CALLS.append("managed-subprocess")
        raise AssertionError("managed subprocess")

    monkeypatch.setattr("chrys.service.sandbox.supervisor.ensure_started", forbid)
    monkeypatch.setattr("chrys.service.sandbox.supervisor.leased_server", forbid_lease)
    monkeypatch.setattr("chrys.foundation.platform.process.managed_subprocess", forbid_managed)


def _install(monkeypatch: pytest.MonkeyPatch, script: _Script) -> list[httpx.Timeout]:
    timeouts: list[httpx.Timeout] = []

    def new_client(**kwargs: Any) -> httpx.AsyncClient:
        timeout = kwargs["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        timeouts.append(timeout)
        built = dict(kwargs)
        built["transport"] = httpx.MockTransport(script.handler)
        return httpx.AsyncClient(**built)

    monkeypatch.setattr("chrys.service.sandbox.client._new_client", new_client)
    return timeouts


def _run(
    script: _Script,
    monkeypatch: pytest.MonkeyPatch,
    settings: _Settings | None = None,
    *,
    argv: tuple[str, ...] = ("echo", "hi"),
    linux: bool = True,
    timeout_seconds: int = 12,
    cwd: str = "/work",
):
    _install(monkeypatch, script)
    return run_shell(
        settings if settings is not None else _Settings(),
        argv=argv,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        linux=linux,
    )


async def test_connection_failure_is_sandbox_error_and_does_not_spawn(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    script.error = httpx.ConnectError("down bearer-token http://user:userinfo-secret@sandbox.test")
    with pytest.raises(SandboxError, match="unreachable") as caught:
        await _run(
            script,
            monkeypatch,
            _Settings(sandbox_token="bearer-token", sandbox_url="http://user:userinfo-secret@sandbox.test"),
        )
    text = str(caught.value)
    assert "bearer-token" not in text
    assert "userinfo-secret" not in text
    assert "supervisor was started" not in text
    assert _SPAWN_CALLS == []


async def test_http_401_and_500_are_sandbox_errors_and_do_not_spawn(monkeypatch: pytest.MonkeyPatch):
    denied = _Script()
    denied.health_status = 401
    denied.health = {"error": "bearer-token"}
    with pytest.raises(SandboxError, match="HTTP 401") as caught:
        await _run(denied, monkeypatch, _Settings(sandbox_token="bearer-token"))
    assert "bearer-token" not in str(caught.value)
    assert _paths(denied) == [("GET", "/health")]
    assert _SPAWN_CALLS == []

    failed = _Script()
    failed.health_status = 500
    with pytest.raises(SandboxError, match="HTTP 500"):
        await _run(failed, monkeypatch)
    assert _paths(failed) == [("GET", "/health")]
    assert _SPAWN_CALLS == []


async def test_timeout_and_unhealthy_server_do_not_spawn(monkeypatch: pytest.MonkeyPatch):
    timed = _Script()
    timed.error = httpx.ReadTimeout("slow bearer-token")
    with pytest.raises(SandboxError, match="timed out") as caught:
        await _run(timed, monkeypatch, _Settings(sandbox_token="bearer-token"))
    assert "bearer-token" not in str(caught.value)
    assert _SPAWN_CALLS == []

    unhealthy = _Script()
    unhealthy.health = {"status": "starting"}
    with pytest.raises(SandboxError, match="health is not ok"):
        await _run(unhealthy, monkeypatch)
    assert _paths(unhealthy) == [("GET", "/health")]
    assert _SPAWN_CALLS == []


async def test_create_then_exec_and_second_call_reuses_the_sandbox_id(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    timeouts = _install(monkeypatch, script)
    settings = _Settings(sandbox_token="")
    first = await run_shell(settings, argv=("bash", "-c", "echo"), cwd="/work", timeout_seconds=12, linux=True)
    second = await run_shell(settings, argv=["pwd"], cwd="/work", timeout_seconds=12, linux=True)
    assert isinstance(first, SandboxExec)
    assert first.exit_code == 0
    assert first.stdout == "out"
    assert first.stderr == "err"
    assert second.exit_code == 0
    creates = [event for event in script.events if event[0] == "POST" and event[1] == "/api/v1/sandboxes"]
    assert len(creates) == 1
    assert creates[0][2] == {
        "policy_mode": "append",
        "policy": {
            "filesystem_policy": {"bind_mounts": [{"host_path": "/work", "sandbox_path": "/work", "mode": "rw"}]}
        },
    }
    assert creates[0][3] is None
    execs = [event for event in script.events if event[1].endswith("/exec")]
    assert [event[2]["command"] for event in execs] == [["bash", "-c", "echo"], ["pwd"]]
    assert execs[0][2]["workdir"] == "/work"
    assert execs[0][2]["timeout_seconds"] == 12
    assert _paths(script).count(("POST", "/api/v1/sandboxes/sb-1/start")) == 0
    assert ("GET", "/api/v1/sandboxes/sb-1") in _paths(script)
    assert timeouts[0].read == 12 + _HTTP_TIMEOUT_SLACK_SECONDS
    assert _SPAWN_CALLS == []


async def test_create_starts_when_the_new_sandbox_is_not_ready(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    script.create_phase = "provisioning"
    result = await _run(script, monkeypatch)
    assert result.exit_code == 0
    assert ("POST", "/api/v1/sandboxes/sb-1/start") in _paths(script)
    assert _SPAWN_CALLS == []


async def test_sandbox_that_stays_unready_is_sandbox_error(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    script.create_phase = "provisioning"
    script.start_phase = "error"
    with pytest.raises(SandboxError, match="did not become ready"):
        await _run(script, monkeypatch)
    assert not any(path.endswith("/exec") for _method, path in _paths(script))
    assert _SPAWN_CALLS == []


async def test_missing_exit_code_is_sandbox_error(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    script.exec_payload = {"stdout": "x", "stderr": "y"}
    with pytest.raises(SandboxError, match="exit code"):
        await _run(script, monkeypatch)
    assert _SPAWN_CALLS == []


async def test_nonzero_exit_code_is_a_result(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    script.exec_payload = {"exit_code": 7, "stdout": "", "stderr": "nope"}
    result = await _run(script, monkeypatch)
    assert isinstance(result, SandboxExec)
    assert result.exit_code == 7
    assert result.stdout == ""
    assert result.stderr == "nope"
    assert _SPAWN_CALLS == []


async def test_configured_url_does_not_start_the_supervisor(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    result = await _run(script, monkeypatch, _Settings(sandbox_url="http://sandbox.test"))
    assert result.stdout == "out"
    assert script.events[0][4] == "sandbox.test"
    assert _SPAWN_CALLS == []


async def test_empty_url_asks_the_supervisor_and_uses_its_address(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    @contextlib.asynccontextmanager
    async def fake_lease(network_mode: str) -> AsyncIterator[tuple[str, str]]:
        calls.append(f"enter:{network_mode}")
        try:
            yield "http://started.sandbox", "generated-token"
        finally:
            calls.append("exit")

    monkeypatch.setattr("chrys.service.sandbox.supervisor.leased_server", fake_lease)
    script = _Script()
    result = await _run(
        script,
        monkeypatch,
        _Settings(sandbox_url="", sandbox_token="bearer-token", sandbox_network="offline"),
    )
    assert calls == ["enter:offline", "exit"]
    assert result.exit_code == 0
    assert script.events
    assert {event[3] for event in script.events} == {"Bearer generated-token"}
    assert script.events[0][4] == "started.sandbox"
    assert _SPAWN_CALLS == []


async def test_non_linux_does_not_open_a_connection(monkeypatch: pytest.MonkeyPatch):
    def refuse_client(**_kwargs: object) -> httpx.AsyncClient:
        raise AssertionError("http client")

    monkeypatch.setattr("chrys.service.sandbox.client._new_client", refuse_client)
    with pytest.raises(SandboxError, match="only on Linux"):
        await run_shell(
            _Settings(sandbox_url=""),
            argv=["echo"],
            cwd="/work",
            timeout_seconds=5,
            linux=False,
        )
    assert _SPAWN_CALLS == []


def test_backend_and_client_have_no_host_shell():
    root = Path(__file__).resolve().parents[3] / "src" / "chrys" / "service" / "sandbox"
    for name in ("__init__.py", "types.py", "client.py", "backend.py"):
        text = (root / name).read_text(encoding="utf-8")
        tree = ast.parse(text)
        assert "subprocess" not in text
        assert "os.system" not in text
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "subprocess" for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.module != "subprocess"
    assert backend.run_shell.__module__ == "chrys.service.sandbox.backend"


async def test_cached_id_that_is_not_ready_is_recreated(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    script.get_phase = "stopped"
    _install(monkeypatch, script)
    settings = _Settings()
    first = await run_shell(settings, argv=("echo", "one"), cwd="/work", timeout_seconds=12, linux=True)
    second = await run_shell(settings, argv=("echo", "two"), cwd="/work", timeout_seconds=12, linux=True)
    assert isinstance(first, SandboxExec)
    assert isinstance(second, SandboxExec)
    assert len(_create_bodies(script)) == 2
    assert _paths(script).count(("GET", "/api/v1/sandboxes/sb-1")) == 1
    assert _paths(script).count(("POST", "/api/v1/sandboxes/sb-1/start")) == 0
    assert backend._sandbox_id == "sb-1"
    assert _SPAWN_CALLS == []


async def test_cached_id_404_recreates_once_and_the_next_command_reuses_it(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    script.distinct_ids = True
    _install(monkeypatch, script)
    settings = _Settings()
    await run_shell(settings, argv=("echo", "one"), cwd="/work", timeout_seconds=12, linux=True)
    script.not_found_gets = 1
    recovered = await run_shell(settings, argv=("echo", "two"), cwd="/work", timeout_seconds=12, linux=True)
    following = await run_shell(settings, argv=("echo", "three"), cwd="/work/nested", timeout_seconds=12, linux=True)
    assert recovered.exit_code == 0
    assert following.exit_code == 0
    assert _create_bodies(script) == [_mount_body("/work", "rw"), _mount_body("/work", "rw")]
    assert _exec_ids(script) == ["sb-1", "sb-2", "sb-2"]
    assert backend._sandbox_id == "sb-2"
    assert backend._writable_root == "/work"
    assert _paths(script).count(("GET", "/api/v1/sandboxes/sb-1")) == 1
    assert _paths(script).count(("GET", "/api/v1/sandboxes/sb-2")) == 1
    assert _SPAWN_CALLS == []


async def test_outside_cwd_reuses_the_writable_sandbox_after_a_404(monkeypatch: pytest.MonkeyPatch):
    """An outside cwd does not bind a directory. A 404 recreates the original root."""
    script = _Script()
    script.distinct_ids = True
    _install(monkeypatch, script)
    settings = _Settings()
    await run_shell(settings, argv=("echo", "rw"), cwd="/work/sub", timeout_seconds=12, linux=True)
    await run_shell(settings, argv=("echo", "out"), cwd="/opt/chrys-sandbox-outside", timeout_seconds=12, linux=True)
    script.not_found_gets = 1
    replaced = await run_shell(
        settings, argv=("echo", "new"), cwd="/opt/chrys-sandbox-outside", timeout_seconds=12, linux=True
    )
    back = await run_shell(settings, argv=("echo", "back"), cwd="/work/sub", timeout_seconds=12, linux=True)
    assert replaced.exit_code == 0
    assert back.exit_code == 0
    assert _create_bodies(script) == [_mount_body("/work/sub", "rw"), _mount_body("/work/sub", "rw")]
    assert _exec_ids(script) == ["sb-1", "sb-1", "sb-2", "sb-2"]
    assert _exec_workdirs(script)[1:3] == ["/opt/chrys-sandbox-outside", "/opt/chrys-sandbox-outside"]
    assert backend._sandbox_id == "sb-2"
    assert backend._writable_root == "/work/sub"
    assert backend._readonly_id is None
    assert _SPAWN_CALLS == []


async def test_a_second_404_while_replacing_a_cached_sandbox_is_an_error(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    script.distinct_ids = True
    _install(monkeypatch, script)
    await run_shell(_SECRET, argv=("echo",), cwd="/work", timeout_seconds=12, linux=True)
    script.not_found_gets = 1
    script.create_status = 404
    with pytest.raises(SandboxError, match="HTTP 404") as caught:
        await run_shell(_SECRET, argv=("echo",), cwd="/work", timeout_seconds=12, linux=True)
    _assert_no_secret(str(caught.value))
    assert len(_create_bodies(script)) == 2
    assert _exec_ids(script) == ["sb-1"]
    assert backend._sandbox_id is None
    assert backend._writable_root is None
    assert _SPAWN_CALLS == []


async def test_cached_get_other_http_error_does_not_create_another_sandbox(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    _install(monkeypatch, script)
    await run_shell(_SECRET, argv=("echo",), cwd="/work", timeout_seconds=12, linux=True)
    script.get_status = 500
    with pytest.raises(SandboxError, match="HTTP 500") as caught:
        await run_shell(_SECRET, argv=("echo",), cwd="/work", timeout_seconds=12, linux=True)
    _assert_no_secret(str(caught.value))
    assert len(_create_bodies(script)) == 1
    assert _exec_ids(script) == ["sb-1"]
    assert backend._sandbox_id == "sb-1"
    assert _SPAWN_CALLS == []


async def test_cwd_outside_the_writable_root_reuses_it_without_a_new_bind(monkeypatch: pytest.MonkeyPatch):
    """``/work`` is not inside ``/work/sub`` and is not protected, so it adds no mount."""
    script = _Script()
    script.distinct_ids = True
    _install(monkeypatch, script)
    settings = _Settings()
    first = await run_shell(settings, argv=("echo", "sub"), cwd="/work/sub", timeout_seconds=12, linux=True)
    nested = await run_shell(settings, argv=("echo", "leaf"), cwd="/work/sub/leaf", timeout_seconds=12, linux=True)
    outside = await run_shell(settings, argv=("echo", "root"), cwd="/work", timeout_seconds=12, linux=True)
    back = await run_shell(settings, argv=("echo", "again"), cwd="/work/sub", timeout_seconds=12, linux=True)
    assert first.exit_code == 0
    assert nested.exit_code == 0
    assert outside.exit_code == 0
    assert back.exit_code == 0
    assert _create_bodies(script) == [_mount_body("/work/sub", "rw")]
    assert _exec_ids(script) == ["sb-1", "sb-1", "sb-1", "sb-1"]
    assert _exec_workdirs(script) == ["/work/sub", "/work/sub/leaf", "/work", "/work/sub"]
    assert backend._sandbox_id == "sb-1"
    assert backend._writable_root == "/work/sub"
    assert backend._readonly_id is None
    assert _SPAWN_CALLS == []


async def test_sibling_directory_is_not_inside_the_writable_root(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    script.distinct_ids = True
    _install(monkeypatch, script)
    settings = _Settings()
    await run_shell(settings, argv=("echo", "work"), cwd="/work", timeout_seconds=12, linux=True)
    sibling = await run_shell(settings, argv=("echo", "extra"), cwd="/work-extra", timeout_seconds=12, linux=True)
    escaped = await run_shell(
        settings, argv=("echo", "dotdot"), cwd="/work/../work-extra", timeout_seconds=12, linux=True
    )
    child = await run_shell(settings, argv=("echo", "child"), cwd="/work/pkg", timeout_seconds=12, linux=True)
    assert sibling.exit_code == 0
    assert escaped.exit_code == 0
    assert child.exit_code == 0
    assert _create_bodies(script) == [_mount_body("/work", "rw")]
    assert _exec_ids(script) == ["sb-1", "sb-1", "sb-1", "sb-1"]
    assert _exec_workdirs(script) == ["/work", "/work-extra", "/work/../work-extra", "/work/pkg"]
    assert backend._sandbox_id == "sb-1"
    assert backend._writable_root == "/work"
    assert backend._readonly_id is None
    assert _SPAWN_CALLS == []


async def test_start_failure_after_create_is_sandbox_error(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    script.create_phase = "provisioning"
    script.start_status = 502
    with pytest.raises(SandboxError, match="HTTP 502") as caught:
        await _run(script, monkeypatch, _SECRET)
    _assert_no_secret(str(caught.value))
    assert ("POST", "/api/v1/sandboxes") in _paths(script)
    assert ("POST", "/api/v1/sandboxes/sb-1/start") in _paths(script)
    assert not any(path.endswith("/exec") for _method, path in _paths(script))
    assert backend._sandbox_id is None
    assert _SPAWN_CALLS == []


async def test_exec_5xx_is_sandbox_error(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    script.exec_status = 503
    with pytest.raises(SandboxError, match="HTTP 503") as caught:
        await _run(script, monkeypatch, _SECRET)
    _assert_no_secret(str(caught.value))
    assert ("POST", "/api/v1/sandboxes/sb-1/exec") in _paths(script)
    assert not any(path.endswith("/stop") for _method, path in _paths(script))
    assert _SPAWN_CALLS == []


async def test_malformed_exec_json_is_sandbox_error(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    script.malformed_path = "/api/v1/sandboxes/sb-1/exec"
    with pytest.raises(SandboxError, match="not JSON") as caught:
        await _run(script, monkeypatch, _SECRET)
    _assert_no_secret(str(caught.value))
    assert _SPAWN_CALLS == []


@pytest.mark.parametrize("omitted", ["stdout", "stderr"])
async def test_missing_stdout_or_stderr_is_empty_text(monkeypatch: pytest.MonkeyPatch, omitted: str):
    """A missing stream is empty output. Only a missing exit code fails the call."""
    script = _Script()
    payload = {"exit_code": 0, "stdout": "out", "stderr": "err"}
    del payload[omitted]
    script.exec_payload = payload
    result = await _run(script, monkeypatch, _SECRET)
    assert isinstance(result, SandboxExec)
    assert result.exit_code == 0
    assert result.stdout == ("" if omitted == "stdout" else "out")
    assert result.stderr == ("" if omitted == "stderr" else "err")
    assert _SPAWN_CALLS == []


async def test_concurrent_first_calls_create_one_sandbox(monkeypatch: pytest.MonkeyPatch):
    backend._sandbox_id = None
    script = _Script()
    entered = asyncio.Event()
    release = asyncio.Event()
    transport = _PausingTransport(script, entered, release, pause_when=_is_create)
    lock = _CountingLock()
    backend._lock = lock
    _install_transport(monkeypatch, transport)
    settings = _Settings()

    async def call(word: str) -> SandboxExec:
        return await run_shell(settings, argv=("echo", word), cwd="/work", timeout_seconds=12, linux=True)

    task_a: asyncio.Task[SandboxExec] | None = asyncio.create_task(call("one"))
    task_b: asyncio.Task[SandboxExec] | None = None
    try:
        await wait_for(lambda: entered.is_set() or task_a.done(), description="first sandbox call reached HTTP")
        if task_a.done():
            await task_a
        assert entered.is_set()
        assert lock.locked()
        assert lock.acquires == 1
        assert _create_bodies(script) == []
        task_b = asyncio.create_task(call("two"))
        await wait_for(
            lambda: lock.waiting == 1 or task_b.done(),
            description="second call waits on the sandbox lock",
        )
        if task_b.done():
            await task_b
        assert lock.waiting == 1
        assert not task_b.done()
        assert _create_bodies(script) == []
        release.set()
        first, second = await asyncio.gather(task_a, task_b)
    finally:
        release.set()
        pending = [task for task in (task_a, task_b) if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert len(_create_bodies(script)) == 1
    assert backend._sandbox_id == "sb-1"
    assert sum(1 for method, path in _paths(script) if method == "POST" and path.endswith("/exec")) == 2
    assert _SPAWN_CALLS == []


async def test_sandbox_id_is_not_written_to_a_session_file(monkeypatch: pytest.MonkeyPatch):
    _assert_backend_has_no_session_writer()
    opened: list[str] = []
    real_open = builtins.open

    def tracking_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, int):
            opened.append(str(file))
        else:
            opened.append(os.fspath(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracking_open)
    script = _Script()
    result = await _run(script, monkeypatch)
    assert isinstance(result, SandboxExec)
    assert backend._sandbox_id == "sb-1"
    assert [path for path in opened if "session" in path.lower()] == []
    assert _SPAWN_CALLS == []


async def test_create_body_is_an_append_fragment_for_the_cwd_bind(monkeypatch: pytest.MonkeyPatch):
    cwd = "/var/chrys-work"
    script = _Script()
    await _run(script, monkeypatch, cwd=cwd)
    bodies = _create_bodies(script)
    assert len(bodies) == 1
    body = bodies[0]
    assert isinstance(body, dict)
    fragment = {
        "policy_mode": "append",
        "policy": {
            "filesystem_policy": {
                "bind_mounts": [{"host_path": cwd, "sandbox_path": cwd, "mode": "rw"}],
            }
        },
    }
    semantics = _append_filesystem_semantics()
    if semantics == "list-merge":
        assert body == fragment
    else:
        filesystem = body["policy"]["filesystem_policy"]
        assert isinstance(filesystem, dict)
        assert set(filesystem) > {"bind_mounts"}
    assert semantics == "list-merge"
    assert _SPAWN_CALLS == []


async def test_http_timeout_exceeds_command_timeout_and_client_closes(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    timeouts, clients = _install_closing(monkeypatch, script)
    command_timeout = 40
    result = await run_shell(
        _Settings(),
        argv=("echo",),
        cwd="/work",
        timeout_seconds=command_timeout,
        linux=True,
    )
    assert result.exit_code == 0
    assert len(timeouts) == 1
    timeout = timeouts[0]
    for field in (timeout.connect, timeout.read, timeout.write, timeout.pool):
        assert isinstance(field, float)
        assert field > command_timeout
    _assert_client_closed(clients)
    assert _SPAWN_CALLS == []


async def test_dropped_exec_connection_is_sandbox_error_and_closes_client(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    script.fail_path = "/api/v1/sandboxes/sb-1/exec"
    script.fail_error = httpx.RemoteProtocolError(
        "Server disconnected bearer-token http://user:userinfo-secret@sandbox.test"
    )
    _timeouts, clients = _install_closing(monkeypatch, script)
    with pytest.raises(SandboxError, match="unreachable") as caught:
        await run_shell(_SECRET, argv=("echo",), cwd="/work", timeout_seconds=12, linux=True)
    text = str(caught.value)
    _assert_no_secret(text)
    assert "disconnected" not in text
    _assert_client_closed(clients)
    assert _SPAWN_CALLS == []


async def test_slow_exec_does_not_block_another_commands_create(monkeypatch: pytest.MonkeyPatch):
    """A paused exec releases the cache lock, so another cwd can reuse the sandbox and exec."""
    script = _Script()
    script.distinct_ids = True
    entered = asyncio.Event()
    release = asyncio.Event()
    transport = _PausingTransport(script, entered, release, pause_when=_is_exec)
    lock = _CountingLock()
    backend._lock = lock
    _install_transport(monkeypatch, transport)
    settings = _Settings()

    async def call(cwd: str, word: str) -> SandboxExec:
        return await run_shell(settings, argv=("echo", word), cwd=cwd, timeout_seconds=12, linux=True)

    task_a: asyncio.Task[SandboxExec] | None = asyncio.create_task(call("/work", "one"))
    task_b: asyncio.Task[SandboxExec] | None = None
    try:
        await wait_for(lambda: entered.is_set() or task_a.done(), description="first exec reached the sandbox")
        if task_a.done():
            await task_a
        assert entered.is_set()
        assert not lock.locked()
        task_b = asyncio.create_task(call("/outside", "two"))
        second = await task_b
        assert not task_a.done()
        assert not release.is_set()
        assert second.exit_code == 0
        assert _create_bodies(script) == [_mount_body("/work", "rw")]
        assert _exec_ids(script) == ["sb-1"]
        release.set()
        first = await task_a
    finally:
        release.set()
        await _cancel_pending(task_a, task_b)
    assert first.exit_code == 0
    assert _exec_ids(script) == ["sb-1", "sb-1"]
    assert _exec_workdirs(script) == ["/outside", "/work"]
    assert backend._sandbox_id == "sb-1"
    assert backend._readonly_id is None
    assert _deleted(script) == []
    assert _SPAWN_CALLS == []


async def test_lock_wait_longer_than_the_command_timeout_does_not_exec(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    _install(monkeypatch, script)
    lock = _CountingLock()
    backend._lock = lock
    await lock.acquire()
    task = asyncio.create_task(run_shell(_Settings(), argv=("echo",), cwd="/work", timeout_seconds=1, linux=True))
    try:
        with pytest.raises(SandboxError, match="timed out waiting") as caught:
            await task
    finally:
        if lock.locked():
            lock.release()
        await _cancel_pending(task)
    _assert_no_secret(str(caught.value))
    assert not any(path.endswith("/exec") for _method, path in _paths(script))
    assert ("POST", "/api/v1/sandboxes") not in _paths(script)
    assert ("GET", "/health") in _paths(script)
    assert _SPAWN_CALLS == []


async def test_lock_wait_reduces_the_exec_timeout_and_http_timeout(monkeypatch: pytest.MonkeyPatch):
    clock = _Clock()
    monkeypatch.setattr(backend, "_monotonic", clock.monotonic)
    script = _Script()
    _install(monkeypatch, script)
    lock = _CountingLock()
    backend._lock = lock
    await lock.acquire()
    task = asyncio.create_task(run_shell(_Settings(), argv=("echo",), cwd="/work", timeout_seconds=12, linux=True))
    try:
        await wait_for(
            lambda: lock.waiting == 1 or task.done(),
            description="command waits for the sandbox lock",
        )
        if task.done():
            await task
        assert lock.waiting == 1
        clock.now = 4.0
        lock.release()
        result = await task
    finally:
        if lock.locked():
            lock.release()
        await _cancel_pending(task)
    assert result.exit_code == 0
    exec_bodies = [event[2] for event in script.events if event[1].endswith("/exec")]
    assert exec_bodies[0]["timeout_seconds"] == 8
    health = [read for path, read in script.read_timeouts if path == "/health"]
    exec_reads = [read for path, read in script.read_timeouts if path.endswith("/exec")]
    assert health == [12 + _HTTP_TIMEOUT_SLACK_SECONDS]
    assert exec_reads == [8 + _HTTP_TIMEOUT_SLACK_SECONDS]
    assert _SPAWN_CALLS == []


async def test_cancel_during_exec_stops_the_sandbox_and_clears_its_cache(monkeypatch: pytest.MonkeyPatch):
    """Cancel keeps the writable root. The next cwd does not become a new mount."""
    script = _Script()
    script.distinct_ids = True
    entered = asyncio.Event()
    release = asyncio.Event()
    transport = _PausingTransport(script, entered, release, pause_when=_is_exec)
    _install_transport(monkeypatch, transport)
    settings = _Settings()
    task = asyncio.create_task(run_shell(settings, argv=("echo", "one"), cwd="/work", timeout_seconds=12, linux=True))
    try:
        await wait_for(lambda: entered.is_set() or task.done(), description="exec is in flight")
        if task.done():
            await task
        assert entered.is_set()
        assert backend._sandbox_id == "sb-1"
        assert backend._writable_root == "/work"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await _cancel_pending(task)
    assert backend._sandbox_id == "sb-1"
    assert backend._writable_root == "/work"
    followed = await run_shell(settings, argv=("echo", "two"), cwd="/work/sub", timeout_seconds=12, linux=True)
    assert followed.exit_code == 0
    assert _create_bodies(script) == [_mount_body("/work", "rw")]
    assert _exec_ids(script)[-1] == "sb-1"
    assert _exec_workdirs(script)[-1] == "/work/sub"
    assert backend._sandbox_id == "sb-1"
    assert backend._writable_root == "/work"
    assert _SPAWN_CALLS == []


async def test_cancel_during_exec_still_cancels_when_stop_fails(monkeypatch: pytest.MonkeyPatch):
    """A failed stop still cancels, and the writable root stays the one already cached."""
    script = _Script()
    script.distinct_ids = True
    entered = asyncio.Event()
    release = asyncio.Event()
    transport = _PausingTransport(script, entered, release, pause_when=_is_exec)
    _install_transport(monkeypatch, transport)
    task = asyncio.create_task(run_shell(_SECRET, argv=("echo",), cwd="/work", timeout_seconds=12, linux=True))
    try:
        await wait_for(lambda: entered.is_set() or task.done(), description="exec is in flight")
        if task.done():
            await task
        assert backend._sandbox_id == "sb-1"
        assert backend._writable_root == "/work"
        script.fail_path = "/api/v1/sandboxes/sb-1/stop"
        script.fail_error = httpx.ConnectError("down bearer-token http://user:userinfo-secret@sandbox.test")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await _cancel_pending(task)
    assert backend._sandbox_id == "sb-1"
    assert backend._writable_root == "/work"
    followed = await run_shell(_SECRET, argv=("echo", "two"), cwd="/work/sub", timeout_seconds=12, linux=True)
    assert followed.exit_code == 0
    assert _create_bodies(script) == [_mount_body("/work", "rw")]
    assert _exec_ids(script)[-1] == "sb-1"
    assert backend._sandbox_id == "sb-1"
    assert backend._writable_root == "/work"
    assert _SPAWN_CALLS == []


async def test_cancel_during_create_does_not_stop_the_sandbox(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    entered = asyncio.Event()
    release = asyncio.Event()
    transport = _PausingTransport(script, entered, release, pause_when=_is_create)
    _install_transport(monkeypatch, transport)
    task = asyncio.create_task(run_shell(_Settings(), argv=("echo",), cwd="/work", timeout_seconds=12, linux=True))
    try:
        await wait_for(lambda: entered.is_set() or task.done(), description="create is in flight")
        if task.done():
            await task
        assert entered.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await _cancel_pending(task)
    assert not any(path.endswith("/stop") for _method, path in _paths(script))
    assert not any(path.endswith("/exec") for _method, path in _paths(script))
    assert _deleted(script) == []
    assert backend._sandbox_id is None
    assert _SPAWN_CALLS == []


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        ("bubblewrap is missing", "Sandbox did not become ready: bubblewrap is missing"),
        ("http://user:userinfo-secret@sandbox.test/nope", "Sandbox did not become ready."),
        ("line one\nline two", "Sandbox did not become ready: line one"),
        ("x" * 201, "Sandbox did not become ready: " + ("x" * 199) + "…"),
        ("\n\n  usable line  \nhttp://example.com/later", "Sandbox did not become ready: usable line"),
        ("http://sandbox.test/fail\nusable second line", "Sandbox did not become ready: usable second line"),
        (("y" * 201) + "\nshort line", "Sandbox did not become ready: " + ("y" * 199) + "…"),
        (
            "bubblewrap missing at http://127.0.0.1:9/health",
            "Sandbox did not become ready: bubblewrap missing at",
        ),
        ("\x01hidden\nplain reason", "Sandbox did not become ready: plain reason"),
    ],
)
async def test_unready_sandbox_includes_only_a_short_error_line(
    monkeypatch: pytest.MonkeyPatch,
    error: str,
    shown: str,
):
    script = _Script()
    script.create_phase = "provisioning"
    script.start_phase = "error"
    script.phase_error = error
    with pytest.raises(SandboxError, match="did not become ready") as caught:
        await _run(script, monkeypatch, _SECRET)
    text = str(caught.value)
    assert text == shown
    _assert_no_secret(text)
    assert "\n" not in text
    assert not any(path.endswith("/exec") for _method, path in _paths(script))
    assert _SPAWN_CALLS == []


async def test_unready_sandbox_ignores_the_http_error_key(monkeypatch: pytest.MonkeyPatch):
    """``error`` is the HTTP failure envelope, not the sandbox ref field."""
    script = _Script()
    script.create_phase = "provisioning"
    script.start_phase = "error"
    script.phase_error = "bubblewrap is missing"
    script.phase_error_field = "error"
    with pytest.raises(SandboxError, match="did not become ready") as caught:
        await _run(script, monkeypatch, _SECRET)
    assert str(caught.value) == "Sandbox did not become ready."
    assert _SPAWN_CALLS == []


def test_unready_detail_reads_the_sandbox_ref_error_message() -> None:
    """The client field is the one ``SandboxRef`` serializes."""
    root = Path(__file__).resolve().parents[3]
    model = (root / "third_party/jiuwenbox/src/jiuwenbox/models/sandbox.py").read_text(encoding="utf-8")
    tree = ast.parse(model)
    fields: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "SandboxRef":
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields.append(stmt.target.id)
    assert "error_message" in fields
    assert "error" not in fields
    not_ready = _function_source(Path(backend.__file__).read_text(encoding="utf-8"), "_not_ready")
    assert 'payload.get("error_message")' in not_ready


async def test_first_command_outside_the_workspace_does_not_become_the_read_write_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """An outside cwd is only the workdir. The writable mount stays the workspace."""
    work = tmp_path / "work"
    pkg = work / "pkg"
    pkg.mkdir(parents=True)
    outside = "/opt/chrys-sandbox-outside"
    child = "/opt/chrys-sandbox-outside/child"
    physical_work = _physical(work)
    physical_outside = os.path.realpath(outside)
    physical_child = os.path.realpath(child)
    script = _Script()
    script.distinct_ids = True
    _install(monkeypatch, script)
    settings = _Settings()
    first = await _run_workspace(settings, argv=("echo", "out"), cwd=outside, workspace_root=str(work))
    nested = await _run_workspace(settings, argv=("echo", "child"), cwd=child, workspace_root=str(work))
    inside = await _run_workspace(settings, argv=("echo", "in"), cwd=str(pkg), workspace_root=str(work))
    assert first.exit_code == 0
    assert nested.exit_code == 0
    assert inside.exit_code == 0
    assert _create_bodies(script) == [_mount_body(physical_work, "rw")]
    assert _exec_ids(script) == ["sb-1", "sb-1", "sb-1"]
    assert _exec_workdirs(script) == [physical_outside, physical_child, _physical(pkg)]
    assert backend._writable_by_root == {physical_work: "sb-1"}
    assert backend._sandbox_id is None
    assert backend._writable_root is None
    assert backend._readonly_id is None
    assert _mount_body(physical_outside, "rw") not in _create_bodies(script)
    assert _mount_body(physical_outside, "ro") not in _create_bodies(script)
    assert _mount_body(_physical(pkg), "rw") not in _create_bodies(script)
    assert _SPAWN_CALLS == []


async def test_subdirectory_command_keeps_the_workspace_root_read_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A later command at the workspace root stays read-write after a subdirectory."""
    work = tmp_path / "work"
    sub = work / "sub"
    sub.mkdir(parents=True)
    physical_work = _physical(work)
    script = _Script()
    script.distinct_ids = True
    _install(monkeypatch, script)
    settings = _Settings()
    first = await _run_workspace(settings, argv=("echo", "sub"), cwd=str(sub), workspace_root=str(work))
    root = await _run_workspace(settings, argv=("echo", "root"), cwd=str(work), workspace_root=str(work))
    assert first.exit_code == 0
    assert root.exit_code == 0
    assert _create_bodies(script) == [_mount_body(physical_work, "rw")]
    assert _exec_ids(script) == ["sb-1", "sb-1"]
    assert _exec_workdirs(script) == [str(sub), str(work)]
    assert backend._writable_by_root == {physical_work: "sb-1"}
    assert _mount_body(_physical(sub), "rw") not in _create_bodies(script)
    assert _SPAWN_CALLS == []


async def test_workspace_sibling_directory_is_not_read_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``work-extra`` is not inside ``work`` after realpath component comparison."""
    work = tmp_path / "work"
    sub = work / "sub"
    sub.mkdir(parents=True)
    sibling = "/opt/chrys-sandbox-sibling"
    physical_work = _physical(work)
    physical_sibling = os.path.realpath(sibling)
    script = _Script()
    script.distinct_ids = True
    _install(monkeypatch, script)
    settings = _Settings()
    await _run_workspace(settings, argv=("echo", "work"), cwd=str(sub), workspace_root=str(work))
    extra = await _run_workspace(settings, argv=("echo", "extra"), cwd=sibling, workspace_root=str(work))
    again = await _run_workspace(settings, argv=("echo", "again"), cwd=str(work), workspace_root=str(work))
    assert extra.exit_code == 0
    assert again.exit_code == 0
    assert _create_bodies(script) == [_mount_body(physical_work, "rw")]
    assert _exec_ids(script) == ["sb-1", "sb-1", "sb-1"]
    assert _exec_workdirs(script)[1] == physical_sibling
    assert backend._writable_by_root == {physical_work: "sb-1"}
    assert backend._readonly_id is None
    assert _mount_body(physical_sibling, "ro") not in _create_bodies(script)
    assert _SPAWN_CALLS == []


async def test_cancel_does_not_retarget_the_workspace_writable_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Cancel keeps the workspace sandbox. The next cwd does not replace that root."""
    work = tmp_path / "work"
    sub = work / "sub"
    sub.mkdir(parents=True)
    elsewhere = "/opt/chrys-sandbox-elsewhere"
    physical_work = _physical(work)
    physical_elsewhere = os.path.realpath(elsewhere)
    script = _Script()
    script.distinct_ids = True
    entered = asyncio.Event()
    release = asyncio.Event()
    transport = _PausingTransport(script, entered, release, pause_when=_is_exec)
    _install_transport(monkeypatch, transport)
    settings = _Settings()
    task = asyncio.create_task(_run_workspace(settings, argv=("echo", "one"), cwd=str(sub), workspace_root=str(work)))
    try:
        await wait_for(lambda: entered.is_set() or task.done(), description="exec is in flight")
        if task.done():
            await task
        assert entered.is_set()
        assert backend._writable_by_root == {physical_work: "sb-1"}
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await _cancel_pending(task)
    assert backend._writable_by_root == {physical_work: "sb-1"}
    followed = await _run_workspace(settings, argv=("echo", "two"), cwd=str(work), workspace_root=str(work))
    outside = await _run_workspace(settings, argv=("echo", "out"), cwd=elsewhere, workspace_root=str(work))
    assert followed.exit_code == 0
    assert outside.exit_code == 0
    assert _create_bodies(script) == [_mount_body(physical_work, "rw")]
    assert _exec_ids(script) == ["sb-1", "sb-1"]
    assert _exec_workdirs(script) == [_physical(work), physical_elsewhere]
    assert backend._writable_by_root == {physical_work: "sb-1"}
    assert backend._readonly_id is None
    assert _deleted(script) == []
    assert _mount_body(_physical(sub), "rw") not in _create_bodies(script)
    assert _mount_body(physical_elsewhere, "ro") not in _create_bodies(script)
    assert _SPAWN_CALLS == []


async def test_two_symlink_spellings_are_one_writable_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Two symlink spellings of one directory share one sandbox and one physical mount."""
    target = tmp_path / "project"
    sub = target / "sub"
    sub.mkdir(parents=True)
    link_a = tmp_path / "via-a"
    link_b = tmp_path / "via-b"
    symlink_or_skip(link_a, target, target_is_directory=True)
    symlink_or_skip(link_b, target, target_is_directory=True)
    physical = _physical(target)
    assert _physical(link_a) == physical
    assert _physical(link_b) == physical
    script = _Script()
    script.distinct_ids = True
    _install(monkeypatch, script)
    settings = _Settings()
    first = await _run_workspace(settings, argv=("echo", "a"), cwd=str(link_a / "sub"), workspace_root=str(link_a))
    second = await _run_workspace(settings, argv=("echo", "b"), cwd=str(link_b), workspace_root=str(link_b))
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert _create_bodies(script) == [_mount_body(physical, "rw")]
    assert _exec_ids(script) == ["sb-1", "sb-1"]
    assert _exec_workdirs(script) == [_physical(sub), physical]
    assert backend._writable_by_root == {physical: "sb-1"}
    assert _mount_body(_physical(sub), "rw") not in _create_bodies(script)
    assert _SPAWN_CALLS == []


async def test_two_outside_directories_share_the_workspace_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Outside directories are workdirs only. They do not create or replace a sandbox."""
    work = tmp_path / "work"
    sub = work / "sub"
    sub.mkdir(parents=True)
    out_a = "/opt/chrys-sandbox-out-a"
    out_b = "/opt/chrys-sandbox-out-b"
    physical_work = _physical(work)
    script = _Script()
    script.distinct_ids = True
    _install(monkeypatch, script)
    settings = _Settings()
    await _run_workspace(settings, argv=("echo", "work"), cwd=str(sub), workspace_root=str(work))
    await _run_workspace(settings, argv=("echo", "a"), cwd=out_a, workspace_root=str(work))
    switched = await _run_workspace(settings, argv=("echo", "b"), cwd=out_b, workspace_root=str(work))
    back = await _run_workspace(settings, argv=("echo", "back"), cwd=str(work), workspace_root=str(work))
    assert switched.exit_code == 0
    assert back.exit_code == 0
    assert _deleted(script) == []
    assert ("POST", "/api/v1/sandboxes/sb-1/stop") not in _paths(script)
    assert _create_bodies(script) == [_mount_body(physical_work, "rw")]
    assert _exec_ids(script) == ["sb-1", "sb-1", "sb-1", "sb-1"]
    assert _exec_workdirs(script)[1:3] == [os.path.realpath(out_a), os.path.realpath(out_b)]
    assert backend._writable_by_root == {physical_work: "sb-1"}
    assert backend._readonly_id is None
    assert _SPAWN_CALLS == []


async def test_extra_workspace_dir_is_not_treated_as_outside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A workspace dir outside the primary root is a read-write bind on the same sandbox."""
    work = tmp_path / "work"
    nested = work / "nested"
    nested.mkdir(parents=True)
    extra = tmp_path / "extra"
    pkg = extra / "pkg"
    pkg.mkdir(parents=True)
    sibling = "/opt/chrys-sandbox-sibling"
    physical_work = _physical(work)
    physical_extra = _physical(extra)
    physical_sibling = os.path.realpath(sibling)
    script = _Script()
    script.distinct_ids = True
    _install(monkeypatch, script)
    settings = _Settings()
    dirs = (str(extra), str(nested))
    in_extra = await _run_workspace(
        settings,
        argv=("echo", "extra"),
        cwd=str(pkg),
        workspace_root=str(work),
        workspace_dirs=dirs,
    )
    in_work = await _run_workspace(
        settings,
        argv=("echo", "work"),
        cwd=str(nested),
        workspace_root=str(work),
        workspace_dirs=dirs,
    )
    outside = await _run_workspace(
        settings,
        argv=("echo", "out"),
        cwd=sibling,
        workspace_root=str(work),
        workspace_dirs=dirs,
    )
    assert in_extra.exit_code == 0
    assert in_work.exit_code == 0
    assert outside.exit_code == 0
    assert _create_bodies(script) == [_mount_body_paths((physical_work, physical_extra), "rw")]
    assert _exec_ids(script) == ["sb-1", "sb-1", "sb-1"]
    assert _exec_workdirs(script) == [_physical(pkg), _physical(nested), physical_sibling]
    assert backend._writable_by_root == {physical_work: "sb-1"}
    assert backend._readonly_id is None
    assert _mount_body(physical_sibling, "ro") not in _create_bodies(script)
    assert _mount_body(_physical(pkg), "rw") not in _create_bodies(script)
    assert _mount_body(_physical(nested), "rw") not in _create_bodies(script)
    assert _SPAWN_CALLS == []


async def test_two_workspace_roots_do_not_share_one_writable_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Each physical workspace root has its own read-write sandbox id."""
    alpha = tmp_path / "alpha"
    sub = alpha / "sub"
    sub.mkdir(parents=True)
    beta = tmp_path / "beta"
    beta.mkdir()
    physical_alpha = _physical(alpha)
    physical_beta = _physical(beta)
    script = _Script()
    script.distinct_ids = True
    _install(monkeypatch, script)
    settings = _Settings()
    first = await _run_workspace(settings, argv=("echo", "a"), cwd=str(sub), workspace_root=str(alpha))
    second = await _run_workspace(settings, argv=("echo", "b"), cwd=str(beta), workspace_root=str(beta))
    third = await _run_workspace(settings, argv=("echo", "a2"), cwd=str(alpha), workspace_root=str(alpha))
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert third.exit_code == 0
    assert _create_bodies(script) == [
        _mount_body(physical_alpha, "rw"),
        _mount_body(physical_beta, "rw"),
    ]
    assert _exec_ids(script) == ["sb-1", "sb-2", "sb-1"]
    assert _exec_workdirs(script) == [str(sub), str(beta), str(alpha)]
    assert backend._writable_by_root == {physical_alpha: "sb-1", physical_beta: "sb-2"}
    assert _mount_body(_physical(sub), "rw") not in _create_bodies(script)
    assert _SPAWN_CALLS == []


async def test_workspace_404_drops_only_that_roots_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A 404 drops the missing workspace id and creates one replacement."""
    alpha = tmp_path / "alpha"
    sub = alpha / "sub"
    sub.mkdir(parents=True)
    beta = tmp_path / "beta"
    beta.mkdir()
    physical_alpha = _physical(alpha)
    physical_beta = _physical(beta)
    script = _Script()
    script.distinct_ids = True
    _install(monkeypatch, script)
    settings = _Settings()
    await _run_workspace(settings, argv=("echo", "a"), cwd=str(sub), workspace_root=str(alpha))
    await _run_workspace(settings, argv=("echo", "b"), cwd=str(beta), workspace_root=str(beta))
    script.not_found_gets = 1
    replaced = await _run_workspace(settings, argv=("echo", "a2"), cwd=str(sub), workspace_root=str(alpha))
    kept = await _run_workspace(settings, argv=("echo", "b2"), cwd=str(beta), workspace_root=str(beta))
    reused = await _run_workspace(settings, argv=("echo", "a3"), cwd=str(alpha), workspace_root=str(alpha))
    assert replaced.exit_code == 0
    assert kept.exit_code == 0
    assert reused.exit_code == 0
    assert _create_bodies(script) == [
        _mount_body(physical_alpha, "rw"),
        _mount_body(physical_beta, "rw"),
        _mount_body(physical_alpha, "rw"),
    ]
    assert _exec_ids(script) == ["sb-1", "sb-2", "sb-3", "sb-2", "sb-3"]
    assert backend._writable_by_root == {physical_alpha: "sb-3", physical_beta: "sb-2"}
    assert _SPAWN_CALLS == []


async def test_dotdot_spelling_and_directory_are_one_writable_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``work/pkg/..`` and ``work`` resolve to one physical root and one read-write mount."""
    work = tmp_path / "work"
    pkg = work / "pkg"
    pkg.mkdir(parents=True)
    via_parent = work / "pkg" / ".."
    physical = _physical(work)
    assert _physical(via_parent) == physical
    script = _Script()
    script.distinct_ids = True
    _install(monkeypatch, script)
    settings = _Settings()
    first = await _run_workspace(
        settings, argv=("echo", "via"), cwd=str(via_parent / "pkg"), workspace_root=str(via_parent)
    )
    second = await _run_workspace(settings, argv=("echo", "real"), cwd=str(work), workspace_root=str(work))
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert _create_bodies(script) == [_mount_body(physical, "rw")]
    assert _exec_ids(script) == ["sb-1", "sb-1"]
    assert _exec_workdirs(script) == [_physical(pkg), physical]
    assert backend._writable_by_root == {physical: "sb-1"}
    assert _mount_body(_physical(pkg), "rw") not in _create_bodies(script)
    assert _SPAWN_CALLS == []


def _deleted(script: _Script) -> list[str]:
    return [path for method, path in _paths(script) if method == "DELETE"]


async def test_adding_a_workspace_dir_creates_a_sandbox_that_mounts_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Same primary root, one more directory: the cached sandbox lacks that mount."""
    work = tmp_path / "work"
    work.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    physical_work = _physical(work)
    physical_extra = _physical(extra)
    script = _Script()
    script.distinct_ids = True
    _install(monkeypatch, script)
    settings = _Settings()
    await _run_workspace(settings, argv=("echo", "before"), cwd=str(work), workspace_root=str(work))
    added = await _run_workspace(
        settings,
        argv=("echo", "added"),
        cwd=str(work),
        workspace_root=str(work),
        workspace_dirs=(str(extra),),
    )
    in_extra = await _run_workspace(
        settings,
        argv=("echo", "extra"),
        cwd=str(extra),
        workspace_root=str(work),
        workspace_dirs=(str(extra),),
    )
    assert added.exit_code == 0
    assert in_extra.exit_code == 0
    assert _create_bodies(script) == [
        _mount_body(physical_work, "rw"),
        _mount_body_paths((physical_work, physical_extra), "rw"),
    ]
    assert _exec_ids(script) == ["sb-1", "sb-2", "sb-2"]
    assert _deleted(script) == ["/api/v1/sandboxes/sb-1"]
    assert backend._writable_by_root == {physical_work: "sb-2"}
    assert backend._writable_mounts == {physical_work: (physical_work, physical_extra)}
    assert _SPAWN_CALLS == []


async def test_removing_a_workspace_dir_stops_using_the_sandbox_that_mounts_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A removed directory is not writable through the sandbox that still mounts it."""
    work = tmp_path / "work"
    work.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    physical_work = _physical(work)
    physical_extra = _physical(extra)
    script = _Script()
    script.distinct_ids = True
    _install(monkeypatch, script)
    settings = _Settings()
    await _run_workspace(
        settings,
        argv=("echo", "both"),
        cwd=str(work),
        workspace_root=str(work),
        workspace_dirs=(str(extra),),
    )
    removed = await _run_workspace(settings, argv=("echo", "work"), cwd=str(work), workspace_root=str(work))
    protected = backend._path_is_protected(_physical(extra))
    in_extra: SandboxExec | None = None
    if protected:
        with pytest.raises(SandboxError, match="was not mounted"):
            await _run_workspace(settings, argv=("echo", "extra"), cwd=str(extra), workspace_root=str(work))
    else:
        in_extra = await _run_workspace(settings, argv=("echo", "extra"), cwd=str(extra), workspace_root=str(work))
        assert in_extra.exit_code == 0
    assert removed.exit_code == 0
    assert _create_bodies(script) == [
        _mount_body_paths((physical_work, physical_extra), "rw"),
        _mount_body(physical_work, "rw"),
    ]
    if in_extra is None:
        assert _exec_ids(script) == ["sb-1", "sb-2"]
    else:
        assert _exec_ids(script) == ["sb-1", "sb-2", "sb-2"]
        assert _exec_workdirs(script)[-1] == _physical(extra)
    assert _deleted(script) == ["/api/v1/sandboxes/sb-1"]
    assert backend._writable_by_root == {physical_work: "sb-2"}
    assert backend._writable_mounts == {physical_work: (physical_work,)}
    assert backend._readonly_id is None
    assert _mount_body(physical_extra, "ro") not in _create_bodies(script)
    assert _SPAWN_CALLS == []


async def test_superseded_workspace_sandbox_is_deleted_only_after_its_running_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Another session's new mount set does not delete the sandbox a command still runs in."""
    work = tmp_path / "work"
    work.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    script = _Script()
    script.distinct_ids = True
    entered = asyncio.Event()
    release = asyncio.Event()
    transport = _PausingTransport(script, entered, release, pause_when=_is_exec)
    _install_transport(monkeypatch, transport)
    settings = _Settings()
    task_a = asyncio.create_task(_run_workspace(settings, argv=("echo", "a"), cwd=str(work), workspace_root=str(work)))
    try:
        await wait_for(lambda: entered.is_set() or task_a.done(), description="first exec is in flight")
        if task_a.done():
            await task_a
        assert not task_a.done()
        second = await _run_workspace(
            settings,
            argv=("echo", "b"),
            cwd=str(work),
            workspace_root=str(work),
            workspace_dirs=(str(extra),),
        )
        assert second.exit_code == 0
        assert not task_a.done()
        assert _deleted(script) == []
        assert backend._retiring == {"sb-1"}
        release.set()
        first = await task_a
    finally:
        release.set()
        await _cancel_pending(task_a)
    assert first.exit_code == 0
    assert _exec_ids(script) == ["sb-2", "sb-1"]
    assert _deleted(script) == ["/api/v1/sandboxes/sb-1"]
    assert _paths(script).index(("DELETE", "/api/v1/sandboxes/sb-1")) > _paths(script).index(
        ("POST", "/api/v1/sandboxes/sb-1/exec")
    )
    assert backend._writable_by_root == {_physical(work): "sb-2"}
    assert backend._in_flight == {}
    assert backend._retiring == set()
    assert _SPAWN_CALLS == []


async def _supersede_while_first_exec_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_Script, asyncio.Task[SandboxExec], asyncio.Event]:
    """Park an exec in ``sb-1``, then finish a command whose mount set replaces it with ``sb-2``."""
    work = tmp_path / "work"
    work.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    script = _Script()
    script.distinct_ids = True
    entered = asyncio.Event()
    release = asyncio.Event()
    transport = _PausingTransport(script, entered, release, pause_when=_is_exec)
    _install_transport(monkeypatch, transport)
    settings = _Settings()
    task_a = asyncio.create_task(_run_workspace(settings, argv=("echo", "a"), cwd=str(work), workspace_root=str(work)))
    try:
        await wait_for(lambda: entered.is_set() or task_a.done(), description="first exec is in flight")
        if task_a.done():
            await task_a
        assert not task_a.done()
        second = await _run_workspace(
            settings,
            argv=("echo", "b"),
            cwd=str(work),
            workspace_root=str(work),
            workspace_dirs=(str(extra),),
        )
    except BaseException:
        release.set()
        await _cancel_pending(task_a)
        raise
    assert second.exit_code == 0
    assert backend._writable_by_root == {_physical(work): "sb-2"}
    assert _create_bodies(script) == [
        _mount_body(_physical(work), "rw"),
        _mount_body_paths((_physical(work), _physical(extra)), "rw"),
    ]
    assert _exec_ids(script) == ["sb-2"]
    return script, task_a, release


async def test_replacing_a_sandbox_waits_for_its_running_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The superseded sandbox is deleted after its last exec, not under it."""
    script, task_a, release = await _supersede_while_first_exec_runs(tmp_path, monkeypatch)
    try:
        assert not task_a.done()
        assert _deleted(script) == []
        assert backend._in_flight == {"sb-1": 1}
        assert backend._retiring == {"sb-1"}
        release.set()
        first = await task_a
    finally:
        release.set()
        await _cancel_pending(task_a)
    assert first.exit_code == 0
    assert _exec_ids(script) == ["sb-2", "sb-1"]
    assert _deleted(script) == ["/api/v1/sandboxes/sb-1"]
    assert _paths(script).index(("DELETE", "/api/v1/sandboxes/sb-1")) > _paths(script).index(
        ("POST", "/api/v1/sandboxes/sb-1/exec")
    )
    assert backend._in_flight == {}
    assert backend._retiring == set()
    assert _SPAWN_CALLS == []


async def test_cancelled_exec_in_a_replaced_sandbox_still_releases_and_deletes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Cancel ends the in-flight use. The replaced sandbox is deleted, and nothing is stopped."""
    script, task_a, release = await _supersede_while_first_exec_runs(tmp_path, monkeypatch)
    try:
        assert _deleted(script) == []
        task_a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task_a
    finally:
        release.set()
        await _cancel_pending(task_a)
    assert _deleted(script) == ["/api/v1/sandboxes/sb-1"]
    assert not any(path.endswith("/stop") for _method, path in _paths(script))
    assert _exec_ids(script) == ["sb-2"]
    assert backend._writable_by_root == {_physical(tmp_path / "work"): "sb-2"}
    assert backend._readonly_id is None
    assert backend._in_flight == {}
    assert backend._retiring == set()
    assert _SPAWN_CALLS == []


@pytest.mark.parametrize("inside_workspace", [True, False], ids=["inside", "outside"])
async def test_symlinked_cwd_execs_in_the_physical_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    inside_workspace: bool,
):
    """The exec workdir is the physical cwd. An outside alias does not become a mount."""
    work = tmp_path / "work"
    work.mkdir()
    target = work if inside_workspace else Path("/opt/chrys-sandbox-outside")
    alias = tmp_path / "alias"
    symlink_or_skip(alias, target, target_is_directory=True)
    physical_target = os.path.realpath(alias)
    assert str(alias) != physical_target
    script = _Script()
    _install(monkeypatch, script)
    result = await _run_workspace(_Settings(), cwd=str(alias), workspace_root=str(work))
    assert result.exit_code == 0
    assert _create_bodies(script) == [_mount_body(_physical(work), "rw")]
    assert _exec_workdirs(script) == [physical_target]
    assert _mount_body(physical_target, "ro") not in _create_bodies(script)
    assert _SPAWN_CALLS == []


_OUTSIDE_WORKDIR = "Sandbox command working directory is outside the workspace and was not mounted."


@pytest.mark.parametrize(
    "cwd",
    [
        "/tmp/chrys-sandbox-secret",
        "/var/tmp/chrys-sandbox-secret",
        "/var/run/chrys-sandbox-secret",
        "/run/chrys-sandbox-secret",
        "/root/chrys-sandbox-secret",
    ],
)
async def test_protected_outside_cwd_is_rejected_without_a_bind(
    monkeypatch: pytest.MonkeyPatch,
    cwd: str,
):
    """A cwd under a protected tree does not create a sandbox or send a bind."""
    script = _Script()
    _install(monkeypatch, script)
    with pytest.raises(SandboxError, match="was not mounted") as caught:
        await _run_workspace(_Settings(), cwd=cwd, workspace_root="/opt/chrys-sandbox-work")
    assert str(caught.value) == _OUTSIDE_WORKDIR
    assert cwd not in str(caught.value)
    assert _create_bodies(script) == []
    assert _exec_ids(script) == []
    assert backend._writable_by_root == {}
    assert _SPAWN_CALLS == []


async def test_account_home_outside_the_workspace_is_rejected(monkeypatch: pytest.MonkeyPatch):
    """The account home is protected when it is not inside a workspace mount."""
    home = str(Path.home())
    script = _Script()
    _install(monkeypatch, script)
    with pytest.raises(SandboxError) as caught:
        await _run_workspace(_Settings(), cwd=home, workspace_root="/opt/chrys-sandbox-work")
    assert str(caught.value) == _OUTSIDE_WORKDIR
    assert home not in str(caught.value)
    assert _create_bodies(script) == []


async def test_cwd_inside_a_protected_workspace_is_not_rejected(monkeypatch: pytest.MonkeyPatch):
    """Protection applies only outside the workspace mounts."""
    root = "/tmp/chrys-sandbox-project"
    cwd = "/tmp/chrys-sandbox-project/src"
    script = _Script()
    _install(monkeypatch, script)
    result = await _run_workspace(_Settings(), cwd=cwd, workspace_root=root)
    assert result.exit_code == 0
    assert _create_bodies(script) == [_mount_body(os.path.realpath(root), "rw")]
    assert _exec_workdirs(script) == [os.path.realpath(cwd)]
    assert backend._readonly_id is None


async def test_legacy_later_protected_cwd_does_not_bind_a_new_directory(monkeypatch: pytest.MonkeyPatch):
    """The first cwd is the writable root. A later protected cwd adds no bind."""
    script = _Script()
    _install(monkeypatch, script)
    settings = _Settings()
    first = await run_shell(
        settings, argv=("echo", "one"), cwd="/tmp/chrys-sandbox-project", timeout_seconds=12, linux=True
    )
    with pytest.raises(SandboxError) as caught:
        await run_shell(settings, argv=("echo", "two"), cwd="/tmp/chrys-sandbox-other", timeout_seconds=12, linux=True)
    assert str(caught.value) == _OUTSIDE_WORKDIR
    reused = await run_shell(
        settings, argv=("echo", "three"), cwd="/opt/chrys-sandbox-outside", timeout_seconds=12, linux=True
    )
    assert first.exit_code == 0
    assert reused.exit_code == 0
    assert _create_bodies(script) == [_mount_body("/tmp/chrys-sandbox-project", "rw")]
    assert _exec_workdirs(script) == ["/tmp/chrys-sandbox-project", "/opt/chrys-sandbox-outside"]
    assert backend._writable_root == "/tmp/chrys-sandbox-project"
    assert backend._readonly_id is None


async def test_realpath_into_a_protected_tree_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A symlink spelling is judged by realpath, so a link into /tmp is protected."""
    work = tmp_path / "work"
    work.mkdir()
    outside = "/opt/chrys-sandbox-alias"
    protected = os.path.realpath("/tmp/chrys-sandbox-secret")
    real_realpath = os.path.realpath

    def fake_realpath(path: str) -> str:
        if path == outside:
            return protected
        return real_realpath(path)

    monkeypatch.setattr(backend.os.path, "realpath", fake_realpath)
    script = _Script()
    _install(monkeypatch, script)
    with pytest.raises(SandboxError) as caught:
        await _run_workspace(_Settings(), cwd=outside, workspace_root=str(work))
    assert str(caught.value) == _OUTSIDE_WORKDIR
    assert "chrys-sandbox" not in str(caught.value)
    assert _create_bodies(script) == []


async def test_symlink_into_a_protected_tree_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A real symlink whose target is under /tmp is not mounted."""
    work = tmp_path / "work"
    work.mkdir()
    link = work / "escape"
    symlink_or_skip(link, Path("/tmp/chrys-sandbox-secret"), target_is_directory=True)
    script = _Script()
    _install(monkeypatch, script)
    with pytest.raises(SandboxError) as caught:
        await _run_workspace(_Settings(), cwd=str(link), workspace_root=str(work))
    assert str(caught.value) == _OUTSIDE_WORKDIR
    assert "chrys-sandbox-secret" not in str(caught.value)
    assert _create_bodies(script) == []


@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        (True, True),
        (False, False),
        ("true", False),
        (1, False),
        (None, False),
    ],
)
async def test_timed_out_is_only_json_true(monkeypatch: pytest.MonkeyPatch, flag: object, expected: bool):
    """Exit code 124 is a normal exit unless the exec JSON says timed_out true."""
    script = _Script()
    payload: dict[str, object] = {"exit_code": 124, "stdout": "out", "stderr": "err"}
    if flag is not None:
        payload["timed_out"] = flag
    script.exec_payload = payload
    result = await _run(script, monkeypatch)
    assert isinstance(result, SandboxExec)
    assert result.exit_code == 124
    assert result.stdout == "out"
    assert result.timed_out is expected


async def test_surrogate_path_becomes_sandbox_error_without_the_path(monkeypatch: pytest.MonkeyPatch):
    """A non-UTF-8 path cannot be sent. The error text does not include it."""
    script = _Script()
    cwd = "/work/\udcff"
    with pytest.raises(SandboxError) as caught:
        await _run(script, monkeypatch, cwd=cwd)
    assert str(caught.value) == "Sandbox path could not be encoded for the sandbox request."
    assert "\udcff" not in str(caught.value)
    assert _create_bodies(script) == []
    assert _SPAWN_CALLS == []


async def test_surrogate_workdir_on_exec_becomes_sandbox_error(monkeypatch: pytest.MonkeyPatch):
    """The same encoding failure on exec does not escape as UnicodeEncodeError."""
    script = _Script()
    await _run(script, monkeypatch, cwd="/work")
    with pytest.raises(SandboxError) as caught:
        await _run(script, monkeypatch, cwd="/work/\udcff")
    assert str(caught.value) == "Sandbox path could not be encoded for the sandbox request."
    assert "\udcff" not in str(caught.value)
    assert _exec_ids(script) == ["sb-1"]
