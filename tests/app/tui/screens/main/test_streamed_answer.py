# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A main turn's answer shows as its deltas arrive, and the final completion snapshot never repeats it."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from textual.widgets import Button

from chrys.app.tui.widgets.chat.messages import AgentMessage, InterruptedMessage, RetryMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.chrome import status_bar as status_bar_module
from chrys.app.tui.widgets.chrome.status_bar import StatusBar
from chrys.app.tui.widgets.markdown.widget import VirtualizedMarkdown
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    InvocationAnswerWithdrawn,
    InvocationContentAbandoned,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationMessage,
    InvocationRetryAttempt,
    InvocationSteered,
    InvocationToolCallStart,
    UserMessage,
)
from chrys.foundation.models.invocations import InvocationOrigin
from tests.support.tui_app_harness import SessionGenerationEngine, make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from pathlib import Path

_ORIGIN = InvocationOrigin("turn", "s1", "turn-1", None)


def _shown(panel: ChatPanel) -> list[str]:
    """The answer text each mounted agent message's markdown body currently renders."""
    return [message.query_one(VirtualizedMarkdown).source for message in panel.query(AgentMessage)]


async def test_streamed_answer_text_shows_before_the_final_message_and_once_after_it(tmp_path: Path) -> None:
    bus = EventBus()
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine(), event_bus=bus)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        main._set_agent_running(True)

        for text in ("Streaming ", "answer"):
            await bus.publish(
                InvocationContentDelta(
                    origin=_ORIGIN, session_id="s1", segment_id="seg-1", channel="answer", text=text
                ),
                raise_handler_errors=True,
            )
        await wait_for(
            lambda: _shown(panel) == ["Streaming answer"],
            pilot=pilot,
            description="streamed answer rendered before the final message",
        )
        (streaming,) = panel.query(AgentMessage)
        assert streaming.has_class("--streaming"), "muted until it is known to be the answer"
        assert main.agent_running_state is True

        await bus.publish(
            InvocationContentFinished(
                origin=_ORIGIN, session_id="s1", segment_id="seg-1", channel="answer", status="completed"
            ),
            raise_handler_errors=True,
        )
        await bus.publish(
            InvocationMessage(origin=_ORIGIN, session_id="s1", text="Streaming answer", is_final=True),
            raise_handler_errors=True,
        )
        await wait_for(
            lambda: not main.agent_running_state and not streaming.has_class("--streaming"),
            pilot=pilot,
            description="turn completed and the streamed message finalized",
        )
        assert list(panel.query(AgentMessage)) == [streaming]
        assert _shown(panel) == ["Streaming answer"]
        assert not streaming.has_class("--streaming") and not streaming.has_class("--intermediate")


def _delta(segment_id: str, text: str) -> InvocationContentDelta:
    return InvocationContentDelta(origin=_ORIGIN, session_id="s1", segment_id=segment_id, channel="answer", text=text)


async def test_a_bare_continue_removes_the_half_answer_it_asks_for_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare Continue drops the interrupted pass's answer from history and asks again; the transcript drops what it names."""
    bus = EventBus()
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine(), event_bus=bus)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        # The harness configures no model; what matters here is what an admitted Continue does.
        monkeypatch.setattr(main, "_model_unconfigured", lambda: False)
        panel = main.query_one(ChatPanel)
        main._set_agent_running(True)
        await bus.publish(_delta("seg-1", "Half an ans"), raise_handler_errors=True)
        await bus.publish(
            InvocationContentFinished(
                origin=_ORIGIN, session_id="s1", segment_id="seg-1", channel="answer", status="interrupted"
            ),
            raise_handler_errors=True,
        )
        await main._input_flow.publish_interrupt()
        status = panel.query_one(InterruptedMessage)
        await wait_for(lambda: _shown(panel) == ["Half an ans"], pilot=pilot, description="half answer shown")

        await click_when_settled(pilot, status.query_one(Button))
        await wait_for(lambda: main.agent_running_state, pilot=pilot, description="continue admitted")
        assert _shown(panel) == ["Half an ans"]

        await bus.publish(
            InvocationAnswerWithdrawn(origin=_ORIGIN, session_id="s1", segment_ids=("seg-1",)),
            raise_handler_errors=True,
        )
        await wait_for(lambda: not panel.query(AgentMessage), pilot=pilot, description="the half answer removed")
        assert panel.query_one(InterruptedMessage) is status

        await bus.publish(_delta("seg-2", "The whole answer"), raise_handler_errors=True)
        await wait_for(lambda: _shown(panel) == ["The whole answer"], pilot=pilot, description="answer asked again")


def _tool_start(call_id: str) -> InvocationToolCallStart:
    return InvocationToolCallStart(origin=_ORIGIN, session_id="s1", tool_name="bash", call_id=call_id, args={})


