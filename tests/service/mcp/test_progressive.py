# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Progressive MCP disclosure and resume safety tests."""

from __future__ import annotations

import asyncio
import inspect
import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import Warning
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.tool_call_context import get_tool_context, set_tool_context
from chrys.foundation.tool_kinds import KIND_MCP, get_tool_kind, set_tool_kind
from chrys.kernel import ChatResponse, ChatResponseUpdate, Content, FunctionTool, Message, ResponseStream
from chrys.kernel.exceptions import ModelVisibleToolError, ToolExecutionException
from chrys.kernel.middleware import ChatMiddlewareLayer, FunctionInvocationContext
from chrys.kernel.sessions import SessionContext
from chrys.service.approval.policy import ApprovalPolicy
from chrys.service.mcp._connection import _create_mcp_tool
from chrys.service.mcp._progressive import (
    ProgressiveMCPResumeProvider,
    _ProgressiveMCPExposure,
)
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.mcp.cache import MCPConnectionCache, clone_mcp_function_tool, mcp_config_cache_key
from chrys.service.mcp.errors import (
    MCPToolNameAmbiguityError,
    MCPToolNameCollisionError,
    MCPToolNameValidationError,
)
from chrys.service.mcp.owned import MCPTool
from chrys.service.profiles.agents.schema import ApprovalConfig, MCPServerConfig
from tests.kernel._fakes import _assert_actionable_argument_error, _ProbeFunction, _result_contents
from tests.service.mcp._helpers import _FakeCatalogTool
from tests.support.transcript_invariants import InvariantCheckedToolLoopLayer


async def _remote(ctx: FunctionInvocationContext, value: str = "") -> str:
    return value or "ok"


def _function(name: str, *, remote_name: str | None = None) -> FunctionTool:
    remote = remote_name or name
    tool = FunctionTool(
        func=_remote,
        name=name,
        description=f"Call {remote}",
        input_model={"type": "object", "properties": {"value": {"type": "string"}}},
        additional_properties={
            "_mcp_remote_name": remote,
            "_mcp_normalized_name": remote.replace("/", "-"),
        },
    )
    set_tool_context(tool, {"server_name": "srv", "remote_name": remote})
    return tool


def _config(**kwargs: object) -> MCPServerConfig:
    return MCPServerConfig(
        name="srv",
        transport="stdio",
        command="python",
        use_progressive_disclosure=True,
        **kwargs,
    )


async def _connected(
    *functions: FunctionTool,
    config: MCPServerConfig | None = None,
    cache: MCPConnectionCache | None = None,
    reserved_tool_names: set[str] | None = None,
) -> tuple[MCPAdapter, _FakeCatalogTool, list[FunctionTool]]:
    adapter = MCPAdapter(cache=cache, reserved_tool_names=reserved_tool_names or ())
    effective_config = config or _config()
    fake = _FakeCatalogTool(list(functions), allowed_tools=effective_config.allowed_tools)
    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake):
        tools = await adapter.connect(effective_config)
    return adapter, fake, tools


def test_progressive_flags_do_not_fragment_connection_cache_key() -> None:
    ordinary = MCPServerConfig(name="srv", transport="stdio", command="python")
    progressive = _config(always_load=["remote"])

    assert mcp_config_cache_key(ordinary) == mcp_config_cache_key(progressive)


def test_programmatic_progressive_config_rejects_invalid_runtime_types() -> None:
    invalid_bool = _config()
    invalid_bool.use_progressive_disclosure = "yes"  # type: ignore[assignment]
    with pytest.raises(ValueError, match=r"use_progressive_disclosure.*boolean"):
        _create_mcp_tool(invalid_bool)

    invalid_always = _config()
    invalid_always.always_load = "remote"  # type: ignore[assignment]
    with pytest.raises(ValueError, match=r"always_load.*list of strings"):
        _create_mcp_tool(invalid_always)


async def test_always_load_name_selecting_two_tools_fails_connection() -> None:
    # Remote ``search`` is called ``gh_search``, the original name of the other tool.
    catalog = (_function("gh_gh_search", remote_name="gh_search"), _function("gh_search", remote_name="search"))

    with pytest.raises(MCPToolNameAmbiguityError, match=r"'gh_search' matches the server's 'gh_search' \(write"):
        await _connected(*catalog, config=_config(tool_name_prefix="gh", always_load=["gh_search"]))

    adapter, _fake, tools = await _connected(*catalog, config=_config(tool_name_prefix="gh", always_load=["search"]))
    assert "gh_search" in {tool.name for tool in tools}
    assert "gh_gh_search" not in {tool.name for tool in tools}
    await adapter.disconnect_all()


async def test_ambiguity_error_says_when_no_name_selects_a_tool_alone() -> None:
    # Under prefix ``gh``, ``gh_a`` names ``a`` (prefixed) and ``gh_a`` (original);
    # ``gh_gh_a``, the other name of ``gh_a``, is the original name of a third tool.
    catalog = (
        _function("gh_a", remote_name="a"),
        _function("gh_gh_a", remote_name="gh_a"),
        _function("gh_gh_gh_a", remote_name="gh_gh_a"),
    )

    with pytest.raises(MCPToolNameAmbiguityError) as exc_info:
        await _connected(*catalog, config=_config(tool_name_prefix="gh", always_load=["gh_a"]))

    message = str(exc_info.value)
    assert (
        "'gh_a' matches the server's 'a' (write 'a' to select only it), the server's 'gh_a' (no name selects only it)."
    ) in message
    assert "Configure a different Tool Name Prefix" in message


