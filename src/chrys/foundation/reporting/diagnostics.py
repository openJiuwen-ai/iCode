# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded, thread-safe counters for the reporting subsystem.

Reporting must never fail a turn, so its only outward evidence is here: what
was queued, what the server took, and what was thrown away (with the reason).
Every mutation is a lock-guarded increment and every read returns a copy, so a
diagnostics surface can look at the numbers from any thread.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace


@dataclass(frozen=True, slots=True)
class DiagnosticsSnapshot:
    """One consistent read of the reporting counters."""

    queued_bytes: int = 0
    accepted_units: int = 0
    duplicate_units: int = 0
    rejected_units: int = 0
    deferred_units: int = 0
    dropped_units: int = 0
    dropped_bytes: int = 0
    batches_sent: int = 0
    send_failures: int = 0
    consecutive_failures: int = 0
    paused: bool = False
    last_error: str = ""
    last_success_at: str = ""


class ReportDiagnostics:
    """Counters shared by the collector thread, the pump thread and a reader."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = DiagnosticsSnapshot()

    def snapshot(self) -> DiagnosticsSnapshot:
        """The current counters."""
        with self._lock:
            return self._snapshot

    def set_queued_bytes(self, value: int) -> None:
        """Record the durable queue's current unacknowledged byte count."""
        with self._lock:
            self._snapshot = replace(self._snapshot, queued_bytes=max(0, value))

    def record_accepted(self, units: int, duplicates: int) -> None:
        """Record what a batch response accepted."""
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                accepted_units=self._snapshot.accepted_units + units,
                duplicate_units=self._snapshot.duplicate_units + duplicates,
            )

    def record_rejected(self, units: int) -> None:
        """Record terminal rejections (the units are dropped with evidence)."""
        with self._lock:
            self._snapshot = replace(self._snapshot, rejected_units=self._snapshot.rejected_units + units)

    def record_deferred(self, units: int) -> None:
        """Record units the server asked to resend later."""
        with self._lock:
            self._snapshot = replace(self._snapshot, deferred_units=self._snapshot.deferred_units + units)

    def record_drop(self, units: int, byte_count: int) -> None:
        """Record units dropped from the queue, with the bytes they took."""
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                dropped_units=self._snapshot.dropped_units + units,
                dropped_bytes=self._snapshot.dropped_bytes + byte_count,
            )

    def record_batch_sent(self, *, at: str) -> None:
        """Record a batch the server answered successfully."""
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                batches_sent=self._snapshot.batches_sent + 1,
                consecutive_failures=0,
                paused=False,
                last_error="",
                last_success_at=at,
            )

    def record_failure(self, reason: str, *, paused: bool = False, at: str = "") -> None:
        """Record a send failure; *paused* marks an authentication stop."""
        with self._lock:
            self._snapshot = replace(
                self._snapshot,
                send_failures=self._snapshot.send_failures + 1,
                consecutive_failures=self._snapshot.consecutive_failures + 1,
                paused=self._snapshot.paused or paused,
                last_error=reason,
                last_success_at=at or self._snapshot.last_success_at,
            )

    def record_paused(self, reason: str) -> None:
        """Record that sending stopped on purpose (bad credentials)."""
        with self._lock:
            self._snapshot = replace(self._snapshot, paused=True, last_error=reason)
