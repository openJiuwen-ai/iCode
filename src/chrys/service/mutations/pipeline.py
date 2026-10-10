# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Mutation tracking pipeline for tool event middleware.

Provides before/after orchestration helpers shared by both
``ToolEventMiddleware`` and ``SubAgentEventMiddleware``, eliminating
duplicated mutation-tracking code.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from chrys.foundation.platform.paths import resolve_workspace_path
from chrys.service.mutations.coordination import canonical_key, fold_case
from chrys.service.mutations.tool_names import _FILE_TOOLS, _IMPLICIT_WRITE_TOOLS
from chrys.service.mutations.trace import (
    allocate_trace_file,
    discard_trace_file,
    parse_trace_write_keys,
    shell_trace_argv,
)
from chrys.service.mutations.types import (
    FileHashDiff,
    FileMutationTextSnapshot,
    MutationProvenance,
    MutationSource,
    SnapshotSkipReason,
)

if TYPE_CHECKING:
    from contextvars import Token

    from chrys.service.mutations.coordination import MutationCoordinator
    from chrys.service.mutations.git_calibrator import GitDiffCalibrator
    from chrys.service.mutations.tracker import MutationTracker
    from chrys.service.mutations.types import FileMutation, MutationOp

logger = logging.getLogger(__name__)

_MAX_SHELL_CHILD_SNAPSHOT_FILES = 500
_MAX_SHELL_CHILD_SNAPSHOT_BYTES = 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# Pipeline dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MutationContext:
    """Pre-execution mutation tracking state captured by ``prepare_mutation_tracking``."""

    tool_operation_id: str | None = None
    file_mutation: FileMutation | None = None
    # True for calls whose writes only implicit detection can observe
    # (shell / implicit write tools).  Every artifact below is optional —
    # coordination off, non-Git cwd, no heuristic targets, fsatrace
    # unavailable can all hold at once — so an abort after execution may
    # have started keys "detection died with the call" off this flag,
    # not off which artifacts happened to arm.
    implicit_detection: bool = False
    shell_scan_before: dict | None = None
    git_calibrator: GitDiffCalibrator | None = None
    # Cross-session coordination: the observation window advertised for
    # this call (shell/implicit only) and when it was opened.  The open
    # timestamp doubles as the write-interval lower bound for every
    # window-detected mutation.
    window_id: str | None = None
    window_opened_at: float | None = None
    workspace_cwd: str | None = None
    # Layer 3: per-call fsatrace artifacts.  ``trace_token`` restores the
    # shell wrapper contextvar and must be reset in the context that set
    # it — the async finalize on the normal path, the sync abort on the
    # cancel/reject paths (all three run in the middleware's context).
    trace_file: str | None = None
    trace_token: Token[tuple[str, ...] | None] | None = None


@dataclass
class MutationResult:
    """Post-execution mutation snapshots produced by ``finalize_mutation_tracking``."""

    file_snapshot: tuple[str, str] | None = None
    # Set only when ``file_snapshot`` was captured; consumers use both
    # fields together to preserve create/modify/delete semantics.
    file_operation: str | None = None
    file_bytes_changed: bool | None = None
    file_hashes: FileHashDiff | None = None
    shell_snapshots: dict[str, FileMutationTextSnapshot] | None = None


@dataclass(frozen=True)
class _ShellObservation:
    """One per-command candidate collected before tracker recording."""

    path: str
    operation: MutationOp
    before_data: bytes | None = None
    calibrated: bool = False
    # Set when the snapshot policy withheld the before content (oversized
    # old blob): the event is still recorded, only the backup is missing.
    before_skip: SnapshotSkipReason | None = None
    before_size: int | None = None
    # The old endpoint was a symlink (Git mode 120000); its blob is the
    # link's target text.  before_symlink_dir is its kind proven from
    # the old tree (None = unprovable).
    before_symlink: bool = False
    before_symlink_dir: bool | None = None


# ---------------------------------------------------------------------------
# Low-level helpers (moved from module-level in old middleware.py)
# ---------------------------------------------------------------------------


