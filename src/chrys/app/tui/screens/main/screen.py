# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Main chat screen — the primary interaction surface."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import reprlib
from collections.abc import Awaitable, Callable
from functools import partial
from types import FunctionType, MethodType
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast

from textual import __version__ as TEXTUAL_VERSION
from textual import on, work
from textual.actions import SkipAction
from textual.binding import Binding
from textual.containers import Horizontal
from textual.content import Content
from textual.events import DescendantFocus, Paste, Resize, ScreenResume, ScreenSuspend
from textual.geometry import Offset, Size
from textual.reactive import reactive
from textual.screen import Screen
from textual.selection import Selection, SelectState
from textual.timer import Timer
from textual.widget import Widget

from chrys.app.tui.behaviors.right_click_copy import RightClickScreenCopyMixin
from chrys.app.tui.binding_display import QUIT_BINDING, localized_binding
from chrys.app.tui.i18n import render_content, render_str
from chrys.app.tui.language import LANGUAGE_OPTIONS, LANGUAGE_UNKNOWN_LOCALE
from chrys.app.tui.screens.dialogs.app_mode import AppModeDialog
from chrys.app.tui.screens.dialogs.editor import EditorDialog, EditorDialogResult
from chrys.app.tui.screens.main.action_availability import check_main_action
from chrys.app.tui.screens.main.commands import INVALID_COMMAND_TITLE_REF, SlashCommandActions
from chrys.app.tui.screens.main.config_actions import (
    RuntimeConfigCallbacks,
    RuntimeConfigController,
)
from chrys.app.tui.screens.main.copy_actions import CopyActionController
from chrys.app.tui.screens.main.diff_controller import DiffController, LiveDiffOwner, LiveDiffTracker
from chrys.app.tui.screens.main.event_handlers import BackendEventCallbacks, BackendEventHandler
from chrys.app.tui.screens.main.input_flow import InputFlowController
from chrys.app.tui.screens.main.model_indicator import (
    compute_model_indicator_state,
    is_model_selection_locked,
)
from chrys.app.tui.screens.main.navigation import MainNavigationController
from chrys.app.tui.screens.main.ports import StatusMessage
from chrys.app.tui.screens.main.rollback_controller import RollbackController
from chrys.app.tui.screens.main.runtime_info import RegistryRuntimeInfoProvider
from chrys.app.tui.screens.main.session_handlers import RestoreRequest, SessionCallbacks, SessionHandler
from chrys.app.tui.screens.main.session_title import SessionTitleController
from chrys.app.tui.screens.main.settings_coordinator import (
    SETTINGS_TITLE,
    SettingsCoordinator,
    SettingsCoordinatorCallbacks,
)
from chrys.app.tui.screens.main.settings_persistence import (
    SETTINGS_FLUSH_LOCK_TIMEOUT_SECONDS,
    SETTINGS_SAVE_DELAY_SECONDS,
    SettingsPersistenceQueue,
)
from chrys.app.tui.screens.main.shell_mode import ShellModeController
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.app.tui.screens.main.subscriptions import MainScreenSubscriptions
from chrys.app.tui.screens.main.suggestions import SuggestionCallbacks, SuggestionHandler
from chrys.app.tui.screens.main.tool_action_bridge import ToolActionBridge
from chrys.app.tui.screens.main.view_adapter import MainScreenViewAdapter
from chrys.app.tui.screens.main.workflow_controller import WorkflowController
from chrys.app.tui.screens.main.workspace_actions import WorkspaceCallbacks, WorkspaceController
from chrys.app.tui.screens.main.workspace_branch import WorkspaceBranchController
from chrys.app.tui.screens.settings import GENERAL_TAB_ID
from chrys.app.tui.screens.settings.dialog import THEME_PREVIEW_HINT
from chrys.app.tui.screens.themes import ThemeEditorPanel
from chrys.app.tui.support.gc_freeze import (
    GcFreezeBlockReason,
    GcFreezeParticipant,
    abort_textual_screen_gc_freeze,
    after_textual_screen_gc_freeze,
    prepare_textual_screen_for_gc,
    raise_gc_freeze_hook_errors,
)
from chrys.app.tui.terminal.panel import ShellPanel
from chrys.app.tui.terminal.widget import Terminal
from chrys.app.tui.widgets.chat.messages import ConversationStatusAction
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.ports import TranscriptLocalizationPort
from chrys.app.tui.widgets.chat.renderers.ask_user import AskUserInlineSubmitted
from chrys.app.tui.widgets.chat.renderers.sleep import SleepSkipClicked
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentAbortClicked, SubAgentRetryClicked
from chrys.app.tui.widgets.chat.scroll_controller import scroll_gc_paused
from chrys.app.tui.widgets.chat.selection_controller import ChatSelectionController
from chrys.app.tui.widgets.chat.session_json import SessionJsonPanel
from chrys.app.tui.widgets.chat.tool_call import ToolViewRequested, is_tool_copy_excluded
from chrys.app.tui.widgets.chrome.app_header import AppHeader
from chrys.app.tui.widgets.chrome.commands import is_slash_command_candidate
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.chrome.status_bar import StatusBar
from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList
from chrys.app.tui.widgets.editor import MESSAGE_EDITOR_MAX_CHARACTERS, EditorBufferSnapshot, EditorMode
from chrys.app.tui.widgets.sidebar.context import ContextUsageState
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
from chrys.app.tui.widgets.sidebar.tasks import TodoListState
from chrys.app.tui.widgets.sidebar.toc import ConversationToc
from chrys.app.tui.widgets.trajectory import TrajectoryDashboard
from chrys.app.tui.widgets.workflow import text as workflow_text
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.app.tui.widgets.workflow.panel import WorkflowPanel
from chrys.foundation.config.settings import (
    DEFAULT_LOCALE,
    DEFAULT_TRAJECTORY_VERIFY_COMMANDS,
    DEFAULT_WORKSPACE_MRU_MAX_ENTRIES,
    persist_editor_keymap,
    persist_tool_groups_expanded,
)
from chrys.foundation.events.types import (
    ApprovalModeUpdated,
    Error,
    ExecutionChanged,
    SessionRestored,
)
from chrys.foundation.i18n import DisplaySequence, Localizer, MessageRef, msg
from chrys.foundation.models.approval_reuse import ReuseChoice
from chrys.foundation.models.ask_user import AskUserAnswer
from chrys.foundation.platform import get_platform, safe_getcwd
from chrys.service.approval.policy import ApprovalMode
from chrys.service.profiles.models.schema import UNCONFIGURED_MODEL_ID, is_model_profile_selectable
from chrys.service.session.sub_agent_transcript import load_persisted_sub_agent_transcript

if TYPE_CHECKING:
    from pathlib import Path

    from textual.app import ComposeResult
    from textual.theme import Theme

    from chrys.app.tui.app import ChrysApp
    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.notifications import NotificationService
    from chrys.app.tui.notifications.settings import NotificationSettings
    from chrys.app.tui.screens.sessions import WorkflowSessionPick
    from chrys.app.tui.screens.themes.picker import ThemesScreen
    from chrys.app.tui.themes.store import ThemeFileRevision, UserThemeStore
    from chrys.app.update_check import UpdateNotice
    from chrys.foundation.config.settings_store import PersistResult, SettingsHandle
    from chrys.foundation.events.bus import EventBus
    from chrys.orchestration.engine.engine import AgentEngine
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.session.sub_agent_transcript import PersistedSubAgentTranscript
    from chrys.service.state.store import StateStore


logger = logging.getLogger(__name__)

type StartupRestore = Literal["restored", "declined", "failed"]
"""Startup restore result; ``declined`` means the user chose no folder for a session whose folder is gone."""

_SESSIONS_BINDING = msg("tui.binding.sessions", fallback="Sessions")
_AGENTS_BINDING = msg("tui.binding.agents", fallback="Agents")
_MODELS_BINDING = msg("tui.binding.models", fallback="Models")
_LOGS_BINDING = msg("tui.binding.logs", fallback="Logs")
_HELP_BINDING = msg("tui.binding.help", fallback="Help")
_SIDEBAR_BINDING = msg("tui.binding.sidebar", fallback="Sidebar")
_THEMES_BINDING = msg("tui.binding.themes", fallback="Themes")
_SETTINGS_BINDING = msg("tui.binding.settings", fallback="Settings")
_SETTINGS_REJECTED = msg(
    "tui.settings.save_rejected",
    fallback="Could not save settings; the store rejected: {keys}.",
)
_BREAK_BINDING = msg("tui.binding.break_agent", fallback="Break")
_TRAJECTORY_BINDING = msg("tui.binding.trajectory", fallback="Trajectory")
_CHAT_PAGE_UP_BINDING = msg("tui.binding.chat_page_up", fallback="Chat Page Up")
_CHAT_PAGE_DOWN_BINDING = msg("tui.binding.chat_page_down", fallback="Chat Page Down")
_CHAT_SCROLL_BOTTOM_BINDING = msg("tui.binding.chat_scroll_bottom", fallback="Chat Scroll Bottom")
_EDITOR_DRAFT_TOO_LARGE_TITLE = msg(
    "tui.editor.title.draft_too_large",
    fallback="Draft too large for editor",
)
_EDITOR_DRAFT_TOO_LARGE = msg(
    "tui.editor.draft_too_large",
    fallback="Editor supports drafts up to {limit} characters.",
)
_MODEL_UNCONFIGURED_TITLE = msg("tui.model_guard.title", fallback="Model not configured or selected")
_MODEL_UNCONFIGURED_MESSAGE = msg(
    "tui.model_guard.message",
    fallback="Your message was not sent. Configure and select a model to get started.",
)
_MODEL_UNCONFIGURED_SETUP = msg("tui.model_guard.button.setup", fallback="Set up model")

TEXTUAL_BACKGROUND_REFRESH_FORK_VERSION = "8.2.7"
"""The Textual release whose private ``Screen._compositor_refresh`` ``MainScreen`` mirrors."""


# Work arguments can be whole prompts; worker descriptions show them abbreviated.
_WORK_ARGUMENT_REPR = reprlib.Repr(maxstring=60, maxother=60)


def _describe_work(work: Callable[[], Awaitable[object]]) -> tuple[str, str]:
    """Name and describe a worker after its work, as ``@work`` did: ``method(args)``."""
    target: Callable[..., object] = work
    args: tuple[object, ...] = ()
    keywords: dict[str, object] = {}
    if isinstance(work, partial):
        target, args, keywords = work.func, work.args, work.keywords
    name = target.__name__ if isinstance(target, FunctionType | MethodType) else type(target).__name__
    arguments = [
        *(_WORK_ARGUMENT_REPR.repr(arg) for arg in args),
        *(f"{key}={_WORK_ARGUMENT_REPR.repr(value)}" for key, value in keywords.items()),
    ]
    return name, f"{name}({', '.join(arguments)})"


