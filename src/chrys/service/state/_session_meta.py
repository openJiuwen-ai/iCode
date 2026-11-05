# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session metadata extraction for the JSON file state store."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.session_surface import SessionSurface, parse_session_surface
from chrys.foundation.models.turns import is_continuation_message
from chrys.foundation.util.time import parse_created_at
from chrys.kernel import Message
from chrys.service.context.providers.history import TURN_INDEX_KEY, CompressedBlock
from chrys.service.session.message_metadata import MESSAGE_CREATED_AT_KEY
from chrys.service.session.runtime_metadata import TOTAL_SESSION_TOKENS_KEY
from chrys.service.state.session_mru import coerce_utc
from chrys.service.state.workflow import WorkflowSessionState
from chrys.service.workflows.history import WorkflowRunMeta

logger = logging.getLogger(__name__)


def resolve_session_kind(meta: dict[str, Any]) -> Literal["chat", "workflow"]:
    """Older Chat archives predate the discriminator; Workflow always records it."""
    kind = meta.get("kind", "chat")
    if kind not in ("chat", "workflow"):
        raise ValueError(f"Invalid session kind: {kind!r}")
    return kind


@dataclass(kw_only=True)
class SessionMetadata:
    """Identity and storage facts shared by Chat and Workflow sessions."""

    session_id: str
    created_at: datetime
    updated_at: datetime
    total_tokens: int = 0
    primary_cwd: str = ""
    working_dirs: list[str] = field(default_factory=list)
    title: str = ""
    custom_title: str = ""
    generated_title: str = ""
    size_bytes: int = 0
    app_version: str = ""
    schema_version: int = 0
    os_name: str = ""
    arch: str = ""
    # The surface of the last turn or run; ``None`` for sessions saved before it was recorded.
    last_surface: SessionSurface | None = None

    @property
    def display_title(self) -> str:
        return self.custom_title or self.generated_title or self.title


@dataclass(kw_only=True)
class ChatSessionMeta(SessionMetadata):
    """Conversation metadata; never attached to Workflow history."""

    kind: Literal["chat"] = field(default="chat", init=False)
    agent_profile: str
    agent_display_name: str
    message_count: int
    turn_count: int = 0
    agent_profile_id: str = ""
    agent_profile_history: list[str] = field(default_factory=list)
    agent_profile_fingerprint: str = ""
    model_provider: str = ""
    model_api_style: str = ""
    model_id: str = ""
    model_profile_id: str = ""
    model_base_url: str = ""
    model_profile_fingerprint: str = ""
    service_session_id: str = ""
    parent_session_id: str = ""
    # Bounded head/tail excerpts of user prompts, never full transcript text.
    user_prompt_search_text: str = ""


@dataclass(kw_only=True)
class WorkflowSessionMeta(SessionMetadata):
    """A bounded summary; run history is loaded explicitly by the catalog."""

    kind: Literal["workflow"] = field(default="workflow", init=False)
    workflow_id: str = ""
    run_count: int = 0
    latest_run_id: str = ""
    latest_run: WorkflowRunMeta | None = None


type SessionMeta = ChatSessionMeta | WorkflowSessionMeta


def copy_session_meta(meta: SessionMeta) -> SessionMeta:
    """Return a copy of *meta* that shares none of its mutable fields.

    The store's listing caches hand out copies, so a caller that edits the
    meta it was given cannot change what later listings read.
    """
    if isinstance(meta, ChatSessionMeta):
        return replace(
            meta, working_dirs=list(meta.working_dirs), agent_profile_history=list(meta.agent_profile_history)
        )
    return replace(meta, working_dirs=list(meta.working_dirs))


def _is_visible_message(m: Message) -> bool:
    """Return True for user messages and non-marker assistant messages with text.

    Synthetic ``continue`` nudges do not count: a crash-leftover nudge is a
    orchestration placeholder, not user content, and must not inflate
    ``meta.message_count`` (or, through it, ``updated_at`` preservation and
    the buddy total).
    """
    chrys_kind = m.additional_properties.get(HistoryMarkerKind.KEY)
    if chrys_kind in HistoryMarkerKind.SESSION_COUNT_EXCLUDED:
        return False
    if m.role == "user":
        return not is_continuation_message(m)
    if m.role == "assistant":
        return any(c.type == "text" and (c.text or "").strip() for c in m.contents)
    return False


