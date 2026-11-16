# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the owned MCP engine: allowed-tools filtering, closure argument hygiene, catalog reloads, lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, cast
from unittest.mock import patch

import anyio
import pytest
from mcp import types
from mcp.shared.message import SessionMessage

import chrys.service.mcp.owned as owned_mcp
from chrys.kernel import ChatResponse, Content, Message
from chrys.kernel.exceptions import ToolExecutionException
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.kernel.types import ChatResponseUpdate
from chrys.service.mcp._http_transport import _HTTPMCPTool
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.mcp.errors import (
    MCPToolNameAmbiguityError,
    MCPToolNameCollisionError,
    MCPToolNameValidationError,
)
from chrys.service.mcp.owned import (
    _MAX_CATALOG_PAGE_REQUESTS,
    _MCP_FRAMEWORK_DENYLIST,
    _MCP_NORMALIZED_NAME_KEY,
    _MCP_REMOTE_NAME_KEY,
    MCPTool,
    _get_input_model_from_mcp_prompt,
)
from chrys.service.profiles.agents.schema import MCPServerConfig
from tests.kernel._fakes import _final_response, _result_contents, _stack, _text_response, _text_update, _user
from tests.service.mcp._helpers import (
    _as_client_session,
    _FakeConnectionTool,
    _function_tool,
    _load_fake_remote_tools,
    _mcp_remote_prompt,
    _mcp_remote_tool,
    _ScriptedClientSession,
)

if TYPE_CHECKING:
    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
    from mcp.client.experimental.task_handlers import ExperimentalTaskHandlers
    from mcp.client.session import (
        ClientSession,
        ElicitationFnT,
        ListRootsFnT,
        LoggingFnT,
        MessageHandlerFnT,
        SamplingFnT,
    )


def _ok_result() -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text="ok")], isError=False)


async def _load_calling_remote_tools(tool: MCPTool, *remote_tools: types.Tool) -> _ScriptedClientSession:
    session = _ScriptedClientSession(tools=remote_tools, call_tool=_ok_result())
    tool.session = _as_client_session(session)
    await tool.load_tools()
    return session


def _tool_list_changed_notification() -> Any:
    from mcp import types

    return types.ServerNotification(root=types.ToolListChangedNotification())


# ---------------------------------------------------------------------------
# MCPTool.functions — allowed_tools filtering
# ---------------------------------------------------------------------------


def test_mcp_tool_empty_allowed_tools_exposes_no_functions() -> None:
    tool = MCPTool(name="m", allowed_tools=[])
    tool._functions = [_function_tool("one"), _function_tool("two")]

    assert tool.functions == []


def test_mcp_allowed_tools_does_not_match_lossy_normalized_alias() -> None:
    exposed = _function_tool("delete-everything")
    exposed.additional_properties = {
        _MCP_REMOTE_NAME_KEY: "delete/everything",
        _MCP_NORMALIZED_NAME_KEY: "delete-everything",
    }
    tool = MCPTool(name="m", allowed_tools=["delete-everything"])
    tool._functions = [exposed]

    assert tool.functions == []


def test_mcp_allowed_tools_accepts_local_name_when_remote_is_already_normalized() -> None:
    exposed = _function_tool("srv_echo")
    exposed.additional_properties = {
        _MCP_REMOTE_NAME_KEY: "echo",
        _MCP_NORMALIZED_NAME_KEY: "echo",
    }
    tool = MCPTool(name="m", allowed_tools=["srv_echo"])
    tool._functions = [exposed]

    assert tool.functions == [exposed]


# ---------------------------------------------------------------------------
# generated closure argument hygiene, header providers, request meta precedence
# ---------------------------------------------------------------------------


class _RecordingMCPTool(MCPTool):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(name="m", **kwargs)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, tool_name: str, **kwargs: Any) -> str:
        self.calls.append((tool_name, dict(kwargs)))
        return "ok"


async def test_mcp_generated_tool_closure_strips_model_supplied_meta() -> None:
    tool = _RecordingMCPTool()
    await _load_fake_remote_tools(tool, _mcp_remote_tool("remote"))
    func = tool._functions[0]

    await func.invoke(arguments={"_meta": {"forged": "bad"}}, skip_parsing=True)

    assert tool.calls == [("remote", {})]


async def test_mcp_generated_tool_closure_preserves_trusted_runtime_meta() -> None:
    tool = _RecordingMCPTool()
    await _load_fake_remote_tools(tool, _mcp_remote_tool("remote"))
    func = tool._functions[0]
    context = FunctionInvocationContext(
        function=func,
        arguments={},
        kwargs={"_meta": {"trusted": "ok"}},
    )

    await func.invoke(arguments={"_meta": {"forged": "bad"}}, context=context, skip_parsing=True)

    assert tool.calls == [("remote", {"_meta": {"trusted": "ok"}})]


