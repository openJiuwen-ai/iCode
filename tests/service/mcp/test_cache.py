# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the shared MCP connection cache."""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.platform import get_platform
from chrys.foundation.text.tokenizer import MixedLanguageTokenizer
from chrys.foundation.tool_kinds import get_tool_kind
from chrys.kernel import FunctionTool
from chrys.orchestration.sub_agents.tools import SubAgentTools
from chrys.service.mcp._connection import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    _create_tool_with_timeout_floor,
)
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.mcp.cache import MCPConnectionCache, mcp_config_cache_key, resolve_mcp_effective_config
from chrys.service.mcp.errors import MCPConnectionError
from chrys.service.profiles.agents.schema import AgentProfile, MCPServerConfig, SubAgentRef, ToolsConfig
from chrys.service.profiles.models.schema import ModelProfile


async def _remote_tool(ctx, value: str = "") -> str:
    return value or "ok"


def _function_tool(name: str = "remote") -> FunctionTool:
    return FunctionTool(
        func=_remote_tool,
        name=name,
        description="Remote tool",
        input_model={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
        additional_properties={
            "_mcp_remote_name": name,
            "_mcp_normalized_name": name,
        },
    )


class _FakeMCPTool:
    def __init__(
        self,
        name: str = "srv",
        *,
        functions: list[FunctionTool] | None = None,
        enter_error: Exception | None = None,
    ) -> None:
        self.name = name
        self.functions = functions if functions is not None else [_function_tool()]
        self.request_timeout = DEFAULT_CONNECT_TIMEOUT_SECONDS
        self.dropped_banner_lines: list[str] = []
        self.enter_error = enter_error
        self.enter_count = 0
        self.exit_count = 0

    async def __aenter__(self) -> _FakeMCPTool:
        self.enter_count += 1
        if self.enter_error is not None:
            raise self.enter_error
        return self

    async def __aexit__(self, *args: object) -> None:
        self.exit_count += 1


async def test_shared_cache_connects_once_and_returns_isolated_tool_clones(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = MCPConnectionCache()
    adapter_a = MCPAdapter(cache=cache)
    adapter_b = MCPAdapter(cache=cache)
    config = MCPServerConfig(name="srv", transport="stdio", command="python")
    fake = _FakeMCPTool()

    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake) as factory:
        monkeypatch.setenv("CHRYS_MODEL_PROFILE", "model-a")
        tools_a = await adapter_a.connect(config)
        monkeypatch.setenv("CHRYS_MODEL_PROFILE", "model-b")
        tools_b = await adapter_b.connect(config)

    assert factory.call_count == 1
    assert fake.enter_count == 1
    assert tools_a[0] is not tools_b[0]
    assert tools_a[0].name == tools_b[0].name == "remote"
    assert get_tool_kind(tools_a[0]) == get_tool_kind(tools_b[0]) == "mcp"
    assert tools_a[0].kind is None

    tools_b[0].description = "mutated"
    assert tools_a[0].description == "Remote tool"

    await adapter_a.disconnect_all()
    assert fake.exit_count == 0
    assert adapter_a.server_names == []
    assert adapter_b.server_names == ["srv"]

    await adapter_b.disconnect_all()
    assert fake.exit_count == 0

    await cache.close_all()
    assert fake.exit_count == 1


async def test_shared_connection_applies_each_acquirers_result_cap() -> None:
    cache = MCPConnectionCache()
    adapter_a = MCPAdapter(cache=cache)
    adapter_b = MCPAdapter(cache=cache)
    config_a = MCPServerConfig(
        name="srv",
        transport="stdio",
        command="python",
        max_tool_result_tokens=100,
    )
    config_b = MCPServerConfig(
        name="srv",
        transport="stdio",
        command="python",
        max_tool_result_tokens=500,
    )
    fake = _FakeMCPTool()
    payload = "x" * 10_000

    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake) as factory:
        tools_a = await adapter_a.connect(config_a)
        tools_b = await adapter_b.connect(config_b)

    result_a = await tools_a[0].func(None, value=payload)
    result_b = await tools_b[0].func(None, value=payload)
    tokenizer = MixedLanguageTokenizer()
    assert factory.call_count == 1
    assert tokenizer.count_tokens(result_a) <= 100
    assert tokenizer.count_tokens(result_b) <= 500
    assert len(result_b) > len(result_a)

    await adapter_a.disconnect_all()
    await adapter_b.disconnect_all()
    await cache.close_all()


