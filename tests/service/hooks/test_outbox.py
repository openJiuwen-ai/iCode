# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the durable outbox."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from chrys.service.hooks import outbox as outbox_module
from chrys.service.hooks.outbox import (
    DONE,
    FAILED,
    PENDING,
    Outbox,
)
from tests.support.windows_replace import briefly_locked_rename


def test_creates_subdirs(tmp_path: Path) -> None:
    Outbox(tmp_path / "outbox")
    assert (tmp_path / "outbox" / PENDING).is_dir()
    assert (tmp_path / "outbox" / DONE).is_dir()
    assert (tmp_path / "outbox" / FAILED).is_dir()


def test_pending_write_then_done(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path / "outbox")
    job = outbox.write_pending(hook_id="h1", event="after_turn", payload={"x": 1})
    pending_path = tmp_path / "outbox" / PENDING / f"{job.job_id}.json"
    assert pending_path.exists()

    outbox.mark_started(job)
    outbox.mark_done(job, exit_code=0)
    assert not pending_path.exists()
    done_path = tmp_path / "outbox" / DONE / f"{job.job_id}.json"
    assert done_path.exists()
    data = json.loads(done_path.read_text())
    assert data["state"] == "done"
    assert data["exit_code"] == 0
    assert data["retries"] == 1


def test_mark_started_retries_a_briefly_locked_pending_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outbox = Outbox(tmp_path / "outbox")
    job = outbox.write_pending(hook_id="h1", event="after_turn", payload={"x": 1})
    rename = briefly_locked_rename(monkeypatch, failures=2)

    outbox.mark_started(job)

    assert len(rename.attempts) == 3
    pending_path = tmp_path / "outbox" / PENDING / f"{job.job_id}.json"
    assert json.loads(pending_path.read_text(encoding="utf-8"))["retries"] == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits are not portable on Windows")
def test_outbox_files_are_owner_only(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path / "outbox")
    job = outbox.write_pending(hook_id="h1", event="after_turn", payload={"prompt": "secret"})
    pending_path = tmp_path / "outbox" / PENDING / f"{job.job_id}.json"
    assert stat.S_IMODE(os.stat(pending_path).st_mode) == 0o600

    outbox.mark_done(job, exit_code=0)
    done_path = tmp_path / "outbox" / DONE / f"{job.job_id}.json"
    assert stat.S_IMODE(os.stat(done_path).st_mode) == 0o600


def test_pending_then_failed(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path / "outbox")
    job = outbox.write_pending(hook_id="h", event="session_end", payload={})
    outbox.mark_started(job)
    outbox.mark_failed(job, exit_code=2, error="boom", stderr_tail="x" * 2000)
    failed_path = tmp_path / "outbox" / FAILED / f"{job.job_id}.json"
    data = json.loads(failed_path.read_text())
    assert data["state"] == "failed"
    assert data["exit_code"] == 2
    assert data["error"] == "boom"
    # Tail is capped.
    assert len(data["stderr_tail"]) == 1024


def test_list_stale_pending(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path / "outbox")
    job = outbox.write_pending(hook_id="h", event="session_end", payload={})

    # Fresh: created_at is now, so retry_age_seconds=60 should NOT include it.
    fresh = outbox.list_stale_pending(retry_age_seconds=60.0)
    assert fresh == []

    # Backdate created_at.
    path = tmp_path / "outbox" / PENDING / f"{job.job_id}.json"
    data = json.loads(path.read_text())
    data["created_at"] = "2000-01-01T00:00:00Z"
    path.write_text(json.dumps(data))

    stale = outbox.list_stale_pending(retry_age_seconds=60.0)
    assert len(stale) == 1
    assert stale[0].job_id == job.job_id


@pytest.mark.parametrize("terminal_state", [DONE, FAILED])
@pytest.mark.parametrize("source", ["project", "workdir"])
def test_recovery_discards_pending_after_interrupted_terminal_move(tmp_path, monkeypatch, terminal_state, source):
    outbox = Outbox(tmp_path / "outbox")
    job = outbox.write_pending(hook_id="archive", event="after_turn", payload={})
    pending = outbox.root / PENDING / f"{job.job_id}.json"
    terminal = outbox.root / terminal_state / pending.name
    unlink = Path.unlink

    def interrupt(path, *args, **kwargs):
        if path == pending:
            raise OSError("interrupted after writing the terminal record")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupt)
        with pytest.raises(OSError, match="interrupted"):
            if terminal_state == DONE:
                outbox.mark_done(job, exit_code=0)
            else:
                outbox.mark_failed(job, exit_code=1)
    assert pending.exists()
    outbox_module._write_json_atomic_owner_only(terminal, {**json.loads(terminal.read_bytes()), "hook_source": source})
    original = terminal.read_bytes()

    assert outbox.list_stale_pending(retry_age_seconds=0) == []
    assert not pending.exists()
    assert terminal.read_bytes() == original


