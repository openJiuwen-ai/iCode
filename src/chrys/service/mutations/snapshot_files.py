# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Helpers for rollback snapshot files and turn prompt previews."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.turns import is_continuation_message
from chrys.foundation.util.lock import FileLock
from chrys.service.state._session_files import (
    SESSION_WRITE_LOCK_TIMEOUT_SECONDS,
    atomic_copy_file,
    parse_snapshot_turn,
)
from chrys.service.trajectory.state import TurnRecord, turn_records

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from chrys.kernel import Message
    from chrys.service.context.providers.history import CompressedBlock

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RollbackSnapshot:
    """A rollback target's snapshot file and the turn registry inside it."""

    path: Path
    turns: list[TurnRecord]
    """The registry as of that turn; empty when the snapshot predates the registry."""
    readable: bool = True
    """Whether the payload parsed. An unreadable snapshot names no turns at all —
    not even the empty registry a readable snapshot without the key holds."""


def rollback_snapshot_paths(session_dir: Path | None) -> list[Path]:
    """Return rollback snapshot files sorted by their filename turn."""
    if session_dir is None:
        return []
    snap_dir = session_dir / "snapshots"
    if not snap_dir.is_dir():
        return []
    return sorted(
        (p for p in snap_dir.glob("*.json") if parse_snapshot_turn(p) >= 1),
        key=parse_snapshot_turn,
    )


def rollback_snapshot_path(session_dir: Path | None, turn: int) -> Path | None:
    """Return the rollback snapshot path for *turn*."""
    if session_dir is None:
        return None
    return session_dir / "snapshots" / f"turn_{turn}.json"


def prune_rollback_snapshots(session_dir: Path | None, keep: int) -> None:
    """Drop rollback snapshots older than the newest *keep* entries."""
    if session_dir is None:
        return
    snap_dir = session_dir / "snapshots"
    if not snap_dir.is_dir():
        return

    effective_keep = max(1, keep)
    snaps = rollback_snapshot_paths(session_dir)
    if len(snaps) <= effective_keep:
        return
    for old in snaps[:-effective_keep]:
        try:
            old.unlink()
        except OSError:
            logger.debug("Failed to prune rollback snapshot %s", old, exc_info=True)


def write_rollback_snapshot(
    *,
    session_dir: Path | None,
    session_id: str | None,
    turn_number: int,
    keep: int,
    lock_path: Path | None,
) -> None:
    """Atomically copy ``session.json`` to the current turn snapshot."""
    if session_dir is None or session_id is None:
        return
    session_file = session_dir / "session.json"
    if not session_file.exists():
        return
    snapshot_path = rollback_snapshot_path(session_dir, turn_number)
    if snapshot_path is None or lock_path is None:
        return
    try:
        with FileLock(lock_path, timeout=SESSION_WRITE_LOCK_TIMEOUT_SECONDS):
            if not session_file.exists():
                return
            atomic_copy_file(session_file, snapshot_path)
    except TimeoutError:
        logger.warning("Timed out acquiring session lock for rollback snapshot %s", snapshot_path, exc_info=True)
        return
    except OSError:
        logger.warning("Failed to write rollback snapshot %s", snapshot_path, exc_info=True)
        return
    prune_rollback_snapshots(session_dir, keep)


def snapshot_target_turns(session_dir: Path | None) -> set[int]:
    """Return rollback target turns implied by on-disk snapshots."""
    turns: set[int] = set()
    for path in rollback_snapshot_paths(session_dir):
        turn = snapshot_target_turn(path)
        if turn is not None and turn >= 1:
            turns.add(turn)
    return turns


def rollback_snapshot_for_target(session_dir: Path | None, target_turn: int) -> RollbackSnapshot | None:
    """Return the newest snapshot whose payload represents *target_turn*, with its turn registry."""
    matches = [path for path in rollback_snapshot_paths(session_dir) if snapshot_target_turn(path) == target_turn]
    if not matches:
        return None
    chosen = max(matches, key=parse_snapshot_turn)
    state = _snapshot_payload_state(chosen)
    if state is None:
        # The payload does not parse: the snapshot cannot be shown to hold any
        # turn, so it is reported as naming none rather than as an empty registry.
        return RollbackSnapshot(path=chosen, turns=[], readable=False)
    return RollbackSnapshot(path=chosen, turns=turn_records(state))