async def test_connect_all_fails_closed_for_programmatic_invalid_prefix() -> None:
    adapter = MCPAdapter()
    config = _config()
    config.tool_name_prefix = "github.v1"

    with pytest.raises(MCPToolNameValidationError, match="underscores, and hyphens"):
        await adapter.connect_all([config])


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (
            MCPServerConfig(
                name="srv",
                transport="stdio",
                command="python",
                always_load=["search"],
            ),
            "always_load requires use_progressive_disclosure=true",
        ),
        (_config(allowed_tools=[]), "progressive disclosure requires at least one permitted tool"),
        (
            _config(allowed_tools=["search"], always_load=["write"]),
            "always_load tools must also appear in allowed_tools: write",
        ),
    ],
)
def test_programmatic_config_rejects_incoherent_tool_loading_policy(
    config: MCPServerConfig,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _create_mcp_tool(config)


async def test_progressive_catalog_preserves_allowed_tools_none_empty_and_restricted() -> None:
    remote = _function("remote")
    other = _function("other")

    all_tool = MCPTool(name="srv", allowed_tools=None)
    all_tool._functions = [remote, other]
    empty_tool = MCPTool(name="srv", allowed_tools=[])
    empty_tool._functions = [remote, other]
    restricted_tool = MCPTool(name="srv", allowed_tools=["remote"])
    restricted_tool._functions = [remote, other]

    all_exposure = _ProgressiveMCPExposure(_config(), all_tool)
    all_list = all_exposure.initial_tools[0]
    assert [
        item["name"]
        for item in await all_list.func(FunctionInvocationContext(function=all_list, arguments={}, tools=[all_list]))
    ] == ["remote", "other"]
    assert empty_tool.functions == []
    assert [tool.name for tool in restricted_tool.functions] == ["remote"]
    empty_exposure = _ProgressiveMCPExposure(_config(), empty_tool)
    list_tool = empty_exposure.initial_tools[0]
    assert await list_tool.func(FunctionInvocationContext(function=list_tool, arguments={}, tools=[list_tool])) == []


async def test_initial_surface_is_server_namespaced_controls_plus_always_load() -> None:
    remote = _function("remote")
    hidden = _function("hidden")
    adapter, _fake, tools = await _connected(remote, hidden, config=_config(always_load=["remote"]))

    assert [tool.name for tool in tools] == [
        "mcp_srv_list_mcp_tools",
        "mcp_srv_load_tool",
        "mcp_srv_unload_tool",
        "remote",
    ]
    assert all(get_tool_kind(tool) == KIND_MCP for tool in tools)
    assert get_tool_context(tools[0]) == {
        "server_name": "srv",
        "progressive_control": "mcp_srv_list_mcp_tools",
    }

    await adapter.disconnect_all()


async def test_always_load_uses_safe_raw_alias_matching() -> None:
    lossy = _function("delete-everything", remote_name="delete/everything")
    adapter, _fake, tools = await _connected(lossy, config=_config(always_load=["delete-everything"]))

    assert [tool.name for tool in tools] == [
        "mcp_srv_list_mcp_tools",
        "mcp_srv_load_tool",
        "mcp_srv_unload_tool",
    ]

    await adapter.disconnect_all()


async def test_list_is_concise_and_observes_list_changed_catalog() -> None:
    remote = _function("remote")
    adapter, fake, tools = await _connected(remote)
    list_tool = tools[0]
    live = list(tools)
    ctx = FunctionInvocationContext(function=list_tool, arguments={}, tools=live)

    first = await list_tool.func(ctx)
    fake.functions.append(_function("new_tool"))
    second = await list_tool.func(ctx)

    assert first == [
        {
            "name": "remote",
            "remote_name": "remote",
            "description": "Call remote",
            "loaded": False,
            "always_loaded": False,
        }
    ]
    assert [item["name"] for item in second] == ["remote", "new_tool"]
    assert all("parameters" not in item for item in second)

    await adapter.disconnect_all()


async def test_list_changed_always_load_refreshes_connect_and_future_run_surface() -> None:
    config = _config(always_load=["late"])
    adapter, fake, initial = await _connected(_function("remote"), config=config)
    provider = adapter.create_resume_provider()
    assert provider is not None
    assert "late" not in {tool.name for tool in initial}

    fake.functions.append(_function("late"))

    refreshed = await adapter.connect(config)
    assert "late" in {tool.name for tool in refreshed}

    context = SessionContext(input_messages=[])
    await provider.before_run(
        agent=SimpleNamespace(default_options={"tools": initial}),
        session=object(),
        context=context,
        state={},
    )
    assert [tool.name for tool in context.tools] == ["late"]

    already_refreshed = SessionContext(input_messages=[])
    await provider.before_run(
        agent=SimpleNamespace(default_options={"tools": refreshed}),
        session=object(),
        context=already_refreshed,
        state={},
    )
    assert already_refreshed.tools == []

    await adapter.disconnect_all()


@pytest.mark.parametrize(
    ("collision_name", "message"),
    [
        ("read_file", r"reserved iCode tool.*read_file"),
        ("mcp_srv_load_tool", r"progressive-disclosure control.*mcp_srv_load_tool"),
    ],
)
async def test_list_changed_collision_fails_before_updated_catalog_is_used(
    collision_name: str,
    message: str,
) -> None:
    adapter, fake, initial = await _connected(
        _function("remote"),
        reserved_tool_names={"read_file"},
    )
    list_tool = initial[0]
    fake.functions.append(_function(collision_name))

    with pytest.raises(MCPToolNameCollisionError, match=message):
        await list_tool.func(FunctionInvocationContext(function=list_tool, arguments={}, tools=initial))

    await adapter.disconnect_all()


async def test_missing_always_load_name_publishes_warning() -> None:
    warnings: list[Warning] = []

    async def collect(event: Warning) -> None:
        warnings.append(event)

    bus = EventBus()
    await bus.subscribe(Warning, collect)
    adapter = MCPAdapter(bus, session_id="session")
    fake = _FakeCatalogTool([_function("remote")])
    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake):
        await adapter.connect(_config(always_load=["typo"]))

    assert [(warning.code, warning.session_id) for warning in warnings] == [("mcp.always_load_missing", "session")]
    assert "typo" in warnings[0].message

    await adapter.disconnect_all()


