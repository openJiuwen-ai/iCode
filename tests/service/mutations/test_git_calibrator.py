# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for GitDiffCalibrator and git-based implicit change detection."""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest

from chrys.foundation.platform import get_platform
from chrys.service.mutations import git_calibrator, git_state
from chrys.service.mutations.git_calibrator import GitDiffCalibrator
from chrys.service.mutations.git_state import GitDeltaResult, read_git_target_kind, repo_relative_git_path
from chrys.service.mutations.store import SnapshotPolicy, SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource, SnapshotSkipReason

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.service.mutations.pipeline import MutationResult


def _init_git_repo(path: Path, git_repo_factory: Callable[[Path], Path]) -> None:
    """Copy the shared committed repository into *path*."""
    git_repo_factory(path)


# ===========================================================================
# find_repo_root
# ===========================================================================


class TestFindRepoRoot:
    def test_finds_repo_root(self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]) -> None:
        _init_git_repo(tmp_path, git_repo_factory)
        assert GitDiffCalibrator.find_repo_root(str(tmp_path)) == str(tmp_path)

    def test_finds_from_subdir(self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]) -> None:
        _init_git_repo(tmp_path, git_repo_factory)
        subdir = tmp_path / "src" / "deep"
        subdir.mkdir(parents=True)
        assert GitDiffCalibrator.find_repo_root(str(subdir)) == str(tmp_path)

    def test_finds_worktree_git_file_from_subdir(self, tmp_path: Path) -> None:
        (tmp_path / ".git").write_text("gitdir: /tmp/chrys-test-worktree\n", encoding="utf-8")
        subdir = tmp_path / "src" / "deep"
        subdir.mkdir(parents=True)

        assert GitDiffCalibrator.find_repo_root(str(subdir)) == str(tmp_path)

    def test_returns_none_for_non_repo(self, tmp_path: Path) -> None:
        assert GitDiffCalibrator.find_repo_root(str(tmp_path)) is None


@pytest.mark.skipif(get_platform().is_windows, reason="symlinks require privileges on Windows")
def test_repo_relative_git_path_keeps_symlink_entry_inside_root(tmp_path: Path) -> None:
    """Git addresses the link entry, never its target: a tracked symlink
    pointing outside the repository is still a repository path."""
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside target", encoding="utf-8")
    link = root / "link"
    link.symlink_to(outside)

    assert repo_relative_git_path(str(root), str(link)) == "link"


def test_read_git_target_kind_repo_root_is_directory(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
) -> None:
    """A target resolving to the repository root queries ``.``, which
    makes ls-tree enumerate the top level — the first record is an
    arbitrary sibling (here a plain file), never evidence for the root
    itself: the root of an answering tree is a directory."""
    _init_git_repo(tmp_path, git_repo_factory)
    (tmp_path / "aaa.txt").write_text("sorts first", encoding="utf-8")
    subprocess.run(["git", "add", "aaa.txt"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "top-level blob"], cwd=tmp_path, capture_output=True, check=True)

    assert read_git_target_kind(str(tmp_path), "HEAD", ".") is True
    assert read_git_target_kind(str(tmp_path), "0" * 40, ".") is None


# ===========================================================================
# capture_before + detect_implicit_changes
# ===========================================================================


