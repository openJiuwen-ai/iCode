# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for what the backend does with a message sent while a turn runs.

It may refuse it, queue it for the turn, or — when the turn ended first —
open a new turn with it. Only the backend's own answer, given inside the
message's publish, decides what the screen shows.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chrys.app.tui.screens.dialogs.vision_unsupported import VisionUnsupportedDialog
from chrys.app.tui.screens.main import MainScreen
from chrys.app.tui.screens.main.event_handlers import BackendEventHandler
from chrys.app.tui.screens.main.input_flow import InputFlowController
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState, RunState
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    Error,
    InvocationMessage,
    UserInjectResult,
    UserInterrupt,
    UserMessage,
    Warning,
)
from chrys.foundation.i18n import MessageRef
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.text.conversation_selections import Quote, compose_outgoing_text
from tests.support.tui_app_harness import SessionGenerationEngine, make_chrys_app
from tests.support.tui_helpers import click_when_settled, fake_session_title, make_backend_handler
from tests.support.waiting import wait_for

_BLOCK_REASON = "hook 'guard' blocked: denied"
_TEXT = "also check the logs"


class _InputBar:
    """The composer: an injection keeps its text while the bar is locked."""

    def __init__(self) -> None:
        self.value = ""
        self.locked = False

    def lock_with_text(self) -> None:
        self.locked = True

    def unlock_and_keep(self) -> None:
        self.locked = False

    def unlock_and_clear(self) -> None:
        self.locked = False
        self.value = ""

    def restore_draft(self, text: str) -> bool:
        if not text or self.value:
            return False
        self.value = text
        return True


class _InputFlowView:
    """The input-flow surface MainScreen's view adapter provides, over the same composer."""

    def __init__(self, input_bar: _InputBar, shown: list[tuple[str, str]]) -> None:
        self._input_bar = input_bar
        self._shown = shown
        self.interrupted_renders = 0
        self.input_history: list[str] = []
        self.run_status: list[str] = []
        self.still_sending_flashes = 0

    def set_terminal_title_for_user_message(self, text: str) -> None:
        self._shown.append(("title", text))

    def clear_terminal_title_result(self) -> None:
        return None

    def lock_input_with_text(self) -> None:
        self._input_bar.lock_with_text()

    def unlock_input_keep_if_locked(self) -> None:
        if self._input_bar.locked:
            self._input_bar.unlock_and_keep()

    def unlock_input_clearing(self, text: str) -> None:
        if self._input_bar.locked or self._input_bar.value.strip() == text:
            self._input_bar.unlock_and_clear()

    def current_chat_session_id(self) -> str:
        return "session-1"

    def add_input_history(self, text: str, *, session_id: str | None) -> None:
        self.input_history.append(text)

    def start_run_status(self, label: MessageRef | str) -> None:
        self.run_status.append(_text(label))

    async def render_user_message(self, text: str, *, created_at: object, contents: list[Any] | None) -> None:
        # Mounting yields to the loop, where the new run's task gets to run.
        await asyncio.sleep(0)
        self._shown.append(("turn opener", text))

    def update_toc(self) -> None:
        return None

    def restore_input_draft(self, text: str) -> bool:
        return self._input_bar.restore_draft(text)

    def flash_submit_still_sending(self) -> None:
        self.still_sending_flashes += 1

    def flash_interrupted(self) -> None:
        return None

    def clear_ask_user_inline_prompts(self) -> None:
        return None

    async def render_interrupted(self) -> None:
        self.interrupted_renders += 1

    def set_retry_mode(self, _enabled: bool, *, label: object = "Retry") -> None:
        return None


def _text(message: MessageRef | str) -> str:
    return message if isinstance(message, str) else format_message(message)


def _agent_message(text: str) -> InvocationMessage:
    return InvocationMessage(
        text=text, is_final=False, is_intermediate=True, origin=InvocationOrigin("turn", "", "turn-test", None)
    )


