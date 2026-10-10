# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SettingsDialog behaviour against stub ports."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.containers import VerticalGroup, VerticalScroll
from textual.pilot import Pilot
from textual.widgets import Button, Checkbox, Input, Select, Static, TabbedContent
from textual.widgets._select import SelectOverlay

import chrys.app.tui.screens.settings.dialog as dialog_module
from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.settings import GENERAL_TAB_ID, NOTIFICATIONS_TAB_ID, SettingsDialog
from chrys.app.tui.screens.settings.dialog import pane_id
from chrys.app.tui.screens.settings.panes.notifications import NotificationsPane
from chrys.app.tui.screens.settings.rows import SettingRow
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.spec import SettingOrigin, Source
from chrys.foundation.i18n.formatting import format_message
from tests.app.tui.screens.settings.support import ROW_COUNT, Host, StubPorts, env_origin, wait_for_every_tab
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for, wait_until


def _rows(dialog: SettingsDialog) -> dict[str, SettingRow]:
    return {row.spec.key: row for row in dialog.rows()}


def _hint(row: SettingRow) -> str:
    return str(row.query_one(".settings-row-hint", Static).render())


def _badges(row: SettingRow) -> str:
    return str(row.query_one(".settings-row-badges", Static).render())


async def _open(pilot: Pilot[None], ports: StubPorts, *, initial_tab: str = GENERAL_TAB_ID) -> SettingsDialog:
    """Open the dialog on *initial_tab* and return once every tab shows its rows.

    Only the opening tab mounts with the dialog; the rest mount one per frame
    after it, and ``pilot.pause()`` does not wait for them.
    """
    dialog = SettingsDialog(ports, initial_tab=initial_tab)
    await pilot.app.push_screen(dialog)
    await wait_for_every_tab(dialog, pilot)
    return dialog


async def _focus_rollback_input(dialog: SettingsDialog, pilot: Pilot[None]) -> Input:
    # Mount queues focus after refresh, and Widget.focus queues another app
    # callback. Let both land before an edit can race the initial checkbox.
    initial = _rows(dialog)["session.title.auto"].query_one(Checkbox)
    await wait_for(lambda: dialog.focused is initial, pilot=pilot, description="initial sessions control focus")
    field = _rows(dialog)["rollback.snapshots_keep"].query_one(Input)
    field.focus()
    await wait_for(
        lambda: dialog.focused is field and field.has_focus,
        pilot=pilot,
        description="rollback input focus before editing",
    )
    return field


@pytest.mark.asyncio
async def test_mounting_the_dialog_writes_nothing_and_focuses_a_control() -> None:
    ports = StubPorts()
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports)
        theme = _rows(dialog)["ui.theme"].query_one(Select)
        await wait_for(lambda: dialog.focused is theme, pilot=pilot, description="initial theme control focus")

        # ``_open`` waited for the tabs behind the opening one too; mounting them writes nothing either.
        assert ports.persisted == []
        assert ports.live == []
        assert ports.confirms == []
        assert ports.notification_ports.saved == []
        assert isinstance(dialog.focused, Select)
        rows = dialog.rows()
        assert len(rows) == ROW_COUNT == 35
        assert all(row.spec.key != "trajectory.verify_commands" for row in rows)
        assert dialog.query_one(TabbedContent).active == pane_id(GENERAL_TAB_ID)


@pytest.mark.parametrize("users", [(), ("z-custom", "chrys-copy", "a-custom")])
async def test_settings_theme_choices_group_users_first_with_a_nonselectable_divider(users: tuple[str, ...]) -> None:
    ports = StubPorts(Settings(theme="chrys-legacy"))
    ports.themes = ["textual-dark", *users, "chrys-legacy", "chrys"]
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports)
        selector = _rows(dialog)["ui.theme"].query_one(Select)
        await wait_for(lambda: dialog.focused is selector, pilot=pilot)
        await pilot.press("enter")
        overlay = selector.query_one(SelectOverlay)
        assert [str(option.prompt) for option in overlay.options] == [
            *sorted(users),
            "chrys",
            "chrys-legacy",
            "textual-dark",
        ]
        assert [index for index, option in enumerate(overlay.options) if option._divider] == (
            [len(users) - 1] if users else []
        )
        assert selector.value == "chrys-legacy"
        assert ports.live == []
        if users:
            overlay.select(len(users))
            await pilot.press("up", "enter")
            assert selector.value == "z-custom"
            assert ports.live == [("ui.theme", "z-custom")]


