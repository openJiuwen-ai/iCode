# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for main-screen dialog controllers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from chrys.app.tui.screens.main.dialog_controllers import (
    _APPROVAL_AUTO_APPROVED_JUDGE,
    _APPROVAL_FLAGGED,
    AgentLoadDialogController,
    AnswerQuestionDialogResult,
    ApprovalBypassDecision,
    ApprovalQueueController,
    ImageCompressionDialogController,
    InlineQuestionDialogResult,
    QuestionQueueController,
)
from chrys.app.tui.widgets import PromptDraft
from chrys.foundation.events.types import (
    AGENT_LOAD_STATUS_DONE,
    AgentLoadFailed,
    AgentLoadProgress,
    ApprovalCancelled,
    ApprovalRequest,
    ApprovalReviewed,
    AskUserTimedOut,
    ImageAttachmentCompressionFinished,
    ImageAttachmentCompressionStarted,
    QuestionToUser,
)
from chrys.foundation.i18n import Localizer, MessageRef
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.approval_reuse import ReuseChoice
from chrys.foundation.models.ask_user import AskUserAnswer, AskUserOption, AskUserQuestion


def _display_message(message: MessageRef | str) -> str:
    return message if isinstance(message, str) else format_message(message)


def _question_event(
    request_id: str,
    question: str,
    *,
    call_id: str = "",
    options: tuple[str, ...] = (),
) -> QuestionToUser:
    return QuestionToUser(
        request_id=request_id,
        call_id=call_id,
        questions=(
            AskUserQuestion(
                question=question,
                options=tuple(AskUserOption(label=label) for label in options),
            ),
        ),
    )


def test_approval_judge_labels_keep_english_and_localize_chinese() -> None:
    flagged = _APPROVAL_FLAGGED.bind(reason="unsafe")
    assert format_message(_APPROVAL_AUTO_APPROVED_JUDGE.bind()) == "auto-approved, judge"
    assert format_message(flagged) == "flagged: unsafe"
    chinese = Localizer("zh-Hans")
    assert chinese.render(_APPROVAL_AUTO_APPROVED_JUDGE.bind()) == "已自动批准，审核者"  # noqa: RUF001
    assert chinese.render(flagged) == "已标记：unsafe"  # noqa: RUF001


def _approval_request(request_id: str, *, tool_name: str = "zsh", judging: bool = True) -> ApprovalRequest:
    return ApprovalRequest(
        request_id=request_id,
        call_id=f"call-{request_id}",
        tool_name=tool_name,
        tool_kind="shell",
        args={"cmd": tool_name},
        judging=judging,
    )


class _ApprovalTimer:
    def __init__(self, delay: float, callback: Callable[[], None]) -> None:
        self.delay = delay
        self.callback = callback
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True

    def fire(self) -> None:
        # A stopped timer never calls back.
        if not self.stopped:
            self.callback()


