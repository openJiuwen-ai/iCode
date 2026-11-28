# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only covers for account files inside a writable sandbox mount.

HTTP is mocked. Bubblewrap is not started; the argv check builds ``BwrapConfig``
and reads ``to_args``.
"""

from __future__ import annotations

import json
import os
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest

import chrys.service.sandbox.backend as backend
from chrys.service.sandbox.backend import SandboxExec, run_shell
from tests.support.symlinks import symlink_or_skip


@dataclass(frozen=True, slots=True)
class _Settings:
    """Stand-in with the four sandbox fields. Not the Settings class."""

    sandbox_enabled: bool = True
    sandbox_url: str = "http://sandbox.test"
    sandbox_token: str = ""
    sandbox_network: str = "guarded"


class _Script:
    def __init__(self) -> None:
        self.events: list[tuple[str, str, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body: object = json.loads(request.content) if request.content else None
        self.events.append((request.method, request.url.path, body))
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok", "features": ["hidden_host_paths"]})
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
    assert set(filesystem) == {"bind_mounts", "hidden_host_paths"}
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


async def test_outside_workdir_does_not_bind_that_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outside cwd is not a mount, so its account files are not covers."""
    work = tmp_path / "work"
    work.mkdir()
    script = _Script()
    await _run(script, monkeypatch, cwd="/opt/chrys-sandbox-outside", workspace_root=str(work))
    assert _bind_mounts(script) == [_bind(_physical(work), "rw")]


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


async def test_chrys_directory_is_covered_and_the_engine_home_is_left_to_the_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``.chrys`` is read-only. ``.jiuwenbox`` gets no cover: the server hides its own home and refuses one."""
    work = tmp_path / "work"
    (work / ".chrys").mkdir(parents=True)
    (work / ".jiuwenbox").mkdir()
    physical = _physical(work)
    script = _Script()
    await _run(script, monkeypatch, cwd=str(work), workspace_root=str(work))
    assert _bind_mounts(script) == [
        _bind(physical, "rw"),
        _bind(os.path.join(physical, ".chrys"), "ro"),
    ]


async def test_account_control_dirs_under_an_ancestor_mount_are_covered(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/home/user/.chrys`` is covered when the writable mount is ``/home``."""
    home = tmp_path / "home"
    user = home / "user"
    (user / ".chrys").mkdir(parents=True)
    monkeypatch.setattr(backend, "_icode_config_dir", lambda: str(user / ".chrys"))
    physical = _physical(home)
    script = _Script()
    await _run(script, monkeypatch, cwd=str(home), workspace_root=str(home))
    assert _bind_mounts(script) == [
        _bind(physical, "rw"),
        _bind(os.path.join(physical, "user", ".chrys"), "ro"),
    ]


def test_icode_config_dir_is_the_platform_config_dir() -> None:
    """Not ``Path.home() / ".chrys"``: the config directory can live elsewhere."""
    from chrys.foundation.platform import get_platform

    assert backend._icode_config_dir() == str(get_platform().config_dir)
    assert backend._icode_config_dir() != str(Path.home() / ".chrys")


def _hidden_paths(script: _Script) -> object:
    bodies = [body for method, path, body in script.events if method == "POST" and path == "/api/v1/sandboxes"]
    assert len(bodies) == 1
    body = bodies[0]
    assert isinstance(body, dict)
    return body["policy"]["filesystem_policy"]["hidden_host_paths"]


