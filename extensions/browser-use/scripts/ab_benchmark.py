# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real-model A/B benchmark runner across the 5 local browser fixtures.

Runs ``icode run`` N times per scenario (default 4 -> 20 runs; 12 -> 60 runs)
in ``full``, ``compact``, or ``subagent`` mode, counting *all* tool calls
(including sub-agent delegation + internal calls, ``shell``, ``view_image``),
measuring end-to-end median and p90 latency, and verifying zero duplicate
server-side submissions.

Example::

    python scripts/ab_benchmark.py --allow-browser --mode compact --runs 4 --output compact-20.json
    python scripts/ab_benchmark.py --allow-browser --mode subagent --runs 4 --compare compact-20.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from benchmark_fixtures import serve  # noqa: E402

TOOL_CALL_RE = re.compile(r"^\s*→\s*(\S+)", re.MULTILINE)
SUMMARY_CALLS_RE = re.compile(r"(\d+)\s+tool calls")

SCENARIOS: tuple[dict[str, str], ...] = (
    {
        "id": "streaming_chat",
        "path": "/streaming-chat",
        "submission_key": "streaming_chat",
        "expected": "FINAL_WEATHER_ANSWER: Shanghai 24C clear",
        "prompt": (
            "Open {url}, submit the follow-up question 'What is Shanghai weather?' exactly once, "
            "wait until the streaming reply finishes, close the browser, and report the exact final answer."
        ),
    },
    {
        "id": "closed_shadow_form",
        "path": "/closed-shadow-form",
        "submission_key": "closed_shadow_form",
        "expected": "TICKET_CONFIRMED_#42",
        "prompt": (
            "Open {url}, fill the closed Shadow DOM 'Ticket Note' field with 'Need invoice copy', "
            "click 'Submit Ticket' once, wait for confirmation, close the browser, and report the confirmation code."
        ),
    },
    {
        "id": "delayed_button",
        "path": "/delayed-button",
        "submission_key": "delayed_button",
        "expected": "RELEASE_APPROVED_OK",
        "prompt": (
            "Open {url}, wait for 'Release Code' to become editable, enter 'REL-2026', "
            "click 'Approve Release' once, close the browser, and report the final release status."
        ),
    },
    {
        "id": "dynamic_ad_page",
        "path": "/dynamic-ad-page",
        "submission_key": "",
        "expected": "REGION_SUMMARY: Q4 revenue grew 18% YoY",
        "prompt": (
            "Open {url}. Ignore the flashing ad banner, wait for the 'Q4 report' section to finish loading, "
            "close the browser, and report the exact summary text."
        ),
    },
    {
        "id": "submit_response_lost",
        "path": "/submit-response-lost",
        "submission_key": "submit_response_lost",
        "expected": "do not resubmit",
        "prompt": (
            "Open {url}, click 'Confirm Order' once. If the response is lost or errors, do NOT resubmit; "
            "close the browser and report the observed status."
        ),
    },
)


def _count_all_tool_calls(stdout: str, stderr: str) -> int:
    combined = stdout + "\n" + stderr
    summary_matches = [int(m) for m in SUMMARY_CALLS_RE.findall(combined)]
    if summary_matches:
        return sum(summary_matches)
    return len(TOOL_CALL_RE.findall(combined))


def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * pct) - 1))
    return ordered[idx]