class _ApprovalPort:
    def __init__(self) -> None:
        self.dialogs: list[SimpleNamespace] = []
        self.timers: list[_ApprovalTimer] = []
        self.debug_calls: list[tuple[str, str]] = []
        self.notifications = 0
        self.responses: list[tuple[str, bool, str, dict[str, Any] | None]] = []
        self.remember_choices: list[ReuseChoice] = []
        self.updated_args: list[tuple[str, dict[str, Any]]] = []
        self.auto_fulfill_blocked: list[str] = []
        self.cancelled_dialogs: list[str] = []
        self.bypass: ApprovalBypassDecision | None = None
        self.defer_while_judging = False
        self.review_counts: list[int] = []
        self.body: object | None = None

    async def build_approval_body(self, _event: ApprovalRequest) -> object | None:
        return object() if self.bypass is not None else self.body

    def approval_body_bypass(self, _body: object | None) -> ApprovalBypassDecision | None:
        return self.bypass

    def show_approval_dialog(
        self,
        event: ApprovalRequest,
        approval_body: object | None,
        on_result: Callable[[tuple[bool, str, dict[str, Any] | None] | None], None],
        *,
        verdict: ApprovalReviewed | None,
    ) -> SimpleNamespace:
        dialog = SimpleNamespace(
            when_ready=lambda callback: callback(),
            request_id=event.request_id,
            tool_name=event.tool_name,
            user_decision_submitted=False,
            remember_choice="",
            is_dismissed=False,
            callback=on_result,
            body=approval_body,
            opened_with=None if verdict is None else (verdict.approved, verdict.reason),
            verdicts=[],
        )
        self.dialogs.append(dialog)
        return dialog

    def dismiss_approval_dialog(self, dialog: SimpleNamespace) -> None:
        dialog.is_dismissed = True
        self.cancelled_dialogs.append(dialog.request_id)
        dialog.callback(None)

    def deliver_approval_verdict(self, dialog: SimpleNamespace, event: ApprovalReviewed) -> None:
        dialog.verdicts.append((event.approved, event.reason))

    def approval_dialog_tool_name(self, dialog: SimpleNamespace) -> str:
        return dialog.tool_name

    def debug(self, key: str, message: str = "") -> None:
        self.debug_calls.append((key, message))

    def notify_approval_required(self) -> None:
        self.notifications += 1

    def update_tool_args(self, call_id: str, args: dict[str, Any]) -> None:
        self.updated_args.append((call_id, args))

    def handle_approval_response(
        self,
        request_id: str,
        approved: bool,
        reason: str,
        modified_args: dict[str, Any] | None = None,
        remember_choice: ReuseChoice = "",
    ) -> None:
        self.responses.append((request_id, approved, reason, modified_args))
        self.remember_choices.append(remember_choice)

    def run_worker(self, _awaitable: object, *, group: str) -> None:
        assert group == "approval-cleanup"

    def set_timer(self, delay: float, callback: Callable[[], None]) -> _ApprovalTimer:
        timer = _ApprovalTimer(delay, callback)
        self.timers.append(timer)
        return timer

    async def publish_auto_fulfill_blocked(self, event: ApprovalReviewed) -> None:
        self.auto_fulfill_blocked.append(event.request_id)

    def approval_defer_while_judging(self) -> bool:
        return self.defer_while_judging

    def set_auto_review_count(self, count: int) -> None:
        self.review_counts.append(count)


async def test_approval_controller_skips_cached_auto_approved_request_and_shows_flagged() -> None:
    port = _ApprovalPort()
    controller = ApprovalQueueController(port)

    await controller.on_request(_approval_request("req-1", tool_name="read"))
    await controller.on_request(_approval_request("req-2", tool_name="write"))
    await controller.on_request(_approval_request("req-3", tool_name="rm"))

    assert [dialog.request_id for dialog in port.dialogs] == ["req-1"]
    await controller.on_reviewed(ApprovalReviewed(request_id="req-2", approved=True, reason="safe"))
    await controller.on_reviewed(ApprovalReviewed(request_id="req-3", approved=False, reason="danger"))

    port.dialogs[0].callback((True, "", None))

    assert [dialog.request_id for dialog in port.dialogs] == ["req-1", "req-3"]
    assert port.dialogs[1].opened_with == (False, "danger")
    assert port.dialogs[1].verdicts == []
    assert controller.pending_verdicts == {}
    assert controller.dialog_open is True


@pytest.mark.parametrize("approved", [True, False])
def test_approval_controller_forwards_the_dialog_remember_choice_only_with_approval(approved: bool) -> None:
    port = _ApprovalPort()
    controller = ApprovalQueueController(port)
    asyncio.run(controller.on_request(_approval_request("req-1", judging=False)))
    port.dialogs[0].remember_choice = "EXACT_SESSION"

    port.dialogs[0].callback((approved, "", None))

    assert port.remember_choices == ["EXACT_SESSION" if approved else ""]


def test_approval_controller_bypass_does_not_open_dialog() -> None:
    port = _ApprovalPort()
    port.bypass = ApprovalBypassDecision(approved=True, reason="identical", debug_reason="no change")
    controller = ApprovalQueueController(port)

    asyncio.run(controller.on_request(_approval_request("req-1", judging=False)))

    assert port.dialogs == []
    assert port.responses == [("req-1", True, "identical", None)]
    assert port.debug_calls == [("ApprovalRequest", "zsh (skipped dialog: no change)")]


