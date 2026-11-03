# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""EnhancedTextArea — terminal-friendly TextArea with clipboard + Ctrl+A.

A drop-in replacement for ``textual.widgets.TextArea`` that fixes a
handful of paper-cuts encountered when running inside real terminals:

* ``Ctrl+A`` selects all (Textual's default ``cursor_line_start`` is
  rarely what users want when ``Home`` already does the same job).
* ``Shift+Enter`` inserts a newline through Textual's normal editing path.
* ``Ctrl+Insert`` copies; ``Shift+Insert``, ``Ctrl+Shift+V``, and ``Super+V``
  paste where terminals forward those keys. VSCode Terminal on Windows
  intercepts ``Ctrl+V`` before it reaches the child process, and most Linux
  terminals accept either Ctrl paste variant.
* ``copy`` / ``cut`` / ``paste`` route through both Textual's app-level
  clipboard *and* the cross-platform OS clipboard (``pbcopy`` on macOS,
  Win32 ``SetClipboardData`` on Windows, ``xclip``/``xsel`` on Linux)
  so the selection survives outside the running app.
* Right-click copies the current selection before Textual's default
  mouse-down handling can collapse it.
* When nothing is selected, ``copy`` falls back to the screen-level
  selection (so the user can copy a chat message rendered elsewhere on
  the screen).
* Pasted text has its line endings normalized to ``\\n``; an optional
  ``max_paste_tokens`` knob caps the inserted text by token count, with
  a warning notification on truncation.
* Textual's inherited ``F5`` / ``F6`` / ``F7`` selection bindings are
  disabled because they collide with screen-level F-key shortcuts (the
  main screen uses ``F6`` for the log viewer).
* ``undo`` / ``redo`` are wrapped to swallow the cursor-out-of-bounds
  ``ValueError`` / ``IndexError`` Textual occasionally raises after
  large edits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from textual import events
from textual.actions import SkipAction
from textual.strip import Strip
from textual.widgets import TextArea

from chrys.app.tui.behaviors.right_click_copy import handle_text_area_right_click_copy
from chrys.app.tui.binding_display import COPY_BINDING, PASTE_BINDING, localized_binding
from chrys.app.tui.clipboard import copy_text_to_clipboards, paste_text_from_clipboards
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.foundation.i18n import MessageDef, msg
from chrys.foundation.text.tokenizer import MixedLanguageTokenizer

if TYPE_CHECKING:
    from textual.css.styles import RenderStyles


_PASTE_TOKENIZER: MixedLanguageTokenizer | None = None
"""Lazy-initialized tokenizer shared across all EnhancedTextArea instances."""

MESSAGE_EDITOR_PASTE_MAX_TOKENS = 30_000
"""Maximum tokens accepted by chat and modal message-editor paste events."""

NEWLINE_SHORTCUT_KEYS = frozenset({"ctrl+j", "shift+enter", "ctrl+enter"})
"""Explicit newline shortcuts; each input decides what bare Enter does."""

_PASTE_TRUNCATED_TITLE = msg("tui.editor.title.paste_truncated", fallback="Paste truncated")
_PASTE_TRUNCATED = msg(
    "tui.editor.paste_truncated",
    fallback="Paste truncated: {original_tokens} tokens exceeds the {limit}-token limit.",
)


def _truncate_to_tokens(text: str, max_tokens: int) -> tuple[str, int]:
    """Truncate *text* so its token count is ``<= max_tokens``.

    Returns ``(truncated_text, original_token_count)``.  Uses a
    proportional initial cut based on the token/char ratio and refines
    downwards if the heuristic over-estimates.
    """
    global _PASTE_TOKENIZER
    if _PASTE_TOKENIZER is None:
        _PASTE_TOKENIZER = MixedLanguageTokenizer()
    tokens = _PASTE_TOKENIZER.count_tokens(text)
    if tokens <= max_tokens:
        return text, tokens
    limit = max(1, int(len(text) * (max_tokens / tokens)))
    while limit > 0:
        candidate = text[:limit]
        if _PASTE_TOKENIZER.count_tokens(candidate) <= max_tokens:
            return candidate, tokens
        limit = int(limit * 0.95)
    return "", tokens


