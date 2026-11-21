# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from icode_browser_use.actions._shared import (
    capture_and_register,
    capture_sources,
    gather_in_order,
    send_watching_dialog,
)
from icode_browser_use.dialog import DialogInfo
from icode_browser_use.errors import CDPTimeout, ConnectionClosed
from icode_browser_use.snapshot.fusion import FrameInfo


class FakeSession:
    def __init__(self) -> None:
        self.dialog: DialogInfo | None = None
        self.checks = 0

    def dialog_since(self, seq: int) -> DialogInfo | None:
        self.checks += 1
        if self.dialog is not None and self.dialog.seq > seq:
            return self.dialog
        return None


def test_a_send_that_finishes_first_returns_its_value() -> None:
    session = FakeSession()

    async def send() -> str:
        return "ok"

    value, dialog = asyncio.run(send_watching_dialog(session, 0, send))
    assert (value, dialog) == ("ok", None)


def test_a_send_error_propagates() -> None:
    session = FakeSession()

    async def send() -> str:
        raise ConnectionClosed("cdp gone")

    with pytest.raises(ConnectionClosed):
        asyncio.run(send_watching_dialog(session, 0, send))


def test_a_dialog_releases_the_action_while_the_send_is_still_in_flight() -> None:
    session = FakeSession()
    started = asyncio.Event()
    resumed = asyncio.Event()
    finished: list[bool] = []

    async def send() -> str:
        started.set()
        await resumed.wait()  # the renderer is paused until the dialog is handled
        finished.append(True)
        return "late"

    async def scenario() -> tuple[str | None, DialogInfo | None]:
        task = asyncio.ensure_future(send_watching_dialog(session, 0, send))
        await started.wait()
        session.dialog = DialogInfo(seq=1, kind="alert", message="paused")
        value, dialog = await asyncio.wait_for(task, 2)
        assert not finished, "the action must not wait for the detached command"
        resumed.set()
        await asyncio.sleep(0)
        return value, dialog

    value, dialog = asyncio.run(scenario())
    assert value is None
    assert dialog is not None and dialog.kind == "alert"


def test_cancelling_the_watch_cancels_the_send() -> None:
    session = FakeSession()
    started = asyncio.Event()
    cancelled: list[bool] = []

    async def send() -> str:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return "never"

    async def scenario() -> None:
        task = asyncio.ensure_future(send_watching_dialog(session, 0, send))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert cancelled == [True]

    asyncio.run(scenario())


class SlowClient:
    """Records how many commands are in flight at once."""

    def __init__(self, failing: set[str] = frozenset()) -> None:
        self.in_flight = 0
        self.peak = 0
        self.failing = failing

    async def send(self, method, params=None, *, session_id=None, timeout=None):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            if method in self.failing:
                raise CDPTimeout(method, 0.01)
            if method == "DOMSnapshot.captureSnapshot":
                return {"documents": [], "strings": []}
            return {"nodes": []}
        finally:
            self.in_flight -= 1


def _capture_session(client: SlowClient) -> SimpleNamespace:
    async def read_frame_metrics():
        await client.send("Runtime.evaluate")
        return FrameInfo(frame_id="main")

    return SimpleNamespace(
        require_client=lambda: client,
        session_id="page",
        cfg=SimpleNamespace(cdp_timeout=1),
        read_frame_metrics=read_frame_metrics,
    )


def test_capture_sends_the_main_frame_reads_together() -> None:
    client = SlowClient()
    session = _capture_session(client)
    snapshot, ax = asyncio.run(capture_sources(session))
    assert snapshot == {"documents": [], "strings": []} and ax == {"nodes": []}
    assert client.peak == 2

    client = SlowClient()
    session = _capture_session(client)
    session.target_id = "target"
    session.snapshot = SimpleNamespace(url="https://example.com/")
    session.register_snapshot = lambda *_args, **_kwargs: None
    nodes, frame = asyncio.run(capture_and_register(session))
    assert nodes == [] and frame.frame_id == "main"
    assert client.peak == 3, "DOMSnapshot, AX tree and frame metrics share one round trip"


def test_capture_survives_an_ax_failure_but_not_a_dom_failure() -> None:
    session = _capture_session(SlowClient(failing={"Accessibility.getFullAXTree"}))
    snapshot, ax = asyncio.run(capture_sources(session))
    assert snapshot == {"documents": [], "strings": []} and ax is None

    session = _capture_session(SlowClient(failing={"DOMSnapshot.captureSnapshot"}))
    with pytest.raises(CDPTimeout):
        asyncio.run(capture_sources(session))

def test_gather_in_order_leaves_nothing_running_and_keeps_serial_error_priority() -> None:
    finished: list[str] = []

    async def slow_ok() -> str:
        await asyncio.sleep(0.02)
        finished.append("slow")
        return "ok"

    async def fast_fail() -> str:
        raise CDPTimeout("Runtime.evaluate", 0.01)

    async def first_fail() -> str:
        await asyncio.sleep(0.01)
        raise ConnectionClosed("first")

    with pytest.raises(CDPTimeout):
        asyncio.run(gather_in_order(slow_ok(), fast_fail()))
    assert finished == ["slow"], "the sibling must settle before the error is raised"
    with pytest.raises(ConnectionClosed):
        asyncio.run(gather_in_order(first_fail(), fast_fail()))
    assert asyncio.run(gather_in_order(slow_ok(), slow_ok())) == ["ok", "ok"]