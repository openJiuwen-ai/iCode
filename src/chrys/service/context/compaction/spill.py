# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Phase-4 dropped-call records, catalog projection, and reconciliation."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, cast
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import atomic_write_text, fsync_directory, is_utf8_encodable
from chrys.foundation.text.images import hashable_image_identity
from chrys.foundation.tool_execution_stamp import (
    canonicalize_effective_arguments,
    execution_stamp_from_metadata,
)
from chrys.foundation.tool_result_metadata import TOOL_ERROR_KIND_METADATA_KEY
from chrys.foundation.util.filenames import bounded_safe_stem, safe_platform_basename
from chrys.kernel import TOOL_CALL_CONTENT_TYPES, Content, is_image_content

if TYPE_CHECKING:
    from chrys.service.context.compaction.scoped import ScopedGroup

logger = logging.getLogger(__name__)

PER_RECORD_CAP_BYTES = 2 * 1024 * 1024
"""Maximum bytes retained in one spill record."""

PER_ROUND_CAP_BYTES = 64 * 1024 * 1024
"""Maximum spill-record bytes admitted by one compaction round."""

PER_SESSION_QUOTA_BYTES = 256 * 1024 * 1024
"""Maximum live spill-record bytes retained by one session."""

COMPACTIONS_DIR_NAME = "compactions"
DROPPED_DIR_NAME = "dropped"
CATALOG_FILE_NAME = "catalog.jsonl"
MANIFEST_FILE_NAME = "manifest.md"
SUB_AGENTS_DIR_NAME = "sub_agents"
RECORD_FILE_SUFFIX = ".md"

CATALOG_RELATIVE_PATH = Path(COMPACTIONS_DIR_NAME) / DROPPED_DIR_NAME / CATALOG_FILE_NAME
"""Session-relative location of the append-only record catalog."""

RECORD_ID_HEX_CHARS = 8
"""Record-ID length: a turn spills at most a few hundred groups and filename
collisions are handled by the exclusive-write retry, so 8 hex chars suffice."""

CATALOG_KIND_GROUP = "group"
"""Catalog ``kind`` for records archiving one dropped scoped group."""

CATALOG_KIND_NOTE = "note"
"""Catalog ``kind`` for records archiving one superseded LAST_WORDS note."""

NOTE_RECORD_TOOL_NAME = "last_words"
"""Pseudo tool name used in note-record filenames and manifest lines."""

NOTE_RECORD_GROUP_ID = "superseded_note"
"""Synthetic manifest group ID for note records (no scoped group backs them)."""

_RECORD_ID_RE = re.compile(rf"^[0-9a-f]{{{RECORD_ID_HEX_CHARS}}}$")
_TURN_DIRECTORY_RE = re.compile(r"^turn[0-9]{3,}$")
_RECORD_TERMINATOR = "<!-- end of record -->\n"
_RECORD_TRUNCATION_TEMPLATE = "\n[… middle-truncated from {size} bytes …]\n"


class _UnsafeCatalogPathError(OSError):
    """The fixed spill catalog path is not owned directly by its session root."""


