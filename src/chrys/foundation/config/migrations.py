# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Migrate notification preferences and rename saved themes in user settings.

Notification imports use a ``migrations:`` ledger with either a completion
stamp or ``{pending_cleanup: true}``. The source is read under its own lock
before taking the settings lock, then three phases hold at most one lock:

- A merges into the settings document and records the source fingerprint with
  ``pending_cleanup``.
- B rechecks that fingerprint under the source lock, then renames the source
  and its backup. Changed or unreadable bytes stay in place for a later retry.
- C replaces the pending entry with a completion stamp. Repeating B after a
  crash is safe.

The document wins on an initial import and while a pending source is unchanged,
so retrying cannot undo later panel edits. A changed pending source or a source
written after completion represents newer legacy preferences and wins instead.

Theme renames update one value under the settings lock and need no ledger.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chrys.foundation.config.notification_events import NotificationEvent
from chrys.foundation.config.settings import RENAMED_THEMES, Settings
from chrys.foundation.config.spec import specs_by_field
from chrys.foundation.config.user_settings import apply_settings_patch, flatten_user_doc, user_settings_path
from chrys.foundation.config.yaml_store import (
    LOCK_TIMEOUT_SECONDS,
    backup_path_for,
    lock_path_for,
    read_yaml_doc,
    read_yaml_document,
    update_yaml_doc,
)
from chrys.foundation.platform.files import file_fingerprint
from chrys.foundation.util.lock import FileLock

logger = logging.getLogger(__name__)

NOTIFICATIONS_COMPONENT = "notifications_v0"


class _AlreadyComplete(Exception):
    """Raised inside the mutator so the document is not rewritten per boot."""


@dataclass(frozen=True, slots=True)
class MigrationResult:
    """Values written by a migration and whether it changed the settings document."""

    migrated: dict[str, Any]
    performed: bool
    """``False`` when the migration was already complete or no rewrite was needed."""


_LEGACY_NOTIFICATION_FIELDS: dict[str, str] = {
    "enabled": "notifications.enabled",
    "desktop": "notifications.delivery.desktop",
    "sound": "notifications.delivery.sound",
    "suppress_when_focused": "notifications.suppress_when_focused",
}


def _all_notification_keys() -> tuple[str, ...]:
    """Every settings key the retired ``notifications.yaml`` could speak for."""
    return (
        *_LEGACY_NOTIFICATION_FIELDS.values(),
        *(f"notifications.events.{event.value}" for event in NotificationEvent),
    )


def _legacy_notifications_path() -> Path:
    from chrys.foundation.platform import get_platform

    return get_platform().config_dir / "notifications.yaml"


def migrate_notifications_v0() -> MigrationResult:
    """Fold ``notifications.yaml`` into the ``notifications.*`` settings keys.

    The legacy loader's reading was ``bool(value)`` per present field with
    missing fields defaulting on, and that is reproduced here so migration
    writes what the old reader would have read. Every written value is a
    canonical bool, so nothing enters the document that a panel could not
    write. Unknown event names and stray fields are dropped, as they always
    were; ``schema_version`` belongs to the retired format.
    """
    legacy_path = _legacy_notifications_path()
    # Read under the legacy file's own lock *before* taking the settings lock:
    # migrations hold at most one configuration lock at any moment. Once the
    # component completes, the rename below makes this a cheap miss per boot.
    # The fingerprint comes out of the same call rather than from a digest
    # taken beside it: this read repairs a corrupt primary from its backup, so
    # a digest taken before it describes a file that no longer exists and one
    # taken after it races the read it is supposed to describe.
    read = read_yaml_document(legacy_path)
    legacy_doc, fingerprint = read.doc, read.fingerprint

    candidates: dict[str, bool] = {}
    if legacy_doc is not None:
        for field_name, key in _LEGACY_NOTIFICATION_FIELDS.items():
            if field_name in legacy_doc:
                candidates[key] = bool(legacy_doc[field_name])
        events = legacy_doc.get("events")
        if isinstance(events, dict):
            for raw_name, value in events.items():
                try:
                    event = NotificationEvent(str(raw_name))
                except ValueError:
                    continue
                candidates[f"notifications.events.{event.value}"] = bool(value)
    # What the old reader would have reported for the whole feature, which is
    # not the same list: it defaulted every absent field to on, so this format
    # has no way to say "unset" and a partial file still describes a complete
    # state. That only matters once the component has completed and the file
    # came back — then the file *is* the newer state and has to replace the
    # document wholesale, or a field the returning writer never mentioned keeps
    # a value the user's old build is not showing them. Before completion the
    # sparse list is the right one: filling in defaults there would overwrite
    # panel edits with "on" for every field the legacy file happens to omit.
    complete_state = dict.fromkeys(_all_notification_keys(), True) | candidates

    known_keys = frozenset(entry.key for entry in specs_by_field(Settings).values())
    migrated: dict[str, Any] = {}

    def fold_in(doc: dict[str, Any]) -> dict[str, Any]:
        entry = _ledger_entry(doc)
        completed = entry is not None and not _pending_cleanup(entry)
        if completed and legacy_doc is None:
            # This file has no role left once the component completes, which
            # renamed the file away. Anything back at this path was written by
            # something that does not know the document exists.
            raise _AlreadyComplete
        document_wins = not completed and _document_is_newer(entry, fingerprint)
        migrated.clear()
        existing, _ = flatten_user_doc(doc, known_keys)
        for key, value in (complete_state if completed else candidates).items():
            if key in existing and document_wins:
                # The document's value is newer: a rerun after a crash between
                # phases must not roll a later panel edit back to the old file.
                # Past completion the reverse holds — see the module docstring.
                # Phase B retires the file, so this is the last moment the
                # disagreement exists.
                logger.info(
                    "Settings document already sets %s; dropping the legacy value in %s",
                    key,
                    legacy_path,
                )
                continue
            migrated[key] = value
        apply_settings_patch(doc, migrated)
        _set_ledger(doc, _pending(fingerprint))
        return doc

    try:
        update_yaml_doc(user_settings_path(), fold_in)
    except _AlreadyComplete:
        return MigrationResult(migrated={}, performed=False)

    result = MigrationResult(migrated=dict(migrated), performed=True)

    # Phase B: the old document is renamed, never deleted — kept for rollback
    # and debugging. Its backup is renamed first; left behind, a crashed rerun
    # would restore the primary from it and resurrect the file just retired —
    # which is why the backup is retired even when the primary is already gone.
    # Recheck the fingerprint under the file's own lock: renaming a source
    # changed since phase A would retire preferences nobody imported.
    backup = backup_path_for(legacy_path)
    if legacy_path.is_file() or backup.is_file():
        with FileLock(lock_path_for(legacy_path), timeout=LOCK_TIMEOUT_SECONDS):
            current = file_fingerprint(legacy_path)
            # A file that is present but cannot be digested has no identity to
            # compare against, and ``None == None`` would pass for proof that
            # it is unchanged — retiring bytes nobody managed to read. A file
            # that reads but does not *parse* still digests, so that case
            # retires as it should rather than deferring forever.
            unreadable = current is None and legacy_path.is_file()
            if current != fingerprint or unreadable:
                logger.warning(
                    "Could not verify that %s is unchanged. Notification migration will retry on the next start.",
                    legacy_path,
                )
                return result
            if backup.is_file():
                backup.replace(backup.with_name(backup.name + ".migrated"))
            if legacy_path.is_file():
                legacy_path.replace(legacy_path.with_name(legacy_path.name + ".migrated"))

    update_yaml_doc(user_settings_path(), _mark_complete)
    return result


