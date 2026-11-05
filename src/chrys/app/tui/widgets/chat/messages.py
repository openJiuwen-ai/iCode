# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Chat message widgets and inline conversation status actions."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, NamedTuple, TypeGuard

from rich.cells import cell_len
from rich.text import Text
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.geometry import Region, Size
from textual.message import Message
from textual.reactive import reactive
from textual.selection import Selection
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Button, Static

from chrys.app.tui.clipboard import copy_text_to_clipboards
from chrys.app.tui.copy_messages import COPIED_TITLE
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.support.gc_freeze import (
    GcAbsorbReason,
    GcAbsorbRequested,
    GcReclaimReason,
    GcReclaimRequested,
    GcRemovedContent,
    current_gc_freeze_epoch,
)
from chrys.app.tui.util.removal import remove_all_shielded, remove_shielded
from chrys.app.tui.util.shimmer import FRAME_SECONDS
from chrys.app.tui.util.source_text import sanitize_source_text
from chrys.app.tui.widgets.chat.fold_gap import FOLD_GAP_ROWS, FoldGap
from chrys.app.tui.widgets.chat.fold_rows import (
    AGENT_LABEL,
    FOLDED_ARROW,
    FOLDED_LABEL,
    OPEN_ARROW,
    THINKING_LABEL,
    THOUGHT_LABEL,
    text_preview,
    text_tail,
)
from chrys.app.tui.widgets.chat.image_preview import extract_image_previews
from chrys.app.tui.widgets.chat.image_view import TerminalImages
from chrys.app.tui.widgets.chat.process_visibility import ProcessMember
from chrys.app.tui.widgets.chat.transcript_panel import note_fold
from chrys.app.tui.widgets.click_affordance import ClickAffordance
from chrys.app.tui.widgets.loading import GatedAnimation
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.app.tui.widgets.markdown.parser import create_user_text_markdown_parser
from chrys.foundation.i18n import DisplayBlock, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.util.time import parse_created_at

if TYPE_CHECKING:
    from textual.app import ComposeResult, RenderResult
    from textual.await_complete import AwaitComplete

    from chrys.foundation.models.invocations import ContentChannel

logger = logging.getLogger(__name__)

INTERRUPTED_ERROR_HEADER_MESSAGE = msg(
    "transcript.interrupted.error_header",
    fallback="✗ Error",
)
INTERRUPTED_WARNING_HEADER_MESSAGE = msg(
    "transcript.interrupted.warning_header",
    fallback="⚠ Interrupted",
)
INTERRUPTED_REASON_PLAIN_MESSAGE = msg(
    "transcript.interrupted.reason_plain",
    fallback="{reason}",
    multiline=True,
)
INTERRUPTED_REASON_BY_USER_MESSAGE = msg(
    "transcript.interrupted.reason_by_user",
    fallback="{reason} by user",
    multiline=True,
)
INTERRUPTED_RETRY_ACTION_MESSAGE = msg(
    "transcript.interrupted.retry_action",
    fallback="Retry",
)
INTERRUPTED_CONTINUE_ACTION_MESSAGE = msg(
    "transcript.interrupted.continue_action",
    fallback="Continue",
)
_AGENT_RESPONSE_COPIED = msg("tui.copy.agent_response", fallback="Copied agent response")
_AGENT_RESPONSE_COPY_TOOLTIP = msg(
    "tui.chat.copy_agent_response_tooltip",
    fallback="Copy raw agent response",
)
COPY_MESSAGE_BUTTON = msg("tui.chat.copy_message_button", fallback="copy")
_STREAMED_BODY_INTERVAL_SECONDS = 0.2
"""Shortest time between two markdown updates of a streaming message.

Each one parses the whole text again, allocating a new parse, and once it adds
a row lays out the whole transcript again: one per delta would keep a CPU core
busy and the garbage collector behind it.
"""
_USER_MESSAGE_COPIED = msg("tui.copy.user_message", fallback="Copied your message")
_USER_MESSAGE_COPY_TOOLTIP = msg("tui.chat.copy_user_message_tooltip", fallback="Copy your raw message")
_THINKING_SUMMARY_LABEL = msg("tui.chat.thinking_summary_label", fallback="Thinking summary")
_RETRY_MESSAGE = msg(
    "tui.chat.retry_message",
    fallback="{message} Retrying in {delay_seconds}s ({attempt}/{max_attempts})...",
    multiline=True,
)


def _format_duration_ms(duration_ms: int) -> str:
    from chrys.app.tui.widgets.chat.tool_call import fmt_duration

    return fmt_duration(duration_ms)


@dataclass(frozen=True)
class InterruptedMessageCopy:
    """Resolved, mount-stable copy for an interruption notice."""

    header: str
    reason: str
    retry_action: str
    continue_action: str


def resolve_interrupted_message_copy(
    reason: str,
    source: str,
    resolver: Callable[[MessageRef], str] = format_message,
) -> InterruptedMessageCopy:
    """Resolve interruption chrome and its closed-set source prose."""
    header_definition = INTERRUPTED_ERROR_HEADER_MESSAGE if source == "error" else INTERRUPTED_WARNING_HEADER_MESSAGE
    reason_definition = INTERRUPTED_REASON_BY_USER_MESSAGE if source == "user" else INTERRUPTED_REASON_PLAIN_MESSAGE
    return InterruptedMessageCopy(
        header=resolver(header_definition.bind()),
        reason=resolver(reason_definition.bind(reason=DisplayBlock(reason))),
        retry_action=resolver(INTERRUPTED_RETRY_ACTION_MESSAGE.bind()),
        continue_action=resolver(INTERRUPTED_CONTINUE_ACTION_MESSAGE.bind()),
    )


def _aware_created_at(value: Any) -> datetime | None:
    """A persisted message timestamp; one with no zone is UTC."""
    dt = parse_created_at(value)
    if dt is not None and (dt.tzinfo is None or dt.utcoffset() is None):
        dt = dt.replace(tzinfo=UTC)
    return dt


def format_message_created_at(value: Any) -> str:
    """Format a persisted message timestamp in the user's local time."""
    dt = _aware_created_at(value)
    if dt is None:
        return ""

    local = dt.astimezone()
    hour = local.hour % 12 or 12
    suffix = "AM" if local.hour < 12 else "PM"
    return f"- {hour}:{local.minute:02d} {suffix}"


def turn_duration_ms(started_at: Any, finished_at: Any) -> int | None:
    """How long a turn took to its answer, in milliseconds: from when its pass began to when the answer finished.

    None unless both are timestamps, in that order.
    """
    start, end = _aware_created_at(started_at), _aware_created_at(finished_at)
    if start is None or end is None or end < start:
        return None
    return (end - start) // timedelta(milliseconds=1)


def format_chat_copy_payload(messages: list[tuple[str, str]]) -> str:
    """Return the transcript payload used by chat copy actions."""
    return "\n\n".join(f"[{name}]\n{text}" for name, text in messages)


def _user_header_text(*, timestamp: str) -> Text:
    """The user message header: the ``You`` label and its timestamp."""
    header = Text()
    header.append("\u276f You", style="bold cyan")
    if timestamp:
        header.append(f" {timestamp}", style="dim")
    return header


