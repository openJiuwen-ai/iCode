# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Instrumented LLM clients that capture intermediate text and request metadata.

When the LLM returns text alongside tool calls (e.g., "Let me read that file"
before calling read_file), the standard client discards that text until the
full tool loop completes.  These subclasses override ``_inner_get_response``
to detect such intermediate text and fire a callback *before* tool execution
begins, so the TUI can render it in real time.

The same subclasses also stamp every provider request with Chrys-managed
headers that depend on final request options, such as ``Chrys-Model-Id``,
and Chrys-owned session attribution.

Each factory returns the Chrys production client stack::

    ToolLoopLayer(                      # chrys-owned tool loop
      ChatMiddlewareLayer(              # chrys-owned per-call chat middleware
        _Instrumented*(                 # mixin + ChatTelemetryLayer + Raw* wire client
          ...)))

The instrumented classes combine loop-free ``Raw*`` wire clients with
``ChatTelemetryLayer`` for GenAI spans. The Raw OpenAI classes inherit
``OTEL_PROVIDER_NAME = "unknown"`` from the base client, so the instrumented
subclasses pin ``"openai"`` explicitly (the Raw Anthropic class already
declares ``"anthropic"``).

Two callback modes (names chosen so async vs sync is visible at a glance):

- ``on_intermediate_text_async`` — awaited inline during **non-streaming**
  responses, before the result is returned to the tool loop.
- ``on_intermediate_text_sync`` — called synchronously from a ``result_hook``
  during **streaming**.  The hook fires after the stream finalizes but
  *before* tool execution.  The callback stores text in an
  ``IntermediateTextBuffer``, released before the next ``ToolCallStart``
  (or, for a call that starts no tool, the next response or the pass end).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol, cast

from chrys.foundation.trajectory.context import (
    TRAJECTORY_EXCHANGE_KWARG,
    ExchangeTrace,
    current_trajectory,
)
from chrys.foundation.trajectory.envelope import ActorKind
from chrys.foundation.trajectory.event_types import ContinuationMode, ExchangeOutcome
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory.usage import normalized_usage, provider_reported_usage, usage_measurements
from chrys.foundation.trajectory_timing import build_trajectory_timing, stamp_trajectory_timing
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
from chrys.service.agent_middleware.events.intermediate_text import intermediate_text_contents
from chrys.service.llm.openai_timestamps import normalize_openai_created_payload
from chrys.service.llm.route_sessions import llm_parent_session_id, llm_route_session_id
from chrys.service.session.message_metadata import stamp_message_response_timing
from chrys.service.trajectory.revisions import record_context_revision

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from anthropic.types.beta import BetaMessageDeltaUsage, BetaUsage

from chrys.kernel import ChatMiddlewareLayer, ChatTelemetryLayer, ToolLoopLayer, in_internal_side_call
from chrys.kernel.instrumentation import _stream_abandoned, _stream_error_of
from chrys.kernel.types import (
    ChatResponse,
    Message,
    ResponseStream,
    UsageDetails,
)

_RESPONSE_BODY_SNIPPET_LIMIT = 2000
_OPENAI_ASSISTANT_ROLE = "assistant"
_OPENAI_ROLE_KEY = "role"
_OPENAI_CONTENT_KEY = "content"
_OPENAI_TOOL_CALLS_KEY = "tool_calls"
_OPENAI_TOOL_CALL_ID_KEY = "tool_call_id"
_OPENAI_REASONING_FIELD_KEYS = ("reasoning_details", "reasoning_content", "reasoning")
_OPENAI_FUNCTION_KEY = "function"
_OPENAI_ARGUMENTS_KEY = "arguments"
_ANTHROPIC_ROLE_KEY = "role"
_ANTHROPIC_CONTENT_KEY = "content"
_ANTHROPIC_ASSISTANT_ROLE = "assistant"
_ANTHROPIC_THINKING_TYPE = "thinking"
_ANTHROPIC_SIGNATURE_KEY = "signature"
_ANTHROPIC_CACHE_CREATION_USAGE_DETAIL_KEY = "anthropic.cache_creation_input_tokens"
_ANTHROPIC_CACHE_READ_USAGE_DETAIL_KEY = "anthropic.cache_read_input_tokens"
_ANTHROPIC_CACHE_USAGE_DETAIL_KEYS = (
    _ANTHROPIC_CACHE_CREATION_USAGE_DETAIL_KEY,
    _ANTHROPIC_CACHE_READ_USAGE_DETAIL_KEY,
)


@dataclass(frozen=True, slots=True)
class _RequestEchoAnchors:
    """Request-scoped strong anchors plus O(1) identity membership indexes."""

    message_metadata: tuple[object, ...]
    contents: tuple[object, ...]
    content_metadata: tuple[object, ...]
    message_metadata_ids: frozenset[int]
    content_ids: frozenset[int]
    content_metadata_ids: frozenset[int]

    @classmethod
    def from_messages(cls, messages: Sequence[Message]) -> _RequestEchoAnchors:
        message_metadata = tuple(message.additional_properties for message in messages)
        contents = tuple(content for message in messages for content in message.contents)
        content_metadata = tuple(content.additional_properties for content in contents)
        return cls(
            message_metadata=message_metadata,
            contents=contents,
            content_metadata=content_metadata,
            message_metadata_ids=frozenset(id(metadata) for metadata in message_metadata),
            content_ids=frozenset(id(content) for content in contents),
            content_metadata_ids=frozenset(id(metadata) for metadata in content_metadata),
        )


