# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow run admission and routing for one engine.

A ``WorkflowRunRequest`` is answered exactly once, with ``WorkflowRunAccepted``
or ``WorkflowRunRejected``; a repeated ``request_id`` gets the same answer
again and starts nothing. Admission reserves the execution lease first (one
workflow run globally, never beside a live turn), then resolves the file,
checks its confirmed bytes before probing its interpreter, checks the prepared
environment, loads it on a fresh worker, checks the caller's expected digests
and the full confirmation record, admits the manifest against
the registries, and opens the run record. Only then is the request accepted
and the runner started. Every rejection along the way releases the lease
and closes the worker.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections import OrderedDict
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from chrys.foundation.config.settings import Settings
from chrys.foundation.config.warnings import settings_warning_events
from chrys.foundation.events.types import (
    SessionDeleted,
    WorkflowCancelRequest,
    WorkflowModelChangeRequest,
    WorkflowModelChangeResult,
    WorkflowNodeAnswer,
    WorkflowNodeRetryRequest,
    WorkflowRollbackRequest,
    WorkflowRollbackResult,
    WorkflowRunAccepted,
    WorkflowRunRejected,
    WorkflowRunRequest,
)
from chrys.foundation.models.workflow_session import WorkflowSessionSelection
from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.util.once_close import finish_close
from chrys.orchestration.engine.execution import WorkflowExecution
from chrys.orchestration.workflows.hooks import WorkflowSessionHooks
from chrys.orchestration.workflows.preview import (
    REJECT_NOT_CONFIRMED,
    REJECT_SPEC_CHANGED,
    WorkflowPreviewError,
    ledger_entry_for,
    load_workflow,
    materialize_runtime_sdk,
    prepare_workflow_environment,
    worker_bytecode_cache_dir,
)
from chrys.orchestration.workflows.session import (
    WorkflowSessionInUse,
    WorkflowSessionNotFound,
    WorkflowSessionOwner,
    WorkflowWorkspaceLocked,
)
from chrys.orchestration.workflows.settings import admission_settings, load_workflow_settings
from chrys.service.approval.policy import ApprovalMode
from chrys.service.hooks.events import HookEvent
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.trajectory.workflow import WorkflowTrace
from chrys.service.workflows.admission import AdmissionError, admit_manifest
from chrys.service.workflows.discovery import SOURCE_KIND_BUILTIN, WorkflowSource, discover_workflows
from chrys.service.workflows.journal import WorkflowJournal
from chrys.service.workflows.layout import run_dir
from chrys.service.workflows.ledger import ConfirmationLedger, ledger_path
from chrys.service.workflows.model_selection import resolve_workflow_model
from chrys.service.workflows.outcomes import REASON_DEADLINE_EXCEEDED, REASON_SHUTDOWN
from chrys.service.workflows.scheduler import RunMode
from chrys.service.workflows.store import RunHeader, RunSpec, WorkflowRunStore, WorkflowStorageFailed

if TYPE_CHECKING:
    from chrys.foundation.config.settings_store import SettingsHandle
    from chrys.foundation.events.bus import EventBus
    from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.orchestration.workflows.runner import WorkflowRunner, WorkflowRunResult
    from chrys.orchestration.workflows.session import HookFactory
    from chrys.service.mcp.cache import MCPConnectionCache
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.session.persistence import SessionPersistence
    from chrys.service.workflows.ledger import LedgerEntry

logger = logging.getLogger(__name__)

REJECT_INVALID_REQUEST: Final = "invalid_request"
REJECT_SESSION_CHANGED: Final = "session_changed"
REJECT_SESSION_NOT_FOUND: Final = "session_not_found"
REJECT_SESSION_IN_USE: Final = "session_in_use"
REJECT_ENGINE_NOT_READY: Final = "engine_not_ready"
REJECT_SHUTTING_DOWN: Final = "shutting_down"
REJECT_CANCELLED: Final = "cancelled"
REJECT_WORKFLOW_ACTIVE: Final = "workflow_active"
REJECT_TURN_ACTIVE: Final = "turn_active"
REJECT_ENGINE_BUSY: Final = "engine_busy"
REJECT_WORKFLOW_NOT_FOUND: Final = "workflow_not_found"
REJECT_ENVIRONMENT_CHANGED: Final = "environment_changed"
REJECT_STORAGE_FAILED: Final = "storage_failed"
REJECT_INTERNAL_ERROR: Final = "internal_error"
REJECT_WORKSPACE_LOCKED: Final = "workspace_locked"
REJECT_WORKING_DIR_MISSING: Final = "working_dir_missing"

