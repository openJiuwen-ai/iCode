# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent chrome and border behavior for the chat panel."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.style import Style
from rich.text import Text
from textual.content import Content
from textual.message import Message
from textual.widgets import Static

from chrys.app.tui.i18n import render_content, render_str
from chrys.app.tui.util.git_branch import format_workspace_subtitle
from chrys.app.tui.util.logo import CHAT_LOGO
from chrys.app.tui.util.removal import remove_shielded
from chrys.app.tui.widgets.chat.ports import ChatChromeHost
from chrys.app.tui.widgets.chat.waiting_row import WaitingRow
from chrys.app.tui.widgets.click_affordance import ClickAffordance
from chrys.app.tui.widgets.welcome import WelcomeWidget, copy_on_click
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message

if TYPE_CHECKING:
    from textual.events import Click
    from textual.widget import Widget

    from chrys.app.tui.i18n import LocaleController
    from chrys.app.update_check import UpdateNotice

# Border-title budget for the session title fragment; the full title is
# available via the click-to-edit dialog and the F1 sessions modal.
_BORDER_TITLE_MAX_CHARS = 48

_CHAT_SESSION_TITLE = msg("tui.chat.session_title", fallback="Session: {session_id}")
_CHAT_SCROLL_TO_BOTTOM = msg("tui.chat.scroll_to_bottom", fallback="Scroll to bottom {glyph}")
_CHAT_SCROLL_TO_BOTTOM_TOOLTIP = msg(
    "tui.chat.scroll_to_bottom_tooltip",
    fallback="Jump to the bottom of the conversation ({shortcut})",
)
_CHAT_UPDATE_AVAILABLE = msg(
    "tui.chat.update_available", fallback="New version found: {version}\nRun: {command} to upgrade", multiline=True
)


def _truncate_border_title(title: str, max_chars: int = _BORDER_TITLE_MAX_CHARS) -> str:
    """Collapse whitespace and cap the title for the one-line border."""
    title = " ".join(title.split())
    if len(title) <= max_chars:
        return title
    return title[: max_chars - 1].rstrip() + "…"


