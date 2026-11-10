# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SessionsScreen — modal for browsing and restoring persisted sessions.

Opens as a centered modal overlay (Ctrl+S).  Sessions list newest first, in
pages of at most 100, filtered by where each was last used (TUI, CLI, ACP;
TUI only by default).  The store snapshots the listing once, so paging never
reshuffles, and each page loads fresh.  The bottom search box searches every
session of the checked surfaces, not just the page shown: every column but
Last Active and Size, plus each session's indexed prompt text, matched
against the snapshot (rows found only through prompt text render in italics,
list after the column matches, and carry the matched context in their
tooltip); results page like the listing, and clearing the search returns to
the page being browsed.  Within the page shown: click a column header to sort
(click again to flip direction), and forked sessions nest under their parent
as a tree.  Resume to load, Delete to remove.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.containers import HorizontalGroup, VerticalGroup
from textual.message import Message
from textual.widgets import Button, DataTable, Input

from chrys.app.tui.binding_display import CLOSE_BINDING, DELETE_BINDING, localized_binding
from chrys.app.tui.i18n import LocaleController, render_str, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.screens.sessions.presenter import (
    COLUMNS,
    SURFACE_LABELS,
    WORKFLOW_COLUMNS,
    SessionRow,
    WorkflowSessionPick,
    build_session_rows,
    build_workflow_rows,
    column_by_key,
    fork_anchors,
    format_tokens,
    last_interaction_display,
    profile_display,
    rank_search_matches,
    surface_label,
    title_display,
    workflow_status_labels,
)
from chrys.app.tui.util.rich_style import rich_style_from_textual_color
from chrys.app.tui.widgets import (
    Checkbox,
    ChrysLoadingIndicator,
    DialogButtonRow,
    DialogButtonSpec,
    HatchedEmptyState,
    PageNavigator,
)
from chrys.app.tui.widgets.input import EnhancedInput
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n import DisplayPath, MessageRef, msg
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.util.session_ids import session_short_id
from chrys.service.state.session_listing import SESSION_PAGE_SIZE, SessionListing, page_slice

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from textual import events
    from textual.app import ComposeResult
    from textual.timer import Timer

    from chrys.service.state.session_listing import SessionPage
    from chrys.service.state.store import SessionMeta, StateStore

logger = logging.getLogger(__name__)

_SEARCH_DEBOUNCE_SECONDS = 0.15
"""Delay between the last search keystroke and the table re-render."""

_TREE_LEVEL_WIDTH = 2
"""Cells one fork level adds to the Session ID column ('└ ' / '│ ')."""

_EVERY_SURFACE = frozenset(SessionSurface)
"""A search result listing is filtered already: page it whatever surfaces its sessions have."""

DEFAULT_SURFACES = frozenset({SessionSurface.TUI})
"""Surfaces listed until the user picks others; sessions from before surfaces were recorded count as TUI."""
WORKFLOW_SURFACES = frozenset({SessionSurface.TUI, SessionSurface.CLI})
"""Where workflow runs start: the TUI and ``icode workflow run``; ACP clients start none."""