class _UserHeader(Static):
    """Header of a user message."""

    def __init__(self, content: Text) -> None:
        super().__init__(content, classes="message-header")

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Copy the user header as a transcript label, not decorative chrome."""
        _ = selection
        return "[You]", "\n"


class _UserImagePreview(TerminalImages):
    """The images a user message carries, one blank row below its text."""

    DEFAULT_CSS = """
    _UserImagePreview {
        padding-top: 1;
    }
    """


class MessageHeaderRow(Horizontal):
    """Header row of a chat message: its label, then inline actions."""

    ALLOW_SELECT = False

    DEFAULT_CSS = """
    MessageHeaderRow {
        width: 100%;
        height: 1;
    }
    MessageHeaderRow > .message-header {
        width: auto;
        height: 1;
        padding: 0 1 0 0;
    }
    MessageHeaderRow > .fold-row-header {
        width: 1fr;
    }
    MessageHeaderRow > MessageCopyButton {
        display: none;
        width: auto;
        height: 1;
        padding: 0 0 0 1;
        color: $text-muted;
        text-style: dim not bold;
        pointer: pointer;
    }
    MessageHeaderRow > MessageCopyButton:hover {
        color: $accent;
        text-style: underline;
    }
    """

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        _ = selection
        return None


class MessageCopyButton(ClickAffordance):
    """Clickable copy affordance in a chat message header; copies the raw text."""

    ALLOW_SELECT = False

    class Clicked(Message):
        """Posted when the copy affordance is clicked."""

    CLICK_MESSAGE = Clicked

    def __init__(self, *, tooltip: str, text: str | None = None) -> None:
        super().__init__(Text(text or format_message(COPY_MESSAGE_BUTTON.bind())))
        self.tooltip = tooltip

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        _ = selection
        return None


class UserMessage(Widget):
    """User message block with cyan accent, rendered as markdown.

    Newlines stay line breaks and HTML tags stay text, as the user typed them.
    """

    DEFAULT_CSS = """
    /* A column short of the right edge: its background ends apart from the scrollbar. */
    UserMessage {
        margin: 1 1 1 0;
        padding: 0 1 0 1;
        border-left: thick $tui-border-user-message $border-opacity;
        background: $tui-user-message-background;
        height: auto;
    }
    /* The terminal owns an ANSI theme's background, which no tint can be worked out over. */
    UserMessage:ansi {
        background: $boost;
    }
    UserMessage.-compressed {
        border-left: thick $tui-border-neutral-160 $border-opacity;
        color: $text-muted;
    }
    UserMessage.-compressed > VirtualizedMarkdown {
        color: $text-muted;
    }
    /* After -compressed, so a selected turn shows its selection even when compressed. */
    UserMessage.-highlighted {
        background: $primary 18%;
        border-left: thick $tui-border-selected-turn $border-opacity;
    }
    UserMessage.-highlighted:ansi {
        background: $tui-ansi-turn-background;
        border-left: thick $tui-border-neutral-128;
    }
    /* A note sent mid-turn looks like any user message, indented to the calls of the turn around it. */
    UserMessage.-injection {
        margin: 1 1 1 2;
    }
    UserMessage > VirtualizedMarkdown {
        padding: 0;
        height: auto;
        min-height: 1;
        background: transparent;
    }
    """

    def __init__(
        self,
        text: str,
        timestamp: str = "",
        compressed: bool = False,
        is_injection: bool = False,
        contents: list[Any] | None = None,
    ) -> None:
        self._text = text
        self._ts = timestamp
        self._compressed = compressed
        self._is_injection = is_injection
        self._image_previews = extract_image_previews(contents)
        super().__init__()
        if compressed:
            self.add_class("-compressed")
        if is_injection:
            self.add_class("-injection")

    def compose(self) -> ComposeResult:
        with MessageHeaderRow():
            yield _UserHeader(_user_header_text(timestamp=self._ts))
            copy_button = MessageCopyButton(
                tooltip=self._render_message(_USER_MESSAGE_COPY_TOOLTIP.bind()),
                text=self._render_message(COPY_MESSAGE_BUTTON.bind()),
            )
            copy_button.display = self._should_show_copy_button()
            yield copy_button
        yield VirtualizedMarkdown(
            sanitize_source_text(self._text),
            parser_factory=create_user_text_markdown_parser,
        )
        if self._image_previews:
            yield _UserImagePreview(self._image_previews)

    @property
    def text(self) -> str:
        """The message as the user wrote it."""
        return self._text

    @property
    def is_injection(self) -> bool:
        """Whether this user message is a mid-turn injection."""
        return self._is_injection

    @property
    def is_compressed(self) -> bool:
        """Whether this message belongs to archived, compressed history."""
        return self._compressed

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """User message content is copied by its header/body children."""
        _ = selection
        return None

    def _render_message(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)

    def _should_show_copy_button(self) -> bool:
        """Offer copy for text outside compressed history, as agent responses do."""
        return not self._compressed and bool(self._text.strip())

    def copy_user_message(self) -> None:
        """Copy the message as the user wrote it to the available clipboards."""
        if not self._text.strip():
            return
        copy_text_to_clipboards(self.app, self._text)
        localizer = widget_localizer(self)
        self.notify(
            render_str(localizer, _USER_MESSAGE_COPIED.bind()),
            title=render_str(localizer, COPIED_TITLE.bind()),
            timeout=2,
            markup=False,
        )

    def on_message_copy_button_clicked(self, event: MessageCopyButton.Clicked) -> None:
        """Handle clicks from the header copy affordance."""
        event.stop()
        if not self._should_show_copy_button():
            return
        self.copy_user_message()


class _AgentHeader(Static):
    """Clickable header of an AgentMessage or ThinkingMessage — posts Clicked on click."""

    class Clicked(Message):
        """Posted when the header is clicked."""

    def __init__(self, label: str, content: Text) -> None:
        self._copy_label = label
        super().__init__(content, classes="agent-header message-header")

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Copy the agent header as a transcript label, not decorative chrome."""
        return f"[{self._copy_label}]", "\n"

    def on_click(self) -> None:
        # A drag across the header ends in a click too; the text it selected stays, and nothing folds.
        if self.screen.selections.get(self) is None:
            self.post_message(self.Clicked())


class _ShownRow(NamedTuple):
    """A fold row header's row as it paints."""

    label: str
    """The label, as a selection copies it."""
    head: str
    """The arrow, then the label the row shows and what parts it from the preview."""
    label_end: int
    """Where the label shown in *head* ends; it starts after the arrow."""
    preview: str


_ROW_LABEL_START = len(OPEN_ARROW) + 1
_MARQUEE_CATCH_UP_SECONDS = 0.5
"""How far a running row trails the streamed text: the text that arrived in about this long."""
_MARQUEE_MIN_CHARS_PER_SECOND = 24.0
"""The slowest a running row moves while it trails the text, so the last words arrive soon."""
_MARQUEE_MAX_STEP_SECONDS = 0.25
"""The longest pause a running row makes up for at once, after a stall in painting."""


