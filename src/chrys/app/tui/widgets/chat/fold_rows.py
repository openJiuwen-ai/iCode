# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The one-line rows a folded thinking message, tool group or process header shows, each read as a sentence."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from operator import add
from typing import TYPE_CHECKING, Final, Literal

from rich.cells import cell_len, get_character_cell_size
from rich.style import Style
from rich.text import Text

from chrys.app.tui.i18n import render_str
from chrys.app.tui.widgets.loading import component_text_style
from chrys.foundation.hosted_tools import HostedToolFamily
from chrys.foundation.i18n import Localizer, MessageDef, msg
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar
from chrys.foundation.tool_kinds import (
    KIND_ASK_USER,
    KIND_DOC_CONVERTER,
    KIND_FILESYSTEM_READ,
    KIND_FILESYSTEM_WRITE,
    KIND_MCP,
    KIND_SEARCH,
    KIND_SHELL,
    KIND_SKILL,
    KIND_SLEEP,
    KIND_SUB_AGENT,
    KIND_TODO,
    KIND_WEB_FETCH,
    KIND_WEB_SEARCH,
)

if TYPE_CHECKING:
    from textual.widget import Widget

THINKING_LABEL = msg("tui.chat.thinking_label", fallback="Thinking")
"""The label of thinking while it streams."""
THOUGHT_LABEL = msg("tui.chat.thought_label", fallback="Thought")
"""The label of thinking once complete."""
TOOLS_LABEL = msg("tui.tool_card.group.label", fallback="Tools")
"""A running tool group's title before it has an activity or a count to show."""
AGENT_LABEL = msg("tui.chat.agent_fallback_label", fallback="Agent")
"""An agent message's label when it has no profile name, and the header of open text between tool calls."""
FOLDED_LABEL = msg("tui.chat.fold_row.folded_label", fallback="{label}: ")
"""A folded row's label and what parts it from the opening of its text after it."""

FOLDED_ARROW = "▸"
OPEN_ARROW = "▾"

_PREVIEW_SOURCE_CHARS = 1024
"""How much of the text a preview reads: more than the widest row shows, a bounded cost for any length."""


def text_preview(text: str) -> str:
    """The opening of *text* as written, on one line: line breaks and runs of spaces become one space.

    Plain text, markdown markers included: the row is a hint at what the text says, not a rendering of it.
    Only the start of the text is read, enough to fill any row; the row cuts it to its width.
    """
    return sanitize_legacy_scalar(" ".join(text[:_PREVIEW_SOURCE_CHARS].split()))


def text_tail(text: str, end: int, cells: int) -> str:
    """*text* up to *end* on one line, as :func:`text_preview` reads its opening: the last of it that fits *cells*.

    Once the start no longer fits, ``…`` stands for what is cut off before it.
    """
    return _tail_fit(sanitize_legacy_scalar(" ".join(text[max(end - _PREVIEW_SOURCE_CHARS, 0) : end].split())), cells)


def _tail_fit(shown: str, cells: int) -> str:
    """The last of *shown* that fits *cells*, as written, ``…`` standing for what is cut off before it."""
    if cell_len(shown) <= cells:
        return shown
    if cells <= 0:
        return ""
    start, width = len(shown), 1
    while start and width + (cell := get_character_cell_size(shown[start - 1])) <= cells:
        start -= 1
        width += cell
    return f"…{shown[start:]}"


ToolOutcome = Literal["", "failed", "interrupted"]
"""How a call ended, as a folded row counts it: "" while it runs or once it succeeded."""

_READS = msg("tui.tool_card.group.summary.reads", fallback="{count} read", plural_fallback="{count} reads")
_SEARCHES = msg("tui.tool_card.group.summary.searches", fallback="{count} search", plural_fallback="{count} searches")
_WEB_SEARCHES = msg(
    "tui.tool_card.group.summary.web_searches", fallback="{count} web search", plural_fallback="{count} web searches"
)
_FETCHES = msg("tui.tool_card.group.summary.fetches", fallback="{count} fetch", plural_fallback="{count} fetches")
_EDITS = msg("tui.tool_card.group.summary.edits", fallback="{count} edit", plural_fallback="{count} edits")
_COMMANDS = msg("tui.tool_card.group.summary.commands", fallback="{count} command", plural_fallback="{count} commands")
_SKILL_CALLS = msg(
    "tui.tool_card.group.summary.skill_calls", fallback="{count} skill call", plural_fallback="{count} skill calls"
)
_MCP_CALLS = msg(
    "tui.tool_card.group.summary.mcp_calls", fallback="{count} MCP call", plural_fallback="{count} MCP calls"
)
_AGENTS = msg("tui.tool_card.group.summary.agents", fallback="{count} agent", plural_fallback="{count} agents")
_QUESTIONS = msg(
    "tui.tool_card.group.summary.questions", fallback="{count} question", plural_fallback="{count} questions"
)
_TODO_UPDATES = msg(
    "tui.tool_card.group.summary.todo_updates", fallback="{count} todo update", plural_fallback="{count} todo updates"
)
_SLEEPS = msg("tui.tool_card.group.summary.sleeps", fallback="{count} sleep", plural_fallback="{count} sleeps")
_OTHER_CALLS = msg(
    "tui.tool_card.group.summary.other_calls", fallback="{count} other call", plural_fallback="{count} other calls"
)

