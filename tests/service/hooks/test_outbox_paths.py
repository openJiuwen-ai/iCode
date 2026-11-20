# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A pending job's id is the name of its file, never somewhere its contents point to."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chrys.service.hooks.outbox import Outbox


@pytest.mark.parametrize("job_id", ["../../escaped", "0123456789abcdef/../x", "ffffffffffffffff"])
def test_a_job_naming_another_file_is_ignored(tmp_path: Path, job_id: str) -> None:
    root = tmp_path / "outbox"
    (root / "pending").mkdir(parents=True)
    (root / "pending" / "0123456789abcdef.json").write_text(
        json.dumps({"job_id": job_id, "hook_id": "h", "event": "e", "payload": {}, "created_at": ""}),
        encoding="utf-8",
    )
    outbox = Outbox(root)
    stale = outbox.list_stale_pending(retry_age_seconds=0)
    assert stale == []
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["0123456789abcdef.json"]


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