class _FoldRowHeader(GatedAnimation, _AgentHeader):
    """The header of thinking or text between tool calls: open, its label; folded, a sentence of the opening of its text.

    Folded, the text follows the label and a colon (``Thought: …``), or stands alone where the row is the agent's
    own words; it is cut to the row's width. The label takes the ``fold-row-header--label`` component's colour and
    the text the ``--preview`` component's, which each kind of message sets, so its rows stand apart from others.

    The row is worked out when it first paints after a fold, unfold or new label and kept until the next one: a
    resize only cuts it again, and a language switch reaches it at its next fold, as it reaches other transcript
    text, so what a selection's offsets index is always the row on screen. A thinking block folded inside a
    hidden activity section never reads its preview.

    Folded while its text streams, the row runs the newest of the text along it instead, coming in from the right
    at the pace the text arrives, smoothed between the bursts it arrives in. Its refresh timer starts the first
    time it runs, so a row that never does keeps none.
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = {"fold-row-header--label", "fold-row-header--preview"}
    _animating = False

    def __init__(
        self, label: MessageRef, *, collapsed: bool, text: str, folded_label: bool = True, streaming: bool = False
    ) -> None:
        self._label = label
        self._folded_label = folded_label
        """Whether the folded row names its label before the text."""
        self._collapsed = collapsed
        self._text = text
        self._streaming = streaming
        """Whether the text still streams; never ``_running``, which is MessagePump's own state."""
        self._marquee_end = 0.0
        """How far into the text a running row has got, in characters: a row folded as it streamed starts at 0."""
        self._marquee_at: float | None = None
        """When the running row last moved (``time.monotonic()``); None until it first paints."""
        self._marquee_shown = ""
        """The text a running row last painted after its label, as a selection copies it."""
        self._preview_source: str | None = None
        self._preview = ""
        self._row: _ShownRow | None = None
        """The row as it last painted; None until it next paints."""
        super().__init__("", Text())
        # Not mounted yet: mounting styles it.
        self.add_class("fold-row-header", update=False)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Copy the label as a transcript label, and from a folded row the preview text the selection covers.

        Offsets are characters of the row; a selection running on past the row copies the whole preview,
        past where the row cuts it.
        """
        label, head, _, preview = self._shown_row()
        if self._runs:
            preview = self._marquee_shown
        if not preview:
            return f"[{label}]", "\n"
        start = 0 if selection.start is None else selection.start.x
        end = None if selection.end is None else selection.end.x
        covered = (head + preview)[max(start, len(head)) : end]
        copied = " ".join(part for part in (f"[{label}]" if start < len(head) else "", covered) if part)
        return copied, "\n"

    def show(self, *, collapsed: bool, text: str, label: MessageRef | None = None, streaming: bool = False) -> None:
        """Show the folded row for *text*, or the open header, under *label* when given; one row, so no layout.

        Folded while *streaming*, the row runs the newest of the text.
        """
        relabelled = label is not None and label != self._label
        reshaped = relabelled or collapsed != self._collapsed or streaming != self._streaming
        if not (reshaped or (collapsed and text is not self._text)):
            return
        if collapsed and streaming and not self._runs:
            # Folded as it streams: the row goes on from the newest text, not from its start.
            self._marquee_end, self._marquee_at = float(len(text)), None
        if label is not None:
            self._label = label
        self._collapsed, self._text, self._streaming = collapsed, text, streaming
        if reshaped or not self._runs:
            # A running row reads the newest text as it paints.
            self._row = None
        self._sync_marquee()
        self.refresh(layout=False)

    @property
    def _runs(self) -> bool:
        """Whether the row runs the streamed text along it."""
        return self._collapsed and self._streaming and self._folded_label

    def on_mount(self) -> None:
        if self._runs:
            # Mounted folded as its text streams; ``is_mounted`` reads True only once the mount handlers return.
            self.auto_refresh = FRAME_SECONDS
        self._sync_marquee()

    def _sync_marquee(self) -> None:
        if not self._runs:
            self.pause_animation()
            return
        if self._auto_refresh_timer is None and self.is_mounted:
            self.auto_refresh = FRAME_SECONDS
        self.resume_animation()

    def _advance_marquee(self) -> int:
        """How far into the text the running row reaches as it paints now, in characters."""
        end = len(self._text)
        now = time.monotonic()
        if self.app.animation_level == "none":
            self._marquee_end = float(end)
        elif self._marquee_end < end and self._marquee_at is not None:
            behind = end - self._marquee_end
            speed = max(behind / _MARQUEE_CATCH_UP_SECONDS, _MARQUEE_MIN_CHARS_PER_SECOND)
            step = speed * min(now - self._marquee_at, _MARQUEE_MAX_STEP_SECONDS)
            self._marquee_end = min(self._marquee_end + step, float(end))
        self._marquee_at = now
        return int(self._marquee_end)

    def _shown_row(self) -> _ShownRow:
        if self._row is not None:
            return self._row
        localizer = widget_localizer(self)
        label = render_str(localizer, self._label)
        if self._runs:
            # The text after the label is the newest of it as the row paints.
            shown = render_str(localizer, FOLDED_LABEL.bind(label=label))
            self._row = _ShownRow(label, f"{FOLDED_ARROW} {shown}", _ROW_LABEL_START + len(shown.rstrip()), "")
            return self._row
        if self._collapsed and self._preview_source is not self._text:
            self._preview_source = self._text
            self._preview = text_preview(self._text)
        if not self._collapsed or not self._preview:
            arrow = FOLDED_ARROW if self._collapsed else OPEN_ARROW
            self._row = _ShownRow(label, f"{arrow} {label}", _ROW_LABEL_START + len(label), "")
        elif self._folded_label:
            shown = render_str(localizer, FOLDED_LABEL.bind(label=label))
            label_end = _ROW_LABEL_START + len(shown.rstrip())
            self._row = _ShownRow(label, f"{FOLDED_ARROW} {shown}", label_end, self._preview)
        else:
            self._row = _ShownRow(label, f"{FOLDED_ARROW} ", _ROW_LABEL_START, self._preview)
        return self._row

    def render(self) -> Text:
        row = self._shown_row()
        width = self.content_size.width
        preview = row.preview
        if self._runs:
            preview = text_tail(self._text, self._advance_marquee(), max(width - cell_len(row.head), 0))
            self._marquee_shown = preview
        text = Text(row.head + preview)
        text.stylize(self.text_style_of("fold-row-header--label"), _ROW_LABEL_START, row.label_end)
        if preview:
            text.stylize(self.text_style_of("fold-row-header--preview"), len(row.head))
        if width:
            text.truncate(width, overflow="ellipsis")
        return text


class _AgentReplyHeader(_FoldRowHeader):
    """An agent message's header: the reply's own header, or a fold row once its text sits between tool calls.

    Folded, that row is the agent's own words, with no label before them.
    """

    def __init__(self, copy_label: str, reply: Text | None, *, collapsed: bool, text: str) -> None:
        super().__init__(AGENT_LABEL.bind(), collapsed=collapsed, text=text, folded_label=False)
        self._copy_label = copy_label
        self._reply = reply
        """The reply header shown, or None for the fold row."""
        self.set_class(reply is None, "fold-row-header", update=False)

    def show_reply(self, reply: Text) -> None:
        """Show *reply* as the reply's header; one row, so no layout."""
        self._reply = reply
        self.refresh(layout=False)

    def show(self, *, collapsed: bool, text: str, label: MessageRef | None = None, streaming: bool = False) -> None:
        if self._reply is not None:
            # The text turned out to sit between tool calls: the row takes the header's width, cut to it.
            self._reply = None
            self._collapsed = collapsed
            self._text = text
            self._row = None
            self.add_class("fold-row-header")
            self.refresh()
            return
        super().show(collapsed=collapsed, text=text, label=label, streaming=streaming)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        if self._reply is not None:
            return f"[{self._copy_label}]", "\n"
        return super().get_selection(selection)

    def render(self) -> Text:
        return super().render() if self._reply is None else self._reply


