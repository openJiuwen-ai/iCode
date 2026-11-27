# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Supervisor unit tests. The bundled launcher is never executed and no socket is bound."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import os
import signal
import socket
import subprocess
import sys
import tomllib
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from chrys.service.sandbox import client, runtime_hold, supervisor
from chrys.service.sandbox.backend import SandboxError, run_shell
from chrys.service.sandbox.runtime_hold import RuntimeHold
from tests.support.waiting import wait_for, wait_until

_REAL_CHOOSE = supervisor._choose_listen_url
_REAL_POLICY = supervisor._bundled_policy_path
_REAL_SPAWN = supervisor._spawn_launcher
_REPO = Path(__file__).resolve().parents[3]
_SRC = _REPO / "src" / "chrys"
# Project dependencies that are ranges on purpose. Everything else stays an exact == pin.
_RANGE_PINS = frozenset(
    {
        "opentelemetry-api",
        "opentelemetry-sdk",
        "opentelemetry-exporter-otlp-proto-grpc",
        "opentelemetry-instrumentation-logging",
        "pyjwt",
    }
)
_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


@dataclass
class _FakeProcess:
    returncode: int | None = None
    stderr: str = ""
    stdout: str = ""
    pid: int | None = None

    async def startup_output(self) -> tuple[str, str]:
        return self.stderr, self.stdout

    async def request_exit(self) -> None:
        return None

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.fixture(autouse=True)
def _block_real_launcher(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(supervisor, "_choose_listen_url", lambda: "http://127.0.0.1:9")
    monkeypatch.setattr(supervisor, "_bundled_policy_path", lambda: "policy.yaml")

    @asynccontextmanager
    async def blocked(_env: dict[str, str]) -> AsyncIterator[_FakeProcess]:
        raise AssertionError("real launcher")
        yield _FakeProcess()

    monkeypatch.setattr(supervisor, "_spawn_launcher", blocked)


def _patch_spawn(monkeypatch: pytest.MonkeyPatch, proc: _FakeProcess) -> list[dict[str, str]]:
    seen: list[dict[str, str]] = []

    @asynccontextmanager
    async def spawn(env: dict[str, str]) -> AsyncIterator[_FakeProcess]:
        seen.append(dict(env))
        yield proc

    monkeypatch.setattr(supervisor, "_spawn_launcher", spawn)
    return seen


async def test_live_child_is_reused_until_it_exits(monkeypatch: pytest.MonkeyPatch):
    proc = _FakeProcess()
    seen = _patch_spawn(monkeypatch, proc)
    issued: list[str] = []

    def issue() -> str:
        token = f"fresh-{len(issued)}"
        issued.append(token)
        return token

    monkeypatch.setattr(supervisor.secrets, "token_urlsafe", issue)
    monkeypatch.setenv("CHRYS_SANDBOX_TOKEN", "user-settings-token")

    async def healthy(_base_url: str, _token: str) -> bool:
        return True

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    first_url, first_token = await supervisor.ensure_started()
    second_url, second_token = await supervisor.ensure_started()
    assert first_url == second_url == "http://127.0.0.1:9"
    assert first_token == second_token == "fresh-0"
    assert first_token != "user-settings-token"
    assert len(seen) == 1
    assert seen[0]["JIUWENBOX_LISTEN"] == "http://127.0.0.1:9"
    assert seen[0]["JIUWENBOX_POLICY_PATH"] == "policy.yaml"
    assert seen[0]["JIUWENBOX_API_TOKEN"] == "fresh-0"
    assert [key for key, value in seen[0].items() if value == "fresh-0"] == ["JIUWENBOX_API_TOKEN"]
    proc.returncode = 1
    replacement = _FakeProcess()

    @asynccontextmanager
    async def spawn_again(env: dict[str, str]) -> AsyncIterator[_FakeProcess]:
        seen.append(dict(env))
        yield replacement

    monkeypatch.setattr(supervisor, "_spawn_launcher", spawn_again)
    third_url, third_token = await supervisor.ensure_started()
    assert third_url == "http://127.0.0.1:9"
    assert third_token == "fresh-1"
    assert third_token != "user-settings-token"
    assert len(seen) == 2
    assert seen[1]["JIUWENBOX_API_TOKEN"] == "fresh-1"
    assert issued == ["fresh-0", "fresh-1"]


async def test_child_exit_before_health_is_sandbox_error(monkeypatch: pytest.MonkeyPatch):
    _patch_spawn(monkeypatch, _FakeProcess(returncode=3))
    monkeypatch.setattr(supervisor.secrets, "token_urlsafe", lambda: "fresh-secret")
    probed = False

    async def healthy(_base_url: str, _token: str) -> bool:
        nonlocal probed
        probed = True
        return True

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    with pytest.raises(SandboxError, match="exited") as caught:
        await supervisor.ensure_started()
    assert probed is False
    assert "fresh-secret" not in str(caught.value)


async def test_health_that_never_arrives_is_sandbox_error(monkeypatch: pytest.MonkeyPatch):
    _patch_spawn(monkeypatch, _FakeProcess())
    monkeypatch.setattr(supervisor.secrets, "token_urlsafe", lambda: "fresh-secret")

    async def unhealthy(_base_url: str, _token: str) -> bool:
        return False

    monkeypatch.setattr(supervisor, "_health_ok", unhealthy)
    with pytest.raises(SandboxError, match="did not become healthy") as caught:
        await supervisor.ensure_started(health_timeout=0)
    assert "fresh-secret" not in str(caught.value)


async def test_exit_before_health_quotes_one_stderr_line(monkeypatch: pytest.MonkeyPatch):
    _patch_spawn(
        monkeypatch,
        _FakeProcess(returncode=3, stderr="warning: ignored\nbwrap: setting up uid map: Permission denied\n"),
    )
    monkeypatch.setattr(supervisor.secrets, "token_urlsafe", lambda: "fresh-secret")

    async def healthy(_base_url: str, _token: str) -> bool:
        raise AssertionError("health probe")

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    with pytest.raises(SandboxError) as caught:
        await supervisor.ensure_started()
    assert str(caught.value) == (
        "Sandbox server exited before it became healthy. bwrap: setting up uid map: Permission denied"
    )
    assert "fresh-secret" not in str(caught.value)
    assert "warning" not in str(caught.value)


async def test_health_timeout_quotes_stderr_and_strips_the_token(monkeypatch: pytest.MonkeyPatch):
    _patch_spawn(
        monkeypatch,
        _FakeProcess(stderr="bwrap: failed fresh-secret for this user\n"),
    )
    monkeypatch.setattr(supervisor.secrets, "token_urlsafe", lambda: "fresh-secret")

    async def unhealthy(_base_url: str, _token: str) -> bool:
        return False

    monkeypatch.setattr(supervisor, "_health_ok", unhealthy)
    with pytest.raises(SandboxError) as caught:
        await supervisor.ensure_started(health_timeout=0)
    message = str(caught.value)
    assert message == "Sandbox server did not become healthy. bwrap: failed for this user"
    assert "fresh-secret" not in message


async def test_stdout_line_is_used_when_stderr_has_nothing_usable(monkeypatch: pytest.MonkeyPatch):
    _patch_spawn(
        monkeypatch,
        _FakeProcess(returncode=4, stderr="fresh-secret\n", stdout="launcher wrote: policy unreadable\n"),
    )
    monkeypatch.setattr(supervisor.secrets, "token_urlsafe", lambda: "fresh-secret")

    async def healthy(_base_url: str, _token: str) -> bool:
        raise AssertionError("health probe")

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    with pytest.raises(SandboxError) as caught:
        await supervisor.ensure_started()
    message = str(caught.value)
    assert message == "Sandbox server exited before it became healthy. launcher wrote: policy unreadable"
    assert "fresh-secret" not in message


async def test_stderr_line_wins_over_stdout(monkeypatch: pytest.MonkeyPatch):
    _patch_spawn(monkeypatch, _FakeProcess(returncode=4, stderr="from stderr\n", stdout="from stdout\n"))
    monkeypatch.setattr(supervisor.secrets, "token_urlsafe", lambda: "fresh-secret")

    async def healthy(_base_url: str, _token: str) -> bool:
        raise AssertionError("health probe")

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    with pytest.raises(SandboxError, match="from stderr") as caught:
        await supervisor.ensure_started()
    assert "from stdout" not in str(caught.value)
    assert "fresh-secret" not in str(caught.value)


def test_output_tail_keeps_the_last_bytes():
    tail = supervisor._OutputTail(8)
    tail.extend(b"0123456789")
    tail.extend(b"abcdef")
    assert tail.text() == "89abcdef"
    huge = supervisor._OutputTail(4)
    huge.extend(b"abcdefghij")
    assert huge.text() == "ghij"


async def test_pumped_stream_keeps_an_8kib_tail():
    reader = asyncio.StreamReader()
    marker = b"TAIL-MARK"
    reader.feed_data(b"a" * (supervisor._LAUNCHER_OUTPUT_BYTES + 50) + marker)
    reader.feed_eof()
    tail = supervisor._OutputTail(supervisor._LAUNCHER_OUTPUT_BYTES)
    await supervisor._pump_stream(reader, tail)
    text = tail.text()
    assert text == ("a" * (supervisor._LAUNCHER_OUTPUT_BYTES - len(marker))) + "TAIL-MARK"


async def test_real_spawn_reports_stderr_text_from_the_fake_child(monkeypatch: pytest.MonkeyPatch):
    """The real spawn path reads stderr text off the fake child and does not exec."""
    from chrys.foundation.platform import process as platform_process

    monkeypatch.setattr(supervisor.secrets, "token_urlsafe", lambda: "fresh-secret")
    seen: dict[str, object] = {}

    @asynccontextmanager
    async def fake_managed(*_args: object, **kwargs: object) -> AsyncIterator[_FakeProcess]:
        seen["stdin"] = kwargs.get("stdin")
        seen["stdout"] = kwargs.get("stdout")
        seen["stderr"] = kwargs.get("stderr")
        yield _FakeProcess(returncode=5, stderr="bwrap: No such file or directory\n")

    monkeypatch.setattr(platform_process, "managed_subprocess", fake_managed)
    monkeypatch.setattr(supervisor, "_spawn_launcher", _REAL_SPAWN)

    async def healthy(_base_url: str, _token: str) -> bool:
        raise AssertionError("health probe")

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    with pytest.raises(SandboxError) as caught:
        await supervisor.ensure_started()
    message = str(caught.value)
    assert message == "Sandbox server exited before it became healthy. bwrap: No such file or directory"
    assert "fresh-secret" not in message
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["stdout"] is subprocess.PIPE
    assert seen["stderr"] is subprocess.PIPE


async def test_startup_failure_keeps_one_short_line(monkeypatch: pytest.MonkeyPatch):
    detail = "bwrap: " + ("x" * 400) + " fresh-secret"
    _patch_spawn(monkeypatch, _FakeProcess(returncode=2, stderr=detail))
    monkeypatch.setattr(supervisor.secrets, "token_urlsafe", lambda: "fresh-secret")

    async def healthy(_base_url: str, _token: str) -> bool:
        raise AssertionError("health probe")

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    with pytest.raises(SandboxError) as caught:
        await supervisor.ensure_started()
    message = str(caught.value)
    prefix = "Sandbox server exited before it became healthy. "
    assert message.startswith(prefix + "bwrap: ")
    assert message.endswith("…")
    assert "fresh-secret" not in message
    assert "\n" not in message
    assert len(message) - len(prefix) == supervisor._FAILURE_LINE_CHARS


def test_listen_address_is_loopback_on_an_ephemeral_port(monkeypatch: pytest.MonkeyPatch):
    opened: list[_FakeSocket] = []

    class _FakeSocket:
        def __init__(self, family: int, sock_type: int, *_args: object, **_kwargs: object) -> None:
            self.family = family
            self.sock_type = sock_type
            self.bound: tuple[str, int] | None = None
            self.closed = False
            opened.append(self)

        def bind(self, address: tuple[str, int]) -> None:
            self.bound = address

        def getsockname(self) -> tuple[str, int]:
            return ("127.0.0.1", 43210)

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(supervisor.socket, "socket", _FakeSocket)
    assert _REAL_CHOOSE() == "http://127.0.0.1:43210"
    assert opened[0].family == socket.AF_INET
    assert opened[0].sock_type == socket.SOCK_STREAM
    assert opened[0].bound == ("127.0.0.1", 0)
    assert opened[0].closed is True


def test_listen_failure_is_sandbox_error(monkeypatch: pytest.MonkeyPatch):
    class _BadSocket:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.closed = False

        def bind(self, _address: tuple[str, int]) -> None:
            raise OSError("denied")

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(supervisor.socket, "socket", _BadSocket)
    with pytest.raises(SandboxError, match="local port"):
        _REAL_CHOOSE()


def test_child_env_sets_policy_listen_and_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", "inherited")
    env = supervisor._child_env("C:/policy.yaml", "http://127.0.0.1:9", "next-token")
    assert env["JIUWENBOX_POLICY_PATH"] == "C:/policy.yaml"
    assert env["JIUWENBOX_LISTEN"] == "http://127.0.0.1:9"
    assert env["JIUWENBOX_API_TOKEN"] == "next-token"

    empty = supervisor._child_env("C:/policy.yaml", "http://127.0.0.1:9", "")
    assert "JIUWENBOX_API_TOKEN" not in empty


def test_child_env_sets_the_engine_home_or_drops_an_inherited_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIUWENBOX_HOME", "/inherited/home")
    env = supervisor._child_env("policy.yaml", "http://127.0.0.1:9", "t", home="/own/home")
    assert env["JIUWENBOX_HOME"] == "/own/home"
    assert "JIUWENBOX_HOME" not in supervisor._child_env("policy.yaml", "http://127.0.0.1:9", "t")


async def test_each_child_gets_its_own_home_and_it_is_removed_after_the_childs_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.foundation.platform import get_platform

    homes: list[Path] = []
    modes: list[int] = []
    present_at_close: list[bool] = []

    @asynccontextmanager
    async def spawn(env: dict[str, str]) -> AsyncIterator[_FakeProcess]:
        home = Path(env["JIUWENBOX_HOME"])
        homes.append(home)
        modes.append(home.stat().st_mode & 0o777)
        (home / "sandboxes").mkdir()
        try:
            yield _FakeProcess()
        finally:
            present_at_close.append(home.is_dir())

    async def healthy(_base_url: str, _token: str) -> bool:
        return True

    monkeypatch.setattr(supervisor, "_spawn_launcher", spawn)
    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    await supervisor.ensure_started(network_mode="offline")
    await supervisor.ensure_started(network_mode="host")
    assert len(homes) == 2
    assert homes[0] != homes[1]
    root = get_platform().config_dir / "sandbox-engine"
    for home in homes:
        assert home.parent == root
        assert home.name.startswith(f"{supervisor._host_tag()}-{os.getpid()}-")
    if sys.platform != "win32":
        assert modes == [0o700, 0o700]
    assert present_at_close == [True]
    assert not homes[0].exists()
    assert homes[1].is_dir()
    await supervisor._shutdown()
    assert present_at_close == [True, True]
    assert not homes[1].exists()
    assert sorted(root.iterdir()) == []


_HOLD_LOCK = """
import sys
from chrys.foundation.util.lock import FileLock
with FileLock(sys.argv[1]):
    print("held", flush=True)
    sys.stdin.read()
"""


def test_a_start_removes_only_this_hosts_homes_whose_lock_is_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = supervisor._host_tag()
    root = tmp_path / "sandbox-engine"
    gone = root / f"{host}-4242-aa"
    # Its pid is not one this host knows, as for iCode in another pid namespace: the held lock keeps it.
    held = root / f"{host}-4243-bb"
    elsewhere = root / "0123456789ab-4244-cc"
    unrelated = root / "notes"
    for path in (gone, held, elsewhere, unrelated):
        (path / "sandboxes").mkdir(parents=True)
    (root / f"{gone.name}.lock").touch()
    orphan_lock = root / f"{host}-4245-dd.lock"
    orphan_lock.touch()
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path)}
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(root / f"{held.name}.lock")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
    )
    try:
        if holder.stdout is None or holder.stdout.readline().strip() != b"held":
            raise AssertionError("the lock holder did not start")
        monkeypatch.setattr(supervisor, "_engine_homes_root", lambda: root)
        created = Path(supervisor._new_engine_home())
        names = sorted(path.name for path in root.iterdir())
    finally:
        if holder.stdin is not None:
            holder.stdin.close()
        holder.wait(15)
        if holder.stdout is not None:
            holder.stdout.close()

    assert created.is_dir()
    assert names == sorted(
        [held.name, f"{held.name}.lock", elsewhere.name, unrelated.name, created.name, f"{created.name}.lock"]
    )
    supervisor._remove_engine_home_now(str(created))
    assert not created.exists()
    assert not (root / f"{created.name}.lock").exists()
    assert supervisor._engine_home_locks == {}


