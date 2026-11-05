# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Streaming trajectory reader for analysis and live-tail refreshes."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from hashlib import blake2b
from io import BufferedReader
from pathlib import Path
from threading import Event
from typing import Literal, Protocol

from chrys.foundation.trajectory.envelope import (
    LINE_BUDGET_BYTES,
    SCHEMA_VERSION,
    EnvelopeError,
    TrajectoryEvent,
    decode_event_line,
    peek_envelope_header,
)
from chrys.foundation.trajectory.event_types import KNOWN_EVENT_TYPES, EventType
from chrys.foundation.trajectory.reader import CorruptLine, CoveredSequenceRanges, UnsupportedLine

EventConsumer = Callable[[TrajectoryEvent, int, int, int], None]
"""Receives ``event, byte_offset, byte_length, line_number`` during a scan."""


@dataclass(slots=True)
class ScanCursor:
    """Persistent prefix state used by initial and live-tail scans."""

    byte_offset: int = 0
    line_number: int = 0
    previous_sequence: int = 0
    covered_ranges: CoveredSequenceRanges = field(default_factory=CoveredSequenceRanges)


class _PrefixHasher(Protocol):
    def update(self, data: bytes, /) -> None: ...
    def digest(self) -> bytes: ...
    def copy(self) -> _PrefixHasher: ...


class TrajectoryScanCancelled(RuntimeError):
    """A cooperative trajectory scan was cancelled before publishing state."""


@dataclass(slots=True)
class ScanBatch:
    """Diagnostics and cursor state produced by one physical append batch."""

    cursor: ScanCursor
    corrupt_lines: list[CorruptLine] = field(default_factory=list)
    unsupported_lines: list[UnsupportedLine] = field(default_factory=list)
    unsupported_event_sequences: list[int] = field(default_factory=list)
    prefix_violations: list[PrefixViolation] = field(default_factory=list)
    torn_tail_bytes: int = 0
    event_count: int = 0
    prefix_digest: bytes = b""
    prefix_hasher: _PrefixHasher | None = None

    @property
    def unsupported_event_count(self) -> int:
        return len(self.unsupported_event_sequences)


@dataclass(frozen=True, slots=True)
class PrefixViolation:
    """One accounted-prefix failure and the first sequence region it can affect."""

    message: str
    first_sequence: int
    last_sequence: int


@dataclass(frozen=True, slots=True)
class _LinePiece:
    """One slice of the byte stream: a whole retained line, a fragment of an
    over-budget line, that line's terminator, or the torn tail."""

    kind: Literal["line", "fragment", "line_end", "tail"]
    offset: int
    data: bytes = b""
    length: int = 0


def _iter_line_pieces(
    handle: BufferedReader,
    *,
    start_offset: int,
    cancel_event: Event | None,
    chunk_size: int = 1 << 16,
) -> Iterator[_LinePiece]:
    """Yield newline-split pieces while retaining at most one budget of a line.

    A line that provably exceeds the write-side ``LINE_BUDGET_BYTES`` is
    handed out as ``fragment`` pieces closed by a ``line_end`` marker, so a
    malformed or foreign file cannot make the reader buffer an arbitrarily
    long line; ``streamed`` is positive exactly while inside such a line.
    """
    offset = start_offset
    pending = b""
    streamed = 0
    handle.seek(start_offset)
    while chunk := handle.read(chunk_size):
        raise_if_cancelled(cancel_event)
        if streamed:
            newline = chunk.find(b"\n")
            if newline < 0:
                streamed += len(chunk)
                yield _LinePiece("fragment", offset, chunk)
                continue
            streamed += newline
            yield _LinePiece("fragment", offset, chunk[:newline])
            yield _LinePiece("line_end", offset, length=streamed)
            offset += streamed + 1
            pending = chunk[newline + 1 :]
            streamed = 0
        else:
            pending += chunk
        while True:
            newline = pending.find(b"\n")
            if newline < 0:
                break
            line = pending[:newline]
            pending = pending[newline + 1 :]
            yield _LinePiece("line", offset, line, length=len(line))
            offset += len(line) + 1
        if len(pending) >= LINE_BUDGET_BYTES:
            # No newline in sight and the line can no longer fit the budget:
            # stop retaining it and stream the rest.
            streamed = len(pending)
            yield _LinePiece("fragment", offset, pending)
            pending = b""
    if streamed or pending:
        yield _LinePiece("tail", offset, length=streamed + len(pending))


