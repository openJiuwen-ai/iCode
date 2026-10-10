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
    InvocationMessage,
    QuestionToUser,
    WorkflowNodeAnswered,
    WorkflowNodeAskUser,
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


class _App:
    def __init__(self) -> None:
        self._driver = _Driver()


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


_IDLE = _seq("state=idle:app=icode")
_WORKING = _seq("state=working:app=icode")
_DONE = _seq("state=done:app=icode")


def _turn_origin() -> InvocationOrigin:
    return InvocationOrigin(kind="turn", session_id="s1", invocation_id="i1", parent=None)


def _sub_agent_origin() -> InvocationOrigin:
    return InvocationOrigin(kind="sub_agent", session_id="s1", invocation_id="i2", parent=_turn_origin())


def _workflow_node_origin() -> InvocationOrigin:
    return InvocationOrigin(kind="workflow_node", session_id="s1", invocation_id="i3", parent=None)


async def _make_reporter(monkeypatch: pytest.MonkeyPatch) -> tuple[EventBus, list[str]]:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    bus = EventBus()
    app = _App()
    reporter = ProgramStatusReporter(bus, app_provider=lambda: app)
    await reporter.subscribe()
    return bus, app._driver.writes


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

    assert report == _seq("state=blocked:kind=permission:app=icode:msg=aGVsbG8=")


def test_report_encodes_utf8_message_as_base64() -> None:
    report = build_program_status_report("done", message="代码完成")

    assert _msg("代码完成") in report


def test_message_collapses_whitespace_and_strips_control_characters() -> None:
    assert program_status_message("line1\nline2\x07c\x9c\x1b[31mend") == "line1 line2cend"


def test_message_strips_surrogates_and_format_characters() -> None:
    assert program_status_message("a\udcffb\u200bc") == "abc"


def test_message_is_capped_at_200_characters() -> None:
    text = "x" * 250

    assert program_status_message(text) == "x" * 200


def test_message_of_only_control_characters_is_empty() -> None:
    assert program_status_message("\x07\x1b[31m\x9c") == ""


def test_emit_writes_through_app_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    app = _App()

    emit_program_status_report(app, "working")

    assert app._driver.writes == [_WORKING]


def test_emit_without_driver_is_noop(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)

    emit_program_status_report(object(), "working")

    assert capsys.readouterr().err == ""


def test_emit_skips_textual_web_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEXTUAL_DRIVER", "textual.drivers.web_driver:WebDriver")
    app = _App()

    emit_program_status_report(app, "working")

    assert app._driver.writes == []


# ------------------------------------------------------------------ #
# Event fold
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

    assert writes == [_IDLE, _WORKING, _seq(f"state=done:app=icode:{_msg('All done second line')}")]


async def test_interrupt_releases_working_to_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn", cancellable=True)),
            ExecutionChanged(snapshot=ExecutionSnapshot("idle")),
        ),
    )

    assert writes == [_IDLE, _WORKING, _IDLE]


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
            InvocationMessage(origin=_turn_origin(), text="summary", is_intermediate=True),
        ),
    )

    assert writes == [_IDLE, _WORKING]


async def test_live_error_reports_error_with_message(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            Error(code="executor_error", message="provider unreachable\nretry budget spent"),
        ),
    )

    assert writes == [_IDLE, _WORKING, _seq(f"state=error:app=icode:{_msg('provider unreachable retry budget spent')}")]


async def test_error_outside_a_live_run_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await bus.publish(Error(code="image_attachment_blocked", message="too large"))

    assert writes == [_IDLE]


async def test_judged_approval_waits_for_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            ApprovalRequest(request_id="a1", tool_name="shell_command", intent_summary="run tests", judging=True),
            ApprovalReviewed(request_id="a1", approved=True),
        ),
    )

    # working was already on record: the judging request stays silent, the
    # approved verdict dedups against it, and nothing flips blocked.
    assert writes == [_IDLE, _WORKING]


async def test_approval_request_blocks_with_tool_message(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            ApprovalRequest(request_id="a1", tool_name="shell_command", intent_summary="run tests"),
        ),
    )

    assert writes[-1] == _seq(f"state=blocked:kind=permission:app=icode:{_msg('shell_command: run tests')}")


async def test_denied_verdict_blocks_then_response_resumes_working(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            ApprovalRequest(request_id="a1", tool_name="shell_command", intent_summary="run tests", judging=True),
            ApprovalReviewed(request_id="a1", approved=False, reason="destructive command"),
            ApprovalResponse(request_id="a1", approved=False),
        ),
    )

    assert writes[-2] == _seq(f"state=blocked:kind=permission:app=icode:{_msg('destructive command')}")
    assert writes[-1] == _WORKING


async def test_cancelled_approval_resumes_working(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            ApprovalRequest(request_id="a1", tool_name="shell_command", intent_summary="run tests"),
            ApprovalCancelled(request_id="a1"),
        ),
    )

    assert writes[-1] == _WORKING


async def test_auto_fulfill_blocked_reports_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            ApprovalAutoFulfillBlocked(request_id="a1"),
        ),
    )

    assert writes[-1] == _seq("state=blocked:kind=permission:app=icode")


async def test_question_blocks_and_answer_resumes_working(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn")),
            QuestionToUser(request_id="q1", questions=(AskUserQuestion(question="Proceed with the deploy?"),)),
            AskUserResponse(request_id="q1"),
        ),
    )

    assert writes[-2] == _seq(f"state=blocked:kind=question:app=icode:{_msg('Proceed with the deploy?')}")
    assert writes[-1] == _WORKING


async def test_expired_question_resumes_working(monkeypatch: pytest.MonkeyPatch) -> None:
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


async def test_workflow_node_question_blocks_and_answer_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("workflow", run_id="r1")),
            WorkflowNodeAskUser(
                run_id="r1",
                node_id="n1",
                request_id="q1",
                questions=(AskUserQuestion(question="Pick a region"),),
            ),
            WorkflowNodeAnswered(run_id="r1", node_id="n1", request_id="q1"),
        ),
    )

    assert writes[-2] == _seq(f"state=blocked:kind=question:app=icode:{_msg('Pick a region')}")
    assert writes[-1] == _WORKING


async def test_identical_reports_are_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    bus, writes = await _make_reporter(monkeypatch)

    await _publish_all(
        bus,
        (
            ExecutionChanged(snapshot=ExecutionSnapshot("turn", request_id="t1")),
            ExecutionChanged(snapshot=ExecutionSnapshot("turn", request_id="t1")),
        ),
    )

    assert writes == [_IDLE, _WORKING]


async def test_unsubscribe_stops_reporting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    bus = EventBus()
    app = _App()
    reporter = ProgramStatusReporter(bus, app_provider=lambda: app)
    await reporter.subscribe()
    app._driver.writes.clear()

    await reporter.unsubscribe()
    await bus.publish(ExecutionChanged(snapshot=ExecutionSnapshot("turn")))

    assert app._driver.writes == []


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