def test_network_mode_keeps_known_values_and_falls_back_to_guarded() -> None:
    assert [supervisor._network_mode(mode) for mode in ("offline", "guarded", "host")] == [
        "offline",
        "guarded",
        "host",
    ]
    assert supervisor._network_mode("") == "guarded"
    assert supervisor._network_mode("HOST") == "guarded"


async def test_ensure_started_exports_offline_network_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    proc = _FakeProcess()
    seen = _patch_spawn(monkeypatch, proc)
    monkeypatch.setattr(supervisor.secrets, "token_urlsafe", lambda: "fresh-secret")

    async def healthy(_base_url: str, _token: str) -> bool:
        return True

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    await supervisor._shutdown()
    try:
        await supervisor.ensure_started(network_mode="offline")
        assert seen[0]["JIUWENBOX_NETWORK_MODE"] == "offline"
    finally:
        proc.returncode = 1
        await supervisor._shutdown()


def _patch_recording_spawn(monkeypatch: pytest.MonkeyPatch, events: list[str]) -> list[dict[str, str]]:
    """Each start yields a new live child and records when its context is closed."""
    seen: list[dict[str, str]] = []

    @asynccontextmanager
    async def spawn(env: dict[str, str]) -> AsyncIterator[_FakeProcess]:
        index = len(seen)
        seen.append(dict(env))
        events.append(f"spawn:{index}")
        try:
            yield _FakeProcess()
        finally:
            events.append(f"close:{index}")

    monkeypatch.setattr(supervisor, "_spawn_launcher", spawn)
    return seen


