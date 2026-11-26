# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CDP JSON-RPC client over one browser-endpoint WebSocket.

Requests are correlated by a monotonically increasing ``id``; page commands
carry a ``sessionId`` and are routed by Chrome to the attached target. Events
(no ``id``) are fanned out to every handler registered for their ``method``.

Three invariants the session layer depends on:

* A closed socket **fails fast** - every in-flight future is completed with
  :class:`~icode_browser_use.errors.ConnectionClosed` instead of waiting out
  its own timeout.
* Keepalive pings every ``WS_PING_INTERVAL``; a full interval with neither a
  pong nor any data frame marks the socket half-open and closes it.
* ``MAX_TIMEOUTS_BEFORE_RECYCLE`` consecutive timeouts of *browser-level*
  commands (``session_id is None``) close the socket, so the session restarts
  Chrome on the next action.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from ..config import CDP_TIMEOUT, MAX_TIMEOUTS_BEFORE_RECYCLE, WS_PING_INTERVAL
from ..errors import BrowserError, CDPTimeout, ConnectionClosed
from ..logging import log
from .websocket import FrameTooLarge, WSClient

#: ``(params, session_id)`` - the second argument is the event's ``sessionId``
#: (``None`` for browser-level events).
EventHandler = Callable[[dict[str, Any], str | None], Awaitable[None]]

#: Injectable sleep seam: tests replace this module attribute with fake time.
_sleep = asyncio.sleep


class CDPConnection(Protocol):
    """The narrow CDP surface the session, policy and target layers use."""

    @property
    def closed(self) -> bool: ...

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = CDP_TIMEOUT,
    ) -> dict[str, Any]: ...

    def on(self, method: str, handler: EventHandler) -> None: ...

    async def aclose(self) -> None: ...


#: Stateful CDP events that must be processed in arrival order (never concurrently).
_ORDERED_EVENT_METHODS = frozenset(
    {
        "Target.attachedToTarget",
        "Target.detachedFromTarget",
        "Browser.downloadWillBegin",
        "Browser.downloadProgress",
        "Page.frameNavigated",
        "Page.navigatedWithinDocument",
        "Page.lifecycleEvent",
        "Page.frameStoppedLoading",
        "Page.javascriptDialogOpening",
        "Page.javascriptDialogClosed",
        "Inspector.detached",
        "Bridge.siteBlocked",
    }
)