class ChatBottomSpacer(Static):
    """Flexible trailing spacer that lets short transcript content scroll to top."""

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    ChatBottomSpacer {
        height: 1fr;
        min-height: 0;
        background: transparent;
    }
    """

    def __init__(self) -> None:
        super().__init__("")


class ScrollToBottomRequested(Message):
    """Posted by the floating bottom-jump affordance."""


class ScrollToBottomButton(ClickAffordance):
    """Floating affordance shown when the transcript is not at the bottom."""

    # Floats over the transcript; its label must never join drag selections
    # or copied text, and it must not hijack selection anchor resolution.
    ALLOW_SELECT = False

    CLICK_MESSAGE = ScrollToBottomRequested

    # ``position: absolute`` keeps it out of the transcript flow (no trailing
    # row); show/hide goes through the ``visibility`` rule — a ``display``
    # toggle would force a full screen reflow plus a compositor full-map
    # rebuild mid-scroll (a multi-hundred-ms hitch on large transcripts).
    # Textual's ``visible`` setter itself escalates to a full reflow too, so
    # ``sync_scroll_to_bottom_button`` writes the rule directly. Hidden
    # widgets are skipped by both painting and hit testing, so the invisible
    # button never blocks clicks.
    DEFAULT_CSS = """
    ScrollToBottomButton {
        position: absolute;
        visibility: hidden;
        width: auto;
        min-width: 20;
        height: 1;
        padding: 0 1;
        background: $foreground 8%;
        color: $text-muted;
        text-style: dim;
        content-align: center middle;
        pointer: pointer;
    }
    ScrollToBottomButton:hover {
        background: $foreground 15%;
        color: $accent;
        text-style: not dim;
    }
    """

    def __init__(self, label: Content | None = None, *, tooltip: Content | None = None) -> None:
        if label is None:
            label = Content.from_text(format_message(_CHAT_SCROLL_TO_BOTTOM.bind(glyph="\u2193")), markup=False)
        if tooltip is None:
            tooltip = Content.from_text(
                format_message(_CHAT_SCROLL_TO_BOTTOM_TOOLTIP.bind(shortcut="Ctrl+End")),
                markup=False,
            )
        super().__init__(label)
        self.tooltip = tooltip


class ChatPanelChrome:
    """Own welcome/waiting row/spacer/button infrastructure and border metadata."""

    def __init__(self, host: ChatChromeHost, *, locale_controller: LocaleController | None = None) -> None:
        self._host = host
        self._locale_controller = locale_controller
        self._welcome: WelcomeWidget | None = None
        self._waiting_row: WaitingRow | None = None
        self._bottom_spacer: ChatBottomSpacer | None = None
        self._scroll_to_bottom_button: ScrollToBottomButton | None = None
        self._session_id: str = ""
        self._session_title: str = ""
        self._workspace_cwd: str = ""
        self._workspace_branch: str = ""
        self._shown_subtitle: Text | None = None
        self._subtitle_width: int | None = None
        self._update_notice: UpdateNotice | None = None

    @property
    def welcome(self) -> WelcomeWidget | None:
        """Current welcome widget, if mounted."""
        return self._welcome

    @welcome.setter
    def welcome(self, widget: WelcomeWidget | None) -> None:
        self._welcome = widget

    @property
    def waiting_row(self) -> WaitingRow | None:
        """Current waiting row widget: transcript entries mount above it."""
        return self._waiting_row

    @property
    def bottom_spacer(self) -> ChatBottomSpacer | None:
        """Current bottom spacer widget."""
        return self._bottom_spacer

    @bottom_spacer.setter
    def bottom_spacer(self, widget: ChatBottomSpacer | None) -> None:
        self._bottom_spacer = widget

    @property
    def scroll_to_bottom_button(self) -> ScrollToBottomButton | None:
        """Current bottom-jump button widget."""
        return self._scroll_to_bottom_button

    @scroll_to_bottom_button.setter
    def scroll_to_bottom_button(self, widget: ScrollToBottomButton | None) -> None:
        self._scroll_to_bottom_button = widget

    @property
    def session_id(self) -> str:
        """Full session id backing the border title and click-to-edit dialog."""
        return self._session_id

    @property
    def workspace_cwd(self) -> str:
        """Current workspace cwd metadata."""
        return self._workspace_cwd

    def initial_widgets(
        self,
    ) -> tuple[WelcomeWidget, WaitingRow, ChatBottomSpacer, ScrollToBottomButton]:
        """Create initial persistent chrome widgets in mount order."""
        return self._create_widgets()

    def detach_infrastructure_refs(self) -> None:
        """Forget current chrome widget refs before async child removal."""
        self._welcome = None
        self._waiting_row = None
        self._bottom_spacer = None
        self._scroll_to_bottom_button = None

    def rebuild_widgets(
        self,
    ) -> tuple[WelcomeWidget, WaitingRow, ChatBottomSpacer, ScrollToBottomButton]:
        """Create fresh persistent chrome widgets after transcript clear."""
        return self._create_widgets()

    def _create_widgets(
        self,
    ) -> tuple[WelcomeWidget, WaitingRow, ChatBottomSpacer, ScrollToBottomButton]:
        # Rebuild from panel metadata, which survives transcript and mode changes.
        welcome = WelcomeWidget(
            CHAT_LOGO, title=self._host.profile_name, cwd=self._host.workspace_cwd, notice=self._update_notice_text()
        )
        # Shown only while a running turn waits.
        waiting = WaitingRow()
        waiting.display = False
        spacer = ChatBottomSpacer()
        spacer.display = False
        button = ScrollToBottomButton(
            self._render_content(_CHAT_SCROLL_TO_BOTTOM.bind(glyph="\u2193")),
            tooltip=self._render_content(_CHAT_SCROLL_TO_BOTTOM_TOOLTIP.bind(shortcut="Ctrl+End")),
        )
        self._welcome = welcome
        self._waiting_row = waiting
        self._bottom_spacer = spacer
        self._scroll_to_bottom_button = button
        return welcome, waiting, spacer, button

    async def dismiss_welcome(self) -> None:
        """Remove the welcome widget on first transcript content."""
        welcome = self._welcome
        if welcome is None:
            return
        self._welcome = None
        try:
            await remove_shielded(welcome)
        finally:
            if self._bottom_spacer is not None:
                self._bottom_spacer.display = True

    def update_welcome(self, profile: str = "", cwd: str = "") -> None:
        """Update welcome widget metadata if the widget is still present."""
        if self._welcome is not None:
            self._welcome.update_info(title=profile or None, cwd=cwd or None)

    def set_update_notice(self, notice: UpdateNotice | None) -> None:
        """Show a newer release on this and every later welcome screen, or stop showing one."""
        if notice == self._update_notice:
            return
        self._update_notice = notice
        if self._welcome is not None:
            self._welcome.set_notice(self._update_notice_text())

    def _update_notice_text(self) -> Text | None:
        notice = self._update_notice
        if notice is None:
            return None
        text = Text(self._render_str(_CHAT_UPDATE_AVAILABLE.bind(version=notice.version, command=notice.command)))
        for value, style in (
            (notice.version, Style(bold=True)),
            (notice.command, Style(bold=True) + copy_on_click(notice.command)),
        ):
            start = text.plain.find(value)
            if start >= 0:
                text.stylize(style, start, start + len(value))
        return text

    def set_session_id(self, session_id: str) -> None:
        """Set session id metadata and refresh the border title."""
        self._session_id = session_id
        self._update_border_title()

    def set_session_title(self, title: str) -> None:
        """Set the display title shown next to the session id in the border."""
        self._session_title = title
        self._update_border_title()

    def set_workspace_cwd(self, cwd: str) -> None:
        """Set workspace cwd metadata and refresh the border subtitle."""
        if cwd == self._workspace_cwd:
            return
        self._workspace_cwd = cwd
        self._update_border_subtitle()

    def set_workspace_branch(self, branch: str) -> None:
        """Set workspace git branch metadata and refresh the border subtitle."""
        if branch == self._workspace_branch:
            return
        self._workspace_branch = branch
        self._update_border_subtitle()

    def resize(self) -> None:
        """Rebuild only when the border's cell budget changed."""
        if self._host.border_region().width != self._subtitle_width:
            self._update_border_subtitle()

    def _update_border_title(self) -> None:
        if not self._session_id:
            return
        from chrys.foundation.util.session_ids import session_short_id

        title = self._render_str(_CHAT_SESSION_TITLE.bind(session_id=session_short_id(self._session_id)))
        display_title = _truncate_border_title(self._session_title)
        if display_title:
            title += f" · {display_title}"
        self._host.set_border_title(Text(title))

    def refresh_localization(self) -> None:
        """Retranslate only stored border metadata, persistent button chrome and the update notice."""
        self._update_border_title()
        if self._welcome is not None and self._update_notice is not None:
            self._welcome.set_notice(self._update_notice_text())
        button = self._scroll_to_bottom_button
        if button is not None:
            self._refresh_scroll_to_bottom_button(button)

    def _refresh_scroll_to_bottom_button(self, button: ScrollToBottomButton) -> None:
        button.update(self._render_content(_CHAT_SCROLL_TO_BOTTOM.bind(glyph="\u2193")), layout=False)
        button.tooltip = self._render_content(_CHAT_SCROLL_TO_BOTTOM_TOOLTIP.bind(shortcut="Ctrl+End"))

    def _render_str(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        if controller is None:
            return format_message(reference)
        return render_str(controller.localizer, reference)

    def _render_content(self, reference: MessageRef) -> Content:
        controller = self._locale_controller
        if controller is None:
            return Content.from_text(format_message(reference), markup=False)
        return render_content(controller.localizer, reference)

    def _update_border_subtitle(self) -> None:
        width = self._host.border_region().width
        # Textual reserves two edge cells before the label's four corner/padding cells.
        subtitle = format_workspace_subtitle(
            self._workspace_cwd,
            self._workspace_branch,
            width=max(0, width - 6) if width else None,
        )
        if subtitle == self._shown_subtitle and width == self._subtitle_width:
            return
        self._shown_subtitle = subtitle
        self._subtitle_width = width
        self._host.set_border_subtitle(subtitle or None)

    def is_infrastructure(self, widget: Widget) -> bool:
        """Return whether ``widget`` is persistent chrome infrastructure."""
        return (
            widget is self._welcome
            or widget is self._waiting_row
            or widget is self._bottom_spacer
            or widget is self._scroll_to_bottom_button
        )

    def handle_click(self, event: Click) -> None:
        """Handle border/title/subtitle clicks."""
        target = event.widget
        if self._host.is_ask_user_interactive_target(target):
            event.stop()
            return
        region = self._host.border_region()
        if event.screen_y == region.y and self._session_id:
            # stop() alone only blocks bubbling; prevent_default() is what
            # keeps Widget._on_click from also running (a double-click on
            # the border would otherwise text-select the whole transcript).
            event.prevent_default()
            event.stop()
            self._host.post_title_clicked()
        elif event.screen_y == region.y + region.height - 1 and self._host.has_border_subtitle():
            event.prevent_default()
            event.stop()
            self._host.post_working_dir_clicked()
