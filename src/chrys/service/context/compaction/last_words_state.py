# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""LAST_WORDS state: the Phase 4 progress note, its dropped-call manifest and the drop breaker.

Phase 4 (``current_turn_drop``) drops the current turn's tool-call history
and leaves a progress note in its place.  ``LastWordsState`` stores that note
per logical turn, in the ``last_words`` slot of the reminder middleware's
per-turn container, and renders it as the ``[LAST_WORDS]`` block the
middleware appends after every other reminder on each call.  Storage and
rendering stay one unit: ``create_reminder`` builds the middleware and this
state on one ``ReminderTurns``, and the compaction strategy is bound to both
at once (``UnifiedContextStrategy.bind_reminder``), so the state Phase 4
writes is the state the next request renders.

Lifetimes:

- attempt-local — a whole-run retry rolls them back with the messages they
  describe (``Phase4RetrySnapshot``): the note, the todo text captured with
  it, and the manifest;
- turn-monotonic — never rolled back: the breaker and the one
  context-pressure notification per turn.

State restored from a saved session waits in a single-shot stash: the next
prepared turn takes it when it preserves LAST_WORDS (resuming the interrupted
turn) and discards it otherwise (``begin_turn``, then ``discard_restored``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.foundation.models.history_markers import dropped_records_heading
from chrys.foundation.platform.files import is_utf8_encodable
from chrys.service.agent_middleware.reminders.todo import TodoSource
from chrys.service.agent_middleware.system_reminder import (
    LAST_WORDS_PREFIX,
    escape_system_reminder_tags,
    wrap_system_reminder,
)

from .spill import (
    DISPLAY_ARGUMENT_DEFAULT_MAX_CHARS,
    DISPLAY_ARGUMENT_PATH_MAX_CHARS,
    DISPLAY_ARGUMENT_REASON_MAX_CHARS,
    NOTE_RECORD_GROUP_ID,
    NOTE_RECORD_TOOL_NAME,
    available_record_path,
    owned_record_directory_relative_path,
    owned_record_relative_path,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from chrys.foundation.models.history_markers import LastWordsRecord
    from chrys.service.agent_middleware.system_reminder import ReminderTurns, TurnReminderState

    from .spill import SpillQuota

logger = logging.getLogger(__name__)

_MANIFEST_MAX_LINES = 50
_MANIFEST_MAX_CHARS = 6_000
_MANIFEST_MAX_PERSISTED = 500
_MANIFEST_FIELD_MAX_CHARS = 1_024
_MANIFEST_TOOL_MAX_CHARS = 255
# State-field bound = the widest legal display_argument: a lone path segment
# or a command segment plus ", " plus a reason segment.  Derived, so raising
# a per-key cap can never silently re-truncate stored values.
MANIFEST_DISPLAY_ARGUMENT_MAX_CHARS = max(
    DISPLAY_ARGUMENT_PATH_MAX_CHARS,
    DISPLAY_ARGUMENT_DEFAULT_MAX_CHARS + 2 + DISPLAY_ARGUMENT_REASON_MAX_CHARS,
)
_MANIFEST_STATUS_MAX_CHARS = 128
_MANIFEST_EXCERPT_MAX_CHARS = 1_024
_MANIFEST_STRING_FIELD_LIMITS = {
    "record_id": _MANIFEST_STATUS_MAX_CHARS,
    "group_id": _MANIFEST_FIELD_MAX_CHARS,
    "record_dir": _MANIFEST_FIELD_MAX_CHARS,
    "relative_path": _MANIFEST_FIELD_MAX_CHARS,
    "tool": _MANIFEST_TOOL_MAX_CHARS,
    "display_argument": MANIFEST_DISPLAY_ARGUMENT_MAX_CHARS,
    "outcome": _MANIFEST_STATUS_MAX_CHARS,
    "no_record_reason": _MANIFEST_STATUS_MAX_CHARS,
    "excerpt": _MANIFEST_EXCERPT_MAX_CHARS,
}
_STATE_INTEGER_MAX = (1 << 63) - 1
_BREAKER_STATE_VERSION = 1


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One code-generated dropped-call stub stored as JSON-compatible state."""

    record_id: str
    group_id: str
    record_dir: str
    relative_path: str
    turn: int
    round: int
    sequence: int
    tool: str
    display_argument: str
    outcome: str
    size_chars: int
    assistant_text: bool = False
    available: bool = True
    no_record_reason: str = ""
    excerpt: str = ""
    """Both ends of the result, for a call dropped although it was the newest work: too large to keep whole."""

    def to_state(self) -> dict[str, Any]:
        """Return a JSON-compatible representation (never a dataclass instance)."""
        state: dict[str, Any] = {
            "record_id": _manifest_state_text(self.record_id, _MANIFEST_STATUS_MAX_CHARS),
            "group_id": _manifest_state_text(self.group_id, _MANIFEST_FIELD_MAX_CHARS),
            "record_dir": _manifest_state_text(self.record_dir, _MANIFEST_FIELD_MAX_CHARS),
            "relative_path": _manifest_state_text(self.relative_path, _MANIFEST_FIELD_MAX_CHARS),
            "turn": self.turn,
            "round": self.round,
            "sequence": self.sequence,
            "tool": _manifest_state_text(self.tool, _MANIFEST_TOOL_MAX_CHARS),
            "display_argument": _manifest_state_text(self.display_argument, MANIFEST_DISPLAY_ARGUMENT_MAX_CHARS),
            "outcome": _manifest_state_text(self.outcome, _MANIFEST_STATUS_MAX_CHARS),
            "size_chars": self.size_chars,
            "assistant_text": self.assistant_text,
            "available": self.available,
            "no_record_reason": _manifest_state_text(self.no_record_reason, _MANIFEST_STATUS_MAX_CHARS),
        }
        if self.excerpt:
            state["excerpt"] = _manifest_state_text(self.excerpt, _MANIFEST_EXCERPT_MAX_CHARS)
        return state

    @classmethod
    def from_state(cls, value: object) -> ManifestEntry | None:
        """Validate and restore one bounded manifest entry."""
        if not isinstance(value, Mapping):
            logger.debug("Dropping malformed LAST_WORDS manifest entry: expected mapping")
            return None
        strings: dict[str, str] = {}
        for key, limit in _MANIFEST_STRING_FIELD_LIMITS.items():
            item = value.get(key, "")
            if not isinstance(item, str) or len(item) > limit or not is_utf8_encodable(item):
                logger.debug("Dropping malformed LAST_WORDS manifest entry: invalid %s", key)
                return None
            strings[key] = item
        record_dir = Path(strings["record_dir"])
        relative_path = Path(strings["relative_path"]) if strings["relative_path"] else None

        if not strings["group_id"] and strings["tool"] == NOTE_RECORD_TOOL_NAME:
            # Superseded-note entries persisted by the first note-records
            # build carried an empty group_id, which this validator rejects;
            # normalize the legacy shape so those sessions keep their note
            # manifest line instead of silently losing it on restore.
            strings["group_id"] = NOTE_RECORD_GROUP_ID

        if not owned_record_directory_relative_path(record_dir):
            logger.debug("Dropping malformed LAST_WORDS manifest entry: unsafe record_dir")
            return None
        if relative_path is not None and (
            not owned_record_relative_path(relative_path) or relative_path.parent != record_dir
        ):
            logger.debug("Dropping malformed LAST_WORDS manifest entry: unsafe relative_path")
            return None
        integer_fields = ("turn", "round", "sequence", "size_chars")
        integers: dict[str, int] = {}
        for key in integer_fields:
            item = value.get(key)
            if not isinstance(item, int) or isinstance(item, bool) or item < 0 or item > _STATE_INTEGER_MAX:
                logger.debug("Dropping malformed LAST_WORDS manifest entry: invalid %s", key)
                return None
            integers[key] = item
        assistant_text = value.get("assistant_text", False)
        available = value.get("available", True)
        if not isinstance(assistant_text, bool) or not isinstance(available, bool):
            logger.debug("Dropping malformed LAST_WORDS manifest entry: invalid flags")
            return None
        if not strings["group_id"] or not strings["record_dir"] or not strings["tool"]:
            logger.debug("Dropping malformed LAST_WORDS manifest entry: missing required fields")
            return None
        if strings["relative_path"] and not strings["record_id"]:
            logger.debug("Dropping malformed LAST_WORDS manifest entry: path without record id")
            return None
        if not strings["relative_path"] and not strings["no_record_reason"]:
            logger.debug("Dropping malformed LAST_WORDS manifest entry: neither record nor refusal")
            return None
        return cls(
            record_id=strings["record_id"],
            group_id=strings["group_id"],
            record_dir=strings["record_dir"],
            relative_path=strings["relative_path"],
            turn=integers["turn"],
            round=integers["round"],
            sequence=integers["sequence"],
            tool=strings["tool"],
            display_argument=strings["display_argument"],
            outcome=strings["outcome"],
            size_chars=integers["size_chars"],
            assistant_text=assistant_text,
            available=available,
            no_record_reason=strings["no_record_reason"],
            excerpt=strings["excerpt"],
        )


@dataclass(frozen=True, slots=True)
class DropRoundBreakerState:
    """Versioned, turn-scoped Phase-4 attempt and spend breaker."""

    version: int = _BREAKER_STATE_VERSION
    attempts: int = 0
    consecutive_no_progress: int = 0
    tail_override: bool = False
    disabled: bool = False
    side_call_tokens: int = 0

    def to_state(self) -> dict[str, Any]:
        """Return the full JSON-compatible breaker state."""
        return {
            "version": self.version,
            "attempts": self.attempts,
            "consecutive_no_progress": self.consecutive_no_progress,
            "tail_override": self.tail_override,
            "disabled": self.disabled,
            "side_call_tokens": self.side_call_tokens,
        }

    @classmethod
    def from_state(cls, value: object) -> DropRoundBreakerState | None:
        """Return a validated breaker state, or ``None`` for none saved and for old/malformed data."""
        if value is None:
            # Nothing was saved: the usual case, and nothing to report.
            return None
        version = value.get("version") if isinstance(value, Mapping) else None
        if (
            not isinstance(value, Mapping)
            or not isinstance(version, int)
            or isinstance(version, bool)
            or version != _BREAKER_STATE_VERSION
        ):
            logger.debug("Dropping malformed or unsupported Phase-4 breaker state")
            return None
        integers: dict[str, int] = {}
        for key in ("attempts", "consecutive_no_progress", "side_call_tokens"):
            item = value.get(key)
            if not isinstance(item, int) or isinstance(item, bool) or item < 0 or item > _STATE_INTEGER_MAX:
                logger.debug("Dropping malformed Phase-4 breaker state: invalid %s", key)
                return None
            integers[key] = item
        tail_override = value.get("tail_override")
        disabled = value.get("disabled")
        if not isinstance(tail_override, bool) or not isinstance(disabled, bool):
            logger.debug("Dropping malformed Phase-4 breaker state: invalid flags")
            return None
        return cls(
            attempts=integers["attempts"],
            consecutive_no_progress=integers["consecutive_no_progress"],
            tail_override=tail_override,
            disabled=disabled,
            side_call_tokens=integers["side_call_tokens"],
        )


@dataclass(frozen=True, slots=True)
class Phase4RetrySnapshot:
    """Attempt-local LAST_WORDS state restored by whole-run retry rollback.

    Breaker accounting and context-pressure delivery are deliberately absent:
    they record real per-turn spend/safety side effects and remain monotonic
    across outer provider retries.  The content-bearing note, its todo
    snapshot, and its active spill manifest instead describe messages dropped
    by one attempt and must roll back with those messages.
    """

    last_words: str | None
    last_words_todo: str | None
    last_words_manifest: tuple[dict[str, Any], ...]


@dataclass
class LastWordsTurn:
    """One logical turn's LAST_WORDS slot in the reminder middleware's per-turn container.

    Intentionally mutable, like the container that holds it: a note set in
    an agent child task stays visible to the sibling retry/resume tasks that
    continue the same turn.
    """

    note: str | None = None
    todo: str | None = None
    """Todo-list text captured when the note was set/restored.

    Captured, never read live: the LAST_WORDS block is rendered on every LLM
    call, so a live provider read would change the request suffix after each
    post-compaction ``todo_write`` — a free-running KV-cache break.  The
    captured text rides the note (same set/refresh/restore/preserve lifecycle).
    """
    manifest: list[dict[str, Any]] | None = None
    breaker: dict[str, Any] | None = None
    # Deliberately not part of persisted breaker state: a restored disabled
    # breaker should warn once in the newly resumed process.
    context_pressure_notified: bool = False


def _format_manifest_size(char_count: int) -> str:
    if char_count < 1_000:
        return str(char_count)
    return f"{char_count / 1_000:.1f}k"


def _manifest_state_text(value: str, max_chars: int) -> str:
    """Return bounded text that strict UTF-8 session persistence can encode."""
    safe = value.encode("utf-8", errors="backslashreplace").decode("utf-8")
    return safe[:max_chars]


def serialized_text_tokens(text: str | None, count_tokens: Callable[[str], int]) -> int:
    """Tokens *text* takes in a request, where it rides inside a serialized message.

    Estimation counts ``json.dumps`` of the message (sorted keys, non-ASCII
    literal), so quotes, backslashes and control characters cost what their
    escape sequences do, not the one character they are: a note or todo list
    full of paths and newlines is larger on the wire than its rendered text.
    Reminder tags are counted escaped as well: the middleware defangs
    ``<system-reminder>`` in reminder text before it is sent.
    """
    return count_tokens(json.dumps(escape_system_reminder_tags(text), ensure_ascii=False)) if text else 0


class LastWordsState:
    """The LAST_WORDS note, manifest and breaker of one reminder middleware's turns.

    Reads fall back to the restored stash asymmetrically, as the session
    save path relies on: the note and its captured todo text when no turn
    container exists or its note is unset, the manifest and the breaker only
    when no turn container exists.
    """

    def __init__(
        self,
        turns: ReminderTurns,
        *,
        spill_quota: SpillQuota | None = None,
        session_root: Path | None = None,
        file_read_available: bool = False,
        todo_provider: Callable[[], str | None] | None = None,
    ) -> None:
        self._turns = turns
        self._spill_quota = spill_quota
        self._session_root = session_root
        self._file_read_available = file_read_available
        # The note captures the todo list the catalog offers, read the same way.
        self._todo = TodoSource(todo_provider)
        # LAST_WORDS note restored from persisted session state (session
        # resume).  Consumed by the next preserving ``begin_turn`` so a
        # post-restart retry/continue re-injects the note exactly like an
        # in-process retry; discarded when a fresh turn starts instead.
        self._restored_last_words: str | None = None
        # Todo-list text captured alongside a restored LAST_WORDS note (same
        # single-shot lifecycle as ``_restored_last_words``).
        self._restored_last_words_todo: str | None = None
        self._restored_last_words_manifest: list[dict[str, Any]] | None = None
        self._restored_last_words_breaker: dict[str, Any] | None = None

    @property
    def turns(self) -> ReminderTurns:
        """The turn holder whose containers carry this state's slot."""
        return self._turns

    # ------------------------------------------------------------------
    # Turn boundary (called by the reminder middleware's prepare_turn)
    # ------------------------------------------------------------------

    def begin_turn(self, previous: TurnReminderState | None, *, preserve: bool) -> LastWordsTurn:
        """The new turn's slot: carried over from *previous* or the stash when *preserve*, else empty.

        A preserving turn with no *previous* container is a post-restart
        retry/continue and takes the restored stash.  The stash stays until
        ``discard_restored``, which ``prepare_turn`` calls once the rest of
        the turn is captured.
        """
        turn = LastWordsTurn()
        if preserve:
            if previous is not None:
                carried = previous.last_words
                if carried is not None:
                    turn.note = carried.note
                    turn.todo = carried.todo
                    turn.manifest = list(carried.manifest or []) or None
                    turn.breaker = dict(carried.breaker) if carried.breaker else None
                    turn.context_pressure_notified = carried.context_pressure_notified
            else:
                # Post-restart retry/continue: no in-memory turn state exists,
                # fall back to the key family restored from persisted state.
                turn.note = self._restored_last_words
                turn.todo = self._restored_last_words_todo
                turn.manifest = list(self._restored_last_words_manifest or []) or None
                turn.breaker = dict(self._restored_last_words_breaker) if self._restored_last_words_breaker else None
        return turn

    def discard_restored(self) -> None:
        """Drop the restored stash: it is single-shot, taken or discarded by the turn just prepared."""
        self._restored_last_words = None
        self._restored_last_words_todo = None
        self._restored_last_words_manifest = None
        self._restored_last_words_breaker = None

    @staticmethod
    def record_paths(turn: LastWordsTurn) -> set[str]:
        """The record paths *turn*'s manifest lists.

        Legacy restored sessions did not persist the archive pointer's
        turn-start count; excluding these from the live count is the best
        compatible approximation, but new saves never rely on it.
        """
        return {
            entry.relative_path
            for value in turn.manifest or []
            if (entry := ManifestEntry.from_state(value)) is not None and entry.relative_path
        }

    # ------------------------------------------------------------------
    # Note, manifest and breaker
    # ------------------------------------------------------------------

    def set_last_words(self, text: str | None) -> None:
        """Set/clear the Phase 4 LAST_WORDS progress note.

        Called by ``UnifiedContextStrategy`` after Phase 4 drops the
        current turn's tool-call history.  The text is appended (as a
        ``<system-reminder>`` block) to the end of the user message's
        content array on every subsequent LLM call until the next turn.

        The current todo-list text is captured here, once, so the dynamic
        block only changes when the note itself changes (set/refresh/restore)
        — never per call.
        """
        turn = self._ensure_turn()
        turn.note = text or None
        turn.todo = self._todo.snapshot() if turn.note else None

    def todo_reminder_tokens(self, count_tokens: Callable[[str], int]) -> int:
        """Tokens the note's reminder adds from the todo list, captured as the note will capture it.

        A caller sizing the request a drop leaves asks before the note exists;
        the todo text is what ``set_last_words`` will record.  Counted in the
        form the request carries it: a serialized message escapes quotes,
        backslashes and control characters, so a todo list full of them takes
        more room than its rendered text does.
        """
        return serialized_text_tokens(self._todo.snapshot(), count_tokens)

    def get_last_words(self) -> str | None:
        """Return the current LAST_WORDS text (for incremental regeneration).

        Falls back to a restored-but-not-yet-consumed note so that saving a
        session that was restored and closed again without resuming the turn
        does not erase the persisted note.
        """
        turn = self._current_turn()
        if turn is not None and turn.note is not None:
            return turn.note
        return self._restored_last_words

    def append_manifest(self, entries: list[ManifestEntry]) -> None:
        """Append code-generated stubs, retaining the newest persisted entries."""
        if not entries:
            return
        turn = self._ensure_turn()
        serialized = [*list(turn.manifest or []), *(entry.to_state() for entry in entries)]
        turn.manifest = serialized[-_MANIFEST_MAX_PERSISTED:]

    def mark_manifest_records_unavailable(self, relative_paths: frozenset[str] | set[str]) -> None:
        """Snapshot quota evictions into already-persisted manifest entries."""
        if not relative_paths:
            return
        turn = self._ensure_turn()
        if not turn.manifest:
            return
        updated: list[dict[str, Any]] = []
        for value in turn.manifest:
            entry = ManifestEntry.from_state(value)
            if entry is None:
                continue
            if entry.relative_path in relative_paths and entry.available:
                entry = replace(entry, available=False)
            updated.append(entry.to_state())
        turn.manifest = updated or None

    def get_last_words_manifest(self) -> list[dict[str, Any]]:
        """Return detached manifest state with current quota evictions applied."""
        state = self._turns.current()
        if state is None:
            source = self._restored_last_words_manifest
        else:
            source = state.last_words.manifest if state.last_words is not None else None
        return self._normalize_active_manifest(source)

    def snapshot_phase4_retry_state(self) -> Phase4RetrySnapshot:
        """Snapshot only the attempt-local, content-bearing Phase-4 state."""
        turn = self._ensure_turn()
        manifest = self._normalize_active_manifest(turn.manifest)
        return Phase4RetrySnapshot(
            last_words=turn.note,
            last_words_todo=turn.todo,
            last_words_manifest=tuple(manifest),
        )

    def restore_phase4_retry_state(self, snapshot: Phase4RetrySnapshot) -> None:
        """Restore LAST_WORDS content without rewinding turn-level safety accounting."""
        turn = self._ensure_turn()
        turn.note = snapshot.last_words
        # This is the todo text captured when the note was created.  Re-reading
        # the live todo provider here would change the restored note's context.
        turn.todo = snapshot.last_words_todo
        manifest = self._normalize_active_manifest(snapshot.last_words_manifest)
        turn.manifest = manifest or None

    def get_drop_round_breaker(self) -> DropRoundBreakerState:
        """Return the active/restored Phase-4 breaker, defaulting empty."""
        state = self._turns.current()
        if state is None:
            source = self._restored_last_words_breaker
        else:
            source = state.last_words.breaker if state.last_words is not None else None
        restored = DropRoundBreakerState.from_state(source) if source is not None else None
        return restored or DropRoundBreakerState()

    def set_drop_round_breaker(self, breaker: DropRoundBreakerState) -> None:
        """Unconditionally replace breaker state, including note-less failures."""
        self._ensure_turn().breaker = breaker.to_state()

    def get_last_words_breaker_state(self) -> dict[str, Any] | None:
        """Return persisted breaker state once Phase 4 has recorded activity."""
        breaker = self.get_drop_round_breaker()
        return breaker.to_state() if breaker != DropRoundBreakerState() else None

    def claim_context_pressure_notification(self) -> bool:
        """Claim the one context-pressure notification allowed per logical turn."""
        turn = self._ensure_turn()
        if turn.context_pressure_notified:
            return False
        turn.context_pressure_notified = True
        return True

    def release_context_pressure_notification(self) -> None:
        """Release a failed synchronous notification attempt for a later retry."""
        self._ensure_turn().context_pressure_notified = False

    # ------------------------------------------------------------------
    # Session restore
    # ------------------------------------------------------------------

    def restore(
        self,
        state: Mapping[str, Any],
        *,
        available_relative_paths: frozenset[str] | set[str] | None = None,
    ) -> None:
        """Stash the persisted note, manifest and breaker for the next preserving turn."""
        self.restore_last_words(state.get("last_words"))
        self.restore_last_words_manifest(
            state.get("last_words_manifest"),
            available_relative_paths=available_relative_paths,
        )
        self.restore_last_words_breaker(state.get("last_words_breaker"))

    def restore_last_words(self, text: str | None) -> None:
        """Stash a LAST_WORDS note restored from persisted session state.

        Session restore calls this on a freshly built state (no turn state
        exists yet).  The next ``prepare_turn(preserve_last_words=True)``
        — i.e. resuming the interrupted turn — picks the note up so the
        request after restart carries the same ``[LAST_WORDS]`` reminder an
        in-process retry would; starting a fresh turn discards it instead.

        The todo section is re-captured from the (already hydrated) tracker —
        callers must restore ``chrys_todos`` before restoring the note.
        """
        self._restored_last_words = text or None
        self._restored_last_words_todo = self._todo.snapshot() if self._restored_last_words else None

    def restore_last_words_manifest(
        self,
        value: object,
        *,
        available_relative_paths: frozenset[str] | set[str] | None = None,
    ) -> None:
        """Validate restored manifest state and snapshot record availability once."""
        if value is not None and not isinstance(value, list):
            logger.debug("Dropping malformed LAST_WORDS manifest state: expected list")
        raw_entries = value if isinstance(value, list) else []
        entries = [entry for item in raw_entries if (entry := ManifestEntry.from_state(item)) is not None]
        entries = entries[-_MANIFEST_MAX_PERSISTED:]
        if available_relative_paths is None:
            available_relative_paths = set()
            if self._session_root is not None:
                available_relative_paths = {
                    entry.relative_path
                    for entry in entries
                    if entry.relative_path and available_record_path(self._session_root, entry.relative_path)
                }
        restored: list[dict[str, Any]] = []
        for entry in entries:
            available = not entry.relative_path or entry.relative_path in available_relative_paths
            restored.append(replace(entry, available=entry.available and available).to_state())
        self._restored_last_words_manifest = restored or None

    def restore_last_words_breaker(self, value: object) -> None:
        """Stash validated breaker state for the next preserving prepare."""
        breaker = DropRoundBreakerState.from_state(value)
        self._restored_last_words_breaker = breaker.to_state() if breaker is not None else None

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> list[str]:
        """Build dynamic reminders that attach after the user message content.

        A filesystem-free function of stored turn state plus the shared
        in-memory spill ledger: the todo section is the text captured at
        set/restore time, rendered INSIDE the ``[LAST_WORDS]``-prefixed string
        (``refresh_last_words_reminder`` strips only ``[LAST_WORDS]``-prefixed
        contents — a separately wrapped block would accumulate stale copies
        across refreshes).

        The middleware records this text on the message that carries it when
        a drop commits and sends the record from then on, so what is rendered
        here reaches the wire once per note, not once per call.
        """
        reminders: list[str] = []
        last_words = self.get_last_words()
        manifest = self._current_manifest_entries()
        if last_words or manifest:
            # Task-anchored wording (§5.4): the reminder rides the LAST user
            # message, which on an injected turn is an injection or a
            # synthetic ``continue`` — "the above request" would anchor the
            # agent on the wrong message.  The ``[LAST_WORDS] `` prefix is
            # load-bearing (the middleware strips by prefix match during
            # refresh); keep it byte-identical.
            text = (
                f"{LAST_WORDS_PREFIX}You previously made progress on the current task "
                "(the user's request and any follow-up messages above) but ran "
                "out of context, so your earlier tool-call history for this "
                "task has been dropped.  The note below is your own updated progress "
                "record — read it and resume from where you left off.  Do NOT redo "
                "work that the note says is already done.  If the note says the "
                "work is finished but you have not yet delivered the final reply "
                "to the user, do not re-gather anything — compose and deliver "
                "that reply now from the note."
            )
            if last_words:
                text += "\n\n" + last_words.strip()
            todo_section = self._current_last_words_todo()
            if todo_section:
                text += "\n\n" + todo_section
            if manifest:
                text += "\n\n" + self._render_manifest(manifest)
            reminders.append(text)
        return reminders

    def render_last_words_reminder_text(self) -> str | None:
        """Render the LAST_WORDS reminder exactly as the next model call injects it.

        Public debug-oriented view over ``render`` for callers (the compaction
        strategy) that want to persist the injected text without
        reimplementing the rendering.  Each block carries the same
        ``<system-reminder>`` envelope and tag-escaping the enrichment path
        applies, so the dump matches the wire prompt (on the wire the blocks
        are separate content items of one message; here they join with a
        blank line).  ``None`` when no LAST_WORDS state exists and nothing
        would be injected.
        """
        reminders = self.render()
        if not reminders:
            return None
        return "\n\n".join(wrap_system_reminder(reminder) for reminder in reminders)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _current_turn(self) -> LastWordsTurn | None:
        """The current turn container's slot, if both exist."""
        state = self._turns.current()
        return state.last_words if state is not None else None

    def _ensure_turn(self) -> LastWordsTurn:
        """The current turn's slot, creating an empty container and slot for this task if needed."""
        state = self._turns.ensure()
        if state.last_words is None:
            state.last_words = LastWordsTurn()
        return state.last_words

    def _normalize_active_manifest(self, source: object) -> list[dict[str, Any]]:
        """Validate manifest rows and synchronize live spill availability."""
        raw = source if isinstance(source, list | tuple) else ()
        entries = [entry for value in raw if (entry := ManifestEntry.from_state(value)) is not None]
        normalized: list[dict[str, Any]] = []
        for entry in entries[-_MANIFEST_MAX_PERSISTED:]:
            if (
                entry.available
                and entry.relative_path
                and self._spill_quota is not None
                and not self._spill_quota.is_record_available(entry.relative_path)
            ):
                entry = replace(entry, available=False)
            normalized.append(entry.to_state())
        return normalized

    def _current_manifest_entries(self) -> list[ManifestEntry]:
        """Restore validated dataclasses from the JSON-only turn state."""
        return [
            entry for value in self.get_last_words_manifest() if (entry := ManifestEntry.from_state(value)) is not None
        ]

    def manifest_records_dir(self) -> str:
        """The directory the rendered dropped-call list points at, relative to the session's; empty with no list."""
        entries = self._current_manifest_entries()
        return entries[-1].record_dir if entries else ""

    def recorded_block(self, record: LastWordsRecord) -> str:
        """A recorded block as this session sends it.

        The block was rendered by the session that wrote the note.  A fork
        copies the records and the block alike, and the block still names the
        parent's copy, which the fork neither owns nor can count on.
        """
        return record.text_in(self._session_root)

    def _render_manifest(self, entries: list[ManifestEntry]) -> str:
        """Render the bounded dropped-call appendix without filesystem access."""
        heading = dropped_records_heading(entries[-1].record_dir, self._session_root)
        rendered_entries = [self._render_manifest_entry(entry) for entry in entries]
        omitted = 0

        affordance = (
            "To inspect archived input/output, read the listed file with read_file; "
            "capped records contain an explicit middle-truncation marker."
            if self._file_read_available and any(entry.available and entry.relative_path for entry in entries)
            else ""
        )

        def _compose() -> str:
            lines = [heading]
            if omitted:
                lines.append(f"… and {omitted} earlier records — see manifest.md")
            lines.extend(rendered_entries)
            if affordance:
                lines.append(affordance)
            return "\n".join(lines)

        rendered = _compose()
        while (
            len(rendered) > _MANIFEST_MAX_CHARS or len(rendered.splitlines()) > _MANIFEST_MAX_LINES
        ) and rendered_entries:
            rendered_entries.pop(0)
            omitted += 1
            rendered = _compose()
        if len(rendered) > _MANIFEST_MAX_CHARS:
            rendered = rendered[: _MANIFEST_MAX_CHARS - 1] + "…"
        return rendered

    @staticmethod
    def _render_manifest_entry(entry: ManifestEntry) -> str:
        size = _format_manifest_size(entry.size_chars)
        if entry.assistant_text:
            body = f"r{entry.round} {entry.sequence:03d} assistant text, {size} chars"
        else:
            argument = f"{entry.display_argument}" if entry.display_argument else "…"
            body = f"r{entry.round} {entry.sequence:03d} {entry.tool}({argument}) → {entry.outcome}, {size} chars"
        # One line, like the entry it belongs to: the manifest is bounded by lines.
        excerpt = f"\n    excerpt: {' '.join(entry.excerpt.split())}" if entry.excerpt else ""
        if entry.no_record_reason:
            return f"{body} — (no record: {entry.no_record_reason}){excerpt}"
        filename = Path(entry.relative_path).name
        suffix = f" — {filename}"
        if not entry.available:
            suffix += " (record missing)"
        return body + suffix + excerpt

    def _current_last_words_todo(self) -> str | None:
        """Todo text captured with the active LAST_WORDS note, if any."""
        turn = self._current_turn()
        if turn is not None and turn.note is not None:
            return turn.todo
        return self._restored_last_words_todo
