# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the session-migration service (copy sessions between two roots)."""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import chrys.service.state.session_migration as migration_module
from chrys.foundation.platform.files import secure_open_owner_only_binary
from chrys.foundation.util.lock import FileLock
from chrys.service.state.session_migration import (
    MigrationItem,
    SessionMigrationError,
    plan_session_migration,
    run_session_migration,
)
from chrys.service.state.session_mru import SESSION_MRU_FILE_NAME
from chrys.service.state.store import (
    SESSION_FILE_NAME,
    session_active_lock_path,
    session_dir_candidates,
    session_write_lock_path,
)
from tests.support.secure_files import plant_owner_only_bytes


def _make_session(root: Path, short_id: str, *, text: str = "hello") -> Path:
    session_dir = root / short_id
    session_dir.mkdir(parents=True)
    (session_dir / SESSION_FILE_NAME).write_text(json.dumps({"meta": {"session_id": short_id}, "text": text}))
    snapshots = session_dir / "snapshots"
    snapshots.mkdir()
    (snapshots / "turn_1.json").write_text("{}")
    return session_dir


def _snapshot_tree(root: Path) -> dict[str, bytes | None]:
    """Relative path -> file bytes (None for directories) for the whole tree."""
    tree: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        tree[rel] = None if path.is_dir() else path.read_bytes()
    return tree