def test_approval_controller_cancellation_dequeues_waiting_request_without_response() -> None:
    port = _ApprovalPort()
    controller = ApprovalQueueController(port)
    asyncio.run(controller.on_request(_approval_request("open", judging=False)))
    asyncio.run(controller.on_request(_approval_request("queued", judging=False)))

    asyncio.run(controller.on_cancelled(ApprovalCancelled(request_id="queued")))

    assert [request.request_id for request in controller.queue] == []
    assert port.cancelled_dialogs == []
    assert port.responses == []


def test_approval_controller_cancellation_dismisses_open_request_and_advances_queue() -> None:
    port = _ApprovalPort()
    controller = ApprovalQueueController(port)
    asyncio.run(controller.on_request(_approval_request("open", judging=False)))
    asyncio.run(controller.on_request(_approval_request("next", judging=False)))

    asyncio.run(controller.on_cancelled(ApprovalCancelled(request_id="open")))

    assert port.cancelled_dialogs == ["open"]
    assert [dialog.request_id for dialog in port.dialogs] == ["open", "next"]
    assert port.responses == []
    assert controller.dialog_open is True


def test_approval_controller_cancellation_wins_race_with_auto_verdict() -> None:
    port = _ApprovalPort()
    controller = ApprovalQueueController(port)
    asyncio.run(controller.on_request(_approval_request("race")))

    asyncio.run(controller.on_reviewed(ApprovalReviewed(request_id="race", approved=True, reason="safe")))
    asyncio.run(controller.on_cancelled(ApprovalCancelled(request_id="race")))
    asyncio.run(controller.on_reviewed(ApprovalReviewed(request_id="race", approved=True, reason="later")))

    assert port.cancelled_dialogs == ["race"]
    assert port.responses == []
    assert controller.pending_verdicts == {}
    assert controller.dialog_open is False


# ──── ui.approval.defer_while_judging: nothing shows while the judge reviews ────


def _deferring_controller() -> tuple[_ApprovalPort, ApprovalQueueController]:
    port = _ApprovalPort()
    port.defer_while_judging = True
    return port, ApprovalQueueController(port)


def test_a_judging_request_waits_out_of_sight_and_is_counted() -> None:
    port, controller = _deferring_controller()

    asyncio.run(controller.on_request(_approval_request("req-1")))

    assert port.dialogs == []
    assert port.notifications == 0
    assert list(controller.deferred) == ["req-1"]
    assert list(controller.queue) == []
    assert controller.dialog_open is False
    assert port.review_counts == [1]


def test_a_request_the_judge_approves_is_never_shown() -> None:
    port, controller = _deferring_controller()
    port.body = object()
    asyncio.run(controller.on_request(_approval_request("req-1")))

    asyncio.run(controller.on_reviewed(ApprovalReviewed(request_id="req-1", approved=True, reason="safe")))

    assert port.dialogs == []
    assert port.notifications == 0
    # The judge's own auto-fulfilment answers the backend; the TUI adds nothing.
    assert port.responses == []
    assert port.auto_fulfill_blocked == []
    assert controller.deferred == {}
    assert controller.bodies == {}
    assert controller.pending_verdicts == {}
    assert port.review_counts == [1, 0]


def test_a_request_the_judge_flags_opens_flagged_and_notifies_once() -> None:
    port, controller = _deferring_controller()
    port.body = body = object()
    asyncio.run(controller.on_request(_approval_request("req-1")))

    asyncio.run(controller.on_reviewed(ApprovalReviewed(request_id="req-1", approved=False, reason="danger")))

    (dialog,) = port.dialogs
    assert (dialog.request_id, dialog.body, dialog.opened_with) == ("req-1", body, (False, "danger"))
    assert dialog.verdicts == []
    assert port.notifications == 1
    assert controller.deferred == {}
    assert controller.pending_verdicts == {}
    assert controller.dialog_open is True
    assert port.review_counts == [1, 0]

    dialog.callback((True, "", None))

    assert port.responses == [("req-1", True, "", None)]
    assert controller.dialog_open is False


