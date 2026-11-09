# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure chartkit, presentation and page-builder units, rendered without mounting an App."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from chrys.app.tui.widgets.trajectory import insights as trajectory_insights
from chrys.app.tui.widgets.trajectory import overview as trajectory_overview
from chrys.app.tui.widgets.trajectory.chartkit import (
    WaterfallLane,
    WaterfallTurn,
    bordered_section,
    coverage_bar,
    fit_cells,
    fit_text_cells,
    section_interior_width,
    time_ruler,
    unresolved_bar,
    waterfall_lanes,
)
from chrys.app.tui.widgets.trajectory.insights import (
    _format_metric_bytes,
    _input_share_metric,
    has_diagnostic_content,
)
from chrys.app.tui.widgets.trajectory.overview import _fit_path_middle
from chrys.app.tui.widgets.trajectory.presentation import (
    RenderContext,
    ResponsiveTier,
    align_edges,
    cache_hit_metric,
    format_duration,
    identity_with_hook_id,
)
from chrys.foundation.trajectory.envelope import SegmentedField
from chrys.foundation.trajectory.metadata import ANALYTICS_ITEM_ID_KEY
from chrys.service.analytics import (
    AnalysisAvailability,
    ChangeVerification,
    ChangeVerificationRow,
    ChangeVerificationState,
    FindingRow,
    FindingSeverity,
    Metric,
    Precision,
    SubmissionLatencyBucket,
    TimelineDiagnosticCode,
    TimeSlice,
    TokenUsage,
    TrajectoryAnalysis,
    TrajectoryAnalyzer,
    TrajectoryDiagnostics,
    UsageBucket,
)
from tests.app.tui.widgets._trajectory_fixtures import _NS, _write_p1_operations, _write_p2_operations, plain_look
from tests.service.analytics._events import NS, EventLog, installed_events_path


def test_coverage_bar_keeps_missing_distinct_from_unresolved() -> None:
    bar = coverage_bar(25, 25, 25, 25, width=8)

    assert bar.plain == "██▒▒░░··"


@pytest.mark.parametrize(
    ("identity", "hook_id", "expected"),
    [
        ("before_tool_call", "hook_id_1", "before_tool (hook_id_1)"),
        ("after_tool_call", "hook_abcdefghijklmnop", "after_tool (hook_abcdefgh...)"),
        ("after_turn", None, "after_turn"),
    ],
)
def test_hook_identity_shortens_tool_event_names_and_caps_only_the_hook_id(
    identity: str,
    hook_id: str | None,
    expected: str,
) -> None:
    assert identity_with_hook_id(identity, hook_id) == expected


def test_operation_reason_messages_cover_every_timeline_diagnostic_code() -> None:
    assert set(trajectory_insights._OPERATION_REASON_MESSAGES) == set(TimelineDiagnosticCode)


def test_grouped_grid_lines_keeps_each_semantic_group_in_one_column() -> None:
    groups = [
        [trajectory_overview._LabeledValue(Text(f"g{group}r{row}"), Text(str(row))) for row in range(3)]
        for group in range(4)
    ]

    lines = trajectory_overview._grouped_grid_lines(groups, width=120, columns=4)

    assert len(lines) == 3
    for row, line in enumerate(lines):
        assert all(f"g{group}r{row}" in line.plain for group in range(4))


@pytest.mark.parametrize(
    "diagnostics",
    [
        TrajectoryDiagnostics(span_duration_mismatch_count=1),
        TrajectoryDiagnostics(containment_violation_count=1),
        TrajectoryDiagnostics(malformed_hook_execution_mode_count=1),
        TrajectoryDiagnostics(side_call_empty_shell_revisions=("a" * 32,)),
        TrajectoryDiagnostics(unidentified_membership_revision_count=1),
    ],
)
def test_diagnostic_content_gate_does_not_depend_on_overview_precision(
    diagnostics: TrajectoryDiagnostics,
) -> None:
    assert has_diagnostic_content(diagnostics) is True
    assert has_diagnostic_content(TrajectoryDiagnostics()) is False


@pytest.mark.parametrize("width", [1, 2, 4, 11])
def test_unknown_timeline_primitives_never_exceed_requested_width(width: int) -> None:
    assert cell_len(unresolved_bar(width).plain) == width
    assert cell_len(time_ruler(_NS, width=width).plain) == width


