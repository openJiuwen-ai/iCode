# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A minimal, client-side RFC6455 WebSocket implementation (stdlib only).

The plugin only ever speaks the CDP WebSocket control plane, so this module
implements exactly the client half of the framing: masked text frames out,
unmasked text frames in, ping/pong keepalive and the close handshake. No
extensions (permessage-deflate) are negotiated, so a "message" is simply a
frame plus the continuation frames that share its opcode stream.

Beyond the frozen :class:`WSClient` surface (``connect`` / ``send`` / ``recv``
/ ``aclose``) this module exposes three extras that :mod:`cdp.client` relies on:

* :meth:`WSClient.ping` - send a masked Ping control frame (the keepalive
  seam; ``send`` only emits text frames).
* :attr:`WSClient.pong_event` - an :class:`asyncio.Event` that is ``set()``
  every time a Pong *or any data frame* arrives. The keepalive clears it,
  pings, then treats a still-clear flag after a full interval as a half-open
  socket. Reading ``pong_event.is_set()`` is the extra "did we hear anything
  recently?" channel; it never blocks and is safe to clear.
* :func:`_parse_ws_url`, :func:`_encode_frame` and :func:`_decode_frame` -
  pure helpers (no I/O) that make the parsing and framing unit-testable.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct
from urllib.parse import urlsplit

from ..errors import ConnectionClosed, Unavailable

