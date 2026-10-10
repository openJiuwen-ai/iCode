# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP update translation and permission protocols owned by the ACP backend."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import logging
import math
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from acp.exceptions import RequestError
from acp.schema import (
    AgentMessageChunk,
    AudioContentBlock,
    ContentToolCallContent,
    EmbeddedResourceContentBlock,
    FileEditToolCallContent,
    ImageContentBlock,
    PermissionOption,
    ResourceContentBlock,
    SessionNotification,
    TerminalToolCallContent,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
)

from chrys.foundation.events.types import (
    ApprovalCancelled,
    ApprovalRequest,
    ApprovalResponse,
    AskUserResponse,
    AskUserTimedOut,
    InvocationMessage,
    InvocationProgress,
    InvocationToolCallResult,
    InvocationToolCallStart,
    QuestionToUser,
)
from chrys.foundation.models.ask_user import Cancelled as AskUserCancelled
from chrys.foundation.models.ask_user import (
    parse_ask_user_answers,
    validate_request_input_params,
    validate_request_input_response,
)
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.text.images import MAX_IMAGE_BYTES
from chrys.foundation.tool_result_metadata import TOOL_FAILED_METADATA_KEY, TOOL_INTERRUPTED_METADATA_KEY
from chrys.foundation.trajectory.context import TrajectoryContext
from chrys.foundation.trajectory.event_types import WaitCategory
from chrys.kernel import Content
from chrys.service.acp_client.errors import (
    AcpTransportError,
)
from chrys.service.acp_client.spec import PermissionDecision
from chrys.service.approval.arbitration import ApprovalDecisionArbiter, ApprovalJudgeInput
from chrys.service.approval.correlation import OneShotCorrelation
from chrys.service.approval.policy import ApprovalMode
from chrys.service.trajectory.approvals import ApprovalDecider, ApprovalTrace
from chrys.service.trajectory.waits import WaitOutcome, WaitTrace

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus
    from chrys.service.approval.judge import ApprovalJudge
    from chrys.service.approval.turn_context import TurnContextReader

logger = logging.getLogger(__name__)

