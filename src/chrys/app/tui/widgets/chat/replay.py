# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Replay planning for persisted chat transcript history."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from chrys.app.tui.widgets.chat.context_fold import ContextFoldWidget
from chrys.app.tui.widgets.chat.messages import (
    INTERRUPTED_CONTINUE_ACTION_MESSAGE,
    INTERRUPTED_RETRY_ACTION_MESSAGE,
    AgentMessage,
    InterruptedMessage,
    ThinkingMessage,
    UserMessage,
    format_message_created_at,
    resolve_interrupted_message_copy,
    turn_duration_ms,
)
from chrys.app.tui.widgets.chat.ports import ChatMountPort, TranscriptLocalizationPort, TranscriptUiPort
from chrys.app.tui.widgets.chat.toc_model import TurnTocModel, turn_index_from_extra
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.foundation.hosted_tools import (
    HOSTED_TOOL_DEFAULT_KIND_BY_FAMILY,
    HostedToolFamily,
)
from chrys.foundation.i18n import MessageDef, MessageRef
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_block
from chrys.foundation.models.history_markers import (
    ANSWER_SEGMENT_KEY,
    AWAITING_SUB_AGENTS_MESSAGE,
    EXECUTION_FAILED_MESSAGE,
    EXECUTION_INTERRUPTED_MESSAGE,
    SESSION_CLOSED_MESSAGE,
    SUB_AGENT_STATE_DISCARDED_MESSAGE,
    HistoryMarkerKind,
)
from chrys.foundation.models.turns import opens_turn
from chrys.foundation.text.images import is_image_media_type
from chrys.foundation.tool_result_metadata import TOOL_INTERRUPTED_METADATA_KEY
from chrys.foundation.trajectory_timing import trajectory_timing_from_metadata
from chrys.foundation.util.time import parse_created_at
from chrys.kernel import Content, Message
from chrys.kernel.exchanges import (
    LEGACY_CALL_CONTENT_TYPE,
    TOOL_CALL_CONTENT_TYPES,
    TOOL_RESULT_CONTENT_TYPES,
    DictAccessor,
    EmptyIdPolicy,
    NoneIdPolicy,
    PairingPolicy,
    iter_exchanges,
    pair_results,
)
from chrys.service.agent_middleware.events.hosted_tools import (
    HostedToolView,
    ResponsePresentationPlan,
    _is_hosted_call,
    _is_hosted_result,
    adapt_hosted_tool,
    hosted_replay_status,
    is_legacy_server_executed_informational_call,
    normalize_hosted_replay_content,
)
from chrys.service.agent_middleware.events.result_persistence import PERSISTED_RESULT_METADATA_KEYS
from chrys.service.context.providers.history import TURN_STARTED_AT_KEY
from chrys.service.llm.reasoning_display import readable_reasoning
from chrys.service.mutations.tool_names import _FILE_TOOLS
from chrys.service.session.message_metadata import (
    MESSAGE_CREATED_AT_KEY,
    TOOL_RESULT_METADATA_KEY,
    is_compaction_tool_summary,
    persisted_tool_call_kind,
)
from chrys.service.text_blocks import join_text_blocks, reconstruct_text_blocks, text_block_id

if TYPE_CHECKING:
    from textual.widget import Widget

    from chrys.foundation.models.invocations import ContentChannel
    from chrys.service.context.providers.history import CompressedBlock

# Replay renders every function-style call it can display: legacy serialized calls
# count as calls, informational calls still get cards, and idless calls/results pair
# positionally within their exchange (None-id and empty-id queues kept separate).
# Non-string ids are stringified so lookups match the str-normalized `_call_id` view.
_REPLAY_PAIRING_POLICY = PairingPolicy(
    call_types=TOOL_CALL_CONTENT_TYPES | {LEGACY_CALL_CONTENT_TYPE},
    include_informational_calls=True,
    result_types=TOOL_RESULT_CONTENT_TYPES,
    none_id=NoneIdPolicy.POSITIONAL,
    empty_id=EmptyIdPolicy.POSITIONAL,
    malformed_id="stringify",
)
_REPLAY_ONLY_CONTENT_KEYS = {
    "_file_snapshot",
    "_replay_approval",
    "_replay_artifacts",
    "_replay_image_contents",
    "_replay_metadata",
    "_replay_result",
}
_STATUS_MESSAGE_DEFINITIONS: dict[str, MessageDef] = {
    HistoryMarkerKind.STATUS_EXECUTION_INTERRUPTED: EXECUTION_INTERRUPTED_MESSAGE,
    HistoryMarkerKind.STATUS_EXECUTION_FAILED: EXECUTION_FAILED_MESSAGE,
    HistoryMarkerKind.STATUS_SESSION_CLOSED: SESSION_CLOSED_MESSAGE,
    HistoryMarkerKind.STATUS_SUB_AGENT_STATE_DISCARDED: SUB_AGENT_STATE_DISCARDED_MESSAGE,
    HistoryMarkerKind.STATUS_AWAITING_SUB_AGENTS: AWAITING_SUB_AGENTS_MESSAGE,
}


@dataclass(frozen=True)
class ReplayCompatibilityAnnotation:
    """Legacy mutation that must still be applied to the caller's raw message dicts."""

    target: dict[str, Any]
    values: dict[str, Any]

    def apply(self) -> None:
        self.target.update(self.values)


@dataclass(frozen=True)
class ReplayToolCall:
    """A planned tool invocation with locally paired replay data."""

    source: dict[str, Any]
    name: str
    call_id: str
    tool_kind: str = ""
    arguments: Any = None
    result: Any = ""
    metadata: dict[str, Any] | None = None
    image_contents: list[dict[str, Any]] | None = None
    file_snapshot: Any = None
    approval: dict[str, Any] | None = None
    provider_hosted: bool = False
    hosted_family: str = ""
    provider: str = ""
    provider_item_type: str = ""
    provider_status: str = ""
    provider_call_id: str = ""
    canonical_status: str = "completed"
    artifacts: list[dict[str, Any]] | None = None
    duration_ms: int | None = None
    started_at: str | None = None


@dataclass(frozen=True)
class ReplayTurnMarker:
    turn_index: int | None
    started_at: Any = None
    """When the pass the marker ends began, which the answer before it is timed from."""


@dataclass(frozen=True)
class ReplayUserMessage:
    text: str
    contents: list[Any]
    created_at: Any = None
    is_injection: bool = False
    profile_switch_to: str = ""
    duration_ms: int | None = None


@dataclass(frozen=True)
class ReplayProfileSwitch:
    profile_name: str


@dataclass(frozen=True)
class ReplayAgentMessage:
    contents: list[Any]
    created_at: Any = None
    is_intermediate: bool = False
    intermediate_texts: list[str] = field(default_factory=list)
    status_code: str | None = None
    status_count: int | None = None
    duration_ms: int | None = None
    is_status: bool = False


@dataclass(frozen=True)
class ReplayStructuredCompletion:
    """The ✓ of a turn that ended with successful structured hosted output and no answer text.

    It carries when the agent message whose output it completes finished, and
    how long its turn took, and follows the thinking after it, as it did live.
    """

    created_at: Any = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class ReplayContextFold:
    context_id: str
    summary: str


@dataclass(frozen=True)
class ReplayInterrupted:
    reason: str
    source: str = "user"
    action_label: MessageDef | None = None
    status_code: str | None = None
    status_count: int | None = None


@dataclass(frozen=True)
class ReplaySuppressedStatus:
    """An unsuccessful terminal status whose prose is omitted from compressed history."""


ReplayEntry = (
    ReplayTurnMarker
    | ReplayUserMessage
    | ReplayProfileSwitch
    | ReplayAgentMessage
    | ReplayStructuredCompletion
    | ReplayContextFold
    | ReplayInterrupted
    | ReplaySuppressedStatus
)


@dataclass(frozen=True)
class ReplayPlan:
    """Ordered replay operations plus compatibility annotations."""

    entries: list[ReplayEntry]
    annotations: list[ReplayCompatibilityAnnotation]

    def apply_compatibility_annotations(self) -> None:
        for annotation in self.annotations:
            annotation.apply()


@dataclass
class _PairedToolData:
    result: Any = ""
    metadata: dict[str, Any] | None = None
    image_contents: list[dict[str, Any]] | None = None
    file_snapshot: Any = None
    hosted_view: HostedToolView | None = None
    artifacts: list[dict[str, Any]] | None = None
    has_result: bool = False
    duration_ms: int | None = None
    started_at: str | None = None


