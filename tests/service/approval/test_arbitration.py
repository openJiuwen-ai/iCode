# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Human decisions take priority while completed and cancelled reviews stay auditable."""

from __future__ import annotations

import asyncio
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalReviewed
from chrys.service.approval.arbitration import ApprovalDecisionArbiter, ApprovalJudgeInput
from chrys.service.approval.judge import ApprovalJudge, FormalEvaluationCancelled, JudgeVerdict
from tests.support.event_capture import capture_event_sequence


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("audit_fails", [False, True])
async def test_human_wins_without_late_review_but_retains_verdict(cancelled, audit_fails):
    bus = EventBus()
    future = asyncio.get_running_loop().create_future()
    verdict = JudgeVerdict(
        False, "risk", audit={"approved": False, "failure_reason": "cancelled" if cancelled else None}
    )
    judge = create_autospec(ApprovalJudge, instance=True)

    async def evaluated(
        user_message,
        tool_name,
        tool_kind,
        args,
        workspace_roots,
        request_id="",
        log_dir=None,
        user_messages=None,
        relay_enhanced=False,
        reviewed_model="",
    ):
        future.set_result("human")
        if cancelled:
            raise FormalEvaluationCancelled(verdict)
        return verdict

    judge.evaluate.side_effect = evaluated
    saved = []

    def record(verdict):
        saved.append(verdict)
        if audit_fails:
            raise ValueError("Audit unavailable")

    async with capture_event_sequence(bus, ApprovalReviewed) as events:
        await ApprovalDecisionArbiter(bus).judge(
            request_id="request",
            judge=judge,
            judge_input=ApprovalJudgeInput(
                tool_name="shell",
                tool_kind="",
                args={},
                user_message="Inspect",
                user_messages=["Inspect"],
                workspace_roots=[],
            ),
            decision_future=future,
            approved_value="model",
            log_dir=None,
            on_verdict=record,
        )
        assert events == []
    assert saved == [verdict]
    assert future.result() == "human"


@pytest.mark.parametrize("approved", [False, True])
async def test_audit_failure_does_not_block_review(approved):
    bus = EventBus()
    future = asyncio.get_running_loop().create_future()
    judge = create_autospec(ApprovalJudge, instance=True)
    judge.evaluate.return_value = JudgeVerdict(approved, "Reviewed")

    def record(verdict):
        raise ValueError("Audit unavailable")

    async with capture_event_sequence(bus, ApprovalReviewed) as events:
        await ApprovalDecisionArbiter(bus).judge(
            request_id="request",
            judge=judge,
            judge_input=ApprovalJudgeInput(
                tool_name="shell",
                tool_kind="",
                args={},
                user_message="Inspect",
                user_messages=["Inspect"],
                workspace_roots=[],
            ),
            decision_future=future,
            approved_value="model",
            log_dir=None,
            on_verdict=record,
        )
    assert len(events) == 1
    assert events[0].approved is approved
    assert future.done() is approved
    if approved:
        assert future.result() == "model"
    else:
        future.cancel()


async def test_human_decides_before_judge_starts():
    future = asyncio.get_running_loop().create_future()
    future.set_result("human")
    judge = create_autospec(ApprovalJudge, instance=True)
    saved = []
    await ApprovalDecisionArbiter(EventBus()).judge(
        request_id="request",
        judge=judge,
        judge_input=ApprovalJudgeInput(
            tool_name="shell",
            tool_kind="",
            args={},
            user_message="Inspect",
            user_messages=["Inspect"],
            workspace_roots=[],
        ),
        decision_future=future,
        approved_value="model",
        log_dir=None,
        on_verdict=saved.append,
    )
    judge.evaluate.assert_not_called()
    assert saved == []
