# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow discovery, transactional previews and workspace-following draft settings."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text

from chrys.app.tui.binding_display import CLOSE_BINDING
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog, NoticeDialog
from chrys.app.tui.screens.dialogs.workflow_confirm import WorkflowConfirmDialog
from chrys.app.tui.screens.dialogs.workflow_input import WorkflowInputDialog
from chrys.app.tui.screens.dialogs.workflow_picker import WorkflowPickerDialog
from chrys.app.tui.screens.main.workflow_feedback import WorkflowNoticeAction
from chrys.app.tui.screens.main.workflow_flow import FlowToken, WorkflowFlow
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen
from chrys.app.tui.widgets.chrome.input_bar import INPUT_NEW
from chrys.app.tui.widgets.workflow import text
from chrys.app.tui.widgets.workflow.selection import WorkflowRow, WorkflowSelection
from chrys.foundation.events import types as events
from chrys.foundation.i18n import DisplayPath
from chrys.foundation.models.workflow_session import WorkflowModelSelection, WorkspaceSnapshot
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform import get_platform
from chrys.foundation.platform.paths import resolve_workspace_path
from chrys.foundation.util.once_close import finish_close
from chrys.orchestration.workflows.catalog import WorkflowCatalog, WorkflowNotFoundError
from chrys.orchestration.workflows.preview import (
    WorkflowInspection,
    WorkflowPreview,
    WorkflowPreviewError,
    WorkflowTrustDeclined,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from chrys.app.tui.screens.main.workflow_controller import WorkflowController
    from chrys.foundation.config.settings_store import LoadedSettings
    from chrys.foundation.models.workflow_session import WorkflowIdentity


PREVIEW_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class DraftSettings:
    workspace: WorkspaceSnapshot
    model: WorkflowModelSelection | None = None
    follows_workspace: bool = True


@dataclass(frozen=True)
class LoadedPreview:
    preview: WorkflowPreview
    catalog: WorkflowCatalog


class WorkflowBrowser:
    def __init__(self, host: WorkflowController) -> None:
        self.host = host
        self.draft = DraftSettings(WorkspaceSnapshot.capture(Workspace.from_cwd(host.cwd())))
        self.loaded: LoadedPreview | None = None
        self.preview_flow = WorkflowFlow(host)
        self.rows_flow = WorkflowFlow(host)
        self.models_flow = WorkflowFlow(host)
        self._catalog: WorkflowCatalog | None = None
        self._workspace_task: asyncio.Task | None = None
        self._confirmation: WorkflowConfirmDialog | None = None
        self._source_signature: tuple[object, ...] = ()
        self._picker: WorkflowPickerDialog | None = None
        self._picker_catalog: WorkflowCatalog | None = None
        self._source_notice: tuple[str, str, str] | None = None
        self._workspace_notice: NoticeDialog | None = None
        self._preview_request_id = ""
        self.startup_settings: LoadedSettings | None = None

    @property
    def workspace_busy(self) -> bool:
        return self._workspace_task is not None

    @property
    def picker(self) -> WorkflowPickerDialog | None:
        return self._picker

    def leave(self) -> None:
        self.preview_flow.invalidate()
        self.rows_flow.invalidate()
        self.models_flow.invalidate()
        self._cancel_workspace_change()
        self._dismiss_confirmation()
        self._dismiss_picker()
        if self._workspace_notice is not None:
            dialog, self._workspace_notice = self._workspace_notice, None
            dialog.finish()
        self.host.panel.end_preview()

    def initialize_draft_model(self) -> None:
        if self.host.session_id or self.draft.model is not None:
            return
        from chrys.service.workflows.model_selection import resolve_workflow_model

        try:
            self.draft = replace(
                self.draft,
                model=resolve_workflow_model(
                    self.host.services.model_registry, self.host.services.active_model_profile_id
                ),
            )
        except ValueError:
            return

    def refresh_preview_models(self) -> None:
        loaded = self.loaded
        handle = self.host.services.settings_handle
        registry = self.host.services.agent_registry
        if loaded is None or registry is None or handle is None:
            return
        workspace, model = self.host.workspace, self.host.model
        startup = self.startup_settings or handle.loaded

        async def resolve(token: FlowToken) -> None:
            from chrys.orchestration.workflows.settings import preview_bindings
            from chrys.service.workflows.admission import AdmissionError

            try:
                nodes = await preview_bindings(
                    loaded.preview.manifest,
                    workspace=workspace,
                    selected=model,
                    agent_registry=registry,
                    model_registry=self.host.services.model_registry,
                    settings_handle=handle,
                    startup=startup,
                )
            except AdmissionError:
                nodes = []
            if (
                self.models_flow.current(token)
                and self.loaded is loaded
                and self.host.model == model
                and self.host.workspace == workspace
            ):
                self.host.panel.set_preview_models(nodes)
                self.host.request_refresh()

        self.models_flow.start(resolve)

    def workspace_locked(self) -> bool:
        return self.host.workflow_mode and (bool(self.host.session_id) or self.source_workspace_locked())

    def request_workspace_change(self) -> bool:
        if not self.workspace_locked():
            return True
        self.explain_workspace_lock()
        return False

    def follow_current_workspace(self) -> None:
        if not self.draft.follows_workspace or self.host.session_id or self.host.awaiting_engine:
            return
        workspace = self.host.current_workspace()
        if self.draft.workspace.primary_cwd == workspace.primary_cwd:
            self.draft = replace(self.draft, workspace=workspace)
            return
        self.host.spawn(self._follow_workspace(workspace))

    async def on_workspace_updated(self, _event: events.WorkspaceUpdated) -> None:
        if self.host.workflow_mode:
            self.follow_current_workspace()

    async def _follow_workspace(self, workspace: WorkspaceSnapshot) -> None:
        generation, loaded = self.host.generation, self.loaded
        if not self.draft.follows_workspace or workspace != self.host.current_workspace():
            return
        try:
            if await self._change_workspace(workspace, bind_workspace=False):
                return
        except OSError, ValueError:
            pass
        if (
            self.host.closed
            or not self.host.workflow_mode
            or generation != self.host.generation
            or self.loaded is not loaded
            or not self.draft.follows_workspace
            or workspace != self.host.current_workspace()
        ):
            return
        # Chat has already committed the directory. Keep the old definition
        # visibly stale until it can be previewed in the current workspace.
        self.draft = replace(self.draft, workspace=workspace)
        self.check_preview(force=True)
        self.host.request_refresh()

    def present_source_warning(self) -> None:
        panel = self.host.panel
        if panel.empty or panel.previewing:
            return
        message = (
            text.STALE
            if panel.stale
            else (text.CODE_CHANGED if panel.code_differs and panel.code_visible and not panel.run_id else None)
        )
        preview = panel.preview
        key = (preview.source.canonical_path, preview.spec_digest, message.key) if preview and message else None
        if key != self._source_notice:
            self._source_notice = key
            if message is not None:
                self.host.feedback.notify(message.bind())

    @property
    def catalog(self) -> WorkflowCatalog:
        config, cwd = get_platform().config_dir, Path(self.host.workdir or self.host.cwd())
        if self._catalog is None or (self._catalog.config_dir, self._catalog.workdir) != (config, cwd):
            self._catalog = WorkflowCatalog(config_dir=config, workdir=cwd, bus=self.host.services.bus)
        return self._catalog

    async def on_preview_progress(self, event: events.WorkflowPreviewProgress) -> None:
        dialog = self.host.feedback.loading
        if self.host.closed or event.request_id != self._preview_request_id or dialog is None:
            return
        dialog.update_title(text.LOAD_WORKFLOW.bind(name=event.title or event.workflow_id))
        for stage, label in (
            ("definition", text.LOAD_DEFINITION),
            ("environment", text.LOAD_ENVIRONMENT),
            ("graph", text.LOAD_GRAPH),
        ):
            active = event.stage == stage
            dialog.update_progress(label.bind(), phase=f"workflow_{stage}", status="active" if active else "done")
            if active:
                break
        if event.stage == "ready":
            dialog.update_progress(
                text.NODES_LOADED.bind(loaded=event.node_count, total=event.node_count),
                phase="workflow_nodes",
                status="done",
            )

    def source_workspace_locked(self) -> bool:
        preview = self.host.panel.preview
        if preview is None or preview.source.source_kind == "builtin":
            return False
        return preview.source.source_kind == "workdir" or Path(preview.source.canonical_path).is_relative_to(
            Path(self.host.workdir or self.host.cwd()).resolve()
        )

    async def change_draft_workspace(self, cwd: str) -> bool:
        if self.workspace_locked() or self.host.services.execution_busy() or self.host.awaiting_engine:
            return False
        return await self._change_workspace(self._directory_workspace(cwd))

    def _directory_workspace(self, path: str) -> WorkspaceSnapshot:
        workspace = self.host.new_session_workspace()
        cwd = resolve_workspace_path(path, base_cwd=workspace.primary_cwd)
        if not Path(cwd).is_dir():
            raise ValueError(
                text.render(text.INVALID_DIRECTORY.bind(path=DisplayPath(cwd)), self.host.locale_controller)
            )
        return replace(workspace, primary_cwd=cwd)

    def _confirm_preview(
        self, preview: WorkflowPreview | WorkflowInspection, callback: Callable[[bool | None], None]
    ) -> WorkflowConfirmDialog:
        self._dismiss_confirmation()
        dialog = WorkflowConfirmDialog(preview, locale_controller=self.host.locale_controller)
        self._confirmation = dialog

        def closed(accepted: bool | None) -> None:
            if self._confirmation is dialog:
                self._confirmation = None
                callback(accepted)

        self.host.panel.app.push_screen(dialog, closed)
        return dialog

    def _dismiss_confirmation(self) -> None:
        dialog, self._confirmation = self._confirmation, None
        if dialog is not None:
            dialog.finish()

    def _can_select_workflow(self) -> bool:
        if bool(self.host.session_id):
            session_id, generation = self.host.session_id, self.host.generation

            def create_new() -> None:
                if self.host.session_id == session_id and self.host.generation == generation:
                    self.enter_selection(new_session=True)

            self.host.feedback.notify(
                text.SESSION_LOCKED.bind(),
                dismiss_label=text.OK.bind(),
                action=(
                    WorkflowNoticeAction(INPUT_NEW.bind(), create_new)
                    if not self.host.services.execution_busy() and not self.host.awaiting_engine
                    else None
                ),
            )
            return False
        return not (self.host.services.execution_busy() or self.host.awaiting_engine)

    def _selection_rows(self, catalog: WorkflowCatalog) -> tuple[list[WorkflowRow], str]:
        discovery = catalog.discover()
        ledger = catalog.ledger()
        rows = []
        for source, shadowed in [
            *((source, False) for source in discovery.sources),
            *((item.source, True) for item in discovery.shadowed),
        ]:
            rows.append(WorkflowRow(source, catalog.title(source, ledger=ledger) or "", shadowed=shadowed))
        rows.extend(
            WorkflowRow(warning, "", shadowed=discovery.find(warning.workflow_id) is not None)
            for warning in discovery.skipped
            if warning.workflow_id is not None and warning.source_kind in {"global", "workdir"}
        )
        return rows, "\n".join(f"⚠ {item.path}: {item.reason}" for item in discovery.skipped)

    def _delete_busy(self, row: WorkflowRow) -> bool:
        execution = self.host.execution()
        if execution.kind != "workflow":
            return False
        run = self.host.session_view.projector.run(execution.run_id)
        return run is None or run.started.workflow_id == row.workflow_id

    def delete_selected(self) -> None:
        row = self._picker.selection.selected_row() if self._picker is not None else None
        if row is None or row.source.source_kind == "builtin":
            return
        if self._delete_busy(row):
            self.host.show_error(text.DELETE_ACTIVE.bind())
            return
        catalog = self._picker_catalog or self.catalog
        try:
            # What deleting would refuse is said now, not after the user confirmed a deletion.
            catalog.check_delete(row.canonical_path)
        except (OSError, ValueError) as exc:
            self.host.show_error(str(exc))
            return
        source_label = text.render(text.SOURCES[row.source.source_kind].bind(), self.host.locale_controller)
        note = text.DELETE_GLOBAL if row.source.source_kind == "global" else text.DELETE_WORKDIR
        notes = [text.render(note.bind(), self.host.locale_controller)]
        if (folder := row.package_folder) is not None:
            package_note = text.DELETE_PACKAGE_NOTE.bind(
                entry=DisplayPath(f"{row.workflow_id}.py"), folder=DisplayPath(folder)
            )
            notes.insert(0, text.render(package_note, self.host.locale_controller))
        dialog = ConfirmDialog(
            title=text.DELETE.bind(),
            message=Text(f"{text.shown(row.canonical_path)}\n{source_label}\n\n" + "\n".join(notes)),
            confirm_label=text.DELETE.bind(),
            confirm_variant="error",
            locale_controller=self.host.locale_controller,
        )

        def confirmed(accepted: bool | None) -> None:
            if not accepted or self.host.closed:
                return
            if self._delete_busy(row):
                self.host.show_error(text.DELETE_ACTIVE.bind())
                return
            try:
                catalog.delete(row.canonical_path)
            except (OSError, ValueError) as exc:
                self.host.show_error(str(exc))
            else:
                self.enter_selection()

        self.host.panel.app.push_screen(dialog, confirmed)

    def explain_workspace_lock(self) -> None:
        if self._workspace_notice is not None:
            return
        session_id = self.host.session_id
        can_start_new = not self.host.services.execution_busy() and not self.host.awaiting_engine
        dialog = NoticeDialog(
            title=text.WORKSPACE_LOCKED_TITLE.bind(),
            message=(text.WORKSPACE_LOCKED if self.host.session_id else text.WORKSPACE_SOURCE_LOCKED).bind(),
            confirm_label=text.NEW_SESSION.bind() if can_start_new else CLOSE_BINDING.bind(),
            cancel_label=CLOSE_BINDING.bind() if can_start_new else None,
            locale_controller=self.host.locale_controller,
        )
        self._workspace_notice = dialog

        def closed(create_new: bool | None) -> None:
            if self._workspace_notice is not dialog:
                return
            self._workspace_notice = None
            if create_new and can_start_new and self.host.workflow_mode and self.host.session_id == session_id:
                self.enter_selection(new_session=True)

        self.host.panel.app.push_screen(dialog, closed)
        return

    def _cancel_workspace_change(self) -> asyncio.Task | None:
        task = self._workspace_task
        if task is not None and task is not asyncio.current_task():
            self.host.cancel_task(task)
        return task

    async def _change_workspace(self, workspace: WorkspaceSnapshot, *, bind_workspace: bool = True) -> bool:
        if (
            self.host.closed
            or not self.host.workflow_mode
            or self.host.session_id
            or self.host.awaiting_engine
            or self.host.services.execution_busy()
            or (bind_workspace and self.workspace_locked())
        ):
            return False
        if self.draft.workspace == workspace:
            if bind_workspace:
                self.draft = replace(self.draft, follows_workspace=False)
            return True
        previous_task = self._cancel_workspace_change()
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Changing the workflow workspace requires an asyncio task.")
        self._workspace_task = task
        self.preview_flow.invalidate()
        self._dismiss_confirmation()
        generation, loaded = self.host.generation, self.loaded
        screen = self.host.panel.app.screen
        input_dialog = screen if isinstance(screen, WorkflowInputDialog) else None
        self.host.request_refresh()

        def current() -> bool:
            return (
                not self.host.closed
                and self.host.workflow_mode
                and not self.host.session_id
                and not self.host.awaiting_engine
                and not self.host.services.execution_busy()
                and generation == self.host.generation
                and self.loaded is loaded
                and self._workspace_task is task
                and (bind_workspace or workspace == self.host.current_workspace())
            )

        try:
            if previous_task is not None and previous_task is not task:
                with contextlib.suppress(asyncio.CancelledError):
                    await finish_close(previous_task)
            if not current():
                return False
            if loaded is not None:
                catalog = WorkflowCatalog(
                    config_dir=self.catalog.config_dir,
                    workdir=Path(workspace.primary_cwd),
                    bus=self.host.services.bus,
                )
                try:
                    preview = await self._load_confirmed_preview(
                        catalog,
                        loaded.preview.source.workflow_id,
                        current,
                        expected_identity=loaded.preview.source.identity,
                        input_dialog=input_dialog,
                    )
                except WorkflowTrustDeclined:
                    return False
                except TimeoutError as exc:
                    raise ValueError(text.render(text.PREVIEW_TIMEOUT.bind(), self.host.locale_controller)) from exc
                except (WorkflowPreviewError, WorkflowNotFoundError) as exc:
                    raise ValueError(str(exc)) from exc
                if not current():
                    return False
                self.draft = replace(self.draft, workspace=workspace, follows_workspace=not bind_workspace)
                self._catalog = catalog
                self._show_preview(preview, catalog)
            else:
                self.draft = replace(self.draft, workspace=workspace, follows_workspace=not bind_workspace)
            return True
        finally:
            if self._workspace_task is task:
                self._workspace_task = None
                self.host.request_refresh()

    async def _load_confirmed_preview(
        self,
        catalog: WorkflowCatalog,
        workflow_id: str,
        current: Callable[[], bool],
        *,
        expected_identity: WorkflowIdentity | None = None,
        request_id: str = "",
        input_dialog: WorkflowInputDialog | None = None,
    ) -> WorkflowPreview:
        source_approved = False

        async def authorize(inspection: WorkflowInspection) -> bool:
            nonlocal source_approved
            self.host.feedback.close_loading()
            source_approved = await self._confirm(inspection, catalog, current, input_dialog=input_dialog)
            if source_approved and request_id:
                self.host.feedback.show_loading(
                    title=text.LOAD_WORKFLOW.bind(name=workflow_id),
                    message=text.LOAD_DEFINITION.bind(),
                    phase="workflow_definition",
                )
            return source_approved

        preview = await catalog.preview(
            workflow_id,
            timeout=PREVIEW_TIMEOUT_SECONDS,
            request_id=request_id,
            expected_identity=expected_identity,
            authorize=authorize,
        )
        if not current():
            raise WorkflowTrustDeclined
        self.host.feedback.close_loading()
        if source_approved:
            # The user authorized these exact bytes before loading; pin the resulting facts.
            await asyncio.to_thread(catalog.confirm, preview)
        elif not await self._confirm(preview, catalog, current, input_dialog=input_dialog):
            raise WorkflowTrustDeclined
        return preview

    async def _confirm(
        self,
        preview: WorkflowPreview | WorkflowInspection,
        catalog: WorkflowCatalog,
        current: Callable[[], bool],
        *,
        input_dialog: WorkflowInputDialog | None = None,
    ) -> bool:
        if not current():
            return False
        if isinstance(preview, WorkflowPreview):
            confirmed = await asyncio.to_thread(lambda: catalog.ledger().is_confirmed(preview.ledger_entry()))
            if not current():
                return False
            if preview.source.source_kind == "builtin" or confirmed:
                return True
        accepted = asyncio.get_running_loop().create_future()

        def answered(value: bool | None) -> None:
            if not accepted.done():
                accepted.set_result(bool(value))

        def present() -> None:
            if current():
                self._confirm_preview(preview, answered)
            elif not accepted.done():
                accepted.set_result(False)

        if is_widget_shown_on_active_screen(self.host.panel) or (
            input_dialog is not None and self.host.panel.app.screen is input_dialog
        ):
            present()
        else:
            self.host.feedback.pending_action = present
            self.host.request_refresh()
        try:
            if not await accepted or not current():
                return False
            if isinstance(preview, WorkflowPreview):
                await asyncio.to_thread(catalog.confirm, preview)
            return current()
        finally:
            self._dismiss_confirmation()

    def enter_selection(self, *, new_session: bool = False) -> None:
        if self._picker is not None and not new_session:
            self._load_rows()
            return
        if new_session:
            if self.host.services.execution_busy() or self.host.awaiting_engine:
                return
            self._dismiss_picker()
        elif not self._can_select_workflow():
            return
        self.preview_flow.invalidate()
        self._dismiss_confirmation()
        self.host.panel.end_preview()
        self.host.feedback.clear()
        self.host.session_view.content.invalidate_source()
        workspace = self.host.new_session_workspace()
        catalog = (
            WorkflowCatalog(
                config_dir=get_platform().config_dir,
                workdir=Path(workspace.primary_cwd),
                bus=self.host.services.bus,
            )
            if new_session
            else self.catalog
        )
        self._picker_catalog = catalog
        current = self.loaded.preview.source if self.loaded and not self.host.panel.stale and not new_session else None
        selection = WorkflowSelection(locale_controller=self.host.locale_controller, current_source=current)
        picker = WorkflowPickerDialog(selection, delete=self.delete_selected)
        self._picker = picker
        generation = self.host.generation

        async def selected(result: str | None) -> None:
            if self._picker is not picker:
                return
            self._picker = None
            self._picker_catalog = None
            self.rows_flow.invalidate()
            if self.host.closed or generation != self.host.generation:
                return
            if result is not None:
                if new_session and not await self.host.session_view.new_session(workspace=workspace):
                    return
                self.open(result)
            self.host.request_refresh()

        self.host.panel.app.push_screen(picker, selected)
        self._load_rows()
        self.host.sync_chrome()

    def _load_rows(self) -> None:
        picker, catalog = self._picker, self._picker_catalog
        if picker is None or catalog is None:
            return

        async def load(token: FlowToken) -> None:
            try:
                rows, warnings = await asyncio.to_thread(self._selection_rows, catalog)
                if self.rows_flow.current(token) and self._picker is picker:
                    picker.selection.show_rows(rows, warnings)
            except (OSError, ValueError) as exc:
                if self.rows_flow.current(token):
                    self.host.show_error(str(exc))

        self.rows_flow.start(load)

    def _dismiss_picker(self) -> None:
        picker, self._picker = self._picker, None
        self._picker_catalog = None
        self.rows_flow.invalidate()
        if picker is not None and picker.app.screen is picker:
            picker.dismiss()

    def open(self, workflow_id: str, *, input_text: str | None = None) -> None:
        if self._can_select_workflow():
            self.load_preview(workflow_id, input_text=input_text)

    def load_preview(self, workflow_id: str, *, input_text: str | None = None) -> None:
        self._cancel_workspace_change()
        self._dismiss_confirmation()
        self.host.feedback.clear()
        self._dismiss_picker()
        self.host.panel.begin_preview()
        self.host.feedback.show_loading(
            title=text.LOAD_WORKFLOW.bind(name=workflow_id),
            message=text.LOAD_DEFINITION.bind(),
            phase="workflow_definition",
        )
        self.preview_flow.start(lambda token: self._preview(token, workflow_id, input_text))

    async def _preview(self, token: FlowToken, workflow_id: str, input_text: str | None) -> None:
        catalog = self.catalog
        selection = self.host.session_view.selection
        self._preview_request_id = f"{token[0]}:{token[1]}"

        def current() -> bool:
            return self.preview_flow.current(token) and self.host.workflow_mode and self.host.panel.is_attached

        try:
            preview = await self._load_confirmed_preview(
                catalog,
                workflow_id,
                current,
                request_id=self._preview_request_id,
                expected_identity=selection.identity if selection is not None else None,
            )
            if not current():
                return
            if selection is not None and selection.identity != preview.source.identity:
                self.host.feedback.notify(text.SESSION_LOCKED.bind())
                return
            self.host.feedback.close_loading()

            def show() -> None:
                if current():
                    self._show_preview(preview, catalog)
                    if input_text is not None:
                        self.host.run_control.start(input_text)

            if is_widget_shown_on_active_screen(self.host.panel):
                show()
            else:
                self.host.feedback.pending_action = show
        except WorkflowTrustDeclined:
            pass
        except TimeoutError:
            if current():
                self.host.show_error(text.PREVIEW_TIMEOUT.bind())
        except WorkflowPreviewError as exc:
            if current():
                self.host.show_error(
                    "\n".join(
                        value for value in (f"{exc.code}: {exc.message}", exc.stdout.text, exc.traceback) if value
                    )
                )
        except (OSError, ValueError, KeyError) as exc:
            if current():
                self.host.show_error(str(exc))
        finally:
            if current():
                self.host.feedback.close_loading()
                self.host.panel.end_preview()
                self.host.request_refresh()

    def _show_preview(self, preview: WorkflowPreview, catalog: WorkflowCatalog) -> None:
        if self.loaded is None or self.loaded.preview.source.identity != preview.source.identity:
            self.host.run_control.clear_input()
        self.loaded = LoadedPreview(preview, catalog)
        panel = self.host.panel
        panel.show_preview(preview, run_id=panel.run_id)
        self.refresh_preview_models()
        self.host.session_view.content.reset()
        self.check_preview(force=True)
        self.host.session_view.content.view_changed()
        self.host.request_refresh()

    def check_preview(self, *, force: bool = False) -> bool:
        panel = self.host.panel
        preview = panel.preview
        if preview is None:
            return False
        catalog = self.catalog
        source = preview.source
        paths = catalog.candidate_paths(source)
        signature: list[object] = [catalog.config_dir, catalog.workdir, source.canonical_path]
        for path in paths:
            try:
                stat = path.stat()
                signature.append((stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino))
            except OSError:
                signature.append(None)
        current_signature = tuple(signature)
        signature_changed = current_signature != self._source_signature
        if not force and not signature_changed:
            return False
        self._source_signature = current_signature
        stale = (self.loaded is None or catalog is not self.loaded.catalog) or not catalog.is_current(preview)
        changed = panel.stale != stale
        panel.stale = stale
        return changed or signature_changed
