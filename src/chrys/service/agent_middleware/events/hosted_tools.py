# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Provider-neutral planning and live presentation for hosted tools."""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import logging
from collections import Counter, defaultdict, deque
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from copy import copy
from dataclasses import dataclass, field, replace
from typing import Any

from chrys.foundation.hosted_tools import (
    HOSTED_TOOL_DEFAULT_RETRY_SAFETY_BY_FAMILY,
    HOSTED_TOOL_DEFAULT_TITLE_BY_FAMILY,
    HostedRetrySafety,
    HostedToolFamily,
    HostedToolPhase,
    HostedToolStatus,
    normalize_hosted_tool_status,
)
from chrys.foundation.io.result_content import extract_result_images, extract_result_text
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.tool_result_metadata import (
    TOOL_ERROR_CODE_METADATA_KEY,
    TOOL_ERROR_MESSAGE_METADATA_KEY,
    TOOL_ERRORED_METADATA_KEY,
    TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY,
)
from chrys.kernel import AgentResponse, Content, Message
from chrys.kernel.exchanges import (
    TOOL_CALL_CONTENT_TYPES,
    TOOL_RESULT_CONTENT_TYPES,
    EmptyIdPolicy,
    LiveAccessor,
    NoneIdPolicy,
    Occurrence,
    PairingPolicy,
    iter_exchanges,
    pair_results,
)
from chrys.kernel.identity import WeakIdentityRegistry
from chrys.kernel.images import is_image_data_uri, is_image_media_type

type ContentOrder = tuple[int, int, int]

_HOSTED_CALL_TYPES = TOOL_CALL_CONTENT_TYPES - {"function_call"}
_HOSTED_RESULT_TYPES = TOOL_RESULT_CONTENT_TYPES - {"function_result"}
_TERMINAL_STATUSES = frozenset({HostedToolStatus.COMPLETED, HostedToolStatus.FAILED, HostedToolStatus.INTERRUPTED})
_PRESENTATION_PAIRING_POLICY = PairingPolicy(
    call_types=TOOL_CALL_CONTENT_TYPES,
    include_informational_calls=True,
    result_types=TOOL_RESULT_CONTENT_TYPES,
    none_id=NoneIdPolicy.POSITIONAL,
    empty_id=EmptyIdPolicy.POSITIONAL,
    malformed_id="stringify",
)


@dataclass(frozen=True)
class HostedToolView:
    """Safe, provider-neutral presentation fields for one hosted occurrence."""

    family: str
    provider: str
    provider_item_type: str
    provider_call_id: str
    tool_name: str
    display_title: str
    arguments: dict[str, Any]
    result_text: str
    image_contents: list[Content]
    artifacts: list[Content]
    provider_status: str
    status: str
    phase: str
    retry_safety: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class HostedToolStartOp:
    """Start one hosted presentation occurrence."""

    occurrence_ordinal: int
    presentation_id: str
    view: HostedToolView
    order: ContentOrder


@dataclass(frozen=True)
class HostedToolArgsOp:
    """Publish a hosted occurrence's current normalized arguments."""

    occurrence_ordinal: int
    presentation_id: str
    view: HostedToolView
    order: ContentOrder


@dataclass(frozen=True)
class HostedToolProgressOp:
    """Publish a non-terminal hosted progress snapshot."""

    occurrence_ordinal: int
    presentation_id: str
    view: HostedToolView
    order: ContentOrder


@dataclass(frozen=True)
class HostedToolStatusOp:
    """Publish a hosted lifecycle status without fabricating a result."""

    occurrence_ordinal: int
    presentation_id: str
    view: HostedToolView
    order: ContentOrder


@dataclass(frozen=True)
class HostedToolResultOp:
    """Publish a terminal hosted result."""

    occurrence_ordinal: int
    presentation_id: str
    view: HostedToolView
    order: ContentOrder


@dataclass(frozen=True)
class FinalTextOp:
    """Sentinel for the final agent message; empty text remains meaningful."""

    text: str
    structured_output_completed: bool = False


type HostedToolOperation = (
    HostedToolStartOp | HostedToolArgsOp | HostedToolProgressOp | HostedToolStatusOp | HostedToolResultOp
)
type PresentationSinkOperation = HostedToolOperation | FinalTextOp
type PresentationSink = Callable[[PresentationSinkOperation], Awaitable[None] | None]

logger = logging.getLogger(__name__)

_CONTEXT_ARGUMENTS_LIMIT = 1_200
_CONTEXT_RESULT_LIMIT = 2_000


def _family_for(content: Content | None) -> str:
    if content is None:
        return HostedToolFamily.GENERIC
    if content.hosted_family:
        return content.hosted_family
    if content.type.startswith("search_"):
        return HostedToolFamily.SEARCH
    if content.type.startswith("mcp_server_"):
        return HostedToolFamily.MCP
    if content.type.startswith("code_interpreter_"):
        return HostedToolFamily.CODE
    if content.type.startswith("image_generation_"):
        return HostedToolFamily.IMAGE
    if content.type.startswith("shell_"):
        return HostedToolFamily.SHELL
    return HostedToolFamily.GENERIC


def _provider_call_id(content: Content | None) -> str:
    if content is None:
        return ""
    raw_id = content.image_id if content.type.startswith("image_generation_") else content.call_id
    return raw_id if isinstance(raw_id, str) else ""


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_safe(item) for item in value]
    if isinstance(value, Content):
        return value.to_dict()
    return str(value)


def _arguments_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        # A bare ValueError is an integer literal past Python's integer-string
        # conversion limit: expose the raw text, as for malformed JSON.
        except ValueError:
            return {"input": value}
        if isinstance(decoded, Mapping):
            return {str(key): _json_safe(item) for key, item in decoded.items()}
        return {"input": _json_safe(decoded)}
    return {"input": _json_safe(value)}


def _content_values(content: Content | None) -> list[Content]:
    if content is None:
        return []
    candidates: list[Any] = []
    if content.items is not None:
        candidates.extend(content.items)
    if isinstance(content.outputs, Sequence) and not isinstance(content.outputs, str | bytes | bytearray):
        candidates.extend(content.outputs)
    if isinstance(content.output, Sequence) and not isinstance(content.output, str | bytes | bytearray):
        candidates.extend(content.output)
    if isinstance(content.result, Sequence) and not isinstance(content.result, str | bytes | bytearray):
        candidates.extend(content.result)
    return [item for item in candidates if isinstance(item, Content)]


def _is_image(content: Content) -> bool:
    # Share the extraction-side predicate: OpenAI code outputs carry the bare
    # "image" media type, which must not double as an artifact.
    return is_image_media_type(content.media_type) or is_image_data_uri(content.uri)


def _is_artifact(content: Content) -> bool:
    if content.type in {"hosted_file", "hosted_vector_store"}:
        return True
    return content.type in {"data", "uri"} and not _is_image(content)


