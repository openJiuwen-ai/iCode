# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Every workspace with its saved chat sessions; the current chat follows its runtime workspace."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import islice
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import events, on
from textual.app import ScreenStackError
from textual.containers import Vertical
from textual.dom import NoScreen
from textual.message import Message
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Tree

from chrys.app.tui import workspace_text as labels
from chrys.app.tui.i18n import render_str
from chrys.app.tui.util.formatting import fallback_title
from chrys.app.tui.util.liveness import composed_live
from chrys.app.tui.util.paths import middle_cut_path
from chrys.app.tui.util.time_ago import time_ago, time_ago_until
from chrys.app.tui.util.visibility import (
    is_widget_displayed_on_active_screen,
    is_widget_shown,
)
from chrys.app.tui.util.workspace_order import WorkspaceOrder
from chrys.app.tui.widgets.sidebar.empty_state import SidebarEmptyStateLabel
from chrys.app.tui.widgets.sidebar.toc import _TocTree
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_scalar
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.util.abandonable import run_abandonable
from chrys.service.state.session_listing import DEFAULT_SURFACES
from chrys.service.state.store import ChatSessionMeta

if TYPE_CHECKING:
    from collections.abc import Iterable

    from textual.app import ComposeResult
    from textual.screen import Screen
    from textual.timer import Timer
    from textual.widgets.tree import TreeNode

    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.models.workspace import Workspace
    from chrys.service.state.session_listing import SessionListing
    from chrys.service.state.store import StateStore
    from chrys.service.workspaces.registry import RegistrySnapshot, WorkspaceRegistry

logger = logging.getLogger(__name__)

_LOAD_FAILED = msg("tui.workspaces.load_failed", fallback="Could not load workspaces.")
_PAGE = 5
"""Rows a level shows, and what each ``Show more`` adds."""
_CLOCK_SECONDS = 60
"""How often the relative times move on: the shortest step ``time_ago`` shows is a minute."""
_MIN_TITLE_CELLS = 8
"""A row keeps its time or its symbols only while its title still shows this many cells."""
_WORKSPACE = "__workspace__"
_PAGE_ROW = "__page__"
_SPACER = "__workspace_spacer__"
_EMPTY = "__workspace_empty__"
_UNSPECIFIED_GROUP = "__unspecified__"
_GROUP_INDENT = " "
_SESSION_INDENT = "  "
_LINK_GAP = 2
"""Cells between the two links of a page row."""


@dataclass(frozen=True, slots=True)
class WorkspaceSessionRow:
    session_id: str
    title: str
    updated_at: datetime
    current: bool = False


@dataclass(frozen=True, slots=True)
class SavedWorkspaceSession:
    session_id: str
    title: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class _Group:
    """A workspace row, or the Unspecified last group, with the chat sessions listed under it."""

    workspace: Workspace | None
    rows: list[WorkspaceSessionRow]


@dataclass(frozen=True, slots=True)
class _CursorSpot:
    """What the highlight was on before a rebuild.

    A session row also remembers its group and its place among that group's
    rows, because a rebuild can give its node another row.
    """

    data: str
    slot: tuple[str, int] | None = None
    line: int = -1
    visible: bool = False


def _entries_by_group(
    listing: SessionListing | None, snapshot: RegistrySnapshot, current_id: str
) -> tuple[dict[str, list[SavedWorkspaceSession]], SavedWorkspaceSession | None, bool]:
    """Keep only the display and restore fields, in listing order, and whether F1 lists the current chat.

    The groups list the surfaces F1 lists by default; the current chat is found on any surface, as F1 can open it.
    """
    groups: dict[str, list[SavedWorkspaceSession]] = {}
    current = None
    current_listed = False
    if listing is None:
        return groups, current, current_listed
    for entry in listing.entries:
        if isinstance(entry.meta, ChatSessionMeta):
            listed = entry.surface in DEFAULT_SURFACES
            if not listed and entry.session_id != current_id:
                continue
            row = SavedWorkspaceSession(entry.session_id, entry.meta.display_title, entry.listed_at)
            if row.session_id == current_id:
                current, current_listed = row, listed
            if listed and snapshot.error is None:
                key = snapshot.workspace_id_for_session(row.session_id) or _UNSPECIFIED_GROUP
                groups.setdefault(key, []).append(row)
    return groups, current, current_listed


def _regroup(
    groups: dict[str, list[SavedWorkspaceSession]], snapshot: RegistrySnapshot
) -> dict[str, list[SavedWorkspaceSession]]:
    """Reassign the committed sample in listing order, retaining only the newest occurrence of each id."""
    if snapshot.error is not None:
        return {}
    rows = sorted(
        (row for items in groups.values() for row in items),
        key=lambda row: (row.updated_at, row.session_id),
        reverse=True,
    )
    result: dict[str, list[SavedWorkspaceSession]] = {}
    seen: set[str] = set()
    for row in rows:
        if row.session_id in seen:
            continue
        seen.add(row.session_id)
        key = snapshot.workspace_id_for_session(row.session_id) or _UNSPECIFIED_GROUP
        result.setdefault(key, []).append(row)
    return result


def _same_groups(snapshot: RegistrySnapshot, other: RegistrySnapshot | None) -> bool:
    """Both group the chats alike: the same workspaces and memberships, recent use aside.

    A missing file reads as a new empty snapshot each time, and opening a chat only marks its workspace used.
    """
    return snapshot is other or (
        other is not None
        and snapshot.error is None
        and other.error is None
        and snapshot.workspaces == other.workspaces
        and snapshot.sessions == other.sessions
    )


def _insert_saved(rows: list[SavedWorkspaceSession], row: SavedWorkspaceSession) -> None:
    """Insert in listing order: newest first, then by ID."""
    order = (row.updated_at, row.session_id)
    rows.insert(
        next((i for i, saved in enumerate(rows) if (saved.updated_at, saved.session_id) < order), len(rows)), row
    )


def _group_key(workspace: Workspace | None) -> str:
    """A group's key: its workspace id, or the Unspecified group."""
    return _UNSPECIFIED_GROUP if workspace is None else workspace.id


def _group_data(key: str) -> str:
    """The node data of a group's own row."""
    return _UNSPECIFIED_GROUP if key == _UNSPECIFIED_GROUP else f"{_WORKSPACE}:{key}"


def _empty_data(key: str) -> str:
    """The node data of an empty group's "No sessions" row, unique per group so a group redraw replaces it."""
    return f"{_EMPTY}:{key}"


class _RowSelected(Tree.NodeSelected[str]):
    """A selection with the row its node showed then: a redraw can give the node another row before it is handled."""

    def __init__(self, node: TreeNode[Any]) -> None:
        super().__init__(node)
        self.row: str | None = node.data


