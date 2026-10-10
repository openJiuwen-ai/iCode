# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Textual view adapter for the main screen."""

from __future__ import annotations

import contextlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypedDict, cast

from textual.css.query import NoMatches
from textual.widgets import Footer

from chrys.app.tui.buddy_messages import BUDDY_TITLE
from chrys.app.tui.clipboard import copy_text_to_clipboards
from chrys.app.tui.notifications import NotificationEvent
from chrys.app.tui.screens.main.dialog_controllers import (
    AgentLoadDialogHandle,
    AnswerQuestionDialogResult,
    ApprovalBypassDecision,
    ApprovalDialogHandle,
    ApprovalTimer,
    ImageCompressionDialogHandle,
    InlineQuestionDialogResult,
    QuestionDialogHandle,
    QuestionDialogResult,
)
from chrys.app.tui.screens.main.ports import (
    ConfirmMessage,
    InputLabel,
    InputRestore,
    NotificationSeverity,
    StatusMessage,
    StatusTrail,
    SuggestionEntry,
)
from chrys.app.tui.terminal.panel import ShellPanel
from chrys.app.tui.terminal.widget import Terminal
from chrys.app.tui.widgets import PromptDraft
from chrys.app.tui.widgets.chat.file_snapshot import FileSnapshotPayload
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.session_json import SessionJsonPanel
from chrys.app.tui.widgets.chrome.app_header import AppHeader
from chrys.app.tui.widgets.chrome.image_paste import clipboard_image_dir_for_session
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.chrome.status_bar import (
    STATUS_AGENT_LOAD_FAILED,
    STATUS_INTERACTIVE_MODE,
    STATUS_INTERRUPTED,
    STATUS_RETRYING,
    STATUS_RUNNING_TOOL,
    STATUS_SHELL_MODE,
    StatusBar,
)
from chrys.app.tui.widgets.sidebar.context import ContextUsageState
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
from chrys.app.tui.widgets.sidebar.tasks import TodoListState
from chrys.app.tui.widgets.sidebar.toc import ConversationToc
from chrys.app.tui.widgets.trajectory import TrajectoryDashboard
from chrys.foundation.events.types import (
    AgentRuntimeDetails,
    ApprovalRequest,
    ApprovalReviewed,
    QuestionToUser,
)
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.ask_user import AskUserAnswer
from chrys.service.approval.policy import ApprovalMode
from chrys.service.profiles.models.schema import DEFAULT_MAX_CONTEXT_TOKENS
from chrys.service.trajectory.session import trajectory_events_path

if TYPE_CHECKING:
    from textual.worker import Worker

    from chrys.app.tui.app import ChrysApp
    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.screens.diff import RollbackProgressModal
    from chrys.app.tui.screens.diff.rollback_modal import RollbackModalState
    from chrys.app.tui.screens.main.engine_read_model import RollbackReadState
    from chrys.app.tui.screens.main.screen import MainScreen
    from chrys.app.tui.screens.main.state import MainScreenState
    from chrys.app.tui.util.diff_entries import DiffFileEntry, DiffLoadResult
    from chrys.app.tui.widgets.chrome.commands import ManPageSpec
    from chrys.foundation.events.types import ProvisionalPresentation
    from chrys.foundation.models.session_surface import SessionSurface
    from chrys.foundation.models.todos import TodoItem
    from chrys.service.approval.judge import JudgeVerdict
    from chrys.service.context.providers.history import CompressedBlock
    from chrys.service.state.store import StateStore

logger = logging.getLogger(__name__)


def _push_screen_untyped(app: Any, screen: object, callback: object | None = None) -> object:
    """Push adapter-typed screens or callbacks that Textual's push_screen overloads cannot model."""
    return app.push_screen(screen) if callback is None else app.push_screen(screen, callback)


def _judge_verdict(event: ApprovalReviewed) -> JudgeVerdict:
    from chrys.service.approval.judge import JudgeVerdict

    return JudgeVerdict(approved=event.approved, reason=event.reason)


class _StatusFlashKwargs(TypedDict, total=False):
    """Keyword subset forwarded only when requested by the presenter."""

    error: bool
    caution: bool
    trail: StatusTrail


