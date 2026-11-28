# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The real-session harness prepares cold dependencies before timing UI readiness."""

from __future__ import annotations

import threading
from unittest.mock import create_autospec

from tests.app.tui.screens import test_prompt_shortcut_send as harness
from tests.support.async_tasks import running_task
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, ENGINE_TURN_TIMEOUT, wait_for, with_wait_deadline


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_cold_sdk_preparation_finishes_before_the_app_starts(agent_engine, tmp_path, monkeypatch) -> None:
    importing = threading.Event()
    release_import = threading.Event()
    prepared = threading.Event()
    preload = harness.preload_sdks
    make_chrys_app = harness.make_chrys_app

    def held_preload() -> None:
        importing.set()
        assert release_import.wait(ENGINE_TURN_TIMEOUT), "test did not release SDK preparation"
        preload()
        prepared.set()

    def open_after_preload(*args, **kwargs):
        assert prepared.is_set(), "app started before SDK preparation finished"
        return make_chrys_app(*args, **kwargs)

    make_app = create_autospec(make_chrys_app, side_effect=open_after_preload)
    monkeypatch.setattr(harness, "preload_sdks", held_preload)
    monkeypatch.setattr(harness, "make_chrys_app", make_app)

    async def start_session() -> None:
        async with harness.running_session(agent_engine, tmp_path, monkeypatch) as session:
            assert session.seen.of(harness.ev.SessionReady)
            assert session.app.screen is session.main
            assert not session.bar.agent_loading

    async with running_task(start_session()) as task:
        try:
            await wait_for(lambda: importing.is_set() or task.done(), description="cold SDK preparation started")
            if task.done():
                await task
            assert importing.is_set()
            make_app.assert_not_called()
        finally:
            release_import.set()
        await task
    make_app.assert_called_once()