class StreamedMarkdownMessage(FoldGap, ProcessMember):
    """A transcript message whose markdown body can stream in.

    Holds what streaming needs and what keeps it cheap: the body catches up at
    most every ``_STREAMED_BODY_INTERVAL_SECONDS`` and a height change lays the
    transcript out once. A subclass composes the header
    and the body (:meth:`_new_body`) and reacts through :meth:`_refresh_header`
    and :meth:`_on_final`; one whose body waits for it to open
    (:meth:`_builds_body_on_opening`) hands a folded body no text and builds
    its body the first time it opens.
    """

    collapsed: reactive[bool] = reactive(False)

    def __init__(self, text: str = "", *, is_final: bool = True, classes: str = "") -> None:
        self._source_text = text
        self._body_stale = False
        """The markdown body has not been handed the latest streamed text."""
        self._body_sync_scheduled = False
        """A callback that brings the markdown body up to date is queued on this widget."""
        self._body_synced_at = float("-inf")
        """When the markdown body was last handed new text (``time.monotonic()``)."""
        self._measured_rows: tuple[int, int] | None = None
        """The rows it was last measured at, as (content width, rows); None until measured."""
        self._is_final = is_final
        self._md_widget: VirtualizedMarkdown | None = None
        self._body_freeze_epoch: int | None = None
        self._absorb_after_parse = False
        """The body was handed its text on opening: that parse is stable content the GC freeze can take."""
        self._body_structure_lock = asyncio.Lock()
        self._body_parse: AwaitComplete | None = None
        """The body's parse of the text it was last handed: it installs that text or newer."""
        self._background_strip_cache_key: tuple[int, int, int, str, str, int, int] | None = None
        self._background_strip_cache: Strip | None = None
        super().__init__(classes=classes)

    @property
    def text(self) -> str:
        """The message text as of the latest ``stream_update()``."""
        return self._source_text.strip()

    @property
    def foldable(self) -> bool:
        """Whether it folds to a one-row header with its turn, or on its own fold: never an answer."""
        return False

    def render(self) -> RenderResult:
        # Its header and body cover the rest: only its gap shows its own content.
        return self.gap_text() if self.gap_above else super().render()

    def _builds_body_on_opening(self) -> bool:
        """Whether its body waits for it to open: built folded, it composes none, and a folded body is handed no text.

        Opening builds the body from the newest text, or hands the one it kept that text. A hidden body handed text
        would parse it, and lay the transcript out, for nothing.
        """
        return False

    def _releases_body_on_folding(self) -> bool:
        """Whether, its body waiting for it to open, the body goes as it folds; removing it lays the transcript out."""
        return False

    def _shows_body(self) -> bool:
        """Whether it shows its body: open, and, a body built or handed its text on opening, once that text shows.

        Until then it ends folded: the row under it keeps its gap, and the transcript its height, until the body
        takes their place.
        """
        body = self._md_widget
        return not self.collapsed and body is not None and body.display

    def _new_body(self) -> VirtualizedMarkdown:
        """The markdown body, built from the current text, for :meth:`compose`."""
        self._body_freeze_epoch = current_gc_freeze_epoch()
        self._md_widget = VirtualizedMarkdown(self.text)
        self._body_parse = None
        # The body starts from the current text, so a queued sync has nothing to hand it.
        self._body_stale = False
        return self._md_widget

    def _refresh_header(self) -> None:
        """Show the header as the message's state now reads (collapsed, timestamp)."""

    def _on_final(self) -> None:
        """A ``stream_update()`` made the text final and handed it to the body, whose parse may still be running.

        Not called for a message built with its final text.
        """

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Message content is copied by its header/body children."""
        return None

    def render_lines(self, crop: Region) -> list[Strip]:
        """Render the repeated message chrome without per-row style work.

        The message only paints a left border, padding, and transparent
        background; child widgets render the header/body text. Every row of
        that chrome is identical, so a clipped parent scroll can reuse one
        strip instead of asking Textual's generic style renderer to rebuild
        it for each visible markdown row. Only its gap, when it shows one,
        draws a guide of its own above them.
        """
        if not self.is_attached:
            return [Strip.blank(crop.width) for _ in crop.line_range]

        if crop.width <= 0 or crop.height <= 0:
            return []
        if not self._can_reuse_chrome_strip():
            return super().render_lines(crop)

        gap = FOLD_GAP_ROWS if self.gap_above else 0
        cache_key = (
            crop.x,
            crop.width,
            self.size.width,
            self.app.theme,
            str(self.app.console.color_system),
            # Private Textual cache key: this fast path is intentionally tied
            # to Textual's current style invalidation model and covered by TUI
            # render-cache tests.
            self.styles._cache_key,
            gap,
        )
        cached = self._background_strip_cache
        if cached is None or cache_key != self._background_strip_cache_key:
            cached = Widget.render_lines(self, Region(crop.x, gap, crop.width, 1))[0]
            self._background_strip_cache = cached
            self._background_strip_cache_key = cache_key
        lines = [cached] * crop.height
        if crop.y < gap:
            gap_lines = Widget.render_lines(self, Region(crop.x, crop.y, crop.width, min(gap, crop.bottom) - crop.y))
            lines[: len(gap_lines)] = gap_lines
        return lines

    def _can_reuse_chrome_strip(self) -> bool:
        """Return true when every message chrome row is visually identical."""
        styles = self.styles
        if styles.gutter.height or self._border_title is not None or self._border_subtitle is not None:
            return False
        if any(edge for edge, _color in styles.outline):
            return False
        if styles.has_rule("hatch") and styles.hatch != "none":
            return False
        if styles.tint.a or styles.background_tint.a:
            return False
        if styles.text_opacity != 1.0 or self.opacity != 1.0:
            return False
        if styles.line_pad:
            return False
        return styles.text_align in {"start", "left"}

    def notify_style_update(self) -> None:
        self._background_strip_cache_key = None
        self._background_strip_cache = None
        super().notify_style_update()

    def watch_collapsed(self, collapsed: bool) -> None:
        if collapsed:
            note_fold(self)
        body = self._md_widget
        if body is not None:
            # A body kept folded and not handed the newest text shows once it shows that text (_sync_folded_body):
            # shown at once, it would show the text it had for a frame and jump to the newest one's rows in the next.
            body.display = not collapsed and not (self._builds_body_on_opening() and body.shown_source != self.text)
        self._refresh_header()
        if self._builds_body_on_opening() and (not collapsed or self._releases_body_on_folding()):
            # Once it is mounted: a message folded before then has no body to build or release yet.
            self.call_later(self._start_folded_body_sync)

    def _start_folded_body_sync(self) -> None:
        """Run :meth:`_sync_folded_body` beside this message's pump, not on it.

        It waits for the body's parse as the body mounts or takes the newest text: on the pump, the messages the
        message handles would wait too, its removal among them.
        """
        self.run_worker(self._sync_folded_body, group="folded-body")

    def stream_update(self, text: str, is_final: bool = False) -> None:
        """Update the message text (for streaming).

        A non-final update only records the newest text: the markdown body
        catches up at most every ``_STREAMED_BODY_INTERVAL_SECONDS``, so a
        stream of deltas costs one markdown update per interval instead of one
        per delta, while ``text`` always reads the newest text. A final update
        hands the body its text at once, before the style and :meth:`_on_final` change.
        """
        self._source_text = text
        self._body_stale = True
        self._is_final = is_final
        if is_final:
            self._sync_body()
            self.remove_class("--streaming")
            self._on_final()
        elif not self._body_sync_scheduled and self._body_takes_text():
            delay = self._body_synced_at + _STREAMED_BODY_INTERVAL_SECONDS - time.monotonic()
            if delay > 0:
                self.set_timer(delay, self._run_scheduled_body_sync)
                self._body_sync_scheduled = True
                return
            self._body_sync_scheduled = self.call_later(self._run_scheduled_body_sync)
            if not self._body_sync_scheduled:
                # The message pump is closing and refused the callback.
                self._sync_body()

    def append_text(self, text: str) -> None:
        """Stream *text* onto the end of this message."""
        self.stream_update(self._source_text + text)

    def _run_scheduled_body_sync(self) -> None:
        self._body_sync_scheduled = False
        self._sync_body()

    def _body_takes_text(self) -> bool:
        """Whether there is a body to hand new text: a folded one that waits for the message to open is handed none."""
        return self._md_widget is not None and not (self.collapsed and self._builds_body_on_opening())

    def _sync_body(self) -> None:
        """Hand the markdown body the current text if it has not seen it yet."""
        body = self._md_widget
        if not self._body_stale or body is None or not self._body_takes_text():
            return
        self._body_stale = False
        self._body_synced_at = time.monotonic()
        self._body_parse = body.update(self.text)

    async def _sync_folded_body(self) -> None:
        """Bring the body to the latest requested open state: release it folded, if it goes; build or update it open.

        A body opened before it shows the newest text, one it builds included, stays hidden until the parse of that
        text installs, or fails and leaves the text it showed.
        """
        async with self._body_structure_lock:
            if not self.is_mounted or self._closing or self._pruning or not self.app.is_running:
                return
            body = self._md_widget
            if self.collapsed and body is not None and self._releases_body_on_folding():
                epoch = self._body_freeze_epoch
                removed = (
                    GcRemovedContent.watch(built_at_epoch=epoch, nodes=body.walk_children(with_self=True))
                    if epoch is not None and epoch == current_gc_freeze_epoch()
                    else None
                )
                self._absorb_after_parse = False
                with self.app.batch_update():
                    if self.app.focused in body.walk_children(with_self=True):
                        self.screen.set_focus(None, scroll_visible=False)
                    try:
                        await remove_shielded(body)
                    finally:
                        self._md_widget = None
                        self._body_parse = None
                        self._body_freeze_epoch = None
                        self._clear_arrangement_cache()
                        _ = self.displayed_children
                self.post_message(
                    GcReclaimRequested(GcReclaimReason.STABLE_CONTENT_REMOVED, prompt=False, removed=removed)
                )
            if self.collapsed or self._closing or self._pruning:
                return
            body = self._md_widget
            if body is None:
                # Built empty and handed the text once mounted: built with it, its mount would wait for the text's
                # parse, and so would its removal. Until then it is no body to hand streamed text to.
                epoch = current_gc_freeze_epoch()
                body = VirtualizedMarkdown("")
                body.display = False
                await self.mount(body)
                self._md_widget = body
                self._body_freeze_epoch = epoch
                self._body_stale = True
                if self.collapsed or self._closing or self._pruning:
                    return
            elif body.display or body.shown_source == self.text:
                body.display = True
                if body.source == self.text:
                    # Handed the newest text already: handing it again would parse it again for nothing.
                    self._body_stale = False
                else:
                    # The text it was not handed while folded; its parse of final text is stable content, as below.
                    self._absorb_after_parse = self._is_final
                    self._sync_body()
                return
            self._body_stale = False
            # Built or kept, its parse is stable content the GC freeze can take; text that still streams is not.
            self._absorb_after_parse = self._is_final
            if body.source == self.text and self._body_parse is not None and not self._body_parse.is_done:
                # Handed the newest text already, as it streamed: that parse's end says when the body shows it. Handed
                # again, the body would parse it again and replace the blocks the GC absorb went to. A parse that
                # ended without showing it failed: the text is handed again.
                installed = self._body_parse
            else:
                self._body_synced_at = time.monotonic()
                installed = self._body_parse = body.update(self.text)
        try:
            await installed
            # Its final text, handed as that parse ran (a final update is handed at once), is the text it shows with.
            while (
                self._is_final
                and self._body_parse is not None
                and self._body_parse is not installed
                and self._md_widget is body
                and not self.collapsed
            ):
                installed = self._body_parse
                await installed
        except Exception:
            # It shows the text it had: better than none.
            logger.warning("Parsing an opened message's text failed", exc_info=True)
        if self._md_widget is body and not self.collapsed:
            body.display = True

    def on_virtualized_markdown_table_of_contents_updated(
        self, event: VirtualizedMarkdown.TableOfContentsUpdated
    ) -> None:
        """The body installed a parse, as it announces after each: one of the text handed on opening is stable content."""
        if self._absorb_after_parse and event.markdown is self._md_widget and event.markdown.shown_source == self.text:
            self._absorb_after_parse = False
            self.post_message(GcAbsorbRequested(GcAbsorbReason.STABLE_CONTENT_MOUNTED))

    def _content_rows(self, container: Size, viewport: Size, width: int) -> int:
        """Rows it gives up go in the layout that measures it, as a fold's do.

        Until its closing markup arrives, a ``**`` or a link's URL can wrap a
        line that then fits again. Noted as a fold, the shrink shows in the
        panel's arrangement measuring it; a view that follows the bottom would
        otherwise hold it a frame, in case the rows came back, and show the row
        blank under the transcript's last line. The gap below it is not its
        text's: it goes as an entry comes below it, whose rows the view then
        follows down.
        """
        height = super()._content_rows(container, viewport, width)
        measured = self._measured_rows
        self._measured_rows = (width, height)
        if measured is not None and measured[0] == width and height < measured[1]:
            note_fold(self)
        return height

    def _size_updated(self, size: Size, virtual_size: Size, container_size: Size, layout: bool = True) -> bool:
        # Textual sets a changed virtual size as a reactive that asks for another
        # layout, but the layout calling this has placed the message already and a
        # message shows no scrollbar: each row streamed text adds would lay the
        # whole transcript out twice.
        return super()._size_updated(size, virtual_size, container_size, layout=False)


class AgentMessage(StreamedMarkdownMessage):
    """Agent message with markdown rendering and streaming support.

    Uses VirtualizedMarkdown for rich rendering of agent responses including
    code blocks, tables, lists, and other markdown elements.
    Click the header to collapse/expand the message body.
    """

    DEFAULT_CSS = """
    AgentMessage {
        margin: 1 0;
        padding: 0 0 0 1;
        border-left: thick $tui-border-agent-message $border-opacity;
        height: auto;
    }
    AgentMessage.--intermediate, AgentMessage.--streaming {
        border-left: thick $tui-border-neutral-gray $border-opacity;
        color: $text-muted;
    }
    /* Text between tool calls, as thinking and tool groups: its guide meets theirs, a blank row below it. While it
       streams, it keeps an answer's spacing, so an answer it turns out to be does not move as it ends. */
    AgentMessage.--intermediate {
        margin: 0;
        border-left: solid $tui-border-neutral-gray $border-opacity;
    }
    AgentMessage.--intermediate > VirtualizedMarkdown, AgentMessage.--streaming > VirtualizedMarkdown {
        color: $text-muted;
    }
    /* Open, as an open thinking body: its own guide under the header's fold arrow, its text two columns in from
       the label, and a blank row under it, as under an open tool group's calls. The guide is the body's, so the
       message keeps no vertical gutter and its chrome rows stay one shared strip. An answer's text stays under
       its arrow. */
    AgentMessage.--intermediate > VirtualizedMarkdown {
        padding: 0 0 1 3;
        border-left: solid $tui-border-neutral-gray $border-opacity;
    }
    /* Each kind of fold row its own colour: open, the label is the agent's green; folded, its words read bright,
       as the agent's own, among muted thinking and tool rows. */
    AgentMessage .fold-row-header > .fold-row-header--label {
        color: $text-success;
    }
    AgentMessage .fold-row-header > .fold-row-header--preview {
        color: $text;
    }
    AgentMessage.--compressed {
        border-left: thick $tui-border-neutral-100 $border-opacity;
        color: $text-muted;
    }
    AgentMessage.--compressed > VirtualizedMarkdown {
        color: $text-muted;
    }
    AgentMessage.--structured-completion > VirtualizedMarkdown {
        color: $success;
    }
    AgentMessage > VirtualizedMarkdown {
        padding: 0;
        height: auto;
        min-height: 1;
        background: transparent;
        /* Sized by its text, the body never scrolls. While streamed text grows
           past the height of the last arrange, a scrollbar would narrow it for
           a frame and shift its code blocks' right edge. */
        overflow-y: hidden;
    }
    """

    @property
    def is_process_final(self) -> bool:
        """A visible answer to keep outside a successful turn's process."""
        return (
            self._is_final
            and not self._is_intermediate
            and not self._is_structured_completion
            and not self.is_status
            and bool(self.text)
        )

    def __init__(
        self,
        text: str = "",
        is_final: bool = True,
        profile_name: str = "",
        is_intermediate: bool = False,
        is_compressed: bool = False,
        is_structured_completion: bool = False,
        timestamp: str = "",
        duration_ms: int | None = None,
        segment_ids: Iterable[str] = (),
        is_status: bool = False,
        collapsed: bool = False,
    ) -> None:
        self.is_status = is_status
        """Persisted lifecycle prose is not an agent's successful final answer."""
        self.segment_ids = frozenset(segment_ids)
        """The content segments whose answer text this message shows, for a bare Continue to withdraw."""
        self._profile_name = profile_name
        self._is_intermediate = is_intermediate
        self._is_compressed = is_compressed
        self._is_structured_completion = is_structured_completion
        self._ts = timestamp
        self._duration_ms = duration_ms
        cls = ""
        if is_intermediate:
            cls = "--intermediate"
        elif is_compressed:
            cls = "--compressed"
        elif is_structured_completion:
            cls = "--structured-completion"
        if not is_final:
            # Muted until the text is known to be the answer, not text before tool calls.
            cls = f"{cls} --streaming".strip()
        super().__init__(text, is_final=is_final, classes=cls)
        if collapsed and self.foldable:
            # Built complete between tool calls, as the main chat's replay builds it: folded to its row, as replayed
            # thinking. Before mount, and without the watcher.
            self.set_reactive(StreamedMarkdownMessage.collapsed, True)

    @property
    def foldable(self) -> bool:
        """Text between tool calls folds once complete; an answer never folds with its turn."""
        return self._is_final and self._is_intermediate

    def takes_fold_gap(self) -> bool:
        # An answer keeps its margins instead.
        return self._is_intermediate

    def ends_folded(self) -> bool:
        return self._is_intermediate and not self._shows_body()

    def _builds_body_on_opening(self) -> bool:
        # Text between tool calls, as thinking; an answer folded by hand keeps its body up to date. Folded as the turn
        # goes on past it, the body it streamed in stays: removing it would lay the transcript out once more.
        return self._is_intermediate

    @property
    def profile_name(self) -> str:
        """The name of the agent that wrote this message; empty when none was given."""
        return self._profile_name

    def _header_text(self) -> Text:
        label = self._copy_label()
        arrow = FOLDED_ARROW if self.collapsed else OPEN_ARROW
        t = Text.assemble((f"{arrow} {label}", "bold green"))
        if self._ts:
            t.append(f" {self._ts}", style="dim")
        if self._duration_ms is not None:
            t.append(f" ({_format_duration_ms(self._duration_ms)})", style="dim")
        return t

    def _copy_label(self) -> str:
        return self._profile_name or self._render_message(AGENT_LABEL.bind())

    def _render_message(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)

    def compose(self) -> ComposeResult:
        with MessageHeaderRow():
            yield _AgentReplyHeader(
                self._copy_label(),
                None if self._is_intermediate else self._header_text(),
                collapsed=self.collapsed,
                text=self._source_text,
            )
            copy_button = MessageCopyButton(
                tooltip=self._render_message(_AGENT_RESPONSE_COPY_TOOLTIP.bind()),
                text=self._render_message(COPY_MESSAGE_BUTTON.bind()),
            )
            copy_button.display = self._should_show_copy_button()
            yield copy_button
        if not (self.collapsed and self._builds_body_on_opening()):
            body = self._new_body()
            body.display = not self.collapsed
            yield body

    def _refresh_header(self) -> None:
        with contextlib.suppress(NoMatches):
            header = self.query_one(_AgentReplyHeader)
            if self._is_intermediate:
                header.show(collapsed=self.collapsed, text=self._source_text)
            else:
                header.show_reply(self._header_text())

    def _on_final(self) -> None:
        self._show_copy_button_if_ready()

    def on__agent_header_clicked(self) -> None:
        if self._is_final:
            self.collapsed = not self.collapsed

    def set_timestamp(self, timestamp: str, *, duration_ms: int | None = None) -> None:
        """Update the header timestamp, and how long the turn took to this answer (*duration_ms*) when given."""
        timestamp = timestamp or self._ts
        duration_ms = self._duration_ms if duration_ms is None else duration_ms
        if (timestamp, duration_ms) == (self._ts, self._duration_ms):
            return
        self._ts = timestamp
        self._duration_ms = duration_ms
        self._refresh_header()

    def _should_show_copy_button(self) -> bool:
        """Return true when this widget represents a final raw agent response."""
        return (
            self._is_final
            and not self._is_intermediate
            and not self._is_compressed
            and not self._is_structured_completion
            and bool(self.format_agent_response_copy().strip())
        )

    def _show_copy_button_if_ready(self) -> None:
        if not self._should_show_copy_button():
            return
        with contextlib.suppress(Exception):
            self.query_one(MessageCopyButton).display = True

    def format_agent_response_copy(self) -> str:
        """Return the raw response text copied by the inline button."""
        return self.text

    def copy_agent_response(self) -> None:
        """Copy this finalized agent response to the available clipboards."""
        payload = self.format_agent_response_copy()
        if not payload.strip():
            return
        copy_text_to_clipboards(self.app, payload)
        localizer = widget_localizer(self)
        self.notify(
            render_str(localizer, _AGENT_RESPONSE_COPIED.bind()),
            title=render_str(localizer, COPIED_TITLE.bind()),
            timeout=2,
            markup=False,
        )

    def on_message_copy_button_clicked(self, event: MessageCopyButton.Clicked) -> None:
        """Handle clicks from the finalized-response copy affordance."""
        event.stop()
        if not self._should_show_copy_button():
            return
        self.copy_agent_response()

    def stream_update(
        self, text: str, is_final: bool = False, *, timestamp: str = "", duration_ms: int | None = None
    ) -> None:
        """Update the message text (for streaming); a *timestamp* and *duration_ms* go in the header first."""
        self.set_timestamp(timestamp, duration_ms=duration_ms)
        super().stream_update(text, is_final)

    def settle_intermediate(self, *, folded: bool = False) -> None:
        """End streaming as text the agent wrote between tool calls: muted, under a fold row header, no copy button.

        *folded*, it folds to its row first: its body parses the final text only if it opens.
        """
        self._is_intermediate = True
        self.add_class("--intermediate")
        if folded:
            self.collapsed = True
        self.stream_update(self._source_text, is_final=True)
        self._refresh_header()


