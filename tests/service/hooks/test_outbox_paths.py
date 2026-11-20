# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A pending job's id is the name of its file, never somewhere its contents point to."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chrys.service.hooks.outbox import Outbox


@pytest.mark.parametrize(
    ("stem", "job_id"),
    [
        ("0123456789abcdef", "../../escaped"),
        ("0123456789abcdef", "0123456789abcdef/../x"),
        ("0123456789abcdef", "ffffffffffffffff"),
        ("abc", "abc"),
        ("0123456789ABCDEF", "0123456789ABCDEF"),
    ],
)
def test_invalid_or_mismatched_pending_job_is_ignored(tmp_path: Path, stem: str, job_id: str) -> None:
    root = tmp_path / "outbox"
    (root / "pending").mkdir(parents=True)
    pending = root / "pending" / f"{stem}.json"
    pending.write_text(
        json.dumps({"job_id": job_id, "hook_id": "h", "event": "e", "payload": {}, "created_at": ""}),
        encoding="utf-8",
    )
    outbox = Outbox(root)
    stale = outbox.list_stale_pending(retry_age_seconds=0)
    assert stale == []
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == [pending]


def test_a_job_named_after_its_file_still_recovers(tmp_path: Path) -> None:
    root = tmp_path / "outbox"
    (root / "pending").mkdir(parents=True)
    (root / "pending" / "0123456789abcdef.json").write_text(
        json.dumps({"job_id": "0123456789abcdef", "hook_id": "h", "event": "e", "payload": {}, "created_at": ""}),
        encoding="utf-8",
    )
    outbox = Outbox(root)
    [job] = outbox.list_stale_pending(retry_age_seconds=0)
    outbox.force_fail(job, reason="test")
    assert (root / "failed" / "0123456789abcdef.json").exists()
    assert not (root / "pending" / "0123456789abcdef.json").exists()


def test_outbox_rejects_invalid_ids_before_writing_job_files(tmp_path: Path) -> None:
    outbox = Outbox(tmp_path / "outbox")
    job = outbox.write_pending(hook_id="h", event="e", payload={})
    pending = outbox.root / "pending" / f"{job.job_id}.json"
    original = pending.read_bytes()
    job.job_id = "invalid"

    with pytest.raises(ValueError, match="Invalid outbox job ID"):
        outbox.mark_started(job)
    with pytest.raises(ValueError, match="Invalid outbox job ID"):
        outbox.mark_done(job, exit_code=0)

    assert pending.read_bytes() == original
    assert list(outbox.root.rglob("*.json")) == [pending]
