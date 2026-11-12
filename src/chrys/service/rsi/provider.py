# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RSI provider contract and Phase 1 mock implementation."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from chrys.foundation.platform.files import atomic_write_text
from chrys.service.rsi.engine_events import EngineEvent
from chrys.service.rsi.models import RsiTaskView, utcnow_iso

EventSink = Callable[[EngineEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RsiRunRequest:
    """Stable request the worker hands a provider."""

    task_id: str
    output_dir: str
    max_iterations: int
    optimization_instruction: str | None
    model_refs: dict[str, Any]
    resume: bool = False


class RsiProvider(Protocol):
    """Provider boundary: run emits EngineEvents then returns."""

    supports_pause: bool
    supports_resume: bool
    supports_terminate: bool

    async def run(self, request: RsiRunRequest, *, on_event: EventSink | None = None) -> Any: ...

    async def resume(self, request: RsiRunRequest, *, on_event: EventSink | None = None) -> Any: ...


class MockRsiProvider:
    """Deterministic provider that emits a small evolution tree then completes."""

    supports_pause = False
    supports_resume = False
    supports_terminate = False

    def __init__(self, *, baseline: float = 0.4, best_score: float = 0.85) -> None:
        self.baseline = baseline
        self.best_score = best_score

    async def run(self, request: RsiRunRequest, *, on_event: EventSink | None = None) -> dict[str, Any]:
        return await self._emit(request, on_event=on_event)

    async def resume(self, request: RsiRunRequest, *, on_event: EventSink | None = None) -> dict[str, Any]:
        return await self._emit(request, on_event=on_event)

    async def _emit(self, request: RsiRunRequest, *, on_event: EventSink | None) -> dict[str, Any]:
        output = Path(request.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        total = max(1, int(request.max_iterations))
        seq = 0

        async def emit(family: str, kind: str, payload: dict[str, Any]) -> None:
            nonlocal seq
            seq += 1
            if on_event is None:
                return
            await on_event(
                EngineEvent(
                    family=family,
                    kind=kind,
                    task_id=request.task_id,
                    event_id=seq,
                    ts=utcnow_iso(),
                    payload=payload,
                )
            )

        await emit(
            "node",
            "created",
            {
                "node": {
                    "ref": "root",
                    "parent_ref": None,
                    "type": "ROOT",
                    "accepted": True,
                    "score": self.baseline,
                    "summary": "baseline",
                }
            },
        )
        await emit(
            "progress",
            "metric",
            {
                "iteration": 0,
                "total_iterations": total,
                "score": self.baseline,
                "baseline": self.baseline,
            },
        )

        for epoch in range(1, total + 1):
            candidate_ref = f"candidate-{epoch}"
            score = self.baseline + (self.best_score - self.baseline) * (epoch / total)
            accepted = epoch == total
            await emit(
                "node",
                "created",
                {
                    "node": {
                        "ref": candidate_ref,
                        "parent_ref": "root" if epoch == 1 else f"candidate-{epoch - 1}",
                        "type": "ADOPTED" if accepted else "PROVISIONAL",
                        "accepted": accepted,
                        "score": score,
                        "summary": f"mock candidate epoch {epoch}",
                    }
                },
            )
            await emit(
                "node",
                "stage",
                {"node_ref": candidate_ref, "stage": "evaluated", "score": score},
            )
            await emit(
                "progress",
                "metric",
                {
                    "iteration": epoch,
                    "total_iterations": total,
                    "score": score,
                    "baseline": self.baseline,
                },
            )

        report = {
            "task_id": request.task_id,
            "status": "completed",
            "best_score": self.best_score,
            "baseline": self.baseline,
            "provider": "mock",
        }
        atomic_write_text(output / "mock_report.json", json.dumps(report, ensure_ascii=True, indent=2) + "\n")
        return report


def build_request(task: RsiTaskView, *, resume: bool = False) -> RsiRunRequest:
    """Map a task view to a provider request."""
    return RsiRunRequest(
        task_id=task.task_id,
        output_dir=task.run_dir,
        max_iterations=max(1, int(task.max_iterations or 1)),
        optimization_instruction=task.optimization_instruction,
        model_refs=dict(task.model_refs or {}),
        resume=resume,
    )