class TestGitDiffCalibration:
    """End-to-end calibration scenarios in a real git repo."""

    def test_clean_repo_no_changes(self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]) -> None:
        """Clean repo, no changes during turn -> no implicit changes."""
        _init_git_repo(tmp_path, git_repo_factory)
        cal = GitDiffCalibrator(str(tmp_path))
        cal.capture_before()
        changes = cal.detect_implicit_changes(set())
        assert changes == []

    def test_detects_newly_dirty_tracked_file(self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]) -> None:
        """A tracked file modified during the turn is detected."""
        _init_git_repo(tmp_path, git_repo_factory)
        cal = GitDiffCalibrator(str(tmp_path))
        cal.capture_before()

        # Simulate implicit modification (e.g. python script.py)
        (tmp_path / "README.md").write_text("modified by script", encoding="utf-8")

        changes = cal.detect_implicit_changes(set())
        assert len(changes) == 1
        assert changes[0].operation == MutationOp.MODIFY
        assert changes[0].path.endswith("README.md")
        # before_data = HEAD content
        assert changes[0].before_data == b"committed content"

    def test_detects_new_untracked_file(self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]) -> None:
        """An untracked file created during the turn is detected."""
        _init_git_repo(tmp_path, git_repo_factory)
        cal = GitDiffCalibrator(str(tmp_path))
        cal.capture_before()

        (tmp_path / "generated.txt").write_text("output", encoding="utf-8")

        changes = cal.detect_implicit_changes(set())
        assert len(changes) == 1
        assert changes[0].operation == MutationOp.CREATE
        assert changes[0].before_data is None  # no HEAD version

    def test_excludes_already_tracked_files(self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]) -> None:
        """Files already tracked by per-tool mutations are excluded."""
        _init_git_repo(tmp_path, git_repo_factory)
        cal = GitDiffCalibrator(str(tmp_path))
        cal.capture_before()

        committed = tmp_path / "README.md"
        committed.write_text("modified", encoding="utf-8")

        norm = os.path.normpath(os.path.abspath(str(committed)))
        changes = cal.detect_implicit_changes({norm})
        assert changes == []

    def test_detects_further_modified_dirty_file(
        self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]
    ) -> None:
        """A file that was dirty before the turn and modified further is detected."""
        _init_git_repo(tmp_path, git_repo_factory)

        # Make the file dirty BEFORE capture_before
        (tmp_path / "README.md").write_text("dirty before turn", encoding="utf-8")

        cal = GitDiffCalibrator(str(tmp_path))
        dirty_files = cal.capture_before()
        assert len(dirty_files) >= 1

        # Modify further during the turn
        (tmp_path / "README.md").write_text("even dirtier during turn", encoding="utf-8")

        changes = cal.detect_implicit_changes(set())
        assert len(changes) == 1
        assert changes[0].operation == MutationOp.MODIFY
        # before_data is None because the file was pre-snapshotted (already dirty)
        assert changes[0].before_data is None

    def test_detects_deleted_file(self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]) -> None:
        """An untracked file that existed before and was deleted during the turn."""
        _init_git_repo(tmp_path, git_repo_factory)
        temp_file = tmp_path / "temp_output.txt"
        temp_file.write_text("temp data", encoding="utf-8")

        cal = GitDiffCalibrator(str(tmp_path))
        cal.capture_before()

        temp_file.unlink()

        changes = cal.detect_implicit_changes(set())
        assert len(changes) == 1
        assert changes[0].operation == MutationOp.DELETE

    def test_detects_clean_tracked_file_deleted_during_turn(
        self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]
    ) -> None:
        """A clean tracked file deleted during the turn is detected with HEAD content."""
        _init_git_repo(tmp_path, git_repo_factory)
        committed = tmp_path / "README.md"

        cal = GitDiffCalibrator(str(tmp_path))
        cal.capture_before()

        committed.unlink()

        changes = cal.detect_implicit_changes(set())
        assert len(changes) == 1
        assert changes[0].operation == MutationOp.DELETE
        assert changes[0].path == os.path.normpath(os.path.abspath(str(committed)))
        assert changes[0].before_data == b"committed content"

    def test_ignores_tracked_file_deleted_before_turn(
        self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]
    ) -> None:
        """A tracked deletion that predates capture_before is not attributed to the command."""
        _init_git_repo(tmp_path, git_repo_factory)
        committed = tmp_path / "README.md"
        committed.unlink()

        cal = GitDiffCalibrator(str(tmp_path))
        dirty_files = cal.capture_before()
        assert os.path.normpath(os.path.abspath(str(committed))) in {
            os.path.normpath(os.path.abspath(path)) for path in dirty_files
        }

        changes = cal.detect_implicit_changes(set())
        assert changes == []

    def test_detects_dirty_tracked_file_deleted_during_turn(
        self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]
    ) -> None:
        """A dirty tracked file deleted during the turn uses the pre-snapshot."""
        _init_git_repo(tmp_path, git_repo_factory)
        committed = tmp_path / "README.md"
        committed.write_text("dirty before turn", encoding="utf-8")

        cal = GitDiffCalibrator(str(tmp_path))
        dirty_files = cal.capture_before()
        assert os.path.normpath(os.path.abspath(str(committed))) in {
            os.path.normpath(os.path.abspath(path)) for path in dirty_files
        }

        committed.unlink()

        changes = cal.detect_implicit_changes(set())
        assert len(changes) == 1
        assert changes[0].operation == MutationOp.DELETE
        assert changes[0].path == os.path.normpath(os.path.abspath(str(committed)))
        assert changes[0].before_data is None

    def test_no_false_positives_unchanged_dirty_file(
        self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]
    ) -> None:
        """A file dirty before the turn but NOT modified during is not detected."""
        _init_git_repo(tmp_path, git_repo_factory)
        (tmp_path / "README.md").write_text("dirty but stable", encoding="utf-8")

        cal = GitDiffCalibrator(str(tmp_path))
        cal.capture_before()

        # Don't modify anything during the turn

        changes = cal.detect_implicit_changes(set())
        assert changes == []

    def test_scope_paths_limit_detected_changes(self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]) -> None:
        """Workspace scoping should ignore repo changes outside the configured root."""
        _init_git_repo(tmp_path, git_repo_factory)
        pkg_a = tmp_path / "pkg_a"
        pkg_b = tmp_path / "pkg_b"
        pkg_a.mkdir()
        pkg_b.mkdir()
        file_a = pkg_a / "a.py"
        file_b = pkg_b / "b.py"
        file_a.write_text("a1", encoding="utf-8")
        file_b.write_text("b1", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "add packages"], cwd=tmp_path, capture_output=True, check=True)

        cal = GitDiffCalibrator(str(tmp_path), scope_paths=[str(pkg_a)])
        cal.capture_before()

        file_a.write_text("a2", encoding="utf-8")
        file_b.write_text("b2", encoding="utf-8")

        changes = cal.detect_implicit_changes(set())
        assert {os.path.relpath(change.path, tmp_path).replace(os.sep, "/") for change in changes} == {"pkg_a/a.py"}

    @pytest.mark.parametrize("cwd_arg_name", ["workdir", "cwd"])
    def test_pipeline_scope_includes_command_cwd_outside_workspace_cwd(
        self, tmp_path: Path, cwd_arg_name: str, git_repo_factory: Callable[[Path], Path]
    ) -> None:
        """Calibration should include the resolved command cwd as well as the primary workspace."""
        from chrys.service.mutations.pipeline import _track_git_capture_before

        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo, git_repo_factory)
        pkg_a = repo / "pkg_a"
        pkg_b = repo / "pkg_b"
        pkg_c = repo / "pkg_c"
        pkg_a.mkdir()
        pkg_b.mkdir()
        pkg_c.mkdir()
        file_a = pkg_a / "a.py"
        file_b = pkg_b / "b.py"
        file_c = pkg_c / "c.py"
        file_a.write_text("a1", encoding="utf-8")
        file_b.write_text("b1", encoding="utf-8")
        file_c.write_text("c1", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "add packages"], cwd=repo, capture_output=True, check=True)

        session_dir = tmp_path / "_session"
        session_dir.mkdir()
        tracker = MutationTracker(SnapshotStore(session_dir))
        tracker.start_turn(1)

        cal = asyncio.run(
            _track_git_capture_before(
                tracker,
                {cwd_arg_name: str(pkg_b)},
                workspace_cwd=str(pkg_a),
            )
        )
        assert cal is not None

        file_b.write_text("b2", encoding="utf-8")
        file_c.write_text("c2", encoding="utf-8")

        changes = cal.detect_implicit_changes(tracker.get_changed_files_set())
        rel_changes = {os.path.relpath(change.path, repo).replace(os.sep, "/") for change in changes}
        assert rel_changes == {"pkg_b/b.py"}


# ===========================================================================
# Integration: GitDiffCalibrator + MutationTracker
# ===========================================================================


