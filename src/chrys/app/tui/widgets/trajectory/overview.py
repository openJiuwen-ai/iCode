# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The trajectory dashboard's Overview page: session info, KPIs, the turn
waterfall, usage, action and change sections, and submission latency."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rich.cells import cell_len
from rich.style import Style
from rich.text import Text

from chrys.app.tui.util.formatting import format_byte_size
from chrys.app.tui.widgets.trajectory.chartkit import (
    coverage_bar,
    fit_cells,
    percentage_meter,
    section_interior_width,
    time_ruler,
    waterfall_lanes,
)
from chrys.app.tui.widgets.trajectory.presentation import (
    BUCKET_IDLE,
    BUCKET_MODEL,
    BUCKET_TOOLS,
    BUCKET_WAIT,
    ELAPSED_SCOPE,
    METRIC_CP_COMPUTE,
    METRIC_CP_RESPONSE,
    METRIC_ELAPSED,
    METRIC_OVERLAP,
    METRIC_PARALLELISM,
    METRIC_USAGE,
    METRIC_WORK,
    NO_TURNS,
    TIME_RULER,
    TOKEN_CACHE_HIT,
    TOKEN_CACHE_READ,
    TOKEN_INPUT,
    TOKEN_OUTPUT,
    TOKEN_REASONING,
    TOOL_USAGE_MORE,
    TURN_LABEL,
    UNAVAILABLE,
    DashboardLook,
    RenderContext,
    ResponsiveTier,
    align_edges,
    badged_section_title,
    bordered_section_row,
    cache_hit_metric,
    callsite,
    derived_metric_precision,
    fit_text_right,
    format_duration,
    format_tokens,
    metric_value,
    precision_badge,
    precision_style,
    section_box,
    section_row_widths,
)
from chrys.app.tui.widgets.trajectory.session_info import SessionStorage
from chrys.foundation.i18n import MessageDef, msg
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.service.analytics import (
    ActionClass,
    ChangeVerificationState,
    Metric,
    Precision,
    SubmissionLatencyBucket,
    SubmissionLatencySample,
    ToolUsagePanel,
    TrajectoryAnalysis,
    TrajectoryOverview,
    UsageBucket,
    WallBucket,
)

