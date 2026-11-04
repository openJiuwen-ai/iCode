# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Memory configuration panel — composable widget for the Memory tab.

Two list sections: ``Files`` (single ``.md``/``.txt`` files) and ``Folders``
(bounded breadth-first scan for ``.md``/``.txt`` — own files first, then
subfolders up to ``FOLDER_MAX_DEPTH`` levels, at most ``FOLDER_MAX_FILES``
files per folder).  Each row is a card with an Input
field and a delete button.  Both file and folder rows may be absolute or
workspace-relative, matching the Skills tab path-mode UX.  Relative rows are
resolved at runtime by the memory loader against the active session's cwd,
not the cwd that was active when the profile was authored.

Styling closely mirrors :mod:`chrys.app.tui.screens.agents.panels.skills` so the two
tabs share the same visual rhythm.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text
from textual import on
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Button, Label, Static

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.agents.validation_messages import (
    CONTEXT_ERROR,
    MEMORY_CONTEXT,
    PATH_ABSOLUTE_DISABLE,
    PATH_RELATIVE_ENABLE,
)
from chrys.app.tui.screens.agents.validation_messages import (
    MEMORY_FILE as _MEMORY_FILE,
)
from chrys.app.tui.screens.agents.validation_messages import (
    MEMORY_FOLDER as _MEMORY_FOLDER,
)
from chrys.app.tui.widgets import ConfigAddButton, HatchedEmptyState
from chrys.app.tui.widgets import EnhancedInput as Input
from chrys.app.tui.widgets.path_entry import WORKSPACE_RELATIVE, PathEntryCard
from chrys.foundation.i18n import DisplayBlock, MessageRef, msg
from chrys.service.context.memory_loader import FOLDER_MAX_DEPTH, FOLDER_MAX_FILES

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.service.profiles.agents.schema import MemoryConfig

_ALLOWED_MEMORY_FILE_SUFFIXES = frozenset({".md", ".txt"})

