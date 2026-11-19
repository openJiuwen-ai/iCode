#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Profile long-session ChatPanel load, scroll, and memory behavior.

This is a focused developer benchmark, not a production entrypoint. It builds a
synthetic transcript with many chat-panel tool renderers, measures headless
Textual load and scroll latency, and reports widget/memory counts.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import statistics
import sys
import threading
import time
import tracemalloc
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psutil

from chrys.foundation.patches import apply_all

apply_all()

from chat_panel_workloads import (  # noqa: E402
    _populate_file_diff,
    _populate_mixed_markdown,
    _populate_process_history,
    _populate_realistic_session,
    _populate_sub_agent_markdown,
    _populate_table_markdown,
)
from textual import events  # noqa: E402
from textual.app import App, ComposeResult  # noqa: E402

from chrys.app.tui.theme import TuiVariableDefaultsMixin  # noqa: E402
from chrys.app.tui.widgets.chat.panel import ChatPanel  # noqa: E402
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown  # noqa: E402
from chrys.service.state.serializers import deserialize_compressed_block  # noqa: E402

_TIMINGS: dict[str, list[float]] = {}
_TIMINGS_LOCK = threading.Lock()
_MARKDOWN_INSTRUMENTATION_INSTALLED = False


@dataclass(frozen=True)
class Metrics:
    workload: str
    turns: int
    tool_sets_per_turn: int
    expected_tool_calls: int
    collapsed: bool
    displayed_transcript_entries: int
    process_blocks: int
    wait_for_diffs: bool
    scroll_driver: str
    scroll_wait: str
    settle_delay_seconds: float
    tracemalloc_enabled: bool
    markdown_lines: int
    markdown_fence_lines: int
    markdown_table_columns: int
    markdown_table_rows: int
    markdown_table_cell_words: int
    size: tuple[int, int]
    load_seconds: float
    settle_seconds: float
    scroll_total_seconds: float
    scroll_step_avg_ms: float
    scroll_step_p95_ms: float
    scroll_step_max_ms: float
    scroll_steps: int
    max_scroll_y: float
    direct_children: int
    descendants: int
    collapsed_tool_groups: int
    mounted_diff_views: int
    mounted_unified_diff_lines: int
    mounted_inline_unified_diff_lines: int
    mounted_code_columns: int
    mounted_sub_agent_calls: int
    mounted_virtualized_markdown: int
    rss_before_mb: float
    rss_after_load_mb: float
    rss_after_scroll_mb: float
    rss_after_gc_mb: float
    tracemalloc_current_mb: float
    tracemalloc_peak_mb: float
    top_widget_types: list[tuple[str, int]]
    timings: list[tuple[str, int, float]]


def _record_timing(name: str, elapsed: float) -> None:
    with _TIMINGS_LOCK:
        entry = _TIMINGS.setdefault(name, [0.0, 0.0])
        entry[0] += 1
        entry[1] += elapsed


def _timing_rows() -> list[tuple[str, int, float]]:
    with _TIMINGS_LOCK:
        rows = [(name, int(values[0]), values[1]) for name, values in _TIMINGS.items()]
    return sorted(rows, key=lambda item: item[2], reverse=True)


def _instrument_method(cls: type, method_name: str, label: str) -> None:
    original = getattr(cls, method_name)

    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            return original(self, *args, **kwargs)
        finally:
            _record_timing(label, time.perf_counter() - start)

    setattr(cls, method_name, wrapped)


def _install_markdown_instrumentation() -> None:
    """Install per-method timers for markdown parse/layout/render hot paths."""
    global _MARKDOWN_INSTRUMENTATION_INSTALLED
    if _MARKDOWN_INSTRUMENTATION_INSTALLED:
        return
    for method_name in (
        "_build_blocks",
        "_layout_blocks",
        "_build_table_layout",
        "_build_table_strips",
        "_build_fence_layout",
        "_build_fence_strips",
        "_render_fence_line",
        "_build_block_strips",
        "_render_lines_direct",
        "render_lines",
        "render_line",
    ):
        _instrument_method(VirtualizedMarkdown, method_name, f"VirtualizedMarkdown.{method_name}")
    _MARKDOWN_INSTRUMENTATION_INSTALLED = True


class ChatProfileApp(TuiVariableDefaultsMixin, App[None]):
    CSS = """
    ChatProfileApp {
        background: $surface;
    }
    """

    def compose(self) -> ComposeResult:
        yield ChatPanel(
            cwd="/tmp/chrys-profile",
            thinking_expanded=lambda: True,
        )


def _rss_mb(process: psutil.Process) -> float:
    return process.memory_info().rss / (1024 * 1024)