@pytest.mark.parametrize("progressive", [False, True])
async def test_connection_test_rejects_permitted_names_reserved_by_chrys(progressive: bool) -> None:
    adapter = MCPAdapter()
    adapter.set_reserved_tool_names({"read_file"})
    fake = _FakeCatalogTool([_function("read_file")])
    config = MCPServerConfig(
        name="srv",
        transport="stdio",
        command="python",
        use_progressive_disclosure=progressive,
    )

    with (
        patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake),
        pytest.raises(MCPToolNameCollisionError, match=r"reserved iCode tool.*read_file"),
    ):
        await adapter.test_connection(config)


async def test_reserved_tool_names_cannot_change_while_adapter_is_connected() -> None:
    adapter, _fake, _tools = await _connected(_function("remote"))

    with pytest.raises(RuntimeError, match="while connections are active"):
        adapter.set_reserved_tool_names({"read_file"})

    await adapter.disconnect_all()


async def test_connection_test_ignores_reserved_remote_name_excluded_from_permitted_set() -> None:
    adapter = MCPAdapter(reserved_tool_names={"read_file"})
    fake = _FakeCatalogTool([_function("read_file"), _function("safe")], allowed_tools=["safe"])
    config = MCPServerConfig(
        name="srv",
        transport="stdio",
        command="python",
        allowed_tools=["safe"],
    )

    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake):
        await adapter.test_connection(config)


@pytest.mark.parametrize("progressive", [False, True])
async def test_agent_connect_ignores_reserved_remote_name_excluded_from_permitted_set(progressive: bool) -> None:
    adapter = MCPAdapter(reserved_tool_names={"read_file"})
    fake = _FakeCatalogTool([_function("read_file"), _function("safe")], allowed_tools=["safe"])
    config = MCPServerConfig(
        name="srv",
        transport="stdio",
        command="python",
        allowed_tools=["safe"],
        use_progressive_disclosure=progressive,
    )

    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake):
        tools = await adapter.connect_all([config])

    assert "read_file" not in {tool.name for tool in tools}
    assert adapter.tool_names_by_server == {"srv": ["safe"]}
    await adapter.disconnect_all()


async def test_tool_namespace_holds_every_name_the_connected_servers_take() -> None:
    """Extensions load after MCP and their tools yield all of these: deferred tools and loading controls included."""
    adapter = MCPAdapter(reserved_tool_names=set())
    fake = _FakeCatalogTool([_function("lookup"), _function("deferred")])
    config = _config(always_load=["lookup"])

    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake):
        visible = {tool.name for tool in await adapter.connect_all([config])}

    assert "deferred" not in visible
    assert visible | {"lookup", "deferred"} <= adapter.tool_namespace
    await adapter.disconnect_all()
    assert adapter.tool_namespace == frozenset()


async def test_connect_all_does_not_downgrade_chrys_name_collision_to_warning() -> None:
    warnings: list[Warning] = []

    async def collect(event: Warning) -> None:
        warnings.append(event)

    bus = EventBus()
    await bus.subscribe(Warning, collect)
    adapter = MCPAdapter(bus, reserved_tool_names={"read_file"})
    fake = _FakeCatalogTool([_function("read_file")])
    config = MCPServerConfig(name="srv", transport="stdio", command="python")

    with (
        patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake),
        pytest.raises(MCPToolNameCollisionError, match=r"reserved iCode tool.*read_file"),
    ):
        await adapter.connect_all([config])

    assert warnings == []
    await adapter.disconnect_all()


async def test_load_and_unload_mutate_only_the_current_live_list() -> None:
    remote = _function("remote")
    adapter, _fake, initial = await _connected(remote)
    load_tool = initial[1]
    unload_tool = initial[2]
    live = list(initial)

    load_ctx = FunctionInvocationContext(function=load_tool, arguments={"tool": "remote"}, tools=live)
    load_result = await load_tool.func(load_ctx, tool="remote")

    loaded = next(tool for tool in live if tool.name == "remote")
    assert loaded is not remote
    assert get_tool_kind(loaded) == KIND_MCP
    assert get_tool_context(loaded) == {"server_name": "srv", "remote_name": "remote"}
    assert "next model iteration" in load_result

    unload_ctx = FunctionInvocationContext(function=unload_tool, arguments={"tool": "remote"}, tools=live)
    unload_result = await unload_tool.func(unload_ctx, tool="remote")

    assert "remote" not in {tool.name for tool in live}
    assert "next model iteration" in unload_result
    # Adapter/base state is unchanged: a new run starts from controls + always_load.
    assert [tool.name for tool in await adapter.connect(_config())] == [
        "mcp_srv_list_mcp_tools",
        "mcp_srv_load_tool",
        "mcp_srv_unload_tool",
    ]

    await adapter.disconnect_all()


