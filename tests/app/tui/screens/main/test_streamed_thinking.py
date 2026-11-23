# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A main turn's thinking shows as it streams, folded; each new segment starts as a one-line row."""

from __future__ import annotations

from typing import TYPE_CHECKING

from chrys.app.tui.widgets.chat.live_process_row import LiveProcessRow
from chrys.app.tui.widgets.chat.messages import AgentMessage, InterruptedMessage, ThinkingMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    InvocationContentAbandoned,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationMessage,
)
from chrys.foundation.models.invocations import InvocationOrigin
from tests.support.tui_app_harness import SessionGenerationEngine, make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.foundation.models.invocations import ContentChannel

_ORIGIN = InvocationOrigin("turn", "s1", "turn-1", None)


def _delta(segment_id: str, text: str, channel: ContentChannel = "reasoning") -> InvocationContentDelta:
    return InvocationContentDelta(origin=_ORIGIN, session_id="s1", segment_id=segment_id, channel=channel, text=text)


def _finished(segment_id: str, channel: ContentChannel = "reasoning") -> InvocationContentFinished:
    return InvocationContentFinished(
        origin=_ORIGIN, session_id="s1", segment_id=segment_id, channel=channel, status="completed"
    )


def _thinking(panel: ChatPanel) -> list[str]:
    return [message.text for message in panel.query(ThinkingMessage)]


async def test_thinking_streams_folded_above_the_answer_and_stays_folded_after_the_process_opens(
    tmp_path: Path,
) -> None:
    bus = EventBus()
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine(), event_bus=bus)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        main._set_agent_running(True)

        for event in (_delta("r1", "Looking "), _delta("r1", "closer.")):
            await bus.publish(event, raise_handler_errors=True)
        await wait_for(lambda: _thinking(panel) == ["Looking closer."], pilot=pilot, description="thinking mounted")
        (first,) = panel.query(ThinkingMessage)
        assert first.collapsed is True
        assert first.process_hidden

        for event in (
            _finished("r1"),
            _delta("a1", "Answer.", channel="answer"),
            _finished("a1", channel="answer"),
        ):
            await bus.publish(event, raise_handler_errors=True)
        await wait_for(lambda: len(panel.query(AgentMessage)) == 1, pilot=pilot, description="answer mounted")
        (answer,) = panel.query(AgentMessage)
        assert panel.children.index(first) < panel.children.index(answer)

        await click_when_settled(pilot, panel.query_one(LiveProcessRow))
        await bus.publish(_delta("r2", "Still folded."), raise_handler_errors=True)
        await wait_for(lambda: len(_thinking(panel)) == 2, pilot=pilot, description="second thinking mounted")
        second = panel.query(ThinkingMessage).last()
        assert (first.collapsed, second.collapsed) == (True, True)

        await bus.publish(_finished("r2"), raise_handler_errors=True)
        await bus.publish(
            InvocationMessage(origin=_ORIGIN, session_id="s1", text="Answer.", is_final=True),
            raise_handler_errors=True,
        )
        await wait_for(lambda: not main.agent_running_state, pilot=pilot, description="turn completed")
        assert _thinking(panel) == ["Looking closer.", "Still folded."]


async def test_a_retry_that_gives_up_thinking_while_the_prompt_mounts_removes_it_in_order(tmp_path: Path) -> None:
    """Events that wait for the user bubble replay in arrival order, so the removal follows the text it removes."""
    bus = EventBus()
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine(), event_bus=bus)
    async with app.run_test(size=(120, 36)):
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        main._set_agent_running(True)
        main._state.render_gate.begin()

        for event in (
            _delta("r1", "Given up."),
            InvocationContentAbandoned(origin=_ORIGIN, session_id="s1", segment_ids=("r1",)),
            _delta("r2", "Thinking again."),
            _finished("r2"),
        ):
            await bus.publish(event, raise_handler_errors=True)
        assert _thinking(panel) == []

        await main._input_flow.flush_deferred_agent_messages()

        assert _thinking(panel) == ["Thinking again."]


async def test_slash_fold_preserves_an_expanded_streaming_thought(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine(), event_bus=EventBus())
    async with app.run_test(size=(80, 20)) as pilot:
        panel = app.screen.query_one(ChatPanel)
        panel.agent_running = True
        await panel.add_user_message("prompt")
        await panel.add_thinking_delta("r1", "reasoning", "Open streaming thought.")
        await click_when_settled(pilot, panel.query_one(LiveProcessRow))
        thought = panel.query_one(ThinkingMessage)
        await click_when_settled(pilot, thought.query_one(".fold-row-header"))
        await wait_for(
            lambda: "Open streaming thought." in "\n".join(s.text for s in app.screen._compositor.render_strips()),
            pilot=pilot,
            description="opened thought body painted",
        )
        bar = app.screen.query_one(InputBar)
        for suffix in ("\n\nMore.", "\n\nStill going."):
            bar.focus_input()
            await pilot.press("slash", "f", "o", "l", "d", "enter")
            await panel.add_thinking_delta("r1", "reasoning", suffix)
            await wait_for(
                lambda suffix=suffix: (
                    suffix.strip() in "\n".join(s.text for s in app.screen._compositor.render_strips())
                ),
                pilot=pilot,
                description="streamed text remains painted after /fold",
            )
        panel.finish_thinking_segment("r1")
        bar.focus_input()
        await pilot.press("slash", "f", "o", "l", "d", "enter")
        await pilot.pause()
        assert "Still going." not in "\n".join(s.text for s in app.screen._compositor.render_strips())


async def test_escape_restores_the_order_of_answer_then_thinking(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine(), event_bus=EventBus())
    async with app.run_test(size=(100, 28)) as pilot:
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        await panel.add_user_message("prompt")
        main._set_agent_running(True)
        await panel.add_answer_delta("a1", "First.")
        panel.finish_answer_segment("a1", completed=True)
        await panel.add_thinking_delta("r1", "reasoning", "Later thought.")
        await click_when_settled(pilot, panel.query_one(LiveProcessRow))
        await pilot.press("escape")
        await click_when_settled(pilot, "#confirm-yes")
        await wait_for(
            lambda: bool(panel.query(InterruptedMessage)) and not panel.query(LiveProcessRow),
            pilot=pilot,
            description="interrupted turn without its live title",
        )
        answer = panel.query_one(AgentMessage)
        thought = panel.query_one(ThinkingMessage)
        assert answer.display and thought.display and answer.region.y < thought.region.y