async def test_shared_cache_adapters_disagree_on_instructions_exposure() -> None:
    """Adapters sharing one cached connection filter instructions independently.

    ``expose_instructions`` is deliberately excluded from the cache key, so two
    profiles that disagree on it still share a single live connection.
    """
    cache = MCPConnectionCache()
    adapter_a = MCPAdapter(cache=cache)
    adapter_b = MCPAdapter(cache=cache)
    exposed = MCPServerConfig(name="srv", transport="stdio", command="python")
    hidden = MCPServerConfig(name="srv", transport="stdio", command="python", expose_instructions=False)
    fake = _FakeMCPTool()
    fake._server_instructions = "Use the remote tool."

    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake) as factory:
        await adapter_a.connect(exposed)
        await adapter_b.connect(hidden)

    assert mcp_config_cache_key(exposed) == mcp_config_cache_key(hidden)
    assert factory.call_count == 1
    assert fake.enter_count == 1

    assert adapter_a.get_server_instructions_map() == {"srv": "Use the remote tool."}
    assert adapter_b.get_server_instructions_map() == {}

    await adapter_a.disconnect_all()
    await adapter_b.disconnect_all()
    await cache.close_all()


async def test_private_adapter_disconnect_all_closes_its_private_cache() -> None:
    adapter = MCPAdapter()
    config = MCPServerConfig(name="srv", transport="stdio", command="python")
    fake = _FakeMCPTool()

    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake):
        await adapter.connect(config)

    await adapter.disconnect_all()

    assert fake.exit_count == 1
    assert adapter.server_names == []


async def test_clone_failure_does_not_leak_refcount() -> None:
    cache = MCPConnectionCache()
    config = MCPServerConfig(name="srv", transport="stdio", command="python")
    fn = _function_tool()

    def fail_parameters() -> dict[str, object]:
        raise RuntimeError("schema broke")

    fn.parameters = fail_parameters
    fake = _FakeMCPTool(functions=[fn])

    with (
        patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake),
        pytest.raises(RuntimeError, match="schema broke"),
    ):
        await cache.acquire(config)

    entry = next(iter(cache._entries.values()))
    assert entry.refcount == 0
    await cache.close_all()
    assert fake.exit_count == 1


async def test_cancelled_cache_hit_during_prune_does_not_leak_refcount() -> None:
    cache = MCPConnectionCache()
    target_config = MCPServerConfig(name="target", transport="stdio", command="python")
    stale_config = MCPServerConfig(name="stale", transport="stdio", command="python")
    target_fake = _FakeMCPTool(name="target")
    stale_fake = _FakeMCPTool(name="stale")

    def factory(config: MCPServerConfig) -> _FakeMCPTool:
        return target_fake if config.name == "target" else stale_fake

    with patch("chrys.service.mcp._connection._create_mcp_tool", side_effect=factory):
        target_lease = await cache.acquire(target_config)
        await cache.release(target_lease)
        stale_lease = await cache.acquire(stale_config)
        await cache.release(stale_lease)

    target_key = target_lease.key
    stale_entry = cache._entries[stale_lease.key]
    stale_entry.last_used = datetime.now(UTC) - timedelta(seconds=600)
    entered_close = asyncio.Event()
    release_close = asyncio.Event()

    async def slow_close_tools(_tools: list[object]) -> None:
        entered_close.set()
        await release_close.wait()

    cache._close_tools = slow_close_tools  # type: ignore[method-assign]
    task = asyncio.create_task(cache.acquire(target_config))
    await asyncio.wait_for(entered_close.wait(), timeout=1.0)
    task.cancel()
    release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert cache._entries[target_key].refcount == 0
    await cache.close_all()


async def test_cancelled_prune_still_closes_removed_tool() -> None:
    cache = MCPConnectionCache()
    config = MCPServerConfig(name="stale", transport="stdio", command="python")
    close_started = asyncio.Event()
    close_continue = asyncio.Event()

    class _SlowExitMCPTool(_FakeMCPTool):
        async def __aexit__(self, *args: object) -> None:
            close_started.set()
            await close_continue.wait()
            await super().__aexit__(*args)

    fake = _SlowExitMCPTool()

    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake):
        lease = await cache.acquire(config)
        await cache.release(lease)

    cache._entries[lease.key].last_used = datetime.now(UTC) - timedelta(seconds=600)
    prune_task = asyncio.create_task(cache.prune_idle())
    await asyncio.wait_for(close_started.wait(), timeout=1.0)
    prune_task.cancel()
    close_continue.set()

    with pytest.raises(asyncio.CancelledError):
        await prune_task

    assert fake.exit_count == 1
    assert lease.key not in cache._entries