async def test_foreign_same_name_is_not_loaded_or_removed_by_progressive_controls() -> None:
    remote = _function("remote")
    adapter, _fake, initial = await _connected(remote)
    list_tool, load_tool, unload_tool = initial
    owned = clone_mcp_function_tool(remote)
    foreign = FunctionTool(func=lambda: "foreign", name="remote", description="Foreign tool")
    live = [*initial, owned, foreign]

    listed = await list_tool.func(FunctionInvocationContext(function=list_tool, arguments={}, tools=live))
    assert listed[0]["loaded"] is True

    unload_result = await unload_tool.func(
        FunctionInvocationContext(function=unload_tool, arguments={}, tools=live),
        tool="remote",
    )
    assert "next model iteration" in unload_result
    assert owned not in live
    assert foreign in live

    # The model reads which name clashed, not a bare "Function failed".
    with pytest.raises(ModelVisibleToolError) as info:
        await load_tool.func(
            FunctionInvocationContext(function=load_tool, arguments={}, tools=live),
            tool="remote",
        )
    assert info.value.model_message == (
        "Cannot load 'remote': another tool already has that name, so none of the requested MCP tools were loaded."
    )
    assert [tool for tool in live if tool.name == "remote"] == [foreign]

    await adapter.disconnect_all()


async def test_load_clash_with_a_function_tool_mapping_names_the_tool() -> None:
    remote = _function("remote")
    adapter, _fake, initial = await _connected(remote)
    _list_tool, load_tool, _unload_tool = initial
    # add_tools also rejects a name a provider-style function-tool mapping declares.
    declared = {"type": "function", "function": {"name": "remote", "parameters": {"type": "object"}}}
    live = [*initial, declared]

    with pytest.raises(ModelVisibleToolError) as info:
        await load_tool.func(
            FunctionInvocationContext(function=load_tool, arguments={}, tools=live),
            tool="remote",
        )

    assert info.value.model_message == (
        "Cannot load 'remote': another tool already has that name, so none of the requested MCP tools were loaded."
    )
    assert live == [*initial, declared]

    await adapter.disconnect_all()


@pytest.mark.parametrize("owned_first", [False, True])
async def test_run_surface_collision_detection_is_order_independent(owned_first: bool) -> None:
    remote = _function("remote")
    adapter, _fake, initial = await _connected(remote, config=_config(always_load=["remote"]))
    provider = adapter.create_resume_provider()
    assert provider is not None
    owned = next(tool for tool in initial if tool.name == "remote")
    foreign = FunctionTool(func=lambda: "foreign", name="remote", description="Foreign tool")
    others = [tool for tool in initial if tool.name != "remote"]
    same_name_tools = [owned, foreign] if owned_first else [foreign, owned]

    with pytest.raises(MCPToolNameCollisionError, match=r"another visible tool.*remote"):
        await provider.before_run(
            agent=SimpleNamespace(default_options={"tools": [*others, *same_name_tools]}),
            session=object(),
            context=SessionContext(input_messages=[]),
            state={},
        )

    await adapter.disconnect_all()


async def test_run_surface_validates_generated_controls_against_foreign_tools() -> None:
    adapter, _fake, initial = await _connected(_function("remote"))
    provider = adapter.create_resume_provider()
    assert provider is not None
    control = initial[0]
    foreign = FunctionTool(func=lambda: "foreign", name=control.name, description="Foreign tool")

    with pytest.raises(MCPToolNameCollisionError, match=rf"another visible tool.*{control.name}"):
        await provider.before_run(
            agent=SimpleNamespace(default_options={"tools": [foreign, *initial]}),
            session=object(),
            context=SessionContext(input_messages=[]),
            state={},
        )

    await adapter.disconnect_all()


async def test_parallel_progressive_controls_serialize_live_list_mutations() -> None:
    adapter, _fake, initial = await _connected(_function("one"), _function("two"))
    load_tool = initial[1]
    unload_tool = initial[2]
    live = list(initial)

    assert inspect.iscoroutinefunction(load_tool.func)
    assert inspect.iscoroutinefunction(unload_tool.func)

    await asyncio.gather(
        load_tool.func(FunctionInvocationContext(function=load_tool, arguments={}, tools=live), tool="one"),
        load_tool.func(FunctionInvocationContext(function=load_tool, arguments={}, tools=live), tool="two"),
    )
    assert {tool.name for tool in live} >= {"one", "two"}

    await asyncio.gather(
        unload_tool.func(FunctionInvocationContext(function=unload_tool, arguments={}, tools=live), tool="one"),
        unload_tool.func(FunctionInvocationContext(function=unload_tool, arguments={}, tools=live), tool="two"),
    )
    assert not {"one", "two"} & {tool.name for tool in live}

    await adapter.disconnect_all()


async def test_batch_mixed_load_unload_and_missing_live_list() -> None:
    one = _function("one")
    two = _function("two")
    adapter, _fake, initial = await _connected(one, two)
    load_tool = initial[1]
    unload_tool = initial[2]
    live = list(initial)

    load_result = await load_tool.func(
        FunctionInvocationContext(function=load_tool, arguments={}, tools=live),
        tool=["one", "missing", "two", "one"],
    )

    assert {tool.name for tool in live} >= {"one", "two"}
    assert "not available" in load_result
    assert "already queued" in load_result

    unload_result = await unload_tool.func(
        FunctionInvocationContext(function=unload_tool, arguments={}, tools=live),
        tool=["one", "missing", "two"],
    )
    assert "one" not in {tool.name for tool in live}
    assert "two" not in {tool.name for tool in live}
    assert "not available" in unload_result

    with pytest.raises(ToolExecutionException, match="active agent tool loop"):
        await load_tool.func(FunctionInvocationContext(function=load_tool, arguments={}, tools=None), tool="one")

    await adapter.disconnect_all()