def migrate_renamed_theme() -> MigrationResult:
    """Rewrite a saved ``ui.theme`` that names a built-in theme by a name it no longer has.

    Nothing draws wrong without this — the loader follows :data:`RENAMED_THEMES`
    at every layer. It is here so the document says what the panel shows, and
    still names a theme for a build that has dropped the redirect.

    There is no ledger entry, because the rewrite is its own record: once the
    old name is gone there is nothing left to do, and a downgraded install that
    writes it back is simply renamed again. Looking before locking keeps the
    ordinary start — nothing to rename — from needing a writable config
    directory, which a read does not.
    """
    path = user_settings_path()
    key = specs_by_field(Settings)["theme"].key
    done = MigrationResult(migrated={}, performed=False)
    if _renamed_theme(read_yaml_doc(path), key) is None:
        return done

    migrated: dict[str, Any] = {}

    def rename(doc: dict[str, Any]) -> dict[str, Any]:
        successor = _renamed_theme(doc, key)
        if successor is None:
            # Another process got here between the look and the lock.
            raise _AlreadyComplete
        migrated[key] = successor
        return apply_settings_patch(doc, migrated)

    try:
        update_yaml_doc(path, rename)
    except _AlreadyComplete:
        return done
    return MigrationResult(migrated=dict(migrated), performed=True)


def _renamed_theme(doc: dict[str, Any] | None, key: str) -> str | None:
    """The name the document's theme goes by now, when it is saved under a retired one."""
    if doc is None:
        return None
    saved = flatten_user_doc(doc, frozenset({key}))[0].get(key)
    return RENAMED_THEMES.get(saved.strip()) if isinstance(saved, str) else None


def _mark_complete(doc: dict[str, Any]) -> dict[str, Any]:
    _set_ledger(doc, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    return doc


def _pending(fingerprint: str | None) -> dict[str, Any]:
    """The ledger entry for an import whose cleanup has not run yet.

    It carries the digest the values were imported from, because "the document
    is newer than the source" is only true while the source has not moved
    since; without recording it, the two reasons a component can be pending
    are indistinguishable. See :func:`_document_is_newer`.
    """
    entry: dict[str, Any] = {"pending_cleanup": True}
    if fingerprint is not None:
        entry["source_fingerprint"] = fingerprint
    return entry


def _document_is_newer(entry: Any, fingerprint: str | None) -> bool:
    """Whether the document outranks the legacy source where both hold a key.

    True for a component that has never run — the document may be something
    the user wrote by hand — and while the source still digests to what the
    pending import read, which is the crash-between-phases case: a rerun there
    must not roll a later panel edit back to the old file.

    When the current fingerprint differs from the recorded one, values read
    from the changed source take precedence on retry. If the pending entry
    has no digest (for example, the source was unreadable), we cannot tell
    that it changed, so existing settings in the document take precedence.
    """
    if not isinstance(entry, dict):
        return True
    recorded = entry.get("source_fingerprint")
    return not isinstance(recorded, str) or recorded == fingerprint


def _set_ledger(doc: dict[str, Any], value: Any) -> None:
    ledger = doc.get("migrations")
    if not isinstance(ledger, dict):
        ledger = {}
        doc["migrations"] = ledger
    ledger[NOTIFICATIONS_COMPONENT] = value


def _ledger_entry(doc: dict[str, Any]) -> Any:
    ledger = doc.get("migrations")
    return ledger.get(NOTIFICATIONS_COMPONENT) if isinstance(ledger, dict) else None


def _pending_cleanup(entry: Any) -> bool:
    return isinstance(entry, dict) and bool(entry.get("pending_cleanup"))