def test_time_ruler_spreads_tick_labels_across_the_axis() -> None:
    ruler = time_ruler(12 * _NS, width=60).plain

    assert cell_len(ruler) == 60
    assert ruler.startswith("0s")
    assert ruler.endswith("12s")
    assert "4.8s" in ruler and "7.2s" in ruler


def test_time_ruler_tick_units_follow_the_axis_magnitude() -> None:
    sub_second = time_ruler(800_000_000, width=60).plain
    minutes = time_ruler(638 * _NS, width=90).plain
    hours = time_ruler(3 * 3600 * _NS + 1800 * _NS, width=90).plain
    days = time_ruler(2 * 86400 * _NS, width=90).plain

    assert sub_second.startswith("0ms") and sub_second.endswith("800ms")
    assert minutes.startswith("0s") and minutes.endswith("10m38s")
    assert "1m46s" in minutes and "3m33s" in minutes
    assert hours.startswith("0s") and hours.endswith("3h30m")
    assert "35m00s" in hours and "1h10m" in hours
    assert days.startswith("0s") and days.endswith("2d00h")
    assert "1d" in days


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(86399, "23h 59m"), (86400, "1d 00h"), (102050, "1d 04h"), (172800, "2d 00h")],
)
def test_trajectory_durations_include_days(seconds: int, expected: str) -> None:
    assert format_duration(seconds * _NS) == expected


@pytest.mark.parametrize(
    ("nanoseconds", "expected"),
    [
        (999_400_000, "999 ms"),
        (999_500_000, "1.00 s"),
        (59_994_000_000, "59.99 s"),
        # Its double sits just below 59.995, so the two-decimal form still reads
        # 59.99 s; the unit changes one nanosecond later.
        (59_995_000_000, "59.99 s"),
        (59_996_000_000, "1m 00s"),
        (3_600_000_000_000, "1h 00m"),
    ],
)
def test_trajectory_durations_switch_units_before_rounding_overflows(nanoseconds: int, expected: str) -> None:
    assert format_duration(nanoseconds) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1_023, "1023 B ✓"),
        (1_048_524, "1023.9 KiB ✓"),
        (1_048_525, "1.0 MiB ✓"),
        (1_048_575, "1.0 MiB ✓"),
        (2_097_152, "2.0 MiB ✓"),
    ],
)
def test_metric_bytes_gain_a_mebibyte_unit_before_kibibytes_read_1024(value: int, expected: str) -> None:
    assert _format_metric_bytes(Metric(value, Precision.EXACT)) == expected


def test_a_time_ruler_just_under_a_second_ends_at_one_second() -> None:
    ruler = time_ruler(999_600_000, width=60).plain

    assert ruler.startswith("0ms")
    assert ruler.rstrip().endswith("1.0s"), ruler


def test_derived_token_ratios_preserve_missing_and_inconsistent_evidence() -> None:
    estimated_usage = TokenUsage(
        {
            UsageBucket.INPUT: Metric(40, Precision.ESTIMATED),
            UsageBucket.CACHE_READ: Metric(20, Precision.MISSING),
        }
    )
    exact_session = TokenUsage({UsageBucket.INPUT: Metric(100, Precision.EXACT)})
    zero_session = TokenUsage({UsageBucket.INPUT: Metric(0, Precision.EXACT)})
    inconsistent_usage = TokenUsage(
        {
            UsageBucket.INPUT: Metric(40, Precision.EXACT),
            UsageBucket.CACHE_READ: Metric(50, Precision.EXACT),
        }
    )

    assert _input_share_metric(estimated_usage, exact_session).precision is Precision.ESTIMATED
    assert _input_share_metric(estimated_usage, zero_session).precision is Precision.MISSING
    assert cache_hit_metric(estimated_usage).precision is Precision.MISSING
    assert cache_hit_metric(inconsistent_usage).precision is Precision.UNRESOLVED


def test_bordered_section_is_cell_width_safe_for_cjk_titles() -> None:
    lines = bordered_section("发现", [Text("一行内容")], width=18, console=Console())

    assert lines[0].plain.startswith("┌─ 发现 ")
    assert all(cell_len(line.plain) == 18 for line in lines)


