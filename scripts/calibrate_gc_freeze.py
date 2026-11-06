# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Measure the GC-freeze retained-byte budget on a realistic TUI transcript.

Run serially from the repository root::

    uv run python scripts/calibrate_gc_freeze.py --turns 12

The probe mounts the real ChrysApp/MainScreen graph and exercises coordinator
sequences, every participant, background timers/tasks, transcript Markdown/diff
LRUs, selection, viewport invalidation, full transcript scrolling, hidden
terminal output, a populated file-suggestion index, and repeated frozen
ToolGroup removal. Optional repeated theme invalidation is a separate policy
stress. The script emits one JSON report and exits nonzero when any whole-App,
retained-byte, dead-object, removal-recovery, or ratio-based latency gate fails.
Action latency includes any deferred full-screen layout an action schedules
(the cyclic-cache fallback's cache renewal), not only the coordinator's
synchronous GC/hook duration. The absorb and full-reclaim gates express it in
bare full collections of a matched unfrozen heap, a basis that no overhead of
the freeze design can inflate; the ratios against the unfrozen scan run through
the participant hooks are reported alongside for comparison.
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import statistics
import sys
import time
import tracemalloc
import weakref
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from gc_freeze_calibration_math import (
    aggregate_dead_cyclic_fraction as calculate_aggregate_dead_cyclic_fraction,
)
from gc_freeze_calibration_math import (
    dead_cyclic_fraction,
    latency_in_unfrozen_collects,
    minimum_deferred_diff_surfaces,
    steady_growth_per_cycle,
    validated_absorb_points,
)
from profile_chat_panel import _populate_realistic_session
from textual import _time as textual_time
from textual.geometry import Offset, Region, Size
from textual.selection import Selection

from chrys.app.features.session_title import SessionTitleUpdater
from chrys.app.tui.app import ChrysApp
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.support import workspace_mru
from chrys.app.tui.support.gc_freeze import (
    _FROZEN_DEAD_BUDGET_BYTES,
    _RECLAIM_HARD_ABSORBS,
    _RECLAIM_SOFT_ABSORBS,
    GcAbsorbReason,
    GcFreezeActionMetrics,
    GcFreezeBlockReason,
    GcFreezeCoordinator,
    GcReclaimReason,
)
from chrys.app.tui.terminal.panel import ShellPanel
from chrys.app.tui.terminal.widget import Terminal
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import BaseToolCard, ToolGroup
from chrys.app.tui.widgets.chrome.file_index import ProjectPathIndex
from chrys.app.tui.widgets.chrome.file_scanner import ProjectPathScanResult, ProjectPathSuggestion
from chrys.app.tui.widgets.chrome.status_bar import StatusBar
from chrys.app.tui.widgets.diff_view import DiffView
from chrys.app.tui.widgets.diff_view.code import CodeColumn
from chrys.app.tui.widgets.diff_view.gutter import GutterColumn
from chrys.app.tui.widgets.diff_view.unified import UnifiedDiffLines
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.app.tui.widgets.sidebar.buddy import BuddyPanel
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.tool_kinds import KIND_SHELL
from chrys.orchestration.engine.execution import ExecutionSnapshot
from chrys.service.state.store import JsonFileStateStore

_MEASUREMENT_NOISE_BYTES = 64 * 1024
_DEAD_CYCLIC_FRACTION_LIMIT = 0.25
_LATENCY_SAMPLE_COUNT = 4
_FROZEN_COLLECT_RATIO_LIMIT = 0.25
# The absorb and full-reclaim limits count bare unfrozen full collections
# (``latency_in_unfrozen_collects``). At --turns 12, with Textual's caches left
# installed, an absorb measured about 0.2 of them and a full reclaim about 2.2. The
# cyclic-cache fallback, whose cache renewal reflows the whole screen, measured about
# 2.2 and 3.9, so it fails both limits.
_ABSORB_TO_UNFROZEN_RATIO_LIMIT = 1.25
_FULL_TO_UNFROZEN_RATIO_LIMIT = 3.0
_POPULATED_CACHE_FULL_RATIO_LIMIT = 2.0
_ABSORB_TRANSCRIPT_SCALING_LIMIT = 3.0
_ACTION_WAIT_TIMEOUT_SECONDS = 5.0
_ACTION_POLL_SECONDS = 0.01
_POST_ACTION_REFRESH_HOPS = 2
_CALIBRATION_SCROLL_SETTLE_SECONDS = 0.35
_COLLAPSE_PROBE_CALL_ID = "gc-collapse-probe"
# At --turns 12 the reclaimed heap rose about 110 KB and 50 KB over the first two
# collapse cycles and about 2 KB per cycle after them. Keeping the removed tool
# subtree would add its whole peak, about 650 KB, every cycle.
_COLLAPSE_CYCLES = 6
_COLLAPSE_WARMUP_CYCLES = 2


@dataclass(frozen=True, slots=True)
class IntervalResult:
    """One matched retained-byte interval."""

    absorbs: int
    retained_bytes: int
    collected_objects: int
    frozen_collect_ms: float
    full_reclaim_ms: float
    absorb_ms: tuple[float, ...]
    absorb_layout_ms: tuple[float, ...]
    absorb_layout_passes: tuple[int, ...]
    full_reclaim_layout_ms: float
    full_reclaim_layout_passes: int
    frozen_objects_added_by_absorbs: int
    dead_cyclic_fraction_of_absorb_additions: float | None
    freeze_count_before_full: int
    freeze_count_after_full: int
    top_retained_allocations: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True, slots=True)
class CollapseCycle:
    """One frozen completed-tool removal: its retained peak and the full reclaim after it."""

    retained_peak_bytes: int
    bytes_before_collapse: int
    bytes_after_reclaim: int
    descendant_alive_while_frozen: bool
    descendant_reclaimed: bool
    removal_reason_recorded: bool
    reclaim_ms: float
    reclaim_layout_ms: float
    reclaim_layout_passes: int
    passed: bool


@dataclass(frozen=True, slots=True)
class CollapseResult:
    """Repeated collapse cycles and how the reclaimed heap grows across them."""

    cycles: tuple[CollapseCycle, ...]
    warmup_cycles: int
    steady_growth_bytes_per_cycle: float
    growth_limit_bytes_per_cycle: int
    gate_passed: bool


@dataclass(frozen=True, slots=True)
class UnfrozenLatencyBaseline:
    """Matched unfrozen scans with and without participant preparation."""

    collect_ms: tuple[float, ...]
    prepared_action_ms: float
    prepared_layout_ms: float
    prepared_layout_passes: int


@dataclass(frozen=True, slots=True)
class _SettledActionMeasurement:
    """Coordinator metrics plus every deferred full-layout pass they scheduled."""

    metrics: GcFreezeActionMetrics
    duration_ms: float
    layout_ms: float
    layout_passes: int


@dataclass(frozen=True, slots=True)
class _LayoutMeasurementTarget:
    """Select layout passes occurring after one coordinator action commits."""

    previous_metrics: object
    record_all: bool = False


