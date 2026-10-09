# ruff: noqa: RUF002, RUF003, S101
"""llm_telemetry 测试：payload 注入、per-call registry、流式终结提取（方案 §4.1）。

覆盖 M1 验收的机器可验部分：搭车 payload 字段、spanId 确定性、流式
``with_result_hook`` 终结后写 registry（流终结 → registry 就绪的时序）、
side call 不参与关联、总开关关闭零注入。网关查库/ACP 审批等端到端项在
验收清单另行推进。
"""

from __future__ import annotations

from typing import Any

import pytest

from chrys.aixcoding.config import clear_settings_cache
from chrys.aixcoding.telemetry import llm_telemetry as lt
from chrys.aixcoding.telemetry.llm_telemetry import (
    AixTelemetryMiddleware,
    build_telemetry_middleware,
    clear_call_registry,
    record_call,
    resolve_call,
    root_span_id,
)
from chrys.foundation.trajectory.context import TrajectoryContext, main_actor, trajectory_scope
from chrys.kernel import ChatResponse, Content, Message, ResponseStream, internal_side_call_scope
from chrys.kernel.middleware import ChatContext, ChatMiddlewareLayer


@pytest.fixture(autouse=True)
def _clean_state():
    clear_call_registry()
    clear_settings_cache()
    yield
    clear_call_registry()
    clear_settings_cache()


class _NullSink:
    """TrajectorySink 最小替身：middleware 只读 turn_id，从不写 sink。"""

    async def emit(self, draft: Any, *, payload_factory: Any = None) -> None:
        return None

    def emit_blocking(self, draft: Any, *, payload_factory: Any = None) -> None:
        return None

    def emit_soon(self, draft: Any, *, payload_factory: Any = None) -> None:
        return None

    @property
    def fingerprint_key(self) -> None:
        return None


def _tool_call_response(*call_ids: str) -> ChatResponse:
    contents = [Content.from_function_call(call_id=cid, name="read_file") for cid in call_ids]
    return ChatResponse(messages=[Message("assistant", contents)], finish_reason="tool_calls", model="test-model")


def _make_context(**options: Any) -> ChatContext:
    return ChatContext(client=object(), messages=[], options=options or None, stream=False)


async def _run_middleware(middleware: AixTelemetryMiddleware, result: Any, **options: Any) -> ChatContext:
    context = _make_context(**options)

    async def call_next() -> None:
        context.result = result

    await middleware.process(context, call_next)
    return context


# -- registry -----------------------------------------------------------------


def test_registry_exact_hit_then_session_fallback():
    record_call("pc-1", "req-1", "span-1", "sess-1")
    record_call("pc-2", "req-2", "span-2", "sess-1")

    assert resolve_call("pc-1", "sess-1") == ("req-1", "span-1")
    assert resolve_call("pc-2", "sess-1") == ("req-2", "span-2")
    # 反查不到时退化为 session 级最新主对话调用
    assert resolve_call("unknown", "sess-1") == ("req-2", "span-2")
    assert resolve_call("unknown") is None
    assert resolve_call("unknown", "other-session") is None


def test_registry_bounded_drops_oldest():
    total = lt._REGISTRY_MAX_ENTRIES + 10
    for index in range(total):
        record_call(f"pc-{index}", f"req-{index}", "", "sess")

    assert resolve_call("pc-0") is None
    assert resolve_call(f"pc-{total - 1}") == (f"req-{total - 1}", "")
    # session 级最新值不受上限淘汰影响
    assert resolve_call("gone", "sess") == (f"req-{total - 1}", "")


def test_root_span_id_is_deterministic_per_turn():
    assert root_span_id("s1", "t1") == root_span_id("s1", "t1")
    assert root_span_id("s1", "t1") != root_span_id("s1", "t2")
    assert root_span_id("s1", "t1") != root_span_id("s2", "t1")


# -- middleware：非流式 -------------------------------------------------------


async def test_non_streaming_injects_payload_and_records_registry():
    middleware = AixTelemetryMiddleware(session_id="sess-1")
    context = await _run_middleware(middleware, _tool_call_response("pc-a"), model="m1", temperature=0.1)

    telemetry = context.options["extra_body"]["telemetry"]  # type: ignore[index]
    assert telemetry["requestId"]
    assert telemetry["sessionId"] == "sess-1"
    assert telemetry["eventType"] == "llm"
    assert telemetry["eventSubType"] == "agent"
    assert telemetry["channelType"] == "cli"
    assert telemetry["pluginVersion"]
    assert telemetry["projectName"]
    assert "spanId" not in telemetry  # 无 trajectory 绑定时无 turn_id
    # 既有 options 键不被覆盖
    assert context.options["model"] == "m1"  # type: ignore[index]

    entry = resolve_call("pc-a", "sess-1")
    assert entry is not None
    assert entry[0] == telemetry["requestId"]