@pytest.mark.asyncio
async def test_opens_on_the_requested_tab_and_falls_back_for_unknown_ids() -> None:
    app = Host()
    # 85% of 46 rows: the tallest tab still fits without scrolling.
    async with app.run_test(size=(100, 46)) as pilot:
        dialog = await _open(pilot, StubPorts(), initial_tab=NOTIFICATIONS_TAB_ID)
        enabled = dialog.query_one("#notifications-enabled", Checkbox)
        # Mount schedules focus after refresh, then Widget.focus queues another
        # app callback. A single Pilot.pause snapshot need not drain both.
        await wait_for(
            lambda: dialog.focused is enabled,
            pilot=pilot,
            description="requested notifications tab control focus",
        )
        assert dialog.query_one(TabbedContent).active == pane_id(NOTIFICATIONS_TAB_ID)
        assert isinstance(dialog.focused, Checkbox)
        pane = dialog.query_one(NotificationsPane)
        assert enabled.value is True
        scroll = pane.query_ancestor(VerticalScroll)
        assert scroll.virtual_size.height <= scroll.container_size.height
        assert pane.query_one("#notifications-test", Button).region.height == 1
        await dialog.dismiss(None)

        dialog = await _open(pilot, StubPorts(), initial_tab="does-not-exist")
        theme = _rows(dialog)["ui.theme"].query_one(Select)
        await wait_for(lambda: dialog.focused is theme, pilot=pilot, description="fallback general tab control focus")
        assert dialog.query_one(TabbedContent).active == pane_id(GENERAL_TAB_ID)


class _HeldFocusDialog(SettingsDialog):
    """Holds the opening focus until the test runs it, as a slow first refresh would."""

    # A relative CSS_PATH resolves next to the defining module.
    CSS_PATH = Path(dialog_module.__file__).with_name("settings.tcss")

    def __init__(self, ports: StubPorts) -> None:
        super().__init__(ports)
        self.held_focus: list[str] = []

    def _focus_first_control(self, tab_id: str) -> None:
        self.held_focus.append(tab_id)

    def run_held_focus(self) -> None:
        for tab_id in self.held_focus:
            super()._focus_first_control(tab_id)


@pytest.mark.asyncio
async def test_a_tab_picked_before_the_opening_focus_lands_stays_active() -> None:
    """The opening focus waits for a refresh; a tab picked before it lands keeps the view.

    Focus inside the opening tab's pane makes the TabbedContent activate that
    pane, so a late opening focus would switch back to it.
    """
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = _HeldFocusDialog(StubPorts())
        await app.push_screen(dialog)
        await wait_for(
            lambda: dialog.held_focus == [GENERAL_TAB_ID], pilot=pilot, description="the opening focus is held"
        )
        tabs = dialog.query_one(TabbedContent)
        await click_when_settled(pilot, f"#--content-tab-{pane_id('tools')}")
        await wait_for(lambda: tabs.active == pane_id("tools"), pilot=pilot, description="the picked tab is active")

        dialog.run_held_focus()
        assert not await wait_until(lambda: tabs.active != pane_id("tools"), pilot=pilot, timeout=0.5)
        assert not isinstance(dialog.focused, Select)

        # Back on the opening tab, the same held focus lands on its first control.
        await click_when_settled(pilot, f"#--content-tab-{pane_id(GENERAL_TAB_ID)}")
        await wait_for(
            lambda: tabs.active == pane_id(GENERAL_TAB_ID), pilot=pilot, description="the opening tab is active again"
        )
        dialog.run_held_focus()
        theme = _rows(dialog)["ui.theme"].query_one(Select)
        await wait_for(lambda: dialog.focused is theme, pilot=pilot, description="the held focus lands on the theme")


