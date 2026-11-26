# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Resources for invoker contract and lifecycle tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.kernel import Agent, AgentSession, LoopRecorder
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.invoker.resources import Conversation
from chrys.service.acp_client import AcpAgentClient, AcpHandshakeInfo
from chrys.service.agent_middleware import ApprovalMiddleware, AskUserMiddleware
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.approval.policy import ApprovalPolicy
from chrys.service.llm.mock import MockChatClient
from chrys.service.profiles.agents.schema import ApprovalConfig
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, ENGINE_TURN_TIMEOUT, wait_for


@pytest.fixture
async def executor() -> AsyncIterator[TurnBindings]:
    bus = EventBus()
    instance = TurnBindings(
        conversation=Conversation(),
        agent=Agent(client=MockChatClient()),
        session=AgentSession(),
        event_bus=bus,
        approval_middleware=ApprovalMiddleware(ApprovalPolicy(ApprovalConfig()), bus),
        ask_user_middleware=AskUserMiddleware(bus),
        injection_middleware=InjectionMiddleware(),
        loop_recorder=LoopRecorder(),
    )
    instance.resource_scope.own(instance.approval.close)
    try:
        yield instance
    finally:
        await instance.resource_scope.aclose()


@pytest.fixture
def wait_for_child_window(monkeypatch: pytest.MonkeyPatch):
    """Observe the first real handshake before timing the prompt or close window."""
    started = asyncio.Event()
    outcomes: list[BaseException | None] = []
    connect = AcpAgentClient.connect
    open_session = AcpAgentClient.open_session

    async def observe_connect(client: AcpAgentClient) -> None:
        started.set()
        try:
            await connect(client)
        except BaseException as error:
            outcomes.append(error)
            raise

    async def observe_session(client: AcpAgentClient) -> AcpHandshakeInfo:
        try:
            session = await open_session(client)
        except BaseException as error:
            outcomes.append(error)
            raise
        outcomes.append(None)
        return session

    monkeypatch.setattr(AcpAgentClient, "connect", observe_connect)
    monkeypatch.setattr(AcpAgentClient, "open_session", observe_session)

    async def wait(
        operation: asyncio.Task,
        entered: asyncio.Event,
        description: str,
        *,
        acp: bool = True,
        window_timeout: float = ENGINE_TURN_TIMEOUT,
    ) -> None:
        if acp:
            await wait_for(
                lambda: started.is_set() or operation.done(),
                timeout=ENGINE_TURN_TIMEOUT,
                description="child starts connecting to the ACP agent",
            )
            await wait_for(
                lambda: outcomes or operation.done(),
                timeout=ENGINE_TEST_WAIT_TIMEOUT,
                description="child completes ACP initialize and session/new",
            )
            assert outcomes, operation.result()
            if (error := outcomes[0]) is not None:
                raise error
        await wait_for(lambda: entered.is_set() or operation.done(), timeout=window_timeout, description=description)
        assert entered.is_set(), operation.result()

    return wait
