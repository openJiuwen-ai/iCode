# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The ``report/1.0`` wire frame: header, payload, unit identity and batching.

A request body to ``POST /v1/batch`` is a run of frames::

    frame := u32BE(header_len) | header_json(UTF-8) | payload(payload_len bytes)

The durable queue stores exactly these bytes, so a pump only slices a
continuous run of frames out of the log and posts it — nothing is re-encoded
between disk and wire. Unit identity is content-derived
(``sha256(kind|session_id|scope|payload_sha256)``), which is what makes a
replayed batch free of duplicates on the server.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import BinaryIO, Final

FRAME_VERSION: Final = 1
"""The only frame version this build writes; a newer one is refused when read."""

CONTENT_TYPE: Final = "application/vnd.chrys.report.frames"

MAX_FRAME_PAYLOAD_BYTES: Final = 1 << 20
"""A single payload never exceeds 1 MiB (client and server both enforce it)."""

MAX_BATCH_BYTES: Final = 8 << 20
"""Uncompressed ceiling of one posted batch."""

MAX_COMPRESSED_BATCH_BYTES: Final = 2 << 20
"""Compressed ceiling of one posted batch."""

MAX_BATCH_FRAMES: Final = 4096
"""The server refuses a batch over this many frames with ``413``."""

GZIP_THRESHOLD_BYTES: Final = 32 * 1024
"""Bodies above this are gzipped; below it the header costs more than it saves."""

_HEADER_LENGTH: Final = struct.Struct(">I")

_HEADER_MAX_BYTES: Final = 8 * 1024

KIND_TRAJECTORY_LINES: Final = "trajectory.lines"
KIND_SNAPSHOT_CHUNK: Final = "snapshot.chunk"
KIND_SNAPSHOT_MANIFEST: Final = "snapshot.manifest"
KIND_SESSION_META: Final = "session.meta"
KIND_WORKSPACE_MANIFEST: Final = "workspace.manifest"
KIND_WORKSPACE_FILE: Final = "workspace.file"
KIND_AGENT_ENV: Final = "agent.env"
KIND_REPO_IDENTITY: Final = "repo.identity"

UNIT_KINDS: Final = frozenset(
    {
        KIND_TRAJECTORY_LINES,
        KIND_SNAPSHOT_CHUNK,
        KIND_SNAPSHOT_MANIFEST,
        KIND_SESSION_META,
        KIND_WORKSPACE_MANIFEST,
        KIND_WORKSPACE_FILE,
        KIND_AGENT_ENV,
        KIND_REPO_IDENTITY,
    }
)
"""Every unit kind this client produces."""


class FrameError(ValueError):
    """The bytes are not a well-formed frame run of this version."""


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hex SHA-256 of *data*."""
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(value: object) -> bytes:
    """Serialize *value* the one way this client hashes and ships JSON payloads.

    Sorted keys, no insignificant whitespace, ``ensure_ascii`` so a raw
    filesystem value (which may hold lone surrogates) encodes injectively.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def unit_id_for(kind: str, session_id: str, scope: str, payload_sha256: str) -> str:
    """The server's idempotency key for one unit."""
    return sha256_hex(f"{kind}|{session_id}|{scope}|{payload_sha256}".encode())


def scope_lines(first_sequence: int, last_sequence: int) -> str:
    """Scope of a ``trajectory.lines`` unit."""
    return f"lines:{first_sequence}-{last_sequence}"


def scope_snapshot_chunk(content_hash: str, index: int) -> str:
    """Scope of one ``snapshot.chunk`` unit."""
    return f"chunk:{content_hash}:{index}"


def scope_snapshot_manifest(content_hash: str) -> str:
    """Scope of a ``snapshot.manifest`` unit."""
    return f"manifest:{content_hash}"


def scope_session_meta(content_hash: str) -> str:
    """Scope of a ``session.meta`` unit."""
    return f"meta:{content_hash}"


def scope_workspace_manifest(manifest_sha256: str) -> str:
    """Scope of a ``workspace.manifest`` unit."""
    return f"ws:{manifest_sha256}"


def scope_workspace_file(file_sha256: str, index: int) -> str:
    """Scope of one ``workspace.file`` unit."""
    return f"wsfile:{file_sha256}:{index}"


def scope_agent_env(payload_sha256: str) -> str:
    """Scope of an ``agent.env`` unit."""
    return f"agentenv:{payload_sha256}"


