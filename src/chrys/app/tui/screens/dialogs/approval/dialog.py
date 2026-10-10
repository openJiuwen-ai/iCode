# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ApprovalDialog — modal dialog for tool call approval."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, ClassVar

from rich.cells import split_graphemes
from rich.markup import escape
from rich.text import Text
from textual import events, on
from textual.binding import Binding
from textual.containers import HorizontalGroup, VerticalGroup
from textual.content import Content
from textual.geometry import Size
from textual.screen import ModalScreen
from textual.scroll_view import ScrollView
from textual.strip import Strip
from textual.widgets import Button, Collapsible, RadioButton, RadioSet, Static, TextArea

from chrys.app.tui.behaviors.insert_clipboard import INSERT_CLIPBOARD_BINDINGS, InsertClipboardScreenMixin
from chrys.app.tui.behaviors.right_click_copy import RightClickScreenCopyMixin
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.dialogs.approval.bodies.file_edit import ApprovalDiffPreview
from chrys.app.tui.util.source_text import mark_hidden_format, sanitize_source_text
from chrys.app.tui.widgets import Checkbox, ChrysLoadingIndicator, EnhancedTextArea, StableAutoHeightScroll
from chrys.app.tui.widgets.text_area import NEWLINE_SHORTCUT_KEYS
from chrys.foundation.i18n import MessageDef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_block, sanitize_legacy_scalar
from chrys.foundation.models.approval_reuse import ApprovalReuseOffer, ReuseChoice
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_READ, KIND_FILESYSTEM_WRITE, KIND_SHELL

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.geometry import Offset
    from textual.selection import Selection
    from textual.widget import Widget

    from chrys.app.tui.screens.dialogs.approval.body import ApprovalBody
    from chrys.service.approval.judge import JudgeVerdict

# Keys shown as the detail line (extracted from args, not repeated in the body).
# Lookup is keyed by the chrys tool kind (``chrys.foundation.tool_kinds`` constants).
_DETAIL_KEYS: dict[str, tuple[str, ...]] = {
    KIND_SHELL: ("reason",),
    KIND_FILESYSTEM_READ: ("path",),
    KIND_FILESYSTEM_WRITE: ("path",),
}

# Rationale-style arg keys any tool may carry (remote agents included);
# tried after the kind-specific keys so e.g. shell still prefers "reason".
_DEFAULT_DETAIL_KEYS: tuple[str, ...] = ("reason", "description")

# Keys to hide from the args body (always uninteresting or redundant)
_HIDDEN_KEYS = {"reason", "max_tokens"}

# Human header labels for bridged remote (ACP) requests, keyed by the
# event's presentation_kind (``display_tool_kind`` values plus the broker's
# "remote" fallback). Unknown values also land on "Remote tool".
_RUN_COMMAND = msg("tui.approval.presentation.run_command", fallback="Run command")
_READ_FILES = msg("tui.approval.presentation.read_files", fallback="Read files")
_EDIT_FILES = msg("tui.approval.presentation.edit_files", fallback="Edit files")
_SEARCH = msg("tui.approval.presentation.search", fallback="Search")
_REMOTE_TOOL = msg("tui.approval.presentation.remote_tool", fallback="Remote tool")
_APPROVAL_REQUIRED = msg("tui.approval.required_title", fallback="Approval Required")
_EVALUATING = msg("tui.approval.evaluating", fallback="Evaluating")
_REASON_PLACEHOLDER = msg(
    "tui.approval.reason_placeholder",
    fallback="Reason (optional, sent to agent on Decline)",
)
_APPROVE = msg("tui.approval.button.approve", fallback="Approve (Y)")
_DECLINE = msg("tui.approval.button.decline", fallback="Decline (N)")
_REASON_TITLE = msg("tui.approval.reason_title", fallback="Add a decline reason")
_REUSE_TITLE = msg("tui.approval.reuse.title", fallback="Remember Approval")
_REUSE_SESSION = msg("tui.approval.reuse.session", fallback="This session")
_REUSE_PROJECT = msg("tui.approval.reuse.project", fallback="This project")
_REUSE_SESSION_HINT = msg("tui.approval.reuse.session_hint", fallback="Also applies when you restore this session.")
_REUSE_PROJECT_HINT = msg("tui.approval.reuse.project_hint", fallback="Also applies in other sessions in this project.")
_REUSE_FILES = msg(
    "tui.approval.reuse.files",
    fallback="Don't ask again to modify these files",
)
_REUSE_COMMAND = msg(
    "tui.approval.reuse.command",
    fallback="Don't ask again for this command",
)
_REUSE_ADVANCED = msg("tui.approval.reuse.advanced", fallback="Advanced options")
_REUSE_EXTRA_ARGS = msg("tui.approval.reuse.extra_args", fallback="Allow arguments added to the end of this command")
_REUSE_PREFIX_WARNING = msg(
    "tui.approval.reuse.prefix_warning",
    fallback="Dangerous extra flags (such as --force) will also run without asking.",
)
_FLAGGED = msg("tui.approval.flagged", fallback="Flagged by Auto-Review")

