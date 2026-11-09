# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure presentation logic for the sessions browser.

Sorting, fork-tree assembly, and search filtering for
:class:`~chrys.app.tui.screens.sessions.screen.SessionsScreen`, kept free of
Textual imports so the ordering/filtering rules stay unit-testable without a
running app.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from chrys.app.tui.util.formatting import elide_middle, format_byte_size
from chrys.foundation.i18n import MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.util.session_ids import session_short_id

if TYPE_CHECKING:
    from collections.abc import Callable

    from chrys.service.state.store import ChatSessionMeta, SessionMeta
    from chrys.service.workflows.history import WorkflowRunMeta


_SESSION_ID_PREVIEW_LEN = 8
"""Characters to show when a session has no title."""

_DIRECTORY_MAX_WIDTH = 24
"""Cell-width cap for the Directory column; longer names elide the middle."""

_JUST_NOW = msg("tui.sessions.time.just_now", fallback="just now")
_MINUTES_AGO = msg("tui.sessions.time.minutes_ago", fallback="{minute_count}m ago")
_HOURS_AGO = msg("tui.sessions.time.hours_ago", fallback="{hour_count}h ago")
_DAYS_AGO = msg(
    "tui.sessions.time.days_ago",
    fallback="1 day ago",
    plural_fallback="{count} days ago",
)
_MONTHS_AGO = msg(
    "tui.sessions.time.months_ago",
    fallback="1 month ago",
    plural_fallback="{count} months ago",
)
_YEARS_AGO = msg(
    "tui.sessions.time.years_ago",
    fallback="1 year ago",
    plural_fallback="{count} years ago",
)
_COLUMN_SESSION_ID = msg("tui.sessions.column.session_id", fallback="Session ID")
_COLUMN_TITLE = msg("tui.sessions.column.title", fallback="Title")
_COLUMN_DIRECTORY = msg("tui.sessions.column.directory", fallback="Directory")
_COLUMN_LAST_ACTIVE = msg("tui.sessions.column.last_active", fallback="Last Active")
_COLUMN_TURNS = msg("tui.sessions.column.turns", fallback="Turns")
_COLUMN_SIZE = msg("tui.sessions.column.size", fallback="Size")
_COLUMN_WORKFLOW = msg("tui.sessions.column.workflow", fallback="Workflow")
_COLUMN_RUNS = msg("tui.sessions.column.runs", fallback="Runs")
_COLUMN_STATUS = msg("tui.sessions.column.status", fallback="Status")
_STATUS_RUNNING = msg("tui.sessions.status.running", fallback="Running")
_STATUS_INTERRUPTED = msg("tui.sessions.status.interrupted", fallback="Interrupted")
_STATUS_CANCELLED = msg("tui.sessions.status.cancelled", fallback="Cancelled")
_STATUS_FAILED = msg("tui.sessions.status.failed", fallback="Error")
_STATUS_COMPLETED = msg("tui.sessions.status.completed", fallback="Completed")

_SURFACE_TUI = msg("tui.sessions.surface.tui", fallback="TUI")
_SURFACE_CLI = msg("tui.sessions.surface.cli", fallback="CLI")
_SURFACE_ACP = msg("tui.sessions.surface.acp", fallback="ACP")
SURFACE_LABELS = {
    SessionSurface.TUI: _SURFACE_TUI,
    SessionSurface.CLI: _SURFACE_CLI,
    SessionSurface.ACP: _SURFACE_ACP,
}
_SURFACE_UNKNOWN = msg("tui.sessions.surface.unknown", fallback="Unknown")


def surface_label(surface: SessionSurface | None) -> MessageRef:
    """Where the session was last used; sessions saved before that was recorded read as unknown."""
    return (SURFACE_LABELS[surface] if surface is not None else _SURFACE_UNKNOWN).bind()


WORKFLOW_STATUSES = {
    "running": _STATUS_RUNNING,
    "interrupted": _STATUS_INTERRUPTED,
    "cancelled": _STATUS_CANCELLED,
    "failed": _STATUS_FAILED,
    "completed": _STATUS_COMPLETED,
}


def format_size(size_bytes: int) -> str:
    """Format bytes as a human-readable string (e.g. '1.2 MB')."""
    return format_byte_size(size_bytes)


_TOKEN_UNITS = (
    # The unit changes where one decimal would otherwise round up into the
    # next unit's name: 999,950 tokens reads "1m", never "1000k".
    (999_950_000, 1_000_000_000, "b"),
    (999_950, 1_000_000, "m"),
    (1_000, 1_000, "k"),
)