async def test_mcp_generated_tool_separates_declared_arguments_from_runtime_kwargs() -> None:
    tool = _RecordingMCPTool()
    await _load_fake_remote_tools(
        tool,
        _mcp_remote_tool(
            "remote",
            input_schema={
                "type": "object",
                "properties": {"session": {"type": "string"}},
            },
        ),
    )
    func = tool._functions[0]
    context = FunctionInvocationContext(
        function=func,
        arguments={"session": "sr-design"},
        kwargs={"session": object(), "future_runtime_key": object()},
    )

    await func.invoke(arguments={"session": "sr-design"}, context=context, skip_parsing=True)

    assert tool.calls == [("remote", {"session": "sr-design"})]


async def test_mcp_generated_tool_does_not_substitute_runtime_value_for_omitted_optional_argument() -> None:
    tool = _RecordingMCPTool()
    await _load_fake_remote_tools(
        tool,
        _mcp_remote_tool(
            "remote",
            input_schema={
                "type": "object",
                "properties": {"session": {"type": "string"}},
            },
        ),
    )
    func = tool._functions[0]
    context = FunctionInvocationContext(
        function=func,
        arguments={},
        kwargs={"session": object()},
    )

    await func.invoke(arguments={}, context=context, skip_parsing=True)

    assert tool.calls == [("remote", {})]


async def test_mcp_generated_tool_forwards_only_explicit_runtime_extras() -> None:
    tool = _RecordingMCPTool(additional_tool_argument_names={"remote": ["tenant_id"]})
    await _load_fake_remote_tools(tool, _mcp_remote_tool("remote"))
    func = tool._functions[0]
    context = FunctionInvocationContext(
        function=func,
        arguments={},
        kwargs={"tenant_id": "trusted-tenant", "internal": object()},
    )

    await func.invoke(arguments={}, context=context, skip_parsing=True)

    assert tool.calls == [("remote", {"tenant_id": "trusted-tenant"})]


