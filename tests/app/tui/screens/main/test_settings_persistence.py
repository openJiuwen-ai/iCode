# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for debounced user-settings persistence."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from typing import Any

import pytest

from chrys.app.tui.screens.main import settings_persistence
from chrys.app.tui.screens.main.settings_persistence import SettingsPersistenceQueue
from chrys.foundation.config.coercion import CoerceReason, invalid
from chrys.foundation.config.settings_store import PersistResult

_DEFAULT_TIMEOUT = object()


class _Recorder:
    def __init__(self, *, rejected: Mapping[str, Any] | None = None, raises: Exception | None = None) -> None:
        self.calls: list[tuple[dict[str, Any], tuple[str, ...], object]] = []
        self._rejected = dict(rejected or {})
        self._raises = raises

    def persist(
        self,
        values: Mapping[str, Any],
        *,
        remove: Iterable[str] = (),
        lock_timeout: object = _DEFAULT_TIMEOUT,
    ) -> PersistResult:
        self.calls.append((dict(values), tuple(remove), lock_timeout))
        if self._raises is not None:
            raise self._raises
        if self._rejected:
            return PersistResult(written={}, rejected=self._rejected)
        return PersistResult(written=dict(values), rejected={})


def _queue(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    *,
    notify_failure=lambda _exc: None,
    notify_rejected=lambda _result: None,
    on_written=lambda _result: None,
    save_delay_seconds: float = 0,
    flush_lock_timeout_seconds: float = 0.2,
) -> SettingsPersistenceQueue:
    monkeypatch.setattr(settings_persistence, "persist", recorder.persist)
    return SettingsPersistenceQueue(
        notify_failure=notify_failure,
        notify_rejected=notify_rejected,
        logger=logging.getLogger(__name__),
        on_written=on_written,
        save_delay_seconds=save_delay_seconds,
        flush_lock_timeout_seconds=flush_lock_timeout_seconds,
    )


async def test_settings_queue_merges_edits_into_one_write(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    queue = _queue(monkeypatch, recorder)

    queue.schedule({"ui.theme": "dark"})
    queue.schedule({"session.title.auto": False})
    queue.schedule({"ui.theme": "light"})
    task = queue.save_task
    assert task is not None
    await task

    assert recorder.calls == [({"ui.theme": "light", "session.title.auto": False}, (), _DEFAULT_TIMEOUT)]


async def test_settings_queue_the_later_instruction_wins_within_the_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    queue = _queue(monkeypatch, recorder, save_delay_seconds=60)

    queue.schedule({"ui.theme": "dark"})
    queue.schedule(remove=("ui.theme",))
    queue.schedule({"session.title.auto": False})
    queue.schedule(remove=("session.title.auto",))
    queue.schedule({"session.title.auto": True})
    await queue.flush()

    assert recorder.calls == [({"session.title.auto": True}, ("ui.theme",), 0.2)]


async def test_settings_queue_flush_skips_debounce_and_uses_the_flush_lock_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _Recorder()
    queue = _queue(monkeypatch, recorder, save_delay_seconds=60)

    queue.schedule({"ui.theme": "dark"})
    await queue.flush()

    assert recorder.calls == [({"ui.theme": "dark"}, (), 0.2)]


async def test_settings_queue_reports_a_save_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    failures: list[Exception] = []
    recorder = _Recorder(raises=RuntimeError("save failed"))
    queue = _queue(monkeypatch, recorder, notify_failure=failures.append)

    queue.schedule({"ui.theme": "dark"})
    task = queue.save_task
    assert task is not None
    await task

    assert [str(exc) for exc in failures] == ["save failed"]


async def test_settings_queue_surfaces_a_rejected_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    rejections: list[PersistResult] = []
    written: list[PersistResult] = []
    verdict = invalid("nonsense", CoerceReason.EXPECTED_BOOL)
    recorder = _Recorder(rejected={"session.title.auto": verdict})
    queue = _queue(monkeypatch, recorder, notify_rejected=rejections.append, on_written=written.append)

    queue.schedule({"session.title.auto": "nonsense"})
    task = queue.save_task
    assert task is not None
    await task

    assert len(rejections) == 1
    assert rejections[0].rejected == {"session.title.auto": verdict}
    # A rejected batch never counts as written.
    assert written == []


async def test_settings_queue_reports_each_successful_write(monkeypatch: pytest.MonkeyPatch) -> None:
    written: list[PersistResult] = []
    recorder = _Recorder()
    queue = _queue(monkeypatch, recorder, on_written=written.append, save_delay_seconds=60)

    queue.schedule({"ui.theme": "dark"})
    await queue.flush()
    queue.schedule({"session.title.auto": False})
    await queue.flush()

    assert [result.written for result in written] == [{"ui.theme": "dark"}, {"session.title.auto": False}]


async def test_settings_queue_flush_can_surface_an_environmental_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    failures: list[Exception] = []
    recorder = _Recorder(raises=RuntimeError("locked"))
    queue = _queue(monkeypatch, recorder, notify_failure=failures.append, save_delay_seconds=60)

    queue.schedule({"ui.theme": "dark"})
    await queue.flush()
    assert failures == []

    queue.schedule({"ui.theme": "light"})
    await queue.flush(notify_on_failure=True)
    assert [str(exc) for exc in failures] == ["locked"]


async def test_immediate_write_commits_the_pending_batch_before_later_edits(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    receipts: list[str] = []
    key = "approval.default_mode"
    queue = _queue(monkeypatch, recorder, on_written=lambda result: receipts.append(result.written[key]))

    queue.schedule({key: "manual"})
    await queue.persist_immediately({key: "bypass"})
    queue.schedule({key: "auto"})
    assert await queue.flush()
    assert receipts == ["manual", "bypass", "auto"]


async def test_flush_joins_an_immediate_write_after_the_debounce_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    from threading import Event

    from tests.support.waiting import DEFAULT_WAIT_TIMEOUT

    recorder = _Recorder()
    queue = _queue(monkeypatch, recorder)
    key = "approval.default_mode"

    release = Event()
    committed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def hold_receipt(values, **kwargs):
        result = recorder.persist(values, **kwargs)
        if values[key] == "bypass":
            loop.call_soon_threadsafe(committed.set)
            assert release.wait(DEFAULT_WAIT_TIMEOUT)
        return result

    monkeypatch.setattr(settings_persistence, "persist", hold_receipt)

    queue.schedule({key: "manual"})
    debounce = queue.save_task
    assert debounce is not None
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(queue.persist_immediately({key: "bypass"}), eager_start=True)
        await debounce
        await asyncio.wait_for(committed.wait(), DEFAULT_WAIT_TIMEOUT)
        flush = tasks.create_task(queue.flush(), eager_start=True)
        try:
            assert not flush.done()
        finally:
            release.set()
    assert flush.result() is True
    assert [values[key] for values, _, _ in recorder.calls] == ["manual", "bypass"]


async def test_settings_queue_an_empty_schedule_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    queue = _queue(monkeypatch, recorder)

    queue.schedule()
    queue.schedule({}, remove=())

    assert queue.save_task is None
    assert recorder.calls == []
