# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Mutation tracking data types — enums and dataclasses.

These types form the serialization-friendly schema for the mutation log.
All paths stored in these structures are absolute and normalized.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, cast

from chrys.foundation.models.mutation_scope import ChatTurnScope, WorkflowRunScope

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class MutationOp(Enum):
    """Type of file system mutation."""

    CREATE = "create"  # file did not exist -> now exists
    MODIFY = "modify"  # file existed -> content changed
    DELETE = "delete"  # file existed -> removed
    MOVE = "move"  # file relocated (old_path -> path)


class MutationSource(Enum):
    """Which tool caused the mutation."""

    WRITE_FILE = "write_file"
    EDIT_FILE = "edit_file"
    SHELL = "shell"
    IMPLICIT = "implicit"  # detected by git calibration, not a specific tool


class MutationProvenance(Enum):
    """Confidence level of a mutation's attribution to this session.

    ``FOREIGN`` mutations are kept in the raw event log for audit and
    conflict detection but excluded from every net-change surface (file
    summaries, diff payloads) and from rollback plans.
    """

    PROVEN = "proven"  # chrys itself performed the write (write_file /
    # edit_file), or the shell child process tree was positively
    # observed writing it (trace evidence).
    ASSUMED = "assumed"  # window-diff inference; could be a concurrent
    # third-party write.
    FOREIGN = "foreign"  # positively attributed to another chrys
    # session (peer claim with content identity).


def default_provenance(source: MutationSource) -> MutationProvenance:
    """Source-derived default: exact tools are proven, window diffs assumed."""
    if source in (MutationSource.WRITE_FILE, MutationSource.EDIT_FILE):
        return MutationProvenance.PROVEN
    return MutationProvenance.ASSUMED


def parse_provenance(value: Any, source: MutationSource) -> MutationProvenance:
    """Deserialize a provenance value leniently.

    Unknown values (from newer chrys versions) and missing keys (legacy
    logs) degrade to the source-derived default, so old sessions load
    with exactly today's implied semantics.
    """
    if value:
        try:
            return MutationProvenance(value)
        except ValueError:
            pass
    return default_provenance(source)


class SnapshotSkipReason(Enum):
    """Why a file's content blob was withheld from the snapshot store.

    Recorded on :class:`FileSnapshot` (before-content) and
    :class:`FileMutation` (per-side) when the :class:`SnapshotPolicy`
    declines to persist a backup blob, or the endpoint cannot be read.
    The mutation *event* is still
    logged — only the content backup is missing, so diff rendering and
    rollback restore are unavailable for the affected side.
    """

    TOO_LARGE = "too_large"  # file exceeds SnapshotPolicy.max_file_bytes
    BINARY = "binary"  # content looks binary (non-text) per EncodingDetector
    UNREADABLE = "unreadable"  # IO failure; neither content nor absence is proven


def parse_skip_reason(value: Any) -> SnapshotSkipReason | None:
    """Deserialize a skip reason leniently.

    Unknown values (from newer chrys versions) degrade to ``None`` —
    restorability and rendering decisions key off missing content
    hashes, not the reason label, so this only loses display detail.
    """
    if not value:
        return None
    try:
        return SnapshotSkipReason(value)
    except ValueError:
        return None


class RestoreOutcome(Enum):
    """Outcome of restoring a single file during a rollback.

    See :meth:`chrys.service.mutations.store.SnapshotStore.restore` for the
    full "force to expected state" contract.  Three-way summary so the
    TUI can report "N reverted, M already matched, K failed" without
    having to reason about disk state itself.
    """

    APPLIED = "applied"  # disk state changed to match the snapshot target
    NOOP = "noop"  # disk was already in the target state — nothing to do
    FAILED = "failed"  # could not apply (IO error, dir-where-file, blob missing, ...)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileHashDiff:
    """Before/after content hashes for a file across one turn or an entire session.

    Returned as the value type of ``MutationTracker.get_turn_file_summary``
    and ``get_session_file_summary``.  Each hash is a SHA-256 hex pointer
    into the session's ``SnapshotStore``; ``None`` means the file did not
    exist at that point (either pre-mutation or post-deletion) — unless the
    matching ``*_skip`` field is set, in which case the backup was withheld
    by policy or unreadable. ``UNREADABLE`` never proves absence; existence
    predicates conservatively keep that endpoint present.
    """

    before: str | None
    after: str | None
    before_skip: SnapshotSkipReason | None = None
    after_skip: SnapshotSkipReason | None = None
    # Folded provenance badges (display-level).
    # ``contested``: a peer session also wrote this path (any contested row,
    # or foreign rows mixed with ours) — disk may differ from "our" after.
    # ``inferred``: the folded net change includes window-diff inference
    # (any contributing row has ASSUMED provenance).
    contested: bool = False
    inferred: bool = False

    @property
    def is_net_zero(self) -> bool:
        """True when there is no *provable* net change.

        Hash equality alone is not enough once SnapshotPolicy skips are
        in play: a skipped create/delete reads ``(None, None)`` but the
        file's existence flipped — a real, provable net change.  Only
        when the hashes match AND existence didn't change is the entry
        net-zero.  That includes the fully-skipped modify (exists →
        exists, both backups withheld), where a net change cannot be
        proven either way — net-content views drop it, per-turn event
        views surface it via ``content_unavailable``.
        """
        return self.before == self.after and self.before_exists == self.after_exists

    @property
    def content_unavailable(self) -> bool:
        """True when either side's content backup was withheld by policy.

        Such entries cannot be rendered as a truthful diff (a ``None``
        hash would read as create/delete) — UI surfaces skip them.
        """
        return self.before_skip is not None or self.after_skip is not None

    @property
    def before_exists(self) -> bool:
        """Whether the file existed before — a real hash OR a skipped backup."""
        return self.before is not None or self.before_skip is not None

    @property
    def after_exists(self) -> bool:
        """Whether the file exists after — a real hash OR a skipped backup."""
        return self.after is not None or self.after_skip is not None


