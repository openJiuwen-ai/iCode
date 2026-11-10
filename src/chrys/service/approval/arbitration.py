# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared judge-versus-user approval race arbitration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from chrys.foundation.events.types import ApprovalAutoFulfillBlocked, ApprovalReviewed
from chrys.service.approval.review_types import ApprovalJudgeInput as ApprovalJudgeInput

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.foundation.events.bus import EventBus
    from chrys.service.approval.judge import ApprovalJudge, JudgeVerdict


_log = logging.getLogger(__name__)


def _record_verdict(verdict: JudgeVerdict, callback: Callable[[JudgeVerdict], None] | None) -> None:
    if callback is not None:
        try:
            callback(verdict)
        except Exception:
            _log.warning("Approval audit callback failed", exc_info=True)


class ApprovalDecisionArbiter:
    """Single-winner arbitration shared by middleware and ACP broker."""

    def __init__(self, bus: EventBus, *, session_id: str | None = None) -> None:
        self._bus = bus
        self._session_id = session_id
        self._blocked: set[str] = set()
        self._subscribed = False

    async def ensure_subscription(self) -> None:
        if self._subscribed:
            return
        await self._bus.subscribe(ApprovalAutoFulfillBlocked, self._on_blocked)
        self._subscribed = True

    async def judge(
        self,
        *,
        request_id: str,
        judge: ApprovalJudge,
        judge_input: ApprovalJudgeInput,
        decision_future: asyncio.Future[Any],
        approved_value: Any,
        log_dir: Path | None,
        on_verdict: Callable[[JudgeVerdict], None] | None = None,
    ) -> None:
        """Publish a verdict and fulfil only while no user decision has won."""
        from chrys.service.approval.judge import FormalEvaluationCancelled, JudgeVerdict

        # A response can win between task creation and its first instruction.
        # Do not spend a model call or emit a stale review in that case.
        if decision_future.done():
            return
        try:
            verdict = await judge.evaluate(
                user_message=judge_input.user_message,
                user_messages=list(judge_input.user_messages),
                tool_name=judge_input.tool_name,
                tool_kind=judge_input.tool_kind,
                args=judge_input.args,
                workspace_roots=list(judge_input.workspace_roots),
                request_id=request_id,
                log_dir=log_dir,
                relay_enhanced=judge_input.relay_enhanced,
                reviewed_model=judge_input.reviewed_model,
            )
        except FormalEvaluationCancelled as exc:
            # Owner-only evidence (e.g. ACP audit ring); no UI verdict and no
            # future fulfillment after cancellation or a human decision.
            _record_verdict(exc.verdict, on_verdict)
            return
        except asyncio.CancelledError:
            return
        except Exception as exc:
            verdict = JudgeVerdict(approved=False, reason=f"Judge evaluation failed: {exc}")

        # Middleware cancels the task when a human responds.  This additional
        # guard covers a response that wins while a provider cancellation is
        # being delivered, so no late Judge verdict reaches the UI.
        _record_verdict(verdict, on_verdict)
        if decision_future.done():
            return
        await self._bus.publish(
            ApprovalReviewed(
                request_id=request_id,
                approved=verdict.approved,
                reason=verdict.reason,
                triggered=verdict.triggered,
                failure_reason=verdict.failure_reason,
                session_id=self._session_id,
            )
        )
        if verdict.approved:
            blocked = request_id in self._blocked
            self._blocked.discard(request_id)
            if not blocked and not decision_future.done():
                decision_future.set_result(approved_value)

    async def _on_blocked(self, event: ApprovalAutoFulfillBlocked) -> None:
        if event.request_id:
            self._blocked.add(event.request_id)

    def clear(self) -> None:
        self._blocked.clear()

    async def close(self) -> None:
        if not self._subscribed:
            return
        await self._bus.unsubscribe(ApprovalAutoFulfillBlocked, self._on_blocked)
        self._subscribed = False
        self._blocked.clear()
