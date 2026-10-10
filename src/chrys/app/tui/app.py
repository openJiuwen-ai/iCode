# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Chrys TUI — Textual-based terminal UI entry point.

This is the Textual app entry point used by ``chrys``.
It launches the Textual app which communicates with the backend Engine
exclusively through the EventBus.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import inspect
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual import __version__ as TEXTUAL_VERSION
from textual import events as textual_events
from textual.app import App, ScreenStackError
from textual.await_remove import AwaitRemove
from textual.css.stylesheet import Stylesheet, StylesheetError
from textual.css.tokenizer import TokenError
from textual.dom import DOMNode, NoScreen
from textual.errors import NoWidget
from textual.screen import Screen
from textual.theme import Theme
from textual.widget import Widget

from chrys import __version__
from chrys.app.cli.launch_cwd import launch_cwd_missing_message
from chrys.app.features.buddy.lifecycle import on_successful_turn as on_buddy_successful_turn
from chrys.app.features.session_title import SessionTitleUpdater
from chrys.app.parsing import SanitizingArgumentParser
from chrys.app.tui.behaviors.insert_clipboard import INSERT_CLIPBOARD_BINDINGS, copy_with_insert, paste_with_insert
from chrys.app.tui.binding_display import QUIT_BINDING, localized_binding
from chrys.app.tui.i18n import LocaleController, render_str
from chrys.app.tui.notifications import NotificationService, NotificationSettings
from chrys.app.tui.screens.main import MainScreen
from chrys.app.tui.screens.settings import SettingsDialog
from chrys.app.tui.screens.settings.dialog import THEME_PREVIEW_HINT
from chrys.app.tui.support.gc_freeze import (
    GC_FREEZE_ENABLED,
    GC_FREEZE_WATCHDOG_PERIOD_SECONDS,
    GcAbsorbReason,
    GcAbsorbRequested,
    GcFreezeBlockReason,
    GcFreezeCoordinator,
    GcReclaimReason,
    GcReclaimRequested,
)
from chrys.app.tui.terminal.program_status import ProgramStatusReporter
from chrys.app.tui.terminal.title import (
    running_under_textual_web_driver,
    set_app_terminal_title_for_current_cwd,
    set_terminal_title_for_current_cwd,
)
from chrys.app.tui.theme import (
    CHRYS_ANSI_THEME,
    CHRYS_LEGACY_THEME,
    CHRYS_THEME,
    TuiVariableDefaultsMixin,
    with_tui_css_variables,
)
from chrys.app.tui.theme_loader import load_user_themes, user_theme_warning
from chrys.app.tui.theme_stylesheet import ThemeStylesheet
from chrys.app.tui.themes.preview import ThemePreview
from chrys.app.tui.toasts import TEXTUAL_TOAST_RACK_FORK_VERSION, refresh_newest_first
from chrys.app.tui.util.debug_log import configure_debug_logging, stop_debug_logging
from chrys.app.tui.widgets.chrome.status_bar import STATUS_AGENT_STARTUP_FAILED
from chrys.foundation import platform as chrys_platform
from chrys.foundation.branding import APP_DISPLAY_NAME, format_app_version_title
from chrys.foundation.config.runtime_pointer import set_model_pointer
from chrys.foundation.config.settings import DEFAULT_AGENT_PROFILE, DEFAULT_THEME, Settings, persist_theme
from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle
from chrys.foundation.config.spec import SettingOrigin, Source
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import Warning
from chrys.foundation.i18n import Localizer, MessageRef, msg
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.util.session_ids import session_short_id
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.engine import AgentEngine
from chrys.orchestration.startup import catalog_load_warning
from chrys.service.profiles.agents.registry import AgentProfileRegistry, resolve_profile_selector
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.resolver import (
    format_available_profile_labels,
    loaded_with_active_model_profile,
)
from chrys.service.profiles.models.resolver import (
    resolve_profile_selector as resolve_model_profile_selector,
)
from chrys.service.state.store import JsonFileStateStore

logger = logging.getLogger(__name__)

_SESSION_NOTIFICATION_TITLE = msg("tui.startup.title.session", fallback="Session")
_AGENT_STARTUP_FAILED_TITLE = msg(
    "tui.startup.title.agent_failed",
    fallback="Agent startup failed",
)
_SESSION_READ_FAILED = msg(
    "tui.startup.session.read_failed",
    fallback="Could not read session {session_id}: {error}",
)
_SESSION_NOT_FOUND = msg(
    "tui.startup.session.not_found",
    fallback="Session {session_id} was not found.",
)
_SESSION_RESTORE_FALLBACK = msg(
    "tui.startup.session.restore_fallback",
    fallback="Could not restore session {session_id}; started a new session instead.",
)
_SESSION_RESTORE_FAILED = msg(
    "tui.startup.session.restore_failed",
    fallback="Could not restore session {session_id}: {error}",
)
_QUIT_HELP = msg("tui.app.quit_help", fallback="Press {key} to quit the app")
_QUIT_HELP_TITLE = msg("tui.app.quit_help_title", fallback="Do you want to quit?")

_GC_FREEZE_DISABLED_WARNING_CODE = "gc_freeze_disabled"
_TEXTUAL_LOAD_SCREEN_CSS_FORK_VERSION = "8.2.7"
_GC_ACTIVITY_EVENTS = (
    textual_events.Key,
    textual_events.Paste,
    textual_events.MouseDown,
    textual_events.MouseUp,
    textual_events.MouseScrollDown,
    textual_events.MouseScrollUp,
    textual_events.MouseScrollLeft,
    textual_events.MouseScrollRight,
    textual_events.Resize,
    textual_events.AppFocus,
)

_TERMINAL_MODE_RESET = (
    "\x1b[?1000l"  # Disable VT200 mouse
    "\x1b[?1002l"  # Disable button-event mouse
    "\x1b[?1003l"  # Disable any-event mouse
    "\x1b[?1005l"  # Disable UTF-8 extended mouse
    "\x1b[?1006l"  # Disable SGR mouse
    "\x1b[?1015l"  # Disable urxvt mouse
    "\x1b[?1004l"  # Disable focus-in/focus-out reports
    "\x1b[?1007l"  # Disable alternate-scroll mouse mode
    "\x1b[?25h"  # Show cursor
    "\x1b[?2004l"  # Disable bracketed paste
    "\x1b[>4;0m"  # Disable xterm modifyOtherKeys
    "\x1b[?9001l"  # Disable win32-input-mode
    "\x1b[<u"  # Disable Kitty keyboard protocol
    "\x1b[=0;1u"  # Hard-reset Kitty keyboard flags
)
if TYPE_CHECKING:
    from textual.timer import Timer

    from chrys.app.tui.widgets.markdown.diagram.messages import DiagramOpenRequested
    from chrys.app.update_check import UpdateCheck
    from chrys.service.profiles.agents.schema import AgentProfile


