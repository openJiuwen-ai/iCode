# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""How a session reaches a browser: launch one, or attach to an existing one.

The transport owns only connection lifecycle. Actions, snapshots and the
network policy stay in ``BrowserSession``; this keeps the attach mode from
forking the core.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import urllib.request
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .config import BrowserConfig
from .errors import BrowserError, Unavailable

Resolver = Callable[[str], Awaitable[str]]


class Transport(Protocol):
    """What a session needs from whatever owns the browser connection."""

    owns_browser: bool

    browser_commands: bool
    """Whether browser-level Target commands (getTargets/activateTarget) work."""

    async def start(self) -> tuple[Any, Any | None]: ...

    async def stop(self, client: Any | None, child: Any | None, *, target_id: str | None) -> None: ...


async def resolve_ws_endpoint(url: str) -> str:
    """Turn an HTTP DevTools endpoint into its browser WebSocket URL."""
    if url.startswith(("ws://", "wss://")):
        return url

    def fetch() -> str:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url.rstrip("/") + "/json/version", timeout=5) as response:
            payload = json.load(response)
        websocket = str(payload.get("webSocketDebuggerUrl") or "")
        if not websocket:
            raise ValueError("the endpoint has no webSocketDebuggerUrl")
        return websocket

    try:
        return await asyncio.to_thread(fetch)
    except Exception as exc:
        raise Unavailable(f"cannot resolve CDP endpoint {url}: {exc}") from exc


class OwnedTransport:
    """The current behavior: launch a dedicated Chrome and own its process."""

    owns_browser = True
    browser_commands = True

    def __init__(
        self,
        cfg: BrowserConfig,
        client_factory: Callable[[str], Awaitable[Any]],
        launcher_factory: Callable[[BrowserConfig, Any], Awaitable[Any]],
    ) -> None:
        self._cfg = cfg
        self._client_factory = client_factory
        self._launcher_factory = launcher_factory

    async def start(self) -> tuple[Any, Any | None]:
        result = await self._launcher_factory(self._cfg, self._cfg.state_dir)
        try:
            client = await self._client_factory(result.browser_ws)
        except BaseException:
            if result.child is not None:
                with contextlib.suppress(Exception):
                    await result.child.terminate_tree()
            raise
        return client, result.child

    async def stop(self, client: Any | None, child: Any | None, *, target_id: str | None) -> None:
        graceful_sent = False
        if client is not None:
            with contextlib.suppress(Exception):
                await client.send("Browser.close", timeout=2.0)
                graceful_sent = True
            with contextlib.suppress(Exception):
                await client.aclose()
        if child is not None:
            if graceful_sent:
                wait_for_exit = getattr(child, "wait_for_exit", None)
                if callable(wait_for_exit):
                    with contextlib.suppress(Exception):
                        await wait_for_exit()
            with contextlib.suppress(Exception):
                await child.terminate_tree()


class AttachedTransport:
    """Attach to a browser the user already runs; never close it or its tabs."""

    owns_browser = False
    browser_commands = True

    def __init__(
        self,
        cfg: BrowserConfig,
        client_factory: Callable[[str], Awaitable[Any]],
        resolver: Resolver = resolve_ws_endpoint,
    ) -> None:
        self._cfg = cfg
        self._client_factory = client_factory
        self._resolver = resolver

    async def start(self) -> tuple[Any, None]:
        if self._cfg.chrome_devtools:
            from .bridge.install import chrome_devtools_ws

            async def connect() -> Any:
                endpoint = await asyncio.to_thread(chrome_devtools_ws)
                client = await self._client_factory(endpoint)
                try:
                    # Chrome may complete the WebSocket upgrade before the user approves CDP.
                    await client.send("Browser.getVersion", timeout=60)
                except BaseException:
                    with contextlib.suppress(Exception):
                        await client.aclose()
                    raise
                return client

            try:
                return await asyncio.wait_for(connect(), timeout=60), None
            except (OSError, RuntimeError, asyncio.TimeoutError, BrowserError) as exc:  # noqa: UP041 - Python 3.10
                raise Unavailable(
                    "Could not connect to your Chrome profile. Open Chrome and approve its connection prompt, "
                    "or use /browser in iCode (icode browser connect --chrome in a terminal) to set it up again."
                ) from exc
        if not self._cfg.cdp_url:
            raise Unavailable("attached mode requires ICODE_BROWSER_CDP_URL")
        websocket = await self._resolver(self._cfg.cdp_url)
        return await self._client_factory(websocket), None

    async def stop(self, client: Any | None, child: Any | None, *, target_id: str | None) -> None:
        if client is not None:
            if target_id:
                # Close only the tab this session created, never the user's other tabs
                # or the browser itself.
                with contextlib.suppress(Exception):
                    await client.send("Target.closeTarget", {"targetId": target_id}, timeout=5.0)
            with contextlib.suppress(Exception):
                await client.aclose()