_UTILIZATION = msg(
    "tui.trajectory.utilization",
    fallback="Busy share (model and tools independent; >100% = parallel work)",
)
_COVERAGE = msg("tui.trajectory.coverage", fallback="data confidence")
_COVERAGE_SHARES = msg(
    "tui.trajectory.coverage_shares",
    fallback="{exact}✓/{estimated}~/{missing}−/{unresolved}✗",  # noqa: RUF001
)
_KPI_TIME = msg("tui.trajectory.kpi.time", fallback="Time & usage")
_KPI_SPLIT = msg("tui.trajectory.kpi.split", fallback="Where time went")
_KPI_PARALLEL = msg("tui.trajectory.kpi.parallel", fallback="Parallelism & busy")
_SESSION_INFO = msg("tui.trajectory.session_info.title", fallback="Session info")
_SESSION_INFO_PATH = msg("tui.trajectory.session_info.path", fallback="folder")
_SESSION_INFO_COPY_PATH = msg("tui.trajectory.session_info.copy_path", fallback="Copy path")
_SESSION_INFO_OPEN_FOLDER = msg("tui.trajectory.session_info.open_folder", fallback="Open folder")
_SESSION_INFO_ON_DISK = msg("tui.trajectory.session_info.on_disk", fallback="on disk")
_SESSION_INFO_MUTATIONS = msg("tui.trajectory.session_info.mutations", fallback="diff backups")
_SESSION_INFO_SNAPSHOTS = msg("tui.trajectory.session_info.snapshots", fallback="rollback snapshots")
_SESSION_INFO_SUB_AGENTS = msg("tui.trajectory.session_info.sub_agents", fallback="sub-agent sessions")
_SESSION_INFO_FIRST_MESSAGE = msg("tui.trajectory.session_info.first_message", fallback="first message")
_SESSION_INFO_LAST_REPLY = msg("tui.trajectory.session_info.last_reply", fallback="last reply")
_SESSION_INFO_SPAN = msg("tui.trajectory.session_info.span", fallback="first → last")
_SESSION_INFO_TURNS = msg("tui.trajectory.session_info.turns", fallback="turns")
_SESSION_INFO_EVENTS = msg("tui.trajectory.session_info.events", fallback="events logged")
_SESSION_INFO_RUNTIMES = msg("tui.trajectory.session_info.runtimes", fallback="times opened")
_SESSION_INFO_FILES = msg("tui.trajectory.session_info.files", fallback="{count} file", plural_fallback="{count} files")
_WATERFALL = msg(
    "tui.trajectory.waterfall",
    fallback="Per-turn time breakdown · turns {first}-{last}",
)
_CHARTS_TOO_NARROW = msg(
    "tui.trajectory.charts_too_narrow",
    fallback="Terminal too narrow; widen it to display charts.",
)
_ACTION_FUNNEL = msg("tui.trajectory.action_funnel.title", fallback="Action breakdown")
_ACTION_SEARCH = msg("tui.trajectory.action.search", fallback="search")
_ACTION_READ = msg("tui.trajectory.action.read", fallback="read")
_ACTION_EDIT = msg("tui.trajectory.action.edit", fallback="edit")
_ACTION_VERIFY = msg("tui.trajectory.action.verify", fallback="verify")
_FAILURE_RECOVERY = msg("tui.trajectory.failure_recovery.title", fallback="Failure recovery")
_TOOL_FAILURES = msg("tui.trajectory.failure_recovery.failures", fallback="tool failures")
_MEDIAN_RECOVERY = msg("tui.trajectory.failure_recovery.median", fallback="median recovery after failure")
_RETRY_AMPLIFICATION = msg(
    "tui.trajectory.failure_recovery.amplification",
    fallback="retry overhead",
)
_REPEATED_SIGNATURES = msg(
    "tui.trajectory.failure_recovery.repeated",
    fallback="repeated identical failures",
)
_TOKEN_COUNT = msg("tui.trajectory.token_count", fallback="{tokens} tokens")
_CHANGE_VERIFICATION = msg("tui.trajectory.change_verification.title", fallback="Change verification")
_CHANGE_FILES = msg("tui.trajectory.change_verification.files", fallback="files")
_CHANGE_COUNTS = msg(
    "tui.trajectory.change_verification.counts",
    fallback="{files} · +{created} ~{modified} -{deleted} · cancelled out {net_zero}",
)
_CHANGE_DETAIL_UNAVAILABLE = msg(
    "tui.trajectory.change_verification.unavailable",
    fallback="File detail unavailable; showing recorded mutation summaries.",
)
_CHANGE_DETECTION_TRUNCATED = msg(
    "tui.trajectory.change_verification.truncated",
    fallback="Recorded/observed counts; mutation detection was truncated.",
)
_CHANGE_STATE_VERIFIED = msg("tui.trajectory.change_verification.state.verified", fallback="verified")
_CHANGE_STATE_AFTER = msg("tui.trajectory.change_verification.state.after", fallback="after verify")
_CHANGE_STATE_UNVERIFIED = msg("tui.trajectory.change_verification.state.unverified", fallback="unverified")
_CHANGE_STATE_NET_ZERO = msg("tui.trajectory.change_verification.state.net_zero", fallback="cancelled out")
_TOKEN_USAGE = msg("tui.trajectory.token_usage.title", fallback="Token usage")
_SKILL_USAGE = msg("tui.trajectory.skill_usage.title", fallback="Skill usage")
_SKILL_USAGE_NONE = msg("tui.trajectory.skill_usage.none", fallback="No skills were used.")
_MCP_USAGE = msg("tui.trajectory.mcp_usage.title", fallback="MCP usage")
_MCP_USAGE_NONE = msg("tui.trajectory.mcp_usage.none", fallback="No MCP tools were called.")
_TOOL_USAGE_UNATTRIBUTED = msg("tui.trajectory.tool_usage.unattributed", fallback="(name unavailable)")
_SUBMISSION_LATENCY = msg(
    "tui.trajectory.submission_latency.title",
    fallback="Submission wait (submit → work starts)",
)
_SUBMISSION_INTRO = msg(
    "tui.trajectory.submission_latency.intro",
    fallback="How long each message waited between being sent and the agent starting on it.",
)
_SUBMISSION_NONE = msg("tui.trajectory.submission_latency.none", fallback="No submissions were recorded.")
_SUBMISSION_BECAME_TURN = msg(
    "tui.trajectory.submission_latency.became_turn",
    fallback="started a new turn",
)
_SUBMISSION_INJECTED = msg(
    "tui.trajectory.submission_latency.injected",
    fallback="injected into an ongoing turn",
)
_SUBMISSION_DID_NOT_BECOME = msg(
    "tui.trajectory.submission_latency.did_not_become",
    fallback="never became a turn",
)
_SUBMISSION_STATS = msg(
    "tui.trajectory.submission_latency.stats",
    fallback="{value} sample · median {p50} · p90 {p90} · slowest {maximum}",
    plural_fallback="{value} samples · median {p50} · p90 {p90} · slowest {maximum}",
)
_SUBMISSION_UNRESOLVED = msg(
    "tui.trajectory.submission_latency.unresolved",
    fallback="{count} unresolved sample",
    plural_fallback="{count} unresolved samples",
)
_SUBMISSION_SAMPLE = msg(
    "tui.trajectory.submission_latency.sample",
    fallback="{outcome} · {duration}",
)
_PREPARATION_OUTCOME_HANDOFF = msg("tui.trajectory.preparation_outcome.handoff", fallback="handoff")
_PREPARATION_OUTCOME_COMPLETED = msg("tui.trajectory.preparation_outcome.completed", fallback="completed")
_PREPARATION_OUTCOME_FAILED = msg("tui.trajectory.preparation_outcome.failed", fallback="failed")
_PREPARATION_OUTCOME_INTERRUPTED = msg("tui.trajectory.preparation_outcome.interrupted", fallback="interrupted")
_PREPARATION_OUTCOME_FRESH_TURN = msg("tui.trajectory.preparation_outcome.fresh_turn", fallback="fresh turn")
_PREPARATION_OUTCOME_RETRY_TURN = msg("tui.trajectory.preparation_outcome.retry_turn", fallback="retry turn")
_PREPARATION_OUTCOME_INJECTED = msg("tui.trajectory.preparation_outcome.injected", fallback="injected")
_PREPARATION_OUTCOME_ABANDONED_NO_TARGET = msg(
    "tui.trajectory.preparation_outcome.abandoned_no_target",
    fallback="abandoned: no target",
)
_PREPARATION_OUTCOME_CANCELLED = msg("tui.trajectory.preparation_outcome.cancelled", fallback="cancelled")
_PREPARATION_OUTCOME_TARGET_STALE = msg(
    "tui.trajectory.preparation_outcome.target_stale",
    fallback="target stale",
)
_PREPARATION_OUTCOME_REJECTED = msg("tui.trajectory.preparation_outcome.rejected", fallback="rejected")
_PREPARATION_OUTCOME_IMAGE_REJECTED = msg(
    "tui.trajectory.preparation_outcome.image_rejected",
    fallback="image rejected",
)
_PREPARATION_OUTCOME_NOT_READY = msg("tui.trajectory.preparation_outcome.not_ready", fallback="not ready")
_PREPARATION_OUTCOME_PREPARATION_FAILED = msg(
    "tui.trajectory.preparation_outcome.preparation_failed",
    fallback="preparation failed",
)
_PREPARATION_OUTCOME_CONFLICT = msg("tui.trajectory.preparation_outcome.conflict", fallback="conflict")
_PREPARATION_OUTCOME_OWNER_CHANGED = msg(
    "tui.trajectory.preparation_outcome.owner_changed",
    fallback="owner changed",
)
_PREPARATION_OUTCOME_SUPERSEDED = msg("tui.trajectory.preparation_outcome.superseded", fallback="superseded")
_PREPARATION_OUTCOME_DROPPED = msg("tui.trajectory.preparation_outcome.dropped", fallback="dropped")
_PREPARATION_OUTCOMES = {
    "handoff": _PREPARATION_OUTCOME_HANDOFF,
    "completed": _PREPARATION_OUTCOME_COMPLETED,
    "failed": _PREPARATION_OUTCOME_FAILED,
    "interrupted": _PREPARATION_OUTCOME_INTERRUPTED,
    "fresh_turn": _PREPARATION_OUTCOME_FRESH_TURN,
    "retry_turn": _PREPARATION_OUTCOME_RETRY_TURN,
    "injected": _PREPARATION_OUTCOME_INJECTED,
    "abandoned_no_target": _PREPARATION_OUTCOME_ABANDONED_NO_TARGET,
    "cancelled": _PREPARATION_OUTCOME_CANCELLED,
    "target_stale": _PREPARATION_OUTCOME_TARGET_STALE,
    "rejected": _PREPARATION_OUTCOME_REJECTED,
    "image_rejected": _PREPARATION_OUTCOME_IMAGE_REJECTED,
    "not_ready": _PREPARATION_OUTCOME_NOT_READY,
    "preparation_failed": _PREPARATION_OUTCOME_PREPARATION_FAILED,
    "conflict": _PREPARATION_OUTCOME_CONFLICT,
    "owner_changed": _PREPARATION_OUTCOME_OWNER_CHANGED,
    "superseded": _PREPARATION_OUTCOME_SUPERSEDED,
    "dropped": _PREPARATION_OUTCOME_DROPPED,
}

