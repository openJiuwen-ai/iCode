# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import contextlib
import json
import logging
import random
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest

from chrys.foundation.reporting.config import ReportConfig
from chrys.foundation.reporting.diagnostics import ReportDiagnostics
from chrys.foundation.reporting.frames import KIND_SNAPSHOT_CHUNK, Frame, scope_snapshot_chunk, sha256_hex
from chrys.foundation.reporting.pump import (
    INITIAL_BACKOFF_SECONDS,
    MAX_DEFERRALS_PER_FRAME,
    MIN_BATCH_BYTES,
    ReportPump,
    TransportResult,
)
from chrys.foundation.reporting.wal import DROPPED_LOG_NAME, DurableWAL


@dataclass
class _Transport:
    """A transport that answers with whatever the test queued."""

    results: list[TransportResult] = field(default_factory=list)
    bodies: list[bytes] = field(default_factory=list)
    compressed: list[bool] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)

    def post_batch(self, url: str, token: str, body: bytes, *, gzip_encoded: bool, timeout: float) -> TransportResult:
        self.urls.append(url)
        self.bodies.append(body)
        self.compressed.append(gzip_encoded)
        return self.results.pop(0)

    def post_manifest(self, url: str, token: str, body: bytes, *, timeout: float) -> TransportResult:
        return self.results.pop(0)

    def close(self) -> None:
        return


@dataclass
class _Harness:
    wal: DurableWAL
    transport: _Transport
    pump: ReportPump
    diagnostics: ReportDiagnostics


@contextlib.contextmanager
def _harness(config: ReportConfig, frames: list[Frame]) -> Iterator[_Harness]:
    diagnostics = ReportDiagnostics()
    wal = DurableWAL(config.directory, queue_bytes_max=config.queue_bytes_max, diagnostics=diagnostics)
    wal.open()
    wal.append(frames)
    transport = _Transport()
    pump = ReportPump(wal, config, diagnostics, transport=transport, random_source=random.Random(0))
    try:
        yield _Harness(wal=wal, transport=transport, pump=pump, diagnostics=diagnostics)
    finally:
        wal.close()


def _frame(payload: bytes) -> Frame:
    return Frame.build(KIND_SNAPSHOT_CHUNK, "session-1", scope_snapshot_chunk(sha256_hex(payload), 0), payload)


def _frame_of(session_id: str, payload: bytes) -> Frame:
    return Frame.build(KIND_SNAPSHOT_CHUNK, session_id, scope_snapshot_chunk(sha256_hex(payload), 0), payload)


def _dropped_records(config: ReportConfig) -> list[dict[str, object]]:
    return [json.loads(line) for line in (config.directory / DROPPED_LOG_NAME).read_text(encoding="utf-8").splitlines()]


def _reply(
    accepted: tuple[str, ...] = (),
    *,
    duplicates: tuple[str, ...] = (),
    rejected: tuple[tuple[str, str], ...] = (),
    deferred: tuple[str, ...] = (),
    retry_after_ms: int = 0,
) -> TransportResult:
    body = json.dumps(
        {
            "accepted": list(accepted),
            "duplicates": list(duplicates),
            "rejected": [{"unit_id": unit_id, "code": code} for unit_id, code in rejected],
            "deferred": list(deferred),
            "retry_after_ms": retry_after_ms,
        }
    ).encode()
    return TransportResult(status=200, body=body)


def test_an_accepted_batch_advances_the_acknowledged_offset(report_config: ReportConfig) -> None:
    frames = [_frame(b"first"), _frame(b"second")]
    with _harness(report_config, frames) as harness:
        harness.transport.results.append(_reply(tuple(frame.header.unit_id for frame in frames)))

        delay = harness.pump._cycle()

        assert delay == 0.0
        assert harness.wal.stats().pending_bytes == 0
        snapshot = harness.diagnostics.snapshot()
        assert (snapshot.accepted_units, snapshot.batches_sent) == (2, 1)


