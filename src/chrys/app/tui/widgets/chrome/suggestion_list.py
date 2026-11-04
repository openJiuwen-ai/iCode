# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SuggestionList — shared popup for commands, files, agents, models, and prompt history."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from textual import events
from textual.app import ComposeResult
from textual.content import Content
from textual.css.scalar import Scalar
from textual.dom import NoScreen
from textual.geometry import Region, Size
from textual.message import Message
from textual.style import Style
from textual.widget import Widget

from chrys.app.tui.i18n import render_str
from chrys.app.tui.util.visibility import (
    resync_compositor_regions,
    set_widget_visibility_without_layout,
)
from chrys.app.tui.widgets.loading import ChrysLoadingIndicator
from chrys.app.tui.widgets.marquee import OverflowMarquee
from chrys.foundation.i18n import msg
from chrys.foundation.i18n.formatting import format_message

if TYPE_CHECKING:
    from textual.timer import Timer

    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.i18n import MessageRef

_SUGGESTIONS_NO_RESULTS = msg("tui.suggestions.no_results", fallback="No results")

_SUGGESTION_ROWS = 12
# Prompt history shows its loading indicator only for loads slower than this,
# so a quick load or a reopen opens the list without a flash.
_HISTORY_LOADING_INDICATOR_DELAY_SECONDS = 0.15


@dataclass(frozen=True)
class SuggestionItem:
    """A structured suggestion row with optional grouping metadata."""

    value: str
    label: str | Content
    section: str = ""
    kind: str = ""
    disabled: bool = False
    disabled_reason: str = ""
    # Character boundary between a pinned identity and its scrolling detail.
    marquee_start: int | None = None
    # ``[start, end)`` character ranges of the label that matched the query,
    # drawn with the ``suggestion-list--match`` style of the current theme.
    match_spans: tuple[tuple[int, int], ...] = ()


