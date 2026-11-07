# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import gzip
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import SessionReady, SessionSaved
from chrys.foundation.reporting.config import ReportConfig
from chrys.foundation.reporting.frames import (
    KIND_SNAPSHOT_MANIFEST,
    KIND_TRAJECTORY_LINES,
    Frame,
    decode_frames,
)
from chrys.foundation.util.session_ids import session_short_id
from chrys.service.reporting.service import ReportingService
from tests.support.waiting import wait_for

SESSION_ID = "12345678-1234-1234-1234-123456789abc"


@dataclass
class _Received:
    """What the loopback server saw, written by its handler thread."""

    paths: list[str] = field(default_factory=list)
    authorizations: list[str] = field(default_factory=list)
    batches: list[list[Frame]] = field(default_factory=list)


class _BatchHandler(BaseHTTPRequestHandler):
    received: _Received

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        frames = decode_frames(body)
        self.received.paths.append(self.path)
        self.received.authorizations.append(self.headers.get("Authorization", ""))
        self.received.batches.append(frames)
        reply = json.dumps(
            {
                "accepted": [frame.header.unit_id for frame in frames],
                "duplicates": [],
                "rejected": [],
                "deferred": [],
                "retry_after_ms": 0,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def batch_server() -> Iterator[tuple[str, _Received]]:
    """A loopback batch endpoint on an ephemeral port, served from its own thread."""
    received = _Received()
    handler = type("_BoundBatchHandler", (_BatchHandler,), {"received": received})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, name="chrys-report-test-server", daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def _line(sequence: int) -> bytes:
    return json.dumps({"schema_version": 1, "sequence": sequence, "event_type": "event.turn"}).encode() + b"\n"


async def test_a_collection_round_reaches_a_loopback_endpoint_and_is_acknowledged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    batch_server: tuple[str, _Received],
    direct_route: None,
) -> None:
    from chrys.service.reporting import service as service_module

    endpoint, received = batch_server
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(service_module, "resolve_sessions_dir", lambda **_kwargs: sessions)
    folder = sessions / session_short_id(SESSION_ID)
    (folder / "trajectory").mkdir(parents=True)
    events = _line(1) + _line(2) + _line(3)
    (folder / "trajectory" / "events.jsonl").write_bytes(events)
    (folder / "session.json").write_bytes(b'{"schema_version":1}')

    config = ReportConfig(
        enabled=True,
        endpoint=endpoint,
        token="test-token",
        upload_timeout_seconds=5.0,
        batch_max_bytes=8 << 20,
        poll_interval_seconds=1.0,
        queue_bytes_max=8 << 20,
        workspace_capture=False,
        agent_config=False,
        diagnostics=False,
        directory=tmp_path / "reporting",
    )
    bus = EventBus()
    reporting = ReportingService(bus, config, settings=Settings())
    await reporting.start()
    try:
        await bus.publish(SessionReady(session_id=SESSION_ID, primary_cwd=str(tmp_path)))
        await bus.publish(SessionSaved(session_id=SESSION_ID))

        await wait_for(lambda: bool(received.batches), timeout=15.0, description="a batch reached the server")

        frames = [frame for batch in received.batches for frame in batch]
        assert received.paths[0] == "/v1/batch"
        assert received.authorizations[0] == "Bearer test-token"
        assert KIND_TRAJECTORY_LINES in {frame.header.kind for frame in frames}
        assert KIND_SNAPSHOT_MANIFEST in {frame.header.kind for frame in frames}
        trajectory = b"".join(frame.payload for frame in frames if frame.header.kind == KIND_TRAJECTORY_LINES)
        assert trajectory == events

        wal = reporting._wal
        assert wal is not None
        await wait_for(lambda: wal.stats().pending_bytes == 0, timeout=15.0, description="the queue is acknowledged")
    finally:
        await reporting.close()
