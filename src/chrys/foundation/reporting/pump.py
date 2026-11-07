# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The one sender: a dedicated thread that posts queued frames in batches.

The pump is a plain ``threading.Thread`` rather than an asyncio task so no
network wait can ever land in the event loop. It owns one in-flight request at
a time, backs off exponentially with jitter, and treats the acknowledged offset
as the only progress marker — a batch advances it only once every frame in it
was accepted, deduplicated or terminally rejected.
"""

from __future__ import annotations

import contextlib
import json
import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from chrys.foundation.reporting.config import BATCH_ENDPOINT_PATH, ReportConfig
from chrys.foundation.reporting.diagnostics import ReportDiagnostics
from chrys.foundation.reporting.frames import (
    CONTENT_TYPE,
    MAX_COMPRESSED_BATCH_BYTES,
    compress_body,
)
from chrys.foundation.reporting.wal import DurableWAL, WalBatch
from chrys.foundation.util.time import utc_iso

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

INITIAL_BACKOFF_SECONDS: Final = 2.0
MAX_BACKOFF_SECONDS: Final = 300.0
BACKOFF_MULTIPLIER: Final = 2.0
JITTER_LOW: Final = 0.8
JITTER_HIGH: Final = 1.2
MIN_BATCH_BYTES: Final = 64 * 1024
IDLE_WAIT_SECONDS: Final = 1.0
"""How long the pump sleeps when the queue is empty before looking again."""

STOP_DRAIN_SECONDS: Final = 5.0
"""Shutdown budget: at most this long is spent waiting for an in-flight request."""


@dataclass(frozen=True, slots=True)
class TransportResult:
    """One HTTP exchange's outcome; ``status == 0`` means no answer at all."""

    status: int
    body: bytes = b""
    retry_after_seconds: float = 0.0
    error: str = ""

    @property
    def answered(self) -> bool:
        """Whether the server answered at all."""
        return self.status > 0