_ABSOLUTE_PATH_ERROR = msg(
    "tui.agent_config.memory.absolute_path_error",
    fallback="Absolute path - Turn off Workspace relative",
)
_RESOLVED_AT_RUNTIME = msg(
    "tui.agent_config.memory.resolved_at_runtime",
    fallback="Resolved at session runtime",
)
_MISSING_IN_WORKSPACE = msg(
    "tui.agent_config.memory.missing_in_workspace",
    fallback="Missing in current workspace",
)
_RELATIVE_PATH_ERROR = msg(
    "tui.agent_config.memory.relative_path_error",
    fallback="Relative path - Enable Workspace relative",
)
_PREVIEW_NOTE = msg("tui.agent_config.memory.preview_note", fallback="  · {note}")
_CURRENT_PATH = msg("tui.agent_config.memory.current_path", fallback="Current: {path}{note}")
_AUTO_LOAD = msg("tui.agent_config.memory.title", fallback="Auto-load")
_AUTO_LOAD_DESCRIPTION = msg(
    "tui.agent_config.memory.description",
    fallback=(
        "Pre-load reference content into the agent's system prompt at session start. Files and folders can be "
        "absolute or workspace relative. Only .md and .txt files. Combined cap: 35,000 tokens."
    ),
)
_FILES = msg("tui.agent_config.memory.files", fallback="Files")
_ADD_FILE = msg("tui.agent_config.memory.add_file", fallback="+ Add File")
_FOLDERS = msg("tui.agent_config.memory.folders", fallback="Folders")
_FOLDER_SCAN_NOTE = msg(
    "tui.agent_config.memory.folder_scan_note",
    fallback=(
        "Each folder loads its own files first, then subfolders up to {maximum_depth} levels deep — at most "
        "{maximum_files} files per folder."
    ),
)
_ADD_FOLDER = msg("tui.agent_config.memory.add_folder", fallback="+ Add Folder")
_EMPTY_FILES = msg("tui.agent_config.memory.empty_files", fallback="No memory files configured")
_EMPTY_FOLDERS = msg("tui.agent_config.memory.empty_folders", fallback="No memory folders configured")
_SELECT_MEMORY_FILE = msg("tui.agent_config.memory.select_file", fallback="Select Memory File")
_FILE_MISSING = msg("tui.agent_config.memory.file_missing", fallback="File does not exist")
_FILE_PLACEHOLDER_RELATIVE_POSIX = msg(
    "tui.agent_config.memory.file_placeholder.relative_posix",
    fallback="relative/path/to/file.md",
)
_FILE_PLACEHOLDER_RELATIVE_WINDOWS = msg(
    "tui.agent_config.memory.file_placeholder.relative_windows",
    fallback=r"relative\path\to\file.md",
)
_FILE_PLACEHOLDER_ABSOLUTE_MACOS = msg(
    "tui.agent_config.memory.file_placeholder.absolute_macos",
    fallback="/Users/you/file.md",
)
_FILE_PLACEHOLDER_ABSOLUTE_LINUX = msg(
    "tui.agent_config.memory.file_placeholder.absolute_linux",
    fallback="/home/you/file.md",
)
_FILE_PLACEHOLDER_ABSOLUTE_WINDOWS = msg(
    "tui.agent_config.memory.file_placeholder.absolute_windows",
    fallback=r"C:\path\to\file.md",
)
_FOLDER_PLACEHOLDER_RELATIVE_POSIX = msg(
    "tui.agent_config.memory.folder_placeholder.relative_posix",
    fallback="relative/path/to/folder",
)
_FOLDER_PLACEHOLDER_RELATIVE_WINDOWS = msg(
    "tui.agent_config.memory.folder_placeholder.relative_windows",
    fallback=r"relative\path\to\folder",
)
_FOLDER_PLACEHOLDER_ABSOLUTE_MACOS = msg(
    "tui.agent_config.memory.folder_placeholder.absolute_macos",
    fallback="/Users/you/folder",
)
_FOLDER_PLACEHOLDER_ABSOLUTE_LINUX = msg(
    "tui.agent_config.memory.folder_placeholder.absolute_linux",
    fallback="/home/you/folder",
)
_FOLDER_PLACEHOLDER_ABSOLUTE_WINDOWS = msg(
    "tui.agent_config.memory.folder_placeholder.absolute_windows",
    fallback=r"C:\path\to\folder",
)
_SELECT_MEMORY_FOLDER = msg("tui.agent_config.memory.select_folder", fallback="Select Memory Folder")
_FOLDER_MISSING = msg("tui.agent_config.memory.folder_missing", fallback="Folder does not exist")


def _render_context_error(widget: object, context: str, reference: MessageRef) -> str:
    localizer = widget_localizer(widget)
    return render_str(
        localizer,
        CONTEXT_ERROR.bind(
            context=DisplayBlock(context),
            message=DisplayBlock(render_str(localizer, reference)),
        ),
    )


# ── Single path card ─────────────────────────────────────────────────


