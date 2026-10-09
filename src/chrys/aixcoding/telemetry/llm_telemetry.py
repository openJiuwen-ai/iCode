# ruff: noqa: RUF001, RUF002, RUF003
"""llm-call 搭车上报 + per-call registry（方案 §4.1）。

搭车通道：每次模型请求在 ``context.options`` 注入 ``extra_body["telemetry"]``
（wire 客户端把 ``extra_body`` merge 进请求 body 顶层，由模型网关解析落库，
iCode 侧不感知回执）。payload 字段对齐 aixcoding ``getTelemetryData``。

per-call registry：``provider_call_id → (requestId, 根spanId)``。id 双体系
（方案 §4.2 缺口②）：registry 键是 provider 原始 call id（模型响应
``FunctionCallContent.call_id``，即 ``loop.py`` 写入 ``metadata["call_id"]``
的同一个值），与工具事件 ``call_id``（Chrys 短 id）不同源——工具事件须补
``provider_call_id`` 字段（源码 #5）方能反查。

提取时机分两路：非流式在 ``call_next()`` 后直接读 ``context.result``；
流式（生产默认）下 ``call_next()`` 返回时 result 是未消费的 ResponseStream，
须经 ``with_result_hook`` 在流终结后提取（``UsageTrackingMiddleware`` 同款
模式）。时序安全：流终结 → 写 registry → 工具执行 → Start 事件发布。

side call（judge/标题/last-words，``in_internal_side_call``）只搭车
（``eventSubType="system"``），不写 registry、不更新 session 级最新值
（"side call 不参与关联"，对齐 aixcoding 仅主对话通道语义）。
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections import OrderedDict
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.foundation.trajectory.context import current_trajectory
from chrys.kernel import ChatResponse, ResponseStream, in_internal_side_call
from chrys.kernel.middleware import ChatContext, ChatMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_ROOT_SPAN_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "chrys-aixcoding-telemetry")
"""轮次根 span 的 uuid5 命名空间（固定值，保证同 session 同轮次恒同 spanId）。"""

_REGISTRY_MAX_ENTRIES = 4096
"""per-call registry 上限：超出丢最旧（长驻进程的简单防护）。"""


# ---------------------------------------------------------------------------
# per-call registry（进程内共享，跨 middleware 实例）
# ---------------------------------------------------------------------------

_registry_lock = threading.Lock()
_registry: OrderedDict[str, tuple[str, str]] = OrderedDict()
_session_latest: dict[str, tuple[str, str]] = {}


def root_span_id(session_id: str, turn_id: str) -> str:
    """轮次根 span：``uuid5(NAMESPACE, f"{session_id}:{turn_id}")``（确定性）。"""
    return str(uuid.uuid5(_ROOT_SPAN_NAMESPACE, f"{session_id}:{turn_id}"))


def record_call(provider_call_id: str, request_id: str, span_id: str, session_id: str | None) -> None:
    """登记一次主对话 LLM 调用的关联元组（供工具事件按 provider_call_id 反查）。"""
    if not provider_call_id:
        return
    with _registry_lock:
        _registry[provider_call_id] = (request_id, span_id)
        while len(_registry) > _REGISTRY_MAX_ENTRIES:
            _registry.popitem(last=False)
        if session_id:
            _session_latest[session_id] = (request_id, span_id)


def resolve_call(provider_call_id: str, session_id: str | None = None) -> tuple[str, str] | None:
    """反查 ``(requestId, 根spanId)``：per-call 命中优先，退化为 session 级最新值。"""
    with _registry_lock:
        if provider_call_id and provider_call_id in _registry:
            return _registry[provider_call_id]
        if session_id and session_id in _session_latest:
            return _session_latest[session_id]
    return None


def clear_call_registry() -> None:
    """清空 registry（测试辅助）。"""
    with _registry_lock:
        _registry.clear()
        _session_latest.clear()


# ---------------------------------------------------------------------------
# middleware
# ---------------------------------------------------------------------------


def build_telemetry_middleware(session_id: str | None) -> list[ChatMiddleware] | None:
    """组栈入口（``instrumented.py`` 调用）；装配异常只降级为不上报。"""
    try:
        return [AixTelemetryMiddleware(session_id=session_id)]
    except Exception:
        logger.warning("AIxCoding telemetry middleware unavailable; llm telemetry disabled", exc_info=True)
        return None


class AixTelemetryMiddleware(ChatMiddleware):
    """llm-call 搭车遥测：payload 注入 + function call 关联登记。"""

    def __init__(self, session_id: str | None) -> None:
        self._session_id = session_id

    async def process(self, context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        if not self._enabled():
            await call_next()
            return
        request_id = str(uuid.uuid4())
        span_id = ""
        trajectory = current_trajectory()
        turn_id = trajectory.turn_id if trajectory is not None else None
        if turn_id:
            span_id = root_span_id(self._session_id or "", turn_id)
        try:
            self._inject_telemetry(context, request_id, span_id)
        except Exception:
            logger.warning("AIxCoding telemetry payload inject failed", exc_info=True)
        side_call = in_internal_side_call()
        await call_next()
        if side_call:
            return
        result = context.result
        if isinstance(result, ChatResponse):
            self._record_response(result, request_id, span_id)
        elif isinstance(result, ResponseStream):

            def _record_on_final(response: ChatResponse) -> ChatResponse:
                self._record_response(response, request_id, span_id)
                return response

            result.with_result_hook(_record_on_final)

    def _enabled(self) -> bool:
        if not self._session_id:
            return False
        try:
            from chrys.aixcoding.config import load_settings

            return load_settings().telemetry_enabled
        except Exception:
            return False

    def _inject_telemetry(self, context: ChatContext, request_id: str, span_id: str) -> None:
        payload: dict[str, Any] = {
            "requestId": request_id,
            "sessionId": self._session_id,
            "eventType": "llm",
            "eventSubType": "system" if in_internal_side_call() else "agent",
        }
        if span_id:
            payload["spanId"] = span_id
        from chrys.aixcoding.context import current_channel, plugin_version

        channel = current_channel()
        payload["channelType"] = channel.channel_type
        payload["channelName"] = channel.channel_name
        if channel.channel_version:
            payload["channelVersion"] = channel.channel_version
        payload["pluginVersion"] = plugin_version()
        cwd = Path.cwd()
        payload["projectName"] = cwd.name or str(cwd)
        from chrys.aixcoding.git_info import collect_git_info

        git = collect_git_info(cwd)
        if git is not None:
            payload.update(
                {
                    key: value
                    for key, value in (
                        ("gitRemote", git.git_remote),
                        ("gitBranch", git.git_branch),
                        ("gitRevision", git.git_revision),
                        ("gitOwner", git.git_owner),
                        ("gitRepo", git.git_repo),
                    )
                    if value
                }
            )
        options = dict(context.options or {})
        extra_body = options.get("extra_body")
        merged = dict(extra_body) if isinstance(extra_body, Mapping) else {}
        merged["telemetry"] = payload
        options["extra_body"] = merged
        context.options = options

    def _record_response(self, response: ChatResponse, request_id: str, span_id: str) -> None:
        try:
            for message in response.messages:
                for content in message.contents:
                    call_id = getattr(content, "call_id", None)
                    if content.type == "function_call" and call_id:
                        record_call(call_id, request_id, span_id, self._session_id)
        except Exception:
            logger.warning("AIxCoding telemetry function-call extraction failed", exc_info=True)
