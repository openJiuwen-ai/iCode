# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compact session file changes below the conversation directory."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.style import Style
from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Static

from chrys.app.tui.i18n import render_str
from chrys.app.tui.util.diff_entries import MAX_PREVIEW_FILES, DiffFileEntry
from chrys.app.tui.util.diff_labels import entry_display_key, entry_label_parts
from chrys.app.tui.util.paths import middle_cut_path
from chrys.app.tui.util.visibility import is_widget_shown
from chrys.app.tui.widgets.sidebar.toc import ConversationToc
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_scalar

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.events import Click, Leave, MouseEvent, MouseMove

    from chrys.app.tui.i18n import LocaleController

_TITLE = msg(
    "tui.sidebar.changes.title",
    fallback="File Changes ({count})",
    plural_fallback="File Changes ({count})",
)
_VIEW_ALL = msg("tui.sidebar.changes.view_all", fallback="View all")


def _counts_text(counts: tuple[int, int]) -> Text:
    added, removed = counts
    return Text.assemble((f"+{added}", "bold green"), " ", (f"-{removed}", "bold red"))


def preview_rows(total: int, height: int) -> int:
    """Keep at least half of Chat for the conversation directory."""
    return max(0, min(total, MAX_PREVIEW_FILES, height // 2 - 3))


class DiffPreview(Vertical, can_focus=False):
    """A fixed, non-scrolling set of file rows; clicks open the shared diff viewer."""

    class Opened(Message):
        def __init__(self, display_key: str | None = None) -> None:
            super().__init__()
            self.display_key = display_key

    class RefreshRequested(Message):
        """The Chat panel became visible; catch up any deferred refresh."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {"diff-preview--hover"}
    DEFAULT_CSS = """
    DiffPreview {
        display: none;
    }
    DiffPreview > .diff-preview--hover {
        background: $primary 30%;
    }
    DiffPreview #diff-preview-footer {
        height: 1;
        align: left middle;
    }
    DiffPreview #diff-preview-all {
        width: auto;
        height: 1;
        color: $text-muted;
        text-style: underline;
        pointer: pointer;
    }
    DiffPreview > #diff-preview-box {
        height: 1fr;
        border: round $tui-border-primary-darken-3 $inner-border-opacity;
        border-title-color: $text-muted;
        border-title-style: bold;
        padding: 0 1;
        overflow: hidden hidden;
    }
    DiffPreview #diff-preview-rows {
        width: 100%;
        height: 1fr;
        pointer: pointer;
    }
    """

    def __init__(self, *, locale_controller: LocaleController | None = None) -> None:
        super().__init__()
        self._locale_controller = locale_controller
        self.entries: tuple[DiffFileEntry, ...] = ()
        self._total_counts: tuple[int, int] | None = None
        self._total_counts_lower_bound = False
        self._shown_title: str | None = None
        self.rows = 0
        self._hovered: int | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="diff-preview-box"):
            yield Static(id="diff-preview-rows")
            with Horizontal(id="diff-preview-footer"):
                yield Static(Text(self._text(_VIEW_ALL.bind())), id="diff-preview-all")

    def on_mount(self) -> None:
        self.refresh_localization()

    def set_entries(self, entries: tuple[DiffFileEntry, ...]) -> None:
        self.entries = entries
        self._total_counts = None
        self._total_counts_lower_bound = False
        added = removed = known = 0
        for entry in entries:
            if entry.line_count_result == "pending":
                break
            if (counts := entry.line_counts) is not None:
                known += 1
                added += counts[0]
                removed += counts[1]
        else:
            if known:
                self._total_counts = added, removed
                self._total_counts_lower_bound = known < len(entries)
        self._paint()

    def resize_rows(self, rows: int) -> None:
        if rows == self.rows:
            return
        self.rows = rows
        self.styles.height = rows + 3
        self.display = rows > 0
        self._hovered = None
        self._paint()

    def refresh_localization(self) -> None:
        if self.is_mounted:
            self.query_one("#diff-preview-all", Static).update(Text(self._text(_VIEW_ALL.bind())))
            self._paint()

    def _text(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        return format_message(reference) if controller is None else render_str(controller.localizer, reference)

    def _paint(self) -> None:
        if not self.is_mounted:
            return
        box = self.query_one("#diff-preview-box")
        title = Text(self._text(_TITLE.bind(count=len(self.entries))))
        if self._total_counts is not None:
            title.append(" ")
            if self._total_counts_lower_bound:
                title.append("≥", "bold green")
            title.append_text(_counts_text(self._total_counts))
        if self._shown_title != title.plain:
            box.border_title = title
            self._shown_title = title.plain
        target = self.query_one("#diff-preview-rows", Static)
        width = target.content_size.width
        text = Text()
        hover = Style(bgcolor=self.get_component_rich_style("diff-preview--hover").bgcolor)
        for index, entry in enumerate(self.entries[: self.rows]):
            if index:
                text.append("\n")
            _, note = entry_label_parts(entry, self._text)
            trailer = _counts_text(entry.line_counts) if entry.line_counts is not None else Text()
            if entry.line_count_result == "pending":
                trailer = Text("…", style="dim")
            room = max(0, width - note.cell_len - trailer.cell_len - bool(trailer))
            path = middle_cut_path(sanitize_legacy_scalar(entry_display_key(entry)), room)
            row = Text.assemble(path, note)
            row.append(" " * max(0, width - row.cell_len - trailer.cell_len))
            row.append_text(trailer)
            if index == self._hovered:
                row.stylize(hover)
            text.append_text(row)
        target.update(text, layout=False)

    def on_resize(self) -> None:
        self._hovered = None
        self._paint()

    def on_hide(self) -> None:
        self._hovered = None
        self._paint()

    def on_click(self, event: Click) -> None:
        if event.chain != 1:
            return
        if event.get_content_offset(self.query_one("#diff-preview-all", Static)) is not None:
            event.stop()
            event.prevent_default()
            self.post_message(self.Opened())
        elif (row := self._row_at(event)) is not None:
            event.stop()
            event.prevent_default()
            self.post_message(self.Opened(entry_display_key(self.entries[row])))

    def _row_at(self, event: MouseEvent) -> int | None:
        target = self.query_one("#diff-preview-rows", Static)
        offset = event.get_content_offset(target) if event.widget is target else None
        return offset.y if offset is not None and 0 <= offset.y < min(self.rows, len(self.entries)) else None

    def on_mouse_move(self, event: MouseMove) -> None:
        hovered = self._row_at(event)
        if hovered != self._hovered:
            self._hovered = hovered
            self._paint()

    def on_leave(self, _event: Leave) -> None:
        if self._hovered is not None:
            self._hovered = None
            self._paint()


class SidebarChatPanel(Vertical, can_focus=False):
    """Conversation directory and a height-bounded file-change preview."""

    DEFAULT_CSS = "SidebarChatPanel { height: 1fr; }"

    def __init__(self, *, locale_controller: LocaleController | None = None) -> None:
        super().__init__()
        self._locale_controller = locale_controller

    def compose(self) -> ComposeResult:
        yield ConversationToc(locale_controller=self._locale_controller)
        yield DiffPreview(locale_controller=self._locale_controller)

    def set_entries(self, entries: tuple[DiffFileEntry, ...]) -> None:
        preview = self.query_one(DiffPreview)
        previous_count = len(preview.entries)
        preview.set_entries(entries)
        if len(entries) != previous_count:
            self._split()

    def on_resize(self) -> None:
        self._split()
        self.post_message(DiffPreview.RefreshRequested())

    def on_show(self) -> None:
        self._split()
        self.post_message(DiffPreview.RefreshRequested())

    def _split(self) -> None:
        if not self.is_mounted:
            return
        preview = self.query_one(DiffPreview)
        if preview.entries and not is_widget_shown(self):
            return
        rows = preview_rows(len(preview.entries), self.content_size.height)
        preview.resize_rows(rows)
