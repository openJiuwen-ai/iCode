# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``/reload`` following hand edits of the LIVE settings, in the real TUI."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from chrys.app.tui.notifications.settings import NotificationEvent, NotificationSettings
from chrys.app.tui.screens.dialogs.editor import EditorDialogResult
from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft
from chrys.app.tui.widgets.editor import EditorMode
from chrys.foundation.config.settings import Settings, persist_theme
from chrys.foundation.config.spec import Apply, Source, field_names_by_key, specs_by_key
from chrys.foundation.config.user_settings import flatten_user_doc
from chrys.foundation.events import types as ev
from chrys.service.profiles.models.env_bridge import activate_model_profile
from tests.app.tui.screens.test_manual_reload import reload_report, settings_path, slash_reload
from tests.app.tui.screens.test_prompt_shortcut_send import (
    MAIN_MODEL,
    Hold,
    Session,
    hold_next,
    notices,
    running_session,
)
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for, wait_until

pytestmark = pytest.mark.usefixtures("clear_model_profile_env")

FIELDS = field_names_by_key(Settings)
SPECS = specs_by_key(Settings)


@pytest.fixture(autouse=True)
def real_theme_writes(monkeypatch: pytest.MonkeyPatch, prevent_theme_preference_persistence: None) -> None:
    """These tests read the theme back from the (per-test) settings file."""
    monkeypatch.setattr("chrys.app.tui.app.persist_theme", persist_theme)


