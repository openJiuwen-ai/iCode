# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Field edit dialogs: temporary preview, explicit commit, and cancellation."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.color import Color
from textual.containers import VerticalGroup, VerticalScroll
from textual.geometry import Region
from textual.widget import Widget
from textual.widgets import Button, Label, Tab, Tabs

from chrys.app.tui.theme import concrete_theme_color
from chrys.app.tui.widgets.color_picker import ColorEditContext, ColorPicker
from chrys.app.tui.widgets.color_picker.model import parse_color
from chrys.app.tui.widgets.dialog_buttons import DialogButtonRow, DialogButtonSpec
from chrys.app.tui.widgets.input import EnhancedInput as Input

from . import messages as M
from .palette import PaletteChanged, _ansi_base_token, _AnsiTokenPicker, _DismissableModal, _Xterm256PalettePicker

if TYPE_CHECKING:
    from .editor import ResettableThemeEditor


class _PickerModal(_DismissableModal):
    """One dialog owns one document/field/transaction identity through dismissal."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", show=False),
        Binding("ctrl+enter", "accept", show=False),
        Binding("ctrl+z", "cancel_and_undo", show=False, priority=True),
        Binding("ctrl+y", "cancel_and_redo", show=False, priority=True),
    ]

    def __init__(
        self,
        editor: ResettableThemeEditor,
        *,
        target: str,
        initial: str | None,
        allow_transparent: bool = True,
    ) -> None:
        super().__init__(locale_controller=editor.host.locale_controller)
        self._editor = editor
        self.document = editor.document
        self.token = self.document.begin(target)
        if editor.host.theme_preview is not None:
            editor.host.theme_preview.checkpoint()
        self.context = ColorEditContext(str(self.token.document), self.token.field, str(self.token.transaction))
        if initial is not None:
            variables = editor.host.theme_variables
            name = target.removeprefix("var:")
            background = (
                variables.get(name.removesuffix("-foreground") + "-background", variables["background"])
                if name.endswith("-foreground")
                else variables["background"]
            )
            initial = concrete_theme_color(initial, background, self.document.draft.dark)
        self._current_value = initial
        self._original_value = initial
        self._allow_transparent = allow_transparent
        self._picker_modes = ("ansi", "xterm") if self.document.draft.ansi else ("rgb", "xterm")
        self._picker_mode = self._initial_picker_mode(initial)
        self._picker: Widget | None = None
        self._raw = target.startswith("var:") and initial is not None and parse_color(initial) is None
        self._transaction_closed = False
        self._preview_error = ""
        self._update_mode_classes()

    def _update_mode_classes(self) -> None:
        self.update_classes(
            {f"--{mode}": not self._raw and self._picker_mode == mode for mode in ("rgb", "ansi", "xterm")}
        )
        self.set_class(self._raw, "--raw")
        self.set_class(not self._allow_transparent, "--opaque")

    def _initial_picker_mode(self, value: str | None) -> str:
        if "ansi" in self._picker_modes:
            return "ansi" if _ansi_base_token(value) is not None else "xterm"
        return "rgb"

    def _current_color(self) -> Color:
        return parse_color(self._current_value or "") or Color(255, 255, 255)

    def compose(self) -> ComposeResult:
        with VerticalGroup() as dialog:
            kind, name = self.token.field.split(":", 1)
            dialog.border_title = Text(M.text(self, M.PICK_COLOR if kind == "color" else M.PICK_VARIABLE, name=name))
            with VerticalScroll(id="picker-body"):
                if self._raw:
                    yield Label(Text(M.text(self, M.CSS_HINT)), id="picker-css-hint")
                    yield Input(self._current_value or "", id="css-value")
                else:
                    labels = {"ansi": "ANSI", "xterm": M.text(self, M.PALETTE_256), "rgb": "RGB / HSV"}
                    yield Tabs(
                        *(Tab(Text(labels[mode]), id=f"picker-mode-{mode}") for mode in self._picker_modes),
                        active=f"picker-mode-{self._picker_mode}",
                        id="picker-mode-tabs",
                    )
                    with VerticalGroup(id="picker-host"):
                        yield self._make_picker()
                yield Label(Text(""), id="picker-error")
            yield DialogButtonRow(
                DialogButtonSpec(Text(M.text(self, M.RESTORE)), "picker-restore"),
                DialogButtonSpec(Text(M.text(self, M.CONFIRM)), "picker-confirm", variant="success"),
                DialogButtonSpec(Text(M.text(self, M.CANCEL)), "picker-cancel", variant="warning"),
                id="picker-actions",
            )

    def _make_picker(self) -> Widget:
        if self._picker_mode == "ansi":
            self._picker = _AnsiTokenPicker(self._current_value, context=self.context)
        elif self._picker_mode == "xterm":
            self._picker = _Xterm256PalettePicker(
                self._current_color(),
                allow_transparent=self._allow_transparent,
                disabled_hint=None if self._allow_transparent else M.OPAQUE.bind(),
                context=self.context,
            )
        else:
            background = parse_color(self.app.theme_variables.get("background", "#202020")) or Color(32, 32, 32)
            self._picker = ColorPicker(
                self._current_value or "",
                original=self._original_value,
                seed=self._current_color(),
                background=background,
                allow_alpha=self._allow_transparent,
                context=self.context,
            )
        return self._picker

    def on_mount(self) -> None:
        if not self._composed_live:
            return
        self._update_confirm_state()
        if self._raw:
            self.query_one(Input).focus()
        elif isinstance(self._picker, ColorPicker):
            self._picker.query_one("#color-sv").focus(scroll_visible=False)
        elif self._picker is not None:
            # The palette owns scrolling to its selected cell. Textual's
            # deferred focus-centering of the whole palette would race it.
            self._picker.focus(scroll_visible=False)
            self.call_after_refresh(self._scroll_palette_selection)

    def on_resize(self) -> None:
        self.call_after_refresh(self._scroll_palette_selection)
        if isinstance(self._picker, ColorPicker):
            self.call_after_refresh(self._scroll_rgb_plane, self._picker)

    @on(events.DescendantFocus, "#color-sv")
    def _rgb_plane_focused(self, event: events.DescendantFocus) -> None:
        picker = self._picker
        if isinstance(picker, ColorPicker) and picker.is_mounted and event.widget is picker.query_one("#color-sv"):
            # focus() is deferred; replacement widgets can finish layout before
            # they acquire focus. Use the actual focus event as the boundary.
            self.call_after_refresh(self._scroll_rgb_plane, picker)

    def _scroll_rgb_plane(self, picker: ColorPicker) -> None:
        # A mode switch may replace this picker before layout finishes. Keep
        # its focus visible without a delayed animation moving the next mode.
        if (
            self._transaction_closed
            or self.app.screen is not self
            or picker is not self._picker
            or not picker.is_mounted
        ):
            return
        plane = picker.query_one("#color-sv")
        if plane.has_focus:
            self._scroll_picker_region(plane.parent, plane.virtual_region)

    def _scroll_picker_region(self, parent: object, region: Region) -> None:
        """Translate virtual coordinates into the body, including nested gutters.

        Screen coordinates lag behind scroll offsets until the next layout;
        using them here would double-count movement in a callback burst.
        """
        body = self.query_one("#picker-body", VerticalScroll)
        while isinstance(parent, Widget) and parent is not body:
            region = region.translate(
                parent.virtual_region.offset + parent.styles.gutter.top_left - parent.scroll_offset
            )
            parent = parent.parent
        if parent is body:
            body.scroll_to_region(region, animate=False, immediate=True)

    def refresh_localization(self) -> None:
        if self._transaction_closed:
            return
        kind, name = self.token.field.split(":", 1)
        self.query_one(VerticalGroup).border_title = Text(
            M.text(self, M.PICK_COLOR if kind == "color" else M.PICK_VARIABLE, name=name)
        )
        for name, definition in (("restore", M.RESTORE), ("confirm", M.CONFIRM), ("cancel", M.CANCEL)):
            self.query_one(f"#picker-{name}", Button).label = Text(M.text(self, definition))
        if self._raw:
            self.query_one("#picker-css-hint", Label).update(Text(M.text(self, M.CSS_HINT)))
        else:
            self.query_one("#picker-mode-xterm", Tab).label = Text(M.text(self, M.PALETTE_256))
            if isinstance(self._picker, (ColorPicker, _Xterm256PalettePicker)) and self._picker.is_mounted:
                self._picker.refresh_localization()
        preview = self._editor.host.theme_preview
        if preview is not None:
            self.show_preview_error(preview.error)

    def _scroll_palette_selection(self) -> None:
        if self._transaction_closed or self.app.screen is not self:
            return
        picker = self._picker
        if isinstance(picker, (_AnsiTokenPicker, _Xterm256PalettePicker)) and picker.is_mounted:
            self._scroll_picker_region(picker, picker.selection_region)

    @on(Tabs.TabActivated, "#picker-mode-tabs")
    async def _mode_changed(self, event: Tabs.TabActivated) -> None:
        event.stop()
        mode = (event.tab.id or "").removeprefix("picker-mode-")
        if self._transaction_closed or mode == self._picker_mode:
            return
        self._picker_mode = mode
        self._update_mode_classes()
        await self._replace_picker()

    async def _replace_picker(self) -> None:
        host = self.query_one("#picker-host", VerticalGroup)
        focused = self.focused
        if focused is not None and host in focused.ancestors:
            # Removing the focused picker would refocus the tabs and queue an
            # animated scroll back to them, undoing the new picker's own scroll.
            self.set_focus(self.query_one("#picker-mode-tabs", Tabs), scroll_visible=False)
        await host.remove_children()
        if not self._transaction_closed:
            await host.mount(self._make_picker())
            self.on_mount()

    def _write_value(self, value: str) -> None:
        if not self._transaction_closed and self.document.owns(self.token):
            self._current_value = value
            # The boundary always carries all three identities, rather than
            # recovering a field/document from whichever editor is now active.
            self._editor.preview_edit(self.token, value)

    @on(ColorPicker.Changed)
    def _rgb_changed(self, event: ColorPicker.Changed) -> None:
        event.stop()
        if event.picker is self._picker and event.context == self.context:
            self._write_value(event.value)

    @on(ColorPicker.ValidityChanged)
    def _rgb_validity_changed(self, event: ColorPicker.ValidityChanged) -> None:
        event.stop()
        if event.picker is self._picker and event.context == self.context:
            self._update_confirm_state()

    def show_preview_error(self, error: str) -> None:
        self._preview_error = error
        self.query_one("#picker-error", Label).update(Text(error))
        self._update_confirm_state()

    def _update_confirm_state(self) -> None:
        self.query_one("#picker-confirm", Button).disabled = bool(self._preview_error) or (
            isinstance(self._picker, ColorPicker) and not self._picker.valid
        )

    @on(PaletteChanged)
    def _palette_changed(self, event: PaletteChanged) -> None:
        event.stop()
        if event.picker is self._picker and event.context == self.context:
            self._write_value("transparent" if event.color.is_transparent else event.color.hex)
            self.call_after_refresh(self._scroll_palette_selection)

    @on(_AnsiTokenPicker.Changed)
    def _ansi_changed(self, event: _AnsiTokenPicker.Changed) -> None:
        event.stop()
        if event.picker is self._picker and event.context == self.context:
            self._write_value(event.token)
            self.call_after_refresh(self._scroll_palette_selection)

    @on(Input.Changed, "#css-value")
    def _css_changed(self, event: Input.Changed) -> None:
        event.stop()
        if event.value == event.input.value and event.value != self._current_value:
            self._write_value(event.value)

    @on(Button.Pressed)
    async def _button(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "picker-confirm":
            self.action_accept()
        elif event.button.id == "picker-cancel":
            self.action_close()
        elif event.button.id == "picker-restore":
            self._current_value = self._original_value
            self._editor.restore_transaction(self.token)
            if self._raw:
                self.query_one(Input).value = self._original_value or ""
            else:
                await self._replace_picker()

    def action_accept(self) -> None:
        if isinstance(self._picker, ColorPicker) and not self._picker.valid:
            return
        if not self._transaction_closed and self._editor.commit_edit(self.token):
            self._transaction_closed = True
            super().action_close()

    def action_close(self) -> None:
        if self._transaction_closed:
            return
        self._transaction_closed = True
        self._editor.cancel_edit(self.token)
        super().action_close()

    def action_cancel_and_undo(self) -> None:
        self.action_close()
        self._editor.action_undo()

    def action_cancel_and_redo(self) -> None:
        self.action_close()
        self._editor.action_redo()

    def on_unmount(self) -> None:
        if not self._transaction_closed:
            self._transaction_closed = True
            self._editor.cancel_edit(self.token)
