# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for MCPAdapter: config validation, the tool factory, connect/disconnect lifecycle, timeouts, and mixins."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chrys.foundation.events.types import Warning as WarningEvent
from chrys.foundation.tool_kinds import get_tool_kind
from chrys.foundation.util.env_templates import EnvVarResolutionError
from chrys.kernel import FunctionTool as ChrysFunctionTool
from chrys.service.mcp._connection import _create_mcp_tool, _validate_config
from chrys.service.mcp._http_transport import _HTTPMCPTool
from chrys.service.mcp._stdio_transport import _SafeStdioTool
from chrys.service.mcp.adapter import (
    DEFAULT_CONNECT_TIMEOUT_SECONDS,
    DEFAULT_TEST_TIMEOUT_SECONDS,
    MCPAdapter,
    MCPConnectionError,
    MCPToolNameCollisionError,
    MCPToolNameValidationError,
)
from chrys.service.mcp.cache import MCPConnectionLease
from chrys.service.mcp.owned import MCPStdioTool, MCPStreamableHTTPTool, MCPTool
from chrys.service.profiles.agents.schema import MCPServerConfig
from tests.service.mcp._helpers import (
    _FakeBannerConnectionTool,
    _FakeConnectionTool,
    _FakeDisconnectTool,
    _function_tool,
    _load_fake_remote_tools,
    _mcp_remote_tool,
)


async def _open_connection(adapter: MCPAdapter, connection_path: str, config: MCPServerConfig) -> Any:
    """Drive either adapter entry point: ``"test"`` (one-shot) or ``"agent"`` (registering)."""
    if connection_path == "test":
        return await adapter.test_connection(config)
    return await adapter.connect(config)


# ---------------------------------------------------------------------------
# _validate_config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [-1, 1, 99, True, False, "100", 1.5])
def test_adapter_rejects_invalid_max_tool_result_tokens(value: Any) -> None:
    config = MCPServerConfig(name="srv", transport="stdio", command="python")
    config.max_tool_result_tokens = value

    with pytest.raises(ValueError, match="max_tool_result_tokens"):
        _validate_config(config)


@pytest.mark.parametrize("value", [None, 0, 100, 1234])
def test_adapter_accepts_valid_max_tool_result_tokens(value: int | None) -> None:
    config = MCPServerConfig(
        name="srv",
        transport="stdio",
        command="python",
        max_tool_result_tokens=value,
    )

    _validate_config(config)


# ---------------------------------------------------------------------------
# MCPAdapter — construction and disconnecting unknown servers
# ---------------------------------------------------------------------------


def test_adapter_init() -> None:
    adapter = MCPAdapter()
    assert adapter.server_names == []
    assert adapter.failures == {}


async def test_adapter_disconnect_nonexistent() -> None:
    """Disconnecting a non-connected server should not raise."""
    adapter = MCPAdapter()
    await adapter.disconnect("nonexistent")
    assert adapter.server_names == []


# ---------------------------------------------------------------------------
# _create_mcp_tool
# ---------------------------------------------------------------------------


def test_create_stdio_tool() -> None:
    config = MCPServerConfig(name="s", transport="stdio", command="python", args=["-m", "srv"], env={"K": "V"})
    tool = _create_mcp_tool(config)
    assert isinstance(tool, _SafeStdioTool)
    assert isinstance(tool, MCPStdioTool)
    assert tool.name == "s"
    assert tool.command == "python"


def test_create_stdio_tool_resolves_env_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_MCP_SECRET", "resolved-secret")
    config = MCPServerConfig(
        name="s",
        transport="stdio",
        command="python",
        env={"TOKEN": "{{CHRYS_MCP_SECRET}}", "AUTH": "Bearer {{CHRYS_MCP_SECRET}}"},
    )

    tool = _create_mcp_tool(config)

    assert tool.env == {"TOKEN": "resolved-secret", "AUTH": "Bearer resolved-secret"}


def test_create_stdio_tool_forwards_allowed_tools() -> None:
    config = MCPServerConfig(name="s", transport="stdio", command="python", allowed_tools=["ping"], request_timeout=7)
    tool = _create_mcp_tool(config)
    assert tool.allowed_tools == ["ping"]
    assert tool.request_timeout == 7


def test_create_stdio_tool_forwards_empty_allowed_tools() -> None:
    config = MCPServerConfig(name="s", transport="stdio", command="python", allowed_tools=[])
    tool = _create_mcp_tool(config)
    tool._functions = [_function_tool("one"), _function_tool("two")]

    assert tool.allowed_tools == []
    assert tool.functions == []


def test_create_stdio_tool_forwards_description_prefix_and_load_prompts() -> None:
    config = MCPServerConfig(
        name="s",
        transport="stdio",
        command="python",
        description="Local server",
        tool_name_prefix="local",
        load_prompts=False,
    )
    tool = _create_mcp_tool(config)

    assert tool.description == "Local server"
    assert tool.tool_name_prefix == "local"
    assert tool.load_prompts_flag is False


