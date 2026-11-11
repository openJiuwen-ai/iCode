# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A single disposable side answer beneath the main chat, beside the full-height sidebar."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Static

from chrys.app.tui.i18n import render_str, render_text, widget_localizer
from chrys.app.tui.support.gc_freeze import (
    GcFreezeBlockReason,
    GcReclaimReason,
    GcReclaimRequested,
    raise_gc_freeze_hook_errors,
)
from chrys.app.tui.util.liveness import composed_live
from chrys.app.tui.util.removal import finish_shielded, remove_all_shielded
from chrys.app.tui.util.source_text import sanitize_source_text
from chrys.app.tui.widgets.chat.messages import AgentMessage, UserMessage
from chrys.app.tui.widgets.loading import RunningTitle
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.i18n import DisplayBlock, msg

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.geometry import Size
    from textual.widget import Widget

    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.models.side_questions import SideQuestionEntry

_CLOSE = msg("tui.btw.close", fallback="Close (Esc)")
_PREVIOUS = msg("tui.btw.previous", fallback="Previous side question")
_NEXT = msg("tui.btw.next", fallback="Next side question")
_EMPTY = msg("tui.btw.empty", fallback="No side questions yet. Use /btw <question> to ask one.")
_READING = msg("tui.btw.reading", fallback="Loading side questions...")
_READ_FAILED = msg("tui.btw.read_failed", fallback="Side question history could not be read. Your saved file was kept.")
_UNAVAILABLE = msg(
    "tui.btw.unavailable",
    fallback="Side questions are unavailable while the session is changing. Try again when it is ready.",
)
_BLOCKED = msg("tui.btw.blocked", fallback="The side question was blocked by a hook.\n{reason}", multiline=True)
_SAVE_FAILED = msg(
    "tui.btw.save_failed",
    fallback="This side answer could not be saved. It stays available until you leave this session.",
)
_TITLE = msg("tui.btw.title", fallback="Side Questions")
_THINKING = msg("tui.btw.thinking", fallback="Thinking...")
_FAILED = msg("tui.btw.failed", fallback="The side question could not finish. Close it and ask again.")
_QUESTION = msg("tui.btw.question", fallback="Question")
_ANSWER = msg("tui.btw.answer", fallback="Answer")
_log = logging.getLogger(__name__)


class ChatColumn(Vertical):
    """The chat and its side question share a column; the sidebar remains its sibling."""

    DEFAULT_CSS = """
    ChatColumn { width: 1fr; height: 1fr; min-width: 35; }
    """


class BtwButton(Button):
    """Card actions keep the composer's focus when clicked."""

    FOCUS_ON_CLICK = False


