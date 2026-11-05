# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The Settings dialog: a tabbed modal over the layout table."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import on
from textual.containers import VerticalGroup, VerticalScroll
from textual.widgets import Button, Checkbox, Input, Select, TabbedContent, TabPane

from chrys.app.tui.binding_display import CLOSE_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.screens.settings.layout import (
    GENERAL_TAB_ID,
    NOTIFICATIONS_TAB_ID,
    TABS,
    RowKind,
    SettingRowSpec,
    SettingsTab,
    tab_by_id,
)
from chrys.app.tui.screens.settings.panes.notifications import NotificationsPane
from chrys.app.tui.screens.settings.panes.sessions import SessionRootRow
from chrys.app.tui.screens.settings.rows import SettingRow, row_class_for
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.spec import specs_by_key
from chrys.foundation.i18n import msg

_CONTROL_TYPES = (Checkbox, Select, Input, Button)

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.widget import AwaitMount, Widget

    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.screens.settings.ports import SettingsPanelPorts

_TITLE = msg("tui.settings.dialog.title", fallback="Settings")
THEME_PREVIEW_HINT = msg(
    "tui.settings.hint.theme_preview", fallback="Close the theme editor before changing the applied theme."
)
_STATUS_IDLE = msg("tui.settings.status.idle", fallback="Changes are saved as you make them")
_STATUS_RESTART = msg(
    "tui.settings.status.restart",
    fallback="{count} change applies after restart",
    plural_fallback="{count} changes apply after restart",
)
_STATUS_RELOAD_ON_CLOSE = msg("tui.settings.status.reload_on_close", fallback="Changes apply on close (reload)")
_STATUS_RELOAD_AFTER_TURN = msg(
    "tui.settings.status.reload_after_turn",
    fallback="Changes apply when the current turn ends",
)

STATUS_SEPARATOR = " · "


def pane_id(tab_id: str) -> str:
    return f"settings-tab-{tab_id}"


def _tab_for_pane(pane: str | None) -> SettingsTab | None:
    return next((tab for tab in TABS if pane_id(tab.id) == pane), None)


