# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Restore workflow sessions and browse their run tabs and artifacts without executing source."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.app.tui.screens.dialogs.workflow_node import WorkflowNodeDialog
from chrys.app.tui.screens.dialogs.workflow_result import WorkflowResultDialog
from chrys.app.tui.screens.main.workflow_browser import DraftSettings
from chrys.app.tui.screens.main.workflow_content import WorkflowContent
from chrys.app.tui.screens.main.workflow_flow import FlowToken, WorkflowFlow
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen
from chrys.app.tui.widgets.workflow import text
from chrys.app.tui.widgets.workflow.projector import ObservedRun, WorkflowProjector
from chrys.app.tui.widgets.workflow.records import read_observed_run
from chrys.foundation.events import types as events
from chrys.foundation.models.workflow_session import WorkflowSessionSelection, WorkspaceSnapshot
from chrys.service.workflows.artifacts import WorkflowRunRecord, session_runs
from chrys.service.workflows.layout import run_dir
from chrys.service.workflows.store import read_run_header

if TYPE_CHECKING:
    from chrys.app.tui.screens.main.workflow_controller import WorkflowController
    from chrys.foundation.i18n import MessageRef
    from chrys.orchestration.workflows.preview import WorkflowPreview


class WorkflowSessionView:
    def __init__(self, host: WorkflowController) -> None:
        self.host = host
        self.selection: WorkflowSessionSelection | None = None
        self._run_ids: list[str] = []
        self._history_run: ObservedRun | None = None
        self._detail: WorkflowNodeDialog | None = None
        self.projector = WorkflowProjector()
        self.content = WorkflowContent(
            panel=host.panel,
            session_dir=self._session_dir,
            spawn=host.spawn,
            refresh=host.request_refresh,
            show_error=host.show_error,
            check_preview=lambda: host.browser.check_preview(force=True),
            locale=host.locale_controller,
        )

        self.restore_flow = WorkflowFlow(host)
        self.tab_flow = WorkflowFlow(host)

    @property
    def run_ids(self) -> tuple[str, ...]:
        return tuple(self._run_ids)

    @property
    def detail_foreground(self) -> bool:
        return self._detail is not None and self.host.panel.app.screen is self._detail

    def refresh_detail(self) -> None:
        if self._detail is not None:
            self._detail.refresh_run()

    async def accept_run(
        self, event: events.WorkflowRunAccepted, preview: WorkflowPreview, selected_run_id: str
    ) -> None:
        self.selection = event.selection
        self._run_ids.append(event.run_id)
        if self.host.panel.run_id == selected_run_id:
            self.host.panel.show_preview(preview, run_id=event.run_id)
            self.content.reset()
        await self.host.panel.show_runs(self._run_ids)

    def leave(self) -> None:
        self.restore_flow.invalidate()
        self.tab_flow.invalidate()
        self.content.invalidate_source()

    def _session_dir(self) -> Path | None:
        session_id = self.host.session_id
        store = self.host.services.state_store
        return store.session_dir(session_id) if session_id and store is not None else None

    def view_run(self) -> ObservedRun | None:
        run_id = self.host.panel.run_id
        observed = self.projector.run(run_id)
        if observed is not None:
            return observed
        return self._history_run if self._history_run and self._history_run.started.run_id == run_id else None

    async def on_invocation(self, event: events.InvocationEvent) -> None:
        if not self.host.closed:
            self.projector.record_invocation(event)
            if isinstance(event, events.InvocationProgress):
                self.host.request_refresh()

    async def new_session(self, *, workspace: WorkspaceSnapshot | None = None) -> bool:
        if self.host.services.execution_busy() or self.host.awaiting_engine:
            return False
        workspace = workspace or self.host.new_session_workspace()
        model = self.host.model
        self.host.leave()
        panel, run = self.host.panel, self.view_run()
        if panel.preview is not None:
            panel.show_preview(panel.preview)
        elif run is not None:
            panel.show_draft_history(run)
        self.host.browser.draft = DraftSettings(workspace, model=model, follows_workspace=False)
        self.selection = None
        self.host.browser.initialize_draft_model()
        self.host.run_control.reset()
        self._run_ids.clear()
        self._history_run = None
        self.projector = WorkflowProjector()
        self.content.reset()
        panel.clear_outputs()
        panel.show_graph()
        self.host.browser.refresh_preview_models()
        await panel.show_runs([])
        self.host.request_refresh()
        return True

    async def restore_session(
        self, session_id: str, run_id: str = "", *, on_missing: Callable[[], None] | None = None
    ) -> None:
        if self.host.services.execution_busy() or self.host.awaiting_engine or self.host.services.state_store is None:
            return
        self.host.leave()
        await self.restore_flow.start(lambda token: self._restore_session(token, session_id, run_id, on_missing))

    async def _restore_session(
        self, token: FlowToken, session_id: str, run_id: str, on_missing: Callable[[], None] | None
    ) -> None:
        store = self.host.services.state_store
        if store is None:
            raise RuntimeError("Restoring a workflow session requires a state store.")
        self.host.feedback.show_loading(
            title=text.LOAD_SESSION.bind(name=""),
            message=text.LOAD_SESSION_RECORD.bind(),
            phase="workflow_session_record",
        )
        name = ""

        def progress(message: MessageRef, phase: str, *, status: str = "done") -> None:
            if self.restore_flow.current(token) and self.host.feedback.loading is not None:
                self.host.feedback.loading.update_title(text.LOAD_SESSION.bind(name=name))
                self.host.feedback.loading.update_progress(message, phase=phase, status=status)

        try:
            meta = await store.load_session_meta(session_id, strict=True)
            if meta is None or meta.kind != "workflow":
                raise ValueError(text.render(text.NO_RECORD.bind(), self.host.locale_controller))
            session_id = meta.session_id
            name = meta.latest_run.title if meta.latest_run is not None else meta.workflow_id
            state = await store.load_workflow_session(session_id)
            if state is None:
                raise ValueError(text.render(text.NO_RECORD.bind(), self.host.locale_controller))
            progress(text.SESSION_READ.bind(), "workflow_session_record")
            progress(text.LOAD_SNAPSHOT.bind(), "workflow_snapshot", status="active")
            directory = store.session_dir(session_id)

            def read() -> tuple[list[str], ObservedRun]:
                records = session_runs(directory)
                candidates = [record for record in records if not run_id or record.directory.name == run_id]
                for record in candidates:
                    try:
                        historical = read_observed_run(record)
                    except OSError, ValueError, KeyError:
                        continue
                    return [record.directory.name for record in reversed(records)], historical
                raise ValueError(text.render(text.NO_RECORD.bind(), self.host.locale_controller))

            ids, historical = await asyncio.to_thread(read)
            progress(text.SNAPSHOT_READ.bind(), "workflow_snapshot")
            progress(text.STATUS_RESTORED.bind(), "workflow_status")
            count = len(historical.started.manifest.get("nodes", []))
            progress(text.NODES_LOADED.bind(loaded=count, total=count), "workflow_nodes")
            if not self.restore_flow.current(token):
                return
            self.host.run_control.reset()
            self.selection = state.selection(session_id)
            self._run_ids = ids
            self.projector = WorkflowProjector()
            self._history_run = historical
            self.host.browser.loaded = None
            self.content.reset()
            self.host.panel.show_history(historical)
            await self.host.panel.show_runs(ids)
            if not self.restore_flow.current(token):
                return
            self.content.view_changed()
            if not run_id and state.latest_run_id and historical.started.run_id != state.latest_run_id:
                self.host.feedback.notify(text.RESTORE_FALLBACK.bind(run_id=historical.started.run_id))
        except (OSError, ValueError, KeyError) as exc:
            if on_missing is not None and self.restore_flow.current(token):
                # A missing run snapshot alone does not mean the session was deleted.
                missing = False
                with suppress(Exception):
                    missing = await store.resolve_startup_session(session_id) is None
                if missing:
                    if self.restore_flow.current(token):
                        on_missing()
                    return
            if self.restore_flow.current(token):
                self.host.show_error(str(exc))
        finally:
            if self.restore_flow.current(token):
                self.host.feedback.close_loading()
                self.host.request_refresh()

    async def select_run(self, run_id: str) -> None:
        if run_id not in self._run_ids or run_id == self.host.panel.run_id:
            return
        await self.tab_flow.start(lambda token: self._select_run(token, run_id))

    async def _select_run(self, token: FlowToken, run_id: str) -> None:
        run = self.projector.run(run_id)
        if run is not None:
            self._show_run(run)
        else:
            self.host.feedback.show_loading(
                title=text.LOAD_SESSION.bind(name=self.host.panel.definition.workflow_id),
                message=text.LOAD_SNAPSHOT.bind(),
                phase="workflow_snapshot",
            )
            try:
                await self._open_history(run_id, token)
            except (OSError, ValueError, KeyError) as exc:
                if self.tab_flow.current(token):
                    self.host.show_error(str(exc))
            finally:
                if self.tab_flow.current(token):
                    self.host.feedback.close_loading()
        if self.tab_flow.current(token):
            await self.host.panel.show_runs(self._run_ids)

    async def _open_history(self, run_id: str, token: FlowToken) -> None:
        directory = self._session_dir()
        if directory is None or Path(run_id).name != run_id or run_id.startswith("."):
            raise ValueError(text.render(text.NO_RECORD.bind(), self.host.locale_controller))

        def read() -> ObservedRun:
            path = run_dir(directory, run_id)
            return read_observed_run(WorkflowRunRecord(path, read_run_header(path)))

        historical = await asyncio.to_thread(read)
        if not self.tab_flow.current(token) or not self.host.workflow_mode:
            return
        self.host.feedback.close_loading()

        def show() -> None:
            if self.tab_flow.current(token):
                self._show_run(historical)

        if is_widget_shown_on_active_screen(self.host.panel):
            show()
        else:
            self.host.feedback.pending_action = show

    async def delete_current_session(self, session_id: str) -> None:
        if self.host.services.execution_busy() or self.host.awaiting_engine:
            self.host.show_error(text.DELETE_ACTIVE.bind())
            return
        generation = self.host.generation
        await self.host.services.bus.publish(events.SessionDelete(session_id=session_id))
        store = self.host.services.state_store
        if (
            store is not None
            and await store.load_session_meta(session_id) is None
            and generation == self.host.generation
            and self.host.session_id == session_id
        ):
            await self.new_session()

    def _show_run(self, run: ObservedRun) -> None:
        preview = self.host.panel.preview
        self._history_run = run
        self.content.reset()
        self.host.panel.show_history(run, preview=preview)
        self.content.view_changed()
        self.host.request_refresh()

    def open_result(self) -> None:
        """Show the selected run's final outputs; a finished run's outputs never change, so nothing refreshes."""
        run, directory = self.view_run(), self._session_dir()
        if run is None or run.finished is None or not run.finished.outputs:
            return
        self.host.panel.app.push_screen(
            WorkflowResultDialog(
                tuple(run.finished.outputs),
                kinds={node["id"]: node["kind"] for node in run.started.manifest.get("nodes", [])},
                directory=run_dir(directory, run.started.run_id) if directory is not None else None,
                outcome=run.finished.outcome,
                locale_controller=self.host.locale_controller,
            )
        )

    def open_node(self, node_id: str) -> None:
        panel, directory = self.host.panel, self._session_dir()
        preview, run = panel.preview, self.view_run()
        if run is not None:
            manifest = run.started.manifest
        elif preview is not None:
            manifest = preview.manifest
        else:
            return
        node = next((node for node in manifest.get("nodes", []) if node["id"] == node_id), None)
        if node is None:
            return

        def history() -> list[WorkflowRunRecord]:
            return session_runs(directory)[:100] if directory is not None else []

        dialog = WorkflowNodeDialog(
            node,
            run=run,
            directory=run_dir(directory, panel.run_id) if directory is not None and panel.run_id else None,
            history=history,
            retry=self.host.run_control.retry_node,
            retry_pending=self.host.run_control.retry_pending,
            locale_controller=self.host.locale_controller,
        )
        self._detail = dialog

        def closed(_result: None) -> None:
            if self._detail is dialog:
                self._detail = None

        panel.app.push_screen(dialog, closed)