@dataclass
class _Harness:
    """A live turn with the real submit flow and backend-event handler on one bus."""

    bus: EventBus = field(default_factory=EventBus)
    state: MainScreenState = field(default_factory=lambda: MainScreenState(run=RunState(agent_running=True)))
    input_bar: _InputBar = field(default_factory=_InputBar)
    running: list[bool] = field(default_factory=list)
    flashes: list[str] = field(default_factory=list)
    chat_errors: list[str] = field(default_factory=list)
    notifications: list[tuple[str, str, str]] = field(default_factory=list)
    # Terminal-tab titles, user bubbles and agent messages, in the order shown.
    shown: list[tuple[str, str]] = field(default_factory=list)
    published: list[UserMessage] = field(default_factory=list)
    interrupts: list[UserInterrupt] = field(default_factory=list)
    workers: list[Callable[[], Awaitable[object]]] = field(default_factory=list)

    def __post_init__(self) -> None:
        harness = self

        class _StatusBar:
            def flash(
                self,
                message: MessageRef | str,
                *,
                warn: bool = False,
                error: bool = False,
                caution: bool = False,
                trail: str = "",
            ) -> None:
                harness.flashes.append(_text(message))

        class _ChatPanel:
            async def add_error(self, message: str, *, action_label: str | None = "Retry") -> None:
                harness.chat_errors.append(message)

            async def add_user_message(
                self,
                text: str,
                *,
                is_injection: bool = False,
                created_at: Any = None,
                contents: list[Any] | None = None,
            ) -> None:
                assert is_injection is True
                harness.shown.append(("bubble", text))

            async def add_agent_message(
                self,
                text: str,
                *,
                is_final: bool,
                is_intermediate: bool = False,
                structured_output_completed: bool = False,
                presentation: object = None,
                created_at: Any = None,
            ) -> None:
                harness.shown.append(("agent", text))

        def query_one(cls: type) -> object:
            if cls.__name__ == "InputBar":
                return self.input_bar
            if cls.__name__ == "StatusBar":
                return _StatusBar()
            if cls.__name__ == "ChatPanel":
                return _ChatPanel()
            raise AssertionError(f"unexpected query_one({cls.__name__})")

        def notify(
            message: MessageRef | str,
            *,
            title: MessageRef | str,
            severity: str,
            timeout: float | None,
            markup: bool,
        ) -> None:
            self.notifications.append((_text(title), severity, _text(message)))

        def set_agent_running(running: bool) -> None:
            self.running.append(running)
            # As MainScreen does: a run's end hands a locked composer back.
            if not running and self.input_bar.locked:
                self.input_bar.unlock_and_keep()

        screen = SimpleNamespace(
            _state=self.state,
            _set_agent_running=set_agent_running,
            _session_title=fake_session_title(
                set_terminal_title_for_user_message=lambda text: self.shown.append(("title", text))
            ),
            query_one=query_one,
            notify=notify,
            _debug=lambda *_args: None,
        )
        self.handler: BackendEventHandler = make_backend_handler(screen)
        self.view = _InputFlowView(self.input_bar, self.shown)
        self.controller = InputFlowController(
            state=self.state,
            services=MainScreenServices(bus=self.bus),
            view=self.view,  # type: ignore[arg-type]
            # Workers are recorded, never run: a withdrawal's backend cancel
            # stays queued, as when the hook answers before it gets to run.
            start_worker=self.workers.append,
            handle_agent_message=self.handler.on_agent_message,
            handle_error=self.handler.on_error,
            set_agent_running=self.handler.set_agent_running,
            set_has_messages=lambda _has: None,
            clear_workspace_marker=lambda: None,
            clear_pending_questions=lambda: None,
            commit_profile_switch_marker=lambda: None,
            post_gc_message=lambda _message: None,
            debug=lambda _key, _msg: None,
        )

    async def start(self, on_message: Callable[[UserMessage], Awaitable[None]] | None = None) -> None:
        """Answer each user message with *on_message*, inside its own publish."""

        async def backend(event: UserMessage) -> None:
            self.published.append(event)
            if on_message is not None:
                await on_message(event)

        async def record_interrupt(event: UserInterrupt) -> None:
            self.interrupts.append(event)

        await self.bus.subscribe(UserMessage, backend)
        await self.bus.subscribe(Error, self.handler.on_error)
        await self.bus.subscribe(UserInjectResult, self.handler.on_injection_outcome)
        await self.bus.subscribe(UserInterrupt, record_interrupt)

    async def send_mid_run(self, text: str) -> None:
        # Enter while the agent runs leaves the text in the composer.
        self.input_bar.value = text
        await self.controller.queue_injection(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["hook_blocked", "prompt_admission_conflict"])
