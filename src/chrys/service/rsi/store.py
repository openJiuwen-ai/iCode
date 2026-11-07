# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Durable RSI task store (JSON per task under the tasks root)."""

from __future__ import annotations

import json
import os
import shutil
import stat
import threading
from collections.abc import Callable
from pathlib import Path

from chrys.foundation.platform.files import atomic_write_text
from chrys.service.rsi.errors import RsiTaskNotFound, RsiTaskStateConflict
from chrys.service.rsi.models import RsiTask, TaskStatus, utcnow_iso
from chrys.service.rsi.paths import task_dir
from chrys.service.rsi.workdir_seed import release_workdir_seeds_under

StatusHook = Callable[[RsiTask, str | None], None]


def _force_remove(func, path, exc) -> None:
    if func not in (os.unlink, os.rmdir):
        raise exc
    os.chmod(path, stat.S_IWRITE)
    func(path)


class RsiTaskStore:
    """Filesystem-backed task CRUD with a status state machine."""

    def __init__(self, tasks_root: Path) -> None:
        self.tasks_root = Path(tasks_root)
        self.tasks_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._status_hooks: list[StatusHook] = []

    def add_status_hook(self, hook: StatusHook) -> None:
        self._status_hooks.append(hook)

    def create(self, task: RsiTask) -> RsiTask:
        with self._lock:
            directory = task_dir(self.tasks_root, task.task_id)
            if directory.exists():
                raise RsiTaskStateConflict(f"task already exists: {task.task_id}")
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "run").mkdir(parents=True, exist_ok=True)
            if not task.run_dir:
                task.run_dir = str((directory / "run").resolve())
            task.updated_at = utcnow_iso()
            self._write_locked(task)
            return task

    def get(self, task_id: str) -> RsiTask:
        with self._lock:
            return self._read_locked(task_id)

    def list_tasks(self, *, scenario: str | None = None) -> list[RsiTask]:
        with self._lock:
            tasks: list[RsiTask] = []
            if not self.tasks_root.is_dir():
                return tasks
            for entry in sorted(self.tasks_root.iterdir()):
                if not entry.is_dir():
                    continue
                path = entry / "task.json"
                if not path.is_file():
                    continue
                try:
                    task = self._load_path(path)
                except OSError, ValueError, TypeError, json.JSONDecodeError, KeyError:
                    continue
                if scenario and task.scenario != scenario:
                    continue
                tasks.append(task)
            return tasks

    def delete(self, task_id: str) -> None:
        with self._lock:
            task = self._read_locked(task_id)
            if task.status not in {
                TaskStatus.CREATED.value,
                TaskStatus.COMPLETED.value,
                TaskStatus.FAILED.value,
                TaskStatus.TERMINATED.value,
            }:
                raise RsiTaskStateConflict(f"cannot delete task in status {task.status}")
            directory = task_dir(self.tasks_root, task_id)
            release_workdir_seeds_under(directory)
            shutil.rmtree(directory, onexc=_force_remove)

    def update_status(
        self,
        task_id: str,
        allowed_from: list[str],
        new_status: str,
        *,
        cause: str = "",
        failure_reason: str | None = None,
    ) -> RsiTask:
        with self._lock:
            task = self._read_locked(task_id)
            previous = task.status
            if previous not in allowed_from:
                raise RsiTaskStateConflict(
                    f"task {task_id}: status {previous} cannot transition to {new_status} (cause={cause})"
                )
            task.status = str(new_status)
            task.updated_at = utcnow_iso()
            task.status_history.append(
                {
                    "from": previous,
                    "to": task.status,
                    "at": task.updated_at,
                    "cause": cause,
                }
            )
            if failure_reason is not None:
                task.failure_reason = failure_reason
            self._write_locked(task)
            hooks = list(self._status_hooks)
            snapshot = RsiTask.from_dict(task.to_dict())
        for hook in hooks:
            hook(snapshot, previous)
        return snapshot

    def save(self, task: RsiTask) -> RsiTask:
        with self._lock:
            task.updated_at = utcnow_iso()
            self._write_locked(task)
            return RsiTask.from_dict(task.to_dict())

    def _read_locked(self, task_id: str) -> RsiTask:
        path = task_dir(self.tasks_root, task_id) / "task.json"
        if not path.is_file():
            raise RsiTaskNotFound(f"task not found: {task_id}")
        try:
            return self._load_path(path)
        except (OSError, ValueError, TypeError, json.JSONDecodeError, KeyError) as exc:
            raise RsiTaskNotFound(f"task not found: {task_id}") from exc

    def _load_path(self, path: Path) -> RsiTask:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("task.json must be an object")
        return RsiTask.from_dict(data)

    def _write_locked(self, task: RsiTask) -> None:
        path = task_dir(self.tasks_root, task.task_id) / "task.json"
        atomic_write_text(path, json.dumps(task.to_dict(), ensure_ascii=True, indent=2) + "\n")
