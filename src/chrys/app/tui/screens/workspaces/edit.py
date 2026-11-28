# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Create and edit workspace definitions without changing session membership."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from rich.cells import cell_len
from rich.text import Text
from textual import events, on
from textual.binding import Binding
from textual.containers import HorizontalGroup, VerticalGroup, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Static

from chrys.app.tui import workspace_text as labels
from chrys.app.tui.binding_display import CANCEL_BINDING, localized_binding
from chrys.app.tui.i18n import LocaleController, render_str, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.dialogs.file_picker import FilePicker, FilePickerMode
from chrys.app.tui.util.paths import middle_cut_path
from chrys.app.tui.util.removal import remove_shielded
from chrys.app.tui.util.rich_style import rich_style_from_textual_color
from chrys.app.tui.widgets import DialogButtonRow, DialogButtonSpec, EnhancedInput
from chrys.app.tui.widgets.dialog_buttons import KeepFocusButton
from chrys.app.tui.widgets.fitted_prompt import FittedPrompt
from chrys.foundation.i18n import MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.models.workspace_operations import WorkspaceDefinitionEdit
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.util.abandonable import run_abandonable
from chrys.service.workspaces.paths import folders_overlap, missing_folders, resolve_workspace_directory
from chrys.service.workspaces.registry import (
    PRIMARY_IMMUTABLE,
    WORKSPACE_CHANGED,
    WorkspaceNameError,
    WorkspaceRegistry,
    WorkspaceRegistryError,
    validate_workspace_name,
)

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType

_EDIT_CONFLICT = msg(
    "tui.workspaces.edit_conflict",
    fallback="This workspace was edited elsewhere. Close and reopen this dialog to edit it. Your draft is still here to copy.",
)
_EDIT_DELETED = msg(
    "tui.workspaces.edit_deleted",
    fallback="This workspace was deleted elsewhere. You can copy your draft before closing this dialog.",
)


_FOLDER_MARK_SEPARATOR_WIDTH = 3
_FOLDER_MIN_PATH_WIDTH = 19


def _folder_fits(path: str, other_folders: tuple[str, ...]) -> bool:
    try:
        return resolve_workspace_directory(path) is not None and not any(
            folders_overlap(path, other) for other in other_folders
        )
    except OSError, ValueError:
        return False


@dataclass(frozen=True, slots=True)
class WorkspaceEdit:
    workspace: Workspace