@dataclass(frozen=True, slots=True)
class CatalogRecord:
    """One live catalog record."""

    record_id: str
    relative_path: str
    turn: int
    round: int
    tool: str
    bytes: int
    created_at: str
    kind: str = CATALOG_KIND_GROUP

    def to_catalog_line(self) -> dict[str, Any]:
        """Return the append-only JSON object for this record."""
        return {
            "record_id": self.record_id,
            "relative_path": self.relative_path,
            "turn": self.turn,
            "round": self.round,
            "tool": self.tool,
            "bytes": self.bytes,
            "created_at": self.created_at,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class SpillManifestItem:
    """Neutral spill result converted to ``last_words_state.ManifestEntry`` state."""

    record_id: str
    group_id: str
    record_dir: str
    relative_path: str
    turn: int
    round: int
    sequence: int
    tool: str
    display_argument: str
    outcome: str
    size_chars: int
    assistant_text: bool = False
    available: bool = True
    no_record_reason: str = ""


@dataclass(frozen=True, slots=True)
class SpillBatchResult:
    """Worker-thread result for one Phase-4 spill batch."""

    entries: tuple[SpillManifestItem, ...]
    unexpected_io_failure: bool = False
    evicted_relative_paths: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class SpillReconciliationResult:
    """Catalog-derived state produced while restoring a session."""

    live_total_bytes: int
    live_record_count: int
    available_relative_paths: frozenset[str]
    deleted_orphans: int = 0


class SpillQuota:
    """Thread-safe session spill ledger with reserve-before-write semantics.

    ``spent_bytes`` reports committed live bytes; outstanding reservations also
    consume headroom internally. ``commit`` rejects an actual byte count larger
    than its reservation and leaves that reservation intact for explicit release.
    ``reclaim`` returns committed headroom after cataloged records are evicted.
    Reconciliation failures mark storage unavailable so retained but unaccounted
    records can never be followed by a fresh quota's worth of writes.

    The separate ``catalog_lock`` serializes the session's append-only catalog
    and its ``manifest.md`` projections across main and sub-agent workers. It is
    intentionally distinct from the quota ledger lock so catalog work may call
    reserve/commit/reclaim without lock recursion.
    """

    def __init__(self, limit_bytes: int = PER_SESSION_QUOTA_BYTES) -> None:
        if limit_bytes < 0:
            raise ValueError("limit_bytes must be non-negative")
        self._limit_bytes = limit_bytes
        self._spent_bytes = 0
        self._reserved_bytes = 0
        self._available_relative_paths: set[str] = set()
        self._committed_bytes_by_path: dict[str, int] = {}
        self._live_record_counts_by_path: dict[str, int] = {}
        self._live_catalog_records_by_id: dict[str, CatalogRecord] = {}
        self._catalog_record_id_by_path: dict[str, str] = {}
        self._live_catalog_records_by_directory: dict[str, dict[str, CatalogRecord]] = {}
        self._live_record_count = 0
        self._storage_available = True
        self._lock = threading.Lock()
        self._catalog_lock = threading.Lock()

    @property
    def spent_bytes(self) -> int:
        """Return committed live bytes, excluding outstanding reservations."""
        with self._lock:
            return self._spent_bytes

    @property
    def catalog_lock(self) -> threading.Lock:
        """Return the engine-owned lock serializing catalog/projection work."""
        return self._catalog_lock

    @property
    def storage_available(self) -> bool:
        """Return whether reconciled spill storage may accept new records."""
        with self._lock:
            return self._storage_available

    def initialize(
        self,
        live_total_bytes: int,
        available_relative_paths: Iterable[str] = (),
        *,
        committed_bytes_by_path: Mapping[str, int] | None = None,
        live_relative_paths: Iterable[str] | None = None,
        live_catalog_records: Iterable[CatalogRecord] | None = None,
    ) -> None:
        """Set the catalog-derived committed baseline before spill work starts."""
        if live_total_bytes < 0:
            raise ValueError("live_total_bytes must be non-negative")
        available = set(available_relative_paths)
        committed = dict(committed_bytes_by_path or {})
        catalog_records = list(live_catalog_records or ())
        if live_relative_paths is not None:
            live_paths = live_relative_paths
        elif catalog_records:
            live_paths = (record.relative_path for record in catalog_records)
        else:
            live_paths = available
        live_counts: dict[str, int] = {}
        for relative_path in live_paths:
            live_counts[relative_path] = live_counts.get(relative_path, 0) + 1
        if any(byte_count < 0 for byte_count in committed.values()):
            raise ValueError("committed record bytes must be non-negative")
        if sum(committed.values()) > live_total_bytes:
            raise ValueError("committed record bytes cannot exceed live_total_bytes")
        with self._lock:
            if self._reserved_bytes:
                raise RuntimeError("cannot initialize SpillQuota with outstanding reservations")
            self._spent_bytes = live_total_bytes
            self._available_relative_paths = available
            self._committed_bytes_by_path = committed
            self._live_record_counts_by_path = live_counts
            self._live_catalog_records_by_id = {record.record_id: record for record in catalog_records}
            self._catalog_record_id_by_path = {record.relative_path: record.record_id for record in catalog_records}
            records_by_directory: dict[str, dict[str, CatalogRecord]] = {}
            for record in catalog_records:
                directory = Path(record.relative_path).parent.as_posix()
                records_by_directory.setdefault(directory, {})[record.record_id] = record
            self._live_catalog_records_by_directory = records_by_directory
            self._live_record_count = sum(live_counts.values())
            self._storage_available = True

    def disable_storage(self) -> None:
        """Fail closed after storage cannot be reconciled safely."""
        with self._lock:
            if self._reserved_bytes:
                raise RuntimeError("cannot disable SpillQuota with outstanding reservations")
            self._spent_bytes = 0
            self._available_relative_paths.clear()
            self._committed_bytes_by_path.clear()
            self._live_record_counts_by_path.clear()
            self._live_catalog_records_by_id.clear()
            self._catalog_record_id_by_path.clear()
            self._live_catalog_records_by_directory.clear()
            self._live_record_count = 0
            self._storage_available = False

    def try_reserve(self, byte_count: int) -> bool:
        """Atomically reserve quota headroom, returning false at the limit."""
        if byte_count < 0:
            raise ValueError("byte_count must be non-negative")
        with self._lock:
            if not self._storage_available:
                return False
            if self._spent_bytes + self._reserved_bytes + byte_count > self._limit_bytes:
                return False
            self._reserved_bytes += byte_count
            return True

    def commit(
        self,
        reserved: int,
        actual: int,
        *,
        relative_path: str | None = None,
        catalog_record: CatalogRecord | None = None,
    ) -> None:
        """Replace one reservation with the actual successfully written bytes."""
        if reserved < 0 or actual < 0:
            raise ValueError("reserved and actual must be non-negative")
        if actual > reserved:
            raise ValueError("actual bytes cannot exceed the reservation")
        if catalog_record is not None:
            if catalog_record.bytes != actual:
                raise ValueError("catalog record bytes must equal committed bytes")
            if relative_path is not None and relative_path != catalog_record.relative_path:
                raise ValueError("catalog record path must equal committed relative_path")
            relative_path = catalog_record.relative_path
        with self._lock:
            if reserved > self._reserved_bytes:
                raise RuntimeError("commit exceeds outstanding SpillQuota reservations")
            if catalog_record is not None and (
                catalog_record.record_id in self._live_catalog_records_by_id
                or catalog_record.relative_path in self._catalog_record_id_by_path
            ):
                raise RuntimeError("catalog record is already committed to SpillQuota")
            self._reserved_bytes -= reserved
            self._spent_bytes += actual
            if relative_path:
                self._available_relative_paths.add(relative_path)
                self._committed_bytes_by_path[relative_path] = actual
                self._live_record_counts_by_path[relative_path] = (
                    self._live_record_counts_by_path.get(relative_path, 0) + 1
                )
                self._live_record_count += 1
            if catalog_record is not None:
                self._live_catalog_records_by_id[catalog_record.record_id] = catalog_record
                self._catalog_record_id_by_path[catalog_record.relative_path] = catalog_record.record_id
                directory = Path(catalog_record.relative_path).parent.as_posix()
                self._live_catalog_records_by_directory.setdefault(directory, {})[catalog_record.record_id] = (
                    catalog_record
                )

    def reclaim(self, byte_count: int, *, relative_path: str | None = None) -> None:
        """Return committed quota after live spill records are evicted."""
        if byte_count < 0:
            raise ValueError("byte_count must be non-negative")
        with self._lock:
            if byte_count > self._spent_bytes:
                raise RuntimeError("reclaim exceeds committed SpillQuota bytes")
            self._spent_bytes -= byte_count
            if relative_path:
                self._available_relative_paths.discard(relative_path)
                self._committed_bytes_by_path.pop(relative_path, None)
                self._decrement_live_record(relative_path)

    def reclaim_record(self, relative_path: str) -> int:
        """Return the bytes actually accounted to one evicted record."""
        with self._lock:
            byte_count = self._committed_bytes_by_path.pop(relative_path, 0)
            if byte_count > self._spent_bytes:
                raise RuntimeError("record reclaim exceeds committed SpillQuota bytes")
            self._spent_bytes -= byte_count
            self._available_relative_paths.discard(relative_path)
            self._decrement_live_record(relative_path)
            return byte_count

    def is_record_available(self, relative_path: str) -> bool:
        """Return current in-memory availability for one cataloged record."""
        with self._lock:
            return relative_path in self._available_relative_paths

    def has_catalog_record_id(self, record_id: str) -> bool:
        """Return whether one record ID is live anywhere in this session's catalog."""
        with self._lock:
            return record_id in self._live_catalog_records_by_id

    def live_record_count(self, *, excluded_relative_paths: Iterable[str] = ()) -> int:
        """Return the in-memory live catalog count, excluding active-turn records."""
        excluded = set(excluded_relative_paths)
        with self._lock:
            return self._live_record_count - sum(
                self._live_record_counts_by_path.get(relative_path, 0) for relative_path in excluded
            )

    def live_catalog_records(self) -> tuple[CatalogRecord, ...]:
        """Return the ordered in-memory mirror of live authoritative catalog records."""
        with self._lock:
            return tuple(self._live_catalog_records_by_id.values())

    def catalog_records_for_directories(self, relative_dirs: Iterable[Path]) -> tuple[CatalogRecord, ...]:
        """Return live records belonging only to the requested projection directories."""
        directory_keys = {Path(relative_dir).as_posix() for relative_dir in relative_dirs}
        with self._lock:
            return tuple(
                record
                for directory in sorted(directory_keys)
                for record in self._live_catalog_records_by_directory.get(directory, {}).values()
            )

    def release(self, reserved: int) -> None:
        """Release a reservation after a clean failed or skipped write."""
        if reserved < 0:
            raise ValueError("reserved must be non-negative")
        with self._lock:
            if reserved > self._reserved_bytes:
                raise RuntimeError("release exceeds outstanding SpillQuota reservations")
            self._reserved_bytes -= reserved

    def _decrement_live_record(self, relative_path: str) -> None:
        count = self._live_record_counts_by_path.get(relative_path, 0)
        if not count:
            return
        self._live_record_count -= 1
        if count <= 1:
            self._live_record_counts_by_path.pop(relative_path, None)
        else:
            self._live_record_counts_by_path[relative_path] = count - 1
        record_id = self._catalog_record_id_by_path.pop(relative_path, None)
        if record_id is not None:
            record = self._live_catalog_records_by_id.pop(record_id, None)
            if record is not None:
                directory = Path(record.relative_path).parent.as_posix()
                directory_records = self._live_catalog_records_by_directory.get(directory)
                if directory_records is not None:
                    directory_records.pop(record_id, None)
                    if not directory_records:
                        self._live_catalog_records_by_directory.pop(directory, None)


def turn_directory_name(absolute_turn: int) -> str:
    """Return the zero-padded directory name for one absolute turn."""
    if absolute_turn < 0:
        raise ValueError("absolute_turn must be non-negative")
    return f"turn{absolute_turn:03d}"


def dropped_turn_relative_path(absolute_turn: int) -> Path:
    """Return ``compactions/dropped/turn<NNN>`` relative to a session root."""
    return Path(COMPACTIONS_DIR_NAME) / DROPPED_DIR_NAME / turn_directory_name(absolute_turn)


def sub_agent_dropped_turn_relative_path(tool_name: str, invocation_id: str, absolute_turn: int) -> Path:
    """Return the isolated dropped-record directory for one sub-agent invocation."""
    return (
        Path(COMPACTIONS_DIR_NAME)
        / SUB_AGENTS_DIR_NAME
        / safe_platform_basename(tool_name, default="agent")
        / safe_platform_basename(invocation_id, default="invocation")
        / DROPPED_DIR_NAME
        / turn_directory_name(absolute_turn)
    )


def build_record_filename(sequence: int, tool_name: str, record_id: str) -> str:
    """Build ``<seq>_<bounded-tool>_<record-id>.md`` within 255 UTF-8 bytes."""
    if sequence < 0:
        raise ValueError("sequence must be non-negative")
    if not _RECORD_ID_RE.fullmatch(record_id):
        raise ValueError(f"record_id must be {RECORD_ID_HEX_CHARS} lowercase hexadecimal characters")
    prefix = f"{sequence:03d}_"
    suffix = f"_{record_id}{RECORD_FILE_SUFFIX}"
    stem = bounded_safe_stem(
        tool_name,
        prefix=prefix,
        suffix=suffix,
        default="tool",
    )
    return f"{prefix}{stem}{suffix}"


_RECORD_ID_MAX_ATTEMPTS = 64


def _generate_record_id(quota: SpillQuota) -> str | None:
    """Draw one short record ID that no live catalog record already uses.

    Eight hex characters span a 32-bit space, so a fresh draw practically
    always succeeds immediately; the attempt cap only guards against a broken
    entropy source. Checking the quota's session-wide live map keeps the
    truncated IDs collision-free across record directories — the same map
    :meth:`SpillQuota.commit` enforces uniqueness against.
    """
    for _ in range(_RECORD_ID_MAX_ATTEMPTS):
        record_id = uuid4().hex[:RECORD_ID_HEX_CHARS]
        if not quota.has_catalog_record_id(record_id):
            return record_id
    return None


def write_spill_batch(
    session_root: Path | None,
    quota: SpillQuota | None,
    groups: Sequence[ScopedGroup],
    *,
    record_dir: Path,
    absolute_turn: int,
    round_number: int,
    session_id: str,
    superseded_note: str | None = None,
) -> SpillBatchResult:
    """Write one ordered spill batch and flush catalog projections.

    This synchronous API is designed to run under ``asyncio.to_thread``. A
    missing session root/quota is a storage-unavailable policy outcome rather
    than an I/O exception; actual filesystem failures set the durability signal.

    ``superseded_note`` archives the LAST_WORDS note this round's merge is
    replacing. Unlike group records it is strictly best-effort: its substance
    already survives inside the merged replacement, so a failed write is
    logged, produces no manifest entry, and never sets the durability signal.
    """
    if session_root is None or quota is None or not quota.storage_available:
        entries = _policy_entries(
            groups,
            record_dir=record_dir,
            absolute_turn=absolute_turn,
            round_number=round_number,
            reason="storage unavailable",
        )
        return SpillBatchResult(entries=tuple(entries))

    entries: list[SpillManifestItem] = []
    protected_record_ids: set[str] = set()
    evicted_relative_paths: set[str] = set()
    dirty_projection_dirs: set[Path] = set()
    projection_dirty = False
    round_bytes = 0
    round_cap_reached = False
    unexpected = False
    root = session_root.resolve(strict=False)
    with quota.catalog_lock:
        for sequence, group in enumerate(groups, start=1):
            if round_cap_reached:
                entries.extend(
                    _group_manifest_items(
                        group,
                        record_dir=record_dir,
                        relative_path="",
                        record_id="",
                        absolute_turn=absolute_turn,
                        round_number=round_number,
                        sequence=sequence,
                        no_record_reason="round cap",
                    )
                )
                continue

            created_at = datetime.now(UTC).isoformat()
            record_id = _generate_record_id(quota)
            if record_id is None:
                logger.warning("Spill record ID space exhausted; skipping one record")
                entries.extend(
                    _group_manifest_items(
                        group,
                        record_dir=record_dir,
                        relative_path="",
                        record_id="",
                        absolute_turn=absolute_turn,
                        round_number=round_number,
                        sequence=sequence,
                        no_record_reason="record ID exhaustion",
                    )
                )
                continue
            tool = _group_tool_name(group)
            payload = _render_record(
                group,
                record_id=record_id,
                absolute_turn=absolute_turn,
                round_number=round_number,
                created_at=created_at,
                session_id=session_id,
            )
            if round_bytes + len(payload) > PER_ROUND_CAP_BYTES:
                round_cap_reached = True
                entries.extend(
                    _group_manifest_items(
                        group,
                        record_dir=record_dir,
                        relative_path="",
                        record_id="",
                        absolute_turn=absolute_turn,
                        round_number=round_number,
                        sequence=sequence,
                        no_record_reason="round cap",
                    )
                )
                continue

            try:
                reserved = len(payload)
                reservation_acquired = quota.try_reserve(reserved)
                if not reservation_acquired and quota.storage_available:
                    evicted_count = len(evicted_relative_paths)
                    try:
                        _evict_until_reservable(
                            root,
                            quota,
                            reserved,
                            protected_record_ids=protected_record_ids,
                            evicted_relative_paths=evicted_relative_paths,
                        )
                    finally:
                        if len(evicted_relative_paths) > evicted_count:
                            projection_dirty = True
                            dirty_projection_dirs.update(Path(path).parent for path in evicted_relative_paths)
                    reservation_acquired = quota.try_reserve(reserved)
                if not reservation_acquired:
                    entries.extend(
                        _group_manifest_items(
                            group,
                            record_dir=record_dir,
                            relative_path="",
                            record_id="",
                            absolute_turn=absolute_turn,
                            round_number=round_number,
                            sequence=sequence,
                            no_record_reason="quota",
                        )
                    )
                    continue
            except OSError, RuntimeError:
                logger.exception("Unexpected spill eviction/catalog failure")
                unexpected = True
                entries.extend(
                    _group_manifest_items(
                        group,
                        record_dir=record_dir,
                        relative_path="",
                        record_id="",
                        absolute_turn=absolute_turn,
                        round_number=round_number,
                        sequence=sequence,
                        no_record_reason="I/O failure",
                    )
                )
                continue

            written_path: Path | None = None
            try:
                relative_path = record_dir / build_record_filename(sequence, tool, record_id)
                full_path = root / relative_path
                if not _record_path_within_root(root, relative_path):
                    raise OSError(f"Unsafe spill record path: {relative_path}")
                full_path.parent.mkdir(parents=True, exist_ok=True)
                written_path, record_id = _exclusive_write_with_retry(
                    full_path,
                    sequence=sequence,
                    tool=tool,
                    payload=payload,
                    original_record_id=record_id,
                    rerender=lambda new_id, spill_group=group, timestamp=created_at: _render_record(
                        spill_group,
                        record_id=new_id,
                        absolute_turn=absolute_turn,
                        round_number=round_number,
                        created_at=timestamp,
                        session_id=session_id,
                    ),
                    regenerate_record_id=lambda: _generate_record_id(quota),
                )
                if written_path != full_path:
                    relative_path = written_path.relative_to(root)
                    payload = written_path.read_bytes()
                record = CatalogRecord(
                    record_id=record_id,
                    relative_path=relative_path.as_posix(),
                    turn=absolute_turn,
                    round=round_number,
                    tool=tool,
                    bytes=len(payload),
                    created_at=created_at,
                )
                _append_catalog_line(root, record.to_catalog_line())
            except OSError, UnicodeError:
                logger.exception("Unexpected spill record/catalog write failure")
                unexpected = True
                quota.release(reserved)
                if written_path is not None:
                    try:
                        written_path.unlink(missing_ok=True)
                    except OSError:
                        logger.debug("Failed to remove un-cataloged spill record %s", written_path, exc_info=True)
                entries.extend(
                    _group_manifest_items(
                        group,
                        record_dir=record_dir,
                        relative_path="",
                        record_id="",
                        absolute_turn=absolute_turn,
                        round_number=round_number,
                        sequence=sequence,
                        no_record_reason="I/O failure",
                    )
                )
                continue

            quota.commit(
                reserved,
                len(payload),
                relative_path=relative_path.as_posix(),
                catalog_record=record,
            )
            protected_record_ids.add(record_id)
            round_bytes += len(payload)
            projection_dirty = True
            dirty_projection_dirs.add(relative_path.parent)
            entries.extend(
                _group_manifest_items(
                    group,
                    record_dir=record_dir,
                    relative_path=relative_path.as_posix(),
                    record_id=record_id,
                    absolute_turn=absolute_turn,
                    round_number=round_number,
                    sequence=sequence,
                )
            )

        if superseded_note:
            note_entry = _write_note_record(
                root,
                quota,
                superseded_note,
                record_dir=record_dir,
                sequence=len(groups) + 1,
                absolute_turn=absolute_turn,
                round_number=round_number,
                session_id=session_id,
            )
            if note_entry is not None:
                entries.append(note_entry)
                projection_dirty = True
                dirty_projection_dirs.add(Path(note_entry.relative_path).parent)

        if projection_dirty:
            try:
                _rebuild_manifest_projections(
                    root,
                    live_records=quota.catalog_records_for_directories(dirty_projection_dirs),
                    relative_dirs=dirty_projection_dirs,
                )
            except OSError, RuntimeError:
                # Records and authoritative catalog lines are already durable.
                # Keep the usable stubs, but signal the unexpected projection
                # failure so the strategy applies its recovery-sidecar policy.
                logger.exception("Unexpected spill manifest projection failure")
                unexpected = True
            durable_directories = {root / CATALOG_RELATIVE_PATH.parent}
            durable_directories.update((root / Path(path)).parent for path in evicted_relative_paths)
            durable_directories.update(
                (root / Path(entry.relative_path)).parent for entry in entries if entry.relative_path
            )
            _fsync_directory_chains(root, durable_directories)

    return SpillBatchResult(
        entries=tuple(entries),
        unexpected_io_failure=unexpected,
        evicted_relative_paths=frozenset(evicted_relative_paths),
    )


def _write_note_record(
    root: Path,
    quota: SpillQuota,
    note_text: str,
    *,
    record_dir: Path,
    sequence: int,
    absolute_turn: int,
    round_number: int,
    session_id: str,
) -> SpillManifestItem | None:
    """Archive one superseded LAST_WORDS note under the round's record dir.

    Caller holds ``quota.catalog_lock``. Best-effort by contract: the note's
    substance already lives on inside the merged replacement, so every failure
    (ID exhaustion, quota pressure, I/O) is logged and swallowed without
    setting the batch durability signal, and quota pressure never evicts older
    records to make room for a note copy.
    """
    created_at = datetime.now(UTC).isoformat()
    record_id = _generate_record_id(quota)
    if record_id is None:
        logger.warning("Spill record ID space exhausted; skipping LAST_WORDS note record")
        return None
    payload = _render_note_record(
        note_text,
        record_id=record_id,
        absolute_turn=absolute_turn,
        round_number=round_number,
        created_at=created_at,
        session_id=session_id,
    )
    reserved = len(payload)
    if not quota.try_reserve(reserved):
        logger.debug("Session spill quota exhausted; skipping LAST_WORDS note record")
        return None
    written_path: Path | None = None
    try:
        relative_path = record_dir / build_record_filename(sequence, NOTE_RECORD_TOOL_NAME, record_id)
        full_path = root / relative_path
        if not _record_path_within_root(root, relative_path):
            raise OSError(f"Unsafe spill record path: {relative_path}")
        full_path.parent.mkdir(parents=True, exist_ok=True)
        written_path, record_id = _exclusive_write_with_retry(
            full_path,
            sequence=sequence,
            tool=NOTE_RECORD_TOOL_NAME,
            payload=payload,
            original_record_id=record_id,
            rerender=lambda new_id, timestamp=created_at: _render_note_record(
                note_text,
                record_id=new_id,
                absolute_turn=absolute_turn,
                round_number=round_number,
                created_at=timestamp,
                session_id=session_id,
            ),
            regenerate_record_id=lambda: _generate_record_id(quota),
        )
        if written_path != full_path:
            relative_path = written_path.relative_to(root)
            payload = written_path.read_bytes()
        record = CatalogRecord(
            record_id=record_id,
            relative_path=relative_path.as_posix(),
            turn=absolute_turn,
            round=round_number,
            tool=NOTE_RECORD_TOOL_NAME,
            bytes=len(payload),
            created_at=created_at,
            kind=CATALOG_KIND_NOTE,
        )
        _append_catalog_line(root, record.to_catalog_line())
    except OSError, UnicodeError:
        logger.warning("Failed to archive the superseded LAST_WORDS note", exc_info=True)
        quota.release(reserved)
        if written_path is not None:
            try:
                written_path.unlink(missing_ok=True)
            except OSError:
                logger.debug("Failed to remove un-cataloged spill record %s", written_path, exc_info=True)
        return None
    quota.commit(reserved, len(payload), relative_path=relative_path.as_posix(), catalog_record=record)
    return SpillManifestItem(
        record_id=record_id,
        # Synthetic marker: last_words_state.ManifestEntry.from_state requires a
        # non-empty group_id, and an empty one would silently drop the entry
        # from the rendered manifest.
        group_id=NOTE_RECORD_GROUP_ID,
        record_dir=record_dir.as_posix(),
        relative_path=relative_path.as_posix(),
        turn=absolute_turn,
        round=round_number,
        sequence=sequence,
        tool=NOTE_RECORD_TOOL_NAME,
        display_argument="superseded by this round's note",
        outcome="merged",
        size_chars=len(note_text),
    )


def catalog_live_records(session_root: Path, quota: SpillQuota | None = None) -> list[CatalogRecord]:
    """Return live records after applying append-only tombstones."""
    root = session_root.resolve(strict=False)
    if quota is None:
        return [record for record in _read_live_catalog(root) if _record_path_within_root(root, record.relative_path)]
    with quota.catalog_lock:
        return [record for record in _read_live_catalog(root) if _record_path_within_root(root, record.relative_path)]


def catalog_live_record_count(session_root: Path, quota: SpillQuota | None = None) -> int:
    """Return the number of live catalog records for a turn-start snapshot."""
    return len(catalog_live_records(session_root, quota))


def owned_record_directory_relative_path(value: str | Path) -> bool:
    """Return whether *value* is one of the two owned spill directory layouts."""
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts and _safe_record_directory_relative_path(path)


def owned_record_relative_path(value: str | Path) -> bool:
    """Return whether *value* is an owned session-relative spill record path."""
    path = Path(value)
    return _safe_record_relative_path(path.as_posix())


def available_record_path(session_root: Path, relative_path: str | Path) -> bool:
    """Return whether an owned record currently resolves to a regular session file."""
    try:
        root = session_root.resolve(strict=False)
    except OSError, RuntimeError:
        return False
    if not _record_path_within_root(root, relative_path):
        return False
    try:
        return (root / relative_path).is_file()
    except OSError:
        return False


def catalog_path_for_read(session_root: Path) -> Path | None:
    """Return the readable catalog path only when every component is session-owned."""
    try:
        root = session_root.resolve(strict=False)
        catalog = _validated_catalog_path(root)
        resolved_catalog = catalog.resolve(strict=True)
        if not resolved_catalog.is_file() or resolved_catalog != catalog:
            return None
    except OSError, RuntimeError:
        return None
    return catalog


def reconcile_spill_storage(session_root: Path, quota: SpillQuota) -> SpillReconciliationResult:
    """Delete un-cataloged records, rebuild projections, and initialize quota."""
    root = session_root.resolve(strict=False)
    root.mkdir(parents=True, exist_ok=True)
    with quota.catalog_lock:
        try:
            live = [
                record for record in _read_live_catalog(root) if _record_path_within_root(root, record.relative_path)
            ]
        except _UnsafeCatalogPathError:
            # An unsafe catalog cannot be the source of truth for orphan
            # deletion. Preserve the on-disk records, expose none as
            # available, and disable future spill writes so they cannot follow
            # the link or consume unaccounted quota.
            logger.warning("Ignoring unsafe spill catalog path under %s", root)
            quota.disable_storage()
            return SpillReconciliationResult(0, 0, frozenset())
        live_paths = {record.relative_path for record in live}
        deleted = 0
        compactions = root / COMPACTIONS_DIR_NAME
        if compactions.is_dir():
            for dropped_dir in compactions.rglob(DROPPED_DIR_NAME):
                try:
                    relative_dropped_dir = dropped_dir.relative_to(root)
                except ValueError:
                    continue
                if not _dropped_directory_within_root(root, relative_dropped_dir) or not dropped_dir.is_dir():
                    continue
                for candidate in dropped_dir.rglob(f"*{RECORD_FILE_SUFFIX}"):
                    if candidate.name == MANIFEST_FILE_NAME:
                        continue
                    try:
                        relative = candidate.relative_to(root).as_posix()
                    except ValueError:
                        continue
                    if not _record_path_within_root(root, relative):
                        continue
                    if relative in live_paths:
                        continue
                    candidate.unlink(missing_ok=True)
                    deleted += 1

        available: set[str] = set()
        committed_bytes_by_path: dict[str, int] = {}
        total = 0
        for record in live:
            path = root / record.relative_path
            try:
                if not path.is_file():
                    continue
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            available.add(record.relative_path)
            committed_bytes_by_path[record.relative_path] = size
            total += size
        quota.initialize(
            total,
            available,
            committed_bytes_by_path=committed_bytes_by_path,
            live_catalog_records=live,
        )
        try:
            _rebuild_manifest_projections(root, live_records=live)
        except OSError, RuntimeError:
            # The append-only catalog and records are authoritative. A stale
            # convenience projection must not prevent session hydration.
            logger.warning("Unable to rebuild spill manifest projections under %s", root, exc_info=True)
    return SpillReconciliationResult(
        live_total_bytes=total,
        live_record_count=len(live),
        available_relative_paths=frozenset(available),
        deleted_orphans=deleted,
    )


def _exclusive_write_with_retry(
    path: Path,
    *,
    sequence: int,
    tool: str,
    payload: bytes,
    original_record_id: str,
    rerender: Any,
    regenerate_record_id: Any,
) -> tuple[Path, str]:
    record_id = original_record_id
    candidate = path
    candidate_payload = payload
    os_module = cast(Any, os)
    for attempt in range(2):
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if get_platform().is_windows:
                flags |= os_module.O_BINARY
            else:
                # O_EXCL rejects existing final components; O_NOFOLLOW makes
                # that intent explicit for platforms that support symlinks.
                flags |= os_module.O_NOFOLLOW
            descriptor = os.open(candidate, flags, 0o600)
        except FileExistsError:
            if attempt:
                raise
            new_id = regenerate_record_id()
            if new_id is None:
                raise
            record_id = new_id
            candidate = path.with_name(build_record_filename(sequence, tool, record_id))
            candidate_payload = rerender(record_id)
            continue
        try:
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                output.write(candidate_payload)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                logger.debug("Failed to remove partial spill record %s", candidate, exc_info=True)
            raise
        return candidate, record_id
    raise RuntimeError("exclusive spill record retry exhausted")


def _render_record(
    group: ScopedGroup,
    *,
    record_id: str,
    absolute_turn: int,
    round_number: int,
    created_at: str,
    session_id: str,
) -> bytes:
    lines = [
        "# Dropped context record",
        "",
        f"- Record ID: `{record_id}`",
        f"- Group ID: `{group.group_id}`",
        f"- Turn: {absolute_turn}",
        f"- Round: {round_number}",
        f"- Timestamp: {created_at}",
        f"- Session ID: `{session_id}`",
        "",
    ]
    calls = _group_calls(group)
    assistant_context = _assistant_group_text(group)
    matched_result_ids: set[int] = set()
    if calls:
        if assistant_context:
            lines.extend(["## Assistant context", "", assistant_context, ""])
        results = _group_results(group)
        for call in calls:
            result = _matching_result(call, results)
            if result is not None:
                matched_result_ids.add(id(result))
            stamp = execution_stamp_from_metadata(result.additional_properties) if result is not None else None
            requested = _requested_call_arguments(call)
            effective = stamp.get("effective_args") if stamp is not None else None
            lines.extend([f"## Call: {_call_tool_name(call)}", ""])
            if stamp is not None and effective is not None and _effective_arguments_differ(requested, stamp):
                lines.extend(
                    [
                        "### Arguments (requested)",
                        "```json",
                        _pretty_arguments(requested),
                        "```",
                        "",
                        "### Arguments (effective)",
                        "```json",
                        _pretty_arguments(effective),
                        "```",
                        "",
                    ]
                )
            else:
                label = "### Arguments (as requested)" if stamp is None else "### Arguments (requested/effective)"
                lines.extend([label, "```json", _pretty_arguments(requested), "```", ""])
            lines.extend([f"Outcome: `{_structured_outcome(result, stamp)}`", "", "## Result", ""])
            lines.append(_result_record_text(result) if result is not None else "(missing result)")
            lines.append("")
    else:
        lines.extend(["## Assistant text", "", assistant_context, ""])
    supplemental = _supplemental_group_contents(group, matched_result_ids)
    if supplemental:
        lines.extend(["## Additional group content", ""])
        for content in supplemental:
            lines.extend([f"### {content.type}", "", _render_result_value(content) or "(empty)", ""])
    lines.append(_RECORD_TERMINATOR.rstrip("\n"))
    return _cap_record_bytes("\n".join(lines) + "\n")


def _render_note_record(
    note_text: str,
    *,
    record_id: str,
    absolute_turn: int,
    round_number: int,
    created_at: str,
    session_id: str,
) -> bytes:
    lines = [
        "# Superseded LAST_WORDS note",
        "",
        f"- Record ID: `{record_id}`",
        f"- Turn: {absolute_turn}",
        f"- Round: {round_number}",
        f"- Timestamp: {created_at}",
        f"- Session ID: `{session_id}`",
        "",
        "This compaction round merged the note below into its replacement; this",
        "file preserves the pre-merge original verbatim.",
        "",
        "## Note",
        "",
        note_text,
        "",
        _RECORD_TERMINATOR.rstrip("\n"),
    ]
    return _cap_record_bytes("\n".join(lines) + "\n")


def _cap_record_bytes(text: str) -> bytes:
    # Tool inputs/results can contain POSIX surrogate-escaped path bytes.
    # Preserve those as visible escape sequences so every ``.md`` record
    # remains strict UTF-8 and ``read_file`` never replaces archived detail.
    raw = text.encode("utf-8", errors="backslashreplace")
    if len(raw) <= PER_RECORD_CAP_BYTES:
        return raw
    terminator = _RECORD_TERMINATOR.encode()
    marker = _RECORD_TRUNCATION_TEMPLATE.format(size=len(raw)).encode("utf-8")
    body = raw.removesuffix(terminator)
    keep = max(0, PER_RECORD_CAP_BYTES - len(marker) - len(terminator))
    head_size = (keep + 1) // 2
    tail_size = keep - head_size
    head = body[:head_size].decode("utf-8", errors="ignore").encode("utf-8")
    tail = body[len(body) - tail_size :].decode("utf-8", errors="ignore").encode("utf-8") if tail_size else b""
    capped = head + marker + tail + terminator
    if len(capped) > PER_RECORD_CAP_BYTES:
        capped = capped[: PER_RECORD_CAP_BYTES - len(terminator)] + terminator
    return capped


def _append_catalog_line(root: Path, value: Mapping[str, Any]) -> None:
    # Validate before mkdir so an existing symlinked parent cannot redirect
    # directory creation, then validate again immediately before opening.
    catalog = _validated_catalog_path(root)
    catalog.parent.mkdir(parents=True, exist_ok=True)
    catalog = _validated_catalog_path(root)
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if get_platform().is_windows:
        with catalog.open("a+b") as output:
            _append_catalog_payload(output, serialized)
        return

    # O_NOFOLLOW closes the final-component race left by the resolved-path
    # check on POSIX. Keep the platform-only flag inside the guarded body so
    # this module remains importable on Windows.
    posix_os = cast(Any, os)
    descriptor = posix_os.open(
        catalog,
        posix_os.O_APPEND | posix_os.O_CREAT | posix_os.O_RDWR | posix_os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "r+b") as output:
            descriptor = -1
            _append_catalog_payload(output, serialized)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _append_catalog_payload(output: BinaryIO, serialized: bytes) -> None:
    """Append one isolated JSON line after any crash-torn final fragment."""
    output.seek(0, os.SEEK_END)
    size = output.tell()
    separator = b""
    if size:
        output.seek(-1, os.SEEK_END)
        if output.read(1) != b"\n":
            separator = b"\n"
    output.seek(0, os.SEEK_END)
    output.write(separator + serialized + b"\n")
    output.flush()
    os.fsync(output.fileno())


def _fsync_directory_chains(root: Path, directories: Iterable[Path]) -> None:
    """Persist newly created or removed entries from spill leaves through the session root."""
    pending: set[Path] = set()
    for directory in directories:
        current = directory
        while current.is_relative_to(root):
            pending.add(current)
            if current == root:
                break
            current = current.parent
    for directory in sorted(pending, key=lambda path: len(path.parts), reverse=True):
        fsync_directory(directory)


def _read_live_catalog(root: Path) -> list[CatalogRecord]:
    catalog = _validated_catalog_path(root)
    try:
        if get_platform().is_windows:
            payload = catalog.read_bytes()
        else:
            posix_os = cast(Any, os)
            descriptor = posix_os.open(catalog, posix_os.O_RDONLY | posix_os.O_NOFOLLOW)
            try:
                with os.fdopen(descriptor, "rb") as input_file:
                    descriptor = -1
                    payload = input_file.read()
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
    except FileNotFoundError:
        return []
    lines = payload.splitlines()
    # ``live`` doubles as the replay order: dict insertion order matches the
    # catalog semantics exactly — the first add of an ID claims a position, a
    # tombstone erases it, and a later re-add (including a reused tombstoned
    # ID) appends at the end as the newest record. This keeps replay O(1) per
    # line where an explicit order list would make tombstones quadratic.
    live: dict[str, CatalogRecord] = {}
    record_id_by_path: dict[str, str] = {}
    for line_number, raw_line in enumerate(lines, start=1):
        try:
            line = raw_line.decode("utf-8")
        except UnicodeDecodeError:
            logger.debug("Ignoring invalid UTF-8 spill catalog line %d", line_number)
            continue
        try:
            value = json.loads(line)
        except TypeError, ValueError:
            logger.debug("Ignoring malformed spill catalog line %d", line_number)
            continue
        if not isinstance(value, Mapping):
            continue
        record_id = value.get("record_id")
        if not isinstance(record_id, str) or not record_id:
            continue
        if value.get("tombstone") is True:
            live.pop(record_id, None)
            continue
        if record_id in live:
            logger.debug(
                "Ignoring duplicate spill catalog record ID %s on line %d",
                record_id,
                line_number,
            )
            continue
        record = _catalog_record_from_mapping(value)
        if record is None:
            continue
        path_owner = record_id_by_path.get(record.relative_path)
        if path_owner is not None and path_owner != record_id:
            logger.debug(
                "Ignoring duplicate spill catalog path on line %d (owned by record %s)",
                line_number,
                path_owner,
            )
            continue
        record_id_by_path[record.relative_path] = record_id
        live[record_id] = record
    return list(live.values())


def _validated_catalog_path(root: Path) -> Path:
    """Return the fixed catalog path only when every component is session-owned."""
    catalog = root / CATALOG_RELATIVE_PATH
    try:
        has_link_component = _path_has_link_component(root, CATALOG_RELATIVE_PATH)
        parent = catalog.parent.resolve(strict=False)
        resolved = catalog.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise _UnsafeCatalogPathError(f"Cannot validate spill catalog path: {catalog}") from exc
    if has_link_component or not parent.is_relative_to(root) or not resolved.is_relative_to(root):
        raise _UnsafeCatalogPathError(f"Unsafe spill catalog path: {catalog}")
    return catalog


def _catalog_record_from_mapping(value: Mapping[str, Any]) -> CatalogRecord | None:
    record_id = value.get("record_id")
    relative_path = value.get("relative_path")
    turn = value.get("turn")
    round_number = value.get("round")
    tool = value.get("tool")
    byte_count = value.get("bytes")
    created_at = value.get("created_at")
    if (
        not isinstance(record_id, str)
        or not record_id
        or not isinstance(relative_path, str)
        or not is_utf8_encodable(relative_path)
        or not _safe_record_relative_path(relative_path)
        or not isinstance(turn, int)
        or turn < 0
        or not isinstance(round_number, int)
        or round_number < 0
        or not isinstance(tool, str)
        or not is_utf8_encodable(record_id)
        or not is_utf8_encodable(tool)
        or not isinstance(byte_count, int)
        or byte_count < 0
        or not isinstance(created_at, str)
        or not is_utf8_encodable(created_at)
    ):
        return None
    # Tolerant by design: catalogs written before the field existed (and any
    # hand-edited line) fall back to the group kind rather than being dropped.
    kind = value.get("kind", CATALOG_KIND_GROUP)
    if not isinstance(kind, str) or not kind or not is_utf8_encodable(kind):
        kind = CATALOG_KIND_GROUP
    return CatalogRecord(record_id, relative_path, turn, round_number, tool, byte_count, created_at, kind)


def _safe_record_relative_path(value: str) -> bool:
    """Return whether a catalog path has one of the two spill-record layouts."""
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or path.name == MANIFEST_FILE_NAME:
        return False
    return _safe_record_directory_relative_path(path.parent) and path.suffix == RECORD_FILE_SUFFIX


def _safe_record_directory_relative_path(path: Path) -> bool:
    """Return whether *path* is an owned main/sub-agent turn directory."""
    parts = path.parts
    main_layout = (
        len(parts) == 3
        and parts[:2] == (COMPACTIONS_DIR_NAME, DROPPED_DIR_NAME)
        and _TURN_DIRECTORY_RE.fullmatch(parts[2]) is not None
    )
    sub_agent_layout = (
        len(parts) == 6
        and parts[0] == COMPACTIONS_DIR_NAME
        and parts[1] == SUB_AGENTS_DIR_NAME
        and bool(parts[2])
        and bool(parts[3])
        and parts[4] == DROPPED_DIR_NAME
        and _TURN_DIRECTORY_RE.fullmatch(parts[5]) is not None
    )
    return main_layout or sub_agent_layout


def _safe_dropped_directory_relative_path(path: Path) -> bool:
    """Return whether *path* is an owned main/sub-agent dropped-record root."""
    parts = path.parts
    main_layout = parts == (COMPACTIONS_DIR_NAME, DROPPED_DIR_NAME)
    sub_agent_layout = (
        len(parts) == 5
        and parts[0] == COMPACTIONS_DIR_NAME
        and parts[1] == SUB_AGENTS_DIR_NAME
        and bool(parts[2])
        and bool(parts[3])
        and parts[4] == DROPPED_DIR_NAME
    )
    return main_layout or sub_agent_layout


def _record_path_within_root(root: Path, relative_path: str | Path) -> bool:
    """Reject record targets redirected away from their owned lexical layout."""
    relative = Path(relative_path)
    value = relative.as_posix()
    if not _safe_record_relative_path(value):
        return False
    try:
        if _path_has_link_component(root, relative):
            return False
        resolved_relative = (root / relative).resolve(strict=False).relative_to(root)
    except OSError, RuntimeError, ValueError:
        return False
    return _safe_record_relative_path(resolved_relative.as_posix())


def _dropped_directory_within_root(root: Path, relative_dir: str | Path) -> bool:
    """Reject dropped-record roots redirected away from their owned lexical layout."""
    relative = Path(relative_dir)
    if not _safe_dropped_directory_relative_path(relative):
        return False
    try:
        if _path_has_link_component(root, relative):
            return False
        resolved_relative = (root / relative).resolve(strict=False).relative_to(root)
    except OSError, RuntimeError, ValueError:
        return False
    return _safe_dropped_directory_relative_path(resolved_relative)


def _record_directory_within_root(root: Path, relative_dir: str | Path) -> bool:
    """Reject spill directories redirected away from their owned lexical layout."""
    relative = Path(relative_dir)
    if not owned_record_directory_relative_path(relative):
        return False
    try:
        if _path_has_link_component(root, relative):
            return False
        resolved_relative = (root / relative).resolve(strict=False).relative_to(root)
    except OSError, RuntimeError, ValueError:
        return False
    return _safe_record_directory_relative_path(resolved_relative)


def _path_has_link_component(root: Path, relative_path: Path) -> bool:
    """Return whether an existing relative component is a symlink or junction."""
    current = root
    for part in relative_path.parts:
        current /= part
        if current.is_symlink() or current.is_junction():
            return True
    return False


def _evict_until_reservable(
    root: Path,
    quota: SpillQuota,
    required: int,
    *,
    protected_record_ids: set[str],
    evicted_relative_paths: set[str],
) -> None:
    for record in quota.live_catalog_records():
        if record.record_id in protected_record_ids:
            continue
        path_is_safe = _record_path_within_root(root, record.relative_path)
        path = root / record.relative_path
        _append_catalog_line(
            root,
            {
                "record_id": record.record_id,
                "tombstone": True,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        try:
            if path_is_safe:
                path.unlink(missing_ok=True)
        except OSError:
            # The tombstone is already authoritative. Restore the live catalog
            # entry when physical deletion failed so a later attempt can retry
            # without hiding the only record copy.
            try:
                _append_catalog_line(root, record.to_catalog_line())
            except OSError:
                logger.exception("Failed to restore spill catalog entry after eviction unlink failure")
            raise
        evicted_relative_paths.add(record.relative_path)
        quota.reclaim_record(record.relative_path)
        if quota.try_reserve(required):
            quota.release(required)
            return


def _rebuild_manifest_projections(
    root: Path,
    *,
    live_records: Sequence[CatalogRecord] | None = None,
    relative_dirs: Iterable[Path] | None = None,
) -> None:
    source = _read_live_catalog(root) if live_records is None else live_records
    live = [record for record in source if _record_path_within_root(root, record.relative_path)]
    grouped: dict[Path, list[CatalogRecord]] = {}
    for record in live:
        grouped.setdefault(Path(record.relative_path).parent, []).append(record)

    if relative_dirs is None:
        target_dirs = set(grouped)
        compactions = root / COMPACTIONS_DIR_NAME
        if compactions.is_dir():
            for path in compactions.rglob(MANIFEST_FILE_NAME):
                try:
                    relative_dir = path.parent.relative_to(root)
                except ValueError:
                    continue
                if _record_directory_within_root(root, relative_dir):
                    target_dirs.add(relative_dir)
    else:
        target_dirs = {
            Path(relative_dir)
            for relative_dir in relative_dirs
            if _record_directory_within_root(root, Path(relative_dir))
        }

    for relative_dir in sorted(target_dirs, key=lambda path: path.as_posix()):
        records = grouped.get(relative_dir, [])
        directory = root / relative_dir
        directory.mkdir(parents=True, exist_ok=True)
        if not records:
            atomic_write_text(directory / MANIFEST_FILE_NAME, "# Dropped context records\n\n(No live records.)\n")
            continue
        lines = ["# Dropped context records", ""]
        for record in records:
            filename = Path(record.relative_path).name
            lines.append(
                f"- r{record.round} `{record.tool}` — [{filename}]({filename}) — "
                f"{record.bytes} bytes — {record.created_at}"
            )
        lines.append("")
        atomic_write_text(directory / MANIFEST_FILE_NAME, "\n".join(lines))


def _policy_entries(
    groups: Sequence[ScopedGroup],
    *,
    record_dir: Path,
    absolute_turn: int,
    round_number: int,
    reason: str,
) -> list[SpillManifestItem]:
    entries: list[SpillManifestItem] = []
    for sequence, group in enumerate(groups, start=1):
        entries.extend(
            _group_manifest_items(
                group,
                record_dir=record_dir,
                relative_path="",
                record_id="",
                absolute_turn=absolute_turn,
                round_number=round_number,
                sequence=sequence,
                no_record_reason=reason,
            )
        )
    return entries


def _group_manifest_items(
    group: ScopedGroup,
    *,
    record_dir: Path,
    relative_path: str,
    record_id: str,
    absolute_turn: int,
    round_number: int,
    sequence: int,
    no_record_reason: str = "",
) -> list[SpillManifestItem]:
    calls = _group_calls(group)
    if not calls:
        text = _assistant_group_text(group)
        return [
            SpillManifestItem(
                record_id=record_id,
                group_id=group.group_id,
                record_dir=record_dir.as_posix(),
                relative_path=relative_path,
                turn=absolute_turn,
                round=round_number,
                sequence=sequence,
                tool="assistant",
                display_argument="",
                outcome="",
                size_chars=len(text),
                assistant_text=True,
                available=not no_record_reason,
                no_record_reason=no_record_reason,
            )
        ]
    results = _group_results(group)
    items: list[SpillManifestItem] = []
    for call in calls:
        result = _matching_result(call, results)
        stamp = execution_stamp_from_metadata(result.additional_properties) if result is not None else None
        items.append(
            SpillManifestItem(
                record_id=record_id,
                group_id=group.group_id,
                record_dir=record_dir.as_posix(),
                relative_path=relative_path,
                turn=absolute_turn,
                round=round_number,
                sequence=sequence,
                tool=_call_tool_name(call),
                display_argument=_display_argument(call, stamp),
                outcome=_structured_outcome(result, stamp),
                size_chars=len(_result_record_text(result)) if result is not None else 0,
                available=not no_record_reason,
                no_record_reason=no_record_reason,
            )
        )
    return items


def group_result_texts(group: ScopedGroup) -> list[str]:
    """The result text of each call of *group*, in the order its manifest items list the calls."""
    results = _group_results(group)
    return [_result_record_text(_matching_result(call, results)) for call in _group_calls(group)]


def _group_calls(group: ScopedGroup) -> list[Content]:
    return [
        content for message in group.messages for content in message.contents if content.type in TOOL_CALL_CONTENT_TYPES
    ]


def _group_results(group: ScopedGroup) -> list[Content]:
    return [content for message in group.messages for content in message.contents if content.type.endswith("_result")]


def _matching_result(call: Content, results: Sequence[Content]) -> Content | None:
    if call.call_id:
        return next((result for result in results if result.call_id == call.call_id), None)
    if call.type == "image_generation_tool_call" and call.image_id:
        return next(
            (
                result
                for result in results
                if result.type == "image_generation_tool_result" and result.image_id == call.image_id
            ),
            None,
        )
    return None


def _group_tool_name(group: ScopedGroup) -> str:
    calls = _group_calls(group)
    return _call_tool_name(calls[0]) if calls else "assistant"


def _call_tool_name(call: Content) -> str:
    if call.type == "function_call":
        value = call.name or "tool"
    else:
        value = call.tool_name or call.type.removesuffix("_tool_call") or "tool"
    return _strict_utf8_text(value)


def _strict_utf8_text(value: str) -> str:
    """Make provider-controlled labels safe for paths and strict UTF-8 state."""
    return value.encode("utf-8", errors="backslashreplace").decode("utf-8")


def _requested_call_arguments(call: Content) -> str:
    """Return the complete provider-visible input for every supported call shape."""
    if call.type in {"function_call", "hosted_tool_call", "mcp_server_tool_call", "search_tool_call"}:
        return _canonical_arguments(call.arguments)
    if call.type == "code_interpreter_tool_call":
        return _canonical_arguments({"inputs": _record_json_value(call.inputs or [])})
    if call.type == "image_generation_tool_call":
        return _canonical_arguments({"image_id": call.image_id})
    if call.type == "shell_tool_call":
        return _canonical_arguments(
            {
                "commands": call.commands or [],
                "max_output_length": call.max_output_length,
                "status": call.status,
                "timeout_ms": call.timeout_ms,
            }
        )
    return _canonical_arguments(_record_json_value(call.to_dict()))


def _record_json_value(value: Any) -> Any:
    """Convert nested call input to JSON state without embedding raw images."""
    if isinstance(value, Content):
        if is_image_content(value):
            return _image_placeholder(value)
        return _record_json_value(value.to_dict())
    if isinstance(value, Mapping):
        return {str(key): _record_json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_record_json_value(item) for item in value]
    return value


def _canonical_arguments(arguments: Any) -> str:
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except ValueError:
            return arguments
        return json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    try:
        return canonicalize_effective_arguments(arguments if arguments is not None else {})
    except Exception:
        return json.dumps(arguments, ensure_ascii=False, sort_keys=True, default=str)


def _pretty_arguments(serialized: str) -> str:
    try:
        parsed = json.loads(serialized)
    except ValueError:
        return serialized
    return json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True, default=str)


def _effective_arguments_differ(requested: str, stamp: Mapping[str, str]) -> bool:
    digest = hashlib.sha256(requested.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
    return digest != stamp.get("effective_args_digest")


def _structured_outcome(result: Content | None, stamp: Mapping[str, str] | None) -> str:
    if stamp is not None:
        if stamp.get("outcome") == "ok":
            return "ok"
        kind = stamp.get("error_kind")
        return f"error({kind})" if kind else "error(unknown)"
    if result is not None:
        kind = result.additional_properties.get(TOOL_ERROR_KIND_METADATA_KEY)
        if isinstance(kind, str) and kind:
            return f"error({kind})"
        shell_outcome = _shell_result_outcome(result)
        if shell_outcome is not None:
            return shell_outcome
    return "unknown"


def _shell_result_outcome(result: Content) -> str | None:
    if result.type != "shell_tool_result" or not result.outputs:
        return None
    outputs = [output for output in result.outputs if output.type == "shell_command_output"]
    if not outputs:
        return None
    if any(output.timed_out is True for output in outputs):
        return "error(timeout)"
    if any(output.exit_code is not None and output.exit_code != 0 for output in outputs):
        return "error(exit_code)"
    if all(output.exit_code == 0 for output in outputs):
        return "ok"
    return None


def _result_record_text(result: Content | None) -> str:
    if result is None:
        return ""
    if result.type in {"function_result", "hosted_tool_result", "search_tool_result"}:
        value = result.items or result.result
    elif result.type == "mcp_server_tool_result":
        value = result.output
    else:
        value = result.outputs
    return _render_result_value(value)


def _render_result_value(value: Any) -> str:
    if isinstance(value, Content):
        if is_image_content(value):
            return _image_placeholder(value)
        if value.type == "text":
            return value.text or ""
        if value.type == "shell_command_output":
            return _render_shell_command_output(value)
        return json.dumps(value.to_dict(), ensure_ascii=False, indent=2, default=str)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return "\n".join(part for item in value if (part := _render_result_value(item)))
    if isinstance(value, Mapping | list):
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    return "" if value is None else str(value)


def _render_shell_command_output(output: Content) -> str:
    blocks: list[str] = []
    if output.stdout is not None:
        blocks.append(f"[stdout]\n{output.stdout}")
    if output.stderr is not None:
        blocks.append(f"[stderr]\n{output.stderr}")
    if output.exit_code is not None:
        blocks.append(f"[exit_code]\n{output.exit_code}")
    if output.timed_out is not None:
        blocks.append(f"[timed_out]\n{str(output.timed_out).lower()}")
    return "\n\n".join(blocks)


def _image_placeholder(content: Content) -> str:
    uri = content.uri or ""
    media_type = content.media_type or content.additional_properties.get("media_type") or "image"
    byte_length, identity = hashable_image_identity(uri)
    if uri.startswith("data:"):
        encoded = uri.split(",", 1)[1] if "," in uri else uri
        try:
            payload = base64.b64decode(encoded, validate=False)
        except ValueError, TypeError:
            payload = identity.encode("ascii", errors="ignore")
        digest = hashlib.sha256(payload).hexdigest()[:16]
    else:
        digest = hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()[:16]
    size = str(byte_length) if byte_length is not None else "unknown bytes"
    return f"[image: {media_type}, {size}, sha256:{digest}]"


def _assistant_group_text(group: ScopedGroup) -> str:
    blocks: list[str] = []
    for message in group.messages:
        for content in message.contents:
            if content.type == "text" and content.text:
                blocks.append(content.text)
            elif content.type == "text_reasoning":
                # Chat Completions ``reasoning_details`` text is a readable copy of the payload written below.
                details = content.additional_properties.get("openai_reasoning_format") == "reasoning_details"
                if content.text and not details:
                    blocks.append(f"[reasoning]\n{content.text}")
                if content.protected_data:
                    blocks.append(f"[protected reasoning data]\n{content.protected_data}")
    return "\n".join(blocks)


def _supplemental_group_contents(group: ScopedGroup, matched_result_ids: set[int]) -> list[Content]:
    return [
        content
        for message in group.messages
        for content in message.contents
        if content.type not in TOOL_CALL_CONTENT_TYPES
        and content.type not in {"text", "text_reasoning"}
        and id(content) not in matched_result_ids
    ]


_DISPLAY_ARG_KEYS_BY_TOOL: dict[str, tuple[str, ...]] = {
    "read_file": ("path", "file_path"),
    "view_image": ("path", "file_path"),
    "write_file": ("path", "file_path"),
    "edit_file": ("path", "file_path"),
    "grep": ("pattern", "path"),
    "glob": ("pattern", "path"),
    "execute": ("command",),
    "run_command": ("command",),
    "run_shell": ("command",),
    "bash": ("command",),
    "cmd": ("command",),
    "csh": ("command",),
    "dash": ("command",),
    "fish": ("command",),
    "git_bash": ("command",),
    "ksh": ("command",),
    "powershell": ("command",),
    "pwsh": ("command",),
    "sh": ("command",),
    "tcsh": ("command",),
    "zsh": ("command",),
    "shell": ("commands",),
    "code_interpreter": ("inputs",),
    "image_generation": ("image_id",),
    "web_search": ("query",),
    "search": ("query",),
    "fetch": ("url",),
    "open_url": ("url",),
    "compress_context": ("marker_id",),
    "recall_context": ("name", "marker_id"),
    "load_skill": ("name",),
    "read_skill_resource": ("name", "path"),
    "run_skill_script": ("name", "path"),
}
# Shell-family tools also surface the model-stated intent after the command;
# derived, so a newly registered shell name picks it up automatically.
_EXTRA_DISPLAY_ARG_KEYS_BY_TOOL: dict[str, tuple[str, ...]] = {
    tool: ("reason",) for tool, keys in _DISPLAY_ARG_KEYS_BY_TOOL.items() if keys == ("command",)
}
# Catalog-line argument preview caps, sized to keep whole commands, paths, and
# shell intents visible; the LAST_WORDS manifest's overall char/line budget
# evicts oldest entries when a long listing overflows, so a longer preview
# trades tail entries, not correctness.  ``last_words_state`` derives its
# stored-field bound from them.
DISPLAY_ARGUMENT_DEFAULT_MAX_CHARS = 120
# Paths middle-truncate (``D:\Repos\…\test.py``) so the filename tail stays.
DISPLAY_ARGUMENT_PATH_MAX_CHARS = 256
# Shell tools append the model-stated ``reason="…"`` after the command.
DISPLAY_ARGUMENT_REASON_MAX_CHARS = 80
_DISPLAY_ARGUMENT_KEY_CAPS = {
    "path": DISPLAY_ARGUMENT_PATH_MAX_CHARS,
    "file_path": DISPLAY_ARGUMENT_PATH_MAX_CHARS,
    "reason": DISPLAY_ARGUMENT_REASON_MAX_CHARS,
}
_MIDDLE_TRUNCATED_DISPLAY_KEYS = frozenset({"path", "file_path"})
_SENSITIVE_KEY_RE = re.compile(r"(?:token|password|passwd|secret|api[_-]?key|authorization|credential)", re.IGNORECASE)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?:bearer\s+[A-Za-z0-9._~+/=-]+|(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,}|"
    r"eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{8,}\.)",
    re.IGNORECASE,
)
_SENSITIVE_URL_PARAMETER_NAMES = frozenset(
    {
        "accesskey",
        "accesstoken",
        "apikey",
        "auth",
        "authorization",
        "clientsecret",
        "credential",
        "credentials",
        "key",
        "passwd",
        "password",
        "privatekey",
        "secret",
        "sig",
        "signature",
        "token",
    }
)


def _display_argument(call: Content, stamp: Mapping[str, str] | None) -> str:
    serialized = stamp.get("effective_args") if stamp is not None else _requested_call_arguments(call)
    if serialized is None:
        return ""
    try:
        arguments = json.loads(serialized)
    except TypeError, ValueError:
        return ""
    if not isinstance(arguments, Mapping):
        return ""
    tool_name = _call_tool_name(call)
    segments: list[str] = []
    for key in _DISPLAY_ARG_KEYS_BY_TOOL.get(tool_name, ()):
        segment = _display_argument_segment(arguments, key)
        if segment is not None:
            segments.append(segment)
            break
    for key in _EXTRA_DISPLAY_ARG_KEYS_BY_TOOL.get(tool_name, ()):
        segment = _display_argument_segment(arguments, key)
        if segment is not None:
            segments.append(segment)
    return ", ".join(segments)


def _display_argument_segment(arguments: Mapping[str, Any], key: str) -> str | None:
    """Render one ``key=value`` preview segment; None when absent or filtered."""
    if key not in arguments or _SENSITIVE_KEY_RE.search(key):
        return None
    value = arguments[key]
    if key == "url" and _url_contains_credentials(value):
        return None
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).replace("\n", "\\n")
    if _SENSITIVE_KEY_RE.search(encoded) or _SENSITIVE_VALUE_RE.search(encoded) or _mapping_has_sensitive_key(value):
        return None
    rendered = f"{key}={encoded}"
    cap = _DISPLAY_ARGUMENT_KEY_CAPS.get(key, DISPLAY_ARGUMENT_DEFAULT_MAX_CHARS)
    if len(rendered) <= cap:
        return rendered
    if key in _MIDDLE_TRUNCATED_DISPLAY_KEYS:
        # The filename tail is the informative end of a long path — drop the
        # middle, never the tail.
        head = (cap - 1) // 2
        return rendered[:head] + "…" + rendered[len(rendered) - (cap - 1 - head) :]
    return rendered[: cap - 1] + "…"


def _url_contains_credentials(value: Any) -> bool:
    """Return whether a display URL contains userinfo or secret-bearing parameters."""
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    if parsed.username is not None or parsed.password is not None:
        return True
    for component in (parsed.query, parsed.fragment):
        for name, _value in parse_qsl(component, keep_blank_values=True):
            normalized = re.sub(r"[^a-z0-9]", "", name.casefold())
            if _SENSITIVE_KEY_RE.search(name) or normalized in _SENSITIVE_URL_PARAMETER_NAMES:
                return True
    return False


def _mapping_has_sensitive_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            _SENSITIVE_KEY_RE.search(str(key)) or _mapping_has_sensitive_key(nested) for key, nested in value.items()
        )
    if isinstance(value, list | tuple):
        return any(_mapping_has_sensitive_key(item) for item in value)
    return False