async def test_control_name_collision_fails_connection_instead_of_hiding_remote_tool() -> None:
    collision = _function("mcp_srv_load_tool")
    adapter = MCPAdapter()
    fake = _FakeCatalogTool([collision])
    config = _config(always_load=["mcp_srv_load_tool"])

    with (
        patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake),
        pytest.raises(MCPToolNameCollisionError, match=r"progressive-disclosure control.*mcp_srv_load_tool"),
    ):
        await adapter.connect(config)

    await adapter.disconnect_all()


async def test_multiple_unprefixed_servers_receive_distinct_control_names() -> None:
    adapter = MCPAdapter()

    def factory(config: MCPServerConfig) -> _FakeCatalogTool:
        return _FakeCatalogTool([_function(f"{config.name}_remote"), _function(f"{config.name}_hidden")])

    configs = [
        MCPServerConfig(
            name="one",
            transport="stdio",
            command="python",
            use_progressive_disclosure=True,
            always_load=["one_remote"],
        ),
        MCPServerConfig(name="two", transport="stdio", command="python", use_progressive_disclosure=True),
    ]
    with patch("chrys.service.mcp._connection._create_mcp_tool", side_effect=factory):
        tools = await adapter.connect_all(configs)

    names = {tool.name for tool in tools}
    assert {"mcp_one_list_mcp_tools", "mcp_one_load_tool", "mcp_one_unload_tool"} <= names
    assert {"mcp_two_list_mcp_tools", "mcp_two_load_tool", "mcp_two_unload_tool"} <= names
    assert adapter.tool_names_by_server == {
        "one": ["one_remote", "one_hidden"],
        "two": ["two_remote", "two_hidden"],
    }
    assert all(not name.startswith("mcp_") for catalog in adapter.tool_names_by_server.values() for name in catalog)

    await adapter.disconnect_all()


def test_lossy_server_names_cannot_collide_control_namespaces() -> None:
    slash = _config()
    slash.name = "a/b"
    dash = _config()
    dash.name = "a-b"

    slash_names = {tool.name for tool in _ProgressiveMCPExposure(slash, _FakeCatalogTool([])).initial_tools}
    dash_names = {tool.name for tool in _ProgressiveMCPExposure(dash, _FakeCatalogTool([])).initial_tools}

    assert slash_names.isdisjoint(dash_names)


def test_long_server_names_generate_bounded_provider_safe_control_names() -> None:
    first = _config()
    first.name = "a" * 50 + "-one"
    second = _config()
    second.name = "a" * 50 + "-two"

    first_names = {tool.name for tool in _ProgressiveMCPExposure(first, _FakeCatalogTool([])).initial_tools}
    second_names = {tool.name for tool in _ProgressiveMCPExposure(second, _FakeCatalogTool([])).initial_tools}

    assert all(len(name) <= 64 for name in first_names | second_names)
    assert max(map(len, first_names)) == 64
    assert all(re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in first_names | second_names)
    assert first_names.isdisjoint(second_names)


@pytest.mark.parametrize("progressive", [False, True])
@pytest.mark.parametrize("connection_path", ["test", "agent", "connect_all"])
async def test_provider_invalid_catalog_name_fails_before_model_request(
    progressive: bool,
    connection_path: str,
) -> None:
    adapter = MCPAdapter()
    invalid_name = "x" * 65
    fake = _FakeCatalogTool([_function(invalid_name)])
    config = MCPServerConfig(
        name="srv",
        transport="stdio",
        command="python",
        use_progressive_disclosure=progressive,
    )

    with (
        patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake),
        pytest.raises(MCPToolNameValidationError, match=r"maximum is 64.*Remote catalog name"),
    ):
        if connection_path == "test":
            await adapter.test_connection(config)
        elif connection_path == "agent":
            await adapter.connect(config)
        else:
            await adapter.connect_all([config])

    await adapter.disconnect_all()


async def test_shared_connection_does_not_share_progressive_live_state() -> None:
    cache = MCPConnectionCache()
    remote = _function("remote")
    fake = _FakeCatalogTool([remote])
    adapter_a = MCPAdapter(cache=cache)
    adapter_b = MCPAdapter(cache=cache)
    config = _config()
    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake) as factory:
        tools_a = await adapter_a.connect(config)
        tools_b = await adapter_b.connect(config)

    live_a = list(tools_a)
    await tools_a[1].func(
        FunctionInvocationContext(function=tools_a[1], arguments={}, tools=live_a),
        tool="remote",
    )

    assert factory.call_count == 1
    assert "remote" in {tool.name for tool in live_a}
    assert "remote" not in {tool.name for tool in tools_b}

    await adapter_a.disconnect_all()
    await adapter_b.disconnect_all()
    await cache.close_all()


async def test_resume_provider_rehydrates_only_unresolved_actionable_hidden_calls() -> None:
    remote = _function("remote")
    paired = _function("paired")
    informational = _function("informational")
    adapter, _fake, _tools = await _connected(remote, paired, informational)
    provider = adapter.create_resume_provider()
    assert provider is not None

    unresolved_call = Content.from_function_call("same", "remote", arguments={})
    paired_call = Content.from_function_call("paired-id", "paired", arguments={})
    informational_call = Content.from_function_call("info-id", "informational", arguments={}, informational_only=True)
    history = [
        Message("user", ["do work"]),
        Message("assistant", [unresolved_call, paired_call, informational_call]),
        Message("tool", [Content.from_function_result("paired-id", result="done")]),
    ]
    context = SessionContext(
        input_messages=[
            Message(
                "user",
                ["continue"],
                additional_properties={HistoryMarkerKind.CONTINUATION_KEY: True},
            )
        ]
    )
    context.extend_messages("history", history)

    await provider.before_run(
        agent=SimpleNamespace(default_options={"tools": _tools}),
        session=object(),
        context=context,
        state={},
    )

    assert [tool.name for tool in context.tools] == ["remote"]

    await adapter.disconnect_all()