@dataclass
class _MergedAssistantEntry:
    contents: list[Any]
    additional_properties: dict[str, Any]
    batch_id: Any = None
    is_intermediate: bool = False
    intermediate_texts: list[str] = field(default_factory=list)
    has_preceding_text: bool = False
    merged_role: str = ""


@dataclass
class _RawEntry:
    message: dict[str, Any]


MergedEntry = _MergedAssistantEntry | _RawEntry


class HistoryReplayPlanner:
    """Transform persisted raw messages into replay operations without mounting widgets."""

    def build_plan(
        self,
        messages: list[dict[str, Any]],
        *,
        file_snapshots: dict[str, list[Any]] | None = None,
        suppress_marker_text: bool = False,
        show_reasoning: bool = False,
    ) -> ReplayPlan:
        """Plan the replay of *messages*; with *show_reasoning*, readable thinking shows, and splits tool groups as live."""
        # Compaction tool summaries are the model's context and never render.
        # Drop them before pairing and merging, so a hidden summary cannot
        # split or join the tool groups around it.
        messages = [message for message in messages if not is_compaction_tool_summary(message)]
        pairings, standalone_hosted_results, annotations = self._pair_tool_calls(
            messages,
            file_snapshots=file_snapshots,
        )
        merged = self._merge_assistant_entries(
            messages, pairings, standalone_hosted_results, annotations, show_reasoning=show_reasoning
        )
        entries = self._build_entries(merged, suppress_marker_text=suppress_marker_text, show_reasoning=show_reasoning)
        return ReplayPlan(entries=entries, annotations=annotations)

    def _pair_tool_calls(
        self,
        messages: list[dict[str, Any]],
        *,
        file_snapshots: dict[str, list[Any]] | None,
    ) -> tuple[dict[int, _PairedToolData], dict[int, ReplayToolCall], list[ReplayCompatibilityAnnotation]]:
        pairings: dict[int, _PairedToolData] = {}
        standalone_result_coordinates: set[tuple[int, int]] = set()
        annotations: list[ReplayCompatibilityAnnotation] = []
        snapshot_cursors: dict[str, int] = {}

        # Exchange-scoped pairing over the serialized transcript: every
        # response sibling shares the exchange's output block, embedded
        # results answer their own message's calls, markers are hard
        # boundaries, and duplicate ids consume one shared per-key cursor.
        accessor = DictAccessor()
        result_by_call: dict[tuple[int, int], dict[str, Any] | None] = {}
        paired_result_coordinates: set[tuple[int, int]] = set()
        for exchange in iter_exchanges(messages, accessor):
            pairing = pair_results(messages, exchange, accessor, _REPLAY_PAIRING_POLICY)
            for slots in (*pairing.truthy_assignments.values(), *pairing.falsy_assignments.values()):
                for call, result in slots:
                    result_content: dict[str, Any] | None = None
                    if result is not None:
                        paired_result_coordinates.add((result.message_index, result.content_index))
                        candidate = _message_contents(messages[result.message_index])[result.content_index]
                        result_content = candidate if isinstance(candidate, dict) else None
                    result_by_call[(call.message_index, call.content_index)] = result_content
            unconsumed_results = [
                result
                for results in (*pairing.unconsumed_results.values(), *pairing.falsy_unconsumed_results.values())
                for result in results
            ]
            for result in [*unconsumed_results, *pairing.unpairable_results]:
                coordinate = (result.message_index, result.content_index)
                candidate = _message_contents(messages[result.message_index])[result.content_index]
                if isinstance(candidate, dict) and _is_hosted_tool_result(candidate):
                    standalone_result_coordinates.add(coordinate)

        # ``iter_exchanges`` deliberately starts only at a call-bearing
        # assistant run, so a completely standalone output belongs to no
        # exchange. Pairing remains exchange-scoped above; this pass only
        # inventories canonical hosted result occurrences that the pairing
        # authority did not consume, without attempting any id matching.
        for message_index, message in enumerate(messages):
            if message.get("role", "") not in {"assistant", "tool"}:
                continue
            if HistoryMarkerKind.KEY in _message_extra(message):
                continue
            for content_index, content in enumerate(_message_contents(message)):
                coordinate = (message_index, content_index)
                if (
                    coordinate not in paired_result_coordinates
                    and isinstance(content, dict)
                    and _is_hosted_tool_result(content)
                ):
                    standalone_result_coordinates.add(coordinate)

        for index, msg in enumerate(messages):
            if msg.get("role", "") != "assistant":
                continue
            if HistoryMarkerKind.KEY in _message_extra(msg):
                # Marker payloads are chrome: they never render as tools, so
                # they must not consume snapshot cursors that belong to the
                # visible calls around them.
                continue
            indexed_tool_calls = [
                (content_index, content)
                for content_index, content in enumerate(_message_contents(msg))
                if _is_tool_call(content)
            ]
            if not indexed_tool_calls:
                continue

            for content_index, tool_call in indexed_tool_calls:
                call_id = _call_id(tool_call)
                result_content = result_by_call.get((index, content_index))

                paired = _PairedToolData()
                timing = _content_timing(result_content) or _content_timing(tool_call)
                if timing is not None:
                    paired.duration_ms = timing["duration_ms"]
                    paired.started_at = timing["started_at"]
                values: dict[str, Any] = {}
                if _is_hosted_tool_call(tool_call):
                    call = _hosted_content_for_replay(tool_call)
                    result = _content_for_replay(result_content) if isinstance(result_content, dict) else None
                    paired.has_result = result is not None
                    paired.hosted_view = adapt_hosted_tool(call, result)
                    paired.result = paired.hosted_view.result_text
                    persisted_metadata = _replay_tool_metadata(result_content) if result_content is not None else None
                    paired.metadata = {
                        **(persisted_metadata or {}),
                        **paired.hosted_view.metadata,
                    } or None
                    paired.image_contents = [content.to_dict() for content in paired.hosted_view.image_contents] or None
                    paired.artifacts = _hosted_artifact_descriptors(paired.hosted_view) or None
                elif isinstance(result_content, dict):
                    paired.result = result_content.get("result", "")
                    paired.metadata = _replay_tool_metadata(result_content)
                    images = _replay_tool_images(result_content)
                    paired.image_contents = images or None
                values["_replay_result"] = paired.result
                if paired.metadata is not None:
                    values["_replay_metadata"] = paired.metadata
                if paired.image_contents:
                    values["_replay_image_contents"] = paired.image_contents
                if paired.artifacts:
                    values["_replay_artifacts"] = paired.artifacts

                if tool_call.get("name", "") in _FILE_TOOLS:
                    snapshots = file_snapshots.get(call_id, []) if file_snapshots else []
                    snapshot_index = snapshot_cursors.get(call_id, 0)
                    if snapshot_index < len(snapshots):
                        paired.file_snapshot = snapshots[snapshot_index]
                        values["_file_snapshot"] = paired.file_snapshot
                        snapshot_cursors[call_id] = snapshot_index + 1

                pairings[id(tool_call)] = paired
                annotations.append(ReplayCompatibilityAnnotation(tool_call, values))

        standalone_hosted_results: dict[int, ReplayToolCall] = {}
        for message_index, message in enumerate(messages):
            for content_index, content in enumerate(_message_contents(message)):
                if (message_index, content_index) not in standalone_result_coordinates:
                    continue
                standalone_hosted_results[id(content)] = self._standalone_hosted_replay_tool(
                    content,
                    message_index=message_index,
                    content_index=content_index,
                )

        return pairings, standalone_hosted_results, annotations

    def _merge_assistant_entries(
        self,
        messages: list[dict[str, Any]],
        pairings: dict[int, _PairedToolData],
        standalone_hosted_results: dict[int, ReplayToolCall],
        annotations: list[ReplayCompatibilityAnnotation],
        *,
        show_reasoning: bool,
    ) -> list[MergedEntry]:
        merged: list[MergedEntry] = []
        legacy_duplicate_sidecars = _legacy_duplicate_intermediate_sidecars(messages)
        for message_index, msg in enumerate(messages):
            role = msg.get("role", "")
            if role not in {"assistant", "tool"}:
                merged.append(_RawEntry(msg))
                continue

            contents = _message_contents(msg)
            extra = _message_extra(msg)
            if HistoryMarkerKind.KEY in extra:
                # Key presence, matching the grammar's boundary rule: a
                # falsy kind is still chrome, never tool activity.
                merged.append(_RawEntry(msg))
                continue

            tool_part = [
                content
                for content in contents
                if (role == "assistant" and _is_tool_call(content)) or id(content) in standalone_hosted_results
            ]

            if role == "tool" and not tool_part:
                continue

            has_text = _has_visible_text(contents)
            embedded_itext = extra.get("_intermediate_text")
            # _intermediate_text is a replay sidecar for intermediate text
            # already shown before tool execution.  Normally suppress it only
            # when the same text is present in this message; the precomputed
            # compatibility set also covers old cross-batch misattribution.
            pending_embedded_itext = (
                None
                if message_index in legacy_duplicate_sidecars or _intermediate_text_is_visible(contents, embedded_itext)
                else embedded_itext
            )

            if not tool_part:
                if has_text:
                    merged.append(
                        _MergedAssistantEntry(
                            contents=list(contents),
                            additional_properties=extra,
                        )
                    )
                else:
                    merged.append(_RawEntry(msg))
                continue

            call_replay_tools = iter(
                self._build_replay_tool_calls(
                    [content for content in tool_part if id(content) not in standalone_hosted_results],
                    extra,
                    pairings,
                    annotations,
                )
            )
            replay_tools = iter(
                [
                    standalone_hosted_results[id(content)]
                    if id(content) in standalone_hosted_results
                    else next(call_replay_tools)
                    for content in tool_part
                ]
            )
            pending_text: list[Any] = []
            pending_tools: list[ReplayToolCall] = []
            text_before_next_tool = False

            for content in contents:
                if _is_tool_call(content) or id(content) in standalone_hosted_results:
                    if self._append_text_segment(
                        merged, pending_text, extra, is_intermediate=True, show_reasoning=show_reasoning
                    ):
                        # Thinking shown before the tools ends the tool group above, as it did live.
                        text_before_next_tool = True
                    pending_text.clear()
                    pending_tools.append(next(replay_tools))
                else:
                    if pending_tools:
                        self._append_replay_tool_group(
                            merged,
                            list(pending_tools),
                            extra,
                            pending_embedded_itext,
                            has_preceding_text=text_before_next_tool,
                        )
                        if pending_embedded_itext:
                            pending_embedded_itext = None
                        pending_tools.clear()
                        text_before_next_tool = False
                    pending_text.append(content)

            if pending_tools:
                self._append_replay_tool_group(
                    merged,
                    list(pending_tools),
                    extra,
                    pending_embedded_itext,
                    has_preceding_text=text_before_next_tool,
                )
            trailing_text_is_final = bool(pending_text) and _hosted_trailing_text_is_final(contents)
            self._append_text_segment(
                merged,
                pending_text,
                extra,
                is_intermediate=not trailing_text_is_final,
                show_reasoning=show_reasoning,
            )

        return self._coalesce_tool_groups(merged)

    @staticmethod
    def _standalone_hosted_replay_tool(
        content: dict[str, Any],
        *,
        message_index: int,
        content_index: int,
    ) -> ReplayToolCall:
        """Build one synthetic card from an unpaired canonical hosted result."""
        result = _content_for_replay(content)
        view = adapt_hosted_tool(None, result)
        canonical_status = hosted_replay_status(view, has_result=True)
        persisted_metadata = _replay_tool_metadata(content)
        metadata = {**(persisted_metadata or {}), **view.metadata} or None
        call_id = _call_id(content) or f"hosted-result:{message_index}:{content_index}"
        return ReplayToolCall(
            source=content,
            name=view.tool_name or view.display_title or "hosted_tool",
            call_id=call_id,
            tool_kind=HOSTED_TOOL_DEFAULT_KIND_BY_FAMILY.get(view.family, ""),
            arguments=view.arguments,
            result=view.result_text or ("interrupted" if canonical_status == "interrupted" else ""),
            metadata=metadata,
            image_contents=[item.to_dict() for item in view.image_contents] or None,
            provider_hosted=True,
            hosted_family=view.family,
            provider=view.provider,
            provider_item_type=view.provider_item_type,
            provider_status=canonical_status if canonical_status == "interrupted" else view.provider_status,
            provider_call_id=view.provider_call_id,
            canonical_status=canonical_status,
            artifacts=_hosted_artifact_descriptors(view) or None,
            duration_ms=_content_duration_ms(content),
            started_at=_content_started_at(content),
        )

    @staticmethod
    def _append_text_segment(
        merged: list[MergedEntry],
        contents: list[Any],
        extra: dict[str, Any],
        *,
        is_intermediate: bool,
        show_reasoning: bool,
    ) -> bool:
        if not (_has_visible_text(contents) or (show_reasoning and _shows_thinking(contents))):
            return False
        merged.append(
            _MergedAssistantEntry(
                contents=list(contents),
                additional_properties=extra,
                is_intermediate=is_intermediate,
            )
        )
        return True

    @staticmethod
    def _append_replay_tool_group(
        merged: list[MergedEntry],
        replay_tools: list[ReplayToolCall],
        extra: dict[str, Any],
        embedded_itext: Any,
        *,
        has_preceding_text: bool,
    ) -> None:
        batch_id = extra.get("_batch_id")
        merge_target = None
        if not has_preceding_text and batch_id is not None:
            for merge_index in range(len(merged) - 1, -1, -1):
                previous = merged[merge_index]
                if (
                    isinstance(previous, _MergedAssistantEntry)
                    and previous.merged_role == "assistant_tools"
                    and previous.batch_id == batch_id
                ):
                    merge_target = merge_index
                    break
                if not (isinstance(previous, _MergedAssistantEntry) and previous.merged_role == "assistant_tools"):
                    break
        if merge_target is not None:
            target = merged[merge_target]
            if isinstance(target, _MergedAssistantEntry):
                target.contents.extend(replay_tools)
                if embedded_itext:
                    target.intermediate_texts.append(embedded_itext)
        else:
            intermediate_texts = [embedded_itext] if embedded_itext else []
            merged.append(
                _MergedAssistantEntry(
                    contents=list(replay_tools),
                    additional_properties=extra,
                    batch_id=batch_id,
                    intermediate_texts=intermediate_texts,
                    has_preceding_text=has_preceding_text,
                    merged_role="assistant_tools",
                )
            )

    def _build_replay_tool_calls(
        self,
        tool_part: list[dict[str, Any]],
        extra: dict[str, Any],
        pairings: dict[int, _PairedToolData],
        annotations: list[ReplayCompatibilityAnnotation],
    ) -> list[ReplayToolCall]:
        approvals: dict[int, dict[str, Any]] = {}
        for tool_call in tool_part:
            content_approval = _approval_from_content(tool_call)
            if content_approval is not None:
                approvals[id(tool_call)] = content_approval
                annotations.append(ReplayCompatibilityAnnotation(tool_call, {"_replay_approval": content_approval}))

        approval = extra.get("_approval")
        if isinstance(approval, dict):
            tool_name = approval.get("tool_name", "")
            for tool_call in tool_part:
                if (
                    tool_call.get("_replay_approval") is not None
                    or id(tool_call) in approvals
                    or _approval_from_content(tool_call) is not None
                ):
                    continue
                if not tool_name or tool_call.get("name", "") == tool_name:
                    approvals[id(tool_call)] = approval
                    annotations.append(ReplayCompatibilityAnnotation(tool_call, {"_replay_approval": approval}))
                    break

        replay_tools: list[ReplayToolCall] = []
        for tool_call in tool_part:
            paired = pairings.get(id(tool_call), _PairedToolData())
            hosted = paired.hosted_view
            canonical_status = (
                hosted_replay_status(hosted, has_result=paired.has_result)
                if hosted is not None
                else _local_replay_status(paired.metadata)
            )
            replay_result = paired.result or ("interrupted" if canonical_status == "interrupted" else "")
            replay_tools.append(
                ReplayToolCall(
                    source=tool_call,
                    name=hosted.tool_name if hosted is not None else str(tool_call.get("name", "tool")),
                    call_id=_call_id(tool_call),
                    tool_kind=(
                        HOSTED_TOOL_DEFAULT_KIND_BY_FAMILY.get(hosted.family, "")
                        if hosted is not None
                        else _persisted_tool_kind(tool_call)
                    ),
                    arguments=hosted.arguments if hosted is not None else tool_call.get("arguments"),
                    result=replay_result,
                    metadata=paired.metadata,
                    image_contents=paired.image_contents,
                    file_snapshot=paired.file_snapshot,
                    approval=approvals.get(id(tool_call)) or tool_call.get("_replay_approval"),
                    provider_hosted=hosted is not None,
                    hosted_family=hosted.family if hosted is not None else "",
                    provider=hosted.provider if hosted is not None else "",
                    provider_item_type=hosted.provider_item_type if hosted is not None else "",
                    provider_status=(
                        canonical_status
                        if hosted is not None and canonical_status == "interrupted"
                        else hosted.provider_status
                        if hosted is not None
                        else ""
                    ),
                    provider_call_id=hosted.provider_call_id if hosted is not None else "",
                    canonical_status=canonical_status,
                    artifacts=paired.artifacts,
                    duration_ms=paired.duration_ms,
                    started_at=paired.started_at,
                )
            )
        return replay_tools

    @staticmethod
    def _coalesce_tool_groups(merged: list[MergedEntry]) -> list[MergedEntry]:
        coalesced: list[MergedEntry] = []
        for entry in merged:
            can_merge = (
                isinstance(entry, _MergedAssistantEntry)
                and entry.merged_role == "assistant_tools"
                and not entry.has_preceding_text
                and not entry.intermediate_texts
                and coalesced
                and isinstance(coalesced[-1], _MergedAssistantEntry)
                and coalesced[-1].merged_role == "assistant_tools"
            )
            if can_merge:
                previous = coalesced[-1]
                if isinstance(previous, _MergedAssistantEntry):
                    previous.contents.extend(entry.contents)
                    # The coalesced entry represents the complete sequence;
                    # message-level completion timing belongs to its latest
                    # assistant response, not the first tool cycle.
                    previous.additional_properties = entry.additional_properties
            else:
                coalesced.append(entry)
        return coalesced

    def _build_entries(
        self,
        merged: list[MergedEntry],
        *,
        suppress_marker_text: bool,
        show_reasoning: bool,
    ) -> list[ReplayEntry]:
        entries: list[ReplayEntry] = []
        seen_user_in_turn = False
        # When the turn's opening message was sent: when its pass began, unless Continue or Retry began it later.
        turn_opened_at: Any = None
        retry_action_marker_index = _trailing_retry_marker_index(merged)

        for msg_index, entry in enumerate(merged):
            if isinstance(entry, _MergedAssistantEntry):
                entries.append(
                    ReplayAgentMessage(
                        contents=list(entry.contents),
                        created_at=_message_created_at(entry.additional_properties),
                        is_intermediate=entry.is_intermediate,
                        intermediate_texts=list(entry.intermediate_texts),
                    )
                )
                continue

            msg = entry.message
            role = msg.get("role", "")
            contents = _message_contents(msg)
            extra = _message_extra(msg)
            marker = extra.get(HistoryMarkerKind.KEY)
            status_code = _recognized_status_code(extra)

            if marker == HistoryMarkerKind.TURN:
                # A marker written before turns kept when their pass began times it from the opening message.
                started_at = extra.get(TURN_STARTED_AT_KEY) or turn_opened_at
                entries.append(ReplayTurnMarker(turn_index_from_extra(extra), started_at=started_at))
                seen_user_in_turn = False
                turn_opened_at = None
                continue

            if marker == HistoryMarkerKind.SUMMARY:
                entries.append(
                    ReplayContextFold(
                        context_id=str(extra.get("_block_id", "")),
                        summary=_fold_summary(contents),
                    )
                )
                seen_user_in_turn = False
                continue

            if marker == HistoryMarkerKind.INTERRUPTED:
                source = str(extra.get("_interrupted_by", "user"))
                action_label = None
                if msg_index == retry_action_marker_index:
                    action_label = (
                        INTERRUPTED_RETRY_ACTION_MESSAGE if source == "error" else INTERRUPTED_CONTINUE_ACTION_MESSAGE
                    )
                entries.append(
                    ReplayInterrupted(
                        reason=_first_text(contents) or "Execution interrupted",
                        source=source,
                        action_label=action_label,
                        status_code=status_code,
                        status_count=_status_count(extra),
                    )
                )
                continue

            if HistoryMarkerKind.KEY in extra:
                # Status and unrecognized marker kinds (falsy included) fall
                # through as plain text only — chrome payloads never surface
                # as conversation tool activity.
                if suppress_marker_text:
                    if role == "assistant" and (marker in HistoryMarkerKind.STATUS_MARKERS or status_code is not None):
                        entries.append(ReplaySuppressedStatus())
                    continue
                text_contents = [
                    {**content, "text": sanitize_legacy_block(str(content.get("text", "")))}
                    for content in contents
                    if isinstance(content, dict) and content.get("type") == "text" and content.get("text")
                ]
                if role == "assistant" and (text_contents or status_code is not None):
                    entries.append(
                        ReplayAgentMessage(
                            contents=text_contents,
                            created_at=_message_created_at(extra),
                            status_code=status_code,
                            status_count=_status_count(extra),
                            duration_ms=_trajectory_duration_ms(extra),
                            is_status=marker in HistoryMarkerKind.STATUS_MARKERS or status_code is not None,
                        )
                    )
                continue

            if role == "user":
                # A flagged synthetic ``continue`` nudge is a zero-content
                # placeholder (the next resume deletes it) — don't render
                # it, and don't flip ``seen_user_in_turn``.  Legacy
                # unflagged nudges are indistinguishable from a user-typed
                # "continue" and keep rendering via the positional fallback.
                if extra.get(HistoryMarkerKind.CONTINUATION_KEY):
                    continue
                text = contents_text(contents)
                profile_switch_to = str(extra.get(HistoryMarkerKind.PROFILE_SWITCH_TO_KEY) or "")
                if not text:
                    if profile_switch_to:
                        entries.append(ReplayProfileSwitch(profile_switch_to))
                    continue
                is_injection = bool(extra.get(HistoryMarkerKind.INJECTED_KEY, False) or seen_user_in_turn)
                entries.append(
                    ReplayUserMessage(
                        text=text,
                        contents=list(contents),
                        created_at=_message_created_at(extra),
                        is_injection=is_injection,
                        profile_switch_to=profile_switch_to,
                        duration_ms=_trajectory_duration_ms(extra),
                    )
                )
                if not is_injection:
                    seen_user_in_turn = True
                if opens_turn(_TurnMessageView(role, extra)):
                    # By its flags, not its place: a message sent after a stopped pass opens a turn of its own,
                    # though the stopped pass left no marker before it.
                    turn_opened_at = _message_created_at(extra)
                continue

            if role == "assistant":
                entries.append(ReplayAgentMessage(contents=list(contents), created_at=_message_created_at(extra)))

        return _time_turn_answers(_add_structured_output_completions(entries, show_reasoning=show_reasoning))


