# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A minimal, client-side RFC6455 WebSocket implementation (stdlib only).

The plugin only ever speaks the CDP WebSocket control plane, so this module
implements exactly the client half of the framing: masked text frames out,
unmasked text frames in, ping/pong keepalive and the close handshake. No
extensions (permessage-deflate) are negotiated, so a "message" is simply a
frame plus the continuation frames that share its opcode stream.

Oversized frames (>32 MiB) are drained cleanly without dropping the connection
and surfaced as a non-fatal :class:`FrameTooLarge` carrying the JSON-RPC ``id``
when recoverable from the first 256 bytes, so the caller can fail only that
request and keep the browser session alive.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import struct
from urllib.parse import urlsplit

from ..errors import BrowserError, ConnectionClosed, Unavailable

# RFC6455 opcodes.
OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

#: The RFC6455 handshake GUID used to derive ``Sec-WebSocket-Accept``.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

#: Hard ceiling on a single frame and on a reassembled message (32 MiB).
MAX_MESSAGE_BYTES = 32 * 1024 * 1024

_ID_HEAD_RE = re.compile(rb'^\s*\{\s*(?:"sessionId"\s*:\s*"[^"]*"\s*,\s*)?"id"\s*:\s*(\d+)')
_DRAIN_CHUNK = 65536


class FrameTooLarge(BrowserError):
    """Non-fatal signal that a single WebSocket message exceeded :data:`MAX_MESSAGE_BYTES`.

    The underlying stream has already been drained to the end of the message, so
    the WebSocket connection remains synchronized and reusable.
    """

    def __init__(self, *, request_id: int | None, size: int) -> None:
        self.request_id = request_id
        self.size = size
        super().__init__(f"CDP 响应超过 32 MiB 上限（{size} 字节）")


