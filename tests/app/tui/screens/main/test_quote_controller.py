# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the pending-quote buffer and its composition into sent messages."""

from __future__ import annotations

import pytest

from chrys.app.tui.screens.main.input_flow import InputFlowController
from chrys.app.tui.screens.main.quotes import QuoteAddOutcome, QuoteController
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.app.tui.widgets.chat.quote_format import (
    MAX_PENDING_QUOTES,
    SINGLE_QUOTE_MAX_CHARS,
    SOURCE_USER,
    Quote,
    compose_outgoing_text,
    split_selections_block,
)
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import InvocationMessage, UserMessage, UserRetry


class _RecordingView:
    """The InputFlowView surface the quote paths touch."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def set_terminal_title_for_user_message(self, text: str) -> None:
        self.calls.append(f"title:{text}")

    def current_chat_session_id(self) -> str:
        return "session"

    def add_input_history(self, text: str, *, session_id: str | None) -> None:
        self.calls.append(f"history:{text}")

    def set_retry_mode(self, enabled: bool, *, label: str = "Retry") -> None:
        self.calls.append(f"retry_mode:{enabled}")

    def start_run_status(self, label: object) -> None:
        self.calls.append("status")

    def lock_input_with_text(self) -> None:
        self.calls.append("lock_input")

    def unlock_input_keep_if_locked(self) -> None:
        self.calls.append("unlock_input")

    def restore_input_draft(self, text: str) -> bool:
        self.calls.append(f"restore_draft:{text}")
        return True

    def flash_submit_still_sending(self) -> None:
        self.calls.append("flash_still_sending")

    def unlock_input_clearing(self, text: str) -> None:
        self.calls.append(f"unlock_clearing:{text}")

    def update_toc(self) -> None:
        self.calls.append("toc")

    def set_retry_pending(self, pending: bool) -> None:
        self.calls.append(f"retry_pending:{pending}")

    def hide_trailing_status_action(self) -> None:
        self.calls.append("hide_status_action")

    def flash_interrupted(self) -> None:
        self.calls.append("flash_interrupted")

    def clear_ask_user_inline_prompts(self) -> None:
        self.calls.append("clear_inline_prompts")

    def clear_terminal_title_result(self) -> None:
        self.calls.append("clear_title_result")

    async def render_user_retry_note(self, text: str, *, created_at: object) -> None:
        self.calls.append(f"render_note:{text}")

    async def render_interrupted(self) -> None:
        self.calls.append("render_interrupted")

    async def render_user_message(self, text: str, *, created_at: object, contents: object) -> None:
        self.calls.append(f"render:{text}")


def _make_flow(
    bus: EventBus,
    quotes: QuoteController,
    view: _RecordingView,
    workers: list[object] | None = None,
) -> tuple[InputFlowController, MainScreenState]:
    state = MainScreenState(quotes=quotes)

    async def _noop_agent_message(_event: InvocationMessage) -> None:
        return None

    controller = InputFlowController(
        state=state,
        services=MainScreenServices(bus=bus),
        view=view,  # type: ignore[arg-type]
        start_worker=workers.append if workers is not None else (lambda work: None),
        handle_agent_message=_noop_agent_message,
        handle_error=_noop_agent_message,  # type: ignore[arg-type]
        set_agent_running=lambda _running: None,
        set_has_messages=lambda _has: None,
        clear_workspace_marker=lambda: None,
        clear_pending_questions=lambda: None,
        commit_profile_switch_marker=lambda: None,
        post_gc_message=lambda _message: None,
        debug=lambda *_args: None,
    )
    return controller, state


def test_add_accepts_and_orders_quotes() -> None:
    quotes = QuoteController()

    assert quotes.add("first", SOURCE_USER) is QuoteAddOutcome.ADDED
    assert quotes.add("second", None) is QuoteAddOutcome.ADDED
    assert quotes.quotes == (Quote("first", SOURCE_USER), Quote("second", None))


def test_add_rejects_empty_and_oversized_text() -> None:
    quotes = QuoteController()

    assert quotes.add("   \n", None) is QuoteAddOutcome.REJECTED_SINGLE_LIMIT
    assert quotes.add("x" * (SINGLE_QUOTE_MAX_CHARS + 1), None) is QuoteAddOutcome.REJECTED_SINGLE_LIMIT
    assert not quotes.quotes


def test_add_enforces_count_and_total_budgets() -> None:
    quotes = QuoteController()

    for index in range(MAX_PENDING_QUOTES):
        assert quotes.add(f"q{index}", None) is QuoteAddOutcome.ADDED
    assert quotes.add("one too many", None) is QuoteAddOutcome.REJECTED_COUNT_LIMIT

    total = QuoteController()
    filler = "x" * SINGLE_QUOTE_MAX_CHARS
    assert total.add(filler, None) is QuoteAddOutcome.ADDED
    assert total.add(filler, None) is QuoteAddOutcome.ADDED
    assert total.add("y", None) is QuoteAddOutcome.REJECTED_TOTAL_LIMIT


def test_remove_clear_and_consume_notify_once_per_real_change() -> None:
    changes: list[int] = []
    quotes = QuoteController(on_changed=lambda: changes.append(1))

    quotes.clear()
    assert changes == []
    quotes.add("q", None)
    quotes.remove_at(0)
    quotes.remove_at(0)
    assert not quotes.quotes
    assert len(changes) == 2
    quotes.add("q", None)
    quotes.consume()
    assert not quotes.quotes
    assert len(changes) == 4


@pytest.mark.asyncio
async def test_send_user_message_composes_quotes_into_wire_text() -> None:
    bus = EventBus()
    published: list[UserMessage] = []

    async def _on_message(event: UserMessage) -> None:
        published.append(event)
        # The engine's admission handler marks accepted turns; mirror it so
        # the flow reaches the open-turn rendering path.
        event.started_turn = True

    await bus.subscribe(UserMessage, _on_message)
    quotes = QuoteController()
    assert quotes.add("quoted answer", "assistant") is QuoteAddOutcome.ADDED
    view = _RecordingView()
    controller, _state = _make_flow(bus, quotes, view)

    await controller.send_user_message("what does this mean")

    assert len(published) == 1
    visible, parsed = split_selections_block(published[0].text)
    assert visible == "what does this mean"
    assert parsed == [Quote("quoted answer", "assistant")]
    # The bubble renders the wire text; history and title keep the typed text.
    assert "history:what does this mean" in view.calls
    assert "title:what does this mean" in view.calls
    assert any(call.startswith("render:what does this mean\n\n# Conversation selections:") for call in view.calls)
    assert not quotes.quotes


@pytest.mark.asyncio
async def test_send_user_message_without_a_turn_restores_wire_text_and_consumes() -> None:
    bus = EventBus()
    quotes = QuoteController()
    quotes.add("quoted", None)
    view = _RecordingView()
    controller, _state = _make_flow(bus, quotes, view)

    await controller.send_user_message("typed")

    # No handler started a turn: the full wire text returns to the composer so
    # the quoted content stays editable, and the pending quotes are consumed
    # into it (a resend must not compose them a second time).
    assert view.calls == [f"restore_draft:{compose_outgoing_text('typed', [Quote('quoted')])}"]
    assert not quotes.quotes


@pytest.mark.asyncio
async def test_send_user_message_blocked_keeps_quotes_pending() -> None:
    bus = EventBus()
    quotes = QuoteController()
    quotes.add("quoted", None)
    view = _RecordingView()
    controller, state = _make_flow(bus, quotes, view)

    async def _blocking_handler(_event: UserMessage) -> None:
        state.submit.blocked = True

    await bus.subscribe(UserMessage, _blocking_handler)

    await controller.send_user_message("typed")

    assert quotes.quotes == (
        Quote(
            "quoted",
        ),
    )
    assert not view.calls


@pytest.mark.asyncio
async def test_queue_injection_composes_quotes_into_injected_text() -> None:
    bus = EventBus()
    events: list[UserMessage] = []

    async def _on_message(event: UserMessage) -> None:
        events.append(event)

    await bus.subscribe(UserMessage, _on_message)
    quotes = QuoteController()
    quotes.add("mid-run quote", None)
    view = _RecordingView()
    controller, state = _make_flow(bus, quotes, view)

    await controller.queue_injection("while running")

    assert len(events) == 1
    visible, parsed = split_selections_block(events[0].text)
    assert visible == "while running"
    assert parsed == [Quote("mid-run quote")]
    # The pending-injection tracker holds the wire text: an undo returns it.
    assert state.pending_injection.text == events[0].text
    assert not quotes.quotes


def test_consume_snapshot_keeps_quotes_added_after_the_composition() -> None:
    quotes = QuoteController()
    quotes.add("staged", None)
    wire_text, staged = quotes.compose("typed")

    quotes.add("added while publishing", None)
    quotes.consume(staged)

    # Only the composed snapshot leaves; the late addition stays pending.
    assert quotes.quotes == (Quote("added while publishing"),)
    assert split_selections_block(wire_text)[1] == [Quote("staged")]


def test_restore_returns_staged_quotes_ahead_of_later_adds() -> None:
    quotes = QuoteController()
    quotes.add("staged", None)
    _wire_text, staged = quotes.compose("typed")
    quotes.consume(staged)
    quotes.add("late", None)

    quotes.restore(staged)

    assert quotes.quotes == (Quote("staged"), Quote("late"))
    # Restoring what never left is a no-op, never a duplicate.
    quotes.restore(staged)
    assert quotes.quotes == (Quote("staged"), Quote("late"))


@pytest.mark.asyncio
async def test_send_user_message_keeps_quotes_added_during_the_publish() -> None:
    bus = EventBus()
    quotes = QuoteController()
    quotes.add("original", None)

    async def _adds_another_quote(_event: UserMessage) -> None:
        quotes.add("selected while publishing", None)

    await bus.subscribe(UserMessage, _adds_another_quote)
    view = _RecordingView()
    controller, _state = _make_flow(bus, quotes, view)

    await controller.send_user_message("typed")

    # The publish awaited a handler (hooks, image preparation in production);
    # the quote added meanwhile must survive the send's consume.
    assert quotes.quotes == (Quote("selected while publishing"),)


@pytest.mark.asyncio
async def test_cancel_pending_injection_restores_staged_quotes() -> None:
    bus = EventBus()
    quotes = QuoteController()
    quotes.add("staged quote", SOURCE_USER)
    view = _RecordingView()
    controller, state = _make_flow(bus, quotes, view)

    await controller.queue_injection("mid-run")
    assert state.pending_injection.quotes == (Quote("staged quote", SOURCE_USER),)
    assert not quotes.quotes
    # The user can quote more while the injection waits for its outcome.
    quotes.add("late quote", None)

    assert controller.cancel_pending_injection() is True

    assert not state.pending_injection.active
    assert quotes.quotes == (Quote("staged quote", SOURCE_USER), Quote("late quote"))
    assert "unlock_input" in view.calls


@pytest.mark.asyncio
async def test_publish_interrupt_restores_pending_injection_quotes() -> None:
    bus = EventBus()
    quotes = QuoteController()
    quotes.add("interrupted quote", None)
    view = _RecordingView()
    controller, state = _make_flow(bus, quotes, view)

    await controller.queue_injection("mid-run")
    assert not quotes.quotes
    state.run.agent_running = True

    await controller.publish_interrupt()

    assert quotes.quotes == (Quote("interrupted quote"),)


@pytest.mark.asyncio
async def test_request_retry_composes_quotes_into_the_retry_note() -> None:
    bus = EventBus()
    retries: list[UserRetry] = []

    async def _on_retry(event: UserRetry) -> None:
        retries.append(event)

    await bus.subscribe(UserRetry, _on_retry)
    quotes = QuoteController()
    quotes.add("quoted failure context", "assistant")
    view = _RecordingView()
    workers: list[object] = []
    controller, _state = _make_flow(bus, quotes, view, workers)

    controller.request_retry("continue with this")

    assert len(workers) == 1
    await workers[0]()

    assert len(retries) == 1
    visible, parsed = split_selections_block(retries[0].text)
    assert visible == "continue with this"
    assert parsed == [Quote("quoted failure context", "assistant")]
    # The note renders the wire text; the title and history keep the typed text.
    assert any(call.startswith("render_note:continue with this\n\n# Conversation selections:") for call in view.calls)
    assert "title:continue with this" in view.calls
    assert "history:continue with this" in view.calls
    assert not quotes.quotes


@pytest.mark.asyncio
async def test_request_retry_blocked_keeps_quotes_pending() -> None:
    bus = EventBus()
    quotes = QuoteController()
    quotes.add("quoted", None)
    view = _RecordingView()
    workers: list[object] = []
    controller, state = _make_flow(bus, quotes, view, workers)

    async def _blocking_handler(_event: UserRetry) -> None:
        state.submit.blocked = True

    await bus.subscribe(UserRetry, _blocking_handler)

    controller.request_retry("try again")
    await workers[0]()

    assert quotes.quotes == (Quote("quoted"),)
    assert not any(call.startswith(("render_note:", "title:", "history:")) for call in view.calls)