@dataclass(frozen=True)
class _TurnMessageView:
    """A persisted message dict as :func:`opens_turn` reads it."""

    role: str
    additional_properties: dict[str, Any]
    text: str | None = None


def _time_turn_answers(entries: list[ReplayEntry]) -> list[ReplayEntry]:
    """Tell on the answer each pass ended with how long its turn took, from when the pass began, as live.

    The answer is the last thing the pass showed before its marker, skipping thinking and messages sent mid-turn
    (history may place one after the answer): its text, or the ✓ of its structured output. A pass that ended
    otherwise (interrupted, failed, awaiting sub-agents) has none to time.
    """
    for index, marker in enumerate(entries):
        if not isinstance(marker, ReplayTurnMarker):
            continue
        before = index - 1
        while before >= 0 and (
            _shows_at_most_thinking(previous := entries[before])
            or (isinstance(previous, ReplayUserMessage) and previous.is_injection)
        ):
            before -= 1
        answer = entries[before] if before >= 0 else None
        if isinstance(answer, ReplayStructuredCompletion) or (
            isinstance(answer, ReplayAgentMessage) and not answer.is_status
        ):
            entries[before] = replace(answer, duration_ms=turn_duration_ms(marker.started_at, answer.created_at))
    return entries


def _add_structured_output_completions(entries: list[ReplayEntry], *, show_reasoning: bool) -> list[ReplayEntry]:
    """Complete a turn-ending successful hosted result that has no text response with a ✓.

    The ✓ goes after the thinking that follows the result: live, it showed
    when the turn ended. Shown thinking splits the tool group that ends a
    turn into several; the ✓ reads them all, as it reads the one group
    otherwise.
    """
    candidate_index: int | None = None
    completions: dict[int, ReplayStructuredCompletion] = {}

    def finalize_candidate() -> None:
        nonlocal candidate_index
        if candidate_index is None:
            return
        candidate = entries[candidate_index]
        contents = list(candidate.contents) if isinstance(candidate, ReplayAgentMessage) else []
        if show_reasoning and _shows_no_answer(candidate):
            before = candidate_index
            while before > 0 and isinstance(previous := entries[before - 1], ReplayAgentMessage):
                # Only what thinking split off: a tool group, or the thinking before one.
                if not (_shows_no_answer(previous) and (previous.is_intermediate or _has_tool_call(previous.contents))):
                    break
                before -= 1
                contents.extend(previous.contents)
        if isinstance(candidate, ReplayAgentMessage) and _has_successful_structured_tool(contents):
            after = candidate_index
            while after + 1 < len(entries) and _shows_at_most_thinking(entries[after + 1]):
                after += 1
            completions[after] = ReplayStructuredCompletion(candidate.created_at)
        candidate_index = None

    for index, entry in enumerate(entries):
        if isinstance(entry, ReplayTurnMarker):
            finalize_candidate()
            continue
        if isinstance(entry, ReplayUserMessage) and not entry.is_injection:
            finalize_candidate()
            continue
        if isinstance(entry, ReplayInterrupted | ReplaySuppressedStatus):
            candidate_index = None
            continue
        if not isinstance(entry, ReplayAgentMessage) or entry.is_intermediate:
            continue
        if _has_visible_text(entry.contents) or _has_tool_call(entry.contents):
            candidate_index = index

    finalize_candidate()
    if not completions:
        return entries
    completed: list[ReplayEntry] = []
    for index, entry in enumerate(entries):
        completed.append(entry)
        if (completion := completions.get(index)) is not None:
            completed.append(completion)
    return completed