class BtwPage(VerticalScroll, can_focus_children=False):
    """The question and its answer, scrolled together; clicks leave typing directed at the composer.

    Tab stops at the page, to scroll it, and moves on to the card's controls: its text is not a stop of its own.
    """

    FOCUS_ON_CLICK = False

    def __init__(self) -> None:
        super().__init__()
        self.content_generation = 0
        self._reveal_target: Widget | None = None
        self._reveal_row: Widget | None = None
        self._reveal_scroll_y = 0.0

    def replace_content(self) -> None:
        """Retire selections and positioning belonging to the previous record."""
        self.content_generation += 1
        self._reveal_target = self._reveal_row = None
        self.clear_selection_in([self])

    def clear_selection_in(self, roots: list[Widget]) -> None:
        """Clear the screen's selection if it reaches into any of *roots*: their text is about to go."""
        if any(root in widget.ancestors_with_self for widget in self.screen.selections for root in roots):
            self.screen.clear_selection()

    def start_at_top(self) -> None:
        """Reset positioning only when the replacement is ready to mount."""
        self._reveal_scroll_y = 0
        self.scroll_to(y=0, animate=False, immediate=True)

    def reveal(self, target: Widget, row: Widget | None = None) -> None:
        """Bring the output that just arrived into view, and keep it there as the question reflows.

        What must show is the first row of *row* (*target* itself by default): an answer's text, not just its
        header. As the chat pulls to an answer or error that lands after its reader scrolled up, it brings this
        one in once; scrolling away afterwards ends the following.
        """
        self._reveal_target = target
        self._reveal_row = row or target
        self.call_later(self._reveal_output)

    def on_resize(self) -> None:
        self.call_later(self._reveal_output)

    def watch_virtual_size(self, _old: Size, _new: Size) -> None:
        self.call_later(self._reveal_output)

    def _reveal_output(self) -> None:
        target, row = self._reveal_target, self._reveal_row
        if not composed_live(self) or target is None or row is None or not row.is_attached or not target.display:
            return
        top = target.virtual_region
        if not top.width or not row.region.width:
            # Just shown, not laid out yet: its place reads as the page's top. Look again once it is.
            if self.region.width:
                self.call_after_refresh(self._reveal_output)
            return
        first = top.y + row.region.y - target.region.y
        height = self.scrollable_content_region.height
        if not self.scroll_y <= first < self.scroll_y + height:
            # Its first row in view, under the output's header when both fit.
            self._reveal_scroll_y = min(max(top.y, first - height + 1), self.max_scroll_y)
            self.scroll_to(y=self._reveal_scroll_y, animate=False, immediate=True)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if new_value != min(self._reveal_scroll_y, self.max_scroll_y):
            self._reveal_target = None
        self.post_message(BtwPanel.Scrolled())


class BtwQuestion(UserMessage):
    """The question, shown as the main chat shows what the user sent, under a plain label."""

    def _copy_label(self) -> str:
        return render_str(widget_localizer(self), _QUESTION.bind())

    def _header_text(self) -> Text:
        return Text.assemble((self._copy_label(), "bold cyan"))


class BtwAnswer(AgentMessage):
    """The answer, shown as the main chat shows a reply; the card's one answer never folds away."""

    def _copy_label(self) -> str:
        return render_str(widget_localizer(self), _ANSWER.bind())

    def _header_text(self) -> Text:
        # Styled as the chat's reply header styles its label, only without the fold arrow.
        return Text.assemble((self._copy_label(), "bold green"))

    def __init__(self) -> None:
        super().__init__()
        self._written = ""
        """The answer as the model wrote it: the page shows it made safe for the terminal."""

    def validate_collapsed(self, collapsed: bool) -> bool:
        return False

    def format_agent_response_copy(self) -> str:
        # Copied as written, tabs and all, as the chat copies its replies.
        return self._written

    async def show_text(self, text: str) -> None:
        """Show *text*; a parse that fails raises here, where the card can say so, not in the app."""
        self._written = text
        self.stream_update(sanitize_source_text(text), is_final=True)
        if self._body_parse is not None:
            await self._body_parse


class BtwThinking(RunningTitle, Static):
    """The existing gated thinking animation, parked while the card is hidden."""

    SHIMMER_COMPONENT = "btw-thinking--lit"
    COMPONENT_CLASSES: ClassVar[set[str]] = {SHIMMER_COMPONENT}
    DEFAULT_CSS = """
    BtwThinking { height: 1; color: $text-muted; }
    BtwThinking > .btw-thinking--lit { color: $text; }
    """

    def render(self) -> Text:
        return self.running_title(render_text(widget_localizer(self), _THINKING.bind()))


_ROOMY_HEIGHT = 4
"""Rows a card needs for its border, the blank row under its title and one row of content."""


