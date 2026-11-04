# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FilePicker — generic modal for selecting files or directories.

A reusable file/folder selection dialog built on Textual's DirectoryTree.
Supports file-extension filtering, cross-platform drive/folder tabs, and
configurable title.  Dismisses with the selected path string, or ``None``
on cancel/escape.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import logging
import os
import string
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalGroup
from textual.message import Message
from textual.widgets import Button, DirectoryTree, OptionList, Static, TabbedContent, TabPane
from textual.widgets._directory_tree import DirEntry
from textual.widgets.option_list import Option
from textual.widgets.tree import UnknownNodeID

from chrys.app.tui.binding_display import CLOSE_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.screens.dialogs.new_folder import NewFolderDialog
from chrys.app.tui.support.path_shortcuts import get_default_favorites
from chrys.foundation.i18n import MessageRef, msg

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Callable

    from rich.style import Style
    from textual.app import App, ComposeResult
    from textual.widgets._tree import Tree, TreeNode


_PARENT_ENTRY_LABEL = ".."
_SELECT_FILE = msg("tui.file_picker.title.file", fallback="Select File")
_SELECT_FOLDER = msg("tui.file_picker.title.folder", fallback="Select Folder")
_FAVORITES = msg("tui.file_picker.tab.favorites", fallback="Favorites")
_DRIVES = msg("tui.file_picker.tab.drives", fallback="Drives")
_SELECT = msg("tui.file_picker.button.select", fallback="Select")
_CANCEL = msg("tui.file_picker.button.cancel", fallback="Cancel")
_NEW_FOLDER = msg("tui.file_picker.button.new_folder", fallback="New Folder")
_RECENT = msg("tui.file_picker.recent", fallback="─ Recent ─")


class FilePickerMode(enum.Enum):
    """Selection mode for the file dialog."""

    FILE = "file"
    FOLDER = "folder"


# ---------------------------------------------------------------------------
# Recent paths protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class RecentPaths(Protocol):
    """Provider of recently used paths for the FilePicker sidebar."""

    async def get_recent_paths(self) -> list[tuple[str, str]]:
        """Return ``(label, absolute_path)`` pairs, most recent first.

        Implementations should filter out paths that no longer exist.
        """
        ...


# ---------------------------------------------------------------------------
# Cross-platform quick-access locations
# ---------------------------------------------------------------------------


