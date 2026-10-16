# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""InputBar widget — multi-line input area with Send/Stop button.

Enter submits. Ctrl+J, Shift+Enter and Ctrl+Enter insert a newline.
Cross-platform system clipboard support (macOS pbcopy/pbpaste,
Win32 ``SetClipboardData``/``GetClipboardData`` on Windows,
xclip/xsel on Linux).
When the agent is running, the button becomes Stop to interrupt.
"""

from __future__ import annotations

import asyncio
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from secrets import token_hex
from time import time
from typing import TYPE_CHECKING, Any, Literal

from rich.text import Text
from textual.containers import Horizontal
from textual.content import Content
from textual.css.scalar import Scalar
from textual.geometry import Spacing
from textual.message import Message
from textual.reactive import reactive
from textual.strip import Strip
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Button, TextArea

from chrys.app.tui.i18n import render_content, render_str
from chrys.app.tui.support.prompt_history import DEFAULT_MAX_ENTRIES, PromptHistoryWriter, append_history, load_history
from chrys.app.tui.util.spinner import SPINNER_FRAMES
from chrys.app.tui.util.visibility import resync_compositor_regions
from chrys.app.tui.widgets import EnhancedTextArea
from chrys.app.tui.widgets.chrome.commands import is_slash_command_candidate
from chrys.app.tui.widgets.chrome.image_paste import (
    convert_pasted_image_paths_to_mentions,
    save_clipboard_image_to_file,
)
from chrys.app.tui.widgets.selection import NonSelectableTextMixin
from chrys.app.tui.widgets.text_area import MESSAGE_EDITOR_PASTE_MAX_TOKENS, NEWLINE_SHORTCUT_KEYS
from chrys.foundation.i18n import Localizer, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.platform import safe_getcwd

# Unicode-name markers for CJK-family scripts where a preceding character cannot be an email local part.
_CJK_NAME_TOKENS = ("CJK", "HIRAGANA", "KATAKANA", "HANGUL", "BOPOMOFO", "IDEOGRAPH")
INPUT_SEND = msg("tui.input.send", fallback="Send")
INPUT_NEW = msg("tui.input.new", fallback="New")
INPUT_NEW_DESCRIPTION = msg("tui.input.new_description", fallback="Create a new session")
INPUT_QUEUED = msg("tui.input.queued", fallback="Queued")
INPUT_INTERRUPT = msg("tui.input.interrupt", fallback="Interrupt")
INPUT_CONTINUE = msg("tui.input.continue", fallback="Continue")
INPUT_RETRY = msg("tui.input.retry", fallback="Retry")
_INPUT_PROMPT_PLACEHOLDER = msg(
    "tui.input.placeholder.prompt",
    fallback="Type a message ({shortcut} for Editor)",
)
_INPUT_INJECT_PLACEHOLDER = msg("tui.input.placeholder.inject", fallback="Inject a message")
_INPUT_PLACEHOLDER_AGENTS = msg("tui.input.placeholder.agents", fallback="Agents")
_INPUT_PLACEHOLDER_MODELS = msg("tui.input.placeholder.models", fallback="Models")
_INPUT_PLACEHOLDER_SHELL = msg("tui.input.placeholder.shell", fallback="Shell")
_INPUT_PLACEHOLDER_COMMANDS = msg("tui.input.placeholder.commands", fallback="Commands")
_INPUT_PLACEHOLDER_FILES = msg("tui.input.placeholder.files", fallback="Files")
_INPUT_NEWLINE_HINT = msg("tui.input.hint.newline", fallback="{shortcut} for newline")
_INPUT_HISTORY_SEARCH_PLACEHOLDER = msg("tui.input.placeholder.history_search", fallback="Search prompt history")
_INPUT_PLACEHOLDER_HISTORY_SELECT = msg("tui.input.placeholder.history_select", fallback="Select")
_INPUT_PLACEHOLDER_HISTORY_CLOSE = msg("tui.input.placeholder.history_close", fallback="Close")

type InputLabel = MessageRef | str


def _is_cjk_character(char: str) -> bool:
    name = unicodedata.name(char, "")
    return any(token in name for token in _CJK_NAME_TOKENS)


def _is_file_trigger_boundary(text: str, at_pos: int) -> bool:
    """Return whether an ``@`` at *at_pos* should open file suggestions."""
    if at_pos == 0:
        return True
    previous = text[at_pos - 1]
    return previous.isspace() or _is_cjk_character(previous)


def _placeholder_fragment(localizer: Localizer | None, reference: MessageRef) -> Content:
    if localizer is None:
        return Content.from_text(format_message(reference), markup=False)
    return render_content(localizer, reference)


def _build_prompt_placeholder(localizer: Localizer | None = None) -> Content:
    """Build the default placeholder with shortcut hints (reverse-video pills)."""
    return Content.assemble(
        _placeholder_fragment(localizer, _INPUT_PROMPT_PLACEHOLDER.bind(shortcut="Ctrl+O")),
        "  ",
        (" # ", "r"),
        " ",
        _placeholder_fragment(localizer, _INPUT_PLACEHOLDER_AGENTS.bind()),
        " ",
        (" $ ", "r"),
        " ",
        _placeholder_fragment(localizer, _INPUT_PLACEHOLDER_MODELS.bind()),
        " ",
        (" / ", "r"),
        " ",
        _placeholder_fragment(localizer, _INPUT_PLACEHOLDER_COMMANDS.bind()),
        " ",
        (" @ ", "r"),
        " ",
        _placeholder_fragment(localizer, _INPUT_PLACEHOLDER_FILES.bind()),
        " ",
        (" ! ", "r"),
        " ",
        _placeholder_fragment(localizer, _INPUT_PLACEHOLDER_SHELL.bind()),
    )


def _build_inject_placeholder(localizer: Localizer | None = None) -> Content:
    """Build placeholder for when agent is running (inject + commands/files)."""
    return Content.assemble(
        _placeholder_fragment(localizer, _INPUT_INJECT_PLACEHOLDER.bind()),
        " ",
        (" / ", "r"),
        " ",
        _placeholder_fragment(localizer, _INPUT_PLACEHOLDER_COMMANDS.bind()),
        " ",
        (" @ ", "r"),
        " ",
        _placeholder_fragment(localizer, _INPUT_PLACEHOLDER_FILES.bind()),
    )


def _build_history_search_placeholder(localizer: Localizer | None = None) -> Content:
    """Build the placeholder shown while the Ctrl+R prompt-history search is open."""
    return Content.assemble(
        _placeholder_fragment(localizer, _INPUT_HISTORY_SEARCH_PLACEHOLDER.bind()),
        "  ",
        (" Enter ", "r"),
        " ",
        _placeholder_fragment(localizer, _INPUT_PLACEHOLDER_HISTORY_SELECT.bind()),
        " ",
        (" Esc ", "r"),
        " ",
        _placeholder_fragment(localizer, _INPUT_PLACEHOLDER_HISTORY_CLOSE.bind()),
    )


if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.app.tui.i18n import LocaleController


_HISTORY_PREVIOUS_KEYS = frozenset({"up"})
_HISTORY_NEXT_KEYS = frozenset({"down"})
_BUTTON_GROUP_HORIZONTAL_PADDING = 2


@dataclass(frozen=True, slots=True)
class InputDraftSnapshot:
    """Immutable chat draft captured before opening the message editor."""

    text: str
    cursor_location: tuple[int, int]
    revision: int


@dataclass
class _HistoryBrowser:
    """Mutable prompt-history browsing state for one history scope."""

    entries: list[str] = field(default_factory=list)
    index: int = -1
    max_entries: int = DEFAULT_MAX_ENTRIES
    storage_loaded: bool = False

    def reset(self, entries: list[str] | None = None) -> None:
        if entries is not None:
            self.entries = list(entries)
        self.index = -1

    def append(self, text: str) -> None:
        text = text.strip()
        if text and (not self.entries or self.entries[-1] != text):
            self.entries.append(text)
            overflow = len(self.entries) - self.max_entries
            if overflow > 0:
                del self.entries[:overflow]
                if self.index != -1:
                    self.index = max(0, self.index - overflow)


class _ChatTextArea(EnhancedTextArea):
    """Chat input with Enter-submit, explicit newline shortcuts and input history."""

    _history: _HistoryBrowser
    _history_browsing: bool
    _history_draft: str | None
    _instance_id: str
    _right_placeholder: str

    class Submitted(Message):
        """Posted when the user presses Enter."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    class ShellModeRequested(Message):
        """Posted when user types ``!`` at position (0, 0) with empty text."""

    class EditorRequested(Message):
        """Posted when the user requests the full-screen message editor."""

    class ShellModeCancelled(Message):
        """Posted when user exits shell mode (Escape or backspace-to-empty)."""

    class SlashTrigger(Message):
        """Posted when user types ``/`` at start of empty input."""

    class FileTrigger(Message):
        """Posted when user types ``@``."""

    class AgentTrigger(Message):
        """Posted when user types ``#`` at start of empty input."""

    class ModelTrigger(Message):
        """Posted when user types ``$`` at start of empty input."""

    def __init__(self, *args, **kwargs) -> None:
        kwargs.setdefault("max_paste_tokens", MESSAGE_EDITOR_PASTE_MAX_TOKENS)
        super().__init__(*args, **kwargs)
        self._history = _HistoryBrowser()
        self._history_browsing = False
        self._history_draft: str | None = None
        self._instance_id = token_hex(8)
        self._history_writer = PromptHistoryWriter(append_history)
        self._suggestions_active: bool = False
        # Prompt-history search types !, /, @, # and $ as query text instead of entering shell mode
        # or opening their popups; Esc closes the search first.
        self._history_search_active: bool = False
        self._right_placeholder = ""
        # Session restore/workspace updates replace this before normal use; this covers early paste during startup.
        self._paste_cwd: str = safe_getcwd()
        self._clipboard_image_dir: Path | None = None

    def set_right_placeholder(self, text: str) -> None:
        """Set literal placeholder text rendered against the input's right edge."""
        if text == self._right_placeholder:
            return
        self._right_placeholder = text
        self.refresh()

    def render_line(self, y: int) -> Strip:
        """Render the optional right placeholder without adding a layout widget."""
        line = super().render_line(y)
        if self.text or y != 0 or not self.placeholder or not self._right_placeholder:
            return line

        hint = Content.from_text(self._right_placeholder, markup=False)
        right_padding = 1
        gap = self.content_size.width - line.cell_length - hint.cell_length - right_padding
        if gap < 1:
            return line

        placeholder_style = self.get_visual_style("text-area--placeholder")
        styled_hint = hint.stylize(placeholder_style)
        hint_strip = Strip(
            styled_hint.render_segments(self.visual_style),
            styled_hint.cell_length,
        )
        return Strip.join(
            (
                line,
                Strip.blank(gap, self.rich_style),
                hint_strip,
                Strip.blank(right_padding, self.rich_style),
            )
        )

    def _set_text(self, text: str) -> None:
        """Replace textarea content with *text* and move cursor to end."""
        self.clear()
        if text:
            self.insert(text)

    async def _refresh_history_entries(self) -> None:
        browser = self._history
        if browser.entries or browser.storage_loaded:
            # Appends reach memory before their queued disk writes.  Once this
            # instance has entries, disk can only be an older prefix and must
            # not replace the authoritative in-memory list.
            return
        refreshed_entries = await asyncio.to_thread(
            load_history,
            max_entries=browser.max_entries,
            instance_id=self._instance_id,
        )
        browser.storage_loaded = True
        if not browser.entries:
            browser.entries = list(refreshed_entries)

    def _capture_visible_draft(self) -> None:
        if not self._history_browsing:
            self._history_draft = self.text

    async def _browse_history(self, direction: Literal[-1, 1]) -> bool:
        """Move within instance history, returning True when the key was handled."""
        browser = self._history

        # First entry into browsing mode saves the visible draft.
        self._capture_visible_draft()

        self._history_browsing = True

        if browser.index == -1:
            await self._refresh_history_entries()
            if not browser.entries:
                self._history_browsing = False
                return False
            browser.index = len(browser.entries) - 1
            self._set_text(browser.entries[browser.index])
            return True

        if direction < 0:
            if browser.index <= 0:
                self._set_text(browser.entries[browser.index])
                return True
            browser.index -= 1
            self._set_text(browser.entries[browser.index])
            return True

        if browser.index < len(browser.entries) - 1:
            browser.index += 1
            self._set_text(browser.entries[browser.index])
            return True

        # Past the last entry — restore the saved draft and exit browsing.
        browser.index = -1
        self._history_browsing = False
        self._set_text(self._history_draft or "")
        return True

    def _reset_history_browsing(self) -> None:
        self._history_browsing = False
        self._history_draft = None
        self._history.reset()

    def _prepare_paste_text(self, text: str) -> str:
        """Convert pasted/dropped supported image paths into ``@`` mentions."""
        try:
            return self._prepare_image_path_paste_text(text) or text
        except OSError, RuntimeError, ValueError:
            return text

    def _prepare_image_path_paste_text(self, text: str) -> str | None:
        """Return converted image-path paste text, or ``None`` when not applicable."""
        converted = convert_pasted_image_paths_to_mentions(text, cwd=Path(self._paste_cwd))
        if converted == text:
            return None
        start, _end = self.selection
        insert_index = self.document.get_index_from_location(start)
        if insert_index > 0 and not self.text[insert_index - 1].isspace():
            return f" {converted}"
        return converted

    def set_paste_cwd(self, cwd: str) -> None:
        """Set the cwd used to resolve relative pasted/dropped image paths."""
        self._paste_cwd = cwd

    def set_clipboard_image_dir(self, directory: str | Path | None) -> None:
        """Set the session-scoped directory used for clipboard image attachments."""
        self._clipboard_image_dir = Path(directory) if directory is not None else None

    def _prepare_clipboard_image_paste_text(self) -> str | None:
        """Return paste text for a bitmap image currently on the platform clipboard."""
        # TODO(perf): Clipboard capture may run osascript/PIL/file I/O. Move
        # this boundary to a worker when paste completion can remain ordered
        # with subsequent input events.
        path = save_clipboard_image_to_file(self._clipboard_image_dir)
        if path is None:
            return None
        try:
            return self._prepare_image_path_paste_text(str(path)) or str(path)
        except OSError, RuntimeError, ValueError:
            return str(path)

    def _insert_paste_text(self, text: str) -> bool:
        text = self._sanitize_paste_text(text)
        if result := self._replace_via_keyboard(text, *self.selection):
            self.move_cursor(result.end_location)
            return True
        return False

    async def _on_paste(self, event) -> None:
        """Handle text paste events and terminal image-clipboard no-ops."""
        if not event.text:
            event.prevent_default()
            if not self.read_only:
                self._insert_clipboard_image_paste()
            event.stop()
            return
        await super()._on_paste(event)

    def _insert_clipboard_image_paste(self) -> bool:
        text = self._prepare_clipboard_image_paste_text()
        if not text:
            return False
        return self._insert_paste_text(text)

    def action_paste(self) -> None:
        """Paste text or a platform clipboard image into the chat input."""
        if self.read_only:
            return
        if not self._insert_clipboard_image_paste():
            super().action_paste()

    async def _on_key(self, event) -> None:
        # Prompt history navigation: up on the first row shown, down on the last.
        # Rows, not lines: a long line wrapped onto several rows is still one
        # line, and the arrows move between its rows first.
        # Skip history when suggestion list is active — arrows navigate suggestions instead
        if (
            event.key in _HISTORY_PREVIOUS_KEYS
            and self.navigator.is_first_wrapped_line(self.cursor_location)
            and not self._suggestions_active
            and await self._browse_history(-1)
        ):
            event.stop()
            event.prevent_default()
            return
        if (
            event.key in _HISTORY_NEXT_KEYS
            and self._history.index != -1
            and not self._suggestions_active
            and self.navigator.is_last_wrapped_line(self.cursor_location)
        ):
            if await self._browse_history(1):
                event.stop()
                event.prevent_default()
            return
        if event.key == "ctrl+o":
            event.stop()
            event.prevent_default()
            self.post_message(self.EditorRequested())
            return
        # Shell mode trigger: "!" or fullwidth "!" (U+FF01) at start of empty input
        if (
            event.character in ("!", "\uff01")
            and not self.text
            and self.cursor_location == (0, 0)
            and not self._history_search_active
        ):
            event.stop()
            event.prevent_default()
            self.post_message(self.ShellModeRequested())
            return
        if event.key == "escape":
            self.post_message(self.ShellModeCancelled())
        if event.key in NEWLINE_SHORTCUT_KEYS:
            # Insert newline
            event.stop()
            event.prevent_default()
            start, end = self.selection
            if self._replace_via_keyboard("\n", start, end):
                self.scroll_cursor_visible()
            return
        if event.key == "enter":
            # Submit (suppressed when read_only / locked)
            event.stop()
            event.prevent_default()
            if self.read_only:
                return
            text = self.text.strip()
            if text or self._suggestions_active:
                self._reset_history_browsing()
                self.post_message(self.Submitted(text))
            return
        await super()._on_key(event)
        if self._history_search_active:
            return
        # After character insertion, check for /, @, #, $ triggers.
        # Accept fullwidth variants (U+FF0F, U+FF20, U+FF03, U+FF04) for CJK keyboards.
        if event.character in ("/", "\uff0f") and self.text in ("/", "\uff0f") and self.cursor_location == (0, 1):
            self.post_message(self.SlashTrigger())
        elif event.character in ("@", "\uff20") and (self.text.endswith("@") or self.text.endswith("\uff20")):
            # Trigger after whitespace/start/CJK: "@", "some text @", or "你好@".
            txt = self.text
            at_pos = len(txt) - 1
            if _is_file_trigger_boundary(txt, at_pos):
                self.post_message(self.FileTrigger())
        elif event.character in ("#", "\uff03") and self.text in ("#", "\uff03") and self.cursor_location == (0, 1):
            self.post_message(self.AgentTrigger())
        elif event.character in ("$", "\uff04") and self.text in ("$", "\uff04") and self.cursor_location == (0, 1):
            self.post_message(self.ModelTrigger())

    def add_to_history(self, text: str, *, session_id: str | None = None) -> None:
        """Append *text* to input history (deduplicates consecutive)."""
        self._history.append(text)
        self._history_writer.enqueue(
            text,
            session_id=session_id,
            instance_id=self._instance_id,
            cwd=self._paste_cwd,
        )

    async def on_unmount(self) -> None:
        """Best-effort drain persisted prompt history during TUI shutdown."""
        await self._history_writer.close()

    async def load_prompt_history(self, *, max_entries: int) -> list[str]:
        """Load global MRU entries after making this instance's queued writes visible."""
        await self._history_writer.flush()
        return await asyncio.to_thread(load_history, max_entries=max_entries)