async def test_sub_agent_partial_register_cleanup_releases_mcp_adapter() -> None:
    cache = MCPConnectionCache()
    sub_agent_tools = SubAgentTools(mcp_cache=cache)
    config = MCPServerConfig(name="srv", transport="stdio", command="python")
    profile = AgentProfile(name="Explore", tools=ToolsConfig(mcp=[config]))
    fake = _FakeMCPTool()

    def fail_context(*args: object, **kwargs: object) -> None:
        # Registration returns only after rollback. Observe the acquired lease at the
        # fault boundary; cleanup after failed registration is now idempotent.
        entry = next(iter(cache._entries.values()))
        assert entry.refcount == 1
        raise RuntimeError("ctx failed")

    with (
        patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake),
        patch("chrys.orchestration.sub_agents.tools.create_client", return_value=MagicMock(aclose=AsyncMock())),
        patch("chrys.orchestration.invoker.runtime.ContextManager", side_effect=fail_context),
        pytest.raises(RuntimeError, match="ctx failed"),
    ):
        await sub_agent_tools.register(
            SubAgentRef(profile="Explore"),
            profile,
            MagicMock(),
            settings=Settings(),
            fallback_profile=ModelProfile(id="test-id", name="test"),
        )

    entry = next(iter(cache._entries.values()))
    assert entry.refcount == 0
    await sub_agent_tools.cleanup()
    assert entry.refcount == 0
    await cache.close_all()
    assert fake.exit_count == 1