@pytest.mark.parametrize(
    ("before", "after"),
    [("host", "offline"), ("guarded", "host"), ("offline", "guarded")],
)
async def test_network_mode_change_replaces_the_live_child(
    monkeypatch: pytest.MonkeyPatch,
    before: str,
    after: str,
) -> None:
    tokens = iter(["first-token", "second-token"])
    monkeypatch.setattr(supervisor.secrets, "token_urlsafe", lambda: next(tokens))
    events: list[str] = []
    seen = _patch_recording_spawn(monkeypatch, events)

    async def healthy(_base_url: str, _token: str) -> bool:
        return True

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    assert await supervisor.ensure_started(network_mode=before) == ("http://127.0.0.1:9", "first-token")
    assert await supervisor.ensure_started(network_mode=after) == ("http://127.0.0.1:9", "second-token")
    # Nothing leased the first child, so it stops before the next one starts.
    assert events == ["spawn:0", "close:0", "spawn:1"]
    assert seen[0].get("JIUWENBOX_NETWORK_MODE") == (None if before == "host" else before)
    assert seen[1].get("JIUWENBOX_NETWORK_MODE") == (None if after == "host" else after)
    assert seen[1]["JIUWENBOX_API_TOKEN"] == "second-token"
    running = supervisor._server
    assert running is not None
    assert running.network_mode == after


async def test_unchanged_network_mode_reuses_the_live_child(monkeypatch: pytest.MonkeyPatch) -> None:
    tokens = iter(["only-token"])
    monkeypatch.setattr(supervisor.secrets, "token_urlsafe", lambda: next(tokens))
    events: list[str] = []
    seen = _patch_recording_spawn(monkeypatch, events)

    async def healthy(_base_url: str, _token: str) -> bool:
        return True

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    assert await supervisor.ensure_started(network_mode="offline") == ("http://127.0.0.1:9", "only-token")
    for _ in range(2):
        async with supervisor.leased_server("offline") as leased:
            assert leased == ("http://127.0.0.1:9", "only-token")
    assert events == ["spawn:0"]
    assert len(seen) == 1
    assert seen[0]["JIUWENBOX_NETWORK_MODE"] == "offline"


@pytest.mark.parametrize("ending", ["finished", "cancelled"])
async def test_a_network_change_stops_the_old_child_only_after_its_running_command(
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
) -> None:
    tokens = iter(["first-token", "second-token"])
    monkeypatch.setattr(supervisor.secrets, "token_urlsafe", lambda: next(tokens))
    events: list[str] = []
    _patch_recording_spawn(monkeypatch, events)

    async def healthy(_base_url: str, _token: str) -> bool:
        return True

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def running_command() -> None:
        async with supervisor.leased_server("host") as (_url, token):
            assert token == "first-token"
            entered.set()
            await release.wait()

    holder = asyncio.create_task(running_command())
    try:
        await wait_for(lambda: entered.is_set() or holder.done(), description="first command holds its server")
        if holder.done():
            await holder
        async with supervisor.leased_server("offline") as (_url, token):
            assert token == "second-token"
        assert events == ["spawn:0", "spawn:1"]
        assert not holder.done()
        if ending == "finished":
            release.set()
            await holder
        else:
            holder.cancel()
            with pytest.raises(asyncio.CancelledError):
                await holder
        assert events == ["spawn:0", "spawn:1", "close:0"]
        assert supervisor._retired == []
        running = supervisor._server
        assert running is not None
        assert running.network_mode == "offline"
    finally:
        release.set()
        if not holder.done():
            holder.cancel()
        await asyncio.gather(holder, return_exceptions=True)


