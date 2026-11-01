# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SettingsDialog composes its opening tab alone and mounts the others after it paints."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from rich.console import RenderableType
from textual import events
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Checkbox, Input, Label, TabbedContent, TabPane

import chrys.app.tui.screens.settings.dialog as dialog_module
from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.notifications.settings import NotificationSettings
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.screens.settings import GENERAL_TAB_ID, NOTIFICATIONS_TAB_ID, TABS, SettingsDialog
from chrys.app.tui.screens.settings.dialog import pane_id
from chrys.app.tui.screens.settings.layout import tab_by_id
from chrys.app.tui.screens.settings.panes.notifications import NotificationsPane
from chrys.app.tui.screens.settings.rows import SettingRow
from chrys.app.tui.widgets.chat.messages import AgentMessage, UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.config.settings import Settings
from tests.app.tui.screens.settings.support import Host, StubPorts, every_tab_projected, wait_for_every_tab
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import EmptyAgentRegistry, make_chrys_app
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from chrys.app.tui.screens.settings.layout import SettingRowSpec
    from chrys.foundation.config.spec import SettingSpec

_HELD_ROW = "llm.retry.max_transient"
"""An input row on the models tab, which none of these tests opens on."""


class _Hold:
    """Parks one widget between its compose and the mount of its children.

    Entered inside ``run_test``: leaving releases the widget, so a failing test
    still lets the app shut down rather than wait on the parked mount.
    """

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self.release = asyncio.Event()
        self.widget: Widget | None = None

    async def __aenter__(self) -> _Hold:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        self.release.set()

    def held(self) -> Widget:
        if self.widget is None:
            raise AssertionError("nothing is held")
        return self.widget


def _held[W: Widget](base: type[W], hold: _Hold) -> type[W]:
    class Held(base):  # type: ignore[valid-type,misc]
        async def mount_composed_widgets(self, widgets: list[Widget]) -> None:
            hold.widget = self
            hold.reached.set()
            await hold.release.wait()
            await super().mount_composed_widgets(widgets)

    return Held


def _hold_mounting(monkeypatch: pytest.MonkeyPatch, held: str, hold: _Hold) -> None:
    """Hold the row keyed *held*, or the notifications pane when *held* is its tab id."""
    if held == NOTIFICATIONS_TAB_ID:
        monkeypatch.setattr(dialog_module, "NotificationsPane", _held(NotificationsPane, hold))
        return
    original = dialog_module.row_class_for

    def row_class_for(spec: SettingSpec, row: SettingRowSpec) -> type[SettingRow]:
        row_class = original(spec, row)
        return _held(row_class, hold) if spec.key == held else row_class

    monkeypatch.setattr(dialog_module, "row_class_for", row_class_for)


def _tabs_with_content(dialog: SettingsDialog) -> set[str]:
    """Tabs whose rows or pane have been handed to the DOM."""
    filled: set[str] = set()
    for tab in TABS:
        pane = dialog.query_one(f"#{pane_id(tab.id)}", TabPane)
        if pane.query(SettingRow) or pane.query(NotificationsPane):
            filled.add(tab.id)
    return filled


def _tab_keys(tab_id: str) -> set[str]:
    tab = tab_by_id(tab_id)
    if tab is None:
        raise AssertionError(f"unknown tab {tab_id}")
    return {row.key for section in tab.sections for row in section.rows}


def _projected_keys(dialog: SettingsDialog) -> set[str]:
    return {row.spec.key for row in dialog.rows()}


def _row(dialog: SettingsDialog, key: str) -> SettingRow:
    return next(row for row in dialog.query(SettingRow) if row.spec.key == key)


