# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MockChatClient — scriptable chat client for replay and testing.

Supports both scripted responses (from a list of canned replies) and
streaming simulation with configurable delays. Useful for:
- Replaying saved sessions without hitting a real LLM
- Testing agent pipelines end-to-end
- UI development without API keys

Usage::

    from chrys.service.llm.mock import MockChatClient, MockResponse

    client = MockChatClient(responses=[
        MockResponse(text="Hello! How can I help?"),
        MockResponse(tool_calls=[("read_file", "call_1", {"path": "foo.py"})]),
        MockResponse(text="Here's the file content."),
    ])
    # Use with Agent like any other client
    agent = Agent(client=client, ...)
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast, overload

from chrys.foundation.trajectory.context import TRAJECTORY_EXCHANGE_KWARG
from chrys.kernel import (
    BaseChatClient,
    ChatMiddlewareLayer,
    FinishReason,
    FinishReasonLiteral,
    ToolLoopLayer,
    in_internal_side_call,
    split_middleware,
)
from chrys.kernel.client import _PreparedRequestObserverClient
from chrys.kernel.types import ChatResponse, ChatResponseUpdate, Content, Message, ResponseStream, UsageDetails
from chrys.service.llm.observer import intermediate_text_signal, open_exchange

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, Awaitable, Callable, Sequence

    from chrys.kernel.compaction import TokenizerProtocol


@dataclass
class MockResponse:
    """A single scripted response the mock client will return.

    Specify either ``text`` for a plain text reply, or ``tool_calls`` for
    function call responses, or both.

    Attributes:
        text: Plain text response content.
        reasoning_text: Hidden reasoning content (``text_reasoning``) emitted
            before the visible text — set it alone to script a reasoning-only
            response.
        tool_calls: List of (function_name, call_id, arguments) tuples.
        finish_reason: Why generation stopped ("stop", "tool_calls", etc.).
        chunk_size: Characters per streaming chunk (0 = single chunk).
        chunk_delay: Seconds between streaming chunks.
        delay: Seconds to wait before returning (non-streaming path).
        model_id: Model ID to report in the response.
        conversation_id: Optional provider-side conversation ID to report.
        usage_details: Optional token usage payload to attach to the response.
    """

    text: str = ""
    reasoning_text: str = ""
    tool_calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    finish_reason: str = ""
    chunk_size: int = 0
    chunk_delay: float = 0.02
    delay: float = 0
    model_id: str = "mock-model"
    conversation_id: str = ""
    usage_details: UsageDetails | None = None


class _ModelCall(_PreparedRequestObserverClient):
    """One model call through the mock's base-client path, beneath its tool loop.

    ``MockChatClient.get_response`` runs the tool loop; the loop's chat layer
    reaches the model through this, so each call takes the path
    ``BaseChatClient.get_response`` gives every wire client.
    """

    def __init__(self, mock: MockChatClient) -> None:
        self._mock = mock

    async def aclose(self) -> None:
        """Nothing to release: the mock owns no provider resources."""

    def get_response(
        self,
        messages: Sequence[Message],
        *,
        stream: bool = False,
        options: Mapping[str, Any] | None = None,
        compaction_strategy: Any = None,
        tokenizer: TokenizerProtocol | None = None,
        client_kwargs: Mapping[str, Any] | None = None,
        request_message_observer: Callable[[Sequence[Message]], None] | None = None,
        **kwargs: Any,
    ) -> Any:
        # Every other keyword (``function_invocation_kwargs`` and whatever chat
        # middleware put in ``context.kwargs``) joins ``client_kwargs`` and
        # overrides it, so it reaches the mock's wire method.
        merged_client_kwargs = dict(client_kwargs or {})
        merged_client_kwargs.update(kwargs)
        return self._mock._call_model(
            messages,
            stream=stream,
            options=options,
            compaction_strategy=compaction_strategy,
            tokenizer=tokenizer,
            client_kwargs=merged_client_kwargs,
            request_message_observer=request_message_observer,
        )