_SESSION_INFO_FOUR_COLUMN_MIN_WIDTH = 144


def overview_lines(
    look: DashboardLook,
    context: RenderContext,
    analysis: TrajectoryAnalysis,
    *,
    folder: Path | None,
    storage: SessionStorage | None,
    can_open_folder: bool,
) -> list[Text]:
    overview = analysis.overview
    if overview is None:
        return [Text(look.message(UNAVAILABLE.bind()))]
    tier = context.tier
    width = context.width
    interior_width = section_interior_width(width)
    if tier in {ResponsiveTier.WIDE, ResponsiveTier.MID}:
        lower_widths = section_row_widths(width, 3)
        lower_interiors = tuple(section_interior_width(box_width) for box_width in lower_widths)
        kpi = bordered_section_row(
            look,
            (
                (_KPI_TIME, _kpi_time_lines(look, overview, width=lower_interiors[0]), lower_widths[0]),
                (_KPI_SPLIT, _kpi_split_lines(look, overview, width=lower_interiors[1]), lower_widths[1]),
                (_KPI_PARALLEL, _kpi_parallel_lines(look, overview, width=lower_interiors[2]), lower_widths[2]),
            ),
        )
    else:
        lower_widths = (width, width, width)
        lower_interiors = (interior_width, interior_width, interior_width)
        kpi = [
            *section_box(look, _KPI_TIME, _kpi_time_lines(look, overview, width=interior_width), width=width),
            *section_box(look, _KPI_SPLIT, _kpi_split_lines(look, overview, width=interior_width), width=width),
            *section_box(look, _KPI_PARALLEL, _kpi_parallel_lines(look, overview, width=interior_width), width=width),
        ]
    if tier is ResponsiveTier.FLOOR:
        notice = Text(look.message(_CHARTS_TOO_NARROW.bind()), style="dim")
        return [*kpi, *notice.wrap(look.console, width)]
    session_info = section_box(
        look,
        badged_section_title(
            look,
            _SESSION_INFO,
            Precision.UNRESOLVED if analysis.diagnostics.integrity_unresolved else Precision.EXACT,
        ),
        _session_info_lines(
            look,
            analysis,
            width=interior_width,
            columns=4 if interior_width >= _SESSION_INFO_FOUR_COLUMN_MIN_WIDTH else 2,
            folder=folder,
            storage=storage,
            can_open_folder=can_open_folder,
        ),
        width=width,
    )
    token_usage = _token_usage_lines(look, analysis, width=lower_interiors[0])
    skill_usage = _tool_usage_lines(look, analysis.skill_usage, empty=_SKILL_USAGE_NONE, width=lower_interiors[1])
    mcp_usage = _tool_usage_lines(look, analysis.mcp_usage, empty=_MCP_USAGE_NONE, width=lower_interiors[2])
    funnel = _action_funnel_lines(look, analysis, width=lower_interiors[0])
    recovery = _failure_recovery_lines(look, analysis, width=lower_interiors[1])
    changes = _change_verification_lines(look, analysis, width=lower_interiors[2])
    if tier in {ResponsiveTier.WIDE, ResponsiveTier.MID}:
        usage_trio = bordered_section_row(
            look,
            (
                (_TOKEN_USAGE, token_usage, lower_widths[0]),
                (_SKILL_USAGE, skill_usage, lower_widths[1]),
                (_MCP_USAGE, mcp_usage, lower_widths[2]),
            ),
        )
        lower = bordered_section_row(
            look,
            (
                (_ACTION_FUNNEL, funnel, lower_widths[0]),
                (_FAILURE_RECOVERY, recovery, lower_widths[1]),
                (_CHANGE_VERIFICATION, changes, lower_widths[2]),
            ),
        )
    else:
        usage_trio = [
            *section_box(look, _TOKEN_USAGE, token_usage, width=width),
            *section_box(look, _SKILL_USAGE, skill_usage, width=width),
            *section_box(look, _MCP_USAGE, mcp_usage, width=width),
        ]
        lower = [
            *section_box(look, _ACTION_FUNNEL, funnel, width=width),
            *section_box(look, _FAILURE_RECOVERY, recovery, width=width),
            *section_box(look, _CHANGE_VERIFICATION, changes, width=width),
        ]
    first = analysis.turns[0].turn_number if analysis.turns else "—"
    last = analysis.turns[-1].turn_number if analysis.turns else "—"
    waterfall_title = _WATERFALL.bind(first=first or "—", last=last or "—")
    return [
        *session_info,
        *kpi,
        *section_box(
            look,
            waterfall_title,
            _waterfall_lines(look, analysis, width=interior_width),
            width=width,
        ),
        *usage_trio,
        *lower,
        *section_box(
            look,
            _SUBMISSION_LATENCY,
            [
                *_submission_latency_lines(look, analysis, width=interior_width),
                Text(look.message(ELAPSED_SCOPE.bind()), style="dim"),
            ],
            width=width,
        ),
    ]