class CDPClient:
    """Concrete :class:`CDPConnection` speaking JSON-RPC over a :class:`WSClient`."""

    def __init__(self, ws: WSClient) -> None:
        self._ws = ws
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._handlers: dict[str, list[EventHandler]] = {}
        self._event_tasks: set[asyncio.Task[None]] = set()
        self._ordered_queue: asyncio.Queue[tuple[EventHandler, dict[str, Any], str | None]] = asyncio.Queue()
        self._event_worker_task: asyncio.Task[None] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._keepalive_task: asyncio.Task[None] | None = None
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._browser_timeouts = 0

    @classmethod
    async def connect(cls, ws_url: str, *, open_timeout: float = 10.0) -> CDPClient:
        """Open the socket and start the reader and keepalive tasks."""
        ws = await WSClient.connect(ws_url, open_timeout=open_timeout)
        client = cls(ws)
        client._start()
        return client

    # -------------------------------------------------------------- lifecycle

    def _start(self) -> None:
        self._reader_task = asyncio.create_task(self._reader(), name="cdp-reader")
        self._keepalive_task = asyncio.create_task(self._keepalive(), name="cdp-keepalive")
        self._event_worker_task = asyncio.create_task(self._event_worker(), name="cdp-events")

    async def aclose(self) -> None:
        """Cancel the workers and close the socket; idempotent."""
        await self._shutdown(None)

    @property
    def closed(self) -> bool:
        return self._closed

    async def _shutdown(self, reason: ConnectionClosed | None) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
        await self._fail_all(reason or ConnectionClosed("CDP 连接已关闭"))
        current = asyncio.current_task()
        tasks_to_stop: list[asyncio.Task[None]] = []
        for task in (self._reader_task, self._keepalive_task, self._event_worker_task):
            if task is not None and task is not current:
                task.cancel()
                tasks_to_stop.append(task)
        for task in list(self._event_tasks):
            if task is not current:
                task.cancel()
                tasks_to_stop.append(task)
        self._event_tasks.clear()
        with contextlib.suppress(Exception):
            await self._ws.aclose()
        if tasks_to_stop:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.gather(*tasks_to_stop, return_exceptions=True), timeout=2.0)

    async def _fail_all(self, exc: ConnectionClosed) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(exc)

    # ------------------------------------------------------------------ sends

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float = CDP_TIMEOUT,
    ) -> dict[str, Any]:
        """Send a command and await its response (or fail fast)."""
        if self._closed:
            raise ConnectionClosed("CDP 连接已关闭")
        loop = asyncio.get_running_loop()
        msg_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[msg_id] = future

        payload: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session_id is not None:
            payload["sessionId"] = session_id
        try:
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
        except ConnectionClosed as exc:
            self._pending.pop(msg_id, None)
            await self._shutdown(exc)
            raise
        except BrowserError:
            self._pending.pop(msg_id, None)
            raise
        except asyncio.CancelledError:
            # The frame may not have been written; a late response would have no
            # waiter, so drop the pending entry before propagating the cancel.
            self._pending.pop(msg_id, None)
            raise
        except Exception as exc:  # translate transport failure
            self._pending.pop(msg_id, None)
            err = ConnectionClosed(f"CDP 发送失败：{exc}")
            await self._shutdown(err)
            raise err from exc

        try:
            result = await asyncio.wait_for(future, timeout)
        except asyncio.CancelledError:
            self._pending.pop(msg_id, None)
            raise
        except (asyncio.TimeoutError, TimeoutError):
            self._pending.pop(msg_id, None)
            if session_id is None:
                self._browser_timeouts += 1
                if self._browser_timeouts >= MAX_TIMEOUTS_BEFORE_RECYCLE:
                    log(f"浏览器级命令连续 {self._browser_timeouts} 次超时，关闭 CDP 连接以便下次动作重启 Chrome")
                    await self._shutdown(ConnectionClosed("浏览器级命令连续超时，连接已关闭"))
            raise CDPTimeout(method, timeout) from None

        if session_id is None:
            self._browser_timeouts = 0
        return result

    # ----------------------------------------------------------------- events

    def on(self, method: str, handler: EventHandler) -> None:
        """Register *handler* for *method*; several handlers may share a method."""
        self._handlers.setdefault(method, []).append(handler)

    # --------------------------------------------------------------- internal

    def _dispatch_response(self, message: dict[str, Any]) -> None:
        msg_id = message.get("id")
        if not isinstance(msg_id, int) or isinstance(msg_id, bool):
            return
        future = self._pending.pop(msg_id, None)
        if future is None or future.done():
            return
        error = message.get("error")
        if error is not None:
            future.set_exception(_error_from_cdp(error))
            return
        result = message.get("result")
        future.set_result(result if isinstance(result, dict) else {})

    def _dispatch_event(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        handlers = self._handlers.get(method) if isinstance(method, str) else None
        if not handlers:
            return
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        session_id = message.get("sessionId")
        session_id = session_id if isinstance(session_id, str) else None
        if method in _ORDERED_EVENT_METHODS:
            if self._event_worker_task is None and not self._closed:
                self._event_worker_task = asyncio.create_task(self._event_worker(), name="cdp-events")
            for handler in list(handlers):
                self._ordered_queue.put_nowait((handler, params, session_id))
            return
        for handler in list(handlers):
            task = asyncio.create_task(self._run_handler(handler, params, session_id))
            self._event_tasks.add(task)
            task.add_done_callback(self._event_tasks.discard)

    async def _event_worker(self) -> None:
        try:
            while not self._closed:
                handler, params, session_id = await self._ordered_queue.get()
                if self._closed:
                    return
                await self._run_handler(handler, params, session_id)
        except asyncio.CancelledError:
            raise

    async def _run_handler(self, handler: EventHandler, params: dict[str, Any], session_id: str | None) -> None:
        try:
            await handler(params, session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a handler must not kill the reader
            log(f"CDP 事件处理器异常：{exc!r}")

    async def _reader(self) -> None:
        try:
            while True:
                try:
                    raw = await self._ws.recv()
                except asyncio.CancelledError:
                    raise
                except FrameTooLarge as exc:
                    log(f"CDP 单帧响应超过上限（id={exc.request_id}, size={exc.size}），保持连接存活")
                    if exc.request_id is not None:
                        future = self._pending.pop(exc.request_id, None)
                        if future is not None and not future.done():
                            future.set_exception(exc)
                    continue
                except Exception as exc:  # any read error is a disconnect
                    log(f"CDP 读取循环异常：{exc!r}")
                    raw = None
                if raw is None:
                    await self._shutdown(ConnectionClosed("CDP 连接已断开"))
                    return
                try:
                    message = json.loads(raw)
                except ValueError:
                    log("CDP 收到无法解析的 JSON，已忽略")
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message:
                    self._dispatch_response(message)
                elif isinstance(message.get("method"), str):
                    self._dispatch_event(message)
        except asyncio.CancelledError:
            raise
        finally:
            if not self._closed:
                await self._shutdown(ConnectionClosed("CDP 读取循环结束"))

    async def _keepalive(self) -> None:
        try:
            while not self._closed:
                await _sleep(WS_PING_INTERVAL)
                if self._closed:
                    return
                self._ws.pong_event.clear()
                try:
                    await self._ws.ping()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # sending failure means dead
                    log(f"CDP 保活 ping 发送失败：{exc!r}")
                    await self._shutdown(ConnectionClosed("CDP 保活 ping 发送失败"))
                    return
                await _sleep(WS_PING_INTERVAL)
                if self._closed:
                    return
                if not self._ws.pong_event.is_set():
                    log("CDP 半开连接：一个完整周期内未收到 pong/数据帧，关闭连接")
                    await self._shutdown(ConnectionClosed("CDP 半开连接（pong 超时）"))
                    return
        except asyncio.CancelledError:
            raise


def _error_from_cdp(error: Any) -> BrowserError:
    """Turn a CDP ``error`` object into a model-visible :class:`BrowserError`."""
    if isinstance(error, dict):
        code = error.get("code")
        message = str(error.get("message") or error)
        return BrowserError(f"CDP 错误 {code}: {message}")
    return BrowserError(f"CDP 错误：{error}")
