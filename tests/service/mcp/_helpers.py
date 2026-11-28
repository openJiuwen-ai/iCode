# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared fakes and factories for the MCP adapter, transport, and owned-engine tests."""

from __future__ import annotations

import builtins
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast
from unittest.mock import AsyncMock

from mcp import ClientSession, types
from mcp.shared.session import ProgressFnT

from chrys.kernel import FunctionTool as ChrysFunctionTool
from chrys.service.mcp.owned import MCPTool, _mcp_config_names_for


async def _remote_tool(ctx, value: str = "") -> str:
    return value or "ok"


def _function_tool(name: str = "remote", *, remote_name: str | None = None) -> ChrysFunctionTool:
    """Engine-domain function tool stand-in.

    ``remote_name`` also stamps the owned catalog's remote/normalized-name
    markers, which the connection cache and progressive exposure read.
    """
    additional_properties = None
    if remote_name is not None:
        additional_properties = {
            "_mcp_remote_name": remote_name,
            "_mcp_normalized_name": remote_name.replace("/", "-"),
        }
    return ChrysFunctionTool(
        func=_remote_tool,
        name=name,
        description="Remote tool",
        input_model={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
        additional_properties=additional_properties,
    )


def _mcp_remote_tool(
    name: str,
    *,
    meta: dict[str, Any] | None = None,
    input_schema: dict[str, Any] | None = None,
) -> types.Tool:
    return types.Tool(
        name=name,
        description="Remote tool",
        inputSchema=input_schema if input_schema is not None else {"type": "object", "properties": {}},
        _meta=meta,
    )


def _mcp_remote_prompt(name: str) -> types.Prompt:
    return types.Prompt(name=name, description="Remote prompt", arguments=[])


async def _load_fake_remote_tools(tool: MCPTool, *remote_tools: types.Tool) -> None:
    tool.session = _as_client_session(_ScriptedClientSession(tools=remote_tools))
    await tool.load_tools()


@dataclass(frozen=True, slots=True)
class _ToolCall:
    name: str
    arguments: dict[str, Any] | None
    meta: dict[str, Any] | None


_SESSION_REQUESTS = frozenset({"send_ping", "list_tools", "list_prompts", "call_tool", "get_prompt"})


class _ScriptedClientSession:
    """The ``ClientSession`` requests a connected owned engine makes, with the SDK's signatures and types.

    The lists answer with the scripted tools and prompts. ``call_tool`` and ``get_prompt`` answer
    with their scripted result or raise their scripted exception; one the test left unscripted
    fails it. ``fail_first`` names requests whose first calls raise the given exceptions instead,
    one each, before they answer. ``requests`` logs every request by method name, and
    ``tool_calls`` what each ``call_tool`` asked for.
    """

    def __init__(
        self,
        *,
        tools: Sequence[types.Tool] = (),
        prompts: Sequence[types.Prompt] = (),
        call_tool: types.CallToolResult | Exception | None = None,
        get_prompt: types.GetPromptResult | Exception | None = None,
        fail_first: Mapping[str, Sequence[Exception]] | None = None,
    ) -> None:
        failures = dict(fail_first or {})
        if unknown := failures.keys() - _SESSION_REQUESTS:
            raise ValueError(f"no such session request: {sorted(unknown)}")
        self._tools = list(tools)
        self._prompts = list(prompts)
        self._call_tool = call_tool
        self._get_prompt = get_prompt
        self._failures = {request: list(errors) for request, errors in failures.items()}
        self.requests: list[str] = []
        self.tool_calls: list[_ToolCall] = []

    def _begin(self, request: str) -> None:
        self.requests.append(request)
        if pending := self._failures.get(request):
            raise pending.pop(0)

    async def send_ping(self) -> types.EmptyResult:
        self._begin("send_ping")
        return types.EmptyResult()

    async def list_tools(
        self, cursor: str | None = None, *, params: types.PaginatedRequestParams | None = None
    ) -> types.ListToolsResult:
        self._begin("list_tools")
        return types.ListToolsResult(tools=self._tools)

    async def list_prompts(
        self, cursor: str | None = None, *, params: types.PaginatedRequestParams | None = None
    ) -> types.ListPromptsResult:
        self._begin("list_prompts")
        return types.ListPromptsResult(prompts=self._prompts)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        read_timeout_seconds: timedelta | None = None,
        progress_callback: ProgressFnT | None = None,
        *,
        meta: dict[str, Any] | None = None,
    ) -> types.CallToolResult:
        self._begin("call_tool")
        self.tool_calls.append(_ToolCall(name, arguments, meta))
        return _scripted_answer(self._call_tool, "call_tool")

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> types.GetPromptResult:
        self._begin("get_prompt")
        return _scripted_answer(self._get_prompt, "get_prompt")