@dataclass(frozen=True)
class FileMutationTextSnapshot:
    """Decoded before/after content plus metadata for one file mutation."""

    before_text: str
    after_text: str
    operation: str
    bytes_changed: bool
    source: str = ""
    before_hash: str | None = None
    after_hash: str | None = None
    # Why a side's content backup was withheld by SnapshotPolicy (its
    # ``*_text`` is then "" and ``*_hash`` is None without meaning the
    # file was absent/empty).
    before_skip: SnapshotSkipReason | None = None
    after_skip: SnapshotSkipReason | None = None
    # MutationProvenance value of the underlying mutation(s); "assumed"
    # sticky across collapsed multi-mutations (any assumed row → assumed).
    provenance: str = ""
    contested: bool = False


@dataclass(frozen=True)
class RestoreResult:
    """Per-file outcome bundle returned by ``rollback``/``restore``.

    ``path`` is the absolute normalized file path the rollback plan
    targeted.  ``outcome`` is the three-way verdict; ``reason`` is a
    short human-readable string populated only when ``outcome`` is
    :data:`RestoreOutcome.FAILED` (e.g. "blob missing", "directory at
    path").  Callers that just want "which files did we actually
    change?" can use :attr:`changed`.
    """

    path: str
    outcome: RestoreOutcome
    reason: str = ""

    @property
    def changed(self) -> bool:
        """True when disk state was mutated (APPLIED only, not NOOP)."""
        return self.outcome is RestoreOutcome.APPLIED

    @property
    def ok(self) -> bool:
        """True when the file ended up in the target state (APPLIED or NOOP)."""
        return self.outcome is not RestoreOutcome.FAILED


class RollbackExclusionReason(Enum):
    """Why a mutated path was dropped from a rollback plan.

    Display-only — every exclusion is surfaced (modal note section,
    ``RollbackResult`` event, ACP ``session/rollback`` response), never
    silent.  ``FOREIGN``/``CONTESTED``/``PEER_MODIFIED_SINCE`` are
    produced once cross-session coordination lands (Layer 2); the values
    exist now so the schema is complete.
    """

    UNRESTORABLE = "unrestorable"  # snapshot content withheld by policy
    MOVE_POISONED = "move_poisoned"  # move-atomicity conservation
    FOREIGN = "foreign"  # change attributed to a peer session
    CONTESTED = "contested"  # local + peer writes on the same path
    PEER_MODIFIED_SINCE = "peer_modified_since"  # peer claim newer than our last mutation


@dataclass(frozen=True)
class RollbackWarning:
    """Advisory, non-blocking notice attached to a rollback plan.

    Repo-level only (e.g. "another chrys session has an active command
    here") — per-path hazards are exclusions, never warnings.  Populated
    by cross-session coordination (Layer 2); the slot exists now so plan
    consumers have a stable shape.
    """

    code: str
    message: str


@dataclass(frozen=True)
class RollbackPlan:
    """Result of rollback-plan construction: actionable entries plus
    everything that was dropped and why.

    ``entries`` is the exact ``(path, snapshot)`` list rollback will act
    on (plan-WYSIWYG: display set == plan paths).  ``exclusions`` and
    ``warnings`` are display/reporting-only.
    """

    entries: list[tuple[str, FileSnapshot]]
    exclusions: list[tuple[str, RollbackExclusionReason]] = field(default_factory=list)
    warnings: list[RollbackWarning] = field(default_factory=list)

    @property
    def paths(self) -> set[str]:
        """Paths the plan will act on."""
        return {path for path, _snap in self.entries}