def _resolve_startup_profile(registry: AgentProfileRegistry, preferred_name: str) -> AgentProfile | None:
    """Resolve the TUI launch profile, falling back through Code to the first visible profile."""
    requested = preferred_name.strip() or DEFAULT_AGENT_PROFILE
    # Match the TUI picker: hidden and sub-agent-only profiles are not valid main launch profiles.
    profiles = registry.list_profiles()
    by_name = {profile.name: profile for profile in profiles}

    profile = resolve_profile_selector(profiles, requested)
    if profile is not None:
        return profile

    if requested != DEFAULT_AGENT_PROFILE:
        fallback = by_name.get(DEFAULT_AGENT_PROFILE)
        if fallback is not None:
            logger.warning(
                "Default agent profile %r is not usable as a launch profile, falling back to %r.",
                requested,
                DEFAULT_AGENT_PROFILE,
            )
            return fallback

    if profiles:
        logger.warning(
            "Default agent profile %r is not usable as a launch profile, falling back to %r.",
            requested,
            profiles[0].name,
        )
        return profiles[0]
    return None


def _apply_startup_active_model_selection(
    parser: argparse.ArgumentParser,
    loaded: LoadedSettings,
    model_registry: ModelProfileRegistry,
    model_profile: str,
) -> LoadedSettings:
    model = model_profile.strip()
    if not model:
        return loaded
    profile = resolve_model_profile_selector(model_registry, model)
    if profile is None:
        error_message = f"Model profile not found: {model}"
        available = format_available_profile_labels(model_registry)
        if available:
            error_message = f"{error_message}. Available model profiles: {available}"
        parser.error(error_message)
    # Keep startup --model durable across SettingsReload without writing the
    # user's .env. ModelConfigScreen may still replace this process-local value
    # later when the user intentionally applies a different active profile.
    set_model_pointer(profile.id, origin=SettingOrigin(layer=Source.CLI))
    return loaded_with_active_model_profile(loaded, profile, Source.CLI)


def _restore_terminal_after_run(app: ChrysApp) -> None:
    """Restore terminal state after a native TUI run."""
    if running_under_textual_web_driver():
        return

    import sys

    reset = _TERMINAL_MODE_RESET
    # Include alt-screen exit only when Textual exited normally. On error exit,
    # Textual already sent \x1b[?1049l in stop_application_mode() before
    # printing the traceback to stderr.
    if app._return_code == 0:
        reset += "\x1b[?1049l"  # Exit alt screen
    sys.stderr.write(reset)
    sys.stderr.flush()


def _reset_terminal_title_after_run() -> None:
    """Leave the host shell with a cwd title instead of the last prompt preview."""
    set_terminal_title_for_current_cwd()


