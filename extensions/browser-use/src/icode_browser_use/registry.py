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
  build-scoped ``owner_key`` (which includes ``ToolContext.agent_id``); a forked
  session copies the file but not the identity, so its records stay
  unclaimable history.
- An unreadable or unexpected manifest never breaks a browser call: callers get
  no records plus a reason to report.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .policy import canonical_origin

SCHEMA_VERSION = 1
MANIFEST_NAME = "browsers.json"

#: Fields a caller may rely on. Anything else is ignored on load.
_FIELDS = (
    "schema",
    "browser_id",
    "resume_id",
    "session_id",
    "agent_name",
    "owner_key",
    "backend",
    "generation",
    "slot",
    "origin",
    "updated_at",
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
) -> dict[str, object]:
    return {
        "schema": SCHEMA_VERSION,
        "browser_id": browser_id,
        "resume_id": resume_id,
        "session_id": session_id,
        "agent_name": agent_name,
        "owner_key": owner_key,
        "backend": backend,
        "generation": generation,
        "slot": slot,
        "origin": redacted_origin(origin_url),
        "updated_at": updated_at,
    }


def load(path: Path) -> tuple[list[dict[str, object]], str | None]:
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
    records: list[dict[str, object]] = []
    for item in payload["records"]:
        if not isinstance(item, Mapping) or item.get("schema") != SCHEMA_VERSION:
            continue
        if not all(name in item for name in _FIELDS):
            continue
        records.append({name: item[name] for name in _FIELDS})
    return records, None


def save(path: Path, records: list[dict[str, object]]) -> None:
    """Replace the manifest atomically; a reader sees the old file or the new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"schema": SCHEMA_VERSION, "records": records}, ensure_ascii=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{MANIFEST_NAME}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def upsert(records: list[dict[str, object]], record: dict[str, object]) -> list[dict[str, object]]:
    """Replace the record with the same ``resume_id`` in place, or append it."""
    resume_id = record["resume_id"]
    for index, item in enumerate(records):
        if item.get("resume_id") == resume_id:
            updated = list(records)
            updated[index] = record
            return updated
    return [*records, record]


def supersede(records: list[dict[str, object]], record: dict[str, object]) -> list[dict[str, object]]:
    """Keep one record per owner slot: an older generation of the same slot is dropped."""
    owner_key = record["owner_key"]
    slot = record["slot"]
    kept = [
        item
        for item in records
        if not (item.get("owner_key") == owner_key and item.get("slot") == slot)
        or item.get("resume_id") == record.get("resume_id")
    ]
    return upsert(kept, record)


def drop(records: list[dict[str, object]], resume_id: str) -> list[dict[str, object]]:
    return [item for item in records if item.get("resume_id") != resume_id]
