# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""提示词层拦截 Hook（``user_prompt_submit``）。

对应《拦截规则一览表》的 IN-01 ~ IN-09：反编译工具、攻击工具、SQL 注入、
XSS、路径穿越、内网资产、同义语义簇、合规类（强制留痕）与失败兜底。

命中后通过 ``$CHRYS_HOOK_RESULT`` 写 ``{"action": "block", "reason": ...}``，
iCode 会把 ``reason`` 以 ``hook_blocked`` 错误展示给用户，同时不执行本轮。

用法见同目录 ``README.md``；规则与词典在 ``rules.json``。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _guard_core as guard  # ty: ignore[unresolved-import]  # copied beside this script at install time


def main() -> int:
    """评估用户提示词；返回进程退出码。"""
    return guard.run_hook("prompt")


if __name__ == "__main__":
    raise SystemExit(main())