async def test_resume_provider_skips_call_resolved_behind_consecutive_assistant_block() -> None:
    """A call answered in the shared result block behind a sibling assistant
    message is resolved — it must not rehydrate a resume tool.

    The kernel folds all of a response's tool results into ONE tool message
    appended after the response's complete message list, so a multi-message
    response persists as ``[assistant, assistant, tool]`` and the first
    message's results sit behind the second assistant message.
    """
    remote = _function("remote")
    other = _function("other")
    adapter, _fake, _tools = await _connected(remote, other)
    provider = adapter.create_resume_provider()
    assert provider is not None

    history = [
        Message("user", ["do work"]),
        Message("assistant", [Content.from_function_call("c1", "remote", arguments={})]),
        Message("assistant", [Content.from_function_call("c2", "other", arguments={})]),
        Message(
            "tool",
            [
                Content.from_function_result("c1", result="done"),
                Content.from_function_result("c2", result="done"),
            ],
        ),
    ]
    context = SessionContext(
        input_messages=[
            Message(
                "user",
                ["continue"],
                additional_properties={HistoryMarkerKind.CONTINUATION_KEY: True},
            )
        ]
    )
    context.extend_messages("history", history)

    await provider.before_run(
        agent=SimpleNamespace(default_options={"tools": _tools}),
        session=object(),
        context=context,
        state={},
    )

    assert context.tools == []

    await adapter.disconnect_all()


async def test_resume_provider_accepts_result_only_assistant_after_empty_sibling() -> None:
    """An assistant-role function result completes the preceding MCP call."""
    remote = _function("remote")
    adapter, _fake, _tools = await _connected(remote)
    provider = adapter.create_resume_provider()
    assert provider is not None

    history = [
        Message("user", ["do work"]),
        Message("assistant", [Content.from_function_call("c1", "remote", arguments={})]),
        Message("assistant", []),
        Message("assistant", [Content.from_function_result("c1", result="done")]),
    ]
    context = SessionContext(
        input_messages=[
            Message(
                "user",
                ["continue"],
                additional_properties={HistoryMarkerKind.CONTINUATION_KEY: True},
            )
        ]
    )
    context.extend_messages("history", history)

    await provider.before_run(
        agent=SimpleNamespace(default_options={"tools": _tools}),
        session=object(),
        context=context,
        state={},
    )

    assert context.tools == []

    await adapter.disconnect_all()


async def test_resume_provider_shares_result_cursor_across_consecutive_assistant_siblings() -> None:
    """One shared result answers only the first same-id sibling call.

    Result cursors already distinguish repeated call ids within one
    assistant message. Splitting the same ordered calls across sibling
    messages must not reset that cursor and consume the shared result twice.
    """
    remote = _function("remote")
    other = _function("other")
    adapter, _fake, _tools = await _connected(remote, other)
    provider = adapter.create_resume_provider()
    assert provider is not None

    history = [
        Message("user", ["do work"]),
        Message("assistant", [Content.from_function_call("c1", "remote", arguments={})]),
        Message("assistant", [Content.from_function_call("c1", "other", arguments={})]),
        Message("tool", [Content.from_function_result("c1", result="one answer")]),
    ]
    context = SessionContext(
        input_messages=[
            Message(
                "user",
                ["continue"],
                additional_properties={HistoryMarkerKind.CONTINUATION_KEY: True},
            )
        ]
    )
    context.extend_messages("history", history)

    await provider.before_run(
        agent=SimpleNamespace(default_options={"tools": _tools}),
        session=object(),
        context=context,
        state={},
    )

    assert [tool.name for tool in context.tools] == ["other"]

    await adapter.disconnect_all()


async def test_resume_provider_does_not_pair_result_across_history_marker() -> None:
    remote = _function("remote")
    adapter, _fake, _tools = await _connected(remote)
    provider = adapter.create_resume_provider()
    assert provider is not None

    marker = Message("assistant", ["Execution interrupted"])
    marker.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.INTERRUPTED
    history = [
        Message("user", ["do work"]),
        Message("assistant", [Content.from_function_call("c1", "remote", arguments={})]),
        marker,
        Message("tool", [Content.from_function_result("c1", result="orphan")]),
    ]
    context = SessionContext(
        input_messages=[
            Message(
                "user",
                ["continue"],
                additional_properties={HistoryMarkerKind.CONTINUATION_KEY: True},
            )
        ]
    )
    context.extend_messages("history", history)

    await provider.before_run(
        agent=SimpleNamespace(default_options={"tools": _tools}),
        session=object(),
        context=context,
        state={},
    )

    assert [tool.name for tool in context.tools] == ["remote"]

    await adapter.disconnect_all()


async def test_resume_provider_does_not_cross_a_new_raw_user_turn() -> None:
    remote = _function("remote")
    adapter, _fake, _tools = await _connected(remote)
    provider = adapter.create_resume_provider()
    assert provider is not None
    history = [
        Message("user", ["old turn"]),
        Message("assistant", [Content.from_function_call("pending", "remote", arguments={})]),
    ]
    context = SessionContext(input_messages=[Message("user", ["new turn"])])
    context.extend_messages("history", history)

    await provider.before_run(
        agent=SimpleNamespace(default_options={"tools": _tools}),
        session=object(),
        context=context,
        state={},
    )

    assert context.tools == []

    await adapter.disconnect_all()


