# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace-changing actions for the main screen.

The working directory can be deleted or moved outside the app. Nothing
watches it: a submit refused for that reason, the end of a turn, or a session
restore asks the user for another folder here, one prompt at a time.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Literal

from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState, WorkspaceActionOwner
from chrys.foundation.events.types import SessionNew
from chrys.foundation.i18n import DisplayPath, MessageRef, msg
from chrys.foundation.models.workspace import Workspace, WorkspaceConfirmation, path_identity, same_directory
from chrys.foundation.platform import safe_getcwd
from chrys.foundation.platform.paths import resolve_workspace_path
from chrys.foundation.util.abandonable import run_abandonable
from chrys.service.workspaces.paths import contains

if TYPE_CHECKING:
    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.screens.dialogs.base import BaseDialog
    from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog, ConfirmMessage
    from chrys.app.tui.screens.dialogs.file_picker import FilePicker
    from chrys.app.tui.screens.main.ports import StartWorker, WorkspaceView

type WorkspaceSwitchChoice = Literal["move", "new", "cancel"]

type MissingDirReason = Literal["submit", "turn_end", "restore"]

_BUSY_TITLE = msg("tui.workspace.title.busy", fallback="Busy")
_INVALID_PATH_TITLE = msg("tui.workspace.title.invalid_path", fallback="Invalid Path")
_CHANGE_DIRECTORY_BUSY = msg(
    "tui.workspace.change_directory_busy",
    fallback="Cannot change directory while agent is busy",
)
_INVALID_DIRECTORY = msg(
    "tui.workspace.invalid_directory",
    fallback="Not a valid directory: {path}",
)
_CHANGE_DIRECTORY = msg("tui.workspace.change_directory", fallback="Change Directory")
_LEAVE_WORKSPACE_TITLE = msg("tui.workspace.leave_workspace.title", fallback="Leave workspace “{name}”?")
_RESTORE_WORKSPACE_MOVE = msg(
    "tui.workspace.leave_workspace.message",
    fallback="• Move this session: open it in {path} as Unspecified.",
)
_LEAVE_WORKSPACE_OUTSIDE = msg(
    "tui.workspace.leave_workspace.outside",
    fallback="{path} is not in workspace “{name}”.",
)
_LEAVE_WORKSPACE_PRIMARY_CHILD = msg(
    "tui.workspace.leave_workspace.primary_child",
    fallback=(
        "{path} is already in workspace “{name}” (inside the primary folder).\n"
        "This session can already use it without changing its working directory."
    ),
    multiline=True,
)
_LEAVE_WORKSPACE_REFERENCE = msg(
    "tui.workspace.leave_workspace.reference",
    fallback=(
        "{path} is already in workspace “{name}” (a reference folder).\n"
        "This session can already use it without changing its working directory."
    ),
    multiline=True,
)
_LEAVE_WORKSPACE_REFERENCE_CHILD = msg(
    "tui.workspace.leave_workspace.reference_child",
    fallback=(
        "{path} is already in workspace “{name}” (inside a reference folder).\n"
        "This session can already use it without changing its working directory."
    ),
    multiline=True,
)
_LEAVE_WORKSPACE_MOVE_RESULT = msg(
    "tui.workspace.leave_workspace.move_result",
    fallback="• Move this session: change its working directory to this folder and make it Unspecified.",
)
_LEAVE_WORKSPACE_NEW_RESULT = msg(
    "tui.workspace.leave_workspace.new_result",
    fallback="• New session: open a new Unspecified session in this folder. The current session stays where it is.",
)
_LEAVE_WORKSPACE_SWITCH = msg("tui.workspace.leave_workspace.switch", fallback="Move this session")
_SWITCH_WORKSPACE_TITLE = msg("tui.workspace.switch.title", fallback="Switch to workspace “{name}”?")
_SWITCH_WORKSPACE_MOVE = msg(
    "tui.workspace.switch.move",
    fallback="• Move this session: bring the conversation along. Its working directory changes to {path}.",
)
_SWITCH_WORKSPACE_MOVE_SAME = msg(
    "tui.workspace.switch.move_same",
    fallback="• Move this session: bring the conversation along. Its working directory stays the same.",
)
_SWITCH_WORKSPACE_NEW = msg(
    "tui.workspace.switch.new",
    fallback="• New session: open a new session in “{name}”. The current session stays where it is.",
)
_UNSPECIFY_WORKSPACE_MESSAGE = msg(
    "tui.workspace.switch.unspecified",
    fallback=(
        "• Move this session: make it Unspecified. Its working directory stays the same.\n"
        "• New session: open a new Unspecified session. The current session stays where it is."
    ),
    multiline=True,
)
_WORKSPACE_CREATED = msg("tui.workspace.switch.created", fallback="Created workspace “{name}”")
_MOVE_TO_CREATED_WORKSPACE = msg(
    "tui.workspace.switch.move_to_created", fallback="Move the current session into “{name}”?"
)
_LEAVE_WORKSPACE_NEW = msg("tui.workspace.leave_workspace.new", fallback="New session")
_MISSING_TITLE = msg("tui.workspace.missing.title", fallback="Working Folder Not Found")
_MISSING_MESSAGE = msg(
    "tui.workspace.missing.message",
    fallback="{path} was deleted or moved. Choose a folder to continue.",
)
_MISSING_MESSAGE_SUBMIT = msg(
    "tui.workspace.missing.message_submit",
    fallback="{path} was deleted or moved. Choose a folder to continue. Your message is still in the input box.",
)
_MISSING_MESSAGE_RESTORE = msg(
    "tui.workspace.missing.message_restore",
    fallback="The folder of this session, {path}, was deleted or moved. Choose a folder to open the session in.",
)
_MISSING_WORKSPACE_RESTORE = msg(
    "tui.workspace.missing.workspace_restore",
    fallback=(
        "The primary folder {path} of workspace “{name}” no longer exists. Restore {path} to keep the saved session in “{name}”.\n\n"
        "• Choose Folder…: select a replacement working directory and open the saved session as Unspecified.\n"
        "• Cancel: keep the current session open."
    ),
    multiline=True,
)
_MISSING_CHOOSE = msg("tui.workspace.missing.choose", fallback="Choose Folder…")
_MISSING_MESSAGES = {
    "submit": _MISSING_MESSAGE_SUBMIT,
    "turn_end": _MISSING_MESSAGE,
    "restore": _MISSING_MESSAGE_RESTORE,
}


