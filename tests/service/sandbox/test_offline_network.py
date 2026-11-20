# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Offline network mode and command-cgroup creation.

Offline unshares the sandbox network and does not create a named netns.
A sandbox that already has a cgroup fails the exec when the command child
cannot be created. Nothing starts bubblewrap or a real cgroup hierarchy.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def jiuwenbox_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the vendored server import on Windows.

    ``pwd`` and ``grp`` are imported at module scope. The stubs only exist
    so those imports succeed; no account lookup runs.
    """
    import os

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


def test_offline_unshares_net_without_a_named_namespace(
    jiuwenbox_importable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenbox.models.policy import NetworkMode, SecurityPolicy
    from jiuwenbox.server.runtime import process as process_module
    from jiuwenbox.server.runtime.process import ProcessRuntime

    created: list[str] = []
    monkeypatch.setattr(
        process_module.network_module,
        "create_named_namespace",
        created.append,
    )
    monkeypatch.setenv("JIUWENBOX_NETWORK_MODE", "offline")
    host = SecurityPolicy(network={"mode": "host"})
    offline = process_module._apply_operator_network_mode(host)
    assert offline.network.mode == NetworkMode.OFFLINE
    assert host.network.mode == NetworkMode.HOST

    runtime = ProcessRuntime()
    assert runtime._ensure_named_netns("sb-offline", offline) is None
    assert created == []

    offline_argv = runtime._build_sandbox_bwrap_args(
        "sb-offline",
        offline,
        ["true"],
        workdir=None,
        sandbox_env=None,
        netns_attached=False,
        seccomp_fd=None,
    )
    assert "--unshare-net" in offline_argv

    monkeypatch.setenv("JIUWENBOX_NETWORK_MODE", "host")
    assert process_module._apply_operator_network_mode(host).network.mode == NetworkMode.HOST
    host_argv = runtime._build_sandbox_bwrap_args(
        "sb-host",
        host,
        ["true"],
        workdir=None,
        sandbox_env=None,
        netns_attached=False,
        seccomp_fd=None,
    )
    assert "--unshare-net" not in host_argv

    isolated = SecurityPolicy(network={"mode": "isolated"})
    assert process_module._apply_operator_network_mode(isolated).network.mode == NetworkMode.ISOLATED
    attached = runtime._build_sandbox_bwrap_args(
        "sb-isolated",
        isolated,
        ["true"],
        workdir=None,
        sandbox_env=None,
        netns_attached=True,
        seccomp_fd=None,
    )
    fresh = runtime._build_sandbox_bwrap_args(
        "sb-isolated",
        isolated,
        ["true"],
        workdir=None,
        sandbox_env=None,
        netns_attached=False,
        seccomp_fd=None,
    )
    assert "--unshare-net" not in attached
    assert "--unshare-net" in fresh


def test_exec_fails_when_the_command_cgroup_cannot_be_created(jiuwenbox_importable: None, tmp_path: Path) -> None:
    from jiuwenbox.server.runtime.process import ProcessRuntime
    from jiuwenbox.supervisor.cgroup import CgroupBackend, CgroupHandle

    parent = tmp_path / "not-a-directory"
    parent.write_text("x", encoding="utf-8")
    runtime = ProcessRuntime()
    runtime._cgroup_handles["sb"] = CgroupHandle(
        backend=CgroupBackend.V2,
        sandbox_id="sb",
        paths=[parent],
        supervisor_paths=[tmp_path / "supervisor"],
    )

    command, failure = runtime._open_command_cgroup("sb")

    assert command is None
    assert failure is not None
    assert failure.exit_code == 1
    assert "command cgroup" in failure.stderr
    missing, missing_failure = runtime._open_command_cgroup("absent")
    assert missing is None
    assert missing_failure is None


def test_command_join_path_is_inside_the_sandbox(jiuwenbox_importable: None) -> None:
    """The command writes a bind under /run, not the read-only host /sys path."""
    from jiuwenbox.supervisor.cgroup import (
        SANDBOX_COMMAND_CGROUP_ROOT,
        CgroupBackend,
        CgroupHandle,
        CommandCgroup,
        sandbox_cgroup_bind_args,
    )

    parent = Path("/sys/fs/cgroup/jiuwenbox/sb/supervisor/commands")
    handle = CgroupHandle(
        backend=CgroupBackend.V2,
        sandbox_id="sb",
        paths=[parent.parent.parent],
        supervisor_paths=[parent.parent],
        command_parent_paths=[parent],
    )
    command = CommandCgroup(
        backend=CgroupBackend.V2,
        paths=[parent / "cmd-abc"],
    )
    assert command.sandbox_procs_paths() == [f"{SANDBOX_COMMAND_CGROUP_ROOT}/cmd-abc/cgroup.procs"]
    assert sandbox_cgroup_bind_args(handle) == [
        "--dir",
        SANDBOX_COMMAND_CGROUP_ROOT,
        "--bind",
        str(parent),
        SANDBOX_COMMAND_CGROUP_ROOT,
    ]


def test_user_namespace_turns_on_only_when_the_probe_succeeds(
    jiuwenbox_importable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenbox.models.policy import SecurityPolicy
    from jiuwenbox.server.runtime import process as process_module

    policy = SecurityPolicy()
    policy = policy.model_copy(update={"namespace": policy.namespace.model_copy(update={"user": False})})
    monkeypatch.setattr(process_module, "unprivileged_user_namespace_works", lambda: False)
    assert process_module._apply_user_namespace(policy).namespace.user is False
    monkeypatch.setattr(process_module, "unprivileged_user_namespace_works", lambda: True)
    assert process_module._apply_user_namespace(policy).namespace.user is True


def test_guarded_blocks_metadata_and_private_addresses(jiuwenbox_importable: None) -> None:
    from jiuwenbox.supervisor.guarded_proxy import address_is_blocked, host_is_blocked

    assert address_is_blocked("169.254.169.254") is True
    assert address_is_blocked("10.1.2.3") is True
    assert address_is_blocked("192.168.0.1") is True
    assert address_is_blocked("127.0.0.1") is True
    assert address_is_blocked("8.8.8.8") is False
    assert host_is_blocked("169.254.169.254") is True
    assert host_is_blocked("8.8.8.8") is False


def test_disk_floor_trips_when_free_space_is_low(jiuwenbox_importable: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenbox.supervisor import disk_floor

    monkeypatch.setattr(disk_floor, "free_bytes", lambda _path: 10)
    assert disk_floor.any_below_floor(["/work"]) is True
    monkeypatch.setattr(disk_floor, "free_bytes", lambda _path: disk_floor.DISK_FREE_FLOOR_BYTES)
    assert disk_floor.any_below_floor(["/work"]) is False