def _persisted_initial_profile(meta: object) -> str:
    """Resolve the oldest transcript label from persisted session metadata."""
    if not isinstance(meta, dict):
        return ""
    profile_history = meta.get("agent_profile_history", meta.get("profile_history", []))
    if isinstance(profile_history, list) and profile_history and isinstance(profile_history[0], str):
        return profile_history[0]
    display_name = meta.get("agent_display_name", meta.get("display_name", ""))
    if isinstance(display_name, str) and display_name:
        return display_name
    agent_profile = meta.get("agent_profile", "")
    return agent_profile if isinstance(agent_profile, str) else ""


async def _populate_panel(panel: ChatPanel, args: argparse.Namespace) -> None:
    if args.session_file is not None:
        envelope = json.loads(args.session_file.read_text(encoding="utf-8"))
        state = envelope.get("state", {})
        messages = state.get("messages", [])
        if not isinstance(messages, list):
            raise ValueError("persisted session state.messages must be a list")
        compressed_blocks = {
            block.compressed_context_id: block
            for raw_block in state.get("compressed_msgs", [])
            if isinstance(raw_block, dict)
            for block in [deserialize_compressed_block(raw_block)]
        }
        await panel.replay_history(
            messages,
            initial_profile=_persisted_initial_profile(envelope.get("meta")),
            compressed_blocks=compressed_blocks,
        )
        return
    if args.workload == "file-diff":
        await _populate_file_diff(
            panel,
            turns=args.turns,
            tool_sets_per_turn=args.tool_sets_per_turn,
            collapsed=args.collapsed,
        )
    elif args.workload == "realistic-session":
        await _populate_realistic_session(
            panel,
            turns=args.turns,
            collapsed=args.collapsed,
        )
    elif args.workload == "process-history":
        await _populate_process_history(panel, turns=args.turns, markdown_lines=args.markdown_lines)
    elif args.workload == "sub-agent-markdown":
        await _populate_sub_agent_markdown(
            panel,
            turns=args.turns,
            tool_sets_per_turn=args.tool_sets_per_turn,
            collapsed=args.collapsed,
            markdown_lines=args.markdown_lines,
        )
    elif args.workload == "table-markdown":
        await _populate_table_markdown(
            panel,
            turns=args.turns,
            messages_per_turn=args.tool_sets_per_turn,
            markdown_lines=args.markdown_lines,
            table_columns=args.markdown_table_columns,
            table_rows=args.markdown_table_rows,
            cell_words=args.markdown_table_cell_words,
        )
    elif args.workload == "mixed-markdown":
        await _populate_mixed_markdown(
            panel,
            turns=args.turns,
            messages_per_turn=args.tool_sets_per_turn,
            markdown_lines=args.markdown_lines,
            fence_lines=args.markdown_fence_lines,
            table_columns=args.markdown_table_columns,
            table_rows=args.markdown_table_rows,
            cell_words=args.markdown_table_cell_words,
        )
    else:
        raise ValueError(f"unknown workload: {args.workload}")


def _expected_tool_calls(args: argparse.Namespace) -> int:
    if args.workload == "process-history":
        return args.turns * 2
    if args.workload == "file-diff":
        return args.turns * args.tool_sets_per_turn * 3
    if args.workload == "realistic-session":
        return args.turns * 6
    if args.workload == "sub-agent-markdown":
        return args.turns * args.tool_sets_per_turn
    return 0


def _walk(widget: Any) -> Iterable[Any]:
    stack = list(widget.children)
    while stack:
        child = stack.pop()
        yield child
        stack.extend(child.children)


def _widget_counts(panel: ChatPanel) -> tuple[int, Counter[str]]:
    counts: Counter[str] = Counter(type(widget).__name__ for widget in _walk(panel))
    return sum(counts.values()), counts


