# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Chrys-owned compaction primitives.

The group-annotation keys, grouping engine, incremental (re-)annotation,
token-count annotation, projection, and exclusion flag primitives live here.
``chrys.service.context.compaction``'s four-phase algorithm and the other consumers
(last-words, history restore, TUI rendering) read and write message metadata
exclusively through this module.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import weakref
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, cast, runtime_checkable

from chrys.foundation.errors import ProviderResponseError
from chrys.foundation.hosted_tools import HOSTED_WIRE_REPLAY_PROPERTY_KEYS
from chrys.foundation.models.history_markers import ANTHROPIC_THINKING_STRIPPED_KEY
from chrys.foundation.reasoning_origin import REASONING_ORIGIN_KEY
from chrys.foundation.text.images import inspect_image_dimensions
from chrys.foundation.tool_execution_stamp import EXECUTION_STAMP_KEY

from ._content import Content
from ._types import ChatResponse, Message
from .exchanges import (
    TOOL_CALL_CONTENT_TYPES,
    Exchange,
    LiveAccessor,
    is_result_only_message,
    iter_exchanges,
)
from .exchanges import (
    TOOL_RESULT_CONTENT_TYPES as TOOL_RESULT_CONTENT_TYPES,  # re-exported; chrys.kernel imports it from here
)
from .images import is_image_content, is_image_data_uri, is_image_media_type

type GroupKind = Literal["system", "user", "assistant_text", "tool_call"]

_LIVE_ACCESSOR = LiveAccessor()

GROUP_ANNOTATION_KEY = "_group"
GROUP_ID_KEY = "id"
GROUP_KIND_KEY = "kind"
GROUP_INDEX_KEY = "index"
GROUP_HAS_REASONING_KEY = "has_reasoning"
GROUP_TOKEN_COUNT_KEY = "token_count"
GROUP_TOKEN_ESTIMATOR_VERSION_KEY = "token_estimator_v"
# Version 1 was implicit and serialized non-ASCII text as ``\\uXXXX`` escapes.
# Version 3 excluded provider-hosted wire replay mirrors. Version 4 additionally
# excludes opaque Responses reasoning state, which providers do not tokenize as
# ordinary prompt text. Version 5 counts Chat Completions ``reasoning_details``
# once, as their replayed payload.
TOKEN_ESTIMATOR_VERSION = 5
EXCLUDED_KEY = "_excluded"
EXCLUDE_REASON_KEY = "_exclude_reason"
SUMMARY_OF_MESSAGE_IDS_KEY = "_summary_of_message_ids"
SUMMARY_OF_GROUP_IDS_KEY = "_summary_of_group_ids"
SUMMARIZED_BY_SUMMARY_ID_KEY = "_summarized_by_summary_id"

_IMAGE_BASE_TOKENS = 85
_IMAGE_TILE_TOKENS = 170
_IMAGE_TILE_SIZE = 512
_IMAGE_FALLBACK_TOKENS = _IMAGE_BASE_TOKENS + (_IMAGE_TILE_TOKENS * 4)
_IMAGE_URI_TOKEN_PLACEHOLDER = "[image data omitted for token estimate]"
_TEXT_BUDGETED_ARGUMENT_CONTENT_TYPES = frozenset({"function_call", "search_tool_call", "mcp_server_tool_call"})
_PROJECTION_ATOMICITY_EXCLUDE_REASON = "partial tool-call group"

logger = logging.getLogger(__name__)


@runtime_checkable
class TokenizerProtocol(Protocol):
    """Protocol for token counters used by token-aware compaction strategies."""

    def count_tokens(self, text: str) -> int:
        """Count tokens for a serialized message payload."""
        ...


# The shared call/result content-type sets live in ``chrys.kernel.exchanges``
# (the exchange grammar needs them without importing this module); the import
# above re-exports them unchanged for every existing importer.


class LastWordsToolCallError(RuntimeError):
    """Raised when a last-words side call responds with tool-call content.

    The side call's no-tools instruction should prevent this; when the model
    ignores it, the requested tool calls must not be executed, so the side
    call is treated as a failed attempt and retried by the caller.
    """


# Set True in a response's ``additional_properties`` when the model filled its
# context window after it began answering: the response reads as cut off
# (``length``) and keeps what it wrote. A LAST_WORDS note fails instead
# (:func:`raise_if_context_window_filled`).
CONTEXT_WINDOW_FILLED_KEY: Final = "chrys_context_window_filled"


def raise_if_context_window_filled(response: ChatResponse[Any]) -> None:
    """Fail a LAST_WORDS note that filled the context window, as an overflow that is not retried.

    The note was cut off and the same prompt fills the window again: the
    overflow has the caller send a smaller one. Every LAST_WORDS side call
    checks it, the completer's and the fallback's.
    """
    if response.additional_properties.get(CONTEXT_WINDOW_FILLED_KEY) is True:
        raise ProviderResponseError(
            "model_context_window_exceeded", "The note filled the context window before it ended.", retryable=False
        )


class CompactionProjectionAtomicityError(RuntimeError):
    """Raised before strict compaction commits a partially projected tool-call group."""


# Marks the current task as being inside an internal side call (today: the
# Phase-4 last-words completer) issued below ``get_response``.  Response
# instrumentation that publishes or persists per-response artifacts — e.g.
# the intermediate-text callbacks wired by the engine — must stay silent for
# these requests: the side-call response is consumed internally and never
# becomes part of the conversation.  A ContextVar (not a client attribute)
# so concurrent live calls on the same client are unaffected.
_INTERNAL_SIDE_CALL: ContextVar[bool] = ContextVar("chrys_internal_side_call", default=False)


def in_internal_side_call() -> bool:
    """Whether the current task is inside an internal side call."""
    return _INTERNAL_SIDE_CALL.get()


@contextmanager
def internal_side_call_scope() -> Generator[None]:
    """Mark the enclosed request as an internal side call."""
    token = _INTERNAL_SIDE_CALL.set(True)
    try:
        yield
    finally:
        _INTERNAL_SIDE_CALL.reset(token)


