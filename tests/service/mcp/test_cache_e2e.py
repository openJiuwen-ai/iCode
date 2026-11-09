# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""End-to-end MCP cache tests using real stdio and HTTP MCP servers."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import chrys.orchestration.engine.build.builder as agent_builder_module
import chrys.orchestration.sub_agents.tools as sub_agent_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.orchestration.engine.build.builder import build_agent
from chrys.orchestration.invoker import runtime as runtime_factory_module
from chrys.service.agent_middleware import ResponseBatchCounter
from chrys.service.approval.policy import ApprovalMode
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.mcp.cache import MCPConnectionCache
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    MCPServerConfig,
    SubAgentRef,
    SubAgentsConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.support.waiting import wait_for

PROBE_SERVER = Path(__file__).with_name("mcp_cache_probe_server.py")


def _stdio_probe_config(
    state_file: Path,
    *,
    env: dict[str, str] | None = None,
    startup_delay: float = 0,
) -> MCPServerConfig:
    args = [
        str(PROBE_SERVER),
        "--transport",
        "stdio",
        "--state-file",
        str(state_file),
    ]
    if startup_delay > 0:
        args.extend(["--startup-delay", str(startup_delay)])
    return MCPServerConfig(
        name="cache-probe",
        transport="stdio",
        command=sys.executable,
        args=args,
        env=env or {},
    )