def test_a_duplicate_counts_as_delivered(report_config: ReportConfig) -> None:
    frames = [_frame(b"first")]
    with _harness(report_config, frames) as harness:
        harness.transport.results.append(_reply(duplicates=(frames[0].header.unit_id,)))

        harness.pump._cycle()

        assert harness.wal.stats().pending_bytes == 0
        assert harness.diagnostics.snapshot().duplicate_units == 1


def test_a_partial_answer_leaves_the_unsettled_frames_queued(report_config: ReportConfig) -> None:
    frames = [_frame(b"first"), _frame(b"second")]
    with _harness(report_config, frames) as harness:
        harness.transport.results.append(_reply((frames[0].header.unit_id,)))

        delay = harness.pump._cycle()

        assert harness.wal.stats().ack_offset == len(frames[0].encode())
        assert delay > 0
        remaining = harness.wal.next_batch(max_bytes=8 << 20)
        assert remaining is not None
        assert remaining.unit_ids == (frames[1].header.unit_id,)


def test_a_deferred_unit_keeps_the_batch_queued(report_config: ReportConfig) -> None:
    frames = [_frame(b"first")]
    with _harness(report_config, frames) as harness:
        harness.transport.results.append(_reply(deferred=(frames[0].header.unit_id,)))

        delay = harness.pump._cycle()

        assert harness.wal.stats().pending_bytes == len(frames[0].encode())
        assert delay > 0
        assert harness.diagnostics.snapshot().deferred_units == 1


def test_a_permanently_deferred_frame_does_not_block_another_session(report_config: ReportConfig) -> None:
    stuck = _frame_of("session-1", b"stuck")
    following = _frame_of("session-2", b"following")
    with _harness(report_config, [stuck, following]) as harness:
        for _ in range(MAX_DEFERRALS_PER_FRAME):
            harness.transport.results.append(_reply((following.header.unit_id,), deferred=(stuck.header.unit_id,)))
            harness.pump._cycle()

        # The stuck frame is finally dropped, which lets the acknowledged
        # offset move past it and over the other session's frame.
        assert harness.wal.next_batch(max_bytes=8 << 20) is None
        assert harness.wal.stats().pending_bytes == 0
        assert [record["reason"] for record in _dropped_records(report_config)] == ["deferral_limit"]
        assert harness.pump.take_deferred_sessions() == ("session-1",)


def test_a_persistently_deferred_frame_is_dropped_once_with_evidence(
    report_config: ReportConfig, caplog: pytest.LogCaptureFixture
) -> None:
    frames = [_frame(b"first")]
    unit_id = frames[0].header.unit_id
    with _harness(report_config, frames) as harness, caplog.at_level(logging.WARNING):
        for _ in range(MAX_DEFERRALS_PER_FRAME - 1):
            harness.transport.results.append(_reply(deferred=(unit_id,)))
            harness.pump._cycle()
        # Below the threshold the frame is still waited for.
        assert harness.wal.stats().ack_offset == 0

        harness.transport.results.append(_reply(deferred=(unit_id,)))
        harness.pump._cycle()

        assert harness.wal.stats().pending_bytes == 0
        assert harness.pump._deferrals == {}
        dropped = _dropped_records(report_config)
        assert [(record["reason"], record["deferrals"], record["unit_id"]) for record in dropped] == [
            ("deferral_limit", MAX_DEFERRALS_PER_FRAME, unit_id)
        ]
        events = [record.getMessage() for record in caplog.records]
        assert len([event for event in events if event.startswith("report_frame_dropped")]) == 1
        dropped_event = next(event for event in events if event.startswith("report_frame_dropped"))
        assert unit_id in dropped_event
        assert "kind=snapshot.chunk" in dropped_event
        assert "session_id=session-1" in dropped_event
        assert f"deferrals={MAX_DEFERRALS_PER_FRAME}" in dropped_event

        # The frame left the queue, so it is never dropped a second time.
        assert harness.pump._cycle() is None
        assert len(_dropped_records(report_config)) == 1


