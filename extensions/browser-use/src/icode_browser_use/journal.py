# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Durable action journal: one SQLite file per browser state directory.

A row is one action id, progressing through phases::

    prepared    recorded before anything was sent to the page
    dispatched  the send boundary returned; the action may have taken effect
    result      a verified outcome (``succeeded`` / ``blocked`` / ``uncertain``)

A crash therefore leaves ``prepared`` or ``dispatched`` behind, and the query
layer reports those as ``uncertain``: the plugin cannot prove the action was not
sent, so callers must check the page instead of repeating it.

Rows are scoped by ``owner`` and ``logical_session`` so two agents sharing a
state directory (attach/bridge modes) cannot read each other's history. Owned
browsers already have a per-agent slot directory, so the file itself is
private. Retention keeps the newest ``limit`` rows per session.

The journal is evidence only: it never retries, never replays and never claims
business idempotency. Storage failures are logged and ignored -- losing the
journal must not break a page action.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_ACTIONS_PER_SESSION = 200
MAX_JOURNAL_AGE_SECONDS = 30 * 24 * 3600
MAX_JOURNAL_GLOBAL_ROWS = 5000

PREPARED = "prepared"
DISPATCHED = "dispatched"
RESULT = "result"

_COLUMNS = (
    "schema",
    "owner",
    "logical_session",
    "generation",
    "tool",
    "phase",
    "status",
    "document_epoch",
    "evidence",
    "target_ref",
    "dispatch",
    "reason",
    "effect",
    "at",
)

_STRUCTURED_COLUMNS = ("target_ref", "dispatch", "reason", "effect")

_SELECT_COLUMNS = ", ".join(_COLUMNS)
_LOOKUP_SQL = (
    f"SELECT action_id, {_SELECT_COLUMNS} FROM actions "  # noqa: S608
    "WHERE action_id=? AND owner=? AND logical_session=?"
)
_ROWS_FOR_SQL = (
    f"SELECT action_id, {_SELECT_COLUMNS} FROM actions "  # noqa: S608
    "WHERE owner=? AND logical_session=? AND tool=? AND document_epoch=? "
    "ORDER BY rowid DESC"
)

logger = logging.getLogger(__name__)


