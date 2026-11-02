# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Permits and owner clocks for serialized runtime lifecycle transitions."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

from chrys.foundation.i18n import MessageRef, msg
from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
from chrys.orchestration.engine.state.active_session import ActiveSession

_RELOAD_DRAIN_FAILED = msg(
    "controls.reload_drain_failed", fallback="Could not finish the previous turn before reloading: {detail}"
)
_RELOAD_DRAIN_CANCELLED = msg(
    "controls.reload_drain_cancelled", fallback="The previous turn was cancelled while settings were waiting to reload."
)

if TYPE_CHECKING:
    from chrys.orchestration.engine.reload_resources import ReloadResources
    from chrys.service.profiles.reload import ProfileReload

RebuildPermitDeniedReason = Literal["shutdown", "session_changed", "superseded", "load_active", "busy", "not_ready"]


@dataclasses.dataclass(frozen=True)
class RebuildControlToken:
    """Captured owner clocks for one runtime-control rebuild request."""

    session_id: str | None
    session_generation: int
    build_generation: int
    load_generation: int


@dataclasses.dataclass(frozen=True)
class RebuildPermit:
    """Serialized rebuild-admission permit."""

    permit_id: int
    owner: str
    token: RebuildControlToken


@dataclasses.dataclass(frozen=True)
class RebuildPermitDenied:
    """Terminal denial for a rebuild-control request."""

    reason: RebuildPermitDeniedReason
    code: str
    message: str
    display_message: MessageRef | None = None