# A remote agent's request can carry a whole file it is about to write, and
# laying out a long value blocks the app for about a second per MiB, on every
# resize too. The dialog lays out its values whole up to this many characters in
# all; a value that does not fit goes into a box that scrolls through all of it
# and draws only the rows in view.
_MAX_VALUE_DISPLAY_CHARS = 64 * 1024

# A stretch of printable ASCII this long is cut into rows by count; the rest of
# a line is measured grapheme by grapheme, as rich draws it. A stretch leaves
# its first and last character to the measured side, so a joiner or variation
# selector next to it still pairs the way rich pairs it.
_ASCII_STRETCH = re.compile(r"[\x20-\x7e]{64,}")

# A value's box replaces its control characters, bidi controls and zero-width
# characters, one for one, so a remote agent's text cannot hide, reorder or
# restyle what is approved; it shows each line break as one LF and each tab as
# spaces up to the next stop this many columns apart. A copy hands back the
# value as given.
_TAB_SIZE = 8
_LINE_BREAK = re.compile(r"\r\n|\r|\n")

_PRESENTATION_LABELS: dict[str, MessageDef] = {
    KIND_SHELL: _RUN_COMMAND,
    KIND_FILESYSTEM_READ: _READ_FILES,
    KIND_FILESYSTEM_WRITE: _EDIT_FILES,
    "search": _SEARCH,
    "remote": _REMOTE_TOOL,
}

# The bridged tool_name is ``acp:`` + the remote-chosen title (spoof-proof
# namespace, mandated by the sub-agent design §9.2) — display strips it.
_ACP_TITLE_PREFIX = "acp:"

_REMOTE_TITLE_MAX_CHARS = 160


def _detail_candidates(tool_kind: str, tool_name: str) -> tuple[str, ...]:
    """Ordered arg keys eligible for the detail line under the header."""
    keys = _DETAIL_KEYS.get(tool_kind, ())
    if not keys and tool_name in ("write_file", "edit_file"):
        keys = ("path",)
    return keys + tuple(k for k in _DEFAULT_DETAIL_KEYS if k not in keys)


def _build_detail(tool_kind: str, tool_name: str, args: dict[str, Any]) -> str:
    """Extract a single human-readable detail line from args."""
    for k in _detail_candidates(tool_kind, tool_name):
        val = args.get(k, "")
        if val:
            return str(val)
    return ""


def _fold_line(line: str, width: int) -> list[str]:
    """Cut one line into pieces at most *width* cells wide, each as full as it can be."""
    if line.isascii():
        return [line[index : index + width] for index in range(0, len(line), width)] or [""]
    if len(line) * 2 <= width:
        return [line]
    pieces: list[str] = []
    row_start = row_cells = 0

    def measured(start: int, end: int) -> None:
        nonlocal row_start, row_cells
        spans, _ = split_graphemes(line[start:end])
        for span_start, _span_end, cells in spans:
            if row_cells and row_cells + cells > width:
                pieces.append(line[row_start : start + span_start])
                row_start, row_cells = start + span_start, 0
            row_cells += cells

    def counted(start: int, end: int) -> None:
        nonlocal row_start, row_cells
        position = start
        while end - position > (room := max(width - row_cells, 0)):
            position += room
            pieces.append(line[row_start:position])
            row_start, row_cells = position, 0
        row_cells += end - position

    position = 0
    for stretch in _ASCII_STRETCH.finditer(line):
        measured(position, stretch.start() + 1)
        counted(stretch.start() + 1, stretch.end() - 1)
        position = stretch.end() - 1
    measured(position, len(line))
    pieces.append(line[row_start:])
    return pieces