class MainScreen(RightClickScreenCopyMixin, Screen):
    """Primary chat screen with header, chat panel, side panel, input bar, and footer."""

    BINDINGS: ClassVar[list] = [
        localized_binding("ctrl+b", "interrupt", _BREAK_BINDING, show=False),
        localized_binding("f1", "sessions", _SESSIONS_BINDING, priority=True),
        localized_binding("f2", "agents_config", _AGENTS_BINDING, priority=True),
        localized_binding("f4", "models_config", _MODELS_BINDING, priority=True),
        localized_binding("f6", "show_log_viewer", _LOGS_BINDING, priority=True),
        localized_binding("f8", "open_guide", _HELP_BINDING, priority=True),
        Binding("ctrl+r", "prompt_history", show=False, priority=True),
        localized_binding("f9", "pick_theme", _THEMES_BINDING, priority=True),
        localized_binding("f10", "settings", _SETTINGS_BINDING, priority=True),
        localized_binding("f12", "toggle_trajectory_dashboard", _TRAJECTORY_BINDING, show=True, priority=True),
        localized_binding("ctrl+g", "toggle_sidebar", _SIDEBAR_BINDING, priority=True),
        localized_binding("pageup", "chat_page_up", _CHAT_PAGE_UP_BINDING, show=False, priority=True),
        localized_binding("pagedown", "chat_page_down", _CHAT_PAGE_DOWN_BINDING, show=False, priority=True),
        localized_binding(
            "ctrl+end",
            "chat_scroll_bottom",
            _CHAT_SCROLL_BOTTOM_BINDING,
            show=False,
            priority=True,
        ),
        Binding("escape", "escape", show=False, priority=True),
        localized_binding("ctrl+q", "quit", QUIT_BINDING, show=False, priority=True),
    ]

    CSS_PATH = "screen.tcss"

    agent_running_state: reactive[bool] = reactive(False, init=False, always_update=True)
    agent_loading_state: reactive[bool] = reactive(False, init=False, always_update=True)
    has_messages_state: reactive[bool] = reactive(False, init=False, always_update=True)
    header_subtitle_parts: reactive[tuple[str, ...]] = reactive(())
    header_approval_mode: reactive[ApprovalMode] = reactive(ApprovalMode.MANUAL, always_update=True)
    chat_profile_name: reactive[str] = reactive("")
    chat_session_id: reactive[str] = reactive("")
    chat_session_title: reactive[str] = reactive("")
    chat_workspace_cwd: reactive[str] = reactive("")
    chat_workspace_branch: reactive[str] = reactive("")
    context_usage_state: reactive[ContextUsageState | None] = reactive(None, always_update=True)
    todo_state: reactive[TodoListState | None] = reactive(None, always_update=True)
    shell_mode_state: reactive[bool] = reactive(False, init=False)
    _reactive_state_initialized: bool = False

    def __init__(
        self,
        event_bus: EventBus,
        state_store: StateStore | None = None,
        agent_registry: AgentProfileRegistry | None = None,
        model_registry: ModelProfileRegistry | None = None,
        active_model_profile_id: str = "",
        apply_saved_model_on_restore: bool = True,
        workspace_mru_max_entries: int = DEFAULT_WORKSPACE_MRU_MAX_ENTRIES,
        editor_keymap: EditorMode | str = EditorMode.STANDARD,
        trajectory_verify_commands: str = DEFAULT_TRAJECTORY_VERIFY_COMMANDS,
        *,
        engine_provider: Callable[[], AgentEngine] | None,
        settings_handle: SettingsHandle | None = None,
        localization: TranscriptLocalizationPort | None = None,
        locale_controller: LocaleController | None = None,
        on_editor_keymap_changed: Callable[[str], None] | None = None,
        tool_groups_expanded: Callable[[], bool] | None = None,
        approval_mode: ApprovalMode = ApprovalMode.MANUAL,
    ) -> None:
        self._services = MainScreenServices(
            bus=event_bus,
            settings_handle=settings_handle,
            state_store=state_store,
            agent_registry=agent_registry,
            model_registry=model_registry,
            active_model_profile_id=active_model_profile_id,
            apply_saved_model_on_restore=apply_saved_model_on_restore,
            engine_provider=engine_provider,
            workspace_mru_max_entries=workspace_mru_max_entries,
        )
        self._state = MainScreenState()
        self._editor_mode = EditorMode.parse(editor_keymap)
        self._trajectory_verify_commands = trajectory_verify_commands
        self._on_editor_keymap_changed = on_editor_keymap_changed
        self._tool_groups_expanded = tool_groups_expanded or (lambda: True)
        self._localization = localization
        self._locale_controller = locale_controller
        self._gc_freeze_participants: tuple[GcFreezeParticipant, ...] = ()
        self.theme_editor: ThemeEditorPanel | None = None
        self._theme_editor_opening = False
        self._shell_requested_after_editor = False
        # Seeded with the engine's launch mode: the startup ApprovalModeUpdated
        # sync then confirms what the header already shows, instead of
        # flipping a placeholder and toasting a change nobody made.
        self._state.runtime.approval_mode = approval_mode
        self._settings_persistence_queue = self._new_settings_persistence()
        super().__init__()
        self.set_reactive(MainScreen.header_approval_mode, approval_mode)
        self._reactive_state_initialized = True

        # Live mutation tracking for /diff during agent runs.
        self._live_diff = LiveDiffTracker()

        # Handler instances
        self._view_adapter = MainScreenViewAdapter(
            self,
            state=self._state,
            state_store=self._services.state_store,
            locale_controller=self._locale_controller,
        )
        self._session_title = SessionTitleController(
            run=self._state.run,
            app=lambda: self.app,
            workspace_cwd=self._workspace_cwd,
            show_display_title=self._show_session_title,
            set_interval=self.set_interval,
            current_session_id=lambda: self.chat_session_id,
            push_screen=self._view_adapter.push_screen,
            start_custom_title_save=lambda title, session_id: self._start_worker(
                partial(self._sessions.apply_custom_session_title, title, session_id)
            ),
            session_dir=self._session_dir_for,
            locale_controller=self._locale_controller,
        )
        self._workspace_branch = WorkspaceBranchController(
            workspace=self._state.workspace,
            workspace_cwd=self._workspace_cwd,
            displayed_cwd=lambda: self.chat_workspace_cwd or self._workspace_cwd(),
            show_branch=self._show_workspace_branch,
            set_timer=self.set_timer,
            set_interval=self.set_interval,
            call_from_thread=lambda callback: self.app.call_from_thread(callback),  # noqa: PLW0108 — app bound later
        )
        self._runtime_info = RegistryRuntimeInfoProvider(self._services)
        self._config_actions = RuntimeConfigController(
            state=self._state,
            services=self._services,
            view=self._view_adapter,
            callbacks=RuntimeConfigCallbacks(
                start_worker=self._start_worker,
                set_profile_display=self._set_profile_display,
                update_subtitle=self._update_subtitle,
                debug=self._debug,
                notification_service=self._notification_service,
                settings_coordinator=self._settings_coordinator,
            ),
            profile_descriptions=self._runtime_info,
            locale_controller=self._locale_controller,
        )
        self._workspace_actions = WorkspaceController(
            state=self._state,
            services=self._services,
            view=self._view_adapter,
            callbacks=WorkspaceCallbacks(
                start_worker=self._start_worker,
                debug=self._debug,
                allow_change=lambda: self._workflow.browser.request_workspace_change(),  # noqa: PLW0108 — constructed later
                selected_cwd=lambda: self._workflow.project_cwd if self._workflow.workflow_mode else "",
                apply_workflow_cwd=self._apply_workflow_cwd,
                workflow_mode=lambda: self._workflow.workflow_mode,
            ),
            locale_controller=self._locale_controller,
        )
        self._copy_actions = CopyActionController(view=self._view_adapter, debug=self._debug)
        self._chat_selection = ChatSelectionController(self)
        self._diff_controller = self._new_diff_controller()
        self._rollback_controller = self._new_rollback_controller()
        self._navigation = self._new_navigation_controller()
        self._tool_actions = ToolActionBridge(publisher=self._services.bus, debug=self._debug)
        self._events = BackendEventHandler(
            state=self._state,
            services=self._services,
            view=self._view_adapter,
            callbacks=BackendEventCallbacks(
                accept_approval_update=self._accept_approval_update,
                route_session_error=self._route_session_error,
                prompt_missing_working_dir=self._workspace_actions.prompt_missing_working_dir,
                set_agent_running=self._set_agent_running,
                set_agent_loading=self._set_agent_loading,
                set_has_messages=self._set_has_messages,
                set_profile_display=self._set_profile_display,
                set_active_model_profile_id=self._set_active_model_profile_id,
                set_creating_new_session=self._set_creating_new_session,
                set_restoring_session=self._set_restoring_session,
                set_workspace_cwd=self._set_workspace_cwd,
                refresh_git_branch=self._workspace_branch.schedule_refresh,
                update_subtitle=self._update_subtitle,
                update_toc=self._update_toc,
                on_session_fork_error=lambda event, message, severity: self._sessions.on_session_fork_error(
                    event,
                    message=message,
                    severity=severity,
                ),
                on_session_clear_error=lambda event, message: self._sessions.on_session_clear_error(
                    event,
                    message=message,
                ),
                handle_approval_response=self._handle_approval_response,
                handle_ask_user_response=self._handle_ask_user_response,
                question_inline_preferred=self._question_inline_preferred,
                approval_defer_while_judging=self._approval_defer_while_judging,
                approval_timeout_seconds=self._approval_timeout_seconds,
                post_gc_message=self.post_message,
                debug=self._debug,
                refresh_model_indicator=self._refresh_model_indicator,
                refresh_notification_settings=self._refresh_notification_settings,
                refresh_trajectory_verify_commands=self._refresh_trajectory_verify_commands,
                settings_reloaded=self._on_settings_reloaded_for_panel,
            ),
            runtime_info=self._runtime_info,
            live_diff=self._live_diff,
            locale_controller=self._locale_controller,
        )
        self._sessions = SessionHandler(
            state=self._state,
            services=self._services,
            view=self._view_adapter,
            callbacks=SessionCallbacks(
                set_agent_loading=self._set_agent_loading,
                set_has_messages=self._set_has_messages,
                set_creating_new_session=self._set_creating_new_session,
                set_restoring_session=self._set_restoring_session,
                set_profile_display=self._set_profile_display,
                set_active_model_profile_id=self._set_active_model_profile_id,
                set_workspace_cwd=self._set_workspace_cwd,
                update_subtitle=self._update_subtitle,
                update_toc=self._update_toc,
                clear_suggestion_file_cache=self._clear_suggestion_file_cache,
                start_worker=self._start_worker,
                post_gc_message=self.post_message,
                debug=self._debug,
                refresh_model_indicator=self._refresh_model_indicator,
                choose_missing_working_dir=partial(self._workspace_actions.choose_replacement_dir, reason="restore"),
            ),
            agent_load=self._events,
            runtime_info=self._runtime_info,
            profile_descriptions=self._runtime_info,
            locale_controller=self._locale_controller,
        )
        self._slash_actions = self._new_slash_command_actions()
        self._suggestions = SuggestionHandler(
            state=self._state,
            services=self._services,
            view=self._view_adapter,
            command_actions=self._slash_actions,
            callbacks=SuggestionCallbacks(
                notify_warning=self._warn_slash_command,
                start_worker=self._start_worker,
                submit_user_text=self._submit_user_text,
                start_agent_profile_switch=self._config_actions.start_agent_profile_switch,
                start_model_profile_switch=self._switch_model_profile,
            ),
            buddy_view=self._view_adapter,
            locale_controller=self._locale_controller,
        )
        self._shell_mode_controller = ShellModeController(
            state=self._state,
            shell_view=self._view_adapter,
            focus_view=self._view_adapter,
            set_shell_mode_state=self._set_shell_mode_state,
            panel_focus_changed=self._sync_sidebar_tab_strip_focus,
            dismiss_suggestions=self._suggestions.dismiss_suggestions,
            debug=self._debug,
        )
        self._input_flow = InputFlowController(
            state=self._state,
            services=self._services,
            view=self._view_adapter,
            start_worker=self._start_worker,
            handle_agent_message=self._events.on_agent_message,
            handle_error=self._events.on_error,
            set_agent_running=self._set_agent_running,
            set_has_messages=self._set_has_messages,
            clear_workspace_marker=self._clear_workspace_marker_for_user_message,
            clear_pending_questions=self._events.clear_pending_questions,
            commit_profile_switch_marker=self._commit_profile_switch_marker,
            post_gc_message=self.post_message,
            debug=self._debug,
        )
        self._subscriptions = MainScreenSubscriptions(
            bus=self._services.bus,
            events=self._events,
            sessions=self._sessions,
            rollback_result_handler=self._rollback_controller.on_result,
            execution_handler=self._on_execution_changed,
        )
        self._workflow_panel = WorkflowPanel(locale_controller=self._locale_controller)
        self._workflow = WorkflowController(
            services=self._services,
            panel=self._workflow_panel,
            cwd=self._workspace_cwd,
            # Keep projection callbacks on MainScreen's pump. call_after_refresh
            # forwards to the active modal, which can close with a refresh still queued.
            schedule=self.call_later,
            sync_chrome=self._sync_workflow_chrome,
            locale_controller=self._locale_controller,
        )
        self._workflow_timer: Timer | None = None
        self._trajectory_binding_available = True
        self._execution_binding_busy = False
        self.chat_workspace_cwd = self._workspace_cwd()

    def _new_settings_persistence(self) -> SettingsPersistenceQueue:
        return SettingsPersistenceQueue(
            notify_failure=self._notify_settings_failure,
            notify_rejected=self._notify_settings_rejected,
            logger=logger,
            on_written=self._on_settings_written,
            save_delay_seconds=SETTINGS_SAVE_DELAY_SECONDS,
            flush_lock_timeout_seconds=SETTINGS_FLUSH_LOCK_TIMEOUT_SECONDS,
        )

    def _settings_persistence(self) -> SettingsPersistenceQueue:
        """The one debounced writer for every panel-owned setting, built on first use."""
        persistence = self.__dict__.get("_settings_persistence_queue")
        if not isinstance(persistence, SettingsPersistenceQueue):
            persistence = self._new_settings_persistence()
            self.__dict__["_settings_persistence_queue"] = persistence
        return persistence

    def _settings_coordinator(self) -> SettingsCoordinator:
        """Process-level state behind the Settings dialog, built on first use.

        Lazy for the same reason as the queue: most sessions never open the
        panel. The RESTART baseline it captures is the same whenever it is
        taken — those values are held in force for the life of the process.
        """
        coordinator = self.__dict__.get("_settings_coordinator_instance")
        if not isinstance(coordinator, SettingsCoordinator):
            coordinator = self._new_settings_coordinator()
            self.__dict__["_settings_coordinator_instance"] = coordinator
        return coordinator

    def _switch_locale_for_panel(self, requested_locale: str) -> None:
        """The panel's locale row goes through the same action as the picker and
        ``/language``: a bundle that fails to load is reported the same way, and
        the coordinator then reprojects the row from what is still in force."""
        self._navigation.set_language(requested_locale)

    def _apply_trajectory_verify_commands(self, value: str) -> None:
        app = cast("ChrysApp", self.app)
        app.settings_handle.override(trajectory_verify_commands=value)
        self._trajectory_verify_commands = value
        self.query_one(TrajectoryDashboard).set_verify_commands(value)

    def _apply_tool_groups_expanded(self, value: bool) -> None:
        """Persist the live choice; existing groups deliberately keep their state."""
        persist_tool_groups_expanded(value)
        cast("ChrysApp", self.app).settings_handle.override(tool_groups_expanded=value)

    def _apply_approval_defer_while_judging(self, value: bool) -> None:
        """Put the choice in force; requests already waiting keep the choice they arrived under."""
        cast("ChrysApp", self.app).settings_handle.override(approval_defer_while_judging=value)

    def _approval_timeout_seconds(self) -> int:
        return cast("ChrysApp", self.app).settings_handle.settings.approval_timeout_seconds

    def _approval_defer_while_judging(self) -> bool:
        return cast("ChrysApp", self.app).settings_handle.settings.approval_defer_while_judging

    def _refresh_trajectory_verify_commands(self) -> None:
        """Re-project the word list a reload only installed into the handle.

        Reads the effective value rather than routing through the panel apply:
        recording a reload as an override would credit it to the runtime layer
        and pin it over every later reload.
        """
        value = cast("ChrysApp", self.app).settings_handle.settings.trajectory_verify_commands
        if value == self._trajectory_verify_commands:
            return
        self._trajectory_verify_commands = value
        self.query_one(TrajectoryDashboard).set_verify_commands(value)

    def _new_settings_coordinator(self) -> SettingsCoordinator:
        app = cast("ChrysApp", self.app)

        return SettingsCoordinator(
            services=self._services,
            settings_handle=app.settings_handle,
            queue=self._settings_persistence(),
            view=self._view_adapter,
            locale_controller=self._locale_controller,
            callbacks=SettingsCoordinatorCallbacks(
                apply_theme=app.apply_theme_setting,
                switch_locale=self._switch_locale_for_panel,
                apply_trajectory_verify_commands=self._apply_trajectory_verify_commands,
                apply_tool_groups_expanded=self._apply_tool_groups_expanded,
                apply_approval_defer_while_judging=self._apply_approval_defer_while_judging,
                list_themes=lambda: sorted(app.available_themes),
                save_notifications=self._schedule_notification_settings_save,
                notification_service=self._notification_service,
                turn_lifecycle_task=self._services.turn_lifecycle_task,
                turn_in_progress=lambda: self._state.run.agent_running or self._services.execution_busy(),
            ),
        )

    def _existing_settings_coordinator(self) -> SettingsCoordinator | None:
        """The coordinator if the panel has ever been opened; never builds one.

        Every queue write originates from the panel, so a write outcome or a
        reload arriving before the coordinator exists has nothing to update.
        """
        coordinator = self.__dict__.get("_settings_coordinator_instance")
        return coordinator if isinstance(coordinator, SettingsCoordinator) else None

    def _on_settings_written(self, result: PersistResult) -> None:
        coordinator = self._existing_settings_coordinator()
        if coordinator is not None:
            coordinator.on_written(result)

    def _on_settings_reloaded_for_panel(self) -> None:
        coordinator = self._existing_settings_coordinator()
        if coordinator is not None:
            coordinator.on_reloaded()

    def _notify_settings_failure(self, exc: Exception) -> None:
        self._view_adapter.notify(str(exc), title=SETTINGS_TITLE.bind(), severity="error", timeout=5)
        coordinator = self._existing_settings_coordinator()
        if coordinator is not None:
            coordinator.on_write_failed()

    def _notify_settings_rejected(self, result: PersistResult) -> None:
        message = _SETTINGS_REJECTED.bind(keys=DisplaySequence(tuple(result.rejected)))
        self._view_adapter.notify(message, title=SETTINGS_TITLE.bind(), severity="error", timeout=5)
        coordinator = self._existing_settings_coordinator()
        if coordinator is not None:
            coordinator.on_write_failed()

    def _workspace_cwd(self) -> str:
        """Return the TUI-tracked workspace cwd."""
        return self._state.workspace_marker.current_cwd or safe_getcwd()

    def _session_dir_for(self, session_id: str) -> Path | None:
        """Return *session_id*'s folder in the session store, or ``None`` without a store."""
        store = self._services.state_store
        return store.session_dir(session_id) if session_id and store is not None else None

    def _notification_service(self) -> NotificationService:
        return cast("ChrysApp", self.app).notification_service

    def _refresh_notification_settings(self) -> None:
        cast("ChrysApp", self.app).refresh_notification_settings()

    def _set_active_model_profile_id(self, profile_id: str) -> None:
        self._services.active_model_profile_id = profile_id

    def _set_profile_display(self, profile: str) -> None:
        self._state.runtime.profile = profile
        self.chat_profile_name = profile

    def _show_session_title(self, title: str) -> None:
        # ``chat_session_title`` is data-bound to the ChatPanel border.
        self.chat_session_title = title

    def _set_workspace_cwd(self, cwd: str) -> None:
        self._state.workspace.current_cwd = cwd
        self._state.workspace_marker.current_cwd = cwd
        self._workspace_branch.configure(cwd)

    def _show_workspace_branch(self, branch: str) -> None:
        self.chat_workspace_branch = branch
        with contextlib.suppress(Exception):
            self.query_one(ChatPanel).set_workspace_branch(branch)

    def _set_creating_new_session(self, creating: bool) -> None:
        self._state.session.creating_new_session = creating
        if creating:
            self._session_title.clear_terminal_title_result()

    def _set_restoring_session(self, restoring: bool) -> None:
        self._state.session.restoring_session = restoring
        if restoring:
            self._session_title.clear_terminal_title_result()

    def _clear_suggestion_file_cache(self) -> None:
        self._suggestions.file_cache = None

    def _warn_slash_command(
        self,
        message: StatusMessage,
        title: StatusMessage = INVALID_COMMAND_TITLE_REF,
        timeout: float | None = 3,
    ) -> None:
        self._view_adapter.notify(message, title=title, severity="warning", timeout=timeout)

    def _language_localizer(self) -> Localizer:
        controller = self._locale_controller
        return Localizer(DEFAULT_LOCALE) if controller is None else controller.localizer

    def _has_selectable_model_profile(self) -> bool:
        """Return whether the registry offers at least one selectable model profile."""
        registry = self._services.model_registry
        return registry is not None and any(
            is_model_profile_selectable(profile) for profile in registry.list_profiles()
        )

    def _refresh_model_indicator(self) -> None:
        """Recompute the model tag from registry availability and confirmed runtime state."""
        details = self._state.runtime.details.model if self._state.runtime.details_confirmed else None
        state = compute_model_indicator_state(
            details,
            self._has_selectable_model_profile(),
            self._state.runtime.profile,
            self._language_localizer(),
            runtime_confirmed=self._state.runtime.details_confirmed,
        )
        self.query_one(StatusBar).set_model(state)

    def refresh_localization(self) -> None:
        """Recompute screen-owned localized chrome text in place."""
        self._refresh_model_indicator()
        if self._workflow.workflow_mode:
            self._workflow.request_refresh()

    def _available_languages(self) -> list[tuple[str, Content]]:
        localizer = self._language_localizer()
        return [
            (requested_locale, render_content(localizer, definition.bind()))
            for requested_locale, definition in LANGUAGE_OPTIONS
        ]

    def _current_language(self) -> str:
        controller = self._locale_controller
        return DEFAULT_LOCALE if controller is None else controller.requested_locale

    def _unknown_language_warning(self, requested_locale: str) -> str:
        return render_str(
            self._language_localizer(),
            LANGUAGE_UNKNOWN_LOCALE.bind(locale=requested_locale),
        )

    def _new_slash_command_actions(self) -> SlashCommandActions:
        return SlashCommandActions(
            list_themes=lambda: sorted(self.app.available_themes),
            get_theme=lambda: self.app.theme,
            apply_theme=self._view_adapter.apply_theme_setting,
            pick_theme=self.action_pick_theme,
            list_languages=self._available_languages,
            get_language=self._current_language,
            apply_language=self.action_set_language,
            pick_language=self.action_pick_language,
            render_unknown_language_warning=self._unknown_language_warning,
            debug_event=self._debug,
            new_session=self._create_new_session,
            clear_session=self._clear_current_session,
            quit_app=self.action_quit,
            resume_session=self._resume_last_session,
            fork_session=self._fork_current_session,
            browse_session_list=self.action_sessions,
            workflow_selection=self.action_workflow,
            open_guide=self._navigation.open_guide,
            edit_session_title=self._session_title.open_editor,
            apply_session_title=self._session_title.apply_custom_title,
            change_directory=self._workspace_actions.start_chdir,
            copy_conversation=self._copy_agent_responses,
            fold_tools=self._toggle_fold,
            open_diff=self.action_show_diff,
            open_rollback=self.action_show_rollback,
            get_approval_mode=lambda: self._state.runtime.approval_mode.value,
            change_approval_mode=self._config_actions.start_approval_mode_change,
            configure_model=self._open_model_config,
            configure_agent=self._open_agent_config,
            configure_agent_tab=self._open_agent_config_tab,
            show_runtime_details=self.action_runtime_details,
            configure_settings=self._open_settings,
            show_manual_pages=lambda pages, start_index: self._view_adapter.show_man_pages(
                pages,
                start_index=start_index,
            ),
            warn=self._warn_slash_command,
        )

    def _new_diff_controller(self) -> DiffController:
        return DiffController(
            services=self._services,
            live_diff=self._live_diff,
            view=self._view_adapter,
            workspace_cwd=self._workspace_cwd,
            is_agent_running=lambda: self._state.run.agent_running,
            run_generation=lambda: self._state.run.generation,
            session_generation=self._services.session_generation,
            turn_lifecycle_task=self._services.turn_lifecycle_task,
            turn_lifecycle_saved=self._services.was_turn_lifecycle_saved,
        )

    def _new_rollback_controller(self) -> RollbackController:
        def _reset_welcome_workspace_marker(cwd: str) -> None:
            self._state.workspace_marker.original_cwd = None
            self._set_workspace_cwd(cwd)

        return RollbackController(
            services=self._services,
            view=self._view_adapter,
            workspace_cwd=self._workspace_cwd,
            is_agent_busy=lambda: (
                self._state.run.agent_running or self._state.run.agent_loading or self._services.execution_busy()
            ),
            current_session_id=self._view_adapter.current_chat_session_id,
            session_generation=self._services.session_generation,
            turn_lifecycle_task=self._services.turn_lifecycle_task,
            profile_name=lambda: self._state.runtime.profile,
            reset_welcome_workspace_marker=_reset_welcome_workspace_marker,
            set_has_messages=self._set_has_messages,
            post_gc_message=self.post_message,
            debug=self._debug,
            locale_controller=self._locale_controller,
        )

    def _set_interrupt_confirm_active(self, active: bool) -> None:
        self._state.overlays.interrupt_confirm_active = active

    def _new_navigation_controller(self) -> MainNavigationController:
        async def _flush_notifications() -> None:
            await self._flush_settings_save()

        async def _delete_current_and_new(session_id: str) -> None:
            await self._delete_session_and_new(session_id)

        async def _restore_session(session_id: str | WorkflowSessionPick) -> None:
            await self._sessions.do_session_restore(session_id)

        def _cancel_pending_injection() -> bool:
            # Deferred attribute lookup: the navigation controller is built
            # before the input-flow controller during screen construction.
            return self._input_flow.cancel_pending_injection()

        def _dismiss_suggestions() -> bool:
            if not self._suggestions_active_for_bindings():
                return False
            self._suggestions.dismiss_suggestions()
            return True

        return MainNavigationController(
            services=self._services,
            view=self._view_adapter,
            is_agent_loading=lambda: self._state.run.agent_loading,
            is_agent_running=lambda: self._state.run.agent_running or self._services.execution_busy(),
            is_submit_pending=lambda: self._state.submit.active or bool(self._workflow.awaiting_engine),
            is_workflow_mode=lambda: self._workflow.workflow_mode,
            has_messages=lambda: self._state.run.has_messages,
            is_dashboard_visible=self._dashboard_visible,
            set_interrupt_confirm_active=lambda active: MainScreen._set_interrupt_confirm_active(self, active),
            publish_interrupt=lambda: MainScreen._publish_interrupt(self),
            dismiss_suggestions=_dismiss_suggestions,
            cancel_pending_injection=_cancel_pending_injection,
            delete_current_and_new=_delete_current_and_new,
            restore_session=_restore_session,
            flush_notifications=_flush_notifications,
            start_worker=self._start_worker,
            debug=self._debug,
            locale_controller=self._locale_controller,
        )

    def _start_worker(self, work: Callable[[], Awaitable[object]]) -> object:
        """Run *work* in a worker on this screen's event loop.

        As with ``@work``, the worker is named after its work, and the
        coroutine is made only when the worker starts, so a worker cancelled
        before it runs leaves nothing unawaited.
        """

        async def run() -> object:
            return await work()

        name, description = _describe_work(work)
        return self.run_worker(run, name=name, description=description, thread=False)

    def show_update_notice(self, notice: UpdateNotice | None) -> None:
        """Startup-facing facade: tell about a newer release on the welcome screen, or stop telling."""
        self.query_one(ChatPanel).set_update_notice(notice)

    def set_startup_agent_loading(self, active: bool) -> None:
        """Startup-facing facade for setting agent loading state."""
        self._set_agent_loading(active)

    def is_startup_agent_loading(self) -> bool:
        """Return whether startup still considers the agent loading."""
        return self._state.run.agent_loading

    def gc_freeze_block_reason(self) -> GcFreezeBlockReason | None:
        """Return the first active MainScreen or participant hard gate."""
        if self._state.run.agent_loading:
            return GcFreezeBlockReason.AGENT_LOADING
        if self._state.run.agent_running:
            return GcFreezeBlockReason.AGENT_RUNNING
        if scroll_gc_paused():
            return GcFreezeBlockReason.SCROLL_GC_PAUSED
        for participant in self._gc_freeze_participants:
            if (reason := participant.gc_freeze_block_reason()) is not None:
                return reason
        return None

    def prepare_for_gc_freeze(self) -> None:
        """Synchronously prepare every registered freeze participant."""
        for participant in self._gc_freeze_participants:
            participant.prepare_for_gc_freeze()
        prepare_textual_screen_for_gc(self)

    def after_gc_freeze(self) -> None:
        """Synchronously renew all caches, continuing past individual failures."""
        errors: list[Exception] = []
        try:
            after_textual_screen_gc_freeze(self)
        except Exception as error:
            errors.append(error)
        for participant in self._gc_freeze_participants:
            try:
                participant.after_gc_freeze()
            except Exception as error:
                errors.append(error)
        raise_gc_freeze_hook_errors("MainScreen GC-freeze renewal failed", errors)

    def abort_gc_freeze(self) -> None:
        """Best-effort restore every cache after an incomplete prepare/after pass."""
        abort_textual_screen_gc_freeze(self)
        for participant in self._gc_freeze_participants:
            try:
                participant.abort_gc_freeze()
            except Exception:
                logger.exception("Failed to restore GC-freeze participant %s", type(participant).__name__)

    async def restore_startup_session(self, session_id: str) -> StartupRestore:
        """Restore at startup and confirm the matching backend success event."""
        restored = False

        async def observe_restored(event: SessionRestored) -> None:
            nonlocal restored
            if event.session_id == session_id:
                restored = True

        await self._services.bus.subscribe(SessionRestored, observe_restored)
        try:
            request = await self._sessions.do_session_restore(session_id, allow_while_loading=True)
        finally:
            await self._services.bus.unsubscribe(SessionRestored, observe_restored)
        if restored:
            return "restored"
        return "declined" if request is RestoreRequest.DECLINED else "failed"

    def cancel_startup_session_restore(self) -> None:
        """Close restore loading UI before falling back to a fresh runtime."""
        self._events.cancel_agent_load()
        self._set_restoring_session(False)

    async def dismiss_startup_load_dialog_before_restore(self) -> None:
        """Dismiss the startup load modal before opening restore-specific loading UI."""
        from chrys.app.tui.screens.dialogs.agent_load import AgentLoadDialog

        if self._events._agent_load_dialog is not None:
            with contextlib.suppress(Exception):
                self._events.cancel_agent_load()
        startup_modals: list[AgentLoadDialog] = []
        with contextlib.suppress(Exception):
            startup_modals = [modal for modal in list(self.app.screen_stack) if isinstance(modal, AgentLoadDialog)]
        for modal in startup_modals:
            with contextlib.suppress(Exception):
                modal.request_dismiss()
        await asyncio.sleep(0)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Conditionally show/hide bindings."""
        if action == "toggle_trajectory_dashboard" and not self._navigation.can_toggle_trajectory_dashboard():
            return False
        suggestions_active = (
            self._suggestions_active_for_bindings()
            if action in {"chat_page_up", "chat_page_down", "chat_scroll_bottom"}
            else False
        )
        return check_main_action(
            action,
            fullscreen_terminal=self._state.shell.fullscreen_terminal,
            shell_mode=self._state.shell.active,
            chat_foreground=self._chat_foreground(),
            suggestions_active=suggestions_active,
            agent_running=self._state.run.agent_running or self._services.execution_busy(),
        )

    def _dashboard_visible(self) -> bool:
        return self.query_one(TrajectoryDashboard).foreground

    def _chat_foreground(self) -> bool:
        return not self._workflow.workflow_mode and not self._dashboard_visible()

    def _suggestions_active_for_bindings(self) -> bool:
        """Return whether the input suggestion popup is active."""
        with contextlib.suppress(Exception):
            return bool(self.query_one(InputBar).suggestions_active)
        return False

    def compose(self) -> ComposeResult:
        yield AppHeader(locale_controller=self._locale_controller, show_mode_badge=True).data_bind(
            subtitle_parts=MainScreen.header_subtitle_parts,
            approval_mode=MainScreen.header_approval_mode,
        )
        with Horizontal():
            yield ChatPanel(
                cwd=self._workspace_cwd(),
                localization=self._localization,
                locale_controller=self._locale_controller,
                tool_groups_expanded=self._tool_groups_expanded,
                sub_agent_transcript_loader=self._load_sub_agent_transcript,
            ).data_bind(
                agent_running=MainScreen.agent_running_state,
                profile_name=MainScreen.chat_profile_name,
                session_id_value=MainScreen.chat_session_id,
                session_title_value=MainScreen.chat_session_title,
                workspace_cwd=MainScreen.chat_workspace_cwd,
                workspace_branch=MainScreen.chat_workspace_branch,
            )
            yield self._workflow_panel
            yield TrajectoryDashboard(
                locale_controller=self._locale_controller,
                verify_commands=self._trajectory_verify_commands,
            )
            yield ShellPanel(working_directory=self._workspace_cwd())
            yield SidebarPanel(locale_controller=self._locale_controller).data_bind(
                context_usage_state=MainScreen.context_usage_state,
                todo_state=MainScreen.todo_state,
            )
        yield StatusBar(locale_controller=self._locale_controller).data_bind(
            agent_running=MainScreen.agent_running_state,
            agent_loading=MainScreen.agent_loading_state,
            shell_mode=MainScreen.shell_mode_state,
        )
        # Anchor the screen overlay at InputBar: its upward offset keeps the
        # popup adjacent to input and paints over the reserved status row
        # without changing transcript geometry while suggestions are filtered.
        yield SuggestionList(locale_controller=self._locale_controller)
        yield InputBar(locale_controller=self._locale_controller).data_bind(
            agent_running=MainScreen.agent_running_state,
            agent_loading=MainScreen.agent_loading_state,
            has_messages=MainScreen.has_messages_state,
            shell_mode=MainScreen.shell_mode_state,
        )
        yield ChrysFooter(locale_controller=self._locale_controller)

    async def _load_sub_agent_transcript(self, log_file: str) -> PersistedSubAgentTranscript | None:
        """Load a transcript relative to the currently displayed session."""
        session_id = self.chat_session_id
        if self._services.state_store is None or not session_id:
            return None
        return await load_persisted_sub_agent_transcript(self._services.state_store.session_dir(session_id), log_file)

    def sync_footer_bindings(self) -> None:
        """Refresh visible Footer content only when its signature changed."""
        self.query_one(ChrysFooter).sync_bindings()

    def on_screen_resume(self, _event: ScreenResume) -> None:
        """Recover deferred workflow and Footer updates after an overlay closes."""
        if self._workflow.workflow_mode:
            self._workflow.shown()
        self._sync_workflow_timer()
        self.query_one(InputBar).sync_deferred_button_geometry()
        self.sync_footer_bindings()
        self.query_one(SuggestionList).resume_marquee()
        if self.theme_editor is not None:
            self.theme_editor.resume()
        self._resume_shell_request()

    def on_resize(self, event: Resize) -> None:
        self._set_theme_editor_compact(self.theme_editor is not None and event.size.width < 128)

    def _set_theme_editor_compact(self, compact: bool) -> None:
        if self.has_class("--theme-editor-compact") == compact:
            return
        sidebar = self.query_one(SidebarPanel)
        sidebar.suppress_visibility("theme-editor", compact)
        # Only these panel roots and the editor use the compact selectors.
        # Updating MainScreen recursively would restyle the whole transcript.
        self.update_classes({"--theme-editor-compact": compact}, update=False)
        self.app.stylesheet.update_nodes((self.query_one(ChatPanel),))
        if self.theme_editor is not None and self.theme_editor.is_mounted:
            self.theme_editor.update_node_styles(animate=False)

    def on_screen_suspend(self, _event: ScreenSuspend) -> None:
        """Stop suggestion animation while an overlay covers this screen."""
        self.query_one(SuggestionList).pause_marquee()
        if self._workflow_timer is not None:
            self._workflow_timer.stop()
            self._workflow_timer = None

    def _update_subtitle(self) -> None:
        """Update header to reflect platform info."""
        platform_label = get_platform().display_label
        self.header_subtitle_parts = (platform_label,) if platform_label else ()

    def watch_agent_running_state(self, running: bool) -> None:
        self._apply_agent_running_state(running)

    def watch_agent_loading_state(self, loading: bool) -> None:
        self._apply_agent_loading_state(loading)

    def watch_has_messages_state(self, has_messages: bool) -> None:
        self._state.run.has_messages = has_messages

    def watch_header_approval_mode(self, mode: ApprovalMode) -> None:
        self._state.runtime.approval_mode = mode

    def watch_chat_workspace_cwd(self, old_cwd: str, cwd: str) -> None:
        self._workspace_branch.displayed_cwd_changed(old_cwd, cwd)

    def watch_shell_mode_state(self, active: bool) -> None:
        self._shell_mode_controller.apply(active)

    def _reactive_state_ready(self) -> bool:
        return isinstance(self, MainScreen) and self._reactive_state_initialized

    def get_widget_and_offset_at(self, x: int, y: int) -> tuple[Widget | None, Offset | None]:
        """Return no selectable target for tool-renderer UI.

        Textual uses this method to start and update text selection. Tool
        renderers are compact status UI in the chat transcript; their details
        will get a separate copy path when expanded details mode lands.
        """
        widget, offset = super().get_widget_and_offset_at(x, y)
        if widget is not None and is_tool_copy_excluded(widget):
            return None, None
        return widget, offset

    def _watch__select_state(self, select_state: SelectState | None) -> None:
        """Route chat drags through the O(visible) logical selection model.

        Textual's generic handler re-walks and re-sorts every widget under the
        selection container on each mouse move — O(total transcript widgets)
        in a long chat. Drags whose both endpoints sit in the ChatPanel go
        through :class:`ChatSelectionController` instead; anything else
        (single-widget selections, non-chat surfaces) keeps stock behavior.
        """
        if select_state is None or select_state.end is None:
            if select_state is None:
                self._chat_selection.clear()
            super()._watch__select_state(select_state)
            return
        if not select_state.is_attached_to_dom() or select_state.is_single_content_widget:
            self._chat_selection.clear()
            super()._watch__select_state(select_state)
            return
        if self._chat_selection.handle_select_state(select_state):
            return
        self._chat_selection.clear()
        super()._watch__select_state(select_state)

    async def _watch_selections(
        self,
        old_selections: dict[Widget, Selection],
        selections: dict[Widget, Selection],
    ) -> None:
        """Notify only widgets whose selection actually changed.

        Textual notifies the union of old and new maps unconditionally.
        ``VirtualizedMarkdown.selection_updated`` drops its rendered-line and
        copy-text caches, so blanket notification re-renders every selected
        transcript widget on every mouse move of a drag.
        """
        for widget in old_selections.keys() | selections.keys():
            new_selection = selections.get(widget)
            if old_selections.get(widget) != new_selection:
                widget.selection_updated(new_selection)

    def clear_selection(self) -> None:
        """Clear the chat logical selection along with screen selection state."""
        self._chat_selection.clear()
        super().clear_selection()

    def schedule_chat_selection_reprojection(self, panel: ChatPanel) -> None:
        """Keep chat selection highlights aligned after the transcript scrolls."""
        self._chat_selection.schedule_reproject(panel)

    def _compositor_refresh(self) -> None:
        """Forward each repaint to the dialog above this screen once.

        Every ``ModalScreen`` lets this screen show through (``chrys.tcss``),
        so a dialog paints this screen's frame as its backdrop and must receive
        every region this screen repaints while covered. Textual's background
        branch forwards those regions, but it also marks them dirty again on
        this screen and sets ``_repaint_required``. The next update then swaps
        the dirty widgets for this screen alone and forwards the old regions
        once more, together with any new damage, and marks the union again.
        Whatever was forwarded once is forwarded on every later update: after
        the first layout, scroll or status change under the dialog, every
        spinner tick makes the dialog re-render and re-blend the whole backdrop.

        Forward only the regions this update repainted. The re-marking keeps
        nothing fresh: the dialog repaints a forwarded region from this
        screen's current compositor output, and Textual repaints this screen's
        whole frame when the dialog closes (``Screen._on_screen_resume`` and
        ``App._replace_screen``). Underlay animation stays visible because
        every tick still reaches the dialog.

        This is a version-gated fork of the non-inline background branch of
        Textual's private ``Screen._compositor_refresh``. The only intentional
        semantic delta is dropping the re-marking; the top-screen and inline
        branches, and other Textual releases, take the stock path.
        """
        app = self.app
        dirty_regions = self._compositor._dirty_regions
        if (
            TEXTUAL_VERSION != TEXTUAL_BACKGROUND_REFRESH_FORK_VERSION
            or app.is_inline
            or not dirty_regions
            or self is app.screen
            or self not in app._background_screens
        ):
            super()._compositor_refresh()
            return
        app.screen.refresh(*dirty_regions)
        dirty_regions.clear()
        self._dirty_widgets.clear()
        app._update_mouse_over(self)

    def _screen_resized(self, size: Size) -> None:
        """Ignore unchanged resize broadcasts while this screen is under an overlay."""
        if size == self._size:
            return
        super()._screen_resized(size)

    def _refresh_layout(self, size: Size | None = None, scroll: bool = False) -> None:
        if self._theme_editor_opening:
            # App.refresh_css calls this directly even inside batch_update.
            # Mount the complete dock before measuring the resized transcript.
            self.refresh(layout=True)
            return
        super()._refresh_layout(size, scroll)
        # Now — and only now — the compositor's visible map matches the new
        # scroll offset, so a pending chat-highlight re-projection is safe.
        self._chat_selection.on_screen_layout_refreshed()
        # Likewise a workflow panel shown before this layout placed it.
        self._workflow.screen_layout_refreshed()

    async def on_mount(self) -> None:
        """Subscribe to backend events when the screen mounts."""
        chat_panel = self.query_one(ChatPanel)
        chat_panel.set_replay_progress_callback(self._events.update_session_history_progress)
        self._gc_freeze_participants = (
            chat_panel,
            self.query_one(TrajectoryDashboard),
            self.query_one(SessionJsonPanel),
            self.query_one(ShellPanel),
            self._suggestions,
        )
        self._suggestions.build_slash_commands()

        await self._subscriptions.subscribe_all()
        await self._workflow.subscribe()
        self._sync_execution_chrome()
        self._sync_workflow_timer()
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)
        self._refresh_model_indicator()
        self._update_subtitle()
        self._workspace_branch.start(self._workspace_cwd())

    async def on_unmount(self) -> None:
        """Flush pending UI-owned settings before the screen is torn down."""
        self._events.close_approval_waits()
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)
        if self._workflow_timer is not None:
            self._workflow_timer.stop()
        await self._workflow.close()
        try:
            await self._subscriptions.unsubscribe_all()
        finally:
            self._session_title.stop_activity()
            await self._workspace_branch.close()
            try:
                await self._suggestions.buddy_command.shutdown()
            finally:
                await self._flush_settings_save()

    # ------------------------------------------------------------------ #
    # Focus guard — keep input bar focused
    # ------------------------------------------------------------------ #

    def on_descendant_focus(self, event: DescendantFocus) -> None:
        """Redirect focus back to the input bar when it lands on a display-only panel."""
        if self._workflow.workflow_mode:
            return
        self._shell_mode_controller.on_descendant_focus(event.widget)

    def on_paste(self, event: Paste) -> None:
        """Route image path drops/pastes into the chat input when it is not focused."""
        if self._workflow.workflow_mode:
            return
        self._shell_mode_controller.on_paste(event)

    # ------------------------------------------------------------------ #
    # User input
    # ------------------------------------------------------------------ #

    @on(InputBar.UserSubmitted)
    def _on_user_input(self, event: InputBar.UserSubmitted) -> None:
        """User pressed Enter — handle slash commands or publish message."""
        if self._workflow.workflow_mode:
            return
        text = event.text.strip()
        if not text:
            return

        # Dispatch slash commands
        if is_slash_command_candidate(text):
            if self._state.run.agent_loading:
                return
            if self._suggestions.dispatch_slash_command(text):
                return

        if self._state.run.agent_loading:
            return
        self._submit_user_text(text)

    @on(InputBar.LockedChanged)
    def _on_input_locked_changed(self, event: InputBar.LockedChanged) -> None:
        """Keep status-bar selector guards synchronized with input locking."""
        self.query_one(StatusBar).set_input_locked(event.locked)

    def _model_unconfigured(self) -> bool:
        """Return whether a chat submit would run with no usable model.

        Confirmed runtime details are authoritative when they name a model:
        the resolver's built-in fallback loads fine but carries the
        non-functional placeholder model id, while a real model id proves
        the session can serve. A blank confirmed id carries no information
        (event sources may omit runtime details), so it falls back to the
        registry's selectable profiles — as does the pre-confirmation state.
        Screens constructed without a registry keep the legacy submit path.
        """
        registry = self._services.model_registry
        if registry is None:
            return False
        if self._state.runtime.details_confirmed:
            model_id = self._state.runtime.details.model.model_id
            if model_id == UNCONFIGURED_MODEL_ID:
                return True
            if model_id.strip():
                return False
        return not self._has_selectable_model_profile()

    def _show_model_unconfigured_dialog(self) -> None:
        """Explain the blocked submit and offer the model tag's action."""
        from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

        def _on_result(confirmed: bool | None) -> None:
            if confirmed:
                self._config_actions.on_model_tag_clicked(
                    "select" if self._has_selectable_model_profile() else "configure"
                )
            else:
                # Dismissal must hand the keyboard back to the composer.
                self.query_one(InputBar).focus_input()

        self.app.push_screen(
            ConfirmDialog(
                title=_MODEL_UNCONFIGURED_TITLE.bind(),
                message=_MODEL_UNCONFIGURED_MESSAGE.bind(),
                confirm_label=_MODEL_UNCONFIGURED_SETUP.bind(),
                locale_controller=self._locale_controller,
            ),
            _on_result,
        )

    def _submit_user_text(self, text: str) -> None:
        """Submit normal user text as a new turn or mid-run injection.

        Sole chokepoint for chat submissions — composer submits and
        suggestion-originated ones (runtime skills, unmatched fallback
        text) alike — so the unconfigured-model guard cannot be bypassed.
        """
        if self._workflow.workflow_mode:
            return
        if self._model_unconfigured():
            # Every submit path clears the composer before reaching here; hand
            # the text back so the blocked message is not lost. The hand-back
            # travels as a message, so a draft typed in the meantime wins.
            self.query_one(InputBar).restore_draft(text)
            self._show_model_unconfigured_dialog()
            return
        self._input_flow.submit_user_text(text)

    def action_chat_page_up(self) -> None:
        """Scroll the transcript up one page, even while the input is focused."""
        panel = self.query_one(ChatPanel)
        panel.note_user_scroll()
        panel.scroll_page_up(animate=False)

    def action_chat_page_down(self) -> None:
        """Scroll the transcript down one page, even while the input is focused."""
        panel = self.query_one(ChatPanel)
        panel.note_user_scroll()
        panel.scroll_page_down(animate=False)

    def action_chat_scroll_bottom(self) -> None:
        """Scroll the transcript to the bottom, even while the input is focused."""
        self.query_one(ChatPanel).jump_to_bottom()

    # ------------------------------------------------------------------ #
    # Message editor
    # ------------------------------------------------------------------ #

    @on(InputBar.EditorRequested)
    def _on_editor_requested(self, _event: InputBar.EditorRequested) -> None:
        if isinstance(self.app.screen, EditorDialog):
            return
        input_bar = self.query_one(InputBar)
        snapshot = input_bar.snapshot_draft()
        if len(snapshot.text) > MESSAGE_EDITOR_MAX_CHARACTERS:
            self._view_adapter.notify(
                _EDITOR_DRAFT_TOO_LARGE.bind(limit=f"{MESSAGE_EDITOR_MAX_CHARACTERS:,}"),
                title=_EDITOR_DRAFT_TOO_LARGE_TITLE.bind(),
                severity="warning",
            )
            return
        dialog = EditorDialog(
            EditorBufferSnapshot(snapshot.text, snapshot.cursor_location, snapshot.revision),
            mode=self._editor_mode,
            can_commit=input_bar.can_commit_editor_draft,
            current_draft_revision=lambda: input_bar.draft_revision,
        )
        self._view_adapter.push_screen(dialog, self._on_editor_result)

    def _on_editor_result(self, result: EditorDialogResult) -> None:
        if result.mode is not self._editor_mode:
            self._editor_mode = result.mode
            persist_editor_keymap(result.mode.value)
            # The same write-point contract as theme and locale: the shared
            # settings handle records the choice as a RUNTIME override, so
            # settings keep describing the screen even when the swallowed
            # disk write above failed.
            if self._on_editor_keymap_changed is not None:
                self._on_editor_keymap_changed(result.mode.value)
        if not result.accepted:
            return
        input_bar = self.query_one(InputBar)
        input_bar.replace_draft(result.text)
        input_bar.focus_input()

    # ------------------------------------------------------------------ #
    # Shell mode
    # ------------------------------------------------------------------ #

    @on(InputBar.ShellModeRequested)
    def _on_shell_mode_requested(self, _event: InputBar.ShellModeRequested) -> None:
        if self.theme_editor is not None:
            self.theme_editor.request_close(after_close=self._enter_shell_after_editor)
        else:
            self.shell_mode_state = True

    def _enter_shell_after_editor(self) -> None:
        self._shell_requested_after_editor = True
        self._resume_shell_request()

    def _resume_shell_request(self) -> None:
        # A dialog may have opened while the dock was unmounting. Wait for
        # MainScreen to resume before changing the terminal layout or focus.
        if self._shell_requested_after_editor and self.is_mounted and self.app.screen is self:
            self._shell_requested_after_editor = False
            if self.theme_editor is None:
                self.shell_mode_state = True

    def _set_shell_mode_state(self, active: bool) -> None:
        self.shell_mode_state = active

    def _sync_sidebar_tab_strip_focus(self) -> None:
        """Make the sidebar tab strip focusable exactly where sidebar focus is not handed back.

        Asks what ``on_descendant_focus`` asks: workflow mode returns before the shell
        controller, whose ``keeps_panel_focus`` covers shell mode and the fullscreen terminal.
        Focus itself moves only with the view switch that follows each mode change
        (``exit_shell_mode`` returns it to a visible input).
        """
        self.query_one(SidebarPanel).set_tab_strip_focusable(
            self._workflow.workflow_mode or self._shell_mode_controller.keeps_panel_focus()
        )

    @on(Terminal.EscapeExited)
    def _on_terminal_escape_exited(self, _event: Terminal.EscapeExited) -> None:
        """User double-tapped Escape in terminal — exit shell mode."""
        self._shell_mode_controller.exit_on_escape()

    @on(ShellPanel.CommandExecuted)
    def _on_shell_command_executed(self, event: ShellPanel.CommandExecuted) -> None:
        """Shell command recorded via shell integration hooks."""
        event.stop()
        self._shell_mode_controller.command_executed(event.command)

    @on(Terminal.AlternateScreenChanged)
    def _on_alternate_screen_changed(self, event: Terminal.AlternateScreenChanged) -> None:
        """When interactive app (vim/htop) activates, enter fullscreen terminal mode."""
        event.stop()
        self._shell_mode_controller.set_alternate_screen_active(event.enabled)

    @on(ShellPanel.Exited)
    def _on_shell_exited(self, _event: ShellPanel.Exited) -> None:
        """Shell process exited — exit shell mode."""
        self._shell_mode_controller.exit_on_shell_closed()

    # ------------------------------------------------------------------ #
    # Suggestion list (/ commands, @ files, # agents, $ models)
    # ------------------------------------------------------------------ #

    @on(InputBar.SlashTriggered)
    def _on_slash_triggered(self, _event: InputBar.SlashTriggered) -> None:
        self._suggestions.on_slash_triggered()

    @on(InputBar.FileTriggered)
    def _on_file_triggered(self, _event: InputBar.FileTriggered) -> None:
        self._suggestions.on_file_triggered()

    @on(InputBar.AgentTriggered)
    def _on_agent_triggered(self, _event: InputBar.AgentTriggered) -> None:
        self._suggestions.on_agent_triggered()

    @on(InputBar.ModelTriggered)
    def _on_model_triggered(self, _event: InputBar.ModelTriggered) -> None:
        self._suggestions.on_model_triggered()

    def action_prompt_history(self) -> None:
        """Open the cross-session prompt-history suggestion list."""
        if self._state.shell.fullscreen_terminal or self._state.shell.active or not self._chat_foreground():
            return
        input_bar = self.query_one(InputBar)
        if input_bar.locked:
            return
        input_bar.focus_input()
        revision = self._suggestions.start_prompt_history()
        self._show_prompt_history(revision)

    @work(thread=False)
    async def _show_prompt_history(self, revision: int) -> None:
        await self._suggestions.show_prompt_history_async(revision=revision)

    @on(InputBar.TextChanged)
    def _on_text_changed_for_suggestions(self, event: InputBar.TextChanged) -> None:
        self._suggestions.on_text_changed(event.text)

    @on(InputBar.SuggestionNavigate)
    def _on_suggestion_navigate(self, event: InputBar.SuggestionNavigate) -> None:
        self._suggestions.on_suggestion_navigate(event.direction)

    @on(InputBar.SuggestionSelect)
    def _on_suggestion_select(self, event: InputBar.SuggestionSelect) -> None:
        self._suggestions.on_suggestion_select(event.execute)

    @on(SuggestionList.Selected)
    def _on_suggestion_selected(self, event: SuggestionList.Selected) -> None:
        self._suggestions.on_suggestion_selected(event.mode, event.text, event.execute, event.kind)

    @on(InputBar.SuggestionDismiss)
    @on(SuggestionList.Dismissed)
    def _on_suggestion_dismissed(self, _event: InputBar.SuggestionDismiss | SuggestionList.Dismissed) -> None:
        self._suggestions.dismiss_suggestions()

    # ------------------------------------------------------------------ #
    # Input bar action buttons
    # ------------------------------------------------------------------ #

    @on(InputBar.InterruptRequested)
    def _on_interrupt_requested(self, _event: InputBar.InterruptRequested) -> None:
        """Stop button clicked — interrupt the running agent."""
        self.action_interrupt()

    @on(InputBar.RetryRequested)
    def _on_retry_requested(self, event: InputBar.RetryRequested) -> None:
        """Retry button clicked — retry from current state.

        ``event.text`` carries any note the user typed before clicking
        Continue/Retry.  When non-empty, the engine uses it as the
        mid-turn continuation prompt.
        """
        if self._workflow.workflow_mode:
            return
        if self._model_unconfigured():
            # A retry re-runs the turn against the model just like a fresh
            # submit. The composer already consumed the typed note, so hand
            # it back — unless a draft landed in the meantime, which wins; the
            # inline status action stays visible for after the user configures
            # a model.
            self.query_one(InputBar).restore_draft(event.text)
            self._show_model_unconfigured_dialog()
            return
        self._input_flow.request_retry(event.text)

    @on(ConversationStatusAction.Pressed)
    def _on_conversation_status_action_pressed(self, event: ConversationStatusAction.Pressed) -> None:
        """Retry/continue button clicked from an error or interrupted status.

        Resumes without a continuation prompt: any draft typed in the input
        bar stays there.  Sending the draft as the mid-turn prompt is the
        input bar's Continue button (``InputBar.RetryRequested``).
        """
        event.stop()
        input_bar = self.query_one(InputBar)
        if self._state.run.agent_loading or not input_bar.retry_mode:
            return
        if self._model_unconfigured():
            self._show_model_unconfigured_dialog()
            return
        self._input_flow.request_retry("")

    @on(ToolViewRequested)
    def _on_tool_view_requested(self, event: ToolViewRequested) -> None:
        """Open a detailed tool-view modal requested by a tool widget."""
        from chrys.app.tui.screens.dialogs.tool_view import ToolDetailModal

        event.stop()
        self.app.push_screen(ToolDetailModal.from_request(event))

    @on(InputBar.NewSessionRequested)
    def _on_new_session_requested(self, _event: InputBar.NewSessionRequested) -> None:
        """New button clicked — start a fresh session."""
        if self._state.run.agent_running or self._state.run.agent_loading or self._services.execution_busy():
            return
        self._create_new_session()

    @on(StatusBar.DetailsClicked)
    def _on_status_details_clicked(self, _event: StatusBar.DetailsClicked) -> None:
        """Open the active runtime details modal."""
        self.action_runtime_details()

    @work(thread=False)
    async def _create_new_session(self) -> None:
        if self._state.run.agent_loading:
            return
        self._session_title.set_terminal_title_for_cwd()
        await self._sessions.create_new_session()

    @work(thread=False)
    async def _resume_last_session(self) -> None:
        if self._state.run.agent_loading:
            return
        await self._sessions.resume_last_session()

    @work(thread=False)
    async def _fork_current_session(self) -> None:
        await self._sessions.fork_current_session()

    def _clear_workspace_marker_for_user_message(self) -> None:
        self._state.workspace_marker.original_cwd = None

    def _commit_profile_switch_marker(self) -> None:
        # Once messages are exchanged, the switch indicator becomes permanent.
        # The next switch will use a new unique key, so the old widget stays
        # untouched in the chat.
        if self._state.profile_marker.from_profile is not None:
            self._state.profile_marker.from_profile = None
            self._state.profile_marker.to_profile = None

    # ------------------------------------------------------------------ #
    # Header and status-bar tags, model switching ($)
    # ------------------------------------------------------------------ #

    @on(AppHeader.ApprovalBadgeClicked)
    def _on_approval_badge_clicked(self, _event: AppHeader.ApprovalBadgeClicked) -> None:
        """Open the approval mode picker modal."""
        self._config_actions.on_approval_badge_clicked()

    @on(StatusBar.ProfileTagClicked)
    def _on_profile_tag_clicked(self, _event: StatusBar.ProfileTagClicked) -> None:
        """Open the agent picker modal."""
        self._config_actions.on_profile_tag_clicked()

    @on(StatusBar.ModelTagClicked)
    def _on_model_tag_clicked(self, event: StatusBar.ModelTagClicked) -> None:
        """Route the model tag's semantic action."""
        if not self._workflow.workflow_mode:
            self._config_actions.on_model_tag_clicked(event.mode)

    @on(StatusBar.SelectorBusy)
    def _on_selector_busy(self, event: StatusBar.SelectorBusy) -> None:
        """Explain why a selector ignored a click during a run."""
        if not self._workflow.workflow_mode:
            self._config_actions.on_selector_busy(event.selector)

    @work(thread=False)
    async def _switch_model_profile(self, profile_id: str) -> None:
        """Commit a Chat model suggestion only while its owner remains active."""
        if self._workflow.workflow_mode or not self._model_selection_is_committable(profile_id):
            return
        await self._config_actions.on_model_picked(profile_id)

    def _model_selection_is_committable(self, profile_id: str) -> bool:
        """Re-check every guard the displayed suggestion rows were built on.

        The suggestion popup outlives a pushed screen, so between the rows
        being built and one being chosen F4 can retire the profile and F2 can
        hand model ownership to an agent. ``activate_model_profile`` persists
        whatever id it is handed, so the last word has to be taken here rather
        than trusted from a row that may have gone stale on screen.
        """
        if self._state.run.agent_running or self._state.run.agent_loading or self._services.execution_busy():
            return False
        if is_model_selection_locked(
            self._state.runtime.details.model,
            runtime_confirmed=self._state.runtime.details_confirmed,
        ):
            return False
        registry = self._services.model_registry
        profile = None if registry is None else registry.get(profile_id)
        return profile is not None and is_model_profile_selectable(profile)

    # ------------------------------------------------------------------ #
    # Model config (/model)
    # ------------------------------------------------------------------ #

    def _open_model_config(self) -> None:
        """Open the model configuration modal."""
        self._config_actions.open_model_config()

    # ------------------------------------------------------------------ #
    # Agent config (/agents with optional tab subcommands)
    # ------------------------------------------------------------------ #

    def action_agents_config(self) -> None:
        """Open the agent configuration modal (F2)."""
        self._open_agent_config()

    def action_models_config(self) -> None:
        """Open the model configuration modal (F4)."""
        self._open_model_config()

    def action_settings(self) -> None:
        """Open the Settings dialog (F10)."""
        self._open_settings()

    def action_runtime_details(self) -> None:
        """Open the active runtime details modal."""
        self._config_actions.open_runtime_details()

    def _open_settings(self, initial_tab: str = GENERAL_TAB_ID) -> None:
        """Open the Settings dialog at *initial_tab*."""
        self._config_actions.open_settings(initial_tab)

    def _schedule_notification_settings_save(self, settings: NotificationSettings) -> None:
        """Record the live choice, then persist it after a short debounce."""
        cast("ChrysApp", self.app).override_notification_settings(settings)
        self._settings_persistence().schedule(settings.to_settings_patch())

    async def _flush_settings_save(self) -> None:
        """Force any debounced settings write to complete now."""
        await self._settings_persistence().flush()

    def _open_agent_config(self) -> None:
        """Open the unified agent configuration modal."""
        self._config_actions.open_agent_config()

    def _open_agent_config_tab(self, tab: str) -> None:
        """Open the agent configuration modal at a specific tab."""
        self._config_actions.open_agent_config_tab(tab)

    @on(ChatPanel.TitleClicked)
    def _on_chat_panel_title_clicked(self, _event: ChatPanel.TitleClicked) -> None:
        """Open the session title editor when the user clicks the border title."""
        self._session_title.open_editor()

    @on(WorkflowPanel.WorkingDirClicked)
    @on(ChatPanel.WorkingDirClicked)
    def _on_chat_panel_working_dir_clicked(
        self, _event: ChatPanel.WorkingDirClicked | WorkflowPanel.WorkingDirClicked
    ) -> None:
        """Open the file dialog when the user clicks the working directory subtitle."""
        self._workspace_actions.open_working_dir_picker()

    def _copy_agent_responses(self, arg: str) -> None:
        """Handle /copy slash command — copy agent, user, or full-conversation turns.

        Supported forms are ``/copy [N]``, ``/copy agent [N|all]``,
        ``/copy user [N|all]``, and ``/copy all``.  The legacy ``/copy N``
        form remains equivalent to ``/copy agent N``.

        Writes to both the host OS clipboard (pbcopy/clip.exe/xclip) and the terminal's
        OSC 52 clipboard. OSC 52 is what lets the copy reach the user's actual terminal
        clipboard in SSH / remote-tty sessions, where the native tools would otherwise
        copy to the remote host's clipboard (or fail outright).
        """
        self._copy_actions.copy_agent_responses(arg)

    def _toggle_fold(self) -> None:
        """Handle /fold slash command — collapse or expand all tool groups."""
        self._copy_actions.toggle_fold()

    def action_show_rollback(self, arg: str = "") -> None:
        """Open the picker or directly execute an explicit target.

        - ``/rollback`` with no argument: open the modal with the last
          turn pre-selected.
        - ``/rollback N``: directly discard the most recent ``N`` turns.
        - ``/rollback to N``: directly keep turns ``1..N`` and discard later turns.
        - Explicit targets restore eligible file changes by default and run
          under a blocking loading modal. Use the picker for conversation-only
          rollback.
        """
        self._rollback_controller.show_rollback(arg)

    def action_show_diff(self) -> None:
        """Open the full-screen diff viewer showing file changes per turn."""
        if self._workflow.workflow_mode:
            self._diff_controller.show_workflow_diff(
                self._workflow.session_id, self._workflow.project_cwd or self._workflow.cwd()
            )
        else:
            self._diff_controller.show_diff()

    # ------------------------------------------------------------------ #
    # Actions (keybindings)
    # ------------------------------------------------------------------ #

    def _set_agent_running(self, running: bool) -> None:
        if MainScreen._reactive_state_ready(self):
            self.agent_running_state = running
        else:
            MainScreen._apply_agent_running_state(self, running)

    def _apply_agent_running_state(self, running: bool) -> None:
        was_running = self._state.run.agent_running
        self._state.run.agent_running = running
        if running:
            self._state.run.turn_end_check_pending = True
            if not was_running:
                self._session_title.run_started()
                self._state.run.generation += 1
                self._live_diff.reset_for_run_start(
                    LiveDiffOwner(
                        session_id=self._view_adapter.current_chat_session_id(),
                        session_generation=self._services.session_generation(),
                        run_generation=self._state.run.generation,
                    )
                )
        else:
            # Agent tool calls (write_file / edit_file / shell) may have
            # created or deleted files during the turn; drop the cached
            # ``@`` suggestion list so the next trigger re-scans.  The
            # scan is lazy — invalidation is cheap; the real work only
            # runs when the user next types ``@``.  Mirrors the
            # invalidation in ``on_workspace_updated``.
            self._suggestions.file_cache = None
            if self._state.run.turn_end_check_pending:
                self._state.run.turn_end_check_pending = False
                self._workspace_actions.check_after_turn()
        input_bar = self.query_one(InputBar)
        if not running and input_bar.locked:
            input_bar.unlock_and_keep()
        self.refresh_bindings()
        # Auto-dismiss interrupt confirmation when agent stops
        if not running and self._state.overlays.interrupt_confirm_active:
            self._navigation.dismiss_interrupt_confirm()
        self._session_title.sync_activity()

    def _set_agent_loading(self, loading: bool) -> None:
        if MainScreen._reactive_state_ready(self):
            self.agent_loading_state = loading
        else:
            MainScreen._apply_agent_loading_state(self, loading)

    def _apply_agent_loading_state(self, loading: bool) -> None:
        self._state.run.agent_loading = loading
        input_bar = self.query_one(InputBar)
        if not loading and input_bar.locked and not self._state.run.agent_running:
            input_bar.unlock_and_keep()

    def _set_has_messages(self, has: bool) -> None:
        if MainScreen._reactive_state_ready(self):
            self.has_messages_state = has
        else:
            self._state.run.has_messages = has

    def action_show_log_viewer(self) -> None:
        """Open the log viewer modal (F6)."""
        self._navigation.show_log_viewer()

    def action_open_guide(self) -> None:
        """Open the user guide (F8), as ``/help`` does."""
        self._navigation.open_guide()

    def action_copy_text(self) -> None:
        """Copy selected text to both terminal and system clipboard."""
        if not self._copy_selected_text():
            # Match stock Screen semantics: an empty selection defers the
            # key to the next namespace (the app-level ctrl+c quit hint).
            raise SkipAction

    def get_selected_text(self) -> str | None:
        """Get selected text, excluding tool-renderer UI chrome and results.

        Chat drags keep ``self.selections`` bounded to the visible slice of
        the transcript; the full anchored range is extracted here, once, from
        the logical selection model.
        """
        chat_text = self._chat_selection.extract_text()
        if chat_text is not None:
            return chat_text
        if not self.selections:
            return None

        widget_text: list[str] = []
        for widget, selection in self.selections.items():
            if is_tool_copy_excluded(widget):
                continue
            if widget.is_attached and (selected_text_in_widget := widget.get_selection(selection)) is not None:
                widget_text.extend(selected_text_in_widget)

        if not widget_text:
            return None
        return "".join(widget_text).rstrip("\n")

    def action_interrupt(self) -> None:
        self._publish_interrupt()

    @work(thread=False)
    async def _publish_interrupt(self) -> None:
        if self._services.execution().kind == "workflow":
            self._workflow.run_control.stop()
            return
        await self._input_flow.publish_interrupt()

    # -- Per-sub-agent retry/abort bridging ---------------------------
    #
    # The :class:`SubAgentToolCall` renderer posts widget-level Textual
    # ``Message`` subclasses when the user clicks its Retry/Abort buttons.
    # Those bubble up the DOM to this screen; Textual dispatches them by
    # snake_case method name (``on_sub_agent_retry_clicked`` matches
    # ``SubAgentRetryClicked``). The screen then translates them into
    # bus events so the engine can route by ``invocation_id``.

    async def on_sub_agent_retry_clicked(self, event: SubAgentRetryClicked) -> None:
        """Bubbled from :class:`SubAgentToolCall` Retry button."""
        event.stop()
        await self._tool_actions.request_sub_agent_retry(event.invocation_id)

    async def on_sub_agent_abort_clicked(self, event: SubAgentAbortClicked) -> None:
        """Bubbled from :class:`SubAgentToolCall` Abort button."""
        event.stop()
        await self._tool_actions.request_sub_agent_abort(event.invocation_id)

    async def on_sleep_skip_clicked(self, event: SleepSkipClicked) -> None:
        """Bubbled from :class:`SleepToolCall` Skip button."""
        event.stop()
        await self._tool_actions.skip_sleep(event.call_id)

    async def on_ask_user_inline_submitted(self, event: AskUserInlineSubmitted) -> None:
        """Bubbled from :class:`AskUserToolCall` inline response controls."""
        event.stop()
        await self._tool_actions.submit_ask_user_inline(event.request_id, event.answers)

    def action_quit(self) -> None:
        self._navigation.quit()

    def action_sessions(self) -> None:
        """Open the sessions modal (Ctrl+N)."""
        self._navigation.sessions()

    def _clear_current_session(self) -> None:
        """/clear — confirm, then delete the current session and start fresh."""
        self._navigation.clear_session()

    async def _delete_session_and_new(self, session_id: str) -> None:
        if not self._workflow.workflow_mode:
            await self._sessions.delete_current_and_new(session_id)
            return
        await self._workflow.session_view.delete_current_session(session_id)

    def action_toggle_sidebar(self) -> None:
        self._navigation.toggle_sidebar()

    def _update_toc(self) -> None:
        self._navigation.update_toc()

    def action_pick_theme(self) -> None:
        """Open the theme picker modal."""
        if self.theme_editor is not None:
            self.notify(
                render_str(self._language_localizer(), THEME_PREVIEW_HINT.bind()), severity="warning", markup=False
            )
            self.theme_editor.editor.focus_first_swatch()
            return
        self._navigation.pick_theme()

    async def open_theme_editor(
        self,
        theme: Theme,
        store: UserThemeStore,
        revision: ThemeFileRevision | None = None,
    ) -> None:
        """Reserve a left dock before the header, transcript and input bar."""
        if self.theme_editor is not None:
            self.theme_editor.editor.focus_first_swatch()
            return

        def closed() -> None:
            self.theme_editor = None
            self._set_theme_editor_compact(False)

        panel = ThemeEditorPanel(theme, store, revision, on_close=closed)
        self.theme_editor = panel
        self._set_theme_editor_compact(self.size.width < 128)
        await self.mount(panel, before=0)

    async def manage_themes(self, *, picker: ThemesScreen | None = None) -> None:
        """Open theme management directly in the editor's document selector."""
        from chrys.app.tui.theme_loader import theme_is_read_only
        from chrys.app.tui.themes.store import ThemeStoreError, UserThemeStore

        if self.theme_editor is not None:
            self.theme_editor.editor.focus_first_swatch()
            return
        app = cast("ChrysApp", self.app)
        store = UserThemeStore()
        theme = app.get_theme(picker.original_theme if picker is not None else app.theme)
        if theme is None:
            raise RuntimeError("The selected theme is not registered.")
        revision = None
        if not theme_is_read_only(theme.name):
            try:
                theme, revision = await asyncio.to_thread(store.load, theme.name)
            except ThemeStoreError as error:
                self.notify(render_str(app.locale_controller.localizer, error.display), severity="error", markup=False)
                return
        # Keep file reads outside the render batch. The picker may have been
        # cancelled or covered while awaiting them; do not mount behind a modal.
        if picker is None:
            if app.screen is self:
                await self.open_theme_editor(theme, store, revision)
            return
        with app.batch_update():
            self._theme_editor_opening = True
            try:
                restoring_theme = app.theme != picker.original_theme
                if not picker.dismiss_for_editor():
                    return
                if restoring_theme:
                    # Finish the queued CSS/signal work before constructing
                    # the dock, so its children receive the final styles once.
                    restored = asyncio.Event()
                    app.call_next(restored.set)
                    await restored.wait()
                if app.screen is not self:
                    return
                await self.open_theme_editor(theme, store, revision)
                # Drain pending mount/layout messages on MainScreen before the
                # batch's first paint, rather than reflowing again for late ones.
                mounted = asyncio.Event()
                if self.call_later(mounted.set):
                    await mounted.wait()
            finally:
                self._theme_editor_opening = False

    def action_pick_language(self) -> None:
        """Open the no-preview language picker modal."""
        self._navigation.pick_language()

    def action_set_language(self, requested_locale: str) -> None:
        """Confirm a requested locale from a slash-command argument."""
        self._navigation.set_language(requested_locale)

    def action_toggle_trajectory_dashboard(self) -> None:
        """Toggle the in-place trajectory dashboard (F12)."""
        self._navigation.toggle_trajectory_dashboard()

    def action_escape(self) -> None:
        """Unified Esc handler — dismiss overlays, confirm interrupt, or confirm exit."""
        if self.theme_editor is not None:
            if self.theme_editor.editor.dismiss_dropdown():
                return
            self._navigation.escape(on_idle=self.theme_editor.action_close)
            return
        if self._suggestions_active_for_bindings():
            self._suggestions.dismiss_suggestions()
            return
        if not self._dashboard_visible() and self._workflow.back():
            return
        self._navigation.escape()

    # ------------------------------------------------------------------ #
    # Approval response
    # ------------------------------------------------------------------ #

    @work(thread=False)
    async def _handle_approval_response(
        self,
        request_id: str,
        approved: bool,
        reason: str = "",
        modified_args: dict[str, Any] | None = None,
        remember_choice: ReuseChoice = "",
    ) -> None:
        await self._tool_actions.publish_approval_response(request_id, approved, reason, modified_args, remember_choice)

    @work(thread=False)
    async def _handle_ask_user_response(self, request_id: str, answers: tuple[AskUserAnswer, ...]) -> None:
        await self._tool_actions.publish_ask_user_response(request_id, answers)

    def _question_inline_preferred(self) -> bool:
        """``tools.ask_user.inline`` as the Settings panel shows it right now.

        A panel write lands in the store at once but reaches the in-force
        settings only after the pending reload (turn end + panel close); a
        question arriving in between honours what the user just ticked.
        """
        coordinator = self._existing_settings_coordinator()
        if coordinator is not None:
            return bool(coordinator.projected_value("tools.ask_user.inline"))
        return cast("ChrysApp", self.app).settings_handle.settings.ask_user_inline

    # ------------------------------------------------------------------ #
    # Debug logging helper
    # ------------------------------------------------------------------ #

    def _debug(self, event_type: str, detail: str = "") -> None:
        """Log an event to the debug panel (if mounted)."""
        try:
            sp = self.query_one(SidebarPanel)
            sp.debug_panel.log_event(event_type, detail)
        except Exception:
            pass

    @on(ConversationToc.TurnSelected)
    def _on_toc_turn_selected(self, event: ConversationToc.TurnSelected) -> None:
        self._navigation.scroll_to_turn(event.turn_id)

    @on(TrajectoryDashboard.StateChanged)
    def _on_trajectory_dashboard_state_changed(self, event: TrajectoryDashboard.StateChanged) -> None:
        event.stop()
        self._view_adapter.sync_main_surface()

    @on(AppHeader.ModeClicked)
    def _on_mode_clicked(self) -> None:
        if not self._can_switch_app_mode():
            # Shell mode is the one block with no run to name; it stays silent.
            if (notice := self._app_mode_busy_notice()) is not None:
                self._view_adapter.notify(notice, title=workflow_text.MODE_BUSY_TITLE.bind(), severity="warning")
            return

        def selected(workflow: bool | None) -> None:
            if workflow is not None:
                self._set_workflow_mode(workflow)

        self.app.push_screen(
            AppModeDialog(self._workflow.workflow_mode, locale_controller=self._locale_controller), selected
        )

    def action_workflow(self) -> None:
        if not self._workflow.workflow_mode and not self._can_switch_app_mode():
            return
        self._set_workflow_mode(True)
        self._workflow.browser.enter_selection()

    def _can_switch_app_mode(self) -> bool:
        """Keep one presentation mode from submission through execution cleanup."""
        return not self._state.shell.active and self._app_mode_busy_notice() is None

    def _app_mode_busy_notice(self) -> MessageRef | None:
        """Name the run that pins the app mode, or None while nothing runs."""
        if self._workflow.awaiting_engine or self._services.execution().kind == "workflow":
            return workflow_text.MODE_WORKFLOW_BUSY.bind()
        if (
            self._state.run.agent_loading
            or self._state.run.agent_running
            or self._state.submit.active
            or self._services.execution_busy()
        ):
            return workflow_text.MODE_AGENT_BUSY.bind()
        return None

    def _set_workflow_mode(self, workflow: bool) -> None:
        if workflow == self._workflow.workflow_mode or not self._can_switch_app_mode():
            return
        input_bar = self.query_one(InputBar)
        if self._dashboard_visible():
            self._view_adapter.hide_trajectory_dashboard()
        if not workflow:
            self._workflow.leave()
        self._workflow.workflow_mode = workflow
        self.query_one(SidebarPanel).set_workflow_mode(workflow)
        self._sync_sidebar_tab_strip_focus()
        self._view_adapter.sync_main_surface()
        self._suggestions.dismiss_suggestions()
        self._sync_workflow_chrome()
        self._sync_execution_chrome()
        if workflow:
            self._workflow.enter()
        else:
            input_bar.focus_input()

    async def _apply_workflow_cwd(self, cwd: str) -> bool:
        if not self._workflow.workflow_mode:
            return False
        try:
            await self._workflow.browser.change_draft_workspace(cwd)
        except (OSError, ValueError) as exc:
            self._workflow.show_error(str(exc))
        return True

    def _accept_approval_update(self, event: ApprovalModeUpdated) -> bool:
        """The engine's launch policy is authoritative in both app modes."""
        return event.session_id is None or event.session_id == self.chat_session_id

    def _route_session_error(self, event: Error) -> bool:
        if not event.session_id or event.session_id != self._workflow.session_id:
            return False
        self._workflow.feedback.notify(event.display_message or event.message)
        self._workflow.request_refresh()
        return True

    def _sync_workflow_chrome(self) -> None:
        # The approval badge is the same in both app modes: one mode is chosen per launch.
        self.query_one(AppHeader).set_workflow_mode(self._workflow.workflow_mode)
        self._sync_main_bindings()

    async def _on_execution_changed(self, _event: ExecutionChanged) -> None:
        self._sync_execution_chrome()
        self._workflow.request_refresh()

    def _sync_execution_chrome(self) -> None:
        self.query_one(StatusBar).set_execution_busy(self._services.execution_busy())
        self._sync_main_bindings()

    def _sync_main_bindings(self) -> None:
        execution_busy = self._services.execution_busy()
        trajectory_available = self._navigation.can_toggle_trajectory_dashboard()
        if trajectory_available != self._trajectory_binding_available or execution_busy != self._execution_binding_busy:
            self._trajectory_binding_available = trajectory_available
            self._execution_binding_busy = execution_busy
            self.refresh_bindings()

    def _sync_workflow_timer(self) -> None:
        visible = self._workflow.workflow_mode and self._workflow_panel.display and self.app.screen is self
        if visible and self._workflow_timer is None:
            self._workflow_timer = self.set_interval(0.1, self._workflow.tick)
        elif not visible and self._workflow_timer is not None:
            self._workflow_timer.stop()
            self._workflow_timer = None

    @on(WorkflowPanel.RunSelected)
    async def _on_workflow_run_selected(self, event: WorkflowPanel.RunSelected) -> None:
        event.stop()
        await self._workflow.session_view.select_run(event.run_id)

    @on(WorkflowPanel.StartRequested)
    def _on_workflow_start(self) -> None:
        self._workflow.run_control.collect_input()

    @on(WorkflowPanel.NewSessionRequested)
    def _on_workflow_new(self) -> None:
        self._workflow.browser.enter_selection(new_session=True)

    @on(WorkflowPanel.StopRequested)
    def _on_workflow_stop(self) -> None:
        self._workflow.run_control.stop()

    @on(WorkflowPanel.ResultRequested)
    def _on_workflow_result(self, event: WorkflowPanel.ResultRequested) -> None:
        event.stop()
        self._workflow.session_view.open_result()

    @on(WorkflowGraph.NodeSelected)
    def _on_workflow_node(self, event: WorkflowGraph.NodeSelected) -> None:
        event.stop()
        self._workflow.session_view.open_node(event.node_id)

    @on(WorkflowGraph.RetryRequested)
    def _on_workflow_retry(self, event: WorkflowGraph.RetryRequested) -> None:
        event.stop()
        run = self._workflow.session_view.projector.run(event.target.run_id)
        attempt = run.nodes.get(event.node_id) if run is not None else None
        if attempt is not None and (attempt.activation_id, attempt.attempt) == (
            event.target.activation_id,
            event.target.attempt,
        ):
            self._workflow.run_control.retry_node(attempt)

    @on(WorkflowPanel.ViewChanged)
    def _on_workflow_view_changed(self) -> None:
        self._workflow.view_changed()
