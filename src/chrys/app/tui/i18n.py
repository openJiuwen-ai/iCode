# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Plain-text TUI adapters and bounded locale-switch coordination."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol, runtime_checkable
from weakref import WeakSet

from rich.text import Text
from textual.content import Content

from chrys.foundation.config.settings import DEFAULT_LOCALE, Settings, persist_locale
from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle
from chrys.foundation.i18n import CatalogLoadWarning, Localizer, MessageRef
from chrys.foundation.i18n.formatting import sanitize_legacy_block, sanitize_legacy_scalar

logger = logging.getLogger(__name__)


class LocaleAwareSurface(Protocol):
    """A live chrome or modal surface that can replace its localized text."""

    def refresh_localization(self) -> None: ...


class LocalizationFooter(Protocol):
    """A mounted registry-resolving footer with an in-place invalidation hook."""

    def invalidate_localization(self) -> None: ...


class LocaleSwitchStatus(StrEnum):
    """Observable outcomes used by confirmation surfaces such as the picker."""

    IDENTICAL_REQUEST = "identical_request"
    REQUESTED_ONLY = "requested_only"
    EFFECTIVE_CHANGED = "effective_changed"
    LOAD_FAILED = "load_failed"


@dataclass(frozen=True, slots=True)
class LocaleSwitchResult:
    """The completed locale-switch outcome and any catalog warning to present."""

    status: LocaleSwitchStatus
    warning: CatalogLoadWarning | None = None
    persist_error: Exception | None = None


def _resolved_display_text(localizer: Localizer, reference: MessageRef) -> str:
    resolved = localizer.render(reference)
    if reference.definition.multiline:
        return sanitize_legacy_block(resolved)
    return sanitize_legacy_scalar(resolved)


def render_text(localizer: Localizer, reference: MessageRef) -> Text:
    """Resolve one semantic message as literal Rich text, never as markup."""
    return Text(_resolved_display_text(localizer, reference))


def render_content(localizer: Localizer, reference: MessageRef) -> Content:
    """Resolve one semantic message as literal Textual content, never as markup."""
    return Content.from_text(_resolved_display_text(localizer, reference), markup=False)


def render_str(localizer: Localizer, reference: MessageRef) -> str:
    """Resolve validated plain text for an API whose contract requires ``str``."""
    return _resolved_display_text(localizer, reference)


@runtime_checkable
class _AppNode(Protocol):
    """A widget or screen: its ``app`` raises a RuntimeError while no app is active."""

    @property
    def app(self) -> object: ...


@runtime_checkable
class LocalizedApp(Protocol):
    """An app that owns a locale controller: ChrysApp, and test hosts that set one."""

    @property
    def locale_controller(self) -> LocaleController: ...


def widget_locale_controller(widget: object) -> LocaleController | None:
    """Resolve the active app's locale controller for a leaf widget or screen.

    None when the widget is detached or the hosting app is not a ChrysApp
    (bare test hosts): Textual types ``widget.app`` as a plain ``App``.
    """
    if not isinstance(widget, _AppNode):
        return None
    try:
        app = widget.app
    except RuntimeError:
        return None
    return app.locale_controller if isinstance(app, LocalizedApp) else None


def widget_localizer(widget: object) -> Localizer:
    """Resolve the active-app localizer for a leaf widget or screen.

    Falls back to a localizer for the default locale (the system's language)
    when the widget is detached or the hosting app is not a ChrysApp (bare
    test hosts).
    """
    controller = widget_locale_controller(widget)
    return Localizer(DEFAULT_LOCALE) if controller is None else controller.localizer