async def _hold_lease(network_mode: str, entered: asyncio.Event, release: asyncio.Event) -> None:
    async with supervisor.leased_server(network_mode):
        entered.set()
        await release.wait()


async def _start_lease_holder(network_mode: str) -> tuple[asyncio.Task[None], asyncio.Event]:
    entered = asyncio.Event()
    release = asyncio.Event()
    holder = asyncio.create_task(_hold_lease(network_mode, entered, release))
    await wait_for(lambda: entered.is_set() or holder.done(), description="command holds its server")
    if holder.done():
        await holder
    return holder, release


async def _settle(task: asyncio.Task[None]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_shutdown_stops_a_replaced_child_once_its_lease_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    """Process exit keeps a leased child alive until the lease ends, then closes it exactly once."""
    tokens = iter(["first-token", "second-token"])
    monkeypatch.setattr(supervisor.secrets, "token_urlsafe", lambda: next(tokens))
    events: list[str] = []
    _patch_recording_spawn(monkeypatch, events)

    async def healthy(_base_url: str, _token: str) -> bool:
        return True

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    holder, release = await _start_lease_holder("host")
    stopping: asyncio.Task[None] | None = None
    try:
        await supervisor.ensure_started(network_mode="offline")
        stopping = asyncio.create_task(supervisor._shutdown())
        # Shutdown stops the unleased current child and waits for the replaced one's lease.
        await wait_for(lambda: "close:1" in events or stopping.done(), description="unleased child stops")
        assert not stopping.done()
        assert events == ["spawn:0", "spawn:1", "close:1"]
        release.set()
        await holder
        await stopping
        assert events == ["spawn:0", "spawn:1", "close:1", "close:0"]
        assert supervisor._retired == []
        assert supervisor._server is None
    finally:
        release.set()
        await _settle(holder)
        if stopping is not None:
            await _settle(stopping)


async def test_shutdown_stops_a_still_leased_child_after_the_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lease that never ends cannot keep its child past process exit; its later end closes nothing."""
    events: list[str] = []
    _patch_recording_spawn(monkeypatch, events)

    async def healthy(_base_url: str, _token: str) -> bool:
        return True

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    monkeypatch.setattr(supervisor, "_LEASE_DRAIN_SECONDS", 0)
    holder, release = await _start_lease_holder("guarded")
    try:
        await supervisor._shutdown()
        assert events == ["spawn:0", "close:0"]
        assert supervisor._retired == []
        release.set()
        await holder
        assert events == ["spawn:0", "close:0"]
    finally:
        release.set()
        await _settle(holder)


async def test_a_cancelled_lease_release_still_stops_its_retired_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancelling a command while its release waits for the gate does not strand the lease."""
    tokens = iter(["first-token", "second-token"])
    monkeypatch.setattr(supervisor.secrets, "token_urlsafe", lambda: next(tokens))
    events: list[str] = []
    _patch_recording_spawn(monkeypatch, events)

    async def healthy(_base_url: str, _token: str) -> bool:
        return True

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    holder, release = await _start_lease_holder("host")
    gate = supervisor._gate()
    try:
        await supervisor.ensure_started(network_mode="offline")
        await gate.acquire()
        release.set()
        # The release is queued behind the held gate; a cold start holds it this way.
        await wait_for(lambda: bool(gate._waiters) or holder.done(), description="release waits for the gate")
        assert not holder.done()
        holder.cancel()
        await asyncio.sleep(0)
        assert not holder.done()
        gate.release()
        with pytest.raises(asyncio.CancelledError):
            await holder
        assert events == ["spawn:0", "spawn:1", "close:0"]
        assert supervisor._retired == []
    finally:
        if gate.locked():
            gate.release()
        release.set()
        await _settle(holder)


async def test_run_shell_after_a_network_change_recreates_the_sandbox_on_the_new_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The replaced server does not know the cached id: it answers 404 and the backend creates one."""
    tokens = iter(["first-token", "second-token"])
    monkeypatch.setattr(supervisor.secrets, "token_urlsafe", lambda: next(tokens))
    events: list[str] = []
    _patch_recording_spawn(monkeypatch, events)

    async def healthy(_base_url: str, _token: str) -> bool:
        return True

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    # Each started child is one server, told apart by the bearer token it was given.
    sandboxes: dict[str, set[str]] = {"first-token": set(), "second-token": set()}
    requests: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        server = request.headers["authorization"].removeprefix("Bearer ")
        owned = sandboxes[server]
        path = request.url.path
        requests.append((server, request.method, path))
        if path == "/health":
            return httpx.Response(200, json={"status": "ok", "features": ["hidden_host_paths"]})
        if request.method == "POST" and path == "/api/v1/sandboxes":
            sandbox_id = f"{server}-box{len(owned)}"
            owned.add(sandbox_id)
            return httpx.Response(200, json={"id": sandbox_id, "phase": "ready"})
        sandbox_id = path.removeprefix("/api/v1/sandboxes/").removesuffix("/exec")
        if sandbox_id not in owned:
            return httpx.Response(404, json={"error": "not found"})
        if path.endswith("/exec"):
            return httpx.Response(200, json={"exit_code": 0, "stdout": server, "stderr": ""})
        return httpx.Response(200, json={"id": sandbox_id, "phase": "ready"})

    def new_client(**kwargs: object) -> httpx.AsyncClient:
        built = dict(kwargs)
        built["transport"] = httpx.MockTransport(handler)
        return httpx.AsyncClient(**built)

    monkeypatch.setattr(client, "_new_client", new_client)

    class _Settings:
        sandbox_enabled = True
        sandbox_url = ""
        sandbox_token = "user-token"

        def __init__(self, sandbox_network: str) -> None:
            self.sandbox_network = sandbox_network

    async def run(settings: _Settings) -> str:
        completed = await run_shell(
            settings,
            argv=["echo"],
            cwd=str(tmp_path),
            timeout_seconds=5,
            linux=True,
            workspace_root=str(tmp_path),
        )
        assert completed.exit_code == 0
        return completed.stdout

    assert await run(_Settings("host")) == "first-token"
    assert await run(_Settings("host")) == "first-token"
    # A reloaded agent passes its new settings; the old server is unleased and stops first.
    assert await run(_Settings("offline")) == "second-token"
    assert events == ["spawn:0", "close:0", "spawn:1"]
    second_server = [(method, path) for server, method, path in requests if server == "second-token"]
    assert second_server == [
        ("GET", "/health"),
        ("GET", "/api/v1/sandboxes/first-token-box0"),
        ("POST", "/api/v1/sandboxes"),
        ("POST", "/api/v1/sandboxes/second-token-box0/exec"),
    ]


class _UrlSettings:
    sandbox_enabled = True
    sandbox_token = ""
    sandbox_network = "guarded"

    def __init__(self, sandbox_url: str) -> None:
        self.sandbox_url = sandbox_url


def _install_servers(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """Each configured host, and each bundled child (told apart by its token), knows only its own sandboxes."""
    sandboxes: dict[str, set[str]] = {}
    requests: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        server = request.headers["authorization"].removeprefix("Bearer ") if host == "127.0.0.1" else host
        owned = sandboxes.setdefault(server, set())
        path = request.url.path
        requests.append((server, request.method, path))
        if path == "/health":
            return httpx.Response(200, json={"status": "ok", "features": ["hidden_host_paths"]})
        if request.method == "POST" and path == "/api/v1/sandboxes":
            sandbox_id = f"{server}-box{len(owned)}"
            owned.add(sandbox_id)
            return httpx.Response(200, json={"id": sandbox_id, "phase": "ready"})
        sandbox_id = path.removeprefix("/api/v1/sandboxes/").removesuffix("/exec")
        if sandbox_id not in owned:
            return httpx.Response(404, json={"error": "not found"})
        if path.endswith("/exec"):
            return httpx.Response(200, json={"exit_code": 0, "stdout": sandbox_id, "stderr": ""})
        return httpx.Response(200, json={"id": sandbox_id, "phase": "ready"})

    def new_client(**kwargs: object) -> httpx.AsyncClient:
        built = dict(kwargs)
        built["transport"] = httpx.MockTransport(handler)
        return httpx.AsyncClient(**built)

    monkeypatch.setattr(client, "_new_client", new_client)
    return requests


async def _run_in(settings: _UrlSettings, workspace: Path) -> str:
    completed = await run_shell(
        settings,
        argv=["echo"],
        cwd=str(workspace),
        timeout_seconds=5,
        linux=True,
        workspace_root=str(workspace),
    )
    assert completed.exit_code == 0
    return completed.stdout


async def test_switching_sandbox_url_away_and_back_reuses_the_first_servers_sandbox(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requests = _install_servers(monkeypatch)
    assert await _run_in(_UrlSettings("http://a.test"), tmp_path) == "a.test-box0"
    assert await _run_in(_UrlSettings("http://b.test"), tmp_path) == "b.test-box0"
    assert await _run_in(_UrlSettings("http://a.test"), tmp_path) == "a.test-box0"
    creates = [server for server, method, path in requests if (method, path) == ("POST", "/api/v1/sandboxes")]
    assert creates == ["a.test", "b.test"]
    # Neither server is asked about the other's sandbox, and nothing is deleted.
    assert all(path.startswith(f"/api/v1/sandboxes/{server}-") for server, _method, path in requests if "-box" in path)
    assert not [request for request in requests if request[1] == "DELETE"]


async def test_the_last_runtime_release_deletes_the_sandboxes_on_each_configured_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(runtime_hold, "_runtimes", 0)
    monkeypatch.setattr(runtime_hold, "_external_used", False)
    requests = _install_servers(monkeypatch)
    first, last = RuntimeHold(), RuntimeHold()
    first.acquire()
    last.acquire()
    one, two = tmp_path / "one", tmp_path / "two"
    one.mkdir()
    two.mkdir()
    assert await _run_in(_UrlSettings("http://a.test"), one) == "a.test-box0"
    assert await _run_in(_UrlSettings("http://a.test"), two) == "a.test-box1"
    assert await _run_in(_UrlSettings("http://b.test"), one) == "b.test-box0"

    def deletes() -> list[tuple[str, str]]:
        return sorted((server, path) for server, method, path in requests if method == "DELETE")

    await first.release()
    assert deletes() == []
    await last.release()
    assert deletes() == [
        ("a.test", "/api/v1/sandboxes/a.test-box0"),
        ("a.test", "/api/v1/sandboxes/a.test-box1"),
        ("b.test", "/api/v1/sandboxes/b.test-box0"),
    ]
    # Nothing deleted stays cached: the next command creates a new sandbox.
    assert await _run_in(_UrlSettings("http://a.test"), one) == "a.test-box2"


async def test_a_configured_url_retires_the_bundled_server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    events: list[str] = []
    _patch_recording_spawn(monkeypatch, events)

    async def healthy(_base_url: str, _token: str) -> bool:
        return True

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    _install_servers(monkeypatch)
    holder, release = await _start_lease_holder("guarded")
    try:
        assert await _run_in(_UrlSettings("http://a.test"), tmp_path) == "a.test-box0"
        # The bundled server a command still leases stops when that lease ends.
        assert events == ["spawn:0"]
        assert supervisor._server is None
        release.set()
        await holder
        assert events == ["spawn:0", "close:0"]
        await supervisor.ensure_started()
        assert events == ["spawn:0", "close:0", "spawn:1"]
        await _run_in(_UrlSettings("http://a.test"), tmp_path)
        assert events == ["spawn:0", "close:0", "spawn:1", "close:1"]
    finally:
        release.set()
        await _settle(holder)


def test_child_env_offline_sets_network_mode_and_host_clears_it(monkeypatch: pytest.MonkeyPatch) -> None:
    offline = supervisor._child_env(
        "C:/policy.yaml",
        "http://127.0.0.1:9",
        "next-token",
        network_mode="offline",
    )
    assert offline["JIUWENBOX_NETWORK_MODE"] == "offline"

    monkeypatch.setenv("JIUWENBOX_NETWORK_MODE", "offline")
    host = supervisor._child_env("C:/policy.yaml", "http://127.0.0.1:9", "next-token")
    assert "JIUWENBOX_NETWORK_MODE" not in host


def test_launcher_argv_detaches_stdin_and_captures_output():
    argv = supervisor._launcher_argv()
    assert argv == [sys.executable, "-P", "-m", "jiuwenbox.server.launcher"]
    assert "-I" not in argv
    assert supervisor._launcher_cwd() == sys.prefix
    source = (_SRC / "service" / "sandbox" / "supervisor.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    spawn = next(
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "_spawn_launcher"
    )
    text = ast.get_source_segment(source, spawn)
    assert text is not None
    assert "managed_subprocess" in text
    assert "stdin=subprocess.DEVNULL" in text
    assert "stdout=subprocess.PIPE" in text
    assert "stderr=subprocess.PIPE" in text
    assert "stdout=subprocess.DEVNULL" not in text
    assert "stderr=subprocess.DEVNULL" not in text
    assert "cwd=_launcher_cwd()" in text


def test_bundled_policy_path_is_the_vendored_code_agent_file():
    path = _REAL_POLICY()
    assert os.path.isfile(path)
    assert os.path.basename(path) == "code-agent-policy.yaml"


async def test_health_probe_requires_status_ok(monkeypatch: pytest.MonkeyPatch):
    seen: list[str | None] = []
    trust_env: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        if len(seen) == 1:
            return httpx.Response(200, json={"status": "ok", "features": ["hidden_host_paths"]})
        return httpx.Response(200, json={"status": "down"})

    def new_client(**kwargs: object) -> httpx.AsyncClient:
        trust_env.append(kwargs["trust_env"])
        built = dict(kwargs)
        built["transport"] = httpx.MockTransport(handler)
        return httpx.AsyncClient(**built)

    monkeypatch.setattr(client, "_new_client", new_client)
    assert await supervisor._health_ok("http://sandbox.test", "sekret") is True
    assert await supervisor._health_ok("http://sandbox.test", "") is False
    assert seen == ["Bearer sekret", None]
    assert len(trust_env) == 2
    assert all(value is False for value in trust_env)


async def test_non_linux_does_not_spawn_the_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """``run_shell(linux=False)`` refuses before the supervisor is asked to spawn."""
    spawned = _patch_spawn(monkeypatch, _FakeProcess())

    async def health_must_not_run(_base_url: str, _token: str) -> bool:
        raise AssertionError(f"launcher spawned: {spawned}")

    monkeypatch.setattr(supervisor, "_health_ok", health_must_not_run)

    class _Settings:
        sandbox_enabled = True
        sandbox_url = ""
        sandbox_token = "sekret"
        sandbox_network = "guarded"

    with pytest.raises(SandboxError, match="only on Linux"):
        await run_shell(
            _Settings(),
            argv=["echo"],
            cwd=".",
            timeout_seconds=5,
            linux=False,
        )
    assert spawned == []


async def test_shutdown_close_does_not_raise_async_generator_already_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing the retained child goes through the real stack close and must not raise."""
    from chrys.foundation.platform import process as platform_process

    proc = _FakeProcess()
    entered = False

    @asynccontextmanager
    async def fake_managed(*args: object, **kwargs: object) -> AsyncIterator[_FakeProcess]:
        nonlocal entered
        entered = True
        assert args == tuple(supervisor._launcher_argv())
        assert "-P" in args
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE
        assert kwargs["cwd"] == sys.prefix
        yield proc

    monkeypatch.setattr(platform_process, "managed_subprocess", fake_managed)
    monkeypatch.setattr(supervisor, "_spawn_launcher", _REAL_SPAWN)

    async def healthy(_base_url: str, _token: str) -> bool:
        return True

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    await supervisor.ensure_started()
    await supervisor._shutdown()
    assert entered is True
    assert proc.returncode is None


_SIGNAL_RECORD = "SANDBOX_LAUNCHER_SIGNAL_PATH"
_CATCHES_SIGTERM = """\
import os
import signal
import time
from pathlib import Path

path = Path(os.environ["SANDBOX_LAUNCHER_SIGNAL_PATH"])

def stop(signum, _frame):
    path.write_text(str(signum), encoding="utf-8")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
path.write_text("ready", encoding="utf-8")
while True:
    time.sleep(3600)
"""
_IGNORES_SIGTERM = """\
import os
import signal
import time
from pathlib import Path

path = Path(os.environ["SANDBOX_LAUNCHER_SIGNAL_PATH"])
signal.signal(signal.SIGTERM, signal.SIG_IGN)
path.write_text("ready", encoding="utf-8")
time.sleep(3600)
"""


async def _start_signal_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    script: str,
) -> tuple[Path, supervisor._RunningServer]:
    marker = tmp_path / "signal.txt"
    monkeypatch.setenv(_SIGNAL_RECORD, str(marker))
    monkeypatch.setattr(supervisor, "_launcher_argv", lambda: [sys.executable, "-c", script])
    monkeypatch.setattr(supervisor, "_spawn_launcher", _REAL_SPAWN)

    async def healthy(_base_url: str, _token: str) -> bool:
        return True

    monkeypatch.setattr(supervisor, "_health_ok", healthy)
    await supervisor.ensure_started()
    running = supervisor._server
    assert running is not None
    await wait_for(
        lambda: marker.is_file() and marker.read_text(encoding="utf-8") == "ready",
        description="launcher child is ready",
    )
    return marker, running


