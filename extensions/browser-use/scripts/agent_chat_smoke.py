# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Model-level icode-browser-use acceptance on the fixture chat SPA.

Unlike the other smokes this one does not call the adapter itself: it hands a
plain user task to a real agent (``icode run -a BrowserExt``) and judges only
what the model did with the browser tools. The fixture's server counters say
whether the message was really sent exactly once; the agent's transcript says
how many tool calls it took and whether it reached for ``browser_evaluate``.

Pass criteria (all of them):

* the agent finishes successfully and reports the answer sentence;
* the fixture counted exactly one submitted message;
* ``browser_evaluate`` was never called;
* no more than ``--max-calls`` browser tool calls were used.

The plugin under test must be installed into the iCode environment that
``icode`` runs from (this script runs the adapter only to read the fixture
state, never to drive the page).

iCode 0.36 writes tool progress to stderr, so the transcript checks read both
streams; counting only stdout would report zero calls and pass vacuously.

Run from the repository root::

    python scripts/agent_chat_smoke.py --allow-browser
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from fixture_site import serve  # noqa: E402

CALL = re.compile(r"^\s*→\s*(\S+)", re.MULTILINE)
SUMMARY = re.compile(r"(\d+)\s+tool calls")


def browser_calls(stdout: str, stderr: str) -> list[str]:
    """Browser tool names the agent called, from both streams (0.36 reports progress on stderr)."""
    return [name for name in CALL.findall(stdout + "\n" + stderr) if name.startswith("browser_")]


def report(event: str, **fields: object) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=True), flush=True)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _host_python() -> str:
    """The interpreter that runs iCode (and therefore the installed extension)."""
    override = os.environ.get("ICODE_HOST_PYTHON")
    if override:
        return override
    if sys.platform == "win32":
        candidate = Path(os.environ.get("APPDATA", "")) / "uv" / "tools" / "icode-tui" / "Scripts" / "python.exe"
    else:
        candidate = Path.home() / ".local" / "share" / "uv" / "tools" / "icode-tui" / "bin" / "python"
    if not candidate.is_file():
        raise SystemExit("agent_chat_smoke cannot find iCode's interpreter; set ICODE_HOST_PYTHON")
    return str(candidate)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--allow-browser", action="store_true", help="Opt in to running a real agent and browser")
    parser.add_argument("--agent", default="BrowserExt", help="iCode agent profile that loads the extension")
    parser.add_argument("--max-calls", type=int, default=25, help="Largest acceptable number of browser tool calls")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds to let the agent run")
    args = parser.parse_args()
    if not args.allow_browser:
        raise SystemExit("agent_chat_smoke runs a real agent and browser; pass --allow-browser")
    host_python = _host_python()

    with tempfile.TemporaryDirectory(prefix="agent-chat-", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp).resolve()
        log_file = Path(tempfile.gettempdir()) / "agent-chat-smoke-last.log"
        session_root = Path(tempfile.gettempdir()) / "agent-chat-sessions"
        shutil.rmtree(session_root, ignore_errors=True)
        with serve() as (origin, state):
            workspace = root / "workspace"
            workspace.mkdir()
            prompt = workspace / "prompt.txt"
            prompt.write_text(
                "打开 {origin}/chat 。左侧是三个只有图标的会话链接，可访问名是 /chat/c1、/chat/c2、/chat/c3。\n"
                "请点开第二个会话 /chat/c2，读一下页面上已有的回答；\n"
                '然后在页面底部的输入框里输入 "What about tomorrow in Shanghai?" 并发送；\n'
                "等新的回答生成完成后，把回答里关于明天天气的那一句（含温度和是否带伞）原样告诉我。\n"
                "只使用浏览器工具，不要写脚本，不要使用 browser_evaluate。\n".format(origin=origin),
                encoding="utf-8",
            )
            env = {
                **os.environ,
                "ICODE_BROWSER_ALLOWED_ORIGINS": origin,
                "ICODE_BROWSER_STATE_DIR": str(root / "state"),
                "ICODE_BROWSER_IDLE_CLOSE_SECONDS": "0",
                "ICODE_BROWSER_HEADLESS": "1",
                "ICODE_BROWSER_EVALUATE": "allow",
                "ICODE_BROWSER_BRIDGE": "",
                "ICODE_BROWSER_CDP_URL": "",
                "CHRYS_SESSION_ROOT_DIR": str(session_root),
            }
            report("fixture", origin=origin, agent=args.agent)
            report(
                "child_env",
                allowed_origins=env["ICODE_BROWSER_ALLOWED_ORIGINS"],
                allowed_domains=env.get("ICODE_BROWSER_ALLOWED_DOMAINS"),
            )
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    [host_python, "-m", "chrys.app.cli.app", "run", "-t", str(prompt), "-a", args.agent],
                    cwd=workspace,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=args.timeout,
                )
            except subprocess.TimeoutExpired as exc:
                report("agent_run", exit="timeout", elapsed_s=round(time.monotonic() - started, 1))
                require(False, f"the agent did not finish within {args.timeout}s: {exc}")
            elapsed = round(time.monotonic() - started, 1)
            output = completed.stdout + ("\n" + completed.stderr if completed.stderr else "")
            log_file.write_text(output, encoding="utf-8")
            # iCode 0.36 reports tool progress on stderr; read both streams so an
            # empty stdout cannot turn the call-count and evaluate checks green.
            calls = CALL.findall(output)
            tool_names = browser_calls(completed.stdout, completed.stderr or "")
            session_id = re.search(r"session ([0-9a-f]{6,})", output)
            tool_calls = int(SUMMARY.search(output).group(1)) if SUMMARY.search(output) else len(calls)
            counters = state.snapshot()
            submits = int(counters.get("counts", {}).get("chat_submits", 0))
            report(
                "agent_run",
                exit=completed.returncode,
                elapsed_s=elapsed,
                tool_calls=tool_calls,
                browser_calls=len(tool_names),
                evaluate_calls=tool_names.count("browser_evaluate"),
                submits=submits,
                tools=tool_names,
                log=str(log_file),
                session_root=str(session_root),
                session_id=session_id.group(1) if session_id else None,
            )
            if completed.returncode != 0 or not tool_names:
                report("agent_output_tail", text=output[-2500:])
            require(completed.returncode == 0, f"the agent run failed (exit {completed.returncode}); see the log")
            require(submits == 1, f"the fixture counted {submits} submitted messages, not exactly one")
            require("browser_evaluate" not in tool_names, "the agent reached for browser_evaluate")
            require(
                len(tool_names) <= args.max_calls,
                f"the agent needed {len(tool_names)} browser calls, more than {args.max_calls}",
            )
            require("light jacket" in output.lower(), "the agent did not report the streamed answer")
            require(
                "what about tomorrow in shanghai" in output.lower(),
                "the agent did not report the question it sent",
            )
            report(
                "acceptance_passed",
                checks=[
                    "agent finished",
                    "exactly one message submitted",
                    "no browser_evaluate",
                    f"browser calls <= {args.max_calls}",
                    "answer sentence reported",
                ],
            )


if __name__ == "__main__":
    main()
