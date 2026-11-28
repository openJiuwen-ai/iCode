# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Coordinate the workspace picker and publish workspace requests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from chrys.app.tui import workspace_text as labels
from chrys.app.tui.i18n import render_str
from chrys.app.tui.screens.dialogs.base import ask_dialog
from chrys.app.tui.screens.workspaces.edit import WorkspaceEdit, WorkspaceEditDialog, delete_confirmation
from chrys.app.tui.screens.workspaces.picker import WorkspacePick, WorkspacesScreen
from chrys.app.tui.util.workspace_order import WorkspaceOrder
from chrys.app.workspace_commands import (
    ADD_REFERENCE_FOLDERS_HINT,
    WORKSPACE,
    WORKSPACE_CREATED,
    WORKSPACE_EDIT_APPLIES_ON_REOPEN,
    WORKSPACE_UPDATE_FAILED,
    WorkspaceCommand,
    WorkspaceCommandReply,
    delete_workspace_request,
    edit_workspace_request,
    find_workspace_by_name,
    manage_workspace,
    parse_workspace_command,
    workspace_change,
)
from chrys.foundation.events.types import SessionNew, WorkspaceChange, WorkspaceChangeFinished, WorkspaceOperation
from chrys.foundation.i18n import MessageRef
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_scalar
from chrys.foundation.models.workspace import WorkingDir, WorkspaceConfirmation, same_directory
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.util.abandonable import run_abandonable
from chrys.service.workspaces.paths import MissingWorkspacePrimary, resolve_workspace_directory
from chrys.service.workspaces.registry import WorkspaceRegistryError, is_unspecified_workspace_name, workspace_not_found

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from typing import Literal

    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.screens.dialogs.base import BaseDialog
    from chrys.app.tui.screens.main.ports import StartWorker, WorkspaceView
    from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState, WorkspaceActionOwner
    from chrys.app.tui.screens.main.workspace_actions import WorkspaceSwitchChoice
    from chrys.foundation.events.types import WorkspaceUpdated
    from chrys.foundation.i18n import MessageDef
    from chrys.foundation.models.workspace import Workspace
    from chrys.foundation.models.workspace_operations import WorkspaceDefinitionEdit
    from chrys.service.workspaces.registry import RegistrySnapshot, WorkspaceRegistry