def _get_quick_access() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return ``(favorites, drives)`` as lists of ``(label, path)`` tuples.

    *favorites* are well-known user directories (Home, Desktop, …).
    *drives* are filesystem roots / mounted volumes.
    """
    from chrys.foundation.platform import get_platform

    info = get_platform()
    favorites = get_default_favorites()

    drives: list[tuple[str, str]] = []

    if info.is_macos:
        volumes = Path("/Volumes")
        root_added = False
        if volumes.is_dir():
            try:
                for v in sorted(volumes.iterdir()):
                    if v.is_dir() and not v.name.startswith("."):
                        try:
                            is_root = os.path.samefile(v, "/")
                        except OSError:
                            is_root = False
                        if is_root:
                            # Use the volume name (e.g. "Macintosh HD") but point to "/"
                            drives.append((v.name, "/"))
                            root_added = True
                        else:
                            drives.append((v.name, str(v)))
            except OSError:
                pass
        if not root_added:
            drives.insert(0, ("/", "/"))
    elif info.is_windows:
        for letter in string.ascii_uppercase:
            dp = f"{letter}:\\"
            if os.path.isdir(dp):
                drives.append((f"{letter}:", dp))
    else:  # Linux / other Unix
        drives.append(("/", "/"))
        user = os.environ.get("USER", "")
        for mount_base in ("/mnt", f"/media/{user}" if user else ""):
            if not mount_base:
                continue
            mp = Path(mount_base)
            if mp.is_dir():
                with contextlib.suppress(OSError):
                    drives.extend((entry.name, str(entry)) for entry in sorted(mp.iterdir()) if entry.is_dir())

    return favorites, drives


class _SeparatorOption(Option):
    """Visual-only separator line in the favorites list."""

    def __init__(self, prompt: str) -> None:
        super().__init__(prompt=prompt + "─" * 18, disabled=True, id="__separator__")


class _FilteredDirectoryTree(DirectoryTree):
    """DirectoryTree that can hide files (folder-only mode) or filter by extension."""

    class RootChanged(Message):
        """Posted when the tree root changes through parent navigation."""

        def __init__(self, path: Path) -> None:
            super().__init__()
            self.path = path

    def __init__(
        self,
        path: str | Path,
        *,
        folder_only: bool = False,
        extensions: frozenset[str] | None = None,
        **kwargs: Any,
    ) -> None:
        self._folder_only = folder_only
        self._extensions = extensions  # e.g. frozenset({".py", ".yaml"})
        super().__init__(path, **kwargs)

    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        """Filter out files in folder-only mode or by extension. Dot-entries are shown."""
        return [
            p
            for p in paths
            if p.is_dir()
            or (not self._folder_only and (self._extensions is None or p.suffix.lower() in self._extensions))
        ]

    def render_label(self, node: TreeNode[DirEntry], base_style: Style, style: Style) -> Text:
        """Render the parent navigation entry as a directory, not as a file."""
        if self.is_parent_navigation_node(node):
            node_label = Text(_PARENT_ENTRY_LABEL)
            node_label.stylize(style)
            if self.is_mounted:
                node_label.stylize_before(self.get_component_rich_style("directory-tree--folder", partial=True))
            return Text.assemble((self.ICON_NODE, base_style), node_label)
        return super().render_label(node, base_style, style)

    def _populate_node(self, node: TreeNode[DirEntry], content: Iterable[Path]) -> None:
        """Populate a directory node, adding root-level parent navigation when possible."""
        super()._populate_node(node, content)
        if node is self.root:
            parent = self._parent_path()
            if parent is not None:
                node.add(_PARENT_ENTRY_LABEL, data=DirEntry(parent), allow_expand=False, before=0)

    async def _on_tree_node_selected(self, event: Tree.NodeSelected[DirEntry]) -> None:
        if not self.is_parent_navigation_node(event.node):
            return
        event.prevent_default()
        event.stop()
        self._navigate_to_parent()

    async def _on_tree_node_expanded(self, event: Tree.NodeExpanded[DirEntry]) -> None:
        if not self.is_parent_navigation_node(event.node):
            return
        event.prevent_default()
        event.stop()
        self._navigate_to_parent()

    def is_parent_navigation_node(self, node: TreeNode[DirEntry]) -> bool:
        """Return whether *node* is the synthetic root-level ``..`` entry."""
        if node.parent is not self.root or node.data is None:
            return False
        parent = self._parent_path()
        label = node.label
        label_text = label.plain if isinstance(label, Text) else label
        return parent is not None and label_text == _PARENT_ENTRY_LABEL and node.data.path == parent

    def _navigate_to_parent(self) -> None:
        parent = self._parent_path()
        if parent is None:
            return
        self.path = parent
        self.post_message(self.RootChanged(parent))

    def _parent_path(self) -> Path | None:
        path = self.PATH(self.path)
        parent = path.parent
        if parent == path:
            return None
        return parent


# ---------------------------------------------------------------------------
# FilePicker ModalScreen
# ---------------------------------------------------------------------------


def _nearest_existing_dir(path: str) -> str | None:
    """Return *path* as an absolute directory, or its nearest existing ancestor.

    ``None`` when nothing on the way up exists, or when a relative *path*
    can't be made absolute because the process working directory is gone.
    """
    try:
        current = os.path.abspath(os.path.expanduser(path))
    except OSError:
        return None
    while not os.path.isdir(current):
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent
    return current


class FilePicker(BaseDialog[str | None]):
    """Generic file/folder selection dialog.

    Parameters
    ----------
    mode:
        ``FilePickerMode.FILE`` for file selection,
        ``FilePickerMode.FOLDER`` for directory selection.
    initial_path:
        Starting directory.  Falls back to ``os.getcwd()`` if ``None`` or
        the path does not exist.
    extensions:
        Optional frozenset of allowed file extensions (e.g.
        ``frozenset({".yaml", ".json"})``).  Only effective in FILE mode.
        When set, files not matching the extension are still shown but the
        Select button is disabled for them.
    title:
        Border title text.  Defaults to ``"Select File"`` or
        ``"Select Folder"`` based on mode.
    recent_paths:
        Optional :class:`RecentPaths` provider.  When supplied, recently
        used paths are appended below a separator in the Favorites tab.

    In FOLDER mode a New Folder button creates a folder inside the
    highlighted one and selects it.
    """

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "dismiss_dialog", CLOSE_BINDING, show=False, priority=True),
    ]

    # Below this width, full-size buttons leave the path bar almost no room.
    HORIZONTAL_BREAKPOINTS: list[tuple[int, str]] | None = [(0, "-narrow"), (100, "-regular")]  # noqa: RUF012 - Textual declares the base as an instance attribute.

    CSS_PATH = "file_picker.tcss"

    def __init__(
        self,
        mode: FilePickerMode = FilePickerMode.FOLDER,
        initial_path: str | None = None,
        extensions: frozenset[str] | None = None,
        title: MessageRef | str | None = None,
        recent_paths: RecentPaths | None = None,
    ) -> None:
        self._mode = mode
        self._extensions = extensions
        self._title: MessageRef | str = title or (
            _SELECT_FILE.bind() if mode == FilePickerMode.FILE else _SELECT_FOLDER.bind()
        )
        self._selected_path: str | None = None
        self._recent_paths = recent_paths
        self._recent_paths_task: asyncio.Task[None] | None = None

        # Resolve initial path: a deleted folder opens at its nearest existing parent.
        nearest = _nearest_existing_dir(initial_path) if initial_path else None
        if nearest is not None:
            self._initial_path = nearest
        else:
            from chrys.foundation.platform import safe_getcwd

            self._initial_path = safe_getcwd()

        super().__init__()

    # -- compose --------------------------------------------------------

    def compose(self) -> ComposeResult:
        favorites, drives = _get_quick_access()
        localizer = widget_localizer(self)

        with VerticalGroup(id="fsd-container") as container:
            title = self._title if isinstance(self._title, str) else render_str(localizer, self._title)
            container.border_title = Text(title)
            with Horizontal(id="fsd-body"):
                with Vertical(id="fsd-sidebar"), TabbedContent(id="fsd-tabs"):
                    with TabPane(render_str(localizer, _FAVORITES.bind()), id="fsd-tab-fav"):
                        yield OptionList(
                            *[Option(Text(label), id=path) for label, path in favorites],
                            id="fsd-favorites",
                        )
                    with TabPane(render_str(localizer, _DRIVES.bind()), id="fsd-tab-drv"):
                        yield OptionList(
                            *[Option(Text(label), id=path) for label, path in drives],
                            id="fsd-drives",
                        )
                yield _FilteredDirectoryTree(
                    self._initial_path,
                    folder_only=self._mode == FilePickerMode.FOLDER,
                    extensions=self._extensions if self._mode == FilePickerMode.FILE else None,
                    id="fsd-tree",
                )
            with Horizontal(id="fsd-buttons"):
                yield Static("", id="fsd-path-bar")
                if self._mode == FilePickerMode.FOLDER:
                    yield Button(
                        Text(render_str(localizer, _NEW_FOLDER.bind())),
                        id="fsd-new-folder",
                        variant="success",
                        flat=True,
                    )
                yield Button(
                    Text(render_str(localizer, _SELECT.bind())),
                    id="fsd-select",
                    variant="primary",
                    disabled=True,
                    flat=True,
                )
                yield Button(Text(render_str(localizer, _CANCEL.bind())), id="fsd-cancel", variant="warning", flat=True)

    async def on_mount(self) -> None:
        self.query_one("#fsd-tree", _FilteredDirectoryTree).focus()
        if self._recent_paths:
            self._recent_paths_task = asyncio.create_task(self._load_recent_paths())

    def on_unmount(self) -> None:
        task = self._recent_paths_task
        if task is not None and not task.done():
            task.cancel()

    # -- quick-access sidebar -------------------------------------------

    @on(OptionList.OptionSelected, "#fsd-favorites")
    @on(OptionList.OptionSelected, "#fsd-drives")
    def _on_quick_access_selected(self, event: OptionList.OptionSelected) -> None:
        path = event.option.id
        if not path or path == "__separator__":
            return
        if os.path.isdir(path):
            tree = self.query_one("#fsd-tree", _FilteredDirectoryTree)
            tree.path = path
            # In folder mode, the sidebar entry itself is a valid selection
            self._selected_path = path if self._mode == FilePickerMode.FOLDER else None
            self._update_path_bar()
            self._update_select_button()
            tree.focus()

    # -- tree events ----------------------------------------------------

    @on(DirectoryTree.DirectorySelected)
    def _on_directory_selected(self, event: DirectoryTree.DirectorySelected) -> None:
        if self._mode == FilePickerMode.FOLDER:
            self._selected_path = str(event.path)
            self._update_path_bar()
            self._update_select_button()

    @on(DirectoryTree.FileSelected)
    def _on_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        if self._mode == FilePickerMode.FILE:
            self._selected_path = str(event.path)
            self._update_path_bar()
            self._update_select_button()

    @on(_FilteredDirectoryTree.RootChanged)
    def _on_tree_root_changed(self, event: _FilteredDirectoryTree.RootChanged) -> None:
        self._selected_path = str(event.path) if self._mode == FilePickerMode.FOLDER else None
        self._update_path_bar()
        self._update_select_button()

    @on(DirectoryTree.NodeHighlighted)
    def _on_node_highlighted(self, event: DirectoryTree.NodeHighlighted) -> None:
        node: TreeNode[DirEntry] = event.node
        if node.data is None:
            return
        tree = self.query_one("#fsd-tree", _FilteredDirectoryTree)
        if tree.is_parent_navigation_node(node):
            self._selected_path = None
            self._update_path_bar()
            self._update_select_button()
            return
        path = node.data.path
        if (self._mode == FilePickerMode.FOLDER and path.is_dir()) or (
            self._mode == FilePickerMode.FILE and path.is_file()
        ):
            self._selected_path = str(path)
        else:
            self._selected_path = None
        self._update_path_bar()
        self._update_select_button()

    # -- buttons --------------------------------------------------------

    @on(Button.Pressed, "#fsd-select")
    def _on_select(self, event: Button.Pressed) -> None:
        if self._selected_path:
            self.dismiss(self._selected_path)

    @on(Button.Pressed, "#fsd-cancel")
    def _on_cancel(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    def action_dismiss_dialog(self) -> None:
        self.dismiss(None)

    # -- new folder -----------------------------------------------------

    @on(Button.Pressed, "#fsd-new-folder")
    def _on_new_folder(self, event: Button.Pressed) -> None:
        event.stop()
        if self.dismiss_requested or self.app.screen is not self:
            return
        tree = self.query_one("#fsd-tree", _FilteredDirectoryTree)
        parent_node = self._new_folder_parent(tree)
        parent = parent_node.data.path if parent_node.data is not None else tree.PATH(tree.path)

        def created(path: Path | None) -> None:
            if path is not None:
                self.run_worker(
                    self._reveal_created_folder(parent_node, path),
                    group="fsd-new-folder",
                    exit_on_error=False,
                )

        self.app.push_screen(NewFolderDialog(parent), created)

    @staticmethod
    def _new_folder_parent(tree: _FilteredDirectoryTree) -> TreeNode[DirEntry]:
        """Return the folder a new folder goes into: the highlighted one, else the tree root.

        Read from the cursor itself; ``_selected_path`` follows it through a
        queued message and can lag behind a fast click.
        """
        node = tree.cursor_node
        if node is None or node.data is None or tree.is_parent_navigation_node(node):
            return tree.root
        if node.allow_expand:
            return node
        return node.parent or tree.root

    async def _reveal_created_folder(self, parent_node: TreeNode[DirEntry], created: Path) -> None:
        """Reload *parent_node*, then move the cursor onto the folder just created."""
        tree = self.query_one("#fsd-tree", _FilteredDirectoryTree)
        # If that level was rebuilt or the picker moved elsewhere meanwhile, the
        # folder is still created; it just isn't highlighted.
        if _still_lists(tree, parent_node, created.parent):
            await tree.reload_node(parent_node)
        # Select from the tree's own queue: an expansion still handled there may
        # list this level again and then put back the cursor it saw when that
        # listing started. Queued behind it, the selection lands last.
        tree.call_later(_select_created_folder, tree, parent_node, created)

    # -- helpers --------------------------------------------------------

    async def _load_recent_paths(self) -> None:
        """Load recent paths from the provider and append to favorites."""
        provider = self._recent_paths
        if provider is None:
            return
        try:
            items = await provider.get_recent_paths()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Failed to load file picker recent paths", exc_info=True)
            return
        if not items:
            return
        try:
            fav_list = self.query_one("#fsd-favorites", OptionList)
            fav_list.add_option(_SeparatorOption(render_str(widget_localizer(self), _RECENT.bind())))
            for label, path in items:
                fav_list.add_option(Option(Text(label), id=path))
        except Exception:
            logger.debug("Could not add file picker recent paths", exc_info=True)

    def _update_path_bar(self) -> None:
        bar = self.query_one("#fsd-path-bar", Static)
        bar.update(Text(self._selected_path or ""))

    def _update_select_button(self) -> None:
        btn = self.query_one("#fsd-select", Button)
        if self._selected_path is None:
            btn.disabled = True
            return
        if self._mode == FilePickerMode.FOLDER:
            btn.disabled = not os.path.isdir(self._selected_path)
        else:
            p = Path(self._selected_path)
            if not p.is_file() or (self._extensions is not None and p.suffix.lower() not in self._extensions):
                btn.disabled = True
            else:
                btn.disabled = False


def _still_lists(tree: _FilteredDirectoryTree, node: TreeNode[DirEntry], path: Path) -> bool:
    """Return whether *node* is still in the tree and still lists *path*.

    Moving the picker to another location reuses the root node for it.
    """
    try:
        in_tree = tree.get_node_by_id(node.id) is node
    except UnknownNodeID:
        return False
    return in_tree and node.data is not None and node.data.path == path


def _select_created_folder(tree: _FilteredDirectoryTree, parent_node: TreeNode[DirEntry], created: Path) -> None:
    child = _find_child(tree, parent_node, created.name) if _still_lists(tree, parent_node, created.parent) else None
    if child is not None:
        # Nodes added by the reload get their line numbers when the tree lines
        # are rebuilt; move_cursor reads that number.
        _ = tree.last_line
        # NodeHighlighted then selects the folder like any other highlight.
        tree.move_cursor(child)
    tree.focus()


def _find_child(tree: _FilteredDirectoryTree, node: TreeNode[DirEntry], name: str) -> TreeNode[DirEntry] | None:
    """Return the child of *node* listing the entry called *name*.

    Matched by name, not path: the tree lists a folder under its resolved path,
    so the children of a symlinked folder carry the link target's path. Some
    file systems (HFS+, SMB shares) list a name in a different Unicode
    normalization than it was created with, so a name that matches in NFC also counts.
    """
    wanted = unicodedata.normalize("NFC", name)
    renormalized: TreeNode[DirEntry] | None = None
    for child in node.children:
        if child.data is None or tree.is_parent_navigation_node(child):
            continue
        listed = child.data.path.name
        if listed == name:
            return child
        if renormalized is None and unicodedata.normalize("NFC", listed) == wanted:
            renormalized = child
    return renormalized


def open_folder_picker(app: App[Any], current: str, start: str, on_result: Callable[[str], None]) -> None:
    """Open the folder picker at *current* when it is a folder, else at *start*; *on_result* gets the folder chosen.

    For directory cards outside ``screens/`` (``widgets.directory_path``), which
    may not import this screen themselves.
    """
    current_expanded = os.path.expanduser(current.strip())
    initial = current_expanded if current.strip() and os.path.isdir(current_expanded) else start

    def _on_result(result: str | None) -> None:
        if result:
            on_result(result)

    app.push_screen(FilePicker(mode=FilePickerMode.FOLDER, initial_path=initial), _on_result)
