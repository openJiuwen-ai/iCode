# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Detached hook worker.

This module is launched as ``python -m chrys.service.hooks.detached_worker <invocation>``.
It runs the real hook subprocess after the parent Chrys process has already
detached, then updates durable outbox state and removes temp payload/result
files.  Keeping this logic in a worker is what makes ``delivery: durable`` and
``detach: true`` compatible without a long-lived daemon.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from chrys.service.hooks.outbox import Outbox, OutboxJob, has_terminal_record, is_valid_job_id


async def _run_child(cmd: list[str], *, cwd: str, env: dict[str, str]) -> int | None:
    kwargs: dict[str, Any] = {
        "stdin": asyncio.subprocess.DEVNULL,
        "cwd": cwd,
        "env": env,
    }
    if sys.platform == "win32":
        from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs

        kwargs.update(windows_hidden_subprocess_kwargs())
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            **kwargs,
        )
    except (FileNotFoundError, OSError, TypeError, ValueError) as exc:
        _stderr(f"Error: failed to launch detached hook: {exc}")
        return None
    return await proc.wait()


async def _main(path: Path) -> int:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _stderr(f"Error: failed to read detached hook invocation {path}: {exc}")
        return 1
    if not isinstance(raw, dict):
        _stderr(f"Error: detached hook invocation {path} must be a JSON object")
        return 1

    cmd = _string_list(raw.get("cmd"))
    cwd = str(raw.get("cwd") or "")
    env = _string_dict(raw.get("env"))
    result_path = Path(str(raw.get("result_path") or ""))
    payload_path = Path(str(raw.get("payload_path") or ""))
    outbox_root = str(raw.get("outbox_root") or "")
    job = _load_job(raw.get("job"))

    try:
        root = Path(outbox_root) if outbox_root else None
        if root is not None and job is not None and has_terminal_record(root, job.job_id):
            return 0
        exit_code = await _run_child(cmd, cwd=cwd, env=env)
        if root is not None and job is not None:
            outbox = Outbox(root)
            if exit_code == 0:
                outbox.mark_done(job, exit_code=0)
            else:
                outbox.mark_failed(
                    job,
                    exit_code=exit_code,
                    error="launch failed" if exit_code is None else f"exit_code={exit_code}",
                )
    finally:
        for cleanup_path in (result_path, payload_path, path):
            with contextlib.suppress(OSError):
                cleanup_path.unlink(missing_ok=True)
    return 0 if exit_code == 0 else 1


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _string_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _load_job(value: Any) -> OutboxJob | None:
    if not isinstance(value, dict):
        return None
    try:
        job = OutboxJob(**value)
    except TypeError as exc:
        _stderr(f"Error: failed to load detached hook outbox job: {exc}")
        return None
    if not is_valid_job_id(job.job_id):
        _stderr("Error: failed to load detached hook outbox job: invalid job ID")
        return None
    return job


def _stderr(message: str) -> None:
    sys.stderr.write(message + "\n")


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        _stderr("Usage: python -m chrys.service.hooks.detached_worker <invocation.json>")
        return 2
    return asyncio.run(_main(Path(args[0])))


if __name__ == "__main__":
    raise SystemExit(main())
