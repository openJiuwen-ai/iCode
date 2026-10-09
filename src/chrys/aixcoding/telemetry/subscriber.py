# ruff: noqa: RUF001, RUF002, RUF003
"""telemetry 订阅装配：事件 → reporter 分发 + ``attach()`` 幂等入口（方案 §4.2）。

与方案的一处实现偏差：方案写 ``bus.stream()`` 订阅，但 ``assemble_agent_engine``
在 TUI 路径是**无事件循环的同步上下文**（Textual ``run()`` 之前构造引擎），
stream 的消费循环无法同步启动——改用 ``bus.subscribe()`` 回调式注册：
handler 仅做 payload 组装 + 串行队列入队（毫秒级），``publish`` 的 inline
await 对发布方无感知；事件零丢失语义不变。注册双分支：事件循环内
``create_task`` 调度（``subscribe`` 本体是纯同步 append）；无循环时
``asyncio.run`` 一次性完成注册。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import weakref

from chrys.aixcoding.telemetry.types import CodeStatus
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ApprovalModeUpdated,
    ApprovalRequest,
    ApprovalResponse,
    InvocationToolCallResult,
    InvocationToolCallStart,
)

logger = logging.getLogger(__name__)

_ATTACHED_BUSES: weakref.WeakSet[EventBus] = weakref.WeakSet()
"""per-bus 幂等记录（WeakSet：bus 回收后可重新装配）。"""

_pending_registrations: set[asyncio.Task] = set()
"""loop 内 create_task 的注册协程引用（防 GC 中断）。"""

_reporter = None
_ai_code = None
_approval_tracker = None


def attach(bus: EventBus) -> None:
    """把 tool-detail / ai-code reporter 挂到 ``bus``（per-bus 幂等；装配失败静默降级）。"""
    try:
        from chrys.aixcoding.config import load_settings

        if not load_settings().telemetry_enabled:
            return
    except Exception:
        return
    if bus in _ATTACHED_BUSES:
        return
    _ATTACHED_BUSES.add(bus)

    reporter = default_reporter()
    tracker = approval_tracker()
    ai_code = default_ai_code_reporter()

    async def _on_start(event: InvocationToolCallStart) -> None:
        try:
            reporter.on_start(event)
            ai_code.on_start(event)
        except Exception:
            logger.warning("AIxCoding telemetry tool-detail start failed", exc_info=True)

    async def _on_result(event: InvocationToolCallResult) -> None:
        try:
            reporter.on_result(event)
            from chrys.aixcoding.telemetry.outcome import classify_result_metadata

            classification = classify_result_metadata(event.metadata)
            ai_code.on_result(
                event,
                code_status=tracker.adopted_code_status(
                    event.call_id,
                    event.session_id,
                    rejected=classification.rejected,
                ),
                errored=classification.code_status != CodeStatus.SUCCESS,
            )
        except Exception:
            logger.warning("AIxCoding telemetry tool-detail result failed", exc_info=True)

    async def _on_approval_mode(event: ApprovalModeUpdated) -> None:
        try:
            tracker.on_mode_updated(event)
        except Exception:
            logger.warning("AIxCoding telemetry approval mode track failed", exc_info=True)

    async def _on_approval_request(event: ApprovalRequest) -> None:
        try:
            tracker.on_request(event)
        except Exception:
            logger.warning("AIxCoding telemetry approval request track failed", exc_info=True)

    async def _on_approval_response(event: ApprovalResponse) -> None:
        try:
            tracker.on_response(event)
        except Exception:
            logger.warning("AIxCoding telemetry approval response track failed", exc_info=True)

    _register(bus, InvocationToolCallStart, _on_start)
    _register(bus, InvocationToolCallResult, _on_result)
    _register(bus, ApprovalModeUpdated, _on_approval_mode)
    _register(bus, ApprovalRequest, _on_approval_request)
    _register(bus, ApprovalResponse, _on_approval_response)


def record_skill_invocation(skill_name: str, session_id: str | None) -> None:
    """输入触发上报（方案 §4.4）：引擎侧 skill 引用命中的唯一调用点，吞一切异常。"""
    try:
        from chrys.aixcoding.config import load_settings

        if not load_settings().telemetry_enabled:
            return
        default_reporter().record_invocation(skill_name, session_id)
    except Exception:
        logger.warning("AIxCoding telemetry skill invocation report failed", exc_info=True)


def default_reporter():
    """进程级共享的 ToolDetailReporter（串行队列单例，save→update 天然保序）。"""
    global _reporter
    if _reporter is None:
        from chrys.aixcoding.telemetry.reporters.tool_detail import ToolDetailReporter

        _reporter = ToolDetailReporter(_shared_client().submit)
    return _reporter


def default_ai_code_reporter():
    """进程级共享的 AiCodeReporter（BatchBuffer 批量缓冲：满 20 条或 10s flush）。"""
    global _ai_code
    if _ai_code is None:
        from chrys.aixcoding.http import BatchBuffer
        from chrys.aixcoding.telemetry.reporters.ai_code import AiCodeReporter
        from chrys.aixcoding.telemetry.types import AI_CODE_SAVE

        _ai_code = AiCodeReporter(BatchBuffer(_shared_client(), AI_CODE_SAVE).add)
    return _ai_code


def approval_tracker():
    """进程级共享的审批采纳事实追踪器（ai-code codeStatus 判定）。"""
    global _approval_tracker
    if _approval_tracker is None:
        from chrys.aixcoding.telemetry.reporters.ai_code import ApprovalTracker

        _approval_tracker = ApprovalTracker()
    return _approval_tracker


_shared_client_instance = None


def _shared_client():
    """进程级 TelemetryHttpClient（串行 HTTP 出口单例）。"""
    global _shared_client_instance
    if _shared_client_instance is None:
        from chrys.aixcoding.config import load_settings
        from chrys.aixcoding.http import TelemetryHttpClient

        settings = load_settings()
        _shared_client_instance = TelemetryHttpClient(settings.report_base_url, settings.token)
    return _shared_client_instance


def _register(bus: EventBus, event_type, handler) -> None:
    coro = bus.subscribe(event_type, handler)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 同步构造期（无事件循环）：subscribe 本体是纯同步 append，一次性驱动。
        with contextlib.suppress(Exception):
            asyncio.run(coro)
    else:
        task = loop.create_task(coro)
        _pending_registrations.add(task)
        task.add_done_callback(_pending_registrations.discard)


def reset_for_tests() -> None:
    """清空装配状态（测试辅助；不关闭共享 client——由用例自行管理）。"""
    global _reporter, _ai_code, _approval_tracker
    _ATTACHED_BUSES.clear()
    _reporter = None
    _ai_code = None
    _approval_tracker = None
