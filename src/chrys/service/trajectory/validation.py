# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``model.validation.finished`` recording for the response validator.

The validation middleware runs beneath the kernel loop, so the kernel's
:class:`ExchangeTrace` is the only handle it has on the exchange it judged.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from chrys.foundation.trajectory.context import ExchangeTrace, forwarded_exchange_trace
from chrys.foundation.trajectory.envelope import Link, LinkRelation
from chrys.foundation.trajectory.event_types import EventType, ValidationOutcome
from chrys.foundation.trajectory.ids import new_analytics_id

logger = logging.getLogger(__name__)


class ValidationTrace:
    """The validation fact for the exchange one ``call_next`` acquires."""

    __slots__ = ("_trace",)

    def __init__(self, trace: ExchangeTrace) -> None:
        self._trace = trace

    @classmethod
    def open(cls, kwargs: Mapping[str, Any]) -> ValidationTrace | None:
        """Pick the kernel's exchange trace out of the middleware context kwargs."""
        trace = forwarded_exchange_trace(kwargs)
        return cls(trace) if trace is not None else None

    @property
    def exchange_operation_id(self) -> str:
        return self._trace.operation_id

    async def finished(self, *, accepted: bool, reason_code: str | None = None) -> None:
        """Record the verdict on the exchange.

        Only the verdict: whether the run sends the request again is the
        retry owner's decision, which a following ``retry.scheduled`` records.
        """
        context = self._trace.context
        exchange_id = self._trace.operation_id
        payload: dict[str, Any] = {
            "outcome": ValidationOutcome.ACCEPTED if accepted else ValidationOutcome.REJECTED,
            "exchange_operation_id": exchange_id,
            "attempt_index": 0,
        }
        if not accepted:
            payload["reason_code"] = reason_code
        try:
            await context.sink.emit(
                context.draft(
                    EventType.MODEL_VALIDATION_FINISHED,
                    operation_id=new_analytics_id(),
                    parent_operation_id=exchange_id,
                    payload=payload,
                    links=(Link(relation=LinkRelation.VALIDATES, target_operation_id=exchange_id),),
                )
            )
        except Exception:
            logger.debug("Trajectory model.validation.finished emit failed", exc_info=True)
