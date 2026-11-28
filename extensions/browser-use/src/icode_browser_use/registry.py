# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Durable browser manifest: identity and recovery information only.

The manifest is written where the host gives the plugin a private folder
(``ctx.session_data_dir`` inside a saved session, else ``ctx.data_dir``) as
``browsers.json``::

    {"schema": 1, "records": [<record>, ...]}

A record carries the logical resource identity the plugin can rebuild --
``browser_id``, ``resume_id`` (slot + generation), owner session/agent, backend
and the redacted page origin -- and nothing that could leak a page's contents or
credentials: no cookies, passwords, full URLs, queries, titles or absolute
profile paths.

Recovery rules this module supports:

- Writes go to a temporary file in the same directory and are swapped in with
  ``os.replace``, so a crash never leaves a half-written manifest.
- Records are matched to their owner by session id, agent name and the
  ``owner_key`` (which includes caller/invocation metadata from
  ``ToolContext.invocation_type``, ``caller_name`` and ``invocation_id`` for
  non-turn invocations); a forked session copies the file but not the identity,
  so its records stay unclaimable history.
- An unreadable or unexpected manifest never breaks a browser call: callers get
  no records plus a reason to report.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ._proc import atomic_write_private
from .policy import canonical_origin

SCHEMA_VERSION = 1
MANIFEST_NAME = "browsers.json"


@dataclass(frozen=True)
class ManifestRecord:
    """Validated, typed browser recovery record stored in ``browsers.json``."""

    schema: int
    browser_id: str
    resume_id: str
    session_id: str | None
    agent_name: str
    owner_key: str
    backend: str
    generation: str
    slot: int
    origin: str
    updated_at: float

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "browser_id": self.browser_id,
            "resume_id": self.resume_id,
            "session_id": self.session_id,
            "agent_name": self.agent_name,
            "owner_key": self.owner_key,
            "backend": self.backend,
            "generation": self.generation,
            "slot": self.slot,
            "origin": self.origin,
            "updated_at": self.updated_at,
        }


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_strict_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _parse_record(item: Any) -> tuple[ManifestRecord | None, bool]:
    """Parse one manifest JSON entry. Returns ``(record, invalid)``."""
    if not isinstance(item, Mapping):
        return None, True
    schema = item.get("schema")
    if not _is_strict_int(schema):
        return None, True
    if schema != SCHEMA_VERSION:
        # Older or newer schema entries are ignored quietly for forward/backward tolerance.
        return None, False
    browser_id = item.get("browser_id")
    resume_id = item.get("resume_id")
    session_id = item.get("session_id")
    agent_name = item.get("agent_name")
    owner_key = item.get("owner_key")
    backend = item.get("backend")
    generation = item.get("generation")
    slot = item.get("slot")
    origin = item.get("origin")
    updated_at = item.get("updated_at")
    if (
        not isinstance(browser_id, str)
        or not browser_id
        or not isinstance(resume_id, str)
        or not resume_id
        or (session_id is not None and not isinstance(session_id, str))
        or not isinstance(agent_name, str)
        or not isinstance(owner_key, str)
        or not owner_key
        or not isinstance(backend, str)
        or not backend
        or not isinstance(generation, str)
        or not generation
        or not _is_strict_int(slot)
        or slot < 0
        or not isinstance(origin, str)
        or not _is_strict_number(updated_at)
    ):
        return None, True
    return (
        ManifestRecord(
            schema=schema,
            browser_id=browser_id,
            resume_id=resume_id,
            session_id=session_id,
            agent_name=agent_name,
            owner_key=owner_key,
            backend=backend,
            generation=generation,
            slot=slot,
            origin=origin,
            updated_at=float(updated_at),
        ),
        False,
    )


def manifest_path(ctx: Any) -> Path:
    """``browsers.json`` in the session folder, or in the plugin's data folder."""
    directory = ctx.session_data_dir
    return (directory if directory is not None else ctx.data_dir) / MANIFEST_NAME


def redacted_origin(url: str) -> str:
    """Scheme, host and port only; queries, credentials and paths never land on disk."""
    return canonical_origin(url) or "(not navigated yet)"


def new_record(
    *,
    browser_id: str,
    resume_id: str,
    session_id: str | None,
    agent_name: str,
    owner_key: str,
    backend: str,
    generation: str,
    slot: int,
    origin_url: str = "",
    updated_at: float = 0.0,
) -> ManifestRecord:
    return ManifestRecord(
        schema=SCHEMA_VERSION,
        browser_id=browser_id,
        resume_id=resume_id,
        session_id=session_id,
        agent_name=agent_name,
        owner_key=owner_key,
        backend=backend,
        generation=generation,
        slot=slot,
        origin=redacted_origin(origin_url),
        updated_at=float(updated_at),
    )


def load(path: Path) -> tuple[list[ManifestRecord], str | None]:
    """Return ``(records, problem)``; a missing file is not a problem."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], None
    except OSError:
        return [], "unreadable"
    try:
        payload = json.loads(raw)
    except ValueError:
        return [], "unreadable"
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
        return [], "unexpected shape"
    records: list[ManifestRecord] = []
    invalid_count = 0
    for item in payload["records"]:
        parsed, invalid = _parse_record(item)
        if parsed is not None:
            records.append(parsed)
        elif invalid:
            invalid_count += 1
    problem = f"{invalid_count} invalid record(s) skipped" if invalid_count else None
    return records, problem


def save(path: Path, records: list[ManifestRecord]) -> None:
    """Replace the manifest atomically with owner-only permissions."""
    payload = json.dumps({"schema": SCHEMA_VERSION, "records": [r.to_dict() for r in records]}, ensure_ascii=True)
    atomic_write_private(path, payload, strict=False)


def upsert(records: list[ManifestRecord], record: ManifestRecord) -> list[ManifestRecord]:
    """Replace the record with the same ``resume_id`` in place, or append it."""
    for index, item in enumerate(records):
        if item.resume_id == record.resume_id:
            updated = list(records)
            updated[index] = record
            return updated
    return [*records, record]


def supersede(records: list[ManifestRecord], record: ManifestRecord) -> list[ManifestRecord]:
    """Keep one record per owner slot: an older generation of the same slot is dropped."""
    kept = [
        item
        for item in records
        if not (item.owner_key == record.owner_key and item.slot == record.slot) or item.resume_id == record.resume_id
    ]
    return upsert(kept, record)


def drop(records: list[ManifestRecord], resume_id: str) -> list[ManifestRecord]:
    return [item for item in records if item.resume_id != resume_id]
