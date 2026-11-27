# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A cwd under a protected tree outside the workspace is refused without a bind.

HTTP is mocked; nothing listens and nothing is spawned.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from chrys.service.sandbox import backend
from chrys.service.sandbox.backend import SandboxError, run_shell
from tests.service.sandbox.test_backend import (
    _SPAWN_CALLS,
    _create_bodies,
    _exec_ids,
    _exec_workdirs,
    _forbid_host_spawn,
    _install,
    _mount_body,
    _run_workspace,
    _Script,
    _Settings,
)
from tests.support.symlinks import symlink_or_skip

_OUTSIDE_WORKDIR = "Sandbox command working directory is outside the workspace and was not mounted."


@pytest.fixture(autouse=True)
def _forbid_supervisor_and_host_spawn(monkeypatch: pytest.MonkeyPatch):
    _forbid_host_spawn(monkeypatch)


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


_HIDDEN_WORKDIR = (
    "Sandbox command working directory is inside the iCode config or session directory, which the sandbox hides."
)


def _config_dir_in(home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = home / ".chrys"
    (config / "skills").mkdir(parents=True)
    monkeypatch.setattr(backend, "_icode_config_dir", lambda: os.path.realpath(config))
    return config


async def test_cwd_in_the_config_dir_under_a_home_workspace_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The home mount shows the config directory only as an empty tmpfs, so the cwd would not exist."""
    home = tmp_path / "home"
    config = _config_dir_in(home, monkeypatch)
    script = _Script()
    _install(monkeypatch, script)
    with pytest.raises(SandboxError) as caught:
        await _run_workspace(_Settings(), cwd=str(config / "skills"), workspace_root=str(home))
    assert str(caught.value) == _HIDDEN_WORKDIR
    assert _create_bodies(script) == []
    assert _exec_ids(script) == []


_SERVER_CANNOT_HIDE = (
    "The sandbox service at sandbox.url cannot hide the iCode config and session directories, and this "
    "workspace contains one of them. Update the sandbox service, or use a workspace that does not contain them."
)


async def test_a_service_that_cannot_hide_directories_is_refused_a_workspace_holding_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    _config_dir_in(home, monkeypatch)
    script = _Script()
    script.health = {"status": "ok"}
    _install(monkeypatch, script)
    with pytest.raises(SandboxError) as caught:
        await _run_workspace(_Settings(), cwd=str(home), workspace_root=str(home))
    assert str(caught.value) == _SERVER_CANNOT_HIDE
    assert _create_bodies(script) == []


async def test_a_service_that_cannot_hide_directories_still_runs_a_workspace_without_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _config_dir_in(tmp_path / "home", monkeypatch)
    work = tmp_path / "work"
    work.mkdir()
    script = _Script()
    script.health = {"status": "ok", "features": []}
    _install(monkeypatch, script)
    result = await _run_workspace(_Settings(), cwd=str(work), workspace_root=str(work))
    assert result.exit_code == 0
    assert _exec_workdirs(script) == [os.path.realpath(work)]


async def test_a_workspace_inside_the_config_dir_keeps_its_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A mount that starts below the hidden directory is a bind of its own and stays visible."""
    config = _config_dir_in(tmp_path / "home", monkeypatch)
    skills = config / "skills"
    script = _Script()
    _install(monkeypatch, script)
    result = await _run_workspace(_Settings(), cwd=str(skills), workspace_root=str(skills))
    assert result.exit_code == 0
    assert _exec_workdirs(script) == [os.path.realpath(skills)]


async def test_the_config_dir_itself_as_the_workspace_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config = _config_dir_in(tmp_path / "home", monkeypatch)
    script = _Script()
    _install(monkeypatch, script)
    with pytest.raises(SandboxError) as caught:
        await _run_workspace(_Settings(), cwd=str(config), workspace_root=str(config))
    assert str(caught.value) == _HIDDEN_WORKDIR
    assert _create_bodies(script) == []


async def test_legacy_cwd_in_the_config_dir_under_the_writable_root_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    home = tmp_path / "home"
    config = _config_dir_in(home, monkeypatch)
    script = _Script()
    _install(monkeypatch, script)
    settings = _Settings()
    first = await run_shell(settings, argv=("echo", "one"), cwd=str(home), timeout_seconds=12, linux=True)
    assert first.exit_code == 0
    with pytest.raises(SandboxError) as caught:
        await run_shell(settings, argv=("echo", "two"), cwd=str(config / "skills"), timeout_seconds=12, linux=True)
    assert str(caught.value) == _HIDDEN_WORKDIR
    assert len(_exec_ids(script)) == 1


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
