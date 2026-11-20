# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The user settings document: path, dotted-key flattening, and patching.

``~/.chrys/settings.yaml`` stores dotted setting keys as nested YAML so a
human can edit it. This module owns the two directions of that shape —
nested document to ``{"ui.theme": ...}`` for the loader, and a dotted-key
patch back into the nested document for :func:`persist` — plus the document
housekeeping (``schema_version``, the ``migrations`` ledger) that must never
be mistaken for settings.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Final

from chrys.foundation.config.coercion import bool_coercer
from chrys.foundation.config.yaml_store import backup_path_for, migrate_yaml_doc
from chrys.foundation.models.workdir_migration import BackupOriginal
from chrys.foundation.platform.files import require_writable_file, secure_open_regular_binary

SCHEMA_VERSION: Final = 1

_OLD_TRUST_KEYS = {
    "project.config_enabled": "workdir.config_enabled",
    "project.hooks_enabled": "workdir.hooks_enabled",
    "project.skills_enabled": "workdir.skills_enabled",
}
_TRUST_BOOL = bool_coercer()

RESERVED_DOC_KEYS: Final = frozenset({"schema_version", "migrations"})
"""Top-level housekeeping keys: never settings, never "unknown"."""

RETIRED_SETTING_KEYS: Final = frozenset(
    {"ui.chat.fold_finished_turns", "ui.chat.show_thinking", "ui.chat.tool_groups_expanded"}
)
"""Settings that no longer exist and replace nothing: a saved value is skipped, never "unknown".

The document is left as it is.
"""


def user_settings_path() -> Path:
    """The user-layer settings document in the platform configuration directory."""
    from chrys.foundation.platform import get_platform

    return get_platform().config_dir / "settings.yaml"