class TestGitCalibrationIntegration:
    """Full lifecycle: calibrator -> tracker.record_calibrated.

    Uses a separate session dir for the SnapshotStore (outside the git
    repo) to avoid blob files showing up as untracked git changes.
    """

    @staticmethod
    def _make_tracker(tmp_path: Path) -> MutationTracker:
        """Create a tracker with its store OUTSIDE the git repo."""
        session_dir = tmp_path / "_session"
        session_dir.mkdir(exist_ok=True)
        return MutationTracker(SnapshotStore(session_dir))

    def test_calibrated_mutation_has_before_after_hashes(
        self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]
    ) -> None:
        """record_calibrated creates mutation with correct hashes."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo, git_repo_factory)
        tracker = self._make_tracker(tmp_path)
        tracker.start_turn(1)

        cal = GitDiffCalibrator(str(repo))
        cal.capture_before()

        # Implicit modification
        committed = repo / "README.md"
        committed.write_text("implicitly changed", encoding="utf-8")

        changes = cal.detect_implicit_changes(tracker.get_changed_files_set())
        assert len(changes) == 1

        m = tracker.record_calibrated(changes[0].path, changes[0].operation, changes[0].before_data)
        assert m is not None
        assert m.source == MutationSource.IMPLICIT
        assert m.before_hash is not None  # HEAD content
        assert m.after_hash is not None  # current disk content
        assert m.before_hash != m.after_hash

        # Verify before blob = committed content
        before_bytes = tracker.store.read_blob(m.before_hash)
        assert before_bytes == b"committed content"

        # Verify after blob = current disk
        after_bytes = tracker.store.read_blob(m.after_hash)
        assert after_bytes == b"implicitly changed"

    def test_calibrated_new_file_has_no_before(self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]) -> None:
        """Untracked new file: before_hash=None, after_hash=content."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo, git_repo_factory)
        tracker = self._make_tracker(tmp_path)
        tracker.start_turn(1)

        cal = GitDiffCalibrator(str(repo))
        cal.capture_before()

        (repo / "new_output.txt").write_text("generated", encoding="utf-8")

        changes = cal.detect_implicit_changes(set())
        m = tracker.record_calibrated(changes[0].path, changes[0].operation, changes[0].before_data)

        assert m.before_hash is None
        assert m.after_hash is not None

    def test_calibrated_dirty_file_uses_presnapshot(
        self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]
    ) -> None:
        """Already-dirty file: before_hash from pre-snapshot, not git HEAD."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo, git_repo_factory)
        tracker = self._make_tracker(tmp_path)
        tracker.start_turn(1)

        # Make dirty before capture
        committed = repo / "README.md"
        committed.write_text("dirty state", encoding="utf-8")

        cal = GitDiffCalibrator(str(repo))
        dirty_files = cal.capture_before()
        # Pre-snapshot dirty files (as engine would do)
        tracker.pre_snapshot(dirty_files)

        # Modify further
        committed.write_text("further modified", encoding="utf-8")

        changes = cal.detect_implicit_changes(tracker.get_changed_files_set())
        assert len(changes) == 1
        # before_data is None (was pre-snapshotted)
        assert changes[0].before_data is None

        m = tracker.record_calibrated(changes[0].path, changes[0].operation, changes[0].before_data)
        assert m is not None
        # before_hash should come from the pre-snapshot (dirty state)
        before_bytes = tracker.store.read_blob(m.before_hash)
        assert before_bytes == b"dirty state"

    def test_calibrated_before_chains_from_prior_tool_edit(self, tmp_path: Path) -> None:
        """Tool edit then opaque shell edit: the calibrated row's diff starts
        at the tool edit's result, not the turn-start content — otherwise the
        shell command's displayed diff re-attributes the tool edit."""
        tracker = self._make_tracker(tmp_path)
        tracker.start_turn(1)
        target = tmp_path / "chained.txt"
        target.write_text("original", encoding="utf-8")
        first = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-1")
        assert first is not None
        target.write_text("tool edited", encoding="utf-8")
        tracker.record_after(first)

        target.write_text("shell edited", encoding="utf-8")
        calibrated = tracker.record_calibrated(str(target), MutationOp.MODIFY, b"original", "call-2")

        assert calibrated is not None
        assert calibrated.before_hash == first.after_hash
        assert tracker.store.read_blob(calibrated.before_hash) == b"tool edited"
        # The net turn summary stays anchored at the turn-start snapshot.
        summary = tracker.get_turn_file_summary(1)
        (diff,) = summary.values()
        assert diff.before == first.before_hash
        assert diff.after == calibrated.after_hash

    def test_calibrated_before_chains_from_prior_create(self, tmp_path: Path) -> None:
        """Created-then-shell-modified must not render as empty-to-new."""
        tracker = self._make_tracker(tmp_path)
        tracker.start_turn(1)
        target = tmp_path / "created.txt"
        first = tracker.record(str(target), MutationOp.CREATE, MutationSource.WRITE_FILE, "call-1")
        assert first is not None
        target.write_text("created content", encoding="utf-8")
        tracker.record_after(first)

        target.write_text("shell edited", encoding="utf-8")
        calibrated = tracker.record_calibrated(str(target), MutationOp.MODIFY, None, "call-2")

        assert calibrated is not None
        assert calibrated.before_hash == first.after_hash
        assert tracker.store.read_blob(calibrated.before_hash) == b"created content"

    def test_calibrated_before_rechains_from_disk_after_retry(self, tmp_path: Path) -> None:
        """A retry clears the content chain (``reset_file_cache``); the next
        pass's pre-execution observation must restart it from current disk,
        not the turn-start snapshot — or a shell edit after the retry
        re-attributes the first pass's edit to itself."""
        tracker = self._make_tracker(tmp_path)
        tracker.start_turn(1)
        target = tmp_path / "retried.txt"
        target.write_text("pass one start", encoding="utf-8")
        first = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-1")
        assert first is not None
        target.write_text("pass one edited", encoding="utf-8")
        tracker.record_after(first)

        tracker.reset_file_cache()

        # Retry pass: the calibrator/scanner pre-snapshots the (still dirty)
        # file before the shell command runs, then the command edits it.
        tracker.pre_snapshot([str(target)])
        target.write_text("retry shell edited", encoding="utf-8")
        calibrated = tracker.record_calibrated(str(target), MutationOp.MODIFY, b"pass one start", "call-2")

        assert calibrated is not None
        assert calibrated.before_hash == first.after_hash
        assert tracker.store.read_blob(calibrated.before_hash) == b"pass one edited"
        # The rollback anchor stays the turn-start snapshot.
        summary = tracker.get_turn_file_summary(1)
        (diff,) = summary.values()
        assert diff.before == first.before_hash
        assert diff.after == calibrated.after_hash

    def test_calibrated_before_uses_fresh_git_data_after_retry(self, tmp_path: Path) -> None:
        """Retry with no pre-execution observation: the fresh old-HEAD blob
        is this row's true before endpoint — falling back to the turn-start
        snapshot would record the second edit as A→C instead of B→C."""
        tracker = self._make_tracker(tmp_path)
        tracker.start_turn(1)
        target = tmp_path / "retried.txt"
        target.write_text("A", encoding="utf-8")
        first = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-1")
        assert first is not None
        target.write_text("B", encoding="utf-8")
        tracker.record_after(first)

        tracker.reset_file_cache()

        # Retry pass: the path is clean at command start (the first pass
        # committed B), so nothing re-observes it pre-execution; Git
        # supplies B as the fresh before endpoint of the second edit.
        target.write_text("C", encoding="utf-8")
        calibrated = tracker.record_calibrated(str(target), MutationOp.MODIFY, b"B", "call-2")

        assert calibrated is not None
        assert calibrated.before_hash == first.after_hash
        assert calibrated.before_hash == SnapshotStore.content_hash(b"B")
        assert calibrated.after_hash == SnapshotStore.content_hash(b"C")
        # The rollback anchor stays the turn-start snapshot (A), untouched
        # by the fresh evidence.
        summary = tracker.get_turn_file_summary(1)
        (diff,) = summary.values()
        assert diff.before == first.before_hash
        assert diff.after == calibrated.after_hash

    @pytest.mark.skipif(get_platform().is_windows, reason="symlinks require privileges on Windows")
    def test_calibrated_symlink_kind_is_supplied_not_probed(self, tmp_path: Path) -> None:
        """The kind is old-tree evidence passed in by the calibrator: the
        live link is post-change and must not be probed, in either
        direction — a supplied kind survives a contradicting disk state,
        and an unsupplied kind stays unproven even when the disk would
        suggest one."""
        tracker = self._make_tracker(tmp_path)
        tracker.start_turn(1)
        a_file = tmp_path / "now_file.txt"
        a_file.write_text("x", encoding="utf-8")
        link = tmp_path / "link"
        link.symlink_to(a_file)

        recorded = tracker.record_calibrated(
            str(link), MutationOp.MODIFY, b"old-target", "call-1", before_symlink=True, before_symlink_dir=True
        )
        assert recorded is not None
        snap = tracker.get_snapshot(str(link), 1)
        assert snap is not None
        assert snap.is_symlink is True
        assert snap.symlink_target_is_dir is True

        a_dir = tmp_path / "now_dir"
        a_dir.mkdir()
        other = tmp_path / "other_link"
        other.symlink_to(a_dir, target_is_directory=True)
        recorded = tracker.record_calibrated(
            str(other), MutationOp.MODIFY, b"old-target-2", "call-1", before_symlink=True
        )
        assert recorded is not None
        snap = tracker.get_snapshot(str(other), 1)
        assert snap is not None
        assert snap.symlink_target_is_dir is None

    def test_remove_turn_reclaims_fresh_calibrated_before_blob(self, tmp_path: Path) -> None:
        """The fresh before blob is neither the turn snapshot nor any after
        endpoint; turn removal must still reclaim it."""
        tracker = self._make_tracker(tmp_path)
        tracker.start_turn(1)
        target = tmp_path / "retried.txt"
        target.write_text("A", encoding="utf-8")
        first = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-1")
        assert first is not None
        target.write_text("B", encoding="utf-8")
        tracker.record_after(first)
        tracker.reset_file_cache()
        target.write_text("C", encoding="utf-8")
        calibrated = tracker.record_calibrated(str(target), MutationOp.MODIFY, b"fresh git before", "call-2")
        assert calibrated is not None
        fresh_hash = calibrated.before_hash
        assert fresh_hash == SnapshotStore.content_hash(b"fresh git before")

        orphaned = tracker.remove_turn(1)

        assert fresh_hash in orphaned
        assert tracker.store.read_blob(fresh_hash) is None

    def test_shell_record_before_rechains_via_presnapshot_after_retry(self, tmp_path: Path) -> None:
        """Same retry seam through the non-Git heuristic-scan flow: the
        pre-scan reseeds the chain from disk, so the post-execution shell
        record chains from it instead of the turn-start snapshot."""
        tracker = self._make_tracker(tmp_path)
        tracker.start_turn(1)
        target = tmp_path / "scanned.txt"
        target.write_text("pass one start", encoding="utf-8")
        first = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-1")
        assert first is not None
        target.write_text("pass one edited", encoding="utf-8")
        tracker.record_after(first)

        tracker.reset_file_cache()

        tracker.pre_snapshot([str(target)])
        target.write_text("retry shell edited", encoding="utf-8")
        shell = tracker.record(str(target), MutationOp.MODIFY, MutationSource.SHELL, "call-2")

        assert shell is not None
        assert shell.before_hash == first.after_hash
        assert tracker.store.read_blob(shell.before_hash) == b"pass one edited"

    def test_record_before_reads_disk_after_retry(self, tmp_path: Path) -> None:
        """File-tool records run pre-write: after a retry severed the chain,
        the before endpoint must be re-read from disk directly (no pre-scan
        runs for file tools)."""
        tracker = self._make_tracker(tmp_path)
        tracker.start_turn(1)
        target = tmp_path / "rewritten.txt"
        target.write_text("pass one start", encoding="utf-8")
        first = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-1")
        assert first is not None
        target.write_text("pass one edited", encoding="utf-8")
        tracker.record_after(first)

        tracker.reset_file_cache()

        second = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-2")
        assert second is not None
        target.write_text("retry tool edited", encoding="utf-8")
        tracker.record_after(second)

        assert second.before_hash == first.after_hash
        assert tracker.store.read_blob(second.before_hash) == b"pass one edited"
        summary = tracker.get_turn_file_summary(1)
        (diff,) = summary.values()
        assert diff.before == first.before_hash
        assert diff.after == second.after_hash

    def test_calibrated_subdir_file_uses_posix_separators_for_git(
        self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]
    ) -> None:
        """Subdirectory files exercise the ``HEAD:path`` separator-normalisation.

        ``git show HEAD:path`` requires forward slashes on every platform.
        ``GitDiffCalibrator._git_head_content`` normalises via
        ``PurePath.as_posix()``.  This test runs on Win/Mac/Linux CI and
        fails fast if the normalisation regresses (Windows would emit
        ``HEAD:src\\README.md`` and git would return non-zero).
        """
        repo = tmp_path / "repo"
        _init_git_repo(repo, git_repo_factory)
        # Add a committed file in a subdirectory.
        subdir = repo / "src" / "deep"
        subdir.mkdir(parents=True)
        nested = subdir / "module.py"
        nested.write_text("nested initial", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True, check=True)

        tracker = self._make_tracker(tmp_path)
        tracker.start_turn(1)

        cal = GitDiffCalibrator(str(repo))
        cal.capture_before()

        nested.write_text("nested modified", encoding="utf-8")

        changes = cal.detect_implicit_changes(tracker.get_changed_files_set())
        assert len(changes) == 1
        # ``before_data`` came from ``git show HEAD:src/deep/module.py`` —
        # would be ``None`` if the path normalisation produced backslashes
        # on Windows (git would reject the request).
        assert changes[0].before_data == b"nested initial"

    def test_rollback_calibrated_mutation(self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]) -> None:
        """Rollback restores implicitly modified file to pre-turn state."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo, git_repo_factory)
        tracker = self._make_tracker(tmp_path)
        tracker.start_turn(1)

        cal = GitDiffCalibrator(str(repo))
        cal.capture_before()

        committed = repo / "README.md"
        committed.write_text("script output", encoding="utf-8")

        changes = cal.detect_implicit_changes(set())
        for c in changes:
            tracker.record_calibrated(c.path, c.operation, c.before_data)

        # Rollback should restore to HEAD content
        tracker.rollback(1)
        assert committed.read_text(encoding="utf-8") == "committed content"

    def test_rollback_calibrated_tracked_delete(self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]) -> None:
        """Rollback restores a clean tracked file deleted by an implicit command."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo, git_repo_factory)
        tracker = self._make_tracker(tmp_path)
        tracker.start_turn(1)

        cal = GitDiffCalibrator(str(repo))
        cal.capture_before()

        committed = repo / "README.md"
        committed.unlink()

        changes = cal.detect_implicit_changes(set())
        assert len(changes) == 1
        mutation = tracker.record_calibrated(changes[0].path, changes[0].operation, changes[0].before_data)
        assert mutation is not None
        assert mutation.before_hash is not None
        assert mutation.after_hash is None

        tracker.rollback(1)
        assert committed.read_text(encoding="utf-8") == "committed content"

    def test_rollback_calibrated_dirty_tracked_delete(
        self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]
    ) -> None:
        """Rollback restores a dirty tracked file deleted by an implicit command."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo, git_repo_factory)
        tracker = self._make_tracker(tmp_path)
        tracker.start_turn(1)

        committed = repo / "README.md"
        committed.write_text("dirty before turn", encoding="utf-8")

        cal = GitDiffCalibrator(str(repo))
        dirty_files = cal.capture_before()
        tracker.pre_snapshot(dirty_files)

        committed.unlink()

        changes = cal.detect_implicit_changes(set())
        assert len(changes) == 1
        mutation = tracker.record_calibrated(changes[0].path, changes[0].operation, changes[0].before_data)
        assert mutation is not None
        assert mutation.before_hash is not None
        assert mutation.after_hash is None

        tracker.rollback(1)
        assert committed.read_text(encoding="utf-8") == "dirty before turn"

    def test_turn_summary_includes_calibrated(self, tmp_path: Path, git_repo_factory: Callable[[Path], Path]) -> None:
        """get_turn_file_summary includes calibrated mutations."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_git_repo(repo, git_repo_factory)
        tracker = self._make_tracker(tmp_path)
        tracker.start_turn(1)

        cal = GitDiffCalibrator(str(repo))
        cal.capture_before()

        committed = repo / "README.md"
        committed.write_text("changed", encoding="utf-8")

        changes = cal.detect_implicit_changes(set())
        for c in changes:
            tracker.record_calibrated(c.path, c.operation, c.before_data)

        summary = tracker.get_turn_file_summary(1)
        norm = os.path.normpath(os.path.abspath(str(committed)))
        assert norm in summary
        assert summary[norm].before is not None
        assert summary[norm].after is not None


