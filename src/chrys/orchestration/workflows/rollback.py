# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""File-only Workflow rollback under the caller's execution and session fences."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from time import time
from typing import TYPE_CHECKING, Any

from chrys.foundation.events.types import WorkflowRollbackRequest, WorkflowRollbackResult
from chrys.foundation.models.mutation_scope import WorkflowRunScope
from chrys.foundation.util.once_close import finish_close
from chrys.service.mutations.coordination import ATTRIBUTION_DIR_NAME, MutationCoordinator
from chrys.service.mutations.store import SnapshotPolicy, SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import FileMutation, MutationOp, MutationProvenance, MutationSource

if TYPE_CHECKING:
    from chrys.foundation.config.settings import Settings
    from chrys.orchestration.workflows.session import WorkflowSessionOwner


def _file_revision(path: str) -> tuple[int, ...] | None:
    """Fence editor writes and endpoint replacements without reading large files."""
    try:
        entry = Path(path).lstat()
    except FileNotFoundError, NotADirectoryError:
        return None
    return (entry.st_dev, entry.st_ino, entry.st_mode, entry.st_size, entry.st_mtime_ns, entry.st_ctime_ns)


async def rollback_files(
    owner: WorkflowSessionOwner, request: WorkflowRollbackRequest, *, settings: Settings
) -> WorkflowRollbackResult:
    """Reload and recheck the plan on both preview and commit; keep Run history intact."""
    if request.session_id is None:
        raise ValueError("Select a workflow session.")
    state = owner.require_state()
    directory, workspace = owner.require_session_dir(), owner.require_workspace()
    if state.mutations is None:
        raise ValueError("This session has no recorded file changes.")
    mutations = state.mutations
    coordinator = MutationCoordinator(
        registry_root=directory.parent / ATTRIBUTION_DIR_NAME, session_id=request.session_id
    )

    def apply() -> tuple[WorkflowRollbackResult, dict[str, Any] | None]:
        from chrys.foundation import guard

        snapshots = SnapshotStore(directory, policy=SnapshotPolicy.from_settings(settings))
        tracker = MutationTracker.deserialize(mutations, snapshots)
        cwd = workspace.primary_cwd
        scope_paths = [cwd, *(directory.path for directory in workspace.working_dirs)]
        reclassified = coordinator.reclassify(tracker, force=True, fallback_root=cwd)
        periods = tracker.get_all_periods()
        target = tracker.get_period(WorkflowRunScope(request.run_id))
        scopes = {period.scope for period in periods if period.period_index >= target.period_index}
        plan = coordinator.augment_rollback_plan(
            tracker, tracker.get_rollback_plan_for_periods(scopes), scope_paths=scope_paths, fallback_root=cwd
        )
        revisions = {path: _file_revision(path) for path, _ in plan.entries}
        token = hashlib.sha256(
            json.dumps(
                {
                    "ledger": tracker.serialize(),
                    "plan": asdict(plan),
                    "run": request.run_id,
                    "files": revisions,
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        result = WorkflowRollbackResult(
            session_id=request.session_id,
            request_id=request.request_id,
            run_id=request.run_id,
            token=token,
            paths=tuple(path for path, _ in plan.entries),
            exclusions=tuple((path, reason.value) for path, reason in plan.exclusions),
            warnings=tuple(warning.message for warning in plan.warnings),
        )
        if request.token:
            if request.token != token:
                raise ValueError("The rollback plan changed. Review the updated plan before confirming.")
            # An open window makes concurrent peers aware of the explicit user operation.
            window = coordinator.open_window(scope_paths, fallback_root=cwd)
            try:
                started = time()
                before = {path: snapshots.probe(path) for path, _ in plan.entries}
                if any(_file_revision(path) != revision for path, revision in revisions.items()):
                    raise ValueError(
                        "Files changed while preparing rollback. Review the updated plan before confirming."
                    )
                # Under the sandbox, as the tool calls that made these changes were.
                with guard.current().session_scope(request.session_id, scope_paths, cwd):
                    restored = tracker.restore_files(plan)
                finished = time()
                restored_paths = {item.path for item in restored if item.changed}
                coordinator.publish_claims(
                    [
                        FileMutation(
                            path=path,
                            operation=(
                                MutationOp.DELETE
                                if not snapshot.existed
                                else MutationOp.MODIFY
                                if before[path].existed
                                else MutationOp.CREATE
                            ),
                            source=MutationSource.IMPLICIT,
                            provenance=MutationProvenance.PROVEN,
                            tool_call_id=request.request_id,
                            timestamp=finished,
                            t_start=started,
                            t_end=finished,
                            before_hash=before[path].content_hash,
                            before_skip=before[path].skip_reason,
                            after_hash=snapshot.content_hash,
                            after_skip=snapshot.skip_reason,
                        )
                        for path, snapshot in plan.entries
                        if path in restored_paths
                    ],
                    fallback_root=cwd,
                )
            finally:
                coordinator.close_window(window)
            result.applied = True
            result.changed = sum(item.changed for item in restored)
            result.error = "\n".join(f"{item.path}: {item.reason}" for item in restored if not item.ok)
        return result, tracker.serialize() if reclassified or result.applied else None

    result: WorkflowRollbackResult | None = None

    async def complete() -> None:
        nonlocal result
        result, updated = await asyncio.to_thread(apply)
        if updated is not None:
            try:
                await owner.save_mutations(updated)
            except OSError as exc:
                result.error = "\n".join(part for part in (result.error, f"Session checkpoint failed: {exc}") if part)

    try:
        # Drain both the file writes and checkpoint before releasing the lock.
        await finish_close(asyncio.create_task(complete()))
        if result is None:
            raise RuntimeError("Workflow rollback completed without a result.")
        return result
    finally:
        await asyncio.to_thread(coordinator.close)
