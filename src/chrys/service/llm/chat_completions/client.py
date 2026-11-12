# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""The Chat Completions clients: one request out, one completion or chunk stream back.

They run over a configured ``AsyncOpenAI`` client and own closing it.
:mod:`.request` builds the request, :mod:`.decode` and :mod:`.stream` read
the answer. OpenAI-compatible endpoints differ only in what a
:class:`ChatCompletionsVariant` describes, so the DeepSeek and GLM clients
are the same client with another variant.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Final, Self, override

import httpx
from openai import BadRequestError

from chrys.foundation.errors import ProviderResponseError
from chrys.foundation.reasoning_origin import ReasoningOrigin
from chrys.foundation.util.once_close import OnceClose
from chrys.kernel import ChatResponse, ChatResponseUpdate, Message, ResponseStream, report_wire_progress
from chrys.kernel.exceptions import ChatClientException
from chrys.service.llm.openai_exceptions import OpenAIContentFilterException
from chrys.service.llm.providers import CHAT_COMPLETIONS_TOKEN_LIMIT_PARAMS
from chrys.service.llm.wire_client import RequestHeaders, WireClient
from chrys.service.profiles.models.options import STREAM_REQUIRES_FINISH_REASON_OPTION

from .decode import decode_completion
from .request import build_request
from .stream import StreamState
from .validation import (
    bounded_body_preview,
    parse_completion,
    raise_invalid_response,
    validate_stream_response,
    zero_event_message,
)

if TYPE_CHECKING:
    from openai import AsyncOpenAI
    from openai.types.chat import ChatCompletionChunk

    from chrys.kernel.compaction import CompactionStrategy, TokenizerProtocol
    from chrys.service.llm.observer import WireCallObserver

logger = logging.getLogger(__name__)

REASONING_PROTOCOL: Final = "chat_completions"
"""The protocol a reasoning stamp names for these clients."""

# How long what follows the end a stream reported is waited for while nothing
# comes that adds to the answer: a connection held open after it must not hold
# the call until it stalls, which would send it again. A refusal that comes
# later, or after the usage, is missed: waiting for one would hold every
# finished answer.
_ENDED_STREAM_WAIT_SECONDS: Final = 5.0
# How the connection may end after that (``RequestError`` includes a body that
# fails to decompress), and data the SDK cannot read (not UTF-8, not JSON):
# neither takes back how the stream said it ends, and each fails before the
# stream state reads anything. An error the service sends (the SDK's
# ``APIError``) and a chunk that cannot be read still fail the reply.
_DISCARDED_AFTER_ENDING: Final = (httpx.RequestError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError)


@dataclass(frozen=True, slots=True)
class ChatCompletionsVariant:
    """What differs between endpoints that speak the Chat Completions protocol."""

    # The wire name of the output-token cap.
    max_output_param: str
    # Reasoning replays only on a request that sends tools or follows a tool
    # interaction, and then every assistant message carries a
    # ``reasoning_content``, empty where it has none.
    reasoning_with_tools: bool
    # Fragments of one message merge as far as their roles allow, and content
    # is a plain string where it can be.
    strict_messages: bool
    # Usage reports DeepSeek's prompt-cache hits.
    reports_prompt_cache_hits: bool


OPENAI = ChatCompletionsVariant(
    max_output_param=CHAT_COMPLETIONS_TOKEN_LIMIT_PARAMS["openai"],
    reasoning_with_tools=False,
    strict_messages=False,
    reports_prompt_cache_hits=False,
)
# DeepSeek takes the legacy ``max_tokens``
# (https://api-docs.deepseek.com/api/create-chat-completion). In thinking
# mode (https://api-docs.deepseek.com/guides/thinking_mode) a request without
# tools ignores historical ``reasoning_content``, while one that sends tools
# must replay all of it, turns without a call included, or get HTTP 400; the
# live API was seen accepting such requests anyway, but the client follows
# the documented contract. Its schema wants a message's same-role fragments
# merged, plain-string content and ``content: ""`` beside ``tool_calls``.
# Usage reports cache reads as ``prompt_cache_hit_tokens``.
DEEPSEEK = ChatCompletionsVariant(
    max_output_param=CHAT_COMPLETIONS_TOKEN_LIMIT_PARAMS["deepseek-openai"],
    reasoning_with_tools=True,
    strict_messages=True,
    reports_prompt_cache_hits=True,
)
# GLM (Zhipu AI / z.ai) documents only the legacy ``max_tokens``. Its
# preserved thinking needs historical ``reasoning_content`` on every
# multi-turn request, turns without a call included, as OpenAI's default
# replay sends it: https://docs.z.ai/guides/llm/glm-4.7 (interleaved and
# preserved thinking) and https://docs.bigmodel.cn/api-reference.
GLM = ChatCompletionsVariant(
    max_output_param=CHAT_COMPLETIONS_TOKEN_LIMIT_PARAMS["glm-openai"],
    reasoning_with_tools=False,
    strict_messages=False,
    reports_prompt_cache_hits=False,
)


