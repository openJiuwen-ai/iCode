# ruff: noqa: RUF001, RUF002
"""运行上下文：channel 三元组（argv 识别 + ``_meta`` 覆盖）与 userId provider 接口。"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

CHANNEL_CLI = "cli"
CHANNEL_DESKTOP = "desktop"


@dataclass(frozen=True)
class ChannelContext:
    channel_type: str
    channel_name: str
    channel_version: str | None


_VALUE_OPTIONS = {"-s", "-a", "-m", "-C"}
"""已知带值启动选项（TUI 的 session/agent/model/workdir）：识别子命令时跳过其值。"""


def _first_positional(args: list[str]) -> str | None:
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in _VALUE_OPTIONS:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg
    return None


def detect_channel(argv: Sequence[str] | None = None) -> ChannelContext:
    """按启动 argv 识别渠道（方案决策 #7）。

    - TUI（无子命令 / ``serve``）→ ``cli`` / ``icode-tui``；
    - headless ``run`` 及其他子命令 → ``cli`` / ``icode-cli``；
    - ``acp`` → ``cli`` / ``icode-acp``（初始值；agent_studio_new 形态经 prompt
      ``_meta`` 注入 ideName/ideVersion 后由 ``set_desktop_channel`` 覆盖为
      ``desktop``/ideName——挂点在 M2 的 ACP `_meta` 集成）。
    """
    args = list(sys.argv if argv is None else argv)[1:]
    positional = _first_positional(args)
    if positional is None or positional == "serve":
        return ChannelContext(CHANNEL_CLI, "icode-tui", None)
    if positional == "acp":
        return ChannelContext(CHANNEL_CLI, "icode-acp", None)
    return ChannelContext(CHANNEL_CLI, "icode-cli", None)


_desktop_channel: ChannelContext | None = None


def set_desktop_channel(channel_name: str, channel_version: str | None) -> None:
    """记录 ACP ``_meta`` 下行的桌面端身份（ideName/ideVersion）。

    进程级最近值近似：iCode ACP server 常驻多 session 时最后下发者赢
    （单 agent_studio_new 客户端场景 ideName 恒定，无实际差异）；若将来
    需要严格 per-session，应把覆盖改存 session 级 channel context。
    """
    global _desktop_channel
    _desktop_channel = ChannelContext(CHANNEL_DESKTOP, channel_name, channel_version)


def clear_desktop_channel() -> None:
    """清除桌面端覆盖（测试辅助）。"""
    global _desktop_channel
    _desktop_channel = None


def current_channel() -> ChannelContext:
    """当前渠道：桌面端覆盖优先，否则按 argv 检测。"""
    return _desktop_channel or detect_channel()


_function_name: str | None = None


def set_current_function_name(name: str | None) -> None:
    """记录 ACP ``_meta`` 下行的功能入口名（per-prompt；``None`` 清除旧值）。

    与 desktop channel 同为进程级最近值近似（见 ``set_desktop_channel``）。
    """
    global _function_name
    _function_name = name


def current_function_name() -> str | None:
    """最近一次 prompt 下行的 functionName（方案 §4.5，报文 agentName 字段）。"""
    return _function_name


class UserIdProvider(Protocol):
    """userId 数据源接口；登录上线后由 ``aixcoding.auth`` 注册新实现（M4），接口不变。"""

    def __call__(self) -> str | None: ...


@dataclass(frozen=True)
class ConfigUserIdProvider:
    """当前实现：读 aixcoding 配置文件的 ``userId``（如工号）。"""

    def __call__(self) -> str | None:
        from chrys.aixcoding.config import load_settings

        return load_settings().user_id


_plugin_version_cache: str | None = None


def plugin_version() -> str:
    """iCode 版本（csas 报文 pluginVersion 字段；缓存首查结果）。"""
    global _plugin_version_cache
    if _plugin_version_cache is None:
        import importlib.metadata

        try:
            _plugin_version_cache = importlib.metadata.version("chrys")
        except importlib.metadata.PackageNotFoundError:  # pragma: no cover - source runs
            _plugin_version_cache = "0.0.0"
    return _plugin_version_cache
