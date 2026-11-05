# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Diff viewer screen — full-screen file change viewer with per-turn tabs.

Layout:
  ┌──────────────────────────────────────────────────────────────┐
  │  #app-header                                                 │
  ├──────────────────────────────────────────────────────────────┤
  │  TabbedContent: T2 │ T5 │ T6 (active)                      │
  ├──────────────────────────────────────────────────────────────┤
  │  ┌──────────────┬───────────────────────────────────────────┐│
  │  │  TreeView     │           DiffView / Message              ││
  │  │  (directory   │           (3/4 width)                     ││
  │  │   structure)  │                                           ││
  │  │  ─ ─ ─ ─ ─ ─ │                                           ││
  │  │  External     │                                           ││
  │  │  (flat list)  │                                           ││
  │  └──────────────┴───────────────────────────────────────────┘│
  ├──────────────────────────────────────────────────────────────┤
  │  Footer: Esc Back │ Space Split View │ Ctrl+G Change List     │
  └──────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import PurePath
from typing import TYPE_CHECKING, ClassVar

from rich.style import Style
from rich.text import Text
from textual import events, on
from textual.containers import HorizontalGroup, VerticalGroup
from textual.content import Content
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Static, TabbedContent, TabPane, Tree

from chrys.app.tui.behaviors.right_click_copy import RightClickScreenCopyMixin
from chrys.app.tui.binding_display import localized_binding
from chrys.app.tui.i18n import LocaleController, render_str, widget_localizer
from chrys.app.tui.util.diff_content import DiffContent, read_diff_content
from chrys.app.tui.util.diff_entries import DiffFileEntry, DiffFocus, DiffLoadResult, entry_has_visible_change
from chrys.app.tui.util.diff_labels import (
    entry_badge_text,
    entry_display_key,
    entry_label_parts,
    entry_source_suffix,
    filter_and_dedupe_entries,
    is_within_cwd,
)
from chrys.app.tui.util.mutation_sources import merge_mutation_source as _merge_source
from chrys.app.tui.util.removal import remove_children_shielded, remove_shielded
from chrys.app.tui.widgets import ChrysLoadingIndicator
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.foundation.i18n import DisplayPath, MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.mutation_scope import MutationScope, WorkflowRunScope
from chrys.foundation.platform.paths import display_relpath
from chrys.service.mutations.types import MutationOp, MutationProvenance, SnapshotSkipReason

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.widgets._tree import TreeNode

    from chrys.app.tui.widgets.diff_view import DiffView
    from chrys.service.state.store import StateStore


# Key used in Rich ``Style.meta`` to mark the ``[x]``/``[ ]``/``[~]``
# span of a tree node label as "click here to toggle the checkbox".
# Rich merges ``meta`` dicts on style addition, so this key survives
# Textual's line-level stylize pass — see ``CheckableTree._on_click``.
_CHECK_TOGGLE_META_KEY = "chrys_check_toggle"
_CHECK_TOGGLE_META = {_CHECK_TOGGLE_META_KEY: True}


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BACK_BINDING = msg("tui.binding.back", fallback="Back")
_TOGGLE_SPLIT_VIEW_BINDING = msg("tui.binding.toggle_split_view", fallback="Toggle Split View")
_TOGGLE_CHANGE_LIST_BINDING = msg("tui.binding.toggle_change_list", fallback="Toggle Change List")
_DIFF_VIEWER_TITLE = msg("tui.diff.title", fallback="Diff Viewer")
_NO_FILE_CHANGES = msg("tui.diff.no_file_changes", fallback="No file changes to show")
_NO_EOL = msg("tui.diff.eol.none", fallback="No EOL")
_MIXED_EOL = msg("tui.diff.eol.mixed", fallback="Mixed")
_BADGE_MODIFIED_INTERNALLY_AND_EXTERNALLY = msg(
    "tui.diff.badge.modified_internally_and_externally",
    fallback="modified internally and externally",
)
_BADGE_IMPLICITLY_DETECTED = msg("tui.diff.badge.implicitly_detected", fallback="implicitly detected")
_BADGE_AUTHORSHIP_UNVERIFIED = msg("tui.diff.badge.authorship_unverified", fallback="authorship unverified")
_SKIP_FILE_TOO_LARGE = msg("tui.diff.skip_reason.file_too_large", fallback="file too large")
_SKIP_BINARY_FILE = msg("tui.diff.skip_reason.binary_file", fallback="binary file")
_SKIP_UNREADABLE = msg("tui.diff.skip_reason.unreadable", fallback="file unreadable")
_CHANGE_LIST = msg("tui.diff.change_list", fallback="Change List")
_SELECT_FILE = msg("tui.diff.select_file", fallback="Select a file to view changes")
_CONTENT_NOT_BACKED_UP = msg(
    "tui.diff.content_not_backed_up",
    fallback="Content not backed up ({reason}): {path}. No diff to display.",
)
_BINARY_FILE = msg("tui.diff.binary_file", fallback="Binary file: {path}")
_FILE_MOVED = msg("tui.diff.file_moved", fallback="File moved: {old_path} → {new_path}")
_ONLY_LINE_ENDINGS_CHANGED = msg(
    "tui.diff.only_line_endings_changed",
    fallback="Only line endings changed. No line content changes to display.",
)
_ONLY_BYTE_REPRESENTATION_CHANGED = msg(
    "tui.diff.only_byte_representation_changed",
    fallback="Only file encoding or byte-level representation changed. No line content changes to display.",
)
_DIFF_VIEWER_SESSION_TITLE = msg(
    "tui.diff.session_title",
    fallback="Diff Viewer - Session: {session_id}",
)
_LOAD_ERROR = msg(
    "tui.diff.load_error",
    fallback="Error: unable to load diff viewer — {error}",
)
_EXTERNAL_TREE = msg("tui.diff.external_tree", fallback="External")
_TURN_TAB = msg("tui.diff.tab.turn", fallback="T{turn_id}")
_RUN_TAB = msg("tui.diff.tab.run", fallback="Run {number}")
_ALL_TAB = msg("tui.diff.tab.all", fallback="All")

# Checkbox prefixes used when DiffPeriodPane runs in checkable mode
# (currently only from the rollback modal).  Tri-state on folders:
# [x] all descendants checked, [ ] none, [~] mixed.  Each entry is a
# ``(glyph, base_style)`` pair; the label builder merges in the
# click-toggle meta so the span is identifiable as a click target.
_CHECK_SEGMENTS: dict[str, tuple[str, str]] = {
    "on": ("[x]", "bold green"),
    "off": ("[ ]", "bold white"),
    "mixed": ("[~]", "bold yellow"),
}


def _entry_is_eol_only_change(entry: DiffFileEntry) -> bool:
    """Return whether a MODIFY changed only line endings / final newline."""
    return (
        not entry.is_binary
        and entry.operation is MutationOp.MODIFY
        and entry.before_text != entry.after_text
        and entry.before_text.splitlines() == entry.after_text.splitlines()
    )


