# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Clipboard queue ownership under slow native calls and repeated copies."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from threading import Event
from types import ModuleType

import pytest

from chrys.app.tui.support import clipboard_writer
from chrys.app.tui.support.clipboard_writer import ClipboardWriter
from tests.support.waiting import wait_for


async def test_transient_contention_retries_without_reporting_failure() -> None:
    attempts: list[str] = []
    failed: list[int] = []

    def write(text: str) -> None:
        attempts.append(text)
        if len(attempts) < 3:
            raise PermissionError("clipboard held by another process")

    writer = ClipboardWriter(write, on_failure=failed.append)
    try:
        assert writer.submit("selection")
        await writer.wait()
        assert attempts == ["selection"] * 3
        assert failed == []
        assert writer.when_copied(lambda: pytest.fail("already completed"))
    finally:
        await writer.close()


@pytest.mark.parametrize("recovers", [False, True])
async def test_slow_first_attempt_does_not_spend_the_retry_window(
    monkeypatch: pytest.MonkeyPatch, recovers: bool
) -> None:
    now = 0.0
    attempts: list[str] = []
    failed: list[int] = []

    def monotonic() -> float:
        return now

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    def write(text: str) -> None:
        nonlocal now
        attempts.append(text)
        # Simulate interpreter startup longer than the outer retry budget.
        now += clipboard_writer._COPY_RETRY_SECONDS + 1
        if not recovers or len(attempts) == 1:
            raise PermissionError("clipboard temporarily locked")

    clock = ModuleType("clipboard_retry_clock")
    clock.monotonic = monotonic
    clock.sleep = sleep
    monkeypatch.setattr(clipboard_writer, "time", clock)
    writer = ClipboardWriter(write, on_failure=failed.append)
    try:
        assert writer.submit("selection")
        await writer.wait()
        assert attempts == ["selection", "selection"]
        assert failed == ([] if recovers else [1])
        assert writer.when_copied(lambda: None) is recovers
    finally:
        await writer.close()


async def test_persistent_contention_reports_failure_once_and_later_copy_recovers(monkeypatch) -> None:
    failed: list[int] = []
    attempts: list[str] = []
    monkeypatch.setattr(clipboard_writer, "_COPY_RETRY_SECONDS", 0)

    def write(text: str) -> None:
        attempts.append(text)
        if text == "failed":
            raise PermissionError("clipboard remains locked")

    writer = ClipboardWriter(write, on_failure=failed.append)
    try:
        assert writer.submit("failed")
        await writer.wait()
        assert failed == [1]
        assert not writer.when_copied(lambda: pytest.fail("failed copy must not report success"))
        assert writer.submit("recovered")
        await writer.wait()
        assert attempts == ["failed", "recovered"]
        assert failed == [1]
        assert writer.when_copied(lambda: None)
        assert not writer.is_current(1)
    finally:
        await writer.close()


async def test_new_selection_supersedes_a_failed_attempt_and_its_feedback() -> None:
    entered, release = Event(), Event()
    attempts: list[str] = []
    failed: list[int] = []
    notices: list[str] = []

    def write(text: str) -> None:
        attempts.append(text)
        if text == "obsolete":
            entered.set()
            assert release.wait(5)
            raise PermissionError("clipboard locked")

    writer = ClipboardWriter(write, on_failure=failed.append)
    try:
        writer.submit("obsolete")
        await wait_for(entered.is_set)
        assert not writer.when_copied(lambda: notices.append("obsolete"))
        writer.submit("latest")
        assert not writer.when_copied(lambda: notices.append("latest"))
        release.set()
        await writer.wait()
        assert attempts == ["obsolete", "latest"]
        assert notices == ["latest"]
        assert failed == []
    finally:
        release.set()
        await writer.close()


async def test_repeated_restarts_preserve_submission_order_when_a_worker_starts_late(monkeypatch) -> None:
    first_entered, release_first, second_started, release_second, third_waiting, third_copied = (
        Event() for _ in range(6)
    )
    copied: list[str] = []
    thread_count = 0

    class _DelayedThread(clipboard_writer._ClipboardThread):
        def __init__(self, *args, **kwargs) -> None:
            nonlocal thread_count
            super().__init__(*args, **kwargs)
            thread_count += 1
            self.delayed = thread_count == 2

        def run(self) -> None:
            if self.delayed:
                second_started.set()
                assert release_second.wait(5)
            super().run()

    class _ObservedFuture(Future):
        def result(self, timeout=None):
            if self is writers[1]._finished and not self.done():
                third_waiting.set()
            return super().result(timeout)

    def write(text: str) -> None:
        if text == "first run":
            first_entered.set()
            assert release_first.wait(5)
        copied.append(text)
        if text == "third run":
            third_copied.set()

    monkeypatch.setattr(clipboard_writer, "_ClipboardThread", _DelayedThread)
    monkeypatch.setattr(clipboard_writer, "Future", _ObservedFuture)
    monkeypatch.setattr(clipboard_writer, "_CLOSE_TIMEOUT_SECONDS", 0)
    writers = [ClipboardWriter(write) for _ in range(3)]
    try:
        assert writers[0].submit("first run")
        await wait_for(first_entered.is_set)
        await writers[0].close()
        assert writers[1].submit("second run")
        await wait_for(second_started.is_set)
        await writers[1].close()
        assert writers[2].submit("third run")
        release_first.set()
        await writers[0].wait()
        await wait_for(lambda: third_waiting.is_set() or third_copied.is_set())
        assert not third_copied.is_set()
        release_second.set()
        await writers[2].wait()
        assert copied == ["first run", "second run", "third run"]
    finally:
        release_first.set()
        release_second.set()
        for writer in writers:
            await writer.wait()
            await writer.close()


