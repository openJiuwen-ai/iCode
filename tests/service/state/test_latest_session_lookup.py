# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Store-level tests for load_latest_session_id and the MRU index (unit tests live in test_session_mru.py)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

import chrys.service.state.session_mru as session_mru_module
import chrys.service.state.store as store_module
from chrys.foundation.util.lock import FileLock
from chrys.kernel import Message
from chrys.service.state.session_mru import SESSION_MRU_FILE_NAME, SessionMruEntry, SessionMruIndex, coerce_utc
from chrys.service.state.store import (
    JsonFileStateStore,
)
from tests.service.state._store_helpers import _save


def _mru_ids(root: Path) -> list[str]:
    snapshot = SessionMruIndex(root).load()
    assert snapshot is not None
    return [entry.session_id for entry in snapshot.sessions]


def _mru_raw(root: Path) -> dict:
    return json.loads((root / SESSION_MRU_FILE_NAME).read_text(encoding="utf-8"))


def _count_scans(monkeypatch: pytest.MonkeyPatch, store: JsonFileStateStore) -> list[int]:
    scans = [0]
    original = store._scan_session_metas_sync

    def counting() -> list:
        scans[0] += 1
        return original()

    monkeypatch.setattr(store, "_scan_session_metas_sync", counting)
    return scans


async def test_save_records_mru_and_unchanged_save_keeps_order(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await _save(store, "older", "a")
    await _save(store, "newer", "a")
    assert _mru_ids(tmp_path) == ["newer", "older"]
    assert _mru_raw(tmp_path)["complete"] is False

    # Same visible message count -> updated_at preserved -> no promotion.
    await _save(store, "older", "a")
    assert _mru_ids(tmp_path) == ["newer", "older"]

    await _save(store, "older", "a", "b")
    assert _mru_ids(tmp_path) == ["older", "newer"]
    assert await store.load_latest_session_id() == "older"


async def test_latest_session_prefers_valid_index_without_scanning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)
    await _save(store, "s1", "a")
    await _save(store, "s2", "a")
    scans = _count_scans(monkeypatch, store)

    # Incomplete index -> exactly one backfill scan, then complete.
    assert await store.load_latest_session_id() == "s2"
    assert scans[0] == 1
    assert _mru_raw(tmp_path)["complete"] is True

    assert await store.load_latest_session_id() == "s2"
    assert scans[0] == 1


async def test_latest_session_rebuilds_missing_and_corrupt_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)
    await _save(store, "s1", "a")
    await _save(store, "s2", "a")
    scans = _count_scans(monkeypatch, store)

    (tmp_path / SESSION_MRU_FILE_NAME).unlink()
    assert await store.load_latest_session_id() == "s2"
    assert scans[0] == 1
    assert _mru_ids(tmp_path) == ["s2", "s1"]

    (tmp_path / SESSION_MRU_FILE_NAME).write_text("{corrupt", encoding="utf-8")
    assert await store.load_latest_session_id() == "s2"
    assert scans[0] == 2
    assert _mru_raw(tmp_path)["complete"] is True

    assert await store.load_latest_session_id() == "s2"
    assert scans[0] == 2