def scope_repo_identity(repository_identity: str) -> str:
    """Scope of a ``repo.identity`` unit."""
    return f"repo:{repository_identity}"


@dataclass(frozen=True, slots=True)
class FrameHeader:
    """The JSON header of one frame; the payload rides beside it.

    Two fields are envelope facts of one kind only, like ``reset``: a
    ``trajectory.lines`` batch says whether it replays after a lost offset, and
    an ``agent.env`` frame carries the workspace classification and the capture
    moment that must not enter the content-addressed payload (a timestamp there
    would break sharing one configuration blob across sessions).
    """

    kind: str
    unit_id: str
    scope: str
    payload_sha256: str
    payload_len: int
    session_id: str = ""
    created_at: str = ""
    reset: bool = False
    workspace_kind: str = ""
    captured_at: str = ""
    version: int = FRAME_VERSION

    @classmethod
    def of(
        cls,
        kind: str,
        session_id: str,
        scope: str,
        payload: bytes,
        *,
        created_at: str = "",
        reset: bool = False,
        workspace_kind: str = "",
        captured_at: str = "",
    ) -> FrameHeader:
        """Build the header for *payload*, deriving its hash and unit id."""
        if kind not in UNIT_KINDS:
            msg = f"unknown report unit kind {kind!r}"
            raise FrameError(msg)
        if len(payload) > MAX_FRAME_PAYLOAD_BYTES:
            msg = f"{kind} payload is {len(payload)} bytes, over the {MAX_FRAME_PAYLOAD_BYTES}-byte frame limit"
            raise FrameError(msg)
        digest = sha256_hex(payload)
        return cls(
            kind=kind,
            unit_id=unit_id_for(kind, session_id, scope, digest),
            scope=scope,
            payload_sha256=digest,
            payload_len=len(payload),
            session_id=session_id,
            created_at=created_at,
            reset=reset,
            workspace_kind=workspace_kind,
            captured_at=captured_at,
        )

    def to_json(self) -> bytes:
        """Serialize the header; kind-specific envelope fields ride only on their kind."""
        document: dict[str, object] = {
            "v": self.version,
            "kind": self.kind,
            "unit_id": self.unit_id,
            "session_id": self.session_id,
            "scope": self.scope,
            "payload_sha256": self.payload_sha256,
            "payload_len": self.payload_len,
            "created_at": self.created_at,
        }
        if self.kind == KIND_TRAJECTORY_LINES:
            document["reset"] = self.reset
        if self.kind == KIND_AGENT_ENV:
            document["workspaceKind"] = self.workspace_kind
            document["capturedAt"] = self.captured_at
        return canonical_json_bytes(document)

    @classmethod
    def from_json(cls, raw: bytes) -> FrameHeader:
        """Parse a header, refusing anything this build cannot trust."""
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            msg = f"frame header is not JSON: {exc}"
            raise FrameError(msg) from exc
        if not isinstance(document, dict):
            msg = "frame header is not a JSON object"
            raise FrameError(msg)
        version = document.get("v")
        if not isinstance(version, int) or version != FRAME_VERSION:
            msg = f"unsupported frame version {version!r}"
            raise FrameError(msg)
        kind = document.get("kind")
        if not isinstance(kind, str) or kind not in UNIT_KINDS:
            msg = f"unknown frame kind {kind!r}"
            raise FrameError(msg)
        unit_id = document.get("unit_id")
        scope = document.get("scope")
        payload_sha256 = document.get("payload_sha256")
        payload_len = document.get("payload_len")
        if not isinstance(unit_id, str) or not isinstance(scope, str) or not isinstance(payload_sha256, str):
            msg = "frame header lacks unit_id, scope or payload_sha256"
            raise FrameError(msg)
        if not isinstance(payload_len, int) or isinstance(payload_len, bool) or payload_len < 0:
            msg = f"frame header has an invalid payload_len {payload_len!r}"
            raise FrameError(msg)
        session_id = document.get("session_id", "")
        created_at = document.get("created_at", "")
        reset = document.get("reset", False)
        workspace_kind = document.get("workspaceKind", "")
        captured_at = document.get("capturedAt", "")
        return cls(
            kind=kind,
            unit_id=unit_id,
            scope=scope,
            payload_sha256=payload_sha256,
            payload_len=payload_len,
            session_id=session_id if isinstance(session_id, str) else "",
            created_at=created_at if isinstance(created_at, str) else "",
            reset=bool(reset),
            workspace_kind=workspace_kind if isinstance(workspace_kind, str) else "",
            captured_at=captured_at if isinstance(captured_at, str) else "",
            version=version,
        )