async def test_failed_worker_start_does_not_break_the_order_of_other_app_runs(monkeypatch) -> None:
    entered, release, waiting, newest_copied = (Event() for _ in range(4))
    copied: list[str] = []

    class _ObservedFuture(Future):
        def result(self, timeout=None):
            if not self.done():
                waiting.set()
            return super().result(timeout)

    def write(text: str) -> None:
        if text == "old":
            entered.set()
            assert release.wait(5)
        copied.append(text)
        if text == "new":
            newest_copied.set()

    def fail_start(self) -> None:
        raise RuntimeError("thread unavailable")

    monkeypatch.setattr(clipboard_writer, "Future", _ObservedFuture)
    old, failed, newest = (ClipboardWriter(write) for _ in range(3))
    try:
        assert old.submit("old")
        await wait_for(entered.is_set)
        with monkeypatch.context() as patch:
            patch.setattr(clipboard_writer.Thread, "start", fail_start)
            assert not failed.submit("rejected")
        assert newest.submit("new")
        await wait_for(lambda: waiting.is_set() or newest_copied.is_set())
        assert not newest_copied.is_set()
        release.set()
        await newest.wait()
        assert copied == ["old", "new"]
    finally:
        release.set()
        for writer in (old, failed, newest):
            await writer.wait()
            await writer.close()


async def test_writes_are_serial_latest_wins_and_wait_cancellation_does_not_cancel_copy() -> None:
    entered, release = Event(), Event()
    copied: list[str] = []

    def write(text: str) -> None:
        if text == "first":
            entered.set()
            assert release.wait(5)
        copied.append(text)

    writer = ClipboardWriter(write)
    try:
        assert writer.submit("first")
        await wait_for(entered.is_set)
        assert writer.submit("obsolete")
        assert writer.submit("latest")
        waiter = asyncio.create_task(writer.wait())
        await asyncio.sleep(0)  # Let this waiter attach to the still-blocked native call.
        assert not waiter.done()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert writer.busy
        release.set()
        await writer.wait()
        assert copied == ["first", "latest"]
        assert not writer.busy
        assert writer._thread is None
        # A later repeat must write again: another app may have changed the clipboard.
        assert writer.submit("latest")
        await writer.wait()
        assert copied == ["first", "latest", "latest"]
    finally:
        release.set()
        await writer.close()


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_close_drains_the_latest_accepted_copy_despite_waiter_cancellation(cancel_waiter: bool) -> None:
    entered, release = Event(), Event()
    copied: list[str] = []

    def write(text: str) -> None:
        entered.set()
        assert release.wait(5)
        copied.append(text)

    writer = ClipboardWriter(write)
    try:
        writer.submit("active")
        await wait_for(entered.is_set)
        writer.submit("pending")
        closing = asyncio.create_task(writer.close())
        await asyncio.sleep(0)  # Enter close while the native call is still held.
        assert not closing.done()
        assert not writer.submit("after close")
        if cancel_waiter:
            closing.cancel()
            await asyncio.sleep(0)  # Deliver cancellation while the native call is held.
            assert not closing.done()
            closing.cancel()
        second_close = asyncio.create_task(writer.close())
        release.set()
        if cancel_waiter:
            with pytest.raises(asyncio.CancelledError):
                await closing
        else:
            await closing
        await second_close
        assert copied == ["active", "pending"]
        assert not writer.busy
    finally:
        release.set()
        await writer.close()


async def test_native_failure_does_not_poison_later_copies() -> None:
    copied: list[str] = []

    def write(text: str) -> None:
        if text == "failed":
            raise OSError("clipboard busy")
        copied.append(text)

    writer = ClipboardWriter(write)
    try:
        writer.submit("failed")
        await writer.wait()
        writer.submit("recovered")
        await writer.wait()
        assert copied == ["recovered"]
    finally:
        await writer.close()


async def test_shutdown_does_not_wait_forever_for_a_native_call(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = Event(), Event()
    copied: list[str] = []

    def write(text: str) -> None:
        entered.set()
        assert release.wait(5)
        copied.append(text)

    writer = ClipboardWriter(write)
    try:
        writer.submit("active")
        await wait_for(entered.is_set)
        writer.submit("latest")
        monkeypatch.setattr(clipboard_writer, "_CLOSE_TIMEOUT_SECONDS", 0)
        await writer.close()
        assert writer.busy
        assert copied == []
        assert not writer.submit("closed")
    finally:
        release.set()
        await writer.wait()
    assert copied == ["active", "latest"]
