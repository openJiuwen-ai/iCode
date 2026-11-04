# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lowering the image size limit shrinks the session's earlier images on its next turn or retry."""

from __future__ import annotations

import asyncio
import base64
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.config.env_layers import freeze_process_env
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import persist
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ImageAttachmentCompressionFinished,
    ImageAttachmentCompressionStarted,
    SettingsReload,
    UserMessage,
    UserRetry,
)
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.text.images import MAX_IMAGE_BYTES, inspect_image_dimensions
from chrys.kernel import Content, Message
from chrys.orchestration.engine.engine import AgentEngine
from chrys.orchestration.engine.run import prompt_content as prompt_content_module
from chrys.orchestration.engine.run.prompt_content import PromptContentPreparer
from chrys.orchestration.engine.state.machine import EngineStateMachine
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import AgentProfile, ApprovalConfig, CompactionConfig, ToolsConfig
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.session.history import SessionHistoryManager
from chrys.service.state.store import JsonFileStateStore
from tests.support.engines import AgentEngineFactory
from tests.support.event_capture import capture_events
from tests.support.images import broken_chunk_png, noisy_image_bytes
from tests.support.waiting import await_run_task_chain, wait_for

_LOWERED_KB = 64
_PROFILE = AgentProfile(
    name="Images",
    instructions="Look at images.",
    tools=ToolsConfig(builtins=["filesystem.read"]),
    approval=ApprovalConfig(default="auto"),
    compaction=CompactionConfig(enabled=False),
)


@dataclass(frozen=True)
class _SentImages:
    """The images one request carried, read as it was sent: the history shrinks the same Contents later."""

    attached: list[bytes]
    viewed: list[bytes]

    @classmethod
    def of(cls, messages: Sequence[Message]) -> _SentImages:
        return cls(
            attached=[
                base64.b64decode(content.uri.split(",", 1)[1])
                for message in messages
                if message.role == "user"
                for content in message.contents
                if content.type == "data" and content.uri
            ],
            viewed=[
                base64.b64decode(item.uri.split(",", 1)[1])
                for message in messages
                for content in message.contents
                if content.type == "function_result"
                for item in content.items or ()
                if item.type == "data" and item.uri
            ],
        )


class _GatewayClient(MockChatClient):
    """A mock model behind a gateway that refuses the request numbered *refused_request* as too large."""

    def __init__(self, responses: list[MockResponse], *, refused_request: int | None = None) -> None:
        super().__init__(responses=responses)
        self.requests: list[_SentImages] = []
        self._refused_request = refused_request

    def _inner_get_response(
        self, *, messages: Sequence[Message], stream: bool, options: Mapping[str, Any], **kwargs: Any
    ) -> Any:
        self.requests.append(_SentImages.of(messages))
        if len(self.requests) == self._refused_request:
            raise RuntimeError("413 Payload Too Large")
        return super()._inner_get_response(messages=messages, stream=stream, options=options, **kwargs)


async def _saved_images(store: JsonFileStateStore, session_id: str) -> list[bytes]:
    messages = await store.load_session_raw(session_id)
    assert messages is not None
    return [
        base64.b64decode(item["uri"].split(",", 1)[1])
        for message in messages
        for content in message["contents"]
        if content["type"] == "function_result"
        for item in content.get("items") or ()
        if item["type"] == "data"
    ]


async def _started_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine: AgentEngineFactory, client: MockChatClient
) -> tuple[EventBus, AgentEngine, JsonFileStateStore]:
    freeze_process_env()
    models = ModelProfileRegistry()
    models.register(
        ModelProfile(id="vision-mock", name="vision mock", provider="mock", model_id="mock", vision=True, stream=False)
    )
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    bus = EventBus()
    store = JsonFileStateStore(tmp_path / "sessions")
    engine = agent_engine(
        bus,
        settings=Settings(model_profile="vision-mock", workspace_change_notice=False),
        model_registry=models,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    engine.pin_model_profile()
    await engine.start(_PROFILE)
    return bus, engine, store


def _tool_result(*items: Content) -> Message:
    return Message("tool", [Content.from_function_result("view-1", result=list(items))])


def _viewed_image(tmp_path: Path) -> Path:
    image = tmp_path / "shot.png"
    image.write_bytes(noisy_image_bytes("PNG"))
    return image


async def _lower_the_limit(bus: EventBus) -> None:
    persist({"tools.image.send_limit_kb": _LOWERED_KB})
    await bus.publish(SettingsReload(), raise_handler_errors=True)


async def _turn(engine: AgentEngine, publish: Callable[[], Awaitable[None]]) -> None:
    await publish()
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)