def _track_file_mutation_before(
    tracker: MutationTracker,
    tool_name: str,
    args: dict,
    call_id: str,
    workspace_cwd: str | None = None,
    tool_operation_id: str | None = None,
) -> FileMutation | None:
    """Snapshot file state before write_file/edit_file execution."""
    import os

    from chrys.service.mutations.types import MutationOp, MutationSource

    file_path = args.get("path", "")
    if not file_path:
        return None
    resolved = resolve_workspace_path(file_path, base_cwd=workspace_cwd)
    if tool_name == "write_file":
        op = MutationOp.CREATE if not os.path.exists(resolved) else MutationOp.MODIFY
        source = MutationSource.WRITE_FILE
    else:
        op = MutationOp.MODIFY
        source = MutationSource.EDIT_FILE
    return tracker.record(resolved, op, source, call_id, tool_operation_id=tool_operation_id)


def _track_shell_scan_before(tracker: MutationTracker, args: dict, workspace_cwd: str | None = None) -> dict | None:
    """Pre-snapshot likely mutation targets before shell command execution."""
    from chrys.service.mutations.detector import ShellMutationDetector
    from chrys.service.mutations.git_state import entry_display_path
    from chrys.service.mutations.scanner import WorkspaceScanner

    command = args.get("command", "")
    raw_cwd = args.get("working_dir") or args.get("cwd") or workspace_cwd
    cwd = resolve_workspace_path(raw_cwd, base_cwd=workspace_cwd) if raw_cwd else resolve_workspace_path(".")

    targets = ShellMutationDetector.detect_targets(command, cwd)
    target_paths = [entry_display_path(path) for path, _ in targets]

    if not target_paths:
        return None

    # Keep a directory alias's children attached to the directory observed
    # before execution. Unlinking/retargeting the alias does not delete them.
    scan_targets = [os.path.realpath(path) if os.path.isdir(path) else path for path in target_paths]
    scanner = WorkspaceScanner(cwd)
    try:
        scan = scanner.scan_paths(scan_targets)
    except OSError:
        logger.debug("Shell before-scan failed", exc_info=True)
        tracker.mark_detection_truncated()
        return None
    # Explicit file/missing/link targets refresh the hash chain independently
    # of the child budget. save() validates their existence; directory links
    # need their own entry backup as well as their children's stable scan.
    explicit_paths = dict.fromkeys(path for path in target_paths if os.path.islink(path) or not os.path.isdir(path))
    children = {path: entry for path, entry in scan.items() if path not in explicit_paths}
    if (
        len(children) > _MAX_SHELL_CHILD_SNAPSHOT_FILES
        or sum(entry.size for entry in children.values()) > _MAX_SHELL_CHILD_SNAPSHOT_BYTES
    ):
        # Never replace a skipped before backup with a post-command read.
        # Drop directory observations at both endpoints; Git calibration can
        # still supply independently verified changes within its own limits.
        tracker.mark_detection_truncated()
        scan_targets = [path for path in scan_targets if path in explicit_paths and not os.path.isdir(path)]
        scan = {path: entry for path, entry in scan.items() if path in scan_targets}
    tracker.pre_snapshot(list(dict.fromkeys([*explicit_paths, *scan])))
    return {"scan": scan, "targets": scan_targets, "cwd": cwd}


def _track_shell_scan_after(before_info: dict) -> list[_ShellObservation]:
    """Collect targeted file changes after shell command execution."""
    from chrys.service.mutations.scanner import WorkspaceScanner

    target_paths = before_info["targets"]
    cwd = before_info["cwd"]

    scanner = WorkspaceScanner(cwd)
    after_scan = scanner.scan_paths(target_paths)
    changes = WorkspaceScanner.diff(before_info["scan"], after_scan)

    return [_ShellObservation(path=path, operation=operation) for path, operation in changes]


def _git_calibration_scope_paths(cwd: str, workspace_cwd: str | None) -> list[str]:
    """Scope git calibration to the primary workspace and the command cwd."""
    scope_paths: list[str] = []
    seen: set[str] = set()
    for path in (workspace_cwd, cwd):
        if not path:
            continue
        normalized = os.path.normpath(os.path.abspath(path))
        if normalized in seen:
            continue
        seen.add(normalized)
        scope_paths.append(normalized)
    return scope_paths


