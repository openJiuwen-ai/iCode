# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Navigation, overlay, and exit actions for the main screen."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING

from chrys.app.tui.i18n import LocaleSwitchResult, LocaleSwitchStatus
from chrys.app.tui.language import LANGUAGE_PICKER_TITLE
from chrys.app.tui.screens.main.session_handlers import CLEAR_SESSION_TITLE
from chrys.app.tui.screens.main.state import MainScreenServices
from chrys.app.tui.screens.sessions import WorkflowSessionPick
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.config.errors import config_error_display
from chrys.foundation.i18n import msg
from chrys.foundation.util.session_ids import session_short_id
from chrys.orchestration.startup import catalog_load_warning

if TYPE_CHECKING:
    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.screens.main.ports import NavigationView, StartWorker


_CONFIRM_INTERRUPT = msg("tui.main.confirm.interrupt", fallback="Interrupt")
_CONFIRM_INTERRUPT_MESSAGE = msg(
    "tui.main.confirm.interrupt_message",
    fallback="Interrupt the current run?",
)
_CONFIRM_EXIT = msg("tui.main.confirm.exit", fallback="Exit")
_CONFIRM_EXIT_MESSAGE = msg("tui.main.confirm.exit_message", fallback="Exit {app_name}?")
_CONFIRM_CLEAR = msg("tui.main.confirm.clear", fallback="Delete")
_CONFIRM_CLEAR_MESSAGE = msg(
    "tui.main.confirm.clear_message",
    fallback=(
        "Delete the current session\n"
        '"{session_id}" and start a new one?\n\n'
        "The session will be permanently deleted and cannot be recovered."
    ),
    multiline=True,
)
_CLEAR_NO_ACTIVE_SESSION = msg(
    "tui.main.clear.no_active_session",
    fallback="No active session to clear",
)
_CLEAR_EMPTY_SESSION = msg(
    "tui.main.clear.empty_session",
    fallback="Nothing to clear: the current session is empty",
)


