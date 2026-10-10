# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Map Chrys backend events to ACP session updates."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from acp import schema as acp_schema
from acp.helpers import (
    start_tool_call,
    tool_content,
    update_agent_message_text,
    update_agent_thought_text,
    update_tool_call,
)

from chrys.app.acp.json_values import to_json_object
from chrys.foundation.events.types import (
    AgentThinking,
    Event,
    InvocationAborted,
    InvocationCascadeAborted,
    InvocationCompactionCommitted,
    InvocationCompactionFinished,
    InvocationCompactionStarted,
    InvocationEvent,
    InvocationMessage,
    InvocationPaused,
    InvocationPresentationAttemptAccepted,
    InvocationPresentationAttemptRejected,
    InvocationProgress,
    InvocationResumed,
    InvocationRetryAttempt,
    InvocationStarted,
    InvocationToolCallArgsUpdated,
    InvocationToolCallProgress,
    InvocationToolCallResult,
    InvocationToolCallStart,
    InvocationToolCallStatusUpdated,
    SessionSaved,
    SessionTitleUpdated,
    TodoListUpdated,
    UsageUpdate,
)
from chrys.foundation.hosted_tools import HostedToolStatus, normalize_hosted_tool_status
from chrys.foundation.tool_kinds import (
    KIND_ASK_USER,
    KIND_DOC_CONVERTER,
    KIND_FILESYSTEM_READ,
    KIND_FILESYSTEM_WRITE,
    KIND_MCP,
    KIND_SEARCH,
    KIND_SHELL,
    KIND_SUB_AGENT,
    KIND_WEB_FETCH,
    KIND_WEB_SEARCH,
)

from .tool_status import tool_result_failed

if TYPE_CHECKING:
    from collections.abc import Sequence

    from chrys.foundation.models.todos import TodoItem

logger = logging.getLogger(__name__)

SessionUpdate = (
    acp_schema.AgentMessageChunk
    | acp_schema.AgentThoughtChunk
    | acp_schema.ToolCallStart
    | acp_schema.ToolCallProgress
    | acp_schema.SessionInfoUpdate
    | acp_schema.UsageUpdate
    | acp_schema.AgentPlanUpdate
)


def with_hosted_metadata[UpdateT: acp_schema.ToolCallStart | acp_schema.ToolCallProgress](
    update: UpdateT,
    *,
    provider_hosted: bool,
    hosted_family: str,
    provider: str,
    provider_item_type: str,
    provider_call_id: str,
    provider_status: str,
) -> UpdateT:
    """Attach the Chrys hosted-tool extension to one ACP tool update."""
    if not provider_hosted:
        return update
    chrys = {
        "provider_hosted": True,
        "hosted_family": hosted_family,
        "provider": provider,
        "provider_item_type": provider_item_type,
        "provider_call_id": provider_call_id,
        "provider_status": provider_status,
    }
    metadata = dict(update.field_meta or {})
    metadata["chrys"] = {key: value for key, value in chrys.items() if value is True or value != ""}
    update.field_meta = metadata
    return update


def _hosted_image_blocks(image_contents: Sequence[Any]) -> list[Any]:
    """Project hosted image contents into ACP content blocks."""
    blocks: list[Any] = []
    for image in image_contents:
        uri = getattr(image, "uri", None) or ""
        media_type = getattr(image, "media_type", None)
        if uri.startswith("data:") and ";base64," in uri:
            prefix, data = uri.split(",", 1)
            mime = media_type or prefix.removeprefix("data:").removesuffix(";base64")
            blocks.append(
                tool_content(acp_schema.ImageContentBlock(type="image", data=data, mimeType=mime or "image/png"))
            )
        elif uri:
            blocks.append(
                tool_content(
                    acp_schema.ResourceContentBlock(
                        type="resource_link",
                        name=getattr(image, "name", None) or getattr(image, "file_id", None) or "Hosted image",
                        uri=uri,
                        mimeType=media_type,
                    )
                )
            )
    return blocks