def test_with_the_setting_off_a_judging_request_shows_at_once() -> None:
    port = _ApprovalPort()
    controller = ApprovalQueueController(port)

    asyncio.run(controller.on_request(_approval_request("req-1")))
    asyncio.run(controller.on_reviewed(ApprovalReviewed(request_id="req-1", approved=False, reason="danger")))

    (dialog,) = port.dialogs
    assert dialog.opened_with is None
    assert dialog.verdicts == [(False, "danger")]
    assert port.notifications == 1
    assert controller.deferred == {}
    assert port.review_counts == []


def test_a_request_nobody_judges_shows_at_once_with_the_setting_on() -> None:
    port, controller = _deferring_controller()

    asyncio.run(controller.on_request(_approval_request("req-1", judging=False)))

    assert [dialog.request_id for dialog in port.dialogs] == ["req-1"]
    assert port.notifications == 1
    assert controller.deferred == {}
    assert port.review_counts == []


def test_the_setting_is_read_once_per_request() -> None:
    """Turning the setting off leaves requests already out of sight there until their verdict."""
    port, controller = _deferring_controller()
    asyncio.run(controller.on_request(_approval_request("before")))
    port.defer_while_judging = False

    asyncio.run(controller.on_request(_approval_request("after")))
    asyncio.run(controller.on_reviewed(ApprovalReviewed(request_id="before", approved=True, reason="safe")))

    assert [dialog.request_id for dialog in port.dialogs] == ["after"]
    assert controller.deferred == {}
    assert port.review_counts == [1, 0]


def test_cancelling_a_request_out_of_sight_drops_it_and_its_late_verdict() -> None:
    port, controller = _deferring_controller()
    port.body = object()
    asyncio.run(controller.on_request(_approval_request("req-1")))

    asyncio.run(controller.on_cancelled(ApprovalCancelled(request_id="req-1")))
    asyncio.run(controller.on_reviewed(ApprovalReviewed(request_id="req-1", approved=False, reason="late")))

    assert port.dialogs == []
    assert port.responses == []
    assert port.cancelled_dialogs == []
    assert controller.deferred == {}
    assert controller.bodies == {}
    assert controller.pending_verdicts == {}
    assert list(controller.queue) == []
    assert port.review_counts == [1, 0]


async def test_flags_open_in_verdict_order_one_dialog_at_a_time() -> None:
    port, controller = _deferring_controller()
    await controller.on_request(_approval_request("first", tool_name="rm"))
    await controller.on_request(_approval_request("second", tool_name="mv"))

    await controller.on_reviewed(ApprovalReviewed(request_id="second", approved=False, reason="b"))
    await controller.on_reviewed(ApprovalReviewed(request_id="first", approved=False, reason="a"))

    assert [(dialog.request_id, dialog.opened_with) for dialog in port.dialogs] == [("second", (False, "b"))]
    assert port.review_counts == [1, 2, 1, 0]

    port.dialogs[0].callback((False, "", None))

    assert [(dialog.request_id, dialog.opened_with) for dialog in port.dialogs] == [
        ("second", (False, "b")),
        ("first", (False, "a")),
    ]
    assert port.notifications == 2


async def test_a_flag_waits_behind_the_open_dialog() -> None:
    port, controller = _deferring_controller()
    await controller.on_request(_approval_request("asked", judging=False))
    await controller.on_request(_approval_request("judged"))

    await controller.on_reviewed(ApprovalReviewed(request_id="judged", approved=False, reason="danger"))

    assert [dialog.request_id for dialog in port.dialogs] == ["asked"]

    port.dialogs[0].callback((True, "", None))

    assert [(dialog.request_id, dialog.opened_with) for dialog in port.dialogs] == [
        ("asked", None),
        ("judged", (False, "danger")),
    ]


def test_a_verdict_for_an_unknown_request_changes_nothing() -> None:
    port, controller = _deferring_controller()
    asyncio.run(controller.on_request(_approval_request("req-1")))

    asyncio.run(controller.on_reviewed(ApprovalReviewed(request_id="other", approved=False, reason="danger")))

    assert port.dialogs == []
    assert list(controller.deferred) == ["req-1"]
    assert port.review_counts == [1]


