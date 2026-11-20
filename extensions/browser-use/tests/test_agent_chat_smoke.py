# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The model-level acceptance must read both streams, not only stdout."""

from __future__ import annotations

import runpy
from pathlib import Path

SMOKE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "agent_chat_smoke.py"))


def test_browser_calls_reads_tool_progress_from_stderr() -> None:
    stdout = "→ browser_open\n→ browser_navigate\n"
    stderr = "→ browser_find\n   → browser_click\n→ read\n"
    assert SMOKE["browser_calls"](stdout, stderr) == [
        "browser_open",
        "browser_navigate",
        "browser_find",
        "browser_click",
    ]


def test_browser_calls_ignores_other_tools_and_empty_output() -> None:
    assert SMOKE["browser_calls"]("→ read_file\n→ shell\n", "") == []
    assert SMOKE["browser_calls"]("", "") == []


def test_browser_calls_still_sees_a_run_that_prints_to_stdout() -> None:
    # Older hosts print progress to stdout; the count must not depend on the stream.
    assert SMOKE["browser_calls"]("", "→ browser_evaluate\n") == ["browser_evaluate"]
