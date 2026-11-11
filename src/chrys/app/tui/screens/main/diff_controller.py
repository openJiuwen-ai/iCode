# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Live diff tracking and diff-screen opening for the main screen."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol, cast

from chrys.app.tui.screens.main.live_diff import (
    LiveFileMutation,
    build_live_diff_entries,
    infer_live_op,
    merge_live_entries_into_all,
    merge_live_mutation,
)
from chrys.app.tui.screens.main.state import MainScreenServices
from chrys.app.tui.util.diff_content import PreviewCountCache, count_preview_entry
from chrys.app.tui.util.diff_entries import DiffFocus
from chrys.foundation.models.mutation_scope import MutationScope
from chrys.foundation.platform import get_platform
from chrys.foundation.platform.paths import resolve_workspace_path
from chrys.service.mutations.git_state import entry_display_path
from chrys.service.mutations.types import FileHashDiff, FileMutationTextSnapshot

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.app.tui.screens.main.ports import DiffView
    from chrys.app.tui.util.diff_entries import DiffFileEntry, DiffLoadResult
    from chrys.service.mutations.tracker import MutationTracker

logger = logging.getLogger(__name__)

_LIFECYCLE_WAIT_TIMEOUT_SECONDS = 1.0


class LiveDiffToolResult(Protocol):
    """Tool-result event surface needed for live diff accumulation."""

    call_id: str
    metadata: dict[str, Any]


class DiffEngine(Protocol):
    """Engine state needed to identify a captured live turn."""

    mutation_coordinator: object | None
    mutation_tracker: MutationTracker | None

    async def refresh_mutation_attribution(self, *, force: bool = False) -> bool: ...


@dataclass(frozen=True)
class LiveDiffOwner:
    """Session and run identity that owns one live mutation snapshot."""

    session_id: str
    session_generation: int
    run_generation: int


def _metadata_bool(value: object, *, default: bool) -> bool:
    """Read a boolean metadata value while preserving explicit ``False``."""
    return value if isinstance(value, bool) else default


def _metadata_hashes(value: object) -> FileHashDiff:
    """Read before/after hashes from event metadata."""
    if isinstance(value, FileHashDiff):
        return value
    if value is not None:
        logger.debug("dropping unexpected file_mutation_hashes payload: %r", value)
    return FileHashDiff(before=None, after=None)


