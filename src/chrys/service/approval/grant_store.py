# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded owner-only JSON grants, re-read under the shared file lock before writes."""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.foundation.config.settings import resolve_sessions_dir
from chrys.foundation.models.workdir_migration import BackupOriginal, ReportReadError
from chrys.foundation.platform.files import (
    SecureFileError,
    atomic_write_owner_only_bytes,
    replace_blocker,
    require_writable_file,
    secure_open_owner_only_binary,
    verify_owner_identity_path,
)
from chrys.foundation.util.lock import FileLock
from chrys.foundation.util.session_ids import session_short_id

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)
GRANTS_FILE = "approval-grants.json"
MAX_GRANTS = 1000
MAX_BYTES = 4 * 1024 * 1024

_WINDOWS_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400


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


def _scope_edit(scope_key: tuple[str, str]) -> Callable[[list[dict[str, Any]]], list[dict[str, Any]] | None]:
    """Build the edit that drops every grant matching *scope_key*; None when nothing matched."""

    def edit(rules: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        kept = []
        for rule in rules:
            row = _grants_edit(rule)
            if row is None or not grant_matches_scope(
                row.get("scope"), row.get("scope_id"), row.get("workdir"), scope_key
            ):
                kept.append(rule)
        return kept if len(kept) < len(rules) else None

    return edit


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

    def _verify_target_repairable(self) -> None:
        """Refuse a leaf that clearing must not replace, checked under the file lock.

        Repairing an unreadable store is only safe when the path names a regular
        file owned by the current user — or nothing at all, so a store that was
        never written can be created. Mode and content are deliberately not
        checked: a world-readable or unparsable file is exactly what ``clear``
        exists to repair. A symlink, FIFO, socket, reparse point or directory is
        rejected, and on POSIX a file owned by someone else is rejected as well,
        instead of being swapped out by the atomic replace that follows.
        """
        try:
            info = self.path.lstat()
        except FileNotFoundError:
            return
        if os.name == "nt":
            # st_file_attributes exists only on the Windows stat_result; reading it
            # through getattr keeps this module type-checkable on Linux and macOS.
            self._verify_windows_target(getattr(info, "st_file_attributes", 0))
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise SecureFileError(f"Approval store is not a regular file: {self.path}")
        if info.st_uid != os.geteuid():
            raise SecureFileError(f"Approval store is not owned by the current user: {self.path}")

    def _verify_windows_target(self, attributes: int) -> None:
        """Reject a Windows leaf that clearing must not replace, owner SID included."""
        if attributes & (_WINDOWS_FILE_ATTRIBUTE_DIRECTORY | _WINDOWS_FILE_ATTRIBUTE_REPARSE_POINT):
            raise SecureFileError(f"Approval store is not a regular file: {self.path}")
        # The read path verifies the owner SID; replacing the file must too, or a
        # writable directory lets `clear` overwrite another user's grants. The
        # identity-only open asks for no data access, so a DACL that denies reading
        # the file — which `clear` exists to repair — does not block the repair.
        try:
            verify_owner_identity_path(self.path)
        except OSError as error:
            raise SecureFileError(f"Approval store owner could not be verified: {self.path} ({error})") from error

    def target_problem(self) -> str | None:
        """Return why ``clear`` cannot replace this store's leaf, or None when it may.

        Mirrors the whole write path, not just the leaf: a repair hint is only worth
        printing when the command it names could actually succeed.
        """
        try:
            self._verify_target_repairable()
        except (OSError, ValueError) as error:
            return str(error) or error.__class__.__name__
        return replace_blocker(self.path)

    def _write_rules(self, rules: list[dict[str, Any]]) -> bool:
        return self._write_rules_or_reason(rules) is None

    def _write_rules_or_reason(self, rules: list[dict[str, Any]]) -> str | None:
        """Rewrite the store without reading it; the caller owns the whole payload."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._write_lock():
                return self._write_rules_locked(rules)
        except (OSError, ValueError, TypeError, RecursionError) as error:
            logger.warning("Approval grants could not be written")
            return str(error) or error.__class__.__name__

    def _write_rules_locked(self, rules: list[dict[str, Any]]) -> str | None:
        """Rewrite the store while the caller holds the write lock.

        Repairing unreadable content is not the same thing as following a planted
        link or replacing a special file: the leaf is re-verified under the lock
        so a concurrent swap cannot slip past the check before the atomic write.
        """
        try:
            self._verify_target_repairable()
            payload = json.dumps({"version": 1, "rules": rules}, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
            self._commit(payload, len(rules))
            self._warned_unreadable = False
            return None
        except (OSError, ValueError, TypeError, RecursionError) as error:
            logger.warning("Approval grants could not be written")
            return str(error) or error.__class__.__name__

    def _update(
        self, edit: Callable[[list[dict[str, Any]]], list[dict[str, Any]] | None], *, unchanged_ok: bool = False
    ) -> bool:
        """Rewrite the locked, re-read rules with *edit*; None from *edit* writes nothing."""
        return self._update_or_reason(edit, unchanged_ok=unchanged_ok) is None

    def _update_or_reason(
        self, edit: Callable[[list[dict[str, Any]]], list[dict[str, Any]] | None], *, unchanged_ok: bool = False
    ) -> str | None:
        """Rewrite the locked, re-read rules with *edit*; None from *edit* writes nothing."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._write_lock():
                _, current = self._read()
                # A successful re-read proves the file is readable again, whether or
                # not *edit* matches anything: otherwise a later breakage stays silent.
                self._warned_unreadable = False
                rules = edit(current)
                if rules is None:
                    return None if unchanged_ok else "No matching grant"
                payload = (
                    json.dumps({"version": 1, "rules": rules}, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
                )
                self._commit(payload, len(rules))
            self._warned_unreadable = False
        except (OSError, ValueError, TypeError, RecursionError) as error:
            logger.warning("Approval grants could not be updated")
            return str(error) or error.__class__.__name__

        return None

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
        return self.clear_or_reason(scope_key=scope_key) is None

    def clear_or_reason(self, *, scope_key: tuple[str, str] | None = None) -> str | None:
        """Clear grants; return why the store could not be repaired, or None on success.

        Clearing everything needs no knowledge of what is stored, so it must never
        depend on a readable store: otherwise an unreadable file is also unrepairable.
        An already-empty store is decided under the write lock, so a concurrent writer
        that is mid-commit cannot make the clear report success before its grants land.
        """
        if scope_key is not None:
            return self._update_or_reason(_scope_edit(scope_key), unchanged_ok=True)
        return self._clear_everything_or_reason()

    def _clear_everything_or_reason(self) -> str | None:
        """Empty the store under one lock; an already-empty readable store is not rewritten."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._write_lock():
                try:
                    _, rules = self._read()
                except OSError, ValueError, RecursionError:
                    # Unreadable content is what the repair path exists for.
                    return self._write_rules_locked([])
                if not rules:
                    # Nothing to change and nothing to write: a read-only parent or a
                    # lock held elsewhere cannot fail a clear that has no work to do.
                    self._warned_unreadable = False
                    return None
                return self._write_rules_locked([])
        except (OSError, ValueError, TypeError, RecursionError) as error:
            logger.warning("Approval grants could not be written")
            return str(error) or error.__class__.__name__


def _identity(rule: dict[str, Any] | None) -> list[object]:
    return [rule.get(field) for field in ("scope", "scope_id", "workdir", "prefix", "key")] if rule is not None else []
