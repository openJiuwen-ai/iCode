# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for OSC 7501 (Program Status Protocol) reporting."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from chrys.app.tui.terminal.program_status import (
    ProgramStatusReporter,
    build_program_status_report,
    emit_program_status_report,
    program_status_message,
)
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ApprovalAutoFulfillBlocked,
    ApprovalCancelled,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalReviewed,
    AskUserResponse,
    AskUserTimedOut,
    Error,
    ExecutionChanged,
    InvocationAborted,
    InvocationCascadeAborted,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationMessage,
    InvocationPaused,
    InvocationResumed,
    QuestionToUser,
    SessionReady,
    SessionRestored,
    UserInterrupt,
    WorkflowNodeAnswered,
    WorkflowNodeAskUser,
    WorkflowNodeStateChanged,
    WorkflowRunFinished,
)
from chrys.foundation.models.ask_user import AskUserQuestion
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.models.invocations import InvocationOrigin
from tests.support.tui_app_harness import make_chrys_app

if TYPE_CHECKING:
    from chrys.foundation.events.types import Event

_ST = "\x1b\\"


class _Driver:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.writes.append(data)


class _FailingDriver:
    def write(self, _data: str) -> None:
        raise OSError("terminal went away")


class _App:
    def __init__(self, driver: object | None = None) -> None:
        self._driver = driver if driver is not None else _Driver()

    @property
    def writes(self) -> list[str]:
        driver = self._driver
        assert isinstance(driver, _Driver)
        return driver.writes


