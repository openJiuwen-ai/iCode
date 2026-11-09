# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Display identity and leaf labels shared by file-change surfaces."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace

from rich.text import Text

from chrys.app.tui.util.diff_entries import DiffFileEntry, entry_has_visible_change
from chrys.app.tui.util.mutation_sources import merge_mutation_source as _merge_source
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.service.mutations.types import MutationOp, MutationSource

_NO_BACKUP = msg("tui.diff.no_backup", fallback=" (no backup)")
_LARGE_FILE = msg("tui.diff.large_file", fallback=" (large file)")

_OP_SEGMENTS: dict[MutationOp, list[tuple[str, str]]] = {
    MutationOp.MODIFY: [("[", "bold orange1"), ("+", "bold green"), ("-", "bold red"), ("]", "bold orange1")],
    MutationOp.CREATE: [("[+]", "bold green")],
    MutationOp.DELETE: [("[-]", "bold red")],
    MutationOp.MOVE: [("[→]", "bold cyan")],
}


def entry_source_suffix(entry: DiffFileEntry) -> list[tuple[str, str]]:
    """Provenance badges rendered right-aligned at the end of a leaf row.

    ``?`` implicit detection, ``~`` window-diff inference (assumed
    provenance), ``!`` contested — a peer chrys session also wrote the
    path, so disk may differ from "our" after state and rollback will
    exclude it.  ``!`` is dim while the row's own attribution is itself
    inference (``?``/``~`` rows) and bold yellow on proven rows, where
    it flags a hard both-sessions-wrote conflict.
    """
    badges: list[tuple[str, str]] = []
    implicit = entry.source == MutationSource.IMPLICIT.value
    if implicit:
        badges.append(("?", "bold yellow"))
    elif entry.inferred:
        badges.append(("~", "dim"))
    if entry.contested:
        badges.append(("!", "dim" if (implicit or entry.inferred) else "bold yellow"))
    return badges


def entry_badge_text(entry: DiffFileEntry) -> Text:
    """Provenance markers separated by one cell on every file-change surface."""
    return Text(" ").join(Text(marker, style=style) for marker, style in entry_source_suffix(entry))


def is_within_cwd(rel_path: str) -> bool:
    """Check if a relative path stays within the CWD (doesn't escape with ..)."""
    return not rel_path.startswith("..") and not os.path.isabs(rel_path)


def entry_display_key(entry: DiffFileEntry) -> str:
    """Key matching where an entry renders in the change-list tree."""
    display_path = entry.rel_path if is_within_cwd(entry.rel_path) else entry.path
    return os.path.normpath(display_path)


def filter_and_dedupe_entries(entries: list[DiffFileEntry]) -> list[DiffFileEntry]:
    """Collapse duplicate tree rows and drop entries with no visible change."""
    latest_by_key: dict[str, DiffFileEntry] = {}
    key_order: list[str] = []
    for entry in entries:
        key = entry_display_key(entry)
        if key not in latest_by_key:
            key_order.append(key)
            latest_by_key[key] = entry
            continue
        # Builders should already aggregate by file. If an older session
        # or live source still hands us duplicates, the later entry reflects
        # the newest net state for that tree row.  Badges are sticky
        # across the collapse (any contested/inferred row marks the key).
        prev = latest_by_key[key]
        latest_by_key[key] = replace(
            entry,
            source=_merge_source(prev.source, entry.source),
            contested=prev.contested or entry.contested,
            inferred=prev.inferred or entry.inferred,
            last_changed_at=max(prev.last_changed_at, entry.last_changed_at),
        )
    return [latest_by_key[key] for key in key_order if entry_has_visible_change(latest_by_key[key])]


def entry_label_parts(
    entry: DiffFileEntry,
    render_message: Callable[[MessageRef], str] = format_message,
) -> tuple[Text, Text]:
    """The operation and availability note; only the path between them may shrink."""
    operation = Text.assemble(*_OP_SEGMENTS.get(entry.operation, [("[?]", "bold white")]), " ")
    note = Text(render_message(_NO_BACKUP.bind()), style="dim") if entry.content_omitted else Text()
    if entry.line_count_result == "large":
        note = Text(render_message(_LARGE_FILE.bind()), style="dim")
    return operation, note
