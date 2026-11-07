# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The collector: one background round per lifecycle event, never in the handler.

EventBus handlers here only record which part of a session needs attention and
wake one driver task; every byte of IO, hashing and JSON happens in a worker
thread (``asyncio.to_thread``) or in the pump thread. That is what keeps the
event loop's work at zero: a turn may not slow down because reporting is on.

The lifecycle binding follows the design's table:

* ``SessionReady`` / ``SessionRestored`` — bind the session and send
  ``repo.identity`` and ``agent.env`` once (never the workspace);
* ``SessionSaved`` — read the trajectory, tap the snapshot and capture the
  post-turn workspace;
* ``TurnStarted`` — the pre-turn workspace signature check;
* ``ExecutionChanged`` on final release — the turn-end fallback read;
* ``SessionDeleted`` — unbind; frames already queued are still sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

from chrys import __version__
from chrys.foundation.config.settings import Settings, resolve_sessions_dir
from chrys.foundation.events.types import (
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
from chrys.service.reporting.agent_config import AgentEnvironment, agent_environment_payload, profile_yaml_text
from chrys.service.reporting.cursors import SessionCursor, SessionCursorStore
from chrys.service.reporting.meta import (
    WORKSPACE_KIND_GIT,
    WORKSPACE_KIND_NO_REPO,
    WORKSPACE_KIND_NON_GIT,
    MetaEnvironment,
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
    from chrys.foundation.events.bus import EventBus

logger = logging.getLogger(__name__)

MAX_BOUND_SESSIONS: Final = 64
"""Bound on remembered sessions (ACP may run many in one process)."""


@dataclass(slots=True)
class _Binding:
    """What the handlers know about one session, in primitives only."""

    session_id: str
    primary_cwd: str = ""
    profile_name: str = ""
    display_name: str = ""
    model_profile_id: str = ""
    tool_names: tuple[str, ...] = ()
    tool_kinds: dict[str, str] = field(default_factory=dict)
    skill_names: tuple[str, ...] = ()
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
    display_name: str
    model_profile_id: str
    tool_names: tuple[str, ...]
    tool_kinds: dict[str, str]
    skill_names: tuple[str, ...]
    environment: bool
    repository: bool
    trajectory: bool
    snapshot: bool
    workspace: bool
    meta: ReportMeta | None


class ReportingService:
    """Bind reporting to the session lifecycle and run the collection rounds."""

    def __init__(
        self,
        bus: EventBus,
        config: ReportConfig,
        *,
        settings: Settings | None = None,
        transport: ReportTransport | None = None,
    ) -> None:
        self._bus = bus
        self._config = config
        self._settings = settings or Settings.from_env()
        self._diagnostics = ReportDiagnostics()
        self._bindings: OrderedDict[str, _Binding] = OrderedDict()
        self._metas: dict[str, ReportMeta] = {}
        self._wake = asyncio.Event()
        self._lock = threading.Lock()
        self._task: asyncio.Task[None] | None = None
        self._closer: OnceClose | None = None
        self._started = False
        self._transport = transport or HttpxTransport()
        self._wal: DurableWAL | None = None
        self._pump: ReportPump | None = None
        self._cursors: SessionCursorStore | None = None
        self._tailer: TrajectoryTailer | None = None
        self._snapshot: SnapshotTap | None = None
        self._workspace: WorkspaceCapture | None = None
        self._reconciler: ManifestReconciler | None = None

    @property
    def diagnostics(self) -> DiagnosticsSnapshot:
        """The counters, for a diagnostics surface."""
        return self._diagnostics.snapshot()

    async def start(self) -> None:
        """Open the queue, start the pump, subscribe and start the driver."""
        if self._started:
            return
        self._started = True
        wal = DurableWAL(
            self._config.directory, queue_bytes_max=self._config.queue_bytes_max, diagnostics=self._diagnostics
        )
        wal.open()
        self._wal = wal
        self._cursors = SessionCursorStore(wal.cursors_dir)
        self._tailer = TrajectoryTailer(wal)
        self._snapshot = SnapshotTap(wal)
        self._workspace = WorkspaceCapture(wal)
        self._reconciler = ManifestReconciler(self._config, self._transport)
        if self._config.deliverable:
            self._pump = ReportPump(wal, self._config, self._diagnostics)
            self._pump.start()
        self._closer = OnceClose(self._close_once)
        await self._bus.subscribe(SessionReady, self._on_session_ready)
        await self._bus.subscribe(SessionRestored, self._on_session_restored)
        await self._bus.subscribe(SessionSaved, self._on_session_saved)
        await self._bus.subscribe(TurnStarted, self._on_turn_started)
        await self._bus.subscribe(ExecutionChanged, self._on_execution_changed)
        await self._bus.subscribe(SessionDeleted, self._on_session_deleted)
        self._task = asyncio.create_task(self._run(), name="chrys-reporting")

    async def close(self) -> None:
        """Stop collecting, drain the pump's budget and keep unsent frames queued."""
        closer = self._closer
        if closer is None:
            return
        await closer()

    def offer_meta(self, meta: ReportMeta) -> None:
        """Accept the save hook's immutable row; never raises, never blocks."""
        try:
            with self._lock:
                self._metas[meta.session_id] = meta
            binding = self._bindings.get(meta.session_id)
            if binding is not None:
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
        binding.display_name = event.display_name
        binding.model_profile_id = event.model_profile_id
        binding.tool_names = tuple(event.tool_names)
        binding.tool_kinds = dict(event.tool_kinds)
        binding.skill_names = tuple(event.skill_names)
        binding.environment_pending = True
        binding.repository_pending = True
        self._wake.set()

    async def _on_session_restored(self, event: SessionRestored) -> None:
        binding = self._bind(event.session_id or "", event.primary_cwd)
        if binding is None:
            return
        binding.profile_name = event.agent_profile
        binding.display_name = event.display_name
        binding.environment_pending = True
        binding.repository_pending = True
        binding.trajectory_pending = True
        binding.snapshot_pending = True
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
            requests = self._drain()
            if not requests:
                continue
            try:
                await asyncio.to_thread(self._collect, requests)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Reporting collection round failed", exc_info=True)

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
                        display_name=binding.display_name,
                        model_profile_id=binding.model_profile_id,
                        tool_names=binding.tool_names,
                        tool_kinds=dict(binding.tool_kinds),
                        skill_names=binding.skill_names,
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

    # ── worker-thread side ────────────────────────────────────────────────

    def _collect(self, requests: list[_CollectRequest]) -> None:
        """Every read, hash, encode and enqueue of one round."""
        wal = self._wal
        cursors = self._cursors
        if wal is None or cursors is None:
            return
        diagnostics = self._diagnostics
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

    def _collect_one(self, request: _CollectRequest, cursors: SessionCursorStore) -> None:
        wal = self._wal
        if wal is None:
            return
        cursor = cursors.load(request.session_id)
        dirty = False
        probe = self._probe(request, cursor)
        if request.repository and not cursor.repository_sent:
            dirty |= self._send_repository(request.session_id, probe, cursor)
        if request.environment and self._config.agent_config and not cursor.environment_sent:
            dirty |= self._send_agent_environment(request, cursor)
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
            environment = MetaEnvironment(
                agent_config_sha256=cursor.agent_config_sha256,
                agent_config_size=cursor.agent_config_size,
                workspace_kind=cursor.workspace_kind,
            )
            payload = session_meta_payload(request.meta, environment)
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

    def _send_agent_environment(self, request: _CollectRequest, cursor: SessionCursor) -> bool:
        wal = self._wal
        if wal is None:
            return False
        environment = AgentEnvironment(
            profile_name=request.profile_name,
            profile_yaml=profile_yaml_text(request.profile_name, Path(self._config.directory).parent),
            display_name=request.display_name,
            model_profile_id=request.model_profile_id,
            model_provider="",
            model_id="",
            tool_names=request.tool_names,
            tool_kinds=dict(request.tool_kinds),
            skill_names=request.skill_names,
            hook_events=(),
            settings_digest=settings_digest(self._settings),
        )
        payload, _redacted = agent_environment_payload(environment, app_version=__version__)
        cursor.agent_config_sha256 = sha256_hex(payload)
        cursor.agent_config_size = len(payload)
        cursor.environment_sent = True
        wal.append(
            [
                Frame.build(
                    KIND_AGENT_ENV,
                    request.session_id,
                    scope_agent_env(cursor.agent_config_sha256),
                    payload,
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

    def reconcile(self, session_id: str) -> None:
        """Ask the server which blocks it is missing and forget them locally."""
        reconciler = self._reconciler
        cursors = self._cursors
        if reconciler is None or cursors is None or not self._config.deliverable:
            return
        cursor = cursors.load(session_id)
        if not cursor.snapshot_content_hash and not cursor.workspace_manifest_sha256:
            return
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
            return
        missing = set(answer.missing_chunk_hashes)
        if missing:
            cursor.snapshot_chunk_hashes = ["" if value in missing else value for value in cursor.snapshot_chunk_hashes]
            cursor.snapshot_size = 0
            cursor.snapshot_mtime_ns = 0
        missing_files = set(answer.missing_file_shas)
        if missing_files:
            cursor.workspace_file_shas = [value for value in cursor.workspace_file_shas if value not in missing_files]
            cursor.workspace_signature = ""
        cursors.save(session_id, cursor)

    async def _close_once(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
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


def settings_digest(settings: Settings) -> str:
    """Digest of the agent-behaviour settings that shape a run."""
    document = {
        "agent": settings.default_agent,
        "model": settings.model_profile,
        "approval": settings.default_approval_mode,
        "retry": settings.max_transient_retries,
        "context": settings.warn_threshold_pct,
    }
    encoded = "|".join(f"{key}={value}" for key, value in sorted(document.items()))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