class MockChatClient(BaseChatClient):
    """Scriptable chat client that returns canned responses in order.

    Drives the chrys-owned tool loop internally: ``get_response`` routes
    through ``ToolLoopLayer(ChatMiddlewareLayer(...))`` — the same stack shape
    as the real clients ``chrys.service.llm.clients`` builds — so agent tool
    loops and chat middleware work automatically.

    It reports each request the way a real wire client does (see
    ``chrys.service.llm.observer``), except that it records no response
    timing and, without a stream, fires the intermediate-text callback before
    the exchange's finish marker.

    When all scripted responses are exhausted, returns a default
    "No more scripted responses" message.

    Args:
        responses: Ordered list of responses to return.
        default_model_id: Model ID reported when none specified in response.
        middleware: Chrys chat/function middleware wired into the internal
            stack (chat → ``ChatMiddlewareLayer``, function → ``ToolLoopLayer``).
    """

    def __init__(
        self,
        responses: Sequence[MockResponse] | None = None,
        default_model_id: str = "mock-model",
        middleware: Sequence[Any] | None = None,
        on_intermediate_text: Callable[[str], None] | None = None,
        tool_result_ceiling_tokens: int | None = None,
        connection: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__()
        self._responses: list[MockResponse] = list(responses) if responses else []
        self._connection = connection
        self._default_model_id = default_model_id
        self._call_index: int = 0
        self._call_history: list[tuple[Sequence[Message], Mapping[str, Any]]] = []
        self._on_intermediate_text = on_intermediate_text
        split = split_middleware(list(middleware) if middleware else None)
        self._tool_loop = ToolLoopLayer(
            ChatMiddlewareLayer(_ModelCall(self), middleware=split.chat),
            middleware=split.function,
            tool_result_ceiling_tokens=tool_result_ceiling_tokens,
        )

    @overload
    def get_response(
        self,
        messages: Sequence[Message],
        *,
        stream: Literal[False] = False,
        options: Mapping[str, Any] | None = None,
        compaction_strategy: Any = None,
        tokenizer: TokenizerProtocol | None = None,
        function_invocation_kwargs: Mapping[str, Any] | None = None,
        client_kwargs: Mapping[str, Any] | None = None,
        request_message_observer: Callable[[Sequence[Message]], None] | None = None,
    ) -> Awaitable[ChatResponse[Any]]: ...

    @overload
    def get_response(
        self,
        messages: Sequence[Message],
        *,
        stream: Literal[True],
        options: Mapping[str, Any] | None = None,
        compaction_strategy: Any = None,
        tokenizer: TokenizerProtocol | None = None,
        function_invocation_kwargs: Mapping[str, Any] | None = None,
        client_kwargs: Mapping[str, Any] | None = None,
        request_message_observer: Callable[[Sequence[Message]], None] | None = None,
    ) -> ResponseStream[ChatResponseUpdate, ChatResponse[Any]]: ...

    def get_response(
        self,
        messages: Sequence[Message],
        *,
        stream: bool = False,
        options: Mapping[str, Any] | None = None,
        compaction_strategy: Any = None,
        tokenizer: TokenizerProtocol | None = None,
        function_invocation_kwargs: Mapping[str, Any] | None = None,
        client_kwargs: Mapping[str, Any] | None = None,
        request_message_observer: Callable[[Sequence[Message]], None] | None = None,
    ) -> Awaitable[ChatResponse[Any]] | ResponseStream[ChatResponseUpdate, ChatResponse[Any]]:
        """Route through the internal tool-loop stack (run-contract §3 shapes)."""
        result = self._tool_loop.get_response(
            messages,
            stream=stream,
            options=options,
            compaction_strategy=compaction_strategy,
            tokenizer=tokenizer,
            function_invocation_kwargs=function_invocation_kwargs,
            client_kwargs=client_kwargs,
            request_message_observer=request_message_observer,
        )
        # ``ChatResponse``'s response-model parameter is erased at runtime;
        # the mock forwards the same ToolLoop result for every option shape.
        return cast(
            "Awaitable[ChatResponse[Any]] | ResponseStream[ChatResponseUpdate, ChatResponse[Any]]",
            result,
        )

    def _call_model(self, messages: Sequence[Message], **kwargs: Any) -> Any:
        """One model call, without the tool loop that ``get_response`` adds."""
        return super().get_response(messages, **kwargs)

    @property
    def call_count(self) -> int:
        """Number of get_response calls made."""
        return self._call_index

    @property
    def call_history(self) -> list[tuple[Sequence[Message], Mapping[str, Any]]]:
        """List of (messages, options) from each call, for test assertions."""
        return self._call_history

    def add_response(self, response: MockResponse) -> None:
        """Append a response to the queue."""
        self._responses.append(response)

    def reset(self) -> None:
        """Reset call index and history."""
        self._call_index = 0
        self._call_history.clear()

    def _next_response(self) -> MockResponse:
        """Get the next scripted response, or a default fallback."""
        if self._call_index < len(self._responses):
            resp = self._responses[self._call_index]
        else:
            resp = MockResponse(text="[No more scripted responses]")
        self._call_index += 1
        return resp

    def _build_messages(self, resp: MockResponse) -> list[Message]:
        """Build Message list from a MockResponse."""
        contents: list[Content] = []

        for name, call_id, args in resp.tool_calls:
            contents.append(Content.from_function_call(call_id, name, arguments=args))

        if resp.reasoning_text:
            contents.append(Content.from_text_reasoning(text=resp.reasoning_text))

        if resp.text:
            contents.append(Content.from_text(resp.text))

        return [Message(role="assistant", contents=contents)]

    def _finish_reason(self, resp: MockResponse) -> FinishReasonLiteral | FinishReason:
        if resp.finish_reason:
            return FinishReason(resp.finish_reason)
        return "tool_calls" if resp.tool_calls else "stop"

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse[Any]] | ResponseStream[ChatResponseUpdate, ChatResponse[Any]]:
        # Loop-to-client trajectory handle; dropped as a real wire client does,
        # so mock-driven sessions record the same exchange facts as real ones.
        forwarded_trace = kwargs.pop(TRAJECTORY_EXCHANGE_KWARG, None)
        self._call_history.append((list(messages), dict(options)))

        resp = self._next_response()
        model_id = resp.model_id or self._default_model_id
        finish_reason = self._finish_reason(resp)

        # Internal side calls (e.g. the Phase-4 last-words completer) never
        # join the conversation, so the intermediate-text callback stays
        # silent for them, as it does for a real wire client.
        suppress_intermediate = in_internal_side_call()
        on_text = None if suppress_intermediate else self._on_intermediate_text
        recorder = open_exchange(
            messages,
            options,
            stream=stream,
            forwarded_trace=forwarded_trace,
            internal_side_call=suppress_intermediate,
            connection=self._connection,
        )

        if stream:
            result = self._build_response_stream(self._stream_updates(resp, model_id, finish_reason))
            if recorder is not None:
                recorder.attach_stream(result)
            if on_text is not None:

                def _on_finalized(response: ChatResponse) -> ChatResponse:
                    if (signal := intermediate_text_signal(response)) is not None:
                        on_text(signal)
                    return response

                result.with_result_hook(_on_finalized)
            return result

        if recorder is not None:
            return recorder.wrap_awaitable(self._mock_response(resp, model_id, finish_reason, on_text))
        return self._mock_response(resp, model_id, finish_reason, on_text)

    async def _mock_response(
        self,
        resp: MockResponse,
        model_id: str,
        finish_reason: FinishReasonLiteral | FinishReason,
        on_text: Callable[[str], None] | None,
    ) -> ChatResponse[Any]:
        if resp.delay > 0:
            await asyncio.sleep(resp.delay)
        response = ChatResponse(
            messages=self._build_messages(resp),
            response_id=f"mock-{self._call_index}",
            conversation_id=resp.conversation_id or None,
            model=model_id,
            finish_reason=finish_reason,
        )
        if resp.usage_details is not None:
            response.usage_details = resp.usage_details.copy()
        if on_text is not None and (signal := intermediate_text_signal(response)) is not None:
            on_text(signal)
        return response

    async def _stream_updates(
        self,
        resp: MockResponse,
        model_id: str,
        finish_reason: FinishReasonLiteral | FinishReason,
    ) -> AsyncIterable[ChatResponseUpdate]:
        """Yield streaming chunks for a MockResponse."""
        # Stream tool calls first (as a single chunk)
        if resp.tool_calls:
            tool_contents = [
                Content.from_function_call(call_id, name, arguments=args) for name, call_id, args in resp.tool_calls
            ]
            yield ChatResponseUpdate(
                contents=tool_contents,
                role="assistant",
                conversation_id=resp.conversation_id or None,
                model=model_id,
            )

        # Stream reasoning as a single chunk before the visible text.
        if resp.reasoning_text:
            yield ChatResponseUpdate(
                contents=[Content.from_text_reasoning(text=resp.reasoning_text)],
                role="assistant",
                conversation_id=resp.conversation_id or None,
                model=model_id,
            )

        # Stream text in chunks
        if resp.text:
            text = resp.text
            chunk_size = resp.chunk_size if resp.chunk_size > 0 else len(text)
            for i in range(0, len(text), chunk_size):
                chunk = text[i : i + chunk_size]
                yield ChatResponseUpdate(
                    contents=[Content.from_text(chunk)],
                    role="assistant",
                    conversation_id=resp.conversation_id or None,
                    model=model_id,
                )
                if resp.chunk_delay > 0 and i + chunk_size < len(text):
                    await asyncio.sleep(resp.chunk_delay)

        if resp.usage_details is not None:
            yield ChatResponseUpdate(
                contents=[Content.from_usage(usage_details=resp.usage_details.copy())],
                role="assistant",
                conversation_id=resp.conversation_id or None,
                model=model_id,
            )

        # Final chunk with finish_reason
        yield ChatResponseUpdate(
            contents=[],
            role="assistant",
            conversation_id=resp.conversation_id or None,
            model=model_id,
            finish_reason=finish_reason,
        )
