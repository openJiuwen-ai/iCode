# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Live transcript rendering for ChatPanel."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from rich.text import Text

from chrys.app.tui.util.removal import remove_all_shielded, remove_shielded
from chrys.app.tui.widgets.chat.compaction_card import CompactionCard
from chrys.app.tui.widgets.chat.context_fold import ContextFoldWidget
from chrys.app.tui.widgets.chat.messages import (
    ERROR_RETRY_ACTION,
    AgentMessage,
    ErrorMessage,
    InterruptedMessage,
    RetryMessage,
    SystemMessage,
    ThinkingMessage,
    UserMessage,
    format_message_created_at,
    resolve_interrupted_message_copy,
    turn_duration_ms,
)
from chrys.app.tui.widgets.chat.ports import (
    ChatMountPort,
    ChatWidgetQueryPort,
    StatusCleanupPort,
    TranscriptLocalizationPort,
    TranscriptUiPort,
)
from chrys.app.tui.widgets.chat.toc_model import TurnTocModel
from chrys.app.tui.widgets.chat.tool_registry import ToolGroupRegistry
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.history_markers import EXECUTION_INTERRUPTED_MESSAGE

if TYPE_CHECKING:
    from textual.widget import Widget

    from chrys.app.tui.widgets.chat.tool_call import ToolGroup
    from chrys.foundation.i18n import MessageRef
    from chrys.foundation.models.invocations import ContentChannel