def _has_tool_call(contents: list[Any]) -> bool:
    return any(
        isinstance(content, ReplayToolCall) or (isinstance(content, dict) and _is_tool_call(content))
        for content in contents
    )


def _shows_no_answer(entry: ReplayEntry) -> bool:
    """Whether *entry* is an agent message that shows no answer or notice: tools and thinking at most."""
    return (
        isinstance(entry, ReplayAgentMessage)
        and entry.status_code is None
        and not any(text.strip() for text in entry.intermediate_texts)
        and not _has_visible_text(entry.contents)
    )


def _shows_at_most_thinking(entry: ReplayEntry) -> bool:
    """Whether *entry* is an agent message that shows no more than thinking: no answer, notice or tool."""
    return _shows_no_answer(entry) and isinstance(entry, ReplayAgentMessage) and not _has_tool_call(entry.contents)


def _has_successful_structured_tool(contents: list[Any]) -> bool:
    """Return whether replay contents carry a successful non-search hosted call."""
    return any(
        isinstance(content, ReplayToolCall)
        and content.provider_hosted
        and content.canonical_status == "completed"
        and content.hosted_family
        not in {HostedToolFamily.SEARCH, HostedToolFamily.FETCH, HostedToolFamily.TOOL_DISCOVERY}
        for content in contents
    )