@dataclass(frozen=True, slots=True)
class Frame:
    """One unit as it travels to disk and over the wire."""

    header: FrameHeader
    payload: bytes

    @classmethod
    def build(
        cls,
        kind: str,
        session_id: str,
        scope: str,
        payload: bytes,
        *,
        created_at: str = "",
        reset: bool = False,
        workspace_kind: str = "",
        captured_at: str = "",
    ) -> Frame:
        """Build a frame for *payload*."""
        header = FrameHeader.of(
            kind,
            session_id,
            scope,
            payload,
            created_at=created_at,
            reset=reset,
            workspace_kind=workspace_kind,
            captured_at=captured_at,
        )
        return cls(header=header, payload=payload)

    def encode(self) -> bytes:
        """The frame's bytes, exactly as the queue stores and the server reads them."""
        header = self.header.to_json()
        return b"".join((_HEADER_LENGTH.pack(len(header)), header, self.payload))


def encode_frames(frames: Iterable[Frame]) -> bytes:
    """Concatenate *frames* into one request body."""
    return b"".join(frame.encode() for frame in frames)


def decode_frame(data: bytes, offset: int = 0) -> tuple[Frame, int]:
    """Decode the frame starting at *offset*, returning it and the next offset."""
    if offset < 0 or offset + _HEADER_LENGTH.size > len(data):
        msg = "frame run ends before a header length"
        raise FrameError(msg)
    (header_len,) = _HEADER_LENGTH.unpack_from(data, offset)
    if header_len == 0 or header_len > _HEADER_MAX_BYTES:
        msg = f"frame header length {header_len} is out of range"
        raise FrameError(msg)
    header_start = offset + _HEADER_LENGTH.size
    header_end = header_start + header_len
    if header_end > len(data):
        msg = "frame run ends inside a header"
        raise FrameError(msg)
    header = FrameHeader.from_json(data[header_start:header_end])
    payload_end = header_end + header.payload_len
    if payload_end > len(data):
        msg = "frame run ends inside a payload"
        raise FrameError(msg)
    payload = data[header_end:payload_end]
    if sha256_hex(payload) != header.payload_sha256:
        msg = f"{header.unit_id}: payload does not match its header digest"
        raise FrameError(msg)
    return Frame(header=header, payload=payload), payload_end


def decode_frames(data: bytes) -> list[Frame]:
    """Decode a whole request body; a truncated tail is an error, not a skip."""
    frames: list[Frame] = []
    offset = 0
    while offset < len(data):
        frame, offset = decode_frame(data, offset)
        frames.append(frame)
    return frames


def iter_frame_spans(data: bytes) -> Iterator[tuple[int, int, Frame]]:
    """Yield ``(start, end, frame)`` for a body held in memory."""
    offset = 0
    while offset < len(data):
        start = offset
        frame, offset = decode_frame(data, offset)
        yield start, offset, frame


def read_frame_header(handle: BinaryIO, offset: int) -> tuple[FrameHeader, int]:
    """Read only the header at *offset*, returning it and the frame's end offset.

    The payload is skipped, never loaded: a queue walk stays bounded by the
    header size however large the log is.
    """
    handle.seek(offset)
    prefix = handle.read(_HEADER_LENGTH.size)
    if len(prefix) < _HEADER_LENGTH.size:
        msg = f"no complete frame header at offset {offset}"
        raise FrameError(msg)
    (header_len,) = _HEADER_LENGTH.unpack(prefix)
    if header_len == 0 or header_len > _HEADER_MAX_BYTES:
        msg = f"frame header length {header_len} is out of range at offset {offset}"
        raise FrameError(msg)
    raw = handle.read(header_len)
    if len(raw) < header_len:
        msg = f"frame header at offset {offset} is truncated"
        raise FrameError(msg)
    header = FrameHeader.from_json(raw)
    return header, offset + _HEADER_LENGTH.size + header_len + header.payload_len


def compress_body(body: bytes) -> tuple[bytes, bool]:
    """Gzip *body* above the threshold; report whether the bytes are compressed."""
    if len(body) <= GZIP_THRESHOLD_BYTES:
        return body, False
    return gzip.compress(body, compresslevel=6), True


def decompress_body(body: bytes, *, compressed: bool) -> bytes:
    """Inverse of :func:`compress_body`, for tests and manifest round-trips."""
    return gzip.decompress(body) if compressed else body