async def test_payload_keeps_existing_extra_body():
    middleware = AixTelemetryMiddleware(session_id="sess-1")
    context = await _run_middleware(
        middleware,
        _tool_call_response(),
        extra_body={"custom": "kept"},
    )

    extra = context.options["extra_body"]  # type: ignore[index]
    assert extra["custom"] == "kept"
    assert "telemetry" in extra


async def test_span_id_from_bound_trajectory_turn():
    trajectory = TrajectoryContext(sink=_NullSink(), session_id="sess-1", actor=main_actor("sess-1"), turn_id="turn-9")
    middleware = AixTelemetryMiddleware(session_id="sess-1")
    with trajectory_scope(trajectory):
        context = await _run_middleware(middleware, _tool_call_response("pc-b"))

    telemetry = context.options["extra_body"]["telemetry"]  # type: ignore[index]
    assert telemetry["spanId"] == root_span_id("sess-1", "turn-9")


async def test_side_call_payload_system_without_registry():
    middleware = AixTelemetryMiddleware(session_id="sess-1")
    with internal_side_call_scope():
        context = await _run_middleware(middleware, _tool_call_response("pc-side"))

    telemetry = context.options["extra_body"]["telemetry"]  # type: ignore[index]
    assert telemetry["eventSubType"] == "system"
    assert resolve_call("pc-side", "sess-1") is None


async def test_disabled_telemetry_is_transparent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AIXCODING_TELEMETRY_DISABLED", "1")
    middleware = AixTelemetryMiddleware(session_id="sess-1")
    context = await _run_middleware(middleware, _tool_call_response("pc-off"))

    assert "extra_body" not in (context.options or {})
    assert resolve_call("pc-off", "sess-1") is None


async def test_missing_session_id_disables_middleware():
    middleware = AixTelemetryMiddleware(session_id=None)
    context = await _run_middleware(middleware, _tool_call_response("pc-nosess"))

    assert "extra_body" not in (context.options or {})
    assert resolve_call("pc-nosess") is None


# -- middleware：流式 ---------------------------------------------------------


async def test_streaming_records_registry_after_finalization():
    final = _tool_call_response("pc-stream-1", "pc-stream-2")

    async def _updates():
        return
        yield  # pragma: no cover - 空迭代器仅为类型成立

    stream = ResponseStream(_updates(), finalizer=lambda updates: final)
    middleware = AixTelemetryMiddleware(session_id="sess-1")
    context = _make_context(stream=True)

    async def call_next() -> None:
        context.result = stream

    await middleware.process(context, call_next)
    # call_next 返回时流未消费：registry 尚未就绪
    assert resolve_call("pc-stream-1", "sess-1") is None

    async for _update in stream:
        pass
    # 流终结 → result_hook → registry 就绪（工具 Start 发布前）
    for provider_call_id in ("pc-stream-1", "pc-stream-2"):
        entry = resolve_call(provider_call_id, "sess-1")
        assert entry is not None
        assert entry[0] == context.options["extra_body"]["telemetry"]["requestId"]  # type: ignore[index]


# -- 组栈入口 ------------------------------------------------------------------


def test_build_telemetry_middleware_returns_stack():
    stack = build_telemetry_middleware(session_id="sess-1")
    assert stack is not None and len(stack) == 1
    assert isinstance(stack[0], AixTelemetryMiddleware)


class _FakeInnerClient:
    """ChatMiddlewareLayer 端到端用：记录到达 inner 的 options。"""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.seen_options: dict[str, Any] | None = None

    async def get_response(
        self,
        messages: Any,
        *,
        stream: bool = False,
        options: Any = None,
        function_invocation_kwargs: Any = None,
        **kwargs: Any,
    ) -> Any:
        self.seen_options = dict(options or {})
        return self._result


async def test_layer_end_to_end_payload_reaches_inner_client():
    inner = _FakeInnerClient(_tool_call_response("pc-e2e"))
    layer = ChatMiddlewareLayer(inner, middleware=build_telemetry_middleware(session_id="sess-1"))
    await layer.get_response([Message("user", ["hi"])], options={"model": "m1"})

    telemetry = inner.seen_options["extra_body"]["telemetry"]  # type: ignore[index]
    assert telemetry["sessionId"] == "sess-1"
    entry = resolve_call("pc-e2e", "sess-1")
    assert entry is not None
    assert entry[0] == telemetry["requestId"]
