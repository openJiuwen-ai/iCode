# ruff: noqa: RUF002, RUF003, S101
"""M2 测试：outcome 分类 / tool-detail 报文组装 / subscriber 装配分发（方案 §4.2/§4.4）。"""

from __future__ import annotations

from typing import Any

import pytest

from chrys.aixcoding.config import clear_settings_cache
from chrys.aixcoding.telemetry import subscriber
from chrys.aixcoding.telemetry.llm_telemetry import clear_call_registry, record_call
from chrys.aixcoding.telemetry.outcome import classify_result_metadata
from chrys.aixcoding.telemetry.reporters.tool_detail import (
    ToolDetailReporter,
    func_type_for_kind,
    line_counts,
    pick_value,
    relative_file_name,
)
from chrys.aixcoding.telemetry.types import (
    TOOL_DETAIL_SAVE,
    TOOL_DETAIL_UPDATE,
    CodeStatus,
    FuncType,
)
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import InvocationToolCallResult, InvocationToolCallStart
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.tool_kinds import KIND_MCP, KIND_SKILL
from chrys.foundation.tool_result_metadata import TOOL_ERRORED_METADATA_KEY


@pytest.fixture(autouse=True)
def _clean_state():
    clear_call_registry()
    clear_settings_cache()
    subscriber.reset_for_tests()
    from chrys.aixcoding.context import clear_desktop_channel, set_current_function_name

    clear_desktop_channel()
    set_current_function_name(None)
    yield
    clear_call_registry()
    clear_settings_cache()
    subscriber.reset_for_tests()
    clear_desktop_channel()
    set_current_function_name(None)


def _origin(session_id: str = "sess-1") -> InvocationOrigin:
    return InvocationOrigin(kind="turn", session_id=session_id, invocation_id="inv-42", parent=None)


def _start(
    *, call_id: str = "c1", provider_call_id: str = "", tool_name: str = "read_file", args: dict | None = None
) -> InvocationToolCallStart:
    return InvocationToolCallStart(
        origin=_origin(),
        tool_name=tool_name,
        tool_kind="",
        args=dict(args or {}),
        call_id=call_id,
        provider_call_id=provider_call_id,
        session_id="sess-1",
    )


def _result(
    *, call_id: str = "c1", provider_call_id: str = "", metadata: dict | None = None, duration_ms: int = 120
) -> InvocationToolCallResult:
    return InvocationToolCallResult(
        origin=_origin(),
        tool_name="read_file",
        call_id=call_id,
        provider_call_id=provider_call_id,
        result="done",
        duration_ms=duration_ms,
        session_id="sess-1",
        metadata=dict(metadata or {}),
    )


class _CaptureSubmitter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, endpoint: str, payload: dict[str, object]) -> None:
        self.calls.append((endpoint, dict(payload)))

    def endpoints(self) -> list[str]:
        return [endpoint for endpoint, _ in self.calls]


# -- outcome 分类 ---------------------------------------------------------------


def test_classify_success():
    assert classify_result_metadata({}).code_status == CodeStatus.SUCCESS
    assert classify_result_metadata({"failed": False}).code_status == CodeStatus.SUCCESS


def test_classify_errored():
    cls = classify_result_metadata({TOOL_ERRORED_METADATA_KEY: True})
    assert cls.code_status == CodeStatus.FAILED
    assert cls.failure_type == "error"


def test_classify_timed_out():
    cls = classify_result_metadata({"shell_timed_out": True})
    assert cls.code_status == CodeStatus.FAILED
    assert cls.failure_type == "timeout"
    cls2 = classify_result_metadata({"process_timed_out": True})
    assert cls2.failure_type == "timeout"


def test_classify_rejected():
    approval = classify_result_metadata({"approval": "user_rejected"})
    assert approval.code_status == CodeStatus.USER_REJECTED
    assert approval.rejected is True
    hook = classify_result_metadata({"tool_error_kind": "approval_rejected"})
    assert hook.code_status == CodeStatus.USER_REJECTED