@dataclass
class FileSnapshot:
    """Metadata for a snapshotted file.

    The actual content blob is stored on disk, keyed by ``content_hash``.
    If the file did not exist at snapshot time, ``existed`` is False
    and ``content_hash`` is None (no blob written).  If the file existed
    but its content backup was withheld by :class:`SnapshotPolicy`,
    ``existed`` is True, ``content_hash`` is None, and ``skip_reason``
    says why (``size`` still records the on-disk byte count). An unreadable
    endpoint also uses ``existed=True`` with ``UNREADABLE``: its prior absence
    was not proven, so it cannot authorize deletion during rollback.

    A symlink entry is snapshotted as itself, never as its destination:
    ``is_symlink=True`` and the blob holds the link's target text (the
    same representation Git uses for symlink blobs), so restore
    recreates the link instead of writing a regular file over it.
    """

    path: str  # absolute, normalized
    period_index: int  # ledger index of the owning period
    existed: bool  # did the file exist before mutation?
    content_hash: str | None = None  # SHA-256 hex (None if !existed or skipped)
    size: int = 0  # original byte count (0 if !existed)
    skip_reason: SnapshotSkipReason | None = None  # why content was withheld
    is_symlink: bool = False  # entry is a symlink; blob = target text
    # Windows encodes file-vs-directory in the link itself and requires
    # it again at creation time; POSIX links are untyped and ignore it.
    # None = kind unprovable (calibrated old endpoints whose target no
    # longer witnesses it) — restore then infers from the target text.
    symlink_target_is_dir: bool | None = None

    @property
    def restorable(self) -> bool:
        """Whether rollback can bring the file to this snapshot's state.

        ``existed=False`` restores by deleting the file (no content
        needed); ``existed=True`` needs the stored blob — a snapshot
        whose content was withheld or unreadable cannot be restored.
        """
        return not self.existed or self.content_hash is not None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "path": self.path,
            "period_index": self.period_index,
            "existed": self.existed,
            "content_hash": self.content_hash,
            "size": self.size,
        }
        if self.skip_reason is not None:
            d["skip_reason"] = self.skip_reason.value
        if self.is_symlink:
            d["is_symlink"] = True
        if self.symlink_target_is_dir is not None:
            d["symlink_target_is_dir"] = self.symlink_target_is_dir
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FileSnapshot:
        return cls(
            path=d["path"],
            period_index=d["period_index"] if "period_index" in d else d["turn_id"],
            existed=d["existed"],
            content_hash=d.get("content_hash"),
            size=d.get("size", 0),
            skip_reason=parse_skip_reason(d.get("skip_reason")),
            is_symlink=bool(d.get("is_symlink", False)),
            symlink_target_is_dir=(None if (kind := d.get("symlink_target_is_dir")) is None else bool(kind)),
        )


@dataclass
class FileMutation:
    """Record of a single file mutation event.

    ``before_skip`` / ``after_skip`` mark sides whose content backup was
    withheld by :class:`SnapshotPolicy` — the corresponding hash is then
    ``None`` even though content existed on disk.

    ``provenance`` defaults to the source-derived value (see
    :func:`default_provenance`) when constructed with ``None`` — pass it
    explicitly only to refine attribution (trace evidence, peer claims).
    ``contested`` marks a mutation that stays ours but conflicts with a
    peer session's proven claim on the same path — it never changes
    provenance, it poisons rollback.

    ``t_start``/``t_end`` bound when the disk write actually occurred —
    tight ``[record, record_after]`` for write/edit, the full
    observation window for shell/implicit detection.  Cross-session
    matching uses these intervals, never ``timestamp``: the point
    timestamp sits on opposite sides of the write depending on source
    (pre-write for file tools, post-window for shell/implicit).
    Persisted so resumed sessions can still match at a post-restart
    rollback; ``None`` (legacy rows) simply never matches — a missed
    demotion, not a correctness hazard.
    """

    path: str  # absolute, normalized
    operation: MutationOp
    source: MutationSource
    tool_call_id: str  # links to originating tool call
    timestamp: float  # time.time() when recorded
    old_path: str | None = None  # source path for MOVE operations
    before_hash: str | None = None  # SHA-256 of content before mutation
    after_hash: str | None = None  # SHA-256 of content after mutation
    before_skip: SnapshotSkipReason | None = None  # why before-content was withheld
    after_skip: SnapshotSkipReason | None = None  # why after-content was withheld
    provenance: MutationProvenance | None = None  # None → derived from source
    contested: bool = False  # peer claim overlaps but the mutation stays ours
    t_start: float | None = None  # write-interval lower bound (see docstring)
    t_end: float | None = None  # write-interval upper bound
    tool_operation_id: str | None = None  # stable tool occurrence, distinct from provider call_id

    def __post_init__(self) -> None:
        if self.provenance is None:
            self.provenance = default_provenance(self.source)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "path": self.path,
            "operation": self.operation.value,
            "source": self.source.value,
            "tool_call_id": self.tool_call_id,
            "timestamp": self.timestamp,
            # Always written — provenance is never "absent" semantically.
            "provenance": self.provenance.value if self.provenance else default_provenance(self.source).value,
        }
        if self.tool_operation_id is not None:
            d["tool_operation_id"] = self.tool_operation_id
        if self.old_path is not None:
            d["old_path"] = self.old_path
        if self.before_hash is not None:
            d["before_hash"] = self.before_hash
        if self.after_hash is not None:
            d["after_hash"] = self.after_hash
        if self.before_skip is not None:
            d["before_skip"] = self.before_skip.value
        if self.after_skip is not None:
            d["after_skip"] = self.after_skip.value
        if self.contested:
            d["contested"] = True
        if self.t_start is not None:
            d["t_start"] = self.t_start
        if self.t_end is not None:
            d["t_end"] = self.t_end
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FileMutation:
        source = MutationSource(d["source"])
        return cls(
            path=d["path"],
            operation=MutationOp(d["operation"]),
            source=source,
            tool_call_id=d["tool_call_id"],
            tool_operation_id=d.get("tool_operation_id"),
            timestamp=d["timestamp"],
            old_path=d.get("old_path"),
            before_hash=d.get("before_hash"),
            after_hash=d.get("after_hash"),
            before_skip=parse_skip_reason(d.get("before_skip")),
            after_skip=parse_skip_reason(d.get("after_skip")),
            provenance=parse_provenance(d.get("provenance"), source),
            contested=bool(d.get("contested", False)),
            t_start=d.get("t_start"),
            t_end=d.get("t_end"),
        )