@pytest.mark.asyncio
async def test_an_opening_focus_that_lands_after_the_dialog_closed_does_nothing() -> None:
    """Textual runs a pending ``call_after_refresh`` on whichever screen is on top when it lands."""
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = _HeldFocusDialog(StubPorts())
        await app.push_screen(dialog)
        await wait_for(
            lambda: dialog.held_focus == [GENERAL_TAB_ID], pilot=pilot, description="the opening focus is held"
        )
        await dialog.dismiss(None)
        await wait_for(lambda: not dialog.is_attached, pilot=pilot, description="the dialog is removed")
        landed = asyncio.Event()

        app.screen.call_after_refresh(dialog.run_held_focus)
        app.screen.call_after_refresh(landed.set)
        await wait_for(landed.is_set, pilot=pilot, description="the held focus ran on the screen below")

        assert app.is_running


@pytest.mark.asyncio
async def test_bool_row_persists_reload_keys_and_live_applies_live_keys() -> None:
    ports = StubPorts()
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports)
        await pilot.pause()
        rows = _rows(dialog)

        rows["session.title.auto"].query_one(Checkbox).value = False
        await pilot.pause()
        assert ports.persisted == [{"session.title.auto": False}]

        rows["ui.theme"].query_one(Select).value = "textual-dark"
        await pilot.pause()
        assert ports.live == [("ui.theme", "textual-dark")]
        # LIVE keys never go through the persistence queue.
        assert ports.persisted == [{"session.title.auto": False}]


async def test_search_ignore_checkbox_persists_in_tools_tab() -> None:
    ports = StubPorts()
    app = Host()
    async with app.run_test(size=(100, 46)) as pilot:
        dialog = await _open(pilot, ports, initial_tab="tools")
        checkbox = _rows(dialog)["tools.search.respect_gitignore"].query_one(Checkbox)
        await wait_for(lambda: dialog.focused is checkbox, pilot=pilot, description="initial tools control focus")
        assert checkbox.value is True
        assert "Respect .gitignore" in str(checkbox.label)
        checkbox.scroll_visible(animate=False)
        await pilot.pause()
        assert await pilot.click(checkbox)
        await wait_for(
            lambda: ports.persisted == [{"tools.search.respect_gitignore": False}],
            pilot=pilot,
            description="search ignore setting persisted",
        )
        assert ports.live == [] and ports.confirms == []


@pytest.mark.asyncio
async def test_select_row_injects_the_current_value_when_it_is_not_a_choice() -> None:
    ports = StubPorts(Settings(theme="retired-theme", default_agent="ghost"))
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports)
        await pilot.pause()
        rows = _rows(dialog)

        theme = rows["ui.theme"].query_one(Select)
        assert theme.value == "retired-theme"
        assert [str(prompt) for prompt, _value in theme._options] == [
            "retired-theme (current)",
            "chrys",
            "chrys-legacy",
            "textual-dark",
        ]

        agent = rows["agent.default_profile"].query_one(Select)
        assert agent.value == "ghost"
        assert [str(prompt) for prompt, _value in agent._options] == ["(default)", "Code", "Chat", "ghost (current)"]
        assert ports.live == [] and ports.persisted == []

        # Picking a real choice writes it; blank means "use the default".
        agent.value = ""
        await pilot.pause()
        assert ports.persisted == [{"agent.default_profile": ""}]