_CATEGORIES: Final[tuple[tuple[str, MessageDef], ...]] = (
    ("read", _READS),
    ("search", _SEARCHES),
    ("web_search", _WEB_SEARCHES),
    ("fetch", _FETCHES),
    ("edit", _EDITS),
    ("command", _COMMANDS),
    ("skill", _SKILL_CALLS),
    ("mcp", _MCP_CALLS),
    ("agent", _AGENTS),
    ("question", _QUESTIONS),
    ("todo", _TODO_UPDATES),
    ("sleep", _SLEEPS),
    ("other", _OTHER_CALLS),
)
"""Every category a folded row counts, in the order it lists them, with the words for its count."""

_CATEGORY_BY_KIND: Final[dict[str, str]] = {
    KIND_FILESYSTEM_READ: "read",
    KIND_DOC_CONVERTER: "read",
    KIND_SEARCH: "search",
    KIND_WEB_SEARCH: "web_search",
    KIND_WEB_FETCH: "fetch",
    KIND_FILESYSTEM_WRITE: "edit",
    KIND_SHELL: "command",
    KIND_SKILL: "skill",
    KIND_MCP: "mcp",
    KIND_SUB_AGENT: "agent",
    KIND_ASK_USER: "question",
    KIND_TODO: "todo",
    KIND_SLEEP: "sleep",
}
_CATEGORY_BY_HOSTED_FAMILY: Final[dict[str, str]] = {
    HostedToolFamily.SEARCH: "web_search",
    HostedToolFamily.FETCH: "fetch",
    HostedToolFamily.MCP: "mcp",
    HostedToolFamily.SHELL: "command",
}

_READ_FILES = msg(
    "tui.tool_card.group.action.reads", fallback="read {count} file", plural_fallback="read {count} files"
)
_SEARCHED_PATTERNS = msg(
    "tui.tool_card.group.action.searches",
    fallback="searched for {count} pattern",
    plural_fallback="searched for {count} patterns",
)
_RAN_WEB_SEARCHES = msg(
    "tui.tool_card.group.action.web_searches",
    fallback="ran {count} web search",
    plural_fallback="ran {count} web searches",
)
_FETCHED_PAGES = msg(
    "tui.tool_card.group.action.fetches", fallback="fetched {count} page", plural_fallback="fetched {count} pages"
)
_EDITED_FILES = msg(
    "tui.tool_card.group.action.edits", fallback="edited {count} file", plural_fallback="edited {count} files"
)
_RAN_COMMANDS = msg(
    "tui.tool_card.group.action.commands", fallback="ran {count} command", plural_fallback="ran {count} commands"
)
_USED_SKILLS = msg(
    "tui.tool_card.group.action.skill_calls", fallback="used {count} skill", plural_fallback="used {count} skills"
)
_MADE_MCP_CALLS = msg(
    "tui.tool_card.group.action.mcp_calls", fallback="made {count} MCP call", plural_fallback="made {count} MCP calls"
)
_RAN_AGENTS = msg(
    "tui.tool_card.group.action.agents", fallback="ran {count} agent", plural_fallback="ran {count} agents"
)
_ASKED_QUESTIONS = msg(
    "tui.tool_card.group.action.questions",
    fallback="asked {count} question",
    plural_fallback="asked {count} questions",
)
_UPDATED_TODOS = msg(
    "tui.tool_card.group.action.todo_updates",
    fallback="made {count} todo update",
    plural_fallback="made {count} todo updates",
)
_SLEPT = msg("tui.tool_card.group.action.sleeps", fallback="slept {duration}")
"""What a group's sleeps did: how long they slept in all, not how many there were."""
_MADE_OTHER_CALLS = msg(
    "tui.tool_card.group.action.other_calls",
    fallback="made {count} other call",
    plural_fallback="made {count} other calls",
)

_ACTIONS: Final[tuple[MessageDef, ...]] = (
    _READ_FILES,
    _SEARCHED_PATTERNS,
    _RAN_WEB_SEARCHES,
    _FETCHED_PAGES,
    _EDITED_FILES,
    _RAN_COMMANDS,
    _USED_SKILLS,
    _MADE_MCP_CALLS,
    _RAN_AGENTS,
    _ASKED_QUESTIONS,
    _UPDATED_TODOS,
    _SLEPT,
    _MADE_OTHER_CALLS,
)
"""What a tool group's calls of each category did, in :data:`_CATEGORIES` order: the clauses of its folded row."""

