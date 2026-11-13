# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One build shares connection attempts without sharing consumer leases."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import Warning as WarningEvent
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.mcp.cache import MCPConnectionCache
from chrys.service.mcp.errors import MCPConnectionError, MCPToolConfigurationError
from chrys.service.profiles.agents.schema import MCPServerConfig
from tests.service.mcp.test_cache import _FakeMCPTool
from tests.support.waiting import wait_for


async def test_sequential_failure_is_shared_only_until_the_build_ends(caplog: pytest.LogCaptureFixture) -> None:
    cache = MCPConnectionCache()
    config = MCPServerConfig(name="srv", transport="stdio", command="unused")
    bad = _FakeMCPTool(enter_error=RuntimeError("offline"))
    good = _FakeMCPTool()
    bus = EventBus()
    warnings: list[WarningEvent] = []

    async def record_warning(event: WarningEvent) -> None:
        warnings.append(event)

    await bus.subscribe(WarningEvent, record_warning)
    adapters = [MCPAdapter(cache=cache, bus=bus) for _ in range(4)]
    caplog.set_level(logging.DEBUG, logger="chrys.service.mcp")
    try:
        with patch("chrys.service.mcp._connection._create_mcp_tool", side_effect=[bad, good]) as factory:
            async with cache.acquisition_scope() as scope:
                for adapter in adapters:
                    assert await adapter.connect_all([config], acquisition_scope=scope) == []
                    assert "srv" in adapter.failures
                assert factory.call_count == 1
                assert len(warnings) == 4
                assert sum(record.exc_info is not None for record in caplog.records) == 1
                assert sum("MCP connection attempt for server" in record.message for record in caplog.records) == 1
                assert sum("Reusing this build's MCP acquisition" in record.message for record in caplog.records) == 3
            async with cache.acquisition_scope() as scope:
                for adapter in adapters:
                    assert await adapter.connect_all([config], acquisition_scope=scope)
                    assert adapter.failures == {}
                assert factory.call_count == 2
        assert len({id(adapter._leases["srv"].functions[0]) for adapter in adapters}) == 4
    finally:
        for adapter in adapters:
            await adapter.disconnect_all()
        await cache.close_all()
        await bus.unsubscribe(WarningEvent, record_warning)
    assert bad.exit_count == good.exit_count == 1


async def test_scope_holds_the_connection_while_consumers_release_their_own_leases() -> None:
    cache = MCPConnectionCache(idle_ttl_seconds=0)
    config = MCPServerConfig(name="srv", transport="stdio", command="unused")
    fake = _FakeMCPTool()
    try:
        with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake) as factory:
            async with cache.acquisition_scope() as scope:
                first = await cache.acquire(config, acquisition_scope=scope)
                await cache.release(first)
                second = await cache.acquire(config, acquisition_scope=scope)
                assert first is not second
                assert first.functions[0] is not second.functions[0]
                assert first.mcp_tool is second.mcp_tool
                assert factory.call_count == 1
                await cache.release(second)
                assert fake.exit_count == 0
            assert fake.exit_count == 1
    finally:
        await cache.close_all()


async def test_cancelled_consumer_leaves_the_shared_attempt_for_the_next_consumer() -> None:
    cache = MCPConnectionCache()
    config = MCPServerConfig(name="srv", transport="stdio", command="unused")
    entered, proceed = asyncio.Event(), asyncio.Event()

    class SlowTool(_FakeMCPTool):
        async def __aenter__(self) -> SlowTool:
            self.enter_count += 1
            entered.set()
            await proceed.wait()
            return self

    fake = SlowTool()
    first = None
    try:
        with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake) as factory:
            async with cache.acquisition_scope() as scope:
                first = asyncio.create_task(cache.acquire(config, acquisition_scope=scope))
                await wait_for(lambda: entered.is_set() or first.done())
                if first.done():
                    await first
                first.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await first
                proceed.set()
                lease = await cache.acquire(config, acquisition_scope=scope)
                assert factory.call_count == fake.enter_count == 1
                await cache.release(lease)
    finally:
        proceed.set()
        if first is not None and not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
        await cache.close_all()
    assert fake.exit_count == 1