@pytest.mark.asyncio
async def test_approval_mode_row_hides_bypass_and_confirms_only_the_move_to_auto() -> None:
    ports = StubPorts()
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports, initial_tab="security")
        await pilot.pause()
        rows = _rows(dialog)
        select = rows["approval.default_mode"].query_one(Select)
        assert [value for _prompt, value in select._options] == ["manual", "auto"]

        select.value = "auto"
        await pilot.pause()
        assert len(ports.confirms) == 1
        assert format_message(ports.confirms[0]).startswith("Auto mode lets a model approve")
        assert ports.persisted == [{"approval.default_mode": "auto"}]

        select.value = "manual"
        await pilot.pause()
        assert len(ports.confirms) == 1
        assert ports.persisted[-1] == {"approval.default_mode": "manual"}


@pytest.mark.asyncio
async def test_dangerous_bool_confirms_when_enabling_and_reverts_when_declined() -> None:
    ports = StubPorts()
    ports.confirm_answer = False
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports, initial_tab="security")
        await pilot.pause()
        rows = _rows(dialog)
        checkbox = rows["log.raw_http_capture"].query_one(Checkbox)

        checkbox.value = True
        await pilot.pause()
        assert len(ports.confirms) == 1
        assert ports.persisted == []
        assert checkbox.value is False

        ports.confirm_answer = True
        checkbox.value = True
        await pilot.pause()
        assert len(ports.confirms) == 2
        assert ports.persisted == [{"log.raw_http_capture": True}]

        # Turning it back off is never a dangerous transition.
        checkbox.value = False
        await pilot.pause()
        assert len(ports.confirms) == 2
        assert ports.persisted[-1] == {"log.raw_http_capture": False}


@pytest.mark.parametrize(
    ("key", "tab", "edits"),
    [
        (
            "rollback.snapshots_keep",
            "sessions",
            [
                ("12345", 12345, None, "12345"),
                ("abc", None, "Expected an integer.", "abc"),
                ("", None, "A value is required.", ""),
                ("0", 1, None, "1"),
            ],
        ),
        (
            "llm.retry.max_transient",
            "models",
            [
                ("", None, None, ""),
                ("3", 3, None, "3"),
                ("-1", None, "Expected a non-negative integer.", "-1"),
                ("999", 50, None, "50"),
            ],
        ),
        (
            "approval.timeout_seconds",
            "security",
            [
                ("600", 600, None, "600"),
                ("45", 45, None, "45"),
                ("0", 0, None, "0"),
                ("abc", None, "Expected an integer.", "abc"),
            ],
        ),
        (
            "tools.ask_user.timeout_seconds",
            "tools",
            [
                ("", 900, None, "900"),
                ("0", None, None, "0"),
                ("90", 90, None, "90"),
            ],
        ),
    ],
)
@pytest.mark.asyncio
async def test_input_rows_commit_through_the_field_coercer(
    key: str,
    tab: str,
    edits: list[tuple[str, int | None, str | None, str]],
) -> None:
    ports = StubPorts(Settings(max_transient_retries=9, buddy_model="old-model", ask_user_timeout_seconds=45))
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports, initial_tab=tab)
        await pilot.pause()
        row = _rows(dialog)[key]
        field = row.query_one(Input)
        for typed, expected, error, displayed in edits:
            before = len(ports.persisted)
            field.value = typed
            row.commit_pending()
            await pilot.pause()
            assert ports.persisted[before:] == ([] if error else [{key: expected}]), typed
            hint = row.query_one(".settings-row-hint", Static)
            assert hint.has_class("-error") == (error is not None), typed
            if error:
                assert _hint(row) == error
            assert field.value == displayed, typed


@pytest.mark.asyncio
async def test_closing_commits_a_pending_input_edit_then_tells_the_ports() -> None:
    ports = StubPorts()
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports, initial_tab="sessions")
        await pilot.pause()
        row = _rows(dialog)["rollback.snapshots_keep"]
        row.query_one(Input).value = "7"

        await pilot.press("escape")
        await pilot.pause()

        assert ports.persisted == [{"rollback.snapshots_keep": 7}]
        assert ports.closed == 1