def _result_payload(content: Content | None) -> Any:
    if content is None:
        return None
    if content.type == "mcp_server_tool_result":
        return content.output
    if content.type in {"code_interpreter_tool_result", "image_generation_tool_result", "shell_tool_result"}:
        return content.outputs
    if content.result is not None:
        return content.result
    if content.items is not None:
        return content.items
    if content.message is not None:
        return content.message
    return None


def _error_code(content: Content | None) -> str | int | None:
    if content is None:
        return None
    if content.error_code:
        return content.error_code
    error = content.additional_properties.get("error")
    if isinstance(error, Mapping):
        code = error.get("error_code", error.get("code"))
        if isinstance(code, str | int) and not isinstance(code, bool):
            return code
    code = content.additional_properties.get("error_code")
    if isinstance(code, str | int) and not isinstance(code, bool):
        return code
    return None


def _is_failed(content: Content | None, provider_status: str) -> bool:
    if normalize_hosted_tool_status(provider_status) is HostedToolStatus.FAILED:
        return True
    return bool(content is not None and content.additional_properties.get("is_error") is True)


def _failure_text(text: str, provider_status: str) -> tuple[str, bool]:
    message = text.strip() or provider_status.strip()
    synthesized = not message
    message = message or "Provider-hosted tool failed."
    return (message if message.startswith("Error: ") else f"Error: {message}", synthesized)


class GenericHostedAdapter:
    """Adapt any canonical hosted pair without provider-specific branches."""

    def adapt(self, call: Content | None, result: Content | None = None) -> HostedToolView:
        family = _family_for(call or result)
        tool_name = ""
        if call is not None:
            tool_name = call.tool_name or call.name or ""
        if not tool_name and result is not None:
            tool_name = result.tool_name or result.name or ""
        if not tool_name:
            tool_name = family

        arguments = self._arguments(call)
        payload = _result_payload(result)
        values = _content_values(result)
        image_contents = extract_result_images(values)
        artifacts = [content for content in values if _is_artifact(content)]
        textual_values = [content for content in values if not _is_image(content) and not _is_artifact(content)]
        if textual_values:
            result_text = extract_result_text(textual_values)
        elif values:
            result_text = ""
        else:
            result_text = extract_result_text(payload) if payload is not None else ""
        provider_status = ""
        for candidate in (result, call):
            if candidate is not None and (candidate.provider_status or candidate.status):
                provider_status = candidate.provider_status or candidate.status or ""
                break
        status = normalize_hosted_tool_status(provider_status)
        failed = _is_failed(result or call, provider_status)
        failure_text_synthesized = False
        if failed:
            status = HostedToolStatus.FAILED
            result_text, failure_text_synthesized = _failure_text(result_text, provider_status)

        phase = ""
        for candidate in (result, call):
            if candidate is not None and candidate.provider_phase:
                phase = candidate.provider_phase
                break
        if not phase:
            phase = HostedToolPhase.TERMINAL if result is not None else HostedToolPhase.START

        retry_safety = ""
        for candidate in (result, call):
            if candidate is not None and candidate.retry_safety:
                retry_safety = candidate.retry_safety
                break
        retry_safety = retry_safety or HOSTED_TOOL_DEFAULT_RETRY_SAFETY_BY_FAMILY.get(family, HostedRetrySafety.UNKNOWN)

        provider = ""
        provider_item_type = ""
        provider_item_id = ""
        for candidate in (call, result):
            if candidate is None:
                continue
            provider = provider or candidate.hosted_provider or ""
            provider_item_type = provider_item_type or candidate.provider_item_type or ""
            provider_item_id = provider_item_id or candidate.provider_item_id or ""

        metadata: dict[str, Any] = {}
        if provider_item_id:
            metadata["provider_item_id"] = provider_item_id
        if phase:
            metadata["provider_phase"] = phase
        if failed:
            metadata[TOOL_ERRORED_METADATA_KEY] = True
            metadata[TOOL_ERROR_MESSAGE_METADATA_KEY] = result_text.removeprefix("Error: ").strip()
            if failure_text_synthesized:
                metadata[TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY] = True
            metadata["provider_status"] = provider_status
            error_code = _error_code(result or call)
            if error_code is not None:
                metadata[TOOL_ERROR_CODE_METADATA_KEY] = error_code

        return HostedToolView(
            family=family,
            provider=provider,
            provider_item_type=provider_item_type,
            provider_call_id=_provider_call_id(call or result),
            tool_name=tool_name,
            display_title=HOSTED_TOOL_DEFAULT_TITLE_BY_FAMILY.get(family, tool_name),
            arguments=arguments,
            result_text=result_text,
            image_contents=image_contents,
            artifacts=artifacts,
            provider_status=provider_status,
            status=status,
            phase=phase,
            retry_safety=retry_safety,
            metadata=metadata,
        )

    def _arguments(self, call: Content | None) -> dict[str, Any]:
        if call is None:
            return {}
        return _arguments_dict(call.arguments)


class SearchHostedAdapter(GenericHostedAdapter):
    """Adapt hosted search and fetch pairs."""


class McpHostedAdapter(GenericHostedAdapter):
    """Adapt provider-managed MCP pairs."""

    def _arguments(self, call: Content | None) -> dict[str, Any]:
        arguments = super()._arguments(call)
        if call is not None and call.server_name:
            return {"server": call.server_name, **arguments}
        return arguments


class CodeHostedAdapter(GenericHostedAdapter):
    """Adapt code-interpreter calls, logs, images, and artifacts."""

    def _arguments(self, call: Content | None) -> dict[str, Any]:
        if call is None:
            return {}
        inputs = call.inputs or []
        code = "".join(content.text or "" for content in inputs if content.type == "text")
        # Anthropic's first canonical representation stored the complete
        # ``{"code": ...}`` input object as text. Unwrap that representation
        # so live and replayed cards expose one provider-neutral code string.
        nested_code = _arguments_dict(code).get("code")
        if isinstance(nested_code, str):
            code = nested_code
        return {"code": code} if code else super()._arguments(call)


class ImageHostedAdapter(GenericHostedAdapter):
    """Adapt image generation snapshots and terminal outputs."""


