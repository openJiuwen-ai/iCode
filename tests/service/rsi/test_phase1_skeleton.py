# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Phase 1 RSI service-domain skeleton: store, mock provider, worker, projector."""

from __future__ import annotations

from pathlib import Path

import pytest

from chrys.service.rsi import (
    RsiBadRequest,
    RsiTaskStateConflict,
    TaskStatus,
    build_rsi_service_context,
)
from chrys.service.rsi.paths import resolve_rsi_tasks_root
from tests.support.waiting import wait_for


@pytest.mark.asyncio
async def test_mock_run_completes_and_builds_tree(tmp_path: Path) -> None:
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    created = ctx.create_task(name="demo", max_iterations=2)
    task_id = created["task_id"]
    assert task_id.startswith("rsi-")
    assert created["status"] == TaskStatus.CREATED.value

    started = ctx.start(task_id)
    assert started["status"] == TaskStatus.QUEUED.value
    await ctx.wait_idle()

    detail = ctx.get_task(task_id)
    assert detail["status"] == TaskStatus.COMPLETED.value
    progress = detail["progress"]
    assert progress is not None
    assert progress["iteration"] == 2
    assert progress["total_iterations"] == 2

    tree = ctx.get_tree(task_id)
    node_types = {node["type"] for node in tree["nodes"]}
    assert "ROOT" in node_types
    assert "ADOPTED" in node_types
    assert (tmp_path / "tasks" / task_id / "task.json").is_file()
    assert (tmp_path / "tasks" / task_id / "tree.json").is_file()
    assert (tmp_path / "tasks" / task_id / "run" / "mock_report.json").is_file()


@pytest.mark.asyncio
async def test_status_push_callback_fires(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    def on_status(event_type: str, task_id: str, payload: dict) -> None:
        seen.append((event_type, str(payload.get("status"))))

    ctx = build_rsi_service_context(
        tasks_root=tmp_path / "tasks",
        push_callbacks={"rsi.training.status.changed": on_status},
    )
    task_id = ctx.create_task(name="push")["task_id"]
    ctx.start(task_id)
    await ctx.wait_idle()
    statuses = [status for event, status in seen if event == "rsi.training.status.changed"]
    assert "QUEUED" in statuses
    assert "RUNNING" in statuses
    assert "COMPLETED" in statuses


def test_create_rejects_empty_name(tmp_path: Path) -> None:
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    with pytest.raises(RsiBadRequest):
        ctx.create_task(name="  ")


def test_enqueue_rejects_non_created(tmp_path: Path) -> None:
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="once")["task_id"]
    ctx.store.update_status(task_id, [TaskStatus.CREATED.value], TaskStatus.COMPLETED.value, cause="test")
    with pytest.raises(RsiTaskStateConflict):
        ctx.start(task_id)


def test_resolve_rsi_tasks_root_under_config_dir(tmp_path: Path) -> None:
    root = resolve_rsi_tasks_root(tmp_path / "cfg", create=True)
    assert root == (tmp_path / "cfg" / "rsi" / "tasks").resolve()
    assert root.is_dir()


@pytest.mark.asyncio
async def test_worker_serializes_two_tasks(tmp_path: Path) -> None:
    import asyncio

    from chrys.service.rsi.provider import MockRsiProvider, RsiRunRequest

    gate = asyncio.Event()
    blocked_task_id: dict[str, str] = {}

    class BlockingMock(MockRsiProvider):
        async def run(self, request: RsiRunRequest, *, on_event=None):
            if request.task_id == blocked_task_id.get("id"):
                await gate.wait()
            return await super().run(request, on_event=on_event)

    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks", provider=BlockingMock())
    first = ctx.create_task(name="a", max_iterations=1)["task_id"]
    second = ctx.create_task(name="b", max_iterations=1)["task_id"]
    blocked_task_id["id"] = first
    ctx.start(first)
    ctx.start(second)
    await wait_for(
        lambda: (
            ctx.get_task(first)["status"] == TaskStatus.RUNNING.value
            and ctx.get_task(second)["status"] == TaskStatus.QUEUED.value
        ),
        timeout=5.0,
        description="first running while second queued",
    )
    gate.set()
    await ctx.wait_idle()
    assert ctx.get_task(first)["status"] == TaskStatus.COMPLETED.value
    assert ctx.get_task(second)["status"] == TaskStatus.COMPLETED.value
