# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""History marker constants for Chrys-owned session messages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from chrys.foundation.i18n import msg

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

# Persisted status-marker writer audit (closed set): fixed semantic statuses are
# interruption/failure fallbacks, checkpoint closure, and reload discard; dynamic
# diagnostic detail is ``last_error``; awaiting sub-agents is dynamic semantic
# status carried by ``_invocation_ids``.
EXECUTION_INTERRUPTED_MESSAGE = msg(
    "history_markers.execution_interrupted",
    fallback="Execution interrupted",
)
EXECUTION_FAILED_MESSAGE = msg(
    "history_markers.execution_failed",
    fallback="Execution failed",
)
SESSION_CLOSED_MESSAGE = msg(
    "history_markers.session_closed",
    fallback="Session closed before the turn finished",
)
SUB_AGENT_STATE_DISCARDED_MESSAGE = msg(
    "history_markers.sub_agent_state_discarded",
    fallback="Sub-agent state was discarded on reload",
)
AWAITING_SUB_AGENTS_MESSAGE = msg(
    "history_markers.awaiting_sub_agents",
    fallback="Awaiting {count} sub-agent(s)",
    plural_fallback="Awaiting {count} sub-agent(s)",
)

ANTHROPIC_THINKING_STRIPPED_KEY: Final[str] = "_chrys_anthropic_thinking_stripped"
"""Set to True in a reasoning ``Content``'s ``additional_properties`` once it is left out for good.

Anthropic refused a request replaying it as bound to a different conversation,
and then accepted that request without any of the thinking it replayed: every
reasoning content the refused request replayed is marked, refused or not, and
never sent again.
"""

ANSWER_SEGMENT_KEY: Final[str] = "_chrys_answer_segment_id"
"""The content segment an answer text ``Content`` was shown in, in its ``additional_properties``.

The invocation's content projector stamps each answer text it publishes with
the ``segment_id`` its frontends key that text by, so the history that keeps
the text can name, when a Continue or steer drops it, exactly what to withdraw.
Never sent on the wire.
"""


class HistoryMarkerKind:
    """String constants stored in ``Message.additional_properties``."""

    KEY: Final[str] = "_chrys_kind"
    PROFILE_SWITCH_TO_KEY: Final[str] = "_switch_to"
    STATUS_CODE_KEY: Final[str] = "_status_code"

    STATUS_EXECUTION_INTERRUPTED: Final[str] = "execution_interrupted"
    STATUS_EXECUTION_FAILED: Final[str] = "execution_failed"
    STATUS_SESSION_CLOSED: Final[str] = "session_closed"
    STATUS_SUB_AGENT_STATE_DISCARDED: Final[str] = "sub_agent_state_discarded"
    STATUS_AWAITING_SUB_AGENTS: Final[str] = "awaiting_sub_agents"
    STATUS_CODES: Final[frozenset[str]] = frozenset(
        {
            STATUS_EXECUTION_INTERRUPTED,
            STATUS_EXECUTION_FAILED,
            STATUS_SESSION_CLOSED,
            STATUS_SUB_AGENT_STATE_DISCARDED,
            STATUS_AWAITING_SUB_AGENTS,
        }
    )

    # Mid-turn user-message flags (see ``chrys.foundation.models.turns``).
    # ``_injected`` marks user-authored mid-turn input (live injections,
    # resume guidance) — permanent user content that must not open a turn.
    # ``_continuation`` marks the synthetic ``"continue"`` nudge the orchestration
    # sends on resume — never user-authored, removed post-run.  Both live in
    # ``additional_properties``: never serialized to the wire, persisted
    # wholesale in ``session.json``.
    INJECTED_KEY: Final[str] = "_injected"
    CONTINUATION_KEY: Final[str] = "_continuation"
    MID_TURN_USER_KEYS: Final[frozenset[str]] = frozenset({INJECTED_KEY, CONTINUATION_KEY})
    # Per-consumption identity for injected messages: stamped on the wire
    # copy at consumption and on the persisted/replayed history copy, so
    # crash-recovery replay dedups by identity — same-TEXT matching cannot
    # distinguish a persisted copy of this consumption from a distinct
    # earlier injection that happens to share the text.
    INJECTION_ID_KEY: Final[str] = "_injection_id"
    # System reminders a user message carried on an established provider
    # request, as ``[{"kind": ..., "text": ...}]``.  The reminder middleware
    # re-renders them on every later call so the conversation prefix stays
    # byte-identical across turns; the message's own contents never hold them.
    SYSTEM_REMINDERS_KEY: Final[str] = "_chrys_system_reminders"
    # The progress note a user message carries for the work of its turn that
    # compaction dropped, as ``{"text": ..., "note": ...}``: ``text`` is the
    # whole LAST_WORDS block the message is sent with (note, todo list and
    # dropped-call manifest), ``note`` the note alone.  Written whenever a
    # drop commits, on the one message of the turn that carries the note, and
    # re-rendered from here on every later call, in later turns too.
    LAST_WORDS_RECORD_KEY: Final[str] = "_chrys_last_words"
    # What the user typed, on a user message whose text was sent otherwise (a
    # prompt shortcut's ``!`command``` placeholders replaced by their output).
    # The approval judge reads it as the user's request; command output is data.
    TYPED_TEXT_KEY: Final[str] = "_chrys_typed_text"
    # On the one summary message that lists archived fold blocks instead of
    # summarising a block of its own: its lines as JSON-ready dicts, oldest
    # first.  Consumers that show one card per fold skip the message.
    ARCHIVE_INDEX_KEY: Final[str] = "_archive_index"

    TURN: Final[str] = "turn"
    SUMMARY: Final[str] = "summary"
    INTERRUPTED: Final[str] = "interrupted"
    AWAITING_SUB_AGENTS: Final[str] = "awaiting_sub_agents"

    STATUS_MARKERS: Final[frozenset[str]] = frozenset({INTERRUPTED, AWAITING_SUB_AGENTS})
    SESSION_COUNT_EXCLUDED: Final[frozenset[str]] = frozenset({TURN, INTERRUPTED, AWAITING_SUB_AGENTS})
    LAST_WORDS_EXCLUDED: Final[frozenset[str]] = frozenset({TURN, SUMMARY, INTERRUPTED})