@pytest.mark.parametrize(
    ("prefix", "progressive", "message"),
    [
        ("bad prefix", False, "tool name prefix"),
        ("github.v1", False, "underscores, and hyphens"),
        ("a" * 50, True, "invalid generated control.*maximum is 64"),
    ],
)
def test_create_mcp_tool_rejects_invalid_tool_name_prefix(
    prefix: str,
    progressive: bool,
    message: str,
) -> None:
    config = MCPServerConfig(
        name="s",
        transport="stdio",
        command="python",
        tool_name_prefix=prefix,
        use_progressive_disclosure=progressive,
    )

    with pytest.raises(MCPToolNameValidationError, match=message):
        _create_mcp_tool(config)


def test_create_mcp_tool_accepts_longest_progressive_control_prefix_boundary() -> None:
    prefix = "a" * 49
    config = MCPServerConfig(
        name="s",
        transport="stdio",
        command="python",
        tool_name_prefix=prefix,
        use_progressive_disclosure=True,
    )

    tool = _create_mcp_tool(config)

    assert tool.tool_name_prefix == prefix


def test_create_http_tool() -> None:
    config = MCPServerConfig(
        name="h",
        transport="http",
        url="http://localhost:8080/mcp",
        headers={"Authorization": "Bearer token"},
        terminate_on_close=True,
        allowed_tools=["echo"],
        request_timeout=15,
    )
    tool = _create_mcp_tool(config)
    assert isinstance(tool, MCPStreamableHTTPTool)
    assert isinstance(tool, _HTTPMCPTool)
    assert tool.name == "h"
    assert tool.url == "http://localhost:8080/mcp"
    assert tool.allowed_tools == ["echo"]
    assert tool.request_timeout == 15
    # Static headers use ``_HTTPMCPTool``'s same-origin request hook, so they
    # reach initialize / list_tools as well as tool calls.
    assert tool._static_headers == {"Authorization": "Bearer token"}


def test_create_http_tool_resolves_header_env_templates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_MCP_HTTP_TOKEN", "token-value")
    config = MCPServerConfig(
        name="h",
        transport="http",
        url="http://localhost:8080/mcp",
        headers={"Authorization": "Bearer {{CHRYS_MCP_HTTP_TOKEN}}"},
    )

    tool = _create_mcp_tool(config)

    assert tool._static_headers == {"Authorization": "Bearer token-value"}


def test_create_mcp_tool_rejects_missing_env_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHRYS_MCP_MISSING", raising=False)
    config = MCPServerConfig(
        name="s",
        transport="stdio",
        command="python",
        env={"TOKEN": "{{CHRYS_MCP_MISSING}}"},
    )

    with pytest.raises(EnvVarResolutionError) as info:
        _create_mcp_tool(config)

    message = str(info.value)
    assert "CHRYS_MCP_MISSING" in message
    assert "MCP server 's' env['TOKEN']" in message


def test_create_http_tool_without_headers_has_empty_static_headers() -> None:
    config = MCPServerConfig(name="h", transport="http", url="http://localhost:8080/mcp")
    tool = _create_mcp_tool(config)
    assert tool._static_headers == {}


def test_create_http_tool_ignores_env_overrides() -> None:
    config = MCPServerConfig(
        name="h",
        transport="http",
        url="http://localhost:8080/mcp",
        env={"NO_PROXY": "*", "SSL_CERT_FILE": "/tmp/ca.pem"},
    )
    tool = _create_mcp_tool(config)
    assert isinstance(tool, _HTTPMCPTool)
    assert tool._needs_prebuild() is False


def test_create_http_tool_carries_bypass_proxy() -> None:
    config = MCPServerConfig(
        name="h",
        transport="http",
        url="http://localhost:8080/mcp",
        bypass_proxy=True,
    )
    tool = _create_mcp_tool(config)
    assert isinstance(tool, _HTTPMCPTool)
    assert tool._bypass_proxy is True


@pytest.mark.parametrize(
    ("config_kwargs", "match"),
    [
        pytest.param({"name": "x", "transport": "grpc"}, "Unknown MCP transport", id="unknown-transport"),
        pytest.param({"name": "s", "transport": "stdio"}, "requires 'command'", id="stdio-without-command"),
        pytest.param({"name": "h", "transport": "http"}, "requires 'url'", id="http-without-url"),
        pytest.param({"name": "", "transport": "stdio", "command": "python"}, "non-empty 'name'", id="without-name"),
    ],
)
def test_create_mcp_tool_rejects_incomplete_config(config_kwargs: dict[str, Any], match: str) -> None:
    config = MCPServerConfig(**config_kwargs)
    with pytest.raises(ValueError, match=match):
        _create_mcp_tool(config)


# ---------------------------------------------------------------------------
# connect / connect_all failure surfacing
# ---------------------------------------------------------------------------


async def test_connect_success_stamps_kind_and_caches() -> None:
    adapter = MCPAdapter()
    config = MCPServerConfig(name="s", transport="stdio", command="python")
    fn = _function_tool()
    fake = _FakeConnectionTool(functions=[fn])

    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake):
        first = await adapter.connect(config)
        second = await adapter.connect(config)  # cache hit, no second aenter

    assert first == second
    assert first[0] is not fn
    assert first[0].name == "remote"
    assert get_tool_kind(first[0]) == "mcp"
    assert adapter.server_names == ["s"]
    fake.__aenter__.assert_awaited_once()