async def _track_git_capture_before(
    tracker: MutationTracker,
    args: dict,
    workspace_cwd: str | None = None,
) -> GitDiffCalibrator | None:
    """Capture git state before a shell command for implicit change detection."""
    from chrys.service.mutations.git_calibrator import GitDiffCalibrator

    raw_cwd = args.get("working_dir") or args.get("cwd") or workspace_cwd
    cwd = resolve_workspace_path(raw_cwd, base_cwd=workspace_cwd) if raw_cwd else resolve_workspace_path(".")

    def _capture() -> GitDiffCalibrator | None:
        repo_root = GitDiffCalibrator.find_repo_root(cwd)
        if repo_root is None:
            return None
        cal = GitDiffCalibrator(
            repo_root,
            scope_paths=_git_calibration_scope_paths(cwd, workspace_cwd),
            max_snapshot_file_bytes=tracker.store.policy.max_file_bytes,
        )
        dirty_files = cal.capture_before()
        if dirty_files:
            tracker.pre_snapshot(dirty_files)
        return cal

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _capture)
    except Exception:
        logger.debug("Git calibration capture_before failed", exc_info=True)
        tracker.mark_detection_truncated()
        return None


async def _track_git_calibrate_after(
    calibrator: GitDiffCalibrator,
    same_call_paths: set[str],
) -> list[_ShellObservation]:
    """Collect Git candidates not emitted by this call's targeted detector."""

    def _calibrate() -> list[_ShellObservation]:
        return [
            _ShellObservation(
                change.path,
                change.operation,
                change.before_data,
                calibrated=True,
                before_skip=change.before_skip,
                before_size=change.before_size,
                before_symlink=change.before_symlink,
                before_symlink_dir=change.before_symlink_dir,
            )
            for change in calibrator.detect_implicit_changes(same_call_paths)
        ]

    try:
        return await asyncio.get_running_loop().run_in_executor(None, _calibrate)
    except Exception:
        logger.debug("Git calibration detect_implicit failed", exc_info=True)
        calibrator.mark_detection_truncated()
        return []


def _observation_key(path: str) -> str:
    """Merge key that resolves the directory route but not the entry itself.

    Resolving the directory keeps aliased routes to one file merged
    (``/tmp`` vs ``/private/tmp``, symlinked parents), while the lexical
    final component keeps a symlink and its destination as the distinct
    endpoints they are — ``canonical_key`` would collapse a retargeted
    link onto the file it points at and silently drop one observation.
    """
    parent, name = os.path.split(os.path.normpath(os.path.abspath(path)))
    try:
        resolved = os.path.realpath(parent)
    except OSError:
        resolved = parent
    return fold_case(os.path.join(resolved, name))


def _record_shell_observations(
    tracker: MutationTracker,
    observations: list[_ShellObservation],
    call_id: str,
    tool_operation_id: str | None = None,
) -> list[FileMutation]:
    """Merge one command's observations and record every resulting endpoint."""
    from chrys.service.mutations.types import MutationSource

    merged: dict[str, _ShellObservation] = {}
    for observation in observations:
        key = _observation_key(observation.path)
        if key not in merged or (not observation.calibrated and merged[key].calibrated):
            merged[key] = observation

    mutations: list[FileMutation] = []
    for observation in merged.values():
        if observation.calibrated:
            mutation = tracker.record_calibrated(
                observation.path,
                observation.operation,
                observation.before_data,
                call_id,
                tool_operation_id=tool_operation_id,
                before_skip=observation.before_skip,
                before_size=observation.before_size,
                before_symlink=observation.before_symlink,
                before_symlink_dir=observation.before_symlink_dir,
            )
        else:
            mutation = tracker.record(
                observation.path,
                observation.operation,
                MutationSource.SHELL,
                call_id,
                tool_operation_id=tool_operation_id,
            )
        if mutation is not None:
            mutations.append(mutation)
    return mutations


def _read_file_snapshot(tracker: MutationTracker, mutation: FileMutation) -> tuple[str, str]:
    """Read before/after blob content for a file mutation.

    Returns ``(before_text, after_text)``.  Called in an executor thread.
    """
    from chrys.foundation.text.encoding import decode_bytes

    store = tracker.store
    before_bytes = store.read_blob(mutation.before_hash) if mutation.before_hash else None
    after_bytes = store.read_blob(mutation.after_hash) if mutation.after_hash else None
    before_text = decode_bytes(before_bytes) if before_bytes else ""
    after_text = decode_bytes(after_bytes) if after_bytes else ""
    return (before_text, after_text)