def test_bordered_section_pads_one_cell_inside_each_vertical_border() -> None:
    lines = bordered_section("T", [Text("x" * 20)], width=12, console=Console())

    assert section_interior_width(12) == 8
    assert lines[1].plain == "│ xxxxxxxx │"
    assert lines[2].plain == "│ xxxxxxxx │"
    assert lines[-1].plain == "└──────────┘"
    assert all(cell_len(line.plain) == 12 for line in lines)


def test_waterfall_lanes_paint_each_cell_in_exactly_one_lane() -> None:
    # One turn, 10 cells: the model covers everything except a tool window that
    # owns most of cells 4-5; model slivers around it must not repaint them.
    turn = WaterfallTurn(
        1000,
        {
            "model": [(0, 420), (421, 430), (580, 1000)],
            "tool": [(430, 580)],
        },
    )
    lanes = waterfall_lanes([turn], width=10, lanes=[WaterfallLane("tool", "▬", ""), WaterfallLane("model", "█", "")])

    assert lanes["model"].plain == "████  ████"
    assert lanes["tool"].plain == "    ▬▬    "
    assert all(
        (model_cell == " ") or (tool_cell == " ")
        for model_cell, tool_cell in zip(lanes["model"].plain, lanes["tool"].plain, strict=True)
    )


def test_waterfall_lanes_keep_turn_separators_and_empty_canvas() -> None:
    lanes = waterfall_lanes(
        [WaterfallTurn(100, {"model": [(0, 100)]}), WaterfallTurn(100, {"model": [(0, 100)]})],
        width=9,
        lanes=[WaterfallLane("model", "█", "")],
    )

    assert lanes["model"].plain == "████┊████"
    assert waterfall_lanes([], width=4, lanes=[WaterfallLane("model", "█", "")])["model"].plain == "    "


def test_two_edge_alignment_is_cell_width_safe_for_cjk_labels() -> None:
    line = align_edges(Text("标签"), Text("值 [精确]"), 20)

    assert cell_len(line.plain) == 20
    assert line.plain.startswith("标签")
    assert line.plain.endswith("值 [精确]")


@pytest.mark.parametrize(
    "path",
    [
        "/Users/0x7c13/Repos/deeply/nested/report.final.json",
        "C:\\Users\\0x7c13\\Repos\\deeply\\nested\\report.final.json",
    ],
)
def test_fit_path_middle_preserves_the_full_filename_and_extension(path: str) -> None:
    fitted = _fit_path_middle(path, 30)
    extension_only_fallback = _fit_path_middle(path, 10)

    assert cell_len(fitted) <= 30
    assert fitted.endswith(("/report.final.json", "\\report.final.json"))
    assert fitted.startswith(("/", "C:"))
    assert "…" in fitted
    assert cell_len(extension_only_fallback) <= 10
    assert extension_only_fallback.startswith("…")
    assert extension_only_fallback.endswith(".json")


@pytest.mark.parametrize(
    ("cache_hit", "semantic_name", "fallback"),
    [
        (29, "error", "red"),
        (30, "warning", "yellow"),
        (60, "warning", "yellow"),
        (61, "success", "green"),
    ],
)
def test_cache_hit_meter_style_uses_threshold_theme_colors(
    cache_hit: int,
    semantic_name: str,
    fallback: str,
) -> None:
    look = plain_look({"error": "#aa0000", "warning": "#bbbb00", "success": "#00cc00"})

    assert trajectory_insights._cache_hit_style(look, cache_hit) == look.semantic_style(semantic_name, fallback)


def _change_verification_analysis(
    rows: tuple[ChangeVerificationRow, ...],
    *,
    modified: Metric | None = None,
    workspace_root: str | None = None,
) -> TrajectoryAnalysis:
    """An available analysis whose change section holds *rows*, with exact counts unless overridden."""
    exact_zero = Metric(0, Precision.EXACT)
    exact_rows = Metric(len(rows), Precision.EXACT)
    return TrajectoryAnalysis(
        availability=AnalysisAvailability.AVAILABLE,
        path=Path("events.jsonl"),
        generation=1,
        change_verification=ChangeVerification(
            detail_available=True,
            detection_truncated=False,
            files_touched=exact_rows,
            created=exact_zero,
            modified=exact_rows if modified is None else modified,
            deleted=exact_zero,
            net_zero=exact_zero,
            rows=rows,
            workspace_root=workspace_root,
        ),
    )