async def test_connect_reraises_cancelled_error() -> None:
    adapter = MCPAdapter()
    config = MCPServerConfig(name="s", transport="stdio", command="python")
    fake = _FakeConnectionTool(enter_error=asyncio.CancelledError())

    with (
        patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake),
        pytest.raises(asyncio.CancelledError),
    ):
        await adapter.connect(config)

    assert adapter.server_names == []
    assert adapter.failures == {}  # cancellation is not a "failure"
    fake.__aexit__.assert_awaited_once()


async def test_connect_all_skips_disabled_servers() -> None:
    adapter = MCPAdapter()
    enabled = MCPServerConfig(name="enabled", transport="stdio", command="python")
    disabled = MCPServerConfig(name="disabled", transport="stdio", command="python", enabled=False)

    with patch.object(adapter, "connect", new=AsyncMock(return_value=["tool-enabled"])) as connect_mock:
        tools = await adapter.connect_all([enabled, disabled])

    assert tools == ["tool-enabled"]
    connect_mock.assert_awaited_once_with(enabled)


@pytest.mark.parametrize(
    ("make_failure", "recorded_cause"),
    [
        pytest.param(
            lambda: MCPConnectionError("bad", "stdio", RuntimeError("spawn fail")),
            None,
            id="mcp-connection-error",
        ),
        pytest.param(lambda: ValueError("malformed config"), ValueError, id="unexpected-exception"),
    ],
)
async def test_connect_all_publishes_warning_on_failure_and_continues(
    make_failure: Any,
    recorded_cause: type[BaseException] | None,
) -> None:
    """A failing server must not abort the build: the good one still comes up and a warning is published.

    Non-``MCPConnectionError`` failures (e.g. ``ValueError`` from ``_validate_config``
    for a malformed config, or any unexpected MCP error) are wrapped and recorded
    too, so the engine does not fail startup and leave the user with "Engine not
    started".
    """
    bus = MagicMock()
    bus.publish = AsyncMock()
    adapter = MCPAdapter(bus=bus, session_id="sess-1")

    good = MCPServerConfig(name="good", transport="stdio", command="python")
    bad = MCPServerConfig(name="bad", transport="stdio", command="python")

    async def fake_connect(cfg: MCPServerConfig) -> list:
        if cfg.name == "bad":
            raise make_failure()
        return [f"tool-{cfg.name}"]

    with patch.object(adapter, "connect", new=AsyncMock(side_effect=fake_connect)):
        tools = await adapter.connect_all([bad, good])

    # Bad server does not stop the good one
    assert tools == ["tool-good"]
    assert adapter.tool_names_by_server == {"good": ["tool-good"]}
    if recorded_cause is not None:
        assert "bad" in adapter.failures
        assert isinstance(adapter.failures["bad"].cause, recorded_cause)
    # Warning event went to the bus with the expected shape
    bus.publish.assert_awaited_once()
    (event,), _ = bus.publish.await_args
    assert isinstance(event, WarningEvent)
    assert event.code == "mcp.connect_failed"
    assert "bad" in event.message
    assert event.session_id == "sess-1"


async def test_connect_all_publishes_warning_for_missing_env_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHRYS_MCP_MISSING", raising=False)
    bus = MagicMock()
    bus.publish = AsyncMock()
    adapter = MCPAdapter(bus=bus, session_id="sess-1")
    bad = MCPServerConfig(
        name="bad",
        transport="stdio",
        command="python",
        env={"TOKEN": "{{CHRYS_MCP_MISSING}}"},
    )

    tools = await adapter.connect_all([bad])

    assert tools == []
    bus.publish.assert_awaited_once()
    (event,), _ = bus.publish.await_args
    assert isinstance(event, WarningEvent)
    assert event.code == "mcp.connect_failed"
    assert "CHRYS_MCP_MISSING" in event.message
    assert "MCP server 'bad' env['TOKEN']" in event.message


async def test_connect_all_without_bus_still_continues() -> None:
    adapter = MCPAdapter()  # bus=None
    bad = MCPServerConfig(name="bad", transport="stdio", command="python")

    async def fake_connect(cfg: MCPServerConfig) -> list:
        raise MCPConnectionError(cfg.name, cfg.transport, RuntimeError("x"))

    with patch.object(adapter, "connect", new=AsyncMock(side_effect=fake_connect)):
        tools = await adapter.connect_all([bad])

    assert tools == []


@pytest.mark.parametrize(
    "make_failure",
    [
        pytest.param(lambda: MCPConnectionError("bad", "stdio", RuntimeError("boom")), id="mcp-connection-error"),
        pytest.param(lambda: ValueError("malformed"), id="unexpected-exception"),
    ],
)
async def test_connect_all_progress_tracks_connected_and_failed_counts(make_failure: Any) -> None:
    """Progress sees the failed count advance for either failure kind and the connected count for the good server."""
    adapter = MCPAdapter()
    bad = MCPServerConfig(name="bad", transport="stdio", command="python")
    good = MCPServerConfig(name="good", transport="stdio", command="python")
    progress_events: list[tuple[str, str, int, int, int]] = []

    async def fake_connect(config: MCPServerConfig) -> list[str]:
        if config.name == "bad":
            raise make_failure()
        return [f"tool-{config.name}"]

    async def progress(config: MCPServerConfig, state: str, current: int, total: int, failed: int) -> None:
        progress_events.append((config.name, state, current, total, failed))

    with patch.object(adapter, "connect", new=AsyncMock(side_effect=fake_connect)):
        tools = await adapter.connect_all([bad, good], progress=progress)

    assert tools == ["tool-good"]
    assert [event for event in progress_events if event[0] == "bad"] == [
        ("bad", "starting", 0, 2, 0),
        ("bad", "failed", 0, 2, 1),
    ]
    assert ("good", "connected", 1, 2, 1) in progress_events


