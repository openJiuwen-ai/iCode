# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Locked ownership of a workflow session, independent of the chat conversation.

History is read directly from the store. Runs and settings/file operations hold
the session guard and drain their resources before releasing it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from chrys.foundation.models.workflow_session import (
    WorkflowIdentity,
    WorkflowModelSelection,
    WorkflowSessionSelection,
    WorkspaceSnapshot,
)
from chrys.foundation.trajectory.event_types import RuntimeFinishReason
from chrys.foundation.util.once_close import finish_close
from chrys.orchestration.invoker.resources import ResourceScope
from chrys.orchestration.session_resources import SessionResources
from chrys.orchestration.session_usage import SessionUsagePublisher
from chrys.service.approval.judge import ApprovalJudge
from chrys.service.approval.relay_risk import RelayRiskLatch
from chrys.service.context.compaction.spill import reconcile_spill_storage
from chrys.service.llm.route_sessions import derive_llm_route_session_id
from chrys.service.mutations.coordination import ATTRIBUTION_DIR_NAME, MutationCoordinator
from chrys.service.mutations.store import SnapshotPolicy, SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.profiles.models.resolver import resolve_active_profile, resolve_judge_profile
from chrys.service.session.runtime_metadata import SessionUsageMetadata
from chrys.service.state.workflow import WorkflowSessionState
from chrys.service.tools.background import BackgroundTasks
from chrys.service.trajectory.session import SessionStartInfo, SessionTrajectory
from chrys.service.workflows.orphans import reconcile_orphaned_runs

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.foundation.config.settings import Settings
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.models.workspace import Workspace
    from chrys.service.hooks.manager import HookManager
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.schema import ModelProfile
    from chrys.service.session.persistence import SessionPersistence


class HookFactory(Protocol):
    async def __call__(
        self, *, project_root: str, project_hooks_enabled: bool, session_id: str, request_id: str = ""
    ) -> HookManager | None: ...


class WorkflowSessionNotFound(ValueError):
    """The selected session no longer has a readable checkpoint."""


class WorkflowSessionInUse(ValueError):
    """Another owner holds the selected session's guard."""


class WorkflowWorkspaceLocked(ValueError):
    """An existing workflow session cannot acquire a different workspace."""


