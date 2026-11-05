# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rollback modal — compact diff viewer with turn picker + rollback actions.

Dismisses with one of:

- ``None`` — user cancelled (Esc / Close).
- ``(target_turn, revert_changes, selected_paths | None)`` — user chose
  a rollback action.  ``target_turn`` follows the engine's "keep N
  turns" convention (``0`` = session start / welcome; ``N >= 1`` =
  keep turns ``1..N``).  The caller publishes a :class:`UserRollback`
  event.

Layout::

    ┌────────────────────────── 80% x 80% ──────────────────────────┐
    │ Title: "Rollback — pick a turn to restore"                    │
    │ ┌─ Select ─────────────────────────────────────────────────┐  │
    │ │ [▼] Turn N (Current) │ Turn N-1 │ … │ Session start       │  │
    │ └──────────────────────────────────────────────────────────┘  │
    │ ┌──────────────────────── DiffPeriodPane ──────────────────────┐│
    │ │  Change list │  DiffView for the selected turn's delta     ││
    │ └────────────────────────────────────────────────────────────┘│
    │ [ Discard conversation after Turn N ]  [*] Revert …  [Close] │
    └───────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING, ClassVar, Protocol

from rich.text import Text
from textual import events, on
from textual.binding import Binding
from textual.containers import HorizontalGroup, VerticalGroup
from textual.content import Content
from textual.strip import Strip
from textual.visual import Visual
from textual.widgets import Button, Static, Tree

