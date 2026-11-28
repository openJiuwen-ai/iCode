# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Parameter validation for the per-tool specs carried by ``ToolDef.params``.

The spec is deliberately tiny - ``type`` / ``required`` / ``default`` / ``enum``
/ ``min`` / ``max`` / ``maxLength`` / ``pattern`` / ``items`` / ``minItems`` /
``maxItems`` - which is all the browser tools need and keeps the generated
``inputSchema`` readable for the model. Anything the model gets wrong is reported
as ``browser_bad_request`` before an action starts.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .errors import BadRequest


def to_json_schema(params: dict[str, Any]) -> dict[str, Any]:
    """Project the internal parameter spec onto a tool ``inputSchema`` object."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, spec in params.items():
        properties[name] = {
            {"min": "minimum", "max": "maximum"}.get(key, key): value
            for key, value in spec.items()
            if key != "required"
        }
        if spec.get("required"):
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def validate(params: dict[str, Any], arguments: Any) -> dict[str, Any]:
    """Validate *arguments* against *params*, applying defaults.

    Returns a new dict holding only known keys. Raises :class:`BadRequest` with a
    model-readable reason on any problem.
    """
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise BadRequest("arguments 必须是 JSON 对象")
    unknown = sorted(set(arguments) - set(params))
    if unknown:
        raise BadRequest(f"未知参数：{', '.join(unknown)}")
    resolved: dict[str, Any] = {}
    for name, spec in params.items():
        if name in arguments:
            resolved[name] = _coerce(name, spec, arguments[name])
        elif "default" in spec:
            resolved[name] = spec["default"]
        elif spec.get("required"):
            raise BadRequest(f"缺少必填参数：{name}")
    return resolved


def _coerce(name: str, spec: dict[str, Any], value: Any) -> Any:
    if "enum" in spec:
        allowed = list(spec["enum"])
        if value not in allowed:
            raise BadRequest(f"参数 {name} 取值非法（允许：{', '.join(map(str, allowed))}）")
        return value
    kind = spec.get("type")
    if kind == "string":
        if not isinstance(value, str):
            raise BadRequest(f"参数 {name} 必须是字符串")
        limit = spec.get("maxLength")
        if limit is not None and len(value) > limit:
            raise BadRequest(f"参数 {name} 最多 {limit} 个字符")
        pattern = spec.get("pattern")
        if pattern and re.fullmatch(pattern, value) is None:
            raise BadRequest(f"参数 {name} 格式非法（应匹配 {pattern}）")
        return value
    if kind == "boolean":
        if not isinstance(value, bool):
            raise BadRequest(f"参数 {name} 必须是布尔值")
        return value
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise BadRequest(f"参数 {name} 必须是整数")
        lower, upper = spec.get("min"), spec.get("max")
        if lower is not None and value < lower:
            raise BadRequest(f"参数 {name} 小于下限 {lower}")
        if upper is not None and value > upper:
            raise BadRequest(f"参数 {name} 大于上限 {upper}")
        return value
    if kind == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BadRequest(f"参数 {name} 必须是数字")
        num = float(value)
        if not math.isfinite(num):
            raise BadRequest(f"参数 {name} 必须是有限数字")
        lower, upper = spec.get("min"), spec.get("max")
        if lower is not None and num < lower:
            raise BadRequest(f"参数 {name} 小于下限 {lower}")
        if upper is not None and num > upper:
            raise BadRequest(f"参数 {name} 大于上限 {upper}")
        return num
    if kind == "array":
        if not isinstance(value, list):
            raise BadRequest(f"参数 {name} 必须是数组")
        items = spec.get("items") or {}
        if items.get("type") == "string":
            limit = items.get("maxLength")
            for item in value:
                if not isinstance(item, str):
                    raise BadRequest(f"参数 {name} 的每一项都必须是字符串")
                if limit is not None and len(item) > limit:
                    raise BadRequest(f"参数 {name} 的每一项最多 {limit} 个字符")
        lower, upper = spec.get("minItems"), spec.get("maxItems")
        if lower is not None and len(value) < lower:
            raise BadRequest(f"参数 {name} 至少需要 {lower} 项")
        if upper is not None and len(value) > upper:
            raise BadRequest(f"参数 {name} 最多允许 {upper} 项")
        return list(value)
    if kind == "object":
        if not isinstance(value, dict):
            raise BadRequest(f"参数 {name} 必须是 JSON 对象")
        max_props = spec.get("maxProperties")
        if max_props is not None and len(value) > max_props:
            raise BadRequest(f"参数 {name} 最多允许 {max_props} 个字段")
        add_props = spec.get("additionalProperties")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise BadRequest(f"参数 {name} 的键必须是字符串")
            result[key] = _coerce(f"{name}.{key}", add_props, item) if isinstance(add_props, dict) else item
        return result
    return value
