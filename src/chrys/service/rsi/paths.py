# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RSI workspace path resolution under ``<config_dir>/rsi/tasks``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import atomic_write_text


def resolve_rsi_tasks_root(config_dir: str | Path | None = None, *, create: bool = True) -> Path:
    """Return ``<config_dir>/rsi/tasks``, creating parents when *create* is true.

    RSI tasks are independent of chat/workflow sessions (Phase 0 decision).
    """
    root = Path(config_dir) if config_dir is not None else get_platform().config_dir
    tasks_root = (root / "rsi" / "tasks").expanduser()
    if create:
        tasks_root.mkdir(parents=True, exist_ok=True)
    return tasks_root.resolve()


def task_dir(tasks_root: Path, task_id: str) -> Path:
    """Return the task directory; refuses empty or path-shaped ids."""
    task = str(task_id or "").strip()
    if not task or Path(task).name != task or task in {".", ".."}:
        raise ValueError(f"invalid task_id: {task_id!r}")
    path = (Path(tasks_root) / task).resolve()
    root = Path(tasks_root).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"task path escapes tasks root: {path}") from exc
    return path


def ensure_directory(path: Path) -> Path:
    """Create *path* (and parents) and return the resolved directory."""
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def resolve_under(root: Path, relative: str) -> Path:
    """Resolve a single path segment under *root*; refuse escapes."""
    name = str(relative or "").strip()
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError(f"invalid relative path: {relative!r}")
    path = (Path(root) / name).resolve()
    base = Path(root).resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"path escapes root: {path}") from exc
    return path


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write JSON with ``ensure_ascii=True`` via the platform atomic writer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, ensure_ascii=True, indent=2) + "\n")


def read_json(path: Path) -> Any:
    """Load a UTF-8 JSON file."""
    return json.loads(path.read_text(encoding="utf-8"))