async def _finalize_opaque_shell(
    tracker: MutationTracker,
    repo: Path,
    mutate: Callable[[], None],
    *,
    call_id: str = "opaque-shell",
) -> MutationResult:
    """Run the mutation pipeline around an opaque command's filesystem effects."""
    from chrys.service.mutations.pipeline import finalize_mutation_tracking, prepare_mutation_tracking

    context = await prepare_mutation_tracking(
        tracker,
        "shell",
        {"command": "opaque-generator", "workdir": str(repo)},
        call_id,
        True,
        str(repo),
    )
    mutate()
    return await finalize_mutation_tracking(tracker, context, call_id)


@pytest.mark.asyncio
async def test_pipeline_detects_clean_to_clean_commit_and_preserves_rollback(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
) -> None:
    _init_git_repo(tmp_path, git_repo_factory)
    target = tmp_path / "README.md"
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)

    def _commit() -> None:
        target.write_text("committed by generator", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "generated"], cwd=tmp_path, capture_output=True, check=True)

    result = await _finalize_opaque_shell(tracker, tmp_path, _commit)
    summary = tracker.get_turn_file_summary(1)[str(target)]
    assert summary.before == SnapshotStore.content_hash(b"committed content")
    assert summary.after == SnapshotStore.content_hash(b"committed by generator")
    assert result.shell_snapshots is not None
    assert str(target) in result.shell_snapshots

    restored = tracker.rollback_turns({1})
    assert restored[0].changed is True
    assert target.read_bytes() == b"committed content"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["create", "delete"])