@pytest.mark.asyncio
async def test_greyed_provenance_disables_the_control_and_explains_why() -> None:
    ports = StubPorts(Settings(theme="chrys-legacy"), provenance={"ui.theme": env_origin()})
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports)
        locale = _rows(dialog)["ui.locale"].query_one(Select)
        await wait_for(lambda: dialog.focused is locale, pilot=pilot, description="focus skips disabled theme control")
        row = _rows(dialog)["ui.theme"]

        assert row.query_one(Select).disabled is True
        assert "env" in _badges(row)
        assert _hint(row) == "Set by environment variable CHRYS_THEME."
        assert isinstance(dialog.focused, Select)
        assert dialog.focused is not row.query_one(Select)


@pytest.mark.asyncio
async def test_badges_and_status_follow_apply_kind_and_pending_state() -> None:
    ports = StubPorts()
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports, initial_tab="security")
        await pilot.pause()
        rows = _rows(dialog)
        container = dialog.query_one("#settings-container", VerticalGroup)

        # Rows carry only the danger mark; when a change takes effect is the
        # dialog's status line, which lives in the frame's bottom edge.
        assert _badges(rows["approval.default_mode"]) == "⚠"
        assert _badges(rows["log.raw_http_capture"]) == "⚠"
        assert _badges(rows["otel.enabled"]) == ""
        assert _badges(rows["project.config_enabled"]) == ""
        assert str(container.border_subtitle) == "Changes are saved as you make them"
        assert not container.has_class("-status-active")

        ports.restart_pending = frozenset({"log.raw_http_capture", "otel.enabled"})
        ports.dirty = True
        dialog.reproject()
        await pilot.pause()
        assert _badges(rows["log.raw_http_capture"]) == "⚠"
        assert str(container.border_subtitle) == "2 changes apply after restart · Changes apply on close (reload)"
        assert container.has_class("-status-active")

        ports.turn_running = True
        dialog.refresh_status()
        assert (
            str(container.border_subtitle) == "2 changes apply after restart · Changes apply when the current turn ends"
        )


@pytest.mark.asyncio
async def test_reproject_repaints_values_without_writing() -> None:
    ports = StubPorts()
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports)
        await pilot.pause()
        rows = _rows(dialog)

        ports.desired["session.title.auto"] = False
        ports.install(Settings(theme="textual-dark"))
        dialog.reproject()
        await pilot.pause()

        assert rows["session.title.auto"].query_one(Checkbox).value is False
        assert rows["ui.theme"].query_one(Select).value == "textual-dark"
        assert ports.persisted == [] and ports.live == []


@pytest.mark.asyncio
async def test_the_buddy_model_is_picked_from_the_registered_model_ids() -> None:
    ports = StubPorts()
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports, initial_tab="models")
        await pilot.pause()
        select = _rows(dialog)["model.role.buddy_model_id"].query_one(Select)

        # Blank stores "" and reads "Use active model"; the options are bare model ids.
        assert select.value == ""
        assert [str(prompt) for prompt, _value in select._options] == ["Use active model", "vendor/one", "vendor/two"]
        select.value = "vendor/two"
        await pilot.pause()
        assert ports.persisted == [{"model.role.buddy_model_id": "vendor/two"}]

        # A blank retry cap shows the frontend default it falls back to.
        retries = _rows(dialog)["llm.retry.max_transient"].query_one(Input)
        assert retries.value == "" and retries.placeholder == "10"