class _ForwardingDriver:
    """Record writes and forward everything to the real driver."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self._inner_write = inner.write
        self.writes: list[str] = []

    def write(self, data: str) -> None:
        self.writes.append(data)
        self._inner_write(data)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def _seq(pairs: str) -> str:
    return f"\x1b]7501;{pairs}{_ST}"


def _msg(text: str) -> str:
    encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return f"msg={encoded}"


def _blocked(kind: str, text: str) -> str:
    return _seq(f"state=blocked:kind={kind}:app=iCode:{_msg(text)}")


_IDLE = _seq("state=idle:app=iCode")
_WORKING = _seq("state=working:app=iCode")
_DONE = _seq("state=done:app=iCode")


def _turn_origin(invocation_id: str = "i1") -> InvocationOrigin:
    return InvocationOrigin(kind="turn", session_id="s1", invocation_id=invocation_id, parent=None)


def _sub_agent_origin(invocation_id: str = "i2") -> InvocationOrigin:
    return InvocationOrigin(kind="sub_agent", session_id="s1", invocation_id=invocation_id, parent=_turn_origin())


def _workflow_node_origin() -> InvocationOrigin:
    return InvocationOrigin(kind="workflow_node", session_id="s1", invocation_id="i3", parent=None)


async def _make_reporter(monkeypatch: pytest.MonkeyPatch, app: _App | None = None) -> tuple[EventBus, list[str]]:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    bus = EventBus()
    holder = app if app is not None else _App()
    reporter = ProgramStatusReporter(bus, app_provider=lambda: holder)
    await reporter.subscribe()
    return bus, holder.writes


async def _publish_all(bus: EventBus, events: tuple[Event, ...]) -> None:
    for event in events:
        await bus.publish(event)


# ------------------------------------------------------------------ #
# Report encoding
# ------------------------------------------------------------------ #


def test_report_carries_state_and_app_only() -> None:
    assert build_program_status_report("working") == _WORKING


def test_report_orders_kind_before_app_and_msg() -> None:
    report = build_program_status_report("blocked", kind="permission", message="hello")

    assert report == _seq("state=blocked:kind=permission:app=iCode:msg=aGVsbG8=")


def test_report_encodes_utf8_message_as_base64() -> None:
    report = build_program_status_report("done", message="代码完成")

    assert _msg("代码完成") in report


def test_message_takes_the_first_non_blank_line() -> None:
    assert program_status_message("All done\nsecond line") == "All done"
    assert program_status_message("\n\n  \nfirst\nrest") == "first"


def test_message_collapses_whitespace_and_strips_control_characters() -> None:
    assert program_status_message("line1 line2\x07c\x9c\x1b[31mend") == "line1 line2cend"


def test_message_strips_surrogates_and_format_characters() -> None:
    assert program_status_message("a\udcffb​c") == "abc"


def test_message_is_capped_at_200_characters() -> None:
    text = "x" * 250

    assert program_status_message(text) == "x" * 200


def test_message_of_only_control_characters_is_empty() -> None:
    assert program_status_message("\x07\x1b[31m\x9c") == ""


def test_emit_writes_through_app_driver_and_reports_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    app = _App()

    assert emit_program_status_report(app, "working") is True
    assert app.writes == [_WORKING]


def test_emit_reports_a_failed_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    app = _App(_FailingDriver())

    assert emit_program_status_report(app, "working") is False


def test_emit_without_driver_is_noop(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)

    assert emit_program_status_report(object(), "working") is False
    assert capsys.readouterr().err == ""


def test_emit_skips_textual_web_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEXTUAL_DRIVER", "textual.drivers.web_driver:WebDriver")
    app = _App()

    assert emit_program_status_report(app, "working") is False
    assert app.writes == []


# ------------------------------------------------------------------ #
# Single-wait and run transitions
# ------------------------------------------------------------------ #


async def test_subscribe_establishes_idle_record(monkeypatch: pytest.MonkeyPatch) -> None:
    _bus, writes = await _make_reporter(monkeypatch)

    assert writes == [_IDLE]


async def test_turn_runs_working_then_done_survives_idle_release(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            InvocationMessage(origin=_turn_origin(), text="All done\nsecond line", is_final=True),
            ExecutionChanged(snapshot=ExecutionSnapshot("idle")),
        ),
    )

    assert writes == [_IDLE, _WORKING, _seq(f"state=done:app=iCode:{_msg('All done')}")]


async def test_interrupt_replaces_outcome_with_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn", cancellable=True)),
            UserInterrupt(),
            ExecutionChanged(snapshot=ExecutionSnapshot("idle")),
        ),
    )

    assert writes == [_IDLE, _WORKING, _IDLE]


async def test_interrupt_replaces_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            Error(code="executor_error", message="provider unreachable"),
            UserInterrupt(),
        ),
    )

    assert writes == [_IDLE, _WORKING, _seq(f"state=error:app=iCode:{_msg('provider unreachable')}"), _IDLE]


async def test_stale_terminal_events_after_an_interrupt_do_not_flip_it(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            UserInterrupt(),
            # Racing the interrupt's teardown, while the lease still reads turn.
            InvocationMessage(origin=_turn_origin(), text="late done", is_final=True),
            Error(code="executor_error", message="late failure"),
        ),
    )

    assert writes == [_IDLE, _WORKING, _IDLE]


async def test_new_run_does_not_show_the_previous_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            Error(code="executor_error", message="provider unreachable"),
            ExecutionChanged(snapshot=ExecutionSnapshot("idle")),
            # The user retries: a fresh run starts from the failed turn.
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
        ),
    )

    # The retry runs as working again; the failed turn's error is gone.
    assert writes == [
        _IDLE,
        _WORKING,
        _seq(f"state=error:app=iCode:{_msg('provider unreachable')}"),
        _WORKING,
    ]


async def test_workflow_lease_reports_working(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("workflow", run_id="r1")),
            ExecutionChanged(snapshot=ExecutionSnapshot("idle")),
        ),
    )

    assert writes == [_IDLE, _WORKING, _IDLE]


async def test_sub_agent_final_keeps_parent_working(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            InvocationMessage(origin=_sub_agent_origin(), text="child done", is_final=True),
        ),
    )

    assert writes == [_IDLE, _WORKING]


async def test_workflow_node_final_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("workflow", run_id="r1")),
            InvocationMessage(origin=_workflow_node_origin(), text="node done", is_final=True),
        ),
    )

    assert writes == [_IDLE, _WORKING]


async def test_streaming_fragments_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            InvocationMessage(origin=_turn_origin(), text="part", is_final=False),
            InvocationContentDelta(origin=_turn_origin(), segment_id="s1", text="part"),
            InvocationContentFinished(origin=_turn_origin(), segment_id="s1"),
        ),
    )

    assert writes == [_IDLE, _WORKING]


async def test_live_error_reports_error_with_first_line(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            Error(code="executor_error", message="provider unreachable\nretry budget spent"),
        ),
    )

    assert writes[-1] == _seq(f"state=error:app=iCode:{_msg('provider unreachable')}")


@pytest.mark.parametrize(
    "code",
    [
        "manual_settings_reload_failed",
        "manual_settings_reload_denied",
        "hook_blocked",
        "image_attachment_error",
        "vision_unsupported",
    ],
)
async def test_rejected_operation_error_keeps_the_run(monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            Error(code=code, message="operation was rejected"),
            UserInterrupt(),
        ),
    )

    assert writes == [_IDLE, _WORKING, _IDLE]


async def test_error_outside_a_live_run_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await bus.publish(Error(code="image_attachment_blocked", message="too large"))

    assert writes == [_IDLE]


async def test_identical_states_are_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn", request_id="t1")),
            ExecutionChanged(snapshot=ExecutionSnapshot("turn", request_id="t1")),
        ),
    )

    assert writes == [_IDLE, _WORKING]


# ------------------------------------------------------------------ #
# Concurrent waits
# ------------------------------------------------------------------ #


async def test_two_approvals_stay_blocked_until_both_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            ApprovalRequest(request_id="a1", tool_name="shell_command"),
            ApprovalRequest(request_id="a2", tool_name="file_edit"),
            ApprovalResponse(request_id="a1", approved=True),
        ),
    )

    # a1 answered, a2 still waits: blocked names a2 now.
    assert writes == [_IDLE, _WORKING, _blocked("permission", "shell_command"), _blocked("permission", "file_edit")]
    await bus.publish(ApprovalResponse(request_id="a2", approved=False))

    assert writes[-1] == _WORKING


async def test_approval_message_is_the_tool_name_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            # intent_summary stays empty for ordinary approvals; the user's
            # prompt must never leak in as the wait's message.
            ApprovalRequest(request_id="a1", tool_name="shell_command", intent_summary="", user_message="fix the bug"),
        ),
    )

    assert writes[-1] == _blocked("permission", "shell_command")


async def test_judged_approval_waits_for_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            ApprovalRequest(request_id="a1", tool_name="shell_command", judging=True),
            ApprovalReviewed(request_id="a1", approved=True),
        ),
    )

    assert writes == [_IDLE, _WORKING]


async def test_denied_verdict_blocks_with_the_tool_name_then_response_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            ApprovalRequest(request_id="a1", tool_name="shell_command", judging=True),
            ApprovalReviewed(request_id="a1", approved=False, reason="destructive command"),
            ApprovalResponse(request_id="a1", approved=False),
        ),
    )

    assert writes[-2] == _blocked("permission", "shell_command")
    assert writes[-1] == _WORKING


@pytest.mark.parametrize("approved", [True, False])
async def test_late_denied_verdict_after_the_user_answered_does_not_reopen(
    monkeypatch: pytest.MonkeyPatch, approved: bool
) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            ApprovalRequest(request_id="a1", tool_name="shell_command", judging=True),
            ApprovalResponse(request_id="a1", approved=approved),
            # The judge's verdict lost the race to the user's own decision.
            ApprovalReviewed(request_id="a1", approved=False, reason="destructive command"),
            ExecutionChanged(snapshot=ExecutionSnapshot("idle")),
        ),
    )

    # The verdict never reopened the wait; the lease release lands on idle.
    assert writes == [_IDLE, _WORKING, _IDLE]


async def test_late_denied_verdict_after_a_cancellation_does_not_reopen(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            ApprovalRequest(request_id="a1", tool_name="shell_command", judging=True),
            ApprovalCancelled(request_id="a1"),
            ApprovalReviewed(request_id="a1", approved=False, reason="destructive command"),
            ExecutionChanged(snapshot=ExecutionSnapshot("idle")),
        ),
    )

    # The verdict never reopened the wait; the lease release lands on idle.
    assert writes == [_IDLE, _WORKING, _IDLE]


async def test_cancelled_approval_resumes_working(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            ApprovalRequest(request_id="a1", tool_name="shell_command"),
            ApprovalCancelled(request_id="a1"),
        ),
    )

    assert writes[-1] == _WORKING


async def test_auto_fulfill_blocked_is_not_a_new_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            ApprovalAutoFulfillBlocked(request_id="a1"),
        ),
    )

    # The user's decision is already on its way (ApprovalResponse follows);
    # the reporter never treats this as another wait.
    assert writes == [_IDLE, _WORKING]


async def test_blocked_kind_and_message_follow_the_wait_source(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            ApprovalRequest(request_id="a1", tool_name="shell_command"),
            QuestionToUser(request_id="q1", questions=(AskUserQuestion(question="Proceed with the deploy?"),)),
            ApprovalResponse(request_id="a1", approved=True),
        ),
    )

    assert writes == [
        _IDLE,
        _WORKING,
        _blocked("permission", "shell_command"),
        # The question outranks the approval; kind and message switch with it.
        _blocked("question", "Proceed with the deploy?"),
        # The approval clearing leaves the question in charge, so no change.
    ]
    await bus.publish(AskUserResponse(request_id="q1"))

    assert writes[-1] == _WORKING


async def test_question_timeout_clears_the_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            QuestionToUser(request_id="q1", questions=(AskUserQuestion(question="Proceed?"),)),
            AskUserTimedOut(request_id="q1"),
        ),
    )

    assert writes[-1] == _WORKING


async def test_paused_sub_agent_blocks_without_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            InvocationPaused(origin=_sub_agent_origin(), agent_name="Explore", reason="stream_stall"),
        ),
    )

    assert writes[-1] == _seq(f"state=blocked:app=iCode:{_msg('Explore failed - retry or abort')}")


async def test_paused_sub_agent_without_a_name_still_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            InvocationPaused(origin=_sub_agent_origin(), reason="framework_exc"),
        ),
    )

    assert writes[-1] == _seq(f"state=blocked:app=iCode:{_msg('sub-agent failed - retry or abort')}")


async def test_two_paused_agents_block_until_both_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            InvocationPaused(origin=_sub_agent_origin("p1"), agent_name="Explore"),
            InvocationPaused(origin=_sub_agent_origin("p2"), agent_name="QA"),
            InvocationResumed(origin=_sub_agent_origin("p1"), agent_name="Explore"),
        ),
    )

    assert writes[-1] == _seq(f"state=blocked:app=iCode:{_msg('QA failed - retry or abort')}")
    await bus.publish(InvocationAborted(origin=_sub_agent_origin("p2"), agent_name="QA"))

    assert writes[-1] == _WORKING


async def test_cascade_aborted_sub_agent_then_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            InvocationPaused(origin=_sub_agent_origin(), agent_name="Explore"),
            InvocationCascadeAborted(origin=_sub_agent_origin(), agent_name="Explore"),
            ExecutionChanged(snapshot=ExecutionSnapshot("idle")),
        ),
    )

    assert writes[-2] == _WORKING
    assert writes[-1] == _IDLE


# ------------------------------------------------------------------ #
# Workflow runs
# ------------------------------------------------------------------ #


async def test_workflow_node_retry_blocks_and_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("workflow", run_id="r1")),
            WorkflowNodeStateChanged(
                run_id="r1", node_id="build", activation_id="act1", attempt=1, state="awaiting_retry"
            ),
            WorkflowNodeStateChanged(run_id="r1", node_id="build", activation_id="act1", attempt=2, state="running"),
        ),
    )

    assert writes[-2] == _seq(f"state=blocked:app=iCode:{_msg('build awaits manual retry')}")
    assert writes[-1] == _WORKING


async def test_workflow_ask_is_retired_when_the_attempt_settles(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("workflow", run_id="r1")),
            WorkflowNodeAskUser(
                run_id="r1",
                node_id="n1",
                activation_id="act1",
                attempt=1,
                request_id="q1",
                questions=(AskUserQuestion(question="Pick a region"),),
            ),
            # The ask times out with the attempt: no WorkflowNodeAnswered.
            WorkflowNodeStateChanged(run_id="r1", node_id="n1", activation_id="act1", attempt=1, state="failed"),
        ),
    )

    assert writes[-2] == _blocked("question", "Pick a region")
    assert writes[-1] == _WORKING


async def test_other_invocation_traffic_cannot_mask_an_open_ask(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("workflow", run_id="r1")),
            WorkflowNodeAskUser(
                run_id="r1",
                node_id="n1",
                activation_id="act1",
                attempt=1,
                request_id="q1",
                questions=(AskUserQuestion(question="Pick a region"),),
            ),
            # Node n2's retry resolution must not clear n1's question.
            WorkflowNodeStateChanged(run_id="r1", node_id="n2", activation_id="act2", attempt=3, state="running"),
            InvocationResumed(origin=_workflow_node_origin(), agent_name="node"),
        ),
    )

    assert writes[-1] == _blocked("question", "Pick a region")
    await bus.publish(WorkflowNodeAnswered(run_id="r1", node_id="n1", activation_id="act1", attempt=1, request_id="q1"))

    assert writes[-1] == _WORKING


async def test_workflow_completed_reports_done(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("workflow", run_id="r1")),
            WorkflowRunFinished(run_id="r1", outcome="completed"),
            ExecutionChanged(snapshot=ExecutionSnapshot("idle")),
        ),
    )

    assert writes == [_IDLE, _WORKING, _DONE]


async def test_workflow_node_failure_reports_error(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("workflow", run_id="r1")),
            WorkflowRunFinished(run_id="r1", outcome="node_failed", error="build step timed out\nmid line"),
            ExecutionChanged(snapshot=ExecutionSnapshot("idle")),
        ),
    )

    assert writes[-1] == _seq(f"state=error:app=iCode:{_msg('build step timed out')}")


async def test_workflow_deadline_reports_error(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("workflow", run_id="r1")),
            WorkflowRunFinished(run_id="r1", outcome="cancelled", reason="deadline_exceeded"),
        ),
    )

    assert writes[-1] == _seq("state=error:app=iCode")


async def test_workflow_user_cancellation_reports_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("workflow", run_id="r1")),
            WorkflowRunFinished(run_id="r1", outcome="cancelled"),
            ExecutionChanged(snapshot=ExecutionSnapshot("idle")),
        ),
    )

    assert writes == [_IDLE, _WORKING, _IDLE]


async def test_workflow_finished_clears_the_runs_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("workflow", run_id="r1")),
            WorkflowNodeAskUser(
                run_id="r1",
                node_id="n1",
                activation_id="act1",
                attempt=1,
                request_id="q1",
                questions=(AskUserQuestion(question="Pick a region"),),
            ),
            WorkflowRunFinished(run_id="r1", outcome="cancelled"),
        ),
    )

    assert writes[-2] == _blocked("question", "Pick a region")
    assert writes[-1] == _IDLE


# ------------------------------------------------------------------ #
# Session switches
# ------------------------------------------------------------------ #


async def test_new_session_clears_the_previous_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            SessionReady(session_id="s1"),
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            InvocationMessage(origin=_turn_origin(), text="All done", is_final=True),
            ExecutionChanged(snapshot=ExecutionSnapshot("idle")),
        ),
    )
    assert writes[-1] == _seq(f"state=done:app=iCode:{_msg('All done')}")

    # /new: a fresh session id lands with the next SessionReady.
    await bus.publish(SessionReady(session_id="s2"))

    assert writes[-1] == _IDLE


async def test_same_session_rebuild_keeps_the_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            SessionReady(session_id="s1"),
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            InvocationMessage(origin=_turn_origin(), text="All done", is_final=True),
            ExecutionChanged(snapshot=ExecutionSnapshot("idle")),
            # A rebuild echoes SessionReady for the same session (profile switch).
            SessionReady(session_id="s1"),
        ),
    )

    assert writes[-1] == _seq(f"state=done:app=iCode:{_msg('All done')}")


async def test_restored_session_clears_the_previous_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            InvocationMessage(origin=_turn_origin(), text="All done", is_final=True),
            ExecutionChanged(snapshot=ExecutionSnapshot("idle")),
            SessionRestored(session_id="s9"),
        ),
    )

    assert writes[-1] == _IDLE


# ------------------------------------------------------------------ #
# Delivery and shutdown
# ------------------------------------------------------------------ #


async def test_failed_write_is_resent_on_the_next_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    bus = EventBus()
    app = _App(_FailingDriver())
    reporter = ProgramStatusReporter(bus, app_provider=lambda: app)
    await reporter.subscribe()
    # The initial idle write failed; nothing was remembered as sent.

    driver = _Driver()
    app._driver = driver
    # The very same idle report must be re-derived and re-sent first: a
    # remember-before-write implementation skips it here.
    await bus.publish(ExecutionChanged(snapshot=ExecutionSnapshot("idle")))
    assert driver.writes == [_IDLE]

    await bus.publish(ExecutionChanged(snapshot=ExecutionSnapshot("turn")))
    await bus.publish(ExecutionChanged(snapshot=ExecutionSnapshot("idle")))

    assert driver.writes == [_IDLE, _WORKING, _IDLE]


async def test_close_reports_idle_then_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    bus = EventBus()
    app = _App()
    reporter = ProgramStatusReporter(bus, app_provider=lambda: app)
    await reporter.subscribe()
    await bus.publish(ExecutionChanged(snapshot=ExecutionSnapshot("turn")))
    await bus.publish(
        InvocationPaused(origin=_sub_agent_origin(), agent_name="Explore"),
    )
    app.writes.clear()

    await reporter.close()
    await bus.publish(ExecutionChanged(snapshot=ExecutionSnapshot("idle")))

    # A mid-run quit settles on idle and later shutdown events stay silent.
    assert app.writes == [_IDLE]


async def test_unsubscribe_stops_reporting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    bus = EventBus()
    app = _App()
    reporter = ProgramStatusReporter(bus, app_provider=lambda: app)
    await reporter.subscribe()
    app.writes.clear()

    await reporter.unsubscribe()
    await bus.publish(ExecutionChanged(snapshot=ExecutionSnapshot("turn")))

    assert app.writes == []


# ------------------------------------------------------------------ #
# ChrysApp wiring
# ------------------------------------------------------------------ #


async def test_chrys_app_reports_program_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    bus = EventBus()
    app = make_chrys_app(tmp_path, event_bus=bus)

    async with app.run_test() as pilot:
        capturing = _ForwardingDriver(app._driver)
        app._driver = capturing
        await pilot.pause()

        await bus.publish(ExecutionChanged(snapshot=ExecutionSnapshot("turn")))
        await bus.publish(ExecutionChanged(snapshot=ExecutionSnapshot("idle")))

        assert capturing.writes == [_WORKING, _IDLE]