def _assert_stopped_by_sigterm(marker: Path, returncode: int | None) -> None:
    recorded = marker.read_text(encoding="utf-8")
    if recorded == str(signal.SIGTERM):
        assert returncode == 0
        return
    # Windows ends the process with TerminateProcess and does not enter the handler.
    assert sys.platform == "win32"
    assert recorded == "ready"
    assert returncode == signal.SIGTERM


async def test_stop_delivers_sigterm_before_the_kill_backstop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A child that exits on SIGTERM is not killed again when the stack closes."""
    marker, running = await _start_signal_child(monkeypatch, tmp_path, _CATCHES_SIGTERM)
    try:
        await supervisor.shutdown()
        _assert_stopped_by_sigterm(marker, running.proc.returncode)
    finally:
        await running.closer.aclose()


async def test_shutdown_leaves_a_leased_server_until_the_lease_ends(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Process exit must not signal a server a command still holds before that lease ends."""
    marker, running = await _start_signal_child(monkeypatch, tmp_path, _CATCHES_SIGTERM)
    holder, release = await _start_lease_holder("guarded")
    stopping = asyncio.create_task(supervisor.shutdown())
    try:
        assert not await wait_until(stopping.done, timeout=0.2)
        assert running.proc.returncode is None
        assert marker.read_text(encoding="utf-8") == "ready"
        release.set()
        await holder
        await stopping
        _assert_stopped_by_sigterm(marker, running.proc.returncode)
    finally:
        release.set()
        await _settle(holder)
        await _settle(stopping)
        await running.closer.aclose()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows cannot ignore SIGTERM")
