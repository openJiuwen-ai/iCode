# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sandbox runtime fixes that can be checked without bubblewrap.

The cgroup join is an fd passed on the exec request, not a writable
``commands`` mount. Landlock grants write on the private ``/tmp``. An
explicit read-write bind wins over a read-only bind of that same path.
A create whose client disconnects is deleted. ``timed_out`` follows the
daemon's ``timeout`` error only.
"""

from __future__ import annotations

import array
import asyncio
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[3]
_CODE_AGENT_POLICY = _REPO / "third_party" / "jiuwenbox" / "src" / "jiuwenbox" / "configs" / "code-agent-policy.yaml"


@pytest.fixture
def jiuwenbox_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the vendored server import on Windows."""
    if not hasattr(os, "geteuid"):
        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)

    def _missing(*_args: object) -> None:
        raise KeyError(_args)

    def _install(name: str) -> None:
        if name in sys.modules:
            return
        try:
            __import__(name)
        except ModuleNotFoundError:
            module = types.ModuleType(name)
            if name == "pwd":
                module.getpwuid = _missing  # type: ignore[attr-defined]
                module.getpwnam = _missing  # type: ignore[attr-defined]
            else:
                module.getgrnam = _missing  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, name, module)

    _install("pwd")
    _install("grp")


def _flag_args(args: list[str], flag: str, arity: int) -> list[tuple[str, ...]]:
    found: list[tuple[str, ...]] = []
    index = 0
    while index < len(args):
        if args[index] == flag:
            found.append(tuple(args[index + 1 : index + 1 + arity]))
            index += 1 + arity
            continue
        index += 1
    return found


def test_explicit_read_write_bind_suppresses_same_destination_enumerated_read_only(
    jiuwenbox_importable: None,
) -> None:
    from jiuwenbox.supervisor.bwrap import BwrapConfig

    config = BwrapConfig(
        command=["true"],
        rw_binds=[
            ("/host/opt", "/opt"),
            ("/host/workspace", "/workspace"),
            ("/host/home", "/home/user"),
        ],
        ro_binds=[
            ("/opt", "/opt"),
            ("/host/workspace-ro", "/workspace"),
            ("/host/secret", "/opt/secret"),
            ("/host/ssh", "/home/user/.ssh"),
        ],
        enumerated_ro_binds=[("/opt", "/opt")],
        remount_ro=["/opt", "/opt/secret", "/workspace", "/home/user", "/home/user/.ssh", "/tmp"],
        tmpfs_mounts=["/tmp"],
    )
    args = config.to_args()
    bind_destinations = {pair[1] for pair in _flag_args(args, "--bind", 2)}
    ro_destinations = {pair[1] for pair in _flag_args(args, "--ro-bind", 2)}
    remounts = {pair[0] for pair in _flag_args(args, "--remount-ro", 1)}

    assert "/opt" in bind_destinations
    assert "/workspace" in bind_destinations
    assert "/home/user" in bind_destinations
    assert "/opt" not in ro_destinations
    assert "/home/user" not in ro_destinations
    assert "/opt" not in remounts
    assert "/home/user" not in remounts
    # A read-only bind the policy named itself protects the path: it is
    # mounted over the read-write bind and stays remounted read-only.
    workspace_binds = [
        (args[index], args[index + 1])
        for index in range(len(args) - 2)
        if args[index] in {"--bind", "--ro-bind"} and args[index + 2] == "/workspace"
    ]
    assert workspace_binds == [("--bind", "/host/workspace"), ("--ro-bind", "/host/workspace-ro")]
    assert "/workspace" in remounts
    assert "/opt/secret" in ro_destinations
    assert "/opt/secret" in remounts
    assert "/home/user/.ssh" in ro_destinations
    assert "/home/user/.ssh" in remounts
    assert "/tmp" in remounts


def test_private_tmp_is_writable_in_landlock_payload(jiuwenbox_importable: None) -> None:
    from jiuwenbox.models.policy import SecurityPolicy
    from jiuwenbox.server.runtime.process import ProcessRuntime
    from jiuwenbox.supervisor.cgroup import SANDBOX_COMMAND_CGROUP_ROOT

    policy = SecurityPolicy.model_validate(yaml.safe_load(_CODE_AGENT_POLICY.read_text(encoding="utf-8")))
    original_read_write = list(policy.filesystem_policy.read_write)
    # ``Path`` rewrites ``/tmp`` to ``\tmp`` on Windows. Either spelling
    # means the policy still lists the directory as read-only.
    assert any(path in {"/tmp", str(Path("/tmp"))} for path in policy.filesystem_policy.read_only)

    runtime = ProcessRuntime()
    encoded = runtime._ensure_landlock_payload("code-agent", policy)
    payload = json.loads(base64.urlsafe_b64decode(encoded.encode()))
    assert "/tmp" in payload["read_write"]
    assert policy.filesystem_policy.read_write == original_read_write

    disabled = policy.model_copy(deep=True)
    disabled.landlock.compatibility = "disabled"
    disabled_payload = json.loads(
        base64.urlsafe_b64decode(runtime._ensure_landlock_payload("code-agent-off", disabled).encode())
    )
    assert "/tmp" not in disabled_payload["read_write"]

    from jiuwenbox.models.policy import CgroupPolicy, FilesystemPolicy, NamespacePolicy
    from jiuwenbox.supervisor.cgroup import CgroupBackend, CgroupHandle

    runtime._cgroup_handles["box"] = CgroupHandle(
        backend=CgroupBackend.V2,
        sandbox_id="box",
        paths=[Path("/sys/fs/cgroup/jiuwenbox/box")],
        supervisor_paths=[Path("/sys/fs/cgroup/jiuwenbox/box/supervisor")],
        command_parent_paths=[Path("/sys/fs/cgroup/jiuwenbox/box/supervisor/commands")],
    )
    writable = SecurityPolicy(
        namespace=NamespacePolicy(user=True),
        filesystem_policy=FilesystemPolicy(read_only=["/tmp", "/opt"], read_write=["/workspace"]),
        cgroup=CgroupPolicy(memory_max=1048576),
    )
    args = runtime._build_sandbox_bwrap_args(
        "box",
        writable,
        ["true"],
        workdir=None,
        sandbox_env=None,
        netns_attached=False,
        seccomp_fd=None,
    )
    assert SANDBOX_COMMAND_CGROUP_ROOT not in args
    assert not any("supervisor/commands" in item for item in args)
    bind_destinations = {pair[1] for pair in _flag_args(args, "--bind", 2)}
    assert "/tmp" not in bind_destinations
    assert "/tmp" in {pair[0] for pair in _flag_args(args, "--tmpfs", 1)}