class ThinkingMessage(StreamedMarkdownMessage):
    """The model's readable thinking, muted, above what it led to.

    It streams like an answer, open or folded to its row as it started, and
    keeps that once complete; its header opens or folds it at any time. Folded
    as it streams, its row runs the newest of the text. A replayed one starts
    folded, and a folded one builds its body the first time it opens. It is
    not part of the answer: it has no copy button, and copying the agent's
    responses leaves it out.
    """

    DEFAULT_CSS = """
    /* No vertical margin: folded rows sit one under another, their thin guides one line. */
    ThinkingMessage {
        margin: 0;
        padding: 0 0 0 1;
        border-left: solid $tui-border-neutral-gray $border-opacity;
        height: auto;
        color: $text-muted;
    }
    ThinkingMessage.--compressed, ThinkingMessage.--compressed > VirtualizedMarkdown {
        border-left: solid $tui-border-neutral-100 $border-opacity;
    }
    /* Each kind of fold row its own colour: thinking's label is lavender, its text stays muted. */
    ThinkingMessage .fold-row-header > .fold-row-header--label {
        color: $text-primary;
    }
    /* Open: its own guide under the header's fold arrow, its text two columns in from the label, and a blank row
       under it, as under an open tool group's calls. Folded rows keep none between them. */
    ThinkingMessage > VirtualizedMarkdown {
        padding: 0 0 1 3;
        border-left: solid $tui-border-neutral-gray $border-opacity;
        height: auto;
        min-height: 1;
        background: transparent;
        color: $text-muted;
        /* As an answer's body: sized by its text, it never scrolls. */
        overflow-y: hidden;
    }
    """

    def __init__(
        self,
        text: str = "",
        *,
        channel: ContentChannel = "reasoning",
        segment_id: str = "",
        is_final: bool = True,
        collapsed: bool = False,
        is_compressed: bool = False,
    ) -> None:
        self.segment_id = segment_id
        """The content segment this message shows, for a retry that gives up its response to remove."""
        self._channel = channel
        self._header: _FoldRowHeader | None = None
        super().__init__(text, is_final=is_final, classes="--compressed" if is_compressed else "")
        if collapsed:
            # Before mount, and without the watcher: the body is built when it first opens.
            self.set_reactive(StreamedMarkdownMessage.collapsed, True)

    @property
    def foldable(self) -> bool:
        """Whether it can fold now: once its text is complete."""
        return self._is_final

    def takes_fold_gap(self) -> bool:
        return True

    def ends_folded(self) -> bool:
        return not self._shows_body()

    def _label_ref(self) -> MessageRef:
        if self._channel == "reasoning_summary":
            return _THINKING_SUMMARY_LABEL.bind()
        return (THOUGHT_LABEL if self._is_final else THINKING_LABEL).bind()

    def compose(self) -> ComposeResult:
        with MessageHeaderRow():
            self._header = _FoldRowHeader(
                self._label_ref(), collapsed=self.collapsed, text=self._source_text, streaming=not self._is_final
            )
            yield self._header
        if not self.collapsed:
            yield self._new_body()

    def _builds_body_on_opening(self) -> bool:
        return True

    def _releases_body_on_folding(self) -> bool:
        return True

    def stream_update(self, text: str, is_final: bool = False) -> None:
        super().stream_update(text, is_final)
        if self.collapsed and not is_final:
            # Its row runs the newest text; a final update shows the row's opening (_on_final).
            self._refresh_header()

    def _refresh_header(self) -> None:
        if self._header is not None:
            self._header.show(
                collapsed=self.collapsed,
                text=self._source_text,
                label=self._label_ref(),
                streaming=not self._is_final,
            )

    def _on_final(self) -> None:
        # Thinking that streamed is now thought.
        self._refresh_header()

    def on__agent_header_clicked(self) -> None:
        self.collapsed = not self.collapsed