class LiveDiffTracker:
    """Accumulate current-turn file mutations for the diff viewer."""

    def __init__(
        self,
        *,
        call_paths: MutableMapping[str, str] | None = None,
        file_mutations: MutableMapping[str, LiveFileMutation] | None = None,
        owner: LiveDiffOwner | None = None,
    ) -> None:
        self.call_paths: MutableMapping[str, str] = call_paths if call_paths is not None else {}
        self.file_mutations: MutableMapping[str, LiveFileMutation] = (
            file_mutations if file_mutations is not None else {}
        )
        self.owner = owner

    def reset_for_run_start(self, owner: LiveDiffOwner) -> None:
        """Clear current-turn state and bind subsequent mutations to *owner*."""
        self.call_paths.clear()
        self.file_mutations.clear()
        self.owner = owner

    def record_tool_start(
        self,
        call_id: str,
        tool_name: str,
        args: dict[str, Any] | None,
        *,
        workspace_cwd: str,
    ) -> None:
        """Remember file-tool paths so results can attach snapshots."""
        if tool_name not in ("write_file", "edit_file") or not args:
            return
        path = args.get("file_path") or args.get("path", "")
        if isinstance(path, str) and path:
            # Match the mutation ledger and shell snapshots before merging:
            # tool paths are relative to the workspace, not the process cwd.
            resolved = resolve_workspace_path(path, base_cwd=workspace_cwd)
            self.call_paths[call_id] = entry_display_path(resolved)

    def record_tool_result(self, event: LiveDiffToolResult) -> bool:
        """Accumulate snapshots and report whether this result carries file state."""
        recorded = False
        metadata = event.metadata
        file_snapshot = metadata.get("file_snapshot")
        if (
            isinstance(file_snapshot, tuple)
            and len(file_snapshot) == 2
            and event.call_id in self.call_paths
            and isinstance(file_snapshot[0], str)
            and isinstance(file_snapshot[1], str)
        ):
            recorded = True
            path = self.call_paths[event.call_id]
            before_text, after_text = file_snapshot
            hashes = _metadata_hashes(metadata.get("file_mutation_hashes"))
            op = metadata.get("file_mutation_op")
            changed_at = metadata.get("file_changed_at")
            skip = hashes.after_skip or hashes.before_skip
            self.accumulate_live_mutation(
                path,
                before_text,
                after_text,
                op if isinstance(op, str) else None,
                bytes_changed=_metadata_bool(
                    metadata.get("file_bytes_changed"),
                    default=before_text != after_text,
                ),
                before_hash=hashes.before,
                after_hash=hashes.after,
                content_omitted=skip.value if skip else "",
                last_changed_at=float(changed_at) if isinstance(changed_at, int | float) else 0.0,
            )
        return self.accumulate_shell_snapshots(metadata) or recorded

    def accumulate_live_mutation(
        self,
        path: str,
        before_text: str,
        after_text: str,
        op_str: str | None = None,
        *,
        bytes_changed: bool | None = None,
        before_hash: str | None = None,
        after_hash: str | None = None,
        source: str = "",
        content_omitted: str = "",
        provenance: str = "",
        contested: bool = False,
        last_changed_at: float = 0.0,
    ) -> None:
        """Record a single live file mutation for the current turn."""
        latest_op = infer_live_op(before_text, op_str)
        latest_bytes_changed = before_text != after_text if bytes_changed is None else bytes_changed
        latest = LiveFileMutation(
            before_text=before_text,
            after_text=after_text,
            operation=latest_op,
            bytes_changed=latest_bytes_changed,
            before_hash=before_hash,
            after_hash=after_hash,
            source=source,
            content_omitted=content_omitted,
            provenance=provenance,
            contested=contested,
            last_changed_at=last_changed_at,
        )
        existing = self.file_mutations.get(path)
        if existing is not None:
            merged = merge_live_mutation(existing, latest)
            if merged is None:
                self.file_mutations.pop(path, None)
            else:
                self.file_mutations[path] = merged
            return

        if latest_op == "modify" and before_text == after_text and not latest_bytes_changed:
            return
        self.file_mutations[path] = latest

    def accumulate_shell_snapshots(self, metadata: dict[str, Any]) -> bool:
        """Accumulate shell-detected file mutations from event metadata."""
        shell_snapshots = metadata.get("shell_file_snapshots")
        if not isinstance(shell_snapshots, dict):
            return False
        recorded = False
        for path, snapshot in shell_snapshots.items():
            if not isinstance(path, str) or not isinstance(snapshot, FileMutationTextSnapshot):
                logger.debug("dropping unexpected shell snapshot payload for %r: %r", path, snapshot)
                continue
            recorded = True
            skip = snapshot.after_skip or snapshot.before_skip
            self.accumulate_live_mutation(
                path,
                snapshot.before_text,
                snapshot.after_text,
                snapshot.operation,
                bytes_changed=snapshot.bytes_changed,
                before_hash=snapshot.before_hash,
                after_hash=snapshot.after_hash,
                source=snapshot.source,
                content_omitted=skip.value if skip else "",
                provenance=snapshot.provenance,
                contested=snapshot.contested,
                last_changed_at=snapshot.last_changed_at,
            )

        return recorded

    def has_live_entries(self) -> bool:
        """Return whether any live mutations are currently tracked."""
        return bool(self.file_mutations)

    def is_owned_by(self, owner: LiveDiffOwner) -> bool:
        """Return whether the tracked mutations belong to *owner*."""
        return self.owner == owner

    def snapshot_file_mutations(self) -> dict[str, LiveFileMutation]:
        """Return an immutable-value snapshot of the current live turn."""
        return dict(self.file_mutations)


@dataclass(frozen=True)
class DiffRequest:
    """One request-time snapshot shared by the modal and sidebar loader."""

    engine: DiffEngine | None
    cwd: str
    session_id: str
    live_mutations: dict[str, LiveFileMutation]
    live_tracker_turn_count: int | None
    live_scope: MutationScope | None
    live_run_generation: int | None
    lifecycle_task: asyncio.Task[None] | None