MAX_REMEMBERED_REPLIES: Final = 256
MAX_REMEMBERED_RESULTS: Final = 16


class _Rejection(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _not_confirmed(source: WorkflowSource) -> _Rejection:
    return _Rejection(
        REJECT_NOT_CONFIRMED,
        f"Workflow {source.workflow_id!r} at {source.canonical_path} is not confirmed for this environment; "
        "review and confirm it first.",
    )


@dataclass(slots=True)
class _ActiveRun:
    execution: WorkflowExecution
    session_id: str = ""
    owner: WorkflowSessionOwner | None = None
    source: WorkflowSource | None = None
    runner: WorkflowRunner | None = None
    cancel_reason: str | None = None
    """The first admission cancellation cause; also guards against cancelling its cleanup twice."""


class WorkflowCoordinator:
    """Owns workflow event subscriptions, the one active run, and the request → reply memory."""

    def __init__(
        self,
        *,
        bus: EventBus,
        session: ActiveSession,
        persistence: SessionPersistence,
        build_hooks: HookFactory,
        turn_state: TurnRuntimeState,
        settings_handle: SettingsHandle,
        agent_registry: AgentProfileRegistry | None,
        model_registry: ModelProfileRegistry | None,
        allow_user_interaction: bool,
        mcp_cache: MCPConnectionCache,
        config_dir: Callable[[], Path] | None = None,
    ) -> None:
        self._mcp_cache = mcp_cache
        self._bus = bus
        self._session = session
        self._persistence = persistence
        self._build_hooks = build_hooks
        self._turn_state = turn_state
        self._settings_handle = settings_handle
        self._startup_settings = settings_handle.loaded
        self._agent_registry = agent_registry
        self._model_registry = model_registry
        self._mode = RunMode.INTERACTIVE if allow_user_interaction else RunMode.HEADLESS
        self._allow_user_interaction = allow_user_interaction
        self._config_dir: Callable[[], Path] = config_dir or (lambda: get_platform().config_dir)
        self._replies: OrderedDict[str, WorkflowRunAccepted | WorkflowRunRejected] = OrderedDict()
        self._rollback_replies: OrderedDict[str, WorkflowRollbackResult] = OrderedDict()
        self._results: OrderedDict[str, WorkflowRunResult] = OrderedDict()
        self._model_changes: set[str] = set()
        self._active: _ActiveRun | None = None
        self._hooks = WorkflowSessionHooks()
        self._closing = False

    # -- observation ---------------------------------------------------------------

    @property
    def active_run_id(self) -> str | None:
        return self._active.execution.run_id if self._active is not None else None

    @property
    def active_request_id(self) -> str | None:
        """The request the active run (admitted or still being admitted) answers to."""
        return self._active.execution.request_id if self._active is not None else None

    @property
    def active_source(self) -> WorkflowSource | None:
        """The reserved run's source, available from discovery through lease release."""
        return self._active.source if self._active is not None else None

    def result(self, run_id: str) -> WorkflowRunResult | None:
        """The full result of a recent run (outputs carry whole values, unlike the event summaries)."""
        return self._results.get(run_id)

    def mutation_snapshot(self, session_id: str) -> dict[str, Any] | None:
        """Capture the selected active session's ledger before a deferred frontend read."""
        active = self._active
        owner = active.owner if active is not None else None
        if owner is None or owner.session.session_id != session_id:
            return None
        tracker = owner.session.mutation_tracker
        return tracker.serialize() if tracker is not None else None

    def set_approval_mode(self, mode: ApprovalMode) -> None:
        """Synchronize live nodes with the engine's launch policy without session I/O."""
        active = self._active
        if active is not None and active.runner is not None:
            active.runner.set_approval_mode(mode)

    @asynccontextmanager
    async def _session_owner(
        self, session_id: str, *, allow_active: bool = True
    ) -> AsyncGenerator[WorkflowSessionOwner]:
        """Borrow an editable active owner or restore one, keeping close behind the operation."""
        active = self._active
        if active is not None and active.session_id == session_id:
            if not allow_active:
                raise ValueError(
                    "This workflow session has an active run. Wait for it to finish before changing its model."
                )
            owner = active.owner
            if owner is not None:
                async with owner.edit() as available:
                    if available:
                        yield owner
                        return
        owner = WorkflowSessionOwner(
            bus=self._bus,
            persistence=self._persistence,
            session_id=session_id,
            workspace=None,
        )
        try:
            await owner.open()
            async with owner.edit() as available:
                if not available:
                    raise RuntimeError("The workflow session closed before it could be edited.")
                yield owner
        finally:
            await finish_close(asyncio.create_task(owner.close()))

    async def on_model_change(self, event: WorkflowModelChangeRequest) -> None:
        result = WorkflowModelChangeResult(session_id=event.session_id, request_id=event.request_id)
        if event.session_id is None:
            result.error = "Select a workflow session."
        elif event.session_id in self._model_changes:
            result.error = "This workflow session's model is being updated. Try again in a moment."
        else:
            # Fence only this session before awaiting its guard. Other sessions
            # can keep executing while this checkpoint is written.
            self._model_changes.add(event.session_id)
            try:
                model = resolve_workflow_model(self._model_registry, event.profile_id)
                if model is None:
                    raise ValueError("Select a model profile.")
                async with self._session_owner(event.session_id, allow_active=False) as owner:
                    await owner.set_model(model)
                    result.selection = owner.selection
            except (ValueError, OSError) as exc:
                result.error = str(exc)
            finally:
                self._model_changes.remove(event.session_id)
        await self._bus.publish(result)

    async def on_rollback(self, event: WorkflowRollbackRequest) -> None:
        from chrys.orchestration.workflows.rollback import rollback_files

        prior = self._rollback_replies.get(event.request_id)
        if prior is not None:
            await self._bus.publish(prior)
            return
        result = WorkflowRollbackResult(session_id=event.session_id, request_id=event.request_id, run_id=event.run_id)
        refusal = self._refusal()
        if refusal is not None:
            result.error = refusal[1]
        else:
            with self._turn_state.lease.session_operation():
                try:
                    if event.session_id is None:
                        raise ValueError("Select a workflow session.")
                    async with self._session_owner(event.session_id) as owner:
                        settings = await self._load_run_settings(
                            Path(owner.require_workspace().primary_cwd), request_id=event.request_id
                        )
                        result = await rollback_files(owner, event, settings=settings)
                except (KeyError, ValueError, OSError) as exc:
                    result.error = str(exc)
        _remember(self._rollback_replies, event.request_id, result, MAX_REMEMBERED_REPLIES)
        await self._bus.publish(result)

    # -- wiring -------------------------------------------------------------------------

    async def subscribe(self) -> None:
        await self._bus.subscribe(WorkflowRollbackRequest, self.on_rollback)
        await self._bus.subscribe(WorkflowRunRequest, self.on_run_request)
        await self._bus.subscribe(WorkflowModelChangeRequest, self.on_model_change)
        await self._bus.subscribe(SessionDeleted, self._on_session_deleted)
        await self._bus.subscribe(WorkflowCancelRequest, self.on_cancel)
        await self._bus.subscribe(WorkflowNodeAnswer, self.on_answer)
        await self._bus.subscribe(WorkflowNodeRetryRequest, self.on_retry)

    async def shutdown(self) -> None:
        self._closing = True
        await self.abandon_active()
        # Reentrant shutdown from a run subscriber returns before that run
        # drains. Its finally owns session-end, after run-end, in that case.
        if self._active is None:
            await self._hooks.close()
            await self._turn_state.lease.settle_notifications()

    async def _on_session_deleted(self, event: SessionDeleted) -> None:
        if event.session_id:
            await self._hooks.release(event.session_id)

    async def abandon_active(self) -> None:
        """Cancel the active run as a shutdown (its owner left) and wait for its worker to exit.

        From inside the admission task (a subscriber to the request's reply awaiting a shutdown), the task is
        not cancelled under its own reply: the reply goes out, and a run it admitted starts cancelled.
        """
        self.cancel_active(reason=REASON_SHUTDOWN)
        await self.wait_idle()

    def cancel_active(self, *, reason: str = "") -> bool:
        """Request cancellation with its cause; return whether this request initiated it."""
        active = self._active
        if active is None:
            return False
        if active.runner is not None:
            return active.runner.cancel(reason=reason) is not None
        return self._cancel_admission(active, reason=reason)

    def _cancel_admission(self, active: _ActiveRun, *, reason: str) -> bool:
        """Interrupt admission once, preserving cleanup and any reply already being published."""
        if active.cancel_reason is not None or active.execution.request_id in self._replies:
            return False
        active.cancel_reason = reason
        task = active.execution.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        return True

    async def wait_idle(self) -> None:
        """Wait until the active run, if any, has released the execution lease.

        Called from inside that run (a subscriber to one of its events awaiting a shutdown, on the run task,
        on the admission task publishing its reply, or on a node task publishing under the runner's lock),
        it returns at once: a run cannot be waited out from within itself, and the cancel already queued
        ends it as soon as the handler returns.
        """
        active = self._active
        if active is None:
            return
        task = active.execution.task
        if task is None:
            return
        current = asyncio.current_task()
        if task is current or (active.runner is not None and active.runner.holds(current)):
            return
        await asyncio.wait({task})

    # -- handlers --------------------------------------------------------------------------

    async def on_run_request(self, event: WorkflowRunRequest) -> None:
        request_id = event.request_id
        if not request_id:
            await self._bus.publish(
                WorkflowRunRejected(
                    request_id="",
                    error=REJECT_INVALID_REQUEST,
                    message="A workflow run request needs a request_id.",
                    session_id=event.session_id or None,
                )
            )
            return
        remembered = self._replies.get(request_id)
        if remembered is not None:
            await self._bus.publish(remembered)
            return
        if self._active is not None and self._active.execution.request_id == request_id:
            return  # the admission in flight answers this request once, when it is decided
        refusal = self._refusal()
        if refusal is None and event.session_id in self._model_changes:
            refusal = REJECT_SESSION_CHANGED, "This workflow session's model is being updated. Try again in a moment."
        if refusal is not None:
            await self._reply(
                WorkflowRunRejected(
                    session_id=event.session_id, request_id=request_id, error=refusal[0], message=refusal[1]
                )
            )
            return
        execution = self._turn_state.lease.begin_workflow(new_analytics_id(), request_id)
        active = _ActiveRun(execution, session_id=event.session_id or "")
        self._active = active
        # Eager start: the run task enters its try/finally before this handler returns, so a shutdown
        # that lands right behind the request releases the lease through the task, never around it.
        execution.task = asyncio.create_task(
            self._run(event, active), name=f"chrys.workflow.run.{execution.run_id}", eager_start=True
        )

    async def on_cancel(self, event: WorkflowCancelRequest) -> None:
        active = self._active
        if active is None or event.run_id != active.execution.run_id:
            return
        self.cancel_active()

    async def on_answer(self, event: WorkflowNodeAnswer) -> None:
        active = self._active
        if active is None or active.runner is None or event.run_id != active.execution.run_id:
            return
        if not active.runner.answer(event.node_id, event.activation_id, event.request_id, event.answers):
            logger.info("workflow run %s: no open ask accepts answer %s", event.run_id, event.request_id)

    async def on_retry(self, event: WorkflowNodeRetryRequest) -> None:
        active = self._active
        if active is None or active.runner is None or event.run_id != active.execution.run_id:
            return
        active.runner.retry(event.node_id, event.activation_id, event.request_id, event.expected_failed_attempt)

    # -- admission ------------------------------------------------------------------------------

    def _refusal(self) -> tuple[str, str] | None:
        """The deterministic reasons a request is refused before anything is reserved."""
        lease = self._turn_state.lease
        if self._persistence.state_store is None:
            return REJECT_ENGINE_NOT_READY, "Workflow execution requires a session store."
        if self._session.shutting_down:
            return REJECT_SHUTTING_DOWN, "The engine is shutting down."
        reason = lease.workflow_start_refusal()
        if reason is None:
            return None
        return reason, {
            REJECT_WORKFLOW_ACTIVE: "A workflow run is already active. Cancel it first.",
            REJECT_TURN_ACTIVE: "A turn is active. Wait for it to finish or interrupt it first.",
            REJECT_ENGINE_BUSY: "The session is being rebuilt, restored or replaced. Try again in a moment.",
        }[reason]

    async def _run(self, event: WorkflowRunRequest, active: _ActiveRun) -> None:
        execution = active.execution
        session_id = self._session.session_id
        try:
            try:
                runner, journal = await self._admit(event, active)
            except _Rejection as exc:
                await self._reply(
                    WorkflowRunRejected(
                        session_id=event.session_id, request_id=event.request_id, error=exc.code, message=exc.message
                    )
                )
                return
            except asyncio.CancelledError:
                # Cancellation is latched once, so cleanup and this reply cannot be cancelled again by a
                # repeated request. Publish on this task: a subscriber awaiting shutdown must not wait on us.
                if event.request_id not in self._replies:
                    cancelled = active.cancel_reason in ("", REASON_DEADLINE_EXCEEDED)
                    await self._reply(
                        WorkflowRunRejected(
                            session_id=event.session_id,
                            request_id=event.request_id,
                            error=REJECT_CANCELLED if cancelled else REJECT_SHUTTING_DOWN,
                            message="The run was cancelled before it was admitted."
                            if cancelled
                            else "The run was given up before it was admitted.",
                        )
                    )
                raise
            except Exception as exc:
                # Admission must answer even when it is the one that is broken, or the requester waits forever.
                logger.exception("workflow run %s: admission failed", execution.run_id)
                await self._reply(
                    WorkflowRunRejected(
                        session_id=event.session_id,
                        request_id=event.request_id,
                        error=REJECT_INTERNAL_ERROR,
                        message=f"{type(exc).__name__}: {exc}",
                    )
                )
                return
            if active.owner is None:
                raise RuntimeError("The active workflow has no session owner.")
            active.runner = runner
            if active.cancel_reason is not None:
                cancelled = runner.cancel(reason=active.cancel_reason)
                if cancelled is None:
                    raise RuntimeError("Cancelling a fresh workflow runner did not return a drain task.")
                await cancelled  # applied before the start: the run begins cancelled and no node executes
            if active.owner.selection is None:
                raise RuntimeError("Accepting a workflow run requires a session selection.")
            await self._reply(
                WorkflowRunAccepted(
                    request_id=event.request_id,
                    run_id=execution.run_id,
                    selection=active.owner.selection,
                )
            )
            workflow_session_id = active.owner.require_session_id()

            async def attach_session() -> None:
                if active.owner is None:
                    raise RuntimeError("The active workflow has no session owner.")
                await self._hooks.attach(active.owner)

            async def start_hooks() -> None:
                await self._hooks.run_event(
                    workflow_session_id,
                    HookEvent.WORKFLOW_RUN_START,
                    run_id=execution.run_id,
                    input_text=event.input_text,
                )

            async def end_hooks(result: WorkflowRunResult) -> None:
                await self._hooks.run_event(
                    workflow_session_id,
                    HookEvent.WORKFLOW_RUN_END,
                    run_id=result.run_id,
                    outcome=result.outcome.value,
                    reason=result.reason,
                )

            try:
                result = await runner.run(
                    event.input_text,
                    startup=(attach_session, start_hooks),
                    record_end=end_hooks,
                )
            finally:
                await journal.store.close()
            _remember(self._results, result.run_id, result, MAX_REMEMBERED_RESULTS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("workflow run %s failed outside the runner", execution.run_id)
        finally:
            try:
                if active.owner is not None:
                    await active.owner.close()
            finally:
                self._turn_state.lease.end_workflow(execution)
                if self._active is active:
                    self._active = None
                if self._closing:
                    await self._hooks.close()
            logger.debug("workflow run %s released the execution lease (session %s)", execution.run_id, session_id)

    async def _load_run_settings(self, project_cwd: Path, *, request_id: str) -> Settings:
        effective = await load_workflow_settings(
            project_cwd, startup=self._startup_settings, handle=self._settings_handle
        )
        for warning in settings_warning_events(effective):
            await self._bus.publish(replace(warning, request_id=request_id))
        return effective.settings

    async def _admit(self, event: WorkflowRunRequest, active: _ActiveRun) -> tuple[WorkflowRunner, WorkflowJournal]:
        """Everything between the reservation and the acceptance; raises :class:`_Rejection`."""
        from chrys.orchestration.workflows.agent_node_build import AgentNodeResources
        from chrys.orchestration.workflows.runner import WorkerCallbacks, WorkflowRunner

        execution = active.execution
        owner = WorkflowSessionOwner(
            bus=self._bus,
            persistence=self._persistence,
            session_id=event.session_id or "",
            workspace=event.target.workspace.materialize(),
        )
        active.owner = owner
        active.session_id = owner.session.session_id or ""
        try:
            await owner.open(reconcile=True)
        except WorkflowWorkspaceLocked as exc:
            raise _Rejection(REJECT_WORKSPACE_LOCKED, str(exc)) from exc
        except WorkflowSessionNotFound as exc:
            raise _Rejection(REJECT_SESSION_NOT_FOUND, str(exc)) from exc
        except WorkflowSessionInUse as exc:
            raise _Rejection(REJECT_SESSION_IN_USE, str(exc)) from exc
        except ValueError as exc:
            raise _Rejection(REJECT_INVALID_REQUEST, str(exc)) from exc
        if (
            isinstance(event.target, WorkflowSessionSelection)
            and owner.selection is not None
            and owner.selection.identity != event.target.identity
        ):
            raise _Rejection(REJECT_SPEC_CHANGED, "This session belongs to another workflow. Start a new session.")
        workspace = owner.require_workspace()
        if (missing := workspace.missing_primary()) is not None:
            raise _Rejection(
                REJECT_WORKING_DIR_MISSING,
                f"The working directory no longer exists: {surrogate_safe_text(missing)}.",
            )
        config_dir = self._config_dir()
        project_cwd = Path(workspace.primary_cwd)
        settings = await self._load_run_settings(project_cwd, request_id=event.request_id)
        if owner.state is not None and owner.state.model != event.target.model:
            raise _Rejection(
                REJECT_SESSION_CHANGED, "The workflow session model changed. Reload the session before starting."
            )
        selected = owner.state.model if owner.state is not None else event.target.model
        settings, model = admission_settings(settings, selected, self._model_registry)
        discovery = await asyncio.to_thread(discover_workflows, config_dir=config_dir, project_cwd=project_cwd)
        source = discovery.find(event.target.workflow_id)
        if source is None:
            raise _Rejection(REJECT_WORKFLOW_NOT_FOUND, f"No workflow named {event.target.workflow_id!r} was found.")
        identity = source.identity
        if owner.selection is not None and owner.selection.identity != identity:
            raise _Rejection(REJECT_SPEC_CHANGED, "This session belongs to another workflow. Start a new session.")
        if event.pins is not None and event.pins.identity != identity:
            raise _Rejection(REJECT_SPEC_CHANGED, "The workflow source differs from the prepared target.")
        active.source = source
        ledger: ConfirmationLedger | None = None
        recorded: LedgerEntry | None = None
        if source.source_kind != SOURCE_KIND_BUILTIN:
            # Even preparation runs code: the metadata can name an arbitrary interpreter executable.
            # Confirm the bytes before probing it; its fingerprint can only be checked after that probe.
            ledger = await asyncio.to_thread(ConfirmationLedger, ledger_path(config_dir))
            recorded = ledger.recorded(source.canonical_path, source.source_kind)
            if recorded is None or recorded.entry_digest != source.source_digest:
                raise _not_confirmed(source)
        try:
            sdk = await materialize_runtime_sdk(config_dir)
            environment = await prepare_workflow_environment(source, sdk=sdk)
        except WorkflowPreviewError as exc:
            raise _Rejection(exc.code, exc.message) from exc
        except OSError as exc:
            raise _Rejection(REJECT_STORAGE_FAILED, f"The workflow SDK could not be materialized: {exc}") from exc
        if event.pins is not None and event.pins.environment_fingerprint != environment.environment_fingerprint:
            raise _Rejection(
                REJECT_ENVIRONMENT_CHANGED, "The workflow's environment differs from the one that was confirmed."
            )
        if recorded is not None and recorded.environment_fingerprint != environment.environment_fingerprint:
            raise _not_confirmed(source)
        callbacks = WorkerCallbacks()
        try:
            loaded = await load_workflow(
                source,
                environment=environment,
                sdk=sdk,
                workspace=project_cwd,
                bytecode_cache=worker_bytecode_cache_dir(config_dir),
                ask_handler=callbacks.ask,
                emit_handler=callbacks.emit,
            )
        except WorkflowPreviewError as exc:
            raise _Rejection(exc.code, exc.message) from exc
        try:
            if event.pins is not None and event.pins.spec_digest != loaded.spec_digest:
                raise _Rejection(REJECT_SPEC_CHANGED, "The workflow file changed since it was confirmed.")
            if ledger is not None and not ledger.is_confirmed(
                ledger_entry_for(source, loaded.load, loaded.spec_digest, environment, title=loaded.manifest["title"])
            ):
                raise _not_confirmed(source)
            try:
                admitted = admit_manifest(
                    loaded.manifest,
                    agent_registry=self._agent_registry or AgentProfileRegistry(),
                    model_registry=self._model_registry,
                    settings=settings,
                )
            except AdmissionError as exc:
                raise _Rejection(exc.code, exc.message) from exc
            try:
                await owner.prepare(
                    run_id=execution.run_id,
                    identity=identity,
                    title=admitted.graph.title,
                    settings=settings,
                    model_registry=self._model_registry,
                    hooks=self._build_hooks,
                    request_id=event.request_id,
                    has_agents=bool(admitted.resolved_nodes()),
                )
            except ValueError as exc:
                raise _Rejection(REJECT_SPEC_CHANGED, str(exc)) from exc
            owner.require_state().model = model
            if self._session.surface is not None:
                # Written by the admission save below; a discarded admission restores the previous surface.
                owner.require_state().last_surface = self._session.surface.value
            session = owner.session
            session_id, session_dir = owner.require_session_id(), owner.require_session_dir()
            header = RunHeader(
                run_id=execution.run_id,
                session_id=session_id,
                workflow_id=source.workflow_id,
                source_kind=source.source_kind,
                canonical_path=source.canonical_path,
                title=admitted.graph.title,
                input_excerpt=event.input_text,
                entry_digest=source.source_digest,
                manifest_digest=loaded.load.manifest_digest,
                schema_version=loaded.manifest["schema_version"],
                spec_digest=loaded.spec_digest,
                started_at=datetime.now(tz=UTC).isoformat(),
                mode=self._mode.value,
                model=model,
            )
            store: WorkflowRunStore | None = None
            directory = run_dir(session_dir, execution.run_id)
            try:
                await finish_close(asyncio.create_task(owner.save()))
                store = WorkflowRunStore.open(
                    directory,
                    header=header,
                    spec=RunSpec(loaded.manifest, asdict(environment), admitted.resolved_nodes()),
                    input_text=event.input_text,
                    source=source.source,
                )
                owner.commit_admission()
            except BaseException as exc:

                async def discard() -> None:
                    if store is not None:
                        await store.close()
                    if directory.exists():
                        await asyncio.to_thread(shutil.rmtree, directory)
                    await owner.discard_admission()
                    if not event.session_id and self._persistence.state_store is not None:
                        await self._persistence.state_store.delete_session(session_id, allow_active=True)

                await finish_close(asyncio.create_task(discard()))
                if isinstance(exc, (WorkflowStorageFailed, OSError)):
                    raise _Rejection(REJECT_STORAGE_FAILED, str(exc)) from exc
                raise
        except BaseException:
            await loaded.client.close()
            raise
        journal = WorkflowJournal(store, self._bus, session_id=session_id)
        resources = AgentNodeResources(
            bus=self._bus,
            session_id=session_id,
            session_dir=session_dir,
            approval_anchor=event.input_text,
            usage_publisher=owner.usage,
            workspace=workspace,
            settings=settings,
            approval_mode=lambda: self._session.approval_mode,
            approval_judge_for=owner.judge_for,
            approval_relay_context=owner.approval_relay_context,
            relay_risk_latch=owner.relay_latch,
            hook_manager=session.hook_manager,
            mutation_tracker=session.mutation_tracker,
            mutation_coordinator=session.mutation_coordinator,
            spill_quota=session.spill_quota,
            background_tasks=session.background_tasks,
            allow_user_interaction=self._allow_user_interaction,
            mcp_cache=self._mcp_cache,
            agent_registry=self._agent_registry,
            model_registry=self._model_registry,
        )
        if owner.trajectory is None:
            raise RuntimeError("Preparing a workflow runner requires a session trajectory.")
        runner = WorkflowRunner(
            admitted=admitted,
            journal=journal,
            worker=loaded.client,
            load_stdout=loaded.load.stdout,
            resources=resources,
            checkpoint=owner.checkpoint,
            mode=self._mode,
            timeout=event.timeout if event.timeout > 0 else None,
            trace=WorkflowTrace(owner.trajectory.context(), run_id=execution.run_id, workflow_id=source.workflow_id),
        )
        callbacks.bind(runner)

        return runner, journal

    async def _reply(self, reply: WorkflowRunAccepted | WorkflowRunRejected) -> None:
        _remember(self._replies, reply.request_id, reply, MAX_REMEMBERED_REPLIES)
        await self._bus.publish(reply)


def _remember[T](cache: OrderedDict[str, T], key: str, value: T, limit: int) -> None:
    cache[key] = value
    while len(cache) > limit:
        cache.popitem(last=False)