@dataclass(frozen=True, slots=True)
class LatencyResult:
    """Ratio-based latency gates that remain comparable across CI hosts.

    The ``*_prepared_ratio`` fields divide by the unfrozen scan run through the participant
    hooks (and any layout they schedule) instead; they are reported for comparison only.
    """

    unfrozen_full_collect_ms: tuple[float, ...]
    unfrozen_full_collect_reference_ms: float
    unfrozen_full_collect_median_ms: float
    unfrozen_prepared_action_ms: float
    unfrozen_prepared_layout_ms: float
    unfrozen_prepared_layout_passes: int
    frozen_collect_ratio: float
    frozen_collect_ratio_limit: float
    absorb_to_unfrozen_ratio: float
    absorb_to_unfrozen_ratio_limit: float
    full_to_unfrozen_ratio: float
    full_to_unfrozen_ratio_limit: float
    absorb_to_unfrozen_prepared_ratio: float
    full_to_unfrozen_prepared_ratio: float
    populated_cache_full_ratio: float
    populated_cache_full_ratio_limit: float
    small_transcript_turns: int
    small_transcript_absorb_ms: tuple[float, ...]
    large_transcript_absorb_ms: tuple[float, ...]
    max_gated_absorb_ms: float
    large_post_absorb_frozen_collect_ms: tuple[float, ...]
    representative_full_reclaim_ms: float
    transcript_scaling_ratio: float
    transcript_scaling_ratio_limit: float
    gate_passed: bool


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """Serializable calibration result and gate inputs."""

    turns: int
    theme_invalidation: bool
    viewport_invalidation: bool
    markdown_widgets: int
    unified_diff_widgets: int
    code_column_widgets: int
    annotation_column_widgets: int
    file_suggestion_count: int
    main_screen_participant_count: int
    watchdog_timer_active: bool
    buddy_timer_active: bool
    status_timer_active: bool
    title_task_active: bool
    mru_task_observed: bool
    whole_app_gate_passed: bool
    collapse: CollapseResult
    latency: LatencyResult
    baseline: IntervalResult
    intervals: tuple[IntervalResult, ...]
    d_base_upper_bytes: int
    d_absorb_upper_bytes: int
    hard_absorb_limit: int
    soft_absorb_limit: int
    projected_retained_bytes: int
    frozen_dead_budget_bytes: int
    safety_factor: int
    byte_gate_passed: bool
    aggregate_dead_cyclic_fraction: float
    dead_cyclic_fraction_limit: float
    object_gate_passed: bool
    collapse_gate_passed: bool
    latency_gate_passed: bool
    gate_passed: bool
    absorb_p95_ms: float
    full_reclaim_without_file_cache_ms: float
    full_reclaim_with_file_cache_max_ms: float


@dataclass(slots=True)
class _BackgroundState:
    """Proof that real whole-App background owners are live during calibration."""

    mru_task_observed: bool = False