class MemoryPathEntryCard(PathEntryCard):
    """Base card for a single memory path entry — Input + Delete."""

    DEFAULT_CSS = """
    MemoryPathEntryCard .mem-row {
        height: auto;
        margin: 0 0 1 0;
    }
    MemoryPathEntryCard .mem-header-row {
        height: auto;
        margin: 0;
    }
    MemoryPathEntryCard Input {
        height: 1;
        border: none;
        background: $foreground 8%;
        padding: 0 1;
        margin: 0;
    }
    MemoryPathEntryCard Input:focus {
        border: none;
        background: $foreground 12%;
    }
    MemoryPathEntryCard .mem-field {
        width: 1fr;
        height: auto;
        margin: 1 0 0 0;
    }
    MemoryPathEntryCard .mem-path-row {
        height: 1;
        width: 1fr;
        margin: 0;
    }
    MemoryPathEntryCard .mem-path-row Input {
        width: 1fr;
        min-width: 18;
    }
    MemoryPathEntryCard .mem-browse-btn {
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
    MemoryPathEntryCard .mem-browse-btn:focus {
        border: solid $tui-border-secondary $border-opacity;
        background: $secondary 18%;
        color: $secondary;
    }
    MemoryPathEntryCard .mem-browse-btn:hover {
        border: solid $tui-border-secondary $border-opacity;
        background: $secondary 28%;
        color: $secondary;
    }
    MemoryPathEntryCard .mem-browse-btn.-active {
        border: solid $tui-border-secondary $border-opacity;
        background: $secondary 18%;
        color: $secondary;
    }
    MemoryPathEntryCard .mem-rel-cb {
        width: auto;
        height: 1;
        border: none;
        background: transparent;
        padding: 0;
        margin: 1 0 0 0;
    }
    MemoryPathEntryCard .mem-rel-cb:focus {
        background: $foreground 8%;
    }
    MemoryPathEntryCard .mem-rel-cb > .toggle--button {
        color: $foreground 35%;
        background: $foreground 6%;
    }
    MemoryPathEntryCard .mem-rel-cb.-on > .toggle--button {
        color: $success;
        background: $secondary 12%;
    }
    MemoryPathEntryCard .mem-preview {
        width: 1fr;
        height: auto;
        color: $text-muted;
        margin: 1 0 0 0;
        text-wrap: wrap;
        text-overflow: fold;
    }
    """

    _style_prefix = "mem"
    _title_label = _MEMORY_FILE
    _placeholder_relative_posix = _FILE_PLACEHOLDER_RELATIVE_POSIX
    _placeholder_relative_windows = _FILE_PLACEHOLDER_RELATIVE_WINDOWS
    _placeholder_absolute_macos = _FILE_PLACEHOLDER_ABSOLUTE_MACOS
    _placeholder_absolute_linux = _FILE_PLACEHOLDER_ABSOLUTE_LINUX
    _placeholder_absolute_windows = _FILE_PLACEHOLDER_ABSOLUTE_WINDOWS
    # File-vs-folder knobs — subclasses pick the picker mode and how the
    # preview decides "exists"; everything else (checkbox, browse, preview
    # rendering) is shared.
    _picker_folder_mode: bool = False
    _picker_extensions: frozenset[str] | None = _ALLOWED_MEMORY_FILE_SUFFIXES
    _picker_title = _SELECT_MEMORY_FILE
    _missing_note = _FILE_MISSING

    class Removed(Message):
        def __init__(self, kind: str, index: int) -> None:
            self.kind = kind
            self.index = index
            super().__init__()

    def _removed_message(self) -> Message:
        return self.Removed(self._id_prefix, self._index)

    def _refresh_preview(self) -> None:
        """Render the current effective file path for this row."""
        from chrys.foundation.platform.paths import is_absolute_path

        # on_mount can fire before this card's children are queryable on slower
        # CI hosts (seen on Linux under xdist load).  Defer one tick instead of
        # crashing — Textual will have the DOM settled by then.
        try:
            label = self.query_one(f"#{self._preview_id}", Label)
            path_input = self.query_one(f"#{self._path_input_id}", Input)
        except NoMatches:
            self.call_after_refresh(self._refresh_preview)
            return
        raw = path_input.value.strip()
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
        missing_note = render_str(localizer, self._missing_note.bind())
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
                # Foreign-absolute under the other platform's rules
                # (e.g. ``/foo`` on Windows, ``C:\foo`` on POSIX). Keep
                # the typed text as-is and still report missing so the
                # user knows it won't load on this host.
                resolved_path = candidate
                resolved_text = expanded

        missing = resolved_path is None or resolve_failed or not self._preview_target_exists(resolved_path)
        note = render_str(localizer, _PREVIEW_NOTE.bind(note=missing_note)) if missing else ""
        label.update(
            Text(
                render_str(localizer, _CURRENT_PATH.bind(path=resolved_text, note=note)),
                style="$warning" if missing else "dim",
            )
        )

    def _preview_target_exists(self, path: Path) -> bool:
        return path.is_file()

    def _open_browse(self) -> None:
        """Open the file/folder picker dialog."""
        from chrys.app.tui.screens.dialogs.file_picker import FilePicker, FilePickerMode
        from chrys.foundation.platform import safe_getcwd

        current = self.query_one(f"#{self._path_input_id}", Input).value.strip()
        current_expanded = os.path.expanduser(current)
        initial = self._workspace_cwd or safe_getcwd()
        if current:
            current_path = Path(current_expanded)
            if current_path.is_file():
                initial = str(current_path.parent)
            elif current_path.is_dir():
                initial = str(current_path)

        def _on_result(result: str | None) -> None:
            if result:
                self.query_one(f"#{self._path_input_id}", Input).value = result

        self.app.push_screen(
            FilePicker(
                mode=FilePickerMode.FOLDER if self._picker_folder_mode else FilePickerMode.FILE,
                initial_path=initial,
                extensions=self._picker_extensions,
                title=render_str(widget_localizer(self), self._picker_title.bind()),
            ),
            _on_result,
        )