async def test_retry_after_lowering_the_limit_sends_the_viewed_image_within_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine: AgentEngineFactory
) -> None:
    image = _viewed_image(tmp_path)
    source = image.read_bytes()
    client = _GatewayClient(
        [MockResponse(tool_calls=[("view_image", "view-1", {"path": str(image)})]), MockResponse(text="Seen")],
        refused_request=2,
    )
    bus, engine, store = await _started_engine(tmp_path, monkeypatch, agent_engine, client)
    shrinking = await capture_events(bus, ImageAttachmentCompressionStarted)

    await _turn(engine, lambda: bus.publish(UserMessage(text="Look at shot.png"), raise_handler_errors=True))
    (refused,) = client.requests[1].viewed
    assert _LOWERED_KB * 1024 < len(refused) <= MAX_IMAGE_BYTES

    await _lower_the_limit(bus)
    await _turn(engine, lambda: bus.publish(UserRetry(), raise_handler_errors=True))

    assert len(client.requests) == 3
    (sent,) = client.requests[2].viewed
    assert len(sent) <= _LOWERED_KB * 1024
    assert sent.startswith(b"\xff\xd8\xff")
    assert [event.image_count for event in shrinking] == [1]
    # The save keeps the image shrunk; the file on disk is untouched.
    assert await _saved_images(store, engine.session.session_id) == [sent]
    assert image.read_bytes() == source


async def test_next_turn_after_lowering_the_limit_shrinks_earlier_images_and_only_then(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine: AgentEngineFactory
) -> None:
    image = _viewed_image(tmp_path)
    client = _GatewayClient(
        [
            MockResponse(tool_calls=[("view_image", "view-1", {"path": str(image)})]),
            MockResponse(text="Seen"),
            MockResponse(text="Still seen"),
            MockResponse(text="Seen smaller"),
        ]
    )
    bus, engine, store = await _started_engine(tmp_path, monkeypatch, agent_engine, client)
    shrinking = await capture_events(bus, ImageAttachmentCompressionStarted)

    await _turn(engine, lambda: bus.publish(UserMessage(text="Look at shot.png"), raise_handler_errors=True))
    await _turn(engine, lambda: bus.publish(UserMessage(text="Again"), raise_handler_errors=True))
    (viewed,) = client.requests[1].viewed
    # Within the limit it was sent at, the image is left as it is.
    assert client.requests[2].viewed == [viewed]
    assert shrinking == []

    await _lower_the_limit(bus)
    await _turn(engine, lambda: bus.publish(UserMessage(text="Once more"), raise_handler_errors=True))

    (sent,) = client.requests[3].viewed
    assert len(sent) <= _LOWERED_KB * 1024 < len(viewed)
    width, height = inspect_image_dimensions(sent)
    viewed_width, viewed_height = inspect_image_dimensions(viewed)
    assert width <= viewed_width
    assert height <= viewed_height
    assert [event.image_count for event in shrinking] == [1]
    assert await _saved_images(store, engine.session.session_id) == [sent]


def _preparer(bus: EventBus, history: SessionHistoryManager, *, timeout: float = 30.0) -> PromptContentPreparer:
    return PromptContentPreparer(
        session=SimpleNamespace(session_id="s1"),  # type: ignore[arg-type]
        current=SimpleNamespace(),  # type: ignore[arg-type]
        bus=bus,
        history=history,
        fsm=EngineStateMachine(),
        compression_timeout_seconds=timeout,
        image_send_limit=lambda: _LOWERED_KB * 1024,
    )