async def test_latest_session_returns_none_without_sessions(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    assert await store.load_latest_session_id() is None
    assert _mru_raw(tmp_path)["complete"] is True
    assert _mru_ids(tmp_path) == []


async def test_recovery_only_session_becomes_latest(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await _save(store, "saved", "a")
    await asyncio.to_thread(
        store.save_recovery_session,
        "crashed",
        {"messages": [Message("user", ["in-flight"])], "compressed_msgs": []},
    )
    assert _mru_ids(tmp_path)[0] == "crashed"
    assert await store.load_latest_session_id() == "crashed"
    listed = sorted(await store.list_sessions(), key=lambda m: m.updated_at, reverse=True)
    assert listed[0].session_id == "crashed"


async def test_latest_session_survives_corrupt_primary_via_backup(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await _save(store, "old", "a")
    await _save(store, "top", "a")
    assert await store.load_latest_session_id() == "top"
    (tmp_path / "top" / "session.json").write_text("{corrupt", encoding="utf-8")
    assert await store.load_latest_session_id() == "top"


@pytest.mark.parametrize(
    ("filename", "deny_by_mode", "reopen_store"),
    [
        ("newer.json", False, False),
        ("newer.json", True, False),
        ("imported.json", False, False),
        ("imported.json", False, True),
    ],
)
async def test_latest_session_skips_an_unreadable_legacy_flat_until_it_can_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str, deny_by_mode: bool, reopen_store: bool
) -> None:
    store = JsonFileStateStore(tmp_path)
    await _save(store, "older", "a")
    await _save(store, "newer", "b")
    assert await store.load_latest_session_id(chat_only=True) == "newer"
    directory = store.session_dir("newer")
    flat = (directory / "session.json").rename(tmp_path / filename)
    (directory / "session.json.bak").unlink()
    directory.rmdir()
    assert {meta.session_id for meta in await store.list_sessions()} == {"older", "newer"}
    if reopen_store:
        store = JsonFileStateStore(tmp_path)
    open_file = Path.open

    def denied(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if path == flat:
            raise PermissionError("archive temporarily unavailable")
        return open_file(path, mode, buffering, encoding, errors, newline)

    with ExitStack() as cleanup:
        if deny_by_mode:
            cleanup.callback(flat.chmod, flat.stat().st_mode)
            flat.chmod(0)
            if os.access(flat, os.R_OK):
                pytest.skip("This filesystem allows reading a mode-000 file")
        else:
            cleanup.enter_context(monkeypatch.context()).setattr(Path, "open", denied)
        assert await store.load_latest_session_id(chat_only=True) == "older"
        assert flat.is_file() and "newer" in _mru_ids(tmp_path)
    assert await store.load_latest_session_id(chat_only=True) == "newer"


async def test_fork_enters_index_first_and_delete_removes(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    parent = str(uuid4())
    await _save(store, parent, "a")
    await _save(store, "other", "a")
    fork_id = await asyncio.to_thread(store.fork_session, parent)
    assert _mru_ids(tmp_path)[0] == fork_id
    assert await store.load_latest_session_id() == fork_id

    await store.delete_session(fork_id)
    assert fork_id not in _mru_ids(tmp_path)
    assert await store.load_latest_session_id() == "other"


async def test_mru_records_before_the_envelope_commits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Save / recovery / fork index first and commit second.

    A crash between the two can only leave the index *ahead* of disk — which
    verification re-ranks in memory — never behind it with a stale
    ``complete`` index that would hide the committed session forever.
    """
    store = JsonFileStateStore(tmp_path)
    parent = str(uuid4())
    await _save(store, parent, "a")
    original_record = store._mru.record
    # Observations only — an assertion raised inside ``record`` would be
    # swallowed by ``_note_mru``'s best-effort boundary, so assert afterwards.
    observed: list[tuple[str, bool, str | None, str]] = []
    probing = False

    def probe(session_id: str, updated_at: datetime) -> None:
        nonlocal probing
        if probing:  # the racing lookup's own write-back of a verified stamp
            original_record(session_id, updated_at)
            return
        probing = True
        on_disk = store._meta_for_session_dir(store._session_dir(session_id))
        # Nothing committed yet (fork) or the pre-save envelope (save/recovery).
        uncommitted = on_disk is None or coerce_utc(on_disk.updated_at) < coerce_utc(updated_at)
        original_record(session_id, updated_at)
        # A lookup racing the commit sees the index ahead of disk: it must
        # neither pick the uncommitted session nor prune it as a ghost.
        observed.append((session_id, uncommitted, store._load_latest_session_id_sync(), _mru_ids(tmp_path)[0]))
        probing = False

    monkeypatch.setattr(store._mru, "record", probe)
    await _save(store, parent, "a", "b")
    await asyncio.to_thread(
        store.save_recovery_session,
        parent,
        {"messages": [Message("user", ["x"])], "compressed_msgs": []},
    )
    fork_id = await asyncio.to_thread(store.fork_session, parent)
    assert observed == [
        (parent, True, parent, parent),
        (parent, True, parent, parent),
        (fork_id, True, parent, fork_id),
    ]
    assert _mru_ids(tmp_path)[0] == fork_id
    assert await store.load_latest_session_id() == fork_id


async def test_ghost_prune_rechecks_absence_under_the_write_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork that commits between lookup's first absence check and its lock probe is kept."""
    store = JsonFileStateStore(tmp_path)
    await _save(store, "parent", "a")
    fork_id = str(uuid4())
    # Mid-commit fork: recorded, write lock held, directory not renamed yet.
    store._mru.record(fork_id, datetime.now(UTC) + timedelta(seconds=1))
    writer = FileLock(store._write_lock_path(fork_id))
    writer.acquire()
    original_legacy_path = store._legacy_path
    committed = False

    def legacy_path(session_id: str) -> Path:
        nonlocal committed
        path = original_legacy_path(session_id)
        if session_id == fork_id and not committed:
            committed = True
            # Lookup has just seen the directory absent; the fork now lands
            # and releases before lookup can probe the write lock.
            writer.release()
            store._save_session_sync(fork_id, {"messages": [Message("user", ["a"])], "compressed_msgs": []})
        return path

    monkeypatch.setattr(store, "_legacy_path", legacy_path)
    removed: list[str] = []
    monkeypatch.setattr(store._mru, "remove", removed.append)
    # The verify pass skips the in-flight entry, but must not prune it; the
    # closing sweep then sees the fork's freshly committed folder.
    assert store._load_latest_session_id_sync() == fork_id
    assert committed
    assert removed == []
    assert fork_id in _mru_ids(tmp_path)
    monkeypatch.setattr(store, "_legacy_path", original_legacy_path)
    assert await store.load_latest_session_id() == fork_id


async def test_stuck_index_lock_neither_stalls_saves_nor_hides_the_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Index writers give up after the (shrunk) timeout; the sweep still finds the commit."""
    monkeypatch.setattr(session_mru_module, "SESSION_MRU_LOCK_TIMEOUT_SECONDS", 0.05)
    store = JsonFileStateStore(tmp_path)
    await _save(store, "a", "a")
    assert await store.load_latest_session_id() == "a"  # complete index
    holder = FileLock(store._mru.lock_path)
    holder.acquire()
    done = threading.Event()

    def save_b() -> None:
        store._save_session_sync("b", {"messages": [Message("user", ["b"])], "compressed_msgs": []})
        done.set()

    worker = threading.Thread(target=save_b)
    worker.start()
    try:
        assert done.wait(2.0)  # bounded: does not wait for the holder
        assert store._session_file("b").exists()
        assert store._mru.load() is None  # read side times out and falls back too
    finally:
        holder.release()
    worker.join(timeout=5.0)
    assert _mru_ids(tmp_path) == ["a"]  # the record was skipped, index untouched
    scans = _count_scans(monkeypatch, store)
    assert await store.load_latest_session_id() == "b"  # via the modified-folder sweep
    assert scans[0] == 0
    assert _mru_ids(tmp_path)[0] == "b"  # ...which also repairs the index


async def test_lookup_stays_bounded_when_the_index_lock_is_stuck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_mru_module, "SESSION_MRU_LOCK_TIMEOUT_SECONDS", 0.05)
    store = JsonFileStateStore(tmp_path)
    await _save(store, "a", "a")
    await _save(store, "b", "a")
    holder = FileLock(store._mru.lock_path)
    holder.acquire()
    try:
        result: list[str | None] = []
        worker = threading.Thread(target=lambda: result.append(store._load_latest_session_id_sync()))
        worker.start()
        worker.join(timeout=5.0)
        assert not worker.is_alive()  # read timeout + scan + rebuild timeout, then returns
        assert result == ["b"]
    finally:
        holder.release()


async def test_sweep_finds_sessions_written_behind_the_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An older chrys or a copied folder never records; a lookup still sees it."""
    store = JsonFileStateStore(tmp_path)
    await _save(store, "old", "a")
    assert await store.load_latest_session_id() == "old"
    # A foreign writer drops a newer session folder straight onto disk.
    copied = str(uuid4())
    shutil.copytree(store.session_dir("old"), store.session_dir(copied))
    session_file = store._session_file(copied)
    data = json.loads(session_file.read_text(encoding="utf-8"))
    data["meta"]["session_id"] = copied
    data["meta"]["updated_at"] = datetime.now(UTC).isoformat()
    session_file.write_text(json.dumps(data), encoding="utf-8")
    scans = _count_scans(monkeypatch, store)
    assert await store.load_latest_session_id() == copied
    assert scans[0] == 0
    assert _mru_ids(tmp_path)[0] == copied  # repaired for next time
    # And a legacy flat file dropped in the same way (ranked via the listing).
    legacy_id = str(uuid4())
    stamp = datetime.now(UTC).isoformat()
    store._legacy_path(legacy_id).write_text(
        json.dumps({"meta": {"session_id": legacy_id, "created_at": stamp, "updated_at": stamp}, "state": {}}),
        encoding="utf-8",
    )
    assert await store.load_latest_session_id() == legacy_id


async def test_sweep_applies_the_listing_precedence_to_duplicate_legacy_ids(tmp_path: Path) -> None:
    """A later-sorted legacy copy of an id the listing suppresses must not win the lookup."""
    store = JsonFileStateStore(tmp_path)
    await _save(store, "winner", "a")
    winner_meta = await store.load_session_meta("winner")
    assert winner_meta is not None
    older = (winner_meta.updated_at - timedelta(minutes=5)).isoformat()
    newer = (winner_meta.updated_at + timedelta(minutes=5)).isoformat()
    for name, stamp in (("dupe.json", older), ("zzz.json", newer)):  # same embedded id; the first sorted wins
        (tmp_path / name).write_text(
            json.dumps({"meta": {"session_id": "dupe", "created_at": stamp, "updated_at": stamp}, "state": {}}),
            encoding="utf-8",
        )
    metas = await store.list_sessions()
    assert max(metas, key=lambda m: m.updated_at).session_id == "winner"
    store._mru.invalidate()
    assert await store.load_latest_session_id() == "winner"  # backfill + sweep
    assert await store.load_latest_session_id() == "winner"  # index path + sweep
    # A legacy copy of a folder session's id is suppressed by the folder too.
    (tmp_path / "copy.json").write_text(
        json.dumps({"meta": {"session_id": "winner", "created_at": newer, "updated_at": newer}, "state": {}}),
        encoding="utf-8",
    )
    metas = await store.list_sessions()
    assert [m.session_id for m in metas if m.session_id == "winner"] == ["winner"]
    assert await store.load_latest_session_id() == "winner"
    snapshot = store._mru.load()
    assert snapshot is not None
    assert snapshot.sessions[0] == SessionMruEntry("winner", coerce_utc(winner_meta.updated_at))  # copy not recorded


async def test_commit_after_failed_record_and_invalidate_is_still_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)
    await _save(store, "b", "a")
    assert await store.load_latest_session_id() == "b"

    broken = True
    real_record, real_invalidate = store._mru.record, store._mru.invalidate

    def record(*args: object, **kwargs: object) -> None:
        if broken:
            raise OSError("root not writable")
        real_record(*args, **kwargs)

    def invalidate() -> None:
        if broken:
            raise OSError("root not writable")
        real_invalidate()

    monkeypatch.setattr(store._mru, "record", record)
    monkeypatch.setattr(store._mru, "invalidate", invalidate)
    await _save(store, "a", "a")  # commits behind the old complete index
    assert _mru_ids(tmp_path) == ["b"]
    broken = False
    metas = await store.list_sessions()
    assert max(metas, key=lambda m: m.updated_at).session_id == "a"
    assert await store.load_latest_session_id() == "a"


async def test_recovery_only_session_hidden_during_backfill_surfaces_after_owner_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)
    await _save(store, "saved", "a")
    owner = FileLock(store.active_lock_path("live"))
    owner.acquire()
    try:
        await asyncio.to_thread(
            store.save_recovery_session,
            "live",
            {"messages": [Message("user", ["in-flight"])], "compressed_msgs": []},
        )
        store._mru.invalidate()  # backfill from scratch while the owner is alive
        assert await store.load_latest_session_id() == "saved"  # hidden, like list_sessions()
        assert _mru_ids(tmp_path) == ["saved"]
    finally:
        owner.release()
    metas = await store.list_sessions()
    assert max(metas, key=lambda m: m.updated_at).session_id == "live"
    scans = _count_scans(monkeypatch, store)
    assert await store.load_latest_session_id() == "live"  # via the sweep, no rescan
    assert scans[0] == 0
    assert _mru_ids(tmp_path)[0] == "live"


async def test_malformed_index_entry_triggers_rebuild(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = JsonFileStateStore(tmp_path)
    await _save(store, "old", "a")
    await _save(store, "new", "a")
    raw = _mru_raw(tmp_path)
    assert raw["sessions"][0]["session_id"] == "new"
    raw["sessions"][0]["last_updated_at"] = "garbage"
    (tmp_path / SESSION_MRU_FILE_NAME).write_text(json.dumps(raw), encoding="utf-8")
    scans = _count_scans(monkeypatch, store)
    assert await store.load_latest_session_id() == "new"
    assert scans[0] == 1
    assert _mru_ids(tmp_path) == ["new", "old"]


async def test_legacy_migration_records_before_rename_so_backfill_cannot_miss_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy file migrated between the folder and flat-file enumerations stays indexed."""
    store = JsonFileStateStore(tmp_path)
    await _save(store, "older", "a")
    newer = str(uuid4())
    stamp = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    envelope = {
        "meta": {"session_id": newer, "created_at": stamp, "updated_at": stamp, "message_count": 0},
        "state": {"messages": [], "compressed_msgs": []},
    }
    store._legacy_path(newer).write_text(json.dumps(envelope), encoding="utf-8")
    store._mru.invalidate()
    original_candidates = store._session_dir_candidates

    def candidates_then_migrate() -> list[Path]:
        found = original_candidates()
        store._migrate_if_needed(newer)  # another window opens the legacy session right now
        return found

    monkeypatch.setattr(store, "_session_dir_candidates", candidates_then_migrate)
    assert await store.load_latest_session_id() == newer
    monkeypatch.setattr(store, "_session_dir_candidates", original_candidates)
    snapshot = store._mru.load()
    assert snapshot is not None and snapshot.complete is True
    assert [e.session_id for e in snapshot.sessions] == [newer, "older"]
    assert await store.load_latest_session_id() == newer
    assert (await store.list_sessions())[0].session_id == newer


async def test_rebuild_keeps_horizon_raised_by_a_record_trimmed_during_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent record that only survives in the horizon must not be erased by the rebuild."""
    store = JsonFileStateStore(tmp_path)
    store._mru = SessionMruIndex(tmp_path, max_entries=2)
    for name in ("a", "y", "x"):
        await _save(store, name, "a")
    y_meta = await store.load_session_meta("y")
    assert y_meta is not None
    # Index retains stale-high x/y; on disk both rolled back below a, so
    # the next lookup has to rescan.
    _shift_updated_at(store, "a", -timedelta(days=2))
    _shift_updated_at(store, "x", -timedelta(days=1))
    _shift_updated_at(store, "y", -timedelta(days=1))
    # Legacy session s: newer than everything on disk, older than the stale
    # retained stamps — its record gets trimmed at once and only lifts the horizon.
    s_id = str(uuid4())
    s_stamp = (y_meta.updated_at - timedelta(minutes=1)).isoformat()
    envelope = {
        "meta": {"session_id": s_id, "created_at": s_stamp, "updated_at": s_stamp, "message_count": 0},
        "state": {"messages": [], "compressed_msgs": []},
    }
    store._legacy_path(s_id).write_text(json.dumps(envelope), encoding="utf-8")
    original_candidates = store._session_dir_candidates

    def candidates_then_migrate() -> list[Path]:
        found = original_candidates()
        store._migrate_if_needed(s_id)
        return found

    monkeypatch.setattr(store, "_session_dir_candidates", candidates_then_migrate)
    # The rescan races the migration: the scan misses s, but the rebuild's
    # merged ranking (not just the kept top-2) still surfaces its record.
    assert await store.load_latest_session_id() == s_id
    monkeypatch.setattr(store, "_session_dir_candidates", original_candidates)
    snapshot = store._mru.load()
    assert snapshot is not None and s_id not in [e.session_id for e in snapshot.sessions]
    assert snapshot.horizon is not None and snapshot.horizon >= coerce_utc(datetime.fromisoformat(s_stamp))
    metas = await store.list_sessions()
    assert max(metas, key=lambda m: m.updated_at).session_id == s_id
    assert await store.load_latest_session_id() == s_id


async def test_delete_removes_index_entry_before_a_same_id_recreate_can_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save recreating a just-deleted id must not lose its record to the delayed removal."""
    store = JsonFileStateStore(tmp_path)
    await _save(store, "other", "a")
    await _save(store, "reborn", "a")
    original_remove = store._mru.remove
    removing = threading.Event()
    release = threading.Event()
    saver_at_lock = threading.Event()
    reborn_lock_path = store._write_lock_path("reborn")

    class SignallingLock(FileLock):
        def acquire(self) -> None:
            if self._path == reborn_lock_path and threading.current_thread().name == "saver":
                saver_at_lock.set()
            super().acquire()

    monkeypatch.setattr(store_module, "FileLock", SignallingLock)

    def slow_remove(session_id: str) -> None:
        removing.set()
        assert release.wait(5.0)
        original_remove(session_id)

    store._mru.remove = slow_remove  # type: ignore[method-assign]
    deleter = threading.Thread(target=lambda: store._delete_session_sync("reborn"))
    deleter.start()
    assert removing.wait(5.0)
    saved = threading.Event()

    def recreate() -> None:
        store._save_session_sync("reborn", {"messages": [Message("user", ["again"])], "compressed_msgs": []})
        saved.set()

    saver = threading.Thread(target=recreate, name="saver")
    saver.start()
    assert saver_at_lock.wait(5.0)  # the recreate is at the write lock...
    assert not saved.wait(0.3)  # ...and blocked there until the removal is done
    release.set()
    deleter.join(timeout=5.0)
    saver.join(timeout=5.0)
    assert saved.is_set()
    assert _mru_ids(tmp_path)[0] == "reborn"
    assert await store.load_latest_session_id() == "reborn"
    metas = await store.list_sessions()
    assert max(metas, key=lambda m: m.updated_at).session_id == "reborn"


async def test_latest_session_returns_post_merge_latest_after_backfill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save that lands during the backfill scan wins this lookup, not just the next."""
    store = JsonFileStateStore(tmp_path)
    await _save(store, "s1", "a")
    await _save(store, "s2", "a")
    store._mru.invalidate()
    original = store._scan_session_metas_sync

    def scan_then_concurrent_save() -> list:
        listed = original()
        store._save_session_sync("s3", {"messages": [Message("user", ["late"])], "compressed_msgs": []})
        return listed

    monkeypatch.setattr(store, "_scan_session_metas_sync", scan_then_concurrent_save)
    assert await store.load_latest_session_id() == "s3"
    snapshot = SessionMruIndex(tmp_path).load()
    assert snapshot is not None and snapshot.complete is True
    assert [e.session_id for e in snapshot.sessions] == ["s3", "s2", "s1"]


async def test_backfill_ranks_every_merged_entry_not_just_the_leader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-flight record above a committed concurrent save must not hide that save."""
    store = JsonFileStateStore(tmp_path)
    await _save(store, "old", "a")
    store._mru.invalidate()
    original = store._scan_session_metas_sync
    inflight_lock = FileLock(store._write_lock_path("inflight"))

    def scan_then_concurrent_writers() -> list:
        listed = original()
        # Writer A: recorded (under its write lock) but not yet committed.
        inflight_lock.acquire()
        store._mru.record("inflight", datetime.now(UTC) + timedelta(seconds=30))
        # Writer B: recorded and committed, newer than "old".
        store._save_session_sync("committed", {"messages": [Message("user", ["b"])], "compressed_msgs": []})
        return listed

    monkeypatch.setattr(store, "_scan_session_metas_sync", scan_then_concurrent_writers)
    try:
        assert await store.load_latest_session_id() == "committed"
    finally:
        inflight_lock.release()
    metas = await store.list_sessions()
    assert max(metas, key=lambda m: m.updated_at).session_id == "committed"


async def test_backfill_accepts_a_merged_leader_that_advanced_before_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)
    await _save(store, "old", "a")
    store._mru.invalidate()
    original_scan = store._scan_session_metas_sync
    original_verify = store._mru_verify

    def scan_then_concurrent_save() -> list:
        listed = original_scan()
        store._save_session_sync("late", {"messages": [Message("user", ["1"])], "compressed_msgs": []})
        return listed

    def verify_after_another_save(session_id: str) -> datetime | None:
        if session_id == "late":
            monkeypatch.setattr(store, "_mru_verify", original_verify)
            store._save_session_sync("late", {"messages": [Message("user", ["1", "2"])], "compressed_msgs": []})
        return original_verify(session_id)

    monkeypatch.setattr(store, "_scan_session_metas_sync", scan_then_concurrent_save)
    monkeypatch.setattr(store, "_mru_verify", verify_after_another_save)
    assert await store.load_latest_session_id() == "late"


async def test_backfill_of_an_empty_root_still_returns_a_concurrently_committed_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)
    original = store._scan_session_metas_sync

    def empty_scan_then_concurrent_save() -> list:
        listed = original()
        assert listed == []
        store._save_session_sync("late", {"messages": [Message("user", ["x"])], "compressed_msgs": []})
        return listed

    monkeypatch.setattr(store, "_scan_session_metas_sync", empty_scan_then_concurrent_save)
    assert await store.load_latest_session_id() == "late"


@pytest.mark.parametrize(
    "stamps",
    [
        pytest.param(("0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59"), id="offset-overflows-utc"),
        pytest.param(("0001-01-01T00:00:00+00:00", "9999-12-31T23:59:59.999999+00:00"), id="datetime-min-max"),
    ],
)
async def test_lookup_tolerates_stamps_at_the_datetime_bounds(tmp_path: Path, stamps: tuple[str, ...]) -> None:
    """Stamps at the datetime bounds — whether their UTC conversion under/overflows or they
    sit at ``datetime.min``/``max`` outright — must not raise out of ``/resume``, neither
    from the backfill nor from the sweep's cutoff arithmetic."""
    store = JsonFileStateStore(tmp_path)
    await _save(store, "only", "a")
    for stamp in stamps:
        for path in (store._session_file("only"), store._backup_file("only")):
            data = json.loads(path.read_text(encoding="utf-8"))
            data["meta"]["updated_at"] = stamp
            path.write_text(json.dumps(data), encoding="utf-8")
        assert [m.session_id for m in await store.list_sessions()] == ["only"]
        store._mru.invalidate()
        assert await store.load_latest_session_id() == "only"  # backfill
        assert await store.load_latest_session_id() == "only"  # index path + sweep


async def test_backfill_does_not_resurrect_a_session_deleted_after_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)
    await _save(store, "older", "a")
    await _save(store, "newer", "a")
    store._mru.invalidate()
    original = store._scan_session_metas_sync

    def scan_then_concurrent_delete() -> list:
        listed = original()
        assert {m.session_id for m in listed} == {"older", "newer"}
        store._delete_session_sync("newer")
        return listed

    monkeypatch.setattr(store, "_scan_session_metas_sync", scan_then_concurrent_delete)
    assert await store.load_latest_session_id() == "older"
    assert "newer" not in _mru_ids(tmp_path)  # the stale scan input was rebuilt in, then pruned as a ghost


async def test_backfill_of_an_empty_root_sees_a_first_save_that_commits_after_failing_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)
    writer = FileLock(store._write_lock_path("first"))
    writer.acquire()  # in-flight first save: recorded, not yet committed
    store._mru.record("first", datetime.now(UTC))
    original_verify = store._mru_verify
    committed = False

    def verify_then_commit(session_id: str) -> datetime | None:
        nonlocal committed
        actual = original_verify(session_id)  # absent, and not prunable: the writer holds the lock
        if session_id == "first" and not committed:
            committed = True
            writer.release()
            store._save_session_sync("first", {"messages": [Message("user", ["x"])], "compressed_msgs": []})
        return actual

    monkeypatch.setattr(store, "_mru_verify", verify_then_commit)
    try:
        assert await store.load_latest_session_id() == "first"
    finally:
        if not committed:
            writer.release()
    assert committed


async def test_lookup_verifies_a_legacy_session_under_a_non_canonical_filename(tmp_path: Path) -> None:
    """The listing attributes ``*.json`` by embedded id; verification must not prune such a session."""
    store = JsonFileStateStore(tmp_path)
    stamp = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    path = tmp_path / "arbitrary-name.json"
    path.write_text(
        json.dumps({"meta": {"session_id": "embedded-id", "created_at": stamp, "updated_at": stamp}, "state": {}}),
        encoding="utf-8",
    )
    aged = (datetime.now(UTC) - timedelta(hours=1)).timestamp()
    os.utime(path, (aged, aged))  # outside the sweep's mtime window
    assert [m.session_id for m in await store.list_sessions()] == ["embedded-id"]
    assert await store.load_latest_session_id() == "embedded-id"  # backfill
    assert await store.load_latest_session_id() == "embedded-id"  # index path: verify, don't prune
    assert _mru_ids(tmp_path) == ["embedded-id"]
    # A corrupt canonical copy must not mask it either (the listing skips unreadable files).
    store._legacy_path("embedded-id").write_text("{not json", encoding="utf-8")
    assert [m.session_id for m in await store.list_sessions()] == ["embedded-id"]
    assert await store.load_latest_session_id() == "embedded-id"
    assert await store.load_latest_session_id() == "embedded-id"


async def test_ghost_prune_lock_errors_do_not_fail_the_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = JsonFileStateStore(tmp_path)
    await _save(store, "older", "a")
    await _save(store, "newer", "a")
    assert await store.load_latest_session_id() == "newer"
    shutil.rmtree(store.session_dir("newer"))  # ghost entry stays in the index
    ghost_lock = store._write_lock_path("newer")

    class DeniedLock(FileLock):
        def acquire(self) -> None:
            if self._path == ghost_lock:
                raise PermissionError("simulated denied per-session lock")
            super().acquire()

    monkeypatch.setattr(store_module, "FileLock", DeniedLock)
    assert await store.load_latest_session_id() == "older"
    assert "newer" in _mru_ids(tmp_path)  # left for a later prune


async def test_pre_commit_crash_folder_is_pruned_but_a_hidden_sidecar_is_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded session whose folder holds nothing restorable is a ghost; one hidden by an active lock is not."""
    store = JsonFileStateStore(tmp_path)
    await _save(store, "a", "a")
    assert await store.load_latest_session_id() == "a"
    # Save crashed after mkdir + record, before the envelope commit.
    store._mru.record("ghost", datetime.now(UTC) + timedelta(seconds=1))
    store.session_dir("ghost").mkdir()
    # Recovery-only session whose owner is alive: hidden, but restorable later.
    owner = FileLock(store.active_lock_path("live"))
    owner.acquire()
    try:
        await asyncio.to_thread(
            store.save_recovery_session, "live", {"messages": [Message("user", ["x"])], "compressed_msgs": []}
        )
        scans = _count_scans(monkeypatch, store)
        assert await store.load_latest_session_id() == "a"
        assert scans[0] == 0
        assert "ghost" not in _mru_ids(tmp_path)  # pruned for good
        assert "live" in _mru_ids(tmp_path)  # merely skipped this time
        assert await store.load_latest_session_id() == "a"
        assert scans[0] == 0
    finally:
        owner.release()
    assert await store.load_latest_session_id() == "live"


def _shift_updated_at(store: JsonFileStateStore, session_id: str, delta: timedelta) -> None:
    path = store._session_file(session_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    stamp = datetime.fromisoformat(data["meta"]["updated_at"]) + delta
    data["meta"]["updated_at"] = stamp.isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")


async def test_latest_session_rescans_when_downgraded_winner_falls_below_horizon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-ranking only the indexed entries is unsafe once they drop below the trim horizon."""
    store = JsonFileStateStore(tmp_path)
    store._mru = SessionMruIndex(tmp_path, max_entries=2)
    for name in ("s0", "s1", "s2"):
        await _save(store, name, "a")
    assert await store.load_latest_session_id() == "s2"
    snapshot = store._mru.load()
    assert snapshot is not None
    assert [e.session_id for e in snapshot.sessions] == ["s2", "s1"]
    s0_meta = await store.load_session_meta("s0")
    assert s0_meta is not None and snapshot.horizon == coerce_utc(s0_meta.updated_at)
    list_sync = store._list_sessions_sync
    scans = _count_scans(monkeypatch, store)

    # s2 rolled back below s0, but s1 still tops the horizon: in-memory
    # re-rank, no scan.
    _shift_updated_at(store, "s2", -timedelta(days=1))
    assert await store.load_latest_session_id() == "s1"
    assert scans[0] == 0

    # Every indexed session now sits below the unindexed s0: only a full
    # scan can find it, and it must agree with list_sessions().
    _shift_updated_at(store, "s1", -timedelta(days=2))
    metas = await asyncio.to_thread(list_sync)
    assert max(metas, key=lambda m: m.updated_at).session_id == "s0"
    assert await store.load_latest_session_id() == "s0"
    assert scans[0] == 1
    # The index never writes a downgrade, so the stale entries survive the
    # rebuild and each lookup keeps rescanning — correct, just slow — until
    # a save re-records one of them above the horizon.
    assert _mru_ids(tmp_path) == ["s2", "s1"]
    assert await store.load_latest_session_id() == "s0"
    assert scans[0] == 2
    await _save(store, "s1", "a", "b")
    assert await store.load_latest_session_id() == "s1"
    assert scans[0] == 2


async def test_latest_session_skips_ghost_top_entry_and_repairs_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)
    await _save(store, "s1", "a")
    await _save(store, "s2", "a")
    assert await store.load_latest_session_id() == "s2"
    scans = _count_scans(monkeypatch, store)

    shutil.rmtree(tmp_path / "s2")  # deleted out of band
    assert await store.load_latest_session_id() == "s1"
    assert scans[0] == 0
    assert _mru_ids(tmp_path) == ["s1"]


