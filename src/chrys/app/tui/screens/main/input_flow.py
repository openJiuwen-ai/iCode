# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""User submit, injection, retry, and interrupt flow for the main screen.

A prompt shortcut's draft is sent like any message. If it switches the
agent or model, that goes to the engine first as one step
(``SessionOnlySwitch``); if it holds ``!`command``` placeholders, the message
carries its shortcut and the engine runs them while admitting it, saying so
(``PromptCommandsRunning`` / ``PromptCommandsEnded``) and taking a withdraw
(Esc) meanwhile. The composer clears at Enter as for any message; whatever
keeps the message from going out hands the draft back whole, once.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import partial
from typing import TYPE_CHECKING
from uuid import uuid4

from chrys.app.tui.screens.main.ports import InputFlowView, StartWorker
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState, SubmitKind
from chrys.app.tui.support.gc_freeze import GcAbsorbReason, GcAbsorbRequested
from chrys.app.tui.widgets.chat.replay import contents_text
from chrys.app.tui.widgets.chrome.input_bar import INPUT_CONTINUE, ChatDraft
from chrys.app.tui.widgets.chrome.status_bar import STATUS_RESUMING, STATUS_THINKING
from chrys.foundation.events.types import (
    Error,
    InvocationContentAbandoned,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationMessage,
    PromptCommandsEnded,
    PromptCommandsRunning,
    PromptShortcutRef,
    SessionOnlySwitch,
    UserInjectCancel,
    UserInterrupt,
    UserMessage,
    UserRetry,
)
from chrys.foundation.i18n import msg
from chrys.service.prompt_shortcuts.shell import shell_commands

if TYPE_CHECKING:
    from chrys.app.tui.screens.main.prompt_card import PendingSwitch
    from chrys.app.tui.screens.main.state import DeferredTurnEvent
    from chrys.foundation.events.bus import EventBus