async def test_a_steer_ends_the_passes_running_tool_cards_and_the_next_pass_opens_its_own(tmp_path: Path) -> None:
    """The steer cancelled the running call and no result follows it: its card ends, and the turn goes on."""
    bus = EventBus()
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine(), event_bus=bus)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        main._set_agent_running(True)
        await bus.publish(_delta("seg-1", "Let me run it."), raise_handler_errors=True)
        await bus.publish(_tool_start("c1"), raise_handler_errors=True)
        await wait_for(lambda: panel.is_tool_running("c1"), pilot=pilot, description="the call runs")

        await bus.publish(InvocationSteered(origin=_ORIGIN, session_id="s1"), raise_handler_errors=True)

        await wait_for(lambda: not panel.is_tool_running("c1"), pilot=pilot, description="the cancelled call ended")
        assert main.agent_running_state is True
        await bus.publish(_tool_start("c2"), raise_handler_errors=True)
        await wait_for(lambda: panel.is_tool_running("c2"), pilot=pilot, description="the next pass's call runs")
        assert len(panel.query(ToolGroup)) == 2, "the next pass's call opens its own group"


async def test_a_steer_while_the_user_bubble_mounts_withdraws_the_answer_the_gate_held(tmp_path: Path) -> None:
    """The steered pass's text waits behind the opener's bubble, and the withdrawal of its answer waits behind it."""
    bus = EventBus()
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine(), event_bus=bus)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        main._set_agent_running(True)
        # The opener's bubble is mounting: the turn's first text waits for it.
        main._state.render_gate.begin()
        for event in (
            _delta("seg-1", "Half an ans"),
            InvocationContentFinished(
                origin=_ORIGIN, session_id="s1", segment_id="seg-1", channel="answer", status="failed"
            ),
            InvocationSteered(origin=_ORIGIN, session_id="s1"),
            InvocationAnswerWithdrawn(origin=_ORIGIN, session_id="s1", segment_ids=("seg-1",)),
            _delta("seg-2", "The steered answer"),
        ):
            await bus.publish(event, raise_handler_errors=True)
        assert not panel.query(AgentMessage)

        await main._input_flow.flush_deferred_agent_messages()

        await wait_for(
            lambda: _shown(panel) == ["The steered answer"], pilot=pilot, description="only the next pass's answer"
        )
        assert main.agent_running_state is True


async def test_a_withdrawal_held_by_a_submit_that_opens_no_turn_still_removes_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop after a steer, then Enter: the steered pass streams on and drops its answer while the gate is up.

    The message joins that run, so no turn opens and the held text goes; the
    withdrawal is history's, and the answer showed before the gate, so it
    still applies.
    """
    bus = EventBus()
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine(), event_bus=bus)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        monkeypatch.setattr(main, "_model_unconfigured", lambda: False)
        panel = main.query_one(ChatPanel)
        main._set_agent_running(True)
        await bus.publish(_delta("seg-1", "Half an ans"), raise_handler_errors=True)
        await wait_for(lambda: _shown(panel) == ["Half an ans"], pilot=pilot, description="half answer shown")
        main._set_agent_running(False)  # Stop handed the composer back

        async def _settling_run_takes_it(event: UserMessage) -> None:
            assert main._state.render_gate.active
            await bus.publish(_delta("seg-1", "wer"))
            await bus.publish(InvocationAnswerWithdrawn(origin=_ORIGIN, session_id="s1", segment_ids=("seg-1",)))
            event.started_turn = False

        await bus.subscribe(UserMessage, _settling_run_takes_it)
        await main._input_flow.send_user_message("also check the logs")

        await wait_for(lambda: not panel.query(AgentMessage), pilot=pilot, description="the dropped answer removed")
        assert not main._state.render_gate.active


async def test_a_withdrawal_arriving_while_its_text_replays_waits_for_that_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replay of a delta awaits before it mounts the message: a withdrawal arriving then goes after it."""
    bus = EventBus()
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine(), event_bus=bus)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        main._set_agent_running(True)
        flow = main._input_flow
        replay_content = flow._handle_agent_content
        replaying = asyncio.Event()
        release = asyncio.Event()

        async def _held_replay(
            event: InvocationContentDelta | InvocationContentFinished | InvocationContentAbandoned,
        ) -> None:
            replaying.set()
            await release.wait()
            await replay_content(event)

        monkeypatch.setattr(flow, "_handle_agent_content", _held_replay)
        main._state.render_gate.begin()
        await bus.publish(_delta("seg-1", "Half an ans"), raise_handler_errors=True)
        drain = asyncio.create_task(flow.flush_deferred_agent_messages())
        await wait_for(lambda: replaying.is_set() or drain.done(), pilot=pilot, description="the delta replays")
        assert not drain.done()

        await bus.publish(
            InvocationAnswerWithdrawn(origin=_ORIGIN, session_id="s1", segment_ids=("seg-1",)),
            raise_handler_errors=True,
        )
        release.set()
        await drain

        assert not panel.query(AgentMessage)


