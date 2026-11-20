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

from ._proc import windows_hidden_kwargs
from .bridge.protocol import MAX_TCP_MESSAGE_BYTES, encode_message, read_async_frame
from .config import BrowserConfig
from .distribution import broker_command
from .errors import BrowserError, ConnectionClosed, Unavailable


def _requested_wait_seconds(fields: dict[str, Any]) -> float:
    """A tool's own ``timeout_ms`` (browser_wait_for may wait 180 s): the read must outlast it."""
    arguments = fields.get("arguments")
    raw = arguments.get("timeout_ms") if isinstance(arguments, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.0
    return max(0.0, min(float(raw), 600_000.0)) / 1000.0


class BrokerCallUnknown(BrowserError):
    """The request may have executed but its response was lost."""

    code = "browser_unknown"


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
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._child: subprocess.Popen[bytes] | None = None

    @property
    def connected(self) -> bool:
        """True while the Broker socket is open and a lease is held."""
        return self._writer is not None and bool(self.lease_id)

    async def acquire(self) -> None:
        if self.lease_id:
            return
        response = await self._request("acquire", owner=self.owner, session_id=self.session_id)
        if not response.get("ok"):
            raise BrowserError(str(response.get("error") or "broker acquire failed"))
        lease_id = response.get("lease_id")
        generation = response.get("generation")
        if not isinstance(lease_id, str) or not lease_id or not isinstance(generation, str):
            raise Unavailable("broker returned an invalid lease")
        self.lease_id = lease_id
        self.lease_generation = generation

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
                return blocks
            message = str(response.get("error") or "broker call failed")
            error = BrowserError(message)
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
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
        self._reader = None
        self._writer = None

    async def close_session(self) -> None:
        """Close the Broker-side BrowserSession (used by extension browser_close)."""
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
        if not response.get("ok"):
            raise BrowserError(str(response.get("error") or "broker close failed"))
        await self.aclose()

    async def _request(self, op: str, *, allow_spawn: bool = True, **fields: Any) -> dict[str, Any]:
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
                    response = (
                        await asyncio.wait_for(
                            read_async_frame(self._reader, limit=MAX_TCP_MESSAGE_BYTES),
                            timeout=max(
                                30.0,
                                self.cfg.bridge_wait + 10.0,
                                self.cfg.ready_timeout + 10.0,
                                self.cfg.nav_load_timeout + 2 * self.cfg.cdp_timeout + 10.0,
                                _requested_wait_seconds(fields) + 10.0,
                            ),
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
                        raise BrowserError(f"broker_unavailable: {exc}") from exc
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
                raise Unavailable(
                    "browser broker is not running; start browser-use-broker "
                    "(or set ICODE_BROWSER_BROKER_AUTOSTART=1 for development)"
                )
            self._spawn()
            payload = await self._wait_discovery()
        if payload is None:
            raise ConnectionClosed("cannot connect to the browser broker")
        if payload.get("protocol_version") != 1:
            raise Unavailable("browser broker protocol mismatch; stop the old broker and retry")
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
        ):
            raise Unavailable(
                "browser broker config differs from this client; "
                "stop the broker with browser-use-broker --stop and restart it"
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
            raise ConnectionClosed("cannot connect to the browser broker") from exc

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
        if pid <= 0:
            return False
        if os.name == "nt":
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _spawn(self) -> None:
        command = broker_command(module_name=__name__)
        if command is None:
            raise Unavailable(
                "cannot start the browser broker from this runtime: it is loaded as a folder package or "
                "sys.executable is not a Python interpreter, so `python -m browser_use.broker` would not "
                "start a broker. Start `browser-use-broker` yourself, or use the Owned or Attach backend"
            )
        self.file.parent.mkdir(parents=True, exist_ok=True)
        log_path = self.file.parent / "broker.log"
        env = {
            **os.environ,
            "ICODE_BROWSER_STATE_DIR": str(self.cfg.state_dir),
            "ICODE_BROWSER_BROKER": "0",
            "ICODE_BROWSER_ALLOWED_DOMAINS": ",".join(self.cfg.allowed_domains),
            "ICODE_BROWSER_ALLOWED_ORIGINS": ",".join(self.cfg.allowed_origins),
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
        if self._writer is not None:
            self._writer.close()
            self._writer = None
        self._reader = None