class HistoryReplayRenderer:
    """Render planned persisted history into transcript widgets."""

    def __init__(
        self,
        mount: ChatMountPort,
        ui: TranscriptUiPort,
        toc: TurnTocModel,
        tool_kind_resolver: Callable[[str], str],
        *,
        default_profile: Callable[[], str],
        localization: TranscriptLocalizationPort | None = None,
        tool_widget_configurator: Callable[[Widget], None] | None = None,
        workspace_cwd: Callable[[], str] | None = None,
        show_reasoning: bool = False,
        fold_passed_text: bool = False,
    ) -> None:
        self._mount = mount
        self._workspace_cwd = workspace_cwd
        self._ui = ui
        self._toc = toc
        self._tool_kind_resolver = tool_kind_resolver
        self._default_profile = default_profile
        self._tool_widget_configurator = tool_widget_configurator
        self._show_reasoning = show_reasoning
        """Whether the transcript shows readable thinking; replayed, it starts folded."""
        self._fold_passed_text = fold_passed_text
        """Whether text between tool calls starts folded, as the live text it was folded once the turn moved on."""
        self._resolve_message = localization.render if localization is not None else format_message
        self._planner = HistoryReplayPlanner()

    async def replay_history(
        self,
        messages: list[dict[str, Any]],
        *,
        initial_profile: str = "",
        file_snapshots: dict[str, list[Any]] | None = None,
        compressed_blocks: dict[str, CompressedBlock] | None = None,
    ) -> None:
        """Replay saved session messages into the chat transcript."""
        await self._ui.dismiss_welcome_for_content()
        self._ui.on_replay_started()
        show_reasoning = self._show_reasoning
        plan = self._planner.build_plan(messages, file_snapshots=file_snapshots, show_reasoning=show_reasoning)
        plan.apply_compatibility_annotations()
        replay_profile = initial_profile or self._default_profile()
        seen_user_in_turn = False
        widgets: list[Widget] = []
        process_failures_after: list[Widget] = []

        for entry in plan.entries:
            if isinstance(entry, ReplayTurnMarker):
                self._toc.apply_turn_marker(entry.turn_index)
                seen_user_in_turn = False
                continue

            if isinstance(entry, ReplayContextFold):
                block = compressed_blocks.get(entry.context_id) if compressed_blocks else None
                if block:
                    replay_profile = await self._replay_compressed_block(
                        block,
                        replay_profile,
                        widgets=widgets,
                        show_reasoning=show_reasoning,
                        process_failures_after=process_failures_after,
                    )
                freed_messages = len(block.messages) if block else 0
                turn_range = block.turn_range if block else (0, 0)
                widgets.append(
                    ContextFoldWidget(
                        entry.context_id,
                        entry.summary,
                        freed_messages,
                        turn_range,
                        resolve_message=self._resolve_message,
                    )
                )
                seen_user_in_turn = False
                continue

            if isinstance(entry, ReplayInterrupted):
                reason = self._resolve_status(entry.status_code, entry.status_count)
                if reason is None:
                    reason = sanitize_legacy_block(entry.reason)
                copy = resolve_interrupted_message_copy(reason, entry.source, self._resolve_message)
                action_label = None
                if entry.action_label is not None:
                    action_label = self._resolve_message(entry.action_label.bind())
                widgets.append(
                    InterruptedMessage(copy.reason, entry.source, action_label=action_label, header=copy.header)
                )
                continue

            if isinstance(entry, ReplayUserMessage):
                if entry.profile_switch_to:
                    replay_profile = entry.profile_switch_to
                is_injection = entry.is_injection or seen_user_in_turn
                if is_injection and self._toc.has_items():
                    turn_id, _item = self._toc.add_injection(entry.text)
                    widget = UserMessage(
                        entry.text,
                        timestamp=format_message_created_at(entry.created_at),
                        is_injection=True,
                        contents=entry.contents,
                    )
                else:
                    turn_id, _item = self._toc.begin_user_turn(entry.text)
                    widget = UserMessage(
                        entry.text,
                        timestamp=format_message_created_at(entry.created_at),
                        contents=entry.contents,
                    )
                    seen_user_in_turn = True
                widget.id = turn_id
                widgets.append(widget)
                continue

            if isinstance(entry, ReplayProfileSwitch):
                replay_profile = entry.profile_name
                continue

            if isinstance(entry, ReplayAgentMessage):
                widgets.extend(
                    AgentMessage(
                        itext,
                        is_final=True,
                        profile_name=replay_profile,
                        is_intermediate=True,
                        collapsed=self._fold_passed_text,
                    )
                    for itext in entry.intermediate_texts
                    if itext.strip()
                )
                contents = entry.contents
                status_text = self._resolve_status(entry.status_code, entry.status_count)
                if status_text is not None:
                    contents = [{"type": "text", "text": status_text}]
                await self._replay_assistant(
                    contents,
                    profile_name=replay_profile,
                    is_intermediate=entry.is_intermediate,
                    created_at=entry.created_at,
                    duration_ms=entry.duration_ms,
                    widgets=widgets,
                    show_reasoning=show_reasoning,
                    is_status=entry.is_status or entry.status_code is not None,
                )
                continue

            if isinstance(entry, ReplayStructuredCompletion):
                widgets.append(_structured_completion_message(entry, replay_profile, is_compressed=False))

        if widgets:
            await self._mount.mount_transcript_widgets(widgets, process_failures_after=tuple(process_failures_after))
        self._ui.after_replay_history_mounted()

    def _resolve_status(self, status_code: str | None, count: int | None) -> str | None:
        if status_code is None:
            return None
        # A recognized code the display map does not cover, or a dynamic
        # semantic marker missing its structured fields, fails closed to the
        # persisted literal — same contract as an unrecognized code.
        definition = _STATUS_MESSAGE_DEFINITIONS.get(status_code)
        if definition is None:
            return None
        reference: MessageRef
        if status_code == HistoryMarkerKind.STATUS_AWAITING_SUB_AGENTS:
            if not count:
                return None
            reference = definition.bind(count=count)
        else:
            reference = definition.bind()
        return self._resolve_message(reference)

    async def _replay_compressed_block(
        self,
        block: CompressedBlock,
        replay_profile: str,
        *,
        widgets: list[Widget] | None = None,
        show_reasoning: bool = False,
        process_failures_after: list[Widget] | None = None,
    ) -> str:
        """Render a compressed block through the canonical replay planner."""
        replay_widgets = widgets if widgets is not None else []
        failure_anchors = process_failures_after if process_failures_after is not None else []
        serialized_messages = [message.to_dict() for message in block.messages]
        plan = self._planner.build_plan(serialized_messages, suppress_marker_text=True, show_reasoning=show_reasoning)
        plan.apply_compatibility_annotations()
        seen_user_in_turn = False
        for entry in plan.entries:
            if isinstance(entry, ReplayInterrupted | ReplaySuppressedStatus):
                # Omitted prose still ends the preceding activity unsuccessfully.
                # Anchor the fact to existing content so it never creates a copy
                # entry, a process member, or a row in the replay mount budget.
                if replay_widgets:
                    failure_anchors.append(replay_widgets[-1])
                continue

            if isinstance(entry, ReplayTurnMarker):
                self._toc.apply_turn_marker(entry.turn_index)
                seen_user_in_turn = False
                continue

            if isinstance(entry, ReplayProfileSwitch):
                replay_profile = entry.profile_name
                continue

            if isinstance(entry, ReplayUserMessage):
                if entry.profile_switch_to:
                    replay_profile = entry.profile_switch_to
                is_injection = entry.is_injection or seen_user_in_turn
                if is_injection and self._toc.has_items():
                    turn_id, _item = self._toc.add_injection(entry.text, compressed=True)
                    widget = UserMessage(
                        entry.text,
                        timestamp=format_message_created_at(entry.created_at),
                        compressed=True,
                        is_injection=True,
                        contents=entry.contents,
                    )
                else:
                    turn_id, _item = self._toc.begin_user_turn(entry.text, compressed=True)
                    widget = UserMessage(
                        entry.text,
                        timestamp=format_message_created_at(entry.created_at),
                        compressed=True,
                        contents=entry.contents,
                    )
                    seen_user_in_turn = True
                widget.id = turn_id
                replay_widgets.append(widget)
                continue

            if isinstance(entry, ReplayAgentMessage):
                replay_widgets.extend(
                    AgentMessage(
                        text,
                        is_final=True,
                        profile_name=replay_profile,
                        is_intermediate=True,
                        is_compressed=True,
                        collapsed=self._fold_passed_text,
                    )
                    for text in entry.intermediate_texts
                    if text.strip()
                )
                visible_contents = [
                    content
                    for content in entry.contents
                    if not isinstance(content, ReplayToolCall)
                    or content.provider_hosted
                    or (
                        content.source.get("type") == "function_call"
                        and content.source.get("informational_only") is True
                    )
                ]
                await self._replay_assistant(
                    visible_contents,
                    profile_name=replay_profile,
                    is_intermediate=entry.is_intermediate,
                    created_at=entry.created_at,
                    duration_ms=entry.duration_ms,
                    is_compressed=True,
                    widgets=replay_widgets,
                    show_reasoning=show_reasoning,
                    is_status=entry.is_status or entry.status_code is not None,
                )
                continue

            if isinstance(entry, ReplayStructuredCompletion):
                replay_widgets.append(_structured_completion_message(entry, replay_profile, is_compressed=True))

        if widgets is None:
            await self._mount.mount_transcript_widgets(replay_widgets, process_failures_after=tuple(failure_anchors))
        return replay_profile

    async def _replay_assistant(
        self,
        contents: list[Any],
        profile_name: str = "",
        is_intermediate: bool = False,
        created_at: Any = None,
        duration_ms: int | None = None,
        is_compressed: bool = False,
        widgets: list[Widget] | None = None,
        show_reasoning: bool = False,
        is_status: bool = False,
    ) -> None:
        """Replay a single assistant message, preserving text/thinking/tool-call order."""
        replay_widgets = widgets if widgets is not None else []
        pending_text: list[tuple[str, str | None]] = []
        # The segment the pending text was shown in: one bubble per segment, as a bare Continue may withdraw each.
        pending_segment: str | None = None
        pending_tools: list[ReplayToolCall | dict[str, Any]] = []
        pending_thinking: list[str] = []
        thinking_channel: ContentChannel | None = None
        # An answer bubble thinking followed, as (index, text, segment ids): live, a later answer settled it.
        before_thinking: tuple[int, str, tuple[str, ...]] | None = None
        timestamp = "" if is_intermediate else format_message_created_at(created_at)

        def answer_message(text: str, segment_ids: tuple[str, ...], *, intermediate: bool) -> AgentMessage:
            return AgentMessage(
                text,
                is_final=True,
                profile_name=profile_name,
                is_intermediate=intermediate,
                is_compressed=is_compressed,
                timestamp="" if intermediate else timestamp,
                duration_ms=None if intermediate else duration_ms,
                segment_ids=segment_ids,
                is_status=is_status,
                collapsed=self._fold_passed_text,
            )

        async def flush_text(*, settled: bool = False, thinking_follows: bool = False) -> None:
            nonlocal before_thinking
            text = join_text_blocks(reconstruct_text_blocks(pending_text)).strip()
            segment_ids = (pending_segment,) if pending_segment else ()
            pending_text.clear()
            if not text:
                return
            if before_thinking is not None:
                index, earlier_text, earlier_segment_ids = before_thinking
                replay_widgets[index] = answer_message(earlier_text, earlier_segment_ids, intermediate=True)
                before_thinking = None
            intermediate = is_intermediate or settled
            replay_widgets.append(answer_message(text, segment_ids, intermediate=intermediate))
            if thinking_follows and not intermediate:
                before_thinking = (len(replay_widgets) - 1, text, segment_ids)

        def flush_thinking() -> None:
            nonlocal thinking_channel
            channel, text = thinking_channel, "".join(pending_thinking)
            thinking_channel = None
            pending_thinking.clear()
            if channel is not None:
                replay_widgets.append(
                    ThinkingMessage(text, channel=channel, collapsed=True, is_compressed=is_compressed)
                )

        async def flush_tools() -> None:
            if not pending_tools:
                return
            group = ToolGroup(
                live=False, tool_widget_configurator=self._tool_widget_configurator, workspace_cwd=self._workspace_cwd
            )
            for idx, tc in enumerate(pending_tools):
                if isinstance(tc, ReplayToolCall):
                    name = tc.name
                    call_id = tc.call_id
                    args = tc.arguments
                    result = tc.result or ""
                    snapshot = tc.file_snapshot
                    replay_approval = tc.approval
                    replay_metadata = tc.metadata
                    replay_image_contents = tc.image_contents
                    replay_artifacts = tc.artifacts
                    provider_hosted = tc.provider_hosted
                    hosted_family = tc.hosted_family
                    provider = tc.provider
                    provider_item_type = tc.provider_item_type
                    provider_status = tc.provider_status
                    provider_call_id = tc.provider_call_id
                    canonical_status = tc.canonical_status
                    replay_duration_ms = tc.duration_ms
                    replay_started_at = tc.started_at
                else:
                    call_id = _call_id(tc)
                    result = tc.get("_replay_result", "") or ""
                    snapshot = tc.get("_file_snapshot")
                    replay_approval = tc.get("_replay_approval")
                    replay_metadata = tc.get("_replay_metadata")
                    replay_image_contents = tc.get("_replay_image_contents")
                    replay_artifacts = tc.get("_replay_artifacts")
                    replay_duration_ms = _content_duration_ms(tc)
                    replay_started_at = _content_started_at(tc)
                    if _is_hosted_tool_call(tc):
                        hosted = adapt_hosted_tool(_hosted_content_for_replay(tc))
                        name = hosted.tool_name
                        args = hosted.arguments
                        provider_hosted = True
                        hosted_family = hosted.family
                        provider = hosted.provider
                        provider_item_type = hosted.provider_item_type
                        provider_status = hosted.provider_status
                        provider_call_id = hosted.provider_call_id
                        canonical_status = hosted_replay_status(hosted, has_result=False)
                    else:
                        name = tc.get("name", "tool")
                        args = tc.get("arguments")
                        provider_hosted = False
                        hosted_family = ""
                        provider = ""
                        provider_item_type = ""
                        provider_status = ""
                        provider_call_id = ""
                        canonical_status = _local_replay_status(replay_metadata)
                if isinstance(args, str):
                    with suppress(json.JSONDecodeError, ValueError):
                        args = json.loads(args)
                args_str = ""
                if isinstance(args, dict):
                    args_str = json.dumps(args, ensure_ascii=False)
                elif args:
                    args_str = str(args)
                tool_args = args if isinstance(args, dict) else None
                if isinstance(tc, ReplayToolCall):
                    tool_kind = tc.tool_kind or self._tool_kind_resolver(name)
                elif provider_hosted:
                    tool_kind = HOSTED_TOOL_DEFAULT_KIND_BY_FAMILY.get(hosted_family, "")
                else:
                    tool_kind = _persisted_tool_kind(tc) or self._tool_kind_resolver(name)
                unique_key = f"{call_id or name}#{idx}"
                tool_approval = replay_approval.get("status") if isinstance(replay_approval, dict) else None
                if not isinstance(replay_metadata, dict):
                    replay_metadata = None
                if not isinstance(replay_image_contents, list):
                    replay_image_contents = None
                if not isinstance(replay_artifacts, list):
                    replay_artifacts = None
                await group.add_collapsed_replay_tool(
                    unique_key,
                    name,
                    tool_kind,
                    args_str,
                    args=tool_args,
                    result=result,
                    file_snapshot=snapshot,
                    approval=tool_approval,
                    metadata=replay_metadata,
                    image_contents=replay_image_contents,
                    artifacts=replay_artifacts,
                    provider_hosted=provider_hosted,
                    hosted_family=hosted_family,
                    provider=provider,
                    provider_item_type=provider_item_type,
                    provider_status=provider_status,
                    provider_call_id=provider_call_id,
                    canonical_status=canonical_status,
                    duration_ms=replay_duration_ms or 0,
                    duration_known=replay_duration_ms is not None,
                    timestamp=format_message_created_at(replay_started_at),
                    lazy=True,
                )
            replay_widgets.append(group)
            pending_tools.clear()

        for content in contents:
            is_tool = isinstance(content, ReplayToolCall) or _is_tool_call(content)
            if is_tool:
                flush_thinking()
                if pending_text:
                    await flush_text()
                pending_tools.append(content)
            else:
                if pending_tools:
                    await flush_tools()
                if isinstance(content, dict) and content.get("type") in TOOL_RESULT_CONTENT_TYPES:
                    # A result ends the thinking before it, as it ended the segment live.
                    flush_thinking()
                if show_reasoning and (shown := _readable_thinking(content)) is not None:
                    channel, thought = shown
                    if channel == thinking_channel:
                        pending_thinking.append(thought)
                    elif thought.strip():
                        # The answer before it shows above it; blank thinking showed nothing live.
                        flush_thinking()
                        await flush_text(thinking_follows=True)
                        thinking_channel = channel
                        pending_thinking.append(thought)
                    continue
                if isinstance(content, dict) and content.get("type") == "text":
                    properties = content.get("additional_properties")
                    text, block_id = content.get("text", "") or "", text_block_id(properties)
                    segment = properties.get(ANSWER_SEGMENT_KEY) if isinstance(properties, dict) else None
                    segment = segment if isinstance(segment, str) and segment else None
                elif isinstance(content, str):
                    text, block_id, segment = content, None, None
                else:
                    continue
                # Even an empty fragment can separate different item IDs, but only
                # text shows a segment. Suppress empty blocks after reconstruction.
                if text:
                    flush_thinking()
                    if pending_text and segment != pending_segment:
                        # A later segment settles this one as intermediate text, as it did live.
                        await flush_text(settled=True)
                    pending_segment = segment
                pending_text.append((text, block_id))

        await flush_tools()
        flush_thinking()
        await flush_text()
        if widgets is None:
            await self._mount.mount_transcript_widgets(replay_widgets)