@pytest.mark.asyncio
async def test_locale_refresh_repaints_tabs_sections_rows_and_status_in_place() -> None:
    controller = LocaleController(Settings())
    ports = StubPorts()
    app = Host(controller)
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = SettingsDialog(ports, locale_controller=controller)
        await app.push_screen(dialog)
        # Every tab is mounted, so each one is repainted in place rather than built in the new locale.
        await wait_for_every_tab(dialog, pilot)
        rows = _rows(dialog)
        theme_select = rows["ui.theme"].query_one(Select)
        notifications_enabled = dialog.query_one(NotificationsPane).query_one("#notifications-enabled", Checkbox)
        tabs = dialog.query_one(TabbedContent)
        assert str(tabs.get_tab(pane_id(GENERAL_TAB_ID)).label) == "General"

        controller.switch_locale("zh-Hans")
        await pilot.pause()

        assert str(tabs.get_tab(pane_id(GENERAL_TAB_ID)).label) == "通用"
        assert str(tabs.get_tab(pane_id(NOTIFICATIONS_TAB_ID)).label) == "通知"
        assert rows["ui.theme"].query_one(".settings-row-label").render().plain == "主题"
        assert _hint(rows["ui.theme"]) == "用 F9 预览主题。"
        assert str(dialog.query_one("#settings-container", VerticalGroup).border_subtitle) == "修改即时保存"
        assert rows["ui.theme"].query_one(Select) is theme_select
        assert theme_select.value == "chrys"
        assert (
            dialog.query_one(NotificationsPane).query_one("#notifications-enabled", Checkbox) is notifications_enabled
        )
        assert notifications_enabled.label.plain == "启用通知"
        assert ports.live == [] and ports.persisted == []


@pytest.mark.asyncio
async def test_a_projection_landing_mid_edit_does_not_wipe_the_typed_text() -> None:
    ports = StubPorts()
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports, initial_tab="sessions")
        field = await _focus_rollback_input(dialog, pilot)
        field.value = "7777"

        # Another row's write lands and the coordinator reprojects everything.
        ports.desired["storage.session_root_dir"] = "/elsewhere"
        dialog.reproject()
        await pilot.pause()

        assert field.value == "7777"
        assert _rows(dialog)["storage.session_root_dir"].query_one(Input).value == "/elsewhere"

        # An unfocused row is repainted as usual.
        storage = _rows(dialog)["storage.session_root_dir"].query_one(Input)
        storage.focus()
        await wait_for(
            lambda: storage.has_focus and ports.persisted == [{"rollback.snapshots_keep": 7777}],
            pilot=pilot,
            description="rollback edit committed after blur",
        )
        assert ports.persisted == [{"rollback.snapshots_keep": 7777}]
        ports.desired["rollback.snapshots_keep"] = 999
        dialog.reproject()
        await pilot.pause()
        assert field.value == "999"


@pytest.mark.asyncio
async def test_a_failed_write_snaps_a_focused_input_back_to_the_value_in_force() -> None:
    ports = StubPorts()
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports, initial_tab="sessions")
        field = await _focus_rollback_input(dialog, pilot)
        in_force = field.value
        field.value = " 7777 "
        await pilot.press("enter")
        await pilot.pause()
        assert ports.persisted == [{"rollback.snapshots_keep": 7777}]
        assert field.value == "7777"
        assert field.has_focus

        # The write is rejected: the coordinator reprojects without any
        # ``desired`` for the key, and the field follows even though it kept focus.
        dialog.reproject()
        await pilot.pause()

        assert field.value == in_force
        # Leaving the row must not resubmit the rejected value.
        storage = _rows(dialog)["storage.session_root_dir"].query_one(Input)
        storage.focus()
        await wait_for(lambda: storage.has_focus, pilot=pilot, description="leave rejected input")
        await pilot.pause()
        assert ports.persisted == [{"rollback.snapshots_keep": 7777}]


@pytest.mark.asyncio
async def test_locale_refresh_keeps_focus_and_uncommitted_input() -> None:
    controller = LocaleController(Settings())
    ports = StubPorts()
    app = Host(controller)
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = SettingsDialog(ports, initial_tab="sessions", locale_controller=controller)
        await app.push_screen(dialog)
        field = await _focus_rollback_input(dialog, pilot)
        row = _rows(dialog)["rollback.snapshots_keep"]
        field.value = "4242"

        controller.switch_locale("zh-Hans")
        await pilot.pause()

        assert dialog.focused is field
        assert field.value == "4242"
        assert row.query_one(".settings-row-label").render().plain == "保留的回滚快照数"
        assert ports.persisted == []