@dataclass(frozen=True)
class CurrentDiffLoad:
    """The disk baseline and its live overlay; only an accepted result may be cached."""

    persisted: DiffLoadResult
    merged: DiffLoadResult


class DiffController:
    """Open the full-screen diff viewer using persisted and live changes."""

    def __init__(
        self,
        *,
        services: MainScreenServices,
        live_diff: LiveDiffTracker,
        view: DiffView,
        workspace_cwd: Callable[[], str],
        is_agent_running: Callable[[], bool],
        run_generation: Callable[[], int],
        session_generation: Callable[[], int],
        turn_lifecycle_task: Callable[[], asyncio.Task[None] | None] | None = None,
        turn_lifecycle_saved: Callable[[asyncio.Task[None]], bool] | None = None,
    ) -> None:
        self._services = services
        self._live_diff = live_diff
        self._view = view
        self._workspace_cwd = workspace_cwd
        self._is_agent_running = is_agent_running
        self._run_generation = run_generation
        self._session_generation = session_generation
        self._turn_lifecycle_task = turn_lifecycle_task or (lambda: None)
        self._turn_lifecycle_saved = turn_lifecycle_saved or (lambda _task: False)

    def show_diff(self, *, focus: DiffFocus | None = None) -> None:
        """Push the loading shell before reading persisted or live changes."""
        request = self._capture_request()

        async def load_data() -> DiffLoadResult:
            return (await self.load_current(request=request)).merged

        platform_label = get_platform().display_label
        self._view.open_diff_screen(
            {},
            cwd=request.cwd,
            subtitle_parts=(platform_label,) if platform_label else (),
            session_id=request.session_id,
            all_entries=[],
            load_data=load_data,
            focus=focus,
        )

    def _capture_request(self) -> DiffRequest:
        engine_provider = self._services.engine_provider
        engine = cast("DiffEngine", engine_provider()) if engine_provider is not None else None
        cwd = self._workspace_cwd()
        session_id = self._view.current_chat_session_id()
        live_mutations: dict[str, LiveFileMutation] = {}
        live_tracker_turn_count: int | None = None
        live_scope: MutationScope | None = None
        live_run_generation: int | None = None
        captured_lifecycle_task: asyncio.Task[None] | None = None
        request_owner = LiveDiffOwner(
            session_id=session_id,
            session_generation=self._session_generation(),
            run_generation=self._run_generation(),
        )
        if self._live_diff.has_live_entries() and self._live_diff.is_owned_by(request_owner):
            # Capture one coherent request-time view. The generation token
            # later distinguishes finalization and a next-run reset without
            # rereading this mutable tracker after the deferred disk load.
            live_mutations = self._live_diff.snapshot_file_mutations()
            live_run_generation = self._run_generation()
            captured_lifecycle_task = self._turn_lifecycle_task()
            if engine is not None and engine.mutation_tracker is not None:
                live_tracker_turn_count = len(engine.mutation_tracker.get_all_turns())
                period = engine.mutation_tracker.current_period
                live_scope = period.scope if period is not None else None

        return DiffRequest(
            engine,
            cwd,
            session_id,
            live_mutations,
            live_tracker_turn_count,
            live_scope,
            live_run_generation,
            captured_lifecycle_task,
        )

    async def load_current(
        self,
        *,
        request: DiffRequest | None = None,
        persisted: DiffLoadResult | None = None,
        include_content: bool = True,
    ) -> CurrentDiffLoad:
        """Load the current session, or merge a cached disk result with a fresh live snapshot."""
        request = request or self._capture_request()
        engine, cwd, session_id = request.engine, request.cwd, request.session_id
        live_mutations = request.live_mutations
        live_tracker_turn_count = request.live_tracker_turn_count
        live_scope = request.live_scope
        live_run_generation = request.live_run_generation
        captured_lifecycle_task = request.lifecycle_task
        if persisted is None and engine is not None and engine.mutation_coordinator is not None:
            try:
                await engine.refresh_mutation_attribution()
            except Exception:
                logger.debug("Pre-diff attribution refresh failed", exc_info=True)
        # A save already completed before the read cannot race that read.
        # Keep cached-only refreshes in memory after the turn has finished.
        persistence_finalized = (
            captured_lifecycle_task is not None
            and captured_lifecycle_task.done()
            and self._turn_lifecycle_saved(captured_lifecycle_task)
        )
        diff_data = (
            persisted
            if persisted is not None
            else await asyncio.to_thread(self._load_persisted_diff, session_id, cwd, include_content=include_content)
        )
        live_turn_still_active = (
            live_run_generation is not None
            and self._is_agent_running()
            and self._run_generation() == live_run_generation
        )
        if captured_lifecycle_task is not None and not live_turn_still_active and not persistence_finalized:
            await _lifecycle_task_completed_successfully(captured_lifecycle_task)
            # The exact task's successful final save is the authority
            # boundary. After-turn hooks may fail after the durable write;
            # that must not make an older live snapshot override disk.
            persistence_finalized = self._turn_lifecycle_saved(captured_lifecycle_task)
            if persistence_finalized:
                # The first read may have raced the exact task's final
                # save. Re-read after observing its save outcome.
                diff_data = await asyncio.to_thread(
                    self._load_persisted_diff, session_id, cwd, include_content=include_content
                )
        merged = await asyncio.to_thread(
            self._merge_live_diff,
            diff_data,
            cwd,
            live_mutations,
            live_tracker_turn_count,
            persistence_finalized,
            live_scope,
        )
        return CurrentDiffLoad(persisted=diff_data, merged=merged)

    def show_workflow_diff(self, session_id: str, cwd: str) -> None:
        """Read one workflow ledger, including a stable snapshot of its active run."""
        from chrys.app.tui.screens.diff import DiffLoadResult, load_diff_entries_by_period

        provider = self._services.engine_provider
        snapshot = provider().workflows.mutation_snapshot(session_id) if provider is not None and session_id else None
        store = self._services.state_store

        async def load_data() -> DiffLoadResult:
            if store is None or not session_id:
                return DiffLoadResult.empty()
            return await asyncio.to_thread(load_diff_entries_by_period, store, session_id, cwd, mutations_data=snapshot)

        self._view.open_diff_screen(
            {}, cwd=cwd, subtitle_parts=(), session_id=session_id, all_entries=[], load_data=load_data
        )

    def preview_entries(self, data: DiffLoadResult, session_id: str) -> tuple[DiffFileEntry, ...]:
        """Sort metadata off the UI thread, without waiting for line counts."""
        return sorted_preview_entries(data)

    def preview_directory(self, session_id: str) -> Path | None:
        """Snapshot namespace captured along with the current preview entries."""
        store = self._services.state_store
        return store.session_dir(session_id) if store is not None and session_id else None

    def _load_persisted_diff(self, session_id: str, cwd: str, *, include_content: bool = True) -> DiffLoadResult:
        """Load persisted entries; async callers run this method in a thread."""
        from chrys.app.tui.screens.diff import DiffLoadResult, load_diff_entries_by_period

        if session_id and self._services.state_store:
            if include_content:
                return load_diff_entries_by_period(self._services.state_store, session_id, cwd)
            return load_diff_entries_by_period(self._services.state_store, session_id, cwd, include_content=False)
        return DiffLoadResult.empty()

    def _merge_live_diff(
        self,
        diff_data: object,
        cwd: str,
        live_mutations: dict[str, LiveFileMutation],
        live_tracker_turn_count: int | None,
        persistence_finalized: bool,
        live_scope: MutationScope | None = None,
    ) -> DiffLoadResult:
        """Merge a request-time live snapshot without duplicating a finalized turn."""
        from chrys.app.tui.screens.diff import DiffLoadResult

        if not isinstance(diff_data, DiffLoadResult):
            raise TypeError("diff loader returned an invalid result")

        periods_data = dict(diff_data.per_period_entries)
        scopes = dict(diff_data.scopes)
        live_turn_id: int | None = None
        all_entries = list(diff_data.all_entries)
        live_files = build_live_diff_entries(live_mutations, cwd)
        if not live_files:
            return diff_data

        if live_tracker_turn_count is not None:
            if diff_data.total_periods > live_tracker_turn_count:
                # Persistence has advanced beyond the captured live turn, so
                # finalization definitely won the race and its rows are newer.
                return diff_data

            live_turn_id = live_tracker_turn_count
            if diff_data.total_periods == live_tracker_turn_count:
                if persistence_finalized:
                    # Finalization does not increment the tracker count. Disk
                    # rows become authoritative only after the exact captured
                    # run task has completed its save and after-turn hooks.
                    return diff_data
                # Persistence may still contain a coordination checkpoint.
                # Overlay the request-time snapshot while retaining
                # persisted-only paths from that turn.
                periods_data[live_turn_id] = _overlay_live_turn_entries(periods_data.get(live_turn_id, []), live_files)
            else:
                # The tracker count proves persistence ends at an earlier
                # turn. Always add the captured turn, even if it happens to be
                # content-identical to the preceding turn (a valid redo).
                periods_data[live_turn_id] = live_files
            all_entries = merge_live_entries_into_all(all_entries, live_files)
        elif not _latest_persisted_turn_matches_live(periods_data, live_files):
            live_turn_id = diff_data.total_periods + 1
            periods_data[live_turn_id] = live_files
            all_entries = merge_live_entries_into_all(all_entries, live_files)
        if live_turn_id is not None and live_scope is not None:
            scopes[live_turn_id] = live_scope
        return DiffLoadResult(
            all_entries=all_entries,
            per_period_entries=periods_data,
            total_periods=diff_data.total_periods,
            scopes=scopes,
        )


