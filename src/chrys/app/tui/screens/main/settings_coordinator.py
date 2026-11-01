# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Process-level state behind the Settings dialog.

The dialog is transient; this is not. It owns the debounced persistence
queue, remembers which written values the live settings have not caught up
with yet (``desired``), decides when a RELOAD is due and publishes exactly one
``SettingsReload`` per close, and routes LIVE keys to the writer that already
persists and overlays them.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.app.tui.screens.settings import (
    DEFERRED_KEYS,
    NotificationPorts,
    SessionStoragePorts,
    Suggestions,
    rendered_keys,
)
from chrys.foundation.config.settings import Settings, probe_session_root
from chrys.foundation.config.spec import Apply, ChoiceProvider, Source, field_names_by_key, specs_by_key
from chrys.foundation.events.types import SettingsReload
from chrys.foundation.i18n import msg

if TYPE_CHECKING:
    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.notifications import NotificationService, NotificationSettings
    from chrys.app.tui.screens.main.ports import RuntimeConfigView
    from chrys.app.tui.screens.main.settings_persistence import SettingsPersistenceQueue
    from chrys.app.tui.screens.main.state import MainScreenServices
    from chrys.foundation.config.settings_store import LoadedSettings, PersistResult, SettingsHandle
    from chrys.foundation.i18n import MessageRef
    from chrys.service.state.session_migration import MigrationPlan, MigrationReport

logger = logging.getLogger(__name__)

_CONFIRM_TITLE = msg("tui.settings.confirm.title", fallback="Confirm setting")
_CONFIRM_BUTTON = msg("tui.settings.confirm.button", fallback="Continue")
SETTINGS_TITLE = msg("tui.settings.title", fallback="Settings")

_GREYED_LAYERS = frozenset({Source.PROJECT, Source.USER_ENV, Source.ENV})

# RELOAD keys the TUI reads only at startup: the engine's rebuild keeps its
# current agent profile and approval mode, so a reload triggered for these
# alone would rebuild for nothing. They stay RELOAD in the foundation (other
# hosts do apply them on reload); they just never mark this panel dirty.
_STARTUP_ONLY_RELOAD_KEYS = frozenset({"agent.default_profile", "approval.default_mode"})


@dataclass(frozen=True, slots=True)
class SettingsCoordinatorCallbacks:
    """Screen- and app-owned effects the coordinator drives."""

    apply_theme: Callable[[str], bool]
    switch_locale: Callable[[str], object]
    apply_trajectory_verify_commands: Callable[[str], None]
    list_themes: Callable[[], Sequence[str]]
    save_notifications: Callable[[NotificationSettings], None]
    notification_service: Callable[[], NotificationService]
    turn_lifecycle_task: Callable[[], asyncio.Task[None] | None]
    turn_in_progress: Callable[[], bool]
    apply_tool_groups_expanded: Callable[[bool], None]
    apply_approval_defer_while_judging: Callable[[bool], None]
    apply_thinking_expanded: Callable[[bool], None]
    apply_fold_finished_turns: Callable[[bool], None]


def _values_by_key(loaded: LoadedSettings) -> dict[str, Any]:
    names = field_names_by_key(Settings)
    by_field = dataclasses.asdict(loaded.settings)
    return {key: by_field[name] for key, name in names.items()}


class _NotificationPortsImpl:
    def __init__(self, callbacks: SettingsCoordinatorCallbacks) -> None:
        self._callbacks = callbacks

    def current(self) -> NotificationSettings:
        return self._callbacks.notification_service().settings

    def save(self, settings: NotificationSettings) -> bool:
        self._callbacks.notification_service().update_settings(settings)
        self._callbacks.save_notifications(settings)
        return True

    async def test(self, settings: NotificationSettings) -> bool:
        return await self._callbacks.notification_service().test(settings)


class _SessionStoragePortsImpl:
    def in_force_sessions_dir(self) -> Path:
        from chrys.foundation.config.settings import resolve_sessions_dir

        return resolve_sessions_dir()

    def default_sessions_dir(self) -> Path:
        from chrys.foundation.config.settings import default_sessions_dir

        return default_sessions_dir()

    def default_root_dir(self) -> Path:
        from chrys.foundation.config.settings import default_session_root_dir

        return default_session_root_dir()

    def probe_root(self, raw: str) -> Path | None:
        return probe_session_root(raw)

    def plan_migration(self, source: Path, destination: Path) -> MigrationPlan:
        from chrys.service.state.session_migration import plan_session_migration

        return plan_session_migration(source, destination)

    async def run_migration(self, plan: MigrationPlan) -> MigrationReport:
        from chrys.service.state.session_migration import run_session_migration

        return await asyncio.to_thread(run_session_migration, plan)