@pytest.mark.asyncio
async def test_cli_and_sealed_provenance_stay_editable_with_a_badge() -> None:
    from pathlib import Path

    from chrys.foundation.config.coercion import Coerced, CoerceReason, CoerceStatus
    from chrys.foundation.config.settings_store import SettingsWarning

    settings_file = Path("/home/me/.chrys/settings.yaml")
    rejected = SettingsWarning(
        key="log.raw_http_capture",
        origin=SettingOrigin(layer=Source.USER, path=settings_file),
        outcome=Coerced(status=CoerceStatus.INVALID, raw="maybe", reason=CoerceReason.EXPECTED_BOOL),
    )
    ports = StubPorts(
        Settings(default_agent="code", raw_http_capture=False),
        provenance={"agent.default_profile": SettingOrigin(layer=Source.CLI)},
        sealed_keys=frozenset({"log.raw_http_capture"}),
        warnings=(rejected,),
    )
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports, initial_tab="models")
        await pilot.pause()
        agent = _rows(dialog)["agent.default_profile"]
        assert agent.query_one(Select).disabled is False
        assert _badges(agent) == "this session"
        assert _hint(agent).startswith("Set for this session by a command-line option")

        dialog.query_one(TabbedContent).active = pane_id("security")
        await pilot.pause()
        raw = _rows(dialog)["log.raw_http_capture"]
        assert raw.query_one(Checkbox).disabled is False
        assert _badges(raw) == "⚠ · sealed"
        # The hint is the loader's own verdict for the key, not a generic sentence.
        # The path renders in the platform's own spelling (backslashes on Windows).
        assert _hint(raw) == f"Ignoring log.raw_http_capture in {settings_file}=maybe: expected a boolean."


@pytest.mark.asyncio
async def test_an_env_pinned_bypass_shows_as_the_current_choice_without_writing() -> None:
    ports = StubPorts(
        Settings(default_approval_mode="bypass"),
        provenance={"approval.default_mode": env_origin()},
    )
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports, initial_tab="security")
        await pilot.pause()
        select = _rows(dialog)["approval.default_mode"].query_one(Select)

        assert select.disabled is True
        assert select.value == "bypass"
        assert [str(prompt) for prompt, _value in select._options] == ["manual", "auto", "bypass (current)"]
        assert ports.persisted == [] and ports.confirms == []


@pytest.mark.asyncio
async def test_a_dormant_project_file_swaps_the_project_gate_hint_until_the_gate_is_on() -> None:
    from pathlib import Path

    from chrys.foundation.config.settings_store import DormantProjectConfig, LoadedSettings

    ports = StubPorts()
    ports.handle.install(
        LoadedSettings(
            settings=Settings(),
            provenance={},
            dormant_project=(
                DormantProjectConfig(
                    path=Path("/ws/.chrys/settings.yaml"), keys=("rollback.snapshots_keep", "app.dev_mode")
                ),
            ),
        )
    )
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports, initial_tab="security")
        await pilot.pause()
        row = _rows(dialog)["project.config_enabled"]

        assert _hint(row) == "Project settings found (2 keys) — enable to apply them."

        row.query_one(Checkbox).value = True
        await pilot.pause()

        assert ports.persisted == [{"project.config_enabled": True}]
        assert _hint(row).startswith("Let <workspace>/.chrys/settings.yaml")


@pytest.mark.asyncio
async def test_commit_pending_lands_a_focused_edit_without_closing() -> None:
    """The app calls this before exiting with the dialog on top (Ctrl+Q is an app binding)."""
    ports = StubPorts()
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports, initial_tab="sessions")
        field = await _focus_rollback_input(dialog, pilot)
        field.value = "7"

        dialog.commit_pending()
        await pilot.pause()

        assert ports.persisted == [{"rollback.snapshots_keep": 7}]
        assert ports.closed == 0