def test_change_verification_path_display_copy_is_surrogate_safe() -> None:
    raw_path = "changed-\udcff.py"
    analysis = _change_verification_analysis(
        (
            ChangeVerificationRow(
                path=raw_path,
                state=ChangeVerificationState.VERIFIED,
                last_change_turn=1,
                precision=Precision.EXACT,
            ),
        )
    )

    lines = trajectory_overview._change_verification_lines(plain_look(), analysis, width=80)

    assert any("changed-\\udcff.py" in line.plain for line in lines)


def test_change_verification_rows_and_counts_carry_precision_badges() -> None:
    """Each row shows its own precision and the counts line shows the worst
    of the five count precisions, so a degraded change section cannot pass
    for exact measurements."""
    analysis = _change_verification_analysis(
        (
            ChangeVerificationRow(
                path="proven.py",
                state=ChangeVerificationState.VERIFIED,
                last_change_turn=1,
                precision=Precision.EXACT,
            ),
            ChangeVerificationRow(
                path="unprovable.py",
                state=ChangeVerificationState.NET_ZERO,
                last_change_turn=1,
                precision=Precision.UNRESOLVED,
            ),
        ),
        modified=Metric(2, Precision.ESTIMATED, "counts include window-inferred or peer-contested mutations"),
    )

    lines = trajectory_overview._change_verification_lines(plain_look(), analysis, width=80)
    narrow = trajectory_overview._change_verification_lines(plain_look(), analysis, width=22)

    assert lines[0].plain.endswith("~")
    assert next(line.plain for line in lines if "proven.py" in line.plain).endswith("✓")
    assert next(line.plain for line in lines if "unprovable.py" in line.plain).endswith("✗")
    # A compressed column truncates the counts, never the badge.
    assert "…" in narrow[0].plain
    assert narrow[0].plain.endswith("~")


def test_change_verification_middle_crops_paths_before_the_filename() -> None:
    analysis = _change_verification_analysis(
        (
            ChangeVerificationRow(
                path="/Users/0x7c13/Repos/deeply/nested/report.final.json",
                state=ChangeVerificationState.UNVERIFIED,
                last_change_turn=1,
                precision=Precision.EXACT,
            ),
        )
    )

    lines = trajectory_overview._change_verification_lines(plain_look(), analysis, width=40)

    row = next(line.plain for line in lines if "report.final.json" in line.plain)
    assert row.startswith("/Users/")
    assert "…/report.final.json" in row
    assert row.endswith("✓")
    assert cell_len(row) == 40


@pytest.mark.parametrize(
    ("precision", "badge"),
    [(Precision.EXACT, "✓"), (Precision.UNRESOLVED, "✗")],
)
def test_insights_section_titles_render_their_panel_precision(
    tmp_path: Path,
    precision: Precision,
    badge: str,
) -> None:
    path = tmp_path / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p2_operations(path)
    analysis = TrajectoryAnalyzer().load(path)
    assert analysis.insights is not None
    reason = "sentinel unresolved panel" if precision is Precision.UNRESOLVED else None
    insights = replace(
        analysis.insights,
        tools=replace(analysis.insights.tools, precision=precision, reason=reason),
        mcp=replace(analysis.insights.mcp, precision=precision, reason=reason),
        skills=replace(analysis.insights.skills, precision=precision, reason=reason),
        context_carrying_precision=precision,
        context_carrying_reason=reason,
    )

    context = RenderContext(width=220, tier=ResponsiveTier.WIDE)
    page = "\n".join(
        line.plain
        for line in trajectory_insights.insights_lines(plain_look(), context, replace(analysis, insights=insights))
    )

    for title in ("Skills", "MCP servers", "Tool activity", "Context re-send cost · top 5"):
        assert f"{title} {badge}" in page