def folds_with_turn(widget: Widget) -> TypeGuard[ThinkingMessage | AgentMessage]:
    """Whether a turn that folds folds *widget* to its one-row header: thinking, and complete text between tool calls."""
    return isinstance(widget, ThinkingMessage) or (isinstance(widget, AgentMessage) and widget.foldable)


async def remove_withdrawn_answer(container: Widget, segment_ids: Iterable[str]) -> None:
    """Remove *container*'s agent messages that show any of *segment_ids*: an answer a retry asks for again."""
    withdrawn = frozenset(segment_ids)
    await remove_all_shielded(
        child for child in container.children if isinstance(child, AgentMessage) and child.segment_ids & withdrawn
    )


class SystemMessage(Static):
    """System notification — dim, unobtrusive."""

    DEFAULT_CSS = """
    SystemMessage {
        margin: 1 0;
        padding: 0 1;
        color: $text-muted;
        height: auto;
    }
    SystemMessage.-warning {
        color: $warning;
    }
    """

    def __init__(self, text: str, *, warning: bool = False) -> None:
        super().__init__(Text(f"  ◦ {text}"))
        self.set_class(warning, "-warning")


class ConversationStatusAction:
    """Messages emitted by inline failed/interrupted conversation actions."""

    class Pressed(Message):
        """Posted when the inline retry/continue action is clicked."""


