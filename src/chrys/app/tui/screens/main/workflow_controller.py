# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Wire workflow browsing, run control and archived session views to the main screen."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any

from chrys.app.tui.screens.main.workflow_browser import WorkflowBrowser
from chrys.app.tui.screens.main.workflow_feedback import WorkflowFeedback
from chrys.app.tui.screens.main.workflow_run_control import WorkflowRunControl
from chrys.app.tui.screens.main.workflow_session_view import WorkflowSessionView
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen
from chrys.app.tui.widgets.workflow.projector import INVOCATION_EVENTS, WORKFLOW_EVENTS
from chrys.foundation.events import types as events
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.models.workflow_session import WorkflowDraft, WorkspaceSnapshot
from chrys.foundation.models.workspace import Workspace

if TYPE_CHECKING:
    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.screens.main.session_title import WorkflowTab
    from chrys.app.tui.screens.main.state import MainScreenServices
    from chrys.app.tui.widgets.workflow.panel import WorkflowPanel
    from chrys.foundation.i18n import MessageRef
    from chrys.foundation.models.workflow_session import WorkflowModelSelection


class WorkflowController:
    def __init__(
        self,
        *,
        services: MainScreenServices,
        panel: WorkflowPanel,
        cwd: Callable[[], str],
        schedule: Callable[[Callable[[], None]], bool],
        sync_chrome: Callable[[], None],
        sync_terminal_title: Callable[[WorkflowTab | None], None],
        reset_program_status: Callable[[], None],
        locale_controller: LocaleController | None,
    ) -> None:
        self.services, self.panel, self.cwd = services, panel, cwd
        self.schedule, self.sync_chrome = schedule, sync_chrome
        self.sync_terminal_title = sync_terminal_title
        self.reset_program_status = reset_program_status
        self.locale_controller = locale_controller
        self.workflow_mode = False
        self.closed = False
        self.generation = 0
        self._refresh_pending = False
        # refresh() found the panel displayed but not yet placed on the screen.
        self._refresh_after_layout = False
        self._tasks: set[asyncio.Task] = set()
        self.feedback = WorkflowFeedback(
            panel=panel,
            refresh=self.request_refresh,
            enabled=lambda: self.workflow_mode and not self.closed,
            locale=locale_controller,
        )
        self.browser = WorkflowBrowser(self)
        self.run_control = WorkflowRunControl(self)
        self.session_view = WorkflowSessionView(self)

        self._subscriptions: tuple[tuple[type[events.Event], Callable[..., Awaitable[None]]], ...] = (
            (events.WorkflowModelChangeResult, self.run_control.on_model_result),
            (events.WorkspaceUpdated, self.browser.on_workspace_updated),
            (events.WorkflowPreviewProgress, self.browser.on_preview_progress),
            *((event_type, self.run_control.on_workflow_event) for event_type in WORKFLOW_EVENTS),
            *((event_type, self.session_view.on_invocation) for event_type in INVOCATION_EVENTS),
        )

    @property
    def session_id(self) -> str:
        selection = self.session_view.selection
        return selection.session_id if selection is not None else ""

    @property
    def workdir(self) -> str:
        return self.workspace.primary_cwd

    @property
    def workspace(self) -> WorkspaceSnapshot:
        selection = self.session_view.selection
        return selection.workspace if selection is not None else self.browser.draft.workspace

    @property
    def model(self) -> WorkflowModelSelection | None:
        selection = self.session_view.selection
        return selection.model if selection is not None else self.browser.draft.model

    def new_target(self, workflow_id: str) -> WorkflowDraft:
        return WorkflowDraft(workflow_id, self.workspace, self.model)

    def new_session_workspace(self) -> WorkspaceSnapshot:
        return self.current_workspace() if self.session_id else self.browser.draft.workspace

    def current_workspace(self) -> WorkspaceSnapshot:
        provider = self.services.engine_provider
        workspace = provider().workspace if provider is not None else None
        return WorkspaceSnapshot.capture(workspace or Workspace.from_cwd(self.cwd()))

    def execution(self) -> ExecutionSnapshot:
        return self.services.execution()

    @property
    def awaiting_engine(self) -> bool:
        return self.run_control.awaiting_engine

    def show_error(self, message: str | MessageRef) -> None:
        if not self.closed:
            self.feedback.notify(message)

    async def subscribe(self) -> None:
        handle = self.services.settings_handle
        self.browser.startup_settings = handle.loaded if handle is not None else None
        for event_type, handler in self._subscriptions:
            await self.services.bus.subscribe(event_type, handler)

    async def close(self) -> None:
        self.closed = True
        self.leave()
        self.session_view.content.close()
        for event_type, handler in self._subscriptions:
            await self.services.bus.unsubscribe(event_type, handler)
        # Sent requests belong to the engine. Drain their publishers, as well as
        # cancelled browsing tasks, before releasing the screen's resources.
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    def leave(self) -> None:
        self.generation += 1
        self._refresh_after_layout = False
        self.browser.leave()
        self.run_control.leave()
        self.session_view.leave()
        self.feedback.clear()

    def enter(self) -> None:
        self.browser.initialize_draft_model()
        self.browser.follow_current_workspace()
        self.browser.refresh_preview_models()
        self.shown()
        self.panel.call_after_refresh(self.request_refresh)

    def spawn(self, coroutine: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def cancel_task(self, task: asyncio.Task) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if task is not asyncio.current_task() and not task.done() and not task.cancelling():
            task.cancel()

    def back(self) -> bool:
        if self.workflow_mode and not (self.panel.empty or self.panel.previewing) and not self.panel.graph_visible:
            self.session_view.content.invalidate_source()
            self.panel.show_graph(focus=True)
            self.request_refresh()
            return True
        if self.browser.preview_flow.active:
            self.browser.enter_selection()
            return True
        return False

    def request_refresh(self) -> None:
        if self._refresh_pending or self.closed:
            return
        self._refresh_pending = self.schedule(self.refresh)

    def tick(self) -> None:
        if not self.workflow_mode:
            return
        panel = self.panel
        if panel.display and not (panel.empty or panel.previewing):
            panel.advance_animation(self.session_view.view_run())
        if (
            panel.display
            and not (panel.empty or panel.previewing)
            and is_widget_shown_on_active_screen(panel)
            and not self.services.execution_busy()
            and not self.awaiting_engine
            and self.browser.check_preview()
        ):
            if panel.code_visible and not panel.run_id:
                self.session_view.content.view_changed()
            self.request_refresh()

    def refresh(self) -> None:
        self._refresh_pending = False
        if self.closed:
            return
        panel = self.panel
        if not panel.is_attached:
            return
        self.run_control.refresh_dialogs()
        self.session_view.refresh_detail()
        run = self.session_view.view_run()
        title = run.started.title if run is not None else panel.definition.manifest.get("title", "")
        cwd = self.workdir or self.cwd()
        panel.set_header(title, session_id=self.session_id, cwd=cwd)
        self.sync_terminal_title(
            self.run_control.terminal_tab(title or panel.definition.workflow_id, cwd) if self.workflow_mode else None
        )
        self.sync_chrome()
        if not panel.display:
            return
        if self.browser.picker is not None:
            self.feedback.present_notice(over=self.browser.picker)
            return
        if not is_widget_shown_on_active_screen(panel):
            # A panel shown from a dialog callback is displayed before the
            # layout that places it; that layout finishes this refresh.
            self._refresh_after_layout = True
            return
        self._refresh_after_layout = False
        if self.feedback.present_pending():
            return
        execution = self.execution()
        panel.project(
            run,
            busy=self.services.execution_busy(),
            workflow_active=execution.kind == "workflow" and execution.run_id == panel.run_id,
            can_stop=self.run_control.can_stop(),
            starting=bool(self.awaiting_engine) or self.browser.workspace_busy,
            retry_pending=self.run_control.pending_retries,
        )
        self.browser.present_source_warning()
        panel.show_status(run)
        self.session_view.content.project(run)
        self.feedback.present_notice(over=self.browser.picker)

    def screen_layout_refreshed(self) -> None:
        """Finish a refresh that ran before a screen layout placed the panel."""
        if self._refresh_after_layout and is_widget_shown_on_active_screen(self.panel):
            self._refresh_after_layout = False
            self.request_refresh()

    def shown(self) -> None:
        self.browser.check_preview(force=True)
        self.view_changed()

    def view_changed(self) -> None:
        self.session_view.content.view_changed()
        self.request_refresh()