class MemoryFileCard(MemoryPathEntryCard):
    """A single auto-load file entry (.md / .txt only)."""

    _title_label = _MEMORY_FILE
    _placeholder_relative_posix = _FILE_PLACEHOLDER_RELATIVE_POSIX
    _placeholder_relative_windows = _FILE_PLACEHOLDER_RELATIVE_WINDOWS
    _placeholder_absolute_macos = _FILE_PLACEHOLDER_ABSOLUTE_MACOS
    _placeholder_absolute_linux = _FILE_PLACEHOLDER_ABSOLUTE_LINUX
    _placeholder_absolute_windows = _FILE_PLACEHOLDER_ABSOLUTE_WINDOWS
    _id_prefix = "mem-file"


class MemoryFolderCard(MemoryPathEntryCard):
    """A single auto-load folder entry (bounded scan for .md / .txt)."""

    _title_label = _MEMORY_FOLDER
    _placeholder_relative_posix = _FOLDER_PLACEHOLDER_RELATIVE_POSIX
    _placeholder_relative_windows = _FOLDER_PLACEHOLDER_RELATIVE_WINDOWS
    _placeholder_absolute_macos = _FOLDER_PLACEHOLDER_ABSOLUTE_MACOS
    _placeholder_absolute_linux = _FOLDER_PLACEHOLDER_ABSOLUTE_LINUX
    _placeholder_absolute_windows = _FOLDER_PLACEHOLDER_ABSOLUTE_WINDOWS
    _id_prefix = "mem-folder"
    _picker_folder_mode = True
    _picker_extensions = None
    _picker_title = _SELECT_MEMORY_FOLDER
    _missing_note = _FOLDER_MISSING

    def _preview_target_exists(self, path: Path) -> bool:
        return path.is_dir()


# ── Main memory config panel ─────────────────────────────────────────