def _xor_mask(payload: bytes, mask_key: bytes) -> bytes:
    """Fast stdlib XOR masking using big-integer arithmetic (zero third-party deps)."""
    n = len(payload)
    if n == 0:
        return b""
    repeated = (mask_key * ((n + 3) // 4))[:n]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(repeated, "big")).to_bytes(n, "big")


def _extract_request_id(head: bytes) -> int | None:
    match = _ID_HEAD_RE.match(head)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


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
    digest = hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()  # noqa: S324 - mandated by RFC 6455
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
    return bytes(header) + mask_key + _xor_mask(payload, mask_key)


def _decode_frame(buf: bytes) -> tuple[bool, int, bytes, int] | None:
    """Decode one frame from *buf*.

    Returns ``(fin, opcode, payload, consumed)`` or ``None`` when *buf* does not
    yet hold a whole frame. Raises :class:`Unavailable` when the advertised
    length exceeds :data:`MAX_MESSAGE_BYTES` or RFC 6455 framing rules fail.
    """
    if len(buf) < 2:
        return None
    first = buf[0]
    second = buf[1]
    if first & 0x70:
        raise Unavailable("WebSocket 帧设置了未协商的 RSV 位")
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    length = second & 0x7F
    if opcode >= 0x8 and (not fin or length > 125):
        raise Unavailable("WebSocket 控制帧非法（分片或长度超过 125）")
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
        if length & (1 << 63):
            raise Unavailable("WebSocket 64 位帧长度最高位非法")
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
    raw = bytes(buf[offset : offset + length])
    payload = _xor_mask(raw, mask_key) if masked else raw
    return fin, opcode, payload, offset + length


async def _abort(writer: asyncio.StreamWriter) -> None:
    """Close a partially-established connection, swallowing teardown errors."""
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
    except Exception:
        transport = getattr(writer, "transport", None)
        if transport is not None:
            try:
                transport.abort()
            except Exception:
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
    """A connected, RFC6455-framed WebSocket to a CDP endpoint."""

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
        except (OSError, asyncio.LimitOverrunError, ValueError) as exc:
            await _abort(writer)
            raise Unavailable(f"CDP 握手失败：{exc}") from exc
        except BaseException:
            await _abort(writer)
            raise

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
                if self._closed and opcode != OP_CLOSE:
                    return
                self._writer.write(_encode_frame(opcode, payload, mask=True))
                await asyncio.wait_for(self._writer.drain(), self._open_timeout)
        except Exception:  # control frames must never mask a failure
            pass

    async def _close_writer(self) -> None:
        await _abort(self._writer)

    async def send(self, text: str) -> None:
        """Send one masked text frame and flush it (with a timeout)."""
        if self._closed:
            raise ConnectionClosed("WebSocket 已关闭，无法发送")
        frame = _encode_frame(OP_TEXT, text.encode("utf-8"), mask=True)
        try:
            async with self._write_lock:
                if self._closed:
                    raise ConnectionClosed("WebSocket 已关闭，无法发送")
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
            if self._closed:
                raise ConnectionClosed("WebSocket 已关闭，无法发送 ping")
            self._writer.write(frame)
            await asyncio.wait_for(self._writer.drain(), self._open_timeout)

    async def _drain_bytes(self, count: int) -> None:
        remaining = count
        while remaining > 0:
            chunk = await self._reader.readexactly(min(remaining, _DRAIN_CHUNK))
            remaining -= len(chunk)

    async def _read_frame(self) -> tuple[bool, int, bytes, bool, int | None, int]:
        """Read one frame. Returns ``(fin, opcode, payload, oversized, req_id, raw_len)``."""
        reader = self._reader
        first = await reader.readexactly(2)
        if first[0] & 0x70:
            raise Unavailable("WebSocket 帧设置了未协商的 RSV 位")
        fin = bool(first[0] & 0x80)
        opcode = first[0] & 0x0F
        masked = bool(first[1] & 0x80)
        if masked:
            raise Unavailable("WebSocket 服务端帧非法设置了掩码位")
        length = first[1] & 0x7F
        if opcode >= 0x8 and (not fin or length > 125):
            raise Unavailable("WebSocket 控制帧非法（分片或长度超过 125）")
        if length == 126:
            length = struct.unpack("!H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", await reader.readexactly(8))[0]
            if length & (1 << 63):
                raise Unavailable("WebSocket 64 位帧长度最高位非法")
        mask_key = await reader.readexactly(4) if masked else b""
        if length > MAX_MESSAGE_BYTES:
            head_len = min(256, length)
            head = await reader.readexactly(head_len)
            if masked:
                head = _xor_mask(head, mask_key)
            req_id = _extract_request_id(head)
            await self._drain_bytes(length - head_len)
            return fin, opcode, head, True, req_id, length
        payload = await reader.readexactly(length)
        if masked:
            payload = _xor_mask(payload, mask_key)
        return fin, opcode, payload, False, None, length

    async def recv(self) -> str | None:
        """Return the next text message, answering control frames as needed.

        Oversized messages (>32 MiB) are drained to the final continuation frame
        and raise non-fatal :class:`FrameTooLarge` so the connection stays open.
        """
        message = bytearray()
        head_buf = bytearray()
        message_opcode: int | None = None
        oversized = False
        oversized_req_id: int | None = None
        oversized_bytes = 0
        while True:
            try:
                fin, opcode, payload, frame_oversized, frame_req_id, raw_len = await self._read_frame()
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
                async with self._write_lock:
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

            self.pong_event.set()
            if len(head_buf) < 256 and payload:
                need = 256 - len(head_buf)
                head_buf.extend(payload[:need])
            if oversized or frame_oversized or len(message) + len(payload) > MAX_MESSAGE_BYTES:
                if not oversized:
                    oversized = True
                    oversized_bytes = len(message) + raw_len
                    oversized_req_id = _extract_request_id(bytes(head_buf))
                    if oversized_req_id is None and frame_req_id is not None and not message:
                        oversized_req_id = frame_req_id
                    message.clear()
                else:
                    oversized_bytes += raw_len
                    if oversized_req_id is None and head_buf:
                        oversized_req_id = _extract_request_id(bytes(head_buf))
                if fin:
                    raise FrameTooLarge(request_id=oversized_req_id, size=oversized_bytes)
                continue

            message.extend(payload)
            if fin:
                try:
                    return message.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise Unavailable(f"WebSocket 文本消息编码非法：{exc}") from exc

    async def aclose(self) -> None:
        """Send a close frame and tear the socket down; idempotent."""
        async with self._write_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._writer.write(_encode_frame(OP_CLOSE, b"", mask=True))
                await asyncio.wait_for(self._writer.drain(), min(2.0, self._open_timeout))
            except Exception:
                pass
            await self._close_writer()