def _hosted_artifact_blocks(artifacts: Sequence[dict[str, Any]]) -> list[Any]:
    """Project hosted artifact descriptors into ACP content blocks.

    Only a real URI becomes a resource_link; a name-only artifact (OpenAI
    hosted_file carries just file_id and filename) is not addressable and
    falls back to text.
    """
    blocks: list[Any] = []
    for artifact in artifacts:
        uri = str(artifact.get("path") or "")
        name = str(artifact.get("name") or "") or str(artifact.get("id") or "")
        mime = str(artifact.get("mime") or "") or None
        if not uri:
            label = name or "Hosted artifact"
            detail = f"{label} ({mime})" if mime else label
            blocks.append(tool_content(acp_schema.TextContentBlock(type="text", text=f"Hosted artifact: {detail}")))
            continue
        size = artifact.get("size")
        blocks.append(
            tool_content(
                acp_schema.ResourceContentBlock(
                    type="resource_link",
                    name=name or uri,
                    uri=uri,
                    mimeType=mime,
                    size=size if isinstance(size, int) and not isinstance(size, bool) else None,
                )
            )
        )
    return blocks


def _tool_result_content_blocks(event: InvocationToolCallResult) -> list[Any]:
    """Project a live result's structured payloads like persisted history does.

    Image-only hosted results (image generation, code-interpreter plots) have
    no result text; without these blocks they complete with nothing visible
    until the session is reloaded.  Local results stay text-only: their
    images address the model, not the ACP client.
    """
    blocks: list[Any] = []
    if event.result:
        blocks.append(tool_content(acp_schema.TextContentBlock(type="text", text=event.result)))
    if not event.provider_hosted:
        return blocks
    blocks.extend(_hosted_image_blocks(event.image_contents))
    blocks.extend(_hosted_artifact_blocks(event.artifacts))
    return blocks


def plan_update_for_todos(items: Sequence[TodoItem]) -> acp_schema.AgentPlanUpdate:
    """AgentPlanUpdate replacing the client's whole plan (empty = clear).

    ``sessionUpdate`` is a REQUIRED aliased discriminator and
    ``PlanEntry.priority`` has no default — omitting either fails pydantic
    validation and no plan update reaches the client.
    """
    return acp_schema.AgentPlanUpdate(
        sessionUpdate="plan",
        entries=[acp_schema.PlanEntry(content=item.content, priority="medium", status=item.status) for item in items],
    )


def session_title_info_update(event: SessionTitleUpdated) -> acp_schema.SessionInfoUpdate:
    """SessionInfoUpdate for a title change.

    Sends the resolved display title: clearing a custom title falls back
    to the generated/first-message title rather than clearing the client's
    label.  Only a session with no title at all maps to null, which ACP
    defines as "clear the title" — clients fall back to their own default
    naming until the next update supplies one.
    """
    return acp_schema.SessionInfoUpdate(
        sessionUpdate="session_info_update",
        title=event.display_title or None,
        updatedAt=event.timestamp.isoformat(),
    )


class AcpEventBridge:
    """Stateful projection from Chrys events into ACP updates."""

    def __init__(self) -> None:
        self._sub_agent_parents: dict[str, str] = {}
        self._tool_kinds_by_call_id: dict[str, str] = {}
        self._last_streaming_agent_text = ""
        # Structured blocks already pushed by hosted progress updates. An ACP
        # content field replaces the card's whole collection, so a later
        # status-only update must re-send these or they vanish.
        self._hosted_progress_images: dict[str, list[Any]] = {}
        # Sub-agent hosted blocks accumulate per invocation and ride EVERY
        # parent-card note until the final parent update: all notes share one
        # parent card, so any note that omits them wipes them.
        self._sub_agent_hosted_blocks: dict[str, dict[str, list[Any]]] = {}

    def updates_for_event(self, event: Event) -> list[SessionUpdate]:
        """Return ACP updates for one Chrys event."""
        if isinstance(event, InvocationEvent):
            if event.origin.root.kind == "workflow_node":
                return []
            if event.origin.kind == "sub_agent":
                return self._sub_agent_updates(event)
        if isinstance(event, InvocationMessage):
            chunk = self._agent_message_delta(event)
            if not chunk:
                return []
            return [update_agent_message_text(chunk)]
        if isinstance(event, AgentThinking):
            if not event.text:
                return []
            return [update_agent_thought_text(event.text)]
        if isinstance(event, InvocationPresentationAttemptAccepted):
            return []
        if isinstance(event, InvocationPresentationAttemptRejected):
            # ACP has no text-retraction primitive. Keep the already-sent
            # stale partial visible, matching its existing retry behavior,
            # but start the next cumulative stream from a clean baseline.
            self._last_streaming_agent_text = ""
            return []
        if isinstance(event, InvocationRetryAttempt):
            self._last_streaming_agent_text = ""
            return []
        if isinstance(event, InvocationToolCallStart):
            if event.call_id:
                self._tool_kinds_by_call_id[event.call_id] = event.tool_kind
            update = start_tool_call(
                event.call_id,
                tool_call_title(event.tool_name, event.tool_kind, event.args),
                kind=acp_tool_kind(event.tool_kind),
                status="in_progress",
                raw_input=to_json_object(event.args),
            )
            return [
                with_hosted_metadata(
                    update,
                    provider_hosted=event.provider_hosted,
                    hosted_family=event.hosted_family,
                    provider=event.provider,
                    provider_item_type=event.provider_item_type,
                    provider_call_id=event.provider_call_id,
                    provider_status=event.provider_status,
                )
            ]
        if isinstance(event, InvocationToolCallProgress):
            text = "\n".join(event.lines)
            blocks: list[Any] = []
            if text:
                blocks.append(tool_content(acp_schema.TextContentBlock(type="text", text=text)))
            if event.provider_hosted:
                # Partial image snapshots must ship now: a transport failure
                # after this point yields only a failed status update, never a
                # terminal result carrying the images.
                image_blocks = _hosted_image_blocks(event.image_contents)
                if image_blocks:
                    self._hosted_progress_images[event.call_id] = image_blocks
                else:
                    image_blocks = self._hosted_progress_images.get(event.call_id, [])
                blocks.extend(image_blocks)
            update = update_tool_call(
                event.call_id,
                status="in_progress",
                raw_output=text,
                content=blocks or None,
            )
            return [
                with_hosted_metadata(
                    update,
                    provider_hosted=event.provider_hosted,
                    hosted_family=event.hosted_family,
                    provider=event.provider,
                    provider_item_type=event.provider_item_type,
                    provider_call_id=event.provider_call_id,
                    provider_status=event.provider_status,
                )
            ]
        if isinstance(event, InvocationToolCallArgsUpdated):
            update = update_tool_call(
                event.call_id,
                status="in_progress",
                raw_input=to_json_object(event.args),
            )
            return [
                with_hosted_metadata(
                    update,
                    provider_hosted=event.provider_hosted,
                    hosted_family=event.hosted_family,
                    provider=event.provider,
                    provider_item_type=event.provider_item_type,
                    provider_call_id=event.provider_call_id,
                    provider_status=event.provider_status,
                )
            ]
        if isinstance(event, InvocationToolCallStatusUpdated):
            status = normalize_hosted_tool_status(event.status)
            acp_status: acp_schema.ToolCallStatus = (
                "completed"
                if status is HostedToolStatus.COMPLETED
                else "failed"
                if status in {HostedToolStatus.FAILED, HostedToolStatus.INTERRUPTED}
                else "in_progress"
            )
            result_text = event.metadata.get("result_text")
            text = result_text if isinstance(result_text, str) else ""
            preserved = (
                self._hosted_progress_images.pop(event.call_id, [])
                if acp_status != "in_progress"
                else self._hosted_progress_images.get(event.call_id, [])
            )
            status_blocks = ([_text_tool_content(text)] if text else []) + preserved
            provider_item_type = event.metadata.get("provider_item_type")
            provider_call_id = event.metadata.get("provider_call_id")
            update = update_tool_call(
                event.call_id,
                status=acp_status,
                raw_output=text or None,
                content=status_blocks or None,
            )
            return [
                with_hosted_metadata(
                    update,
                    provider_hosted=event.provider_hosted,
                    hosted_family=event.hosted_family,
                    provider=event.provider,
                    provider_item_type=provider_item_type if isinstance(provider_item_type, str) else "",
                    provider_call_id=provider_call_id if isinstance(provider_call_id, str) else "",
                    provider_status=event.provider_status,
                )
            ]
        if isinstance(event, InvocationToolCallResult):
            self._hosted_progress_images.pop(event.call_id, None)
            tool_kind = self._tool_kinds_by_call_id.pop(event.call_id, "") if event.call_id else ""
            provider_status = normalize_hosted_tool_status(event.provider_status)
            status = (
                "failed"
                if provider_status in {HostedToolStatus.FAILED, HostedToolStatus.INTERRUPTED}
                or tool_result_failed(
                    event.result,
                    event.metadata,
                    tool_kind=tool_kind,
                    tool_name=event.tool_name,
                )
                else "completed"
            )
            content_blocks = _tool_result_content_blocks(event)
            # The final parent update replaces the card's content too: attach
            # the owning invocation's accumulated hosted blocks before
            # dropping it.  call_id alone is ambiguous — per-run counters can
            # collide across concurrent sub-agents.
            invocation_id = event.metadata.get("sub_agent_invocation_id")
            if isinstance(invocation_id, str) and invocation_id:
                content_blocks.extend(self._sub_agent_blocks_for_invocation(invocation_id))
                self._sub_agent_parents.pop(invocation_id, None)
                self._sub_agent_hosted_blocks.pop(invocation_id, None)
            update = update_tool_call(
                event.call_id,
                status=status,
                raw_output=event.result,
                content=content_blocks or None,
            )
            return [
                with_hosted_metadata(
                    update,
                    provider_hosted=event.provider_hosted,
                    hosted_family=event.hosted_family,
                    provider=event.provider,
                    provider_item_type=event.provider_item_type,
                    provider_call_id=event.provider_call_id,
                    provider_status=event.provider_status,
                )
            ]
        if isinstance(event, UsageUpdate):
            # Standard ACP UsageUpdate represents the session-window gauge.
            # Sub-agent UsageUpdates (``usage_source_id`` set to the sub-agent
            # invocation id rather than the session id) must NOT replace it —
            # otherwise standard ACP clients see the session gauge flicker
            # between parent and sub-agent contexts.  The richer
            # ``chrys/usage_update`` extension notification still carries the
            # sub-agent event for clients that want it.
            if event.usage_source_id and event.usage_source_id != event.session_id:
                return []
            return [
                acp_schema.UsageUpdate(
                    sessionUpdate="usage_update",
                    used=max(0, event.total_tokens or event.local_tokens),
                    size=max(0, event.max_context_tokens),
                )
            ]
        if isinstance(event, SessionSaved):
            return [
                acp_schema.SessionInfoUpdate(
                    sessionUpdate="session_info_update",
                    updatedAt=event.timestamp.isoformat(),
                )
            ]
        if isinstance(event, SessionTitleUpdated):
            return [session_title_info_update(event)]
        if isinstance(event, TodoListUpdated):
            return [plan_update_for_todos(event.items)]
        return []

    def _sub_agent_updates(self, event: Event) -> list[SessionUpdate]:
        if isinstance(event, InvocationStarted):
            self._sub_agent_parents[event.origin.invocation_id] = event.parent_call_id
            return [
                update_tool_call(
                    event.parent_call_id,
                    status="in_progress",
                    raw_output=f"Sub-agent {event.agent_name} started.",
                    content=[_text_tool_content(f"Sub-agent {event.agent_name} started.")],
                )
            ]
        if isinstance(event, InvocationToolCallStart):
            return self._sub_agent_note(
                event.origin.invocation_id,
                f"Sub-agent {event.agent_name} started tool {event.tool_name}.",
            )
        if isinstance(event, InvocationToolCallArgsUpdated):
            return self._sub_agent_note(
                event.origin.invocation_id,
                f"Sub-agent {event.agent_name} updated tool {event.tool_name} arguments.",
            )
        if isinstance(event, InvocationToolCallProgress):
            text = "\n".join(event.lines) or f"Sub-agent {event.agent_name} tool {event.tool_name} is running."
            if event.provider_hosted:
                image_blocks = _hosted_image_blocks(event.image_contents)
                if image_blocks:
                    self._sub_agent_hosted_blocks.setdefault(event.origin.invocation_id, {})[event.call_id] = (
                        image_blocks
                    )
            return self._sub_agent_note(event.origin.invocation_id, text)
        if isinstance(event, InvocationToolCallStatusUpdated):
            return self._sub_agent_note(
                event.origin.invocation_id,
                f"Sub-agent {event.agent_name} tool {event.tool_name}: {event.status}.",
            )
        if isinstance(event, InvocationToolCallResult):
            if event.provider_hosted:
                # The terminal payload is authoritative for its call: replace
                # any partial snapshot, or clear it when nothing structured
                # survived to the terminal result.
                result_blocks = _hosted_image_blocks(event.image_contents) + _hosted_artifact_blocks(event.artifacts)
                calls = self._sub_agent_hosted_blocks.setdefault(event.origin.invocation_id, {})
                if result_blocks:
                    calls[event.call_id] = result_blocks
                else:
                    calls.pop(event.call_id, None)
            # Each note replaces the parent card's content, and a nested result can be a whole file: the card
            # shows its first line, and the extension notification carries the full result.
            preview = _result_preview(event.result)
            return self._sub_agent_note(
                event.origin.invocation_id,
                f"Sub-agent {event.agent_name} completed tool {event.tool_name}" + (f": {preview}" if preview else "."),
            )
        if isinstance(event, InvocationProgress):
            usage_parts: list[str] = []
            if event.total_usage_tokens:
                usage_parts.append(f"{event.total_usage_tokens} total token(s)")
            if event.usage_unreported_attempts:
                usage_parts.append(f"{event.usage_unreported_attempts} unreported attempt(s)")
            usage = f", {', '.join(usage_parts)}" if usage_parts else ""
            return self._sub_agent_note(
                event.origin.invocation_id,
                (
                    f"Sub-agent {event.agent_name}: {event.tool_call_count} tool call(s), "
                    f"{event.total_tokens} context token(s){usage}."
                ),
            )
        if isinstance(event, InvocationRetryAttempt):
            return self._sub_agent_note(
                event.origin.invocation_id,
                (f"Sub-agent {event.agent_name} retry {event.attempt}/{event.max_attempts}: {event.message}"),
            )
        if isinstance(event, InvocationCompactionStarted):
            return self._sub_agent_note(
                event.origin.invocation_id,
                f"Sub-agent {event.agent_name} compacting conversation...",
            )
        if isinstance(event, InvocationCompactionFinished):
            if event.outcome == "ok":
                text = f"Sub-agent {event.agent_name} compacted conversation."
                if event.format_violation:
                    text += f" Summary format warning: {event.format_violation}."
            elif event.outcome == "canceled":
                text = f"Sub-agent {event.agent_name} compaction interrupted."
            elif event.failure_reason:
                text = f"Sub-agent {event.agent_name} compaction failed ({event.failure_reason})."
            else:
                text = f"Sub-agent {event.agent_name} compaction failed."
            return self._sub_agent_note(event.origin.invocation_id, text)
        if isinstance(event, InvocationCompactionCommitted):
            # Deliberately silent in the human-facing note stream — the
            # finished note above already narrated the outcome.  Structured
            # consumers distinguish committed from abandoned rounds via the
            # ACP server's chrys/sub_agent_compaction_committed extension
            # notification instead.
            return []
        if isinstance(event, InvocationPaused):
            return self._sub_agent_note(
                event.origin.invocation_id,
                f"Sub-agent {event.agent_name} paused: {event.last_error or event.reason}",
            )
        if isinstance(event, InvocationResumed):
            return self._sub_agent_note(event.origin.invocation_id, f"Sub-agent {event.agent_name} resumed.")
        if isinstance(event, InvocationAborted):
            updates = self._sub_agent_note(
                event.origin.invocation_id,
                f"Sub-agent {event.agent_name} aborted: {event.last_error}",
                status="failed",
            )
            self._sub_agent_parents.pop(event.origin.invocation_id, None)
            self._sub_agent_hosted_blocks.pop(event.origin.invocation_id, None)
            return updates
        if isinstance(event, InvocationCascadeAborted):
            updates = self._sub_agent_note(
                event.origin.invocation_id,
                f"Sub-agent {event.agent_name} cancelled.",
                status="failed",
            )
            self._sub_agent_parents.pop(event.origin.invocation_id, None)
            self._sub_agent_hosted_blocks.pop(event.origin.invocation_id, None)
            return updates
        return []

    def _sub_agent_note(
        self,
        invocation_id: str,
        text: str,
        *,
        status: acp_schema.ToolCallStatus | None = "in_progress",
    ) -> list[SessionUpdate]:
        parent_call_id = self._sub_agent_parents.get(invocation_id)
        if not parent_call_id:
            return []
        # Every note replaces the parent card's whole content collection, so
        # the accumulated hosted blocks must ride along or this note wipes
        # them.  Scope strictly to this invocation: parent call_ids are
        # per-run counters and can collide across concurrent sub-agents.
        return [
            update_tool_call(
                parent_call_id,
                status=status,
                raw_output=text,
                content=[_text_tool_content(text), *self._sub_agent_blocks_for_invocation(invocation_id)],
            )
        ]

    def _sub_agent_blocks_for_invocation(self, invocation_id: str) -> list[Any]:
        blocks: list[Any] = []
        for call_blocks in self._sub_agent_hosted_blocks.get(invocation_id, {}).values():
            blocks.extend(call_blocks)
        return blocks

    def _agent_message_delta(self, event: InvocationMessage) -> str:
        text = event.text
        if not text:
            return ""
        if event.is_intermediate:
            self._last_streaming_agent_text = ""
            return text
        if self._last_streaming_agent_text and not text.startswith(self._last_streaming_agent_text):
            logger.warning("ACP agent message stream was not cumulative; emitting full snapshot text.")
            self._last_streaming_agent_text = "" if event.is_final else text
            return text
        delta = text.removeprefix(self._last_streaming_agent_text) if self._last_streaming_agent_text else text
        self._last_streaming_agent_text = "" if event.is_final else text
        return delta


