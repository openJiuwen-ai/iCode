# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Active session state and its derived filesystem paths."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workspace import Workspace
from chrys.orchestration.session_resources import SessionResources
from chrys.service.approval.policy import ApprovalMode
from chrys.service.session.runtime_metadata import SessionRuntimeMetadata

if TYPE_CHECKING:
    from chrys.foundation.config.settings import Settings
    from chrys.orchestration.engine.build.construction import StagedBuild
    from chrys.service.mutations.tracker import MutationTracker
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.session.persistence import SessionPersistence
    from chrys.service.todos.tracker import TodoTracker


def session_pin_overrides(session: ActiveSession, settings: Settings) -> dict[str, Any]:
    """Snapshot the per-session pins every re-load of *session*'s settings must carry.

    *settings* is the layered load (``SettingsHandle.base``), never the
    effective value: a session's own model (``Source.SESSION`` in the handle)
    is kept apart from the load and is not a pin. Pins travel only while
    pinned, so an unpinned session picks up a changed env value on its next
    load:

    * ``model_profile`` / ``model_profile_override`` — pinned by a caller that
      owns the model for the session's host (an ACP model switch, headless
      ``--model``).
    * ``ask_user_timeout_seconds`` — pinned when ACP owns the ask_user lifetime
      (set via dataclasses.replace at launch — see app/cli/acp.py).
    * ``frontend_default_max_transient_retries`` — launch-mode policy; travels
      as the reload eval context instead, and the separate env-derived override
      is deliberately re-read.
    """
    overrides: dict[str, Any] = {}
    if session.ask_user_timeout_pinned:
        overrides["ask_user_timeout_seconds"] = settings.ask_user_timeout_seconds
    if session.model_profile_pinned:
        overrides["model_profile"] = settings.model_profile
        overrides["model_profile_override"] = settings.model_profile_override
        overrides["model_profile_override_sub_agents"] = settings.model_profile_override_sub_agents
    return overrides


