# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import io
import json
import struct

import pytest

from chrys.foundation.reporting.frames import (
    GZIP_THRESHOLD_BYTES,
    KIND_AGENT_ENV,
    KIND_SNAPSHOT_CHUNK,
    KIND_TRAJECTORY_LINES,
    MAX_FRAME_PAYLOAD_BYTES,
    Frame,
    FrameError,
    FrameHeader,
    compress_body,
    decode_frame,
    decode_frames,
    decompress_body,
    encode_frames,
    iter_frame_spans,
    read_frame_header,
    scope_lines,
    scope_snapshot_chunk,
    sha256_hex,
    unit_id_for,
)


def _frame(payload: bytes = b"payload", *, kind: str = KIND_SNAPSHOT_CHUNK) -> Frame:
    scope = scope_snapshot_chunk(sha256_hex(payload), 0) if kind == KIND_SNAPSHOT_CHUNK else scope_lines(1, 1)
    return Frame.build(kind, "session-1", scope, payload)


def test_a_frame_run_decodes_to_the_frames_that_were_encoded() -> None:
    frames = [_frame(b"first"), _frame(b"second", kind=KIND_TRAJECTORY_LINES)]

    decoded = decode_frames(encode_frames(frames))

    assert [frame.header for frame in decoded] == [frame.header for frame in frames]
    assert [frame.payload for frame in decoded] == [b"first", b"second"]


def test_the_unit_id_is_derived_from_kind_session_scope_and_payload() -> None:
    payload = b"same bytes"
    digest = sha256_hex(payload)
    scope = scope_snapshot_chunk(digest, 0)

    first = FrameHeader.of(KIND_SNAPSHOT_CHUNK, "session-1", scope, payload)
    second = FrameHeader.of(KIND_SNAPSHOT_CHUNK, "session-1", scope, payload)
    other_session = FrameHeader.of(KIND_SNAPSHOT_CHUNK, "session-2", scope, payload)

    assert first.unit_id == second.unit_id
    assert first.unit_id == unit_id_for(KIND_SNAPSHOT_CHUNK, "session-1", scope, digest)
    assert other_session.unit_id != first.unit_id


def test_only_trajectory_frames_carry_the_reset_flag() -> None:
    trajectory = _frame(kind=KIND_TRAJECTORY_LINES)
    document = json.loads(trajectory.header.to_json())

    assert document["reset"] is False
    assert "reset" not in json.loads(_frame().header.to_json())


def test_agent_env_frames_carry_the_workspace_kind_and_capture_moment() -> None:
    frame = Frame.build(
        KIND_AGENT_ENV,
        "session-1",
        "agentenv:abc",
        b"{}",
        workspace_kind="git",
        captured_at="2026-11-07T06:00:00Z",
    )
    document = json.loads(frame.header.to_json())

    assert document["workspaceKind"] == "git"
    assert document["capturedAt"] == "2026-11-07T06:00:00Z"
    # The payload stays the content-addressed fact; the envelope does not
    # change the unit id, so a re-send for a new classification is the
    # server's duplicate to resolve.
    assert frame.header.unit_id == Frame.build(KIND_AGENT_ENV, "session-1", "agentenv:abc", b"{}").header.unit_id

    decoded = decode_frames(frame.encode())
    assert decoded[0].header == frame.header


def test_only_agent_env_frames_carry_the_environment_envelope() -> None:
    chunk = _frame(b"payload")

    document = json.loads(chunk.header.to_json())

    assert "workspaceKind" not in document
    assert "capturedAt" not in document
    assert "reset" not in document


def test_a_payload_over_the_frame_limit_is_refused() -> None:
    with pytest.raises(FrameError):
        Frame.build(KIND_SNAPSHOT_CHUNK, "session-1", "chunk:x:0", b"x" * (MAX_FRAME_PAYLOAD_BYTES + 1))


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(FrameError):
        Frame.build("session.unknown", "session-1", "x:1", b"x")


def test_a_truncated_run_is_an_error_not_a_skip() -> None:
    body = encode_frames([_frame(b"first"), _frame(b"second")])

    with pytest.raises(FrameError):
        decode_frame(body, offset=len(body) - 1)


def test_a_payload_that_does_not_match_its_digest_is_refused() -> None:
    body = bytearray(encode_frames([_frame(b"first")]))
    body[-1] ^= 0xFF

    with pytest.raises(FrameError):
        decode_frame(bytes(body))


def test_reading_a_header_skips_the_payload_and_names_the_frame_end() -> None:
    first = _frame(b"first")
    second = _frame(b"second")
    body = encode_frames([first, second])
    handle = io.BytesIO(body)

    header, frame_end = read_frame_header(handle, 0)
    second_header, second_end = read_frame_header(handle, frame_end)

    assert header == first.header
    assert second_header == second.header
    assert second_end == len(body)


def test_frame_spans_walk_a_body_without_re_decoding_offsets() -> None:
    first = _frame(b"first")
    second = _frame(b"second")
    body = encode_frames([first, second])

    spans = list(iter_frame_spans(body))

    assert [span[2].header.unit_id for span in spans] == [first.header.unit_id, second.header.unit_id]
    assert spans[0][0] == 0
    assert spans[0][1] == spans[1][0]
    assert body[spans[1][0] : spans[1][1]] == second.encode()


def test_the_header_length_prefix_is_big_endian() -> None:
    body = encode_frames([_frame(b"first")])
    (declared,) = struct.unpack_from(">I", body)

    assert json.loads(body[4 : 4 + declared])["kind"] == KIND_SNAPSHOT_CHUNK


def test_small_bodies_are_sent_as_they_are_and_large_ones_are_gzipped() -> None:
    small, small_compressed = compress_body(b"x" * GZIP_THRESHOLD_BYTES)
    large, large_compressed = compress_body(b"x" * (GZIP_THRESHOLD_BYTES + 1))

    assert small_compressed is False
    assert small == b"x" * GZIP_THRESHOLD_BYTES
    assert large_compressed is True
    assert decompress_body(large, compressed=True) == b"x" * (GZIP_THRESHOLD_BYTES + 1)