async def test_create_names_the_config_dir_and_session_store_as_hidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both real paths are sent, whatever the mounts, for the server to cover with a tmpfs."""
    from chrys.foundation.config.settings import resolve_sessions_dir
    from chrys.foundation.platform import get_platform

    work = tmp_path / "work"
    work.mkdir()
    script = _Script()
    await _run(script, monkeypatch, cwd=str(work), workspace_root=str(work))
    config_dir = os.path.realpath(get_platform().config_dir)
    sessions = os.path.realpath(resolve_sessions_dir(create=False))
    assert not sessions.startswith(config_dir + os.sep)
    assert _hidden_paths(script) == [config_dir, sessions]


async def test_a_session_store_inside_the_config_dir_is_not_named_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    (config_dir / "sessions").mkdir(parents=True)
    monkeypatch.setattr(backend, "_icode_config_dir", lambda: str(config_dir))
    monkeypatch.setattr(
        "chrys.foundation.config.settings.resolve_sessions_dir",
        lambda *, create: config_dir / "sessions",
    )
    work = tmp_path / "work"
    work.mkdir()
    script = _Script()
    await _run(script, monkeypatch, cwd=str(work), workspace_root=str(work))
    assert _hidden_paths(script) == [os.path.realpath(config_dir)]


async def test_missing_account_control_dir_is_not_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A control directory that does not exist is skipped."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(backend, "_icode_config_dir", lambda: str(tmp_path / "missing-chrys"))
    script = _Script()
    await _run(script, monkeypatch, cwd=str(work), workspace_root=str(work))
    assert _bind_mounts(script) == [_bind(_physical(work), "rw")]


async def test_account_control_dir_outside_the_mount_is_not_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real account directory is not a cover when it sits outside the mount."""
    work = tmp_path / "work"
    work.mkdir()
    sibling = tmp_path / "sibling"
    (sibling / ".chrys").mkdir(parents=True)
    monkeypatch.setattr(backend, "_icode_config_dir", lambda: str(sibling / ".chrys"))
    script = _Script()
    await _run(script, monkeypatch, cwd=str(work), workspace_root=str(work))
    assert _bind_mounts(script) == [_bind(_physical(work), "rw")]


async def test_account_control_symlink_that_leaves_the_mount_is_not_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symlink from the mount onto a directory outside it is not followed."""
    home = tmp_path / "home"
    home.mkdir()
    outside = tmp_path / "outside-user"
    (outside / ".chrys").mkdir(parents=True)
    link = home / "user"
    symlink_or_skip(link, outside, target_is_directory=True)
    monkeypatch.setattr(backend, "_icode_config_dir", lambda: str(link / ".chrys"))
    script = _Script()
    await _run(script, monkeypatch, cwd=str(home), workspace_root=str(home))
    assert _bind_mounts(script) == [_bind(_physical(home), "rw")]


async def test_account_control_dir_is_not_bound_twice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The relative cover and the account-directory cover of the same path are one bind."""
    work = tmp_path / "work"
    (work / ".chrys").mkdir(parents=True)
    physical = _physical(work)
    covered = os.path.join(physical, ".chrys")
    monkeypatch.setattr(backend, "_icode_config_dir", lambda: covered)
    script = _Script()
    await _run(script, monkeypatch, cwd=str(work), workspace_root=str(work))
    assert _bind_mounts(script) == [_bind(physical, "rw"), _bind(covered, "ro")]


def _platform_named(platform: str) -> types.ModuleType:
    shadow = types.ModuleType("sys")
    shadow.platform = platform
    return shadow


def test_password_database_home_prefers_pw_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """On POSIX ``pw_dir`` wins over ``Path.home()``."""
    fake = types.ModuleType("pwd")

    class _Record:
        pw_dir = "/srv/pw-home"

    fake.getpwuid = lambda _uid: _Record()
    monkeypatch.setitem(sys.modules, "pwd", fake)
    monkeypatch.setattr(backend, "sys", _platform_named("linux"))
    monkeypatch.setattr(backend.os, "geteuid", lambda: 0, raising=False)
    assert backend._password_database_home() == "/srv/pw-home"


def test_password_database_home_on_windows_is_path_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def refuse(_uid: int) -> NoReturn:
        raise AssertionError("pwd is not consulted on Windows")

    fake = types.ModuleType("pwd")
    fake.getpwuid = refuse
    monkeypatch.setitem(sys.modules, "pwd", fake)
    monkeypatch.setattr(backend, "sys", _platform_named("win32"))
    monkeypatch.setattr(backend.Path, "home", classmethod(lambda _cls: tmp_path))
    assert backend._password_database_home() == str(tmp_path)


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
