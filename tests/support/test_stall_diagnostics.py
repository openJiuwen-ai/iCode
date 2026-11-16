# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The stall sampler tells a busy loop thread from a starved executor."""

from __future__ import annotations

import gc
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.support.stall_diagnostics import PhaseRecord, StallSampler, format_report
from tests.support.waiting import wait_for

_SAMPLES = 5
_SPIN_DEADLINE_SECONDS = 5.0


def _spin_until_sampled(phase: PhaseRecord) -> None:
    """Keep the calling thread busy in Python until the sampler has seen it enough times and timed a stretch of it.

    A stretch takes two samples in a row that find the thread on the same line, and a loop gives up the GIL on more
    than one line: spinning on what the sampler recorded, not on the clock or a sample count, holds under any load.
    """
    deadline = time.monotonic() + _SPIN_DEADLINE_SECONDS
    while phase.samples < _SAMPLES or not phase.longest_busy[0]:
        assert time.monotonic() < deadline, "the sampler stopped sampling"


def _hold(started: threading.Event, release: threading.Event) -> None:
    started.set()
    release.wait()


def test_sampler_attributes_a_busy_loop_thread_and_a_starved_executor() -> None:
    reports: list[str] = []
    sampler = StallSampler(
        loop_thread_ident=threading.get_ident(), sink=reports.append, interval=0.02, report_after=0.0
    )
    sampler.start()
    release = threading.Event()
    try:
        # One thread, two holds: the second sits in the queue for as long as the phase lasts.
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="asyncio") as pool:
            try:
                started = threading.Event()
                pool.submit(_hold, started, release)
                # Queued before the thread took the first, the second would briefly make a queue of two.
                assert started.wait(_SPIN_DEADLINE_SECONDS), "the executor never ran the first hold"
                pool.submit(_hold, threading.Event(), release)
                phase = sampler.begin_phase("busy")
                _spin_until_sampled(phase)
                assert sampler.end_phase(failed=False) is phase
            finally:
                # Leaving the pool waits for both holds.
                release.set()
    finally:
        sampler.close()

    assert phase.samples >= _SAMPLES
    assert phase.idle_samples == 0
    assert sum(phase.busy.values()) == phase.samples
    assert all("_spin_until_sampled" in signature for signature in phase.busy)
    assert "_spin_until_sampled" in phase.longest_busy[0]
    assert 0.0 < phase.longest_busy[1] <= phase.seconds
    assert phase.executor_threads_max == 1
    assert phase.executor_queue_max == 1
    assert any("_hold" in signature for signature in phase.executor_busy)
    assert not phase.freezes
    # Long enough to be reported, with every finding in the text.
    assert reports == [format_report(phase, failed=False)]
    report = reports[0]
    assert "passed after" in report
    assert "_spin_until_sampled" in report and "_hold" in report and "queue max 1" in report


@pytest.mark.asyncio
async def test_sampler_sees_a_loop_waiting_in_the_selector_as_idle() -> None:
    reports: list[str] = []
    sampler = StallSampler(loop_thread_ident=threading.get_ident(), sink=reports.append, interval=0.02)
    sampler.start()
    try:
        sampler.begin_phase("idle")
        # The subject: between two polls of this predicate the loop has nothing to run and sits in the
        # selector, which the sampler must recognise on this platform's event loop as idle.
        await wait_for(lambda: sampler.sampled()[1] >= _SAMPLES, description="the loop was sampled idle")
        phase = sampler.end_phase(failed=False)
    finally:
        sampler.close()

    assert phase is not None
    assert phase.idle_samples >= _SAMPLES
    assert reports == []  # short and green: nothing to report
    assert "in the selector" in format_report(phase, failed=True)


def test_sampler_reports_a_failed_phase_however_short(monkeypatch: pytest.MonkeyPatch) -> None:
    reports: list[str] = []
    sampler = StallSampler(loop_thread_ident=threading.get_ident(), sink=reports.append, interval=0.02)
    real_monotonic = time.monotonic
    opened = real_monotonic()
    sampler.begin_phase("failed")
    # A test that pins the clock mid-phase (as the duration-formatting tests do) must not pin the phase's length.
    monkeypatch.setattr(time, "monotonic", lambda: 1e6)
    phase = sampler.end_phase(failed=True)
    closed = real_monotonic()
    assert phase is not None
    # The phase's real length, however long the runner stalled in it, never one the pinned clock made up.
    assert 0.0 <= phase.seconds <= closed - opened
    assert reports == [format_report(phase, failed=True)]
    assert f"failed after {phase.seconds:.1f}s" in reports[0] and "no samples" in reports[0]
    assert sampler.end_phase(failed=True) is None


def test_sampler_lists_the_collections_that_paused_the_phase() -> None:
    reports: list[str] = []
    # Any collection counts, so the one forced below is listed whatever this machine's speed.
    sampler = StallSampler(loop_thread_ident=threading.get_ident(), sink=reports.append, interval=0.02, gc_pause=0.0)
    sampler.start()
    try:
        sampler.begin_phase("collecting")
        gc.collect()
        phase = sampler.end_phase(failed=False)
    finally:
        sampler.close()
    assert sampler._on_gc not in gc.callbacks

    assert phase is not None
    assert [generation for _, generation, _ in phase.gc_pauses][-1] == 2
    assert all(0.0 <= at <= phase.seconds for at, _, _ in phase.gc_pauses)
    assert "gen2" in format_report(phase, failed=True)