def messages_contain_tool_calls(messages: Sequence[Message]) -> bool:
    """Return whether any message carries tool-call content of any recognized type."""
    return any(content.type in TOOL_CALL_CONTENT_TYPES for message in messages for content in message.contents)


@runtime_checkable
class LastWordsCompleter(Protocol):
    """Client-owned capability: scoped natural-completion side call.

    Sends ``base_messages`` plus one appended user message containing
    ``instruction`` through the same client, route session, options, and
    tool definitions as the live call — with ``tool_choice`` forced to
    ``"none"`` when tools are present — and returns only the assistant
    text. Tool-call markup in plain text is additionally suppressed by the
    instruction itself. Never persists anything into history and never
    mutates the live call's options.

    With ``keep_tool_choice`` the live call's ``tool_choice`` is sent as it
    is: the side call then extends the live request, whose prefix the
    provider has cached, and a changed ``tool_choice`` would forfeit that
    cache. Only for a request ``CompactionCallContext.last_words_extends_request``
    allows; an answer that calls a tool is still refused.
    """

    async def complete_last_words(
        self,
        base_messages: Sequence[Message],
        instruction: str,
        *,
        max_output_tokens: int,
        on_usage: Callable[[Mapping[str, Any]], None] | None = None,
        keep_tool_choice: bool = False,
    ) -> str:
        """Run the side call over ``base_messages`` and return the note text.

        ``on_usage`` receives the raw provider-reported ``usage_details`` of
        every side-call response as soon as it is observed — including
        responses the completer subsequently rejects (a refused response
        still spent real tokens).  It must be cheap and non-raising.
        """
        ...


@dataclass(frozen=True)
class CompactionCallContext:
    """Per-call capabilities a client passes into a compaction strategy.

    Deliberately minimal: no client object, no options object by reference,
    no route/session mutation surface. Strategies see exactly the
    affordances listed here.
    """

    last_words_completer: LastWordsCompleter | None = None
    last_words_extends_request: bool = False
    """Whether the completer may be given the request's own messages and tool choice.

    True only when every tool the request carries runs on the client: a tool
    call in the side call's answer is then data nothing executes, and the
    completer refuses the answer.  A tool the provider runs has already run
    by the time its answer arrives, so such a request never extends.
    """
    tool_definition_tokens: int = 0
    """Deprecated tools-only estimate retained for public API compatibility."""
    request_overhead_tokens: int = 0
    """Estimate of all fixed request content outside the message list."""
    next_exchange_operation_id: str | None = None
    """Exchange operation already reserved for the request being compacted."""


@runtime_checkable
class CompactionAdmissionState(Protocol):
    """Strategy state required to admit an outbound model request safely."""

    max_context_tokens: int

    @property
    def last_included_tokens(self) -> int: ...

    @property
    def system_overhead_tokens(self) -> int: ...

    @property
    def calibration_ratio(self) -> float: ...


@runtime_checkable
class ContextOverflowSink(Protocol):
    """Strategy that takes a provider's verdict that the context window is full.

    Separate from :class:`CompactionAdmissionState` on purpose: that
    protocol's isinstance check gates the output-cap clamp, which a strategy
    without this method must keep.
    """

    def note_context_overflow(self, exc: BaseException | None = None) -> bool:
        """Make the next pass compact whatever the local estimate says.

        *exc* is the provider's rejection, or ``None`` when no error carried
        the verdict.  Returns whether compacting and resending the same
        request can help.
        """
        ...


@runtime_checkable
class CompactionStrategy(Protocol):
    """Protocol for strategies run by ``BaseChatClient`` before model calls.

    .. note:: **Breaking change (Phase-4 LAST_WORDS side call).**  Strategies
       are now invoked as ``strategy(messages, context)`` — two positional
       arguments, where ``context`` is a :class:`CompactionCallContext` or
       ``None``.  Strategies written against the earlier single-parameter
       shape (``__call__(self, messages)``) fail loudly with ``TypeError``
       and must add the ``context`` parameter (accepting and ignoring it is
       valid: it only carries the optional cache-safe last-words completer).
    """

    async def __call__(self, messages: list[Message], context: CompactionCallContext | None = None) -> bool:
        """Mutate messages in place and return whether anything changed."""
        ...


def _has_content_type(message: Message, content_type: str) -> bool:
    """Return whether a message contains the requested content type."""
    return any(content.type == content_type for content in message.contents)


def _has_tool_call(message: Message) -> bool:
    """Return whether a message contains any recognized tool invocation."""
    return any(content.type in TOOL_CALL_CONTENT_TYPES for content in message.contents)


def _has_reasoning(message: Message) -> bool:
    """Return whether a message contains reasoning text."""
    return _has_content_type(message, "text_reasoning")


def _is_tool_call_assistant(message: Message) -> bool:
    """Return whether an assistant message contains a recognized tool invocation."""
    return message.role == "assistant" and _has_tool_call(message)


def _is_reasoning_only_assistant(message: Message) -> bool:
    """Return whether a non-empty assistant message contains only reasoning text."""
    if message.role != "assistant" or not message.contents:
        return False
    return all(content.type == "text_reasoning" for content in message.contents)


def _ensure_message_ids(
    messages: list[Message], *, id_offset: int = 0, reserved_ids: Iterable[str] | None = None
) -> None:
    """Assign positional ids to messages that lack them, avoiding known collisions.

    Ids are POSITIONAL (``msg_{index}``) and collision-checked only against
    ``messages`` plus ``reserved_ids``. A later pass can re-mint an id still
    present in an archived ``compressed_msgs`` block, so ``message_id`` is not
    a session-wide identity. For archived/live audit deduplication, follow the
    contract in the module header of ``service/session/sub_agent_transcript.py``.
    """
    existing_ids: set[str] = set(reserved_ids) if reserved_ids is not None else set()
    existing_ids.update(message.message_id for message in messages if message.message_id)
    for index, message in enumerate(messages):
        if message.message_id:
            continue
        candidate = f"msg_{id_offset + index}"
        if candidate in existing_ids:
            counter = id_offset + len(messages)
            candidate = f"msg_{counter}"
            while candidate in existing_ids:
                counter += 1
                candidate = f"msg_{counter}"
        message.message_id = candidate
        existing_ids.add(candidate)


