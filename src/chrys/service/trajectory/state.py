# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Turn identity persisted alongside the conversation (``chrys_trajectory`` state key).

``turn_id`` is minted per logical turn and never reused, so it survives the
ordinal shifting ``turn_number`` goes through: older sessions can carry
counter drift, and a rollback may hand an ordinal out again. Identity is
therefore the id, and the registry is
an append-ordered list — a later turn never overwrites an earlier one's
identity even when they share an ordinal.

The registry rides along with every save, snapshot, restore and fork exactly
like the messages it describes, and a rollback snapshot therefore restores
the registry as of that turn. ``started_sequence`` is the sequence the
turn's ``turn.started`` landed at in *this* session's log: it is what a
rollback turns into a superseded range, so a turn inherited by a fork starts
without one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

TRAJECTORY_STATE_KEY: Final = "chrys_trajectory"
_TURNS_KEY: Final = "turns"
"""Pre-append registry layout: ``{turn_number: {turn_id, started_sequence}}``. Read-only."""
_TURN_LOG_KEY: Final = "turn_log"
"""Append-ordered list of turn records, each holding only its non-None fields."""
_LEGACY_RETRY_GUARD_KEY: Final = "legacy_retry_guard"
"""A converted legacy registry still needs ordinal validation before its last turn is resumed."""


@dataclass(frozen=True, slots=True)
class TurnRecord:
    turn_number: int
    turn_id: str
    started_sequence: int | None = None
    """Sequence of the turn's ``turn.started`` in this session's log; ``None`` when unrecorded."""
    opening_item_id: str | None = None
    """``_chrys_analytics_item_id`` of the user message that opened the turn."""
    user_message_id: str | None = None
    """Client-supplied (ACP ``messageId``) or backend-minted id of that same message."""
    origin_session_id: str | None = None
    """Session a fork inherited this turn from; the turn may also have run in the fork."""


def append_turn(state: dict[str, Any], record: TurnRecord) -> None:
    """Register *record*, keeping every earlier turn's identity.

    Appends unconditionally: the same ordinal may name two different turns
    once the runtime and saved counters drift apart, and the append is what
    keeps both identifiable. Which turn a retry belongs to is the runner's
    call, not this function's.
    """
    entries = _entries_for_write(state)
    entries.append(_entry_from_record(record))
    state[TRAJECTORY_STATE_KEY].pop(_LEGACY_RETRY_GUARD_KEY, None)


def confirm_resumed_turn(state: dict[str, Any]) -> None:
    """Trust the last entry after the runner has validated and admitted its retry.

    Admission establishes which turn is current even without a trajectory
    writer. Future retries can use that identity despite saved-counter drift.
    """
    _entries_for_write(state)
    state[TRAJECTORY_STATE_KEY].pop(_LEGACY_RETRY_GUARD_KEY, None)


def mark_turn_sequence(state: dict[str, Any], logical_turn_id: str, sequence: int) -> None:
    """Record where *logical_turn_id*'s ``turn.started`` landed in this session's log.

    Only the first pass of a turn fills this in: the sequence a retry lands
    at is where its own ``turn.started`` sits, and a rollback has to supersede
    the turn from where it first opened. A turn a fork inherited has none
    until it runs here, which is how its first local pass claims the range.
    """
    entries = _entries_for_read(state)
    for index, entry in enumerate(entries):
        if entry.get("turn_id") != logical_turn_id:
            continue
        if entry.get("started_sequence") is not None:
            return
        updated = dict(entry, started_sequence=sequence)
        _entries_for_write(state)[index] = updated
        return


def turn_records(state: dict[str, Any] | None) -> list[TurnRecord]:
    """Return every registered turn, in the order the turns started."""
    records: list[TurnRecord] = []
    for entry in _entries_for_read(state):
        record = _record_from_entry(entry)
        if record is not None:
            records.append(record)
    return records


def discard_last_turn(state: dict[str, Any], *, turn_number: int) -> TurnRecord | None:
    """Remove just the empty turn being replaced, preserving older identities."""
    entries = _entries_for_read(state)
    for index in range(len(entries) - 1, -1, -1):
        record = _record_from_entry(entries[index])
        if record is None:
            continue
        if record.turn_number != turn_number:
            return None
        del _entries_for_write(state)[index]
        return record
    return None


def legacy_turn_records(state: dict[str, Any] | None) -> list[TurnRecord] | None:
    """Return turns requiring legacy ordinal validation, or ``None`` for a trusted registry.

    A caller that has to judge a stored turn against the ordinal it reads
    needs to know whether the last turn was registered at admission. Converting
    an ordinal-keyed registry, including while forking, does not establish that:
    its last entry only belongs to a pass whose ordinal it carries.
    """
    if not state:
        return None
    registry = state.get(TRAJECTORY_STATE_KEY)
    if not isinstance(registry, dict):
        return None
    if isinstance(registry.get(_TURN_LOG_KEY), list) and registry.get(_LEGACY_RETRY_GUARD_KEY) is not True:
        return None
    return turn_records(state)


