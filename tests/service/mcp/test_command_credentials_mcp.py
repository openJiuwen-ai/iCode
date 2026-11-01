# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``{{!command}}`` values in an MCP server's HTTP headers and STDIO environment."""

from __future__ import annotations

import asyncio
import base64
import os
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from mcp.client.stdio import StdioServerParameters

import chrys.service.command_credentials as credentials_module
from chrys.foundation.util.env_templates import EnvVarResolutionError, resolve_env_templates
from chrys.service.command_credentials import _CommandFailedError, refresh
from chrys.service.mcp._connection import _create_mcp_tool
from chrys.service.mcp._http_transport import _HTTPMCPTool
from chrys.service.mcp._stdio_transport import tolerant_stdio_client
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.mcp.cache import mcp_config_cache_key, resolve_mcp_effective_config
from chrys.service.mcp.owned import MCPTool
from chrys.service.profiles.agents.schema import MCPServerConfig
from tests.support.waiting import ENGINE_TURN_TIMEOUT

_URL = "https://origin.test/mcp"
_COMMAND = "secret-tool get"
_PLACEHOLDER = f"{{{{!{_COMMAND}}}}}"


@pytest.fixture(autouse=True)
def _no_tokens_from_other_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials_module, "_credentials", {})


class _Commands:
    """Stands in for running a command: answers from a script and counts the runs."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.outcomes: list[str | Exception] = []
        self.runs = 0
        monkeypatch.setattr(credentials_module, "_run_command", self._run)

    async def _run(self, _command: str) -> str:
        self.runs += 1
        outcome = self.outcomes[0] if len(self.outcomes) == 1 else self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> _Commands:
    return _Commands(monkeypatch)


def _http_config(*, url: str = _URL, **fields: Any) -> MCPServerConfig:
    return MCPServerConfig(name="srv", transport="http", url=url, **fields)


@asynccontextmanager
async def _client_of(
    config: MCPServerConfig, handler: Callable[[httpx.Request], httpx.Response]
) -> AsyncIterator[httpx.AsyncClient]:
    """The HTTP client the server's own tool builds, answering through *handler*."""
    real_client = httpx.AsyncClient

    def on_mock_transport(
        *, follow_redirects: bool, timeout: httpx.Timeout, verify: bool, auth: httpx.Auth | None = None
    ) -> httpx.AsyncClient:
        return real_client(
            transport=httpx.MockTransport(handler),
            follow_redirects=follow_redirects,
            timeout=timeout,
            verify=verify,
            auth=auth,
        )

    tool = _create_mcp_tool(config)
    assert isinstance(tool, _HTTPMCPTool)
    with (
        patch("httpx.AsyncClient", side_effect=on_mock_transport),
        patch("chrys.service.mcp._http_transport._chrys_streamable_http_client", return_value=object()),
    ):
        tool.get_mcp_client()
    client = tool._owned_httpx_client
    assert client is not None
    try:
        yield client
    finally:
        await tool._close_owned_httpx_client()