class SettingsCoordinator:
    """Implements ``SettingsPanelPorts`` for the main screen."""

    def __init__(
        self,
        *,
        services: MainScreenServices,
        settings_handle: SettingsHandle,
        queue: SettingsPersistenceQueue,
        view: RuntimeConfigView,
        callbacks: SettingsCoordinatorCallbacks,
        locale_controller: LocaleController | None,
    ) -> None:
        self._services = services
        self._settings_handle = settings_handle
        self._queue = queue
        self._view = view
        self._callbacks = callbacks
        self._locale_controller = locale_controller
        self._specs = specs_by_key(Settings)
        self._panel_keys = rendered_keys() | DEFERRED_KEYS
        self._restart_keys = frozenset(key for key in self._panel_keys if self._specs[key].apply is Apply.RESTART)
        self._reload_keys = (
            frozenset(key for key in self._panel_keys if self._specs[key].apply is Apply.RELOAD)
            - _STARTUP_ONLY_RELOAD_KEYS
        )
        in_force = _values_by_key(settings_handle.loaded)
        self.restart_in_force: Mapping[str, Any] = {key: in_force[key] for key in self._restart_keys}
        self._desired: dict[str, Any] = {}
        self._reload_dirty = False
        self._close_epoch = 0
        self._ack = False
        self._finalize_task: asyncio.Task[None] | None = None
        self._dialog: weakref.ReferenceType[Any] | None = None
        self._dialog_open = False
        self._notifications = _NotificationPortsImpl(callbacks)
        self._session_storage = _SessionStoragePortsImpl()

    # ── dialog lifecycle ─────────────────────────────────────────────
    def attach_dialog(self, dialog: Any) -> None:
        """Remember the open dialog (weakly) so writes and reloads reproject it."""
        self._dialog = weakref.ref(dialog)
        self._dialog_open = True
        self._prune_desired()

    def _reproject_dialog(self) -> None:
        if not self._dialog_open:
            return
        dialog = None if self._dialog is None else self._dialog()
        if dialog is None:
            return
        try:
            dialog.reproject()
        except Exception:
            # Feedback still flows; a projection that cannot paint is a bug worth seeing.
            logger.warning("Settings dialog reprojection failed", exc_info=True)

    # ── SettingsPanelPorts ──────────────────────────────────────────
    @property
    def loaded(self) -> LoadedSettings:
        return self._settings_handle.loaded

    def resolve_choices(self, source: ChoiceProvider | Suggestions) -> Sequence[tuple[str, str]]:
        if source is ChoiceProvider.THEMES:
            return [(name, name) for name in sorted(self._callbacks.list_themes())]
        if source is Suggestions.AGENT_PROFILES:
            registry = self._services.agent_registry
            if registry is None:
                return []
            return [
                (profile.name, profile.display_name or profile.name)
                for profile in registry.list_profiles(include_sub_agent_only=False)
            ]
        if source is Suggestions.MODEL_PROFILES:
            registry = self._services.model_registry
            if registry is None:
                return []
            return [(profile.id, profile.name or profile.id) for profile in registry.list_profiles()]
        if source is Suggestions.MODEL_IDS:
            registry = self._services.model_registry
            if registry is None:
                return []
            model_ids = dict.fromkeys(profile.model_id for profile in registry.list_profiles() if profile.model_id)
            return [(model_id, model_id) for model_id in model_ids]
        return []

    def schedule_persist(self, values: Mapping[str, Any]) -> None:
        self._queue.schedule(values)

    def apply_live(self, key: str, value: Any) -> None:
        if key == "ui.theme":
            self._callbacks.apply_theme(str(value))
        elif key == "ui.locale":
            self._callbacks.switch_locale(str(value))
            # A bundle that failed to load leaves the old locale in force: the
            # row must show that, not the choice that did not take.
            self._reproject_dialog()
        elif key == "trajectory.verify_commands":
            self._callbacks.apply_trajectory_verify_commands(str(value))
        elif key == "ui.chat.tool_groups_expanded":
            self._callbacks.apply_tool_groups_expanded(bool(value))
        elif key == "ui.approval.defer_while_judging":
            self._queue.schedule({key: bool(value)})
            self._callbacks.apply_approval_defer_while_judging(bool(value))
        elif key == "ui.chat.show_thinking":
            self._queue.schedule({key: bool(value)})
            self._callbacks.apply_thinking_expanded(bool(value))
        elif key == "ui.chat.fold_finished_turns":
            self._queue.schedule({key: bool(value)})
            self._callbacks.apply_fold_finished_turns(bool(value))
        else:
            msg_text = f"{key}: no live writer registered"
            raise ValueError(msg_text)

    def projected_value(self, key: str) -> Any:
        loaded = self._settings_handle.loaded
        values = _values_by_key(loaded)
        current = values[key]
        if loaded.source_for(key).layer in _GREYED_LAYERS:
            self._desired.pop(key, None)
            return current
        if key in self._desired and self._desired[key] == current:
            del self._desired[key]
        return self._desired.get(key, current)

    def restart_pending_keys(self) -> frozenset[str]:
        return frozenset(
            key
            for key in self._restart_keys
            if key in self._desired and self._desired[key] != self.restart_in_force[key]
        )

    def reload_dirty(self) -> bool:
        return self._reload_dirty

    def turn_in_progress(self) -> bool:
        return self._callbacks.turn_in_progress()

    def on_dialog_closed(self) -> None:
        self._dialog_open = False
        self._close_epoch += 1
        task = self._finalize_task
        if task is not None and not task.done():
            return
        task = asyncio.create_task(self._finalize())
        self._finalize_task = task
        task.add_done_callback(self._on_finalize_done)

    def notifications(self) -> NotificationPorts:
        return self._notifications

    def session_storage(self) -> SessionStoragePorts:
        return self._session_storage

    def confirm(self, message: MessageRef, on_result: Callable[[bool], None]) -> None:
        from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

        dialog = ConfirmDialog(
            title=_CONFIRM_TITLE.bind(),
            message=message,
            confirm_label=_CONFIRM_BUTTON.bind(),
            locale_controller=self._locale_controller,
        )
        self._view.push_screen(dialog, lambda confirmed: on_result(bool(confirmed)))

    def notify_error(self, message: MessageRef) -> None:
        self._view.notify(message, title=SETTINGS_TITLE.bind(), severity="error", timeout=5)

    # ── write / reload feedback ─────────────────────────────────────
    def on_written(self, result: PersistResult) -> None:
        """A persisted batch landed: remember it and mark a reload if needed."""
        for key, value in result.written.items():
            if key in self._panel_keys and self._specs[key].apply is not Apply.LIVE:
                self._desired[key] = value
        if any(key in self._reload_keys for key in result.written):
            self._reload_dirty = True
        self._reproject_dialog()

    def on_write_failed(self) -> None:
        """A batch did not land: rows snap back to what is in force."""
        self._reproject_dialog()

    def note_external_write(self, key: str, value: Any) -> None:
        """Another writer persisted *key*; project it like our own write."""
        if key in self._panel_keys and self._specs[key].apply is not Apply.LIVE:
            self._desired[key] = value
        self._reproject_dialog()

    def on_reloaded(self) -> None:
        """``SettingsReloaded`` arrived, from our finalize task or elsewhere."""
        if self._finalize_task is not None and asyncio.current_task() is self._finalize_task:
            self._ack = True
        self._prune_desired()
        self._reproject_dialog()

    def _prune_desired(self) -> None:
        if not self._desired:
            return
        loaded = self._settings_handle.loaded
        values = _values_by_key(loaded)
        for key in list(self._desired):
            if loaded.source_for(key).layer in _GREYED_LAYERS or values[key] == self._desired[key]:
                del self._desired[key]

    async def _finalize(self) -> None:
        while True:
            await self._flush()
            task = self._callbacks.turn_lifecycle_task()
            if task is not None and not task.done():
                # ``wait`` rather than ``await task``: the turn being interrupted
                # must not read as this task being cancelled.
                await asyncio.wait({task})
            epoch = self._close_epoch
            await self._flush()
            # Reopened while the turn ran or while that flush waited: the edits
            # made since belong to that panel's close, which starts its own
            # finalize. Applying now would land them under the user's hands.
            # Checked after the flush, the last await before the reload.
            if self._dialog_open:
                return
            if not self._reload_dirty:
                return
            self._reload_dirty = False
            self._ack = False
            await self._services.bus.publish(SettingsReload())
            if not self._ack:
                self._reload_dirty = True
                return
            if self._close_epoch == epoch:
                return

    async def _flush(self) -> None:
        try:
            await self._queue.flush(notify_on_failure=True)
        except Exception:
            logger.debug("Settings flush failed", exc_info=True)

    def _on_finalize_done(self, task: asyncio.Task[None]) -> None:
        if task is self._finalize_task:
            self._finalize_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("Settings finalize task failed", exc_info=True)


__all__ = ["SETTINGS_TITLE", "SettingsCoordinator", "SettingsCoordinatorCallbacks"]