class EnhancedTextArea(TextArea):
    """``TextArea`` with terminal-friendly clipboard + Ctrl+A select-all.

    Subclass freely to add app-specific behavior (e.g. Enter-submit,
    history navigation).  Subclasses that override ``_on_key`` should
    call ``super()._on_key(event)`` so the Ctrl+A handler still fires;
    do not copy that pattern to handlers whose base implementation
    leaves Textual's default action enabled.
    """

    BINDINGS: ClassVar[list] = [
        localized_binding("ctrl+insert", "copy", COPY_BINDING, show=False),
        localized_binding("shift+insert", "paste", PASTE_BINDING, show=False),
        localized_binding("ctrl+shift+v", "paste", PASTE_BINDING, show=False),
        localized_binding("super+v", "paste", PASTE_BINDING, show=False),
    ]

    def __init__(
        self,
        *args: Any,
        max_paste_tokens: int | None = None,
        **kwargs: Any,
    ) -> None:
        """Construct an ``EnhancedTextArea``.

        Args:
            max_paste_tokens: When set, paste events whose tokenized
                length exceeds this value are truncated and a warning
                notification is shown.  ``None`` disables the cap.
            *args, **kwargs: Forwarded to ``TextArea``.
        """
        self._max_paste_tokens = max_paste_tokens
        super().__init__(*args, **kwargs)

    def get_component_styles(self, *names: str) -> RenderStyles:
        """Resolve component styles, tolerating Textual's mid-recompose race.

        Textual issue #6208 (closed "won't fix"): when a recompose fires
        during active event processing — e.g. rebuilding a card list from
        inside a ``Button.Pressed`` handler — this ``TextArea`` can be
        rendered before the stylesheet has applied its component styles, so
        a component class the widget *declares* (notably
        ``text-area--gutter``) is briefly absent from ``_component_styles``
        and the base call raises ``KeyError``, crashing the render. Render
        with whatever is materialized this frame; the follow-up refresh
        reapplies the real styles. A name the class never declares is a
        genuine error and still propagates.
        """
        try:
            return super().get_component_styles(*names)
        except KeyError:
            declared = type(self)._get_component_classes()
            if any(name not in declared for name in names):
                raise
            return super().get_component_styles(*(name for name in names if name in self._component_styles))

    def render_line(self, y: int) -> Strip:
        """Render safely while Textual is laying out extremely narrow terminals."""
        if self.content_size.width <= 0:
            return Strip.blank(0, self.visual_style.rich_style)
        return super().render_line(y)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"select_word", "select_line", "select_all"}:
            return False
        return super().check_action(action, parameters)

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "shift+enter":
            event.stop()
            event.prevent_default()
            # Delegate read-only, selection, undo and subclass edit limits to
            # the same path as Enter, without redispatching to modal keymaps.
            await super()._on_key(events.Key("enter", "\r"))
            return
        if event.key == "ctrl+a":
            event.stop()
            event.prevent_default()
            self.select_all()
            return
        await super()._on_key(event)

    def action_undo(self) -> None:
        try:
            super().action_undo()
        except ValueError, IndexError:
            self.move_cursor((0, 0))

    def action_redo(self) -> None:
        try:
            super().action_redo()
        except ValueError, IndexError:
            last_line = self.document.line_count - 1
            last_col = len(self.document.get_line(last_line))
            self.move_cursor((last_line, last_col))

    async def _on_paste(self, event: events.Paste) -> None:
        """Sanitize pasted text and (optionally) cap by token count.

        Calls ``prevent_default`` so Textual's MRO does not also insert
        the unsanitized text.
        """
        event.prevent_default()
        if self.read_only:
            return
        text = self._sanitize_paste_text(event.text)
        # Nothing to paste (a terminal's empty paste with no picture to take) changes nothing, the selection included.
        if text and (result := self._replace_via_keyboard(text, *self.selection)):
            self.move_cursor(result.end_location)
        event.stop()

    def _sanitize_paste_text(self, text: str) -> str:
        """Normalize pasted text and apply the optional token cap."""
        return self._cap_to_paste_tokens(text.replace("\r\n", "\n").replace("\r", "\n"))

    def _cap_to_paste_tokens(
        self, text: str, *, notice: MessageDef = _PASTE_TRUNCATED, title: MessageDef = _PASTE_TRUNCATED_TITLE
    ) -> str:
        """*text* cut to the optional paste token cap, saying so with *notice* when it was cut."""
        if self._max_paste_tokens is None:
            return text
        text, original_tokens = _truncate_to_tokens(text, self._max_paste_tokens)
        if original_tokens > self._max_paste_tokens:
            localizer = widget_localizer(self)
            self.notify(
                render_str(
                    localizer,
                    notice.bind(original_tokens=f"{original_tokens:,}", limit=f"{self._max_paste_tokens:,}"),
                ),
                title=render_str(localizer, title.bind()),
                severity="warning",
                markup=False,
            )
        return text

    def action_copy(self) -> None:
        """Copy selection to system + app clipboard.

        Falls through to the screen-level selection (e.g. text rendered
        in chat output) when nothing is selected inside this widget.
        """
        selected = self.selected_text
        if not selected:
            selected = self.screen.get_selected_text()
        if not selected:
            # Match stock TextArea semantics: with nothing to copy anywhere,
            # defer the key to the next namespace (ultimately the app-level
            # ctrl+c quit hint).
            raise SkipAction
        copy_text_to_clipboards(self.app, selected)

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        if handle_text_area_right_click_copy(self, event):
            # The helper calls prevent_default(), so Textual's MRO dispatch
            # will not continue into TextArea._on_mouse_down for right-click.
            return
        # Let Textual's MRO dispatch run TextArea._on_mouse_down once; a
        # same-name super() here would run the base mouse handler twice.

    def action_cut(self) -> None:
        if self.read_only:
            return
        selected = self.selected_text
        if not selected:
            return
        start, end = self.selection
        self._delete_via_keyboard(start, end)
        copy_text_to_clipboards(self.app, selected)

    def action_paste(self) -> None:
        """Paste from the freshest clipboard available to this frontend."""
        if self.read_only:
            return
        text = paste_text_from_clipboards(self.app)
        text = self._sanitize_paste_text(text)
        if text and (result := self._replace_via_keyboard(text, *self.selection)):
            self.move_cursor(result.end_location)
