# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A single disposable side answer beneath the main chat, beside the full-height sidebar."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, ContentSwitcher, Rule, Static, Tab, Tabs

from chrys.app.tui.i18n import render_str, render_text, widget_localizer
from chrys.app.tui.support.gc_freeze import GcFreezeBlockReason
from chrys.app.tui.util.source_text import sanitize_source_text
from chrys.app.tui.util.visibility import set_widgets_visibility_without_layout
from chrys.app.tui.widgets.loading import RunningTitle
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.i18n import DisplayBlock, msg
from chrys.foundation.i18n.formatting import sanitize_legacy_block

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.events import Mount
    from textual.widgets._tabs import Underline

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
_QUESTION = msg("tui.btw.tab.question", fallback="Question")
_ANSWER = msg("tui.btw.tab.output", fallback="Answer")
_log = logging.getLogger(__name__)


class ChatColumn(Vertical):
    """The chat and its side question share a column; the sidebar remains its sibling."""

    DEFAULT_CSS = """
    ChatColumn { width: 1fr; height: 1fr; min-width: 35; }
    """


class BtwTabs(Tabs):
    """Mouse navigation leaves keyboard focus in the composer."""

    FOCUS_ON_CLICK = False

    async def _on_tab_clicked(self, event: Tab.Clicked) -> None:
        event.prevent_default()
        event.stop()
        self.active = event.tab.id or ""

    def _on_underline_clicked(self, event: Underline.Clicked) -> None:
        event.prevent_default()
        event.stop()
        offset = (event.offset.x, event.offset.y - 1)
        for tab in self.query(Tab):
            if offset in tab.region and not tab.disabled:
                self.active = tab.id or ""
                break


class BtwButton(Button):
    """Card actions keep the composer's focus when clicked."""

    FOCUS_ON_CLICK = False


class BtwScroll(VerticalScroll):
    """Card pages remain keyboard-focusable without taking focus on clicks."""

    FOCUS_ON_CLICK = False


class BtwMarkdown(VirtualizedMarkdown):
    """Answer clicks leave typing directed at the composer."""

    FOCUS_ON_CLICK = False


class BtwQuestion(Static):
    """The whole question as plain text, including its original line breaks."""

    def __init__(self) -> None:
        super().__init__(markup=False)
        self.question = ""

    def set_question(self, question: str) -> None:
        self.question = sanitize_legacy_block(question)
        self.update(self.question)


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


class BtwPages(ContentSwitcher):
    """Both pages retain their geometry so a tab switch does not lay out the main transcript."""

    DEFAULT_CSS = """
    BtwPages > * { position: absolute; width: 100%; height: 100%; offset: 0 0; }
    """

    def _on_mount(self, _: Mount) -> None:
        _.prevent_default()
        self.set_reactive(ContentSwitcher.current, self._initial)
        self.watch_current(None, self.current)

    def watch_current(self, old: str | None, new: str | None) -> None:
        set_widgets_visibility_without_layout([(child, child.id == new) for child in self.children])


