# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the no-preview language picker and slash command."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from textual.app import App
from textual.containers import VerticalGroup
from textual.events import Key
from textual.widgets import OptionList

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.i18n import LocaleController, render_content, render_str
from chrys.app.tui.language import LANGUAGE_OPTIONS, LANGUAGE_PICKER_TITLE, LANGUAGE_UNKNOWN_LOCALE
from chrys.app.tui.screens.main.buddy_command import BuddyCommandController
from chrys.app.tui.screens.main.commands import MainSlashCommandRegistry
from chrys.app.tui.screens.main.navigation import MainNavigationController
from chrys.app.tui.screens.main.state import MainScreenServices
from chrys.app.tui.screens.menus.languages import LanguagesScreen
from chrys.foundation.config.settings import DEFAULT_LOCALE, Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.i18n import Localizer, MessageRef
from chrys.foundation.i18n.formatting import format_message
from tests.support.waiting import wait_for


class _PickerApp(App[None]):
    def __init__(
        self,
        controller: LocaleController,
        *,
        apply_result: Callable[[str], None] | None = None,
    ) -> None:
        self.locale_controller = controller
        self.results: list[str | None] = []
        self._apply_result = apply_result
        super().__init__()

    def on_mount(self) -> None:
        self.push_screen(
            LanguagesScreen(self.locale_controller.requested_locale),
            self._on_result,
        )

    def _on_result(self, result: str | None) -> None:
        self.results.append(result)
        if result is not None and self._apply_result is not None:
            self._apply_result(result)


class _NavigationView:
    def __init__(self) -> None:
        self.language_requests: list[tuple[str, Callable[[str | None], None]]] = []
        self.notifications: list[tuple[str, str, str]] = []

    def open_language_picker(self, current_locale: str, on_result: object) -> None:
        self.language_requests.append((current_locale, on_result))  # type: ignore[arg-type]

    def notify(
        self,
        message: MessageRef | str,
        *,
        title: MessageRef | str,
        severity: str = "information",
        timeout: float | None = 3,
    ) -> None:
        del timeout
        display_message = message if isinstance(message, str) else format_message(message)
        display_title = title if isinstance(title, str) else format_message(title)
        self.notifications.append((display_message, display_title, severity))


async def _unused_session_operation(_session_id: str) -> None:
    return None


async def _unused_flush() -> None:
    return None


def _new_navigation(controller: LocaleController, view: _NavigationView) -> MainNavigationController:
    return MainNavigationController(
        services=MainScreenServices(bus=EventBus()),
        view=view,  # type: ignore[arg-type]
        is_agent_loading=lambda: False,
        is_agent_running=lambda: False,
        is_submit_pending=lambda: False,
        is_workflow_mode=lambda: False,
        has_messages=lambda: True,
        is_dashboard_visible=lambda: False,
        set_interrupt_confirm_active=lambda _active: None,
        publish_interrupt=lambda: None,
        dismiss_suggestions=lambda: False,
        cancel_pending_injection=lambda: False,
        delete_current_and_new=_unused_session_operation,
        restore_session=_unused_session_operation,
        flush_notifications=_unused_flush,
        start_worker=lambda _work: None,
        debug=lambda _key, _message: None,
        locale_controller=controller,
    )


def _option_prompts(options: OptionList) -> list[str]:
    return [options.get_option_at_index(index).prompt.plain for index in range(options.option_count)]


def _language_command(actions: MagicMock):
    commands = MainSlashCommandRegistry(
        actions=actions,
        buddy=BuddyCommandController(MagicMock()),
    ).build()
    return next(command for command in commands if command.name == "language")


@pytest.fixture(autouse=True)
def isolated_locale_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHRYS_LOCALE", raising=False)


@pytest.mark.asyncio
async def test_picker_resolves_title_labels_and_initial_highlight_at_mount() -> None:
    controller = LocaleController(Settings(locale="zh-Hans"))

    async with _PickerApp(controller).run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        options = screen.query_one(OptionList)

        assert isinstance(screen, LanguagesScreen)
        assert screen.query_one("#container", VerticalGroup).border_title == "显示语言"
        assert _option_prompts(options) == ["跟随系统", "English", "简体中文"]
        assert options.highlighted == 2


