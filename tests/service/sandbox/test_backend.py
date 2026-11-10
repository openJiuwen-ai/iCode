# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fail-closed run_shell. HTTP is mocked; nothing listens and nothing is spawned."""

from __future__ import annotations

import ast
import asyncio
import builtins
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx
import pytest

from chrys.service.sandbox import backend
from chrys.service.sandbox.backend import (
    _HTTP_TIMEOUT_SLACK_SECONDS,
    _STOP_TIMEOUT_SECONDS,
    SandboxError,
    SandboxExec,
    run_shell,
)
from tests.support.waiting import wait_for

_SPAWN_CALLS: list[str] = []


@dataclass(frozen=True, slots=True)
class _Settings:
    """Stand-in with the three sandbox fields. Not the Settings class."""

    sandbox_enabled: bool = True
    sandbox_url: str = "http://sandbox.test"
    sandbox_token: str = ""


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


def _mount_body(cwd: str, mode: str) -> dict[str, object]:
    return {
        "policy_mode": "append",
        "policy": {
            "filesystem_policy": {
                "bind_mounts": [{"host_path": cwd, "sandbox_path": cwd, "mode": mode}],
            }
        },
    }


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
    backend._readonly_id = None
    backend._readonly_root = None
    yield
    backend._writable_root = None
    backend._readonly_id = None
    backend._readonly_root = None


@pytest.fixture(autouse=True)
def _forbid_supervisor_and_host_spawn(monkeypatch: pytest.MonkeyPatch):
    """A call into the supervisor or a managed child is a host-side path."""
    _SPAWN_CALLS.clear()

    async def forbid(*_args: object, **_kwargs: object) -> tuple[str, str]:
        _SPAWN_CALLS.append("ensure-started")
        raise SandboxError("supervisor was started")

    def forbid_managed(*_args: object, **_kwargs: object) -> object:
        _SPAWN_CALLS.append("managed-subprocess")
        raise AssertionError("managed subprocess")

    monkeypatch.setattr("chrys.service.sandbox.supervisor.ensure_started", forbid)
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
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def fake_started(*args: object, **kwargs: object) -> tuple[str, str]:
        calls.append((args, kwargs))
        return "http://started.sandbox", "generated-token"

    monkeypatch.setattr("chrys.service.sandbox.supervisor.ensure_started", fake_started)
    script = _Script()
    result = await _run(
        script,
        monkeypatch,
        _Settings(sandbox_url="", sandbox_token="bearer-token"),
    )
    assert calls == [((), {})]
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


async def test_readonly_cache_404_recreates_and_keeps_the_writable_sandbox(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    script.distinct_ids = True
    _install(monkeypatch, script)
    settings = _Settings()
    await run_shell(settings, argv=("echo", "rw"), cwd="/work/sub", timeout_seconds=12, linux=True)
    await run_shell(settings, argv=("echo", "ro"), cwd="/outside", timeout_seconds=12, linux=True)
    script.not_found_gets = 1
    replaced = await run_shell(settings, argv=("echo", "new"), cwd="/outside", timeout_seconds=12, linux=True)
    back = await run_shell(settings, argv=("echo", "back"), cwd="/work/sub", timeout_seconds=12, linux=True)
    assert replaced.exit_code == 0
    assert back.exit_code == 0
    assert _create_bodies(script) == [
        _mount_body("/work/sub", "rw"),
        _mount_body("/outside", "ro"),
        _mount_body("/outside", "ro"),
    ]
    assert _exec_ids(script) == ["sb-1", "sb-2", "sb-3", "sb-1"]
    assert backend._sandbox_id == "sb-1"
    assert backend._writable_root == "/work/sub"
    assert backend._readonly_id == "sb-3"
    assert backend._readonly_root == "/outside"
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


async def test_cwd_outside_the_writable_root_runs_read_only(monkeypatch: pytest.MonkeyPatch):
    """``/work`` is not inside the writable mount ``/work/sub``, so it runs read-only."""
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
    assert _create_bodies(script) == [_mount_body("/work/sub", "rw"), _mount_body("/work", "ro")]
    assert _exec_ids(script) == ["sb-1", "sb-1", "sb-2", "sb-1"]
    assert backend._sandbox_id == "sb-1"
    assert backend._writable_root == "/work/sub"
    assert backend._readonly_id == "sb-2"
    assert backend._readonly_root == "/work"
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
    assert _create_bodies(script) == [_mount_body("/work", "rw"), _mount_body("/work-extra", "ro")]
    assert _exec_ids(script) == ["sb-1", "sb-2", "sb-2", "sb-1"]
    assert backend._sandbox_id == "sb-1"
    assert backend._writable_root == "/work"
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
    """A paused exec releases the cache lock, so another cwd can create and exec."""
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
        assert _create_bodies(script) == [_mount_body("/work", "rw"), _mount_body("/outside", "ro")]
        assert _exec_ids(script) == ["sb-2"]
        release.set()
        first = await task_a
    finally:
        release.set()
        await _cancel_pending(task_a, task_b)
    assert first.exit_code == 0
    assert _exec_ids(script) == ["sb-2", "sb-1"]
    assert backend._sandbox_id == "sb-1"
    assert backend._readonly_id == "sb-2"
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
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await _cancel_pending(task)
    assert ("POST", "/api/v1/sandboxes/sb-1/stop") in _paths(script)
    stop_reads = [read for path, read in script.read_timeouts if path.endswith("/stop")]
    assert stop_reads == [_STOP_TIMEOUT_SECONDS]
    assert backend._sandbox_id is None
    assert backend._writable_root is None
    followed = await run_shell(settings, argv=("echo", "two"), cwd="/work", timeout_seconds=12, linux=True)
    assert followed.exit_code == 0
    assert _create_bodies(script) == [_mount_body("/work", "rw"), _mount_body("/work", "rw")]
    assert backend._sandbox_id == "sb-2"
    assert _SPAWN_CALLS == []


async def test_cancel_during_exec_still_cancels_when_stop_fails(monkeypatch: pytest.MonkeyPatch):
    script = _Script()
    entered = asyncio.Event()
    release = asyncio.Event()
    transport = _PausingTransport(script, entered, release, pause_when=_is_exec)
    _install_transport(monkeypatch, transport)
    task = asyncio.create_task(run_shell(_SECRET, argv=("echo",), cwd="/work", timeout_seconds=12, linux=True))
    try:
        await wait_for(lambda: entered.is_set() or task.done(), description="exec is in flight")
        if task.done():
            await task
        script.fail_path = "/api/v1/sandboxes/sb-1/stop"
        script.fail_error = httpx.ConnectError("down bearer-token http://user:userinfo-secret@sandbox.test")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await _cancel_pending(task)
    assert ("POST", "/api/v1/sandboxes/sb-1/stop") in _paths(script)
    assert backend._sandbox_id is None
    assert backend._writable_root is None
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
    assert backend._sandbox_id is None
    assert _SPAWN_CALLS == []


@pytest.mark.parametrize(
    ("error", "shown"),
    [
        ("bubblewrap is missing", "Sandbox did not become ready: bubblewrap is missing"),
        ("http://user:userinfo-secret@sandbox.test/nope", "Sandbox did not become ready."),
        ("line one\nline two", "Sandbox did not become ready."),
        ("x" * 201, "Sandbox did not become ready."),
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
