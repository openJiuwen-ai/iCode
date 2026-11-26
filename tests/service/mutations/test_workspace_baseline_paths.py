# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace baseline path containment, including persisted Windows device paths."""

from __future__ import annotations

import json
import os
from ntpath import relpath as windows_relpath
from pathlib import Path, PureWindowsPath
from unittest.mock import create_autospec

import pytest

from chrys.foundation.models.workspace import WorkingDir, Workspace, WorkspaceConfirmation
from chrys.service.mutations import workspace_changes
from chrys.service.mutations.git_state import GitHeadState, GitStatusResult, canonical_path
from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker


def test_unspecified_link_entry_is_displayed_under_the_users_cwd_alias(tmp_path: Path) -> None:
    from tests.support.symlinks import symlink_or_skip

    real, alias, outside = (tmp_path / name for name in ("repo", "alias", "outside"))
    real.mkdir()
    outside.mkdir()
    symlink_or_skip(alias, real, target_is_directory=True)
    symlink_or_skip(real / "link", outside, target_is_directory=True)
    scope = workspace_changes._ResolvedScope((), display_roots=((canonical_path(str(real)), str(alias)),))

    assert workspace_changes._display_path(str(real / "link"), scope) == str(alias / "link")
    assert not workspace_changes._path_in_git_scope(str(real), str(alias / "link"), (".",))


@pytest.mark.skipif(os.name == "nt", reason="Requires POSIX symlink traversal")
def test_workspace_baseline_resolves_symlink_before_parent_traversal(tmp_path: Path) -> None:
    from tests.support.symlinks import symlink_or_skip

    primary, physical, lexical = tmp_path / "primary", tmp_path / "real" / "refs", tmp_path / "refs"
    link_target = physical.parent / "nested"
    for directory in (primary, physical, lexical, link_target):
        directory.mkdir(parents=True, exist_ok=True)
    alias = tmp_path / "alias"
    symlink_or_skip(alias, link_target, target_is_directory=True)
    related = str(alias / ".." / "refs")
    workspace = Workspace(str(primary), [WorkingDir(related)])

    scope = workspace_changes._resolve_workspace_scope(workspace)
    physical_root = str(physical.resolve())
    assert [root.root for root in scope.roots] == [str(primary.resolve()), physical_root]
    assert canonical_path(related) == physical_root
    assert WorkspaceConfirmation.capture(None, related).resolved_cwd == physical_root
    assert workspace_changes._display_path(str(physical / "file.txt"), scope) == os.path.join(physical_root, "file.txt")


@pytest.mark.parametrize("scope", [(".",), ("child",)])
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"d:\repo\child\kept.txt", True),
        (r"d:\repo\child\deleted.txt", True),
        (r"\\.\nul", False),
        (r"\\.\CON", False),
        (r"c:\repo\child\other.txt", False),
        (r"\\server\share\child\other.txt", False),
        (r"d:\repo-other\child\other.txt", False),
        (r"d:\repo\..\outside.txt", False),
    ],
)
def test_windows_git_scope_rejects_paths_outside_repository(
    monkeypatch: pytest.MonkeyPatch, scope: tuple[str, ...], path: str, expected: bool
) -> None:
    # These are serialized Windows identities, not paths on the test host.
    # Use the real Windows lexical operations without changing the host OS.
    monkeypatch.setattr(os.path, "relpath", create_autospec(os.path.relpath, side_effect=windows_relpath))
    monkeypatch.setattr(workspace_changes, "PurePath", PureWindowsPath)

    assert workspace_changes._path_in_git_scope(r"d:\repo", path, scope) is expected


@pytest.mark.parametrize("scope", [".", "child"])
def test_restore_filters_foreign_mount_and_keeps_comparable_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    child = tmp_path / "child"
    child.mkdir()
    root = canonical_path(str(tmp_path))
    monkeypatch.setattr(
        workspace_changes,
        "resolve_git_root",
        create_autospec(workspace_changes.resolve_git_root, return_value=root),
    )
    kept = os.path.join(root, "child", "kept.txt")
    outside = os.path.join(root, "..", "outside.txt")
    device = r"\\.\nul"
    state = {"exists": True, "mtime_ns": 1, "size": 2, "mode": None, "statuses": ["??"]}
    payload = {
        "version": 1,
        "turn_id": 7,
        "roots": {
            root: {
                "root": root,
                "mode": "git",
                "head": "saved-head",
                "branch": "saved-branch",
                "scope": ["."],
                "files": dict.fromkeys((kept, outside, device), state),
            }
        },
        "degraded": {},
        "pending_safety": [{"text": "Retained changes need review", "cwd": None}],
    }
    original_relpath = os.path.relpath

    def relpath(path: str, start: str = os.curdir) -> str:
        if path == device:
            # Reproduce ntpath's actual foreign-mount error on every host.
            return windows_relpath(path, r"d:\repo")
        return original_relpath(path, start)

    monkeypatch.setattr(os.path, "relpath", create_autospec(original_relpath, side_effect=relpath))
    tracker = WorkspaceChangeTracker()
    tracker.restore(payload, Workspace(str(tmp_path if scope == "." else child), id="ws_app", name="App"))

    baseline = tracker.baseline
    assert baseline is not None
    assert baseline.turn_id == 7
    assert baseline.degraded == {}
    assert baseline.roots[root].scope == (scope,)
    assert baseline.roots[root].head == "saved-head"
    assert set(baseline.roots[root].files) == {kept}
    serialized = tracker.serialize()
    assert serialized is not None
    assert set(serialized["roots"][root]["files"]) == {kept}
    assert tracker.take_pending_notice() == "Retained changes need review"