_MAX_CALLS = 5_000
_MAX_FIELD_CHARS = 2_000
_MAX_VALUE_DEPTH = 16
_MAX_COLLECTION_ITEMS = 256
_AUDIT_RING_ITEMS = 500
_AUDIT_RING_BYTES = 256 * 1024
_ALLOW_KINDS = ("allow_once", "allow_always")
_REJECT_KINDS = ("reject_once", "reject_always")
_MAX_BASE64_IMAGE_CHARS = ((MAX_IMAGE_BYTES + 2) // 3) * 4
# A maximum-size accepted image expands to 4 MiB in base64. Keep one
# additional MiB for its data-URI prefix and the surrounding bounded record.
_MAX_ASSEMBLED_BYTES = _MAX_BASE64_IMAGE_CHARS + 1024 * 1024


def display_tool_kind(kind: str | None) -> str:
    """Map ACP presentation kinds to Chrys display/judge kinds."""
    if kind == "execute":
        return "shell"
    if kind == "read":
        return "filesystem.read"
    if kind in {"edit", "delete", "move"}:
        return "filesystem.write"
    if kind == "search":
        return "search"
    return ""


def preview_text(value: Any, *, limit: int = _MAX_FIELD_CHARS) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        except Exception:
            text = str(value)
    if len(text) <= limit:
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"{text[: limit - 28]}… [sha256:{digest}]"


def _bounded_value(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON-safe bounded value before it enters events or persistence."""
    if depth >= _MAX_VALUE_DEPTH:
        return "[depth limit]"
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        rendered = str(value)
        return value if len(rendered) <= _MAX_FIELD_CHARS else preview_text(rendered)
    if type(value) is float:
        return value if math.isfinite(value) else preview_text(value)
    if isinstance(value, str):
        return preview_text(value)
    if hasattr(value, "model_dump"):
        return _bounded_value(value.model_dump(mode="json", by_alias=True), depth=depth)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= _MAX_COLLECTION_ITEMS:
                result["…"] = "[item limit]"
                break
            result[preview_text(key, limit=200)] = _bounded_value(item, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [_bounded_value(item, depth=depth + 1) for item in value[:_MAX_COLLECTION_ITEMS]]
    return preview_text(value)


def canonical_raw_input(value: Any) -> dict[str, Any]:
    bounded = _bounded_value(value)
    if isinstance(bounded, dict):
        return bounded
    return {"input": preview_text(bounded)}


def _raw_args_dict(value: Any) -> dict[str, Any]:
    """Dict-shaped raw arguments for approval/judge inputs — never truncated.

    The judge and the approval dialog must see the arguments the remote will
    execute (§9.2): a preview cap here would let a destructive suffix hide
    beyond the truncation point and be approved unseen. Size is already
    bounded upstream — permission frames ride the client's per-payload caps
    before SDK routing — so the untruncated form is safe to publish.
    canonical_raw_input stays for retained records and audit appends (§7.4).
    """
    if isinstance(value, Mapping):
        return dict(value)
    return {"input": value}


def _content_block_text(block: Any) -> str:
    if isinstance(block, TextContentBlock):
        return block.text
    if isinstance(block, ResourceContentBlock):
        return f"[resource: {block.uri}]"
    if isinstance(block, EmbeddedResourceContentBlock):
        resource = block.resource
        uri = getattr(resource, "uri", "")
        return f"[resource: {uri}]" if uri else "[resource]"
    if isinstance(block, ImageContentBlock | AudioContentBlock):
        return ""
    return preview_text(block)


def _structured_tool_content(content: Sequence[Any]) -> tuple[list[Content], list[dict[str, Any]]]:
    """Extract images and resource descriptors before bounded text projection."""
    image_contents: list[Content] = []
    artifacts: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, ContentToolCallContent):
            continue
        block = item.content
        if isinstance(block, ImageContentBlock):
            media_type = block.mime_type.strip().lower() or "image/png"
            if block.data:
                if len(block.data) > _MAX_BASE64_IMAGE_CHARS:
                    raise AcpTransportError("The ACP agent returned an image that exceeds the supported size limit.")
                try:
                    data = base64.b64decode(block.data, validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise AcpTransportError("The ACP agent returned invalid base64 image content.") from exc
                if not data:
                    raise AcpTransportError("The ACP agent returned empty image content.")
                if len(data) > MAX_IMAGE_BYTES:
                    raise AcpTransportError("The ACP agent returned an image that exceeds the supported size limit.")
                image_contents.append(Content.from_data(data=data, media_type=media_type))
            elif block.uri:
                image_contents.append(Content.from_uri(uri=block.uri, media_type=media_type))
            continue
        if isinstance(block, ResourceContentBlock):
            descriptor: dict[str, Any] = {"name": block.name, "path": block.uri}
            if block.mime_type:
                descriptor["mime"] = block.mime_type
            if block.size is not None:
                descriptor["size"] = block.size
            artifacts.append(descriptor)
            continue
        if isinstance(block, EmbeddedResourceContentBlock):
            resource = block.resource
            uri = resource.uri
            descriptor = {"name": uri.rstrip("/").rsplit("/", 1)[-1] or uri, "path": uri}
            if resource.mime_type:
                descriptor["mime"] = resource.mime_type
            artifacts.append(descriptor)
    return image_contents, artifacts


def _tool_content_text(content: Sequence[Any] | None) -> str:
    rendered: list[str] = []
    for item in content or ():
        if isinstance(item, ContentToolCallContent):
            text = _content_block_text(item.content)
        elif isinstance(item, FileEditToolCallContent):
            text = f"edited {item.path}"
        elif isinstance(item, TerminalToolCallContent):
            text = f"[terminal {item.terminal_id}]"
        else:
            text = preview_text(item)
        if text:
            rendered.append(text)
    return preview_text("\n".join(rendered))


@dataclass(slots=True)
class _CallRecord:
    local_call_id: str
    title: str = "tool"
    kind: str = ""
    raw_input: dict[str, Any] = field(default_factory=dict)
    raw_output: Any = None
    content: list[Any] = field(default_factory=list)
    image_contents: list[Content] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    locations: list[Any] = field(default_factory=list)
    status: str = ""
    started_at: float = field(default_factory=time.monotonic)
    start_published: bool = False
    terminal_published: bool = False
    last_start_signature: str = ""
    provider_hosted: bool = False
    hosted_family: str = ""
    provider: str = ""
    provider_item_type: str = ""
    provider_call_id: str = ""
    provider_status: str = ""


def _explicit_hosted_extension(update: ToolCallStart | ToolCallProgress) -> dict[str, Any]:
    """Read hosted presentation metadata only when the remote explicitly sends it."""
    metadata = update.field_meta or {}
    chrys_meta = metadata.get("chrys")
    if not isinstance(chrys_meta, Mapping):
        return {}
    family = chrys_meta.get("hosted_family", chrys_meta.get("hostedFamily"))
    provider_hosted = chrys_meta.get("provider_hosted", chrys_meta.get("providerHosted")) is True
    if not provider_hosted and not isinstance(family, str):
        return {}

    # These strings ride every event about the call and are kept with it, and
    # the client caps no string in an update but its ids, so they are bounded
    # like the title.
    extension: dict[str, Any] = {"provider_hosted": provider_hosted}
    if isinstance(family, str):
        extension["hosted_family"] = preview_text(family)

    def _copy_text_value(target_key: str, snake_key: str, camel_key: str) -> None:
        value = chrys_meta.get(snake_key, chrys_meta.get(camel_key))
        if isinstance(value, str):
            extension[target_key] = preview_text(value)

    _copy_text_value("provider", "provider", "provider")
    _copy_text_value("provider_item_type", "provider_item_type", "providerItemType")
    _copy_text_value("provider_call_id", "provider_call_id", "providerCallId")
    _copy_text_value("provider_status", "provider_status", "providerStatus")
    return extension


class AcpUpdateTranslator:
    """Merge ACP partial updates into bounded Chrys events and final text."""

    def __init__(
        self,
        *,
        event_bus: EventBus | None,
        session_id: str | None,
        agent_name: str,
        invocation_id: str,
        attempt: int,
        origin: InvocationOrigin,
        result_mode: Literal["last_segment", "transcript"] = "last_segment",
        completed_before: int = 0,
        spend_before: int = 0,
        unreported_before: int = 0,
    ) -> None:
        self._bus = event_bus
        self._session_id = session_id
        self.origin = origin
        self.agent_name = agent_name
        self.invocation_id = invocation_id
        self.attempt = attempt
        self.result_mode = result_mode
        self.calls: dict[str, _CallRecord] = {}
        self._current_call_ids: dict[str, str] = {}
        self._call_occurrences: dict[str, int] = {}
        self._next_fallback_call_id = 1
        self._completed_before = completed_before
        self._completed_here = 0
        self._spend = spend_before
        self._unreported = unreported_before
        self.latest_context_used = 0
        self.latest_context_size: int | None = None
        self._segments: list[str] = []
        self._open_segment = ""
        self._text_finalized = False
        self._unpublished_final_text = ""
        self._message_id: str | None = None
        self._retained_bytes = 0
        self._retained_bytes_by_call: dict[str, int] = {}
        self._audit: deque[dict[str, Any]] = deque()
        # Include the JSON list delimiters so the persisted ring itself,
        # rather than only the sum of its entries, stays within the cap.
        self._audit_bytes = 2
        self.on_audit_changed: Callable[[], None] | None = None

    @property
    def completed_count(self) -> int:
        return self._completed_before + self._completed_here

    @property
    def translated_updates(self) -> list[dict[str, Any]]:
        return list(self._audit)

    async def put(self, seq: int, notification: SessionNotification) -> None:
        await self.on_update(seq, notification)

    async def on_update(self, seq: int, notification: SessionNotification) -> None:
        update = notification.update
        self._append_audit(seq, update)
        if isinstance(update, ToolCallStart | ToolCallProgress):
            # Only a ToolCallStart seals the open segment (§7.5): progress
            # merges tool state and must not split an assistant message that
            # keeps streaming around a running call — last_segment would
            # otherwise return just the suffix.
            if isinstance(update, ToolCallStart):
                await self._seal_segment(publish=True)
            await self._merge_tool(update)
        elif isinstance(update, AgentMessageChunk):
            await self._consume_message(update)
        elif isinstance(update, UsageUpdate):
            # PR-1 validates raw exact-int usage before SDK coercion. Keep this
            # defensive check for direct translator tests and custom sinks.
            if type(update.used) is int and type(update.size) is int and update.used >= 0 and update.size >= 0:
                self.latest_context_used = update.used
                self.latest_context_size = update.size
                await self.publish_progress()

    def _append_audit(self, seq: int, update: Any) -> None:
        self._append_audit_item({"seq": seq, "update": _bounded_value(update.model_dump(mode="json", by_alias=True))})

    def record_permission_request(self, *, title: str, kind: str, raw_input: Any, outcome: str) -> None:
        """Append a permission request to the audit ring.

        ACP permits ``session/request_permission`` before any ``ToolCallStart``;
        without this entry a standalone request's rawInput would reach no
        durable artifact (judge logging is deliberately ``log_dir=None``, §9.2
        — the owner-only envelope is the place remote rawInput is audited).
        """
        self._append_audit_item(
            {
                "seq": None,
                "update": {
                    "sessionUpdate": "permission_request",
                    "title": preview_text(title),
                    "kind": preview_text(kind),
                    "rawInput": _bounded_value(raw_input),
                    "outcome": outcome,
                },
            }
        )

    def _append_audit_item(self, item: dict[str, Any]) -> None:
        item["attempt"] = self.attempt
        size = len(json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str).encode())
        if size > _AUDIT_RING_BYTES:
            item = {"attempt": self.attempt, "seq": item.get("seq"), "update": preview_text(item)}
            size = len(json.dumps(item, ensure_ascii=False).encode())
        added = size + (1 if self._audit else 0)
        while self._audit and (len(self._audit) >= _AUDIT_RING_ITEMS or self._audit_bytes + added > _AUDIT_RING_BYTES):
            removed = self._audit.popleft()
            removed_size = len(json.dumps(removed, ensure_ascii=False, separators=(",", ":")).encode())
            self._audit_bytes -= removed_size + (1 if self._audit else 0)
            added = size + (1 if self._audit else 0)
        if self._audit_bytes + added <= _AUDIT_RING_BYTES:
            self._audit.append(item)
            self._audit_bytes += added
            if self.on_audit_changed is not None:
                self.on_audit_changed()

    def _charge(self, value: Any) -> None:
        self._retained_bytes += len(json.dumps(value, ensure_ascii=False, default=str).encode())
        if self._retained_bytes > _MAX_ASSEMBLED_BYTES:
            raise AcpTransportError("The ACP agent exceeded the translated update budget.")

    def _charge_tool_update(self, call_id: str, value: Any, image_uris: Sequence[str]) -> None:
        # A call's record keeps only its latest state, so each update's charge
        # replaces the one the call's previous update made.
        call_bytes = len(json.dumps(value, ensure_ascii=False, default=str).encode())
        if image_uris:
            call_bytes += len(json.dumps(image_uris, ensure_ascii=False).encode())
        retained_bytes = self._retained_bytes - self._retained_bytes_by_call.get(call_id, 0) + call_bytes
        if retained_bytes > _MAX_ASSEMBLED_BYTES:
            raise AcpTransportError("The ACP agent exceeded the translated update budget.")
        self._retained_bytes = retained_bytes
        self._retained_bytes_by_call[call_id] = call_bytes

    async def _merge_tool(self, update: ToolCallStart | ToolCallProgress) -> None:
        remote_id = update.tool_call_id
        local_id = self._current_call_ids.get(remote_id, "")
        record = self.calls.get(local_id)
        status = getattr(update, "status", None)
        starts_new_occurrence = record is None or (
            record.terminal_published
            and (
                isinstance(update, ToolCallStart) or (isinstance(status, str) and status not in {"completed", "failed"})
            )
        )
        if starts_new_occurrence:
            if len(self.calls) >= _MAX_CALLS:
                raise AcpTransportError("The ACP agent exceeded the tool-call update budget.")
            occurrence = self._call_occurrences.get(remote_id, 0) + 1
            self._call_occurrences[remote_id] = occurrence
            preferred_id = f"a{self.attempt}:{remote_id}" + (f":{occurrence}" if occurrence > 1 else "")
            local_id = preferred_id
            while local_id in self.calls:
                local_id = f"a{self.attempt}:occurrence:{self._next_fallback_call_id}"
                self._next_fallback_call_id += 1
            record = _CallRecord(local_call_id=local_id)
            self.calls[local_id] = record
            self._current_call_ids[remote_id] = local_id

        title = getattr(update, "title", None)
        kind = getattr(update, "kind", None)
        raw_input = getattr(update, "raw_input", None)
        raw_output = getattr(update, "raw_output", None)
        content = getattr(update, "content", None)
        locations = getattr(update, "locations", None)
        if title is not None:
            record.title = preview_text(title) or "tool"
        if kind is not None:
            record.kind = display_tool_kind(kind)
        if raw_input is not None:
            record.raw_input = canonical_raw_input(raw_input)
        if raw_output is not None:
            record.raw_output = _bounded_value(raw_output)
        if content is not None:
            record.image_contents, record.artifacts = _structured_tool_content(content)
            record.content = list(_bounded_value(content) or [])
        if locations is not None:
            record.locations = list(_bounded_value(locations) or [])
        if status is not None:
            record.status = status
        hosted_extension = _explicit_hosted_extension(update)
        if hosted_extension:
            record.provider_hosted = bool(hosted_extension["provider_hosted"])
            if "hosted_family" in hosted_extension:
                record.hosted_family = str(hosted_extension["hosted_family"])
            if "provider" in hosted_extension:
                record.provider = str(hosted_extension["provider"])
            if "provider_item_type" in hosted_extension:
                record.provider_item_type = str(hosted_extension["provider_item_type"])
            if "provider_call_id" in hosted_extension:
                record.provider_call_id = str(hosted_extension["provider_call_id"])
            if "provider_status" in hosted_extension:
                record.provider_status = str(hosted_extension["provider_status"])
        self._charge_tool_update(
            record.local_call_id,
            {
                "title": record.title,
                "kind": record.kind,
                "input": record.raw_input,
                "output": record.raw_output,
                "content": record.content,
                "artifacts": record.artifacts,
                "locations": record.locations,
            },
            [image.uri or "" for image in record.image_contents],
        )

        await self._publish_start(record)
        if record.status in {"completed", "failed"}:
            await self._publish_terminal(record)

    async def _publish_start(self, record: _CallRecord, *, ensure: bool = False) -> None:
        # The record keeps a digest of what it published: a finished call's
        # input can be replaced by a smaller one, and the record must then keep
        # no more than its latest update is charged for.
        signature = hashlib.sha256(
            json.dumps([record.title, record.kind, record.raw_input], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if not ensure and (
            record.terminal_published or (record.start_published and signature == record.last_start_signature)
        ):
            return
        record.start_published = True
        record.last_start_signature = signature
        if self._bus is not None:
            await self._bus.publish(
                InvocationToolCallStart(
                    origin=self.origin,
                    agent_name=self.agent_name,
                    tool_name=record.title,
                    tool_kind=record.kind,
                    args=dict(record.raw_input),
                    call_id=record.local_call_id,
                    provider_hosted=record.provider_hosted,
                    hosted_family=record.hosted_family,
                    provider=record.provider,
                    provider_item_type=record.provider_item_type,
                    provider_call_id=record.provider_call_id,
                    provider_status=record.provider_status,
                    session_id=self._session_id,
                )
            )

    async def _publish_terminal(self, record: _CallRecord, *, interrupted: bool = False) -> None:
        if record.terminal_published:
            return
        # Claim the terminal synchronously, before any await. The live update
        # consumer and a cancel-path flush_interrupted() run as separate tasks;
        # if the guard above and the claim below straddled an await (the
        # _publish_start / bus.publish points), both could pass the guard and
        # double-deliver a InvocationToolCallResult (and double-count). Claiming
        # up front serializes them — the loser returns at the guard. The
        # ensure=True start below intentionally bypasses this same flag.
        record.terminal_published = True
        self._completed_here += 1
        try:
            await self._publish_start(record, ensure=True)
            if interrupted:
                result = "(interrupted)"
                metadata = {TOOL_INTERRUPTED_METADATA_KEY: True}
                provider_status = "interrupted"
            else:
                result = _tool_content_text_from_bounded(record.content)
                if not result:
                    result = preview_text(record.raw_output)
                failed = record.status == "failed"
                metadata = {TOOL_FAILED_METADATA_KEY: True} if failed else {}
                provider_status = record.provider_status
            if self._bus is not None:
                await self._bus.publish(
                    InvocationToolCallResult(
                        origin=self.origin,
                        agent_name=self.agent_name,
                        tool_name=record.title,
                        call_id=record.local_call_id,
                        result=result,
                        image_contents=list(record.image_contents),
                        duration_ms=max(0, int((time.monotonic() - record.started_at) * 1_000)),
                        metadata=metadata,
                        artifacts=[dict(artifact) for artifact in record.artifacts],
                        provider_hosted=record.provider_hosted,
                        hosted_family=record.hosted_family,
                        provider=record.provider,
                        provider_item_type=record.provider_item_type,
                        provider_call_id=record.provider_call_id,
                        provider_status=provider_status,
                        session_id=self._session_id,
                    )
                )
        except asyncio.CancelledError:
            # A cancellation mid-publish must not eat the terminal: release the
            # claim so the cancel path's flush_interrupted() re-delivers (a
            # duplicate for early subscribers beats a permanently dangling card).
            record.terminal_published = False
            self._completed_here -= 1
            raise
        await self.publish_progress()

    async def flush_interrupted(self) -> None:
        for record in list(self.calls.values()):
            if not record.terminal_published:
                await self._publish_terminal(record, interrupted=True)

    async def _consume_message(self, update: AgentMessageChunk) -> None:
        if update.message_id is not None and self._message_id is not None and update.message_id != self._message_id:
            await self._seal_segment(publish=True)
        if update.message_id is not None:
            self._message_id = update.message_id
        text = _content_block_text(update.content)
        if text:
            self._charge(text)
            self._open_segment += text

    def _take_open_segment(self) -> str:
        segment = self._open_segment
        self._open_segment = ""
        if segment.strip():
            self._segments.append(segment)
        return segment

    async def _seal_segment(self, *, publish: bool) -> None:
        segment = self._take_open_segment()
        if publish and segment.strip() and self._bus is not None:
            await self._bus.publish(
                InvocationMessage(
                    is_final=False,
                    is_intermediate=True,
                    origin=self.origin,
                    agent_name=self.agent_name,
                    text=segment,
                    session_id=self._session_id,
                )
            )

    def result_text(self) -> str:
        self._finalize_text()
        segments = [segment for segment in self._segments if segment.strip()]
        if not segments:
            return ""
        if self.result_mode == "transcript":
            return "\n\n".join(segments)
        return segments[-1]

    def unpublished_final_text(self) -> str:
        """Return the final segment that was not emitted as an intermediate event."""
        self._finalize_text()
        return self._unpublished_final_text

    def _finalize_text(self) -> None:
        if self._text_finalized:
            return
        self._text_finalized = True
        segment = self._take_open_segment()
        self._unpublished_final_text = segment if segment.strip() else ""

    def partial_text(self) -> str:
        segments = [*self._segments]
        if self._open_segment.strip():
            segments.append(self._open_segment)
        return "\n\n".join(segment for segment in segments if segment.strip())

    async def finalize_usage(self, *, spend: int, unreported_attempts: int) -> None:
        self._spend = spend
        self._unreported = unreported_attempts
        await self.publish_progress()

    async def publish_progress(self) -> None:
        if self._bus is not None:
            await self._bus.publish(
                InvocationProgress(
                    origin=self.origin,
                    agent_name=self.agent_name,
                    tool_call_count=self.completed_count,
                    total_tokens=self.latest_context_used,
                    total_usage_tokens=self._spend,
                    usage_unreported_attempts=self._unreported,
                    session_id=self._session_id,
                )
            )


def _tool_content_text_from_bounded(content: Sequence[Any]) -> str:
    rendered: list[str] = []
    for item in content:
        if not isinstance(item, Mapping):
            rendered.append(preview_text(item))
            continue
        item_type = item.get("type")
        if item_type == "content":
            block = item.get("content")
            if isinstance(block, Mapping):
                if block.get("type") == "text":
                    rendered.append(str(block.get("text", "")))
                elif block.get("type") == "resource_link":
                    rendered.append(f"[resource: {block.get('uri', '')}]")
                elif block.get("type") == "resource":
                    rendered.append("[resource]")
        elif item_type == "diff":
            rendered.append(f"edited {item.get('path', '')}")
        elif item_type == "terminal":
            rendered.append(f"[terminal {item.get('terminalId', item.get('terminal_id', ''))}]")
    return preview_text("\n".join(part for part in rendered if part))


class AcpPermissionBroker:
    """Translate remote permissions and ask-user requests into Chrys events."""

    def __init__(
        self,
        *,
        event_bus: EventBus,
        session_id: str | None,
        caller_name: str,
        mode_getter: Callable[[], ApprovalMode],
        turn_context: TurnContextReader,
        workspace_roots: Sequence[str],
        workspace_cwd: str,
        approval_judge: ApprovalJudge | None,
        ask_user_timeout_seconds: float | None,
        allow_user_interaction: bool = True,
        trajectory_context: TrajectoryContext | None = None,
        trajectory_boundary_operation_id: str | None = None,
    ) -> None:
        self._bus = event_bus
        self._session_id = session_id
        self._caller_name = caller_name
        self._mode_getter = mode_getter
        self._turn_context = turn_context
        self._workspace_roots = list(workspace_roots)
        self._workspace_cwd = workspace_cwd
        self._judge = approval_judge
        self._ask_timeout = ask_user_timeout_seconds
        self._allow_user_interaction = allow_user_interaction
        self._trajectory_context = trajectory_context
        self._trajectory_boundary_operation_id = trajectory_boundary_operation_id
        self._translator: AcpUpdateTranslator | None = None
        self._arbiter = ApprovalDecisionArbiter(event_bus, session_id=session_id)
        self._permission_waits: dict[str, asyncio.Future[ApprovalResponse]] = {}
        self._ask_waits: dict[str, asyncio.Future[AskUserResponse]] = {}
        # Latched by cascade_abort() synchronously, ahead of the backgrounded
        # cancel_pending_waits(). Any in-flight or freshly-arriving permission
        # decision consults it so an already-settled "allow" cannot slip
        # through after the global interrupt.
        self._aborted = False

    def mark_aborted(self) -> None:
        """Latch the abort flag synchronously (called from cascade_abort)."""
        self._aborted = True

    def set_translator(self, translator: AcpUpdateTranslator) -> None:
        self._translator = translator

    def bind_trajectory(self, context: TrajectoryContext | None, *, boundary_operation_id: str | None) -> None:
        """Bind the caller's current pass before any remote permission requests arrive."""
        self._trajectory_context = context
        self._trajectory_boundary_operation_id = boundary_operation_id

    async def on_update(self, seq: int, notification: SessionNotification) -> None:
        translator = self._translator
        if translator is not None:
            await translator.on_update(seq, notification)

    async def on_permission_request(
        self,
        tool_call: ToolCallUpdate,
        options: Sequence[PermissionOption],
    ) -> PermissionDecision:
        # Every permission request lands in the owner-only audit ring with its
        # outcome — including ones the remote sends before any ToolCallStart,
        # which would otherwise reach no durable artifact.
        decision = PermissionDecision.cancelled()
        try:
            decision = await self._decide_permission(tool_call, options)
            return decision
        finally:
            self._record_permission_audit(tool_call, decision)

    def _record_permission_audit(self, tool_call: ToolCallUpdate, decision: PermissionDecision) -> None:
        translator = self._translator
        if translator is None:
            return
        outcome = {"allow": "allowed", "deny": "denied"}.get(decision.action, "cancelled")
        translator.record_permission_request(
            title=str(tool_call.title or "tool"),
            kind=display_tool_kind(tool_call.kind),
            raw_input=tool_call.raw_input,
            outcome=outcome,
        )

    async def _decide_permission(
        self,
        tool_call: ToolCallUpdate,
        options: Sequence[PermissionOption],
    ) -> PermissionDecision:
        # A cascade abort already fired: never open a fresh permission — even a
        # BYPASS-mode auto-allow — after the global interrupt.
        if self._aborted:
            return PermissionDecision.cancelled()
        allow = _select_option(options, _ALLOW_KINDS)
        reject = _select_option(options, _REJECT_KINDS)
        # Polarity is checked before every mode, including BYPASS.
        if allow is None:
            return PermissionDecision.deny(reject.option_id) if reject is not None else PermissionDecision.cancelled()
        mode = self._mode_getter()
        if mode == ApprovalMode.BYPASS:
            return PermissionDecision.allow(allow.option_id)

        approval_trace = ApprovalTrace.open_for_operation(
            context=self._trajectory_context,
            target_operation_id=self._trajectory_boundary_operation_id,
        )
        async with OneShotCorrelation(self._bus, ApprovalResponse) as correlation:
            request_id = correlation.request_id
            future = correlation.future
            self._permission_waits[request_id] = future
            judging = mode == ApprovalMode.AUTO and self._judge is not None
            judge_title, judge_kind, raw_args = _permission_judge_fields(tool_call)
            presentation_title = preview_text(tool_call.title or "tool", limit=_MAX_FIELD_CHARS - 4)
            published_args = _raw_args_dict(tool_call.raw_input)
            request_publish_started = False
            try:
                if judging:
                    await self._arbiter.ensure_subscription()
                if approval_trace is not None:
                    await approval_trace.requested(
                        tool_name=f"acp:{presentation_title}",
                        approval_mode=mode.value,
                        approval_level="require",
                    )
                request_publish_started = True
                await self._bus.publish(
                    ApprovalRequest(
                        request_id=request_id,
                        caller_name=self._caller_name,
                        tool_name=f"acp:{presentation_title}",
                        tool_kind="",
                        # judge_kind folds in the chrys _meta enhancement (nested
                        # chrys declares kind precisely); the "remote" fallback
                        # keeps this non-empty for every bridged request so the
                        # dialog renders the friendly header even when the remote
                        # claims no recognizable kind.
                        presentation_kind=judge_kind or "remote",
                        args=published_args,
                        intent_summary=f"Allow remote tool: {presentation_title}",
                        user_message=self._turn_context.user_message,
                        workspace_roots=list(self._workspace_roots),
                        workspace_cwd=self._workspace_cwd,
                        judging=judging,
                        session_id=self._session_id,
                    )
                )
            except asyncio.CancelledError:
                self._permission_waits.pop(request_id, None)
                if approval_trace is not None:
                    approval_trace.interrupted_soon(reason_code="cancelled")
                if request_publish_started:
                    await self._publish_initial_dialog_cleanup(
                        ApprovalCancelled(request_id=request_id, session_id=self._session_id)
                    )
                return PermissionDecision.cancelled()
            except BaseException:
                self._permission_waits.pop(request_id, None)
                if approval_trace is not None:
                    approval_trace.interrupted_soon(reason_code="failed")
                raise
            judge_task: asyncio.Task[None] | None = None
            try:
                if judging:
                    judge_task = asyncio.create_task(
                        self._arbiter.judge(
                            request_id=request_id,
                            judge=self._judge,
                            judge_input=ApprovalJudgeInput(
                                tool_name=judge_title,
                                tool_kind=judge_kind,
                                args=raw_args,
                                user_message=self._turn_context.user_message,
                                user_messages=self._turn_context.user_messages,
                                workspace_roots=list(self._workspace_roots),
                            ),
                            decision_future=future,
                            approved_value=ApprovalResponse(
                                request_id=request_id,
                                approved=True,
                                session_id=self._session_id,
                            ),
                            log_dir=None,
                        ),
                    )
            except BaseException:
                self._permission_waits.pop(request_id, None)
                if approval_trace is not None:
                    approval_trace.interrupted_soon(reason_code="failed")
                raise
            try:
                response = await future
            except asyncio.CancelledError:
                should_notify = self._permission_waits.pop(request_id, None) is not None
                if approval_trace is not None:
                    approval_trace.interrupted_soon(reason_code="cancelled")
                if should_notify:
                    await self._bus.publish(ApprovalCancelled(request_id=request_id, session_id=self._session_id))
                return PermissionDecision.cancelled()
            finally:
                self._permission_waits.pop(request_id, None)
                if judge_task is not None and not judge_task.done():
                    judge_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await judge_task

        # A cascade abort that landed while this permission was in flight must
        # never resolve to allow, even when the ApprovalResponse future was
        # already settled (or its resolving continuation raced ahead of the
        # backgrounded cancel_pending_waits): cascade_abort calls
        # broker.mark_aborted() synchronously before yielding, so by the time
        # `await future` returns the flag is visible. Honoring the approval
        # here would let the remote run a tool after the global interrupt.
        if self._aborted:
            if approval_trace is not None:
                approval_trace.interrupted_soon(reason_code="cascade_abort")
            return PermissionDecision.cancelled()
        if response.approved:
            if approval_trace is not None:
                await approval_trace.resolved(
                    approved=True,
                    decider=ApprovalDecider.USER if correlation.resolved_by_event else ApprovalDecider.JUDGE,
                    reason_code="approved",
                    arguments_modified=False,
                )
            return PermissionDecision.allow(allow.option_id)
        if approval_trace is not None:
            await approval_trace.resolved(
                approved=False,
                decider=ApprovalDecider.USER if correlation.resolved_by_event else ApprovalDecider.JUDGE,
                reason_code="rejected",
                arguments_modified=False,
            )
        return PermissionDecision.deny(reject.option_id) if reject is not None else PermissionDecision.cancelled()

    async def on_ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method != "chrys/request_input":
            raise RequestError.method_not_found(method)
        try:
            questions = validate_request_input_params(params)
        except ValueError as exc:
            raise RequestError.invalid_params({"method": method}) from exc
        if not self._allow_user_interaction:
            # The same cancellation reply serves aborted and expired requests.
            # Do not publish QuestionToUser: noninteractive owners have no
            # response channel to service it.
            return {"cancelled": True}
        caller = params.get("callerName")
        caller_name = preview_text(caller, limit=_MAX_FIELD_CHARS) if isinstance(caller, str) else self._caller_name

        # A cascade abort already fired: do not open a fresh question dialog
        # (symmetric with _decide_permission). Return the same cancellation the
        # timeout/cancel paths use so the remote cannot proceed post-interrupt.
        if self._aborted:
            return {"cancelled": True}
        wait = WaitTrace.open(
            WaitCategory.USER_INPUT,
            target_operation_id=self._trajectory_boundary_operation_id,
            context=self._trajectory_context,
        )
        async with OneShotCorrelation(self._bus, AskUserResponse) as correlation:
            self._ask_waits[correlation.request_id] = correlation.future
            question_publish_started = False
            try:
                if wait is not None:
                    await wait.started()
                question_publish_started = True
                await self._bus.publish(
                    QuestionToUser(
                        questions=questions,
                        request_id=correlation.request_id,
                        caller_name=caller_name or self._caller_name,
                        session_id=self._session_id,
                    )
                )
            except asyncio.CancelledError:
                self._ask_waits.pop(correlation.request_id, None)
                if wait is not None:
                    wait.finished_soon(outcome=WaitOutcome.CANCELLED)
                if question_publish_started:
                    await self._publish_initial_dialog_cleanup(
                        AskUserTimedOut(request_id=correlation.request_id, session_id=self._session_id)
                    )
                return {"cancelled": True}
            except BaseException:
                self._ask_waits.pop(correlation.request_id, None)
                if wait is not None:
                    wait.finished_soon(outcome=WaitOutcome.FAILED)
                raise
            try:
                if self._ask_timeout is None:
                    response = await correlation.future
                else:
                    async with asyncio.timeout(self._ask_timeout):
                        response = await correlation.future
            except TimeoutError:
                if wait is not None:
                    wait.finished_soon(outcome=WaitOutcome.TIMED_OUT)
                await self._bus.publish(AskUserTimedOut(request_id=correlation.request_id, session_id=self._session_id))
                return {"cancelled": True}
            except asyncio.CancelledError:
                should_notify = self._ask_waits.pop(correlation.request_id, None) is not None
                if wait is not None:
                    wait.finished_soon(outcome=WaitOutcome.CANCELLED)
                if should_notify:
                    await self._bus.publish(
                        AskUserTimedOut(
                            request_id=correlation.request_id,
                            session_id=self._session_id,
                        )
                    )
                return {"cancelled": True}
            finally:
                self._ask_waits.pop(correlation.request_id, None)
        # An abort that latched while the answer future was settling (its
        # continuation racing ahead of cancel_pending_waits) must not hand the
        # remote a real answer after the global interrupt.
        if self._aborted:
            if wait is not None:
                await wait.finished(outcome=WaitOutcome.CANCELLED)
            return {"cancelled": True}
        if wait is not None:
            await wait.finished()
        if response.cancelled:
            return {"cancelled": True}
        answers = parse_ask_user_answers(response.answers or (), question_count=len(questions))
        payload: dict[str, Any] = {
            "answers": [{"values": list(answer.values), "note": answer.note} for answer in answers],
            "cancelled": False,
        }
        if isinstance(validate_request_input_response(payload, questions=questions), AskUserCancelled):
            return {"cancelled": True}
        return payload

    async def _publish_initial_dialog_cleanup(self, event: ApprovalCancelled | AskUserTimedOut) -> None:
        """Finish cleanup even when cancellation interrupted the opening publish."""
        task = asyncio.create_task(self._bus.publish(event))
        with contextlib.suppress(asyncio.CancelledError):
            await drain_acp_task(task)

    async def cancel_pending_waits(self) -> None:
        permission_waits = list(self._permission_waits.items())
        ask_waits = list(self._ask_waits.items())
        for request_id, future in permission_waits:
            if self._permission_waits.pop(request_id, None) is None:
                continue
            if not future.done():
                future.cancel()
            await self._bus.publish(ApprovalCancelled(request_id=request_id, session_id=self._session_id))
        for request_id, future in ask_waits:
            if self._ask_waits.pop(request_id, None) is None:
                continue
            if not future.done():
                future.cancel()
            await self._bus.publish(AskUserTimedOut(request_id=request_id, session_id=self._session_id))

    async def close(self) -> None:
        await self.cancel_pending_waits()
        await self._arbiter.close()


def _select_option(
    options: Sequence[PermissionOption],
    preferred_kinds: Sequence[str],
) -> PermissionOption | None:
    for kind in preferred_kinds:
        for option in options:
            if option.kind == kind:
                return option
    return None


def _permission_judge_fields(tool_call: ToolCallUpdate) -> tuple[str, str, dict[str, Any]]:
    title = preview_text(tool_call.title or "tool", limit=_MAX_FIELD_CHARS)
    kind = display_tool_kind(tool_call.kind)
    metadata = tool_call.field_meta or {}
    chrys_meta = metadata.get("chrys")
    if isinstance(chrys_meta, Mapping):
        remote_name = chrys_meta.get("tool_name", chrys_meta.get("toolName"))
        remote_kind = chrys_meta.get("tool_kind", chrys_meta.get("toolKind"))
        if isinstance(remote_name, str) and remote_name:
            title = preview_text(remote_name, limit=_MAX_FIELD_CHARS)
        if isinstance(remote_kind, str):
            kind = preview_text(remote_kind, limit=_MAX_FIELD_CHARS)
    return title, kind, _raw_args_dict(tool_call.raw_input)


async def drain_acp_task(task: asyncio.Future[Any]) -> None:
    """Await *task* to completion, absorbing external cancels of the awaiter.

    ``asyncio.shield`` only stops an external cancel from cancelling the SHIELDED
    task — the awaiter still receives ``CancelledError``, so a single
    ``await asyncio.shield(task)`` abandons *task* the instant a SECOND interrupt
    (or shutdown) lands on the awaiter. Re-await in a loop until the task is
    genuinely done, then re-raise ``CancelledError`` if any external cancel was
    absorbed. A cancel raised inside the task's own body still surfaces via
    ``task.result()``.

    This is the single shield-loop the ACP teardown/flush/cascade-publish paths
    all funnel through, so no future call site can reintroduce the naive
    single-shield hazard.
    """
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError
