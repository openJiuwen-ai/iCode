# 在 Herdr 里使用 iCode

[Herdr](https://herdr.dev) 是一个为编码 agent 设计的终端多路复用器:它把 agent 会话排布在各个 pane 里,监视它们的状态,并在需要你时通知你。iCode 不需要为它做任何内置支持——[hooks](../extensions/hooks.md) 已经能看到每一个生命周期事件,一个小配方就能把它们转发给 Herdr 的自助上报 CLI。配置一次,所有运行 `icode` 的 Herdr pane 都会显示实时状态。

## 你会得到什么

- Herdr 侧栏把该 pane 列为 agent **icode**,`herdr agent list` 也会显示它及其状态。
- 状态跟随你的会话:回合运行中为 **working**,工具等待你审批时为 **blocked**,回合结束后为 **idle**。
- 当 pane 位于后台标签页时,回合结束或需要你决策的那一刻,Herdr 可以发送系统通知并播放提示音。
- 会话打开期间,pane 会报告自己的会话 id,因此 Herdr 服务器重启后会在 pane 里自动执行 `icode --session <会话id>`,带你回到原对话(需 Herdr 0.9.2 或更高版本)。

## 安装桥接

桥接由一个 hook 脚本和七个 hook 条目组成,安装在你的全局 hooks 目录:macOS/Linux 为 `~/.chrys/hooks/`,Windows 为 `%APPDATA%\chrys\hooks\`。

**1.** 把下面的脚本保存为 `<config_dir>/hooks/scripts/herdr_report.py`:

```python
"""Forward iCode hook events to the Herdr pane owner (https://herdr.dev)."""

import json
import os
import subprocess
import sys
import time


def _seq() -> str:
    # Monotonic across processes: Herdr drops reports with an old seq.
    return str(time.time_ns())


def _run(argv: list[str]) -> None:
    try:
        subprocess.run(
            argv,
            check=False,
            timeout=5,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def main() -> int:
    if os.environ.get("HERDR_ENV") != "1":
        return 0
    pane_id = os.environ.get("HERDR_PANE_ID", "").strip()
    bin_path = os.environ.get("HERDR_BIN_PATH", "").strip()
    if not pane_id or not bin_path:
        return 0
    payload = json.load(sys.stdin)
    event = payload.get("event", "")
    session_id = payload.get("session_id", "")

    def report(state: str, message: str = "", with_session: bool = False) -> list[str]:
        argv = [
            bin_path, "pane", "report-agent", pane_id,
            "--source", "icode", "--agent", "icode",
            "--state", state, "--seq", _seq(),
        ]
        if message:
            argv += ["--message", message]
        if with_session:
            argv += ["--agent-session-id", session_id, "--", "icode", "--session", session_id]
        return argv

    if event == "before_turn":
        _run(report("working"))
    elif event == "after_turn":
        message = "turn failed" if payload.get("status") == "failed" else ""
        _run(report("idle", message=message))
    elif event == "approval_requested":
        tool = payload.get("tool") or {}
        _run(report("blocked", message=tool.get("name") or "approval"))
    elif event == "approval_resolved":
        _run(report("working"))
    elif event in ("session_start", "session_restored"):
        _run(report("idle", with_session=True))
    elif event == "session_end":
        _run([
            bin_path, "pane", "release-agent", pane_id,
            "--source", "icode", "--agent", "icode", "--seq", _seq(),
        ])
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**2.** 把这些条目加进 `<config_dir>/hooks/hooks.yaml`(如果这是你的第一个 hook,先创建该文件;完整格式见 [hooks 配置参考](https://github.com/openJiuwen-ai/iCode/blob/main/src/chrys/service/hooks/docs/configuration.md)):

```yaml
version: 1

hooks:
  - id: herdr-before-turn
    event: before_turn
    run: {type: script, path: scripts/herdr_report.py}
    execution: {mode: fire_and_forget, timeout_seconds: 5, on_error: ignore}

  - id: herdr-after-turn
    event: after_turn
    run: {type: script, path: scripts/herdr_report.py}
    execution: {mode: fire_and_forget, timeout_seconds: 5, on_error: ignore}

  - id: herdr-approval-requested
    event: approval_requested
    run: {type: script, path: scripts/herdr_report.py}
    execution: {mode: fire_and_forget, timeout_seconds: 5, on_error: ignore}

  - id: herdr-approval-resolved
    event: approval_resolved
    run: {type: script, path: scripts/herdr_report.py}
    execution: {mode: fire_and_forget, timeout_seconds: 5, on_error: ignore}

  - id: herdr-session-start
    event: session_start
    run: {type: script, path: scripts/herdr_report.py}
    execution: {mode: fire_and_forget, timeout_seconds: 5, on_error: ignore}

  - id: herdr-session-restored
    event: session_restored
    run: {type: script, path: scripts/herdr_report.py}
    execution: {mode: fire_and_forget, timeout_seconds: 5, on_error: ignore}

  - id: herdr-session-end
    event: session_end
    run: {type: script, path: scripts/herdr_report.py}
    execution: {mode: fire_and_forget, timeout_seconds: 5, on_error: ignore}
```

**3.** 在 Herdr pane 里重启 iCode 并跑一轮对话。`herdr agent list` 此时会显示 `icode`,状态也会跟随对话变化。

## 状态如何映射

| Hook 事件 | Herdr 状态 |
| --- | --- |
| `before_turn` | working |
| `after_turn`(ok 或 interrupted) | idle |
| `after_turn`(failed) | idle,附 "turn failed" 说明 |
| `approval_requested` | blocked,并给出工具名 |
| `approval_resolved` | working |
| `session_start` / `session_restored` | idle,同时上报会话 id 与恢复命令 |
| `session_end` | 释放 pane |

## 说明

- 这些 hook 以 `fire_and_forget` 运行且 `on_error: ignore`,上报从不等待 Herdr;Herdr 缺失或卡住也没有任何影响。
- 在 Herdr pane 之外,脚本会在做任何事之前直接退出:不上报、无副作用,这份配方装在哪台机器上都安全。
- 工具审批会报 **blocked**。`ask_user` 工具提的问题目前还没有对应的 hook 事件,因此仍显示 working。