def scan_trajectory_batch(
    path: Path,
    consumer: EventConsumer,
    *,
    cursor: ScanCursor | None = None,
    known_event_types: frozenset[str] = KNOWN_EVENT_TYPES,
    cancel_event: Event | None = None,
) -> ScanBatch:
    """Scan one complete physical prefix or append batch without retaining events."""
    with path.open("rb") as handle:
        return scan_open_trajectory_batch(
            handle,
            consumer,
            cursor=cursor,
            known_event_types=known_event_types,
            cancel_event=cancel_event,
        )


def scan_open_trajectory_batch(
    handle: BufferedReader,
    consumer: EventConsumer,
    *,
    cursor: ScanCursor | None = None,
    known_event_types: frozenset[str] = KNOWN_EVENT_TYPES,
    cancel_event: Event | None = None,
    prefix_hasher: _PrefixHasher | None = None,
) -> ScanBatch:
    """Scan from an already-open handle so identity and bytes share one generation."""
    raise_if_cancelled(cancel_event)
    state = cursor or ScanCursor()
    batch = ScanBatch(cursor=state)
    hasher = prefix_hasher or blake2b(digest_size=16)
    checkpoint: _PrefixHasher | None = None
    for piece in _iter_line_pieces(handle, start_offset=state.byte_offset, cancel_event=cancel_event):
        raise_if_cancelled(cancel_event)
        if piece.kind == "fragment":
            if checkpoint is None:
                checkpoint = hasher.copy()
            hasher.update(piece.data)
            continue
        if piece.kind == "tail":
            if checkpoint is not None:
                # The streamed fragments belong to an unconsumed torn line;
                # the published digest must cover consumed bytes only.
                hasher = checkpoint
            batch.torn_tail_bytes = piece.length
            break
        if piece.kind == "line":
            hasher.update(piece.data)
        hasher.update(b"\n")
        checkpoint = None
        offset = piece.offset
        raw = piece.data
        state.line_number += 1
        byte_length = piece.length + 1
        if byte_length > LINE_BUDGET_BYTES:
            # The writer gaps any line over the budget, so an over-budget
            # line cannot be a legitimate event no matter what it decodes to.
            batch.corrupt_lines.append(
                CorruptLine(
                    line_number=state.line_number,
                    byte_offset=offset,
                    byte_length=byte_length,
                    reason=f"line is {byte_length} bytes, over the {LINE_BUDGET_BYTES}-byte line budget",
                    previous_sequence=state.previous_sequence,
                )
            )
            state.byte_offset = offset + byte_length
            continue
        try:
            event = decode_event_line(raw)
        except EnvelopeError as exc:
            header = peek_envelope_header(raw)
            if header is not None and header.schema_version > SCHEMA_VERSION:
                unsupported = UnsupportedLine(
                    line_number=state.line_number,
                    byte_offset=offset,
                    sequence=header.sequence,
                    schema_version=header.schema_version,
                )
                batch.unsupported_lines.append(unsupported)
                batch.unsupported_event_sequences.append(header.sequence)
                _account_sequence(header.sequence, state=state, violations=batch.prefix_violations)
            else:
                batch.corrupt_lines.append(
                    CorruptLine(
                        line_number=state.line_number,
                        byte_offset=offset,
                        byte_length=byte_length,
                        reason=str(exc),
                        previous_sequence=state.previous_sequence,
                    )
                )
            state.byte_offset = offset + byte_length
            continue
        future_schema = event.schema_version > SCHEMA_VERSION
        if not future_schema and event.event_type == EventType.GAP:
            first = event.payload.get("first_sequence")
            last = event.payload.get("last_sequence")
            first_sequence = plain_int_value(first)
            last_sequence = plain_int_value(last)
            if first_sequence is not None and last_sequence is not None and first_sequence <= last_sequence:
                state.covered_ranges.add(first_sequence, last_sequence)
        _account_sequence(event.sequence, state=state, violations=batch.prefix_violations)
        if future_schema or event.event_type not in known_event_types:
            batch.unsupported_event_sequences.append(event.sequence)
            batch.unsupported_lines.append(
                UnsupportedLine(
                    line_number=state.line_number,
                    byte_offset=offset,
                    sequence=event.sequence,
                    schema_version=event.schema_version,
                )
            )
        if not future_schema:
            consumer(event, offset, byte_length, state.line_number)
        batch.event_count += 1
        state.byte_offset = offset + byte_length
    raise_if_cancelled(cancel_event)
    batch.prefix_digest = hasher.digest()
    # The returned state covers exactly the consumed bytes, so the next append
    # batch can continue it instead of replaying the prefix from byte zero.
    batch.prefix_hasher = hasher
    return batch


