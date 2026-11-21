# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Chrome native-messaging framing: a 4-byte little-endian length plus UTF-8 JSON.

Chrome caps a native message at 1 MiB; the same cap applies between the native
host and the plugin so a malformed peer cannot make us allocate unbounded memory.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from typing import Any, BinaryIO

MAX_MESSAGE_BYTES = 1 << 20
MAX_TCP_MESSAGE_BYTES = 64 << 20
#: Bridge wire protocol version; extension and plugin must agree at handshake.
PROTOCOL_VERSION = 1
CHUNK_PAYLOAD_BYTES = 500_000
CHUNK_THRESHOLD = 600_000
_MAX_CHUNKS = 128


class ProtocolError(RuntimeError):
    """The peer sent a frame this protocol cannot accept."""


def _json_bytes(payload: dict[str, Any]) -> bytes:
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProtocolError(f"native message is not JSON-serializable: {exc}") from exc


def _decode_object(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"native message is not UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("native message must be a JSON object")
    return payload


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise ProtocolError("truncated native message")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_message(stream: BinaryIO, *, limit: int = MAX_MESSAGE_BYTES) -> dict[str, object] | None:
    """Read one frame; ``None`` means clean EOF before a header."""
    header = stream.read(4)
    if not header:
        return None
    if len(header) < 4:
        header += _read_exact(stream, 4 - len(header))
    length = int.from_bytes(header, "little")
    if length <= 0 or length > limit:
        raise ProtocolError(f"native message length {length} is outside 1..{limit}")
    return _decode_object(_read_exact(stream, length))


def encode_message(payload: dict[str, object], *, limit: int = MAX_MESSAGE_BYTES) -> bytes:
    """Encode one frame, refusing anything Chrome would reject."""
    body = _json_bytes(payload)
    if len(body) > limit:
        raise ProtocolError(f"native message exceeds {limit} bytes")
    return len(body).to_bytes(4, "little") + body


def write_message(stream: BinaryIO, payload: dict[str, object]) -> None:
    """Write one frame and flush it to the peer."""
    stream.write(encode_message(payload))
    stream.flush()


async def read_async_frame(reader: asyncio.StreamReader, *, limit: int = MAX_MESSAGE_BYTES) -> dict[str, Any] | None:
    """Read one frame from an asyncio stream; ``None`` on clean EOF."""
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError:
        return None
    length = int.from_bytes(header, "little")
    if length <= 0 or length > limit:
        raise ProtocolError(f"native message length {length} is outside 1..{limit}")
    try:
        body = await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise ProtocolError("native message is truncated") from exc
    return _decode_object(body)


def split_payload(
    payload: dict[str, Any],
    *,
    chunk_bytes: int = CHUNK_PAYLOAD_BYTES,
    threshold: int = CHUNK_THRESHOLD,
) -> list[dict[str, Any]]:
    """Split a payload that exceeds Chrome's native-message cap into chunks.

    Chunks carry base64 of the original JSON bytes; each stays well below 1 MiB.
    The host and extension reassemble before dispatch, so the plugin sees the
    original message.
    """
    body = _json_bytes(payload)
    if len(body) <= threshold:
        return [payload]
    chunk_id = secrets.token_hex(12)
    total = (len(body) + chunk_bytes - 1) // chunk_bytes
    if total > _MAX_CHUNKS:
        raise ProtocolError(f"native message needs more than {_MAX_CHUNKS} chunks")
    frames: list[dict[str, Any]] = []
    for index in range(total):
        chunk = body[index * chunk_bytes : (index + 1) * chunk_bytes]
        frames.append(
            {
                "__bridge_chunk": {"id": chunk_id, "index": index, "total": total},
                "data": base64.b64encode(chunk).decode("ascii"),
            }
        )
    return frames


class ChunkAssembler:
    """Reassemble host/extension chunk frames; non-chunk frames pass through."""

    def __init__(self) -> None:
        self._parts: dict[str, list[str | None]] = {}
        self._total: dict[str, int] = {}

    def add(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        meta = payload.get("__bridge_chunk")
        if not isinstance(meta, dict):
            return payload
        chunk_id = meta.get("id")
        index = meta.get("index")
        total = meta.get("total")
        data = payload.get("data")
        if (
            not isinstance(chunk_id, str)
            or not chunk_id
            or not isinstance(index, int)
            or isinstance(index, bool)
            or not isinstance(total, int)
            or isinstance(total, bool)
            or not 1 <= total <= _MAX_CHUNKS
            or not 0 <= index < total
            or not isinstance(data, str)
        ):
            raise ProtocolError("invalid bridge chunk")
        expected = self._total.get(chunk_id)
        if expected is not None and expected != total:
            raise ProtocolError("bridge chunk total changed")
        self._total[chunk_id] = total
        parts = self._parts.setdefault(chunk_id, [None] * total)
        parts[index] = data
        if any(part is None for part in parts):
            return None
        try:
            chunks = [base64.b64decode(part, validate=True) for part in parts if part is not None]
        except (ValueError, TypeError) as exc:
            raise ProtocolError(f"invalid bridge chunk data: {exc}") from exc
        body = b"".join(chunks)
        if len(body) > MAX_TCP_MESSAGE_BYTES:
            raise ProtocolError("reassembled bridge message is too large")
        self._parts.pop(chunk_id, None)
        self._total.pop(chunk_id, None)
        return _decode_object(body)