async def test_pipeline_committed_create_delete_have_safe_before_endpoints(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
    operation: str,
) -> None:
    _init_git_repo(tmp_path, git_repo_factory)
    target = tmp_path / ("created.txt" if operation == "create" else "README.md")
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)

    def _commit() -> None:
        if operation == "create":
            target.write_text("new", encoding="utf-8")
        else:
            target.unlink()
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", operation], cwd=tmp_path, capture_output=True, check=True)

    await _finalize_opaque_shell(tracker, tmp_path, _commit)
    summary = tracker.get_turn_file_summary(1)[str(target)]
    if operation == "create":
        assert summary.before is None
        assert summary.after == SnapshotStore.content_hash(b"new")
    else:
        assert summary.before == SnapshotStore.content_hash(b"committed content")
        assert summary.after is None

    tracker.rollback_turns({1})
    if operation == "create":
        assert target.exists() is False
    else:
        assert target.read_bytes() == b"committed content"


@pytest.mark.asyncio
async def test_pipeline_later_shell_delete_and_restore_append_true_endpoints(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
) -> None:
    _init_git_repo(tmp_path, git_repo_factory)
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)

    created = tmp_path / "created.txt"
    create = tracker.record(str(created), MutationOp.CREATE, MutationSource.WRITE_FILE, "write")
    assert create is not None
    created.write_text("temporary", encoding="utf-8")
    tracker.record_after(create)
    await _finalize_opaque_shell(tracker, tmp_path, created.unlink, call_id="delete")
    assert tracker.get_turn_file_summary(1)[str(created)].is_net_zero is True

    target = tmp_path / "README.md"
    edit = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "edit")
    assert edit is not None
    target.write_text("edited", encoding="utf-8")
    tracker.record_after(edit)

    def _restore() -> None:
        subprocess.run(["git", "restore", "README.md"], cwd=tmp_path, capture_output=True, check=True)

    await _finalize_opaque_shell(tracker, tmp_path, _restore, call_id="restore")
    summary = tracker.get_turn_file_summary(1)[str(target)]
    assert summary.is_net_zero is True
    turn = tracker.get_turn_mutations(1)
    assert turn is not None
    assert [mutation.tool_call_id for mutation in turn.mutations if mutation.path == str(target)][-1] == "restore"


