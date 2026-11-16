# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for MockChatClient."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import pytest

from chrys.kernel import (
    ChatContext,
    ChatMiddleware,
    ChatResponse,
    CompactionCallContext,
    Content,
    LastWordsCompletion,
    Message,
    internal_side_call_scope,
)
from chrys.service.llm.mock import MockChatClient, MockResponse


@pytest.mark.asyncio
async def test_non_streaming_text_response() -> None:
    client = MockChatClient(responses=[MockResponse(text="Hello world")])
    resp = await client.get_response([Message(role="user", contents=["Hi"])])
    assert resp.text == "Hello world"
    assert resp.finish_reason == "stop"
    assert resp.model == "mock-model"
    assert client.call_count == 1


@pytest.mark.asyncio
async def test_non_streaming_conversation_id() -> None:
    client = MockChatClient(responses=[MockResponse(text="Hello world", conversation_id="resp_123")])
    resp = await client.get_response([Message(role="user", contents=["Hi"])])
    assert resp.conversation_id == "resp_123"


@pytest.mark.asyncio
async def test_non_streaming_tool_call() -> None:
    client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("read_file", "call_1", {"path": "foo.py"})]),
        ]
    )
    resp = await client.get_response([Message(role="user", contents=["Read foo"])])
    assert resp.finish_reason == "tool_calls"
    msg = resp.messages[0]
    assert msg.role == "assistant"
    fc = [c for c in msg.contents if c.type == "function_call"]
    assert len(fc) == 1
    assert fc[0].name == "read_file"
    assert fc[0].call_id == "call_1"


@pytest.mark.asyncio
async def test_non_streaming_mixed() -> None:
    client = MockChatClient(
        responses=[
            MockResponse(
                text="Let me read that.",
                tool_calls=[("read_file", "c1", {"path": "a.py"})],
            ),
        ]
    )
    resp = await client.get_response([Message(role="user", contents=["Go"])])
    assert resp.finish_reason == "tool_calls"
    msg = resp.messages[0]
    types = [c.type for c in msg.contents]
    assert "function_call" in types
    assert "text" in types


@pytest.mark.asyncio
async def test_streaming_text() -> None:
    client = MockChatClient(
        responses=[
            MockResponse(text="abcdef", chunk_size=3, chunk_delay=0),
        ]
    )
    # get_response(stream=True) returns ResponseStream which is both
    # awaitable and async-iterable; await resolves the inner stream.
    stream = await client.get_response(  # type: ignore[misc]
        [Message(role="user", contents=["Hi"])],
        stream=True,
    )
    chunks: list[str] = []
    async for update in stream:
        if update.text:
            chunks.append(update.text)
    assert "".join(chunks) == "abcdef"
    assert len(chunks) == 2  # "abc" + "def"

    final = await stream.get_final_response()  # type: ignore[union-attr]
    assert final.finish_reason == "stop"


@pytest.mark.asyncio
async def test_streaming_conversation_id() -> None:
    client = MockChatClient(
        responses=[
            MockResponse(text="abcdef", conversation_id="resp_stream", chunk_size=3, chunk_delay=0),
        ]
    )
    stream = await client.get_response(  # type: ignore[misc]
        [Message(role="user", contents=["Hi"])],
        stream=True,
    )
    updates = [update async for update in stream]
    assert {update.conversation_id for update in updates} == {"resp_stream"}
    final = await stream.get_final_response()  # type: ignore[union-attr]
    assert final.conversation_id == "resp_stream"


@pytest.mark.asyncio
async def test_streaming_tool_calls() -> None:
    client = MockChatClient(
        responses=[
            MockResponse(
                tool_calls=[("zsh", "c1", {"command": "ls"})],
                chunk_delay=0,
            ),
        ]
    )
    stream = await client.get_response(  # type: ignore[misc]
        [Message(role="user", contents=["List files"])],
        stream=True,
    )
    updates = []
    async for update in stream:
        updates.append(update)
    # Tool call chunk + empty final chunk
    fc_updates = [u for u in updates if any(c.type == "function_call" for c in u.contents)]
    assert len(fc_updates) == 1


@pytest.mark.asyncio
async def test_multiple_responses_in_order() -> None:
    client = MockChatClient(
        responses=[
            MockResponse(text="First"),
            MockResponse(text="Second"),
            MockResponse(text="Third"),
        ]
    )
    msgs = [Message(role="user", contents=["Go"])]
    r1 = await client.get_response(msgs)
    r2 = await client.get_response(msgs)
    r3 = await client.get_response(msgs)
    assert r1.text == "First"
    assert r2.text == "Second"
    assert r3.text == "Third"
    assert client.call_count == 3


@pytest.mark.asyncio
async def test_exhausted_responses_fallback() -> None:
    client = MockChatClient(responses=[MockResponse(text="Only one")])
    msgs = [Message(role="user", contents=["Go"])]
    await client.get_response(msgs)
    r2 = await client.get_response(msgs)
    assert "No more scripted responses" in r2.text


