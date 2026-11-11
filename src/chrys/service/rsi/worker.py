# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RSI worker: global concurrency=1 queue over a single provider."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from chrys.service.rsi.engine_events import EngineEvent
from chrys.service.rsi.errors import RsiTaskStateConflict
from chrys.service.rsi.models import RsiTaskView, TaskStatus
from chrys.service.rsi.projector import RsiProjector
from chrys.service.rsi.provider import RsiProvider, build_request
from chrys.service.rsi.store import RsiTaskStore

logger = logging.getLogger(__name__)

PushCallback = Callable[[str, str, dict[str, Any]], Awaitable[None] | None]


class RsiWorker:
    """Single-slot async worker shared across RSI tasks."""

    def __init__(
        self,
        store: RsiTaskStore,
        provider: RsiProvider,
        projector: RsiProjector,
        *,
        push_callbacks: dict[str, PushCallback] | None = None,
    ) -> None:
        self.store = store
        self.provider = provider
        self.projector = projector
        self._push_callbacks = push_callbacks or {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._queued: set[str] = set()
        self._runner: asyncio.Task[None] | None = None
        self._running_task_id: str | None = None
        self._execution: dict[str, asyncio.Task[Any]] = {}

    def holds(self, task_id: str) -> bool:
        """True while this worker has *task_id* queued or running."""
        return task_id in self._queued or task_id == self._running_task_id

    def enqueue(self, task_id: str) -> str:
        """Transition CREATED→QUEUED and ensure the runner is alive.

        A task already QUEUED that this worker does not hold (left by a process
        that exited) is queued again.
        """
        task = self.store.get(task_id)
        if task.status == TaskStatus.QUEUED.value:
            if not self.holds(task_id):
                self._queued.add(task_id)
                self._queue.put_nowait(task_id)
            self._ensure_runner()
            return TaskStatus.QUEUED.value
        if task.status != TaskStatus.CREATED.value:
            raise RsiTaskStateConflict(f"status {task.status} cannot enqueue")
        self.store.update_status(
            task_id,
            [TaskStatus.CREATED.value],
            TaskStatus.QUEUED.value,
            cause="enqueue",
        )
        self._queued.add(task_id)
        self._queue.put_nowait(task_id)
        self._ensure_runner()
        return TaskStatus.QUEUED.value

    def terminate(self, task_id: str) -> str:
        task = self.store.get(task_id)
        if task.status == TaskStatus.QUEUED.value:
            self._dequeue(task_id)
            result = self.store.update_status(
                task_id,
                [TaskStatus.QUEUED.value],
                TaskStatus.TERMINATED.value,
                cause="terminate",
            )
            return result.status
        if task.status == TaskStatus.RUNNING.value:
            running = self._execution.get(task_id)
            if running is not None and not running.done():
                running.cancel()
            return task.status
        if task.status in {TaskStatus.CREATED.value, TaskStatus.PAUSED.value}:
            result = self.store.update_status(
                task_id,
                [task.status],
                TaskStatus.TERMINATED.value,
                cause="terminate",
            )
            return result.status
        raise RsiTaskStateConflict(f"status {task.status} cannot terminate")

    async def drain(self) -> None:
        """Wait until the queue and the active run finish (tests / CLI)."""
        await self._queue.join()
        runner = self._runner
        if runner is not None:
            # Give the runner one more cycle to exit when the queue is empty.
            await asyncio.sleep(0)
            if self._running_task_id is None and self._queue.empty():
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await runner
                self._runner = None

    def _ensure_runner(self) -> None:
        if self._runner is not None and not self._runner.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise RsiTaskStateConflict("RSI worker requires a running event loop to enqueue") from exc
        self._runner = loop.create_task(self._run_loop(), name="chrys-rsi-worker")

    async def _run_loop(self) -> None:
        # One task's failure must not strand the tasks queued behind it.
        while True:
            task_id = await self._queue.get()
            try:
                await self._run_one(task_id)
            except Exception:
                logger.exception("RSI worker could not run %s", task_id)
            finally:
                self._queue.task_done()

    async def _run_one(self, task_id: str) -> None:
        self._queued.discard(task_id)
        task = self.store.get(task_id)
        if task.status != TaskStatus.QUEUED.value:
            return
        self._running_task_id = task_id
        try:
            await self._execute(task_id)
        finally:
            self._execution.pop(task_id, None)
            self._running_task_id = None

    async def _execute(self, task_id: str) -> None:
        self.store.update_status(
            task_id,
            [TaskStatus.QUEUED.value],
            TaskStatus.RUNNING.value,
            cause="start",
        )
        self.projector.register_root(task_id)
        view = RsiTaskView.from_task(self.store.get(task_id))
        request = build_request(view, resume=False)

        async def on_event(event: EngineEvent) -> None:
            await self._consume_event(event)

        run_task = asyncio.create_task(self.provider.run(request, on_event=on_event), name=f"chrys-rsi-run-{task_id}")
        self._execution[task_id] = run_task
        try:
            await run_task
        except asyncio.CancelledError:
            self.store.update_status(
                task_id,
                [TaskStatus.RUNNING.value],
                TaskStatus.TERMINATED.value,
                cause="cancelled",
            )
            current = asyncio.current_task()
            # terminate() cancels only the run task; the worker goes on to the next one.
            if current is not None and current.cancelling():
                raise
        except Exception as exc:
            logger.exception("RSI provider failed for %s", task_id)
            self.store.update_status(
                task_id,
                [TaskStatus.RUNNING.value],
                TaskStatus.FAILED.value,
                cause="provider_error",
                failure_reason=str(exc) or type(exc).__name__,
            )
        else:
            current = self.store.get(task_id)
            if current.status == TaskStatus.RUNNING.value:
                self.store.update_status(
                    task_id,
                    [TaskStatus.RUNNING.value],
                    TaskStatus.COMPLETED.value,
                    cause="provider_done",
                )

    async def _consume_event(self, event: EngineEvent) -> None:
        task_id = event.task_id
        if event.is_node_created:
            node = self.projector.on_node_created(task_id, event.payload)
            if node is not None:
                await self._push("rsi.training.tree.delta", task_id, {"node": node.to_dict()})
        elif event.is_progress_metric:
            progress = self.projector.on_progress_metric(task_id, event.payload)
            await self._push("rsi.training.progress", task_id, progress)
        elif event.is_node_stage:
            await self._push(
                "rsi.training.tree.delta",
                task_id,
                {"stage": event.payload},
            )

    async def _push(self, event_type: str, task_id: str, payload: dict[str, Any]) -> None:
        callback = self._push_callbacks.get(event_type)
        if callback is None:
            return
        result = callback(event_type, task_id, payload)
        if result is not None:
            await result

    def _dequeue(self, task_id: str) -> None:
        self._queued.discard(task_id)
        pending: list[str] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item != task_id:
                pending.append(item)
            self._queue.task_done()
        for item in pending:
            self._queue.put_nowait(item)
