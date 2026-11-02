# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Chrys-owned chat client base.

The ``BaseChatClient`` subset Chrys consumes:
option validation, response-stream construction, compaction preparation, and
the public ``get_response`` dispatcher. The ``as_agent`` and
embedding surfaces are intentionally not ported.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from abc import ABC, abstractmethod
from collections.abc import AsyncIterable, Awaitable, Callable, Collection, Generator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, TypeGuard, TypeIs, cast, overload

from chrys.foundation.errors import ProviderResponseError
from chrys.foundation.trajectory.context import TRAJECTORY_EXCHANGE_KWARG, ExchangeTrace, side_call_scope
from chrys.foundation.trajectory.envelope import ActorRole

from ._serialization import SerializationMixin, make_json_safe
from ._types import ChatResponse, ChatResponseUpdate, Message, ResponseStream, validate_chat_options

if TYPE_CHECKING:
    from .compaction import CompactionCallContext, TokenizerProtocol


_log = logging.getLogger(__name__)

# A streamed wire pull's stall watchdog also times the work that runs before
# the provider's first byte: compaction and its LAST_WORDS side call.  That
# work reports liveness through this callback, which the watchdog installs in
# the context it times, so the watchdog times idle gaps instead of the whole
# pull.  A timer nested inside (a side call's own read timeout) chains to it.
# Outside a watchdog it is None.
_WIRE_PROGRESS: ContextVar[Callable[[], None] | None] = ContextVar("chrys_wire_progress", default=None)


def report_wire_progress() -> None:
    """Restart the idle timers of the enclosing stall watchdogs, if any are running."""
    callback = _WIRE_PROGRESS.get()
    if callback is not None:
        callback()


@contextmanager
def wire_progress_scope(on_progress: Callable[[], None]) -> Generator[None]:
    """Report progress from the block, and every task it spawns, to *on_progress*.

    A report also reaches the scopes this one is nested in: an inner timer
    never hides progress from the watchdog waiting on its work. Spawned tasks
    copy the context, so they can report after the block exits: every
    callback must tolerate a report once its timer is gone.
    """
    enclosing = _WIRE_PROGRESS.get()

    def _report() -> None:
        on_progress()
        if enclosing is not None:
            enclosing()

    token = _WIRE_PROGRESS.set(_report)
    try:
        yield
    finally:
        _WIRE_PROGRESS.reset(token)


def start_with_wire_progress(awaitable: Awaitable[Any], on_progress: Callable[[], None]) -> asyncio.Future[Any]:
    """Schedule *awaitable* as a task whose context reports progress to *on_progress*.

    The task copies the context at creation, so the callback reaches
    everything the task runs and nothing the caller runs afterwards.
    """
    with wire_progress_scope(on_progress):
        return asyncio.ensure_future(awaitable)


def _report_side_call_update(_update: ChatResponseUpdate) -> None:
    report_wire_progress()


EXHAUSTION_TAIL_KWARG = "exhaustion_tail"
"""``client_kwargs`` key of the tool loop's exhaustion tail call: its chat middleware leaves a response that showed
nothing to the loop's fallback text. A loop-to-middleware flag, never a provider request parameter."""

# Output-cap spellings in provider conflict-priority order. The canonical
# ``max_tokens`` alias wins when more than one is supplied.
OUTPUT_CAP_OPTION_ALIASES: tuple[str, ...] = ("max_tokens", "max_output_tokens", "max_completion_tokens")

# Option keys that must never reach a LAST_WORDS side call.
#
# Continuation markers would chain the throwaway request into service-side
# conversation state: the OpenAI Responses serializer treats every one of
# conversation_id / previous_response_id / conversation as a service-side
# storage marker, the raw provider keys pass through its run options
# untouched, and a continuation_token short-circuits _inner_get_response
# into retrieving an existing background response — the side call's messages
# would be ignored and a live response returned as the note. The side call
# sends a self-contained scoped slice, so it needs no continuation state to be
# useful.
_SIDE_CALL_CONTINUATION_KEYS = ("conversation_id", "previous_response_id", "conversation", "continuation_token")
CONVERSATION_HANDLE_KEYS = ("conversation_id", "previous_response_id", "conversation")
_FORCED_STATELESS_OPTION_KEYS = (*CONVERSATION_HANDLE_KEYS, "continuation_token", "background")
# Output-shaping keys would force the note into schema JSON — non-empty, so
# it would pass the have_note guard. Beyond the first-class response_format,
# provider-native passthroughs survive serialization: OpenAI Responses honors
# a raw ``text`` (carrying ``format``) / ``text_format``, and the Anthropic
# serializer forwards a raw ``output_format`` as a request kwarg.
_SIDE_CALL_OUTPUT_SHAPING_KEYS = ("response_format", "text", "text_format", "output_format", "output_config")
# Execution-mode keys: an inherited Responses ``background=True`` would
# launch a never-polled background response per attempt (its in_progress
# status parses to empty text, so the retry loop would keep spawning
# orphans). The side call must run as a plain foreground request.
_SIDE_CALL_EXECUTION_MODE_KEYS = ("background",)
# Keys scrubbed from a forwarded ``extra_body`` mapping: provider SDKs merge
# ``extra_body`` into the request body AFTER the named parameters, so a
# nested spelling of any of these would silently override the side call's
# top-level sanitization (``store=False``, ``tool_choice="none"``, the
# output cap) on the wire.
_SIDE_CALL_EXTRA_BODY_STRIP_KEYS = frozenset(
    {
        *_SIDE_CALL_CONTINUATION_KEYS,
        *_SIDE_CALL_OUTPUT_SHAPING_KEYS,
        *_SIDE_CALL_EXECUTION_MODE_KEYS,
        *OUTPUT_CAP_OPTION_ALIASES,
        "store",
        "tool_choice",
        # Input-bearing keys: a nested spelling would replace the scoped
        # LAST_WORDS slice or its instruction — or re-enable tools — once
        # the SDK merges ``extra_body`` over the prepared body.  The profile
        # loader strips these from user config, but programmatic options and
        # client kwargs never pass through it.
        "input",
        "messages",
        "prompt",
        "tools",
        "system",
        "instructions",
        "stream",
    }
)


