# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The confirmation ledger: which (file, spec, environment) triples the user has looked at and approved.

Entries are structured records rather than a path-to-digest map, so a changed
entry file, a changed topology and a changed interpreter all fall out of the
ledger the same way. Builtin sources never enter it: they ship with chrys and
are exempt from the gate. The file is owner-only and rewritten atomically.
"""

from __future__ import annotations

import errno
import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

from chrys.foundation.models.workdir_migration import BackupOriginal, ReportReadError, source_edit
from chrys.foundation.platform.files import (
    atomic_write_owner_only_bytes,
    require_writable_file,
    secure_open_owner_verified_binary,
)
from chrys.foundation.util.lock import FileLock
from chrys.service.workflows.discovery import SOURCE_KIND_BUILTIN

logger = logging.getLogger(__name__)

LEDGER_FILE: Final = "trusted.json"
LEDGER_LOCK_FILE: Final = f"{LEDGER_FILE}.lock"
LEDGER_LOCK_TIMEOUT: Final = 10.0  # seconds
LEDGER_VERSION: Final = 1
MAX_LEDGER_BYTES: Final = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    canonical_path: str
    source_kind: str
    workflow_id: str
    entry_digest: str
    manifest_digest: str
    schema_version: int
    spec_digest: str
    environment_fingerprint: str
    title: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ConfirmationLedger:
    """Owner-verified read on construction; every ``confirm`` re-reads under the ledger's lock and rewrites the file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._entries: list[LedgerEntry] = _load(path)

    def is_confirmed(self, entry: LedgerEntry) -> bool:
        return entry in self._entries

    def remove(self, canonical_path: str) -> None:
        """Forget one file, preserving confirmations another process wrote since this ledger was read."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock(self._path):
            self._write([entry for entry in _load(self._path) if entry.canonical_path != canonical_path])

    def recorded(self, canonical_path: str, source_kind: str) -> LedgerEntry | None:
        """The record for the file at *canonical_path* under *source_kind*: what its last confirmation saw."""
        for entry in reversed(self._entries):
            if (entry.canonical_path, entry.source_kind) == (canonical_path, source_kind):
                return entry
        return None

    def confirm(self, entry: LedgerEntry) -> None:
        """Record *entry*, replacing any earlier record for the same file and source kind.

        The file is re-read under its lock right before the rewrite, so a confirmation another chrys
        process recorded since this ledger was loaded (two ``--trust`` runs at once) is kept, not overwritten.
        """
        if entry.source_kind == SOURCE_KIND_BUILTIN:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _write_lock(self._path):
            entries = [
                kept
                for kept in _load(self._path)
                if (kept.canonical_path, kept.source_kind) != (entry.canonical_path, entry.source_kind)
            ]
            self._write([*entries, entry])

    def _write(self, entries: list[LedgerEntry]) -> None:
        payload = {"version": LEDGER_VERSION, "entries": [item.to_dict() for item in entries]}
        write_ledger(self._path, payload)
        self._entries = entries


def _write_lock(path: Path) -> FileLock:
    return FileLock(path.with_name(LEDGER_LOCK_FILE), timeout=LEDGER_LOCK_TIMEOUT)


def write_ledger(path: Path, payload: dict[str, Any], *, before_write: Callable[[], None] | None = None) -> None:
    """Atomically replace the ledger while its caller holds the ledger lock."""
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_LEDGER_BYTES:
        raise ValueError(f"Workflow confirmation ledger {path} is too large (limit: {MAX_LEDGER_BYTES:,} bytes)")
    if before_write is not None:
        before_write()
    atomic_write_owner_only_bytes(path, encoded)


def migrate_ledger(path: Path, *, backup: BackupOriginal, on_read_error: ReportReadError) -> bool:
    """Rewrite old source names without changing unrelated ledger data."""

    def candidate() -> tuple[bytes, dict[str, Any]] | None:
        try:
            record = _read_ledger(path)
            if record is None:
                return None
            raw, payload = record
            if not isinstance(payload, dict):
                raise ValueError("Expected a JSON object")
            entries, changed = _normalise_entries(payload)
            if not changed:
                return None
            payload["entries"] = entries
            return raw, payload
        except (OSError, ValueError, RecursionError) as error:
            on_read_error(path, error)
            return None

    if candidate() is None:
        return False
    try:
        with _write_lock(path):
            record = candidate()
            if record is None:
                return False
            raw, payload = record

            def preserve() -> None:
                require_writable_file(path)
                backup(path, raw)

            write_ledger(path, payload, before_write=preserve)
    except OSError, ValueError, TypeError, RecursionError:
        return False
    return True


def ledger_path(config_dir: Path) -> Path:
    from chrys.service.workflows.discovery import global_workflows_dir

    return global_workflows_dir(config_dir) / LEDGER_FILE


def _read_ledger(path: Path) -> tuple[bytes, Any] | None:
    try:
        with secure_open_owner_verified_binary(path) as handle:
            raw = handle.read(MAX_LEDGER_BYTES + 1)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return None  # nothing confirmed yet
        raise
    if len(raw) > MAX_LEDGER_BYTES:
        raise ValueError("Workflow confirmation ledger is too large")
    return raw, json.loads(raw.decode("utf-8"))


def _normalise_entries(payload: object) -> tuple[list[Any], bool]:
    items = payload.get("entries") if isinstance(payload, dict) and payload.get("version") == LEDGER_VERSION else None
    if not isinstance(items, list):
        return [], False
    changed = False
    latest: dict[tuple[str, str], int] = {}
    superseded: set[int] = set()
    for index, item in enumerate(items):
        changed |= source_edit(item)
        if not isinstance(item, dict):
            continue
        path, scope = item.get("canonical_path"), item.get("source_kind")
        if isinstance(path, str) and isinstance(scope, str):
            key = (path, scope)
            previous = latest.get(key)
            if previous is not None:
                superseded.add(previous)
            latest[key] = index
    if superseded:
        return [item for index, item in enumerate(items) if index not in superseded], True
    return items, changed


def _load(path: Path) -> list[LedgerEntry]:
    try:
        record = _read_ledger(path)
    except OSError:
        logger.warning("workflow confirmation ledger %s is unreadable; treating it as empty", path, exc_info=True)
        return []
    except (ValueError, RecursionError) as error:
        logger.warning("workflow confirmation ledger %s could not be loaded; treating it as empty (%s)", path, error)
        return []
    if record is None:
        return []
    items, _ = _normalise_entries(record[1])
    return [entry for item in items if (entry := _entry_from(item)) is not None]


def _entry_from(item: Any) -> LedgerEntry | None:
    if not isinstance(item, dict):
        return None
    strings = {}
    for key in (
        "canonical_path",
        "source_kind",
        "workflow_id",
        "entry_digest",
        "manifest_digest",
        "spec_digest",
        "environment_fingerprint",
        "title",
    ):
        value = item.get(key)
        if not isinstance(value, str):
            return None
        strings[key] = value
    schema_version = item.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        return None
    return LedgerEntry(schema_version=schema_version, **strings)