_RUNNING_COMMANDS = msg(
    "tui.input.running_commands",
    fallback="Running {count} command… Esc to cancel",
    plural_fallback="Running {count} commands… Esc to cancel",
)


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
        handle_agent_content: Callable[
            [InvocationContentDelta | InvocationContentFinished | InvocationContentAbandoned], Awaitable[None]
        ],
        handle_error: Callable[[Error], Awaitable[None]],
        set_agent_running: Callable[[bool], None],
        set_has_messages: Callable[[bool], None],
        clear_workspace_marker: Callable[[], None],
        clear_pending_questions: Callable[[], None],
        commit_profile_switch_marker: Callable[[], None],
        post_gc_message: Callable[[GcAbsorbRequested], object],
        debug: Callable[[str, str], None],
        pending_switch: Callable[[ChatDraft], PendingSwitch | None],
        model_left_out: Callable[[], None],
        show_error_notice: Callable[[Error], None],
    ) -> None:
        self._state = state
        self._services = services
        self._view = view
        self._start_worker = start_worker
        self._handle_agent_message = handle_agent_message
        self._handle_agent_content = handle_agent_content
        self._handle_error = handle_error
        self._set_agent_running = set_agent_running
        self._set_has_messages = set_has_messages
        self._clear_workspace_marker = clear_workspace_marker
        self._clear_pending_questions = clear_pending_questions
        self._commit_profile_switch_marker = commit_profile_switch_marker
        self._post_gc_message = post_gc_message
        self._debug = debug
        self._pending_switch = pending_switch
        self._model_left_out = model_left_out
        self._show_error_notice = show_error_notice
        self._commands_status = False

    @property
    def _bus(self) -> EventBus:
        return self._services.bus

    def submit_user_text(self, text: str, draft: ChatDraft | None = None) -> None:
        """Submit normal user text as a new turn or mid-run injection; *draft* is the composer's whole draft."""
        if self._state.run.agent_running:
            self._start_worker(partial(self.queue_injection, text))
            return
        self._start_worker(partial(self.send_user_message, text, draft))

    def request_retry(self, text: str = "", draft: ChatDraft | None = None) -> None:
        """Schedule retry/continue from a sync Textual event handler; *draft* is the composer's whole draft."""
        event = self._begin_retry(text, draft)
        if event is not None:
            self._start_worker(partial(self._publish_retry, event))
            return
        if not text or (self._state.submit.active and self._state.submit.kind == "retry"):
            # A second Continue for the retry in flight does nothing: that retry carries the same note.
            return
        # A turn began after the keypress, or a message is still on its way (a prompt
        # shortcut's admission can take a while): the typed note comes back, as for Enter.
        if self._view.restore_input_draft(draft if draft is not None else text) and self._state.submit.active:
            self._view.flash_submit_still_sending()

    def withdraw_submit(self) -> bool:
        """Esc while a message carrying a prompt shortcut is on its way: withdraw it; False for anything else.

        Owns Esc for that whole time: the first one withdraws (a message not yet
        published is never published; one the engine is admitting is told to
        stop, its commands with it), later ones do nothing.
        """
        submit = self._state.submit
        if not submit.active or submit.message_id is None:
            return False
        if not submit.withdrawn:
            submit.withdrawn = True
            self._start_worker(partial(self._bus.publish, UserInjectCancel(injection_id=submit.message_id)))
        return True

    def _claim_submit(self, text: str, *, kind: SubmitKind, draft: ChatDraft | None = None) -> bool:
        """Make *text* the one submit in flight, or hand it back to the composer.

        Called before the worker's first await, so of two submits scheduled in
        the same tick only the first claims: the one in flight owns the submit
        state, and its cleanup would erase a second one's refusal. A running
        turn's Enter never cleared the composer; a new turn's did, so the text
        coming back says why — until the first submit's own outcome shows.
        """
        if self._state.submit.active:
            if self._view.restore_input_draft(draft if draft is not None else text):
                self._view.flash_submit_still_sending()
            return False
        self._begin_claim(text, kind=kind, draft=draft)
        return True

    def _begin_claim(self, text: str, *, kind: SubmitKind, draft: ChatDraft | None = None) -> None:
        """Claim the submit in flight with what it hands back if it does not go out: *draft*, else plain *text*."""
        # Where a shortcut's draft was taken, read only for one: it goes back without its owner elsewhere.
        generation, cwd = self._place() if draft is not None and draft.owner is not None else (0, "")
        self._state.submit.begin(text, kind=kind, draft=draft, session_generation=generation, cwd=cwd)

    def _place(self) -> tuple[int, str]:
        """The engine's session generation and the working folder, now."""
        return self._services.session_generation(), self._state.workspace.current_cwd

    def _give_back_draft(self) -> None:
        """Hand the claimed draft back whole, if no refusal handler took it already."""
        draft = self._state.submit.take_draft_for(self._place)
        if draft is not None:
            self._view.restore_input_draft(draft)

    def _finish_unrendered(self) -> None:
        """The submit opened no turn: stop deferring, and show any error held back meanwhile as a notice."""
        for error in self._state.render_gate.finish(rendered=False):
            self._show_error_notice(error)

    async def _publish_submit(self, event: UserMessage | UserRetry | SessionOnlySwitch) -> bool:
        """Publish the submit in flight; return whether the backend refused it.

        ``EventBus.publish`` awaits its handlers in order, so a refusal reaches
        the frontend on this task before the publish returns.
        """
        self._state.submit.publish_from_current_task()
        await self._bus.publish(event)
        return self._state.submit.blocked

    async def send_user_message(self, text: str, draft: ChatDraft | None = None) -> None:
        """Publish and render a normal user message, a prompt shortcut's draft included.

        A draft that switches the agent or model has the engine do that first,
        as one step: the message goes only once it did, and in the same
        session. One whose text holds ``!`command``` placeholders carries its
        shortcut, so the engine runs them while it admits the message.
        """
        if self._state.run.agent_loading:
            # A load began after the keypress, before this worker ran: the draft waits in the composer.
            self._view.restore_input_draft(draft if draft is not None else text)
            return
        if not self._claim_submit(text, kind="turn", draft=draft):
            return
        submit = self._state.submit
        ref = _shortcut_ref(draft, text)
        if ref is not None:
            submit.message_id = uuid4().hex[:12]
        event = UserMessage(text=text, injection_id=submit.message_id, prompt_shortcut=ref)
        published = False
        blocked = False
        try:
            switch = self._pending_switch(draft) if draft is not None else None
            if (switch is None or await self._switch_session(switch)) and not submit.withdrawn:
                self._state.render_gate.begin()
                published = True
                blocked = await self._publish_submit(event)
        finally:
            self._clear_commands_status()
            if not (published and event.started_turn):
                # Refused, withdrawn, switched nowhere, or taken by a run still
                # finishing: the draft is the user's again (once: a refusal's
                # handler may have handed it back already).
                self._give_back_draft()
            self._state.submit.clear()
        if published and (blocked or not event.started_turn):
            # Neither refused nor a new turn: the run seen ending here was still
            # finishing and took the message, or the backend dropped it. If that
            # run used it, its outcome shows it in the transcript and clears the composer.
            self._finish_unrendered()
        if not (published and event.started_turn):
            self._debug("UserMessage (no turn)", text[:60])
            return
        await self._open_turn(event)
        self._debug("UserMessage", text[:60])

    async def _switch_session(self, switch: PendingSwitch) -> bool:
        """Have the engine switch this session's own agent and model; whether it did, in this same session."""
        generation = self._services.session_generation()
        event = SessionOnlySwitch(agent=switch.agent, model_profile=switch.model_profile)
        if await self._publish_submit(event):
            return False
        if event.model_left_out:
            self._model_left_out()
        return event.applied and self._services.session_generation() == generation

    async def on_prompt_commands_running(self, event: PromptCommandsRunning) -> None:
        """The engine runs the commands of the message in flight: say so until they end."""
        if event.injection_id is None or event.injection_id != self._state.submit.message_id:
            return
        self._commands_status = True
        self._view.show_status(_RUNNING_COMMANDS.bind(count=event.count))

    async def on_prompt_commands_ended(self, event: PromptCommandsEnded) -> None:
        if event.injection_id is not None and event.injection_id == self._state.submit.message_id:
            self._clear_commands_status()

    def _clear_commands_status(self) -> None:
        if self._commands_status:
            self._commands_status = False
            self._view.clear_status()

    async def _open_turn(self, event: UserMessage) -> None:
        """Show *event* as the opener of the turn its publish just started.

        Called right after that publish returns, before the new run's task has
        had a chance to run, with the render gate up: the run's first events
        then find the turn live and wait for the bubble.
        """
        text = event.text
        # What the model receives (a prompt shortcut's command output included), as a
        # restored session shows it; the title and the input history keep what was typed.
        shown = contents_text(event.prepared_contents) if event.prepared_contents is not None else text
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
                shown or text, created_at=event.timestamp, contents=event.prepared_contents
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
        try:
            injection_id = uuid4().hex[:12]
            self._view.lock_input_with_text()
            self._state.pending_injection.begin(injection_id, text)
            event = UserMessage(text=text, injection_id=injection_id)
            if await self._publish_submit(event):
                self._state.pending_injection.clear()
                return
        finally:
            self._state.submit.clear()
        if event.started_turn:
            # The run ended before the backend admitted this message, so it
            # opened a new turn with it: show that turn as a new turn's Enter
            # would. The run's end or an Esc handed the composer back meanwhile,
            # so only the sent text leaves it.
            if self._state.pending_injection.matches(injection_id):
                self._state.pending_injection.clear()
                self._view.unlock_input_clearing(text)
            self._state.render_gate.begin()
            await self._open_turn(event)
            self._debug("UserMessage (inject → new turn)", text[:60])
            return
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
        pending.clear()
        self._view.unlock_input_keep_if_locked()
        if injection_id is not None:
            self._start_worker(partial(self._publish_inject_cancel, injection_id))
        return True

    async def _publish_inject_cancel(self, injection_id: str) -> None:
        """Publish the backend cancel for a withdrawn injection."""
        await self._bus.publish(UserInjectCancel(injection_id=injection_id))
        self._debug("UserInjectCancel", injection_id)

    async def flush_deferred_agent_messages(self) -> None:
        """Render backend events that arrived while the user bubble was mounting, in arrival order."""
        await self._state.render_gate.drain(self._replay_deferred)

    async def _replay_deferred(self, event: DeferredTurnEvent) -> None:
        if isinstance(event, InvocationMessage):
            await self._handle_agent_message(event)
        elif isinstance(event, InvocationContentDelta | InvocationContentFinished | InvocationContentAbandoned):
            await self._handle_agent_content(event)
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
            # cannot act on an entry that no longer belongs to a live turn.
            self._state.pending_injection.clear()
            self._view.unlock_input_keep_if_locked()
        self._post_gc_message(terminal_request)
        self._view.set_retry_mode(True, label=INPUT_CONTINUE.bind())
        self._debug("UserInterrupt", "")

    def _begin_retry(self, text: str, draft: ChatDraft | None = None) -> UserRetry | None:
        """Reserve admission before scheduling, including same-tick clicks."""
        if self._state.run.agent_running or self._state.submit.active:
            return None

        event = UserRetry(text=text)
        self._state.render_gate.begin()
        # A refusal gives back the whole draft, its unfilled fields included, as for Enter.
        self._begin_claim(text, kind="retry", draft=draft)
        self._view.set_retry_pending(True)
        return event

    async def _publish_retry(self, event: UserRetry) -> None:
        text = event.text
        try:
            if await self._publish_submit(event):
                self._finish_unrendered()
                return
        except BaseException:
            self._state.render_gate.finish(rendered=False)
            raise
        finally:
            self._state.submit.clear()
            self._view.set_retry_pending(False)

        self._view.hide_trailing_status_action()
        self._state.run.started_at = event.timestamp
        self._set_agent_running(True)
        self._view.start_run_status(STATUS_RESUMING.bind())

        if text:
            self._view.set_terminal_title_for_user_message(text)
            self._view.add_input_history(text, session_id=self._view.current_chat_session_id() or None)
            rendered = False
            try:
                await self._view.render_user_retry_note(text, created_at=event.timestamp)
                rendered = True
            finally:
                self._state.render_gate.finish(rendered=rendered)
            await self.flush_deferred_agent_messages()
            self._view.update_toc()
        else:
            # The backend withdraws the answer it drops to ask for it again (InvocationAnswerWithdrawn).
            await self.flush_deferred_agent_messages()

        self._debug("UserRetry", text[:60] if text else "")


def _shortcut_ref(draft: ChatDraft | None, text: str) -> PromptShortcutRef | None:
    """The shortcut a message carries: only a shortcut's draft whose text holds commands for the engine to run."""
    owner = draft.owner if draft is not None else None
    if owner is None or not shell_commands(text):
        return None
    return PromptShortcutRef(name=owner.name, path=owner.path)
