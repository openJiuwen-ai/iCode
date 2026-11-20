# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded owner-only JSON grants, re-read under the shared file lock before writes."""

from __future__ import annotations

import errno
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.foundation.config.settings import resolve_sessions_dir
from chrys.foundation.platform.files import (
    SecureFileError,
    atomic_write_owner_only_text,
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

    def _read(self) -> list[dict[str, Any]]:
        try:
            with secure_open_owner_only_binary(self.path) as handle:
                raw = handle.read(MAX_BYTES + 1)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                return []
            raise
        if len(raw) > MAX_BYTES:
            raise ValueError("Grant store is too large")
        data = json.loads(raw)
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("rules"), list):
            raise ValueError("Unknown grant schema")
        rules = data["rules"]
        if len(rules) > MAX_GRANTS or any(not isinstance(rule, dict) for rule in rules):
            raise ValueError("Invalid grant records")
        return rules

    def load(self) -> list[dict[str, Any]]:
        self.unreadable = False
        try:
            rules = self._read()
        except OSError, ValueError, RecursionError:
            self.unreadable = True
            if not self._warned_unreadable:
                self._warned_unreadable = True
                # The line stays readable in a terminal; the traceback is one `debug` away.
                logger.warning("Approval grants could not be read; no grants reused")
                logger.debug("Approval grants could not be read", exc_info=True)
            return []
        self._warned_unreadable = False
        return rules

    def _write_rules(self, rules: list[dict[str, Any]]) -> bool:
        """Rewrite the store without reading it; the caller owns the whole payload.

        Repairing unreadable content is not the same thing as following a planted link:
        the leaf is refused here because ``_read`` used to be the only place that did it.
        """
        try:
            if self.path.is_symlink():
                raise SecureFileError(f"Approval store is a symbolic link: {self.path}")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(self.lock_path, timeout=1):
                payload = (
                    json.dumps({"version": 1, "rules": rules}, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
                )
                if len(rules) > MAX_GRANTS or len(payload.encode("utf-8")) > MAX_BYTES:
                    return False
                atomic_write_owner_only_text(self.path, payload)
            self._warned_unreadable = False
            return True
        except OSError, ValueError, TypeError, RecursionError:
            logger.warning("Approval grants could not be written")
            return False

    def _update(self, edit: Callable[[list[dict[str, Any]]], list[dict[str, Any]] | None]) -> bool:
        """Rewrite the locked, re-read rules with *edit*; None from *edit* writes nothing."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(self.lock_path, timeout=1):
                rules = edit(self._read())
                if rules is None:
                    return False
                payload = (
                    json.dumps({"version": 1, "rules": rules}, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
                )
                if len(rules) > MAX_GRANTS or len(payload.encode("utf-8")) > MAX_BYTES:
                    return False
                atomic_write_owner_only_text(self.path, payload)
            return True
        except OSError, ValueError, TypeError, RecursionError:
            logger.warning("Approval grants could not be updated")
            return False

    def add_many(self, additions: list[dict[str, Any]]) -> bool:
        def edit(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
            # Identical grants replace themselves rather than consuming the bound.
            added = [_identity(rule) for rule in additions]
            return [rule for rule in rules if _identity(rule) not in added] + additions

        return self._update(edit)

    def revoke(self, rule_id: str) -> bool:
        def edit(rules: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
            kept = [rule for rule in rules if rule.get("id") != rule_id]
            return kept if len(kept) < len(rules) else None

        return bool(rule_id) and self._update(edit)

    def clear(self, *, project: str | None = None) -> bool:
        # Clearing everything needs no knowledge of what is stored, so it must never
        # depend on a readable store: otherwise an unreadable file is also unrepairable.
        if project is None:
            return self._write_rules([])
        return self._update(lambda rules: [rule for rule in rules if rule.get("project") != project])


def _identity(rule: dict[str, Any]) -> list[object]:
    return [rule.get(field) for field in ("scope", "scope_id", "prefix", "key")]
