# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The session title on the chat border and the terminal tab, and its editing."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Literal

from chrys.app.tui.terminal.title import (
    set_app_terminal_title_for_cwd,
    set_app_terminal_title_for_session_title,
    set_app_terminal_title_for_user_message,
)

if TYPE_CHECKING:
    from pathlib import Path

    from textual.timer import Timer

    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.screens.main.state import RunState

_ACTIVITY_INTERVAL_SECONDS = 0.65
_RUNNING_FRAMES = ("◇", "◈", "◆", "◈")
_COMPLETED_MARK = "✓"
_FAILED_MARK = "✗"

type _TerminalTitleSource = Literal["cwd", "session", "user_message"]


class SessionTitleController:
    """Own the session title and project it onto the chat border and the terminal tab.

    Three titles feed the display title on the border: ``custom`` is user-set
    and wins; ``generated`` is the latest post-turn LLM summary; ``fallback``
    mirrors the persisted first-user-message title so the border has something
    before a summary lands. The terminal tab shows a submitted prompt's preview
    until the titles or the cwd change, which bring back the display title (or
    the cwd when there is none); a custom title pins the tab, so prompt
    previews never replace it. A spinner prefixes the tab while the agent runs,
    and the last turn's result mark after it ends.
    The session handler persists titles; this controller only displays them.
    """

    def __init__(
        self,
        *,
        run: RunState,
        app: Callable[[], object],
        workspace_cwd: Callable[[], str],
        show_display_title: Callable[[str], None],
        set_interval: Callable[[float, Callable[[], None]], Timer],
        current_session_id: Callable[[], str],
        push_screen: Callable[[object, Callable[[str | None], None]], object],
        start_custom_title_save: Callable[[str, str], object],
        session_dir: Callable[[str], Path | None],
        locale_controller: LocaleController | None = None,
    ) -> None:
        self._run = run
        self._app = app
        self._workspace_cwd = workspace_cwd
        self._show_display_title = show_display_title
        self._set_interval = set_interval
        self._current_session_id = current_session_id
        self._push_screen = push_screen
        self._start_custom_title_save = start_custom_title_save
        self._session_dir = session_dir
        self._locale_controller = locale_controller
        self._custom = ""
        self._generated = ""
        self._fallback = ""
        self._terminal_source: _TerminalTitleSource = "cwd"
        self._terminal_content = ""
        self._result_mark = ""
        self._activity_frame = 0
        self._activity_timer: Timer | None = None

    # ------------------------------------------------------------------ #
    # Session title
    # ------------------------------------------------------------------ #

    @property
    def custom_title(self) -> str:
        return self._custom

    @property
    def display_title(self) -> str:
        """User-facing session title: custom wins, then generated, then fallback."""
        return self._custom or self._generated or self._fallback

    def set_session_title_state(
        self,
        *,
        custom: str | None = None,
        generated: str | None = None,
        fallback: str | None = None,
    ) -> None:
        """Update the titles (``None`` leaves a field unchanged) and refresh."""
        if custom is not None:
            self._custom = custom
        if generated is not None:
            self._generated = generated
        if fallback is not None:
            self._fallback = fallback
        self._refresh_display_title()

    def reset_session_title_state(self) -> None:
        """Clear every title and the result mark (new or blank session)."""
        self._result_mark = ""
        self._custom = ""
        self._generated = ""
        self._fallback = ""
        self._refresh_display_title()

    def _refresh_display_title(self) -> None:
        self._show_display_title(self.display_title)
        self.set_terminal_title_for_cwd()

    def open_editor(self) -> None:
        """Push the custom-title dialog for the current session (border click or /rename)."""
        session_id = self._current_session_id()
        if not session_id:
            return
        from chrys.app.tui.screens.dialogs.session_title import SessionTitleDialog

        dialog = SessionTitleDialog(
            session_id=session_id,
            session_folder=self._session_dir(session_id),
            custom_title=self._custom,
            auto_title=self._generated or self._fallback,
            locale_controller=self._locale_controller,
        )

        def on_result(result: str | None) -> None:
            # Pin the edit to the session the dialog was opened for — the
            # UI may have restored another session while it was open.
            if result is not None:
                self._start_custom_title_save(result, session_id)

        self._push_screen(dialog, on_result)

    def apply_custom_title(self, custom_title: str) -> None:
        """Apply a non-empty ``/rename <title>`` argument without the dialog."""
        session_id = self._current_session_id()
        if session_id:
            self._start_custom_title_save(custom_title, session_id)

    # ------------------------------------------------------------------ #
    # Terminal tab
    # ------------------------------------------------------------------ #

    def set_terminal_title_for_cwd(self, cwd: str | None = None) -> None:
        display = self.display_title
        if display:
            # A session title pins the terminal tab; cwd changes (workspace
            # updates, restores) must not unpin it. The tab falls back to
            # the cwd once the title state clears.
            self._terminal_source = "session"
            self._terminal_content = display
        else:
            self._terminal_source = "cwd"
            self._terminal_content = self._workspace_cwd() if cwd is None else cwd
        self._render_terminal_title()

    def set_terminal_title_for_user_message(self, text: str) -> None:
        self._result_mark = ""
        if not self._fallback and text.strip():
            # Mirror the persisted first-user-message title so the border
            # shows a title before the first auto-summary lands.
            self._fallback = " ".join(text.split())
            self._refresh_display_title()
        if self._custom:
            # A custom title pins the terminal tab; prompt previews must
            # not replace it.
            self._terminal_source = "session"
            self._terminal_content = self._custom
        else:
            self._terminal_source = "user_message"
            self._terminal_content = text
        self._render_terminal_title()

    def mark_terminal_title_completed(self) -> None:
        self._result_mark = _COMPLETED_MARK
        if not self._run.agent_running:
            self._render_terminal_title()

    def mark_terminal_title_failed(self) -> None:
        self._result_mark = _FAILED_MARK
        if not self._run.agent_running:
            self._render_terminal_title()

    def clear_terminal_title_result(self) -> None:
        if not self._result_mark:
            return
        self._result_mark = ""
        self._render_terminal_title()

    def run_started(self) -> None:
        """A new turn drops the last result mark and restarts the spinner."""
        self._result_mark = ""
        self._activity_frame = 0

    def sync_activity(self) -> None:
        """Run the spinner exactly while the agent runs, then repaint the tab."""
        if self._run.agent_running:
            if self._activity_timer is None:
                self._activity_timer = self._set_interval(_ACTIVITY_INTERVAL_SECONDS, self._advance_activity)
        else:
            self.stop_activity()
        self._render_terminal_title()

    def stop_activity(self) -> None:
        """Stop the spinner's timer; the titles and the result mark stay."""
        if self._activity_timer is not None:
            self._activity_timer.stop()
            self._activity_timer = None

    def _advance_activity(self) -> None:
        if not self._run.agent_running or self._activity_timer is None:
            return
        self._activity_frame = (self._activity_frame + 1) % len(_RUNNING_FRAMES)
        self._render_terminal_title()

    @property
    def _indicator(self) -> str:
        if self._run.agent_running:
            return _RUNNING_FRAMES[self._activity_frame]
        return self._result_mark

    def _with_indicator(self, title: str) -> str:
        indicator = self._indicator
        if not indicator:
            return title
        return f"{indicator} {title}" if title else indicator

    def _render_terminal_title(self) -> None:
        app = self._app()
        content = self._terminal_content
        if self._terminal_source == "cwd":
            set_app_terminal_title_for_cwd(app, content or self._workspace_cwd(), indicator=self._indicator)
        elif self._terminal_source == "session":
            set_app_terminal_title_for_session_title(app, self._with_indicator(content))
        else:
            set_app_terminal_title_for_user_message(app, self._with_indicator(content))
