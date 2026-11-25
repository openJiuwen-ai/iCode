# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression unit tests for Batch 2 deterministic bug fixes (Q1-Q10)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from icode_browser_use import broker, extension
from icode_browser_use.actions import _shared, click
from icode_browser_use.config import BrowserConfig
from icode_browser_use.errors import BrowserError
from icode_browser_use.bridge.protocol import ProtocolError, encode_message
from icode_browser_use.registry import ManifestRecord
from icode_browser_use.session import BrowserSession
from icode_browser_use.snapshot.fusion import _tag_interactive


@pytest.mark.asyncio
async def test_q1_idle_watchdog_callback_exception_does_not_raise_type_error(tmp_path: Path) -> None:
    cfg = BrowserConfig(state_dir=tmp_path, idle_close_seconds=0.05)

    async def failing_on_idle() -> None:
        raise RuntimeError("boom")

    session = BrowserSession(cfg, on_idle=failing_on_idle)
    session._state = "ready"
    session._last_active = 0.0

    async def fake_shutdown_idle() -> None:
        session._state = "idle"

    session._shutdown_idle = fake_shutdown_idle  # type: ignore[method-assign]
    # Must complete cleanly without raising TypeError from log() call.
    await session._idle_watchdog()
    assert session._state == "idle"