@pytest.mark.asyncio
async def test_pipeline_oversized_old_blob_records_content_unavailable_row(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
) -> None:
    _init_git_repo(tmp_path, git_repo_factory)
    tracker = MutationTracker(SnapshotStore(tmp_path / "session", policy=SnapshotPolicy(max_file_bytes=1)))
    tracker.start_turn(1)
    target = tmp_path / "README.md"

    def _commit() -> None:
        target.write_text("new committed value", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "oversized"], cwd=tmp_path, capture_output=True, check=True)

    await _finalize_opaque_shell(tracker, tmp_path, _commit)
    turn = tracker.get_turn_mutations(1)
    assert turn is not None
    # A content skip is not detection truncation: the mutation event is
    # recorded with the skip reason, only the backup is withheld.
    assert turn.detection_truncated is False
    diff = tracker.get_turn_file_summary(1)[str(target)]
    assert diff.before is None
    assert diff.before_skip is SnapshotSkipReason.TOO_LARGE
    assert diff.content_unavailable is True


@pytest.mark.asyncio
async def test_pipeline_entry_meta_failure_sets_truncation_without_recording(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git_repo(tmp_path, git_repo_factory)
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    target = tmp_path / "README.md"
    monkeypatch.setattr(git_calibrator, "read_git_entry_meta", lambda *args, **kwargs: None)

    def _commit() -> None:
        target.write_text("new committed value", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "generated"], cwd=tmp_path, capture_output=True, check=True)

    await _finalize_opaque_shell(tracker, tmp_path, _commit)
    turn = tracker.get_turn_mutations(1)
    assert turn is not None
    assert turn.detection_truncated is True
    assert tracker.get_turn_file_summary(1) == {}