def _group_id_for(message: Message, group_index: int) -> str:
    """Derive a group id from its message id, falling back to the group index."""
    if message.message_id:
        return f"group_{message.message_id}"
    return f"group_index_{group_index}"


def group_messages(
    messages: list[Message], *, id_offset: int = 0, reserved_ids: Iterable[str] | None = None
) -> list[dict[str, Any]]:
    """Compute group spans and metadata for annotation.

    Args:
        messages: The messages (or a slice of them) to group.

    Keyword Args:
        id_offset: Absolute starting index used when auto-assigning
            ``message_id`` values, so incremental annotation of a list slice
            produces ids that stay unique across the full list.
        reserved_ids: Message ids that already exist outside ``messages`` (for
            example in a preserved prefix). Auto-assigned ids are guaranteed
            not to collide with these, preventing duplicate ids across the
            full list.

    Returns:
        Ordered list of lightweight span dicts with keys:
        ``group_id``, ``kind``, ``start_index``, ``end_index``,
        ``has_reasoning``.

    Exchange boundaries — which output block answers which response run,
    with history markers and non-assistant roles as hard boundaries — come
    from the shared exchange grammar. Spans remain a secondary,
    compaction-owned partition of each exchange (see
    :func:`_exchange_spans`); messages outside every exchange group by
    role as before.
    """
    _ensure_message_ids(messages, id_offset=id_offset, reserved_ids=reserved_ids)
    spans: list[dict[str, Any]] = []

    def append_span(kind: GroupKind, start_index: int, end_index: int, *, has_reasoning: bool) -> None:
        spans.append(
            {
                "group_id": _group_id_for(messages[start_index], len(spans)),
                "kind": kind,
                "start_index": start_index,
                "end_index": end_index,
                "has_reasoning": has_reasoning,
            }
        )

    exchange_by_start = {
        exchange.response_indices[0]: exchange for exchange in iter_exchanges(messages, _LIVE_ACCESSOR)
    }

    i = 0
    while i < len(messages):
        exchange = exchange_by_start.get(i)
        if exchange is not None:
            for kind, start_index, end_index, has_reasoning in _exchange_spans(messages, exchange):
                append_span(kind, start_index, end_index, has_reasoning=has_reasoning)
            i = (exchange.output_indices or exchange.response_indices)[-1] + 1
            continue

        current = messages[i]
        if current.role == "system":
            append_span("system", i, i, has_reasoning=_has_reasoning(current))
            i += 1
            continue
        if current.role == "user":
            append_span("user", i, i, has_reasoning=_has_reasoning(current))
            i += 1
            continue
        if current.role == "tool":
            # Orphan tool output answering no call run still groups as a run.
            k = i + 1
            while k < len(messages) and messages[k].role == "tool":
                k += 1
            append_span("tool_call", i, k - 1, has_reasoning=False)
            i = k
            continue
        append_span("assistant_text", i, i, has_reasoning=_has_reasoning(current))
        i += 1

    return spans


def _exchange_spans(messages: list[Message], exchange: Exchange) -> list[tuple[GroupKind, int, int, bool]]:
    """Secondary partition of one exchange into span tuples.

    A maximal run of call-carrying assistant siblings — reasoning-only
    riders included, covering the OpenAI Responses shape where reasoning
    and function_call contents are co-located as well as reasoning
    prefixes in their own messages — forms one ``tool_call`` span. An
    ordinary text-only sibling keeps its own span and splits call runs.

    Once the exchange has results, calls and results must share ONE group:
    compaction excludes group-wise, and a call grouped apart from its
    result lets a partial exclusion send a result without its call (or a
    call without its answered result) to the provider — a hard protocol
    violation. So with an output block (tool-role or result-only assistant
    messages, hosted MCP included), everything from the first call span
    through the end of the output block fuses into one ``tool_call`` span,
    absorbing interleaved non-call siblings; with only embedded results,
    the call spans fuse through the last of them. Reasoning anywhere in
    the fused range counts toward the group. Text before the first call
    keeps its own span, and an exchange with no results at all keeps its
    split spans.
    """
    response_end = exchange.response_indices[-1]
    segments: list[list[Any]] = []

    i = exchange.response_indices[0]
    while i <= response_end:
        message = messages[i]
        if _is_tool_call_assistant(message) or _is_reasoning_only_assistant(message):
            run_start = i
            saw_call = _is_tool_call_assistant(message)
            i += 1
            while i <= response_end and (
                _is_tool_call_assistant(messages[i]) or _is_reasoning_only_assistant(messages[i])
            ):
                saw_call = saw_call or _is_tool_call_assistant(messages[i])
                i += 1
            if saw_call:
                has_reasoning = any(_has_reasoning(messages[k]) for k in range(run_start, i))
                segments.append(["tool_call", run_start, i - 1, has_reasoning])
            else:
                # Reasoning-only siblings with no call to ride keep per-message spans.
                segments.extend(["assistant_text", k, k, _has_reasoning(messages[k])] for k in range(run_start, i))
            continue
        segments.append(["assistant_text", i, i, _has_reasoning(message)])
        i += 1

    output = exchange.output_indices
    first_call_pos = next((pos for pos, segment in enumerate(segments) if segment[0] == "tool_call"), None)
    if first_call_pos is not None and (output or exchange.embedded_results):
        fuse_end = output[-1] if output else max(segment[2] for segment in segments if segment[0] == "tool_call")
        absorbed = [segment for segment in segments[first_call_pos:] if segment[1] <= fuse_end]
        kept_tail = segments[first_call_pos + len(absorbed) :]
        has_reasoning = any(segment[3] for segment in absorbed) or any(_has_reasoning(messages[k]) for k in output)
        segments[first_call_pos:] = [
            ["tool_call", segments[first_call_pos][1], fuse_end, has_reasoning],
            *kept_tail,
        ]

    return [(kind, start_index, end_index, has_reasoning) for kind, start_index, end_index, has_reasoning in segments]


