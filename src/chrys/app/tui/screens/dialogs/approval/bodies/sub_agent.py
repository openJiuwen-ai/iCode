# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Approval dialog body for sub-agent delegation in developer mode."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.containers import VerticalGroup
from textual.widgets import Static

from chrys.app.tui.i18n import render_text, widget_localizer
from chrys.app.tui.screens.dialogs.approval.body import ApprovalBody, register_approval_kind_body
from chrys.app.tui.util.source_text import mark_hidden_format
from chrys.app.tui.widgets import EnhancedTextArea
from chrys.foundation.i18n import msg
from chrys.foundation.i18n.formatting import sanitize_terminal_block
from chrys.foundation.tool_kinds import KIND_SUB_AGENT

if TYPE_CHECKING:
    from textual.app import ComposeResult


_DELEGATION_PROMPT = msg("tui.approval.sub_agent.prompt_title", fallback="Delegation Prompt")
_DELEGATION_REVIEW = msg(
    "tui.approval.sub_agent.review",
    fallback="Review or edit the message before it is sent to the sub-agent. Edits apply only to this delegation.",
)
_DELEGATION_DETAIL = msg(
    "tui.approval.sub_agent.detail",
    fallback="Review sub-agent delegation",
)


class SubAgentPromptEditor(VerticalGroup):
    """Editable prompt field for a pending sub-agent delegation."""

    def __init__(self, prompt: str) -> None:
        super().__init__(classes="approval-sub-agent-prompt")
        # The field shows the prompt with its control, bidi and zero-width
        # characters marked, so none of them can hide or reorder what is
        # approved; the prompt goes out as given unless the user edits it.
        self._shown_prompt = mark_hidden_format(sanitize_terminal_block(prompt))
        self._prompt_input = EnhancedTextArea(
            self._shown_prompt,
            id="approval-sub-agent-prompt-input",
            soft_wrap=True,
            show_line_numbers=False,
        )

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        self.border_title = render_text(localizer, _DELEGATION_PROMPT.bind())
        yield Static(
            render_text(localizer, _DELEGATION_REVIEW.bind()),
            classes="approval-sub-agent-help",
        )
        yield self._prompt_input

    def modified_args(self) -> dict[str, Any] | None:
        prompt = self._prompt_input.text
        if prompt == self._shown_prompt:
            return None
        return {"prompt": prompt}


def _build_sub_agent_body(
    tool_kind: str,
    args: dict[str, Any],
    workspace_cwd: str | None = None,
) -> ApprovalBody | None:
    del workspace_cwd
    if tool_kind != KIND_SUB_AGENT:
        return None

    prompt_arg = args.get("prompt", "")
    prompt = prompt_arg if isinstance(prompt_arg, str) else str(prompt_arg)
    editor = SubAgentPromptEditor(prompt)
    return ApprovalBody(
        detail=_DELEGATION_DETAIL.bind(),
        widgets=[editor],
        hidden_arg_keys=frozenset({"prompt"}),
        modified_args=editor.modified_args,
    )


register_approval_kind_body(KIND_SUB_AGENT, _build_sub_agent_body)
