# ruff: noqa: RUF002, S101
"""M3 测试：ai-code blocks / codeStatus 采纳语义 / reporter 报文（方案 §4.3）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from chrys.aixcoding.config import clear_settings_cache
from chrys.aixcoding.telemetry import subscriber
from chrys.aixcoding.telemetry.llm_telemetry import clear_call_registry
from chrys.aixcoding.telemetry.reporters.ai_code import (
    AiCodeReporter,
    ApprovalTracker,
    ai_code_blocks,
)
from chrys.aixcoding.telemetry.types import CodeStatus
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ApprovalModeUpdated,
    ApprovalRequest,
    ApprovalResponse,
    InvocationToolCallResult,
    InvocationToolCallStart,
)
from chrys.foundation.models.invocations import InvocationOrigin


@pytest.fixture(autouse=True)
def _clean_state():
    clear_call_registry()
    clear_settings_cache()
    subscriber.reset_for_tests()
    yield
    clear_call_registry()
    clear_settings_cache()
    subscriber.reset_for_tests()


def _origin() -> InvocationOrigin:
    return InvocationOrigin(kind="turn", session_id="sess-1", invocation_id="inv-7", parent=None)


def _write_start(
    call_id: str = "c1", *, tool_name: str = "write_file", args: dict | None = None
) -> InvocationToolCallStart:
    return InvocationToolCallStart(
        origin=_origin(),
        tool_name=tool_name,
        tool_kind="",
        args=dict(args if args is not None else {"path": "src/a.py", "content": "new"}),
        call_id=call_id,
        session_id="sess-1",
    )


def _write_result(
    call_id: str = "c1",
    *,
    before: str | None = None,
    after: str = "hello\nworld",
    errored: bool = False,
) -> InvocationToolCallResult:
    snapshot = SimpleNamespace(before_text=before, after_text=after)
    metadata: dict[str, Any] = {"file_snapshot": snapshot}
    if errored:
        metadata["errored"] = True
    return InvocationToolCallResult(
        origin=_origin(),
        tool_name="write_file",
        call_id=call_id,
        result="ok",
        duration_ms=50,
        session_id="sess-1",
        metadata=metadata,
    )


class _CaptureAdd:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, payload: dict[str, object]) -> None:
        self.payloads.append(dict(payload))


# -- blocks 计算 ---------------------------------------------------------------


def test_blocks_new_file():
    blocks = ai_code_blocks(None, "a\nb\nc")
    assert blocks == [{"snippet": "a\nb\nc", "rangeStart": 1, "rangeEnd": 3}]


def test_blocks_locates_embedded_text():
    before = "line1\nline2\nhello\nworld\nline5"
    blocks = ai_code_blocks(before, "hello\nworld")
    assert blocks[0]["rangeStart"] == 3
    assert blocks[0]["rangeEnd"] == 4


def test_blocks_replacement_falls_back_to_one():
    blocks = ai_code_blocks("old content here", "brand new")
    assert blocks[0]["rangeStart"] == 1
    assert blocks[0]["rangeEnd"] == 1


def test_blocks_empty_after():
    assert ai_code_blocks("x", "") == []


# -- codeStatus 采纳语义 --------------------------------------------------------


def _tracker_with(*, mode: str, approved: bool) -> ApprovalTracker:
    tracker = ApprovalTracker()
    tracker.on_mode_updated(SimpleNamespace(session_id="sess-1", mode=mode))
    tracker.on_request(SimpleNamespace(session_id="sess-1", request_id="r1", call_id="c1"))
    tracker.on_response(SimpleNamespace(request_id="r1", approved=approved))
    return tracker


def test_adopted_no_approval_is_success():
    tracker = ApprovalTracker()
    assert tracker.adopted_code_status("c1", "sess-1", rejected=False) == CodeStatus.SUCCESS


def test_adopted_manual_approval_maps_to_five():
    assert (
        _tracker_with(mode="manual", approved=True).adopted_code_status("c1", "sess-1", rejected=False)
        == CodeStatus.USER_APPROVED
    )


def test_adopted_auto_judge_maps_to_one():
    assert (
        _tracker_with(mode="auto", approved=True).adopted_code_status("c1", "sess-1", rejected=False)
        == CodeStatus.SUCCESS
    )


def test_adopted_rejected_maps_to_four():
    assert (
        _tracker_with(mode="manual", approved=True).adopted_code_status("c1", "sess-1", rejected=True)
        == CodeStatus.USER_REJECTED
    )


# -- reporter -------------------------------------------------------------------


def test_reporter_write_success_emits_payload():
    add = _CaptureAdd()
    reporter = AiCodeReporter(add)
    reporter.on_start(_write_start())
    reporter.on_result(_write_result(before=None, after="hello\nworld"), code_status=5, errored=False)

    assert len(add.payloads) == 1
    payload = add.payloads[0]
    assert payload["filepath"] == "src/a.py"
    assert payload["sourceType"] == "edit"
    assert payload["codeStatus"] == 5
    assert payload["spanId"] == "inv-7"
    assert payload["blocks"] == [{"snippet": "hello\nworld", "rangeStart": 1, "rangeEnd": 2}]
    assert payload["reportId"]
    assert payload["pluginVersion"]
    assert payload["projectName"]


def test_reporter_skips_errored_and_non_write_tools():
    add = _CaptureAdd()
    reporter = AiCodeReporter(add)
    reporter.on_start(_write_start())
    reporter.on_result(_write_result(errored=True), code_status=2, errored=True)
    assert add.payloads == []

    reporter.on_start(_write_start(call_id="c2", tool_name="read_file"))
    reporter.on_result(_write_result(call_id="c2"), code_status=1, errored=False)
    assert add.payloads == []


def test_reporter_missing_path_skips():
    add = _CaptureAdd()
    reporter = AiCodeReporter(add)
    reporter.on_start(_write_start(args={}))
    reporter.on_result(_write_result(), code_status=1, errored=False)
    assert add.payloads == []


# -- subscriber 端到端 -----------------------------------------------------------


async def _publish_approval_flow(bus: EventBus, *, mode: str) -> None:
    await bus.publish(ApprovalModeUpdated(mode=mode, session_id="sess-1"))
    await bus.publish(ApprovalRequest(request_id="r1", call_id="c1", tool_name="write_file", session_id="sess-1"))
    await bus.publish(ApprovalResponse(request_id="r1", approved=True, session_id="sess-1"))


async def test_subscriber_ai_code_manual_approval_end_to_end():
    bus = EventBus()
    add = _CaptureAdd()
    subscriber._ai_code = AiCodeReporter(add)  # 注入测试替身
    subscriber.attach(bus)
    await asyncio_sleep_zero()

    await _publish_approval_flow(bus, mode="manual")
    await bus.publish(_write_start())
    await bus.publish(_write_result())

    assert len(add.payloads) == 1
    assert add.payloads[0]["codeStatus"] == CodeStatus.USER_APPROVED


async def test_subscriber_ai_code_auto_judge_end_to_end():
    bus = EventBus()
    add = _CaptureAdd()
    subscriber._ai_code = AiCodeReporter(add)  # 注入测试替身
    subscriber.attach(bus)
    await asyncio_sleep_zero()

    await _publish_approval_flow(bus, mode="auto")
    await bus.publish(_write_start())
    await bus.publish(_write_result())

    assert len(add.payloads) == 1
    assert add.payloads[0]["codeStatus"] == CodeStatus.SUCCESS


async def asyncio_sleep_zero() -> None:
    import asyncio

    await asyncio.sleep(0)