@pytest.mark.asyncio
async def test_quit_commits_a_settings_dialog_that_sits_under_another_modal() -> None:
    """Ctrl+Q with a confirmation or file picker above the dialog still lands the edit."""
    from types import SimpleNamespace

    from chrys.app.tui.app import ChrysApp

    committed: list[str] = []
    exits: list[bool] = []
    dialog = SettingsDialog.__new__(SettingsDialog)
    dialog.commit_pending = lambda: committed.append("settings")  # type: ignore[method-assign]
    fake_app = SimpleNamespace(
        screen_stack=[object(), dialog, SimpleNamespace(name="confirm-on-top")],
        exit=lambda: exits.append(True),
    )

    await ChrysApp.action_quit(fake_app)  # type: ignore[arg-type]

    assert committed == ["settings"]
    assert exits == [True]


@pytest.mark.asyncio
async def test_a_pane_keeps_its_content_width_when_the_scrollbar_appears() -> None:
    from textual.containers import VerticalScroll

    from tests.support.waiting import wait_for

    ports = StubPorts()
    app = Host()
    async with app.run_test(size=(100, 60)) as pilot:
        dialog = await _open(pilot, ports, initial_tab="models")
        await pilot.pause()
        pane = dialog.query_one(f"#{pane_id('models')}")
        scroll = pane.query_one(VerticalScroll)
        row = _rows(dialog)["agent.default_profile"]
        assert scroll.styles.scrollbar_gutter == "stable"
        await wait_for(
            lambda: row.size.width > 0 and not scroll.show_vertical_scrollbar,
            pilot=pilot,
            description="the models tab to settle without a scrollbar",
        )
        width_without_scrollbar = row.size.width

        await pilot.resize_terminal(100, 16)
        await wait_for(
            lambda: scroll.show_vertical_scrollbar,
            pilot=pilot,
            description="the models tab to overflow and show its scrollbar",
        )

        assert row.size.width == width_without_scrollbar


@pytest.mark.asyncio
async def test_profile_display_names_are_literal_text_not_markup() -> None:
    from textual.widgets._select import SelectCurrent

    ports = StubPorts(Settings(default_agent="ops"))
    ports.agent_profiles = [("ops", "Ops [/oops]"), ("work", "Code [Work]"), ("plain [odd]", "plain [odd]")]
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports, initial_tab="models")
        await pilot.pause()
        select = _rows(dialog)["agent.default_profile"].query_one(Select)

        assert select.value == "ops"
        assert [str(prompt) for prompt, _value in select._options] == [
            "(default)",
            "Ops [/oops]",
            "Code [Work]",
            "plain [odd]",
        ]
        shown = select.query_one(SelectCurrent).query_one("#label", Static)
        assert "Ops [/oops]" in str(shown.render())

        # An unlisted stored value is injected verbatim as well.
        ports.install(Settings(default_agent="ghost [x]"))
        dialog.reproject()
        await pilot.pause()
        assert str(select._options[-1][0]) == "ghost [x] (current)"


@pytest.mark.asyncio
async def test_a_select_shows_its_placeholder_when_the_projected_value_is_blank() -> None:
    """``Select.value`` only notifies on change; a blank projection over new
    options must still repaint the current-value label."""
    from textual.widgets._select import SelectCurrent

    ports = StubPorts(Settings(default_agent=""))
    app = Host()
    async with app.run_test(size=(100, 40)) as pilot:
        dialog = await _open(pilot, ports, initial_tab="models")
        await pilot.pause()
        rows = _rows(dialog)

        def _label(key: str) -> str:
            return str(rows[key].query_one(Select).query_one(SelectCurrent).query_one("#label", Static).render())

        assert _label("model.role.session_title") == "Use active model"
        assert _label("agent.default_profile") == "(default)"

        ports.install(Settings(default_agent="chat"))
        dialog.reproject()
        await pilot.pause()
        assert _label("agent.default_profile") == "Chat"
        assert ports.persisted == [] and ports.live == []
