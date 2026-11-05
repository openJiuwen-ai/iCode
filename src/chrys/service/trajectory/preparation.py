# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Preparation-container recording for admission and dispatch work."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, Final, Protocol

from chrys.foundation.trajectory.context import TrajectoryContext, current_trajectory
from chrys.foundation.trajectory.envelope import MeasurementSource, measurement
from chrys.foundation.trajectory.event_types import EventType, WaitCategory
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.service.trajectory.waits import WaitOutcome, WaitTrace

logger = logging.getLogger(__name__)

TrajectoryContextProvider = Callable[[], TrajectoryContext | None]


class InputAdmissionWaitRegistration(Protocol):
    """Mutable owner that exposes the currently open input-admission wait."""

    current_wait: WaitTrace | None


class PreparationScope:
    """``preparation.*.scope`` values emitted by Chrys."""

    PRE_TURN = "pre_turn"
    TURN_PREAMBLE = "turn_preamble"
    TOOL_PREAMBLE = "tool_preamble"


class PreparationOutcome:
    """Common terminal outcomes for turn/tool preparation containers."""

    HANDOFF: Final = "handoff"
    COMPLETED: Final = "completed"
    FAILED: Final = "failed"
    INTERRUPTED: Final = "interrupted"
    FRESH_TURN: Final = "fresh_turn"
    RETRY_TURN: Final = "retry_turn"
    INJECTED: Final = "injected"
    ABANDONED_NO_TARGET: Final = "abandoned_no_target"
    CANCELLED: Final = "cancelled"
    TARGET_STALE: Final = "target_stale"
    REJECTED: Final = "rejected"
    IMAGE_REJECTED: Final = "image_rejected"
    COMMAND_FAILED: Final = "command_failed"
    """A prompt shortcut's ``!`command``` failed to start, timed out or was stopped."""
    NOT_READY: Final = "not_ready"
    PREPARATION_FAILED: Final = "preparation_failed"
    CONFLICT: Final = "conflict"
    OWNER_CHANGED: Final = "owner_changed"
    SUPERSEDED: Final = "superseded"
    DROPPED: Final = "dropped"