@pytest.mark.parametrize(
    ("precision", "badge"),
    [(Precision.EXACT, "✓"), (Precision.UNRESOLVED, "✗")],
)
def test_submission_aggregate_renders_derived_precision_for_the_same_duration(
    tmp_path: Path,
    precision: Precision,
    badge: str,
) -> None:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p1_operations(path)
    analysis = TrajectoryAnalyzer().load(path)
    assert analysis.submission_latency is not None
    stats = next(
        bucket for bucket in analysis.submission_latency.buckets if bucket.bucket is SubmissionLatencyBucket.BECAME_TURN
    )
    reason = "sentinel unresolved aggregate" if precision is Precision.UNRESOLVED else None
    four_seconds = Metric(4 * _NS, precision, reason)
    submission = replace(
        analysis.submission_latency,
        buckets=(replace(stats, p50_ns=four_seconds, p90_ns=four_seconds, max_ns=four_seconds),),
    )

    lines = trajectory_overview._submission_latency_lines(
        plain_look(), replace(analysis, submission_latency=submission), width=80
    )

    aggregate = next(line.plain for line in lines if "started a new turn" in line.plain)
    assert aggregate.count("4.00 s") == 3
    assert aggregate.endswith(badge)


@pytest.mark.parametrize("value", ["", "abc", "中文", "e\u0301", "a中b"])
@pytest.mark.parametrize("width", [-1, 0, 1, 2, 5, 12])
def test_fit_cells_pads_and_crops_to_terminal_width(value: str, width: int) -> None:
    assert cell_len(fit_cells(value, width)) == max(0, width)


@pytest.mark.parametrize("value", ["", "abc", "中文", "e\u0301", "a中b"])
@pytest.mark.parametrize("width", [-1, 0, 1, 2, 5, 12])
def test_fit_text_cells_pads_and_crops_styled_text_to_terminal_width(value: str, width: int) -> None:
    assert cell_len(fit_text_cells(Text(value, style="bold"), width).plain) == max(0, width)


def test_fit_text_cells_marks_a_crop_with_an_ellipsis_and_keeps_the_style() -> None:
    fitted = fit_text_cells(Text("abcdef", style="bold"), 4)

    assert fitted.plain == "abc…"
    assert fitted.get_style_at_offset(Console(), 0).bold is True


def _finding_row(rule_id: str, severity: FindingSeverity, *, turn: int, **detail_args: int) -> FindingRow:
    return FindingRow(
        evidence_key=f"{rule_id}:{turn}",
        occurrence_id=f"{rule_id}:{turn}",
        rule_id=rule_id,
        severity=severity,
        deterministic=True,
        detail_args=tuple(sorted(detail_args.items())),
        precision=Precision.EXACT,
        turn_id=f"turn-{turn}",
        turn_number=turn,
    )


def test_findings_render_one_block_per_rule_ranked_by_impact(tmp_path: Path) -> None:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p1_operations(path)
    analysis = TrajectoryAnalyzer().load(path)
    repeated = tuple(
        _finding_row("repeated-read", FindingSeverity.WARNING, turn=turn, count=turn, duration_ms=turn * 1000)
        for turn in range(1, 6)
    )
    unverified = _finding_row(
        "unverified-change", FindingSeverity.ERROR, turn=5, count=4, verifies=0, verify_attempts=0, unclassified_shell=0
    )

    lines = [
        line.plain
        for line in trajectory_insights._finding_lines(
            plain_look(), replace(analysis, findings=(*repeated, unverified)), width=200
        )
    ]

    # The error-severity rule leads, and each rule gets one header however often it fired.
    assert [line for line in lines if not line.startswith(" ")] == ["! Unverified changes  ✓", "△ Repeated reads  ✓"]
    assert "4 edits were never followed by a test or check" in lines[1]
    repeated_header = lines.index("△ Repeated reads  ✓")
    assert lines[repeated_header + 1].startswith("    15 reads or searches repeated identical calls")
    assert lines[-4:] == [
        "    · Turn 5 · 5 repeats · 5.00 s",
        "    · Turn 4 · 4 repeats · 4.00 s",
        "    · Turn 3 · 3 repeats · 3.00 s",
        "    · +2 more",
    ]