def test_classify_structured_failure():
    cls = classify_result_metadata({"failed": True})
    assert cls.code_status == CodeStatus.FAILED
    assert cls.failure_type == "error"


# -- 参数白名单与行数 -----------------------------------------------------------


def test_func_type_mapping():
    assert func_type_for_kind(KIND_SKILL) == FuncType.SKILL
    assert func_type_for_kind(KIND_MCP) == FuncType.MCP
    assert func_type_for_kind("") == FuncType.BUILTIN
    assert func_type_for_kind("shell") == FuncType.BUILTIN


def test_pick_value_whitelist():
    assert pick_value("read_file", {"path": "src/a.py", "extra": "x"}, full_mode=False) == "src/a.py"
    assert pick_value("load_skill", {"skill_name": "code_review"}, full_mode=False) == "code_review"
    assert pick_value("write_file", {"filepath": "src/a.py"}, full_mode=False) is None
    assert pick_value("read_file", {"path": 123}, full_mode=False) == "123"
    assert pick_value("read_file", {}, full_mode=False) is None


def test_pick_value_full_mode_truncates():
    args = {"blob": "x" * 5000}
    value = pick_value("any_tool", args, full_mode=True)
    assert value is not None and len(value) == 2000


def test_line_counts_diff():
    # 真链路 file_snapshot 是 tuple(before, after)（pipeline.py:89），非对象属性。
    counts = line_counts({"file_snapshot": ("a\nb\nc", "a\nx\nc\nd"), "file_mutation_op": "modify"})
    assert counts == {"originalLines": 3, "addedLines": 2, "deletedLines": 1}
    assert line_counts({}) == {}


def test_line_counts_create_and_delete_rules():
    # 创建：original=0、added=新文件行数、deleted=0（2026-10-09 用户定稿）
    assert line_counts({"file_snapshot": ("", "a\nb\nc"), "file_mutation_op": "create"}) == {
        "originalLines": 0,
        "addedLines": 3,
        "deletedLines": 0,
    }
    # 删除：original=0、added=0、deleted=被删文件行数
    assert line_counts({"file_snapshot": ("a\nb\nc", ""), "file_mutation_op": "delete"}) == {
        "originalLines": 0,
        "addedLines": 0,
        "deletedLines": 3,
    }
    # 无 op 时按空侧推断（before 空=创建 / after 空=删除）
    assert line_counts({"file_snapshot": ("", "x\ny")})["addedLines"] == 2
    assert line_counts({"file_snapshot": ("x\ny", "")})["deletedLines"] == 2


# -- reporter 报文 --------------------------------------------------------------


def test_reporter_start_then_result_ordering_and_fields():
    submitter = _CaptureSubmitter()
    reporter = ToolDetailReporter(submitter)
    record_call("pc-1", "req-9", "span-9", "sess-1")

    reporter.on_start(_start(provider_call_id="pc-1", args={"path": "src/a.py"}))
    reporter.on_result(
        _result(provider_call_id="pc-1", metadata={"file_snapshot": ("a\nb", "a\nx\ny"), "file_mutation_op": "modify"})
    )

    assert submitter.endpoints() == [TOOL_DETAIL_SAVE, TOOL_DETAIL_UPDATE, TOOL_DETAIL_UPDATE]
    save = submitter.calls[0][1]
    assert save["funcId"] == "pc-1"  # csas 语义：provider id（模型生成），非 Chrys 短 id
    assert save["funcName"] == "read_file"
    assert save["funcType"] == FuncType.BUILTIN
    assert save["sessionId"] == "sess-1"
    assert save["spanId"] == "span-9"
    assert save["requestId"] == "req-9"
    assert "parentSpanId" not in save
    assert save["value"] == "src/a.py"
    assert save["pluginVersion"]
    assert save["projectName"]

    pending = submitter.calls[1][1]
    assert pending["funcId"] == "pc-1"
    assert pending["funcName"] == "read_file"
    assert pending["codeStatus"] == CodeStatus.PENDING

    final = submitter.calls[2][1]
    assert final["funcId"] == "pc-1"  # update 关联键与 save 同源（provider id）
    assert final["codeStatus"] == CodeStatus.SUCCESS
    assert final["executionDurationMs"] == 120
    assert final["originalLines"] == 2
    assert final["addedLines"] == 2
    assert final["deletedLines"] == 1
    assert final["executionStartedAt"]
    assert final["executionFinishedAt"]


