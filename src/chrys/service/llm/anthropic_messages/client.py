# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""The Messages API client: one request out, one message or event stream back.

It runs over a configured Anthropic SDK client (direct, Bedrock, Foundry or
Vertex) and owns closing it. :mod:`.request` builds the request,
:mod:`.decode` and :mod:`.stream` read the answer.

When the service refuses replayed thinking as bound to a different
conversation, the client resends the request once without it, unless the
request asked for that refusal; see :meth:`AnthropicMessagesClient._create`.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterable, Awaitable, Collection, Mapping, Sequence
from inspect import isawaitable
from typing import TYPE_CHECKING, Any, ClassVar, Final, Self, override

from anthropic import Omit

from chrys.foundation.errors import is_thinking_binding_rejection
from chrys.foundation.models.history_markers import ANTHROPIC_THINKING_STRIPPED_KEY
from chrys.foundation.reasoning_origin import ReasoningOrigin
from chrys.foundation.util.once_close import OnceClose
from chrys.kernel import ChatResponse, ChatResponseUpdate, Message, ResponseStream, report_wire_progress
from chrys.service.llm.wire_client import RequestHeaders, WireClient

from .decode import decode_message
from .request import BETA_HEADER, BuiltRequest, build_request
from .stream import StreamState

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic, AsyncAnthropicBedrock, AsyncAnthropicFoundry, AsyncAnthropicVertex

    from chrys.kernel import Content
    from chrys.kernel.compaction import CompactionStrategy, TokenizerProtocol
    from chrys.service.llm.observer import WireCallObserver

    type AnthropicSdkClient = AsyncAnthropic | AsyncAnthropicBedrock | AsyncAnthropicFoundry | AsyncAnthropicVertex

logger = logging.getLogger(__name__)

REASONING_PROTOCOL: Final = "anthropic_messages"
"""The protocol a reasoning stamp names for this client."""