async def _lifecycle_task_completed_successfully(
    task: asyncio.Task[None],
    *,
    timeout_seconds: float = _LIFECYCLE_WAIT_TIMEOUT_SECONDS,
) -> bool:
    """Bound one lifecycle wait without cancelling it or swallowing caller cancellation."""
    try:
        async with asyncio.timeout(timeout_seconds):
            await asyncio.shield(task)
    except TimeoutError:
        logger.debug("Timed out waiting for captured turn lifecycle; using live diff snapshot")
        return False
    except asyncio.CancelledError:
        waiter = asyncio.current_task()
        if waiter is not None and waiter.cancelling():
            raise
        if not task.cancelled():
            raise
        logger.debug("Captured turn lifecycle was cancelled before final persistence")
        return False
    except Exception:
        logger.debug("Captured turn lifecycle failed before final persistence", exc_info=True)
        return False
    return True


def _overlay_live_turn_entries(
    persisted_entries: list[DiffFileEntry],
    live_entries: list[DiffFileEntry],
) -> list[DiffFileEntry]:
    """Overlay live content while retaining persisted-only row metadata."""
    by_path = {entry.path: entry for entry in persisted_entries}
    for live_entry in live_entries:
        persisted_entry = by_path.get(live_entry.path)
        if persisted_entry is not None:
            live_entry = replace(
                live_entry,
                old_path=persisted_entry.old_path,
                is_binary=persisted_entry.is_binary,
                encoding=persisted_entry.encoding,
            )
        by_path[live_entry.path] = live_entry
    return sorted(by_path.values(), key=lambda entry: entry.rel_path)


