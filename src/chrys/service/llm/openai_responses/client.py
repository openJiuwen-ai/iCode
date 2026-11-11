# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""The Responses API clients: one request out, one response or event stream back.

They run over a configured ``AsyncOpenAI`` client and own closing it.
:mod:`.request` builds the request, :mod:`.decode` and :mod:`.stream` read
the answer. A continuation token in the options resumes a background
response instead of sending a new request.

Requests to OpenAI's own endpoint route the prompt cache by the session id
the session headers carry, unless the options set ``prompt_cache_key``.

DeepSeek's Responses endpoint maps onto chat and keeps nothing: requests
carry no stored-response handle, a requested ``store`` goes out as false,
and reasoning comes back as plaintext. :data:`DEEPSEEK_RESPONSES` describes
it, and the DeepSeek client differs from the OpenAI one only by that variant
and its storage traits.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Final, Self, cast, override

from openai import AsyncStream, BadRequestError

from chrys.foundation.errors import ProviderResponseError
from chrys.foundation.errors.route import origin_of
from chrys.foundation.reasoning_origin import ReasoningOrigin
from chrys.foundation.util.once_close import OnceClose
from chrys.kernel import ChatResponse, ChatResponseUpdate, Message, ResponseStream
from chrys.kernel.exceptions import ChatClientException
from chrys.service.llm.openai_exceptions import OpenAIContentFilterException
from chrys.service.llm.providers import PROVIDERS
from chrys.service.llm.wire_client import RequestHeaders, WireClient

from .decode import decode_response
from .request import build_request, reject_stateful_options, set_prompt_cache_key
from .stream import StreamState

if TYPE_CHECKING:
    from openai import AsyncOpenAI
    from openai.types.responses.parsed_response import ParsedResponse
    from openai.types.responses.response import Response
    from openai.types.responses.response_stream_event import ResponseStreamEvent
    from pydantic import BaseModel

    from chrys.kernel.compaction import CompactionStrategy, TokenizerProtocol
    from chrys.service.llm.observer import WireCallObserver

    from .decode import OpenAIContinuationToken

_SERVED_MODEL_HEADER = "x-ms-served-model"
_OPENAI_ORIGIN: Final = origin_of(PROVIDERS["openai"].default_base_url)

REASONING_PROTOCOL: Final = "openai_responses"
"""The protocol a reasoning stamp names for these clients."""


@dataclass(frozen=True, slots=True)
class ResponsesVariant:
    """What differs between endpoints that speak the Responses protocol."""

    # The provider hosted-tool contents record.
    hosted_provider: str
    # The endpoint stores nothing: requests carry no stored-response handle,
    # responses yield no conversation id or continuation token, and
    # background runs are refused.
    stateless: bool
    # Reasoning replays as encrypted payloads, which requests ask for;
    # otherwise as plaintext reasoning text.
    encrypted_reasoning: bool
    # Usage reports DeepSeek's prompt-cache hits.
    reports_prompt_cache_hits: bool


OPENAI_RESPONSES = ResponsesVariant(
    hosted_provider="openai", stateless=False, encrypted_reasoning=True, reports_prompt_cache_hits=False
)
DEEPSEEK_RESPONSES = ResponsesVariant(
    hosted_provider="deepseek-openai", stateless=True, encrypted_reasoning=False, reports_prompt_cache_hits=True
)


