# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Stub ports and a bare host app for settings-panel widget tests."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from textual.app import App

from chrys.app.tui.notifications.settings import NotificationSettings
from chrys.app.tui.screens.settings import NOTIFICATIONS_TAB_ID, TABS, SettingsDialog, Suggestions
from chrys.app.tui.screens.settings.panes.notifications import NotificationsPane
from chrys.app.tui.theme import TuiVariableDefaultsMixin
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle, SettingsWarning
from chrys.foundation.config.spec import ChoiceProvider, SettingOrigin, Source, field_names_by_key
from chrys.foundation.i18n import MessageRef
from tests.support.waiting import wait_for

ROW_COUNT = sum(len(section.rows) for tab in TABS if tab.id != NOTIFICATIONS_TAB_ID for section in tab.sections)
"""Setting rows across every tab; the notifications tab is a pane of its own."""


class StubNotificationPorts:
    def __init__(self) -> None:
        self.settings = NotificationSettings()
        self.saved: list[NotificationSettings] = []

    def current(self) -> NotificationSettings:
        return self.settings

    def save(self, settings: NotificationSettings) -> bool:
        self.saved.append(settings)
        self.settings = settings
        return True

    async def test(self, settings: NotificationSettings) -> bool:
        return True


class StubSessionStorage:
    def __init__(self, root: Path, default_root: Path | None = None) -> None:
        self.root = root
        self.default_root = root if default_root is None else default_root
        self.probed: list[str] = []
        self.plans: list[tuple[Path, Path]] = []
        self.reports: list[object] = []
        self.plan_result: object = None
        self.report_result: object = None

    def in_force_sessions_dir(self) -> Path:
        return self.root / "sessions"

    def default_sessions_dir(self) -> Path:
        return self.default_root / "sessions"

    def default_root_dir(self) -> Path:
        return self.default_root

    def probe_root(self, raw: str) -> Path | None:
        self.probed.append(raw)
        if raw.strip().endswith("bad"):
            return None
        base = Path(raw.strip()) if raw.strip() else self.default_root
        return base / "sessions"

    def plan_migration(self, source: Path, destination: Path) -> Any:
        self.plans.append((source, destination))
        return self.plan_result

    async def run_migration(self, plan: Any) -> Any:
        self.reports.append(plan)
        return self.report_result


class StubPorts:
    """Records everything the dialog does; projections come from ``settings``."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        provenance: Mapping[str, SettingOrigin] | None = None,
        sealed_keys: frozenset[str] = frozenset(),
        warnings: tuple[SettingsWarning, ...] = (),
        root: Path = Path("/stub"),
        default_root: Path | None = None,
    ) -> None:
        self.handle = SettingsHandle(
            LoadedSettings(
                settings=settings or Settings(),
                provenance=dict(provenance or {}),
                warnings=warnings,
                sealed_keys=sealed_keys,
            )
        )
        self.persisted: list[dict[str, Any]] = []
        self.live: list[tuple[str, Any]] = []
        self.confirms: list[MessageRef] = []
        self.confirm_answer = True
        self.errors: list[MessageRef] = []
        self.closed = 0
        self.desired: dict[str, Any] = {}
        self.restart_pending: frozenset[str] = frozenset()
        self.dirty = False
        self.turn_running = False
        self.themes = ["chrys", "chrys-legacy", "textual-dark"]
        self.agent_profiles = [("code", "Code"), ("chat", "Chat")]
        self.model_profiles = [("model-one", "Model One"), ("model-two", "Model Two")]
        self.model_ids = [("vendor/one", "vendor/one"), ("vendor/two", "vendor/two")]
        self.notification_ports = StubNotificationPorts()
        self.storage = StubSessionStorage(root, default_root)

    @property
    def loaded(self) -> LoadedSettings:
        return self.handle.loaded

    def install(self, settings: Settings, provenance: Mapping[str, SettingOrigin] | None = None) -> None:
        self.handle.install(LoadedSettings(settings=settings, provenance=dict(provenance or {})))

    def resolve_choices(self, source: ChoiceProvider | Suggestions) -> Sequence[tuple[str, str]]:
        if source is ChoiceProvider.THEMES:
            return [(name, name) for name in self.themes]
        if source is Suggestions.AGENT_PROFILES:
            return list(self.agent_profiles)
        if source is Suggestions.MODEL_PROFILES:
            return list(self.model_profiles)
        if source is Suggestions.MODEL_IDS:
            return list(self.model_ids)
        return []

    def schedule_persist(self, values: Mapping[str, Any]) -> None:
        self.persisted.append(dict(values))

    def apply_live(self, key: str, value: Any) -> None:
        self.live.append((key, value))

    def projected_value(self, key: str) -> Any:
        if key in self.desired:
            return self.desired[key]
        return dataclasses.asdict(self.loaded.settings)[field_names_by_key(Settings)[key]]

    def restart_pending_keys(self) -> frozenset[str]:
        return self.restart_pending

    def reload_dirty(self) -> bool:
        return self.dirty

    def turn_in_progress(self) -> bool:
        return self.turn_running

    def on_dialog_closed(self) -> None:
        self.closed += 1

    def notifications(self) -> StubNotificationPorts:
        return self.notification_ports

    def session_storage(self) -> StubSessionStorage:
        return self.storage

    def confirm(self, message: MessageRef, on_result: Callable[[bool], None]) -> None:
        self.confirms.append(message)
        on_result(self.confirm_answer)

    def notify_error(self, message: MessageRef) -> None:
        self.errors.append(message)


class Host(TuiVariableDefaultsMixin, App[None]):
    """Bare host: the dialog is pushed by each test."""

    def __init__(self, locale_controller: object | None = None) -> None:
        super().__init__()
        if locale_controller is not None:
            self.locale_controller = locale_controller


def env_origin() -> SettingOrigin:
    return SettingOrigin(layer=Source.ENV)


def every_tab_projected(dialog: SettingsDialog) -> bool:
    """Every row and the notifications pane show their values, the later tabs included."""
    return len(dialog.rows()) == ROW_COUNT and any(pane.projected for pane in dialog.query(NotificationsPane))


async def wait_for_every_tab(dialog: SettingsDialog, pilot: Any) -> None:
    """Wait for the tabs the dialog mounts after it opens."""
    await wait_for(lambda: every_tab_projected(dialog), pilot=pilot, description="every settings tab mounted")


__all__ = [
    "ROW_COUNT",
    "Host",
    "StubNotificationPorts",
    "StubPorts",
    "StubSessionStorage",
    "env_origin",
    "every_tab_projected",
    "wait_for_every_tab",
]