class ShellHostedAdapter(GenericHostedAdapter):
    """Adapt provider-hosted shell calls and command outputs."""

    def adapt(self, call: Content | None, result: Content | None = None) -> HostedToolView:
        if result is None or result.type != "shell_tool_result" or result.outputs is None:
            return super().adapt(call, result)

        normalized_outputs: list[Content] = []
        command_details: list[dict[str, Any]] = []
        for output in result.outputs:
            if output.type != "shell_command_output":
                normalized_outputs.append(output)
                continue
            details = {
                key: value
                for key, value in {
                    "stdout": output.stdout,
                    "stderr": output.stderr,
                    "exit_code": output.exit_code,
                    "timed_out": output.timed_out,
                }.items()
                if value is not None
            }
            command_details.append(details)
            text = "\n".join(value for value in (output.stdout, output.stderr) if isinstance(value, str) and value)
            if text:
                normalized_outputs.append(Content.from_text(text))

        normalized_result = Content.from_shell_tool_result(
            call_id=result.call_id,
            outputs=normalized_outputs,
            max_output_length=result.max_output_length,
            hosted_provider=result.hosted_provider,
            provider_item_type=result.provider_item_type,
            provider_item_id=result.provider_item_id,
            provider_phase=result.provider_phase,
            provider_status=result.provider_status,
            retry_safety=result.retry_safety,
            annotations=result.annotations,
            additional_properties=dict(result.additional_properties),
            raw_representation=result.raw_representation,
        )
        view = super().adapt(call, normalized_result)
        if not command_details:
            return view

        metadata = dict(view.metadata)
        metadata["outputs"] = command_details
        stdout = "\n".join(
            str(details["stdout"]) for details in command_details if details.get("stdout") not in (None, "")
        )
        stderr = "\n".join(
            str(details["stderr"]) for details in command_details if details.get("stderr") not in (None, "")
        )
        if stdout:
            metadata["stdout"] = stdout
        if stderr:
            metadata["stderr"] = stderr
        exit_codes = [
            details["exit_code"]
            for details in command_details
            if isinstance(details.get("exit_code"), int) and not isinstance(details.get("exit_code"), bool)
        ]
        if exit_codes:
            metadata["exit_code"] = next((code for code in exit_codes if code != 0), exit_codes[-1])
        if any(details.get("timed_out") is True for details in command_details):
            metadata["timed_out"] = True
        elif any("timed_out" in details for details in command_details):
            metadata["timed_out"] = False
        return replace(view, metadata=metadata)

    def _arguments(self, call: Content | None) -> dict[str, Any]:
        if call is None:
            return {}
        arguments: dict[str, Any] = {}
        if call.commands is not None:
            arguments["commands"] = list(call.commands)
        if call.timeout_ms is not None:
            arguments["timeout_ms"] = call.timeout_ms
        if call.max_output_length is not None:
            arguments["max_output_length"] = call.max_output_length
        return arguments or super()._arguments(call)


_SEARCH_ADAPTER = SearchHostedAdapter()
_MCP_ADAPTER = McpHostedAdapter()
_CODE_ADAPTER = CodeHostedAdapter()
_IMAGE_ADAPTER = ImageHostedAdapter()
_SHELL_ADAPTER = ShellHostedAdapter()
_GENERIC_ADAPTER = GenericHostedAdapter()


def get_hosted_adapter(family: str) -> GenericHostedAdapter:
    """Return the six-family adapter selected by canonical hosted family."""
    if family in {HostedToolFamily.SEARCH, HostedToolFamily.FETCH}:
        return _SEARCH_ADAPTER
    if family == HostedToolFamily.MCP:
        return _MCP_ADAPTER
    if family == HostedToolFamily.CODE:
        return _CODE_ADAPTER
    if family == HostedToolFamily.IMAGE:
        return _IMAGE_ADAPTER
    if family == HostedToolFamily.SHELL:
        return _SHELL_ADAPTER
    return _GENERIC_ADAPTER


def adapt_hosted_tool(call: Content | None, result: Content | None = None) -> HostedToolView:
    """Adapt a canonical hosted call and optional paired result."""
    return get_hosted_adapter(_family_for(call or result)).adapt(call, result)


def hosted_replay_status(view: HostedToolView, *, has_result: bool) -> str:
    """Return the terminal status used when restoring a persisted occurrence."""
    status = normalize_hosted_tool_status(view.status)
    if status in _TERMINAL_STATUSES:
        return status.value
    if view.phase == HostedToolPhase.TERMINAL:
        return HostedToolStatus.COMPLETED
    # Persisted history cannot keep a live card alive. A pending/running call,
    # or a partial result snapshot, was interrupted when the session stopped.
    del has_result
    return HostedToolStatus.INTERRUPTED


def _is_successful_structured_output(view: HostedToolView, *, has_result: bool) -> bool:
    """Return whether a hosted occurrence can stand as the turn's visible output."""
    return hosted_replay_status(view, has_result=has_result) == HostedToolStatus.COMPLETED and view.family not in {
        HostedToolFamily.SEARCH,
        HostedToolFamily.FETCH,
        HostedToolFamily.TOOL_DISCOVERY,
    }


def _bounded_context_value(value: Any, limit: int) -> str:
    try:
        rendered = json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)
    except TypeError, ValueError:
        rendered = str(value)
    if len(rendered) <= limit:
        return rendered
    return f"{rendered[: limit - 1]}…"


def hosted_context_summary(call: Content | None, result: Content | None = None) -> str:
    """Build a bounded provider-neutral assistant summary for one occurrence."""
    view = adapt_hosted_tool(call, result)
    status = hosted_replay_status(view, has_result=result is not None)
    lines = [
        "[Provider-hosted tool context]",
        f"Tool: {view.tool_name or view.display_title}",
        f"Family: {view.family}",
        f"Status: {status}",
    ]
    if view.arguments:
        lines.append(f"Arguments: {_bounded_context_value(view.arguments, _CONTEXT_ARGUMENTS_LIMIT)}")
    if view.result_text:
        lines.append(f"Result: {_bounded_context_value(view.result_text, _CONTEXT_RESULT_LIMIT)}")
    if view.image_contents:
        lines.append(f"Images: {len(view.image_contents)}")
    if view.artifacts:
        lines.append(f"Artifacts: {len(view.artifacts)}")
    return "\n".join(lines)


def _is_hosted_call(content: Content) -> bool:
    if content.type in _HOSTED_CALL_TYPES:
        return True
    return content.type == "function_call" and content.informational_only and content.provider_hosted


def _is_hosted_result(content: Content) -> bool:
    return content.type in _HOSTED_RESULT_TYPES or (content.type == "function_result" and content.provider_hosted)


def is_legacy_server_executed_informational_call(content: Content | Mapping[str, Any]) -> bool:
    """Recognize old OpenAI informational calls with explicit server execution proof."""
    if isinstance(content, Content):
        if content.type != "function_call" or not content.informational_only:
            return False
        extra: Mapping[str, Any] = content.additional_properties
    else:
        if content.get("type") != "function_call" or content.get("informational_only") is not True:
            return False
        raw_extra = content.get("additional_properties")
        if not isinstance(raw_extra, Mapping):
            return False
        extra = raw_extra
    item_type = extra.get("item_type")
    if item_type not in {"tool_search_call", "custom_tool_call"}:
        return False
    return extra.get("execution") == "server" or (
        "server_execution" in extra and extra.get("server_execution") is not None
    )


def normalize_hosted_replay_content(content: Content) -> Content:
    """Promote supported legacy server-executed calls to canonical hosted content."""
    if not is_legacy_server_executed_informational_call(content):
        return content
    promoted = copy(content)
    extra = promoted.additional_properties
    item_type = str(extra.get("item_type") or "")
    promoted.provider_hosted = True
    promoted.hosted_family = (
        HostedToolFamily.TOOL_DISCOVERY if item_type == "tool_search_call" else HostedToolFamily.GENERIC
    )
    promoted.hosted_provider = "openai"
    promoted.provider_item_type = item_type
    promoted.provider_item_id = str(extra.get("item_id") or "") or None
    promoted.provider_status = str(extra.get("status") or "") or None
    promoted.provider_phase = HostedToolPhase.TERMINAL
    promoted.tool_name = promoted.name or "tool"
    return promoted


