# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One versioned SQLite table; transactions avoid lost updates and stale revocation."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)


class DAAStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def _connection(self, *, create: bool = False) -> Iterator[sqlite3.Connection]:
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.path.is_file():
            raise OSError("No DAA store")
        connection = sqlite3.connect(self.path, timeout=1)
        try:
            with connection:
                if create:
                    connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 0 and create:
                    # Never adopt an unknown pre-existing database's tables.
                    if connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                        raise ValueError("Unknown DAA schema")
                    connection.execute("CREATE TABLE rules (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                    connection.execute("PRAGMA user_version=1")
                elif version != 1:
                    raise ValueError("Unknown DAA schema")
                yield connection
        finally:
            connection.close()

    def load(self) -> list[tuple[str, str]]:
        try:
            with self._connection() as connection:
                return [
                    (row[0], row[1])
                    for row in connection.execute("SELECT id, payload FROM rules")
                    if isinstance(row[0], str) and isinstance(row[1], str)
                ]
        except OSError, ValueError, sqlite3.Error:
            return []

    def add(self, rule_id: str, payload: str) -> bool:
        return self.add_many([(rule_id, payload)])

    def add_many(self, rules: list[tuple[str, str]]) -> bool:
        try:
            with self._connection(create=True) as connection:
                connection.executemany("INSERT INTO rules VALUES (?, ?)", rules)
            return True
        except OSError, ValueError, sqlite3.Error:
            logger.warning("DAA rule could not be saved; approval applies only once")
            return False

    def revoke(self, rule_id: str) -> bool:
        try:
            with self._connection() as connection:
                return connection.execute("DELETE FROM rules WHERE id=?", (rule_id,)).rowcount > 0
        except OSError, ValueError, sqlite3.Error:
            return False

    def clear(self) -> bool:
        try:
            with self._connection() as connection:
                connection.execute("DELETE FROM rules")
            return True
        except OSError, ValueError, sqlite3.Error:
            return False
