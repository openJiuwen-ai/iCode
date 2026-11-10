# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reusable controls for answering ``ask_user`` prompts."""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events, on
from textual.containers import HorizontalGroup, VerticalGroup
from textual.content import Content
from textual.css.scalar import Scalar, Unit
from textual.geometry import Region, Size
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, OptionList, SelectionList, TextArea
from textual.widgets.selection_list import Selection

from chrys.app.tui.i18n import render_str, render_text, widget_localizer
from chrys.app.tui.util.source_text import sanitize_source_text
from chrys.app.tui.widgets import EnhancedTextArea
from chrys.app.tui.widgets.checkbox import CHECKED_MARKER, UNCHECKED_MARKER
from chrys.app.tui.widgets.text_area import NEWLINE_SHORTCUT_KEYS
from chrys.foundation.i18n import msg
from chrys.foundation.models.ask_user import AskUserAnswer, AskUserOption

if TYPE_CHECKING:
    from collections.abc import Iterable

    from rich.console import RenderableType
    from textual.app import ComposeResult


ASK_USER_INTERACTIVE_CLASS = "askuser-interactive"
ASK_USER_INPUT_MIN_HEIGHT = 3
ASK_USER_INPUT_MAX_HEIGHT = 7
ASK_USER_BUTTON_ROW_HEIGHT = 3
ASK_USER_DESCRIPTION_GLYPH = "└─"
_ASK_USER_TEXTAREA_FRAME_ROWS = 2
_ASK_USER_LAYOUT_REFRESH_ANCESTORS = 8

_SUBMIT = msg("tui.ask_user.button.submit", fallback="Submit")
_ANSWER_INLINE = msg("tui.ask_user.button.answer_inline", fallback="Answer Inline")
_ANSWER_NEXT = msg("tui.ask_user.button.answer_next", fallback="Answer & Next")
_ANSWER_REVIEW = msg("tui.ask_user.button.answer_review", fallback="Answer & Review")
_SUBMIT_ALL = msg("tui.ask_user.button.submit_all", fallback="Submit answers")
_SUBMIT_ANYWAY = msg("tui.ask_user.button.submit_anyway", fallback="Submit anyway")
_SUBMIT_EMPTY_CONFIRM = msg(
    "tui.ask_user.button.submit_empty_confirm",
    fallback="Press again to submit with no answers",
)
_CUSTOM_RESPONSE_PLACEHOLDER = msg(
    "tui.ask_user.placeholder.custom_response",
    fallback="Type a custom response...",
)

type FooterPosition = Literal["single", "next", "last", "review"]


class AskUserSubmitted(Message):
    """User submitted fixed-length positional answers to an ask-user request."""

    def __init__(self, request_id: str, answers: tuple[AskUserAnswer, ...]) -> None:
        super().__init__()
        self.request_id = request_id
        self.answers = answers


class AskUserInlineRequested(Message):
    """User requested to move the complete prompt state into the transcript."""

    def __init__(self, request_id: str, draft: object) -> None:
        super().__init__()
        self.request_id = request_id
        self.draft = draft


class AskUserOptionChosen(Message):
    """One option button was activated."""

    def __init__(self, request_id: str, question_index: int, option_index: int) -> None:
        super().__init__()
        self.request_id = request_id
        self.question_index = question_index
        self.option_index = option_index


class AskUserFooterAction(Message):
    """The shared footer's primary or inline action was activated."""

    def __init__(self, request_id: str, *, inline: bool = False) -> None:
        super().__init__()
        self.request_id = request_id
        self.inline = inline


class AskUserDraftChanged(Message):
    """The active question's free-text draft changed."""

    def __init__(self, request_id: str, question_index: int, text: str) -> None:
        super().__init__()
        self.request_id = request_id
        self.question_index = question_index
        self.text = text