class PreparationTrace:
    """One preparation container under an explicit or ambient trajectory scope."""

    __slots__ = (
        "_context",
        "_finished",
        "_operation_id",
        "_parent_operation_id",
        "_phase",
        "_scope",
        "_start_committed",
        "_start_settlement",
        "_started_ns",
        "_target_operation_id",
        "_terminal_settlement",
    )

    def __init__(
        self,
        context: TrajectoryContext,
        *,
        scope: str,
        phase: str,
        parent_operation_id: str | None,
        target_operation_id: str | None,
    ) -> None:
        self._context = context
        self._scope = scope
        self._phase = phase
        self._parent_operation_id = parent_operation_id
        self._target_operation_id = target_operation_id
        self._operation_id = new_analytics_id()
        self._started_ns = time.monotonic_ns()
        self._start_committed = False
        self._start_settlement: asyncio.Task[None] | None = None
        self._finished = False
        self._terminal_settlement: asyncio.Task[None] | None = None

    @classmethod
    def open(
        cls,
        *,
        scope: str,
        phase: str,
        parent_operation_id: str | None = None,
        target_operation_id: str | None = None,
        context: TrajectoryContext | None = None,
        provider: TrajectoryContextProvider | None = None,
    ) -> PreparationTrace | None:
        """Bind to *context*, then ambient/provider context; return ``None`` when disabled."""
        resolved = context or current_trajectory()
        if resolved is None and provider is not None:
            resolved = provider()
        if resolved is None:
            return None
        return cls(
            resolved,
            scope=scope,
            phase=phase,
            parent_operation_id=parent_operation_id,
            target_operation_id=target_operation_id,
        )

    @property
    def operation_id(self) -> str:
        return self._operation_id

    @property
    def context(self) -> TrajectoryContext:
        return self._context

    @property
    def start_committed(self) -> bool:
        return self._start_committed

    @property
    def committed_operation_id(self) -> str | None:
        """Return the operation id only after its opening event owns a sequence."""
        return self._operation_id if self._start_committed else None

    @property
    def finished_state(self) -> bool:
        return self._finished

    def _payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"scope": self._scope, "phase": self._phase}
        if self._target_operation_id is not None:
            payload["target_operation_id"] = self._target_operation_id
        return payload

    async def started(self) -> None:
        settlement = self._start_settlement
        if settlement is None:
            if self._finished:
                return
            settlement = asyncio.create_task(self._emit_start())
            self._start_settlement = settlement
        await asyncio.shield(settlement)

    async def _emit_start(self) -> None:
        payload = self._payload()

        def _commit(_sequence: int) -> Mapping[str, Any]:
            self._start_committed = True
            return payload

        try:
            await self._context.sink.emit(
                self._context.draft(
                    EventType.PREPARATION_STARTED,
                    operation_id=self._operation_id,
                    parent_operation_id=self._parent_operation_id,
                    payload=payload,
                ),
                payload_factory=_commit,
            )
        except Exception:
            logger.debug("Trajectory preparation.started emit failed", exc_info=True)

    def _finished_draft(self, *, outcome: str, target_turn_id: str | None) -> Any:
        payload = self._payload()
        payload["outcome"] = outcome
        payload["duration_ms"] = max(0, (time.monotonic_ns() - self._started_ns) // 1_000_000)
        if target_turn_id is not None:
            payload["target_turn_id"] = target_turn_id
        return self._context.draft(
            EventType.PREPARATION_FINISHED,
            operation_id=self._operation_id,
            parent_operation_id=self._parent_operation_id,
            payload=payload,
            measurements={"/payload/duration_ms": measurement(MeasurementSource.MONOTONIC_CLOCK, method_version=1)},
        )

    async def finished(self, *, outcome: str, target_turn_id: str | None = None) -> None:
        settlement = self._terminal_settlement
        if not self._finished:
            self._finished = True
            settlement = asyncio.create_task(
                self._emit_finished_after_start(
                    outcome=outcome,
                    target_turn_id=target_turn_id,
                    wait_for_ack=True,
                )
            )
            self._terminal_settlement = settlement
        if settlement is not None:
            await asyncio.shield(settlement)

    def finished_soon(self, *, outcome: str, target_turn_id: str | None = None) -> None:
        """Close without waiting for the writer acknowledgement."""
        if self._finished:
            return
        self._finished = True
        start_settlement = self._start_settlement
        if self._start_committed or start_settlement is None or start_settlement.done():
            if self._start_committed:
                self._emit_finished_soon(outcome=outcome, target_turn_id=target_turn_id)
            return
        self._terminal_settlement = asyncio.create_task(
            self._emit_finished_after_start(
                outcome=outcome,
                target_turn_id=target_turn_id,
                wait_for_ack=False,
            )
        )

    async def _emit_finished_after_start(
        self,
        *,
        outcome: str,
        target_turn_id: str | None,
        wait_for_ack: bool,
    ) -> None:
        start_settlement = self._start_settlement
        if not self._start_committed and start_settlement is not None:
            await asyncio.wait([start_settlement])
        if not self._start_committed:
            return
        if not wait_for_ack:
            self._emit_finished_soon(outcome=outcome, target_turn_id=target_turn_id)
            return
        try:
            await self._context.sink.emit(self._finished_draft(outcome=outcome, target_turn_id=target_turn_id))
        except Exception:
            logger.debug("Trajectory preparation.finished emit failed", exc_info=True)

    def _emit_finished_soon(self, *, outcome: str, target_turn_id: str | None) -> None:
        try:
            self._context.sink.emit_soon(self._finished_draft(outcome=outcome, target_turn_id=target_turn_id))
        except Exception:
            logger.debug("Trajectory preparation.finished emit failed", exc_info=True)

    async def state(self, state: str) -> None:
        """Record a non-terminal state marker for this preparation scope."""
        if not self._start_committed or self._finished:
            return
        try:
            await self._context.sink.emit(
                self._context.draft(
                    EventType.PREPARATION_STATE,
                    operation_id=self._operation_id,
                    parent_operation_id=self._parent_operation_id,
                    payload={
                        "state": state,
                        "scope_operation_id": self._operation_id,
                        "scope": self._scope,
                    },
                )
            )
        except Exception:
            logger.debug("Trajectory preparation.state emit failed", exc_info=True)

    def state_soon(self, state: str) -> None:
        """Queue a non-terminal state marker from a synchronous state transition."""
        if not self._start_committed or self._finished:
            return
        try:
            self._context.sink.emit_soon(
                self._context.draft(
                    EventType.PREPARATION_STATE,
                    operation_id=self._operation_id,
                    parent_operation_id=self._parent_operation_id,
                    payload={
                        "state": state,
                        "scope_operation_id": self._operation_id,
                        "scope": self._scope,
                    },
                )
            )
        except Exception:
            logger.debug("Trajectory preparation.state emit failed", exc_info=True)


async def input_admission_wait(
    wait_for_gate: Callable[[], Awaitable[Any]],
    preparation: PreparationTrace | None,
    registration: InputAdmissionWaitRegistration | None,
) -> Any:
    """Await one pre-turn gate with a child ``wait.input_admission`` interval."""
    wait = (
        WaitTrace.open(
            WaitCategory.INPUT_ADMISSION,
            target_operation_id=preparation.committed_operation_id,
            context=preparation.context,
        )
        if preparation is not None and preparation.committed_operation_id is not None
        else None
    )
    if registration is not None:
        registration.current_wait = wait
    try:
        if wait is not None:
            await wait.started()
        result = await wait_for_gate()
    except asyncio.CancelledError:
        if wait is not None:
            wait.finished_soon(outcome=WaitOutcome.CANCELLED)
        raise
    except BaseException:
        if wait is not None:
            await wait.finished(outcome=WaitOutcome.FAILED)
        raise
    else:
        if wait is not None:
            await wait.finished()
        return result
    finally:
        if registration is not None and registration.current_wait is wait:
            registration.current_wait = None


@asynccontextmanager
async def preparation_lock(lock: asyncio.Lock, preparation: PreparationTrace | None) -> AsyncGenerator[None]:
    """Acquire *lock* with a child ``wait.tool_admission`` interval."""
    wait = (
        WaitTrace.open(
            WaitCategory.TOOL_ADMISSION,
            target_operation_id=preparation.committed_operation_id,
            context=preparation.context,
        )
        if preparation is not None and preparation.committed_operation_id is not None
        else None
    )
    acquired = False
    try:
        if wait is not None:
            await wait.started()
        await lock.acquire()
        acquired = True
    except asyncio.CancelledError:
        if wait is not None:
            wait.finished_soon(outcome=WaitOutcome.CANCELLED)
        raise
    except BaseException:
        if wait is not None:
            await wait.finished(outcome=WaitOutcome.FAILED)
        raise
    try:
        if wait is not None:
            await wait.finished()
        yield
    finally:
        if acquired:
            lock.release()


__all__ = [
    "PreparationOutcome",
    "PreparationScope",
    "PreparationTrace",
    "input_admission_wait",
    "preparation_lock",
]