def _coord(occurrence: Occurrence) -> tuple[int, int]:
    return occurrence.message_index, occurrence.content_index


@dataclass(frozen=True)
class _Pair:
    call: tuple[int, int] | None
    result: tuple[int, int] | None


def _presentation_pairs(messages: Sequence[Message]) -> dict[tuple[int, int], _Pair]:
    accessor = LiveAccessor()
    pairs: dict[tuple[int, int], _Pair] = {}
    for exchange in iter_exchanges(messages, accessor):
        pairing = pair_results(messages, exchange, accessor, _PRESENTATION_PAIRING_POLICY)
        assignments = [assignment for entries in pairing.truthy_assignments.values() for assignment in entries] + [
            assignment for entries in pairing.falsy_assignments.values() for assignment in entries
        ]
        for call_occurrence, result_occurrence in assignments:
            call_coord = _coord(call_occurrence)
            result_coord = _coord(result_occurrence) if result_occurrence is not None else None
            pair = _Pair(call_coord, result_coord)
            pairs[call_coord] = pair
            if result_coord is not None:
                pairs[result_coord] = pair
        unconsumed = [occurrence for entries in pairing.unconsumed_results.values() for occurrence in entries] + [
            occurrence for entries in pairing.falsy_unconsumed_results.values() for occurrence in entries
        ]
        for occurrence in [*unconsumed, *pairing.unpairable_results]:
            result_coord = _coord(occurrence)
            pairs.setdefault(result_coord, _Pair(None, result_coord))
        for occurrence in pairing.unpairable_calls:
            call_coord = _coord(occurrence)
            pairs.setdefault(call_coord, _Pair(call_coord, None))

    # ``iter_exchanges`` starts at a call-bearing assistant run, so a fully
    # standalone hosted result belongs to no exchange. Inventory those
    # occurrences without attempting any global ID matching; each result is
    # its own presentation occurrence and degradation anchor.
    for message_index, message in enumerate(messages):
        if message.role not in {"assistant", "tool"} or HistoryMarkerKind.KEY in message.additional_properties:
            continue
        for content_index, content in enumerate(message.contents):
            coordinate = (message_index, content_index)
            if coordinate not in pairs and _is_hosted_result(content):
                pairs[coordinate] = _Pair(None, coordinate)
    return pairs


def _content_at(messages: Sequence[Message], coordinate: tuple[int, int] | None) -> Content | None:
    if coordinate is None:
        return None
    message_index, content_index = coordinate
    return messages[message_index].contents[content_index]


def cross_provider_hosted_degradations(
    messages: Sequence[Message],
    *,
    target_provider: str,
    unsafe_same_provider_families: Collection[str] = (),
) -> dict[int, str | None]:
    """Map hosted contents that are unsafe to replay to one neutral summary each.

    A provider switch always makes a hosted item unsafe. Callers may also
    identify same-provider families whose server-issued item identities cannot
    be replayed through a locally managed transcript.
    """
    pairs = _presentation_pairs(messages)
    degradations: dict[int, str | None] = {}
    handled_pairs: set[_Pair] = set()
    for message_index, message in enumerate(messages):
        for content_index, content in enumerate(message.contents):
            # Hosted-tool persistence shipped together with this marker, so
            # pre-marker histories cannot contain supported hosted records.
            # Current producers must use the canonical factories, which always
            # set it; marker-less hand-built payloads are outside compatibility.
            if not content.provider_hosted:
                continue
            source_provider = content.hosted_provider or ""
            family = _family_for(content)
            # Chat Completions passes an empty target because that wire has no
            # hosted item types: every provider-hosted item is unsafe there,
            # including factory-created contents with unknown provenance. A
            # hosted-capable target may still receive unstamped contents built
            # for its current dialect, so only an explicit mismatch is foreign.
            cross_provider = not target_provider or bool(source_provider and source_provider != target_provider)
            unsafe_same_provider = source_provider == target_provider and family in unsafe_same_provider_families
            if not cross_provider and not unsafe_same_provider:
                continue
            pair = pairs.get((message_index, content_index), _Pair((message_index, content_index), None))
            if pair in handled_pairs:
                continue
            handled_pairs.add(pair)
            call = _content_at(messages, pair.call)
            result = _content_at(messages, pair.result)
            members = [candidate for candidate in (call, result) if candidate is not None and candidate.provider_hosted]
            if not members:
                continue
            anchor = call if call is not None and call.provider_hosted else members[0]
            for member in members:
                degradations[id(member)] = None
            degradations[id(anchor)] = hosted_context_summary(call, result)
            representative = call or result
            if representative is None:
                raise RuntimeError("A hosted exchange requires a call or a result.")
            logger.debug(
                "Degrading provider-hosted history to assistant context: source=%s target=%s family=%s item_type=%s",
                source_provider,
                target_provider,
                _family_for(call or result),
                representative.provider_item_type,
            )
    return degradations


def _hosted_ops(
    *,
    occurrence: int,
    view: HostedToolView,
    order: ContentOrder,
    start: bool,
    result_content: bool,
) -> list[HostedToolOperation]:
    operations: list[HostedToolOperation] = []
    suborder = order[2]
    if start:
        operations.append(HostedToolStartOp(occurrence, "", view, (order[0], order[1], suborder)))
        suborder += 1
    if view.arguments and (start or result_content):
        # Some providers attach arguments only to the terminal item (OpenAI
        # web_search action), so result observations re-announce them; the
        # publish-side signature dedupes unchanged payloads.
        operations.append(HostedToolArgsOp(occurrence, "", view, (order[0], order[1], suborder)))
        suborder += 1
    status = normalize_hosted_tool_status(view.status)
    nonterminal_snapshot = (
        view.phase in {HostedToolPhase.DELTA, HostedToolPhase.SNAPSHOT} and status not in _TERMINAL_STATUSES
    )
    if result_content and nonterminal_snapshot:
        operations.append(HostedToolProgressOp(occurrence, "", view, (order[0], order[1], suborder)))
    elif result_content:
        operations.append(HostedToolResultOp(occurrence, "", view, (order[0], order[1], suborder)))
    elif view.phase in {HostedToolPhase.DELTA, HostedToolPhase.SNAPSHOT} and (view.result_text or view.image_contents):
        operations.append(HostedToolProgressOp(occurrence, "", view, (order[0], order[1], suborder)))
    elif status is not HostedToolStatus.UNKNOWN:
        operations.append(HostedToolStatusOp(occurrence, "", view, (order[0], order[1], suborder)))
    return operations


