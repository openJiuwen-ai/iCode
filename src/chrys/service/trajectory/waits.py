# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``wait.started`` / ``wait.finished`` recording for waits without a lifecycle of their own.

Approvals, retries and hook runs are already wait nodes on the timeline and
never get a ``wait.*`` twin. This module records user input, MCP connection
setup, input-admission gates, tool-admission lock acquisition, a hook's
``ask`` waiting for the user (``hook_confirmation``, whose finish adds the
``result`` and ``end_reason`` codes), and a message's prompt-shortcut commands
running before its turn (``prompt_commands``). Categories
remain an open payload domain; declared categories need not have producers.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any

from chrys.foundation.trajectory.context import TrajectoryContext, current_trajectory
from chrys.foundation.trajectory.envelope import MeasurementSource, measurement
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.ids import new_analytics_id

logger = logging.getLogger(__name__)


class WaitOutcome:
    """``wait.finished.outcome``."""

    COMPLETED = "completed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    FAILED = "failed"


class WaitTrace:
    """One wait interval under the ambient trajectory scope."""

    __slots__ = (
        "_category",
        "_context",
        "_finished",
        "_operation_id",
        "_server_name",
        "_started",
        "_started_ns",
        "_target_operation_id",
    )

    def __init__(
        self,
        context: TrajectoryContext,
        *,
        category: str,
        target_operation_id: str | None,
        server_name: str | None = None,
    ) -> None:
        self._context = context
        self._category = category
        self._target_operation_id = target_operation_id
        self._server_name = server_name
        self._operation_id = new_analytics_id()
        self._started_ns = time.monotonic_ns()
        self._started = False
        self._finished = False

    @classmethod
    def open(
        cls,
        category: str,
        *,
        target_operation_id: str | None = None,
        server_name: str | None = None,
        context: TrajectoryContext | None = None,
        provider: Callable[[], TrajectoryContext | None] | None = None,
    ) -> WaitTrace | None:
        """Bind to explicit, ambient, or provider scope; ``None`` when nothing records."""
        resolved = context or current_trajectory()
        if resolved is None and provider is not None:
            resolved = provider()
        if resolved is None:
            return None
        return cls(
            resolved,
            category=category,
            target_operation_id=target_operation_id,
            server_name=server_name,
        )

    @property
    def operation_id(self) -> str:
        return self._operation_id

    def _parent(self) -> str | None:
        return self._target_operation_id or self._context.innermost_model_operation_id

    def _payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"category": self._category}
        if self._target_operation_id is not None:
            payload["target_operation_id"] = self._target_operation_id
        if self._server_name is not None:
            payload["server_name"] = self._server_name
        return payload

    async def started(self) -> None:
        payload = self._payload()

        def _commit(_sequence: int) -> Mapping[str, Any]:
            self._started = True
            return payload

        try:
            await self._context.sink.emit(
                self._context.draft(
                    EventType.WAIT_STARTED,
                    operation_id=self._operation_id,
                    parent_operation_id=self._parent(),
                    payload=payload,
                ),
                payload_factory=_commit,
            )
        except asyncio.CancelledError:
            self.finished_soon(outcome=WaitOutcome.CANCELLED)
            raise
        except Exception:
            logger.debug("Trajectory wait.started emit failed", exc_info=True)

    def _finished_draft(self, *, outcome: str, codes: Mapping[str, str]) -> Any:
        payload = self._payload()
        payload.update(codes)
        payload["outcome"] = outcome
        payload["duration_ms"] = max(0, (time.monotonic_ns() - self._started_ns) // 1_000_000)
        return self._context.draft(
            EventType.WAIT_FINISHED,
            operation_id=self._operation_id,
            parent_operation_id=self._parent(),
            payload=payload,
            measurements={"/payload/duration_ms": measurement(MeasurementSource.MONOTONIC_CLOCK, method_version=1)},
        )

    async def finished(self, *, outcome: str = WaitOutcome.COMPLETED, **codes: str) -> None:
        """Close the wait; *codes* are extra closed-code fields its category defines (never free text)."""
        if self._finished:
            return
        self._finished = True
        if not self._started:
            return
        try:
            await self._context.sink.emit(self._finished_draft(outcome=outcome, codes=codes))
        except Exception:
            logger.debug("Trajectory wait.finished emit failed", exc_info=True)

    def finished_soon(self, *, outcome: str, **codes: str) -> None:
        """Close without awaiting the ack (cancellation paths)."""
        if self._finished:
            return
        self._finished = True
        if not self._started:
            return
        try:
            self._context.sink.emit_soon(self._finished_draft(outcome=outcome, codes=codes))
        except Exception:
            logger.debug("Trajectory wait.finished emit failed", exc_info=True)
