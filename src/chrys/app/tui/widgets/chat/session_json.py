# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SessionJsonPanel — virtualized syntax-highlighted viewer for the current session's raw JSON."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, ClassVar

from rich.cells import cell_len
from rich.segment import Segment
from rich.style import Style as RichStyle
from rich.syntax import Syntax
from rich.text import Text
from textual.cache import LRUCache
from textual.dom import NoScreen
from textual.geometry import Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.strip import Strip

from chrys.app.tui.clipboard import copy_text_to_clipboards, notify_after_copy
from chrys.app.tui.copy_messages import COPIED_TITLE
from chrys.app.tui.i18n import render_str
from chrys.app.tui.support.gc_freeze import DetachedLruCache, GcFreezeBlockReason, detach_lru_cache, renew_lru_cache
from chrys.app.tui.util.rich_style import rich_style_from_textual_color
from chrys.app.tui.widgets.hatch import hatch_text_style, hatched_text_line
from chrys.app.tui.widgets.syntax_theme import TransparentPygmentsTheme
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.util.session_ids import session_short_id
from chrys.service.state.store import JsonFileStateStore

if TYPE_CHECKING:
    from pathlib import Path

    from textual.events import Click
    from textual.selection import Selection

    from chrys.app.tui.i18n import LocaleController

_SESSION_JSON_TITLE = msg("tui.chat.session_json.title", fallback="Session JSON")
_SESSION_JSON_NOT_FOUND = msg("tui.chat.session_json.not_found", fallback="No session file found.")
_SESSION_JSON_LOADING = msg("tui.chat.session_json.loading", fallback="Loading…")
_SESSION_JSON_READ_ERROR = msg(
    "tui.chat.session_json.read_error",
    fallback="Error reading session file: {error}",
)
_SESSION_JSON_SESSION_TITLE = msg(
    "tui.chat.session_json.session_title",
    fallback="Session JSON: {session_id}",
)
_SESSION_PATH_COPIED = msg("tui.chat.session_json.path_copied", fallback="Path copied")


