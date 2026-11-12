# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Reproducible microbenchmark: legacy readonly pre-judge vs structured AST/YAML PR1.

Measures the synchronous decision functions only; does not execute commands,
perform Hooks or LLM review, or measure end-to-end tool authorization.
Only the optional Tree-sitter parser dependencies are needed.
"""
from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

from chrys.service.approval.prejudge_v2 import analyze, load_policy, route
from chrys.service.tools.builtins.shell_filter import is_safe_readonly_command

CASES = (
    ("simple_git", "git status"),
    ("simple_echo", "echo ok"),
    ("quoted_git", "git 'status'"),
    ("concatenated_head", "g''it status"),
    ("quoted_dangerous_argument", 'echo "rm"'),
    ("and_chain", "git status && echo done"),
    ("or_chain", "git status || echo failed"),
    ("pipeline", "echo done | wc -c"),
    ("semicolon_chain", "git status; echo ok"),
    ("newline_chain", "git status\necho ok"),
    ("brace_group", "{ git status; echo ok; }"),
    ("subshell", "(git status; echo ok)"),
    ("nested_group", "git status && { echo ok; pwd; }"),
    ("quoted_operator", "echo 'a|b'"),
    ("command_substitution", 'echo "$(rm file.txt)"'),
    ("redirect", "git status > out.txt"),
    ("dynamic_suffix", 'rm "$TARGET"'),
    ("opaque_wrapper", "sudo git status"),
)

POLICY_YAML = """schema_version: 1
mode: "enforce"
policy_id: benchmark-pilot-v1
rules:
  - id: block-rm
    effect: block
    target: command
    match:
      prefix_first: [rm]
  - id: allow-git-status
    effect: allow
    target: command
    match:
      argv_exact: [git, status]
  - id: allow-echo
    effect: allow
    target: command
    match:
      argv_first: [echo]
  - id: allow-pwd
    effect: allow
    target: command
    match:
      argv_exact: [pwd]
  - id: allow-wc
    effect: allow
    target: command
    match:
      argv_first: [wc]
"""


def _percentile_ns(values: list[int], fraction: float) -> float:
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, int(fraction * (len(ordered) - 1)))])


def _summarize(values: list[int]) -> dict[str, float]:
    return {
        "mean_us": round(statistics.fmean(values) / 1000, 3),
        "p50_us": round(_percentile_ns(values, 0.50) / 1000, 3),
        "p95_us": round(_percentile_ns(values, 0.95) / 1000, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000, help="iterations per case per method")
    parser.add_argument("--warmup", type=int, default=100)
    args = parser.parse_args()
    if args.iterations < 1 or args.warmup < 0:
        parser.error("iterations must be positive and warmup nonnegative")

    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "prejudge.yaml"
        path.write_text(POLICY_YAML, encoding="utf-8")
        policy = load_policy(path)

    def old(command: str) -> bool:
        return is_safe_readonly_command(command, shell_name="bash")

    def new(command: str) -> str:
        return route(command, "bash", policy).route.value

    # Every case must produce a result; unsupported syntax must be reported
    # rather than silently dropped. Include parsing status, not only decision.
    comparisons = []
    for name, command in CASES:
        parsed = analyze(command, dialect="bash")
        comparisons.append({
            "case": name,
            "command": command,
            "legacy_auto_allow": old(command),
            "new_syntax_valid": parsed.syntax_valid,
            "new_coverage_complete": parsed.coverage_complete,
            "new_executable_argv": [list(node.argv) if node.argv is not None else None
                                    for node in parsed.executable_nodes],
            "new_unsupported": list(parsed.unsupported),
            "new_route": new(command),
        })

    samples = list(CASES) * args.iterations
    random.Random(20261028).shuffle(samples)
    for _ in range(args.warmup):
        for _, command in CASES:
            old(command)
            new(command)

    readings: dict[str, dict[str, list[int]]] = {
        name: {"old": [], "new": []} for name, _ in CASES
    }
    # Alternate method order for each sample, eliminating the systematic
    # first/second method cache bias.
    for index, (name, command) in enumerate(samples):
        methods = (("old", old), ("new", new))
        if index % 2:
            methods = tuple(reversed(methods))
        for method_name, fn in methods:
            start = time.perf_counter_ns()
            fn(command)
            readings[name][method_name].append(time.perf_counter_ns() - start)

    overall = {
        method: _summarize([
            elapsed for pair in readings.values() for elapsed in pair[method]
        ])
        for method in ("old", "new")
    }
    overhead = {
        "mean_delta_us": round(overall["new"]["mean_us"] - overall["old"]["mean_us"], 3),
        "mean_ratio": round(overall["new"]["mean_us"] / overall["old"]["mean_us"], 3),
        "p50_delta_us": round(overall["new"]["p50_us"] - overall["old"]["p50_us"], 3),
        "p95_delta_us": round(overall["new"]["p95_us"] - overall["old"]["p95_us"], 3),
    }
    result = {
        "benchmark": "prejudge_pr1_microbenchmark",
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "iterations_per_case": args.iterations,
        "warmup_per_case": args.warmup,
        "cases": comparisons,
        "overall": overall,
        "overhead": overhead,
        "by_case": {
            name: {method: _summarize(data[method]) for method in ("old", "new")}
            for name, data in readings.items()
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