# RFC6455 opcodes.
OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: The RFC6455 handshake GUID used to derive ``Sec-WebSocket-Accept``.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: Hard ceiling on a single frame and on a reassembled message (design 3.3:
#: continuation is reassembled up to 32 MiB). ``config.py`` has no entry for
#: this transport-level bound, so it lives here as the single named constant.
MAX_MESSAGE_BYTES = 32 * 1024 * 1024


def _parse_ws_url(url: str) -> tuple[str, int, str]:
    """Split ``ws://host:port/path`` into ``(host, port, path)``.

    Only plain ``ws://`` is supported - the CDP endpoint is always loopback and
    never TLS. Anything else raises :class:`Unavailable` before a socket opens.
    """
    parts = urlsplit(url)
    if parts.scheme != "ws":
        raise Unavailable(f"仅支持 ws:// 端点：{url}")
    host = parts.hostname
    if not host:
        raise Unavailable(f"非法的 WebSocket 地址：{url}")
    port = parts.port or 80
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return host, port, path


def _host_header(host: str, port: int) -> str:
    """Render a ``Host`` header value, bracketing IPv6 literals."""
    literal = f"[{host}]" if ":" in host else host
    return f"{literal}:{port}"


def _handshake_request(host: str, port: int, path: str, key: str) -> bytes:
    """Build the HTTP Upgrade request. Deliberately **no** ``Origin`` header."""
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {_host_header(host, port)}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


def _expected_accept(key: str) -> str:
    """The ``Sec-WebSocket-Accept`` value the server must echo for *key*."""
    digest = hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _encode_frame(opcode: int, payload: bytes, *, fin: bool = True, mask: bool = True) -> bytes:
    """Encode one frame with 7-bit / 7+16-bit / 7+64-bit length encoding.

    Client frames must be masked; ``mask=False`` exists only so tests can feed
    server-shaped (unmasked) frames into the reader.
    """
    length = len(payload)
    header = bytearray()
    header.append((0x80 if fin else 0x00) | (opcode & 0x0F))
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length < (1 << 16):
        header.append(mask_bit | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(mask_bit | 127)
        header.extend(struct.pack("!Q", length))
    if not mask:
        return bytes(header) + payload
    mask_key = os.urandom(4)
    masked = bytes(byte ^ mask_key[i & 3] for i, byte in enumerate(payload))
    return bytes(header) + mask_key + masked


def _decode_frame(buf: bytes) -> tuple[bool, int, bytes, int] | None:
    """Decode one frame from *buf*.

    Returns ``(fin, opcode, payload, consumed)`` or ``None`` when *buf* does not
    yet hold a whole frame. Raises :class:`Unavailable` when the advertised
    length exceeds :data:`MAX_MESSAGE_BYTES`.
    """
    if len(buf) < 2:
        return None
    first = buf[0]
    second = buf[1]
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    offset = 2
    if length == 126:
        if len(buf) < offset + 2:
            return None
        length = struct.unpack_from("!H", buf, offset)[0]
        offset += 2
    elif length == 127:
        if len(buf) < offset + 8:
            return None
        length = struct.unpack_from("!Q", buf, offset)[0]
        offset += 8
    if length > MAX_MESSAGE_BYTES:
        raise Unavailable("WebSocket 帧长度超过上限")
    mask_key = b""
    if masked:
        if len(buf) < offset + 4:
            return None
        mask_key = buf[offset : offset + 4]
        offset += 4
    if len(buf) < offset + length:
        return None
    if masked:
        payload = bytes(buf[offset + i] ^ mask_key[i & 3] for i in range(length))
    else:
        payload = bytes(buf[offset : offset + length])
    return fin, opcode, payload, offset + length


async def _abort(writer: asyncio.StreamWriter) -> None:
    """Close a partially-established connection, swallowing teardown errors."""
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:  # best-effort cleanup on the error path
        pass


async def _read_http_response(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    """Read an HTTP status line plus headers (used only for the handshake)."""
    status_line = await reader.readline()
    if not status_line:
        raise Unavailable("CDP 握手失败：连接在响应前关闭")
    parts = status_line.decode("latin-1", "replace").rstrip("\r\n").split(" ", 2)
    if len(parts) < 2:
        raise Unavailable(f"CDP 握手失败：非法状态行 {status_line!r}")
    try:
        status = int(parts[1])
    except ValueError as exc:
        raise Unavailable(f"CDP 握手失败：非法状态码 {parts[1]!r}") from exc
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line:
            raise Unavailable("CDP 握手失败：响应头不完整")
        if line in (b"\r\n", b"\n"):
            break
        text = line.decode("latin-1", "replace").rstrip("\r\n")
        if ":" not in text:
            continue
        name, value = text.split(":", 1)
        headers[name.strip().lower()] = value.strip()
    return status, headers


class WSClient:
    """A connected, RFC6455-framed WebSocket to a CDP endpoint.

    Instances own the ``(reader, writer)`` pair returned by
    :func:`asyncio.open_connection`. :attr:`pong_event` is the extra keepalive
    observation channel described in the module docstring.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        open_timeout: float = 10.0,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._open_timeout = open_timeout
        self._closed = False
        self._write_lock = asyncio.Lock()
        #: ``set()`` on every Pong and on every data frame; see module docstring.
        self.pong_event = asyncio.Event()

    @classmethod
    async def connect(cls, url: str, *, open_timeout: float = 10.0) -> WSClient:
        """Open a socket to *url* and complete the WebSocket handshake."""
        host, port, path = _parse_ws_url(url)
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), open_timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise Unavailable(f"无法连接 CDP 端点 {url}：{exc}") from exc

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        try:
            writer.write(_handshake_request(host, port, path, key))
            await asyncio.wait_for(writer.drain(), open_timeout)
            status, headers = await asyncio.wait_for(_read_http_response(reader), open_timeout)
        except asyncio.TimeoutError as exc:
            await _abort(writer)
            raise Unavailable(f"CDP 握手超时：{url}") from exc
        except Unavailable:
            await _abort(writer)
            raise
        except OSError as exc:
            await _abort(writer)
            raise Unavailable(f"CDP 握手失败：{exc}") from exc

        if status != 101:
            await _abort(writer)
            raise Unavailable(f"CDP 握手失败：HTTP {status}")
        if headers.get("sec-websocket-accept", "").strip() != _expected_accept(key):
            await _abort(writer)
            raise Unavailable("CDP 握手失败：Sec-WebSocket-Accept 校验不通过")
        return cls(reader, writer, open_timeout=open_timeout)

    async def _write_control(self, opcode: int, payload: bytes) -> None:
        """Best-effort send of a control frame (Pong / Close) from the reader."""
        try:
            async with self._write_lock:
                self._writer.write(_encode_frame(opcode, payload, mask=True))
                await asyncio.wait_for(self._writer.drain(), self._open_timeout)
        except Exception:  # control frames must never mask a failure
            pass

    async def _close_writer(self) -> None:
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:  # idempotent teardown
            pass

    async def send(self, text: str) -> None:
        """Send one masked text frame and flush it (with a timeout)."""
        if self._closed:
            raise ConnectionClosed("WebSocket 已关闭，无法发送")
        frame = _encode_frame(OP_TEXT, text.encode("utf-8"), mask=True)
        try:
            async with self._write_lock:
                self._writer.write(frame)
                await asyncio.wait_for(self._writer.drain(), self._open_timeout)
        except asyncio.TimeoutError as exc:
            raise ConnectionClosed("WebSocket 发送超时（连接可能已断开）") from exc

    async def ping(self, payload: bytes = b"") -> None:
        """Send one masked Ping control frame (the keepalive seam)."""
        if self._closed:
            raise ConnectionClosed("WebSocket 已关闭，无法发送 ping")
        frame = _encode_frame(OP_PING, payload, mask=True)
        async with self._write_lock:
            self._writer.write(frame)
            await asyncio.wait_for(self._writer.drain(), self._open_timeout)

    async def _read_frame(self) -> tuple[bool, int, bytes]:
        """Read exactly one frame from the stream."""
        reader = self._reader
        first = await reader.readexactly(2)
        fin = bool(first[0] & 0x80)
        opcode = first[0] & 0x0F
        masked = bool(first[1] & 0x80)
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack("!H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await reader.readexactly(8))[0]
        if length > MAX_MESSAGE_BYTES:
            raise Unavailable("WebSocket 帧长度超过上限")
        mask_key = await reader.readexactly(4) if masked else b""
        payload = await reader.readexactly(length)
        if masked:
            payload = bytes(byte ^ mask_key[i & 3] for i, byte in enumerate(payload))
        return fin, opcode, payload

    async def recv(self) -> str | None:
        """Return the next text message, answering control frames as needed.

        Pings are answered with a pong and skipped; a close frame is echoed and
        yields ``None``; a peer EOF (the browser closing first) also yields
        ``None``. Continuation frames are reassembled up to
        :data:`MAX_MESSAGE_BYTES`.
        """
        message = bytearray()
        message_opcode: int | None = None
        while True:
            try:
                fin, opcode, payload = await self._read_frame()
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                return None
            if opcode == OP_PING:
                await self._write_control(OP_PONG, payload)
                self.pong_event.set()
                continue
            if opcode == OP_PONG:
                self.pong_event.set()
                continue
            if opcode == OP_CLOSE:
                await self._write_control(OP_CLOSE, payload[:2])
                self._closed = True
                await self._close_writer()
                return None
            if opcode == OP_CONTINUATION:
                if message_opcode is None:
                    raise Unavailable("收到没有起始帧的 continuation 帧")
            elif opcode in (OP_TEXT, OP_BINARY):
                if message_opcode is not None:
                    raise Unavailable("收到嵌套的数据帧（协议错误）")
                message_opcode = opcode
            else:
                raise Unavailable(f"不支持的 WebSocket opcode：{opcode}")

            if len(message) + len(payload) > MAX_MESSAGE_BYTES:
                raise Unavailable("WebSocket 消息超过 32 MiB 上限")
            message.extend(payload)
            self.pong_event.set()
            if fin:
                try:
                    return message.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise Unavailable(f"WebSocket 文本消息编码非法：{exc}") from exc

    async def aclose(self) -> None:
        """Send a close frame and tear the socket down; idempotent."""
        if self._closed:
            return
        self._closed = True
        await self._write_control(OP_CLOSE, b"")
        await self._close_writer()