class _WorkspaceTree(_TocTree):
    """The page's tree: a click on a page row is the page's to take, not the tree's.

    Textual's ``Tree`` moves the highlight to the clicked row and then selects
    it. A page row instead only acts on its links, and a link leaves the
    highlight alone: the rebuild it causes finds that row again by data. A
    click also never focuses the tree, so only the keyboard shows a cursor.
    """

    def __init__(self, page: WorkspacePanel, *, id: str | None = None) -> None:
        super().__init__("", id=id)
        self._page = page

    def focus_on_click(self) -> bool:
        return False

    def post_message(self, message: Message) -> bool:
        if type(message) is Tree.NodeSelected:
            message = _RowSelected(message.node)
        return super().post_message(message)

    def on_resize(self, _event: events.Resize) -> None:
        # The first read can show this hidden tree without resizing the page itself.
        self._page._fit_rows()
        self.call_after_refresh(self._scroll_focused_cursor)

    def _scroll_focused_cursor(self) -> None:
        node = self.cursor_node
        top = self.scroll_offset.y
        if (
            self.has_focus
            and node is not None
            and not top <= self.cursor_line < top + self.scrollable_content_region.height
        ):
            self.scroll_to_node(node, animate=False)

    def get_component_rich_style(self, *names: str, partial: bool = False, default: Style | None = None) -> Style:
        # Textual lays the cursor's full style, colour included, over the label of
        # the line it sits on. Unfocused the cursor is not shown at all, so it must
        # not repaint that line either: the current workspace's yellow would turn
        # white whenever the hidden cursor happened to rest on its name.
        if names == ("tree--cursor",) and not self.has_focus:
            return Style()
        return super().get_component_rich_style(*names, partial=partial, default=default)

    def validate_cursor_line(self, value: int) -> int:
        """Step over rows with nothing to act on: spacers, and a page row too narrow for any link."""
        line = super().validate_cursor_line(value)
        step = 1 if value > self.cursor_line else -1
        # Past the last row to act on (End, Page Down), the nearest one before it.
        found = self._actionable_line(line, step)
        if found is None:
            found = self._actionable_line(line, -step)
        return self.cursor_line if found is None else found

    def _actionable_line(self, line: int, step: int) -> int | None:
        while (node := self.get_node_at_line(line)) is not None and self._page.inert(node.data):
            ahead = super().validate_cursor_line(line + step)
            if ahead == line:
                return None
            line = ahead
        return line

    def render_line(self, y: int) -> Strip:
        node = self.get_node_at_line(y + self.scroll_offset.y)
        if node is not None and node.data == _SPACER:
            # The tree's own base style, as Tree draws the lines past its last node.
            return Strip.blank(self.size.width, self.rich_style)
        return super().render_line(y)

    async def _on_click(self, event: events.Click) -> None:
        """Take a page row's click from the tree; every other row keeps the tree's own."""
        if self._page._page_click(self, event):
            event.prevent_default()
            event.stop()