class _QuestionPort:
    def __init__(self) -> None:
        self.dialogs: list[SimpleNamespace] = []
        self.inline_results: dict[str, bool] = {}
        self.inline_calls: list[tuple[str, PromptDraft | None]] = []
        self.inline_clears: list[tuple[str, str]] = []
        self.running_results: dict[str, bool] = {}
        self.responses: list[tuple[str, tuple[AskUserAnswer, ...]]] = []
        self.notifications = 0
        self.focus_calls = 0
        self.inline_preferred = False

    def show_question_dialog(
        self,
        event: QuestionToUser,
        draft: PromptDraft | None,
        on_result: Callable[[object], None],
    ) -> SimpleNamespace:
        dialog = SimpleNamespace(
            request_id=event.request_id,
            draft=draft,
            callback=on_result,
            timed_out=False,
            dismiss_due_to_timeout=lambda: setattr(dialog, "timed_out", True),
        )
        self.dialogs.append(dialog)
        return dialog

    def parse_question_dialog_result(self, result: object):
        return result

    def show_question_inline(self, event: QuestionToUser, draft: PromptDraft | None = None) -> bool:
        self.inline_calls.append((event.request_id, draft))
        return self.inline_results.get(event.request_id, False)

    def clear_question_inline(self, call_id: str, request_id: str) -> bool:
        self.inline_clears.append((call_id, request_id))
        return True

    def question_can_reopen_modal(self, event: QuestionToUser) -> bool:
        return self.running_results.get(event.request_id, True)

    def question_inline_preferred(self) -> bool:
        return self.inline_preferred

    def handle_ask_user_response(self, request_id: str, answers: tuple[AskUserAnswer, ...]) -> None:
        self.responses.append((request_id, answers))

    def debug(self, _key: str, _message: str = "") -> None:
        return

    def notify_ask_user(self) -> None:
        self.notifications += 1

    def focus_input(self) -> None:
        self.focus_calls += 1


def test_question_controller_hands_question_inline_then_clears_on_tool_result() -> None:
    port = _QuestionPort()
    port.inline_results["q1"] = True
    controller = QuestionQueueController(port)

    asyncio.run(controller.on_question(_question_event("q1", "Need input?", call_id="c1")))
    draft = PromptDraft(drafts=("draft",))
    port.dialogs[0].callback(InlineQuestionDialogResult(draft=draft))

    assert port.inline_calls == [("q1", draft)], "the modal draft travels to the tool card"
    assert controller.inline_call_ids == {"q1": "c1"}
    assert controller.inline_request_ids == {"c1": "q1"}
    assert controller.finish_inline_for_tool_result("c1") is True
    assert controller.inline_call_ids == {}
    assert controller.inline_request_ids == {}
    assert port.focus_calls == 1


def test_question_controller_goes_inline_first_when_the_setting_asks_for_it() -> None:
    port = _QuestionPort()
    port.inline_preferred = True
    port.inline_results["q1"] = True
    controller = QuestionQueueController(port)

    asyncio.run(controller.on_question(_question_event("q1", "Need input?", call_id="c1")))

    assert port.dialogs == [], "no modal when the tool card hosts the question"
    assert port.inline_calls == [("q1", None)]
    assert controller.inline_call_ids == {"q1": "c1"}
    assert controller.inline_request_ids == {"c1": "q1"}
    assert controller.dialog_open is False
    assert port.notifications == 1
    assert controller.finish_inline_for_tool_result("c1") is True


def test_question_controller_hands_the_keyboard_back_only_after_the_last_inline_question() -> None:
    port = _QuestionPort()
    port.inline_preferred = True
    port.inline_results.update({"q1": True, "q2": True})
    controller = QuestionQueueController(port)

    asyncio.run(controller.on_question(_question_event("q1", "One?", call_id="c1")))
    asyncio.run(controller.on_question(_question_event("q2", "Two?", call_id="c2")))
    assert controller.inline_request_ids == {"c1": "q1", "c2": "q2"}

    assert controller.finish_inline_for_tool_result("c1") is True
    assert port.focus_calls == 0, "the second card is still waiting for its answer"
    assert controller.finish_inline_for_tool_result("c2") is True
    assert port.focus_calls == 1