_DELETE_SESSION_TITLE = msg("tui.sessions.title.delete", fallback="Delete Session")
_SESSION_OPEN_ELSEWHERE = msg(
    "tui.sessions.delete.open_elsewhere",
    fallback="Session is open in another {app_name} instance.",
)
_SESSION_NOT_DELETED = msg(
    "tui.sessions.delete.failed",
    fallback="The session could not be fully deleted. Close any program that may have its files open, then try again.",
)
_SESSIONS_TITLE = msg("tui.sessions.title", fallback="Chat Sessions")
_WORKFLOW_SESSIONS_TITLE = msg("tui.sessions.title.workflow", fallback="Workflow Sessions")
_TOOLTIP_WORKFLOW = msg("tui.sessions.tooltip.workflow", fallback="Workflow: {workflow}")
_TOOLTIP_STATUS = msg("tui.sessions.tooltip.status", fallback="Status: {status}")
_TOOLTIP_RUN = msg("tui.sessions.tooltip.run", fallback="Run: {run_id}")
_NO_SAVED_SESSIONS = msg("tui.sessions.empty", fallback="No saved sessions.")
_NO_FILTERED_SESSIONS = msg("tui.sessions.empty.filtered", fallback="No sessions match the selected filters.")
_NO_SEARCH_MATCHES = msg("tui.sessions.empty.search", fallback="No sessions match your search.")
_SEARCH_PLACEHOLDER = msg(
    "tui.sessions.search_placeholder",
    fallback="Search all sessions… (IDs, titles, directories & your prompts)",
)
_RESUME = msg("tui.sessions.button.resume", fallback="Resume")
_DELETE = msg("tui.sessions.button.delete", fallback="Delete")
_CLOSE = msg("tui.sessions.button.close", fallback="Close")
_LOADING_SESSIONS = msg("tui.sessions.loading", fallback="Loading sessions")
_SESSION_COUNT = msg("tui.sessions.count", fallback="{count_text} sessions")
_WORKFLOW_SESSION_COUNT = msg("tui.sessions.workflow_count", fallback="{count_text} workflow sessions")
_TOOLTIP_TITLE = msg("tui.sessions.tooltip.title", fallback="Title: {title}")
_TOOLTIP_AGENT = msg("tui.sessions.tooltip.agent", fallback="Agent: {agent}")
_TOOLTIP_PROMPT_MATCH = msg("tui.sessions.tooltip.prompt_match", fallback="Prompt match: {prompt}")
_TOOLTIP_DIRECTORY = msg("tui.sessions.tooltip.directory", fallback="Directory: {path}")
_TOOLTIP_TURNS = msg("tui.sessions.tooltip.turns", fallback="Turns: {turns}")
_TOOLTIP_TOTAL_TOKENS = msg("tui.sessions.tooltip.total_tokens", fallback="Total tokens: {tokens}")
_TOOLTIP_LAST_INTERACTION = msg(
    "tui.sessions.tooltip.last_interaction",
    fallback="Last interaction: {interaction}",
)
_TOOLTIP_FORKED_FROM = msg("tui.sessions.tooltip.forked_from", fallback="Forked from: {session_id}")
_TOOLTIP_SURFACE = msg("tui.sessions.tooltip.surface", fallback="Last used in: {surface}")
_DELETE_SESSION_MESSAGE = msg(
    "tui.sessions.delete.confirm_message",
    fallback='Delete session\n"{session_id}"?\n\nThis cannot be undone.',
    multiline=True,
)


