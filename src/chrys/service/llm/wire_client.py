# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The base class every provider wire client derives from.

A wire client encodes one request, sends it over its provider SDK and decodes
the response or stream; the tool loop and chat middleware wrap it. The client
factory composes the production stack::

    ToolLoopLayer(              # chrys-owned tool loop
      ChatMiddlewareLayer(      # chrys-owned per-call chat middleware
        <WireClient subclass>)) # telemetry + observer + protocol codec

:class:`WireClient` implements the kernel's ``_inner_get_response`` hook once
for every protocol: it drops the loop's trajectory handle from the call
keywords, lets its :class:`~chrys.service.llm.observer.WireCallObserver`
report the request, and dispatches to the protocol's ``_send`` or
``_open_stream``. :class:`RequestHeaders` holds the Chrys metadata each
protocol stamps on the request it builds.
"""

from __future__ import annotations

from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self, final, override

from chrys.foundation.trajectory.context import TRAJECTORY_EXCHANGE_KWARG
from chrys.foundation.util.chrys_headers import (
    MODEL_ID_HEADER,
    PARENT_SESSION_ID_HEADER,
    SESSION_ID_HEADER,
    X_PARENT_SESSION_ID_HEADER,
    X_SESSION_ID_HEADER,
    is_chrys_managed_header_name,
)
from chrys.foundation.util.header_charset import (
    header_name_charset_error,
    header_value_charset_error,
    model_id_charset_error,
)
from chrys.kernel import BaseChatClient, ChatTelemetryLayer
from chrys.service.llm.route_sessions import llm_parent_session_id, llm_route_session_id

if TYPE_CHECKING:
    from collections.abc import Awaitable, Sequence

    from chrys.kernel.compaction import CompactionStrategy, TokenizerProtocol
    from chrys.kernel.types import ChatResponse, ChatResponseUpdate, Message, ResponseStream
    from chrys.service.llm.observer import WireCallObserver


def _reject_wire_unsafe_request_values(model_id: Any, headers: Mapping[Any, Any] | None) -> None:
    """Reject per-request option values httpx cannot encode into HTTP headers.

    Final wire boundary for values ``create_client`` never sees: resolved
    ``chat_options.extra_headers`` (env templates resolve per run, after
    the client is built) and a per-request ``model`` override, which
    becomes the ``Chrys-Model-Id`` header.  Raising here replaces the
    opaque ``UnicodeEncodeError``/``LocalProtocolError`` the transport
    would otherwise produce with an error naming the offending field —
    without echoing secret header values.
    """
    problems: list[str] = []
    if model_id:
        model_error = model_id_charset_error(str(model_id))
        if model_error:
            problems.append(model_error)
    if isinstance(headers, Mapping):
        for index, (name, value) in enumerate(headers.items(), start=1):
            if not isinstance(name, str):
                problems.append(f"Header name at position {index} must be a string.")
                continue
            name_error = header_name_charset_error(name)
            if name_error:
                problems.append(name_error)
            if not isinstance(value, str):
                problems.append(f"Header {name!r} value must be a string.")
                continue
            value_error = header_value_charset_error(name, value)
            if value_error:
                problems.append(value_error)
    if problems:
        raise ValueError("Chat request options contain values that cannot be sent over HTTP: " + " ".join(problems))


@dataclass(frozen=True, slots=True)
class RequestHeaders:
    """The Chrys metadata headers one client stamps on every request it builds.

    *use_route_session_context* makes the per-invocation route-session
    ContextVars win over the client's own session ids, for clients that
    several sub-agent invocations share.
    """

    session_id: str | None = None
    parent_session_id: str | None = None
    use_route_session_context: bool = False

    def route_session_id(self) -> str | None:
        """The session the request being built belongs to, as the session headers name it."""
        if self.use_route_session_context:
            return llm_route_session_id.get() or self.session_id
        return self.session_id

    def stamp(self, request: dict[str, Any]) -> None:
        """Set the metadata headers on a built request, including the ``X-Session-ID`` alias.

        The request's final ``model`` becomes ``Chrys-Model-Id``. Caller
        headers that collide with a Chrys-managed name are dropped, and the
        remaining ones must be sendable over HTTP.
        """
        model_id = request.get("model")
        session_id = self.route_session_id()
        if self.use_route_session_context:
            parent_session_id = llm_parent_session_id.get() or self.parent_session_id
        else:
            parent_session_id = self.parent_session_id
        if not model_id and not session_id and not parent_session_id:
            # No Chrys metadata to merge — any caller-supplied extra headers
            # still go to the wire as-is, so they still need the charset gate.
            _reject_wire_unsafe_request_values(None, request.get("extra_headers"))
            return

        raw_headers = request.get("extra_headers")
        headers = (
            {k: v for k, v in raw_headers.items() if not is_chrys_managed_header_name(str(k))}
            if isinstance(raw_headers, Mapping)
            else {}
        )
        # Validate after the managed-name filter (a dropped header never
        # reaches the wire) and before Chrys' own metadata joins the dict.
        _reject_wire_unsafe_request_values(model_id, headers)
        if model_id:
            headers[MODEL_ID_HEADER] = str(model_id)
        if session_id:
            headers[X_SESSION_ID_HEADER] = session_id
            headers[SESSION_ID_HEADER] = session_id
        if parent_session_id:
            headers[X_PARENT_SESSION_ID_HEADER] = parent_session_id
            headers[PARENT_SESSION_ID_HEADER] = parent_session_id
        request["extra_headers"] = headers


class WireClient(ChatTelemetryLayer, BaseChatClient):
    """A provider wire client: OTel spans, request reporting and one protocol codec.

    Subclasses implement ``_send`` and ``_open_stream``. Both are plain, not
    async, methods: whatever they check before returning fails the call
    itself, after the observer reported the request as started.
    ``_open_stream`` returns its stream without consuming any of it. Every
    protocol builds its request through one method that ends with
    :meth:`_stamp_request_headers`.

    The observer and request headers are optional: a client built without
    them is the bare codec, which the codec tests drive directly.
    """

    def __init__(
        self,
        *,
        observer: WireCallObserver | None = None,
        request_headers: RequestHeaders | None = None,
        compaction_strategy: CompactionStrategy | None = None,
        tokenizer: TokenizerProtocol | None = None,
        additional_properties: dict[str, Any] | None = None,
    ) -> None:
        self._observer = observer
        self._request_headers = request_headers
        super().__init__(
            compaction_strategy=compaction_strategy,
            tokenizer=tokenizer,
            additional_properties=additional_properties,
        )

    @classmethod
    def from_sdk_client(
        cls,
        sdk_client: Any,
        *,
        model: str,
        observer: WireCallObserver | None = None,
        request_headers: RequestHeaders | None = None,
    ) -> Self:
        """Build this client over a configured provider SDK client."""
        raise NotImplementedError(f"{cls.__name__} does not take a provider SDK client")

    @final
    @override
    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool = False,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse[Any]] | ResponseStream[ChatResponseUpdate, ChatResponse[Any]]:
        # The trajectory exchange trace is a loop-to-client handle, never a
        # provider request parameter: drop it before any kwargs are forwarded.
        forwarded_trace = kwargs.pop(TRAJECTORY_EXCHANGE_KWARG, None)
        from chrys.service.llm.observer import WireCall

        call = (
            self._observer.begin(messages, options, stream=stream, forwarded_trace=forwarded_trace)
            if self._observer is not None
            else WireCall(None, stream=stream)
        )
        try:
            result = self._dispatch(messages, options, stream=stream, kwargs=kwargs)
        except BaseException as exc:
            call.failed(exc)
            raise
        return call.observe(result)

    def _dispatch(
        self,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        *,
        stream: bool,
        kwargs: dict[str, Any],
    ) -> Awaitable[ChatResponse[Any]] | ResponseStream[ChatResponseUpdate, ChatResponse[Any]]:
        if stream:
            return self._open_stream(messages=messages, options=options, **kwargs)
        return self._send(messages=messages, options=options, **kwargs)

    @abstractmethod
    def _send(
        self,
        *,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse[Any]]:
        """Send one request and return the pending response."""

    @abstractmethod
    def _open_stream(
        self,
        *,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> ResponseStream[ChatResponseUpdate, ChatResponse[Any]]:
        """Return the stream of one request; nothing is sent until it is consumed."""

    def _stamp_request_headers(self, request: dict[str, Any]) -> None:
        """Last step of building a request: add this client's Chrys metadata headers.

        ``Chrys-Request-Attempt-Id`` is not added here: SDK retries and redirects
        resend this one request, so the profile HTTP client's request hook
        (``service/llm/request_tracking.py``) stamps a fresh id on every attempt.
        """
        if self._request_headers is not None:
            self._request_headers.stamp(request)
