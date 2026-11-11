# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Retry Now: the user ends the wait before a main-turn retry, and only the wait they clicked for."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

import chrys.orchestration.engine.run.bindings as bindings_module
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import InvocationMessage, InvocationRetryAttempt, RetryWaitSkip
from chrys.kernel import Agent, AgentSession, LoopRecorder
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.invoker.resources import Conversation
from chrys.service.agent_middleware import ApprovalMiddleware, AskUserMiddleware
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.approval.policy import ApprovalPolicy
from chrys.service.llm.mock import MockChatClient
from chrys.service.profiles.agents.schema import ApprovalConfig
from chrys.service.profiles.models.schema import API_STYLE_RESPONSES
from tests.support.event_capture import capture_events
from tests.support.mock_provider_turns import ProviderTurnProgress, mock_provider_profile, run_mock_provider_turn
from tests.support.waiting import (
    ENGINE_TEST_WAIT_TIMEOUT,
    ENGINE_TURN_TIMEOUT,
    wait_for,
    wait_until,
    with_wait_deadline,
)
from tests.support.wire_cases._kit import cc_text, json_reply, resp_message, resp_response

# Longer than any test runs: a wait these tests see end was ended by a skip or an interrupt.
_FOREVER = 3600


@pytest.fixture(autouse=True)
def _no_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a wake ends a wait early, never the interrupt latch read on a timer."""
    monkeypatch.setattr(bindings_module, "_RETRY_WAIT_POLL_SECONDS", float(_FOREVER))


@dataclass(frozen=True, slots=True)
class _Rig:
    bus: EventBus
    bindings: TurnBindings
    notices: list[InvocationRetryAttempt]
    sleeps: list[asyncio.Task[bool]]

    def start_sleep(self) -> asyncio.Task[bool]:
        """Start a retry wait as a pass would, owned by the fixture so a failing test leaves none behind."""
        task = asyncio.create_task(self.bindings._interruptible_sleep(_FOREVER))
        self.sleeps.append(task)
        return task


@pytest.fixture
async def rig() -> AsyncIterator[_Rig]:
    """Real main-turn bindings whose retry notices publish as a pass's would."""
    bus = EventBus()
    notices = await capture_events(bus, InvocationRetryAttempt)
    bindings = TurnBindings(
        conversation=Conversation(),
        agent=Agent(client=MockChatClient()),
        session=AgentSession(),
        event_bus=bus,
        approval_middleware=ApprovalMiddleware(ApprovalPolicy(ApprovalConfig()), bus),
        ask_user_middleware=AskUserMiddleware(bus),
        injection_middleware=InjectionMiddleware(),
        loop_recorder=LoopRecorder(),
    )
    bindings.resource_scope.own(bindings.approval.close)
    origin = bindings.inputs.origin
    bindings._bound_emitter = bindings._invocation_publishers.bind(origin)
    sleeps: list[asyncio.Task[bool]] = []
    try:
        yield _Rig(bus, bindings, notices, sleeps)
    finally:
        for task in sleeps:
            task.cancel()
        await asyncio.gather(*sleeps, return_exceptions=True)
        bindings._invocation_publishers.unbind(origin)
        await bindings.resource_scope.aclose()


async def _notice(bindings: TurnBindings, delay_seconds: int = _FOREVER) -> None:
    await bindings._publish_retry_notice("Connection error.", 1, 10, delay_seconds, ConnectionError())


async def test_a_skip_ends_the_wait_its_notice_announced(rig: _Rig) -> None:
    bindings, notices = rig.bindings, rig.notices
    await _notice(bindings)
    wait_id = notices[-1].wait_id

    sleep = rig.start_sleep()
    assert not await wait_until(sleep.done, timeout=0.2)
    assert bindings.skip_retry_wait(wait_id) is True

    assert await asyncio.wait_for(sleep, ENGINE_TURN_TIMEOUT) is False
    # That wait is over: clicking again starts nothing.
    assert bindings.skip_retry_wait(wait_id) is False


async def test_a_skip_sent_while_the_notice_is_shown_ends_the_wait_after_it(rig: _Rig) -> None:
    bindings = rig.bindings

    async def click_on_show(event: InvocationRetryAttempt) -> None:
        assert bindings.skip_retry_wait(event.wait_id) is True

    await rig.bus.subscribe(InvocationRetryAttempt, click_on_show)
    await _notice(bindings)

    assert await asyncio.wait_for(bindings._interruptible_sleep(_FOREVER), ENGINE_TURN_TIMEOUT) is False