class WorkflowSessionOwner:
    def __init__(
        self,
        *,
        bus: EventBus,
        persistence: SessionPersistence,
        session_id: str,
        workspace: Workspace | None,
    ) -> None:
        self.restoring = bool(session_id)
        self.persistence = persistence
        self.session = SessionResources(
            runtime_meta=SessionUsageMetadata(),
            persistence=persistence,
            workspace=deepcopy(workspace),
            session_id=session_id or str(uuid4()),
        )
        self.usage = SessionUsagePublisher(bus=bus, session=self.session)
        self.trajectory: SessionTrajectory | None = None
        self._judge_model: Callable[[ModelProfile | None], ModelProfile | None] | None = None
        self._run_model: ModelProfile | None = None
        # One latch for the owner's lifetime: relay risk discovered in any run
        # (relay node model, relay node child) persists through runtime_meta,
        # which restored sessions reload.
        self._relay_latch = RelayRiskLatch(on_set=lambda: setattr(self.session.runtime_meta, "relay_risk_seen", True))
        self._judges: dict[str, ApprovalJudge] = {}
        self._auto_review = "direct"
        self.state: WorkflowSessionState | None = None
        self.title = ""
        self.hooks_transferred = False
        self._lifecycle_lock = asyncio.Lock()
        self._opened = False
        self._admission_checkpoint: WorkflowSessionState | None = None
        self._resources = ResourceScope()
        self._resources.own(self._release_guard)
        self._resources.own(self._close_trajectory)
        self._resources.own(self.usage.settle)

    def require_session_id(self) -> str:
        session_id = self.session.session_id
        if not session_id:
            raise RuntimeError("The workflow session has no id.")
        return session_id

    def require_session_dir(self) -> Path:
        directory = self.session.session_dir
        if directory is None:
            raise RuntimeError("The workflow session has no directory.")
        return directory

    def require_workspace(self) -> Workspace:
        workspace = self.session.workspace
        if workspace is None:
            raise RuntimeError("The workflow session has no workspace.")
        return workspace

    def require_state(self) -> WorkflowSessionState:
        if self.state is None:
            raise RuntimeError("The workflow session has not been prepared.")
        return self.state

    @property
    def identity(self) -> WorkflowIdentity | None:
        return self.state.identity if self.state is not None else None

    @property
    def selection(self) -> WorkflowSessionSelection | None:
        session_id = self.session.session_id
        return self.state.selection(session_id) if self.state is not None and session_id is not None else None

    async def open(self, *, reconcile: bool = False) -> None:
        """Serialize restoration with settings writes and guard release."""
        async with self._lifecycle_lock:
            if self._resources.closing:
                raise ValueError("The workflow session is closing.")
            await self._open(reconcile=reconcile)
            self._opened = True

    async def _open(self, *, reconcile: bool) -> None:
        """Lock and restore the workspace before any discovery, settings or worker loading."""
        session = self.session
        self.require_session_id()
        store = self.persistence.state_store
        if store is None:
            raise RuntimeError("The workflow session requires a state store.")
        await finish_close(asyncio.create_task(self._acquire_guard()))
        if self.restoring:
            state = await store.load_workflow_session(self.require_session_id())
            if state is None:
                raise WorkflowSessionNotFound("The workflow session no longer exists. Start a new session.")
            self.state = state
            if session.workspace is not None and WorkspaceSnapshot.capture(session.workspace) != self.state.workspace:
                raise WorkflowWorkspaceLocked(
                    "This workflow session's workspace is fixed. Start a new session to change it."
                )
            session.workspace = self.state.workspace.materialize()
            if reconcile:
                await finish_close(asyncio.create_task(asyncio.to_thread(self._reconcile_storage)))
        if session.workspace is None:
            raise ValueError("A workflow needs a workspace.")
        self._admission_checkpoint = deepcopy(self.state)

    async def prepare(
        self,
        *,
        run_id: str,
        identity: WorkflowIdentity,
        title: str,
        settings: Settings,
        model_registry: ModelProfileRegistry | None,
        has_agents: bool,
        hooks: HookFactory,
        request_id: str = "",
    ) -> None:
        session = self.session
        session_id = self.require_session_id()
        session_dir = self.require_session_dir()
        workspace = self.require_workspace()
        if self.state is not None and self.state.identity != identity:
            raise ValueError("This session belongs to another workflow. Start a new session.")
        if self.state is None:
            self.state = WorkflowSessionState(identity, WorkspaceSnapshot.capture(workspace))
        self.title = title
        self.state.run_count += 1
        self.state.latest_run_id = run_id
        session.runtime_meta = self.state.runtime or SessionUsageMetadata()
        snapshots = SnapshotStore(session_dir, policy=SnapshotPolicy.from_settings(settings))
        mutations = self.state.mutations
        session.mutation_tracker = (
            MutationTracker.deserialize(mutations, snapshots) if mutations is not None else MutationTracker(snapshots)
        )
        session.mutation_tracker.start_workflow_run(run_id)
        if settings.mutation_coordination:
            session.mutation_coordinator = MutationCoordinator(
                registry_root=session_dir.parent / ATTRIBUTION_DIR_NAME,
                session_id=session_id,
            )
        self._resources.own(self._close_mutations)
        if session.background_tasks is None:
            session.background_tasks = BackgroundTasks(
                session_dir, trajectory=lambda: self.trajectory.context() if self.trajectory is not None else None
            )
            self._resources.own(session.background_tasks.close)
        session.hook_manager = await hooks(
            project_root=workspace.primary_cwd,
            project_hooks_enabled=settings.project_hooks_enabled,
            session_id=session_id,
            request_id=request_id,
        )
        self._resources.own(self._close_hooks)
        if has_agents:
            self._prepare_agents(settings, model_registry)
        cwd = self.require_workspace().primary_cwd
        session_id = self.require_session_id()
        session_dir = self.require_session_dir()
        self.trajectory = SessionTrajectory(
            session_id=session_id,
            session_dir=session_dir,
            write_lock_path=session.session_write_lock_path(session_id),
            session_start_info=lambda: SessionStartInfo(cwd, "", ""),
        )

    def _reconcile_storage(self) -> None:
        session = self.session
        directory = self.require_session_dir()
        try:
            reconcile_orphaned_runs(directory)
        except OSError, RuntimeError, UnicodeError, ValueError:
            logger.warning("Unable to reconcile workflow runs under %s", directory, exc_info=True)
        try:
            reconcile_spill_storage(directory, session.spill_quota)
        except OSError, RuntimeError, UnicodeError:
            logger.warning("Unable to reconcile spill storage under %s", directory, exc_info=True)
            session.spill_quota.disable_storage()

    def _prepare_agents(self, settings: Settings, model_registry: ModelProfileRegistry | None) -> None:
        run_model = resolve_active_profile(model_registry, settings)
        self._run_model = run_model

        def judge_model(node_model: ModelProfile | None) -> ModelProfile | None:
            # None while a relay node model has no independent judge: the node
            # runs without one and AUTO approval defers to the user. The
            # latched history risk takes part: a relay run must not fall back
            # to a judge the relay operator controls once the risk is known.
            return resolve_judge_profile(
                model_registry,
                settings,
                run_model if node_model is None else node_model,
                relay_history_risk=self.session.runtime_meta.relay_risk_seen,
            )

        self._judge_model = judge_model
        self._auto_review = settings.approval_auto_review
        self._judges = {}

    def approval_relay_context(self, node_model: ModelProfile | None) -> tuple[bool, str]:
        """Relay-enhancement flag and reviewed model id for a node's approval.

        Enhanced when the node's own model is a relay, the run's model is one,
        or the session already latched relay risk (earlier runs, restored
        sessions): a relay run cannot launder a protected call through a node
        on a direct-connected model, and relay-sourced content in the run's
        conversation keeps the strict review on after a switch to direct
        models. A relay node model latches the same session fact for what
        follows it. The reviewed model is named only when it is itself
        relay-routed; otherwise the review prompt describes the latched
        history risk instead.
        """
        model = self._run_model if node_model is None else node_model
        run_relay = self._run_model is not None and self._run_model.is_relay
        model_relay = model is not None and model.is_relay
        if model_relay:
            self._relay_latch.set()
        enhanced = run_relay or model_relay or bool(self._relay_latch) or self.session.runtime_meta.relay_risk_seen
        return enhanced, (model.model_id if model_relay else "")

    @property
    def relay_latch(self) -> RelayRiskLatch:
        """The session's one-way relay-risk latch, wired into every node build."""
        return self._relay_latch

    def judge_for(self, node_model: ModelProfile | None) -> ApprovalJudge | None:
        """The approval judge for a node that runs on ``node_model``; ``None`` before a run with agents.

        Nodes that resolve to the same judge model share one judge.

        With no judge model configured, the judge follows its node. A run needs no model of its
        own when every node brings one (its own, or its agent profile's), and a judge left on the
        run's missing model would fail every call those nodes make. A node without a model (ACP)
        gets the run's.
        """
        if self._judge_model is None:
            return None
        session = self.session
        session_id = self.require_session_id()
        profile = self._judge_model(node_model)
        if profile is None:
            return None
        judge = self._judges.get(profile.id)
        if judge is None:
            judge = ApprovalJudge(
                profile,
                auto_review=self._auto_review,
                session_id=derive_llm_route_session_id(session_id, route_kind="approval-judge", model_profile=profile),
                parent_session_id=session_id,
                session_dir=session.session_dir,
            )
            self._resources.own(judge.aclose)
            self._judges[profile.id] = judge
        return judge

    async def checkpoint(self) -> None:
        """Drain run-owned publishers and hooks, then save before publishing the run terminal.

        The session lock stays open until the runner records the final outcome.
        """
        if self.session.hook_manager is not None:
            await self.session.hook_manager.drain_session(close=False)
        await self.usage.settle()
        await self.save()

    def _require_open(self) -> None:
        if not self._opened or self._resources.closing:
            raise ValueError("The workflow session is not open.")
        if not self._lifecycle_lock.locked():
            raise RuntimeError("Session writes require the lifecycle lock")

    @asynccontextmanager
    async def edit(self) -> AsyncGenerator[bool]:
        """Hold the lifecycle lock for settings/file operations; decline a closing owner.

        Settings methods below run inside this scope. Run checkpoints acquire
        the same lock themselves, and close waits for every admitted edit.
        """
        async with self._lifecycle_lock:
            yield self._opened and not self._resources.closing

    async def set_model(self, model: WorkflowModelSelection) -> None:
        """Keep the session lock through the actual write, including caller cancellation."""
        await finish_close(asyncio.create_task(self._set_model(model)))

    async def _set_model(self, model: WorkflowModelSelection) -> None:
        self._require_open()
        if self.state is None:
            raise ValueError("The workflow session has not been prepared.")
        checkpoint = deepcopy(self.state)
        checkpoint.model = model
        await self._write_state(checkpoint)
        self.state = checkpoint

    async def save_mutations(self, mutations: dict[str, Any]) -> None:
        """Save a rollback's attribution under the caller's edit scope."""
        await finish_close(asyncio.create_task(self._save_mutations(mutations)))

    async def _save_mutations(self, mutations: dict[str, Any]) -> None:
        self._require_open()
        checkpoint = deepcopy(self.require_state())
        checkpoint.mutations = mutations
        await self._write_state(checkpoint)
        self.state = checkpoint

    async def save(self) -> None:
        await finish_close(asyncio.create_task(self._save()))

    async def _save(self) -> None:
        async with self._lifecycle_lock:
            self._require_open()
            session = self.session
            state = self.require_state()
            state.runtime = session.runtime_meta
            if session.mutation_tracker is not None:
                state.mutations = session.mutation_tracker.serialize()
            await self._write_state(state)

    def commit_admission(self) -> None:
        """The run is admitted; subsequent writes belong to this run's state."""
        self._admission_checkpoint = None

    async def _write_state(self, state: WorkflowSessionState) -> None:
        store = self.persistence.state_store
        if store is None:
            raise RuntimeError("The workflow session requires a state store.")
        self.require_workspace()
        await store.save_workflow_session(self.require_session_id(), state, title=self.title)

    async def discard_admission(self) -> None:
        """Undo a failed admission save while still holding the session lock.

        A save may fail after replacement (for example during a durability check).
        Restore the previous checkpoint so no rejected run remains in the summary.
        """
        await finish_close(asyncio.create_task(self._discard_admission()))

    async def _discard_admission(self) -> None:
        async with self._lifecycle_lock:
            self._require_open()
            if self._admission_checkpoint is None:
                return
            try:
                await self._write_state(self._admission_checkpoint)
                self.state = deepcopy(self._admission_checkpoint)
            except OSError:
                logger.exception("Unable to restore the session checkpoint after failed workflow admission")

    async def _acquire_guard(self) -> None:
        session_id = self.require_session_id()
        if not await asyncio.to_thread(self.session.guard.ensure, session_id):
            raise WorkflowSessionInUse(self.session.guard.conflict_message(session_id))

    async def _release_guard(self) -> None:
        self.session.guard.release()

    async def _close_trajectory(self) -> None:
        if self.trajectory is not None:
            await self.trajectory.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)

    async def _close_hooks(self) -> None:
        if not self.hooks_transferred and self.session.hook_manager is not None:
            await self.session.hook_manager.drain_session()

    async def _close_mutations(self) -> None:
        if self.session.mutation_coordinator is not None:
            await asyncio.to_thread(self.session.mutation_coordinator.close)

    async def close(self) -> None:
        async with self._lifecycle_lock:
            # Finish earlier writes and fence later ones before releasing the
            # guard. Drain outside the lock: publishers may call back inline.
            self._opened = False
        await self._resources.aclose()
