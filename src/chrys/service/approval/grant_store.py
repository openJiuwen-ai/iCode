# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded owner-only JSON grants, re-read under the shared file lock before writes."""

from __future__ import annotations

import errno
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.foundation.config.settings import resolve_sessions_dir
from chrys.foundation.models.workdir_migration import BackupOriginal, ReportReadError
from chrys.foundation.platform.files import (
    SecureFileError,
    atomic_write_owner_only_bytes,
    require_writable_file,
    secure_open_owner_only_binary,
)
from chrys.foundation.util.lock import FileLock
from chrys.foundation.util.session_ids import session_short_id

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)
GRANTS_FILE = "approval-grants.json"
MAX_GRANTS = 1000
MAX_BYTES = 4 * 1024 * 1024


def _grants_edit(original: object, *, validate: bool = False) -> dict[str, Any] | None:
    """Convert a legacy grant without granting access through a malformed row."""
    if not isinstance(original, dict):
        return None
    row = original
    if row.get("scope") == "PROJECT" or (row.get("scope") == "SESSION" and "project" in row):
        row = original.copy()
        directory = row.pop("project", None)
        if not isinstance(directory, str) or not os.path.isabs(directory):
            return None
        if row["scope"] == "PROJECT":
            # Both legacy directory constraints had to match for the grant to be usable.
            if directory != row.get("scope_id") or "workdir" in row:
                return None
            row["scope"] = "WORKDIR"
        elif row.setdefault("workdir", directory) != directory:
            return None
    if validate or row is not original:
        from chrys.service.approval.reuse import ApprovalGrant

        try:
            ApprovalGrant.model_validate(row)
        except ValueError, TypeError, RecursionError:
            return None
    return row


def grant_matches_scope(scope: object, scope_id: object, workdir: object, key: tuple[str, str]) -> bool:
    """Match a full workspace id or a directory across the two grant stores."""
    return (scope, scope_id) == key or (scope == "SESSION" and ("WORKDIR", workdir) == key)


def _rule_spans(text: str, count: int) -> list[tuple[int, int]]:
    """Find rule values in the already validated document, keeping their original spelling."""

    def skip_space(index: int) -> int:
        while index < len(text) and text[index] in " \t\r\n":
            index += 1
        return index

    decode = json.JSONDecoder().raw_decode
    index = skip_space(skip_space(int(text.startswith("\ufeff"))) + 1)
    rules_start = 0
    while text[index] != "}":
        key, end = decode(text, index)
        start = skip_space(skip_space(end) + 1)
        _, end = decode(text, start)
        if key == "rules":
            rules_start = start
        index = skip_space(end)
        if text[index] == ",":
            index = skip_space(index + 1)
    spans = []
    index = rules_start + 1
    for _ in range(count):
        start = skip_space(index)
        _, end = decode(text, start)
        spans.append((start, end))
        index = skip_space(end) + 1
    return spans


def session_grants_path(config_dir: Path, session_id: str) -> Path:
    short_id = session_short_id(session_id)
    if short_id in {"", ".", ".."}:
        raise ValueError("A session ID is required")
    return resolve_sessions_dir(config_dir, create=False) / short_id / GRANTS_FILE


class ApprovalGrantStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        # Set by `load`: an unreadable store is not an empty one, and callers must not
        # report it as "no grants" without saying so.
        self.unreadable = False
        # Every match re-reads the store, so warn once per broken spell, not per call.
        self._warned_unreadable = False

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(self.path.name + ".lock")

    def _write_lock(self) -> FileLock:
        return FileLock(self.lock_path, timeout=1)

    def _read(self, *, allow_malformed_rows: bool = False) -> tuple[bytes, list[Any]]:
        try:
            with secure_open_owner_only_binary(self.path) as handle:
                raw = handle.read(MAX_BYTES + 1)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                return b"", []
            raise
        if len(raw) > MAX_BYTES:
            raise ValueError("Grant store is too large")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("rules"), list):
            raise ValueError("Unknown grant schema")
        rules = data["rules"]
        if len(rules) > MAX_GRANTS or (not allow_malformed_rows and any(not isinstance(rule, dict) for rule in rules)):
            raise ValueError("Invalid grant records")
        return raw, rules

    def load(self) -> list[dict[str, Any]]:
        self.unreadable = False
        try:
            _, rules = self._read()
            rows = [row for rule in rules if (row := _grants_edit(rule)) is not None]
        except OSError, ValueError, RecursionError:
            self.unreadable = True
            if not self._warned_unreadable:
                self._warned_unreadable = True
                # The line stays readable in a terminal; the traceback is one `debug` away.
                logger.warning("Approval grants could not be read; no grants reused")
                logger.debug("Approval grants could not be read", exc_info=True)
            return []
        self._warned_unreadable = False
        return rows

    def _write_rules(self, rules: list[dict[str, Any]]) -> bool:
        """Rewrite the store without reading it; the caller owns the whole payload.

        Repairing unreadable content is not the same thing as following a planted link:
        the leaf is refused here because ``_read`` used to be the only place that did it.
        """
        try:
            if self.path.is_symlink():
                raise SecureFileError(f"Approval store is a symbolic link: {self.path}")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._write_lock():
                payload = (
                    json.dumps({"version": 1, "rules": rules}, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
                )
                self._commit(payload, len(rules))
            self._warned_unreadable = False
            return True
        except OSError, ValueError, TypeError, RecursionError:
            logger.warning("Approval grants could not be written")
            return False

    def _update(
        self, edit: Callable[[list[dict[str, Any]]], list[dict[str, Any]] | None], *, unchanged_ok: bool = False
    ) -> bool:
        """Rewrite the locked, re-read rules with *edit*; None from *edit* writes nothing."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._write_lock():
                _, current = self._read()
                rules = edit(current)
                if rules is None:
                    return unchanged_ok
                payload = (
                    json.dumps({"version": 1, "rules": rules}, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
                )
                self._commit(payload, len(rules))
        except OSError, ValueError, TypeError, RecursionError:
            logger.warning("Approval grants could not be updated")
            return False

        return True

    def _commit(self, payload: str, count: int, *, before_write: Callable[[], None] | None = None) -> None:
        encoded = payload.encode("utf-8")
        if count > MAX_GRANTS or len(encoded) > MAX_BYTES:
            raise ValueError("Grant store is too large")
        if before_write is not None:
            before_write()
        atomic_write_owner_only_bytes(self.path, encoded)

    def _migration_rows(self, on_read_error: ReportReadError) -> tuple[bytes, int, dict[int, dict[str, Any]], int]:
        try:
            raw, rules = self._read(allow_malformed_rows=True)
        except (OSError, ValueError, RecursionError) as error:
            on_read_error(self.path, error)
            return b"", 0, {}, 0
        changed = {}
        unmigrated = 0
        for index, rule in enumerate(rules):
            row = _grants_edit(rule, validate=True)
            if row is None:
                unmigrated += 1
            elif row is not rule:
                changed[index] = row
        return raw, len(rules), changed, unmigrated

    def migrate(self, *, backup: BackupOriginal, on_read_error: ReportReadError) -> tuple[bool, int]:
        """Upgrade usable rows under the write lock, retaining every other byte."""
        _, _, changed, unmigrated = self._migration_rows(on_read_error)
        if not changed:
            return False, unmigrated
        try:
            with self._write_lock():
                raw, count, changed, unmigrated = self._migration_rows(on_read_error)
                if not changed:
                    return False, unmigrated
                text = raw.decode("utf-8")
                spans = _rule_spans(text, count)
                parts = []
                end = 0
                for index, row in changed.items():
                    start, stop = spans[index]
                    parts.extend((text[end:start], json.dumps(row, ensure_ascii=True, allow_nan=False)))
                    end = stop
                payload = "".join((*parts, text[end:]))

                def preserve() -> None:
                    require_writable_file(self.path)
                    backup(self.path, raw)

                self._commit(payload, count, before_write=preserve)
        except OSError, ValueError, TypeError, RecursionError:
            return False, 0
        return True, unmigrated

    def add_many(self, additions: list[dict[str, Any]]) -> bool:
        def edit(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
            # Identical grants replace themselves rather than consuming the bound.
            added = [_identity(rule) for rule in additions]
            return [rule for rule in rules if _identity(_grants_edit(rule)) not in added] + additions

        return self._update(edit)

    def revoke(self, rule_id: str) -> bool:
        def edit(rules: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
            kept = [rule for rule in rules if rule.get("id") != rule_id]
            return kept if len(kept) < len(rules) else None

        return bool(rule_id) and self._update(edit)

    def clear(self, *, scope_key: tuple[str, str] | None = None) -> bool:
        # Clearing everything needs no knowledge of what is stored, so it must never
        # depend on a readable store: otherwise an unreadable file is also unrepairable.
        if scope_key is None:
            return self._write_rules([])

        def edit(rules: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
            kept = []
            for rule in rules:
                row = _grants_edit(rule)
                if row is None or not grant_matches_scope(
                    row.get("scope"), row.get("scope_id"), row.get("workdir"), scope_key
                ):
                    kept.append(rule)
            return kept if len(kept) < len(rules) else None

        return self._update(edit, unchanged_ok=True)


def _identity(rule: dict[str, Any] | None) -> list[object]:
    return [rule.get(field) for field in ("scope", "scope_id", "workdir", "prefix", "key")] if rule is not None else []