def format_tokens(count: int) -> str:
    """Compact token count (500, 3k, 1.5m, 5b)."""
    for smallest, scale, suffix in _TOKEN_UNITS:
        if count >= smallest:
            scaled = f"{count / scale:.1f}".removesuffix(".0")
            return f"{scaled}{suffix}"
    return str(count)


def time_ago(
    dt: datetime,
    *,
    now: datetime | None = None,
    render_message: Callable[[MessageRef], str] = format_message,
) -> str:
    """Format a datetime as a relative time string."""
    delta = (now or datetime.now(UTC)) - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return render_message(_JUST_NOW.bind())
    minutes = seconds // 60
    if minutes < 60:
        return render_message(_MINUTES_AGO.bind(minute_count=minutes))
    hours = minutes // 60
    if hours < 24:
        return render_message(_HOURS_AGO.bind(hour_count=hours))
    days = hours // 24
    if days == 1:
        return render_message(_DAYS_AGO.bind(count=1))
    if days < 30:
        return render_message(_DAYS_AGO.bind(count=days))
    months = days // 30
    if months == 1:
        return render_message(_MONTHS_AGO.bind(count=1))
    if months < 12:
        return render_message(_MONTHS_AGO.bind(count=months))
    # Months are 30 days throughout this function, so twelve of them make a
    # year: 360 days reads "1 year ago", never "0 years ago".
    years = months // 12
    if years == 1:
        return render_message(_YEARS_AGO.bind(count=1))
    return render_message(_YEARS_AGO.bind(count=years))


def last_interaction_display(dt: datetime) -> str:
    """Absolute local-timezone timestamp (tooltip counterpart of time_ago)."""
    return dt.astimezone().strftime("%Y/%m/%d %H:%M")


def profile_display(meta: ChatSessionMeta) -> str:
    """User-facing profile name for a session row."""
    if meta.agent_profile_history:
        return meta.agent_profile_history[-1]
    return meta.agent_display_name or meta.agent_profile or "?"


def title_display(meta: SessionMeta) -> str:
    """User-facing title, falling back to a session-id preview."""
    return meta.display_title or f"{meta.session_id[:_SESSION_ID_PREVIEW_LEN]}.."


def directory_display(meta: SessionMeta) -> str:
    """Trailing directory name of the session's primary cwd."""
    full_path = meta.primary_cwd or ""
    return full_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1] if full_path else ""


@dataclass(frozen=True)
class SessionColumn:
    """One sortable column of the sessions table."""

    key: str
    label: MessageDef
    width: int | None
    """Fixed cell width; ``None`` lets the column flex."""
    sort_value: Callable[[SessionRow], Any]
    """Comparable sort key for a row (values are homogeneous per column)."""
    default_reverse: bool = False
    """First-click sort direction (True = descending first)."""
    numeric: bool = False
    """Right-align cell contents (counts / sizes)."""


COLUMNS: tuple[SessionColumn, ...] = (
    SessionColumn("id", _COLUMN_SESSION_ID, 12, lambda row: session_short_id(row.meta.session_id)),
    SessionColumn("title", _COLUMN_TITLE, 30, lambda row: row.cells["title"].lower()),
    SessionColumn("directory", _COLUMN_DIRECTORY, None, lambda row: directory_display(row.meta).lower()),
    SessionColumn("last_active", _COLUMN_LAST_ACTIVE, 11, lambda row: row.updated_at, default_reverse=True),
    SessionColumn("turns", _COLUMN_TURNS, 5, lambda row: int(row.cells["turns"]), default_reverse=True, numeric=True),
    SessionColumn("size", _COLUMN_SIZE, 9, lambda row: row.meta.size_bytes, default_reverse=True, numeric=True),
)

WORKFLOW_COLUMNS: tuple[SessionColumn, ...] = (
    COLUMNS[0],
    SessionColumn("workflow", _COLUMN_WORKFLOW, 30, lambda row: row.cells["workflow"].casefold()),
    COLUMNS[2],
    COLUMNS[3],
    SessionColumn("status", _COLUMN_STATUS, 11, lambda row: row.cells["status"].casefold()),
    SessionColumn(
        "runs",
        _COLUMN_RUNS,
        5,
        lambda row: int(row.cells["runs"]),
        default_reverse=True,
        numeric=True,
    ),
    COLUMNS[5],
)

DEFAULT_SORT_COLUMN = "last_active"
DEFAULT_SORT_REVERSE = True

