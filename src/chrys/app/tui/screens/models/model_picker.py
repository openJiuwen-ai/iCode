# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Editable model picker with the form's Select keyboard and overlay conventions."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import on
from textual.actions import SkipAction
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message as UIMessage
from textual.widget import Widget
from textual.widgets import Static
from textual.widgets._select import SelectOverlay
from textual.widgets.option_list import Option

from chrys.app.tui.util.formatting import elide_middle
from chrys.app.tui.widgets import EnhancedInput as Input
from chrys.app.tui.widgets.click_affordance import ClickAffordance
from chrys.app.tui.widgets.select import _LazySelectOverlay
from chrys.service.llm.model_list import ModelInfo

if TYPE_CHECKING:
    from textual import events
    from textual.app import ComposeResult
    from textual.geometry import Region, Size
    from textual.strip import Strip


class _ModelInput(Input):
    """Keep free input while offering the Select keyboard entry point."""

    BINDINGS: ClassVar[list] = [Binding("down", "show_models", show=False)]

    async def _on_key(self, event: events.Key) -> None:
        parent = next((ancestor for ancestor in self.ancestors if isinstance(ancestor, _ModelPicker)), None)
        if event.key == "tab" and isinstance(parent, _ModelPicker) and parent.expanded:
            event.stop()
            event.prevent_default()
            if self.screen.focus_next() is parent.query_one(_ModelOverlay):
                self.screen.focus_next()

    def action_show_models(self) -> None:
        parent = next((ancestor for ancestor in self.ancestors if isinstance(ancestor, _ModelPicker)), None)
        if not isinstance(parent, _ModelPicker) or not parent.models or self.disabled:
            raise SkipAction
        if parent.expanded:
            overlay = parent.query_one(_ModelOverlay)
            overlay.focus(scroll_visible=False)
            overlay.action_cursor_down()
        else:
            parent.set_expanded(True)


class _ModelArrow(ClickAffordance):
    class Toggle(UIMessage):
        """Toggle the model menu without entering the Tab order."""

    CLICK_MESSAGE = Toggle


class _ModelOverlay(_LazySelectOverlay):
    """Display the IDs with matches first; edits belong to the model Input."""

    class EditQuery(UIMessage):
        """Append *text* to the model Input, or delete its last character when *text* is None."""

        def __init__(self, text: str | None) -> None:
            super().__init__()
            self.text = text

    def __init__(self) -> None:
        super().__init__(type_to_search=False)
        self.id = "mc-model-overlay"
        self.model_ids: list[str] = []
        self.bold_text = ""
        self._prompt_width = 256

    @staticmethod
    def _model_prompt(value: str, width: int, bold_text: str) -> Text:
        full = Text(value)
        if bold_text:
            full.highlight_words([bold_text], "bold", case_sensitive=False)
        displayed = elide_middle(value, width)
        if displayed == value:
            return full
        prompt = Text(displayed)
        head, _, tail = displayed.partition("…")
        tail_start = len(value) - len(tail)
        for span in full.spans:
            start, end = max(0, span.start), min(len(head), span.end)
            if start < end:
                prompt.stylize(span.style, start, end)
            start, end = max(tail_start, span.start), min(len(value), span.end)
            if start < end:
                offset = len(head) + 1 - tail_start
                prompt.stylize(span.style, start + offset, end + offset)
        return prompt

    def set_choices(self, model_ids: list[str], bold_text: str) -> None:
        """Show *model_ids* in order, bolding *bold_text* (already case-folded; empty bolds nothing)."""
        self.model_ids = model_ids
        self.bold_text = bold_text
        self.clear_options()
        self.add_options(Option(self._model_prompt(value, self._prompt_width, bold_text)) for value in model_ids)

    def _refresh_prompts(self, content_width: int | None = None) -> None:
        padding = self.get_component_styles("option-list--option").padding.width
        if content_width is None:
            content_width = self.scrollable_content_region.width
        width = max(1, content_width - padding)
        if width == self._prompt_width:
            return
        self._prompt_width = width
        for index, value in enumerate(self.model_ids):
            self.replace_option_prompt_at_index(index, self._model_prompt(value, width, self.bold_text))

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        # Elide before measuring so the opening layout already uses single-line IDs.
        self._refresh_prompts(width - self.scrollbar_size_vertical)
        return super().get_content_height(container, viewport, width)

    def render_lines(self, crop: Region) -> list[Strip]:
        if self.display:
            # Resolve scrollbar space before formatting the first painted rows.
            super()._update_lines()
            self._refresh_prompts()
        return super().render_lines(crop)

    def on_show(self) -> None:
        self._refresh_prompts()
        self.scroll_to_highlight()

    def on_resize(self, _event: events.Resize) -> None:
        if self.display:
            self._refresh_prompts()
            self.scroll_to_highlight()

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "backspace" or (event.character is not None and event.is_printable):
            event.stop()
            event.prevent_default()
            self.post_message(self.EditQuery(None if event.key == "backspace" else event.character))

    def _on_paste(self, event: events.Paste) -> None:
        # Pasting goes to the Input as typing does, with the first line only as Input takes it.
        event.stop()
        if event.text:
            self.post_message(self.EditQuery(event.text.splitlines()[0]))