def test_finding_lines_wrap_under_their_indent(tmp_path: Path) -> None:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p1_operations(path)
    analysis = TrajectoryAnalyzer().load(path)
    repeated = _finding_row(
        "repeated-read", FindingSeverity.WARNING, turn=2, count=3, duration_ms=1500, result_tokens=7
    )

    lines = [
        line.plain
        for line in trajectory_insights._finding_lines(plain_look(), replace(analysis, findings=(repeated,)), width=24)
    ]

    assert lines[0] == "△ Repeated reads  ✓"
    example_start = next(index for index, line in enumerate(lines) if line.startswith("    · "))
    assert all(line.startswith("    ") and not line.startswith("    · ") for line in lines[1:example_start])
    assert len(lines) > example_start + 1
    assert all(line.startswith("      ") for line in lines[example_start + 1 :])
    assert all(cell_len(line) <= 24 for line in lines)


def test_unresolved_metrics_are_grouped_by_reason_with_capped_scopes(tmp_path: Path) -> None:
    path = tmp_path / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p2_operations(path)
    analysis = TrajectoryAnalyzer().load(path)
    (turn,) = analysis.turns
    turns = tuple(replace(turn, turn_number=number) for number in range(1, 9))

    lines = [
        line.plain
        for line in trajectory_insights._unresolved_metric_lines(plain_look(), replace(analysis, turns=turns))
    ]

    # The overview ratios carry their turns' root reason, so one cause is one line.
    assert len(lines) == 1
    assert "unresolved in session, Turn 1, Turn 2, Turn 3, Turn 4, Turn 5, +3 more: " in lines[0]
    assert "tool operation lacks parent_model_operation_id" in lines[0]


@pytest.mark.parametrize(("right_height", "paired"), [(3, True), (40, False)])
def test_section_pair_stacks_when_one_side_is_much_taller(right_height: int, paired: bool) -> None:
    lines = trajectory_insights._adaptive_section_pair(
        plain_look(),
        Text("Left"),
        lambda _: [Text("a")] * 3,
        Text("Right"),
        lambda _: [Text("b")] * right_height,
        width=120,
        paired=True,
    )

    assert any("Left" in line.plain and "Right" in line.plain for line in lines) is paired


@pytest.mark.parametrize(
    ("detail_args", "summary"),
    [
        (
            {"count": 2, "verifies": 0, "verify_attempts": 0, "unclassified_shell": 0},
            "2 edits were never followed by a test or check",
        ),
        (
            {"count": 2, "verifies": 0, "verify_attempts": 1, "unclassified_shell": 0},
            "2 edits never had a passing test or check",
        ),
        (
            {"count": 2, "verifies": 1, "verify_attempts": 2, "unclassified_shell": 0, "last_verify_turn": 3},
            (
                "2 edits were not confirmed by a test or check: the record does not show the last successful one"
                " (turn 3) starting after they finished"
            ),
        ),
        # A later command whose text is unavailable may have been a passing
        # check, whatever the recognized ones did.
        *(
            (
                {"count": 2, "unclassified_shell": 1, **seen},
                (
                    "2 edits have no recognized passing test or check after them, but the text of some later shell"
                    " commands is unavailable"
                ),
            )
            for seen in (
                {"verifies": 0, "verify_attempts": 0},
                {"verifies": 0, "verify_attempts": 1},
                {"verifies": 1, "verify_attempts": 1, "last_verify_turn": 3},
            )
        ),
    ],
)
def test_unverified_change_summary_says_why_nothing_confirmed_the_edits(
    tmp_path: Path, detail_args: dict[str, int], summary: str
) -> None:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p1_operations(path)
    analysis = TrajectoryAnalyzer().load(path)
    row = _finding_row("unverified-change", FindingSeverity.ERROR, turn=5, **detail_args)

    text = " ".join(
        line.plain.strip()
        for line in trajectory_insights._finding_lines(plain_look(), replace(analysis, findings=(row,)), width=400)
    )

    assert summary in text
    # Only when every command was read and none was recognized as a test does the text point at the setting.
    advises_setting = detail_args["verify_attempts"] == 0 and not detail_args["unclassified_shell"]
    assert ("trajectory.verify_commands" in text) is advises_setting


def test_costly_context_example_keeps_its_total_on_one_line(tmp_path: Path) -> None:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p1_operations(path)
    analysis = TrajectoryAnalyzer().load(path)
    row = _finding_row("context-carrying-load", FindingSeverity.INFO, turn=1, load=114_000, tokens=8_100, requests=14)

    for width in range(40, 121):
        lines = [
            line.plain.rstrip()
            for line in trajectory_insights._finding_lines(
                plain_look(), replace(analysis, findings=(row,)), width=width
            )
        ]
        assert any("≈114.0k" in line for line in lines), width
        assert not any(line.endswith("≈") for line in lines), width