class LiveTranscriptRenderer:
    """Render live user, agent, system, status, and context messages."""

    def __init__(
        self,
        mount: ChatMountPort,
        ui: TranscriptUiPort,
        status: StatusCleanupPort,
        query: ChatWidgetQueryPort,
        toc: TurnTocModel,
        tools: ToolGroupRegistry,
        *,
        localization: TranscriptLocalizationPort | None = None,
        thinking_expanded: Callable[[], bool] | None = None,
        fold_passed_text: bool = False,
        rollback_targets: Callable[[], set[int]] | None = None,
        cleanup_failed_input: bool = False,
    ) -> None:
        self._mount = mount
        self._ui = ui
        self._status = status
        self._query = query
        self._toc = toc
        self._tools = tools
        self._rollback_targets = rollback_targets or set
        self._cleanup_failed_input = cleanup_failed_input
        self._resolve_message = localization.render if localization is not None else format_message
        self._pending_user_message: UserMessage | None = None
        self._current_agent_msg: AgentMessage | None = None
        self._streamed_answer: AgentMessage | None = None
        """The answer text streamed last, until more of the turn follows it: the turn's completion stamps it.

        A boundary (a context fold, a message sent mid-turn) may have let its cursor go first.
        """
        self._profile_name: str = ""
        self._compaction_cards: dict[str, CompactionCard] = {}
        # The answer segment being streamed, and its text while it is still
        # blank: whitespace-only text mounts no message.
        self._segment_id = ""
        self._blank_segment_text = ""
        self._answer_streamed = False
        """Whether answer text streamed since the turn opened."""
        self._thinking_expanded = thinking_expanded
        """Whether new thinking opens, read as each segment opens; None where no thinking shows at all."""
        # The thinking segment being streamed, as the answer's: its message,
        # and its text while it is still blank.
        self._thinking_msg: ThinkingMessage | None = None
        self._thinking_segment_id = ""
        self._blank_thinking_text = ""
        self._thinking_to_fold: ThinkingMessage | None = None
        """The thinking mounted last, which folds once what follows it starts, as a tool group does."""
        self._fold_passed_text = fold_passed_text
        """Whether text between tool calls folds once what follows it starts, as thinking does."""
        self._group_to_fold: ToolGroup | None = None
        """The tool group a message sent mid-turn closed, which folds once the turn goes on below the message."""

    @property
    def current_agent_msg(self) -> AgentMessage | None:
        """Current streamed agent message cursor."""
        return self._current_agent_msg

    @current_agent_msg.setter
    def current_agent_msg(self, message: AgentMessage | None) -> None:
        self._current_agent_msg = message

    @property
    def profile_name(self) -> str:
        """Current agent profile label for live agent message headers."""
        return self._profile_name

    @profile_name.setter
    def profile_name(self, name: str) -> None:
        self._profile_name = name

    def set_profile(self, name: str) -> None:
        """Set the current agent profile label."""
        self._profile_name = name

    def reset_for_clear(self) -> None:
        """Clear per-transcript cursors while preserving durable metadata."""
        self._pending_user_message = None
        self._current_agent_msg = None
        self._streamed_answer = None
        self._compaction_cards.clear()
        self._segment_id = ""
        self._blank_segment_text = ""
        self._answer_streamed = False
        self._thinking_msg = None
        self._thinking_segment_id = ""
        self._blank_thinking_text = ""
        self._thinking_to_fold = None
        self._group_to_fold = None

    def _finalize_current_agent_message(self) -> None:
        """Terminate and release the active streamed-message presentations: the answer's and the thinking's."""
        self._finalize_thinking()
        if self._current_agent_msg is None:
            return
        self._current_agent_msg.stream_update(
            self._current_agent_msg.format_agent_response_copy(),
            is_final=True,
        )
        self._current_agent_msg = None

    def stop_answer(self) -> None:
        """The run stopped before completing its answer: the text it streamed stays, final."""
        self._streamed_answer = None
        self._segment_id = ""
        self._blank_segment_text = ""
        self._finalize_current_agent_message()

    def settle_answer_before_tools(self) -> None:
        """The streamed answer text is followed by more of the turn (tool calls, another segment): show it as intermediate text.

        Streamed thinking ends there too.
        """
        self._finalize_thinking()
        self._streamed_answer = None
        message = self._current_agent_msg
        if message is None:
            return
        # What follows it starts now: it folds at once, as what it went past does.
        message.settle_intermediate(folded=self._fold_passed_text)
        self._current_agent_msg = None

    async def add_user_message(
        self,
        text: str,
        *,
        is_injection: bool = False,
        created_at: Any = None,
        contents: list[Any] | None = None,
        item_id: str | None = None,
        client_message_id: str | None = None,
    ) -> None:
        """Display a live user or injection message.

        *item_id* is the message's history item when it is already known (a
        consumed injection's); an opener learns it from its TurnStarted, and a
        retry's guidance from the retry's, which names it by its
        *client_message_id*.
        """
        await self._ui.dismiss_welcome_for_content()
        if not is_injection:
            self._ui.on_live_user_turn_started()
            if self._cleanup_failed_input:
                await self._remove_nested_failed_input()
            else:
                status = self._status.failed_turn_status()
                if isinstance(status, (ErrorMessage, InterruptedMessage, RetryMessage)):
                    await remove_shielded(status)
            self._answer_streamed = False
            self._streamed_answer = None
            self._thinking_to_fold = None
            self._group_to_fold = None
        elif self._tools.current_tool_group is not None:
            # The turn goes on below the message: what it went past folds once that starts, as with no message.
            self._group_to_fold = self._tools.current_tool_group
        self._tools.end_group()
        self._finalize_current_agent_message()
        timestamp = format_message_created_at(created_at)

        if is_injection and self._toc.has_items():
            turn_id, _item = self._toc.add_injection(text)
            widget = UserMessage(text, timestamp=timestamp, is_injection=True, contents=contents)
        else:
            turn_id, _item = self._toc.begin_user_turn(text)
            turn_number = _item.turn_index
            widget = UserMessage(
                text,
                timestamp=timestamp,
                contents=contents,
                turn_number=turn_number,
                rollback_available=UserMessage.undo_link_available(turn_number, self._rollback_targets()),
            )
        widget.id = turn_id
        widget.history_item_id = item_id
        widget.client_message_id = client_message_id
        if not is_injection:
            self._pending_user_message = widget
        await self._mount.mount_transcript_widget(widget)
        self._ui.scroll_user_message_to_top(widget)

    async def _remove_nested_failed_input(self) -> None:
        """Preserve nested journals' local cleanup: they have no backend turn-boundary stream."""
        status = self._status.failed_turn_status()
        if not isinstance(status, (ErrorMessage, InterruptedMessage, RetryMessage)):
            return
        prompt = self._status.preceding_content(status)
        tail: list[Widget] = []
        while isinstance(prompt, (ThinkingMessage, UserMessage)):
            tail.append(prompt)
            if isinstance(prompt, UserMessage) and not prompt.is_injection:
                break
            prompt = self._status.preceding_content(prompt)
        await remove_shielded(status)
        if isinstance(prompt, UserMessage) and not prompt.is_injection:
            for widget in tail:
                if isinstance(widget, UserMessage):
                    self._toc.remove_failed_prompt(widget.id)
            await remove_all_shielded(tail)
        else:
            unanswered_end = 0
            for index, widget in enumerate(tail, start=1):
                if isinstance(widget, UserMessage):
                    self._toc.remove_failed_prompt(widget.id)
                    unanswered_end = index
            if unanswered_end:
                await remove_all_shielded(tail[:unanswered_end])

    async def apply_fresh_turn_cleanup(self, *, retired_previous_turn: bool, removed_item_ids: tuple[str, ...]) -> None:
        """Project the backend's cleanup result onto the preceding turn, without judging its output.

        A retired turn goes whole; otherwise only the notes history removed go, matched by their history items.
        """
        pending = self._pending_user_message
        children = self._query.transcript_content_children()
        if pending is None or pending not in children:
            return
        previous = children[: children.index(pending)]
        opener = next(
            (
                index
                for index, widget in reversed(list(enumerate(previous)))
                if isinstance(widget, UserMessage) and not widget.is_injection
            ),
            None,
        )
        if opener is None:
            return
        preceding_turn = previous[opener:]
        if retired_previous_turn:
            removed = [
                widget
                for widget in preceding_turn
                if isinstance(widget, (UserMessage, ThinkingMessage, ErrorMessage, InterruptedMessage, RetryMessage))
            ]
        elif removed_item_ids:
            dropped = set(removed_item_ids)
            selected = [
                widget
                for widget in preceding_turn
                if isinstance(widget, UserMessage) and widget.is_injection and widget.history_item_id in dropped
            ]
            if not selected:
                return
            first = preceding_turn.index(selected[0])
            removed = [
                widget for widget in preceding_turn[first:] if isinstance(widget, ThinkingMessage) or widget in selected
            ]
        else:
            return
        for widget in removed:
            if isinstance(widget, UserMessage):
                self._toc.remove_failed_prompt(widget.id)
        await remove_all_shielded(removed)

    def apply_turn_started(self, turn_number: int, *, opening_item_id: str | None = None) -> None:
        """Bind the live opener and its TOC children to the backend's turn number and history item."""
        message = self._pending_user_message
        if message is None:
            return
        self._pending_user_message = None
        self._toc.apply_turn_marker(turn_number)
        message.turn_number = turn_number
        message.history_item_id = opening_item_id
        message.set_rollback_available(UserMessage.undo_link_available(turn_number, self._rollback_targets()))
        self._ui.scroll_user_message_to_top(message)

    def apply_retry_started(self, note_message_id: str | None, opening_item_id: str | None) -> None:
        """Bind the note the retry names by its client id to the history item the retry opened with."""
        if note_message_id is None:
            return
        for message in reversed(self._query.mounted_user_messages()):
            if message.client_message_id == note_message_id:
                message.history_item_id = opening_item_id
                return

    async def add_answer_delta(self, segment_id: str, text: str) -> None:
        """Stream new answer text into its segment's message, mounting it once the text is not blank."""
        self._answer_streamed = True
        if segment_id != self._segment_id:
            self._segment_id = segment_id
            self._blank_segment_text = ""
        current = self._current_agent_msg
        if current is not None and segment_id in current.segment_ids:
            current.append_text(text)
            self._ui.schedule_anchor_sync()
            return
        self._blank_segment_text += text
        if not self._blank_segment_text.strip():
            return
        await self._status.remove_trailing_status()
        # A failed segment was finalized when it closed: one still current completed, and the turn went on past
        # it. It settles as this text shows, in the same frame as the folds, and stays the answer if none does.
        self.settle_answer_before_tools()
        self._fold_activity()
        message = AgentMessage(
            self._blank_segment_text, is_final=False, profile_name=self._profile_name, segment_ids=(segment_id,)
        )
        self._blank_segment_text = ""
        self._current_agent_msg = message
        self._streamed_answer = message
        await self._mount.mount_transcript_widget(message)
        self._ui.schedule_anchor_sync()

    def fold_before_tools(self) -> None:
        """Settle the answer a tool batch follows, and fold the thinking and text before it, for its group to start below."""
        self.settle_answer_before_tools()
        self.fold_passed_text()

    def _fold_activity(self) -> None:
        """End the current tool group, and fold the thinking and text before streamed text, for the text to start below.

        Only what the text follows is still open: what came before folded as a tool group after it started.
        """
        group = self._tools.current_tool_group
        if group is not None:
            # A control in it may wait for the user: it stays open.
            group.collapsed = True
        self._tools.end_group()
        self.fold_passed_text()

    def fold_passed_text(self) -> None:
        """Fold the thinking, and the tool group a message sent mid-turn closed, the turn has gone on past.

        Whether thinking opened as it streamed, it folds once what follows it starts, as a tool group does, and as
        the text the agent wrote before tool calls does as it settles (:meth:`settle_answer_before_tools`).
        """
        passed = (self._thinking_to_fold, self._group_to_fold)
        self._thinking_to_fold = self._group_to_fold = None
        for widget in passed:
            if widget is not None and widget.is_attached:
                widget.collapsed = True

    def finish_answer_segment(self, segment_id: str, *, completed: bool) -> None:
        """Close a streamed answer segment; a failed or interrupted one keeps its partial text."""
        if segment_id != self._segment_id:
            return
        self._segment_id = ""
        self._blank_segment_text = ""
        if not completed:
            # A completed segment stays current: it is the final answer
            # unless tool calls follow it.
            self._finalize_current_agent_message()

    async def add_thinking_delta(self, segment_id: str, channel: ContentChannel, text: str) -> None:
        """Stream new thinking text into its segment's message, mounting it once the text is not blank.

        Whether it opens is read as it mounts. An answer that streamed before stays as it is: thinking
        does not tell whether the turn goes on past it.
        """
        thinking_expanded = self._thinking_expanded
        if thinking_expanded is None:
            return
        if segment_id != self._thinking_segment_id:
            self._finalize_thinking()
            self._thinking_segment_id = segment_id
        if self._thinking_msg is not None:
            self._thinking_msg.append_text(text)
            if not self._thinking_msg.collapsed:
                self._ui.schedule_anchor_sync()
            return
        self._blank_thinking_text += text
        if not self._blank_thinking_text.strip():
            return
        await self._status.remove_trailing_status()
        self._fold_activity()
        expanded = thinking_expanded()
        message = ThinkingMessage(
            self._blank_thinking_text, channel=channel, segment_id=segment_id, is_final=False, collapsed=not expanded
        )
        self._blank_thinking_text = ""
        self._thinking_msg = message
        self._thinking_to_fold = message
        await self._mount.mount_transcript_widget(message)
        self._ui.schedule_anchor_sync()

    def finish_thinking_segment(self, segment_id: str) -> None:
        """Close a streamed thinking segment; one that failed or was interrupted keeps its partial text."""
        if segment_id == self._thinking_segment_id:
            self._finalize_thinking()

    def _finalize_thinking(self) -> None:
        if self._thinking_msg is not None:
            self._thinking_msg.stream_update(self._thinking_msg.text, is_final=True)
        self._thinking_msg = None
        self._thinking_segment_id = ""
        self._blank_thinking_text = ""

    async def abandon_thinking(self, segment_ids: Iterable[str]) -> None:
        """Remove the thinking a retry gave up with its response: the retry thinks again."""
        abandoned = frozenset(segment_ids)
        if self._thinking_segment_id in abandoned:
            # Its closing event that follows finds no segment to close.
            self._thinking_msg = None
            self._finalize_thinking()
        if self._thinking_to_fold is not None and self._thinking_to_fold.segment_id in abandoned:
            # Removed, it folds no more: held here, it and its body would outlive it.
            self._thinking_to_fold = None
        await remove_all_shielded(
            child
            for child in self._query.direct_children()
            if isinstance(child, ThinkingMessage) and child.segment_id in abandoned
        )

    async def add_agent_message(
        self,
        text: str,
        *,
        streamed: bool = True,
        structured_output_completed: bool = False,
        created_at: Any = None,
        turn_started_at: Any = None,
    ) -> None:
        """Complete the agent's answer: its text already streamed, so only its message finalizes.

        The text is mounted only when no answer streamed (a transcript replayed
        without its deltas) or when it is not the streamed answer (*streamed*
        False: a failure result). Its header tells how long the turn took to it
        from *turn_started_at*, when the pass began, as replay tells it.
        """
        timestamp = format_message_created_at(created_at)
        duration_ms = turn_duration_ms(turn_started_at, created_at)
        streamed_answer, self._streamed_answer = self._streamed_answer, None
        await self._status.remove_trailing_status()
        self._ui.on_final_response_started()
        if self._tools.current_tool_group is not None:
            self._tools.current_tool_group.collapsed = True
        self._tools.end_group()
        self._segment_id = ""
        self._blank_segment_text = ""
        self._finalize_thinking()
        self.fold_passed_text()
        if streamed and self._current_agent_msg is not None:
            self._current_agent_msg.stream_update(
                self._current_agent_msg.text, is_final=True, timestamp=timestamp, duration_ms=duration_ms
            )
            self._current_agent_msg = None
            self._ui.schedule_anchor_sync()
            return
        self._finalize_current_agent_message()
        final_text = "" if (streamed and self._answer_streamed) or not text.strip() else text
        if not final_text and not structured_output_completed:
            if streamed and streamed_answer is not None and streamed_answer.is_attached:
                # Its text already showed whole: only its header learns when the turn completed it.
                streamed_answer.set_timestamp(timestamp, duration_ms=duration_ms)
            return
        msg = AgentMessage(
            final_text or "✓",
            is_final=True,
            profile_name=self._profile_name,
            is_structured_completion=structured_output_completed and not final_text,
            timestamp=timestamp,
            duration_ms=duration_ms,
        )
        await self._mount.mount_transcript_widget(msg)
        self._ui.schedule_anchor_sync()

    async def add_context_fold(
        self,
        compressed_context_id: str,
        summary: str,
        freed_messages: int = 0,
        turn_range: tuple[int, int] = (0, 0),
    ) -> None:
        """Display a context compression event as fresh run output."""
        await self._status.remove_trailing_status()
        # A failed stream may leave this cursor pointing at its partial
        # AgentMessage.  The fold is a presentation boundary: recovered text
        # must mount below it instead of updating that stale widget in place.
        # Finalize the visible partial first so its streaming cursor cannot
        # outlive the owner reference discarded below.
        self._finalize_current_agent_message()
        self._tools.end_group()
        widget = ContextFoldWidget(
            compressed_context_id,
            summary,
            freed_messages,
            turn_range,
            resolve_message=self._resolve_message,
        )
        await self._mount.mount_transcript_widget(widget)

    async def add_compaction_start(self, compaction_id: str) -> None:
        """Mount a live Phase-4 compaction card, closing the open tool group.

        Compaction runs between model calls, so every tool in the current
        group has already completed — collapse it (matching the intermediate
        agent-text transition) and let the next tool batch open a fresh
        group below the card.
        """
        await self._status.remove_trailing_status()
        self._finalize_current_agent_message()
        if self._tools.current_tool_group is not None:
            self._tools.current_tool_group.collapsed = True
        self._tools.end_group()
        card = CompactionCard(compaction_id)
        self._compaction_cards[compaction_id] = card
        await self._mount.mount_transcript_widget(card)
        self._ui.schedule_anchor_sync()

    def complete_compaction(
        self,
        compaction_id: str,
        *,
        outcome: str,
        duration_ms: int = 0,
        last_words: str = "",
        format_violation: str = "",
        failure_reason: str = "",
    ) -> None:
        """Finalize the matching live compaction card, if still tracked.

        ``format_violation`` stays in the event payload for surfaces that
        narrate it (sub-agent lines, the ACP bridge) but is deliberately not
        shown on the main card: the compaction succeeded, so the summary
        stands without a lingering warning.
        """
        del format_violation
        card = self._compaction_cards.pop(compaction_id, None)
        if card is None:
            return
        card.set_complete(
            outcome=outcome,
            duration_ms=duration_ms,
            last_words=last_words,
            failure_reason=failure_reason,
        )
        self._ui.schedule_anchor_sync()

    def show_compaction_retry(self, message: str, attempt: int, max_attempts: int, delay_seconds: int) -> None:
        """Route a Phase-4 side-call retry notice onto the running compaction card.

        Retry events carry no compaction id, but main-agent compactions are
        strictly sequential — at most one tracked card is still running.
        With no live card (stale event) the notice is dropped.
        """
        for card in reversed(self._compaction_cards.values()):
            if card.status == "running":
                card.show_retry_notice(message, attempt, max_attempts, delay_seconds)
                return

    def _finalize_dangling_compactions(self) -> None:
        """Mark still-running compaction cards interrupted (run ended first)."""
        for card in self._compaction_cards.values():
            card.set_complete(outcome="canceled")
        self._compaction_cards.clear()

    async def add_system(self, text: str, *, key: str | None = None, warning: bool = False) -> None:
        """Display a system message."""
        self._tools.end_group()
        msg = SystemMessage(text, warning=warning)
        if key is not None:
            msg.id = key
        await self._mount.mount_transcript_widget(msg)

    async def update_system(self, key: str, new_text: str) -> None:
        """Update the first system message matching ``key``."""
        for msg in self._query.direct_children():
            if isinstance(msg, SystemMessage) and msg.id == key:
                msg.update(Text(f"  ◦ {new_text}"))
                break

    async def remove_system(self, key: str) -> None:
        """Remove the first system message matching ``key``."""
        for msg in self._query.direct_children():
            if isinstance(msg, SystemMessage) and msg.id == key:
                await remove_shielded(msg)
                break

    def _reason_text(self, reason: MessageRef | str | None) -> str:
        """Resolve an interruption's reason in the transcript's own locale.

        No reason names the shared interruption wording the replay path shows for the same marker.
        """
        if reason is None:
            return self._resolve_message(EXECUTION_INTERRUPTED_MESSAGE.bind())
        if isinstance(reason, str):
            return reason
        return self._resolve_message(reason)

    def _action_label_text(self, action_label: MessageRef | str | None) -> str | None:
        """Resolve an inline status action's label in the transcript's own locale, None keeping no action."""
        if action_label is None:
            return None
        if isinstance(action_label, str):
            return action_label
        return self._resolve_message(action_label)

    async def add_interrupted(
        self,
        reason: MessageRef | str | None = None,
        source: str = "user",
        *,
        show_action: bool = True,
        stopped_at: float | None = None,
    ) -> None:
        """Display an interruption notice, replacing any trailing status widget.

        *stopped_at* is when the interrupt stopped the calls still running (``time.monotonic()``), now if not given.
        """
        self.stop_answer()
        await self.cancel_running_tools(stopped_at=stopped_at)
        await self._status.remove_trailing_status()

        copy = resolve_interrupted_message_copy(self._reason_text(reason), source, self._resolve_message)
        label = copy.retry_action if source == "error" else copy.continue_action
        msg = InterruptedMessage(
            copy.reason,
            source,
            action_label=label if show_action else None,
            header=copy.header,
            offers_discard=show_action and source != "error",
        )
        await self._mount.mount_transcript_widget(msg)
        self._ui.on_status_message_mounted()

    async def add_error(
        self,
        text: str,
        *,
        action_label: MessageRef | str | None = ERROR_RETRY_ACTION,
        stopped_at: float | None = None,
    ) -> None:
        """Display an error message, replacing any trailing status widget.

        *stopped_at* is when the error stopped the calls still running (``time.monotonic()``), now if not given.
        """
        self.stop_answer()
        await self.cancel_running_tools(stopped_at=stopped_at)
        await self._status.remove_trailing_status()

        msg = ErrorMessage(text, action_label=self._action_label_text(action_label))
        await self._mount.mount_transcript_widget(msg)
        self._ui.on_status_message_mounted()

    async def add_retry(
        self,
        message: str,
        attempt: int,
        max_attempts: int,
        delay_seconds: int,
        *,
        deadline: float | None = None,
        wait_id: str = "",
    ) -> None:
        """Display a retry notice, replacing any trailing status widget.

        With the wait's *deadline* (``time.monotonic()``) it counts down; see :class:`RetryMessage`.
        """
        await self._status.remove_trailing_status()
        self._tools.end_group()
        msg = RetryMessage(message, attempt, max_attempts, delay_seconds, deadline=deadline, wait_id=wait_id)
        await self._mount.mount_transcript_widget(msg)
        self._ui.on_status_message_mounted()

    async def prepare_retry(self, *, stopped_at: float | None = None) -> None:
        """Finalize pending widgets before a main-agent stream retry."""
        self._tools.cancel_running_and_end_group(stopped_at=stopped_at)
        self._finalize_current_agent_message()

    async def cancel_running_tools(self, *, stopped_at: float | None = None) -> None:
        """Cancel all in-progress tool calls, stopped at *stopped_at* (``time.monotonic()``), now if not given."""
        self._tools.cancel_running_and_end_group(stopped_at=stopped_at)
        self._finalize_dangling_compactions()