def _session_info_lines(
    look: DashboardLook,
    analysis: TrajectoryAnalysis,
    *,
    width: int,
    columns: int,
    folder: Path | None,
    storage: SessionStorage | None,
    can_open_folder: bool,
) -> list[Text]:
    """Folder, on-disk footprint, and wall-clock anchors of the session."""
    span = analysis.session_span
    label_style = Style()

    def label(message: MessageDef) -> Text:
        return Text(look.message(message.bind()), style=label_style)

    def size(value: int | None) -> Text:
        return Text("—" if value is None else format_byte_size(value))

    controls = Text()
    if folder is not None:
        controls.append(
            look.message(_SESSION_INFO_COPY_PATH.bind()),
            style=look.semantic_style("primary", "blue", bold=True)
            + Style(underline=True)
            + Style.from_meta({"@click": "copy_session_path"}),
        )
    if can_open_folder:
        if controls:
            controls.append("  ")
        controls.append(
            look.message(_SESSION_INFO_OPEN_FOLDER.bind()),
            style=look.semantic_style("primary", "blue", bold=True)
            + Style(underline=True)
            + Style.from_meta({"@click": "open_session_folder"}),
        )
    folder_label = label(_SESSION_INFO_PATH)
    path_room = width - cell_len(folder_label.plain) - 2
    if controls:
        path_room -= cell_len(controls.plain) + 2
    folder_text = Text(
        _fit_path_tail(surrogate_safe_text(str(folder)), max(1, path_room)) if folder is not None else "—"
    )
    lines = [align_edges(Text.assemble(folder_label, Text("  "), folder_text), controls, width)]
    on_disk = Text("—")
    if storage is not None:
        on_disk = Text.assemble(
            Text(format_byte_size(storage.total_bytes)),
            Text(" · ", style="dim"),
            Text(look.message(_SESSION_INFO_FILES.bind(count=storage.file_count)), style="dim"),
        )
    first_message = _local_clock(span.first_turn_started_at) if span is not None else None
    last_reply = _local_clock(span.last_turn_finished_at) if span is not None else None
    clock_span = _clock_span_ns(span.first_turn_started_at, span.last_turn_finished_at) if span is not None else None
    groups: list[list[tuple[Text, Text]]] = [
        [
            (label(_SESSION_INFO_ON_DISK), on_disk),
            (Text("session.json", style=label_style), size(storage.session_json_bytes if storage else None)),
            (Text("events.jsonl", style=label_style), size(storage.events_bytes if storage else None)),
        ],
        [
            (label(_SESSION_INFO_MUTATIONS), size(storage.mutations_bytes if storage else None)),
            (label(_SESSION_INFO_SNAPSHOTS), size(storage.snapshots_bytes if storage else None)),
            (label(_SESSION_INFO_SUB_AGENTS), size(storage.sub_agents_bytes if storage else None)),
        ],
        [
            (label(_SESSION_INFO_FIRST_MESSAGE), Text(first_message or "—")),
            (label(_SESSION_INFO_LAST_REPLY), Text(last_reply or "—")),
            (label(_SESSION_INFO_SPAN), Text("—" if clock_span is None else format_duration(clock_span))),
        ],
        [
            (label(_SESSION_INFO_TURNS), Text(str(len(analysis.turns)))),
            (label(_SESSION_INFO_EVENTS), Text(f"{analysis.diagnostics.line_count:,}")),
            (label(_SESSION_INFO_RUNTIMES), Text("—" if span is None else str(span.runtime_count))),
        ],
    ]
    lines.extend(_grouped_grid_lines(groups, width=width, columns=columns))
    return lines


def _kpi_time_lines(look: DashboardLook, overview: TrajectoryOverview, *, width: int) -> list[Text]:
    cells = [
        _metric_cell(look, METRIC_ELAPSED, overview.elapsed_ns),
        _metric_cell(look, METRIC_WORK, overview.exclusive_work_ns),
        _metric_cell(look, METRIC_CP_RESPONSE, overview.response_cp_ns),
        _metric_cell(look, METRIC_CP_COMPUTE, overview.compute_cp_ns),
        _metric_cell(look, METRIC_USAGE, overview.usage_tokens, tokens=True),
        _coverage_cell(look, overview),
    ]
    return [align_edges(left, right, width) for left, right in cells]


def _kpi_split_lines(look: DashboardLook, overview: TrajectoryOverview, *, width: int) -> list[Text]:
    wall_percentages = _partition_percentages(overview)
    cells = [
        _percentage_cell(look, label, overview.wall_time_ns[bucket], wall_percentages[bucket], bucket=bucket)
        for bucket, label in (
            (WallBucket.MODEL, BUCKET_MODEL),
            (WallBucket.TOOLS, BUCKET_TOOLS),
            (WallBucket.WAIT, BUCKET_WAIT),
            (WallBucket.IDLE, BUCKET_IDLE),
        )
    ]
    return [align_edges(left, right, width) for left, right in cells]