class ActiveSession(SessionResources[SessionRuntimeMetadata]):
    """Hold the active session's identity, bookkeeping, and lock ownership."""

    def __init__(
        self,
        *,
        persistence: SessionPersistence,
        workspace: Workspace | None,
        approval_mode: ApprovalMode | None,
        surface: SessionSurface | None = None,
    ) -> None:
        super().__init__(
            persistence=persistence,
            runtime_meta=SessionRuntimeMetadata(),
            workspace=workspace,
        )
        # Launch policy survives session resets and is never restored from checkpoints.
        self.approval_mode = approval_mode or ApprovalMode.MANUAL
        # The surface this launch serves. Opening or saving a session never
        # records it; a turn does, through ``mark_surface``.
        self.surface = surface
        self._surface_mark: tuple[str, SessionSurface] | None = None
        self.agent_profile: AgentProfile | None = None
        # The agent a new session starts on: the launch profile, then the user's
        # own pick (``#``). Survives /new and restore, like the approval mode; a
        # prompt shortcut's switch and a restored session's own agent change only
        # ``agent_profile``. Empty until the launch profile is known.
        self.new_session_agent: str = ""
        self.shutting_down: bool = False
        self.turn_number: int = 0
        self.todo_tracker: TodoTracker | None = None
        # Set only by a per-session ACP model switch (SetModelProfile) or a caller
        # that owns the model (headless ``--model``). When True, SettingsReload
        # keeps the live ``settings.model_profile`` instead of reverting to the
        # global default. A session's own model in the TUI (a prompt shortcut's,
        # a restored session's) is not a pin: it lives in the settings handle's
        # session layer.
        self.model_profile_pinned: bool = False
        # Set via ``pin_ask_user_timeout`` when the caller owns
        # ``ask_user_timeout_seconds`` out-of-band (ACP injects it via
        # dataclasses.replace at launch, not env). When True, SettingsReload
        # preserves the live value instead of reverting to the env default; TUI/CLI
        # leave it False so a changed CHRYS_ASK_USER_TIMEOUT_SECONDS takes effect.
        self.ask_user_timeout_pinned: bool = False
        self.recovered_from_sidecar: bool = False
        # When True, ``_save_current_session`` is a no-op.  Set by the
        # rollback handler immediately before it calls the session
        # restore path, which would otherwise call ``shutdown()`` →
        # ``_save_current_session()`` and clobber the just-swapped
        # ``session.json`` with the in-memory pre-rollback state.
        self.suppress_save: bool = False
        # ``session_end`` fires once per session: normally from ``shutdown()``,
        # but earlier when the live session is deleted underneath the engine
        # (clear / delete-current), so hooks still see the id and the files.
        self.session_end_fired = False
        # Background outbox-recovery task kicked off in ``start()``.
        # Held here so it isn't garbage-collected mid-run (asyncio only
        # weak-refs tasks created via ``create_task``).
        self.outbox_recovery_task: asyncio.Task[int] | None = None

    def begin(self, *, agent_profile: AgentProfile, workspace: Workspace | None) -> None:
        """Initialize the identity and defaults for a session start."""
        self.session_end_fired = False
        self.agent_profile = agent_profile
        if self.workspace is None and workspace is None:
            # A fresh default, not a candidate: with no live workspace there is
            # nothing a failed build could corrupt, and error paths after such a
            # failure have always been able to read the ambient root from here.
            self.workspace = Workspace.from_cwd()
        if self.session_id is None:
            from uuid import uuid4

            self.session_id = str(uuid4())

    def install_build(
        self, staged: StagedBuild, *, mutation_tracker: MutationTracker | None, todo_tracker: TodoTracker | None
    ) -> None:
        """Install the session fields belonging to a completed build."""
        self.agent_profile = staged.agent_profile
        self.workspace = staged.workspace
        self.hook_manager = staged.hook_manager
        self.mutation_coordinator = staged.mutation_coordinator
        if mutation_tracker is not None:
            self.mutation_tracker = mutation_tracker
        if todo_tracker is not None:
            self.todo_tracker = todo_tracker

    def reset(self, *, session_id: str | None, workspace: Workspace | None) -> None:
        """Reset session identity and bookkeeping before a fresh start."""
        self.shutting_down = False
        self.workspace = workspace or Workspace.from_cwd()
        self.session_id = session_id
        self._surface_mark = None
        self.reset_spill_quota()
        self.turn_number = 0
        self.runtime_meta = SessionRuntimeMetadata()
        self.mutation_tracker = None
        self.mutation_coordinator = None
        self.todo_tracker = None
        self.recovered_from_sidecar = False

    def adopt_restore_identity(self, *, session_id: str, recovered_from_sidecar: bool) -> None:
        """Adopt the restore identity before hydrating its state."""
        self.shutting_down = False
        self.session_id = session_id
        # Reopening is not a turn, even of the session last marked here:
        # another launch may have worked in it meanwhile.
        self._surface_mark = None
        self.reset_spill_quota()
        self.recovered_from_sidecar = recovered_from_sidecar

    def restore_position(self, *, runtime_meta: SessionRuntimeMetadata, turn_number: int) -> None:
        """Restore the saved runtime metadata and turn position together."""
        self.runtime_meta = runtime_meta
        self.turn_number = turn_number

    def mark_surface(self) -> None:
        """Record that a turn of the current session runs on this launch's surface."""
        if self.surface is not None and self.session_id is not None:
            self._surface_mark = (self.session_id, self.surface)

    def marked_surface(self) -> SessionSurface | None:
        """The surface a save of the current session records; ``None`` keeps the stored one.

        A reset or restore clears the mark; it also names its session, so no
        other identity change can hand it to another session.
        """
        mark = self._surface_mark
        if mark is None or mark[0] != self.session_id:
            return None
        return mark[1]

    def mark_closing(self) -> None:
        """Mark entry into session teardown."""
        self.shutting_down = True

    def mark_session_end_fired(self) -> None:
        """Record that the session end hooks have fired."""
        self.session_end_fired = True

    def mark_recovered_from_sidecar(self, recovered: bool) -> None:
        """Record which persistence source currently wins."""
        self.recovered_from_sidecar = recovered

    def detach_for_delete(self) -> str | None:
        """Detach before deletion and return the previous identity."""
        session_id = self.session_id
        self.session_id = None
        return session_id

    def reattach_after_failed_delete(self, session_id: str | None) -> None:
        """Restore identity and rearm end hooks after an ordinary delete failure."""
        self.session_id = session_id
        self.session_end_fired = False

    @contextmanager
    def saves_suppressed(self) -> Generator[None]:
        """Suppress saves within this scope, preserving an enclosing scope."""
        previous = self.suppress_save
        self.suppress_save = True
        try:
            yield
        finally:
            self.suppress_save = previous