def test_reporter_write_file_save_includes_file_name():
    submitter = _CaptureSubmitter()
    reporter = ToolDetailReporter(submitter)
    record_call("pc-w", "req-w", "span-w", "sess-1")

    reporter.on_start(
        _start(provider_call_id="pc-w", tool_name="write_file", args={"path": "docs/new.md", "content": "hi"})
    )
    save = submitter.calls[0][1]
    assert save["fileName"] == "docs/new.md"  # 写类工具 save 附 fileName（契约对齐）
    reporter.on_result(
        _result(provider_call_id="pc-w", metadata={"file_snapshot": ("", "hi\n"), "file_mutation_op": "create"})
    )
    final = submitter.calls[2][1]
    assert final["originalLines"] == 0  # 创建：original=0、added=新文件行数
    assert final["addedLines"] == 1
    assert final["deletedLines"] == 0


def test_relative_file_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    # 工程内绝对路径 → 相对路径（POSIX 分隔符，含文件名）
    assert relative_file_name(str(sub / "a.kt")) == "sub/a.kt"
    # 工程外绝对路径 → 原样保留
    outside = str(tmp_path.parent / "elsewhere.kt")
    assert relative_file_name(outside) == outside
    # 相对入参原样
    assert relative_file_name("docs/new.md") == "docs/new.md"


def test_reporter_result_without_registry_still_reports():
    submitter = _CaptureSubmitter()
    reporter = ToolDetailReporter(submitter)
    reporter.on_start(_start())
    reporter.on_result(
        _result(
            metadata={TOOL_ERRORED_METADATA_KEY: True},
        )
    )
    save = submitter.calls[0][1]
    assert "requestId" not in save
    assert save["funcId"] == "c1"  # provider id 为空时 fallback Chrys 短 id
    final = submitter.calls[2][1]
    assert final["funcId"] == "c1"
    assert final["codeStatus"] == CodeStatus.FAILED
    assert final["failureType"] == "error"
    assert final["funcErrorMessage"] == "done"


def test_reporter_rejected_marks_user_rejected():
    submitter = _CaptureSubmitter()
    reporter = ToolDetailReporter(submitter)
    reporter.on_start(_start())
    reporter.on_result(_result(metadata={"approval": "user_rejected"}))
    assert submitter.calls[2][1]["codeStatus"] == CodeStatus.USER_REJECTED


def test_record_invocation_single_save():
    submitter = _CaptureSubmitter()
    reporter = ToolDetailReporter(submitter)
    record_call("pc-0", "req-prev", "", "sess-1")
    reporter.record_invocation("java_code_review", "sess-1")
    assert submitter.endpoints() == [TOOL_DETAIL_SAVE]
    save = submitter.calls[0][1]
    assert save["funcName"] == "java_code_review"
    assert save["funcType"] == FuncType.SKILL
    assert save["sessionId"] == "sess-1"
    assert "requestId" not in save  # 输入触发不误挂上一轮请求（aixcoding 同语义）
    assert "spanId" not in save  # session 级 fallback span 为空则不带


# -- subscriber 装配 ------------------------------------------------------------