async def test_connect_all_starts_enabled_servers_in_parallel() -> None:
    adapter = MCPAdapter()
    gate = asyncio.Event()
    entered = {"a": asyncio.Event(), "b": asyncio.Event()}
    calls: list[str] = []
    progress_events: list[tuple[str, str, int, int, int]] = []
    fn_a = _function_tool("a_remote")
    fn_b = _function_tool("b_remote")

    class _SlowTool:
        def __init__(self, name: str, fn: ChrysFunctionTool) -> None:
            self.name = name
            self.functions = [fn]
            self.request_timeout = DEFAULT_CONNECT_TIMEOUT_SECONDS
            self.dropped_banner_lines: list[str] = []

        async def __aenter__(self) -> _SlowTool:
            calls.append(f"{self.name}:enter")
            entered[self.name].set()
            await gate.wait()
            calls.append(f"{self.name}:ready")
            return self

        async def __aexit__(self, *args: object) -> None:
            calls.append(f"{self.name}:exit")

    tools = {"a": _SlowTool("a", fn_a), "b": _SlowTool("b", fn_b)}

    def fake_factory(cfg: MCPServerConfig) -> _SlowTool:
        return tools[cfg.name]

    config_a = MCPServerConfig(name="a", transport="stdio", command="python")
    config_b = MCPServerConfig(name="b", transport="stdio", command="python")

    async def progress(config: MCPServerConfig, state: str, current: int, total: int, failed: int) -> None:
        progress_events.append((config.name, state, current, total, failed))

    with patch("chrys.service.mcp._connection._create_mcp_tool", side_effect=fake_factory):
        task = asyncio.create_task(adapter.connect_all([config_a, config_b], progress=progress))
        await asyncio.wait_for(
            asyncio.gather(entered["a"].wait(), entered["b"].wait()),
            timeout=20.0,
        )
        assert sorted(calls) == ["a:enter", "b:enter"]
        gate.set()
        result = await task

    assert [tool.name for tool in result] == ["a_remote", "b_remote"]
    assert all(get_tool_kind(tool) == "mcp" for tool in result)
    assert all(tool.kind is None for tool in result)
    starting = [event for event in progress_events if event[1] == "starting"]
    connected = [event for event in progress_events if event[1] == "connected"]
    assert sorted((current, total, failed) for _, _, current, total, failed in starting) == [(0, 2, 0), (0, 2, 0)]
    assert sorted(current for _, _, current, _, _ in connected) == [1, 2]
    await adapter.disconnect_all()


async def test_disconnect_all_cancels_in_flight_connect_and_closes_partial_tool() -> None:
    adapter = MCPAdapter()
    entered = asyncio.Event()
    exited = asyncio.Event()

    class _HangingTool:
        def __init__(self) -> None:
            self.functions: list = []
            self.request_timeout = DEFAULT_CONNECT_TIMEOUT_SECONDS
            self.dropped_banner_lines: list[str] = []

        async def __aenter__(self) -> _HangingTool:
            entered.set()
            await asyncio.Event().wait()
            return self

        async def __aexit__(self, *args: object) -> None:
            exited.set()

    config = MCPServerConfig(name="slow", transport="stdio", command="python")

    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=_HangingTool()):
        task = asyncio.create_task(adapter.connect_all([config]))
        await asyncio.wait_for(entered.wait(), timeout=20.0)
        await asyncio.wait_for(adapter.disconnect_all(), timeout=5.0)

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2.0)
    assert exited.is_set()
    assert adapter.server_names == []


async def test_connect_all_propagates_cancelled_error() -> None:
    """``CancelledError`` is structural and must propagate out of ``connect_all``."""
    adapter = MCPAdapter()
    bad = MCPServerConfig(name="bad", transport="stdio", command="python")

    async def fake_connect(cfg: MCPServerConfig) -> list:
        raise asyncio.CancelledError

    with (
        patch.object(adapter, "connect", new=AsyncMock(side_effect=fake_connect)),
        pytest.raises(asyncio.CancelledError),
    ):
        await adapter.connect_all([bad])


async def test_connect_new_releases_duplicate_lease_if_server_already_registered() -> None:
    """If registration was won elsewhere, the duplicate cache lease must release.

    The duplicate branch reads ``existing_server.functions`` — engine-domain
    native instances — so MCPAdapter must deliver FunctionTool clones
    instead of returning shared originals to another acquisition.
    """
    adapter = MCPAdapter()
    config = MCPServerConfig(name="s", transport="stdio", command="python")
    existing_fn = _function_tool("existing")
    existing = _FakeConnectionTool(functions=[existing_fn])
    duplicate_exit = AsyncMock(return_value=None)

    class _DuplicateTool:
        def __init__(self) -> None:
            self.functions = [_function_tool("duplicate")]
            self.request_timeout = DEFAULT_CONNECT_TIMEOUT_SECONDS
            self.dropped_banner_lines: list[str] = []

        async def __aenter__(self) -> _DuplicateTool:
            adapter._servers["s"] = existing  # type: ignore[assignment]
            return self

        async def __aexit__(self, *args: object) -> None:
            await duplicate_exit(*args)

    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=_DuplicateTool()):
        result = await adapter._connect_new(config)

    assert len(result) == 1
    delivered = result[0]
    assert delivered is not existing_fn
    assert isinstance(delivered, ChrysFunctionTool)
    assert delivered.name == "existing"
    duplicate_exit.assert_awaited_once_with(None, None, None)
    assert adapter.server_names == ["s"]