def _normalize_anthropic_usage_for_context(usage_details: UsageDetails | None) -> UsageDetails | None:
    """Count Anthropic prompt-cache tokens as input for context accounting.

    Chrys' Anthropic wire client maps native ``cache_creation_input_tokens`` /
    ``cache_read_input_tokens`` attributes into provider-prefixed
    ``UsageDetails`` keys. They still occupy the request context window, so
    Chrys folds them into ``input_token_count`` while preserving the native
    fields for cache-hit display.
    """
    if usage_details is None:
        return None

    cache_input_tokens = sum(int(usage_details.get(key) or 0) for key in _ANTHROPIC_CACHE_USAGE_DETAIL_KEYS)
    if cache_input_tokens <= 0 or "input_token_count" not in usage_details:
        return usage_details

    normalized = cast("UsageDetails", dict(usage_details))
    normalized["input_token_count"] = int(normalized.get("input_token_count") or 0) + cache_input_tokens
    return normalized


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
    if isinstance(headers, MappingABC):
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


def _set_chrys_request_headers(
    options: dict[str, Any],
    *,
    session_id: str | None,
    parent_session_id: str | None = None,
    use_route_session_context: bool = False,
) -> None:
    """Set Chrys-managed metadata, including the ``X-Session-ID`` compatibility alias."""
    model_id = options.get("model")
    effective_session_id = (llm_route_session_id.get() or session_id) if use_route_session_context else session_id
    effective_parent_session_id = (
        (llm_parent_session_id.get() or parent_session_id) if use_route_session_context else parent_session_id
    )
    if not model_id and not effective_session_id and not effective_parent_session_id:
        # No Chrys metadata to merge — any caller-supplied extra headers
        # still go to the wire as-is, so they still need the charset gate.
        _reject_wire_unsafe_request_values(None, options.get("extra_headers"))
        return

    raw_headers = options.get("extra_headers")
    headers = (
        {k: v for k, v in raw_headers.items() if not is_chrys_managed_header_name(str(k))}
        if isinstance(raw_headers, MappingABC)
        else {}
    )
    # Validate after the managed-name filter (a dropped header never
    # reaches the wire) and before Chrys' own metadata joins the dict.
    _reject_wire_unsafe_request_values(model_id, headers)
    if model_id:
        headers[MODEL_ID_HEADER] = str(model_id)
    if effective_session_id:
        headers[X_SESSION_ID_HEADER] = effective_session_id
        headers[SESSION_ID_HEADER] = effective_session_id
    if effective_parent_session_id:
        headers[X_PARENT_SESSION_ID_HEADER] = effective_parent_session_id
        headers[PARENT_SESSION_ID_HEADER] = effective_parent_session_id
    options["extra_headers"] = headers


def _ensure_openai_response_has_choices(response: Any) -> None:
    """Fallback guard for custom/stand-in clients that bypass the raw SDK boundary.

    OpenAI-compatible gateways can return an error envelope (e.g.
    ``{"error": "rate limit"}``) instead of a ``ChatCompletion``.
    The SDK then constructs a ``ChatCompletion`` with ``choices=None``, which
    crashes ``_parse_response_from_openai`` with the cryptic
    ``'NoneType' object is not iterable``. The SDK's ``BaseModel`` uses
    ``extra="allow"``, so the gateway's actual error fields are preserved on
    the parsed object and surface in ``model_dump_json()``.  It raises the
    raw boundary's two-level shape, whose typed inner error marks the failure
    as the model service's answer.
    """
    choices = response.choices
    if isinstance(choices, list):
        return
    from chrys.service.llm._chat_stream_validation import _raise_invalid_chat_completion_response

    try:
        payload = response.model_dump_json()
    except Exception:
        payload = repr(response)
    if len(payload) > _RESPONSE_BODY_SNIPPET_LIMIT:
        suffix = "...[truncated]"
        payload = payload[: _RESPONSE_BODY_SNIPPET_LIMIT - len(suffix)] + suffix
    if choices is None:
        problem = "is missing the required 'choices' array"
    else:
        problem = f"'choices' is {type(choices).__name__}; expected an array"
    _raise_invalid_chat_completion_response(f"OpenAI Chat Completions response {problem}. Parsed payload: {payload}")


def _drop_unsigned_anthropic_thinking_blocks(message: dict[str, Any]) -> dict[str, Any]:
    """Remove assistant Anthropic ``thinking`` blocks that lack a provider signature.

    Anthropic requires assistant ``thinking`` blocks in replayed history to
    carry the provider-issued ``signature``. Some gateways omit that signature
    even when they return the visible thinking text. Re-sending such blocks
    produces HTTP 400 (``signature: Field required``), and converting them to
    normal text would leak reasoning into the visible assistant transcript.

    Signature-only fragments are handled separately by the Anthropic
    serializer: it attaches them to the preceding thinking block or skips
    orphaned signatures. Reasoning with text but no signature can still
    produce an unsigned thinking block; this filter removes that wire-invalid
    output before replay.
    """
    if message.get(_ANTHROPIC_ROLE_KEY) != _ANTHROPIC_ASSISTANT_ROLE:
        return message
    content = message.get(_ANTHROPIC_CONTENT_KEY)
    if not isinstance(content, list):
        return message
    filtered = [
        block
        for block in content
        if not (
            isinstance(block, dict)
            and block.get("type") == _ANTHROPIC_THINKING_TYPE
            and not block.get(_ANTHROPIC_SIGNATURE_KEY)
        )
    ]
    if len(filtered) == len(content):
        return message
    message = dict(message)
    message[_ANTHROPIC_CONTENT_KEY] = filtered
    return message