class ResponsesApiClient(WireClient):
    """Responses API wire client over a configured ``AsyncOpenAI`` client.

    The tool loop and chat middleware wrap it in the stack the client factory
    builds.
    """

    OTEL_PROVIDER_NAME: ClassVar[str] = "openai"
    INJECTABLE: ClassVar[set[str]] = {"sdk_client"}
    STORES_BY_DEFAULT: ClassVar[bool] = True
    MIN_OUTPUT_CAP_TOKENS: ClassVar[int] = 16
    VARIANT: ClassVar[ResponsesVariant] = OPENAI_RESPONSES

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

    @override
    def service_url(self) -> str:
        return str(self.sdk_client.base_url)

    async def aclose(self) -> None:
        """Close the SDK client and its HTTP pool; concurrent callers share one close."""
        await self._close_sdk()

    async def _close_sdk_client(self) -> None:
        await self.sdk_client.close()

    def reasoning_origin(self) -> ReasoningOrigin | None:
        """The endpoint this client's reasoning comes from, and the only one its encrypted reasoning replays to."""
        return ReasoningOrigin.of(REASONING_PROTOCOL, self.sdk_client.base_url)

    def _build_request(self, messages: Sequence[Message], options: Mapping[str, Any]) -> dict[str, Any]:
        request = build_request(
            messages, options, model=self.model, variant=self.VARIANT, origin=self.reasoning_origin()
        )
        set_prompt_cache_key(request, options, session_id=self._prompt_cache_session())
        self._finalize_request(request)
        return request

    def _prompt_cache_session(self) -> str | None:
        """The session id that routes the prompt cache: on OpenAI's own endpoint only."""
        if self._request_headers is None or origin_of(self.sdk_client.base_url) != _OPENAI_ORIGIN:
            return None
        return self._request_headers.route_session_id()

    @override
    def _send(
        self,
        *,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse]:
        if self.VARIANT.stateless:
            reject_stateful_options(options)
        token: OpenAIContinuationToken | None = options.get("continuation_token")
        if token is not None:
            return self._poll(token, options)
        return self._create(messages, options)

    async def _create(self, messages: Sequence[Message], options: Mapping[str, Any]) -> ChatResponse:
        validated = await self._validate_options(options)
        request = self._build_request(messages, validated)
        try:
            if "text_format" in request:
                raw = await self.sdk_client.responses.with_raw_response.parse(stream=False, **request)
            else:
                raw = await self.sdk_client.responses.with_raw_response.create(stream=False, **request)
            # Without streaming the SDK parses a whole response (a parsed
            # one when it was given a model to parse into).
            response = cast("Response | ParsedResponse[BaseModel]", raw.parse())
        except Exception as ex:
            raise _service_error(ex) from ex
        return self._decoded(response, raw, validated)

    async def _poll(self, token: OpenAIContinuationToken, options: Mapping[str, Any]) -> ChatResponse:
        """The current state of a background response."""
        validated = await self._validate_options(options)
        try:
            raw = await self.sdk_client.responses.with_raw_response.retrieve(token["response_id"])
            response = cast("Response", raw.parse())
        except Exception as ex:
            raise _service_error(ex) from ex
        chat_response = self._decoded(response, raw, validated)
        # The tool loop reuses the caller's options across iterations: a
        # token left there would retrieve this finished response again
        # instead of sending the tool results. ``background`` stays, so later
        # requests still run in the background.
        if chat_response.continuation_token is None and isinstance(options, dict):
            options.pop("continuation_token", None)
        return chat_response

    def _decoded(self, response: Any, raw: Any, options: Mapping[str, Any]) -> ChatResponse:
        chat_response = decode_response(response, options, variant=self.VARIANT, origin=self.reasoning_origin())
        # Telemetry wrappers may hide the headers; the response stands without them.
        if (model := served_model(getattr(raw, "headers", None))) is not None:
            chat_response.model = model
        return chat_response

    @override
    def _open_stream(
        self,
        *,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> ResponseStream[ChatResponseUpdate, ChatResponse]:
        if self.VARIANT.stateless:
            reject_stateful_options(options)
        token = options.get("continuation_token")
        # Set once the options are validated; the finalizer only runs after
        # the updates were read, so a structured response can still parse.
        response_format: Any = None

        async def updates() -> AsyncIterable[ChatResponseUpdate]:
            nonlocal response_format
            validated = await self._validate_options(options)
            # Resuming sends no new request.
            request = {} if token is not None else self._build_request(messages, validated)
            response_format = validated.get("response_format")
            state = StreamState(validated, model=self.model, variant=self.VARIANT, origin=self.reasoning_origin())
            try:
                if token is None and "text_format" in request:
                    # The SDK's parsing stream keeps partial structured output
                    # but hides the raw response, so this one path reports the
                    # requested model rather than the served one.
                    async with self.sdk_client.responses.stream(**request) as parsed_events:
                        async for event in parsed_events:
                            for update in state.updates_for(event):
                                yield update
                            # The response ended: a connection that breaks
                            # off or stays open after it must not lose it.
                            if state.ended:
                                break
                    served = None
                else:
                    if token is not None:
                        raw = await self.sdk_client.responses.with_raw_response.retrieve(
                            token["response_id"], stream=True
                        )
                    else:
                        raw = await self.sdk_client.responses.with_raw_response.create(stream=True, **request)
                    served = served_model(getattr(raw, "headers", None))
                    # A streaming call parses to an event stream; the raw
                    # wrapper's generic return type loses that overload.
                    events = cast("AsyncStream[ResponseStreamEvent]", raw.parse())
                    async with events as stream:
                        async for event in stream:
                            for update in state.updates_for(event):
                                if served is not None:
                                    update.model = served
                                yield update
                            if state.ended:
                                break
                if (tail := state.finish()) is not None:
                    if served is not None:
                        tail.model = served
                    yield tail
            except ProviderResponseError:
                # The failure the response itself reported, or a stream that
                # ended before its response did, with its retry decision and
                # the hosted work it showed.
                raise
            except Exception as ex:
                # A refusal with calls outranks how the stream broke off.
                if (failure := state.failure()) is not None:
                    raise failure from ex
                raise _service_error(ex) from ex

        return ResponseStream(
            updates(),
            finalizer=lambda collected: self._finalize_response_updates(collected, response_format=response_format),
        )


class DeepSeekResponsesApiClient(ResponsesApiClient):
    """DeepSeek's Responses endpoint: stateless, with plaintext reasoning."""

    STORES_BY_DEFAULT: ClassVar[bool] = False
    FORCES_STATELESS: ClassVar[bool] = True
    VARIANT: ClassVar[ResponsesVariant] = DEEPSEEK_RESPONSES


def served_model(headers: Any) -> str | None:
    """The model snapshot that ran, from the response headers.

    ``response.model`` repeats the alias the request named. A blank header
    counts as absent.
    """
    if headers is None:
        return None
    value = headers.get(_SERVED_MODEL_HEADER)
    if isinstance(value, str) and (stripped := value.strip()):
        return stripped
    return None


def _service_error(error: Exception) -> ChatClientException:
    if isinstance(error, BadRequestError) and error.code == "content_filter":
        return OpenAIContentFilterException(
            f"Responses API request was blocked by a content filter: {error}", inner_exception=error
        )
    return ChatClientException(f"Responses API request failed: {error}", inner_exception=error)
