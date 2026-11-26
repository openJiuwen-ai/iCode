# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Client side of the persistent browser Broker."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
from types import SimpleNamespace
from typing import Any

from ._proc import pid_alive, rotate_log_if_needed, windows_hidden_kwargs
from .bridge.protocol import MAX_TCP_MESSAGE_BYTES, encode_message, read_async_frame
from .config import BrowserConfig
from .distribution import broker_command
from .errors import BrowserError, ConnectionClosed, Unavailable


def _requested_wait_seconds(fields: dict[str, Any]) -> float:
    """A tool's own ``timeout_ms`` or ``expect_timeout_ms``: the read must outlast it."""
    arguments = fields.get("arguments")
    if not isinstance(arguments, dict):
        return 0.0
    total_ms = 0.0
    for key in ("timeout_ms", "expect_timeout_ms"):
        raw = arguments.get(key)
        if not isinstance(raw, bool) and isinstance(raw, (int, float)):
            total_ms += max(0.0, min(float(raw), 600_000.0))
    return min(total_ms, 600_000.0) / 1000.0


class BrokerCallUnknown(BrowserError):
    """The request may have executed but its response was lost."""

    code = "browser_unknown"


class _BrokerNotRunning(Unavailable):
    """Internal marker: no live Broker process was found at discovery time."""

    def __init__(self, message: str, *, details: str = "", blocked_origin: str = "") -> None:
        super().__init__(message, details=details, blocked_origin=blocked_origin, pre_dispatch=True)


class BrokerSession:
    """A BrowserSession-shaped facade over a Broker lease (extension adapter)."""

    def __init__(
        self,
        cfg: BrowserConfig,
        *,
        owner: str,
        session_id: str,
    ) -> None:
        self.client = BrokerClient(cfg, owner=owner, session_id=session_id)
        self.cfg = cfg
        self.state = "ready"
        self.snapshot = SimpleNamespace(url="", title="")

    async def call_tool(self, tool: str, arguments: Any) -> list[dict[str, Any]]:
        blocks = await self.client.call(tool, arguments)
        self.snapshot.url = self.client.last_url
        self.snapshot.title = self.client.last_title
        return blocks

    async def list_pages(self) -> list[SimpleNamespace]:
        """Refresh owned tab metadata from the Broker without spawning or launching Chrome."""
        return await self.client.list_pages()

    def mark_disconnected(self) -> None:
        return None

    @property
    def connected(self) -> bool:
        """Whether the Broker connection is currently open (a call would not reconnect)."""
        return self.client.connected

    async def aclose(self) -> None:
        await self.client.close_session()
        self.state = "closed"