def _drop_empty_anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove provider messages with no content blocks after Chrys filtering."""
    return [message for message in messages if message.get(_ANTHROPIC_CONTENT_KEY)]


def _canonicalize_openai_tool_call_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalize OpenAI-compatible Chat Completions messages containing tool calls.

    The Chat Completions serializer copied into Chrys can split one
    assistant ``Message`` containing text + ``function_call`` into two
    adjacent wire messages: one text-only assistant message followed by
    one tool-call assistant message.  Strict OpenAI-compatible servers
    such as vLLM can reject the replay, and the rejected pair then
    persists in ``session.json``.

    This pass only operates on the list produced for a *single*
    kernel message, so it does not merge assistant messages from
    different conversation turns.
    """
    canonical: list[dict[str, Any]] = []
    for raw_msg in messages:
        msg = dict(raw_msg)
        _repair_openai_tool_call_arguments(msg)
        if _needs_assistant_tool_call_content(msg):
            msg[_OPENAI_CONTENT_KEY] = ""

        if canonical and _can_merge_assistant_tool_call_messages(canonical[-1], msg):
            _merge_openai_assistant_message(canonical[-1], msg)
            if _needs_assistant_tool_call_content(canonical[-1]):
                canonical[-1][_OPENAI_CONTENT_KEY] = ""
            continue

        if canonical and _must_precede_tool_call_carrier(canonical[-1], msg):
            canonical.insert(len(canonical) - 1, msg)
            continue

        canonical.append(msg)
    return canonical


def _repair_openai_tool_call_arguments(message: dict[str, Any]) -> None:
    """Replace malformed assistant tool-call arguments with valid empty JSON.

    Some OpenAI-compatible backends validate historical assistant
    ``tool_calls[].function.arguments`` on replay. If a previous model
    response emitted an incomplete JSON string such as ``"{"``, the tool
    layer can still return an error result, but the next LLM request is
    rejected unless the historical argument string is valid JSON.
    """
    if message.get(_OPENAI_ROLE_KEY) != _OPENAI_ASSISTANT_ROLE:
        return

    raw_tool_calls = message.get(_OPENAI_TOOL_CALLS_KEY)
    if not isinstance(raw_tool_calls, list):
        return

    repaired_tool_calls: list[Any] = []
    changed = False
    for raw_call in raw_tool_calls:
        if not isinstance(raw_call, dict):
            repaired_tool_calls.append(raw_call)
            continue

        call = dict(raw_call)
        function = call.get(_OPENAI_FUNCTION_KEY)
        if isinstance(function, dict):
            repaired_function = dict(function)
            arguments = repaired_function.get(_OPENAI_ARGUMENTS_KEY)
            normalized_arguments = _normalize_openai_tool_arguments(arguments)
            if normalized_arguments != arguments:
                repaired_function[_OPENAI_ARGUMENTS_KEY] = normalized_arguments
                call[_OPENAI_FUNCTION_KEY] = repaired_function
                changed = True
        repaired_tool_calls.append(call)

    if changed:
        message[_OPENAI_TOOL_CALLS_KEY] = repaired_tool_calls


def _normalize_openai_tool_arguments(arguments: Any) -> str:
    """Return an OpenAI-compatible JSON object string for tool arguments."""
    if isinstance(arguments, MappingABC):
        try:
            return json.dumps(arguments)
        except TypeError, ValueError:
            return "{}"
    if not isinstance(arguments, str):
        return "{}"
    try:
        parsed = json.loads(arguments)
    except json.JSONDecodeError:
        return "{}"
    if isinstance(parsed, dict):
        return arguments
    return "{}"


def _needs_assistant_tool_call_content(message: dict[str, Any]) -> bool:
    """Return True when an assistant tool-call message needs explicit content."""
    return (
        message.get(_OPENAI_ROLE_KEY) == _OPENAI_ASSISTANT_ROLE
        and _OPENAI_TOOL_CALLS_KEY in message
        and _OPENAI_CONTENT_KEY not in message
    )