class LifecyclePermits:
    """Issue and validate permits for rebuilds, session transitions, and loads."""

    def __init__(
        self,
        *,
        turn_state: TurnRuntimeState,
        session: ActiveSession,
        turn_executing: Callable[[], bool] = lambda: False,
    ) -> None:
        self._turn_executing = turn_executing
        self._turn_state = turn_state
        self._session = session
        self._agent_loading = False
        self._agent_load_idle = asyncio.Event()
        self._agent_load_idle.set()
        self._session_generation: int = 0
        self._build_generation: int = 0
        self._load_generation: int = 0
        self._rebuild_gate_lock = asyncio.Lock()
        self._next_rebuild_permit_id = 1
        self._staged_profiles: ProfileReload | None = None
        self._staged_reload_resources: ReloadResources | None = None
        self._active_rebuild_permit_id: int | None = None
        self._active_rebuild_permit_owner: str | None = None
        self._active_rebuild_permit_task: asyncio.Task[Any] | None = None
        self._next_session_transition_permit_id = 1
        self._active_session_transition_permit_id: int | None = None
        self._active_session_transition_permit_owner: str | None = None
        self._active_session_transition_permit_task: asyncio.Task[Any] | None = None
        self._active_session_transition_committed = False

    @property
    def session_generation(self) -> int:
        return self._session_generation

    @property
    def build_generation(self) -> int:
        return self._build_generation

    @property
    def load_generation(self) -> int:
        return self._load_generation

    @property
    def agent_loading(self) -> bool:
        return self._agent_loading

    @property
    def gate_lock(self) -> asyncio.Lock:
        return self._rebuild_gate_lock

    def advance_session_generation(self) -> None:
        """Advance the session owner clock; an input still being checked belongs to the old session."""
        self._session_generation += 1
        self._turn_state.input_operations.invalidate_all("session_switched")

    def prompt_admission_owner_for_current_task(self) -> str | None:
        """Return the transition owner held by this task, if any."""
        return (
            self._active_session_transition_permit_owner if self.current_task_owns_session_transition_permit() else None
        )

    def begin_agent_load(self) -> None:
        """Mark agent infrastructure as loading and block new user turns."""
        self._load_generation += 1
        self._agent_loading = True
        self._agent_load_idle.clear()

    def finish_agent_load(self) -> None:
        """Mark the current agent load attempt as settled."""
        self._agent_loading = False
        self._agent_load_idle.set()

    def advance_build_generation(self) -> None:
        """Record one successful replacement of build-scoped collaborators; inputs being checked are stale."""
        self._build_generation += 1
        self._turn_state.input_operations.invalidate_all("rebuilt")

    def invalidate_for_session_transition_pre_shutdown(self) -> None:
        """Compatibility seam for state-only tests that exercise pre-shutdown invalidation.

        Real lifecycle transitions use ``begin_session_transition()``, which also
        closes prompt/retry admission under the serialized gate before invalidating
        session-owned turn state.
        """
        old_generation = self._session_generation
        self.advance_session_generation()
        self._turn_state.lease.invalidate_for_session_transition_pre_shutdown(
            old_session_generation=old_generation,
        )

    async def begin_session_transition(self, operation: str) -> str:
        """Acquire the serialized session-transition boundary and close turn admission."""
        owner = await self.prepare_session_transition(operation)
        if owner is None:  # No owner token was supplied, so validation cannot fail.
            raise RuntimeError("Session transition acquisition unexpectedly failed")
        try:
            self.commit_session_transition(owner)
        except BaseException:
            self.abort_prepared_session_transition(owner)
            raise
        return owner

    async def prepare_session_transition_if_current(
        self,
        operation: str,
        *,
        session_id: str | None,
        session_generation: int,
    ) -> str | None:
        """Fence new prompts and drain prior admissions without invalidating turn state."""
        return await self.prepare_session_transition(
            operation,
            expected_owner=(session_id, session_generation),
            wait_for_active_admissions=True,
        )

    async def prepare_session_transition(
        self,
        operation: str,
        *,
        expected_owner: tuple[str | None, int] | None = None,
        wait_for_active_admissions: bool = False,
    ) -> str | None:
        """Acquire the shared gate and close new admission without committing."""
        await self._rebuild_gate_lock.acquire()
        if expected_owner is not None and expected_owner != (self._session.session_id, self._session_generation):
            self._rebuild_gate_lock.release()
            return None
        permit_id = self._next_session_transition_permit_id
        self._next_session_transition_permit_id += 1
        owner = f"session:{operation}:{permit_id}"
        self._active_session_transition_permit_id = permit_id
        self._active_session_transition_permit_owner = owner
        self._active_session_transition_permit_task = asyncio.current_task()
        self._active_session_transition_committed = False
        self._turn_state.lease.close_prompt_admission_for_rebuild(owner)
        try:
            if wait_for_active_admissions:
                await self._turn_state.lease.wait_for_active_admissions_idle()
            await self._turn_state.lease.wait_for_active_injection_commits_idle()
            if expected_owner is not None and expected_owner != (self._session.session_id, self._session_generation):
                self.abort_prepared_session_transition(owner)
                return None
        except BaseException:
            self.abort_prepared_session_transition(owner)
            raise
        return owner

    def commit_session_transition(self, owner: str) -> None:
        """Invalidate the old session generation after a prepared transition is accepted."""
        if (
            self._active_session_transition_permit_owner != owner
            or not self.current_task_owns_session_transition_permit()
            or self._active_session_transition_committed
        ):
            raise RuntimeError("Invalid session transition permit")
        self._active_session_transition_committed = True
        old_generation = self._session_generation
        self.advance_session_generation()
        self._turn_state.lease.invalidate_for_session_transition_pre_shutdown(
            old_session_generation=old_generation,
            prompt_admission_owner=owner,
        )

    def abort_prepared_session_transition(self, owner: str) -> None:
        """Release a prepared transition without changing session ownership."""
        self._turn_state.lease.reopen_prompt_admission_after_rebuild(owner)
        self._active_session_transition_permit_id = None
        self._active_session_transition_permit_owner = None
        self._active_session_transition_permit_task = None
        self._active_session_transition_committed = False
        self._rebuild_gate_lock.release()

    def finish_session_transition(self, owner: str) -> None:
        """Release the serialized session-transition boundary and reopen admission."""
        if self._active_session_transition_permit_owner != owner:
            return
        self._turn_state.lease.reopen_prompt_admission_after_rebuild(owner)
        self._active_session_transition_permit_id = None
        self._active_session_transition_permit_owner = None
        self._active_session_transition_permit_task = None
        self._active_session_transition_committed = False
        self._rebuild_gate_lock.release()

    async def wait_for_agent_load_idle(self) -> None:
        """Wait until no agent build/rebuild is in progress."""
        while self._agent_loading:
            await self._agent_load_idle.wait()

    def capture_control_token(self) -> RebuildControlToken:
        """Capture owner clocks for one runtime-control rebuild request."""
        return RebuildControlToken(
            session_id=self._session.session_id,
            session_generation=self._session_generation,
            build_generation=self._build_generation,
            load_generation=self._load_generation,
        )

    async def acquire_rebuild_permit(
        self,
        token: RebuildControlToken,
        *,
        reject_running: bool = False,
    ) -> RebuildPermit | RebuildPermitDenied:
        """Close turn admission and acquire the serialized rebuild boundary."""
        await self._rebuild_gate_lock.acquire()
        permit_id = self._next_rebuild_permit_id
        self._next_rebuild_permit_id += 1
        owner = f"rebuild:{permit_id}"
        self._turn_state.lease.close_prompt_admission_for_rebuild(owner)
        try:
            await self._turn_state.lease.wait_for_active_admissions_idle()
            if reject_running and (self._turn_executing() or self._turn_state.lease.workflow is not None):
                return self._deny_rebuild_permit_locked(
                    owner, reason="busy", message="Reload when the current turn or workflow finishes."
                )
            await self._turn_state.lease.wait_for_active_injection_commits_idle()
            try:
                drain_outcome = await self._turn_state.drain_for_boundary()
            except Exception as exc:
                return self._deny_rebuild_permit_locked(
                    owner,
                    reason="busy",
                    message=f"Cannot rebuild because the active run failed: {exc}",
                    display_message=_RELOAD_DRAIN_FAILED.bind(detail=str(exc)),
                )
            await self._turn_state.lease.wait_for_active_injection_commits_idle()
            await self.wait_for_agent_load_idle()
            denied = self._validate_rebuild_token_after_boundary(token, drain_cancelled=drain_outcome.cancelled)
            if denied is not None:
                return self._deny_rebuild_permit_locked(owner, denied=denied)
            permit = RebuildPermit(permit_id=permit_id, owner=owner, token=token)
            self._active_rebuild_permit_id = permit_id
            self._active_rebuild_permit_owner = owner
            self._active_rebuild_permit_task = asyncio.current_task()
            return permit
        except BaseException:
            self._turn_state.lease.reopen_prompt_admission_after_rebuild(owner)
            self._rebuild_gate_lock.release()
            raise

    def _validate_rebuild_token_after_boundary(
        self,
        token: RebuildControlToken,
        *,
        drain_cancelled: bool,
    ) -> RebuildPermitDenied | None:
        if self._session.shutting_down:
            return self._rebuild_denied(
                "shutdown",
                "runtime_mutation_shutdown",
                "Cannot rebuild while the engine is shutting down.",
            )
        if self._session.session_id != token.session_id or self._session_generation != token.session_generation:
            return self._rebuild_denied(
                "session_changed",
                "runtime_mutation_session_changed",
                "Cannot rebuild because the active session changed.",
            )
        if self._build_generation != token.build_generation:
            return self._rebuild_denied(
                "superseded",
                "runtime_mutation_superseded",
                "Cannot rebuild because a newer runtime is already active.",
            )
        if self._load_generation != token.load_generation:
            return self._rebuild_denied(
                "superseded",
                "runtime_mutation_superseded",
                "Cannot rebuild because a newer load attempt already settled.",
            )
        if self._agent_loading:
            return self._rebuild_denied(
                "load_active",
                "runtime_mutation_load_active",
                "Cannot rebuild while an agent load is still active.",
            )
        if self._turn_state.lease.active_admission_count() > 0:
            return self._rebuild_denied(
                "busy",
                "runtime_mutation_busy",
                "Cannot rebuild while a prompt or retry is being admitted.",
            )
        run_task = self._turn_state.lease.run_task
        if run_task is not None and not run_task.done():
            return self._rebuild_denied(
                "busy",
                "runtime_mutation_busy",
                "Cannot rebuild while a run is active.",
            )
        if self._turn_state.lease.workflow is not None:
            return self._rebuild_denied(
                "busy",
                "runtime_mutation_busy",
                "Cannot rebuild while a workflow run is active. Cancel the workflow run first.",
            )
        if drain_cancelled:
            return self._rebuild_denied(
                "busy",
                "runtime_mutation_busy",
                "Cannot rebuild because the active run was cancelled.",
                display_message=_RELOAD_DRAIN_CANCELLED.bind(),
            )
        return None

    def _rebuild_denied(
        self,
        reason: RebuildPermitDeniedReason,
        code: str,
        message: str,
        display_message: MessageRef | None = None,
    ) -> RebuildPermitDenied:
        return RebuildPermitDenied(reason=reason, code=code, message=message, display_message=display_message)

    def _deny_rebuild_permit_locked(
        self,
        owner: str,
        *,
        reason: RebuildPermitDeniedReason | None = None,
        message: str | None = None,
        denied: RebuildPermitDenied | None = None,
        display_message: MessageRef | None = None,
    ) -> RebuildPermitDenied:
        self._turn_state.lease.reopen_prompt_admission_after_rebuild(owner)
        self._rebuild_gate_lock.release()
        if denied is not None:
            return denied
        return self._rebuild_denied(
            reason or "busy",
            "runtime_mutation_busy",
            message or "Cannot rebuild while the runtime is busy.",
            display_message,
        )

    def stage_profiles(self, permit: RebuildPermit, profiles: ProfileReload) -> None:
        """Attach a candidate only to the owning rebuild, until it releases the permit."""
        self.ensure_rebuild_permit(permit)
        self._staged_profiles = profiles

    def staged_profiles(self) -> ProfileReload | None:
        return self._staged_profiles if self.current_task_owns_rebuild_permit() else None

    def stage_reload_resources(self, permit: RebuildPermit, resources: ReloadResources) -> None:
        """Mark the owning rebuild as a manual reload, until it releases the permit."""
        self.ensure_rebuild_permit(permit)
        self._staged_reload_resources = resources

    def staged_reload_resources(self) -> ReloadResources | None:
        return self._staged_reload_resources if self.current_task_owns_rebuild_permit() else None

    def release_rebuild_permit(self, permit: RebuildPermit) -> None:
        """Release a serialized rebuild permit and reopen prompt admission."""
        if self._active_rebuild_permit_id != permit.permit_id:
            return
        self._turn_state.lease.reopen_prompt_admission_after_rebuild(permit.owner)
        self._staged_profiles = None
        self._staged_reload_resources = None
        self._active_rebuild_permit_id = None
        self._active_rebuild_permit_owner = None
        self._active_rebuild_permit_task = None
        self._rebuild_gate_lock.release()

    def current_task_owns_rebuild_permit(self) -> bool:
        try:
            task = asyncio.current_task()
        except RuntimeError:
            return False
        return (
            task is not None and self._active_rebuild_permit_id is not None and self._active_rebuild_permit_task is task
        )

    def current_task_owns_session_transition_permit(self) -> bool:
        try:
            task = asyncio.current_task()
        except RuntimeError:
            return False
        return (
            task is not None
            and self._active_session_transition_permit_id is not None
            and self._active_session_transition_permit_task is task
        )

    def ensure_rebuild_permit(self, permit: RebuildPermit) -> None:
        if (
            self._active_rebuild_permit_id != permit.permit_id
            or self._active_rebuild_permit_owner != permit.owner
            or not self.current_task_owns_rebuild_permit()
        ):
            raise RuntimeError("Invalid rebuild permit")
