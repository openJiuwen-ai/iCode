# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Raw response streams, socket-level network doubles and build assembly for web tool tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import httpx

from chrys.foundation.config.settings import Settings
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.service.profiles.agents.schema import ToolsConfig
from chrys.service.tools.builtins.web.build import WebTools, assemble_web_tools
from chrys.service.tools.builtins.web.config import WebFetchConfigPatch, WebSearchConfigPatch
from chrys.service.tools.packs import AssembledTools, ToolBuild, assemble_tools


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


SocketHandler = Callable[[httpx.Request], Awaitable[httpx.Response]]


@dataclass(frozen=True)
class SocketNetwork:
    """The two replaced seams beneath ``ValidatedAsyncTransport``."""

    resolve: MagicMock
    send: MagicMock


@contextmanager
def socket_network(handle: SocketHandler, addresses: Mapping[str, str | Exception]) -> Iterator[SocketNetwork]:
    """Resolve fixed names and answer at the socket layer, beneath the validated transport.

    Every request still passes through ``ValidatedAsyncTransport``'s destination
    checks; only DNS and the inner ``httpx.AsyncHTTPTransport`` are replaced. The
    handler sees the pinned request, whose ``Host`` header names the logical host.
    An exception in ``addresses`` is the lookup's failure for that name.
    """

    async def resolve(host: str, port: int) -> tuple[str, ...]:
        address = addresses[host]
        if isinstance(address, Exception):
            raise address
        return (address,)

    async def send(_transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        return await handle(request)

    with (
        patch(
            "chrys.service.tools.builtins.web.http.resolve_addresses", autospec=True, side_effect=resolve
        ) as resolver,
        patch.object(httpx.AsyncHTTPTransport, "handle_async_request", autospec=True, side_effect=send) as sender,
    ):
        yield SocketNetwork(resolver, sender)


def assemble(
    categories: list[str],
    search: WebSearchConfigPatch | None = None,
    fetch: WebFetchConfigPatch | None = None,
    *,
    settings: Settings | None = None,
    chat_options: dict[str, Any] | None = None,
) -> WebTools:
    """Assemble web tools as an agent build does, for agent ``Test`` on model profile ``model``."""
    return assemble_web_tools(
        categories,
        settings or Settings(),
        search,
        fetch,
        chat_options=chat_options,
        agent="Test",
        model_profile_id="model",
        session_id="s",
    )


async def assemble_agent_tools(
    categories: list[str],
    search: WebSearchConfigPatch | None = None,
    fetch: WebFetchConfigPatch | None = None,
    *,
    chat_options: dict[str, Any] | None = None,
) -> AssembledTools:
    """Assemble every local tool of an agent build, for the same agent and model profile as :func:`assemble`."""
    return await assemble_tools(
        ToolBuild(
            tools=ToolsConfig(builtins=categories, web_search=search, web_fetch=fetch),
            agent_name="Test",
            runtime=SessionEnvironment.capture(),
            settings=Settings(),
            session_id="s",
            model_profile_id="model",
            chat_options=chat_options,
        )
    )