def _kpi_parallel_lines(look: DashboardLook, overview: TrajectoryOverview, *, width: int) -> list[Text]:
    lines = [
        align_edges(*_metric_cell(look, METRIC_PARALLELISM, overview.parallelism), width),
        align_edges(*_metric_cell(look, METRIC_OVERLAP, overview.overlap_gain_ns), width),
        Text(look.message(_UTILIZATION.bind()), style="dim"),
    ]
    for bucket, label in ((WallBucket.MODEL, BUCKET_MODEL), (WallBucket.TOOLS, BUCKET_TOOLS)):
        lines.append(align_edges(*_utilization_cell(look, label, overview.utilization[bucket], bucket), width))
    return lines


def _coverage_cell(look: DashboardLook, overview: TrajectoryOverview) -> tuple[Text, Text]:
    metrics = _overview_metrics(overview)
    total = max(1, len(metrics))
    exact = sum(metric.precision is Precision.EXACT for metric in metrics) * 100 / total
    estimated = sum(metric.precision is Precision.ESTIMATED for metric in metrics) * 100 / total
    missing = sum(metric.precision is Precision.MISSING for metric in metrics) * 100 / total
    unresolved = 100.0 - exact - estimated - missing
    return (
        Text(look.message(_COVERAGE.bind())),
        Text.assemble(
            coverage_bar(
                exact,
                estimated,
                missing,
                unresolved,
                width=5,
                exact_style=precision_style(look, Precision.EXACT),
                estimated_style=precision_style(look, Precision.ESTIMATED),
                missing_style=precision_style(look, Precision.MISSING),
                unresolved_style=precision_style(look, Precision.UNRESOLVED),
            ),
            Text(" "),
            Text(
                look.message(
                    _COVERAGE_SHARES.bind(
                        exact=f"{exact:.0f}",
                        estimated=f"{estimated:.0f}",
                        missing=f"{missing:.0f}",
                        unresolved=f"{unresolved:.0f}",
                    )
                )
            ),
        ),
    )


def _waterfall_lines(look: DashboardLook, analysis: TrajectoryAnalysis, *, width: int) -> list[Text]:
    if not analysis.turns:
        return [Text(look.message(NO_TURNS.bind()))]
    chart_width = max(12, width - 10)
    # Lane order doubles as the tie-break when two buckets cover a cell
    # equally; tools and waits outrank the model so short calls stay visible.
    lane_specs = (
        (WallBucket.TOOLS, BUCKET_TOOLS, "▬"),
        (WallBucket.WAIT, BUCKET_WAIT, "▒"),
        (WallBucket.MODEL, BUCKET_MODEL, "█"),
        (WallBucket.IDLE, BUCKET_IDLE, "·"),
    )
    turns: list[tuple[int, dict[WallBucket, list[tuple[int, int]]]]] = []
    for turn in analysis.turns:
        span = max(0, turn.axis_end_ns - turn.axis_start_ns)
        intervals: dict[WallBucket, list[tuple[int, int]]] = {bucket: [] for bucket, _, _ in lane_specs}
        for item in turn.slices:
            if item.wall_bucket in intervals and item.end_ns > turn.axis_start_ns and item.start_ns < turn.axis_end_ns:
                intervals[item.wall_bucket].append(
                    (
                        max(0, item.start_ns - turn.axis_start_ns),
                        min(span, item.end_ns - turn.axis_start_ns),
                    )
                )
        turns.append((span, intervals))
    lanes = waterfall_lanes(
        turns,
        width=chart_width,
        lanes=[(bucket, glyph, _bucket_style(look, bucket)) for bucket, _, glyph in lane_specs],
    )
    lines = [
        Text.assemble(
            Text(fit_cells(look.message(label.bind()), 8)),
            Text(" "),
            lanes[bucket],
        )
        for bucket, label, _ in (
            (WallBucket.MODEL, BUCKET_MODEL, "█"),
            (WallBucket.TOOLS, BUCKET_TOOLS, "▬"),
            (WallBucket.WAIT, BUCKET_WAIT, "▒"),
            (WallBucket.IDLE, BUCKET_IDLE, "·"),
        )
    ]
    # The axis is cumulative turn time (turn spans concatenated, gaps
    # between turns excluded) — the same scale the lanes are drawn on.
    ruler = time_ruler(sum(span for span, _ in turns), width=chart_width)
    ruler.stylize("dim")
    lines.append(
        Text.assemble(
            Text(fit_cells(look.message(TIME_RULER.bind()), 8)),
            Text(" "),
            ruler,
        )
    )
    return lines


def _action_funnel_lines(look: DashboardLook, analysis: TrajectoryAnalysis, *, width: int) -> list[Text]:
    validation = analysis.validation
    if validation is None:
        return []
    metrics = (
        (_ACTION_SEARCH, validation.funnel.search, ActionClass.SEARCH),
        (_ACTION_READ, validation.funnel.read, ActionClass.READ),
        (_ACTION_EDIT, validation.funnel.edit, ActionClass.EDIT),
        (_ACTION_VERIFY, validation.funnel.verify, ActionClass.VERIFY),
    )
    count_width = max(cell_len(_format_count(metric)) for _, metric, _ in metrics)
    badge_width = max(cell_len(precision_badge(look, metric.precision).plain) for _, metric, _ in metrics)
    label_reserve = min(8, max(cell_len(look.message(label.bind())) for label, _, _ in metrics))
    bar_width = max(1, min(12, width - label_reserve - count_width - badge_width - 3))
    maximum = max((int(metric.value) for _, metric, _ in metrics if metric.value is not None), default=0)
    lines: list[Text] = []
    for label, metric, action in metrics:
        count = int(metric.value) if metric.value is not None else None
        bar_length = 0 if maximum == 0 or count is None else max(1, round(count / maximum * bar_width))
        lines.append(
            align_edges(
                Text(look.message(label.bind())),
                Text.assemble(
                    fit_text_right(Text(_format_count(metric)), count_width),
                    Text(" "),
                    Text("▬" * bar_length, style=_action_style(look, action)),
                    Text(" " * (bar_width - bar_length)),
                    Text(" "),
                    fit_text_right(precision_badge(look, metric.precision), badge_width),
                ),
                width,
            )
        )
    return lines


