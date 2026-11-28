# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Renderer for the ``ask_user`` tool.

One bordered panel shows the question as Markdown while the agent waits, the
live prompt (tabs, question, options, input, buttons) while the user answers
inline, and the question plus the user's plain-text answer once complete.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual import events, on
from textual.containers import VerticalScroll
from textual.message import Message
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from chrys.app.tui.i18n import render_str, render_text, widget_localizer
from chrys.app.tui.util.removal import finish_shielded
from chrys.app.tui.util.source_text import sanitize_source_text
from chrys.app.tui.widgets import (
    ASK_USER_INPUT_MAX_HEIGHT,
    AskUserActiveQuestionChanged,
    AskUserPrompt,
    AskUserResponseResized,
    AskUserSubmitted,
    PromptDraft,
)
from chrys.app.tui.widgets.ask_user_controls import (
    ASK_USER_BUTTON_ROW_HEIGHT,
    AskUserContentResized,
    ask_user_hanging_answer,
    ask_user_hanging_grid,
)
from chrys.app.tui.widgets.ask_user_prompt import ASK_USER_NOT_ANSWERED_REF
from chrys.app.tui.widgets.chat.tool_call import (
    TOOL_CARD_ERRORED,
    TOOL_CARD_INTERRUPTED,
    BaseToolCard,
    ToolCardHeader,
    fmt_duration,
    tool_result_render_status,
)
from chrys.app.tui.widgets.chat.tool_view_builders import TOOL_VIEW_EMPTY, build_code_view, build_params_view
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.i18n import msg
from chrys.foundation.models.ask_user import (
    AskUserAnswer,
    AskUserQuestion,
    parse_ask_user_answers,
    parse_recorded_ask_user_questions,
)
from chrys.foundation.tool_kinds import KIND_ASK_USER
from chrys.foundation.tool_result_metadata import TOOL_INTERRUPTED_METADATA_KEY

_ASK_USER_QUESTION = msg("tui.tool_card.ask_user.question", fallback="Question")
_ASK_USER_ANSWER = msg("tui.tool_card.ask_user.answer", fallback="Answer")
_ASK_USER_WAITING = msg("tui.tool_card.ask_user.waiting", fallback="waiting")

if TYPE_CHECKING:
    from rich.console import RenderableType
    from textual.app import ComposeResult
    from textual.await_complete import AwaitComplete
    from textual.await_remove import AwaitRemove

_USER_RESPONSE_PREFIX = "User response:"
_INLINE_PANEL_FRAME_ROWS = 2
_INLINE_CARD_HEADER_ROWS = 1
_INLINE_CARD_BOTTOM_MARGIN_ROWS = 1
_INLINE_RESERVED_TRANSCRIPT_ROWS = 4
_INLINE_TAB_BAR_ROWS = 2


class AskUserInlineSubmitted(Message):
    """User submitted an inline ask_user response from the chat renderer."""

    def __init__(self, call_id: str, request_id: str, answers: tuple[AskUserAnswer, ...]) -> None:
        super().__init__()
        self.call_id = call_id
        self.request_id = request_id
        self.answers = answers


class AskUserInlineResized(Message):
    """Inline ask_user controls changed size inside a tool renderer."""

    def __init__(self, call_id: str) -> None:
        super().__init__()
        self.call_id = call_id


def _extract_questions(args_summary: str, args: dict[str, Any]) -> tuple[AskUserQuestion, ...]:
    """Extract canonical questions from normalized args or their JSON summary."""
    questions = parse_recorded_ask_user_questions(args)
    if questions:
        return questions
    if not args_summary:
        return ()
    try:
        parsed = json.loads(args_summary)
    except TypeError, ValueError:
        return ()
    if not isinstance(parsed, dict):
        return ()
    return parse_recorded_ask_user_questions(parsed)


def _questions_markdown(questions: tuple[AskUserQuestion, ...]) -> str:
    if len(questions) == 1:
        return questions[0].question
    return "\n".join(f"{index}. {question.question}" for index, question in enumerate(questions, start=1))