def test_question_controller_falls_back_to_the_modal_when_inline_is_preferred_but_unavailable() -> None:
    port = _QuestionPort()
    port.inline_preferred = True
    port.inline_results["q1"] = False
    controller = QuestionQueueController(port)

    asyncio.run(controller.on_question(_question_event("q1", "Need input?", call_id="c1")))

    assert [dialog.request_id for dialog in port.dialogs] == ["q1"]
    assert controller.inline_call_ids == {}
    assert controller.dialog_open is True


def test_question_controller_reopens_modal_with_draft_when_inline_handoff_fails() -> None:
    port = _QuestionPort()
    port.inline_results["q1"] = False
    port.running_results["q1"] = True
    controller = QuestionQueueController(port)

    asyncio.run(controller.on_question(_question_event("q1", "Need input?", call_id="c1")))
    draft = PromptDraft(drafts=("draft",))
    port.dialogs[0].callback(InlineQuestionDialogResult(draft=draft))

    assert len(port.dialogs) == 2
    assert port.dialogs[1].request_id == "q1"
    assert port.dialogs[1].draft == draft


def test_question_controller_inline_timeout_hands_the_keyboard_back_after_the_last_question() -> None:
    port = _QuestionPort()
    port.inline_preferred = True
    port.inline_results.update({"q1": True, "q2": True})
    controller = QuestionQueueController(port)

    asyncio.run(controller.on_question(_question_event("q1", "One?", call_id="c1")))
    asyncio.run(controller.on_question(_question_event("q2", "Two?", call_id="c2")))

    asyncio.run(controller.on_timed_out(AskUserTimedOut(request_id="q1")))
    assert port.inline_clears == [("c1", "q1")]
    assert port.focus_calls == 0, "the second card is still waiting for its answer"
    assert controller.inline_request_ids == {"c2": "q2"}
    asyncio.run(controller.on_timed_out(AskUserTimedOut(request_id="q2")))
    assert port.inline_clears == [("c1", "q1"), ("c2", "q2")]
    assert port.focus_calls == 1
    assert controller.inline_request_ids == {}
    assert controller.finish_inline_for_tool_result("c1") is False


def test_old_inline_timeout_does_not_clear_reused_call_id_request() -> None:
    port = _QuestionPort()
    port.inline_preferred = True
    port.inline_results.update({"old": True, "new": True})
    controller = QuestionQueueController(port)

    asyncio.run(controller.on_question(_question_event("old", "Old?", call_id="shared")))
    asyncio.run(controller.on_question(_question_event("new", "New?", call_id="shared")))
    assert controller.inline_request_ids == {"shared": "new"}

    asyncio.run(controller.on_timed_out(AskUserTimedOut(request_id="old")))

    assert port.inline_clears == []
    assert controller.inline_request_ids == {"shared": "new"}
    assert controller.inline_call_ids == {"new": "shared"}


def test_question_controller_records_text_response_and_timeout_paths() -> None:
    port = _QuestionPort()
    controller = QuestionQueueController(port)

    asyncio.run(controller.on_question(_question_event("q1", "Need input?")))
    answers = (AskUserAnswer(values=("answer",)),)
    port.dialogs[0].callback(AnswerQuestionDialogResult(request_id="q1", answers=answers))
    assert port.responses == [("q1", answers)]

    asyncio.run(controller.on_question(_question_event("q2", "Second?")))
    asyncio.run(controller.on_timed_out(AskUserTimedOut(request_id="q2")))
    assert port.dialogs[1].timed_out is True