class _ModelPicker(Vertical):
    """A model Input with the same floating menu as the form's Selects.

    Re-check this adapter together with widgets/select.py on Textual upgrades.
    """

    def __init__(self, placeholder: str) -> None:
        super().__init__(id="mc-model-picker")
        self.placeholder = placeholder
        self.models: list[ModelInfo] = []
        self.expanded = False
        self._ordered_models: list[ModelInfo] = []
        self._query: str | None = None
        self._match_count = 0

    def compose(self) -> ComposeResult:
        with Horizontal(id="mc-model-row"):
            yield _ModelInput(placeholder=self.placeholder, id="mc-model")
            arrow = _ModelArrow("▼", id="mc-model-arrow")
            arrow.display = False
            yield arrow
        yield _ModelOverlay()

    def set_models(self, models: list[ModelInfo]) -> None:
        if models != self.models:
            self.set_expanded(False, refocus=self.expanded)
            self.models = models
            self.query_one("#mc-model-arrow").display = bool(models)
        self._update_choices(force=True)

    def _update_choices(self, *, force: bool = False) -> None:
        """List models containing the input text first, then the rest; the text never hides a model."""
        query = self.query_one(Input).value
        if not force and query == self._query:
            return
        self._query = query
        needle = query.casefold()
        matches = [model for model in self.models if needle in model.id.casefold()]
        self._match_count = len(matches)
        self._ordered_models = matches + [model for model in self.models if needle not in model.id.casefold()]
        overlay = self.query_one(_ModelOverlay)
        # Bold what the order matched (the folded text), and skip the scan when nothing matched.
        overlay.set_choices([model.id for model in self._ordered_models], needle if matches else "")
        if self.expanded:
            overlay.select(self._default_highlight())

    def _default_highlight(self) -> int | None:
        """The current value's row, else the first match; none when nothing matches, so Enter keeps the text."""
        current = self.query_one(Input).value
        exact = next((i for i, model in enumerate(self._ordered_models) if model.id == current), None)
        if exact is not None:
            return exact
        return 0 if self._match_count else None

    def _scroll_for_overlay(self, overlay: _ModelOverlay) -> bool:
        scroll = self.screen.query_one("#mc-scroll", VerticalScroll)
        viewport = self.screen.size
        height = (
            overlay.get_content_height(
                self.content_size, viewport, max(1, self.content_size.width - overlay.styles.gutter.width)
            )
            + overlay.styles.gutter.height
        )
        if maximum := overlay.styles.max_height:
            height = min(height, int(maximum.resolve(self.content_size, viewport)))
        region = self.virtual_region.grow((0, 0, height, 0))
        parent = self.parent
        while isinstance(parent, Widget) and parent is not scroll:
            region = region.translate(
                parent.virtual_region.offset + parent.styles.gutter.top_left - parent.scroll_offset
            )
            parent = parent.parent
        return bool(scroll.scroll_to_region(region, animate=False, x_axis=False, immediate=True))

    def _show_overlay(self) -> None:
        if not self.expanded or not self.is_attached:
            return
        overlay = self.query_one(_ModelOverlay)
        self.add_class("-expanded")
        overlay.add_class("-expanded")
        overlay.focus(scroll_visible=False)
        overlay.select(self._default_highlight())

    def set_expanded(self, expanded: bool, *, refocus: bool = False) -> None:
        expanded = expanded and bool(self.models) and not self.query_one(Input).disabled
        if expanded == self.expanded:
            return
        if expanded:
            self._update_choices()
        self.expanded = expanded
        overlay = self.query_one(_ModelOverlay)
        self.query_one("#mc-model-arrow", Static).update("▲" if expanded else "▼")
        if expanded:
            if self._scroll_for_overlay(overlay):
                # Reflow the scrolled form before the screen-constrained menu is visible.
                if not self.call_after_refresh(self._show_overlay):
                    self._show_overlay()
            else:
                self._show_overlay()
        else:
            self.remove_class("-expanded")
            overlay.remove_class("-expanded")
            if refocus:
                self.query_one(Input).focus(scroll_visible=False)

    @on(Input.Changed, "#mc-model")
    def _query_changed(self, _event: Input.Changed) -> None:
        # Collapsed edits need no rebuild: set_expanded reorders on open.
        if self.expanded:
            self._update_choices()

    @on(Input.Submitted, "#mc-model")
    def _submit_model(self, event: Input.Submitted) -> None:
        if self.expanded:
            event.stop()
            self._accept_model(self.query_one(_ModelOverlay).highlighted)

    def on_descendant_blur(self, event: events.DescendantBlur) -> None:
        if self.expanded and event.widget is self.query_one(Input):
            self.post_message(SelectOverlay.Dismiss(lost_focus=True))

    @on(_ModelOverlay.EditQuery)
    def _edit_query(self, event: _ModelOverlay.EditQuery) -> None:
        event.stop()
        input_widget = self.query_one(Input)
        value = input_widget.value
        input_widget.value = value[:-1] if event.text is None else value + event.text
        input_widget.cursor_position = len(input_widget.value)
        # Reorder now rather than a frame later; the queued Input.Changed then finds the same text and skips.
        self._update_choices()

    @on(_ModelArrow.Toggle)
    def _toggle(self, event: _ModelArrow.Toggle) -> None:
        event.stop()
        self.set_expanded(not self.expanded, refocus=True)

    @on(SelectOverlay.Dismiss)
    def _dismiss_overlay(self, event: SelectOverlay.Dismiss) -> None:
        event.stop()
        if event.lost_focus and self.screen.focused in (self.query_one(Input), self.query_one(_ModelOverlay)):
            return
        self.set_expanded(False, refocus=not event.lost_focus)

    def _accept_model(self, index: int | None) -> None:
        if index is not None and 0 <= index < len(self._ordered_models):
            self.query_one(Input).value = self._ordered_models[index].id
        self.set_expanded(False, refocus=True)

    @on(SelectOverlay.UpdateSelection)
    def _select_model(self, event: SelectOverlay.UpdateSelection) -> None:
        event.stop()
        self._accept_model(event.option_index)