@pytest.mark.parametrize("withdrawn", [False, True], ids=["queued", "withdrawn_during_hook"])
async def test_refused_mid_run_message_leaves_the_turn_running(code: str, withdrawn: bool) -> None:
    h = _Harness()

    async def refuse(_event: UserMessage) -> None:
        # As the backend's admission does: the Error is published from the
        # handler the message's own publish awaits.
        if withdrawn:
            # Esc while the hook runs withdraws the queued message.
            assert h.controller.cancel_pending_injection() is True
        await h.bus.publish(Error(code=code, message=_BLOCK_REASON))

    await h.start(refuse)
    await h.send_mid_run(_TEXT)

    # The turn the message was meant for is still running: only the message
    # was refused, so the run UI, its status line and its tool cards stay.
    assert h.running == []
    assert h.state.run.agent_running is True
    assert h.flashes == []
    assert h.chat_errors == []
    assert h.notifications == [("Message not sent", "error", _BLOCK_REASON)]
    assert h.input_bar.locked is False
    assert h.input_bar.value == _TEXT
    assert h.state.pending_injection.active is False
    assert h.state.submit.active is False
    assert h.shown == []

    # Esc still reaches the backend turn.
    await h.controller.publish_interrupt()

    assert len(h.interrupts) == 1
    assert h.view.interrupted_renders == 1
    assert h.running == [False]