class _FakeLoadDialog:
    COMPLETE_STEP_HOLD_SECONDS = 0

    def __init__(self, *, title: str, subtitle: str) -> None:
        self.title = title
        self.subtitle = subtitle
        self.progress: list[tuple[str, dict[str, object]]] = []
        self.finish_progress: list[str] = []
        self.finished: list[str] = []
        self.dismiss_requested = False
        self.result: tuple[bool, str, bool] | None = None

    def update_progress(self, message: MessageRef | str, **kwargs: object) -> bool:
        self.progress.append((_display_message(message), kwargs))
        return False

    def update_finish_progress(self, message: MessageRef | str) -> None:
        self.finish_progress.append(_display_message(message))

    def finish(self, message: MessageRef | str = "") -> None:
        self.finished.append(_display_message(message))

    def request_dismiss(self) -> None:
        self.dismiss_requested = True

    def set_result(self, success: bool, message: MessageRef | str, *, allow_esc: bool = False) -> None:
        self.result = (success, _display_message(message), allow_esc)


class _LoadPort:
    def __init__(self) -> None:
        self.dialogs: list[_FakeLoadDialog] = []
        self.status_messages: list[str] = []
        self.loading_states: list[bool] = []
        self.flashed: list[str] = []
        self.pushed = 0

    def create_agent_load_dialog(self, *, title: MessageRef | str, subtitle: str) -> _FakeLoadDialog:
        dialog = _FakeLoadDialog(title=_display_message(title), subtitle=subtitle)
        self.dialogs.append(dialog)
        return dialog

    def prepare_agent_load_ui(self, **_kwargs: object) -> dict:
        self.loading_states.append(True)
        return {"status": "before"}

    async def push_agent_load_dialog(self, _dialog: SimpleNamespace) -> None:
        self.pushed += 1

    def set_agent_loading(self, value: bool) -> None:
        self.loading_states.append(value)

    def restore_agent_load_status(self, _snapshot: dict) -> None:
        return

    def show_load_status(self, message: MessageRef | str) -> None:
        self.status_messages.append(_display_message(message))

    def render_status_message(self, message: MessageRef | str) -> str:
        return _display_message(message)

    def flash_agent_load_failed(self, message: str) -> None:
        self.flashed.append(message)

    def debug(self, _key: str, _message: str = "") -> None:
        return


def test_agent_load_controller_reuses_dialog_and_formats_progress() -> None:
    port = _LoadPort()
    controller = AgentLoadDialogController(port)

    asyncio.run(
        controller.show_dialog(
            title="Switching Agent",
            subtitle="Code",
            session_id="s1",
            initial_message="Preparing agent",
        )
    )
    asyncio.run(
        controller.on_progress(
            AgentLoadProgress(
                phase="mcp",
                message="Connected MCP server fs",
                current=1,
                total=2,
                status=AGENT_LOAD_STATUS_DONE,
            )
        )
    )
    asyncio.run(
        controller.show_dialog(
            title="Switching Agent",
            subtitle="Code",
            session_id="s1",
            initial_message="Still preparing",
        )
    )

    assert len(port.dialogs) == 1
    assert port.pushed == 1
    assert port.status_messages == ["Connected MCP server fs (1/2)"]
    assert port.dialogs[0].progress[0][0] == "Preparing agent"
    assert port.dialogs[0].progress[1][1]["status"] == AGENT_LOAD_STATUS_DONE
    assert port.dialogs[0].progress[-1][0] == "Still preparing"


def test_agent_load_controller_count_suffix_is_status_driven_for_zero_counts() -> None:
    port = _LoadPort()
    controller = AgentLoadDialogController(port)

    asyncio.run(controller.on_progress(AgentLoadProgress(phase="skills", message="Skills loaded")))
    asyncio.run(
        controller.on_progress(
            AgentLoadProgress(phase="skills", message="Skills loaded", status=AGENT_LOAD_STATUS_DONE)
        )
    )
    asyncio.run(
        controller.on_progress(
            AgentLoadProgress(phase="agent", message="opaque completion", status=AGENT_LOAD_STATUS_DONE)
        )
    )
    asyncio.run(
        controller.on_progress(
            AgentLoadProgress(phase="mcp", message="Connected MCP server fs", current=1, total=2, failed=1)
        )
    )

    assert port.status_messages == [
        "Skills loaded",
        "Skills loaded (-)",
        "opaque completion (-)",
        "Connected MCP server fs (1/2, failed: 1)",
    ]


