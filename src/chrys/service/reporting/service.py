# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The collector: one background round per lifecycle event, never in the handler.

EventBus handlers here only record which part of a session needs attention and
wake one driver task; every byte of IO, hashing and JSON happens in a worker
thread (``asyncio.to_thread``) or in the pump thread. That is what keeps the
event loop's work at zero: a turn may not slow down because reporting is on.

The lifecycle binding follows the design's table:

* ``SessionReady`` / ``SessionRestored`` — bind the session and send
  ``repo.identity`` and ``agent.env`` (never the workspace);
* ``AgentRuntimeUpdated`` — refresh the runtime facts and re-arm the
  environment capture when they actually changed;
* ``SessionSaved`` — read the trajectory, tap the snapshot and capture the
  post-turn workspace;
* ``TurnStarted`` — the pre-turn workspace signature check;
* ``ExecutionChanged`` on final release — the turn-end fallback read;
* ``SessionDeleted`` — unbind; frames already queued are still sent.

One process runs one service, but a process may host several buses (one
``ChrysSessionHost`` per ACP session, say): :meth:`attach` subscribes the same
handlers on each of them, and :meth:`detach` releases a bus whose host shut
down — the service closes once the last bus is gone.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

from chrys import __version__
from chrys.foundation.config.settings import resolve_sessions_dir
from chrys.foundation.events.types import (
    AgentRuntimeDetails,
    AgentRuntimeUpdated,
    ExecutionChanged,
    SessionDeleted,
    SessionReady,
    SessionRestored,
    SessionSaved,
    TurnStarted,
)
from chrys.foundation.reporting.config import ReportConfig
from chrys.foundation.reporting.diagnostics import DiagnosticsSnapshot, ReportDiagnostics
from chrys.foundation.reporting.frames import (
    KIND_AGENT_ENV,
    KIND_REPO_IDENTITY,
    KIND_SESSION_META,
    Frame,
    scope_agent_env,
    scope_repo_identity,
    scope_session_meta,
    sha256_hex,
)
from chrys.foundation.reporting.manifest import ManifestReconciler, ManifestRequest
from chrys.foundation.reporting.pump import HttpxTransport, ReportPump, ReportTransport
from chrys.foundation.reporting.wal import DurableWAL
from chrys.foundation.util.once_close import OnceClose
from chrys.foundation.util.session_ids import session_short_id
from chrys.foundation.util.time import utc_iso
from chrys.service.reporting.agent_config import (
    SessionFacts,
    agent_environment_payload,
    collect_agent_environment,
)
from chrys.service.reporting.cursors import SessionCursor, SessionCursorStore
from chrys.service.reporting.meta import (
    WORKSPACE_KIND_GIT,
    WORKSPACE_KIND_NO_REPO,
    WORKSPACE_KIND_NON_GIT,
    ReportMeta,
    session_meta_payload,
)
from chrys.service.reporting.repository import RepositoryProbe, probe_repository
from chrys.service.reporting.snapshot import SnapshotTap
from chrys.service.reporting.tailer import TrajectoryTailer
from chrys.service.reporting.workspace import WorkspaceCapture
from chrys.service.state._session_files import SESSION_FILE_NAME
from chrys.service.trajectory.session import TRAJECTORY_DIR_NAME, TRAJECTORY_EVENTS_FILE_NAME

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import Event

    LifecycleHandler = Callable[..., Awaitable[None]]
    """One bound lifecycle handler; the dispatch table is heterogeneous."""

logger = logging.getLogger(__name__)

MAX_BOUND_SESSIONS: Final = 64
"""Bound on remembered sessions (ACP may run many in one process)."""


@dataclass(slots=True)
class _Binding:
    """What the handlers know about one session, in primitives only."""

    session_id: str
    primary_cwd: str = ""
    profile_name: str = ""
    facts: SessionFacts | None = None
    environment_pending: bool = False
    repository_pending: bool = False
    trajectory_pending: bool = False
    snapshot_pending: bool = False
    workspace_pending: bool = False
    meta_pending: bool = False


@dataclass(frozen=True, slots=True)
class _CollectRequest:
    """One session's work for one round, handed to the worker thread."""

    session_id: str
    primary_cwd: str
    profile_name: str
    facts: SessionFacts | None
    environment: bool
    repository: bool
    trajectory: bool
    snapshot: bool
    workspace: bool
    meta: ReportMeta | None