@dataclass(frozen=True, slots=True)
class StorageModeResolution:
    """First-request storage mode plus handle-free request copies."""

    service_side: bool
    handles_present: bool
    options_without_handles: dict[str, Any]
    client_kwargs_without_handles: dict[str, Any]


def _copy_without_conversation_handles(
    payload: Mapping[str, Any] | None,
    *,
    force_stateless: bool = False,
) -> tuple[dict[str, Any], bool]:
    copied = dict(payload or {})
    stripped_keys = _FORCED_STATELESS_OPTION_KEYS if force_stateless else CONVERSATION_HANDLE_KEYS
    handles_present = any(key in copied for key in stripped_keys)
    for key in stripped_keys:
        copied.pop(key, None)
    extra_body = copied.get("extra_body")
    if isinstance(extra_body, Mapping):
        clean_extra_body = dict(extra_body)
        handles_present = handles_present or any(key in clean_extra_body for key in stripped_keys)
        for key in stripped_keys:
            clean_extra_body.pop(key, None)
        copied["extra_body"] = clean_extra_body
    return copied, handles_present


def conversation_handle_value(value: Any) -> str | None:
    """Normalize one conversation-handle spelling to its string id.

    ``conversation`` admits a mapping form carrying the id under ``"id"``;
    the other spellings carry the id directly. Anything else returns None —
    an unrecognized shape is foreign state this layer must not interpret.
    """
    if isinstance(value, Mapping):
        value = value.get("id")
    if isinstance(value, str) and value:
        return value
    return None


def continuation_token_response_id(value: Any) -> str | None:
    """Normalize a continuation token to the response id it would retrieve.

    Provider tokens are opaque TypedDicts; the Responses client's token
    carries the background response's id under ``"response_id"`` and
    short-circuits the request into retrieving that response outright while
    the request messages are ignored. Tokens of any other shape return
    None — they cannot be interpreted at this layer.
    """
    if isinstance(value, Mapping):
        response_id = value.get("response_id")
        if isinstance(response_id, str) and response_id:
            return response_id
    return None


def collect_conversation_handles(payload: Mapping[str, Any] | None) -> list[str]:
    """Collect every normalized conversation handle riding a request mapping.

    Scans each ``CONVERSATION_HANDLE_KEYS`` spelling at the top level and
    nested inside ``extra_body``: provider SDKs merge ``extra_body`` into the
    request body after the named parameters, so a nested spelling reaches the
    wire as continuation state all the same. A top-level
    ``continuation_token`` counts too — its response id is continuation
    state exactly like ``previous_response_id`` (nested extra_body copies
    never reach the retrieve short-circuit, so they don't participate).
    """
    handles: list[str] = []
    if not payload:
        return handles
    extra_body = payload.get("extra_body")
    for source in (payload, extra_body if isinstance(extra_body, Mapping) else None):
        if source is None:
            continue
        for key in CONVERSATION_HANDLE_KEYS:
            handle = conversation_handle_value(source.get(key))
            if handle is not None:
                handles.append(handle)
    token_response_id = continuation_token_response_id(payload.get("continuation_token"))
    if token_response_id is not None:
        handles.append(token_response_id)
    return handles


def strip_invalidated_conversation_handles(
    payload: Mapping[str, Any] | None,
    invalidated: Collection[str],
) -> dict[str, Any]:
    """Copy a request mapping without the handles a session has invalidated.

    The provider-facing reflection view: history providers decide whether
    the service owns a run's history by looking at the request's
    continuation handles, so a handle the wire choke point strips must not
    appear live to them. Removal-only and copy-on-write across the same
    spellings :func:`collect_conversation_handles` scans; live handles pass
    through untouched.
    """
    copied = dict(payload or {})
    if not invalidated:
        return copied
    for key in CONVERSATION_HANDLE_KEYS:
        handle = conversation_handle_value(copied.get(key))
        if handle is not None and handle in invalidated:
            del copied[key]
    token_response_id = continuation_token_response_id(copied.get("continuation_token"))
    if token_response_id is not None and token_response_id in invalidated:
        del copied["continuation_token"]
    extra_body = copied.get("extra_body")
    if isinstance(extra_body, Mapping):
        poisoned_keys = [
            key
            for key in CONVERSATION_HANDLE_KEYS
            if (nested := conversation_handle_value(extra_body.get(key))) is not None and nested in invalidated
        ]
        if poisoned_keys:
            clean_extra_body = dict(extra_body)
            for key in poisoned_keys:
                del clean_extra_body[key]
            copied["extra_body"] = clean_extra_body
    return copied


