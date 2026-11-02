# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RSI task and tree models (Phase 1: profile/skills scenario only)."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

TASK_ID_PREFIX = "rsi-"


def utcnow_iso() -> str:
    """Return an UTC ISO-8601 timestamp with second precision."""
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def generate_task_id() -> str:
    """Generate ``rsi-<uuid8>`` task ids."""
    return f"{TASK_ID_PREFIX}{uuid.uuid4().hex[:8]}"


class TaskStatus(StrEnum):
    """Task status set (aligned with the JiuwenSwarm web contract)."""

    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    PAUSED = "PAUSED"
    TERMINATED = "TERMINATED"

    @classmethod
    def terminal(cls) -> frozenset[TaskStatus]:
        return frozenset({cls.COMPLETED, cls.FAILED, cls.TERMINATED})


class Scenario(StrEnum):
    """Optimization scenario. Phase 1 only materializes PROFILE."""

    PROFILE = "PROFILE"


@dataclass(slots=True)
class RsiTask:
    """Durable task record under ``<tasks_root>/<task_id>/task.json``."""

    task_id: str
    name: str
    scenario: str
    status: str
    created_at: str
    max_iterations: int = 1
    optimization_instruction: str | None = None
    input_file: str | None = None
    model_refs: dict[str, Any] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    run_dir: str = ""
    updated_at: str | None = None
    status_history: list[dict[str, Any]] = field(default_factory=list)
    failure_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RsiTask:
        known = {
            "task_id",
            "name",
            "scenario",
            "status",
            "created_at",
            "max_iterations",
            "optimization_instruction",
            "input_file",
            "model_refs",
            "config",
            "run_dir",
            "updated_at",
            "status_history",
            "failure_reason",
        }
        return cls(**{key: value for key, value in data.items() if key in known})

    def list_projection(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "scenario": self.scenario,
            "status": self.status,
            "created_at": self.created_at,
        }

    def get_projection(
        self,
        *,
        progress: dict[str, Any] | None = None,
        best_artifact: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "name": self.name,
            "scenario": self.scenario,
            "status": self.status,
            "config": {
                "model": {
                    "optimizer": (self.model_refs or {}).get("optimizer"),
                    "tester": (self.model_refs or {}).get("tester"),
                },
                "max_iterations": self.max_iterations,
                "optimization_instruction": self.optimization_instruction,
                "input_file": self.input_file,
            },
            "progress": progress,
            "best_artifact": best_artifact,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.status == TaskStatus.FAILED.value and self.failure_reason:
            payload["failure_reason"] = self.failure_reason
        return payload


@dataclass(slots=True)
class RsiTaskView:
    """Read-only view handed to providers (no store mutation)."""

    task_id: str
    name: str
    scenario: str
    status: str
    max_iterations: int
    optimization_instruction: str | None
    input_file: str | None
    model_refs: dict[str, Any]
    config: dict[str, Any]
    run_dir: str

    @classmethod
    def from_task(cls, task: RsiTask) -> RsiTaskView:
        return cls(
            task_id=task.task_id,
            name=task.name,
            scenario=task.scenario,
            status=task.status,
            max_iterations=task.max_iterations,
            optimization_instruction=task.optimization_instruction,
            input_file=task.input_file,
            model_refs=dict(task.model_refs or {}),
            config=dict(task.config or {}),
            run_dir=task.run_dir,
        )


@dataclass(slots=True)
class RsiTreeNode:
    """One node in the evolution tree projection."""

    node_id: str
    iteration: int
    parent_id: str | None
    type: str
    adopted: bool = False
    score: float | None = None
    description: str | None = None
    failure_reason: str | None = None
    failure_class: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "iteration": self.iteration,
            "parent_id": self.parent_id,
            "type": self.type,
            "adopted": self.adopted,
            "score": self.score,
            "description": self.description,
            "failure_reason": self.failure_reason,
            "failure_class": self.failure_class,
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RsiTreeNode:
        return cls(
            node_id=str(data.get("node_id") or ""),
            iteration=int(data.get("iteration") or 0),
            parent_id=data.get("parent_id"),
            type=str(data.get("type") or ""),
            adopted=bool(data.get("adopted")),
            score=_optional_float(data.get("score")),
            description=_optional_str(data.get("description")),
            failure_reason=_optional_str(data.get("failure_reason")),
            failure_class=_optional_str(data.get("failure_class")),
            extra=dict(data.get("extra") or {}),
        )


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None