def flatten_user_doc(
    doc: Mapping[str, Any],
    known_keys: frozenset[str],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """Flatten a nested document into dotted keys, splitting off strangers.

    A known dotted key is a leaf even when its value is a mapping — the wrong
    shape for a setting is that setting's invalid value, to be rejected with
    its own warning, not a batch of unknown grandchildren. Unknown keys are
    returned, not dropped silently: they stay in the file on write (a patch
    never rewrites what it did not touch), and the loader reports them once.
    A key in :data:`RETIRED_SETTING_KEYS` is neither: it is skipped unless the
    caller asks for it by name.
    """
    values: dict[str, Any] = {}
    unknown: list[str] = []

    def walk(prefix: str, node: Mapping[str, Any]) -> None:
        for name, value in node.items():
            dotted = f"{prefix}.{name}" if prefix else str(name)
            if not prefix and dotted in RESERVED_DOC_KEYS:
                continue
            if dotted in RETIRED_SETTING_KEYS and dotted not in known_keys:
                continue
            if dotted in known_keys:
                values[dotted] = value
            elif isinstance(value, Mapping):
                walk(dotted, value)
            else:
                unknown.append(dotted)

    walk("", doc)
    return values, tuple(unknown)


def apply_settings_patch(
    doc: dict[str, Any],
    values: Mapping[str, Any],
    remove: Iterable[str] = (),
) -> dict[str, Any]:
    """Apply a dotted-key patch to a nested document, in place, and return it.

    Only the named keys move; everything else — unknown keys included — is
    written back untouched, so downgrading to a version that wrote keys this
    one does not know cannot lose them. Removal prunes emptied parent
    mappings so a cleared setting does not leave ``ui: {}`` litter behind.

    One exception, stated rather than defended: a *scalar* sitting where a
    patched key needs a mapping (``ui: something`` under a patch for
    ``ui.theme``) is replaced, because the two cannot both be there. Such a
    node is already reported as an unknown key by every load, so this replaces
    something the reader was told was not in effect.

    A patched or removed key also sheds its rival spellings: the flattener
    accepts a known key written as a dotted literal at any depth, and a
    surviving ``ui.theme: dark`` line would keep answering for a setting the
    patch just moved elsewhere. Explicit trust edits also remove retired spellings.
    """
    doc.setdefault("schema_version", SCHEMA_VERSION)
    remove = tuple(remove)
    edited = values.keys() | set(remove)
    old_keys = {old for old, new in _OLD_TRUST_KEYS.items() if new in edited}
    if old_keys and isinstance(workdir := doc.get("workdir"), dict):
        # A YAML alias can share this mapping with unrelated settings.
        doc["workdir"] = workdir.copy()
    for dotted, value in values.items():
        node = doc
        parts = dotted.split(".")
        for part in parts[:-1]:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[parts[-1]] = value
        _prune_dotted_spellings(doc, parts)
    for dotted in remove:
        parts = dotted.split(".")
        _remove_dotted(doc, parts)
        _prune_dotted_spellings(doc, parts)
    _remove_legacy_trust_keys(doc, old_keys)
    return doc


def settings_edit(doc: dict[str, Any], *, on_conflict: Callable[[str], None] | None = None) -> dict[str, Any] | None:
    """Move the previous trust settings to their current keys in place."""
    existing, _ = flatten_user_doc(doc, frozenset((*_OLD_TRUST_KEYS, *_OLD_TRUST_KEYS.values())))
    old_keys = existing.keys() & _OLD_TRUST_KEYS.keys()
    if not old_keys:
        return None
    writes: dict[str, Any] = {}
    for old, new in _OLD_TRUST_KEYS.items():
        if old not in old_keys:
            continue
        value = existing[old]
        if new not in existing:
            writes[new] = value
        elif _TRUST_BOOL(value).value != _TRUST_BOOL(existing[new]).value:
            writes[new] = False
            if on_conflict is not None:
                on_conflict(new)
    if writes:
        workdir = doc.get("workdir", {})
        if isinstance(workdir, dict):
            doc["workdir"] = {**workdir, **{key.split(".", 1)[1]: value for key, value in writes.items()}}
            for key in writes:
                doc.pop(key, None)
        else:
            doc.update(writes)
    _remove_legacy_trust_keys(doc, old_keys)
    return doc


def migrate_trust_settings(path: Path, backup: BackupOriginal) -> tuple[bool, list[str]]:
    """Rewrite the trust keys, preserving the bytes of both affected settings files."""
    changed = False
    conflicts: list[str] = []

    def preserve(target: Path, raw: bytes | None) -> None:
        if raw is not None:
            require_writable_file(target)
            backup(path, raw)
        sibling = backup_path_for(path)
        try:
            with secure_open_regular_binary(sibling.resolve()) as stream:
                previous = stream.read()
        except FileNotFoundError:
            return
        require_writable_file(sibling)
        backup(sibling, previous)

    def committed() -> None:
        nonlocal changed
        changed = True

    try:
        migrate_yaml_doc(
            path,
            lambda doc: settings_edit(doc, on_conflict=conflicts.append),
            needs_edit=lambda doc: settings_edit(doc) is not None,
            before_write=preserve,
            committed=committed,
        )
    except OSError, ValueError, TypeError, RecursionError:
        # The normal settings load reports read errors and any surviving conflict.
        return False, []
    return changed, conflicts


def _remove_legacy_trust_keys(doc: dict[str, Any], old_keys: set[str]) -> None:
    if not old_keys:
        return
    legacy_section = "project"
    old_trust = doc.get(legacy_section)
    if isinstance(old_trust, dict):
        retained = {key: value for key, value in old_trust.items() if f"{legacy_section}.{key}" not in old_keys}
        if len(retained) != len(old_trust):
            if retained:
                doc[legacy_section] = retained
            else:
                del doc[legacy_section]
    for old in old_keys:
        doc.pop(old, None)


def _remove_dotted(node: dict[str, Any], parts: list[str]) -> None:
    head, *rest = parts
    if not rest:
        node.pop(head, None)
        return
    child = node.get(head)
    if not isinstance(child, dict):
        return
    _remove_dotted(child, rest)
    if not child:
        node.pop(head, None)


def _prune_dotted_spellings(node: dict[str, Any], parts: list[str], start: int = 0, *, pure: bool = True) -> None:
    """Remove every spelling of *parts* except the fully nested one.

    ``flatten_user_doc`` reads ``ui.theme: dark`` at the top level — or
    ``ui: {"editor.keymap": ...}`` halfway down — as the same setting as the
    nested shape. A stale spelling left behind by a patch could win over the
    patched value. Emptied parents are pruned like removal does.
    """
    for end in range(start + 1, len(parts) + 1):
        name = ".".join(parts[start:end])
        if name not in node:
            continue
        here_pure = pure and end - start == 1
        if end == len(parts):
            if not here_pure:
                del node[name]
            continue
        child = node[name]
        if isinstance(child, dict):
            _prune_dotted_spellings(child, parts, end, pure=here_pure)
            if not child:
                del node[name]