def snapshot_target_turn(path: Path) -> int | None:
    """Return the turn count represented by a rollback snapshot.

    Modern snapshots are taken at the start of turn ``N`` and therefore
    contain ``state.turn_counter == N - 1``.  Older snapshots were named
    after the completed turn and contain ``state.turn_counter == N``.
    Reading the payload first keeps both layouts usable.  Malformed
    prefixed snapshots fall back to the modern filename convention;
    malformed bare numeric snapshots fall back to the legacy after-turn
    convention (``1.json`` means turn 1).
    """
    turn = _snapshot_payload_turn_counter(path)
    if turn is not None:
        return turn
    snapshot_turn = parse_snapshot_turn(path)
    if snapshot_turn >= 1:
        return snapshot_turn if _is_legacy_numeric_snapshot_name(path) else snapshot_turn - 1
    return None


def _is_legacy_numeric_snapshot_name(path: Path) -> bool:
    stem = path.stem
    return bool(stem) and stem.isdecimal()


def _snapshot_payload_turn_counter(path: Path) -> int | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return _cached_snapshot_turn(path, stat.st_mtime_ns, stat.st_size)


@cache
def _cached_snapshot_turn(path: Path, mtime_ns: int, size: int) -> int | None:
    # A replaced whole-file snapshot gets a new key, including when its turn number is reused.
    _ = mtime_ns, size
    state = _snapshot_payload_state(path)
    if state is None:
        return None
    counter = _nonnegative_int(state.get("turn_counter"))
    if counter is not None:
        if counter > 0 or not _state_has_messages(state):
            return counter
        return _max_marker_turn(state)
    return _max_marker_turn(state)


def _snapshot_payload_state(path: Path) -> dict[str, object] | None:
    """The snapshot's provider state, read from the envelope or the bare payload."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError, UnicodeDecodeError, OSError:
        return None
    if not isinstance(raw, dict):
        return None
    state = raw.get("state")
    if isinstance(state, dict):
        return state
    return raw


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _state_has_messages(state: dict[str, object]) -> bool:
    messages = state.get("messages")
    return isinstance(messages, list) and bool(messages)


def _max_marker_turn(state: dict[str, object]) -> int | None:
    messages = state.get("messages")
    if not isinstance(messages, list):
        return None
    max_turn: int | None = None
    for message in messages:
        if not isinstance(message, dict):
            continue
        additional = message.get("additional_properties")
        if not isinstance(additional, dict):
            continue
        turn = _nonnegative_int(additional.get("_turn"))
        if turn is not None and (max_turn is None or turn > max_turn):
            max_turn = turn
    return max_turn


def scan_turn_prompts(messages: Sequence[Message], out: dict[int, str]) -> None:
    """Walk messages and fill ``out[turn] = first_user_prompt``.

    Synthetic ``continue`` nudges are skipped — a crash-leftover nudge
    would otherwise label a rollback preview "continue".
    """
    pending: list[str] = []
    for message in messages:
        ap = message.additional_properties or {}
        kind = ap.get(HistoryMarkerKind.KEY)
        if kind == HistoryMarkerKind.TURN:
            idx = ap.get("_turn")
            if isinstance(idx, int) and pending and idx not in out:
                out[idx] = pending[0]
            pending = []
            continue
        if kind is not None:
            continue
        if message.role == "user" and not is_continuation_message(message):
            text = (message.text or "").strip()
            if text:
                pending.append(text)


def turn_prompt_previews(
    messages: Sequence[Message],
    compressed_blocks: Iterable[CompressedBlock],
) -> dict[int, str]:
    """Return ``{turn_number: first_user_prompt}`` from live and compressed history."""
    result: dict[int, str] = {}
    try:
        scan_turn_prompts(messages, result)
        for block in compressed_blocks:
            scan_turn_prompts(list(block.messages or []), result)
    except Exception:
        return {}
    return result