@dataclass(frozen=True)
class ResponsePresentationPlan:
    """Pure, exchange-aware presentation plan for an assembled response.

    ``operations`` present its hosted tool activity; ``final_text`` is the
    text after the last tool call or hosted output, the response's answer.
    """

    operations: list[HostedToolOperation]
    final_text: str
    structured_output_completed: bool = False

    @classmethod
    def from_messages(cls, messages: Sequence[Message]) -> ResponsePresentationPlan:
        """Plan hosted operations and find the final text without rebuilding any content."""
        has_hosted = any(
            _is_hosted_call(content) or _is_hosted_result(content)
            for message in messages
            for content in message.contents
        )
        if not has_hosted:
            final_text = ""
            if messages:
                final_text = "".join(
                    content.text or "" for content in messages[-1].contents if content.type == "text" and content.text
                )
            return cls([], final_text)

        pairs = _presentation_pairs(messages)
        operations: list[HostedToolOperation] = []
        occurrence_by_pair: dict[_Pair, int] = {}
        emitted_starts: set[int] = set()
        successful_structured_occurrences: set[int] = set()
        pending_text: list[Content] = []
        trailing_text_final = False
        next_occurrence = 0

        for message_index, message in enumerate(messages):
            for content_index, content in enumerate(message.contents):
                coordinate = (message_index, content_index)
                order = (message_index, content_index, 0)
                if content.type == "text":
                    if content.text:
                        pending_text.append(content)
                    continue
                if content.type == "function_call" and not content.informational_only:
                    pending_text.clear()
                    trailing_text_final = False
                    continue
                if content.type == "function_result" and not _is_hosted_result(content):
                    # A full-run transcript interleaves local results between
                    # responses; the loop resumed past this call, so text that
                    # follows is the model's post-tool answer, not preamble.
                    pending_text.clear()
                    trailing_text_final = True
                    continue

                pair = pairs.get(coordinate)
                if pair is None:
                    continue
                call = _content_at(messages, pair.call)
                result = _content_at(messages, pair.result)
                if call is not None and not _is_hosted_call(call):
                    continue
                if call is None and (result is None or not _is_hosted_result(result)):
                    continue
                occurrence = occurrence_by_pair.get(pair)
                if occurrence is None:
                    occurrence = next_occurrence
                    next_occurrence += 1
                    occurrence_by_pair[pair] = occurrence

                if _is_hosted_result(content):
                    pending_text.clear()
                    start = occurrence not in emitted_starts
                    view = adapt_hosted_tool(call, content)
                    operations.extend(
                        _hosted_ops(
                            occurrence=occurrence,
                            view=view,
                            order=order,
                            start=start,
                            result_content=True,
                        )
                    )
                    emitted_starts.add(occurrence)
                    status = normalize_hosted_tool_status(view.status)
                    if view.phase == HostedToolPhase.TERMINAL or status in _TERMINAL_STATUSES:
                        if _is_successful_structured_output(view, has_result=True):
                            successful_structured_occurrences.add(occurrence)
                        trailing_text_final = True
                    else:
                        trailing_text_final = False
                    continue

                if _is_hosted_call(content) and occurrence not in emitted_starts:
                    pending_text.clear()
                    view = adapt_hosted_tool(content)
                    operations.extend(
                        _hosted_ops(
                            occurrence=occurrence,
                            view=view,
                            order=order,
                            start=True,
                            result_content=False,
                        )
                    )
                    emitted_starts.add(occurrence)
                    has_later_result = pair.result is not None and pair.result != coordinate
                    status = normalize_hosted_tool_status(view.status)
                    terminal_call = not has_later_result and (
                        view.phase == HostedToolPhase.TERMINAL or status in _TERMINAL_STATUSES
                    )
                    if terminal_call:
                        if _is_successful_structured_output(view, has_result=False):
                            successful_structured_occurrences.add(occurrence)
                        trailing_text_final = True
                    else:
                        trailing_text_final = False

        final_text = "".join(content.text or "" for content in pending_text) if trailing_text_final else ""
        return cls(operations, final_text, bool(successful_structured_occurrences))


@dataclass(frozen=True, slots=True)
class FinalSegment:
    """A delegated agent's answer: the caller's result and the transcript's last text.

    The caller receives the response's final segment, as a main turn's answer
    is its last message: text written between tool calls and hosted outputs
    was shown as the model wrote it. A response whose final segment is empty
    (it ends on a tool result or hosted output) gives the caller everything
    it said instead; all of that was shown already, so ``transcript`` stays
    empty.
    """

    result: str
    transcript: str

    @classmethod
    def of(cls, response: AgentResponse[Any]) -> FinalSegment:
        final_text = ResponsePresentationPlan.from_messages(response.messages).final_text
        return cls(final_text or response.text, final_text)


# Model text in a landed response: its caller shows it once the response is ready.
_LANDED_TEXT_TYPES = frozenset({"text", "text_reasoning"})


@dataclass
class _LiveRecord:
    operation: HostedToolOperation | None = None
    local_call_id: str = ""
    local_call_content: Content | None = None
    text_content: Content | None = None
    released: bool = False


@dataclass
class _HostedOccurrence:
    ordinal: int
    presentation_id: str
    pairing_key: tuple[str, str]
    provider_item_id: str
    call_seen: bool = False
    result_seen: bool = False
    start_published: bool = False
    terminal: bool = False
    last_view: HostedToolView | None = None


@dataclass
class _PlanBarrier:
    call_id: str
    order: ContentOrder
    content: Content
    released: bool = False


_hosted_run_generations = itertools.count(1)


def next_hosted_run_generation() -> int:
    """Return a hosted pass generation that no other pass in this process has used.

    Presentation ids embed it, and frontends key tool cards by those ids. A
    counter per bindings object restarts on every engine rebuild and would
    hand a new card the id of an earlier one.
    """
    return next(_hosted_run_generations)


