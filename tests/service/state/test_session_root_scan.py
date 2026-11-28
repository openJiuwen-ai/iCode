# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The ``scandir`` listing of the sessions root returns exactly what the pathlib listing it replaced did."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
from collections.abc import Generator
from pathlib import Path

import pytest

import chrys.service.state._session_files as session_files
import chrys.service.state.store as store_module
from chrys.service.state._session_files import (
    SESSION_BACKUP_FILE_NAME,
    SESSION_FILE_NAME,
    SESSION_RECOVERY_FILE_NAME,
    legacy_session_files,
    parse_snapshot_turn,
    session_dir_candidates,
)
from chrys.service.state.session_mru import SESSION_MRU_FILE_NAME
from chrys.service.state.store import JsonFileStateStore
from chrys.service.trajectory.tombstone import INTENT_SUFFIX, tombstones_dir
from tests.support.symlinks import junction_or_skip, symlink_or_skip


def _pathlib_candidates(root: Path) -> list[Path]:
    """The folder listing before the scan: ``iterdir`` + ``is_dir`` + an artifact probe, sorted as paths."""

    def has_artifacts(folder: Path) -> bool:
        return (
            (folder / SESSION_FILE_NAME).exists()
            or (folder / SESSION_BACKUP_FILE_NAME).exists()
            or (folder / SESSION_RECOVERY_FILE_NAME).exists()
            or any(parse_snapshot_turn(snap) >= 1 for snap in (folder / "snapshots").glob("*.json"))
        )

    return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".") and has_artifacts(p))


def _pathlib_legacy(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("*.json") if p.name != SESSION_MRU_FILE_NAME)


def _assert_matches_pathlib(root: Path) -> list[Path]:
    candidates = session_dir_candidates(root)
    assert candidates == _pathlib_candidates(root)
    assert legacy_session_files(root) == _pathlib_legacy(root)
    return candidates


def _linked(candidates: list[Path]) -> list[str]:
    return [path.name for path in candidates if path.is_symlink() or path.is_junction()]


def _write(path: Path, text: str = "{}") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _plant_root(root: Path) -> None:
    _write(root / "aaaaaaaaaaaa" / "session.json")
    _write(root / "Bbbbbbbbbbbb" / "session.json")  # sorts by case where paths do
    _write(root / "cccccccccccc" / "session.json.bak")
    _write(root / "dddddddddddd" / "session.recovery.json")
    _write(root / "eeeeeeeeeeee" / "snapshots" / "turn_1.json")
    _write(root / "ffffffffffff" / "snapshots" / "draft.json")  # no turn: nothing to restore
    (root / "000000000000").mkdir()
    _write(root / ".partial" / "session.json")
    _write(root / ".locks" / "x.write.lock", "")
    _write(root / "notes.txt", "")
    _write(root / "111111111111.json")
    _write(root / ".hidden.json")
    _write(root / "Upper.JSON")  # legacy only where names compare case-insensitively
    _write(root / "session_mru.json")
    _write(root / "trace.jsonl")
    _write(root / "folder.json" / "session.json")  # both a folder session and a ``*.json`` match


def test_scan_lists_what_pathlib_listed(tmp_path: Path) -> None:
    _plant_root(tmp_path)

    _assert_matches_pathlib(tmp_path)