def _parse_session_timestamp(value: object) -> datetime | None:
    """Return one persisted timestamp as an aware UTC datetime, if valid."""
    parsed = parse_created_at(value)
    return coerce_utc(parsed) if parsed is not None else None


def recorded_surface(
    primary_meta: Mapping[str, Any], recovery_meta: Mapping[str, Any], *, sidecar_first: bool = False
) -> str | None:
    """The surface the session's last turn recorded, verbatim; ``None`` when none ever was.

    A sidecar newer than the primary holds the turn a crash cut short, which
    is the session's last one, so its surface outranks the primary's.
    """
    primary_updated_at = _parse_session_timestamp(primary_meta.get("updated_at"))
    recovery_updated_at = _parse_session_timestamp(recovery_meta.get("updated_at"))
    recovery_first = sidecar_first or (
        recovery_updated_at is not None and (primary_updated_at is None or recovery_updated_at > primary_updated_at)
    )
    candidates = (recovery_meta, primary_meta) if recovery_first else (primary_meta, recovery_meta)
    return next((value for meta in candidates if isinstance(value := meta.get("last_surface"), str) and value), None)


def _message_created_at(message: object) -> datetime | None:
    """Read a Chrys message timestamp from either live or serialized state."""
    if isinstance(message, Message):
        properties = message.additional_properties
    elif isinstance(message, dict):
        raw_properties = message.get("additional_properties")
        properties = raw_properties if isinstance(raw_properties, dict) else {}
    else:
        return None
    return _parse_session_timestamp(properties.get(MESSAGE_CREATED_AT_KEY))


def _first_message_created_at(messages: object) -> datetime | None:
    """Return the first valid message timestamp in an ordered message list."""
    if not isinstance(messages, list):
        return None
    for message in messages:
        created_at = _message_created_at(message)
        if created_at is not None:
            return created_at
    return None


def _earliest_history_created_at(state: dict[str, Any]) -> datetime | None:
    """Return the oldest available timestamp from ordered live or serialized history.

    Compaction preserves chronological order. Scan blocks oldest-first until
    one carries a timestamp, then fall back to the live tail; this handles a
    legacy unstamped block followed by blocks created after timestamping was
    introduced. Both runtime ``CompressedBlock`` values and serialized dict
    mirrors are accepted.
    """
    compressed_blocks = state.get("compressed_msgs")
    if isinstance(compressed_blocks, list):
        for block in compressed_blocks:
            if isinstance(block, CompressedBlock):
                compressed_messages: object = block.messages
            elif isinstance(block, dict):
                compressed_messages = block.get("messages")
            else:
                continue
            created_at = _first_message_created_at(compressed_messages)
            if created_at is not None:
                return created_at
    return _first_message_created_at(state.get("messages"))


_TITLE_MAX_LEN = 200

_PROMPT_SEARCH_EDGE_CHARS = 1000
"""Head AND tail characters kept per user message for prompt search."""

_PROMPT_SEARCH_TOTAL_CHARS = 8000
"""Per-session cap on ``SessionMeta.user_prompt_search_text`` (meta-cache bound)."""

_PROMPT_SEARCH_SCAN_BLOCK = 4096
"""Fixed raw-text block size for the bounded collapse scan — the transient
memory unit of ``_collapsed_prompt_excerpt`` (never a growing slice)."""


def title_from_text(text: str) -> str:
    """Use the same single-line, bounded title for every user-authored prompt."""
    text = text.strip().replace("\n", " ")
    return text[:_TITLE_MAX_LEN] + "..." if len(text) > _TITLE_MAX_LEN else text


def _extract_title(messages: list[Message]) -> str:
    """Extract title from the first user message text, truncated.

    Skips synthetic ``continue`` nudges: in the no-prior-user resume shape
    the first user message IS a nudge and the session would be titled
    "continue".
    """
    for m in messages:
        if m.role != "user" or is_continuation_message(m):
            continue
        for c in m.contents:
            if c.type == "text":
                text = title_from_text(c.text or "")
                if text:
                    return text
    return ""


