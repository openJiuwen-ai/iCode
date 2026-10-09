# ruff: noqa: RUF002, RUF003
"""SQLite 原文落库（telemetry-mock 专用，仅标准库）。

设计要点（对齐方案 §六 / TS 版 ``store.ts``，并按决策清理 rev.5 残留列
``turn_content_hash`` / ``analysis_version`` / ``latest_update_json`` 及其幂等机制——
mock 的职责是观测收到的报文，全部裸插入，行为最可预测）：

- 每个接口一张表 + 一张 ``rejected_reports``，``body_json`` 保存完整原始报文；
- 单连接 + 线程锁（ThreadingHTTPServer 每请求一线程，sqlite 连接不可跨线程裸用）；
- ``:memory:`` 为默认（进程内测试）；文件路径时自动创建父目录。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

# 接口短名 → 表名（/debug/dump 与 /debug/reports 的键用短名）
INTERFACE_TABLES: dict[str, str] = {
    "tool-detail/save": "tool_detail_saves",
    "tool-detail/batch-save": "batch_saves",
    "tool-detail/update": "tool_detail_updates",
    "ai-code/save": "ai_code_saves",
    "event-reaction/save": "event_reactions",
    "rejected": "rejected_reports",
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tool_detail_saves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at TEXT NOT NULL,
  session_id TEXT,
  func_id TEXT,
  func_type INTEGER,
  func_name TEXT,
  span_id TEXT,
  request_id TEXT,
  code_status INTEGER,
  has_value INTEGER NOT NULL,
  body_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_saves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at TEXT NOT NULL,
  session_id TEXT,
  span_id TEXT,
  item_count INTEGER NOT NULL,
  body_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_detail_updates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at TEXT NOT NULL,
  func_id TEXT,
  code_status INTEGER,
  has_error_message INTEGER NOT NULL,
  original_lines INTEGER,
  added_lines INTEGER,
  deleted_lines INTEGER,
  body_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_code_saves (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at TEXT NOT NULL,
  report_id TEXT,
  session_id TEXT,
  span_id TEXT,
  request_id TEXT,
  source_type TEXT,
  language TEXT,
  filepath TEXT,
  block_count INTEGER NOT NULL,
  body_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_reactions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at TEXT NOT NULL,
  body_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rejected_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at TEXT NOT NULL,
  interface TEXT NOT NULL,
  problem TEXT NOT NULL,
  body_json TEXT NOT NULL
);
"""


# 各表支持的查询过滤列（/debug/reports 的 sessionId/funcId 只对有该列的表生效）
_QUERY_FILTER_COLUMNS: dict[str, tuple[str, ...]] = {
    "tool-detail/save": ("session_id", "func_id"),
    "tool-detail/batch-save": ("session_id",),
    "tool-detail/update": ("func_id",),
    "ai-code/save": ("session_id",),
    "event-reaction/save": (),
    "rejected": (),
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    return None


def _dumps(body: object) -> str:
    return json.dumps(body, ensure_ascii=False)


class MockStore:
    """线程安全的报文落库。"""

    def __init__(self, database_path: str | Path = ":memory:") -> None:
        self._database_path = str(database_path)
        if self._database_path != ":memory:":
            Path(self._database_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._database_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    @property
    def database_path(self) -> str:
        return self._database_path

    def insert_tool_detail_save(self, body: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO tool_detail_saves (received_at, session_id, func_id, func_type, func_name,"
                " span_id, request_id, code_status, has_value, body_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(),
                    _text_or_none(body.get("sessionId")),
                    _text_or_none(body.get("funcId")),
                    _int_or_none(body.get("funcType")),
                    _text_or_none(body.get("funcName")),
                    _text_or_none(body.get("spanId")),
                    _text_or_none(body.get("requestId")),
                    _int_or_none(body.get("codeStatus")),
                    1 if "value" in body else 0,
                    _dumps(body),
                ),
            )
            self._conn.commit()

    def insert_batch_save(self, body: list) -> None:
        first = body[0] if body and isinstance(body[0], dict) else {}
        with self._lock:
            self._conn.execute(
                "INSERT INTO batch_saves (received_at, session_id, span_id, item_count, body_json)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    _now(),
                    _text_or_none(first.get("sessionId")),
                    _text_or_none(first.get("spanId")),
                    len(body),
                    _dumps(body),
                ),
            )
            self._conn.commit()

    def insert_update(self, body: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO tool_detail_updates (received_at, func_id, code_status, has_error_message,"
                " original_lines, added_lines, deleted_lines, body_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(),
                    _text_or_none(body.get("funcId")),
                    _int_or_none(body.get("codeStatus")),
                    1 if _text_or_none(body.get("funcErrorMessage")) else 0,
                    _int_or_none(body.get("originalLines")),
                    _int_or_none(body.get("addedLines")),
                    _int_or_none(body.get("deletedLines")),
                    _dumps(body),
                ),
            )
            self._conn.commit()

    def insert_ai_code(self, body: dict) -> None:
        blocks = body.get("blocks")
        with self._lock:
            self._conn.execute(
                "INSERT INTO ai_code_saves (received_at, report_id, session_id, span_id, request_id,"
                " source_type, language, filepath, block_count, body_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    _now(),
                    _text_or_none(body.get("reportId")),
                    _text_or_none(body.get("sessionId")),
                    _text_or_none(body.get("spanId")),
                    _text_or_none(body.get("requestId")),
                    _text_or_none(body.get("sourceType")),
                    _text_or_none(body.get("language")),
                    _text_or_none(body.get("filepath")),
                    len(blocks) if isinstance(blocks, list) else 0,
                    _dumps(body),
                ),
            )
            self._conn.commit()

    def insert_event_reaction(self, body: object) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO event_reactions (received_at, body_json) VALUES (?, ?)",
                (_now(), _dumps(body)),
            )
            self._conn.commit()

    def insert_rejected(self, interface: str, problem: str, body: object) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO rejected_reports (received_at, interface, problem, body_json) VALUES (?, ?, ?, ?)",
                (_now(), interface, problem, _dumps(body)),
            )
            self._conn.commit()

    def query(
        self,
        interface: str,
        *,
        session_id: str | None = None,
        func_id: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        table = INTERFACE_TABLES[interface]
        columns = _QUERY_FILTER_COLUMNS[interface]
        clauses: list[str] = []
        params: list[object] = []
        if session_id is not None and "session_id" in columns:
            clauses.append("session_id = ?")
            params.append(session_id)
        if func_id is not None and "func_id" in columns:
            clauses.append("func_id = ?")
            params.append(func_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM {table}{where} ORDER BY id DESC LIMIT ?",  # noqa: S608
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def dump(self, *, limit: int = 500) -> dict[str, list[dict]]:
        result: dict[str, list[dict]] = {}
        with self._lock:
            for interface, table in INTERFACE_TABLES.items():
                rows = self._conn.execute(
                    f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?",  # noqa: S608
                    (limit,),
                ).fetchall()
                result[interface] = [dict(row) for row in rows]
        return result

    def clear(self) -> None:
        with self._lock:
            for table in INTERFACE_TABLES.values():
                self._conn.execute(f"DELETE FROM {table}")  # noqa: S608
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