def _facts_from_runtime(details: AgentRuntimeDetails) -> SessionFacts:
    """The agent-config facts of one runtime snapshot, in primitives only."""
    hooks_count = 0
    hook_events: set[str] = set()
    hook_sources: list[tuple[str, str]] = []
    for source in details.hook_sources:
        hook_sources.append((source.scope, source.source_path))
        for hook in source.hooks:
            hooks_count += 1
            if hook.event:
                hook_events.add(hook.event)
    return SessionFacts(
        model_profile_id=details.model.profile_id,
        model_provider=details.model.provider,
        model_id=details.model.model_id,
        skills=tuple((skill.name, skill.source) for skill in details.skill_details),
        hooks_count=hooks_count,
        hook_events=tuple(sorted(hook_events)),
        hook_sources=tuple(hook_sources),
    )


class ReportingService:
    """Bind reporting to the session lifecycle and run the collection rounds."""

    def __init__(
        self,
        bus: EventBus,
        config: ReportConfig,
        *,
        transport: ReportTransport | None = None,
    ) -> None:
        self._bus = bus
        self._config = config
        self._diagnostics = ReportDiagnostics()
        self._bindings: OrderedDict[str, _Binding] = OrderedDict()
        self._metas: dict[str, ReportMeta] = {}
        self._wake = asyncio.Event()
        self._lock = threading.Lock()
        self._task: asyncio.Task[None] | None = None
        self._closer: OnceClose | None = None
        self._started = False
        self._closed = False
        self._transport = transport or HttpxTransport()
        self._wal: DurableWAL | None = None
        self._pump: ReportPump | None = None
        self._cursors: SessionCursorStore | None = None
        self._tailer: TrajectoryTailer | None = None
        self._snapshot: SnapshotTap | None = None
        self._workspace: WorkspaceCapture | None = None
        self._reconciler: ManifestReconciler | None = None
        self._subscriptions: list[tuple[EventBus, type[Event], LifecycleHandler]] = []

    @property
    def diagnostics(self) -> DiagnosticsSnapshot:
        """The counters, for a diagnostics surface."""
        return self._diagnostics.snapshot()

    async def start(self) -> None:
        """Open the queue, start the pump, subscribe and start the driver."""
        if self._started:
            return
        wal = DurableWAL(
            self._config.directory, queue_bytes_max=self._config.queue_bytes_max, diagnostics=self._diagnostics
        )
        # Recovery walks every unacknowledged frame header in the log — after a
        # crash with a full queue that is seconds of IO, so it never runs on
        # the event loop (a frontend calls this between its first paint and
        # its first turn).
        await asyncio.to_thread(wal.open)
        self._started = True
        self._wal = wal
        self._cursors = SessionCursorStore(wal.cursors_dir)
        self._tailer = TrajectoryTailer(wal)
        self._snapshot = SnapshotTap(wal)
        self._workspace = WorkspaceCapture(wal)
        self._reconciler = ManifestReconciler(self._config, self._transport)
        if self._config.deliverable:
            self._pump = ReportPump(wal, self._config, self._diagnostics, transport=self._transport)
            self._pump.start()
        self._closer = OnceClose(self._close_once)
        await self._subscribe_all(self._bus)
        self._task = asyncio.create_task(self._run(), name="chrys-reporting")

    async def attach(self, bus: EventBus) -> None:
        """Observe another frontend's bus: a second session host in this process.

        Without this, only the first host's sessions would ever be collected,
        because every host owns its own bus.
        """
        if self._closed:
            return
        await self._subscribe_all(bus)

    async def detach(self, bus: EventBus) -> bool:
        """Stop observing *bus* — its host is shutting down.

        The runtime closes once no bus is left, so its pump gets a bounded drain
        instead of observing a dead bus forever. Returns whether any bus is
        still observed.
        """
        remaining: list[tuple[EventBus, type[Event], LifecycleHandler]] = []
        for record in self._subscriptions:
            if record[0] is bus:
                subscribed_bus, event_type, handler = record
                with contextlib.suppress(Exception):
                    await subscribed_bus.unsubscribe(event_type, handler)
            else:
                remaining.append(record)
        self._subscriptions = remaining
        if not remaining:
            await self.close()
            return False
        return True

    async def _subscribe_all(self, bus: EventBus) -> None:
        """Subscribe every lifecycle handler on *bus*; idempotent per bus."""
        if any(recorded is bus for recorded, _event_type, _handler in self._subscriptions):
            return
        # Recorded before subscribing: a failure part-way through leaves the
        # records a superset of reality, and unsubscribing a handler that never
        # landed is a safe no-op.
        self._subscriptions.extend(
            [
                (bus, SessionReady, self._on_session_ready),
                (bus, SessionRestored, self._on_session_restored),
                (bus, AgentRuntimeUpdated, self._on_agent_runtime_updated),
                (bus, SessionSaved, self._on_session_saved),
                (bus, TurnStarted, self._on_turn_started),
                (bus, ExecutionChanged, self._on_execution_changed),
                (bus, SessionDeleted, self._on_session_deleted),
            ]
        )
        await bus.subscribe(SessionReady, self._on_session_ready)
        await bus.subscribe(SessionRestored, self._on_session_restored)
        await bus.subscribe(AgentRuntimeUpdated, self._on_agent_runtime_updated)
        await bus.subscribe(SessionSaved, self._on_session_saved)
        await bus.subscribe(TurnStarted, self._on_turn_started)
        await bus.subscribe(ExecutionChanged, self._on_execution_changed)
        await bus.subscribe(SessionDeleted, self._on_session_deleted)

    async def close(self) -> None:
        """Stop collecting, drain the pump's budget and keep unsent frames queued."""
        closer = self._closer
        if closer is None:
            return
        await closer()

    def offer_meta(self, meta: ReportMeta) -> None:
        """Accept the save hook's immutable row; never raises, never blocks.

        Only a bound session's row is kept: an unbound id would never be
        drained and would only grow the dictionary.
        """
        try:
            binding = self._bindings.get(meta.session_id)
            if binding is None:
                return
            with self._lock:
                self._metas[meta.session_id] = meta
            binding.meta_pending = True
            binding.snapshot_pending = True
            binding.trajectory_pending = True
            binding.workspace_pending = True
            self._wake.set()
        except Exception:  # the hook runs inside the save path
            logger.warning("Reporting could not accept the session meta", exc_info=True)

    async def _on_session_ready(self, event: SessionReady) -> None:
        binding = self._bind(event.session_id or "", event.primary_cwd)
        if binding is None:
            return
        binding.profile_name = event.agent_profile
        binding.facts = _facts_from_runtime(event.runtime_details)
        binding.environment_pending = True
        binding.repository_pending = True
        self._wake.set()

    async def _on_session_restored(self, event: SessionRestored) -> None:
        binding = self._bind(event.session_id or "", event.primary_cwd)
        if binding is None:
            return
        binding.profile_name = event.agent_profile
        # A restore does not carry runtime facts; the first
        # AgentRuntimeUpdated or rebuild fills them in and re-arms the capture.
        binding.environment_pending = True
        binding.repository_pending = True
        binding.trajectory_pending = True
        binding.snapshot_pending = True
        self._wake.set()

    async def _on_agent_runtime_updated(self, event: AgentRuntimeUpdated) -> None:
        # Runtime facts changed without a rebuild (model switch, skill refresh):
        # only a real change re-arms the environment capture, so a per-turn
        # skill refresh costs a comparison and nothing else.
        binding = self._bindings.get(event.session_id or "")
        if binding is None:
            return
        facts = _facts_from_runtime(event.runtime_details)
        if facts == binding.facts:
            return
        binding.facts = facts
        binding.environment_pending = True
        self._wake.set()

    async def _on_session_saved(self, event: SessionSaved) -> None:
        binding = self._bindings.get(event.session_id or "")
        if binding is None:
            return
        binding.trajectory_pending = True
        binding.snapshot_pending = True
        binding.workspace_pending = True
        self._wake.set()

    async def _on_turn_started(self, event: TurnStarted) -> None:
        # The turn baseline: a signature check decides whether the still
        # snapshot from the last turn end is still true.
        binding = self._bindings.get(event.session_id or "")
        if binding is None:
            return
        binding.workspace_pending = True
        self._wake.set()

    async def _on_execution_changed(self, event: ExecutionChanged) -> None:
        # Only the final lease release is a turn-end fallback trigger; the
        # admission that starts a turn is not.
        if event.snapshot.kind != "idle":
            return
        binding = self._bindings.get(event.session_id or "")
        if binding is None:
            return
        binding.trajectory_pending = True
        self._wake.set()

    async def _on_session_deleted(self, event: SessionDeleted) -> None:
        session_id = event.session_id or ""
        with self._lock:
            self._bindings.pop(session_id, None)
            self._metas.pop(session_id, None)
        cursors = self._cursors
        if cursors is not None:
            await asyncio.to_thread(cursors.delete, session_id)

    def _bind(self, session_id: str, primary_cwd: str) -> _Binding | None:
        if not session_id:
            return None
        binding = self._bindings.get(session_id)
        if binding is None:
            binding = _Binding(session_id=session_id)
            self._bindings[session_id] = binding
            while len(self._bindings) > MAX_BOUND_SESSIONS:
                self._bindings.popitem(last=False)
        if primary_cwd:
            binding.primary_cwd = primary_cwd
        self._bindings.move_to_end(session_id)
        return binding

    async def _run(self) -> None:
        """Wake on any lifecycle change, or once per poll interval, and collect."""
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._config.poll_interval_seconds)
            self._wake.clear()
            while True:
                requests = self._drain()
                deferred = self._take_deferred_sessions()
                if not requests and not deferred:
                    break
                try:
                    reopened = await asyncio.to_thread(self._collect, requests, deferred)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning("Reporting collection round failed", exc_info=True)
                    break
                if not reopened:
                    break

    def _drain(self) -> list[_CollectRequest]:
        requests: list[_CollectRequest] = []
        with self._lock:
            for binding in self._bindings.values():
                meta = self._metas.pop(binding.session_id, None) if binding.meta_pending else None
                if not (
                    binding.environment_pending
                    or binding.repository_pending
                    or binding.trajectory_pending
                    or binding.snapshot_pending
                    or binding.workspace_pending
                    or binding.meta_pending
                ):
                    continue
                requests.append(
                    _CollectRequest(
                        session_id=binding.session_id,
                        primary_cwd=binding.primary_cwd,
                        profile_name=binding.profile_name,
                        facts=binding.facts,
                        environment=binding.environment_pending,
                        repository=binding.repository_pending,
                        trajectory=binding.trajectory_pending,
                        snapshot=binding.snapshot_pending,
                        workspace=binding.workspace_pending,
                        meta=meta,
                    )
                )
                binding.environment_pending = False
                binding.repository_pending = False
                binding.trajectory_pending = False
                binding.snapshot_pending = False
                binding.workspace_pending = False
                binding.meta_pending = False
        return requests

    def _take_deferred_sessions(self) -> tuple[str, ...]:
        """Sessions the pump saw deferred or dropped since the last round."""
        pump = self._pump
        return () if pump is None else pump.take_deferred_sessions()

    # ── worker-thread side ────────────────────────────────────────────────

    def _collect(self, requests: list[_CollectRequest], deferred: Sequence[str] = ()) -> bool:
        """Every read, hash, encode and enqueue of one round.

        Returns whether a negotiation re-opened work, so the driver runs the
        next round at once instead of waiting out the poll interval.
        """
        wal = self._wal
        cursors = self._cursors
        if wal is None or cursors is None:
            return False
        diagnostics = self._diagnostics
        reopened = False
        for session_id in deferred:
            try:
                reopened = self.reconcile(session_id) or reopened
            except Exception:  # a failed negotiation never stops the queue
                logger.warning("Reporting could not reconcile session %s", session_id, exc_info=True)
        for request in requests:
            try:
                self._collect_one(request, cursors)
            except Exception:
                logger.warning("Reporting could not collect session %s", request.session_id, exc_info=True)
        wal.flush()
        diagnostics.set_queued_bytes(wal.stats().pending_bytes)
        pump = self._pump
        if pump is not None:
            pump.wake()
        return reopened

    def _collect_one(self, request: _CollectRequest, cursors: SessionCursorStore) -> None:
        wal = self._wal
        if wal is None:
            return
        cursor = cursors.load(request.session_id)
        dirty = False
        probe = self._probe(request, cursor)
        if request.repository and not cursor.repository_sent:
            dirty |= self._send_repository(request.session_id, probe, cursor)
        if request.environment and self._config.agent_config:
            # The classification rides the agent.env envelope, and a change of
            # it re-sends even an unchanged payload.
            workspace_kind = (probe.workspace_kind if probe is not None else "") or cursor.workspace_kind
            dirty |= self._send_agent_environment(request, cursor, workspace_kind)
        session_dir = _session_dir(request.session_id)
        tailer = self._tailer
        if request.trajectory and tailer is not None:
            events_path = session_dir / TRAJECTORY_DIR_NAME / TRAJECTORY_EVENTS_FILE_NAME
            before = (cursor.trajectory_offset, cursor.trajectory_last_sequence)
            tailer.collect(request.session_id, events_path, cursor)
            dirty |= (cursor.trajectory_offset, cursor.trajectory_last_sequence) != before
        snapshot = self._snapshot
        if request.snapshot and snapshot is not None:
            before_snapshot = (cursor.snapshot_content_hash, cursor.snapshot_size)
            snapshot.collect(request.session_id, session_dir / SESSION_FILE_NAME, cursor)
            dirty |= (cursor.snapshot_content_hash, cursor.snapshot_size) != before_snapshot
        if request.workspace:
            dirty |= self._capture_workspace(request, probe, cursor)
        if request.meta is not None:
            payload = session_meta_payload(request.meta)
            wal.append(
                [
                    Frame.build(
                        KIND_SESSION_META,
                        request.session_id,
                        scope_session_meta(sha256_hex(payload)),
                        payload,
                    )
                ]
            )
            dirty = True
        if dirty:
            cursors.save(request.session_id, cursor)

    def _probe(self, request: _CollectRequest, cursor: SessionCursor) -> RepositoryProbe | None:
        """The repository probe, reusing a classified non-git workspace."""
        if not request.workspace and not request.repository:
            return None
        if cursor.workspace_kind in {WORKSPACE_KIND_NON_GIT, WORKSPACE_KIND_NO_REPO}:
            return RepositoryProbe(workspace_kind=cursor.workspace_kind)
        return probe_repository(request.primary_cwd)

    def _send_repository(self, session_id: str, probe: RepositoryProbe | None, cursor: SessionCursor) -> bool:
        wal = self._wal
        if wal is None:
            return False
        cursor.repository_sent = True
        if probe is None or probe.identity is None:
            return True
        identity = probe.identity
        payload = identity.payload()
        wal.append(
            [
                Frame.build(
                    KIND_REPO_IDENTITY,
                    session_id,
                    scope_repo_identity(identity.identity),
                    payload,
                )
            ]
        )
        if identity.identity not in cursor.repository_identities:
            cursor.repository_identities = [*cursor.repository_identities, identity.identity][-16:]
        return True

    def _send_agent_environment(self, request: _CollectRequest, cursor: SessionCursor, workspace_kind: str) -> bool:
        """Queue ``agent.env`` when its content or the classification changed.

        The capture moment and the workspace classification ride the frame
        envelope, not the payload, so the payload stays content-addressed and
        one configuration is shared across sessions. An unchanged content hash
        with an unchanged classification queues nothing.
        """
        wal = self._wal
        if wal is None:
            return False
        environment = collect_agent_environment(
            request.facts,
            profile_name=request.profile_name,
            config_dir=self._config.config_dir,
        )
        payload, _redacted = agent_environment_payload(environment, app_version=__version__)
        content_hash = sha256_hex(payload)
        if content_hash == cursor.agent_config_sha256 and workspace_kind == cursor.workspace_kind:
            return False
        cursor.agent_config_sha256 = content_hash
        # Remember the classification this frame carries, so a later round that
        # probes (or does not) compares against the same fact.
        if workspace_kind:
            cursor.workspace_kind = workspace_kind
        wal.append(
            [
                Frame.build(
                    KIND_AGENT_ENV,
                    request.session_id,
                    scope_agent_env(content_hash),
                    payload,
                    workspace_kind=workspace_kind,
                    captured_at=utc_iso(),
                )
            ]
        )
        return True

    def _capture_workspace(
        self,
        request: _CollectRequest,
        probe: RepositoryProbe | None,
        cursor: SessionCursor,
    ) -> bool:
        if not self._config.workspace_capture:
            return False
        workspace = self._workspace
        if workspace is None or probe is None:
            return False
        if probe.workspace_kind != WORKSPACE_KIND_GIT and not probe.root:
            if cursor.workspace_kind != probe.workspace_kind:
                cursor.workspace_kind = probe.workspace_kind
                return True
            return False
        signature = workspace.signature(probe.root, head_sha=probe.head_sha)
        if signature and signature == cursor.workspace_signature:
            return False
        identity = probe.identity.identity if probe.identity is not None else ""
        result = workspace.capture(
            request.session_id,
            probe.root,
            cursor,
            repository_identity=identity,
            head_sha=probe.head_sha,
        )
        cursor.workspace_signature = result.signature or signature
        cursor.workspace_manifest_sha256 = result.manifest_sha256
        cursor.workspace_file_shas = list(dict.fromkeys([*cursor.workspace_file_shas, *result.file_shas]))
        cursor.workspace_kind = WORKSPACE_KIND_GIT
        return True

    def reconcile(self, session_id: str) -> bool:
        """Ask the server which blocks it is missing and forget them locally.

        Returns whether the cursor was re-opened, that is whether the next
        collection round resends the blocks the server named. A failed
        negotiation is not an error: the cursor and the blocks stay as they
        were and the next deferral asks again.
        """
        reconciler = self._reconciler
        cursors = self._cursors
        if reconciler is None or cursors is None or not self._config.deliverable:
            return False
        cursor = cursors.load(session_id)
        if not cursor.snapshot_content_hash and not cursor.workspace_manifest_sha256:
            return False
        answer = reconciler.reconcile(
            session_id,
            ManifestRequest(
                snapshot_content_hash=cursor.snapshot_content_hash,
                chunk_hashes=tuple(cursor.snapshot_chunk_hashes),
                workspace_manifest_sha256=cursor.workspace_manifest_sha256,
                file_shas=tuple(cursor.workspace_file_shas),
            ),
        )
        if answer is None:
            return False
        missing_chunks = set(answer.missing_chunk_hashes)
        if missing_chunks:
            # A blank hash can never match the file's block, so the next tap
            # reads and queues that block again.
            cursor.snapshot_chunk_hashes = [
                "" if value in missing_chunks else value for value in cursor.snapshot_chunk_hashes
            ]
            cursor.snapshot_size = 0
            cursor.snapshot_mtime_ns = 0
        missing_files = set(answer.missing_file_shas)
        if missing_files:
            cursor.workspace_file_shas = [value for value in cursor.workspace_file_shas if value not in missing_files]
            cursor.workspace_signature = ""
        if answer.acked_last_sequence > cursor.trajectory_last_sequence:
            cursor.trajectory_last_sequence = answer.acked_last_sequence
        cursors.save(session_id, cursor)
        reopened = bool(missing_chunks or missing_files)
        if reopened:
            self._reopen_collection(session_id, snapshot=bool(missing_chunks), workspace=bool(missing_files))
        return reopened

    def _reopen_collection(self, session_id: str, *, snapshot: bool, workspace: bool) -> None:
        """Ask the next round to collect the parts the server is still missing."""
        with self._lock:
            binding = self._bindings.get(session_id)
            if binding is None:
                return
            binding.snapshot_pending = binding.snapshot_pending or snapshot
            binding.workspace_pending = binding.workspace_pending or workspace

    async def _close_once(self) -> None:
        self._closed = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        subscriptions, self._subscriptions = self._subscriptions, []
        for subscribed_bus, event_type, handler in subscriptions:
            with contextlib.suppress(Exception):
                await subscribed_bus.unsubscribe(event_type, handler)
        pump = self._pump
        self._pump = None
        if pump is not None:
            # The drain budget bounds the whole shutdown, in-flight request included.
            await asyncio.to_thread(pump.stop)
        transport = self._transport
        with contextlib.suppress(Exception):
            transport.close()
        wal = self._wal
        self._wal = None
        if wal is not None:
            wal.close()

    def shutdown_sync(self, *, timeout: float = 5.0) -> None:
        """Stop the sender and close the queue without an event loop.

        Used at interpreter exit, where no task can run: the pump gets its
        bounded drain and everything unsent stays in the log for the next start.
        """
        self._closed = True
        pump = self._pump
        self._pump = None
        if pump is not None:
            pump.stop(timeout=timeout)
        with contextlib.suppress(Exception):
            self._transport.close()
        wal = self._wal
        self._wal = None
        if wal is not None:
            wal.close()


def _session_dir(session_id: str) -> Path:
    return resolve_sessions_dir(create=False) / session_short_id(session_id)