class ChrysApp(TuiVariableDefaultsMixin, App):
    """Textual application for Chrys."""

    TITLE = format_app_version_title(__version__)
    CSS_PATH: ClassVar[list[str]] = ["chrys.tcss", "screens/themes/editor.tcss"]
    COMMAND_PALETTE_BINDING = "ctrl+q"
    BINDINGS: ClassVar[list] = [
        # Application fallbacks cover selectable screens and stock Textual
        # controls. Chrys editors bind these keys themselves so specialized
        # paste behavior (image paste and Vim mode gating) runs first, and
        # modal roots repeat the bindings because Textual stops the
        # non-priority binding chain at a modal screen.
        *INSERT_CLIPBOARD_BINDINGS,
        # Shadow stock App's ctrl+q so screens without their own quit binding
        # (e.g. DiffScreen) still resolve a localized command-palette slot.
        localized_binding("ctrl+q", "quit", QUIT_BINDING, show=False, priority=True),
    ]

    def __init__(
        self,
        event_bus: EventBus,
        engine: AgentEngine,
        settings: Settings | None = None,
        profile_name: str = "",
        state_store: JsonFileStateStore | None = None,
        agent_registry: AgentProfileRegistry | None = None,
        model_registry: ModelProfileRegistry | None = None,
        startup_warnings: list[Warning] | None = None,
        settings_warnings: list[Warning] | None = None,
        startup_session_id: str = "",
        apply_saved_model_on_restore: bool = True,
        session_title_updater: SessionTitleUpdater | None = None,
        *,
        localizer: Localizer | None = None,
        settings_handle: SettingsHandle | None = None,
        gc_freeze_enabled: bool | None = None,
        gc_freeze_measure_counts: bool = False,
        update_check: UpdateCheck | None = None,
    ) -> None:
        self._bus = event_bus
        self._engine = engine
        if settings_handle is not None and settings is not None and settings is not settings_handle.settings:
            error_message = "Pass either settings or settings_handle, not two different ones."
            raise ValueError(error_message)
        # ``main()`` passes the engine's handle, so the two read one value
        # rather than two that started equal. An app built without one owns
        # its settings alone.
        self._settings_handle = settings_handle or SettingsHandle(
            LoadedSettings(settings=settings or Settings.from_env(), provenance={})
        )
        self._locale_controller = LocaleController(
            localizer=localizer,
            settings_handle=self._settings_handle,
        )
        self._profile_name = (profile_name or self._settings.default_agent).strip() or DEFAULT_AGENT_PROFILE
        self._state_store = state_store or JsonFileStateStore()
        self._agent_registry = agent_registry or AgentProfileRegistry()
        self._model_registry = model_registry or ModelProfileRegistry()
        # Warnings collected during pre-mount setup (e.g. invalid env
        # values scrubbed before the bus had subscribers); flushed after
        # MainScreen subscribes to ``Warning`` so the user sees them as
        # toast notifications.
        self._startup_session_id = startup_session_id.strip()
        self._startup_warnings: list[Warning] = list(startup_warnings or [])
        # Settings-derived warnings describe the bootstrap load rooted at the
        # process cwd. A startup restore replaces those settings with ones
        # loaded from the restored session's own root — and the restore
        # republishes that load's warnings itself — so flushing these up
        # front would report a repository the session doesn't live in (or
        # duplicate every warning when the roots coincide). Deferred until
        # the restore's outcome is known; dropped on success, flushed when
        # the app falls back to the bootstrap settings.
        self._deferred_settings_warnings: list[Warning] = []
        if self._startup_session_id:
            self._deferred_settings_warnings.extend(settings_warnings or [])
        else:
            self._startup_warnings.extend(settings_warnings or [])
        if self.localizer.first_load_warning is not None:
            self._startup_warnings.append(catalog_load_warning(self.localizer.first_load_warning))
        self._apply_saved_model_on_restore = apply_saved_model_on_restore
        self._session_title_updater = session_title_updater
        # OSC 7501 reporting lives and dies with the app: subscribed before
        # any screen mounts (so the first turn event is never missed) and
        # torn down first on unmount.
        self._program_status_reporter = ProgramStatusReporter(event_bus, app_provider=lambda: self)
        # Projected from the loaded settings: bootstrap folds any legacy
        # ``notifications.yaml`` into the settings document before those
        # settings are loaded, so this view never reads the retired file.
        self._notification_service = NotificationService(
            self,
            NotificationSettings.from_settings(self._settings),
            locale_controller=self._locale_controller,
        )
        # Crash-log state: first crash within a run truncates error.log,
        # subsequent crashes append (so cascading teardown errors survive).
        self._crash_log_initialized = False
        self._startup_task: asyncio.Task[None] | None = None
        self._update_check = update_check
        self._update_check_task: asyncio.Task[None] | None = None
        self._main_screen: MainScreen | None = None
        self._gc_freeze_watchdog: Timer | None = None
        self._gc_pointer_buttons_down: set[int] = set()
        self._gc_effective_theme = ""
        # Set before ``super().__init__()``: Textual initialization can invoke
        # the theme watcher, which must not treat startup as a user's choice.
        self._theme_persist_armed = False
        self.theme_preview: ThemePreview | None = None
        self._theme_preview_source = ""
        self._theme_selection_depth = 0
        freeze_enabled = GC_FREEZE_ENABLED if gc_freeze_enabled is None else gc_freeze_enabled
        # Textual initialization may invoke reactive watchers, so the
        # coordinator exists first.  It stays inert until ``start()`` after the
        # main screen has mounted.
        self._gc_freeze = GcFreezeCoordinator(
            self,
            enabled=freeze_enabled,
            measure_freeze_counts=gc_freeze_measure_counts,
        )
        super().__init__()
        self.register_theme(CHRYS_THEME)
        self.register_theme(CHRYS_LEGACY_THEME)
        self.register_theme(CHRYS_ANSI_THEME)
        # User themes come from <config_dir>/themes/*.yaml; per-file failures
        # surface as startup warnings and never block the app.
        user_themes, user_theme_warnings = load_user_themes()
        for user_theme in user_themes:
            self.register_theme(user_theme)
        self._user_theme_names = {theme.name for theme in user_themes}
        stylesheet = ThemeStylesheet(variables=self.get_css_variables(), recover=self._recover_user_theme)
        stylesheet.source = self.stylesheet.source.copy()
        self.stylesheet = stylesheet
        self._startup_warnings.extend(user_theme_warnings)
        # Resolve the persisted theme; fall back to the default if empty,
        # unknown, or unregistered.
        theme_name = self._settings.theme
        if not theme_name or theme_name not in self.available_themes:
            theme_name = DEFAULT_THEME
        self.theme = theme_name
        # Only now may a theme change be written back. The fallback above is a
        # decision about what to *draw* when the saved name resolves to nothing
        # — a theme renamed between releases, a typo, a value some other writer
        # damaged. Persisting it would delete the name the user chose, which is
        # the one thing the loader's ``FALL_THROUGH`` policy exists to preserve:
        # an unusable value costs them this session's colours, not the setting.
        self._theme_persist_armed = True

    @property
    def _settings(self) -> Settings:
        """Read-only: settings change through the handle, not the holder."""
        return self._settings_handle.settings

    @property
    def _loaded_settings(self) -> LoadedSettings:
        return self._settings_handle.loaded

    @property
    def settings_handle(self) -> SettingsHandle:
        """The shared settings cell, for screens that project or overlay it."""
        return self._settings_handle

    @property
    def engine(self) -> AgentEngine:
        """Public accessor for the backend engine.

        Screens and widgets should read the engine through this property
        rather than poking at ``self.app._engine`` — keeps the
        private-attr boundary explicit and lets the TUI layer mock a
        stand-in engine in tests by subclassing ``ChrysApp``.
        """
        return self._engine

    @property
    def localizer(self) -> Localizer:
        """Application-owned display localizer."""
        return self._locale_controller.localizer

    @property
    def locale_controller(self) -> LocaleController:
        """Application-owned locale switch and live-surface controller."""
        return self._locale_controller

    @property
    def notification_service(self) -> NotificationService:
        """Runtime desktop notification service."""
        return self._notification_service

    def override_notification_settings(self, settings: NotificationSettings) -> None:
        """Install a live notification choice in the shared handle.

        The same pairing every other live surface uses — theme, locale and the
        approval mode each persist *and* overlay — and for the same reason. The
        write to disk is debounced, so between the edit and the write the handle
        is the only place the choice exists; a reload landing in that window
        installs the pre-edit document, and :meth:`refresh_notification_settings`
        then projects it straight back over the delivery service. The overlay
        survives that reload by construction, which is what makes the edit
        survive it too.
        """
        self._settings_handle.override(**settings.to_settings_fields())

    def refresh_notification_settings(self) -> None:
        """Re-project the notification settings into the delivery service.

        The service holds a projection taken from ``Settings``, not the
        settings themselves — so when a reload installs new values into the
        shared handle, the LIVE ``notifications.*`` fields only take effect
        once someone projects them again. This is that someone.
        """
        self._notification_service.update_settings(NotificationSettings.from_settings(self._settings))

    def freeze_block_reason(self) -> GcFreezeBlockReason | None:
        """Return the App-owned top-screen gate for GC freeze dispatch."""
        if self._gc_pointer_buttons_down:
            return GcFreezeBlockReason.POINTER_BUTTON_HELD
        # Tests use deliberately tiny engine stand-ins.  Production always
        # owns AgentEngine, whose run task remains active through session save
        # and after-turn hooks even after the UI has rendered its terminal.
        if isinstance(self._engine, AgentEngine) and self._engine.is_turn_lifecycle_active:
            return GcFreezeBlockReason.BACKEND_TURN_LIFECYCLE
        if self._main_screen is None or self.screen is not self._main_screen:
            return GcFreezeBlockReason.TOP_SCREEN
        return self._main_screen.gc_freeze_block_reason()

    def prepare_for_gc_freeze(self) -> None:
        """Prepare mounted freeze participants.

        The MainScreen aggregate owns the concrete participant list.
        """
        if self._main_screen is not None:
            self._main_screen.prepare_for_gc_freeze()

    def after_gc_freeze(self) -> None:
        """Renew mounted mutable cyclic participants after a freeze.

        The MainScreen aggregate owns the concrete participant list.
        """
        if self._main_screen is not None:
            self._main_screen.after_gc_freeze()

    def abort_gc_freeze(self) -> None:
        """Best-effort normalize mounted caches after an incomplete hook pass."""
        if self._main_screen is not None:
            self._main_screen.abort_gc_freeze()

    def on_gc_absorb_requested(self, event: GcAbsorbRequested) -> None:
        """Forward a UI-local absorb request with its source timestamp."""
        self._gc_freeze.request_absorb(
            reason=event.reason,
            terminal_boundary=event.terminal_boundary,
            requested_at=event.time,
        )

    def on_gc_reclaim_requested(self, event: GcReclaimRequested) -> None:
        """Forward a UI-local reclaim request with its source timestamp."""
        self._gc_freeze.request_reclaim(
            reason=event.reason,
            prompt=event.prompt,
            requested_at=event.time,
            removed=event.removed,
        )

    def on_diagram_open_requested(self, event: DiagramOpenRequested) -> None:
        """Present a Markdown diagram without introducing a widget-to-screen import."""
        from chrys.app.tui.screens.dialogs.diagram import DiagramDialog

        event.stop()
        self.push_screen(DiagramDialog(event.diagram))

    def _watch_app_focus(self, focus: bool) -> None:
        """Preserve native widget focus without restyling or relayout.

        Textual clears and restores the focused widget across App blur/focus.
        Each transition refreshes bindings, which makes Footer request several
        full-screen layouts. Chrys's native UI has no App-focus-dependent CSS,
        and it receives no input while the terminal is unfocused, so retaining
        the internal focused widget is both sufficient and transcript-size
        independent. The web host keeps Textual's exact behavior.
        """
        if running_under_textual_web_driver():
            super()._watch_app_focus(focus)
            return

        self._last_focused_on_app_blur = None

    def _on_app_focus(self, event: textual_events.AppFocus) -> None:  # ty: ignore[invalid-method-override]  # Textual dispatch accepts sync handlers.
        """Record focus and recover bindings for the visible screen only."""
        self._notification_service.set_focused(True)
        if running_under_textual_web_driver():
            return
        event.prevent_default()
        self.app_focus = True
        screen_stack = self.screen_stack
        if not screen_stack:
            return
        screen = screen_stack[-1]
        if self._main_screen is not None and screen is self._main_screen:
            self._main_screen.sync_footer_bindings()
        else:
            # Stock Footer deliberately ignores binding changes while the App
            # is unfocused. Deferred screens may become interactive during
            # that interval, so mirror Textual's refocus recovery for the
            # active overlay without restyling the suspended transcript.
            screen.refresh_bindings()

    def _on_app_blur(self, event: textual_events.AppBlur) -> None:  # ty: ignore[invalid-method-override]  # Textual dispatch accepts sync handlers.
        """Record blur while retaining the native focused widget."""
        self._notification_service.set_focused(False)
        if running_under_textual_web_driver():
            return
        event.prevent_default()
        self.app_focus = False

    def _load_screen_css(self, screen: Screen) -> None:
        """Load native secondary-screen CSS without restyling the underlay.

        Textual updates the entire App when it discovers a Screen stylesheet.
        Chrys secondary-screen styles are screen-local, so existing MainScreen
        nodes cannot start matching them. Parse globally so subsequently mounted
        children see the new rules, then update only the already-registered
        screen root. Keep Textual's behavior for web and the startup MainScreen.

        This is a version-gated fork of Textual's private
        ``App._load_screen_css`` implementation. The intentional semantic deltas
        are updating ``screen`` rather than the entire App, and parsing the live
        stylesheet in place rather than reparsing into a fresh one: the fresh
        stylesheet's empty rule cache re-tokenizes every source, while the live
        cache only holds rules for the current variables (``set_variables``
        clears it), so only the new sources are tokenized.
        """
        if (
            TEXTUAL_VERSION != _TEXTUAL_LOAD_SCREEN_CSS_FORK_VERSION
            or running_under_textual_web_driver()
            or isinstance(screen, MainScreen)
        ):
            super()._load_screen_css(screen)
            return

        if self.css_monitor is not None:
            self.css_monitor.add_paths(screen.css_path)

        update = False
        for path in screen.css_path:
            if not self.stylesheet.has_source(str(path), ""):
                self.stylesheet.read(path)
                update = True
        if screen.CSS:
            try:
                screen_path = inspect.getfile(screen.__class__)
            except TypeError, OSError:
                screen_path = ""
            screen_class_var = f"{screen.__class__.__name__}.CSS"
            if not self.stylesheet.has_source(screen_path, screen_class_var):
                self.stylesheet.add_source(
                    screen.CSS,
                    read_from=(screen_path, screen_class_var),
                    is_default_css=False,
                    scope=screen._css_type_name if screen.SCOPED_CSS else "",
                )
                update = True
        if update:
            self.stylesheet.parse()
            self.stylesheet.update(screen)

    def _refresh_notifications(self) -> None:
        """Show toasts under the header with the newest on top.

        Textual appends each new toast below the older ones and scrolls the
        rack to its end, the right stacking for its bottom-docked rack. Chrys
        docks the rack under the header, so new toasts mount ahead of the
        older ones and the rack keeps its start in view: the stack grows
        downward from the newest toast.

        This is a version-gated fork of Textual's private
        ``App._refresh_notifications`` and ``ToastRack.show``. The only
        intentional semantic delta is where new toasts mount and which end of
        the rack stays in view.
        """
        if TEXTUAL_VERSION != TEXTUAL_TOAST_RACK_FORK_VERSION:
            super()._refresh_notifications()
            return
        refresh_newest_first(self)

    def _prune(self, *nodes: Widget, parent: DOMNode | None = None) -> AwaitRemove:
        """Prune whole-screen overlays without relayout of the screen beneath.

        Textual's generic DOM prune refreshes the parent with ``layout=True``.
        Screens fill the viewport independently, so removing a Screen from the
        App cannot change MainScreen geometry. Descendant pruning retains the
        stock path, including its required parent layout.
        """
        whole_screens = parent is self and nodes and all(isinstance(node, Screen) for node in nodes)
        if not whole_screens:
            return super()._prune(*nodes, parent=parent)

        # Suppress only App.refresh(layout=True). Textual's normal post-prune
        # path also transfers hover state to the newly revealed screen, which
        # must still happen without moving the mouse.
        await_remove = super()._prune(*nodes, parent=None)
        try:
            revealed_screen = self.screen
        except ScreenStackError, NoScreen:
            return await_remove

        if self.mouse_over is None:
            return await_remove

        async def update_mouse_over_after_remove() -> None:
            await await_remove
            if revealed_screen._running:
                try:
                    mouse_over, hover_over = revealed_screen.get_hover_widgets_at(*self.mouse_position).widgets
                except NoWidget:
                    self._set_mouse_over(None, None)
                else:
                    self._set_mouse_over(mouse_over, hover_over)

        self.call_next(update_mouse_over_after_remove)
        return await_remove

    async def on_event(self, event: textual_events.Event) -> None:
        """Observe GC-relevant activity before Textual dispatches it."""
        if not event.is_forwarded:
            if isinstance(event, textual_events.MouseDown) and event.button:
                self._gc_pointer_buttons_down.add(event.button)
            elif isinstance(event, textual_events.MouseUp) and event.button:
                self._gc_pointer_buttons_down.discard(event.button)
            elif isinstance(event, textual_events.MouseMove):
                if event.button:
                    self._gc_pointer_buttons_down.add(event.button)
                    self._gc_freeze.note_input(occurred_at=event.time)
                elif self._gc_pointer_buttons_down:
                    self._gc_pointer_buttons_down.clear()
            elif isinstance(event, (textual_events.AppBlur, textual_events.AppFocus)):
                self._gc_pointer_buttons_down.clear()

            if isinstance(event, _GC_ACTIVITY_EVENTS):
                self._gc_freeze.note_input(occurred_at=event.time)
                if isinstance(event, textual_events.Resize):
                    self._gc_freeze.request_absorb(
                        reason=GcAbsorbReason.VIEWPORT_UPDATED,
                        terminal_boundary=False,
                        requested_at=event.time,
                    )
        await super().on_event(event)

    @property
    def current_theme(self) -> Theme:
        if self.theme_preview is not None and self.theme_preview.current is not None:
            return self.theme_preview.current
        return super().current_theme

    def get_css_variables(self) -> dict[str, str]:
        if self.theme_preview is not None and self.theme_preview.variables is not None:
            self.theme_variables = self.theme_preview.variables.copy()
            return self.theme_variables
        return super().get_css_variables()

    def begin_theme_preview(self, theme: Theme) -> bool:
        if self.theme_preview is not None:
            return False
        self._theme_preview_source = self.theme
        self.theme_preview = ThemePreview(self)
        return self.theme_preview.show(theme)

    def end_theme_preview(self) -> None:
        preview = self.theme_preview
        if preview is None:
            return
        source = self.get_theme(self.theme)
        restored = source is not None and preview.show(source)
        self.theme_preview = None
        if not restored:
            # Restore classes, Rich consumers and CSS even if preview admission
            # failed. Closing an editor must never persist a fallback selection.
            self.suspend_theme_choice()
            try:
                if source is None:
                    self.set_reactive(App.theme, DEFAULT_THEME)
                self._watch_theme(self.theme)
            finally:
                self.resume_theme_choice()

    def suspend_theme_choice(self) -> None:
        self._theme_selection_depth += 1

    def resume_theme_choice(self) -> None:
        self._theme_selection_depth = max(0, self._theme_selection_depth - 1)

    def register_user_theme(self, theme: Theme) -> None:
        from chrys.app.tui.theme_loader import theme_is_read_only

        if theme_is_read_only(theme.name):
            raise ValueError("Built-in themes are read-only")
        self.register_theme(theme)
        self._user_theme_names.add(theme.name)

    def apply_saved_theme(self, theme: Theme) -> None:
        """Apply an editor's successful save and make it the new preview exit target."""
        self.register_user_theme(theme)
        preview = self.theme_preview
        # Only a completed save commits a selection while editing. Reuse the
        # normal watcher/persistence path, then retain the editor's live preview.
        self.theme_preview = None
        try:
            self.apply_theme_setting(theme.name)
            self._theme_preview_source = self.theme
        finally:
            self.theme_preview = preview

    def unregister_user_theme(self, name: str) -> None:
        """Forget a deleted file and repair its applied selection, including previews."""
        from chrys.app.tui.theme_loader import theme_is_read_only

        if theme_is_read_only(name):
            raise ValueError("Built-in themes are read-only")
        if name in {self.theme, self._settings.theme}:
            preview = self.theme_preview
            self.theme_preview = None
            previous = self.theme
            self.apply_theme_setting(DEFAULT_THEME)
            if preview is not None:
                # Applying a different name schedules the required style pass.
                # An already active fallback also needs to shed its draft CSS.
                if previous == self.theme:
                    self._watch_theme(self.theme)
                self._theme_preview_source = self.theme
                self.theme_preview = ThemePreview(self)
        self.unregister_theme(name)
        self._user_theme_names.discard(name)

    def _watch_theme(self, theme_name: str) -> None:
        """Apply the selected theme and persist confirmed choices."""
        if self.theme_preview is not None:
            self.set_reactive(App.theme, self._theme_preview_source)
            return
        previous_theme = self._gc_effective_theme
        if self.ansi_color is not None:
            self.ansi_color = None
        super()._watch_theme(theme_name)
        # Persist the choice so it survives restart.  Skip no-op writes
        # (startup applies the already-persisted value), ignore transient names
        # not registered as real themes, and stay out of startup entirely —
        # see ``_theme_persist_armed`` in ``__init__``.
        if (
            self._theme_persist_armed
            and theme_name
            and theme_name in self.available_themes
            and theme_name != self._settings.theme
        ):
            self._record_theme_choice(theme_name)
        self._gc_effective_theme = theme_name
        if previous_theme and theme_name != previous_theme:
            self._gc_freeze.request_reclaim(
                reason=GcReclaimReason.THEME_UPDATED,
                prompt=False,
            )

    def _recover_user_theme(self, stylesheet: Stylesheet, error: Exception) -> bool:
        """Fall back only if these same styles work with the default theme."""
        if self.theme_preview is not None:
            return self.theme_preview.recover(stylesheet, error)
        name = self.theme
        if name not in self._user_theme_names:
            return False
        fallback = with_tui_css_variables(
            CHRYS_THEME,
            {**self.get_theme_variable_defaults(), **CHRYS_THEME.to_color_system().generate(), **CHRYS_THEME.variables},
        )
        probe = Stylesheet(variables=fallback)
        probe.source = stylesheet.source.copy()
        try:
            probe.parse()
        except StylesheetError, TokenError:
            # An application CSS error must still surface as an application error.
            return False
        stylesheet.set_variables(fallback)
        # As with an unknown saved theme, recovery must not overwrite the choice.
        armed = self._theme_persist_armed
        self._theme_persist_armed = False
        try:
            self.theme = DEFAULT_THEME
        finally:
            self._theme_persist_armed = armed
        logger.warning("User theme %r failed CSS parsing; using %s", name, DEFAULT_THEME, exc_info=error)
        warning = user_theme_warning(name, f"Theme {name!r} has invalid CSS; using {DEFAULT_THEME}")
        if self._main_screen is None:
            self._startup_warnings.append(warning)
        else:
            self.call_next(self._bus.publish, warning)
        return True

    def _record_theme_choice(self, theme_name: str) -> None:
        """Persist a theme choice and overlay it on the shared handle, as a pair."""
        if self.theme_preview is not None or self._theme_selection_depth:
            return
        persist_theme(theme_name)
        self._settings_handle.override(theme=theme_name)

    def apply_theme_setting(self, theme_name: str) -> bool:
        """Apply and persist an explicit theme choice, including an unchanged fallback.

        Normally the reactive assignment runs ``_watch_theme`` and that
        persists. The exception is a theme already showing but not saved —
        a saved theme that no longer exists fell back to the default at
        startup, and the user now picks that default: the reactive does not
        fire on an equal value, so the persist/overlay pair runs directly.
        """
        if self.theme_preview is not None:
            self.notify(render_str(self.localizer, THEME_PREVIEW_HINT.bind()), severity="warning", markup=False)
            return False
        if theme_name not in self.available_themes:
            return False
        if theme_name == self.theme:
            if theme_name != self._settings.theme:
                self._record_theme_choice(theme_name)
            return True
        self.theme = theme_name
        return True

    def action_command_palette(self) -> None:
        """Repurpose command palette shortcut as quit."""
        self.exit()

    async def action_quit(self) -> None:
        """Ctrl+Q with a modal on top: let the Settings dialog commit what is being typed first.

        The dialog may sit under another modal (a confirmation, a file picker),
        so the whole stack is checked rather than the top screen alone.
        """
        for screen in self.screen_stack:
            if isinstance(screen, SettingsDialog):
                screen.commit_pending()
        self.exit()

    def action_copy_with_insert(self) -> None:
        """Copy the focused editor selection, then fall back to the screen selection."""
        copy_with_insert(self)

    def action_paste_with_insert(self) -> None:
        """Route clipboard text through the same Paste event used by the terminal driver."""
        paste_with_insert(self)

    def action_help_quit(self) -> None:
        """Localize stock App's inherited ctrl+c hint (English notify)."""
        for key, active_binding in self.active_bindings.items():
            if active_binding.binding.action in ("quit", "app.quit"):
                self.notify(
                    render_str(self.localizer, _QUIT_HELP.bind(key=key)),
                    title=render_str(self.localizer, _QUIT_HELP_TITLE.bind()),
                    markup=False,
                )
                return

    def _record_editor_keymap_override(self, value: str) -> None:
        """Record the editor's keymap pick like the theme and locale pickers.

        A RUNTIME override on the shared handle keeps settings describing the
        screen — the persisting write next to it swallows failures, and a
        reload must not un-switch the visible keymap.
        """
        self._settings_handle.override(editor_keymap=value)

    def _build_main_screen(self) -> MainScreen:
        """Build the main screen mounted at startup.

        Subclasses can override this factory to provide a MainScreen variant
        while reusing the standard app bootstrap.
        """
        return MainScreen(
            self._bus,
            state_store=self._state_store,
            agent_registry=self._agent_registry,
            model_registry=self._model_registry,
            active_model_profile_id=self._settings.model_profile,
            apply_saved_model_on_restore=self._apply_saved_model_on_restore,
            workspace_mru_max_entries=self._settings.workspace_mru_max_entries,
            editor_keymap=self._settings.editor_keymap,
            trajectory_verify_commands=self._settings.trajectory_verify_commands,
            engine_provider=lambda: self._engine,
            settings_handle=self._settings_handle,
            localization=self.localizer,
            locale_controller=self._locale_controller,
            on_editor_keymap_changed=self._record_editor_keymap_override,
            tool_groups_expanded=lambda: self._settings_handle.settings.tool_groups_expanded,
            approval_mode=self._engine.approval_mode,
        )

    async def on_mount(self) -> None:
        """Push the main screen and start the engine."""
        await self._program_status_reporter.subscribe()
        self._set_terminal_title_for_cwd()
        screen = self._build_main_screen()
        await self.push_screen(screen)
        self._main_screen = screen
        if self._update_check is not None:
            self._update_check_task = asyncio.create_task(self._run_update_check(self._update_check, screen))
        self._gc_freeze.start()
        if self._gc_freeze.startup_warning is not None:
            self._startup_warnings.append(
                Warning(code=_GC_FREEZE_DISABLED_WARNING_CODE, message=self._gc_freeze.startup_warning)
            )
        if self._gc_freeze.started:
            self._gc_freeze_watchdog = self.set_interval(GC_FREEZE_WATCHDOG_PERIOD_SECONDS, self._gc_freeze.on_tick)

        # MainScreen has now subscribed to ``Warning``; flush any
        # warnings collected before the bus had listeners.
        for w in self._startup_warnings:
            await self._bus.publish(w)
        self._startup_warnings.clear()

        registry = self._agent_registry
        if not registry.list_profiles():
            registry.load_all()

        profile = _resolve_startup_profile(registry, self._profile_name)

        if profile is not None:
            screen.set_startup_agent_loading(True)
            self._startup_task = asyncio.create_task(self._start_engine(profile, screen))
        else:
            # No engine start means no restore either: the bootstrap settings
            # stay in force, so their warnings are due after all.
            await self._flush_deferred_settings_warnings()

    async def _run_update_check(self, check: UpdateCheck, screen: MainScreen) -> None:
        # A check that fails in any way just leaves the welcome screen as it is.
        try:
            await check.run(screen.show_update_notice)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Update check failed", exc_info=True)

    async def _flush_deferred_settings_warnings(self) -> None:
        for w in self._deferred_settings_warnings:
            await self._bus.publish(w)
        self._deferred_settings_warnings.clear()

    async def _start_engine(self, profile: AgentProfile, screen: MainScreen) -> None:
        """Start the backend without holding Textual's mount path."""
        try:
            if self._startup_session_id:
                # Session restore builds the saved profile/runtime itself. Preparing
                # subscriptions first avoids building the default agent only to tear
                # it down immediately in the restore path.
                await self._engine.prepare(profile)
                if await self._restore_startup_session(screen):
                    # The restore republished the warnings of its own settings
                    # load; the bootstrap ones no longer describe anything live.
                    self._deferred_settings_warnings.clear()
                    return
                await self._engine.reset_after_failed_startup_restore()
                await self._flush_deferred_settings_warnings()
            await self._engine.start(profile)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if screen.is_startup_agent_loading():
                screen.set_startup_agent_loading(False)
                self._show_startup_failure(screen, exc)
            logger.exception("Engine startup failed")

    async def _restore_startup_session(self, screen: MainScreen) -> bool:
        """Restore the requested startup session, returning whether it succeeded."""
        session_id = self._startup_session_id
        if not session_id:
            return False
        self._startup_session_id = ""
        screen.set_startup_agent_loading(True)
        try:
            meta = await self._state_store.load_session_meta(session_id)
        except Exception as exc:
            screen.set_startup_agent_loading(False)
            logger.warning("Failed to read startup session %s", session_id, exc_info=True)
            self._show_startup_session_warning(
                screen,
                _SESSION_READ_FAILED.bind(session_id=session_short_id(session_id), error=str(exc)),
            )
            return False
        if meta is None:
            screen.set_startup_agent_loading(False)
            self._show_startup_session_warning(
                screen,
                _SESSION_NOT_FOUND.bind(session_id=session_short_id(session_id)),
            )
            return False
        session_id = meta.session_id or session_id
        await self._dismiss_startup_load_dialog_before_restore(screen)
        try:
            outcome = await screen.restore_startup_session(session_id)
            if outcome == "restored":
                return True
            screen.cancel_startup_session_restore()
            if outcome == "declined":
                # The user chose no folder for a session whose folder is gone: start fresh, quietly.
                return False
            logger.warning("Startup session restore produced no SessionRestored event for %s", session_id)
            self._show_startup_session_warning(
                screen,
                _SESSION_RESTORE_FALLBACK.bind(session_id=session_short_id(session_id)),
            )
            return False
        except Exception as exc:
            screen.cancel_startup_session_restore()
            screen.set_startup_agent_loading(False)
            logger.warning("Failed to restore startup session %s", session_id, exc_info=True)
            self._show_startup_session_warning(
                screen,
                _SESSION_RESTORE_FAILED.bind(session_id=session_short_id(session_id), error=str(exc)),
            )
            return False

    async def _dismiss_startup_load_dialog_before_restore(self, screen: MainScreen) -> None:
        """Clear the startup loading modal before opening the restore modal."""
        await screen.dismiss_startup_load_dialog_before_restore()

    def _show_startup_session_warning(self, screen: MainScreen, message: MessageRef | str) -> None:
        with contextlib.suppress(Exception):
            from chrys.app.tui.widgets.chrome.status_bar import StatusBar

            screen.query_one(StatusBar).flash(message, error=True)
        with contextlib.suppress(Exception):
            screen.notify(
                message if isinstance(message, str) else render_str(self.localizer, message),
                title=render_str(self.localizer, _SESSION_NOTIFICATION_TITLE.bind()),
                severity="warning",
                timeout=6,
                markup=False,
            )

    def _set_terminal_title_for_cwd(self) -> None:
        """Set the terminal title for the active workspace directory."""
        set_app_terminal_title_for_current_cwd(self)

    def _show_startup_failure(self, screen: MainScreen, exc: Exception) -> None:
        """Surface startup failures that occur before AgentLoadStarted exists."""
        message = str(exc) or type(exc).__name__
        with contextlib.suppress(Exception):
            from chrys.app.tui.widgets.chrome.status_bar import StatusBar

            screen.query_one(StatusBar).flash(STATUS_AGENT_STARTUP_FAILED.bind(message=message), error=True)
        with contextlib.suppress(Exception):
            screen.notify(
                message,
                title=render_str(self.localizer, _AGENT_STARTUP_FAILED_TITLE.bind()),
                severity="error",
                timeout=10,
                markup=False,
            )

    async def on_unmount(self) -> None:
        """Shutdown engine on exit."""
        await self._program_status_reporter.unsubscribe()
        self._gc_freeze.close()
        if self._gc_freeze_watchdog is not None:
            self._gc_freeze_watchdog.stop()
            self._gc_freeze_watchdog = None
        if self._startup_task is not None and not self._startup_task.done():
            self._startup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._startup_task
        self._startup_task = None
        if self._update_check_task is not None and not self._update_check_task.done():
            self._update_check_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._update_check_task
        self._update_check_task = None
        await self._engine.shutdown()
        # After the engine: its shutdown drains a still-finalizing run
        # whose success callback can schedule one last title task; the
        # updater must be the last thing torn down so that task is
        # cancelled and awaited (its closed flag backstops other hosts).
        if self._session_title_updater is not None:
            await self._session_title_updater.shutdown()

    def _handle_exception(self, error: Exception) -> None:
        """Mirror Textual's crash handling, but also dump the traceback to a file.

        Textual's default path renders the traceback to ``self.error_console``
        (stderr) only after the terminal has been restored at shutdown.  That
        is great for visibility but the trace is lost the moment the user
        clears the terminal.  Writing it to ``~/.chrys/logs/error.log`` gives
        the user a persistent copy without changing Textual's console output.
        """
        # File logging must NEVER break the crash-handling path.
        with contextlib.suppress(Exception):
            self._write_crash_log(error)
        super()._handle_exception(error)

    def _write_crash_log(self, error: Exception) -> None:
        """Write a plain-text crash report to ``~/.chrys/logs/error.log``.

        The first crash within a single app run truncates the file (so stale
        data from a previous run never confuses the user); subsequent crashes
        in the same run append, preserving cascading teardown errors that
        Textual would otherwise collect into ``_exit_renderables``.
        """
        import traceback
        from datetime import datetime

        pinfo = chrys_platform.get_platform()
        log_path = pinfo.config_dir / "logs" / "error.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)

        mode = "w" if not self._crash_log_initialized else "a"
        self._crash_log_initialized = True

        version = __version__

        # Indent every line of the (possibly multi-line) error message by 4
        # spaces so continuation lines stay visually inside the Error block.
        msg_lines = str(error).splitlines() or [""]
        first_msg, *rest_msg = msg_lines
        message_block = "\n".join([f"    Message: {first_msg}", *(f"    {line}" for line in rest_msg)])

        header = (
            "================================================================\n"
            f"{APP_DISPLAY_NAME} Crash Report\n"
            f"Time:     {datetime.now().astimezone().isoformat(timespec='seconds')}\n"
            f"Version:  {version}\n"
            f"OS:       {pinfo.os_display}\n"
            f"Release:  {pinfo.os_version}\n"
            f"Arch:     {pinfo.arch}\n"
            f"Shell:    {pinfo.shell.path}\n"
            f"Cwd:      {chrys_platform.safe_getcwd()}\n"
            "Error:\n"
            f"    {type(error).__name__}\n"
            f"{message_block}\n"
            "================================================================\n"
        )

        # Try a rich Traceback for parity with what Textual prints to stderr
        # (frames + locals).  Fall back to stdlib if rich rendering itself
        # raises — we must always produce *some* output here.
        body: str
        try:
            import io

            import rich
            from rich.console import Console
            from rich.traceback import Traceback

            buf = io.StringIO()
            console = Console(file=buf, no_color=True, force_terminal=False, width=120)
            tb = Traceback.from_exception(
                type(error),
                error,
                error.__traceback__,
                show_locals=True,
                locals_max_length=5,
                suppress=[rich],
            )
            console.print(tb)
            body = buf.getvalue()
        except Exception:
            body = "".join(traceback.format_exception(type(error), error, error.__traceback__))

        with open(log_path, mode, encoding="utf-8") as f:
            f.write(header)
            f.write(body)
            if not body.endswith("\n"):
                f.write("\n")
            f.write("\n")