class _SessionTable(DataTable):
    """DataTable subclass that shows per-row tooltips on hover.

    Textual's tooltip system is per-widget: when the mouse moves within the
    same widget and the tooltip is already visible, the screen hides it
    (screen.py ``_handle_mouse_move`` line ~1657).  To support per-ROW
    tooltips we defer the tooltip update via ``call_later`` so it runs
    *after* the screen's hide logic, then immediately trigger the tooltip
    display.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._row_tooltips: dict[int, Text] = {}
        self._last_hover_row: int = -1

    def set_row_tooltips(self, tooltips: dict[int, Text]) -> None:
        """Set tooltip text for each row index."""
        self._row_tooltips = tooltips

    def _on_mouse_move(self, event: events.MouseMove) -> None:
        # Textual dispatches DataTable._on_mouse_move after this method; do
        # not call super() here or hover bookkeeping runs twice.
        meta = event.style.meta
        if meta and "row" in meta:
            row_idx = meta["row"]
            if row_idx != self._last_hover_row:
                self._last_hover_row = row_idx
                tip = self._row_tooltips.get(row_idx)
                # Defer so it runs after screen._handle_mouse_move hides tooltip
                self.call_later(self._apply_row_tooltip, tip)
        else:
            self._last_hover_row = -1
            self.tooltip = None

    def _apply_row_tooltip(self, tip: Text | None) -> None:
        """Apply the tooltip and force immediate display."""
        self.tooltip = tip
        if tip is not None:
            import contextlib

            with contextlib.suppress(Exception):
                self.screen._handle_tooltip_timer(self)


class _SearchInput(EnhancedInput):
    """Filter box: Escape clears the query, then hands focus back."""

    class Escaped(Message):
        """Posted when Escape is pressed on an already-empty search box."""

    async def _on_key(self, event: events.Key) -> None:
        # No super() call: Textual dispatches the EnhancedInput/Input base
        # handlers through the MRO on its own.
        if event.key != "escape":
            return
        event.stop()
        event.prevent_default()
        if self.value:
            self.value = ""
        else:
            self.post_message(self.Escaped())


def _next_cursor_row_after_delete(deleted_row: int, remaining_count: int) -> int | None:
    """Return the row to highlight after deleting one row from a table."""
    if remaining_count <= 0:
        return None
    return min(deleted_row, remaining_count - 1)


def _next_session_id_after_delete(session_ids: list[str], deleted_row: int) -> str | None:
    """Return the visible neighbor to highlight after deleting one row."""
    if not 0 <= deleted_row < len(session_ids):
        return None
    next_row = deleted_row + 1
    if next_row < len(session_ids):
        return session_ids[next_row]
    previous_row = deleted_row - 1
    if previous_row >= 0:
        return session_ids[previous_row]
    return None


class SessionsScreen(BaseDialog[str | WorkflowSessionPick | None]):
    """Modal for browsing persisted sessions.

    Returns a chat session id or a specific workflow run to open; None closes the modal.
    """

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "dismiss", CLOSE_BINDING),
        localized_binding("delete", "delete_session", DELETE_BINDING),
    ]

    CSS_PATH = "screen.tcss"

    #: Sentinel prefix: tells caller to delete this session and start fresh.
    DELETE_AND_NEW_PREFIX = "__delete_and_new__:"

    def __init__(
        self,
        state_store: StateStore,
        current_session_id: str = "",
        *,
        locale_controller: LocaleController | None = None,
        workflow_mode: bool = False,
        surfaces: Collection[SessionSurface] | None = None,
        on_surfaces_changed: Callable[[frozenset[SessionSurface]], None] | None = None,
    ) -> None:
        self._state_store = state_store
        self._current_session_id = current_session_id
        self._locale_controller = locale_controller
        self._workflow_mode = workflow_mode
        self._kind: Literal["chat", "workflow"] = "workflow" if workflow_mode else "chat"
        self._columns = WORKFLOW_COLUMNS if workflow_mode else COLUMNS
        self._offered_surfaces = tuple(
            surface for surface in SURFACE_LABELS if not workflow_mode or surface in WORKFLOW_SURFACES
        )
        # A surface the browser offers no checkbox for could never be unchecked.
        chosen = DEFAULT_SURFACES if surfaces is None else surfaces
        self._surfaces = frozenset(surface for surface in chosen if surface in self._offered_surfaces)
        self._on_surfaces_changed = on_surfaces_changed
        # Taken once, on the first load: pages keep their order however sessions change meanwhile.
        self._listing: SessionListing | None = None
        self._page = 1
        self._page_count = 1
        self._filtered_total = 0
        """Sessions of the selected surfaces, across every page."""
        self._search_query = ""
        """The search the latest load asked for ('' browses the listing); set when the typing settles."""
        self._shown_query = ""
        """The search the rows shown were loaded for: what they are filtered, highlighted and counted by."""
        self._shown_surfaces = self._surfaces
        """The surfaces the rows shown were loaded for."""
        self._match_count = 0
        """Sessions matching the shown search, across every result page."""
        self._browse_page = 1
        """The listing page to show again once the search is cleared."""
        self._hydration: asyncio.Task[SessionPage] | None = None
        """The page load in flight, which every later load waits for (see ``_load_page``)."""
        self._page_loaded = False
        # The page's sessions: what sorting and searching work on.
        self._rendered_sessions: list[SessionMeta] = []
        self._session_ids: list[str] = []
        self._rows: list[SessionRow] = []
        self._sort_column: str = column_by_key("").key
        self._sort_reverse: bool = column_by_key("").default_reverse
        self._loading = True
        self._loading_request: tuple[int, str] | None = None
        """The page and search the load in flight asked for (the page may be clamped); None once a load finishes."""
        self._focus_results_of: str | None = None
        """The search whose results take focus when they show: Enter asked for them while they loaded."""
        # Becomes True once the cursor has jumped to the currently loaded
        # session (or the initial scan finished without finding it).
        self._cursor_initialized = False
        self._search_timer: Timer | None = None
        super().__init__()

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        with VerticalGroup(id="container") as container:
            title = _WORKFLOW_SESSIONS_TITLE if self._workflow_mode else _SESSIONS_TITLE
            container.border_title = Text(render_str(localizer, title.bind()))
            with VerticalGroup(id="sessions-loading-state"):
                yield ChrysLoadingIndicator(id="sessions-loading")
            yield HatchedEmptyState(render_str(localizer, _NO_SAVED_SESSIONS.bind()), id="empty-note")
            table = _SessionTable(id="sessions", cursor_type="row")
            table.display = False
            yield table
            with HorizontalGroup(id="filters") as filters:
                filters.display = False
                with HorizontalGroup(id="surface-filters"):
                    for surface in self._offered_surfaces:
                        yield Checkbox(
                            render_str(localizer, SURFACE_LABELS[surface].bind()),
                            value=surface in self._surfaces,
                            id=f"surface-{surface.value}",
                        )
                yield PageNavigator(self._locale_controller, id="session-pages")
            with HorizontalGroup(id="footer") as footer:
                footer.display = False
                # The themed border lives on the wrapper so the input's
                # background stays inside it (a border directly on the Input
                # would share its background; compact=True would strip the
                # border entirely with `border: none !important`).
                with HorizontalGroup(id="search-box"):
                    yield _SearchInput(
                        placeholder=render_str(localizer, _SEARCH_PLACEHOLDER.bind()),
                        id="search",
                        compact=True,
                    )
                yield DialogButtonRow(
                    DialogButtonSpec(
                        Text(render_str(localizer, _RESUME.bind())), id="resume", variant="primary", disabled=True
                    ),
                    DialogButtonSpec(
                        Text(render_str(localizer, _DELETE.bind())), id="delete", variant="error", disabled=True
                    ),
                    DialogButtonSpec(Text(render_str(localizer, _CLOSE.bind())), id="cancel", variant="warning"),
                    id="buttons",
                )

    def on_mount(self) -> None:
        self._request_page(1)

    def on_unmount(self) -> None:
        if self._hydration is not None:
            self._hydration.cancel()

    # ------------------------------------------------------------------
    # Loading & rendering
    # ------------------------------------------------------------------

    def _request_page(
        self, page: int, *, preferred_cursor_row: int | None = None, preferred_session_id: str | None = None
    ) -> None:
        """Load and show *page* of the current search (or of the listing), replacing any load in flight."""
        query = self._search_query
        self._loading_request = (page, query)
        # Enter claims focus for the results of its own request (claimed after this), not of any later one.
        self._focus_results_of = None
        self._load_sessions(
            page=page,
            query=query,
            preferred_cursor_row=preferred_cursor_row,
            preferred_session_id=preferred_session_id,
        )

    @work(exclusive=True, group="load-sessions")
    async def _load_sessions(
        self,
        page: int = 1,
        query: str = "",
        preferred_cursor_row: int | None = None,
        preferred_session_id: str | None = None,
    ) -> None:
        """Load one page of the listing, or of the sessions matching *query*, then render it.

        The listing is snapshotted by the first load; later loads (paging,
        filtering, searching, reloading after a delete) work on that
        snapshot. A search ranks every snapshot entry of the selected
        surfaces by the meta the snapshot read (off the event loop, ranking
        again on every load, so no earlier result can go stale), then loads
        only its result page fresh, with the unmatched parents its forks nest
        under (uncounted, shown dimmed), which the presenter filters by the
        same rule. So a session another process changed since the snapshot may be
        counted but not shown, as one deleted since already is. Loads are
        exclusive: a newer request cancels the one in flight, so the page
        shown is always the last one asked for.
        """
        self._loading = True
        self._update_chrome()
        if self._listing is None:
            self._listing = await self._state_store.open_session_listing(kind=self._kind)
        listing = self._listing
        surfaces = self._surfaces
        candidates = listing.filtered(surfaces)
        if query:
            ranked = await asyncio.to_thread(
                rank_search_matches, candidates, query, status_labels=workflow_status_labels(self._render_message)
            )
            selected, page, page_count, match_count = page_slice(
                SessionListing(listing.kind, ranked), _EVERY_SURFACE, page
            )
            metas: tuple[SessionMeta, ...] = ()
            if selected:
                # The unmatched parents of its forks come along, uncounted, to anchor them in the tree.
                anchors = await asyncio.to_thread(fork_anchors, selected, candidates, ranked)
                shown = SessionListing(listing.kind, (*selected, *anchors))
                metas = (await self._load_page(shown, _EVERY_SURFACE, 1, page_size=len(shown.entries))).metas
            filtered_total = len(candidates)
        else:
            loaded = await self._load_page(listing, surfaces, page)
            metas, page, page_count, filtered_total = loaded.metas, loaded.page, loaded.page_count, loaded.total
            match_count = filtered_total
        if not self._composed_live:
            # Closing prunes the widgets before Textual cancels this worker.
            return
        self._loading = False
        self._loading_request = None
        self._page, self._page_count, self._filtered_total = page, page_count, filtered_total
        self._shown_query, self._shown_surfaces, self._match_count = query, surfaces, match_count
        self._page_loaded = True
        self._render_table(
            preferred_cursor_row=preferred_cursor_row,
            preferred_session_id=preferred_session_id,
            source_sessions=list(metas),
        )
        self._cursor_initialized = True
        if self._focus_results_of is not None:
            focus_results, self._focus_results_of = self._focus_results_of == query, None
            search = self.query_one("#search", _SearchInput)
            # Not once the user went on typing, or moved focus elsewhere.
            if focus_results and self.focused is search and search.value.strip() == query:
                self._focus_table()

    async def _load_page(
        self,
        listing: SessionListing,
        surfaces: frozenset[SessionSurface],
        page: int,
        *,
        page_size: int = SESSION_PAGE_SIZE,
    ) -> SessionPage:
        """Load one page's fresh metas and folder sizes, one load at a time.

        The store reads in a thread that cancelling a load cannot stop, so a
        load first waits for the one before it to finish, and is shielded
        itself: a newer request (each settled search asks for a page) never
        starts its reads beside an abandoned one's. Exclusive loads make only
        the newest waiting request go on.
        """
        previous = self._hydration
        if previous is not None and not previous.done():
            await asyncio.wait([previous])
        task = asyncio.create_task(
            self._state_store.load_session_page(listing, surfaces=surfaces, page=page, page_size=page_size)
        )
        # An abandoned load's error has no one awaiting it: retrieve it so asyncio doesn't log it as lost.
        task.add_done_callback(lambda done: done.cancelled() or done.exception())
        self._hydration = task
        return await asyncio.shield(task)

    def _render_table(
        self,
        preferred_cursor_row: int | None = None,
        preferred_session_id: str | None = None,
        *,
        prefer_live_selection: bool = True,
        source_sessions: list[SessionMeta] | None = None,
    ) -> None:
        """Rebuild the table from the in-memory metas (sort + filter + tree)."""
        table = self.query_one("#sessions", _SessionTable)
        query = self._shown_query
        previous_selected = self._get_selected_session_id()

        render_source = source_sessions
        if render_source is None:
            render_source = self._rendered_sessions

        build_rows = build_workflow_rows if self._workflow_mode else build_session_rows
        rows = build_rows(
            render_source,
            sort_column=self._sort_column,
            sort_reverse=self._sort_reverse,
            query=query,
            render_message=self._render_message,
        )
        self._rows = rows
        self._session_ids = [row.key for row in rows]

        table.clear(columns=True)
        max_depth = max((row.depth for row in rows), default=0)
        for column in self._columns:
            label = self._render_message(column.label.bind())
            if column.key == self._sort_column:
                label += " ↓" if self._sort_reverse else " ↑"
            width = column.width
            if width is not None:
                if column.key == "id":
                    width += _TREE_LEVEL_WIDTH * max_depth
                width = max(width, cell_len(label))
            table.add_column(Text(label), width=width, key=column.key)

        highlight = query.strip()
        match_style = self._match_style()
        row_tooltips: dict[int, Text] = {}
        for row_index, row in enumerate(rows):
            table.add_row(*self._row_texts(row, highlight, match_style), key=row.key)
            row_tooltips[row_index] = self._row_tooltip(row)
        table.set_row_tooltips(row_tooltips)

        self._restore_cursor(
            previous_selected,
            preferred_cursor_row,
            preferred_session_id,
            prefer_live_selection=prefer_live_selection,
        )
        self._rendered_sessions = list(render_source)
        has_rows = bool(rows)
        self.query_one("#resume", Button).disabled = not has_rows
        self.query_one("#delete", Button).disabled = not has_rows
        self._update_chrome()

    def _match_style(self) -> Style:
        """Search-match highlight style, themed to the warning color."""
        warning = self.app.theme_variables.get("warning", "yellow")
        return rich_style_from_textual_color(warning, reverse=True)

    def _row_texts(self, row: SessionRow, highlight: str, match_style: Style) -> list[Text]:
        """Cell renderables for one row, with tree guides and match marks."""
        texts: list[Text] = []
        for column in self._columns:
            cell = sanitize_legacy_scalar(surrogate_safe_text(row.cells[column.key]))
            value = Text(cell, justify="right" if column.numeric else "left")
            if highlight:
                if column.searched:
                    value.highlight_words([highlight], match_style, case_sensitive=False)
                if not row.matched:
                    # Ancestor kept only to anchor a matching fork subtree.
                    value.stylize("dim")
                elif row.prompt_only:
                    # Match lives in prompt text — nothing in the row
                    # highlights, so italics flag it (tooltip has the
                    # matched context).
                    value.stylize("italic")
            if column.key == "id" and row.tree_prefix:
                value = Text(row.tree_prefix, style="dim").append_text(value)
            texts.append(value)
        return texts

    def _row_tooltip(self, row: SessionRow) -> Text:
        """Hover tooltip, one labelled line per fact.

        The agent profile lives only here since its column was dropped
        (rarely distinguishing between rows).
        """
        meta = row.meta
        if row.workflow is not None:
            parts = [
                self._render_message(_TOOLTIP_WORKFLOW.bind(workflow=row.workflow.title)),
                self._render_message(_TOOLTIP_STATUS.bind(status=row.cells["status"])),
                self._render_message(_TOOLTIP_RUN.bind(run_id=row.workflow.run_id)),
            ]
        else:
            if meta.kind != "chat":
                raise ValueError("Unsupported session kind for chat tooltip.")
            parts = [
                self._render_message(_TOOLTIP_TITLE.bind(title=title_display(meta))),
                self._render_message(_TOOLTIP_AGENT.bind(agent=profile_display(meta))),
            ]
        if row.prompt_snippet:
            parts.insert(1, self._render_message(_TOOLTIP_PROMPT_MATCH.bind(prompt=row.prompt_snippet)))
        if meta.primary_cwd:
            parts.append(self._render_message(_TOOLTIP_DIRECTORY.bind(path=DisplayPath(meta.primary_cwd))))
        if meta.kind == "chat":
            parts.append(self._render_message(_TOOLTIP_TURNS.bind(turns=meta.turn_count)))
            parts.append(self._render_message(_TOOLTIP_TOTAL_TOKENS.bind(tokens=format_tokens(meta.total_tokens))))
        updated = row.workflow.updated_at if row.workflow is not None else meta.updated_at
        parts.append(
            self._render_message(_TOOLTIP_LAST_INTERACTION.bind(interaction=last_interaction_display(updated)))
        )
        surface = self._render_message(surface_label(meta.last_surface))
        parts.append(self._render_message(_TOOLTIP_SURFACE.bind(surface=surface)))
        if meta.kind == "chat" and meta.parent_session_id:
            parts.append(
                self._render_message(_TOOLTIP_FORKED_FROM.bind(session_id=session_short_id(meta.parent_session_id)))
            )
        return Text("\n".join(parts))

    def _restore_cursor(
        self,
        previous_selected: str | None,
        preferred_cursor_row: int | None,
        preferred_session_id: str | None,
        *,
        prefer_live_selection: bool,
    ) -> None:
        """Keep the highlighted session stable across re-renders.

        The jump to the currently loaded session happens at most once, on
        the initial open (``_cursor_initialized``) — a later reload (e.g.
        after a delete) must not yank the cursor back to it.  Delete refreshes
        prefer a concrete neighbor session id, with row number only as a last
        fallback if that neighbor disappeared during the reload.  Final
        reload renders prefer live user selection; optimistic delete renders
        force the precomputed neighbor for immediate visual stability.
        """
        table = self.query_one("#sessions", _SessionTable)
        if table.row_count == 0:
            return
        target_id = previous_selected if previous_selected in self._session_ids else None
        if preferred_session_id is not None:
            if (not prefer_live_selection or target_id is None) and preferred_session_id in self._session_ids:
                target_id = preferred_session_id
            elif not prefer_live_selection:
                target_id = None
            if target_id is not None:
                table.move_cursor(row=self._session_ids.index(target_id))
            elif not self._loading and preferred_cursor_row is not None:
                table.move_cursor(row=min(preferred_cursor_row, table.row_count - 1))
            else:
                table.move_cursor(row=0)
            return

        if target_id is None and not self._cursor_initialized:
            target_id = next((row.key for row in self._rows if row.meta.session_id == self._current_session_id), None)
            self._cursor_initialized = True
        if target_id is not None:
            table.move_cursor(row=self._session_ids.index(target_id))
        elif preferred_cursor_row is not None:
            table.move_cursor(row=min(preferred_cursor_row, table.row_count - 1))
        else:
            table.move_cursor(row=0)

    def _update_chrome(self) -> None:
        """Which of loading, empty note, table and controls show, plus the count in the border.

        With no sessions at all the dialog shrinks to its empty note. Once
        any exist, the filters, pager and footer stay so every filter can be
        undone, and a note replaces the table when the selected surfaces or
        the search leave nothing to show.
        """
        show_loading = not self._page_loaded
        has_sessions = self._listing is not None and bool(self._listing.entries)
        visible_rows = len(self._rows)
        search = self.query_one("#search", _SearchInput)
        searching = bool(self._shown_query)
        container = self.query_one("#container")
        if has_sessions or show_loading:
            container.remove_class("-empty")
        else:
            container.add_class("-empty")
        show_table = not show_loading and visible_rows > 0
        table = self.query_one("#sessions", _SessionTable)
        self.query_one("#sessions-loading-state").display = show_loading
        table.display = show_table
        note = self.query_one("#empty-note", HatchedEmptyState)
        note.display = not show_loading and not show_table
        if not has_sessions:
            reason = _NO_SAVED_SESSIONS
        elif searching and self._filtered_total:
            reason = _NO_SEARCH_MATCHES
        else:
            reason = _NO_FILTERED_SESSIONS
        if (label := self._render_message(reason.bind())) != note.label:
            note.update_label(label)
        show_controls = not show_loading and has_sessions
        self.query_one("#filters").display = show_controls
        self.query_one("#footer").display = show_controls
        navigator = self.query_one(PageNavigator)
        # Focus must not stay on a pager button this disables (a page turned while the search hid
        # the table) or on the table once it hides (a page where the search matches nothing).
        if (show_table or show_controls) and (
            navigator.disables_focused(self._page, self._page_count) or (self.focused is table and not show_table)
        ):
            self.set_focus(table if show_table else search, scroll_visible=False)
        navigator.show(self._page, self._page_count, view=(self._shown_query, self._shown_surfaces))

        if show_loading:
            container.border_subtitle = Text(self._render_message(_LOADING_SESSIONS.bind()))
        elif has_sessions:
            count = f"{self._match_count}/{self._filtered_total}" if searching else f"{self._filtered_total}"
            label = _WORKFLOW_SESSION_COUNT if self._workflow_mode else _SESSION_COUNT
            container.border_subtitle = Text(self._render_message(label.bind(count_text=count)))
        else:
            container.border_subtitle = ""

    # ------------------------------------------------------------------
    # Surface filters & paging
    # ------------------------------------------------------------------

    @on(Checkbox.Changed, "#surface-filters Checkbox")
    def _on_surface_toggled(self, event: Checkbox.Changed) -> None:
        """A surface was checked or unchecked: list the first page of the new selection."""
        event.stop()
        surfaces = frozenset(
            surface for surface in self._offered_surfaces if self.query_one(f"#surface-{surface.value}", Checkbox).value
        )
        if surfaces == self._surfaces:
            return
        self._surfaces = surfaces
        if self._on_surfaces_changed is not None:
            self._on_surfaces_changed(surfaces)
        self._browse_page = 1
        self._request_page(1)

    @on(PageNavigator.Changed, "#session-pages")
    def _on_page_changed(self, event: PageNavigator.Changed) -> None:
        event.stop()
        # A press leaves focus on the pager. One that arrives after the user moved on (into the search box,
        # say) must not undo what they did since: where they put focus, or an Enter's claim on its results.
        if event.navigator.has_focus_within:
            self._focus_results_of = None
            table = self.query_one("#sessions", _SessionTable)
            if table.display:
                self.set_focus(table, scroll_visible=False)
        if event.view != (self._search_query, self._surfaces):
            # Pressed on the pages of a search or selection since replaced, shown or on its way: none is the new one's.
            return
        # The pager still shows the old page while a slow load runs: asking again would only restart it.
        if (event.page, self._search_query) != self._loading_request:
            self._request_page(event.page)

    # ------------------------------------------------------------------
    # Sorting & searching
    # ------------------------------------------------------------------

    @on(DataTable.HeaderSelected, "#sessions")
    def _on_header_selected(self, event: DataTable.HeaderSelected) -> None:
        """Click a column header — sort by it; click again to flip direction."""
        key = event.column_key.value
        if key is None:
            return
        column = column_by_key(str(key), workflow=self._workflow_mode)
        if self._sort_column == column.key:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column.key
            self._sort_reverse = column.default_reverse
        self._render_table()

    @on(Input.Changed, "#search")
    def _on_search_changed(self, event: Input.Changed) -> None:
        """Debounce searches while the user is still typing."""
        if self._search_timer is not None:
            self._search_timer.stop()
        self._search_timer = self.set_timer(_SEARCH_DEBOUNCE_SECONDS, self._apply_search)

    def _apply_search(self) -> None:
        """Search for what the box holds now: the first result page, or back to the page browsed before."""
        if self._search_timer is not None:
            self._search_timer.stop()
            self._search_timer = None
        query = self.query_one("#search", _SearchInput).value.strip()
        if query == self._search_query:
            return
        if not self._search_query:
            self._browse_page = self._page_asked_for()
        self._search_query = query
        self._request_page(1 if query else self._browse_page)

    def _page_asked_for(self) -> int:
        """The page last asked for: the one on its way while a load runs, else the one shown."""
        return self._page if self._loading_request is None else self._loading_request[0]

    @on(Input.Submitted, "#search")
    def _on_search_submitted(self, event: Input.Submitted) -> None:
        """Enter in the search box — search now, and jump to the results."""
        self._apply_search()
        if self._loading_request is None:
            self._focus_table()
        else:
            # The results, not the rows they replace: those may give way to an empty note.
            self._focus_results_of = self._search_query

    @on(_SearchInput.Escaped)
    def _on_search_escaped(self, event: _SearchInput.Escaped) -> None:
        """Escape on an empty search box — return focus to the table."""
        self._focus_table()

    def _focus_table(self) -> None:
        # A hidden table can still take focus, which would leave the keyboard nowhere visible.
        table = self.query_one("#sessions", _SessionTable)
        if table.display:
            table.focus()

    # ------------------------------------------------------------------
    # Selection & dismissal
    # ------------------------------------------------------------------

    def _get_selected_session_id(self) -> str | None:
        """Return the session ID stored as the highlighted row key."""
        table = self.query_one("#sessions", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        if row_key is not None and row_key.value is not None:
            return str(row_key.value)
        return None

    def _resume_selected(self) -> None:
        key = self._get_selected_session_id()
        row = next((row for row in self._rows if row.key == key), None)
        if row is not None:
            self.dismiss(WorkflowSessionPick(row.meta.session_id) if row.workflow is not None else row.meta.session_id)

    @on(DataTable.RowHighlighted)
    def _on_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Enable buttons when a row is highlighted."""
        self.query_one("#resume", Button).disabled = False
        self.query_one("#delete", Button).disabled = False

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        """Double-click or Enter on a row — resume that session."""
        self._resume_selected()

    @on(Button.Pressed, "#resume")
    def _on_resume(self, event: Button.Pressed) -> None:
        self._resume_selected()

    @on(Button.Pressed, "#cancel")
    def _on_cancel(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#delete")
    def _on_delete_button(self, event: Button.Pressed) -> None:
        self.action_delete_session()

    def action_delete_session(self) -> None:
        """Delete key or Delete button — confirm removing the highlighted session."""
        session_id = self._get_selected_session_id()
        if session_id:
            table = self.query_one("#sessions", DataTable)
            selected_row = table.cursor_coordinate.row
            session_ids = list(self._session_ids)
            preferred_session_id = _next_session_id_after_delete(session_ids, selected_row)
            preferred_cursor_row = _next_cursor_row_after_delete(selected_row, len(session_ids) - 1)

            from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

            dialog = ConfirmDialog(
                title=self._render_message(_DELETE_SESSION_TITLE.bind()),
                message=self._render_message(_DELETE_SESSION_MESSAGE.bind(session_id=session_short_id(session_id))),
                confirm_label=self._render_message(_DELETE.bind()),
                confirm_variant="error",
                locale_controller=self._locale_controller,
            )
            self.app.push_screen(  # ty: ignore[no-matching-overload]  # Textual's callback overload rejects the work-decorated handler result.
                dialog,
                callback=lambda confirmed: (
                    self._do_delete(
                        session_id,
                        preferred_cursor_row=preferred_cursor_row,
                        preferred_session_id=preferred_session_id,
                    )
                    if confirmed
                    else None
                ),
            )

    def _render_message(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)

    @work(thread=False)
    async def _do_delete(
        self,
        session_id: str,
        *,
        preferred_cursor_row: int | None,
        preferred_session_id: str | None,
    ) -> None:
        """Delete a session from the store and refresh the table."""
        if session_id == self._current_session_id:
            # Don't delete here — let MainScreen handle it via the engine
            # so shutdown() won't re-save the file.
            self.dismiss(f"{self.DELETE_AND_NEW_PREFIX}{session_id}")
        else:
            try:
                await self._state_store.delete_session(session_id)
            except TimeoutError:
                self.notify(
                    render_str(
                        widget_localizer(self),
                        _SESSION_OPEN_ELSEWHERE.bind(app_name=APP_DISPLAY_NAME),
                    ),
                    title=render_str(
                        widget_localizer(self),
                        _DELETE_SESSION_TITLE.bind(),
                    ),
                    severity="warning",
                    timeout=4,
                    markup=False,
                )
                return
            except OSError:
                logger.exception("Failed to delete session %s", session_id)
                self.notify(
                    render_str(widget_localizer(self), _SESSION_NOT_DELETED.bind()),
                    title=render_str(widget_localizer(self), _DELETE_SESSION_TITLE.bind()),
                    severity="error",
                    markup=False,
                )
                return
            if not self._composed_live:
                # Closing prunes the widgets before Textual cancels this worker.
                return
            if self._listing is not None:
                self._listing = self._listing.without(session_id)
            optimistic_sessions = [meta for meta in self._rendered_sessions if meta.session_id != session_id]
            if len(optimistic_sessions) < len(self._rendered_sessions):
                self._filtered_total = max(0, self._filtered_total - 1)
                # Not a parent the result page shows only to anchor a matching fork.
                if any(row.matched and row.meta.session_id == session_id for row in self._rows):
                    self._match_count = max(0, self._match_count - 1)
            self._render_table(
                preferred_cursor_row=preferred_cursor_row,
                preferred_session_id=preferred_session_id,
                prefer_live_selection=False,
                source_sessions=optimistic_sessions,
            )
            # The next page's first session moves up into this one, or into the page on its way.
            self._request_page(
                self._page_asked_for(),
                preferred_cursor_row=preferred_cursor_row,
                preferred_session_id=preferred_session_id,
            )
