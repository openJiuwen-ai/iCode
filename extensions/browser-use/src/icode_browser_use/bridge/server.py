# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Plugin side of the extension bridge: a loopback server the native host dials.

The native host is launched by Chrome and connects here with the token written
to the rendezvous file. Frames are the same 4-byte-length JSON frames Chrome
uses for native messaging, so the host only has to pump bytes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from ..errors import BrowserError, CDPTimeout, ConnectionClosed, Unavailable
from .install import _harden_file
from .protocol import MAX_TCP_MESSAGE_BYTES, ProtocolError, encode_message, read_async_frame

EventHandler = Callable[[dict[str, Any], str | None], Awaitable[None] | None]


async def _read_frame(reader: asyncio.StreamReader) -> dict[str, Any] | None:
    return await read_async_frame(reader, limit=MAX_TCP_MESSAGE_BYTES)


class BridgeClient:
    """A CDP-shaped client over the host socket; the extension answers commands."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        on_close: Callable[[BridgeClient], None] | None = None,
        lease_id: str = "",
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_close = on_close
        self._lease_id = lease_id
        self._next_id = secrets.randbits(31) or 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._handlers: dict[str, list[EventHandler]] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._close_notified = False
        self._timeouts = 0
        #: Highest request id already issued when the last timeout was counted; requests
        #: in flight together with it time out for the same stall and are not counted again.
        self._timeout_mark = 0
        #: What the extension declared in hello/configure (set by the transport).
        self.capabilities: frozenset[str] = frozenset()
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._read_task = asyncio.get_running_loop().create_task(self._read_loop())

    @property
    def closed(self) -> bool:
        return self._closed

    def _notify_closed(self) -> None:
        if self._close_notified:
            return
        self._close_notified = True
        self._closed = True
        if self._on_close is not None:
            self._on_close(self)

    def on(self, method: str, handler: EventHandler) -> None:
        """Register *handler* for one event method."""
        self._handlers.setdefault(method, []).append(handler)

    def start_heartbeat(self, interval: float = 15.0, *, timeout: float = 5.0) -> None:
        """Start liveness pings after the extension declares the capability."""
        if self._heartbeat_task is not None or self._closed:
            return
        self._heartbeat_task = asyncio.get_running_loop().create_task(self._heartbeat(interval, timeout))

    async def _heartbeat(self, interval: float, timeout: float) -> None:
        while not self._closed:
            await asyncio.sleep(interval)
            if self._closed:
                return
            try:
                await self.send("Bridge.ping", {}, timeout=timeout)
            except ConnectionClosed:
                return
            except BrowserError:
                # Two consecutive timeouts close the client in send().
                continue

    async def send(
        self, method: str, params: Any = None, session_id: str | None = None, timeout: float = 20.0
    ) -> dict[str, Any]:
        """Send one command and wait for its result."""
        if self._closed:
            raise ConnectionClosed("bridge is closed")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload: dict[str, Any] = {
            "id": request_id,
            "method": method,
            "params": params or {},
        }
        if self._lease_id:
            payload["leaseId"] = self._lease_id
        if session_id:
            payload["sessionId"] = session_id
        try:
            try:
                await self._write(payload)
            except (OSError, ProtocolError) as exc:
                await self.aclose()
                raise ConnectionClosed(f"bridge write failed: {exc}") from exc
            try:
                result = await asyncio.wait_for(future, timeout)
            except asyncio.TimeoutError as exc:
                if request_id > self._timeout_mark:
                    self._timeouts += 1
                    self._timeout_mark = self._next_id - 1
                if self._timeouts >= 2:
                    await self.aclose()
                raise CDPTimeout(method, timeout) from exc
            self._timeouts = 0
            return result
        finally:
            self._pending.pop(request_id, None)

    async def aclose(self) -> None:
        if self._closed and self._close_notified:
            return
        self._closed = True
        current = asyncio.current_task()
        if self._read_task is not current:
            self._read_task.cancel()
        if self._heartbeat_task is not None:
            hb = self._heartbeat_task
            self._heartbeat_task = None
            if hb is not current:
                hb.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb
        if self._read_task is not current:
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task
        for future in self._pending.values():
            if not future.done():
                future.set_exception(ConnectionClosed("bridge is closed"))
        self._pending.clear()
        self._writer.close()
        with contextlib.suppress(Exception):
            await self._writer.wait_closed()
        self._notify_closed()

    async def _write(self, payload: dict[str, Any]) -> None:
        async with self._write_lock:
            self._writer.write(encode_message(payload, limit=MAX_TCP_MESSAGE_BYTES))
            await self._writer.drain()

    async def _read_loop(self) -> None:
        try:
            while not self._closed:
                message = await _read_frame(self._reader)
                if message is None:
                    break
                if "id" in message:
                    self._resolve(message)
                elif isinstance(message.get("method"), str):
                    self._dispatch(message)
        except (ProtocolError, OSError):
            pass
        finally:
            self._notify_closed()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionClosed("bridge connection closed"))
            with contextlib.suppress(Exception):
                self._writer.close()

    def _resolve(self, message: dict[str, Any]) -> None:
        request_id = message.get("id")
        if not isinstance(request_id, int):
            return
        future = self._pending.pop(request_id, None)
        if future is None or future.done():
            return
        error = message.get("error")
        if error:
            future.set_exception(Unavailable(str(error)))
            return
        result = message.get("result")
        future.set_result(result if isinstance(result, dict) else {})

    def _dispatch(self, message: dict[str, Any]) -> None:
        method = str(message["method"])
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        session_id = message.get("sessionId")
        session_id = session_id if isinstance(session_id, str) else None
        for handler in list(self._handlers.get(method, ())):
            task = asyncio.create_task(self._run(handler, params, session_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    @staticmethod
    async def _run(handler: EventHandler, params: dict[str, Any], session_id: str | None) -> None:
        try:
            result = handler(params, session_id)
            if result is not None:
                await result
        except Exception:
            pass


def _unpacked_extension_installed() -> bool:
    """Whether the Bridge's unpacked extension is copied on this machine."""
    from .install import default_extension_install_dir

    try:
        return (default_extension_install_dir() / "manifest.json").is_file()
    except OSError:
        return False