async def test_subscriber_attach_dispatches_and_is_idempotent():
    bus = EventBus()
    submitter = _CaptureSubmitter()
    subscriber._reporter = ToolDetailReporter(submitter)  # 注入测试替身

    subscriber.attach(bus)
    await asyncio_sleep_zero()  # create_task 注册完成
    await bus.publish(_start(provider_call_id="pc-1"))
    await bus.publish(_result(metadata={}))

    subscriber.attach(bus)  # 幂等：二次 attach 不重复注册
    await asyncio_sleep_zero()
    await bus.publish(_start(call_id="c2"))
    await bus.publish(_result(call_id="c2", metadata={}))

    assert submitter.endpoints() == [
        TOOL_DETAIL_SAVE,
        TOOL_DETAIL_UPDATE,
        TOOL_DETAIL_UPDATE,
        TOOL_DETAIL_SAVE,
        TOOL_DETAIL_UPDATE,
        TOOL_DETAIL_UPDATE,
    ]


async def test_record_skill_invocation_uses_shared_reporter():
    submitter = _CaptureSubmitter()
    subscriber._reporter = ToolDetailReporter(submitter)  # 注入测试替身
    subscriber.record_skill_invocation("code_review", "sess-7")
    assert submitter.endpoints() == [TOOL_DETAIL_SAVE]
    assert submitter.calls[0][1]["funcName"] == "code_review"


async def test_attach_disabled_by_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AIXCODING_TELEMETRY_DISABLED", "1")
    clear_settings_cache()
    bus = EventBus()
    submitter = _CaptureSubmitter()
    subscriber._reporter = ToolDetailReporter(submitter)  # 注入测试替身
    subscriber.attach(bus)
    await asyncio_sleep_zero()
    await bus.publish(_start())
    assert submitter.calls == []


async def asyncio_sleep_zero() -> None:
    import asyncio

    await asyncio.sleep(0)


# -- 评审补强：acp_meta / agentName / 有界记忆 ------------------------------------------------


def test_read_ide_channel_meta_sets_channel_and_function_name():
    from chrys.aixcoding.context import current_channel, current_function_name
    from chrys.aixcoding.telemetry import acp_meta

    acp_meta.read_ide_channel_meta(
        {
            "agent-studio.dev/ide-name": {"schemaVersion": 1, "ideName": "AgentStudio"},
            "agent-studio.dev/ide-version": {"schemaVersion": 1, "ideVersion": "1.2.3"},
            "agent-studio.dev/function-name": {"schemaVersion": 1, "functionName": "code_gen"},
        }
    )
    channel = current_channel()
    assert channel.channel_type == "desktop"
    assert channel.channel_name == "AgentStudio"
    assert channel.channel_version == "1.2.3"
    assert current_function_name() == "code_gen"
    # 无 function-name 的 prompt 清除旧值；非法 envelope 不生效
    acp_meta.read_ide_channel_meta({"agent-studio.dev/function-name": {"schemaVersion": 2, "functionName": "x"}})
    assert current_function_name() is None


def test_reporter_payload_includes_agent_name():
    from chrys.aixcoding.context import set_current_function_name

    set_current_function_name("code_gen")
    submitter = _CaptureSubmitter()
    reporter = ToolDetailReporter(submitter)
    reporter.on_start(_start(args={"filepath": "src/a.py"}))
    assert submitter.calls[0][1]["agentName"] == "code_gen"
    set_current_function_name(None)


def test_telemetry_response_meta_roundtrip():
    from chrys.aixcoding.telemetry.acp_meta import telemetry_response_meta

    record_call("pc-r", "req-r", "span-r", "sess-1")
    meta = telemetry_response_meta("sess-1")
    assert meta == {"agent-studio.dev/telemetry": {"schemaVersion": 1, "requestId": "req-r", "spanId": "span-r"}}
    assert telemetry_response_meta("unknown-session") is None


def test_remember_bounded_drops_oldest():
    from chrys.aixcoding.telemetry.reporters import BOUNDED_MEMORY_LIMIT, remember_bounded

    mapping: dict[str, int] = {}
    for index in range(BOUNDED_MEMORY_LIMIT + 10):
        remember_bounded(mapping, f"k{index}", index)
    assert len(mapping) == BOUNDED_MEMORY_LIMIT
    assert "k0" not in mapping
    assert mapping[f"k{BOUNDED_MEMORY_LIMIT + 9}"] == BOUNDED_MEMORY_LIMIT + 9