async def test_ignored_sigterm_is_killed_within_the_grace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The grace wait is the bound. When it ends, the close still SIGKILLs the child."""
    marker, running = await _start_signal_child(monkeypatch, tmp_path, _IGNORES_SIGTERM)

    async def wait_for_exit(awaitable: Awaitable[int], timeout: float) -> int:
        assert timeout == supervisor._STOP_GRACE_SECONDS
        return await asyncio.wait_for(awaitable, 0)

    shadow = ModuleType("asyncio")
    shadow.__dict__.update(vars(asyncio), wait_for=wait_for_exit)
    monkeypatch.setattr(supervisor, "asyncio", shadow)
    try:
        await supervisor.shutdown()
        assert marker.read_text(encoding="utf-8") == "ready"
        assert running.proc.returncode == -signal.SIGKILL
    finally:
        await running.closer.aclose()


@pytest.mark.skipif(sys.platform != "linux", reason="the bundled sandbox server runs on Linux only")
async def test_cancelling_the_stop_still_kills_a_child_that_ignores_sigterm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A second cancel waits out the grace and still reaches the kill backstop."""
    marker, running = await _start_signal_child(monkeypatch, tmp_path, _IGNORES_SIGTERM)
    grace_entered = asyncio.Event()
    release_grace = asyncio.Event()

    async def wait_for_exit(awaitable: Awaitable[int], timeout: float) -> int:
        assert timeout == supervisor._STOP_GRACE_SECONDS
        grace_entered.set()
        await release_grace.wait()
        return await asyncio.wait_for(awaitable, 0)

    shadow = ModuleType("asyncio")
    shadow.__dict__.update(vars(asyncio), wait_for=wait_for_exit)
    monkeypatch.setattr(supervisor, "asyncio", shadow)
    task = asyncio.create_task(supervisor.shutdown())
    try:
        await wait_for(
            lambda: grace_entered.is_set() or task.done(),
            description="grace wait is in progress",
        )
        assert not task.done()
        task.cancel()
        release_grace.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert marker.read_text(encoding="utf-8") == "ready"
        assert running.proc.returncode == -signal.SIGKILL
    finally:
        release_grace.set()
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await running.closer.aclose()


