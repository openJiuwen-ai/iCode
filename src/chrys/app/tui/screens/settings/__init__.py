# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Settings dialog: the public face other screen packages import."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

from chrys.app.tui.screens.settings.layout import (
    DEFERRED_KEYS,
    GENERAL_TAB_ID,
    NOTIFICATIONS_TAB_ID,
    TAB_IDS,
    TABS,
    Suggestions,
    rendered_keys,
)
from chrys.app.tui.screens.settings.ports import NotificationPorts, SessionStoragePorts, SettingsPanelPorts
from chrys.app.tui.screens.settings.provenance import is_greyed_layer

if TYPE_CHECKING:
    from chrys.app.tui.screens.settings.dialog import SettingsDialog

# The modal loads on first use; its layout and ports above stay light.
_EXPORTS = {"SettingsDialog": "dialog"}

__all__ = [
    "DEFERRED_KEYS",
    "GENERAL_TAB_ID",
    "NOTIFICATIONS_TAB_ID",
    "TABS",
    "TAB_IDS",
    "NotificationPorts",
    "SessionStoragePorts",
    "SettingsDialog",
    "SettingsPanelPorts",
    "Suggestions",
    "is_greyed_layer",
    "rendered_keys",
]


def __getattr__(name: str) -> Any:
    """Lazily resolve the settings modal."""
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(f"{__name__}.{_EXPORTS[name]}")
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Return the default module attributes plus facade exports."""
    return sorted({*globals(), *__all__})
