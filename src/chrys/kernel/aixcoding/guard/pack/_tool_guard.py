# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""工具调用前拦截 Hook（``before_tool_call``）。

对应《拦截规则一览表》的 T-* 规则：Shell 危险命令 / Zsh 攻击 / 命令注入 /
Shell 内建、PowerShell USB 审计与逆向工具、Python 反编译、文件写入白名单、
路径与扩展名黑名单、网络黑白名单、MCP 校验、敏感信息入参。

命中后通过 ``$CHRYS_HOOK_RESULT`` 写 ``{"action": "block", "reason": ...}``，
iCode 会拒绝本次工具调用，并把 ``reason`` 展示给用户和模型。

用法见同目录 ``README.md``；规则与词典在 ``rules.json``。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _guard_core as guard  # ty: ignore[unresolved-import]  # copied beside this script at install time


def main() -> int:
    """评估本次工具调用；返回进程退出码。"""
    return guard.run_hook("tool")


if __name__ == "__main__":
    raise SystemExit(main())
