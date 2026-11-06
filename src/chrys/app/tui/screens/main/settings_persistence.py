# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Debounced user-settings persistence for the main screen.

What accumulates across the debounce window is a patch, not a snapshot. The
panels edit a few fields at a time against a document other writers share, so
carrying a whole-document snapshot would let the last edit resurrect values a
concurrent writer had just changed. A patch merges — two edits inside one
window become one write, and each write touches only the keys that were
actually edited.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from chrys.foundation.config.settings_store import PersistResult, persist

SETTINGS_SAVE_DELAY_SECONDS = 0.25
SETTINGS_FLUSH_LOCK_TIMEOUT_SECONDS = 0.1


def _ignore_written(_result: PersistResult) -> None:
    return None


class SettingsPersistenceQueue:
    """Persist settings-panel edits without blocking the TUI event loop."""

    def __init__(
        self,
        *,
        notify_failure: Callable[[Exception], None],
        notify_rejected: Callable[[PersistResult], None],
        logger: logging.Logger,
        on_written: Callable[[PersistResult], None] = _ignore_written,
        save_delay_seconds: float = SETTINGS_SAVE_DELAY_SECONDS,
        flush_lock_timeout_seconds: float = SETTINGS_FLUSH_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self._notify_failure = notify_failure
        self._notify_rejected = notify_rejected
        self._on_written = on_written
        self._logger = logger
        self._save_delay_seconds = save_delay_seconds
        self._flush_lock_timeout_seconds = flush_lock_timeout_seconds
        self._pending_values: dict[str, Any] = {}
        self._pending_removals: dict[str, None] = {}
        self.failures = 0
        self._flush_lock = asyncio.Lock()
        self.save_generation = 0
        self.save_task: asyncio.Task[None] | None = None
        self.save_persisting = False
        self.flush_requested = False

    def schedule(self, values: Mapping[str, Any] | None = None, *, remove: Iterable[str] = ()) -> None:
        """Merge one edit into the pending patch and (re)arm the debounce.

        Within the patch a key is either set or removed, never both — the
        later instruction wins, exactly as it would have had the two writes
        gone to disk separately.
        """
        for key, value in (values or {}).items():
            self._pending_values[key] = value
            self._pending_removals.pop(key, None)
        for key in remove:
            self._pending_removals[key] = None
            self._pending_values.pop(key, None)
        if not self._pending_values and not self._pending_removals:
            return
        self.save_generation += 1
        self.flush_requested = False
        task = self.save_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._save_loop())
        self.save_task = task
        task.add_done_callback(self._on_save_done)

    async def flush(self, *, notify_on_failure: bool = False) -> bool:
        """Join concurrent flushes, including their disk write, and report failure."""
        failures = self.failures
        async with self._flush_lock:
            await self._flush_pending(notify_on_failure=notify_on_failure)
        return failures == self.failures

    async def _flush_pending(self, *, notify_on_failure: bool) -> None:
        """Force any pending debounced write to complete immediately.

        ``notify_on_failure`` surfaces an environmental failure (lock, I/O)
        the way the debounced path does; the teardown flushes keep it off
        because there is no screen left to show the toast on.
        """
        task = self.save_task
        if not self._pending_values and not self._pending_removals:
            if task is not None and not task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            return

        self.flush_requested = True
        if task is not None and not task.done():
            if self.save_persisting:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                return
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        patch = self._take_pending()
        if patch is None:
            return
        self.flush_requested = False
        values, removals = patch
        await self.persist_patch(
            values,
            removals,
            lock_timeout=self._flush_lock_timeout_seconds,
            notify_on_failure=notify_on_failure,
        )

    async def persist_patch(
        self,
        values: Mapping[str, Any],
        removals: Iterable[str],
        *,
        lock_timeout: float | None = None,
        notify_on_failure: bool = True,
    ) -> None:
        """Write one merged patch to the user settings document."""
        try:
            if lock_timeout is None:
                result = await asyncio.to_thread(persist, values, remove=removals)
            else:
                result = await asyncio.to_thread(persist, values, remove=removals, lock_timeout=lock_timeout)
        except Exception as exc:
            self.failures += 1
            self._logger.warning("Failed to save settings", exc_info=True)
            if notify_on_failure:
                self._notify_failure(exc)
            return
        if not result.ok:
            # Not an environmental failure: the panel handed over a value the
            # store refused, and the whole batch stayed out of the file. Always
            # surfaced, flush included — silence here is the panel showing an
            # edit that never landed.
            self.failures += 1
            self._logger.warning("Settings batch rejected: %s", ", ".join(result.rejected))
            self._notify_rejected(result)
            return
        self._on_written(result)

    def _take_pending(self) -> tuple[dict[str, Any], tuple[str, ...]] | None:
        if not self._pending_values and not self._pending_removals:
            return None
        patch = (dict(self._pending_values), tuple(self._pending_removals))
        self._pending_values = {}
        self._pending_removals = {}
        return patch

    async def _save_loop(self) -> None:
        while True:
            observed_generation = self.save_generation
            if not self.flush_requested:
                await asyncio.sleep(self._save_delay_seconds)
                if observed_generation != self.save_generation:
                    continue

            self.flush_requested = False
            patch = self._take_pending()
            if patch is None:
                return

            values, removals = patch
            self.save_persisting = True
            try:
                await self.persist_patch(values, removals)
            finally:
                self.save_persisting = False
            if not self._pending_values and not self._pending_removals:
                return

    def _on_save_done(self, task: asyncio.Task[None]) -> None:
        if task is self.save_task:
            self.save_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            self._logger.debug("Settings save task failed", exc_info=True)
