# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace baselines, external-change detection, and notice rendering."""

from __future__ import annotations

import errno
import logging
import os
import stat
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path, PurePath
from typing import TYPE_CHECKING, Any, cast

from chrys.foundation.platform.paths import display_relpath
from chrys.foundation.text.model_json import model_path
from chrys.service.mutations.git_state import (
    GIT_PATH_LIMIT,
    GitDeltaEntry,
    canonical_git_pathspecs,
    canonical_path,
    entry_display_path,
    entry_path,
    lexical_display_path,
    read_git_head,
    read_git_head_delta,
    read_git_status,
    resolve_git_root,
)
from chrys.service.mutations.scanner import DEFAULT_EXCLUDES, WorkspaceScanner
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.types import FileHashDiff, MutationOp, MutationProvenance, SnapshotSkipReason

if TYPE_CHECKING:
    from chrys.foundation.models.workspace import Workspace
    from chrys.service.mutations.tracker import MutationTracker

logger = logging.getLogger(__name__)

_MAX_CAPTURE_ROOTS = 32
_CAPTURE_DEADLINE_SECONDS = 15.0
_SCAN_OVERFLOW_SENTINEL = 5_001
_DIAGNOSTIC_LINE_LIMIT = 8
_SERIALIZATION_VERSION = 1
_NOTICE_TIMEOUT_WARNING = (
    "Workspace change detection could not be completed for this turn. Files may "
    "have been changed outside this session without being reported, and later "
    "notices will not include them. Re-read externally relevant files before "
    "relying on their remembered contents."
)


class BaselineMode(Enum):
    """How a workspace root is observed."""

    GIT = "git"
    SCAN = "scan"


class DegradedReason(Enum):
    """Why a complete comparable root baseline is unavailable."""

    TOO_MANY_FILES = "too_many_files"
    CAPTURE_FAILED = "capture_failed"
    CAPTURE_DEADLINE = "capture_deadline"
    MISSING_BASELINE = "missing_baseline"


class ExternalOp(Enum):
    """Operation shown in the external-change section."""

    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"
    RECONCILED = "reconciled"
    TRACKING_CHANGED = "tracking_changed"


@dataclass(frozen=True, slots=True)
class BaselineFileState:
    """Filesystem and Git state for one observed path."""

    exists: bool
    mtime_ns: int
    size: int
    mode: int | None
    statuses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RootBaseline:
    """One complete root baseline."""

    root: str
    mode: BaselineMode
    head: str | None
    branch: str | None
    scope: tuple[str, ...]
    files: Mapping[str, BaselineFileState]


@dataclass(frozen=True, slots=True)
class DegradedBaseline:
    """One incomplete root capture."""

    reason: DegradedReason
    mode: BaselineMode | None
    scope: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkspaceBaseline:
    """Complete published workspace state for a turn boundary."""

    turn_id: int
    roots: Mapping[str, RootBaseline]
    degraded: Mapping[str, DegradedBaseline]
    root_limit_omitted: int = 0


@dataclass(frozen=True, slots=True)
class AgentChange:
    """One agent-attributed change ready for rendering."""

    path: str
    operation: MutationOp
    content_not_compared: bool = False
    contested: bool = False


@dataclass(frozen=True, slots=True)
class PeerChange:
    """One peer-session write observed by this session's mutation log.

    Peer writes land before the end-of-turn baseline capture, so the
    next external diff cannot see them — the turn log is the only
    witness and these rows must be carried into the notice directly.
    """

    path: str
    operation: MutationOp


@dataclass(frozen=True, slots=True)
class ExternalChange:
    """One externally observed change ready for rendering."""

    root: str
    path: str
    operation: ExternalOp
    agent_overlap: bool = False


@dataclass(frozen=True, slots=True)
class HeadMove:
    """One scoped repository HEAD movement."""

    root: str
    old_head: str | None
    new_head: str | None
    uncertain: bool = False


@dataclass(frozen=True, slots=True)
class WorkspaceChangeNotice:
    """Bounded notice payload independent of rendering cwd."""

    agent_changes: tuple[AgentChange, ...] = ()
    agent_omitted: int = 0
    external_changes: tuple[ExternalChange, ...] = ()
    external_omitted: int = 0
    peer_changes: tuple[PeerChange, ...] = ()
    peer_omitted: int = 0
    head_moves: tuple[HeadMove, ...] = ()
    degraded: tuple[tuple[str, DegradedBaseline], ...] = ()
    root_limit_omitted: int = 0
    agent_detection_truncated: bool = False


@dataclass(frozen=True, slots=True)
class _CaptureRoot:
    root: str
    mode: BaselineMode | None
    scope: tuple[str, ...]
    exclude_roots: frozenset[str] = frozenset()
    unavailable_reason: DegradedReason | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedScope:
    roots: tuple[_CaptureRoot, ...]
    root_limit_omitted: int = 0
    display_roots: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkspaceRetarget:
    """Resolved workspace values ready to install without filesystem access."""

    scope: _ResolvedScope
    resolve_scope: bool
    new_cwd: str | None
    canonical_new_cwd: str | None
    source_baseline: WorkspaceBaseline | None = None
    baseline: WorkspaceBaseline | None = None
    source_generation: int | None = None


@dataclass(frozen=True, slots=True)
class _SafetyNotice:
    """A queued safety notice plus the cwd its relative paths were rendered
    against; ``None`` once the base is unknown or already annotated."""

    text: str
    cwd: str | None = None
    canonical_cwd: str | None = None