def test_finding_prose_breaks_between_cjk_characters() -> None:
    sentence = "15 次读取或搜索与同一轮中更早的调用完全相同\uff0c且期间没有任何改动。"

    lines = trajectory_insights._wrap_prose(sentence, 30)

    # The number stays with the sentence instead of standing alone on a line.
    assert lines[0].startswith("15 次读取")
    assert "".join(lines) == sentence
    assert all(cell_len(line) <= 30 for line in lines)
    assert not any(line.startswith(("\uff0c", "。")) for line in lines)
    # Words still break only at spaces, and a closing mark stays with its word.
    assert trajectory_insights._wrap_prose("加入 trajectory.verify。", 20) == ["加入", "trajectory.verify。"]
    # A word too long for the line is cut, but its closing mark does not start the last piece.
    assert trajectory_insights._wrap_prose("加入 trajectory.verify_commands。", 26) == [
        "加入",
        "trajectory.verify_command",
        "s。",
    ]
    # An opening bracket does not end a line.
    assert trajectory_insights._wrap_prose("最后一次成功的测试或检查\uff08第 4 轮\uff09之后", 26) == [
        "最后一次成功的测试或检查",
        "\uff08第 4 轮\uff09之后",
    ]


@pytest.mark.parametrize(
    ("path", "root", "shown"),
    [
        ("/home/me/proj/src/a.py", "/home/me/proj", "src/a.py"),
        ("/home/me/proj/src/a.py", "/home/me/proj/", "src/a.py"),
        ("/home/me/project2/a.py", "/home/me/proj", "/home/me/project2/a.py"),
        ("/home/me/Proj/a.py", "/home/me/proj", "/home/me/Proj/a.py"),
        # A Windows folder keeps the case it was typed in; the path has the case on disk.
        ("C:\\Users\\Me\\proj\\src\\a.cs", "c:\\users\\me\\proj", "src\\a.cs"),
        ("/a.py", None, "/a.py"),
    ],
)
def test_change_verification_paths_are_shown_relative_to_the_working_folder(
    path: str, root: str | None, shown: str
) -> None:
    analysis = _change_verification_analysis(
        (ChangeVerificationRow(path, ChangeVerificationState.VERIFIED, 1, Precision.EXACT),), workspace_root=root
    )

    lines = trajectory_overview._change_verification_lines(plain_look(), analysis, width=80)

    assert any(line.plain.lstrip().startswith(shown) for line in lines)


def test_approval_finding_names_the_tools_waited_on_in_every_attempt_of_its_turn(tmp_path: Path) -> None:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p1_operations(path)
    analysis = TrajectoryAnalyzer().load(path)
    turn = analysis.turns[0]
    (first_attempt,) = turn.attempts
    wait = TimeSlice(
        family="approval",
        slice_index=0,
        turn_id="retried-attempt",
        runtime_id=turn.runtime_id,
        operation_id=None,
        owner="approval",
        start_ns=0,
        end_ns=2_000_000_000,
        wall_bucket=None,
        counts_as_work=False,
        compute_weight=False,
        response_weight=False,
        tool_name="run_shell",
    )
    # The finding names the turn by one attempt but measures the whole turn,
    # so the waits of every attempt count; a turn it does not name does not.
    other_wait = replace(wait, turn_id=first_attempt.turn_id, tool_name="edit_file", end_ns=20_000_000_000)
    retried = replace(
        turn,
        attempts=(first_attempt, replace(first_attempt, turn_id="retried-attempt", is_retry=True)),
        slices=(*turn.slices, other_wait, wait),
    )
    unnamed = replace(
        turn,
        turn_id="other-turn",
        attempts=(replace(first_attempt, turn_id="other-turn"),),
        slices=(replace(wait, turn_id="other-turn", tool_name="grep_files"),),
    )
    row = replace(
        _finding_row("approval-blocking-share", FindingSeverity.WARNING, turn=1, duration_ms=2000, percentage=40),
        turn_id="retried-attempt",
    )

    text = " ".join(
        line.plain.strip()
        for line in trajectory_insights._finding_lines(
            plain_look(),
            replace(analysis, turns=(retried, *analysis.turns[1:], unnamed), findings=(row,)),
            width=400,
        )
    )

    assert "Longest waits: edit_file 20.00 s, run_shell 2.00 s" in text
    assert "grep_files" not in text


