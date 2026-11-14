# Use iCode inside Herdr

[Herdr](https://herdr.dev) is a terminal multiplexer built for coding agents: it arranges agent sessions in panes, watches their state, and notifies you when they need you. iCode needs no built-in support for it — its [hooks](../extensions/hooks.md) already see every lifecycle event, and a small recipe forwards them to Herdr's self-service reporting CLI. Set it up once and every Herdr pane running `icode` shows live state.

## What you get

- Herdr's sidebar lists the pane as the agent **icode**, and `herdr agent list` shows it with its state.
- The state follows your session: **working** while a turn runs, **blocked** while a tool waits for your approval, **idle** when the turn ends.
- When the pane sits in a background tab, Herdr can send a system notification and play a sound when a turn finishes or needs your decision.
- While a session is open, the pane reports its session id, so a restarted Herdr runs `icode --session <session-id>` in the pane and you return to the same conversation (Herdr 0.9.2 or newer).

## Install the bridge

The bridge is one hook script plus seven hook entries. It lives in your global hooks directory: `~/.chrys/hooks/` on macOS/Linux, `%APPDATA%\chrys\hooks\` on Windows.

**1.** Save this script as `<config_dir>/hooks/scripts/herdr_report.py`:

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

**2.** Add these entries to `<config_dir>/hooks/hooks.yaml` (create the file if this is your first hook; see the [hooks configuration reference](https://github.com/openJiuwen-ai/iCode/blob/main/src/chrys/service/hooks/docs/configuration.md) for the full format):

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

**3.** Restart iCode inside a Herdr pane and run a turn. `herdr agent list` now shows `icode`, and the state tracks the conversation.

## How states map

| Hook event | Herdr state |
| --- | --- |
| `before_turn` | working |
| `after_turn` (ok or interrupted) | idle |
| `after_turn` (failed) | idle, with a "turn failed" note |
| `approval_requested` | blocked, naming the tool |
| `approval_resolved` | working |
| `session_start` / `session_restored` | idle, reporting the session id and resume command |
| `session_end` | pane released |

## Notes

- The hooks run `fire_and_forget` with `on_error: ignore`, so reporting never waits on Herdr and a missing or hung Herdr harms nothing.
- Outside a Herdr pane the script exits before doing anything: no reports, no side effects, and the recipe is safe to keep installed everywhere.
- Tool approvals report **blocked**. Questions asked through the `ask_user` tool have no hook event yet, so they keep showing working.
