# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Adapter-backed dialog gateway for main-screen controllers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from chrys.app.tui.screens.main.dialog_controllers import (
    AgentLoadDialogHandle,
    ApprovalBypassDecision,
    ApprovalDialogHandle,
    ApprovalResponseWorker,
    ApprovalTimer,
    ImageCompressionDialogHandle,
    QuestionDialogHandle,
    QuestionDialogResult,
)
from chrys.app.tui.screens.main.ports import DialogGatewayView, StatusMessage
from chrys.app.tui.widgets import PromptDraft
from chrys.foundation.events.types import ApprovalRequest, ApprovalReviewed, QuestionToUser
from chrys.foundation.models.approval_reuse import ReuseChoice
from chrys.foundation.models.ask_user import AskUserAnswer


@dataclass(frozen=True, slots=True)
class UiGatewayCallbacks:
    """Non-UI dialog effects supplied by the screen owner."""

    debug: Callable[[str, str], None]
    handle_approval_response: Callable[
        [str, bool, str, dict[str, Any] | None, ReuseChoice], ApprovalResponseWorker | None
    ]
    publish_auto_fulfill_blocked: Callable[[ApprovalReviewed], Awaitable[None]]
    handle_ask_user_response: Callable[[str, tuple[AskUserAnswer, ...]], object]
    question_inline_preferred: Callable[[], bool]
    approval_defer_while_judging: Callable[[], bool]
    set_agent_loading: Callable[[bool], None]


class UiGateway:
    """Facade exposing the exact dialog-controller port surface."""

    def __init__(self, view: DialogGatewayView, callbacks: UiGatewayCallbacks) -> None:
        self._view = view
        self._callbacks = callbacks

    def debug(self, key: str, message: str = "") -> None:
        self._callbacks.debug(key, message)

    async def build_approval_body(self, event: ApprovalRequest) -> object | None:
        return await self._view.build_approval_body(event)

    def approval_body_bypass(self, body: object | None) -> ApprovalBypassDecision | None:
        return self._view.approval_body_bypass(body)

    def show_approval_dialog(
        self,
        event: ApprovalRequest,
        approval_body: object | None,
        on_result: Callable[[tuple[bool, str, dict[str, Any] | None] | None], None],
        *,
        verdict: ApprovalReviewed | None,
    ) -> ApprovalDialogHandle:
        return self._view.show_approval_dialog(event, approval_body, on_result, verdict=verdict)

    def deliver_approval_verdict(self, dialog: ApprovalDialogHandle, event: ApprovalReviewed) -> None:
        self._view.deliver_approval_verdict(dialog, event)

    def dismiss_approval_dialog(self, dialog: ApprovalDialogHandle) -> None:
        self._view.dismiss_approval_dialog(dialog)

    def approval_dialog_tool_name(self, dialog: ApprovalDialogHandle) -> str:
        return self._view.approval_dialog_tool_name(dialog)

    def approval_defer_while_judging(self) -> bool:
        return self._callbacks.approval_defer_while_judging()

    def set_auto_review_count(self, count: int) -> None:
        self._view.set_auto_review_count(count)

    def notify_approval_required(self) -> None:
        self._view.notify_approval_required()

    def update_tool_args(self, call_id: str, args: dict[str, Any]) -> None:
        self._view.update_tool_args(call_id, args)

    def handle_approval_response(
        self,
        request_id: str,
        approved: bool,
        reason: str,
        modified_args: dict[str, Any] | None = None,
        remember_choice: ReuseChoice = "",
    ) -> ApprovalResponseWorker | None:
        return self._callbacks.handle_approval_response(request_id, approved, reason, modified_args, remember_choice)

    def run_worker(self, awaitable: Awaitable[Any], *, group: str) -> None:
        self._view.run_worker(awaitable, group=group)

    def set_timer(self, delay: float, callback: Callable[[], None]) -> ApprovalTimer:
        return self._view.set_timer(delay, callback)

    async def publish_auto_fulfill_blocked(self, event: ApprovalReviewed) -> None:
        await self._callbacks.publish_auto_fulfill_blocked(event)

    def show_question_dialog(
        self,
        event: QuestionToUser,
        draft: PromptDraft | None,
        on_result: Callable[[object], None],
    ) -> QuestionDialogHandle:
        return self._view.show_question_dialog(event, draft, on_result)

    def parse_question_dialog_result(self, result: object) -> QuestionDialogResult:
        return self._view.parse_question_dialog_result(result)

    def show_question_inline(self, event: QuestionToUser, draft: PromptDraft | None = None) -> bool:
        return self._view.show_question_inline(event, draft)

    def clear_question_inline(self, call_id: str, request_id: str) -> bool:
        return self._view.clear_question_inline(call_id, request_id)

    def question_can_reopen_modal(self, event: QuestionToUser) -> bool:
        return self._view.question_can_reopen_modal(event)

    def question_inline_preferred(self) -> bool:
        return self._callbacks.question_inline_preferred()

    def handle_ask_user_response(self, request_id: str, answers: tuple[AskUserAnswer, ...]) -> None:
        self._callbacks.handle_ask_user_response(request_id, answers)

    def notify_ask_user(self) -> None:
        self._view.notify_ask_user()

    def focus_input(self) -> None:
        self._view.focus_input()

    def create_agent_load_dialog(self, *, title: StatusMessage, subtitle: str) -> AgentLoadDialogHandle:
        return self._view.create_agent_load_dialog(title=title, subtitle=subtitle)

    def prepare_agent_load_ui(self, **kwargs: Any) -> dict | None:
        self._callbacks.set_agent_loading(True)
        return self._view.prepare_agent_load_ui(**kwargs)

    async def push_agent_load_dialog(self, dialog: AgentLoadDialogHandle) -> None:
        await self._view.push_agent_load_dialog(dialog)

    def set_agent_loading(self, value: bool) -> None:
        self._callbacks.set_agent_loading(value)

    def restore_agent_load_status(self, snapshot: dict) -> None:
        self._view.restore_agent_load_status(snapshot)

    def show_load_status(self, message: StatusMessage) -> None:
        self._view.show_load_status(message)

    def render_status_message(self, message: StatusMessage) -> str:
        return self._view.render_status_message(message)

    def flash_agent_load_failed(self, message: str) -> None:
        self._view.flash_agent_load_failed(message)

    def create_image_compression_dialog(self, *, title: StatusMessage) -> ImageCompressionDialogHandle:
        return self._view.create_image_compression_dialog(title=title)

    async def push_image_compression_dialog(self, dialog: ImageCompressionDialogHandle) -> None:
        await self._view.push_image_compression_dialog(dialog)