async def run_profile(args: argparse.Namespace) -> Metrics:
    with _TIMINGS_LOCK:
        _TIMINGS.clear()
    if args.instrument_markdown:
        _install_markdown_instrumentation()

    process = psutil.Process()
    gc.collect()
    if not args.no_tracemalloc:
        tracemalloc.start()
    rss_before = _rss_mb(process)

    async with ChatProfileApp().run_test(size=tuple(args.size)) as pilot:
        panel = pilot.app.query_one(ChatPanel)

        async def wait_for_frame() -> None:
            if args.scroll_wait == "pilot":
                await pilot.pause()
                return
            await asyncio.sleep(0)
            pilot.app.screen._on_timer_update()
            await asyncio.sleep(0)

        def scroll_to_position(y: float) -> None:
            if args.scroll_driver == "deferred":
                panel.scroll_to(y=y, animate=False)
            elif args.scroll_driver == "immediate":
                panel.scroll_to(y=y, animate=False, immediate=True)
            elif args.scroll_driver == "direct":
                panel._scroll_to(y=y, animate=False, release_anchor=True)
            elif args.scroll_driver == "set-reactive":
                panel.scroll_y = y
                panel.scroll_target_y = y
            else:
                raise ValueError(f"unknown scroll driver: {args.scroll_driver}")

        def post_mouse_scroll(index: int, midpoint: int) -> None:
            event_cls = events.MouseScrollDown if index < midpoint else events.MouseScrollUp
            panel.post_message(
                event_cls(
                    panel,
                    x=1,
                    y=1,
                    delta_x=0,
                    delta_y=1,
                    button=0,
                    shift=False,
                    meta=False,
                    ctrl=False,
                )
            )

        load_start = time.perf_counter()
        await _populate_panel(panel, args)
        load_seconds = time.perf_counter() - load_start

        settle_start = time.perf_counter()
        if args.settle_delay > 0:
            await pilot.pause(args.settle_delay)
        if args.wait_for_diffs and args.workload in {"file-diff", "realistic-session"} and not args.collapsed:
            if args.workload == "realistic-session":
                expected_diff_views = args.turns * 2
            else:
                expected_diff_views = args.turns * args.tool_sets_per_turn * 2
            deadline = time.perf_counter() + 10.0
            while time.perf_counter() < deadline:
                _total, widget_counts = _widget_counts(panel)
                if (
                    widget_counts["DiffView"]
                    + widget_counts["InlineUnifiedDiffLines"]
                    + widget_counts["UnifiedDiffLines"]
                    >= expected_diff_views
                ):
                    break
                await pilot.pause(0.05)
        for _ in range(args.settle_frames):
            await wait_for_frame()
        settle_seconds = time.perf_counter() - settle_start
        rss_after_load = _rss_mb(process)

        max_scroll_y = panel.max_scroll_y
        steps = max(1, args.scroll_steps)
        positions = [round(max_scroll_y * index / steps) for index in range(steps + 1)]
        positions.extend(reversed(positions))
        mouse_steps = max(1, round(max_scroll_y / max(1, pilot.app.scroll_sensitivity_y)))
        mouse_total_steps = mouse_steps * 2
        step_times: list[float] = []
        scroll_start = time.perf_counter()
        scroll_iterations = mouse_total_steps if args.scroll_driver == "mouse" else len(positions)
        for index in range(scroll_iterations):
            step_start = time.perf_counter()
            if args.scroll_driver == "mouse":
                post_mouse_scroll(index, mouse_steps)
            else:
                scroll_to_position(positions[index])
            await wait_for_frame()
            step_times.append(time.perf_counter() - step_start)
        scroll_total_seconds = time.perf_counter() - scroll_start
        rss_after_scroll = _rss_mb(process)

        descendants, widget_counts = _widget_counts(panel)
        collapsed_tool_groups = sum(1 for group in panel.query("ToolGroup") if group.collapsed)
        gc.collect()
        rss_after_gc = _rss_mb(process)
        if args.no_tracemalloc:
            current = peak = 0
        else:
            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()

        return Metrics(
            workload=args.workload,
            turns=args.turns,
            tool_sets_per_turn=args.tool_sets_per_turn,
            expected_tool_calls=_expected_tool_calls(args),
            collapsed=args.collapsed,
            displayed_transcript_entries=sum(widget.display for widget in panel.transcript_content_children()),
            process_blocks=len(panel.query("ProcessBlock")),
            wait_for_diffs=args.wait_for_diffs,
            scroll_driver=args.scroll_driver,
            scroll_wait=args.scroll_wait,
            settle_delay_seconds=args.settle_delay,
            tracemalloc_enabled=not args.no_tracemalloc,
            markdown_lines=args.markdown_lines
            if args.workload in {"sub-agent-markdown", "table-markdown", "mixed-markdown"}
            else 0,
            markdown_fence_lines=args.markdown_fence_lines if args.workload == "mixed-markdown" else 0,
            markdown_table_columns=args.markdown_table_columns
            if args.workload in {"table-markdown", "mixed-markdown"}
            else 0,
            markdown_table_rows=args.markdown_table_rows
            if args.workload in {"table-markdown", "mixed-markdown"}
            else 0,
            markdown_table_cell_words=args.markdown_table_cell_words
            if args.workload in {"table-markdown", "mixed-markdown"}
            else 0,
            size=tuple(args.size),
            load_seconds=load_seconds,
            settle_seconds=settle_seconds,
            scroll_total_seconds=scroll_total_seconds,
            scroll_step_avg_ms=statistics.fmean(step_times) * 1000,
            scroll_step_p95_ms=statistics.quantiles(step_times, n=20)[18] * 1000 if len(step_times) >= 20 else 0,
            scroll_step_max_ms=max(step_times) * 1000,
            scroll_steps=len(step_times),
            max_scroll_y=max_scroll_y,
            direct_children=len(panel.children),
            descendants=descendants,
            collapsed_tool_groups=collapsed_tool_groups,
            mounted_diff_views=widget_counts["DiffView"],
            mounted_unified_diff_lines=widget_counts["UnifiedDiffLines"],
            mounted_inline_unified_diff_lines=widget_counts["InlineUnifiedDiffLines"],
            mounted_code_columns=widget_counts["CodeColumn"],
            mounted_sub_agent_calls=widget_counts["SubAgentToolCall"],
            mounted_virtualized_markdown=widget_counts["VirtualizedMarkdown"],
            rss_before_mb=rss_before,
            rss_after_load_mb=rss_after_load,
            rss_after_scroll_mb=rss_after_scroll,
            rss_after_gc_mb=rss_after_gc,
            tracemalloc_current_mb=current / (1024 * 1024),
            tracemalloc_peak_mb=peak / (1024 * 1024),
            top_widget_types=widget_counts.most_common(12),
            timings=_timing_rows(),
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workload",
        choices=(
            "file-diff",
            "realistic-session",
            "sub-agent-markdown",
            "table-markdown",
            "mixed-markdown",
            "process-history",
        ),
        default="file-diff",
    )
    parser.add_argument(
        "--session-file",
        type=Path,
        help="Replay messages from a persisted session.json instead of generating a synthetic workload.",
    )
    parser.add_argument("--turns", type=int, default=30)
    parser.add_argument("--tool-sets-per-turn", type=int, default=4)
    parser.add_argument(
        "--markdown-lines",
        type=int,
        default=1200,
        help="Approximate line count for each synthetic markdown result.",
    )
    parser.add_argument(
        "--markdown-fence-lines",
        type=int,
        default=80,
        help="Number of source lines in each synthetic mixed-markdown code fence.",
    )
    parser.add_argument(
        "--markdown-table-columns",
        type=int,
        default=8,
        help="Number of columns in each synthetic table-markdown table.",
    )
    parser.add_argument(
        "--markdown-table-rows",
        type=int,
        default=120,
        help="Number of body rows in each synthetic table-markdown table.",
    )
    parser.add_argument(
        "--markdown-table-cell-words",
        type=int,
        default=4,
        help="Extra words added to each synthetic table cell to force wrapping.",
    )
    parser.add_argument("--scroll-steps", type=int, default=80)
    parser.add_argument(
        "--scroll-driver",
        choices=("deferred", "immediate", "direct", "set-reactive", "mouse"),
        default="deferred",
        help=(
            "Scroll implementation to measure. 'deferred' matches Widget.scroll_to default scheduling; "
            "'immediate' and 'direct' avoid the after-refresh callback; 'mouse' posts wheel events."
        ),
    )
    parser.add_argument(
        "--scroll-wait",
        choices=("pilot", "light"),
        default="pilot",
        help=(
            "How to advance a frame after each scroll. 'pilot' waits for every widget to drain messages "
            "(useful for tests but O(widget count)); 'light' runs the screen timer update directly."
        ),
    )
    parser.add_argument("--settle-frames", type=int, default=6)
    parser.add_argument(
        "--settle-delay",
        type=float,
        default=0.0,
        help="Wall-clock seconds to wait after population before measuring scroll.",
    )
    parser.add_argument(
        "--wait-for-diffs",
        action="store_true",
        help="For expanded file-diff runs, wait until every write/edit diff widget is mounted.",
    )
    parser.add_argument("--size", type=int, nargs=2, default=(120, 36), metavar=("WIDTH", "HEIGHT"))
    parser.add_argument("--expanded", dest="collapsed", action="store_false")
    parser.set_defaults(collapsed=True)
    parser.add_argument("--instrument-markdown", action="store_true")
    parser.add_argument("--no-tracemalloc", action="store_true", help="Disable tracemalloc for latency-focused runs.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a human-readable summary.")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    metrics = asyncio.run(run_profile(args))
    payload = asdict(metrics)
    if args.json:
        sys.stdout.write(json.dumps(payload, indent=2) + "\n")
    else:
        for key, value in payload.items():
            sys.stdout.write(f"{key}: {value}\n")


if __name__ == "__main__":
    main()