def _accepting(token: str, seen: list[str | None]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        sent = request.headers.get("Authorization")
        seen.append(sent)
        return httpx.Response(200 if sent == f"Bearer {token}" else 401)

    return handler


# ── HTTP headers ─────────────────────────────────────────────────────


async def test_a_refused_header_goes_out_again_with_a_new_token(commands: _Commands) -> None:
    commands.outcomes = ["old", "new"]
    seen: list[str | None] = []
    config = _http_config(headers={"Authorization": f"Bearer {_PLACEHOLDER}"})

    async with _client_of(config, _accepting("new", seen)) as client:
        response = await client.post(_URL, json={})

    assert response.status_code == 200
    assert seen == ["Bearer old", "Bearer new"]


async def test_a_header_token_past_the_interval_is_replaced_before_the_next_request(commands: _Commands) -> None:
    commands.outcomes = ["old", "new"]
    seen: list[str | None] = []

    def accept_anything(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200)

    config = _http_config(headers={"Authorization": f"Bearer {_PLACEHOLDER}"})

    async with _client_of(config, accept_anything) as client:
        await client.post(_URL, json={})
        await client.post(_URL, json={})
        for credential in credentials_module._credentials.values():
            credential.fetched_at -= 10 * 24 * 3600
        await client.post(_URL, json={})

    assert seen == ["Bearer old", "Bearer old", "Bearer new"]
    assert commands.runs == 2


async def test_a_command_finishes_before_the_connection_starts_counting_its_time(commands: _Commands) -> None:
    commands.outcomes = ["good"]
    tool = _create_mcp_tool(_http_config(headers={"X-T": _PLACEHOLDER}, request_timeout=1))
    runs_when_connecting: list[int] = []

    async def connect(_tool: MCPTool) -> None:
        runs_when_connecting.append(commands.runs)

    with patch.object(MCPTool, "__aenter__", connect):
        await tool.__aenter__()
    assert isinstance(tool, _HTTPMCPTool)
    await tool._close_owned_httpx_client()

    assert runs_when_connecting == [1]


async def test_a_cross_origin_request_gets_no_token(commands: _Commands) -> None:
    commands.outcomes = ["good"]
    seen: list[str | None] = []
    config = _http_config(headers={"Authorization": f"Bearer {_PLACEHOLDER}"})

    async with _client_of(config, _accepting("good", seen)) as client:
        await client.post(_URL, json={})
        await client.post("https://foreign.test/mcp", json={})

    assert seen == ["Bearer good", None]


async def test_a_value_the_server_sent_never_comes_back_as_a_token(commands: _Commands) -> None:
    """A session id shaped like the handle of another command, a model's say, stays what it is."""
    commands.outcomes = ["good"]
    other = resolve_env_templates("{{!model-key-tool}}", location="a model profile")
    await refresh([other])
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Mcp-Session-Id"))
        return httpx.Response(200)

    config = _http_config(headers={"X-T": _PLACEHOLDER})
    async with _client_of(config, handler) as client:
        await client.post(_URL, headers={"Mcp-Session-Id": other}, json={})
        await client.delete(_URL, headers={"Mcp-Session-Id": other})

    assert seen == [other, other]


async def test_ending_a_session_runs_no_command(commands: _Commands) -> None:
    commands.outcomes = ["old", "new"]
    seen: list[str | None] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-T"))
        return httpx.Response(200 if request.method == "POST" else 401)

    async with _client_of(_http_config(headers={"X-T": _PLACEHOLDER}), refuse) as client:
        await client.post(_URL, json={})
        for credential in credentials_module._credentials.values():
            credential.fetched_at -= 10 * 24 * 3600
        await client.delete(_URL)

    assert seen == ["old", "old"]
    assert commands.runs == 1


async def test_a_header_the_transport_would_refuse_fails_before_it_can_quote_a_token(commands: _Commands) -> None:
    commands.outcomes = ["the-token"]
    config = _http_config(headers={"Authorization": f"Bearer {_PLACEHOLDER} "})
    adapter = MCPAdapter()

    tools = await asyncio.wait_for(adapter.connect_all([config]), timeout=ENGINE_TURN_TIMEOUT)

    assert tools == []
    shown = str(adapter.failures["srv"])
    assert "Authorization" in shown
    assert "the-token" not in shown
    assert commands.runs == 0


async def test_literal_headers_stay_as_written_and_run_nothing(commands: _Commands) -> None:
    """An ACP client's headers are literal, even one that matches a profile's command."""
    commands.outcomes = ["must-not-appear"]
    handle = resolve_env_templates(_PLACEHOLDER, location="a profile")
    await refresh([handle])
    for credential in credentials_module._credentials.values():
        credential.fetched_at -= 10 * 24 * 3600
    seen: list[dict[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"handle": request.headers.get("X-Handle"), "written": request.headers.get("X-Written")})
        return httpx.Response(401)

    config = _http_config(headers={"X-Handle": handle, "X-Written": _PLACEHOLDER}, resolve_header_templates=False)

    async with _client_of(config, handler) as client:
        assert client.auth is None
        await client.post(_URL, json={})

    tool = _create_mcp_tool(config)
    assert isinstance(tool, _HTTPMCPTool)

    async def connect(_tool: MCPTool) -> None:
        return None

    with patch.object(MCPTool, "__aenter__", connect):
        await tool.__aenter__()
    await tool._close_owned_httpx_client()

    assert seen == [{"handle": handle, "written": _PLACEHOLDER}]
    assert commands.runs == 1


async def test_a_server_without_commands_keeps_the_credentials_in_its_url() -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        return httpx.Response(200)

    url = "https://user:pass@origin.test/mcp"
    async with _client_of(_http_config(url=url, headers={"X-Plain": "plain"}), handler) as client:
        assert client.auth is None
        await client.post(url, json={})

    assert seen == [f"Basic {base64.b64encode(b'user:pass').decode()}"]


@pytest.mark.parametrize(
    "config",
    [
        _http_config(headers={"Authorization": f"Bearer {_PLACEHOLDER}"}),
        MCPServerConfig(name="srv", transport="stdio", command="tool", env={"TOKEN": _PLACEHOLDER}),
    ],
    ids=["http", "stdio"],
)
async def test_a_new_token_keeps_the_shared_connection(config: MCPServerConfig, commands: _Commands) -> None:
    commands.outcomes = ["first", "second"]
    handle = resolve_env_templates(_PLACEHOLDER, location="test")

    await refresh([handle])
    before = mcp_config_cache_key(config, stdio_cwd="/")
    await refresh([handle])

    assert mcp_config_cache_key(config, stdio_cwd="/") == before
    assert commands.runs == 2


# ── STDIO environment ────────────────────────────────────────────────


async def _token_a_launch_sees(handle: str) -> str:
    """Start a real child that reports the ``TOKEN`` it was given as a JSON-RPC method name."""
    code = (
        "import json, os\n"
        "print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': os.environ['TOKEN']}), flush=True)\n"
        "input()\n"
    )
    server = StdioServerParameters(command=sys.executable, args=["-c", code], env={"TOKEN": handle})
    async with tolerant_stdio_client(server) as (read_stream, _write_stream):
        message = await asyncio.wait_for(read_stream.receive(), timeout=ENGINE_TURN_TIMEOUT)
    return message.message.root.method


async def test_each_launch_of_a_stdio_server_gets_a_token_just_fetched(commands: _Commands) -> None:
    commands.outcomes = ["first", "second"]
    handle = resolve_env_templates(_PLACEHOLDER, location="test")
    before = dict(os.environ)

    assert await _token_a_launch_sees(handle) == "first"
    assert await _token_a_launch_sees(handle) == "second"

    assert dict(os.environ) == before


async def test_a_stdio_server_gets_tokens_only_for_its_own_settings(
    commands: _Commands, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands.outcomes = ["own token"]
    monkeypatch.setenv("INHERITED", resolve_env_templates("{{!another setting's command}}", location="test"))
    code = (
        "import json, os\n"
        "seen = os.environ['TOKEN'] + '/' + os.environ.get('INHERITED', 'not inherited')\n"
        "print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': seen}), flush=True)\n"
        "input()\n"
    )
    config = MCPServerConfig(name="srv", transport="stdio", command="tool", env={"TOKEN": _PLACEHOLDER})
    effective_env = resolve_mcp_effective_config(config, stdio_cwd=os.getcwd()).stdio_env

    server = StdioServerParameters(command=sys.executable, args=["-c", code], env=effective_env)
    async with tolerant_stdio_client(server, inherit_env=False) as (read_stream, _write_stream):
        message = await asyncio.wait_for(read_stream.receive(), timeout=ENGINE_TURN_TIMEOUT)

    assert message.message.root.method == "own token/not inherited"
    assert commands.runs == 1


async def test_a_stdio_launch_whose_command_fails_keeps_the_last_token(commands: _Commands) -> None:
    commands.outcomes = ["first", _CommandFailedError("exited with code 3")]
    handle = resolve_env_templates(_PLACEHOLDER, location="test")

    assert await _token_a_launch_sees(handle) == "first"
    assert await _token_a_launch_sees(handle) == "first"


async def test_a_stdio_launch_whose_command_never_worked_starts_nothing(commands: _Commands) -> None:
    commands.outcomes = [_CommandFailedError("exited with code 3")]
    handle = resolve_env_templates(_PLACEHOLDER, location="test")
    server = StdioServerParameters(command="must-not-start", args=[], env={"TOKEN": handle})

    with (
        patch("mcp.client.stdio._create_platform_compatible_process", side_effect=AssertionError) as spawn,
        pytest.raises(EnvVarResolutionError, match="exited with code 3"),
    ):
        async with tolerant_stdio_client(server):
            pass

    spawn.assert_not_called()


# ── A command that never worked ──────────────────────────────────────


def _timed_out() -> _CommandFailedError:
    """What the real run raises: its TimeoutError hangs off it."""
    failure = _CommandFailedError("timed out after 30 seconds")
    failure.__cause__ = TimeoutError()
    return failure


@pytest.mark.parametrize(
    "config",
    [
        MCPServerConfig(name="srv", transport="http", url="http://127.0.0.1:9/mcp", headers={"X-T": _PLACEHOLDER}),
        MCPServerConfig(name="srv", transport="stdio", command=sys.executable, env={"TOKEN": _PLACEHOLDER}),
    ],
    ids=["http", "stdio"],
)
@pytest.mark.parametrize(
    ("failure", "reason"),
    [(_CommandFailedError("exited with code 3"), "exited with code 3"), (_timed_out(), "timed out after 30 seconds")],
    ids=["exit_code", "timeout"],
)
async def test_the_server_fails_to_connect_naming_the_setting_and_not_the_command(
    config: MCPServerConfig, failure: _CommandFailedError, reason: str, commands: _Commands
) -> None:
    commands.outcomes = [failure]
    adapter = MCPAdapter()

    tools = await asyncio.wait_for(adapter.connect_all([config]), timeout=ENGINE_TURN_TIMEOUT)

    assert tools == []
    shown = str(adapter.failures["srv"])
    assert reason in shown
    assert "MCP server 'srv'" in shown
    assert "did not complete" not in shown
    assert _COMMAND not in shown
