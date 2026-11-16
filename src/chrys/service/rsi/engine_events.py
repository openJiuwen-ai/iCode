# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Normalized provider→worker event envelope (not EventBus types)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class EngineEvent:
    """Provider progress/node event consumed by the projector and status sink."""

    family: str
    kind: str
    task_id: str = ""
    event_id: int | None = None
    ts: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def is_progress_metric(self) -> bool:
        return self.family == "progress" and self.kind == "metric"

    @property
    def is_progress_usage(self) -> bool:
        return self.family == "progress" and self.kind == "usage"

    @property
    def is_node_created(self) -> bool:
        return self.family == "node" and self.kind == "created"

    @property
    def is_node_stage(self) -> bool:
        return self.family == "node" and self.kind == "stage"

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "task_id": self.task_id,
            "ts": self.ts,
            "family": self.family,
            "kind": self.kind,
            "payload": dict(self.payload),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EngineEvent:
        return cls(
            family=str(data.get("family") or ""),
            kind=str(data.get("kind") or ""),
            task_id=str(data.get("task_id") or ""),
            event_id=data.get("event_id") if isinstance(data.get("event_id"), int) else None,
            ts=str(data.get("ts") or ""),
            payload=dict(data.get("payload") or {}),
        )