class AnthropicMessagesClient(WireClient):
    """Messages API wire client over a configured Anthropic SDK client.

    The tool loop and chat middleware wrap it in the stack the client factory
    builds.
    """

    OTEL_PROVIDER_NAME: ClassVar[str] = "anthropic"
    # The Messages API keeps no conversation state, so a ``store`` option
    # copied into a profile never moves its history to the service side.
    FORCES_STATELESS: ClassVar[bool] = True

    def __init__(
        self,
        model: str | None = None,
        *,
        sdk_client: AnthropicSdkClient | None = None,
        observer: WireCallObserver | None = None,
        request_headers: RequestHeaders | None = None,
        compaction_strategy: CompactionStrategy | None = None,
        tokenizer: TokenizerProtocol | None = None,
        additional_properties: dict[str, Any] | None = None,
    ) -> None:
        if sdk_client is None:
            raise ValueError("AnthropicMessagesClient requires a pre-configured sdk_client.")
        super().__init__(
            observer=observer,
            request_headers=request_headers,
            compaction_strategy=compaction_strategy,
            tokenizer=tokenizer,
            additional_properties=additional_properties,
        )
        self.sdk_client = sdk_client
        self.model = model or ""
        self._close_sdk = OnceClose(self._close_sdk_client)

    @classmethod
    @override
    def from_sdk_client(
        cls,
        sdk_client: AnthropicSdkClient,
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
        """The endpoint this client's thinking comes from, and the only one it replays to."""
        return ReasoningOrigin.of(REASONING_PROTOCOL, self.sdk_client.base_url)

    def _build_request(
        self,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        call_kwargs: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._prepare(messages, options, call_kwargs).request

    def _prepare(
        self,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        call_kwargs: Mapping[str, Any],
        *,
        skip_thinking: Collection[Content] = (),
    ) -> BuiltRequest:
        built = build_request(
            messages,
            options,
            call_kwargs,
            model=self.model,
            base_url=self.sdk_client.base_url,
            default_headers=self.sdk_client.default_headers,
            origin=self.reasoning_origin(),
            skip_thinking=skip_thinking,
        )
        self._finalize_request(built.request)
        return built

    async def _create(
        self,
        built: BuiltRequest,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        call_kwargs: Mapping[str, Any],
        *,
        stream: bool,
    ) -> Any:
        """Send *built*, and once more without its thinking if the service refuses that thinking.

        The service refuses replayed thinking bound to a different
        conversation unless the request lets it drop the block. The request
        is rebuilt without the thinking it replayed and sent again, unless
        it asked for the refusal (``prefix_mismatch_behavior: error``).
        Nothing was answered yet, so nothing is replayed twice. Once the
        resend is accepted, that thinking is marked and never sent again,
        which keeps later requests' history the same as the accepted one's.
        A second failure is raised as it is.
        """
        try:
            return await self._sdk_create(built.request, stream=stream)
        except Exception as exc:
            if not (
                built.thinking and built.policy.mismatch_behavior != "error" and is_thinking_binding_rejection(exc)
            ):
                raise
        resend = self._prepare(messages, options, call_kwargs, skip_thinking=built.thinking)
        # The refusal is the service answering: the resend's wait for its first byte is timed afresh.
        report_wire_progress()
        result = await self._sdk_create(resend.request, stream=stream)
        for content in built.thinking:
            content.additional_properties[ANTHROPIC_THINKING_STRIPPED_KEY] = True
        logger.warning(
            "Anthropic refused replayed thinking as bound to a different conversation; the request was resent "
            "without the %d reasoning item(s) it replayed, which are not sent again.",
            len(built.thinking),
        )
        return result

    def _sdk_create(self, request: dict[str, Any], *, stream: bool) -> Awaitable[Any]:
        return self.sdk_client.beta.messages.create(**self._sdk_request(request), stream=stream)  # type: ignore[misc]

    def _sdk_request(self, request: dict[str, Any]) -> dict[str, Any]:
        """*request* with the SDK client's default ``anthropic-beta`` headers it replaces left out.

        The SDK merges its default headers into a request's case-sensitively,
        so a default spelled otherwise than the request's header, or any one
        when the request sends none, would go out as a second header. An
        ``Omit()`` drops it; it is added only here, as it is no header value
        the stamped request may carry.
        """
        headers = request["extra_headers"]
        omitted = {
            name: Omit()
            for name in self.sdk_client.default_headers
            if name.lower() == BETA_HEADER and name not in headers
        }
        return {**request, "extra_headers": {**headers, **omitted}} if omitted else request

    @override
    def _send(
        self,
        *,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse]:
        built = self._prepare(messages, options, kwargs)

        async def response() -> ChatResponse:
            message = await self._create(built, messages, options, kwargs, stream=False)
            return decode_message(
                message, response_format=options.get("response_format"), origin=self.reasoning_origin()
            )

        return response()

    @override
    def _open_stream(
        self,
        *,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> ResponseStream[ChatResponseUpdate, ChatResponse]:
        built = self._prepare(messages, options, kwargs)

        async def updates() -> AsyncIterable[ChatResponseUpdate]:
            state = StreamState(origin=self.reasoning_origin())
            events: Any = None
            try:
                events = await self._create(built, messages, options, kwargs, stream=True)
                async for event in events:
                    for update in state.updates_for(event):
                        yield update
                    # The message's last event: a gateway may hold the
                    # connection open after it, and the reply must not wait.
                    if event.type == "message_stop":
                        break
                state.finish()
            finally:
                if events is not None:
                    await _close_event_stream(events)

        return self._build_response_stream(updates(), response_format=options.get("response_format"))


async def _close_event_stream(events: Any) -> None:
    """Close the SDK event stream; a failure to close is only logged."""
    try:
        close = getattr(events, "close", None) or getattr(events, "aclose", None)
        if close is not None and isawaitable(closing := close()):
            await closing
    except Exception:
        logger.debug("Failed to close Anthropic message stream", exc_info=True)