@pytest.mark.asyncio
async def test_picker_highlight_has_no_switch_persistence_or_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = LocaleController(Settings(locale=DEFAULT_LOCALE))
    switch_calls: list[str] = []
    persistence_calls: list[str] = []
    monkeypatch.setattr(controller, "switch_locale", switch_calls.append)
    monkeypatch.setattr(tui_i18n, "persist_locale", persistence_calls.append)

    async with _PickerApp(controller).run_test() as pilot:
        options = pilot.app.screen.query_one(OptionList)
        options.highlighted = 2
        await pilot.pause()

        assert pilot.app.results == []
        assert switch_calls == []
        assert persistence_calls == []
        assert controller.revision == 0


@pytest.mark.asyncio
async def test_picker_escape_dismisses_without_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = LocaleController(Settings(locale=DEFAULT_LOCALE))
    switch_calls: list[str] = []
    persistence_calls: list[str] = []
    monkeypatch.setattr(controller, "switch_locale", switch_calls.append)
    monkeypatch.setattr(tui_i18n, "persist_locale", persistence_calls.append)

    async with _PickerApp(controller).run_test() as pilot:
        await pilot.press("escape")
        await pilot.pause()

        assert pilot.app.results == [None]
        assert switch_calls == []
        assert persistence_calls == []
        assert controller.revision == 0


@pytest.mark.asyncio
async def test_picker_backdrop_dismisses_without_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = LocaleController(Settings(locale=DEFAULT_LOCALE))
    switch_calls: list[str] = []
    persistence_calls: list[str] = []
    monkeypatch.setattr(controller, "switch_locale", switch_calls.append)
    monkeypatch.setattr(tui_i18n, "persist_locale", persistence_calls.append)

    async with _PickerApp(controller).run_test() as pilot:
        screen = pilot.app.screen
        screen._dismiss_clicked_outside()
        await pilot.pause()

        assert pilot.app.results == [None]
        assert switch_calls == []
        assert persistence_calls == []
        assert controller.revision == 0


@pytest.mark.asyncio
async def test_picker_enter_applies_highlight_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = LocaleController(Settings(locale="en"))
    original_switch = controller.switch_locale
    switch_calls: list[str] = []
    persistence_calls: list[str] = []

    def switch_locale(requested_locale: str) -> None:
        switch_calls.append(requested_locale)
        original_switch(requested_locale)

    monkeypatch.setattr(controller, "switch_locale", switch_locale)
    monkeypatch.setattr(tui_i18n, "persist_locale", persistence_calls.append)

    async with _PickerApp(controller, apply_result=controller.switch_locale).run_test() as pilot:
        options = pilot.app.screen.query_one(OptionList)
        options.highlighted = 2
        await pilot.press("enter")
        await pilot.pause()

        assert pilot.app.results == ["zh-Hans"]
        assert switch_calls == ["zh-Hans"]
        assert persistence_calls == ["zh-Hans"]
        assert controller.revision == 1


@pytest.mark.asyncio
async def test_picker_enter_typed_right_after_a_move_confirms_the_row_moved_to() -> None:
    """A terminal hands over what was typed while the loop was busy back to back: Enter follows the move."""
    controller = LocaleController(Settings(locale="en"))
    async with _PickerApp(controller).run_test() as pilot:
        options = pilot.app.screen.query_one(OptionList)
        await wait_for(lambda: options.has_focus, pilot=pilot, description="the choices focused")
        assert options.highlighted is not None
        moved_to = options.get_option_at_index(options.highlighted + 1).id
        assert moved_to and moved_to != "en"

        pilot.app.post_message(Key("down", None))
        pilot.app.post_message(Key("enter", "\r"))
        await wait_for(lambda: bool(pilot.app.results), pilot=pilot, description="the picker confirmed")

        assert pilot.app.results == [moved_to]


@pytest.mark.asyncio
async def test_picker_double_click_confirmation_is_guarded_to_one_dismiss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = LocaleController(Settings(locale="en"))
    switch_calls: list[str] = []
    monkeypatch.setattr(controller, "switch_locale", switch_calls.append)

    async with _PickerApp(controller, apply_result=controller.switch_locale).run_test() as pilot:
        screen = pilot.app.screen
        options = screen.query_one(OptionList)
        option = options.get_option("zh-Hans")
        event = OptionList.OptionSelected(options, option, 2)
        screen._on_selected(event)
        screen._on_selected(OptionList.OptionSelected(options, option, 2))
        await pilot.pause()

        assert pilot.app.results == ["zh-Hans"]
        assert switch_calls == ["zh-Hans"]