async def test_bundled_server_sends_a_fresh_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Argv, cwd, child env, and the health client share one fresh token."""
    from chrys.foundation.platform import process as platform_process

    monkeypatch.setenv("CHRYS_SANDBOX_TOKEN", "user-settings-token")
    monkeypatch.setenv("JIUWENBOX_API_TOKEN", "inherited-token")
    monkeypatch.setattr(supervisor.secrets, "token_urlsafe", lambda: "fresh-server-token")
    captured: dict[str, object] = {}
    headers: list[str | None] = []
    trust_env: list[object] = []

    @asynccontextmanager
    async def fake_managed(*args: object, **kwargs: object) -> AsyncIterator[_FakeProcess]:
        captured["args"] = args
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        captured["stdin"] = kwargs.get("stdin")
        captured["stdout"] = kwargs.get("stdout")
        captured["stderr"] = kwargs.get("stderr")
        yield _FakeProcess()

    monkeypatch.setattr(platform_process, "managed_subprocess", fake_managed)
    monkeypatch.setattr(supervisor, "_spawn_launcher", _REAL_SPAWN)

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"status": "ok", "features": ["hidden_host_paths"]})

    def new_client(**kwargs: object) -> httpx.AsyncClient:
        trust_env.append(kwargs["trust_env"])
        built = dict(kwargs)
        built["transport"] = httpx.MockTransport(handler)
        return httpx.AsyncClient(**built)

    monkeypatch.setattr(client, "_new_client", new_client)
    url, token = await supervisor.ensure_started()
    assert url == "http://127.0.0.1:9"
    assert token == "fresh-server-token"
    assert token not in url
    assert token != "user-settings-token"
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["JIUWENBOX_API_TOKEN"] == "fresh-server-token"
    assert env["JIUWENBOX_API_TOKEN"] != "inherited-token"
    assert [key for key, value in env.items() if value == "fresh-server-token"] == ["JIUWENBOX_API_TOKEN"]
    assert captured["args"] == (sys.executable, "-P", "-m", "jiuwenbox.server.launcher")
    assert captured["cwd"] == sys.prefix
    assert captured["cwd"] != os.getcwd()
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.PIPE
    assert headers == ["Bearer fresh-server-token"]
    assert len(trust_env) == 1
    assert trust_env[0] is False
    running = supervisor._server
    assert running is not None
    assert token not in repr(running)


async def test_run_shell_disables_trust_env_only_for_the_bundled_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty ``sandbox_url`` connects with ``trust_env=False``.

    A configured URL does not. The kwargs are those ``connect`` passes to the
    HTTP client constructor.
    """
    trust_env: list[object] = []
    hosts: list[str] = []
    started_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        if request.url.path != "/health":
            raise AssertionError(request.url.path)
        return httpx.Response(200, json={"status": "down"})

    def new_client(**kwargs: object) -> httpx.AsyncClient:
        trust_env.append(kwargs["trust_env"])
        built = dict(kwargs)
        built["transport"] = httpx.MockTransport(handler)
        return httpx.AsyncClient(**built)

    @asynccontextmanager
    async def started(network_mode: str) -> AsyncIterator[tuple[str, str]]:
        nonlocal started_calls
        assert network_mode == "guarded"
        started_calls += 1
        yield "http://started.sandbox", "generated-token"

    monkeypatch.setattr(client, "_new_client", new_client)
    monkeypatch.setattr(supervisor, "leased_server", started)

    class _Settings:
        def __init__(self, sandbox_url: str) -> None:
            self.sandbox_enabled = True
            self.sandbox_url = sandbox_url
            self.sandbox_token = "user-token"
            self.sandbox_network = "guarded"

    with pytest.raises(SandboxError, match="health is not ok"):
        await run_shell(
            _Settings(""),
            argv=["echo"],
            cwd="/work",
            timeout_seconds=5,
            linux=True,
        )
    assert started_calls == 1
    assert hosts == ["started.sandbox"]
    assert len(trust_env) == 1
    assert trust_env[0] is False

    trust_env.clear()
    hosts.clear()
    with pytest.raises(SandboxError, match="health is not ok"):
        await run_shell(
            _Settings("http://sandbox.test"),
            argv=["echo"],
            cwd="/work",
            timeout_seconds=5,
            linux=True,
        )
    assert started_calls == 1
    assert hosts == ["sandbox.test"]
    assert len(trust_env) == 1
    assert trust_env[0] is True


def test_jiuwenbox_appears_only_inside_supervisor_functions() -> None:
    allowed = (_SRC / "service" / "sandbox" / "supervisor.py").resolve()
    assert _boundary_violations(_SRC, allowed_file=allowed) == []


def test_import_boundary_scanner_rejects_a_planted_snippet(tmp_path: Path) -> None:
    allowed = tmp_path / "service" / "sandbox" / "supervisor.py"
    allowed.parent.mkdir(parents=True)
    allowed.write_text(
        "import jiuwenbox\nLISTENER = 'http://jiuwenbox'\ndef nested() -> str:\n    return 'jiuwenswarm'\n",
        encoding="utf-8",
    )
    elsewhere = tmp_path / "service" / "other.py"
    elsewhere.write_text(
        '"""Aligned with the JiuwenSwarm web contract."""\n'
        "# JiuwenSwarm is mentioned in a comment.\n"
        "import jiuwenswarm.agent\n"
        "class Status:\n"
        '    """Also aligned with JiuwenSwarm."""\n'
        "def home() -> str:\n"
        '    """JiuwenSwarm keeps state under the home directory."""\n'
        "    from jiuwenswarm import paths\n"
        "    return '~/.JiuwenSwarm/state'\n",
        encoding="utf-8",
    )
    (tmp_path / "policy.yaml").write_text("bind: ~/.jiuwenswarm\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("Ported from JiuwenSwarm.\n", encoding="utf-8")
    violations = _boundary_violations(tmp_path, allowed_file=allowed)
    assert sorted(violations) == [
        "policy.yaml:1:jiuwenswarm-data:in_function=False",
        "service/other.py:3:jiuwenswarm-import:in_function=False",
        "service/other.py:8:jiuwenswarm-import:in_function=True",
        "service/other.py:9:jiuwenswarm-string:in_function=True",
        "service/sandbox/supervisor.py:1:jiuwenbox-import:in_function=False",
        "service/sandbox/supervisor.py:2:jiuwenbox-string:in_function=False",
        "service/sandbox/supervisor.py:4:jiuwenswarm-string:in_function=True",
    ]


def test_sandbox_package_import_does_not_load_jiuwenbox_fastapi_or_bwrap() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _IMPORT_DELTA_PROBE],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_jiuwenbox_is_vendored_and_other_pins_stay_exact() -> None:
    project = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    parsed = dict(_dependency_name_and_specifier(item) for item in project["project"]["dependencies"])
    # The engine is copied into the wheel. A published Requires-Dist would
    # send pip to PyPI for a distribution that is not there.
    assert "jiuwenbox" not in parsed
    assert "jiuwenbox" not in project["tool"]["uv"].get("sources", {})
    assert parsed["fastapi"] == "==0.162.0"
    assert parsed["uvicorn"] == "==0.51.0"
    # No [standard] extras: uvloop would take SIGCHLD from the engine's zombie reaper.
    assert "uvicorn==0.51.0" in project["project"]["dependencies"]
    loosened = sorted(
        name for name, specifier in parsed.items() if name not in _RANGE_PINS and not _is_exact_pin(specifier)
    )
    assert loosened == []
    unbounded = sorted(name for name, specifier in parsed.items() if name in _RANGE_PINS and "<" not in specifier)
    assert unbounded == []

    lock = tomllib.loads((_REPO / "uv.lock").read_text(encoding="utf-8"))
    assert [package for package in lock["package"] if package["name"] == "jiuwenbox"] == []
    requires = next(package for package in lock["package"] if package["name"] == "icode-tui")
    by_name = {item["name"]: item for item in requires["metadata"]["requires-dist"]}
    assert "jiuwenbox" not in by_name
    for name, specifier in parsed.items():
        assert by_name[name]["specifier"] == specifier


