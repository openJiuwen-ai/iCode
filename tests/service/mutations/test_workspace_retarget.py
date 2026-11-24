# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prepared workspace retargeting and safety notice directory identities."""

from __future__ import annotations

import json

import pytest

from chrys.foundation.models.workspace import Workspace
from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware
from chrys.service.mutations import workspace_changes as module
from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker


def _header(cwd):
    return f"Note: relative paths below are against the previous workspace directory {json.dumps(str(cwd), ensure_ascii=True)}."


def _state(tracker):
    return (
        tracker._generation,
        tracker._scope,
        tracker.baseline,
        tracker._pending_boundary_notice,
        tracker._pending_safety,
    )


def test_disabled_scope_resolution_preserves_baseline(tmp_path, monkeypatch):
    workspace = Workspace(primary_cwd=str(tmp_path))
    tracker = WorkspaceChangeTracker()
    tracker.apply_retarget(tracker.resolve_retarget(workspace))
    tracker.capture_baseline(7)
    before = tracker.serialize()

    def fail(*args):
        pytest.fail("disabled scope resolution probed the filesystem")

    monkeypatch.setattr(module, "_resolve_workspace_scope", fail)
    tracker.apply_retarget(tracker.resolve_retarget(workspace, resolve_scope=False))
    assert tracker.serialize() == before


@pytest.mark.parametrize("operation", ["resolve", "restore"])
def test_new_directory_resolution_failure_preserves_all_tracker_state(operation, tmp_path, monkeypatch) -> None:
    tracker = WorkspaceChangeTracker()
    tracker.apply_retarget(tracker.resolve_retarget(Workspace(primary_cwd=str(tmp_path))))
    tracker.capture_baseline(3)
    tracker.queue_safety_notice("sole notice", cwd=str(tmp_path))
    tracker._pending_boundary_notice = "boundary"
    before = _state(tracker)
    payload = tracker.serialize()
    new = str(tmp_path / "new")
    canonical = module.canonical_path

    def fail_new(path):
        if path == new:
            raise UnicodeError("unencodable directory")
        return canonical(path)

    monkeypatch.setattr(module, "canonical_path", fail_new)
    with pytest.raises(UnicodeError, match="unencodable"):
        if operation == "resolve":
            tracker.resolve_retarget(Workspace(primary_cwd=new), resolve_scope=False)
        else:
            tracker.restore(payload, Workspace(primary_cwd=new), resolve_scope=False)
    assert _state(tracker) == before
    assert tracker._pending_safety is before[-1]
    assert tracker.serialize() == payload


def test_apply_retarget_with_pending_and_newly_queued_notices_never_resolves_paths(tmp_path, monkeypatch) -> None:
    old = tmp_path / "old"
    workspace = Workspace(primary_cwd=str(tmp_path / "new"))
    tracker = WorkspaceChangeTracker()
    tracker.apply_retarget(tracker.resolve_retarget(Workspace(primary_cwd=str(tmp_path))))
    tracker.capture_baseline(3)
    tracker.queue_safety_notice("first", cwd=str(old))
    retarget = tracker.resolve_retarget(workspace, resolve_scope=True, freeze_baseline=True)
    tracker.queue_safety_notice("second", cwd=str(old))

    def fail(*args, **kwargs):
        raise AssertionError("filesystem access during apply")

    monkeypatch.setattr(module.os.path, "realpath", fail)
    monkeypatch.setattr(module, "_resolve_workspace_scope", fail)
    monkeypatch.setattr(module, "_reconcile_baseline", fail)
    tracker.apply_retarget(retarget)
    assert tracker.take_pending_notice() == f"{_header(old)}\nfirst\n\n{_header(old)}\nsecond"


@pytest.mark.parametrize("error", [OSError, UnicodeError])
@pytest.mark.parametrize("operation", ["queue", "requeue", "restore"])
def test_notice_resolution_failure_keeps_full_text_and_marks_original_base_once(
    operation, error, tmp_path, monkeypatch
) -> None:
    old = str(tmp_path / "old")
    notice = '- modified: "file.txt"\nkeep all text'
    tracker = WorkspaceChangeTracker()
    if operation == "requeue":
        tracker.queue_safety_notice(notice, cwd=old)
        reminder = SystemReminderMiddleware(file_change_provider=tracker.take_pending_notice)
        reminder.prepare_turn()
        notice = reminder.take_undelivered_file_change()
        assert notice == '- modified: "file.txt"\nkeep all text'
        assert tracker.take_pending_notice() is None
    canonical = module.canonical_path

    def fail_old(path):
        if path == old:
            raise error("bad notice directory")
        return canonical(path)

    monkeypatch.setattr(module, "canonical_path", fail_old)
    if operation == "restore":
        tracker.restore({"version": 1, "pending_safety": [{"text": notice, "cwd": old}]}, resolve_scope=False)
    elif operation == "requeue":
        tracker.requeue_notice(notice, cwd=old)
    else:
        tracker.queue_safety_notice(notice, cwd=old)
    payload = tracker.serialize()
    assert payload["pending_safety"] == [{"text": f"{_header(old)}\n{notice}", "cwd": None}]
    tracker.apply_retarget(tracker.resolve_retarget(Workspace(primary_cwd=str(tmp_path / "new")), resolve_scope=False))
    tracker.apply_retarget(
        tracker.resolve_retarget(Workspace(primary_cwd=str(tmp_path / "another")), resolve_scope=False)
    )
    assert tracker.take_pending_notice() == f"{_header(old)}\n{notice}"
    assert tracker.take_pending_notice() is None


def test_retarget_uses_queue_time_symlink_identity_unlike_re_resolving_the_old_directory(tmp_path) -> None:
    first, second, link = tmp_path / "first", tmp_path / "second", tmp_path / "link"
    first.mkdir()
    second.mkdir()
    try:
        link.symlink_to(first, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable")
    tracker = WorkspaceChangeTracker()
    tracker.queue_safety_notice("unchanged text", cwd=str(link))
    link.unlink()
    link.symlink_to(second, target_is_directory=True)
    tracker.apply_retarget(tracker.resolve_retarget(Workspace(primary_cwd=str(first)), resolve_scope=False))
    assert tracker.take_pending_notice() == "unchanged text"