@dataclass(frozen=True, slots=True)
class BatchReply:
    """The ``200`` body of ``POST /v1/batch``, partitioned per unit."""

    accepted: tuple[str, ...] = ()
    duplicates: tuple[str, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()
    deferred: tuple[str, ...] = ()
    retry_after_seconds: float = 0.0

    @classmethod
    def parse(cls, body: bytes) -> BatchReply:
        """Parse a batch response; raise ``ValueError`` when it is not one."""
        document = json.loads(body.decode("utf-8"))
        if not isinstance(document, dict):
            msg = "batch response is not a JSON object"
            raise ValueError(msg)

        def _strings(key: str) -> tuple[str, ...]:
            value = document.get(key, [])
            if not isinstance(value, list):
                msg = f"batch response field {key!r} is not a list"
                raise ValueError(msg)
            return tuple(item for item in value if isinstance(item, str))

        raw_rejected = document.get("rejected", [])
        if not isinstance(raw_rejected, list):
            msg = "batch response field 'rejected' is not a list"
            raise ValueError(msg)
        rejected: list[tuple[str, str]] = []
        for entry in raw_rejected:
            if not isinstance(entry, dict):
                msg = "batch response 'rejected' entry is not an object"
                raise ValueError(msg)
            unit_id = entry.get("unit_id")
            code = entry.get("code", "")
            if isinstance(unit_id, str):
                rejected.append((unit_id, code if isinstance(code, str) else ""))
        retry_after_ms = document.get("retry_after_ms", 0)
        retry_after = (
            max(0.0, retry_after_ms / 1000.0)
            if isinstance(retry_after_ms, int | float) and not isinstance(retry_after_ms, bool)
            else 0.0
        )
        return cls(
            accepted=_strings("accepted"),
            duplicates=_strings("duplicates"),
            rejected=tuple(rejected),
            deferred=_strings("deferred"),
            retry_after_seconds=retry_after,
        )


class ReportTransport(Protocol):
    """The HTTP surface the pump needs; tests substitute a loopback fake."""

    def post_batch(self, url: str, token: str, body: bytes, *, gzip_encoded: bool, timeout: float) -> TransportResult:
        """Post one batch body."""
        ...

    def post_manifest(self, url: str, token: str, body: bytes, *, timeout: float) -> TransportResult:
        """Post one manifest negotiation body."""
        ...

    def close(self) -> None:
        """Release client resources."""
        ...


class HttpxTransport:
    """``httpx``-backed transport, created on first use and closed with the pump."""

    def __init__(self) -> None:
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()

    def _acquire(self) -> httpx.Client:
        with self._lock:
            if self._client is None:
                import httpx

                self._client = httpx.Client(follow_redirects=False, trust_env=True)
            return self._client

    def post_batch(self, url: str, token: str, body: bytes, *, gzip_encoded: bool, timeout: float) -> TransportResult:
        """Post frames to ``/v1/batch``."""
        return self._post(url, token, body, gzip_encoded=gzip_encoded, timeout=timeout)

    def post_manifest(self, url: str, token: str, body: bytes, *, timeout: float) -> TransportResult:
        """Post a JSON manifest query."""
        return self._post(url, token, body, gzip_encoded=False, timeout=timeout, content_type="application/json")

    def _post(
        self,
        url: str,
        token: str,
        body: bytes,
        *,
        gzip_encoded: bool,
        timeout: float,
        content_type: str = CONTENT_TYPE,
    ) -> TransportResult:
        import httpx

        headers = {"Content-Type": content_type}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if gzip_encoded:
            headers["Content-Encoding"] = "gzip"
        client = self._acquire()
        try:
            response = client.post(url, content=body, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            return TransportResult(status=0, error=f"{type(exc).__name__}: {exc}")
        except OSError as exc:
            return TransportResult(status=0, error=f"{type(exc).__name__}: {exc}")
        return TransportResult(
            status=response.status_code,
            body=response.content,
            retry_after_seconds=_retry_after_seconds(response.headers.get("Retry-After")),
        )

    def close(self) -> None:
        """Close the HTTP client, if one was ever built."""
        with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()


def _retry_after_seconds(raw: str | None) -> float:
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


class ReportPump:
    """Ship queued frames; own exactly one request in flight."""

    def __init__(
        self,
        wal: DurableWAL,
        config: ReportConfig,
        diagnostics: ReportDiagnostics,
        *,
        transport: ReportTransport | None = None,
        random_source: random.Random | None = None,
    ) -> None:
        self._wal = wal
        self._config = config
        self._diagnostics = diagnostics
        self._transport = transport or HttpxTransport()
        self._random = random_source or random.SystemRandom()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._max_batch_bytes = config.batch_max_bytes
        self._backoff = INITIAL_BACKOFF_SECONDS

    @property
    def running(self) -> bool:
        """Whether the sender thread is alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        """Start the sender thread once."""
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="chrys-report-pump", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        """Ask the thread to look at the queue now."""
        self._wake.set()

    def stop(self, *, timeout: float = STOP_DRAIN_SECONDS) -> bool:
        """Ask the thread to stop and wait up to *timeout*; unsent frames stay queued."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        self._thread = None
        if thread is None:
            return True
        thread.join(timeout)
        drained = not thread.is_alive()
        with contextlib.suppress(Exception):
            self._transport.close()
        return drained

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                delay = self._cycle()
            except Exception as exc:  # never let the queue thread die
                logger.warning("Report pump cycle failed", exc_info=True)
                self._diagnostics.record_failure(f"{type(exc).__name__}: {exc}")
                delay = self._next_backoff()
            self._wait(delay if delay is not None else IDLE_WAIT_SECONDS)

    def _wait(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if self._wake.wait(min(remaining, IDLE_WAIT_SECONDS)):
                # New work: re-examine the queue now instead of sleeping out
                # the remaining backoff.
                self._wake.clear()
                return

    def _cycle(self) -> float | None:
        """Send one batch; return how long to wait before the next look."""
        batch = self._wal.next_batch(max_bytes=self._max_batch_bytes)
        if batch is None:
            self._diagnostics.set_queued_bytes(self._wal.stats().pending_bytes)
            return None
        body = self._wal.read(batch)
        payload, compressed = compress_body(body)
        if compressed and len(payload) > MAX_COMPRESSED_BATCH_BYTES and len(batch.headers) > 1:
            self._max_batch_bytes = max(MIN_BATCH_BYTES, len(body) // 2)
            return 0.0
        result = self._transport.post_batch(
            self._config.url(BATCH_ENDPOINT_PATH),
            self._config.token,
            payload,
            gzip_encoded=compressed,
            timeout=self._config.upload_timeout_seconds,
        )
        return self._handle(batch, result)

    def _handle(self, batch: WalBatch, result: TransportResult) -> float:
        status = result.status
        if status == 200:
            return self._handle_accepted(batch, result)
        if status == 400:
            # The server cannot parse what we send: our bug. Discard the batch
            # with evidence instead of resending it forever.
            self._discard(batch, "batch_format_rejected")
            self._wal.advance(batch.end)
            return 0.0
        if status in {401, 403}:
            self._diagnostics.record_paused("invalid_credentials")
            return self._next_backoff()
        if status == 413:
            # The batch is too large: re-slice smaller rather than resend it.
            if len(batch.headers) <= 1:
                self._discard(batch, "payload_too_large")
                self._wal.advance(batch.end)
                return 0.0
            self._max_batch_bytes = max(MIN_BATCH_BYTES, batch.size // 2)
            return 0.0
        if status == 429:
            self._diagnostics.record_failure("rate_limited")
            return max(result.retry_after_seconds, self._next_backoff())
        reason = result.error or f"http_{status}"
        self._diagnostics.record_failure(reason)
        return self._next_backoff()

    def _handle_accepted(self, batch: WalBatch, result: TransportResult) -> float:
        try:
            reply = BatchReply.parse(result.body)
        except (ValueError, UnicodeDecodeError) as exc:
            self._diagnostics.record_failure(f"malformed_response: {exc}")
            return self._next_backoff()
        self._diagnostics.record_batch_sent(at=utc_iso())
        self._diagnostics.record_accepted(len(reply.accepted), len(reply.duplicates))
        self._diagnostics.record_rejected(len(reply.rejected))
        self._diagnostics.record_deferred(len(reply.deferred))
        for unit_id, code in reply.rejected:
            self._wal.record_evidence({"unit_id": unit_id, "reason": "server_rejected", "code": code, "at": utc_iso()})
        settled = set(reply.accepted) | set(reply.duplicates) | {unit_id for unit_id, _code in reply.rejected}
        advance = batch.start
        for header, frame_end in zip(batch.headers, batch.ends, strict=True):
            if header.unit_id not in settled:
                break
            advance = frame_end
        if advance > batch.start:
            self._wal.advance(advance)
            self._backoff = INITIAL_BACKOFF_SECONDS
        if advance < batch.end:
            # Frames the server deferred stay queued; a partial batch is retried
            # from the first unsettled frame.
            return max(reply.retry_after_seconds, self._next_backoff())
        return 0.0

    def _discard(self, batch: WalBatch, reason: str) -> None:
        for header in batch.headers:
            self._wal.record_evidence({"unit_id": header.unit_id, "kind": header.kind, "reason": reason})
        self._diagnostics.record_drop(len(batch.headers), batch.size)

    def _next_backoff(self) -> float:
        delay = min(MAX_BACKOFF_SECONDS, self._backoff)
        self._backoff = min(MAX_BACKOFF_SECONDS, self._backoff * BACKOFF_MULTIPLIER)
        return delay * self._random.uniform(JITTER_LOW, JITTER_HIGH)