async def test_registration_revalidation_failure_releases_unregistered_lease() -> None:
    """A future await before registration must not turn a collision into a leaked lease."""
    config = MCPServerConfig(name="s", transport="stdio", command="python")
    function = _function_tool("remote")
    fake = _FakeConnectionTool(functions=[function])
    lease = MCPConnectionLease(
        key="key",
        server_name="s",
        config=config,
        functions=[function],
        mcp_tool=fake,  # type: ignore[arg-type]
        entry=MagicMock(),
    )
    cache = MagicMock()
    cache.acquire = AsyncMock(return_value=lease)
    cache.release = AsyncMock()
    adapter = MCPAdapter(cache=cache)
    collision = MCPToolNameCollisionError(
        "s",
        "stdio",
        conflicting_names={"remote"},
        conflict_with="another tool",
        guidance="Rename it.",
    )

    with (
        patch.object(adapter, "_validate_server_namespace", side_effect=[None, collision]),
        pytest.raises(MCPToolNameCollisionError, match="remote"),
    ):
        await adapter.connect(config)

    cache.release.assert_awaited_once_with(lease)
    assert adapter.server_names == []


async def test_disconnect_all_finishes_private_cache_close_when_cancelled() -> None:
    """Private adapters should not leave cached tools running after cancellation."""
    adapter = MCPAdapter()
    config = MCPServerConfig(name="s", transport="stdio", command="python")
    close_started = asyncio.Event()
    close_continue = asyncio.Event()

    class _SlowExitTool:
        def __init__(self) -> None:
            self.functions = [_function_tool()]
            self.request_timeout = DEFAULT_CONNECT_TIMEOUT_SECONDS
            self.dropped_banner_lines: list[str] = []
            self.exit_count = 0

        async def __aenter__(self) -> _SlowExitTool:
            return self

        async def __aexit__(self, *args: object) -> None:
            close_started.set()
            await close_continue.wait()
            self.exit_count += 1

    fake = _SlowExitTool()

    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake):
        await adapter.connect(config)

    task = asyncio.create_task(adapter.disconnect_all())
    await asyncio.wait_for(close_started.wait(), timeout=20.0)
    task.cancel()
    close_continue.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert fake.exit_count == 1
    assert adapter.server_names == []
    assert adapter._cache.closed is False


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------


async def test_test_connection_enters_and_exits_tool() -> None:
    adapter = MCPAdapter()
    config = MCPServerConfig(name="s", transport="stdio", command="python")
    fake = _FakeConnectionTool()

    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake):
        await adapter.test_connection(config)

    fake.__aenter__.assert_awaited_once()
    fake.__aexit__.assert_awaited_once_with(None, None, None)


async def test_test_connection_returns_server_report() -> None:
    """A successful test snapshots identity, capabilities, and the catalog."""
    from mcp import types as mcp_types

    owned_tool = MCPTool(name="s")
    await _load_fake_remote_tools(owned_tool, _mcp_remote_tool("echo"), _mcp_remote_tool("greet"))
    # Mark "greet" as prompt-derived so the report splits it out of the tools list.
    owned_tool._loaded_prompt_remote_names = {"greet"}

    fake = _FakeConnectionTool(functions=owned_tool.functions)
    fake._server_info = mcp_types.Implementation(name="everything", title="Everything Server", version="1.0.0")
    fake._protocol_version = "2025-06-18"
    fake._server_capabilities = mcp_types.ServerCapabilities(
        tools=mcp_types.ToolsCapability(listChanged=True),
        prompts=mcp_types.PromptsCapability(),
    )
    fake._server_instructions = "Use the echo tool."
    fake._loaded_prompt_remote_names = owned_tool._loaded_prompt_remote_names

    adapter = MCPAdapter()
    config = MCPServerConfig(name="s", transport="stdio", command="python")
    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake):
        report = await adapter.test_connection(config)

    assert report.server_name == "everything"
    assert report.server_title == "Everything Server"
    assert report.server_version == "1.0.0"
    assert report.protocol_version == "2025-06-18"
    assert report.capabilities == ("prompts", "tools", "tools.listChanged")
    assert report.instructions == "Use the echo tool."
    assert report.tools == (("echo", "Remote tool"),)
    assert report.prompts == (("greet", "Remote tool"),)
    assert report.initial_tool_names is None


