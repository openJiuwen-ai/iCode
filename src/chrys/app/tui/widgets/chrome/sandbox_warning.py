# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent warning when a requested guard cannot supply its own TUI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.widgets.selection import NonSelectableStatic
from chrys.foundation import guard
from chrys.foundation.i18n import msg

if TYPE_CHECKING:
    from chrys.app.tui.i18n import LocaleController

_UNAVAILABLE = msg("tui.guard.unavailable", fallback="⚠ Sandbox NOT active")


class SandboxWarning(NonSelectableStatic):
    """Core-owned warning, available even when the optional package is missing."""

    DEFAULT_CSS = """
    SandboxWarning {
        width: auto;
        max-width: 100%;
        height: 1;
        padding: 0 1;
        color: $error;
        text-style: bold;
        background: $footer-item-background;
        text-wrap: nowrap;
    }
    """

    def __init__(self, *, locale_controller: LocaleController | None = None) -> None:
        super().__init__("", markup=False)
        self._locale_controller = locale_controller

    def on_mount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)
        self.refresh_localization()

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)

    def refresh_localization(self) -> None:
        self.update(render_str(widget_localizer(self), _UNAVAILABLE.bind()))
        self.tooltip = "\n".join(guard.status().reasons)