@pytest.mark.parametrize("progressive", [False, True])
async def test_duplicate_local_names_across_mcp_servers_fail_closed(progressive: bool) -> None:
    adapter = MCPAdapter()

    def factory(config: MCPServerConfig) -> _FakeCatalogTool:
        function = _function("remote")
        set_tool_context(function, {"server_name": config.name, "remote_name": "remote"})
        return _FakeCatalogTool([function])

    configs = [
        MCPServerConfig(name="one", transport="stdio", command="python", use_progressive_disclosure=progressive),
        MCPServerConfig(name="two", transport="stdio", command="python", use_progressive_disclosure=progressive),
    ]
    with (
        patch("chrys.service.mcp._connection._create_mcp_tool", side_effect=factory),
        pytest.raises(MCPToolNameCollisionError, match="remote"),
    ):
        await adapter.connect_all(configs)

    await adapter.disconnect_all()


def test_progressive_controls_and_runtime_loaded_tools_follow_mcp_policy() -> None:
    controls = [
        _function("mcp_srv_list_mcp_tools"),
        _function("mcp_srv_load_tool"),
        _function("mcp_srv_unload_tool"),
    ]
    for tool in controls:
        set_tool_kind(tool, KIND_MCP)

    automatic = ApprovalPolicy(ApprovalConfig(default="auto"), tools=controls)
    required = ApprovalPolicy(ApprovalConfig(default="auto", overrides={"mcp": "require"}), tools=controls)
    exact = ApprovalPolicy(
        ApprovalConfig(default="auto", overrides={"mcp.mcp_srv_load_tool": "require"}),
        tools=controls,
    )

    assert not automatic.should_require_approval(controls[0].name, KIND_MCP)
    assert required.should_require_approval(controls[0].name, KIND_MCP)
    assert required.should_require_approval("later_loaded_remote", KIND_MCP)
    assert exact.should_require_approval("mcp_srv_load_tool", KIND_MCP)
    assert not exact.should_require_approval("mcp_srv_list_mcp_tools", KIND_MCP)


class _ScriptedClient:
    def __init__(self, turns: list[Any]) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    def get_response(
        self,
        messages: Any,
        *,
        stream: bool = False,
        options: Any = None,
        **kwargs: Any,
    ) -> Any:
        copied_options = dict(options or {})
        self.calls.append(
            {
                "messages": list(messages),
                "options": copied_options,
                "tool_names": [tool.name for tool in copied_options.get("tools", [])],
            }
        )
        turn = self.turns.pop(0)
        if not stream:

            async def resolve() -> ChatResponse:
                return turn

            return resolve()

        async def updates() -> Any:
            for update in turn:
                yield update

        return ResponseStream(updates(), finalizer=ChatResponse.from_updates)


def _call_response(call_id: str, name: str, arguments: dict[str, Any]) -> ChatResponse:
    return ChatResponse(
        messages=[Message("assistant", [Content.from_function_call(call_id, name, arguments=arguments)])]
    )


def _call_update(call_id: str, name: str, arguments: dict[str, Any]) -> ChatResponseUpdate:
    return ChatResponseUpdate(
        role="assistant",
        contents=[Content.from_function_call(call_id, name, arguments=arguments)],
    )


async def test_blocking_loop_sees_loaded_mcp_tool_on_next_iteration() -> None:
    adapter, _fake, initial = await _connected(_function("remote"))
    load_name = "mcp_srv_load_tool"
    wire = _ScriptedClient(
        [
            _call_response("load", load_name, {"tool": "remote"}),
            _call_response("remote", "remote", {"value": "called"}),
            ChatResponse(messages=[Message("assistant", ["done"])]),
        ]
    )
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire))

    await layer.get_response([Message("user", ["work"])], options={"tools": initial})

    assert "remote" not in wire.calls[0]["tool_names"]
    assert "remote" in wire.calls[1]["tool_names"]

    await adapter.disconnect_all()


@pytest.mark.parametrize("control", ["list_mcp_tools", "load_tool", "unload_tool"])
async def test_control_tools_reject_extra_arguments_before_execution_and_accept_retry(control: str) -> None:
    remote = _function("remote")
    remote_schema = remote.parameters()
    adapter, _fake, initial = await _connected(remote)
    try:
        name = f"mcp_srv_{control}"
        arguments = {} if control == "list_mcp_tools" else {"tool": "remote"}
        preparation = (
            [_call_response("prepare", "mcp_srv_load_tool", {"tool": "remote"})] if control == "unload_tool" else []
        )
        wire = _ScriptedClient(
            [
                *preparation,
                _call_response("bad-call", name, {**arguments, "_call": "extra"}),
                _call_response("bad-note", name, {**arguments, "note": "extra"}),
                _call_response("retry", name, arguments),
                ChatResponse(messages=[Message("assistant", ["done"])]),
            ]
        )
        probe = _ProbeFunction()
        layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire))

        response = await layer.get_response([Message("user", ["work"])], options={"tools": initial}, middleware=[probe])

        bad_call, bad_note, retry = _result_contents(response)[-3:]
        for result, field in ((bad_call, "_call"), (bad_note, "note")):
            _assert_actionable_argument_error(result, name, f"unknown '{field}'")
        assert retry.exception is None
        assert not str(retry.result).startswith("Error:")
        assert [context.arguments for context in probe.contexts] == [
            *([{"tool": "remote"}] if preparation else []),
            arguments,
        ]
        assert ("remote" in wire.calls[-1]["tool_names"]) is (control == "load_tool")
        if control == "list_mcp_tools":
            assert "Expected: {}." in str(bad_call.result)
            assert "remote" in str(retry.result)
        # Closing local control schemas must not change remote declarations.
        assert remote.parameters() == clone_mcp_function_tool(remote).parameters() == remote_schema
        assert "additionalProperties" not in remote_schema
    finally:
        await adapter.disconnect_all()


