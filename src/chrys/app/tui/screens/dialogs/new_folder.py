# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""New-folder prompt opened from the folder picker.

The typed name is checked as the user types with string rules only (no
filesystem access). On submit the folder is created off the event loop; a
filesystem failure stays in the dialog so the name can be fixed, and success
dismisses with the created path.
"""

from __future__ import annotations

import asyncio
import errno
import ntpath
import sys
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import on
from textual.containers import VerticalGroup
from textual.widgets import Button, Static

from chrys.app.tui.binding_display import CANCEL_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.widgets import DialogButtonRow, DialogButtonSpec, EnhancedInput
from chrys.foundation.i18n import DisplayPath, MessageRef, msg
from chrys.foundation.platform import get_platform

if TYPE_CHECKING:
    from textual.app import ComposeResult


_TITLE = msg("tui.file_picker.new_folder.title", fallback="New Folder")
_HINT = msg("tui.file_picker.new_folder.hint", fallback="Create a folder in {path}")
_PLACEHOLDER = msg("tui.file_picker.new_folder.placeholder", fallback="Folder name")
_CREATE = msg("tui.file_picker.new_folder.button.create", fallback="Create")
_CANCEL = msg("tui.file_picker.new_folder.button.cancel", fallback="Cancel")
_DOT_NAME = msg(
    "tui.file_picker.new_folder.error.dot_name",
    fallback='"." and ".." can\'t be used as folder names.',
)
_CONTROL_CHAR = msg(
    "tui.file_picker.new_folder.error.control_char",
    fallback="A folder name can't contain control characters.",
)
_INVALID_CHAR = msg(
    "tui.file_picker.new_folder.error.invalid_char",
    fallback="A folder name can't contain {char}",
)
_TRAILING_DOT = msg(
    "tui.file_picker.new_folder.error.trailing_dot",
    fallback="On Windows, a folder name can't end with a period.",
)
_RESERVED = msg(
    "tui.file_picker.new_folder.error.reserved",
    fallback="{name} is a reserved name on Windows.",
)
_EXISTS = msg(
    "tui.file_picker.new_folder.error.exists",
    fallback="A file or folder named {name} already exists here.",
)
_PERMISSION = msg(
    "tui.file_picker.new_folder.error.permission",
    fallback="You don't have permission to create a folder here.",
)
_READ_ONLY = msg("tui.file_picker.new_folder.error.read_only", fallback="This location is read-only.")
_PARENT_MISSING = msg(
    "tui.file_picker.new_folder.error.parent_missing",
    fallback="The containing folder no longer exists.",
)
_TOO_LONG = msg("tui.file_picker.new_folder.error.too_long", fallback="The folder name or path is too long.")
_NOT_ALLOWED = msg(
    "tui.file_picker.new_folder.error.not_allowed",
    fallback="This name isn't allowed on this drive.",
)
_FAILED = msg("tui.file_picker.new_folder.error.failed", fallback="Couldn't create the folder: {reason}")

# ASCII controls are rejected on every platform before this set is consulted.
_WINDOWS_INVALID_CHARS = frozenset('\\<>:"|?*')
# CPython maps these to EACCES and ENOENT, which would read as "no permission"
# and "the containing folder is gone".
_WINDOWS_ERROR_WRITE_PROTECT = 19
_WINDOWS_ERROR_FILENAME_EXCED_RANGE = 206


class NewFolderError(Exception):
    """A folder could not be created; ``message`` is what the dialog shows."""

    def __init__(self, message: MessageRef) -> None:
        super().__init__(message.definition.key)
        self.message = message


def validate_folder_name(raw: str, *, windows: bool) -> tuple[str, MessageRef | None]:
    """Return the trimmed name and why it can't be used, or ``None`` when it can.

    An empty name has no error; the caller disables creation instead. Only
    string rules apply here: whether the name is taken, too long for the file
    system or refused by the drive is left to :func:`create_folder`.
    """
    name = raw.strip()
    if not name:
        return name, None
    if name in {".", ".."}:
        return name, _DOT_NAME.bind()
    if any(unicodedata.category(char) == "Cc" for char in name):
        return name, _CONTROL_CHAR.bind()
    for char in name:
        if char == "/" or (windows and char in _WINDOWS_INVALID_CHARS):
            return name, _INVALID_CHAR.bind(char=char)
    if windows and name.endswith("."):
        return name, _TRAILING_DOT.bind()
    if windows and ntpath.isreserved(name):
        return name, _RESERVED.bind(name=name)
    return name, None


def create_folder(parent: Path, name: str) -> Path:
    """Create ``parent / name`` (blocking) or raise :class:`NewFolderError`."""
    target = parent / name
    try:
        target.mkdir()
    except OSError as error:
        raise NewFolderError(_describe_error(error, name)) from error
    except ValueError as error:
        # A name the filesystem encoding can't represent; typed names don't get here.
        raise NewFolderError(_FAILED.bind(reason=str(error))) from error
    return target


def _describe_error(error: OSError, name: str) -> MessageRef:
    if sys.platform == "win32":
        if error.winerror == _WINDOWS_ERROR_WRITE_PROTECT:
            return _READ_ONLY.bind()
        if error.winerror == _WINDOWS_ERROR_FILENAME_EXCED_RANGE:
            return _TOO_LONG.bind()
    if isinstance(error, FileExistsError):
        return _EXISTS.bind(name=name)
    if isinstance(error, PermissionError):
        return _PERMISSION.bind()
    if error.errno == errno.EROFS:
        return _READ_ONLY.bind()
    if error.errno == errno.ENAMETOOLONG:
        return _TOO_LONG.bind()
    if isinstance(error, FileNotFoundError | NotADirectoryError):
        return _PARENT_MISSING.bind()
    if error.errno == errno.EINVAL:
        return _NOT_ALLOWED.bind()
    return _FAILED.bind(reason=error.strerror or type(error).__name__)


class NewFolderDialog(BaseDialog[Path | None]):
    """Ask for a folder name and create it inside *parent*.

    Dismisses with the created path, or ``None`` when cancelled. Closing is
    refused while the folder is being created, so a folder that gets created
    is always reported back to the picker.
    """

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "cancel", CANCEL_BINDING, show=False, priority=True),
    ]

    CSS_PATH = "new_folder.tcss"

    def __init__(self, parent: Path) -> None:
        self._folder = parent
        self._windows = get_platform().is_windows
        self._creating = False
        # A create failure is shown only while the name that caused it is typed.
        self._failure: tuple[str, MessageRef] | None = None
        super().__init__()

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        with VerticalGroup(id="new-folder-container") as container:
            container.border_title = Text(render_str(localizer, _TITLE.bind()))
            with VerticalGroup(id="new-folder-inner"):
                yield Static(
                    Text(render_str(localizer, _HINT.bind(path=DisplayPath(self._folder)))),
                    id="new-folder-hint",
                )
                yield EnhancedInput(placeholder=render_str(localizer, _PLACEHOLDER.bind()), id="new-folder-input")
                yield Static(Text(""), id="new-folder-error")
                yield DialogButtonRow(
                    DialogButtonSpec(
                        Text(render_str(localizer, _CREATE.bind())),
                        id="new-folder-create",
                        variant="primary",
                        disabled=True,
                    ),
                    DialogButtonSpec(
                        Text(render_str(localizer, _CANCEL.bind())),
                        id="new-folder-cancel",
                        variant="warning",
                    ),
                    id="new-folder-buttons",
                )

    def on_mount(self) -> None:
        if not self._composed_live:
            return
        self.query_one("#new-folder-input", EnhancedInput).focus()

    @on(EnhancedInput.Changed, "#new-folder-input")
    def _on_name_changed(self, event: EnhancedInput.Changed) -> None:
        event.stop()
        self._refresh_state()

    @on(EnhancedInput.Submitted, "#new-folder-input")
    def _on_name_submitted(self, event: EnhancedInput.Submitted) -> None:
        event.stop()
        self._start_create()

    @on(Button.Pressed, "#new-folder-create")
    def _on_create(self, event: Button.Pressed) -> None:
        event.stop()
        self._start_create()

    @on(Button.Pressed, "#new-folder-cancel")
    def _on_cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_cancel()

    def action_cancel(self) -> None:
        if not self._creating:
            self.dismiss(None)

    def _allow_click_outside_dismiss(self) -> bool:
        return not self._creating and super()._allow_click_outside_dismiss()

    def _start_create(self) -> None:
        # Handlers return at once so a second submit or a backdrop click made
        # while the folder is being created reaches the guard, not a queue.
        if self._creating or self.dismiss_requested or self.app.screen is not self:
            return
        name, error = validate_folder_name(self._input().value, windows=self._windows)
        if not name or error is not None:
            return
        self._creating = True
        self._set_busy(True)
        self.run_worker(self._create(name), group="new-folder", exclusive=True, exit_on_error=False)

    async def _create(self, name: str) -> None:
        created: Path | None = None
        try:
            created = await asyncio.to_thread(create_folder, self._folder, name)
        except NewFolderError as failure:
            self._failure = (name, failure.message)
        finally:
            self._creating = False
        if created is not None:
            self.dismiss_when_topmost(created)
            return
        self._set_busy(False)
        self._refresh_state()
        self._input().focus()

    def _input(self) -> EnhancedInput:
        return self.query_one("#new-folder-input", EnhancedInput)

    def _refresh_state(self) -> None:
        name, error = validate_folder_name(self._input().value, windows=self._windows)
        shown = error
        if shown is None and self._failure is not None and self._failure[0] == name:
            shown = self._failure[1]
        self.query_one("#new-folder-error", Static).update(
            Text("" if shown is None else render_str(widget_localizer(self), shown))
        )
        self.query_one("#new-folder-create", Button).disabled = self._creating or not name or error is not None

    def _set_busy(self, busy: bool) -> None:
        self._input().disabled = busy
        self.query_one("#new-folder-create", Button).disabled = busy
        self.query_one("#new-folder-cancel", Button).disabled = busy
