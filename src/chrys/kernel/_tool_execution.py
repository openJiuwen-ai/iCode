# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from Microsoft Agent Framework (MIT License; see NOTICE).

"""Tool-call execution for the model/tool loop.

A landed batch runs concurrently: each call is looked up, validated against
its schema, then run through the function middleware pipeline. Approval
middleware controls tool admission; ``MiddlewareTermination`` stops the loop.
A tool that raises answers the model with a fixed ``Error: Function failed.``
unless it raised ``ModelVisibleToolError``, whose message the model reads;
argument-validation errors include safe schema guidance without echoing the
full argument payload. Every failed result also records the exception tree in
its ``exception`` field, which never goes on the wire.

Tool execution owns the invocation logs and per-tool spans. Raw arguments and
results are logged only when ``TELEMETRY_GATE.sensitive_data`` is set; spans
require ``TELEMETRY_GATE.enabled``. Direct ``FunctionTool.invoke`` calls
remain silent. Tool containers are normalized again before each batch, since
direct callers and progressive exposure can add tools after agent preparation.

This module also defines how a tool result is shaped
(``_result_additional_properties``, ``_tool_trajectory_timing``,
``_failure_record_text``). The recovery journal builds its interrupted results
with the same rules, so a change here must hold for those results too. Results
reach the journal only through ``ToolResultCommit``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import decimal
import json
import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from copy import copy, deepcopy
from time import perf_counter, time_ns
from typing import TYPE_CHECKING, Any, Protocol, TypeGuard

from pydantic import BaseModel, ValidationError

from chrys.foundation.observability.gate import TELEMETRY_GATE
from chrys.foundation.tool_call_context import (
    TOOL_CALL_CONTEXT_METADATA_KEY,
    get_tool_context,
    merge_tool_call_context_property,
)
from chrys.foundation.tool_execution_stamp import EXECUTION_STAMP_KEY, execution_stamp_from_metadata
from chrys.foundation.tool_invocation_order import TOOL_INVOCATION_ORDER_KEY, read_tool_invocation_order
from chrys.foundation.tool_kinds import TOOL_CALL_KIND_METADATA_KEY, get_tool_kind
from chrys.foundation.tool_result_metadata import (
    TOOL_ERROR_KIND_METADATA_KEY,
    TOOL_ERROR_MESSAGE_METADATA_KEY,
    TOOL_FAILED_METADATA_KEY,
    TOOL_RESULT_METADATA_KEY,
)
from chrys.foundation.trajectory.context import current_trajectory
from chrys.foundation.trajectory.event_types import ToolOutcome
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory.metadata import (
    ANALYTICS_ITEM_ID_KEY,
    OPERATION_ID_KEY,
    TOOL_RESULT_CARRIER_ITEM_ID_METADATA_KEY,
    TOOL_RESULT_ITEM_ID_METADATA_KEY,
    read_analytics_item_id,
    read_operation_id,
)
from chrys.foundation.trajectory.tools import tool_operation_finished_draft, tool_operation_started_draft
from chrys.foundation.trajectory.writer import EmitResult
from chrys.foundation.trajectory_timing import (
    TRAJECTORY_TIMING_KEY,
    build_instant_trajectory_timing,
    trajectory_timing_from_metadata,
)
from chrys.foundation.util.sub_agent_context import (
    SUB_AGENT_RESULT_COMMIT_CALLBACK_KEY,
    sub_agent_parent_result_metadata,
)

from ._content import Content
from ._result_ceiling import apply_result_ceiling
from ._tool_arg_errors import (
    _accepts_arbitrary_argument_names,
    _argument_validation_message,
    _rejects_unexpected_arguments,
    _unexpected_argument_names,
)
from .exceptions import ChrysException, tool_error_result_text
from .instrumentation import (
    FUNCTION_SPAN_EXCLUDED_KWARGS,
    OtelAttr,
    capture_exception,
    get_function_duration_histogram,
    get_function_span,
    get_function_span_attributes,
)
from .middleware import FunctionMiddlewarePipeline, MiddlewareTermination
from .sessions import AgentSession
from .tools import (
    FunctionTool,
    SyncToolCancelledAfterCompletion,
    _is_skip_parsing_sentinel,
    _validate_arguments_against_schema,
    normalize_tools,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Container

    from .middleware import FunctionInvocationContext

# Log lines keep the loop's logger name: log configuration and tests filter on it.
logger = logging.getLogger("chrys.kernel.loop")


def _is_actionable_function_call(content: Content) -> bool:
    """Return whether a function call must be executed by the local tool loop."""
    return content.type == "function_call" and not content.informational_only


class ToolResultCommit(Protocol):
    """Journal handle for one call's result slot.

    Defined by its user: the recovery journal's slot handle satisfies it
    structurally, so tool execution never imports the journal.
    ``commit_raw`` runs as soon as the tool returns, before function
    middleware unwinds; ``commit_final`` records the terminal result;
    ``commit_interrupted`` records a call cancelled before it settled. A
    sub-agent tool calls ``commit_interrupted`` again for the same call, through
    ``SUB_AGENT_RESULT_COMMIT_CALLBACK_KEY``, to add result metadata that
    arrives late; the slot must merge it into the interrupted result it
    already holds, even after the exchange was sealed.
    """

    def commit_raw(self, result: Content) -> None: ...

    def commit_final(self, result: Content) -> None: ...

    def commit_interrupted(
        self,
        function_call: Content,
        invocation_context: FunctionInvocationContext | None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None: ...


def _describe_arguments(arguments: Mapping[str, Any] | BaseModel, schema_names: Container[str]) -> str:
    """Render tool arguments for the DEBUG log line.

    Raw argument text is gated on ``TELEMETRY_GATE.sensitive_data``. With the
    gate off, only names listed in the tool schema's ``properties`` are
    disclosed: the model controls the actual keys (schemas rarely pin
    ``additionalProperties: false``, so unexpected keys survive validation)
    and could smuggle sensitive text through a key name — such keys are
    reported as an ``+N unrecognized`` count only.
    """
    if _is_argument_mapping(arguments):
        argument_mapping: Mapping[str, Any] = arguments
    else:
        # The middleware contract admits ``BaseModel`` rewrites of
        # ``context.arguments``; pydantic models iterate as (key, value)
        # pairs, so ``dict()`` recovers the mapping form. ``tool.invoke``
        # normalizes the same way internally — a describe-only crash here
        # would otherwise be converted into a tool error result.
        argument_mapping = dict(arguments)
    if TELEMETRY_GATE.sensitive_data:
        return str(dict(argument_mapping))
    if not argument_mapping:
        return "0 key(s)"
    known = [key for key in argument_mapping if key in schema_names]
    parts = (
        known
        if len(known) == len(argument_mapping)
        else [*known, f"+{len(argument_mapping) - len(known)} unrecognized"]
    )
    return f"{len(argument_mapping)} key(s) ({', '.join(parts)})"


def _is_argument_mapping(value: Mapping[str, Any] | BaseModel) -> TypeGuard[Mapping[str, Any]]:
    """Narrow middleware arguments after the runtime mapping check."""
    return isinstance(value, Mapping)


def _describe_result(result: Any, *, skip_parsing: bool) -> str:
    """Render a tool result for DEBUG logging under the sensitive-data gate.

    Skip-parsing tools return arbitrary values, including non-Content lists:
    render those with ``str`` when sensitive data is enabled, otherwise use
    the type name. Parsed results are ``list[Content]`` and can be summarized
    by item type or, when permitted, joined as text.
    """
    if skip_parsing or not isinstance(result, list):
        return str(result) if TELEMETRY_GATE.sensitive_data else type(result).__name__
    if TELEMETRY_GATE.sensitive_data:
        text = "\n".join(item.text or "" for item in result if item.type == "text")
        return text or str(result)
    if not result:
        return "None"
    return f"{len(result)} item(s) ({', '.join(item.type for item in result)})"


_FUNCTION_DURATION_HISTOGRAM: Any = None


def _function_duration_histogram() -> Any:
    """Cache the function-duration histogram on its first gated use.

    Lazy construction lets provider setup finish before obtaining the meter.
    """
    global _FUNCTION_DURATION_HISTOGRAM
    if _FUNCTION_DURATION_HISTOGRAM is None:
        _FUNCTION_DURATION_HISTOGRAM = get_function_duration_histogram()
    return _FUNCTION_DURATION_HISTOGRAM


async def _invoke_with_function_span(
    tool: FunctionTool,
    context: Any,
    call_id: str | None,
    *,
    arguments: BaseModel | Mapping[str, Any],
    arguments_in_callable_keyspace: bool = False,
    tool_result_ceiling_tokens: int | None = None,
) -> Any:
    """Run ``tool.invoke`` inside an ``execute_tool {name}`` span.

    Capture arguments and results only under ``TELEMETRY_GATE.sensitive_enabled``;
    record exceptions and duration on failure as well as success. The caller
    owns logging. Skip-parsing results use ``str(result)``; parsed Content
    lists contribute their text, selected by the same ``SKIP_PARSING`` sentinel
    that ``invoke`` uses.

    Captured arguments are the middleware-final values. Validation happens
    inside ``invoke``, so the span does not capture its normalized argument dict.
    """
    attributes = get_function_span_attributes(tool, tool_call_id=call_id)
    # The middleware contract admits ``BaseModel`` rewrites of
    # ``context.arguments`` (same shape ``_describe_arguments`` / ``invoke``
    # handle); normalize it to a dict before filtering so sensitive capture
    # doesn't silently record ``"None"`` for a model-shaped argument set.
    if isinstance(arguments, BaseModel):
        argument_items: dict[str, Any] = arguments.model_dump(exclude_none=True)
    elif isinstance(arguments, Mapping):
        argument_items = dict(arguments)
    else:
        argument_items = {}
    serializable_kwargs = {k: v for k, v in argument_items.items() if k not in FUNCTION_SPAN_EXCLUDED_KWARGS}
    if TELEMETRY_GATE.sensitive_enabled:
        attributes[OtelAttr.TOOL_ARGUMENTS] = (
            json.dumps(serializable_kwargs, default=str, ensure_ascii=False) if serializable_kwargs else "None"
        )
    with get_function_span(attributes=attributes) as span:
        attributes[OtelAttr.MEASUREMENT_FUNCTION_TAG_NAME] = tool.name
        start_time_stamp = perf_counter()
        end_time_stamp: float | None = None
        try:
            result = await tool.invoke(
                arguments=arguments,
                context=context,
                tool_call_id=call_id,
                _arguments_in_callable_keyspace=arguments_in_callable_keyspace,
            )
            result = apply_result_ceiling(result, tool_result_ceiling_tokens)
            end_time_stamp = perf_counter()
        except Exception as exception:
            end_time_stamp = perf_counter()
            attributes[OtelAttr.ERROR_TYPE] = type(exception).__name__
            capture_exception(span=span, exception=exception, timestamp=time_ns())
            raise
        else:
            if TELEMETRY_GATE.sensitive_enabled:
                if _is_skip_parsing_sentinel(tool.result_parser):
                    result_str = str(result)
                else:
                    result_str = "\n".join(c.text or "" for c in result if c.type == "text") or str(result)
                span.set_attribute(OtelAttr.TOOL_RESULT, result_str)
            return result
        finally:
            duration = (end_time_stamp or perf_counter()) - start_time_stamp
            span.set_attribute(OtelAttr.MEASUREMENT_FUNCTION_INVOCATION_DURATION, duration)
            _function_duration_histogram().record(duration, attributes=attributes)


def _arguments_unparseable(arguments: Any) -> bool:
    """True when raw tool-call arguments are an empty or invalid JSON string.

    This is the fingerprint of a truncated / incomplete argument payload — the
    model was cut off before or during JSON emission, so ``Content.parse_arguments``
    either returns ``{}`` for an empty string or falls back to wrapping the raw
    blob under ``{"raw": ...}``. It deliberately does NOT flag arguments that
    parsed cleanly (a dict, or a pre-parsed mapping) but failed schema validation
    — those are real argument errors, not a token-limit cutoff.
    """
    if not isinstance(arguments, str):
        return False
    if not arguments:
        return True
    try:
        json.loads(arguments)
    except json.JSONDecodeError:
        return True
    return False


def _arguments_not_object(arguments: Any) -> bool:
    """True when raw tool-call arguments are present but are not a JSON object.

    ``Content.parse_arguments`` wraps such a payload as ``{"raw": ...}``, and a
    tool whose schema accepts extra keys would then run on arguments the model
    never gave it (MCP adapters drop the undeclared ``raw`` and call with none).
    ``None`` and ``""`` mean "no arguments"; whitespace, undecodable text and
    any JSON value other than an object do not, nor does a parsed non-mapping
    value, however falsy (``[]``, ``0``, ``False``).
    """
    if arguments is None:
        return False
    if isinstance(arguments, str):
        if not arguments:
            return False
        try:
            loaded = json.loads(arguments)
        except json.JSONDecodeError:
            return True
        return not isinstance(loaded, dict)
    return not isinstance(arguments, Mapping)


def _middleware_arguments_equal(left: Any, right: Any) -> bool:
    """Type-strict structural equality for the trusted middleware snapshot."""
    if type(left) is not type(right):
        return False
    if type(left) is dict:
        if len(left) != len(right):
            return False
        unmatched = list(right.items())
        for left_key, left_value in left.items():
            for index, (right_key, right_value) in enumerate(unmatched):
                if _middleware_arguments_equal(left_key, right_key):
                    if not _middleware_arguments_equal(left_value, right_value):
                        return False
                    unmatched.pop(index)
                    break
            else:
                return False
        return True
    if type(left) in {list, tuple}:
        return len(left) == len(right) and all(
            _middleware_arguments_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    if type(left) in {set, frozenset}:
        if len(left) != len(right):
            return False
        unmatched = list(right)
        for left_item in left:
            for index, right_item in enumerate(unmatched):
                if _middleware_arguments_equal(left_item, right_item):
                    unmatched.pop(index)
                    break
            else:
                return False
        return True
    if type(left) in {float, complex, decimal.Decimal}:
        return bool(left == right and repr(left) == repr(right))
    if type(left) in {datetime.datetime, datetime.time}:
        return bool(
            left == right
            and left.utcoffset() == right.utcoffset()
            and left.tzname() == right.tzname()
            and left.fold == right.fold
        )
    return bool(left == right)


def _arguments_look_like_truncated_mapping(arguments: Any, schema: Mapping[str, Any]) -> bool:
    """True for parsed Anthropic tool args that look cut off, not just invalid."""
    if not isinstance(arguments, Mapping):
        return False
    if not arguments:
        return True

    required = schema.get("required")
    properties = schema.get("properties")
    if not isinstance(required, Sequence) or isinstance(required, (str, bytes)):
        return False
    if not isinstance(properties, Mapping):
        return False

    argument_keys = {key for key in arguments if isinstance(key, str)}
    if len(argument_keys) != len(arguments):
        return False
    if not argument_keys.issubset(properties.keys()):
        return False
    return any(isinstance(key, str) and key not in arguments for key in required)


def _stamp_call_provenance(function_call: Content, tool: FunctionTool) -> None:
    """Stamp tool kind + static context onto the call content, first-write-wins.

    Runs right after tool lookup, BEFORE argument validation, so calls that
    fail pre-pipeline validation — which return without entering the
    middleware pipeline — still persist their provenance. Static context
    only: the per-call builder needs validated args and belongs to the
    record pipeline.
    """
    props = function_call.additional_properties
    kind = get_tool_kind(tool)
    if kind and TOOL_CALL_KIND_METADATA_KEY not in props:
        props[TOOL_CALL_KIND_METADATA_KEY] = kind
    static = get_tool_context(tool)
    if static and TOOL_CALL_CONTEXT_METADATA_KEY not in props:
        # Already sanitized at set_tool_context() — single sanitization
        # authority; the stamp only copies, never re-sanitizes.
        props[TOOL_CALL_CONTEXT_METADATA_KEY] = dict(static)


# The failure record is saved with the session and exported to telemetry: a long
# cause chain, a large exception group or a message that embeds a response body
# must not bloat either. Each message is clipped before the record is joined, the
# exception cap also bounds the record's recursion, and the total cap is the backstop.
_FAILURE_RECORD_MAX_EXCEPTIONS = 16
_FAILURE_RECORD_MAX_MESSAGE_CHARS = 1000
_FAILURE_RECORD_MAX_CHARS = 4000


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _exception_message(exc: BaseException) -> str:
    """Return *exc*'s own message, clipped for the failure record.

    ``str()`` of a ChrysException built with an ``inner_exception`` is the repr
    of its args tuple, and a KeyError's is quoted; both hold the message itself
    as their first argument. A ``__str__`` that raises must not stop the call
    from becoming a failed result, so it records what a traceback would.
    """
    if isinstance(exc, ChrysException | KeyError) and exc.args and isinstance(exc.args[0], str):
        message = exc.args[0]
    else:
        try:
            message = str(exc)
        except Exception:
            message = "<exception str() failed>"
    return _clip(message.strip(), _FAILURE_RECORD_MAX_MESSAGE_CHARS)


def _exception_causes(exc: BaseException) -> list[BaseException]:
    """Return what *exc* was explicitly raised from and, when it differs, the ``inner_exception`` a ChrysException wraps."""
    causes = [] if exc.__cause__ is None else [exc.__cause__]
    if isinstance(exc, ChrysException) and len(exc.args) > 1:
        inner = exc.args[1]
        if isinstance(inner, BaseException) and inner is not exc.__cause__:
            causes.append(inner)
    return causes


def _failure_record_text(exc: BaseException) -> str:
    """Return the ``exception`` a failed result records: ``Type: message`` for *exc* and everything it came from.

    The record is a tree, ``Type: message [member; …] (caused by cause; …)``:
    an exception group lists its members in brackets (its ``str()`` only
    counts them), and the causes are ``raise … from`` plus a ChrysException's
    ``inner_exception`` when that is a different exception. It follows only
    these explicit links, never the implicit ``__context__`` of whatever was
    being handled when the tool raised — unlike ``foundation.errors``' linear
    chain walk, which reads that context to classify retryable errors.

    The record is for people reading a saved session, never for the model. It
    is never empty: readers take a non-empty record to mean the call failed.
    """
    seen: set[int] = set()

    def describe(current: BaseException) -> str:
        seen.add(id(current))
        if isinstance(current, BaseExceptionGroup):
            message = _clip(current.message.strip(), _FAILURE_RECORD_MAX_MESSAGE_CHARS)
            members = describe_each(current.exceptions)
        else:
            message = _exception_message(current)
            members = ""
        text = f"{type(current).__name__}: {message}" if message else type(current).__name__
        if members:
            text = f"{text} [{members}]"
        causes = describe_each(_exception_causes(current))
        return f"{text} (caused by {causes})" if causes else text

    def describe_each(exceptions: Sequence[BaseException]) -> str:
        parts: list[str] = []
        for member in exceptions:
            if id(member) in seen:
                continue
            if len(seen) >= _FAILURE_RECORD_MAX_EXCEPTIONS:
                parts.append("…")
                break
            parts.append(describe(member))
        return "; ".join(parts)

    return _clip(describe(exc), _FAILURE_RECORD_MAX_CHARS)


def _result_additional_properties(
    function_call: Content,
    invocation_context: FunctionInvocationContext | None = None,
) -> dict[str, Any]:
    """Return call properties for a function result, minus call provenance.

    Every result-construction path propagates the call's properties; this
    filters call-only provenance (kind, context, invocation ordinal) and any
    stale call-side execution stamp or result metadata. Without the provenance
    filter, the nested context dict would also be shared by reference between
    call and result (the Content constructor's copy is shallow). A fresh stamp
    and fresh result metadata come only from this invocation's context — this
    fold is the single attachment point for ``_chrys_tool_result_metadata``,
    at result construction, when the result's identity is unambiguous.
    """
    result = {
        key: value
        for key, value in function_call.additional_properties.items()
        if key
        not in (
            TOOL_CALL_KIND_METADATA_KEY,
            TOOL_CALL_CONTEXT_METADATA_KEY,
            TOOL_INVOCATION_ORDER_KEY,
            EXECUTION_STAMP_KEY,
            TOOL_RESULT_METADATA_KEY,
            TRAJECTORY_TIMING_KEY,
            ANALYTICS_ITEM_ID_KEY,
        )
    }
    timing = _tool_trajectory_timing(function_call, invocation_context)
    result[TRAJECTORY_TIMING_KEY] = timing
    # The result is its own item (the call's operation id is inherited above
    # so call and result share one tool operation). A pre-minted id on the
    # invocation context keeps the raw-commit checkpoint and the terminal
    # result naming the same item.
    result[ANALYTICS_ITEM_ID_KEY] = _result_item_id(invocation_context)
    if invocation_context is not None:
        stamp = execution_stamp_from_metadata(invocation_context.metadata)
        if stamp is not None:
            result[EXECUTION_STAMP_KEY] = stamp
        carried = invocation_context.metadata.get(TOOL_RESULT_METADATA_KEY)
        if isinstance(carried, Mapping) and carried:
            result[TOOL_RESULT_METADATA_KEY] = dict(carried)
    return result


def _result_item_id(invocation_context: FunctionInvocationContext | None) -> str:
    if invocation_context is not None:
        pre_minted = invocation_context.metadata.get(TOOL_RESULT_ITEM_ID_METADATA_KEY)
        if isinstance(pre_minted, str) and pre_minted:
            return pre_minted
    return new_analytics_id()


def _stamped_tool_context(props: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The static provenance segment stamped on a call, when there is one.

    A call that never enters the pipeline has no builder output — only what
    landing copied off the tool — so an MCP call still names its server.
    """
    context = props.get(TOOL_CALL_CONTEXT_METADATA_KEY)
    return context if isinstance(context, Mapping) and context else None


async def _record_unexecuted_tool_operation(
    function_call: Content,
    *,
    outcome: str,
    result_carrier_item_id: str | None = None,
    result: Content | None = None,
    queued: bool = False,
) -> None:
    """Emit the ``tool.operation`` pair for a call that never entered the pipeline.

    Unknown-tool and invalid-argument calls still own a tool operation id
    (stamped by response dispatch collection) and a result item, so the
    trajectory closes them with their terminal outcome instead of leaving
    dangling operations. A filtered call owns the operation but never gets a
    result, and the pair closes without naming one.
    """
    context = current_trajectory()
    if context is None:
        return
    operation_id = read_operation_id(function_call.additional_properties)
    if operation_id is None:
        return
    props = function_call.additional_properties
    kind = props.get(TOOL_CALL_KIND_METADATA_KEY)
    try:
        started = tool_operation_started_draft(
            context,
            operation_id=operation_id,
            tool_name=function_call.name or "",
            tool_kind=kind if isinstance(kind, str) else "",
            invocation_order=read_tool_invocation_order(props),
            batch_index=None,
            arguments=function_call.arguments,
            call_item_id=read_analytics_item_id(props),
            tool_context=_stamped_tool_context(props),
        )
        finished = tool_operation_finished_draft(
            context,
            operation_id=operation_id,
            outcome=outcome,
            duration_ms=0,
            result_item_id=read_analytics_item_id(result.additional_properties) if result is not None else None,
            result_carrier_item_id=result_carrier_item_id if result is not None else None,
            error_kind=outcome,
        )
    except Exception:
        logger.debug("Trajectory tool operation emit failed", exc_info=True)
        return
    if queued:
        # The caller is unwinding from a cancellation: waiting for the write
        # acknowledgement here would hold a Stop for as long as the writer
        # takes to answer. Both lines take their sequence now and are
        # acknowledged in the background — and a start the sink refuses
        # outright still leaves nothing to close.
        try:
            if context.sink.emit_soon(started) is None:
                context.sink.emit_soon(finished)
        except Exception:
            logger.debug("Trajectory tool operation emit failed", exc_info=True)
        return
    try:
        opened = await context.sink.emit(started)
    except Exception:
        logger.debug("Trajectory tool operation emit failed", exc_info=True)
        return
    except BaseException:
        # Cancelled while the opening line was in flight: the write is
        # committed regardless, so the terminal has to follow it. Only this
        # wait needs the rescue — the one below can first be cancelled once
        # its own line is committed too.
        with contextlib.suppress(Exception):
            context.sink.emit_soon(finished)
        raise
    if opened is not EmitResult.WRITTEN:
        # An unknown tool's name is whatever the model asked for, and one past
        # the line budget makes the whole opening event unwritable: its slot
        # becomes a gap. A terminal behind that gap closes an operation the
        # log never opened, which is not a shape readers handle; an operation
        # that opens and never closes is one they already do.
        return
    try:
        await context.sink.emit(finished)
    except Exception:
        logger.debug("Trajectory tool operation emit failed", exc_info=True)


def _tool_trajectory_timing(
    function_call: Content,
    invocation_context: FunctionInvocationContext | None,
) -> dict[str, Any]:
    """Resolve one tool span and mirror it onto its persisted call content."""
    timing = trajectory_timing_from_metadata(invocation_context.metadata) if invocation_context is not None else None
    if timing is None:
        timing = trajectory_timing_from_metadata(function_call.additional_properties)
    if timing is None:
        timing = build_instant_trajectory_timing()
    function_call.additional_properties[TRAJECTORY_TIMING_KEY] = dict(timing)
    return timing


async def _invoke_function_call(
    function_call: Content,
    *,
    result_commit: ToolResultCommit | None,
    tool_map: dict[str, FunctionTool],
    custom_args: dict[str, Any],
    invocation_session: AgentSession | None,
    pipeline: FunctionMiddlewarePipeline,
    live_tools: list[Any] | None,
    response_truncated: bool = False,
    function_call_may_be_truncated: bool = False,
    same_tool_calls_in_batch: int = 1,
    tool_result_ceiling_tokens: int | None = None,
    result_carrier_item_id: str,
) -> Content:
    """Invoke one model-requested function call through the middleware pipeline.

    A raised exception answers the model with ``tool_error_result_text``: a
    fixed line, or a ``ModelVisibleToolError``'s own message. Argument-validation
    failures include safe schema guidance so the model can repair its next call.
    Each failed result records ``_failure_record_text`` as its ``exception``.
    """
    from .middleware import FunctionInvocationContext

    tool = tool_map.get(function_call.name)  # type: ignore[arg-type]
    if tool is None:
        message = f'Requested function "{function_call.name}" not found.'
        exc = KeyError(f'Function "{function_call.name}" not found.')
        additional = _result_additional_properties(function_call)
        additional.update(
            {
                TOOL_FAILED_METADATA_KEY: True,
                TOOL_ERROR_KIND_METADATA_KEY: "tool_not_found",
                TOOL_ERROR_MESSAGE_METADATA_KEY: message,
            }
        )
        result = Content.from_function_result(
            call_id=function_call.call_id,  # type: ignore[arg-type]
            result=f"Error: {message}",
            exception=_failure_record_text(exc),
            additional_properties=additional,
        )
        await _record_unexecuted_tool_operation(
            function_call,
            outcome=ToolOutcome.UNKNOWN_TOOL,
            result_carrier_item_id=result_carrier_item_id,
            result=result,
        )
        return result

    _stamp_call_provenance(function_call, tool)

    # Judged on the raw payload: parsing would wrap a non-object as {"raw": ...}
    # and turn a falsy parsed value into {}.
    arguments_not_object = _arguments_not_object(function_call.arguments)
    parsed_args: dict[str, Any] = {} if arguments_not_object else dict(function_call.parse_arguments() or {})

    # Filter out internal kwargs before passing to tools; conversation_id is an
    # internal tracking id that must not be forwarded.
    runtime_kwargs: dict[str, Any] = {
        key: value
        for key, value in custom_args.items()
        if key not in {"_function_middleware_pipeline", "middleware", "conversation_id"}
    }
    if invocation_session is not None:
        runtime_kwargs["session"] = invocation_session

    # Pre-pipeline validation: bad arguments return an error
    # result without entering the middleware pipeline (no approval dialog for
    # a call that could never run).
    argument_schema = tool.parameters()
    typed_model_dump = not tool._schema_supplied and tool.input_model is not None
    validation_input_model = tool.input_model if typed_model_dump else None
    reject_unexpected = _rejects_unexpected_arguments(
        argument_schema, typed_model_dump=typed_model_dump
    ) and not _accepts_arbitrary_argument_names(validation_input_model)
    try:
        if arguments_not_object:
            raise TypeError(f"Arguments for '{tool.name}' are not a JSON object.")
        unexpected_arguments = _unexpected_argument_names(
            parsed_args,
            argument_schema,
            reject_unexpected=reject_unexpected,
            input_model=validation_input_model,
        )
        if unexpected_arguments:
            raise TypeError(f"Unexpected argument(s) for '{tool.name}'.")
        if typed_model_dump:
            try:
                validation_args = deepcopy(parsed_args)
            except Exception:
                validation_args = parsed_args
            validated = tool.input_model.model_validate(validation_args)
            transformed_args = tool._dump_arguments(validated)
            _validate_arguments_against_schema(
                arguments=transformed_args,
                schema=tool._serialization_schema,
                tool_name=tool.name,
                tuples_as_arrays=True,
                values_prevalidated=True,
            )
            pipeline_args = transformed_args
        else:
            pipeline_args = _validate_arguments_against_schema(
                arguments=parsed_args, schema=argument_schema, tool_name=tool.name
            )
    except (TypeError, ValidationError) as exc:
        # ``response_truncated`` (finish_reason == "length") is a response-wide
        # signal, so it alone doesn't prove THIS call's arguments were cut off —
        # a fully-parsed but schema-invalid call, or one bad call among several,
        # can ride along in a truncated response. Only report truncation when
        # this call's own argument payload is actually incomplete/unparseable
        # (raw JSON string), or when the final content block is a parsed mapping
        # that looks incomplete (Anthropic blocking path). Otherwise it's a real
        # argument error the model should fix by correcting the arguments, not by
        # raising max_tokens.
        arguments_unparseable = _arguments_unparseable(function_call.arguments)
        arguments_cut_off = arguments_unparseable or (
            function_call_may_be_truncated
            and _arguments_look_like_truncated_mapping(function_call.arguments, argument_schema)
        )
        if response_truncated and arguments_cut_off:
            message = (
                "The tool call was cut off at the model's output token limit "
                "(max_tokens) before its arguments were complete, so they could not "
                "be parsed. Raise max_tokens for this model, or split the work into "
                "smaller calls (e.g. write the file in parts)."
            )
            error_kind = "argument_truncated"
        else:
            message = _argument_validation_message(
                tool_name=tool.name,
                arguments=parsed_args,
                arguments_unparseable=arguments_unparseable or arguments_not_object,
                schema=argument_schema,
                exception=exc,
                reject_unexpected=reject_unexpected,
                input_model=validation_input_model,
            )
            error_kind = "argument_parsing"
        additional = _result_additional_properties(function_call)
        additional.update(
            {
                TOOL_FAILED_METADATA_KEY: True,
                TOOL_ERROR_KIND_METADATA_KEY: error_kind,
                TOOL_ERROR_MESSAGE_METADATA_KEY: message,
            }
        )
        result = Content.from_function_result(
            call_id=function_call.call_id,  # type: ignore[arg-type]
            result=f"Error: {message}",
            exception=_failure_record_text(exc),
            additional_properties=additional,
        )
        await _record_unexecuted_tool_operation(
            function_call,
            outcome=ToolOutcome.INVALID_ARGUMENTS,
            result_carrier_item_id=result_carrier_item_id,
            result=result,
        )
        return result

    args = dict(pipeline_args)
    try:
        pipeline_args_snapshot = deepcopy(args)
    except Exception:
        pipeline_args_snapshot = None

    call_id = function_call.call_id
    if call_id is None:
        # Landing stamped this call an operation id and the batch already
        # counted it as dispatched, so the terminal is owed here — the same
        # debt the unknown-tool and invalid-argument exits above settle.
        await _record_unexecuted_tool_operation(function_call, outcome=ToolOutcome.FILTERED)
        raise KeyError(f'Function "{function_call.name}" is missing call_id.')

    context = FunctionInvocationContext(
        function=tool,
        arguments=args,
        session=invocation_session,
        kwargs=runtime_kwargs.copy(),
        tools=live_tools,
    )
    context.same_tool_calls_in_batch = same_tool_calls_in_batch
    # Always pass call_id to middleware.
    context.metadata["call_id"] = call_id
    # Seed the kernel-stamped invocation ordinal for middleware readers
    # (approval, event persistence). The call content is the single source;
    # the middleware never renumbers a seeded context.
    ordinal = read_tool_invocation_order(function_call.additional_properties)
    if ordinal is not None:
        context.metadata[TOOL_INVOCATION_ORDER_KEY] = ordinal
    # Trajectory identities for middleware readers: the call's tool operation
    # id (landing stamped it on the content) and the item id every result
    # construction path below will give this call's result.
    operation_id = read_operation_id(function_call.additional_properties)
    if operation_id is not None:
        context.metadata[OPERATION_ID_KEY] = operation_id
    # The call's own item id travels the same way, so a call that runs names
    # its request item exactly like one that never reaches the pipeline.
    call_item_id = read_analytics_item_id(function_call.additional_properties)
    if call_item_id is not None:
        context.metadata[ANALYTICS_ITEM_ID_KEY] = call_item_id
    context.metadata[TOOL_RESULT_ITEM_ID_METADATA_KEY] = new_analytics_id()
    context.metadata[TOOL_RESULT_CARRIER_ITEM_ID_METADATA_KEY] = result_carrier_item_id
    if result_commit is not None:

        def _commit_sub_agent_interruption(metadata: Mapping[str, Any]) -> None:
            result_commit.commit_interrupted(function_call, context, metadata)

        context.metadata[SUB_AGENT_RESULT_COMMIT_CALLBACK_KEY] = _commit_sub_agent_interruption

    async def final_function_handler(context_obj: Any) -> Any:
        # Middleware sees normalized callable-keyspace values. An untouched
        # context goes back through invoke in the original validation keyspace;
        # a trusted mapping rewrite bypasses validation-keyspace conversion.
        logger.info("Function name: %s", tool.name)
        if logger.isEnabledFor(logging.DEBUG):
            schema_names = tool.parameters().get("properties") or {}
            logger.debug("Function arguments: %s", _describe_arguments(context_obj.arguments, schema_names))
        # No failure log line here: the outer except below already reports
        # failures (tool name + exception + "returning an error result") — a
        # second line would be duplication (invoke itself is chrys-owned and
        # telemetry/logging-free; the span records the exception).
        start = perf_counter()
        try:
            arguments_unchanged = (
                pipeline_args_snapshot is not None
                and context_obj.arguments is args
                and _middleware_arguments_equal(context_obj.arguments, pipeline_args_snapshot)
            )
        except Exception:
            arguments_unchanged = False
        invocation_arguments = parsed_args if arguments_unchanged else context_obj.arguments
        arguments_in_callable_keyspace = (
            typed_model_dump and not arguments_unchanged and isinstance(invocation_arguments, Mapping)
        )

        def _commit_raw_result(value: Any) -> None:
            if result_commit is None:
                return
            # This callback runs before FunctionMiddleware ``finally`` blocks
            # publish their measured span. The instant timing makes the raw
            # crash checkpoint complete; terminal construction overwrites it
            # from ``context.metadata`` after middleware unwinds.
            additional = _result_additional_properties(function_call, context)
            parent_metadata = sub_agent_parent_result_metadata.get()
            if parent_metadata is not None:
                carried: dict[str, Any] = {}
                if parent_metadata.sub_agent_invocation_id:
                    carried["sub_agent_invocation_id"] = parent_metadata.sub_agent_invocation_id
                if parent_metadata.sub_agent_log_file:
                    carried["sub_agent_log_file"] = parent_metadata.sub_agent_log_file
                if parent_metadata.sub_agent_audit_complete:
                    carried["sub_agent_audit_complete"] = True
                if carried:
                    additional[TOOL_RESULT_METADATA_KEY] = carried
            result_commit.commit_raw(
                Content.from_function_result(
                    call_id=call_id,
                    result=value,
                    additional_properties=additional,
                )
            )

        try:
            if TELEMETRY_GATE.enabled:
                result = await _invoke_with_function_span(
                    tool,
                    context_obj,
                    call_id,
                    arguments=invocation_arguments,
                    arguments_in_callable_keyspace=arguments_in_callable_keyspace,
                    tool_result_ceiling_tokens=tool_result_ceiling_tokens,
                )
            else:
                result = await tool.invoke(
                    arguments=invocation_arguments,
                    context=context_obj,
                    tool_call_id=call_id,
                    _arguments_in_callable_keyspace=arguments_in_callable_keyspace,
                )
                result = apply_result_ceiling(result, tool_result_ceiling_tokens)
        except SyncToolCancelledAfterCompletion as exc:
            # The worker finished before cancellation landed: journal the
            # completed value as this slot's raw result so the interrupt
            # handler preserves it instead of recording a fabricated
            # interruption for work that actually ran.
            exc.completed_result = apply_result_ceiling(exc.completed_result, tool_result_ceiling_tokens)
            _commit_raw_result(exc.completed_result)
            raise
        _commit_raw_result(result)
        logger.info("Function %s succeeded in %.3fs.", tool.name, perf_counter() - start)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Function result: %s",
                _describe_result(result, skip_parsing=_is_skip_parsing_sentinel(tool.result_parser)),
            )
        return result

    try:
        function_result = await pipeline.execute(
            context=context,
            final_handler=final_function_handler,
        )
        return Content.from_function_result(
            call_id=call_id,
            result=function_result,
            additional_properties=_result_additional_properties(function_call, context),
        )
    except asyncio.CancelledError:
        # Function middleware ``finally`` blocks have completed at this
        # boundary, so their measured timing and result metadata are now on
        # the invocation context. Commit with that context before the outer
        # batch-level cancellation fallback loses it.
        if result_commit is not None:
            result_commit.commit_interrupted(function_call, context)
        raise
    except MiddlewareTermination as term_exc:
        # Re-raise to signal loop termination, capturing any middleware-set
        # result first.
        if context.result is not None:
            term_exc.result = Content.from_function_result(
                call_id=call_id,
                result=context.result,
                additional_properties=_result_additional_properties(function_call, context),
            )
        elif isinstance(term_exc.result, Content):
            # Middleware may cache and reuse a prebuilt result across concurrent
            # calls or later runs.  Own a wrapper per invocation before binding
            # call-scoped identity and metadata; its payload may contain
            # external objects that are deliberately unsafe to deep-copy.
            owned_result = copy(term_exc.result)
            owned_result.call_id = call_id
            prebuilt_properties = term_exc.result.additional_properties
            # This call's properties go on top of whatever the prebuilt result
            # carried, the same fold every other result path uses: a result
            # cached across calls arrives wearing another invocation's identity,
            # and the tool operation and pre-minted item id have to be this
            # one's or the trajectory's terminal names an item nothing saved.
            properties = {**prebuilt_properties, **_result_additional_properties(function_call, context)}
            owned = prebuilt_properties.get(TOOL_RESULT_METADATA_KEY)
            if isinstance(owned, Mapping) and owned:
                # A middleware that built its own result Content owns whatever
                # metadata it chose to attach — fold first-write-wins only.
                properties[TOOL_RESULT_METADATA_KEY] = dict(owned)
            owned_result.additional_properties = properties
            term_exc.result = owned_result
        else:
            term_exc.result = Content.from_function_result(
                call_id=call_id,
                result=term_exc.result,
                additional_properties=_result_additional_properties(function_call, context),
            )
        raise
    except Exception as exc:
        logger.warning(
            "Function '%s' raised an exception; returning an error result to the model. Exception: %r",
            tool.name,
            exc,
        )
        return Content.from_function_result(
            call_id=function_call.call_id,  # type: ignore[arg-type]
            result=tool_error_result_text(exc),
            # Never None: it marks the result failed for the consecutive-error
            # count and the providers' error flag, whatever the model reads.
            exception=_failure_record_text(exc),
            additional_properties=_result_additional_properties(function_call, context),
        )
    finally:
        # Backfill per-call provenance subkeys (builder context resolved from
        # the final middleware args) onto the persisted call content. Runs on
        # every pipeline exit; on cancellation the middleware wrote no
        # carriage, so there is nothing to merge.
        carried_context = context.metadata.get(TOOL_CALL_CONTEXT_METADATA_KEY)
        if isinstance(carried_context, Mapping):
            merge_tool_call_context_property(function_call.additional_properties, carried_context)


