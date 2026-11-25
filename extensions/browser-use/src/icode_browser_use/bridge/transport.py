# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport that runs the bridge server and waits for Chrome's native host."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import BrowserConfig
from ..errors import BrowserError, ConnectionClosed, Unavailable
from .protocol import PROTOCOL_VERSION
from .server import BridgeClient, BridgeServer


class BridgeTransport:
    """Connect through the extension bridge; never close the user's browser."""

    owns_browser = False
    browser_commands = False

    def __init__(
        self,
        cfg: BrowserConfig,
        *,
        server_factory: Callable[[BrowserConfig], Any] | None = None,
    ) -> None:
        self._cfg = cfg
        self._server_factory = server_factory or self._default_server
        self._server: Any | None = None
        self._stale_target: str | None = None

    def _default_server(self, cfg: BrowserConfig) -> BridgeServer:
        bridge_file = Path(cfg.bridge_file) if cfg.bridge_file else cfg.state_dir / "bridge.json"
        return BridgeServer(bridge_file)

    async def start(self) -> tuple[Any, None]:
        self._server = self._server_factory(self._cfg)
        client: Any | None = None
        try:
            await self._server.start()
            client = await self._server.wait_for_client(timeout=self._server.wait_timeout(self._cfg.bridge_wait))
            try:
                hello = await client.send("Bridge.hello", {"protocol": PROTOCOL_VERSION}, timeout=5.0)
            except (BrowserError, ConnectionClosed) as exc:
                raise Unavailable(
                    "bridge handshake failed; reload the extension in chrome://extensions "
                    f"(plugin protocol {PROTOCOL_VERSION}): {exc}"
                ) from exc
            if not isinstance(hello, dict) or hello.get("protocol") != PROTOCOL_VERSION:
                version = hello.get("extensionVersion") if isinstance(hello, dict) else None
                browser = hello.get("browser") if isinstance(hello, dict) else None
                raise Unavailable(
                    f"bridge protocol mismatch: plugin={PROTOCOL_VERSION}, "
                    f"extension={hello.get('protocol') if isinstance(hello, dict) else 'unknown'}, "
                    f"extensionVersion={version or 'unknown'}, browser={browser or 'unknown'}; "
                    "reload the extension or re-run install"
                )
            capabilities = [str(item) for item in hello.get("capabilities", [])]
            if self._stale_target is not None:
                with contextlib.suppress(Exception):
                    await client.send("Target.closeTarget", {"targetId": self._stale_target}, timeout=5.0)
                self._stale_target = None
            try:
                configured = await client.send(
                    "Bridge.configure",
                    {
                        "allowedDomains": list(self._cfg.allowed_domains),
                        "allowedOrigins": list(self._cfg.allowed_origins),
                        "publicWeb": self._cfg.public_web,
                    },
                    timeout=5.0,
                )
                if isinstance(configured, dict):
                    capabilities.extend(str(item) for item in configured.get("capabilities", []))
            except Exception as exc:  # noqa: BLE001 - old extensions still work with the popup
                from ..logging import log

                log(f"bridge configure failed: {exc!r}")
            if "ping" in capabilities and isinstance(client, BridgeClient):
                client.start_heartbeat()
            client.capabilities = frozenset(capabilities)  # the session reads what the extension supports
            return client, None
        except BaseException:
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.aclose()
            if self._server is not None:
                with contextlib.suppress(Exception):
                    await self._server.aclose()
                self._server = None
            raise

    async def stop(self, client: Any | None, child: Any | None, *, target_id: str | None) -> None:
        if client is not None:
            if target_id:
                with contextlib.suppress(Exception):
                    await client.send("Target.closeTarget", {"targetId": target_id}, timeout=5.0)
            with contextlib.suppress(Exception):
                await client.aclose()
        elif target_id:
            self._stale_target = target_id
        if self._server is not None:
            with contextlib.suppress(Exception):
                await self._server.aclose()
            self._server = None