def _symlink_or_skip(link: Path, target: Path, *, directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")


def _partials(root: Path) -> list[Path]:
    return [p for p in root.iterdir() if ".partial-" in p.name]


def test_plan_and_run_copy_sessions_into_fresh_destination(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    first_session = _make_session(src, "aaaaaaaaaaaa")
    _make_session(src, "bbbbbbbbbbbb", text="second")
    source_image = first_session / "doc_converter" / "image-copied.png"
    source_image.parent.mkdir()
    plant_owner_only_bytes(source_image, b"copied-image")
    (src / ".locks").mkdir()
    (src / ".locks" / "stray.write.lock").write_text("")
    (src / SESSION_MRU_FILE_NAME).write_text("{}")
    before = _snapshot_tree(src)

    plan = plan_session_migration(src, dst)
    assert [item.session_id for item in plan.items] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
    assert all(not item.already_present and not item.legacy_file for item in plan.items)
    assert plan.rejected == ()
    # Planning creates nothing.
    assert not dst.exists()

    with patch.object(
        migration_module,
        "reharden_document_image_artifacts",
        wraps=migration_module.reharden_document_image_artifacts,
    ) as reharden:
        report = run_session_migration(plan)

    assert report.copied == ("aaaaaaaaaaaa", "bbbbbbbbbbbb")
    assert report.skipped_present == report.skipped_active == report.skipped_busy == ()
    assert report.failed == ()
    assert reharden.call_count == 2
    assert (dst / ".locks").is_dir()
    for short_id in ("aaaaaaaaaaaa", "bbbbbbbbbbbb"):
        assert _snapshot_tree(dst / short_id) == _snapshot_tree(src / short_id)
    with secure_open_owner_only_binary(dst / "aaaaaaaaaaaa" / "doc_converter" / "image-copied.png") as copied:
        assert copied.read() == b"copied-image"
    # Root-level lock files and the MRU index are never carried over.
    assert not (dst / ".locks" / "stray.write.lock").exists()
    assert not (dst / SESSION_MRU_FILE_NAME).exists()
    assert _partials(dst) == []
    # Source is untouched apart from the lock files the copy took under .locks/.
    after = _snapshot_tree(src)
    assert {k: v for k, v in after.items() if not k.startswith(".locks/")} == {
        k: v for k, v in before.items() if not k.startswith(".locks/")
    }
    assert {k for k in after if k.startswith(".locks/")} == {
        ".locks/stray.write.lock",
        ".locks/aaaaaaaaaaaa.active.lock",
        ".locks/aaaaaaaaaaaa.write.lock",
        ".locks/bbbbbbbbbbbb.active.lock",
        ".locks/bbbbbbbbbbbb.write.lock",
    }
    assert session_dir_candidates(dst) == [dst / "aaaaaaaaaaaa", dst / "bbbbbbbbbbbb"]


def test_run_skips_already_present_destination_without_overwriting(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_session(src, "aaaaaaaaaaaa", text="from-source")
    _make_session(dst, "aaaaaaaaaaaa", text="already-there")
    _make_session(src, "bbbbbbbbbbbb")

    plan = plan_session_migration(src, dst)
    assert {item.session_id: item.already_present for item in plan.items} == {
        "aaaaaaaaaaaa": True,
        "bbbbbbbbbbbb": False,
    }

    report = run_session_migration(plan)

    assert report.skipped_present == ("aaaaaaaaaaaa",)
    assert report.copied == ("bbbbbbbbbbbb",)
    assert "already-there" in (dst / "aaaaaaaaaaaa" / SESSION_FILE_NAME).read_text()


def test_run_treats_destination_appearing_after_plan_as_present(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_session(src, "aaaaaaaaaaaa", text="from-source")
    plan = plan_session_migration(src, dst)
    assert plan.items[0].already_present is False

    _make_session(dst, "aaaaaaaaaaaa", text="raced-in")
    report = run_session_migration(plan)

    assert report.skipped_present == ("aaaaaaaaaaaa",)
    assert report.copied == ()
    assert "raced-in" in (dst / "aaaaaaaaaaaa" / SESSION_FILE_NAME).read_text()


def test_run_rechecks_the_destination_after_taking_its_write_lock(tmp_path: Path) -> None:
    """A second migration that lands the same session while this one waits for
    the destination lock must be left alone — even a legacy flat file, which
    ``os.replace`` would otherwise silently overwrite."""
    import threading

    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "legacylegacy.json").write_text(json.dumps({"meta": {"session_id": "legacylegacy"}, "text": "mine"}))
    plan = plan_session_migration(src, dst)
    assert [item.legacy_file for item in plan.items] == [True]
    (dst / ".locks").mkdir(parents=True)
    holder = FileLock(session_write_lock_path(dst, "legacylegacy"), timeout=1.0)
    holder.acquire()

    def land_the_other_copy_then_release() -> None:
        (dst / "legacylegacy.json").write_text(json.dumps({"meta": {"session_id": "legacylegacy"}, "text": "theirs"}))
        holder.release()

    timer = threading.Timer(0.2, land_the_other_copy_then_release)
    timer.start()
    try:
        report = run_session_migration(plan)
    finally:
        timer.join()

    assert report.skipped_present == ("legacylegacy",)
    assert report.copied == ()
    assert "theirs" in (dst / "legacylegacy.json").read_text()


def test_run_skips_session_whose_active_lock_is_held(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_session(src, "aaaaaaaaaaaa")
    _make_session(src, "bbbbbbbbbbbb")
    (src / ".locks").mkdir()
    holder = FileLock(session_active_lock_path(src, "aaaaaaaaaaaa"), timeout=1.0)
    holder.acquire()
    try:
        report = run_session_migration(plan_session_migration(src, dst))
    finally:
        holder.release()

    assert report.skipped_active == ("aaaaaaaaaaaa",)
    assert report.copied == ("bbbbbbbbbbbb",)
    assert not (dst / "aaaaaaaaaaaa").exists()
    assert (dst / "bbbbbbbbbbbb" / SESSION_FILE_NAME).exists()


def test_run_holds_source_active_lock_while_copying(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_session(src, "aaaaaaaaaaaa")
    real_copytree = shutil.copytree
    contended: list[str] = []

    # copytree recurses through the module attribute, so nested calls land
    # here too (positionally); only probe the session-root call.
    def probing_copytree(source: object, *args: object, **kwargs: object) -> object:
        source_path = Path(str(source))
        if source_path.parent == src:
            probe = FileLock(session_active_lock_path(src, source_path.name), timeout=0)
            try:
                probe.acquire()
            except TimeoutError:
                contended.append(source_path.name)
            else:
                probe.release()
        return real_copytree(source, *args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", probing_copytree)
    report = run_session_migration(plan_session_migration(src, dst))

    assert report.copied == ("aaaaaaaaaaaa",)
    assert contended == ["aaaaaaaaaaaa"]
    # Released afterwards: the lock is free again.
    with FileLock(session_active_lock_path(src, "aaaaaaaaaaaa"), timeout=0):
        pass


@pytest.mark.parametrize("busy_root", ["source", "destination"])
def test_run_skips_session_whose_write_lock_is_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, busy_root: str
) -> None:
    monkeypatch.setattr(migration_module, "MIGRATION_WRITE_LOCK_TIMEOUT_SECONDS", 0.05)
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_session(src, "aaaaaaaaaaaa")
    _make_session(src, "bbbbbbbbbbbb")
    lock_root = src if busy_root == "source" else dst
    (lock_root / ".locks").mkdir(parents=True)
    holder = FileLock(session_write_lock_path(lock_root, "aaaaaaaaaaaa"), timeout=1.0)
    holder.acquire()
    try:
        report = run_session_migration(plan_session_migration(src, dst))
    finally:
        holder.release()

    assert report.skipped_busy == ("aaaaaaaaaaaa",)
    assert report.copied == ("bbbbbbbbbbbb",)
    assert not (dst / "aaaaaaaaaaaa").exists()


def test_failed_session_does_not_stop_others_and_leaves_no_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    for short_id in ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"):
        _make_session(src, short_id)
    real_copytree = shutil.copytree

    def failing_copytree(source: object, destination: object, *args: object, **kwargs: object) -> object:
        if Path(str(source)) == src / "bbbbbbbbbbbb":
            # Leave a half-written partial behind so cleanup has work to do.
            Path(str(destination)).mkdir(exist_ok=True)
            (Path(str(destination)) / SESSION_FILE_NAME).write_text("{}")
            raise OSError("boom")
        return real_copytree(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", failing_copytree)
    report = run_session_migration(plan_session_migration(src, dst))

    assert report.copied == ("aaaaaaaaaaaa", "cccccccccccc")
    assert report.failed == ((src / "bbbbbbbbbbbb", "boom"),)
    assert not (dst / "bbbbbbbbbbbb").exists()
    assert _partials(dst) == []
    # Locks are released again after the failure.
    with FileLock(session_active_lock_path(src, "bbbbbbbbbbbb"), timeout=0):
        pass
    with FileLock(session_write_lock_path(dst, "bbbbbbbbbbbb"), timeout=0):
        pass


def test_unlockable_session_is_reported_and_does_not_stop_the_others(tmp_path: Path) -> None:
    """A lock path that cannot be opened fails that session only."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    for short_id in ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"):
        _make_session(src, short_id)
    # A directory where the source write lock file should be: ``os.open``
    # refuses it with an ``OSError`` that is not a timeout.
    session_write_lock_path(src, "bbbbbbbbbbbb").mkdir(parents=True)

    report = run_session_migration(plan_session_migration(src, dst))

    assert report.copied == ("aaaaaaaaaaaa", "cccccccccccc")
    assert [path for path, _reason in report.failed] == [src / "bbbbbbbbbbbb"]
    assert not (dst / "bbbbbbbbbbbb").exists()
    assert _partials(dst) == []
    # The active lock taken before the failure was released again.
    with FileLock(session_active_lock_path(src, "bbbbbbbbbbbb"), timeout=0):
        pass


def test_session_dir_copy_leaves_a_pre_existing_partial_dir_alone(tmp_path: Path) -> None:
    """The partial is created exclusively; a folder planted at the old predictable name is not removed."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_session(src, "aaaaaaaaaaaa")
    dst.mkdir()
    planted = dst / f".partial-aaaaaaaaaaaa-{os.getpid()}"
    planted.mkdir()
    (planted / "keep.txt").write_text("mine")

    report = run_session_migration(plan_session_migration(src, dst))

    assert report.copied == ("aaaaaaaaaaaa",)
    assert (dst / "aaaaaaaaaaaa" / SESSION_FILE_NAME).read_bytes() == (
        src / "aaaaaaaaaaaa" / SESSION_FILE_NAME
    ).read_bytes()
    assert (planted / "keep.txt").read_text() == "mine"
    assert [p.name for p in _partials(dst)] == [planted.name]


def test_a_session_dir_swapped_for_a_link_after_planning_is_not_followed(tmp_path: Path) -> None:
    """The plan saw a real folder; a link put in its place before the copy is refused."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_session(src, "aaaaaaaaaaaa")
    _make_session(src, "bbbbbbbbbbbb")
    victim = tmp_path / "victim"
    victim.mkdir()
    outside_locks = victim.parent / ".locks"
    outside_locks.mkdir()
    (victim / SESSION_FILE_NAME).write_text(json.dumps({"secret": True}))
    plan = plan_session_migration(src, dst)
    assert [item.session_id for item in plan.items] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]

    shutil.rmtree(src / "aaaaaaaaaaaa")
    _symlink_or_skip(src / "aaaaaaaaaaaa", victim, directory=True)
    report = run_session_migration(plan)

    assert report.copied == ("bbbbbbbbbbbb",)
    assert report.failed == ((src / "aaaaaaaaaaaa", "linked entry"),)
    assert not (dst / "aaaaaaaaaaaa").exists()
    assert not list(outside_locks.iterdir())
    assert _partials(dst) == []


def test_a_session_dir_swapped_for_a_junction_after_planning_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``lstat`` sees a junction as a plain directory; the run-time re-check asks by name too."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    session_dir = _make_session(src, "aaaaaaaaaaaa")
    _make_session(src, "bbbbbbbbbbbb")
    plan = plan_session_migration(src, dst)
    assert [item.session_id for item in plan.items] == ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]

    # "Replace" the planned folder with a junction: same real directory on
    # disk (junctions cannot be created on POSIX), reported as one from now on.
    planted = os.path.normcase(os.path.abspath(session_dir))
    real_isjunction = os.path.isjunction

    def fake_isjunction(candidate: object) -> bool:
        return os.path.normcase(os.path.abspath(candidate)) == planted or real_isjunction(candidate)  # type: ignore[arg-type]

    monkeypatch.setattr(os.path, "isjunction", fake_isjunction)
    report = run_session_migration(plan)

    assert report.copied == ("bbbbbbbbbbbb",)
    assert report.failed == ((src / "aaaaaaaaaaaa", "linked entry"),)
    assert not (dst / "aaaaaaaaaaaa").exists()


def test_a_legacy_file_swapped_for_a_link_after_planning_is_not_followed(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    (src / "legacylegacy.json").write_text(json.dumps({"meta": {"session_id": "legacylegacy"}}))
    victim = tmp_path / "victim.json"
    victim.write_text(json.dumps({"secret": True}))
    plan = plan_session_migration(src, dst)
    assert [item.session_id for item in plan.items] == ["legacylegacy"]

    (src / "legacylegacy.json").unlink()
    _symlink_or_skip(src / "legacylegacy.json", victim, directory=False)
    report = run_session_migration(plan)

    assert report.copied == ()
    assert report.failed == ((src / "legacylegacy.json", "linked entry"),)
    assert not (dst / "legacylegacy.json").exists()
    assert _partials(dst) == []


def test_a_legacy_file_open_refuses_a_link_even_without_the_lstat_check(tmp_path: Path) -> None:
    """The descriptor-level guard: opening the leaf never follows a link."""
    victim = tmp_path / "victim.json"
    victim.write_text("{}")
    link = tmp_path / "link.json"
    _symlink_or_skip(link, victim, directory=False)
    with pytest.raises(OSError):
        migration_module._open_source_file(link)


def test_leftover_partial_dirs_are_not_sessions(tmp_path: Path) -> None:
    root = tmp_path / "root"
    _make_session(root, "aaaaaaaaaaaa")
    leftover = root / ".partial-bbbbbbbbbbbb-4242"
    leftover.mkdir()
    (leftover / SESSION_FILE_NAME).write_text("{}")

    assert session_dir_candidates(root) == [root / "aaaaaaaaaaaa"]
    plan = plan_session_migration(root, tmp_path / "dst")
    assert [item.session_id for item in plan.items] == ["aaaaaaaaaaaa"]


def test_legacy_flat_file_is_copied(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    plant_owner_only_bytes(src / "legacylegacy.json", json.dumps({"meta": {"session_id": "legacylegacy"}}).encode())
    (src / SESSION_MRU_FILE_NAME).write_text("{}")

    plan = plan_session_migration(src, dst)
    assert plan.items == (
        MigrationItem(
            session_id="legacylegacy",
            source=src / "legacylegacy.json",
            destination=dst / "legacylegacy.json",
            legacy_file=True,
            already_present=False,
        ),
    )

    report = run_session_migration(plan)

    assert report.copied == ("legacylegacy",)
    assert (dst / "legacylegacy.json").read_bytes() == (src / "legacylegacy.json").read_bytes()
    assert not (dst / SESSION_MRU_FILE_NAME).exists()
    assert _partials(dst) == []


def test_legacy_file_copy_never_follows_a_planted_partial_symlink(tmp_path: Path) -> None:
    """A predictable partial name planted as a symlink must not redirect the copy."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    payload = json.dumps({"meta": {"session_id": "legacylegacy"}})
    plant_owner_only_bytes(src / "legacylegacy.json", payload.encode())
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched")
    _symlink_or_skip(dst / f"legacylegacy.json.partial-{os.getpid()}", victim, directory=False)

    plan = plan_session_migration(src, dst)
    report = run_session_migration(plan)

    assert report.copied == ("legacylegacy",)
    assert victim.read_text() == "untouched"
    landed = dst / "legacylegacy.json"
    assert not landed.is_symlink() and landed.read_text() == payload
    assert landed.stat().st_mtime == (src / "legacylegacy.json").stat().st_mtime
    # Only the planted link is left behind; the copy's own partial is gone.
    assert [p.name for p in _partials(dst)] == [f"legacylegacy.json.partial-{os.getpid()}"]


def test_legacy_file_already_present_is_skipped(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    dst.mkdir()
    (src / "legacylegacy.json").write_text("source")
    (dst / "legacylegacy.json").write_text("existing")

    report = run_session_migration(plan_session_migration(src, dst))

    assert report.skipped_present == ("legacylegacy",)
    assert (dst / "legacylegacy.json").read_text() == "existing"


def test_plan_rejects_same_and_nested_roots(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    with pytest.raises(SessionMigrationError):
        plan_session_migration(root, root)
    with pytest.raises(SessionMigrationError):
        plan_session_migration(root, root / "child")
    with pytest.raises(SessionMigrationError):
        plan_session_migration(root / "child", root)
    with pytest.raises(SessionMigrationError):
        plan_session_migration(root, root / "a" / "b" / "c")
    # Nothing was created while validating.
    assert list(root.iterdir()) == []


def test_plan_rejects_same_and_nested_roots_spelled_in_another_case(tmp_path: Path) -> None:
    """On a case-insensitive volume the roots are compared by identity, not spelling."""
    root = tmp_path / "sessions"
    root.mkdir()
    alias = tmp_path / "SESSIONS"
    if not alias.is_dir() or not os.path.samefile(root, alias):
        pytest.skip("case-sensitive filesystem")
    with pytest.raises(SessionMigrationError, match="same directory"):
        plan_session_migration(root, alias)
    with pytest.raises(SessionMigrationError, match="inside source"):
        plan_session_migration(root, alias / "nested" / "deeper")
    with pytest.raises(SessionMigrationError, match="inside destination"):
        plan_session_migration(root / "child", alias)
    assert list(root.iterdir()) == []


def test_plan_rejects_same_root_through_symlink(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    link = tmp_path / "sessions-link"
    _symlink_or_skip(link, root, directory=True)
    with pytest.raises(SessionMigrationError):
        plan_session_migration(root, link)
    with pytest.raises(SessionMigrationError):
        plan_session_migration(link, root / "nested")


def test_plan_rejects_source_that_is_not_a_directory(tmp_path: Path) -> None:
    with pytest.raises(SessionMigrationError):
        plan_session_migration(tmp_path / "missing", tmp_path / "dst")
    file_source = tmp_path / "file"
    file_source.write_text("")
    with pytest.raises(SessionMigrationError):
        plan_session_migration(file_source, tmp_path / "dst")


def test_symlinked_session_root_is_rejected_not_copied(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    _make_session(src, "aaaaaaaaaaaa")
    real = _make_session(tmp_path / "elsewhere", "linklinklink")
    _symlink_or_skip(src / "linklinklink", real, directory=True)

    plan = plan_session_migration(src, dst)
    assert [item.session_id for item in plan.items] == ["aaaaaaaaaaaa"]
    assert plan.rejected == ((src / "linklinklink", "linked entry"),)

    report = run_session_migration(plan)

    assert report.copied == ("aaaaaaaaaaaa",)
    assert report.failed == ((src / "linklinklink", "linked entry"),)
    assert not os.path.lexists(dst / "linklinklink")


def test_symlinked_legacy_file_is_rejected_not_copied(tmp_path: Path) -> None:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    real = tmp_path / "elsewhere.json"
    real.write_text("{}")
    _symlink_or_skip(src / "linklinklink.json", real, directory=False)

    plan = plan_session_migration(src, dst)
    assert plan.items == ()
    assert plan.rejected == ((src / "linklinklink.json", "linked entry"),)

    report = run_session_migration(plan)
    assert report.failed == ((src / "linklinklink.json", "linked entry"),)
    assert not os.path.lexists(dst / "linklinklink.json")


def test_junctions_dropped_when_nested_and_rejected_at_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """NT directory junctions cannot be created on POSIX; simulate them via os.path.isjunction."""
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    session_dir = _make_session(src, "aaaaaaaaaaaa")
    nested_junction = session_dir / "compactions" / "planted"
    nested_junction.mkdir(parents=True)
    (nested_junction / "payload.txt").write_text("x")
    (session_dir / "compactions" / "real").mkdir()
    (session_dir / "compactions" / "real" / "keep.txt").write_text("y")
    root_junction = _make_session(src, "jjjjjjjjjjjj")
    planted_keys = {
        os.path.normcase(os.path.abspath(nested_junction)),
        os.path.normcase(os.path.abspath(root_junction)),
    }
    real_isjunction = os.path.isjunction

    def fake_isjunction(candidate: object) -> bool:
        return os.path.normcase(os.path.abspath(candidate)) in planted_keys or real_isjunction(candidate)  # type: ignore[arg-type]

    monkeypatch.setattr(os.path, "isjunction", fake_isjunction)

    plan = plan_session_migration(src, dst)
    assert [item.session_id for item in plan.items] == ["aaaaaaaaaaaa"]
    assert plan.rejected == ((root_junction, "linked entry"),)

    report = run_session_migration(plan)

    assert report.copied == ("aaaaaaaaaaaa",)
    assert report.failed == ((root_junction, "linked entry"),)
    assert not (dst / "jjjjjjjjjjjj").exists()
    assert not (dst / "aaaaaaaaaaaa" / "compactions" / "planted").exists()
    assert (dst / "aaaaaaaaaaaa" / "compactions" / "real" / "keep.txt").read_text() == "y"


@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_legacy_permissions_are_accepted_but_destination_is_private(tmp_path: Path, mode: int) -> None:
    src = tmp_path / "src"
    src.mkdir()
    source = src / "legacylegacy.json"
    # Deliberately reproduce an older writer, including the elevated token's
    # default owner on Windows, rather than planting a modern owner-only file.
    source.write_bytes(b'{"legacy":true}')
    source.chmod(mode)
    dst = tmp_path / "dst"
    report = run_session_migration(plan_session_migration(src, dst))
    assert report.failed == ()
    assert report.copied == ("legacylegacy",)
    copied = dst / source.name
    assert copied.read_bytes() == b'{"legacy":true}'
    assert source.read_bytes() == b'{"legacy":true}'
    if os.name != "nt":
        # The copy never inherits a broad legacy mode: it is written as
        # privately as the session store writes its own files.
        assert stat.S_IMODE(copied.stat().st_mode) == 0o600
        assert source.stat().st_mode & 0o777 == mode


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_session_folder_copy_keeps_files_private_and_their_timestamps(tmp_path: Path) -> None:
    src = tmp_path / "src"
    session = _make_session(src, "aaaaaaaaaaaa")
    readable = {SESSION_FILE_NAME: 0o644, "snapshots/turn_1.json": 0o640, "hook.sh": 0o755}
    (session / "hook.sh").write_text("#!/bin/sh\n")
    for relative, mode in readable.items():
        (session / relative).chmod(mode)
    os.utime(session / SESSION_FILE_NAME, (1_000_000, 1_000_000))
    dst = tmp_path / "dst"

    report = run_session_migration(plan_session_migration(src, dst))

    assert report.failed == ()
    assert report.copied == ("aaaaaaaaaaaa",)
    copied = dst / "aaaaaaaaaaaa"
    assert {relative: stat.S_IMODE((copied / relative).stat().st_mode) for relative in readable} == {
        SESSION_FILE_NAME: 0o600,
        "snapshots/turn_1.json": 0o600,
        "hook.sh": 0o700,
    }
    assert (copied / SESSION_FILE_NAME).stat().st_mtime == 1_000_000
    assert {relative: stat.S_IMODE((session / relative).stat().st_mode) for relative in readable} == readable


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS file flags")
def test_locked_session_files_are_copied_private_and_stay_locked(tmp_path: Path) -> None:
    src = tmp_path / "src"
    session = _make_session(src, "aaaaaaaaaaaa")
    locked = session / SESSION_FILE_NAME
    locked.chmod(0o644)
    content = locked.read_bytes()
    dst = tmp_path / "dst"
    copied = dst / "aaaaaaaaaaaa" / SESSION_FILE_NAME
    # Finder's "Locked": copy2 copies it after the mode, and a locked file
    # refuses even its owner's chmod.
    os.chflags(locked, stat.UF_IMMUTABLE)
    try:
        report = run_session_migration(plan_session_migration(src, dst))

        assert report.failed == ()
        assert report.copied == ("aaaaaaaaaaaa",)
        info = os.stat(copied)
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert info.st_flags & stat.UF_IMMUTABLE
        with secure_open_owner_only_binary(copied) as handle:
            assert handle.read() == content
    finally:
        for path in (locked, copied):
            if os.path.lexists(path):
                os.chflags(path, 0)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
@pytest.mark.parametrize(("error", "migrates"), [(errno.EPERM, True), (errno.EIO, False)])
def test_only_a_filesystem_that_refuses_the_mode_keeps_a_broad_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: int, migrates: bool
) -> None:
    src = tmp_path / "src"
    session = _make_session(src, "aaaaaaaaaaaa")
    (session / SESSION_FILE_NAME).chmod(0o644)
    real_chmod = os.chmod

    def refuse_narrowing(path: str, mode: int, *, follow_symlinks: bool = True) -> None:
        # copy2 still copies the source's mode; only the narrowing is refused.
        if mode == 0o600:
            raise OSError(error, os.strerror(error), path)
        real_chmod(path, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(migration_module.os, "chmod", refuse_narrowing)
    dst = tmp_path / "dst"

    report = run_session_migration(plan_session_migration(src, dst))

    if migrates:
        # exFAT, FAT and some network shares refuse modes: the copy keeps the
        # one the filesystem reports, as the store's own saves do there.
        assert report.failed == ()
        assert report.copied == ("aaaaaaaaaaaa",)
        assert stat.S_IMODE((dst / "aaaaaaaaaaaa" / SESSION_FILE_NAME).stat().st_mode) == 0o644
    else:
        assert report.copied == ()
        assert [source for source, _reason in report.failed] == [session]
        assert os.strerror(errno.EIO) in report.failed[0][1]
        assert not (dst / "aaaaaaaaaaaa").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS ACLs grant access beside the mode bits")
def test_migrated_copies_drop_an_acl_the_destination_passes_on(tmp_path: Path) -> None:
    from chrys.foundation.platform import files

    shared = tmp_path / "shared"
    shared.mkdir()
    granted = subprocess.run(
        ["chmod", "+a", "everyone allow read,list,search,file_inherit,directory_inherit", str(shared)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    if granted.returncode != 0:
        pytest.skip(f"this volume cannot carry ACLs: {granted.stderr!r}")

    def assert_no_acl(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            files._verify_darwin_no_acl(fd)
        finally:
            os.close(fd)

    # The destination really passes its ACL on to what is created in it.
    (shared / "control.json").write_text("{}")
    with pytest.raises(files.SecureFileError, match="extended ACL"):
        assert_no_acl(shared / "control.json")
    src = tmp_path / "src"
    _make_session(src, "aaaaaaaaaaaa")
    (src / "legacylegacy.json").write_bytes(b"legacy")
    dst = shared / "sessions"

    report = run_session_migration(plan_session_migration(src, dst))

    assert report.failed == ()
    assert set(report.copied) == {"aaaaaaaaaaaa", "legacylegacy"}
    session = dst / "aaaaaaaaaaaa"
    for copied in (dst / "legacylegacy.json", session / SESSION_FILE_NAME, session / "snapshots" / "turn_1.json"):
        # Owner-only, as the session store's secure readers require: 0600 and no ACL.
        with secure_open_owner_only_binary(copied) as handle:
            assert handle.read()
    for directory in (session, session / "snapshots"):
        assert_no_acl(directory)


def test_legacy_copy_does_not_require_a_filesystem_that_stores_owner_only_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # exFAT, FAT and some network shares report a fixed broad mode; the store
    # still saves sessions there, so migration must still copy into them.
    from chrys.foundation.platform import files

    def cannot_store_owner_only(_fd: int) -> None:
        raise files.SecureFileError("Secure file permissions are not owner-only.")

    monkeypatch.setattr(files, "verify_owner_only_fd", cannot_store_owner_only)
    src = tmp_path / "src"
    src.mkdir()
    (src / "legacylegacy.json").write_bytes(b"legacy")
    dst = tmp_path / "dst"
    report = run_session_migration(plan_session_migration(src, dst))
    assert report.failed == ()
    assert report.copied == ("legacylegacy",)
    assert (dst / "legacylegacy.json").read_bytes() == b"legacy"
    assert _partials(dst) == []


def test_migration_accepts_parent_aliases_for_legacy_files_and_directories(tmp_path: Path) -> None:
    import chrys.service.tools.session_artifacts as session_artifacts

    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    _symlink_or_skip(alias, real, directory=True)
    src = alias / "src"
    session = _make_session(src, "aaaaaaaaaaaa")
    (session / "doc_converter").mkdir()
    # Written as an older release did, not as an owner-only publish.
    (session / "doc_converter" / "image-copied.png").write_bytes(b"copied-image")
    (src / "legacylegacy.json").write_bytes(b"legacy")
    dst = alias / "dst"
    # Run the Windows re-publish of document images on every host.
    with patch.object(session_artifacts, "get_platform", return_value=SimpleNamespace(is_windows=True)):
        report = run_session_migration(plan_session_migration(src, dst))
    assert report.failed == ()
    assert set(report.copied) == {"aaaaaaaaaaaa", "legacylegacy"}
    assert (dst / "legacylegacy.json").read_bytes() == b"legacy"
    with secure_open_owner_only_binary(real / "dst" / "aaaaaaaaaaaa" / "doc_converter" / "image-copied.png") as copied:
        assert copied.read() == b"copied-image"


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership boundary")
def test_legacy_import_accepts_readable_foreign_owner_without_relaxing_secure_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.foundation.platform import files

    source = tmp_path.resolve() / "legacy.json"
    source.write_bytes(b"old elevated session")
    actual_uid = source.stat().st_uid
    monkeypatch.setattr(files, "_posix_effective_uid", lambda: actual_uid + 1)
    with pytest.raises(files.SecureFileError, match="not owned"):
        files.secure_open_owner_verified_binary(source)
    with migration_module._open_source_file(source) as handle:
        assert handle.read() == b"old elevated session"


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory search permission")
def test_legacy_file_migrates_beneath_an_ancestor_that_can_be_entered_but_not_listed(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")
    ancestor = tmp_path / "enter-only"
    src = ancestor / "src"
    _make_session(src, "aaaaaaaaaaaa")
    (src / "legacylegacy.json").write_bytes(b"legacy")
    dst = tmp_path / "dst"
    ancestor.chmod(0o111)
    try:
        report = run_session_migration(plan_session_migration(src, dst))
    finally:
        ancestor.chmod(0o755)
    assert report.failed == ()
    assert set(report.copied) == {"aaaaaaaaaaaa", "legacylegacy"}
    assert (dst / "legacylegacy.json").read_bytes() == b"legacy"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions")
def test_unreadable_legacy_file_reports_the_system_reason(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    src = tmp_path / "src"
    src.mkdir()
    source = src / "legacylegacy.json"
    source.write_bytes(b"legacy")
    source.chmod(0)
    try:
        report = run_session_migration(plan_session_migration(src, tmp_path / "dst"))
    finally:
        source.chmod(0o600)
    assert report.copied == ()
    # The dialog tells the user to fix what the reason names.
    assert report.failed == ((source, f"[Errno 13] Permission denied: '{source}'"),)


def test_legacy_missing_source_has_actionable_reason(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="source changed since planning"):
        migration_module._open_source_file(tmp_path / "missing.json")


@pytest.mark.parametrize("legacy", [True, False])
def test_partial_cleanup_failure_logs_path_without_masking_copy_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, legacy: bool
) -> None:
    src = tmp_path / "src"
    if legacy:
        src.mkdir()
        (src / "legacylegacy.json").write_bytes(b"session data")
    else:
        session = _make_session(src, "aaaaaaaaaaaa")
        (session / "nested").mkdir()
        (session / "nested" / "blocked.txt").write_text("held open")
        for index in range(5):
            (session / "nested" / f"turn-{index}.json").write_text("{}")
    dst = tmp_path / "dst"

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("original publish failure")

    monkeypatch.setattr(migration_module.os, "replace", fail_replace)
    if legacy:
        original_unlink = Path.unlink

        def fail_partial_unlink(path: Path, missing_ok: bool = False) -> None:
            if ".partial-" in path.name:
                raise PermissionError("cleanup refused")
            original_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", fail_partial_unlink)
    else:
        original_os_unlink = os.unlink

        def refuse_blocked_unlink(path: str, *, dir_fd: int | None = None) -> None:
            if os.path.basename(os.fspath(path)) == "blocked.txt":
                raise PermissionError("cleanup refused")
            original_os_unlink(path, dir_fd=dir_fd)

        monkeypatch.setattr(migration_module.os, "unlink", refuse_blocked_unlink)

    report = run_session_migration(plan_session_migration(src, dst))
    assert report.copied == ()
    assert len(report.failed) == 1
    assert report.failed[0][1] == "original publish failure"
    leftovers = _partials(dst)
    assert len(leftovers) == 1
    if not legacy:
        # One undeletable entry must not keep every other copied file behind.
        assert [entry.name for entry in leftovers[0].rglob("*") if entry.is_file()] == ["blocked.txt"]
    assert str(leftovers[0]) in caplog.text
    assert "cleanup refused" in caplog.text
    assert "may contain session data" in caplog.text