def scan_event_types(
    path: Path,
    event_types: frozenset[str],
    consumer: Callable[[TrajectoryEvent], None],
    *,
    cancel_event: Event | None = None,
) -> None:
    """Hand *consumer* each event of *event_types* in *path*, decoding only the lines that can hold one.

    Decoding every line of a long history takes seconds; looking for the type's
    JSON string takes a fraction of that, and text that merely mentions a type
    is decoded and dropped. Nothing else is accounted for: no sequence checks,
    no digest, and a line that does not decode is skipped.
    """
    needles = tuple(f'"{event_type}"'.encode() for event_type in event_types)
    with path.open("rb") as handle:
        while line := handle.readline(LINE_BUDGET_BYTES):
            raise_if_cancelled(cancel_event)
            if not line.endswith(b"\n"):
                # Over the write-side budget, or a torn tail: never a whole event.
                while (rest := handle.readline(LINE_BUDGET_BYTES)) and not rest.endswith(b"\n"):
                    pass
                continue
            if not any(needle in line for needle in needles):
                continue
            try:
                event = decode_event_line(line)
            except EnvelopeError:
                continue
            if event.schema_version <= SCHEMA_VERSION and event.event_type in event_types:
                consumer(event)


def verify_prefix(
    handle: BufferedReader,
    *,
    length: int,
    expected_digest: bytes,
    cancel_event: Event | None = None,
    chunk_size: int = 1 << 20,
) -> _PrefixHasher | None:
    """Return a reusable digest state when the consumed prefix is byte-identical."""
    hasher = blake2b(digest_size=16)
    remaining = length
    handle.seek(0)
    while remaining:
        raise_if_cancelled(cancel_event)
        chunk = handle.read(min(chunk_size, remaining))
        if not chunk:
            return None
        hasher.update(chunk)
        remaining -= len(chunk)
    return hasher if hasher.digest() == expected_digest else None


def raise_if_cancelled(cancel_event: Event | None) -> None:
    """Raise when a trajectory read or projection has been cancelled."""
    if cancel_event is not None and cancel_event.is_set():
        raise TrajectoryScanCancelled


def _account_sequence(sequence: int, *, state: ScanCursor, violations: list[PrefixViolation]) -> None:
    previous = state.previous_sequence
    if sequence <= previous:
        violations.append(
            PrefixViolation(
                message=f"sequence {sequence} does not increase after {previous}",
                first_sequence=sequence,
                last_sequence=previous,
            )
        )
    elif sequence > previous + 1:
        missing = state.covered_ranges.first_uncovered(previous + 1, sequence - 1)
        if missing is not None:
            violations.append(
                PrefixViolation(
                    message=f"sequence {missing} is missing and not covered by an earlier gap",
                    first_sequence=missing,
                    last_sequence=sequence - 1,
                )
            )
    state.previous_sequence = sequence


def plain_int_value(value: object) -> int | None:
    """Return an integer JSON leaf while rejecting booleans."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None
