# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Man page dialog — displays command help in a modal overlay with pagination."""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.containers import Vertical
from textual.events import Key
from textual.widgets import Static

from chrys.app.tui.binding_display import CLOSE_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, render_text, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.widgets import StableAutoHeightScroll
from chrys.app.tui.widgets.chrome.commands import (
    ManPageHeading,
    ManPageProseBlock,
    ManPageRows,
    ManPageSpec,
    ManPageVerbatimBlock,
)
from chrys.foundation.i18n import msg

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.app.tui.i18n import LocaleController


_FOOTER = msg(
    "tui.man_page.footer",
    fallback="Page {page}/{total}  ↑ Up ↓ Down  Esc to close",
)


class ManPageDialog(BaseDialog[None]):
    """Modal dialog for displaying command help (man pages).

    Supports paginated navigation when multiple commands are available.
    Press Escape or q to close, up/down arrows (or j/k) to navigate between commands.
    """

    _pages: list[ManPageSpec]
    _index: int

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "close", CLOSE_BINDING, show=False, priority=True),
        localized_binding("q", "close", CLOSE_BINDING, show=False),
    ]

    CSS_PATH = "man_page.tcss"

    def __init__(
        self,
        pages: list[ManPageSpec],
        *,
        start_index: int = 0,
        locale_controller: LocaleController | None = None,
    ) -> None:
        """Initialize the man page dialog.

        Args:
            pages: Locale-neutral specifications for all pages.
            start_index: Initial page index to display.
            locale_controller: Optional live locale-switch coordinator.
        """
        self._pages = pages
        self._index = start_index
        self._locale_controller = locale_controller
        super().__init__()

    @property
    def _current_page(self) -> ManPageSpec:
        return self._pages[self._index]

    @property
    def _footer_text(self) -> Text:
        total = len(self._pages)
        idx = self._index + 1
        return render_text(widget_localizer(self), _FOOTER.bind(page=idx, total=total))

    @property
    def _current_title_and_body(self) -> tuple[Text, str]:
        page = self._current_page
        return Text.assemble((f" /{page.name} ", "reverse")), self._render_page(page)

    def _render_page(self, page: ManPageSpec) -> str:
        localizer = widget_localizer(self)
        blocks: list[str] = []
        for segment in page.segments:
            if isinstance(segment, ManPageHeading):
                blocks.append(render_str(localizer, segment.message))
            elif isinstance(segment, ManPageProseBlock):
                prose = render_str(localizer, segment.message)
                blocks.append(textwrap.indent(textwrap.dedent(prose).strip(), "    "))
            elif isinstance(segment, ManPageVerbatimBlock):
                blocks.append(textwrap.indent(textwrap.dedent(segment.text).strip(), " " * segment.indent))
            elif isinstance(segment, ManPageRows):
                rows = "\n".join(prefix + render_str(localizer, reference) for prefix, reference in segment.rows)
                blocks.append(textwrap.indent(rows, " " * segment.indent))
        return "\n".join(blocks)

    def compose(self) -> ComposeResult:
        title, content = self._current_title_and_body
        with Vertical(id="man-container"):
            yield Static(title, id="man-title", markup=False)
            with StableAutoHeightScroll(id="man-scroll"):
                yield Static(content, id="man-content", markup=False)
            yield Static(self._footer_text, id="man-footer")

    def on_mount(self) -> None:
        """Reset scroll to top on mount."""
        if not self._composed_live:
            return
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)
        scroll = self.query_one("#man-scroll", StableAutoHeightScroll)
        scroll.scroll_to(y=0)
        self._update_border_title()

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)

    def refresh_localization(self) -> None:
        """Retranslate the visible page in place without changing scroll or index."""
        self._refresh_page(reset_scroll=False)

    def on_key(self, event: Key) -> None:
        """Handle key events for pagination, regardless of focus."""
        if event.key in ("up", "k"):
            self._navigate(-1)
            event.stop()
        elif event.key in ("down", "j"):
            self._navigate(1)
            event.stop()

    def _navigate(self, delta: int) -> None:
        """Navigate by delta pages."""
        new_index = self._index + delta
        if not (0 <= new_index < len(self._pages)):
            return
        self._index = new_index
        self._refresh_page(reset_scroll=True)

    def _refresh_page(self, *, reset_scroll: bool) -> None:
        """Render the current spec into the existing dialog widgets."""
        title, content = self._current_title_and_body

        title_widget = self.query_one("#man-title", Static)
        title_widget.update(title)
        content_widget = self.query_one("#man-content", Static)
        content_widget.update(content)

        if reset_scroll:
            scroll = self.query_one("#man-scroll", StableAutoHeightScroll)
            scroll.scroll_to(y=0, animate=False)

        footer = self.query_one("#man-footer", Static)
        footer.update(self._footer_text)
        self._update_border_title()

    def _update_border_title(self) -> None:
        """Update the container border title to show current command."""
        container = self.query_one("#man-container", Vertical)
        container.border_title = Text(f"/{self._current_page.name}")

    def action_close(self) -> None:
        """Close the dialog."""
        self.dismiss(None)
