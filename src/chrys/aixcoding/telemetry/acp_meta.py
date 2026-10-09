# ruff: noqa: RUF002
"""ACP ``_meta`` envelope 解析/组装（方案 §4.5；键与校验对齐 pi-acp）。

下行：agent_studio_new 在 prompt 请求 ``_meta`` 下发
``agent-studio.dev/ide-name`` / ``agent-studio.dev/ide-version`` envelope
（``{schemaVersion: 1, <field>: string}``；ACP SDK dispatch 会把 ``_meta``
内容直接展开进方法 ``kwargs``，键名即 envelope 键）。命中后覆盖 channel
上下文为 ``desktop``/ideName（决策 #7）。

上行：prompt 响应回传 ``agent-studio.dev/telemetry`` envelope
（``{schemaVersion: 1, requestId, spanId}``，取 registry 的 session 级最新
主对话调用），供 agent_studio_new 将来报 UI 交互数据时 join。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

IDE_NAME_META_KEY = "agent-studio.dev/ide-name"
IDE_VERSION_META_KEY = "agent-studio.dev/ide-version"
FUNCTION_NAME_META_KEY = "agent-studio.dev/function-name"
TELEMETRY_META_KEY = "agent-studio.dev/telemetry"

_MAX_OPTIONAL_VALUE_LENGTH = 512


def _envelope_string(metadata: Mapping[str, Any], meta_key: str, field_name: str) -> str | None:
    """读 envelope 字段：``schemaVersion == 1`` + 非空短字符串 + 无控制字符。"""
    value = metadata.get(meta_key)
    if not isinstance(value, Mapping):
        return None
    if value.get("schemaVersion") != 1:
        return None
    field = value.get(field_name)
    if not isinstance(field, str) or not field or len(field) > _MAX_OPTIONAL_VALUE_LENGTH:
        return None
    if any(ord(character) <= 0x1F or ord(character) == 0x7F for character in field):
        return None
    return field


def read_ide_channel_meta(kwargs: Mapping[str, Any]) -> None:
    """从 prompt ``kwargs`` 读 IDE 身份与功能入口 envelope。

    ide-name/ide-version 命中 → 覆盖为 desktop 渠道；function-name 独立读取
    （无 ide-name 的 prompt 也可能带功能入口），无则清除旧值。
    """
    from chrys.aixcoding.context import set_current_function_name, set_desktop_channel

    ide_name = _envelope_string(kwargs, IDE_NAME_META_KEY, "ideName")
    if ide_name is not None:
        set_desktop_channel(ide_name, _envelope_string(kwargs, IDE_VERSION_META_KEY, "ideVersion"))
    set_current_function_name(_envelope_string(kwargs, FUNCTION_NAME_META_KEY, "functionName"))


def telemetry_response_meta(session_id: str | None) -> dict[str, Any] | None:
    """prompt 响应的 ``_meta``（telemetry envelope）；无关联数据时返回 ``None``。"""
    from chrys.aixcoding.telemetry.llm_telemetry import resolve_call

    request = resolve_call("", session_id)
    if request is None:
        return None
    envelope: dict[str, Any] = {"schemaVersion": 1, "requestId": request[0]}
    if request[1]:
        envelope["spanId"] = request[1]
    return {TELEMETRY_META_KEY: envelope}