from chrys.app.tui.binding_display import CLOSE_BINDING, TOGGLE_VIEW_BINDING, localized_binding
from chrys.app.tui.i18n import render_str
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.screens.diff.screen import DiffPeriodPane
from chrys.app.tui.util.diff_entries import DiffFileEntry
from chrys.app.tui.util.diff_labels import filter_and_dedupe_entries
from chrys.app.tui.util.mutation_sources import merge_mutation_source as _merge_source
from chrys.app.tui.util.removal import remove_children_shielded, remove_shielded
from chrys.app.tui.widgets import Checkbox, ChrysLoadingIndicator, Select
from chrys.foundation.i18n import DisplayBlock, DisplayPath, MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.platform.paths import display_relpath
from chrys.service.mutations.types import (
    FileHashDiff,
    MutationOp,
    MutationProvenance,
    RollbackExclusionReason,
    SnapshotSkipReason,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from textual.app import ComposeResult
    from textual.css.styles import RulesMap
    from textual.style import Style
    from textual.visual import RenderOptions

    from chrys.app.tui.i18n import LocaleController
    from chrys.service.mutations.tracker import MutationTracker
    from chrys.service.mutations.types import RollbackPlan, RollbackWarning

logger = logging.getLogger(__name__)

ROLLBACK_TITLE = msg("tui.rollback.title", fallback="Rollback")
_CHECK_BINDING = msg("tui.binding.toggle_check", fallback="Check")
_ROLLBACK_FAILED = msg("tui.rollback.failed", fallback="Rollback failed: {error}")
_NO_ROLLBACK_SNAPSHOTS = msg(
    "tui.rollback.no_snapshots",
    fallback="No rollback snapshots are available yet.",
)
_FILES_EXCLUDED_NOTE = msg(
    "tui.rollback.files_excluded_note",
    fallback="{count} file excluded from revert: {preview}",
    plural_fallback="{count} files excluded from revert: {preview}",
)
_REVERT_FILE_CHANGES = msg(
    "tui.rollback.revert_file_changes",
    fallback="Revert {count} file change (WYSIWYG)",
    plural_fallback="Revert {count} file changes (WYSIWYG)",
)
_ROLLBACK_TO = msg("tui.rollback.picker.prompt", fallback="Rollback to:")
_ROLLBACK_CLOSE = msg("tui.rollback.button.close", fallback="Close")
_NO_FILE_CHANGES = msg("tui.rollback.no_file_changes", fallback="No file changes to revert.")
_DISCARD_ALL = msg("tui.rollback.button.discard_all", fallback="Discard All")
_DISCARD_AFTER_TURN = msg(
    "tui.rollback.button.discard_after_turn",
    fallback="Discard conversation after Turn {turn}",
)
_TURN_CURRENT = msg("tui.rollback.picker.turn_current", fallback="Turn {turn} (Current)")
_TURN_CURRENT_PREVIEW = msg(
    "tui.rollback.picker.turn_current_preview",
    fallback="Turn {turn} (Current) - ",
)
_TURN = msg("tui.rollback.picker.turn", fallback="Turn {turn}")
_TURN_PREVIEW = msg("tui.rollback.picker.turn_preview", fallback="Turn {turn} - ")
_SESSION_START = msg("tui.rollback.picker.session_start", fallback="Session start")
_DISCARD_ALL_SUFFIX = msg("tui.rollback.picker.discard_all_suffix", fallback="  — discard all")
_LOAD_PREVIEW_ERROR = msg(
    "tui.rollback.preview.load_error",
    fallback="Error: unable to load rollback preview — {detail}",
    multiline=True,
)
_REFRESH_PREVIEW_ERROR = msg(
    "tui.rollback.preview.refresh_error",
    fallback="Error: unable to load this rollback preview — {detail}",
    multiline=True,
)
_WARNING_NOTE = msg("tui.rollback.warning_note", fallback="⚠ {message}")
_MORE_EXCLUSIONS = msg("tui.rollback.exclusions.more", fallback="+{extra_count} more")
_EXCLUSION_UNRESTORABLE = msg(
    "tui.rollback.exclusion.unrestorable",
    fallback="no restorable backup",
)
_EXCLUSION_MOVE_POISONED = msg(
    "tui.rollback.exclusion.move_poisoned",
    fallback="move source unrecoverable",
)
_EXCLUSION_FOREIGN = msg(
    "tui.rollback.exclusion.foreign",
    fallback="changed by another session",
)
_EXCLUSION_CONTESTED = msg(
    "tui.rollback.exclusion.contested",
    fallback="modified internally and externally",
)
_EXCLUSION_PEER_MODIFIED_SINCE = msg(
    "tui.rollback.exclusion.peer_modified_since",
    fallback="changed by another session since",
)
EXCLUSION_PREVIEW_ITEM = msg(
    "tui.rollback.exclusion.preview_item",
    fallback="{path} ({reason})",
)


# Dismissal payload: ``(target_turn, revert_changes, selected_paths | None)``
# — or ``None`` on close/cancel.  ``selected_paths`` is ``None`` when the
# user pressed the plain rollback button (no file revert) and a concrete
# list of absolute paths when they pressed "& Revert Changes".  An empty
# list means the user un-checked everything before confirming — still a
# valid outcome (turns are still popped) and treated as "revert nothing".
RollbackChoice = tuple[int, bool, list[str] | None]


class RollbackModalState(Protocol):
    """Deferred engine projection consumed after the modal's first paint."""

    tracker: MutationTracker
    available_turns: list[int]
    current_turn: int
    workspace_cwd: str
    turn_prompts: dict[int, str]
    plan_augment: Callable[[RollbackPlan, str], RollbackPlan] | None
    attribution_refresh: Callable[[], Awaitable[bool]] | None
    projection_acquire: Callable[[], Awaitable[str]] | None
    projection_release: Callable[[str], None] | None


# Sentinel ``Select`` value used for the "you-are-here" entry at the top
# of the turn picker.  Under the "keep N turns" convention the engine
# accepts ``target_turn >= 0`` (``0`` is the welcome target), so we use
# ``-1`` to guarantee no collision with a real rollback target — the
# entry stays visually present (and dimmed) but semantically inert:
# selecting it in ``_on_turn_changed`` snaps the Select back to the
# previous value.
_LATEST_SENTINEL = -1


class RollbackLoadCancelled(Exception):
    """Picker projection was superseded while its loading shell was open."""


class RollbackProgressModal(BaseDialog[None]):
    """Non-dismissible loading shell shown until session restore UI takes over."""

    BINDINGS: ClassVar[list] = [Binding("escape", "noop", show=False, priority=True)]

    CSS_PATH = "rollback_modal.tcss"

    def __init__(
        self,
        operation: Callable[[], Awaitable[None]],
        *,
        start_worker: Callable[[Awaitable[None]], object] | None = None,
        locale_controller: LocaleController | None = None,
    ) -> None:
        super().__init__(dismiss_on_backdrop=False)
        self._operation = operation
        self._start_worker = start_worker
        self._locale_controller = locale_controller
        self._dismiss_requested = False

    def compose(self) -> ComposeResult:
        with VerticalGroup(id="rollback-progress-container") as container:
            container.border_title = Text(self._render_message(ROLLBACK_TITLE.bind()))
            with VerticalGroup(id="rollback-progress-state"):
                yield ChrysLoadingIndicator(id="rollback-progress-loading")

    def on_mount(self) -> None:
        # Do not start the potentially expensive state scan/event dispatch
        # until Textual has painted the blocking shell at least once.
        self.call_after_refresh(self._start_operation)

    def _start_operation(self) -> None:
        operation = self._run_operation()
        if self._start_worker is None:
            self.run_worker(operation, exclusive=True, group="rollback-progress")
        else:
            self._start_worker(operation)

    async def _run_operation(self) -> None:
        try:
            await self._operation()
        except Exception as exc:
            logger.exception("Rollback operation failed")
            self.notify(
                self._render_message(_ROLLBACK_FAILED.bind(error=str(exc))),
                title=self._render_message(ROLLBACK_TITLE.bind()),
                severity="error",
                timeout=4,
                markup=False,
            )
        finally:
            self._dismiss_requested = True
            self._dismiss_if_current()

    def _dismiss_if_current(self) -> None:
        """Dismiss only after any restore-time loading screen above us closes."""
        if self.is_attached and self.app.screen is self:
            self._dismiss_requested = False
            self.dismiss(None)

    def handoff_to_session_restore(self) -> None:
        """Close this shell before the engine's session-restore modal is pushed."""
        self._dismiss_requested = True
        self._dismiss_if_current()

    def on_screen_resume(self, _event: events.ScreenResume) -> None:
        """Honor completion deferred while agent restore UI covered this modal."""
        if self._dismiss_requested:
            self._dismiss_if_current()

    def action_noop(self) -> None:
        """Consume Escape while rollback owns the active session state."""

    def _render_message(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        return format_message(reference) if controller is None else render_str(controller.localizer, reference)


def _build_cumulative_entries(
    tracker: MutationTracker,
    from_turn_id: int,
    cwd: str,
) -> list[DiffFileEntry]:
    """Aggregate per-file diffs for all turns with ``turn_id >= from_turn_id``.

    Matches :func:`_build_session_entries` semantics but filtered to a
    suffix of the turn history: earliest ``before_hash`` wins, latest
    ``after_hash`` wins, net-zero churn is filtered out.

    Which entries are offered mirrors the rollback plan exactly
    (WYSIWYG): only paths present in
    :meth:`MutationTracker.get_rollback_plan_for_turns` for the same
    window are shown, so every plan-side safety filter (restorability,
    move atomicity) is inherited rather than re-implemented.  Entries
    whose content backup was withheld by SnapshotPolicy are still
    offered when the plan can act on them (delete a skipped create,
    restore a grown/binary file from its stored before-blob), carrying
    ``content_omitted`` so the pane renders a message instead of an
    untruthful empty diff.
    """
    from chrys.foundation.text.encoding import EncodingDetector, decode_bytes

    turns = [t for t in tracker.get_all_turns() if t.turn_id >= from_turn_id]
    if not turns:
        return []

    result: dict[str, FileHashDiff] = {}
    old_paths: dict[str, str | None] = {}
    sources: dict[str, str] = {}
    inferred_paths: set[str] = set()

    def _set_before_if_first(path: str, turn_id: int) -> tuple[str | None, SnapshotSkipReason | None]:
        if path not in result:
            snap = tracker.get_snapshot(path, turn_id)
            before = snap.content_hash if snap else None
            before_skip = snap.skip_reason if snap else None
            result[path] = FileHashDiff(before=before, after=None, before_skip=before_skip)
            return before, before_skip
        return result[path].before, result[path].before_skip

    for turn in sorted(turns, key=lambda t: t.turn_id):
        for m in turn.mutations:
            # Foreign rows never fold into "our" endpoints; their paths
            # are excluded from the plan anyway (intersection below).
            if m.provenance is MutationProvenance.FOREIGN:
                continue
            if m.provenance is MutationProvenance.ASSUMED:
                inferred_paths.add(m.path)
                if m.old_path:
                    inferred_paths.add(m.old_path)
            before, before_skip = _set_before_if_first(m.path, turn.turn_id)
            result[m.path] = FileHashDiff(
                before=before, after=m.after_hash, before_skip=before_skip, after_skip=m.after_skip
            )
            sources[m.path] = _merge_source(sources.get(m.path, ""), m.source.value)
            if m.old_path:
                old_before, old_before_skip = _set_before_if_first(m.old_path, turn.turn_id)
                result[m.old_path] = FileHashDiff(before=old_before, after=None, before_skip=old_before_skip)
                old_paths[m.path] = m.old_path
                sources[m.old_path] = _merge_source(sources.get(m.old_path, ""), m.source.value)

    # Offer exactly what the rollback plan will act on — intersecting
    # with the plan inherits its safety filters (non-restorable
    # snapshots, move atomicity, foreign/contested provenance) without
    # duplicating them.  On top of that, drop net-zero churn from the
    # display (``is_net_zero`` is skip-aware: a skipped create is
    # absent → exists, not net-zero).
    plan_paths = tracker.get_rollback_plan_for_turns({t.turn_id for t in turns}).paths
    filtered: dict[str, FileHashDiff] = {}
    for path, diff in result.items():
        if path not in plan_paths:
            continue
        if diff.is_net_zero:
            continue
        filtered[path] = diff
    if not filtered:
        return []

    detector = EncodingDetector()
    store = tracker.store
    entries: list[DiffFileEntry] = []
    for file_path, diff in filtered.items():
        before_bytes = store.read_blob(diff.before) if diff.before else None
        after_bytes = store.read_blob(diff.after) if diff.after else None

        binary = bool(
            (before_bytes and EncodingDetector.looks_binary(before_bytes))
            or (after_bytes and EncodingDetector.looks_binary(after_bytes))
        )
        before_text = "" if binary or before_bytes is None else decode_bytes(before_bytes)
        after_text = "" if binary or after_bytes is None else decode_bytes(after_bytes)

        # Skip-aware existence: a side with a withheld backup still
        # *exists* on disk (a skipped create must show as CREATE, not
        # as a hash-less MODIFY/DELETE).
        if not diff.before_exists and diff.after_exists:
            op = MutationOp.CREATE
        elif diff.before_exists and not diff.after_exists:
            op = MutationOp.DELETE
        else:
            op = MutationOp.MODIFY

        enc = "utf-8"
        content_for_detect = after_bytes or before_bytes
        if content_for_detect and not binary:
            det = detector.detect_from_bytes(content_for_detect)
            enc = det.encoding or "utf-8"

        rel = display_relpath(file_path, cwd)

        entries.append(
            DiffFileEntry(
                path=file_path,
                rel_path=rel,
                operation=op,
                old_path=old_paths.get(file_path),
                before_text=before_text,
                after_text=after_text,
                is_binary=binary,
                encoding=enc,
                bytes_changed=True,
                before_hash=diff.before,
                after_hash=diff.after,
                source=sources.get(file_path, ""),
                # Only the after side can be skip-marked here — a
                # before-side skip makes the earliest snapshot
                # non-restorable, which the filter above already drops.
                content_omitted=diff.after_skip.value if diff.after_skip else "",
                inferred=file_path in inferred_paths,
            ),
        )

    entries.sort(key=lambda e: e.rel_path)
    return entries


# Human labels for the modal's exclusion note section.
_EXCLUSION_LABELS: dict[RollbackExclusionReason, MessageDef] = {
    RollbackExclusionReason.UNRESTORABLE: _EXCLUSION_UNRESTORABLE,
    RollbackExclusionReason.MOVE_POISONED: _EXCLUSION_MOVE_POISONED,
    RollbackExclusionReason.FOREIGN: _EXCLUSION_FOREIGN,
    RollbackExclusionReason.CONTESTED: _EXCLUSION_CONTESTED,
    RollbackExclusionReason.PEER_MODIFIED_SINCE: _EXCLUSION_PEER_MODIFIED_SINCE,
}

_EXCLUSION_NOTE_MAX_PATHS = 3


def exclusion_reason_label(reason: RollbackExclusionReason | str) -> MessageRef | str:
    """Human label for an exclusion reason — enum or raw ``.value`` string.

    Event payloads carry reasons as primitive strings (foundation must
    not import the service enum); unknown values render verbatim so a
    newer engine never breaks an older consumer.
    """
    if isinstance(reason, str):
        try:
            reason = RollbackExclusionReason(reason)
        except ValueError:
            return str(reason)
    definition = _EXCLUSION_LABELS.get(reason)
    return reason.value if definition is None else definition.bind()


def _plan_for_target(tracker: MutationTracker, target_turn: int) -> RollbackPlan | None:
    """Build the rollback plan for the same turn window ``_entries_for_target`` shows."""
    all_turns = tracker.get_all_turns()
    if target_turn < 0 or not all_turns:
        return None
    turn_ids = {t.turn_id for t in all_turns if target_turn == 0 or t.turn_id > target_turn}
    if not turn_ids:
        return None
    return tracker.get_rollback_plan_for_turns(turn_ids)


def _exclusion_note_text(
    exclusions: list[tuple[str, RollbackExclusionReason]],
    cwd: str,
    locale_controller: LocaleController | None = None,
) -> str:
    """One-line summary of plan exclusions for the modal note section."""
    parts: list[str] = []
    for path, reason in exclusions[:_EXCLUSION_NOTE_MAX_PATHS]:
        rel = display_relpath(path, cwd)
        label = exclusion_reason_label(reason)
        rendered_label = (
            label
            if isinstance(label, str)
            else format_message(label)
            if locale_controller is None
            else render_str(locale_controller.localizer, label)
        )
        item_reference = EXCLUSION_PREVIEW_ITEM.bind(path=DisplayPath(rel), reason=rendered_label)
        parts.append(
            format_message(item_reference)
            if locale_controller is None
            else render_str(locale_controller.localizer, item_reference)
        )
    more = len(exclusions) - _EXCLUSION_NOTE_MAX_PATHS
    if more > 0:
        more_reference = _MORE_EXCLUSIONS.bind(extra_count=more)
        parts.append(
            format_message(more_reference)
            if locale_controller is None
            else render_str(locale_controller.localizer, more_reference)
        )
    count = len(exclusions)
    reference = _FILES_EXCLUDED_NOTE.bind(count=count, preview=", ".join(parts))
    return (
        format_message(reference) if locale_controller is None else render_str(locale_controller.localizer, reference)
    )


def _warning_note_text(
    warnings: list[RollbackWarning],
    locale_controller: LocaleController | None = None,
) -> str:
    """Advisory peer warnings for the modal note section.

    Deduped by message: the coordinator dedupes per peer session, so two
    active peers yield two identical strings — one line suffices.
    """
    unique = dict.fromkeys(w.message for w in warnings)
    references = (_WARNING_NOTE.bind(message=message) for message in unique)
    if locale_controller is None:
        return "\n".join(format_message(reference) for reference in references)
    return "\n".join(render_str(locale_controller.localizer, reference) for reference in references)


def _invert_entry(entry: DiffFileEntry) -> DiffFileEntry:
    """Return the reverse of ``entry`` — what the revert will apply.

    Swaps ``before_text`` ↔ ``after_text`` and flips the operation:

    - ``CREATE`` (file was created) → ``DELETE`` (revert will remove it)
    - ``DELETE`` (file was deleted) → ``CREATE`` (revert will restore it)
    - ``MODIFY`` stays ``MODIFY``, but texts are swapped (revert writes
      the pre-range content)

    ``old_path`` is cleared: the reverse of a MOVE A→B is naturally
    represented by the already-emitted pair (``DELETE`` at B plus
    ``CREATE`` at A) that :func:`_build_cumulative_entries` produces.
    Carrying the forward-direction ``old_path`` annotation through
    would mis-label those entries.
    """
    if entry.operation is MutationOp.CREATE:
        new_op = MutationOp.DELETE
    elif entry.operation is MutationOp.DELETE:
        new_op = MutationOp.CREATE
    else:
        new_op = entry.operation

    return DiffFileEntry(
        path=entry.path,
        rel_path=entry.rel_path,
        operation=new_op,
        old_path=None,
        before_text=entry.after_text,
        after_text=entry.before_text,
        is_binary=entry.is_binary,
        encoding=entry.encoding,
        bytes_changed=entry.bytes_changed,
        before_hash=entry.after_hash,
        after_hash=entry.before_hash,
        source=entry.source,
        content_omitted=entry.content_omitted,
        contested=entry.contested,
        inferred=entry.inferred,
    )


def _entries_for_target(
    tracker: MutationTracker,
    target_turn: int,
    cwd: str,
) -> list[DiffFileEntry]:
    """Build the diff entries shown when the user previews keeping ``target_turn`` turns.

    The diff represents **what the workspace will look like AFTER the
    revert** compared to the current state — i.e. the inverse of the
    cumulative changes made across turns with ``tracker_turn_id >
    target_turn``.  We reuse the same "All"-tab aggregation (filtered
    to turns that would be discarded) and then invert each entry:
    created files become deletions, deleted files become creations,
    and modifies swap their before/after texts.  The effect is that
    "left" in the viewer shows the current on-disk content and "right"
    shows what the file will contain after the revert is applied.

    Under the "keep N turns" convention, ``target_turn = 0`` aggregates
    every tracker turn (discard everything); ``target_turn = N >= 1``
    aggregates tracker turns with ``turn_id >= target_turn + 1``.
    ``turn_id`` matches against tracker entries directly (NOT treated
    as an index into ``get_all_turns()``) — the two are kept in sync
    by the engine (``start_turn(self._turn_number)`` uses the same
    counter that names snapshot files and history turn markers), so a
    ``turn_id`` lookup is both correct and gap-safe.
    """
    all_turns = tracker.get_all_turns()
    if target_turn < 0 or not all_turns:
        return []

    if target_turn == 0:
        # "Session start" — aggregate every turn the tracker knows
        # about.  Use the min turn_id so ``_build_cumulative_entries``
        # doesn't skip pre-history turns that might arrive with
        # turn_id > 1 from a restored session.
        first_turn_id = min(t.turn_id for t in all_turns)
        forward = _build_cumulative_entries(tracker, first_turn_id, cwd)
    else:
        # Keep turns 1..target_turn, discard turns with id > target_turn.
        # Direct turn_id match — gap-safe and not tied to list ordering.
        forward = _build_cumulative_entries(tracker, target_turn + 1, cwd)

    return [_invert_entry(e) for e in forward]


def _build_preview_state(
    tracker: MutationTracker,
    target_turn: int,
    cwd: str,
) -> tuple[list[DiffFileEntry], RollbackPlan | None]:
    """Build the expensive rollback entry and plan projection off-loop."""
    entries = filter_and_dedupe_entries(_entries_for_target(tracker, target_turn, cwd))
    return entries, _plan_for_target(tracker, target_turn)


class _HangingIndentVisual(Visual):
    """Render a fixed turn prefix beside a width-aware wrapped message."""

    def __init__(self, prefix: Content, message: Content) -> None:
        self._prefix = prefix
        self._message = message
        self._combined = prefix + message
        self._prefix_width = prefix.cell_length

    def get_optimal_width(self, rules: RulesMap, container_width: int) -> int:
        return self._combined.get_optimal_width(rules, container_width)

    def get_height(self, rules: RulesMap, width: int) -> int:
        if width <= self._prefix_width or not self._message:
            return self._combined.get_height(rules, width)
        return max(1, self._message.get_height(rules, width - self._prefix_width))

    def render_strips(
        self,
        width: int,
        height: int | None,
        style: Style,
        options: RenderOptions,
    ) -> list[Strip]:
        if width <= 0 or height == 0:
            return []
        if width <= self._prefix_width or not self._message:
            return self._combined.render_strips(width, height, style, options)

        message_width = width - self._prefix_width
        prefix_strips = self._prefix.render_strips(self._prefix_width, 1, style, options)
        message_strips = self._message.render_strips(message_width, height, style, options)
        if not prefix_strips or not message_strips:
            return self._combined.render_strips(width, height, style, options)

        indent = Strip.blank(self._prefix_width, style.rich_style)
        strips = [
            Strip.join([prefix_strips[0], message_strips[0]]),
            *(Strip.join([indent, message_strip]) for message_strip in message_strips[1:]),
        ]
        return strips if height is None else strips[:height]


class _TurnOptionText(Text):
    """Searchable Rich text whose Textual visual uses a hanging indent."""

    __slots__ = ("_message_text", "_prefix_text")

    def __init__(
        self,
        prefix: str,
        message: str,
        *,
        prefix_style: str = "",
        message_style: str = "dim",
    ) -> None:
        super().__init__()
        self._prefix_text = Text(prefix, style=prefix_style)
        self._message_text = Text(message, style=message_style)
        self.append(self._prefix_text)
        self.append(self._message_text)

    def visualize(self) -> Visual:
        """Build the width-aware visual while retaining ``Text.plain`` for search."""
        return _HangingIndentVisual(
            Content.from_rich_text(self._prefix_text),
            Content.from_rich_text(self._message_text),
        )


class _RollbackContent(VerticalGroup):
    """Modal content composed with the shell.

    The turn picker and footer are cheap to build (constructor data
    only), so they render immediately; the diff area starts as a
    loading indicator occupying the same slot the "No file changes"
    placeholder uses, and :meth:`RollbackModal._render_diff_pane`
    replaces it once the preview projection is built off-loop.
    """

    def __init__(
        self,
        options: list[tuple[Text, int]],
        selected_turn: int,
        render_message: Callable[[MessageRef], str],
    ) -> None:
        super().__init__(classes="rollback-content")
        self._options = options
        self._selected_turn = selected_turn
        self._render_message = render_message

    def compose(self) -> ComposeResult:
        select: Select[int] = Select(
            self._options,
            id="rollback-turn-select",
            value=self._selected_turn,
            allow_blank=False,
            prompt=self._render_message(_ROLLBACK_TO.bind()),
        )
        yield select

        with VerticalGroup(id="rollback-diff-wrapper"):
            yield ChrysLoadingIndicator(id="rollback-loading")

        with HorizontalGroup(id="rollback-buttons"):
            checkbox = Checkbox(
                self._render_message(_REVERT_FILE_CHANGES.bind(count=0)),
                value=True,
                id="revert-checkbox",
            )
            # Hidden until a preview with actual file changes mounts.
            checkbox.display = False
            yield checkbox
            yield Static("", id="rollback-buttons-spacer")
            yield Button(
                self._render_message(ROLLBACK_TITLE.bind()),
                id="btn-rollback",
                variant="primary",
                flat=True,
                disabled=True,
            )
            yield Button(self._render_message(_ROLLBACK_CLOSE.bind()), id="btn-close", variant="warning", flat=True)


class RollbackModal(BaseDialog[RollbackChoice | None]):
    """Compact modal for choosing a rollback target and action."""

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "close", CLOSE_BINDING, priority=True),
        # ``space`` mirrors the full /diff screen: toggles split/unified
        # view for the file currently shown in the DiffView.
        localized_binding("space", "toggle_view", TOGGLE_VIEW_BINDING, priority=True),
        # ``x`` checks/unchecks the file (or folder) under the cursor in
        # the change-list tree, scoping which files are reverted when the
        # user confirms "& Revert Changes".
        localized_binding("x", "toggle_check", _CHECK_BINDING, priority=True),
    ]
    _CONTENT_ACTIONS: ClassVar[frozenset[str]] = frozenset({"toggle_view", "toggle_check"})

    CSS_PATH = "rollback_modal.tcss"

    def __init__(
        self,
        tracker: MutationTracker | None,
        available_turns: list[int],
        cwd: str,
        turn_prompts: dict[int, str] | None = None,
        current_turn: int | None = None,
        plan_augment: Callable[[RollbackPlan, str], RollbackPlan] | None = None,
        attribution_refresh: Callable[[], Awaitable[bool]] | None = None,
        load_state: Callable[[], Awaitable[RollbackModalState | None]] | None = None,
        locale_controller: LocaleController | None = None,
    ) -> None:
        """
        Args:
            tracker: Live mutation tracker to read turn diffs from.
            available_turns: ``target_turn`` values eligible for
                rollback under the "keep N turns" convention.
                Typically ``[0, 1, ..., N-1]`` where ``N`` is the
                current turn count.  ``0`` means "session start".
            cwd: Working directory for computing relative paths.
            turn_prompts: Optional ``{turn_number: user_prompt}`` keyed
                by history turn index (``1``-based).  Used to show a
                dimmed preview of each turn's user message in the
                picker.  Missing turns render without a preview.
            current_turn: Engine's current turn counter — the index
                of the most-recently-started turn.  Drives the
                "Turn N (Current)" sentinel row in the picker.
                When ``None`` (back-compat for tests), falls back to
                ``max(available_turns) + 1`` which matches the prior
                behaviour but can mislabel the sentinel when snapshots
                have been pruned/deleted.
            plan_augment: Optional cross-session peer check applied to
                the preview plan (``(plan, cwd) -> plan``), mirroring
                the engine's rollback-time augmentation so the modal
                stays plan-WYSIWYG.
            attribution_refresh: Optional peer reclassification hook
                (``() -> awaitable[changed]``, the engine's refresh
                path) run before each preview build.  A peer claim
                published after the modal opened can demote local rows
                (contested/foreign), reshaping the plan itself —
                ``plan_augment`` alone cannot surface that.
            load_state: Optional deferred engine projection.  When set,
                the modal paints only its loading shell, then awaits this
                callback and keeps the shell visible through preview
                preparation.  This is the production path for long
                sessions; direct tracker arguments remain useful for
                already-loaded callers and focused widget tests.
        """
        super().__init__()
        self._tracker = tracker
        self._available_turns = sorted(available_turns)
        self._cwd = cwd
        self._plan_augment = plan_augment
        self._attribution_refresh = attribution_refresh
        self._projection_acquire: Callable[[], Awaitable[str]] | None = None
        self._projection_release: Callable[[str], None] | None = None
        self._turn_prompts = turn_prompts or {}
        self._current_turn = current_turn
        self._load_state = load_state
        self._locale_controller = locale_controller
        # Default: preview rolling back the most-recent completable turn
        # (largest ``target_turn`` — under "keep N turns" this is the
        # state right after the last completed turn's predecessor).
        self._selected_turn: int = self._available_turns[-1] if self._available_turns else 0
        self._pane: DiffPeriodPane | None = None
        self._content_loading = True
        self._content_ready = False
        self._preview_refreshing = False
        self._preview_failed = False

    def _render_message(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        return format_message(reference) if controller is None else render_str(controller.localizer, reference)

    def compose(self) -> ComposeResult:
        # Production starts with a state-loading shell. Already-loaded
        # callers start one stage later with the Select/footer and a preview
        # spinner in the "No file changes" slot. Both remain compact until
        # a real diff expands the modal (``_set_empty_layout``).
        with VerticalGroup(id="rollback-container", classes="empty") as container:
            container.border_title = Text(self._render_message(ROLLBACK_TITLE.bind()))
            if self._load_state is None:
                yield _RollbackContent(self._build_turn_options(), self._selected_turn, self._render_message)
            else:
                with VerticalGroup(id="rollback-initial-loading-state"):
                    yield ChrysLoadingIndicator(id="rollback-loading")

    def on_mount(self) -> None:
        if self._load_state is None:
            self._refresh_button_labels()
        self.call_after_refresh(self._start_initial_load)

    def _start_initial_load(self) -> None:
        self.run_worker(self._load_and_mount_content(), exclusive=True, group="rollback-modal-content")

    async def _load_and_mount_content(self) -> None:
        """Resolve state and preview while the first-painted loading shell remains."""
        deferred_content: _RollbackContent | None = None
        try:
            if self._load_state is not None:
                state = await self._load_state()
                if state is None:
                    self.notify(
                        self._render_message(_NO_ROLLBACK_SNAPSHOTS.bind()),
                        title=self._render_message(ROLLBACK_TITLE.bind()),
                        severity="warning",
                        timeout=3,
                        markup=False,
                    )
                    self._content_loading = False
                    self.dismiss(None)
                    return
                self._apply_loaded_state(state)
                deferred_content = _RollbackContent(
                    self._build_turn_options(),
                    self._selected_turn,
                    self._render_message,
                )
                deferred_content.display = False
                await self.query_one("#rollback-container", VerticalGroup).mount(deferred_content)
                self._refresh_button_labels()

            entries, plan = await self._load_preview_state_guarded()
            await self._render_diff_pane(entries, plan)
            if deferred_content is not None:
                await self._reveal_deferred_content(deferred_content)
            self._content_loading = False
            self._content_ready = True
            # Re-enables the rollback button and refreshes bindings.
            self._set_preview_refreshing(False)
        except RollbackLoadCancelled:
            self._content_loading = False
            self.dismiss(None)
        except Exception as exc:
            self._content_ready = False
            try:
                await self._show_load_error(exc)
                if deferred_content is not None:
                    await self._reveal_deferred_content(deferred_content)
            finally:
                self._content_loading = False
                self._preview_refreshing = False
                with suppress(Exception):
                    self.refresh_bindings()

    def _apply_loaded_state(self, state: RollbackModalState) -> None:
        """Install one immutable-value engine projection for preview rendering."""
        self._tracker = state.tracker
        self._available_turns = sorted(state.available_turns)
        self._turn_prompts = state.turn_prompts
        self._current_turn = state.current_turn
        self._cwd = state.workspace_cwd
        self._plan_augment = state.plan_augment
        self._attribution_refresh = state.attribution_refresh
        self._projection_acquire = state.projection_acquire
        self._projection_release = state.projection_release
        if self._available_turns:
            self._selected_turn = self._available_turns[-1]
        else:
            self._selected_turn = 0

    async def _reveal_deferred_content(self, content: _RollbackContent) -> None:
        """Swap fully prepared content for the initial indicator in one event-loop turn."""
        content.display = True
        loading_states = list(self.query("#rollback-initial-loading-state"))
        if loading_states:
            await remove_shielded(loading_states[0])

    async def _show_load_error(self, exc: Exception) -> None:
        """Replace the preview slot with a recoverable, fail-closed error state."""
        if not self.is_attached:
            return
        with suppress(Exception):
            self.query_one("#btn-rollback", Button).disabled = True
        self._set_revert_checkbox_visible(False)
        self._set_empty_layout(True)
        message = Static(
            Text(self._render_message(_LOAD_PREVIEW_ERROR.bind(detail=DisplayBlock(str(exc))))),
            id="rollback-load-error",
        )
        wrappers = list(self.query("#rollback-diff-wrapper"))
        if wrappers:
            with suppress(Exception):
                wrapper = wrappers[0]
                await remove_children_shielded(wrapper)
                await wrapper.mount(message)
            return
        # State projection itself failed, before the Select/footer could
        # be built. Keep the modal recoverable by replacing the initial
        # spinner with an error and a normal close affordance.
        with suppress(Exception):
            loading_state = self.query_one("#rollback-initial-loading-state", VerticalGroup)
            await remove_children_shielded(loading_state)
            await loading_state.mount(
                message,
                Button(
                    self._render_message(_ROLLBACK_CLOSE.bind()),
                    id="btn-close",
                    variant="warning",
                    flat=True,
                ),
            )

    _PROMPT_PREVIEW_MAX = 160

    def _build_turn_options(self) -> list[tuple[Text, int]]:
        """Return ``(label, value)`` pairs for the turn-picker ``Select``.

        Under the "keep N turns" convention, each ``target_turn = N``
        directly labels itself "Turn N" (= last kept turn).  Layout,
        top → bottom:

        1. ``Turn {current}  (Current)`` — disabled sentinel
           communicating "you are here".  ``current`` is the engine's
           :attr:`AgentEngine.current_turn_number` when provided, else
           ``max(available_turns) + 1`` as a fallback.  Using the
           engine counter keeps the sentinel correct even when every
           snapshot on disk has been pruned/deleted (in which case
           ``available_turns`` collapses to ``[0]``).  Value is
           :data:`_LATEST_SENTINEL` so :meth:`_on_turn_changed` can
           detect accidental selection and snap back.
        2. For each rollback target ``t >= 1`` in ``available_turns``
           (reverse sorted): ``Turn {t}  <user prompt preview>``
           (value = ``t``).  Preview pulled from
           ``self._turn_prompts[t]`` (history turn index = target).
        3. ``Session start  — discard all`` (value = ``0``), iff ``0``
           is in ``available_turns``.
        """
        if not self._available_turns:
            return []
        options: list[tuple[Text, int]] = []
        # Prefer the engine-supplied current turn number — it's
        # authoritative even when snapshots have been deleted (in
        # which case ``available_turns`` may collapse to just ``[0]``
        # and ``max + 1`` would under-report the true current turn).
        current_turn = self._current_turn if self._current_turn is not None else self._available_turns[-1] + 1
        options.append((self._format_latest_entry(current_turn), _LATEST_SENTINEL))
        for t in sorted(self._available_turns, reverse=True):
            if t == 0:
                continue
            options.append((self._format_keep_entry(t), t))
        if 0 in self._available_turns:
            options.append((self._format_session_start_entry(), 0))
        return options

    def _format_latest_entry(self, latest: int) -> Text:
        """``Turn N (Current) - <preview>`` — fully dimmed."""
        preview = self._clip_preview(self._turn_prompts.get(latest, ""))
        if preview:
            return _TurnOptionText(
                self._render_message(_TURN_CURRENT_PREVIEW.bind(turn=latest)),
                preview,
                prefix_style="dim",
                message_style="dim italic",
            )
        return Text(self._render_message(_TURN_CURRENT.bind(turn=latest)), style="dim")

    def _format_keep_entry(self, kept: int) -> Text:
        """``Turn N - <dim user prompt preview>`` for a selectable target."""
        preview = self._clip_preview(self._turn_prompts.get(kept, ""))
        if preview:
            return _TurnOptionText(self._render_message(_TURN_PREVIEW.bind(turn=kept)), preview)
        return Text(self._render_message(_TURN.bind(turn=kept)))

    def _format_session_start_entry(self) -> Text:
        """``Session start  — discard all`` bottom row."""
        label = Text(self._render_message(_SESSION_START.bind()))
        label.append(self._render_message(_DISCARD_ALL_SUFFIX.bind()), style="dim")
        return label

    def _clip_preview(self, raw: str) -> str:
        """Collapse whitespace + truncate to :attr:`_PROMPT_PREVIEW_MAX`."""
        preview = " ".join(raw.split()).strip()
        if len(preview) > self._PROMPT_PREVIEW_MAX:
            preview = preview[: self._PROMPT_PREVIEW_MAX - 1] + "…"
        return preview

    async def _load_preview_state(self) -> tuple[list[DiffFileEntry], RollbackPlan | None]:
        """Refresh attribution and build the selected preview without touching the DOM."""
        if self._attribution_refresh is not None:
            # Peer claims published after the modal opened can demote
            # local rows (contested/foreign), reshaping the plan the
            # entries below are built from — reclassify BEFORE reading
            # the tracker, matching the engine's execution-time order
            # (reclassify → build plan → augment).  This is the ENGINE
            # refresh (persists on change); cheap when nothing changed;
            # failures fall back to the stale view, which the engine
            # re-checks destructively anyway.
            try:
                await self._attribution_refresh()
            except Exception:
                logger.debug("Rollback preview attribution refresh failed", exc_info=True)
        tracker = self._tracker
        if tracker is None:
            raise RuntimeError("rollback state was not loaded")
        entries, plan = await asyncio.to_thread(_build_preview_state, tracker, self._selected_turn, self._cwd)
        # Paths the plan dropped (non-restorable, move-poisoned, foreign,
        # …) are surfaced as a non-checkable note, never silently.
        if plan is not None and self._plan_augment is not None:
            # Same peer check the engine runs at execution time
            # (PEER_MODIFIED_SINCE) — the preview must not offer files
            # the backend will then exclude.  Registry I/O failures
            # leave the un-augmented plan; the engine re-checks anyway.
            # Executor hop: augmentation scans and reads peer registry
            # files — like every engine-side call of the same method,
            # that I/O must not run on the UI event loop.
            try:
                plan = await asyncio.to_thread(self._plan_augment, plan, self._cwd)
            except Exception:
                logger.debug("Rollback preview peer augmentation failed", exc_info=True)
            else:
                # ``entries`` intersected the UN-augmented plan (inside
                # ``_entries_for_target``); drop what the peer check just
                # excluded so the tree matches the plan.
                excluded_paths = {path for path, _reason in plan.exclusions}
                entries = [e for e in entries if e.path not in excluded_paths]
        return entries, plan

    async def _load_preview_state_guarded(self) -> tuple[list[DiffFileEntry], RollbackPlan | None]:
        """Build one preview while backend prompt admission remains fenced."""
        acquire = self._projection_acquire
        release = self._projection_release
        if acquire is None or release is None:
            return await self._load_preview_state()

        owner = await acquire()
        projection_task = asyncio.create_task(self._load_preview_state())
        release_deferred = False
        try:
            return await asyncio.shield(projection_task)
        except asyncio.CancelledError:
            release_deferred = True

            def _finish_background_projection(
                task: asyncio.Task[tuple[list[DiffFileEntry], RollbackPlan | None]],
            ) -> None:
                try:
                    task.result()
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.debug("Cancelled rollback preview projection failed", exc_info=True)
                finally:
                    release(owner)

            projection_task.add_done_callback(_finish_background_projection)
            raise
        finally:
            if not release_deferred:
                release(owner)

    async def _refresh_diff_pane(self) -> None:
        """Load and rebuild the preview for a newly selected turn."""
        self._preview_failed = False
        self._set_preview_refreshing(True)
        try:
            entries, plan = await self._load_preview_state_guarded()
            await self._render_diff_pane(entries, plan)
        except RollbackLoadCancelled:
            self.dismiss(None)
        except asyncio.CancelledError:
            self._preview_failed = True
            raise
        except Exception as exc:
            # The selected target changes before its projection is loaded.  A
            # failure must therefore invalidate the old pane: otherwise its
            # checked paths could be confirmed against the new target turn.
            self._preview_failed = True
            logger.warning("Rollback preview refresh failed", exc_info=True)
            await self._show_preview_error(exc)
        finally:
            self._set_preview_refreshing(False)

    async def _show_preview_error(self, exc: Exception) -> None:
        """Replace any prior projection with a non-destructive error state."""
        self._pane = None
        self._set_revert_checkbox_visible(False)
        self._set_empty_layout(True)
        try:
            wrapper = self.query_one("#rollback-diff-wrapper", VerticalGroup)
            await remove_children_shielded(wrapper)
            await wrapper.mount(
                Static(
                    Text(self._render_message(_REFRESH_PREVIEW_ERROR.bind(detail=DisplayBlock(str(exc))))),
                    id="rollback-preview-error",
                )
            )
        except Exception:
            # The safety state is the cleared pane + failed flag.  Keep those
            # authoritative even if teardown races removal of the wrapper.
            logger.debug("Unable to render rollback preview error", exc_info=True)

    async def _render_diff_pane(self, entries: list[DiffFileEntry], plan: RollbackPlan | None) -> None:
        """Replace preview widgets from an already-computed entry/plan projection."""
        wrapper = self.query_one("#rollback-diff-wrapper", VerticalGroup)
        # Drop the old pane reference before yielding so a queued confirm
        # can never act on a preview that is being replaced.
        self._pane = None
        await remove_children_shielded(wrapper)
        has_changes = bool(entries)
        exclusions = plan.exclusions if plan is not None else []
        warnings = plan.warnings if plan is not None else []
        # The revert checkbox has nothing to gate when there are no
        # file changes at all.  Hide it (rather than disable it) so
        # the footer collapses to a clean "Rollback / Close" pair.
        self._set_revert_checkbox_visible(has_changes)
        # Collapse the modal to content height when there is no diff
        # to show — the big 90% overlay feels empty otherwise.  A CSS
        # class on the container drives the layout swap (see
        # ``#rollback-container.empty`` in the .tcss); the class stays
        # absent while a diff is mounted so the container returns to
        # its full 90%.
        self._set_empty_layout(not has_changes)
        if not has_changes:
            widgets: list[Static] = [
                Static(Text(self._render_message(_NO_FILE_CHANGES.bind())), id="rollback-diff-placeholder")
            ]
            if warnings:
                widgets.append(
                    Static(Text(_warning_note_text(warnings, self._locale_controller)), id="rollback-warnings-note")
                )
            if exclusions:
                # Text() so paths like ``a[bad].py`` render literally
                # instead of being parsed as Textual markup.
                widgets.append(
                    Static(
                        Text(_exclusion_note_text(exclusions, self._cwd, self._locale_controller)),
                        id="rollback-exclusions-note",
                    )
                )
            await wrapper.mount(*widgets)
            # Reset checkbox state so the next turn (which may have
            # changes) starts in a known state.
            self._sync_revert_checkbox(selected=0, total=0)
            return
        # ``checkable=True`` renders [x]/[ ] prefixes on every tree
        # entry.  Defaults to all-checked; rebuilding the pane on turn
        # change (this method re-runs for every Select change) resets
        # the selection — satisfies "change turn → re-check everything".
        pane = DiffPeriodPane(entries, checkable=True, lazy=True)
        widgets_with_pane: list[Static | DiffPeriodPane] = [pane]
        if warnings:
            # The destructive confirm must not be the first place the
            # user learns a peer is mid-command — surface the advisory
            # BEFORE the buttons, next to the exclusion note.
            widgets_with_pane.append(
                Static(Text(_warning_note_text(warnings, self._locale_controller)), id="rollback-warnings-note")
            )
        if exclusions:
            widgets_with_pane.append(
                Static(
                    Text(_exclusion_note_text(exclusions, self._cwd, self._locale_controller)),
                    id="rollback-exclusions-note",
                )
            )
        await wrapper.mount(*widgets_with_pane)
        await pane.ensure_initialized()
        self._pane = pane
        # Fresh pane → every entry starts checked.
        self._sync_revert_checkbox(selected=len(entries), total=len(entries))

    def _set_preview_refreshing(self, refreshing: bool) -> None:
        """Disable destructive/preview actions while a turn projection is rebuilt."""
        self._preview_refreshing = refreshing
        with suppress(Exception):
            self.query_one("#btn-rollback", Button).disabled = refreshing or self._preview_failed
        if self._content_ready:
            self.refresh_bindings()

    def _set_revert_checkbox_visible(self, visible: bool) -> None:
        """Toggle visibility of the footer revert checkbox."""
        try:
            cb = self.query_one("#revert-checkbox", Checkbox)
        except Exception:
            return
        cb.display = visible

    def _set_empty_layout(self, empty: bool) -> None:
        """Add/remove the ``empty`` class on the container.

        Drives the height collapse/expand in the .tcss: the class
        swaps ``height: 90%`` for ``height: auto`` on the container
        and its wrapper children, so the modal shrinks to just the
        Select + placeholder + button row while loading and when
        there are no changes to revert.  The container composes with
        the class already set; the first mounted diff flips it off so
        a turn with changes gets the full overlay.
        """
        try:
            container = self.query_one("#rollback-container", VerticalGroup)
        except Exception:
            return
        container.set_class(empty, "empty")

    def _sync_revert_checkbox(self, *, selected: int, total: int) -> None:
        """Update the revert checkbox's label to reflect the current count.

        Also auto-flips ``value`` at the edges so the visible state
        matches what the Rollback button will actually do:

        - ``selected == 0`` → uncheck (nothing to revert, revert=False).
        - ``selected == total`` → check (every file included).

        Programmatic value changes suppress ``Checkbox.Changed`` at the
        source so they cannot cascade back onto the tree after this method
        returns (the tree is the source of truth here).
        """
        try:
            cb = self.query_one("#revert-checkbox", Checkbox)
        except Exception:
            return
        cb.label = self._render_message(_REVERT_FILE_CHANGES.bind(count=selected))
        with cb.prevent(Checkbox.Changed):
            if selected == 0 and cb.value:
                cb.value = False
            # On turn change (total changed) force-reset value to True so
            # the user doesn't inherit the previous turn's toggle state.
            # Heuristic: a new turn always starts with selected == total.
            if selected == total and total > 0 and not cb.value:
                cb.value = True

    def _refresh_button_labels(self) -> None:
        # Under the "keep N turns" convention the selected value *is*
        # the last kept turn, so the label is a direct substitution
        # — no gap-aware lookup needed.  ``target = 0`` is the welcome
        # case and collapses to "Discard All".
        target = self._selected_turn
        label_plain = (
            self._render_message(_DISCARD_ALL.bind())
            if target <= 0
            else self._render_message(_DISCARD_AFTER_TURN.bind(turn=target))
        )
        self.query_one("#btn-rollback", Button).label = label_plain

    @on(Select.Changed, "#rollback-turn-select")
    async def _on_turn_changed(self, event: Select.Changed) -> None:
        if event.value is Select.NULL:
            return
        new_turn = int(event.value)
        if new_turn == self._selected_turn:
            return
        if new_turn == _LATEST_SENTINEL or not self._content_ready or self._preview_refreshing:
            # The "you-are-here" sentinel entry is purely informational,
            # and a change landing before content is ready (or while a
            # refresh is in flight) cannot be honored.  Either way the
            # Select must not be left displaying a turn that
            # ``_selected_turn`` doesn't hold — a silent drop here
            # desyncs the dropdown from the turn a later confirm
            # dismisses with.  Snap back to the prior valid target;
            # ``prevent`` suppresses the recursive ``Select.Changed``
            # the reassignment would otherwise emit.
            with self.prevent(Select.Changed):
                event.select.value = self._selected_turn
            return
        self._selected_turn = new_turn
        self._refresh_button_labels()
        await self._refresh_diff_pane()

    @on(Button.Pressed, "#btn-rollback")
    def _on_rollback(self, _event: Button.Pressed) -> None:
        """Confirm handler — decide whether to revert files based on
        the footer checkbox **and** the tree selection.

        The rule the user asked for: revert iff the checkbox is
        checked *and* at least one file is selected in the tree.
        Otherwise fall through to a history-only rollback with no
        file restoration — matches what the user sees in the
        checkbox state.
        """
        if not self._content_ready or self._preview_refreshing or self._preview_failed:
            return
        paths = self._pane.selected_paths() if self._pane is not None else []
        try:
            cb = self.query_one("#revert-checkbox", Checkbox)
            want_revert = bool(cb.value)
        except Exception:
            want_revert = False
        if want_revert and paths:
            self.dismiss((self._selected_turn, True, paths))
        else:
            # No revert — selected_paths is moot, pass ``None`` so the
            # backend takes the plain-rollback path unambiguously.
            self.dismiss((self._selected_turn, False, None))

    @on(Button.Pressed, "#btn-close")
    def _on_close(self, _event: Button.Pressed) -> None:
        self.dismiss(None)

    @on(DiffPeriodPane.SelectionChanged)
    def _on_selection_changed(self, event: DiffPeriodPane.SelectionChanged) -> None:
        """Keep the revert-checkbox label in sync with the tree."""
        event.stop()
        self._sync_revert_checkbox(selected=event.selected_count, total=event.total_count)

    @on(Checkbox.Changed, "#revert-checkbox")
    def _on_revert_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Cascade footer checkbox → every leaf in the tree.

        Clicking the "Revert N file changes" checkbox off should clear
        the tree selection; clicking it back on should re-select
        everything. Programmatic writes from ``_sync_revert_checkbox``
        prevent this message at the source because they already reflect the
        tree state.
        """
        event.stop()
        if self._pane is None:
            return
        self._pane.set_all_checked(bool(event.value))
        selected, total = self._pane.selection_counts()
        self._sync_revert_checkbox(selected=selected, total=total)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Hide and disable preview actions until interactive content is ready."""
        if action in self._CONTENT_ACTIONS and (
            not self._content_ready or self._preview_refreshing or self._preview_failed
        ):
            return False
        return super().check_action(action, parameters)

    def _before_dismiss(self, _result: object | None = None) -> None:
        """Cancel initial preview work before any Esc, button, or backdrop dismissal."""
        if self._content_loading and self.is_attached:
            self.workers.cancel_group(self, "rollback-modal-content")
        super()._before_dismiss(_result)

    def action_close(self) -> None:
        self.dismiss(None)

    def action_toggle_view(self) -> None:
        """Space binding — delegate split/unified toggle to the pane."""
        if self._content_ready and not self._preview_refreshing and not self._preview_failed and self._pane is not None:
            self._pane.toggle_view()

    def action_toggle_check(self) -> None:
        """X binding — toggle the check state of the cursor node.

        Operates on whichever ``Tree`` inside the pane currently has
        focus.  Folder cursor → cascades to every descendant leaf
        (fully-checked folders flip to unchecked, otherwise everything
        flips on) per the common tri-state convention.
        """
        if not self._content_ready or self._preview_refreshing or self._preview_failed or self._pane is None:
            return
        focused = self.focused
        if not isinstance(focused, Tree):
            return
        node = focused.cursor_node
        if node is None:
            return
        self._pane.toggle_check_node(node)