def _failure_recovery_lines(look: DashboardLook, analysis: TrajectoryAnalysis, *, width: int) -> list[Text]:
    validation = analysis.validation
    if validation is None:
        return []
    rows = (
        (
            look.message(_TOOL_FAILURES.bind()),
            Text(f"{_format_count(validation.tool_failure_count)}/{_format_count(validation.tool_count)}"),
            validation.tool_failure_count,
        ),
        (
            look.message(_MEDIAN_RECOVERY.bind()),
            Text(metric_value(validation.failure_recovery_median_ns)),
            validation.failure_recovery_median_ns,
        ),
        (
            look.message(_RETRY_AMPLIFICATION.bind()),
            Text(
                look.message(
                    _TOKEN_COUNT.bind(tokens=_format_count(validation.retry_amplification_tokens, signed=True))
                )
            ),
            validation.retry_amplification_tokens,
        ),
        (
            look.message(_REPEATED_SIGNATURES.bind()),
            Text(_format_count(validation.repeated_failure_signature_count)),
            validation.repeated_failure_signature_count,
        ),
    )
    value_width = max(cell_len(value.plain) for _, value, _ in rows)
    badge_width = max(cell_len(precision_badge(look, metric.precision).plain) for _, _, metric in rows)
    return [
        align_edges(
            Text(label),
            Text.assemble(
                fit_text_right(value, value_width),
                Text(" "),
                fit_text_right(precision_badge(look, metric.precision), badge_width),
            ),
            width,
        )
        for label, value, metric in rows
    ]


def _token_usage_lines(look: DashboardLook, analysis: TrajectoryAnalysis, *, width: int) -> list[Text]:
    usage = analysis.token_usage
    if usage is None:
        return []
    cache_hit = cache_hit_metric(usage)
    rows = [
        (
            look.message(label.bind()),
            Text(format_tokens(usage.buckets[bucket].value)),
            usage.buckets[bucket],
        )
        for bucket, label in (
            (UsageBucket.INPUT, TOKEN_INPUT),
            (UsageBucket.OUTPUT, TOKEN_OUTPUT),
            (UsageBucket.REASONING, TOKEN_REASONING),
            (UsageBucket.CACHE_READ, TOKEN_CACHE_READ),
        )
    ]
    rows.append(
        (
            look.message(TOKEN_CACHE_HIT.bind()),
            Text("—" if cache_hit.value is None else f"{cache_hit.value}%"),
            cache_hit,
        )
    )
    value_width = max(cell_len(value.plain) for _, value, _ in rows)
    badge_width = max(cell_len(precision_badge(look, metric.precision).plain) for _, _, metric in rows)
    return [
        align_edges(
            Text(label),
            Text.assemble(
                fit_text_right(value, value_width),
                Text(" "),
                fit_text_right(precision_badge(look, metric.precision), badge_width),
            ),
            width,
        )
        for label, value, metric in rows
    ]


def _tool_usage_lines(
    look: DashboardLook, panel: ToolUsagePanel | None, *, empty: MessageDef, width: int
) -> list[Text]:
    if panel is None:
        return []
    if not panel.total:
        return [Text(look.message(empty.bind()), style="dim")]
    rows = [(row.name, row.count) for row in panel.rows]
    if panel.unattributed:
        rows.append((look.message(_TOOL_USAGE_UNATTRIBUTED.bind()), panel.unattributed))
        rows.sort(key=lambda row: -row[1])
    visible = rows[:5]
    count_width = max(cell_len(f"{count:,}") for _, count in visible)
    badge = precision_badge(look, panel.precision)
    lines = [
        align_edges(
            Text(name),
            Text.assemble(
                fit_text_right(Text(f"{count:,}"), count_width),
                Text(" "),
                badge.copy(),
            ),
            width,
        )
        for name, count in visible
    ]
    if len(rows) > len(visible):
        lines.append(
            Text(
                look.message(TOOL_USAGE_MORE.bind(value=len(rows) - len(visible))),
                style="dim",
            )
        )
    return lines


def _change_verification_lines(look: DashboardLook, analysis: TrajectoryAnalysis, *, width: int) -> list[Text]:
    change = analysis.change_verification
    if change is None:
        return []
    lines: list[Text] = []
    counts = look.message(
        _CHANGE_COUNTS.bind(
            files=_format_count(change.files_touched),
            created=_format_count(change.created),
            modified=_format_count(change.modified),
            deleted=_format_count(change.deleted),
            net_zero=_format_count(change.net_zero),
        )
    )
    # The counts fold provenance and skip evidence into their precision;
    # one badge for the worst of them keeps that visible in the line.
    precision_order = (Precision.EXACT, Precision.ESTIMATED, Precision.MISSING, Precision.UNRESOLVED)
    counts_precision = max(
        (
            metric.precision
            for metric in (
                change.files_touched,
                change.created,
                change.modified,
                change.deleted,
                change.net_zero,
            )
        ),
        key=precision_order.index,
    )
    lines.append(
        _align_edges_badged(
            Text(look.message(_CHANGE_FILES.bind())),
            Text(counts),
            precision_badge(look, counts_precision),
            width,
        )
    )
    if not change.detail_available:
        lines.append(Text(look.message(_CHANGE_DETAIL_UNAVAILABLE.bind()), style="dim"))
    else:
        states = {
            ChangeVerificationState.VERIFIED: _CHANGE_STATE_VERIFIED,
            ChangeVerificationState.AFTER_VERIFY: _CHANGE_STATE_AFTER,
            ChangeVerificationState.UNVERIFIED: _CHANGE_STATE_UNVERIFIED,
            ChangeVerificationState.NET_ZERO: _CHANGE_STATE_NET_ZERO,
        }
        for row in change.rows:
            style = (
                look.semantic_style("success", "green")
                if row.state is ChangeVerificationState.VERIFIED
                else look.semantic_style("warning", "yellow")
            )
            lines.append(
                _align_path_edges_badged(
                    surrogate_safe_text(row.path),
                    style,
                    Text(look.message(states[row.state].bind()), style=style),
                    precision_badge(look, row.precision),
                    width,
                )
            )
    if change.detection_truncated:
        lines.append(Text(look.message(_CHANGE_DETECTION_TRUNCATED.bind()), style="dim"))
    return lines