async def _execute_function_calls(
    *,
    function_calls: Sequence[Content],
    result_commits: Sequence[ToolResultCommit] | None,
    tool_options: dict[str, Any],
    custom_args: dict[str, Any],
    invocation_session: AgentSession | None,
    pipeline: FunctionMiddlewarePipeline,
    result_carrier_item_id: str,
    response_truncated: bool = False,
    truncated_final_function_call_ids: set[int] | None = None,
    tool_result_ceiling_tokens: int | None = None,
    dispatch_observer: Callable[[Sequence[Content]], None] | None = None,
) -> tuple[list[Content], bool, bool]:
    """Execute a batch concurrently and collect results, termination and error flags."""
    raw_tools = tool_options.get("tools")
    if not raw_tools:
        return [], False, False

    # Normalize per batch: direct callers and progressive exposure can add
    # tools after agent preparation. Every executed tool must use FunctionTool.invoke.
    tools = normalize_tools(raw_tools)
    tool_options["tools"] = tools
    if not tools:
        return [], False, False
    # Rebuild from the live list so progressive exposure takes effect next batch.
    # Tool admission has already enforced unique names.
    tool_map: dict[str, FunctionTool] = {t.name: t for t in tools if isinstance(t, FunctionTool)}
    live_tools: list[Any] | None = tools

    # Per-name call counts for this batch: middleware with singleton semantics
    # (e.g. whole-list-replacement tools) reads the stamped count to reject
    # same-batch duplicates deterministically instead of racing the gather.
    same_name_counts = Counter(fc.name for fc in function_calls)

    commits: Sequence[ToolResultCommit | None]
    if result_commits is None:
        commits = (None,) * len(function_calls)
    else:
        if len(result_commits) != len(function_calls):
            raise ValueError("Tool-result commit bindings do not match the extracted call batch.")
        commits = result_commits

    async def invoke_with_termination_handling(
        function_call: Content,
        result_commit: ToolResultCommit | None,
    ) -> tuple[Content, bool]:
        """Catch MiddlewareTermination, returning ``(result, should_terminate)``."""
        # The handover is per call and happens on this task's own first step,
        # not once for the batch: a task cancelled before it ever runs never
        # executes a line of this body, so its operation has to stay in the
        # loop's ledger for the reconciliation pass to close.
        if dispatch_observer is not None:
            dispatch_observer((function_call,))
        try:
            result = await _invoke_function_call(
                function_call,
                result_commit=result_commit,
                tool_map=tool_map,
                custom_args=custom_args,
                invocation_session=invocation_session,
                pipeline=pipeline,
                live_tools=live_tools,
                response_truncated=response_truncated,
                function_call_may_be_truncated=(
                    response_truncated
                    and truncated_final_function_call_ids is not None
                    and id(function_call) in truncated_final_function_call_ids
                ),
                same_tool_calls_in_batch=same_name_counts[function_call.name],
                tool_result_ceiling_tokens=tool_result_ceiling_tokens,
                result_carrier_item_id=result_carrier_item_id,
            )
            result = apply_result_ceiling(result, tool_result_ceiling_tokens)
            if result_commit is not None:
                result_commit.commit_final(result)
            return (result, False)
        except MiddlewareTermination as exc:
            # exc.result may already be a Content (set by _invoke_function_call)
            # or a raw value.
            if isinstance(exc.result, Content):
                bounded_result = apply_result_ceiling(exc.result, tool_result_ceiling_tokens)
                if result_commit is not None:
                    result_commit.commit_final(bounded_result)
                return (bounded_result, True)
            result_content = Content.from_function_result(
                call_id=function_call.call_id,  # type: ignore[arg-type]
                result=exc.result,
                additional_properties=_result_additional_properties(function_call),
            )
            result_content = apply_result_ceiling(result_content, tool_result_ceiling_tokens)
            if result_commit is not None:
                result_commit.commit_final(result_content)
            return (result_content, True)
        except asyncio.CancelledError:
            if result_commit is not None:
                result_commit.commit_interrupted(function_call, None)
            raise

    tasks = [
        asyncio.create_task(invoke_with_termination_handling(function_call, result_commit))
        for function_call, result_commit in zip(function_calls, commits, strict=True)
    ]
    try:
        settled = await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        for task in tasks:
            if not task.done():
                task.cancel()
        settling = asyncio.gather(*tasks, return_exceptions=True)
        while not settling.done():
            try:
                await asyncio.shield(settling)
            except asyncio.CancelledError:
                continue
        raise

    for outcome in settled:
        if isinstance(outcome, BaseException):
            raise outcome
    execution_results = [outcome for outcome in settled if not isinstance(outcome, BaseException)]

    contents: list[Content] = [result[0] for result in execution_results]
    should_terminate = any(result[1] for result in execution_results)
    had_errors = any(fcr.exception is not None for fcr in contents if fcr.type == "function_result")
    return contents, should_terminate, had_errors
