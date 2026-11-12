# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only covers for account files inside a writable sandbox mount.

HTTP is mocked. Bubblewrap is not started; the argv check builds ``BwrapConfig``
and reads ``to_args``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

import chrys.service.sandbox.backend as backend
from chrys.service.sandbox.backend import SandboxExec, run_shell
from tests.support.symlinks import symlink_or_skip


@dataclass(frozen=True, slots=True)
class _Settings:
    """Stand-in with the three sandbox fields. Not the Settings class."""

    sandbox_enabled: bool = True
    sandbox_url: str = "http://sandbox.test"
    sandbox_token: str = ""


class _Script:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body: object = json.loads(request.content) if request.content else None
        self.events.append((request.method, request.url.path, body))
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "POST" and path == "/api/v1/sandboxes":
            return httpx.Response(201, json={"id": "sb-1", "phase": "ready"})
        if request.method == "POST" and path.endswith("/exec"):
            return httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})
        return httpx.Response(500, json={"error": "unexpected"})


@pytest.fixture(autouse=True)
def _forbid_host_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """A supervisor start or a managed child is a host-side path."""

    async def forbid(*_args: object, **_kwargs: object) -> tuple[str, str]:
        raise AssertionError("supervisor was started")

    def forbid_managed(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("managed subprocess")

    monkeypatch.setattr("chrys.service.sandbox.supervisor.ensure_started", forbid)
    monkeypatch.setattr("chrys.foundation.platform.process.managed_subprocess", forbid_managed)


def _install(monkeypatch: pytest.MonkeyPatch, script: _Script) -> None:
    def new_client(**kwargs: Any) -> httpx.AsyncClient:
        built = dict(kwargs)
        built["transport"] = httpx.MockTransport(script.handler)
        return httpx.AsyncClient(**built)

    monkeypatch.setattr("chrys.service.sandbox.client._new_client", new_client)


async def _run(
    script: _Script,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cwd: str,
    workspace_root: str | None = None,
    workspace_dirs: tuple[str, ...] = (),
) -> SandboxExec:
    _install(monkeypatch, script)
    return await run_shell(
        _Settings(),
        argv=("echo", "hi"),
        cwd=cwd,
        timeout_seconds=12,
        linux=True,
        workspace_root=workspace_root,
        workspace_dirs=workspace_dirs,
    )


def _bind(path: str, mode: str) -> dict[str, str]:
    return {"host_path": path, "sandbox_path": path, "mode": mode}


def _bind_mounts(script: _Script) -> list[object]:
    bodies = [body for method, path, body in script.events if method == "POST" and path == "/api/v1/sandboxes"]
    assert len(bodies) == 1
    body = bodies[0]
    assert isinstance(body, dict)
    assert body["policy_mode"] == "append"
    policy = body["policy"]
    assert isinstance(policy, dict)
    filesystem = policy["filesystem_policy"]
    assert isinstance(filesystem, dict)
    assert set(filesystem) == {"bind_mounts"}
    mounts = filesystem["bind_mounts"]
    assert isinstance(mounts, list)
    return mounts


def _physical(path: Path) -> str:
    return os.path.realpath(path)


async def test_writable_workspace_covers_ssh_directory_and_gitconfig_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real ``.ssh`` directory and a real ``.gitconfig`` file are read-only covers."""
    work = tmp_path / "work"
    (work / ".ssh").mkdir(parents=True)
    (work / ".gitconfig").write_text("name = test\n", encoding="utf-8")
    physical = _physical(work)
    script = _Script()
    result = await _run(script, monkeypatch, cwd=str(work), workspace_root=str(work))
    assert result.exit_code == 0
    assert _bind_mounts(script) == [
        _bind(physical, "rw"),
        _bind(os.path.join(physical, ".ssh"), "ro"),
        _bind(os.path.join(physical, ".gitconfig"), "ro"),
    ]


async def test_credential_files_are_covered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GnuPG and npm config inside the writable mount are read-only covers."""
    work = tmp_path / "work"
    (work / ".gnupg").mkdir(parents=True)
    (work / ".npmrc").write_text("token\n", encoding="utf-8")
    physical = _physical(work)
    script = _Script()
    await _run(script, monkeypatch, cwd=str(work), workspace_root=str(work))
    assert _bind_mounts(script) == [
        _bind(physical, "rw"),
        _bind(os.path.join(physical, ".gnupg"), "ro"),
        _bind(os.path.join(physical, ".npmrc"), "ro"),
    ]


async def test_missing_sensitive_name_is_not_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``.ssh`` is covered. ``.bashrc`` is absent, so it is not a bind."""
    work = tmp_path / "work"
    (work / ".ssh").mkdir(parents=True)
    physical = _physical(work)
    script = _Script()
    await _run(script, monkeypatch, cwd=str(work), workspace_root=str(work))
    assert _bind_mounts(script) == [
        _bind(physical, "rw"),
        _bind(os.path.join(physical, ".ssh"), "ro"),
    ]


async def test_ssh_symlink_is_not_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``.ssh`` symlink is not a cover, and neither is the directory it names."""
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside-ssh"
    outside.mkdir()
    (outside / "authorized_keys").write_text("key\n", encoding="utf-8")
    symlink_or_skip(work / ".ssh", outside, target_is_directory=True)
    (work / ".gitconfig").write_text("name = test\n", encoding="utf-8")
    physical = _physical(work)
    script = _Script()
    await _run(script, monkeypatch, cwd=str(work), workspace_root=str(work))
    assert _bind_mounts(script) == [
        _bind(physical, "rw"),
        _bind(os.path.join(physical, ".gitconfig"), "ro"),
    ]


async def test_sensitive_path_outside_the_workspace_is_not_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``work-extra`` is a sibling of ``work``. Its account files are not covers."""
    work = tmp_path / "work"
    work.mkdir()
    (work / ".gitconfig").write_text("name = test\n", encoding="utf-8")
    sibling = tmp_path / "work-extra"
    (sibling / ".ssh").mkdir(parents=True)
    (sibling / ".gitconfig").write_text("name = other\n", encoding="utf-8")
    physical = _physical(work)
    script = _Script()
    await _run(script, monkeypatch, cwd=str(work), workspace_root=str(work))
    assert _bind_mounts(script) == [
        _bind(physical, "rw"),
        _bind(os.path.join(physical, ".gitconfig"), "ro"),
    ]


async def test_realpath_outside_the_mount_is_not_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A path whose real location is ``work-extra`` is not inside ``work``.

    Component comparison rejects that sibling. A string prefix of ``work``
    would accept ``work-extra``.
    """
    work = tmp_path / "work"
    (work / ".ssh").mkdir(parents=True)
    sibling = tmp_path / "work-extra" / ".ssh"
    sibling.mkdir(parents=True)
    physical = _physical(work)
    ssh_real = os.path.realpath(os.path.join(physical, ".ssh"))
    sibling_real = os.path.realpath(sibling)
    ssh_key = os.path.normcase(ssh_real)
    real_realpath = os.path.realpath

    def fake_realpath(path: str) -> str:
        resolved = real_realpath(path)
        if os.path.normcase(resolved) == ssh_key:
            return sibling_real
        return resolved

    monkeypatch.setattr(backend.os.path, "realpath", fake_realpath)
    script = _Script()
    await _run(script, monkeypatch, cwd=str(work), workspace_root=str(work))
    assert _bind_mounts(script) == [_bind(physical, "rw")]


async def test_cover_uses_the_path_under_the_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The bind is the path under the mount, not a different realpath spelling."""
    work = tmp_path / "work"
    work.mkdir()
    (work / ".gitconfig").write_text("name = test\n", encoding="utf-8")
    physical = _physical(work)
    candidate = os.path.join(physical, ".gitconfig")
    spelled = os.path.join(physical, ".gitconfig.physical")
    candidate_key = os.path.normcase(os.path.realpath(candidate))
    real_realpath = os.path.realpath

    def fake_realpath(path: str) -> str:
        resolved = real_realpath(path)
        if os.path.normcase(resolved) == candidate_key:
            return spelled
        return resolved

    monkeypatch.setattr(backend.os.path, "realpath", fake_realpath)
    script = _Script()
    await _run(script, monkeypatch, cwd=str(work), workspace_root=str(work))
    assert _bind_mounts(script) == [_bind(physical, "rw"), _bind(candidate, "ro")]


async def test_symlink_config_child_is_not_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``.config`` as a symlink is not followed, even when ``gh`` is inside the mount."""
    work = tmp_path / "work"
    actual = work / "actual-config"
    (actual / "gh").mkdir(parents=True)
    symlink_or_skip(work / ".config", actual, target_is_directory=True)
    physical = _physical(work)
    script = _Script()
    await _run(script, monkeypatch, cwd=str(work), workspace_root=str(work))
    assert _bind_mounts(script) == [_bind(physical, "rw")]


async def test_config_children_are_covered_without_the_parent_or_git_hooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``.config/gh`` and ``.config/git`` are covers. ``.config`` and ``.git/hooks`` are not."""
    work = tmp_path / "work"
    (work / ".config" / "gh").mkdir(parents=True)
    (work / ".config" / "git").mkdir(parents=True)
    (work / ".config" / "other").mkdir(parents=True)
    (work / ".git" / "hooks").mkdir(parents=True)
    physical = _physical(work)
    script = _Script()
    await _run(script, monkeypatch, cwd=str(work), workspace_root=str(work))
    assert _bind_mounts(script) == [
        _bind(physical, "rw"),
        _bind(os.path.join(physical, ".config", "gh"), "ro"),
        _bind(os.path.join(physical, ".config", "git"), "ro"),
    ]


async def test_read_only_sandbox_does_not_add_sensitive_covers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cwd outside the workspace is read-only and does not gain account covers."""
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    (outside / ".ssh").mkdir(parents=True)
    (outside / ".gitconfig").write_text("name = test\n", encoding="utf-8")
    script = _Script()
    await _run(script, monkeypatch, cwd=str(outside), workspace_root=str(work))
    assert _bind_mounts(script) == [_bind(_physical(outside), "ro")]


async def test_legacy_writable_cwd_covers_sensitive_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The single-cwd writable create covers account paths under that cwd."""
    work = tmp_path / "legacy"
    work.mkdir()
    (work / ".bashrc").write_text("echo hi\n", encoding="utf-8")
    cwd = str(work)
    script = _Script()
    await _run(script, monkeypatch, cwd=cwd)
    assert _bind_mounts(script) == [
        _bind(cwd, "rw"),
        _bind(os.path.join(cwd, ".bashrc"), "ro"),
    ]


async def test_each_writable_mount_covers_its_own_sensitive_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every read-write mount appends covers for the account paths inside it."""
    work = tmp_path / "work"
    work.mkdir()
    (work / ".gitconfig").write_text("name = test\n", encoding="utf-8")
    extra = tmp_path / "extra"
    (extra / ".ssh").mkdir(parents=True)
    physical_work = _physical(work)
    physical_extra = _physical(extra)
    script = _Script()
    await _run(
        script,
        monkeypatch,
        cwd=str(work),
        workspace_root=str(work),
        workspace_dirs=(str(extra),),
    )
    assert _bind_mounts(script) == [
        _bind(physical_work, "rw"),
        _bind(physical_extra, "rw"),
        _bind(os.path.join(physical_work, ".gitconfig"), "ro"),
        _bind(os.path.join(physical_extra, ".ssh"), "ro"),
    ]


def _recorded_binds(argv: list[str]) -> list[tuple[str, str, str]]:
    recorded: list[tuple[str, str, str]] = []
    index = 0
    while index < len(argv):
        flag = argv[index]
        if flag in {"--ro-bind", "--bind", "--dev-bind"} and index + 2 < len(argv):
            recorded.append((flag, argv[index + 1], argv[index + 2]))
            index += 3
            continue
        index += 1
    return recorded


def test_parent_read_write_bind_precedes_child_read_only_bind() -> None:
    """Non-root binds are shallowest first, so a parent ``--bind`` cannot hide a child ``--ro-bind``."""
    from jiuwenbox.supervisor.bwrap import BwrapConfig

    config = BwrapConfig(
        command=["/bin/true"],
        rw_binds=[
            ("/home/user", "/home/user"),
            ("/opt/work", "/opt/work"),
        ],
        ro_binds=[
            ("/home", "/home"),
            ("/var/cache", "/var/cache"),
            ("/home/user/.ssh", "/home/user/.ssh"),
        ],
        device_binds=[("/dev/null", "/dev/null")],
    )
    argv = config.to_args()
    assert argv.index("--proc") < argv.index("--dev") < argv.index("--ro-bind")
    assert _recorded_binds(argv) == [
        ("--ro-bind", "/home", "/home"),
        ("--bind", "/home/user", "/home/user"),
        ("--bind", "/opt/work", "/opt/work"),
        ("--ro-bind", "/var/cache", "/var/cache"),
        ("--ro-bind", "/home/user/.ssh", "/home/user/.ssh"),
        ("--dev-bind", "/dev/null", "/dev/null"),
    ]