def _entry_is_metadata_only_change(entry: DiffFileEntry) -> bool:
    """Return whether bytes changed but decoded line content did not."""
    return (
        not entry.is_binary
        and entry.operation is MutationOp.MODIFY
        and entry.bytes_changed
        and entry.before_text == entry.after_text
    )


def _text_eol_label(
    text: str,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    """Return a compact label for the line endings present in ``text``."""
    crlf = text.count("\r\n")
    cr = text.count("\r") - crlf
    lf = text.count("\n") - crlf
    kinds = int(crlf > 0) + int(cr > 0) + int(lf > 0)
    if kinds == 0:
        return render_message(_NO_EOL.bind())
    if kinds > 1:
        return render_message(_MIXED_EOL.bind())
    if crlf:
        return "CRLF"
    if cr:
        return "CR"
    return "LF"


def _entry_eol_text(entry: DiffFileEntry) -> str:
    """Return the entry text that represents the displayed file state."""
    if entry.operation is MutationOp.DELETE:
        return entry.before_text
    return entry.after_text


# One-line legend per provenance badge, shown in the diff-content
# subtitle so the markers are self-explanatory in place.
_BADGE_LEGENDS: dict[str, MessageDef] = {
    "!": _BADGE_MODIFIED_INTERNALLY_AND_EXTERNALLY,
    "?": _BADGE_IMPLICITLY_DETECTED,
    "~": _BADGE_AUTHORSHIP_UNVERIFIED,
}
_BADGE_LEGEND_ORDER = "!?~"


def _entry_content_subtitle(
    entry: DiffFileEntry,
    render_message: Callable[[MessageRef], str] = format_message,
) -> Text:
    """Build the diff-content subtitle: badge legend + EOL and encoding.

    Format: ``!: legend - ?: legend - ~: legend - LF - UTF-8``, with
    only the badges the entry actually carries.
    """
    encoding = entry.encoding.upper()
    tail = encoding if entry.is_binary else f"{_text_eol_label(_entry_eol_text(entry), render_message)} - {encoding}"
    parts: list[Text] = []
    for marker, style in sorted(entry_source_suffix(entry), key=lambda ms: _BADGE_LEGEND_ORDER.index(ms[0])):
        legend = Text()
        legend.append(marker, style)
        legend.append(f": {render_message(_BADGE_LEGENDS[marker].bind())}")
        parts.append(legend)
    parts.append(Text(tail))
    return Text(" - ").join(parts)


# Human-readable labels for ``DiffFileEntry.content_omitted`` values
# (``SnapshotSkipReason`` — why SnapshotPolicy withheld the backup).
_SKIP_REASON_LABELS: dict[str, MessageDef] = {
    SnapshotSkipReason.TOO_LARGE.value: _SKIP_FILE_TOO_LARGE,
    SnapshotSkipReason.BINARY.value: _SKIP_BINARY_FILE,
    SnapshotSkipReason.UNREADABLE.value: _SKIP_UNREADABLE,
}


def _checkbox_text(state: str) -> Text:
    """Build the Rich ``Text`` for a checkbox glyph carrying toggle meta.

    The meta key is inspected by :class:`CheckableTree._on_click` to
    detect mouse clicks that land on the ``[x]`` span so they can be
    routed to a toggle action instead of the default node-select.
    """
    glyph, style_str = _CHECK_SEGMENTS[state]
    return Text(glyph, style=Style.parse(style_str) + Style(meta=_CHECK_TOGGLE_META))


@dataclass
class _FolderNodeData:
    """Sentinel attached to directory tree nodes so we can distinguish
    them from leaf (file) nodes without resorting to label parsing."""

    name: str


class CheckableTree(Tree):
    """``Tree`` subclass that posts a :class:`CheckToggled` message when
    the user clicks on a checkbox span in a node label.

    The checkbox span is identified by the
    :data:`_CHECK_TOGGLE_META_KEY` entry in the Rich ``Style.meta`` of
    the clicked character.  When detected we:

    1. Move ``cursor_line`` to the clicked row (for visual feedback —
       the cursor "follows" the checkbox the user toggled).
    2. Post the message so the owning widget can mutate its own
       selection state.
    3. Stop propagation so the base Tree does **not** also run
       ``select_cursor`` — we don't want a checkbox click to also
       switch which diff is shown in the preview pane.

    Clicks that land outside the checkbox span fall through to the
    default Tree click handler (select + show diff / expand / collapse).
    """

    class CheckToggled(Message):
        """Posted when the user clicks a checkbox span."""

        def __init__(self, node: TreeNode) -> None:  # type: ignore[type-arg]
            super().__init__()
            self.node = node

    async def _on_click(self, event: events.Click) -> None:
        # ``prevent_default`` is critical here: Textual dispatches
        # ``_on_click`` for *every* class in the MRO, so without it the
        # base ``Tree._on_click`` would run in addition to ours.  That
        # caused two bugs:
        #   1. Clicking ``[x]`` also fired ``select_cursor`` →
        #      ``NodeSelected`` → ``_expand_node_on_select`` (auto_expand
        #      is True by default), collapsing/expanding the folder.
        #   2. Clicking the ▼/▶ guide toggled the node twice — once
        #      via our explicit ``super()._on_click`` call below and
        #      once via the MRO dispatch — so the visible state never
        #      changed.
        # ``event.stop()`` only halts bubbling to parents; it does not
        # stop sibling MRO handlers from being invoked on this widget.
        event.prevent_default()
        meta = event.style.meta
        line = meta.get("line")
        if meta.get(_CHECK_TOGGLE_META_KEY) and line is not None:
            node = self.get_node_at_line(line)
            if node is not None:
                # Move the cursor so the user sees which row they just
                # toggled; skip scrolling so it's a pure visual hint.
                self.cursor_line = line
                self.post_message(self.CheckToggled(node))
                event.stop()
                return
        # Not a checkbox click — let the base class do its thing
        # (select + fire NodeSelected, or expand/collapse the guide).
        await super()._on_click(event)


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------


def _build_period_entries(tracker, turn, cwd: str, *, include_content: bool = True) -> list[DiffFileEntry]:
    """Build DiffFileEntry list for a single turn from a deserialized tracker."""
    last_op: dict[str, MutationOp] = {}
    old_paths: dict[str, str | None] = {}
    sources: dict[str, str] = {}
    changed_at: dict[str, float] = {}
    for m in turn.mutations:
        # Foreign rows are excluded from the summary fold below; keep
        # this metadata consistent or a peer's op/old_path/source leaks
        # onto a row whose hashes came from our rows only.
        if m.provenance is MutationProvenance.FOREIGN:
            continue
        if (change_time := m.changed_at) is not None:
            changed_at[m.path] = max(changed_at.get(m.path, 0.0), change_time)
            if m.old_path:
                changed_at[m.old_path] = max(changed_at.get(m.old_path, 0.0), change_time)
        last_op[m.path] = m.operation
        old_paths[m.path] = m.old_path
        sources[m.path] = _merge_source(sources.get(m.path, ""), m.source.value)
        if m.old_path:
            sources[m.old_path] = _merge_source(sources.get(m.old_path, ""), m.source.value)

    file_summary = {
        path: diff
        for path, diff in tracker.get_period_file_summary(turn.scope).items()
        # A skip-marked diff is not net-zero even with (None, None)
        # hashes — the content exists but was never backed up.
        if not diff.is_net_zero or diff.content_unavailable or last_op.get(path) is MutationOp.MOVE
    }
    if not file_summary:
        return []

    # A MOVE card already represents both paths. Keep the complete two-path
    # summary for rollback/analytics, but avoid a duplicate DELETE card unless
    # the source was subsequently recreated or the destination no longer moves.
    moved_sources = {
        old_path
        for path, old_path in old_paths.items()
        if old_path is not None and path in file_summary and last_op.get(path) is MutationOp.MOVE
    }
    entries: list[DiffFileEntry] = []
    store = tracker.store
    for file_path, diff in file_summary.items():
        if file_path in moved_sources and diff.after is None and diff.after_skip is None:
            continue
        # Content withheld by SnapshotPolicy (too large / binary): no
        # blobs to render, but the mutation still happened — keep the
        # row visible with a ``content_omitted`` marker (matching the
        # live /diff behavior) instead of silently dropping the event.
        # MOVE keeps its plain rename row — that info is content-free.
        content_omitted = ""
        if diff.content_unavailable and last_op.get(file_path) is not MutationOp.MOVE:
            skip = diff.after_skip or diff.before_skip
            content_omitted = skip.value if skip else ""

        content = read_diff_content(store, diff.before, diff.after) if include_content else DiffContent(available=False)

        op = last_op.get(file_path, MutationOp.MODIFY)
        rel = display_relpath(file_path, cwd)

        entry = DiffFileEntry(
            path=file_path,
            rel_path=rel,
            operation=op,
            old_path=old_paths.get(file_path),
            before_text=content.before_text,
            after_text=content.after_text,
            is_binary=content.is_binary,
            encoding=content.encoding,
            bytes_changed=not diff.is_net_zero or bool(content_omitted),
            before_hash=diff.before,
            after_hash=diff.after,
            source=sources.get(file_path, ""),
            content_omitted=content_omitted,
            contested=diff.contested,
            inferred=diff.inferred,
            last_changed_at=changed_at.get(file_path, 0.0),
        )
        if entry_has_visible_change(entry):
            entries.append(entry)

    entries.sort(key=lambda e: e.rel_path)
    return entries


def _build_session_entries(tracker, cwd: str, *, include_content: bool = True) -> list[DiffFileEntry]:
    """Build DiffFileEntry list for the net session-wide changeset.

    Aggregates mutations across all turns: each file's ``before_text`` is
    its pre-session state (from the earliest turn that touched it) and
    ``after_text`` is its latest state after the last mutation.
    Net-zero churn is already filtered by ``get_session_file_summary``.
    """
    file_summary = tracker.get_session_file_summary()
    if not file_summary:
        return []

    # Track last-seen old_path across the session (for MOVE display).
    # Iterate in turn_id order so "last-seen" is chronological — turns
    # can be appended with non-monotonic ids via session restore/retry.
    old_paths: dict[str, str | None] = {}
    sources: dict[str, str] = {}
    changed_at: dict[str, float] = {}
    for turn in sorted(tracker.get_all_periods(), key=lambda period: period.period_index):
        for m in turn.mutations:
            # Match the summary fold: foreign rows must not contribute
            # a MOVE's old_path or their source marker to our entries.
            if m.provenance is MutationProvenance.FOREIGN:
                continue
            if (change_time := m.changed_at) is not None:
                changed_at[m.path] = max(changed_at.get(m.path, 0.0), change_time)
                if m.old_path:
                    changed_at[m.old_path] = max(changed_at.get(m.old_path, 0.0), change_time)
            sources[m.path] = _merge_source(sources.get(m.path, ""), m.source.value)
            if m.old_path:
                old_paths[m.path] = m.old_path
                sources[m.old_path] = _merge_source(sources.get(m.old_path, ""), m.source.value)

    entries: list[DiffFileEntry] = []
    store = tracker.store
    for file_path, diff in file_summary.items():
        # Content withheld by SnapshotPolicy on either side: keep the
        # row (it IS a net change — fully-skipped files are already
        # netted out by the tracker) but mark it so the pane renders a
        # message instead of an untruthful empty diff.
        content_omitted = ""
        if diff.content_unavailable:
            skip = diff.after_skip or diff.before_skip
            content_omitted = skip.value if skip else ""

        content = read_diff_content(store, diff.before, diff.after) if include_content else DiffContent(available=False)

        # Derive net op from skip-aware existence (a side with a
        # withheld backup still exists on disk).  Net-zero
        # (before == after) is already filtered by the tracker, so if
        # both hashes are present here, they necessarily differ → MODIFY.
        # This is more accurate than the last-recorded op, which for a
        # pre-existing-file DELETE→CREATE sequence would be CREATE but
        # the true net effect is MODIFY.
        if not diff.before_exists and diff.after_exists:
            op = MutationOp.CREATE
        elif diff.before_exists and not diff.after_exists:
            op = MutationOp.DELETE
        else:
            op = MutationOp.MODIFY

        rel = display_relpath(file_path, cwd)

        entry = DiffFileEntry(
            path=file_path,
            rel_path=rel,
            operation=op,
            old_path=old_paths.get(file_path),
            before_text=content.before_text,
            after_text=content.after_text,
            is_binary=content.is_binary,
            encoding=content.encoding,
            bytes_changed=True,
            before_hash=diff.before,
            after_hash=diff.after,
            source=sources.get(file_path, ""),
            content_omitted=content_omitted,
            contested=diff.contested,
            inferred=diff.inferred,
            last_changed_at=changed_at.get(file_path, 0.0),
        )
        if entry_has_visible_change(entry):
            entries.append(entry)

    entries.sort(key=lambda e: e.rel_path)
    return entries


def load_diff_entries_by_period(
    state_store: StateStore,
    session_id: str,
    cwd: str,
    *,
    mutations_data: dict | None = None,
    include_content: bool = True,
) -> DiffLoadResult:
    """Build execution-scoped and session-wide diffs from persisted state."""
    import json

    from chrys.service.mutations.store import SnapshotStore
    from chrys.service.mutations.tracker import MutationTracker
    from chrys.service.state.store import SESSION_FILE_NAME

    try:
        path = state_store.session_dir(session_id) / SESSION_FILE_NAME
        if mutations_data is None:
            if not path.exists():
                return DiffLoadResult.empty()
            raw = json.loads(path.read_text(encoding="utf-8"))
            state = raw.get("state", {})
            mutations_data = state.get("chrys_mutations")
        if not mutations_data:
            return DiffLoadResult.empty()

        # ``path`` came from ``state_store._session_file(session_id)``, so
        # ``path.parent`` is already the canonical session folder.  No
        # need to rederive it — which would re-sanitize the id and break
        # for canonical (dashed) UUIDs whose folder name is the 12-char
        # dashless projection, not the raw id.
        tracker = MutationTracker.deserialize(mutations_data, SnapshotStore(path.parent))
    except Exception:
        return DiffLoadResult.empty()

    periods = sorted(tracker.get_all_periods(), key=lambda period: period.period_index)
    entries_by_period: dict[int, list[DiffFileEntry]] = {}
    scopes: dict[int, MutationScope] = {}
    for number, period in enumerate(periods, start=1):
        scopes[number] = period.scope
        entries = (
            _build_period_entries(tracker, period, cwd, include_content=include_content) if period.mutations else []
        )
        if entries:
            entries_by_period[number] = entries

    return DiffLoadResult(
        all_entries=_build_session_entries(tracker, cwd, include_content=include_content),
        per_period_entries=entries_by_period,
        total_periods=len(periods),
        scopes=scopes,
    )


# ---------------------------------------------------------------------------
# DiffPeriodPane — per-turn content widget
# ---------------------------------------------------------------------------


def _entry_all_tab_key(entry: DiffFileEntry) -> tuple[str, str, MutationOp, str | None, str, str, str]:
    """Projected entry identity for deciding whether the All tab is redundant."""
    return (
        entry.path,
        entry.rel_path,
        entry.operation,
        entry.old_path,
        entry.before_text,
        entry.after_text,
        entry.source,
    )


def _should_show_all_tab(all_entries: list[DiffFileEntry], periods_data: dict[int, list[DiffFileEntry]]) -> bool:
    """Return whether the session-wide All tab adds distinct information."""
    if not all_entries:
        return False
    if len(periods_data) != 1:
        return True
    all_keys = [_entry_all_tab_key(entry) for entry in filter_and_dedupe_entries(all_entries)]
    only_turn_keys = [
        _entry_all_tab_key(entry) for entry in filter_and_dedupe_entries(next(iter(periods_data.values())))
    ]
    return all_keys != only_turn_keys


class DiffPeriodPane(Widget):
    """Per-turn changelist + diff viewer.

    Contains a directory-structured tree for workspace files, an optional
    flat list for external files, and a DiffView panel on the right.
    """

    class SelectionChanged(Message):
        """Posted whenever the set of checked leaves changes.

        Only fires in checkable mode — the ``/diff`` screen never sees
        this.  Consumers (currently the rollback modal) use it to keep
        a "Revert N files" affordance in sync with the tree.  Both
        totals are included so the consumer doesn't have to track
        ``len(entries)`` separately.
        """

        def __init__(self, selected_count: int, total_count: int) -> None:
            super().__init__()
            self.selected_count = selected_count
            self.total_count = total_count

    def __init__(
        self,
        entries: list[DiffFileEntry],
        *,
        checkable: bool = False,
        lazy: bool = False,
        initial_display_key: str | None = None,
    ) -> None:
        super().__init__()
        self._entries = filter_and_dedupe_entries(entries)
        self._diff_cache: dict[str, DiffView] = {}  # path → DiffView widget
        self._active_path: str | None = None
        self._view_overrides: dict[str, bool] = {}  # path → user-set split mode
        self._cwd_entries = [e for e in self._entries if is_within_cwd(e.rel_path)]
        self._ext_entries = [e for e in self._entries if not is_within_cwd(e.rel_path)]
        # Checkable mode: leaves render with a [x]/[ ] prefix and folders
        # with [x]/[~]/[ ].  ``_unchecked`` stores leaf paths the user has
        # deselected; default empty = all checked.  Toggled in place via
        # ``toggle_check_node`` and re-rendered via ``_relabel_all``.
        self._checkable = checkable
        self._unchecked: set[str] = set()
        # path → spaces between the label body and its badge column
        # (computed per tree in ``_assign_badge_pads``).
        self._badge_pad: dict[str, int] = {}
        self._lazy = lazy
        self._initialized = False
        self._initializing = False
        self._initial_display_key = initial_display_key

    def _render_message(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)

    def compose(self) -> ComposeResult:
        with HorizontalGroup(classes="diff-body"):
            changelist = VerticalGroup(classes="diff-changelist")
            changelist.border_title = self._render_message(_CHANGE_LIST.bind())
            with changelist:
                # ``CheckableTree`` is a drop-in Tree that routes clicks
                # on checkbox spans to a ``CheckToggled`` message.  Its
                # custom path is dormant unless labels carry the toggle
                # meta, so using it in non-checkable mode too is safe.
                workspace_tree: CheckableTree = CheckableTree("Workspace", classes="workspace-tree")
                workspace_tree.show_root = False
                workspace_tree.show_guides = True
                workspace_tree.guide_depth = 2
                yield workspace_tree
                if self._ext_entries:
                    ext_tree: CheckableTree = CheckableTree(
                        self._render_message(_EXTERNAL_TREE.bind()), classes="external-tree"
                    )
                    ext_tree.show_root = True
                    ext_tree.show_guides = False
                    yield ext_tree
            diff_content = VerticalGroup(classes="diff-content")
            diff_content.border_title = ""
            with diff_content:
                yield Static(self._render_message(_SELECT_FILE.bind()), classes="diff-message")

    async def on_mount(self) -> None:
        if self._lazy:
            return
        await self.ensure_initialized()

    async def ensure_initialized(self) -> None:
        """Build tree contents and mount the first file diff on demand."""
        if self._initialized or self._initializing:
            return
        self._initializing = True
        try:
            await self._initialize()
            self._initialized = True
        finally:
            self._initializing = False
        # Tree regions can still be 0 while building (lazy tabs mount
        # before layout); re-anchor once the layout has settled.
        self.call_after_refresh(self._refresh_badge_alignment)

    async def _initialize(self) -> None:
        self._build_workspace_tree()
        if self._ext_entries:
            self._build_external_tree()
        if self._initial_display_key is not None:
            for tree in self._iter_trees():
                node = self._find_display_key(tree.root, self._initial_display_key)
                if node is not None and isinstance(node.data, DiffFileEntry):
                    # New leaves acquire line numbers lazily; scrolling needs the built line map.
                    _ = tree.last_line
                    tree.move_cursor(node)
                    tree.scroll_to_node(node, animate=False)
                    await self._show_file(node.data)
                    self.call_after_refresh(tree.scroll_to_node, node, animate=False)
                    return
        # Auto-select first file leaf
        ws_tree = self.query_one(".workspace-tree", Tree)
        first_leaf = self._find_first_leaf(ws_tree.root)
        # Only auto-show if we actually landed on a file leaf — folder
        # nodes now also carry data (``_FolderNodeData``) so a plain
        # ``is not None`` check would incorrectly pass a folder into
        # ``_show_file``.
        if first_leaf and isinstance(first_leaf.data, DiffFileEntry):
            # Newly added TreeNodes get render line numbers lazily; build them
            # before moving the cursor so the visible selection lands on the file.
            _ = ws_tree.last_line
            ws_tree.move_cursor(first_leaf)
            await self._show_file(first_leaf.data)
        elif self._ext_entries:
            ext_tree = self.query_one(".external-tree", Tree)
            if ext_tree.root.children:
                first = ext_tree.root.children[0]
                # See workspace-tree selection above.
                _ = ext_tree.last_line
                ext_tree.move_cursor(first)
                if isinstance(first.data, DiffFileEntry):
                    await self._show_file(first.data)

    @staticmethod
    def _find_display_key(node: TreeNode, key: str) -> TreeNode | None:  # type: ignore[type-arg]
        if isinstance(node.data, DiffFileEntry) and entry_display_key(node.data) == key:
            return node
        for child in node.children:
            found = DiffPeriodPane._find_display_key(child, key)
            if found is not None:
                return found
        return None

    @staticmethod
    def _find_first_leaf(node: TreeNode) -> TreeNode | None:  # type: ignore[type-arg]
        """DFS to find the first leaf node carrying a ``DiffFileEntry``.

        Checks the data type rather than ``is not None`` — folder nodes
        also carry data (``_FolderNodeData``) in checkable mode, so we
        have to discriminate or we'd pick a folder as the "first leaf".
        """
        if isinstance(node.data, DiffFileEntry):
            return node
        for child in node.children:
            result = DiffPeriodPane._find_first_leaf(child)
            if result is not None:
                return result
        return None

    def _build_workspace_tree(self) -> None:
        """Build directory-structured tree for CWD files."""
        tree = self.query_one(".workspace-tree", Tree)
        tree.clear()
        dir_nodes: dict[str, TreeNode] = {}  # type: ignore[type-arg]

        ordered = sorted(self._cwd_entries, key=lambda e: e.rel_path)
        self._assign_badge_pads([(e, (len(PurePath(e.rel_path).parts) - 1) * tree.guide_depth) for e in ordered], tree)
        for entry in ordered:
            # ``PurePath`` is platform-aware: parses ``os.sep`` correctly on
            # both Windows and POSIX, no manual separator munging needed.
            parts = PurePath(entry.rel_path).parts
            if not parts:
                continue

            # Create/find parent directory nodes
            current = tree.root
            for i, part in enumerate(parts[:-1]):
                key = "/".join(parts[: i + 1])
                if key not in dir_nodes:
                    node = current.add(Text(part), expand=True, data=_FolderNodeData(name=part))
                    dir_nodes[key] = node
                current = dir_nodes[key]

            # Add file leaf (filename is derived from entry.rel_path inside _leaf_label).
            current.add_leaf(self._leaf_label(entry), data=entry)

        if self._checkable:
            self._relabel_all()

    def _build_external_tree(self) -> None:
        """Build flat list for files outside CWD."""
        tree = self.query_one(".external-tree", Tree)
        tree.clear()  # idempotent: a retried _initialize() must not stack leaves
        ordered = sorted(self._ext_entries, key=lambda e: e.path)
        # External tree has no intermediate folder nodes; labels use
        # absolute paths (via _leaf_display).  Build via the shared leaf
        # helper so the checkbox prefix (when enabled) is applied
        # uniformly.  All leaves sit at the same depth, so the badge
        # column only needs a uniform indent term.
        self._assign_badge_pads([(e, tree.guide_depth) for e in ordered], tree)
        for entry in ordered:
            tree.root.add_leaf(self._leaf_label(entry), data=entry)
        tree.root.expand()

        if self._checkable:
            self._relabel_all()

    @staticmethod
    def _leaf_display(entry: DiffFileEntry) -> str:
        """Display name for a leaf: basename in the workspace tree, full
        absolute path in the external tree (which has no folder nodes)."""
        if is_within_cwd(entry.rel_path):
            return PurePath(entry.rel_path).name
        return entry.path

    def _leaf_body(self, entry: DiffFileEntry) -> Text:
        """Everything left of the badge column — checkbox, op, name, notes.

        When the pane runs in checkable mode the label is prefixed with
        a clickable ``[x]``/``[ ]`` span (the checkbox glyph carries
        ``_CHECK_TOGGLE_META`` so :class:`CheckableTree` can intercept
        mouse clicks).  The op badge ([+]/[-]/[→]/[+-]) follows the
        checkbox so the checkbox column stays leftmost.  The name is
        never truncated — rows wider than the pane scroll horizontally.
        """
        operation, note = entry_label_parts(entry, self._render_message)
        parts: list[Text | str] = []
        if self._checkable:
            parts += [_checkbox_text("off" if entry.path in self._unchecked else "on"), " "]
        return Text.assemble(*parts, operation, self._leaf_display(entry), note)

    def _leaf_label(self, entry: DiffFileEntry) -> Text:
        """Leaf body plus provenance badges padded into a shared right column.

        ``_assign_badge_pads`` picks the column (one cell past the
        widest body in the same tree, guide indentation included) so
        ``?``/``~``/``!`` line up vertically regardless of name length.
        """
        label = self._leaf_body(entry)
        badges = entry_badge_text(entry)
        if badges:
            label.append(" " * self._badge_pad.get(entry.path, 1))
            label.append_text(badges)
        return label

    def _assign_badge_pads(self, rows: list[tuple[DiffFileEntry, int]], tree: Tree) -> None:
        """Compute per-leaf padding that right-aligns badges to ``tree``'s edge.

        ``rows`` pair each entry with its guide indentation (in cells)
        so alignment holds across tree depths, not just label-local.
        Badge blocks END flush at the tree's visible right edge; a row
        whose body is already wider keeps one trailing space instead
        (names are never truncated — horizontal scroll reveals its
        badge).  Before layout (width 0) fall back to one cell past the
        widest badge-carrying body so alignment still holds headless.
        """
        measured: list[tuple[DiffFileEntry, int, int]] = []
        for entry, indent in rows:
            badges = entry_badge_text(entry)
            if not badges:
                continue
            measured.append((entry, indent + self._leaf_body(entry).cell_len, badges.cell_len))
        if not measured:
            return
        visible = tree.scrollable_content_region.width
        end = visible if visible > 0 else max(w + 1 + bw for _, w, bw in measured)
        for entry, width, badge_width in measured:
            self._badge_pad[entry.path] = max(1, end - badge_width - width)

    def _refresh_badge_alignment(self) -> None:
        """Re-anchor the badge column after the pane's width changes."""
        if not self._initialized:
            return
        # After-refresh callbacks can land after this pane left the DOM
        # (the rollback modal swaps panes on turn change while an
        # init/resize callback is still pending) — the trees are
        # unmounted with it, so there is nothing to re-anchor.
        try:
            ws_tree = self.query_one(".workspace-tree", Tree)
        except NoMatches:
            return
        self._assign_badge_pads(
            [(e, (len(PurePath(e.rel_path).parts) - 1) * ws_tree.guide_depth) for e in self._cwd_entries],
            ws_tree,
        )
        if self._ext_entries:
            ext_tree = self.query_one(".external-tree", Tree)
            self._assign_badge_pads([(e, ext_tree.guide_depth) for e in self._ext_entries], ext_tree)
        self._relabel_all()

    def on_resize(self) -> None:
        # After-refresh so tree regions reflect the settled layout.
        self.call_after_refresh(self._refresh_badge_alignment)

    def _folder_label(self, name: str, state: str) -> Text:
        """Build the Rich ``Text`` label for a directory node.

        ``state`` is one of ``"on"`` / ``"off"`` / ``"mixed"``.  The
        checkbox glyph carries the same toggle meta as leaf checkboxes,
        so clicking a folder's ``[~]`` cascades to all descendants (see
        :meth:`toggle_check_node`).
        """
        if self._checkable:
            return Text.assemble(_checkbox_text(state), " ", name)
        return Text(name)

    # ------------------------------------------------------------------
    # Checkbox API (no-ops when ``checkable=False``)
    # ------------------------------------------------------------------

    def _iter_trees(self) -> list[Tree]:
        """Return every Tree widget inside this pane (workspace + ext).

        Queries on ``Tree`` (the base class) rather than ``CheckableTree``
        so behaviour is unaffected if a subclass later swaps in a plain
        Tree for some reason.
        """
        return list(self.query(Tree))

    @on(CheckableTree.CheckToggled)
    def _on_check_toggled(self, event: CheckableTree.CheckToggled) -> None:
        """Mouse-click-to-toggle path — mirrors the ``x`` key action.

        The tree subclass identifies which node the user clicked via
        its checkbox meta; we just dispatch to the same cascade logic
        the keyboard binding uses.  Event is stopped so the message
        doesn't bubble out past this pane.
        """
        event.stop()
        self.toggle_check_node(event.node)

    def _relabel_all(self) -> None:
        """Recompute every leaf + folder label from ``self._unchecked``.

        Folder tri-state is derived from descendant leaves: all checked
        → ``[x]``, all unchecked → ``[ ]``, anything in between → ``[~]``.
        """
        for tree in self._iter_trees():
            self._relabel_subtree(tree.root)

    def _relabel_subtree(self, node: TreeNode) -> tuple[int, int]:  # type: ignore[type-arg]
        """Relabel ``node`` and its descendants.

        Returns ``(total_leaves, unchecked_leaves)`` so parent folders
        can compute their tri-state in one walk.
        """
        data = node.data
        # Leaf node: a DiffFileEntry directly.
        if isinstance(data, DiffFileEntry):
            is_unchecked = data.path in self._unchecked
            node.set_label(self._leaf_label(data))
            return (1, 1 if is_unchecked else 0)

        total = 0
        unchecked = 0
        for child in node.children:
            ct, cu = self._relabel_subtree(child)
            total += ct
            unchecked += cu

        # Update the folder label if this node represents a directory.
        # The tree root has data=None (default) — skip it so the hidden
        # root never gets a checkbox prefix.
        if isinstance(data, _FolderNodeData) and total > 0:
            if unchecked == 0:
                state = "on"
            elif unchecked == total:
                state = "off"
            else:
                state = "mixed"
            node.set_label(self._folder_label(data.name, state))

        return (total, unchecked)

    def toggle_check_node(self, node: TreeNode) -> None:  # type: ignore[type-arg]
        """Toggle the check state for ``node``.

        Leaves flip their own state.  Folders follow the common
        tri-state convention: if every descendant is already checked,
        uncheck them all; otherwise check them all (completing from
        ``[ ]`` or ``[~]``).  No-op when the pane isn't checkable.

        Emits :class:`SelectionChanged` on every successful toggle so
        outside widgets (e.g. the rollback modal's "Revert N files"
        checkbox) can stay in sync.
        """
        if not self._checkable:
            return

        data = node.data
        if isinstance(data, DiffFileEntry):
            if data.path in self._unchecked:
                self._unchecked.discard(data.path)
            else:
                self._unchecked.add(data.path)
            self._relabel_all()
            self._post_selection_changed()
            return

        # Folder / root: gather descendant leaves.
        leaves = self._collect_descendant_leaves(node)
        if not leaves:
            return
        all_checked = all(p not in self._unchecked for p in leaves)
        if all_checked:
            self._unchecked.update(leaves)
        else:
            for p in leaves:
                self._unchecked.discard(p)
        self._relabel_all()
        self._post_selection_changed()

    def _post_selection_changed(self) -> None:
        """Fire :class:`SelectionChanged` with the current totals."""
        self.post_message(
            self.SelectionChanged(
                selected_count=len(self.selected_paths()),
                total_count=len(self._entries),
            )
        )

    def set_all_checked(self, checked: bool) -> None:
        """Bulk check or uncheck every leaf in the pane.

        Used by the rollback modal's footer checkbox to mirror the
        "select all / select none" intent onto the tree.  Emits a
        :class:`SelectionChanged` message once at the end so consumers
        don't see intermediate per-leaf transitions.  No-op when the
        pane isn't checkable or the request matches current state.
        """
        if not self._checkable:
            return
        if checked:
            if not self._unchecked:
                return  # already all-checked
            self._unchecked.clear()
        else:
            all_paths = {e.path for e in self._entries}
            if self._unchecked == all_paths:
                return  # already all-unchecked
            self._unchecked = all_paths
        self._relabel_all()
        self._post_selection_changed()

    def selection_counts(self) -> tuple[int, int]:
        """Return ``(selected_count, total_count)`` for checkable consumers."""
        return (len(self.selected_paths()), len(self._entries))

    def _collect_descendant_leaves(self, node: TreeNode) -> list[str]:  # type: ignore[type-arg]
        """Return every file path under ``node``."""
        result: list[str] = []

        def walk(n: TreeNode) -> None:  # type: ignore[type-arg]
            if isinstance(n.data, DiffFileEntry):
                result.append(n.data.path)
                return
            for child in n.children:
                walk(child)

        walk(node)
        return result

    def selected_paths(self) -> list[str]:
        """Paths the user wants to include (i.e. NOT in ``_unchecked``).

        Returns all entry paths when the pane isn't checkable — the
        rollback modal is currently the only caller, but leaving this
        always-callable keeps the API simple for future reuse.
        """
        return [e.path for e in self._entries if e.path not in self._unchecked]

    def set_change_list_visible(self, visible: bool) -> None:
        """Show or hide the left-side file change tree."""
        self.query_one(".diff-changelist", VerticalGroup).display = visible

    async def on_tree_node_selected(self, event: Tree.NodeSelected[object]) -> None:
        # Folder nodes carry ``_FolderNodeData`` — show_file only makes
        # sense for real file leaves.
        if isinstance(event.node.data, DiffFileEntry):
            await self._show_file(event.node.data)

    async def _show_file(self, entry: DiffFileEntry) -> None:
        """Display the diff for the selected file."""
        if entry.path == self._active_path:
            return

        message = self.query_one(".diff-message", Static)
        content_box = self.query_one(".diff-content", VerticalGroup)

        # Handle non-diffable cases
        if entry.content_omitted:
            # Content backup withheld by SnapshotPolicy — the row is
            # still actionable (e.g. revert deletes a skipped create)
            # but there is nothing truthful to render as a diff.
            self._hide_all_diffs()
            reason_definition = _SKIP_REASON_LABELS.get(entry.content_omitted)
            reason = (
                self._render_message(reason_definition.bind())
                if reason_definition is not None
                else entry.content_omitted
            )
            message.update(
                Text(self._render_message(_CONTENT_NOT_BACKED_UP.bind(reason=reason, path=DisplayPath(entry.rel_path))))
            )
            message.display = True
            self._update_content_border(content_box, entry)
            self._active_path = entry.path
            return

        if entry.is_binary:
            self._hide_all_diffs()
            message.update(Text(self._render_message(_BINARY_FILE.bind(path=DisplayPath(entry.rel_path)))))
            message.display = True
            self._update_content_border(content_box, entry)
            self._active_path = entry.path
            return

        if entry.operation == MutationOp.MOVE:
            self._hide_all_diffs()
            old_rel = entry.old_path or "?"
            message.update(
                Text(
                    self._render_message(
                        _FILE_MOVED.bind(
                            old_path=DisplayPath(old_rel),
                            new_path=DisplayPath(entry.rel_path),
                        )
                    )
                )
            )
            message.display = True
            self._update_content_border(content_box, entry)
            self._active_path = entry.path
            return

        if _entry_is_eol_only_change(entry):
            self._hide_all_diffs()
            message.update(Text(self._render_message(_ONLY_LINE_ENDINGS_CHANGED.bind())))
            message.display = True
            self._update_content_border(content_box, entry, counts=(0, 0))
            self._active_path = entry.path
            return

        if _entry_is_metadata_only_change(entry):
            self._hide_all_diffs()
            message.update(Text(self._render_message(_ONLY_BYTE_REPRESENTATION_CHANGED.bind())))
            message.display = True
            self._update_content_border(content_box, entry, counts=(0, 0))
            self._active_path = entry.path
            return

        # Hide message, show diff
        message.display = False

        # Hide current active diff
        if self._active_path and self._active_path in self._diff_cache:
            from chrys.app.tui.widgets.diff_view import DiffView

            old_dv = self._diff_cache[self._active_path]
            if isinstance(old_dv, DiffView):
                old_dv.display = False

        # Check cache or create new DiffView
        dv = None
        if entry.path in self._diff_cache:
            from chrys.app.tui.widgets.diff_view import DiffView

            dv = self._diff_cache[entry.path]
            if isinstance(dv, DiffView):
                dv.display = True
        else:
            from chrys.app.tui.widgets.diff_view import DiffView

            dv = DiffView(
                entry.path,
                entry.path,
                entry.before_text,
                entry.after_text,
                id=f"dv-{hash(entry.path) & 0xFFFFFFFF:08x}",
            )
            await dv.prepare()
            split = self._view_overrides.get(entry.path)
            if split is None:
                additions, removals = dv.counts
                split = additions > 0 and removals > 0
            dv.split = split
            await content_box.mount(dv, before=message)
            self._diff_cache[entry.path] = dv

        self._update_content_border(content_box, entry, dv=dv)
        self._active_path = entry.path

    def _update_content_border(
        self,
        content_box: VerticalGroup,
        entry: DiffFileEntry,
        dv: DiffView | None = None,
        counts: tuple[int, int] | None = None,
    ) -> None:
        """Update the diff-content border title with file path + stats and subtitle metadata."""
        from chrys.app.tui.widgets.diff_view import DiffView

        title = entry.path
        if counts is not None:
            adds, rems = counts
            title = f"{entry.path} (+{adds}, -{rems})"
        elif isinstance(dv, DiffView):
            adds, rems = dv.counts
            title = f"{entry.path} (+{adds}, -{rems})"
        content_box.border_title = Text(title)
        content_box.border_subtitle = _entry_content_subtitle(entry, self._render_message)

    def _hide_all_diffs(self) -> None:
        """Hide all cached DiffView widgets."""
        from chrys.app.tui.widgets.diff_view import DiffView

        for dv in self._diff_cache.values():
            if isinstance(dv, DiffView):
                dv.display = False

    def toggle_view(self) -> None:
        """Toggle split/unified for the active file."""
        from chrys.app.tui.widgets.diff_view import DiffView

        if self._active_path and self._active_path in self._diff_cache:
            dv = self._diff_cache[self._active_path]
            if isinstance(dv, DiffView):
                new_split = not dv.split
                dv.split = new_split
                self._view_overrides[self._active_path] = new_split


# ---------------------------------------------------------------------------
# DiffScreen — full screen
# ---------------------------------------------------------------------------


class _DiffScreenContent(Widget):
    """Deferred tab host mounted only after the outer screen has painted."""

    def __init__(
        self,
        periods_data: dict[int, list[DiffFileEntry]],
        all_entries: list[DiffFileEntry],
        *,
        initial_tab_id: str,
        scopes: dict[int, MutationScope],
        focus: DiffFocus | None = None,
    ) -> None:
        super().__init__(classes="diff-screen-content")
        self._periods_data = periods_data
        self._all_entries = all_entries
        self._initial_tab_id = initial_tab_id
        self._scopes = scopes
        self._focus = focus

    def compose(self) -> ComposeResult:
        render_message = functools.partial(render_str, widget_localizer(self))
        with TabbedContent(initial=self._initial_tab_id):
            for turn_id in sorted(self._periods_data):
                yield TabPane(
                    Content.from_text(
                        render_message(
                            _RUN_TAB.bind(number=turn_id)
                            if isinstance(self._scopes.get(turn_id), WorkflowRunScope)
                            else _TURN_TAB.bind(turn_id=turn_id)
                        ),
                        markup=False,
                    ),
                    DiffPeriodPane(
                        self._periods_data[turn_id],
                        lazy=True,
                        initial_display_key=(
                            self._focus.display_key
                            if self._focus is not None and self._initial_tab_id == f"turn-{turn_id}"
                            else None
                        ),
                    ),
                    id=f"turn-{turn_id}",
                )
            # Keep the session-wide net changeset after the chronological
            # turn tabs. Hide it when it matches the sole visible turn.
            if _should_show_all_tab(self._all_entries, self._periods_data):
                yield TabPane(
                    Content.from_text(render_message(_ALL_TAB.bind()), markup=False),
                    DiffPeriodPane(
                        self._all_entries,
                        lazy=True,
                        initial_display_key=self._focus.display_key if self._focus is not None else None,
                    ),
                    id="turn-all",
                )


class DiffScreen(RightClickScreenCopyMixin, Screen):
    """Full-screen diff viewer with Chat Turn or Workflow Run tabs."""

    CSS_PATH = "screen.tcss"
    _CONTENT_ACTIONS: ClassVar[frozenset[str]] = frozenset({"toggle_view", "toggle_change_list"})

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "go_back", _BACK_BINDING, priority=True),
        localized_binding("space", "toggle_view", _TOGGLE_SPLIT_VIEW_BINDING, priority=True),
        localized_binding("ctrl+g", "toggle_change_list", _TOGGLE_CHANGE_LIST_BINDING, priority=True),
    ]

    def __init__(
        self,
        periods_data: dict[int, list[DiffFileEntry]],
        cwd: str = "",
        subtitle_parts: tuple[str, ...] = (),
        session_id: str = "",
        all_entries: list[DiffFileEntry] | None = None,
        load_data: Callable[[], Awaitable[DiffLoadResult]] | None = None,
        locale_controller: LocaleController | None = None,
        scopes: dict[int, MutationScope] | None = None,
        focus: DiffFocus | None = None,
    ) -> None:
        super().__init__()
        self._periods_data = periods_data
        self._scopes = scopes or {}
        self._cwd = cwd
        self._subtitle_parts = subtitle_parts
        self._session_id = session_id
        self._all_entries = all_entries or []
        self._load_data = load_data
        self._locale_controller = locale_controller
        self._change_list_visible = True
        self._content_loading = True
        self._content_ready = False
        self._close_when_current = False
        self._focus = focus

    def _render_toast(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        return format_message(reference) if controller is None else render_str(controller.localizer, reference)

    def _initial_tab_id(self) -> str:
        """Return the tab that should be active when the screen opens."""
        if self._focus is not None and _should_show_all_tab(self._all_entries, self._periods_data):
            return "turn-all"
        if self._periods_data:
            return f"turn-{max(self._periods_data)}"
        if self._all_entries:
            return "turn-all"
        return ""

    def compose(self) -> ComposeResult:
        from chrys.app.tui.widgets.chrome.app_header import AppHeader

        header = AppHeader(show_approval_badge=False, locale_controller=self._locale_controller)
        yield header
        container = VerticalGroup(id="diff-container")
        from chrys.foundation.util.session_ids import session_short_id

        title = (
            self._render_toast(_DIFF_VIEWER_SESSION_TITLE.bind(session_id=session_short_id(self._session_id)))
            if self._session_id
            else self._render_toast(_DIFF_VIEWER_TITLE.bind())
        )
        container.border_title = Text(title)
        container.border_subtitle = Text(self._cwd)
        with container, VerticalGroup(id="diff-loading-state"):
            yield ChrysLoadingIndicator(id="diff-loading")
        yield ChrysFooter(locale_controller=self._locale_controller)

    def on_mount(self) -> None:
        # Propagate subtitle from the caller (platform info).
        if self._subtitle_parts:
            from chrys.app.tui.widgets.chrome.app_header import AppHeader

            self.query_one(AppHeader).set_subtitle(*self._subtitle_parts)
        # Do not start loading until Textual has painted the header, outer
        # border, centered indicator, and footer at least once.
        self.call_after_refresh(self._start_content_loading)

    def _start_content_loading(self) -> None:
        self.run_worker(self._load_and_mount_content(), exclusive=True, group="diff-screen-content")

    async def _load_and_mount_content(self) -> None:
        """Load data off-loop, prepare the active pane, then reveal it atomically."""
        try:
            if self._load_data is not None:
                result = await self._load_data()
                self._periods_data = result.per_period_entries
                self._scopes = result.scopes
                self._all_entries = result.all_entries
                if not self._periods_data and not self._all_entries:
                    self._content_loading = False
                    self.notify(
                        self._render_toast(_NO_FILE_CHANGES.bind()),
                        title=self._render_toast(_DIFF_VIEWER_TITLE.bind()),
                        severity="warning",
                        timeout=3,
                        markup=False,
                    )
                    self._request_close_when_current()
                    return

            content = _DiffScreenContent(
                self._periods_data,
                self._all_entries,
                initial_tab_id=self._initial_tab_id(),
                scopes=self._scopes,
                focus=self._focus,
            )
            content.display = False
            await self.query_one("#diff-container", VerticalGroup).mount(content)

            # Keep the loading state visible until both the change tree and
            # first DiffView have completed their expensive preparation.
            await self._ensure_active_turn_pane_initialized()
            await remove_shielded(self.query_one("#diff-loading-state", VerticalGroup))
            content.display = True
            self._content_loading = False
            self._content_ready = True
            self.refresh_bindings()
        except Exception as exc:
            self._content_ready = False
            try:
                await self._show_load_error(exc)
            finally:
                self._content_loading = False
                with suppress(Exception):
                    self.refresh_bindings()

    async def _show_load_error(self, exc: Exception) -> None:
        """Show a recoverable error whether or not the loading shell remains."""
        if not self.is_attached:
            return
        with suppress(Exception):
            message = Static(
                Text(self._render_toast(_LOAD_ERROR.bind(error=str(exc)))),
                id="diff-load-error",
            )
            loading_states = list(self.query("#diff-loading-state"))
            if loading_states:
                loading_state = loading_states[0]
                await remove_children_shielded(loading_state)
                await loading_state.mount(message)
                return
            for content in self.query(".diff-screen-content"):
                content.display = False
            await self.query_one("#diff-container", VerticalGroup).mount(message)

    def _request_close_when_current(self) -> None:
        """Close this screen now or after any transient screen above it."""
        self._close_when_current = True
        self._close_if_current()

    def _close_if_current(self) -> None:
        """Pop only when this exact screen owns the top stack slot."""
        # ``Screen.is_current`` also includes transparent background
        # screens, so it is not strong enough to protect the top modal.
        if self.is_attached and self.app.screen is self:
            self._close_when_current = False
            self.app.pop_screen()

    def on_screen_resume(self, _event: events.ScreenResume) -> None:
        """Honor a deferred close and recover stranded footer recomposition."""
        if self._close_when_current:
            self._close_if_current()
            return
        # A bindings signal that fires before this screen becomes active is
        # deliberately stranded by the Footer's active-screen guard; resuming
        # is the recovery point that replays it.
        self.query_one(ChrysFooter).sync_bindings()

    @on(TabbedContent.TabActivated)
    async def _on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if self._content_loading:
            return
        if event.tabbed_content is not self.query_one(TabbedContent):
            return
        pane = event.pane.query_one(DiffPeriodPane)
        pane.set_change_list_visible(self._change_list_visible)
        await pane.ensure_initialized()

    async def _ensure_active_turn_pane_initialized(self) -> None:
        """Initialize the active turn pane without touching inactive tabs."""
        tc = self.query_one(TabbedContent)
        active = tc.active
        if not active:
            return
        active_pane = tc.query_one(f"#{active}", TabPane)
        turn_pane = active_pane.query_one(DiffPeriodPane)
        turn_pane.set_change_list_visible(self._change_list_visible)
        await turn_pane.ensure_initialized()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide and disable content actions until the prepared view is visible."""
        if action in self._CONTENT_ACTIONS and not self._content_ready:
            return False
        return super().check_action(action, parameters)

    def action_go_back(self) -> None:
        if self._content_loading:
            # Unmount also cancels screen-owned workers, but cancel first so
            # no loader continuation can race with the asynchronous pop.
            self.workers.cancel_group(self, "diff-screen-content")
        self.app.pop_screen()

    def action_toggle_view(self) -> None:
        """Toggle split/unified on the active tab's DiffPeriodPane."""
        if not self._content_ready:
            return
        try:
            tc = self.query_one(TabbedContent)
        except NoMatches:
            return
        active_pane = tc.query_one(f"#{tc.active}", TabPane)
        if active_pane is not None:
            turn_pane = active_pane.query_one(DiffPeriodPane)
            if turn_pane is not None:
                turn_pane.toggle_view()

    def action_toggle_change_list(self) -> None:
        """Toggle the file change list tree for every diff tab."""
        if not self._content_ready:
            return
        self._change_list_visible = not self._change_list_visible
        for turn_pane in self.query(DiffPeriodPane):
            turn_pane.set_change_list_visible(self._change_list_visible)
