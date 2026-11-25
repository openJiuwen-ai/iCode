# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace resolution and operation planning shared by every engine entry point."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from chrys.foundation.config.errors import ConfigError
from chrys.foundation.events.types import Warning, WorkspaceOperation, WorkspaceUpdated
from chrys.foundation.i18n import DisplayPath, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.workspace import (
    Workspace,
    WorkspaceConfirmation,
    WorkspaceDeparture,
    same_directory,
)
from chrys.foundation.util.once_close import finish_close
from chrys.service.workspaces.paths import MissingWorkspacePrimary, resolve_workspace_directory
from chrys.service.workspaces.registry import (
    RegistrySnapshot,
    WorkspaceRegistryError,
    workspace_not_found,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.i18n import MessageRef
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.service.workspaces.registry import WorkspaceRegistry

logger = logging.getLogger(__name__)


def validate_workspace_primary(path: str) -> str:
    """Resolve the primary directory of the workspace for this open."""
    if (resolved := resolve_workspace_directory(path)) is None:
        raise MissingWorkspacePrimary(path)
    return resolved


def _resolve_reference_dirs(workspace: Workspace) -> tuple[Workspace, tuple[str, ...]]:
    working_dirs = []
    missing = []
    for directory in workspace.working_dirs:
        if (resolved := resolve_workspace_directory(directory.path)) is None:
            missing.append(directory.path)
        else:
            working_dirs.append(replace(directory, path=resolved))
    return replace(workspace, working_dirs=working_dirs), tuple(missing)


_INHERITED_WORKSPACE_MISSING = msg(
    "workspaces.inherited_workspace_missing",
    fallback="Workspace “{name}” was deleted. The new session is Unspecified.",
)

NEW_MEMBERSHIP_WRITE_FAILED = msg(
    "workspaces.new_membership_write_failed",
    fallback="This session stays in workspace “{name}”, but its membership could not be saved; it will reopen as Unspecified.",
)
LEAVE_MEMBERSHIP_WRITE_FAILED = msg(
    "workspaces.leave_membership_write_failed",
    fallback="This session is now Unspecified, but leaving workspace “{name}” could not be saved; reopened later, it is back in the workspace.",
)
FORK_MEMBERSHIP_WRITE_FAILED = msg(
    "workspaces.fork_membership_write_failed",
    fallback="The forked session could not be added to workspace “{name}”; it opens as Unspecified.",
)

WORKSPACE_LEFT = msg("workspaces.left", fallback="Left workspace “{name}”; this session is now Unspecified.")
WORKSPACE_SELECTED = msg(
    "workspaces.selected_from_other",
    fallback="Moved this session from workspace “{previous}” to workspace “{selected}”.",
)
WORKSPACE_CHANGED = msg("workspaces.selection_changed", fallback="The workspace changed. Try again.")
WORKSPACE_CWD_MISSING = msg(
    "workspaces.working_dir_missing",
    fallback="The working directory does not exist. Choose a replacement directory first.",
)

_MEMBERSHIP_SAVED_OPEN_FAILED = msg(
    "workspaces.membership_saved_open_failed",
    fallback="Membership was saved for {target}. This run is at {running}. Reopening uses {target}. Opening failed: {detail}",
)
_MEMBERSHIP_SAVED_NO_RUNTIME = msg(
    "workspaces.membership_saved_no_runtime",
    fallback="Membership was saved for {target}. There is no active session in this run. Reopening uses {target}. Opening failed: {detail}",
)
_MEMBERSHIP_CLEARED_OPEN_FAILED = msg(
    "workspaces.membership_cleared_open_failed",
    fallback="Workspace membership was cleared. This run is at {running}. The session reopens as Unspecified in its saved directory. Opening failed: {detail}",
)
_MEMBERSHIP_CLEARED_NO_RUNTIME = msg(
    "workspaces.membership_cleared_no_runtime",
    fallback="Workspace membership was cleared. There is no active session in this run. The session reopens as Unspecified in its saved directory. Opening failed: {detail}",
)

_LEAVE_CONFIRMATION = msg(
    "workspaces.leave_confirmation_required", fallback="The workspace changed. Confirm leaving it again."
)


class LeaveConfirmationRequired(ValueError):
    """The caller's confirmation does not authorize this exact departure."""

    display_message = _LEAVE_CONFIRMATION.bind()

    def __init__(self) -> None:
        super().__init__(format_message(self.display_message))


def workspace_error_display(error: Exception) -> MessageRef | None:
    """Preserve the display text of workspace and configuration failures."""
    if isinstance(error, (WorkspaceRegistryError, MissingWorkspacePrimary, LeaveConfirmationRequired)):
        return error.display_message
    return error.display if isinstance(error, ConfigError) else None


@dataclass(frozen=True)
class _WorkspacePlan:
    """A candidate environment, installed only with its completed runtime."""

    workspace: Workspace
    departure: WorkspaceDeparture | None = None
    noop: bool = field(default=False, compare=False)
    left_workspace: bool = field(default=False, compare=False)
    missing_working_dirs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if bool(self.workspace.id) != bool(self.workspace.name):
            raise ValueError("A workspace requires both an id and a name.")

    @property
    def workspace_id(self) -> str | None:
        return self.workspace.id or None


WorkspacePlan = _WorkspacePlan


class StaleWorkspacePlan(WorkspaceRegistryError):
    """A candidate was superseded before it could be installed."""


class MembershipWrite:
    """Save one session's membership at the installation boundary."""

    def __init__(
        self,
        registry: WorkspaceRegistry,
        workspace: Workspace,
        *,
        best_effort: bool = False,
        leaving: str | None = None,
    ) -> None:
        self.registry = registry
        self.workspace = workspace
        self.best_effort = best_effort
        self.leaving = leaving
        self.failure: WorkspaceRegistryError | None = None
        self.saved = False

    def save(self, session_id: str) -> None:
        try:
            if self.leaving is None:
                self.registry.set_session_workspace(session_id, self.workspace.id or None)
                self.saved = True
            else:
                self.saved = self.registry.set_session_workspace(session_id, None, leaving=self.leaving)
        except WorkspaceRegistryError as error:
            if not self.best_effort:
                raise
            self.failure = error

    def open_failure(self, current: Workspace | None, cause: Exception) -> WorkspaceRegistryError:
        if self.workspace.id:
            target = self.workspace.name
            if current is None:
                detail = _MEMBERSHIP_SAVED_NO_RUNTIME.bind(target=target, detail=str(cause))
            else:
                detail = _MEMBERSHIP_SAVED_OPEN_FAILED.bind(
                    target=target, running=DisplayPath(current.primary_cwd), detail=str(cause)
                )
        elif current is None:
            detail = _MEMBERSHIP_CLEARED_NO_RUNTIME.bind(detail=str(cause))
        else:
            detail = _MEMBERSHIP_CLEARED_OPEN_FAILED.bind(running=DisplayPath(current.primary_cwd), detail=str(cause))
        return WorkspaceRegistryError(format_message(detail), detail, code="workspace_membership_saved_open_failed")


def _workspace_warning(
    code: str, reference: MessageRef, session_id: str | None, *, message: str | None = None
) -> Warning:
    return Warning(
        code=code,
        message=message if message is not None else format_message(reference),
        display_message=reference,
        session_id=session_id,
    )


def workspace_left_warning(session_id: str | None, workspace_name: str) -> Warning:
    """Tell ``icode run`` that changing directory left the workspace: its output shows no workspace otherwise."""
    return _workspace_warning("workspace_left", WORKSPACE_LEFT.bind(name=workspace_name), session_id)


def workspace_selected_warning(session_id: str | None, previous: Workspace, selected: Workspace) -> Warning:
    reference = WORKSPACE_SELECTED.bind(previous=previous.name, selected=selected.name)
    return _workspace_warning("workspace_selected", reference, session_id)


def workspace_updated_event(
    session: ActiveSession,
    *,
    session_id: str | None,
) -> WorkspaceUpdated:
    """The event announcing the session's committed workspace after a rebuild."""
    workspace = session.workspace
    return WorkspaceUpdated(
        primary_cwd=workspace.primary_cwd if workspace is not None else "",
        workspace=session.session_workspace,
        working_dirs=[directory.path for directory in workspace.working_dirs] if workspace is not None else [],
        reference_files=list(workspace.reference_files) if workspace is not None else [],
        session_id=session_id,
    )


def registry_unreadable_warning(session_id: str | None, error: WorkspaceRegistryError) -> Warning:
    """Carry the registry's localized error across the restore boundary."""
    return _workspace_warning(error.code, error.display_message, session_id, message=str(error))


class SessionWorkspaces:
    """Plan workspace changes and manage membership in the session store's registry."""

    def __init__(self, bus: EventBus, registry: WorkspaceRegistry | None) -> None:
        self.registry = registry
        self._bus = bus
        self._warned = False

    def snapshot(self) -> RegistrySnapshot:
        return self.registry.snapshot() if self.registry is not None else RegistrySnapshot()

    async def read_and_warn(self, session_id: str | None) -> RegistrySnapshot:
        """Warn once per open cycle since a failed read cannot establish whether the session is Unspecified."""
        snapshot = self.snapshot()
        if snapshot.error and not self._warned:
            self._warned = True
            await self.publish_unreadable(session_id, snapshot.error)
        return snapshot

    async def publish_unreadable(self, session_id: str | None, error: WorkspaceRegistryError) -> None:
        """Report an open attempt without changing the active session's warning state."""
        await self._bus.publish(registry_unreadable_warning(session_id, error))

    async def inherited_workspace(self, installed: Workspace, session_id: str | None) -> Workspace | None:
        snapshot = self.snapshot()
        if snapshot.error:
            # A failed read cannot establish deletion. Keep the installed definition
            # so the best-effort membership write can report the failure as usual.
            return installed
        latest = snapshot.workspace(installed.id)
        if latest is None:
            warning = _INHERITED_WORKSPACE_MISSING.bind(name=installed.name)
            await self._bus.publish(_workspace_warning("workspace_inherited_missing", warning, session_id))
            return None
        return replace(installed, name=latest.name, working_dirs=latest.working_dirs)

    def begin_session(self, *, warned: bool = False) -> None:
        """Use the warning state of the open attempt that reached commit."""
        self._warned = warned

    async def warn_membership_write(
        self,
        session_id: str | None,
        failure_message: MessageRef,
        error: WorkspaceRegistryError,
        *,
        request_id: str = "",
    ) -> None:
        """Report a best-effort membership failure without changing the operation's outcome.

        The operation is already committed, so a cancel cannot drop the warning.
        """
        message = format_message(failure_message)
        logger.warning("Workspace membership was not saved for %s: %s: %s", session_id, message, error)
        warning = Warning(
            code="workspace_membership_write_failed",
            request_id=request_id,
            reason=str(error),
            message=message,
            display_message=failure_message,
            display_hint=error.display_message,
            session_id=session_id,
        )
        await finish_close(asyncio.create_task(self._bus.publish(warning)))

    def membership_write(
        self, plan: WorkspacePlan, operation: WorkspaceOperation, *, source_id: str | None
    ) -> MembershipWrite | None:
        if self.registry is None:
            return None
        if operation in (WorkspaceOperation.SELECT, WorkspaceOperation.UNSPECIFY):
            return MembershipWrite(self.registry, plan.workspace)
        if plan.left_workspace and source_id is not None:
            return MembershipWrite(self.registry, plan.workspace, best_effort=True, leaving=source_id)
        return None

    async def touch(self, session_workspace: Workspace, cwd: str, *, is_current: Callable[[], bool]) -> None:
        """Record the workspace's use for the picker's order."""
        if self.registry is None or not is_current() or not os.path.isdir(cwd):
            return
        await asyncio.to_thread(self.registry.touch, session_workspace.id)

    @staticmethod
    def plan(
        *,
        workspace: Workspace,
        snapshot: RegistrySnapshot,
        operation: WorkspaceOperation = WorkspaceOperation.NONE,
        live_plan: WorkspacePlan | None = None,
        session_id: str | None = None,
        restoring: bool = False,
        workspace_id: str | None = None,
        target_cwd: str = "",
        require_leave_confirmation: bool = False,
        confirmation: WorkspaceConfirmation | None = None,
        opened_cwd: str = "",
        use_target_cwd: bool = False,
    ) -> WorkspacePlan:
        """Plan one explicit workspace operation against the committed baseline."""
        session_workspace = (
            snapshot.session_workspace(session_id) if session_id is not None and live_plan is None else None
        )
        if live_plan is not None and live_plan.workspace_id is not None:
            session_workspace = live_plan.workspace
        source_id = session_workspace.id if session_workspace is not None else None
        departure = live_plan.departure if live_plan is not None and not restoring else None
        noop = leaving = False
        if (operation is not WorkspaceOperation.NONE or confirmation is not None) and snapshot.error:
            raise snapshot.error
        baseline = session_workspace or workspace
        registered_cwd = (
            resolve_workspace_directory(session_workspace.primary_cwd)
            if session_workspace is not None and live_plan is None
            else None
        )
        baseline_cwd = registered_cwd or baseline.primary_cwd
        selected = snapshot.workspace(workspace_id) if operation is WorkspaceOperation.SELECT else None
        if operation is WorkspaceOperation.SELECT:
            if selected is None:
                if confirmation is not None:
                    raise LeaveConfirmationRequired()
                raise workspace_not_found(workspace_id)
            confirmation_cwd = opened_cwd or selected.primary_cwd
        elif operation is WorkspaceOperation.UNSPECIFY:
            confirmation_cwd = baseline_cwd
        else:
            confirmation_cwd = target_cwd or opened_cwd or baseline_cwd
        if confirmation is not None and not confirmation.matches(source_id, confirmation_cwd):
            raise LeaveConfirmationRequired()
        if selected is not None:
            if opened_cwd:
                target = replace(selected, primary_cwd=opened_cwd)
            else:
                target = replace(selected, primary_cwd=validate_workspace_primary(selected.primary_cwd))
            departure = None
        elif operation is WorkspaceOperation.UNSPECIFY:
            noop = session_workspace is None
            target = workspace if session_workspace is None else Workspace(primary_cwd=baseline_cwd)
            if session_workspace is not None:
                if (live_plan is None and registered_cwd is None) or target.missing_primary():
                    raise MissingWorkspacePrimary(baseline_cwd, display_message=WORKSPACE_CWD_MISSING.bind())
                departure = WorkspaceDeparture(session_workspace.name)
                leaving = True
        elif session_workspace is not None:
            target = replace(session_workspace, primary_cwd=opened_cwd) if opened_cwd else session_workspace
            if target_cwd and not (
                target_cwd == baseline.primary_cwd
                or (
                    (live_plan is not None or registered_cwd is not None)
                    and (resolved_target := resolve_workspace_directory(target_cwd)) is not None
                    and same_directory(baseline_cwd, resolved_target)
                )
            ):
                if require_leave_confirmation and confirmation is None:
                    raise LeaveConfirmationRequired()
                target = Workspace.from_cwd(confirmation.primary_cwd if confirmation is not None else target_cwd)
                departure = None if restoring else WorkspaceDeparture(session_workspace.name)
                leaving = True
            elif target_cwd and use_target_cwd:
                target = replace(session_workspace, primary_cwd=validate_workspace_primary(target_cwd))
            elif restoring and not opened_cwd:
                target = replace(
                    session_workspace,
                    primary_cwd=registered_cwd or validate_workspace_primary(session_workspace.primary_cwd),
                )
            elif live_plan is not None and not restoring:
                return live_plan
        else:
            target = workspace

        missing = ()
        if target.id:
            target, missing = _resolve_reference_dirs(target)
        if selected is not None and live_plan is not None and not restoring:
            noop = live_plan.workspace == target and live_plan.missing_working_dirs == missing
        return _WorkspacePlan(target, departure, noop=noop, left_workspace=leaving, missing_working_dirs=missing)