def _content_changed(
    before_hash: str | None,
    after_hash: str | None,
    before_skip: SnapshotSkipReason | None,
    after_skip: SnapshotSkipReason | None,
) -> bool:
    """Whether a mutation changed file content, given possibly-withheld backups.

    Hash inequality proves change; a policy skip on either side means
    change cannot be ruled out (the content was never backed up, so both
    hashes read ``None``) — report changed rather than silently dropping
    a real edit as a no-op.
    """
    if before_skip is not None or after_skip is not None:
        return True
    return before_hash != after_hash


def _read_shell_snapshots(
    tracker: MutationTracker, mutations: list[FileMutation]
) -> dict[str, FileMutationTextSnapshot]:
    """Read before/after blob content for shell-detected mutations.

    Returns decoded snapshot payloads keyed by path.  Collapses
    multi-mutations per path, preserving the first ``before_text``,
    first ``before_hash``, and first operation.  Called in an executor
    thread.

    Mutations known ``FOREIGN`` at this point are not surfaced as live
    rows (they are a peer session's work), but a surviving local row on
    a foreign-touched path is marked ``contested`` — mixed paths keep
    the badge.  ``provenance`` collapses sticky-assumed and
    ``contested`` sticky-True across multi-mutations.
    """
    foreign_touched: set[str] = set()
    for m in mutations:
        if m.provenance is MutationProvenance.FOREIGN:
            foreign_touched.add(m.path)
            if m.old_path:
                foreign_touched.add(m.old_path)

    result: dict[str, FileMutationTextSnapshot] = {}
    for m in mutations:
        if m.provenance is MutationProvenance.FOREIGN:
            continue
        provenance = m.provenance.value if m.provenance else ""
        contested = m.contested or m.path in foreign_touched
        before_text, after_text = _read_file_snapshot(tracker, m)
        # Preserve the first before_text per path (collapse multi-mutations)
        if m.path in result:
            original = result[m.path]
            source = (
                MutationSource.IMPLICIT.value
                if original.source == MutationSource.IMPLICIT.value or m.source is MutationSource.IMPLICIT
                else original.source or m.source.value
            )
            if MutationProvenance.ASSUMED.value in (original.provenance, provenance):
                provenance = MutationProvenance.ASSUMED.value
            result[m.path] = FileMutationTextSnapshot(
                before_text=original.before_text,
                after_text=after_text,
                operation=original.operation,
                bytes_changed=_content_changed(original.before_hash, m.after_hash, original.before_skip, m.after_skip),
                source=source,
                before_hash=original.before_hash,
                after_hash=m.after_hash,
                before_skip=original.before_skip,
                after_skip=m.after_skip,
                provenance=provenance,
                contested=original.contested or contested,
            )
        else:
            result[m.path] = FileMutationTextSnapshot(
                before_text=before_text,
                after_text=after_text,
                operation=m.operation.value,
                bytes_changed=_content_changed(m.before_hash, m.after_hash, m.before_skip, m.after_skip),
                source=m.source.value,
                before_hash=m.before_hash,
                after_hash=m.after_hash,
                before_skip=m.before_skip,
                after_skip=m.after_skip,
                provenance=provenance,
                contested=contested,
            )
    return result


# ---------------------------------------------------------------------------
# High-level pipeline (shared by ToolEventMiddleware / SubAgentEventMiddleware)
# ---------------------------------------------------------------------------