def _can_merge_assistant_tool_call_messages(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """Return True when adjacent assistant fragments came from one kernel message."""
    if previous.get(_OPENAI_ROLE_KEY) != _OPENAI_ASSISTANT_ROLE:
        return False
    if current.get(_OPENAI_ROLE_KEY) != _OPENAI_ASSISTANT_ROLE:
        return False
    if _OPENAI_TOOL_CALL_ID_KEY in previous or _OPENAI_TOOL_CALL_ID_KEY in current:
        return False
    # A reasoning-bearing assistant message is an aggregate the serializer
    # assembled complete — its reasoning fields sit next to their tool calls
    # exactly once. Merging into or out of it would duplicate or displace
    # them, or absorb excluded non-text parts into a rejected shape.
    if any(key in previous or key in current for key in _OPENAI_REASONING_FIELD_KEYS):
        return False
    # Two non-empty content values of conflicting shapes (str vs list) cannot
    # combine without dropping one side; keep them as separate wire messages.
    # Empty content (None/""/[]) stays neutral so text-less tool-call
    # fragments still merge with multimodal siblings.
    previous_content = previous.get(_OPENAI_CONTENT_KEY)
    current_content = current.get(_OPENAI_CONTENT_KEY)
    if (
        previous_content not in (None, "", [])
        and current_content not in (None, "", [])
        and isinstance(previous_content, str) != isinstance(current_content, str)
    ):
        return False
    return _OPENAI_TOOL_CALLS_KEY in previous or _OPENAI_TOOL_CALLS_KEY in current


def _must_precede_tool_call_carrier(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """Return True when an unmergeable assistant fragment must slot in before the carrier.

    Tool results must directly follow the assistant message carrying their
    ``tool_calls``, so a content-only assistant fragment that could not merge
    into that carrier (e.g. conflicting str/list content shapes) moves ahead
    of the carrier instead of stranding between the carrier and its results —
    the same standalone-ahead-of-aggregate layout the reasoning serializer
    emits.
    """
    return (
        previous.get(_OPENAI_ROLE_KEY) == _OPENAI_ASSISTANT_ROLE
        and _OPENAI_TOOL_CALLS_KEY in previous
        and _OPENAI_TOOL_CALL_ID_KEY not in previous
        and current.get(_OPENAI_ROLE_KEY) == _OPENAI_ASSISTANT_ROLE
        and _OPENAI_TOOL_CALLS_KEY not in current
        and _OPENAI_TOOL_CALL_ID_KEY not in current
    )


def _merge_openai_assistant_message(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Merge a later assistant fragment into an earlier assistant wire message."""
    if source_tool_calls := source.get(_OPENAI_TOOL_CALLS_KEY):
        target.setdefault(_OPENAI_TOOL_CALLS_KEY, []).extend(source_tool_calls)

    source_content = source.get(_OPENAI_CONTENT_KEY)
    if source_content not in (None, "", []):
        target_content = target.get(_OPENAI_CONTENT_KEY)
        if target_content in (None, "", []):
            target[_OPENAI_CONTENT_KEY] = source_content
        elif isinstance(target_content, str) and isinstance(source_content, str):
            target[_OPENAI_CONTENT_KEY] = target_content + source_content
        elif isinstance(target_content, list) and isinstance(source_content, list):
            target[_OPENAI_CONTENT_KEY] = [*target_content, *source_content]

    for key, value in source.items():
        if key in {_OPENAI_ROLE_KEY, _OPENAI_CONTENT_KEY, _OPENAI_TOOL_CALLS_KEY}:
            continue
        if key not in target or target[key] in (None, "", []):
            target[key] = value


def _extract_intermediate_text(response: ChatResponse[Any]) -> str | None:
    """Return the text the intermediate-text callback publishes for *response*, if any."""
    return "".join(content.text or "" for content in intermediate_text_contents(response.messages)) or None


def _count_function_calls(response: ChatResponse[Any]) -> int:
    """Return the number of function_call content items in a response."""
    count = 0
    for msg in response.messages:
        for content in msg.contents:
            if content.type == "function_call" and not content.informational_only:
                count += 1
    return count


def _stamp_response_trajectory_timing(
    response: ChatResponse[Any],
    *,
    started_at: datetime,
    started_monotonic: float,
    echo_anchors: _RequestEchoAnchors,
) -> None:
    """Attach one measured provider-response span without mutating request echoes."""
    finished_at = datetime.now(UTC)
    timing = build_trajectory_timing(
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=int((time.monotonic() - started_monotonic) * 1000),
    )
    for message in response.messages:
        if id(message.additional_properties) not in echo_anchors.message_metadata_ids:
            stamp_message_response_timing(
                message,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=timing["duration_ms"],
            )
        for content in message.contents:
            if (
                content.provider_hosted
                and id(content) not in echo_anchors.content_ids
                and id(content.additional_properties) not in echo_anchors.content_metadata_ids
            ):
                stamp_trajectory_timing(content.additional_properties, timing, overwrite=True)


_OPAQUE_ID_LIMIT = 256


def _bounded_opaque_id(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    cleaned = "".join(ch for ch in value if ch.isprintable())
    return cleaned[:_OPAQUE_ID_LIMIT] or None


def resolve_exchange_trace(forwarded: object, *, internal_side_call: bool) -> ExchangeTrace | None:
    """Pick the exchange trace this wire request reports to.

    The kernel loop hands the conversation's trace down in ``client_kwargs``;
    a side call inherits those kwargs verbatim, so it must never report to
    the parent's exchange — it gets its own only when the caller rebound the
    ambient context to a side-call actor (which also covers side calls that
    bypass the loop entirely and arrive with no forwarded trace).
    """
    if not internal_side_call and isinstance(forwarded, ExchangeTrace):
        return forwarded
    context = current_trajectory()
    if context is None or context.actor.kind != ActorKind.SIDE_CALL:
        return None
    # A side call (the approval judge, a title generator, the last-words
    # completer) reports under its own actor and its own exchange.
    return ExchangeTrace(context.with_exchange(new_analytics_id()))


def exchange_request_facts(options: Mapping[str, Any], *, stream: bool) -> dict[str, Any]:
    """``model.exchange.started`` facts the wire client knows at request time.

    The client's own provider name stays out of this: it is the OTel dialect
    label (every OpenAI-compatible client answers ``openai``), while the
    ``provider`` both exchange markers carry is the model profile's — and the
    request facts are spread last, so reporting it here would overwrite the
    profile's answer on the start marker alone.
    """
    facts: dict[str, Any] = {
        "stream": stream,
        "continuation_mode": ContinuationMode.POLL
        if options.get("continuation_token") is not None
        else ContinuationMode.NONE,
    }
    # Bounded like the response's own model identifier: a per-request override
    # comes from a profile's unrestricted chat options, and one long enough to
    # push the line past the writer's budget would turn this start marker into
    # a gap while the terminal — which carries the profile's model — still
    # landed, leaving a close with nothing it closes.
    request_model = _bounded_opaque_id(options.get("model"))
    if request_model is not None:
        facts["request_model"] = request_model
    return facts


def exchange_response_facts(response: ChatResponse[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """``model.exchange.finished`` facts and measurements taken from a landed response."""
    payload: dict[str, Any] = {}
    response_id = _bounded_opaque_id(response.response_id)
    if response_id is not None:
        payload["response_id"] = response_id
    if response.model:
        payload["response_model"] = str(response.model)[:_OPAQUE_ID_LIMIT]
    if response.finish_reason is not None:
        payload["finish_reason"] = str(response.finish_reason)
    usage = response.usage_details
    reported = provider_reported_usage(usage)
    normalized = normalized_usage(usage)
    usage_payload: dict[str, Any] = {"normalized": normalized}
    if reported is not None:
        usage_payload["provider_reported"] = reported.values
        if reported.omitted:
            # Naming what did not fit keeps a reader from reading the mirror
            # as everything the provider said.
            usage_payload["provider_reported_omitted"] = list(reported.omitted)
    payload["usage"] = usage_payload
    return payload, usage_measurements(normalized)


def _update_has_visible_text(update: Any) -> bool:
    contents = update.contents
    if not contents:
        return False
    return any(content.type == "text" and content.text for content in contents)


class _ExchangeObserver:
    """Drive an :class:`ExchangeTrace` from one wire request's result.

    *owned* says whether this client minted the trace. A trace the loop
    handed down belongs to the loop: this observer reports what it can see
    (the landed response) and leaves every abnormal ending to the layer that
    knows which one it was.
    """

    __slots__ = ("_owned", "_trace")

    def __init__(self, trace: ExchangeTrace, *, owned: bool) -> None:
        self._trace = trace
        self._owned = owned

    def started(self, options: Mapping[str, Any], *, stream: bool) -> None:
        self._trace.started(payload=exchange_request_facts(options, stream=stream))

    def finished(self, response: ChatResponse[Any]) -> None:
        payload, measurements = exchange_response_facts(response)
        self._trace.finished(outcome=ExchangeOutcome.SUCCESS, payload=payload, measurements=measurements)

    def failed(self, exc: BaseException) -> None:
        if not self._owned:
            # The loop closes its own exchanges with the outcome it knows
            # (stalled, interrupted, retryable) while unwinding, and the
            # trace's first-close-wins guard would let this coarser verdict
            # beat it there.
            return
        outcome = ExchangeOutcome.CANCELLED if isinstance(exc, asyncio.CancelledError) else ExchangeOutcome.ERROR
        payload: dict[str, Any] = {}
        if outcome == ExchangeOutcome.ERROR:
            payload["error_code"] = type(exc).__name__
        self._trace.finished(outcome=outcome, payload=payload)

    def attach_stream(self, stream: ResponseStream[Any, ChatResponse[Any]]) -> None:
        trace = self._trace

        def _observe(update: Any) -> Any:
            trace.chunk_observed(visible=_update_has_visible_text(update))
            return update

        stream.with_transform_hook(_observe)
        stream.with_result_hook(self._finish_stream)
        if self._owned:
            # A trace this client opened for itself (a side call below the
            # kernel) has nobody above it to close the exchange, so a stream
            # that dies before its final response has to report its own end.
            stream.with_cleanup_hook(lambda: self._close_dropped_stream(stream))

    def _finish_stream(self, response: ChatResponse[Any]) -> ChatResponse[Any]:
        self.finished(response)
        return response

    def _close_dropped_stream(self, stream: ResponseStream[Any, ChatResponse[Any]]) -> None:
        """Close an exchange whose stream never reached a final response.

        Runs as a cleanup hook, which also fires on the way to a successful
        finalization — where the stream reports neither an error nor an
        abandonment and this does nothing.
        """
        error = _stream_error_of(stream)
        if error is not None:
            self.failed(error)
        elif _stream_abandoned(stream):
            self._trace.abandon()

    def wrap_awaitable(self, awaitable: Awaitable[ChatResponse[Any]]) -> Awaitable[ChatResponse[Any]]:
        async def _observe() -> ChatResponse[Any]:
            try:
                response = await awaitable
            except BaseException as exc:
                self.failed(exc)
                raise
            self.finished(response)
            return response

        return _observe()


if TYPE_CHECKING:

    class _IntermediateTextBase(Protocol):
        def _inner_get_response(
            self,
            *,
            messages: Sequence[Message],
            options: Mapping[str, Any],
            stream: bool = False,
            **kwargs: Any,
        ) -> Awaitable[ChatResponse[Any]] | Any: ...

else:
    _IntermediateTextBase = object


class _IntermediateTextMixin(_IntermediateTextBase):
    """Mixin that wraps ``_inner_get_response`` to detect intermediate text.

    Intermediate text = text content returned alongside function_call content
    in a single LLM response.  When detected, the appropriate callback fires
    *before* the response is returned to the tool loop.

    Each timing envelope covers one wire request. Service-side continuation
    or background polling therefore records the terminal poll latency on the
    persisted response, not the wall time accumulated across earlier polls.
    """

    _on_intermediate_text_async: Callable[[str], Awaitable[None]] | None = None
    _on_intermediate_text_sync: Callable[[str], None] | None = None

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        stream: bool = False,
        **kwargs: Any,
    ) -> Awaitable[ChatResponse[Any]] | Any:
        # The trajectory exchange trace is a loop-to-client handle, never a
        # provider request parameter: pop it before anything forwards kwargs.
        forwarded_trace = kwargs.pop(TRAJECTORY_EXCHANGE_KWARG, None)
        internal_side_call = in_internal_side_call()
        trace = resolve_exchange_trace(forwarded_trace, internal_side_call=internal_side_call)
        # A trace the resolver minted here belongs to this client; one the
        # loop handed down is closed by the loop.
        owns_trace = trace is not None and trace is not forwarded_trace
        observer = _ExchangeObserver(trace, owned=owns_trace) if trace is not None else None
        if trace is not None and not internal_side_call:
            # The exact request this acquisition sends, as a revision of the
            # actor's context chain. Named before the start marker is written,
            # because both markers carry it and only the start one survives a
            # process that dies mid-request.
            trace.set_context_revision(record_context_revision(trace.context, messages))
        if observer is not None:
            observer.started(options, stream=stream)
        if internal_side_call:
            # Internal side calls are consumed below get_response and never
            # join the conversation, so neither timing nor echo anchors are
            # useful for their throwaway response.
            result = self._call_inner(messages=messages, options=options, stream=stream, observer=observer, **kwargs)
            return self._observe_exchange(result, observer, stream=stream)

        started_at = datetime.now(UTC)
        started_monotonic = time.monotonic()
        echo_anchors = _RequestEchoAnchors.from_messages(messages)
        result = self._call_inner(messages=messages, options=options, stream=stream, observer=observer, **kwargs)

        if stream:
            return self._wrap_stream_intermediate(
                self._observe_exchange(result, observer, stream=True),
                started_at=started_at,
                started_monotonic=started_monotonic,
                echo_anchors=echo_anchors,
            )

        callback = self._on_intermediate_text_async
        observed = self._observe_exchange(result, observer, stream=False)

        async def _intercept() -> ChatResponse[Any]:
            response = await observed
            _stamp_response_trajectory_timing(
                response,
                started_at=started_at,
                started_monotonic=started_monotonic,
                echo_anchors=echo_anchors,
            )
            if callback is not None:
                text = _extract_intermediate_text(response)
                if text:
                    await callback(text)
                elif _count_function_calls(response):
                    # Signal a batch boundary even when there's no text, so the
                    # engine's batch_id counter stays aligned with messages.
                    await callback("")
            return response

        return _intercept()

    def _call_inner(
        self,
        *,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        stream: bool,
        observer: _ExchangeObserver | None,
        **kwargs: Any,
    ) -> Any:
        try:
            return super()._inner_get_response(  # type: ignore[misc]
                messages=messages,
                options=options,
                stream=stream,
                **kwargs,
            )
        except BaseException as exc:
            if observer is not None:
                observer.failed(exc)
            raise

    @staticmethod
    def _observe_exchange(result: Any, observer: _ExchangeObserver | None, *, stream: bool) -> Any:
        if observer is None:
            return result
        if stream:
            if isinstance(result, ResponseStream):
                observer.attach_stream(result)
            return result
        return observer.wrap_awaitable(result)

    def _wrap_stream_intermediate(
        self,
        result: Any,
        *,
        started_at: datetime,
        started_monotonic: float,
        echo_anchors: _RequestEchoAnchors,
    ) -> Any:
        """Add a result_hook to the streaming ResponseStream for intermediate text.

        The ``result_hook`` fires during ``get_final_response()`` — after the
        inner stream is fully consumed but *before* the ``ToolLoopLayer``
        inspects the response for tool calls.  This gives the correct event
        ordering:

        1. ``result_hook`` fires → sync callback publishes intermediate text
        2. ``ToolLoopLayer`` detects tool calls
        3. ``ToolEventMiddleware`` publishes ``ToolCallStart``

        The sync callback (``_on_intermediate_text_sync``) stores text in an
        ``IntermediateTextBuffer``, released before the next ``ToolCallStart``
        (or, for a call that starts no tool, the next response or the pass end).

        **Important**: ``_inner_get_response(stream=True)`` returns a
        ``ResponseStream`` directly (not a coroutine), and ``ResponseStream``
        implements ``__await__``.  We must NOT wrap it in an async function
        and ``await`` it — that would eagerly initialize the stream and break
        the lazy resolution chain that higher layers (``get_response``,
        ``ToolLoopLayer``) rely on.  Instead, add the hook directly.
        """
        sync_cb = self._on_intermediate_text_sync

        def _on_finalized(response: ChatResponse[Any]) -> ChatResponse[Any]:
            _stamp_response_trajectory_timing(
                response,
                started_at=started_at,
                started_monotonic=started_monotonic,
                echo_anchors=echo_anchors,
            )
            if sync_cb is not None:
                text = _extract_intermediate_text(response)
                if text:
                    sync_cb(text)
                elif _count_function_calls(response):
                    sync_cb("")  # Batch boundary signal
            return response

        if isinstance(result, ResponseStream):
            result.with_result_hook(_on_finalized)
        return result


def _compose_client_stack(
    chat_client: Any,
    *,
    session_id: str | None = None,
    workspace_cwd: str | None = None,
    max_iterations: int | None,
    max_consecutive_errors: int | None,
    tool_result_ceiling_tokens: int | None = None,
) -> ToolLoopLayer:
    """Wrap an instrumented wire client in the chrys loop + chat-middleware stack.

    ``None`` knobs fall back to :class:`ToolLoopLayer` defaults.
    """
    # AIxCoding telemetry: llm-call piggyback middleware (chrys/aixcoding/telemetry/llm_telemetry.py).
    from chrys.aixcoding.telemetry.llm_telemetry import build_telemetry_middleware

    knobs: dict[str, Any] = {}
    if max_iterations is not None:
        knobs["max_iterations"] = max_iterations
    if max_consecutive_errors is not None:
        knobs["max_consecutive_errors"] = max_consecutive_errors
    if tool_result_ceiling_tokens is not None:
        knobs["tool_result_ceiling_tokens"] = tool_result_ceiling_tokens
    # AIxCoding telemetry: workspace_cwd is the projectName/git basis (source #6).
    return ToolLoopLayer(
        ChatMiddlewareLayer(chat_client, middleware=build_telemetry_middleware(session_id, workspace_cwd)), **knobs
    )


def create_instrumented_openai_client(
    *,
    model_id: str,
    client: Any,
    session_id: str | None = None,
    parent_session_id: str | None = None,
    workspace_cwd: str | None = None,
    use_route_session_context: bool = False,
    on_intermediate_text_async: Callable[[str], Awaitable[None]] | None = None,
    on_intermediate_text_sync: Callable[[str], None] | None = None,
    chat_client_cls: type[Any] | None = None,
    max_iterations: int | None = None,
    max_consecutive_errors: int | None = None,
    tool_result_ceiling_tokens: int | None = None,
) -> Any:
    """Create an OpenAI Chat Completions client stack with intermediate text capture.

    Args:
        model_id: Default model id passed to the provider client.
        session_id: Current session id to stamp into Chrys-managed request headers.
        parent_session_id: Optional parent session id for sub-agent request
            headers.
        use_route_session_context: Whether request preparation should prefer
            per-invocation route-session ContextVars over this client's
            default session ids.
        client: Pre-configured ``AsyncOpenAI`` instance (preserves custom
            timeout/retry settings).
        chat_client_cls: Optional ``RawOpenAIChatCompletionClient`` subclass for
            provider-specific compatibility behavior.
        on_intermediate_text_async: Async callback for non-streaming mode.
        on_intermediate_text_sync: Sync callback for streaming mode.
        max_iterations: Tool-loop iteration cap (``ToolLoopLayer``).
        max_consecutive_errors: Tool-loop consecutive-error cap (``ToolLoopLayer``).
    """
    from chrys.service.llm.openai_chat_completion import RawOpenAIChatCompletionClient

    base_client_cls: Any = chat_client_cls or RawOpenAIChatCompletionClient

    class _InstrumentedOpenAIChatCompletionClient(_IntermediateTextMixin, ChatTelemetryLayer, base_client_cls):  # type: ignore[misc, valid-type]
        """OpenAI client with intermediate text detection + gateway-error guard."""

        OTEL_PROVIDER_NAME = "openai"

        def _prepare_options(self, messages: Sequence[Message], options: Mapping[str, Any]) -> dict[str, Any]:
            prepared = super()._prepare_options(messages, options)  # type: ignore[misc]
            _set_chrys_request_headers(
                prepared,
                session_id=session_id,
                parent_session_id=parent_session_id,
                use_route_session_context=use_route_session_context,
            )
            return prepared

        def _prepare_message_for_openai(self, message: Message, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            prepared = super()._prepare_message_for_openai(message, *args, **kwargs)  # type: ignore[misc]
            return _canonicalize_openai_tool_call_messages(prepared)

        def _parse_response_from_openai(self, response: Any, options: Mapping[str, Any]) -> Any:
            _ensure_openai_response_has_choices(response)
            return super()._parse_response_from_openai(  # type: ignore[misc]
                normalize_openai_created_payload(response),
                options,
            )

        def _parse_response_update_from_openai(self, chunk: Any, *args: Any, **kwargs: Any) -> Any:
            return super()._parse_response_update_from_openai(  # type: ignore[misc]
                normalize_openai_created_payload(chunk),
                *args,
                **kwargs,
            )

        def _parse_usage_from_openai(self, usage: Any) -> Any:
            """Preserve ``cached_tokens=0`` that the base parser drops.

            The base parser uses ``if tokens := ...:`` on
            ``prompt_tokens_details.cached_tokens``, which silently omits the
            field when a provider explicitly reports 0 cache hits. Chrys'
            usage pipeline relies on ``None`` (provider didn't report) being
            distinct from ``0`` (provider reported zero), so we re-inject the
            explicit zero here.
            """
            details = super()._parse_usage_from_openai(usage)  # type: ignore[misc]
            if details is None:
                return details
            pt_details = getattr(usage, "prompt_tokens_details", None)
            if pt_details is not None:
                cached = getattr(pt_details, "cached_tokens", None)
                if cached is not None and "prompt/cached_tokens" not in details:
                    details["prompt/cached_tokens"] = cached  # type: ignore[typeddict-unknown-key]
            return details

    kwargs: dict[str, Any] = {"model": model_id, "async_client": client}
    chat_client = _InstrumentedOpenAIChatCompletionClient(**kwargs)
    chat_client._on_intermediate_text_async = on_intermediate_text_async
    chat_client._on_intermediate_text_sync = on_intermediate_text_sync
    return _compose_client_stack(
        chat_client,
        session_id=session_id,
        workspace_cwd=workspace_cwd,
        max_iterations=max_iterations,
        max_consecutive_errors=max_consecutive_errors,
        tool_result_ceiling_tokens=tool_result_ceiling_tokens,
    )


def create_instrumented_openai_responses_client(
    *,
    model_id: str,
    client: Any,
    session_id: str | None = None,
    parent_session_id: str | None = None,
    workspace_cwd: str | None = None,
    use_route_session_context: bool = False,
    on_intermediate_text_async: Callable[[str], Awaitable[None]] | None = None,
    on_intermediate_text_sync: Callable[[str], None] | None = None,
    chat_client_cls: type[Any] | None = None,
    max_iterations: int | None = None,
    max_consecutive_errors: int | None = None,
    tool_result_ceiling_tokens: int | None = None,
) -> Any:
    """Create an OpenAI-compatible Responses client stack with Chrys instrumentation.

    ``chat_client_cls`` selects an optional provider-specific raw Responses
    subclass while preserving the same middleware, telemetry, callbacks, and
    request-header layers.
    """
    from chrys.service.llm.openai_responses import RawOpenAIChatClient

    base_client_cls: Any = chat_client_cls or RawOpenAIChatClient

    class _InstrumentedOpenAIResponsesClient(_IntermediateTextMixin, ChatTelemetryLayer, base_client_cls):  # type: ignore[misc, valid-type]
        """OpenAI Responses client with intermediate text and Chrys headers."""

        OTEL_PROVIDER_NAME = "openai"

        async def _prepare_options(self, messages: Sequence[Message], options: Mapping[str, Any]) -> dict[str, Any]:
            prepared = await super()._prepare_options(messages, options)
            _set_chrys_request_headers(
                prepared,
                session_id=session_id,
                parent_session_id=parent_session_id,
                use_route_session_context=use_route_session_context,
            )
            return prepared

        def _parse_usage_from_openai(self, usage: Any) -> Any:
            """Preserve Responses ``cached_tokens=0`` that the base parser drops."""
            details = super()._parse_usage_from_openai(usage)
            if details is None:
                return details
            input_details = getattr(usage, "input_tokens_details", None)
            if input_details is not None:
                cached = getattr(input_details, "cached_tokens", None)
                if cached is not None and "openai.cached_input_tokens" not in details:
                    details["openai.cached_input_tokens"] = cached  # type: ignore[typeddict-unknown-key]
            return details

    kwargs: dict[str, Any] = {"model": model_id, "async_client": client}
    chat_client = _InstrumentedOpenAIResponsesClient(**kwargs)
    chat_client._on_intermediate_text_async = on_intermediate_text_async
    chat_client._on_intermediate_text_sync = on_intermediate_text_sync
    return _compose_client_stack(
        chat_client,
        session_id=session_id,
        workspace_cwd=workspace_cwd,
        max_iterations=max_iterations,
        max_consecutive_errors=max_consecutive_errors,
        tool_result_ceiling_tokens=tool_result_ceiling_tokens,
    )


def create_instrumented_anthropic_client(
    *,
    model_id: str,
    anthropic_client: Any,
    session_id: str | None = None,
    parent_session_id: str | None = None,
    workspace_cwd: str | None = None,
    use_route_session_context: bool = False,
    on_intermediate_text_async: Callable[[str], Awaitable[None]] | None = None,
    on_intermediate_text_sync: Callable[[str], None] | None = None,
    max_iterations: int | None = None,
    max_consecutive_errors: int | None = None,
    tool_result_ceiling_tokens: int | None = None,
) -> Any:
    """Create an Anthropic chat client stack with intermediate text capture."""
    from chrys.service.llm.anthropic_chat import RawAnthropicClient

    class _InstrumentedAnthropicClient(_IntermediateTextMixin, ChatTelemetryLayer, RawAnthropicClient):
        """AnthropicClient with intermediate text detection."""

        def _prepare_options(
            self,
            messages: Sequence[Message],
            options: Mapping[str, Any],
            **kwargs: Any,
        ) -> dict[str, Any]:
            prepared = super()._prepare_options(messages, options, **kwargs)
            _set_chrys_request_headers(
                prepared,
                session_id=session_id,
                parent_session_id=parent_session_id,
                use_route_session_context=use_route_session_context,
            )
            return prepared

        def _prepare_messages_for_anthropic(self, messages: Sequence[Message]) -> list[dict[str, Any]]:
            prepared = super()._prepare_messages_for_anthropic(messages)
            return _drop_empty_anthropic_messages(prepared)

        def _prepare_message_for_anthropic(
            self,
            message: Message,
            *,
            hosted_degradations: Mapping[int, str | None] | None = None,
        ) -> dict[str, Any]:
            prepared = super()._prepare_message_for_anthropic(
                message,
                hosted_degradations=hosted_degradations,
            )
            # Drop unsigned assistant thinking blocks: replaying them returns
            # HTTP 400 with "signature: Field required".
            # See _drop_unsigned_anthropic_thinking_blocks.
            return _drop_unsigned_anthropic_thinking_blocks(prepared)

        def _parse_usage_from_anthropic(
            self,
            usage: BetaUsage | BetaMessageDeltaUsage | None,
        ) -> UsageDetails | None:
            details = super()._parse_usage_from_anthropic(usage)
            return _normalize_anthropic_usage_for_context(details)

    kwargs: dict[str, Any] = {"model": model_id, "anthropic_client": anthropic_client}
    client = _InstrumentedAnthropicClient(**kwargs)
    client._on_intermediate_text_async = on_intermediate_text_async
    client._on_intermediate_text_sync = on_intermediate_text_sync
    return _compose_client_stack(
        client,
        session_id=session_id,
        workspace_cwd=workspace_cwd,
        max_iterations=max_iterations,
        max_consecutive_errors=max_consecutive_errors,
        tool_result_ceiling_tokens=tool_result_ceiling_tokens,
    )