def test_entries_are_checked_in_listing_order_and_only_candidates_sorted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As ``iterdir`` did: a later entry is checked later, whatever its name, so it is seen as it is by then."""
    for name in ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"):
        _write(tmp_path / name / "session.json")
    scandir, has_artifacts = os.scandir, session_files.session_dir_has_artifacts
    checked: list[str] = []

    @contextlib.contextmanager
    def reversed_scandir(path: str) -> Generator[list[os.DirEntry[str]]]:  # the root lists in reverse name order
        with scandir(path) as entries:
            yield sorted(entries, key=lambda entry: entry.name, reverse=True)

    def check(folder: str | os.PathLike[str]) -> bool:
        checked.append(os.path.basename(folder))
        if checked == ["cccccccccccc"]:
            (tmp_path / "aaaaaaaaaaaa" / "session.json").unlink()  # gone before the listing reaches it
        return has_artifacts(folder)

    monkeypatch.setattr(session_files.os, "scandir", reversed_scandir)
    monkeypatch.setattr(session_files, "session_dir_has_artifacts", check)

    assert [path.name for path in session_dir_candidates(tmp_path)] == ["bbbbbbbbbbbb", "cccccccccccc"]
    assert checked == ["cccccccccccc", "bbbbbbbbbbbb", "aaaaaaaaaaaa"]


def test_missing_root_lists_no_flat_files_but_raises_for_folders(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    assert legacy_session_files(missing) == []  # as ``glob`` swallowed it
    with pytest.raises(FileNotFoundError):
        session_dir_candidates(missing)  # as ``iterdir`` raised it


def test_scan_follows_symlinks_as_pathlib_did(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    _plant_root(root)
    _write(outside / "archive" / "session.json")
    _write(outside / "backup-only" / "session.json.bak")
    _write(outside / "flat.json")
    symlink_or_skip(root / "222222222222", outside / "archive", target_is_directory=True)
    symlink_or_skip(root / "333333333333", outside / "backup-only", target_is_directory=True)
    symlink_or_skip(root / "444444444444", outside / "missing", target_is_directory=True)
    symlink_or_skip(root / "555555555555", outside / "flat.json")
    symlink_or_skip(root / "666666666666.json", outside / "flat.json")

    assert _linked(_assert_matches_pathlib(root)) == ["222222222222", "333333333333"]


def test_scan_follows_junctions_as_pathlib_did(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    _plant_root(root)
    _write(outside / "archive" / "session.json")
    (outside / "gone").mkdir()
    junction_or_skip(root / "222222222222", outside / "archive")
    junction_or_skip(root / "333333333333", outside / "gone")
    (outside / "gone").rmdir()  # a dangling junction lists as a folder but holds nothing

    assert _linked(_assert_matches_pathlib(root)) == ["222222222222"]


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX permission bits bind non-root users")
def test_unreadable_folder_is_skipped_as_pathlib_did(tmp_path: Path) -> None:
    _plant_root(tmp_path)
    locked = tmp_path / "777777777777"
    _write(locked / "session.json")
    locked.chmod(0)
    try:
        assert locked not in _assert_matches_pathlib(tmp_path)
    finally:
        locked.chmod(0o700)


async def test_delete_intent_in_another_case_hides_the_session_where_names_fold(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    identity = "abcdef12-3456-4111-8222-123456789abc"
    await store.save_session(identity, {"messages": []})
    folder = store.session_dir(identity)
    other_case = folder.with_name(folder.name.upper())
    graveyard = tombstones_dir(tmp_path)
    graveyard.mkdir()
    (graveyard / f"{other_case.name}{INTENT_SUFFIX}").write_text(other_case.name, encoding="utf-8")

    listed = [entry.session_id for entry in (await store.open_session_listing(kind="chat")).entries]
    # On a case-insensitive volume the intent names this very folder; elsewhere it names another.
    assert listed == ([] if other_case.exists() else [identity])
    assert [meta.session_id for meta in await store.list_sessions()] == listed


@pytest.mark.parametrize("change", ["retitle", "remove"])
async def test_listing_reads_each_folder_as_it_reaches_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    """A session saved or removed after the root was listed shows as it is when its row is read."""
    store = JsonFileStateStore(tmp_path)
    identity = "abcdef12-3456-4111-8222-123456789abc"
    await store.save_session(identity, {"messages": []})
    assert [entry.meta.display_title for entry in (await store.open_session_listing(kind="chat")).entries] == [""]
    candidates = store._session_dir_candidates

    def candidates_then_change() -> list[Path]:
        found = candidates()
        if change == "retitle":
            store._update_session_titles_sync(identity, custom_title="Renamed")
        else:
            shutil.rmtree(store.session_dir(identity))
        return found

    monkeypatch.setattr(store, "_session_dir_candidates", candidates_then_change)
    entries = (await store.open_session_listing(kind="chat")).entries

    if change == "retitle":
        assert [entry.meta.display_title for entry in entries] == ["Renamed"]
    else:
        assert entries == ()
        catalog = tmp_path / ".cache" / "session_catalog.json"
        assert store.session_dir(identity).name not in json.loads(catalog.read_text(encoding="utf-8"))["sessions"]


async def test_listing_reads_delete_intents_and_flat_files_after_probing_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete recorded once the folders were probed, or a legacy file copied in once rows are read, counts."""
    store = JsonFileStateStore(tmp_path)
    deleted, kept, imported = (f"{prefix}bcdef12-3456-4111-8222-123456789abc" for prefix in "abc")
    for identity in (deleted, kept):
        await store.save_session(identity, {"messages": []})
    stamp = "2026-01-01T00:00:00+00:00"
    envelope = {
        "meta": {"session_id": imported, "created_at": stamp, "updated_at": stamp, "message_count": 0},
        "state": {"messages": [], "compressed_msgs": []},
    }
    probe = store_module.session_dir_candidates

    def probe_then_change(root: Path) -> list[Path]:
        found = probe(root)
        graveyard = tombstones_dir(tmp_path)
        graveyard.mkdir(exist_ok=True)
        name = store.session_dir(deleted).name
        (graveyard / f"{name}{INTENT_SUFFIX}").write_text(name, encoding="utf-8")
        return found

    read_row = store._listing_meta

    def import_then_read_row(session_dir: Path) -> object:
        store._legacy_path(imported).write_text(json.dumps(envelope), encoding="utf-8")
        return read_row(session_dir)

    monkeypatch.setattr(store_module, "session_dir_candidates", probe_then_change)
    monkeypatch.setattr(store, "_listing_meta", import_then_read_row)
    listed = [entry.session_id for entry in (await store.open_session_listing(kind="chat")).entries]

    assert sorted(listed) == [kept, imported]