def file_values(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return flatten_user_doc(document, frozenset(SPECS))[0]


def hand_edit(path: Path, **changes: Any) -> bytes:
    """Change dotted keys in the settings file (``__`` for ``.``) as an editor would; return its bytes."""
    values = file_values(path)
    for name, value in changes.items():
        key = name.replace("__", ".")
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    return path.read_bytes()


def edit_key(path: Path, key: str, value: Any) -> bytes:
    return hand_edit(path, **{key.replace(".", "__"): value})


def choose_in_panel(key: str) -> Callable[[Session, Any], None]:
    def choose(session: Session, value: Any) -> None:
        session.main._settings_coordinator().apply_live(key, value)

    return choose


def choose_notification(apply: Callable[[NotificationSettings, bool], None]) -> Callable[[Session, Any], None]:
    def choose(session: Session, value: Any) -> None:
        settings = NotificationSettings.from_settings(session.app.settings_handle.settings)
        apply(settings, value)
        session.main._settings_coordinator().notifications().save(settings)

    return choose


def choose_keymap(session: Session, value: Any) -> None:
    session.main._on_editor_result(
        EditorDialogResult(accepted=False, draft=ChatDraft(""), cursor_location=(0, 0), mode=EditorMode(value)),
        opened=None,
        opened_mode=session.main._editor_mode,
    )


def set_attribute(name: str) -> Callable[[NotificationSettings, bool], None]:
    return lambda settings, value: setattr(settings, name, value)


def set_event(event: NotificationEvent) -> Callable[[NotificationSettings, bool], None]:
    return lambda settings, value: settings.events.__setitem__(event, value)


def shown_notification(key: str) -> Callable[[Session], Any]:
    return lambda session: session.app.notification_service.settings.to_settings_patch()[key]


@dataclass(frozen=True)
class LiveKey:
    key: str
    choose: Callable[[Session, Any], None]
    shown: Callable[[Session], Any]
    """What the user actually gets from the key: read from where its consumer reads it."""
    first: Any = None
    second: Any = None

    def values(self) -> tuple[Any, Any]:
        """A choice that is not the default, then a hand edit that differs from it."""
        if self.first is not None:
            return self.first, self.second
        default = getattr(Settings(), FIELDS[self.key])
        return (not default), default


NOTIFICATIONS = {
    "notifications.enabled": set_attribute("enabled"),
    "notifications.delivery.desktop": set_attribute("desktop"),
    "notifications.delivery.sound": set_attribute("sound"),
    "notifications.suppress_when_focused": set_attribute("suppress_when_focused"),
    "notifications.events.approval_required": set_event(NotificationEvent.APPROVAL_REQUIRED),
    "notifications.events.ask_user": set_event(NotificationEvent.ASK_USER),
    "notifications.events.turn_complete": set_event(NotificationEvent.TURN_COMPLETE),
    "notifications.events.turn_error": set_event(NotificationEvent.TURN_ERROR),
}
LIVE_KEYS = (
    *(LiveKey(key, choose_notification(apply), shown_notification(key)) for key, apply in NOTIFICATIONS.items()),
    LiveKey(
        "trajectory.verify_commands",
        choose_in_panel("trajectory.verify_commands"),
        lambda session: session.main._trajectory_verify_commands,
        "picked",
        "edited",
    ),
    LiveKey("ui.theme", choose_in_panel("ui.theme"), lambda session: session.app.theme, "nord", "gruvbox"),
    LiveKey(
        "ui.locale",
        choose_in_panel("ui.locale"),
        lambda session: session.app.locale_controller.localizer.requested_locale,
        "zh-Hans",
        "en",
    ),
    LiveKey("ui.editor.keymap", choose_keymap, lambda session: session.main._editor_mode.value, "vim", "emacs"),
    LiveKey(
        "ui.approval.defer_while_judging",
        choose_in_panel("ui.approval.defer_while_judging"),
        lambda session: session.main._approval_defer_while_judging(),
    ),
    LiveKey(
        "ui.chat.tool_groups_expanded",
        choose_in_panel("ui.chat.tool_groups_expanded"),
        lambda session: session.main._tool_groups_expanded(),
    ),
    LiveKey(
        "ui.chat.show_thinking",
        choose_in_panel("ui.chat.show_thinking"),
        lambda session: session.main._thinking_expanded(),
    ),
    LiveKey(
        "ui.chat.fold_finished_turns",
        choose_in_panel("ui.chat.fold_finished_turns"),
        lambda session: session.main._fold_finished_turns(),
    ),
)


def test_the_table_covers_every_live_setting() -> None:
    assert {live.key for live in LIVE_KEYS} == {key for key, spec in SPECS.items() if spec.apply is Apply.LIVE}
    assert len(LIVE_KEYS) == 16


async def on_disk(session: Session, path: Path, key: str, value: Any) -> None:
    """Whatever is queued has landed, and the file holds *value*."""
    await session.main._settings_persistence().flush()
    assert file_values(path)[key] == value


@pytest.mark.parametrize("live", LIVE_KEYS, ids=lambda live: live.key)
async def test_a_saved_choice_gives_way_to_a_hand_edit(live: LiveKey, agent_engine, tmp_path, monkeypatch) -> None:
    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    first, second = live.values()
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        handle = session.app.settings_handle
        live.choose(session, first)
        await on_disk(session, path, live.key, first)
        assert live.shown(session) == first
        assert handle.loaded.source_for(live.key).layer is Source.RUNTIME

        edited = edit_key(path, live.key, second)
        lines = await reload_report(session)

        assert getattr(handle.settings, FIELDS[live.key]) == second
        assert handle.loaded.source_for(live.key).layer is Source.USER
        assert live.shown(session) == second
        assert live.key in lines["controls.reload_applied"]
        assert "controls.reload_shadowed" not in lines
        await session.pilot.pause()
        assert path.read_bytes() == edited
        assert not session.main._settings_persistence()._pending_values


async def test_a_project_file_that_may_not_set_the_key_does_not_hold_the_saved_choice(
    agent_engine, tmp_path, monkeypatch
) -> None:
    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    hand_edit(path, project__config_enabled=True)
    project = Path.cwd() / ".chrys"
    project.mkdir()
    (project / "settings.yaml").write_text("ui.theme: dracula\n", encoding="utf-8")
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        handle = session.app.settings_handle
        session.main._settings_coordinator().apply_live("ui.theme", "nord")
        await on_disk(session, path, "ui.theme", "nord")

        edit_key(path, "ui.theme", "gruvbox")
        lines = await reload_report(session)

        assert session.app.theme == "gruvbox"
        assert handle.loaded.source_for("ui.theme").layer is Source.USER
        assert "controls.reload_shadowed" not in lines


async def test_a_second_hand_edit_is_followed_too_and_a_removed_key_returns_to_the_default(
    agent_engine, tmp_path, monkeypatch
) -> None:
    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        handle = session.app.settings_handle
        session.main._settings_coordinator().apply_live("ui.theme", "nord")
        await on_disk(session, path, "ui.theme", "nord")

        edit_key(path, "ui.theme", "gruvbox")
        await reload_report(session)
        assert session.app.theme == "gruvbox"

        edited = edit_key(path, "ui.theme", "dracula")
        await reload_report(session)
        assert session.app.theme == "dracula"
        assert handle.loaded.source_for("ui.theme").layer is Source.USER
        assert path.read_bytes() == edited

        session.main._settings_coordinator().apply_live("ui.theme", "nord")
        await on_disk(session, path, "ui.theme", "nord")
        edited = edit_key(path, "ui.theme", None)
        await reload_report(session)
        assert handle.loaded.source_for("ui.theme").layer is Source.DEFAULT
        assert session.app.theme == Settings().theme
        assert path.read_bytes() == edited


async def test_a_choice_that_never_reached_the_file_outlives_the_reload(agent_engine, tmp_path, monkeypatch) -> None:
    import chrys.app.tui.app as app_module
    from chrys.foundation.config.errors import INVALID_SETTING, ConfigValueError

    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        handle = session.app.settings_handle
        coordinator = session.main._settings_coordinator()
        coordinator.apply_live("ui.chat.show_thinking", not handle.settings.chat_thinking_expanded)
        await session.main._settings_persistence().flush()
        with monkeypatch.context() as patch:
            patch.setattr(
                app_module, "persist_theme", lambda _name: ConfigValueError(INVALID_SETTING.bind(key="ui.theme"))
            )
            coordinator.apply_live("ui.theme", "nord")
        assert "ui.theme" not in file_values(path)

        hand_edit(path, ui__theme="gruvbox", ui__chat__show_thinking=handle.base.settings.chat_thinking_expanded)
        lines = await reload_report(session)

        assert session.app.theme == "nord"
        assert handle.loaded.source_for("ui.theme").layer is Source.RUNTIME
        assert "ui.theme" in lines["controls.reload_shadowed"]
        assert handle.loaded.source_for("ui.chat.show_thinking").layer is Source.USER


async def test_a_key_chosen_again_during_the_reload_keeps_the_new_choice(agent_engine, tmp_path, monkeypatch) -> None:
    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        handle = session.app.settings_handle
        coordinator = session.main._settings_coordinator()
        coordinator.apply_live("ui.theme", "nord")
        coordinator.apply_live("ui.approval.defer_while_judging", not handle.settings.approval_defer_while_judging)
        await session.main._settings_persistence().flush()
        hand_edit(
            path,
            ui__theme="gruvbox",
            ui__approval__defer_while_judging=handle.base.settings.approval_defer_while_judging,
        )

        hold = Hold()
        await hold_next(session.bus, ev.AgentLoadStarted, hold)
        await slash_reload(session)
        await wait_for(hold.entered.is_set, pilot=session.pilot, description="the reload is building")
        coordinator.apply_live("ui.theme", "dracula")
        hold.release()
        await wait_for(
            lambda: session.seen.of(ev.SettingsReloaded), pilot=session.pilot, description="the reload finished"
        )

        assert session.app.theme == "dracula"
        assert handle.settings.theme == "dracula"
        assert handle.loaded.source_for("ui.theme").layer is Source.RUNTIME
        assert handle.loaded.source_for("ui.approval.defer_while_judging").layer is Source.USER
        await on_disk(session, path, "ui.theme", "dracula")


async def test_a_choice_saved_after_the_file_was_read_is_not_mistaken_for_one_the_file_moved_past(
    agent_engine, tmp_path, monkeypatch
) -> None:
    from chrys.orchestration.engine.state import controls

    settings_path()
    activate_model_profile(MAIN_MODEL)
    real_load = controls.load_settings
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        handle = session.app.settings_handle

        def read_then_saved_elsewhere(**kwargs: Any) -> Any:
            loaded = real_load(**kwargs)
            handle.override(theme="nord")
            handle.confirm_written({"ui.theme": "nord"})
            return loaded

        with monkeypatch.context() as patch:
            patch.setattr(controls, "load_settings", read_then_saved_elsewhere)
            await reload_report(session)

        assert handle.settings.theme == "nord"
        assert handle.loaded.source_for("ui.theme").layer is Source.RUNTIME


@pytest.fixture
def reload_held_before_its_read(monkeypatch: pytest.MonkeyPatch) -> Iterator[threading.Event]:
    """Hold a started reload at its theme scan, before anything is read or a loading state shows."""
    import chrys.app.tui.theme_loader as theme_loader

    release = threading.Event()
    real_scan = theme_loader.scan_user_themes

    def held_scan(*args: Any, **kwargs: Any) -> Any:
        release.wait(timeout=ENGINE_TURN_TIMEOUT)
        return real_scan(*args, **kwargs)

    monkeypatch.setattr(theme_loader, "scan_user_themes", held_scan)
    yield release
    release.set()


async def load_dialog_closed(session: Session) -> None:
    """Wait out the reload's own success dialog, which sits on top of whatever is open."""
    from chrys.app.tui.screens.dialogs.agent_load import AgentLoadDialog

    def closed() -> bool:
        return not any(isinstance(screen, AgentLoadDialog) for screen in session.app.screen_stack)

    await wait_for(closed, pilot=session.pilot, description="the load dialog closed")


async def test_an_editor_left_untouched_during_a_reload_does_not_undo_it(
    agent_engine, tmp_path, monkeypatch, reload_held_before_its_read
) -> None:
    from chrys.app.tui.screens.dialogs.editor import EditorDialog

    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        choose_keymap(session, "emacs")
        await on_disk(session, path, "ui.editor.keymap", "emacs")
        edited = edit_key(path, "ui.editor.keymap", "vim")

        session.main._start_manual_reload()
        await wait_for(lambda: session.main._manual_reloads_running, pilot=session.pilot, description="reload started")
        await session.pilot.press("ctrl+o")
        await wait_for(
            lambda: isinstance(session.app.screen, EditorDialog), pilot=session.pilot, description="editor open"
        )
        reload_held_before_its_read.set()
        await wait_for(
            lambda: session.seen.of(ev.SettingsReloaded), pilot=session.pilot, description="the reload finished"
        )
        await load_dialog_closed(session)
        assert isinstance(session.app.screen, EditorDialog)
        assert session.main._editor_mode is EditorMode.VIM

        await session.pilot.press("escape")
        await wait_for(lambda: session.app.screen is session.main, pilot=session.pilot, description="editor closed")

        assert session.main._editor_mode is EditorMode.VIM
        assert session.app.settings_handle.loaded.source_for("ui.editor.keymap").layer is Source.USER
        assert path.read_bytes() == edited


async def test_the_theme_picker_stays_shut_while_a_reload_may_move_the_theme(
    agent_engine, tmp_path, monkeypatch, reload_held_before_its_read
) -> None:
    from chrys.app.tui.screens.themes import ThemesScreen

    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        session.main._settings_coordinator().apply_live("ui.theme", "nord")
        await on_disk(session, path, "ui.theme", "nord")
        edit_key(path, "ui.theme", "gruvbox")

        session.main._start_manual_reload()
        await wait_for(lambda: session.main._manual_reloads_running, pilot=session.pilot, description="reload started")
        await session.pilot.press("f9")
        assert not await wait_until(
            lambda: isinstance(session.app.screen, ThemesScreen), timeout=0.2, pilot=session.pilot
        )
        reload_held_before_its_read.set()
        await wait_for(lambda: session.app.theme == "gruvbox", pilot=session.pilot, description="the theme followed")
        await wait_for(lambda: not session.main._manual_reloads_running, pilot=session.pilot, description="reload over")
        await load_dialog_closed(session)

        await session.pilot.press("f9")
        await wait_for(
            lambda: isinstance(session.app.screen, ThemesScreen), pilot=session.pilot, description="the picker opens"
        )


async def test_a_failed_read_keeps_the_choice_and_the_repair_is_followed(agent_engine, tmp_path, monkeypatch) -> None:
    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        handle = session.app.settings_handle
        session.main._settings_coordinator().apply_live("ui.theme", "nord")
        await on_disk(session, path, "ui.theme", "nord")
        repaired = hand_edit(path, ui__theme="gruvbox")

        path.write_text("ui: [broken", encoding="utf-8")
        await slash_reload(session)
        await wait_for(
            lambda: any(str(path) in message for _, message in notices(session.app)),
            pilot=session.pilot,
            description="the read failure shown",
        )
        assert session.app.theme == "nord"
        assert handle.settings.theme == "nord"
        assert handle.loaded.source_for("ui.theme").layer is Source.RUNTIME

        path.write_bytes(repaired)
        await reload_report(session)
        assert session.app.theme == "gruvbox"
        assert handle.loaded.source_for("ui.theme").layer is Source.USER


async def test_a_failed_build_keeps_the_choice(agent_engine, tmp_path, monkeypatch) -> None:
    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        handle = session.app.settings_handle
        session.main._settings_coordinator().apply_live("ui.theme", "nord")
        await on_disk(session, path, "ui.theme", "nord")
        edit_key(path, "ui.theme", "gruvbox")

        session.builds.failing.add(MAIN_MODEL)
        await slash_reload(session)
        await wait_for(lambda: session.seen.of(ev.AgentLoadFailed), pilot=session.pilot, description="the build failed")
        assert session.app.theme == "nord"
        assert handle.settings.theme == "nord"
        assert handle.loaded.source_for("ui.theme").layer is Source.RUNTIME
        assert not session.seen.of(ev.SettingsReloaded)


async def test_a_reload_with_no_edit_changes_no_value_or_source(agent_engine, tmp_path, monkeypatch) -> None:
    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        handle = session.app.settings_handle
        session.main._settings_coordinator().apply_live("ui.theme", "nord")
        await on_disk(session, path, "ui.theme", "nord")
        before = path.read_bytes()
        lines = await reload_report(session)
        assert handle.loaded.source_for("ui.theme").layer is Source.RUNTIME
        assert session.app.theme == "nord"
        assert "ui.theme" not in lines.get("controls.reload_applied", "")
        assert "controls.reload_shadowed" not in lines
        assert path.read_bytes() == before


async def test_an_ordinary_reload_follows_nothing(agent_engine, tmp_path, monkeypatch) -> None:
    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        handle = session.app.settings_handle
        session.main._settings_coordinator().apply_live("ui.theme", "nord")
        await on_disk(session, path, "ui.theme", "nord")
        hand_edit(path, ui__theme="gruvbox", ui__editor__keymap="vim", ui__locale="zh-Hans")

        await session.bus.publish(ev.SettingsReload(), raise_handler_errors=True)
        await wait_for(
            lambda: session.seen.of(ev.SettingsReloaded), pilot=session.pilot, description="ordinary reload done"
        )

        assert session.app.theme == "nord"
        assert handle.settings.theme == "nord"
        assert handle.loaded.source_for("ui.theme").layer is Source.RUNTIME
        assert session.main._editor_mode is EditorMode.STANDARD
        assert session.app.locale_controller.localizer.requested_locale != "zh-Hans"


async def test_a_locale_that_will_not_load_is_reported_and_nothing_is_written(
    agent_engine, tmp_path, monkeypatch
) -> None:
    from chrys.foundation.i18n import Localizer
    from chrys.foundation.i18n.localizer import CatalogLoadWarning

    path = settings_path()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        localizer = session.app.locale_controller.localizer
        monkeypatch.setattr(Localizer, "switch_locale", lambda _self, requested: CatalogLoadWarning(requested))
        edited = hand_edit(path, ui__locale="zh-Hans")
        await reload_report(session)
        assert localizer.requested_locale != "zh-Hans"
        assert path.read_bytes() == edited
        await wait_for(
            lambda: any("zh-Hans" in message for severity, message in notices(session.app) if severity == "warning"),
            pilot=session.pilot,
            description="the failed bundle reported",
        )
        assert session.app.settings_handle.loaded.source_for("ui.locale").layer is Source.USER