_COLUMNS_BY_KEY = {column.key: column for column in COLUMNS}


def column_by_key(key: str, *, workflow: bool = False) -> SessionColumn:
    """Resolve a column spec, falling back to the default sort column."""
    columns = {column.key: column for column in WORKFLOW_COLUMNS} if workflow else _COLUMNS_BY_KEY
    return columns.get(key, columns[DEFAULT_SORT_COLUMN])


def _common_cells(meta: SessionMeta) -> dict[str, str]:
    return {
        "id": session_short_id(meta.session_id),
        "directory": elide_middle(directory_display(meta), _DIRECTORY_MAX_WIDTH),
        "size": format_size(meta.size_bytes),
    }


def row_cells(
    meta: ChatSessionMeta,
    *,
    now: datetime | None = None,
    render_message: Callable[[MessageRef], str] = format_message,
) -> dict[str, str]:
    """Plain display strings for a Chat row, keyed by column key."""
    return _common_cells(meta) | {
        "title": title_display(meta),
        "last_active": time_ago(meta.updated_at, now=now, render_message=render_message),
        "turns": str(meta.turn_count),
    }


def _searchable_haystack(cells: dict[str, str]) -> str:
    """Lower-cased searchable text: exactly the visible cell strings.

    Deliberately NOT the full session UUID or full cwd path — matching text
    the table doesn't display reads as a false positive (a row appears with
    no visible highlight).
    """
    return "\n".join(cells.values()).lower()


def _prompt_haystack(meta: ChatSessionMeta) -> str:
    """User-typed prompt text searchable beyond the visible cells.

    ``meta.user_prompt_search_text`` carries head+tail-capped excerpts
    from EVERY turn's user messages (extracted during the meta scan);
    metas built without it fall back to ``meta.title``, the first prompt
    capped at save time.  Text that doubles as the displayed title is
    harmless here — such a query already cell-matches, so ``prompt_only``
    stays False and the row ranks as a visible hit.  The final ``""`` is
    load-bearing: a legacy envelope can carry ``title: null``, which the
    state reader passes through despite the ``str`` annotation, and the
    caller lowercases this return value.
    """
    return meta.user_prompt_search_text or meta.title or ""


_PROMPT_SNIPPET_CONTEXT = 24
"""Characters of first-prompt context kept on each side of a match."""