async def test_mcp_generated_tool_cannot_override_bound_remote_name() -> None:
    tool = MCPTool(name="m", allowed_tools=["safe"])
    session = await _load_calling_remote_tools(
        tool,
        _mcp_remote_tool(
            "safe",
            input_schema={
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        ),
        _mcp_remote_tool("danger"),
    )
    assert [func.name for func in tool.functions] == ["safe"]

    await tool.functions[0].invoke(
        arguments={"value": "ok", "_remote_tool_name": "danger"},
        skip_parsing=True,
    )

    (call,) = session.tool_calls
    assert call.name == "safe"
    assert call.arguments == {"value": "ok"}


@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
@pytest.mark.parametrize(
    ("arguments", "server_calls"),
    [("oops", []), ("[1]", []), ("null", []), ("", [{}]), ('{"state": "open"}', [{"state": "open"}])],
    ids=["word", "array", "null", "empty", "object"],
)
async def test_mcp_tool_called_through_the_loop_never_runs_on_non_object_arguments(
    stream: bool, arguments: str, server_calls: list[dict[str, Any]]
) -> None:
    """A lenient remote schema would accept the parser's ``{"raw": ...}`` wrapper; the server must not be called."""
    tool = MCPTool(name="m")
    session = await _load_calling_remote_tools(
        tool,
        _mcp_remote_tool(
            "list_issues",
            input_schema={
                "type": "object",
                "properties": {"state": {"type": "string"}, "limit": {"type": "integer"}},
            },
        ),
    )
    (function,) = tool.functions
    call = Content.from_function_call("c1", function.name, arguments=arguments)
    if stream:
        turns: list[Any] = [[ChatResponseUpdate(contents=[call], role="assistant")], [_text_update("done")]]
    else:
        turns = [ChatResponse(messages=[Message("assistant", [call])]), _text_response()]
    layer, _wire = _stack(turns)

    response = await _final_response(layer, [_user()], stream=stream, options={"tools": [function]})

    assert [server_call.arguments for server_call in session.tool_calls] == server_calls
    (result,) = _result_contents(response)
    assert str(result.result).startswith("Error: ") is (not server_calls)


async def test_mcp_declared_remote_name_argument_is_data_not_dispatch_control() -> None:
    tool = MCPTool(name="m", allowed_tools=["safe"])
    session = await _load_calling_remote_tools(
        tool,
        _mcp_remote_tool(
            "safe",
            input_schema={
                "type": "object",
                "properties": {"_remote_tool_name": {"type": "string"}},
            },
        ),
        _mcp_remote_tool("danger"),
    )

    await tool.functions[0].invoke(
        arguments={"_remote_tool_name": "danger"},
        skip_parsing=True,
    )

    (call,) = session.tool_calls
    assert call.name == "safe"
    assert call.arguments == {"_remote_tool_name": "danger"}


async def test_mcp_declared_ctx_argument_survives_context_injection() -> None:
    tool = MCPTool(name="m")
    session = await _load_calling_remote_tools(
        tool,
        _mcp_remote_tool(
            "safe",
            input_schema={
                "type": "object",
                "properties": {"ctx": {"type": "string"}},
                "required": ["ctx"],
            },
        ),
    )

    await tool.functions[0].invoke(
        arguments={"ctx": "business-value"},
        skip_parsing=True,
    )

    (call,) = session.tool_calls
    assert call.name == "safe"
    assert call.arguments == {"ctx": "business-value"}


async def test_mcp_trusted_runtime_extra_overrides_same_named_model_argument() -> None:
    tool = MCPTool(
        name="m",
        additional_tool_argument_names={"safe": ["tenant_id"]},
    )
    session = await _load_calling_remote_tools(
        tool,
        _mcp_remote_tool(
            "safe",
            input_schema={
                "type": "object",
                "properties": {"tenant_id": {"type": "string"}},
            },
        ),
    )
    func = tool.functions[0]
    context = FunctionInvocationContext(
        function=func,
        arguments={"tenant_id": "model-tenant"},
        kwargs={"tenant_id": "trusted-tenant"},
    )

    await func.invoke(
        arguments={"tenant_id": "model-tenant"},
        context=context,
        skip_parsing=True,
    )

    (call,) = session.tool_calls
    assert call.name == "safe"
    assert call.arguments == {"tenant_id": "trusted-tenant"}


@pytest.mark.parametrize("argument_name", sorted(_MCP_FRAMEWORK_DENYLIST - {"_meta"}))
def test_mcp_declared_framework_named_argument_is_forwarded(argument_name: str) -> None:
    tool = MCPTool(name="m")
    tool._tool_param_names_by_name = {"remote": {argument_name}}

    filtered, _meta = tool._prepare_call_kwargs("remote", {argument_name: "declared-value"})

    assert filtered == {argument_name: "declared-value"}


async def test_mcp_declared_collision_reaches_client_session_without_model_meta() -> None:
    session = _ScriptedClientSession(
        tools=[
            _mcp_remote_tool(
                "remote",
                input_schema={
                    "type": "object",
                    "properties": {"session": {"type": "string"}},
                },
            )
        ],
        call_tool=_ok_result(),
    )
    tool = MCPTool(name="m", session=_as_client_session(session))
    await tool.load_tools()
    func = tool._functions[0]
    context = FunctionInvocationContext(
        function=func,
        arguments={"session": "sr-design", "_meta": {"forged": "bad"}},
        kwargs={"session": object()},
    )

    await func.invoke(
        arguments={"session": "sr-design", "_meta": {"forged": "bad"}},
        context=context,
        skip_parsing=True,
    )

    (call,) = session.tool_calls
    assert call.name == "remote"
    assert call.arguments == {"session": "sr-design"}
    assert call.meta is None or "forged" not in call.meta


def test_mcp_request_meta_precedence_is_tool_meta_over_otel_over_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    def inject(carrier: dict[str, str]) -> None:
        carrier.update({"traceparent": "otel", "baggage": "otel"})

    monkeypatch.setattr(owned_mcp.propagate, "inject", inject)
    tool = MCPTool(name="m")
    tool._tool_param_names_by_name = {"remote": {"value"}}
    tool._tool_call_meta_by_name = {"remote": {"traceparent": "tool", "tool": "meta"}}

    filtered, meta = tool._prepare_call_kwargs(
        "remote",
        {
            "value": "ok",
            "_meta": {"traceparent": "caller", "caller": "meta"},
        },
    )

    assert filtered == {"value": "ok"}
    assert meta == {
        "traceparent": "tool",
        "caller": "meta",
        "baggage": "otel",
        "tool": "meta",
    }


# ---------------------------------------------------------------------------
# owned catalog — normalization, collisions, notification-driven reloads
# ---------------------------------------------------------------------------


async def test_owned_catalog_normalizes_periods_to_provider_safe_names() -> None:
    owned_tool = MCPTool(name="s")
    await _load_fake_remote_tools(owned_tool, _mcp_remote_tool("github.v1.search"))

    assert [
        (function.name, function.additional_properties[_MCP_REMOTE_NAME_KEY]) for function in owned_tool.functions
    ] == [("github-v1-search", "github.v1.search")]


@pytest.mark.parametrize("connection_path", ["test", "agent"])
async def test_prefix_and_remote_name_combination_over_64_characters_fails_early(connection_path: str) -> None:
    prefix = "a" * 50
    remote_name = "b" * 14
    owned_tool = MCPTool(name="s", tool_name_prefix=prefix)
    await _load_fake_remote_tools(owned_tool, _mcp_remote_tool(remote_name))
    assert owned_tool.functions[0].name == f"{prefix}_{remote_name}"

    adapter = MCPAdapter()
    config = MCPServerConfig(name="s", transport="stdio", command="python", tool_name_prefix=prefix)
    fake = _FakeConnectionTool(functions=owned_tool.functions)
    with (
        patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake),
        pytest.raises(MCPToolNameValidationError, match=r"65 characters.*maximum is 64"),
    ):
        if connection_path == "test":
            await adapter.test_connection(config)
        else:
            await adapter.connect(config)

    await adapter.disconnect_all()