class MainNavigationController:
    """Coordinate non-chat navigation and confirmation flows."""

    def __init__(
        self,
        *,
        services: MainScreenServices,
        view: NavigationView,
        is_agent_loading: Callable[[], bool],
        is_agent_running: Callable[[], bool],
        is_submit_pending: Callable[[], bool],
        is_workflow_mode: Callable[[], bool],
        has_messages: Callable[[], bool],
        is_dashboard_visible: Callable[[], bool],
        set_interrupt_confirm_active: Callable[[bool], None],
        publish_interrupt: Callable[[], object],
        dismiss_suggestions: Callable[[], bool],
        cancel_pending_injection: Callable[[], bool],
        delete_current_and_new: Callable[[str], Awaitable[None]],
        restore_session: Callable[[str | WorkflowSessionPick], Awaitable[None]],
        flush_notifications: Callable[[], Awaitable[None]],
        start_worker: StartWorker,
        debug: Callable[[str, str], None],
        locale_controller: LocaleController | None = None,
        background_tasks_running: Callable[[], bool] = lambda: False,
        confirm_stopping_background_tasks: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self._services = services
        self._view = view
        self._is_agent_loading = is_agent_loading
        self._is_agent_running = is_agent_running
        self._is_submit_pending = is_submit_pending
        self._is_workflow_mode = is_workflow_mode
        self._has_messages = has_messages
        self._is_dashboard_visible = is_dashboard_visible
        self._set_interrupt_confirm_active = set_interrupt_confirm_active
        self._publish_interrupt = publish_interrupt
        self._dismiss_suggestions = dismiss_suggestions
        self._cancel_pending_injection = cancel_pending_injection
        self._delete_current_and_new = delete_current_and_new
        self._restore_session = restore_session
        self._flush_notifications = flush_notifications
        self._start_worker = start_worker
        self._debug = debug
        self._locale_controller = locale_controller
        self._background_tasks_running = background_tasks_running
        self._confirm_stopping_background_tasks = confirm_stopping_background_tasks
        self._asking_to_quit = False

    def show_log_viewer(self) -> None:
        """Open the log viewer modal."""
        if self._is_agent_loading():
            return
        self._view.open_log_viewer()

    def open_guide(self) -> None:
        """Open the user guide modal from the help command."""
        self._view.open_guide(self._locale_controller)

    def quit(self) -> None:
        """Stop the shell panel, flush notification settings, and exit.

        While background commands run, it first asks whether they may stop; quitting
        again while it asks quits at once.
        """
        confirm = self._confirm_stopping_background_tasks
        if confirm is not None and not self._asking_to_quit and self._background_tasks_running():

            async def quit_if_confirmed() -> None:
                self._asking_to_quit = True
                try:
                    confirmed = await confirm()
                finally:
                    self._asking_to_quit = False
                if confirmed:
                    self._quit_now()

            self._start_worker(quit_if_confirmed)
            return
        self._quit_now()

    def _quit_now(self) -> None:
        self._view.stop_shell()
        try:
            self._start_worker(self._quit_after_notification_flush)
        except RuntimeError:
            self._view.exit_app()

    async def _quit_after_notification_flush(self) -> None:
        try:
            await self._flush_notifications()
        finally:
            self._view.exit_app()

    def sessions(self) -> None:
        """Open the sessions modal."""
        if not self._services.state_store or self._is_agent_running() or self._is_agent_loading():
            return
        self._view.open_sessions_screen(
            self._services.state_store,
            current_session_id=self._view.current_session_id(),
            on_result=self.handle_session_selected,
        )

    def handle_session_selected(self, session_id: str | WorkflowSessionPick | None) -> None:
        """Handle a selected session from the sessions modal."""
        from chrys.app.tui.screens.sessions import SessionsScreen

        if isinstance(session_id, str) and session_id.startswith(SessionsScreen.DELETE_AND_NEW_PREFIX):
            deleted_id = session_id[len(SessionsScreen.DELETE_AND_NEW_PREFIX) :]
            self._start_worker(partial(self._delete_current_and_new, deleted_id))
        elif session_id is not None:
            self._start_worker(partial(self._restore_session, session_id))

    def toggle_sidebar(self) -> None:
        """Toggle the sidebar unless agent loading is active."""
        if not self._is_agent_loading():
            self._view.toggle_sidebar()

    def update_toc(self) -> None:
        """Refresh TOC rows from the chat transcript."""
        self._view.update_toc()

    def pick_theme(self) -> None:
        """Open the theme picker modal."""
        if self._is_agent_loading():
            return

        def _on_theme_result(result: str | None) -> None:
            if result and self._view.apply_theme_setting(result):
                self._debug("ThemeChanged", result)

        self._view.open_theme_picker(_on_theme_result)

    def pick_language(self) -> None:
        """Open the no-preview language picker modal."""
        controller = self._locale_controller
        if self._is_agent_loading() or controller is None:
            return

        def _on_language_result(result: str | None) -> None:
            if result is not None:
                self.set_language(result)

        self._view.open_language_picker(controller.requested_locale, _on_language_result)

    def set_language(self, requested_locale: str) -> None:
        """Apply one picker or slash-command confirmation through the controller."""
        controller = self._locale_controller
        if controller is None:
            return
        self._report_locale_result(controller.switch_locale(requested_locale))

    def follow_language(self) -> None:
        """Put the locale a reload left in the settings on screen; only a failed bundle has anything to say."""
        controller = self._locale_controller
        if controller is not None:
            self._report_locale_result(controller.follow_settings())

    def _report_locale_result(self, result: LocaleSwitchResult) -> None:
        if result.persist_error is not None:
            self._view.notify(
                config_error_display(result.persist_error), title=LANGUAGE_PICKER_TITLE.bind(), severity="error"
            )
        if result.status is not LocaleSwitchStatus.LOAD_FAILED or result.warning is None:
            return
        notice = catalog_load_warning(result.warning)
        if notice.display_message is None:
            return
        self._view.notify(
            notice.display_message,
            title=LANGUAGE_PICKER_TITLE.bind(),
            severity="warning",
        )

    def can_toggle_trajectory_dashboard(self) -> bool:
        """Keep the agent dashboard in Chat mode, after execution has fully settled."""
        return not self._is_workflow_mode() and (
            self._is_dashboard_visible()
            or not (self._is_agent_loading() or self._is_agent_running() or self._is_submit_pending())
        )

    def toggle_trajectory_dashboard(self) -> None:
        """Toggle the in-place trajectory dashboard."""
        if not self.can_toggle_trajectory_dashboard():
            return
        if self._is_dashboard_visible():
            self._view.hide_trajectory_dashboard()
            return
        self._dismiss_suggestions()
        self._view.show_trajectory_dashboard()

    def escape(self, *, on_idle: Callable[[], None] | None = None) -> None:
        """Dismiss overlays, cancel a queued injection, confirm interrupt, or confirm exit."""
        if self._dismiss_suggestions():
            # The /, @, # suggestion popup is the most local overlay: Esc
            # collapses it (the typed input is kept) before any interrupt
            # or exit prompting, even while a run is active.
            return
        if self._is_dashboard_visible():
            self._view.hide_trajectory_dashboard()
        elif self._is_agent_loading():
            return
        elif self._cancel_pending_injection():
            # A queued mid-run injection was withdrawn and returned to the
            # input bar; the run itself keeps going, so no interrupt prompt.
            return
        elif self._is_agent_running():
            self.confirm_interrupt()
        else:
            (on_idle or self.confirm_exit)()

    def confirm_interrupt(self) -> None:
        """Ask before interrupting the current run."""
        if self._services.execution().kind == "workflow":
            self._publish_interrupt()  # The workflow controller owns its cancellation warning.
            return
        self._set_interrupt_confirm_active(True)
        self._view.open_confirm_dialog(
            title=_CONFIRM_INTERRUPT.bind(),
            message=_CONFIRM_INTERRUPT_MESSAGE.bind(),
            confirm_label=_CONFIRM_INTERRUPT.bind(),
            confirm_variant="error",
            on_result=self.on_interrupt_confirmed,
        )

    def on_interrupt_confirmed(self, confirmed: bool) -> None:
        """Handle interrupt confirmation result."""
        self._set_interrupt_confirm_active(False)
        if confirmed and self._is_agent_running():
            self._publish_interrupt()

    def dismiss_interrupt_confirm(self) -> None:
        """Dismiss the interrupt confirmation after the run stops."""
        self._set_interrupt_confirm_active(False)
        self._view.dismiss_interrupt_confirm()

    def confirm_exit(self) -> None:
        """Ask before exiting the app."""
        if self._background_tasks_running():
            self.quit()  # Its question names the background commands that stop.
            return
        self._view.open_confirm_dialog(
            title=_CONFIRM_EXIT.bind(),
            message=_CONFIRM_EXIT_MESSAGE.bind(app_name=APP_DISPLAY_NAME),
            confirm_label=_CONFIRM_EXIT.bind(),
            confirm_variant="error",
            on_result=self.on_exit_confirmed,
        )

    def on_exit_confirmed(self, confirmed: bool) -> None:
        """Handle exit confirmation result."""
        if confirmed:
            self.quit()

    def clear_session(self) -> None:
        """Ask before deleting the current session and starting a fresh one.

        ``/new`` keeps the current session on disk; ``/clear`` removes it, so
        the deletion is gated behind an explicit confirmation.
        """
        if self._is_clear_blocked():
            return
        session_id = self._view.current_session_id()
        if not session_id:
            # An empty id would address the sessions root, never a session.
            self._view.notify(
                _CLEAR_NO_ACTIVE_SESSION.bind(),
                title=CLEAR_SESSION_TITLE.bind(),
                severity="warning",
            )
            return
        if not self._has_messages() and not self._session_dir_exists(session_id):
            # Nothing in the transcript and nothing on disk: a "cannot be
            # recovered" prompt would be noise (mirrors /fork).  The disk check
            # matters after a rollback-to-welcome, which empties the chat but
            # may keep committed sub-agent artifacts that /clear should remove.
            self._view.notify(
                _CLEAR_EMPTY_SESSION.bind(),
                title=CLEAR_SESSION_TITLE.bind(),
                severity="warning",
            )
            return
        self._view.open_confirm_dialog(
            title=CLEAR_SESSION_TITLE.bind(),
            message=_CONFIRM_CLEAR_MESSAGE.bind(session_id=session_short_id(session_id)),
            confirm_label=_CONFIRM_CLEAR.bind(),
            confirm_variant="error",
            on_result=lambda confirmed: self.on_clear_confirmed(confirmed, session_id),
        )

    def _session_dir_exists(self, session_id: str) -> bool:
        """Return whether *session_id* (non-empty) has a directory in the store."""
        store = self._services.state_store
        if store is None:
            return False
        try:
            return store.session_dir(session_id).exists()
        except OSError:
            return False

    def _is_clear_blocked(self) -> bool:
        """Return whether the current turn state forbids clearing the session.

        A pending submit counts as busy: ``agent_running`` flips only after the
        backend finished admitting the prompt, and clearing in that gap would
        delete the session the prompt is being prepared against.
        """
        return (
            self._services.execution_busy()
            or self._is_agent_running()
            or self._is_agent_loading()
            or self._is_submit_pending()
        )

    def on_clear_confirmed(self, confirmed: bool, session_id: str) -> None:
        """Delete *session_id* and start a new session once the user confirmed."""
        if not confirmed:
            return
        if self._is_clear_blocked():
            return
        # Only delete the session the dialog named; bail if the screen moved on.
        if not session_id or self._view.current_session_id() != session_id:
            return
        self._start_worker(partial(self._delete_current_and_new, session_id))

    def scroll_to_turn(self, turn_id: str) -> None:
        """Route a TOC turn to the surface currently in the foreground."""
        if self._is_dashboard_visible():
            self._view.select_trajectory_turn(turn_id)
        else:
            self._view.scroll_chat_to_turn(turn_id)
