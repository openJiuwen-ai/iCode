# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run one main-agent turn against a real provider SDK answering from ``httpx.MockTransport``.

The engine, the provider SDK and Chrys's client stack are real; only the
HTTP transport is scripted, so retry lanes, error classification and event
publication run exactly as in production.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import Error, InvocationMessage, InvocationRetryAttempt, UserMessage
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    SkillsConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import API_STYLE_CHAT_COMPLETIONS, ApiStyle, ModelProfile
from tests.support.engines import AgentEngineFactory
from tests.support.event_capture import capture_events
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for


@dataclass(frozen=True, slots=True)
class ProviderTurn:
    """What one turn sent and published."""

    requests: list[httpx.Request]
    retries: list[InvocationRetryAttempt]
    terminal: Error | InvocationMessage


@dataclass(frozen=True, slots=True)
class ProviderTurnProgress:
    """What a turn still running has sent and published so far: both lists grow as it goes on."""

    requests: list[httpx.Request]
    retries: list[InvocationRetryAttempt]


def mock_provider_profile(
    provider: str,
    *,
    stream: bool,
    http_max_retries: int = 0,
    api_style: ApiStyle = API_STYLE_CHAT_COMPLETIONS,
    chat_options: str = "",
) -> ModelProfile:
    """Return a model profile for *provider* pointing at a fake host."""
    return ModelProfile(
        id=f"mock-{provider}",
        name=f"mock-{provider}",
        provider=provider,
        api_style=api_style,
        model_id="test-model",
        base_url="https://provider.example/v1" if provider != "anthropic" else "https://provider.example",
        api_key="test-key",
        http_max_retries=http_max_retries,
        stream=stream,
        chat_options=chat_options,
    )


async def run_mock_provider_turn(
    agent_engine: AgentEngineFactory,
    monkeypatch: pytest.MonkeyPatch,
    model_profile: ModelProfile,
    respond: Callable[[httpx.Request], httpx.Response],
    *,
    user_message: UserMessage | None = None,
    backoff_seconds: tuple[int, ...] = (0,),
    during_turn: Callable[[EventBus, ProviderTurnProgress], Awaitable[None]] | None = None,
) -> ProviderTurn:
    """Run one turn of *user_message* (default ``hello``) whose provider HTTP traffic *respond* answers.

    Retries back off *backoff_seconds* (0 s by default). *during_turn*, given, runs once the message is
    published and before the turn's terminal event is awaited.
    """
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return respond(request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    import chrys.service.llm.clients as clients_module

    monkeypatch.setattr(clients_module, "_build_profile_http_client", lambda *args, **kwargs: http_client)
    monkeypatch.setattr(TurnBindings, "_BACKOFF_SCHEDULE", backoff_seconds)
    registry = ModelProfileRegistry()
    registry.register(model_profile)
    bus = EventBus()
    terminal: asyncio.Future[Error | InvocationMessage] = asyncio.get_running_loop().create_future()

    async def on_error(event: Error) -> None:
        if not terminal.done():
            terminal.set_result(event)

    async def on_message(event: InvocationMessage) -> None:
        if event.is_final and not terminal.done():
            terminal.set_result(event)

    await bus.subscribe(Error, on_error)
    await bus.subscribe(InvocationMessage, on_message)
    retries = await capture_events(bus, InvocationRetryAttempt)
    engine = agent_engine(bus, settings=Settings(model_profile=model_profile.id), model_registry=registry)
    profile = AgentProfile(
        name="mock-provider-turn",
        instructions="Reply briefly.",
        tools=ToolsConfig(builtins=[]),
        skills=SkillsConfig(auto_load_user_agents_skills=False, auto_load_cwd_agents_skills=False),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )
    try:
        await engine.start(profile)
        await bus.publish(user_message or UserMessage(text="hello"))
        if during_turn is not None:
            await during_turn(bus, ProviderTurnProgress(requests=requests, retries=retries))
        await wait_for(terminal.done, timeout=ENGINE_TURN_TIMEOUT, description="terminal engine event")
    finally:
        await engine.shutdown()
        await http_client.aclose()
    return ProviderTurn(requests=requests, retries=retries, terminal=terminal.result())