def _submission_latency_lines(look: DashboardLook, analysis: TrajectoryAnalysis, *, width: int) -> list[Text]:
    submission = analysis.submission_latency
    if submission is None:
        return []
    bucket_labels = {
        SubmissionLatencyBucket.BECAME_TURN: _SUBMISSION_BECAME_TURN,
        SubmissionLatencyBucket.INJECTED: _SUBMISSION_INJECTED,
        SubmissionLatencyBucket.DID_NOT_BECOME_TURN: _SUBMISSION_DID_NOT_BECOME,
    }
    lines: list[Text] = [Text(look.message(_SUBMISSION_INTRO.bind()), style="dim")]
    rendered_any = False
    for stats in submission.buckets:
        if not stats.sample_count and not stats.unresolved_count and not stats.samples:
            continue
        rendered_any = True
        label = look.message(bucket_labels[stats.bucket].bind())
        aggregate_precision, _ = derived_metric_precision(stats.p50_ns, stats.p90_ns, stats.max_ns)
        lines.append(
            _align_edges_badged(
                Text(label),
                Text(
                    look.message(
                        _SUBMISSION_STATS.bind(
                            count=stats.sample_count,
                            value=stats.sample_count,
                            p50=metric_value(stats.p50_ns),
                            p90=metric_value(stats.p90_ns),
                            maximum=metric_value(stats.max_ns),
                        )
                    )
                ),
                precision_badge(look, aggregate_precision),
                width,
            )
        )
        if stats.unresolved_count:
            lines.append(
                align_edges(
                    Text(),
                    Text(
                        look.message(_SUBMISSION_UNRESOLVED.bind(count=stats.unresolved_count)),
                        style=look.semantic_style("warning", "yellow"),
                    ),
                    width,
                )
            )
        lines.extend(
            align_edges(
                Text(_submission_sample_label(look, analysis, sample)),
                Text.assemble(
                    Text(
                        look.message(
                            _SUBMISSION_SAMPLE.bind(
                                outcome=_preparation_outcome(look, sample.outcome),
                                duration=metric_value(sample.duration_ns),
                            )
                        )
                    ),
                    Text(" "),
                    precision_badge(look, sample.duration_ns.precision),
                ),
                width,
            )
            for sample in stats.samples
        )
    if not rendered_any:
        lines.append(Text(look.message(_SUBMISSION_NONE.bind()), style="dim"))
    return lines


def _submission_sample_label(look: DashboardLook, analysis: TrajectoryAnalysis, sample: SubmissionLatencySample) -> str:
    turn = analysis.turn(sample.turn_id) if sample.turn_id is not None else None
    if turn is not None and turn.turn_number is not None:
        return look.message(TURN_LABEL.bind(turn=turn.turn_number))
    return callsite(sample.scope_operation_id)


def _preparation_outcome(look: DashboardLook, outcome: str | None) -> str:
    if outcome is None:
        return "—"
    definition = _PREPARATION_OUTCOMES.get(outcome)
    return look.message(definition.bind()) if definition is not None else surrogate_safe_text(outcome)


def _action_style(look: DashboardLook, action: ActionClass) -> Style:
    if action is ActionClass.EDIT:
        return look.semantic_style("warning", "yellow", bold=True)
    if action is ActionClass.VERIFY:
        return look.semantic_style("success", "green", bold=True)
    if action is ActionClass.SEARCH:
        return look.semantic_style("primary", "blue", bold=True)
    return look.semantic_style("secondary", "cyan", bold=True)


def _metric_cell(look: DashboardLook, label: MessageDef, metric: Metric, *, tokens: bool = False) -> tuple[Text, Text]:
    value = format_tokens(metric.value) if tokens else metric_value(metric)
    return (
        Text(look.message(label.bind())),
        Text.assemble(Text(value), Text(" "), precision_badge(look, metric.precision)),
    )


def _percentage_cell(
    look: DashboardLook,
    label: MessageDef,
    metric: Metric,
    value: float | None,
    *,
    bucket: WallBucket,
) -> tuple[Text, Text]:
    return (
        Text(look.message(label.bind()), style=_bucket_style(look, bucket)),
        Text.assemble(
            percentage_meter(value, width=8, style=_bucket_style(look, bucket)),
            Text(" "),
            precision_badge(look, metric.precision),
        ),
    )


def _utilization_cell(look: DashboardLook, label: MessageDef, metric: Metric, bucket: WallBucket) -> tuple[Text, Text]:
    value = None if metric.value is None else float(metric.value) * 100
    return (
        Text(look.message(label.bind()), style=_bucket_style(look, bucket)),
        Text.assemble(
            percentage_meter(value, width=8, style=_bucket_style(look, bucket)),
            Text(" "),
            precision_badge(look, metric.precision),
        ),
    )


