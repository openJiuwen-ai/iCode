# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from chrys.foundation.reporting.frames import KIND_WORKSPACE_FILE, KIND_WORKSPACE_MANIFEST, Frame
from chrys.foundation.reporting.wal import DurableWAL
from chrys.service.reporting.cursors import SessionCursor
from chrys.service.reporting.workspace import REMOTE_KIND_GIT, SCHEMA_NAME, WorkspaceCapture


def _records(manifest: dict[str, object], section: str) -> list[dict[str, object]]:
    sections = manifest["sections"]
    assert isinstance(sections, dict)
    records = sections[section]
    assert isinstance(records, list)
    return records


def test_a_dirty_tree_becomes_a_manifest_plus_one_block_per_file(
    tmp_path: Path,
    queue: DurableWAL,
    queued: Callable[[DurableWAL], list[Frame]],
    git_repo_factory: Callable[[Path], Path],
) -> None:
    repo = git_repo_factory(tmp_path / "repo")
    (repo / "README.md").write_text("changed content", encoding="utf-8")
    (repo / "notes.txt").write_text("untracked content", encoding="utf-8")
    (repo / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    (repo / "secret.txt").write_text("never send me", encoding="utf-8")
    cursor = SessionCursor()

    result = WorkspaceCapture(queue).capture("session-1", str(repo), cursor, head_sha="head-sha")

    frames = queued(queue)
    assert frames[-1].header.kind == KIND_WORKSPACE_MANIFEST
    assert all(frame.header.kind == KIND_WORKSPACE_FILE for frame in frames[:-1])
    manifest = json.loads(frames[-1].payload)
    assert manifest["schema"] == SCHEMA_NAME
    assert manifest["repository"] == {"identity": "", "headSha": "head-sha"}
    assert manifest["capture"]["remoteKind"] == REMOTE_KIND_GIT
    assert manifest["capture"]["complete"] is True
    assert result.manifest_sha256 == hashlib.sha256(frames[-1].payload).hexdigest()
    assert result.units == len(frames)

    worktree = {record["path"]: record for record in _records(manifest, "worktree")}
    assert worktree["README.md"]["content"]["sha256"] == hashlib.sha256(b"changed content").hexdigest()
    untracked = {record["path"] for record in _records(manifest, "untracked")}
    assert {"notes.txt", ".gitignore"} <= untracked
    assert manifest["ignored"] == {"count": 1, "truncated": False, "entries": ["secret.txt"]}

    sent = {frame.payload for frame in frames[:-1]}
    assert b"never send me" not in sent
    assert b"untracked content" in sent


def test_unchanged_content_is_not_sent_twice(
    tmp_path: Path,
    queue: DurableWAL,
    queued: Callable[[DurableWAL], list[Frame]],
    git_repo_factory: Callable[[Path], Path],
) -> None:
    repo = git_repo_factory(tmp_path / "repo")
    (repo / "notes.txt").write_text("untracked content", encoding="utf-8")
    cursor = SessionCursor()
    capture = WorkspaceCapture(queue)
    first = capture.capture("session-1", str(repo), cursor)
    cursor.workspace_file_shas = list(first.file_shas)

    second = capture.capture("session-1", str(repo), cursor)

    assert second.file_shas == []
    assert second.units == 1
    assert len(queued(queue)) == first.units + 1


def test_the_signature_changes_only_when_the_tree_does(
    tmp_path: Path, queue: DurableWAL, git_repo_factory: Callable[[Path], Path]
) -> None:
    repo = git_repo_factory(tmp_path / "repo")
    capture = WorkspaceCapture(queue)
    baseline = capture.signature(str(repo), head_sha="head-sha")

    assert capture.signature(str(repo), head_sha="head-sha") == baseline

    (repo / "notes.txt").write_text("new content", encoding="utf-8")

    assert capture.signature(str(repo), head_sha="head-sha") != baseline


def test_the_manifest_has_the_three_sections_even_when_they_are_empty(
    tmp_path: Path,
    queue: DurableWAL,
    queued: Callable[[DurableWAL], list[Frame]],
    git_repo_factory: Callable[[Path], Path],
) -> None:
    repo = git_repo_factory(tmp_path / "repo")

    WorkspaceCapture(queue).capture("session-1", str(repo), SessionCursor())

    manifest = json.loads(queued(queue)[-1].payload)
    assert set(manifest["sections"]) == {"staged", "worktree", "untracked"}
    assert manifest["omitted"] == []
