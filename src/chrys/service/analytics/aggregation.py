# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Three-stage trajectory aggregation for the P0 dashboard.

The loader performs one physical JSONL scan into compact, reversible facts.
Resolution happens only after the scan reaches EOF (or the complete live-tail
prefix), so rollback projection and forward references are settled before any
metric is published.

``_resolve`` wires the stages: context evidence (``_context_evidence``) and
physical turn attempts (``_turns``) first, then actions and change
verification (``_actions``). Session totals and retry folding stay here:
retry amplification feeds validation, and the folded logical turns become the
published turns. The insight panels (``_insights``) read the physical attempts
and use the folded turns only to count skill use per logical turn.
"""

from __future__ import annotations

from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from io import BufferedReader
from itertools import pairwise
from os import fstat
from pathlib import Path
from threading import Event
from typing import Final, cast

import chrys.service.analytics._turns as _turns
from chrys.foundation.config.settings import DEFAULT_TRAJECTORY_VERIFY_COMMANDS
from chrys.foundation.platform.key_files import read_owner_only_key
from chrys.foundation.trajectory.fingerprint import FINGERPRINT_KEY_BYTES
from chrys.foundation.trajectory.keys import TRAJECTORY_KEY_FILE_NAME
from chrys.foundation.trajectory.sequences import SequenceRange
from chrys.service.analytics._actions import (
    _action_count_metrics,
    _degrade_change_verification,
    _degrade_validation_metrics,
    _resolve_actions,
    _resolve_change_verification,
    _validation_metrics,
)
from chrys.service.analytics._context_evidence import _revision_memberships, _RevisionResolution
from chrys.service.analytics._facts import (
    _UNREPORTED_FAILURE_USAGE_REASON,
    _active,
    _closed_sequence_union,
    _Endpoint,
    _Intermediate,
    _lifecycle_cut,
    _LifecycleCut,
    _Membership,
    _Node,
    _payload_str,
)
from chrys.service.analytics._insights import (
    _context_carrying_load,
    _degrade_insights,
    _degrade_tool_usage_panel,
    _insights_analysis,
    _tool_usage_panel,
)
from chrys.service.analytics._metric_ops import (
    _cap_session_metric,
    _merge_reasons,
    _percentile_metric,
    _sum_metrics,
    _sum_optional_bucket_metrics,
)
from chrys.service.analytics._session_projection import _lazy_session_projection, _SessionProjectionCache
from chrys.service.analytics._timeline import (
    _exclusively_inactive,
    _merge_projection_memberships,
    _node_owner_membership,
    _timeline_diagnostics,
    _turn_start_membership,
)
from chrys.service.analytics._workflows import include_workflow_overview, workflow_runs
from chrys.service.analytics.findings import RetriedExchange, SequenceSpan, evaluate_findings
from chrys.service.analytics.model import (
    FLOW_TERMINAL_INDEX,
    ActionClass,
    ActionOperation,
    AnalysisAvailability,
    Metric,
    Precision,
    SequenceRangeDiagnostic,
    SessionCounterSamples,
    SessionSpan,
    SubmissionLatencyBucket,
    SubmissionLatencyOverview,
    SubmissionLatencySample,
    SubmissionLatencyStats,
    TimelineOperation,
    TimelineOperationDiagnostic,
    TimeSlice,
    TokenUsage,
    TrajectoryAnalysis,
    TrajectoryDiagnostics,
    TrajectoryOverview,
    TurnAnalysis,
    TurnAttemptRef,
    TurnFlow,
    UsageBucket,
    WallBucket,
    least_precision,
)
from chrys.service.analytics.reader import (
    ScanCursor,
    TrajectoryScanCancelled,
    _PrefixHasher,
    scan_open_trajectory_batch,
    verify_prefix,
)
from chrys.service.analytics.reader import raise_if_cancelled as _check_cancelled
from chrys.service.analytics.session_paths import session_directory_for_events
from chrys.service.trajectory.preparation import PreparationOutcome

MAX_RESIDENT_MEMORY_BYTES: Final = 200 * 1024 * 1024
"""P0 acceptance ceiling for a 200 MiB input fixture."""

_PREFIX_REVERIFY_FLOOR_BYTES: Final = 1 << 20
"""Smallest growth that triggers a full prefix replay between doublings."""

_PREFIX_PROBE_BYTES: Final = 1 << 12
"""Consumed-suffix window reread before trusting growth as an append."""


def _read_prefix_probe(handle: BufferedReader, byte_offset: int) -> bytes:
    length = min(byte_offset, _PREFIX_PROBE_BYTES)
    handle.seek(byte_offset - length)
    return handle.read(length)


def _read_installed_fingerprint_key(path: Path) -> bytes | None:
    session_dir = session_directory_for_events(path)
    if session_dir is None:
        return None
    from chrys.foundation.platform import get_platform

    # The recorder keeps the key in the platform config directory. Beside the
    # session root covers the default layout (the root IS the config
    # directory) and trees copied along with their key; the config directory
    # is the fallback for a custom session root, whose sessions live
    # elsewhere while the key stays put.
    candidates = [session_dir.parent.parent / TRAJECTORY_KEY_FILE_NAME]
    config_key_path = get_platform().config_dir / TRAJECTORY_KEY_FILE_NAME
    if config_key_path not in candidates:
        candidates.append(config_key_path)
    for key_path in candidates:
        try:
            key = read_owner_only_key(key_path, max_bytes=FINGERPRINT_KEY_BYTES + 1)
        except OSError:
            continue
        if len(key) == FINGERPRINT_KEY_BYTES:
            return key
    return None


class TrajectoryAnalyzer:
    """Path-identity cache with append-only live-tail scans and dirty-turn resolves."""

    def __init__(
        self,
        *,
        fingerprint_key: bytes | None = None,
        verify_commands: str = DEFAULT_TRAJECTORY_VERIFY_COMMANDS,
    ) -> None:
        self._path: Path | None = None
        self._identity: tuple[int, int] | None = None
        self._size = 0
        self._mtime_ns = 0
        self._prefix_digest = b""
        self._prefix_hasher: _PrefixHasher | None = None
        self._prefix_probe = b""
        self._verified_offset = 0
        self._cursor = ScanCursor()
        self._intermediate: _Intermediate | None = None
        self._resolution_cache = _turns._ResolutionCache()
        self._analysis: TrajectoryAnalysis | None = None
        self._generation = 0
        self._fingerprint_key = fingerprint_key
        self._verify_commands = verify_commands
        self._session_projection_cache = _SessionProjectionCache()

    def load(self, path: Path, *, cancel_event: Event | None = None) -> TrajectoryAnalysis:
        """Perform the one initial physical scan and resolve after EOF."""
        self._path = path
        self._generation += 1
        try:
            handle = path.open("rb")
        except FileNotFoundError:
            self._reset_cache()
            self._path = path
            self._analysis = TrajectoryAnalysis(
                availability=AnalysisAvailability.UNAVAILABLE,
                path=path,
                generation=self._generation,
            )
            return self._analysis
        except OSError as exc:
            return self._read_error(path, exc)
        with handle:
            return self._load_opened(path, handle, cancel_event=cancel_event)

    def _load_opened(
        self,
        path: Path,
        handle: BufferedReader,
        *,
        cancel_event: Event | None,
    ) -> TrajectoryAnalysis:
        intermediate = _Intermediate(path)
        cursor = ScanCursor()
        try:
            batch = scan_open_trajectory_batch(
                handle,
                intermediate.consume,
                cursor=cursor,
                cancel_event=cancel_event,
            )
        except TrajectoryScanCancelled:
            self._reset_cache()
            raise
        except OSError as exc:
            return self._read_error(path, exc)
        if cancel_event is not None and cancel_event.is_set():
            self._reset_cache()
            raise TrajectoryScanCancelled
        intermediate.absorb_batch(batch)
        stat = fstat(handle.fileno())
        self._cursor = cursor
        self._intermediate = intermediate
        self._identity = (stat.st_dev, stat.st_ino)
        self._size = cursor.byte_offset + batch.torn_tail_bytes
        self._mtime_ns = stat.st_mtime_ns
        self._prefix_digest = batch.prefix_digest
        self._prefix_hasher = batch.prefix_hasher
        self._prefix_probe = _read_prefix_probe(handle, cursor.byte_offset)
        self._verified_offset = cursor.byte_offset
        self._resolution_cache = _turns._ResolutionCache()
        try:
            self._analysis = _resolve(
                intermediate,
                generation=self._generation,
                resolution_cache=self._resolution_cache,
                cancel_event=cancel_event,
                fingerprint_key=self._fingerprint_key or _read_installed_fingerprint_key(path),
                verify_commands=self._verify_commands,
                session_cache=self._session_projection_cache,
            )
        except TrajectoryScanCancelled:
            self._reset_cache()
            raise
        intermediate.mark_resolved()
        return self._analysis

    def refresh(self, *, cancel_event: Event | None = None) -> TrajectoryAnalysis:
        """Consume one append batch; replacement or truncation performs a fresh load."""
        path = self._path
        if path is None:
            raise RuntimeError("TrajectoryAnalyzer.refresh() requires load() first")
        try:
            handle = path.open("rb")
        except OSError:
            return self.load(path, cancel_event=cancel_event)
        with handle:
            stat = fstat(handle.fileno())
            identity = (stat.st_dev, stat.st_ino)
            intermediate = self._intermediate
            self._generation += 1
            if intermediate is None or identity != self._identity or stat.st_size < self._cursor.byte_offset:
                return self._load_opened(path, handle, cancel_event=cancel_event)
            if stat.st_size == self._size:
                # An unchanged log still goes stale when the companion
                # session document moved under it: carriers, commands, and
                # mutation detail all fold from that document.
                if (
                    stat.st_mtime_ns == self._mtime_ns
                    and self._analysis is not None
                    and not self._session_projection_cache.changed(path)
                ):
                    self._generation -= 1
                    return self._analysis
                return self._load_opened(path, handle, cancel_event=cancel_event)
            retained = self._prefix_hasher
            appended_since_verify = self._cursor.byte_offset - self._verified_offset
            # Replaying the whole consumed prefix on every append batch is
            # quadratic against a log polled twice a second, so the retained
            # digest state continues across batches and the replay re-verifies
            # only once the log outgrows the last verified length again:
            # amortized linear, still fail-closed at every doubling and load.
            if retained is not None and appended_since_verify < max(
                self._verified_offset, _PREFIX_REVERIFY_FLOOR_BYTES
            ):
                # Growth alone is not append provenance: a same-inode
                # replacement also grows the file. Rereading the consumed
                # suffix catches a replaced prefix at the boundary before any
                # stale state is combined with the new bytes; a replacement
                # that forges the probe window still fails the next replay.
                if _read_prefix_probe(handle, self._cursor.byte_offset) != self._prefix_probe:
                    return self._load_opened(path, handle, cancel_event=cancel_event)
                prefix_hasher = retained
            else:
                try:
                    prefix_hasher = verify_prefix(
                        handle,
                        length=self._cursor.byte_offset,
                        expected_digest=self._prefix_digest,
                        cancel_event=cancel_event,
                    )
                except TrajectoryScanCancelled:
                    self._reset_cache()
                    raise
                if prefix_hasher is None:
                    return self._load_opened(path, handle, cancel_event=cancel_event)
                self._verified_offset = self._cursor.byte_offset
            try:
                batch = scan_open_trajectory_batch(
                    handle,
                    intermediate.consume,
                    cursor=self._cursor,
                    cancel_event=cancel_event,
                    prefix_hasher=prefix_hasher,
                )
            except TrajectoryScanCancelled:
                self._reset_cache()
                raise
            except OSError as exc:
                return self._read_error(path, exc)
            if cancel_event is not None and cancel_event.is_set():
                self._reset_cache()
                raise TrajectoryScanCancelled
            intermediate.absorb_batch(batch)
            final_stat = fstat(handle.fileno())
            self._size = self._cursor.byte_offset + batch.torn_tail_bytes
            self._mtime_ns = final_stat.st_mtime_ns
            self._prefix_digest = batch.prefix_digest
            self._prefix_hasher = batch.prefix_hasher
            self._prefix_probe = _read_prefix_probe(handle, self._cursor.byte_offset)
            try:
                self._analysis = _resolve(
                    intermediate,
                    generation=self._generation,
                    resolution_cache=self._resolution_cache,
                    previous=self._analysis,
                    cancel_event=cancel_event,
                    fingerprint_key=self._fingerprint_key or _read_installed_fingerprint_key(path),
                    verify_commands=self._verify_commands,
                    session_cache=self._session_projection_cache,
                )
            except TrajectoryScanCancelled:
                self._reset_cache()
                raise
            intermediate.mark_resolved()
            return self._analysis

    def release(self) -> None:
        """Release all generation-bound state owned by the analyzer."""
        self._path = None
        self._analysis = None
        self._session_projection_cache = _SessionProjectionCache()
        self._reset_cache()

    def counter_samples(self) -> SessionCounterSamples:
        """Project per-turn counter samples from the retained scan facts.

        Computed transiently for export; retaining timestamped samples on every
        TurnAnalysis would scale the resident footprint with session length.
        """
        intermediate = self._intermediate
        if intermediate is None:
            return SessionCounterSamples(usage_by_turn={}, context_by_turn={})
        inactive_ranges = _closed_sequence_union(intermediate.rollback_ranges)
        return SessionCounterSamples(
            usage_by_turn=_turns._usage_samples_by_turn(intermediate, inactive_ranges),
            context_by_turn=_turns._context_samples_by_turn(intermediate, inactive_ranges),
        )

    def _read_error(self, path: Path, exc: OSError) -> TrajectoryAnalysis:
        self._reset_cache()
        self._path = path
        self._analysis = TrajectoryAnalysis(
            availability=AnalysisAvailability.READ_ERROR,
            path=path,
            generation=self._generation,
            read_error=str(exc),
        )
        return self._analysis

    def _reset_cache(self) -> None:
        self._identity = None
        self._size = 0
        self._mtime_ns = 0
        self._prefix_digest = b""
        # A scan abandoned mid-batch leaves the retained digest state partially
        # updated, so every reset path must also drop it.
        self._prefix_hasher = None
        self._prefix_probe = b""
        self._verified_offset = 0
        self._cursor = ScanCursor()
        self._intermediate = None
        self._resolution_cache = _turns._ResolutionCache()


def analyze_trajectory(
    path: Path,
    *,
    fingerprint_key: bytes | None = None,
    verify_commands: str = DEFAULT_TRAJECTORY_VERIFY_COMMANDS,
) -> TrajectoryAnalysis:
    """Analyze *path* without retaining a live-tail cache."""
    return TrajectoryAnalyzer(fingerprint_key=fingerprint_key, verify_commands=verify_commands).load(path)


def _resolve(
    intermediate: _Intermediate,
    *,
    generation: int,
    resolution_cache: _turns._ResolutionCache,
    previous: TrajectoryAnalysis | None = None,
    cancel_event: Event | None = None,
    fingerprint_key: bytes | None = None,
    session_cache: _SessionProjectionCache | None = None,
    verify_commands: str = DEFAULT_TRAJECTORY_VERIFY_COMMANDS,
) -> TrajectoryAnalysis:
    _check_cancelled(cancel_event)
    active_ranges = _closed_sequence_union(intermediate.rollback_ranges)
    rollback_projection_unresolved = _rollback_projection_unresolved(intermediate, active_ranges)
    session_integrity_reason = _session_integrity_reason(
        intermediate,
        rollback_projection_unresolved=rollback_projection_unresolved,
    )
    active_turn_ids: set[str] = set()
    for turn_id, turn in intermediate.turns.items():
        _check_cancelled(cancel_event)
        if any(_active(event.sequence, active_ranges) for event in turn.starts):
            active_turn_ids.add(turn_id)
    revisions = _revision_memberships(
        intermediate,
        active_ranges,
        fingerprint_key=fingerprint_key,
        cancel_event=cancel_event,
    )
    session_projection = (
        session_cache.lazy(intermediate.path, cancel_event=cancel_event)
        if session_cache is not None
        else _lazy_session_projection(intermediate.path, cancel_event=cancel_event)
    )

    def mapped_carrier(item_id: str) -> str | None:
        return session_projection().carriers.get(item_id)

    # A folded logical turn is not a valid cache entry for any one of its
    # physical attempts. Re-resolve those attempts and fold them again; regular
    # one-attempt turns retain the live-tail fast path.
    previous_turns = (
        {turn.turn_id: turn for turn in previous.turns if len(turn.attempts) == 1} if previous is not None else {}
    )
    session_carrier_turn_ids = {
        turn_id
        for turn_id in active_turn_ids
        if _turns._turn_uses_session_carrier_fallback(
            intermediate,
            turn_id,
            active_ranges,
            cancel_event=cancel_event,
        )
    }
    resolved_turns: list[TurnAnalysis] = []
    for turn_id in active_turn_ids:
        _check_cancelled(cancel_event)
        if (
            not intermediate.dirty.full
            and turn_id not in intermediate.dirty.turn_ids
            and turn_id not in session_carrier_turn_ids
            and turn_id in previous_turns
        ):
            resolved_turns.append(previous_turns[turn_id])
        else:
            resolved_turns.append(
                _turns._resolve_turn(
                    intermediate,
                    intermediate.turns[turn_id],
                    active_ranges,
                    resolution_cache=resolution_cache,
                    revisions=revisions,
                    mapped_carrier=mapped_carrier,
                    rollback_projection_unresolved=rollback_projection_unresolved,
                    refresh_counter_axis=intermediate.dirty.full or turn_id in intermediate.dirty.turn_ids,
                    cancel_event=cancel_event,
                )
            )
    resolved_turns.sort(key=lambda turn: turn.start_sequence)
    actions = _resolve_actions(
        intermediate,
        active_ranges,
        resolved_turns,
        session_projection=session_projection,
        verify_commands=verify_commands,
        cancel_event=cancel_event,
    )
    actions_by_turn: dict[str, list[ActionOperation]] = defaultdict(list)
    for action in actions:
        actions_by_turn[action.turn_id].append(action)
    resolved_turns = [
        replace(
            turn,
            action_counts=_action_count_metrics(
                tuple(actions_by_turn.get(turn.turn_id, ())),
                projection_precision=turn.action_projection_precision,
                projection_reason=turn.action_projection_reason,
            ),
        )
        for turn in resolved_turns
    ]
    change_verification = _resolve_change_verification(
        intermediate,
        active_ranges,
        resolved_turns,
        actions,
        session_projection(),
        rollback_projection_unresolved=rollback_projection_unresolved,
        cancel_event=cancel_event,
    )
    if session_integrity_reason is not None:
        change_verification = _degrade_change_verification(change_verification, session_integrity_reason)
    retry = _retry_amplification(
        intermediate,
        active_ranges,
        revisions,
        cancel_event=cancel_event,
    )
    validation = _validation_metrics(
        actions,
        resolved_turns,
        change_verification,
        retry.tokens,
        cancel_event=cancel_event,
    )
    if session_integrity_reason is not None:
        validation = _degrade_validation_metrics(
            validation,
            session_integrity_reason,
            session_integrity_cap=True,
        )
    context_carrying_load = _context_carrying_load(
        intermediate,
        active_ranges,
        resolved_turns,
        revisions,
        session_projection(),
        cancel_event=cancel_event,
    )
    logical_turns = _fold_retry_turns(resolved_turns, cancel_event=cancel_event)
    runs = workflow_runs(
        intermediate, active_ranges, integrity_reason=session_integrity_reason, cancel_event=cancel_event
    )
    findings = evaluate_findings(
        actions=actions,
        turns=logical_turns,
        validation=validation,
        change_verification=change_verification,
        retry_amplification_evidence=retry.evidence,
        retried_exchange=retry.exchange,
        # A workflow session shows no Insights, which is what names the
        # items a costly-context finding is about.
        context_carrying_load=() if runs else context_carrying_load,
        outside_changes=(*_background_task_spans(intermediate), *_current_turn_drops(intermediate)),
        cancel_event=cancel_event,
    )
    span_mismatches, containment_violations = _timeline_diagnostics(
        intermediate,
        active_ranges,
        cancel_event=cancel_event,
    )
    diagnostics = TrajectoryDiagnostics(
        line_count=intermediate.line_count,
        byte_count=intermediate.byte_count,
        torn_tail_bytes=intermediate.torn_tail_bytes,
        corrupt_line_count=len(intermediate.corrupt_after_sequences),
        unsupported_event_count=intermediate.unsupported_event_count,
        accounted_prefix_violations=tuple(violation.message for violation in intermediate.prefix_violations),
        accounted_prefix_violation_details=tuple(
            SequenceRangeDiagnostic(violation.message, violation.first_sequence, violation.last_sequence)
            for violation in intermediate.prefix_violations
        ),
        explicit_gap_count=len(intermediate.explicit_gaps),
        explicit_gaps=tuple(
            SequenceRangeDiagnostic("trajectory gap", first, last) for first, last in intermediate.explicit_gaps
        ),
        rollback_projection_unresolved=rollback_projection_unresolved,
        span_duration_mismatch_count=len(span_mismatches),
        span_duration_mismatches=span_mismatches,
        containment_violation_count=len(containment_violations),
        containment_violations=containment_violations,
        malformed_hook_execution_mode_count=intermediate.malformed_hook_execution_mode_count,
        side_call_empty_shell_revisions=revisions.side_call_empty_shell_revisions,
        unidentified_membership_revision_count=revisions.unidentified_membership_revision_count,
        corrupt_lines=tuple(intermediate.corrupt_lines),
        unsupported_lines=tuple(intermediate.unsupported_lines),
    )
    overview = _overview(resolved_turns, cancel_event=cancel_event)
    token_usage = _token_usage(intermediate, active_ranges, resolved_turns, cancel_event=cancel_event)
    skill_usage = _tool_usage_panel(
        actions,
        resolved_turns,
        tool_kind="skill",
        display_name=lambda action: session_projection().skill_names.get(action.call_item_id or ""),
    )
    mcp_usage = _tool_usage_panel(
        actions,
        resolved_turns,
        tool_kind="mcp",
        display_name=lambda action: action.tool_name,
    )
    diagnostics = replace(
        diagnostics,
        timeline_operations=_timeline_operation_diagnostics(logical_turns),
    )
    insights = _insights_analysis(
        intermediate,
        active_ranges,
        resolved_turns,
        logical_turns,
        actions,
        context_carrying_load=context_carrying_load,
        cancel_event=cancel_event,
    )
    submission_latency = _submission_latency(intermediate, active_ranges, cancel_event=cancel_event)
    if session_integrity_reason is not None:
        overview = _degrade_overview(overview, session_integrity_reason)
        token_usage = _degrade_token_usage(token_usage, session_integrity_reason)
        skill_usage = _degrade_tool_usage_panel(skill_usage, session_integrity_reason)
        mcp_usage = _degrade_tool_usage_panel(mcp_usage, session_integrity_reason)
        insights = _degrade_insights(insights, session_integrity_reason)
        submission_latency = _degrade_submission_latency(submission_latency, session_integrity_reason)
    if runs:
        overview = include_workflow_overview(overview, runs)
    return TrajectoryAnalysis(
        availability=AnalysisAvailability.AVAILABLE,
        path=intermediate.path,
        generation=generation,
        overview=overview,
        workflow_runs=runs,
        turns=tuple(logical_turns),
        diagnostics=diagnostics,
        actions=actions,
        validation=None if runs else validation,
        findings=findings,
        submission_latency=None if runs else submission_latency,
        change_verification=None if runs else change_verification,
        token_usage=None if runs else token_usage,
        skill_usage=None if runs else skill_usage,
        mcp_usage=None if runs else mcp_usage,
        insights=None if runs else insights,
        session_span=SessionSpan(
            first_turn_started_at=intermediate.first_turn_started_at,
            last_turn_finished_at=intermediate.last_turn_finished_at,
            runtime_count=len(intermediate.runtime_starts),
        ),
    )


def _session_integrity_reason(
    intermediate: _Intermediate,
    *,
    rollback_projection_unresolved: bool,
) -> str | None:
    """Explain physical damage or an unresolvable live-history projection."""

    problems: list[str] = []
    if intermediate.line_count == 0 and intermediate.byte_count == 0 and intermediate.torn_tail_bytes == 0:
        problems.append("empty log")
    if intermediate.corrupt_after_sequences:
        problems.append("corrupt lines")
    if intermediate.unsupported_event_count:
        problems.append("unsupported events")
    if intermediate.prefix_violations:
        problems.append("accounted-prefix violations")
    if intermediate.explicit_gaps:
        problems.append("explicit gaps")
    if intermediate.torn_tail_bytes:
        problems.append("torn tail")
    if rollback_projection_unresolved:
        problems.append("unresolved rollback projection")
    if not problems:
        return None
    return f"session trajectory integrity is unresolved: {', '.join(problems)}"


def _rollback_projection_unresolved(
    intermediate: _Intermediate,
    inactive_ranges: tuple[SequenceRange, ...],
) -> bool:
    if any(_active(sequence, inactive_ranges) for sequence in intermediate.rollback_errors):
        return True
    rollback_pairs = {
        (old_branch, new_branch)
        for sequence, old_branch, new_branch in intermediate.rollback_branch_pairs
        if _active(sequence, inactive_ranges)
    }
    return any(
        _active(sequence, inactive_ranges)
        and (old_branch is None or new_branch is None or (old_branch, new_branch) not in rollback_pairs)
        for sequence, old_branch, new_branch in intermediate.branch_supersessions
    )


def _background_task_spans(intermediate: _Intermediate) -> tuple[SequenceSpan, ...]:
    """Where each background task the log records was running.

    A task whose start fell outside the log is taken as running from the
    beginning. A task with no finish event may outlive a runtime that closed
    the log properly (a rollback reopens the log in place and keeps
    background commands running), but not one that ended without closing it:
    the process is gone, and on Windows its job with it, while on POSIX a
    later launch stops the task or leaves it running untracked. So the task
    counts as running until the next runtime after such an end starts, or to
    the end of the log.
    """
    runtime_starts = sorted(
        (endpoint.sequence, runtime_id)
        for runtime_id, endpoints in intermediate.runtime_starts.items()
        for endpoint in endpoints
    )
    unclosed_ends = [
        sequence
        for (_previous_start, previous_id), (sequence, _runtime_id) in pairwise(runtime_starts)
        if not intermediate.runtime_finishes.get(previous_id)
    ]
    spans: list[SequenceSpan] = []
    for task in intermediate.background_tasks.values():
        start = 0 if task.started is None else task.started
        end = (
            task.finished
            if task.finished is not None
            else next((sequence for sequence in unclosed_ends if sequence > start), None)
        )
        spans.append(SequenceSpan(start, end))
    return tuple(spans)


def _current_turn_drops(intermediate: _Intermediate) -> tuple[SequenceSpan, ...]:
    """Where compaction dropped tool results of the turn it ran in.

    Only phase 4 touches the running turn; the other phases rewrite earlier,
    completed turns.
    """
    return tuple(
        SequenceSpan(finish.sequence, finish.sequence)
        for node in intermediate.nodes.get("compaction.phase", {}).values()
        for finish in node.finishes[-1:]
        if _payload_str(finish.payload, "phase") == "phase4"
    )


def _timeline_operation_diagnostics(
    turns: list[TurnAnalysis],
) -> tuple[TimelineOperationDiagnostic, ...]:
    """Project sparse row diagnostics without losing physical retry ownership."""
    diagnostics: list[TimelineOperationDiagnostic] = []
    for turn in turns:
        for attempt in turn.attempts:
            for operation in turn.operations[attempt.operation_start_index : attempt.operation_end_index]:
                code = operation.diagnostic_code
                reason = operation.reason
                if code is None or reason is None:
                    continue
                diagnostics.append(
                    TimelineOperationDiagnostic(
                        turn_id=attempt.turn_id,
                        turn_number=turn.turn_number,
                        operation_id=operation.operation_id,
                        family=operation.family,
                        precision=operation.precision,
                        code=code,
                        reason=reason,
                        identity=operation.identity,
                        hook_id=operation.hook_id,
                    )
                )
    return tuple(diagnostics)


def _overview(
    turns: list[TurnAnalysis],
    *,
    cancel_event: Event | None = None,
    metric_subject: str = "selected turns",
) -> TrajectoryOverview:
    for _turn in turns:
        _check_cancelled(cancel_event)
    elapsed = _sum_metrics([turn.elapsed_ns for turn in turns], metric_subject=metric_subject)
    compute_cp = _sum_metrics([turn.compute_cp_ns for turn in turns], metric_subject=metric_subject)
    response_cp = _sum_metrics([turn.response_cp_ns for turn in turns], metric_subject=metric_subject)
    exclusive = _sum_metrics([turn.exclusive_work_ns for turn in turns], metric_subject=metric_subject)
    overlap = _sum_metrics([turn.overlap_gain_ns for turn in turns], metric_subject=metric_subject)
    usage = _sum_metrics(
        [turn.usage_tokens for turn in turns],
        missing_is_missing=True,
        metric_subject=metric_subject,
    )
    ratio_precision = (
        Precision.EXACT
        if elapsed.precision is Precision.EXACT and exclusive.precision is Precision.EXACT
        else Precision.UNRESOLVED
    )
    ratio_reason = (
        None
        if ratio_precision is Precision.EXACT
        else _merge_reasons(elapsed.reason, exclusive.reason) or f"one or more {metric_subject} are unresolved"
    )
    elapsed_value = int(elapsed.value) if elapsed.value is not None else None
    exclusive_value = int(exclusive.value) if exclusive.value is not None else None
    wall = {
        bucket: _sum_metrics(
            [turn.wall_time_ns[bucket] for turn in turns],
            metric_subject=metric_subject,
        )
        for bucket in WallBucket
    }
    utilization: dict[WallBucket, Metric] = {}
    for bucket in (WallBucket.MODEL, WallBucket.TOOLS):
        work_ns = 0
        for turn in turns:
            _check_cancelled(cancel_event)
            work_ns += sum(
                item.duration_ns for item in turn.slices if item.counts_as_work and item.wall_bucket is bucket
            )
        utilization[bucket] = Metric(
            work_ns / elapsed_value
            if elapsed_value is not None and elapsed_value > 0
            else 0.0
            if elapsed_value == 0
            else None,
            ratio_precision,
            ratio_reason,
        )
    return TrajectoryOverview(
        elapsed_ns=elapsed,
        compute_cp_ns=compute_cp,
        response_cp_ns=response_cp,
        exclusive_work_ns=exclusive,
        parallelism=Metric(
            exclusive_value / elapsed_value
            if elapsed_value is not None and elapsed_value > 0 and exclusive_value is not None
            else 0.0
            if elapsed_value == 0 and exclusive_value is not None
            else None,
            ratio_precision,
            ratio_reason,
        ),
        overlap_gain_ns=overlap,
        wall_time_ns=wall,
        utilization=utilization,
        usage_tokens=usage,
    )


def _token_usage(
    intermediate: _Intermediate,
    inactive_ranges: tuple[SequenceRange, ...],
    turns: list[TurnAnalysis],
    *,
    cancel_event: Event | None,
) -> TokenUsage:
    per_turn: dict[UsageBucket, list[Metric]] = {bucket: [] for bucket in UsageBucket}
    for turn in turns:
        _check_cancelled(cancel_event)
        usage = turn.token_usage
        if usage is None:
            usage = _turns._turn_token_usage(intermediate, turn.turn_id, inactive_ranges, turn.usage_tokens)
        for bucket in UsageBucket:
            per_turn[bucket].append(usage.buckets[bucket])
    buckets = {
        UsageBucket.INPUT: _sum_metrics(per_turn[UsageBucket.INPUT], missing_is_missing=True),
        UsageBucket.OUTPUT: _sum_metrics(per_turn[UsageBucket.OUTPUT], missing_is_missing=True),
    }
    for bucket in _turns._OPTIONAL_USAGE_BUCKETS:
        buckets[bucket] = _sum_optional_bucket_metrics(per_turn[bucket])
    return TokenUsage(buckets=buckets)


def _fold_retry_turns(turns: list[TurnAnalysis], *, cancel_event: Event | None = None) -> list[TurnAnalysis]:
    """Fold consecutive retry/resume attempts into their one user-level turn.

    Producers keep a fresh physical ``turn_id`` per pass so every lifecycle can
    still be validated independently. ``turn_number`` is the user-level
    ordinal, and ``is_retry`` explicitly says that a later pass continues the
    preceding ordinal rather than opening another turn.
    """
    groups: list[list[TurnAnalysis]] = []
    for turn in turns:
        _check_cancelled(cancel_event)
        if (
            turn.attempts[0].is_retry
            and groups
            and turn.turn_number is not None
            and turn.turn_number == groups[-1][0].turn_number
        ):
            groups[-1].append(turn)
        else:
            groups.append([turn])
    return [_fold_turn_group(group, cancel_event=cancel_event) for group in groups]


def _fold_turn_group(attempts: list[TurnAnalysis], *, cancel_event: Event | None) -> TurnAnalysis:
    canonical = attempts[0]
    if len(attempts) == 1:
        attempt = attempts[0]
        source = attempt.attempts[0]
        diagnostics = list(attempt.diagnostics)
        if source.physical_axis_end_ns < source.physical_axis_start_ns:
            diagnostics.append("folded attempt axis end precedes its start")
        normalized = replace(
            source,
            logical_axis_start_ns=attempt.axis_start_ns,
            operation_start_index=0,
            operation_end_index=len(attempt.operations),
            slice_start_index=0,
            slice_end_index=len(attempt.slices),
        )
        return replace(
            attempt,
            axis_end_ns=attempt.axis_start_ns + normalized.duration_ns,
            diagnostics=tuple(dict.fromkeys(diagnostics)),
            attempts=(normalized,),
        )

    overview = _overview(
        attempts,
        cancel_event=cancel_event,
        metric_subject="attempts of this turn",
    )
    axis_start = canonical.axis_start_ns
    cursor = axis_start
    operations: list[TimelineOperation] = []
    slices: list[TimeSlice] = []
    attempt_refs: list[TurnAttemptRef] = []
    fold_diagnostics: list[str] = []
    parent_pairs = array("I")
    causal_pairs = array("I")
    root_index: int | None = None
    flows_exact = all(attempt.flow is not None for attempt in attempts)
    if len({attempt.runtime_id for attempt in attempts}) > 1:
        fold_diagnostics.append("logical turn spans multiple trajectory runtimes")
    for attempt_index, attempt in enumerate(attempts):
        _check_cancelled(cancel_event)
        source = attempt.attempts[0]
        if len(attempt.attempts) != 1:
            fold_diagnostics.append("fold input already contains multiple physical attempts")
        if source.physical_axis_end_ns < source.physical_axis_start_ns:
            fold_diagnostics.append("folded attempt axis end precedes its start")
        shift = cursor - attempt.axis_start_ns
        operation_offset = len(operations)
        operations.extend(
            replace(
                operation,
                start_ns=operation.start_ns + shift if operation.start_ns is not None else None,
                end_ns=operation.end_ns + shift if operation.end_ns is not None else None,
            )
            for operation in attempt.operations
        )
        operation_end = len(operations)
        slice_offset = len(slices)
        for item in attempt.slices:
            slices.append(
                replace(
                    item,
                    slice_index=len(slices),
                    start_ns=item.start_ns + shift,
                    end_ns=item.end_ns + shift,
                )
            )
        attempt_refs.append(
            TurnAttemptRef(
                turn_id=source.turn_id,
                runtime_id=source.runtime_id,
                is_retry=source.is_retry,
                physical_axis_start_ns=source.physical_axis_start_ns,
                physical_axis_end_ns=source.physical_axis_end_ns,
                logical_axis_start_ns=cursor,
                operation_start_index=operation_offset,
                operation_end_index=operation_end,
                slice_start_index=slice_offset,
                slice_end_index=len(slices),
            )
        )
        flow = attempt.flow
        if flow is not None:
            if root_index is None and flow.root_index is not None:
                root_index = flow.root_index + operation_offset
            # A cancelled/interrupted physical attempt may still have a typed
            # response terminal. Only the final attempt owns the response of
            # the stitched logical turn, so earlier terminal edges stop here.
            for edge_source, target in flow.parent_edges():
                if target == FLOW_TERMINAL_INDEX and attempt_index < len(attempts) - 1:
                    continue
                # FLOW_TERMINAL_INDEX is producer-defined as a target-only
                # response sentinel, so every source is a real operation index.
                parent_pairs.extend(
                    (
                        edge_source + operation_offset,
                        FLOW_TERMINAL_INDEX if target == FLOW_TERMINAL_INDEX else target + operation_offset,
                    )
                )
            for edge_source, target in flow.causal_edges():
                if target == FLOW_TERMINAL_INDEX and attempt_index < len(attempts) - 1:
                    continue
                causal_pairs.extend(
                    (
                        edge_source + operation_offset,
                        FLOW_TERMINAL_INDEX if target == FLOW_TERMINAL_INDEX else target + operation_offset,
                    )
                )
        cursor += attempt_refs[-1].duration_ns

    action_counts = {
        action_class: _sum_metrics(
            [attempt.action_counts.get(action_class, Metric(0, Precision.EXACT)) for attempt in attempts],
            metric_subject="attempts of this turn",
        )
        for action_class in ActionClass
    }
    action_precision = least_precision(attempt.action_projection_precision for attempt in attempts)
    action_reason = next(
        (
            attempt.action_projection_reason
            for attempt in attempts
            if attempt.action_projection_precision is not Precision.EXACT and attempt.action_projection_reason
        ),
        None,
    )
    token_usages = [attempt.token_usage for attempt in attempts]
    token_usage = (
        _merge_turn_token_usage([usage for usage in token_usages if usage is not None])
        if all(usage is not None for usage in token_usages)
        else None
    )
    flow = (
        TurnFlow(
            turn_id=canonical.turn_id,
            root_index=root_index,
            has_terminal=bool(attempts[-1].flow and attempts[-1].flow.has_terminal),
            parent_pairs=parent_pairs.tobytes(),
            causal_pairs=causal_pairs.tobytes(),
            acyclic=all(attempt.flow is not None and attempt.flow.acyclic for attempt in attempts),
        )
        if flows_exact
        else None
    )
    return replace(
        canonical,
        end_sequence=attempts[-1].end_sequence,
        elapsed_ns=overview.elapsed_ns,
        compute_cp_ns=overview.compute_cp_ns,
        response_cp_ns=overview.response_cp_ns,
        exclusive_work_ns=overview.exclusive_work_ns,
        parallelism=overview.parallelism,
        overlap_gain_ns=overview.overlap_gain_ns,
        wall_time_ns=overview.wall_time_ns,
        utilization=overview.utilization,
        usage_tokens=overview.usage_tokens,
        axis_end_ns=cursor,
        operations=tuple(operations),
        slices=tuple(slices),
        diagnostics=tuple(
            dict.fromkeys(
                (
                    *(diagnostic for attempt in attempts for diagnostic in attempt.diagnostics),
                    *fold_diagnostics,
                )
            )
        ),
        action_counts=action_counts,
        critical_tool_contributions_ns=dict(
            sum((Counter(attempt.critical_tool_contributions_ns) for attempt in attempts), Counter())
        ),
        server_critical_contributions_ns=dict(
            sum((Counter(attempt.server_critical_contributions_ns) for attempt in attempts), Counter())
        ),
        action_projection_precision=action_precision,
        action_projection_reason=action_reason,
        token_usage=token_usage,
        flow=flow,
        attempts=tuple(attempt_refs),
    )


def _merge_turn_token_usage(usages: list[TokenUsage]) -> TokenUsage:
    per_bucket = {bucket: [usage.buckets[bucket] for usage in usages] for bucket in UsageBucket}
    buckets = {
        UsageBucket.INPUT: _sum_metrics(
            per_bucket[UsageBucket.INPUT],
            missing_is_missing=True,
            metric_subject="attempts of this turn",
        ),
        UsageBucket.OUTPUT: _sum_metrics(
            per_bucket[UsageBucket.OUTPUT],
            missing_is_missing=True,
            metric_subject="attempts of this turn",
        ),
    }
    for bucket in _turns._OPTIONAL_USAGE_BUCKETS:
        buckets[bucket] = _sum_optional_bucket_metrics(
            per_bucket[bucket],
            metric_subject="attempts of this turn",
        )
    return TokenUsage(buckets=buckets)


@dataclass(frozen=True, slots=True)
class _RetryAmplification:
    """Tokens resent by retried model exchanges, with what the finding needs to name them."""

    tokens: Metric
    evidence: tuple[str, ...]
    """The rollback-stable context items the retried requests carried."""
    exchange: RetriedExchange | None
    """The first retried exchange, even when its usage is zero or was never reported."""


def _retry_amplification(
    intermediate: _Intermediate,
    inactive_ranges: tuple[SequenceRange, ...],
    revisions: _RevisionResolution,
    *,
    cancel_event: Event | None,
) -> _RetryAmplification:
    attribution_exact = True
    retry_ids: set[str] = set()
    for node in intermediate.nodes.get("retry", {}).values():
        _check_cancelled(cancel_event)
        if _all_side_call_lifecycle(node):
            continue
        cut = _lifecycle_cut(node, inactive_ranges)
        if cut is not _LifecycleCut.NONE:
            ownership = _node_owner_membership(
                intermediate,
                node,
                inactive_ranges,
                target_operation_id=node.starts[0].parent_operation_id,
            )
            if not _exclusively_inactive(ownership):
                attribution_exact = False
            continue
        starts = [event for event in node.starts if _active(event.sequence, inactive_ranges)]
        finishes = [event for event in node.finishes if _active(event.sequence, inactive_ranges)]
        endpoints = (*starts, *finishes)
        if not endpoints or all(event.side_call for event in endpoints):
            continue
        if any(event.side_call for event in endpoints):
            attribution_exact = False
            continue
        if len(starts) == 1 and not finishes:
            # A scheduled retry whose backoff never starts adds no retry usage.
            continue
        if (
            len(starts) != 1
            or len(finishes) != 1
            or starts[0].scope != finishes[0].scope
            or finishes[0].sequence <= starts[0].sequence
        ):
            attribution_exact = False
            continue
        retry_ids.add(node.operation_id)
    if not retry_ids:
        metric = (
            Metric(0, Precision.EXACT)
            if attribution_exact
            else Metric(None, Precision.UNRESOLVED, "retry lifecycle attribution is incomplete")
        )
        return _RetryAmplification(metric, (), None)
    parent_by_operation: dict[str, str | None] = {}
    for family in ("model.run", "model.cycle", "model.exchange"):
        for node in intermediate.nodes.get(family, {}).values():
            _check_cancelled(cancel_event)
            if _all_side_call_lifecycle(node):
                continue
            cut = _lifecycle_cut(node, inactive_ranges)
            if cut is not _LifecycleCut.NONE:
                ownership = _node_owner_membership(
                    intermediate,
                    node,
                    inactive_ranges,
                    target_operation_id=node.starts[0].parent_operation_id,
                )
                if not _exclusively_inactive(ownership):
                    attribution_exact = False
                continue
            starts = [event for event in node.starts if _active(event.sequence, inactive_ranges)]
            finishes = [event for event in node.finishes if _active(event.sequence, inactive_ranges)]
            endpoints = (*starts, *finishes)
            if not endpoints or all(event.side_call for event in endpoints):
                continue
            if any(event.side_call for event in endpoints) or len(starts) != 1:
                attribution_exact = False
            else:
                parent_by_operation[node.operation_id] = starts[0].parent_operation_id

    def retried(operation_id: str | None) -> bool:
        current = operation_id
        visited: set[str] = set()
        while current is not None and current not in visited:
            _check_cancelled(cancel_event)
            if current in retry_ids:
                return True
            visited.add(current)
            current = parent_by_operation.get(current)
        return False

    retried_exchanges: dict[int, tuple[str, _Endpoint]] = {}
    for node in intermediate.nodes.get("model.exchange", {}).values():
        _check_cancelled(cancel_event)
        if _all_side_call_lifecycle(node):
            continue
        if not retried(node.operation_id):
            continue
        cut = _lifecycle_cut(node, inactive_ranges)
        if cut is not _LifecycleCut.NONE:
            ownership = _node_owner_membership(
                intermediate,
                node,
                inactive_ranges,
                target_operation_id=node.starts[0].parent_operation_id,
            )
            if not _exclusively_inactive(ownership):
                attribution_exact = False
            continue
        starts = [event for event in node.starts if _active(event.sequence, inactive_ranges)]
        finishes = [event for event in node.finishes if _active(event.sequence, inactive_ranges)]
        endpoints = (*starts, *finishes)
        if endpoints and all(event.side_call for event in endpoints):
            continue
        if (
            any(event.side_call for event in endpoints)
            or len(starts) != 1
            or len(finishes) != 1
            or starts[0].scope != finishes[0].scope
            or finishes[0].sequence <= starts[0].sequence
        ):
            attribution_exact = False
        else:
            retried_exchanges[finishes[0].sequence] = (node.operation_id, starts[0])
    amplified = [
        usage
        for usages in intermediate.usage_by_turn.values()
        for usage in usages
        if _active(usage.sequence, inactive_ranges) and usage.sequence in retried_exchanges
    ]
    if not amplified:
        metric = (
            Metric(0, Precision.EXACT)
            if attribution_exact
            else Metric(None, Precision.UNRESOLVED, "retry exchange attribution is incomplete")
        )
        return _RetryAmplification(metric, (), None)
    reported = [usage for usage in amplified if not usage.unreported_failure]
    missing = any(
        usage.normalization_unavailable or usage.input_total is None or usage.output_total is None for usage in reported
    )
    value = sum((usage.input_total or 0) + (usage.output_total or 0) for usage in reported)
    provenance_exact = all(
        usage.bucket_has_provider_provenance(UsageBucket.INPUT)
        and usage.bucket_has_provider_provenance(UsageBucket.OUTPUT)
        for usage in reported
    )
    unreported = any(usage.failed for usage in amplified)
    precision = (
        Precision.MISSING
        if missing
        else Precision.UNRESOLVED
        if not (provenance_exact and attribution_exact)
        else Precision.ESTIMATED
        if unreported
        else Precision.EXACT
    )
    reason = (
        "normalized usage is missing for one or more retried exchanges"
        if missing
        else "retry exchange attribution is incomplete"
        if not attribution_exact
        else "normalized retry usage provenance is incomplete"
        if not provenance_exact
        else _UNREPORTED_FAILURE_USAGE_REASON
        if unreported
        else None
    )
    stable_items: set[str] = set()
    exchange: RetriedExchange | None = None
    for usage in amplified:
        _check_cancelled(cancel_event)
        operation_id, start = retried_exchanges[usage.sequence]
        revision_id = _payload_str(start.payload, "context_revision_id")
        stable_items.update(revisions.memberships.get(revision_id or "", ()))
        if exchange is None:
            exchange = RetriedExchange(start.turn_id, operation_id, usage.sequence)
    return _RetryAmplification(
        Metric(value if not missing else None, precision, reason), tuple(sorted(stable_items)), exchange
    )


def _all_side_call_lifecycle(node: _Node) -> bool:
    """Whether every raw endpoint belongs to a non-main actor."""
    endpoints = (*node.starts, *node.finishes)
    return bool(endpoints) and all(endpoint.side_call for endpoint in endpoints)


# The vocabulary this build's writer can emit; an outcome outside it is
# damaged or from a different writer and cannot pass for read evidence.
_RECOGNIZED_PREPARATION_OUTCOMES: Final = frozenset(
    value for name, value in vars(PreparationOutcome).items() if not name.startswith("_") and isinstance(value, str)
)
_PROMOTED_PREPARATION_OUTCOMES: Final = frozenset({PreparationOutcome.FRESH_TURN, PreparationOutcome.RETRY_TURN})


def _submission_latency(
    intermediate: _Intermediate,
    inactive_ranges: tuple[SequenceRange, ...],
    *,
    cancel_event: Event | None,
) -> SubmissionLatencyOverview:
    turn_by_scope: dict[str, list[_Endpoint]] = defaultdict(list)
    turn_scope_membership: dict[str, _Membership] = {}
    for turn in intermediate.turns.values():
        _check_cancelled(cancel_event)
        for start in turn.starts:
            scope_id = _payload_str(start.payload, "preparation_scope_operation_id")
            if scope_id is None:
                continue
            active = _active(start.sequence, inactive_ranges)
            turn_scope_membership[scope_id] = _merge_projection_memberships(
                turn_scope_membership.get(scope_id, _Membership(False, False)),
                _Membership(active, not active),
            )
            if active:
                turn_by_scope[scope_id].append(start)
    samples: list[SubmissionLatencySample] = []
    for node in intermediate.nodes.get("preparation", {}).values():
        _check_cancelled(cancel_event)
        starts = [event for event in node.starts if _active(event.sequence, inactive_ranges)]
        finishes = [event for event in node.finishes if _active(event.sequence, inactive_ranges)]
        endpoints = sorted((*starts, *finishes), key=lambda endpoint: endpoint.sequence)
        if not endpoints or _payload_str(endpoints[0].payload, "scope") != "pre_turn":
            continue
        if all(endpoint.side_call for endpoint in endpoints):
            continue
        if _drop_submission_lifecycle_cut(intermediate, node, inactive_ranges, turn_scope_membership):
            # The raw lifecycle is complete, but rollback cut its projection
            # away from every active owner. Its surviving endpoint is not a
            # live submission sample; ownership ambiguity remains diagnosable.
            continue
        start = starts[0] if len(starts) == 1 else None
        finish = finishes[0] if len(finishes) == 1 else None
        outcome = _payload_str(finish.payload, "outcome") if finish is not None else None
        bucket = _submission_bucket(outcome)
        turn_candidates = turn_by_scope.get(node.operation_id, ())
        if outcome in {"fresh_turn", "retry_turn"}:
            end = turn_candidates[0] if len(turn_candidates) == 1 else None
            turn_id = (
                end.turn_id if end is not None else _payload_str(finish.payload, "target_turn_id") if finish else None
            )
        else:
            end = finish
            turn_id = _payload_str(finish.payload, "target_turn_id") if finish is not None else None
        exact = (
            start is not None
            and finish is not None
            and end is not None
            and outcome in _RECOGNIZED_PREPARATION_OUTCOMES
            and not start.side_call
            and finish.monotonic_measurement
            and finish.scope == start.scope
            and finish.sequence > start.sequence
            and finish.monotonic_ns >= start.monotonic_ns
            and end.runtime_id == start.runtime_id
            and end.branch_id == start.branch_id
            and end.coverage_id == start.coverage_id
            and end.actor_id == start.actor_id
            and not end.side_call
            and end.sequence > start.sequence
            and end.monotonic_ns >= start.monotonic_ns
        )
        reason = None
        if len(starts) != 1:
            reason = "preparation scope does not have exactly one start"
        elif len(finishes) != 1:
            reason = "preparation scope does not have exactly one finished event"
        elif outcome is None:
            reason = "preparation terminal outcome is missing"
        elif outcome not in _RECOGNIZED_PREPARATION_OUTCOMES:
            reason = "preparation terminal outcome is unrecognized"
        elif end is None:
            reason = "promoted turn start cannot be resolved uniquely"
        elif start is not None and start.side_call:
            reason = "preparation scope belongs to a side-call actor"
        elif finish is not None and not finish.monotonic_measurement:
            reason = "preparation duration lacks monotonic provenance"
        elif start is not None and finish is not None and finish.scope != start.scope:
            reason = "preparation endpoints cross scope"
        elif (
            start is not None
            and finish is not None
            and (finish.sequence <= start.sequence or finish.monotonic_ns < start.monotonic_ns)
        ):
            reason = "preparation lifecycle endpoints are not ordered"
        elif (
            start is not None
            and end is not None
            and (
                end.runtime_id != start.runtime_id
                or end.branch_id != start.branch_id
                or end.coverage_id != start.coverage_id
                or end.actor_id != start.actor_id
                or end.side_call
            )
        ):
            reason = "submission endpoints cross scope"
        elif end is not None and start is not None and end.sequence <= start.sequence:
            reason = "submission endpoints are not ordered"
        elif end is not None and start is not None and end.monotonic_ns < start.monotonic_ns:
            reason = "submission latency has a negative endpoint interval"
        start_ns = start.monotonic_ns if start is not None else None
        raw_end_ns = end.monotonic_ns if end is not None else None
        end_ns = raw_end_ns if start_ns is None or raw_end_ns is None or raw_end_ns >= start_ns else None
        samples.append(
            SubmissionLatencySample(
                scope_operation_id=node.operation_id,
                outcome=outcome,
                bucket=bucket,
                start_sequence=start.sequence if start is not None else endpoints[0].sequence,
                start_ns=start_ns,
                end_ns=end_ns,
                duration_ns=Metric(
                    end_ns - start_ns if exact and end_ns is not None and start_ns is not None else None,
                    Precision.EXACT if exact else Precision.UNRESOLVED,
                    reason,
                ),
                turn_id=turn_id,
                finished_count=len(finishes),
            )
        )
    ordered = tuple(sorted(samples, key=lambda sample: sample.start_sequence))
    stats: list[SubmissionLatencyStats] = []
    for bucket in SubmissionLatencyBucket:
        _check_cancelled(cancel_event)
        bucket_samples = tuple(sample for sample in ordered if sample.bucket is bucket)
        resolved = [
            cast("int", sample.duration_ns.value)
            for sample in bucket_samples
            if sample.duration_ns.precision is Precision.EXACT
        ]
        stats.append(
            SubmissionLatencyStats(
                bucket=bucket,
                sample_count=len(bucket_samples),
                unresolved_count=len(bucket_samples) - len(resolved),
                p50_ns=_percentile_metric(resolved, 0.50),
                p90_ns=_percentile_metric(resolved, 0.90),
                max_ns=Metric(max(resolved), Precision.EXACT)
                if resolved
                else Metric(None, Precision.MISSING, "no resolved samples"),
                samples=bucket_samples,
            )
        )
    return SubmissionLatencyOverview(tuple(stats))


def _drop_submission_lifecycle_cut(
    intermediate: _Intermediate,
    node: _Node,
    inactive_ranges: tuple[SequenceRange, ...],
    turn_scope_membership: dict[str, _Membership],
) -> bool:
    """Whether a complete cut lifecycle has no active submission owner."""
    if _lifecycle_cut(node, inactive_ranges) is _LifecycleCut.NONE:
        return False
    raw_finish = node.finishes[0]
    outcome = _payload_str(raw_finish.payload, "outcome")
    if outcome not in _RECOGNIZED_PREPARATION_OUTCOMES:
        return False
    scope_membership = turn_scope_membership.get(node.operation_id, _Membership(False, False))
    target_turn_id = _payload_str(raw_finish.payload, "target_turn_id")
    target_membership = _turn_start_membership(
        intermediate,
        {target_turn_id} if target_turn_id is not None else set(),
        inactive_ranges,
    )
    ownership = _merge_projection_memberships(scope_membership, target_membership)
    if ownership.active:
        return False
    if outcome in _PROMOTED_PREPARATION_OUTCOMES:
        # A promoted turn must claim the preparation scope. Absence is damaged
        # evidence rather than proof that the lifecycle was superseded.
        return _exclusively_inactive(scope_membership)
    if outcome == PreparationOutcome.INJECTED:
        # Successful injection terminals explicitly identify their target turn.
        return target_turn_id is not None and _exclusively_inactive(target_membership)
    # Rejected/cancelled/failed admission normally creates no turn claim. For
    # these derived samples, a complete cut with no active owner is superseded.
    return True


def _submission_bucket(outcome: str | None) -> SubmissionLatencyBucket:
    if outcome in {"fresh_turn", "retry_turn"}:
        return SubmissionLatencyBucket.BECAME_TURN
    if outcome == "injected":
        return SubmissionLatencyBucket.INJECTED
    return SubmissionLatencyBucket.DID_NOT_BECOME_TURN


def _degrade_overview(overview: TrajectoryOverview, reason: str) -> TrajectoryOverview:
    """Cap selection-wide metrics without changing still-provable turn metrics."""

    return TrajectoryOverview(
        elapsed_ns=_cap_session_metric(overview.elapsed_ns, reason),
        compute_cp_ns=_cap_session_metric(overview.compute_cp_ns, reason),
        response_cp_ns=_cap_session_metric(overview.response_cp_ns, reason),
        exclusive_work_ns=_cap_session_metric(overview.exclusive_work_ns, reason),
        parallelism=_cap_session_metric(overview.parallelism, reason),
        overlap_gain_ns=_cap_session_metric(overview.overlap_gain_ns, reason),
        wall_time_ns={bucket: _cap_session_metric(metric, reason) for bucket, metric in overview.wall_time_ns.items()},
        utilization={bucket: _cap_session_metric(metric, reason) for bucket, metric in overview.utilization.items()},
        usage_tokens=_cap_session_metric(overview.usage_tokens, reason),
    )


def _degrade_token_usage(usage: TokenUsage, reason: str) -> TokenUsage:
    """Cap normalized session token totals when events may be missing."""

    return TokenUsage(buckets={bucket: _cap_session_metric(metric, reason) for bucket, metric in usage.buckets.items()})


def _degrade_submission_latency(latency: SubmissionLatencyOverview, reason: str) -> SubmissionLatencyOverview:
    """Cap session percentile summaries without degrading intact samples."""

    return SubmissionLatencyOverview(
        buckets=tuple(
            replace(
                stats,
                p50_ns=_cap_session_metric(stats.p50_ns, reason),
                p90_ns=_cap_session_metric(stats.p90_ns, reason),
                max_ns=_cap_session_metric(stats.max_ns, reason),
            )
            for stats in latency.buckets
        )
    )