def _coerce_group_kind(value: object) -> GroupKind | None:
    """Coerce a raw annotation value into a known group kind.

    The membership test intentionally preserves equality-scan semantics, so it
    remains safe for unhashable values.
    """
    if value in ("system", "user", "assistant_text", "tool_call"):
        return cast("GroupKind", value)
    return None


def _read_group_annotation(message: Message) -> dict[str, Any] | None:
    """Read a validated group annotation.

    Return ``None`` unless id, kind, index and has_reasoning are present and
    well-typed, and any token count is an integer.
    """
    raw_annotation = _read_group_annotation_raw(message)
    if raw_annotation is None:
        return None

    group_id = raw_annotation.get(GROUP_ID_KEY)
    group_kind = _coerce_group_kind(raw_annotation.get(GROUP_KIND_KEY))
    group_index = raw_annotation.get(GROUP_INDEX_KEY)
    has_reasoning = raw_annotation.get(GROUP_HAS_REASONING_KEY)
    token_count = raw_annotation.get(GROUP_TOKEN_COUNT_KEY)
    if token_count is not None and not isinstance(token_count, int):
        return None
    if (
        not isinstance(group_id, str)
        or group_kind is None
        or not isinstance(group_index, int)
        or not isinstance(has_reasoning, bool)
    ):
        return None

    return raw_annotation


def _read_group_annotation_raw(message: Message) -> dict[str, Any] | None:
    """Return the live annotation mapping for in-place mutation, or None when absent."""
    annotation = message.additional_properties.get(GROUP_ANNOTATION_KEY)
    if isinstance(annotation, Mapping):
        return cast("dict[str, Any]", annotation)
    return None


def _write_group_annotation(
    message: Message,
    *,
    group_id: str,
    kind: GroupKind,
    index: int,
    has_reasoning: bool,
) -> None:
    """Rewrite core group fields while preserving token counts and unknown fields.

    This retains extensions such as ``SUMMARIZED_BY_SUMMARY_ID_KEY`` during reannotation.
    """
    existing_raw_annotation = _read_group_annotation_raw(message)
    unknown_fields: dict[str, Any] = {}
    token_count: int | None = None
    if existing_raw_annotation is not None:
        raw_token_count = existing_raw_annotation.get(GROUP_TOKEN_COUNT_KEY)
        if isinstance(raw_token_count, int) or raw_token_count is None:
            token_count = raw_token_count
        unknown_fields = {
            key: value
            for key, value in existing_raw_annotation.items()
            if key
            not in {
                GROUP_ID_KEY,
                GROUP_KIND_KEY,
                GROUP_INDEX_KEY,
                GROUP_HAS_REASONING_KEY,
                GROUP_TOKEN_COUNT_KEY,
            }
        }

    annotation = {
        GROUP_ID_KEY: group_id,
        GROUP_KIND_KEY: kind,
        GROUP_INDEX_KEY: index,
        GROUP_HAS_REASONING_KEY: has_reasoning,
        GROUP_TOKEN_COUNT_KEY: token_count,
    }
    annotation.update(unknown_fields)
    message.additional_properties[GROUP_ANNOTATION_KEY] = annotation


def _group_id(message: Message) -> str | None:
    """Read the group id from a valid annotation, or None when unavailable."""
    annotation = _read_group_annotation(message)
    if annotation is None:
        return None
    group_id = annotation.get(GROUP_ID_KEY)
    return group_id if isinstance(group_id, str) else None


def _group_kind(message: Message) -> GroupKind | None:
    """Read the group kind from a valid annotation, or None when unavailable."""
    annotation = _read_group_annotation(message)
    if annotation is None:
        return None
    return _coerce_group_kind(annotation.get(GROUP_KIND_KEY))


def _group_index(message: Message) -> int | None:
    """Read the group index from a valid annotation, or None when unavailable."""
    annotation = _read_group_annotation(message)
    if annotation is None:
        return None
    group_index = annotation.get(GROUP_INDEX_KEY)
    return group_index if isinstance(group_index, int) else None


def _token_count(message: Message) -> int | None:
    """Read a token count only when its annotation uses the current estimator version."""
    annotation = _read_group_annotation(message)
    if annotation is None:
        return None
    estimator_version = annotation.get(GROUP_TOKEN_ESTIMATOR_VERSION_KEY)
    if (
        not isinstance(estimator_version, int)
        or isinstance(estimator_version, bool)
        or estimator_version != TOKEN_ESTIMATOR_VERSION
    ):
        return None
    token_count = annotation.get(GROUP_TOKEN_COUNT_KEY)
    return token_count if isinstance(token_count, int) else None


def _write_token_count(message: Message, token_count: int) -> None:
    """Store a token count and estimator version in the existing annotation.

    Silently do nothing when no annotation mapping exists.
    """
    annotation = _read_group_annotation_raw(message)
    if annotation is None:
        return
    annotation[GROUP_TOKEN_COUNT_KEY] = token_count
    annotation[GROUP_TOKEN_ESTIMATOR_VERSION_KEY] = TOKEN_ESTIMATOR_VERSION
    message.additional_properties[GROUP_ANNOTATION_KEY] = annotation


def _ordered_group_ids_from_annotations(messages: Sequence[Message]) -> list[str]:
    """Collect distinct annotated group ids in first-occurrence order."""
    ordered_group_ids: list[str] = []
    seen: set[str] = set()
    for message in messages:
        group_id = _group_id(message)
        if group_id is not None and group_id not in seen:
            seen.add(group_id)
            ordered_group_ids.append(group_id)
    return ordered_group_ids


def _first_untokenized_index(messages: Sequence[Message]) -> int | None:
    """Find the first message without a current token estimate, or None when all have one."""
    for index, message in enumerate(messages):
        if _token_count(message) is None:
            return index
    return None


def _first_annotation_gaps(
    messages: Sequence[Message],
    *,
    include_tokens: bool,
) -> tuple[int | None, int | None]:
    """Scan until the first missing group annotation or requested token count."""
    first_unannotated: int | None = None
    first_untokenized: int | None = None
    for index, message in enumerate(messages):
        missing_group_annotation = first_unannotated is None and _group_id(message) is None
        missing_token_annotation = include_tokens and first_untokenized is None and _token_count(message) is None

        if missing_group_annotation:
            first_unannotated = index
        if missing_token_annotation:
            first_untokenized = index

        if missing_group_annotation or missing_token_annotation:
            break
    return first_unannotated, first_untokenized


