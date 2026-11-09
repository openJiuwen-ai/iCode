# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only TUI projection over backend engine state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from chrys.service.mutations.tracker import MutationTracker

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from chrys.foundation.models.hook_runs import HookRunDetail, HookRunRecord
    from chrys.service.hooks.run_log import HookRunLog
    from chrys.service.mutations.coordination import MutationCoordinator
    from chrys.service.mutations.types import RollbackPlan


class EngineSnapshotSource(Protocol):
    """Engine surface read by the main-screen UI projection."""

    @property
    def mutation_tracker(self) -> MutationTracker | None: ...

    @property
    def mutation_coordinator(self) -> MutationCoordinator | None: ...

    @property
    def current_turn_number(self) -> int: ...

    @property
    def conversation_revision(self) -> int: ...

    @property
    def build_generation(self) -> int: ...

    @property
    def workspace_primary_cwd(self) -> str: ...

    @property
    def hook_runs(self) -> HookRunLog | None: ...

    def available_rollback_turns(self) -> list[int]:
        """Return rollback turn numbers available for display."""

    def turn_prompt_previews(self) -> dict[int, str]:
        """Return prompt previews keyed by turn number."""

    async def refresh_mutation_attribution(self, *, force: bool = False) -> bool:
        """Reclassify against peer claims; persist and report changes."""

    async def begin_rollback_projection(
        self,
        *,
        session_id: str | None,
        session_generation: int,
    ) -> str | None:
        """Fence prompt admission while projecting rollback state."""

    def finish_rollback_projection(self, owner: str) -> None:
        """Release a rollback projection fence."""


@dataclass(frozen=True)
class RollbackReadState:
    """Read-only state needed to open the rollback modal."""

    tracker: MutationTracker
    available_turns: list[int]
    current_turn: int
    conversation_revision: int
    build_generation: int
    workspace_cwd: str
    turn_prompts: dict[int, str]
    # Cross-session coordination hook: ``(plan, cwd) -> plan`` applying
    # the same peer augmentation the engine runs at rollback execution
    # (PEER_MODIFIED_SINCE exclusions + in-flight warnings), so the
    # modal preview stays plan-WYSIWYG.  ``None`` when coordination is
    # disabled.
    plan_augment: Callable[[RollbackPlan, str], RollbackPlan] | None = None
    # ``() -> awaitable[changed]`` re-running peer reclassification via
    # the ENGINE refresh path.  The modal awaits this BEFORE building
    # its plan: a peer claim published after the modal opened can demote
    # local rows (contested/foreign), which reshapes the plan itself —
    # augmentation alone cannot surface that. The first call forces a
    # full refresh; later picker changes use the signature short-circuit.
    # ``None`` when coordination is disabled.
    attribution_refresh: Callable[[], Awaitable[bool]] | None = None
    # Preview projections re-read the live mutation tracker after the initial
    # history/target scan. The controller installs these callbacks so every
    # worker-thread read uses the same backend prompt-admission fence.
    projection_acquire: Callable[[], Awaitable[str]] | None = None
    projection_release: Callable[[str], None] | None = None


class EngineReadModel:
    """TUI-local read model for engine-owned state."""

    def __init__(self, engine: EngineSnapshotSource) -> None:
        self._engine = engine

    @property
    def current_turn_number(self) -> int:
        """Return the current session turn counter without scanning history."""
        return self._engine.current_turn_number

    @property
    def conversation_revision(self) -> int:
        """Return the fresh/retry lifecycle revision without scanning history."""
        return self._engine.conversation_revision

    @property
    def build_generation(self) -> int:
        """Return the successful runtime-build generation."""
        return self._engine.build_generation

    @property
    def workspace_cwd(self) -> str:
        """Return the normalized primary cwd owned by the current runtime build."""
        return self._engine.workspace_primary_cwd

    def hook_runs(self) -> tuple[HookRunRecord, ...]:
        """Every session's hook runs since iCode started, in the Hooks panel's order; in memory, cheap."""
        log = self._engine.hook_runs
        return log.records() if log is not None else ()

    def hook_runs_dropped(self) -> int:
        """Ended runs the run log no longer keeps, past its cap."""
        log = self._engine.hook_runs
        return log.dropped if log is not None else 0

    def hook_run_detail(self, run_id: str) -> HookRunDetail | None:
        """One run's details; None once the run log no longer keeps the run."""
        log = self._engine.hook_runs
        return log.detail(run_id) if log is not None else None

    async def begin_rollback_projection(
        self,
        *,
        session_id: str | None,
        session_generation: int,
    ) -> str | None:
        """Fence prompt admission before reading live session-owned state."""
        return await self._engine.begin_rollback_projection(
            session_id=session_id,
            session_generation=session_generation,
        )

    def finish_rollback_projection(self, owner: str) -> None:
        """Release the prompt-admission fence after projection completes."""
        self._engine.finish_rollback_projection(owner)

    async def load_rollback_state(self) -> RollbackReadState | None:
        """Build rollback state without blocking the Textual event loop.

        Discovering available targets reads every retained rollback snapshot,
        and prompt previews scan the full live and compressed history.  Both
        operations grow with session size, so callers that are opening UI must
        use this deferred projection rather than :meth:`rollback_state`
        directly on the message-pump thread.
        """
        return await asyncio.to_thread(self.rollback_state)

    async def load_available_rollback_turns(self) -> list[int]:
        """Read retained snapshot headers without blocking the message pump."""
        return await asyncio.to_thread(self._engine.available_rollback_turns)

    def rollback_state(self) -> RollbackReadState | None:
        """Return rollback modal state, or ``None`` when rollback is unavailable."""
        tracker = self._engine.mutation_tracker
        available = self._engine.available_rollback_turns()
        if tracker is None or not available:
            return None
        coordinator = self._engine.mutation_coordinator
        plan_augment = None
        attribution_refresh = None
        if coordinator is not None:

            def plan_augment(plan: RollbackPlan, cwd: str) -> RollbackPlan:
                return coordinator.augment_rollback_plan(tracker, plan, scope_paths=[cwd], fallback_root=cwd)

            # The ENGINE refresh, not a raw ``coordinator.reclassify``:
            # the engine persists the session when rows change. Preserve
            # the former pre-modal forced refresh, but defer it into the
            # modal's post-paint worker; later picker changes use the normal
            # signature short-circuit.
            force_next_refresh = True

            async def attribution_refresh() -> bool:
                nonlocal force_next_refresh
                force = force_next_refresh
                force_next_refresh = False
                return await self._engine.refresh_mutation_attribution(force=force)

        return RollbackReadState(
            tracker=tracker,
            available_turns=available,
            current_turn=self._engine.current_turn_number,
            conversation_revision=self._engine.conversation_revision,
            build_generation=self._engine.build_generation,
            workspace_cwd=self._engine.workspace_primary_cwd,
            turn_prompts=self._engine.turn_prompt_previews(),
            plan_augment=plan_augment,
            attribution_refresh=attribution_refresh,
        )
