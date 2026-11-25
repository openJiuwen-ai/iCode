# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Serialize native clipboard writes without holding the TUI event loop."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Callable
from concurrent.futures import Future
from contextvars import Context
from threading import Lock, Thread
from typing import TextIO

from chrys.foundation.util.once_close import finish_close

logger = logging.getLogger(__name__)
_CLOSE_TIMEOUT_SECONDS = 2.0
_COPY_RETRY_SECONDS = 1.0
_COPY_RETRY_INTERVAL = 0.02
# Reserve batch order at submission, before workers can be scheduled out of
# order across app runs. This lock never covers a native call or a wait.
_schedule_lock = Lock()
_last_batch: Future[None] | None = None


class _ClipboardThread(Thread):
    """A daemon whose CPython shutdown fallback does not own Textual's App."""

    _stderr: TextIO | None

    def __init__(self, target: Callable[[Future[None] | None], None], previous: Future[None] | None) -> None:
        super().__init__(target=target, args=(previous,), name="clipboard-copy", daemon=True, context=Context())
        # CPython saves sys.stderr for exception reporting during shutdown.
        # Textual's replacement owns the App; retain only the process stream.
        self._stderr = sys.__stderr__


class ClipboardWriter:
    """One active native write and at most one newer pending selection.

    A thread exits as soon as its queue is empty. It never owns an App, and
    its context and stderr fallback do not retain Textual's active App.
    The writer drops its Thread reference on completion. The production write
    backend terminates and reaps a stalled native helper before returning.
    Close keeps accepted work and bounds the wait for it to drain.
    """

    def __init__(self, write: Callable[[str], None], *, on_failure: Callable[[int], None] | None = None) -> None:
        self._write = write
        self._on_failure = on_failure
        self._lock = Lock()
        self._pending: tuple[int, str] | None = None
        self._sequence = 0
        self._success: bool | None = None
        self._on_copied: Callable[[], None] | None = None
        self._thread: _ClipboardThread | None = None
        self._closed = False
        self._finished: Future[None] = Future()
        self._finished.set_result(None)
        self._close_task: asyncio.Task[None] | None = None

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    def is_current(self, sequence: int) -> bool:
        with self._lock:
            return sequence == self._sequence

    def when_copied(self, callback: Callable[[], None]) -> bool:
        """Run pending feedback on the writer thread; callers must dispatch UI work.

        Return True without calling back when the write already succeeded.
        """
        with self._lock:
            success = self._success
            if success is None:
                self._on_copied = callback
        return success is True

    def submit(self, text: str) -> bool:
        """Keep the newest request; never overlap native clipboard owners."""
        global _last_batch
        with self._lock:
            if self._closed:
                return False
            self._sequence += 1
            sequence = self._sequence
            self._pending = (sequence, text)
            self._success = None
            self._on_copied = None
            accepted = True
            if self._thread is None:
                self._finished = Future()
                with _schedule_lock:
                    self._thread = _ClipboardThread(self._drain, _last_batch)
                    try:
                        self._thread.start()
                    except RuntimeError:
                        self._thread = None
                        self._pending = None
                        self._success = False
                        self._finished.set_result(None)
                        logger.debug("Could not start native clipboard writer", exc_info=True)
                        accepted = False
                    else:
                        _last_batch = self._finished
        if not accepted:
            self._report_failure(sequence)
        return accepted

    def _report_failure(self, sequence: int) -> None:
        if self._on_failure is not None:
            try:
                self._on_failure(sequence)
            except Exception:
                logger.debug("Could not report clipboard failure", exc_info=True)

    def _copy(self, sequence: int, text: str) -> bool | None:
        deadline: float | None = None
        while True:
            try:
                self._write(text)
                return True
            except PermissionError:
                # OpenClipboard reports ERROR_ACCESS_DENIED while another
                # process owns the lock. Retry off the UI thread, but let a
                # newer selection replace a write that has not succeeded.
                if not self.is_current(sequence):
                    return None
                now = time.monotonic()
                if deadline is None:
                    # Cold helper startup must not consume the contention
                    # retry window. Later attempts share this one deadline.
                    deadline = now + _COPY_RETRY_SECONDS
                remaining = deadline - now
                if remaining <= 0:
                    logger.debug("System clipboard remained unavailable", exc_info=True)
                    return False
                time.sleep(min(_COPY_RETRY_INTERVAL, remaining))
            except Exception:
                logger.debug("Host OS clipboard copy failed", exc_info=True)
                return False

    def _drain(self, previous: Future[None] | None) -> None:
        global _last_batch
        if previous is not None:
            # Only the daemon waits. Futures carry no App and preserve order
            # even when several preceding app runs have already timed out.
            previous.result()
            previous = None
        while True:
            with self._lock:
                pending, self._pending = self._pending, None
                if pending is None:
                    self._thread = None
                    finished = self._finished
                    break
            sequence, text = pending
            success = self._copy(sequence, text)
            with self._lock:
                current = sequence == self._sequence
                callback = None
                if current:
                    self._success = success
                    if success:
                        callback = self._on_copied
                    self._on_copied = None
            if current and success is False:
                self._report_failure(sequence)
            if callback is not None:
                try:
                    callback()
                except Exception:
                    logger.debug("Could not report clipboard success", exc_info=True)
        finished.set_result(None)
        with _schedule_lock:
            if _last_batch is finished:
                _last_batch = None

    async def wait(self) -> None:
        """Wait for the current batch without cancelling its native call."""
        with self._lock:
            finished = self._finished
        await asyncio.shield(asyncio.wrap_future(finished))

    async def close(self) -> None:
        """Stop accepting work, then drain accepted copies within the exit budget."""
        with self._lock:
            self._closed = True
        if self._close_task is None:
            # The registry may keep this writer after a timeout. Do not retain
            # Textual's active App through the cleanup task's context either.
            self._close_task = asyncio.create_task(self._settle_close(), context=Context())
        await finish_close(self._close_task)

    async def _settle_close(self) -> None:
        try:
            async with asyncio.timeout(_CLOSE_TIMEOUT_SECONDS):
                await self.wait()
        except TimeoutError:
            logger.debug("Native clipboard write is still settling after app shutdown")