@dataclass(frozen=True, slots=True)
class LastWordsRecord:
    """The progress note recorded on a user message (``LAST_WORDS_RECORD_KEY``)."""

    text: str
    """The LAST_WORDS block as the message is sent with it, without the reminder tags."""
    note: str
    """The note alone: what the next note of the turn merges, and what a fold of the turn keeps."""
    records_dir: str = ""
    """The directory of dropped-call records *text* points at, relative to the session's own; empty when it names none.

    *text* names it under the directory the session had when the note was
    written.  A copy of the session lives in another, so the block is sent
    with the directory of the session that sends it.
    """

    def to_record(self) -> dict[str, str]:
        """A fresh JSON-ready dict; a stored record is replaced, never changed in place."""
        record = {"text": self.text, "note": self.note}
        if self.records_dir:
            record["records_dir"] = self.records_dir
        return record

    def text_in(self, session_root: Path | None) -> str:
        """The block as the session at *session_root* shows it: its dropped-call records are that session's copies.

        Everything that shows the block to a model goes through here, the
        request and a recall of the folded turn alike.  Without a session
        directory, or a list of records, the text stands as recorded.
        """
        if session_root is None or not self.records_dir:
            return self.text
        heading = dropped_records_heading(self.records_dir, session_root)
        if heading in self.text:
            return self.text
        lines = self.text.split("\n")
        stale_end = f"/{self.records_dir.rstrip('/')}{_DROPPED_RECORDS_HEADING_END}"
        for index in range(len(lines) - 1, -1, -1):
            line = lines[index]
            if line.startswith(_DROPPED_RECORDS_HEADING_START) and line.endswith(stale_end):
                lines[index] = heading
                return "\n".join(lines)
        return self.text


_DROPPED_RECORDS_HEADING_START = "--- Dropped this turn (records under "
_DROPPED_RECORDS_HEADING_END = "/) ---"


def dropped_records_heading(records_dir: str, session_root: Path | None) -> str:
    """The line of a progress-note block that says where the turn's dropped-call records are.

    The one line of the block that names a place on disk, and so the one
    line that differs between a session and a fork of it.
    """
    directory = (session_root / records_dir).absolute().as_posix() if session_root is not None else records_dir
    return f"{_DROPPED_RECORDS_HEADING_START}{directory.rstrip('/')}{_DROPPED_RECORDS_HEADING_END}"


def read_last_words_record(properties: Mapping[str, Any] | None) -> LastWordsRecord | None:
    """The progress note recorded in a user message's *properties*, if a well-formed one is."""
    raw = properties.get(HistoryMarkerKind.LAST_WORDS_RECORD_KEY) if properties is not None else None
    if not isinstance(raw, dict):
        return None
    text = raw.get("text")
    note = raw.get("note")
    if not isinstance(text, str) or not text:
        return None
    records_dir = raw.get("records_dir")
    return LastWordsRecord(
        text,
        note if isinstance(note, str) else "",
        records_dir if isinstance(records_dir, str) else "",
    )


def copy_reminder_record(source: Mapping[str, Any] | None, target: dict[str, Any]) -> None:
    """Carry *source*'s per-message record onto *target*, another copy of the same user message.

    The record is what the message carried on its established request (its
    reminders and the progress note of its turn) and what the user typed when
    that differs from its text (``TYPED_TEXT_KEY``). A rebuilt copy that lost
    the reminders or the note would render bare on later requests and change
    the cached prefix from there on, and one that lost the typed text would
    show the approval judge command output as the user's request, so every
    site that rebuilds a user message calls this: the persisted injection
    copy, retry replay and its failure fallback, the crash-recovery
    checkpoint and a child's retry seed.  Entries are copied fresh: the
    source dict may still be live.
    """
    if source is None:
        return
    record = source.get(HistoryMarkerKind.SYSTEM_REMINDERS_KEY)
    if isinstance(record, list) and record:
        target[HistoryMarkerKind.SYSTEM_REMINDERS_KEY] = [dict(entry) for entry in record if isinstance(entry, dict)]
    last_words = read_last_words_record(source)
    if last_words is not None:
        target[HistoryMarkerKind.LAST_WORDS_RECORD_KEY] = last_words.to_record()
    typed = source.get(HistoryMarkerKind.TYPED_TEXT_KEY)
    if isinstance(typed, str) and typed:
        target[HistoryMarkerKind.TYPED_TEXT_KEY] = typed