class _InputBarButton(NonSelectableTextMixin, Button):
    """Input action button excluded from terminal drag selection and copy."""


class InputBar(Widget):
    """Multi-line input bar with Send/Stop button.

    - Enter or clicking Send submits the message.
    - Ctrl+J, Shift+Enter and Ctrl+Enter insert a newline for multi-line input.
    - When agent is running, button becomes Stop (fires ``InterruptRequested``).
    """

    DEFAULT_CSS = """
    InputBar {
        height: auto;
        max-height: 9;
        padding: 0;
    }
    InputBar > Horizontal {
        height: auto;
        max-height: 9;
        border: round $tui-border-accent $border-opacity;
    }
    InputBar > Horizontal > #editor-btn {
        width: 3;
        min-width: 3;
        height: 1;
        border: none;
        padding: 0;
        content-align: center middle;
        background: transparent;
        color: $accent;
        text-style: bold;
    }
    InputBar > Horizontal > #editor-btn:hover,
    InputBar > Horizontal > #editor-btn:focus,
    InputBar > Horizontal > #editor-btn.-active {
        background: $foreground;
        color: $background;
    }
    InputBar > Horizontal > _ChatTextArea {
        width: 1fr;
        min-width: 4;
        height: auto;
        max-height: 7;
        background: $foreground 4%;
        padding: 0 0 0 1;
        scrollbar-background: $foreground 12%;
        scrollbar-background-hover: $foreground 16%;
        scrollbar-background-active: $foreground 20%;
    }
    InputBar > Horizontal > #btn-group {
        width: auto;
        height: 1;
        align: left middle;
        padding: 0 1;
    }
    InputBar > Horizontal > #btn-group > Button {
        height: 1;
        border: none;
        padding: 0 1;
        background: $accent;
        color: $button-color-foreground;
    }
    InputBar > Horizontal > #btn-group > #send-btn {
        width: auto;
        min-width: 0;
    }
    InputBar > Horizontal > #btn-group > Button:hover,
    InputBar > Horizontal > #btn-group > Button:focus,
    InputBar > Horizontal > #btn-group > Button.-active {
        background: $foreground;
        color: $background;
    }
    InputBar > Horizontal > #btn-group > #send-btn.-stop {
        background: $error;
    }
    InputBar > Horizontal > #btn-group > #new-btn {
        visibility: hidden;
        width: 0;
        min-width: 0;
        margin: 0;
    }
    InputBar.-locked > Horizontal > #editor-btn {
        color: $text-muted;
    }
    InputBar.-locked > Horizontal > _ChatTextArea {
        color: $text-muted;
    }
    """

    agent_running: reactive[bool] = reactive(False)
    agent_loading: reactive[bool] = reactive(False)
    locked: reactive[bool] = reactive(False)
    has_messages: reactive[bool] = reactive(False)
    shell_mode: reactive[bool] = reactive(False)
    # ``_retry_label`` is a semantic plain attribute, so label-only transitions
    # (e.g. Retry -> Continue while retry_mode stays True) reach the interaction
    # refresh via the always-firing watcher; the refresh itself dedupes.
    retry_mode: reactive[bool] = reactive(False, always_update=True)
    retry_pending: reactive[bool] = reactive(False)
    _retry_label: InputLabel

    class UserSubmitted(Message):
        """Posted when the user submits text."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    class InterruptRequested(Message):
        """Posted when the user clicks Stop."""

    class RetryRequested(Message):
        """Posted when the user clicks Retry / Continue.

        Carries the current input-bar text (may be empty).  When
        non-empty, the engine uses it as the mid-turn continuation
        prompt instead of the default ``"continue"`` placeholder.
        """

        def __init__(self, text: str = "") -> None:
            self.text = text
            super().__init__()

    class ShellModeRequested(Message):
        """Posted when the user requests shell mode from the input."""

    class EditorRequested(Message):
        """Posted when the user requests the full-screen message editor."""

    class NewSessionRequested(Message):
        """Posted when the user clicks New."""

    class SlashTriggered(Message):
        """Posted when ``/`` is typed at start of empty input."""

    class FileTriggered(Message):
        """Posted when ``@`` is typed at a file-mention boundary."""

    class AgentTriggered(Message):
        """Posted when ``#`` is typed at start of empty input."""

    class ModelTriggered(Message):
        """Posted when ``$`` is typed at start of empty input."""

    class SuggestionNavigate(Message):
        """Arrow key pressed while suggestions are active."""

        def __init__(self, direction: str) -> None:
            self.direction = direction
            super().__init__()

    class SuggestionSelect(Message):
        """Tab or Enter pressed while suggestions are active."""

        def __init__(self, execute: bool = False) -> None:
            self.execute = execute
            super().__init__()

    class SuggestionDismiss(Message):
        """Escape pressed while suggestions are active."""

    class TextChanged(Message):
        """Text changed while suggestions are active."""

        def __init__(self, text: str) -> None:
            self.text = text
            super().__init__()

    class LockedChanged(Message):
        """Posted when queued-injection locking changes."""

        def __init__(self, locked: bool) -> None:
            self.locked = locked
            super().__init__()

    def __init__(
        self,
        placeholder: str | Content | None = None,
        *,
        locale_controller: LocaleController | None = None,
        **kwargs: Any,
    ) -> None:
        self._draft_revision = 0
        self._locale_controller = locale_controller
        self._uses_default_placeholder = placeholder is None
        self._placeholder = placeholder if placeholder is not None else _build_prompt_placeholder(self._localizer())
        self._suggestions_active: bool = False
        # Session restore/workspace updates replace this before normal use; this covers early paste during startup.
        self._paste_cwd: str = safe_getcwd()
        self._clipboard_image_dir: Path | None = None
        self._spin_timer: Timer | None = None
        self._lock_label: InputLabel = ""
        self._lock_stop_style: bool = False
        self._lock_spin_prompt: bool = False
        self._interaction_signature: tuple[object, ...] | None = None
        self._button_geometry_dirty = False
        self._retry_label = INPUT_CONTINUE.bind()
        self._inject_placeholder = _build_inject_placeholder(self._localizer())
        self._history_search_placeholder = _build_history_search_placeholder(self._localizer())
        super().__init__(**kwargs)

    def set_suggestions_active(self, active: bool, mode: str = "") -> None:
        """Set suggestion-active state on both InputBar and _ChatTextArea."""
        self._suggestions_active = active
        self._suggestion_mode = mode
        text_area = self.query_one("#chat-input", _ChatTextArea)
        text_area._suggestions_active = active
        text_area._history_search_active = active and mode == "history"
        self._apply_placeholders(text_area)

    def _apply_placeholders(self, text_area: _ChatTextArea) -> None:
        """Show the placeholder and right-edge hint for the current input mode.

        Both setters skip unchanged values, so a repeated call repaints nothing.
        """
        if text_area._history_search_active:
            text_area.placeholder = self._history_search_placeholder
            text_area.set_right_placeholder("")
            return
        text_area.placeholder = self._inject_placeholder if self.agent_running else self._placeholder
        text_area.set_right_placeholder(self._render_newline_hint())

    @property
    def suggestions_active(self) -> bool:
        """Whether the completion suggestion list is currently using input keys."""
        return self._suggestions_active

    def set_paste_cwd(self, cwd: str) -> None:
        """Set the cwd used to resolve relative pasted/dropped image paths."""
        self._paste_cwd = cwd
        if self.is_mounted:
            self.query_one("#chat-input", _ChatTextArea).set_paste_cwd(cwd)

    def set_clipboard_image_dir(self, directory: str | Path | None) -> None:
        """Set the session-scoped directory used for clipboard image attachments."""
        self._clipboard_image_dir = Path(directory) if directory is not None else None
        if self.is_mounted:
            self.query_one("#chat-input", _ChatTextArea).set_clipboard_image_dir(self._clipboard_image_dir)

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield _InputBarButton(Text(">"), id="editor-btn")
            yield _ChatTextArea(
                id="chat-input",
                compact=True,
                soft_wrap=True,
                show_line_numbers=False,
            )
            with Horizontal(id="btn-group"):
                yield _InputBarButton(Text(self._render_label(INPUT_SEND.bind())), id="send-btn")
                yield _InputBarButton(
                    Text(self._render_label(INPUT_NEW.bind())),
                    id="new-btn",
                    variant="primary",
                    flat=True,
                    tooltip=Text(self._render_label(INPUT_NEW_DESCRIPTION.bind())),
                )

    def on_mount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)
        ta = self.query_one("#chat-input", _ChatTextArea)
        ta.set_paste_cwd(self._paste_cwd)
        ta.set_clipboard_image_dir(self._clipboard_image_dir)
        self._apply_placeholders(ta)
        self.focus_input()
        # Pin the group to the current visible button widths. Subsequent state
        # changes remap only this fixed InputBar row, never MainScreen layout.
        self._set_btn_label(self._render_label(INPUT_SEND.bind()), defer_geometry=True)
        self._refresh_interaction_state()

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)

    def refresh_localization(self) -> None:
        """Retranslate placeholders and semantic button state in place."""
        if self._uses_default_placeholder:
            self._placeholder = _build_prompt_placeholder(self._localizer())
        self._inject_placeholder = _build_inject_placeholder(self._localizer())
        self._history_search_placeholder = _build_history_search_placeholder(self._localizer())
        self._interaction_signature = None
        if not self.is_mounted:
            return
        self._apply_placeholders(self.query_one("#chat-input", _ChatTextArea))
        self._set_btn_label(
            self._render_label(INPUT_NEW.bind()),
            button_id="new-btn",
            defer_geometry=True,
        )
        self.query_one("#new-btn", Button).tooltip = Text(self._render_label(INPUT_NEW_DESCRIPTION.bind()))
        self._refresh_interaction_state()

    def _localizer(self) -> Localizer | None:
        controller = self._locale_controller
        if controller is None:
            return None
        return controller.localizer

    def _render_label(self, label: InputLabel) -> str:
        if isinstance(label, str):
            return label
        localizer = self._localizer()
        if localizer is None:
            return format_message(label)
        return render_str(localizer, label)

    def _render_newline_hint(self) -> str:
        return self._render_label(_INPUT_NEWLINE_HINT.bind(shortcut="Ctrl+J"))

    def _update_new_btn_visibility(self) -> None:
        """Show New when Chat has history and execution is idle."""
        btn = self.query_one("#new-btn", Button)
        # Admission can outlive an interrupted run's unwind. Reflect the
        # navigation guard instead of leaving a seemingly actionable button.
        btn.disabled = self.retry_pending
        visible = self.has_messages and not self.retry_mode and not self.agent_running and not self.agent_loading
        width = Scalar.from_number(Text(str(btn.label)).cell_len + 4 if visible else 0)
        changed = btn.styles.set_rule("width", width)
        changed = btn.styles.set_rule("min_width", width) or changed
        changed = btn.styles.set_rule("margin", Spacing(0, 0, 0, 1 if visible else 0)) or changed
        changed = btn.styles.set_rule("padding", Spacing(0, 1, 0, 1) if visible else Spacing.all(0)) or changed
        changed = btn.styles.set_rule("visibility", "visible" if visible else "hidden") or changed
        if not visible and btn.has_focus:
            btn.blur()
        if changed:
            self._mark_button_geometry_dirty()
        self._flush_button_geometry()

    def watch_agent_running(self, running: bool) -> None:
        if running:
            self.retry_mode = False
        self._refresh_interaction_state()

    def watch_agent_loading(self, loading: bool) -> None:
        self._refresh_interaction_state()

    def watch_locked(self, locked: bool) -> None:
        self.post_message(self.LockedChanged(locked))
        self._refresh_interaction_state()

    def watch_retry_mode(self, retry: bool) -> None:
        self._refresh_interaction_state()

    def watch_retry_pending(self, pending: bool) -> None:
        self._refresh_interaction_state()

    def _set_btn_label(
        self,
        label: str,
        *,
        button_id: str = "send-btn",
        defer_geometry: bool = False,
    ) -> None:
        """Set a cell-aware natural label width without escalating screen layout."""
        btn = self.query_one(f"#{button_id}", Button)
        if str(btn.label) != label:
            btn.label = Text(label)
        changed = False
        if button_id == "send-btn":
            width = Scalar.from_number(Text(label).cell_len + 4)
            changed = btn.styles.set_rule("width", width)
            changed = btn.styles.set_rule("min_width", width) or changed
        else:
            changed = True
        if changed:
            self._mark_button_geometry_dirty()
        if not defer_geometry:
            self._flush_button_geometry()

    def _mark_button_geometry_dirty(self) -> None:
        """Invalidate only the InputBar row affected by button geometry."""
        self._button_geometry_dirty = True
        if self.is_mounted:
            group = self.query_one("#btn-group", Horizontal)
            self._pin_button_group_width(group)
            group._clear_arrangement_cache()
            row = group.parent
            if isinstance(row, Widget):
                row._clear_arrangement_cache()

    def _flush_button_geometry(self) -> None:
        """Apply cache-local geometry, deferring while an overlay is active."""
        if not self._button_geometry_dirty or not self.is_mounted or self.app.screen is not self.screen:
            return
        group = self.query_one("#btn-group", Horizontal)
        group._clear_arrangement_cache()
        row = group.parent
        if not isinstance(row, Widget):
            return
        row._clear_arrangement_cache()
        resync_compositor_regions(row)
        self._button_geometry_dirty = False

    def _pin_button_group_width(self, group: Horizontal) -> None:
        """Content-size the group from its currently visible buttons."""
        send = self.query_one("#send-btn", Button)
        new = self.query_one("#new-btn", Button)
        send_width = Text(str(send.label)).cell_len + 4
        new_visible = new.styles.visibility == "visible"
        new_width = Text(str(new.label)).cell_len + 4 if new_visible else 0
        inter_button_margin = 1 if new_visible else 0
        width = send_width + new_width + inter_button_margin + _BUTTON_GROUP_HORIZONTAL_PADDING
        group.styles.set_rule("width", Scalar.from_number(width))

    def sync_deferred_button_geometry(self) -> None:
        """Apply geometry deferred while an overlay covered the owning screen."""
        self._flush_button_geometry()

    def _set_prompt_spinner(self, active: bool) -> None:
        if active:
            if self._spin_timer is None:
                self._spin_timer = self.set_interval(0.08, self._spin_prompt)
            return
        if self._spin_timer is not None:
            self._spin_timer.stop()
            self._spin_timer = None
        self.query_one("#editor-btn", Button).label = Text(">")

    def _request_editor(self) -> None:
        """Request the prompt editor through the shared keyboard/button path."""
        if self.locked or self.shell_mode:
            return
        # A mouse press focuses the prompt button before this handler runs.
        # Restore the chat input first so closing the editor returns focus and
        # its cursor to the draft instead of leaving focus on the button.
        self.focus_input()
        if self._suggestions_active:
            self.post_message(self.SuggestionDismiss())
        self.post_message(self.EditorRequested())

    def _refresh_interaction_state(self) -> None:
        """Derive the complete input/button state from the current mode flags."""
        ta = self.query_one("#chat-input", _ChatTextArea)
        btn = self.query_one("#send-btn", Button)
        self.query_one("#editor-btn", Button).disabled = self.locked or self.shell_mode

        signature = (
            self.locked,
            self._lock_label,
            self._lock_stop_style,
            self._lock_spin_prompt,
            self.agent_running,
            self.agent_loading,
            self.retry_mode,
            self.retry_pending,
            self._retry_label,
            bool(ta.text.strip()),
            self.has_messages,
            self.shell_mode,
        )
        if signature == self._interaction_signature:
            return
        self._interaction_signature = signature

        # A hard lock (queued injection) takes precedence over startup
        # loading if both flags are ever set.
        if self.locked:
            self.add_class("-locked")
            # ``read_only`` blocks edits; ``disabled`` also suppresses cursor
            # movement and history navigation while the queued injection is pending.
            ta.read_only = True
            ta.disabled = True
            self._set_btn_label(self._render_label(self._lock_label or INPUT_QUEUED.bind()), defer_geometry=True)
            btn.set_class(self._lock_stop_style, "-stop")
            btn.disabled = True
            self._set_prompt_spinner(self._lock_spin_prompt)
            self._update_new_btn_visibility()
            return

        self.remove_class("-locked")
        ta.disabled = False
        ta.read_only = False
        ta.show_cursor = True
        self._set_prompt_spinner(False)

        if self.agent_running:
            self._set_btn_label(self._render_label(INPUT_INTERRUPT.bind()), defer_geometry=True)
            btn.add_class("-stop")
            btn.disabled = False
        else:
            btn.remove_class("-stop")
            retry = self.retry_mode
            label = self._retry_label if retry else INPUT_SEND.bind()
            self._set_btn_label(self._render_label(label), defer_geometry=True)
            btn.disabled = self.retry_pending or self.agent_loading or (not retry and not bool(ta.text.strip()))

        self._apply_placeholders(ta)
        self._update_new_btn_visibility()

    def watch_has_messages(self, _has_messages: bool) -> None:
        self._refresh_interaction_state()

    def watch_shell_mode(self, shell_mode: bool) -> None:
        ta = self.query_one("#chat-input", _ChatTextArea)
        if not shell_mode:
            # Restore normal state when exiting shell mode
            ta.clear()
        self._refresh_interaction_state()

    def on__chat_text_area_shell_mode_requested(self, event: _ChatTextArea.ShellModeRequested) -> None:
        event.stop()
        if self.agent_running or self.locked:
            return
        self.post_message(self.ShellModeRequested())

    def on__chat_text_area_editor_requested(self, event: _ChatTextArea.EditorRequested) -> None:
        """Forward editor requests when the chat draft remains writable."""
        event.stop()
        self._request_editor()

    def on__chat_text_area_shell_mode_cancelled(self, event: _ChatTextArea.ShellModeCancelled) -> None:
        event.stop()
        if self._suggestions_active:
            self.post_message(self.SuggestionDismiss())

    def on__chat_text_area_slash_trigger(self, event: _ChatTextArea.SlashTrigger) -> None:
        event.stop()
        if self.locked or self.shell_mode:
            return
        self.post_message(self.SlashTriggered())

    def on__chat_text_area_file_trigger(self, event: _ChatTextArea.FileTrigger) -> None:
        event.stop()
        if self.locked or self.shell_mode:
            return
        self.post_message(self.FileTriggered())

    def on__chat_text_area_agent_trigger(self, event: _ChatTextArea.AgentTrigger) -> None:
        event.stop()
        if self.agent_running or self.agent_loading or self.locked or self.shell_mode:
            return
        self.post_message(self.AgentTriggered())

    def on__chat_text_area_model_trigger(self, event: _ChatTextArea.ModelTrigger) -> None:
        event.stop()
        # Same four guards the status-bar selectors use: a profile switch that
        # lands mid-load is dropped downstream, after the draft is already gone.
        if self.agent_running or self.agent_loading or self.locked or self.shell_mode:
            return
        self.post_message(self.ModelTriggered())

    def on__chat_text_area_submitted(self, event: _ChatTextArea.Submitted) -> None:
        """Forward Enter-submit, selecting the active suggestion when applicable."""
        event.stop()
        if not self.display:
            return
        if self._suggestions_active and self._suggestion_mode == "history":
            self.post_message(self.SuggestionSelect(execute=True))
            return
        if self.retry_pending:
            return
        if self.agent_loading:
            self._refresh_interaction_state()
            return
        if self._suggestions_active:
            if self._suggestion_mode in ("files", "agents", "models", "commands", "subcommands"):
                self.post_message(self.SuggestionSelect(execute=True))
                return
            self.post_message(self.SuggestionDismiss())
        # In retry_mode a plain Enter-submit (no slash command) is treated
        # the same as clicking Continue/Retry: the typed text rides along
        # as a mid-turn note rather than starting a brand-new turn.
        # Slash commands always route through ``UserSubmitted`` so the
        # screen's dispatcher handles them.
        is_slash_command = is_slash_command_candidate(event.text)
        if self.retry_mode and not self.agent_running and not is_slash_command:
            text = event.text.strip()
            self.post_message(self.RetryRequested(text=text))
            self.query_one("#chat-input", _ChatTextArea).clear()
            self._refresh_interaction_state()
            return
        self.post_message(self.UserSubmitted(event.text))
        if not self.agent_running or is_slash_command:
            self.query_one("#chat-input", _ChatTextArea).clear()
            self._refresh_interaction_state()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Forward text changes when suggestions are active."""
        self._draft_revision += 1
        if self._suggestions_active:
            self.post_message(self.TextChanged(event.text_area.text))
        self._refresh_interaction_state()

    def on_key(self, event) -> None:
        """Intercept arrow keys and Tab when suggestions are active."""
        if not self._suggestions_active:
            return
        if event.key == "up":
            event.stop()
            event.prevent_default()
            self.post_message(self.SuggestionNavigate(direction="up"))
        elif event.key == "down":
            event.stop()
            event.prevent_default()
            self.post_message(self.SuggestionNavigate(direction="down"))
        elif event.key == "tab":
            event.stop()
            event.prevent_default()
            self.post_message(self.SuggestionSelect())

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if not self.display:
            return
        if event.button.id == "editor-btn":
            self._request_editor()
            return
        if self.locked or self.retry_pending:
            return  # keep the admission owner stable; draft editing stays available
        if self.agent_loading:
            return
        if event.button.id == "new-btn":
            self.post_message(self.NewSessionRequested())
            return
        if self.agent_running:
            self.post_message(self.InterruptRequested())
        elif self.retry_mode:
            # In retry mode the button always resumes the current turn.
            # Any text the user typed rides along as a mid-turn note;
            # the engine will use it as the continuation prompt in place
            # of the default ``"continue"`` placeholder.
            self.post_message(self.RetryRequested(text=self.consume_retry_text()))
        else:
            self._do_submit()

    def _do_submit(self) -> None:
        """Submit text from the text area."""
        if not self.display:
            return
        if self.agent_loading or self.retry_pending:
            self._refresh_interaction_state()
            return
        ta = self.query_one("#chat-input", _ChatTextArea)
        text = ta.text.strip()
        if text:
            ta._reset_history_browsing()
            self.post_message(self.UserSubmitted(text))
            ta.clear()
        self._refresh_interaction_state()

    def _lock(self, label: InputLabel, *, stop_style: bool, spin_prompt: bool = True) -> None:
        """Lock the input bar — text stays visible, input becomes read-only."""
        self._lock_label = label
        self._lock_stop_style = stop_style
        self._lock_spin_prompt = spin_prompt
        self.locked = True
        self._refresh_interaction_state()

    def lock_with_text(self) -> None:
        """Lock while a typed injection is queued for a running agent."""
        self._lock(INPUT_QUEUED.bind(), stop_style=True)

    def _spin_prompt(self) -> None:
        """Update the prompt indicator with a spinner frame."""
        frame = SPINNER_FRAMES[int(time() * 8) % len(SPINNER_FRAMES)]
        self.query_one("#editor-btn", Button).label = Text(frame)

    def _unlock(self) -> None:
        """Common unlock logic."""
        self.locked = False
        self._lock_label = ""
        self._lock_stop_style = False
        self._lock_spin_prompt = False
        self._refresh_interaction_state()

    def unlock_and_clear(self) -> None:
        """Unlock and clear the text — injection was consumed."""
        self._unlock()
        ta = self.query_one("#chat-input", _ChatTextArea)
        ta.clear()
        self.focus_input()
        self._refresh_interaction_state()

    def unlock_and_keep(self) -> None:
        """Unlock but keep the text — injection was abandoned."""
        self._unlock()
        self.focus_input()
        self._refresh_interaction_state()

    def focus_input(self) -> None:
        """Focus the visible composer without activating a hidden Chat draft."""
        if self.display:
            self.query_one("#chat-input", _ChatTextArea).focus()

    def snapshot_draft(self) -> InputDraftSnapshot:
        """Capture the current chat draft and cursor without mutating them."""
        text_area = self.query_one("#chat-input", _ChatTextArea)
        return InputDraftSnapshot(text_area.text, text_area.cursor_location, self._draft_revision)

    @property
    def draft_revision(self) -> int:
        """Return the monotonic generation of the underlying chat draft."""
        return self._draft_revision

    def restore_draft(self, text: str) -> bool:
        """Hand *text* back to an empty composer; return whether it was written.

        Every caller reaches this from a path that already consumed the draft —
        a submit, a retry, a rollback, a rejection — and the hand-back travels
        as a message, so the user may have started typing before it lands.
        ``replace_draft`` would destroy that draft and move the cursor, which
        also breaks an in-progress IME composition, so a draft that is already
        there wins and the caller is told nothing was restored. Whitespace is
        such a draft: an indent, a newline or a single space is something the
        user typed, and no consume path can leave one behind — a whitespace-only
        composer never submits.
        """
        if not text:
            return False
        if self.query_one("#chat-input", _ChatTextArea).text:
            return False
        self.replace_draft(text)
        return True

    def replace_draft(self, text: str) -> None:
        """Replace the draft, move its cursor to the end, and refresh controls."""
        self._draft_revision += 1
        text_area = self.query_one("#chat-input", _ChatTextArea)
        text_area._reset_history_browsing()
        text_area.clear()
        if text:
            text_area.insert(text)
        self._refresh_interaction_state()

    def can_commit_editor_draft(self) -> bool:
        """Return whether an open editor may currently replace this draft."""
        return not self.locked and not self.shell_mode

    def insert_image_paste_payload(self, text: str) -> bool:
        """Insert a pasted/dropped image path payload into the chat input.

        Returns ``True`` only when *text* was recognized as supported image
        path input and inserted. Normal prose/code pastes are left for the
        original event target to handle.
        """
        # MainScreen also guards shell mode; keep this local guard for direct InputBar callers.
        if self.shell_mode:
            return False
        ta = self.query_one("#chat-input", _ChatTextArea)
        if ta.read_only:
            return False
        try:
            prepared = ta._prepare_image_path_paste_text(text)
        except OSError, RuntimeError, ValueError:
            return False
        if not prepared:
            return False
        # This fallback handles path-only image payloads; normal large text pastes stay on the original target.
        result = ta._replace_via_keyboard(prepared, *ta.selection)
        if not result:
            return False
        ta.move_cursor(result.end_location)
        self.focus_input()
        self._refresh_interaction_state()
        return True

    def insert_clipboard_image_paste(self) -> bool:
        """Insert the current platform clipboard bitmap into the chat input."""
        # MainScreen also guards shell mode; keep this local guard for direct InputBar callers.
        if self.shell_mode:
            return False
        ta = self.query_one("#chat-input", _ChatTextArea)
        if ta.read_only:
            return False
        if not ta._insert_clipboard_image_paste():
            return False
        self.focus_input()
        self._refresh_interaction_state()
        return True

    def replace_trigger_text(self, trigger: str, replacement: str) -> None:
        """Replace the trigger token (e.g. ``/query`` or ``@query``) with *replacement*.

        Finds the last occurrence of *trigger* character in the text and replaces
        everything from that position to the cursor with *replacement*.
        Also checks for fullwidth CJK equivalents (e.g. U+FF20 for ``@``).
        """
        ta = self.query_one("#chat-input", _ChatTextArea)
        text = ta.text
        idx = text.rfind(trigger)
        # Check fullwidth variant for CJK keyboard support (ASCII+0xFEE0)
        if idx < 0 and len(trigger) == 1 and 0x21 <= ord(trigger) <= 0x7E:
            idx = text.rfind(chr(ord(trigger) + 0xFEE0))
        if idx < 0:
            return
        before = text[:idx]
        ta.clear()
        new_text = before + replacement
        if new_text:
            ta.insert(new_text)

    @property
    def value(self) -> str:
        return self.query_one("#chat-input", _ChatTextArea).text

    @value.setter
    def value(self, text: str) -> None:
        self.replace_draft(text)

    def consume_retry_text(self) -> str:
        """Return and clear the typed retry/continue note, if any."""
        ta = self.query_one("#chat-input", _ChatTextArea)
        text = ta.text.strip()
        if text:
            ta._reset_history_browsing()
            ta.clear()
        self._refresh_interaction_state()
        return text

    def add_to_history(self, text: str, *, session_id: str | None = None) -> None:
        """Add *text* to input history."""
        self.query_one("#chat-input", _ChatTextArea).add_to_history(text, session_id=session_id)

    async def load_prompt_history(self, *, max_entries: int) -> list[str]:
        """Load global prompt history for the suggestion popup."""
        return await self.query_one("#chat-input", _ChatTextArea).load_prompt_history(max_entries=max_entries)

    async def action_submit(self) -> None:
        """Programmatic submit (for testing)."""
        self._do_submit()