async def test_an_activated_tab_mounts_as_it_opens_and_the_others_stay_unbuilt() -> None:
    class _NoPrebuild(SettingsDialog):
        # A relative CSS_PATH resolves next to the defining module.
        CSS_PATH = Path(dialog_module.__file__).with_name("settings.tcss")

        async def _prebuild_next_tab(self) -> None:
            return

    ports = StubPorts()
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = _NoPrebuild(ports)
        await app.push_screen(dialog)
        await wait_for(
            lambda: _tab_keys(GENERAL_TAB_ID) <= _projected_keys(dialog),
            pilot=pilot,
            description="opening tab projected",
        )
        assert _tabs_with_content(dialog) == {GENERAL_TAB_ID}
        assert _projected_keys(dialog) == _tab_keys(GENERAL_TAB_ID)

        tabs = dialog.query_one("#settings-tabs", TabbedContent)
        tabs.active = pane_id("tools")
        await wait_for(
            lambda: _tab_keys("tools") <= _projected_keys(dialog),
            pilot=pilot,
            description="activated tools tab projected",
        )
        assert _projected_keys(dialog) == _tab_keys(GENERAL_TAB_ID) | _tab_keys("tools")
        tools_rows = list(dialog.query_one(f"#{pane_id('tools')}", TabPane).query(SettingRow))

        tabs.active = pane_id(NOTIFICATIONS_TAB_ID)
        await wait_for(
            lambda: any(pane.projected for pane in dialog.query(NotificationsPane)),
            pilot=pilot,
            description="activated notifications tab projected",
        )
        tabs.active = pane_id("tools")
        await wait_for(lambda: tabs.active == pane_id("tools"), pilot=pilot, description="tools tab active again")

        assert _tabs_with_content(dialog) == {GENERAL_TAB_ID, "tools", NOTIFICATIONS_TAB_ID}
        # Opening a tab again reuses its rows rather than mounting another set.
        assert list(dialog.query_one(f"#{pane_id('tools')}", TabPane).query(SettingRow)) == tools_rows
        assert ports.persisted == [] and ports.live == [] and ports.notification_ports.saved == []


@pytest.mark.parametrize("held", [_HELD_ROW, NOTIFICATIONS_TAB_ID])
async def test_closing_while_a_later_tab_mounts_commits_the_pending_edit_and_skips_the_unmounted(
    monkeypatch: pytest.MonkeyPatch, held: str
) -> None:
    hold = _Hold()
    _hold_mounting(monkeypatch, held, hold)
    ports = StubPorts()
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot, hold:
        dialog = SettingsDialog(ports, initial_tab="sessions")
        await app.push_screen(dialog)
        # The dialog is busy mounting the held tab from here on: no pilot waits,
        # which would wait for it to go idle.
        await wait_for(hold.reached.is_set, description="prebuild reaches the held tab")
        _row(dialog, "rollback.snapshots_keep").query_one(Input).value = "7"

        app.post_message(events.Key("escape", None))
        await wait_for(lambda: ports.closed == 1, description="dialog closed on escape")
        assert ports.persisted == [{"rollback.snapshots_keep": 7}]

        await wait_for(lambda: hold.held()._pruning, description="held widget removed with the dialog")
        hold.release.set()
        await wait_for(
            lambda: app.screen is not dialog and not dialog.is_attached,
            pilot=pilot,
            description="dialog removed",
        )

        held_widget = hold.held()
        assert isinstance(held_widget, (SettingRow, NotificationsPane))
        assert not held_widget.projected
        assert ports.persisted == [{"rollback.snapshots_keep": 7}]
        assert ports.live == [] and ports.notification_ports.saved == []
        assert ports.closed == 1
        assert app.is_running and app.return_code is None


@pytest.mark.parametrize("held", [_HELD_ROW, NOTIFICATIONS_TAB_ID])
async def test_quitting_while_a_tab_mounts_ends_cleanly_without_projecting_it(
    monkeypatch: pytest.MonkeyPatch, held: str
) -> None:
    """Quit (commit, then exit) with a tab mid-mount: its controls never start, so it must not project."""
    hold = _Hold()
    _hold_mounting(monkeypatch, held, hold)
    app = Host()
    async with app.run_test(size=(100, 40)), hold:
        dialog = SettingsDialog(StubPorts())
        await app.push_screen(dialog)
        await wait_for(hold.reached.is_set, description="prebuild reaches the held tab")
        dialog.commit_pending()
        # The held mount resumes only once the App loop has stopped.
        hold.release.set()
        app.exit()

    held_widget = hold.held()
    assert isinstance(held_widget, (SettingRow, NotificationsPane))
    assert not held_widget.projected
    assert app.return_code == 0


