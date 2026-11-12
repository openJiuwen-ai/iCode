# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A transparent modal holding a multiline comment bar beside its anchored code line."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from rich.cells import cell_len
from rich.text import Text
from textual import events, on
from textual.containers import VerticalGroup
from textual.geometry import Region, Size
from textual.message import Message

from chrys.app.tui.binding_display import CANCEL_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.util.diff_comments import DiffComment, DiffDraft
from chrys.app.tui.util.formatting import elide_middle
from chrys.app.tui.util.placement import pin_fixed_placement
from chrys.app.tui.widgets import EnhancedTextArea
from chrys.app.tui.widgets.chat.quote_popup import QUOTE_INPUT_UNAVAILABLE
from chrys.app.tui.widgets.diff_view import DiffView
from chrys.app.tui.widgets.text_area import MESSAGE_EDITOR_PASTE_MAX_TOKENS, NEWLINE_SHORTCUT_KEYS
from chrys.foundation.i18n import msg
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar
from chrys.foundation.platform.files import surrogate_safe_text

if TYPE_CHECKING:
    from textual._arrange import DockArrangeResult
    from textual.app import ComposeResult

    from chrys.app.tui.widgets.diff_view.code import CodeColumn
    from chrys.app.tui.widgets.diff_view.quote import DiffQuote
    from chrys.foundation.text.draft_references import DraftReference

_PLACEHOLDER = msg("tui.diff_quote.placeholder", fallback="Comment (optional)")
_KEYS = msg("tui.diff_quote.keys", fallback="Enter Add to input · Esc Cancel")
_KEYS_COMPACT = msg("tui.diff_quote.keys_compact", fallback="↵/Esc")
_ADDED = msg("tui.diff_quote.added", fallback="Added to the message input: {label}")
COMMENT_CHANGED = msg(
    "tui.diff_quote.changed",
    fallback="This comment has changed in the message input. Please edit it there.",
)
_MIN_PATH_CELLS = 12
_BEFORE = msg("tui.diff_quote.before", fallback="Before · Line {line}")
_AFTER = msg("tui.diff_quote.after", fallback="After · Line {line}")


class DiffCommentInput(EnhancedTextArea):
    """Use the chat composer's explicit newline keys and submit bare Enter."""

    class Submitted(Message):
        """Add the current comment to the message input."""

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter" or event.key in NEWLINE_SHORTCUT_KEYS:
            event.stop()
            event.prevent_default()
            if self.read_only:
                return
            if event.key == "enter":
                self.post_message(self.Submitted())
            elif self._replace_via_keyboard("\n", *self.selection):
                self.scroll_cursor_visible()
            return
        await super()._on_key(event)


class DiffCommentBar(BaseDialog[None]):
    """The modal owns positioning and clears the independent highlight on every dismissal path."""

    CSS_PATH = "comment_bar.tcss"
    BINDINGS: ClassVar[list] = [localized_binding("escape", "cancel", CANCEL_BINDING, show=False, priority=True)]

    def __init__(
        self,
        column: CodeColumn,
        index: int,
        quote: DiffQuote,
        draft: DiffDraft,
        record: DiffComment | None,
        comment: str,
        added: Callable[[DraftReference], None],
        saved: Callable[[], None],
    ) -> None:
        super().__init__()
        self._column = column
        self._index = index
        self._quote = quote
        self._draft = draft
        self._record = record
        self._comment = comment
        self._added = added
        self._saved = saved
        self._error = False
        self._column.set_current_comment(index)

    def compose(self) -> ComposeResult:
        with VerticalGroup(id="diff-comment-bar") as bar:
            bar.border_subtitle = Text(render_str(widget_localizer(self), _KEYS.bind()))
            yield DiffCommentInput(
                text=self._comment,
                max_paste_tokens=MESSAGE_EDITOR_PASTE_MAX_TOKENS,
                placeholder=render_str(widget_localizer(self), _PLACEHOLDER.bind()),
                id="diff-comment-input",
            )

    def on_mount(self) -> None:
        if not self._composed_live:
            return
        self.query_one(DiffCommentInput).focus()

    def arrange(self, size: Size, _optimal: bool = False) -> DockArrangeResult:
        result = super().arrange(size, _optimal)
        if not self._composed_live:
            # Opened as the app exits: its children may be pruned before it lays out.
            return result
        bar = self.query_one("#diff-comment-bar", VerticalGroup)
        # The covered screen is arranged first, so this measures the resized column as well.
        area = self._column.scrollable_content_region
        quote = self._quote
        localizer = widget_localizer(self)
        # The same version and line number the quote gives the model; the tab bar already names the turn.
        version, number = quote.line
        suffix = " · " + render_str(localizer, (_BEFORE if version == "before" else _AFTER).bind(line=number))
        path = sanitize_legacy_scalar(surrogate_safe_text(quote.path))
        # A narrow split column still shows the whole version and line: the bar reaches past the column instead,
        # sideways within the diff, never over its frame.
        view = next(ancestor for ancestor in self._column.ancestors if isinstance(ancestor, DiffView))
        bounds = view.region.intersection(Region(0, 0, size.width, size.height))
        needed = 6 + min(cell_len(path), _MIN_PATH_CELLS) + cell_len(suffix)
        width = min(bounds.width, max(area.width, needed))
        y = area.y + self._index - round(self._column.scroll_offset.y)
        height = 5  # Three input rows, plus the two borders.
        below = y + 1
        top = below if below + height <= min(area.bottom, size.height) else y - height
        x = max(bounds.x, min(area.x, bounds.right - width))
        target = Region(x, max(0, min(top, size.height - height)), width, height)
        title = elide_middle(path, max(1, width - 6 - cell_len(suffix))) + suffix
        title = elide_middle(title, max(1, width - 6))  # Only a terminal narrower than the suffix gets here.
        if bar.border_title != title:
            bar.border_title = Text(title)
        if not self._error:
            keys = render_str(localizer, _KEYS.bind())
            if cell_len(keys) > max(1, width - 6):
                keys = render_str(localizer, _KEYS_COMPACT.bind())
            if bar.border_subtitle != keys:
                bar.border_subtitle = Text(keys)
        pin_fixed_placement(result, bar, lambda _arranged: target)
        return result

    def _before_dismiss(self, _result: object | None = None) -> None:
        self._column.set_current_comment(None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(DiffCommentInput.Submitted)
    def _submit(self, event: DiffCommentInput.Submitted) -> None:
        event.stop()
        if self.dismiss_requested or self.app.screen is not self:
            return
        comment = self.query_one(DiffCommentInput).text
        record = self._record
        if record is None:
            handle = self._draft.add_diff_quote(self._quote.content, comment)
            outcome = "unavailable" if handle is None else "success"
            if handle is not None:
                self._added(handle)
        else:
            handle = record.handle()
            outcome = "changed" if handle is None else self._draft.replace_diff_comment(handle, self._comment, comment)
        localizer = widget_localizer(self)
        if outcome != "success":
            self._error = True
            bar = self.query_one("#diff-comment-bar", VerticalGroup)
            bar.add_class("-error")
            bar.border_subtitle = Text(
                render_str(localizer, (COMMENT_CHANGED if outcome == "changed" else QUOTE_INPUT_UNAVAILABLE).bind())
            )
            return
        self._saved()
        if handle is not None:
            self.notify(render_str(localizer, _ADDED.bind(label=handle.label)), markup=False)
        self.dismiss(None)