class SuggestionList(Widget):
    """Shared suggestion list for commands, files, agents, models, and prompt history.

    Hidden by default. Shown as a screen overlay immediately above InputBar,
    painting over the reserved StatusBar row when necessary, so filtering
    never resizes the transcript viewport. Does NOT take focus — InputBar
    keeps focus and forwards key events.
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "suggestion-list--option-hover",
        "suggestion-list--match",
        "suggestion-list--section",
    }

    DEFAULT_CSS = """
    SuggestionList {
        overlay: screen;
        offset-y: -100%;
        width: 100%;
        height: 14;
        visibility: hidden;
        border: round $tui-border-accent $border-opacity;
        border-title-color: $accent;
        padding: 0 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    SuggestionList > .suggestion-list--option-hover {
        background: $primary 30%;
    }
    SuggestionList > .suggestion-list--section {
        color: $text-primary;
    }
    SuggestionList > .suggestion-list--match {
        color: $accent;
    }
    SuggestionList > #suggestion-list-loading {
        width: 100%;
        height: 1;
        content-align: center middle;
    }
    """

    @dataclass
    class Selected(Message):
        """An item was selected from the suggestion list."""

        text: str
        mode: str
        execute: bool = False
        kind: str = ""

    @dataclass
    class Dismissed(Message):
        """The suggestion list was dismissed."""

    class RowWidthChanged(Message):
        """The cells a row can show changed while the list was visible."""

    def __init__(
        self,
        *,
        marquee: OverflowMarquee | None = None,
        locale_controller: LocaleController | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._locale_controller = locale_controller
        self._mode: str = ""
        self._values: list[str | None] = []
        self._kinds: list[str] = []
        self._contents: list[Content] = []
        self._disabled: list[bool] = []
        self._marquee_starts: list[int | None] = []
        self._highlighted: int | None = None
        self._hovered_row: int | None = None
        self._window_start = 0
        self._marquee = marquee or OverflowMarquee()
        self._marquee_timer: Timer | None = None
        self._loading_timer: Timer | None = None
        self._row_width = 0
        self._marquee_generation = 0
        self._loading = False
        self._loading_indicator = ChrysLoadingIndicator(id="suggestion-list-loading")
        self._loading_indicator.display = False

    def compose(self) -> ComposeResult:
        """Mount the loading state without creating widgets for result rows."""
        yield self._loading_indicator

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def is_loading(self) -> bool:
        """Return whether the popup is waiting for its initial result set."""
        return self._loading

    def show_loading(self, mode: str, *, title: str = "") -> None:
        """Show an immediate, non-selectable loading state for *mode*."""
        self._mode = mode
        self.border_title = Content(title) if title else None
        self._clear_items()
        self._set_loading(True)
        self._sync_height()
        set_widget_visibility_without_layout(self, True)
        self._cancel_marquee()
        self.refresh()

    def show(
        self,
        mode: str,
        items: list[SuggestionItem | tuple[str, str | Content]],
        disabled: set[str] | None = None,
        initial: str | None = None,
        title: str = "",
    ) -> None:
        """Show the suggestion list with the given items.

        Args:
            mode: ``"commands"``, ``"files"``, ``"agents"``, ``"models"``, or ``"history"``.
            items: List of ``(value, display_label)`` pairs. Label can be a
                plain string or a pre-styled :class:`Content`.
            disabled: Set of values that should be shown but not selectable.
            initial: Value to highlight initially (instead of the first enabled item).
            title: Border title naming what is being suggested (empty for none).
        """
        self._set_loading(False)
        self._mode = mode
        # Plain Content: a path in the title must not be parsed as markup.
        self.border_title = Content(title) if title else None
        self._populate(items, disabled, initial)
        set_widget_visibility_without_layout(self, True)
        self._restart_marquee()

    def hide(self) -> None:
        """Hide and clear the suggestion list."""
        self._cancel_marquee()
        self._set_loading(False)
        set_widget_visibility_without_layout(self, False)
        self.border_title = None
        self._clear_items()
        self._mode = ""
        self.refresh()

    def update(
        self,
        items: list[SuggestionItem | tuple[str, str | Content]],
        disabled: set[str] | None = None,
        initial: str | None = None,
        title: str | None = None,
        empty_text: str | None = None,
        *,
        keep_selection: bool = False,
    ) -> None:
        """Replace displayed items with a new filtered set.

        Args:
            initial: Value to highlight (instead of the first enabled item),
                unless *keep_selection* keeps the one highlighted now.
            title: When given, re-renders the border title so per-keystroke
                rebuilds pick up the active locale (``""`` clears it; ``None``
                keeps the current title).
            empty_text: Rendered row shown when *items* is empty; ``None``
                shows the generic "No results".
            keep_selection: Highlight the value highlighted now, at the same
                scroll position, when the new items still hold it.
        """
        self._set_loading(False)
        if title is not None:
            # Plain Content: a path in the title must not be parsed as markup.
            self.border_title = Content(title) if title else None
        highlighted = self._highlighted
        kept = self._values[highlighted] if keep_selection and highlighted is not None else None
        window_start = self._window_start
        self._populate(items, disabled, kept if kept is not None else initial, empty_text=empty_text)
        if kept is not None and self._highlighted is not None and self._values[self._highlighted] == kept:
            self._window_start = window_start
            self._ensure_highlight_visible()
        self._restart_marquee()

    def _set_loading(self, loading: bool) -> None:
        """Switch the loading state; prompt history shows the indicator only once its load turns slow."""
        self._loading = loading
        timer, self._loading_timer = self._loading_timer, None
        if timer is not None:
            timer.stop()
        delayed = loading and self._mode == "history"
        self._loading_indicator.display = loading and not delayed
        if delayed:
            self._loading_timer = self.set_timer(
                _HISTORY_LOADING_INDICATOR_DELAY_SECONDS, self._reveal_loading_indicator
            )

    def _reveal_loading_indicator(self) -> None:
        self._loading_timer = None
        if self._loading:
            self._loading_indicator.display = True

    def _clear_items(self) -> None:
        """Clear every rendered and interactive suggestion row."""
        self._values.clear()
        self._kinds.clear()
        self._contents.clear()
        self._disabled.clear()
        self._marquee_starts.clear()
        self._highlighted = None
        self._hovered_row = None
        self._window_start = 0

    def _render_message(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        if controller is None:
            return format_message(reference)
        return render_str(controller.localizer, reference)

    def _populate(
        self,
        items: list[SuggestionItem | tuple[str, str | Content]],
        disabled: set[str] | None = None,
        initial: str | None = None,
        *,
        empty_text: str | None = None,
    ) -> None:
        """Replace the fixed-height rendered suggestion rows."""
        self._clear_items()
        if not items:
            if empty_text is None:
                empty_text = self._render_message(_SUGGESTIONS_NO_RESULTS.bind())
            self._values.append(None)
            self._kinds.append("")
            self._contents.append(Content.styled(self._single_line(empty_text), "#888888"))
            self._disabled.append(True)
            self._marquee_starts.append(None)
            self._highlighted = None
            self._sync_height()
            self.refresh()
            return
        disabled = disabled or set()
        first_enabled: int | None = None
        initial_idx: int | None = None
        previous_section = ""
        normalized = self._normalize_items(items)
        match_style = self._match_style() if any(item.match_spans for item in normalized) else None
        section_style = self._section_style() if any(item.section for item in normalized) else None
        for item in normalized:
            if item.section and item.section != previous_section:
                previous_section = item.section
                self._values.append(None)
                self._kinds.append("")
                self._contents.append(Content.styled(self._single_line(item.section), section_style or ""))
                self._disabled.append(True)
                self._marquee_starts.append(None)
            option_index = len(self._contents)
            self._values.append(item.value)
            self._kinds.append(item.kind)
            is_disabled = item.disabled or item.value in disabled
            self._contents.append(self._content_for_item(item, is_disabled=is_disabled, match_style=match_style))
            self._disabled.append(is_disabled)
            self._marquee_starts.append(item.marquee_start)
            if not is_disabled and first_enabled is None:
                first_enabled = option_index
            if initial is not None and item.value == initial and not is_disabled:
                initial_idx = option_index
        self._highlighted = initial_idx if initial_idx is not None else first_enabled
        self._ensure_highlight_visible()
        self._sync_height()
        self.refresh()

    @staticmethod
    def _normalize_items(items: list[SuggestionItem | tuple[str, str | Content]]) -> list[SuggestionItem]:
        """Normalize legacy tuple items into structured suggestion items."""
        normalized: list[SuggestionItem] = []
        for item in items:
            if isinstance(item, SuggestionItem):
                normalized.append(item)
            else:
                value, label = item
                normalized.append(SuggestionItem(value=value, label=label))
        return normalized

    @staticmethod
    def _single_line(text: str) -> str:
        """Collapse newlines: window math and click mapping assume one row per item."""
        return " ".join(text.splitlines()) if "\n" in text or "\r" in text else text

    def _section_style(self) -> Style:
        """Return the style of a group heading: its own color, not bold like the rows, apart from the title's accent."""
        return Style(foreground=self.get_component_styles("suggestion-list--section").color)

    def _match_style(self) -> Style:
        """Return the theme's style for matched characters: its match color, in bold.

        Bold keeps matches visible where colors are not shown.
        """
        return Style(foreground=self.get_component_styles("suggestion-list--match").color, bold=True)

    @classmethod
    def _content_for_item(cls, item: SuggestionItem, *, is_disabled: bool, match_style: Style | None = None) -> Content:
        """Return single-line display content for one selectable item."""
        label = item.label
        if item.disabled_reason:
            content = Content.assemble(label, ("  " + item.disabled_reason, "dim"))
        elif isinstance(label, Content):
            content = label
        else:
            # Suggestion strings include user-authored prompt history and must
            # never be interpreted as Textual markup.  A Pydantic diagnostic
            # such as ``[type=missing, input_value={}]`` is otherwise enough
            # to raise MarkupError when Ctrl+R opens the history popup.
            content = Content(label)
        if "\n" in content.plain:
            # Degenerate multi-line label (e.g. user-authored skill metadata):
            # flatten, trading its inline styling for correct row accounting.
            content = Content(cls._single_line(content.plain))
        if is_disabled:
            return Content.styled(str(content), "dim")
        if match_style is not None:
            for start, end in item.match_spans:
                content = content.stylize(match_style, start, end)
        return content

    @property
    def is_visible(self) -> bool:
        return self.visible

    def move_cursor_up(self) -> None:
        """Move the highlight cursor up, skipping disabled items."""
        self._move_cursor(-1)

    def move_cursor_down(self) -> None:
        """Move the highlight cursor down, skipping disabled items."""
        self._move_cursor(1)

    def move_cursor_page(self, direction: int) -> None:
        """Move the highlight one page of rows up (-1) or down (1), stopping at either end."""
        selectable = [index for index, disabled in enumerate(self._disabled) if not disabled]
        if not selectable:
            return
        if self._highlighted not in selectable:
            position = 0 if direction > 0 else len(selectable) - 1
        else:
            position = selectable.index(self._highlighted) + direction * _SUGGESTION_ROWS
        self._highlighted = selectable[min(max(position, 0), len(selectable) - 1)]
        self._ensure_highlight_visible()
        self._restart_marquee()
        self.refresh()

    def _move_cursor(self, direction: int) -> None:
        selectable = [index for index, disabled in enumerate(self._disabled) if not disabled]
        if not selectable:
            return
        if self._highlighted not in selectable:
            self._highlighted = selectable[0 if direction > 0 else -1]
        else:
            position = selectable.index(self._highlighted)
            self._highlighted = selectable[(position + direction) % len(selectable)]
        self._ensure_highlight_visible()
        self._restart_marquee()
        self.refresh()

    def _restart_marquee(self) -> None:
        """Start one overflow animation for the visible highlighted row."""
        self._cancel_marquee()
        highlighted = self._highlighted
        if (
            not self.visible
            or not self._is_on_current_screen()
            or self._mode not in {"agents", "models", "commands", "history"}
            or highlighted is None
            or not self._window_start <= highlighted < self._window_start + _SUGGESTION_ROWS
        ):
            return
        marquee_start = self._marquee_starts[highlighted]
        if marquee_start is None:
            return
        delay = self._marquee.activate(
            self._contents[highlighted],
            self.content_size.width,
            fixed_prefix_end=marquee_start,
        )
        if delay is not None:
            self._schedule_marquee(delay, self._marquee_generation)

    def _cancel_marquee(self) -> None:
        """Invalidate queued callbacks, stop the timer, and restore static text."""
        self._marquee_generation += 1
        timer = self._marquee_timer
        self._marquee_timer = None
        if timer is not None:
            timer.stop()
        self._marquee.reset()

    def _schedule_marquee(self, delay: float, generation: int) -> None:
        """Schedule the next frame for the current animation generation."""
        self._marquee_timer = self.set_timer(delay, partial(self._advance_marquee, generation))

    def _advance_marquee(self, generation: int) -> None:
        """Paint one frame unless selection or lifecycle changes made it stale."""
        if generation != self._marquee_generation:
            return
        if not self.visible:
            self._cancel_marquee()
            return
        if not self._is_on_current_screen():
            self._cancel_marquee()
            return
        self._marquee_timer = None
        delay = self._marquee.advance()
        if delay is None:
            return
        self.refresh()
        self._schedule_marquee(delay, generation)

    def _is_on_current_screen(self) -> bool:
        """Return whether repainting this widget can affect the visible screen."""
        try:
            return self.screen is self.app.screen
        except NoScreen:
            return False

    def pause_marquee(self) -> None:
        """Stop animation while the owning screen is covered."""
        self._cancel_marquee()

    def resume_marquee(self) -> None:
        """Restore static text and restart animation after screen resume."""
        self._restart_marquee()
        # Screen.refresh() does not invalidate cached render strips owned by
        # descendants. Repaint this leaf so a frame cached before suspension
        # cannot remain visible after the covering screen is popped.
        self.refresh()

    def on_hide(self) -> None:
        """Do not retain a timer for compositor-hidden suggestions."""
        self._cancel_marquee()

    def on_show(self) -> None:
        """Start animation when visibility changes outside :meth:`show`."""
        if self._marquee_timer is None:
            self._restart_marquee()

    def on_resize(self, _event: events.Resize) -> None:
        """Recalculate overflow from the new content width."""
        self._restart_marquee()
        width = self.content_size.width
        if width != self._row_width:
            self._row_width = width
            if self.visible and self._mode:
                self.post_message(self.RowWidthChanged())

    @property
    def row_width(self) -> int:
        """Return the cells one row can show, or 0 before the list is laid out."""
        return self.content_size.width

    def on_unmount(self) -> None:
        """Release the timer and active content during teardown."""
        self._cancel_marquee()
        if self._loading_timer is not None:
            self._loading_timer.stop()
            self._loading_timer = None

    def _ensure_highlight_visible(self) -> None:
        old_window_start = self._window_start
        highlighted = self._highlighted
        if highlighted is None:
            self._window_start = 0
        else:
            if highlighted < self._window_start:
                preceding_row = highlighted - 1
                self._window_start = (
                    preceding_row if preceding_row >= 0 and self._values[preceding_row] is None else highlighted
                )
            elif highlighted >= self._window_start + _SUGGESTION_ROWS:
                self._window_start = highlighted - _SUGGESTION_ROWS + 1
            self._window_start = min(self._window_start, max(0, len(self._contents) - _SUGGESTION_ROWS))
        if self._window_start != old_window_start:
            self._hovered_row = None

    def select_highlighted(self, execute: bool = False) -> bool:
        """Post a Selected message for the currently highlighted item.

        Returns True if a selection was made, False if nothing was highlighted.
        """
        highlighted = self._highlighted
        if highlighted is not None and highlighted < len(self._values):
            if self._disabled[highlighted]:
                return False
            value = self._values[highlighted]
            if value is None:
                return False
            self.post_message(
                self.Selected(
                    text=value,
                    mode=self._mode,
                    execute=execute,
                    kind=self._kinds[highlighted],
                )
            )
            return True
        return False

    def _sync_height(self) -> None:
        """Content-size the overlay so it never blanks rows it does not use.

        The widget must not reserve its maximum window height: an oversized
        overlay paints opaque empty rows over the transcript and swallows the
        mouse events aimed there. Pin the height rule to the rendered row
        count plus the separator border — no layout escalation. The
        ``offset-y: -100%`` anchor keeps the bottom edge glued to the
        InputBar whatever the height.
        """
        rows = max(1, min(len(self._contents), _SUGGESTION_ROWS))
        outer_height = rows + self.gutter.height
        if not self.styles.set_rule("height", Scalar.from_number(outer_height)):
            return
        parent = self.parent
        if isinstance(parent, Widget):
            parent._clear_arrangement_cache()
        if not self.visible:
            # While hidden, the pending visibility flip performs the remap.
            return
        if not self._patch_overlay_geometry(outer_height):
            resync_compositor_regions(self)

    def _patch_overlay_geometry(self, outer_height: int) -> bool:
        """Rewrite this widget's compositor entries in place; True if done.

        Filtering changes the row count per keystroke, and even a cache-hot
        compositor-wide remap per keystroke hitches large transcripts. As an
        ``overlay: screen`` widget this popup's height affects no sibling
        geometry. Its loading child is hidden on the typing path, so only the
        popup map entries change: height, virtual content size, and the top
        edge (the ``offset-y: -100%`` anchor keeps the bottom pinned).
        Equivalence with a real reflow is pinned by
        test_suggestion_list_surgical_height_patch_matches_real_reflow.
        """
        try:
            screen = self.screen
        except NoScreen:
            return False
        compositor = getattr(screen, "_compositor", None)
        if compositor is None:
            return False
        if compositor._full_map_invalidated:
            # A rebuild is already owed; take the guarded full resync instead
            # of patching a map that is about to be recomputed.
            return False
        dirty: list[Region] = []
        patched = False
        # With the mounted loading child, stock layout uses the gutter-shrunk
        # content size for both virtual_size and container_size.
        container_height = outer_height - self.gutter.height
        for node_map in (compositor._full_map, compositor._visible_map):
            if not node_map:
                continue
            geometry = node_map.get(self)
            if geometry is None:
                continue
            patched = True
            region = geometry.region
            if region.height == outer_height:
                continue
            new_region = Region(region.x, region.y + region.height - outer_height, region.width, outer_height)
            virtual_region = geometry.virtual_region
            content_size = Size(geometry.container_size.width, container_height)
            node_map[self] = geometry._replace(
                region=new_region,
                virtual_size=content_size,
                container_size=content_size,
                virtual_region=Region(virtual_region.x, virtual_region.y, virtual_region.width, outer_height),
            )
            dirty.append(geometry.visible_region)
            dirty.append(node_map[self].visible_region)
        if not patched:
            return False
        compositor._visible_widgets = None
        compositor._cuts = None
        compositor._layers = None
        compositor._layers_visible = None
        for dirty_region in dirty:
            if dirty_region:
                compositor._dirty_regions.add(dirty_region)
        geometry = compositor._full_map.get(self) or (compositor._visible_map or {}).get(self)
        if geometry is not None and self._size_updated(
            geometry.region.size, geometry.virtual_size, geometry.container_size, layout=False
        ):
            self.post_message(events.Resize(geometry.region.size, geometry.virtual_size, geometry.container_size))
        self.refresh()
        return True

    def render(self) -> Content:
        """Render the current window; the widget is sized to exactly these rows."""
        if self._loading:
            return Content("")
        visible = self._contents[self._window_start : self._window_start + _SUGGESTION_ROWS]
        rendered: list[Content] = []
        for row_offset, content in enumerate(visible):
            index = self._window_start + row_offset
            selected_content = self._marquee.frame if index == self._highlighted and self._marquee.active else content
            if row_offset == self._hovered_row:
                hover = self.get_component_styles("suggestion-list--option-hover")
                hover_background = Style(background=hover.background)
                rendered.append(
                    selected_content.truncate(self.content_size.width, ellipsis=True, pad=True).stylize(
                        hover_background
                    )
                )
            elif index == self._highlighted:
                rendered.append(selected_content.stylize("reverse"))
            else:
                rendered.append(content)
        return Content.from_text("\n", markup=False).join(rendered)

    def _set_hovered_row(self, row: int | None) -> None:
        """Repaint only when the selectable row beneath the pointer changes."""
        if row == self._hovered_row:
            return
        self._hovered_row = row
        self.refresh()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        """Paint a lightweight hover background without mounting row widgets."""
        offset = event.get_content_offset_capture(self)
        index = self._index_at_y(offset.y) if 0 <= offset.x < self.content_size.width else None
        hovered_row = (
            offset.y if index is not None and not self._disabled[index] and self._values[index] is not None else None
        )
        self._set_hovered_row(hovered_row)

    def on_leave(self, _event: events.Leave) -> None:
        """Clear the row hover when the pointer leaves the popup."""
        self._set_hovered_row(None)

    def _index_at_y(self, y: int) -> int | None:
        """Map a local overlay row to its suggestion index."""
        visible_count = min(_SUGGESTION_ROWS, len(self._contents) - self._window_start)
        index = self._window_start + y
        if y < 0 or y >= visible_count or index >= len(self._values):
            return None
        return index

    def on_click(self, event: events.Click) -> None:
        """Select the clicked visible row without mounting per-option widgets."""
        # Event coordinates are widget-region relative: shift past the border,
        # and reject clicks on the border columns themselves — only the
        # content area maps to rows.
        if event.x < self.gutter.left or event.x >= self.size.width - self.gutter.right:
            return
        index = self._index_at_y(event.y - self.gutter.top)
        if index is None or self._disabled[index]:
            return
        value = self._values[index]
        if value is None:
            return
        event.prevent_default()
        event.stop()
        self._highlighted = index
        self._restart_marquee()
        self.refresh()
        self.post_message(self.Selected(text=value, mode=self._mode, kind=self._kinds[index]))

    def _scroll_window(self, direction: int) -> None:
        """Pan the visible window by one row, clamped at both edges.

        Wheel semantics from the replaced ``OptionList``: scrolling moves the
        viewport without touching the highlight and never wraps — keyboard
        navigation keeps the highlight visible (and wraps) separately.
        """
        max_start = max(0, len(self._contents) - _SUGGESTION_ROWS)
        window_start = min(max(self._window_start + direction, 0), max_start)
        if window_start == self._window_start:
            return
        self._window_start = window_start
        self._hovered_row = None
        self._restart_marquee()
        self.refresh()

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.prevent_default()
        event.stop()
        self._scroll_window(-1)

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.prevent_default()
        event.stop()
        self._scroll_window(1)