def _prompt_snippet(prompt: str, query_norm: str) -> str:
    """Whitespace-collapsed context around the first match in *prompt*.

    Lowercasing may change string length ("İ" lowers to "i" plus a
    combining dot), so an index found in ``prompt.lower()`` cannot be
    applied to *prompt* itself — after any expanding character it drifts
    right of the real match and slices a wrong or even empty window.
    The match position is ALWAYS taken from the whole-string lowered
    copy — the same string the row-match filter searches, including its
    Greek final-sigma context handling; re-finding in a char-by-char
    lowered copy could hit an earlier position the filter never matched.
    The slow path only builds a lowered-offset → source-offset table,
    which stays aligned because per-char lower widths equal whole-string
    lower widths: the sole expanding default mapping ("İ") is
    unconditional, and the sole conditional one (final sigma) keeps
    width 1 either way.
    """
    if not query_norm:
        return ""
    lowered = prompt.lower()
    index = lowered.find(query_norm)
    if index < 0:
        return ""
    if len(lowered) == len(prompt):
        # Equal lengths guarantee a 1:1 char mapping (every char lowers
        # to at least one char), so the index is a source offset already.
        match_start = index
        match_end = index + len(query_norm)
    else:
        offsets: list[int] = []
        for position, char in enumerate(prompt):
            offsets.extend([position] * len(char.lower()))
        if len(offsets) != len(lowered):
            # A casing rule broke the width alignment (none exists in
            # current Unicode): clamp rather than crash or mis-slice far.
            match_start = min(index, len(prompt) - 1)
            match_end = min(index + len(query_norm), len(prompt))
        else:
            match_start = offsets[index]
            match_end = offsets[index + len(query_norm) - 1] + 1
    start = max(0, match_start - _PROMPT_SNIPPET_CONTEXT)
    end = min(len(prompt), match_end + _PROMPT_SNIPPET_CONTEXT)
    snippet = " ".join(prompt[start:end].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(prompt) else ""
    return f"{prefix}{snippet}{suffix}"


@dataclass(frozen=True)
class SessionRow:
    """One display row of the (tree-flattened, sorted, filtered) table."""

    meta: SessionMeta
    depth: int
    """0 for roots; +1 per fork level."""
    tree_prefix: str
    """Tree guide glyphs prepended to the first column ('' for roots)."""
    cells: dict[str, str]
    """Plain display strings keyed by column key (no tree prefix)."""
    matched: bool
    """Whether this row itself matched the active query (vs. shown as a
    tree ancestor of a matching fork)."""
    prompt_only: bool = False
    """Matched ONLY via user prompt text — nothing visible in the row
    contains the query, so the screen renders an explicit cue."""
    prompt_snippet: str = ""
    """Prompt context around the match ('' unless ``prompt_only``);
    surfaced in the row tooltip as match evidence."""
    workflow: WorkflowRunMeta | None = None

    @property
    def key(self) -> str:
        return self.meta.session_id

    @property
    def updated_at(self) -> datetime:
        return self.workflow.updated_at if self.workflow is not None else self.meta.updated_at


@dataclass(frozen=True, slots=True)
class WorkflowSessionPick:
    session_id: str


def build_workflow_rows(
    sessions: list[SessionMeta],
    *,
    sort_column: str = DEFAULT_SORT_COLUMN,
    sort_reverse: bool = DEFAULT_SORT_REVERSE,
    query: str = "",
    now: datetime | None = None,
    render_message: Callable[[MessageRef], str] = format_message,
) -> list[SessionRow]:
    """One row per workflow session, summarized by its latest run."""
    query_norm = query.strip().lower()
    column = column_by_key(sort_column, workflow=True)
    rows = []
    for meta in sessions:
        if meta.kind != "workflow" or meta.latest_run is None:
            continue
        run = meta.latest_run
        cells = _common_cells(meta)
        cells.update(
            workflow=run.title,
            runs=str(meta.run_count),
            status=render_message(WORKFLOW_STATUSES[run.status].bind()),
            last_active=time_ago(run.updated_at, now=now, render_message=render_message),
        )
        visible_match = not query_norm or query_norm in _searchable_haystack(cells)
        prompt_match = bool(query_norm) and query_norm in run.input_text.lower()
        if visible_match or prompt_match:
            rows.append(
                SessionRow(
                    meta,
                    0,
                    "",
                    cells,
                    True,
                    prompt_only=prompt_match and not visible_match,
                    prompt_snippet=_prompt_snippet(run.input_text, query_norm) if not visible_match else "",
                    workflow=run,
                )
            )

    rows.sort(key=column.sort_value, reverse=sort_reverse)
    rows.sort(key=lambda row: row.prompt_only)
    return rows


def build_session_rows(
    sessions: list[SessionMeta],
    *,
    sort_column: str = DEFAULT_SORT_COLUMN,
    sort_reverse: bool = DEFAULT_SORT_REVERSE,
    query: str = "",
    now: datetime | None = None,
    render_message: Callable[[MessageRef], str] = format_message,
) -> list[SessionRow]:
    """Assemble display rows: fork tree, per-level sorting, query filter.

    Forked sessions (``parent_session_id`` set and the parent present in
    *sessions*) nest under their parent; nesting recurses for forks of
    forks.  Siblings — including the roots — are ordered by *sort_column* /
    *sort_reverse*.  A non-empty *query* keeps rows whose searchable text
    contains it case-insensitively, plus their ancestors so the tree stays
    anchored.  Corrupt parent chains (cycles) are defensively promoted to
    roots rather than dropped.

    Matching is two-tier: visible cell strings first, then the user-typed
    prompt excerpts of every turn (``meta.user_prompt_search_text``,
    head+tail-capped at scan time).  Within every sibling group — roots and fork children
    alike — subtrees containing at least one visible cell match list
    before subtrees found only through prompt text, so rows whose match
    the user can actually see stay on top.
    """
    column = column_by_key(sort_column)
    sort_key = column.sort_value
    query_norm = query.strip().lower()

    by_short: dict[str, ChatSessionMeta] = {}
    for meta in sessions:
        if meta.kind != "chat":
            continue
        by_short.setdefault(session_short_id(meta.session_id), meta)

    children: dict[str, list[ChatSessionMeta]] = {}
    roots: list[ChatSessionMeta] = []
    for meta in by_short.values():
        short = session_short_id(meta.session_id)
        parent_short = session_short_id(meta.parent_session_id) if meta.parent_session_id else ""
        if parent_short and parent_short != short and parent_short in by_short:
            children.setdefault(parent_short, []).append(meta)
        else:
            roots.append(meta)

    # Parent cycles (corrupt data) leave members unreachable from any root;
    # promote them so every session still lists.  The emitted-set below
    # keeps the traversal from looping or duplicating rows.
    reachable: set[str] = set()
    stack = [session_short_id(root.session_id) for root in roots]
    while stack:
        short = stack.pop()
        if short in reachable:
            continue
        reachable.add(short)
        stack.extend(session_short_id(child.session_id) for child in children.get(short, ()))
    roots.extend(meta for short, meta in by_short.items() if short not in reachable)

    rows_by_short = {
        short: SessionRow(meta, 0, "", row_cells(meta, now=now, render_message=render_message), True)
        for short, meta in by_short.items()
    }
    cell_matched = {
        short: not query_norm or query_norm in _searchable_haystack(rows_by_short[short].cells) for short in by_short
    }
    prompt_matched = {
        short: bool(query_norm) and query_norm in _prompt_haystack(meta).lower() for short, meta in by_short.items()
    }
    matched = {short: cell_matched[short] or prompt_matched[short] for short in by_short}

    # Bottom-up subtree visibility: a row stays when it matches or any
    # descendant does (ancestors anchor the tree for matching forks).
    # Iterative post-order — fork chains can be arbitrarily deep, so no
    # recursion here (or in the emit walk below).  The same walk aggregates
    # whether a subtree holds any VISIBLE cell match (`cell_reach`), which
    # decides the root-level cell-vs-prompt ordering below.
    visible: dict[str, bool] = {}
    cell_reach: dict[str, bool] = {}
    for start in by_short:
        if start in visible:
            continue
        on_path: set[str] = set()
        walk: list[tuple[str, bool]] = [(start, False)]
        while walk:
            short, expanded = walk.pop()
            if expanded:
                on_path.discard(short)
                child_shorts = [session_short_id(child.session_id) for child in children.get(short, ())]
                visible[short] = matched[short] or any(visible.get(child, False) for child in child_shorts)
                cell_reach[short] = cell_matched[short] or any(cell_reach.get(child, False) for child in child_shorts)
                continue
            if short in visible or short in on_path:
                continue
            on_path.add(short)
            walk.append((short, True))
            walk.extend(
                (child_short, False)
                for child in children.get(short, ())
                if (child_short := session_short_id(child.session_id)) not in visible and child_short not in on_path
            )

    def ranked(metas: list[ChatSessionMeta]) -> list[ChatSessionMeta]:
        """Sibling ordering: the user's sort, then a stable partition that
        moves subtrees found only through prompt text behind subtrees with
        a visible cell match (no-op without a query — everything
        cell-matches).  Applied at EVERY level, not just the roots, so a
        prompt-only fork cannot overtake its visibly matching sibling."""
        result = sorted(
            metas, key=lambda meta: sort_key(rows_by_short[session_short_id(meta.session_id)]), reverse=sort_reverse
        )
        result.sort(key=lambda meta: not cell_reach[session_short_id(meta.session_id)])
        return result

    def guide_prefix(ancestor_last_flags: tuple[bool, ...]) -> str:
        if not ancestor_last_flags:
            return ""
        runs = ["  " if is_last else "│ " for is_last in ancestor_last_flags[:-1]]
        runs.append("└ " if ancestor_last_flags[-1] else "├ ")
        return "".join(runs)

    rows: list[SessionRow] = []
    emitted: set[str] = set()
    # Pre-order DFS with an explicit stack (children pushed reversed so the
    # ranked order pops first).
    stack: list[tuple[ChatSessionMeta, tuple[bool, ...]]] = [
        (root, ()) for root in reversed(ranked(roots)) if visible[session_short_id(root.session_id)]
    ]
    while stack:
        meta, ancestor_last_flags = stack.pop()
        short = session_short_id(meta.session_id)
        if short in emitted:
            continue
        emitted.add(short)
        rows.append(
            replace(
                rows_by_short[short],
                depth=len(ancestor_last_flags),
                tree_prefix=guide_prefix(ancestor_last_flags),
                matched=matched[short],
                prompt_only=prompt_matched[short] and not cell_matched[short],
                prompt_snippet=(
                    _prompt_snippet(_prompt_haystack(meta), query_norm)
                    if prompt_matched[short] and not cell_matched[short]
                    else ""
                ),
            )
        )
        kept = [
            child
            for child in ranked(children.get(short, []))
            if visible[session_short_id(child.session_id)] and session_short_id(child.session_id) not in emitted
        ]
        last_index = len(kept) - 1
        for index in range(last_index, -1, -1):
            stack.append((kept[index], (*ancestor_last_flags, index == last_index)))
    return rows