def _structured_completion_message(
    entry: ReplayStructuredCompletion, profile_name: str, *, is_compressed: bool
) -> AgentMessage:
    return AgentMessage(
        "✓",
        is_final=True,
        profile_name=profile_name,
        is_compressed=is_compressed,
        is_structured_completion=True,
        timestamp=format_message_created_at(entry.created_at),
        duration_ms=entry.duration_ms,
    )


def _message_contents(message: dict[str, Any]) -> list[Any]:
    contents = message.get("contents", [])
    return contents if isinstance(contents, list) else []


def _message_extra(message: dict[str, Any]) -> dict[str, Any]:
    extra = message.get("additional_properties") or {}
    return extra if isinstance(extra, dict) else {}


def _trajectory_duration_ms(metadata: dict[str, Any]) -> int | None:
    timing = trajectory_timing_from_metadata(metadata)
    return timing["duration_ms"] if timing is not None else None


def _message_created_at(metadata: dict[str, Any]) -> Any:
    created_at = metadata.get(MESSAGE_CREATED_AT_KEY)
    if parse_created_at(created_at) is not None:
        return created_at
    timing = trajectory_timing_from_metadata(metadata)
    return timing["finished_at"] if timing is not None else None


def _content_timing(content: dict[str, Any] | None) -> dict[str, Any] | None:
    if content is None:
        return None
    metadata = content.get("additional_properties")
    return trajectory_timing_from_metadata(metadata) if isinstance(metadata, dict) else None


