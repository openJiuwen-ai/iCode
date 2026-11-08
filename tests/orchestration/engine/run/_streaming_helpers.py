# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A main-turn engine on a mock client whose first stream holds mid-response, and what it published and saved."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    Error,
    InvocationAnswerWithdrawn,
    InvocationContentAbandoned,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationMessage,
    InvocationRetryAttempt,
    InvocationToolCallStart,
)
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workspace import Workspace
from chrys.kernel import ChatResponseUpdate, Content, FunctionTool
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.engine.run._engine_run_helpers import _PROFILE, _make_registry
from tests.support.event_capture import collect_events
from tests.support.pipeline_helpers import echo_tool, make_mock_settings_and_registry
from tests.support.tool_packs import install_tools
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from chrys.orchestration.engine.engine import AgentEngine

_EVENT_TYPES: tuple[type, ...] = (
    InvocationAnswerWithdrawn,
    InvocationContentAbandoned,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationMessage,
    InvocationToolCallStart,
    InvocationRetryAttempt,
    Error,
)


class _HeldStreamClient(MockChatClient):
    """Holds its first stream after the first text update until released, then continues or fails.

    *then* streams right after that text, before the hold. *hold_after* names
    the content type whose first update the stream holds after;
    *hold_stream* which stream holds, counting from 1.
    """

    def __init__(
        self,
        responses: list[MockResponse],
        *,
        failure: BaseException | None = None,
        then: tuple[Content, ...] = (),
        hold_after: str = "text",
        hold_stream: int = 1,
    ) -> None:
        super().__init__(responses=responses)
        self.shown = asyncio.Event()
        self.release = asyncio.Event()
        self._failure = failure
        self._then = then
        self._hold_after = hold_after
        self._hold_stream = hold_stream
        self._streams = 0

    async def _stream_updates(
        self, resp: MockResponse, model_id: str, finish_reason: Any
    ) -> AsyncIterator[ChatResponseUpdate]:
        self._streams += 1
        hold = self._streams == self._hold_stream
        async for update in super()._stream_updates(resp, model_id, finish_reason):
            yield update
            if hold and any(content.type == self._hold_after for content in update.contents):
                if self._then:
                    yield ChatResponseUpdate(role="assistant", contents=list(self._then), model=model_id)
                # Resumed only once the caller asks for the next update: the first one was handled.
                hold = False
                self.shown.set()
                await self.release.wait()
                if self._failure is not None:
                    raise self._failure


@dataclass(slots=True)
class _Started:
    engine: AgentEngine
    bus: EventBus
    events: list[object]
    store: JsonFileStateStore


async def _start(
    agent_engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: MockChatClient,
    *,
    stream: bool = True,
    surface: SessionSurface | None = None,
    service_side: bool = False,
    event_types: Sequence[type] = (),
    tools: Sequence[FunctionTool] = (echo_tool,),
) -> _Started:
    events: list[object] = []
    bus = EventBus()
    for cls in (*_EVENT_TYPES, *event_types):
        await bus.subscribe(cls, lambda event, _events=events: collect_events(_events, event))
    settings, model_registry = make_mock_settings_and_registry(stream=stream)
    if service_side:
        # Responses with `store: true`: the provider keeps the conversation, so a retry runs the whole pass again.
        model_registry = ModelProfileRegistry()
        model_registry.register(
            ModelProfile(
                id="mock-profile",
                name="mock",
                provider="openai",
                api_style="responses",
                model_id="gpt-mock",
                stream=stream,
                max_context_tokens=100_000,
                chat_options='{"store": true}',
            )
        )

    async def _create_client(_settings: object = None, **kwargs: Any) -> MockChatClient:
        # A wire client numbers tool batches through the build's callbacks; the mock needs them handed over.
        client._on_intermediate_text = kwargs.get("on_intermediate_text")
        return client

    monkeypatch.setattr(builder_module, "create_client", _create_client)
    install_tools(monkeypatch, list(tools))
    store = JsonFileStateStore(tmp_path)
    engine = agent_engine(
        bus,
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=model_registry,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
        surface=surface,
    )
    await engine.start(_PROFILE)
    return _Started(engine=engine, bus=bus, events=events, store=store)


def _turn_events[E](events: list[object], cls: type[E]) -> list[E]:
    return [event for event in events if isinstance(event, cls) and event.origin.kind == "turn"]


async def _wait_shown(started: _Started, client: _HeldStreamClient) -> None:
    """Wait until the held stream showed its first text, surfacing a run that ended first."""

    def _shown_or_ended() -> bool:
        task = started.engine.turns.turn_state.lease.run_task
        return client.shown.is_set() or (task is not None and task.done())

    await wait_for(_shown_or_ended, timeout=ENGINE_TURN_TIMEOUT, description="the stream's first text shown")
    assert client.shown.is_set(), "the run ended before its stream showed any text"


async def _saved_assistant_messages(started: _Started) -> list[dict[str, Any]]:
    saved = await started.store.load_session_raw(started.engine.session.session_id)
    assert saved is not None
    return [message for message in saved if message["role"] == "assistant"]


def _texts(message: dict[str, Any]) -> list[str]:
    return [content["text"] for content in message["contents"] if content["type"] == "text"]


def _marker(message: dict[str, Any]) -> object:
    return message["additional_properties"].get(HistoryMarkerKind.KEY)