async def prepare_mutation_tracking(
    tracker: MutationTracker | None,
    tool_name: str,
    args: dict,
    call_id: str,
    is_shell: bool,
    workspace_cwd: str | None = None,
    coordinator: MutationCoordinator | None = None,
    tool_operation_id: str | None = None,
) -> MutationContext:
    """Capture pre-execution mutation state.

    Call this before publishing the start event and executing the tool.
    """
    ctx = MutationContext(workspace_cwd=workspace_cwd, tool_operation_id=tool_operation_id)
    if tracker is None:
        return ctx

    ctx.implicit_detection = is_shell or tool_name in _IMPLICIT_WRITE_TOOLS
    loop = asyncio.get_running_loop()

    # Advertise the observation window FIRST — before git capture, shell
    # pre-scan, or any snapshot — so the published window is a superset
    # of the observation.  A peer write in a pre-scan gap would be
    # attributed by the diff yet never overlap our interval otherwise.
    if coordinator is not None and (is_shell or tool_name in _IMPLICIT_WRITE_TOOLS):
        raw_cwd = args.get("working_dir") or args.get("cwd") or workspace_cwd
        cwd = resolve_workspace_path(raw_cwd, base_cwd=workspace_cwd) if raw_cwd else resolve_workspace_path(".")
        scope = _git_calibration_scope_paths(cwd, workspace_cwd)
        ctx.window_opened_at = time.time()
        # A cancellation landing exactly on this await abandons the
        # executor call, but the thread may still open (or already have
        # opened) the window — with the id lost to every abort path, it
        # would advertise a live command until shutdown.  The handshake
        # makes exactly one side close it: the thread publishes the id
        # under the lock unless the await was abandoned first, in which
        # case the thread closes its own window.
        window_lock = threading.Lock()
        window_state: dict[str, str | bool | None] = {"id": None, "abandoned": False}

        def _open_window() -> str | None:
            wid = coordinator.open_window(scope, fallback_root=workspace_cwd)
            with window_lock:
                if not window_state["abandoned"]:
                    window_state["id"] = wid
                    return wid
            coordinator.close_window(wid)
            return None

        try:
            ctx.window_id = await loop.run_in_executor(None, _open_window)
        except BaseException:
            with window_lock:
                window_state["abandoned"] = True
                orphaned = window_state["id"]
            if isinstance(orphaned, str):
                try:
                    coordinator.close_window(orphaned)
                except Exception:
                    logger.debug("Orphaned window close failed", exc_info=True)
            raise

    # Everything below the window open runs guarded: a cancellation
    # landing on any of these awaits would propagate before the caller
    # ever owns the MutationContext, so no middleware abort could reach
    # the just-opened window (or the armed trace wrapper) — clean up
    # here and re-raise.
    try:
        # Layer 3: allocate the fsatrace write-trace and hand the wrapper
        # argv to the shell tool via contextvar (tool schema and args stay
        # untouched).  ShellTools has no session-dir access — this is the
        # handoff point.  The first call pays a one-shot binary probe.
        if is_shell:
            allocation = await loop.run_in_executor(None, allocate_trace_file, tracker.store.mutations_dir)
            if allocation is not None:
                wrapper_prefix, trace_file = allocation
                ctx.trace_file = trace_file
                ctx.trace_token = shell_trace_argv.set(tuple(wrapper_prefix))

        # File mutation snapshot (write_file / edit_file)
        if tool_name in _FILE_TOOLS:
            ctx.file_mutation = await loop.run_in_executor(
                None, _track_file_mutation_before, tracker, tool_name, args, call_id, workspace_cwd, tool_operation_id
            )
            if ctx.file_mutation is not None:
                # Tight write-interval lower bound: record() runs pre-write.
                ctx.file_mutation.t_start = ctx.file_mutation.timestamp

        # Git calibration + shell heuristic scan
        if is_shell or tool_name in _IMPLICIT_WRITE_TOOLS:
            ctx.git_calibrator = await _track_git_capture_before(tracker, args, workspace_cwd)
            if is_shell:
                ctx.shell_scan_before = await loop.run_in_executor(
                    None, _track_shell_scan_before, tracker, args, workspace_cwd
                )
    except BaseException:
        abort_mutation_tracking(ctx, coordinator)
        raise

    return ctx