class BrokerClient:
    """A leased connection to the local Broker; safe to recreate after a plugin rebuild."""

    def __init__(
        self,
        cfg: BrowserConfig,
        *,
        owner: str | None = None,
        session_id: str | None = None,
    ) -> None:
        self.cfg = cfg
        self.owner = owner or cfg.broker_owner
        self.session_id = session_id or cfg.browser_session_id
        self.file = cfg.state_dir / "broker" / "broker.json"
        self.lease_id = ""
        self.lease_generation = ""
        self.last_url = ""
        self.last_title = ""
        self._last_pages: list[SimpleNamespace] = []
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._child: subprocess.Popen[Any] | None = None

    @property
    def connected(self) -> bool:
        """True while the Broker socket is open and a lease is held."""
        return self._writer is not None and bool(self.lease_id)

    async def acquire(self) -> None:
        if self.lease_id:
            return
        response = await self._request("acquire", owner=self.owner, session_id=self.session_id)
        if not response.get("ok"):
            raise BrowserError(str(response.get("error") or "broker acquire failed"), pre_dispatch=True)
        lease_id = response.get("lease_id")
        generation = response.get("generation")
        if not isinstance(lease_id, str) or not lease_id or not isinstance(generation, str):
            raise Unavailable("broker returned an invalid lease", pre_dispatch=True)
        self.lease_id = lease_id
        self.lease_generation = generation

    @staticmethod
    def _parse_pages(raw_pages: Any) -> list[SimpleNamespace]:
        if not isinstance(raw_pages, list):
            return []
        return [
            SimpleNamespace(
                index=int(item.get("index", idx + 1)),
                target_id=str(item.get("target_id", "") or ""),
                url=str(item.get("url", "") or ""),
                title=str(item.get("title", "") or ""),
                current=bool(item.get("current", False)),
            )
            for idx, item in enumerate(raw_pages)
            if isinstance(item, dict)
        ]

    async def list_pages(self) -> list[SimpleNamespace]:
        """Query the Broker for owned tabs when idle; returns cached tabs immediately when busy."""
        if not self.connected:
            return []
        if self._lock.locked():
            return list(self._last_pages)
        try:
            response = await self._request(
                "tabs",
                lease_id=self.lease_id,
                generation=self.lease_generation,
                allow_spawn=False,
                timeout=2.5,
            )
        except Exception:
            return list(self._last_pages)
        if not response.get("ok"):
            return list(self._last_pages)
        pages = self._parse_pages(response.get("pages"))
        self._last_pages = pages
        return list(pages)

    async def call(self, tool: str, arguments: Any) -> list[dict[str, Any]]:
        for attempt in range(2):
            await self.acquire()
            try:
                response = await self._request(
                    "call",
                    lease_id=self.lease_id,
                    generation=self.lease_generation,
                    tool=tool,
                    arguments=arguments,
                    toolset=self.cfg.toolset,
                )
            except ConnectionClosed:
                self.lease_id = ""
                self.lease_generation = ""
                raise
            if response.get("ok"):
                blocks = response.get("blocks")
                if not isinstance(blocks, list):
                    raise Unavailable("broker returned an invalid result")
                self.last_url = str(response.get("url", "") or "")
                self.last_title = str(response.get("title", "") or "")
                if "pages" in response:
                    self._last_pages = self._parse_pages(response.get("pages"))
                return blocks
            message = str(response.get("error") or "broker call failed")
            error = BrowserError(
                message,
                blocked_origin=str(response.get("blocked_origin") or ""),
                pre_dispatch=bool(response.get("pre_dispatch", False)),
            )
            error.code = str(response.get("code") or "browser_unavailable")
            if error.code == "browser_unavailable" and "lease is not active" in message:
                self.lease_id = ""
                self.lease_generation = ""
                if attempt == 0:
                    continue
            raise error
        raise Unavailable("broker call failed")

    async def aclose(self) -> None:
        if self.lease_id and self._writer is not None:
            with contextlib.suppress(Exception):
                await self._request(
                    "release",
                    lease_id=self.lease_id,
                    generation=self.lease_generation,
                    allow_spawn=False,
                )
        self.lease_id = ""
        self.lease_generation = ""
        self._last_pages = []
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
        self._reader = None
        self._writer = None

    async def close_session(self) -> None:
        """Close the Broker-side BrowserSession (used by extension browser_close)."""
        try:
            if self._writer is None:
                await self._connect(allow_spawn=False)
            response = await self._request(
                "close",
                lease_id=self.lease_id,
                generation=self.lease_generation,
                session_id=self.session_id,
                owner=self.owner,
                allow_spawn=False,
            )
        except _BrokerNotRunning:
            # No Broker process is running, so no Broker-side session exists to close.
            self.lease_id = ""
            self.lease_generation = ""
            self._last_pages = []
            await self.aclose()
            return
        if not response.get("ok"):
            raise BrowserError(str(response.get("error") or "broker close failed"))
        # The Broker already dropped this lease during close; skip a redundant release.
        self.lease_id = ""
        self.lease_generation = ""
        self._last_pages = []
        await self.aclose()

    async def _request(
        self, op: str, *, allow_spawn: bool = True, timeout: float | None = None, **fields: Any
    ) -> dict[str, Any]:
        async with self._lock:
            for attempt in range(2):
                sent = False
                try:
                    await self._connect(allow_spawn=allow_spawn)
                    self._request_id += 1
                    request = {"id": self._request_id, "op": op, **fields}
                    if self._writer is None:
                        raise ConnectionClosed("broker writer is not connected")
                    self._writer.write(encode_message(request, limit=MAX_TCP_MESSAGE_BYTES))
                    await self._writer.drain()
                    sent = True
                    read_timeout = (
                        float(timeout)
                        if timeout is not None
                        else max(
                            30.0,
                            self.cfg.bridge_wait + 10.0,
                            self.cfg.ready_timeout + 10.0,
                            self.cfg.nav_load_timeout + 2 * self.cfg.cdp_timeout + 10.0,
                            _requested_wait_seconds(fields) + 10.0,
                        )
                    )
                    response = (
                        await asyncio.wait_for(
                            read_async_frame(self._reader, limit=MAX_TCP_MESSAGE_BYTES),
                            timeout=read_timeout,
                        )
                        if self._reader
                        else None
                    )
                    if response is None:
                        raise ConnectionClosed("broker closed")
                    if response.get("id") != request["id"]:
                        raise ConnectionClosed("broker response id mismatch")
                    return response
                except asyncio.CancelledError:
                    self._drop_connection()
                    raise
                except (
                    ConnectionError,
                    OSError,
                    ConnectionClosed,
                    asyncio.IncompleteReadError,
                    asyncio.TimeoutError,
                ) as exc:
                    self._drop_connection()
                    if op == "call" and sent:
                        raise BrokerCallUnknown(
                            "status=uncertain retryable=false: broker call may have executed; do not retry"
                        ) from exc
                    if not sent:
                        raise BrowserError(f"broker_unavailable: {exc}", pre_dispatch=True) from exc
                    if attempt == 0:
                        continue
                    raise ConnectionClosed("broker unavailable") from exc
            raise ConnectionClosed("broker unavailable")

    async def _connect(self, *, allow_spawn: bool = True) -> None:
        if self._writer is not None:
            return
        payload = self._read_discovery()
        if payload is None:
            with contextlib.suppress(OSError):
                self.file.unlink()
        if payload is not None and not self._pid_alive(payload):
            with contextlib.suppress(OSError):
                self.file.unlink()
            payload = None
        if payload is None:
            if not self.cfg.broker_autostart or not allow_spawn:
                raise _BrokerNotRunning(
                    "browser broker is not running; start icode-browser-use-broker "
                    "(or set ICODE_BROWSER_BROKER_AUTOSTART=1 for development)"
                )
            self._spawn()
            payload = await self._wait_discovery()
        if payload is None:
            raise ConnectionClosed("cannot connect to the browser broker", pre_dispatch=True)
        if payload.get("protocol_version") != 1:
            raise Unavailable("browser broker protocol mismatch; stop the old broker and retry", pre_dispatch=True)
        if (
            payload.get("allowed_domains") != list(self.cfg.allowed_domains)
            or payload.get("allowed_origins") != list(self.cfg.allowed_origins)
            or any(
                payload.get(key) != expected
                for key, expected in (
                    ("headless", self.cfg.headless),
                    ("bridge", self.cfg.bridge),
                    ("evaluate", self.cfg.evaluate_enabled),
                )
            )
            or any(
                key in payload and payload.get(key) != expected
                for key, expected in (
                    ("save_state", self.cfg.save_state_enabled),
                    ("chrome_devtools", self.cfg.chrome_devtools),
                    ("cdp_url", self.cfg.cdp_url or ""),
                    ("upload_roots", [str(p) for p in self.cfg.upload_roots]),
                )
            )
        ):
            raise Unavailable(
                "browser broker config differs from this client; "
                "stop the broker with icode-browser-use-broker --stop and restart it",
                pre_dispatch=True,
            )
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", int(payload["port"])), timeout=5
            )
            writer.write(encode_message({"token": str(payload["token"])}, limit=MAX_TCP_MESSAGE_BYTES))
            await writer.drain()
            self._reader = reader
            self._writer = writer
        except (OSError, KeyError, ValueError) as exc:
            self._drop_connection()
            self._reader = None
            self._writer = None
            raise ConnectionClosed("cannot connect to the browser broker", pre_dispatch=True) from exc

    def _read_discovery(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    async def _wait_discovery(self) -> dict[str, Any] | None:
        for _ in range(100):
            payload = self._read_discovery()
            if payload is not None:
                return payload
            await asyncio.sleep(0.1)
        return None

    @staticmethod
    def _pid_alive(payload: dict[str, Any]) -> bool:
        try:
            pid = int(payload.get("pid", 0))
        except (TypeError, ValueError):
            return False
        return pid_alive(pid)

    def _spawn(self) -> None:
        command = broker_command(module_name=__name__)
        if command is None:
            raise Unavailable(
                "cannot start the browser broker from this runtime: it is loaded as a folder package or "
                "sys.executable is not a Python interpreter, so `python -m icode_browser_use.broker` would not "
                "start a broker. Start `icode-browser-use-broker` yourself, or use the Owned or Attach backend"
            )
        self.file.parent.mkdir(parents=True, exist_ok=True)
        log_path = self.file.parent / "broker.log"
        rotate_log_if_needed(log_path)
        env = {
            **os.environ,
            "ICODE_BROWSER_STATE_DIR": str(self.cfg.state_dir),
            "ICODE_BROWSER_BROKER": "0",
            "ICODE_BROWSER_ALLOWED_DOMAINS": ",".join(self.cfg.allowed_domains),
            "ICODE_BROWSER_ALLOWED_ORIGINS": ",".join(self.cfg.allowed_origins),
            "ICODE_BROWSER_HEADLESS": "1" if self.cfg.headless else "0",
            "ICODE_BROWSER_BRIDGE": "1" if self.cfg.bridge else "0",
            "ICODE_BROWSER_CHROME_DEVTOOLS": "1" if self.cfg.chrome_devtools else "0",
            "ICODE_BROWSER_EVALUATE": "allow" if self.cfg.evaluate_enabled else "",
            "ICODE_BROWSER_SAVE_STATE": "allow" if self.cfg.save_state_enabled else "",
            "ICODE_BROWSER_CDP_URL": self.cfg.cdp_url or "",
            "ICODE_BROWSER_UPLOAD_ROOTS": os.pathsep.join(str(p) for p in self.cfg.upload_roots),
        }
        log = log_path.open("ab")
        try:
            self._child = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=os.name != "nt",
                **windows_hidden_kwargs(),
            )
        finally:
            log.close()

    def _drop_connection(self) -> None:
        self.lease_id = ""
        self.lease_generation = ""
        self._last_pages = []
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        self._reader = None