def _summarize(records: list[dict[str, object]], baseline: dict[str, object] | None = None) -> dict[str, object]:
    total = len(records)
    passed = [r for r in records if r["passed"]]
    timeouts = sum(1 for r in records if r["timed_out"])
    duplicates = sum(int(r["duplicate_submissions"]) for r in records)
    all_calls = [int(r["tool_calls"]) for r in records]
    pass_times = [int(r["elapsed_ms"]) for r in passed]
    avg_calls = round(sum(all_calls) / total, 2) if total else 0.0
    median_ms = int(statistics.median(pass_times)) if pass_times else 0
    p90_ms = _percentile(pass_times, 0.90)
    gates: dict[str, object] = {
        "avg_calls_le_15": avg_calls <= 15.0,
        "zero_duplicate_submissions": duplicates == 0,
    }
    if baseline and int(baseline.get("median_ms") or 0) > 0:
        base_med = int(baseline["median_ms"])  # type: ignore[arg-type]
        reduction = round((base_med - median_ms) / base_med, 3)
        gates["median_latency_drop_pct"] = round(reduction * 100, 1)
        gates["median_drop_ge_30pct"] = reduction >= 0.30
    return {
        "total_runs": total,
        "passed": len(passed),
        "failed": total - len(passed),
        "timeouts": timeouts,
        "duplicate_submissions": duplicates,
        "avg_tool_calls": avg_calls,
        "median_ms": median_ms,
        "p90_ms": p90_ms,
        "gates": gates,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--allow-browser", action="store_true", help="opt in to running real icode + Chrome")
    parser.add_argument("--mode", choices=["full", "compact", "subagent"], default="compact")
    parser.add_argument("--agent", default="Code", help="iCode agent profile to run")
    parser.add_argument("--runs", type=int, default=4, help="repetitions per scenario (4=20 runs, 12=60 runs)")
    parser.add_argument("--timeout", type=int, default=180, help="per-run timeout in seconds")
    parser.add_argument("--icode-cmd", default="icode", help="icode executable name or path")
    parser.add_argument("--output", type=Path, default=None, help="optional JSON report output path")
    parser.add_argument("--compare", type=Path, default=None, help="optional baseline JSON report to compare against")
    args = parser.parse_args()
    if not args.allow_browser:
        raise SystemExit("ab_benchmark runs real iCode agent turns and Chrome; pass --allow-browser")

    baseline_summary: dict[str, object] | None = None
    if args.compare is not None:
        baseline_summary = json.loads(args.compare.read_text(encoding="utf-8")).get("summary")

    records: list[dict[str, object]] = []
    with serve() as (origin, state):
        for run_idx in range(args.runs):
            for scenario in SCENARIOS:
                state.reset()
                prompt = scenario["prompt"].format(url=f"{origin}{scenario['path']}")
                with tempfile.TemporaryDirectory(prefix="icode-ab-", ignore_cleanup_errors=True) as tmp:
                    env = dict(os.environ)
                    env["ICODE_BROWSER_ALLOWED_ORIGINS"] = origin
                    env["ICODE_BROWSER_STATE_DIR"] = str(Path(tmp) / "state")
                    env["ICODE_BROWSER_HEADLESS"] = "1"
                    env["ICODE_BROWSER_TOOLSET"] = "compact" if args.mode in {"compact", "subagent"} else "full"
                    t0 = time.monotonic()
                    timed_out = False
                    stdout = ""
                    stderr = ""
                    returncode = -1
                    try:
                        proc = subprocess.run(
                            [args.icode_cmd, "run", prompt, "-a", args.agent],
                            stdin=subprocess.DEVNULL,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            timeout=args.timeout,
                            env=env,
                            check=False,
                        )
                        stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
                    except subprocess.TimeoutExpired as exc:
                        timed_out = True
                        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
                        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    tool_calls = _count_all_tool_calls(stdout, stderr)
                    sub_key = scenario["submission_key"]
                    sub_count = state.count(sub_key) if sub_key else 0
                    duplicates = max(0, sub_count - 1) if sub_key else 0
                    expected_ok = scenario["expected"].lower() in stdout.lower()
                    sub_ok = (sub_count == 1) if sub_key else True
                    passed = (returncode == 0) and (not timed_out) and expected_ok and sub_ok and (duplicates == 0)
                    record = {
                        "run": run_idx + 1,
                        "scenario": scenario["id"],
                        "mode": args.mode,
                        "passed": passed,
                        "timed_out": timed_out,
                        "returncode": returncode,
                        "elapsed_ms": elapsed_ms,
                        "tool_calls": tool_calls,
                        "submissions": sub_count,
                        "duplicate_submissions": duplicates,
                    }
                    records.append(record)
                    print(json.dumps({"event": "trial", **record}, ensure_ascii=True), flush=True)

    summary = _summarize(records, baseline=baseline_summary)
    report = {
        "mode": args.mode,
        "agent": args.agent,
        "runs_per_scenario": args.runs,
        "summary": summary,
        "records": records,
    }
    if args.output is not None:
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"event": "benchmark_summary", **summary}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