@pytest.mark.parametrize("damaged", ["empty", "truncated", "incomplete", "invalid_payload", "wrong_id", "wrong_state"])
def test_recovery_keeps_pending_with_an_invalid_terminal(tmp_path, caplog, damaged):
    outbox = Outbox(tmp_path / "outbox")
    job = outbox.write_pending(hook_id="archive", event="after_turn", payload={})
    pending = outbox.root / PENDING / f"{job.job_id}.json"
    terminal = outbox.root / DONE / pending.name
    original = pending.read_bytes()
    complete = {**json.loads(original), "state": DONE}
    invalid = {
        "empty": "",
        "truncated": '{"job_id":',
        "incomplete": json.dumps({"job_id": job.job_id, "state": DONE}),
        "invalid_payload": json.dumps({**complete, "payload": None}),
        "wrong_id": json.dumps({**complete, "job_id": "unrelated"}),
        "wrong_state": json.dumps({**complete, "state": PENDING}),
    }
    terminal.write_text(invalid[damaged], encoding="utf-8")

    assert outbox.list_stale_pending(retry_age_seconds=0) == [job]
    assert pending.read_bytes() == original
    assert outbox.mark_started(job)
    (warning,) = [record for record in caplog.records if str(terminal) in record.getMessage()]
    assert warning.levelname == "WARNING"


def test_recovery_checks_large_terminal_records(tmp_path, caplog):
    outbox = Outbox(tmp_path / "outbox")
    job = outbox.write_pending(hook_id="archive", event="after_turn", payload={"text": "x" * (16 * 1024 * 1024)})
    pending = outbox.root / PENDING / f"{job.job_id}.json"
    terminal = outbox.root / DONE / pending.name
    outbox_module._write_json_atomic_owner_only(terminal, {**json.loads(pending.read_bytes()), "state": DONE})

    assert outbox.list_stale_pending(retry_age_seconds=0) == []
    assert not pending.exists()
    assert caplog.messages == [f"Removed pending hook record {pending} because a terminal record already exists"]


def test_recovery_keeps_pending_when_terminal_exceeds_read_limit(tmp_path, monkeypatch, caplog):
    from chrys.service.hooks import outbox as outbox_module

    outbox = Outbox(tmp_path / "outbox")
    job = outbox.write_pending(hook_id="archive", event="after_turn", payload={})
    pending = outbox.root / PENDING / f"{job.job_id}.json"
    terminal = outbox.root / DONE / pending.name
    outbox_module._write_json_atomic_owner_only(terminal, {**json.loads(pending.read_bytes()), "state": DONE})
    monkeypatch.setattr(outbox_module, "MAX_TERMINAL_RECORD_BYTES", terminal.stat().st_size - 1)

    assert outbox.list_stale_pending(retry_age_seconds=0) == [job]
    assert pending.exists()
    assert "size limit" in caplog.text


def test_recovery_processes_pending_with_an_unreadable_terminal(tmp_path, monkeypatch, caplog):
    from chrys.service.hooks import outbox as outbox_module

    outbox = Outbox(tmp_path / "outbox")
    job = outbox.write_pending(hook_id="archive", event="after_turn", payload={})
    terminal = outbox.root / DONE / f"{job.job_id}.json"
    open_regular = outbox_module.secure_open_regular_binary

    def unreadable(path):
        if path == terminal:
            raise PermissionError("terminal record cannot be read")
        return open_regular(path)

    monkeypatch.setattr(outbox_module, "secure_open_regular_binary", unreadable)

    assert outbox.list_stale_pending(retry_age_seconds=0) == [job]
    assert outbox.mark_started(job)
    (warning,) = [record for record in caplog.records if str(terminal) in record.getMessage()]
    assert warning.levelname == "WARNING"


def test_unknown_fields_round_trip_through_extra(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path / "outbox")
    job = outbox.write_pending(hook_id="h", event="session_end", payload={})
    path = tmp_path / "outbox" / PENDING / f"{job.job_id}.json"
    data = json.loads(path.read_text())
    data["future_field"] = "from a newer chrys"
    path.write_text(json.dumps(data))

    stale = outbox.list_stale_pending(retry_age_seconds=0.0)
    assert stale[0].extra == {"future_field": "from a newer chrys"}


def test_force_fail(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path / "outbox")
    job = outbox.write_pending(hook_id="h", event="session_end", payload={})
    outbox.force_fail(job, reason="exceeded retries")
    failed_path = tmp_path / "outbox" / FAILED / f"{job.job_id}.json"
    assert failed_path.exists()
    data = json.loads(failed_path.read_text())
    assert data["error"] == "exceeded retries"
