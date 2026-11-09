# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persisted listing metadata for the session browser.

``<sessions_root>/.cache/session_catalog.json`` keeps, per session folder, the
listing metadata derived from that session's ``session.json`` together with
the file's ``(inode, mtime_ns, size)`` signature, so listing sessions costs a
``stat`` per folder instead of a parse per envelope. Session files stay the
only source of truth: an entry is used only while its signature still matches
the file on disk, and a missing, corrupt or differently versioned catalog is
rebuilt from the envelopes.

Only metadata read from a valid primary file with no recovery sidecar, whose
signature was identical before and after the read, is recorded; a session
resolved from a sidecar, backup or rollback snapshot is parsed live every
time. Directory sizes and workflow run status are never recorded (both
change without touching ``session.json``); a workflow session's listing time
is, keyed on its latest run's header and log signatures.

The file holds prompt excerpts, so it is owner-only and a deleted session's
entry is removed with it. Writers re-read and merge under a leaf
:class:`FileLock` (nothing else is acquired while it is held), keep only
entries whose session file still exists, and give up after
:data:`SESSION_CATALOG_LOCK_TIMEOUT_SECONDS` — a stuck peer costs parses, never
a failed session operation. A catalog that exists but cannot be read right now
is never overwritten, and one written by a newer derivation is not downgraded
(deleting a session drops such a file whole rather than leave its excerpts).
"""

from __future__ import annotations

import dataclasses
import errno
import functools
import json
import logging
import os
import typing
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from chrys.foundation.models.session_surface import SessionSurface, parse_session_surface
from chrys.foundation.platform.files import atomic_write_owner_only_bytes, read_owner_verified_bounded
from chrys.foundation.util.lock import FileLock
from chrys.service.state._session_meta import ChatSessionMeta, SessionMeta, WorkflowSessionMeta

logger = logging.getLogger(__name__)

CATALOG_DERIVATION_VERSION = 2
"""Bump whenever the recorded metadata changes shape, meaning or derivation (turn counts, prompt excerpts, …),
so older catalogs rebuild; ``tests/service/state/test_session_catalog.py`` pins all three."""

SESSION_CATALOG_DIR_NAME = ".cache"
SESSION_CATALOG_FILE_NAME = "session_catalog.json"
SESSION_CATALOG_LOCK_FILE_NAME = "session_catalog.lock"
SESSION_CATALOG_LOCK_TIMEOUT_SECONDS = 2.0
MAX_SESSION_CATALOG_BYTES = 256 * 1024 * 1024

type FileSignature = tuple[int, int, int]
"""``(st_ino, st_mtime_ns, st_size)``: an atomic replace changes the inode even when size and mtime match."""

# Never recorded: the folder size and the run summary change without touching ``session.json``.
_UNRECORDED_FIELDS = frozenset({"kind", "size_bytes", "latest_run"})


def file_signature(path: Path) -> FileSignature | None:
    """The change signature of *path*, or ``None`` when it cannot be stat'ed."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_ino, stat.st_mtime_ns, stat.st_size)


@dataclass(frozen=True, slots=True)
class RunListingKey:
    """The files a workflow session's listing time is read from."""

    run_id: str
    header: FileSignature | None
    events: FileSignature | None


@dataclass(frozen=True, slots=True)
class RunListing:
    """A workflow session's listing time; ``None`` when its latest run has no readable summary."""

    key: RunListingKey
    listed_at: datetime | None


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """Listing metadata (``size_bytes`` and ``latest_run`` unset) derived from one ``session.json``."""

    signature: FileSignature
    meta: SessionMeta
    run: RunListing | None = None


@dataclass(frozen=True, slots=True)
class CatalogCommit:
    """The outcome of one merge into the catalog file."""

    settled: bool
    """No retry needed: the updates were written, or a newer derivation owns the file."""
    signature: FileSignature | None = None
    """The file as this merge wrote it, read before the lock was released."""


@functools.cache
def _recorded_fields(cls: type[SessionMeta]) -> dict[str, object]:
    hints = typing.get_type_hints(cls)
    return {
        field.name: hints[field.name]
        for field in dataclasses.fields(cls)
        if field.init and field.name not in _UNRECORDED_FIELDS
    }


def _meta_class(kind: object) -> type[SessionMeta]:
    if kind == "chat":
        return ChatSessionMeta
    if kind == "workflow":
        return WorkflowSessionMeta
    raise ValueError(f"Unknown session kind in catalog: {kind!r}")


def _encode_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, SessionSurface):
        return value.value
    if isinstance(value, list):
        return list(value)
    return value


