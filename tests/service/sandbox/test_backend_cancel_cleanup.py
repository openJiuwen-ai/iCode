# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What a cancelled sandbox call leaves behind.

A cancel during create, after an id is known, deletes that id. A cancel before
any id is known deletes nothing. A cancelled exec keeps the writable sandbox.


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


def _is_start(request: httpx.Request) -> bool:
    return request.method == "POST" and request.url.path.endswith("/start")


def _is_delete(request: httpx.Request) -> bool:
    return request.method == "DELETE"


async def test_cancel_after_create_returns_an_id_deletes_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Once an id is known and not cached, cancel DELETEs it. A second cancel still does."""
    work = tmp_path / "work"
    work.mkdir()
    script = _Script()
    script.create_phase = "created"
    script.distinct_ids = True
    release = asyncio.Event()
    transport = _HoldingTransport(
        script,
        release,
        hold_when=lambda request: _is_start(request) or _is_delete(request),
    )
    _install_transport(monkeypatch, transport)
    task = asyncio.create_task(_run_workspace(_Settings(), cwd=str(work), workspace_root=str(work)))
    try:
        await wait_for(lambda: transport.held == 1 or task.done(), description="start is in flight")
        if task.done():
            await task
        assert transport.held == 1
        task.cancel()
        await wait_for(lambda: transport.held == 2 or task.done(), description="delete is in flight")
        if task.done():
            await task
        assert transport.held == 2
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await _cancel_pending(task)
    assert _create_bodies(script) == [_mount_body(_physical(work), "rw")]
    assert _deleted(script) == ["/api/v1/sandboxes/sb-1"]
    assert _exec_ids(script) == []
    assert _no_stop(script)
    assert backend._writable_by_root == {}
    assert backend._sandbox_id is None
    assert backend._in_flight == {}
    assert backend._retiring == set()


async def test_cancel_before_create_returns_an_id_deletes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A cancel before the create response has no id, so the client deletes nothing."""
    work = tmp_path / "work"
    work.mkdir()
    script = _Script()
    release = asyncio.Event()

    def _is_create(request: httpx.Request) -> bool:
        return request.method == "POST" and request.url.path == "/api/v1/sandboxes"

    transport = _HoldingTransport(script, release, hold_when=_is_create)
    _install_transport(monkeypatch, transport)
    task = asyncio.create_task(_run_workspace(_Settings(), cwd=str(work), workspace_root=str(work)))
    try:
        await wait_for(lambda: transport.held == 1 or task.done(), description="create is in flight")
        if task.done():
            await task
        assert transport.held == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await _cancel_pending(task)
    assert _create_bodies(script) == []
    assert _deleted(script) == []
    assert _exec_ids(script) == []
    assert _no_stop(script)
    assert backend._writable_by_root == {}
    assert backend._sandbox_id is None


async def test_cancelled_writable_exec_never_deletes_the_workspace_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Cancel keeps a cached writable sandbox, including when the cwd is outside it."""
    work = tmp_path / "work"
    work.mkdir()
    outside = "/opt/chrys-sandbox-outside"
    physical_work = _physical(work)
    script = _Script()
    script.distinct_ids = True
    release = asyncio.Event()
    transport = _HoldingTransport(script, release, hold_when=_is_exec)
    _install_transport(monkeypatch, transport)
    settings = _Settings()
    tasks: list[asyncio.Task[SandboxExec]] = []
    try:
        for index, cwd in enumerate((str(work), str(work), outside)):
            task = asyncio.create_task(
                _run_workspace(settings, argv=("echo", str(index)), cwd=cwd, workspace_root=str(work))
            )
            tasks.append(task)
            await _cancel_once_in_exec(task, transport, index + 1)
            assert backend._writable_by_root == {physical_work: "sb-1"}
    finally:
        release.set()
        await _cancel_pending(*tasks)
    assert _create_bodies(script) == [_mount_body(physical_work, "rw")]
    assert _deleted(script) == []
    assert _no_stop(script)
    followed = await _run_workspace(settings, argv=("echo", "again"), cwd=str(work), workspace_root=str(work))
    assert followed.exit_code == 0
    assert _exec_ids(script) == ["sb-1"]
    assert _deleted(script) == []
    assert backend._writable_by_root == {physical_work: "sb-1"}
    assert backend._in_flight == {}
    assert backend._retiring == set()