def test_a_server_rejection_is_recorded_before_the_batch_advances(report_config: ReportConfig) -> None:
    frames = [_frame(b"first")]
    with _harness(report_config, frames) as harness:
        harness.transport.results.append(_reply(rejected=((frames[0].header.unit_id, "schema"),)))

        harness.pump._cycle()

        assert harness.wal.stats().pending_bytes == 0
        assert harness.diagnostics.snapshot().rejected_units == 1
        assert [record["reason"] for record in _dropped_records(report_config)] == ["server_rejected"]


def test_a_bad_request_discards_the_batch_with_evidence(report_config: ReportConfig) -> None:
    with _harness(report_config, [_frame(b"first")]) as harness:
        harness.transport.results.append(TransportResult(status=400))

        harness.pump._cycle()

        assert harness.wal.stats().pending_bytes == 0
        assert [record["reason"] for record in _dropped_records(report_config)] == ["batch_format_rejected"]
        assert harness.diagnostics.snapshot().dropped_units == 1


def test_payload_too_large_shrinks_the_batch_before_discarding_it(report_config: ReportConfig) -> None:
    frames = [_frame(b"x" * 40_000), _frame(b"y" * 40_000)]
    with _harness(report_config, frames) as harness:
        harness.transport.results.append(TransportResult(status=413))

        harness.pump._cycle()

        assert harness.wal.stats().ack_offset == 0
        assert harness.pump._max_batch_bytes >= MIN_BATCH_BYTES

        # The re-sliced batch is one frame, which the server refuses again.
        harness.transport.results.append(TransportResult(status=413))
        harness.pump._cycle()

        assert harness.wal.stats().ack_offset == len(frames[0].encode())
        assert [record["reason"] for record in _dropped_records(report_config)] == ["payload_too_large"]


def test_invalid_credentials_pause_the_pump(report_config: ReportConfig) -> None:
    with _harness(report_config, [_frame(b"first")]) as harness:
        harness.transport.results.append(TransportResult(status=401))

        harness.pump._cycle()

        snapshot = harness.diagnostics.snapshot()
        assert snapshot.paused is True
        assert snapshot.last_error == "invalid_credentials"
        assert harness.wal.stats().ack_offset == 0


def test_rate_limiting_is_honoured_over_the_backoff(report_config: ReportConfig) -> None:
    with _harness(report_config, [_frame(b"first")]) as harness:
        harness.transport.results.append(TransportResult(status=429, retry_after_seconds=42.0))

        delay = harness.pump._cycle()

        assert delay == 42.0
        assert harness.diagnostics.snapshot().last_error == "rate_limited"


def test_a_server_error_backs_off_with_jitter(report_config: ReportConfig) -> None:
    with _harness(report_config, [_frame(b"first")]) as harness:
        harness.transport.results.append(TransportResult(status=503))

        delay = harness.pump._cycle()

        assert INITIAL_BACKOFF_SECONDS * 0.8 <= delay <= INITIAL_BACKOFF_SECONDS * 1.2
        assert harness.diagnostics.snapshot().consecutive_failures == 1


def test_a_malformed_success_body_does_not_advance_the_queue(report_config: ReportConfig) -> None:
    with _harness(report_config, [_frame(b"first")]) as harness:
        harness.transport.results.append(TransportResult(status=200, body=b"not json"))

        harness.pump._cycle()

        assert harness.wal.stats().ack_offset == 0
        assert harness.diagnostics.snapshot().send_failures == 1


def test_an_empty_queue_sends_nothing(report_config: ReportConfig) -> None:
    with _harness(report_config, []) as harness:
        assert harness.pump._cycle() is None
        assert harness.transport.bodies == []
