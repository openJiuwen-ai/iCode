# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Named close boundaries preserve teardown ordering and save suppression."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.service.hooks.manager import HookManager
from tests.support.loaded_agents import install_loaded_agent


@pytest.mark.parametrize(
    ("entry", "release_lock", "close_mcp"),
    [("session", True, False), ("in_place", False, False), ("shutdown", True, True)],
)
async def test_close_entry_resource_combinations(
    entry: str, release_lock: bool, close_mcp: bool, monkeypatch: pytest.MonkeyPatch, agent_engine
) -> None:
    engine = agent_engine(EventBus(), settings=Settings())
    release = Mock()
    completed: list[str] = []

    async def close_mcp_cache() -> None:
        await asyncio.sleep(0)
        completed.append("closed")

    close = AsyncMock(spec=engine.loader.close, side_effect=close_mcp_cache)
    monkeypatch.setattr(engine.session.guard, "release", release)
    monkeypatch.setattr(engine.loader, "close", close)

    if entry == "session":
        await engine.lifecycle.close_session()
    elif entry == "in_place":
        await engine.lifecycle.close_session_in_place()
    else:
        await engine.shutdown()

    if release_lock:
        release.assert_called_once_with()
    else:
        release.assert_not_called()
    if close_mcp:
        close.assert_awaited_once_with()
        assert completed == ["closed"]
    else:
        close.assert_not_awaited()
        assert completed == []


async def test_close_failure_keeps_cleared_resources_and_skips_later_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    manager = Mock(spec=HookManager)
    manager.drain_session = AsyncMock()
    engine.session.hook_manager = manager
    monkeypatch.setattr(engine.lifecycle, "fire_session_end_hooks", AsyncMock())
    monkeypatch.setattr(engine.writer, "save_current_session", AsyncMock(side_effect=RuntimeError("save failed")))
    release_current = AsyncMock()
    release_lock = Mock()
    monkeypatch.setattr(engine.loader, "release_current", release_current)
    monkeypatch.setattr(engine.session.guard, "release", release_lock)
    engine.turns.turn_state.paused_sub_agents.add("child")
    completed_task = asyncio.create_task(asyncio.sleep(0))
    await completed_task
    engine.turns.turn_state.lease.run_task = completed_task

    with pytest.raises(RuntimeError, match="save failed"):
        await engine.lifecycle.close_session()

    assert engine.session.shutting_down is True
    assert engine.turns.turn_state.lease.run_task is None
    assert engine.session.hook_manager is None
    manager.drain_session.assert_awaited_once()
    release_current.assert_not_awaited()
    release_lock.assert_not_called()
    assert engine.turns.turn_state.paused_sub_agents == {"child"}


async def test_the_last_live_engine_to_shut_down_stops_the_bundled_sandbox(
    monkeypatch: pytest.MonkeyPatch, agent_engine
) -> None:
    """An engine counts once it takes events; one that never did cannot stop another's server."""
    from chrys.service.sandbox import runtime_hold, supervisor

    monkeypatch.setattr(runtime_hold, "_runtimes", 0)
    stops: list[str] = []

    async def shutdown() -> None:
        stops.append("shutdown")

    monkeypatch.setattr(supervisor, "shutdown", shutdown)
    first = agent_engine(EventBus(), settings=Settings())
    second = agent_engine(EventBus(), settings=Settings())
    never_prepared = agent_engine(EventBus(), settings=Settings())
    await first.prepare()
    await second.prepare()

    await never_prepared.shutdown()
    await first.shutdown()
    assert stops == []
    await second.shutdown()
    assert stops == ["shutdown"]


@pytest.mark.parametrize("initial_suppression", [False, True])
async def test_timeout_suppresses_save_and_restores_entry_value(
    initial_suppression: bool, monkeypatch: pytest.MonkeyPatch, agent_engine
) -> None:
    engine = agent_engine(EventBus(), settings=Settings())
    install_loaded_agent(engine)
    engine.session.suppress_save = initial_suppression
    save = engine.writer.save_current_session
    observed: list[bool] = []
    results: list[bool] = []

    async def observe_save() -> bool:
        observed.append(engine.session.suppress_save)
        result = await save()
        results.append(result)
        return result

    monkeypatch.setattr(engine.writer, "save_current_session", observe_save)
    monkeypatch.setattr("chrys.orchestration.engine.session_lifecycle._SHUTDOWN_POST_RUN_TIMEOUT_SECONDS", 0)
    task = asyncio.create_task(asyncio.Event().wait())
    engine.turns.turn_state.lease.run_task = task

    await engine.shutdown()

    assert task.cancelled()
    assert engine.turns.turn_state.shutdown_used_cancel_fallback is True
    assert observed == [True]
    assert results == [False]
    assert engine.session.suppress_save is initial_suppression