def test_prepare_cgroup_does_not_grant_landlock_write(
    jiuwenbox_importable: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenbox.models.policy import CgroupPolicy, FilesystemPolicy, SecurityPolicy
    from jiuwenbox.server.runtime import process as process_module
    from jiuwenbox.supervisor.cgroup import SANDBOX_COMMAND_CGROUP_ROOT, CgroupBackend, CgroupHandle

    handle = CgroupHandle(
        backend=CgroupBackend.V2,
        sandbox_id="box",
        supervisor_paths=[Path("/cg/supervisor")],
        command_parent_paths=[Path("/cg/commands")],
    )
    monkeypatch.setattr(process_module.cgroup_module, "setup", lambda *_args, **_kwargs: handle)
    policy = SecurityPolicy(
        cgroup=CgroupPolicy(memory_max=1048576),
        filesystem_policy=FilesystemPolicy(read_write=["/workspace"]),
    )
    before = list(policy.filesystem_policy.read_write)
    runtime = process_module.ProcessRuntime()
    runtime._prepare_cgroup("box", policy)
    assert policy.filesystem_policy.read_write == before
    assert SANDBOX_COMMAND_CGROUP_ROOT not in policy.filesystem_policy.read_write
    assert runtime._cgroup_handles["box"] is handle

    empty = SecurityPolicy(filesystem_policy=FilesystemPolicy(read_write=["/workspace"]))
    empty_before = list(empty.filesystem_policy.read_write)
    runtime._prepare_cgroup("empty", empty)
    assert empty.filesystem_policy.read_write == empty_before
    assert "empty" not in runtime._cgroup_handles


def test_command_cgroup_fd_is_write_only_and_cloexec(
    jiuwenbox_importable: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenbox.supervisor import cgroup as cgroup_module
    from jiuwenbox.supervisor.cgroup import CgroupBackend, CommandCgroup, open_command_cgroup_fds

    recorded: list[tuple[str, int]] = []

    def _open(path: str, flags: int) -> int:
        recorded.append((path, flags))
        return 40 + len(recorded)

    monkeypatch.setattr(cgroup_module.os, "open", _open)
    if not getattr(os, "O_CLOEXEC", 0):
        monkeypatch.setattr(os, "O_CLOEXEC", 0x80000, raising=False)
    command = CommandCgroup(
        backend=CgroupBackend.V1,
        paths=[Path("/cg/memory/cmd"), Path("/cg/cpu/cmd")],
    )
    fds = open_command_cgroup_fds(command)
    assert fds == [41, 42]
    assert [path for path, _flags in recorded] == command.procs_paths()
    for _path, flags in recorded:
        assert flags & os.O_WRONLY
        assert flags & os.O_CLOEXEC

    monkeypatch.setattr(os, "O_CLOEXEC", 0, raising=False)
    with pytest.raises(OSError, match="O_CLOEXEC"):
        open_command_cgroup_fds(command)


def test_exec_frame_carries_only_the_command_cgroup_fd(
    jiuwenbox_importable: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenbox.supervisor.daemon_ipc import send_frame_with_fds

    if getattr(socket, "SCM_RIGHTS", None) is None:
        monkeypatch.setattr(socket, "SCM_RIGHTS", 1, raising=False)

    class _Sock:
        def __init__(self) -> None:
            self.sent = b""
            self.ancillary: object = None

        def sendmsg(self, buffers: list[bytes | memoryview], ancillary: object) -> int:
            self.ancillary = ancillary
            chunk = bytes(buffers[0])
            self.sent += chunk
            return len(chunk)

    sock = _Sock()
    payload = b'{"type":"exec"}'
    send_frame_with_fds(sock, payload, [7, 9])  # type: ignore[arg-type]
    size = struct.unpack(">I", sock.sent[:4])[0]
    assert sock.sent[4 : 4 + size] == payload
    ancillary = sock.ancillary
    assert isinstance(ancillary, list)
    level, rights, raw = ancillary[0]
    assert level == socket.SOL_SOCKET
    assert rights == socket.SCM_RIGHTS
    got = array.array("i")
    got.frombytes(raw)
    assert list(got) == [7, 9]


def test_exec_frame_fails_when_fd_passing_is_unavailable(
    jiuwenbox_importable: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenbox.supervisor import daemon_ipc

    monkeypatch.setattr(socket, "SCM_RIGHTS", None, raising=False)

    class _Sock:
        def sendmsg(self, *_args: object) -> int:
            raise AssertionError("frame was sent without SCM_RIGHTS")

        def sendall(self, _data: bytes) -> None:
            raise AssertionError("frame was sent without SCM_RIGHTS")

    with pytest.raises(OSError, match="SCM_RIGHTS"):
        daemon_ipc.send_frame_with_fds(_Sock(), b"{}", [3])  # type: ignore[arg-type]


def test_preexec_writes_current_pid_and_closes_cgroup_fd(
    jiuwenbox_importable: None,
    tmp_path: Path,
) -> None:
    from jiuwenbox.supervisor.sandbox_daemon import _make_command_preexec

    path = tmp_path / "cgroup.procs"
    binary = getattr(os, "O_BINARY", 0)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | binary, 0o600)
    _make_command_preexec([], [fd])()
    assert path.read_bytes() == b"0\n"
    with pytest.raises(OSError):
        os.close(fd)


def test_start_command_passes_cgroup_fd_and_closes_the_parent_copy(
    jiuwenbox_importable: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenbox.supervisor import sandbox_daemon as daemon

    path = tmp_path / "cgroup.procs"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_TRUNC, 0o600)
    seen: dict[str, object] = {}
    closed: list[int] = []
    real_close = os.close

    def _close(target: int) -> None:
        closed.append(target)
        real_close(target)

    def _popen(*_args: object, **kwargs: object) -> object:
        seen["pass_fds"] = kwargs.get("pass_fds")
        raise OSError("stop before spawn")

    monkeypatch.setattr(daemon.os, "close", _close)
    monkeypatch.setattr(daemon.subprocess, "Popen", _popen)
    token = daemon._COMMAND_CGROUP_FDS.set((fd,))
    try:
        with pytest.raises(OSError, match="stop before spawn"):
            daemon._start_command(
                ["true"],
                env={},
                workdir=None,
                stdin=subprocess.DEVNULL,
                output=subprocess.DEVNULL,
                cgroup_procs=[],
            )
    finally:
        daemon._COMMAND_CGROUP_FDS.reset(token)
    pass_fds = seen["pass_fds"]
    assert isinstance(pass_fds, tuple)
    assert fd in pass_fds
    assert closed.count(fd) == 1


def test_daemon_rejects_cgroup_fd_count_without_fds(
    jiuwenbox_importable: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenbox.supervisor import sandbox_daemon as daemon
    from jiuwenbox.supervisor.daemon_ipc import recv_frame

    def _spawn(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("command was spawned")

    monkeypatch.setattr(daemon, "_start_command", _spawn)
    host, peer = socket.socketpair()
    host.settimeout(5)
    peer.settimeout(5)
    try:
        daemon._handle_exec(
            peer,
            {"command": ["true"], "cgroup_fd_count": 1},
            object(),  # type: ignore[arg-type]
        )
        response = json.loads(recv_frame(host, 1_000_000).decode("utf-8"))
    finally:
        host.close()
        peer.close()
    assert response["error"] == "bad_request"
    assert "failed to pass command cgroup" in response["stderr"]


def test_open_failure_fails_the_exec_without_a_mount_fallback(
    jiuwenbox_importable: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenbox.server.runtime import process as process_module
    from jiuwenbox.supervisor.cgroup import CgroupBackend, CgroupHandle, CommandCgroup

    command = CommandCgroup(backend=CgroupBackend.V2, paths=[Path("/cg/cmd")])
    released: list[object] = []
    monkeypatch.setattr(
        process_module.cgroup_module,
        "create_command_cgroup",
        lambda *_args, **_kwargs: command,
    )

    def _open(_command: object) -> list[int]:
        raise OSError("cannot pass")

    monkeypatch.setattr(process_module.cgroup_module, "open_command_cgroup_fds", _open)

    def _release(item: object) -> None:
        released.append(item)

    monkeypatch.setattr(process_module.cgroup_module, "release_command_cgroup", _release)
    runtime = process_module.ProcessRuntime()
    runtime._cgroup_handles["box"] = CgroupHandle(
        backend=CgroupBackend.V2,
        sandbox_id="box",
        supervisor_paths=[Path("/cg/supervisor")],
        command_parent_paths=[Path("/cg/commands")],
    )
    got, fds, failure = runtime._take_command_cgroup_fds("box")
    assert got is None
    assert fds == []
    assert failure is not None
    assert failure.exit_code == 1
    assert failure.timed_out is False
    assert failure.stderr.startswith("failed to pass command cgroup:")
    assert released == [command]


def test_timed_out_follows_only_the_daemon_timeout_error(jiuwenbox_importable: None) -> None:
    from jiuwenbox.models.sandbox import ExecResult
    from jiuwenbox.server.runtime.process import ProcessRuntime

    timed = ProcessRuntime._exec_result_from_daemon_response(
        {"exit_code": 124, "stderr": "Command timed out", "error": "timeout"}
    )
    other = ProcessRuntime._exec_result_from_daemon_response(
        {"exit_code": 124, "stderr": "daemon IPC timeout: timed out", "error": "ipc"}
    )
    absent = ProcessRuntime._exec_result_from_daemon_response({"exit_code": 124, "stderr": "no"})
    assert timed.timed_out is True
    assert other.timed_out is False
    assert absent.timed_out is False
    assert ExecResult(exit_code=124, stderr="daemon IPC timeout: x").timed_out is False
    assert ExecResult(exit_code=0).model_dump()["timed_out"] is False
    assert timed.model_dump()["timed_out"] is True


class _ExitedDaemon:
    returncode = 1

    def poll(self) -> int:
        return self.returncode


@pytest.mark.parametrize(
    ("log", "first_line"),
    [
        (
            b"setting up\nbwrap: Can't mkdir /jiuwenbox: Permission denied\n\n",
            "Sandbox daemon exited during startup with code 1: bwrap: Can't mkdir /jiuwenbox: Permission denied",
        ),
        (
            b"Traceback\n  File x\nOSError: \x1b[31mno space\x1b[0m\n",
            "Sandbox daemon exited during startup with code 1: OSError: [31mno space[0m",
        ),
        (b"\x1b\x07\n", "Sandbox daemon exited during startup with code 1."),
    ],
)
def test_a_daemon_that_dies_at_startup_names_its_last_line_first(
    jiuwenbox_importable: None,
    tmp_path: Path,
    log: bytes,
    first_line: str,
) -> None:
    from jiuwenbox.server.runtime.process import ProcessRuntime

    from chrys.service.sandbox import backend

    runtime = ProcessRuntime()
    runtime._cleanup_sandbox_artifacts = lambda *_args: None  # type: ignore[method-assign]
    with (tmp_path / "startup.log").open("w+b") as startup_log:
        startup_log.write(log)
        with pytest.raises(RuntimeError) as caught:
            runtime._verify_daemon_alive("box", _ExitedDaemon(), None, startup_log)  # type: ignore[arg-type]
    message = str(caught.value)
    assert message.splitlines()[0] == first_line
    assert str(backend._not_ready({"error_message": message})) == f"Sandbox did not become ready: {first_line}"


class _CreateRuntime:
    def __init__(self) -> None:
        self.mode = "ok"
        self.entered = asyncio.Event()
        self.cleaned: list[str] = []

    async def create(self, *, sandbox_id: str, policy_path: object, env: object) -> int:
        del policy_path, env
        self.entered.set()
        if self.mode == "ok":
            return 7
        if self.mode == "swallow":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return 8
            return 8
        await asyncio.Event().wait()
        return 9

    async def cleanup(self, sandbox_id: str) -> None:
        self.cleaned.append(sandbox_id)

    async def get_sandbox_ip_address(self, sandbox_id: str) -> None:
        del sandbox_id
        return


class _DisconnectRequest:
    def __init__(self, gone: asyncio.Event | None) -> None:
        self._gone = gone

    async def is_disconnected(self) -> bool:
        if self._gone is None:
            return False
        await self._gone.wait()
        return True


async def test_cancelled_creates_do_not_leave_ready_sandboxes(
    jiuwenbox_importable: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenbox.models.sandbox import SandboxPhase
    from jiuwenbox.server.policy_engine import PolicyEngine
    from jiuwenbox.server.routes import sandbox as sandbox_routes
    from jiuwenbox.server.routes.sandbox import CreateSandboxRequest, create_sandbox
    from jiuwenbox.server.sandbox_manager import SandboxManager
    from starlette.responses import Response

    class _Audit:
        def log(self, *_args: object, **_kwargs: object) -> None:
            return None

    runtime = _CreateRuntime()
    manager = SandboxManager(
        runtime=runtime,  # type: ignore[arg-type]
        policy_engine=PolicyEngine(policies_dir=tmp_path / "policies"),
        audit_logger=_Audit(),  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        policy_path=tmp_path / "missing-policy.yaml",
        update_policy_path=tmp_path / "missing-updates.yaml",
    )
    monkeypatch.setattr(sandbox_routes, "_mgr", lambda: manager)

    async def _cancel(sandbox_id: str, mode: str) -> None:
        runtime.entered.clear()
        runtime.mode = mode
        gone = asyncio.Event()
        task = asyncio.create_task(
            create_sandbox(CreateSandboxRequest(sandbox_id=sandbox_id), _DisconnectRequest(gone))  # type: ignore[arg-type]
        )
        await asyncio.wait_for(runtime.entered.wait(), timeout=10)
        gone.set()
        result = await task
        assert isinstance(result, Response)
        assert result.status_code == 499

    await _cancel("cancel-raise", "raise")
    await _cancel("cancel-race", "swallow")

    runtime.mode = "ok"
    kept = await create_sandbox(
        CreateSandboxRequest(sandbox_id="create-ok"),
        _DisconnectRequest(None),  # type: ignore[arg-type]
    )
    assert kept.id == "create-ok"
    assert kept.phase == SandboxPhase.READY

    listed = await manager.list_sandboxes()
    assert [item.id for item in listed] == ["create-ok"]
    assert listed[0].phase == SandboxPhase.READY
    assert set(runtime.cleaned) == {"cancel-raise", "cancel-race"}


def _route_manager(tmp_path: Path, runtime: _CreateRuntime) -> Any:
    from jiuwenbox.server.policy_engine import PolicyEngine
    from jiuwenbox.server.sandbox_manager import SandboxManager

    class _Audit:
        def log(self, *_args: object, **_kwargs: object) -> None:
            return None

    return SandboxManager(
        runtime=runtime,  # type: ignore[arg-type]
        policy_engine=PolicyEngine(policies_dir=tmp_path / "policies"),
        audit_logger=_Audit(),  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        policy_path=tmp_path / "missing-policy.yaml",
        update_policy_path=tmp_path / "missing-updates.yaml",
    )


async def test_dropped_duplicate_create_keeps_the_existing_sandbox(
    jiuwenbox_importable: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create whose client drops before it registers deletes nothing, not the id's current owner."""
    from jiuwenbox.models.sandbox import SandboxPhase
    from jiuwenbox.server.routes import sandbox as sandbox_routes
    from jiuwenbox.server.routes.sandbox import CreateSandboxRequest, create_sandbox
    from starlette.responses import Response

    from tests.support.waiting import wait_for

    runtime = _CreateRuntime()
    manager = _route_manager(tmp_path, runtime)
    monkeypatch.setattr(sandbox_routes, "_mgr", lambda: manager)
    kept = await create_sandbox(
        CreateSandboxRequest(sandbox_id="taken"),
        _DisconnectRequest(None),  # type: ignore[arg-type]
    )
    assert kept.phase == SandboxPhase.READY

    gone = asyncio.Event()
    gone.set()
    # The held lock keeps the duplicate from registering, or from being
    # refused, until its client has dropped.
    await manager._lock.acquire()
    try:
        task = asyncio.create_task(
            create_sandbox(CreateSandboxRequest(sandbox_id="taken"), _DisconnectRequest(gone))  # type: ignore[arg-type]
        )
        await wait_for(task.done, description="the dropped duplicate create returned without the manager lock")
    finally:
        manager._lock.release()
    result = await task

    assert isinstance(result, Response)
    assert result.status_code == 499
    listed = await manager.list_sandboxes()
    assert [(item.id, item.phase) for item in listed] == [("taken", SandboxPhase.READY)]
    assert listed[0] is kept
    assert runtime.cleaned == []


async def test_the_server_health_lists_the_hidden_host_paths_feature(
    jiuwenbox_importable: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    from jiuwenbox.models.common import HealthResponse
    from jiuwenbox.server.app import create_app

    from chrys.service.sandbox import backend

    monkeypatch.setenv("JIUWENBOX_API_TOKEN", "health-token")
    transport = httpx.ASGITransport(app=create_app())
    headers = {"Authorization": "Bearer health-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://sandbox.test", headers=headers) as client:
        assert await backend._require_health(client) is True
    assert HealthResponse(version="0").features == []


class _LateSpawnRuntime(_CreateRuntime):
    """Spawns its child only when ``spawn`` is set, then waits for a readiness that never comes."""

    def __init__(self) -> None:
        super().__init__()
        self.spawn = asyncio.Event()
        self.running: set[str] = set()

    async def create(self, *, sandbox_id: str, policy_path: object, env: object) -> int:
        del policy_path, env
        self.entered.set()
        await self.spawn.wait()
        self.running.add(sandbox_id)
        await asyncio.Event().wait()
        return 7

    async def cleanup(self, sandbox_id: str) -> None:
        await super().cleanup(sandbox_id)
        self.running.discard(sandbox_id)


async def test_a_delete_during_a_cold_start_also_stops_the_child_spawned_after_it(
    jiuwenbox_importable: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenbox.models.sandbox import SandboxPhase
    from jiuwenbox.server.routes import sandbox as sandbox_routes
    from jiuwenbox.server.routes.sandbox import CreateSandboxRequest, create_sandbox
    from starlette.responses import Response

    from tests.support.waiting import wait_for

    runtime = _LateSpawnRuntime()
    manager = _route_manager(tmp_path, runtime)
    monkeypatch.setattr(sandbox_routes, "_mgr", lambda: manager)
    gone = asyncio.Event()
    creating = asyncio.create_task(
        create_sandbox(CreateSandboxRequest(sandbox_id="late"), _DisconnectRequest(gone))  # type: ignore[arg-type]
    )
    await wait_for(lambda: runtime.entered.is_set() or creating.done(), description="the runtime start under way")
    ref = manager._sandboxes["late"]
    deleting = asyncio.create_task(manager.delete_sandbox("late"))
    await wait_for(
        lambda: deleting.done() or ref.phase == SandboxPhase.DELETING,
        description="the delete marked the sandbox",
    )
    runtime.spawn.set()
    await wait_for(lambda: "late" in runtime.running, description="the child spawned after the delete began")
    assert not deleting.done()
    gone.set()
    result = await creating
    await deleting

    assert isinstance(result, Response)
    assert result.status_code == 499
    assert runtime.running == set()
    assert await manager.list_sandboxes() == []
    assert manager._starting == {}


class _SlowStartRuntime(_LateSpawnRuntime):
    """Spawns its child once ``spawn`` is set, then returns or raises once ``finish`` is set."""

    def __init__(self, *, fails: bool) -> None:
        super().__init__()
        self.finish = asyncio.Event()
        self.fails = fails

    async def create(self, *, sandbox_id: str, policy_path: object, env: object) -> int:
        del policy_path, env
        self.entered.set()
        await self.spawn.wait()
        self.running.add(sandbox_id)
        await self.finish.wait()
        if self.fails:
            raise RuntimeError("the daemon did not answer")
        return 7


@pytest.mark.parametrize("start", ["returns", "raises"])
@pytest.mark.parametrize("ends", ["wait_runs_out", "delete_cancelled"])
async def test_a_delete_that_stops_waiting_for_a_start_finishes_once_the_start_ends(
    jiuwenbox_importable: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ends: str,
    start: str,
) -> None:
    """The client stays connected, so only the deferred delete removes the sandbox and its late child."""
    from jiuwenbox.models.sandbox import SandboxPhase
    from jiuwenbox.server import sandbox_manager
    from jiuwenbox.server.routes import sandbox as sandbox_routes
    from jiuwenbox.server.routes.sandbox import CreateSandboxRequest, create_sandbox

    from tests.support.waiting import wait_for

    if ends == "wait_runs_out":
        monkeypatch.setattr(sandbox_manager, "DELETE_START_WAIT_SECONDS", 0.0)
    runtime = _SlowStartRuntime(fails=start == "raises")
    manager = _route_manager(tmp_path, runtime)
    monkeypatch.setattr(sandbox_routes, "_mgr", lambda: manager)
    creating = asyncio.create_task(
        create_sandbox(CreateSandboxRequest(sandbox_id="slow"), _DisconnectRequest(None))  # type: ignore[arg-type]
    )
    await wait_for(lambda: runtime.entered.is_set() or creating.done(), description="the runtime start under way")
    ref = manager._sandboxes["slow"]
    deleting = asyncio.create_task(manager.delete_sandbox("slow"))
    if ends == "wait_runs_out":
        await deleting
    else:
        await wait_for(
            lambda: deleting.done() or ref.phase == SandboxPhase.DELETING,
            description="the delete marked the sandbox",
        )
        deleting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await deleting

    assert not creating.done()
    assert manager._sandboxes == {"slow": ref}
    assert ref.phase == SandboxPhase.DELETING
    assert len(manager._deferred_deletes) == 1
    runtime.spawn.set()
    await wait_for(lambda: "slow" in runtime.running, description="the child spawned after the delete returned")
    runtime.finish.set()
    result = await creating
    await wait_for(lambda: not manager._deferred_deletes, description="the deferred delete finished")

    assert result is ref
    assert result.phase == SandboxPhase.DELETING
    assert runtime.running == set()
    assert await manager.list_sandboxes() == []
    assert manager._starting == {}


async def test_delete_with_an_expected_ref_spares_another_owner(
    jiuwenbox_importable: None,
    tmp_path: Path,
) -> None:
    from jiuwenbox.models.sandbox import SandboxSpec
    from jiuwenbox.server.runtime.errors import SandboxNotFoundError
    from jiuwenbox.server.sandbox_manager import CreateDelivery

    runtime = _CreateRuntime()
    manager = _route_manager(tmp_path, runtime)
    delivery = CreateDelivery()
    ref = await manager.create_sandbox(SandboxSpec(sandbox_id="owned"), delivery=delivery)
    assert delivery.registered is ref

    with pytest.raises(SandboxNotFoundError):
        await manager.delete_sandbox("owned", expected=ref.model_copy())
    assert [item.id for item in await manager.list_sandboxes()] == ["owned"]
    assert runtime.cleaned == []

    await manager.delete_sandbox("owned", expected=ref)
    assert await manager.list_sandboxes() == []
    assert runtime.cleaned == ["owned"]


def test_any_bind_naming_the_engine_home_is_reserved_and_a_parent_bind_is_not(
    jiuwenbox_importable: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parent bind of ~/.jiuwenbox is allowed; the runtime hides the home under it.

    A bind that names the home itself, read-only cover included, is reserved:
    a cover inside a writable parent would be dropped by a writable root bind
    and leave the home readable.
    """
    from jiuwenbox.models.policy import BindMount, FilesystemPolicy, SecurityPolicy
    from jiuwenbox.server import policy_engine as policy_engine_module
    from jiuwenbox.server.policy_engine import PolicyEngine, PolicyValidationError

    monkeypatch.setattr(policy_engine_module, "JIUWENBOX_HOME", "/home/user/.jiuwenbox")
    monkeypatch.setattr(policy_engine_module, "SANDBOX_WORKSPACE", "/home/user/.jiuwenbox/workspace")
    engine = PolicyEngine(policies_dir=tmp_path / "policies")
    home = "/home/user"
    engine_home = "/home/user/.jiuwenbox"

    def _bind(path: str, mode: str, sandbox_path: str | None = None) -> BindMount:
        # Keep the POSIX spelling. ``BindMount`` expands through ``Path``, which
        # rewrites a leading slash on Windows and would hide the reserved check.
        return BindMount.model_construct(
            host_path=path,
            sandbox_path=path if sandbox_path is None else sandbox_path,
            mode=mode,
        )

    parent = SecurityPolicy(filesystem_policy=FilesystemPolicy(bind_mounts=[_bind(home, "rw")]))
    warnings = engine.validate_policy(parent)
    assert all("reserved" not in item for item in warnings)

    covered = SecurityPolicy(
        filesystem_policy=FilesystemPolicy(bind_mounts=[_bind(home, "rw"), _bind(engine_home, "ro")])
    )
    with pytest.raises(PolicyValidationError, match="reserved"):
        engine.validate_policy(covered)

    direct = SecurityPolicy(filesystem_policy=FilesystemPolicy(bind_mounts=[_bind(engine_home, "rw")]))
    with pytest.raises(PolicyValidationError, match="reserved"):
        engine.validate_policy(direct)

    exposed = SecurityPolicy(filesystem_policy=FilesystemPolicy(bind_mounts=[_bind(engine_home, "ro")]))
    with pytest.raises(PolicyValidationError, match="reserved"):
        engine.validate_policy(exposed)

    remapped = SecurityPolicy(
        filesystem_policy=FilesystemPolicy(
            bind_mounts=[_bind(home, "rw"), _bind(engine_home, "ro", sandbox_path="/tmp/engine")]
        )
    )
    with pytest.raises(PolicyValidationError, match="reserved"):
        engine.validate_policy(remapped)


def test_hidden_host_paths_must_be_absolute_and_not_the_root(
    jiuwenbox_importable: None,
    tmp_path: Path,
) -> None:
    from jiuwenbox.models.policy import FilesystemPolicy, SecurityPolicy
    from jiuwenbox.server.policy_engine import PolicyEngine, PolicyValidationError

    engine = PolicyEngine(policies_dir=tmp_path / "policies")

    def _policy(*paths: str) -> SecurityPolicy:
        return SecurityPolicy(filesystem_policy=FilesystemPolicy(hidden_host_paths=list(paths)))

    engine.validate_policy(_policy("/home/user/.chrys"))
    with pytest.raises(PolicyValidationError, match="absolute"):
        engine.validate_policy(_policy("relative/.chrys"))
    with pytest.raises(PolicyValidationError, match="'/'"):
        engine.validate_policy(_policy("//"))


def test_hidden_host_paths_survive_an_append_merge_and_are_not_expanded(
    jiuwenbox_importable: None,
    tmp_path: Path,
) -> None:
    from jiuwenbox.models.policy import FilesystemPolicy, SecurityPolicy
    from jiuwenbox.server.policy_engine import PolicyEngine

    engine = PolicyEngine(policies_dir=tmp_path / "policies")
    base = SecurityPolicy(filesystem_policy=FilesystemPolicy(hidden_host_paths=["/srv/base"]))
    merged = engine.merge_policy(
        base,
        {"filesystem_policy": {"hidden_host_paths": ["/home/user/$HOME/~x", "/srv/base"]}},
    )
    assert merged.filesystem_policy.hidden_host_paths == ["/srv/base", "/home/user/$HOME/~x"]


def test_clear_persistent_state_leaves_the_other_managers_files(
    jiuwenbox_importable: None,
    tmp_path: Path,
) -> None:
    from jiuwenbox.models.sandbox import SandboxPhase, SandboxRef
    from jiuwenbox.server.policy_engine import PolicyEngine
    from jiuwenbox.server.sandbox_manager import SandboxManager

    class _Audit:
        def log(self, *_args: object, **_kwargs: object) -> None:
            return None

    state = tmp_path / "state"
    policies = tmp_path / "policies"

    def _manager() -> SandboxManager:
        return SandboxManager(
            runtime=object(),  # type: ignore[arg-type]
            policy_engine=PolicyEngine(policies_dir=policies),
            audit_logger=_Audit(),  # type: ignore[arg-type]
            state_dir=state,
            policy_path=tmp_path / "missing-policy.yaml",
        )

    first = _manager()
    second = _manager()
    first._save_state(SandboxRef(id="sandbox-a", phase=SandboxPhase.READY))
    first._write_policy("sandbox-a", first.policy)
    second._save_state(SandboxRef(id="sandbox-b", phase=SandboxPhase.READY))
    second._write_policy("sandbox-b", second.policy)

    first.clear_persistent_state()

    assert not (state / "sandbox-a.json").exists()
    assert not (policies / "sandbox-a_sandbox_policy.yaml").exists()
    assert (state / "sandbox-b.json").is_file()
    assert (policies / "sandbox-b_sandbox_policy.yaml").is_file()


def test_clear_persistent_state_leaves_a_reused_id_to_its_new_owner(
    jiuwenbox_importable: None,
    tmp_path: Path,
) -> None:
    """A manager that deleted an id no longer owns its files once another manager reuses it."""
    from jiuwenbox.models.sandbox import SandboxPhase, SandboxRef
    from jiuwenbox.server.policy_engine import PolicyEngine
    from jiuwenbox.server.sandbox_manager import SandboxManager

    class _Audit:
        def log(self, *_args: object, **_kwargs: object) -> None:
            return None

    state = tmp_path / "state"
    policies = tmp_path / "policies"

    def _manager() -> SandboxManager:
        return SandboxManager(
            runtime=object(),  # type: ignore[arg-type]
            policy_engine=PolicyEngine(policies_dir=policies),
            audit_logger=_Audit(),  # type: ignore[arg-type]
            state_dir=state,
            policy_path=tmp_path / "missing-policy.yaml",
        )

    first = _manager()
    second = _manager()
    first._save_state(SandboxRef(id="reused", phase=SandboxPhase.READY))
    first._write_policy("reused", first.policy)
    first._delete_state("reused")
    first._delete_policy("reused")
    second._save_state(SandboxRef(id="reused", phase=SandboxPhase.READY))
    second._write_policy("reused", second.policy)

    first.clear_persistent_state()

    assert (state / "reused.json").is_file()
    assert (policies / "reused_sandbox_policy.yaml").is_file()


async def test_served_create_cancel_does_not_leave_ready_sandboxes(
    jiuwenbox_importable: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped HTTP client during cold start does not leave a READY sandbox.

    Idle reaping stays off. Two drops and one successful create leave the
    one sandbox the client received.
    """
    import contextlib

    import uvicorn
    from jiuwenbox.models.sandbox import SandboxPhase
    from jiuwenbox.server import app as app_module
    from jiuwenbox.server.policy_engine import PolicyEngine
    from jiuwenbox.server.sandbox_manager import SandboxManager

    from tests.support.waiting import wait_for

    class _Audit:
        def log(self, *_args: object, **_kwargs: object) -> None:
            return None

    class _Hold:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.swallow = False
            self.cancelled: list[str] = []
            self.cleaned: list[str] = []

        async def create(self, *, sandbox_id: str, policy_path: object, env: object) -> int:
            del policy_path, env
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.append(sandbox_id)
                if not self.swallow:
                    raise
                # A runtime that handles cancellation and still returns a pid.
                # The next await in the manager must not set READY.
                task = asyncio.current_task()
                if task is not None:
                    while task.uncancel():
                        pass
                return 7
            return 7

        async def cleanup(self, sandbox_id: str) -> None:
            self.cleaned.append(sandbox_id)

        async def get_sandbox_ip_address(self, sandbox_id: str) -> None:
            del sandbox_id
            return

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> contextlib.AbstractContextManager[None]:
            yield

    monkeypatch.setenv("JIUWENBOX_API_TOKEN", "sekret")
    runtime = _Hold()
    manager = SandboxManager(
        runtime=runtime,  # type: ignore[arg-type]
        policy_engine=PolicyEngine(policies_dir=tmp_path / "policies"),
        audit_logger=_Audit(),  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        policy_path=tmp_path / "missing-policy.yaml",
    )
    previous = app_module._sandbox_manager
    app_module._sandbox_manager = manager
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    address = listener.getsockname()[:2]
    server = _Server(uvicorn.Config(app_module.app, lifespan="off", log_config=None, timeout_graceful_shutdown=5))
    serving = asyncio.create_task(server.serve(sockets=[listener]))

    def _post(sandbox_id: str) -> bytes:
        body = json.dumps({"sandbox_id": sandbox_id}).encode("utf-8")
        head = (
            "POST /api/v1/sandboxes HTTP/1.1\r\n"
            "Host: jiuwenbox\r\n"
            "Authorization: Bearer sekret\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "\r\n"
        )
        return head.encode("ascii") + body

    async def _drop(sandbox_id: str, *, swallow: bool) -> None:
        runtime.swallow = swallow
        runtime.started.clear()
        runtime.release = asyncio.Event()
        _reader, writer = await asyncio.open_connection(*address)
        writer.write(_post(sandbox_id))
        await writer.drain()
        await asyncio.wait_for(runtime.started.wait(), timeout=10)
        writer.close()
        await writer.wait_closed()
        await wait_for(
            lambda: sandbox_id in runtime.cancelled,
            timeout=10,
            description=f"{sandbox_id} create cancelled after the client dropped",
        )
        await wait_for(
            lambda: all(item.id != sandbox_id for item in manager._sandboxes.values()),
            timeout=10,
            description=f"{sandbox_id} was removed",
        )

    try:
        await wait_for(lambda: server.started or serving.done(), description="jiuwenbox app serving")
        if serving.done():
            await serving
        await _drop("drop-raise", swallow=False)
        await _drop("drop-keep", swallow=True)
        runtime.swallow = False
        runtime.started.clear()
        runtime.release = asyncio.Event()
        reader, writer = await asyncio.open_connection(*address)
        try:
            writer.write(_post("drop-ok"))
            await writer.drain()
            await asyncio.wait_for(runtime.started.wait(), timeout=10)
            runtime.release.set()
            line = await asyncio.wait_for(reader.readline(), timeout=10)
        finally:
            writer.close()
            await writer.wait_closed()
        assert int(line.split(b" ")[1]) == 201
        listed = await manager.list_sandboxes()
        assert [item.id for item in listed] == ["drop-ok"]
        assert listed[0].phase == SandboxPhase.READY
    finally:
        runtime.release.set()
        app_module._sandbox_manager = previous
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(serving, 15)
        listener.close()


@pytest.mark.skipif(sys.platform == "win32", reason="bwrap enumerates POSIX host roots")
def test_policy_read_only_cover_wins_over_a_read_write_bind_of_the_same_path(
    jiuwenbox_importable: None,
    tmp_path: Path,
) -> None:
    from jiuwenbox.models.policy import BindMount, BindRootEntries, FilesystemPolicy, SecurityPolicy
    from jiuwenbox.supervisor.bwrap import BwrapConfig

    root = tmp_path / "root"
    (root / "opt").mkdir(parents=True)
    (root / "srv").mkdir()
    opt, srv = str(root / "opt"), str(root / "srv")
    policy = SecurityPolicy(
        filesystem_policy=FilesystemPolicy(
            bind_root_entries=[BindRootEntries(host_root=str(root), sandbox_path="/", mode="ro")],
            bind_mounts=[
                BindMount(host_path=opt, sandbox_path="/opt", mode="rw"),
                BindMount(host_path=srv, sandbox_path="/srv", mode="rw"),
                BindMount(host_path=srv, sandbox_path="/srv", mode="ro"),
            ],
        ),
    )
    args = BwrapConfig.from_policy(policy, ["true"]).to_args()
    binds = [
        (args[index], args[index + 2])
        for index in range(len(args) - 2)
        if args[index] in {"--bind", "--ro-bind"} and args[index + 2] in {"/opt", "/srv"}
    ]
    assert binds == [("--bind", "/opt"), ("--bind", "/srv"), ("--ro-bind", "/srv")]


def _run_bpf(program: bytes, *, arch: int, nr: int, args: tuple[int, ...] = ()) -> int:
    """Evaluate the classic-BPF subset the seccomp builder emits against one syscall."""
    values = (*args, 0, 0, 0, 0, 0, 0)[:6]
    data = struct.pack("<iIQ", nr, arch, 0) + b"".join(struct.pack("<Q", value & (2**64 - 1)) for value in values)
    instructions = [struct.unpack_from("<HBBI", program, offset) for offset in range(0, len(program), 8)]
    accumulator = 0
    pc = 0
    while True:
        code, jump_true, jump_false, k = instructions[pc]
        if code == 0x20:
            (accumulator,) = struct.unpack_from("<I", data, k)
            pc += 1
        elif code == 0x15:
            pc += 1 + (jump_true if accumulator == k else jump_false)
        elif code == 0x05:
            pc += 1 + k
        elif code == 0x06:
            return k
        else:
            raise AssertionError(f"unexpected BPF opcode {code:#x}")


def test_the_signal_guard_protects_the_daemon_and_spares_the_own_process_group(
    jiuwenbox_importable: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenbox.supervisor import seccomp

    monkeypatch.setattr(seccomp, "_machine", lambda: "x86_64")
    monkeypatch.setattr(seccomp, "_get_syscall_numbers", seccomp._fallback_syscall_numbers)
    table = seccomp._fallback_syscall_numbers()
    program = seccomp._assemble_bpf([])
    arch = seccomp._audit_arch()
    allow = seccomp.SECCOMP_RET_ALLOW
    eperm = seccomp.SECCOMP_RET_ERRNO | seccomp.ERRNO_EPERM
    enosys = seccomp.SECCOMP_RET_ERRNO | seccomp.ERRNO_ENOSYS

    def verdict(name: str, *args: int) -> int:
        return _run_bpf(program, arch=arch, nr=table[name], args=args)

    assert verdict("kill", 0, 15) == allow
    assert verdict("kill", 18, 9) == allow
    assert verdict("kill", 2, 9) == eperm
    assert verdict("kill", -1, 9) == eperm
    assert verdict("kill", -2, 9) == eperm
    assert verdict("tgkill", 18, 2, 9) == eperm
    assert verdict("tgkill", 18, 19, 9) == allow
    for name in ("rt_sigqueueinfo", "rt_tgsigqueueinfo", "pidfd_open"):
        assert verdict(name, 2) == eperm, name
        assert verdict(name, 1) == eperm, name
        assert verdict(name, 18) == allow, name
    assert verdict("pidfd_send_signal", 3, 9) == enosys
    assert verdict("getpid") == allow


def test_a_landlock_rule_for_a_file_grants_only_that_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A read-write rule for ``/run/guarded-proxy.sock`` must not make all of ``/run`` writable."""
    import ctypes
    import importlib.util

    stub_libc = types.SimpleNamespace(syscall=lambda *_args: 0, prctl=lambda *_args: 0)
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: stub_libc)
    source = Path(__file__).resolve().parents[3] / "third_party/jiuwenbox/src/jiuwenbox/supervisor/landlock_launcher.py"
    spec = importlib.util.spec_from_file_location("landlock_launcher_under_test", source)
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)

    _read_only, read_write = launcher._access_masks(3)
    socket_file = tmp_path / "guarded-proxy.sock"
    socket_file.write_bytes(b"")
    file_rights = launcher._rule_access(str(socket_file), read_write)
    assert file_rights == read_write & launcher.FILE_ACCESS
    assert not file_rights & (launcher.MAKE_REG | launcher.MAKE_DIR | launcher.REMOVE_FILE | launcher.MAKE_SOCK)
    assert launcher._rule_access(str(tmp_path), read_write) == read_write