class BtwPanel(Vertical):
    """One side question and its answer on a bounded card, with history controls and an always-visible close."""

    DEFAULT_CSS = """
    BtwPanel {
        height: 1fr;
        border: round $tui-border-accent $border-opacity;
        border-title-align: left;
        border-title-color: $accent;
        /* One blank row under the title, as between the question and the answer. */
        padding: 1 0 0 0;
        layers: page controls;
    }
    /* A card of a single row gives the blank row up, so the controls stay on it. */
    BtwPanel.-cramped { padding: 0; }
    /* Over the page's top-right corner, beside the question's header: the page keeps the card's first content row.
       Kept tight, to leave the header's label and copy action as much of the row as they can.
       The margin, not padding, keeps the page's scrollbar showing beside them. */
    BtwPanel #btw-history-controls { layer: controls; dock: right; width: auto; height: 1; margin: 0 1 0 0; }
    BtwPanel Button.btw-action {
        width: 3; min-width: 3; height: 1; padding: 0;
        border: none; background: transparent; color: $text-muted; text-style: none;
    }
    BtwPanel Button.btw-action:hover, BtwPanel Button.btw-action:focus {
        background: $foreground 8%; color: $accent; text-style: bold; text-opacity: 100%;
    }
    BtwPanel Button.btw-action:disabled { background: transparent; color: $foreground 25%; }
    BtwPanel #btw-close { background: $primary 20%; color: $text; text-style: bold; }
    BtwPanel #btw-close:hover, BtwPanel #btw-close:focus { background: $primary 35%; }
    BtwPanel #btw-position { width: auto; min-width: 3; height: 1; padding: 0; color: $text-muted; text-align: center; }
    BtwPanel BtwPage { height: 1fr; min-height: 1; padding: 0 0 0 1; overflow-x: hidden; scrollbar-size: 1 1; }
    /* The question opens the page, plain on the card, under the controls' row. */
    BtwPanel BtwQuestion, BtwPanel BtwQuestion:ansi { margin: 0 0 1 0; background: transparent; }
    BtwPanel BtwThinking { margin: 1 0 0 1; }
    BtwPanel #btw-error { height: auto; margin: 1 0 0 0; padding: 0 1; }
    """

    class Closed(Message):
        """The user closes this one question, even while its request is running."""

    class Navigate(Message):
        def __init__(self, delta: int) -> None:
            super().__init__()
            self.delta = delta

    class Scrolled(Message):
        """The page scrolled: a quote offer for its text no longer sits beside it."""

    def __init__(self, *, locale_controller: LocaleController | None = None) -> None:
        super().__init__()
        self._locale_controller = locale_controller
        self.request_id = ""
        self.running = False
        self.display = False
        self._notice = _FAILED.bind()
        self._content_task: asyncio.Task[None] | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="btw-history-controls"):
            yield BtwButton("‹", id="btw-previous", classes="btw-action", compact=True, disabled=True)  # noqa: RUF001 - navigation glyph
            yield Static("0/0", id="btw-position", markup=False)
            yield BtwButton("›", id="btw-next", classes="btw-action", compact=True, disabled=True)  # noqa: RUF001 - navigation glyph
            yield BtwButton("✕", id="btw-close", classes="btw-action", compact=True)
        with BtwPage():
            yield BtwThinking()
            yield Static(markup=False, id="btw-error")

    def gc_freeze_block_reason(self) -> GcFreezeBlockReason | None:
        return GcFreezeBlockReason.PARTICIPANT if self.running else None

    def prepare_for_gc_freeze(self) -> None:
        for markdown in self.query(VirtualizedMarkdown):
            markdown.prepare_for_gc_freeze()

    def after_gc_freeze(self) -> None:
        """Renew every body's render caches, then report every failure."""
        errors: list[Exception] = []
        for markdown in self.query(VirtualizedMarkdown):
            try:
                markdown.after_gc_freeze()
            except Exception as error:
                errors.append(error)
        raise_gc_freeze_hook_errors("Side question cache renewal failed", errors)

    def abort_gc_freeze(self) -> None:
        """Best-effort restore every body's detached render caches."""
        for markdown in self.query(VirtualizedMarkdown):
            try:
                markdown.abort_gc_freeze()
            except Exception:
                _log.exception("Failed to restore a detached side question cache")

    def on_mount(self) -> None:
        self.query_one(BtwThinking).display = False
        self.query_one("#btw-error").display = False
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)
        self.refresh_localization()

    def on_resize(self) -> None:
        # Measured with the border and the blank row, so giving the row up cannot undo itself.
        self.set_class(self.region.height < _ROOMY_HEIGHT, "-cramped")

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)
        # Leaving with the card: removals it started are shielded and still finish.
        if self._content_task is not None:
            self._content_task.cancel()

    def refresh_localization(self) -> None:
        if not self.children:
            return
        localizer = widget_localizer(self)
        self.border_title = render_str(localizer, _TITLE.bind())
        self.query_one("#btw-close", Button).tooltip = render_text(localizer, _CLOSE.bind())
        self.query_one("#btw-previous", Button).tooltip = render_text(localizer, _PREVIOUS.bind())
        self.query_one("#btw-next", Button).tooltip = render_text(localizer, _NEXT.bind())
        self._refresh_notice()
        self.query_one(BtwThinking).refresh()
        for message in (*self.query(BtwQuestion), *self.query(BtwAnswer)):
            message.refresh_labels()

    def _refresh_notice(self) -> None:
        self.query_one("#btw-error", Static).update(render_text(widget_localizer(self), self._notice))

    def _show(self, question: str | None, answer: str | None, request_id: str) -> asyncio.Task[None] | None:
        """Queue a replacement, or put the answer under the question already on this record.

        The card owns these tasks: cancelling an EventBus publisher cannot cancel DOM work.
        Only replacing a record invalidates earlier work, including a queued quote request.
        None when the card is leaving the screen: there is nothing left to show.
        """
        if not composed_live(self):
            return None
        page = self.query_one(BtwPage)
        if question is not None:
            page.replace_content()
        generation = page.content_generation
        previous = self._content_task

        def current() -> bool:
            return composed_live(self) and composed_live(page) and page.content_generation == generation

        async def present() -> None:
            reply: BtwAnswer | None = None
            try:
                if previous is not None:
                    await previous
                if not current():
                    return
                with self.app.batch_update():
                    # A record's own answer replaces any it showed, as a repeated completion does.
                    old = list(page.query_children("BtwQuestion, BtwAnswer" if question is not None else "BtwAnswer"))
                    # A replaced answer takes a selection in it along, as a new record does with the page's.
                    page.clear_selection_in(old)
                    if old:
                        await self._remove_messages(old)
                        if not current():
                            return
                    if question is not None:
                        page.start_at_top()
                    shown: list[Widget] = [BtwQuestion(question)] if question else []
                    # Parse only after mounting: failures belong to this card, not the App.
                    if answer is not None:
                        reply = BtwAnswer()
                        # Hidden until its text shows, as the card always showed an answer: whole or not at all.
                        reply.display = False
                        shown.append(reply)
                    if shown:
                        await page.mount(*shown, before=self.query_one(BtwThinking))
                        if not current():
                            return
                if reply is not None and answer is not None:
                    await reply.show_text(answer)
                    if not current():
                        return
                    self.running = False
                    self._stop_thinking()
                    reply.display = True
                    # An earlier completion of this record may have failed: this one stands.
                    self.query_one("#btw-error").display = False
                    page.reveal(reply, reply.query_one(VirtualizedMarkdown))
                elif self.query_one(BtwThinking).display:
                    page.reveal(self.query_one(BtwThinking))
                elif self.query_one("#btw-error").display:
                    page.reveal(self.query_one("#btw-error"))
            except asyncio.CancelledError:
                # Cancelled only as the card leaves: whoever waits on it just stops waiting.
                return
            except Exception:
                _log.warning("Could not render side question", exc_info=True)
                try:
                    if reply is not None:
                        await self._remove_messages([reply])
                        if not current():
                            return
                except Exception:
                    _log.exception("Could not remove side question answer")
                finally:
                    if current():
                        self.fail(request_id)

        self._content_task = asyncio.create_task(present())
        return self._content_task

    async def _remove_messages(self, messages: list[Widget]) -> None:
        await remove_all_shielded(messages)
        if composed_live(self) and messages:
            self.post_message(GcReclaimRequested(GcReclaimReason.STABLE_CONTENT_REMOVED, prompt=False))

    def begin(self, request_id: str, question: str) -> None:
        self.request_id = request_id
        self.running = True
        self.set_history_position(None, 0)
        self.query_one("#btw-error").display = False
        thinking = self.query_one(BtwThinking)
        thinking.display = True
        thinking.set_running(True)
        self._show(question, None, request_id)
        self.display = True

    async def finish(self, request_id: str, answer: str) -> None:
        if self.request_id != request_id:
            return
        # The thinking row stays until the answer takes its place: with neither, the page would jump to its top.
        if (task := self._show(None, answer, request_id)) is not None:
            await finish_shielded(task)

    def fail(self, request_id: str, *, blocked_reason: str | None = None) -> None:
        if self.request_id != request_id:
            return
        self.running = False
        self._stop_thinking()
        self._notice = _FAILED.bind() if blocked_reason is None else _BLOCKED.bind(reason=DisplayBlock(blocked_reason))
        self._refresh_notice()
        self.query_one("#btw-error").display = True
        self.query_one(BtwPage).reveal(self.query_one("#btw-error"))

    def show_history_notice(self, request_id: str, *, loading: bool = False, reveal: bool = True) -> None:
        self.request_id = request_id
        self.running = False
        self._stop_thinking()
        self._show("", None, request_id)
        self._notice = (_READING if loading else _READ_FAILED).bind()
        self._refresh_notice()
        self.query_one("#btw-error").display = True
        self.set_history_position(None, 0)
        if reveal:
            self.display = True

    def notify_empty_history(self) -> None:
        self.close_question()
        localizer = widget_localizer(self)
        self.notify(
            render_str(localizer, _EMPTY.bind()),
            title=render_str(localizer, _TITLE.bind()),
            severity="warning",
            timeout=3,
            markup=False,
        )

    def notify_history_unavailable(self) -> None:
        self.close_question()
        self.notify(render_str(widget_localizer(self), _UNAVAILABLE.bind()))

    def notify_history_read_failed(self) -> None:
        self.notify(render_str(widget_localizer(self), _READ_FAILED.bind()), severity="warning")

    async def show_record(self, request_id: str, entry: SideQuestionEntry) -> None:
        self.request_id = request_id
        self.running = False
        self._stop_thinking()
        self.query_one("#btw-error").display = False
        self.display = True
        if (task := self._show(entry.question, entry.answer, request_id)) is not None:
            await finish_shielded(task)

    def set_history_position(self, index: int | None, total: int, created_at: str = "") -> None:
        """Show which saved record the card shows; with none (a question still on its way, a notice), no pager."""
        for pager in self.query("#btw-previous, #btw-position, #btw-next"):
            pager.display = index is not None
        counter = self.query_one("#btw-position", Static)
        counter.update(f"{index + 1 if index is not None else 0}/{total}")
        counter.tooltip = (
            datetime.fromisoformat(created_at).astimezone().strftime("%Y-%m-%d %H:%M:%S") if created_at else None
        )
        self.query_one("#btw-previous", Button).disabled = self.running or index is None or index <= 0
        self.query_one("#btw-next", Button).disabled = self.running or index is None or index >= total - 1

    def warn_unsaved(self) -> None:
        self.notify(render_str(widget_localizer(self), _SAVE_FAILED.bind()), severity="warning")

    def _stop_thinking(self) -> None:
        thinking = self.query_one(BtwThinking)
        thinking.set_running(False)
        thinking.display = False

    def close_question(self) -> None:
        self.request_id = ""
        self.running = False
        if composed_live(self) and self.children:
            self._stop_thinking()
            self.display = False
            self._show("", None, "")

    @on(Button.Pressed, "#btw-close")
    def _close_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(self.Closed())

    @on(Button.Pressed, "#btw-previous")
    def _previous_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(self.Navigate(-1))

    @on(Button.Pressed, "#btw-next")
    def _next_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.post_message(self.Navigate(1))
