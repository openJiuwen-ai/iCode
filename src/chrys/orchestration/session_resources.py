# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared session identity, workspace, accounting and filesystem resources."""

from __future__ import annotations

from typing import TYPE_CHECKING

from chrys.service.context.compaction.spill import SpillQuota
from chrys.service.session.runtime_metadata import SessionUsageMetadata
from chrys.service.state.locks import ActiveSessionGuard
from chrys.service.state.store import session_write_lock_path

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.foundation.models.workspace import Workspace
    from chrys.service.hooks.manager import HookManager
    from chrys.service.mutations.coordination import MutationCoordinator
    from chrys.service.mutations.tracker import MutationTracker
    from chrys.service.session.persistence import SessionPersistence
    from chrys.service.tools.background import BackgroundTasks


class SessionResources[Metadata: SessionUsageMetadata]:
    """Resources used by either mode; contains no conversation, turn or agent state."""

    def __init__(
        self,
        *,
        persistence: SessionPersistence,
        runtime_meta: Metadata,
        workspace: Workspace | None,
        session_id: str | None = None,
    ) -> None:
        self._persistence = persistence
        self._workspace = workspace
        self.session_id: str | None = session_id
        self.runtime_meta = runtime_meta
        self.spill_quota = SpillQuota()
        self.guard = ActiveSessionGuard(persistence.state_store)
        self.mutation_tracker: MutationTracker | None = None
        self.mutation_coordinator: MutationCoordinator | None = None
        self.hook_manager: HookManager | None = None
        self.background_tasks: BackgroundTasks | None = None
        """The session's shell commands running in the background; it outlives every rebuild."""

    @property
    def workspace(self) -> Workspace | None:
        return self._workspace

    def present_workflow_workspace(self, workspace: Workspace) -> None:
        self._workspace = workspace

    def workspace_cwd(self) -> str:
        """Return the current workspace cwd, falling back only before workspace initialization."""
        if self.workspace is not None:
            return self.workspace.primary_cwd
        from chrys.foundation.platform import safe_getcwd

        return safe_getcwd()

    @property
    def session_dir(self) -> Path | None:
        """Return the session directory path, or ``None`` if no session is active.

        Delegates to the state store when available so that tests using
        ``tmp_path``-based stores write to the temp directory. Without a
        store, fall back to the active sessions directory.
        """
        session_id = self.session_id
        if not session_id:
            return None
        return self.session_dir_for(session_id)

    def session_dir_for(self, session_id: str) -> Path:
        """Resolve a session directory for an explicit, already-captured id."""
        store = self._persistence.state_store
        if store is not None:
            return store.session_dir(session_id)
        from chrys.foundation.config.settings import resolve_sessions_dir
        from chrys.foundation.util.session_ids import session_short_id

        return resolve_sessions_dir() / session_short_id(session_id)

    def sessions_root_dir(self, session_id: str) -> Path | None:
        """Return the sessions root used for lock files."""
        store = self._persistence.state_store
        if store is not None:
            return store.session_dir(session_id).parent
        from chrys.foundation.config.settings import resolve_sessions_dir

        return resolve_sessions_dir()

    def session_write_lock_path(self, session_id: str) -> Path | None:
        sessions_dir = self.sessions_root_dir(session_id)
        if sessions_dir is None:
            return None
        path = session_write_lock_path(sessions_dir, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def reset_spill_quota(self) -> None:
        """Start a fresh engine-owned spill ledger for a newly opened session."""
        self.spill_quota = SpillQuota()