@pytest.mark.asyncio
async def test_error_the_running_turn_publishes_meanwhile_is_the_turns() -> None:
    """A blocking code from another task is no refusal of the message being admitted."""
    h = _Harness()

    async def turn_ends_meanwhile(_event: UserMessage) -> None:
        # The turn's own task ends it — a queued retry dropped for a deleted
        # working folder — while the message is still being admitted.
        await asyncio.create_task(h.bus.publish(Error(code="working_dir_missing", message="Working directory is gone")))

    await h.start(turn_ends_meanwhile)
    await h.send_mid_run(_TEXT)

    assert h.running == [False]
    assert h.chat_errors == ["Working directory is gone"]
    assert h.notifications == []
    # The message still waits for the backend's word on it.
    assert h.state.pending_injection.active is True
    assert h.input_bar.value == _TEXT


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["consumed", "consumed_after_turn_end", "queued", "abandoned", "withdrawn"])
async def test_terminal_title_names_a_mid_run_message_once_the_turn_gets_it(outcome: str) -> None:
    h = _Harness()
    await h.start()
    await h.send_mid_run(_TEXT)
    injection_id = h.state.pending_injection.injection_id

    if outcome == "withdrawn":
        # Esc while the message waits for the next model call; the turn then
        # drains it unused.
        assert h.controller.cancel_pending_injection() is True
    if outcome == "consumed_after_turn_end":
        # The turn finished first and its result already owns the tab.
        h.state.run.agent_running = False
    if outcome in ("abandoned", "withdrawn"):
        await h.bus.publish(UserInjectResult(text=_TEXT, consumed=False, injection_id=injection_id))
    if outcome.startswith("consumed"):
        await h.bus.publish(UserInjectResult(text=_TEXT, consumed=True, injection_id=injection_id))

    expected = {
        # The tab is named before the bubble's mount awaits, which a turn
        # result could otherwise land during and be overwritten after.
        "consumed": [("title", _TEXT), ("bubble", _TEXT)],
        "consumed_after_turn_end": [("bubble", _TEXT)],
    }
    assert h.shown == expected.get(outcome, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("consumed", [False, True], ids=["abandoned_restores", "consumed_drops"])
async def test_injection_outcome_settles_its_staged_quotes(consumed: bool) -> None:
    """The quotes a queued injection carried come back unless the model saw it."""
    h = _Harness()
    await h.start()
    h.state.quotes.add("quoted while running", "assistant")
    await h.send_mid_run(_TEXT)

    # The quotes left the chip with the submit and ride the pending tracker.
    assert h.state.quotes.quotes == ()
    assert h.state.pending_injection.quotes == (Quote("quoted while running", "assistant"),)
    injection_id = h.state.pending_injection.injection_id

    await h.bus.publish(UserInjectResult(text=_TEXT, consumed=consumed, injection_id=injection_id))

    if consumed:
        assert h.state.quotes.quotes == ()
    else:
        # The injection never reached the model: its quotes return to the
        # chip next to the composer's own text, ready for the next submit.
        assert h.state.quotes.quotes == (Quote("quoted while running", "assistant"),)


@pytest.mark.asyncio
async def test_consumed_injection_title_names_the_typed_body_not_the_block() -> None:
    h = _Harness()
    await h.start()
    h.state.quotes.add("quoted while running", None)
    await h.send_mid_run(_TEXT)
    injection_id = h.state.pending_injection.injection_id
    wire_text = compose_outgoing_text(_TEXT, [Quote("quoted while running")])

    await h.bus.publish(UserInjectResult(text=wire_text, consumed=True, injection_id=injection_id))

    # The tab shows the typed body; the bubble renders the full wire text.
    assert h.shown == [("title", _TEXT), ("bubble", wire_text)]


@pytest.mark.asyncio
async def test_cancel_during_admission_keeps_restored_quotes_when_not_refused() -> None:
    """Esc while a message is being admitted restores its quotes for good.

    The backend abandons a cancelled submit without refusing it, so the
    queued flow's consume after the publish must not wipe the quotes the
    cancel already handed back to the chip.
    """
    h = _Harness()

    async def cancel_during_admission(_event: UserMessage) -> None:
        # Esc while the hook runs; the backend marks the submit cancelled and
        # abandons it later — no Error, no refusal on the publish.
        assert h.controller.cancel_pending_injection() is True

    await h.start(cancel_during_admission)
    h.state.quotes.add("quoted while running", "assistant")
    await h.send_mid_run(_TEXT)

    assert h.state.quotes.quotes == (Quote("quoted while running", "assistant"),)
    assert h.input_bar.locked is False
    assert h.input_bar.value == _TEXT
    assert h.state.pending_injection.active is False
    assert h.shown == []


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_running", [True, False], ids=["mid_run", "new_turn"])
async def test_second_submit_in_the_same_tick_goes_back_to_the_composer(agent_running: bool) -> None:
    """Two submits scheduled before either worker runs: only the first is sent.

    The submit in flight owns the admission state: a second one would be sent
    on top of it, and the first's cleanup would erase the second's refusal.
    """
    h = _Harness(state=MainScreenState(run=RunState(agent_running=agent_running)))
    hook_entered = asyncio.Event()
    release_hook = asyncio.Event()

    async def hold_then_refuse(_event: UserMessage) -> None:
        # Only the first message is held; any later one is refused at once.
        if not hook_entered.is_set():
            hook_entered.set()
            await release_hook.wait()
        await h.bus.publish(Error(code="hook_blocked", message=_BLOCK_REASON))

    await h.start(hold_then_refuse)
    # A running turn's Enter leaves the text in the composer, so a repeated
    # Enter sends it again; a new turn's Enter clears it.
    h.input_bar.value = _TEXT if agent_running else ""
    second = _TEXT if agent_running else "second message"
    h.controller.submit_user_text(_TEXT)
    h.controller.submit_user_text(second)
    assert len(h.workers) == 2

    first_task = asyncio.create_task(h.workers[0]())
    await wait_for(lambda: hook_entered.is_set() or first_task.done(), description="first submit in its hook")
    await h.workers[1]()

    assert not first_task.done()
    assert [event.text for event in h.published] == [_TEXT]
    assert h.input_bar.value == second
    # Only text that visibly came back says why.
    assert h.view.still_sending_flashes == (0 if agent_running else 1)

    release_hook.set()
    await first_task

    # Nothing stays reserved: the next submit is sent.
    assert h.state.submit.active is False
    h.controller.submit_user_text(second)
    await h.workers[2]()
    assert [event.text for event in h.published] == [_TEXT, second]


@pytest.mark.asyncio
@pytest.mark.parametrize("meanwhile", ["nothing", "esc", "edit"])
async def test_mid_run_message_whose_run_ended_first_shows_the_turn_it_opened(meanwhile: str) -> None:
    h = _Harness()
    first_events: list[asyncio.Task[None]] = []

    async def admit_as_new_turn(event: UserMessage) -> None:
        # The run finished while the message waited for admission (the UI saw
        # its end), so the backend opened a new turn with it instead.
        h.handler.set_agent_running(False)
        if meanwhile == "esc":
            # Its backend cancel arrives too late.
            assert h.controller.cancel_pending_injection() is True
        if meanwhile == "edit":
            # The run's end handed the composer back, so it is editable.
            h.input_bar.value = "edited draft"
        event.started_turn = True
        # The new run's task: its first event comes once the publish returned.
        first_events.append(asyncio.create_task(h.bus.publish(_agent_message("first step"))))

    await h.bus.subscribe(InvocationMessage, h.handler.on_agent_message)
    await h.start(admit_as_new_turn)
    await h.send_mid_run(_TEXT)
    await asyncio.gather(*first_events)

    assert h.running == [False, True]
    assert h.state.run.agent_running is True
    # The turn's first event waited for its opener.
    assert h.shown == [("title", _TEXT), ("turn opener", _TEXT), ("agent", "first step")]
    assert h.view.input_history == [_TEXT]
    assert h.view.run_status == ["Thinking"]
    assert h.state.pending_injection.active is False
    assert h.state.submit.active is False
    assert h.state.render_gate.active is False
    assert h.input_bar.locked is False
    # Only the sent text leaves the composer; a draft edited since stays.
    assert h.input_bar.value == {"nothing": "", "esc": _TEXT, "edit": "edited draft"}[meanwhile]


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["consumed", "abandoned", "dropped"])
async def test_new_turn_message_the_finishing_run_took_opens_no_turn(outcome: str) -> None:
    """The screen saw the turn end, but the backend's run was still finishing.

    The backend took the message for that run instead of opening a turn, and
    says so only by not marking it ``started_turn``; the run's outcome for it
    comes later, as for any queued message — or never, when it was dropped.
    """
    h = _Harness(state=MainScreenState(run=RunState(agent_running=False)))
    await h.start()
    # A new turn's Enter clears the composer.
    await h.controller.send_user_message(_TEXT)

    assert h.running == []
    assert h.shown == []
    assert h.view.run_status == []
    assert h.state.render_gate.active is False
    assert h.state.submit.active is False
    assert h.input_bar.value == _TEXT

    if outcome != "dropped":
        await h.bus.publish(UserInjectResult(text=_TEXT, consumed=outcome == "consumed", injection_id=None))

    assert h.shown == ([("bubble", _TEXT)] if outcome == "consumed" else [])
    assert h.input_bar.value == ("" if outcome == "consumed" else _TEXT)