def test_effective_stdio_snapshot_is_used_for_tool_creation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Resolved env and cwd overrides should not be re-read during tool creation."""
    monkeypatch.delenv("CHRYS_SECOND_PASS", raising=False)
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    config = MCPServerConfig(name="srv", transport="stdio", command="python", env={"TOKEN": "{{CHRYS_SECOND_PASS}}"})

    tool = _create_tool_with_timeout_floor(
        config,
        DEFAULT_CONNECT_TIMEOUT_SECONDS,
        resolved_stdio_env={"TOKEN": "{{CHRYS_SECOND_PASS}}"},
        stdio_cwd=str(cwd),
    )

    assert tool.env == {"TOKEN": "{{CHRYS_SECOND_PASS}}"}
    with patch("chrys.service.mcp._stdio_transport.tolerant_stdio_client") as stdio_client:
        tool.get_mcp_client()
    args, kwargs = stdio_client.call_args
    assert args == ()
    assert kwargs["errlog"] is not None
    assert kwargs["inherit_env"] is False
    assert kwargs["server"].cwd == str(cwd)


def test_http_headers_can_bypass_env_template_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret-value")
    config = MCPServerConfig(
        name="srv",
        transport="http",
        url="https://example.test/mcp",
        headers={"Authorization": "Bearer {{OPENAI_API_KEY}}"},
        resolve_header_templates=False,
    )

    effective = resolve_mcp_effective_config(config)
    tool = _create_tool_with_timeout_floor(config, DEFAULT_CONNECT_TIMEOUT_SECONDS)

    assert effective.http_headers == {"Authorization": "Bearer {{OPENAI_API_KEY}}"}
    assert tool._static_headers == {"Authorization": "Bearer {{OPENAI_API_KEY}}"}


def _surrogateescaped_dir(tmp_path: Path, name: bytes) -> tuple[bytes, str]:
    if not get_platform().is_linux:
        pytest.skip("invalid-UTF-8 filesystem paths are Linux-specific")
    raw_path = os.path.join(os.fsencode(tmp_path), name)
    os.mkdir(raw_path)
    return raw_path, os.fsdecode(raw_path)


def test_stdio_cache_key_serializes_raw_getcwd_without_aliasing_literal_escape() -> None:
    raw_cwd = "/work/pro\udcffject"
    literal_escape_cwd = r"/work/pro\udcffject"
    config = MCPServerConfig(name="srv", transport="stdio", command="python")

    with patch("chrys.service.mcp.cache.os.getcwd", return_value=raw_cwd):
        effective = resolve_mcp_effective_config(config)

    assert effective.resolved_stdio_cwd == raw_cwd
    assert effective.key
    assert effective.key != mcp_config_cache_key(config, stdio_cwd=literal_escape_cwd)


def test_stdio_tool_creation_preserves_explicit_surrogateescaped_cwd(tmp_path: Path) -> None:
    raw_bytes, raw_cwd = _surrogateescaped_dir(tmp_path, b"mcp-explicit-\xfe")
    literal_escape_cwd = f"{raw_cwd[:-1]}\\udcfe"
    config = MCPServerConfig(name="srv", transport="stdio", command="python")

    effective = resolve_mcp_effective_config(config, stdio_cwd=raw_cwd)
    tool = _create_tool_with_timeout_floor(
        config,
        DEFAULT_CONNECT_TIMEOUT_SECONDS,
        resolved_stdio_env=effective.stdio_env,
        stdio_cwd=effective.resolved_stdio_cwd,
    )
    with patch("chrys.service.mcp._stdio_transport.tolerant_stdio_client") as stdio_client:
        tool.get_mcp_client()
    _args, kwargs = stdio_client.call_args

    assert effective.resolved_stdio_cwd == raw_cwd
    assert os.fsencode(effective.resolved_stdio_cwd) == raw_bytes
    assert os.path.isdir(effective.resolved_stdio_cwd)
    assert kwargs["server"].cwd == raw_cwd
    assert os.fsencode(kwargs["server"].cwd) == raw_bytes
    assert effective.key != mcp_config_cache_key(config, stdio_cwd=literal_escape_cwd)


async def test_failures_are_not_cached_and_next_acquire_retries() -> None:
    cache = MCPConnectionCache()
    adapter = MCPAdapter(cache=cache)
    config = MCPServerConfig(name="srv", transport="stdio", command="python")
    bad = _FakeMCPTool(enter_error=RuntimeError("boom"))
    good = _FakeMCPTool()

    with patch("chrys.service.mcp._connection._create_mcp_tool", side_effect=[bad, good]) as factory:
        with pytest.raises(MCPConnectionError):
            await adapter.connect(config)

        assert factory.call_count == 1
        assert "srv" in adapter.failures

        tools = await adapter.connect(config)

    assert factory.call_count == 2
    assert tools[0].name == "remote"
    assert adapter.failures == {}
    await adapter.disconnect_all()
    await cache.close_all()


async def test_acquire_after_close_all_raises() -> None:
    cache = MCPConnectionCache()
    await cache.close_all()

    with pytest.raises(RuntimeError, match="closed"):
        await cache.acquire(MCPServerConfig(name="srv", transport="stdio", command="python"))


async def test_cancelled_waiter_does_not_cancel_shared_connect_task() -> None:
    cache = MCPConnectionCache()
    config = MCPServerConfig(name="srv", transport="stdio", command="python")
    entered = asyncio.Event()
    release_enter = asyncio.Event()

    class _SlowMCPTool(_FakeMCPTool):
        async def __aenter__(self) -> _SlowMCPTool:
            self.enter_count += 1
            entered.set()
            await release_enter.wait()
            return self

    fake = _SlowMCPTool()

    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake) as factory:
        first = asyncio.create_task(cache.acquire(config))
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        second = asyncio.create_task(cache.acquire(config))
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first
        release_enter.set()
        lease = await asyncio.wait_for(second, timeout=1.0)

    assert factory.call_count == 1
    assert lease.functions[0].name == "remote"
    await cache.release(lease)
    await cache.close_all()
    assert fake.exit_count == 1


async def test_cwd_change_creates_distinct_cache_entry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache = MCPConnectionCache()
    config = MCPServerConfig(name="srv", transport="stdio", command="python")
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    calls = 0

    def counted_factory(_config: MCPServerConfig) -> _FakeMCPTool:
        nonlocal calls
        calls += 1
        return _FakeMCPTool(functions=[_function_tool(f"remote_{calls}")])

    with patch("chrys.service.mcp._connection._create_mcp_tool", side_effect=counted_factory):
        monkeypatch.chdir(first_dir)
        lease_a = await cache.acquire(config)
        await cache.release(lease_a)
        monkeypatch.chdir(second_dir)
        lease_b = await cache.acquire(config)

    assert calls == 2
    assert lease_a.key != lease_b.key
    await cache.release(lease_b)
    await cache.close_all()


async def test_explicit_stdio_cwd_controls_cache_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cache = MCPConnectionCache()
    config = MCPServerConfig(name="srv", transport="stdio", command="python")
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    process_dir = tmp_path / "process"
    first_dir.mkdir()
    second_dir.mkdir()
    process_dir.mkdir()
    calls = 0

    def counted_factory(_config: MCPServerConfig) -> _FakeMCPTool:
        nonlocal calls
        calls += 1
        return _FakeMCPTool(functions=[_function_tool(f"remote_{calls}")])

    with patch("chrys.service.mcp._connection._create_mcp_tool", side_effect=counted_factory):
        monkeypatch.chdir(process_dir)
        lease_a = await cache.acquire(config, stdio_cwd=str(first_dir))
        await cache.release(lease_a)
        monkeypatch.chdir(second_dir)
        lease_b = await cache.acquire(config, stdio_cwd=str(first_dir))
        await cache.release(lease_b)
        lease_c = await cache.acquire(config, stdio_cwd=str(second_dir))

    assert calls == 2
    assert lease_a.key == lease_b.key
    assert lease_c.key != lease_a.key
    await cache.release(lease_c)
    await cache.close_all()


def test_cache_key_tracks_inherited_stdio_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    config = MCPServerConfig(name="srv", transport="stdio", command="python")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("CHRYS_CACHE_TEST_ENV", raising=False)

    key_a = mcp_config_cache_key(config)
    monkeypatch.setenv("CHRYS_CACHE_TEST_ENV", "one")
    key_b = mcp_config_cache_key(config)
    monkeypatch.setenv("NO_PROXY", "localhost")
    key_c = mcp_config_cache_key(config)

    assert key_a != key_b
    assert key_c != key_b

    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "new-inherited-choice")
    assert mcp_config_cache_key(config) == key_c
    config.env = {"CHRYS_MODEL_PROFILE": "explicit-choice"}
    assert mcp_config_cache_key(config) != key_c


def test_cache_key_distinguishes_unspecified_empty_and_named_allowed_tools() -> None:
    common = {"name": "srv", "transport": "stdio", "command": "python"}
    key_all = mcp_config_cache_key(MCPServerConfig(**common))
    key_none = mcp_config_cache_key(MCPServerConfig(**common, allowed_tools=[]))
    key_named = mcp_config_cache_key(MCPServerConfig(**common, allowed_tools=["echo"]))

    assert len({key_all, key_none, key_named}) == 3


async def test_clone_preserves_out_of_band_tool_context() -> None:
    """additional_properties dict-copy does not carry attributes — the clone must re-apply them."""
    from chrys.foundation.tool_call_context import get_tool_context, set_tool_context
    from chrys.service.mcp.cache import clone_mcp_function_tool

    source = _function_tool()
    set_tool_context(source, {"server_name": "srv", "remote_name": "remote", "normalized_name": "remote"})

    clone = clone_mcp_function_tool(source)

    assert clone is not source
    assert get_tool_context(clone) == {"server_name": "srv", "remote_name": "remote", "normalized_name": "remote"}
    assert get_tool_context(clone) is not get_tool_context(source), "clones must not share the context mapping"

    bare_clone = clone_mcp_function_tool(_function_tool())
    assert get_tool_context(bare_clone) is None


async def test_shared_cache_clones_carry_static_context_per_consumer() -> None:
    from chrys.foundation.tool_call_context import get_tool_context, set_tool_context

    cache = MCPConnectionCache()
    adapter_a = MCPAdapter(cache=cache)
    adapter_b = MCPAdapter(cache=cache)
    config = MCPServerConfig(name="srv", transport="stdio", command="python")
    source = _function_tool()
    set_tool_context(source, {"server_name": "srv", "remote_name": "remote", "normalized_name": "remote"})
    fake = _FakeMCPTool(functions=[source])

    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake):
        tools_a = await adapter_a.connect(config)
        tools_b = await adapter_b.connect(config)

    try:
        expected = {"server_name": "srv", "remote_name": "remote", "normalized_name": "remote"}
        assert get_tool_context(tools_a[0]) == expected
        assert get_tool_context(tools_b[0]) == expected
        assert get_tool_context(tools_a[0]) is not get_tool_context(tools_b[0])
    finally:
        await adapter_a.disconnect_all()
        await adapter_b.disconnect_all()
        await cache.close_all()