async def test_a_withdrawal_of_an_answer_the_gate_does_not_hold_applies_while_its_publish_waits(
    tmp_path: Path,
) -> None:
    """A Continue drops an answer shown long before: it goes at once, and the run that dropped it waits for that.

    Held behind the note's bubble instead, the retried pass would run on
    while a restore's replay still mounts, its tool cards above its text.
    """
    bus = EventBus()
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine(), event_bus=bus)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        main._set_agent_running(True)
        await bus.publish(_delta("seg-1", "Half an ans"), raise_handler_errors=True)
        await wait_for(lambda: _shown(panel) == ["Half an ans"], pilot=pilot, description="half answer shown")
        main._state.render_gate.begin()  # the note's bubble mounts

        await bus.publish(
            InvocationAnswerWithdrawn(origin=_ORIGIN, session_id="s1", segment_ids=("seg-1",)),
            raise_handler_errors=True,
        )

        assert main._state.render_gate.active
        assert not panel.query(AgentMessage)


async def test_a_steer_while_the_user_bubble_mounts_keeps_the_passes_text_before_its_call_intermediate(
    tmp_path: Path,
) -> None:
    """Text the steered pass wrote before its call stays, replayed from the gate as intermediate text, as without it.

    The steer itself is never held: tool cards are not, so a held steer would
    end the pass after its replayed text and take that text for the answer.
    """
    bus = EventBus()
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine(), event_bus=bus)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        main._set_agent_running(True)
        main._state.render_gate.begin()
        for event in (
            _delta("seg-1", "Let me run it."),
            InvocationContentFinished(
                origin=_ORIGIN, session_id="s1", segment_id="seg-1", channel="answer", status="completed"
            ),
            _tool_start("c1"),
            InvocationSteered(origin=_ORIGIN, session_id="s1"),
            _delta("seg-2", "The steered answer"),
        ):
            await bus.publish(event, raise_handler_errors=True)

        await main._input_flow.flush_deferred_agent_messages()

        await wait_for(
            lambda: _shown(panel) == ["Let me run it.", "The steered answer"],
            pilot=pilot,
            description="the kept text and the next pass's answer",
        )
        kept, _answer = panel.query(AgentMessage)
        assert kept.has_class("--intermediate")
        assert not panel.is_tool_running("c1")


async def test_a_steer_during_a_retry_wait_removes_the_wait_it_cut_short(tmp_path: Path) -> None:
    bus = EventBus()
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine(), event_bus=bus)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        main._set_agent_running(True)
        await bus.publish(
            InvocationRetryAttempt(
                origin=_ORIGIN, session_id="s1", message="Rate limited", attempt=1, max_attempts=10, delay_seconds=60
            ),
            raise_handler_errors=True,
        )
        await wait_for(lambda: panel.query(RetryMessage), pilot=pilot, description="the retry wait shows")

        await bus.publish(InvocationSteered(origin=_ORIGIN, session_id="s1"), raise_handler_errors=True)

        await wait_for(lambda: not panel.query(RetryMessage), pilot=pilot, description="the cut-short wait removed")
        assert main.agent_running_state is True


async def test_the_status_bar_rates_the_turns_own_text_but_not_a_sub_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [100.0]
    monkeypatch.setattr(status_bar_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    bus = EventBus()
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine(), event_bus=bus)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        status_bar = main.query_one(StatusBar)
        main._set_agent_running(True)
        await wait_for(lambda: status_bar._output_speed() is None, pilot=pilot, description="no rate before output")

        sub_agent = InvocationOrigin("sub_agent", "s1", "child-1", _ORIGIN)
        for _ in range(2):
            await bus.publish(
                InvocationContentDelta(
                    origin=sub_agent, session_id="s1", segment_id="c-1", channel="answer", text="word " * 50
                ),
                raise_handler_errors=True,
            )
            now[0] += 1.0
        assert status_bar._output_speed() is None

        await bus.publish(_delta("seg-1", "word " * 50), raise_handler_errors=True)
        now[0] += 1.0
        await bus.publish(_delta("seg-1", "word " * 50), raise_handler_errors=True)
        answer_speed = status_bar._output_speed()
        assert answer_speed is not None

        await bus.publish(
            InvocationContentDelta(
                origin=_ORIGIN, session_id="s1", segment_id="seg-2", channel="reasoning", text="word " * 50
            ),
            raise_handler_errors=True,
        )
        rate_with_thinking = status_bar._output_speed()
        assert rate_with_thinking is not None
        assert rate_with_thinking > answer_speed