@pytest.mark.asyncio
async def test_call_history() -> None:
    client = MockChatClient(responses=[MockResponse(text="OK")])
    msgs = [Message(role="user", contents=["Hello"])]
    await client.get_response(msgs)
    assert len(client.call_history) == 1
    recorded_msgs, _opts = client.call_history[0]
    assert recorded_msgs[0].role == "user"


@pytest.mark.asyncio
async def test_request_message_observer_includes_exact_mock_wire_messages() -> None:
    client = MockChatClient(responses=[MockResponse(text="OK")])
    request_content = Content.from_text("Hello")
    request = Message(role="user", contents=[request_content])
    observed: list[Sequence[Message]] = []

    await client.get_response([request], request_message_observer=observed.append)

    assert observed
    recorded_messages, _options = client.call_history[0]
    assert observed[-1][0] is recorded_messages[0]
    assert observed[-1][0] is not request
    assert observed[-1][0].contents[0] is request_content


@pytest.mark.asyncio
async def test_add_response_and_reset() -> None:
    client = MockChatClient()
    client.add_response(MockResponse(text="Dynamic"))
    msgs = [Message(role="user", contents=["Go"])]
    r = await client.get_response(msgs)
    assert r.text == "Dynamic"
    assert client.call_count == 1

    client.reset()
    assert client.call_count == 0
    r2 = await client.get_response(msgs)
    assert r2.text == "Dynamic"


@pytest.mark.asyncio
async def test_custom_model_id() -> None:
    client = MockChatClient(
        responses=[MockResponse(text="Hi", model_id="custom-v1")],
        default_model_id="default-v0",
    )
    r = await client.get_response([Message(role="user", contents=["Hey"])])
    assert r.model == "custom-v1"


@pytest.mark.asyncio
async def test_custom_finish_reason() -> None:
    client = MockChatClient(responses=[MockResponse(text="...", finish_reason="length")])
    r = await client.get_response([Message(role="user", contents=["Go"])])
    assert r.finish_reason == "length"


# ──────────────── LAST_WORDS completer through the mock stack ────────────
#
# Beneath its tool loop the mock calls BaseChatClient.get_response, so it
# builds the same per-call CompactionCallContext (via
# _build_compaction_call_context) and mock-stack Phase 4 exercises the
# cache-safe completer path — including the store-mode gate and side-call
# intermediate-text suppression — instead of silently diverging to the
# reconstruction fallback.


class _ContextCapturingStrategy:
    def __init__(self) -> None:
        self.contexts: list[Any] = []

    async def __call__(self, messages: list[Any], context: Any = None) -> bool:
        self.contexts.append(context)
        return False


@pytest.mark.asyncio
async def test_mock_stack_offers_completer_context_to_strategy() -> None:
    client = MockChatClient(responses=[MockResponse(text="final")])
    strategy = _ContextCapturingStrategy()

    resp = await client.get_response(
        [Message(role="user", contents=["Hi"])],
        options={"model": "mock"},
        compaction_strategy=strategy,
    )

    assert resp.text == "final"
    context = strategy.contexts[0]
    assert isinstance(context, CompactionCallContext)
    assert context.last_words_completer is not None


@pytest.mark.asyncio
async def test_mock_stack_store_mode_gates_completer_off() -> None:
    client = MockChatClient(responses=[MockResponse(text="final")])
    strategy = _ContextCapturingStrategy()

    await client.get_response(
        [Message(role="user", contents=["Hi"])],
        options={"model": "mock", "store": True},
        compaction_strategy=strategy,
    )

    assert isinstance(strategy.contexts[0], CompactionCallContext)
    assert strategy.contexts[0].last_words_completer is None


@pytest.mark.asyncio
async def test_mock_stack_completer_side_call_consumes_scripted_response() -> None:
    """A Phase-4-like strategy's side call goes through the mock's own wire
    method: the side call consumes the first scripted response as the note,
    the live call the second."""

    class _Phase4LikeStrategy:
        def __init__(self) -> None:
            self.note: str | None = None

        async def __call__(self, messages: list[Any], context: Any = None) -> bool:
            self.note = await context.last_words_completer.complete_last_words(
                list(messages), "SUMMARIZE", max_output_tokens=50
            )
            return False

    client = MockChatClient(responses=[MockResponse(text="side note"), MockResponse(text="live answer")])
    strategy = _Phase4LikeStrategy()

    resp = await client.get_response(
        [Message(role="user", contents=["Hi"])],
        options={"model": "mock"},
        compaction_strategy=strategy,
    )

    assert strategy.note == LastWordsCompletion("side note")
    assert resp.text == "live answer"
    side_messages, side_options = client.call_history[0]
    assert side_messages[-1].text == "SUMMARIZE"
    assert side_options.get("max_tokens") == 50


