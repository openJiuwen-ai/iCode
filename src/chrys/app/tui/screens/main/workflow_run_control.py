# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow run control operations and owned UI state."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from uuid import uuid4

from chrys.app.tui.screens.dialogs.ask_user import AskUserDialog, AskUserDialogResult
from chrys.app.tui.screens.dialogs.confirm import NoticeDialog
from chrys.app.tui.screens.main.recent_dirs import WorkspaceMruRecentDirs
from chrys.app.tui.screens.main.session_title import WorkflowTab
from chrys.app.tui.widgets.workflow import text
from chrys.foundation.events import types as events
from chrys.foundation.events.workflow import WorkflowRunEvent
from chrys.foundation.models.ask_user import validate_ask_user_answers
from chrys.foundation.models.workflow_session import (
    WorkflowSessionSelection,
)
from chrys.orchestration.workflows.preview import PreparedWorkflow
from chrys.service.workflows.outcomes import run_status

if TYPE_CHECKING:
    from chrys.app.tui.screens.dialogs.workflow_input import WorkflowInputContext
    from chrys.app.tui.screens.main.workflow_controller import WorkflowController

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _PendingRun:
    request_id: str
    prepared: PreparedWorkflow
    selected_run_id: str


class WorkflowRunControl:
    def __init__(self, host: WorkflowController) -> None:
        self.host = host
        self._model_request = ""
        self._pending_run: _PendingRun | None = None
        self._input_draft = ""
        self._input_open = False
        self._question: tuple[events.WorkflowNodeAskUser, AskUserDialog] | None = None
        self._pending_answers: set[str] = set()
        self._retried: set[tuple[str, str, int]] = set()
        self._cancel_confirmation: tuple[str, NoticeDialog] | None = None
        self._input_generation = 0
        # The run the terminal tab follows: the last one this screen started.
        # Its terminal is kept apart from the projector, which drops a
        # terminal that arrives without ``WorkflowRunStarted``.
        self._tab_run_id = ""
        self._tab_finished: events.WorkflowRunFinished | None = None

    @property
    def awaiting_engine(self) -> bool:
        return self._pending_run is not None or bool(self._model_request)

    def leave(self) -> None:
        self._forget_tab_run()
        self._input_generation += 1
        self._input_open = False
        self._close_cancel_confirmation()
        if self._question is not None:
            self._question[1].dismiss_due_to_timeout()

    def clear_input(self) -> None:
        self._input_draft = ""

    @property
    def pending_retries(self) -> frozenset[tuple[str, str, int]]:
        return frozenset(self._retried)

    def refresh_dialogs(self) -> None:
        if self._cancel_confirmation is not None:
            run_id, _dialog = self._cancel_confirmation
            run = self.host.session_view.projector.run(run_id)
            execution = self.host.execution()
            if execution.kind != "workflow" or execution.run_id != run_id or (run is not None and run.finished):
                self._close_cancel_confirmation()
        self._refresh_question()

    def reset(self) -> None:
        self.host.reset_program_status()
        self.clear_input()
        self._pending_answers.clear()
        self._retried.clear()
        self._forget_tab_run()

    def _forget_tab_run(self) -> None:
        self._tab_run_id = ""
        self._tab_finished = None

    def terminal_tab(self, title: str, cwd: str) -> WorkflowTab:
        """The terminal tab for the run this screen started last."""
        if self._pending_run is not None:
            return WorkflowTab(title, cwd, "running")
        run_id = self._tab_run_id
        if (finished := self._tab_finished) is not None:
            # A cancelled or interrupted run shows no mark, as a chat interrupt does.
            status = run_status(finished.outcome, finished.reason, active=False)
            if status == "completed":
                return WorkflowTab(title, cwd, "completed")
            return WorkflowTab(title, cwd, "failed" if status == "failed" else "idle")
        execution = self.host.execution()
        if not run_id or execution.kind != "workflow" or execution.run_id != run_id:
            # Never started, or the lease is gone without a terminal: nothing is live.
            return WorkflowTab(title, cwd)
        if (run := self.host.session_view.projector.run(run_id)) is None:
            return WorkflowTab(title, cwd, "running")
        if any(
            node.state == "awaiting_retry" and (run_id, node.activation_id, node.attempt) not in self._retried
            for node in run.nodes.values()
        ):
            return WorkflowTab(title, cwd, "awaiting_retry")
        if any(request_id not in self._pending_answers for request_id in run.questions):
            return WorkflowTab(title, cwd, "question")
        return WorkflowTab(title, cwd, "running")

    def can_change_model(self) -> bool:
        return bool(
            self.host.workflow_mode
            and not self.host.closed
            and not self.host.services.execution_busy()
            and not self.host.awaiting_engine
            and not self.host.browser.workspace_busy
            and self.host.feedback.loading is None
            and not self.host.browser.preview_flow.active
        )

    async def choose_model(self, profile_id: str) -> bool:
        if not self.can_change_model():
            return False
        from chrys.service.workflows.model_selection import resolve_workflow_model

        try:
            model = resolve_workflow_model(self.host.services.model_registry, profile_id)
        except ValueError as exc:
            self.host.show_error(str(exc))
            return False
        if model is None:
            return False
        if model == self.host.model:
            return True
        if isinstance(self.host.session_view.selection, WorkflowSessionSelection):
            request_id = uuid4().hex
            self._model_request = request_id
            self.host.refresh()
            try:
                await self.host.services.bus.publish(
                    events.WorkflowModelChangeRequest(
                        session_id=self.host.session_view.selection.session_id,
                        profile_id=model.profile_id,
                        request_id=request_id,
                    )
                )
            finally:
                if self._model_request == request_id:
                    self._model_request = ""
                    self.host.request_refresh()
        else:
            self.host.browser.draft = replace(self.host.browser.draft, model=model)
            self.host.browser.refresh_preview_models()
            self.host.request_refresh()
        return self.host.model == model

    async def on_model_result(self, event: events.WorkflowModelChangeResult) -> None:
        if event.request_id != self._model_request or event.session_id != self.host.session_id or self.host.closed:
            return
        self._model_request = ""
        if event.selection is not None:
            self.host.session_view.selection = event.selection
            self.host.browser.refresh_preview_models()
        else:
            self.host.show_error(event.error)
        self.host.request_refresh()

    def _run_input_context(self) -> WorkflowInputContext:
        from chrys.app.tui.screens.dialogs.workflow_input import WorkflowInputContext

        registry = self.host.services.agent_registry
        panel = self.host.panel
        run = self.host.session_view.view_run()
        recorded = {node["node_id"]: node for node in run.started.resolved_nodes} if run else {}
        has_kernel_agents = False
        for node_id, agent in panel.definition.agents.items():
            profile = registry.resolve_selector(agent.profile) if registry is not None else None
            # A missing model must not hide the control needed to select one.
            # Archived backend facts remain useful when a profile has been removed.
            acp = profile.acp is not None if profile is not None else recorded.get(node_id, {}).get("acp", False)
            if not acp:
                has_kernel_agents = True
                break
        return WorkflowInputContext(
            title=panel.definition.manifest.get("title") or panel.definition.workflow_id,
            description=panel.definition.manifest.get("description") or "",
            source_kind=panel.definition.source_kind,
            working_directory=self.host.workdir or self.host.cwd(),
            has_kernel_agents=has_kernel_agents,
            can_change_directory=(
                self.host.session_view.selection is None
                and self.host.panel.preview is not None
                and self.can_change_model()
                and not self.host.browser.source_workspace_locked()
            ),
            directory_notice=(
                text.DIRECTORY_SESSION_BOUND.bind()
                if self.host.session_id
                else text.DIRECTORY_SOURCE_BOUND.bind()
                if self.host.browser.source_workspace_locked()
                else None
            ),
        )

    def collect_input(self) -> None:
        """Capture the selected target while collecting a draft; only submit starts a run."""
        from chrys.app.tui.screens.dialogs.workflow_input import WorkflowInputDialog, WorkflowInputResult

        if self.host.panel.empty or self.host.panel.previewing:
            self.host.show_error(text.PICK_HINT.bind())
            return
        if self._input_open or self.host.awaiting_engine or self.host.services.execution_busy():
            return
        generation = self.host.generation
        self._input_generation += 1
        input_generation = self._input_generation
        selection, loaded = self.host.session_view.selection, self.host.browser.loaded
        draft_settings = self.host.browser.draft
        self._input_open = True

        def current() -> bool:
            return (
                not self.host.closed
                and generation == self.host.generation
                and self.host.workflow_mode
                and input_generation == self._input_generation
                and selection == self.host.session_view.selection
                and loaded is self.host.browser.loaded
                and draft_settings is self.host.browser.draft
            )

        async def change_directory(path: str) -> WorkflowInputContext | None:
            nonlocal loaded, draft_settings
            if not current() or not self._run_input_context().can_change_directory or selection is not None:
                return None
            if not await self.host.browser.change_draft_workspace(path):
                return None
            loaded = self.host.browser.loaded
            draft_settings = self.host.browser.draft
            return self._run_input_context()

        async def collected(result: WorkflowInputResult | None) -> None:
            if input_generation == self._input_generation:
                self._input_open = False
            if not current():
                return
            if result is not None:
                self._input_draft = result.text
                if result.submitted:
                    if result.model_profile_id is not None and not await self.choose_model(result.model_profile_id):
                        return
                    if self.host.closed or generation != self.host.generation or not self.host.workflow_mode:
                        return
                    self.start(result.text)

        self.host.panel.app.push_screen(
            WorkflowInputDialog(
                self._input_draft,
                context=self._run_input_context(),
                change_directory=change_directory,
                recent_paths=WorkspaceMruRecentDirs(
                    self.host.services.state_store,
                    max_entries=self.host.services.workspace_mru_max_entries,
                ),
                model=self.host.model,
                model_registry=self.host.services.model_registry,
                locale_controller=self.host.locale_controller,
            ),
            collected,
        )

    def start(self, draft: str) -> bool:
        panel = self.host.panel
        if panel.empty or panel.previewing:
            self.host.show_error(text.PICK_HINT.bind())
            return False
        if self.host.awaiting_engine or self.host.browser.workspace_busy:
            return False
        if self.host.services.execution_busy():
            self.host.show_error(text.TURN_BUSY.bind())
            return False
        self.host.browser.check_preview(force=True)
        if panel.stale and self.host.session_id:
            self.host.browser.load_preview(panel.definition.workflow_id, input_text=draft)
            return False
        if panel.stale:
            self.host.feedback.notify(text.STALE.bind())
            self.host.refresh()
            return False
        loaded = self.host.browser.loaded
        if loaded is None or panel.definition.workflow_id != loaded.preview.source.workflow_id:
            if panel.definition.workflow_id:
                self.host.browser.load_preview(panel.definition.workflow_id, input_text=draft)
            return False
        target = self.host.session_view.selection or self.host.new_target(loaded.preview.source.workflow_id)
        prepared = PreparedWorkflow(target, loaded.preview)
        self._pending_run = _PendingRun(uuid4().hex, prepared, panel.run_id)
        self._forget_tab_run()
        event = prepared.run_request(input_text=draft, request_id=self._pending_run.request_id)
        self.host.refresh()
        self.host.spawn(self.host.services.bus.publish(event))
        if panel.code_visible or panel.output_visible:
            self.host.session_view.content.invalidate_source()
        panel.show_graph(focus=True)
        return True

    def can_stop(self) -> bool:
        """Own either an accepted run or the request holding the lease during admission."""
        execution = self.host.execution()
        return (
            not self.host.closed
            and execution.kind == "workflow"
            and bool(execution.run_id)
            and (
                execution.run_id in self.host.session_view.run_ids
                or (self._pending_run is not None and execution.request_id == self._pending_run.request_id)
            )
        )

    def stop(self) -> None:
        """Confirm cancellation of this exact run before sending it to the engine."""
        execution = self.host.execution()
        if not self.can_stop() or self._cancel_confirmation is not None:
            return
        run_id = execution.run_id
        dialog = NoticeDialog(
            title=text.CANCEL_RUN_TITLE.bind(),
            message=text.CANCEL_RUN_MESSAGE.bind(run_id=execution.run_id),
            confirm_label=text.CANCEL_RUN_CONFIRM.bind(),
            cancel_label=text.KEEP_RUNNING.bind(),
            confirm_variant="error",
            locale_controller=self.host.locale_controller,
        )
        self._cancel_confirmation = run_id, dialog

        def confirmed(accepted: bool | None) -> None:
            if self._cancel_confirmation != (run_id, dialog):
                return
            self._cancel_confirmation = None
            current = self.host.execution()
            run = self.host.session_view.projector.run(run_id)
            if (
                accepted
                and not self.host.closed
                and current.kind == "workflow"
                and current.run_id == run_id
                and (run is None or run.finished is None)
            ):
                self.host.spawn(self.host.services.bus.publish(events.WorkflowCancelRequest(run_id=run_id)))
            self.host.request_refresh()

        self.host.panel.app.push_screen(dialog, confirmed)

    def _close_cancel_confirmation(self) -> None:
        confirmation, self._cancel_confirmation = self._cancel_confirmation, None
        if confirmation is not None:
            confirmation[1].finish()

    async def on_workflow_event(
        self, event: WorkflowRunEvent | events.WorkflowRunAccepted | events.WorkflowRunRejected
    ) -> None:
        if self.host.closed:
            return
        if (
            not isinstance(event, (events.WorkflowRunAccepted, events.WorkflowRunRejected))
            and event.session_id
            and event.session_id != self.host.session_id
        ):
            return
        if isinstance(event, (events.WorkflowRunAccepted, events.WorkflowRunRejected)):
            pending = self._pending_run
            if pending is None or event.request_id != pending.request_id:
                return
            self._pending_run = None
            if isinstance(event, events.WorkflowRunAccepted):
                self._tab_run_id = event.run_id
                self._input_draft = ""
                await self.host.session_view.accept_run(event, pending.prepared.preview, pending.selected_run_id)
            elif event.error != "cancelled":
                self.host.show_error(f"{event.error}: {event.message}")
        else:
            if isinstance(event, events.WorkflowRunFinished) and event.run_id == self._tab_run_id:
                self._tab_finished = event
            self.host.session_view.projector.record(event)
            self._prune_actions()
        self.host.request_refresh()

    def retry_node(self, attempt: events.WorkflowNodeStateChanged) -> bool:
        run = self.host.session_view.projector.run(attempt.run_id)
        execution = self.host.execution()
        key = (attempt.run_id, attempt.activation_id, attempt.attempt)
        if (
            run is None
            or execution.kind != "workflow"
            or self.host.panel.run_id != attempt.run_id
            or execution.run_id != attempt.run_id
            or run.finished
            or run.nodes.get(attempt.node_id) != attempt
            or attempt.state != "awaiting_retry"
            or key in self._retried
        ):
            return False
        self._retried.add(key)
        self.host.request_refresh()
        self.host.spawn(
            self.host.services.bus.publish(
                events.WorkflowNodeRetryRequest(
                    run_id=attempt.run_id,
                    node_id=attempt.node_id,
                    activation_id=attempt.activation_id,
                    expected_failed_attempt=attempt.attempt,
                    request_id=uuid4().hex,
                )
            )
        )
        return True

    def retry_pending(self, attempt: events.WorkflowNodeStateChanged) -> bool:
        return (attempt.run_id, attempt.activation_id, attempt.attempt) in self._retried

    def _prune_actions(self) -> None:
        """Only live questions and failed attempts can still own a pending UI action."""
        runs = [
            run
            for run in (self.host.session_view.projector.current, self.host.session_view.projector.previous)
            if run and not run.finished
        ]
        self._pending_answers.intersection_update(key for run in runs for key in run.questions)
        self._retried.intersection_update(
            (run.started.run_id, node.activation_id, node.attempt)
            for run in runs
            for node in run.nodes.values()
            if node.state == "awaiting_retry"
        )

    def _refresh_question(self) -> None:
        if self._question is not None:
            request, dialog = self._question
            run = self.host.session_view.projector.run(request.run_id)
            if run is None or run.finished is not None or request.request_id not in run.questions:
                dialog.dismiss_due_to_timeout()
            return
        panel = self.host.panel
        if panel.app.screen is not panel.screen and not self.host.session_view.detail_foreground:
            return
        run = self.host.session_view.projector.current
        if run is None or run.finished is not None:
            return
        request = next((request for key, request in run.questions.items() if key not in self._pending_answers), None)
        if request is None:
            return
        dialog = AskUserDialog(request.request_id, request.questions, caller_name=request.node_id, allow_inline=False)
        self._question = request, dialog

        def answered(result: AskUserDialogResult) -> None:
            self._question = None
            answers = None
            if isinstance(result, tuple):
                # The runner applies this same check to the same questions. Failing it here, before the request
                # is marked answered, reopens the question instead of stranding an answer the runner would reject.
                answers = validate_ask_user_answers(result[1], questions=request.questions)
                if answers is None:
                    logger.warning("workflow question %s: dropped an answer that does not fit it", request.request_id)
                    self.host.request_refresh()
                    return
            self._pending_answers.add(request.request_id)
            live = self.host.session_view.projector.run(request.run_id)
            if (
                answers is not None
                and live is not None
                and live.finished is None
                and request.request_id in live.questions
            ):
                self.host.spawn(
                    self.host.services.bus.publish(
                        events.WorkflowNodeAnswer(
                            run_id=request.run_id,
                            node_id=request.node_id,
                            activation_id=request.activation_id,
                            request_id=request.request_id,
                            answers=answers,
                        )
                    )
                )
            self.host.request_refresh()

        panel.app.push_screen(dialog, answered)