class Journal:
    """Append-mostly store for one browser session's action history."""

    def __init__(
        self,
        path: Path | None,
        *,
        owner: str = "",
        logical_session: str = "",
        generation: str = "",
        limit: int = MAX_ACTIONS_PER_SESSION,
    ) -> None:
        self.owner = owner
        self.logical_session = logical_session
        self.generation = generation
        self.limit = max(1, limit)
        self._lock = threading.Lock()
        self._write_count = 0
        self._connection = self._connect(path)

    def _connect(self, path: Path | None) -> sqlite3.Connection | None:
        try:
            if path is None:
                connection = sqlite3.connect(":memory:", check_same_thread=False)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(path, timeout=5.0, check_same_thread=False)
                connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS actions ("
                "action_id TEXT PRIMARY KEY, schema INTEGER NOT NULL, owner TEXT NOT NULL, "
                "logical_session TEXT NOT NULL, generation TEXT NOT NULL, tool TEXT NOT NULL, "
                "phase TEXT NOT NULL, status TEXT NOT NULL, document_epoch INTEGER NOT NULL, "
                "evidence TEXT NOT NULL, target_ref TEXT NOT NULL DEFAULT '', "
                "dispatch TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', "
                "effect TEXT NOT NULL DEFAULT '', at REAL NOT NULL)"
            )
            existing = {row[1] for row in connection.execute("PRAGMA table_info(actions)").fetchall()}
            for column in _STRUCTURED_COLUMNS:
                if column not in existing:
                    connection.execute(f"ALTER TABLE actions ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_actions_owner_session "
                "ON actions (owner, logical_session, tool, document_epoch)"
            )
            connection.execute("CREATE INDEX IF NOT EXISTS idx_actions_at ON actions (at)")
            self._prune_global(connection)
            connection.commit()
            return connection
        except (sqlite3.Error, OSError) as exc:
            logger.warning("browser journal unavailable: %s", exc)
            return None

    def _prune_global(self, connection: sqlite3.Connection) -> None:
        cutoff = time.time() - MAX_JOURNAL_AGE_SECONDS
        connection.execute("DELETE FROM actions WHERE at < ?", (cutoff,))
        connection.execute(
            "DELETE FROM actions WHERE action_id NOT IN ("
            "SELECT action_id FROM actions ORDER BY at DESC, rowid DESC LIMIT ?)",
            (MAX_JOURNAL_GLOBAL_ROWS,),
        )

    def record(
        self,
        *,
        action_id: str,
        tool: str,
        phase: str,
        status: str,
        evidence: str,
        document_epoch: int = 0,
        target_ref: str = "",
        dispatch: str = "",
        reason: str = "",
        effect: str = "",
    ) -> None:
        with self._lock:
            connection = self._connection
            if connection is None:
                return
            try:
                connection.execute(
                    "INSERT INTO actions (action_id, schema, owner, logical_session, generation, tool, "
                    "phase, status, document_epoch, evidence, target_ref, dispatch, reason, effect, at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(action_id) DO UPDATE SET phase=excluded.phase, status=excluded.status, "
                    "document_epoch=excluded.document_epoch, evidence=excluded.evidence, "
                    "target_ref=excluded.target_ref, dispatch=excluded.dispatch, "
                    "reason=excluded.reason, effect=excluded.effect, at=excluded.at, "
                    "generation=excluded.generation",
                    (
                        action_id,
                        SCHEMA_VERSION,
                        self.owner,
                        self.logical_session,
                        self.generation,
                        tool,
                        phase,
                        status,
                        int(document_epoch),
                        evidence[:500],
                        target_ref[:80],
                        dispatch[:40],
                        reason[:80],
                        effect[:40],
                        time.time(),
                    ),
                )
                self._trim(connection)
                self._write_count += 1
                if self._write_count % 50 == 0:
                    self._prune_global(connection)
                connection.commit()
            except sqlite3.Error as exc:
                logger.warning("browser journal write failed: %s", exc)

    async def arecord(self, **kwargs: Any) -> None:
        """Async wrapper running :meth:`record` off the event loop."""
        await asyncio.to_thread(self.record, **kwargs)

    def _trim(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "DELETE FROM actions WHERE owner=? AND logical_session=? AND action_id NOT IN ("
            "SELECT action_id FROM actions WHERE owner=? AND logical_session=? ORDER BY rowid DESC LIMIT ?)",
            (self.owner, self.logical_session, self.owner, self.logical_session, self.limit),
        )

    def lookup(self, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            connection = self._connection
            if connection is None:
                return None
            try:
                row = connection.execute(
                    _LOOKUP_SQL,
                    (action_id, self.owner, self.logical_session),
                ).fetchone()
            except sqlite3.Error as exc:
                logger.warning("browser journal read failed: %s", exc)
                return None
        if row is None:
            return None
        return {"action_id": row[0], **dict(zip(_COLUMNS, row[1:], strict=True))}

    def rows_for(self, tool: str, document_epoch: int) -> list[dict[str, Any]]:
        """Every row of *tool* recorded against *document_epoch*, newest first.

        Rows still at ``prepared``/``dispatched`` are included: an action that
        never reached its result row may have taken effect all the same.
        """
        with self._lock:
            connection = self._connection
            if connection is None:
                return []
            try:
                rows = connection.execute(
                    _ROWS_FOR_SQL,
                    (self.owner, self.logical_session, tool, int(document_epoch)),
                ).fetchall()
            except sqlite3.Error as exc:
                logger.warning("browser journal read failed: %s", exc)
                return []
        return [{"action_id": row[0], **dict(zip(_COLUMNS, row[1:], strict=True))} for row in rows]

    def count(self) -> int:
        with self._lock:
            connection = self._connection
            if connection is None:
                return 0
            try:
                row = connection.execute(
                    "SELECT COUNT(*) FROM actions WHERE owner=? AND logical_session=?",
                    (self.owner, self.logical_session),
                ).fetchone()
            except sqlite3.Error:
                return 0
            return int(row[0]) if row else 0

    def close(self) -> None:
        with self._lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass


def describe(record: dict[str, Any]) -> str:
    """Render a record for the model, downgrading incomplete phases to uncertain."""
    payload = dict(record)
    for column in _STRUCTURED_COLUMNS:
        if not payload.get(column):
            payload.pop(column, None)
    phase = str(payload.get("phase", ""))
    if phase in {PREPARED, DISPATCHED}:
        payload["status"] = "uncertain"
        payload["reason"] = (
            "no result was recorded; the action may or may not have been sent "
            "(the plugin stopped mid-action). Check the page before repeating it."
        )
    payload["retryable"] = False
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
