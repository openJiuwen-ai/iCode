# ruff: noqa: RUF002
"""故障注入配置与判定（telemetry-mock 专用，仅标准库）。

对齐 agent_studio_new TS 版 ``apps/telemetry-mock`` 的行为契约：
- 6 种模式：none / http500 / http503 / slow / envelope_reject / drop_body；
- ``rate`` 按确定性伪随机（同一毫秒内判定一致）抽样；
- ``interfaces`` 支持完整路径或短名（如 ``tool-detail/save``）定向。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

FAULT_MODES = ("none", "http500", "http503", "slow", "envelope_reject", "drop_body")

CSAS_PREFIX = "/csas/telemetry/api/v1/"


@dataclass
class FaultConfig:
    """运行期故障注入配置（经 ``POST /debug/faults`` 切换）。"""

    mode: str = "none"
    rate: float = 1.0
    slow_ms: int = 2000
    interfaces: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "rate": self.rate,
            "slowMs": self.slow_ms,
            "interfaces": list(self.interfaces),
        }


def short_name(path: str) -> str:
    """``/csas/telemetry/api/v1/tool-detail/save`` → ``tool-detail/save``。"""
    return path.removeprefix(CSAS_PREFIX)


def parse_fault_config(data: object) -> FaultConfig | None:
    """校验并构造配置；非法输入返回 ``None``（调用方回 400）。"""
    if not isinstance(data, dict):
        return None
    mode = data.get("mode", "none")
    if mode not in FAULT_MODES:
        return None
    rate = data.get("rate", 1.0)
    if isinstance(rate, bool) or not isinstance(rate, int | float) or not 0 <= rate <= 1:
        return None
    slow_ms = data.get("slowMs", 2000)
    if isinstance(slow_ms, bool) or not isinstance(slow_ms, int) or not 0 <= slow_ms <= 60_000:
        return None
    interfaces = data.get("interfaces", [])
    if not isinstance(interfaces, list) or not all(isinstance(item, str) for item in interfaces):
        return None
    return FaultConfig(
        mode=mode,
        rate=float(rate),
        slow_ms=slow_ms,
        interfaces=list(interfaces),
    )


def _pseudo_random(seed: float) -> float:
    """确定性伪随机：``frac(sin(seed) * 10000)``，同一毫秒内判定恒定。"""
    return math.sin(seed) * 10000.0 - math.trunc(math.sin(seed) * 10000.0)


def active_fault(config: FaultConfig, path: str, now_ms: float) -> str:
    """判定本次请求命中的故障模式（返回 ``"none"`` 表示不注入）。"""
    if config.mode == "none":
        return "none"
    if config.interfaces and path not in config.interfaces and short_name(path) not in config.interfaces:
        return "none"
    if config.rate < 1 and _pseudo_random(now_ms) > config.rate:
        return "none"
    return config.mode