def test_pytest_does_not_collect_third_party_jiuwenbox() -> None:
    project = tomllib.loads((_REPO / "pyproject.toml").read_text(encoding="utf-8"))
    options = project["tool"]["pytest"]["ini_options"]
    normalized = [str(path).replace("\\", "/").strip("/") for path in options["testpaths"]]
    assert set(normalized) <= {"src", "tests"}
    assert "tests" in normalized
    assert all("third_party" not in path for path in normalized)
    assert isinstance(options["norecursedirs"], list)


def test_engine_import_is_inside_a_function_and_import_does_not_load_it():
    mentions = _engine_mentions()
    assert mentions["top_level_imports"] == []
    assert mentions["module_strings"] == []
    nested = mentions["nested_imports"]
    assert isinstance(nested, list) and nested
    assert all(name == "jiuwenbox" for _depth, _line, name in nested)
    assert mentions["other_files"] == []
    completed = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


_IMPORT_PROBE = """
import sys
import chrys.service.sandbox
import chrys.service.sandbox.types
import chrys.service.sandbox.client
import chrys.service.sandbox.backend
import chrys.service.sandbox.supervisor
banned = [
    name
    for name in sys.modules
    if name == "jiuwenbox"
    or name.startswith("jiuwenbox.")
    or name == "fastapi"
    or name.startswith("fastapi.")
]
if banned:
    sys.stderr.write("\\n".join(sorted(banned)))
    raise SystemExit(1)
"""


def _engine_mentions() -> dict[str, object]:
    supervisor_path = _SRC / "service" / "sandbox" / "supervisor.py"
    tree = ast.parse(supervisor_path.read_text(encoding="utf-8"))
    imports: list[tuple[int, int, str]] = []
    module_strings: list[int] = []
    _walk(tree, 0, imports, module_strings)
    other: list[tuple[str, list[str]]] = []
    for path in _SRC.rglob("*.py"):
        if path.resolve() == supervisor_path.resolve():
            continue
        text = path.read_text(encoding="utf-8").lower()
        problems: list[str] = []
        if "jiuwenbox" in text:
            problems.append(path.relative_to(_SRC).as_posix())
        other.append((path.as_posix(), problems))
    return {
        "top_level_imports": [item for item in imports if item[0] == 0],
        "nested_imports": [item for item in imports if item[0] > 0],
        "module_strings": module_strings,
        "other_files": [(path, problems) for path, problems in other if problems],
    }


def _walk(
    node: ast.AST,
    depth: int,
    imports: list[tuple[int, int, str]],
    module_strings: list[int],
) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "jiuwenbox" or alias.name.startswith("jiuwenbox."):
                imports.append((depth, node.lineno, alias.name))
    elif isinstance(node, ast.ImportFrom) and node.module:
        if node.module == "jiuwenbox" or node.module.startswith("jiuwenbox."):
            imports.append((depth, node.lineno, node.module))
    elif (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "jiuwenbox" in node.value.lower()
        and depth == 0
    ):
        module_strings.append(node.lineno)
    child_depth = (
        depth + 1 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)) else depth
    )
    for child in ast.iter_child_nodes(node):
        _walk(child, child_depth, imports, module_strings)


_IMPORT_DELTA_PROBE = """
import sys
before = set(sys.modules)
import chrys.service.sandbox
import chrys.service.sandbox.types
import chrys.service.sandbox.client
import chrys.service.sandbox.backend
import chrys.service.sandbox.supervisor
added = set(sys.modules) - before
banned = [
    name
    for name in added
    if name == "jiuwenbox"
    or name.startswith("jiuwenbox.")
    or name == "fastapi"
    or name.startswith("fastapi.")
    or "bwrap" in name
]
if banned:
    sys.stderr.write("\\n".join(sorted(banned)))
    raise SystemExit(1)
"""


@dataclass(frozen=True)
class _BoundaryHit:
    path: Path
    line: int
    kind: str
    in_function: bool


def _boundary_violations(root: Path, *, allowed_file: Path) -> list[str]:
    """jiuwenbox strings and imports may live only inside functions of ``allowed_file``.

    Nothing may depend on jiuwenswarm, the product the engine was taken from:
    no import, no string value (a path, env name or module name) and no data
    file that names it. Docstrings, comments and Markdown may mention it.
    """
    allowed = allowed_file.resolve()
    root_resolved = root.resolve()
    violations: list[str] = []
    for hit in _scan_boundary(root_resolved):
        if hit.kind.startswith("jiuwenswarm") or hit.path.resolve() != allowed or not hit.in_function:
            relative = hit.path.resolve().relative_to(root_resolved).as_posix()
            violations.append(f"{relative}:{hit.line}:{hit.kind}:in_function={hit.in_function}")
    return violations


# Prose a person reads, and the catalog compiled from translated prose.
_PROSE_SUFFIXES = frozenset({".md", ".mo"})


def _scan_boundary(root: Path) -> list[_BoundaryHit]:
    hits: list[_BoundaryHit] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        data = path.read_bytes()
        if path.suffix == ".py":
            tree = ast.parse(data.decode("utf-8"))
            _walk_boundary(tree, path, False, _docstring_ids(tree), hits)
            continue
        if path.suffix in _PROSE_SUFFIXES or b"jiuwenswarm" not in data.lower():
            continue
        text = data.decode("utf-8", errors="replace")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "jiuwenswarm" in line.lower():
                hits.append(_BoundaryHit(path, lineno, "jiuwenswarm-data", False))
    return hits


def _docstring_ids(tree: ast.Module) -> frozenset[int]:
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    found: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, owners) or not node.body:
            continue
        first = node.body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
            found.add(id(first.value))
    return frozenset(found)


def _names_package(module: str, package: str) -> bool:
    return module == package or module.startswith(f"{package}.")


def _walk_boundary(
    node: ast.AST,
    path: Path,
    in_function: bool,
    docstrings: frozenset[int],
    hits: list[_BoundaryHit],
) -> None:
    modules: list[str] = []
    if isinstance(node, ast.Import):
        modules = [alias.name for alias in node.names]
    elif isinstance(node, ast.ImportFrom) and node.module is not None:
        modules = [node.module]
    for module in modules:
        for package in ("jiuwenbox", "jiuwenswarm"):
            if _names_package(module, package):
                hits.append(_BoundaryHit(path, node.lineno, f"{package}-import", in_function))
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        lowered = node.value.lower()
        if "jiuwenbox" in lowered:
            hits.append(_BoundaryHit(path, node.lineno, "jiuwenbox-string", in_function))
        if "jiuwenswarm" in lowered and id(node) not in docstrings:
            hits.append(_BoundaryHit(path, node.lineno, "jiuwenswarm-string", in_function))
    child_in_function = in_function or isinstance(node, _FUNCTION_NODES)
    for child in ast.iter_child_nodes(node):
        _walk_boundary(child, path, child_in_function, docstrings, hits)


def _dependency_name_and_specifier(requirement: str) -> tuple[str, str]:
    body = requirement.split(";", 1)[0].strip()
    if "[" in body:
        name, rest = body.split("[", 1)
        _extras, specifier = rest.split("]", 1)
        return name.strip(), specifier.strip()
    for operator in ("===", "==", ">=", "<=", "~=", "!=", ">", "<"):
        index = body.find(operator)
        if index != -1:
            return body[:index].strip(), body[index:].strip()
    return body.strip(), ""


def _is_exact_pin(specifier: str) -> bool:
    return specifier.startswith("==") and not specifier.startswith("===") and "," not in specifier