class BridgeServer:
    """A one-connection loopback server; the native host authenticates with a token."""

    def __init__(self, bridge_file: Path, *, host: str = "127.0.0.1", port: int = 0, token: str | None = None) -> None:
        self.bridge_file = bridge_file
        self.handshake_file = bridge_file.with_name(f"{bridge_file.stem}.handshake")
        """Marker that this state folder has seen the extension connect (survives restarts)."""
        self._host = host
        self._requested_port = port
        self.token = token or secrets.token_hex(16)
        self.generation = secrets.token_hex(8)
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._client: BridgeClient | None = None
        self._client_future: asyncio.Future[BridgeClient] = self._new_client_future()
        self._instance_lock: Any | None = None
        self._closed = False
        self.seen_handshake = False

    def _new_client_future(self) -> asyncio.Future[BridgeClient]:
        return asyncio.get_running_loop().create_future()

    async def start(self) -> None:
        from ..locking import acquire_instance_lock

        self._instance_lock = await acquire_instance_lock(self.bridge_file.parent, wait=0.0)
        try:
            self.seen_handshake = self._previous_handshake()
            self._server = await asyncio.start_server(self._accept, self._host, self._requested_port)
            sockets = self._server.sockets or ()
            if not sockets:
                raise Unavailable("bridge server has no listening socket")
            self.port = int(sockets[0].getsockname()[1])
            self.bridge_file.parent.mkdir(parents=True, exist_ok=True)
            self._write_discovery()
        except BaseException:
            if self._server is not None:
                self._server.close()
                self._server = None
            self._release_instance_lock()
            raise

    def _previous_handshake(self) -> bool:
        """Whether an earlier run in this state folder already saw the extension connect."""
        if self.handshake_file.is_file():
            return True
        try:
            payload = json.loads(self.bridge_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return bool(isinstance(payload, dict) and payload.get("handshake"))

    def _mark_handshake(self) -> None:
        with contextlib.suppress(OSError):
            self.handshake_file.write_text("1\n", encoding="utf-8")
            _harden_file(self.handshake_file)

    def _write_discovery(self) -> None:
        payload = {
            "port": self.port,
            "token": self.token,
            "pid": os.getpid(),
            "generation": self.generation,
            "handshake": self.seen_handshake,
        }
        temporary = self.bridge_file.with_name(f".{self.bridge_file.name}.{self.generation}.{secrets.token_hex(4)}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload))
        except BaseException:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise
        os.replace(temporary, self.bridge_file)
        _harden_file(self.bridge_file)

    def wait_timeout(self, configured: float) -> float:
        """How long to wait for the extension before failing the tool call.

        Fail fast only when nothing suggests the extension exists: no handshake
        ever and no unpacked copy on disk. A fresh install keeps the full wait,
        because the MV3 worker reconnects on its own 30 s alarm.
        """
        if self.seen_handshake or _unpacked_extension_installed():
            return configured
        return min(5.0, configured)

    async def wait_for_client(self, timeout: float = 15.0) -> BridgeClient:
        if self._client is not None and not self._client.closed:
            return self._client
        if self._client_future.done():
            self._client_future = self._new_client_future()
        try:
            return await asyncio.wait_for(asyncio.shield(self._client_future), timeout)
        except asyncio.TimeoutError as exc:
            if self.seen_handshake:
                state = f"after a previous handshake; if the extension is gone, delete {self.handshake_file}"
                advice = "reload it in chrome://extensions, or raise ICODE_BROWSER_BRIDGE_WAIT"
            else:
                state = "and this state folder has never handshaked"
                advice = (
                    "install or reload it in chrome://extensions and retry in a few seconds "
                    "(it reconnects on its 30 s alarm); diagnose with `icode-browser-use doctor --bridge`"
                )
            raise Unavailable(
                f"extension bridge host did not connect within {timeout:.0f}s ({state}); "
                f"the extension may not be installed or enabled — {advice}"
            ) from exc

    def _on_client_closed(self, client: BridgeClient) -> None:
        if self._client is client:
            self._client = None
        if self._closed:
            return
        if self._client_future.done() and not self._client_future.cancelled():
            if self._client_future.result() is client:
                self._client_future = self._new_client_future()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Free the coordination lock before any awaited cleanup: a hung client close
        # must not wedge this process's state directory.
        self._release_instance_lock()
        try:
            if self._client is not None:
                try:
                    await self._client.aclose()
                finally:
                    self._client = None
        finally:
            try:
                if self._server is not None:
                    self._server.close()
                    with contextlib.suppress(Exception):
                        await self._server.wait_closed()
                    self._server = None
            finally:
                self._unlink_own_discovery()
                self._release_instance_lock()

    def _release_instance_lock(self) -> None:
        lock = self._instance_lock
        self._instance_lock = None
        if lock is not None:
            with contextlib.suppress(Exception):
                lock.release()

    def _unlink_own_discovery(self) -> None:
        try:
            data = json.loads(self.bridge_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if data.get("generation") == self.generation:
            with contextlib.suppress(OSError):
                self.bridge_file.unlink()

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if getattr(self, "_pending_accepts", 0) >= 16:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        self._pending_accepts = getattr(self, "_pending_accepts", 0) + 1
        try:
            try:
                frame = await asyncio.wait_for(read_async_frame(reader, limit=65536), 5.0)
            except (asyncio.TimeoutError, ProtocolError, OSError):
                frame = None
            token = frame.get("token") if isinstance(frame, dict) else None
            if not isinstance(token, str) or not secrets.compare_digest(token, self.token):
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()
                return
        finally:
            self._pending_accepts -= 1
        if self._client is not None and not self._client.closed:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return
        if self._client_future.done():
            self._client_future = self._new_client_future()
        client = BridgeClient(reader, writer, on_close=self._on_client_closed, lease_id=self.generation)
        self._client = client
        self._client_future.set_result(client)
        if not self.seen_handshake:
            # The extension reached this state folder: later starts may wait through its alarm.
            self.seen_handshake = True
            self._mark_handshake()
            with contextlib.suppress(OSError):
                self._write_discovery()