def _content_duration_ms(content: dict[str, Any] | None) -> int | None:
    timing = _content_timing(content)
    return timing["duration_ms"] if timing is not None else None


def _content_started_at(content: dict[str, Any] | None) -> str | None:
    timing = _content_timing(content)
    return timing["started_at"] if timing is not None else None


def _recognized_status_code(extra: dict[str, Any]) -> str | None:
    status_code = extra.get(HistoryMarkerKind.STATUS_CODE_KEY)
    if isinstance(status_code, str) and status_code in HistoryMarkerKind.STATUS_CODES:
        return status_code
    return None


def _status_count(extra: dict[str, Any]) -> int:
    invocation_ids = extra.get("_invocation_ids")
    return len(invocation_ids) if isinstance(invocation_ids, list) else 0


def _message_timestamp(extra: dict[str, Any] | None) -> str:
    if not extra:
        return ""
    return format_message_created_at(extra.get(MESSAGE_CREATED_AT_KEY))


def _is_tool_call(content: Any) -> bool:
    return isinstance(content, dict) and content.get("type") in (TOOL_CALL_CONTENT_TYPES | {LEGACY_CALL_CONTENT_TYPE})


def _is_hosted_tool_call(content: dict[str, Any]) -> bool:
    return (
        bool(content.get("provider_hosted"))
        or content.get("type") in TOOL_CALL_CONTENT_TYPES - {"function_call"}
        or is_legacy_server_executed_informational_call(content)
    )


def _is_hosted_tool_result(content: dict[str, Any]) -> bool:
    """Return whether persisted result content belongs to a hosted occurrence."""
    content_type = content.get("type")
    return content_type in TOOL_RESULT_CONTENT_TYPES - {"function_result"} or (
        content_type == "function_result" and bool(content.get("provider_hosted"))
    )


def _hosted_content_for_replay(content: dict[str, Any]) -> Content:
    """Return a canonical in-memory hosted view without rewriting persisted data."""
    return normalize_hosted_replay_content(_content_for_replay(content))


def _content_for_replay(content: dict[str, Any]) -> Content:
    """Decode persisted content without planner-only compatibility fields."""
    canonical = {key: value for key, value in content.items() if key not in _REPLAY_ONLY_CONTENT_KEYS}
    return Content.from_dict(canonical)