class ErrorMessage(ProcessMember):
    """Error display with red accent."""

    DEFAULT_CSS = """
    ErrorMessage {
        margin: 1 0;
        padding: 0 1 0 2;
        border-left: thick $tui-border-status-error $border-opacity;
        height: auto;
    }
    ErrorMessage > .status-body {
        height: auto;
        color: $error;
    }
    ErrorMessage > .status-action-row {
        height: 1;
        margin: 1 0 0 0;
    }
    ErrorMessage > .status-action-row > Button {
        height: 1;
        min-width: 10;
        border: none;
        background: $error;
        color: $button-color-foreground;
        text-style: bold;
    }
    ErrorMessage > .status-action-row > Button:hover,
    ErrorMessage > .status-action-row > Button:focus,
    ErrorMessage > .status-action-row > Button.-active {
        background: $foreground;
        color: $background;
        text-style: bold;
    }
    """

    def __init__(self, text: str, *, action_label: str | None = None) -> None:
        self._text = text
        self._action_label = action_label
        super().__init__()

    def _render_text(self) -> Text:
        t = Text()
        t.append(render_str(widget_localizer(self), INTERRUPTED_ERROR_HEADER_MESSAGE.bind()), style="bold")
        t.append("\n")
        t.append(self._text)
        return t

    def compose(self) -> ComposeResult:
        yield Static(self._render_text(), classes="status-body")
        if self._action_label is not None:
            with Horizontal(classes="status-action-row"):
                yield Button(self._action_label, compact=True, classes="status-action-btn")

    def on_mount(self) -> None:
        self._size_action_button()

    def _size_action_button(self) -> None:
        if self._action_label is None:
            return
        button = self.query_one(".status-action-btn", Button)
        width = len(self._action_label) + 4
        button.styles.width = width
        button.styles.min_width = width

    def hide_action(self) -> None:
        """Hide the inline retry action after it has been used."""
        for row in self.query(".status-action-row"):
            row.display = False

    def set_action_disabled(self, disabled: bool) -> None:
        """Prevent repeat clicks while the retry is being admitted."""
        for button in self.query(".status-action-btn").results(Button):
            button.disabled = disabled

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not event.button.has_class("status-action-btn"):
            return
        event.stop()
        if event.button.disabled:
            return
        self.post_message(ConversationStatusAction.Pressed())