def _shown_text(value: str) -> str:
    """Return *value* as a value's box shows it."""
    return mark_hidden_format(sanitize_source_text(value, tab_size=_TAB_SIZE))


def _shown_line(value: str) -> str:
    """Return remote text shown on one line, such as a title, as the boxes would."""
    return mark_hidden_format(sanitize_legacy_scalar(value))


def _value_index(value: str, index: int, *, end: bool) -> int:
    """Return where in *value* the box's text at *index* falls.

    An index inside the spaces a tab is shown as falls before the tab, or after
    it with *end*, so a selection that touches a tab copies all of it.
    """
    shown = line_start = 0
    line_end = len(value)
    for line_break in _LINE_BREAK.finditer(value):
        width = len(value[line_start : line_break.start()].expandtabs(_TAB_SIZE))
        if index <= shown + width:
            line_end = line_break.start()
            break
        shown += width + 1
        line_start = line_break.end()
    line = value[line_start:line_end]
    column = index - shown
    position = shown = 0
    while (tab := line.find("\t", position)) != -1 and column > shown + tab - position:
        shown += tab - position
        width = _TAB_SIZE - shown % _TAB_SIZE
        if column < shown + width:
            return line_start + tab + (1 if end else 0)
        shown += width
        position = tab + 1
    return line_start + min(position + column - shown, len(line))


def _value_slice(value: str, start: int, end: int) -> str:
    """Return the part of *value* the box's text shows from *start* to *end*."""
    return value[_value_index(value, start, end=False) : _value_index(value, end, end=True)]


def _fold_rows(text: str, width: int) -> tuple[list[str], list[int]]:
    """Cut *text* into rows at most *width* cells wide, with where each row starts in it."""
    rows: list[str] = []
    starts: list[int] = []
    line_start = 0
    for line in text.split("\n"):
        start = line_start
        for piece in _fold_line(line, width):
            rows.append(piece)
            starts.append(start)
            start += len(piece)
        line_start += len(line) + 1
    return rows, starts