@pytest.mark.asyncio
async def test_mid_run_message_failing_before_its_publish_releases_the_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A claimed submit that raises before publishing must not refuse every later submit."""
    h = _Harness()
    await h.start()

    def screen_gone() -> None:
        raise RuntimeError("input bar is gone")

    monkeypatch.setattr(h.view, "lock_input_with_text", screen_gone)
    with pytest.raises(RuntimeError, match="input bar is gone"):
        await h.controller.queue_injection(_TEXT)

    assert h.state.submit.active is False
    assert h.published == []


async def test_use_paths_instead_rewrites_a_mid_run_message_refused_for_its_image(tmp_path: Path) -> None:
    """The message stays in the input box while it is sent, so the box is not
    empty when the refusal hands it back; the button still rewrites it."""
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine())
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    sent: list[UserMessage] = []

    async def refuse_images(event: UserMessage) -> None:
        # What the backend does with an image in a message sent while a turn runs.
        sent.append(event)
        await app._bus.publish(
            Warning(
                code="image_attachment_while_running", message="Images cannot be added while the agent is responding."
            )
        )

    await app._bus.subscribe(UserMessage, refuse_images)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        main = app.screen
        assert isinstance(main, MainScreen)
        main._state.workspace.current_cwd = str(tmp_path)
        main._state.runtime.details_confirmed = True
        main._state.runtime.details.model.model_id = "vendor/model"
        main._set_agent_running(True)
        input_bar = main.query_one(InputBar)
        input_bar.value = f'look at @"{image}"'
        input_bar.focus_input()
        await wait_for(lambda: input_bar.query_one("#chat-input").has_focus, pilot=pilot)

        await pilot.press("enter")
        await wait_for(lambda: isinstance(app.screen, VisionUnsupportedDialog), pilot=pilot)
        assert [event.injection_id is not None for event in sent] == [True]
        await click_when_settled(pilot, "#vision-unsupported-use-paths")
        await wait_for(lambda: app.screen is main, pilot=pilot)

        assert input_bar.value == f"look at {image}"
        assert not input_bar.locked