def _reannotation_start(messages: Sequence[Message], index: int) -> int:
    """Back the start index up to a position where regrouping is exchange-safe.

    First to the beginning of the group the preceding message belongs to,
    then — while the rewound position can still sit inside an exchange
    begun earlier — further back along the exchange's backward shape:
    output-block members (tool-role or result-only assistant) may follow
    output or response-run members, response-run members only other
    response-run members. Call/result fusion spans whole exchanges, so a
    rewind stopping inside one regroups the suffix without its earlier
    call spans — a result landing after a previously annotated
    ``[call, text]`` prefix would freeze the split grouping and leave the
    call excludable apart from its result. A response-run member preceded
    by an output-block member marks a COMPLETED earlier exchange, and
    user/system roles and history markers are boundaries no exchange
    crosses; stopping there keeps re-annotation per-exchange — walking
    past finished exchanges would rewrite their annotations on every pass
    and turn a long tool loop's incremental annotation quadratic.
    """
    if index <= 0:
        return 0
    previous_index = index - 1
    previous_group_id = _group_id(messages[previous_index])
    previous_group_index = _group_index(messages[previous_index])
    if previous_group_id is not None:
        # Key on the group OCCURRENCE (id AND index): group ids are minted
        # from the start message's id, so preset message_id collisions can
        # give adjacent groups the same id — an id-only walk would cross
        # them before the exchange-shape boundary check below runs.
        while previous_index > 0:
            if (
                _group_id(messages[previous_index - 1]) != previous_group_id
                or _group_index(messages[previous_index - 1]) != previous_group_index
            ):
                break
            previous_index -= 1
    while previous_index > 0:
        current = messages[previous_index]
        preceding = messages[previous_index - 1]
        if _is_output_block_member(current):
            if not (_is_output_block_member(preceding) or _is_response_run_member(preceding)):
                break
        elif _is_response_run_member(current):
            if not _is_response_run_member(preceding):
                break
        else:
            break
        previous_index -= 1
    return previous_index


def _is_output_block_member(message: Message) -> bool:
    """Markerless tool-role or result-only assistant message — the shapes an
    exchange's output block is made of."""
    if _LIVE_ACCESSOR.has_marker(message):
        return False
    return message.role == "tool" or is_result_only_message(message, _LIVE_ACCESSOR)


def _is_response_run_member(message: Message) -> bool:
    """Markerless assistant message that is not result-only — the shapes a
    response run is made of."""
    return (
        message.role == "assistant"
        and not _LIVE_ACCESSOR.has_marker(message)
        and not is_result_only_message(message, _LIVE_ACCESSOR)
    )


def annotate_message_groups(
    messages: list[Message],
    *,
    from_index: int | None = None,
    force_reannotate: bool = False,
    tokenizer: TokenizerProtocol | None = None,
) -> list[str]:
    """Annotate message groups while reusing existing annotations when possible.

    By default, the function re-annotates only the suffix that contains new
    messages and keeps previously annotated prefixes untouched. When a
    ``tokenizer`` is provided, token-count annotations are also populated
    incrementally.
    """
    if not messages:
        return []

    if force_reannotate:
        start_index = 0
    elif from_index is not None:
        start_index = max(0, min(from_index, len(messages) - 1))
    else:
        first_unannotated_index, first_untokenized_index = _first_annotation_gaps(
            messages,
            include_tokens=tokenizer is not None,
        )
        candidate_starts = [index for index in (first_unannotated_index, first_untokenized_index) if index is not None]
        if not candidate_starts:
            return _ordered_group_ids_from_annotations(messages)
        start_index = min(candidate_starts)

    start_index = _reannotation_start(messages, start_index)

    # Continue group indices from the preserved prefix when only re-annotating a suffix.
    group_index_offset = 0
    if start_index > 0:
        previous_group_index = _group_index(messages[start_index - 1])
        if previous_group_index is not None:
            group_index_offset = previous_group_index + 1

    reserved_ids = {message.message_id for message in messages[:start_index] if message.message_id}
    spans = group_messages(messages[start_index:], id_offset=start_index, reserved_ids=reserved_ids)
    for span_index, span in enumerate(spans):
        group_id = str(span["group_id"])
        kind = _coerce_group_kind(span["kind"])
        if kind is None:
            raise ValueError(f"Unexpected group kind in span: {span['kind']}")
        local_start_index = int(span["start_index"])
        local_end_index = int(span["end_index"])
        has_reasoning = bool(span["has_reasoning"])
        for idx in range(start_index + local_start_index, start_index + local_end_index + 1):
            message = messages[idx]
            _write_group_annotation(
                message,
                group_id=group_id,
                kind=kind,
                index=group_index_offset + span_index,
                has_reasoning=has_reasoning,
            )
            message.additional_properties.setdefault(EXCLUDED_KEY, False)
            if tokenizer is not None and _token_count(message) is None:
                _write_token_count(message, _message_token_estimate(message, tokenizer))
    return _ordered_group_ids_from_annotations(messages)


def _serialize_content(content: Content) -> dict[str, Any]:
    """Serialize content for token estimation, excluding duplicate and opaque replay data."""
    payload = content.to_dict(exclude_none=True)
    payload.pop("raw_representation", None)
    additional_properties = payload.get("additional_properties")
    if isinstance(additional_properties, dict):
        additional_properties.pop(EXECUTION_STAMP_KEY, None)
        additional_properties.pop(REASONING_ORIGIN_KEY, None)
        additional_properties.pop(ANTHROPIC_THINKING_STRIPPED_KEY, None)
        for key in HOSTED_WIRE_REPLAY_PROPERTY_KEYS:
            additional_properties.pop(key, None)
    # ``items`` mirrors ``result`` for function_result content; exclude it
    # to avoid double-counting tokens during estimation.
    payload.pop("items", None)
    # Responses reasoning items carry server-encrypted replay state alongside
    # an item id. Although this blob is sent back to the provider, it is opaque
    # state rather than prompt text; tokenizing its base64 representation can
    # overestimate the context by thousands of tokens. Id-less protected data
    # remains included because chat-completions dialects use it for replayable
    # reasoning_content/reasoning_details payloads.
    if content.type == "text_reasoning" and content.id is not None:
        payload.pop("protected_data", None)
    # Chat Completions ``reasoning_details`` replay as their protected payload;
    # the text beside it is that payload's readable copy, shown but never sent.
    if (
        content.type == "text_reasoning"
        and content.additional_properties.get("openai_reasoning_format") == "reasoning_details"
    ):
        payload.pop("text", None)
    return _redact_serialized_image_uris(payload)