class _LongValueView(ScrollView):
    """A value too long to lay out at once, shown whole in a scrolling box.

    Its text is cut into rows as wide as the box, again when the box's width
    changes, and only the rows in view are drawn. A selection copies the value
    as given, its tabs and line breaks included, without the breaks the rows add.
    """

    def __init__(self, value: str, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self._value = value
        self._text = _shown_text(value)
        self._rows: list[str] = []
        self._row_starts: list[int] = []
        self._fold_width = 0

    def on_resize(self, _event: events.Resize) -> None:
        # One blank column keeps the text off the scrollbar, as the left
        # padding keeps it off the border.
        width = self.scrollable_content_region.width - 1
        if width < 1 or width == self._fold_width:
            return
        self._fold_width = width
        self._rows, self._row_starts = _fold_rows(self._text, width)
        self.virtual_size = Size(width, len(self._rows))
        self.refresh()

    def render_line(self, y: int) -> Strip:
        scroll_x, scroll_y = self.scroll_offset
        row = scroll_y + y
        width = self.size.width
        rich_style = self.rich_style
        if row >= len(self._rows):
            return Strip.blank(width, rich_style)
        # Text.render drops the base style of a text without spans, so the box's
        # colors go on as a span.
        text = Text(self._rows[row], no_wrap=True)
        text.stylize(rich_style)
        selection = self.text_selection
        if selection is not None and (span := selection.get_span(row)) is not None:
            start, end = span
            selection_style = self.screen.get_component_rich_style("screen--selection")
            text.stylize(selection_style, start, len(text) if end == -1 else end)
        strip = Strip(text.render(self.app.console), text.cell_len)
        return strip.crop_extend(scroll_x, scroll_x + width, rich_style).apply_offsets(scroll_x, row)

    def selection_updated(self, selection: Selection | None) -> None:
        self.refresh()

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        start = 0 if selection.start is None else self._text_index(selection.start)
        end = len(self._text) if selection.end is None else self._text_index(selection.end)
        return _value_slice(self._value, start, end), "\n"

    def _text_index(self, offset: Offset) -> int:
        if offset.y >= len(self._rows):
            return len(self._text)
        row = max(offset.y, 0)
        return self._row_starts[row] + min(max(offset.x, 0), len(self._rows[row]))


class _ValueText(Static):
    """A value short enough to lay out at once, shown and copied as the long box does."""

    def __init__(self, value: str, *, id: str | None = None, classes: str | None = None) -> None:
        self._value = value
        self._text = _shown_text(value)
        super().__init__(Text(self._text), id=id, classes=classes)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        # A selection offset names a line of the text and a character in it.
        line_starts = [0, *(line_break.end() for line_break in re.finditer("\n", self._text))]
        line_ends = [start - 1 for start in line_starts[1:]] + [len(self._text)]

        def text_index(offset: Offset) -> int:
            if offset.y >= len(line_starts):
                return len(self._text)
            line = max(offset.y, 0)
            return min(line_starts[line] + max(offset.x, 0), line_ends[line])

        start = 0 if selection.start is None else text_index(selection.start)
        end = len(self._text) if selection.end is None else text_index(selection.end)
        return _value_slice(self._value, start, end), "\n"


def _value_box(value: str, label: str, *, whole: bool) -> Widget:
    """Return the bordered box that shows one argument *value* under *label*, laid out *whole* or scrolling."""
    if whole:
        box: Widget = _ValueText(value, classes="approval-arg-box")
    else:
        box = _LongValueView(value, classes="approval-arg-box approval-long-value")
    box.border_title = Text(_shown_line(label))
    return box


def _build_args_lines(
    args: dict[str, Any],
    detail_key: str,
    hidden_keys: frozenset[str] | None = None,
) -> list[tuple[str, str]]:
    """Build displayable (label, value) pairs, skipping the detail key and hidden keys."""
    extra_hidden = hidden_keys or frozenset()
    skip = _HIDDEN_KEYS | extra_hidden | ({detail_key} if detail_key else set())
    lines: list[tuple[str, str]] = []
    for k, v in args.items():
        if k in skip:
            continue
        lines.append((k, str(v)))
    return lines


class _ApprovalReasonTextArea(EnhancedTextArea):
    """Decline reason input: Enter and explicit newline shortcuts add a line."""

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter" or event.key in NEWLINE_SHORTCUT_KEYS:
            event.stop()
            event.prevent_default()
            start, end = self.selection
            if self._replace_via_keyboard("\n", start, end):
                self.scroll_cursor_visible()
            return
        await super()._on_key(event)


class _ApprovalButton(Button, inherit_bindings=False):
    """Approval dialog button without Enter-to-press behavior."""

    BINDINGS: ClassVar[list] = []


class _ApprovalRadioButton(RadioButton):
    """Distinguish the selected scope by its mark as well as its color."""

    def render(self) -> Content:
        return Content.assemble(
            ("(*)" if self.value else "( )", self.get_visual_style("toggle--button")),
            self.label.pad(1, 1).stylize_before(self.get_visual_style("toggle--label")),
        )


class ApprovalDialog(
    RightClickScreenCopyMixin,
    InsertClipboardScreenMixin,
    ModalScreen[tuple[bool, str, dict[str, Any] | None] | None],
):
    """Modal dialog for tool call approval.

    User must explicitly approve or decline — Esc is disabled.
    Dismisses with ``(approved, reason, modified_args)`` tuple.

    When ``judging=True`` (AUTO mode), the dialog shows a loading indicator
    at the bottom while the LLM judge evaluates.  Call ``receive_verdict()``
    to deliver the result: approved verdicts auto-dismiss; flagged verdicts
    display the reason and let the user decide.  The user can always click
    Approve/Decline at any time, even while the judge is still running.

    A flagged ``verdict`` given at construction (or delivered before mount)
    opens the dialog flagged with the reason input focused: it may pop up
    while the user is typing, and a stray ``y`` must not approve it.
    """

    BINDINGS: ClassVar[list] = [
        Binding("escape", "noop", show=False, priority=True),
        Binding("left", "switch_focus", show=False),
        Binding("right", "switch_focus", show=False),
        Binding("y,Y", "approve", show=False),
        Binding("n,N", "decline", show=False),
        *INSERT_CLIPBOARD_BINDINGS,
    ]

    CSS_PATH = "approval.tcss"

    def __init__(
        self,
        caller_name: str,
        tool_name: str,
        tool_kind: str = "",
        args: dict[str, Any] | None = None,
        judging: bool = False,
        approval_body: ApprovalBody | None = None,
        presentation_kind: str = "",
        reuse_offer: ApprovalReuseOffer | None = None,
        verdict: JudgeVerdict | None = None,
    ) -> None:
        self._ready_callbacks: list[Callable[[], None]] = []
        self._reuse_offer = reuse_offer
        self.remember_choice: ReuseChoice = ""
        self._tool_name = tool_name
        self._tool_kind = tool_kind
        self._presentation_kind = presentation_kind
        self._args = args or {}
        self._caller_name = caller_name
        self._judging = judging
        self._approval_body = approval_body
        body_detail = approval_body.detail if approval_body is not None else None
        # Bridged (ACP) requests publish tool_kind="" and carry the chrys
        # kind in presentation_kind — fall back so e.g. a remote
        # filesystem.read still gets its path as the detail line.
        detail_kind = tool_kind or presentation_kind
        self._detail = body_detail if body_detail is not None else _build_detail(detail_kind, tool_name, self._args)
        # Find which key was used for the detail line so we can skip it in body
        self._detail_key = ""
        if isinstance(self._detail, str) and self._detail:
            for k in _detail_candidates(detail_kind, tool_name):
                if str(self._args.get(k, "")) == self._detail:
                    self._detail_key = k
                    break
        hidden_keys = approval_body.hidden_arg_keys if approval_body is not None else frozenset()
        self._arg_lines = _build_args_lines(self._args, self._detail_key, hidden_keys)
        self._dismissed = False
        self._dismiss_completed = False
        self._dismiss_result: tuple[bool, str, dict[str, Any] | None] | None = None
        self._user_decision_submitted = False
        self._dismiss_on_resume = False
        # A flag that lands before ``on_mount`` waits here; its widgets exist only then.
        self._flagged: JudgeVerdict | None = None
        self._verdict_widgets_ready = False
        super().__init__()
        if verdict is not None:
            self.receive_verdict(verdict)

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        with VerticalGroup(id="approval-container") as container:
            container.set_class(self._approval_body is not None, "-with-preview")
            container.border_title = Text(render_str(localizer, _APPROVAL_REQUIRED.bind()))
            if self._caller_name:
                container.border_subtitle = Text(self._caller_name)
            # Scrollable body — grows to fill, scrolls when tall.
            with StableAutoHeightScroll(id="approval-inner", can_focus=False):
                if self._presentation_kind:
                    label_definition = _PRESENTATION_LABELS.get(self._presentation_kind, _REMOTE_TOOL)
                    label = render_str(localizer, label_definition.bind())
                    yield Static(
                        f"[reverse] {escape(label)} [/reverse]",
                        id="approval-tool",
                        markup=True,
                    )
                    if remote_title := self._novel_remote_title():
                        yield Static(Text(_shown_line(remote_title)), id="approval-remote-title")
                else:
                    yield Static(
                        f"[reverse] {escape(self._tool_name)} [/reverse]",
                        id="approval-tool",
                        markup=True,
                    )
                # Characters the dialog can still lay out whole.
                room = _MAX_VALUE_DISPLAY_CHARS
                if self._detail:
                    detail = self._detail if isinstance(self._detail, str) else render_str(localizer, self._detail)
                    if len(detail) <= room:
                        room -= len(detail)
                        yield _ValueText(detail, id="approval-detail")
                    else:
                        detail_box = _value_box(detail, self._detail_key, whole=False)
                        detail_box.add_class("approval-detail-box")
                        yield detail_box
                if self._approval_body is not None:
                    yield from self._approval_body.widgets
                if self._arg_lines:
                    with VerticalGroup(id="approval-args"):
                        for label, value in self._arg_lines:
                            whole = len(value) <= room
                            if whole:
                                room -= len(value)
                            yield _value_box(value, label, whole=whole)
                if self._reuse_offer is not None:
                    yield from self._compose_reuse_controls()
                with Collapsible(
                    title=render_str(localizer, _REASON_TITLE.bind()),
                    collapsed=self._flagged is None,
                    id="approval-reason-section",
                ):
                    reason_input = _ApprovalReasonTextArea(
                        id="approval-reason",
                        compact=True,
                        soft_wrap=True,
                        show_line_numbers=False,
                    )
                    reason_input.placeholder = render_str(localizer, _REASON_PLACEHOLDER.bind())
                    yield reason_input
            # Docked footer — separator + judge + buttons always pinned to bottom.
            with VerticalGroup(id="approval-footer"):
                separator = Static("\u2500" * 200, id="approval-separator", markup=False)
                yield separator
                separator.display = self._judging
                with VerticalGroup(id="approval-judge") as judge_area:
                    judge_area.border_title = Text(render_str(localizer, _EVALUATING.bind()))
                    yield ChrysLoadingIndicator(id="approval-judge-loading")
                    yield Static(id="approval-concern")
                judge_area.display = self._judging
                with HorizontalGroup(id="approval-buttons"):
                    yield _ApprovalButton(
                        Text(render_str(localizer, _APPROVE.bind())),
                        id="approval-yes",
                        variant="success",
                        flat=True,
                    )
                    yield _ApprovalButton(
                        Text(render_str(localizer, _DECLINE.bind())),
                        id="approval-no",
                        variant="error",
                        flat=True,
                    )

    def _compose_reuse_controls(self) -> ComposeResult:
        offer = self._reuse_offer
        if offer is None:
            return
        localizer = widget_localizer(self)
        with VerticalGroup(id="approval-reuse") as reuse_group:
            reuse_group.border_title = Text(render_str(localizer, _REUSE_TITLE.bind()))
            description = _REUSE_FILES if offer.kind == "files" else _REUSE_COMMAND
            yield Checkbox(
                Text(render_str(localizer, description.bind())),
                id="reuse-remember",
                compact=True,
                tooltip=Text(mark_hidden_format(sanitize_legacy_block("\n".join(offer.targets)))),
            )
            with VerticalGroup(id="reuse-options") as options:
                options.display = False
                with RadioSet(id="reuse-scope", compact=True):
                    yield _ApprovalRadioButton(
                        Text(render_str(localizer, _REUSE_SESSION.bind())),
                        value=True,
                        id="reuse-session",
                        tooltip=render_str(localizer, _REUSE_SESSION_HINT.bind()),
                    )
                    yield _ApprovalRadioButton(
                        Text(render_str(localizer, _REUSE_PROJECT.bind())),
                        id="reuse-project",
                        tooltip=render_str(localizer, _REUSE_PROJECT_HINT.bind()),
                    )
                if offer.prefix:
                    with Collapsible(title=render_str(localizer, _REUSE_ADVANCED.bind()), id="reuse-advanced"):
                        yield Checkbox(
                            Text(render_str(localizer, _REUSE_EXTRA_ARGS.bind())),
                            id="reuse-extra-args",
                            compact=True,
                        )
                        warning = Static(Text(render_str(localizer, _REUSE_PREFIX_WARNING.bind())), id="reuse-warning")
                        warning.display = False
                        yield warning

    @on(Checkbox.Changed, "#reuse-remember")
    def _on_reuse_remember_changed(self, event: Checkbox.Changed) -> None:
        self.query_one("#reuse-options").display = event.value
        if not event.value and self._reuse_offer is not None and self._reuse_offer.prefix:
            # Opting out must not leave a broader hidden permission selected.
            self.query_one("#reuse-extra-args", Checkbox).value = False
            self.query_one("#reuse-advanced", Collapsible).collapsed = True

    @on(Checkbox.Changed, "#reuse-extra-args")
    def _on_reuse_extra_args_changed(self, event: Checkbox.Changed) -> None:
        self.query_one("#reuse-warning").display = event.value

    def _selected_reuse_choice(self) -> ReuseChoice:
        offer = self._reuse_offer
        if offer is None or not self.query_one("#reuse-remember", Checkbox).value:
            return ""
        session = self.query_one("#reuse-session", RadioButton).value
        prefix = offer.prefix and self.query_one("#reuse-extra-args", Checkbox).value
        if prefix:
            return "PREFIX_SESSION" if session else "PREFIX_PROJECT"
        return "EXACT_SESSION" if session else "EXACT_PROJECT"

    def _novel_remote_title(self) -> str:
        """Remote-chosen title, only when it adds information beyond the body.

        For shell-style requests the remote title is the command itself and
        already fills an arg box — repeating it as a header is pure noise.
        Probe with a bounded prefix so the broker's over-2000-char digest
        suffix can never defeat the containment check.
        """
        title = self._tool_name.removeprefix(_ACP_TITLE_PREFIX).strip().strip("`").strip()
        if not title:
            return ""
        definition = _PRESENTATION_LABELS.get(self._presentation_kind, _REMOTE_TOOL)
        english_label = format_message(definition.bind())
        localized_label = render_str(widget_localizer(self), definition.bind())
        if title.casefold() in {english_label.casefold(), localized_label.casefold()}:
            # A title that merely echoes the header chip adds nothing.
            return ""
        probe = title[:120]
        haystacks = [str(value) for value in self._args.values()]
        if self._detail:
            detail = self._detail if isinstance(self._detail, str) else render_str(widget_localizer(self), self._detail)
            haystacks.append(detail)
        if any(probe in haystack for haystack in haystacks):
            return ""
        if len(title) > _REMOTE_TITLE_MAX_CHARS:
            return title[: _REMOTE_TITLE_MAX_CHARS - 1] + "…"
        return title

    def on_mount(self) -> None:
        self.call_after_refresh(self._notify_ready)
        self._verdict_widgets_ready = True
        if self._flagged is not None:
            self._show_flagged(self._flagged)
            self.query_one("#approval-reason", _ApprovalReasonTextArea).focus()
        else:
            self.query_one("#approval-yes", Button).focus()
        if self._dismissed:
            # The dialog counts as mounted only once its mount handlers have returned.
            self.call_later(self._dismiss_if_top)

    def when_ready(self, callback: Callable[[], None]) -> None:
        """Run after mount and asynchronous preview content have been painted."""
        if self._dismissed:
            return
        self._ready_callbacks.append(callback)
        if self.is_mounted:
            self.call_after_refresh(self._notify_ready)

    @on(ApprovalDiffPreview.Ready)
    def _on_preview_ready(self, event: ApprovalDiffPreview.Ready) -> None:
        event.stop()
        self.call_after_refresh(self._notify_ready)

    def _notify_ready(self) -> None:
        if not self.is_mounted or self._dismissed or self.app.screen is not self:
            return
        if any(not preview.is_ready for preview in self.query(ApprovalDiffPreview)):
            return
        callbacks, self._ready_callbacks = self._ready_callbacks, []
        for callback in callbacks:
            callback()

    @property
    def tool_name(self) -> str:
        """The tool name this dialog asks about, as it was given."""
        return self._tool_name

    @property
    def is_dismissed(self) -> bool:
        """True once the dialog has been dismissed (approve/decline/verdict)."""
        return self._dismissed

    @property
    def user_decision_submitted(self) -> bool:
        """True once the user has clicked Approve or Decline."""
        return self._user_decision_submitted

    def _safe_dismiss(
        self,
        result: tuple[bool, str, dict[str, Any] | None] | None,
        *,
        user_decision: bool = False,
    ) -> None:
        """Dismiss the dialog at most once, preventing double-dismiss races.

        Hides the container synchronously before calling ``dismiss()`` so the
        user gets instant visual feedback: Textual's ``dismiss`` schedules the
        result callback via ``call_next`` and the DOM removal as an
        ``AwaitComplete`` — until those run, the dialog stays visible and any
        further button clicks hit this same guard and silently no-op.  Hiding
        the container here guarantees the dialog disappears the moment the
        verdict lands, even when the judge auto-approves from an event-bus
        handler (where the Textual message pump might be busy).
        """
        if self._dismissed:
            return
        self._dismissed = True
        self._ready_callbacks.clear()
        self._dismiss_result = result
        self._user_decision_submitted = user_decision
        with contextlib.suppress(Exception):
            self.query_one("#approval-container").display = False
        self._dismiss_if_top()

    def _dismiss_if_top(self) -> None:
        if self._dismiss_completed:
            return
        if not self.is_mounted:
            # Removing a dialog that is still composing mounts its widgets without their children.
            return
        is_top = False
        with contextlib.suppress(Exception):
            is_top = self.app.screen is self
        if not is_top:
            self._dismiss_on_resume = True
            return
        self._dismiss_on_resume = False
        self._dismiss_completed = True
        self.dismiss(self._dismiss_result)

    def dismiss_due_to_cancellation(self) -> None:
        """Close an abandoned request without manufacturing a user decision."""
        self._safe_dismiss(None)

    def on_screen_resume(self, _event: events.ScreenResume) -> None:
        self.call_after_refresh(self._notify_ready)
        if self._dismiss_on_resume:
            self._dismiss_on_resume = False
            # Resume is queued; another modal may already cover us again.
            self._dismiss_if_top()

    def receive_verdict(self, verdict: JudgeVerdict) -> None:
        """Deliver the LLM judge result to the dialog.

        Called by the event handler once the judge finishes.  If the verdict
        approves the tool call, the dialog auto-dismisses.  Otherwise it
        shows the concern and lets the user decide; before mount it keeps
        the verdict and opens flagged.

        If the user already clicked Approve/Decline, this is a no-op.
        """
        if self._dismissed:
            return

        if verdict.approved:
            self._safe_dismiss((True, "", None))
            return

        self._flagged = verdict
        if self._verdict_widgets_ready:
            self._show_flagged(verdict)

    def _show_flagged(self, verdict: JudgeVerdict) -> None:
        """Hide the spinner, show the concern and switch the border to error."""
        judge_area = self.query_one("#approval-judge", VerticalGroup)
        judge_area.display = True
        judge_area.border_title = Text(render_str(widget_localizer(self), _FLAGGED.bind()))
        judge_area.add_class("judge-flagged")
        self.query_one("#approval-judge-loading").display = False
        concern_widget = self.query_one("#approval-concern", Static)
        concern_widget.update(Text(mark_hidden_format(sanitize_legacy_block(verdict.reason))))
        concern_widget.display = True
        self.query_one("#approval-reason-section", Collapsible).collapsed = False

    @on(TextArea.Changed, "#approval-reason")
    def _on_reason_changed(self, event: TextArea.Changed) -> None:
        self._refresh_approve_disabled(event.text_area.text)

    def _refresh_approve_disabled(self, raw_reason: str) -> None:
        approve = self.query_one("#approval-yes", Button)
        should_disable = bool(raw_reason)
        approve_had_focus = approve.has_focus
        approve.disabled = should_disable
        if should_disable and approve_had_focus:
            self.query_one("#approval-no", Button).focus()

    @on(Button.Pressed, "#approval-yes")
    def _on_approve(self, event: Button.Pressed) -> None:
        if event.button.disabled:
            return
        modified_args_fn = self._approval_body.modified_args if self._approval_body is not None else None
        modified_args = modified_args_fn() if modified_args_fn is not None else None
        reason = self.query_one("#approval-reason", _ApprovalReasonTextArea).text.strip()
        if not modified_args:
            self.remember_choice = self._selected_reuse_choice()
        self._safe_dismiss((True, reason, modified_args), user_decision=True)

    @on(Button.Pressed, "#approval-no")
    def _on_decline(self, event: Button.Pressed) -> None:
        reason = self.query_one("#approval-reason", _ApprovalReasonTextArea).text.strip()
        self._safe_dismiss((False, reason, None), user_decision=True)

    def action_switch_focus(self) -> None:
        yes = self.query_one("#approval-yes", Button)
        no = self.query_one("#approval-no", Button)
        if yes.has_focus:
            no.focus()
        else:
            yes.focus()

    def action_approve(self) -> None:
        approve = self.query_one("#approval-yes", Button)
        if not approve.disabled:
            approve.press()

    def action_decline(self) -> None:
        self.query_one("#approval-no", Button).press()

    def action_noop(self) -> None:
        """Swallow Esc — user must explicitly approve or decline."""