class _CalibrationEngine:
    """Minimal backend boundary for a mounted real ChrysApp without model I/O."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session_id = "gc-freeze-calibration"
        self.session_generation = 0
        self.is_turn_active = False

    def execution(self) -> ExecutionSnapshot:
        return ExecutionSnapshot("idle")

    def execution_busy(self) -> bool:
        return False

    async def shutdown(self) -> None:
        return


class _EmptyRegistry:
    """Keep the calibration App mounted without starting a real agent backend."""

    def list_profiles(self) -> list[object]:
        return []

    def load_all(self) -> None:
        return

    def get(self, _name: str) -> None:
        return None


class _CalibrationMainScreen(MainScreen):
    """Real MainScreen with calibration-only accounting for deferred layouts."""

    CSS_PATH = Path(__file__).resolve().parents[1] / "src/chrys/app/tui/screens/main/screen.tcss"
    _layout_measurement_target: _LayoutMeasurementTarget | None = None
    _measured_layout_ms = 0.0
    _measured_layout_passes = 0

    def begin_action_layout_measurement(self, previous_metrics: object) -> None:
        """Record layouts only after *previous_metrics* is superseded."""
        if self._layout_measurement_target is not None:
            raise RuntimeError("nested GC layout measurement")
        self._layout_measurement_target = _LayoutMeasurementTarget(previous_metrics)
        self._measured_layout_ms = 0.0
        self._measured_layout_passes = 0

    def begin_unfrozen_layout_measurement(self) -> None:
        """Record every layout scheduled by one synchronous baseline action."""
        if self._layout_measurement_target is not None:
            raise RuntimeError("nested GC layout measurement")
        self._layout_measurement_target = _LayoutMeasurementTarget(object(), record_all=True)
        self._measured_layout_ms = 0.0
        self._measured_layout_passes = 0

    def finish_layout_measurement(self) -> tuple[float, int]:
        """Stop and return accumulated layout CPU time and pass count."""
        result = (self._measured_layout_ms, self._measured_layout_passes)
        self._layout_measurement_target = None
        self._measured_layout_ms = 0.0
        self._measured_layout_passes = 0
        return result

    def _refresh_layout(self, size: Size | None = None, scroll: bool = False) -> None:
        started_at = time.perf_counter()
        super()._refresh_layout(size, scroll)
        target = self._layout_measurement_target
        if target is None:
            return
        if not target.record_all and self.app._gc_freeze.last_action_metrics is target.previous_metrics:
            return
        self._measured_layout_ms += (time.perf_counter() - started_at) * 1_000
        self._measured_layout_passes += 1


class _CalibrationApp(ChrysApp):
    """ChrysApp that composes the instrumented real MainScreen graph."""

    CSS_PATH = Path(__file__).resolve().parents[1] / "src/chrys/app/tui/chrys.tcss"
    _calibration_action_blocked = False

    def set_calibration_action_blocked(self, blocked: bool) -> None:
        """Hold coordinator requests while one measured generation is prepared."""
        self._calibration_action_blocked = blocked

    def freeze_block_reason(self) -> GcFreezeBlockReason | None:
        if self._calibration_action_blocked:
            return GcFreezeBlockReason.PARTICIPANT
        return super().freeze_block_reason()

    def _build_main_screen(self) -> MainScreen:
        return _CalibrationMainScreen(
            self._bus,
            state_store=self._state_store,
            agent_registry=self._agent_registry,
            model_registry=self._model_registry,
            active_model_profile_id=self._settings.model_profile,
            workspace_mru_max_entries=self._settings.workspace_mru_max_entries,
            engine_provider=lambda: self._engine,
        )


class _CalibrationTitleUpdater(SessionTitleUpdater):
    """Keep the real title-task lifecycle active without making an LLM call."""

    def __init__(self, bus: EventBus, state_store: JsonFileStateStore, engine: _CalibrationEngine) -> None:
        super().__init__(bus, state_store, engine_getter=lambda: engine)  # type: ignore[arg-type]
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def _refresh_generated_title(self, _engine: object, _session_id: str) -> None:
        self.started.set()
        await self.release.wait()


def _build_app(
    state_root: Path,
    *,
    enabled: bool,
    measure_freeze_counts: bool = False,
) -> tuple[_CalibrationApp, _CalibrationTitleUpdater]:
    bus = EventBus()
    settings = Settings(theme="chrys", session_title_auto=True)
    state_store = JsonFileStateStore(state_root)
    engine = _CalibrationEngine(settings)
    updater = _CalibrationTitleUpdater(bus, state_store, engine)
    app = _CalibrationApp(
        bus,
        engine,  # type: ignore[arg-type]
        settings=settings,
        state_store=state_store,
        agent_registry=_EmptyRegistry(),  # type: ignore[arg-type]
        session_title_updater=updater,
        gc_freeze_enabled=enabled,
        gc_freeze_measure_counts=measure_freeze_counts,
    )
    return app, updater


def _build_file_cache(count: int) -> tuple[ProjectPathScanResult, ProjectPathIndex]:
    paths = [
        ProjectPathSuggestion(f"src/package_{index // 100:04d}/module_{index:06d}.py", "file") for index in range(count)
    ]
    scan = ProjectPathScanResult.from_suggestions(root="/calibration", paths=paths)
    return scan, ProjectPathIndex.build(scan)


def _install_file_cache(screen: MainScreen, count: int) -> ProjectPathIndex:
    scan, index = _build_file_cache(count)
    root = screen._suggestions._current_file_root()
    screen._suggestions._install_file_cache(root, scan, index)
    return index


def _build_split_diff() -> DiffView:
    """Build a real split diff so entry/strip LRUs both enter calibration."""
    before_lines = [f"VALUE_{line:03d} = {line}" for line in range(120)]
    after_lines = [
        f"VALUE_{line:03d} = {line + 10_000}" if line % 7 == 0 else value for line, value in enumerate(before_lines)
    ]
    diff = DiffView(
        "src/calibration_before.py",
        "src/calibration_after.py",
        "\n".join(before_lines),
        "\n".join(after_lines),
    )
    diff.split = True
    diff.max_display_lines = 40
    return diff


def _render_cache_surfaces(
    panel: ChatPanel,
) -> tuple[list[VirtualizedMarkdown], list[UnifiedDiffLines], list[CodeColumn], list[GutterColumn]]:
    return (
        list(panel.query(VirtualizedMarkdown)),
        list(panel.query(UnifiedDiffLines)),
        list(panel.query(CodeColumn)),
        list(panel.query(GutterColumn)),
    )


def _populate_render_caches(
    markdown: list[VirtualizedMarkdown],
    unified: list[UnifiedDiffLines],
    code: list[CodeColumn],
    gutters: list[GutterColumn],
) -> None:
    for widget in markdown:
        widget._frame_visible_height = max(1, widget._total_lines)
        for line in range(widget._total_lines):
            widget.render_line(line)
    for widget in (*unified, *code, *gutters):
        widget.render_lines(Region(0, 0, max(1, widget.size.width), min(40, len(widget.rows))))


async def _wait_for_render_surfaces(*, pilot, panel: ChatPanel, turns: int) -> None:
    """Wait for the complete workload, tolerating a bounded timed-out worker tail."""
    expected = (turns * 3, turns * 2, 2, 4)
    for _ in range(200):
        counts = tuple(len(surfaces) for surfaces in _render_cache_surfaces(panel))
        if all(actual >= minimum for actual, minimum in zip(counts, expected, strict=True)):
            return
        await pilot.pause(0.05)
    minimum = (turns * 3, minimum_deferred_diff_surfaces(turns), 2, 4)
    if all(actual >= required for actual, required in zip(counts, minimum, strict=True)):
        return
    msg = f"deferred render surfaces did not settle: expected at least {minimum}, got {counts}"
    raise RuntimeError(msg)


async def _wait_for_initial_freeze(*, app: ChrysApp, pilot) -> None:
    deadline = asyncio.get_running_loop().time() + _ACTION_WAIT_TIMEOUT_SECONDS
    while asyncio.get_running_loop().time() < deadline:
        if app._gc_freeze.frozen:
            return
        await asyncio.sleep(_ACTION_POLL_SECONDS)
    raise RuntimeError("real ChrysApp coordinator did not complete its initial freeze")


def _calibration_screen(pilot) -> _CalibrationMainScreen:
    screen = pilot.app.screen
    if not isinstance(screen, _CalibrationMainScreen):
        raise RuntimeError("GC calibration requires the instrumented real MainScreen")
    return screen


async def _wait_for_post_action_refreshes(*, app: ChrysApp, timeout_seconds: float) -> None:
    """Cross enough refresh boundaries to include action-triggered full layouts."""
    settled = asyncio.Event()
    scheduling_failed = False
    remaining_hops = _POST_ACTION_REFRESH_HOPS

    def after_refresh() -> None:
        nonlocal remaining_hops, scheduling_failed
        remaining_hops -= 1
        if remaining_hops == 0:
            settled.set()
            return
        if not app.call_after_refresh(after_refresh):
            scheduling_failed = True
            settled.set()

    if not app.call_after_refresh(after_refresh):
        raise RuntimeError("real ChrysApp rejected the post-GC refresh measurement barrier")
    await asyncio.wait_for(settled.wait(), timeout=timeout_seconds)
    if scheduling_failed:
        raise RuntimeError("real ChrysApp rejected a post-GC refresh measurement hop")


def _begin_action_measurement(*, coordinator: GcFreezeCoordinator, pilot) -> object:
    previous_metrics = coordinator.last_action_metrics
    _calibration_screen(pilot).begin_action_layout_measurement(previous_metrics)
    return previous_metrics


async def _wait_for_action(
    *,
    coordinator: GcFreezeCoordinator,
    pilot,
    previous_metrics: object,
    expected_action: str,
    timeout_seconds: float = _ACTION_WAIT_TIMEOUT_SECONDS,
) -> _SettledActionMeasurement:
    screen = _calibration_screen(pilot)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_seconds
    try:
        while loop.time() < deadline:
            metrics = coordinator.last_action_metrics
            if metrics is not previous_metrics and metrics is not None and metrics.action == expected_action:
                app = pilot.app
                if not isinstance(app, _CalibrationApp):
                    raise RuntimeError("GC calibration requires the calibration App")
                # Close the dispatch gate immediately when the measured action
                # commits, before post-action layout barriers yield to the
                # one-second watchdog. This keeps a soft-cadence idle reclaim
                # from contaminating the same interval.
                app.set_calibration_action_blocked(True)
                remaining = max(_ACTION_POLL_SECONDS, deadline - loop.time())
                await _wait_for_post_action_refreshes(app=app, timeout_seconds=remaining)
                layout_ms, layout_passes = screen.finish_layout_measurement()
                return _SettledActionMeasurement(
                    metrics=metrics,
                    duration_ms=metrics.duration_ms + layout_ms,
                    layout_ms=layout_ms,
                    layout_passes=layout_passes,
                )
            await asyncio.sleep(_ACTION_POLL_SECONDS)
        metrics = coordinator.last_action_metrics
        actual = None if metrics is previous_metrics or metrics is None else metrics.action
        raise RuntimeError(
            f"real ChrysApp coordinator did not complete expected {expected_action!r} action "
            f"within {timeout_seconds:.1f}s (actual={actual!r})"
        )
    finally:
        if screen._layout_measurement_target is not None:
            screen.finish_layout_measurement()


async def _request_absorb(*, coordinator: GcFreezeCoordinator, pilot) -> _SettledActionMeasurement:
    app = pilot.app
    if not isinstance(app, _CalibrationApp):
        raise RuntimeError("GC calibration requires the calibration App")
    previous = _begin_action_measurement(coordinator=coordinator, pilot=pilot)
    app.set_calibration_action_blocked(False)
    try:
        coordinator.request_absorb(
            reason=GcAbsorbReason.STABLE_CONTENT_MOUNTED,
            terminal_boundary=True,
        )
        return await _wait_for_action(
            coordinator=coordinator,
            pilot=pilot,
            previous_metrics=previous,
            expected_action="absorb",
        )
    finally:
        app.set_calibration_action_blocked(True)


async def _request_full_reclaim(*, coordinator: GcFreezeCoordinator, pilot) -> _SettledActionMeasurement:
    app = pilot.app
    if not isinstance(app, _CalibrationApp):
        raise RuntimeError("GC calibration requires the calibration App")
    previous = _begin_action_measurement(coordinator=coordinator, pilot=pilot)
    app.set_calibration_action_blocked(False)
    try:
        coordinator.request_reclaim(reason=GcReclaimReason.PERIODIC, prompt=True)
        return await _wait_for_action(
            coordinator=coordinator,
            pilot=pilot,
            previous_metrics=previous,
            expected_action="full",
        )
    finally:
        app.set_calibration_action_blocked(True)


async def _populate_whole_app(
    *,
    app: ChrysApp,
    pilot,
    turns: int,
    include_collapse_probe: bool,
) -> tuple[MainScreen, ChatPanel, Terminal]:
    screen = app._main_screen
    if screen is None:
        raise RuntimeError("real ChrysApp did not mount MainScreen")
    panel = screen.query_one(ChatPanel)
    terminal = screen.query_one(ShellPanel).query_one(Terminal)
    await _populate_realistic_session(panel, turns=turns, collapsed=False)
    if include_collapse_probe:
        await _add_tool_group_collapse_probe(panel)
    await panel.mount(_build_split_diff())
    await _wait_for_render_surfaces(pilot=pilot, panel=panel, turns=turns)
    for _ in range(3):
        await pilot.pause()
    return screen, panel, terminal


async def _add_tool_group_collapse_probe(panel: ChatPanel) -> None:
    """Mount one completed, prunable tool group before the freeze baseline."""
    # The realistic per-turn group intentionally includes a SubAgentToolCall,
    # whose unsnapshotted presentation state makes that whole subtree
    # ineligible for pruning. Keep the calibration's frozen-removal probe
    # representative by adding one independent, fully restorable tool group.
    await panel.add_tool_start(
        _COLLAPSE_PROBE_CALL_ID,
        "shell",
        KIND_SHELL,
        args={"command": "uv run pytest tests/gc_collapse_probe.py -q"},
    )
    panel._tool_groups_by_call_id[_COLLAPSE_PROBE_CALL_ID].collapsed = False
    await panel.add_tool_result(
        _COLLAPSE_PROBE_CALL_ID,
        "shell",
        "\n".join(f"gc collapse probe output {line:03d}" for line in range(160)),
        1_250,
    )
    panel._end_tool_group()


def _schedule_mru_touch(background: _BackgroundState) -> None:
    workspace_mru.schedule_workspace_mru_touches(
        [str(Path.cwd())],
        max_entries=20,
        session_id="gc-freeze-calibration",
        used_at=datetime.now(UTC),
    )
    background.mru_task_observed = background.mru_task_observed or bool(workspace_mru._BACKGROUND_TASKS)


def _terminal_generation_output(*, label: str, generation: int, line_count: int) -> str:
    """Build styled shell output with in-place progress replacement."""
    chunks = [f"\x1b[36m$ {label} --generation {generation}\x1b[0m\r\n"]
    for line in range(line_count):
        color = 31 + line % 6
        chunks.append(f"\x1b[{color}m{label} generation {generation}: terminal line {line:03d}\x1b[0m\r\n")
        if line % 20 == 0:
            chunks.append(f"\r\x1b[2Kprogress {line + 1}/{line_count}")
    chunks.append(f"\r\x1b[2K\x1b[32mcomplete {line_count}/{line_count}\x1b[0m\r\n")
    return "".join(chunks)


async def _exercise_fixed_latency_generation(
    *,
    pilot,
    panel: ChatPanel,
    terminal: Terminal,
    file_index: ProjectPathIndex,
    generation: int,
    background: _BackgroundState,
) -> None:
    markdown, unified, code, gutters = _render_cache_surfaces(panel)
    fixed_markdown = markdown[:3]
    fixed_unified = unified[:2]
    fixed_code = code[:2]
    fixed_gutters = gutters[:4]
    fixed_surfaces = [*fixed_markdown, *fixed_unified, *fixed_code, *fixed_gutters]
    for surface in fixed_surfaces:
        surface.notify_style_update()
    _populate_render_caches(fixed_markdown, fixed_unified, fixed_code, fixed_gutters)
    if fixed_markdown:
        selected = fixed_markdown[generation % len(fixed_markdown)]
        selection = Selection(Offset(0, 0), Offset(12, 1))
        pilot.app.screen.selections = {selected: selection}
        selected.selection_updated(selection)
        selected.render_lines(Region(0, 0, max(1, selected.size.width), min(3, selected._total_lines)))
        pilot.app.screen.selections = {}
        selected.selection_updated(None)
    await terminal.write(_terminal_generation_output(label="latency-probe", generation=generation, line_count=20))
    for line in range(min(terminal.height, 12)):
        terminal.render_line(line)
    file_index.query(f"module_{generation:03d}", limit=30)
    _schedule_mru_touch(background)
    await pilot.pause()


async def _measure_absorb_latency_samples(
    *,
    coordinator: GcFreezeCoordinator,
    pilot,
    panel: ChatPanel,
    terminal: Terminal,
    file_index: ProjectPathIndex,
    background: _BackgroundState,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    samples: list[float] = []
    frozen_collect_samples: list[float] = []
    for generation in range(_LATENCY_SAMPLE_COUNT):
        await _exercise_fixed_latency_generation(
            pilot=pilot,
            panel=panel,
            terminal=terminal,
            file_index=file_index,
            generation=generation,
            background=background,
        )
        measurement = await _request_absorb(coordinator=coordinator, pilot=pilot)
        samples.append(measurement.duration_ms)
        started_at = time.perf_counter()
        gc.collect()
        frozen_collect_samples.append((time.perf_counter() - started_at) * 1_000)
    return tuple(samples), tuple(frozen_collect_samples)


async def _exercise_generation(
    *,
    pilot,
    panel: ChatPanel,
    terminal: Terminal,
    file_index: ProjectPathIndex,
    generation: int,
    theme_invalidation: bool,
    viewport_invalidation: bool,
    background: _BackgroundState,
) -> None:
    app = pilot.app
    if not isinstance(app, _CalibrationApp):
        raise RuntimeError("GC generation workload requires the calibration App")
    app.set_calibration_action_blocked(True)
    try:
        markdown, unified, code, gutters = _render_cache_surfaces(panel)
        _populate_render_caches(markdown, unified, code, gutters)

        if markdown:
            selected = markdown[generation % len(markdown)]
            selection = Selection(Offset(0, 0), Offset(12, 1))
            pilot.app.screen.selections = {selected: selection}
            selected.selection_updated(selection)
            selected.render_lines(Region(0, 0, max(1, selected.size.width), min(3, selected._total_lines)))
            pilot.app.screen.selections = {}
            selected.selection_updated(None)

        for surface in [*markdown, *unified, *code, *gutters]:
            surface.notify_style_update()

        panel.scroll_to(y=panel.max_scroll_y if generation % 2 == 0 else 0, animate=False, immediate=True)
        if viewport_invalidation:
            width = 112 if generation % 2 == 0 else 120
            await pilot.resize_terminal(width, 36)
        if theme_invalidation:
            pilot.app.theme = "textual-light" if generation % 2 == 0 else "textual-dark"

        await terminal.write(
            _terminal_generation_output(label="background-shell", generation=generation, line_count=80)
        )
        for line in range(min(terminal.height, 24)):
            terminal.render_line(line)

        file_index.query(f"module_{generation:03d}", limit=30)
        _schedule_mru_touch(background)
        # Let the real ChatPanel scroll-settle timer release its GC pause while
        # the calibration gate prevents viewport/theme requests from executing
        # before the explicit measured action can coalesce them.
        await pilot.pause(_CALIBRATION_SCROLL_SETTLE_SECONDS)
    finally:
        # Explicit measurement helpers briefly open this gate for exactly one
        # requested action and close it again as soon as metrics commit.
        app.set_calibration_action_blocked(True)


async def _measure_interval(
    *,
    coordinator: GcFreezeCoordinator,
    pilot,
    panel: ChatPanel,
    terminal: Terminal,
    file_index: ProjectPathIndex,
    absorbs: int,
    exercise_final_generation: bool,
    theme_invalidation: bool,
    viewport_invalidation: bool,
    background: _BackgroundState,
) -> IntervalResult:
    absorb_ms: list[float] = []
    absorb_layout_ms: list[float] = []
    absorb_layout_passes: list[int] = []
    frozen_objects_added_by_absorbs = 0
    for generation in range(absorbs):
        await _exercise_generation(
            pilot=pilot,
            panel=panel,
            terminal=terminal,
            file_index=file_index,
            generation=generation,
            theme_invalidation=theme_invalidation,
            viewport_invalidation=viewport_invalidation,
            background=background,
        )
        action = await _request_absorb(coordinator=coordinator, pilot=pilot)
        action_metrics = action.metrics
        absorb_ms.append(action.duration_ms)
        absorb_layout_ms.append(action.layout_ms)
        absorb_layout_passes.append(action.layout_passes)
        if action_metrics.freeze_count_before is None or action_metrics.freeze_count_after is None:
            raise RuntimeError("calibration coordinator did not enable freeze-count measurements")
        frozen_objects_added_by_absorbs += max(
            0,
            action_metrics.freeze_count_after - action_metrics.freeze_count_before,
        )
        await pilot.pause()

    if exercise_final_generation:
        await _exercise_generation(
            pilot=pilot,
            panel=panel,
            terminal=terminal,
            file_index=file_index,
            generation=absorbs,
            theme_invalidation=theme_invalidation,
            viewport_invalidation=viewport_invalidation,
            background=background,
        )
    started_at = time.perf_counter()
    gc.collect()
    frozen_collect_ms = (time.perf_counter() - started_at) * 1_000
    retained_before = tracemalloc.get_traced_memory()[0]
    snapshot_before = tracemalloc.take_snapshot()
    freeze_count_before = gc.get_freeze_count()

    action = await _request_full_reclaim(coordinator=coordinator, pilot=pilot)
    action_metrics = action.metrics
    full_reclaim_ms = action.duration_ms
    retained_after = tracemalloc.get_traced_memory()[0]
    snapshot_after = tracemalloc.take_snapshot()
    await pilot.pause()

    return IntervalResult(
        absorbs=absorbs,
        retained_bytes=max(0, retained_before - retained_after),
        collected_objects=action_metrics.collected,
        frozen_collect_ms=frozen_collect_ms,
        full_reclaim_ms=full_reclaim_ms,
        absorb_ms=tuple(absorb_ms),
        absorb_layout_ms=tuple(absorb_layout_ms),
        absorb_layout_passes=tuple(absorb_layout_passes),
        full_reclaim_layout_ms=action.layout_ms,
        full_reclaim_layout_passes=action.layout_passes,
        frozen_objects_added_by_absorbs=frozen_objects_added_by_absorbs,
        dead_cyclic_fraction_of_absorb_additions=None,
        freeze_count_before_full=freeze_count_before,
        freeze_count_after_full=gc.get_freeze_count(),
        top_retained_allocations=tuple(
            (
                f"{Path(stat.traceback[0].filename).name}:{stat.traceback[0].lineno}",
                stat.size_diff,
                stat.count_diff,
            )
            for stat in snapshot_before.compare_to(snapshot_after, "lineno")
            if stat.size_diff > 0
        )[:12],
    )


def _upper_bounds(
    baseline: IntervalResult,
    intervals: tuple[IntervalResult, ...],
) -> tuple[int, int]:
    zero = next(interval for interval in intervals if interval.absorbs == 0)
    d_base = max(0, zero.retained_bytes - baseline.retained_bytes) + _MEASUREMENT_NOISE_BYTES
    slopes = [
        max(0, interval.retained_bytes - baseline.retained_bytes - d_base) / interval.absorbs
        for interval in intervals
        if interval.absorbs
    ]
    d_absorb = int(max(slopes, default=0)) + _MEASUREMENT_NOISE_BYTES
    return d_base, d_absorb


def _weak_tool_descendant(group: ToolGroup) -> weakref.ReferenceType[BaseToolCard]:
    # BaseToolCard covers every tool-card renderer (ToolCall and specialized
    # siblings like ExecuteToolCall) — the probe only needs a weakly
    # referenced descendant, not a specific renderer class.
    descendant = next(tool for tool in group._tools.values() if isinstance(tool, BaseToolCard))
    return weakref.ref(descendant)


def _weakref_alive(reference: weakref.ReferenceType[object]) -> bool:
    return reference() is not None


async def _measure_tool_group_collapse(
    *,
    app: ChrysApp,
    pilot,
    panel: ChatPanel,
) -> CollapseCycle:
    group = panel._tool_groups_by_call_id.get(_COLLAPSE_PROBE_CALL_ID)
    if (
        group is None
        or not group._content_mounted
        or not group._tools
        or not group._completed_widget_pruning_supported()
    ):
        raise RuntimeError("whole-App calibration collapse probe is not populated and prunable")
    descendant_ref = _weak_tool_descendant(group)
    bytes_before_collapse = tracemalloc.get_traced_memory()[0]
    app._gc_freeze.note_input(occurred_at=textual_time.get_time())
    group.collapsed = True
    for _ in range(100):
        await pilot.pause(0.02)
        if not group._content_mounted and app._gc_freeze._idle_reclaim_pending:
            break
    if group._content_mounted or not app._gc_freeze._idle_reclaim_pending:
        raise RuntimeError("completed ToolGroup did not reach the frozen-removal reclaim boundary")
    gc.collect()
    descendant_alive_while_frozen = _weakref_alive(descendant_ref)
    peak_bytes = tracemalloc.get_traced_memory()[0]
    previous_metrics = _begin_action_measurement(coordinator=app._gc_freeze, pilot=pilot)
    # This probe deliberately relies on the production idle-reclaim path.
    # Explicit measurement helpers leave the calibration gate closed, so open
    # it only for this watchdog-driven action and close it again as soon as the
    # action commits (also handled inside ``_wait_for_action``).
    app.set_calibration_action_blocked(False)
    try:
        action = await _wait_for_action(
            coordinator=app._gc_freeze,
            pilot=pilot,
            previous_metrics=previous_metrics,
            expected_action="full",
            timeout_seconds=8.0,
        )
    finally:
        app.set_calibration_action_blocked(True)
    action_metrics = action.metrics
    for _ in range(3):
        await pilot.pause()
    bytes_after_reclaim = tracemalloc.get_traced_memory()[0]
    descendant_reclaimed = not _weakref_alive(descendant_ref)
    retained_peak_bytes = max(0, peak_bytes - bytes_after_reclaim)
    removal_reason_recorded = GcReclaimReason.STABLE_CONTENT_REMOVED.value in action_metrics.idle_reclaim_reasons
    result = CollapseCycle(
        retained_peak_bytes=retained_peak_bytes,
        bytes_before_collapse=bytes_before_collapse,
        bytes_after_reclaim=bytes_after_reclaim,
        descendant_alive_while_frozen=descendant_alive_while_frozen,
        descendant_reclaimed=descendant_reclaimed,
        removal_reason_recorded=removal_reason_recorded,
        reclaim_ms=action.duration_ms,
        reclaim_layout_ms=action.layout_ms,
        reclaim_layout_passes=action.layout_passes,
        passed=(
            descendant_alive_while_frozen
            and descendant_reclaimed
            and retained_peak_bytes > 0
            and removal_reason_recorded
        ),
    )
    group.collapsed = False
    for _ in range(100):
        await pilot.pause(0.02)
        if group._content_mounted and group._tools:
            break
    if not group._content_mounted or not group._tools:
        raise RuntimeError("collapsed ToolGroup did not rebuild after reclaim calibration")
    return result


async def _measure_repeated_tool_group_collapse(
    *,
    app: ChrysApp,
    pilot,
    panel: ChatPanel,
    turns: int,
    cycles: int = _COLLAPSE_CYCLES,
    warmup_cycles: int = _COLLAPSE_WARMUP_CYCLES,
) -> CollapseResult:
    """Collapse the probe repeatedly and gate on how the reclaimed heap grows.

    One cycle's heap before the collapse and after its reclaim differ by whether the
    probe's content was render-cached when it collapsed, which swings the difference
    by that content's size between runs of the same code. Every cycle ends in the
    same state, collapsed and fully reclaimed, so retained work shows as steady
    growth of that settled heap once the warm-up cycles have filled one-off caches.
    """
    measured: list[CollapseCycle] = []
    for _ in range(cycles):
        measured.append(await _measure_tool_group_collapse(app=app, pilot=pilot, panel=panel))
        # Freeze the rebuilt content, so the next collapse removes frozen content
        # again and reaches the idle full reclaim.
        await _wait_for_render_surfaces(pilot=pilot, panel=panel, turns=turns)
        await _request_full_reclaim(coordinator=app._gc_freeze, pilot=pilot)
    growth = steady_growth_per_cycle(
        [cycle.bytes_after_reclaim for cycle in measured],
        warmup_cycles=warmup_cycles,
    )
    return CollapseResult(
        cycles=tuple(measured),
        warmup_cycles=warmup_cycles,
        steady_growth_bytes_per_cycle=growth,
        growth_limit_bytes_per_cycle=_MEASUREMENT_NOISE_BYTES,
        gate_passed=all(cycle.passed for cycle in measured) and growth <= _MEASUREMENT_NOISE_BYTES,
    )


async def _measure_unfrozen_full_collect(
    *,
    state_root: Path,
    turns: int,
    suggestion_count: int,
    workload_generations: int,
    background: _BackgroundState,
) -> UnfrozenLatencyBaseline:
    app, updater = _build_app(state_root, enabled=False)
    async with app.run_test(size=(120, 36)) as pilot:
        screen, panel, terminal = await _populate_whole_app(
            app=app,
            pilot=pilot,
            turns=turns,
            include_collapse_probe=False,
        )
        file_index = _install_file_cache(screen, suggestion_count)
        updater.on_turn_finished()
        await asyncio.wait_for(updater.started.wait(), timeout=5)
        for generation in range(workload_generations):
            await _exercise_generation(
                pilot=pilot,
                panel=panel,
                terminal=terminal,
                file_index=file_index,
                generation=generation,
                theme_invalidation=False,
                viewport_invalidation=True,
                background=background,
            )
        samples: list[float] = []
        for _ in range(3):
            started_at = time.perf_counter()
            gc.collect()
            samples.append((time.perf_counter() - started_at) * 1_000)
        await _exercise_generation(
            pilot=pilot,
            panel=panel,
            terminal=terminal,
            file_index=file_index,
            generation=workload_generations,
            theme_invalidation=False,
            viewport_invalidation=True,
            background=background,
        )
        calibration_screen = _calibration_screen(pilot)
        calibration_screen.begin_unfrozen_layout_measurement()
        started_at = time.perf_counter()
        screen.prepare_for_gc_freeze()
        gc.collect()
        screen.after_gc_freeze()
        synchronous_action_ms = (time.perf_counter() - started_at) * 1_000
        await _wait_for_post_action_refreshes(app=app, timeout_seconds=_ACTION_WAIT_TIMEOUT_SECONDS)
        layout_ms, layout_passes = calibration_screen.finish_layout_measurement()
        return UnfrozenLatencyBaseline(
            collect_ms=tuple(samples),
            prepared_action_ms=synchronous_action_ms + layout_ms,
            prepared_layout_ms=layout_ms,
            prepared_layout_passes=layout_passes,
        )


async def _measure_small_transcript_absorbs(
    *,
    state_root: Path,
    turns: int,
    suggestion_count: int,
    background: _BackgroundState,
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    app, updater = _build_app(state_root, enabled=True)
    async with app.run_test(size=(120, 36)) as pilot:
        await _wait_for_initial_freeze(app=app, pilot=pilot)
        screen, panel, terminal = await _populate_whole_app(
            app=app,
            pilot=pilot,
            turns=turns,
            include_collapse_probe=False,
        )
        file_index = _install_file_cache(screen, suggestion_count)
        updater.on_turn_finished()
        await asyncio.wait_for(updater.started.wait(), timeout=5)
        await _request_full_reclaim(coordinator=app._gc_freeze, pilot=pilot)
        return await _measure_absorb_latency_samples(
            coordinator=app._gc_freeze,
            pilot=pilot,
            panel=panel,
            terminal=terminal,
            file_index=file_index,
            background=background,
        )


async def _measure_fresh_interval(
    *,
    state_root: Path,
    turns: int,
    suggestion_count: int,
    absorbs: int,
    exercise_final_generation: bool,
    theme_invalidation: bool,
    viewport_invalidation: bool,
    background: _BackgroundState,
) -> IntervalResult:
    """Measure one retained-byte point in a fresh equivalently seeded App."""
    with patch(
        "chrys.app.tui.support.workspace_mru.workspace_mru_path",
        lambda: state_root / "workspace_mru.json",
    ):
        workspace_mru.ensure_workspace_mru_index(
            [(str(Path.cwd()), datetime.now(UTC))],
            max_entries=20,
            root_key="gc-freeze-calibration",
        )
        app, updater = _build_app(
            state_root / "app-state",
            enabled=True,
            measure_freeze_counts=True,
        )
        async with app.run_test(size=(120, 36)) as pilot:
            await _wait_for_initial_freeze(app=app, pilot=pilot)
            screen, panel, terminal = await _populate_whole_app(
                app=app,
                pilot=pilot,
                turns=turns,
                include_collapse_probe=False,
            )
            updater.on_turn_finished()
            await asyncio.wait_for(updater.started.wait(), timeout=5)
            file_index = _install_file_cache(screen, suggestion_count)
            await _request_full_reclaim(coordinator=app._gc_freeze, pilot=pilot)
            result = await _measure_interval(
                coordinator=app._gc_freeze,
                pilot=pilot,
                panel=panel,
                terminal=terminal,
                file_index=file_index,
                absorbs=absorbs,
                exercise_final_generation=exercise_final_generation,
                theme_invalidation=theme_invalidation,
                viewport_invalidation=viewport_invalidation,
                background=background,
            )
        if workspace_mru._BACKGROUND_TASKS:
            await asyncio.gather(*tuple(workspace_mru._BACKGROUND_TASKS), return_exceptions=True)
        return result


async def calibrate(
    *,
    turns: int,
    absorb_points: tuple[int, ...],
    suggestion_count: int,
    theme_invalidation: bool,
    viewport_invalidation: bool,
) -> CalibrationReport:
    gc.collect()
    tracemalloc.start()
    try:
        with (
            TemporaryDirectory(prefix="chrys-gc-freeze-calibration-") as temp_dir_name,
            patch(
                "chrys.app.tui.support.workspace_mru.workspace_mru_path",
                lambda: Path(temp_dir_name) / "workspace_mru.json",
            ),
            patch("chrys.app.tui.widgets.sidebar.buddy.current_buddy", lambda: None),
        ):
            temp_dir = Path(temp_dir_name)
            workspace_mru.ensure_workspace_mru_index(
                [(str(Path.cwd()), datetime.now(UTC))],
                max_entries=20,
                root_key="gc-freeze-calibration",
            )
            background = _BackgroundState()
            unfrozen = await _measure_unfrozen_full_collect(
                state_root=temp_dir / "unfrozen-state",
                turns=turns,
                suggestion_count=suggestion_count,
                workload_generations=max(absorb_points) + 1,
                background=background,
            )
            small_turns = max(1, min(turns, max(2, turns // 4)))
            small_absorb_samples, _small_frozen_collects = await _measure_small_transcript_absorbs(
                state_root=temp_dir / "small-state",
                turns=small_turns,
                suggestion_count=suggestion_count,
                background=background,
            )

            app, updater = _build_app(temp_dir / "whole-app-state", enabled=True)
            async with app.run_test(size=(120, 36)) as pilot:
                await _wait_for_initial_freeze(app=app, pilot=pilot)
                screen, panel, terminal = await _populate_whole_app(
                    app=app,
                    pilot=pilot,
                    turns=turns,
                    include_collapse_probe=True,
                )
                markdown, unified, code, gutters = _render_cache_surfaces(panel)
                surface_counts = (len(markdown), len(unified), len(code), len(gutters))
                updater.on_turn_finished()
                await asyncio.wait_for(updater.started.wait(), timeout=5)
                participant_count = len(screen._gc_freeze_participants)
                watchdog_timer_active = app._gc_freeze_watchdog is not None
                buddy_timer_active = screen.query_one(BuddyPanel)._timer is not None
                status_timer_active = screen.query_one(StatusBar)._timer is not None
                title_task_active = updater._task is not None and not updater._task.done()

                _install_file_cache(screen, 0)
                await _request_full_reclaim(coordinator=app._gc_freeze, pilot=pilot)
                collapse = await _measure_repeated_tool_group_collapse(
                    app=app,
                    pilot=pilot,
                    panel=panel,
                    turns=turns,
                )

                file_index = _install_file_cache(screen, suggestion_count)
                large_absorb_samples, large_frozen_collect_samples = await _measure_absorb_latency_samples(
                    coordinator=app._gc_freeze,
                    pilot=pilot,
                    panel=panel,
                    terminal=terminal,
                    file_index=file_index,
                    background=background,
                )
                await _request_full_reclaim(coordinator=app._gc_freeze, pilot=pilot)
            if workspace_mru._BACKGROUND_TASKS:
                await asyncio.gather(*tuple(workspace_mru._BACKGROUND_TASKS), return_exceptions=True)

            baseline = await _measure_fresh_interval(
                state_root=temp_dir / "matched-baseline",
                turns=turns,
                suggestion_count=0,
                absorbs=0,
                exercise_final_generation=False,
                theme_invalidation=theme_invalidation,
                viewport_invalidation=viewport_invalidation,
                background=background,
            )
            intervals = tuple(
                [
                    await _measure_fresh_interval(
                        state_root=temp_dir / f"matched-{absorbs}",
                        turns=turns,
                        suggestion_count=suggestion_count,
                        absorbs=absorbs,
                        exercise_final_generation=True,
                        theme_invalidation=theme_invalidation,
                        viewport_invalidation=viewport_invalidation,
                        background=background,
                    )
                    for absorbs in absorb_points
                ]
            )
            zero_collected = next(interval.collected_objects for interval in intervals if interval.absorbs == 0)
            intervals = tuple(
                replace(
                    interval,
                    dead_cyclic_fraction_of_absorb_additions=dead_cyclic_fraction(
                        collected_objects=interval.collected_objects,
                        zero_collected=zero_collected,
                        frozen_objects_added_by_absorbs=interval.frozen_objects_added_by_absorbs,
                    ),
                )
                for interval in intervals
            )
            d_base, d_absorb = _upper_bounds(baseline, intervals)
            projected = d_base + d_absorb * _RECLAIM_HARD_ABSORBS
            positive_intervals = [interval for interval in intervals if interval.absorbs]
            aggregate_dead_cyclic_fraction = (
                calculate_aggregate_dead_cyclic_fraction(
                    (
                        interval.collected_objects,
                        zero_collected,
                        interval.frozen_objects_added_by_absorbs,
                    )
                    for interval in positive_intervals
                )
                or 0.0
            )
            byte_gate_passed = projected * 2 < _FROZEN_DEAD_BUDGET_BYTES
            object_gate_passed = aggregate_dead_cyclic_fraction < _DEAD_CYCLIC_FRACTION_LIMIT
            absorb_samples = [duration for interval in intervals for duration in interval.absorb_ms]

            unfrozen_reference = unfrozen.collect_ms[0]
            unfrozen_median = statistics.median(unfrozen.collect_ms)
            unfrozen_action_reference = unfrozen.prepared_action_ms
            max_gated_absorb_ms = max([*large_absorb_samples, *absorb_samples])
            frozen_collect_ratio = max(large_frozen_collect_samples) / unfrozen_reference
            absorb_to_unfrozen_ratio = latency_in_unfrozen_collects(
                max_gated_absorb_ms,
                unfrozen_collect_ms=unfrozen_reference,
            )
            full_with_file_ms = max(interval.full_reclaim_ms for interval in intervals)
            representative_full_ms = max(
                baseline.full_reclaim_ms,
                collapse.cycles[0].reclaim_ms,
                statistics.median(interval.full_reclaim_ms for interval in intervals),
            )
            full_to_unfrozen_ratio = latency_in_unfrozen_collects(
                representative_full_ms,
                unfrozen_collect_ms=unfrozen_reference,
            )
            populated_cache_full_ratio = full_with_file_ms / max(baseline.full_reclaim_ms, 0.001)
            transcript_scaling_ratio = statistics.median(large_absorb_samples) / max(
                statistics.median(small_absorb_samples),
                0.001,
            )
            latency_gate_passed = (
                frozen_collect_ratio < _FROZEN_COLLECT_RATIO_LIMIT
                and absorb_to_unfrozen_ratio < _ABSORB_TO_UNFROZEN_RATIO_LIMIT
                and full_to_unfrozen_ratio < _FULL_TO_UNFROZEN_RATIO_LIMIT
                and populated_cache_full_ratio < _POPULATED_CACHE_FULL_RATIO_LIMIT
                and transcript_scaling_ratio < _ABSORB_TRANSCRIPT_SCALING_LIMIT
            )
            latency = LatencyResult(
                unfrozen_full_collect_ms=unfrozen.collect_ms,
                unfrozen_full_collect_reference_ms=unfrozen_reference,
                unfrozen_full_collect_median_ms=unfrozen_median,
                unfrozen_prepared_action_ms=unfrozen.prepared_action_ms,
                unfrozen_prepared_layout_ms=unfrozen.prepared_layout_ms,
                unfrozen_prepared_layout_passes=unfrozen.prepared_layout_passes,
                frozen_collect_ratio=frozen_collect_ratio,
                frozen_collect_ratio_limit=_FROZEN_COLLECT_RATIO_LIMIT,
                absorb_to_unfrozen_ratio=absorb_to_unfrozen_ratio,
                absorb_to_unfrozen_ratio_limit=_ABSORB_TO_UNFROZEN_RATIO_LIMIT,
                full_to_unfrozen_ratio=full_to_unfrozen_ratio,
                full_to_unfrozen_ratio_limit=_FULL_TO_UNFROZEN_RATIO_LIMIT,
                absorb_to_unfrozen_prepared_ratio=max_gated_absorb_ms / unfrozen_action_reference,
                full_to_unfrozen_prepared_ratio=representative_full_ms / unfrozen_action_reference,
                populated_cache_full_ratio=populated_cache_full_ratio,
                populated_cache_full_ratio_limit=_POPULATED_CACHE_FULL_RATIO_LIMIT,
                small_transcript_turns=small_turns,
                small_transcript_absorb_ms=small_absorb_samples,
                large_transcript_absorb_ms=large_absorb_samples,
                max_gated_absorb_ms=max_gated_absorb_ms,
                large_post_absorb_frozen_collect_ms=large_frozen_collect_samples,
                representative_full_reclaim_ms=representative_full_ms,
                transcript_scaling_ratio=transcript_scaling_ratio,
                transcript_scaling_ratio_limit=_ABSORB_TRANSCRIPT_SCALING_LIMIT,
                gate_passed=latency_gate_passed,
            )
            whole_app_gate_passed = (
                # Census of MainScreen.on_mount's registrations: chat panel,
                # /btw panel, trajectory dashboard, session JSON panel, shell panel, and
                # suggestions. A drifted count means a surface joined or left
                # the freeze protocol without recalibration.
                participant_count == 6
                and watchdog_timer_active
                and buddy_timer_active
                and status_timer_active
                and title_task_active
                and background.mru_task_observed
            )
            report = CalibrationReport(
                turns=turns,
                theme_invalidation=theme_invalidation,
                viewport_invalidation=viewport_invalidation,
                markdown_widgets=surface_counts[0],
                unified_diff_widgets=surface_counts[1],
                code_column_widgets=surface_counts[2],
                annotation_column_widgets=surface_counts[3],
                file_suggestion_count=suggestion_count,
                main_screen_participant_count=participant_count,
                watchdog_timer_active=watchdog_timer_active,
                buddy_timer_active=buddy_timer_active,
                status_timer_active=status_timer_active,
                title_task_active=title_task_active,
                mru_task_observed=background.mru_task_observed,
                whole_app_gate_passed=whole_app_gate_passed,
                collapse=collapse,
                latency=latency,
                baseline=baseline,
                intervals=intervals,
                d_base_upper_bytes=d_base,
                d_absorb_upper_bytes=d_absorb,
                hard_absorb_limit=_RECLAIM_HARD_ABSORBS,
                soft_absorb_limit=_RECLAIM_SOFT_ABSORBS,
                projected_retained_bytes=projected,
                frozen_dead_budget_bytes=_FROZEN_DEAD_BUDGET_BYTES,
                safety_factor=2,
                byte_gate_passed=byte_gate_passed,
                aggregate_dead_cyclic_fraction=aggregate_dead_cyclic_fraction,
                dead_cyclic_fraction_limit=_DEAD_CYCLIC_FRACTION_LIMIT,
                object_gate_passed=object_gate_passed,
                collapse_gate_passed=collapse.gate_passed,
                latency_gate_passed=latency_gate_passed,
                gate_passed=(
                    whole_app_gate_passed
                    and byte_gate_passed
                    and object_gate_passed
                    and collapse.gate_passed
                    and latency_gate_passed
                ),
                absorb_p95_ms=(
                    statistics.quantiles(absorb_samples, n=20)[18]
                    if len(absorb_samples) >= 20
                    else max(absorb_samples, default=0.0)
                ),
                full_reclaim_without_file_cache_ms=baseline.full_reclaim_ms,
                full_reclaim_with_file_cache_max_ms=full_with_file_ms,
            )
            return report
    finally:
        gc.unfreeze()
        gc.collect()
        tracemalloc.stop()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=12)
    parser.add_argument("--absorb-points", type=int, nargs="+", default=(0, 4, 8, 10))
    parser.add_argument("--suggestion-count", type=int, default=20_000)
    parser.add_argument(
        "--theme-invalidation",
        action="store_true",
        help="Stress repeated theme invalidation separately; production theme changes request idle full reclaim.",
    )
    parser.add_argument("--no-viewport-invalidation", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    absorb_points = validated_absorb_points(args.absorb_points, max_absorbs=_RECLAIM_SOFT_ABSORBS)
    report = asyncio.run(
        calibrate(
            turns=max(1, args.turns),
            absorb_points=absorb_points,
            suggestion_count=max(1, args.suggestion_count),
            theme_invalidation=args.theme_invalidation,
            viewport_invalidation=not args.no_viewport_invalidation,
        )
    )
    sys.stdout.write(f"{json.dumps(asdict(report), sort_keys=True)}\n")
    return 0 if report.gate_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