def _redact_serialized_image_uris(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {
            key: nested
            if key == "arguments" and _serialized_arguments_are_text_budgeted(value)
            else _redact_serialized_image_uris(nested)
            for key, nested in value.items()
        }
        if _serialized_dict_is_image_content(redacted) and "uri" in redacted:
            redacted["uri"] = _IMAGE_URI_TOKEN_PLACEHOLDER
        return redacted
    if isinstance(value, list):
        return [_redact_serialized_image_uris(item) for item in value]
    return value


def _serialized_dict_is_image_content(value: Mapping[Any, Any]) -> bool:
    content_type = value.get("type")
    if content_type not in ("data", "uri"):
        return False
    return (
        is_image_media_type(value.get("media_type"))
        or is_image_media_type(_serialized_additional_media_type(value))
        or is_image_data_uri(value.get("uri"))
    )


def _serialized_additional_media_type(value: Mapping[Any, Any]) -> Any:
    additional_properties = value.get("additional_properties")
    if not isinstance(additional_properties, dict):
        return None
    return additional_properties.get("media_type")


def _serialized_arguments_are_text_budgeted(value: Mapping[Any, Any]) -> bool:
    return value.get("type") in _TEXT_BUDGETED_ARGUMENT_CONTENT_TYPES


def _serialize_message(message: Message) -> str:
    """Produce stable estimator serialization with sorted keys.

    Keep non-ASCII text literal for mixed-language token counting.
    """
    serialized_contents = [_serialize_content(content) for content in message.contents]
    payload = {
        "role": message.role,
        "message_id": message.message_id,
        "contents": serialized_contents,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _message_token_estimate(message: Message, tokenizer: TokenizerProtocol) -> int:
    """Return serialized text tokens plus image-content estimates."""
    image_tokens = estimate_message_image_tokens(message)
    return tokenizer.count_tokens(_serialize_message(message)) + image_tokens


def estimate_message_tokens(message: Message, tokenizer: TokenizerProtocol) -> int:
    """Estimate one message without mutating message or content metadata.

    The annotation path opportunistically caches decoded image dimensions on
    content objects. Scoped Phase-4 slices must remain strictly read-only
    during admission and clipping calculations, so they use this variant.
    """
    image_tokens = sum(
        _estimate_image_tokens_in_tree(content, set(), cache_dimensions=False) for content in message.contents
    )
    return tokenizer.count_tokens(_serialize_message(message)) + image_tokens


def estimate_message_image_tokens(message: Message) -> int:
    """Estimate image tokens for direct and nested image contents."""
    total = 0
    stack: set[int] = set()
    for content in message.contents:
        total += _estimate_image_tokens_in_tree(content, stack, cache_dimensions=True)
    return total


def estimate_toolresult_image_tokens(message: Message) -> int:
    """Estimate image tokens for function_result.items omitted from serialization."""
    total = 0
    for content in message.contents:
        if content.type != "function_result" or not content.items:
            continue
        for item in content.items:
            if is_image_content(item):
                total += _estimate_image_tokens(item)
    return total


def _estimate_image_tokens_in_tree(value: Any, stack: set[int], *, cache_dimensions: bool) -> int:
    if isinstance(value, Content):
        content_id = id(value)
        if content_id in stack:
            return 0
        stack.add(content_id)
        try:
            total = _estimate_image_tokens(value, cache_dimensions=cache_dimensions) if is_image_content(value) else 0
            for child_value in _content_child_values(value):
                total += _estimate_image_tokens_in_tree(child_value, stack, cache_dimensions=cache_dimensions)
            return total
        finally:
            stack.remove(content_id)
    if isinstance(value, Mapping):
        mapping_id = id(value)
        if mapping_id in stack:
            return 0
        stack.add(mapping_id)
        try:
            if _serialized_dict_is_image_content(value):
                return _estimate_serialized_image_tokens(value, cache_dimensions=cache_dimensions)
            total = 0
            for key, nested in value.items():
                if key == "arguments" and _serialized_arguments_are_text_budgeted(value):
                    continue
                total += _estimate_image_tokens_in_tree(nested, stack, cache_dimensions=cache_dimensions)
            return total
        finally:
            stack.remove(mapping_id)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        sequence_id = id(value)
        if sequence_id in stack:
            return 0
        stack.add(sequence_id)
        try:
            total = 0
            for nested in value:
                total += _estimate_image_tokens_in_tree(nested, stack, cache_dimensions=cache_dimensions)
            return total
        finally:
            stack.remove(sequence_id)
    return 0


def _content_child_values(content: Content) -> Iterable[Any]:
    # Keep this aligned with the serialized fields walked by _redact_serialized_image_uris.
    return (
        content.items,
        content.inputs,
        content.outputs,
        content.output,
        content.result,
    )


def _estimate_image_tokens(content: Content, *, cache_dimensions: bool = True) -> int:
    width = _positive_int(content.additional_properties.get("width"))
    height = _positive_int(content.additional_properties.get("height"))
    if width is None or height is None:
        inferred_dimensions = _image_dimensions_from_data_uri(content.uri)
        if inferred_dimensions is not None:
            width, height = inferred_dimensions
            if cache_dimensions:
                _cache_content_image_dimensions(content, width, height)
    if width is None or height is None:
        return _IMAGE_FALLBACK_TOKENS
    return _image_tokens_from_dims(width, height)


def _estimate_serialized_image_tokens(content: Mapping[Any, Any], *, cache_dimensions: bool = True) -> int:
    width = _positive_int(content.get("width"))
    height = _positive_int(content.get("height"))
    additional_properties = content.get("additional_properties")
    if isinstance(additional_properties, Mapping):
        width = width or _positive_int(additional_properties.get("width"))
        height = height or _positive_int(additional_properties.get("height"))
    if width is None or height is None:
        inferred_dimensions = _image_dimensions_from_data_uri(content.get("uri"))
        if inferred_dimensions is not None:
            width, height = inferred_dimensions
            if cache_dimensions and isinstance(content, dict):
                _cache_serialized_image_dimensions(content, width, height)
    if width is None or height is None:
        return _IMAGE_FALLBACK_TOKENS
    return _image_tokens_from_dims(width, height)


def _image_tokens_from_dims(width: int, height: int) -> int:
    tiles = max(1, math.ceil(width / _IMAGE_TILE_SIZE)) * max(1, math.ceil(height / _IMAGE_TILE_SIZE))
    return _IMAGE_BASE_TOKENS + (_IMAGE_TILE_TOKENS * tiles)


def _cache_content_image_dimensions(content: Content, width: int, height: int) -> None:
    content.additional_properties["width"] = width
    content.additional_properties["height"] = height


def _cache_serialized_image_dimensions(content: dict[Any, Any], width: int, height: int) -> None:
    additional_properties = content.get("additional_properties")
    if not isinstance(additional_properties, dict):
        additional_properties = {}
        content["additional_properties"] = additional_properties
    additional_properties["width"] = width
    additional_properties["height"] = height


def _image_dimensions_from_data_uri(uri: Any) -> tuple[int, int] | None:
    if not isinstance(uri, str):
        return None
    normalized_uri = uri.lower()
    marker = ";base64,"
    marker_index = normalized_uri.find(marker)
    if not normalized_uri.startswith("data:") or marker_index < 0:
        return None
    encoded = uri[marker_index + len(marker) :]
    try:
        data = base64.b64decode(encoded, validate=False)
    except binascii.Error, ValueError:
        return None
    try:
        return inspect_image_dimensions(data)
    except Exception:
        return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def annotate_token_counts(
    messages: list[Message],
    *,
    tokenizer: TokenizerProtocol,
    from_index: int | None = None,
    force_retokenize: bool = False,
) -> None:
    """Annotate token-count metadata, incrementally by default."""
    if not messages:
        return

    # Token counts are stored inside group annotations.
    annotate_message_groups(messages, from_index=from_index)

    if force_retokenize:
        start_index = 0
    elif from_index is not None:
        start_index = max(0, min(from_index, len(messages) - 1))
    else:
        first_untokenized_index = _first_untokenized_index(messages)
        if first_untokenized_index is None:
            return
        start_index = first_untokenized_index

    for message in messages[start_index:]:
        _write_token_count(message, _message_token_estimate(message, tokenizer))


def recount_user_messages(messages: list[Message], *, tokenizer: TokenizerProtocol) -> None:
    """Count every user message again.

    A user message reaches compaction as this call's copy, carrying this call's
    reminders, while its count sits in the annotation it shares with history: a
    count taken under another call's reminders is stale.
    """
    for message in messages:
        if message.role == "user":
            _write_token_count(message, _message_token_estimate(message, tokenizer))


def included_messages(messages: list[Message]) -> list[Message]:
    """Return the messages not marked as excluded, preserving their order and identity."""
    return [message for message in messages if not message.additional_properties.get(EXCLUDED_KEY, False)]


def _carries_call_or_result(message: Message) -> bool:
    """Whether the message participates in the group's call/result pairing.

    Narration-only members (assistant text fused between a call and its
    results) are wire-legal on their own; only carriers must project as an
    all-or-nothing set. Classification is content-scoped regardless of the
    kernel role carrying it: provider serializers emit call/result blocks by
    content (a user-role function_result still becomes a paired result block
    on the wire), so pairing atomicity must cover every carrier the group
    spans even when the exchange grammar treats the message as transcript
    input for boundary purposes."""
    return any(
        content.type in TOOL_CALL_CONTENT_TYPES or content.type in TOOL_RESULT_CONTENT_TYPES
        for content in message.contents
    )


@dataclass(frozen=True, slots=True)
class _PartialToolCallGroup:
    """One positional tool-call annotation occurrence with a partial exclusion.

    ``degrade_indices`` are the absolute indices projection must exclude to
    restore atomicity: the group's call/result carriers plus its
    reasoning-bearing members (reasoning riders never float free of their
    calls). Plain narration members stay included."""

    position: int
    degrade_indices: tuple[int, ...]
    group_id: str
    member_snapshot: tuple[weakref.ref[Message], ...]
    """Weak snapshot of the member Messages at discovery time.

    Compared across the strategy await by :func:`_same_live_members` —
    order-insensitive, and dead members never compare equal, so a freed
    group can never vouch for a same-shaped newcomer at a recycled
    address.
    """


def _same_live_members(
    left: tuple[weakref.ref[Message], ...],
    right: tuple[weakref.ref[Message], ...],
) -> bool:
    """Whether two member snapshots name the same set of LIVE referents.

    Order-insensitive by design: today's semantics accept a strategy that
    merely reorders a partial group's messages, and an order-sensitive
    comparison would reclassify that as a newly introduced partial group.
    Any dead member fails the comparison outright.
    """
    left_members = [ref() for ref in left]
    right_members = [ref() for ref in right]
    if any(member is None for member in left_members) or any(member is None for member in right_members):
        return False
    return {id(member) for member in left_members} == {id(member) for member in right_members}


def group_annotation_signature(message: Message) -> tuple[str, GroupKind, int, bool] | None:
    """Return the validated fields that delimit one contiguous annotation occurrence."""
    annotation = _read_group_annotation(message)
    if annotation is None:
        return None
    return (
        cast("str", annotation[GROUP_ID_KEY]),
        cast("GroupKind", annotation[GROUP_KIND_KEY]),
        cast("int", annotation[GROUP_INDEX_KEY]),
        cast("bool", annotation[GROUP_HAS_REASONING_KEY]),
    )


def _partial_tool_call_groups(messages: Sequence[Message]) -> list[_PartialToolCallGroup]:
    """Find partially excluded tool-call groups without joining disjoint equal IDs."""
    partial_groups: list[_PartialToolCallGroup] = []
    index = 0
    position = 0
    while index < len(messages):
        signature = group_annotation_signature(messages[index])
        if signature is None:
            index += 1
            continue

        # Adjacent but distinct occurrences can persist byte-identical
        # signatures; a tool-call group never resumes its assistant block after
        # its results, so an assistant message following the group's results
        # starts a new occurrence even under an unchanged signature — unless it
        # only carries results for the block. Results ride tool-role messages
        # or result-only assistant messages (hosted MCP), matching the group
        # annotator's own follower rule; other roles carrying result contents
        # are transcript input, never the group's output run.
        seen_results = messages[index].role == "tool" or is_result_only_message(messages[index], _LIVE_ACCESSOR)
        end_index = index + 1
        while end_index < len(messages) and group_annotation_signature(messages[end_index]) == signature:
            follower = messages[end_index]
            follower_is_result = follower.role == "tool" or is_result_only_message(follower, _LIVE_ACCESSOR)
            if seen_results and follower.role == "assistant" and not follower_is_result:
                break
            seen_results = seen_results or follower_is_result
            end_index += 1

        group_id, kind, _group_index_value, _has_reasoning_value = signature
        if kind == "tool_call":
            # Atomic set: call/result carriers plus reasoning-bearing members
            # (a reasoning item must never outlive its exchange). Plain fused
            # narration is wire-legal alone and stays free in every group.
            atomic_indices = [
                i
                for i in range(index, end_index)
                if _carries_call_or_result(messages[i]) or _has_reasoning(messages[i])
            ]
            excluded = [bool(messages[i].additional_properties.get(EXCLUDED_KEY, False)) for i in atomic_indices]
            if any(excluded) and not all(excluded):
                members = messages[index:end_index]
                partial_groups.append(
                    _PartialToolCallGroup(
                        position=position,
                        degrade_indices=tuple(atomic_indices),
                        group_id=group_id,
                        member_snapshot=tuple(weakref.ref(message) for message in members),
                    )
                )

        position += 1
        index = end_index
    return partial_groups


def _degrade_partial_tool_call_groups(
    messages: list[Message],
    partial_groups: Sequence[_PartialToolCallGroup],
) -> None:
    """Exclude each partial group's surviving atomic members."""
    if not partial_groups:
        return

    for group in partial_groups:
        for message_index in group.degrade_indices:
            message = messages[message_index]
            if not message.additional_properties.get(EXCLUDED_KEY, False):
                set_excluded(
                    message,
                    excluded=True,
                    reason=_PROJECTION_ATOMICITY_EXCLUDE_REASON,
                )

    logger.warning(
        "Degraded partially projected tool-call groups: positions=%s group_ids=%s",
        [group.position for group in partial_groups],
        [group.group_id for group in partial_groups],
    )


def project_included_messages(messages: list[Message]) -> list[Message]:
    """Return model-input messages after atomically degrading partial tool-call groups."""
    _degrade_partial_tool_call_groups(
        messages,
        _partial_tool_call_groups(messages),
    )
    return included_messages(messages)


def included_token_count(messages: list[Message]) -> int:
    """Sum included token counts; untokenized messages count as 0."""
    total = 0
    for message in included_messages(messages):
        token_count = _token_count(message)
        if token_count is not None:
            total += token_count
    return total


def set_excluded(message: Message, *, excluded: bool, reason: str | None = None) -> bool:
    """Set the exclusion flag and return whether it changed.

    A ``None`` reason leaves any previously recorded reason in place.
    """
    changed = bool(message.additional_properties.get(EXCLUDED_KEY, False)) != excluded
    if changed:
        message.additional_properties[EXCLUDED_KEY] = excluded
    if reason is not None:
        message.additional_properties[EXCLUDE_REASON_KEY] = reason
    return changed


def exclude_group_ids(messages: list[Message], group_ids: set[str], *, reason: str) -> bool:
    """Exclude all messages whose annotation belongs to one of ``group_ids``."""
    changed = False
    for message in messages:
        group_id = _group_id(message)
        if group_id is not None and group_id in group_ids:
            changed = set_excluded(message, excluded=True, reason=reason) or changed
    return changed


async def apply_compaction(
    messages: list[Message],
    *,
    strategy: CompactionStrategy | None,
    tokenizer: TokenizerProtocol | None = None,
    context: CompactionCallContext | None = None,
    strict_projection_atomicity: bool = False,
) -> list[Message]:
    """Apply configured compaction and return projected model-input messages.

    The strategy is always called with both positional arguments
    (``strategy(messages, context)``); see the :class:`CompactionStrategy`
    breaking-change note for pre-context strategy signatures. Strict
    projection atomicity runs the strategy on a transactional copy and
    commits only when the strategy did not introduce a partial tool-call
    group. Partial groups already present before the strategy are degraded
    normally.
    """
    if strategy is None:
        return project_included_messages(messages)

    working_messages = deepcopy(messages) if strict_projection_atomicity else messages
    annotate_message_groups(working_messages)
    if tokenizer is not None:
        annotate_token_counts(working_messages, tokenizer=tokenizer)
    preexisting_partial_snapshots = (
        [group.member_snapshot for group in _partial_tool_call_groups(working_messages)]
        if strict_projection_atomicity
        else []
    )

    await strategy(working_messages, context)

    partial_groups = _partial_tool_call_groups(working_messages)
    if strict_projection_atomicity:
        introduced_partial_groups = [
            group
            for group in partial_groups
            if not any(
                _same_live_members(group.member_snapshot, snapshot) for snapshot in preexisting_partial_snapshots
            )
        ]
        if introduced_partial_groups:
            raise CompactionProjectionAtomicityError(
                "Strict compaction rejected partially projected tool-call groups "
                f"at positions {[group.position for group in introduced_partial_groups]}"
            )

    projected = project_included_messages(working_messages)
    if strict_projection_atomicity:
        messages[:] = working_messages
    return projected
