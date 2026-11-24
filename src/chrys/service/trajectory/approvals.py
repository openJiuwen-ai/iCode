# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``approval.requested`` / ``approval.resolved`` recording for the approval middleware."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from typing import Any

from chrys.foundation.trajectory.context import TrajectoryContext, current_trajectory
from chrys.foundation.trajectory.envelope import MeasurementSource, measurement, utc_now_rfc3339
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory.metadata import OPERATION_ID_KEY

logger = logging.getLogger(__name__)


class ApprovalDecision:
    """``approval.resolved.decision``."""

    APPROVED = "approved"
    REJECTED = "rejected"
    INTERRUPTED = "interrupted"


class ApprovalDecider:
    """``approval.resolved.decider``."""

    USER = "user"
    JUDGE = "judge"
    NONE = "none"
    """Nobody decided — the request was abandoned before an answer arrived."""


class ApprovalTrace:
    """One approval request: requested at publish, resolved once the decision lands."""

    __slots__ = (
        "_context",
        "_generic_target",
        "_request_id",
        "_requested_committed",
        "_requested_ns",
        "_resolved",
        "_target_operation_id",
    )

    def __init__(
        self,
        context: TrajectoryContext,
        *,
        target_operation_id: str | None,
        generic_target: bool = False,
    ) -> None:
        self._context = context
        # The log mints its own request id: the middleware's is a short-lived
        # UI correlation token of a different shape, and nothing outside the
        # in-flight request ever sees it. Both approval events carry this one,
        # and ``target_tool_operation_id`` joins them to the call itself.
        self._request_id = new_analytics_id()
        self._target_operation_id = target_operation_id
        self._generic_target = generic_target
        self._requested_ns = time.monotonic_ns()
        self._requested_committed = False
        self._resolved = False

    @property
    def request_id(self) -> str:
        """Stable approval interval identity for observer hook attribution."""
        return self._request_id

    @classmethod
    def open(cls, invocation_metadata: Mapping[str, Any]) -> ApprovalTrace | None:
        context = current_trajectory()
        if context is None:
            return None
        target = invocation_metadata.get(OPERATION_ID_KEY)
        return cls(context, target_operation_id=target if isinstance(target, str) else None)

    @classmethod
    def open_for_operation(
        cls,
        *,
        context: TrajectoryContext | None,
        target_operation_id: str | None,
    ) -> ApprovalTrace | None:
        """Open a non-tool-specific approval targeting an arbitrary boundary."""
        if context is None:
            return None
        return cls(context, target_operation_id=target_operation_id, generic_target=True)

    def _target_payload(self) -> dict[str, str | None]:
        if self._target_operation_id is None:
            return {}
        key = "target_operation_id" if self._generic_target else "target_tool_operation_id"
        return {key: self._target_operation_id}

    def _parent(self) -> str | None:
        if self._generic_target and self._target_operation_id is not None:
            return self._target_operation_id
        return self._context.innermost_model_operation_id

    def _operation_id(self) -> str | None:
        """Use the approval's own identity when its target is an arbitrary operation."""
        if self._generic_target:
            return self._request_id
        return self._target_operation_id

    async def requested(self, *, tool_name: str, approval_mode: str, approval_level: str) -> None:
        payload: dict[str, Any] = {
            "approval_request_id": self._request_id,
            **self._target_payload(),
            "approval_mode": approval_mode,
            "approval_level": approval_level,
            "tool_name": tool_name,
            "requested_at": utc_now_rfc3339(),
        }

        def _commit(_sequence: int) -> Mapping[str, Any]:
            self._requested_committed = True
            return payload

        try:
            await self._context.sink.emit(
                self._context.draft(
                    EventType.APPROVAL_REQUESTED,
                    operation_id=self._operation_id(),
                    parent_operation_id=self._parent(),
                    payload=payload,
                ),
                payload_factory=_commit,
            )
        except asyncio.CancelledError:
            self.interrupted_soon(reason_code="cancelled")
            raise
        except Exception:
            logger.debug("Trajectory approval.requested emit failed", exc_info=True)

    def _resolved_draft(
        self,
        *,
        decision: str,
        decider: str,
        reason_code: str,
        arguments_modified: bool,
        judge_audit: Mapping[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {
            "approval_request_id": self._request_id,
            **self._target_payload(),
            "decision": decision,
            "decider": decider,
            "reason_code": reason_code,
            "arguments_modified": arguments_modified,
            "resolved_at": utc_now_rfc3339(),
            "wait_ms": max(0, (time.monotonic_ns() - self._requested_ns) // 1_000_000),
        }
        if judge_audit is not None:
            payload["formal_judge"] = _bounded_formal_audit(judge_audit)
        return self._context.draft(
            EventType.APPROVAL_RESOLVED,
            operation_id=self._operation_id(),
            parent_operation_id=self._parent(),
            payload=payload,
            measurements={"/payload/wait_ms": measurement(MeasurementSource.MONOTONIC_CLOCK, method_version=1)},
        )

    async def resolved(
        self,
        *,
        approved: bool,
        decider: str,
        reason_code: str,
        arguments_modified: bool,
        judge_audit: Mapping[str, Any] | None = None,
    ) -> None:
        if self._resolved:
            return
        self._resolved = True
        if not self._requested_committed:
            return
        draft = self._resolved_draft(
            decision=ApprovalDecision.APPROVED if approved else ApprovalDecision.REJECTED,
            decider=decider,
            reason_code=reason_code,
            arguments_modified=arguments_modified,
            judge_audit=judge_audit,
        )
        try:
            await self._context.sink.emit(draft)
        except Exception:
            logger.debug("Trajectory approval.resolved emit failed", exc_info=True)

    def interrupted_soon(
        self, *, reason_code: str = "interrupted", judge_audit: Mapping[str, Any] | None = None
    ) -> None:
        """Close a request nobody answered (queued in order, ack not awaited).

        The interrupt that abandons an approval also cancels the task waiting
        on it, so the close cannot await an acknowledgement it would never be
        allowed to receive — and a request with no resolution is a span the
        reader could never close.
        """
        if self._resolved:
            return
        self._resolved = True
        if not self._requested_committed:
            return
        draft = self._resolved_draft(
            decision=ApprovalDecision.INTERRUPTED,
            decider=ApprovalDecider.NONE,
            reason_code=reason_code,
            arguments_modified=False,
            judge_audit=judge_audit,
        )
        try:
            self._context.sink.emit_soon(draft)
        except Exception:
            logger.debug("Trajectory approval.resolved emit failed", exc_info=True)


def _bounded_formal_audit(audit: Mapping[str, Any]) -> dict[str, Any]:
    """Keep durable trajectory evidence structured and free of raw prompts."""
    fields = (
        "route",
        "stages",
        "failure_reason",
        "predicate_results",
        "asset_version",
        "decision_version",
        "asset_digest",
        "usage",
        "approved",
    )

    result = {key: audit[key][:4096] if isinstance(audit[key], str) else audit[key] for key in fields if key in audit}
    result["stages"] = list(audit.get("stages", []))[:4]
    result["predicate_results"] = [
        {key: item[key][:128] if isinstance(item[key], str) else item[key] for key in ("id", "value") if key in item}
        | ({"consent_bar_met": item["consent_bar_met"]} if type(item.get("consent_bar_met")) is bool else {})
        for item in (audit.get("predicate_results") or [])[:32]
    ]
    result["usage"] = {key: value for key, value in audit.get("usage", {}).items() if type(value) is int}
    return result