@pytest.mark.parametrize("connection_path", ["test", "agent"])
async def test_owned_catalog_normalized_collision_fails_test_and_agent_connection(connection_path: str) -> None:
    owned_tool = MCPTool(name="s")
    await _load_fake_remote_tools(owned_tool, _mcp_remote_tool("a/b"), _mcp_remote_tool("a-b"))
    exposed = [
        (function.name, function.additional_properties[_MCP_REMOTE_NAME_KEY]) for function in owned_tool.functions
    ]
    assert exposed == [("a-b", "a/b"), ("a-b", "a-b")]

    adapter = MCPAdapter()
    config = MCPServerConfig(name="s", transport="stdio", command="python")
    fake = _FakeConnectionTool(functions=owned_tool.functions)
    with (
        patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake),
        pytest.raises(MCPToolNameCollisionError, match=r"invalid tool configuration.*a-b"),
    ):
        if connection_path == "test":
            await adapter.test_connection(config)
        else:
            await adapter.connect(config)

    await adapter.disconnect_all()


@pytest.mark.parametrize("remote_order", [("a/b", "a-b"), ("a-b", "a/b")])
async def test_owned_catalog_filters_allowlist_before_collision_validation(remote_order: tuple[str, str]) -> None:
    owned_tool = MCPTool(name="s", allowed_tools=["a/b"])
    await _load_fake_remote_tools(owned_tool, *(_mcp_remote_tool(name) for name in remote_order))

    assert [
        (function.name, function.additional_properties[_MCP_REMOTE_NAME_KEY]) for function in owned_tool.functions
    ] == [("a-b", "a/b")]


async def _prefixed_catalog_where_one_local_name_is_another_remote_name(
    allowed_tools: list[str] | None,
) -> list[Any]:
    owned_tool = MCPTool(name="s", tool_name_prefix="gh", allowed_tools=allowed_tools)
    await _load_fake_remote_tools(owned_tool, _mcp_remote_tool("gh_search"), _mcp_remote_tool("search"))
    return owned_tool.functions


@pytest.mark.parametrize("connection_path", ["test", "agent"])
async def test_allowed_tools_name_selecting_two_tools_fails_test_and_agent_connection(connection_path: str) -> None:
    # ``gh_search`` is one tool's original name and the other's prefixed name.
    functions = await _prefixed_catalog_where_one_local_name_is_another_remote_name(["gh_search"])
    assert [(function.name, function.additional_properties[_MCP_REMOTE_NAME_KEY]) for function in functions] == [
        ("gh_gh_search", "gh_search"),
        ("gh_search", "search"),
    ]

    adapter = MCPAdapter()
    config = MCPServerConfig(
        name="s", transport="stdio", command="python", tool_name_prefix="gh", allowed_tools=["gh_search"]
    )
    fake = _FakeConnectionTool(functions=functions)
    with (
        patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake),
        pytest.raises(MCPToolNameAmbiguityError) as exc_info,
    ):
        if connection_path == "test":
            await adapter.test_connection(config)
        else:
            await adapter.connect(config)

    assert (
        "has invalid tool configuration: a configured tool name selects more than one tool: 'gh_search' matches "
        "the server's 'gh_search' (write 'gh_gh_search' to select only it), "
        "the server's 'search' (write 'search' to select only it)."
    ) in str(exc_info.value)
    assert "Tool Name Prefix" not in str(exc_info.value)
    await adapter.disconnect_all()


async def test_allowed_tools_names_selecting_one_tool_each_connect() -> None:
    # The names the ambiguity error suggests for the two tools.
    functions = await _prefixed_catalog_where_one_local_name_is_another_remote_name(["search", "gh_gh_search"])

    adapter = MCPAdapter()
    config = MCPServerConfig(
        name="s",
        transport="stdio",
        command="python",
        tool_name_prefix="gh",
        allowed_tools=["search", "gh_gh_search"],
    )
    with patch("chrys.service.mcp._connection._create_mcp_tool", return_value=_FakeConnectionTool(functions=functions)):
        tools = await adapter.connect(config)

    assert sorted(tool.name for tool in tools) == ["gh_gh_search", "gh_search"]
    await adapter.disconnect_all()


async def test_owned_catalog_tool_and_prompt_collision_fails_connection() -> None:
    owned_tool = MCPTool(name="s")
    owned_tool.session = _as_client_session(
        _ScriptedClientSession(tools=[_mcp_remote_tool("shared")], prompts=[_mcp_remote_prompt("shared")])
    )
    await owned_tool.load_tools()
    await owned_tool.load_prompts()
    assert [function.name for function in owned_tool.functions] == ["shared", "shared"]

    adapter = MCPAdapter()
    config = MCPServerConfig(name="s", transport="stdio", command="python")
    fake = _FakeConnectionTool(functions=owned_tool.functions)
    with (
        patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake),
        pytest.raises(MCPToolNameCollisionError, match=r"invalid tool configuration.*shared") as exc_info,
    ):
        await adapter.connect(config)

    assert "disable 'Expose server prompts'" in str(exc_info.value)
    await adapter.disconnect_all()


async def test_owned_catalog_reload_deduplicates_only_the_same_remote_declaration() -> None:
    owned_tool = MCPTool(name="s")
    owned_tool.session = _as_client_session(
        _ScriptedClientSession(tools=[_mcp_remote_tool("tool")], prompts=[_mcp_remote_prompt("prompt")])
    )

    await owned_tool.load_tools()
    await owned_tool.load_prompts()
    await owned_tool.load_tools()
    await owned_tool.load_prompts()

    assert [function.name for function in owned_tool.functions] == ["tool", "prompt"]


