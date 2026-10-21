# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""User submit, injection, retry, and interrupt flow for the main screen."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

from chrys.app.tui.screens.main.ports import InputFlowView, StartWorker
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState, SubmitKind
from chrys.app.tui.support.gc_freeze import GcAbsorbReason, GcAbsorbRequested
from chrys.app.tui.widgets.chrome.input_bar import INPUT_CONTINUE
from chrys.app.tui.widgets.chrome.status_bar import STATUS_RESUMING, STATUS_THINKING
from chrys.foundation.events.types import (
    Error,
    InvocationMessage,
    UserInjectCancel,
    UserInterrupt,
    UserMessage,
    UserRetry,
)

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.text.conversation_selections import Quote


class InputFlowController:
    """Own the temporal contracts around user submits and turn rendering."""

    def __init__(
        self,
        *,
        state: MainScreenState,
        services: MainScreenServices,
        view: InputFlowView,
        start_worker: StartWorker,
        handle_agent_message: Callable[[InvocationMessage], Awaitable[None]],
        handle_error: Callable[[Error], Awaitable[None]],
        set_agent_running: Callable[[bool], None],
        set_has_messages: Callable[[bool], None],
        clear_workspace_marker: Callable[[], None],
        clear_pending_questions: Callable[[], None],
        commit_profile_switch_marker: Callable[[], None],
        post_gc_message: Callable[[GcAbsorbRequested], object],
        debug: Callable[[str, str], None],
    ) -> None:
        self._state = state
        self._services = services
        self._view = view
        self._quotes = state.quotes
        self._start_worker = start_worker
        self._handle_agent_message = handle_agent_message
        self._handle_error = handle_error
        self._set_agent_running = set_agent_running
        self._set_has_messages = set_has_messages
        self._clear_workspace_marker = clear_workspace_marker
        self._clear_pending_questions = clear_pending_questions
        self._commit_profile_switch_marker = commit_profile_switch_marker
        self._post_gc_message = post_gc_message
        self._debug = debug

    @property
    def _bus(self) -> EventBus:
        return self._services.bus

    def submit_user_text(self, text: str) -> None:
        """Submit normal user text as a new turn or mid-run injection."""
        if self._state.run.agent_running:
            self._start_worker(partial(self.queue_injection, text))
            return
        self._start_worker(partial(self.send_user_message, text))

    def request_retry(self, text: str = "") -> None:
        """Schedule retry/continue from a sync Textual event handler."""
        prepared = self._begin_retry(text)
        if prepared is not None:
            event, sent_quotes = prepared
            self._start_worker(partial(self._publish_retry, event, sent_quotes, text))

    def _claim_submit(self, text: str, *, kind: SubmitKind) -> bool:
        """Make *text* the one submit in flight, or hand it back to the composer.

        Called before the worker's first await, so of two submits scheduled in
        the same tick only the first claims: the one in flight owns the submit
        state, and its cleanup would erase a second one's refusal. A running
        turn's Enter never cleared the composer; a new turn's did, so the text
        coming back says why — until the first submit's own outcome shows.
        """
        if self._state.submit.active:
            if self._view.restore_input_draft(text):
                self._view.flash_submit_still_sending()
            return False
        self._state.submit.begin(text, kind=kind)
        return True

    async def _publish_submit(self, event: UserMessage | UserRetry) -> bool:
        """Publish the submit in flight; return whether the backend refused it.

        ``EventBus.publish`` awaits its handlers in order, so a refusal reaches
        the frontend on this task before the publish returns.
        """
        self._state.submit.publish_from_current_task()
        await self._bus.publish(event)
        return self._state.submit.blocked

    async def send_user_message(self, text: str) -> None:
        """Publish and render a normal user message."""
        if self._state.run.agent_loading:
            return
        if not self._claim_submit(text, kind="turn"):
            return
        # Pending quotes ride the wire text; the bubble renders what was sent,
        # while history and the terminal title keep what the user typed. The
        # snapshot settles the consume below: quotes added while the publish
        # awaited hooks or image preparation stay pending for the next message.
        wire_text, sent_quotes = self._quotes.compose(text)
        try:
            event = UserMessage(text=wire_text)
            self._state.render_gate.begin()
            if await self._publish_submit(event):
                self._state.render_gate.finish(rendered=False)
                return
        finally:
            self._state.submit.clear()
        self._quotes.consume(sent_quotes)
        if not event.started_turn:
            # Neither refused nor a new turn: the run seen ending here was still
            # finishing and took the message, or the backend dropped it. The
            # text is the user's again; if that run used it, its outcome shows
            # it in the transcript and clears the composer. The wire text comes
            # back so quoted content stays editable rather than vanishing.
            self._state.render_gate.finish(rendered=False)
            self._view.restore_input_draft(wire_text)
            self._debug("UserMessage (no turn)", text[:60])
            return
        await self._open_turn(event, typed_text=text)
        self._debug("UserMessage", text[:60])

    async def _open_turn(self, event: UserMessage, *, typed_text: str | None = None) -> None:
        """Show *event* as the opener of the turn its publish just started.

        Called right after that publish returns, before the new run's task has
        had a chance to run, with the render gate up: the run's first events
        then find the turn live and wait for the bubble. ``typed_text`` is the
        composer's text behind the event (history and terminal title); the
        event's own text is what the model received and the bubble renders.
        """
        text = event.text if typed_text is None else typed_text
        self._state.run.started_at = event.timestamp
        self._view.set_terminal_title_for_user_message(text)
        self._set_agent_running(True)
        self._set_has_messages(True)
        self._clear_workspace_marker()
        self._view.add_input_history(text, session_id=self._view.current_chat_session_id() or None)
        self._view.set_retry_mode(False)
        self._view.start_run_status(STATUS_THINKING.bind())
        self._commit_profile_switch_marker()

        rendered = False
        try:
            # EventBus.publish is sequential; the backend fills prepared_contents
            # before returning so this previews exactly what the model receives.
            await self._view.render_user_message(
                event.text, created_at=event.timestamp, contents=event.prepared_contents
            )
            rendered = True
        finally:
            self._state.render_gate.finish(rendered=rendered)

        await self.flush_deferred_agent_messages()
        self._view.update_toc()

    async def queue_injection(self, text: str) -> None:
        """Queue a user message for mid-run injection without chat display."""
        if not self._claim_submit(text, kind="injection"):
            return
        # The wire text is what the engine holds. The staged quotes ride it
        # and are restored to the chip when the injection is cancelled or
        # abandoned — the locked composer only ever shows the typed text.
        wire_text, sent_quotes = self._quotes.compose(text)
        try:
            injection_id = uuid4().hex[:12]
            self._view.lock_input_with_text()
            self._state.pending_injection.begin(injection_id, wire_text, quotes=sent_quotes)
            event = UserMessage(text=wire_text, injection_id=injection_id)
            if await self._publish_submit(event):
                self._state.pending_injection.clear()
                return
        finally:
            self._state.submit.clear()
        if event.started_turn:
            # The run ended before the backend admitted this message, so it
            # opened a new turn with it: show that turn as a new turn's Enter
            # would. The run's end or an Esc handed the composer back meanwhile,
            # so only the sent text leaves it. The model saw the text, quotes
            # included — even a cancel fired during the admission above cannot
            # take them back.
            self._quotes.consume(sent_quotes)
            if self._state.pending_injection.matches(injection_id):
                self._state.pending_injection.clear()
                self._view.unlock_input_clearing(wire_text)
            self._state.render_gate.begin()
            await self._open_turn(event, typed_text=text)
            self._debug("UserMessage (inject → new turn)", text[:60])
            return
        # Queued on the live run. A cancel pressed while the publish awaited
        # already restored the quotes; consuming unconditionally here would
        # wipe them again, and the abandoned result cannot undo it (stale id).
        if self._state.pending_injection.matches(injection_id):
            self._quotes.consume(sent_quotes)
        self._debug("UserMessage (inject)", text[:60])

    def cancel_pending_injection(self) -> bool:
        """Withdraw the queued injection and hand the text back for editing.

        Optimistically unlocks the input right away — the backend cancel is
        best-effort, and a too-late (already consumed) outcome arrives as a
        ``consumed=True`` result that no longer touches the input bar.
        """
        pending = self._state.pending_injection
        if not pending.active:
            return False
        injection_id = pending.injection_id
        staged_quotes = pending.quotes
        pending.clear()
        self._quotes.restore(staged_quotes)
        self._view.unlock_input_keep_if_locked()
        if injection_id is not None:
            self._start_worker(partial(self._publish_inject_cancel, injection_id))
        return True

    async def _publish_inject_cancel(self, injection_id: str) -> None:
        """Publish the backend cancel for a withdrawn injection."""
        await self._bus.publish(UserInjectCancel(injection_id=injection_id))
        self._debug("UserInjectCancel", injection_id)

    async def flush_deferred_agent_messages(self) -> None:
        """Render backend events that arrived while the user bubble was mounting."""
        for event in self._state.render_gate.consume_deferred():
            if isinstance(event, InvocationMessage):
                await self._handle_agent_message(event)
            elif isinstance(event, Error):
                await self._handle_error(event)

    async def publish_interrupt(self) -> None:
        """Publish an interrupt and update local UI state after the backend sees it."""
        if not self._state.run.agent_running:
            return

        await self._bus.publish(UserInterrupt())
        terminal_request = GcAbsorbRequested(GcAbsorbReason.TURN_TERMINAL, terminal_boundary=True)
        try:
            self._clear_pending_questions()
            self._view.flash_interrupted()
            self._view.clear_ask_user_inline_prompts()
            await self._view.render_interrupted()
        finally:
            self._view.clear_terminal_title_result()
            self._set_agent_running(False)
            # Teardown events (late tool results, compaction finishing) that
            # raced the awaits above saw ``agent_running`` still True and may
            # have flipped the status bar back to run mode; now that the gate
            # is closed, re-assert the interrupted flash so nothing leaves a
            # stuck "Thinking" spinner behind.
            self._view.flash_interrupted()
            # The interrupt already unlocks the input with the text kept; drop
            # pending-injection tracking so a later abandoned result (or Esc)
            # cannot act on an entry that no longer belongs to a live turn,
            # and hand a still-queued injection's quotes back for editing.
            staged_quotes = self._state.pending_injection.quotes
            self._state.pending_injection.clear()
            self._quotes.restore(staged_quotes)
            self._view.unlock_input_keep_if_locked()
        self._post_gc_message(terminal_request)
        self._view.set_retry_mode(True, label=INPUT_CONTINUE.bind())
        self._debug("UserInterrupt", "")

    def _begin_retry(self, text: str) -> tuple[UserRetry, tuple[Quote, ...]] | None:
        """Reserve admission before scheduling, including same-tick clicks."""
        if self._state.run.agent_running or self._state.submit.active:
            return None

        # Pending quotes ride the retry note like a normal message; history
        # and the terminal title keep what the user typed.
        wire_text, sent_quotes = self._quotes.compose(text)
        event = UserRetry(text=wire_text)
        self._state.render_gate.begin()
        self._state.submit.begin(text, kind="retry")
        self._view.set_retry_pending(True)
        return event, sent_quotes

    async def _publish_retry(self, event: UserRetry, sent_quotes: tuple[Quote, ...], typed_text: str) -> None:
        try:
            if await self._publish_submit(event):
                self._state.render_gate.finish(rendered=False)
                return
        except BaseException:
            self._state.render_gate.finish(rendered=False)
            raise
        finally:
            self._state.submit.clear()
            self._view.set_retry_pending(False)

        self._quotes.consume(sent_quotes)
        self._view.hide_trailing_status_action()
        self._state.run.started_at = event.timestamp
        self._set_agent_running(True)
        self._view.start_run_status(STATUS_RESUMING.bind())

        if event.text:
            # A quotes-only retry (empty typed text) still renders its note;
            # the title and history stay keyed to text the user typed.
            if typed_text:
                self._view.set_terminal_title_for_user_message(typed_text)
                self._view.add_input_history(typed_text, session_id=self._view.current_chat_session_id() or None)
            rendered = False
            try:
                await self._view.render_user_retry_note(event.text, created_at=event.timestamp)
                rendered = True
            finally:
                self._state.render_gate.finish(rendered=rendered)
            await self.flush_deferred_agent_messages()
            self._view.update_toc()
        else:
            self._state.render_gate.finish(rendered=True)
            await self.flush_deferred_agent_messages()

        self._debug("UserRetry", typed_text[:60] if typed_text else "")