async def finalize_mutation_tracking(
    tracker: MutationTracker | None,
    ctx: MutationContext,
    call_id: str,
    coordinator: MutationCoordinator | None = None,
    tool_errored: bool = False,
) -> MutationResult:
    """Run post-execution mutation detection and read snapshots.

    Call this after tool execution completes (in the ``finally`` block).
    Skipped when the tool was rejected by approval.

    ``tool_errored`` marks a tool whose call raised/failed: the mutation
    record still exists (record-in-prepare + finalize-in-``finally``
    leaves one even for failed calls), but chrys may not have authored
    the observed delta — such write/edit mutations publish no claim.
    """
    result = MutationResult()
    if tracker is None:
        return result

    loop = asyncio.get_running_loop()

    # Capture after-content for file mutations (for DiffView replay)
    if ctx.file_mutation is not None:
        await loop.run_in_executor(None, tracker.record_after, ctx.file_mutation)
        # Tight write-interval upper bound: the disk write happened
        # during tool execution, strictly before this stamp.
        ctx.file_mutation.t_end = time.time()
        snapshot = await loop.run_in_executor(None, _read_file_snapshot, tracker, ctx.file_mutation)
        if snapshot:
            result.file_snapshot = snapshot
            result.file_operation = ctx.file_mutation.operation.value
            result.file_bytes_changed = _content_changed(
                ctx.file_mutation.before_hash,
                ctx.file_mutation.after_hash,
                ctx.file_mutation.before_skip,
                ctx.file_mutation.after_skip,
            )
            result.file_hashes = FileHashDiff(
                before=ctx.file_mutation.before_hash,
                after=ctx.file_mutation.after_hash,
                before_skip=ctx.file_mutation.before_skip,
                after_skip=ctx.file_mutation.after_skip,
            )

    # Collect per-command observations before recording. Targeted scan rows
    # own the worktree endpoint; Git fills only paths that scan did not see.
    observations: list[_ShellObservation] = []
    if ctx.shell_scan_before is not None:
        try:
            observations = await loop.run_in_executor(None, _track_shell_scan_after, ctx.shell_scan_before)
        except OSError:
            logger.debug("Shell after-scan failed", exc_info=True)
            tracker.mark_detection_truncated()

    if ctx.git_calibrator is not None:
        same_call_paths = {observation.path for observation in observations}
        observations.extend(await _track_git_calibrate_after(ctx.git_calibrator, same_call_paths))
        if ctx.git_calibrator.detection_truncated:
            tracker.mark_detection_truncated()

    shell_mutations = await loop.run_in_executor(
        None, _record_shell_observations, tracker, observations, call_id, ctx.tool_operation_id
    )
    if shell_mutations:
        logger.debug("Mutation detection: %d file changes from shell command", len(shell_mutations))

    # Window-detected mutations get the full observation window as their
    # write interval — the point timestamp sits post-window and must not
    # be used for overlap matching.
    if shell_mutations and ctx.window_opened_at is not None:
        stamp = time.time()
        for m in shell_mutations:
            m.t_start = ctx.window_opened_at
            m.t_end = stamp

    # Layer 3: positive trace evidence upgrades this call's window
    # inferences to PROVEN — before persistence, display, or claims.
    proven_shell: list[FileMutation] = []
    if ctx.trace_file is not None:
        proven_shell = await loop.run_in_executor(None, _apply_trace_upgrades, ctx, shell_mutations)
    _release_trace_wrapper(ctx)

    if coordinator is not None:
        await loop.run_in_executor(
            None, _coordinate_after_detection, tracker, ctx, coordinator, tool_errored, proven_shell
        )

    if shell_mutations:
        result.shell_snapshots = await loop.run_in_executor(None, _read_shell_snapshots, tracker, shell_mutations)

    return result


def _coordinate_after_detection(
    tracker: MutationTracker,
    ctx: MutationContext,
    coordinator: MutationCoordinator,
    tool_errored: bool,
    proven_shell: list[FileMutation],
) -> None:
    """Registry protocol tail of finalize: close window, claim, reclassify.

    Post-detection first (the caller just ran it), then the window
    closes and claims for proven mutations publish — write/edit claims
    thus appear within milliseconds of the bytes hitting disk.  Errors
    never propagate: coordination must not break tool execution.

    Trace-proven shell rows publish regardless of ``tool_errored``: the
    positive trace IS the authorship evidence (a failing build still
    wrote what it wrote).  The tool-success gate exists only for
    write/edit rows, whose authorship is otherwise assumed from the
    observed delta.
    """
    try:
        if ctx.file_mutation is not None and not tool_errored:
            coordinator.publish_claims([ctx.file_mutation], fallback_root=ctx.workspace_cwd)
        if proven_shell:
            coordinator.publish_claims(proven_shell, fallback_root=ctx.workspace_cwd)
        coordinator.close_window(ctx.window_id)
        coordinator.reclassify(tracker, fallback_root=ctx.workspace_cwd)
    except Exception:
        logger.debug("Mutation coordination failed in finalize", exc_info=True)