class AskUserResponseResized(Message):
    """Response input height changed and parent layout should be remeasured."""

    def __init__(self, input_height: int, *, generation: int = 0, question_index: int = 0) -> None:
        super().__init__()
        self.input_height = input_height
        self.generation = generation
        self.question_index = question_index


class AskUserContentResized(Message):
    """A question pane's laid-out height changed; the inline card should remeasure."""


class _AskUserTextArea(EnhancedTextArea):
    """Free-text response input: Enter and explicit newline shortcuts add a line."""

    def __init__(self, *args: Any, defer_layout_to_parent: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.cursor_blink = False
        self._defer_layout_to_parent = defer_layout_to_parent
        self._last_reported_height = 0
        self._last_wrapped_width: int | None = None

    def on_mount(self) -> None:
        self.resize_to_content(rewrap=True)

    def _on_resize(self, event: events.Resize) -> None:
        event.prevent_default()
        if self.wrap_width == self._last_wrapped_width:
            return
        self.resize_to_content(rewrap=True)

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter" or event.key in NEWLINE_SHORTCUT_KEYS:
            event.stop()
            event.prevent_default()
            start, end = self.selection
            if self._replace_via_keyboard("\n", start, end):
                self.scroll_cursor_visible()
            return
        await super()._on_key(event)

    def resize_to_content(self, *, rewrap: bool = False) -> int:
        """Grow the textarea viewport with its wrapped content, up to a cap."""
        wrap_width = self.wrap_width
        if rewrap or wrap_width != self._last_wrapped_width:
            self._last_wrapped_width = wrap_width
            self._rewrap_and_refresh_virtual_size()
        height = min(
            max(self.virtual_size.height + _ASK_USER_TEXTAREA_FRAME_ROWS, ASK_USER_INPUT_MIN_HEIGHT),
            ASK_USER_INPUT_MAX_HEIGHT,
        )
        if height == self._last_reported_height:
            return height
        self._last_reported_height = height
        if self._defer_layout_to_parent:
            self.styles.set_rule("height", Scalar(float(height), Unit.CELLS, Unit.WIDTH))
        else:
            self.styles.height = height
            _refresh_layout_chain(self)
        return height

    def cached_content_height(self) -> int | None:
        """Return the measured height when it still matches the current wrap width."""
        if not self._last_reported_height or self.wrap_width != self._last_wrapped_width:
            return None
        return self._last_reported_height


class _AskUserSelection(Selection[int]):
    """Selection whose prompt keeps the description on its second line."""

    def __init__(self, prompt: Content, value: int, *, initial_state: bool, id: str) -> None:
        super().__init__(prompt, value, initial_state, id)
        # ``Selection`` keeps only the first prompt line; restore the full prompt.
        self._set_prompt(prompt)


class AskUserOptions(SelectionList[int]):
    """Checkbox list of preset answers for one question.

    The prompt owns the selection state: every toggle is reported as
    :class:`AskUserOptionChosen` and the prompt pushes the resulting selection
    back through :meth:`set_selection`, so single-select questions never keep
    two boxes checked.
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = SelectionList.COMPONENT_CLASSES | {
        "askuser-options--marker",
        "askuser-options--marker-selected",
    }

    DEFAULT_CSS = """
    AskUserOptions {
        width: 100%;
        height: auto;
        /* OptionList caps itself at 100% and scrolls; the shared region
           scrolls instead, so lift the cap beyond any terminal. */
        max-height: 100000;
        background: transparent;
        scrollbar-size: 1 1;
        text-wrap: wrap;
        text-overflow: fold;
        & > .askuser-options--marker {
            color: $text-disabled;
        }
        & > .askuser-options--marker-selected {
            color: $success;
        }
    }
    """

    def __init__(
        self,
        request_id: str,
        question_index: int,
        options: tuple[AskUserOption, ...],
        *,
        multi_select: bool = False,
        initial_selection: tuple[int, ...] = (),
    ) -> None:
        self._request_id = request_id
        self._question_index = question_index
        self._presets = options
        self._multi_select = multi_select
        self._locked = False
        self._reveal_on_show = False
        selections = [
            _AskUserSelection(
                self._option_content(option),
                index,
                initial_state=index in initial_selection,
                id=f"askuser-q{question_index}-opt-{index}",
            )
            for index, option in enumerate(options)
        ]
        # ``OptionList`` reserves one separator row after a divided option;
        # ``render_line`` paints that row blank so options are spaced apart.
        for selection in selections[:-1]:
            selection._divider = True
        super().__init__(
            *selections,
            id=f"askuser-q{question_index}-options",
            classes=f"{ASK_USER_INTERACTIVE_CLASS} askuser-options",
            compact=True,
        )

    @staticmethod
    def _option_content(option: AskUserOption) -> Content:
        # Display copies only: the selection still answers with the original ``option.label``.
        content = Content.from_text(sanitize_source_text(option.label), markup=False)
        if option.description:
            description = sanitize_source_text(option.description)
            content = Content.assemble(content, "\n", (f"{ASK_USER_DESCRIPTION_GLYPH} {description}", "dim"))
        return content

    @property
    def selection(self) -> tuple[int, ...]:
        """Return the checked option indexes in check order."""
        return tuple(self.selected)

    @staticmethod
    def _gutter_for_width(width: int) -> int:
        # ``OptionList`` wraps every prompt at the available width minus this
        # gutter, and a zero wrap width is a crash inside the wrapper, so the
        # gutter gives up cells before the prompt loses its last one.
        return min(len(CHECKED_MARKER) + 1, max(0, width - 1))

    def _get_left_gutter_width(self) -> int:
        # A hidden or not-yet-laid-out list has no region to fit; keep the
        # full gutter so out-of-pass renders match what layout will draw.
        width = self.scrollable_content_region.width
        return self._gutter_for_width(width) if width > 0 else len(CHECKED_MARKER) + 1

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        # ``OptionList`` measures wrapped prompts at the full width but renders
        # them right of the marker gutter; measure at the rendered width so the
        # list never scrolls inside itself. The gutter derives from the width
        # being measured, not the current region, which is stale (or empty)
        # during the layout pass that asks.
        return super().get_content_height(container, viewport, max(1, width - self._gutter_for_width(width)))

    def render_line(self, y: int) -> Strip:
        line = OptionList.render_line(self, y)
        try:
            option_index, line_offset = self._lines[self.scroll_offset.y + y]
        except IndexError:
            return line
        if self.get_option_at_index(option_index)._divider and line_offset == self._heights[option_index] - 1:
            # The separator row carries no option meta and no highlight.
            return Strip.blank(
                self.scrollable_content_region.width,
                self.get_visual_style("option-list--option").rich_style,
            )
        segments = list(line)
        base_style = segments[0].style if segments and segments[0].style is not None else self.rich_style
        gutter = self._get_left_gutter_width()
        if line_offset:
            return Strip([Segment(" " * gutter, base_style), *segments])
        checked = self.get_option_at_index(option_index).value in self._selected
        marker_class = "askuser-options--marker-selected" if checked else "askuser-options--marker"
        marker_style = base_style + self.get_component_rich_style(marker_class, partial=True)
        marker = CHECKED_MARKER if checked else UNCHECKED_MARKER
        # The marker is cropped to the gutter it was given, so a shrunken gutter
        # never pushes the prompt text past the region.
        prefix = [Segment(marker[:gutter], marker_style)]
        if gutter > len(marker):
            prefix.append(Segment(" " * (gutter - len(marker)), base_style))
        return Strip([*prefix, *segments])

    def set_selection(self, selection: tuple[int, ...]) -> None:
        """Refresh the pure option view from prompt-owned selection state."""
        wanted = [index for index in selection if index in self._values]
        if list(self._selected) == wanted:
            return
        with self.prevent(self.SelectedChanged):
            self.deselect_all()
            for index in wanted:
                self.select(index)
        self.refresh()

    def focus_option(self, index: int) -> None:
        """Highlight ``index`` (clamped) and take keyboard focus."""
        if self.option_count:
            highlighted = max(0, min(index, self.option_count - 1))
            self.highlighted = highlighted
            # A pane switch may keep the same highlighted index, in which case
            # the reactive watcher does not run and the shared region still
            # needs to reveal the restored option.
            self._reveal_on_show = not bool(self.region)
            self._reveal_option(highlighted)
        # The list deliberately grows to its full content height. Textual's
        # default focus handling tries to center that entire oversized widget,
        # racing the option-level reveal with an animated ancestor scroll.
        self.focus(scroll_visible=False)

    def on_show(self) -> None:
        # A queued pane-focus callback can run before the pane's new layout,
        # when its option region is still empty. Show arrives after layout;
        # complete only that pending reveal, without scrolling unfocused lists
        # merely because an ancestor becomes visible again.
        if self._reveal_on_show and self.region:
            self._reveal_on_show = False
            if self.highlighted is not None:
                self._reveal_option(self.highlighted)

    def disable_controls(self) -> None:
        """Disable controls after the whole prompt is submitted."""
        self._locked = True
        self.disabled = True

    def _on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        # ``SelectionList`` toggles whatever is highlighted when this queued
        # message runs; keys typed at once (a lagging TUI) may have moved the
        # highlight by then. Toggle the option Space, Enter or the click chose,
        # and keep the base handler from toggling a second one.
        event.stop()
        event.prevent_default()
        self.toggle(self.get_option_at_index(event.option_index))

    @on(SelectionList.SelectionToggled)
    def _on_selection_toggled(self, event: SelectionList.SelectionToggled) -> None:
        event.stop()
        if self._locked:
            return
        index = event.selection_index
        if not self._multi_select and index in self._selected:
            with self.prevent(self.SelectedChanged):
                for value in [value for value in self._selected if value != index]:
                    self.deselect(value)
        self.post_message(AskUserOptionChosen(self._request_id, self._question_index, index))

    @on(SelectionList.SelectionHighlighted)
    def _on_selection_highlighted(self, event: SelectionList.SelectionHighlighted) -> None:
        self._reveal_option(event.selection_index)

    def _reveal_option(self, index: int) -> None:
        """Scroll every ancestor so the option's own lines are visible.

        ``OptionList`` only scrolls itself; the list sits inside the prompt's
        scroll region, which has to follow keyboard highlight moves as well.
        """
        if not self.is_mounted:
            return
        lines = [line for line, (option_index, _offset) in enumerate(self._lines) if option_index == index]
        if not lines:
            return
        offset = self.virtual_region.offset
        region = Region(offset.x, offset.y + lines[0], self.virtual_region.width, len(lines))
        widget: Widget = self
        while isinstance(container := widget.parent, Widget):
            if not region:
                break
            scrolled = container.scroll_to_region(region, animate=False, x_axis=False, immediate=True)
            region = (
                (
                    region.translate(-scrolled)
                    .translate(container.styles.margin.top_left)
                    .translate(container.styles.border.spacing.top_left)
                    .translate(container.virtual_region_with_margin.offset)
                )
                .grow(container.styles.margin)
                .intersection(container.virtual_region_with_margin)
            )
            widget = container


class AskUserResponseFooter(VerticalGroup):
    """One shared custom-response input and action row for the active pane."""

    def __init__(
        self,
        request_id: str,
        *,
        allow_inline: bool = False,
        initial_response: str = "",
        defer_layout_to_parent: bool = False,
    ) -> None:
        super().__init__(id="askuser-footer", classes=ASK_USER_INTERACTIVE_CLASS)
        self._request_id = request_id
        self._allow_inline = allow_inline
        self._initial_response = initial_response
        self._defer_layout_to_parent = defer_layout_to_parent
        self._locked = False
        self._last_input_height = 0
        self._last_footer_height = 0
        self._question_index = 0
        self._position: FooterPosition = "single"
        self._has_selection = False
        self._generation = 0
        self._answered_count = 0
        self._question_count = 1
        self._armed_empty_submit = False

    def on_mount(self) -> None:
        self.call_after_refresh(self._capture_initial_input_height)

    def _capture_initial_input_height(self) -> None:
        with suppress(Exception):
            self._last_input_height = self.measure_input_height()

    def on_resize(self, event: events.Resize) -> None:
        if event.size.height == self._last_footer_height:
            return
        self._last_footer_height = event.size.height
        with suppress(Exception):
            text_area = self.query_one("#askuser-input", _AskUserTextArea)
            input_height = text_area.cached_content_height()
            if input_height is None:
                input_height = text_area.resize_to_content(rewrap=True)
            self.notify_input_resized(input_height)

    def compose(self) -> ComposeResult:
        text_area = _AskUserTextArea(
            text=self._initial_response,
            id="askuser-input",
            compact=True,
            soft_wrap=True,
            show_line_numbers=False,
            defer_layout_to_parent=self._defer_layout_to_parent,
        )
        text_area.placeholder = render_str(widget_localizer(self), _CUSTOM_RESPONSE_PLACEHOLDER.bind())
        yield text_area
        with HorizontalGroup(id="askuser-buttons"):
            yield Button(
                render_text(widget_localizer(self), _SUBMIT.bind()),
                id="askuser-submit",
                variant="success",
                flat=True,
            )
            if self._allow_inline:
                yield Button(
                    render_text(widget_localizer(self), _ANSWER_INLINE.bind()),
                    id="askuser-inline",
                    variant="warning",
                    flat=True,
                )

    def set_context(
        self,
        question_index: int,
        draft: str,
        *,
        has_selection: bool,
        position: FooterPosition,
        generation: int = 0,
        answered_count: int = 0,
        question_count: int = 1,
        armed_empty_submit: bool = False,
    ) -> None:
        """Rebind the shared footer after its old draft has been captured."""
        question_changed = question_index != self._question_index
        self._question_index = question_index
        self._position = position
        self._has_selection = has_selection
        self._generation = generation
        self._answered_count = answered_count
        self._question_count = question_count
        self._armed_empty_submit = armed_empty_submit
        with suppress(Exception):
            text_area = self.query_one("#askuser-input", _AskUserTextArea)
            # The textarea is the source of truth for the active question's
            # draft: DraftChanged snapshots are queue-delayed, so writing one
            # back would clobber keystrokes that landed while it was in flight
            # (and load_text resets the cursor to (0, 0), breaking IME input).
            if question_changed and text_area.text != draft:
                text_area.load_text(draft)
            text_area.display = position != "review"
            self.sync_response_state(text_area)
        self._refresh_buttons()

    def _submit_message(self):
        if self._position == "next":
            return _ANSWER_NEXT
        if self._position == "last":
            return _ANSWER_REVIEW
        if self._position == "review":
            if self._armed_empty_submit:
                return _SUBMIT_EMPTY_CONFIRM
            if self._answered_count < self._question_count:
                return _SUBMIT_ANYWAY
            return _SUBMIT_ALL
        return _SUBMIT

    def _refresh_buttons(self) -> None:
        with suppress(Exception):
            submit = self.query_one("#askuser-submit", Button)
            label = render_text(widget_localizer(self), self._submit_message().bind())
            current = submit.label
            if not isinstance(current, Content) or current.plain != label.plain:
                submit.label = label
                # Button.label repaints without relayout; an auto width must grow.
                submit.refresh(layout=True)
            submit.disabled = self._locked or (
                self._position != "review" and not self._has_selection and not bool(self.draft_text.strip())
            )
        with suppress(Exception):
            self.query_one("#askuser-inline", Button).display = self._position != "review"

    @on(TextArea.Changed, "#askuser-input")
    def _on_response_changed(self, event: TextArea.Changed) -> None:
        self.sync_response_state(event.text_area)
        self.post_message(AskUserDraftChanged(self._request_id, self._question_index, event.text_area.text))

    def sync_response_state(self, text_area: TextArea | None = None) -> None:
        """Synchronize layout and primary-action availability."""
        if text_area is None:
            text_area = self.query_one("#askuser-input", _AskUserTextArea)
        if isinstance(text_area, _AskUserTextArea):
            input_height = text_area.resize_to_content()
            self.notify_input_resized(input_height)
        if not self._locked:
            self._refresh_buttons()

    def measure_input_height(self) -> int:
        """Measure the response input after TextArea has applied its edit."""
        return self.query_one("#askuser-input", _AskUserTextArea).resize_to_content()

    def notify_input_resized(self, input_height: int) -> None:
        """Notify parent renderers when the input height actually changes."""
        if input_height == self._last_input_height:
            return
        self._last_input_height = input_height
        self.post_message(
            AskUserResponseResized(
                input_height,
                generation=self._generation,
                question_index=self._question_index,
            )
        )

    def focus_input(self) -> None:
        """Focus the custom response input if it is mounted."""
        with suppress(Exception):
            self.query_one("#askuser-input", _AskUserTextArea).focus()

    def focus_submit(self) -> None:
        """Focus the global submit action on the review pane."""
        with suppress(Exception):
            self.query_one("#askuser-submit", Button).focus()

    @property
    def draft_text(self) -> str:
        """Return the current custom-response text without submitting it."""
        with suppress(Exception):
            return self.query_one("#askuser-input", _AskUserTextArea).text
        return ""

    def disable_controls(self) -> None:
        """Disable controls after a response path has been chosen."""
        self._locked = True
        with suppress(Exception):
            self.query_one("#askuser-input", _AskUserTextArea).disabled = True
        for button in self.query(Button):
            button.disabled = True

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id not in {"askuser-submit", "askuser-inline"}:
            return
        event.stop()
        if self._locked:
            return
        self.post_message(AskUserFooterAction(self._request_id, inline=button_id == "askuser-inline"))


def _refresh_layout_chain(widget: Widget) -> None:
    target = widget
    for _ in range(_ASK_USER_LAYOUT_REFRESH_ANCESTORS - 1):
        parent = target.parent
        if not isinstance(parent, Widget):
            break
        target = parent
    target.refresh(layout=True)


def ask_user_hanging_grid(rows: Iterable[tuple[str | Text, RenderableType]]) -> Table:
    """Lay ``(gutter, body)`` rows out so wrapped body lines hang under the body.

    The gutter column is as wide as its widest entry and never wraps; the
    body column takes the remaining width, so a question or answer that soft
    wraps keeps every continuation line aligned with its first character
    instead of falling back to the margin.
    """
    grid = Table.grid(padding=0, expand=True)
    grid.add_column(no_wrap=True)
    grid.add_column(ratio=1, overflow="fold")
    for gutter, body in rows:
        grid.add_row(gutter if isinstance(gutter, Text) else Text(gutter), body)
    return grid


def ask_user_hanging_answer(answer: AskUserAnswer, *, unanswered: str, style: Style | None = None) -> Table:
    """Hang *answer* under the description glyph, with its note dimmed below.

    *style* colours the whole answer block (glyph included); the note and the
    unanswered placeholder are dimmed on top of it.
    """
    base = style or Style()
    faint = base + Style(dim=True)
    values = (
        Text(sanitize_source_text(", ".join(answer.values)), style=base)
        if answer.values
        else Text(unanswered, style=faint)
    )
    rows: list[tuple[str | Text, RenderableType]] = [(Text(f"{ASK_USER_DESCRIPTION_GLYPH} ", style=base), values)]
    if answer.note:
        rows.append(("", Text(sanitize_source_text(answer.note), style=faint)))
    return ask_user_hanging_grid(rows)