def test_q2_chrome_module_has_main_entrypoint(tmp_path: Path) -> None:
    env = {
        **os.environ,
        "ICODE_BROWSER_STATE_DIR": str(tmp_path),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHONUTF8": "1",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "icode_browser_use.chrome", "status"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() != ""
    assert "icode browser" not in proc.stdout


@pytest.mark.asyncio
async def test_q3_send_watching_dialog_cancels_task_and_flushes_pending_release() -> None:
    events: list[tuple[str, dict[str, Any]]] = []
    press_started = asyncio.Event()
    press_release = asyncio.Event()

    class _FakeClient:
        closed = False

        async def send(self, method: str, params: dict[str, Any], **_kw: Any) -> dict[str, Any]:
            events.append((method, dict(params)))
            if method == "Input.dispatchMouseEvent" and params.get("type") == "mousePressed":
                press_started.set()
                await press_release.wait()
            return {}

    fake_dialog = SimpleNamespace(kind="alert", message="hi", fields=lambda: "dialog=alert")
    dialog_holder: list[Any] = [None]
    session = SimpleNamespace(
        _client=_FakeClient(),
        require_client=lambda: session._client,
        session_id="sid-1",
        cfg=SimpleNamespace(cdp_timeout=5.0),
        _pending_input_releases=[],
        dialog_since=lambda _seq: dialog_holder[0],
    )

    async def trigger_dialog() -> None:
        await press_started.wait()
        dialog_holder[0] = fake_dialog

    trigger = asyncio.create_task(trigger_dialog())
    _, seen = await _shared.send_watching_dialog(
        session, 0, lambda: click._dispatch_click(session, 10.0, 20.0, "left", 1)
    )
    await trigger
    press_release.set()
    await asyncio.sleep(0.02)

    assert seen is fake_dialog
    # mouseReleased must NOT have fired after cancellation
    types_sent = [p.get("type") for m, p in events if m == "Input.dispatchMouseEvent"]
    assert types_sent == ["mouseMoved", "mousePressed"]
    assert len(session._pending_input_releases) == 1

    # Next click flushes the pending mouseReleased before dispatching the new click.
    dialog_holder[0] = None
    events.clear()
    await click._dispatch_click(session, 30.0, 40.0, "left", 1)
    types_after = [p.get("type") for m, p in events if m == "Input.dispatchMouseEvent"]
    assert types_after == ["mouseReleased", "mouseMoved", "mousePressed", "mouseReleased"]
    assert session._pending_input_releases == []


@pytest.mark.asyncio
async def test_q4_eval_and_call_value_raise_on_exception_details_and_dom_click_checks_clickable() -> None:
    class _ExcClient:
        async def send(self, method: str, params: dict[str, Any], **_kw: Any) -> dict[str, Any]:
            return {
                "result": {"type": "object", "subtype": "error"},
                "exceptionDetails": {
                    "text": "Uncaught",
                    "exception": {"description": "TypeError: this.click is not a function"},
                },
            }

    session = SimpleNamespace(
        require_client=lambda: _ExcClient(),
        session_id="sid",
        cfg=SimpleNamespace(evaluate_timeout=5.0),
    )
    with pytest.raises(BrowserError, match="页面脚本异常"):
        await _shared.eval_value(session, "boom()")
    with pytest.raises(BrowserError, match="页面脚本异常"):
        await _shared.call_value(session, "obj-1", "function(){ this.click(); }")

    fake_session = SimpleNamespace(page_epoch=1, journal=SimpleNamespace(rows_for=lambda *_a: []))
    fake_entry = SimpleNamespace(token="@e1#1")
    refusal = click._dom_click_allowed(
        fake_session, fake_entry, {"tag": "svg", "role": "button", "clickable": False}, False
    )
    assert refusal is not None
    assert "原生 click()" in refusal


def test_q5_is_submit_like_respects_submit_like_fact() -> None:
    # Bare <button> outside <form> has type="submit" in DOM, but submitLike=False
    assert click._is_submit_like({"tag": "button", "type": "submit", "submitLike": False, "name": "Toggle"}) is False
    # Inside <form> -> submitLike=True
    assert click._is_submit_like({"tag": "button", "type": "submit", "submitLike": True, "name": "Toggle"}) is True
    # Submit word in label still counts
    assert click._is_submit_like({"tag": "button", "type": "button", "submitLike": False, "name": "Save"}) is True


def test_q6_hidden_input_not_interactive() -> None:
    assert _tag_interactive("input", {"type": "hidden"}) is False
    assert _tag_interactive("input", {"type": "HIDDEN"}) is False
    assert _tag_interactive("input", {"type": "text"}) is True
    assert _tag_interactive("input", {}) is True


@pytest.mark.asyncio
async def test_q7_close_and_forget_cleans_up_on_aclose_error_and_preserves_corrupt_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "browsers.json"
    manifest.write_text("{not valid json", encoding="utf-8")

    class _FailingSession:
        owns_state_dir = False

        async def aclose(self) -> None:
            raise RuntimeError("close failed")

    record = ManifestRecord(
        schema=1,
        browser_id="b-q7",
        resume_id="res-1",
        session_id="sess",
        agent_name="Code",
        owner_key="owner",
        backend="owned",
        generation="gen-1",
        slot=0,
        origin="",
        updated_at=0.0,
    )
    browser = extension._Browser(
        id="b-q7",
        session=_FailingSession(),  # type: ignore[arg-type]
        owner_session="sess",
        owner_agent="Code",
        manifest=manifest,
        record=record,
    )
    extension._browsers[browser.id] = browser
    with pytest.raises(RuntimeError, match="close failed"):
        await extension._close_and_forget(browser)
    # Handle must still be removed despite aclose failure
    assert browser.id not in extension._browsers
    # Unreadable manifest must NOT be overwritten/wiped
    assert manifest.read_text(encoding="utf-8") == "{not valid json"


@pytest.mark.asyncio
async def test_q8_broker_cancellable_read_rejects_pipelined_data_and_cancels_on_eof(tmp_path: Path) -> None:
    server = broker.BrokerServer(BrowserConfig(state_dir=tmp_path))
    cancelled = []

    async def slow_dispatch(_msg: dict[str, Any], _writer: Any) -> dict[str, Any]:
        try:
            await asyncio.sleep(10.0)
            return {}
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    server._dispatch = slow_dispatch  # type: ignore[method-assign]

    # Non-EOF pipelined data while a request is in flight -> ProtocolError
    reader_data = asyncio.StreamReader()
    reader_data.feed_data(encode_message({"id": 2, "op": "ping"}))
    with pytest.raises(ProtocolError, match="pipelined"):
        await server._dispatch_cancellable_read({"id": 1}, reader_data, None)  # type: ignore[arg-type]
    assert len(cancelled) == 1

    # Real EOF -> returns None and cancels handler
    reader_eof = asyncio.StreamReader()
    reader_eof.feed_eof()
    res = await server._dispatch_cancellable_read({"id": 1}, reader_eof, None)  # type: ignore[arg-type]
    assert res is None
    assert len(cancelled) == 2


@pytest.mark.asyncio
async def test_q9_teardown_transport_releases_lock_and_journal_on_stop_error(tmp_path: Path) -> None:
    released = {"lock": False, "journal": False}

    class _FakeLock:
        def release(self) -> None:
            released["lock"] = True

    class _FakeJournal:
        def close(self) -> None:
            released["journal"] = True

    class _FailingTransport:
        owns_browser = True
        browser_commands = True

        async def stop(self, *_a: Any, **_kw: Any) -> None:
            raise RuntimeError("stop boom")

    cfg = BrowserConfig(state_dir=tmp_path)
    session = BrowserSession(cfg)
    session._transport = _FailingTransport()  # type: ignore[assignment]
    session._instance_lock = _FakeLock()  # type: ignore[assignment]
    session._journal = _FakeJournal()  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="stop boom"):
        await session._teardown_transport()
    assert released["lock"] is True
    assert released["journal"] is True


def test_q10_broker_run_config_honors_state_dir(tmp_path: Path) -> None:
    custom = tmp_path / "custom-broker-state"
    cfg = broker._run_config(custom)
    assert cfg.state_dir == custom
    assert cfg.download_dir is None
