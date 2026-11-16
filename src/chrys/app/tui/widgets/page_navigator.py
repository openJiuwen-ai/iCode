# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Previous / "Page X of Y" / Next controls shared by paged views."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual import on
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Button, Static

from chrys.app.tui.i18n import render_text, widget_localizer
from chrys.foundation.i18n import msg

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.i18n import MessageRef

PREVIOUS_PAGE = msg("tui.page_navigator.previous", fallback="Previous")
NEXT_PAGE = msg("tui.page_navigator.next", fallback="Next")
PAGE_OF = msg("tui.page_navigator.page", fallback="Page {page} of {pages}")


class PageNavigator(Horizontal):
    """Previous and Next around "Page X of Y", disabled at either end.

    The owner loads the page a :class:`Changed` asks for and reports it
    through :meth:`show`, with a key for what it pages if that can change:
    a :class:`Changed` carries the key shown when its button was pressed,
    so the owner can drop one that reaches it after it replaced what the
    user paged. It moves focus to its content first (see
    :meth:`disables_focused`): disabling a focused boundary button would
    otherwise hand focus to the button's sibling.
    """

    # Ids outrank Button's own `.-style-default` rules, whose tall borders would fill the one-row button.
    DEFAULT_CSS = """
    PageNavigator { width: auto; height: 1; align: right middle; }
    PageNavigator > #previous-page, PageNavigator > #next-page {
        min-width: 10; width: auto; height: 1; border: none; padding: 0 1;
        background: $primary 20%; color: $primary; text-style: none;
    }
    PageNavigator > #previous-page:hover, PageNavigator > #previous-page:focus,
    PageNavigator > #next-page:hover, PageNavigator > #next-page:focus {
        background: $primary 40%; text-style: none;
    }
    PageNavigator > #previous-page:disabled, PageNavigator > #next-page:disabled {
        background: transparent; color: $text-muted;
    }
    PageNavigator > #page-number { width: auto; margin: 0 2; }
    """

    class Changed(Message):
        """The user asked for another page."""

        def __init__(self, navigator: PageNavigator, page: int, view: object = None) -> None:
            super().__init__()
            self.navigator = navigator
            self.page = page
            """1-based."""
            self.view = view
            """The key :meth:`PageNavigator.show` gave for what the pager showed when its button was pressed."""

        @property
        def control(self) -> PageNavigator:
            return self.navigator

    def __init__(self, locale: LocaleController | None = None, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self._locale = locale
        self._page = 1
        self._pages = 1
        self._view: object = None

    @property
    def page(self) -> int:
        return self._page

    @property
    def pages(self) -> int:
        return self._pages

    def compose(self) -> ComposeResult:
        yield Button(self._text(PREVIOUS_PAGE.bind()), id="previous-page", disabled=True)
        yield Static(self._text(PAGE_OF.bind(page=1, pages=1)), id="page-number")
        yield Button(self._text(NEXT_PAGE.bind()), id="next-page", disabled=True)

    def disables_focused(self, page: int, pages: int) -> bool:
        """Whether showing *page* of *pages* would disable the button that has focus."""
        focused = self.screen.focused
        return (page <= 1 and focused is self.query_one("#previous-page", Button)) or (
            page >= pages and focused is self.query_one("#next-page", Button)
        )

    def show(self, page: int, pages: int, *, view: object = None) -> None:
        """Show *page* of *pages*, both 1-based, of what the owner keys as *view*."""
        self._view = view
        if (page, pages) == (self._page, self._pages):
            return
        self._page, self._pages = page, pages
        self.query_one("#previous-page", Button).disabled = page <= 1
        self.query_one("#next-page", Button).disabled = page >= pages
        self.query_one("#page-number", Static).update(self._text(PAGE_OF.bind(page=page, pages=pages)))

    def refresh_localization(self) -> None:
        self.query_one("#previous-page", Button).label = self._text(PREVIOUS_PAGE.bind())
        self.query_one("#next-page", Button).label = self._text(NEXT_PAGE.bind())
        self.query_one("#page-number", Static).update(self._text(PAGE_OF.bind(page=self._page, pages=self._pages)))

    @on(Button.Pressed, "#previous-page")
    @on(Button.Pressed, "#next-page")
    def _on_page_button(self, event: Button.Pressed) -> None:
        event.stop()
        page = self._page + (1 if event.button.id == "next-page" else -1)
        if 1 <= page <= self._pages:
            self.post_message(self.Changed(self, page, self._view))

    def _text(self, reference: MessageRef) -> Text:
        localizer = self._locale.localizer if self._locale is not None else widget_localizer(self)
        return render_text(localizer, reference)