class WorkspaceChangeTracker:
    """Generation-guarded workspace state owned by one engine."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._capture_gate = threading.Lock()
        self._generation = 0
        self._scope = _ResolvedScope(roots=())
        self._baseline: WorkspaceBaseline | None = None
        self._pending_boundary_notice: str | None = None
        self._pending_safety: list[_SafetyNotice] = []

    @property
    def baseline(self) -> WorkspaceBaseline | None:
        """Return the immutable current baseline."""
        with self._lock:
            return self._baseline

    def resolve_retarget(
        self, workspace: Workspace | None, *, resolve_scope: bool = True, freeze_baseline: bool = False
    ) -> WorkspaceRetarget:
        """Resolve candidate scope and directory identity without writing live state.

        Args:
            resolve_scope: ``False`` when the change notice is disabled: the
                scope is then left inactive (empty) without probing the roots
                at all, and the baseline is kept as it is rather than
                reconciled against that empty scope. Resolution asks Git about
                every root — a synchronous subprocess per root, on the
                caller's thread — and nothing reads the scope while the
                notice is off; re-enabling the setting rebuilds the runtime,
                which retargets again with resolution on and reconciles the
                kept baseline then. Safety notices are rebased regardless:
                they are delivered whether or not the notice is enabled.
        """
        source_baseline = source_generation = None
        if freeze_baseline:
            with self._lock:
                source_baseline = self._baseline
                source_generation = self._generation
        scope = _resolve_workspace_scope(workspace) if resolve_scope else _ResolvedScope(roots=())
        baseline = (
            _reconcile_baseline(source_baseline, scope)
            if resolve_scope and source_baseline is not None
            else source_baseline
        )
        new_cwd, cwd_identity = _notice_cwd(workspace)
        return WorkspaceRetarget(
            scope=scope,
            resolve_scope=resolve_scope,
            new_cwd=new_cwd,
            canonical_new_cwd=cwd_identity,
            source_baseline=source_baseline,
            baseline=baseline,
            source_generation=source_generation,
        )

    def retarget_is_current(self, retarget: WorkspaceRetarget) -> bool:
        """Whether the candidate still describes the live baseline at install time."""
        if retarget.source_generation is None:
            return True
        with self._lock:
            return self._baseline is retarget.source_baseline and self._generation == retarget.source_generation

    def apply_retarget(self, retarget: WorkspaceRetarget) -> None:
        """Install the resolved workspace values and rebase queued notices in memory."""
        with self._lock:
            old_baseline = self._baseline
            self._generation += 1
            self._scope = retarget.scope
            self._pending_boundary_notice = None
            # Safety notices deliberately survive a workspace switch, but
            # their relative paths were rendered against the old cwd —
            # pin the base explicitly so the model does not resolve them
            # against the new workspace.
            self._pending_safety = [
                _rebase_safety_notice(chunk, retarget.new_cwd, retarget.canonical_new_cwd)
                for chunk in self._pending_safety
            ]
            if old_baseline is None or not retarget.resolve_scope:
                return
            self._baseline = (
                _reconcile_baseline(old_baseline, retarget.scope)
                if retarget.source_generation is None
                else retarget.baseline
            )

    def reset_for_restart(self) -> None:
        """Clear session-owned state while preserving the tracker instance."""
        with self._lock:
            self._generation += 1
            self._scope = _ResolvedScope(roots=())
            self._baseline = None
            self._pending_boundary_notice = None
            self._pending_safety = []

    def invalidate(self) -> None:
        """Invalidate the external baseline without clearing safety delivery."""
        with self._lock:
            self._generation += 1
            self._baseline = None
            self._pending_boundary_notice = None

    def capture_baseline(self, turn_id: int) -> WorkspaceBaseline:
        """Capture and publish a complete turn-end baseline."""
        if not self._capture_gate.acquire(blocking=False):
            return self._install_capture_deadline(turn_id)
        try:
            with self._lock:
                generation = self._generation
                scope = self._scope
            result = _capture_workspace(turn_id, scope, deadline=time.monotonic() + _CAPTURE_DEADLINE_SECONDS)
            with self._lock:
                if generation == self._generation and scope == self._scope:
                    self._baseline = result
            return result
        finally:
            self._capture_gate.release()

    def capture_timed_out(self, turn_id: int) -> None:
        """Supersede an uninterruptible capture worker after its async deadline."""
        self._install_capture_deadline(turn_id)

    def capture_cancelled(self) -> None:
        """Reject a capture result whose awaiting lifecycle was cancelled."""
        self.invalidate()

    def compute_turn_notice(
        self,
        *,
        turn_id: int,
        mutation_tracker: MutationTracker | None,
        agent_turn_id: int | None,
        overlap_turn_id: int | None,
        cwd: str,
        max_entries: int,
        recovered: bool = False,
        latest_agent_turn: int | None = None,
    ) -> str | None:
        """Compute and publish the next one-shot boundary notice."""
        with self._lock:
            generation = self._generation
            scope = self._scope
            baseline = self._baseline

        agent_changes, agent_truncated = _agent_changes(mutation_tracker, agent_turn_id)
        # Retry omits the agent section (``agent_turn_id`` is None) but must
        # not drop the interrupted turn's truncation flag with it — the
        # unobserved writes it stands for get absorbed by the baseline, so
        # this warning is their only surface.
        agent_truncated = agent_truncated or _detection_truncated(mutation_tracker, latest_agent_turn)
        overlap = _mutation_paths(mutation_tracker, overlap_turn_id, overlap_turn_id)
        external: list[ExternalChange] = []
        head_moves: list[HeadMove] = []
        degraded: dict[str, DegradedBaseline] = {}
        root_limit_omitted = baseline.root_limit_omitted if baseline is not None else 0

        if baseline is not None:
            if not self._capture_gate.acquire(blocking=False):
                degraded = {
                    root.root: DegradedBaseline(DegradedReason.CAPTURE_DEADLINE, root.mode, root.scope)
                    for root in scope.roots
                }
                with self._lock:
                    self._generation += 1
                    generation = self._generation
            else:
                deadline = time.monotonic() + _CAPTURE_DEADLINE_SECONDS
                try:
                    current = _capture_workspace(turn_id, scope, deadline=deadline)
                    external, head_moves, degraded = _diff_workspace(baseline, current, deadline=deadline)
                    root_limit_omitted = current.root_limit_omitted
                finally:
                    self._capture_gate.release()

            if recovered:
                if mutation_tracker is None:
                    degraded["recovery"] = DegradedBaseline(
                        DegradedReason.MISSING_BASELINE,
                        None,
                        (),
                    )
                    external = []
                else:
                    # Subtract only rows the recovered log fully explains: a
                    # recovered mutation names the path AND current disk
                    # provably matches its recorded endpoint. Anything else —
                    # a post-crash edit on top of the agent's write, or an
                    # unverifiable endpoint — stays visible as external.
                    endpoints = _recovered_endpoints(
                        mutation_tracker,
                        baseline.turn_id + 1,
                        latest_agent_turn,
                    )
                    max_verify_bytes = mutation_tracker.store.policy.max_file_bytes
                    external = [
                        entry
                        for entry in external
                        if not _matches_recovered_endpoint(
                            endpoints.get(entry_path(entry.path)), entry.path, max_verify_bytes
                        )
                    ]

        agent_changes.sort(key=lambda entry: canonical_path(entry.path))
        bounded_agent = tuple(agent_changes[:max_entries])

        peer_changes = _peer_witnessed_changes(mutation_tracker, latest_agent_turn)
        if peer_changes:
            # The current diff (fresher state) and the agent rows that will
            # actually be displayed (whose contested badge already names the
            # peer) both outrank a raw peer row for the same path. Rows folded
            # into the "...and N more" overflow never render their badge, so
            # they must not suppress the peer fallback.
            covered = {entry_path(entry.path) for entry in external}
            covered.update(entry_path(entry.path) for entry in bounded_agent)
            peer_changes = [change for change in peer_changes if entry_path(change.path) not in covered]
            peer_changes = [replace(change, path=_display_path(change.path, scope)) for change in peer_changes]
            peer_changes.sort(key=lambda change: canonical_path(change.path))

        external = [replace(entry, agent_overlap=entry_path(entry.path) in overlap) for entry in external]
        external = [
            replace(
                entry,
                root=_display_path(entry.root, scope),
                path=_display_path(entry.path, scope),
            )
            for entry in external
        ]
        head_moves = [replace(move, root=_display_path(move.root, scope)) for move in head_moves]
        degraded = {_display_path(root, scope): value for root, value in degraded.items()}
        external.sort(key=lambda entry: (entry.root, canonical_path(entry.path)))
        bounded_external = tuple(external[:max_entries])
        bounded_peer = tuple(peer_changes[:max_entries])
        notice = WorkspaceChangeNotice(
            agent_changes=tuple(replace(entry, path=_display_path(entry.path, scope)) for entry in bounded_agent),
            agent_omitted=max(0, len(agent_changes) - len(bounded_agent)),
            external_changes=bounded_external,
            external_omitted=max(0, len(external) - len(bounded_external)),
            peer_changes=bounded_peer,
            peer_omitted=max(0, len(peer_changes) - len(bounded_peer)),
            head_moves=tuple(head_moves),
            degraded=tuple(sorted(degraded.items())),
            root_limit_omitted=root_limit_omitted,
            agent_detection_truncated=agent_truncated,
        )
        rendered = format_workspace_change_notice(notice, cwd=cwd)
        with self._lock:
            if generation == self._generation and scope == self._scope:
                self._pending_boundary_notice = rendered
        return rendered

    def notice_cancelled(self) -> None:
        """Reject a late notice worker while retaining the saved baseline."""
        with self._lock:
            self._generation += 1
            self._pending_boundary_notice = None

    def notice_timed_out(self) -> None:
        """Reject a late notice worker and warn that the comparison is lost.

        Unlike cancellation — where the turn unwinds and the retained
        baseline gets compared again at the next boundary — a timed-out
        turn continues to finalization, whose fresh capture absorbs
        everything the comparison would have reported.  Queue a
        persistent safety caveat so that absorption is never silent.
        """
        self.notice_cancelled()
        self.queue_safety_notice(_NOTICE_TIMEOUT_WARNING)

    def clear_boundary_notice(self) -> None:
        """Clear only ordinary boundary state after an advisory failure."""
        with self._lock:
            self._pending_boundary_notice = None

    def queue_safety_notice(self, notice: str, *, cwd: str | None = None) -> None:
        """Queue an explicit safety warning independently of boundary compute.

        ``cwd`` records the base its relative paths were rendered against,
        so a later workspace switch can pin the base instead of letting
        the paths silently re-anchor to the new workspace.
        """
        if not notice:
            return
        base = str(Path(cwd).absolute()) if cwd else None
        chunk = _resolve_safety_notice(_SafetyNotice(notice, base))
        with self._lock:
            self._pending_safety.append(chunk)

    def requeue_notice(self, notice: str, *, cwd: str | None = None) -> None:
        """Preserve a notice drained for a pre-executor run that never reached the model."""
        self.queue_safety_notice(notice, cwd=cwd)

    def take_pending_notice(self) -> str | None:
        """Atomically drain safety first and boundary second exactly once."""
        with self._lock:
            safety_chunks = self._pending_safety
            boundary = self._pending_boundary_notice
            self._pending_safety = []
            self._pending_boundary_notice = None
        safety: str | None = None
        for chunk in safety_chunks:
            safety = _join_notices(safety, chunk.text)
        return _join_notices(safety, boundary)

    def serialize(self) -> dict[str, Any] | None:
        """Return the versioned payload: baseline plus undelivered safety notices.

        Boundary notices never persist — the next boundary recomputes them
        from the saved baseline. Safety notices are not recomputable (their
        source data is gone once queued: the rollback invalidated the
        baseline, or the requeued boundary's changes were already absorbed),
        so they ride the payload until a model request delivers them.
        """
        with self._lock:
            baseline = self._baseline
            pending_safety = list(self._pending_safety)
        if baseline is None and not pending_safety:
            return None
        payload = _serialize_baseline(baseline) if baseline is not None else {"version": _SERIALIZATION_VERSION}
        if pending_safety:
            payload["pending_safety"] = [{"text": chunk.text, "cwd": chunk.cwd} for chunk in pending_safety]
        return payload

    def restore(
        self,
        value: object,
        workspace: Workspace | None = None,
        *,
        resolve_scope: bool = True,
    ) -> None:
        """Restore a persisted payload and reconcile it with the freshly resolved scope.

        Args:
            resolve_scope: Same contract as :meth:`resolve_retarget` — ``False``
                skips the Git probes, installs the inactive (empty) scope in
                place of whatever the previous session left behind, and keeps
                the restored baseline exactly as persisted for the next
                enabled retarget to reconcile.
        """
        baseline = _deserialize_baseline(value)
        pending_safety = [_resolve_safety_notice(chunk) for chunk in _deserialize_pending_safety(value)]
        if not resolve_scope:
            scope: _ResolvedScope | None = _ResolvedScope(roots=())
        elif workspace is not None:
            scope = _resolve_workspace_scope(workspace)
        else:
            scope = None
        new_cwd, cwd_identity = _notice_cwd(workspace)
        with self._lock:
            self._generation += 1
            self._pending_boundary_notice = None
            # Undelivered safety notices are the sole record of their
            # warnings; rehydrate them, pinning the path base exactly as a
            # workspace switch would when the restored cwd differs from
            # queue time.
            self._pending_safety = [_rebase_safety_notice(chunk, new_cwd, cwd_identity) for chunk in pending_safety]
            if scope is not None:
                self._scope = scope
            self._baseline = (
                _reconcile_baseline(baseline, self._scope)
                if resolve_scope and baseline is not None and self._scope.roots
                else baseline
            )

    def install_restored_state(self, prepared: WorkspaceChangeTracker) -> None:
        """Install a restore's decoded state without reparsing or probing the filesystem."""
        with prepared._lock:
            scope, baseline = prepared._scope, prepared._baseline
            pending_safety = list(prepared._pending_safety)
        with self._lock:
            self._generation += 1
            self._scope = scope
            self._baseline = baseline
            self._pending_boundary_notice = None
            self._pending_safety = pending_safety

    def _install_capture_deadline(self, turn_id: int) -> WorkspaceBaseline:
        with self._lock:
            self._generation += 1
            scope = self._scope
            result = WorkspaceBaseline(
                turn_id=turn_id,
                roots={},
                degraded={
                    root.root: DegradedBaseline(DegradedReason.CAPTURE_DEADLINE, root.mode, root.scope)
                    for root in scope.roots
                },
                root_limit_omitted=scope.root_limit_omitted,
            )
            self._baseline = result
            return result


def format_workspace_change_notice(notice: WorkspaceChangeNotice, *, cwd: str) -> str | None:
    """Render a safe one-shot reminder, returning ``None`` for an empty payload."""
    has_content = bool(
        notice.agent_changes
        or notice.agent_omitted
        or notice.external_changes
        or notice.external_omitted
        or notice.peer_changes
        or notice.peer_omitted
        or notice.head_moves
        or notice.degraded
        or notice.root_limit_omitted
        or notice.agent_detection_truncated
    )
    if not has_content:
        return None

    lines = ["Workspace changes since the previous turn (contents not shown):"]
    if notice.agent_changes or notice.agent_omitted:
        lines.extend(("", "Changed by you or your sub-agents:"))
        for entry in notice.agent_changes:
            suffix = " [content not compared]" if entry.content_not_compared else ""
            if entry.contested:
                suffix += " [another session also wrote this; re-read it]"
            lines.append(f"- {_agent_label(entry.operation)}: {_quoted_path(entry.path, cwd)}{suffix}")
        if notice.agent_omitted:
            lines.append(f"- ...and {notice.agent_omitted} more")

    if notice.agent_detection_truncated:
        lines.extend(("", "Some agent file changes could not be determined completely."))

    if notice.external_changes or notice.external_omitted:
        lines.extend(("", "Changed outside your tool calls (by the user, another program or a background command):"))
        for entry in notice.external_changes:
            overlap = " [you also edited this; re-read it]" if entry.agent_overlap else ""
            lines.append(f"- {_external_label(entry.operation)}: {_quoted_path(entry.path, cwd)}{overlap}")
        if notice.external_omitted:
            lines.append(f"- ...and {notice.external_omitted} more")

    if notice.peer_changes or notice.peer_omitted:
        lines.extend(("", "Changed by another session working in this workspace:"))
        lines.extend(
            f"- {_agent_label(entry.operation)}: {_quoted_path(entry.path, cwd)}" for entry in notice.peer_changes
        )
        if notice.peer_omitted:
            lines.append(f"- ...and {notice.peer_omitted} more")

    diagnostics: list[str] = []
    for move in notice.head_moves:
        old = move.old_head[:8] if move.old_head else "unborn"
        new = move.new_head[:8] if move.new_head else "unborn"
        suffix = "; scoped files may differ" if move.uncertain else " (scoped files listed above)"
        diagnostics.append(f"git HEAD moved: {old} -> {new}{suffix}")
    for root, degraded in notice.degraded:
        diagnostics.append(
            f"Workspace state unavailable for {_quoted_path(root, cwd)}: {_degraded_label(degraded.reason)}."
        )
    root_limit_line = (
        f"{notice.root_limit_omitted} additional workspace root(s) were not inspected."
        if notice.root_limit_omitted
        else None
    )
    if diagnostics or root_limit_line:
        lines.append("")
        lines.extend(diagnostics[:_DIAGNOSTIC_LINE_LIMIT])
        if len(diagnostics) > _DIAGNOSTIC_LINE_LIMIT:
            lines.append(f"...and {len(diagnostics) - _DIAGNOSTIC_LINE_LIMIT} more workspace diagnostics")
        if root_limit_line:
            # The exact root-limit aggregate never competes with the capped
            # HEAD/degraded diagnostics.
            lines.append(root_limit_line)

    if notice.external_changes or notice.peer_changes:
        lines.extend(("", "Re-read externally changed files before relying on their contents."))
    return "\n".join(lines)


def format_retained_changes_notice(
    mutation_tracker: MutationTracker | None,
    *,
    cwd: str,
    max_entries: int,
) -> str | None:
    """Build the target-zero conversation-only rollback safety notice."""
    if mutation_tracker is None:
        return "Files retained from the discarded conversation:\n- Conversation history was reset without reverting the workspace."
    summary = mutation_tracker.get_session_file_summary()
    entries = [
        AgentChange(
            path=path,
            operation=_operation_from_summary(diff),
            content_not_compared=diff.content_unavailable,
            contested=diff.contested,
        )
        for path, diff in summary.items()
    ]
    entries.sort(key=lambda entry: canonical_path(entry.path))
    turns = mutation_tracker.get_all_turns()
    unverified = any(
        mutation.before_skip is not None or mutation.after_skip is not None
        for turn in turns
        for mutation in turn.mutations
        if mutation.provenance is not MutationProvenance.FOREIGN
    )
    # Truncated implicit detection may have missed changes entirely — the
    # safety notice must still warn even when zero rows were recorded.
    truncated = any(turn.detection_truncated for turn in turns)
    if not entries and not unverified and not truncated:
        return None
    lines = ["Files retained from the discarded conversation:"]
    for entry in entries[:max_entries]:
        suffix = " [content not compared]" if entry.content_not_compared else ""
        if entry.contested:
            suffix += " [another session also wrote this; re-read it]"
        lines.append(f"- {_agent_label(entry.operation)}: {_quoted_path(entry.path, cwd)}{suffix}")
    omitted = max(0, len(entries) - max_entries)
    if omitted:
        lines.append(f"- ...and {omitted} more")
    if unverified:
        lines.append("Some retained changes could not be verified because file content was not compared.")
    if truncated:
        lines.append("Some file changes from the discarded conversation could not be determined completely.")
    return "\n".join(lines)


def format_partial_revert_notice(
    retained_paths: Sequence[str],
    *,
    cwd: str,
    max_entries: int,
    detection_incomplete: bool = False,
) -> str | None:
    """Warn about rollback candidates that were not actually restored.

    A reverting rollback can retain files (a partial path selection, a
    peer-modified exclusion, a failed restore); the conversation no
    longer mentions those edits, so without this notice the model has no
    way to learn the files still carry them. ``detection_incomplete``
    means a rolled-back turn had truncated implicit detection: writes
    that were never recorded could not become candidates, so the notice
    must warn even when every known candidate restored.
    """
    if not retained_paths and not detection_incomplete:
        return None
    ordered = sorted(retained_paths, key=canonical_path)
    lines = ["Files not reverted by the rollback (they keep changes from the rolled-back conversation):"]
    lines.extend(f"- {_quoted_path(path, cwd)}" for path in ordered[:max_entries])
    omitted = max(0, len(ordered) - max_entries)
    if omitted:
        lines.append(f"- ...and {omitted} more")
    if detection_incomplete:
        lines.append(
            "Some file changes from the rolled-back conversation could not be determined completely, "
            "so the rollback may not have reverted all of them."
        )
    return "\n".join(lines)


def _resolve_workspace_scope(workspace: Workspace | None) -> _ResolvedScope:
    if workspace is None:
        return _ResolvedScope(roots=())
    raw_inputs = [workspace.primary_cwd, *(working_dir.path for working_dir in workspace.working_dirs)]
    inputs: list[tuple[str, str]] = []
    seen: set[str] = set()
    seen_stat_ids: set[tuple[int, int]] = set()
    for raw in raw_inputs:
        physical = canonical_path(raw)
        display = os.path.abspath(raw)
        if os.pardir in PurePath(raw).parts and canonical_path(display) != physical:
            display = os.path.realpath(raw)
        if physical in seen:
            continue
        seen.add(physical)
        # realpath keeps case aliases distinct on case-insensitive volumes
        # (normcase folds only on Windows) — stat identity is the physical
        # dedup key whenever the root exists.
        try:
            probe = os.stat(physical)
        except OSError:
            stat_id = None
        else:
            stat_id = (probe.st_dev, probe.st_ino)
        if stat_id is not None:
            if stat_id in seen_stat_ids:
                continue
            seen_stat_ids.add(stat_id)
        inputs.append((display, physical))
    omitted = max(0, len(inputs) - _MAX_CAPTURE_ROOTS)
    inputs = inputs[:_MAX_CAPTURE_ROOTS]
    deadline = time.monotonic() + _CAPTURE_DEADLINE_SECONDS

    git_groups: dict[str, list[str]] = {}
    git_root_by_input: dict[str, str] = {}
    non_git: list[str] = []
    unavailable: dict[str, DegradedReason] = {}
    for _display, physical in inputs:
        if not os.path.isdir(physical):
            unavailable[physical] = DegradedReason.CAPTURE_FAILED
            continue
        remaining = min(10.0, deadline - time.monotonic())
        if remaining <= 0:
            unavailable[physical] = DegradedReason.CAPTURE_DEADLINE
            continue
        git_root = resolve_git_root(physical, timeout=remaining)
        if git_root is None:
            if time.monotonic() >= deadline:
                unavailable[physical] = DegradedReason.CAPTURE_DEADLINE
                continue
            non_git.append(physical)
            continue
        rel = PurePath(os.path.relpath(physical, git_root)).as_posix()
        git_groups.setdefault(git_root, []).append("." if rel == "." else rel)
        git_root_by_input[physical] = git_root

    managed_git_roots = tuple(git_groups)
    scan_roots: list[str] = []
    for root in non_git:
        if any(_contains(existing, root) for existing in scan_roots):
            continue
        scan_roots = [existing for existing in scan_roots if not _contains(root, existing)]
        scan_roots.append(root)

    roots: list[_CaptureRoot] = []
    emitted: set[str] = set()
    for _display, physical in inputs:
        if physical in emitted:
            continue
        if physical in unavailable:
            roots.append(
                _CaptureRoot(
                    physical,
                    None,
                    (),
                    unavailable_reason=unavailable[physical],
                )
            )
            emitted.add(physical)
            continue
        matching_git = git_root_by_input.get(physical)
        if matching_git is not None:
            if matching_git not in emitted:
                roots.append(
                    _CaptureRoot(
                        matching_git,
                        BaselineMode.GIT,
                        canonical_git_pathspecs(git_groups[matching_git]),
                    )
                )
                emitted.add(matching_git)
            continue
        scan_root = next((root for root in scan_roots if _contains(root, physical)), None)
        if scan_root is not None and scan_root not in emitted:
            exclusions = frozenset(
                root for root in managed_git_roots if root != scan_root and _contains(scan_root, root)
            )
            roots.append(
                _CaptureRoot(
                    scan_root,
                    BaselineMode.SCAN,
                    tuple(sorted(exclusions)),
                    exclusions,
                )
            )
            emitted.add(scan_root)
    display_roots = [(physical, display) for display, physical in inputs]
    for git_root in git_groups:
        for display, physical in inputs:
            if git_root_by_input.get(physical) != git_root:
                continue
            # Ownership guarantees a shared ancestor, but the build path must
            # never break on a pathological spelling — skip instead of raising.
            try:
                ascent = os.path.relpath(git_root, physical)
            except ValueError:
                continue
            display_roots.append((git_root, os.path.normpath(os.path.join(display, ascent))))
            break
    return _ResolvedScope(
        roots=tuple(roots),
        root_limit_omitted=omitted,
        display_roots=tuple(display_roots),
    )


def _capture_workspace(turn_id: int, scope: _ResolvedScope, *, deadline: float) -> WorkspaceBaseline:
    roots: dict[str, RootBaseline] = {}
    degraded: dict[str, DegradedBaseline] = {}
    for index, capture_root in enumerate(scope.roots):
        if time.monotonic() >= deadline:
            for unfinished in scope.roots[index:]:
                degraded[unfinished.root] = DegradedBaseline(
                    DegradedReason.CAPTURE_DEADLINE,
                    unfinished.mode,
                    unfinished.scope,
                )
            break
        if capture_root.mode is None or not os.path.isdir(capture_root.root):
            degraded[capture_root.root] = DegradedBaseline(
                capture_root.unavailable_reason or DegradedReason.CAPTURE_FAILED,
                capture_root.mode,
                capture_root.scope,
            )
            continue
        try:
            if capture_root.mode is BaselineMode.GIT:
                root_baseline, reason = _capture_git_root(capture_root, deadline)
            else:
                root_baseline, reason = _capture_scan_root(capture_root, deadline)
        except Exception:
            logger.debug("Workspace root capture failed for %s", capture_root.root, exc_info=True)
            root_baseline, reason = None, DegradedReason.CAPTURE_FAILED
        if root_baseline is not None:
            roots[capture_root.root] = root_baseline
        else:
            degraded[capture_root.root] = DegradedBaseline(reason, capture_root.mode, capture_root.scope)
    return WorkspaceBaseline(
        turn_id=turn_id,
        roots=roots,
        degraded=degraded,
        root_limit_omitted=scope.root_limit_omitted,
    )


def _capture_git_root(
    capture_root: _CaptureRoot,
    deadline: float,
) -> tuple[RootBaseline | None, DegradedReason]:
    for _attempt in range(2):
        remaining = min(10.0, deadline - time.monotonic())
        if remaining <= 0:
            return None, DegradedReason.CAPTURE_DEADLINE
        before_head = read_git_head(capture_root.root, timeout=remaining)
        remaining = min(10.0, deadline - time.monotonic())
        if remaining <= 0:
            return None, DegradedReason.CAPTURE_DEADLINE
        status_result = read_git_status(
            capture_root.root,
            capture_root.scope,
            timeout=remaining,
            max_paths=GIT_PATH_LIMIT,
        )
        if status_result.failed:
            return None, DegradedReason.CAPTURE_FAILED
        if status_result.truncated:
            return None, DegradedReason.TOO_MANY_FILES
        files: dict[str, BaselineFileState] = {}
        for rel_path, statuses in status_result.statuses.items():
            # Lexical: the entry may be a tracked symlink whose identity
            # (and lstat) must be the link itself, not its destination.
            # Case-preserving so the notice can render the on-disk spelling.
            path = lexical_display_path(os.path.join(capture_root.root, rel_path))
            # Git already applied the pathspecs; reject only entries whose
            # normalized filesystem identity escapes the repository.
            if not _path_in_git_scope(capture_root.root, path, (".",)):
                continue
            files[path] = _stat_baseline_file(path, statuses)
        remaining = min(10.0, deadline - time.monotonic())
        if remaining <= 0:
            return None, DegradedReason.CAPTURE_DEADLINE
        after_head = read_git_head(capture_root.root, timeout=remaining)
        if before_head.head == after_head.head:
            return (
                RootBaseline(
                    root=capture_root.root,
                    mode=BaselineMode.GIT,
                    head=after_head.head,
                    branch=after_head.branch,
                    scope=capture_root.scope,
                    files=files,
                ),
                DegradedReason.CAPTURE_FAILED,
            )
    return None, DegradedReason.CAPTURE_FAILED


def _capture_scan_root(
    capture_root: _CaptureRoot,
    deadline: float,
) -> tuple[RootBaseline | None, DegradedReason]:
    scanner = WorkspaceScanner(
        capture_root.root,
        excludes=DEFAULT_EXCLUDES,
        max_depth=sys.maxsize,
        max_files=_SCAN_OVERFLOW_SENTINEL,
        exclude_roots=capture_root.exclude_roots,
        deadline=deadline,
    )
    files = scanner.scan()
    if time.monotonic() >= deadline:
        return None, DegradedReason.CAPTURE_DEADLINE
    if len(files) >= _SCAN_OVERFLOW_SENTINEL:
        return None, DegradedReason.TOO_MANY_FILES
    states = {
        lexical_display_path(path): BaselineFileState(True, value.mtime_ns, value.size, value.mode, ())
        for path, value in files.items()
    }
    return (
        RootBaseline(
            root=capture_root.root,
            mode=BaselineMode.SCAN,
            head=None,
            branch=None,
            scope=capture_root.scope,
            files=states,
        ),
        DegradedReason.CAPTURE_FAILED,
    )


def _stat_baseline_file(path: str, statuses: tuple[str, ...]) -> BaselineFileState:
    try:
        value = os.lstat(path)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return BaselineFileState(False, 0, 0, None, statuses)
        raise
    return BaselineFileState(
        True,
        value.st_mtime_ns,
        value.st_size,
        stat.S_IMODE(value.st_mode),
        statuses,
    )


def _diff_workspace(
    old: WorkspaceBaseline,
    new: WorkspaceBaseline,
    *,
    deadline: float,
) -> tuple[list[ExternalChange], list[HeadMove], dict[str, DegradedBaseline]]:
    external: list[ExternalChange] = []
    head_moves: list[HeadMove] = []
    degraded = {**old.degraded, **new.degraded}
    for root, old_root in old.roots.items():
        new_root = new.roots.get(root)
        if new_root is None or old_root.mode is not new_root.mode or old_root.scope != new_root.scope:
            degraded.setdefault(
                root,
                DegradedBaseline(DegradedReason.MISSING_BASELINE, new_root.mode if new_root else None, ()),
            )
            continue
        status_paths: set[str] = set()
        if old_root.mode is BaselineMode.GIT:
            status_entries = _diff_git_status(root, old_root.files, new_root.files)
            external.extend(status_entries)
            status_paths = {entry.path for entry in status_entries}
            if old_root.head != new_root.head:
                remaining = min(10.0, deadline - time.monotonic())
                delta = read_git_head_delta(
                    root,
                    old_root.head,
                    new_root.head,
                    new_root.scope,
                    timeout=remaining,
                    max_paths=GIT_PATH_LIMIT,
                )
                if delta.failed or delta.truncated:
                    head_moves.append(HeadMove(root, old_root.head, new_root.head, uncertain=True))
                elif delta.entries:
                    head_moves.append(HeadMove(root, old_root.head, new_root.head))
                    external.extend(
                        entry for entry in _external_from_delta(root, delta.entries) if entry.path not in status_paths
                    )
        else:
            external.extend(_diff_scan(root, old_root.files, new_root.files))
    return external, head_moves, degraded


def _diff_git_status(
    root: str,
    old: Mapping[str, BaselineFileState],
    new: Mapping[str, BaselineFileState],
) -> list[ExternalChange]:
    result: list[ExternalChange] = []
    for path in new.keys() - old.keys():
        state = new[path]
        status_chars = "".join(state.statuses)
        staged_delete_untracked = "D " in state.statuses and "??" in state.statuses and state.exists
        if not state.exists and "D" in status_chars:
            operation = ExternalOp.DELETED
        elif staged_delete_untracked:
            operation = ExternalOp.TRACKING_CHANGED
        elif (state.exists and ("R" in status_chars or "C" in status_chars or "A" in status_chars)) or (
            state.statuses and set(state.statuses) == {"??"}
        ):
            operation = ExternalOp.CREATED
        else:
            operation = ExternalOp.MODIFIED
        result.append(ExternalChange(root, path, operation))
    for path in old.keys() & new.keys():
        old_state = old[path]
        new_state = new[path]
        if old_state.exists and not new_state.exists:
            result.append(ExternalChange(root, path, ExternalOp.DELETED))
        elif (
            old_state.exists != new_state.exists
            or old_state.mtime_ns != new_state.mtime_ns
            or old_state.size != new_state.size
            or old_state.mode != new_state.mode
        ):
            result.append(ExternalChange(root, path, ExternalOp.MODIFIED))
        elif old_state.statuses != new_state.statuses:
            # An index-only transition (git add/reset/restore --staged)
            # leaves every filesystem field untouched; the status tuple is
            # the only observable.
            result.append(ExternalChange(root, path, ExternalOp.TRACKING_CHANGED))
    for path in old.keys() - new.keys():
        old_state = old[path]
        if os.path.lexists(path) or (not old_state.exists and any("D" in code for code in old_state.statuses)):
            operation = ExternalOp.RECONCILED
        else:
            operation = ExternalOp.DELETED
        result.append(ExternalChange(root, path, operation))
    return result


def _diff_scan(
    root: str,
    old: Mapping[str, BaselineFileState],
    new: Mapping[str, BaselineFileState],
) -> list[ExternalChange]:
    result = [ExternalChange(root, path, ExternalOp.CREATED) for path in new.keys() - old.keys()]
    result.extend(ExternalChange(root, path, ExternalOp.DELETED) for path in old.keys() - new.keys())
    for path in old.keys() & new.keys():
        before = old[path]
        after = new[path]
        if (before.mtime_ns, before.size, before.mode) != (after.mtime_ns, after.size, after.mode):
            result.append(ExternalChange(root, path, ExternalOp.MODIFIED))
    return result


def _external_from_delta(root: str, entries: tuple[GitDeltaEntry, ...]) -> list[ExternalChange]:
    result: list[ExternalChange] = []
    for entry in entries:
        status = entry.status[:1]
        path = lexical_display_path(os.path.join(root, entry.path))
        if status == "R" and entry.old_path is not None:
            result.append(
                ExternalChange(root, lexical_display_path(os.path.join(root, entry.old_path)), ExternalOp.DELETED)
            )
            result.append(ExternalChange(root, path, ExternalOp.CREATED))
        elif status in {"A", "C"}:
            result.append(ExternalChange(root, path, ExternalOp.CREATED))
        elif status == "D":
            result.append(ExternalChange(root, path, ExternalOp.DELETED))
        else:
            result.append(ExternalChange(root, path, ExternalOp.MODIFIED))
    return result


def _agent_changes(
    tracker: MutationTracker | None,
    turn_id: int | None,
) -> tuple[list[AgentChange], bool]:
    if tracker is None or turn_id is None:
        return [], False
    summary = tracker.get_turn_file_summary(turn_id)
    changes = [
        AgentChange(path, _operation_from_summary(diff), diff.content_unavailable, contested=diff.contested)
        for path, diff in summary.items()
        if not diff.is_net_zero or diff.content_unavailable
    ]
    turn = tracker.get_turn_mutations(turn_id)
    if turn is None:
        return changes, False
    for index, mutation in enumerate(turn.mutations):
        if mutation.provenance is MutationProvenance.FOREIGN or mutation.old_path is None:
            continue
        recreated = any(
            later.path == mutation.old_path
            and later.operation is not MutationOp.DELETE
            and later.provenance is not MutationProvenance.FOREIGN
            for later in turn.mutations[index + 1 :]
        )
        if recreated:
            continue
        # An earlier edit of the source leaves a stale summary entry; the
        # move's final endpoint for that path is the deletion.
        changes = [entry for entry in changes if entry.path != mutation.old_path]
        changes.append(AgentChange(mutation.old_path, MutationOp.DELETE, contested=mutation.contested))
    return changes, turn.detection_truncated


def _detection_truncated(tracker: MutationTracker | None, turn_id: int | None) -> bool:
    if tracker is None or turn_id is None:
        return False
    turn = tracker.get_turn_mutations(turn_id)
    return turn is not None and turn.detection_truncated


def _peer_witnessed_changes(
    tracker: MutationTracker | None,
    turn_id: int | None,
) -> list[PeerChange]:
    """Collect paths whose peer-session write only the turn log witnessed.

    ``FOREIGN`` rows are excluded from every net-change fold, and the
    end-of-turn baseline absorbs the peer's on-disk effect, so this raw
    pass over the turn log is the only surface that can report them. A
    ``contested`` local row also records a peer's overlapping write;
    when its own agent row is not displayed (Retry/Resume omits the
    section, net-zero folding drops the entry) this harvest is the only
    remaining witness — the caller's dedup against displayed agent rows
    keeps the contested badge as the primary channel elsewhere. Later
    rows win per path; a move contributes a deletion at its source.
    """
    if tracker is None or turn_id is None:
        return []
    turn = tracker.get_turn_mutations(turn_id)
    if turn is None:
        return []
    ops: dict[str, MutationOp] = {}
    for mutation in turn.mutations:
        if mutation.provenance is not MutationProvenance.FOREIGN and not mutation.contested:
            continue
        if mutation.operation is MutationOp.MOVE and mutation.old_path:
            ops[mutation.old_path] = MutationOp.DELETE
        ops[mutation.path] = mutation.operation
    return [PeerChange(path=path, operation=operation) for path, operation in ops.items()]


def _mutation_paths(
    tracker: MutationTracker | None,
    first_turn: int | None,
    last_turn: int | None,
) -> set[str]:
    if tracker is None or first_turn is None or last_turn is None:
        return set()
    result: set[str] = set()
    for turn in tracker.get_all_turns():
        if turn.turn_id < first_turn or turn.turn_id > last_turn:
            continue
        for mutation in turn.mutations:
            if mutation.provenance is MutationProvenance.FOREIGN:
                continue
            result.add(entry_path(mutation.path))
            if mutation.old_path:
                result.add(entry_path(mutation.old_path))
    return result


@dataclass(frozen=True, slots=True)
class _RecoveredEndpoint:
    """Last recorded endpoint for one path in the recovered turn range."""

    after_hash: str | None = None
    exists: bool = True
    verifiable: bool = True


def _recovered_endpoints(
    tracker: MutationTracker | None,
    first_turn: int | None,
    last_turn: int | None,
) -> dict[str, _RecoveredEndpoint]:
    if tracker is None or first_turn is None or last_turn is None:
        return {}
    result: dict[str, _RecoveredEndpoint] = {}
    for turn in tracker.get_all_turns():
        if turn.turn_id < first_turn or turn.turn_id > last_turn:
            continue
        for mutation in turn.mutations:
            if mutation.provenance is MutationProvenance.FOREIGN:
                continue
            if mutation.old_path:
                result[entry_path(mutation.old_path)] = _RecoveredEndpoint(exists=False)
            if mutation.operation is MutationOp.DELETE:
                result[entry_path(mutation.path)] = _RecoveredEndpoint(exists=False)
            elif mutation.after_hash is not None:
                result[entry_path(mutation.path)] = _RecoveredEndpoint(after_hash=mutation.after_hash)
            else:
                # Skip-marked content or a row that never saw its
                # ``record_after`` (crash mid-command): the recorded
                # endpoint proves nothing about current disk.
                result[entry_path(mutation.path)] = _RecoveredEndpoint(verifiable=False)
    return result


def _matches_recovered_endpoint(expected: _RecoveredEndpoint | None, path: str, max_bytes: int) -> bool:
    """Whether current disk provably matches a recovered mutation endpoint."""
    if expected is None or not expected.verifiable:
        return False
    if not expected.exists:
        return not os.path.lexists(path)
    data = _read_entry_bytes(path, max_bytes)
    return data is not None and SnapshotStore.content_hash(data) == expected.after_hash


def _read_entry_bytes(path: str, max_bytes: int) -> bytes | None:
    """Entry content for endpoint verification; a symlink is its target text."""
    try:
        if os.path.islink(path):
            return os.fsencode(os.readlink(path))
        if not os.path.isfile(path):
            return None
        if max_bytes > 0 and os.path.getsize(path) > max_bytes:
            return None
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


def _operation_from_summary(diff: FileHashDiff) -> MutationOp:
    if SnapshotSkipReason.UNREADABLE in (diff.before_skip, diff.after_skip):
        return MutationOp.MODIFY
    if not diff.before_exists and diff.after_exists:
        return MutationOp.CREATE
    if diff.before_exists and not diff.after_exists:
        return MutationOp.DELETE
    return MutationOp.MODIFY


def _reconcile_baseline(baseline: WorkspaceBaseline, scope: _ResolvedScope) -> WorkspaceBaseline:
    roots: dict[str, RootBaseline] = {}
    degraded: dict[str, DegradedBaseline] = {}
    for capture_root in scope.roots:
        old_root = baseline.roots.get(capture_root.root)
        old_degraded = baseline.degraded.get(capture_root.root)
        if old_root is not None and old_root.mode is capture_root.mode:
            if old_root.mode is BaselineMode.GIT and _git_scope_narrow_or_equal(old_root.scope, capture_root.scope):
                roots[capture_root.root] = replace(
                    old_root,
                    scope=capture_root.scope,
                    files={
                        path: state
                        for path, state in old_root.files.items()
                        if _path_in_git_scope(capture_root.root, path, capture_root.scope)
                    },
                )
                continue
            if old_root.mode is BaselineMode.SCAN and old_root.scope == capture_root.scope:
                roots[capture_root.root] = old_root
                continue
        if (
            old_degraded is not None
            and old_degraded.mode is not None
            and old_degraded.mode is capture_root.mode
            and old_degraded.scope == capture_root.scope
        ):
            degraded[capture_root.root] = old_degraded
        else:
            degraded[capture_root.root] = DegradedBaseline(
                DegradedReason.MISSING_BASELINE,
                capture_root.mode,
                capture_root.scope,
            )
    return WorkspaceBaseline(
        turn_id=baseline.turn_id,
        roots=roots,
        degraded=degraded,
        root_limit_omitted=scope.root_limit_omitted,
    )


def _git_scope_narrow_or_equal(old: tuple[str, ...], new: tuple[str, ...]) -> bool:
    if old == new or old == (".",):
        return True
    if new == (".",):
        return False
    return all(any(item == parent or item.startswith(f"{parent}/") for parent in old) for item in new)


def _path_in_git_scope(root: str, path: str, scope: tuple[str, ...]) -> bool:
    # Persisted entries can name Windows devices (e.g. \\.\nul) or a
    # different drive. They cannot belong to this repository, even when
    # the whole repository is selected. Keep the comparison lexical so
    # symlinks and deleted entries retain their own identities.
    try:
        rel = PurePath(os.path.relpath(path, root)).as_posix()
    except ValueError:
        return False
    if rel == ".." or rel.startswith("../"):
        return False
    if scope == (".",):
        return True
    return any(rel == item or rel.startswith(f"{item}/") for item in scope)


def _serialize_baseline(baseline: WorkspaceBaseline) -> dict[str, Any]:
    return {
        "version": _SERIALIZATION_VERSION,
        "turn_id": baseline.turn_id,
        "roots": {
            key: {
                "root": value.root,
                "mode": value.mode.value,
                "head": value.head,
                "branch": value.branch,
                "scope": list(value.scope),
                "files": {
                    path: {
                        "exists": state.exists,
                        "mtime_ns": state.mtime_ns,
                        "size": state.size,
                        "mode": state.mode,
                        "statuses": list(state.statuses),
                    }
                    for path, state in value.files.items()
                },
            }
            for key, value in baseline.roots.items()
        },
        "degraded": {
            key: {
                "reason": value.reason.value,
                "mode": value.mode.value if value.mode is not None else None,
                "scope": list(value.scope),
            }
            for key, value in baseline.degraded.items()
        },
        "root_limit_omitted": baseline.root_limit_omitted,
    }


def _deserialize_baseline(value: object) -> WorkspaceBaseline | None:
    if not isinstance(value, Mapping) or value.get("version") != _SERIALIZATION_VERSION:
        return None
    try:
        payload = cast("Mapping[str, object]", value)
        raw_roots = payload.get("roots", {})
        raw_degraded = payload.get("degraded", {})
        if not isinstance(raw_roots, Mapping) or not isinstance(raw_degraded, Mapping):
            return None
        roots: dict[str, RootBaseline] = {}
        for key, raw_value in raw_roots.items():
            raw = cast("Mapping[str, object]", raw_value)
            if not isinstance(key, str) or not isinstance(raw, Mapping):
                return None
            raw_files = raw.get("files", {})
            if not isinstance(raw_files, Mapping):
                return None
            files: dict[str, BaselineFileState] = {}
            for path, state_value in raw_files.items():
                if not isinstance(path, str) or not isinstance(state_value, Mapping):
                    return None
                state = cast("Mapping[str, object]", state_value)
                mode_value = state.get("mode")
                files[path] = BaselineFileState(
                    exists=_serialized_bool(state["exists"]),
                    mtime_ns=_serialized_int(state["mtime_ns"]),
                    size=_serialized_int(state["size"]),
                    mode=_serialized_int(mode_value) if mode_value is not None else None,
                    statuses=_serialized_strings(state.get("statuses", [])),
                )
            roots[key] = RootBaseline(
                root=_serialized_str(raw["root"]),
                mode=BaselineMode(_serialized_str(raw["mode"])),
                head=_serialized_str(raw["head"]) if raw.get("head") is not None else None,
                branch=_serialized_str(raw["branch"]) if raw.get("branch") is not None else None,
                scope=_serialized_strings(raw.get("scope", [])),
                files=files,
            )
        degraded: dict[str, DegradedBaseline] = {}
        for key, raw_value in raw_degraded.items():
            if not isinstance(key, str) or not isinstance(raw_value, Mapping):
                return None
            raw = cast("Mapping[str, object]", raw_value)
            mode_value = raw.get("mode")
            degraded[key] = DegradedBaseline(
                reason=DegradedReason(_serialized_str(raw["reason"])),
                mode=BaselineMode(_serialized_str(mode_value)) if mode_value is not None else None,
                scope=_serialized_strings(raw.get("scope", [])),
            )
        return WorkspaceBaseline(
            turn_id=_serialized_int(payload["turn_id"]),
            roots=roots,
            degraded=degraded,
            root_limit_omitted=_serialized_int(payload.get("root_limit_omitted", 0)),
        )
    except KeyError, TypeError, ValueError:
        logger.debug("Dropping malformed workspace baseline", exc_info=True)
        return None


def _deserialize_pending_safety(value: object) -> list[_SafetyNotice]:
    """Rehydrate undelivered safety notices; malformed entries are dropped."""
    if not isinstance(value, Mapping) or value.get("version") != _SERIALIZATION_VERSION:
        return []
    raw = value.get("pending_safety", [])
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return []
    result: list[_SafetyNotice] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        text = entry.get("text")
        if not isinstance(text, str) or not text:
            continue
        cwd = entry.get("cwd")
        result.append(_SafetyNotice(text, cwd if isinstance(cwd, str) else None))
    return result


def _serialized_bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError
    return value


def _serialized_int(value: object) -> int:
    if not isinstance(value, int):
        raise TypeError
    return value


def _serialized_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError
    return value


def _serialized_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError
        result.append(item)
    return tuple(result)


def _quoted_path(path: str, cwd: str) -> str:
    return model_path(display_relpath(path, cwd))


def _external_label(operation: ExternalOp) -> str:
    return {
        ExternalOp.CREATED: "created",
        ExternalOp.MODIFIED: "modified",
        ExternalOp.DELETED: "deleted",
        ExternalOp.RECONCILED: "reconciled (committed or reverted)",
        ExternalOp.TRACKING_CHANGED: "Git tracking changed",
    }[operation]


def _agent_label(operation: MutationOp) -> str:
    return {
        MutationOp.CREATE: "created",
        MutationOp.MODIFY: "modified",
        MutationOp.DELETE: "deleted",
        MutationOp.MOVE: "moved",
    }[operation]


def _degraded_label(reason: DegradedReason) -> str:
    return {
        DegradedReason.TOO_MANY_FILES: "too many files",
        DegradedReason.CAPTURE_FAILED: "capture failed",
        DegradedReason.CAPTURE_DEADLINE: "capture deadline exceeded",
        DegradedReason.MISSING_BASELINE: "no comparable previous baseline",
    }[reason]


def _contains(parent: str, child: str) -> bool:
    try:
        return os.path.commonpath([parent, child]) == parent
    except ValueError:
        return False


def _display_path(path: str, scope: _ResolvedScope) -> str:
    # Canonicalize only the directory (mapping root aliases onto display
    # roots): the final component may be a symlink whose identity is the
    # link itself — resolving it would rename the entry to its
    # destination, possibly outside every display root. The relative tail
    # is taken from the case-preserving spelling: relpath folds case only
    # while matching the prefix, so Windows notices keep the on-disk case
    # the comparison identity discards.
    physical_path = entry_path(path)
    for physical, display in sorted(scope.display_roots, key=lambda item: len(item[0]), reverse=True):
        if _contains(physical, physical_path):
            return os.path.normpath(os.path.join(display, os.path.relpath(entry_display_path(path), physical)))
    return path


def _resolve_safety_notice(chunk: _SafetyNotice) -> _SafetyNotice:
    """Capture the notice's directory identity, retaining text if resolution fails."""
    if chunk.cwd is None:
        return chunk
    try:
        canonical = canonical_path(chunk.cwd)
        return replace(chunk, canonical_cwd=canonical)
    except OSError, UnicodeError:
        return _pin_safety_notice(chunk.text, chunk.cwd)


def _notice_cwd(workspace: Workspace | None) -> tuple[str | None, str | None]:
    if workspace is None:
        return None, None
    cwd = str(Path(workspace.primary_cwd).absolute())
    return cwd, canonical_path(cwd)


def _rebase_safety_notice(chunk: _SafetyNotice, cwd: str | None, identity: str | None) -> _SafetyNotice:
    """Pin a pending safety notice's path base when the workspace moves.

    The notice text cannot be re-rendered (its source data is gone), so a
    switch to a different primary cwd prepends the original base once;
    after that the chunk is base-independent and never annotated again.
    """
    if chunk.cwd is None or cwd is None or chunk.canonical_cwd == identity:
        return chunk
    return _pin_safety_notice(chunk.text, chunk.cwd)


def _pin_safety_notice(text: str, cwd: str) -> _SafetyNotice:
    """Annotate the original directory once while preserving all notice text."""
    header = f"Note: relative paths below are against the previous workspace directory {model_path(cwd)}."
    return _SafetyNotice(f"{header}\n{text}", cwd=None)


def _join_notices(first: str | None, second: str | None) -> str | None:
    parts = [part for part in (first, second) if part]
    return "\n\n".join(parts) if parts else None