def _scripted_answer[T](answer: T | Exception | None, request: str) -> T:
    if answer is None:
        raise AssertionError(f"the test scripted no {request} answer")
    if isinstance(answer, Exception):
        raise answer
    return answer


def _as_client_session(session: _ScriptedClientSession) -> ClientSession:
    """The scripted session in the engine's ``ClientSession`` slot, which it fills structurally."""
    return cast("ClientSession", session)


class _FakeConnectionTool:
    """What ``_create_mcp_tool`` returns on the plain connect / test-connection paths.

    Deliberately carries nothing but ``functions`` and the context-manager pair: no
    ``name``, ``allowed_tools``, ``request_timeout`` or ``dropped_banner_lines``. Those
    paths must not reach for them, and the ``AttributeError`` if they ever do is the
    assertion. ``__aenter__``/``__aexit__`` are per-instance ``AsyncMock``s so
    ``fake.__aenter__.assert_awaited_once()`` works.
    """

    def __init__(self, *, functions: list[Any] | None = None, enter_error: BaseException | None = None) -> None:
        self.functions: list[Any] = functions if functions is not None else []
        self.__aenter__ = AsyncMock(side_effect=enter_error) if enter_error else AsyncMock(return_value=self)
        self.__aexit__ = AsyncMock(return_value=None)


class _FakeBannerConnectionTool:
    """A connect stand-in for the failure-wrapping path, which does read banners and the timeout.

    Separate from :class:`_FakeConnectionTool` precisely because only this path reads
    ``request_timeout`` and ``dropped_banner_lines``.
    """

    def __init__(self, *, enter_error: BaseException, request_timeout: float, banner_lines: list[str]) -> None:
        self.functions: list[Any] = []
        self.request_timeout = request_timeout
        self.dropped_banner_lines = list(banner_lines)
        self.__aenter__ = AsyncMock(side_effect=enter_error)
        self.__aexit__ = AsyncMock(return_value=None)


class _FakeDisconnectTool:
    """An already-registered server for the ``disconnect``/``disconnect_all`` tests.

    These place it straight into ``adapter._servers``, so the connection surface it
    exposes is ``__aexit__`` alone: no ``__aenter__`` and no catalog, so a teardown path
    that tries to enter it or read its functions fails instead of quietly succeeding.
    ``exit_count`` is the test-side ledger of how often teardown released it, which is
    what lets a caller pin exactly-once release rather than merely "was released".
    """

    def __init__(self, *, exit_error: BaseException | None = None) -> None:
        self.exit_count = 0
        self._exit_error = exit_error

    async def __aexit__(self, *args: object) -> None:
        self.exit_count += 1
        if self._exit_error is not None:
            raise self._exit_error


class _FakeCatalogTool:
    """Progressive-disclosure stand-in: the catalog surface the exposure reads, and nothing else.

    ``functions`` is required and read-only — the exposure derives its control tools from
    it and must never assign it — and ``allowed_tools`` filters it the way the owned engine
    does. No enter/exit counters and no error injection: this fake is never used to test a
    connection failure.
    """

    def __init__(self, functions: list[Any], *, allowed_tools: list[str] | None = None) -> None:
        self.name = "srv"
        self._functions = functions
        self.allowed_tools = allowed_tools
        self.request_timeout = 30
        self.dropped_banner_lines: list[str] = []

    @property
    def functions(self) -> list[Any]:
        if self.allowed_tools is None:
            return self._functions
        allowed_names = set(self.allowed_tools)
        return [tool for tool in self._functions if not allowed_names.isdisjoint(_mcp_config_names_for(tool))]

    async def __aenter__(self) -> _FakeCatalogTool:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def block_import(module: str, symbol: str) -> Any:
    """An ``__import__`` replacement that fails ``from <module> import <symbol>``.

    Simulates the SDK moving one private symbol while every other import keeps
    working, which is what drives the adapter's ImportError fallbacks.
    """
    real_import = builtins.__import__

    def guarded_import(
        name: str,
        globals_: dict[str, Any] | None = None,
        locals_: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == module and symbol in fromlist:
            raise ImportError(f"{module}.{symbol} moved")
        return real_import(name, globals_, locals_, fromlist, level)

    return guarded_import