@pytest.mark.asyncio
@pytest.mark.skipif(get_platform().is_windows, reason="symlinks require privileges on Windows")
async def test_pipeline_tracked_symlink_to_outside_target_keeps_row(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo, git_repo_factory)
    outside = tmp_path / "outside.txt"
    outside.write_text("old target", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere.txt"
    elsewhere.write_text("new target", encoding="utf-8")
    link = repo / "link"
    link.symlink_to(outside)

    def _commit_link(message: str) -> None:
        subprocess.run(["git", "add", "link"], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", message], cwd=repo, capture_output=True, check=True)

    _commit_link("link")
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)

    def _relink() -> None:
        link.unlink()
        link.symlink_to(elsewhere)
        _commit_link("relink")

    await _finalize_opaque_shell(tracker, repo, _relink)
    turn = tracker.get_turn_mutations(1)
    assert turn is not None
    assert turn.detection_truncated is False
    rows = [row for row in turn.mutations if row.path == str(link)]
    assert len(rows) == 1
    # The before endpoint is the link entry's old-HEAD blob (its target
    # path), addressed through the entry identity — not the target file.
    assert rows[0].before_hash is not None
    assert tracker.store.read_blob(rows[0].before_hash) == os.fsencode(str(outside))

    # Rollback recreates the LINK pointing at the old target — never a
    # regular file containing the target text.
    tracker.rollback_turns({1})
    assert link.is_symlink()
    assert os.readlink(link) == str(outside)


@pytest.mark.asyncio
@pytest.mark.skipif(get_platform().is_windows, reason="symlinks require privileges on Windows")
async def test_pipeline_symlink_kind_proven_from_old_tree_not_disk(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
) -> None:
    """The same command may rewrite the link's old target too: the old
    tree is the only witness of the old kind, so a directory target that
    the command replaced with a plain file still records as a directory
    link."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo, git_repo_factory)
    folder = repo / "assets"
    folder.mkdir()
    kept = folder / "kept.txt"
    kept.write_text("x", encoding="utf-8")
    link = repo / "link"
    link.symlink_to(folder, target_is_directory=True)

    def _commit(message: str) -> None:
        subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", message], cwd=repo, capture_output=True, check=True)

    _commit("dir link")
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)

    def _rewrite() -> None:
        link.unlink()
        kept.unlink()
        folder.rmdir()
        folder.write_text("now a plain file", encoding="utf-8")
        link.symlink_to(repo / "elsewhere.txt")
        _commit("rewrite")

    await _finalize_opaque_shell(tracker, repo, _rewrite)
    turn = tracker.get_turn_mutations(1)
    assert turn is not None
    snap = tracker.get_snapshot(str(link), 1)
    assert snap is not None
    assert snap.is_symlink is True
    assert snap.symlink_target_is_dir is True


@pytest.mark.parametrize(
    "name",
    [
        "dot.name",
        "bracket[name]",
        # Windows forbids these characters in file names; the literal-scope
        # semantics they pin are covered by the remaining names there.
        *(
            pytest.param(
                name, marks=pytest.mark.skipif(get_platform().is_windows, reason="illegal in Windows file names")
            )
            for name in ("star*name", "question?name", ":leading")
        ),
    ],
)
def test_buffered_calibrator_queries_use_literal_scopes(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
    name: str,
) -> None:
    _init_git_repo(tmp_path, git_repo_factory)
    folder = tmp_path / name
    folder.mkdir()
    target = folder / "file.txt"
    target.write_text("before", encoding="utf-8")
    env = {**os.environ, "GIT_LITERAL_PATHSPECS": "1"}
    subprocess.run(["git", "add", "--", name], cwd=tmp_path, env=env, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "literal"], cwd=tmp_path, capture_output=True, check=True)
    calibrator = GitDiffCalibrator(str(tmp_path), scope_paths=[str(folder)])
    calibrator.capture_before()
    target.write_text("after", encoding="utf-8")

    changes = calibrator.detect_implicit_changes(set())
    assert [(change.path, change.operation) for change in changes] == [(str(target), MutationOp.MODIFY)]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["rename", "copy"])
async def test_pipeline_decomposes_committed_rename_and_copy(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
    operation: str,
) -> None:
    _init_git_repo(tmp_path, git_repo_factory)
    source = tmp_path / "README.md"
    destination = tmp_path / f"{operation}.md"
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)

    def _commit() -> None:
        if operation == "rename":
            source.rename(destination)
        else:
            subprocess.run(
                ["git", "config", "diff.renames", "copies"],
                cwd=tmp_path,
                capture_output=True,
                check=True,
            )
            destination.write_bytes(source.read_bytes())
            source.write_text("source changed to make copy detection eligible", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", operation], cwd=tmp_path, capture_output=True, check=True)

    await _finalize_opaque_shell(tracker, tmp_path, _commit)
    summary = tracker.get_turn_file_summary(1)
    if operation == "rename":
        assert summary[str(source)].before == SnapshotStore.content_hash(b"committed content")
        assert summary[str(source)].after is None
    else:
        assert summary[str(source)].before == SnapshotStore.content_hash(b"committed content")
        assert summary[str(source)].after == SnapshotStore.content_hash(
            b"source changed to make copy detection eligible"
        )
    assert summary[str(destination)].before is None
    assert summary[str(destination)].after == SnapshotStore.content_hash(b"committed content")

    tracker.rollback_turns({1})
    assert source.read_bytes() == b"committed content"
    assert destination.exists() is False


@pytest.mark.asyncio
async def test_pipeline_rename_onto_existing_destination_uses_old_destination_bytes(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
) -> None:
    _init_git_repo(tmp_path, git_repo_factory)
    source = tmp_path / "README.md"
    destination = tmp_path / "existing.md"

    def _seed_destination() -> None:
        destination.write_text("old destination", encoding="utf-8")
        subprocess.run(["git", "add", "existing.md"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "destination"], cwd=tmp_path, capture_output=True, check=True)

    await asyncio.to_thread(_seed_destination)
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)

    def _commit() -> None:
        os.replace(source, destination)
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "overwrite"], cwd=tmp_path, capture_output=True, check=True)

    await _finalize_opaque_shell(tracker, tmp_path, _commit)
    summary = tracker.get_turn_file_summary(1)
    assert summary[str(source)].before == SnapshotStore.content_hash(b"committed content")
    assert summary[str(source)].after is None
    assert summary[str(destination)].before == SnapshotStore.content_hash(b"old destination")
    assert summary[str(destination)].after == SnapshotStore.content_hash(b"committed content")

    tracker.rollback_turns({1})
    assert source.read_bytes() == b"committed content"
    assert destination.read_bytes() == b"old destination"


def test_covered_paths_never_probe_old_head_or_flag_truncation(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
) -> None:
    _init_git_repo(tmp_path, git_repo_factory)
    calibrator = GitDiffCalibrator(str(tmp_path), max_snapshot_file_bytes=1)
    calibrator.capture_before()
    target = tmp_path / "README.md"
    target.write_text("new committed value", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "covered"], cwd=tmp_path, capture_output=True, check=True)

    changes = calibrator.detect_implicit_changes({os.path.normpath(str(target))})

    assert changes == []
    assert calibrator.detection_truncated is False


@pytest.mark.parametrize(
    ("tracked", "expected_listings"),
    [
        (False, [("diff", False), ("diff", False), ("ls-files", True)]),
        # Modified tracked files fill the first listing, so the later two never run.
        (True, [("diff", True)]),
    ],
    ids=["untracked", "tracked"],
)
def test_too_many_dirty_files_stop_git_listing_once_past_the_limit(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
    monkeypatch: pytest.MonkeyPatch,
    tracked: bool,
    expected_listings: list[tuple[str, bool]],
) -> None:
    _init_git_repo(tmp_path, git_repo_factory)
    dirty = [tmp_path / f"dirty_{index:04}.txt" for index in range(git_calibrator.MAX_DIRTY_FILES + 100)]
    for path in dirty:
        path.write_text("new\n", encoding="utf-8")
    if tracked:
        subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "dirty"], cwd=tmp_path, capture_output=True, check=True)
        for path in dirty:
            path.write_text("changed\n", encoding="utf-8")
    original_stream = git_calibrator._run_git_nul_stream
    listings: list[tuple[str, bool]] = []

    def recording_stream(
        root: str, args: list[str], *, timeout: float, consume: Callable[[bytes], bool]
    ) -> git_state._StreamResult:
        stream = original_stream(root, args, timeout=timeout, consume=consume)
        listings.append((args[0], stream.stopped))
        return stream

    monkeypatch.setattr(git_calibrator, "_run_git_nul_stream", recording_stream)
    calibrator = GitDiffCalibrator(str(tmp_path))

    assert calibrator.capture_before() == []
    assert calibrator.detection_truncated is True
    assert listings == expected_listings
    assert len(calibrator._before_paths) == git_calibrator.MAX_DIRTY_FILES + 1


@pytest.mark.asyncio
async def test_pipeline_commit_then_dirty_keeps_pre_command_head_bytes(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
) -> None:
    _init_git_repo(tmp_path, git_repo_factory)
    target = tmp_path / "README.md"
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)

    def _commit_then_dirty() -> None:
        target.write_text("committed endpoint", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "generated"], cwd=tmp_path, capture_output=True, check=True)
        target.write_text("dirty endpoint", encoding="utf-8")

    await _finalize_opaque_shell(tracker, tmp_path, _commit_then_dirty)
    summary = tracker.get_turn_file_summary(1)[str(target)]
    assert summary.before == SnapshotStore.content_hash(b"committed content")
    assert summary.after == SnapshotStore.content_hash(b"dirty endpoint")
    tracker.rollback_turns({1})
    assert target.read_bytes() == b"committed content"


@pytest.mark.asyncio
async def test_file_edit_followed_by_shell_change_appends_latest_endpoint(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
) -> None:
    _init_git_repo(tmp_path, git_repo_factory)
    target = tmp_path / "README.md"
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    edit = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "edit")
    assert edit is not None
    target.write_text("tool edit", encoding="utf-8")
    tracker.record_after(edit)

    await _finalize_opaque_shell(
        tracker,
        tmp_path,
        lambda: target.write_text("shell endpoint", encoding="utf-8"),
        call_id="shell",
    )

    turn = tracker.get_turn_mutations(1)
    assert turn is not None
    assert [row.tool_call_id for row in turn.mutations if row.path == str(target)] == ["edit", "shell"]
    assert tracker.get_turn_file_summary(1)[str(target)].after == SnapshotStore.content_hash(b"shell endpoint")


@pytest.mark.asyncio
async def test_targeted_chmod_records_content_net_zero_and_rollback_keeps_mode(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
) -> None:
    from chrys.service.mutations.pipeline import finalize_mutation_tracking, prepare_mutation_tracking

    _init_git_repo(tmp_path, git_repo_factory)
    target = tmp_path / "README.md"
    original_mode = target.stat().st_mode
    changed_mode = original_mode ^ 0o100
    target.chmod(changed_mode)
    if target.stat().st_mode == original_mode:
        pytest.skip("filesystem does not support executable-bit changes")
    target.chmod(original_mode)
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    context = await prepare_mutation_tracking(
        tracker,
        "shell",
        {"command": "chmod +x README.md", "workdir": str(tmp_path)},
        "chmod",
        True,
        str(tmp_path),
    )
    target.chmod(changed_mode)
    result = await finalize_mutation_tracking(tracker, context, "chmod")

    turn = tracker.get_turn_mutations(1)
    assert turn is not None
    rows = [row for row in turn.mutations if row.path == str(target)]
    assert len(rows) == 1
    assert rows[0].operation is MutationOp.MODIFY
    assert tracker.get_turn_file_summary(1)[str(target)].is_net_zero is True
    assert result.shell_snapshots is not None
    assert result.shell_snapshots[str(target)].bytes_changed is False
    tracker.rollback_turns({1})
    assert target.stat().st_mode == changed_mode


@pytest.mark.asyncio
@pytest.mark.parametrize("delta_result", [GitDeltaResult((), failed=True), GitDeltaResult((), truncated=True)])
async def test_pipeline_delta_problem_sets_turn_warning(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
    monkeypatch: pytest.MonkeyPatch,
    delta_result: GitDeltaResult,
) -> None:
    _init_git_repo(tmp_path, git_repo_factory)
    target = tmp_path / "README.md"
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    monkeypatch.setattr(git_calibrator, "read_git_head_delta", lambda *args, **kwargs: delta_result)

    def _commit() -> None:
        target.write_text("new", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "generated"], cwd=tmp_path, capture_output=True, check=True)

    await _finalize_opaque_shell(tracker, tmp_path, _commit)
    turn = tracker.get_turn_mutations(1)
    assert turn is not None
    assert turn.detection_truncated is True


@pytest.mark.asyncio
async def test_oversized_old_blob_is_never_read(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_git_repo(tmp_path, git_repo_factory)
    target = tmp_path / "README.md"
    tracker = MutationTracker(SnapshotStore(tmp_path / "session", policy=SnapshotPolicy(max_file_bytes=1)))
    tracker.start_turn(1)

    def _unexpected_read(*args: object, **kwargs: object) -> bytes:
        raise AssertionError("oversized blob was read")

    monkeypatch.setattr(git_calibrator, "read_git_blob", _unexpected_read)

    def _commit() -> None:
        target.write_text("new", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "generated"], cwd=tmp_path, capture_output=True, check=True)

    await _finalize_opaque_shell(tracker, tmp_path, _commit)
    turn = tracker.get_turn_mutations(1)
    assert turn is not None and turn.detection_truncated is False
    rows = [row for row in turn.mutations if row.path == str(target)]
    assert len(rows) == 1
    assert rows[0].before_hash is None
    assert rows[0].before_skip is SnapshotSkipReason.TOO_LARGE


def test_turn_mutations_detection_truncated_round_trip() -> None:
    from chrys.service.mutations.types import TurnMutations

    restored = TurnMutations.from_dict({"turn_id": 1, "mutations": []})
    assert restored.detection_truncated is False
    restored.detection_truncated = True
    assert TurnMutations.from_dict(restored.to_dict()).detection_truncated is True