class WorkspacePickerController:
    """Own picker navigation and validate that a choice still belongs to the active chat."""

    def __init__(
        self,
        *,
        state: MainScreenState,
        services: MainScreenServices,
        view: WorkspaceView,
        start_worker: StartWorker,
        cwd: Callable[[], str],
        refresh: Callable[[], None],
        snapshot: Callable[[], RegistrySnapshot | None],
        start_session: Callable[[SessionNew, Callable[[], bool]], Awaitable[bool]],
        confirm_switch: Callable[[Workspace | None, bool], Awaitable[WorkspaceSwitchChoice]],
        workflow_mode: Callable[[], bool],
        is_topmost: Callable[[], bool],
        wait_after_edit: Callable[[], Awaitable[bool]],
        order: Callable[[], WorkspaceOrder | None],
        apply_order: Callable[[WorkspaceOrder], None],
        allow_change: Callable[[], bool],
        locale_controller: LocaleController | None = None,
    ) -> None:
        self._state = state
        self._services = services
        self._view = view
        self._start_worker = start_worker
        self._cwd = cwd
        self._refresh = refresh
        self._snapshot = snapshot
        self._order = order
        self._apply_order = apply_order
        self._allow_change = allow_change
        self._start_session = start_session
        self._confirm_switch = confirm_switch
        self._workflow_mode = workflow_mode
        self._is_topmost = is_topmost
        self._wait_after_edit = wait_after_edit
        self._locale_controller = locale_controller
        self._open = False
        self._screen_generation = 0
        self._manage_lock = asyncio.Lock()

    @property
    def _registry(self) -> WorkspaceRegistry:
        return self._services.workspaces

    def busy(self, message: MessageDef = labels.BUSY) -> bool:
        if self._workflow_mode() or not self._view.is_attached:
            return True
        state = self._state
        if (
            state.run.agent_running
            or state.run.agent_loading
            or state.submit.active
            or state.session.restoring_session
            or self._services.execution_busy()
        ):
            self._view.notify(message.bind(), title=labels.BUSY_TITLE.bind(), severity="warning")
            return True
        return False

    def _owner(self) -> WorkspaceActionOwner:
        return self._services.workspace_action_owner(self._state, workflow_mode=self._workflow_mode())

    def _current(self, owner: WorkspaceActionOwner) -> bool:
        return self._view.is_attached and owner == self._owner()

    def open(self) -> None:
        if not self._open and not self.busy() and self._is_topmost():
            self._start_worker(partial(self._pick, self._owner(), self._screen_generation))

    def screen_changed(self) -> None:
        self._screen_generation += 1

    def run_command(self, argument: str) -> None:
        if not self._allow_change() or self.busy():
            return
        command = parse_workspace_command(f"/{WORKSPACE} {argument}")
        if isinstance(command, WorkspaceCommandReply):
            self._show_command_reply(command)
        elif isinstance(command, WorkspaceCommand):
            if command.action != "switch":
                snapshot = self._snapshot()
                # What the completion and picker show now: a delete must hit the workspace listed there.
                shown = snapshot.workspaces if snapshot is not None and snapshot.error is None else None
                self._start_worker(partial(self._manage, command, self._owner(), self._screen_generation, shown))
            elif command.argument:
                self._start_worker(partial(self._select_name, command.argument, self._screen_generation))
            else:
                self.open()

    async def _manage(
        self,
        command: WorkspaceCommand,
        owner: WorkspaceActionOwner,
        screen_generation: int,
        shown: Sequence[Workspace] | None = None,
    ) -> None:
        if self.busy() or not self._current(owner):
            return
        async with self._manage_lock:
            if self.busy() or not self._current(owner):
                return
            prepare = partial(
                manage_workspace,
                self._registry,
                command,
                current=self._state.workspace.membership,
                cwd=self._cwd(),
                session_id=owner[0][1] or None,
                localizer=self._locale_controller.localizer if self._locale_controller is not None else None,
                shown=shown,
            )
            # Only `new` writes here; the other actions read folders and build a request the engine applies.
            if command.action == "new":
                reply = await asyncio.to_thread(prepare)
            else:
                reply = await run_abandonable("icode-workspace-command", prepare)
            if reply.error and not self._current(owner):
                return
            if reply.created is not None:
                if self._current(owner):
                    await self._finish_create(reply.created, owner, command_screen_generation=screen_generation)
                else:
                    self._finish_write(WorkspaceCommandReply((labels.CREATED_PICKER.bind(name=reply.created.name),)))
                return
            if reply.change is None:
                self._finish_write(reply)
                return
            try:
                updated = await self._apply_definition_change(reply.change, owner)
            except WorkspaceRegistryError as error:
                self._show_command_reply(WorkspaceCommandReply((error.display_message,), error=True))
            else:
                if updated is not None:
                    self._finish_write(reply)

    def _finish_write(self, reply: WorkspaceCommandReply, *, deferred: bool = False) -> None:
        """Report the registry result independently of the session update."""
        if not self._view.is_attached:
            return
        if deferred:
            reply = WorkspaceCommandReply((*reply.messages, WORKSPACE_EDIT_APPLIES_ON_REOPEN.bind()))
        self._show_command_reply(reply)
        self._refresh()

    async def _finish_create(
        self, workspace: Workspace, owner: WorkspaceActionOwner, *, command_screen_generation: int | None = None
    ) -> None:
        may_switch = command_screen_generation is None or self._can_show_dialog(owner, command_screen_generation)
        applied = may_switch and await self._switch(workspace, owner, created=True)
        message = WORKSPACE_CREATED if applied else labels.CREATED_PICKER
        messages = (message.bind(name=workspace.name),)
        if command_screen_generation is not None and applied and self._state.workspace.workspace_id == workspace.id:
            messages = (*messages, ADD_REFERENCE_FOLDERS_HINT.bind())
        self._finish_write(WorkspaceCommandReply(messages))

    def _show_command_reply(self, reply: WorkspaceCommandReply) -> None:
        if not self._view.is_attached:
            return
        locale = self._locale_controller
        message = "\n".join(
            format_message(ref) if locale is None else render_str(locale.localizer, ref) for ref in reply.messages
        )
        self._notice(message, severity="error" if reply.error else "information")

    def argument_choices(self, action: str) -> list[tuple[str, str]]:
        """Suggestions for a ``/workspace`` action's argument, as the picker lists them; the current item is marked."""
        if action == "remove-folder":
            current = self._state.workspace.membership
            if current is None:
                return []
            snapshot = self._snapshot()
            workspace = (snapshot.workspace(current.id) if snapshot is not None else None) or current
            return [
                (folder.path, sanitize_legacy_scalar(surrogate_safe_text(folder.path)))
                for folder in workspace.working_dirs
                if "\r" not in folder.path and "\n" not in folder.path
            ]
        if action not in ("switch", "delete"):
            return []
        snapshot, order = self._snapshot(), self._order()
        current_id = self._state.workspace.workspace_id
        choices = []
        if snapshot is not None and order is not None and order.matches(snapshot, self._cwd(), current_id):
            choices = [
                (
                    workspace.name,
                    ("◦ " if current_id == workspace.id else "  ")
                    + sanitize_legacy_scalar(surrogate_safe_text(workspace.name)),
                )
                for workspace in order.workspaces
            ]
        if action == "switch":
            locale = self._locale_controller
            label = labels.UNSPECIFIED.bind()
            unspecified = format_message(label) if locale is None else render_str(locale.localizer, label)
            choices.append((unspecified, ("◦ " if current_id is None else "  ") + unspecified))
        return choices

    async def _ask[ResultT](self, dialog: BaseDialog[ResultT]) -> ResultT | None:
        return await ask_dialog(self._view.push_screen, dialog)

    async def _select_name(self, selector: str, screen_generation: int) -> None:
        owner = self._owner()
        locale = self._locale_controller
        if is_unspecified_workspace_name(selector, locale.localizer if locale is not None else None):
            if self._can_show_dialog(owner, screen_generation):
                await self._switch(None, owner)
            return
        snapshot = await run_abandonable("icode-workspace-registry", self._registry.snapshot)
        if not self._can_show_dialog(owner, screen_generation):
            return
        try:
            workspace = find_workspace_by_name(snapshot, selector)
        except WorkspaceRegistryError as error:
            self._notice(error.display_message, severity="warning" if snapshot.error is not None else "error")
            return
        await self._switch(workspace, owner)

    def _can_show_dialog(self, owner: WorkspaceActionOwner, screen_generation: int) -> bool:
        return (
            screen_generation == self._screen_generation
            and self._current(owner)
            and self._is_topmost()
            and not self.busy()
        )

    async def _pick(self, owner: WorkspaceActionOwner, screen_generation: int) -> None:
        if self._open:
            return
        self._open = True
        highlight = None
        try:
            # Each pass shows the picker once. A failed switch, a cancelled create or a saved edit shows it
            # again, as long as nothing else took the screen in between.
            while self._can_show_dialog(owner, screen_generation):
                pick = await self._ask(
                    WorkspacesScreen(
                        self._registry,
                        self._cwd(),
                        self._state.workspace.membership,
                        highlight=highlight,
                        order=self._order(),
                        on_read=self._apply_order,
                    )
                )
                if not self._current(owner) or not isinstance(pick, WorkspacePick):
                    return
                if pick.action != "create":
                    highlight = pick.workspace_id or "unspecified"
                if pick.action == "select":
                    if await self._switch(pick.workspace, owner):
                        return
                elif pick.action == "create":
                    if self.busy():
                        return
                    result = await self._ask(
                        WorkspaceEditDialog(self._registry, self._cwd(), locale_controller=self._locale_controller)
                    )
                    if result is not None:
                        await self._finish_create(result.workspace, owner)
                        return
                elif pick.workspace_id is not None:
                    deleting = pick.action == "delete"
                    result = await self._edit(pick.workspace_id, owner, self._screen_generation, delete=deleting)
                    if result is not None:
                        # Our own save can update the view without starting a different chat.
                        updated_owner = self._owner()
                        if updated_owner[0] != owner[0] or not await self._wait_after_edit():
                            return
                        owner = updated_owner
                screen_generation = self._screen_generation
        finally:
            self._open = False

    async def _edit(
        self, workspace_id: str, owner: WorkspaceActionOwner, screen_generation: int, *, delete: bool = False
    ) -> WorkspaceEdit | None:
        snapshot = await run_abandonable("icode-workspace-registry", self._registry.snapshot)
        if not self._can_show_dialog(owner, screen_generation):
            return None
        if snapshot.error is not None:
            self._notice(snapshot.error.display_message)
            return None
        workspace = snapshot.workspace(workspace_id)
        if workspace is None:
            self._notice(workspace_not_found(workspace_id).display_message)
            return None
        remove = partial(self._remove_workspace, owner=owner)
        if delete:
            confirmed = await self._ask(delete_confirmation(workspace, self._locale_controller))
            if confirmed is True:
                try:
                    if await remove(workspace):
                        return WorkspaceEdit(workspace)
                except WorkspaceRegistryError as error:
                    self._notice(error.display_message)
            return None

        return await self._ask(
            WorkspaceEditDialog(
                self._registry,
                self._cwd(),
                workspace,
                locale_controller=self._locale_controller,
                update_workspace=partial(self._save_workspace, owner=owner),
                delete_workspace=remove,
            )
        )

    async def _save_workspace(self, edit: WorkspaceDefinitionEdit, *, owner: WorkspaceActionOwner) -> Workspace | None:
        deferred = edit.expected.id == self._state.workspace.workspace_id and not same_directory(
            self._cwd(), edit.expected.primary_cwd, realpath_fallback=True
        )
        event = edit_workspace_request(edit, owner[0][1] or None)
        if await self._apply_definition_change(event, owner) is None:
            return None
        snapshot = await run_abandonable("icode-workspace-registry", self._registry.snapshot)
        saved = snapshot.workspace(edit.expected.id)
        self._finish_write(
            WorkspaceCommandReply((labels.WORKSPACE_SAVED.bind(name=edit.name.strip()),)), deferred=deferred
        )
        return saved or replace(
            edit.expected, name=edit.name.strip(), working_dirs=[WorkingDir(path=path) for path in edit.folders]
        )

    async def _remove_workspace(self, workspace: Workspace, *, owner: WorkspaceActionOwner) -> bool:
        reply = delete_workspace_request(
            workspace, current_id=self._state.workspace.workspace_id, session_id=owner[0][1] or None
        )
        if reply.change is not None and await self._apply_definition_change(reply.change, owner) is not None:
            self._finish_write(reply)
            return True
        return False

    async def _apply_definition_change(
        self, event: WorkspaceChange, owner: WorkspaceActionOwner
    ) -> WorkspaceUpdated | None:
        result = await self._request_change(replace(event, report_error=False), owner)
        if result is None:
            return None
        if result.error is not None:
            raise WorkspaceRegistryError(
                result.error.message, result.error.display_message or WORKSPACE_UPDATE_FAILED.bind()
            )
        return result.workspace_update

    async def _switch(self, target: Workspace | None, owner: WorkspaceActionOwner, *, created: bool = False) -> bool:
        if not self._current(owner):
            return False
        workspace_id = target.id if target is not None else None
        source_id = self._state.workspace.workspace_id
        if self.busy():
            return False
        if workspace_id == source_id or target is None:
            screen_generation = self._screen_generation
            snapshot = await run_abandonable("icode-workspace-registry", self._registry.snapshot)
            if not self._can_show_dialog(owner, screen_generation):
                return False
            if snapshot.error is not None:
                self._notice(snapshot.error.display_message)
                return False
            if workspace_id == source_id:
                if snapshot.workspace_id_for_session(owner[0][1]) == workspace_id:
                    return True
                return await self._publish_change(
                    WorkspaceChange(
                        workspace_operation=WorkspaceOperation.ALIGN_MEMBERSHIP,
                        workspace_id=workspace_id,
                        session_id=owner[0][1] or None,
                    ),
                    owner,
                )
        primary = target.primary_cwd if target is not None else self._cwd()
        screen_generation = self._screen_generation

        def prepare() -> tuple[bool, WorkspaceConfirmation]:
            available = target is None or resolve_workspace_directory(primary) is not None
            return available, WorkspaceConfirmation.capture(source_id, primary)

        available, confirmation = await run_abandonable("icode-workspace-folders", prepare)
        if not self._can_show_dialog(owner, screen_generation):
            return False
        if not available and target is not None:
            self._notice(MissingWorkspacePrimary(primary, workspace_name=target.name).display_message)
            return False
        choice = await self._confirm_switch(target, created)
        if choice == "cancel":
            return False
        if not self._current(owner) or self.busy():
            return False
        if choice == "new":
            return await self._start_session(
                SessionNew(workspace_id=workspace_id, start_unspecified=target is None, confirmation=confirmation),
                partial(self._current, owner),
            )
        return await self._change(workspace_id, owner, confirmation=confirmation if target is not None else None)

    async def _change(
        self,
        workspace_id: str | None,
        owner: WorkspaceActionOwner,
        *,
        confirmation: WorkspaceConfirmation | None = None,
    ) -> bool:
        return await self._publish_change(
            workspace_change(workspace_id, owner[0][1] or None, confirmation=confirmation), owner
        )

    async def _publish_change(self, event: WorkspaceChange, owner: WorkspaceActionOwner) -> bool:
        result = await self._request_change(event, owner)
        return result is not None and result.error is None and result.workspace_update is not None

    async def _request_change(
        self, event: WorkspaceChange, owner: WorkspaceActionOwner
    ) -> WorkspaceChangeFinished | None:
        if self.busy() or not self._current(owner):
            return None
        reply = None

        async def finished(result: WorkspaceChangeFinished) -> None:
            nonlocal reply
            if result.request_id == event.event_id:
                reply = result

        bus = self._services.bus
        await bus.subscribe(WorkspaceChangeFinished, finished)
        try:
            if self.busy() or not self._current(owner):
                return None
            await bus.publish(event)
        finally:
            await bus.unsubscribe(WorkspaceChangeFinished, finished)
        return reply

    def _notice(
        self, message: MessageRef | str, *, severity: Literal["information", "warning", "error"] = "warning"
    ) -> None:
        self._view.notify(message, title=labels.WORKSPACES.bind(), severity=severity, timeout=8)
