# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A workspace whose mount set changes gets a new sandbox; the old one is deleted after its last exec.

HTTP is mocked; nothing listens and nothing is spawned.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chrys.service.sandbox import backend
from chrys.service.sandbox.backend import SandboxError, SandboxExec
from tests.service.sandbox.test_backend import (
    _SPAWN_CALLS,
    _cancel_pending,
    _create_bodies,
    _deleted,
    _exec_ids,
    _exec_workdirs,
    _forbid_host_spawn,
    _install,
    _install_transport,
    _is_exec,
    _mount_body,
    _mount_body_paths,
    _paths,
    _PausingTransport,
    _physical,
    _run_workspace,
    _Script,
    _Settings,
)
from tests.support.waiting import wait_for


@pytest.fixture(autouse=True)
def _forbid_supervisor_and_host_spawn(monkeypatch: pytest.MonkeyPatch):
    _forbid_host_spawn(monkeypatch)


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