def _local_replay_status(metadata: dict[str, Any] | None) -> str:
    """Derive the non-hosted canonical state from persisted result metadata."""
    if metadata is not None and metadata.get(TOOL_INTERRUPTED_METADATA_KEY) is True:
        return "interrupted"
    return "completed"


def _hosted_trailing_text_is_final(contents: list[Any]) -> bool:
    """Mirror live planner semantics: text after terminal hosted work is final."""
    typed: list[Content] = []
    for content in contents:
        if not isinstance(content, dict):
            return False
        typed.append(_hosted_content_for_replay(content) if _is_tool_call(content) else _content_for_replay(content))
    if not any(_is_hosted_call(content) or _is_hosted_result(content) for content in typed):
        # Without hosted occurrences the planner reports any trailing text as final.
        return False
    return bool(ResponsePresentationPlan.from_messages([Message("assistant", typed)]).final_text)


def _call_id(content: dict[str, Any]) -> str:
    if str(content.get("type", "")).startswith("image_generation_"):
        return str(content.get("image_id") or "")
    return str(content.get("call_id") or "")


def _hosted_artifact_descriptors(view: HostedToolView) -> list[dict[str, Any]]:
    descriptors: list[dict[str, Any]] = []
    for artifact in view.artifacts:
        descriptor: dict[str, Any] = {
            "id": artifact.file_id or artifact.vector_store_id or artifact.id or "",
            "path": artifact.uri or artifact.name or "",
            "mime": artifact.media_type or "",
        }
        size = artifact.additional_properties.get("size")
        if isinstance(size, int) and not isinstance(size, bool):
            descriptor["size"] = size
        descriptors.append({key: value for key, value in descriptor.items() if value != ""})
    return descriptors


def _approval_from_content(content: dict[str, Any]) -> dict[str, Any] | None:
    extra = content.get("additional_properties")
    if not isinstance(extra, dict):
        return None
    approval = extra.get("_approval")
    return approval if isinstance(approval, dict) else None


def _replay_tool_metadata(content: dict[str, Any]) -> dict[str, Any] | None:
    extra = content.get("additional_properties")
    if not isinstance(extra, dict):
        return None
    metadata: dict[str, Any] = {}
    nested = extra.get(TOOL_RESULT_METADATA_KEY)
    if isinstance(nested, dict):
        metadata.update(nested)
    metadata.update({key: extra[key] for key in PERSISTED_RESULT_METADATA_KEYS if key in extra})
    return metadata or None


def _persisted_tool_kind(content: dict[str, Any]) -> str:
    return persisted_tool_call_kind(content.get("additional_properties"))


def _replay_tool_images(content: dict[str, Any]) -> list[dict[str, Any]]:
    items = content.get("items")
    if not isinstance(items, list):
        return []

    image_items: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        media_type = item.get("media_type")
        if media_type is None:
            extra = item.get("additional_properties")
            media_type = extra.get("media_type") if isinstance(extra, dict) else None
        if is_image_media_type(media_type):
            image_items.append(item)
    return image_items


def _fold_summary(contents: list[Any]) -> str:
    fallback = ""
    for content in contents:
        text = (
            content.get("text", "") if isinstance(content, dict) else str(content) if isinstance(content, str) else ""
        )
        if text and "Summary:" in text:
            return text.split("Summary:", 1)[1].strip()
        if text:
            fallback = text
    return fallback


def _first_text(contents: list[Any]) -> str:
    for content in contents:
        if isinstance(content, dict) and content.get("type") == "text":
            return str(content.get("text", ""))
        if isinstance(content, str):
            return content
    return ""


def _has_visible_text(contents: list[Any]) -> bool:
    return any(
        (isinstance(content, dict) and content.get("type") == "text" and content.get("text"))
        or isinstance(content, str)
        for content in contents
    )


def _readable_thinking(content: Any) -> tuple[ContentChannel, str] | None:
    """The channel and text a persisted reasoning content shows, or ``None`` when it has nothing readable."""
    if not isinstance(content, dict) or content.get("type") != "text_reasoning":
        return None
    return readable_reasoning(Content.from_dict(content))


def _shows_thinking(contents: list[Any]) -> bool:
    """Whether *contents* hold thinking to show: readable text that is not blank, as live."""
    return any((shown := _readable_thinking(content)) is not None and shown[1].strip() for content in contents)


def _intermediate_text_is_visible(contents: list[Any], intermediate_text: Any) -> bool:
    if not isinstance(intermediate_text, str) or not intermediate_text.strip():
        return False
    normalized = intermediate_text.strip()
    return any(text == normalized for text in _visible_text_variants(contents))


def _legacy_duplicate_intermediate_sidecars(messages: list[dict[str, Any]]) -> set[int]:
    """Find sidecars misattributed to an adjacent persisted tool batch.

    Older Chrys versions paired captured intermediate text to assistant
    messages positionally.  Retry, resume, and mid-pass compaction could shift
    that pairing by one batch, leaving the real text in one assistant message
    and an identical ``_intermediate_text`` sidecar on the adjacent batch.

    This compatibility logic should be deprecated once sessions written by
    those buggy versions no longer need replay support.  Keep it deliberately
    narrow in the meantime: both messages must be batch-tagged tool calls and
    separated only by one or more tool-result messages.
    """
    duplicates: set[int] = set()
    for index, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        contents = _message_contents(message)
        if not any(_is_tool_call(content) for content in contents):
            continue
        extra = _message_extra(message)
        intermediate_text = extra.get("_intermediate_text")
        if "_batch_id" not in extra or _intermediate_text_is_visible(contents, intermediate_text):
            continue

        for direction in (-1, 1):
            cursor = index + direction
            crossed_tool_result = False
            while 0 <= cursor < len(messages) and messages[cursor].get("role") == "tool":
                crossed_tool_result = True
                cursor += direction
            if not crossed_tool_result or not 0 <= cursor < len(messages):
                continue

            adjacent = messages[cursor]
            if adjacent.get("role") != "assistant":
                continue
            adjacent_contents = _message_contents(adjacent)
            adjacent_extra = _message_extra(adjacent)
            if (
                "_batch_id" in adjacent_extra
                and any(_is_tool_call(content) for content in adjacent_contents)
                and _intermediate_text_is_visible(adjacent_contents, intermediate_text)
            ):
                duplicates.add(index)
                break
    return duplicates


def _visible_text_variants(contents: list[Any]) -> list[str]:
    variants: list[str] = []
    all_parts: list[str] = []
    segment_parts: list[str] = []
    # Legacy replay inserted newlines between every fragment, while live text
    # and session sidecar suppression concatenated without separators. Recognize
    # both historical forms for backward-compatible _intermediate_text matching.
    for content in contents:
        if isinstance(content, dict) and content.get("type") == "text":
            text = str(content.get("text", ""))
        elif isinstance(content, str):
            text = content
        else:
            if segment_parts:
                variants.append("\n".join(segment_parts).strip())
                variants.append("".join(segment_parts).strip())
                segment_parts.clear()
            continue
        if text:
            all_parts.append(text)
            segment_parts.append(text)
    if segment_parts:
        variants.append("\n".join(segment_parts).strip())
        variants.append("".join(segment_parts).strip())
    if all_parts:
        variants.append("\n".join(all_parts).strip())
        variants.append("".join(all_parts).strip())
    return [text for text in variants if text]


def contents_text(contents: list[Any]) -> str:
    """A user message's text as its contents hold it: what the model received, shown live and on replay alike."""
    return "\n".join(_text_parts(contents)).strip()


def _text_parts(contents: list[Any]) -> list[str]:
    parts: list[str] = []
    for content in contents:
        if isinstance(content, dict) and content.get("type") == "text":
            parts.append(str(content.get("text", "")))
        elif isinstance(content, str):
            parts.append(content)
    return parts


def _trailing_retry_marker_index(messages: list[MergedEntry]) -> int | None:
    for index in range(len(messages) - 1, -1, -1):
        entry = messages[index]
        if not isinstance(entry, _RawEntry):
            return None
        extra = _message_extra(entry.message)
        marker = extra.get(HistoryMarkerKind.KEY)
        if marker == HistoryMarkerKind.TURN:
            continue
        return index if marker == HistoryMarkerKind.INTERRUPTED else None
    return None