def _apply_trace_upgrades(ctx: MutationContext, shell_mutations: list[FileMutation]) -> list[FileMutation]:
    """Upgrade window inferences positively observed in this call's trace.

    Evidence-only: matches against the rows detection already produced
    and never adds mutations — out-of-scope writes (gitignored build
    output) stay untracked by policy.  A MOVE row matches on either
    side.  The trace file is deleted afterwards regardless of outcome,
    and any failure degrades to "no upgrades".  Returns the upgraded
    rows (they become Layer-2 claims).

    Claims ``ctx.trace_file`` (nulls it) at entry: a cancellation can
    leave this executor call racing the middleware's sync abort, and on
    Windows the abort's unlink can lose to our open handle — claiming
    first means the abort skips the discard entirely and this
    ``finally`` keeps a path to delete after the handle closes.
    """
    trace_file = ctx.trace_file
    ctx.trace_file = None
    upgraded: list[FileMutation] = []
    if not trace_file:
        # The abort claimed (and discarded) it first.
        return upgraded
    try:
        candidates: dict[str, list[FileMutation]] = {}
        for m in shell_mutations:
            candidates.setdefault(canonical_key(m.path), []).append(m)
            if m.old_path:
                candidates.setdefault(canonical_key(m.old_path), []).append(m)
        if candidates:
            hit_keys = parse_trace_write_keys(trace_file, candidates.keys())
            seen: set[int] = set()
            for key in hit_keys:
                for m in candidates[key]:
                    if id(m) in seen:
                        continue
                    seen.add(id(m))
                    m.provenance = MutationProvenance.PROVEN
                    upgraded.append(m)
    except Exception:
        logger.debug("Trace upgrade failed", exc_info=True)
    finally:
        discard_trace_file(trace_file)
    return upgraded


def _release_trace_wrapper(ctx: MutationContext) -> None:
    """Reset the shell wrapper contextvar set by prepare (idempotent)."""
    if ctx.trace_token is None:
        return
    try:
        shell_trace_argv.reset(ctx.trace_token)
    except ValueError:
        # Defensive: reset from a different context — the var dies with
        # the task's context anyway.
        logger.debug("Trace wrapper token reset outside its context", exc_info=True)
    ctx.trace_token = None


def abort_mutation_tracking(
    ctx: MutationContext,
    coordinator: MutationCoordinator | None,
    *,
    tracker: MutationTracker | None = None,
) -> None:
    """Sync cleanup for calls that skip the async finalize (cancel/reject).

    When the agent task is cancelled (interrupt), the middleware cannot
    run the async finalize — a cancelled task raises ``CancelledError``
    at its next ``await``; a tool rejected at approval never executed and
    deliberately skips finalize — so this must stay synchronous.  It
    releases the Layer-3 trace wrapper (contextvar + trace file) and
    closes the observation window; without the latter, a window opened
    in prepare keeps advertising a live command in the registry until
    shutdown, causing false peer-active warnings.  The window end stamps
    at the abort instant, which is exactly the upper bound of when an
    interrupted tool could still have written.  No claims publish
    (authorship of any delta is unknowable here) and errors never
    propagate.

    ``tracker`` is passed only from abort sites where the command may have
    actually run (cancellation mid-execution or mid-finalize — never
    approval rejection or the prepare phase): implicit detection died with
    the call, so a write it performed before the interrupt would otherwise
    vanish from the notice entirely — no mutation row, and absorbed by the
    baseline the finalizer still captures.  Marking the turn's detection
    truncated surfaces that honestly.  The gate is the call class
    (``implicit_detection``), not which artifacts armed: an executed
    shell with zero artifacts (coordination off, non-Git cwd, opaque
    command, no fsatrace) has exactly the same blind spot.  File tools
    stay unmarked — their explicit mutation row survives the abort.
    """
    if tracker is not None and ctx.implicit_detection:
        try:
            tracker.mark_detection_truncated()
        except Exception:
            logger.debug("Detection-truncated marking failed during abort", exc_info=True)
    _release_trace_wrapper(ctx)
    # Claim-then-discard, mirroring _apply_trace_upgrades: whichever side
    # claims the path first owns its deletion; the other sees None.
    trace_file = ctx.trace_file
    ctx.trace_file = None
    discard_trace_file(trace_file)
    if coordinator is None or ctx.window_id is None:
        return
    try:
        coordinator.close_window(ctx.window_id)
    except Exception:
        logger.debug("Mutation coordination window abort failed", exc_info=True)