def _decode_timestamp(value: str) -> datetime:
    """A recorded stamp, whatever it times.

    Fail closed rather than replay an offset-free one: the reader that derived
    this value always records an aware UTC stamp, so this entry came from
    another generation or from outside, and serving it would hand consumers a
    value they cannot compare (the entry is skipped and re-derived).
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"Catalog timestamp {value!r} has no timezone.")
    return parsed


def _decode_value(hint: object, value: object) -> object:
    if hint is str and isinstance(value, str):
        return value
    if hint is int and type(value) is int:
        return value
    if hint is datetime and isinstance(value, str):
        return _decode_timestamp(value)
    if hint == list[str] and isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    if hint == SessionSurface | None:
        return None if value is None else parse_session_surface(value)
    raise TypeError(f"Catalog value {value!r} does not match {hint!r}")


def encode_meta(meta: SessionMeta) -> dict[str, Any]:
    """The recorded fields of *meta* as JSON values."""
    values = dataclasses.asdict(meta)
    record: dict[str, Any] = {"kind": meta.kind}
    for name in _recorded_fields(type(meta)):
        record[name] = _encode_value(values[name])
    return record


def decode_meta(record: object) -> SessionMeta:
    """Rebuild listing metadata; raises ``ValueError``/``TypeError``/``KeyError`` on anything malformed."""
    if not isinstance(record, dict):
        raise TypeError("A catalog meta record must be an object.")
    cls = _meta_class(record.get("kind"))
    # Each value was checked against its field's hint by ``_decode_value``.
    values: dict[str, Any] = {name: _decode_value(hint, record[name]) for name, hint in _recorded_fields(cls).items()}
    return cls(**values)


def _encode_signature(signature: FileSignature | None) -> list[int] | None:
    return list(signature) if signature is not None else None


def _decode_signature(value: object, *, optional: bool = False) -> FileSignature | None:
    if value is None and optional:
        return None
    if isinstance(value, list) and len(value) == 3 and all(type(item) is int for item in value):
        return (value[0], value[1], value[2])
    raise TypeError(f"Invalid catalog file signature: {value!r}")


def encode_entry(entry: CatalogEntry) -> dict[str, Any]:
    record: dict[str, Any] = {"signature": list(entry.signature), "meta": encode_meta(entry.meta)}
    if entry.run is not None:
        record["run"] = {
            "run_id": entry.run.key.run_id,
            "header": _encode_signature(entry.run.key.header),
            "events": _encode_signature(entry.run.key.events),
            "listed_at": entry.run.listed_at.isoformat() if entry.run.listed_at is not None else None,
        }
    return record


def is_recordable(entry: CatalogEntry) -> bool:
    """Whether *entry* reads back from the catalog; one that would not (a malformed envelope) stays unrecorded."""
    try:
        decode_entry(encode_entry(entry))
    except KeyError, ValueError, TypeError:
        return False
    return True


def decode_entry(record: object) -> CatalogEntry:
    """Rebuild one entry; raises ``ValueError``/``TypeError``/``KeyError`` on anything malformed."""
    if not isinstance(record, dict):
        raise TypeError("A catalog entry must be an object.")
    signature = _decode_signature(record["signature"])
    if signature is None:
        raise TypeError("A catalog entry needs a file signature.")
    meta = decode_meta(record["meta"])
    run: RunListing | None = None
    raw_run = record.get("run")
    if raw_run is not None:
        if not isinstance(raw_run, dict) or not isinstance(raw_run.get("run_id"), str):
            raise TypeError("Invalid catalog run record.")
        listed_at = raw_run.get("listed_at")
        if listed_at is not None and not isinstance(listed_at, str):
            raise TypeError("Invalid catalog run time.")
        run = RunListing(
            RunListingKey(
                raw_run["run_id"],
                _decode_signature(raw_run.get("header"), optional=True),
                _decode_signature(raw_run.get("events"), optional=True),
            ),
            _decode_timestamp(listed_at) if listed_at is not None else None,
        )
    return CatalogEntry(signature, meta, run)


class SessionCatalogFile:
    """Locked read-merge-write access to one sessions root's listing catalog."""

    def __init__(self, sessions_root: Path) -> None:
        self._root = Path(sessions_root)

    @property
    def path(self) -> Path:
        return self._root / SESSION_CATALOG_DIR_NAME / SESSION_CATALOG_FILE_NAME

    @property
    def lock_path(self) -> Path:
        return self._root / ".locks" / SESSION_CATALOG_LOCK_FILE_NAME

    def load(self) -> dict[str, CatalogEntry] | None:
        """Entries of the current derivation, skipping malformed ones; ``None`` when the file cannot be read now.

        A missing, corrupt or differently versioned catalog reads as empty.
        """
        read = self._read_records()
        if read is None:
            return None
        version, records = read
        if version != CATALOG_DERIVATION_VERSION:
            return {}
        entries: dict[str, CatalogEntry] = {}
        for short_id, record in records.items():
            try:
                entries[short_id] = decode_entry(record)
            except KeyError, ValueError, TypeError:
                logger.debug("Skipping malformed session catalog entry %s", short_id, exc_info=True)
        return entries

    def commit(
        self,
        updates: Mapping[str, CatalogEntry],
        *,
        still_valid: Callable[[str, CatalogEntry], bool],
        is_live: Callable[[str], bool],
    ) -> CatalogCommit:
        """Merge *updates* into the file.

        Under the lock, each update is re-checked with *still_valid* (so an
        entry whose session changed since it was derived never replaces a
        peer's fresher one) and every entry whose session is gone is dropped.
        """
        try:
            with self._lock():
                read = self._read_records()
                if read is None:
                    return CatalogCommit(settled=False)
                version, records = read
                if version is not None and version > CATALOG_DERIVATION_VERSION:
                    return CatalogCommit(settled=True)
                current = records if version == CATALOG_DERIVATION_VERSION else {}
                merged = dict(current)
                for short_id, entry in updates.items():
                    if still_valid(short_id, entry):
                        merged[short_id] = encode_entry(entry)
                merged = {short_id: record for short_id, record in merged.items() if is_live(short_id)}
                if (merged != current or version != CATALOG_DERIVATION_VERSION) and not self._write(merged):
                    return CatalogCommit(settled=False)
                return CatalogCommit(settled=True, signature=file_signature(self.path))
        except TimeoutError:
            logger.debug("Session catalog %s is locked; skipping update", self.path)
        except OSError:
            logger.debug("Failed to update session catalog %s", self.path, exc_info=True)
        return CatalogCommit(settled=False)

    def remove(self, short_ids: Collection[str]) -> bool:
        """Drop the entries of deleted sessions; whether none of them remains.

        A catalog this version cannot read or rewrite (unreadable, corrupt or
        another version) goes whole: it is only a cache, and it may still hold
        the deleted sessions' prompt excerpts.
        """
        try:
            with self._lock():
                read = self._read_records()
                if read is None or read[0] != CATALOG_DERIVATION_VERSION:
                    self.path.unlink(missing_ok=True)
                    return True
                records = read[1]
                if not any(short_id in records for short_id in short_ids):
                    return True
                return self._write({key: value for key, value in records.items() if key not in short_ids})
        except TimeoutError:
            logger.warning("Session catalog %s is locked; a deleted session's entry remains for now", self.path)
        except OSError:
            logger.warning("Failed to remove deleted sessions from catalog %s", self.path, exc_info=True)
        return False

    def _lock(self) -> FileLock:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(self.lock_path, timeout=SESSION_CATALOG_LOCK_TIMEOUT_SECONDS)

    def _read_records(self) -> tuple[int | None, dict[str, Any]] | None:
        """``(version, records)``; ``(None, {})`` when missing or corrupt, ``None`` when unreadable right now."""
        try:
            raw = json.loads(read_owner_verified_bounded(self.path, max_bytes=MAX_SESSION_CATALOG_BYTES))
        except OSError as exc:
            if isinstance(exc, FileNotFoundError) or exc.errno in (errno.ENOENT, errno.ENOTDIR):
                return None, {}
            # Possibly transient (a peer's replace on Windows): never mistake it for an empty catalog.
            logger.debug("Cannot read session catalog %s now", self.path, exc_info=True)
            return None
        except ValueError:
            logger.debug("Corrupt session catalog %s", self.path, exc_info=True)
            return None, {}
        if not isinstance(raw, dict) or type(raw.get("version")) is not int:
            return None, {}
        sessions = raw.get("sessions")
        if not isinstance(sessions, dict):
            return None, {}
        return raw["version"], {key: value for key, value in sessions.items() if isinstance(key, str)}

    def _write(self, records: dict[str, Any]) -> bool:
        text = json.dumps(
            {"version": CATALOG_DERIVATION_VERSION, "sessions": records}, ensure_ascii=False, separators=(",", ":")
        )
        # A lone surrogate (a surrogateescaped path) is written as its JSON escape and reads back unchanged.
        payload = text.encode("utf-8", errors="backslashreplace")
        if len(payload) > MAX_SESSION_CATALOG_BYTES:
            logger.warning("Session catalog %s would exceed its size ceiling; not written", self.path)
            return False
        atomic_write_owner_only_bytes(self.path, payload)
        return True
