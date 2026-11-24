# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Approval concerns carry stable fields and render in the selected language."""

from __future__ import annotations

import pytest

from chrys.app.tui.screens.dialogs.approval.review_reason import _RISKS, review_reason
from chrys.app.tui.screens.main.view_adapter import _judge_verdict
from chrys.foundation.events.types import ApprovalReviewed
from chrys.foundation.i18n import Localizer
from chrys.service.approval.judge import JudgeVerdict
from chrys.service.approval.predicate import load_default_asset


def test_triggered_conditions_reach_chinese_dialog():
    event = ApprovalReviewed(
        approved=False, reason="Requires human review: file_folder_deletion.", triggered=("file_folder_deletion",)
    )
    verdict = _judge_verdict(event)
    text = review_reason(verdict, Localizer("zh-Hans"))
    assert "删除" in text
    assert "file_folder_deletion" not in text
    assert "Requires human review" not in text


@pytest.mark.parametrize(
    "kind",
    ["invalid_input", "asset_unavailable", "timeout", "invalid_shape", "invalid_verdict", "rate_limited"],
)
def test_fixed_failure_reasons_are_localized(kind):
    event = ApprovalReviewed(approved=False, reason="English failure", failure_reason=kind)
    text = review_reason(_judge_verdict(event), Localizer("zh-Hans"))
    assert "English failure" not in text
    assert any("\u4e00" <= character <= "\u9fff" for character in text)


def test_direct_reason_keeps_model_language():
    reason = "模型说明为什么需要人工确认"
    assert review_reason(JudgeVerdict(False, reason), Localizer("en")) == reason


def test_display_names_cover_the_packaged_conditions():
    assert set(_RISKS) == {principle.id for principle in load_default_asset().principles}


@pytest.mark.parametrize("triggered", [("custom_condition",), ("security_boundary_change", "custom_condition")])
def test_unknown_condition_keeps_the_original_reason(triggered):
    verdict = JudgeVerdict(False, "Custom policy requires confirmation.", triggered=triggered)
    assert review_reason(verdict, Localizer("zh-Hans")) == verdict.reason


@pytest.mark.parametrize(
    "kind,english,chinese",
    [
        ("auth_failed", "authentication or access", "鉴权或访问权限"),
        ("rate_limited", "rate limiting", "请求限流"),
        ("server_error", "internal error", "内部错误"),
    ],
)
def test_provider_failure_shows_its_specific_cause(kind, english, chinese):
    verdict = JudgeVerdict(False, "English failure", failure_reason=kind)
    assert english in review_reason(verdict, Localizer("en"))
    assert chinese in review_reason(verdict, Localizer("zh-Hans"))


def test_unmapped_failure_keeps_the_generic_message():
    verdict = JudgeVerdict(False, "Provider details", failure_reason="unknown")
    assert review_reason(verdict, Localizer("en")) == "Automatic review failed. Please check the action."