async def test_cancelled_attempt_is_not_a_cached_connection_failure() -> None:
    cache = MCPConnectionCache()
    config = MCPServerConfig(name="srv", transport="stdio", command="unused")
    fake = _FakeMCPTool()
    try:
        with patch.object(cache, "_open_mcp_tool", side_effect=[asyncio.CancelledError(), fake]) as factory:
            async with cache.acquisition_scope() as scope:
                with pytest.raises(asyncio.CancelledError):
                    await cache.acquire(config, acquisition_scope=scope)
                lease = await cache.acquire(config, acquisition_scope=scope)
                assert factory.await_count == 2
                await cache.release(lease)
    finally:
        await cache.close_all()


async def test_scope_does_not_coalesce_different_effective_configurations() -> None:
    cache = MCPConnectionCache()
    configs = [
        MCPServerConfig(name="srv", transport="stdio", command="unused", env={"TOKEN": token}) for token in ("a", "b")
    ]
    try:
        with patch(
            "chrys.service.mcp._connection._create_mcp_tool", side_effect=[_FakeMCPTool(), _FakeMCPTool()]
        ) as factory:
            async with cache.acquisition_scope() as scope:
                first = await cache.acquire(configs[0], acquisition_scope=scope)
                second = await cache.acquire(configs[1], acquisition_scope=scope)
                assert factory.call_count == 2
                assert first.mcp_tool is not second.mcp_tool
                await cache.release(first)
                await cache.release(second)
    finally:
        await cache.close_all()


async def test_unscoped_acquire_retries_after_a_scoped_failure() -> None:
    cache = MCPConnectionCache()
    config = MCPServerConfig(name="srv", transport="stdio", command="unused")
    try:
        with patch.object(cache, "_open_mcp_tool", side_effect=[MCPConnectionError("srv", "stdio"), _FakeMCPTool()]):
            async with cache.acquisition_scope() as scope:
                with pytest.raises(MCPConnectionError):
                    await cache.acquire(config, acquisition_scope=scope)
                lease = await cache.acquire(config)
                await cache.release(lease)
    finally:
        await cache.close_all()


async def test_consumer_name_collision_does_not_poison_the_shared_connection() -> None:
    cache = MCPConnectionCache()
    config = MCPServerConfig(name="srv", transport="stdio", command="unused")
    bad = MCPAdapter(cache=cache, reserved_tool_names={"remote"})
    good = MCPAdapter(cache=cache)
    try:
        with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=_FakeMCPTool()) as factory:
            async with cache.acquisition_scope() as scope:
                with pytest.raises(MCPToolConfigurationError):
                    await bad.connect(config, acquisition_scope=scope)
                (tool,) = await good.connect(config, acquisition_scope=scope)
                (answer,) = await tool.invoke(value="still usable")
                assert answer.text == "still usable"
                assert factory.call_count == 1
    finally:
        await bad.disconnect_all()
        await good.disconnect_all()
        await cache.close_all()


async def test_scope_rejects_use_after_close_and_use_by_another_cache() -> None:
    cache, other = MCPConnectionCache(), MCPConnectionCache()
    config = MCPServerConfig(name="srv", transport="stdio", command="unused")
    try:
        async with cache.acquisition_scope() as scope:
            with pytest.raises(ValueError, match="different cache"):
                await other.acquire(config, acquisition_scope=scope)
        with pytest.raises(RuntimeError, match="scope is closed"):
            await cache.acquire(config, acquisition_scope=scope)
    finally:
        await cache.close_all()
        await other.close_all()


async def test_concurrent_consumers_share_failure_and_late_consumers_do_not_retry() -> None:
    cache = MCPConnectionCache()
    config = MCPServerConfig(name="srv", transport="stdio", command="unused")
    entered, proceed = asyncio.Event(), asyncio.Event()
    started = 0

    class SlowFailure(_FakeMCPTool):
        async def __aenter__(self) -> SlowFailure:
            self.enter_count += 1
            entered.set()
            await proceed.wait()
            raise RuntimeError("offline")

    fake = SlowFailure()
    tasks = []
    try:
        with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake) as factory:
            async with cache.acquisition_scope() as scope:

                async def acquire() -> None:
                    nonlocal started
                    started += 1
                    with pytest.raises(MCPConnectionError):
                        await cache.acquire(config, acquisition_scope=scope)

                tasks = [asyncio.create_task(acquire()) for _ in range(3)]
                await wait_for(lambda: (entered.is_set() and started == 3) or any(task.done() for task in tasks))
                assert not any(task.done() for task in tasks)
                proceed.set()
                await asyncio.gather(*tasks)
                await acquire()
                assert factory.call_count == 1
    finally:
        proceed.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await cache.close_all()