class ChatCompletionsClient(WireClient):
    """Chat Completions wire client over a configured ``AsyncOpenAI`` client.

    The tool loop and chat middleware wrap it in the stack the client factory
    builds.
    """

    OTEL_PROVIDER_NAME: ClassVar[str] = "openai"
    # Chat Completions keeps no conversation state (``store`` only keeps a
    # completion for evals), so the option never moves history to the service side.
    FORCES_STATELESS: ClassVar[bool] = True
    INJECTABLE: ClassVar[set[str]] = {"sdk_client"}
    VARIANT: ClassVar[ChatCompletionsVariant] = OPENAI

    def __init__(
        self,
        model: str | None = None,
        *,
        sdk_client: AsyncOpenAI | None = None,
        observer: WireCallObserver | None = None,
        request_headers: RequestHeaders | None = None,
        compaction_strategy: CompactionStrategy | None = None,
        tokenizer: TokenizerProtocol | None = None,
        additional_properties: dict[str, Any] | None = None,
    ) -> None:
        if sdk_client is None:
            raise ValueError(f"{type(self).__name__} requires a pre-configured sdk_client.")
        self.sdk_client = sdk_client
        self._close_sdk = OnceClose(self._close_sdk_client)
        self.model = model or ""
        super().__init__(
            observer=observer,
            request_headers=request_headers,
            compaction_strategy=compaction_strategy,
            tokenizer=tokenizer,
            additional_properties=additional_properties,
        )

    @classmethod
    @override
    def from_sdk_client(
        cls,
        sdk_client: AsyncOpenAI,
        *,
        model: str,
        observer: WireCallObserver | None = None,
        request_headers: RequestHeaders | None = None,
    ) -> Self:
        return cls(model=model, sdk_client=sdk_client, observer=observer, request_headers=request_headers)

    async def aclose(self) -> None:
        """Close the SDK client and its HTTP pool; concurrent callers share one close."""
        await self._close_sdk()

    async def _close_sdk_client(self) -> None:
        await self.sdk_client.close()

    @override
    def service_url(self) -> str:
        if not self.sdk_client:
            return "Unknown"
        return str(self.sdk_client.base_url)

    def reasoning_origin(self) -> ReasoningOrigin | None:
        """The endpoint this client's reasoning comes from, and the only one its ``reasoning_details`` replay to."""
        return ReasoningOrigin.of(REASONING_PROTOCOL, self.sdk_client.base_url)

    def _build_request(self, messages: Sequence[Message], options: Mapping[str, Any]) -> dict[str, Any]:
        request = build_request(
            messages, options, model=self.model, variant=self.VARIANT, origin=self.reasoning_origin()
        )
        self._finalize_request(request)
        return request

    @override
    def _send(
        self,
        *,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse]:
        # Built now, so a request that cannot be built fails the call itself.
        request = self._build_request(messages, options)
        return self._complete(request, options)

    async def _complete(self, request: dict[str, Any], options: Mapping[str, Any]) -> ChatResponse:
        try:
            # The raw wrapper (it adds ``X-Stainless-Raw-Response: true``)
            # keeps the status, headers and body the diagnostics quote.
            raw = await self.sdk_client.chat.completions.with_raw_response.create(stream=False, **request)
            return decode_completion(
                parse_completion(raw), options, variant=self.VARIANT, origin=self.reasoning_origin()
            )
        except ChatClientException, ProviderResponseError:
            # Already the failure to report; wrapping would hide its verdict.
            raise
        except Exception as ex:
            raise _service_error(ex) from ex

    @override
    def _open_stream(
        self,
        *,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> ResponseStream[ChatResponseUpdate, ChatResponse]:
        request = self._build_request(messages, options)
        request["stream_options"] = {"include_usage": True}
        requires_finish_reason = options.get(STREAM_REQUIRES_FINISH_REASON_OPTION) is True

        async def updates() -> AsyncIterable[ChatResponseUpdate]:
            state = StreamState(self.VARIANT, origin=self.reasoning_origin())
            sdk_stream: Any = None
            try:
                # The same raw wrapper as the blocking path, for the same reason.
                raw: Any = await self.sdk_client.chat.completions.with_raw_response.create(stream=True, **request)
                replay = await validate_stream_response(raw)
                sdk_stream = raw.parse()
                chunks = aiter(sdk_stream)
                received = False
                async for chunk in chunks:
                    received = True
                    for update in state.updates_for(chunk):
                        yield update
                    if state.ended:
                        # Also when the chunk the stream ended with carried
                        # usage: a usage chunk after it may repeat or complete
                        # that (Kimi repeats it).
                        async for update in _read_after_ending(chunks, state):
                            yield update
                        break
                if not received:
                    # Valid framing with no event after it (comments only, or
                    # ``[DONE]`` alone) is never a usable completion.
                    raise_invalid_response(zero_event_message(replay))
                # Some gateways end the stream without a finish reason.
                if (calls := state.finish(requires_finish_reason=requires_finish_reason)) is not None:
                    yield calls
            except ProviderResponseError:
                raise
            except Exception as ex:
                # What the stream reported before it broke off or went quiet
                # outranks how it ended: a refusal with calls, or a failure
                # finish reason.
                if (failure := state.failure()) is not None:
                    raise failure from ex
                if isinstance(ex, json.JSONDecodeError):
                    raise_invalid_response(
                        f"Chat Completions API returned invalid stream event JSON ({ex}). "
                        f"Event data: {bounded_body_preview(ex.doc)}"
                    )
                if isinstance(ex, ChatClientException):
                    raise
                raise _service_error(ex) from ex
            finally:
                if sdk_stream is not None:
                    try:
                        await sdk_stream.close()
                    except Exception:
                        logger.debug("Failed to close OpenAI chat-completion stream", exc_info=True)

        return self._build_response_stream(updates(), response_format=options.get("response_format"))


async def _read_after_ending(
    chunks: AsyncIterator[ChatCompletionChunk], state: StreamState
) -> AsyncIterator[ChatResponseUpdate]:
    """The updates of what follows the chunk the stream ended with, up to its usage.

    What follows is read for its usage and for a refusal or failure it still
    reports, until the usage comes or nothing that adds to the answer comes
    within the bound. That chunk arrived: the stall watchdog's idle timer
    restarts, so it fires only after the bound. A stream that settled on a
    failure yields nothing more: the state raises its error once the usage is
    in, and the client when the stream or the bound ends first
    (``StreamState.failure``). A finished answer stands when the stream breaks
    off, sends data that is no chunk (not UTF-8, not JSON) or stays open after
    it; only its usage may be missing. A choice that first shows up here and
    is then unfinished is cut off by that, as by any break.

    A chunk that cannot be read fails the reply here as before the end, on
    purpose: see the comment where it is read.
    """
    report_wire_progress()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _ENDED_STREAM_WAIT_SECONDS
    while True:
        try:
            # One pull at a time: the bound must not span a yield, as each
            # pull may run in a task of its own.
            async with asyncio.timeout_at(deadline):
                chunk = await anext(chunks)
        except StopAsyncIteration:
            # Not a ``None`` default: the SDK yields ``None`` for ``data: null``,
            # which is a chunk that cannot be read, not the end of the stream.
            return
        except _DISCARDED_AFTER_ENDING as error:
            if not state.ended:
                raise
            # Accepted: should the bound run out while the SDK closes the
            # response after an error the service sent just before it, the
            # error is lost and the finished answer stands.
            held_open = isinstance(error, TimeoutError)
            logger.warning(
                "Chat Completions stream %s after its end; usage it would still report is missing",
                "stayed open" if held_open else "failed",
                exc_info=not held_open,
            )
            return
        # Deliberately not skipped when it cannot be read, though the answer
        # already finished: reading a chunk takes in its usage, refusal and
        # calls before its last check can fail, so a skipped chunk could keep a
        # call it began or lose a filter it reports. Only what fails before a
        # chunk is read is safe to drop (``_DISCARDED_AFTER_ENDING``). No
        # service sends one only after the end: a field one leaves out, every
        # chunk lacks, so its stream already failed before it.
        updates = state.updates_for(chunk)
        for update in updates:
            yield update
        contents = [content for update in updates for content in update.contents]
        if state.ended and any(content.type == "usage" for content in contents):
            return
        if any(content.type != "usage" for content in contents):
            deadline = loop.time() + _ENDED_STREAM_WAIT_SECONDS


class DeepSeekChatCompletionsClient(ChatCompletionsClient):
    """DeepSeek's Chat Completions endpoint, thinking mode included."""

    VARIANT: ClassVar[ChatCompletionsVariant] = DEEPSEEK


class GlmChatCompletionsClient(ChatCompletionsClient):
    """GLM's Chat Completions endpoint, with preserved thinking."""

    VARIANT: ClassVar[ChatCompletionsVariant] = GLM


def _service_error(error: Exception) -> ChatClientException:
    if isinstance(error, BadRequestError) and error.code == "content_filter":
        return OpenAIContentFilterException(
            f"Chat Completions request was blocked by a content filter: {error}", inner_exception=error
        )
    return ChatClientException(f"Chat Completions request failed: {error}", inner_exception=error)