class _HeldShrink:
    """Holds the shrink of the image numbered *held* (from 1) in its worker thread until released."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, held: int) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self._held = held
        self._calls = 0
        self._shrink = prompt_content_module._shrink_image
        monkeypatch.setattr(prompt_content_module, "_shrink_image", self)

    def __call__(self, uri: str, max_bytes: int) -> Any:
        self._calls += 1
        if self._calls == self._held:
            self.started.set()
            # Bounded: on the event loop, a regression would block the test forever.
            self.release.wait(timeout=10)
        return self._shrink(uri, max_bytes)


async def test_a_cancelled_shrink_leaves_the_image_being_shrunk_as_it_was(monkeypatch: pytest.MonkeyPatch) -> None:
    shrink = _HeldShrink(monkeypatch, held=1)
    started, release = shrink.started, shrink.release
    attached = Content.from_data(noisy_image_bytes("PNG"), "image/png")
    uri = attached.uri
    history = SessionHistoryManager()
    history.bind({"messages": [Message("user", ["look", attached])]})
    bus = EventBus()
    finished = await capture_events(bus, ImageAttachmentCompressionFinished)
    preparer = _preparer(bus, history)

    task = asyncio.create_task(preparer.shrink_history_images())
    try:
        await wait_for(started.is_set, description="history images shrinking")
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()

    assert attached.uri is uri
    assert attached.media_type == "image/png"
    assert [event.image_count for event in finished] == [1]


async def test_a_shrink_past_the_timeout_keeps_the_images_it_finished(monkeypatch: pytest.MonkeyPatch) -> None:
    shrink = _HeldShrink(monkeypatch, held=2)
    first = Content.from_data(noisy_image_bytes("PNG"), "image/png")
    second = Content.from_data(noisy_image_bytes("PNG"), "image/png")
    second_uri = second.uri
    history = SessionHistoryManager()
    history.bind({"messages": [Message("user", ["look", first]), _tool_result(second)]})
    bus = EventBus()
    finished = await capture_events(bus, ImageAttachmentCompressionFinished)
    # The test says when the timeout expires: once the first image is done.
    timeouts: list[tuple[float | None, asyncio.Timeout]] = []

    def controlled_timeout(delay: float | None) -> asyncio.Timeout:
        timeouts.append((delay, timeout := asyncio.timeout(None)))
        return timeout

    controlled_asyncio = ModuleType("asyncio")
    controlled_asyncio.__dict__.update(vars(asyncio), timeout=controlled_timeout)
    monkeypatch.setattr(prompt_content_module, "asyncio", controlled_asyncio)
    preparer = _preparer(bus, history, timeout=12.5)

    task = asyncio.create_task(preparer.shrink_history_images())
    try:
        await wait_for(shrink.started.is_set, description="second history image shrinking")
        ((delay, timeout),) = timeouts
        assert delay == 12.5
        assert not task.done()
        timeout.reschedule(asyncio.get_running_loop().time())
        # The held second image outlasts the timeout: the shrink returns without it.
        await task
    finally:
        shrink.release.set()

    assert first.media_type == "image/jpeg"
    assert second.uri is second_uri
    assert [event.image_count for event in finished] == [2]
    # The next turn or retry goes on with the rest.
    (rest,) = history.images_over(_LOWERED_KB * 1024)
    assert rest.content is second


async def test_attachments_and_viewed_images_follow_a_lowered_limit_from_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine: AgentEngineFactory
) -> None:
    image = _viewed_image(tmp_path)
    client = _GatewayClient(
        [MockResponse(tool_calls=[("view_image", "view-1", {"path": str(image)})]), MockResponse(text="Seen")]
    )
    bus, engine, _store = await _started_engine(tmp_path, monkeypatch, agent_engine, client)
    shrinking = await capture_events(bus, ImageAttachmentCompressionStarted)
    await _lower_the_limit(bus)

    await _turn(engine, lambda: bus.publish(UserMessage(text="Look at @shot.png"), raise_handler_errors=True))

    sizes = [len(data) for data in [*client.requests[1].attached, *client.requests[1].viewed]]
    assert len(sizes) == 2
    assert all(size <= _LOWERED_KB * 1024 for size in sizes)
    # The one notice is the attachment's: both images were made within the
    # limit, so the history had nothing to shrink.
    assert [event.image_count for event in shrinking] == [1]


async def test_an_image_the_decoder_fails_on_is_kept_as_it_is() -> None:
    broken = Content.from_data(broken_chunk_png(), "image/png")
    broken_uri = broken.uri
    viewed = Content.from_data(noisy_image_bytes("PNG"), "image/png")
    history = SessionHistoryManager()
    history.bind({"messages": [Message("user", ["look", broken]), _tool_result(viewed)]})

    await _preparer(EventBus(), history).shrink_history_images()

    assert broken.uri is broken_uri
    assert viewed.media_type == "image/jpeg"


async def test_a_shrink_that_fails_unexpectedly_fails_no_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(_data: bytes, *, max_bytes: int) -> Any:
        raise RuntimeError(f"decoder bug at {max_bytes}")

    monkeypatch.setattr(prompt_content_module, "shrink_image_data", failing)
    attached = Content.from_data(noisy_image_bytes("PNG"), "image/png")
    uri = attached.uri
    history = SessionHistoryManager()
    history.bind({"messages": [Message("user", ["look", attached])]})
    bus = EventBus()
    finished = await capture_events(bus, ImageAttachmentCompressionFinished)

    await _preparer(bus, history).shrink_history_images()

    assert attached.uri is uri
    assert [event.image_count for event in finished] == [1]