async def test_latest_session_reranks_when_index_timestamp_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)
    await _save(store, "s1", "a")
    await _save(store, "s2", "a")
    assert await store.load_latest_session_id() == "s2"
    scans = _count_scans(monkeypatch, store)

    # Index claims s1 is far in the future, but disk says otherwise: the
    # verified (older) value re-ranks in memory only.
    SessionMruIndex(tmp_path).record("s1", datetime(2999, 1, 1, tzinfo=UTC))
    assert _mru_ids(tmp_path) == ["s1", "s2"]
    assert await store.load_latest_session_id() == "s2"
    assert scans[0] == 0
    assert _mru_ids(tmp_path) == ["s1", "s2"]

    # Index lagging behind disk (older stamp than the envelope) is written
    # back once verified.
    await _save(store, "s1", "a", "b")  # promotes s1 on disk (and in the index)
    s1_meta = await store.load_session_meta("s1")
    s2_meta = await store.load_session_meta("s2")
    assert s1_meta is not None and s2_meta is not None
    index = SessionMruIndex(tmp_path)
    index.remove("s1")
    index.record("s1", s2_meta.updated_at + timedelta(microseconds=1))
    assert _mru_ids(tmp_path) == ["s1", "s2"]
    assert await store.load_latest_session_id() == "s1"
    assert scans[0] == 0
    repaired = index.load()
    assert repaired is not None
    assert repaired.sessions[0] == SessionMruEntry("s1", coerce_utc(s1_meta.updated_at))


