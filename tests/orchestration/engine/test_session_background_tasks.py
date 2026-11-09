# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rollback and delete stop the session's background commands."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import UserRollback
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.service.mutations.snapshot_files import write_rollback_snapshot
from chrys.service.state.store import JsonFileStateStore, session_write_lock_path
from chrys.service.tools.background import BackgroundTask, BackgroundTasks
from tests.orchestration.engine._rollback_helpers import (
    _make_engine,
    _state_after_turns,
    fake_restore_factory,
    seed_turns,
)
from tests.support.background_tasks import HeldProcess


def _adopt(tasks: BackgroundTasks, command: str) -> tuple[BackgroundTask, HeldProcess]:
    process = HeldProcess()
    task = tasks.adopt(process, command=command, trigger="timeout")
    assert task is not None
    return task, process


async def test_rollback_stops_only_the_commands_of_the_turns_it_removes(tmp_path: Path, *, engine_services) -> None:
    engine = _make_engine(tmp_path, engine_services=engine_services)
    store = await seed_turns(engine, 3, engine_services=engine_services)
    engine.lifecycle.on_session_restore = fake_restore_factory(engine, store, engine_services=engine_services)  # type: ignore[assignment]
    tasks = BackgroundTasks(tmp_path / "bg", turn=lambda: engine.session.turn_number)
    engine.session.background_tasks = tasks
    try:
        engine.session.turn_number = 1
        kept, kept_process = _adopt(tasks, "serve")
        engine.session.turn_number = 3
        removed, _ = _adopt(tasks, "watch")

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        assert removed.outcome == "stopped by rollback"
        assert tasks.running == [kept]
        assert kept_process.signals == []
    finally:
        await tasks.close()


async def test_rolling_back_every_turn_stops_every_command_before_the_session_resets(
    tmp_path: Path, *, engine_services
) -> None:
    engine = _make_engine(tmp_path, engine_services=engine_services)
    store = engine_services(engine).persistence.state_store
    assert isinstance(store, JsonFileStateStore)
    await store.save_session("rb_test", _state_after_turns(1))
    await asyncio.to_thread(
        write_rollback_snapshot,
        session_dir=store.session_dir("rb_test"),
        session_id="rb_test",
        turn_number=2,
        keep=10,
        lock_path=session_write_lock_path(tmp_path, "rb_test"),
    )
    engine_services(engine).history.bind(_state_after_turns(2))
    tasks = BackgroundTasks(tmp_path / "bg", turn=lambda: engine.session.turn_number)
    engine.session.background_tasks = tasks
    seen: list[str] = []

    async def _reset(
        _session_id: str,
        *,
        write_lock_held: bool = False,
        after_delete: Callable[[], Awaitable[None]] | None = None,
        before_restart: Callable[[], None] | None = None,
    ) -> bool:
        _ = write_lock_held, after_delete, before_restart
        seen.append(running.outcome)
        return True

    engine.lifecycle.reset_session_to_welcome = _reset  # type: ignore[assignment]
    try:
        engine.session.turn_number = 1
        running, _ = _adopt(tasks, "serve")
        engine.session.turn_number = 2
        built, build = _adopt(tasks, "build")
        build.exit(0)
        await built.ended.wait()

        await engine._on_user_rollback(UserRollback(target_turn=0, revert_changes=False))

        assert seen == ["stopped by rollback"]
        # The session goes on with the same registry, and nothing of the removed turns is listed.
        assert engine.session.background_tasks is tasks
        assert tasks.reminder("bash") is None
        assert tasks.adopt(HeldProcess(), command="later", trigger="timeout") is not None
    finally:
        await tasks.close()


async def test_a_failed_delete_of_the_live_session_keeps_it_able_to_move_commands_to_the_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=JsonFileStateStore(tmp_path))
    engine.session.adopt_restore_identity(session_id="live", recovered_from_sidecar=False)
    monkeypatch.setattr(engine.session.guard, "owns", lambda _session_id: True)
    monkeypatch.setattr(engine.lifecycle, "fire_session_end_hooks", AsyncMock())
    monkeypatch.setattr(engine.lifecycle, "close_trajectory_log", AsyncMock())
    monkeypatch.setattr(
        engine_services(engine).persistence, "delete_session", AsyncMock(side_effect=TimeoutError("busy"))
    )
    tasks = BackgroundTasks(tmp_path / "bg")
    engine.session.background_tasks = tasks
    try:
        task, _ = _adopt(tasks, "serve")

        failure = await engine.lifecycle._delete_session_reporting("live")

        assert failure is not None and failure.code == "session_busy"
        assert task.outcome == "stopped at session end"
        assert engine.session.background_tasks is tasks
        assert tasks.adopt(HeldProcess(), command="later", trigger="timeout") is not None
    finally:
        await tasks.close()