def main(app_cls: type[ChrysApp] = ChrysApp) -> None:
    """Entry point for the ``chrys`` console script.

    ``app_cls`` lets development tools swap in a ``ChrysApp`` subclass
    while reusing the rest of the bootstrap wiring.  Defaults to
    ``ChrysApp`` for the production entry point.
    """
    import logging

    from chrys.app.update_check import for_this_copy
    from chrys.foundation.config.warnings import settings_warning_events
    from chrys.orchestration.startup import bootstrap_runtime, configure_utf8_stdio

    configure_utf8_stdio()

    # Early argparse prose remains English; dynamic error details are sanitized.
    parser = SanitizingArgumentParser(
        description=f"{APP_DISPLAY_NAME} - A general-purpose extensible agent platform for rapidly building and evolving custom agents."
    )
    parser.add_argument("-s", "--session", default="", help="Restore the given session id after startup.")
    parser.add_argument(
        "-a", "--agent", default="", help="Start with the given agent profile id, name, or display name."
    )
    parser.add_argument(
        "-m",
        "--model",
        default="",
        help="Start with the given active model profile id or name.",
    )
    parser.add_argument("-C", "--workdir", default="", metavar="DIR", help="Start in the given working directory.")
    args = parser.parse_args()
    if (missing_cwd := launch_cwd_missing_message(workdir_flag=True, workdir=args.workdir)) is not None:
        parser.exit(1, f"Error: {missing_cwd}\n")
    if args.workdir:
        workdir = Path(args.workdir).expanduser()
        if not workdir.is_dir():
            parser.error(f"workdir does not exist or is not a directory: {args.workdir}")
        os.chdir(workdir)

    log_dir = chrys_platform.get_platform().config_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    debug_log = configure_debug_logging(log_dir / "debug.log")
    try:
        # Silence noisy third-party loggers
        logging.getLogger("markdown_it").setLevel(logging.WARNING)

        # Install the in-memory log handler used by the F6 Log Viewer modal
        # *before* any engine / agent / profile code runs, so logs emitted
        # during startup (profile load, engine build, OTel setup, first agent
        # construction) are captured and visible the moment the viewer is
        # opened.
        from chrys.app.tui.screens.logs import install_log_handler

        install_log_handler()

        bootstrap = bootstrap_runtime(dotenv_override=True, project_root=Path(os.getcwd()))

        bus = EventBus()
        registry = AgentProfileRegistry()
        registry.load_all()
        model_registry = ModelProfileRegistry()
        model_registry.load_all()
        loaded = _apply_startup_active_model_selection(parser, bootstrap.loaded, model_registry, args.model)
        state_store = JsonFileStateStore()
        session_title_updater = SessionTitleUpdater(bus, state_store)

        def _on_successful_turn() -> None:
            on_buddy_successful_turn()
            session_title_updater.on_turn_finished()

        engine = assemble_agent_engine(
            bus,
            loaded_settings=loaded,
            agent_registry=registry,
            state_store=state_store,
            model_registry=model_registry,
            on_successful_turn=_on_successful_turn,
            on_turn_started=session_title_updater.on_turn_started,
            surface=SessionSurface.TUI,
        )

        app = app_cls(
            bus,
            engine,
            settings_handle=engine.settings_handle,
            profile_name=args.agent,
            state_store=state_store,
            agent_registry=registry,
            model_registry=model_registry,
            startup_warnings=[*bootstrap.warnings],
            settings_warnings=settings_warning_events(loaded),
            startup_session_id=args.session,
            apply_saved_model_on_restore=not bool(args.model.strip()),
            session_title_updater=session_title_updater,
            update_check=for_this_copy(enabled=loaded.settings.update_check, served=running_under_textual_web_driver()),
        )
        try:
            # On Windows, the ProactorEventLoop shutdown can raise KeyboardInterrupt
            # from GetQueuedCompletionStatus when a pending signal races with asyncio
            # runner cleanup.  The app has already exited at this point — safe to suppress.
            with contextlib.suppress(KeyboardInterrupt):
                app.run()
        finally:
            # Safety net: ensure terminal is fully restored after native Textual exits.
            # Textual should handle this, but shell PTY activity during shutdown can
            # race with cleanup, leaving mouse tracking, focus reports, or alt screen on.
            _restore_terminal_after_run(app)
            _reset_terminal_title_after_run()
    finally:
        stop_debug_logging(debug_log)


if __name__ == "__main__":
    main()