class SessionMetaMixin:
    """Session metadata extraction methods for JSON state stores."""

    @staticmethod
    def _envelope_turn_count(envelope: dict[str, Any]) -> int:
        """Total turns recorded in a serialized envelope's state payload.

        Takes the max of ``state["turn_counter"]`` (authoritative — kept
        aligned with history turn markers by ``SessionHistoryManager``) and
        the evidence surviving on disk: the highest ``_turn`` index stamped
        on live turn markers (with the marker count as a floor) and the
        ``turn_range`` ends of compacted blocks, whose original markers were
        folded away.  The max guards pre-counter sessions later re-saved
        with a restarted counter.
        """
        state = envelope.get("state")
        if not isinstance(state, dict):
            return 0
        counter = state.get("turn_counter", 0)
        best = counter if isinstance(counter, int) and counter > 0 else 0
        messages = state.get("messages", [])
        if isinstance(messages, list):
            marker_count = 0
            for message in messages:
                if not isinstance(message, dict):
                    continue
                props = message.get("additional_properties")
                if not (isinstance(props, dict) and props.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.TURN):
                    continue
                marker_count += 1
                turn_index = props.get(TURN_INDEX_KEY)
                if isinstance(turn_index, int):
                    best = max(best, turn_index)
            best = max(best, marker_count)
        blocks = state.get("compressed_msgs", [])
        if isinstance(blocks, list):
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                turn_range = block.get("turn_range", ())
                if isinstance(turn_range, list | tuple) and len(turn_range) == 2 and isinstance(turn_range[1], int):
                    best = max(best, turn_range[1])
        return best

    @staticmethod
    def _collapsed_prompt_excerpt(text: str, edge: int) -> str:
        """Whitespace-collapsed ``text``, capped to head+tail ``edge`` chars.

        Output-equivalent to capping ``" ".join(text.split())`` in
        O(edge + block) transient memory instead of O(input):
        ``str.split()`` on a multi-megabyte paste of short words costs
        ~14x its size, and even a growing window slice copies O(input)
        raw text when whitespace-dominated input keeps the collapsed
        yield low.  The scan therefore walks FIXED-size blocks by index
        from each end, collapsing block by block and stitching at block
        boundaries: a boundary splitting a word glues the pieces, a
        boundary touching whitespace on either side becomes the single
        space the full collapse would have produced there, and an
        all-whitespace block contributes nothing now but leaves a
        whitespace boundary char for the next stitch to see.  The head
        accumulator is an exact PREFIX of the full collapse and the tail
        accumulator an exact SUFFIX, so each side may stop as soon as it
        covers its capped region.
        """
        block = _PROMPT_SEARCH_SCAN_BLOCK
        limit = 2 * edge
        head = ""
        pos = 0
        while pos < len(text) and len(head) <= limit:
            piece = " ".join(text[pos : pos + block].split())
            if piece:
                if not head:
                    head = piece
                elif text[pos - 1].isspace() or text[pos].isspace():
                    head = f"{head} {piece}"
                else:
                    head += piece
            pos += block
        if len(head) <= limit:
            # The loop only exits with head <= limit once the whole text
            # is consumed, so head IS the full collapse: no cap needed.
            return head
        tail = ""
        pos = len(text)
        while pos > 0 and len(tail) < edge:
            start = max(0, pos - block)
            piece = " ".join(text[start:pos].split())
            if piece:
                if not tail:
                    tail = piece
                elif text[pos - 1].isspace() or text[pos].isspace():
                    tail = f"{piece} {tail}"
                else:
                    tail = piece + tail
            pos = start
        return f"{head[:edge]}\n{tail[-edge:]}"

    @staticmethod
    def _message_prompt_excerpt(message: dict[str, Any]) -> str | None:
        """Capped search excerpt of one user-typed prompt, else ``None``.

        ``None`` means the message is not a user-typed prompt: wrong role,
        synthetic history (turn/status markers, ``continue`` nudges), or no
        text content.  Legacy sessions store ``contents`` as plain strings;
        those count as text under the same rule replay's
        ``_load_session_raw_sync`` uses (a ``Content(type=`` repr blob is
        not text).  Capping each block to its collapsed head+tail before
        joining yields the same final excerpt as capping the full joined
        collapse — the message-level windows only ever read a block's
        first/last ``edge`` chars, which block-level capping preserves.
        """
        if message.get("role") != "user":
            return None
        props = message.get("additional_properties")
        if isinstance(props, dict) and (
            props.get(HistoryMarkerKind.KEY) or props.get(HistoryMarkerKind.CONTINUATION_KEY)
        ):
            return None
        contents = message.get("contents")
        if not isinstance(contents, list):
            return None
        chunks: list[str] = []
        for content in contents:
            if isinstance(content, dict) and content.get("type") == "text":
                text = content.get("text")
            elif isinstance(content, str) and not content.startswith("Content(type="):
                text = content
            else:
                continue
            if isinstance(text, str) and text and not text.isspace():
                chunks.append(SessionMetaMixin._collapsed_prompt_excerpt(text, _PROMPT_SEARCH_EDGE_CHARS))
        if not chunks:
            return None
        joined = "\n".join(chunks)
        if len(joined) > 2 * _PROMPT_SEARCH_EDGE_CHARS:
            joined = f"{joined[:_PROMPT_SEARCH_EDGE_CHARS]}\n{joined[-_PROMPT_SEARCH_EDGE_CHARS:]}"
        return joined

    @staticmethod
    def _envelope_user_prompt_search_text(envelope: dict[str, Any]) -> str:
        """Capped search excerpts of the user-typed prompts in an envelope.

        Walks the messages of the envelope the caller already parsed for
        :meth:`_envelope_turn_count` — compacted originals folded into
        ``compressed_msgs`` blocks first (they are the older turns), then
        the live list — keeping ``role == "user"`` text while skipping
        synthetic history (turn/status markers, ``continue`` nudges).
        Legacy sessions store ``contents`` as plain strings; those count as
        text under the same rule replay's ``_load_session_raw_sync`` uses
        (a ``Content(type=`` repr blob is not text).
        Compaction can leave a message in both places; such twins dedup by
        their capped TEXT, never by ``message_id`` — ids restart across
        runs, so two different prompts can share one (observed in real
        sessions), while an identical indexed string is worthless twice by
        construction.  Whitespace runs inside each text block collapse to single
        spaces so a space-separated query matches a phrase that wrapped
        across lines.  Long prompts keep their head AND tail
        (``_PROMPT_SEARCH_EDGE_CHARS`` each) — users often paste an error
        dump first and type the actual ask at the end.  When the session
        total overruns ``_PROMPT_SEARCH_TOTAL_CHARS`` the same rule applies
        across turns: the earliest and latest prompts survive (half the
        budget each) and the middle is elided.  Dedup then happens INSIDE
        that front/back selection, not before it: repeats of an
        already-kept text skip for free, so a text stays searchable
        whenever ANY of its copies lands in a kept window.  Collapsing
        repeats up front would pin a text to its first copy — and when that
        copy sits in the elided middle, a final-turn repeat of it would
        vanish from the index (review-caught).  Message joins and every
        elision seam use a newline, so no phantom match can straddle them
        (the single-line search box cannot type one).  The scan is
        memory-bounded end to end: block collapse walks fixed-size raw
        blocks (:meth:`_collapsed_prompt_excerpt`), the accumulation
        pass stops retaining as soon as the budget overruns, and the
        over-budget pass re-derives excerpts per message while keeping
        only the selected windows — so a huge paste or a very long session
        allocates O(budget) transient text.  The one whole-session
        allocation left is the flat list of message REFERENCES below,
        pointers into the already-parsed envelope.
        """
        state = envelope.get("state")
        if not isinstance(state, dict):
            return ""
        message_dicts: list[dict[str, Any]] = []
        blocks = state.get("compressed_msgs", [])
        if isinstance(blocks, list):
            for block in blocks:
                folded = block.get("messages", []) if isinstance(block, dict) else []
                if isinstance(folded, list):
                    message_dicts.extend(message for message in folded if isinstance(message, dict))
        messages = state.get("messages", [])
        if isinstance(messages, list):
            message_dicts.extend(message for message in messages if isinstance(message, dict))

        excerpt = SessionMetaMixin._message_prompt_excerpt
        deduped: list[str] = []
        seen_texts: set[str] = set()
        total = 0
        over_budget = False
        for message in message_dicts:
            part = excerpt(message)
            # Twin dedup keys on the indexed text, NEVER message_id: msg_N
            # ids are positional and collide across compaction archives
            # (same id, different prompt — observed in real sessions).
            if part is None or part in seen_texts:
                continue
            # Separator newlines only exist BETWEEN excerpts — charging one
            # for the first excerpt too would flag an index whose joined
            # length is exactly the budget as over it and needlessly evict
            # middle prompts (review-caught).  total tracks the exact
            # length of "\n".join(deduped).
            total += len(part) + 1 if deduped else len(part)
            seen_texts.add(part)
            deduped.append(part)
            if total > _PROMPT_SEARCH_TOTAL_CHARS:
                over_budget = True
                break
        if not over_budget:
            # Every unique text fits, so collapsing repeats onto their first
            # copy loses nothing a substring search could ever see.
            return "\n".join(deduped)
        # Over budget: keep whole prompts from the front and the back until
        # each half fills.  The walks re-derive excerpts in RAW message
        # order — repeats of an already-kept text skip for free (no budget,
        # no break) rather than being pre-collapsed onto a first copy that
        # the elided middle may swallow: a text survives if ANY copy lands
        # in a window.  A capped excerpt (≤ 2·edge+1 chars) always fits an
        # empty half, so the earliest and latest prompts always stay
        # searchable.  Unlike the accumulation pass above, each half DOES
        # charge a separator for its first excerpt: those two spare chars
        # are what keep the final front+back join within the total budget.
        half = _PROMPT_SEARCH_TOTAL_CHARS // 2
        selected: set[str] = set()
        front: list[str] = []
        used = 0
        boundary = len(message_dicts)
        for index, message in enumerate(message_dicts):
            part = excerpt(message)
            if part is None or part in selected:
                continue
            if used + len(part) + 1 > half:
                boundary = index
                break
            front.append(part)
            selected.add(part)
            used += len(part) + 1
        back: list[str] = []
        used = 0
        # The message that overflowed the front half is still eligible for
        # the back half, hence boundary itself is included in the walk.
        for index in range(len(message_dicts) - 1, boundary - 1, -1):
            part = excerpt(message_dicts[index])
            if part is None or part in selected:
                continue
            if used + len(part) + 1 > half:
                break
            back.append(part)
            selected.add(part)
            used += len(part) + 1
        return "\n".join([*front, *reversed(back)])

    @staticmethod
    def _envelope_total_tokens(envelope: dict[str, Any]) -> int:
        """Cumulative session token usage recorded in a serialized envelope."""
        state = envelope.get("state")
        if not isinstance(state, dict):
            return 0
        total = state.get(TOTAL_SESSION_TOKENS_KEY, 0)
        return total if isinstance(total, int) and total > 0 else 0

    @staticmethod
    def _session_meta_from_envelope(envelope: dict[str, Any], *, size_bytes: int) -> SessionMeta:
        """Build the purpose-specific metadata without reading any run artifacts."""
        meta = envelope["meta"]
        common: dict[str, Any] = {
            "session_id": meta["session_id"],
            "created_at": datetime.fromisoformat(meta["created_at"]),
            "updated_at": datetime.fromisoformat(meta["updated_at"]),
            "total_tokens": SessionMetaMixin._envelope_total_tokens(envelope),
            "primary_cwd": meta.get("primary_cwd") or "",
            "working_dirs": meta.get("working_dirs") or [],
            "title": meta.get("title", ""),
            "custom_title": meta.get("custom_title", ""),
            "generated_title": meta.get("generated_title", ""),
            "size_bytes": size_bytes,
            "app_version": meta.get("app_version", ""),
            "schema_version": meta.get("schema_version", 0),
            "os_name": meta.get("os_name", ""),
            "arch": meta.get("arch", ""),
        }
        if resolve_session_kind(meta) == "workflow":
            state = WorkflowSessionState.decode(envelope.get("state"))
            return WorkflowSessionMeta(
                **common,
                last_surface=state.surface,
                workflow_id=state.identity.workflow_id,
                run_count=state.run_count,
                latest_run_id=state.latest_run_id,
            )
        return ChatSessionMeta(
            **common,
            last_surface=parse_session_surface(meta.get("last_surface")),
            agent_profile=meta.get("agent_profile", ""),
            agent_display_name=meta.get("agent_display_name", meta.get("display_name", "")),
            message_count=meta.get("message_count", 0),
            turn_count=SessionMetaMixin._envelope_turn_count(envelope),
            agent_profile_id=meta.get("agent_profile_id", ""),
            agent_profile_history=meta.get("agent_profile_history", meta.get("profile_history", [])),
            agent_profile_fingerprint=meta.get("agent_profile_fingerprint", ""),
            model_provider=meta.get("model_provider", ""),
            model_api_style=meta.get("model_api_style", ""),
            model_id=meta.get("model_id", ""),
            model_profile_id=meta.get("model_profile_id", ""),
            model_base_url=meta.get("model_base_url", ""),
            model_profile_fingerprint=meta.get("model_profile_fingerprint", ""),
            service_session_id=meta.get("service_session_id", ""),
            parent_session_id=meta.get("parent_session_id", ""),
            user_prompt_search_text=SessionMetaMixin._envelope_user_prompt_search_text(envelope),
        )
