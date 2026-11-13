# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compaction event payloads, shared value types, and exclusion reasons."""

from __future__ import annotations

import logging
import weakref
from dataclasses import dataclass, field
from typing import Any

from chrys.kernel import Message
from chrys.kernel.identity import WeakIdentityRegistry
from chrys.service.context.providers.archive_index import REASON_ARCHIVED as _REASON_ARCHIVED

_log = logging.getLogger(__name__)

# Exclude reasons set by this compaction strategy's _compact_group (Phase 1)
# and _remove_group (Phase 2/3).  Used to recognise flags that were persisted
# from a prior session and should not be cleared on session restore.
_REASON_COMPACTION = "budget_tool_compaction"
_REASON_REMOVAL = "budget_tool_removal"
_REASON_CURRENT_TURN_DROP = "current_turn_drop"
_REASON_COMPRESSION = "cross_turn_compression"
_COMPACTION_EXCLUDE_REASONS = frozenset(
    {_REASON_COMPACTION, _REASON_REMOVAL, _REASON_CURRENT_TURN_DROP, _REASON_COMPRESSION, _REASON_ARCHIVED}
)
_STATE_OWNED_EXCLUDE_REASONS = frozenset({_REASON_COMPRESSION, _REASON_ARCHIVED})
"""Exclusions a history rewrite set on provider state itself: nothing to carry over after the run."""

# ---------------------------------------------------------------------------
# Compaction info
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompactionInfo:
    """Details about an intra-turn compaction round, passed to the on_compaction callback."""

    compacted_groups: int
    """Number of tool-call groups that were compacted."""

    phase: str = ""
    """Which phase: ``"phase1"`` to ``"phase4"`` (see module docstring)."""

    turn_numbers: list[int] = field(default_factory=list)
    """Absolute turn numbers affected (phases 1/2: compacted previous
    turns; phase 4: the current turn)."""

    tool_names: list[str] = field(default_factory=list)
    """Tool names from compacted groups (may contain duplicates across groups)."""

    tokens_before: int = 0
    """Estimated included token count before compaction."""

    tokens_after: int = 0
    """Estimated included token count after compaction."""

    last_words_generated: bool = False
    """True when phase 4 produced/updated a LAST_WORDS progress note."""

    consumed_item_ids: list[str] = field(default_factory=list)
    """Stable item ids folded into the Phase-4 note before the next wire request."""


@dataclass(frozen=True)
class PreCompactInfo:
    """Details about a compaction phase before it mutates context."""

    trigger: str
    """Which phase is about to run: ``"phase1"`` to ``"phase4"``."""

    usage_pct: float
    """Estimated context usage fraction at the phase boundary."""

    tokens_before: int = 0
    """Estimated included token count before the phase runs."""

    trajectory_operation_id: str | None = None
    """Compaction container targeted by blocking hooks, when already open."""


@dataclass(frozen=True)
class CompressInfo:
    """Details about a cross-turn context compression, passed to the on_compress callback."""

    compressed_context_id: str
    """Unique identifier for the compressed block."""

    summary: str = ""
    """Agent-provided summary of the compressed content."""

    freed_messages: int = 0
    """Number of messages that were folded into the compressed block."""

    turn_range: tuple[int, int] = (0, 0)
    """Inclusive backend turn range folded into the compressed block."""

    source: str = "agent"
    """What initiated the compression: ``"agent"`` (LLM called compress_context) or ``"auto"`` (platform)."""


def _compressed_turn_range(state: dict[str, Any] | None, compressed_context_id: str) -> tuple[int, int]:
    """Return the persisted turn range for a just-created compressed block."""
    if state is None:
        return (0, 0)
    compressed_blocks = state.get("compressed_msgs")
    if not isinstance(compressed_blocks, list):
        return (0, 0)

    from chrys.service.context.providers.history import CompressedBlock

    for block in reversed(compressed_blocks):
        if isinstance(block, CompressedBlock) and block.compressed_context_id == compressed_context_id:
            return block.turn_range
    return (0, 0)


@dataclass
class PendingCompression:
    """A queued cross-turn compression request from the ``compress_context`` tool.

    Created by :meth:`UnifiedContextStrategy.queue_compression`, processed
    inside :meth:`UnifiedContextStrategy.__call__` on FunctionInvocationLayer's message list.
    """

    compressed_context_id: str
    """Pre-generated ID, returned to the tool caller immediately."""

    marker_id: str
    """The turn marker to fold up to."""

    summary_text: str
    """Agent-provided summary."""

    freed_count: int = 0
    """Pre-computed count of messages in the fold range."""


@dataclass(frozen=True)
class CachedSummary:
    """A Phase 1 tool-result summary, cached for defensive re-injection.

    The active tool loop keeps inserted summaries on its message list.
    Chrys still caches summaries so copied or replayed lists that retain
    ``_summarized_by_summary_id`` on excluded originals can be reconstructed.
    Stored in :attr:`UnifiedContextStrategy._summary_cache`, keyed by
    a unique occurrence ``summary_id`` (``"tool_summary_<item_id>"``),
    independent of positional group and message IDs.
    """

    summary_text: str
    """The compacted summary text shown in place of the full tool result."""

    original_ids: list[str]
    """Message IDs of the original tool-call group members being summarised."""

    group_id: str
    """ID of the tool-call group this summary replaces."""

    item_id: str
    """Stable occurrence identity shared by wire, stored and re-injected summaries."""

    message: Message
    """Summary contents and write-through metadata, without retaining original messages."""


@dataclass(frozen=True)
class CachedCompressedContextSummary:
    """A cross-turn compressed-context summary cached for same-run re-injection.

    Identity snapshots are WEAK registries held across calls: an entry lives
    exactly as long as its object, so a garbage-collected original simply
    stops matching — its recycled id can never false-match fresh wire
    content — and the cache never keeps the folded originals alive.
    """

    summary_message: Message
    """The compressed-context summary inserted into state."""

    folded_contents: WeakIdentityRegistry
    """Weak identities of the original folded messages' contents lists."""

    folded_summary_ids: frozenset[str]
    """Stable summary occurrence references belonging to folded messages."""

    folded_content_objects: WeakIdentityRegistry
    """Weak identities of the folded messages' content OBJECTS.

    Matches the kernel wire's per-call message views, whose fresh contents
    lists defeat ``folded_contents``; the content objects stay shared.
    Usage contents are deliberately included — fold evidence must keep
    seeing them.
    """

    folded_messages: tuple[weakref.ref[Message], ...]
    """Weak references to the folded state messages themselves.

    Matches the reminder middleware's enriched user messages, which carry
    fresh contents and share only the original's ``additional_properties``
    dict. A plain dict can't be weakly referenced, so the dicts are read off
    the still-live originals at match time; their ids are therefore always
    those of live objects.
    """