async def test_owned_catalog_notification_reloads_without_duplicate_remote_tools() -> None:
    owned_tool = MCPTool(name="s")
    session = _ScriptedClientSession(tools=[_mcp_remote_tool("tool")])
    owned_tool.session = _as_client_session(session)
    await owned_tool.load_tools()

    await owned_tool.message_handler(_tool_list_changed_notification())
    tasks = list(owned_tool._pending_reload_tasks)
    assert len(tasks) == 1
    await asyncio.gather(*tasks)

    assert [function.name for function in owned_tool.functions] == ["tool"]
    assert session.requests.count("list_tools") == 2


async def test_owned_catalog_notifications_coalesce_by_cancelling_first_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned_tool = MCPTool(name="s")
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    second_release = asyncio.Event()
    calls = 0
    cancelled = 0

    async def slow_load_tools() -> None:
        nonlocal calls, cancelled
        calls += 1
        if calls == 1:
            first_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled += 1
                raise
        second_started.set()
        await second_release.wait()

    monkeypatch.setattr(owned_tool, "load_tools", slow_load_tools)
    await owned_tool.message_handler(_tool_list_changed_notification())
    first_task = next(iter(owned_tool._pending_reload_tasks))
    await asyncio.wait_for(first_started.wait(), timeout=5)

    await owned_tool.message_handler(_tool_list_changed_notification())
    second_task = next(task for task in owned_tool._pending_reload_tasks if task is not first_task)
    await asyncio.wait_for(second_started.wait(), timeout=5)
    await asyncio.gather(first_task, return_exceptions=True)

    assert cancelled == 1
    assert calls == 2
    second_release.set()
    await second_task


async def test_owned_catalog_close_cancels_pending_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    owned_tool = MCPTool(name="s")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def pending_load_tools() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(owned_tool, "load_tools", pending_load_tools)
    await owned_tool.message_handler(_tool_list_changed_notification())
    await asyncio.wait_for(started.wait(), timeout=5)

    await owned_tool.close()

    assert cancelled.is_set()
    assert owned_tool._pending_reload_tasks == set()