class SettingsDialog(BaseDialog[None]):
    """Tabbed settings modal; every row saves itself through the ports.

    Only the opening tab is composed with the dialog, so its first frame waits
    on that tab's rows alone. The other tabs mount after it, one per frame, and
    a tab activated before its turn mounts as it opens. Rows are reached through
    :meth:`rows`, which lists only rows showing their value: a tab that is not
    mounted yet has nothing to project, commit or relocalize, and reads the
    ports and the locale in force when it mounts.
    """

    CSS_PATH = "settings.tcss"

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "close", CLOSE_BINDING, show=False, priority=True),
    ]

    def __init__(
        self,
        ports: SettingsPanelPorts,
        *,
        initial_tab: str = GENERAL_TAB_ID,
        locale_controller: LocaleController | None = None,
    ) -> None:
        self._ports = ports
        self._initial_tab = initial_tab if tab_by_id(initial_tab) is not None else GENERAL_TAB_ID
        self._locale_controller = locale_controller
        self._specs = specs_by_key(Settings)
        self._mounted_tabs: set[str] = set()
        """Tabs whose content was handed to ``mount``; each tab mounts once."""
        super().__init__()

    # ── composition ─────────────────────────────────────────────────
    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        with VerticalGroup(id="settings-container") as container:
            container.border_title = Text(render_str(localizer, _TITLE.bind()))
            with TabbedContent(id="settings-tabs", initial=pane_id(self._initial_tab)):
                for tab in TABS:
                    with (
                        TabPane(render_str(localizer, tab.title.bind()), id=pane_id(tab.id)),
                        VerticalScroll(classes="settings-pane-scroll"),
                    ):
                        if tab.id == self._initial_tab:
                            self._mounted_tabs.add(tab.id)
                            yield from self._tab_content(tab)

    def _tab_content(self, tab: SettingsTab) -> list[Widget]:
        """Build *tab*'s widgets, titled in the locale in force now."""
        if tab.id == NOTIFICATIONS_TAB_ID:
            return [NotificationsPane(self._ports.notifications())]
        localizer = widget_localizer(self)
        groups: list[Widget] = []
        for section in tab.sections:
            group = VerticalGroup(*(self._build_row(row) for row in section.rows), classes="settings-section")
            group.border_title = Text(render_str(localizer, section.title.bind()))
            groups.append(group)
        return groups

    def _mount_tab(self, tab: SettingsTab) -> AwaitMount | None:
        """Mount *tab*'s content unless it already was or the dialog is closing.

        The content is registered, section groups included, before this
        returns; its rows compose and project as the mount runs.
        """
        if tab.id in self._mounted_tabs or not self._is_open():
            return None
        self._mounted_tabs.add(tab.id)
        scroll = self.query_one(f"#{pane_id(tab.id)}", TabPane).query_one(VerticalScroll)
        return scroll.mount_all(self._tab_content(tab))

    def _is_open(self) -> bool:
        # A dismissed dialog is on its way out: its edits are committed and the
        # ports told, so a tab mounted now is wasted work, and a callback
        # deferred past a refresh may land once its content is removed. Textual
        # also raises on a detached parent and silently drops a mount under a
        # closing one.
        return (
            self.app.is_running and self.is_attached and not (self._closing or self._pruning or self.dismiss_requested)
        )

    async def _prebuild_next_tab(self) -> None:
        """Mount the next tab not mounted yet, then schedule the one after it past the next refresh."""
        tab = next((tab for tab in TABS if tab.id not in self._mounted_tabs), None)
        mounting = None if tab is None else self._mount_tab(tab)
        if mounting is None:
            return
        await mounting
        # Rejected only once this dialog has stopped taking messages; a tab
        # left unmounted then still mounts when it is activated.
        self.call_after_refresh(self._prebuild_next_tab)

    @on(TabbedContent.TabActivated, "#settings-tabs")
    async def _mount_activated_tab(self, event: TabbedContent.TabActivated) -> None:
        # Opened before the prebuild reached it: mount it now rather than after
        # the tabs ahead of it.
        tab = _tab_for_pane(event.pane.id)
        mounting = None if tab is None else self._mount_tab(tab)
        if mounting is not None:
            await mounting

    def _build_row(self, row: SettingRowSpec) -> SettingRow:
        from chrys.app.tui.app import ChrysApp

        spec = self._specs[row.key]
        if row.special is RowKind.SESSION_ROOT:
            return SessionRootRow(spec, row, self._ports)
        widget = row_class_for(spec, row)(spec, row, self._ports)
        if row.key == "ui.theme" and isinstance(self.app, ChrysApp) and self.app.theme_preview is not None:
            widget.read_only_reason = THEME_PREVIEW_HINT.bind()
        return widget

    def on_mount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)
        self.refresh_status()
        # Defer focus until the mounted pane's layout is ready.
        self.call_after_refresh(self._focus_first_control, self._initial_tab)
        # The other tabs start mounting once the opening tab has painted.
        self.call_after_refresh(self._prebuild_next_tab)

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)

    def _focus_first_control(self, tab_id: str) -> None:
        # The focus lands after a refresh, on whichever screen is on top by
        # then: a dialog closed meanwhile takes none. A tab the user picked by
        # then keeps the focus they gave it: focus inside *tab_id*'s pane would
        # make the TabbedContent switch back to it.
        if not self._is_open() or self.query_one("#settings-tabs", TabbedContent).active != pane_id(tab_id):
            return
        panes = self.query(f"#{pane_id(tab_id)}")
        if not panes:
            return
        for widget in panes.first().query("*"):
            if not isinstance(widget, _CONTROL_TYPES):
                continue
            if widget.display and not widget.disabled:
                widget.focus()
                return

    # ── refresh ────────────────────────────────────────────────────
    def rows(self) -> list[SettingRow]:
        """Rows showing their value; a row still mounting projects the ports as it mounts."""
        return [row for row in self.query(SettingRow) if row.projected]

    def _notification_panes(self) -> list[NotificationsPane]:
        return [pane for pane in self.query(NotificationsPane) if pane.projected]

    def reproject(self) -> None:
        """Re-read every value/badge from the ports; controls are not rebuilt."""
        for row in self.rows():
            row.project()
        for pane in self._notification_panes():
            pane.project()
        self.refresh_status()

    def refresh_localization(self) -> None:
        """Replace text in place: tab titles, sections, rows, status."""
        localizer = widget_localizer(self)
        self.query_one("#settings-container", VerticalGroup).border_title = Text(render_str(localizer, _TITLE.bind()))
        tabs = self.query_one("#settings-tabs", TabbedContent)
        for tab in TABS:
            tabs.get_tab(pane_id(tab.id)).label = render_str(localizer, tab.title.bind())
            if tab.custom_pane or tab.id not in self._mounted_tabs:
                # A tab not handed to mount yet titles its sections when it is.
                continue
            pane = self.query_one(f"#{pane_id(tab.id)}", TabPane)
            # Registered with the mount call, so a tab still mounting is retitled too.
            groups = list(pane.query(".settings-section"))
            for group, section in zip(groups, tab.sections, strict=True):
                group.border_title = Text(render_str(localizer, section.title.bind()))
        for row in self.rows():
            row.refresh_localization()
        for notifications in self._notification_panes():
            notifications.refresh_localization()
        self.refresh_status()

    def refresh_status(self) -> None:
        localizer = widget_localizer(self)
        parts: list[str] = []
        pending = len(self._ports.restart_pending_keys())
        if pending:
            parts.append(render_str(localizer, _STATUS_RESTART.bind(count=pending)))
        if self._ports.reload_dirty():
            definition = _STATUS_RELOAD_AFTER_TURN if self._ports.turn_in_progress() else _STATUS_RELOAD_ON_CLOSE
            parts.append(render_str(localizer, definition.bind()))
        text = STATUS_SEPARATOR.join(parts) if parts else render_str(localizer, _STATUS_IDLE.bind())
        container = self.query_one("#settings-container", VerticalGroup)
        container.set_class(bool(parts), "-status-active")
        container.border_subtitle = Text(text)

    # ── close ──────────────────────────────────────────────────────
    def _before_dismiss(self, _result: object | None = None) -> None:
        self.commit_pending()
        self._ports.on_dialog_closed()

    def commit_pending(self) -> None:
        """Commit edits the controls have not reported yet (text being typed).

        Called on close, and by the app before it exits with this dialog on
        top: Ctrl+Q is an app-level priority binding, and by the time the
        dialog unmounts its rows are already gone.
        """
        for row in self.rows():
            row.commit_pending()

    def action_close(self) -> None:
        self.dismiss(None)


__all__ = ["STATUS_SEPARATOR", "SettingsDialog", "pane_id"]