async def test_streaming_loop_sees_loaded_mcp_tool_on_next_iteration() -> None:
    adapter, _fake, initial = await _connected(_function("remote"))
    load_name = "mcp_srv_load_tool"
    wire = _ScriptedClient(
        [
            [_call_update("load", load_name, {"tool": "remote"})],
            [_call_update("remote", "remote", {"value": "called"})],
            [ChatResponseUpdate(role="assistant", contents=[{"type": "text", "text": "done"}])],
        ]
    )
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire))

    stream = layer.get_response([Message("user", ["work"])], stream=True, options={"tools": initial})
    await stream.get_final_response()

    assert "remote" not in wire.calls[0]["tool_names"]
    assert "remote" in wire.calls[1]["tool_names"]

    await adapter.disconnect_all()


# --- _unresolved_actionable_calls: exchange-shape pins -------------------

_scan_unresolved = ProgressiveMCPResumeProvider._unresolved_actionable_calls


def _scan_call(call_id: object, name: str = "remote") -> Content:
    return Content.from_function_call(call_id, name, arguments={})


def _scan_result(call_id: object) -> Content:
    return Content.from_function_result(call_id, result=f"r_{call_id!r}")


def _scan_marker(*contents: Content) -> Message:
    message = Message("assistant", list(contents))
    message.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    return message


def test_scan_user_role_result_message_does_not_resolve_call() -> None:
    """A user-role message carrying only results never answers a call."""
    messages = [
        Message("user", ["go"]),
        Message("assistant", [_scan_call("c1")]),
        Message("user", [_scan_result("c1")]),
    ]
    assert _scan_unresolved(messages) == [("remote", None)]


def test_scan_empty_id_call_surfaces_unresolved() -> None:
    """An empty-id call can never consume a result and stays surfaced."""
    messages = [
        Message("user", ["go"]),
        Message("assistant", [_scan_call("")]),
        Message("tool", [_scan_result("x")]),
    ]
    assert _scan_unresolved(messages) == [("remote", None)]


def test_scan_falsy_result_id_never_collected() -> None:
    """A falsy result id resolves nothing."""
    messages = [
        Message("user", ["go"]),
        Message("assistant", [_scan_call("c1")]),
        Message("tool", [_scan_result(0)]),
    ]
    assert _scan_unresolved(messages) == [("remote", None)]


def test_scan_falsy_numeric_call_surfaces_unresolved() -> None:
    """A falsy non-string call id joins the empty-id stream and stays surfaced."""
    messages = [
        Message("user", ["go"]),
        Message("assistant", [_scan_call(0)]),
        Message("tool", [_scan_result(0)]),
    ]
    assert _scan_unresolved(messages) == [("remote", None)]


def test_scan_falsy_unhashable_call_surfaces_unresolved() -> None:
    """A falsy unhashable call id is coerced and surfaced without crashing."""
    messages = [Message("user", ["go"]), Message("assistant", [_scan_call([])])]
    assert _scan_unresolved(messages) == [("remote", None)]


def test_scan_marker_message_result_does_not_resolve_call() -> None:
    """A history-marker message never carries answers for a preceding call."""
    messages = [
        Message("user", ["go"]),
        Message("assistant", [_scan_call("c1")]),
        _scan_marker(_scan_result("c1")),
    ]
    assert _scan_unresolved(messages) == [("remote", None)]


def test_scan_embedded_same_message_result_resolves_call() -> None:
    """A result embedded in the call's own message answers that call."""
    messages = [
        Message("user", ["go"]),
        Message("assistant", [_scan_call("c1"), _scan_result("c1")]),
    ]
    assert _scan_unresolved(messages) == []


def test_scan_numeric_call_resolved_by_digit_string_result() -> None:
    """Malformed ids pair by string form: int 7 is answered by "7"."""
    messages = [
        Message("user", ["go"]),
        Message("assistant", [_scan_call(7)]),
        Message("tool", [_scan_result("7")]),
    ]
    assert _scan_unresolved(messages) == []


def test_scan_separately_constructed_nan_pair_resolves() -> None:
    """Two distinct NaN id objects pair through their identical string forms."""
    messages = [
        Message("user", ["go"]),
        Message("assistant", [_scan_call(float("nan"))]),
        Message("tool", [_scan_result(float("nan"))]),
    ]
    assert _scan_unresolved(messages) == []


def test_scan_int_call_not_resolved_by_bool_result() -> None:
    """Python-equal ids of different types split by string form: 1 is not True."""
    messages = [
        Message("user", ["go"]),
        Message("assistant", [_scan_call(1)]),
        Message("tool", [_scan_result(True)]),
    ]
    assert _scan_unresolved(messages) == [("remote", None)]


def test_scan_truthy_unhashable_pair_resolves_without_crash() -> None:
    """Unhashable ids are processed by string form, never a TypeError."""
    messages = [
        Message("user", ["go"]),
        Message("assistant", [_scan_call(["x"])]),
        Message("tool", [_scan_result(["x"])]),
    ]
    assert _scan_unresolved(messages) == []


def test_scan_truthy_unhashable_dangling_call_surfaces_unresolved() -> None:
    """A dangling unhashable-id call is surfaced without crashing."""
    messages = [Message("user", ["go"]), Message("assistant", [_scan_call(["x"])])]
    assert _scan_unresolved(messages) == [("remote", None)]