class RetryMessage(Static, ProcessMember):
    """Transient retry notice — shown while the executor waits before retrying."""

    DEFAULT_CSS = """
    RetryMessage {
        margin: 1 0;
        padding: 0 1 0 2;
        border-left: thick $tui-border-status-error $border-opacity;
        height: auto;
        color: $error;
    }
    """

    def __init__(self, message: str, attempt: int, max_attempts: int, delay_seconds: int) -> None:
        self._message = message
        self._attempt = attempt
        self._max_attempts = max_attempts
        self._delay = delay_seconds
        super().__init__()

    def render(self) -> Text:
        t = Text()
        t.append(self._render_message(INTERRUPTED_ERROR_HEADER_MESSAGE.bind()), style="bold")
        t.append("\n")
        t.append(
            self._render_message(
                _RETRY_MESSAGE.bind(
                    message=DisplayBlock(self._message),
                    delay_seconds=self._delay,
                    attempt=self._attempt,
                    max_attempts=self._max_attempts,
                )
            ),
        )
        return t

    def _render_message(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)


class InterruptedMessage(ProcessMember):
    """Interruption notice — yellow for user interrupts, red for errors."""

    DEFAULT_CSS = """
    InterruptedMessage {
        margin: 1 0;
        padding: 0 1 0 2;
        border-left: thick $tui-border-status-warning $border-opacity;
        height: auto;
    }
    InterruptedMessage.-error {
        border-left: thick $tui-border-status-error $border-opacity;
    }
    InterruptedMessage.-error > .status-action-row > Button {
        background: $error;
    }
    InterruptedMessage > .status-body {
        height: auto;
        color: $warning;
    }
    InterruptedMessage.-error > .status-body {
        color: $error;
    }
    InterruptedMessage > .status-action-row {
        height: 1;
        margin: 1 0 0 0;
    }
    InterruptedMessage > .status-action-row > Button {
        height: 1;
        min-width: 10;
        border: none;
        background: $warning;
        color: $button-color-foreground;
        text-style: bold;
    }
    InterruptedMessage > .status-action-row > Button:hover,
    InterruptedMessage > .status-action-row > Button:focus,
    InterruptedMessage > .status-action-row > Button.-active {
        background: $foreground;
        color: $background;
        text-style: bold;
    }
    """

    def __init__(
        self,
        reason: str = "Execution interrupted",
        source: str = "user",
        *,
        action_label: str | None = None,
        header: str | None = None,
    ) -> None:
        if header is None:
            copy = resolve_interrupted_message_copy(reason, source)
            header = copy.header
            reason = copy.reason
        self._header = header
        self._reason = reason
        self._source = source
        self._action_label = action_label
        super().__init__()
        if source == "error":
            self.add_class("-error")

    def _render_text(self) -> Text:
        t = Text()
        t.append(self._header, style="bold")
        t.append("\n")
        t.append(self._reason)
        return t

    def compose(self) -> ComposeResult:
        yield Static(self._render_text(), classes="status-body")
        if self._action_label is not None:
            with Horizontal(classes="status-action-row"):
                yield Button(self._action_label, compact=True, classes="status-action-btn")

    def on_mount(self) -> None:
        self._size_action_button()

    def _size_action_button(self) -> None:
        if self._action_label is None:
            return
        button = self.query_one(".status-action-btn", Button)
        width = Text(self._action_label).cell_len + 4
        button.styles.width = width
        button.styles.min_width = width

    def hide_action(self) -> None:
        """Hide the inline retry/continue action after it has been used."""
        for row in self.query(".status-action-row"):
            row.display = False

    def set_action_disabled(self, disabled: bool) -> None:
        """Prevent repeat clicks while the retry is being admitted."""
        for button in self.query(".status-action-btn").results(Button):
            button.disabled = disabled

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if not event.button.has_class("status-action-btn"):
            return
        event.stop()
        if event.button.disabled:
            return
        self.post_message(ConversationStatusAction.Pressed())


class ThinkingIndicator(Widget):
    """Loading indicator with label text."""

    DEFAULT_CSS = """
    ThinkingIndicator {
        margin: 1 0;
        padding: 0 1;
        height: 1;
        layout: horizontal;
    }
    ThinkingIndicator > LoadingIndicator {
        width: 5;
        height: 1;
        color: $accent;
    }
    ThinkingIndicator > .thinking-label {
        width: auto;
        height: 1;
        color: $accent;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

    def compose(self) -> ComposeResult:
        from textual.widgets import LoadingIndicator

        yield LoadingIndicator()
        yield Static("thinking", classes="thinking-label", id="thinking-label")

    def update_label(self, label: str) -> None:
        """Change the displayed text."""
        import contextlib

        with contextlib.suppress(Exception):
            self.query_one("#thinking-label", Static).update(Text(label))