def _bucket_style(look: DashboardLook, bucket: WallBucket) -> Style:
    if bucket is WallBucket.MODEL:
        return look.semantic_style("primary", "blue")
    if bucket is WallBucket.TOOLS:
        return look.semantic_style("warning", "yellow")
    if bucket is WallBucket.WAIT:
        return look.semantic_style("accent", "magenta")
    return Style(dim=True)


def _parse_clock(value: str | None) -> datetime | None:
    """Parse a producer RFC 3339 stamp; naive or malformed stamps are unusable."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _local_clock(value: str | None) -> str | None:
    """Render a producer RFC 3339 UTC stamp on the viewer's wall clock."""
    parsed = _parse_clock(value)
    return None if parsed is None else parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _clock_span_ns(first: str | None, last: str | None) -> int | None:
    start = _parse_clock(first)
    end = _parse_clock(last)
    if start is None or end is None or end < start:
        return None
    return int((end - start).total_seconds() * 1_000_000_000)


def _fit_path_tail(value: str, width: int) -> str:
    """Keep the end of a path visible when it must be cropped."""
    usable = max(1, width)
    if cell_len(value) <= usable:
        return value
    tail = value
    while tail and cell_len(tail) > usable - 1:
        tail = tail[1:]
    return f"…{tail}"


def _fit_path_middle(value: str, width: int) -> str:
    """Crop a file path in the middle, preserving its full basename first."""
    usable = max(0, width)
    if usable == 0:
        return ""
    if cell_len(value) <= usable:
        return value
    if usable == 1:
        return "…"

    separator_index = max(value.rfind("/"), value.rfind("\\"))
    if separator_index >= 0:
        suffix = value[separator_index:]
        if cell_len(suffix) + 1 <= usable:
            head_room = usable - cell_len(suffix) - 1
            head = Text(value[:separator_index])
            head.truncate(head_room, overflow="crop")
            return f"{head.plain}…{suffix}"

    tail = value[separator_index + 1 :]
    while tail and cell_len(tail) > usable - 1:
        tail = tail[1:]
    return f"…{tail}"


def _align_edges_badged(left: Text, value: Text, badge: Text, width: int) -> Text:
    """Two-edge alignment whose right side ends in a badge that survives.

    ``align_edges`` truncates the right side from its tail, which would
    drop an appended precision badge exactly in the narrow columns where
    the value is most compressed; the value is fitted first so the badge
    always stays visible.
    """
    badge_width = cell_len(badge.plain)
    fitted_value = value.copy()
    fitted_value.truncate(max(0, width - badge_width - 1), overflow="ellipsis")
    return align_edges(left, Text.assemble(fitted_value, Text(" "), badge), width)


def _align_path_edges_badged(path: str, path_style: Style, value: Text, badge: Text, width: int) -> Text:
    """Align a path while reserving its available cells for middle cropping."""
    usable = max(0, width)
    badge_width = cell_len(badge.plain)
    fitted_value = value.copy()
    fitted_value.truncate(max(0, usable - badge_width - 1), overflow="ellipsis")
    right = Text.assemble(fitted_value, Text(" "), badge)
    right_width = cell_len(right.plain)
    separator_width = int(bool(path and right.plain and right_width < usable))
    path_room = max(0, usable - right_width - separator_width)
    fitted_path = Text(_fit_path_middle(path, path_room), style=path_style)
    return align_edges(fitted_path, right, usable)


def _grouped_grid_lines(
    groups: list[list[tuple[Text, Text]]],
    *,
    width: int,
    columns: int,
    gap: int = 3,
) -> list[Text]:
    """Lay each semantic group down one column before starting the next band."""
    columns = max(1, columns)
    usable = max(columns, width - gap * (columns - 1))
    base = usable // columns
    widths = [base] * (columns - 1) + [usable - base * (columns - 1)]
    lines: list[Text] = []
    for group_start in range(0, len(groups), columns):
        band = groups[group_start : group_start + columns]
        if lines:
            lines.append(Text())
        for row_index in range(max((len(group) for group in band), default=0)):
            parts: list[Text] = []
            for column_index, column_width in enumerate(widths):
                if column_index:
                    parts.append(Text(" " * gap))
                if column_index < len(band) and row_index < len(band[column_index]):
                    label, value = band[column_index][row_index]
                else:
                    label, value = Text(), Text()
                parts.append(align_edges(label, value, column_width))
            lines.append(Text.assemble(*parts))
    return lines


def _partition_percentages(overview: TrajectoryOverview) -> dict[WallBucket, float | None]:
    elapsed = overview.elapsed_ns.value
    values = [overview.wall_time_ns[bucket].value for bucket in WallBucket]
    if elapsed is None or float(elapsed) <= 0 or any(value is None for value in values):
        return dict.fromkeys(WallBucket)
    numeric_values = [value for value in values if value is not None]
    raw_tenths = [float(value) / float(elapsed) * 1000 for value in numeric_values]
    tenths = [int(value) for value in raw_tenths]
    for index in sorted(
        range(len(tenths)),
        key=lambda item: raw_tenths[item] - tenths[item],
        reverse=True,
    )[: 1000 - sum(tenths)]:
        tenths[index] += 1
    return {bucket: tenths[index] / 10 for index, bucket in enumerate(WallBucket)}


def _overview_metrics(overview: TrajectoryOverview | None) -> list[Metric]:
    if overview is None:
        return []
    return [
        overview.elapsed_ns,
        overview.response_cp_ns,
        overview.compute_cp_ns,
        overview.exclusive_work_ns,
        overview.parallelism,
        overview.overlap_gain_ns,
        overview.usage_tokens,
        *(overview.wall_time_ns[bucket] for bucket in WallBucket),
        *(overview.utilization[bucket] for bucket in (WallBucket.MODEL, WallBucket.TOOLS)),
    ]


def _format_count(metric: Metric, *, signed: bool = False) -> str:
    if metric.value is None:
        return "—"
    value = int(metric.value)
    return f"{value:+,}" if signed else f"{value:,}"
