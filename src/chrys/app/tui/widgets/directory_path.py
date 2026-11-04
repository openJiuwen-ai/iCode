# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared directory path card: styling, removal messages, and the preview of the folder a row resolves to.

Its Browse button opens the folder picker, a screen this module may not
import: each card opens it with ``screens.dialogs.file_picker.open_folder_picker``.
"""

from __future__ import annotations

import os
from pathlib import Path

from rich.text import Text
from textual.message import Message
from textual.widgets import Input, Label

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.widgets.path_entry import PathEntryCard
from chrys.foundation.i18n import msg
from chrys.foundation.platform.paths import is_absolute_path

# The keys stay the skill folder card's, which used them first.
_PATH_DOES_NOT_EXIST = msg("tui.agent_config.skills.path_missing", fallback="Path does not exist")
_ABSOLUTE_PATH_ERROR = msg(
    "tui.agent_config.skills.absolute_path_error",
    fallback="Absolute path - Turn off Workspace relative",
)
_RESOLVED_AT_RUNTIME = msg(
    "tui.agent_config.skills.resolved_at_runtime",
    fallback="Resolved at session runtime",
)
_MISSING_IN_WORKSPACE = msg(
    "tui.agent_config.skills.missing_in_workspace",
    fallback="Missing in current workspace",
)
_RELATIVE_PATH_ERROR = msg(
    "tui.agent_config.skills.relative_path_error",
    fallback="Relative path - Enable Workspace relative",
)
PREVIEW_NOTE = msg("tui.agent_config.skills.preview_note", fallback="  · {note}")
_CURRENT_PATH = msg("tui.agent_config.skills.current_path", fallback="Current: {path}{note}")


class DirectoryPathCard(PathEntryCard):
    """A directory entry whose owner supplies labels and resolved-path preview."""

    DEFAULT_CSS = """
    DirectoryPathCard .sk-row {
        height: auto;
        margin: 0 0 1 0;
    }
    DirectoryPathCard .sk-header-row {
        height: auto;
        margin: 0;
    }
    DirectoryPathCard .sk-label {
        height: 1;
        color: $text-muted;
        margin: 0 0 1 0;
    }
    DirectoryPathCard .sk-separator {
        display: none;
    }
    DirectoryPathCard Input {
        height: 1;
        border: none;
        background: $foreground 8%;
        padding: 0 1;
        margin: 0;
    }
    DirectoryPathCard Input:focus {
        border: none;
        background: $foreground 12%;
    }
    DirectoryPathCard .sk-field {
        width: 1fr;
        height: auto;
        margin: 1 0 0 0;
    }
    DirectoryPathCard .sk-ext-row {
        height: auto;
        margin: 0 0 1 0;
    }
    DirectoryPathCard .sk-path-row {
        height: 1;
        width: 1fr;
        margin: 0;
    }
    DirectoryPathCard .sk-path-row Input {
        width: 1fr;
        min-width: 18;
    }
    DirectoryPathCard .sk-browse-btn {
        min-width: 10;
        height: 1;
        border: solid $tui-border-secondary $border-opacity;
        background: $secondary 18%;
        color: $secondary;
        text-style: bold;
        margin: 0 0 0 1;
        content-align: center middle;
        text-align: center;
    }
    DirectoryPathCard .sk-browse-btn:focus {
        border: solid $tui-border-secondary $border-opacity;
        background: $secondary 18%;
        color: $secondary;
    }
    DirectoryPathCard .sk-browse-btn:hover {
        border: solid $tui-border-secondary $border-opacity;
        background: $secondary 28%;
        color: $secondary;
    }
    DirectoryPathCard .sk-browse-btn.-active {
        border: solid $tui-border-secondary $border-opacity;
        background: $secondary 18%;
        color: $secondary;
    }
    DirectoryPathCard .sk-rel-cb {
        width: auto;
        height: 1;
        border: none;
        background: transparent;
        padding: 0;
        margin: 1 0 0 0;
    }
    DirectoryPathCard .sk-rel-cb:focus {
        background: $foreground 8%;
    }
    DirectoryPathCard .sk-rel-cb > .toggle--button {
        color: $foreground 35%;
        background: $foreground 6%;
    }
    DirectoryPathCard .sk-rel-cb.-on > .toggle--button {
        color: $success;
        background: $secondary 12%;
    }
    DirectoryPathCard .sk-preview {
        width: 1fr;
        height: auto;
        color: $text-muted;
        margin: 1 0 0 0;
        text-wrap: wrap;
        text-overflow: fold;
    }
    """

    class Removed(Message):
        def __init__(self, index: int) -> None:
            self.index = index
            super().__init__()

    _id_prefix = "sk"
    _style_prefix = "sk"

    def _removed_message(self) -> Message:
        return self.Removed(self._index)

    def _refresh_preview(self) -> None:
        """Render the folder this row resolves to, or why it cannot be used."""
        label = self.query_one(f"#{self._preview_id}", Label)
        raw = self.query_one(f"#{self._path_input_id}", Input).value.strip()
        if not raw:
            label.update("")
            label.display = False
            return
        label.display = True

        expanded = os.path.expanduser(raw)
        resolved_path: Path | None = None
        resolve_failed = False
        resolved_text = expanded
        localizer = widget_localizer(self)
        missing_note = render_str(localizer, _PATH_DOES_NOT_EXIST.bind())
        if self._is_relative:
            if is_absolute_path(expanded):
                label.update(Text(render_str(localizer, _ABSOLUTE_PATH_ERROR.bind()), style="$error"))
                return
            if not self._workspace_cwd:
                label.update(Text(render_str(localizer, _RESOLVED_AT_RUNTIME.bind()), style="dim"))
                return
            resolved_path, resolve_failed = self._resolve_for_preview(Path(self._workspace_cwd) / expanded)
            resolved_text = str(resolved_path)
            missing_note = render_str(localizer, _MISSING_IN_WORKSPACE.bind())
        else:
            if not is_absolute_path(expanded):
                label.update(Text(render_str(localizer, _RELATIVE_PATH_ERROR.bind()), style="$error"))
                return
            candidate = Path(expanded)
            if candidate.is_absolute():
                resolved_path, resolve_failed = self._resolve_for_preview(candidate)
                resolved_text = str(resolved_path)
            else:
                # Foreign-absolute under the other platform's rules (``/foo``
                # on Windows, ``C:\foo`` on POSIX): keep the typed text and
                # still report it missing, since it won't load on this host.
                resolved_path = candidate
                resolved_text = expanded

        note = ""
        missing = resolved_path is not None and (resolve_failed or not resolved_path.is_dir())
        if missing:
            note = render_str(localizer, PREVIEW_NOTE.bind(note=missing_note))
        if resolved_path is not None:
            note += self._preview_extra_note(resolved_path)

        label.update(
            Text(
                render_str(localizer, _CURRENT_PATH.bind(path=resolved_text, note=note)),
                style="$warning" if missing else "dim",
            )
        )

    def _browse_from(self) -> tuple[str, str]:
        """What the folder picker opens at: the typed folder, else the workspace (or the current folder)."""
        from chrys.foundation.platform import safe_getcwd

        typed = self.query_one(f"#{self._path_input_id}", Input).value
        return typed, self._workspace_cwd or safe_getcwd()

    def _take_browsed(self, folder: str) -> None:
        """Put the folder the picker chose into this row."""
        self.query_one(f"#{self._path_input_id}", Input).value = folder

    def _preview_extra_note(self, resolved: Path) -> str:
        """A note about *resolved* a subclass adds after the preview; none by default."""
        del resolved
        return ""


__all__ = ["PREVIEW_NOTE", "DirectoryPathCard"]