def test_agent_load_controller_formats_session_history_percentage() -> None:
    port = _LoadPort()
    controller = AgentLoadDialogController(port)
    dialog = _FakeLoadDialog(title="Restoring Session", subtitle="session")
    controller.dialog = dialog

    controller.update_session_history_progress(32, 40)
    controller.update_session_history_progress(40, 40)

    assert dialog.finish_progress == ["Restoring session history (80%)", "Restoring session history (100%)"]
    assert port.status_messages == ["Restoring session history (80%)", "Restoring session history (100%)"]


def test_agent_load_controller_surfaces_mcp_tool_collision_message() -> None:
    port = _LoadPort()
    controller = AgentLoadDialogController(port)
    dialog = _FakeLoadDialog(title="Loading Agent", subtitle="Code")
    controller.dialog = dialog
    message = "MCP server 'github' exposes permitted tool name collision with a reserved iCode tool: read_file"

    controller.on_failed(AgentLoadFailed(operation="switch", agent_profile="Code", message=message))

    assert dialog.result == (False, message, True)
    assert controller.dialog is None
    assert port.flashed == [message]


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        pytest.param("Connection error.", "Can't resolve api.example.com.\nConnection error.", id="with-raw-text"),
        pytest.param("", "Can't resolve api.example.com.", id="without-raw-text"),
    ],
)
def test_agent_load_controller_shows_the_display_above_the_raw_text(raw: str, shown: str) -> None:
    port = _LoadPort()
    controller = AgentLoadDialogController(port)
    dialog = _FakeLoadDialog(title="Loading Agent", subtitle="Code")
    controller.dialog = dialog

    controller.on_failed(
        AgentLoadFailed(operation="switch", agent_profile="Code", message=raw),
        display="Can't resolve api.example.com.",
    )

    assert dialog.result == (False, shown, True)
    # The status bar has room for one line: the display alone.
    assert port.flashed == ["Can't resolve api.example.com."]


def test_agent_load_controller_flashes_the_summary_without_the_hint() -> None:
    port = _LoadPort()
    controller = AgentLoadDialogController(port)
    dialog = _FakeLoadDialog(title="Loading Agent", subtitle="Code")
    controller.dialog = dialog
    display = "Can't resolve api.example.com. Check your network connection first."

    controller.on_failed(
        AgentLoadFailed(operation="switch", agent_profile="Code", message="Connection error."),
        display=display,
        summary="Can't resolve api.example.com.",
    )

    assert dialog.result == (False, f"{display}\nConnection error.", True)
    assert port.flashed == ["Can't resolve api.example.com."]


def test_agent_load_controller_cancel_requests_stack_safe_dismissal() -> None:
    port = _LoadPort()
    controller = AgentLoadDialogController(port)
    dialog = _FakeLoadDialog(title="Loading Agent", subtitle="Code")
    controller.dialog = dialog

    controller.cancel()

    assert dialog.dismiss_requested is True
    assert controller.dialog is None
    assert port.loading_states == [False]


class _ImagePort:
    def __init__(self) -> None:
        self.dialogs: list[SimpleNamespace] = []
        self.pushed = 0

    def create_image_compression_dialog(self, *, title: MessageRef | str) -> SimpleNamespace:
        dialog = SimpleNamespace(
            title=_display_message(title),
            finished=False,
            finish=lambda: setattr(dialog, "finished", True),
        )
        self.dialogs.append(dialog)
        return dialog

    async def push_image_compression_dialog(self, _dialog: SimpleNamespace) -> None:
        self.pushed += 1

    def debug(self, _key: str, _message: str = "") -> None:
        return


def test_image_compression_controller_opens_once_and_finishes() -> None:
    port = _ImagePort()
    controller = ImageCompressionDialogController(port)

    asyncio.run(controller.on_started(ImageAttachmentCompressionStarted(image_count=2)))
    asyncio.run(controller.on_started(ImageAttachmentCompressionStarted(image_count=2)))
    assert len(port.dialogs) == 1
    assert port.dialogs[0].title == "Preparing Images"
    assert port.pushed == 1

    asyncio.run(controller.on_finished(ImageAttachmentCompressionFinished(image_count=2)))
    assert port.dialogs[0].finished is True
    assert controller.dialog is None