def _text_tool_content(text: str) -> acp_schema.ContentToolCallContent:
    return tool_content(acp_schema.TextContentBlock(type="text", text=text))


_TITLE_MAX_CHARS = 160


def _clamp_title(text: str) -> str:
    return text if len(text) <= _TITLE_MAX_CHARS else text[: _TITLE_MAX_CHARS - 1] + "…"


def _result_preview(result: str) -> str:
    """A nested tool result's first non-empty line, clamped like a title."""
    return _clamp_title(next((line.strip() for line in result.splitlines() if line.strip()), ""))


def tool_call_title(tool_name: str, tool_kind: str, args: object, *, intent_summary: str = "") -> str:
    """Human title for a tool call surfaced to ACP clients.

    Shell calls are titled by the command itself (first non-empty line,
    clamped) per the convention established ACP agents follow, so clients
    render something informative instead of a generic verb + tool name —
    the shell tool's name is the bare shell binary ("zsh"), which carries
    no information. Truncation only ever drops characters PAST position
    120: a chrys parent's approval dialog probes the first 120 title chars
    for containment in the arg values to suppress a title that merely
    repeats the ``command`` argument, and an ellipsis inside the probe
    window would defeat that dedup.
    """
    if summary := intent_summary.strip():
        return _clamp_title(" ".join(summary.split()))
    if isinstance(args, dict):
        if tool_kind == KIND_SHELL:
            command = args.get("command")
            if isinstance(command, str):
                first_line = next((line.strip() for line in command.splitlines() if line.strip()), "")
                if first_line:
                    return _clamp_title(first_line)
        elif tool_kind in {KIND_FILESYSTEM_READ, KIND_FILESYSTEM_WRITE}:
            path = args.get("path")
            if isinstance(path, str) and path.strip():
                return _clamp_title(path.strip())
    return _clamp_title(tool_name.replace("_", " ").strip()) or "Tool call"


def acp_tool_kind(tool_kind: str) -> acp_schema.ToolKind:
    """Map a Chrys tool kind to the closest ACP presentation kind."""
    if tool_kind == KIND_FILESYSTEM_READ:
        return "read"
    if tool_kind == KIND_FILESYSTEM_WRITE:
        return "edit"
    if tool_kind == KIND_SHELL:
        return "execute"
    if tool_kind == KIND_SEARCH:
        return "search"
    if tool_kind == KIND_WEB_SEARCH:
        return "search"
    if tool_kind == KIND_WEB_FETCH:
        return "fetch"
    if tool_kind == KIND_SUB_AGENT:
        return "other"
    if tool_kind in {KIND_ASK_USER, KIND_MCP, KIND_DOC_CONVERTER}:
        return "other"
    return "other"
