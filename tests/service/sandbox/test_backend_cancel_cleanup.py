# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What a cancelled exec leaves behind: read-only sandboxes are deleted, the writable one stays.

HTTP is mocked; nothing listens and nothing is spawned.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from chrys.service.sandbox import backend
from chrys.service.sandbox.backend import SandboxExec
from tests.service.sandbox.test_backend import (
    _cancel_pending,
    _create_bodies,
    _deleted,
    _exec_ids,
    _install_transport,
    _is_exec,
    _mount_body,
    _paths,
    _physical,
    _run_workspace,
    _Script,
    _Settings,
)
from tests.support.waiting import wait_for


class _HoldingTransport(httpx.AsyncBaseTransport):
    """Holds every matching request until ``release``; ``held`` counts them."""

    def __init__(self, script: _Script, release: asyncio.Event, *, hold_when: Callable[[httpx.Request], bool]) -> None:
        self._script = script
        self._release = release
        self._hold_when = hold_when
        self.held = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._hold_when(request):
            self.held += 1
            await self._release.wait()
        return self._script.handler(request)


async def _cancel_once_in_exec(task: asyncio.Task[SandboxExec], transport: _HoldingTransport, held: int) -> None:
    """Wait until ``task``'s exec is held, cancel it, and see the cancellation through."""
    await wait_for(lambda: transport.held == held or task.done(), description="exec is in flight")
    if task.done():
        await task
    assert transport.held == held
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def _no_stop(script: _Script) -> bool:
    return not any(path.endswith("/stop") for _method, path in _paths(script))


async def test_cancelled_readonly_commands_each_delete_their_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A read-only sandbox forgotten on cancel is deleted, not left idle. Nothing is stopped."""
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    script = _Script()
    script.distinct_ids = True
    release = asyncio.Event()
    transport = _HoldingTransport(script, release, hold_when=_is_exec)
    _install_transport(monkeypatch, transport)
    settings = _Settings()
    tasks: list[asyncio.Task[SandboxExec]] = []
    try:
        for index in range(3):
            task = asyncio.create_task(
                _run_workspace(settings, argv=("echo", str(index)), cwd=str(outside), workspace_root=str(work))
            )
            tasks.append(task)
            await _cancel_once_in_exec(task, transport, index + 1)
            assert backend._readonly_id is None
    finally:
        release.set()
        await _cancel_pending(*tasks)
    assert _create_bodies(script) == [_mount_body(_physical(outside), "ro")] * 3
    assert _exec_ids(script) == []
    assert _deleted(script) == [
        "/api/v1/sandboxes/sb-1",
        "/api/v1/sandboxes/sb-2",
        "/api/v1/sandboxes/sb-3",
    ]
    assert _no_stop(script)
    assert backend._writable_by_root == {}
    assert backend._in_flight == {}
    assert backend._retiring == set()


async def test_cancelled_readonly_exec_keeps_its_sandbox_for_another_running_exec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The forgotten read-only sandbox is deleted once, after the other exec in it ends."""
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    script = _Script()
    script.distinct_ids = True
    release = asyncio.Event()
    transport = _HoldingTransport(script, release, hold_when=_is_exec)
    _install_transport(monkeypatch, transport)
    settings = _Settings()
    task_a = asyncio.create_task(
        _run_workspace(settings, argv=("echo", "a"), cwd=str(outside), workspace_root=str(work))
    )
    task_b: asyncio.Task[SandboxExec] | None = None
    try:
        await wait_for(lambda: transport.held == 1 or task_a.done(), description="first exec is in flight")
        if task_a.done():
            await task_a
        task_b = asyncio.create_task(
            _run_workspace(settings, argv=("echo", "b"), cwd=str(outside), workspace_root=str(work))
        )
        await _cancel_once_in_exec(task_a, transport, 2)
        assert not task_b.done()
        assert _deleted(script) == []
        assert backend._readonly_id is None
        assert backend._in_flight == {"sb-1": 1}
        assert backend._retiring == {"sb-1"}
        release.set()
        second = await task_b
    finally:
        release.set()
        await _cancel_pending(task_a, task_b)
    assert second.exit_code == 0
    assert _create_bodies(script) == [_mount_body(_physical(outside), "ro")]
    assert _exec_ids(script) == ["sb-1"]
    assert _deleted(script) == ["/api/v1/sandboxes/sb-1"]
    assert _paths(script).index(("DELETE", "/api/v1/sandboxes/sb-1")) > _paths(script).index(
        ("POST", "/api/v1/sandboxes/sb-1/exec")
    )
    assert _no_stop(script)
    assert backend._in_flight == {}
    assert backend._retiring == set()


async def test_cancelled_writable_exec_never_deletes_the_workspace_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Cancel keeps the workspace sandbox cached and alive, while a cancelled read-only one goes."""
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    physical_work = _physical(work)
    script = _Script()
    script.distinct_ids = True
    release = asyncio.Event()
    transport = _HoldingTransport(script, release, hold_when=_is_exec)
    _install_transport(monkeypatch, transport)
    settings = _Settings()
    tasks: list[asyncio.Task[SandboxExec]] = []
    try:
        for index, cwd in enumerate((work, work, outside)):
            task = asyncio.create_task(
                _run_workspace(settings, argv=("echo", str(index)), cwd=str(cwd), workspace_root=str(work))
            )
            tasks.append(task)
            await _cancel_once_in_exec(task, transport, index + 1)
            assert backend._writable_by_root == {physical_work: "sb-1"}
    finally:
        release.set()
        await _cancel_pending(*tasks)
    assert _create_bodies(script) == [_mount_body(physical_work, "rw"), _mount_body(_physical(outside), "ro")]
    assert _deleted(script) == ["/api/v1/sandboxes/sb-2"]
    assert _no_stop(script)
    followed = await _run_workspace(settings, argv=("echo", "again"), cwd=str(work), workspace_root=str(work))
    assert followed.exit_code == 0
    assert _exec_ids(script) == ["sb-1"]
    assert _deleted(script) == ["/api/v1/sandboxes/sb-2"]
    assert backend._writable_by_root == {physical_work: "sb-1"}
    assert backend._in_flight == {}
    assert backend._retiring == set()