def fork_turn_registry(state: dict[str, Any], *, origin_session_id: str) -> None:
    """Carry the turn registry into a fork, restating where each turn came from.

    The fork records into a log of its own, so the sequences point into the
    parent's log and would make a rollback here name a range that never
    existed: they are cleared and only a pass the fork itself runs claims
    one. ``origin_session_id`` names the session that first recorded the
    turn and is only filled in when unset, so a fork of a fork still points
    at the original.
    """
    needs_ordinal_check = legacy_turn_records(state) is not None
    entries = _entries_for_read(state)
    forked: list[dict[str, Any]] = []
    for entry in entries:
        carried = {key: value for key, value in entry.items() if key != "started_sequence"}
        # A stored origin the reader would drop (empty, or not a string) reads
        # as absent, so it is restated rather than left to read as no origin.
        origin = _optional_str(carried.get("origin_session_id"))
        carried["origin_session_id"] = origin if origin is not None else origin_session_id
        forked.append(carried)
    registry: dict[str, Any] = {_TURN_LOG_KEY: forked}
    if needs_ordinal_check:
        registry[_LEGACY_RETRY_GUARD_KEY] = True
    state[TRAJECTORY_STATE_KEY] = registry


def _entries_for_write(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the append-ordered entries, rewriting a legacy registry in place."""
    registry = state.get(TRAJECTORY_STATE_KEY)
    if not isinstance(registry, dict):
        registry = {}
        state[TRAJECTORY_STATE_KEY] = registry
    entries = registry.get(_TURN_LOG_KEY)
    if not isinstance(entries, list):
        entries = _legacy_entries(registry.get(_TURNS_KEY))
        if _TURNS_KEY in registry:
            registry[_LEGACY_RETRY_GUARD_KEY] = True
        registry.pop(_TURNS_KEY, None)
        registry[_TURN_LOG_KEY] = entries
    else:
        entries = [entry for entry in entries if isinstance(entry, dict)]
        registry[_TURN_LOG_KEY] = entries
    return entries


def _entries_for_read(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return the entries without touching *state* (a legacy registry included)."""
    if not state:
        return []
    registry = state.get(TRAJECTORY_STATE_KEY)
    if not isinstance(registry, dict):
        return []
    entries = registry.get(_TURN_LOG_KEY)
    if isinstance(entries, list):
        return [entry for entry in entries if isinstance(entry, dict)]
    return _legacy_entries(registry.get(_TURNS_KEY))


def _legacy_entries(turns: Any) -> list[dict[str, Any]]:
    """Convert the ordinal-keyed layout to the append-ordered one, earliest turn first.

    Ordering the entries means deciding when their turns ran, and only the
    sequences can say that: the ordinal an entry is keyed by is exactly what
    drifts (a failed or cancelled turn leaves the saved counter behind the
    running one, and a reopen restores the saved one). A registry where every
    entry recorded its sequence is therefore ordered by it. One where any entry
    did not — an older layout, a partial write — is ordered by the ordinal
    throughout: the entries that do carry a sequence cannot be interleaved with
    the ones that do not without ranking a known time against an unknown one,
    and the ordinal is at least the order those entries were stored in.
    """
    if not isinstance(turns, dict):
        return []
    keyed: list[tuple[int, dict[str, Any]]] = []
    for key, entry in turns.items():
        if not isinstance(entry, dict) or not isinstance(key, str) or not key.isdigit():
            continue
        keyed.append((int(key), entry))
    entries = [dict(entry, turn_number=turn_number) for turn_number, entry in keyed]
    sequences = [_nonnegative_int(entry.get("started_sequence")) for entry in entries]
    if entries and all(sequence is not None for sequence in sequences):
        entries.sort(key=lambda entry: _nonnegative_int(entry.get("started_sequence")) or 0)
    else:
        entries.sort(key=lambda entry: _nonnegative_int(entry.get("turn_number")) or 0)
    return entries


def _entry_from_record(record: TurnRecord) -> dict[str, Any]:
    entry: dict[str, Any] = {"turn_number": record.turn_number, "turn_id": record.turn_id}
    if record.started_sequence is not None:
        entry["started_sequence"] = record.started_sequence
    if record.opening_item_id is not None:
        entry["opening_item_id"] = record.opening_item_id
    if record.user_message_id is not None:
        entry["user_message_id"] = record.user_message_id
    if record.origin_session_id is not None:
        entry["origin_session_id"] = record.origin_session_id
    return entry


def _record_from_entry(entry: dict[str, Any]) -> TurnRecord | None:
    """Build a record from a stored entry, dropping anything malformed."""
    turn_number = _nonnegative_int(entry.get("turn_number"))
    turn_id = entry.get("turn_id")
    if turn_number is None or not isinstance(turn_id, str) or not turn_id:
        return None
    return TurnRecord(
        turn_number=turn_number,
        turn_id=turn_id,
        started_sequence=_nonnegative_int(entry.get("started_sequence")),
        opening_item_id=_optional_str(entry.get("opening_item_id")),
        user_message_id=_optional_str(entry.get("user_message_id")),
        origin_session_id=_optional_str(entry.get("origin_session_id")),
    )


def _nonnegative_int(value: object) -> int | None:
    """Return *value* as a non-negative int, rejecting bools and anything else."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