class WorkspacePanel(Widget, can_focus=False):
    DEFAULT_CSS = """
    WorkspacePanel { height: 1fr; }
    WorkspacePanel > #workspace-sessions-box { height: 1fr; }
    WorkspacePanel #workspace-sessions-tree {
        height: 1fr; background: transparent; padding: 0;
        /* Match the stable scrollbar gutter with an empty column at the left. */
        margin-left: 1;
        overflow-x: hidden; scrollbar-size: 1 1; scrollbar-gutter: stable;
    }
    /* No row background for the mouse: a click opens a row without focusing the tree. */
    WorkspacePanel Tree > .tree--highlight-line { background: transparent; }
    WorkspacePanel Tree:focus > .tree--cursor { color: $foreground; background: $primary 28%; }
    WorkspacePanel Tree:ansi:focus > .tree--cursor { background: $tui-ansi-turn-focus-background; }
    WorkspacePanel .workspace-empty { width: 1fr; height: 1fr; content-align: center middle; color: $text-muted; }
    WorkspacePanel > .workspace--current { color: $warning; }
    WorkspacePanel > .workspace--group { background: $foreground 15%; }
    WorkspacePanel:ansi > .workspace--group { background: $tui-ansi-turn-background; }
    """
    COMPONENT_CLASSES: ClassVar[set[str]] = {"workspace--current", "workspace--group"}
    """The theme's own yellow for the current session's workspace name and its own row;
    the grey band under each group's own row, the status bar labels' own, so the purple
    keyboard cursor stays the only purple row."""

    class OrderWanted(Message):
        """Ask the main screen, the sole physical-path ordering owner, for a fresh order."""

    class SessionSelected(Message):
        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    class NewSessionRequested(Message):
        def __init__(self, workspace_id: str | None) -> None:
            super().__init__()
            self.workspace_id = workspace_id

    class EditRequested(Message):
        def __init__(self, workspace: Workspace) -> None:
            super().__init__()
            self.workspace = workspace

    def __init__(self, *, locale_controller: LocaleController | None = None) -> None:
        super().__init__()
        self._locale_controller = locale_controller
        self._workspace: Workspace | None = None
        self._store: StateStore | None = None
        self._registry: WorkspaceRegistry | None = None
        self._startup_pending = True
        self._current_id = ""
        self._current_title = ""
        # The last read: registered workspaces and the ordered snapshot of their saved sessions.
        self._workspaces: tuple[Workspace, ...] | None = None
        self._saved: dict[str, list[SavedWorkspaceSession]] = {}
        self._current_saved: SavedWorkspaceSession | None = None
        self._current_listed: bool | None = None
        """Whether F1 lists the current chat by default, as the page's listed row, a read or a save said; None until one says."""
        # An unsaved chat is as old as the moment it became current.
        self._current_since = datetime.now(UTC)
        self._registry_error: MessageRef | None = None
        """The error from the last attempt to read the workspace page."""
        # What each node of the current tree is, so a relabel or a restore finds it by data.
        self._row_nodes: dict[str, WorkspaceSessionRow] = {}
        self._group_nodes: dict[str, Workspace | None] = {}
        """Each group row's node data, and the workspace it names (None for Unspecified)."""
        self._page_nodes: dict[str, tuple[str, bool, bool]] = {}
        self._group_rows: dict[str, list[WorkspaceSessionRow]] = {}
        self._row_slots: dict[str, tuple[str, int]] = {}
        """Each drawn session's group key and its place among that group's rows."""
        # The clickable spans of each page row, in cells from the row's first column.
        self._link_spans: dict[str, tuple[tuple[str, tuple[int, int]], ...]] = {}
        self._tooltip_hint: tuple[str, str, str] | None = None
        self._clock: Timer | None = None
        self._ages_until: dict[str, datetime] = {}
        """When each drawn row's relative time next reads differently."""
        # Both limits live only in memory, by group key, so a read keeps what the user opened.
        self._workspace_limit = _PAGE
        self._session_limits: dict[str, int] = {}
        self._collapsed: set[str] = set()
        self._rows_width = 0
        self._theme_styles: tuple[Style, Style] | None = None
        """The resolved colours the rows were drawn in, so a theme change repaints only when they differ."""
        self._ordered: tuple[Workspace, ...] = ()
        self._refresh_pending = False
        self._stale = False
        self._queued: Literal["read", "regroup"] | None = None
        self._refresh_running: Literal["read", "regroup"] | None = None
        self._current_change = 0
        self._latest_order: WorkspaceOrder | None = None
        self._loaded_snapshot: RegistrySnapshot | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="workspace-sessions-box"):
            tree = _WorkspaceTree(self, id="workspace-sessions-tree")
            tree.show_root = False
            tree.show_guides = False
            tree.display = False
            # Until rows exist, the app's first focus must not land here and select this tab.
            tree.disabled = True
            yield tree
            yield SidebarEmptyStateLabel(Text(self._render_message(labels.LOADING.bind())), classes="workspace-empty")

    def set_startup_pending(self, pending: bool) -> None:
        """Show loading until startup either binds a session or gives up restoring it."""
        if self._startup_pending == pending:
            return
        self._startup_pending = pending
        if self.is_mounted and self._registry is None:
            hint = labels.LOADING if pending else labels.NO_WORKSPACES
            self.query_one(".workspace-empty", SidebarEmptyStateLabel).update(
                Text(self._render_message(hint.bind())), layout=False
            )

    def set_session(
        self,
        workspace: Workspace | None,
        store: StateStore | None,
        session_id: str,
        title: str,
        *,
        registry: WorkspaceRegistry,
    ) -> None:
        fields = (workspace, store, registry, session_id, title)
        if (
            self._workspace,
            self._store,
            self._registry,
            self._current_id,
            self._current_title,
        ) == fields:
            # A save that changed nothing on this page repaints nothing: an
            # unchanged list must not request a layout of the whole screen.
            return
        if workspace != self._workspace and self._workspaces is not None:
            # Keep saved definitions until the main screen supplies the new order.
            registered = {item.id for item in self._workspaces}
            self._ordered = tuple(item for item in self._ordered or self._workspaces if item.id in registered)
            if workspace is not None and workspace.id not in registered:
                self._ordered = (*self._ordered, workspace)
        first_binding = self._registry is None and self._loaded_snapshot is None
        self._workspace, self._store, self._registry = workspace, store, registry
        # Bound to a session: startup has settled, whatever it opened.
        self._startup_pending = False
        if session_id != self._current_id:
            if self._refresh_running == "read":
                self._request_refresh("read")
            # A session the page already read keeps the row it was listed with, so
            # selecting it never moves it; only a session the page never read is new.
            saved = self._saved_entry(session_id)
            self._current_saved = saved
            self._current_listed = True if saved is not None else None
            self._current_since = saved.updated_at if saved is not None else datetime.now(UTC)
        self._current_id, self._current_title = session_id, title
        if first_binding:
            self._request_refresh("read")
        if self.is_mounted:
            self._render_rows()
            self.screen_resumed()

    def update_current_title(self, session_id: str, title: str) -> None:
        if session_id != self._current_id or title == self._current_title:
            return
        self._current_title = title
        if self._current_saved is not None:
            self._update_saved_row(title, self._current_saved.updated_at)
        else:
            self._current_change += 1
            self._render_current_group()

    def current_session_saved(self, session_id: str, updated_at: datetime, surface: SessionSurface | None) -> None:
        if session_id != self._current_id:
            return
        # The saved surface says whether F1 lists the chat now, so a first save or a turn here joins
        # the list, and a chat continued on another surface leaves it, without reading it again.
        listed = (surface or SessionSurface.TUI) in DEFAULT_SURFACES
        if listed != self._current_listed:
            # Counted as a change, so a read still running merges this save rather than its older surface.
            self._current_change += 1
        self._current_listed = listed
        title = self._current_title or (self._current_saved.title if self._current_saved else "")
        self._update_saved_row(title, updated_at)

    def _update_saved_row(self, title: str, updated_at: datetime, *, render: bool = True) -> None:
        """Record the current chat's save, adding or removing its row as F1 now lists it or not."""
        row = SavedWorkspaceSession(self._current_id, title, updated_at)
        previous = self._current_saved
        snapshot = self._loaded_snapshot
        group = _group_key(self._workspace)
        if snapshot is not None:
            group = snapshot.workspace_id_for_session(row.session_id) or _UNSPECIFIED_GROUP
        rows = self._saved.get(group, [])
        index = next((i for i, saved in enumerate(rows) if saved.session_id == row.session_id), None)
        # Like a read, an unreadable registry groups no chats.
        joins = index is None and self._current_listed is True and snapshot is not None and snapshot.error is None
        # Continued on another surface since the page listed it: F1 no longer lists it by default.
        leaves = index is not None and self._current_listed is False
        if row == previous and not joins and not leaves:
            return
        self._current_saved = row
        self._current_change += 1
        if leaves:
            rows.pop(index)
        elif index is not None:
            if rows[index].updated_at == row.updated_at:
                rows[index] = row
            else:
                rows.pop(index)
                _insert_saved(rows, row)
        elif joins:
            _insert_saved(rows, row)
            self._saved[group] = rows
        if render:
            self._render_current_group()

    def on_mount(self) -> None:
        # The row colours are resolved into the labels, so a theme change redraws them.
        self.app.theme_changed_signal.subscribe(self, self._theme_changed)
        self.app.screen_change_signal.subscribe(self, self._screen_changed)
        # Moves the relative times on; it runs only while the page is drawn.
        self._clock = self.set_interval(_CLOCK_SECONDS, self._tick, pause=True)

    def on_hide(self) -> None:
        if self._clock is not None:
            self._clock.pause()

    def _tick(self) -> None:
        """Relabel only the rows whose relative time now reads differently."""
        if not self._page_readable():
            # Nothing sends a Hide when an opaque screen or a hidden container covers the page.
            if self._clock is not None:
                self._clock.pause()
            self._stale = True
            return
        now = datetime.now(UTC)
        for node in self.query_one(Tree).root.children:
            row = self._row_nodes.get(str(node.data))
            if row is not None and self._ages_until.get(row.session_id, now) <= now:
                node.set_label(self._row_label(row))

    def on_unmount(self) -> None:
        self.app.theme_changed_signal.unsubscribe(self)
        self.app.screen_change_signal.unsubscribe(self)
        self._queued = None
        self._refresh_pending = False

    def _listed_workspaces(self) -> tuple[Workspace, ...]:
        """Use saved definitions for groups and edits; membership marks the current id."""
        listed = self._ordered or self._workspaces or ()
        current = self._workspace
        if current is None:
            return listed
        if any(workspace.id == current.id for workspace in listed):
            return listed
        return (current, *listed)

    def _current_row(self) -> WorkspaceSessionRow:
        """This chat's row, dated by its saved record when the page has read one."""
        saved = self._current_saved
        return WorkspaceSessionRow(
            self._current_id,
            self._current_title or (saved.title if saved else "") or fallback_title(self._current_id),
            saved.updated_at if saved else self._current_since,
            True,
        )

    def _saved_entry(self, session_id: str) -> SavedWorkspaceSession | None:
        if not session_id:
            return None
        return next((row for rows in self._saved.values() for row in rows if row.session_id == session_id), None)

    def can_restore_session(self, session_id: str) -> bool:
        """Only a row this tree draws can be opened: a level's limit hides the rest."""
        return session_id != self._current_id and session_id in self._row_nodes

    def refresh_sessions(self) -> None:
        """The saved session sample needs a fresh listing."""
        self._request_refresh("read")

    def workspace_order_changed(self, order: WorkspaceOrder) -> None:
        """Accept the main screen's order without resampling sessions."""
        self._latest_order = order
        # After a failed list read the rows are stale, so even unchanged groups take the regroup path to a read.
        # A read or regroup in flight commits after this and may hold an older registry, so regroup once it is done.
        if (
            self._refresh_running is None
            and self._registry_error is None
            and _same_groups(order.snapshot, self._loaded_snapshot)
        ):
            self._loaded_snapshot = order.snapshot
            ordered = self._order_for(order.snapshot)
            if ordered != self._ordered:
                # Another chat in the same workspace brings a new order object with the same groups.
                self._ordered = ordered
                self._render_rows()
        else:
            self._request_refresh("regroup")

    def _order_for(self, snapshot: RegistrySnapshot) -> tuple[Workspace, ...]:
        """Use the main screen's order or retain surviving groups until it catches up."""
        order = self._latest_order
        if (
            order is not None
            and order.snapshot.workspaces == snapshot.workspaces
            and order.snapshot.last_used_at == snapshot.last_used_at
        ):
            return order.workspaces
        remaining = {workspace.id: workspace for workspace in snapshot.workspaces}
        ordered = [remaining.pop(workspace.id) for workspace in self._ordered if workspace.id in remaining]
        ordered.extend(
            sorted(remaining.values(), key=lambda workspace: snapshot.last_used_at.get(workspace.id, ""), reverse=True)
        )
        if order is not None:
            # Before its first order the main screen is already computing one; it arrives unasked.
            self.post_message(self.OrderWanted())
        return tuple(ordered)

    def _request_refresh(self, request: Literal["read", "regroup"]) -> None:
        if request == "read" or self._queued is None:
            self._queued = request
        self._stale = True
        self.screen_resumed()

    def screen_resumed(self) -> None:
        """Retry a deferred read or render when the owning screen becomes visible."""
        if not self._stale and self._queued is None:
            return
        if self._registry is not None and self.is_mounted and not self._refresh_pending:
            self._refresh_pending = self.call_after_refresh(self._start_refresh)

    def _screen_changed(self, screen: Screen) -> None:
        """Observe every stack change, including an opaque screen pushed above a dialog."""
        if screen is not self.screen and screen.styles.background.a >= 1:
            self._request_refresh("read")
        elif self._page_readable() and (self._queued is not None or self._stale):
            self.screen_resumed()

    def on_show(self) -> None:
        """Read fresh sessions each time the page becomes visible."""
        if self._registry is not None:
            self._request_refresh("read")

    def _page_readable(self) -> bool:
        """Read on the foreground page or when it shows through an overlay.

        The compositor's cut comes from the last arranged frame, so the page that
        just opened can miss it for one frame. Dropping its only read over that
        would leave the page empty until the next session event.
        """
        if not all(node.display for node in self.ancestors_with_self):
            return False
        try:
            screen = self.screen
            active = self.app.screen
        except NoScreen, ScreenStackError:
            # Shutdown empties the stack before it cancels the clock and workers that ask.
            return False
        if screen is active:
            return is_widget_shown(self) or is_widget_displayed_on_active_screen(self)
        # Textual's current screens include the backdrop. An opaque top screen
        # still has a backdrop in that list, but none of the backdrop shows through.
        return active.styles.background.a < 1 and screen.is_current and is_widget_shown(self)

    def _start_refresh(self) -> None:
        self._refresh_pending = False
        if (
            self._refresh_running is not None
            or self._registry is None
            or not composed_live(self)
            or not self._page_readable()
        ):
            return
        if self._queued is not None:
            self._refresh_running = self._queued
            self.run_worker(self._refresh(), group="workspace-sessions", exit_on_error=False)
        elif self._stale:
            self._stale = False
            self._render_rows()

    async def _refresh(self) -> None:
        """One worker drains coalesced samples and regroup requests while the page is readable."""
        try:
            while composed_live(self) and self._page_readable() and self._queued is not None:
                self._refresh_running, self._queued = self._queued, None
                if self._refresh_running == "read":
                    await self._load()
                else:
                    await self._regroup_rows()
        finally:
            self._refresh_running = None
            if composed_live(self) and (self._queued is not None or self._stale):
                self.screen_resumed()

    def _read_superseded(self) -> bool:
        """Check every full-read boundary before starting another stage or committing."""
        if not composed_live(self):
            return True
        if not self._page_readable():
            self._request_refresh("read")
        return self._queued == "read"

    async def _load(self) -> None:
        store, registry = self._store, self._registry
        if registry is None:
            return
        current_id, change = self._current_id, self._current_change
        try:
            listing = await store.open_session_listing(kind="chat") if store is not None else None
            if self._read_superseded():
                return
            snapshot = await run_abandonable("icode-workspace-registry", registry.snapshot)
            if self._read_superseded():
                return
            saved, current, listed = await asyncio.to_thread(_entries_by_group, listing, snapshot, current_id)
            del listing
            if self._read_superseded():
                return
        except Exception:
            if self._read_superseded():
                return
            logger.warning("Could not load the workspace page", exc_info=True)
            self._stale = False
            self._registry_error = _LOAD_FAILED.bind()
            self._render_rows()
            return
        self._stale = False
        self._registry_error = snapshot.error.display_message if snapshot.error is not None else None
        self._workspaces = snapshot.workspaces
        self._loaded_snapshot = snapshot
        self._ordered = self._order_for(snapshot)
        self._saved = saved
        newer = self._current_saved if change != self._current_change else None
        self._current_saved = current or newer
        # A save made during the read is newer than what the read saw, and adds the chat if it now lists.
        newer_wins = newer is not None and (current is None or newer.updated_at >= current.updated_at)
        if current is not None and not newer_wins:
            self._current_listed = listed
        if newer_wins:
            self._update_saved_row(newer.title, newer.updated_at, render=False)
        self._render_rows()

    def _regroup_superseded(self, change: int) -> bool:
        """A new handoff or an incremental edit takes precedence over the copied rows."""
        if not composed_live(self) or self._queued is not None:
            return True
        if self._current_change != change or not self._page_readable():
            self._request_refresh("regroup")
            return True
        return False

    async def _regroup_rows(self) -> None:
        """Refresh ownership of the committed rows without listing sessions or probing workspace folders."""
        loaded, registry = self._loaded_snapshot, self._registry
        if registry is None:
            return
        if self._registry_error is not None and (loaded is None or loaded.error is None):
            # The last read or regroup failed, so the rows shown are stale or missing: only a full read fixes them.
            self._request_refresh("read")
            return
        if loaded is None:
            # The first read is still running; it commits with the latest order.
            return
        rows = {key: list(items) for key, items in self._saved.items()}
        change = self._current_change
        try:
            snapshot = await run_abandonable("icode-workspace-registry", registry.snapshot)
            if self._regroup_superseded(change):
                return
            if _same_groups(snapshot, loaded):
                # The committed rows are already grouped this way: only the order may change.
                self._stale = False
                self._loaded_snapshot = snapshot
                self._ordered = self._order_for(snapshot)
                self._render_rows()
                return
            if loaded.error is not None and snapshot.error is None:
                # An unreadable registry grouped no chats: only a full read has rows to group now.
                self._request_refresh("read")
                return
            groups = await asyncio.to_thread(_regroup, rows, snapshot)
            if self._regroup_superseded(change):
                return
        except Exception:
            if self._regroup_superseded(change):
                return
            logger.warning("Could not regroup the workspace page", exc_info=True)
            self._stale = False
            self._registry_error = _LOAD_FAILED.bind()
            self._render_rows()
            return
        self._stale = False
        self._saved = groups
        self._loaded_snapshot = snapshot
        self._workspaces = snapshot.workspaces
        self._registry_error = snapshot.error.display_message if snapshot.error is not None else None
        self._ordered = self._order_for(snapshot)
        self._render_rows()

    def _page_rows(self, key: str) -> list[WorkspaceSessionRow]:
        """Build the page and one lookahead; the current chat follows its runtime workspace."""
        limit = self._limit_for(key)
        saved = (entry for entry in self._saved.get(key, ()) if entry.session_id != self._current_id)
        entries = list(islice(saved, limit + 1))
        current = self._current_row() if self._current_id and key == _group_key(self._workspace) else None
        if current is not None and len(entries) > limit and current.updated_at > entries[limit - 1].updated_at:
            # The current chat takes one of the visible slots, leaving the last entry beyond the lookahead.
            entries.pop()
        rows = [
            WorkspaceSessionRow(entry.session_id, entry.title or fallback_title(entry.session_id), entry.updated_at)
            for entry in entries
        ]
        if current is not None:
            order = (current.updated_at, current.session_id)
            index = next((i for i, row in enumerate(rows) if (row.updated_at, row.session_id) < order), len(rows))
            rows.insert(index, current)
        return rows

    def _render_message(self, message: MessageRef) -> str:
        return (
            format_message(message)
            if self._locale_controller is None
            else render_str(self._locale_controller.localizer, message)
        )

    def _groups(self) -> list[_Group]:
        """The visible workspace page, followed by Unspecified when it has chats."""
        groups = [
            _Group(workspace, [] if workspace.id in self._collapsed else self._page_rows(workspace.id))
            for workspace in self._listed_workspaces()[: self._workspace_limit]
        ]
        if _UNSPECIFIED_GROUP in self._collapsed:
            # The heading remains while folded; skip the current chat's saved placement.
            if (self._current_id and self._workspace is None) or any(
                entry.session_id != self._current_id for entry in self._saved.get(_UNSPECIFIED_GROUP, ())
            ):
                groups.append(_Group(None, []))
        elif unspecified := self._page_rows(_UNSPECIFIED_GROUP):
            groups.append(_Group(None, unspecified))
        return groups

    def _limit_for(self, key: str) -> int:
        """How many rows a level shows: no key is the workspace list, a key is that group."""
        return self._workspace_limit if not key else self._session_limits.get(key, _PAGE)

    def _render_rows(self, *, reuse_labels: bool = False) -> None:
        """Keep an unchanged tree; otherwise update it and restore the highlight.

        *reuse_labels* keeps each unchanged row's text, so paging the workspace list builds only the rows it adds.
        """
        if not self._page_readable():
            self._stale = True
            return
        if self._clock is not None:
            self._clock.resume()
        tree = self.query_one(_WorkspaceTree)
        empty = self.query_one(".workspace-empty", SidebarEmptyStateLabel)
        spot = self._cursor_spot(tree)
        width = tree.scrollable_content_region.width
        labels_by_row = (
            self._drawn_row_labels(tree.root.children) if reuse_labels and width == self._rows_width else None
        )
        groups = [] if self._workspaces is None else self._groups()
        workspaces = [group for group in groups if group.workspace is not None]
        self._group_rows = {_group_key(group.workspace): group.rows for group in groups}
        self._row_nodes = {}
        self._group_nodes = {}
        self._page_nodes = {}
        self._row_slots = {}
        self._link_spans = {}
        tree.display = bool(groups)
        tree.disabled = not groups or self._too_narrow(width)
        empty.display = not groups
        hint = (
            labels.LOADING
            if self._workspaces is None and (self._registry is not None or self._startup_pending)
            else labels.NO_WORKSPACES
        )
        empty.update(
            self._error_label(width=empty.content_region.width)
            if self._registry_error is not None
            else Text(self._render_message(hint.bind())),
            layout=False,
        )
        if not groups:
            self._ages_until = {}
            if tree.root.children:
                tree.clear()
            if self._registry_error is not None:
                self.call_after_refresh(self._fit_rows)
            return
        self._rows_width = width
        self._theme_styles = self._resolve_theme_styles()
        planned: list[tuple[Text, str]] = []
        for group in workspaces:
            self._add_group(planned, group.workspace)
            if _group_key(group.workspace) not in self._collapsed:
                self._add_rows(planned, _group_key(group.workspace), group.rows, labels_by_row)
        more = len(self._listed_workspaces()) > self._workspace_limit
        if more or self._workspace_limit > _PAGE:
            # The workspace list's own page row: after the last workspace, before Unspecified.
            self._add_spacer(planned)
            self._add_page(planned, "", more=more, less=self._workspace_limit > _PAGE)
        for group in groups[len(workspaces) :]:
            self._add_group(planned, None)
            if _UNSPECIFIED_GROUP not in self._collapsed:
                self._add_rows(planned, _UNSPECIFIED_GROUP, group.rows, labels_by_row)
        if self._registry_error is not None:
            planned.append((self._error_label(), ""))
        # Compare what the page actually displays, including localized relative
        # times. Reads may return identical records after "just now" became "1m ago".
        if not self._apply_rows(tree, list(tree.root.children), planned, 0):
            return
        tree.root.expand()
        self._restore_cursor(tree, spot)
        # The tree may only now be shown and measured.
        self.call_after_refresh(self._fit_rows)

    @staticmethod
    def _too_narrow(width: int) -> bool:
        """No session row can show a title cell, so nothing on the page takes a click or key."""
        return 0 < width <= 2 * len(_SESSION_INDENT)

    def _drawn_row_labels(self, nodes: Iterable[TreeNode[str]]) -> dict[str, tuple[WorkspaceSessionRow, Text]]:
        """Each drawn session row's record and text, for rows a redraw leaves unchanged."""
        return {
            str(node.data): (row, node.label)
            for node in nodes
            if (row := self._row_nodes.get(str(node.data))) is not None and isinstance(node.label, Text)
        }

    def _apply_rows(
        self, tree: Tree[str], old: list[TreeNode[str]], planned: list[tuple[Text, str]], start: int
    ) -> bool:
        """Redraw each existing node in place as the planned row at its position; add or remove only the difference.

        *old* are the nodes from index *start* on that *planned* redraws. Every row is a leaf, so a node can show
        any row: reordering keeps the tree's structure, and only a change in the number of rows lays it out again.
        True when any node now shows another row.
        """
        self._ages_until = {key: until for key, until in self._ages_until.items() if key in self._row_nodes}
        moved = len(old) != len(planned)
        for node, (label, data) in zip(old, planned, strict=False):
            if node.data != data:
                node.data = data
                moved = True
            elif node.label is label or (
                isinstance(node.label, Text) and node.label == label and node.label.style == label.style
            ):
                continue
            node.set_label(label)
        if not moved:
            return False
        self._clear_tooltip(tree)
        for node in old[len(planned) :]:
            node.remove()
        for index, (label, data) in enumerate(planned[len(old) :], start + len(old)):
            tree.root.add_leaf(label, data, before=index)
        return True

    def _render_current_group(self) -> None:
        """Update the current group's session nodes while preserving the other groups."""
        self._render_group(_group_key(self._workspace))

    def _render_group(self, key: str, *, reuse_labels: bool = False) -> bool:
        """Redraw one drawn, expanded group's rows and page links; False when it is not drawn.

        *reuse_labels* keeps each unchanged row's text, so paging builds only the rows it adds.
        """
        if not self.is_mounted or not composed_live(self):
            return True
        if not self._page_readable():
            self._stale = True
            return True
        heading = _group_data(key)
        if heading not in self._group_nodes or key in self._collapsed:
            return False
        tree = self.query_one(_WorkspaceTree)
        spot = self._cursor_spot(tree)
        children = tree.root.children
        start = next((index for index, node in enumerate(children) if node.data == heading), None)
        if start is None:
            return False
        old = []
        for index in range(start + 1, len(children)):
            node = children[index]
            data = node.data
            if data is None or (
                data != _empty_data(key)
                and self._page_nodes.get(data, (None, False, False))[0] != key
                and self._row_slots.get(data, (None, 0))[0] != key
            ):
                break
            old.append(node)
        labels_by_row = self._drawn_row_labels(old) if reuse_labels else None
        for node in old:
            data = str(node.data)
            self._row_nodes.pop(data, None)
            self._row_slots.pop(data, None)
            self._page_nodes.pop(data, None)
            self._link_spans.pop(data, None)
        rows = self._group_rows[key] = self._page_rows(key)
        planned: list[tuple[Text, str]] = []
        self._add_rows(planned, key, rows, labels_by_row)
        if self._apply_rows(tree, old, planned, start + 1):
            self._restore_cursor(tree, spot)
        return True

    @staticmethod
    def _add_spacer(planned: list[tuple[Text, str]]) -> None:
        if planned:
            planned.append((Text(), _SPACER))

    def _add_group(self, planned: list[tuple[Text, str]], workspace: Workspace | None) -> None:
        self._add_spacer(planned)
        data = _group_data(_group_key(workspace))
        self._group_nodes[data] = workspace
        planned.append((self._group_label(data), data))

    def _add_rows(
        self,
        planned: list[tuple[Text, str]],
        key: str,
        rows: list[WorkspaceSessionRow],
        labels_by_row: dict[str, tuple[WorkspaceSessionRow, Text]] | None = None,
    ) -> None:
        if not rows:
            planned.append((self._empty_row_label(), _empty_data(key)))
            return
        limit = self._limit_for(key)
        shown = rows[:limit]
        current = next((row for row in rows[limit:] if row.current), None)
        if current is not None:
            # Older than the rows shown, the current chat takes the last slot, so it stays in sight and a
            # save that moves it up keeps the number of rows: the tree never needs a new layout for it.
            shown = [*rows[: limit - 1], current]
        shown_ids = {row.session_id for row in shown}
        for index, row in enumerate(rows):
            if row.session_id not in shown_ids:
                continue
            drawn = labels_by_row.get(row.session_id) if labels_by_row else None
            label = drawn[1] if drawn is not None and drawn[0] == row else self._row_label(row)
            planned.append((label, row.session_id))
            self._row_nodes[row.session_id] = row
            self._row_slots[row.session_id] = (key, index)
        more = len(rows) > len(shown)
        if more or limit > _PAGE:
            self._add_page(planned, key, more=more, less=limit > _PAGE)

    def _add_page(self, planned: list[tuple[Text, str]], key: str, *, more: bool, less: bool) -> None:
        data = f"{_PAGE_ROW}:{key}"
        self._page_nodes[data] = (key, more, less)
        if more and less and self._page_links_split(key):
            self._page_nodes[data] = (key, True, False)
            planned.append((self._page_label(data), data))
            data = f"{data}:less"
            self._page_nodes[data] = (key, False, True)
            planned.append((self._page_label(data), data))
        else:
            planned.append((self._page_label(data), data))

    def _page_links_split(self, key: str) -> bool:
        """Whether both pagination links need separate rows at the measured width."""
        width = cell_len(_SESSION_INDENT if key else _GROUP_INDENT) + _LINK_GAP
        width += cell_len(self._render_message(labels.SHOW_MORE.bind()))
        width += cell_len(self._render_message(labels.SHOW_LESS.bind()))
        return bool(self._rows_width and width > self._rows_width - 1)

    def _error_label(self, *, width: int | None = None) -> Text:
        """Leave the reason whole and fit its path in the remaining row cells."""
        error = self._registry_error
        if error is None:
            return Text()
        # _TocTree blanks the last cell of a tree row.
        room = max(0, self._rows_width - 1) if width is None else width
        message = self._render_message(error)
        if room and self._registry is not None:
            path = sanitize_legacy_scalar(str(self._registry.path))
            if path in message:
                room_for_path = max(1, room - (cell_len(message) - cell_len(path)))
                message = message.replace(path, middle_cut_path(path, room_for_path), 1)
        return Text(message, style="dim")

    def _page_label(self, data: str) -> Text:
        """Keep each link whole and record its cell offsets after the row's indent."""
        key, more, less = self._page_nodes[data]
        label = Text(_SESSION_INDENT if key else _GROUP_INDENT)
        spans: list[tuple[str, tuple[int, int]]] = []
        for action, text, shown in (
            ("more", self._render_message(labels.SHOW_MORE.bind()), more),
            ("less", self._render_message(labels.SHOW_LESS.bind()), less),
        ):
            if shown:
                start = cell_len(label.plain) + (_LINK_GAP if spans else 0)
                end = start + cell_len(text)
                # A link the row's edge or the clear last column would cut is left out, with its click.
                if self._rows_width and end > self._rows_width - 1:
                    continue
                if spans:
                    label.append(" " * _LINK_GAP)
                label.append(text, style="underline")
                spans.append((action, (start, end)))
        self._link_spans[data] = tuple(spans)
        return label

    def _current_style(self) -> Style:
        """The theme's own yellow, resolved through CSS so no colour is written here."""
        return Style(color=self.get_component_rich_style("workspace--current", default=Style(color="yellow")).color)

    def _highlight_style(self) -> Style:
        """Bold in the theme's yellow: the current chat's own row and its workspace's name."""
        return Style.combine([Style(bold=True), self._current_style()])

    def _group_style(self) -> Style:
        """A group's own row: bold on the theme's band, which the keyboard cursor still covers."""
        return Style(bold=True, bgcolor=self.get_component_rich_style("workspace--group").bgcolor)

    def _resolve_theme_styles(self) -> tuple[Style, Style]:
        return self._current_style(), self._group_style()

    def _theme_changed(self, _theme: object) -> None:
        if self._theme_styles is not None and self._resolve_theme_styles() != self._theme_styles:
            self._render_rows()

    def _group_label(self, data: str) -> Text:
        """The name gives way to its arrow and the right-aligned actions.

        Each symbol owns a blank on either side. The last blank matches the
        opening indent and occupies the tree's clear final column.
        """
        workspace = self._group_nodes[data]
        if workspace is None:
            name = self._render_message(labels.UNSPECIFIED.bind())
            current = bool(self._current_id) and self._workspace is None
        else:
            name = workspace.name
            current = self._workspace is not None and workspace.id == self._workspace.id
        # Only the name takes the current group's colour; the arrow and symbols look the same on every row.
        title = Text(sanitize_legacy_scalar(name), style=self._highlight_style() if current else "")
        arrow = " ▸" if _group_key(workspace) in self._collapsed else " ▾"
        actions = (("new_session", " + "), ("edit", " ✎ ")) if workspace is not None else (("new_session", " + "),)
        room = self._rows_width
        label = Text(_GROUP_INDENT, style=self._group_style())
        if room:
            # The name comes first: a symbol is shown only while the name still shows enough to say whose it is.
            fixed = label.cell_len + cell_len(arrow) + 1
            readable = min(title.cell_len, _MIN_TITLE_CELLS)
            while actions and room - fixed - sum(cell_len(symbol) for _, symbol in actions) < readable:
                actions = actions[:-1]
            title_width = room - fixed - sum(cell_len(symbol) for _, symbol in actions)
            if title_width > 0:
                title.truncate(title_width, overflow="ellipsis")
            else:
                title = Text()
        suffix_width = sum(cell_len(symbol) for _, symbol in actions)
        label.append_text(title)
        label.append(arrow)
        label.append(" " * max(1, room - label.cell_len - suffix_width))
        spans: list[tuple[str, tuple[int, int]]] = []
        for action, symbol in actions:
            start = label.cell_len
            label.append(symbol)
            spans.append((action, (start, label.cell_len)))
        self._link_spans[data] = tuple(spans)
        if room:
            # Never past the drawn width: a cell beyond it would show in the scrollbar's gutter.
            label.truncate(room)
        return label

    def _row_label(self, row: WorkspaceSessionRow) -> Text:
        """The title gives way so the time stays whole, like the picker's folder count.

        One cell farther in than the group name; only the current chat uses the highlight colour.
        Too narrow to keep a readable title beside it, the time gives way instead.
        """
        self._ages_until[row.session_id] = time_ago_until(row.updated_at)
        style = self._highlight_style() if row.current else Style()
        title = Text(sanitize_legacy_scalar(row.title))
        age = "  " + time_ago(row.updated_at, render_message=self._render_message)
        room = self._rows_width
        if room:
            # The opening indent, and the blanks that end the row through the tree's clear last column.
            title_room = room - 2 * len(_SESSION_INDENT)
            if title_room - cell_len(age) < _MIN_TITLE_CELLS:
                age = ""
            title_room -= cell_len(age)
            if title_room > 0:
                title.truncate(title_room, overflow="ellipsis", pad=True)
            else:
                title = Text()
        label = Text(_SESSION_INDENT, style=style)
        label.append_text(title)
        label.append(age, style="dim")
        # Fill through the tree's clear last column so the cursor covers the full band width.
        label.append(_SESSION_INDENT)
        if room:
            # Never past the drawn width: a cell beyond it would show in the scrollbar's gutter.
            label.truncate(room)
        return label

    def _empty_row_label(self) -> Text:
        """Starts where a session's title does."""
        return Text(_SESSION_INDENT + self._render_message(labels.NO_SESSIONS.bind()), style="dim")

    def _cursor_spot(self, tree: Tree[str]) -> _CursorSpot | None:
        """What the highlight is on now, found by data rather than by line."""
        node = tree.cursor_node
        if node is None or node.data is None:
            return None
        data = str(node.data)
        top = tree.scroll_offset.y
        visible = top <= tree.cursor_line < top + tree.scrollable_content_region.height
        return _CursorSpot(data, self._row_slots.get(data), tree.cursor_line, visible)

    def _restore_cursor(self, tree: _WorkspaceTree, spot: _CursorSpot | None) -> None:
        """Put the highlight back on its own node, or where that node used to be.

        A row the limit now hides takes its level's last shown row, one that
        moved to another group or went takes the nearest shown row of the group
        it was in, and a group with no row left takes its name. An unfocused
        tree keeps its scroll; a focused tree keeps the restored row visible
        if it was visible, and otherwise keeps the scroll the user moved to.
        """
        if spot is None:
            return
        lines = {
            str(node.data): line for line, node in enumerate(tree.root.children) if node.data not in (None, _SPACER, "")
        }
        slot, key = spot.slot, "" if spot.slot is None else spot.slot[0]
        held, rows = self._row_slots.get(spot.data), self._group_rows.get(key, [])
        shown = [row for row in rows if row.session_id in lines and self._row_slots[row.session_id][0] == key]
        line: int | None = None
        if slot is None:
            line = lines.get(spot.data)
            if line is None and spot.data.startswith(f"{_PAGE_ROW}:"):
                page_key = spot.data.removeprefix(f"{_PAGE_ROW}:").removesuffix(":less")
                line = lines.get(f"{_PAGE_ROW}:{page_key}")
                if page_key in self._collapsed:
                    line = lines.get(_group_data(page_key))
        elif spot.data in lines and held is not None and held[0] == key:
            # The same row, still in its own group: follow it to its new line.
            line = lines[spot.data]
        elif not shown:
            # The group has no row left to show: its name is the nearest.
            line = lines.get(_group_data(key))
        else:
            place = {row.session_id: index for index, row in enumerate(rows)}
            if spot.data in place:
                # Still in this group, only hidden by the limit now: its last shown row.
                line = lines.get(shown[-1].session_id)
            else:
                nearest = min(shown, key=lambda row: abs(place[row.session_id] - slot[1]))
                line = lines.get(nearest.session_id)
        if line is None and spot.line >= 0:
            # Nothing of that group is left to aim at: the line it used to occupy.
            line = min(max(spot.line, 0), tree.last_line)
        if line is not None:
            tree.cursor_line = line
            if spot.visible:
                tree.call_after_refresh(tree._scroll_focused_cursor)

    def on_resize(self, _event: events.Resize) -> None:
        self.call_after_refresh(self._fit_rows)

    def _fit_rows(self) -> None:
        """Refit every row to the tree's own width; it may only now be measured."""
        if not self.is_mounted or not composed_live(self) or not self._page_readable():
            return
        tree = self.query_one(Tree)
        empty = self.query_one(".workspace-empty", SidebarEmptyStateLabel)
        if empty.display and self._registry_error is not None:
            empty.update(self._error_label(width=empty.content_region.width), layout=False)
        width = tree.scrollable_content_region.width
        if not tree.root.children or width == self._rows_width:
            return
        self._rows_width = width
        tree.disabled = self._too_narrow(width)
        # A hint names what was under the pointer before the rows moved.
        self._clear_tooltip(tree)
        for data, (key, more, less) in self._page_nodes.items():
            split = f"{data}:less" in self._page_nodes
            if more and (less or split) and split != self._page_links_split(key):
                self._render_rows()
                return
        self._link_spans = {}
        for node in tree.root.children:
            data = "" if node.data is None else str(node.data)
            row = self._row_nodes.get(data)
            if row is not None:
                node.set_label(self._row_label(row))
            elif data in self._group_nodes:
                node.set_label(self._group_label(data))
            elif data in self._page_nodes:
                node.set_label(self._page_label(data))
            elif self._registry_error is not None and node is tree.root.children[-1]:
                node.set_label(self._error_label())

    def _page_click(self, tree: Tree[str], event: events.Click) -> bool:
        """Take group and pagination clicks before the tree moves its cursor.

        A page row is no row of its own: only its links act, and a link leaves
        the highlight where it was. The rest of a page row with a link moves the
        highlight but never selects it: Enter on the row is what runs it; a page
        row too narrow to draw a link takes no click. The rest of a group's row
        folds or unfolds the group and leaves the highlight alone.
        """
        if event.button != 1:
            return False
        offset = event.get_content_offset(tree)
        if offset is None:
            return False
        line = offset.y + tree.scroll_offset.y
        node = tree.get_node_at_line(line)
        if node is None or node.data is None:
            return False
        data = str(node.data)
        if self.inert(data):
            return True
        if data not in self._page_nodes and data not in self._group_nodes:
            return False
        action = next((hit for hit, (start, end) in self._link_spans.get(data, ()) if start <= offset.x < end), None)
        if data in self._group_nodes:
            if action is None:
                self._toggle_group(data)
            else:
                workspace = self._group_nodes[data]
                if action == "new_session":
                    self.post_message(self.NewSessionRequested(None if workspace is None else workspace.id))
                elif action == "edit" and workspace is not None:
                    self.post_message(self.EditRequested(workspace))
        elif action is None:
            tree.cursor_line = line
        else:
            self._page_action(data, action)
        return True

    def on_mouse_move(self, event: events.MouseMove) -> None:
        tree = self.query_one(Tree)
        # A hidden tree has no region yet; asking for one would arrange the screen.
        offset = event.get_content_offset(tree) if tree.display else None
        # The hovered link or error row, and the text that explains it.
        hint: tuple[str, str, str] | None = None
        if offset is not None:
            node = tree.get_node_at_line(offset.y + tree.scroll_offset.y)
            data = "" if node is None or node.data is None else str(node.data)
            for action, (start, end) in self._link_spans.get(data, ()):
                if start <= offset.x < end:
                    message = {"new_session": labels.NEW_SESSION, "edit": labels.EDIT}.get(action)
                    if message is not None:
                        hint = (data, action, self._render_message(message.bind()))
                    break
            if (
                hint is None
                and self._registry_error is not None
                and node is not None
                and node is tree.root.children[-1]
                and not data
            ):
                full = self._render_message(self._registry_error)
                if str(node.label) != full or cell_len(full) > max(0, self._rows_width - 1):
                    hint = (data, "error", full)
        if hint == self._tooltip_hint:
            return
        moving_between_hints = self._tooltip_hint is not None and hint is not None
        self._tooltip_hint = hint
        tree.tooltip = None if hint is None else Text(hint[2])
        if moving_between_hints:
            # Textual hides a visible tooltip on motion within a widget without rearming its timer.
            self.screen._handle_tooltip_timer(tree)

    def on_leave(self, _event: events.Leave) -> None:
        self._clear_tooltip(self.query_one(Tree))

    def inert(self, data: object) -> bool:
        """A row with nothing to act on."""
        # The spacer, an empty group's "No sessions" and the load-error row ("").
        if not data or data == _SPACER or str(data).startswith(f"{_EMPTY}:"):
            return True
        return data in self._page_nodes and not self._link_spans.get(str(data))

    def _clear_tooltip(self, tree: Tree[str]) -> None:
        self._tooltip_hint = None
        tree.tooltip = None

    @on(Tree.NodeSelected)
    def _selected(self, event: Tree.NodeSelected[str]) -> None:
        event.stop()
        if not self.is_mounted:
            return
        tree = self.query_one(Tree)
        if not any(child is event.node for child in tree.root.children):
            # A rebuild replaced this node: its selection is no row of this page.
            return
        data = (event.row if isinstance(event, _RowSelected) else event.node.data) or ""
        if self.inert(data):
            return
        if data in self._page_nodes:
            self._page_action(data, "auto")
        elif data in self._group_nodes:
            self._toggle_group(data)
        elif self.can_restore_session(data):
            self.post_message(self.SessionSelected(data))

    def _toggle_group(self, data: str) -> None:
        key = _group_key(self._group_nodes[data])
        if key in self._collapsed:
            self._collapsed.remove(key)
        else:
            self._collapsed.add(key)
        self._render_rows()

    def _page_action(self, data: str, action: str) -> None:
        """Show more for *more*, Show less for *less*, and Enter on the row for *auto*."""
        key, _, _ = self._page_nodes[data]
        if action == "auto":
            # Enter runs the first link the row draws.
            action = next((hit for hit, _ in self._link_spans.get(data, ())), "")
        if action == "more":
            self._set_limit(key, self._limit_for(key) + _PAGE)
        elif action == "less":
            self._set_limit(key, _PAGE)

    def _set_limit(self, key: str, limit: int) -> None:
        if key:
            self._session_limits[key] = limit
            # Paging one group leaves every other row as it is.
            if self._render_group(key, reuse_labels=True):
                return
            self._render_rows()
        else:
            self._workspace_limit = limit
            self._render_rows(reuse_labels=True)

    def refresh_localization(self) -> None:
        if self.is_mounted:
            self._render_rows()
