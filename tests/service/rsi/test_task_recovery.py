# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tasks left behind by a killed command or a damaged task.json stay removable."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chrys.service.rsi import RsiBadRequest, RsiTaskCorrupt, TaskStatus, build_rsi_service_context
from chrys.service.rsi.provider import MockRsiProvider, RsiRunRequest
from tests.support.waiting import wait_for


@pytest.mark.parametrize("left_status", [TaskStatus.RUNNING.value, TaskStatus.QUEUED.value])
def test_delete_removes_a_task_a_killed_process_left_running(tmp_path: Path, left_status: str) -> None:
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="stale")["task_id"]
    ctx.store.update_status(task_id, [TaskStatus.CREATED.value], left_status, cause="killed")
    ctx.delete_task(task_id)
    assert not (tmp_path / "tasks" / task_id).exists()


@pytest.mark.asyncio
async def test_delete_refuses_a_task_this_process_is_running(tmp_path: Path) -> None:
    gate = asyncio.Event()

    class _Blocking(MockRsiProvider):
        async def run(self, request: RsiRunRequest, *, on_event=None):
            await gate.wait()
            return await super().run(request, on_event=on_event)

    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks", provider=_Blocking())
    running = ctx.create_task(name="running", max_iterations=1)["task_id"]
    queued = ctx.create_task(name="queued", max_iterations=1)["task_id"]
    ctx.start(running)
    ctx.start(queued)
    await wait_for(
        lambda: ctx.get_task(running)["status"] == TaskStatus.RUNNING.value,
        timeout=5.0,
        description="first mock task running",
    )
    with pytest.raises(RsiBadRequest, match="terminate it before delete"):
        ctx.delete_task(running)
    with pytest.raises(RsiBadRequest, match="terminate it before delete"):
        ctx.delete_task(queued)
    gate.set()
    await ctx.wait_idle()
    ctx.delete_task(running)
    ctx.delete_task(queued)
    assert ctx.list_tasks() == []


def test_corrupt_task_json_is_listed_reported_and_deletable(tmp_path: Path) -> None:
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="damaged")["task_id"]
    healthy = ctx.create_task(name="healthy")["task_id"]
    (tmp_path / "tasks" / task_id / "task.json").write_text("{not json", encoding="utf-8")

    listed = {item["task_id"]: item["status"] for item in ctx.list_tasks()}
    assert listed == {task_id: "CORRUPT", healthy: TaskStatus.CREATED.value}
    with pytest.raises(RsiTaskCorrupt, match=r"unreadable task\.json"):
        ctx.get_task(task_id)
    ctx.delete_task(task_id)
    assert not (tmp_path / "tasks" / task_id).exists()
    assert [item["task_id"] for item in ctx.list_tasks()] == [healthy]


@pytest.mark.asyncio
async def test_run_mock_finishes_the_task_before_returning(tmp_path: Path) -> None:
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="mock", max_iterations=1)["task_id"]
    assert await ctx.run_mock(task_id) == TaskStatus.COMPLETED.value
    assert ctx.get_task(task_id)["status"] == TaskStatus.COMPLETED.value