def _questions_display_markdown(questions: tuple[AskUserQuestion, ...]) -> str:
    """The card's question block; copy keeps :func:`_questions_markdown`'s raw text."""
    return sanitize_source_text(_questions_markdown(questions))


def _answers_from_result(result: str, questions: tuple[AskUserQuestion, ...]) -> tuple[AskUserAnswer, ...] | None:
    """Structured answers for *questions*, or ``None`` to show *result* verbatim.

    Without parsable questions there is nothing to pair answers with, so the
    recorded text is shown as-is rather than an empty answer list.
    """
    if not questions:
        return None
    if result.startswith(_USER_RESPONSE_PREFIX):
        value = result[len(_USER_RESPONSE_PREFIX) :].removeprefix(" ")
        return parse_ask_user_answers(
            (AskUserAnswer(values=(value,)),),
            question_count=len(questions),
        )
    try:
        payload = json.loads(result)
    except TypeError, ValueError:
        return None
    if type(payload) is not dict or type(payload.get("responses")) is not list:
        return None
    raw_answers = []
    for response in payload["responses"]:
        if type(response) is not dict:
            raw_answers.append({})
            continue
        raw_answers.append({"values": response.get("answers", []), "note": response.get("note", "")})
    return parse_ask_user_answers(raw_answers, question_count=len(questions))


def _answer_from_result(result: str) -> str:
    """Return the user-facing answer body from the middleware result string."""
    if result.startswith(_USER_RESPONSE_PREFIX):
        return result[len(_USER_RESPONSE_PREFIX) :].removeprefix(" ")
    return result