def resolve_storage_mode_and_handles(
    options: Mapping[str, Any] | None,
    *,
    stores_by_default: bool,
    client_kwargs: Mapping[str, Any] | None = None,
    force_stateless: bool = False,
) -> StorageModeResolution:
    """Resolve effective storage and copy both request mappings without handles.

    The wire truth lives in ``options``: every current client derives
    ``store`` from options alone, with a provider ``extra_body`` value
    overriding the named parameter (a present ``None`` reaches the wire as
    JSON ``null``, selecting the provider's storage default).  Client
    kwargs never reach the request,
    so a kwargs-side ``store`` may only *escalate* the judgment toward
    service-side — misclassifying a stored request as local would license
    in-place re-creates that duplicate hosted work, while the reverse merely
    retries more conservatively.  Conversation handles never decide storage
    mode; the first request's effective ``store`` value does.

    ``force_stateless`` is a client capability veto: it keeps the request in
    local-history/retry mode regardless of every ``store`` spelling and
    removes conversation, continuation, and background state from the
    reflected copies. The provider-facing options remain the caller's own
    view so a raw client can reject unsupported execution modes explicitly.
    """
    effective_options = options or {}
    effective_kwargs = client_kwargs or {}
    option_extra = effective_options.get("extra_body")
    kwargs_extra = effective_kwargs.get("extra_body")
    if isinstance(option_extra, Mapping) and "store" in option_extra:
        # A *present* extra_body key overrides the named parameter even when
        # its value is None: the merged request body then carries
        # ``"store": null`` and the provider applies its storage default —
        # so fall to ``stores_by_default`` rather than the named value.
        explicit_store = option_extra["store"]
    else:
        explicit_store = effective_options.get("store")
    kwargs_escalation = bool(
        (kwargs_extra.get("store") if isinstance(kwargs_extra, Mapping) else None) or effective_kwargs.get("store")
    )
    clean_options, option_handles = _copy_without_conversation_handles(
        effective_options,
        force_stateless=force_stateless,
    )
    clean_kwargs, kwargs_handles = _copy_without_conversation_handles(
        effective_kwargs,
        force_stateless=force_stateless,
    )
    return StorageModeResolution(
        service_side=False
        if force_stateless
        else (bool(explicit_store) if explicit_store is not None else stores_by_default) or kwargs_escalation,
        handles_present=option_handles or kwargs_handles,
        options_without_handles=clean_options,
        client_kwargs_without_handles=clean_kwargs,
    )


def _scrub_side_call_extra_body(payload: dict[str, Any]) -> None:
    """Replace a Mapping ``extra_body`` with a copy minus wire-overriding keys."""
    extra_body = payload.get("extra_body")
    if isinstance(extra_body, Mapping):
        payload["extra_body"] = {
            key: value for key, value in extra_body.items() if key not in _SIDE_CALL_EXTRA_BODY_STRIP_KEYS
        }