@dataclass
class HostedPresentationBridge:
    """Stateful streaming/blocking bridge that presents hosted tool activity.

    Model text is not its concern: the run's content projector shows it.
    Hosted operations that follow a local call in a response wait for that
    call's tool start, those that follow text in a landed response wait
    until the projector shows that text (:meth:`text_shown`), and the final
    message waits for every one of them.

    It handles one call at a time. The run, its parallel tool calls and an
    interrupt call it from their own tasks, and a call that publishes holds
    the next one: a frontend gets each card's events in order, and an
    attempt's state is never cleared under a drain.
    """

    publish: PresentationSink
    run_generation: int = 0
    response_index: int = 0
    attempt_index: int = field(default=-1, init=False)
    next_occurrence_ordinal: int = field(default=0, init=False)
    # Whether the hosted work being shown is a failed or rejected response's: the text before it never completed.
    shows_failed_attempt: bool = field(default=False, init=False)
    published_terminal_signatures: set[tuple[Any, ...]] = field(default_factory=set, init=False)
    _published_signatures: set[tuple[Any, ...]] = field(default_factory=set, init=False, repr=False)
    _live_records: list[_LiveRecord] = field(default_factory=list, init=False, repr=False)
    _live_cursor: int = field(default=0, init=False, repr=False)
    _occurrences: list[_HostedOccurrence] = field(default_factory=list, init=False, repr=False)
    _accepted_occurrences: list[_HostedOccurrence] = field(default_factory=list, init=False, repr=False)
    _open_occurrences: dict[tuple[str, str], deque[_HostedOccurrence]] = field(
        default_factory=lambda: defaultdict(deque), init=False, repr=False
    )
    _provider_item_candidates: dict[str, list[_HostedOccurrence]] = field(
        default_factory=lambda: defaultdict(list), init=False, repr=False
    )
    _started_local_call_contents: WeakIdentityRegistry = field(
        default_factory=WeakIdentityRegistry, init=False, repr=False
    )
    _accepted_barriers: list[_PlanBarrier] = field(default_factory=list, init=False, repr=False)
    _text_barriers: list[_PlanBarrier] = field(default_factory=list, init=False, repr=False)
    _unbound_local_start_counts: Counter[str] = field(default_factory=Counter, init=False, repr=False)
    _pending_reconciliation: deque[PresentationSinkOperation] = field(default_factory=deque, init=False, repr=False)
    _finalized_response_keys: set[tuple[int, int]] = field(default_factory=set, init=False, repr=False)
    _response_started: bool = field(default=False, init=False, repr=False)
    _serial: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def begin_response(self, *, response_index: int | None = None) -> None:
        """Begin another logical response without resetting run-monotonic ordinals."""
        async with self._serial:
            if response_index is not None:
                self.response_index = response_index
            elif self._response_started:
                self.response_index += 1
            self._response_started = True
            await self._release_text()
            # Provider-controlled call ids may repeat in a later response. Only
            # signals that could not yet bind to a concrete Content are scoped to
            # the current response; identity-bound starts survive for cumulative
            # final reconciliation. Continuation attempts do not call this method.
            self._unbound_local_start_counts.clear()
            self.attempt_index = -1
            self._clear_attempt_state()

    async def attempt_started(self, *, continuation: bool = False) -> None:
        """Open a validation attempt; continuation resumes the current occurrence set."""
        async with self._serial:
            await self._attempt_started(continuation=continuation)

    async def observe_contents(
        self, contents: Sequence[Content], *, is_final: bool = False, failed: bool = False
    ) -> None:
        """Observe one raw provider update or final content batch in content order.

        *failed* contents are the hosted work of a response that failed before it landed.
        """
        async with self._serial:
            self.shows_failed_attempt = failed
            try:
                await self._observe_contents(contents, is_final=is_final)
            finally:
                self.shows_failed_attempt = False

    async def _observe_contents(self, contents: Sequence[Content], *, is_final: bool) -> None:
        if self.attempt_index < 0:
            await self._attempt_started()
        for content in contents:
            if content.type == "function_call" and not content.informational_only:
                call_id = _provider_call_id(content)
                record = _LiveRecord(local_call_id=call_id, local_call_content=content)
                if self._unbound_local_start_counts[call_id] > 0:
                    record.released = True
                    self._started_local_call_contents.register(content)
                    self._unbound_local_start_counts[call_id] -= 1
                    if self._unbound_local_start_counts[call_id] == 0:
                        del self._unbound_local_start_counts[call_id]
                self._live_records.append(record)
                await self._drain_live_prefix()
                continue
            if _is_hosted_call(content):
                await self._observe_call(content)
            elif _is_hosted_result(content):
                await self._observe_result(content)
            elif is_final and content.type in _LANDED_TEXT_TYPES:
                self._live_records.append(_LiveRecord(text_content=content))

    async def local_call_start_published(self, provider_call_id: str) -> None:
        """Release the next local-call ordering barrier for a provider call id."""
        async with self._serial:
            bound = False
            for record in self._live_records:
                if record.local_call_id == provider_call_id and not record.released:
                    record.released = True
                    if record.local_call_content is not None:
                        self._started_local_call_contents.register(record.local_call_content)
                    bound = True
                    break
            for barrier in self._accepted_barriers:
                if barrier.call_id == provider_call_id and not barrier.released:
                    barrier.released = True
                    self._started_local_call_contents.register(barrier.content)
                    bound = True
                    break
            if not bound:
                self._unbound_local_start_counts[provider_call_id] += 1
            await self._drain_live_prefix()
            await self._drain_reconciliation()

    async def attempt_rejected(self, reason: str = "", *, status: str = HostedToolStatus.FAILED) -> None:
        """Terminalize visible cards and conclude all rejected-attempt buffers."""
        async with self._serial:
            self.shows_failed_attempt = True
            try:
                await self._attempt_rejected(reason, status)
            finally:
                self.shows_failed_attempt = False

    async def _attempt_rejected(self, reason: str, status: str) -> None:
        # The rejected response's text never shows and its local calls never
        # start: the hosted work it ran shows now.
        for record in self._live_records:
            record.released = True
        await self._release_text()
        terminal_status = (
            HostedToolStatus.INTERRUPTED if status == HostedToolStatus.INTERRUPTED else HostedToolStatus.FAILED
        )
        for occurrence in self._occurrences:
            if not occurrence.start_published or occurrence.terminal or occurrence.last_view is None:
                continue
            metadata = dict(occurrence.last_view.metadata)
            result_text = occurrence.last_view.result_text
            if terminal_status is HostedToolStatus.FAILED:
                metadata[TOOL_ERRORED_METADATA_KEY] = True
                metadata[TOOL_ERROR_MESSAGE_METADATA_KEY] = reason or "Response attempt rejected."
                result_text, failure_text_synthesized = _failure_text(reason, terminal_status)
                if failure_text_synthesized:
                    metadata[TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY] = True
            terminal_view = replace(
                occurrence.last_view,
                result_text=result_text,
                status=terminal_status,
                provider_status=terminal_status,
                phase=HostedToolPhase.TERMINAL,
                metadata=metadata,
            )
            operation = HostedToolStatusOp(
                occurrence.ordinal,
                occurrence.presentation_id,
                terminal_view,
                (0, len(self._live_records), 0),
            )
            await self._publish_once(operation)
            occurrence.terminal = True
        self._clear_attempt_state()

    async def attempt_accepted(self, messages: Sequence[Message] | None = None) -> None:
        """Mark validation accepted and reconcile its live-visible operations."""
        async with self._serial:
            for occurrence in self._occurrences:
                if occurrence not in self._accepted_occurrences:
                    self._accepted_occurrences.append(occurrence)
            if messages is not None:
                # A logical response inside agent.run() is not necessarily the
                # turn's final response: local function calls can continue the
                # loop. Publish its canonical tool ordering now, but leave the
                # terminal FinalTextOp to the run-completion reconciliation.
                await self._reconcile_accepted(
                    messages,
                    final=False,
                    publish_final=False,
                    occurrence_candidates=tuple(self._occurrences),
                )

    async def text_shown(self, content: Content | None = None) -> None:
        """The landed *content*, or every landed text, went out: the hosted operations after it follow."""
        async with self._serial:
            await self._release_text(content)

    async def reconcile_accepted(
        self,
        messages: Sequence[Message],
        *,
        final: bool = False,
        publish_final: bool = True,
    ) -> None:
        """Reconcile live events against assembled content and emit final text last.

        ``final`` marks the run-completion reconcile: no further local tool
        starts can arrive after it. ``publish_final`` is false while accepting
        one non-terminal response inside the tool loop.
        """
        async with self._serial:
            accepted_ordinals = {occurrence.ordinal for occurrence in self._accepted_occurrences}
            occurrence_candidates = (
                *self._accepted_occurrences,
                *(occurrence for occurrence in self._occurrences if occurrence.ordinal not in accepted_ordinals),
            )
            await self._reconcile_accepted(
                messages,
                final=final,
                publish_final=publish_final,
                occurrence_candidates=occurrence_candidates,
            )

    async def _attempt_started(self, *, continuation: bool = False) -> None:
        self._response_started = True
        # Text the last attempt landed was shown by now, or never will be. A
        # background response's text shows only with the poll that finishes
        # it: the hosted work an earlier poll saw shows meanwhile.
        await self._release_text()
        if continuation and self.attempt_index >= 0:
            return
        self.attempt_index += 1
        self._clear_attempt_state()

    async def _reconcile_accepted(
        self,
        messages: Sequence[Message],
        *,
        final: bool,
        publish_final: bool,
        occurrence_candidates: Sequence[_HostedOccurrence],
    ) -> None:
        response_key = (self.attempt_index, self.response_index)
        if publish_final and response_key in self._finalized_response_keys:
            return
        plan = ResponsePresentationPlan.from_messages(messages)
        mapped_occurrences: dict[int, _HostedOccurrence] = {}
        used_ordinals: set[int] = set()
        reconciled: list[PresentationSinkOperation] = []
        for operation in plan.operations:
            occurrence = mapped_occurrences.get(operation.occurrence_ordinal)
            if occurrence is None:
                occurrence = self._match_reconciled_occurrence(
                    operation.view,
                    used_ordinals,
                    occurrence_candidates,
                )
                mapped_occurrences[operation.occurrence_ordinal] = occurrence
                used_ordinals.add(occurrence.ordinal)
            reconciled.append(
                replace(
                    operation,
                    occurrence_ordinal=occurrence.ordinal,
                    presentation_id=occurrence.presentation_id,
                )
            )

        self._accepted_barriers = self._plan_barriers(messages)
        # Only an attempt's acceptance comes before its response's text shows; a run's reconcile comes after.
        self._text_barriers = [] if final or publish_final else self._text_plan_barriers(messages)
        for barrier in self._accepted_barriers:
            if barrier.content in self._started_local_call_contents:
                barrier.released = True
                continue
            if self._unbound_local_start_counts[barrier.call_id] > 0:
                barrier.released = True
                self._started_local_call_contents.register(barrier.content)
                self._unbound_local_start_counts[barrier.call_id] -= 1
                if self._unbound_local_start_counts[barrier.call_id] == 0:
                    del self._unbound_local_start_counts[barrier.call_id]
        if final:
            # Calls that never entered the tool pipeline (unknown tool,
            # pre-pipeline argument rejection) publish no start; once the run
            # is over their barriers can only gate FinalTextOp forever.
            for barrier in self._accepted_barriers:
                barrier.released = True
        pending: list[PresentationSinkOperation] = list(reconciled)
        if publish_final:
            pending.append(
                FinalTextOp(
                    plan.final_text,
                    structured_output_completed=not plan.final_text.strip() and plan.structured_output_completed,
                )
            )
            self._finalized_response_keys.add(response_key)
        self._pending_reconciliation = deque(pending)
        await self._drain_reconciliation()

    async def _observe_call(self, content: Content) -> None:
        view = adapt_hosted_tool(content)
        occurrence = self._call_occurrence(content, view)
        if occurrence.terminal and not occurrence.call_seen:
            occurrence.call_seen = True
            if occurrence.last_view is not None:
                occurrence.last_view = replace(
                    occurrence.last_view,
                    tool_name=view.tool_name,
                    display_title=view.display_title,
                    arguments=view.arguments,
                )
            return
        occurrence.call_seen = True
        occurrence.last_view = view
        operations = _hosted_ops(
            occurrence=occurrence.ordinal,
            view=view,
            order=(0, len(self._live_records), 0),
            start=not self._has_start_record(occurrence),
            result_content=False,
        )
        for operation in operations:
            await self._append_live(replace(operation, presentation_id=occurrence.presentation_id))
        status = normalize_hosted_tool_status(view.status)
        if view.phase == HostedToolPhase.TERMINAL or status in _TERMINAL_STATUSES:
            occurrence.terminal = True

    async def _observe_result(self, content: Content) -> None:
        key = self._pairing_key(content)
        provider_item_id = content.provider_item_id or ""
        occurrence = (
            self._provider_item_candidates[provider_item_id][-1]
            if provider_item_id and self._provider_item_candidates[provider_item_id]
            else next(
                (candidate for candidate in reversed(self._open_occurrences[key]) if not candidate.result_seen),
                None,
            )
        )
        if occurrence is None:
            view = adapt_hosted_tool(None, content)
            occurrence = self._allocate_occurrence(view, pairing_key=key)
        view = adapt_hosted_tool(None, content)
        if occurrence.call_seen:
            view = replace(
                view,
                tool_name=occurrence.last_view.tool_name if occurrence.last_view is not None else view.tool_name,
                display_title=(
                    occurrence.last_view.display_title if occurrence.last_view is not None else view.display_title
                ),
                arguments=occurrence.last_view.arguments if occurrence.last_view is not None else view.arguments,
            )
        occurrence.result_seen = True
        occurrence.last_view = view
        operations = _hosted_ops(
            occurrence=occurrence.ordinal,
            view=view,
            order=(0, len(self._live_records), 0),
            start=not self._has_start_record(occurrence),
            result_content=True,
        )
        for operation in operations:
            await self._append_live(replace(operation, presentation_id=occurrence.presentation_id))
        status = normalize_hosted_tool_status(view.status)
        if view.phase == HostedToolPhase.TERMINAL or status in _TERMINAL_STATUSES:
            occurrence.terminal = True

    def _call_occurrence(self, content: Content, view: HostedToolView) -> _HostedOccurrence:
        provider_item_id = content.provider_item_id or ""
        if provider_item_id and self._provider_item_candidates[provider_item_id]:
            return self._provider_item_candidates[provider_item_id][-1]
        key = self._pairing_key(content)
        for occurrence in reversed(self._open_occurrences[key]):
            if not occurrence.call_seen or not occurrence.result_seen:
                return occurrence
        return self._allocate_occurrence(view, pairing_key=key)

    def _allocate_occurrence(
        self,
        view: HostedToolView,
        *,
        pairing_key: tuple[str, str] | None = None,
    ) -> _HostedOccurrence:
        ordinal = self.next_occurrence_ordinal
        self.next_occurrence_ordinal += 1
        occurrence = _HostedOccurrence(
            ordinal=ordinal,
            presentation_id=(
                f"hosted:{self.run_generation}:{max(self.attempt_index, 0)}:{self.response_index}:{ordinal}"
            ),
            pairing_key=pairing_key or ("call_id", view.provider_call_id),
            provider_item_id=str(view.metadata.get("provider_item_id", "")),
            last_view=view,
        )
        self._occurrences.append(occurrence)
        self._open_occurrences[occurrence.pairing_key].append(occurrence)
        if occurrence.provider_item_id:
            self._provider_item_candidates[occurrence.provider_item_id].append(occurrence)
        return occurrence

    def _match_reconciled_occurrence(
        self,
        view: HostedToolView,
        used_ordinals: set[int],
        candidates: Sequence[_HostedOccurrence],
    ) -> _HostedOccurrence:
        provider_item_id = str(view.metadata.get("provider_item_id", ""))
        if provider_item_id:
            for occurrence in candidates:
                if occurrence.provider_item_id != provider_item_id:
                    continue
                if occurrence.ordinal not in used_ordinals:
                    return occurrence
        for occurrence in candidates:
            if occurrence.ordinal in used_ordinals:
                continue
            if occurrence.pairing_key == ("call_id", view.provider_call_id):
                return occurrence
            if occurrence.last_view is not None and (
                occurrence.last_view.family,
                occurrence.last_view.tool_name,
            ) == (view.family, view.tool_name):
                return occurrence
        return self._allocate_occurrence(view)

    def _pairing_key(self, content: Content) -> tuple[str, str]:
        if content.type.startswith("image_generation_"):
            return "image_id", content.image_id or ""
        return "call_id", content.call_id or ""

    def _has_start_record(self, occurrence: _HostedOccurrence) -> bool:
        return any(
            isinstance(record.operation, HostedToolStartOp)
            and record.operation.occurrence_ordinal == occurrence.ordinal
            for record in self._live_records
        )

    async def _append_live(self, operation: HostedToolOperation) -> None:
        self._live_records.append(_LiveRecord(operation=operation))
        await self._drain_live_prefix()

    async def _drain_live_prefix(self) -> None:
        while self._live_cursor < len(self._live_records):
            record = self._live_records[self._live_cursor]
            if record.local_call_id or record.text_content is not None:
                if not record.released:
                    return
                self._live_cursor += 1
                continue
            if record.operation is None:
                self._live_cursor += 1
                continue
            await self._publish_once(record.operation)
            if isinstance(record.operation, HostedToolStartOp):
                occurrence = self._occurrence_by_ordinal(record.operation.occurrence_ordinal)
                occurrence.start_published = True
            self._live_cursor += 1

    async def _drain_reconciliation(self) -> None:
        while self._pending_reconciliation:
            operation = self._pending_reconciliation[0]
            barriers = (*self._accepted_barriers, *self._text_barriers)
            if isinstance(operation, FinalTextOp):
                if any(not barrier.released for barrier in barriers):
                    return
            elif any(not barrier.released and barrier.order < operation.order for barrier in barriers):
                return
            self._pending_reconciliation.popleft()
            await self._publish_once(operation)

    async def _release_text(self, content: Content | None = None) -> None:
        """Release the text barriers of *content*, or all of them, and publish what waited."""
        for record in self._live_records:
            if record.text_content is not None and (content is None or record.text_content is content):
                record.released = True
        for barrier in self._text_barriers:
            if content is None or barrier.content is content:
                barrier.released = True
        await self._drain_live_prefix()
        await self._drain_reconciliation()

    @staticmethod
    def _text_plan_barriers(messages: Sequence[Message]) -> list[_PlanBarrier]:
        return [
            _PlanBarrier("", (message_index, content_index, 0), content)
            for message_index, message in enumerate(messages)
            for content_index, content in enumerate(message.contents)
            if content.type in _LANDED_TEXT_TYPES
        ]

    def _plan_barriers(self, messages: Sequence[Message]) -> list[_PlanBarrier]:
        barriers: list[_PlanBarrier] = []
        for message_index, message in enumerate(messages):
            for content_index, content in enumerate(message.contents):
                if content.type == "function_call" and not content.informational_only:
                    barriers.append(
                        _PlanBarrier(
                            _provider_call_id(content),
                            (message_index, content_index, 0),
                            content,
                        )
                    )
        return barriers

    def _occurrence_by_ordinal(self, ordinal: int) -> _HostedOccurrence:
        return next(occurrence for occurrence in self._occurrences if occurrence.ordinal == ordinal)

    async def _publish_once(self, operation: PresentationSinkOperation) -> None:
        signature = self._signature(operation)
        if signature in self._published_signatures:
            return
        self._published_signatures.add(signature)
        if isinstance(operation, HostedToolResultOp) or (
            isinstance(operation, HostedToolStatusOp)
            and normalize_hosted_tool_status(operation.view.status) in _TERMINAL_STATUSES
        ):
            self.published_terminal_signatures.add(signature)
        await self._publish_sink(operation)

    async def _publish_sink(self, operation: PresentationSinkOperation) -> None:
        outcome = self.publish(operation)
        if inspect.isawaitable(outcome):
            await outcome

    def _signature(self, operation: PresentationSinkOperation) -> tuple[Any, ...]:
        if isinstance(operation, FinalTextOp):
            return (
                "final",
                self.attempt_index,
                self.response_index,
                operation.text,
                operation.structured_output_completed,
            )
        view = operation.view
        base = (type(operation).__name__, operation.presentation_id)
        if isinstance(operation, HostedToolStartOp):
            return base
        if isinstance(operation, HostedToolArgsOp):
            return (*base, json.dumps(_json_safe(view.arguments), sort_keys=True, ensure_ascii=False))
        if isinstance(operation, HostedToolProgressOp):
            images = tuple((content.uri, content.media_type, content.file_id) for content in view.image_contents)
            return (*base, view.phase, view.provider_status, view.result_text, images)
        if isinstance(operation, HostedToolStatusOp):
            return (*base, view.status, view.provider_status)
        artifacts = tuple((content.file_id, content.uri, content.media_type) for content in view.artifacts)
        images = tuple((content.uri, content.media_type, content.file_id) for content in view.image_contents)
        return (*base, view.status, view.provider_status, view.result_text, images, artifacts)

    def _clear_attempt_state(self) -> None:
        self._live_records.clear()
        self._live_cursor = 0
        self._occurrences.clear()
        self._open_occurrences.clear()
        self._provider_item_candidates.clear()
        self._accepted_barriers.clear()
        self._text_barriers.clear()
        self._pending_reconciliation.clear()


__all__ = [
    "CodeHostedAdapter",
    "FinalSegment",
    "FinalTextOp",
    "GenericHostedAdapter",
    "HostedPresentationBridge",
    "HostedToolArgsOp",
    "HostedToolProgressOp",
    "HostedToolResultOp",
    "HostedToolStartOp",
    "HostedToolStatusOp",
    "HostedToolView",
    "ImageHostedAdapter",
    "McpHostedAdapter",
    "PresentationSinkOperation",
    "ResponsePresentationPlan",
    "SearchHostedAdapter",
    "ShellHostedAdapter",
    "adapt_hosted_tool",
    "cross_provider_hosted_degradations",
    "get_hosted_adapter",
    "hosted_context_summary",
    "hosted_replay_status",
    "is_legacy_server_executed_informational_call",
    "next_hosted_run_generation",
    "normalize_hosted_replay_content",
]