async def test_a_skip_naming_an_earlier_wait_leaves_the_current_one_alone(rig: _Rig) -> None:
    bindings, notices = rig.bindings, rig.notices
    await _notice(bindings)
    first = notices[-1].wait_id
    assert bindings.skip_retry_wait(first) is True
    assert await asyncio.wait_for(bindings._interruptible_sleep(_FOREVER), ENGINE_TURN_TIMEOUT) is False

    await _notice(bindings)
    second = notices[-1].wait_id
    assert second not in {"", first}
    sleep = rig.start_sleep()
    assert bindings.skip_retry_wait(first) is False
    assert bindings.skip_retry_wait("") is False
    assert not await wait_until(sleep.done, timeout=0.2)

    assert bindings.skip_retry_wait(second) is True
    assert await asyncio.wait_for(sleep, ENGINE_TURN_TIMEOUT) is False


async def test_a_resend_without_a_wait_offers_nothing_to_skip(rig: _Rig) -> None:
    bindings, notices = rig.bindings, rig.notices
    await _notice(bindings, delay_seconds=0)

    assert notices[-1].wait_id == ""
    assert bindings.skip_retry_wait("") is False


async def test_an_interrupt_ends_the_wait_as_interrupted(rig: _Rig) -> None:
    bindings = rig.bindings
    await _notice(bindings)
    sleep = rig.start_sleep()
    assert not await wait_until(sleep.done, timeout=0.2)

    await bindings.interrupt()

    assert await asyncio.wait_for(sleep, ENGINE_TURN_TIMEOUT) is True


async def test_an_interrupt_with_a_skip_still_interrupts(rig: _Rig) -> None:
    bindings, notices = rig.bindings, rig.notices
    await _notice(bindings)
    sleep = rig.start_sleep()

    assert bindings.skip_retry_wait(notices[-1].wait_id) is True
    await bindings.interrupt()

    assert await asyncio.wait_for(sleep, ENGINE_TURN_TIMEOUT) is True


def _failed(request: httpx.Request) -> httpx.Response:
    return httpx.Response(500, json={"error": {"message": "temporary", "type": "server_error"}}, request=request)


def _scripted(*answers: Any) -> Any:
    queue = list(answers)

    def respond(request: httpx.Request) -> httpx.Response:
        if not queue:
            raise AssertionError(f"unscripted request: {request.method} {request.url}")
        answer = queue.pop(0)
        if callable(answer):
            return answer(request)
        return httpx.Response(answer.status, headers=list(answer.headers), content=answer.body, request=request)

    return respond


@pytest.mark.parametrize("lane", ["wire", "run"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_retry_now_sends_only_the_retry_it_was_clicked_for(
    lane: str, agent_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    if lane == "wire":
        profile = mock_provider_profile("openai", stream=False)
        answer = json_reply(cc_text("Sunny.", response_id="chatcmpl-1"))
    else:
        # Service-side storage: the whole run retries, with no wire lane.
        profile = mock_provider_profile(
            "openai", stream=False, api_style=API_STYLE_RESPONSES, chat_options='{"store": true}'
        )
        answer = json_reply(resp_response(response_id="resp_1", output=[resp_message("msg_1", "Sunny.")]))

    async def click(bus: EventBus, turn: ProviderTurnProgress) -> None:
        await wait_for(lambda: len(turn.retries) == 1, timeout=ENGINE_TURN_TIMEOUT, description="first notice")
        first = turn.retries[0].wait_id
        # A double click: the second one finds that wait over or names it still, never a later wait.
        await bus.publish(RetryWaitSkip(wait_id=first))
        await bus.publish(RetryWaitSkip(wait_id=first))
        await wait_for(lambda: len(turn.retries) == 2, timeout=ENGINE_TURN_TIMEOUT, description="second notice")
        second = turn.retries[1].wait_id
        assert second not in {"", first}
        await bus.publish(RetryWaitSkip(wait_id=first))
        assert not await wait_until(lambda: len(turn.requests) > 2, timeout=0.3)
        await bus.publish(RetryWaitSkip(wait_id=second))

    turn = await run_mock_provider_turn(
        agent_engine,
        monkeypatch,
        profile,
        _scripted(_failed, _failed, answer),
        backoff_seconds=(_FOREVER,),
        during_turn=click,
    )

    assert len(turn.requests) == 3
    assert [(retry.scope, retry.delay_seconds) for retry in turn.retries] == [(lane, _FOREVER)] * 2
    assert isinstance(turn.terminal, InvocationMessage)
    assert turn.terminal.text == "Sunny."
