# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pinned message ids for approval dialogs, DAA choices and approval modes."""

from __future__ import annotations

APPROVAL_MESSAGE_IDS: frozenset[str | tuple[str, str]] = frozenset(
    {
        "tui.approval.button.approve",
        "tui.approval.button.decline",
        "tui.approval.daa.command",
        "tui.approval.daa.description",
        "tui.approval.daa.exact_project",
        "tui.approval.daa.exact_session",
        "tui.approval.daa.files",
        "tui.approval.daa.once",
        "tui.approval.daa.prefix",
        "tui.approval.daa.prefix_project",
        "tui.approval.daa.prefix_session",
        "tui.approval.evaluating",
        "tui.approval.file_edit.content",
        "tui.approval.file_edit.planned_diff",
        "tui.approval.file_edit.prepare_diff_error",
        "tui.approval.file_edit.preparing_diff",
        ("tui.approval.file_edit.replacements", "tui.approval.file_edit.replacements#plural"),
        "tui.approval.flagged",
        "tui.approval.mode_changed",
        "tui.approval.presentation.edit_files",
        "tui.approval.presentation.read_files",
        "tui.approval.presentation.remote_tool",
        "tui.approval.presentation.run_command",
        "tui.approval.presentation.search",
        "tui.approval.reason_placeholder",
        "tui.approval.required_title",
        "tui.approval.sub_agent.detail",
        "tui.approval.sub_agent.prompt_title",
        "tui.approval.sub_agent.review",
        "tui.approval.title",
        "tui.approval_mode.description.auto",
        "tui.approval_mode.description.bypass",
        "tui.approval_mode.description.manual",
        "tui.approval_mode.title",
        "tui.approval.judge.auto_approved",
        "tui.approval.judge.flagged",
    }
)