class MainScreenViewAdapter:
    """Own Textual widget lookup and screen side effects for main-screen controllers."""

    def __init__(
        self,
        screen: MainScreen,
        *,
        state: MainScreenState,
        state_store: StateStore | None = None,
        locale_controller: LocaleController | None = None,
    ) -> None:
        self._screen = screen
        self._state = state
        self._state_store = state_store
        self._locale_controller = locale_controller
        self._rollback_progress_modal: RollbackProgressModal | None = None
        self._input_restore_generation = 0
        # The sessions browser's surface filter per mode (workflow mode or not), kept while the app runs.
        self._session_surfaces: dict[bool, frozenset[SessionSurface]] = {}
        # The status bar as shell mode found it, restored when shell mode exits.
        self._shell_status_snapshot: dict = {}

    # -------------------------------------------------------------- #
    # Generic screen effects
    # -------------------------------------------------------------- #

    def debug(self, key: str, message: str = "") -> None:
        self._screen._debug(key, message)

    def notify(
        self,
        message: StatusMessage,
        *,
        title: StatusMessage,
        severity: NotificationSeverity = "information",
        timeout: float | None = 3,
    ) -> None:
        self._screen.notify(
            self._render_status_message(message),
            title=self._render_status_message(title),
            severity=severity,
            timeout=timeout,
            markup=False,
        )

    def notify_event(self, event: NotificationEvent) -> None:
        try:
            cast("ChrysApp", self._screen.app).notification_service.notify(event)
        except Exception:
            logger.debug("Notification dispatch failed", exc_info=True)

    def notify_buddy(
        self,
        message: StatusMessage,
        *,
        severity: NotificationSeverity = "information",
        timeout: float = 10,
    ) -> None:
        self._screen.notify(
            self._render_status_message(message),
            title=self._render_status_message(BUDDY_TITLE.bind()),
            severity=severity,
            timeout=timeout,
            markup=False,
        )

    def _render_status_message(self, message: StatusMessage) -> str:
        if isinstance(message, str):
            return message
        controller = self._locale_controller
        if controller is None:
            return format_message(message)
        from chrys.app.tui.i18n import render_str

        return render_str(controller.localizer, message)

    def refresh_buddy_panel(self, *, focus_tab: bool) -> None:
        try:
            sidebar = self._screen.query_one(SidebarPanel)
        except NoMatches:  # the screen is going away, or never had a sidebar
            return
        if focus_tab:
            sidebar.focus_tab("tab-buddy")
        sidebar.buddy_panel.reload()

    def push_screen(self, screen: object, callback: object | None = None) -> object:
        return _push_screen_untyped(self._screen.app, screen, callback)

    def open_guide(self, locale_controller: LocaleController | None) -> None:
        from chrys.app.tui.screens.guides import GuideDialog

        self._screen.app.push_screen(GuideDialog(locale_controller=locale_controller))

    def show_man_pages(self, pages: list[ManPageSpec], *, start_index: int = 0) -> None:
        from chrys.app.tui.screens.dialogs.man_page import ManPageDialog

        self._screen.app.push_screen(
            ManPageDialog(
                pages,
                start_index=start_index,
                locale_controller=self._locale_controller,
            )
        )

    def open_runtime_details(self, details: AgentRuntimeDetails) -> None:
        from chrys.app.tui.screens.dialogs.runtime_details import RuntimeDetailsDialog

        self._screen.app.push_screen(RuntimeDetailsDialog(details))

    def run_worker(self, awaitable: Awaitable[Any], *, group: str) -> None:
        self._screen.run_worker(awaitable, exclusive=False, group=group)

    def set_timer(self, delay: float, callback: Callable[[], None]) -> ApprovalTimer:
        return self._screen.set_timer(delay, callback)

    def call_after_refresh(self, callback: Callable[[], None]) -> None:
        self._screen.call_after_refresh(callback)

    def stop_shell(self) -> None:
        self._screen.query_one(ShellPanel).stop()

    def exit_app(self) -> None:
        self._screen.app.exit()

    # -------------------------------------------------------------- #
    # Input flow
    # -------------------------------------------------------------- #

    def set_terminal_title_for_user_message(self, text: str) -> None:
        self._screen._session_title.set_terminal_title_for_user_message(text)

    def clear_terminal_title_result(self) -> None:
        self._screen._session_title.clear_terminal_title_result()

    def set_terminal_title_for_cwd(self, cwd: str | None = None) -> None:
        self._screen._session_title.set_terminal_title_for_cwd(cwd)

    def current_chat_session_id(self) -> str:
        return self._screen.query_one(ChatPanel).session_id

    def current_session_id(self) -> str:
        workflow = self._screen._workflow
        return workflow.session_id if workflow.workflow_mode else self.current_chat_session_id()

    def chat_session_id(self) -> str:
        return self.current_chat_session_id()

    def add_input_history(self, text: str, *, session_id: str | None) -> None:
        self._screen.query_one(InputBar).add_to_history(text, session_id=session_id)

    def set_retry_mode(self, enabled: bool, *, label: InputLabel = "Retry") -> None:
        input_bar = self._screen.query_one(InputBar)
        input_bar._retry_label = label
        # retry_mode is always_update: assignment refreshes even when the
        # flag is unchanged and only the label above differs.
        input_bar.retry_mode = enabled

    def set_input_retry_mode(self, value: bool) -> None:
        self._screen.query_one(InputBar).retry_mode = value

    def set_input_retry(self, label: InputLabel, value: bool = True) -> None:
        self.set_retry_mode(value, label=label)

    def start_run_status(self, label: StatusMessage) -> None:
        status = self._screen.query_one(StatusBar)
        status.start_run()
        status.show(label)

    async def render_user_message(self, text: str, *, created_at: datetime, contents: list[Any] | None) -> None:
        await self._screen.query_one(ChatPanel).add_user_message(text, created_at=created_at, contents=contents)

    async def render_user_retry_note(self, text: str, *, created_at: datetime) -> None:
        await self._screen.query_one(ChatPanel).add_user_message(text, is_injection=True, created_at=created_at)

    def hide_trailing_status_action(self) -> None:
        self._screen.query_one(ChatPanel).hide_trailing_status_action()

    def set_retry_pending(self, pending: bool) -> None:
        self._screen.query_one(InputBar).retry_pending = pending
        self._screen.query_one(ChatPanel).set_trailing_status_action_disabled(pending)

    def clear_ask_user_inline_prompts(self) -> None:
        self._screen.query_one(ChatPanel).clear_ask_user_inline_prompts()

    def lock_input_with_text(self) -> None:
        self._screen.query_one(InputBar).lock_with_text()

    def unlock_input_keep_if_locked(self) -> None:
        input_bar = self._screen.query_one(InputBar)
        if input_bar.locked:
            input_bar.unlock_and_keep()

    def flash_interrupted(self) -> None:
        self._screen.query_one(StatusBar).flash(STATUS_INTERRUPTED.bind(), caution=True)

    async def render_interrupted(self) -> None:
        await self._screen.query_one(ChatPanel).add_interrupted()

    def update_toc(self) -> None:
        with contextlib.suppress(Exception):
            chat = self._screen.query_one(ChatPanel)
            toc = self._screen.query_one(ConversationToc)
            toc.update_items(chat.toc_items)

    # -------------------------------------------------------------- #
    # Status/header/chat presentation
    # -------------------------------------------------------------- #

    def set_tool_info(self, trail: StatusTrail) -> None:
        self._screen.query_one(StatusBar).set_tool_info(trail)

    def show_status(self, text: StatusMessage) -> None:
        self._screen.query_one(StatusBar).show(text)

    def clear_status(self) -> None:
        self._screen.query_one(StatusBar).clear_status()

    def flash_status(
        self,
        text: StatusMessage,
        *,
        error: bool = False,
        caution: bool = False,
        trail: StatusTrail | None = None,
    ) -> None:
        kwargs = _StatusFlashKwargs()
        if error:
            kwargs["error"] = True
        if caution:
            kwargs["caution"] = True
        if trail is not None:
            kwargs["trail"] = trail
        self._screen.query_one(StatusBar).flash(text, **kwargs)

    def flash_turn_complete(self) -> None:
        self._screen.query_one(StatusBar).flash_completed()

    def mark_terminal_title_completed(self) -> None:
        self._screen._session_title.mark_terminal_title_completed()

    def mark_terminal_title_failed(self) -> None:
        self._screen._session_title.mark_terminal_title_failed()

    def start_tool_status(self, tool_name: str) -> None:
        status = self._screen.query_one(StatusBar)
        status.add_tool_call()
        status.show(STATUS_RUNNING_TOOL.bind(tool_name=tool_name))

    def set_chat_profile(self, profile: str) -> None:
        self._screen.chat_profile_name = profile
        self._screen.query_one(ChatPanel).set_profile(profile)

    def set_chat_tool_kinds(self, tool_kinds: dict[str, str]) -> None:
        self._screen.query_one(ChatPanel).set_tool_kinds(tool_kinds)

    async def clear_chat(self) -> None:
        await self._screen.query_one(ChatPanel).clear()

    def set_chat_workspace_cwd(self, cwd: str) -> None:
        self._screen.chat_workspace_cwd = cwd
        self._screen.query_one(ChatPanel).set_workspace_cwd(cwd)

    def update_welcome(self, *, profile: str = "", cwd: str = "") -> None:
        self._screen.query_one(ChatPanel).update_welcome(profile=profile, cwd=cwd)

    def set_chat_session_id(self, session_id: str) -> None:
        dashboard = self._screen.query_one(TrajectoryDashboard)
        if dashboard.foreground and session_id != self._screen.chat_session_id:
            self.hide_trajectory_dashboard()
        self._screen.chat_session_id = session_id
        self._screen.query_one(ChatPanel).set_session_id(session_id)

    def set_session_title_state(
        self,
        *,
        custom: str | None = None,
        generated: str | None = None,
        fallback: str | None = None,
    ) -> None:
        self._screen._session_title.set_session_title_state(custom=custom, generated=generated, fallback=fallback)

    def reset_session_title_state(self) -> None:
        self._screen._session_title.reset_session_title_state()

    def session_custom_title(self) -> str:
        return self._screen._session_title.custom_title

    def set_context_usage_state(self, state: ContextUsageState | None) -> None:
        self._screen.context_usage_state = state

    @property
    def context_usage_state(self) -> ContextUsageState | None:
        return self._screen.context_usage_state

    def set_todo_state(self, items: tuple[TodoItem, ...]) -> None:
        self._screen.todo_state = TodoListState(items=tuple(items))

    def clear_todos(self) -> None:
        self._screen.todo_state = TodoListState()

    def set_header_approval_mode(self, mode: ApprovalMode) -> None:
        self._screen.header_approval_mode = mode

    def set_status_profile(self, profile: str, *, description: str = "") -> None:
        self._screen.query_one(StatusBar).set_profile(profile, description=description)

    def set_input_clipboard_dir(self, session_id: str | None) -> None:
        self._screen.query_one(InputBar).set_clipboard_image_dir(
            clipboard_image_dir_for_session(self._state_store, session_id)
        )

    def set_input_paste_cwd(self, cwd: str) -> None:
        self._screen.query_one(InputBar).set_paste_cwd(cwd)

    def restore_input_text(self, text: str) -> InputRestore | None:
        """Hand a consumed prompt back to an empty composer.

        Returns proof of that restore for a caller that has to rewrite the same
        text afterwards, or ``None`` when nothing was written — the composer
        already held a draft, which is the user's and never ours to replace.

        Every attempt consumes a generation, declined ones included, so an
        outstanding credential dies the moment anything else tries to restore.
        """
        self._input_restore_generation += 1
        input_bar = self._screen.query_one(InputBar)
        if input_bar.locked:
            input_bar.unlock_and_keep()
        if not input_bar.restore_draft(text):
            return None
        return InputRestore(generation=self._input_restore_generation, text=text)

    def rewrite_restored_input(self, restore: InputRestore | None, text: str) -> bool:
        """Rewrite the text *restore* put there, while that restore still stands.

        Authority comes from the credential, never from what the composer reads:
        it buys nothing once the restore never happened, or any later restore —
        successful or declined — superseded it. Byte identity is then required
        on top, so an edit made under the credential's own restore hands the
        draft back to the user.

        The credential is spent either way, so it can never be replayed.
        """
        if restore is None or restore.generation != self._input_restore_generation:
            return False
        self._input_restore_generation += 1
        input_bar = self._screen.query_one(InputBar)
        if input_bar.value != restore.text:
            return False
        # Rewriting identical text would still reset the cursor and break an
        # in-progress IME composition, so leave the composer alone.
        if text != restore.text:
            input_bar.value = text
        return True

    async def change_shell_directory(self, cwd: str) -> None:
        await self._screen.query_one(ShellPanel).change_directory(cwd)

    def reset_context_usage(self, max_context_tokens: int = 0) -> None:
        self._screen.query_one(SidebarPanel).context_panel.reset(max_context_tokens)

    def clear_compressed_blocks(self) -> None:
        self._screen.query_one(SidebarPanel).context_panel.clear_blocks()

    def restore_session_chrome(self, *, session_id: str, profile: str, cwd: str) -> None:
        self.set_chat_session_id(session_id)
        self.set_chat_profile(profile)
        self.set_chat_workspace_cwd(cwd)
        self.update_welcome(profile=profile, cwd=cwd)

    # -------------------------------------------------------------- #
    # Copy and fold actions
    # -------------------------------------------------------------- #

    def chat_copy_messages(self, target: str) -> list[tuple[str, str]]:
        panel = self._screen.query_one(ChatPanel)
        if target == "all":
            return list(panel.get_all_messages())
        if target == "user":
            return list(panel.get_user_messages())
        return list(panel.get_agent_responses())

    def copy_text(self, text: str) -> None:
        copy_text_to_clipboards(self._screen.app, text)

    def toggle_chat_fold_all(self) -> bool:
        return self._screen.query_one(ChatPanel).toggle_fold_all()

    # -------------------------------------------------------------- #
    # Navigation and overlays
    # -------------------------------------------------------------- #

    def open_log_viewer(self) -> None:
        from chrys.app.tui.screens.logs.screen import LogViewerScreen

        self._screen.app.push_screen(LogViewerScreen())

    def open_sessions_screen(self, state_store: StateStore, *, current_session_id: str, on_result: object) -> None:
        from chrys.app.tui.screens.sessions import SessionsScreen

        workflow_mode = self._screen._workflow.workflow_mode

        def remember(surfaces: frozenset[SessionSurface]) -> None:
            self._session_surfaces[workflow_mode] = surfaces

        _push_screen_untyped(
            self._screen.app,
            SessionsScreen(
                state_store,
                current_session_id=current_session_id,
                locale_controller=self._locale_controller,
                workflow_mode=workflow_mode,
                surfaces=self._session_surfaces.get(workflow_mode),
                on_surfaces_changed=remember,
            ),
            on_result,
        )

    async def restore_workflow_session(self, session_id: str) -> None:
        if self._screen._workflow.workflow_mode:
            await self._screen._workflow.session_view.restore_session(session_id)

    def toggle_sidebar(self) -> None:
        self._screen.query_one(SidebarPanel).toggle()

    def open_theme_picker(self, on_result: object) -> None:
        from chrys.app.tui.screens.themes import ThemesScreen

        _push_screen_untyped(self._screen.app, ThemesScreen(self._screen.app.theme), on_result)

    def apply_theme_setting(self, name: str) -> bool:
        return cast("ChrysApp", self._screen.app).apply_theme_setting(name)

    def open_language_picker(self, current_locale: str, on_result: object) -> None:
        from chrys.app.tui.screens.menus.languages import LanguagesScreen

        _push_screen_untyped(self._screen.app, LanguagesScreen(current_locale), on_result)

    def dashboard_visible(self) -> bool:
        return self._screen.query_one(TrajectoryDashboard).foreground

    def show_trajectory_dashboard(self) -> None:
        session_id = self.current_chat_session_id()
        path = (
            trajectory_events_path(self._state_store.session_dir(session_id))
            if session_id and self._state_store is not None
            else None
        )
        self._screen.query_one(TrajectoryDashboard).show_session(session_id, path)
        self.sync_main_surface()

    def hide_trajectory_dashboard(self) -> None:
        self._screen.query_one(TrajectoryDashboard).hide_dashboard()
        self.sync_main_surface()
        self._screen.query_one(InputBar).focus_input()

    def select_trajectory_turn(self, turn_id: str) -> None:
        dashboard = self._screen.query_one(TrajectoryDashboard)
        dashboard.select_turn(turn_id)
        self.sync_main_surface()

    def sync_main_surface(self) -> None:
        dashboard = self._screen.query_one(TrajectoryDashboard)
        shell = self._state.shell.active
        workflow = self._screen._workflow.workflow_mode
        dashboard.display = dashboard.foreground and not shell
        self._screen._workflow_panel.display = workflow and not dashboard.foreground and not shell
        chat = not workflow and not dashboard.foreground and not shell
        self._screen.query_one(ChatPanel).display = chat
        self._screen.query_one(StatusBar).display = shell or chat
        self._screen.query_one(InputBar).display = chat
        self._screen.query_one(Footer).display = not shell
        self._screen._sync_workflow_timer()

    def open_confirm_dialog(
        self,
        *,
        title: StatusMessage,
        message: ConfirmMessage,
        confirm_label: StatusMessage,
        confirm_variant: str,
        on_result: object,
    ) -> None:
        from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

        _push_screen_untyped(
            self._screen.app,
            ConfirmDialog(
                title=title,
                message=message,
                confirm_label=confirm_label,
                confirm_variant=confirm_variant,
                locale_controller=self._locale_controller,
            ),
            on_result,
        )

    def dismiss_interrupt_confirm(self) -> None:
        from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

        if isinstance(self._screen.app.screen, ConfirmDialog):
            self._screen.app.screen.dismiss(False)

    def scroll_chat_to_turn(self, turn_id: str) -> None:
        self._screen.query_one(ChatPanel).scroll_to_turn(turn_id)

    # -------------------------------------------------------------- #
    # Diff and rollback
    # -------------------------------------------------------------- #

    def open_diff_screen(
        self,
        periods_data: dict[int, list[DiffFileEntry]],
        *,
        cwd: str,
        subtitle_parts: tuple[str, ...],
        session_id: str,
        all_entries: list[DiffFileEntry],
        load_data: Callable[[], Awaitable[DiffLoadResult]] | None = None,
    ) -> None:
        from chrys.app.tui.screens.diff import DiffScreen

        self._screen.app.push_screen(
            DiffScreen(
                periods_data,
                cwd=cwd,
                subtitle_parts=subtitle_parts,
                session_id=session_id,
                all_entries=all_entries,
                load_data=load_data,
                locale_controller=self._locale_controller,
            )
        )

    def open_rollback_modal(
        self,
        *,
        cwd: str,
        load_state: Callable[[], Awaitable[RollbackReadState | None]],
        on_result: object,
    ) -> None:
        from chrys.app.tui.screens.diff import RollbackModal

        # RollbackReadState explicitly provides every RollbackModalState member.
        modal_load_state = cast("Callable[[], Awaitable[RollbackModalState | None]]", load_state)
        modal = RollbackModal(
            tracker=None,
            available_turns=[],
            cwd=cwd,
            load_state=modal_load_state,
            locale_controller=self._locale_controller,
        )
        _push_screen_untyped(self._screen.app, modal, on_result)

    def open_rollback_progress_modal(self, operation: Callable[[], Awaitable[None]]) -> None:
        from chrys.app.tui.screens.diff import RollbackProgressModal

        def _start_worker(awaitable: Awaitable[None]) -> object:
            # A restore handoff can leave this operation finishing behind its
            # dismissed modal. A later rollback must not cancel that tail.
            return self._screen.run_worker(awaitable, exclusive=False, group="rollback-progress")

        modal = RollbackProgressModal(
            operation,
            start_worker=_start_worker,
            locale_controller=self._locale_controller,
        )
        self._rollback_progress_modal = modal

        def _clear_modal(_result: None = None) -> None:
            if self._rollback_progress_modal is modal:
                self._rollback_progress_modal = None

        self._screen.app.push_screen(modal, _clear_modal)

    async def restore_welcome_rollback(self, *, session_id: str | None, profile: str, cwd: str) -> None:
        self._screen.query_one(InputBar).retry_mode = False
        panel = self._screen.query_one(ChatPanel)
        await panel.clear()
        # The rolled-back session's file is gone; stale title state would
        # otherwise keep suppressing future auto-title updates (a custom
        # title in UI state blocks them) and pin a stale terminal title.
        self.reset_session_title_state()
        if session_id:
            self._screen.chat_session_id = session_id
            panel.set_session_id(session_id)
        self._screen.chat_workspace_cwd = cwd
        max_context_tokens = (
            self._screen.context_usage_state.max_context_tokens
            if self._screen.context_usage_state
            else DEFAULT_MAX_CONTEXT_TOKENS
        )
        self._screen.context_usage_state = ContextUsageState.with_window(
            used_tokens=0,
            max_context_tokens=max_context_tokens,
        )
        panel.set_workspace_cwd(cwd)
        panel.update_welcome(profile=profile, cwd=cwd)
        self.update_toc()
        self.clear_todos()
        with contextlib.suppress(Exception):
            self._screen.query_one(SidebarPanel).context_panel.reset()

    # -------------------------------------------------------------- #
    # Chat transcript rendering
    # -------------------------------------------------------------- #

    async def add_error(self, message: str, *, action_label: str | None = "Retry") -> None:
        await self._screen.query_one(ChatPanel).add_error(message, action_label=action_label)

    async def add_system(self, text: str, *, key: str | None = None) -> None:
        await self._screen.query_one(ChatPanel).add_system(text, key=key)

    async def update_system(self, key: str, text: str) -> None:
        await self._screen.query_one(ChatPanel).update_system(key, text)

    async def remove_system(self, key: str) -> None:
        await self._screen.query_one(ChatPanel).remove_system(key)

    async def replay_history(
        self,
        messages: list[dict[str, Any]],
        *,
        initial_profile: str,
        file_snapshots: dict[str, list[FileSnapshotPayload]] | None,
        compressed_blocks: dict[str, CompressedBlock] | None,
    ) -> None:
        await self._screen.query_one(ChatPanel).replay_history(
            messages,
            initial_profile=initial_profile,
            file_snapshots=file_snapshots,
            compressed_blocks=compressed_blocks,
        )

    async def add_agent_message(
        self,
        text: str,
        *,
        is_final: bool,
        is_intermediate: bool = False,
        structured_output_completed: bool = False,
        presentation: ProvisionalPresentation | None = None,
        created_at: datetime | None = None,
    ) -> None:
        await self._screen.query_one(ChatPanel).add_agent_message(
            text,
            is_final=is_final,
            is_intermediate=is_intermediate,
            structured_output_completed=structured_output_completed,
            presentation=presentation,
            created_at=created_at,
        )

    async def accept_presentation_attempt(self, attempt_id: str, segment_ids: tuple[str, ...]) -> None:
        await self._screen.query_one(ChatPanel).accept_presentation_attempt(attempt_id, segment_ids)

    async def reject_presentation_attempt(self, attempt_id: str) -> None:
        await self._screen.query_one(ChatPanel).reject_presentation_attempt(attempt_id)

    async def add_user_injection(self, text: str, *, created_at: datetime) -> None:
        await self._screen.query_one(ChatPanel).add_user_message(text, is_injection=True, created_at=created_at)

    def unlock_input_after_consumed_injection(self) -> None:
        self._screen.query_one(InputBar).unlock_and_clear()

    def unlock_input_after_abandoned_injection(self) -> None:
        self._screen.query_one(InputBar).unlock_and_keep()

    async def add_tool_start(
        self,
        call_id: str,
        tool_name: str,
        tool_kind: str,
        args_json: str,
        *,
        args: dict[str, Any] | None,
        provider_hosted: bool = False,
        hosted_family: str = "",
        provider: str = "",
        provider_item_type: str = "",
        provider_status: str = "",
        provider_call_id: str = "",
        canonical_status: str = "running",
    ) -> None:
        await self._screen.query_one(ChatPanel).add_tool_start(
            call_id,
            tool_name,
            tool_kind,
            args_json,
            args=args,
            provider_hosted=provider_hosted,
            hosted_family=hosted_family,
            provider=provider,
            provider_item_type=provider_item_type,
            provider_status=provider_status,
            provider_call_id=provider_call_id,
            canonical_status=canonical_status,
        )

    def update_tool_progress(
        self,
        call_id: str,
        lines: list[str],
        *,
        image_contents: list[object] | None = None,
        snapshot_metadata: dict[str, Any] | None = None,
        provider_status: str = "",
    ) -> None:
        self._screen.query_one(ChatPanel).update_tool_progress(
            call_id,
            lines,
            image_contents=image_contents,
            snapshot_metadata=snapshot_metadata,
            provider_status=provider_status,
        )

    def update_tool_args(self, call_id: str, args: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            self._screen.query_one(ChatPanel).update_tool_args(call_id, args)

    def update_tool_status(
        self,
        call_id: str,
        status: str,
        *,
        provider_status: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._screen.query_one(ChatPanel).update_tool_status(
            call_id,
            status,
            provider_status=provider_status,
            metadata=metadata,
        )

    async def add_tool_result(
        self,
        call_id: str,
        tool_name: str,
        result: str,
        duration_ms: int,
        *,
        image_contents: list[object] | None,
        file_snapshot: FileSnapshotPayload | None,
        approval: Any,
        metadata: dict[str, Any],
        artifacts: list[dict[str, Any]] | None = None,
        provider_status: str = "",
        canonical_status: str = "completed",
    ) -> None:
        await self._screen.query_one(ChatPanel).add_tool_result(
            call_id,
            tool_name,
            result,
            duration_ms,
            image_contents=image_contents,
            file_snapshot=file_snapshot,
            approval=approval,
            metadata=metadata,
            artifacts=artifacts,
            provider_status=provider_status,
            canonical_status=canonical_status,
        )

    async def add_context_fold(
        self, context_id: str, summary: str, freed_messages: int, turn_range: tuple[int, int]
    ) -> bool:
        panel = self._screen.query_one(ChatPanel)
        await panel.add_context_fold(context_id, summary, freed_messages, turn_range)
        return panel.mark_turn_range_compressed(turn_range)

    def add_compressed_block(
        self,
        context_id: str,
        summary: str,
        freed_messages: int = 0,
        turn_range: tuple[int, int] = (0, 0),
    ) -> None:
        context_panel = self._screen.query_one(SidebarPanel).context_panel
        context_panel.add_compressed_block(context_id, summary, freed_messages, turn_range)

    async def add_compaction_start(self, compaction_id: str) -> None:
        await self._screen.query_one(ChatPanel).add_compaction_start(compaction_id)

    def complete_compaction(
        self,
        compaction_id: str,
        *,
        outcome: str,
        duration_ms: int = 0,
        last_words: str = "",
        format_violation: str = "",
        failure_reason: str = "",
    ) -> None:
        self._screen.query_one(ChatPanel).complete_compaction(
            compaction_id,
            outcome=outcome,
            duration_ms=duration_ms,
            last_words=last_words,
            format_violation=format_violation,
            failure_reason=failure_reason,
        )

    def show_compaction_retry(self, message: str, attempt: int, max_attempts: int, delay_seconds: int) -> None:
        self._screen.query_one(ChatPanel).show_compaction_retry(message, attempt, max_attempts, delay_seconds)

    # -------------------------------------------------------------- #
    # Sub-agent transcript rendering
    # -------------------------------------------------------------- #

    def link_sub_agent_invocation(
        self,
        parent_call_id: str,
        invocation_id: str,
        agent_name: str,
        sub_agent_log_file: str = "",
        *,
        tool_name: str = "",
    ) -> None:
        self._screen.query_one(ChatPanel).link_sub_agent_invocation(
            parent_call_id,
            invocation_id,
            agent_name,
            sub_agent_log_file,
            tool_name=tool_name,
        )

    def add_sub_agent_message(
        self,
        agent_name: str,
        invocation_id: str,
        text: str,
        *,
        presentation: ProvisionalPresentation | None = None,
    ) -> None:
        self._screen.query_one(ChatPanel).add_sub_agent_message(
            agent_name,
            invocation_id,
            text,
            presentation=presentation,
        )

    def accept_sub_agent_presentation(
        self,
        invocation_id: str,
        attempt_id: str,
        segment_ids: tuple[str, ...],
    ) -> None:
        self._screen.query_one(ChatPanel).accept_sub_agent_presentation(
            invocation_id,
            attempt_id,
            segment_ids,
        )

    def reject_sub_agent_presentation(self, invocation_id: str, attempt_id: str) -> None:
        self._screen.query_one(ChatPanel).reject_sub_agent_presentation(invocation_id, attempt_id)

    async def add_sub_agent_tool_start(
        self,
        agent_name: str,
        invocation_id: str,
        tool_name: str,
        args: dict[str, Any],
        call_id: str,
        *,
        tool_kind: str,
        provider_hosted: bool = False,
        hosted_family: str = "",
        provider: str = "",
        provider_item_type: str = "",
        provider_status: str = "",
        provider_call_id: str = "",
    ) -> None:
        await self._screen.query_one(ChatPanel).add_sub_agent_tool_start(
            agent_name,
            invocation_id,
            tool_name,
            args,
            call_id,
            tool_kind=tool_kind,
            provider_hosted=provider_hosted,
            hosted_family=hosted_family,
            provider=provider,
            provider_item_type=provider_item_type,
            provider_status=provider_status,
            provider_call_id=provider_call_id,
        )

    def update_sub_agent_tool_args(self, invocation_id: str, call_id: str, args: dict[str, Any]) -> None:
        self._screen.query_one(ChatPanel).update_sub_agent_tool_args(invocation_id, call_id, args)

    def update_sub_agent_tool_status(
        self,
        invocation_id: str,
        call_id: str,
        status: str,
        *,
        metadata: dict[str, Any] | None = None,
        provider_status: str = "",
    ) -> None:
        self._screen.query_one(ChatPanel).update_sub_agent_tool_status(
            invocation_id,
            call_id,
            status,
            metadata=metadata,
            provider_status=provider_status,
        )

    def update_sub_agent_tool_progress(
        self,
        invocation_id: str,
        call_id: str,
        lines: list[str],
        *,
        image_contents: list[object] | None = None,
        snapshot_metadata: dict[str, Any] | None = None,
        provider_status: str = "",
    ) -> None:
        self._screen.query_one(ChatPanel).update_sub_agent_tool_progress(
            invocation_id,
            call_id,
            lines,
            image_contents=image_contents,
            snapshot_metadata=snapshot_metadata,
            provider_status=provider_status,
        )

    def complete_sub_agent_tool(
        self,
        agent_name: str,
        invocation_id: str,
        call_id: str,
        result: str,
        duration_ms: int,
        *,
        image_contents: list[object] | None,
        artifacts: list[dict[str, Any]] | None,
        approval: Any,
        metadata: dict[str, Any],
        provider_status: str = "",
        canonical_status: str = "completed",
    ) -> None:
        self._screen.query_one(ChatPanel).complete_sub_agent_tool(
            agent_name,
            invocation_id,
            call_id,
            result,
            duration_ms,
            image_contents=image_contents,
            artifacts=artifacts,
            approval=approval,
            metadata=metadata,
            provider_status=provider_status,
            canonical_status=canonical_status,
        )

    def update_sub_agent_progress(
        self,
        invocation_id: str,
        tool_call_count: int,
        total_tokens: int,
        total_usage_tokens: int,
        usage_unreported_attempts: int = 0,
    ) -> None:
        self._screen.query_one(ChatPanel).update_sub_agent_progress(
            invocation_id,
            tool_call_count,
            total_tokens,
            total_usage_tokens,
            usage_unreported_attempts,
        )

    def add_sub_agent_compaction_start(self, agent_name: str, invocation_id: str, compaction_id: str) -> None:
        self._screen.query_one(ChatPanel).add_sub_agent_compaction_start(agent_name, invocation_id, compaction_id)

    def complete_sub_agent_compaction(
        self,
        invocation_id: str,
        compaction_id: str,
        *,
        outcome: str,
        duration_ms: int = 0,
        format_violation: str = "",
        failure_reason: str = "",
    ) -> None:
        self._screen.query_one(ChatPanel).complete_sub_agent_compaction(
            invocation_id,
            compaction_id,
            outcome=outcome,
            duration_ms=duration_ms,
            format_violation=format_violation,
            failure_reason=failure_reason,
        )

    def record_sub_agent_compaction_committed(self, invocation_id: str, compaction_id: str) -> None:
        self._screen.query_one(ChatPanel).record_sub_agent_compaction_committed(
            invocation_id,
            compaction_id,
        )

    def sub_agent_retry_attempt(
        self,
        invocation_id: str,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
    ) -> None:
        self._screen.query_one(ChatPanel).sub_agent_retry_attempt(
            invocation_id,
            message,
            attempt,
            max_attempts,
            delay_seconds,
        )

    def sub_agent_paused(
        self,
        invocation_id: str,
        reason: str,
        last_error: str,
        retry_attempts: int,
        diagnostic_path: str | None = None,
        last_error_display: str | None = None,
    ) -> None:
        self._screen.query_one(ChatPanel).sub_agent_paused(
            invocation_id,
            reason,
            last_error,
            retry_attempts,
            diagnostic_path,
            last_error_display,
        )

    def sub_agent_resumed_after_pause(self, invocation_id: str) -> None:
        self._screen.query_one(ChatPanel).sub_agent_resumed_after_pause(invocation_id)

    def sub_agent_cascade_aborted(self, invocation_id: str) -> None:
        self._screen.query_one(ChatPanel).sub_agent_cascade_aborted(invocation_id)

    def sub_agent_aborted(self, invocation_id: str, last_error: str) -> None:
        self._screen.query_one(ChatPanel).sub_agent_aborted(invocation_id, last_error)

    async def show_retry_attempt(
        self,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
    ) -> None:
        panel = self._screen.query_one(ChatPanel)
        await panel.prepare_retry()
        await panel.add_retry(message, attempt, max_attempts, delay_seconds)
        self.show_status(STATUS_RETRYING.bind(attempt=attempt, max_attempts=max_attempts))

    # -------------------------------------------------------------- #
    # Approval dialogs
    # -------------------------------------------------------------- #

    async def build_approval_body(self, event: ApprovalRequest) -> object | None:
        from chrys.app.tui.screens.dialogs.approval import create_approval_body

        return await create_approval_body(
            event.tool_name,
            event.tool_kind,
            event.args,
            workspace_cwd=event.workspace_cwd,
        )

    def approval_body_bypass(self, body: object | None) -> ApprovalBypassDecision | None:
        from chrys.app.tui.screens.dialogs.approval import ApprovalBody

        if body is None:
            return None
        if not isinstance(body, ApprovalBody):
            return None
        bypass = body.bypass
        if bypass is None:
            return None
        return ApprovalBypassDecision(
            approved=bypass.approved,
            reason=bypass.reason,
            debug_reason=bypass.debug_reason,
        )

    def show_approval_dialog(
        self,
        event: ApprovalRequest,
        approval_body: object | None,
        on_result: Callable[[tuple[bool, str, dict[str, Any] | None] | None], None],
        *,
        verdict: ApprovalReviewed | None,
    ) -> ApprovalDialogHandle:
        from chrys.app.tui.screens.dialogs.approval import ApprovalBody, ApprovalDialog

        body = approval_body if isinstance(approval_body, ApprovalBody) else None
        dialog = ApprovalDialog(
            caller_name=event.caller_name,
            tool_name=event.tool_name,
            tool_kind=event.tool_kind,
            args=event.args,
            judging=event.judging,
            approval_body=body,
            presentation_kind=event.presentation_kind,
            reuse_offer=event.reuse_offer,
            verdict=_judge_verdict(verdict) if verdict is not None else None,
        )
        self._screen.app.push_screen(dialog, on_result)
        return dialog

    def deliver_approval_verdict(self, dialog: ApprovalDialogHandle, event: ApprovalReviewed) -> None:
        from chrys.app.tui.screens.dialogs.approval import ApprovalDialog

        if isinstance(dialog, ApprovalDialog):
            # The dialog keeps a verdict that beats its mount and shows it then.
            dialog.receive_verdict(_judge_verdict(event))

    def dismiss_approval_dialog(self, dialog: ApprovalDialogHandle) -> None:
        """Close a cancelled approval through its cancellation-only path."""
        from chrys.app.tui.screens.dialogs.approval import ApprovalDialog

        if isinstance(dialog, ApprovalDialog):
            dialog.dismiss_due_to_cancellation()

    def approval_dialog_tool_name(self, dialog: ApprovalDialogHandle) -> str:
        from chrys.app.tui.screens.dialogs.approval import ApprovalDialog

        if isinstance(dialog, ApprovalDialog):
            return dialog.tool_name
        return ""

    def set_auto_review_count(self, count: int) -> None:
        self._screen.query_one(AppHeader).set_auto_review_count(count)

    def notify_approval_required(self) -> None:
        self.notify_event(NotificationEvent.APPROVAL_REQUIRED)

    # -------------------------------------------------------------- #
    # Ask-user dialogs
    # -------------------------------------------------------------- #

    def show_question_dialog(
        self,
        event: QuestionToUser,
        draft: PromptDraft | None,
        on_result: Callable[[object], None],
    ) -> QuestionDialogHandle:
        from chrys.app.tui.screens.dialogs.ask_user import AskUserDialog

        # Session identity determines whether the question belongs to the Chat transcript.
        dialog = AskUserDialog(
            request_id=event.request_id,
            questions=event.questions,
            caller_name=event.caller_name,
            draft=draft,
            allow_inline=bool(event.call_id)
            and (event.session_id is None or event.session_id == self.current_chat_session_id()),
        )
        self._screen.app.push_screen(dialog, on_result)
        return dialog

    def parse_question_dialog_result(self, result: object) -> QuestionDialogResult:
        from chrys.app.tui.screens.dialogs.ask_user import AskUserInlineResult

        if result is None:
            return None
        if isinstance(result, AskUserInlineResult):
            return InlineQuestionDialogResult(draft=result.draft)
        if isinstance(result, tuple) and len(result) == 2:
            request_id, answers = result
            if (
                isinstance(request_id, str)
                and isinstance(answers, tuple)
                and all(isinstance(answer, AskUserAnswer) for answer in answers)
            ):
                return AnswerQuestionDialogResult(request_id=request_id, answers=answers)
        return None

    def show_question_inline(self, event: QuestionToUser, draft: PromptDraft | None = None) -> bool:
        if not event.call_id:
            return False
        try:
            panel = self._screen.query_one(ChatPanel)
        except Exception:
            return False
        return panel.show_ask_user_inline(
            event.call_id,
            event.request_id,
            event.questions,
            draft=draft,
        )

    def clear_question_inline(self, call_id: str, request_id: str) -> bool:
        """Clear a timed-out inline prompt if its request still matches."""
        try:
            panel = self._screen.query_one(ChatPanel)
        except Exception:
            return False
        return panel.clear_ask_user_inline(call_id, request_id)

    def question_can_reopen_modal(self, event: QuestionToUser) -> bool:
        if not event.call_id:
            return True
        try:
            panel = self._screen.query_one(ChatPanel)
        except Exception:
            return True
        return panel.is_tool_running(event.call_id)

    def notify_ask_user(self) -> None:
        self.notify_event(NotificationEvent.ASK_USER)

    def focus_input(self) -> None:
        with contextlib.suppress(Exception):
            self._screen.query_one(InputBar).focus_input()

    # -------------------------------------------------------------- #
    # Suggestions
    # -------------------------------------------------------------- #

    @property
    def is_attached(self) -> bool:
        return self._screen.is_attached

    @property
    def suggestions_active(self) -> bool:
        with contextlib.suppress(Exception):
            return bool(self._screen.query_one(InputBar).suggestions_active)
        return False

    def show_suggestions(
        self,
        mode: str,
        items: Sequence[SuggestionEntry],
        *,
        disabled: set[str] | None = None,
        initial: str | None = None,
        title: str = "",
    ) -> None:
        from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList

        with contextlib.suppress(NoMatches):
            self._screen.query_one(SuggestionList).show(
                mode, list(items), disabled=disabled, initial=initial, title=title
            )
            self._screen.query_one(InputBar).set_suggestions_active(True, mode=mode)

    def show_suggestions_loading(self, mode: str, *, title: str = "") -> None:
        """Open a suggestion popup immediately with only its loading indicator."""
        from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList

        with contextlib.suppress(NoMatches):
            self._screen.query_one(SuggestionList).show_loading(mode, title=title)
            self._screen.query_one(InputBar).set_suggestions_active(True, mode=mode)

    def update_suggestions(
        self,
        items: Sequence[SuggestionEntry],
        *,
        disabled: set[str] | None = None,
        title: str | None = None,
    ) -> None:
        from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList

        with contextlib.suppress(NoMatches):
            self._screen.query_one(SuggestionList).update(list(items), disabled=disabled, title=title)

    def hide_suggestions(self) -> None:
        from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList

        with contextlib.suppress(NoMatches):
            self._screen.query_one(SuggestionList).hide()
            input_bar = self._screen.query_one(InputBar)
            input_bar.set_suggestions_active(False)
            input_bar.focus_input()

    async def load_prompt_history(self, *, max_entries: int) -> list[str]:
        """Load cross-session prompt history without blocking the TUI loop."""
        return await self._screen.query_one(InputBar).load_prompt_history(max_entries=max_entries)

    def run_file_suggestion_worker(self, awaitable: Awaitable[None], *, name: str, group: str) -> Worker[None]:
        return self._screen.run_worker(awaitable, name=name, group=group)

    def move_suggestion_cursor(self, direction: str) -> None:
        from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList

        suggestions = self._screen.query_one(SuggestionList)
        if direction == "up":
            suggestions.move_cursor_up()
            return
        suggestions.move_cursor_down()

    def select_highlighted_suggestion(self, *, execute: bool) -> bool:
        from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList

        return bool(self._screen.query_one(SuggestionList).select_highlighted(execute=execute))

    def show_subcommand_suggestions(
        self,
        items: Sequence[SuggestionEntry],
        *,
        initial: str | None = None,
    ) -> None:
        from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList

        self._screen.query_one(SuggestionList).show("subcommands", list(items), initial=initial)

    def input_value(self) -> str:
        return str(self._screen.query_one(InputBar).value)

    def set_input_value(self, value: str) -> None:
        self._screen.query_one(InputBar).value = value

    def replace_input_trigger_text(self, trigger: str, replacement: str) -> None:
        self._screen.query_one(InputBar).replace_trigger_text(trigger, replacement)

    # -------------------------------------------------------------- #
    # Shell mode, focus, and paste
    # -------------------------------------------------------------- #

    def enter_shell_mode(self) -> None:
        dashboard = self._screen.query_one(TrajectoryDashboard)
        session_json = self._screen.query_one(SessionJsonPanel)
        shell = self._screen.query_one(ShellPanel)
        status = self._screen.query_one(StatusBar)
        sidebar = self._screen.query_one(SidebarPanel)
        self._shell_status_snapshot = status.snapshot()
        if dashboard.suspend_for_shell_mode():
            session_json.suspend_for_shell_mode()
        else:
            session_json.hide_session_json()
        # The dashboard hides the status bar while foreground; shell mode needs it.
        self.sync_main_surface()
        sidebar.add_class("-shell-active")
        shell.show()
        # Flash last: its layout-free face flip rebuilds the compositor map
        # synchronously, and any widget repaint that lands before the screen's
        # deferred relayout paints from that map — it must already describe
        # the final shell layout or the half-applied one flashes on screen.
        status.flash(STATUS_SHELL_MODE.bind(), warn=True)
        shell.query_one(Terminal).focus()

    def exit_shell_mode(self) -> None:
        dashboard = self._screen.query_one(TrajectoryDashboard)
        session_json = self._screen.query_one(SessionJsonPanel)
        shell = self._screen.query_one(ShellPanel)
        status = self._screen.query_one(StatusBar)
        input_bar = self._screen.query_one(InputBar)
        sidebar = self._screen.query_one(SidebarPanel)
        shell.hide()
        dashboard_restored = dashboard.finish_shell_mode()
        session_json_visible = dashboard.session_json_visible
        session_json.finish_shell_mode(restore=session_json_visible)
        self.sync_main_surface()
        sidebar.remove_class("-shell-active")
        # Restore last for the same reason flash runs last in enter_shell_mode:
        # its map rebuild must see the fully restored layout.
        status.restore(self._shell_status_snapshot)
        if not dashboard_restored and input_bar.display:
            input_bar.focus_input()

    def set_alternate_screen_active(self, active: bool) -> None:
        status = self._screen.query_one(StatusBar)
        footer = self._screen.query_one(Footer)
        sidebar = self._screen.query_one(SidebarPanel)
        sidebar.suppress_visibility("alternate-screen", active)
        if active:
            footer.display = False
            status.flash(STATUS_INTERACTIVE_MODE.bind(), warn=True)
            self._screen.query_one(ShellPanel).query_one(Terminal).focus()
            return
        if self._state.shell.active:
            status.flash(STATUS_SHELL_MODE.bind(), warn=True)
            self._screen.query_one(ShellPanel).query_one(Terminal).focus()
        else:
            footer.display = True
            status.clear_status()

    def insert_paste_payload(self, text: str) -> bool:
        try:
            input_bar = self._screen.query_one(InputBar)
        except Exception:
            return False
        if not text:
            return input_bar.insert_clipboard_image_paste()
        return input_bar.insert_image_paste_payload(text)

    # -------------------------------------------------------------- #
    # Agent-load dialogs
    # -------------------------------------------------------------- #

    def create_agent_load_dialog(self, *, title: StatusMessage, subtitle: str) -> AgentLoadDialogHandle:
        from chrys.app.tui.screens.dialogs.agent_load import AgentLoadDialog

        return AgentLoadDialog(title=title, subtitle=subtitle, locale_controller=self._locale_controller)

    def prepare_agent_load_ui(
        self,
        *,
        title: StatusMessage,
        session_id: str | None,
        update_clipboard_dir: bool,
        capture_status_snapshot: bool,
    ) -> dict | None:
        status = self._screen.query_one(StatusBar)
        snapshot = status.snapshot() if capture_status_snapshot else None
        status.start_run()
        status.show(title)

        input_bar = self._screen.query_one(InputBar)
        if update_clipboard_dir:
            input_bar.set_clipboard_image_dir(clipboard_image_dir_for_session(self._state_store, session_id))
        return snapshot

    async def push_agent_load_dialog(self, dialog: AgentLoadDialogHandle) -> None:
        rollback_modal = self._rollback_progress_modal
        if rollback_modal is not None:
            self._rollback_progress_modal = None
            rollback_modal.handoff_to_session_restore()

        def _restore_input_focus(_result: None = None) -> None:
            if self._state.shell.active or self._state.shell.fullscreen_terminal:
                return
            with contextlib.suppress(Exception):
                self._screen.query_one(InputBar).focus_input()

        pushed = _push_screen_untyped(self._screen.app, dialog, _restore_input_focus)
        if inspect.isawaitable(pushed):
            await pushed

    def restore_agent_load_status(self, snapshot: dict) -> None:
        with contextlib.suppress(Exception):
            self._screen.query_one(StatusBar).restore(snapshot)

    def show_load_status(self, message: StatusMessage) -> None:
        self._screen.query_one(StatusBar).show(message)

    def render_status_message(self, message: StatusMessage) -> str:
        return self._render_status_message(message)

    def flash_agent_load_failed(self, message: str) -> None:
        self._screen.query_one(StatusBar).flash(STATUS_AGENT_LOAD_FAILED.bind(message=message), error=True)

    # -------------------------------------------------------------- #
    # Image-compression dialogs
    # -------------------------------------------------------------- #

    def create_image_compression_dialog(self, *, title: StatusMessage) -> ImageCompressionDialogHandle:
        from chrys.app.tui.screens.dialogs.image_compression import ImageCompressionDialog

        return ImageCompressionDialog(title=title, locale_controller=self._locale_controller)

    async def push_image_compression_dialog(self, dialog: ImageCompressionDialogHandle) -> None:
        pushed = _push_screen_untyped(self._screen.app, dialog)
        if inspect.isawaitable(pushed):
            await pushed