def test_language_command_is_static_but_choices_resolve_lazily() -> None:
    localizer = Localizer("en")
    current = [DEFAULT_LOCALE]
    actions = MagicMock()
    actions.current_language.side_effect = lambda: current[0]
    actions.available_languages.side_effect = lambda: [
        (requested_locale, render_content(localizer, definition.bind()))
        for requested_locale, definition in LANGUAGE_OPTIONS
    ]
    command = _language_command(actions)

    first_labels = command.subcommands()
    localizer.switch_locale("zh-Hans")
    current[0] = "zh-Hans"
    second_labels = command.subcommands()

    assert format_message(command.description) == "Set display language"
    assert command.synopsis == "/language [locale]"
    assert command.initial() == "zh-Hans"
    assert [(value, label.plain) for value, label in first_labels] == [
        ("system", "● Follow System"),
        ("en", "  English"),
        ("zh-Hans", "  简体中文"),
    ]
    assert [(value, label.plain) for value, label in second_labels] == [
        ("system", "  跟随系统"),
        ("en", "  English"),
        ("zh-Hans", "● 简体中文"),
    ]


def test_language_command_routes_picker_direct_confirmation_and_localized_unknown_warning() -> None:
    localizer = Localizer("zh-Hans")
    actions = MagicMock()
    actions.available_languages.return_value = [
        (requested_locale, render_content(localizer, definition.bind()))
        for requested_locale, definition in LANGUAGE_OPTIONS
    ]
    actions.unknown_language_warning.side_effect = lambda requested_locale: render_str(
        localizer,
        LANGUAGE_UNKNOWN_LOCALE.bind(locale=requested_locale),
    )
    command = _language_command(actions)

    command.action("")
    command.action(" zh-Hans ")
    command.action("fr")

    actions.open_language_picker.assert_called_once_with()
    actions.set_language.assert_called_once_with("zh-Hans")
    actions.notify_warning.assert_called_once_with("未知的 /language 区域设置：fr")  # noqa: RUF001


def test_navigation_picker_callback_uses_single_switch_entry_and_repeats_are_noops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = LocaleController(Settings(locale="en"))
    view = _NavigationView()
    navigation = _new_navigation(controller, view)
    persistence_calls: list[str] = []
    monkeypatch.setattr(tui_i18n, "persist_locale", persistence_calls.append)

    navigation.pick_language()
    current_locale, on_result = view.language_requests[0]
    on_result("zh-Hans")
    on_result("zh-Hans")

    assert current_locale == "en"
    assert controller.requested_locale == controller.effective_locale == "zh-Hans"
    assert controller.revision == 1
    assert persistence_calls == ["zh-Hans"]


def test_navigation_load_failure_notifies_through_old_locale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog_root = tmp_path / "catalogs"
    catalog_path = catalog_root / "zh-Hans" / "LC_MESSAGES" / "chrys.mo"
    catalog_path.parent.mkdir(parents=True)
    catalog_path.write_bytes(b"not a catalog")
    controller = LocaleController(
        Settings(locale="en"),
        localizer=Localizer("en", catalog_root=catalog_root),
    )
    view = _NavigationView()
    navigation = _new_navigation(controller, view)
    persistence_calls: list[str] = []
    monkeypatch.setattr(tui_i18n, "persist_locale", persistence_calls.append)

    navigation.set_language("zh-Hans")

    assert controller.requested_locale == controller.effective_locale == "en"
    assert controller.revision == 0
    assert persistence_calls == []
    assert view.notifications == [
        ("Could not load translations for locale zh-Hans; using English.", "Language", "warning")
    ]


def test_requested_locale_domain_is_derived_from_foundation_constants() -> None:
    assert tuple(requested_locale for requested_locale, _definition in LANGUAGE_OPTIONS) == (
        "system",
        "en",
        "zh-Hans",
    )
    assert os.environ.get("CHRYS_LOCALE") is None
    assert render_str(Localizer("zh-Hans"), LANGUAGE_PICKER_TITLE.bind()) == "显示语言"
