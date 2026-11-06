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

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

SCHEMA_VERSION: Final = 1

RESERVED_DOC_KEYS: Final = frozenset({"schema_version", "migrations"})
"""Top-level housekeeping keys: never settings, never "unknown"."""


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
    """
    values: dict[str, Any] = {}
    unknown: list[str] = []

    def walk(prefix: str, node: Mapping[str, Any]) -> None:
        for name, value in node.items():
            dotted = f"{prefix}.{name}" if prefix else str(name)
            if not prefix and dotted in RESERVED_DOC_KEYS:
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
    patch just moved elsewhere.
    """
    doc.setdefault("schema_version", SCHEMA_VERSION)
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
    return doc


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
    nested shape, and in a sorted document the literal flattens *after* the
    nested value, so a stale spelling left behind by a patch would win every
    load. Emptied parents are pruned like removal does.
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