@dataclass(frozen=True, slots=True)
class WorkspaceCallbacks:
    """Screen-owned effects required by workspace actions."""

    start_worker: StartWorker
    debug: Callable[[str, str], None]
    allow_change: Callable[[], bool] = lambda: True
    selected_cwd: Callable[[], str] = lambda: ""
    is_topmost: Callable[[], bool] = lambda: True
    apply_workflow_cwd: Callable[[str], Awaitable[bool]] | None = None
    workflow_mode: Callable[[], bool] = lambda: False
    start_new_session: Callable[[SessionNew, Callable[[], bool]], Awaitable[None]] | None = None


class WorkspaceController:
    """Coordinate working-directory picker and /chdir publishing."""

    def __init__(
        self,
        *,
        state: MainScreenState,
        services: MainScreenServices,
        view: WorkspaceView,
        callbacks: WorkspaceCallbacks,
        locale_controller: LocaleController | None = None,
    ) -> None:
        self._state = state
        self._services = services
        self._view = view
        self._callbacks = callbacks
        self._locale_controller = locale_controller
        self._missing_prompt_active = False

    def open_working_dir_picker(self) -> None:
        """Open the file dialog when the user clicks the working directory subtitle."""
        if self._can_change_workspace():
            self._push_directory_picker()

    def start_chdir(self, arg: str) -> object:
        """Run :meth:`chdir` in a screen worker."""
        return self._callbacks.start_worker(partial(self.chdir, arg)) if self._can_change_workspace() else None

    async def chdir(self, arg: str) -> None:
        """Handle /chdir slash command — change the working directory."""
        path = arg.strip()
        if not path:
            self.open_working_dir_picker()
            return

        current_cwd = self.workspace_cwd()
        resolved = resolve_workspace_path(path, base_cwd=current_cwd)

        if not os.path.isdir(resolved):
            self._view.notify(
                _INVALID_DIRECTORY.bind(path=DisplayPath(resolved)),
                title=_INVALID_PATH_TITLE.bind(),
                severity="error",
                timeout=4,
            )
            return

        if self._already_in_directory(resolved, current_cwd):
            return

        await self.apply_chdir(resolved)

    def on_chdir_dialog_result(self, result: str | None, *, owner: WorkspaceActionOwner | None = None) -> None:
        """Callback for the file dialog — apply the selected directory."""
        if owner is not None and owner != self._workspace_action_owner():
            return
        if result and os.path.isdir(result) and not self._already_in_directory(result, self.workspace_cwd()):
            self._callbacks.start_worker(partial(self.apply_chdir, result, owner=owner))

    def _already_in_directory(self, target: str, current: str) -> bool:
        if self._state.workspace.workspace_id is not None and not self._callbacks.workflow_mode():
            return same_directory(target, current)
        try:
            return os.path.samefile(target, current)
        except OSError:
            return False

    def _workspace_action_owner(self) -> WorkspaceActionOwner:
        return self._services.workspace_action_owner(self._state, workflow_mode=self._callbacks.workflow_mode())

    async def apply_chdir(self, resolved: str, *, owner: WorkspaceActionOwner | None = None) -> None:
        """Publish a WorkspaceChange for the selected directory."""
        from chrys.foundation.events.types import WorkspaceChange

        current = self._workspace_action_owner()
        if (owner is not None and owner != current) or not self._can_change_workspace():
            return
        apply_workflow = self._callbacks.apply_workflow_cwd
        if apply_workflow is not None and await apply_workflow(resolved):
            self._callbacks.debug("Chdir", resolved)
            return
        if current != self._workspace_action_owner() or not self._can_change_workspace():
            return
        confirmation = None
        if self._state.workspace.workspace_id is not None:
            allowed, confirmation = await self.confirm_workspace_leave(resolved)
            if not allowed or current != self._workspace_action_owner() or not self._can_change_workspace():
                return
        await self._services.bus.publish(WorkspaceChange(primary_cwd=resolved, confirmation=confirmation))
        self._callbacks.debug("Chdir", resolved)

    async def confirm_workspace_leave(
        self, resolved: str, session_id: str = "", workspace: Workspace | None = None
    ) -> tuple[bool, WorkspaceConfirmation | None]:
        """Confirm the source membership and destination for a switch or restore."""

        owner = self._workspace_action_owner()
        source = workspace if session_id else self._state.workspace.membership
        if source is None:
            return True, None
        offer_new = not session_id and self._state.run.has_messages

        def prepare() -> tuple[WorkspaceConfirmation, bool, tuple[MessageRef, ...]]:
            confirmation = WorkspaceConfirmation.capture(source.id, resolved)
            same = path_identity(source.primary_cwd) == confirmation.identity
            message = (
                (_RESTORE_WORKSPACE_MOVE.bind(path=DisplayPath(resolved)),)
                if session_id
                else self._chdir_leave_message(resolved, source, offer_new=offer_new)
            )
            return confirmation, same, message

        confirmation, same, message = await run_abandonable("icode-workspace-folders", prepare)
        if owner != self._workspace_action_owner() or not self._view.is_attached or not self._callbacks.is_topmost():
            return False, None
        if same:
            return True, confirmation

        def confirmation_is_current() -> bool:
            return owner == self._workspace_action_owner()

        dialog = self._workspace_switch_dialog(
            _LEAVE_WORKSPACE_TITLE.bind(name=source.name), message, offer_new=offer_new
        )
        confirmed = await self._ask(dialog)
        if not confirmation_is_current():
            return False, None
        if confirmed is True and dialog.selected_alternate:
            if start := self._callbacks.start_new_session:
                await start(
                    SessionNew(start_unspecified=True, primary_cwd=resolved, confirmation=confirmation),
                    confirmation_is_current,
                )
            return False, None
        return confirmed is True, confirmation if confirmed is True else None

    @staticmethod
    def _chdir_leave_message(resolved: str, workspace: Workspace, *, offer_new: bool) -> tuple[MessageRef, ...]:
        definition = _LEAVE_WORKSPACE_OUTSIDE
        if contains(workspace.primary_cwd, resolved):
            definition = _LEAVE_WORKSPACE_PRIMARY_CHILD
        else:
            for directory in workspace.working_dirs:
                if same_directory(directory.path, resolved):
                    definition = _LEAVE_WORKSPACE_REFERENCE
                    break
                if contains(directory.path, resolved):
                    definition = _LEAVE_WORKSPACE_REFERENCE_CHILD
                    break
        message = (
            definition.bind(path=DisplayPath(resolved), name=workspace.name),
            _LEAVE_WORKSPACE_MOVE_RESULT.bind(),
        )
        return (*message, _LEAVE_WORKSPACE_NEW_RESULT.bind()) if offer_new else message

    async def confirm_workspace_switch(self, target: Workspace | None, created: bool) -> WorkspaceSwitchChoice:
        """Ask whether an existing conversation should move to the selected workspace."""
        owner = self._workspace_action_owner()
        current = self._state.workspace.membership
        if not self._state.run.has_messages:
            return "move"
        if target is not None:
            title = (_WORKSPACE_CREATED if created else _SWITCH_WORKSPACE_TITLE).bind(name=target.name)
            move = (
                _SWITCH_WORKSPACE_MOVE_SAME.bind()
                if await run_abandonable(
                    "icode-workspace-folders", same_directory, self.workspace_cwd(), target.primary_cwd
                )
                else _SWITCH_WORKSPACE_MOVE.bind(path=DisplayPath(target.primary_cwd))
            )
            message = (move, _SWITCH_WORKSPACE_NEW.bind(name=target.name))
            if created:
                message = (_MOVE_TO_CREATED_WORKSPACE.bind(name=target.name), *message)
        elif current is not None:
            title = _LEAVE_WORKSPACE_TITLE.bind(name=current.name)
            message = (_UNSPECIFY_WORKSPACE_MESSAGE.bind(),)
        else:
            return "cancel"
        if owner != self._workspace_action_owner() or not self._view.is_attached or not self._callbacks.is_topmost():
            return "cancel"
        dialog = self._workspace_switch_dialog(title, message, offer_new=True)
        if await self._ask(dialog) is not True:
            return "cancel"
        return "new" if dialog.selected_alternate else "move"

    def _workspace_switch_dialog(self, title: MessageRef, message: ConfirmMessage, *, offer_new: bool) -> ConfirmDialog:
        from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

        dialog = ConfirmDialog(
            title=title,
            message=message,
            confirm_label=_LEAVE_WORKSPACE_SWITCH.bind(),
            alternate_label=_LEAVE_WORKSPACE_NEW.bind() if offer_new else None,
            message_align="left" if isinstance(message, tuple) else "center",
            locale_controller=self._locale_controller,
        )
        dialog.add_class("-warning-border")
        return dialog

    def _can_change_workspace(self) -> bool:
        if not self._callbacks.allow_change():
            return False
        if (
            self._state.run.agent_running
            or self._state.run.agent_loading
            or self._state.submit.active
            or self._services.execution_busy()
        ):
            self._view.notify(_CHANGE_DIRECTORY_BUSY.bind(), title=_BUSY_TITLE.bind(), severity="warning")
            return False
        return True

    def workspace_cwd(self) -> str:
        """Return the current workspace cwd from explicit main-screen state."""
        return (
            self._callbacks.selected_cwd()
            or self._state.workspace_marker.current_cwd
            or self._state.workspace.current_cwd
            or safe_getcwd()
        )

    def prompt_missing_working_dir(self, reason: MissingDirReason) -> None:
        """Ask for another folder when the current one is gone; the chosen one becomes the workspace."""
        if not self._missing_prompt_active and not os.path.isdir(self.workspace_cwd()):
            self._callbacks.start_worker(partial(self._replace_missing_working_dir, reason))

    def check_after_turn(self) -> None:
        """Once the finished turn is saved and its hooks ran, prompt if its folder is gone.

        The folder is checked after that wait: it can disappear while the turn
        saves or its hooks run. A refused submit has no turn to wait for and
        prompts on its own.
        """
        if self._state.submit.active or self._callbacks.workflow_mode():
            return
        lifecycle = self._services.turn_lifecycle_task()
        if (lifecycle is None or lifecycle.done()) and os.path.isdir(self.workspace_cwd()):
            return
        self._callbacks.start_worker(
            partial(
                self._prompt_after_turn,
                lifecycle,
                self._services.session_generation(),
                self._state.run.generation,
            )
        )

    async def choose_replacement_dir(
        self, missing: str, workspace_name: str | None = None, *, reason: MissingDirReason
    ) -> str | None:
        """Ask for a folder in place of *missing*; ``None`` when the user declines or a prompt is open."""
        if self._missing_prompt_active:
            return None
        self._missing_prompt_active = True
        owner = self._workspace_action_owner()
        try:
            from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

            path = DisplayPath(missing)
            message = (
                _MISSING_WORKSPACE_RESTORE.bind(path=path, name=workspace_name)
                if workspace_name is not None
                else _MISSING_MESSAGES[reason].bind(path=path)
            )
            confirmed = await self._ask(
                ConfirmDialog(
                    title=_MISSING_TITLE.bind(),
                    message=message,
                    confirm_label=_MISSING_CHOOSE.bind(),
                    locale_controller=self._locale_controller,
                )
            )
            if confirmed is not True or owner != self._workspace_action_owner():
                return None
            chosen = await self._ask(self._directory_picker(missing))
        finally:
            self._missing_prompt_active = False
        if owner != self._workspace_action_owner():
            return None
        return chosen if isinstance(chosen, str) and os.path.isdir(chosen) else None

    async def _prompt_after_turn(
        self, lifecycle: asyncio.Task[None] | None, session_generation: int, run_generation: int
    ) -> None:
        if lifecycle is not None and not lifecycle.done():
            # Waited, not awaited: cancelling this worker must not cancel the turn.
            await asyncio.wait({lifecycle})
        if (
            self._services.session_generation() != session_generation
            or self._state.run.generation != run_generation
            or self._state.run.agent_running
            or self._state.run.agent_loading
            or self._callbacks.workflow_mode()
        ):
            return
        await self._replace_missing_working_dir("turn_end")

    async def _replace_missing_working_dir(self, reason: MissingDirReason) -> None:
        missing = self.workspace_cwd()
        if os.path.isdir(missing):
            return
        chosen = await self.choose_replacement_dir(missing, reason=reason)
        if chosen is not None:
            await self.apply_chdir(chosen)

    async def _ask[ResultT](self, dialog: BaseDialog[ResultT]) -> ResultT | None:
        from chrys.app.tui.screens.dialogs.base import ask_dialog

        return await ask_dialog(self._view.push_screen, dialog)

    def _push_directory_picker(self) -> None:
        owner = self._workspace_action_owner()
        self._view.push_screen(
            self._directory_picker(self.workspace_cwd()), partial(self.on_chdir_dialog_result, owner=owner)
        )

    def _directory_picker(self, initial_path: str) -> FilePicker:
        from chrys.app.tui.screens.dialogs.file_picker import FilePicker, FilePickerMode
        from chrys.app.tui.screens.main.recent_dirs import WorkspaceMruRecentDirs

        # Provider works without a state store too: it reads an existing MRU
        # index but never creates one (creation requires the one-time session
        # backfill, which needs the store).
        return FilePicker(
            mode=FilePickerMode.FOLDER,
            initial_path=initial_path,
            title=_CHANGE_DIRECTORY.bind(),
            recent_paths=WorkspaceMruRecentDirs(
                self._services.state_store,
                max_entries=self._services.workspace_mru_max_entries,
            ),
        )