class _FolderChoice(Static, can_focus=True):
    """Open the folder picker by click or Enter."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("enter", "choose", show=False)]

    class Selected(Message):
        def __init__(self, control: _FolderChoice) -> None:
            super().__init__()
            self.choice = control

        @property
        def control(self) -> _FolderChoice:
            return self.choice

    def on_click(self, event: events.Click) -> None:
        if event.button == 1:
            event.prevent_default()
            event.stop()
            self.action_choose()

    def action_choose(self) -> None:
        if not self.disabled:
            self.post_message(self.Selected(self))


def delete_confirmation(workspace: Workspace, locale_controller: LocaleController | None = None) -> ConfirmDialog:
    return ConfirmDialog(
        title=labels.DELETE_TITLE.bind(),
        message=labels.DELETE_MESSAGE.bind(name=workspace.name),
        confirm_label=labels.DELETE.bind(),
        confirm_variant="error",
        locale_controller=locale_controller,
    )


class WorkspaceEditDialog(BaseDialog[WorkspaceEdit | None]):
    """The empty-name, error-row and submit guard follow NewFolderDialog."""

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "cancel", CANCEL_BINDING, show=False, priority=True),
    ]
    CSS_PATH = "edit.tcss"

    def __init__(
        self,
        registry: WorkspaceRegistry,
        cwd: str,
        workspace: Workspace | None = None,
        *,
        locale_controller: LocaleController | None = None,
        update_workspace: Callable[[WorkspaceDefinitionEdit], Awaitable[Workspace | None]] | None = None,
        delete_workspace: Callable[[Workspace], Awaitable[bool]] | None = None,
    ) -> None:
        self._registry = registry
        self._locale_controller = locale_controller
        self._update_workspace = update_workspace
        self._delete_workspace = delete_workspace
        self._workspace = workspace
        self._primary = workspace.primary_cwd if workspace else cwd
        self._folders = [directory.path for directory in workspace.working_dirs] if workspace else []
        self._missing_folders: set[str] = set()
        self._saving = False
        super().__init__()

    def _label(self, definition: MessageDef, *, required: bool = False) -> Text:
        text = render_str(widget_localizer(self), definition.bind())
        return Text.assemble(("*", "red"), " ", text) if required else Text(text)

    def _fitted_folder_label(self, path: str) -> FittedPrompt:
        shown = sanitize_legacy_scalar(surrogate_safe_text(path))
        localizer = widget_localizer(self)
        mark = render_str(localizer, labels.FOLDER_MISSING.bind())
        short_mark = render_str(localizer, labels.MISSING.bind())
        full_mark_width = cell_len(mark)
        short_mark_width = cell_len(short_mark)
        missing = path in self._missing_folders

        def folder_label(width: int) -> Text:
            # Reserve the warning column before probing so a missing mark never moves the path.
            mark_width = min(
                full_mark_width, max(short_mark_width, width - _FOLDER_MARK_SEPARATOR_WIDTH - _FOLDER_MIN_PATH_WIDTH)
            )
            path_width = max(1, width - mark_width - _FOLDER_MARK_SEPARATOR_WIDTH)
            text = Text(middle_cut_path(shown, path_width))
            if missing:
                text.truncate(path_width, pad=True)
                text.append(" · ")
                warning = rich_style_from_textual_color(self.app.theme_variables["warning"])
                text.append(mark if mark_width == full_mark_width else short_mark, style=warning)
            return text

        return FittedPrompt(folder_label)

    def _folder_row(self, path: str) -> HorizontalGroup:
        return HorizontalGroup(
            Static(self._fitted_folder_label(path), name=path, classes="workspace-folder-path"),
            KeepFocusButton("✕", classes="workspace-folder-remove"),
        )

    def compose(self) -> ComposeResult:
        with VerticalGroup(id="workspace-edit-container") as container:
            container.border_title = self._label(labels.EDIT_TITLE if self._workspace else labels.NEW_TITLE)
            with VerticalScroll(id="workspace-edit-inner"):
                yield Static(self._label(labels.NAME, required=True), id="workspace-name-label")
                yield EnhancedInput(
                    self._workspace.name if self._workspace else "",
                    placeholder=render_str(widget_localizer(self), labels.NAME_PLACEHOLDER.bind()),
                    id="workspace-name",
                )
                yield Static(self._label(labels.PRIMARY, required=True), id="workspace-primary-label")
                choice = self._fitted_folder_label(self._primary)
                if self._workspace is None:
                    yield _FolderChoice(choice, id="workspace-primary")
                    hint = labels.PRIMARY_HINT
                else:
                    yield Static(choice, id="workspace-primary", classes="workspace-primary-readonly")
                    hint = PRIMARY_IMMUTABLE
                yield Static(self._label(hint), id="workspace-primary-hint")
                yield Static(self._label(labels.REFERENCE_FOLDERS), id="workspace-reference-label")
                with VerticalScroll(id="workspace-folders", can_focus=False):
                    for folder in self._folders:
                        yield self._folder_row(folder)
                    yield _FolderChoice(self._label(labels.ADD_FOLDER), id="workspace-add-folder")
                yield Static(Text(""), id="workspace-error")
            specs = []
            if self._workspace:
                specs.append(DialogButtonSpec(self._label(labels.DELETE), id="workspace-delete", variant="error"))
            specs.extend(
                (
                    DialogButtonSpec(
                        self._label(labels.SAVE if self._workspace else labels.CREATE),
                        id="workspace-save",
                        variant="primary",
                        disabled=True,
                        focus_on_click=False,
                    ),
                    DialogButtonSpec(self._label(labels.CANCEL), id="workspace-cancel", variant="warning"),
                )
            )
            yield DialogButtonRow(*specs, id="workspace-buttons")

    def on_mount(self) -> None:
        if not self._composed_live:
            return
        self._refresh_state()
        self.query_one("#workspace-name", EnhancedInput).focus()
        self.call_after_refresh(self._start_folder_probe)

    def _start_folder_probe(self) -> None:
        if self._live():
            self.run_worker(self._load_missing_folders(), exit_on_error=False)

    async def _load_missing_folders(self) -> None:
        paths = (self._primary, *self._folders)
        missing = await run_abandonable("icode-workspace-folders", missing_folders, paths)
        if not self._live() or not missing:
            return
        self._missing_folders = missing
        if self._primary in missing:
            primary = self.query_one("#workspace-primary", Static)
            primary.update(self._fitted_folder_label(self._primary), layout=False)
        for row in self.query(".workspace-folder-path").results(Static):
            path = row.name
            if path is not None and path in missing:
                row.update(self._fitted_folder_label(path), layout=False)

    def _live(self) -> bool:
        """The dialog is composed and has not started closing."""
        return self._composed_live and not self.dismiss_requested

    def _editable(self) -> bool:
        """The dialog is live and no registry write of its own is in flight."""
        return self._live() and not self._saving

    def _interactive(self) -> bool:
        """The dialog is editable and is the screen the user acts on."""
        return self._editable() and self.app.screen is self

    @on(EnhancedInput.Changed, "#workspace-name")
    def _name_changed(self, event: EnhancedInput.Changed) -> None:
        event.stop()
        self._refresh_state()

    @on(EnhancedInput.Submitted, "#workspace-name")
    def _name_submitted(self, event: EnhancedInput.Submitted) -> None:
        event.stop()
        self._start_save()

    @on(_FolderChoice.Selected, "#workspace-primary")
    def _choose_primary(self) -> None:
        if self._workspace is None and self._interactive():
            self.app.push_screen(
                FilePicker(
                    mode=FilePickerMode.FOLDER,
                    initial_path=self._primary,
                    title=labels.SELECT_PRIMARY.bind(),
                ),
                self._primary_chosen,
            )

    async def _primary_chosen(self, path: str | None) -> None:
        if self._workspace is not None or not self._editable() or path is None or path == self._primary:
            return
        primary, folders = self._primary, tuple(self._folders)
        if not await run_abandonable("icode-workspace-folders", _folder_fits, path, folders):
            return
        if not self._editable() or (self._primary, tuple(self._folders)) != (primary, folders):
            return
        self._missing_folders.discard(path)
        self._primary = path
        self.query_one("#workspace-primary", Static).update(self._fitted_folder_label(path))
        self._refresh_state()

    @on(_FolderChoice.Selected, "#workspace-add-folder")
    def _choose_folder(self) -> None:
        if self._interactive():
            self.app.push_screen(
                FilePicker(
                    mode=FilePickerMode.FOLDER,
                    initial_path=os.path.dirname(self._primary),
                    title=labels.ADD_REFERENCE.bind(),
                ),
                self._folder_added,
            )

    async def _folder_added(self, path: str | None) -> None:
        if path is None or not self._editable():
            return
        scope = (self._primary, *self._folders)
        if not await run_abandonable("icode-workspace-folders", _folder_fits, path, scope):
            return
        if not self._editable() or (self._primary, *self._folders) != scope:
            return
        self._missing_folders.discard(path)
        await self._append_folder(path)
        self._refresh_state()

    async def _append_folder(self, path: str) -> None:
        folders = self.query_one("#workspace-folders", VerticalScroll)
        add = folders.query_one("#workspace-add-folder", _FolderChoice)
        self._folders.append(path)
        await folders.mount(self._folder_row(path), before=add)
        if not self._live():
            return
        if self.focused is not add:
            self.set_focus(add, scroll_visible=False)
        self.call_after_refresh(add.scroll_visible, animate=False, immediate=True)

    @on(Button.Pressed, ".workspace-folder-remove")
    async def _remove_folder(self, event: Button.Pressed) -> None:
        event.stop()
        row = event.button.parent
        if self._interactive() and isinstance(row, HorizontalGroup):
            folders = self.query_one("#workspace-folders", VerticalScroll)
            index = folders.children.index(row)
            self._folders.pop(index)
            if self.focused is event.button:
                if self._folders:
                    neighbor = index + 1 if index < len(self._folders) else index - 1
                    focus = folders.children[neighbor].query_one(".workspace-folder-remove", Button)
                else:
                    focus = folders.query_one("#workspace-add-folder", _FolderChoice)
                self.set_focus(focus, scroll_visible=False)
            await remove_shielded(row)
            self._refresh_state()

    def _refresh_state(self, *, preserve_error: bool = False) -> bool:
        if not self._composed_live:
            return False
        name = self.query_one("#workspace-name", EnhancedInput).value
        can_submit = True
        shown = ""
        try:
            validate_workspace_name(name, (), check_reserved=False)
        except WorkspaceRegistryError as error:
            can_submit = False
            if name.strip():
                shown = render_str(widget_localizer(self), error.display_message)
        if not preserve_error:
            error_label = self.query_one("#workspace-error", Static)
            if error_label.content != Text(shown):
                error_label.update(Text(shown))
        self.query_one("#workspace-save", Button).disabled = self._saving or not can_submit
        return can_submit

    def _set_saving(self, saving: bool) -> None:
        if self._saving == saving:
            return
        self._saving = saving
        if not self._composed_live:
            return
        self.query_one("#workspace-name", EnhancedInput).disabled = saving
        for choice in self.query(_FolderChoice):
            choice.disabled = saving
        for button in self.query(Button):
            button.disabled = saving

    @on(Button.Pressed, "#workspace-save")
    def _save_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self._start_save()

    def _start_save(self) -> None:
        if not self._interactive() or not self._refresh_state():
            return
        self.run_worker(self._save, group="workspace-edit", exclusive=True, exit_on_error=False)

    async def _save(self) -> None:
        if not self._interactive() or not self._refresh_state():
            return
        name = self.query_one("#workspace-name", EnhancedInput).value
        existing = self._workspace
        primary = self._primary
        folders = tuple(self._folders)

        async def write() -> Workspace | None:
            if existing is None:
                return await asyncio.to_thread(self._registry.create, name, primary, folders)
            if self._update_workspace is None:
                raise RuntimeError("Editing a workspace requires an update callback.")
            return await self._update_workspace(WorkspaceDefinitionEdit(existing, name, folders))

        await self._commit(write, name=name)

    async def _commit(
        self,
        write: Callable[[], Awaitable[Workspace | None]],
        *,
        name: str | None = None,
    ) -> None:
        """Recheck name availability, then let the registry validate the committed write."""
        workspace: Workspace | None = None
        save_error: WorkspaceRegistryError | None = None
        focused = self.focused
        self._set_saving(True)
        try:
            self._refresh_state()
            if name is not None:
                snapshot = await run_abandonable("icode-workspace-registry", self._registry.snapshot)
                try:
                    if snapshot.error is not None:
                        raise snapshot.error
                    workspace_id = self._workspace.id if self._workspace is not None else None
                    if workspace_id is not None and snapshot.workspace(workspace_id) is None:
                        raise WorkspaceRegistryError("The workspace was deleted elsewhere.", _EDIT_DELETED.bind())
                    validate_workspace_name(name, snapshot.workspaces, workspace_id)
                except WorkspaceRegistryError as error:
                    save_error = error
                    self._show_error(error.display_message)
                    return
            try:
                workspace = await write()
            except WorkspaceRegistryError as error:
                save_error = error
                await self._handle_write_error(error)
                return
        finally:
            self._set_saving(False)
            self._refresh_state(preserve_error=True)
            if name is not None and save_error is not None and self._interactive():
                focus = (
                    self.query_one("#workspace-name", EnhancedInput)
                    if isinstance(save_error, WorkspaceNameError)
                    else focused
                )
                if focus is not None:
                    focus.focus()
        if workspace is not None and self._composed_live:
            self.dismiss_when_topmost(WorkspaceEdit(workspace))

    def _show_error(self, message: MessageRef) -> None:
        if self._composed_live:
            self.query_one("#workspace-error", Static).update(Text(render_str(widget_localizer(self), message)))

    async def _handle_write_error(self, error: WorkspaceRegistryError) -> None:
        existing = self._workspace
        if existing is not None and error.display_message.definition is WORKSPACE_CHANGED:
            snapshot = await run_abandonable("icode-workspace-registry", self._registry.snapshot)
            if snapshot.error is not None:
                self._show_error(snapshot.error.display_message)
                return
            message = _EDIT_DELETED if snapshot.workspace(existing.id) is None else _EDIT_CONFLICT
            self._show_error(message.bind())
            return
        self._show_error(error.display_message)

    @on(Button.Pressed, "#workspace-delete")
    def _delete_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if self._workspace is not None and self._interactive():
            self.app.push_screen(delete_confirmation(self._workspace, self._locale_controller), self._delete_confirmed)

    def _delete_confirmed(self, confirmed: bool | None) -> None:
        if confirmed and self._editable():
            self.run_worker(self._delete, group="workspace-edit", exclusive=True, exit_on_error=False)

    async def _delete(self) -> None:
        workspace = self._workspace
        if workspace is None or not self._editable():
            return

        async def delete() -> Workspace | None:
            if self._delete_workspace is None:
                raise RuntimeError("Deleting a workspace requires a delete callback.")
            return workspace if await self._delete_workspace(workspace) else None

        await self._commit(delete)

    @on(Button.Pressed, "#workspace-cancel")
    def _cancel_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()

    def action_cancel(self) -> None:
        if self._interactive():
            self.dismiss(None)

    def _allow_click_outside_dismiss(self) -> bool:
        return not self._saving and super()._allow_click_outside_dismiss()