def test_git_capture_does_not_persist_paths_resolved_to_another_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = canonical_path(str(tmp_path))
    monkeypatch.setattr(
        workspace_changes,
        "resolve_git_root",
        create_autospec(workspace_changes.resolve_git_root, return_value=root),
    )
    device = r"\\.\nul"
    original_lexical = workspace_changes.lexical_display_path
    original_relpath = os.path.relpath

    def lexical(path: str) -> str:
        # Model a Windows path normalizer resolving a reserved device name.
        return device if path == os.path.join(root, "nul") else original_lexical(path)

    def relpath(path: str, start: str = os.curdir) -> str:
        return windows_relpath(path, r"d:\repo") if path == device else original_relpath(path, start)

    monkeypatch.setattr(os.path, "relpath", create_autospec(original_relpath, side_effect=relpath))
    monkeypatch.setattr(
        workspace_changes, "lexical_display_path", create_autospec(original_lexical, side_effect=lexical)
    )
    monkeypatch.setattr(
        workspace_changes,
        "read_git_head",
        create_autospec(workspace_changes.read_git_head, return_value=GitHeadState("head", "branch")),
    )
    monkeypatch.setattr(
        workspace_changes,
        "read_git_status",
        create_autospec(
            workspace_changes.read_git_status,
            return_value=GitStatusResult({"nul": ("??",), "deleted.txt": ("D ",)}),
        ),
    )
    tracker = WorkspaceChangeTracker()
    tracker.apply_retarget(tracker.resolve_retarget(Workspace(str(tmp_path), id="ws_app", name="App")))

    baseline = tracker.capture_baseline(1)

    assert baseline.degraded == {}
    assert set(baseline.roots[root].files) == {os.path.join(root, "deleted.txt")}
    assert baseline.roots[root].files[os.path.join(root, "deleted.txt")].exists is False


@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("has_workspace", [False, True])
def test_git_scope_expansion_drops_the_previous_comparison(tmp_path, git_repo_factory, restore, has_workspace):
    repo = git_repo_factory(tmp_path / "repo")
    child = repo / "pkg"
    child.mkdir()
    tracker = WorkspaceChangeTracker()
    membership = {"id": "workspace", "name": "Team"} if has_workspace else {}
    tracker.apply_retarget(tracker.resolve_retarget(Workspace(str(child), **membership)))
    tracker.capture_baseline(1)
    expanded = Workspace(str(repo), **membership)
    if restore:
        saved = tracker.serialize()
        tracker = WorkspaceChangeTracker()
        tracker.restore(saved, expanded)
    else:
        tracker.apply_retarget(tracker.resolve_retarget(expanded))
    (child / "external.txt").write_text("external change", encoding="utf-8")

    notice = tracker.compute_turn_notice(
        turn_id=2, mutation_tracker=None, agent_turn_id=None, overlap_turn_id=None, cwd=str(repo), max_entries=20
    )

    assert notice is not None and "no comparable previous baseline" in notice
    assert "external.txt" not in notice
    assert "comparable_scope" not in json.dumps(tracker.serialize())


def test_scan_reopen_compares_an_empty_v1_snapshot(tmp_path: Path) -> None:
    workspace = Workspace.from_cwd(str(tmp_path))
    tracker = WorkspaceChangeTracker()
    tracker.apply_retarget(tracker.resolve_retarget(workspace))
    assert tracker.capture_baseline(1).degraded == {}
    saved = tracker.serialize()
    assert saved is not None and saved["version"] == 1

    restored = WorkspaceChangeTracker()
    restored.restore(saved, workspace)
    notice = restored.compute_turn_notice(
        turn_id=2, mutation_tracker=None, agent_turn_id=None, overlap_turn_id=None, cwd=str(tmp_path), max_entries=20
    )
    assert notice is None

    (tmp_path / "new.txt").write_text("new file with a baseline", encoding="utf-8")
    notice = restored.compute_turn_notice(
        turn_id=2, mutation_tracker=None, agent_turn_id=None, overlap_turn_id=None, cwd=str(tmp_path), max_entries=20
    )
    assert notice is not None
    assert [line for line in notice.splitlines() if line.startswith("- ")] == ['- created: "new.txt"']
    assert "Workspace state unavailable" not in notice
