# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace resolution and operation planning shared by every engine entry point."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from chrys.foundation.config.errors import ConfigError
from chrys.foundation.events.types import Warning, WorkspaceOperation, WorkspaceUpdated
from chrys.foundation.i18n import DisplayPath, DisplaySequence, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.workspace import (
    WorkingDir,
    Workspace,
    WorkspaceConfirmation,
    WorkspaceDeparture,
    path_identity,
    same_directory,
)
from chrys.foundation.util.abandonable import run_abandonable
from chrys.foundation.util.once_close import finish_close
from chrys.service.workspaces.paths import MissingWorkspacePrimary, resolve_workspace_directory
from chrys.service.workspaces.registry import WORKSPACE_CHANGED as WORKSPACE_CHANGED
from chrys.service.workspaces.registry import (
    RegistrySnapshot,
    WorkspaceRegistryError,
    retry_workspace_write,
    workspace_not_found,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.i18n import MessageRef
    from chrys.foundation.models.workspace_operations import WorkspaceDefinitionEdit
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.service.workspaces.registry import WorkspaceRegistry

logger = logging.getLogger(__name__)


def validate_workspace_primary(path: str, *, workspace_name: str | None = None) -> str:
    """Resolve the primary directory of the workspace for this open."""
    if (resolved := resolve_workspace_directory(path)) is None:
        raise MissingWorkspacePrimary(path, workspace_name=workspace_name)
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

WORKSPACE_REFERENCE_MISSING = "workspace_reference_missing"
_REFERENCE_MISSING = msg(
    "workspaces.reference_missing",
    fallback="Workspace “{name}” has a missing reference folder: {paths}",
    plural_fallback="Workspace “{name}” has {count} missing reference folders: {paths}",
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
WORKSPACE_CWD_MISSING = msg(
    "workspaces.working_dir_missing",
    fallback="The primary folder of workspace “{name}”, {path}, does not exist. Choose a replacement directory first.",
)

_DEFINITION_SAVED_OPEN_FAILED = msg(
    "workspaces.definition_saved_open_failed",
    fallback="The definition of workspace “{target}” was saved, but applying it to this session failed: {detail}",
)

_DELETED_OPEN_FAILED = msg(
    "workspaces.deleted_open_failed",
    fallback="Workspace “{target}” was deleted, but applying it to this session failed: {detail}",
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
    workspace_folders: tuple[str, ...] = ()
    """Registered skill roots present among this plan's effective directories."""

    def __post_init__(self) -> None:
        if bool(self.workspace.id) != bool(self.workspace.name):
            raise ValueError("A workspace requires both an id and a name.")

    @property
    def workspace_id(self) -> str | None:
        return self.workspace.id or None


WorkspacePlan = _WorkspacePlan


class StaleWorkspacePlan(WorkspaceRegistryError):
    """A candidate was superseded before it could be installed."""


class _StopGate:
    """Abandons a workspace change on the requester's stop, until the change first writes or installs."""

    def __init__(self, stop: asyncio.Event | None) -> None:
        self._stop = stop
        self._watch: asyncio.Task[None] | None = None
        if stop is not None:
            requester = asyncio.current_task()

            async def cancel_requester() -> None:
                await stop.wait()
                if requester is not None:
                    requester.cancel()

            # Interrupts the awaits before the commit: folder planning, settings staging, lock retries.
            self._watch = asyncio.create_task(cancel_requester())

    def check(self) -> None:
        """Called synchronously right before each write attempt: the watch may not have run yet this tick."""
        if self._stop is not None and self._stop.is_set():
            raise asyncio.CancelledError

    def close(self) -> None:
        """The change wrote or is about to install: from here on it completes."""
        if self._watch is not None:
            self._watch.cancel()
            self._watch = None
        self._stop = None

    def commit(self) -> None:
        self.check()
        self.close()


_STOP_GATE: ContextVar[_StopGate | None] = ContextVar("workspace_change_stop", default=None)


@contextlib.contextmanager
def stoppable_workspace_change(stop: asyncio.Event | None) -> Generator[None]:
    """Let *stop* abandon the workspace change run inside, until it first writes the registry or installs.

    The owners of those steps enforce the boundary: every ``MembershipWrite`` attempt checks the stop
    and its first success ends it; writing off the loop and installing call ``commit_workspace_change``.
    """
    gate = _StopGate(stop)
    token = _STOP_GATE.set(gate)
    try:
        yield
    finally:
        gate.close()
        _STOP_GATE.reset(token)


def commit_workspace_change() -> None:
    """Honor a stop already requested, then let the change complete: it is about to write off the loop or install."""
    if (gate := _STOP_GATE.get()) is not None:
        gate.commit()


class MembershipWrite:
    """Save one session's membership at the installation boundary."""

    def __init__(
        self,
        registry: WorkspaceRegistry,
        workspace: Workspace,
        *,
        best_effort: bool = False,
        leaving: str | None = None,
        delete: Workspace | None = None,
        edit: WorkspaceDefinitionEdit | None = None,
        report_error: bool = True,
        folders_checked: bool = False,
    ) -> None:
        self.registry = registry
        self.workspace = workspace
        self.best_effort = best_effort
        self.leaving = leaving
        self.delete = delete
        self.edit = edit
        self.report_error = report_error
        # The edit's folders were already probed off the event loop (the engine's preview).
        self.folders_checked = folders_checked
        self.failure: WorkspaceRegistryError | None = None
        self.saved = False

    @property
    def edits_definition(self) -> bool:
        return self.delete is not None or self.edit is not None

    async def save_async(self, validate: Callable[[], str | None]) -> None:
        """Yield only while another process holds the lock; validate and write in each attempt."""
        validated = False
        gate = _STOP_GATE.get()

        def attempt() -> None:
            nonlocal validated
            validated = False
            session_id = validate()
            validated = True
            if gate is not None:
                gate.check()
            self.save(session_id, try_lock=True, folders_checked=self.edit is not None)

        try:
            if self.edit is not None and not self.folders_checked:
                # For callers that did not preview the edit. The folders are the same in every attempt:
                # read them once, not once per lock retry.
                self.registry.check_folders(self.edit.expected.primary_cwd, self.edit.folders)
            await retry_workspace_write(attempt)
        except WorkspaceRegistryError as error:
            # A validation failure is the caller's; only a lock timeout is best-effort.
            if not validated or not self.best_effort:
                raise
            self.failure = error
        else:
            # Only a write that changed the list commits; a best-effort failure or a no-op leaves Stop armed.
            if gate is not None and self.saved:
                gate.close()

    async def save_off_loop(self, session_id: str | None) -> None:
        """Write on a thread, waiting for the lock there; the write completes even if the caller stops."""
        commit_workspace_change()
        await asyncio.to_thread(self.save, session_id)

    def save(self, session_id: str | None, *, try_lock: bool = False, folders_checked: bool = False) -> None:
        try:
            if self.delete is not None:
                self.registry.delete(
                    self.delete.id, expected=self.delete, clear_session_id=session_id, try_lock=try_lock
                )
                self.saved = True
            elif self.edit is not None:
                expected = self.edit.expected
                self.registry.update(
                    expected.id,
                    name=self.edit.name,
                    primary=expected.primary_cwd,
                    folders=self.edit.folders,
                    expected=expected,
                    try_lock=try_lock,
                    folders_checked=folders_checked,
                )
                self.saved = True
            elif session_id is None:
                raise ValueError("Saving workspace membership requires a session ID.")
            elif self.leaving is None:
                self.registry.set_session_workspace(session_id, self.workspace.id or None, try_lock=try_lock)
                self.saved = True
            else:
                self.saved = self.registry.set_session_workspace(
                    session_id, None, leaving=self.leaving, try_lock=try_lock
                )
        except WorkspaceRegistryError as error:
            if not self.best_effort:
                raise
            self.failure = error

    async def record_use(self) -> None:
        """Record a named workspace's use once its runtime has been installed."""
        if self.failure is None:
            await record_workspace_use(self.registry, self.workspace)

    def open_failure(self, current: Workspace | None, cause: Exception) -> WorkspaceRegistryError:
        if self.delete is not None:
            detail = _DELETED_OPEN_FAILED.bind(target=self.delete.name, detail=str(cause))
        elif self.edit is not None:
            detail = _DEFINITION_SAVED_OPEN_FAILED.bind(target=self.workspace.name, detail=str(cause))
        elif self.workspace.id:
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


async def record_workspace_use(registry: WorkspaceRegistry | None, workspace: Workspace | None) -> None:
    if registry is None or workspace is None or not workspace.id:
        return
    if await run_abandonable("icode-workspace-folders", os.path.isdir, workspace.primary_cwd):
        await asyncio.to_thread(registry.touch, workspace.id)


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

    async def warn_missing_references(self, session_id: str | None, plan: WorkspacePlan) -> None:
        """Report missing references together when the workspace is opened."""
        if plan.workspace_id is not None and plan.missing_working_dirs:
            paths = DisplaySequence(DisplayPath(path) for path in plan.missing_working_dirs)
            display = _REFERENCE_MISSING.bind(
                name=plan.workspace.name, paths=paths, count=len(plan.missing_working_dirs)
            )
            await self._bus.publish(_workspace_warning(WORKSPACE_REFERENCE_MISSING, display, session_id))

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
        reference_override: list[str] | None = None,
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
                target = replace(
                    selected, primary_cwd=validate_workspace_primary(selected.primary_cwd, workspace_name=selected.name)
                )
            departure = None
        elif operation is WorkspaceOperation.UNSPECIFY:
            noop = session_workspace is None
            target = workspace if session_workspace is None else Workspace(primary_cwd=baseline_cwd)
            if session_workspace is not None:
                if live_plan is None and (registered_cwd is None or target.missing_primary()):
                    raise MissingWorkspacePrimary(
                        baseline_cwd,
                        workspace_name=session_workspace.name,
                        display_message=WORKSPACE_CWD_MISSING.bind(
                            name=session_workspace.name, path=DisplayPath(baseline_cwd)
                        ),
                    )
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
                target = replace(
                    session_workspace,
                    primary_cwd=validate_workspace_primary(target_cwd, workspace_name=session_workspace.name),
                )
            elif restoring and not opened_cwd:
                target = replace(
                    session_workspace,
                    primary_cwd=registered_cwd
                    or validate_workspace_primary(session_workspace.primary_cwd, workspace_name=session_workspace.name),
                )
            elif live_plan is not None and not restoring:
                return live_plan
        else:
            target = workspace

        missing = ()
        workspace_folders = ()
        if target.id:
            registered_dirs = target.working_dirs
            if reference_override is not None:
                target = replace(target, working_dirs=[WorkingDir(path) for path in reference_override])
            target, missing = _resolve_reference_dirs(target)
            if reference_override is None:
                workspace_folders = tuple(directory.path for directory in target.working_dirs)
            else:
                effective = {path_identity(directory.path): directory.path for directory in target.working_dirs}
                workspace_folders = tuple(
                    effective[identity]
                    for directory in registered_dirs
                    if (identity := path_identity(directory.path)) in effective
                )
        if selected is not None and live_plan is not None and not restoring:
            noop = (
                live_plan.workspace == target
                and live_plan.missing_working_dirs == missing
                and live_plan.workspace_folders == workspace_folders
            )
        return _WorkspacePlan(
            target,
            departure,
            noop=noop,
            left_workspace=leaving,
            missing_working_dirs=missing,
            workspace_folders=workspace_folders,
        )
