# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CompressibleHistoryProvider — History provider with marker-based context compression.

Supports automatic turn markers, stacking context compression, and
recall via sub-agent queries on compressed blocks.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.turns import is_continuation_message
from chrys.foundation.trajectory.metadata import ensure_analytics_item_id
from chrys.foundation.util.time import utc_iso
from chrys.kernel import EXCLUDED_KEY as _EXCLUDED_KEY
from chrys.kernel import (
    TOOL_CALL_CONTENT_TYPES,
    AgentSession,
    HistoryProvider,
    Message,
    SessionContext,
    collect_conversation_handles,
    strip_invalidated_conversation_handles,
)
from chrys.service.context.middleware.usage import contains_provider_hosted_content, resolve_hosted_context_input_tokens
from chrys.service.context.tool_content import tool_call_name
from chrys.service.session.message_metadata import (
    LAST_ASSISTANT_CREATED_AT_STATE_KEY,
    MESSAGE_CREATED_AT_KEY,
    should_stamp_message_created_at,
    stamp_message_created_at,
    try_normalize_created_at,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    from chrys.service.context.compaction import UnifiedContextStrategy


@dataclass
class CompressedBlock:
    """A block of original messages that were compressed into a summary.

    Attributes:
        compressed_context_id: Unique identifier for this block (e.g., ``ctx_a1b2c3d4``).
        messages: Deep copy of the original messages that were compressed.
        summary_text: The summary that replaced them in the active history.
        marker_id: The marker_id up to which compression occurred.
        turn_range: ``(from_turn, to_turn)`` inclusive turn indices.
        created_at: ISO timestamp of when compression occurred.
    """

    compressed_context_id: str
    messages: list[Message] = field(default_factory=list)
    summary_text: str = ""
    marker_id: str = ""
    turn_range: tuple[int, int] = (0, 0)
    created_at: str = ""


# ---------------------------------------------------------------------------
# Sentinel keys for identifying special messages in history
# ---------------------------------------------------------------------------
_TURN_ID_KEY = "_turn_id"
TURN_INDEX_KEY = "_turn"
_TURN_KEY = TURN_INDEX_KEY
TURN_STARTED_AT_KEY = "_turn_started_at"
"""On a turn marker: when the pass it closes began, as the user sent its message or pressed Continue or Retry."""
_BLOCK_ID_KEY = "_block_id"
PRE_OUTPUT_HISTORY_LEN_STATE_KEY = "_chrys_pre_output_history_len"


def _is_turn_marker(msg: Message) -> bool:
    """Return True if the message is a turn marker."""
    return msg.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.TURN


def _is_compressed_summary(msg: Message) -> bool:
    """Return True if the message is a compressed summary."""
    return msg.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.SUMMARY


def _uses_service_side_context(
    context: SessionContext,
    default_options: Mapping[str, Any] | None = None,
    invalidated_handles: Collection[str] | None = None,
) -> bool:
    """Return True when the current run should continue server-side history."""
    options = dict(default_options or {})
    options.update(context.options)
    if invalidated_handles:
        # ``context.options`` arrives already sanitized by the Agent, but
        # sanitization REMOVES keys, and an absent key cannot override a
        # configured default through the merge above — an invalidated
        # default handle would read as live here while the wire choke point
        # strips it, leaving the request with neither remote nor local
        # history. Strip the merged view so the defaults obey the same
        # invalidation verdicts as the request options.
        options = strip_invalidated_conversation_handles(options, invalidated_handles)
    # Normalized handle view: every continuation spelling counts — plain
    # strings, mapping ``{"id": ...}`` forms, extra_body copies, and
    # continuation tokens — since each reaches the wire as service-side
    # continuation state; a handle that survives the strip above is live.
    if collect_conversation_handles(options):
        return True
    if options.get("store") is False:
        return False
    return bool(context.service_session_id)


def _refresh_pre_output_history_len_from_anchors(state: dict[str, Any], anchors: list[Message]) -> None:
    """Refresh the current-turn floor after post-output history rewrites."""
    if not anchors:
        return
    messages = state.get("messages")
    if not isinstance(messages, list):
        return
    anchor_ids = {id(anchor) for anchor in anchors}
    for index, message in enumerate(messages):
        if id(message) in anchor_ids:
            state[PRE_OUTPUT_HISTORY_LEN_STATE_KEY] = index
            return


def _compress_state(
    state: dict[str, Any],
    marker_id: str,
    summary_text: str,
    compressed_context_id: str | None = None,
) -> tuple[str, list[Message]]:
    """Core compression: deep-copy fold range, create CompressedBlock, replace state messages.

    Returns ``(compressed_context_id, fold_range)`` where *fold_range* is the
    list of **original** Message objects that were in the fold range before
    the list replacement.  Callers that need tool-loop visibility can set
    ``_excluded`` flags on these shared objects.

    Raises:
        ValueError: If the marker is not found or invalid.
    """
    messages: list[Message] = state.get("messages", [])
    if not messages:
        raise ValueError("Nothing to compress — history is empty.")

    # Find the target marker
    marker_idx: int | None = None
    for i, msg in enumerate(messages):
        if msg.additional_properties.get(_TURN_ID_KEY) == marker_id:
            marker_idx = i
            break

    if marker_idx is None:
        raise ValueError(f"Marker '{marker_id}' not found in history.")

    # Find fold_start: first message after the last compressed summary
    fold_start = 0
    for i, msg in enumerate(messages):
        if _is_compressed_summary(msg):
            fold_start = i + 1

    if fold_start > marker_idx:
        raise ValueError(f"Marker '{marker_id}' is already within or before an existing compressed block.")

    # Determine turn range from markers in the fold range
    turn_indices = []
    for msg in messages[fold_start : marker_idx + 1]:
        ti = msg.additional_properties.get(_TURN_KEY)
        if ti is not None:
            turn_indices.append(ti)
    turn_range = (min(turn_indices), max(turn_indices)) if turn_indices else (0, 0)

    # Deep-copy the fold range (originals preserved in block)
    fold_range = messages[fold_start : marker_idx + 1]
    for message in fold_range:
        ensure_analytics_item_id(message.additional_properties)
    replaced = copy.deepcopy(fold_range)

    # Create compressed block
    if compressed_context_id is None:
        compressed_context_id = f"ctx_{uuid.uuid4().hex[:8]}"
    block = CompressedBlock(
        compressed_context_id=compressed_context_id,
        messages=replaced,
        summary_text=summary_text,
        marker_id=marker_id,
        turn_range=turn_range,
        created_at=utc_iso(),
    )

    compressed: list[CompressedBlock] = state.setdefault("compressed_msgs", [])
    compressed.append(block)

    # Create summary message
    summary_msg = Message(
        "assistant",
        [f"[Compressed context: {compressed_context_id}]\nSummary: {summary_text}"],
    )
    summary_msg.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.SUMMARY
    summary_msg.additional_properties[_BLOCK_ID_KEY] = compressed_context_id
    # It stands in for the whole folded range from the next request on, so it
    # is stamped where it is minted rather than at the turn-ending save.
    ensure_analytics_item_id(summary_msg.additional_properties)

    # Replace state["messages"] with the clean list
    state["messages"] = [
        *messages[:fold_start],
        summary_msg,
        *messages[marker_idx + 1 :],
    ]

    return compressed_context_id, fold_range


def _compress_state_validate(state: dict[str, Any], marker_id: str) -> int:
    """Validate that *marker_id* can be compressed, return fold range length.

    Used by :meth:`UnifiedContextStrategy.queue_compression` for early
    validation before the deferred execution in ``__call__``.

    Raises:
        ValueError: If the marker is not found or invalid.
    """
    messages: list[Message] = state.get("messages", [])
    if not messages:
        raise ValueError("Nothing to compress — history is empty.")

    marker_idx: int | None = None
    for i, msg in enumerate(messages):
        if msg.additional_properties.get(_TURN_ID_KEY) == marker_id:
            marker_idx = i
            break
    if marker_idx is None:
        raise ValueError(f"Marker '{marker_id}' not found in history.")

    fold_start = 0
    for i, msg in enumerate(messages):
        if _is_compressed_summary(msg):
            fold_start = i + 1
    if fold_start > marker_idx:
        raise ValueError(f"Marker '{marker_id}' is already within or before an existing compressed block.")

    return marker_idx + 1 - fold_start


class CompressibleHistoryProvider(HistoryProvider):
    """In-memory history provider with marker-based context compression.

    All state lives in ``session.state`` under the provider's ``source_id`` key
    (passed by Agent as ``state`` in before/after_run).

    State layout::

        state["messages"]         -> list[Message]          (active message history)
        state["compressed_msgs"]  -> list[CompressedBlock]   (stack of compressed blocks)
        state["turn_counter"]     -> int                     (monotonic turn counter)

    The kernel ``SessionContext.extend_messages`` appends state messages
    as-is (no copy), so ``_excluded`` flags set by intra-run compaction land
    directly on stored history and persist across ``agent.run()`` calls.
    The ``after_run`` anchor pass remains for the one path aliasing cannot
    cover: streaming finalizers rebuild current-turn response messages from
    updates, so their flags must be re-anchored onto the stored objects.
    """

    DEFAULT_SOURCE_ID = "chrys_history"

    def __init__(
        self,
        source_id: str | None = None,
        compaction_strategy: UnifiedContextStrategy | None = None,
        max_context_tokens: int = 0,
        force_compress_pct: float = 0.70,
        skip_local_history_for_service_context: bool = False,
        service_context_default_options: Mapping[str, Any] | None = None,
        use_local_context_estimate_for_hosted_usage: bool = False,
    ):
        super().__init__(
            source_id=source_id or self.DEFAULT_SOURCE_ID,
            load_messages=True,
            store_inputs=True,
            store_outputs=True,
        )
        self._compaction_strategy: UnifiedContextStrategy | None = compaction_strategy
        self._max_context_tokens = max_context_tokens
        self._force_compress_pct = force_compress_pct
        self._skip_local_history_for_service_context = skip_local_history_for_service_context
        self._service_context_default_options = dict(service_context_default_options or {})
        self._use_local_context_estimate_for_hosted_usage = use_local_context_estimate_for_hosted_usage

    # ---- HistoryProvider abstract methods ----

    async def get_messages(
        self, session_id: str | None, *, state: dict[str, Any] | None = None, **kwargs: Any
    ) -> list[Message]:
        if state is None:
            return []
        messages = state.get("messages", [])
        # Filter out turn markers (structural metadata), excluded messages
        # (compacted tool-call groups whose summaries replace them), and
        # crash-leftover synthetic ``continue`` nudges — a state-RESIDENT
        # flagged nudge is by construction a leftover (a live nudge travels
        # as the run's input prefix and reaches state only after the run,
        # where post-run cleanup removes it) and must not be re-sent to the
        # model on every subsequent run.  Injections are NOT filtered: they
        # are real user input.
        # Compressed summaries ARE kept because the LLM needs to see them.
        return [
            m
            for m in messages
            if not _is_turn_marker(m)
            and not m.additional_properties.get(_EXCLUDED_KEY, False)
            and not is_continuation_message(m)
        ]

    async def save_messages(
        self,
        session_id: str | None,
        messages: Sequence[Message],
        *,
        state: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        if state is None:
            return
        existing = state.get("messages", [])
        state["messages"] = [*existing, *messages]

    # ---- Lifecycle hooks ----

    async def before_run(
        self,
        *,
        agent: Any,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        """Load history and expose message count."""
        if not self._skip_local_history_for_service_context or not _uses_service_side_context(
            context,
            self._service_context_default_options,
            session.invalidated_service_session_ids,
        ):
            await super().before_run(agent=agent, session=session, context=context, state=state)
        session.state["_chrys_history_len"] = len(state.get("messages", []))
        session.state.pop(LAST_ASSISTANT_CREATED_AT_STATE_KEY, None)
        # Bind strategy to state so it can process compressions and list markers.
        if self._compaction_strategy is not None:
            self._compaction_strategy.bind_state(state)

    async def after_run(
        self,
        *,
        agent: Any,
        session: AgentSession,
        context: SessionContext,
        state: dict[str, Any],
    ) -> None:
        """Store outputs and persist compaction flags on the stored state messages.

        History messages reach the wire un-copied (kernel
        ``SessionContext.extend_messages``), so their intra-run ``_excluded``
        flags are already on the stored objects. Current-turn response
        messages are the exception: streaming finalizers rebuild them from
        updates before they are stored, dropping flags set on the loop-level
        objects.

        Instead of re-running the full compaction algorithm on state messages
        (which would see all messages as non-excluded and potentially compact
        far more aggressively than the tool loop did), we:

        1. Flush any pending compressions that weren't processed during the
           tool loop (edge case: ``compress_context`` called right before the
           run ended).
        2. Propagate tool-loop exclusion flags onto the stored messages via
           anchors — identity hits are no-ops for the aliased history
           messages; structural fallbacks re-anchor the streaming-rebuilt
           current-turn messages.

        For Responses service-side sessions, this remains local bookkeeping
        only; compaction does not rewrite the provider's server-side context.
        """
        await super().after_run(agent=agent, session=session, context=context, state=state)
        current_turn_anchors = list(context.input_messages)
        if context.response is not None:
            current_turn_anchors.extend(context.response.messages)
        messages = state.get("messages", [])
        if isinstance(messages, list):
            pre_output_len = state.get(PRE_OUTPUT_HISTORY_LEN_STATE_KEY)
            start_idx = pre_output_len if isinstance(pre_output_len, int) else session.state.get("_chrys_history_len")
            if not isinstance(start_idx, int):
                start_idx = len(messages)
            elif start_idx < 0:
                start_idx = 0
            elif start_idx > len(messages):
                start_idx = len(messages)
            timestamp = utc_iso()
            last_assistant_timestamp: str | None = None
            for message in messages[start_idx:]:
                if not should_stamp_message_created_at(message) or message.role != "assistant":
                    continue
                stamp_message_created_at(message, timestamp)
                created_at = message.additional_properties.get(MESSAGE_CREATED_AT_KEY)
                normalized = try_normalize_created_at(created_at)
                if normalized is not None:
                    last_assistant_timestamp = normalized
            if last_assistant_timestamp is not None:
                session.state[LAST_ASSISTANT_CREATED_AT_STATE_KEY] = last_assistant_timestamp
        else:
            messages = []
        state_rewritten_after_output = False
        if self._compaction_strategy is not None and messages:
            state_rewritten_after_output = await self._compaction_strategy.flush_pending_compressions()
            # Flushing can replace the list. Persist into the owned list, then
            # refresh the current-turn floor if Phase 1 inserted summaries.
            messages = state.get("messages", [])
            previous_length = len(messages)
            self._compaction_strategy.persist_exclusions_to_state(messages)
            state_rewritten_after_output |= len(messages) != previous_length

        # A turn that ended above the force line books a turn-boundary pass on
        # the strategy (history is not rewritten here); a provider without a
        # strategy folds its oldest turn instead.
        state_rewritten_after_output = (
            await self._check_turn_end_usage(state, context, session.invalidated_service_session_ids)
            or state_rewritten_after_output
        )
        if state_rewritten_after_output:
            _refresh_pre_output_history_len_from_anchors(state, current_turn_anchors)

    async def _check_turn_end_usage(
        self,
        state: dict[str, Any],
        context: SessionContext,
        invalidated_handles: Collection[str] | None = None,
    ) -> bool:
        """Act on a turn that ended above ``force_compress_pct``; return whether history was rewritten.

        With a strategy this only books a turn-boundary pass: the next request
        compacts down to the target in one pass, oldest tool output first.
        Folding one turn here instead would rewrite the prompt head after
        nearly every turn while freeing about as much as the turn added.  A
        strategy-less provider folds its oldest turn with the static
        ``compress()``.
        """
        if self._max_context_tokens <= 0 or self._force_compress_pct >= 1.0:
            return False
        service_options = dict(self._service_context_default_options)
        service_options.update(context.options)
        if self._skip_local_history_for_service_context and (
            service_options.get("store") is True
            or _uses_service_side_context(context, self._service_context_default_options, invalidated_handles)
        ):
            # Rewriting local history cannot shrink a provider-owned
            # continuation and would only create a misleading compression.
            return False

        # ``usage_details`` is the billable aggregate across every model call
        # in a tool loop.  This check needs the final call's input size,
        # which represents the actual context occupancy.
        if not context.response or not context.response.latest_usage_details:
            return False
        if (
            self._use_local_context_estimate_for_hosted_usage
            and self._compaction_strategy is not None
            and contains_provider_hosted_content(context.response.messages)
        ):
            input_tokens = resolve_hosted_context_input_tokens(
                context.response.latest_usage_details,
                local_estimate=self._compaction_strategy.estimated_context_input_tokens,
            )
        else:
            input_tokens = context.response.latest_usage_details.get("input_token_count") or 0
        if input_tokens <= 0:
            return False

        pct = input_tokens / self._max_context_tokens
        if pct <= self._force_compress_pct:
            return False

        # The turn that just ended has no marker yet, so every marker here is
        # an older turn: the ones a turn-boundary pass may work on.
        info = self.list_compressed(state)
        markers = info.get("markers", [])
        if not markers:
            return False

        if self._compaction_strategy is not None:
            self._compaction_strategy.request_turn_boundary_pass()
            return False

        marker_id = markers[0]["marker_id"]
        summary = _auto_summary(state, markers[0].get("position", 0))
        try:
            self.compress(state, marker_id, summary)
        except ValueError:
            return False
        return True

    # ---- Marker operations (static, operate on session state) ----

    @staticmethod
    def insert_marker(state: dict[str, Any], turn_index: int, *, started_at: str | None = None) -> str:
        """Insert a turn marker at the end of message history.

        Args:
            state: The provider-scoped state dict (``session.state[source_id]``).
            turn_index: Sequential turn number.
            started_at: When the pass the marker closes began, if known.

        Returns:
            The ``marker_id`` for this marker.
        """
        marker_id = f"turn_{turn_index}"
        msg = Message("assistant", [""])
        msg.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
        msg.additional_properties[_TURN_ID_KEY] = marker_id
        msg.additional_properties[_TURN_KEY] = turn_index
        if started_at is not None:
            msg.additional_properties[TURN_STARTED_AT_KEY] = started_at
        ensure_analytics_item_id(msg.additional_properties)

        messages: list[Message] = state.setdefault("messages", [])
        messages.append(msg)
        state["turn_counter"] = turn_index

        return marker_id

    # ---- Compression operations (static) ----

    @staticmethod
    def compress(
        state: dict[str, Any],
        marker_id: str,
        summary_text: str,
    ) -> str:
        """Compress messages from the last summary up to (inclusive) the target marker.

        Thin wrapper around :func:`_compress_state`.  Use the unified
        strategy's ``queue_compression`` for
        tool-loop-aware compression.

        Returns:
            The ``compressed_context_id`` for the new block.

        Raises:
            ValueError: If the marker_id is not found or invalid.
        """
        ctx_id, _fold_range = _compress_state(state, marker_id, summary_text)
        return ctx_id

    @staticmethod
    def list_compressed(state: dict[str, Any]) -> dict[str, Any]:
        """Return metadata about all compressed blocks and available fold markers.

        Returns:
            Dict with ``"blocks"`` (compressed block metadata) and
            ``"markers"`` (available fold point markers).
        """
        # Compressed blocks
        compressed: list[CompressedBlock] = state.get("compressed_msgs", [])
        blocks = [
            {
                "compressed_context_id": b.compressed_context_id,
                "summary": b.summary_text,
                "marker_id": b.marker_id,
                "turn_range": b.turn_range,
                "num_messages": len(b.messages),
                "created_at": b.created_at,
            }
            for b in compressed
        ]

        # Available fold markers (markers that are after the last summary)
        messages: list[Message] = state.get("messages", [])
        last_summary_idx = -1
        for i, msg in enumerate(messages):
            if _is_compressed_summary(msg):
                last_summary_idx = i

        markers = []
        for i, msg in enumerate(messages):
            if i <= last_summary_idx:
                continue
            if _is_turn_marker(msg):
                markers.append(
                    {
                        "marker_id": msg.additional_properties.get(_TURN_ID_KEY, ""),
                        "turn_index": msg.additional_properties.get(_TURN_KEY, 0),
                        "position": i,
                    }
                )

        return {"blocks": blocks, "markers": markers}


# ---------------------------------------------------------------------------
# Deterministic summary of a turn folded without the model
# ---------------------------------------------------------------------------

_AUTO_SUMMARY_USER_MAX = 100
"""Max characters for user message text in auto-generated summaries."""


def _auto_summary(state: dict[str, Any], marker_position: int) -> str:
    """Generate a summary from messages up to marker_position for auto-compression.

    Extracts user message snippets and tool names from the fold range.
    """
    messages: list[Message] = state.get("messages", [])

    # Find fold_start (after last compressed summary)
    fold_start = 0
    for i, msg in enumerate(messages):
        if _is_compressed_summary(msg):
            fold_start = i + 1

    fold_range = messages[fold_start : marker_position + 1]
    if not fold_range:
        return "Earlier conversation"

    user_snippets: list[str] = []
    tool_names: set[str] = set()

    for msg in fold_range:
        # Skip synthetic ``continue`` nudges so a fold summary never opens
        # with "User: continue"; injections are real user input and stay.
        if msg.role == "user" and not is_continuation_message(msg):
            text = msg.text or ""
            if text:
                if len(text) > _AUTO_SUMMARY_USER_MAX:
                    text = text[:_AUTO_SUMMARY_USER_MAX] + "..."
                user_snippets.append(text)
        for c in msg.contents:
            if c.type in TOOL_CALL_CONTENT_TYPES and (name := tool_call_name(c)):
                tool_names.add(name)

    parts: list[str] = []
    if user_snippets:
        parts.append(f"User: {user_snippets[0]}")
        if len(user_snippets) > 1:
            parts.append(f"(+{len(user_snippets) - 1} more messages)")
    if tool_names:
        parts.append(f"Tools: {', '.join(sorted(tool_names))}")

    return "; ".join(parts) if parts else "Earlier conversation"