def test_flatten_server_capabilities_walks_nested_feature_trees() -> None:
    """Nested capability objects (tasks, extras) flatten to dotted presence rows."""
    from mcp import types as mcp_types

    from chrys.service.mcp.adapter import _flatten_server_capabilities

    caps = mcp_types.ServerCapabilities(
        tools=mcp_types.ToolsCapability(listChanged=True),
        tasks={"list": {}, "cancel": {}, "requests": {"tools": {}}},
    )

    assert _flatten_server_capabilities(caps) == (
        "tasks",
        "tasks.cancel",
        "tasks.list",
        "tasks.requests",
        "tasks.requests.tools",
        "tools",
        "tools.listChanged",
    )


def test_flatten_server_capabilities_cap_never_starves_standard_groups() -> None:
    """A bloated extras branch must not make a declared standard capability
    look unadvertised: the entry cap only drops the deepest/extra leaves."""
    from mcp import types as mcp_types

    from chrys.service.mcp.adapter import _MAX_CAPABILITY_ENTRIES, _flatten_server_capabilities

    caps = mcp_types.ServerCapabilities(
        experimental={f"e{i:03d}": {} for i in range(_MAX_CAPABILITY_ENTRIES + 50)},
        tools=mcp_types.ToolsCapability(listChanged=True),
    )

    flattened = _flatten_server_capabilities(caps)

    assert len(flattened) == _MAX_CAPABILITY_ENTRIES
    assert "tools" in flattened


async def test_test_connection_reports_progressive_initial_surface() -> None:
    """Progressive configs report the initial visible surface by name."""
    owned_tool = MCPTool(name="s")
    await _load_fake_remote_tools(owned_tool, _mcp_remote_tool("echo"), _mcp_remote_tool("search"))
    fake = _FakeConnectionTool(functions=owned_tool.functions)

    adapter = MCPAdapter()
    config = MCPServerConfig(
        name="s",
        transport="stdio",
        command="python",
        use_progressive_disclosure=True,
        always_load=["search"],
    )
    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake):
        report = await adapter.test_connection(config)

    assert report.initial_tool_names is not None
    assert "search" in report.initial_tool_names
    assert "echo" not in report.initial_tool_names
    # Server-scoped control tools are part of the initial surface.
    assert "mcp_s_list_mcp_tools" in report.initial_tool_names


async def test_test_connection_wraps_missing_env_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHRYS_MCP_TEST_MISSING", raising=False)
    adapter = MCPAdapter()
    config = MCPServerConfig(
        name="s",
        transport="stdio",
        command="python",
        env={"TOKEN": "{{CHRYS_MCP_TEST_MISSING}}"},
    )

    with pytest.raises(MCPConnectionError) as info:
        await adapter.test_connection(config)

    message = str(info.value)
    assert "CHRYS_MCP_TEST_MISSING" in message
    assert "MCP server 's' env['TOKEN']" in message
    assert isinstance(info.value.cause, EnvVarResolutionError)


async def test_test_connection_reraises_mcp_connection_error() -> None:
    """A pre-wrapped MCPConnectionError is preserved exactly."""
    adapter = MCPAdapter()
    config = MCPServerConfig(name="s", transport="stdio", command="python")
    original = MCPConnectionError("s", "stdio", RuntimeError("wrapped"))
    fake = _FakeConnectionTool(enter_error=original)

    with (
        patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake),
        pytest.raises(MCPConnectionError) as info,
    ):
        await adapter.test_connection(config)

    assert info.value is original
    fake.__aexit__.assert_awaited_once_with(None, None, None)


# ---------------------------------------------------------------------------
# disconnect_all — runs in parallel
# ---------------------------------------------------------------------------


async def test_disconnect_all_runs_in_parallel() -> None:
    adapter = MCPAdapter()
    gate = asyncio.Event()
    # Each __aexit__ signals its own "entered" barrier before awaiting the
    # shared gate.  Waiting on both barriers (rather than spinning the loop
    # with ``sleep(0)``) proves the two exits are running concurrently
    # without depending on scheduler step count.
    entered: dict[str, asyncio.Event] = {"a": asyncio.Event(), "b": asyncio.Event()}
    calls: list[str] = []

    class _SlowTool:
        def __init__(self, name: str) -> None:
            self.name = name

        async def __aexit__(self, *args: object) -> None:
            calls.append(f"{self.name}:enter")
            entered[self.name].set()
            await gate.wait()
            calls.append(f"{self.name}:exit")

    adapter._servers = {"a": _SlowTool("a"), "b": _SlowTool("b")}  # type: ignore[dict-item]

    task = asyncio.create_task(adapter.disconnect_all())
    # Both __aexit__ coroutines must be in flight before either is released.
    await asyncio.wait_for(
        asyncio.gather(entered["a"].wait(), entered["b"].wait()),
        timeout=20.0,
    )
    assert sorted(calls) == ["a:enter", "b:enter"]

    gate.set()
    await task
    assert sorted(calls) == ["a:enter", "a:exit", "b:enter", "b:exit"]
    assert adapter.server_names == []


async def test_disconnect_all_tolerates_individual_failures() -> None:
    adapter = MCPAdapter()
    good = _FakeDisconnectTool()
    adapter._servers = {"bad": _FakeDisconnectTool(exit_error=RuntimeError("bad")), "good": good}  # type: ignore[dict-item]

    await adapter.disconnect_all()
    assert good.exit_count == 1
    assert adapter.server_names == []


async def test_disconnect_cancels_in_flight_connect() -> None:
    adapter = MCPAdapter()
    task = asyncio.create_task(asyncio.Event().wait())
    adapter._connecting["slow"] = task

    await adapter.disconnect("slow")

    assert task.cancelled()
    assert adapter.server_names == []


async def test_disconnect_tolerates_server_exit_failure() -> None:
    adapter = MCPAdapter()
    adapter._servers["bad"] = _FakeDisconnectTool(exit_error=RuntimeError("exit failed"))  # type: ignore[assignment]

    await adapter.disconnect("bad")

    assert adapter.server_names == []


# ---------------------------------------------------------------------------
# MCPAdapter timeouts — connect / test_connection (request_timeout injection)
# ---------------------------------------------------------------------------
#
# These verify the adapter injects a default timeout floor into the SDK's
# own ``request_timeout`` (= ``ClientSession.read_timeout_seconds``) when
# the user didn't configure one.  The SDK's ``anyio.fail_after`` then fires
# from inside MCPTool._run_lifecycle_owner, which is the only safe
# place to cancel — see ``_create_tool_with_timeout_floor``'s docstring.
#
# We don't try to drive a real subprocess hang here (that lives in the
# integration tests).  These unit tests verify the *wiring*: the right
# timeout value reaches ``_create_mcp_tool``, and a synthetic
# timeout-style failure from ``__aenter__`` becomes a
# ``MCPConnectionError`` whose ``cause`` is a ``TimeoutError``.


def _capture_create_tool_calls() -> tuple[list[MCPServerConfig], Any]:
    """Spy on ``_create_mcp_tool`` while still returning a usable fake."""
    seen_configs: list[MCPServerConfig] = []

    def _record(cfg: MCPServerConfig) -> _FakeConnectionTool:
        seen_configs.append(cfg)
        return _FakeConnectionTool()

    return seen_configs, _record


@pytest.mark.parametrize(
    ("connection_path", "config_kwargs", "expected_timeout"),
    [
        pytest.param("agent", {}, DEFAULT_CONNECT_TIMEOUT_SECONDS, id="connect-default-floor"),
        pytest.param("test", {}, DEFAULT_TEST_TIMEOUT_SECONDS, id="test-connection-default-floor"),
        pytest.param("agent", {"request_timeout": 5}, 5, id="connect-preserves-user-timeout"),
        pytest.param("test", {"request_timeout": 5}, 5, id="test-connection-preserves-user-timeout"),
    ],
)
async def test_default_timeout_floor_is_injected_only_when_user_did_not_set_one(
    connection_path: str,
    config_kwargs: dict[str, Any],
    expected_timeout: float,
) -> None:
    """Each entry point patches the config so the SDK's request_timeout is its floor; a user-set value is kept.

    The floor cases omit ``request_timeout`` rather than passing ``None``: what
    they prove is that ``MCPServerConfig``'s own default reaches the floor
    logic, which an explicit ``None`` would mask.
    """
    adapter = MCPAdapter()
    config = MCPServerConfig(name="srv", transport="stdio", command="python", **config_kwargs)
    seen, factory = _capture_create_tool_calls()

    with patch("chrys.service.mcp._connection._create_mcp_tool", side_effect=factory):
        await _open_connection(adapter, connection_path, config)

    assert len(seen) == 1
    assert seen[0].request_timeout == expected_timeout


class _ErrData:
    code = 408
    message = "Timed out"


class _McpError(Exception):
    """Shape of ``McpError(ErrorData(code=408))``, the MCP SDK's read-timeout signal."""

    def __init__(self) -> None:
        self.error = _ErrData()
        super().__init__("Timed out")


@pytest.mark.parametrize("connection_path", ["test", "agent"])
@pytest.mark.parametrize(
    ("make_error", "cause_type", "cause_text"),
    [
        pytest.param(lambda: RuntimeError("spawn failed"), RuntimeError, "spawn failed", id="arbitrary-failure"),
        pytest.param(
            lambda: TimeoutError("read_timeout fired inside session.initialize()"),
            TimeoutError,
            "did not complete within 4s",
            id="sdk-timeout",
        ),
        pytest.param(_McpError, TimeoutError, "did not complete within 4s", id="mcp-error-408"),
    ],
)
async def test_enter_failure_is_wrapped_with_recognized_cause(
    connection_path: str,
    make_error: Any,
    cause_type: type[BaseException],
    cause_text: str,
) -> None:
    """Both entry points wrap a non-cancellation ``__aenter__`` failure as ``MCPConnectionError``.

    A SDK-style ``TimeoutError`` or ``McpError(ErrorData(code=408))`` becomes the
    user-facing ``cause=TimeoutError``; any other failure keeps the original
    exception as ``cause``.  Partial banners ride along, the failure is recorded
    (agent path only), and the partial transport is closed.
    """
    adapter = MCPAdapter()
    config = MCPServerConfig(name="hung", transport="stdio", command="python", request_timeout=4)
    enter_error = make_error()
    fake = _FakeBannerConnectionTool(enter_error=enter_error, request_timeout=4, banner_lines=["welcome"])

    with (
        patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake),
        pytest.raises(MCPConnectionError) as info,
    ):
        await _open_connection(adapter, connection_path, config)

    err = info.value
    assert err.server_name == "hung"
    assert err.transport == "stdio"
    assert err.__cause__ is enter_error
    assert isinstance(err.cause, cause_type)
    assert cause_text in str(err.cause)
    assert err.banner_lines == ["welcome"]
    assert adapter.server_names == []
    assert ("hung" in adapter.failures) is (connection_path == "agent")
    fake.__aenter__.assert_awaited_once()
    fake.__aexit__.assert_awaited_once_with(None, None, None)


# ---------------------------------------------------------------------------
# _NoPrePagePingMixin / _StructuredContentFallbackMixin
# ---------------------------------------------------------------------------


class TestNoPrePagePing:
    """Both subclasses must override ``_ensure_connected`` to a no-op.

    Locks the workaround in place: if a future MCP transport change
    renames the method or the mixin's MRO position changes, this test fails
    instead of silently re-introducing the POST → GET → DELETE reconnect
    storm against servers that don't implement the optional ``ping`` utility.
    """

    async def test_http_tool_skips_ping(self) -> None:
        tool = _HTTPMCPTool(name="h", url="http://localhost/mcp")
        tool.session = MagicMock()
        tool.session.send_ping = AsyncMock(side_effect=RuntimeError("ping should not be called"))

        await tool._ensure_connected()

        tool.session.send_ping.assert_not_called()

    async def test_stdio_tool_skips_ping(self) -> None:
        tool = _SafeStdioTool(name="s", command="python", args=["-m", "srv"])
        tool.session = MagicMock()
        tool.session.send_ping = AsyncMock(side_effect=RuntimeError("ping should not be called"))

        await tool._ensure_connected()

        tool.session.send_ping.assert_not_called()


class TestStructuredContentFallback:
    """``_StructuredContentFallbackMixin`` surfaces ``CallToolResult.structuredContent``
    when ``content`` carries no meaningful payload.

    The MCP spec lets servers deliver their result via the ``structuredContent``
    JSON object, optionally with an empty ``TextContent`` placeholder in
    ``content`` for clients that don't yet read structured output.  Upstream
    ``MCPTool._parse_tool_result_from_mcp`` only walks ``content`` and would
    drop the structured payload, so chrys's tool result would render empty.
    """

    @staticmethod
    def _text(text: str) -> Any:
        from mcp import types

        return types.TextContent(type="text", text=text)

    @staticmethod
    def _result(content: list[Any], structured: dict[str, Any] | None, *, is_error: bool = False) -> Any:
        from mcp import types

        return types.CallToolResult(content=content, structuredContent=structured, isError=is_error)

    def _parse(self, tool: Any, result: Any) -> list[Any]:
        return tool._parse_tool_result_from_mcp(result)

    def test_empty_content_with_structured_returns_json_dump(self) -> None:
        tool = _HTTPMCPTool(name="h", url="http://localhost/mcp")
        out = self._parse(tool, self._result([], {"items": [1, 2], "ok": True}))
        assert len(out) == 1
        assert out[0].text == '{"items": [1, 2], "ok": true}'

    def test_empty_text_block_with_structured_returns_json_dump(self) -> None:
        tool = _SafeStdioTool(name="s", command="python")
        out = self._parse(tool, self._result([self._text("")], {"k": "v"}))
        assert len(out) == 1
        assert out[0].text == '{"k": "v"}'

    def test_whitespace_text_block_with_structured_returns_json_dump(self) -> None:
        tool = _HTTPMCPTool(name="h", url="http://localhost/mcp")
        out = self._parse(tool, self._result([self._text("   \n  ")], {"k": "v"}))
        assert len(out) == 1
        assert out[0].text == '{"k": "v"}'

    def test_meaningful_text_with_structured_uses_framework_parser(self) -> None:
        """A populated text fallback wins — structuredContent is treated as a duplicate."""
        tool = _HTTPMCPTool(name="h", url="http://localhost/mcp")
        out = self._parse(tool, self._result([self._text("hello")], {"k": "v"}))
        assert len(out) == 1
        assert out[0].text == "hello"

    def test_empty_content_without_structured_uses_framework_parser(self) -> None:
        """No structured payload → framework's ``"null"`` placeholder is preserved."""
        tool = _HTTPMCPTool(name="h", url="http://localhost/mcp")
        out = self._parse(tool, self._result([], None))
        assert len(out) == 1
        assert out[0].text == "null"

    def test_non_text_content_with_structured_uses_framework_parser(self) -> None:
        """Image/audio/etc. items count as meaningful — don't override their rendering."""
        from mcp import types

        tool = _HTTPMCPTool(name="h", url="http://localhost/mcp")
        image = types.ImageContent(type="image", data="aGVsbG8=", mimeType="image/png")
        out = self._parse(tool, self._result([image], {"k": "v"}))
        assert len(out) == 1
        assert getattr(out[0], "media_type", None) == "image/png"

    def test_cyclic_structured_falls_back_to_str(self) -> None:
        """Cyclic ``structuredContent`` defeats ``json.dumps`` — the ``str()`` fallback must catch it."""
        cyclic: dict[str, Any] = {"k": "v"}
        cyclic["self"] = cyclic

        tool = _HTTPMCPTool(name="h", url="http://localhost/mcp")
        out = self._parse(tool, self._result([], cyclic))
        assert len(out) == 1
        assert isinstance(out[0].text, str)
        assert out[0].text  # non-empty