def test_approval_finding_counts_a_tools_overlapping_waits_once(tmp_path: Path) -> None:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p1_operations(path)
    analysis = TrajectoryAnalyzer().load(path)
    turn = analysis.turns[0]
    wait = TimeSlice(
        family="approval",
        slice_index=0,
        turn_id=turn.turn_id,
        runtime_id=turn.runtime_id,
        operation_id=None,
        owner="approval",
        start_ns=0,
        end_ns=2_000_000_000,
        wall_bucket=None,
        counts_as_work=False,
        compute_weight=False,
        response_weight=False,
        tool_name="zsh",
    )
    # Parallel calls ask at the same time: waits from 0 to 2 s and from 1 to
    # 3 s are 3 s of waiting, as the finding's total counts them.
    overlapping = replace(wait, slice_index=1, start_ns=1_000_000_000, end_ns=3_000_000_000)
    row = replace(
        _finding_row("approval-blocking-share", FindingSeverity.WARNING, turn=1, duration_ms=3000, percentage=40),
        turn_id=turn.turn_id,
    )

    text = " ".join(
        line.plain.strip()
        for line in trajectory_insights._finding_lines(
            plain_look(),
            replace(
                analysis,
                turns=(replace(turn, slices=(*turn.slices, wait, overlapping)), *analysis.turns[1:]),
                findings=(row,),
            ),
            width=400,
        )
    )

    assert "Longest waits: zsh 3.00 s" in text


def test_costly_context_examples_pair_each_items_name_with_its_own_figures(tmp_path: Path) -> None:
    # Every item still in context at the end was last carried by the same
    # request; each example must still describe its own item.
    call_item, result_item = "1" * 32, "2" * 32
    revision_id, segment_id, exchange_id = "8" * 32, "9" * 32, "a" * 32
    path = installed_events_path(tmp_path)
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    revision = log.add(
        "context.revision.recorded",
        NS,
        operation_id=revision_id,
        parent_operation_id=exchange_id,
        payload={
            "revision_id": revision_id,
            "is_checkpoint": True,
            "item_count": 2,
            "untokenized_item_count": 0,
            "unidentified_item_count": 0,
        },
        segmented_fields=(SegmentedField(field_pointer="/payload/refs", segment_group_id=segment_id, segment_count=1),),
    )
    log.add(
        "event.segment",
        NS,
        operation_id=None,
        payload={
            "parent_event_id": revision.event_id,
            "field_pointer": "/payload/refs",
            "segment_group_id": segment_id,
            "segment_index": 0,
            "segment_count": 1,
            "encoding": "array_slice",
            "entries": [
                {"item_id": item_id, "occurrence": 0, "position": position, "action": "add"}
                for position, item_id in enumerate((call_item, result_item))
            ],
        },
    )
    log.span("model.exchange", exchange_id, 2 * NS, 3 * NS, start_payload={"context_revision_id": revision_id})
    log.add("turn.finished", 4 * NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)
    messages = [
        {
            "role": "assistant",
            "additional_properties": {ANALYTICS_ITEM_ID_KEY: call_item, "_group": {"token_count": 150_000}},
            "contents": [{"type": "function_call", "call_id": "call-1", "name": "read_file", "arguments": "{}"}],
        },
        {
            "role": "tool",
            "additional_properties": {ANALYTICS_ITEM_ID_KEY: result_item, "_group": {"token_count": 120_000}},
            "contents": [{"type": "function_result", "call_id": "call-1", "result": "ok"}],
        },
    ]
    path.parents[1].joinpath("session.json").write_text(json.dumps({"state": {"messages": messages}}), encoding="utf-8")

    lines = [
        line.plain.strip()
        for line in trajectory_insights._finding_lines(plain_look(), TrajectoryAnalyzer().load(path), width=400)
    ]

    examples = [line for line in lines if " tok " in line]
    assert len(examples) == 2
    assert any("assistant message" in line and "150" in line for line in examples), examples
    assert any("tool result" in line and "120" in line for line in examples), examples