async def test_owned_catalog_reload_exception_is_logged_not_propagated(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    owned_tool = MCPTool(name="s")

    async def failing_load_tools() -> None:
        raise RuntimeError("catalog failed")

    monkeypatch.setattr(owned_tool, "load_tools", failing_load_tools)
    with caplog.at_level(logging.WARNING, logger=owned_mcp.__name__):
        await owned_tool.message_handler(_tool_list_changed_notification())
        tasks = list(owned_tool._pending_reload_tasks)
        assert len(tasks) == 1
        await asyncio.gather(*tasks)

    assert "Background MCP reload failed" in caplog.text
    assert "catalog failed" in caplog.text


class _PagedSession:
    """``list_tools``/``list_prompts`` answering one scripted page per cursor, with the SDK's signatures.

    A page is ``(items, next_cursor)``, or an exception to raise; ``cursors`` logs the
    cursor each list request asked for (None for the first page). Past
    ``_RUNAWAY_LISTING`` requests it fails the test: a listing that never ends.
    """

    _RUNAWAY_LISTING = 5 * _MAX_CATALOG_PAGE_REQUESTS

    def __init__(
        self,
        *,
        tool_pages: Callable[[str | None], tuple[list[types.Tool], str | None] | Exception] | None = None,
        prompt_pages: Callable[[str | None], tuple[list[types.Prompt], str | None] | Exception] | None = None,
    ) -> None:
        self._tool_pages = tool_pages
        self._prompt_pages = prompt_pages
        self.cursors: list[str | None] = []

    def _page[T](
        self, pages: Callable[[str | None], tuple[list[T], str | None] | Exception] | None, params: Any
    ) -> tuple[list[T], str | None]:
        cursor = params.cursor if params is not None else None
        self.cursors.append(cursor)
        if len(self.cursors) > self._RUNAWAY_LISTING:
            raise AssertionError("the listing never ended")
        if pages is None:
            raise AssertionError("the test scripted no pages for this listing")
        page = pages(cursor)
        if isinstance(page, Exception):
            raise page
        return page

    async def send_ping(self) -> types.EmptyResult:
        return types.EmptyResult()

    async def list_tools(
        self, cursor: str | None = None, *, params: types.PaginatedRequestParams | None = None
    ) -> types.ListToolsResult:
        tools, next_cursor = self._page(self._tool_pages, params)
        return types.ListToolsResult(tools=tools, nextCursor=next_cursor)

    async def list_prompts(
        self, cursor: str | None = None, *, params: types.PaginatedRequestParams | None = None
    ) -> types.ListPromptsResult:
        prompts, next_cursor = self._page(self._prompt_pages, params)
        return types.ListPromptsResult(prompts=prompts, nextCursor=next_cursor)


def _described_tool(name: str, description: str, *params: str) -> types.Tool:
    return types.Tool(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": {param: {"type": "string"} for param in params}},
    )


def test_prompt_argument_without_description_has_no_description_key() -> None:
    prompt = types.Prompt(
        name="p",
        arguments=[types.PromptArgument(name="bare"), types.PromptArgument(name="told", description="Told.")],
    )

    assert _get_input_model_from_mcp_prompt(prompt)["properties"] == {
        "bare": {"type": "string"},
        "told": {"type": "string", "description": "Told."},
    }


async def test_tool_listing_replaces_the_previous_tool_catalog() -> None:
    owned_tool = MCPTool(name="s")
    owned_tool.session = _as_client_session(
        _ScriptedClientSession(
            tools=[_described_tool("kept", "Old.", "a"), _described_tool("gone", "Gone.")],
            prompts=[_mcp_remote_prompt("prompt")],
        )
    )
    await owned_tool.load_tools()
    await owned_tool.load_prompts()

    owned_tool.session = _as_client_session(
        _ScriptedClientSession(tools=[_described_tool("kept", "New.", "b"), _described_tool("added", "Added.")])
    )
    await owned_tool.load_tools()

    assert [(f.name, f.description) for f in owned_tool.functions] == [
        ("kept", "New."),
        ("added", "Added."),
        ("prompt", "Remote prompt"),
    ]
    assert owned_tool._tool_param_names_by_name == {"kept": {"b"}, "added": set()}


async def test_prompt_listing_replaces_the_previous_prompt_catalog() -> None:
    owned_tool = MCPTool(name="s")
    owned_tool.session = _as_client_session(
        _ScriptedClientSession(
            tools=[_mcp_remote_tool("tool")], prompts=[_mcp_remote_prompt("gone"), _mcp_remote_prompt("kept")]
        )
    )
    await owned_tool.load_tools()
    await owned_tool.load_prompts()

    owned_tool.session = _as_client_session(
        _ScriptedClientSession(
            prompts=[types.Prompt(name="kept", description="New.", arguments=[types.PromptArgument(name="q")])]
        )
    )
    await owned_tool.load_prompts()

    assert [function.name for function in owned_tool.functions] == ["tool", "kept"]
    kept = owned_tool.functions[1]
    assert kept.description == "New."
    assert kept.parameters()["properties"] == {"q": {"type": "string"}}


async def test_listing_that_fails_partway_keeps_the_previous_catalog() -> None:
    owned_tool = MCPTool(name="s")
    owned_tool.session = _as_client_session(_ScriptedClientSession(tools=[_described_tool("old", "Old.", "x")]))
    await owned_tool.load_tools()

    def pages(cursor: str | None) -> tuple[list[types.Tool], str | None] | Exception:
        if cursor is None:
            return [_described_tool("new", "New.", "y")], "page-2"
        return RuntimeError("page 2 failed")

    owned_tool.session = _as_client_session(cast("Any", _PagedSession(tool_pages=pages)))
    with pytest.raises(RuntimeError, match="page 2 failed"):
        await owned_tool.load_tools()

    assert [function.name for function in owned_tool.functions] == ["old"]
    assert owned_tool._tool_param_names_by_name == {"old": {"x"}}


async def test_listing_that_repeats_a_cursor_fails_instead_of_looping() -> None:
    owned_tool = MCPTool(name="s")
    session = _PagedSession(
        tool_pages=lambda cursor: ([_mcp_remote_tool(f"t-{cursor}")], "b" if cursor == "a" else "a")
    )
    owned_tool.session = _as_client_session(cast("Any", session))

    with pytest.raises(ToolExecutionException, match="repeated a page cursor"):
        await owned_tool.load_tools()

    assert session.cursors == [None, "a", "b"]
    assert owned_tool.functions == []


async def test_listing_that_never_ends_fails_at_the_page_request_cap() -> None:
    owned_tool = MCPTool(name="s")
    session = _PagedSession(prompt_pages=lambda cursor: ([], f"{cursor}+"))
    owned_tool.session = _as_client_session(cast("Any", session))

    with pytest.raises(ToolExecutionException, match=f"within {_MAX_CATALOG_PAGE_REQUESTS} page requests"):
        await owned_tool.load_prompts()

    assert len(session.cursors) == _MAX_CATALOG_PAGE_REQUESTS


async def test_listing_restarts_from_the_first_page_on_a_replaced_session() -> None:
    owned_tool = _HTTPMCPTool(name="h", url="http://localhost/mcp")

    def pages(tools: str) -> Callable[[str | None], tuple[list[types.Tool], str | None] | Exception]:
        failures = [anyio.ClosedResourceError()] if tools == "old" else []

        def page(cursor: str | None) -> tuple[list[types.Tool], str | None] | Exception:
            if cursor is None:
                return [_mcp_remote_tool(f"{tools}-1")], "page-2"
            return failures.pop() if failures else ([_mcp_remote_tool(f"{tools}-2")], None)

        return page

    old_session, new_session = _PagedSession(tool_pages=pages("old")), _PagedSession(tool_pages=pages("new"))
    failed_sessions: list[ClientSession | None] = []

    async def reconnect_without_loading(failed_session: ClientSession | None) -> None:
        failed_sessions.append(failed_session)
        owned_tool.session = _as_client_session(cast("Any", new_session))

    owned_tool._reconnect_without_loading = reconnect_without_loading  # type: ignore[method-assign]
    owned_tool.session = _as_client_session(cast("Any", old_session))
    await owned_tool.load_tools()

    assert old_session.cursors == [None, "page-2"]
    assert new_session.cursors == [None, "page-2"]
    assert [function.name for function in owned_tool.functions] == ["new-1", "new-2"]
    assert failed_sessions == [old_session]


# ---------------------------------------------------------------------------
# lifecycle owner task, reconnect wrapping, session-state reset
# ---------------------------------------------------------------------------


class _HandshakeClientSession:
    """``ClientSession`` for tests that patch it where the engine opens a session: the SDK's constructor, the
    context-manager pair and ``initialize``, which answers for a server offering no tools, prompts or logging."""

    def __init__(
        self,
        read_stream: MemoryObjectReceiveStream[SessionMessage | Exception],
        write_stream: MemoryObjectSendStream[SessionMessage],
        read_timeout_seconds: timedelta | None = None,
        sampling_callback: SamplingFnT | None = None,
        elicitation_callback: ElicitationFnT | None = None,
        list_roots_callback: ListRootsFnT | None = None,
        logging_callback: LoggingFnT | None = None,
        message_handler: MessageHandlerFnT | None = None,
        client_info: types.Implementation | None = None,
        *,
        sampling_capabilities: types.SamplingCapability | None = None,
        experimental_task_handlers: ExperimentalTaskHandlers | None = None,
    ) -> None:
        pass

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        pass

    async def initialize(self) -> types.InitializeResult:
        return types.InitializeResult(
            protocolVersion="2024-11-05",
            capabilities=types.ServerCapabilities(),
            serverInfo=types.Implementation(name="handshake", version="0"),
        )


async def test_owned_mcp_close_runs_on_lifecycle_owner_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """Open/close must happen in the same task for anyio cancel scopes."""
    enter_task: asyncio.Task[object] | None = None
    exit_task: asyncio.Task[object] | None = None

    @asynccontextmanager
    async def transport() -> Any:
        nonlocal enter_task, exit_task
        enter_task = asyncio.current_task()
        yield object(), object()
        exit_task = asyncio.current_task()

    class _TaskRecordingTool(MCPTool):
        def get_mcp_client(self) -> Any:
            return transport()

    monkeypatch.setattr("mcp.client.session.ClientSession", _HandshakeClientSession)

    tool = _TaskRecordingTool(name="task-recorder")
    await tool.connect()
    assert enter_task is not None
    assert enter_task is not asyncio.current_task()

    close_task = asyncio.create_task(tool.close())
    await close_task

    assert exit_task is enter_task


async def test_owned_mcp_initialize_declares_no_sampling_capability() -> None:
    """The client answers no sampling requests, so the handshake must not offer sampling to the server."""
    client_send, server_receive = anyio.create_memory_object_stream[SessionMessage](4)
    server_send, client_receive = anyio.create_memory_object_stream[SessionMessage | Exception](4)
    declared: list[types.ClientCapabilities] = []

    async def stub_server() -> None:
        request = (await server_receive.receive()).message.root
        assert isinstance(request, types.JSONRPCRequest)
        assert request.method == "initialize"
        declared.append(types.InitializeRequestParams.model_validate(request.params).capabilities)
        result = types.InitializeResult(
            protocolVersion=types.LATEST_PROTOCOL_VERSION,
            capabilities=types.ServerCapabilities(),
            serverInfo=types.Implementation(name="stub", version="0"),
        )
        response = types.JSONRPCResponse(
            jsonrpc="2.0", id=request.id, result=result.model_dump(by_alias=True, mode="json", exclude_none=True)
        )
        await server_send.send(SessionMessage(types.JSONRPCMessage(response)))
        notification = (await server_receive.receive()).message.root
        assert isinstance(notification, types.JSONRPCNotification)
        assert notification.method == "notifications/initialized"

    @asynccontextmanager
    async def transport() -> Any:
        yield client_receive, client_send

    class _StubServerTool(MCPTool):
        def get_mcp_client(self) -> Any:
            return transport()

    tool = _StubServerTool(name="stub", load_tools=False, load_prompts=False)
    server = asyncio.create_task(stub_server())
    async with client_send, client_receive, server_send, server_receive:
        try:
            await tool.connect()
            await server
        finally:
            await tool.close()
            server.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await server

    assert len(declared) == 1
    assert declared[0].sampling is None


async def test_owned_mcp_initialize_failure_does_not_commit_closed_session(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def transport() -> Any:
        yield object(), object()

    class _FailingHandshake(_HandshakeClientSession):
        async def initialize(self) -> types.InitializeResult:
            raise RuntimeError("init failed")

    class _InitFailTool(MCPTool):
        def get_mcp_client(self) -> Any:
            return transport()

    monkeypatch.setattr("mcp.client.session.ClientSession", _FailingHandshake)

    tool = _InitFailTool(name="init-fail")
    with pytest.raises(Exception, match="init failed"):
        await tool.connect()

    assert tool.session is None
    assert tool.is_connected is False


async def test_concurrent_calls_on_a_dropped_session_reconnect_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calls that all saw one session drop replace it once, then share the replacement."""
    callers = 3
    transports_opened = 0
    sessions: list[_HandshakeClientSession] = []
    all_in_flight = asyncio.Event()
    in_flight = 0

    @asynccontextmanager
    async def transport() -> Any:
        nonlocal transports_opened
        transports_opened += 1
        yield object(), object()

    class _DroppingSession(_HandshakeClientSession):
        async def initialize(self) -> types.InitializeResult:
            sessions.append(self)
            return await super().initialize()

        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any] | None = None,
            read_timeout_seconds: timedelta | None = None,
            progress_callback: Any = None,
            *,
            meta: dict[str, Any] | None = None,
        ) -> types.CallToolResult:
            nonlocal in_flight
            if self is not sessions[0]:
                return _ok_result()
            in_flight += 1
            if in_flight == callers:
                all_in_flight.set()
            await all_in_flight.wait()
            raise anyio.ClosedResourceError

    class _DroppingTool(MCPTool):
        def get_mcp_client(self) -> Any:
            return transport()

    monkeypatch.setattr("mcp.client.session.ClientSession", _DroppingSession)
    tool = _DroppingTool(name="dropping")
    await tool.connect()
    try:
        results = await asyncio.gather(*(tool.call_tool("echo") for _ in range(callers)))
    finally:
        await tool.close()

    assert transports_opened == 2
    assert len(sessions) == 2
    assert [[content.text for content in result] for result in results] == [["ok"]] * callers


async def test_reset_naming_a_replaced_session_keeps_the_replacement(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def transport() -> Any:
        yield object(), object()

    class _Tool(MCPTool):
        def get_mcp_client(self) -> Any:
            return transport()

    monkeypatch.setattr("mcp.client.session.ClientSession", _HandshakeClientSession)
    tool = _Tool(name="t")
    await tool.connect()
    first = tool.session
    try:
        await tool.connect(reset=True, failed_session=first)
        second = tool.session
        await tool.connect(reset=True, failed_session=first)

        assert second is not first
        assert tool.session is second
        await tool.connect(reset=True, failed_session=second)
        assert tool.session is not second
    finally:
        await tool.close()


async def test_load_retries_reconnect_without_recursive_configured_load() -> None:
    """ClosedResourceError during list pagination must not deadlock on the load lock."""
    tool = _HTTPMCPTool(name="h", url="http://localhost/mcp")
    reconnect_calls = 0

    async def reconnect_without_loading(failed_session: ClientSession | None) -> None:
        nonlocal reconnect_calls
        reconnect_calls += 1
        assert tool._function_load_lock.locked()

    tool._reconnect_without_loading = reconnect_without_loading  # type: ignore[method-assign]
    session = _ScriptedClientSession(
        fail_first={"list_tools": [anyio.ClosedResourceError()], "list_prompts": [anyio.ClosedResourceError()]}
    )
    tool.session = _as_client_session(session)

    await asyncio.wait_for(tool.load_tools(), timeout=20.0)
    await asyncio.wait_for(tool.load_prompts(), timeout=20.0)

    assert reconnect_calls == 2
    assert session.requests == ["list_tools", "list_tools", "list_prompts", "list_prompts"]


async def test_load_reconnect_failure_is_wrapped() -> None:
    tool = _HTTPMCPTool(name="h", url="http://localhost/mcp")

    async def reconnect_without_loading(failed_session: ClientSession | None) -> None:
        raise RuntimeError("reconnect failed")

    tool._reconnect_without_loading = reconnect_without_loading  # type: ignore[method-assign]
    tool.session = _as_client_session(_ScriptedClientSession(fail_first={"list_tools": [anyio.ClosedResourceError()]}))

    with pytest.raises(Exception) as info:
        await tool.load_tools()

    assert type(info.value).__name__ == "ToolExecutionException"
    assert "Failed to reconnect to MCP server." in str(info.value)


async def test_get_prompt_reconnect_failure_is_wrapped() -> None:
    tool = _HTTPMCPTool(name="h", url="http://localhost/mcp")
    resets: list[bool] = []

    async def connect(*, reset: bool = False, failed_session: ClientSession | None = None) -> None:
        resets.append(reset)
        raise RuntimeError("reconnect failed")

    tool.connect = connect  # type: ignore[method-assign]
    tool.session = _as_client_session(_ScriptedClientSession(get_prompt=anyio.ClosedResourceError()))

    with pytest.raises(Exception) as info:
        await tool.get_prompt("p")

    assert type(info.value).__name__ == "ToolExecutionException"
    assert "Failed to reconnect to MCP server." in str(info.value)
    assert resets == [True]


def test_server_instructions_reset_on_session_state_reset() -> None:
    """_server_instructions is cleared after _reset_session_state."""
    tool = MCPTool(name="test")
    tool._server_instructions = "Some instructions"
    assert tool._server_instructions == "Some instructions"

    tool._reset_session_state()
    assert tool._server_instructions is None
