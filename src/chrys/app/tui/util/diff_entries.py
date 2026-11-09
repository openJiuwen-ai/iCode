# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Diff entry data models shared by TUI diff surfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from chrys.foundation.models.mutation_scope import MutationScope
from chrys.service.mutations.types import MutationOp

MAX_PREVIEW_FILES = 10
MAX_COUNTED_LINES = 20000


@dataclass(frozen=True)
class DiffFocus:
    """Open the session-wide changes and optionally select a display key."""

    display_key: str | None = None


@dataclass
class DiffFileEntry:
    """One changed file to display in the diff viewer."""

    path: str  # absolute path
    rel_path: str  # relative to workspace cwd (for display)
    operation: MutationOp  # CREATE, MODIFY, DELETE, MOVE
    old_path: str | None  # source path for MOVE
    before_text: str  # "" if file didn't exist or binary
    after_text: str  # "" if file was deleted or binary
    is_binary: bool  # True if null bytes detected in content
    encoding: str  # detected encoding of the file content
    bytes_changed: bool = False  # True when before/after snapshot hashes differ
    before_hash: str | None = None
    after_hash: str | None = None
    source: str = ""  # mutation source, e.g. "implicit" for uncertain git-calibrated changes
    # SnapshotSkipReason value ("too_large" / "binary") when the file's
    # content backup was withheld by SnapshotPolicy — the entry is still
    # shown/actionable but has no diff content to render.  "" = content
    # available.
    content_omitted: str = ""
    # Folded provenance badges: a peer
    # session also wrote this path / the net change includes window-diff
    # inference.
    contested: bool = False
    inferred: bool = False
    last_changed_at: float = 0.0
    # A numeric pair is known; every other outcome has no numeric payload.
    # Large files are unavailable only to the preview, not the full diff.
    line_count_result: tuple[int, int] | Literal["pending", "unavailable", "large"] = "unavailable"

    @property
    def line_counts(self) -> tuple[int, int] | None:
        """Expose counts only when the preview has a known result."""
        return self.line_count_result if isinstance(self.line_count_result, tuple) else None


@dataclass
class DiffLoadResult:
    """Result of loading diff data from a persisted session.

    ``all_entries`` — net session-wide changeset (for the "All" tab).
    Display numbers are 1-based and independent of execution identities.
    ``scopes`` maps each display number (including empty periods) to its real
    Chat Turn or Workflow Run. A rollback selector must use that identity,
    never the displayed ordinal. ``total_periods`` includes empty periods.
    """

    all_entries: list[DiffFileEntry]
    per_period_entries: dict[int, list[DiffFileEntry]]
    total_periods: int
    scopes: dict[int, MutationScope] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> DiffLoadResult:
        return cls(all_entries=[], per_period_entries={}, total_periods=0)


def entry_has_visible_change(entry: DiffFileEntry) -> bool:
    """Return whether a diff entry should appear in the tree."""
    return entry.bytes_changed or entry.before_text != entry.after_text or entry.operation is MutationOp.MOVE