_FAILED = msg("tui.tool_card.group.summary.failed", fallback="{count} failed", plural_fallback="{count} failed")
_INTERRUPTED = msg(
    "tui.tool_card.group.summary.interrupted",
    fallback="{count} interrupted",
    plural_fallback="{count} interrupted",
)

_SEPARATOR = " · "

_SECONDS = msg("tui.tool_card.group.duration.seconds", fallback="{seconds}s")
_MINUTES = msg("tui.tool_card.group.duration.minutes", fallback="{minutes}m")
_MINUTES_SECONDS = msg("tui.tool_card.group.duration.minutes_seconds", fallback="{minutes}m {seconds}s")
_HOURS = msg("tui.tool_card.group.duration.hours", fallback="{hours}h")
_HOURS_MINUTES = msg("tui.tool_card.group.duration.hours_minutes", fallback="{hours}h {minutes}m")


def duration_words(localizer: Localizer, seconds: int) -> str:
    """*seconds* as a row says a span of time: ``1m 5s``, and from an hour on ``1h 4m``."""
    minutes, rest = divmod(max(seconds, 0), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        words = _HOURS_MINUTES.bind(hours=hours, minutes=minutes) if minutes else _HOURS.bind(hours=hours)
    elif minutes:
        words = _MINUTES_SECONDS.bind(minutes=minutes, seconds=rest) if rest else _MINUTES.bind(minutes=minutes)
    else:
        words = _SECONDS.bind(seconds=rest)
    return render_str(localizer, words)


def tool_category(tool_kind: str, hosted_family: str = "") -> str:
    """The category a folded row counts a call under: a provider-hosted call by its family, others by kind."""
    return _CATEGORY_BY_HOSTED_FAMILY.get(hosted_family) or _CATEGORY_BY_KIND.get(tool_kind, "other")


@dataclass(frozen=True)
class ToolTally:
    """The calls a folded row counts, per category in row order, and how many failed or were interrupted."""

    counts: tuple[int, ...] = (0,) * len(_CATEGORIES)
    failed: int = 0
    interrupted: int = 0
    slept_ms: int = 0
    """How long its sleeps slept in all."""

    @classmethod
    def of(cls, calls: Iterable[tuple[str, ToolOutcome]], *, slept_ms: int = 0) -> ToolTally:
        """Count *calls*, each a category from :func:`tool_category` and how it ended; its sleeps slept *slept_ms*."""
        categories: Counter[str] = Counter()
        outcomes: Counter[ToolOutcome] = Counter()
        for category, outcome in calls:
            categories[category] += 1
            outcomes[outcome] += 1
        return cls(
            tuple(categories[name] for name, _ in _CATEGORIES),
            failed=outcomes["failed"],
            interrupted=outcomes["interrupted"],
            slept_ms=slept_ms,
        )

    def __add__(self, other: ToolTally) -> ToolTally:
        return ToolTally(
            tuple(map(add, self.counts, other.counts, strict=True)),
            failed=self.failed + other.failed,
            interrupted=self.interrupted + other.interrupted,
            slept_ms=self.slept_ms + other.slept_ms,
        )

    @property
    def total(self) -> int:
        return sum(self.counts)

    def count_words(self, localizer: Localizer) -> tuple[str, ...]:
        """Each category with calls, in row order, as its words: ``4 reads``."""
        return tuple(
            render_str(localizer, words.bind(count=count))
            for count, (_, words) in zip(self.counts, _CATEGORIES, strict=True)
            if count
        )

    def action_words(self, localizer: Localizer) -> tuple[str, ...]:
        """Each category with calls, in row order, as what its calls did: ``read 4 files``.

        The first starts the row's sentence, so it opens with a capital. Sleeps say how long they slept.
        """
        clauses: list[str] = []
        for count, words in zip(self.counts, _ACTIONS, strict=True):
            if not count:
                continue
            if words is _SLEPT:
                duration = duration_words(localizer, self.slept_ms // 1000)
                clauses.append(render_str(localizer, _SLEPT.bind(duration=duration)))
            else:
                clauses.append(render_str(localizer, words.bind(count=count)))
        if clauses:
            clauses[0] = clauses[0][:1].upper() + clauses[0][1:]
        return tuple(clauses)

    def outcomes(self, localizer: Localizer) -> tuple[str, str]:
        """The failed and interrupted counts as a row shows them, each empty when there are none."""
        return (
            render_str(localizer, _FAILED.bind(count=self.failed)) if self.failed else "",
            render_str(localizer, _INTERRUPTED.bind(count=self.interrupted)) if self.interrupted else "",
        )


@dataclass(frozen=True)
class FoldRow:
    """A folded row's text: the head, the counts after it, and the outcomes it always keeps in view.

    The head ends with what parts it from the counts, its space or colon included; the counts and outcomes follow one
    another after separators.
    """

    head: str
    counts: tuple[str, ...] = ()
    """Each count's words, as :meth:`ToolTally.count_words` gives them."""
    failed: str = ""
    interrupted: str = ""
    kept: str = ""
    """What follows the head and stays in view as it gives way, as the outcomes do: a running sleep's time left."""
    subject: str = ""
    """A path in the last count's words that a narrow row cuts in its middle, its file name kept, before its end."""

    @property
    def lead(self) -> str:
        """The head and what it keeps in view after it."""
        return f"{self.head}{_SEPARATOR}{self.kept}" if self.kept else self.head


@dataclass(frozen=True)
class FoldRowStyles:
    """The styles of a row's counts and outcomes; the head takes the row's own. A count reads in one style, its
    number included."""

    detail: Style
    failed: Style
    interrupted: Style
    separator: Style | None = field(default=None, kw_only=True)
    """The separators' style; None gives them *detail*."""


def fold_row_styles(widget: Widget, prefix: str) -> FoldRowStyles:
    """*widget*'s ``<prefix>--detail``, ``--failed`` and ``--interrupted`` component styles.

    Each is its colour and text style without a background, so the row keeps the widget's own.
    """

    return FoldRowStyles(
        detail=component_text_style(widget, f"{prefix}--detail"),
        failed=component_text_style(widget, f"{prefix}--failed"),
        interrupted=component_text_style(widget, f"{prefix}--interrupted"),
    )


def fit_fold_row(row: FoldRow, width: int, styles: FoldRowStyles) -> Text:
    """*row* cut to *width* cells: what the head keeps in view and the outcomes keep their room, and the head and
    detail give way first, a path in them from its middle.

    A narrow terminal still shows that calls failed, or how long a sleep has left; only a row narrower than those
    themselves cuts them.
    """
    separator = styles.detail if styles.separator is None else styles.separator
    tail = Text()
    if row.kept:
        tail.append(_SEPARATOR, style=separator)
        tail.append(row.kept)
    for outcome, style in ((row.failed, styles.failed), (row.interrupted, styles.interrupted)):
        if outcome:
            if row.counts or tail:
                tail.append(_SEPARATOR, style=separator)
            tail.append(outcome, style=style)
    counts = row.counts
    room = max(width - tail.cell_len, 1) if tail else width
    if width > 0 and row.subject and counts:
        over = cell_len(row.head) + sum(map(cell_len, counts)) + cell_len(_SEPARATOR) * (len(counts) - 1) - room
        before, found, after = counts[-1].rpartition(row.subject)
        # With no room for even ``…``, the path goes as the row's end does.
        if over > 0 and found and (cut := middle_cut_path(row.subject, cell_len(row.subject) - over)):
            counts = (*counts[:-1], f"{before}{cut}{after}")
    text = Text(row.head)
    for index, words in enumerate(counts):
        if index:
            text.append(_SEPARATOR, style=separator)
        text.append(words, style=styles.detail)
    if width <= 0:
        text.append_text(tail)
        return text
    if tail:
        text.truncate(room, overflow="ellipsis")
        text.append_text(tail)
    text.truncate(width, overflow="ellipsis")
    return text


def middle_cut_path(path: str, cells: int) -> str:
    """*path* in at most *cells* cells: its file name, and as many of its folders from either end as fit, ``…``
    standing for those cut from the middle (``src/chrys/…/tui/app.py``).

    Where the file name alone does not fit, the end of it shows after ``…``, its spaces kept: they tell names apart.
    Empty where not even ``…`` fits.
    """
    if cell_len(path) <= cells:
        return path
    separator = "\\" if "\\" in path and "/" not in path else "/"
    *folders, name = path.split(separator)

    def shown(start: int, end: int) -> str:
        return separator.join([*folders[:start], "…", *folders[len(folders) - end :], name])

    if not folders or cell_len(shown(0, 0)) > cells:
        # ``…`` still tells of the folders cut off: a bare file name would read as one in the working folder.
        return _tail_fit(shown(0, 0) if folders else name, cells)
    start = end = 0
    while start + end < len(folders):
        # Folders join from either end in turn: the root it hangs from, and those nearest the file. Once one end's next
        # folder no longer fits, the other end's still may.
        turns = ((start + 1, end), (start, end + 1)) if start <= end else ((start, end + 1), (start + 1, end))
        grown = next((size for size in turns if cell_len(shown(*size)) <= cells), None)
        if grown is None:
            break
        start, end = grown
    return shown(start, end)