class AskUserToolCall(BaseToolCard):
    """Rich renderer for ``ask_user`` tool calls."""

    DEFAULT_CSS = """
    AskUserToolCall {
        height: auto;
        padding: 0 0 0 2;
        margin: 0 0 1 0;
    }
    AskUserToolCall #ask-label {
        height: auto;
    }
    AskUserToolCall > #ask-panel {
        height: auto;
        margin: 0 0 0 2;
        border: round $tui-border-warning 50%;
        border-title-color: $warning;
        border-title-style: bold;
        padding: 0 1;
    }
    AskUserToolCall.-success > #ask-panel {
        border: round $tui-border-success 30%;
        border-title-color: $success;
        border-title-style: not bold;
    }
    AskUserToolCall.-error > #ask-panel {
        border: round $tui-border-error 50%;
        border-title-color: $text-error;
        border-title-style: not bold;
        border-subtitle-color: $text-error;
        border-subtitle-style: not bold;
    }
    AskUserToolCall.-rejected > #ask-panel {
        border: round $tui-border-warning 50%;
        border-title-color: $warning;
        border-title-style: not bold;
    }
    AskUserToolCall #ask-question {
        height: auto;
        min-height: 1;
        padding: 0;
        background: transparent;
    }
    AskUserToolCall #ask-spinner {
        height: auto;
    }
    AskUserToolCall #ask-answer {
        height: auto;
        color: $text-muted;
        display: none;
    }
    /* Multi-question completions list the questions inside the answer block;
       they keep the question colour while each hanging answer stays muted. */
    AskUserToolCall > .askuser-answer--question {
        color: $foreground;
    }
    AskUserToolCall #ask-inline {
        height: auto;
    }
    AskUserToolCall #ask-inline > #askuser-inner {
        scrollbar-color: $accent;
    }
    AskUserToolCall #askuser-footer {
        width: 100%;
        height: auto;
    }
    AskUserToolCall #askuser-input {
        width: 100%;
        height: auto;
        /* Keep in sync with ASK_USER_INPUT_MIN_HEIGHT / ASK_USER_INPUT_MAX_HEIGHT. */
        min-height: 3;
        max-height: 7;
        /* The compact TextArea strips its frame with an !important rule of its
           own; this frame must outrank it, and must not depend on the modal
           dialog's stylesheet having been loaded first. */
        border: round $tui-border-accent $border-opacity !important;
        margin: 0;
    }
    AskUserToolCall #askuser-buttons {
        width: 100%;
        align: center top;
        height: 3;
        padding: 0;
    }
    AskUserToolCall #askuser-buttons > Button {
        margin: 0 1;
    }
    AskUserToolCall.-inline #ask-question {
        display: none;
    }
    AskUserToolCall.-inline #ask-spinner {
        display: none;
    }
    AskUserToolCall.-done #ask-spinner {
        display: none;
    }
    AskUserToolCall.-done #ask-answer {
        display: block;
    }
    /* An interrupted question has no answer to show; the panel subtitle
       carries the status instead of a lone word in the body. */
    AskUserToolCall.-interrupted #ask-answer {
        display: none;
    }
    /* Multi-question answers interleave each question with its answer, so
       the numbered Markdown would only repeat them. */
    AskUserToolCall.-done.-multi #ask-question {
        display: none;
    }
    """

    COMPONENT_CLASSES: ClassVar[set[str]] = BaseToolCard.COMPONENT_CLASSES | {"askuser-answer--question"}

    def __init__(
        self,
        call_id: str,
        tool_name: str,
        args_summary: str = "",
        args: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(call_id, tool_name, args_summary, args=args)
        self._questions = _extract_questions(args_summary, self.args)
        self._question = self._questions[0].question if self._questions else ""
        self._question_source = ""
        """Text requested by compose or update, including before the Markdown mounts."""
        self._question_parse: AwaitComplete | None = None
        self._spin_idx = 0
        self._timer: Timer | None = None
        self._inline_request_id = ""
        self._inline_submitted = False
        self._inline_last_width = 0
        self._chat_viewport_height = 0
        self._inline_resize_generation = 0
        self._inline_scheduled_sync_generation: int | None = None
        self._inline_mount_generation = 0
        """Advanced by every show and clear; a prompt waiting for its predecessor to go mounts only if still current."""

    def _label_text(self, duration_ms: int = 0) -> Text:
        t = Text()
        t.append("• ", style="bold")
        t.append(self.tool_name, style="bold")
        if duration_ms:
            t.append(f" ({fmt_duration(duration_ms)})", style="dim")
        return t

    def compose(self) -> ComposeResult:
        yield ToolCardHeader(self._label_text(), id="ask-label")

        self._question_source = _questions_display_markdown(self._questions)
        panel = VerticalScroll(
            VirtualizedMarkdown(self._question_source, id="ask-question"),
            id="ask-panel",
            can_focus=False,
        )
        panel.border_title = render_text(widget_localizer(self), _ASK_USER_QUESTION.bind())
        yield panel

    def on_mount(self) -> None:
        panel = self.query_one("#ask-panel")
        panel.mount(Static(self._render_spinner(), id="ask-spinner"))
        panel.mount(Static("", id="ask-answer"))
        self._timer = self.set_interval(0.12, self._spin)

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()

    def on_resize(self, event: events.Resize) -> None:
        if self.status != "running" or not self._inline_request_id:
            if self.status in {"complete", "error", "rejected"}:
                self._sync_completed_layout()
            return
        width_changed = event.size.width != self._inline_last_width
        if not width_changed:
            return
        self._inline_last_width = event.size.width
        self._inline_resize_generation += 1
        self._schedule_inline_layout_sync()

    def handle_chat_viewport_resize(self, viewport_height: int) -> None:
        """Cache chat-local height and recompute live or completed caps."""
        if viewport_height <= 0 or viewport_height == self._chat_viewport_height:
            return
        self._chat_viewport_height = viewport_height
        if self.status == "running" and self._inline_request_id:
            self._inline_resize_generation += 1
            self._schedule_inline_layout_sync()
        elif self.status in {"complete", "error", "rejected"}:
            self.call_after_refresh(self._sync_completed_layout)

    def _spin(self) -> None:
        if self.status == "running":
            self._spin_idx = (self._spin_idx + 1) % len(self._SPINNERS)
            with suppress(Exception):
                self.query_one("#ask-spinner", Static).update(self._render_spinner(), layout=False)

    def _render_spinner(self) -> Text:
        t = Text()
        t.append(f"{self._SPINNERS[self._spin_idx]} ", style="yellow")
        t.append(render_str(widget_localizer(self), _ASK_USER_WAITING.bind()), style="yellow")
        return t

    def show_inline_prompt(
        self,
        request_id: str,
        questions: tuple[AskUserQuestion, ...],
        *,
        draft: PromptDraft | None = None,
    ) -> bool:
        """Mount live answer controls inside the renderer."""
        if self.status != "running" or not request_id:
            return False
        prompt = AskUserPrompt(
            request_id,
            questions,
            inline=True,
            allow_inline=False,
            draft=draft,
        )
        self._questions = questions
        self._question = questions[0].question if questions else ""
        self._inline_request_id = request_id
        self._inline_submitted = False
        self._inline_mount_generation += 1
        try:
            panel = self.query_one("#ask-panel")
            # A previous prompt keeps its id until its removal completes, even
            # one removed already: this one mounts once it is gone.
            previous = list(panel.query_children("#ask-inline"))
            if previous:
                # A partial, not a coroutine: a later show can cancel this worker
                # before it starts, and a coroutine never awaited warns.
                self.run_worker(
                    partial(
                        self._mount_inline_prompt_after,
                        panel.remove_children(previous),
                        panel,
                        prompt,
                        self._inline_mount_generation,
                    ),
                    group="ask-inline-mount",
                    exclusive=True,
                )
            else:
                panel.mount(prompt)
        except Exception:
            self._inline_request_id = ""
            return False

        if self._timer is not None:
            self._timer.stop()
            self._timer = None

        with suppress(Exception):
            self.query_one("#ask-spinner", Static).display = False

        self.add_class("-inline")
        self._schedule_inline_layout_sync()
        self._update_active_question(prompt.active_index)
        return True

    async def _mount_inline_prompt_after(
        self, removal: AwaitRemove, panel: Widget, prompt: AskUserPrompt, generation: int
    ) -> None:
        await finish_shielded(removal)
        # A later show or clear took over meanwhile, or the card is going.
        if generation != self._inline_mount_generation or not panel.is_attached or panel._pruning:
            return
        panel.mount(prompt)
        self._schedule_inline_layout_sync()
        self._update_active_question(prompt.active_index)

    def clear_inline_prompt(self) -> None:
        """Remove any active inline answer controls."""
        self.remove_class("-inline")
        self._inline_request_id = ""
        self._inline_submitted = False
        self._inline_mount_generation += 1
        self._inline_last_width = 0
        self._inline_resize_generation += 1
        self._inline_scheduled_sync_generation = None
        with suppress(Exception):
            panel = self.query_one("#ask-panel")
            panel.border_title = render_text(widget_localizer(self), _ASK_USER_QUESTION.bind())
            panel.styles.height = "auto"
            panel.styles.min_height = 0
            panel.styles.max_height = None
            self.styles.min_height = 0
            self.styles.max_height = None
        with suppress(Exception):
            self.query_one("#ask-inline").remove()

    def clear_inline_prompt_for(self, request_id: str) -> bool:
        """Clear the inline prompt only when it still belongs to ``request_id``."""
        if not request_id or request_id != self._inline_request_id:
            return False
        self.clear_inline_prompt()
        return True

    def _lock_inline_prompt(self) -> None:
        with suppress(Exception):
            self.query_one("#ask-inline", AskUserPrompt).disable_controls()

    def _schedule_inline_layout_sync(self) -> None:
        if self._inline_scheduled_sync_generation is not None:
            return
        generation = self._inline_resize_generation
        self._inline_scheduled_sync_generation = generation
        if not self.call_after_refresh(self._sync_inline_layout_after_resize, generation):
            self._inline_scheduled_sync_generation = None

    def _sync_inline_layout_after_resize(self, generation: int) -> None:
        if generation != self._inline_scheduled_sync_generation:
            return
        if self.status != "running" or not self._inline_request_id:
            self._inline_scheduled_sync_generation = None
            return
        if generation != self._inline_resize_generation:
            latest_generation = self._inline_resize_generation
            self._inline_scheduled_sync_generation = latest_generation
            if not self.call_after_refresh(self._sync_inline_layout_after_resize, latest_generation):
                self._inline_scheduled_sync_generation = None
            return
        self._inline_scheduled_sync_generation = None
        if self._sync_inline_layout():
            self.post_message(AskUserInlineResized(self.call_id))

    @staticmethod
    def _allocate_inline_budget(viewport_rows: int, *, has_tabs: bool, live: bool) -> tuple[int, int]:
        """Return ``(content_cap, fixed_rows)`` for the single panel region."""
        fixed = _INLINE_CARD_HEADER_ROWS + _INLINE_PANEL_FRAME_ROWS + _INLINE_CARD_BOTTOM_MARGIN_ROWS
        if live:
            fixed += ASK_USER_INPUT_MAX_HEIGHT + ASK_USER_BUTTON_ROW_HEIGHT
            if has_tabs:
                fixed += _INLINE_TAB_BAR_ROWS
        budget = max(0, viewport_rows - _INLINE_RESERVED_TRANSCRIPT_ROWS)
        return max(0, budget - fixed), fixed

    def _sync_inline_layout(self) -> bool:
        """Cap the shared content region under the viewport budget.

        The card otherwise sizes to its content: the region follows the active
        pane and the input as they grow or shrink, and only scrolls once a pane
        outgrows the cap.
        """
        if self.status != "running":
            return False
        try:
            prompt = self.query_one("#ask-inline", AskUserPrompt)
            inner = prompt.query_one("#askuser-inner")
        except Exception:
            return False
        has_tabs = len(prompt.questions) > 1
        viewport_height = self._chat_viewport_height or self.screen.size.height
        content_cap, _fixed = self._allocate_inline_budget(viewport_height, has_tabs=has_tabs, live=True)
        tab_rows = _INLINE_TAB_BAR_ROWS if has_tabs else 0
        inner_max_height = max(content_cap, 1) + tab_rows
        if str(inner.styles.max_height) == str(inner_max_height):
            return False
        inner.styles.max_height = inner_max_height
        return True

    def _sync_completed_layout(self) -> bool:
        """Cap the completed panel under the shared viewport budget."""
        if self.status not in {"complete", "error", "rejected"}:
            return False
        try:
            panel = self.query_one("#ask-panel")
        except Exception:
            return False
        content_cap, fixed = self._allocate_inline_budget(
            self._chat_viewport_height or self.screen.size.height,
            has_tabs=False,
            live=False,
        )
        panel_max = content_cap + _INLINE_PANEL_FRAME_ROWS
        tool_max = fixed + content_cap
        content_saturates_budget = content_cap == 0 or panel.virtual_size.height >= content_cap
        tool_min = tool_max if content_saturates_budget else 0
        changed = (
            str(panel.styles.max_height) != str(panel_max)
            or str(self.styles.max_height) != str(tool_max)
            or str(self.styles.min_height) != str(tool_min)
        )
        if not changed:
            return False
        panel.styles.max_height = panel_max
        self.styles.max_height = tool_max
        self.styles.min_height = tool_min
        return True

    def _update_active_question(self, index: int) -> None:
        _ = index
        if not self._questions:
            return
        with suppress(Exception):
            prompt = self.query_one("#ask-inline", AskUserPrompt)
            self.query_one("#ask-panel").border_title = prompt.active_title()
        self._inline_resize_generation += 1
        self._schedule_inline_layout_sync()

    @on(AskUserActiveQuestionChanged)
    def _on_active_question_changed(self, event: AskUserActiveQuestionChanged) -> None:
        if event.request_id != self._inline_request_id or self.status != "running":
            return
        event.stop()
        self._update_active_question(event.index)

    @on(AskUserSubmitted)
    def _on_ask_user_submitted(self, event: AskUserSubmitted) -> None:
        if event.request_id != self._inline_request_id or self.status != "running":
            return
        event.stop()
        if self._inline_submitted:
            return
        self._inline_submitted = True
        self._lock_inline_prompt()
        self.post_message(AskUserInlineSubmitted(self.call_id, event.request_id, event.answers))

    @on(AskUserContentResized)
    def _on_ask_user_content_resized(self, event: AskUserContentResized) -> None:
        # The card sizes to its pane, so the transcript relayouts and
        # re-anchors whenever a pane lays out or re-wraps.
        event.stop()
        if self.status != "running" or not self._inline_request_id:
            return
        self.post_message(AskUserInlineResized(self.call_id))

    @on(AskUserResponseResized)
    def _on_ask_user_response_resized(self, event: AskUserResponseResized) -> None:
        if self.status != "running" or not self._inline_request_id:
            return
        event.stop()
        self.post_message(AskUserInlineResized(self.call_id))

    def set_complete(self, result: str, duration_ms: int = 0, **kwargs: Any) -> None:
        """Mark the question as answered."""
        self.clear_inline_prompt()
        self.result_text = result
        self.duration_ms = duration_ms
        self.approval = kwargs.get("approval")
        metadata = kwargs.get("metadata")
        self.metadata = metadata if isinstance(metadata, dict) else {}
        self.status = "complete"
        if self._timer is not None:
            self._timer.stop()

        with suppress(Exception):
            self.query_one("#ask-label", Static).update(self._label_text(duration_ms))

        render_status = tool_result_render_status(result, self.approval, self.tool_kind or KIND_ASK_USER, self.metadata)
        interrupted = self._is_interrupted()
        if interrupted:
            render_status = "error"
        self.status = render_status
        if render_status == "rejected":
            self.add_class("-rejected")
        elif render_status == "error":
            self.add_class("-error")
        else:
            self.add_class("-success")

        with suppress(Exception):
            if interrupted:
                self._render_interrupted()
            else:
                self._render_completed(result)
        self.add_class("-done")
        self.call_after_refresh(self._sync_completed_layout)
        self._show_tool_copy_button()

    def _is_interrupted(self) -> bool:
        """Whether the recorded result is the kernel's interruption filler.

        A live cancel hands ``set_error`` the "cancelled" marker, but a restored
        session replays the model-facing filler result the kernel wrote for the
        interrupted call, recognisable only by its metadata.
        """
        metadata = self.metadata or {}
        return metadata.get(TOOL_INTERRUPTED_METADATA_KEY) is True

    def _update_question_markdown(self, questions: tuple[AskUserQuestion, ...]) -> None:
        source = _questions_display_markdown(questions)
        if source != self._question_source:
            self._question_parse = self.query_one("#ask-question", VirtualizedMarkdown).update(source)
            self._question_source = source

    async def mount_pending_content(self) -> bool:
        """Wait for any visible question update that started after the card mounted."""
        if self._question_parse is None:
            return False
        question = self.query_one("#ask-question", VirtualizedMarkdown)
        installed = False
        while question.display and (pending := self._question_parse) is not None:
            try:
                await asyncio.shield(pending)
            finally:
                if self._question_parse is pending and pending.is_done:
                    self._question_parse = None
            installed = True
        return installed

    def _render_interrupted(self) -> None:
        """Show the interrupt as the panel status; there is no answer to show.

        The questions stay readable and the model-facing filler text stays out
        of the card (it remains in ``result_text`` for the copy payload).
        """
        questions = _extract_questions(self.args_summary, self.args)
        self._update_question_markdown(questions)
        self.add_class("-interrupted")
        self.query_one("#ask-panel").border_subtitle = render_text(widget_localizer(self), TOOL_CARD_INTERRUPTED.bind())
        self.query_one("#ask-answer", Static).update(Text(""))

    def _render_completed(self, result: str) -> None:
        questions = _extract_questions(self.args_summary, self.args)
        answers = _answers_from_result(result, questions)
        self._update_question_markdown(questions)
        if answers is None:
            self.query_one("#ask-answer", Static).update(Text(sanitize_source_text(_answer_from_result(result))))
            return
        self.set_class(len(questions) > 1, "-multi")
        unanswered = render_str(widget_localizer(self), ASK_USER_NOT_ANSWERED_REF.bind())
        # Each answer hangs under its question the way an option's description
        # hangs under its label, and a soft-wrapped question or answer keeps
        # its continuation lines under its own first character. A single
        # question keeps its Markdown block above, so the glyph starts at the
        # margin.
        rendered: RenderableType
        if len(questions) > 1:
            question_style = self.get_component_rich_style("askuser-answer--question", partial=True)
            rows: list[tuple[str, RenderableType]] = []
            for index, (question, answer) in enumerate(zip(questions, answers, strict=True), start=1):
                rows.append((f"{index}. ", Text(sanitize_source_text(question.question), style=question_style)))
                rows.append(("", ask_user_hanging_answer(answer, unanswered=unanswered)))
            rendered = ask_user_hanging_grid(rows)
        else:
            (answer,) = answers
            rendered = ask_user_hanging_answer(answer, unanswered=unanswered)
        self.query_one("#ask-answer", Static).update(rendered)

    def set_error(self, error: str) -> None:
        """Mark the question as failed."""
        self.clear_inline_prompt()
        self.result_text = error
        self.status = "error"
        if self._timer is not None:
            self._timer.stop()

        self.add_class("-error")
        with suppress(Exception):
            if error == "cancelled" or self._is_interrupted():
                self._render_interrupted()
            else:
                self.query_one("#ask-panel").border_subtitle = render_text(
                    widget_localizer(self), TOOL_CARD_ERRORED.bind()
                )
                self.query_one("#ask-answer", Static).update(Text(error))
        self.add_class("-done")
        self.call_after_refresh(self._sync_completed_layout)
        self._show_tool_copy_button()

    def _tool_copy_input(self) -> tuple[str, str]:
        """Preserve question text and every remaining recorded input field."""
        markdown = _questions_markdown(self._questions) or self._render_message(TOOL_VIEW_EMPTY.bind())
        extra = self._input_extra_args()
        if extra:
            encoded = json.dumps(extra, indent=2, ensure_ascii=False, default=str)
            markdown = f"{markdown}\n\n```json\n{encoded}\n```"
        return "markdown", markdown

    def _tool_copy_sections(self) -> list[tuple[str, str, str]]:
        """Copy the plain answer body without the middleware transport prefix."""
        return [
            (
                self._render_message(_ASK_USER_ANSWER.bind()),
                "text",
                _answer_from_result(self.result_text) or self._render_message(TOOL_VIEW_EMPTY.bind()),
            )
        ]

    def _input_extra_args(self) -> dict[str, Any]:
        """Return recorded non-question-text input, retaining native question fields."""
        args = self._tool_input_args()
        extra = {key: value for key, value in args.items() if key not in {"question", "questions"}}
        raw_questions = args.get("questions")
        if type(raw_questions) is list and raw_questions:
            extra["questions"] = [
                {key: value for key, value in question.items() if key != "question"}
                if type(question) is dict
                else question
                for question in raw_questions
            ]
        return extra

    def _tool_view_input_widgets(self) -> list[Widget]:
        """Render the question as markdown plus any remaining args as JSON."""
        dark = self._view_dark()
        widgets: list[Widget] = [
            build_code_view(
                "markdown",
                _questions_markdown(self._questions) or self._render_message(TOOL_VIEW_EMPTY.bind()),
                dark=dark,
                render_message=self._render_message,
            )
        ]
        extra = self._input_extra_args()
        if extra:
            widgets.extend(build_params_view(extra, dark=dark, render_message=self._render_message))
        return widgets