@pytest.mark.parametrize("held", [_HELD_ROW, NOTIFICATIONS_TAB_ID])
async def test_a_tab_mounting_through_a_locale_switch_and_a_reprojection_shows_both(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, held: str
) -> None:
    hold = _Hold()
    _hold_mounting(monkeypatch, held, hold)
    controller = LocaleController(Settings())
    ports = StubPorts()
    app = Host(controller)
    async with app.run_test(size=(100, 40)) as pilot, hold:
        dialog = SettingsDialog(ports, locale_controller=controller)
        await app.push_screen(dialog)
        await wait_for(hold.reached.is_set, description="prebuild reaches the held tab")

        ports.desired[_HELD_ROW] = 5
        ports.notification_ports.settings = NotificationSettings(enabled=False)
        dialog.reproject()
        with caplog.at_level(logging.ERROR):
            controller.switch_locale("zh-Hans")
        dialog.commit_pending()
        hold.release.set()
        await wait_for_every_tab(dialog, pilot)

        models = dialog.query_one(f"#{pane_id('models')}", TabPane)
        assert [str(group.border_title) for group in models.query(".settings-section")] == ["智能体", "模型角色", "LLM"]
        retries = _row(dialog, _HELD_ROW)
        assert retries.query_one(".settings-row-label", Label).render().plain == "瞬时错误最大重试次数"
        assert retries.query_one(Input).value == "5"
        pane = dialog.query_one(NotificationsPane)
        enabled = pane.query_one("#notifications-enabled", Checkbox)
        assert enabled.label.plain == "启用通知"
        assert enabled.value is False
        assert pane.query_one("#notifications-enabled-settings").display is False
        assert not [record for record in caplog.records if "Failed to refresh" in record.getMessage()]
        assert ports.persisted == [] and ports.live == [] and ports.notification_ports.saved == []


class _SettingsRegistry(EmptyAgentRegistry):
    def list_profiles(self, *, include_sub_agent_only: bool = True) -> list[object]:
        return []


async def test_populated_main_screen_f10_paints_the_opening_tab_before_mounting_the_rest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    app = make_chrys_app(tmp_path, agent_registry=_SettingsRegistry())
    async with app.run_test(size=(120, 36)) as pilot:
        main_screen = app.screen
        assert isinstance(main_screen, MainScreen)
        panel = main_screen.query_one(ChatPanel)
        transcript: list[Widget] = []
        for index in range(24):
            transcript.extend(
                (
                    UserMessage(f"Question {index}\nwith a second line"),
                    AgentMessage(f"## Answer {index}\n\nA paragraph with `code` and **emphasis**."),
                )
            )
        await panel.mount(*transcript)
        await wait_for(
            lambda: (
                len(panel.walk_children()) >= 96
                and all(markdown.source for markdown in panel.query(VirtualizedMarkdown))
            ),
            pilot=pilot,
            description="populated transcript composition",
        )
        await wait_for(lambda: screen_is_settled(app, main_screen), pilot=pilot, description="settled underlay layout")

        paints: list[tuple[set[str], int]] = []
        display = app._display

        def record_paint(screen: Screen[object], renderable: RenderableType | None) -> None:
            if isinstance(screen, SettingsDialog) and renderable is not None and not app._batch_count:
                paints.append((_tabs_with_content(screen), len(screen.query(SettingRow))))
            display(screen, renderable)

        monkeypatch.setattr(app, "_display", record_paint)
        style_updates: list[bool] = []
        layout_refreshes: list[None] = []
        footer_recomposes: list[None] = []
        monkeypatch.setattr(main_screen, "update_node_styles", lambda animate=True: style_updates.append(animate))
        monkeypatch.setattr(main_screen, "_refresh_layout", lambda *_args, **_kwargs: layout_refreshes.append(None))

        async def record_footer_recompose() -> None:
            footer_recomposes.append(None)

        monkeypatch.setattr(main_screen.query_one(ChrysFooter), "recompose", record_footer_recompose)

        await pilot.press("f10")
        await wait_for(lambda: bool(paints), pilot=pilot, description="settings dialog painted")
        dialog = app.screen
        assert isinstance(dialog, SettingsDialog)
        # The first frame waits on the opening tab's rows alone; the rest mount after it.
        assert paints[0] == ({GENERAL_TAB_ID}, len(_tab_keys(GENERAL_TAB_ID)))

        await wait_for(lambda: every_tab_projected(dialog), pilot=pilot, description="every settings tab mounted")
        assert _tabs_with_content(dialog) == {tab.id for tab in TABS}
        assert style_updates == []
        assert layout_refreshes == []
        assert footer_recomposes == []