class MemoryConfigPanel(VerticalScroll):
    """Composable widget for the Memory configuration tab."""

    DEFAULT_CSS = """
    MemoryConfigPanel {
        height: 1fr;
        padding: 0 0 0 2;
        scrollbar-size-vertical: 1;
    }
    MemoryConfigPanel .mem-section-title {
        color: $secondary;
        text-style: bold;
        height: 1;
    }
    MemoryConfigPanel .mem-section-desc {
        color: $text-muted;
        height: auto;
        width: 1fr;
        margin: 0;
    }
    MemoryConfigPanel .mem-scan-note {
        color: $text-disabled;
        height: auto;
        width: 1fr;
        margin: 0 2 1 0;
    }
    MemoryConfigPanel .mem-label {
        height: 1;
        color: $text-muted;
        margin: 0 0 1 0;
    }
    MemoryConfigPanel .mem-header-bar {
        height: auto;
        margin: 0 0 1 0;
    }
    MemoryConfigPanel .mem-section-separator {
        height: auto;
        max-height: 1;
        margin: 1 2 0 0;
        border-top: solid $tui-border-foreground 15%;
    }
    MemoryConfigPanel #mem-add-file,
    MemoryConfigPanel #mem-add-folder {
        min-width: 10;
        height: 1;
        margin: 0;
    }
    MemoryConfigPanel #mem-files,
    MemoryConfigPanel #mem-folders {
        height: auto;
    }
    MemoryConfigPanel .mem-empty {
        margin: 0 2 1 0;
    }
    """

    def __init__(
        self,
        memory_config: MemoryConfig | None = None,
        *,
        workspace_cwd: str | None = None,
        read_only: bool = False,
    ) -> None:
        from chrys.service.profiles.agents.schema import MemoryConfig as MC

        self._memory = memory_config or MC()
        self._workspace_cwd = workspace_cwd
        self._read_only = read_only
        # Defensive copies so we mutate panel state, not the live profile
        # object — the parent screen calls ``get_config`` to commit.
        self._files: list[str] = list(self._memory.files)
        self._folders: list[str] = list(self._memory.folders)
        self._rebuilding_files = False
        self._rebuilding_folders = False
        super().__init__()

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        # Top header — section title + description, grouped so the
        # ``mem-header-bar`` bottom margin produces the gap above Files.
        with Vertical(classes="mem-header-bar"):
            yield Label(render_str(localizer, _AUTO_LOAD.bind()), classes="mem-section-title")
            yield Label(
                render_str(localizer, _AUTO_LOAD_DESCRIPTION.bind()),
                classes="mem-section-desc",
            )

        yield Static("", classes="mem-section-separator")

        # Files subsection — title + add button live inside the same
        # ``mem-header-bar`` so its bottom margin gives the gap above the
        # ``#mem-files`` list, mirroring Skills' header-bar/dirs layout.
        with Vertical(classes="mem-header-bar"):
            yield Label(render_str(localizer, _FILES.bind()), classes="mem-label")
            add_file = ConfigAddButton(render_str(localizer, _ADD_FILE.bind()), id="mem-add-file")
            add_file.disabled = self._read_only
            add_file.display = not self._read_only
            yield add_file
        yield Vertical(id="mem-files")

        yield Static("", classes="mem-section-separator")

        # Folders subsection — same pattern, plus the bounded-scan note so
        # the limits are visible where folders are configured.
        with Vertical(classes="mem-header-bar"):
            yield Label(render_str(localizer, _FOLDERS.bind()), classes="mem-label")
            yield Label(
                render_str(
                    localizer,
                    _FOLDER_SCAN_NOTE.bind(
                        maximum_depth=FOLDER_MAX_DEPTH,
                        maximum_files=FOLDER_MAX_FILES,
                    ),
                ),
                classes="mem-scan-note",
            )
            add_folder = ConfigAddButton(render_str(localizer, _ADD_FOLDER.bind()), id="mem-add-folder")
            add_folder.disabled = self._read_only
            add_folder.display = not self._read_only
            yield add_folder
        yield Vertical(id="mem-folders")

    async def on_mount(self) -> None:
        await self._rebuild_files()
        await self._rebuild_folders()

    # ── Rebuilders ────────────────────────────────────────────────────

    async def _rebuild_files(self) -> None:
        container = self.query_one("#mem-files", Vertical)
        self._rebuilding_files = True
        try:
            with self.app.batch_update():
                await container.remove_children()
                if not self._files:
                    await container.mount(
                        HatchedEmptyState(render_str(widget_localizer(self), _EMPTY_FILES.bind()), classes="mem-empty")
                    )
                    return
                await container.mount(
                    *(
                        MemoryFileCard(p, index=i, workspace_cwd=self._workspace_cwd, read_only=self._read_only)
                        for i, p in enumerate(self._files)
                    )
                )
        finally:
            self._rebuilding_files = False

    async def _rebuild_folders(self) -> None:
        container = self.query_one("#mem-folders", Vertical)
        self._rebuilding_folders = True
        try:
            with self.app.batch_update():
                await container.remove_children()
                if not self._folders:
                    await container.mount(
                        HatchedEmptyState(
                            render_str(widget_localizer(self), _EMPTY_FOLDERS.bind()), classes="mem-empty"
                        )
                    )
                    return
                await container.mount(
                    *(
                        MemoryFolderCard(p, index=i, workspace_cwd=self._workspace_cwd, read_only=self._read_only)
                        for i, p in enumerate(self._folders)
                    )
                )
        finally:
            self._rebuilding_folders = False

    # ── Add / remove ──────────────────────────────────────────────────

    @on(Button.Pressed, "#mem-add-file")
    async def _on_add_file(self, _event: Button.Pressed) -> None:
        if self._read_only:
            return
        # Sync edits before re-render so in-progress typing isn't lost.
        self._files = self._collect_paths(MemoryFileCard)
        self._files.insert(0, "")
        await self._rebuild_files()

    @on(Button.Pressed, "#mem-add-folder")
    async def _on_add_folder(self, _event: Button.Pressed) -> None:
        if self._read_only:
            return
        self._folders = self._collect_paths(MemoryFolderCard)
        self._folders.insert(0, "")
        await self._rebuild_folders()

    @on(MemoryPathEntryCard.Removed)
    async def _on_remove(self, event: MemoryPathEntryCard.Removed) -> None:
        if self._read_only:
            return
        if event.kind == "mem-file":
            self._files = self._collect_paths(MemoryFileCard)
            if 0 <= event.index < len(self._files):
                self._files.pop(event.index)
                await self._rebuild_files()
        elif event.kind == "mem-folder":
            self._folders = self._collect_paths(MemoryFolderCard)
            if 0 <= event.index < len(self._folders):
                self._folders.pop(event.index)
                await self._rebuild_folders()

    def _collect_paths(self, card_cls: type[MemoryPathEntryCard]) -> list[str]:
        """Snapshot the current Input values from cards of the given type.

        Includes empty strings so list indices remain stable across the
        ``add → rebuild`` cycle.  ``get_config`` filters out blanks for
        the saved YAML.
        """
        seed = self._files if card_cls is MemoryFileCard else self._folders
        # Only the rebuilding section uses its snapshot; the other stays editable.
        rebuilding = self._rebuilding_files if card_cls is MemoryFileCard else self._rebuilding_folders
        if rebuilding:
            return list(seed)
        cards = list(self.query(card_cls))
        if len(cards) != len(seed):
            return list(seed)
        return [card.get_path() for card in cards]

    # ── Public API ────────────────────────────────────────────────────

    def get_config(self) -> MemoryConfig:
        from chrys.service.profiles.agents.schema import MemoryConfig as MC

        files = [p for p in self._collect_paths(MemoryFileCard) if p]
        folders = [p for p in self._collect_paths(MemoryFolderCard) if p]
        return MC(files=files, folders=folders)

    def validate(self) -> list[str]:
        """Validate memory configuration via :func:`validate_memory_config`.

        Per-entry path-mode checks are handled from the mounted cards.
        Extension checks and cross-entry checks (case-insensitive duplicate
        files/folders, filesystem-root folders) are delegated to the pure
        validator so the same rules apply whether the config arrives via
        the UI or directly from a YAML profile.  We pass
        the *unfiltered* path list so 1-based error indices stay aligned with
        the on-screen card order even when blanks sit between filled rows.
        """
        from chrys.foundation.platform.paths import is_absolute_path
        from chrys.service.context.memory_loader import validate_memory_config
        from chrys.service.profiles.agents.schema import MemoryConfig as MC

        files = self._collect_paths(MemoryFileCard)
        folders = self._collect_paths(MemoryFolderCard)
        localizer = widget_localizer(self)
        workspace_relative_label = render_str(localizer, WORKSPACE_RELATIVE.bind())
        errors: list[str] = []
        for label_definition, card_cls in ((_MEMORY_FILE, MemoryFileCard), (_MEMORY_FOLDER, MemoryFolderCard)):
            label = render_str(localizer, label_definition.bind())
            for i, card in enumerate(self.query(card_cls), 1):
                context = render_str(localizer, MEMORY_CONTEXT.bind(label=label, index=i))
                path = card.get_path()
                if not path:
                    continue
                expanded = os.path.expanduser(path)
                is_relative = card.is_relative_checked()
                if is_relative and is_absolute_path(expanded):
                    errors.append(
                        _render_context_error(
                            self,
                            context,
                            PATH_ABSOLUTE_DISABLE.bind(
                                path=DisplayBlock(path),
                                label=workspace_relative_label,
                            ),
                        )
                    )
                    continue
                if not is_relative and not is_absolute_path(expanded):
                    errors.append(
                        _render_context_error(
                            self,
                            context,
                            PATH_RELATIVE_ENABLE.bind(
                                path=DisplayBlock(path),
                                label=workspace_relative_label,
                            ),
                        )
                    )
                    continue
        errors.extend(validate_memory_config(MC(files=files, folders=folders), workspace_cwd=self._workspace_cwd))
        return errors