def _latest_persisted_turn_matches_live(
    periods_data: dict[int, list[DiffFileEntry]],
    live_files: list[DiffFileEntry],
) -> bool:
    """Fallback duplicate guard for callers without an engine tracker count."""
    if not periods_data or not live_files:
        return False
    persisted = periods_data[max(periods_data)]
    persisted_keys = {_diff_entry_state_key(entry) for entry in persisted}
    live_keys = {_diff_entry_state_key(entry) for entry in live_files}
    return bool(live_keys) and live_keys <= persisted_keys


def _diff_entry_state_key(entry: DiffFileEntry) -> tuple[object, ...]:
    """Content identity used only to recognize an already-persisted live row."""
    return (
        entry.path,
        entry.operation,
        entry.old_path,
        entry.before_text,
        entry.after_text,
        entry.bytes_changed,
        entry.before_hash,
        entry.after_hash,
        entry.content_omitted,
    )


def sorted_preview_entries(data: DiffLoadResult) -> tuple[DiffFileEntry, ...]:
    """Newest observed change first, with a stable display-path tie break."""
    from chrys.app.tui.util.diff_labels import entry_display_key, filter_and_dedupe_entries

    return tuple(
        sorted(
            filter_and_dedupe_entries(data.all_entries),
            key=lambda entry: (-entry.last_changed_at, entry_display_key(entry)),
        )
    )