class BtwPanel(Vertical):
    """Question and answer tabs share one bounded card with an always-visible close button."""

    DEFAULT_CSS = """
    BtwPanel {
        height: 1fr;
        border: round $tui-border-accent $border-opacity;
        border-title-align: left;
        border-title-color: $accent;
        padding: 0;
    }
    BtwPanel > Horizontal { height: 2; margin: 0 1; }
    BtwPanel Tabs { width: 1fr; min-width: 0; }
    BtwPanel #btw-toolbar { width: auto; height: 2; }
    BtwPanel #btw-history-controls { width: auto; height: 1; padding-left: 1; }
    BtwPanel #btw-toolbar Rule { expand: optimal; height: 1; margin: 0; color: $foreground 15%; }
    BtwPanel Button.btw-action {
        width: 3; min-width: 3; height: 1; padding: 0;
        border: none; background: transparent; color: $text-muted; text-style: none;
    }
    BtwPanel Button.btw-action:hover, BtwPanel Button.btw-action:focus {
        background: $foreground 8%; color: $accent; text-style: bold; text-opacity: 100%;
    }
    BtwPanel Button.btw-action:disabled { background: transparent; color: $foreground 25%; }
    BtwPanel #btw-close { margin-left: 1; background: $primary 20%; color: $text; text-style: bold; }
    BtwPanel #btw-close:hover, BtwPanel #btw-close:focus { background: $primary 35%; }
    BtwPanel #btw-position { width: auto; min-width: 3; height: 1; padding: 0 1; color: $text-muted; text-align: center; }
    BtwPanel > ContentSwitcher { width: 100%; height: 1fr; min-height: 1; }
    BtwPanel #btw-question-page {
        height: 100%; background: transparent;
        overflow-x: hidden; scrollbar-size: 1 1;
    }
    BtwPanel BtwQuestion { height: auto; margin: 0 2; color: $text; }
    BtwPanel #btw-output-page { height: 100%; overflow-x: hidden; scrollbar-size: 1 1; }
    BtwPanel VirtualizedMarkdown {
        height: 1fr; min-height: 1; padding: 0 0 0 2; scrollbar-size: 1 1;
        background: transparent;
    }
    BtwPanel #btw-output-page > Static { height: auto; padding: 0 2; }
    """

    class Closed(Message):
        """The user closes this one question, even while its request is running."""

    class Navigate(Message):
        def __init__(self, delta: int) -> None:
            super().__init__()
            self.delta = delta

    def __init__(self, *, locale_controller: LocaleController | None = None) -> None:
        super().__init__()
        self._locale_controller = locale_controller
        self.request_id = ""
        self.running = False
        self.display = False
        self._notice = _FAILED.bind()

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield BtwTabs(
                Tab("", id="btw-question-tab"),
                Tab("", id="btw-output-tab"),
                active="btw-output-tab",
            )
            with Vertical(id="btw-toolbar"):
                with Horizontal(id="btw-history-controls"):
                    yield BtwButton("‹", id="btw-previous", classes="btw-action", compact=True, disabled=True)  # noqa: RUF001 - navigation glyph
                    yield Static("0/0", id="btw-position", markup=False)
                    yield BtwButton("›", id="btw-next", classes="btw-action", compact=True, disabled=True)  # noqa: RUF001 - navigation glyph
                    yield BtwButton("✕", id="btw-close", classes="btw-action", compact=True)
                yield Rule(line_style="heavy")
        with BtwPages(initial="btw-output-page"):
            with BtwScroll(id="btw-question-page"):
                yield BtwQuestion()
            with BtwScroll(id="btw-output-page"):
                yield BtwThinking()
                yield BtwMarkdown(id="btw-answer")
                yield Static(markup=False, id="btw-error")

    def gc_freeze_block_reason(self) -> GcFreezeBlockReason | None:
        return GcFreezeBlockReason.PARTICIPANT if self.running else None

    def prepare_for_gc_freeze(self) -> None:
        self.query_one(VirtualizedMarkdown).prepare_for_gc_freeze()

    def after_gc_freeze(self) -> None:
        self.query_one(VirtualizedMarkdown).after_gc_freeze()

    def abort_gc_freeze(self) -> None:
        self.query_one(VirtualizedMarkdown).abort_gc_freeze()

    def on_mount(self) -> None:
        self.query_one(VirtualizedMarkdown).display = False
        self.query_one("#btw-error").display = False
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)
        self.refresh_localization()

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)

    def refresh_localization(self) -> None:
        if not self.children:
            return
        localizer = widget_localizer(self)
        self.border_title = render_str(localizer, _TITLE.bind())
        self.query_one("#btw-close", Button).tooltip = render_text(localizer, _CLOSE.bind())
        self.query_one("#btw-previous", Button).tooltip = render_text(localizer, _PREVIOUS.bind())
        self.query_one("#btw-next", Button).tooltip = render_text(localizer, _NEXT.bind())
        self.query_one("#btw-question-tab", Tab).label = render_text(localizer, _QUESTION.bind())
        self.query_one("#btw-output-tab", Tab).label = render_text(localizer, _ANSWER.bind())
        self.query_one("#btw-error", Static).update(render_text(localizer, self._notice))
        self.query_one(BtwThinking).refresh()

    def begin(self, request_id: str, question: str) -> None:
        self.request_id = request_id
        self.running = True
        self.set_history_position(None, 0)
        self.query_one(BtwQuestion).set_question(question)
        self.query_one(VerticalScroll).scroll_home(animate=False)
        self.query_one(Tabs).active = "btw-output-tab"
        self.query_one(ContentSwitcher).current = "btw-output-page"
        self.query_one(VirtualizedMarkdown).display = False
        self.query_one("#btw-error").display = False
        thinking = self.query_one(BtwThinking)
        thinking.display = True
        thinking.set_running(True)
        self.display = True

    async def finish(self, request_id: str, answer: str) -> None:
        if self.request_id != request_id:
            return
        self.running = False
        self._stop_thinking()
        markdown = self.query_one(VirtualizedMarkdown)
        try:
            await markdown.update(sanitize_source_text(answer))
        except Exception:
            _log.warning("Could not render side question answer", exc_info=True)
            self.fail(request_id)
            return
        if self.request_id != request_id:
            return
        markdown.display = True
        markdown.scroll_home(animate=False)

    def fail(self, request_id: str, *, blocked_reason: str | None = None) -> None:
        if self.request_id != request_id:
            return
        self.running = False
        self._stop_thinking()
        self._notice = _FAILED.bind() if blocked_reason is None else _BLOCKED.bind(reason=DisplayBlock(blocked_reason))
        self.refresh_localization()
        self.query_one("#btw-error").display = True

    def show_history_notice(self, request_id: str, *, loading: bool = False, reveal: bool = True) -> None:
        self.request_id = request_id
        self.running = False
        self._stop_thinking()
        self.query_one(BtwQuestion).set_question("")
        self.query_one(VirtualizedMarkdown).display = False
        self.query_one(Tabs).active = "btw-output-tab"
        self.query_one(ContentSwitcher).current = "btw-output-page"
        self._notice = (_READING if loading else _READ_FAILED).bind()
        self.refresh_localization()
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
        self.query_one(BtwQuestion).set_question(entry.question)
        self.query_one(VerticalScroll).scroll_home(animate=False)
        self.query_one("#btw-error").display = False
        self.query_one(VirtualizedMarkdown).display = False
        self.display = True
        await self.finish(request_id, entry.answer)

    def set_history_position(self, index: int | None, total: int, created_at: str = "") -> None:
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
        if self.is_attached and self.children and not self._pruning and not self._closing:
            self._stop_thinking()
            self.display = False
            self.query_one(BtwQuestion).set_question("")
            self.query_one(VirtualizedMarkdown).update("")

    @on(Tabs.TabActivated)
    def _tab_activated(self, event: Tabs.TabActivated) -> None:
        event.stop()
        self.query_one(ContentSwitcher).current = (
            "btw-question-page" if event.tabs.active == "btw-question-tab" else "btw-output-page"
        )

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