def sanitize_side_call_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Copy options without continuation, output-schema, or remote execution controls."""
    clean = dict(options)
    for key in (*_SIDE_CALL_CONTINUATION_KEYS, *_SIDE_CALL_OUTPUT_SHAPING_KEYS, *_SIDE_CALL_EXECUTION_MODE_KEYS):
        clean.pop(key, None)
    _scrub_side_call_extra_body(clean)
    return clean


def _tool_choice_forces_tool_use(tool_choice: Any) -> bool:
    """Return whether a standard or provider-native choice forbids prose-only output."""
    if isinstance(tool_choice, str):
        return tool_choice.lower() in {"required", "any"}
    if not isinstance(tool_choice, Mapping):
        tool_choice = make_json_safe(tool_choice)
    if not isinstance(tool_choice, Mapping):
        return False

    mode = tool_choice.get("mode")
    if isinstance(mode, str):
        return mode.lower() in {"required", "any"}
    choice_type = tool_choice.get("type")
    if isinstance(choice_type, str) and choice_type.lower() in {"required", "any", "function", "tool", "custom"}:
        return True
    return bool(tool_choice.get("required_function_name"))


def _estimate_tool_definition_tokens(options: Mapping[str, Any], tokenizer: TokenizerProtocol | None) -> int:
    """Estimate the live call's tool definitions without touching provider objects."""
    tools = options.get("tools")
    if tools is None or tokenizer is None:
        return 0
    serialized = json.dumps(make_json_safe(tools), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return max(0, tokenizer.count_tokens(serialized))


def _estimate_value_tokens(value: Any, tokenizer: TokenizerProtocol) -> int:
    if isinstance(value, type):
        # A class-valued option (e.g. a Pydantic ``response_format``) wires
        # as its JSON schema; ``make_json_safe`` would instead walk the class
        # ``__dict__`` (core schema, validators, …) and inflate a one-field
        # model to ~100k serialized characters.
        schema_method = getattr(value, "model_json_schema", None)
        try:
            value = schema_method() if callable(schema_method) else str(value)
        except Exception:
            value = str(value)
    text = (
        value
        if isinstance(value, str)
        else json.dumps(
            make_json_safe(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return max(0, tokenizer.count_tokens(text))


# Instruction-bearing plus every provider-native output-shaping spelling —
# the same set the side call scrubs, because the same keys carry wire payload.
_REQUEST_OVERHEAD_OPTION_KEYS = ("instructions", "system", *_SIDE_CALL_OUTPUT_SHAPING_KEYS)


def _estimate_fixed_request_overhead_tokens(
    options: Mapping[str, Any],
    client_kwargs: Mapping[str, Any],
    tokenizer: TokenizerProtocol | None,
    *,
    tool_definition_tokens: int | None = None,
) -> int:
    """Estimate fixed request content injected outside the message list."""
    if tokenizer is None:
        return 0
    total = (
        _estimate_tool_definition_tokens(options, tokenizer)
        if tool_definition_tokens is None
        else tool_definition_tokens
    )
    # ``extra_body`` merges into the request body after the named parameters,
    # so nested spellings of these keys ship on the wire too.
    extra_bodies = [
        body for body in (client_kwargs.get("extra_body"), options.get("extra_body")) if isinstance(body, Mapping)
    ]
    for key in _REQUEST_OVERHEAD_OPTION_KEYS:
        # Providers disagree on options-vs-kwargs precedence for these keys
        # (Anthropic ultimately wires ``options["instructions"]`` over a
        # kwargs-derived ``system``), so admission counts the larger
        # candidate: overestimating only trims output room, undercounting
        # could overflow the context.
        candidates = [client_kwargs.get(key), options.get(key), *(body.get(key) for body in extra_bodies)]
        estimates = [_estimate_value_tokens(value, tokenizer) for value in candidates if value is not None]
        if estimates:
            total += max(estimates)
    return total


def _thinking_budget_tokens(options: Mapping[str, Any], client_kwargs: Mapping[str, Any]) -> int:
    thinking = client_kwargs["thinking"] if "thinking" in client_kwargs else options.get("thinking")
    if not isinstance(thinking, Mapping) or thinking.get("type") != "enabled":
        return 0
    budget = thinking.get("budget_tokens")
    if isinstance(budget, int) and not isinstance(budget, bool) and budget > 0:
        return budget
    return 0


# Headroom for estimate drift since the last calibration: the calibrated
# input estimate can trail the provider's count by a few tokens, and
# input + output past the window is a hard 400.
_ADMISSION_SAFETY_MARGIN_MIN_TOKENS = 256
_ADMISSION_SAFETY_MARGIN_PCT = 1
# The margin never squeezes a cap below this, and never lifts one past the room.
_ADMISSION_MIN_USEFUL_OUTPUT_TOKENS = 1_024


def _admission_safety_margin(max_context_tokens: int) -> int:
    return max(_ADMISSION_SAFETY_MARGIN_MIN_TOKENS, math.ceil(max_context_tokens * _ADMISSION_SAFETY_MARGIN_PCT / 100))


def _clamp_output_cap_for_context(
    options: Mapping[str, Any],
    *,
    strategy: Any,
    request_overhead_tokens: int,
    client_kwargs: Mapping[str, Any],
    provider_min_output_cap_tokens: int = 1,
) -> dict[str, Any]:
    """Clamp present output caps to the calibrated room left in the context.

    The admitted cap keeps a safety margin inside the room while the room is
    wide; when it is narrow, the margin yields down to a minimum useful output
    but the cap still never exceeds the room (only a provider's legal minimum
    can).
    """
    from .compaction import CompactionAdmissionState

    copied = dict(options)
    if not isinstance(strategy, CompactionAdmissionState):
        return copied
    present_aliases = [alias for alias in OUTPUT_CAP_OPTION_ALIASES if alias in copied]
    if not present_aliases:
        _log.debug("Context admission skipped output-cap clamp because no cap alias is present")
        return copied

    overhead = max(strategy.system_overhead_tokens, request_overhead_tokens)
    estimated_input = math.ceil((strategy.last_included_tokens + overhead) * strategy.calibration_ratio)
    room = strategy.max_context_tokens - estimated_input
    margin = _admission_safety_margin(strategy.max_context_tokens)
    guarded_room = min(room, max(room - margin, _ADMISSION_MIN_USEFUL_OUTPUT_TOKENS))
    thinking_budget = _thinking_budget_tokens(copied, client_kwargs)
    min_legal = max(provider_min_output_cap_tokens, thinking_budget + 1 if thinking_budget else 1)
    admitted_cap = max(guarded_room, min_legal)
    clamped: list[tuple[str, int, int]] = []
    for alias in present_aliases:
        value = copied[alias]
        if isinstance(value, int) and not isinstance(value, bool) and value > admitted_cap:
            copied[alias] = admitted_cap
            clamped.append((alias, value, admitted_cap))
    if clamped:
        _log.warning(
            "Clamped model output cap for context admission: estimated_input=%d, max_context_tokens=%d, "
            "room=%d, margin=%d, min_legal=%d, caps=%s",
            estimated_input,
            strategy.max_context_tokens,
            room,
            margin,
            min_legal,
            ", ".join(f"{key}:{old}->{new}" for key, old, new in clamped),
        )
    return copied


class _PreparedRequestObserverClient:
    """Marker for clients that consume a post-prepare request observer.

    ``ChatMiddlewareLayer`` keeps its own final-handler observation for every
    inner client.  Marked clients additionally receive the observer so request
    preparation below that layer (notably compaction summary insertion) can
    generate fresh wrapper views and register their contents immediately before
    the provider call.  The marker prevents the chrys-only callback from
    leaking into arbitrary custom-client kwargs.
    """


def _is_chat_response_stream(
    value: object,
) -> TypeIs[ResponseStream[ChatResponseUpdate, ChatResponse[Any]]]:
    """Narrow the response-stream branch of the client result contract."""
    return isinstance(value, ResponseStream)


def _is_message_list(messages: Sequence[Message]) -> TypeGuard[list[Message]]:
    """Narrow a mutable caller-owned message sequence without copying it."""
    return isinstance(messages, list)


def _wire_message_view(message: Message) -> Message:
    """Per-call wrapper view of an outgoing conversation message.

    Fresh wrapper + fresh contents list, so a client that mutates a received
    message in place (appending contents to a history message) can rewrite
    neither landed history nor caller session-state objects — the structural
    corruption vectors (duplicate ordinals, duplicate objects, corrupted
    stored history). ``additional_properties`` stays THE wrapper's dict on
    purpose: message-level metadata is the stack's sanctioned write-through
    channel (compaction exclusion flags land through it), and metadata writes
    cannot violate the transcript's structural invariants. Content objects
    stay shared like everywhere else.

    The single view builder for both outgoing directions: the tool loop's
    wire views of conversation messages and the final provider-request views
    built below.
    """
    view = copy(message)
    view.contents = list(message.contents)
    return view


def _prepare_provider_request_messages(
    messages: Sequence[Message],
    request_message_observer: Callable[[Sequence[Message]], None] | None = None,
) -> list[Message]:
    """Build final provider views and observe the exact outgoing contents."""
    wire_messages = [_wire_message_view(message) for message in messages]
    if request_message_observer is not None:
        request_message_observer(wire_messages)
    return wire_messages


class _ClientLastWordsCompleter:
    """``LastWordsCompleter`` bound to one ``get_response`` invocation.

    Scoped LAST_WORDS side call: sends the strategy-chosen slice plus one
    appended user instruction through the same client, stream mode,
    options, tool definitions, and client kwargs as the live call.
    ``tool_choice`` is forced to ``"none"`` whenever tools ride along:
    instruction-only suppression proved insufficient (models still attempted
    typed tool calls), and the prefix-cache argument for inheriting the live
    value died with the physically scoped slice — the side call's message
    prefix already diverges from the live call, so the options delta forfeits
    nothing. The no-tools instruction text (composed by the strategy layer)
    stays because ``tool_choice`` cannot stop tool-call markup emitted as
    plain text, with the response guard below as the hard backstop. Goes through
    ``_inner_get_response`` — not ``get_response`` — so the side call never
    re-enters compaction. Returns only the note text; the side-call response
    object never escapes.
    """

    def __init__(
        self,
        client: BaseChatClient,
        *,
        stream: bool,
        options: Mapping[str, Any],
        client_kwargs: Mapping[str, Any],
        kwargs_cap_ceiling: int | None = None,
    ) -> None:
        self._client = client
        self._stream = stream
        self._options = options
        self._client_kwargs = client_kwargs
        self._kwargs_cap_ceiling = kwargs_cap_ceiling

    async def complete_last_words(
        self,
        base_messages: Sequence[Message],
        instruction: str,
        *,
        max_output_tokens: int,
        on_usage: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> str:
        from .compaction import (
            LastWordsToolCallError,
            internal_side_call_scope,
            messages_contain_tool_calls,
            raise_if_context_window_filled,
        )

        # Shallow copy with top-level-key overrides only: nested values (the
        # tools list in particular) stay shared by reference and are never
        # mutated; the live call's options dict is never touched.
        side_options: dict[str, Any] = dict(self._options)
        side_options["max_tokens"] = (
            min(max_output_tokens, self._kwargs_cap_ceiling)
            if self._kwargs_cap_ceiling is not None
            else max_output_tokens
        )
        # Hard-disable tool use for the summarization side call — see the
        # class docstring.  Only when tools are actually present: "none"
        # without tools is meaningless and providers differ on rejecting the
        # combination, so a tool-less side call keeps the live value (or its
        # absence) untouched.
        if side_options.get("tools"):
            side_options["tool_choice"] = "none"
        # The throwaway summarization response must never become a chainable
        # node in service-side storage (OpenAI Responses store mode). Only
        # providers that store by default (or were asked to) get the key —
        # other providers would reject an unknown request parameter.
        if "store" in side_options or type(self._client).STORES_BY_DEFAULT:
            side_options["store"] = False
        side_options = sanitize_side_call_options(side_options)

        side_messages = _prepare_provider_request_messages([*base_messages, Message("user", [instruction])])
        # The scope keeps response instrumentation layered onto
        # ``_inner_get_response`` (intermediate-text publication in
        # particular) silent: even if the model ignores the no-tools
        # instruction and the guard below rejects the response, nothing
        # about it may be published or persisted.  It covers the stream
        # drain too — stream hooks fire during ``get_final_response()``.
        forwarded_kwargs = {
            key: value
            for key, value in self._client_kwargs.items()
            if key
            not in (
                *_SIDE_CALL_CONTINUATION_KEYS,
                *_SIDE_CALL_OUTPUT_SHAPING_KEYS,
                *_SIDE_CALL_EXECUTION_MODE_KEYS,
                "store",
            )
        }
        _scrub_side_call_extra_body(forwarded_kwargs)
        # A streamed note arrives chunk by chunk: every chunk (and each
        # attempt's dispatch) restarts the live call's stall watchdog, which
        # still cancels a side call that goes idle.
        report_wire_progress()
        try:
            with internal_side_call_scope(), side_call_scope(ActorRole.COMPLETER):
                result = self._client._inner_get_response(
                    messages=side_messages,
                    stream=self._stream,
                    options=side_options,
                    **forwarded_kwargs,
                )
                response: ChatResponse[Any]
                if _is_chat_response_stream(result):
                    response = await result.with_transform_hook(_report_side_call_update).get_final_response()
                else:
                    awaited = await result
                    response = (
                        await awaited.with_transform_hook(_report_side_call_update).get_final_response()
                        if isinstance(awaited, ResponseStream)
                        else awaited
                    )
        except ProviderResponseError as err:
            # A response the adapter failed consumed provider tokens too.
            if on_usage is not None and err.usage_details:
                on_usage(err.usage_details)
            raise
        report_wire_progress()
        # Report spend before any acceptance decision: a response the guard
        # rejects below still consumed real provider tokens.
        if on_usage is not None and response.usage_details:
            on_usage(response.usage_details)
        raise_if_context_window_filled(response)
        if messages_contain_tool_calls(response.messages):
            raise LastWordsToolCallError(
                "last-words side call returned tool-call content despite the no-tools instruction"
            )
        # Verbatim, not ``.text``: outer whitespace (leading indentation in
        # particular) is meaningful to the structured-note validator.
        return response.raw_text


class SupportsAclose(Protocol):
    """A chat client stack's one close entry; each layer forwards it inward."""

    async def aclose(self) -> None: ...


class BaseChatClient(SerializationMixin, _PreparedRequestObserverClient, ABC):
    """Abstract base class for loop-free chat clients."""

    OTEL_PROVIDER_NAME: ClassVar[str] = "unknown"
    DEFAULT_EXCLUDE: ClassVar[set[str]] = {
        "additional_properties",
        "compaction_strategy",
        "tokenizer",
    }
    STORES_BY_DEFAULT: ClassVar[bool] = False
    # Provider dialects that cannot persist or resume any request state set
    # this independently from the ordinary storage default.
    FORCES_STATELESS: ClassVar[bool] = False
    MIN_OUTPUT_CAP_TOKENS: ClassVar[int] = 1

    def __init__(
        self,
        *,
        compaction_strategy: Any = None,
        tokenizer: TokenizerProtocol | None = None,
        additional_properties: dict[str, Any] | None = None,
    ) -> None:
        self.additional_properties = additional_properties or {}
        self.compaction_strategy = compaction_strategy
        self.tokenizer = tokenizer
        super().__init__()

    async def aclose(self) -> None:
        """Release provider resources; clients that own none keep this no-op."""

    def to_dict(self, *, exclude: set[str] | None = None, exclude_none: bool = True) -> dict[str, Any]:
        """Serialize the client, lifting additional properties to the root."""
        result = super().to_dict(exclude=exclude, exclude_none=exclude_none)
        if self.additional_properties:
            result.update(self.additional_properties)
        return result

    async def _validate_options(self, options: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and normalize chat options."""
        return await validate_chat_options(dict(options))

    def _finalize_response_updates(
        self,
        updates: Sequence[ChatResponseUpdate],
        *,
        response_format: Any | None = None,
    ) -> ChatResponse[Any]:
        """Finalize streamed updates into one chat response."""
        return ChatResponse.from_updates(updates, output_format_type=response_format)

    def _build_response_stream(
        self,
        stream: AsyncIterable[ChatResponseUpdate] | Awaitable[AsyncIterable[ChatResponseUpdate]],
        *,
        response_format: Any | None = None,
    ) -> ResponseStream[ChatResponseUpdate, ChatResponse[Any]]:
        """Create a response stream with the standard finalizer."""
        return ResponseStream(
            stream,
            finalizer=lambda updates: self._finalize_response_updates(updates, response_format=response_format),
        )

    async def _prepare_messages_for_model_call(
        self,
        messages: Sequence[Message],
        *,
        compaction_strategy: Any = None,
        tokenizer: TokenizerProtocol | None = None,
        context: CompactionCallContext | None = None,
    ) -> list[Message]:
        """Apply annotation/compaction before a provider model call."""
        prepared_messages = list(messages)
        if compaction_strategy is None:
            if tokenizer is None:
                return prepared_messages
            from .compaction import annotate_message_groups

            annotate_message_groups(prepared_messages, tokenizer=tokenizer)
            return prepared_messages

        from .compaction import apply_compaction

        if _is_message_list(messages):
            working_messages: list[Message] = messages
        else:
            working_messages = prepared_messages
        return await apply_compaction(
            working_messages,
            strategy=compaction_strategy,
            tokenizer=tokenizer,
            context=context,
        )

    def _resolve_compaction_overrides(
        self,
        *,
        compaction_strategy: Any = None,
        tokenizer: TokenizerProtocol | None = None,
    ) -> dict[str, Any]:
        current_compaction_strategy = self.compaction_strategy
        current_tokenizer = self.tokenizer
        ret: dict[str, Any] = {}
        if current_compaction_strategy is not None or compaction_strategy is not None:
            ret["compaction_strategy"] = (
                current_compaction_strategy if compaction_strategy is None else compaction_strategy
            )
        if current_tokenizer is not None or tokenizer is not None:
            ret["tokenizer"] = current_tokenizer if tokenizer is None else tokenizer
        return ret

    def _build_compaction_call_context(
        self,
        *,
        stream: bool,
        options: Mapping[str, Any] | None,
        client_kwargs: Mapping[str, Any],
        tokenizer: TokenizerProtocol | None = None,
        kwargs_cap_ceiling: int | None = None,
    ) -> CompactionCallContext:
        """Build the per-call context offered to the compaction strategy.

        Single construction point for the LAST_WORDS completer — every stack
        that routes compaction through ``_prepare_messages_for_model_call``
        (``get_response`` here, which ``MockChatClient`` also calls beneath its
        tool loop) must build the context through this method so gating stays
        uniform.

        Side-call behavior under OpenAI Responses server-side storage still
        needs live smoke validation. Until then, store-mode profiles get no
        completer and Phase 4 keeps the validated reconstruction fallback.
        """
        from .compaction import CompactionCallContext

        effective_options = options or {}
        storage = resolve_storage_mode_and_handles(
            effective_options,
            stores_by_default=type(self).STORES_BY_DEFAULT,
            client_kwargs=client_kwargs,
            force_stateless=type(self).FORCES_STATELESS,
        )
        forced_tool_choice = _tool_choice_forces_tool_use(effective_options.get("tool_choice"))
        completer = (
            None
            if storage.service_side or forced_tool_choice
            else _ClientLastWordsCompleter(
                self,
                stream=stream,
                options=effective_options,
                client_kwargs=client_kwargs,
                kwargs_cap_ceiling=kwargs_cap_ceiling,
            )
        )
        effective_tokenizer = tokenizer or self.tokenizer
        tool_definition_tokens = _estimate_tool_definition_tokens(effective_options, effective_tokenizer)
        exchange_trace = client_kwargs.get(TRAJECTORY_EXCHANGE_KWARG)
        return CompactionCallContext(
            last_words_completer=completer,
            tool_definition_tokens=tool_definition_tokens,
            request_overhead_tokens=_estimate_fixed_request_overhead_tokens(
                effective_options,
                client_kwargs,
                effective_tokenizer,
                tool_definition_tokens=tool_definition_tokens,
            ),
            next_exchange_operation_id=(
                exchange_trace.operation_id if isinstance(exchange_trace, ExchangeTrace) else None
            ),
        )

    def _normalize_wire_inputs(
        self,
        options: Mapping[str, Any] | None,
        client_kwargs: Mapping[str, Any],
    ) -> tuple[Any, dict[str, Any], int | None]:
        """Snapshot admission inputs and fold kwargs output caps into options."""
        if options is None:
            options_snapshot: Any = {}
        elif isinstance(options, Mapping):
            options_snapshot = dict(options)
            tools = options_snapshot.get("tools")
            if isinstance(tools, list | tuple):
                options_snapshot["tools"] = list(tools)
            elif isinstance(tools, Mapping):
                options_snapshot["tools"] = dict(tools)
            thinking = options_snapshot.get("thinking")
            if isinstance(thinking, Mapping):
                options_snapshot["thinking"] = dict(thinking)
        else:
            return options, dict(client_kwargs), None

        sanitized_client_kwargs = dict(client_kwargs)
        kwargs_caps: dict[str, Any] = {}
        for alias in OUTPUT_CAP_OPTION_ALIASES:
            if alias in sanitized_client_kwargs:
                kwargs_caps[alias] = sanitized_client_kwargs.pop(alias)
        if kwargs_caps:
            # A kwargs cap must keep overriding an options cap even when the
            # spellings differ: provider serializers resolve alias conflicts
            # toward canonical ``max_tokens``, so leaving the options spelling
            # in place would invert the historical kwargs-wins precedence.
            for alias in OUTPUT_CAP_OPTION_ALIASES:
                options_snapshot.pop(alias, None)
            options_snapshot.update(kwargs_caps)
        kwargs_cap_ceiling = next(
            (
                value
                for alias in OUTPUT_CAP_OPTION_ALIASES
                if isinstance((value := kwargs_caps.get(alias)), int) and not isinstance(value, bool) and value > 0
            ),
            None,
        )
        return options_snapshot, sanitized_client_kwargs, kwargs_cap_ceiling

    async def _prepare_wire_call(
        self,
        messages: Sequence[Message],
        *,
        call_context: CompactionCallContext,
        options_snapshot: Mapping[str, Any],
        sanitized_client_kwargs: Mapping[str, Any],
        compaction_overrides: Mapping[str, Any],
        request_message_observer: Callable[[Sequence[Message]], None] | None = None,
    ) -> tuple[list[Message], dict[str, Any]]:
        """Apply compaction and context admission for one wire attempt."""
        prepared = await self._prepare_messages_for_model_call(
            messages,
            context=call_context,
            **compaction_overrides,
        )
        wire_messages = _prepare_provider_request_messages(prepared, request_message_observer)
        wire_options = _clamp_output_cap_for_context(
            options_snapshot,
            strategy=compaction_overrides.get("compaction_strategy"),
            request_overhead_tokens=call_context.request_overhead_tokens,
            client_kwargs=sanitized_client_kwargs,
            provider_min_output_cap_tokens=type(self).MIN_OUTPUT_CAP_TOKENS,
        )
        # The provider's time to first byte starts now, not when compaction did.
        report_wire_progress()
        return wire_messages, wire_options

    @abstractmethod
    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse[Any]] | ResponseStream[ChatResponseUpdate, ChatResponse[Any]]:
        """Send prepared messages to the model service."""

    @overload
    def get_response(
        self,
        messages: Sequence[Message],
        *,
        stream: Literal[False] = ...,
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
        """Get a non-streaming or streaming chat response."""
        del function_invocation_kwargs
        compaction_overrides = self._resolve_compaction_overrides(
            compaction_strategy=compaction_strategy,
            tokenizer=tokenizer,
        )
        merged_client_kwargs = dict(client_kwargs) if client_kwargs is not None else {}
        merged_client_kwargs.pop(EXHAUSTION_TAIL_KWARG, None)
        options_snapshot, sanitized_client_kwargs, kwargs_cap_ceiling = self._normalize_wire_inputs(
            options,
            merged_client_kwargs,
        )

        # Non-Mapping options bypass compaction too: they cannot be admitted
        # or clamped, and ``options or {}`` in the context build would
        # silently swallow falsey invalid values instead of letting the
        # provider reject them like every other path does.
        if not compaction_overrides or not isinstance(options_snapshot, Mapping):
            wire_messages = _prepare_provider_request_messages(messages, request_message_observer)
            return self._inner_get_response(
                messages=wire_messages,
                stream=stream,
                options=cast("Mapping[str, Any]", options_snapshot),
                **sanitized_client_kwargs,
            )

        call_context = self._build_compaction_call_context(
            stream=stream,
            options=cast("Mapping[str, Any]", options_snapshot),
            client_kwargs=sanitized_client_kwargs,
            tokenizer=compaction_overrides.get("tokenizer"),
            kwargs_cap_ceiling=kwargs_cap_ceiling,
        )

        if stream:

            async def _get_stream() -> ResponseStream[ChatResponseUpdate, ChatResponse[Any]]:
                prepared_messages, wire_options = await self._prepare_wire_call(
                    messages,
                    call_context=call_context,
                    options_snapshot=cast("Mapping[str, Any]", options_snapshot),
                    sanitized_client_kwargs=sanitized_client_kwargs,
                    compaction_overrides=compaction_overrides,
                    request_message_observer=request_message_observer,
                )
                stream_response = self._inner_get_response(
                    messages=prepared_messages,
                    stream=True,
                    options=wire_options,
                    **sanitized_client_kwargs,
                )
                if _is_chat_response_stream(stream_response):
                    return stream_response
                awaited_stream_response = await stream_response
                if _is_chat_response_stream(awaited_stream_response):
                    return awaited_stream_response
                raise ValueError("Streaming responses must return a ResponseStream.")

            return ResponseStream.from_awaitable(_get_stream())

        async def _get_response() -> ChatResponse[Any]:
            prepared_messages, wire_options = await self._prepare_wire_call(
                messages,
                call_context=call_context,
                options_snapshot=cast("Mapping[str, Any]", options_snapshot),
                sanitized_client_kwargs=sanitized_client_kwargs,
                compaction_overrides=compaction_overrides,
                request_message_observer=request_message_observer,
            )
            response = cast(
                "Awaitable[ChatResponse[Any]]",
                self._inner_get_response(
                    messages=prepared_messages,
                    stream=False,
                    options=wire_options,
                    **sanitized_client_kwargs,
                ),
            )
            return await response

        return _get_response()

    def service_url(self) -> str:
        """Return the provider service URL when a subclass exposes one."""
        return "Unknown"
