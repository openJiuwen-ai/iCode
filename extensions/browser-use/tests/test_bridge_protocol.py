# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import io
import json

import pytest

from icode_browser_use.bridge.protocol import (
    MAX_MESSAGE_BYTES,
    MAX_TCP_MESSAGE_BYTES,
    ChunkAssembler,
    ProtocolError,
    encode_message,
    read_message,
    split_payload,
    write_message,
)


def test_roundtrip_unicode() -> None:
    payload = {"method": "Page.navigate", "params": {"url": "https://例子.测试/路径"}}
    stream = io.BytesIO()
    write_message(stream, payload)
    stream.seek(0)
    assert read_message(stream) == payload


def test_eof_returns_none() -> None:
    assert read_message(io.BytesIO(b"")) is None


def test_encoded_frame_has_little_endian_length() -> None:
    frame = encode_message({"a": 1})
    assert int.from_bytes(frame[:4], "little") == len(frame) - 4


def test_reads_in_small_chunks() -> None:
    class Chunked:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def read(self, size: int) -> bytes:
            chunk = self._data[:1]
            self._data = self._data[1:]
            return chunk

    frame = encode_message({"ok": True})
    assert read_message(Chunked(frame)) == {"ok": True}


def test_rejects_oversize_frame() -> None:
    stream = io.BytesIO((MAX_MESSAGE_BYTES + 1).to_bytes(4, "little") + b"{}")
    with pytest.raises(ProtocolError):
        read_message(stream)


def test_rejects_truncated_body() -> None:
    frame = encode_message({"ok": True})
    with pytest.raises(ProtocolError):
        read_message(io.BytesIO(frame[:4] + frame[4:-1]))


def test_rejects_non_object_json() -> None:
    body = b"[1,2,3]"
    with pytest.raises(ProtocolError):
        read_message(io.BytesIO(len(body).to_bytes(4, "little") + body))


def test_rejects_invalid_json() -> None:
    body = b"not json"
    with pytest.raises(ProtocolError):
        read_message(io.BytesIO(len(body).to_bytes(4, "little") + body))


def test_encode_rejects_oversize_payload() -> None:
    with pytest.raises(ProtocolError):
        encode_message({"blob": "x" * (MAX_MESSAGE_BYTES + 1)})


def test_json_is_utf8() -> None:
    frame = encode_message({"x": "😀"})
    assert json.loads(frame[4:].decode("utf-8")) == {"x": "😀"}


def test_small_payload_is_not_chunked() -> None:
    payload = {"method": "Page.navigate", "params": {"url": "https://example.com"}}
    assert split_payload(payload) == [payload]


def test_large_payload_chunks_fit_native_limit_and_reassemble() -> None:
    payload = {"id": 1, "result": {"blob": "x" * (MAX_MESSAGE_BYTES + 100)}}
    frames = split_payload(payload)
    assert len(frames) > 1
    for frame in frames:
        assert len(encode_message(frame)) <= MAX_MESSAGE_BYTES + 4
    assembler = ChunkAssembler()
    restored = None
    for frame in reversed(frames):
        restored = assembler.add(frame)
    assert restored == payload


def test_chunk_assembler_rejects_bad_metadata() -> None:
    assembler = ChunkAssembler()
    with pytest.raises(ProtocolError):
        assembler.add({"__bridge_chunk": {"id": "x", "index": 0, "total": 0}, "data": ""})
    with pytest.raises(ProtocolError):
        assembler.add({"__bridge_chunk": {"id": "x", "index": 1, "total": 1}, "data": ""})


def test_tcp_frames_allow_larger_payloads() -> None:
    payload = {"blob": "x" * (MAX_MESSAGE_BYTES + 100)}
    frame = encode_message(payload, limit=MAX_TCP_MESSAGE_BYTES)
    assert len(frame) > MAX_MESSAGE_BYTES