class SessionJsonPanel(ScrollView):
    """Virtualized JSON viewer for session files.

    Uses ``ScrollView.render_line()`` so only visible viewport lines are
    rendered — avoiding the performance cliff of pushing thousands of
    syntax-highlighted lines through a ``Static`` widget.

    Heavy work (file I/O, JSON formatting, Pygments highlighting) runs in
    a background thread so the UI stays responsive.
    """

    ALLOW_SELECT = True

    COMPONENT_CLASSES: ClassVar[set[str]] = {"session-json--status", "hatch--pattern"}

    DEFAULT_CSS = """
    SessionJsonPanel > .hatch--pattern {
        color: $hatch-color;
    }
    SessionJsonPanel > .session-json--status {
        color: $text-muted;
    }
    SessionJsonPanel {
        height: 1fr;
        display: none;
        border: round $tui-border-accent $border-opacity;
        border-title-align: left;
        border-title-color: $tui-border-title-accent;
        border-subtitle-align: right;
        border-subtitle-color: $tui-border-title-accent;
        scrollbar-size: 1 1;
        overflow: auto auto;
    }
    """

    class LoadStateChanged(Message):
        """A background load started or settled; hosts can overlay a shared indicator."""

        def __init__(self, *, loading: bool) -> None:
            super().__init__()
            self.loading = loading

    def __init__(self, *, locale_controller: LocaleController | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._locale_controller = locale_controller
        self._loading = False
        self._session_id: str = ""
        self._session_path: str = ""
        self._formatted_json: str = ""
        self._file_mtime: float = 0.0
        self._last_theme_key: tuple[bool, str | None] | None = None
        self._text_lines: list[Text] = []
        self._plain_lines: list[str] = []
        self._line_widths: list[int] = []
        self._max_width: int = 0
        self._strip_cache: LRUCache[int, Strip] = LRUCache(maxsize=500)
        self._cache_scroll_x: int = -1
        self._cache_width: int = -1
        self._status: str = ""
        self._content_generation = 0
        self._shell_mode_suspended = False
        self._update_border_title()

    def on_mount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)

    def refresh_localization(self) -> None:
        """Retranslate the border from stored session state."""
        self._update_border_title()

    def _update_border_title(self) -> None:
        if self._session_id:
            reference = _SESSION_JSON_SESSION_TITLE.bind(session_id=session_short_id(self._session_id))
        else:
            reference = _SESSION_JSON_TITLE.bind()
        self.border_title = Text(self._render_message(reference))

    def _render_message(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        if controller is None:
            return format_message(reference)
        return render_str(controller.localizer, reference)

    @property
    def is_loading(self) -> bool:
        """Whether a background load is still producing this viewer's content."""
        return self._loading

    def _set_loading(self, loading: bool) -> None:
        if loading == self._loading:
            return
        self._loading = loading
        self.post_message(self.LoadStateChanged(loading=loading))

    def gc_freeze_block_reason(self) -> GcFreezeBlockReason | None:
        """Block while the JSON viewer is visible or temporarily covered by shell mode."""
        return GcFreezeBlockReason.SESSION_JSON_VISIBLE if self.display or self._shell_mode_suspended else None

    def suspend_for_shell_mode(self) -> None:
        """Hide without cancelling an active load or releasing rendered content."""
        self._shell_mode_suspended = True
        self.display = False

    def finish_shell_mode(self, *, restore: bool) -> None:
        """End shell suspension, restoring the viewer or cancelling its retained content."""
        if restore:
            self.display = True
            self._shell_mode_suspended = False
        elif self._shell_mode_suspended:
            self.hide_session_json()
        else:
            self.display = False

    def hide_session_json(self) -> None:
        """Hide the viewer and release its large content/cache graph."""
        self._shell_mode_suspended = False
        self.display = False
        self._release_hidden_content()

    def prepare_for_gc_freeze(self) -> None:
        """Idempotently release hidden content immediately before a freeze."""
        if not self.display and not self._shell_mode_suspended:
            self._release_hidden_content()
            # gc-freeze swaps in an acyclic capacity token; renew restores a real cache before next use.
            self._strip_cache = detach_lru_cache(self._strip_cache)  # ty: ignore[invalid-assignment]

    def after_gc_freeze(self) -> None:
        """Recreate the mutable cyclic LRU after the permanent generation changes."""
        self._strip_cache = renew_lru_cache(self._strip_cache)

    def abort_gc_freeze(self) -> None:
        """Restore the strip LRU after an incomplete hook pass."""
        self.after_gc_freeze()

    def _release_hidden_content(self) -> None:
        self._content_generation += 1
        self._set_loading(False)
        self._formatted_json = ""
        self._file_mtime = 0.0
        self._last_theme_key = None
        self._text_lines = []
        self._plain_lines = []
        self._line_widths = []
        self._max_width = 0
        if not isinstance(self._strip_cache, DetachedLruCache):
            self._strip_cache.clear()
        self._cache_scroll_x = -1
        self._cache_width = -1
        self._status = ""
        self.virtual_size = Size(0, 0)

    def _can_commit_worker(self, generation: int) -> bool:
        return generation == self._content_generation and (self.display or self._shell_mode_suspended)

    def notify_style_update(self) -> None:
        """Re-highlight when the theme changes (dark/light, ANSI, gutter colour).

        Re-highlights from the cached formatted JSON rather than re-reading the
        file, so transient style-update storms (e.g. terminal tab focus changes)
        don't cause a "Loading…" flash. If the theme hasn't actually changed,
        this is a no-op beyond clearing the strip cache.
        """
        super().notify_style_update()
        self._strip_cache.clear()
        if not self._formatted_json:
            return
        theme = self.app.current_theme
        is_ansi = theme.ansi
        gutter_color = None if is_ansi else theme.secondary
        theme_key = (theme.dark, gutter_color)
        if theme_key == self._last_theme_key:
            # Content and theme unchanged — render_line will use existing text_lines.
            return
        self.run_worker(
            self._rehighlight_worker(theme.dark, gutter_color, self._content_generation),
            exclusive=True,
        )

    def on_click(self, event: Click) -> None:
        """Copy session path when the border is clicked (not the content area)."""
        if self._session_path and not self.scrollable_content_region.contains(event.screen_x, event.screen_y):
            dispatched = copy_text_to_clipboards(self.app, self._session_path)
            notify_after_copy(
                self,
                self._render_message(_SESSION_PATH_COPIED.bind()),
                dispatched=dispatched,
                title=self._render_message(COPIED_TITLE.bind()),
                timeout=1,
                markup=False,
            )

    @property
    def session_path(self) -> str:
        """The checkpoint path currently displayed by the viewer."""
        return self._session_path

    def _resolve_session_path(self, session_id: str) -> Path | None:
        """Resolve the on-disk session JSON path for the given session ID."""
        store = JsonFileStateStore()
        return store.resolve_session_file(session_id)

    def set_status(self, message: str) -> None:
        """Show a single-line status message instead of JSON content."""
        self._content_generation += 1
        self._set_loading(False)
        self._set_status(message)

    def _set_status(self, message: str) -> None:
        self._status = message
        self._text_lines = []
        self._plain_lines = []
        self._line_widths = []
        self._max_width = 0
        self._strip_cache.clear()
        self.virtual_size = Size(80, 1)
        self.refresh()

    def load_session(self, session_id: str) -> None:
        """Load and display the JSON for the given session ID.

        Skips duplicate loads while the viewer remains open and the file mtime
        is unchanged. Hiding deliberately releases the formatted content and
        mtime, so reopening performs a fresh load.
        """
        problem = None
        try:
            path = self._resolve_session_path(session_id)
            mtime = path.stat().st_mtime if path is not None else 0.0
        except FileNotFoundError, NotADirectoryError:
            path = None
        except OSError as exc:
            path = None
            problem = _SESSION_JSON_READ_ERROR.bind(error=str(exc))
        if path is None:
            self._session_id = session_id
            self._session_path = ""
            self._formatted_json = ""
            self._file_mtime = 0.0
            self._update_border_title()
            self.border_subtitle = ""
            self.set_status(self._render_message(problem or _SESSION_JSON_NOT_FOUND.bind()))
            return

        # Fast path: same session, file unchanged, cache populated — nothing to do.
        if (
            session_id == self._session_id
            and str(path) == self._session_path
            and mtime == self._file_mtime
            and self._formatted_json
            and self._text_lines
            and not self._status
        ):
            return

        self._session_id = session_id
        self._file_mtime = mtime
        self._session_path = str(path)
        self._update_border_title()
        self.border_subtitle = Text(self._session_path)
        self._content_generation += 1
        generation = self._content_generation
        self._set_status(self._render_message(_SESSION_JSON_LOADING.bind()))
        self._set_loading(True)
        theme = self.app.current_theme
        is_ansi = self.app.current_theme.ansi
        gutter_color = None if is_ansi else theme.secondary
        self.run_worker(self._load_worker(path, theme.dark, gutter_color, generation), exclusive=True)

    @staticmethod
    def _highlight(
        json_text: str, dark: bool, gutter_color: str | None
    ) -> tuple[list[Text], list[str], list[int], int]:
        """Highlight JSON and build per-line ``Text`` objects with line numbers.

        Uses a custom ``TransparentPygmentsTheme`` that strips all ``bgcolor``
        from token styles, so the widget's Textual theme background shows
        through instead of the Pygments theme background.

        Thread-safe — designed to run via ``asyncio.to_thread``.
        """
        theme_name = "native" if dark else "default"
        syntax = Syntax(json_text, "json", theme=TransparentPygmentsTheme(theme_name))
        highlighted = syntax.highlight(json_text)

        text_lines = highlighted.split("\n")
        if text_lines and not text_lines[-1].plain:
            text_lines.pop()

        num_lines = len(text_lines)
        gutter_pad = max(len(str(num_lines)), 3)
        # ANSI themes: use dim (terminal handles it).
        # User themes may use Textual-only spellings such as ANSI or HSL.
        gutter_style = rich_style_from_textual_color(gutter_color) if gutter_color else RichStyle(dim=True)

        result_lines: list[Text] = []
        plain_lines: list[str] = []
        widths: list[int] = []
        for i, text_line in enumerate(text_lines, 1):
            gutter = Text(f" {i:>{gutter_pad}} \u2502 ", style=gutter_style, end="")
            full_line = Text.assemble(gutter, text_line, no_wrap=True, end="")
            result_lines.append(full_line)
            plain = full_line.plain
            plain_lines.append(plain)
            widths.append(cell_len(plain))

        max_w = max(widths) if widths else 0
        return result_lines, plain_lines, widths, max_w

    async def _load_worker(self, path: Path, dark: bool, gutter_color: str | None, generation: int) -> None:
        """Background worker: read, format, highlight, then update the widget."""
        try:
            raw_text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            raw = json.loads(raw_text)
            formatted = json.dumps(raw, indent=2, ensure_ascii=False)
            text_lines, plain_lines, widths, max_w = await asyncio.to_thread(
                self._highlight, formatted, dark, gutter_color
            )
        except Exception as e:
            if self._can_commit_worker(generation):
                self._set_loading(False)
                self._set_status(self._render_message(_SESSION_JSON_READ_ERROR.bind(error=str(e))))
            return

        if not self._can_commit_worker(generation):
            return
        self._set_loading(False)
        self._formatted_json = formatted
        self._last_theme_key = (dark, gutter_color)
        self._status = ""
        self._text_lines = text_lines
        self._plain_lines = plain_lines
        self._line_widths = widths
        self._max_width = max_w
        self._strip_cache.clear()
        self._cache_scroll_x = -1
        self._cache_width = -1
        self.virtual_size = Size(max_w, len(text_lines))
        self.scroll_home(animate=False)
        self.refresh()

    async def _rehighlight_worker(self, dark: bool, gutter_color: str | None, generation: int) -> None:
        """Re-highlight cached JSON for a theme change without re-reading disk."""
        if not self._formatted_json:
            return
        try:
            text_lines, plain_lines, widths, max_w = await asyncio.to_thread(
                self._highlight, self._formatted_json, dark, gutter_color
            )
        except Exception:
            return
        if not self._can_commit_worker(generation):
            return
        self._last_theme_key = (dark, gutter_color)
        self._text_lines = text_lines
        self._plain_lines = plain_lines
        self._line_widths = widths
        self._max_width = max_w
        self._strip_cache.clear()
        self._cache_scroll_x = -1
        self._cache_width = -1
        self.virtual_size = Size(max_w, len(text_lines))
        self.refresh()

    # -- Selection support ------------------------------------------------- #

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        if not self._plain_lines:
            return None
        text = "\n".join(self._plain_lines)
        return selection.extract(text), "\n"

    def selection_updated(self, selection: Selection | None) -> None:
        self._strip_cache.clear()
        self.refresh()

    # -- Rendering --------------------------------------------------------- #

    def render_line(self, y: int) -> Strip:
        if not self.is_attached:
            return Strip.blank(max(0, self.size.width)).apply_offsets(0, y)

        width = self.scrollable_content_region.width
        bg_style = self.visual_style.rich_style

        # Status-message mode (loading / error / empty) — the same hatch and
        # centered muted label as the trajectory dashboard's empty states.
        if self._status:
            height = self.scrollable_content_region.height
            # $text-muted is a composite value ("auto 60%", "ansi_white 40%")
            # only the stylesheet can resolve; strip the stamped background so
            # the label stays transparent over the hatch.
            muted = self.get_component_rich_style("session-json--status")
            line = hatched_text_line(
                width,
                self._status if y == height // 2 else None,
                hatch_style=hatch_text_style(self),
                label_style=muted.without_color + RichStyle.from_color(muted.color),
            )
            console = self.app.console
            strip = Strip(list(line.render(console)), cell_len(line.plain))
            # Text.render() drops a Text's own style; re-apply it under the
            # span styles exactly as the dashboard's text view does.
            return strip.adjust_cell_length(width).apply_style(bg_style + console.get_style(line.style))

        scroll_x = round(self.scroll_offset.x)
        scroll_y = round(self.scroll_offset.y)
        content_y = scroll_y + y

        if content_y >= len(self._text_lines):
            return Strip.blank(width, bg_style)

        selection = self._safe_text_selection()
        use_cache = selection is None

        # Invalidate strip cache when horizontal scroll or width changes
        if scroll_x != self._cache_scroll_x or width != self._cache_width:
            self._strip_cache.clear()
            self._cache_scroll_x = scroll_x
            self._cache_width = width

        if use_cache:
            cached = self._strip_cache.get(content_y)
            if cached is not None:
                return cached

        text_line = self._text_lines[content_y]

        # Apply selection highlight (requires a copy to avoid mutating stored Text)
        if selection is not None and (span := selection.get_span(content_y)) is not None:
            start, end = span
            if end == -1:
                end = len(text_line)
            text_line = text_line.copy()
            selection_style = self.screen.get_component_rich_style("screen--selection")
            text_line.stylize(selection_style, start, end)

        # Token styles have no bgcolor (TransparentPygmentsTheme strips them).
        # Merge the widget's bg_style as a base so the Textual theme background
        # fills every cell.  For ANSI themes bg_style is the terminal default
        # (transparent); for non-ANSI themes it is a solid colour.
        segments = [
            Segment(s.text, bg_style + s.style if s.style else bg_style) for s in text_line.render(self.app.console)
        ]
        cell_length = self._line_widths[content_y]
        strip = Strip(segments, cell_length)
        strip = strip.crop_extend(scroll_x, scroll_x + width, bg_style)
        strip = strip.apply_offsets(scroll_x, content_y)

        if use_cache:
            self._strip_cache[content_y] = strip

        return strip

    def _safe_text_selection(self) -> Selection | None:
        try:
            return self.text_selection
        except NoScreen:
            return None