def _read_events(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _event_count(path: Path, event: str) -> int:
    return sum(1 for item in _read_events(path) if item.get("event") == event)


async def _wait_for_event_count(path: Path, event: str, count: int, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if _event_count(path, event) >= count:
            return
        await asyncio.sleep(0.02)
    raise TimeoutError(f"Timed out waiting for {count} {event!r} events in {path}")


def _tool_by_name(tools: list[object], name: str) -> object:
    return next(tool for tool in tools if getattr(tool, "name", None) == name)


async def _call_probe(tools: list[object], label: str) -> dict[str, object]:
    tool = _tool_by_name(tools, "probe")
    result = await tool.invoke(arguments={"label": label})
    text = "\n".join(content.text for content in result)
    return json.loads(text)


async def _wait_for_port(port: int) -> None:
    deadline = asyncio.get_running_loop().time() + 10
    last_error: OSError | None = None
    while asyncio.get_running_loop().time() < deadline:
        try:
            _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError as exc:
            last_error = exc
            await asyncio.sleep(0.05)
            continue
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return
    raise TimeoutError(f"MCP HTTP probe server did not listen on {port}") from last_error


@contextlib.asynccontextmanager
async def _http_probe_server(tmp_path: Path) -> AsyncIterator[tuple[MCPServerConfig, Path]]:
    state_file = tmp_path / "http-probe.jsonl"
    port_file = tmp_path / "http-probe.port"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(PROBE_SERVER),
        "--transport",
        "http",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
        "--port-file",
        str(port_file),
        "--state-file",
        str(state_file),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        # The probe server creates the file before it writes the port.
        await wait_for(
            lambda: (
                (port_file.exists() and port_file.read_text(encoding="utf-8").isdigit()) or proc.returncode is not None
            ),
            timeout=10,
            interval=0.02,
            description="MCP HTTP probe server assigned port",
        )
        if proc.returncode is not None:
            raise RuntimeError(f"MCP HTTP probe server exited with {proc.returncode} before reporting its port")
        port = int(port_file.read_text(encoding="utf-8"))
        await _wait_for_port(port)
        yield (
            MCPServerConfig(
                name="cache-probe-http",
                transport="http",
                url=f"http://127.0.0.1:{port}/mcp",
                bypass_proxy=True,
            ),
            state_file,
        )
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                proc.kill()
                await proc.wait()


@pytest.mark.integration
async def test_real_stdio_cache_reuses_process_across_adapters_and_reacquire(tmp_path: Path) -> None:
    """Two adapters with the same stdio config share one real MCP subprocess."""
    cache = MCPConnectionCache()
    adapter_a = MCPAdapter(cache=cache)
    adapter_b = MCPAdapter(cache=cache)
    state_file = tmp_path / "stdio-probe.jsonl"
    config = _stdio_probe_config(state_file)

    tools_a = await adapter_a.connect(config)
    tools_b = await adapter_b.connect(config)

    assert tools_a[0] is not tools_b[0]
    assert adapter_a._leases[config.name].mcp_tool is adapter_b._leases[config.name].mcp_tool
    assert _event_count(state_file, "start") == 1
    # MCPAdapter must deliver every tool out of the MCP domain as a chrys
    # FunctionTool clone, on every return path.
    from chrys.kernel import FunctionTool as ChrysFunctionTool

    assert all(type(t) is ChrysFunctionTool for t in [*tools_a, *tools_b])

    first = await _call_probe(tools_a, "first")
    second = await _call_probe(tools_b, "second")
    assert second["pid"] == first["pid"]
    assert second["call_count"] == 2

    await adapter_a.disconnect_all()
    assert cache._entries[adapter_b._leases[config.name].key].refcount == 1
    third = await _call_probe(tools_b, "after-one-release")
    assert third["pid"] == first["pid"]
    assert third["call_count"] == 3

    await adapter_b.disconnect_all()
    assert cache._entries[next(iter(cache._entries))].refcount == 0

    adapter_c = MCPAdapter(cache=cache)
    tools_c = await adapter_c.connect(config)
    warm = await _call_probe(tools_c, "warm-reacquire")
    assert warm["pid"] == first["pid"]
    assert warm["call_count"] == 4
    assert _event_count(state_file, "start") == 1

    await adapter_c.disconnect_all()
    await cache.close_all()


@pytest.mark.integration
async def test_real_stdio_inflight_cancelled_waiter_still_shares_single_process(tmp_path: Path) -> None:
    """Cancelling one real-connect waiter must not cancel or double-spawn the shared connect."""
    cache = MCPConnectionCache()
    state_file = tmp_path / "stdio-cancel-probe.jsonl"
    config = _stdio_probe_config(state_file, startup_delay=0.5)
    adapter_cancelled = MCPAdapter(cache=cache)
    adapter_survivor = MCPAdapter(cache=cache)

    first = asyncio.create_task(adapter_cancelled.connect(config))
    await _wait_for_event_count(state_file, "start", 1)
    second = asyncio.create_task(adapter_survivor.connect(config))

    first.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first

    tools = await asyncio.wait_for(second, timeout=10)
    assert adapter_cancelled.server_names == []
    assert adapter_survivor.server_names == [config.name]
    assert _event_count(state_file, "start") == 1
    assert cache._entries[adapter_survivor._leases[config.name].key].refcount == 1

    probe = await _call_probe(tools, "survivor")
    assert probe["call_count"] == 1

    await adapter_survivor.disconnect_all()
    assert cache._entries[next(iter(cache._entries))].refcount == 0

    warm_adapter = MCPAdapter(cache=cache)
    warm_tools = await warm_adapter.connect(config)
    warm = await _call_probe(warm_tools, "warm-after-cancel")
    assert warm["pid"] == probe["pid"]
    assert warm["call_count"] == 2
    assert _event_count(state_file, "start") == 1

    await warm_adapter.disconnect_all()
    await cache.close_all()

    with pytest.raises(RuntimeError, match="closed"):
        await cache.acquire(config)


@pytest.mark.integration
async def test_real_stdio_cache_key_separates_different_effective_env(tmp_path: Path) -> None:
    """Changing stdio env creates a separate real MCP subprocess instead of reusing stale env."""
    cache = MCPConnectionCache()
    state_file = tmp_path / "stdio-env-probe.jsonl"
    config_a = _stdio_probe_config(state_file, env={"CHRYS_E2E_TOKEN": "one"})
    config_b = _stdio_probe_config(state_file, env={"CHRYS_E2E_TOKEN": "two"})
    adapter_a = MCPAdapter(cache=cache)
    adapter_b = MCPAdapter(cache=cache)

    tools_a = await adapter_a.connect(config_a)
    tools_b = await adapter_b.connect(config_b)

    first = await _call_probe(tools_a, "env-one")
    second = await _call_probe(tools_b, "env-two")
    assert first["token"] == "one"
    assert second["token"] == "two"
    assert first["pid"] != second["pid"]
    assert _event_count(state_file, "start") == 2

    await adapter_a.disconnect_all()
    await adapter_b.disconnect_all()
    await cache.close_all()


@pytest.mark.integration
async def test_real_stdio_parent_and_sub_agent_share_connection_but_not_wrappers(tmp_path: Path) -> None:
    """A real parent/sub-agent build shares one MCP session while keeping wrappers isolated."""
    cache = MCPConnectionCache()
    state_file = tmp_path / "stdio-parent-sub-probe.jsonl"
    config = _stdio_probe_config(state_file)
    parent = AgentProfile(
        name="Parent",
        instructions="Parent instructions.",
        tools=ToolsConfig(builtins=[], mcp=[config]),
        sub_agents=SubAgentsConfig(agents=[SubAgentRef(profile="Explore", tool_name="Explore")]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )
    explore = AgentProfile(
        name="Explore",
        instructions="Explore instructions.",
        tools=ToolsConfig(builtins=[], mcp=[config]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )
    agent_registry = AgentProfileRegistry()
    agent_registry.register(parent)
    agent_registry.register(explore)
    model_registry = ModelProfileRegistry()
    model_registry.register(ModelProfile(id="mock-model", name="mock", provider="mock", model_id="mock"))

    class _FakeCompactionStrategy:
        def bind_reminder(self, _middleware: object, _last_words: object) -> None:
            return None

        def set_last_words_generator(self, _generator: object) -> None:
            return None

    class _FakeContextManager:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.providers: list[object] = []
            self.middleware: list[object] = []
            self.compaction_strategy = _FakeCompactionStrategy()
            self.context_mgmt_provider = None

    class _FakeAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.tools = kwargs["tools"]
            self.exited = False

        async def __aenter__(self) -> _FakeAgent:
            return self

        async def __aexit__(self, *_args: object) -> None:
            self.exited = True

        def create_session(self) -> MagicMock:
            return MagicMock()

    fake_executor = MagicMock()
    fake_executor.state.running = False
    fake_executor.backend.history_state = {}

    with (
        patch.object(runtime_factory_module, "Agent", _FakeAgent),
        patch.object(runtime_factory_module, "Agent", _FakeAgent),
        patch.object(runtime_factory_module, "ContextManager", _FakeContextManager),
        patch.object(runtime_factory_module, "ContextManager", _FakeContextManager),
        patch.object(agent_builder_module, "create_client", return_value=MagicMock(aclose=AsyncMock())),
        patch.object(sub_agent_module, "create_client", return_value=MagicMock(aclose=AsyncMock())),
        patch.object(agent_builder_module, "effective_chat_options", return_value={}),
        patch.object(agent_builder_module, "TurnBindings", return_value=fake_executor),
        patch("chrys.service.skills.adapter.create_skills_provider", new=AsyncMock(return_value=(None, []))),
    ):
        result = await build_agent(
            profile=parent,
            settings=Settings(model_profile="mock-model"),
            workspace=None,
            session_id="real-parent-sub",
            bus=EventBus(),
            agent_registry=agent_registry,
            injection=MagicMock(),
            batch_counter=ResponseBatchCounter(),
            on_intermediate=MagicMock(),
            on_usage=MagicMock(),
            model_registry=model_registry,
            approval_mode=ApprovalMode.BYPASS,
            session_dir=tmp_path / "session",
            mcp_cache=cache,
        )

    assert result.mcp_adapter is not None
    assert result.sub_agent_tools is not None
    sub_adapter = result.sub_agent_tools._mcp_adapters["Explore"]
    parent_lease = result.mcp_adapter._leases[config.name]
    sub_lease = sub_adapter._leases[config.name]
    assert parent_lease.mcp_tool is sub_lease.mcp_tool
    assert parent_lease.functions[0] is not sub_lease.functions[0]
    assert cache._entries[parent_lease.key].refcount == 2
    assert _event_count(state_file, "start") == 1

    parent_probe = await _call_probe(parent_lease.functions, "parent")
    sub_probe = await _call_probe(sub_lease.functions, "sub")
    assert sub_probe["pid"] == parent_probe["pid"]
    assert sub_probe["call_count"] == 2

    with patch.object(parent_lease.mcp_tool, "__aexit__", wraps=parent_lease.mcp_tool.__aexit__) as exit_tool:
        await result.conversation.aclose()
        assert cache._entries[parent_lease.key].refcount == 2
        await result.sub_agent_tools._prepared_by_tool["Explore"].aclose()
        assert cache._entries[parent_lease.key].refcount == 1
        await result.prepared.aclose()
        assert cache._entries[parent_lease.key].refcount == 0
        exit_tool.assert_not_awaited()
        await cache.close_all()
        await cache.close_all()
        exit_tool.assert_awaited_once()


@pytest.mark.integration
async def test_real_http_cache_shares_live_mcp_tool_and_releases_refs(tmp_path: Path) -> None:
    """HTTP MCP configs share one live MCPTool while wrappers stay per-adapter."""
    async with _http_probe_server(tmp_path) as (config, state_file):
        cache = MCPConnectionCache()
        adapter_a = MCPAdapter(cache=cache)
        adapter_b = MCPAdapter(cache=cache)

        tools_a = await adapter_a.connect(config)
        tools_b = await adapter_b.connect(config)

        lease_a = adapter_a._leases[config.name]
        lease_b = adapter_b._leases[config.name]
        assert lease_a.mcp_tool is lease_b.mcp_tool
        assert lease_a.functions[0] is not lease_b.functions[0]
        assert cache._entries[lease_a.key].refcount == 2

        first = await _call_probe(tools_a, "http-first")
        second = await _call_probe(tools_b, "http-second")
        assert second["pid"] == first["pid"]
        assert second["call_count"] == 2
        assert _event_count(state_file, "start") == 1

        await adapter_a.disconnect_all()
        assert cache._entries[lease_b.key].refcount == 1
        await adapter_b.disconnect_all()
        assert cache._entries[lease_b.key].refcount == 0

        adapter_c = MCPAdapter(cache=cache)
        tools_c = await adapter_c.connect(config)
        assert adapter_c._leases[config.name].mcp_tool is lease_a.mcp_tool
        warm = await _call_probe(tools_c, "http-warm")
        assert warm["call_count"] == 3

        await adapter_c.disconnect_all()
        await cache.close_all()


@pytest.mark.integration
async def test_real_build_wires_mcp_instructions_provider(tmp_path: Path) -> None:
    """build_agent wires the adapter's instruction renderer into the reminder middleware."""
    from tests.service.mcp.mcp_echo_server import ECHO_SERVER_INSTRUCTIONS

    cache = MCPConnectionCache()
    config = MCPServerConfig(
        name="echo-server",
        transport="stdio",
        command=sys.executable,
        args=[str(Path(__file__).with_name("mcp_echo_server.py"))],
    )
    profile = AgentProfile(
        name="Parent",
        instructions="Parent instructions.",
        tools=ToolsConfig(builtins=[], mcp=[config]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )
    agent_registry = AgentProfileRegistry()
    agent_registry.register(profile)
    model_registry = ModelProfileRegistry()
    model_registry.register(ModelProfile(id="mock-model", name="mock", provider="mock", model_id="mock"))

    class _FakeCompactionStrategy:
        def bind_reminder(self, _middleware: object, _last_words: object) -> None:
            return None

        def set_last_words_generator(self, _generator: object) -> None:
            return None

    class _FakeContextManager:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.providers: list[object] = []
            self.middleware: list[object] = []
            self.compaction_strategy = _FakeCompactionStrategy()
            self.context_mgmt_provider = None

    class _FakeAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.tools = kwargs["tools"]

        async def __aenter__(self) -> _FakeAgent:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def create_session(self) -> MagicMock:
            return MagicMock()

    fake_executor = MagicMock()
    fake_executor.state.running = False
    fake_executor.backend.history_state = {}

    with (
        patch.object(runtime_factory_module, "Agent", _FakeAgent),
        patch.object(runtime_factory_module, "ContextManager", _FakeContextManager),
        patch.object(agent_builder_module, "create_client", return_value=MagicMock(aclose=AsyncMock())),
        patch.object(agent_builder_module, "effective_chat_options", return_value={}),
        patch.object(agent_builder_module, "TurnBindings", return_value=fake_executor),
        patch("chrys.service.skills.adapter.create_skills_provider", new=AsyncMock(return_value=(None, []))),
    ):
        result = await build_agent(
            profile=profile,
            settings=Settings(model_profile="mock-model"),
            workspace=None,
            session_id="instructions-wiring",
            bus=EventBus(),
            agent_registry=agent_registry,
            injection=MagicMock(),
            batch_counter=ResponseBatchCounter(),
            on_intermediate=MagicMock(),
            on_usage=MagicMock(),
            model_registry=model_registry,
            approval_mode=ApprovalMode.BYPASS,
            session_dir=tmp_path / "session",
            mcp_cache=cache,
        )

    try:
        assert result.mcp_adapter is not None
        provider = result.reminder_middleware.sources.mcp._provider
        # The exact bound method, not a stale pre-rendered string.
        assert provider == result.mcp_adapter.render_instructions_reminder
        rendered = provider()
        assert rendered is not None
        assert '<server name="echo-server">' in rendered
        assert ECHO_SERVER_INSTRUCTIONS in rendered
    finally:
        await result.prepared.aclose()
        await cache.close_all()