@pytest.mark.asyncio
async def test_mock_stack_observes_and_views_post_compaction_summary() -> None:
    """Beneath its tool loop the mock keeps the same final provider boundary as Base."""
    summary_content = Content.from_text("COMPACTED")
    summary = Message("assistant", [summary_content], additional_properties={"summary": True})
    seen_provider_messages: list[Message] = []

    class _EchoingMock(MockChatClient):
        def _inner_get_response(
            self,
            *,
            messages: Sequence[Message],
            stream: bool,
            options: Mapping[str, Any],
            **kwargs: Any,
        ) -> Any:
            del stream, options, kwargs
            provider_summary = messages[0]
            seen_provider_messages.append(provider_summary)
            provider_summary.contents.append(Content.from_text("WIRE MUTATION"))

            async def response() -> ChatResponse:
                return ChatResponse(messages=[Message("assistant", [summary_content, Content.from_text("done")])])

            return response()

    async def insert_summary(messages: list[Message], _context: object) -> None:
        messages.insert(0, summary)

    client = _EchoingMock()
    response = await client.get_response(
        [Message("user", ["hi"])],
        options={},
        compaction_strategy=insert_summary,
    )

    assert response.text == "done"
    assert summary.contents == [summary_content]
    assert len(seen_provider_messages) == 1
    assert seen_provider_messages[0] is not summary
    assert seen_provider_messages[0].additional_properties is summary.additional_properties


class _SetCallKwargs(ChatMiddleware):
    def __init__(self, kwargs: Mapping[str, Any], function_invocation_kwargs: Mapping[str, Any] | None = None) -> None:
        self._kwargs = kwargs
        self._function_invocation_kwargs = function_invocation_kwargs

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        context.kwargs.update(self._kwargs)
        if self._function_invocation_kwargs is not None:
            context.function_invocation_kwargs = self._function_invocation_kwargs
        await call_next()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("compacting", [False, True])
@pytest.mark.parametrize("client_kwargs", [None, {"max_tokens": 20}], ids=["joins", "overrides"])
async def test_mock_middleware_kwargs_join_and_override_client_kwargs(
    stream: bool, compacting: bool, client_kwargs: dict[str, Any] | None
) -> None:
    client = MockChatClient(responses=[MockResponse(text="capped")], middleware=[_SetCallKwargs({"max_tokens": 7})])
    strategy = _ContextCapturingStrategy() if compacting else None
    messages = [Message(role="user", contents=["Hi"])]
    options = {"model": "mock"}

    if stream:
        response = await client.get_response(
            messages, stream=True, options=options, compaction_strategy=strategy, client_kwargs=client_kwargs
        ).get_final_response()
    else:
        response = await client.get_response(
            messages, options=options, compaction_strategy=strategy, client_kwargs=client_kwargs
        )

    assert response.text == "capped"
    _messages, sent_options = client.call_history[0]
    assert sent_options == {"model": "mock", "max_tokens": 7}
    if strategy is not None:
        assert len(strategy.contexts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("client_function_invocation_kwargs", [None, {"y": 0}], ids=["joins", "overrides"])
@pytest.mark.parametrize("middleware_function_invocation_kwargs", [{"x": 1}, None], ids=["middleware", "no-middleware"])
async def test_mock_wire_method_receives_client_kwargs_and_layer_function_invocation_kwargs(
    client_function_invocation_kwargs: dict[str, Any] | None,
    middleware_function_invocation_kwargs: dict[str, Any] | None,
) -> None:
    # Without middleware the chat layer still passes function_invocation_kwargs=None,
    # and that None overrides a value nested in client_kwargs.
    seen_kwargs: list[dict[str, Any]] = []

    class _KwargsSpyMock(MockChatClient):
        def _inner_get_response(
            self,
            *,
            messages: Sequence[Message],
            stream: bool,
            options: Mapping[str, Any],
            **kwargs: Any,
        ) -> Any:
            seen_kwargs.append(dict(kwargs))
            return super()._inner_get_response(messages=messages, stream=stream, options=options, **kwargs)

    client_kwargs: dict[str, Any] = {"keep": "client"}
    if client_function_invocation_kwargs is not None:
        client_kwargs["function_invocation_kwargs"] = client_function_invocation_kwargs
    middleware = (
        [_SetCallKwargs({}, function_invocation_kwargs=middleware_function_invocation_kwargs)]
        if middleware_function_invocation_kwargs is not None
        else []
    )
    client = _KwargsSpyMock(responses=[MockResponse(text="OK")], middleware=middleware)

    await client.get_response([Message(role="user", contents=["Hi"])], client_kwargs=client_kwargs)

    assert seen_kwargs == [{"keep": "client", "function_invocation_kwargs": middleware_function_invocation_kwargs}]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_mock_intermediate_callback_suppressed_in_internal_side_call(stream: bool) -> None:
    fired: list[str] = []
    client = MockChatClient(
        responses=[
            MockResponse(text="thinking", tool_calls=[("t", "c1", {})]),
            MockResponse(text="thinking", tool_calls=[("t", "c2", {})]),
        ],
        on_intermediate_text=fired.append,
    )
    messages = [Message(role="user", contents=["hi"])]

    async def respond() -> None:
        result = client._inner_get_response(messages=messages, stream=stream, options={})
        await (result.get_final_response() if stream else result)

    with internal_side_call_scope():
        await respond()
    assert fired == []

    # Control: outside the scope the callback fires once the response lands.
    await respond()
    assert fired == ["thinking"]
