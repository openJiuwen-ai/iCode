# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the detached hook worker."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from chrys.service.hooks import detached_worker
from chrys.service.hooks.outbox import DONE, FAILED, PENDING, Outbox, OutboxJob


def _write_invocation(
    tmp_path: Path,
    *,
    outbox_root: Path | None = None,
    job: OutboxJob | dict[str, object] | None = None,
) -> tuple[Path, Path, Path]:
    invocation_path = tmp_path / "invocation.json"
    payload_path = tmp_path / "payload.json"
    result_path = tmp_path / "result.json"
    payload_path.write_text("payload", encoding="utf-8")
    result_path.write_text("result", encoding="utf-8")
    raw_job: object = asdict(job) if isinstance(job, OutboxJob) else job
    invocation_path.write_text(
        json.dumps(
            {
                "cmd": ["hook", "arg"],
                "cwd": str(tmp_path),
                "env": {"TOKEN": "test"},
                "result_path": str(result_path),
                "payload_path": str(payload_path),
                "outbox_root": str(outbox_root) if outbox_root is not None else "",
                "job": raw_job,
            }
        ),
        encoding="utf-8",
    )
    return invocation_path, payload_path, result_path


def _job_file(root: Path, state: str, job: OutboxJob) -> dict[str, Any]:
    return json.loads((root / state / f"{job.job_id}.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_run_child_uses_hidden_console_kwargs_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    import chrys.foundation.platform.process as process_mod

    seen: dict[str, object] = {}

    class _Proc:
        async def wait(self) -> int:
            return 0

    async def _fake_create_subprocess_exec(*cmd: str, **kwargs: object) -> _Proc:
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(detached_worker.sys, "platform", "win32")
    monkeypatch.setattr(process_mod, "windows_hidden_subprocess_kwargs", lambda: {"creationflags": 16})
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    exit_code = await detached_worker._run_child(["cmd.exe", "/c", "echo ok"], cwd=".", env={})

    assert exit_code == 0
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["creationflags"] == 16


@pytest.mark.asyncio
async def test_main_marks_durable_job_done_and_cleans_temp_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox_root = tmp_path / "outbox"
    job = Outbox(outbox_root).write_pending(hook_id="done", event="after_turn", payload={"x": 1}, hook_source="project")
    invocation, payload, result = _write_invocation(tmp_path, outbox_root=outbox_root, job=job)

    async def _success(_cmd: list[str], *, cwd: str, env: dict[str, str]) -> int:
        assert cwd == str(tmp_path)
        assert env == {"TOKEN": "test"}
        return 0

    monkeypatch.setattr(detached_worker, "_run_child", _success)

    assert await detached_worker._main(invocation) == 0
    assert _job_file(outbox_root, DONE, job)["state"] == DONE
    assert _job_file(outbox_root, DONE, job)["hook_source"] == "workdir"
    assert not (outbox_root / PENDING / f"{job.job_id}.json").exists()
    assert all(not path.exists() for path in (invocation, payload, result))


@pytest.mark.asyncio
async def test_detached_worker_skips_a_job_completed_after_handoff(tmp_path, monkeypatch):
    outbox = Outbox(tmp_path / "outbox")
    job = outbox.write_pending(hook_id="done", event="after_turn", payload={})
    invocation, payload, result = _write_invocation(tmp_path, outbox_root=outbox.root, job=job)
    pending = outbox.root / PENDING / f"{job.job_id}.json"
    pending_bytes = pending.read_bytes()
    outbox.mark_done(job, exit_code=0)
    pending.write_bytes(pending_bytes)
    terminal = outbox.root / DONE / f"{job.job_id}.json"
    original = terminal.read_bytes()

    async def run_child(*args, **kwargs):
        pytest.fail("worker re-executed a terminal job")

    monkeypatch.setattr(detached_worker, "_run_child", run_child)
    assert await detached_worker._main(invocation) == 0
    assert terminal.read_bytes() == original
    assert pending.read_bytes() == pending_bytes
    assert all(not path.exists() for path in (invocation, payload, result))


async def test_main_launches_the_hook_before_recreating_the_outbox(tmp_path, monkeypatch):
    root = tmp_path / "missing-outbox"
    job = OutboxJob(job_id="0123456789abcdef", hook_id="done", event="after_turn", payload={}, created_at="")
    invocation, _, _ = _write_invocation(tmp_path, outbox_root=root, job=job)

    async def run_child(*args, **kwargs):
        assert not root.exists()
        return 0

    monkeypatch.setattr(detached_worker, "_run_child", run_child)
    assert await detached_worker._main(invocation) == 0
    assert _job_file(root, DONE, job)["state"] == DONE


@pytest.mark.asyncio
async def test_main_marks_nonzero_exit_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outbox_root = tmp_path / "outbox"
    job = Outbox(outbox_root).write_pending(hook_id="failed", event="after_turn", payload={})
    invocation, _payload, _result = _write_invocation(tmp_path, outbox_root=outbox_root, job=job)

    async def _nonzero(_cmd: list[str], *, cwd: str, env: dict[str, str]) -> int:
        del cwd, env
        return 7

    monkeypatch.setattr(detached_worker, "_run_child", _nonzero)

    assert await detached_worker._main(invocation) == 1
    failed = _job_file(outbox_root, FAILED, job)
    assert failed["exit_code"] == 7
    assert failed["error"] == "exit_code=7"


@pytest.mark.asyncio
async def test_main_marks_launch_failure_without_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outbox_root = tmp_path / "outbox"
    job = Outbox(outbox_root).write_pending(hook_id="missing", event="after_turn", payload={})
    invocation, _payload, _result = _write_invocation(tmp_path, outbox_root=outbox_root, job=job)

    async def _launch_failed(_cmd: list[str], *, cwd: str, env: dict[str, str]) -> None:
        del cwd, env

    monkeypatch.setattr(detached_worker, "_run_child", _launch_failed)

    assert await detached_worker._main(invocation) == 1
    failed = _job_file(outbox_root, FAILED, job)
    assert failed["exit_code"] is None
    assert failed["error"] == "launch failed"


@pytest.mark.asyncio
async def test_main_cleans_temp_files_when_child_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    invocation, payload, result = _write_invocation(tmp_path)

    async def _failed(_cmd: list[str], *, cwd: str, env: dict[str, str]) -> int:
        del cwd, env
        return 2

    monkeypatch.setattr(detached_worker, "_run_child", _failed)

    assert await detached_worker._main(invocation) == 1
    assert all(not path.exists() for path in (invocation, payload, result))


@pytest.mark.asyncio
async def test_main_rejects_non_object_json_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    invocation = tmp_path / "invocation.json"
    invocation.write_text("[]", encoding="utf-8")

    assert await detached_worker._main(invocation) == 1
    assert "must be a JSON object" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_main_reports_unreadable_invocation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    missing = tmp_path / "missing.json"

    assert await detached_worker._main(missing) == 1
    assert "failed to read detached hook invocation" in capsys.readouterr().err


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job",
    [
        {"unexpected": "field"},
        {"job_id": "invalid", "hook_id": "h", "event": "after_turn", "payload": {}, "created_at": ""},
    ],
)
async def test_main_tolerates_malformed_outbox_job_dict(
    tmp_path: Path,
    job: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    outbox_root = tmp_path / "outbox"
    Outbox(outbox_root)
    invocation, _payload, _result = _write_invocation(
        tmp_path,
        outbox_root=outbox_root,
        job=job,
    )

    async def _success(_cmd: list[str], *, cwd: str, env: dict[str, str]) -> int:
        del cwd, env
        return 0

    monkeypatch.setattr(detached_worker, "_run_child", _success)

    assert await detached_worker._main(invocation) == 0
    assert "failed to load detached hook outbox job" in capsys.readouterr().err
    assert not list((outbox_root / DONE).glob("*.json"))
    assert not list((outbox_root / FAILED).glob("*.json"))


def test_main_requires_exactly_one_invocation_argument(capsys: pytest.CaptureFixture[str]) -> None:
    assert detached_worker.main([]) == 2
    assert "Usage: python -m chrys.service.hooks.detached_worker" in capsys.readouterr().err