class LocaleController:
    """Own one Localizer and coordinate an O(chrome) locale refresh pass."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        localizer: Localizer | None = None,
        settings_handle: SettingsHandle | None = None,
    ) -> None:
        if settings_handle is not None and settings is not None and settings is not settings_handle.settings:
            error_message = "Pass either settings or settings_handle, not two different ones."
            raise ValueError(error_message)
        # The handle rather than a Settings snapshot: a settings reload
        # installs a new base into the shared handle, and a snapshot taken
        # here would keep answering with a locale the app no longer claims.
        # A bare controller (tests, detached hosts) owns its settings alone.
        # Writes go through ``override`` in both shapes, so the chosen locale
        # keeps its provenance and survives later reloads.
        self._settings_handle = settings_handle or SettingsHandle(
            LoadedSettings(settings=settings or Settings(), provenance={})
        )
        self._localizer = localizer or Localizer(self._settings_handle.settings.locale)
        self._revision = 0
        self._surfaces: WeakSet[LocaleAwareSurface] = WeakSet()
        self._footers: WeakSet[LocalizationFooter] = WeakSet()

    @property
    def localizer(self) -> Localizer:
        """Return the live Localizer shared by mount-time presentation paths."""
        return self._localizer

    @property
    def requested_locale(self) -> str:
        """Return the requested value Settings currently hold."""
        return self._settings_handle.settings.locale

    @property
    def effective_locale(self) -> str:
        """Return the locale of the Localizer's active immutable bundle."""
        return self._localizer.effective_locale

    @property
    def revision(self) -> int:
        """Return the number of successfully installed effective-locale changes."""
        return self._revision

    def register_surface(self, surface: LocaleAwareSurface) -> None:
        """Weakly register one live surface for text-only locale refreshes.

        Implementations must update text inside already-reserved geometry. The
        callback must not recompose an outer container, restyle the App, or walk
        mounted transcript widgets.
        """
        self._surfaces.add(surface)

    def unregister_surface(self, surface: LocaleAwareSurface) -> None:
        """Remove a surface registration before or during unmount."""
        self._surfaces.discard(surface)

    def register_footer(self, footer: LocalizationFooter) -> None:
        """Weakly register one mounted registry-resolving footer."""
        self._footers.add(footer)

    def unregister_footer(self, footer: LocalizationFooter) -> None:
        """Remove a footer registration before or during unmount."""
        self._footers.discard(footer)

    def switch_locale(self, requested_locale: str) -> LocaleSwitchResult:
        """Apply one requested locale, distinguishing persistence from bundle work."""
        # Identical only when nothing is left to do on either side: Settings
        # already hold the value AND the bundle was last asked for it. After
        # a reload changes Settings underneath a still-old bundle, picking
        # the new value must fall through and finish the switch.
        if requested_locale == self.requested_locale and requested_locale == self._localizer.requested_locale:
            return LocaleSwitchResult(LocaleSwitchStatus.IDENTICAL_REQUEST)

        previous_effective = self._localizer.effective_locale
        warning = self._localizer.switch_locale(requested_locale)
        if warning is not None:
            return LocaleSwitchResult(LocaleSwitchStatus.LOAD_FAILED, warning)

        self._settings_handle.override(locale=requested_locale)
        persist_error = persist_locale(requested_locale)
        if persist_error is None:
            self._settings_handle.confirm_written({"ui.locale": requested_locale})
        return replace(self._present(previous_effective), persist_error=persist_error)

    def follow_settings(self) -> LocaleSwitchResult:
        """Put the locale the settings now hold on screen, which is neither a choice nor a write."""
        requested_locale = self.requested_locale
        if requested_locale == self._localizer.requested_locale:
            return LocaleSwitchResult(LocaleSwitchStatus.IDENTICAL_REQUEST)
        previous_effective = self._localizer.effective_locale
        warning = self._localizer.switch_locale(requested_locale)
        if warning is not None:
            return LocaleSwitchResult(LocaleSwitchStatus.LOAD_FAILED, warning)
        return self._present(previous_effective)

    def _present(self, previous_effective: str) -> LocaleSwitchResult:
        """Redraw the registered surfaces if the bundle just installed changed what they say."""
        if self._localizer.effective_locale == previous_effective:
            return LocaleSwitchResult(LocaleSwitchStatus.REQUESTED_ONLY)
        self._revision += 1
        self._refresh_registered_presentation()
        return LocaleSwitchResult(LocaleSwitchStatus.EFFECTIVE_CHANGED)

    def _refresh_registered_presentation(self) -> None:
        for surface in tuple(self._surfaces):
            try:
                surface.refresh_localization()
            except Exception:
                logger.exception("Failed to refresh a locale-aware TUI surface.")
        for footer in tuple(self._footers):
            try:
                footer.invalidate_localization()
            except Exception:
                logger.exception("Failed to invalidate a localized TUI footer.")


__all__ = [
    "LocaleAwareSurface",
    "LocaleController",
    "LocaleSwitchResult",
    "LocaleSwitchStatus",
    "LocalizationFooter",
    "render_content",
    "render_str",
    "render_text",
]
