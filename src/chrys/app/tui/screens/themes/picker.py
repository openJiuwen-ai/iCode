# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ThemesScreen — modal for browsing and selecting themes."""

from __future__ import annotations

from time import monotonic
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import on
from textual.containers import VerticalGroup
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from chrys.app.tui.binding_display import CLOSE_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.theme_loader import theme_is_read_only
from chrys.app.tui.widgets.choice_list import ChoiceList
from chrys.foundation.i18n import msg

from . import messages as M

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.app.tui.i18n import LocaleController

_MANAGE_ID = "__manage_themes__"
_DOUBLE_CLICK_THRESHOLD = 0.4
_THEMES = msg("tui.theme_picker.title", fallback="Themes")


class ThemesScreen(BaseDialog[str | None]):
    """Modal for selecting a theme.

    Single click previews. Double-click/Enter applies and dismisses.
    Escape reverts to the original theme.
    """

    DEFAULT_CSS = """
    ThemesScreen {
        align: center middle;
    }
    ThemesScreen > #container {
        width: 48;
        max-width: 90%;
        max-height: 90%;
        height: auto;
        background: $surface;
        border: round $tui-border-primary $border-opacity;
        border-title-align: left;
        border-title-color: $tui-border-title-primary;
        padding: 0;
        overflow-x: hidden;
    }
    ThemesScreen > #container > OptionList {
        height: auto;
        max-height: 100%;
        border: none;
        padding: 0 0 0 1;
        scrollbar-size: 1 1;
    }
    """

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "cancel", CLOSE_BINDING),
    ]

    def __init__(self, current_theme: str) -> None:
        self._original_theme = current_theme
        self._last_selected: str | None = None
        self._last_selected_time: float = 0
        self._dismissed = False
        self._opening_editor = False
        self._choice_suspended = False
        self._locale_controller: LocaleController | None = None
        super().__init__()

    def compose(self) -> ComposeResult:
        with VerticalGroup(id="container") as container:
            container.border_title = render_str(widget_localizer(self), _THEMES.bind())
            yield ChoiceList()

    def on_mount(self) -> None:
        if not self._composed_live:
            return
        from chrys.app.tui.app import ChrysApp

        if isinstance(self.app, ChrysApp):
            self._locale_controller = self.app.locale_controller
            self._locale_controller.register_surface(self)
            if not self._choice_suspended:
                self.app.suspend_theme_choice()
                self._choice_suspended = True
        ol = self.query_one(OptionList)
        ol.clear_options()
        themes = sorted(self.app.available_themes, key=lambda name: (theme_is_read_only(name), name))
        for i, name in enumerate(themes):
            if i and not theme_is_read_only(themes[i - 1]) and theme_is_read_only(name):
                ol.add_option(None)
            ol.add_option(Option(Text(name), id=name))
            if name == self._original_theme:
                ol.highlighted = i
        ol.add_option(None)
        ol.add_option(Option(Text(M.text(self, M.MANAGE)), id=_MANAGE_ID))

    def refresh_localization(self) -> None:
        self.query_one("#container").border_title = Text(render_str(widget_localizer(self), _THEMES.bind()))
        self.query_one(OptionList).replace_option_prompt(_MANAGE_ID, Text(M.text(self, M.MANAGE)))

    @on(OptionList.OptionHighlighted)
    def _on_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """Preview theme on highlight."""
        # A move typed right before Enter or Escape reports its row after they closed the picker.
        # Previewing it then would replace the choice or the restored theme, and save it.
        if self._dismissed:
            return
        if event.option.id and event.option.id != _MANAGE_ID:
            self.app.theme = event.option.id

    @on(OptionList.OptionSelected)
    def _on_selected(self, event: OptionList.OptionSelected) -> None:
        """Single click previews; double-click/Enter applies and dismisses."""
        if not event.option.id:
            return
        if event.option.id == _MANAGE_ID:
            event.stop()
            self._manage()
            return
        now = monotonic()
        if self._last_selected == event.option.id and (now - self._last_selected_time) < _DOUBLE_CLICK_THRESHOLD:
            event.stop()
            self._safe_dismiss(event.option.id)
        else:
            self._last_selected = event.option.id
            self._last_selected_time = now

    def action_choose(self) -> None:
        """Enter applies the highlighted theme and dismisses, or opens theme management."""
        option = self.query_one(OptionList).highlighted_option
        if option is not None and option.id == _MANAGE_ID:
            self._manage()
        else:
            # The row, not the preview: a move just before Enter has not reported its row yet.
            self._safe_dismiss(option.id if option is not None and option.id else self.app.theme)

    def action_cancel(self) -> None:
        self._safe_dismiss(None, restore_original=True)

    def _dismiss_clicked_outside(self) -> None:
        # Backdrop click is a cancel: restore the previewed theme and close.
        self.action_cancel()

    def _safe_dismiss(self, result: str | None, *, restore_original: bool = False) -> None:
        if self._dismissed:
            return
        # Local guard gates side effects; the mixin guard only protects Textual's dismiss.
        self._dismissed = True
        if restore_original:
            self.app.theme = self._original_theme
        self.dismiss(result)

    def _manage(self) -> None:
        if self._dismissed or self._opening_editor:
            return
        app = self.app
        from chrys.app.tui.screens.main import MainScreen

        main = next(screen for screen in app.screen_stack if isinstance(screen, MainScreen))
        self._opening_editor = True

        async def open_editor() -> None:
            try:
                await main.manage_themes(picker=self)
            finally:
                self._opening_editor = False

        # Keep both message pumps free to finish CSS and mount messages. Main
        # owns the worker because dismissing this picker must not cancel it.
        main.run_worker(open_editor(), name="open-theme-editor", group="theme-editor-open", exclusive=True)

    @property
    def original_theme(self) -> str:
        return self._original_theme

    def dismiss_for_editor(self) -> bool:
        """Restore the applied choice only if this picker still owns the screen."""
        if self._dismissed or self.app.screen is not self:
            return False
        self._safe_dismiss(None, restore_original=True)
        return True

    def _before_dismiss(self, _result: object | None = None) -> None:
        self._release_choice()

    def _release_choice(self) -> None:
        from chrys.app.tui.app import ChrysApp

        if self._choice_suspended and isinstance(self.app, ChrysApp):
            self._choice_suspended = False
            self.app.resume_theme_choice()

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)
        self._release_choice()