async def test_latest_session_rescans_when_complete_index_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)
    for i in range(3):
        await _save(store, f"s{i}", "a")
    assert await store.load_latest_session_id() == "s2"
    index = SessionMruIndex(tmp_path)
    for i in range(3):
        index.remove(f"s{i}")
    assert _mru_ids(tmp_path) == []
    scans = _count_scans(monkeypatch, store)

    assert await store.load_latest_session_id() == "s2"
    assert scans[0] == 1
    assert _mru_ids(tmp_path) == ["s2", "s1", "s0"]


async def test_index_file_is_not_listed_as_legacy_session(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await _save(store, "s1", "a")
    assert (tmp_path / SESSION_MRU_FILE_NAME).exists()
    metas = await store.list_sessions()
    assert [m.session_id for m in metas] == ["s1"]
    listing = await store.open_session_listing(kind="chat")
    assert [entry.session_id for entry in listing.entries] == ["s1"]


async def test_mru_failure_never_fails_session_operations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = JsonFileStateStore(tmp_path)
    parent = str(uuid4())
    await _save(store, parent, "a")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("index disk full")

    monkeypatch.setattr(store._mru, "record", boom)
    monkeypatch.setattr(store._mru, "remove", boom)

    await _save(store, parent, "a", "b")
    await asyncio.to_thread(
        store.save_recovery_session,
        parent,
        {"messages": [Message("user", ["x"])], "compressed_msgs": []},
    )
    fork_id = await asyncio.to_thread(store.fork_session, parent)
    await store.delete_session(fork_id)
    assert not (tmp_path / SESSION_MRU_FILE_NAME).exists()  # invalidated
    assert await store.load_session(parent) is not None
    assert not store.session_dir(fork_id).exists()


async def test_latest_session_matches_list_sessions_under_active_lock(tmp_path: Path) -> None:
    """A live owner keeps the primary authoritative; the index must agree."""
    store = JsonFileStateStore(tmp_path)
    await _save(store, "held", "a")
    await _save(store, "free", "a")
    await asyncio.to_thread(
        store.save_recovery_session,
        "held",
        {"messages": [Message("user", ["in-flight"])], "compressed_msgs": []},
    )
    assert _mru_ids(tmp_path)[0] == "held"

    assert await store.load_latest_session_id() == "held"  # backfilled, complete

    lock = FileLock(store.active_lock_path("held"), timeout=1.0)
    lock.acquire()
    try:
        listed = sorted(await store.list_sessions(), key=lambda m: m.updated_at, reverse=True)
        assert listed[0].session_id == "free"
        assert await store.load_latest_session_id() == "free"
    finally:
        lock.release()
    # The owner "crashed": the sidecar wins again and the index still knows
    # it (the lock-time downgrade was never persisted).
    assert _mru_ids(tmp_path)[0] == "held"
    assert await store.load_latest_session_id() == "held"