@dataclass
class TurnMutations:
    """All mutations within one agent turn.

    Every turn is recorded even if ``mutations`` is empty, so the
    timeline is continuous and multi-step rollback can enumerate turns.
    """

    turn_id: int
    mutations: list[FileMutation] = field(default_factory=list)
    detection_truncated: bool = False

    @property
    def period_index(self) -> int:
        """Chat's ledger index follows its durable turn identity."""
        return self.turn_id

    @property
    def scope(self) -> ChatTurnScope:
        return ChatTurnScope(self.turn_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "mutations": [m.to_dict() for m in self.mutations],
            "detection_truncated": self.detection_truncated,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TurnMutations:
        return cls(
            turn_id=d["turn_id"],
            mutations=[FileMutation.from_dict(m) for m in d.get("mutations", [])],
            detection_truncated=bool(d.get("detection_truncated", False)),
        )


@dataclass
class RunMutations:
    """All node/attempt mutations from one Workflow run.

    ``period_index`` orders the ledger and its snapshots. ``run_id`` is the
    stable identity used by queries, UI selection and rollback requests.
    """

    run_id: str
    period_index: int
    mutations: list[FileMutation] = field(default_factory=list)
    detection_truncated: bool = False

    @property
    def scope(self) -> WorkflowRunScope:
        return WorkflowRunScope(self.run_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "period_index": self.period_index,
            "mutations": [mutation.to_dict() for mutation in self.mutations],
            "detection_truncated": self.detection_truncated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunMutations:
        run_id, index = data["run_id"], data["period_index"]
        if not isinstance(run_id, str) or not run_id or type(index) is not int or index < 1:
            raise ValueError("Invalid workflow mutation period")
        return cls(
            run_id=run_id,
            period_index=index,
            mutations=[FileMutation.from_dict(item) for item in data.get("mutations", [])],
            detection_truncated=bool(data.get("detection_truncated", False)),
        )


type MutationPeriod = TurnMutations | RunMutations


@dataclass
class MutationLog:
    """One session's ordered mutation periods and their file snapshots.

    Chat and Workflow sessions own distinct ledgers. Snapshots use a neutral
    period index; consumers select periods using typed execution identities.
    """

    periods: list[MutationPeriod] = field(default_factory=list)
    snapshots: dict[str, FileSnapshot] = field(default_factory=dict)

    def require_chat(self) -> None:
        """Reject Turn selectors against a Workflow ledger."""
        if any(not isinstance(period, TurnMutations) for period in self.periods):
            raise TypeError("Workflow mutations must be selected by run_id")

    @property
    def turns(self) -> list[TurnMutations]:
        """Chat-only view; never reinterpret a Run as a Turn."""
        self.require_chat()
        return cast("list[TurnMutations]", self.periods)

    @staticmethod
    def snapshot_key(path: str, period_index: int) -> str:
        """Canonical key for one file's snapshot at the period boundary."""
        return f"{os.path.normpath(os.path.abspath(path))}::{period_index}"