class DiffPreviewController:
    """Coalesce refreshes and publish serial progress while the current owner stays valid."""

    def __init__(
        self,
        *,
        diff: DiffController,
        owner: Callable[[], tuple[str, int, str]],
        visible: Callable[[], bool],
        show: Callable[[tuple[DiffFileEntry, ...]], None],
        start_worker: Callable[[Callable[[], Awaitable[object]]], object],
    ) -> None:
        self._diff = diff
        self._owner = owner
        self._visible = visible
        self._show = show
        self._start_worker = start_worker
        self._cached: DiffLoadResult | None = None
        self._cached_owner: tuple[str, int, str] | None = None
        self._seq = 0
        self._reread_seq = 0
        self._reread_needed = True
        self._stale = True
        self._refreshing = False
        self._counts = PreviewCountCache()
        self._counting = False

    def request_refresh(self, *, reread: bool = False) -> None:
        owner = self._owner()
        if owner != self._cached_owner:
            previous = self._cached_owner
            self._cached = None
            self._cached_owner = owner
            self._counts = PreviewCountCache()
            self._reread_needed = True
            if previous is None or (previous[0], previous[2]) != (owner[0], owner[2]):
                self._show(())
        if reread:
            self._reread_seq += 1
        self._reread_needed |= reread
        self._seq += 1
        self._stale = True
        self.screen_resumed()

    def screen_resumed(self) -> None:
        """Resume deferred work when Chat is visible, including after an overlay."""
        if self._owner() != self._cached_owner:
            self.request_refresh(reread=True)
            return
        if self._stale and not self._refreshing and self._visible():
            self._refreshing = True
            self._start_worker(self._refresh)
        elif not self._stale and self._visible() and self._counts.entries:
            self._show(self._counts.entries)
        self._resume_counts()

    def _resume_counts(self) -> None:
        if not self._counting and self._visible() and self._counts.next_pending() is not None:
            self._counting = True
            self._start_worker(self._count)

    async def _count(self) -> None:
        """One independent worker follows the latest list after every file."""
        try:
            while self._visible():
                cache, owner = self._counts, self._cached_owner
                entry = cache.next_pending()
                if entry is None:
                    return
                directory = cache.directory
                remember = True
                try:
                    counted = await count_preview_entry(entry, directory)
                except Exception:
                    logger.debug("Unable to count sidebar file changes", exc_info=True)
                    counted = replace(entry, line_count_result="unavailable")
                    # A failed file must not hold up other rows. Retry transient
                    # errors on the next list refresh, without another state.
                    remember = False
                if cache is not self._counts:
                    continue
                if owner != self._owner():
                    # The session moved on before its event arrived: refetch
                    # for the new owner, whose refresh resumes counting.
                    self.request_refresh(reread=True)
                    return
                if cache.accept(counted, directory, remember=remember) and self._visible():
                    self._show(cache.entries)
        finally:
            self._counting = False

    async def _refresh(self) -> None:
        try:
            while self._stale and self._visible():
                if self._owner() != self._cached_owner:
                    self.request_refresh(reread=True)
                seq, owner = self._seq, self._owner()
                reread, reread_seq = self._reread_needed, self._reread_seq
                try:
                    result = await self._diff.load_current(
                        persisted=None if reread else self._cached, include_content=False
                    )
                    entries = await asyncio.to_thread(self._diff.preview_entries, result.merged, owner[0])
                except Exception:
                    logger.debug("Unable to refresh sidebar file changes", exc_info=True)
                    if seq == self._seq:
                        self._stale = False
                        return
                    continue
                if owner != self._owner():
                    continue
                if not self._visible():
                    return
                self._cached = result.persisted
                self._cached_owner = owner
                if reread and reread_seq == self._reread_seq:
                    self._reread_needed = False
                # One worker computes serially: even with newer requests pending,
                # this result advances the display. Only an owner change invalidates it.
                self._stale = seq != self._seq
                self._show(self._counts.fill(entries, self._diff.preview_directory(owner[0])))
                self._resume_counts()
        finally:
            self._refreshing = False
