# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One live session's trajectory recorder.

:class:`SessionTrajectory` is the engine-owned :class:`TrajectorySink`. It is
created cheaply whenever a session becomes current and activates itself on
the first event: ensure the owner-only ``<session>/trajectory/`` directory,
take the per-session writer lease, recover a torn tail under the session
write lock, then start the single writer and record the coverage prelude
(``coverage.started`` / ``runtime.started`` / ``runtime.recovered`` /
``session.started``). Activation failures never fail the session — the
recorder simply stays disabled and every ``emit`` answers ``DEGRADED``.

Lazy activation keeps the session directory unmaterialized for sessions that
never produce an event, which is what the empty-session cleanup relies on;
the engine closes the recorder *before* that cleanup runs, so a writer never
outlives its directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from chrys.foundation.platform.files import SecureFileError, secure_open_owner_only_append
from chrys.foundation.trajectory.context import TrajectoryContext, main_actor, side_call_actor, sub_agent_actor
from chrys.foundation.trajectory.envelope import (
    INT64_MAX,
    SYSTEM_ACTOR,
    Actor,
    EventDraft,
    monotonic_now_ns,
    utc_now_rfc3339,
)
from chrys.foundation.trajectory.event_types import CoverageReason, EventType
from chrys.foundation.trajectory.finalizers import RecordingFinalizers
from chrys.foundation.trajectory.fingerprint import DOMAIN_WORKSPACE, fingerprint_text
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory.keys import ensure_owner_only_directory, load_or_create_fingerprint_key
from chrys.foundation.trajectory.lease import WRITER_LEASE_FILE_NAME, WriterLease
from chrys.foundation.trajectory.recovery import (
    RecoveryScan,
    scan_for_recovery,
    scan_open_file,
    truncate_torn_tail,
)
from chrys.foundation.trajectory.revisions import RevisionRegistry
from chrys.foundation.trajectory.writer import (
    DEFAULT_CLOSE_JOIN_TIMEOUT_SECONDS,
    DEFAULT_WRITE_ACK_TIMEOUT_SECONDS,
    EmitResult,
    FdWriteBackend,
    TrajectoryWriter,
)
from chrys.foundation.util.lock import FileLock

logger = logging.getLogger(__name__)

TRAJECTORY_DIR_NAME: Final = "trajectory"
TRAJECTORY_EVENTS_FILE_NAME: Final = "events.jsonl"
ACTIVATION_LOCK_TIMEOUT_SECONDS: Final = 10.0
MAX_ACTIVATION_ATTEMPTS: Final = 2


class TrajectoryDisabledReason:
    """Why a session records nothing; never persisted.

    Every reason but ``NO_SESSION_DIR`` (nothing to record into) and
    ``CLOSED`` is reported once to ``on_activation_failed``, which the engine
    turns into a warning carrying the reason.
    """

    NO_SESSION_DIR = "no_session_dir"
    LEASE_HELD = "lease_held"
    KEY_UNAVAILABLE = "key_unavailable"
    PERMISSION = "permission"
    """The session's files refused this user: owner, mode, ACL, a link, or a read-only folder."""
    ACTIVATION_FAILED = "activation_failed"
    SCHEMA_TOO_NEW = "schema_too_new"
    UNREADABLE_TAIL = "unreadable_tail"
    SEQUENCE_EXHAUSTED = "sequence_exhausted"
    CLOSED = "closed"


_UNREPORTED_REASONS: Final = frozenset({TrajectoryDisabledReason.NO_SESSION_DIR, TrajectoryDisabledReason.CLOSED})
_PERMISSION_ERRNOS: Final = frozenset({errno.EACCES, errno.EPERM, errno.EROFS, errno.ELOOP})
# ERROR_ACCESS_DENIED, ERROR_WRITE_PROTECT, ERROR_NETWORK_ACCESS_DENIED.
_WINDOWS_PERMISSION_ERRORS: Final = frozenset({5, 19, 65})


def _activation_failure_reason(exc: Exception) -> str:
    """``PERMISSION`` when the session's files refused this user, else ``ACTIVATION_FAILED``."""
    if isinstance(exc, PermissionError):
        return TrajectoryDisabledReason.PERMISSION
    if isinstance(exc, SecureFileError):
        from chrys.foundation.platform import get_platform

        if exc.errno is None:
            # Raised by the owner-only checks themselves: the wrong owner, group
            # or other mode bits, an ACL, a link in the path.
            return TrajectoryDisabledReason.PERMISSION
        if get_platform().is_windows:
            # A Win32 error code, which must not be read as an errno.
            return (
                TrajectoryDisabledReason.PERMISSION
                if exc.errno in _WINDOWS_PERMISSION_ERRORS
                else TrajectoryDisabledReason.ACTIVATION_FAILED
            )
    if isinstance(exc, OSError) and exc.errno in _PERMISSION_ERRNOS:
        return TrajectoryDisabledReason.PERMISSION
    return TrajectoryDisabledReason.ACTIVATION_FAILED


@dataclass(frozen=True, slots=True)
class SessionStartInfo:
    """Inputs of ``session.started``; the recorder adds environment and fingerprints."""

    primary_cwd: str
    agent_profile_fingerprint: str
    model_profile_fingerprint: str


def trajectory_dir(session_dir: Path) -> Path:
    return session_dir / TRAJECTORY_DIR_NAME


def trajectory_events_path(session_dir: Path) -> Path:
    return trajectory_dir(session_dir) / TRAJECTORY_EVENTS_FILE_NAME


def trajectory_lease_path(session_dir: Path) -> Path:
    return trajectory_dir(session_dir) / WRITER_LEASE_FILE_NAME


def runtime_environment() -> dict[str, str]:
    """``app_version`` / ``os_name`` / ``arch`` as stamped on runtime and session starts."""
    from chrys import __version__
    from chrys.foundation.platform import get_platform

    platform = get_platform()
    return {"app_version": __version__, "os_name": platform.os_name, "arch": platform.arch}


def _session_has_persisted_state(session_dir: Path) -> bool:
    from chrys.service.state.store import session_dir_has_artifacts

    return session_dir_has_artifacts(session_dir)


@dataclass(frozen=True, slots=True)
class _Activation:
    """The writer and the verdict on the activation that made it, as one answer.

    Both are written by an activation running on another thread, so reading
    them one at a time can mix two attempts: a producer that read
    ``disabled_reason`` before a failed activation and ``writer`` after it
    would write into a log whose runtime was never announced, and one that
    read ``writer`` before a successful activation would give up on a session
    that is recording. They are replaced together and read together.
    """

    writer: TrajectoryWriter | None = None
    disabled_reason: str | None = None


class SessionTrajectory:
    """Engine-owned recorder for one session (see module docstring)."""

    def __init__(
        self,
        *,
        session_id: str,
        session_dir: Path | None,
        write_lock_path: Path | None = None,
        config_dir: Path | None = None,
        session_start_info: Callable[[], SessionStartInfo | None] | None = None,
        write_ack_timeout: float = DEFAULT_WRITE_ACK_TIMEOUT_SECONDS,
        close_join_timeout: float = DEFAULT_CLOSE_JOIN_TIMEOUT_SECONDS,
        persisted_state_probe: Callable[[Path], bool] = _session_has_persisted_state,
        on_activation_failed: Callable[[str], None] | None = None,
    ) -> None:
        self._session_id = session_id
        self._session_dir = session_dir
        self._write_lock_path = write_lock_path
        self._config_dir = config_dir
        self._session_start_info = session_start_info
        self._write_ack_timeout = write_ack_timeout
        self._close_join_timeout = close_join_timeout
        self._persisted_state_probe = persisted_state_probe
        self._on_activation_failed = on_activation_failed

        self._main_actor = main_actor(session_id)
        # The prelude is written when the log opens, and the log opens on the
        # first event the session records — which can be long after the
        # runtime took this session. Stamped here, ``runtime.started`` says
        # when the runtime started rather than when it first had something to
        # say, and an idle stretch before the first turn stays visible.
        self._bound_at = utc_now_rfc3339()
        self._bound_monotonic_ns = monotonic_now_ns()
        # Context-revision chains live for the whole runtime so every run's
        # exchanges extend the same per-actor lineage (delta, not checkpoint).
        self._revisions = RevisionRegistry()
        self._finalizers = RecordingFinalizers()
        self._activation_lock = threading.Lock()
        self._activation = _Activation()
        self._activation_attempts = 0
        self._disabled_reported = False
        # A writer whose prelude failed belongs only to the thread holding
        # ``_activation_lock``. Publishing it through ``_activation`` would
        # expose a transient ACTIVATION_FAILED verdict to lock-free emitters,
        # which would then drop events even if the retry succeeds.
        self._failed_activation_writer: TrajectoryWriter | None = None
        self._lease: WriterLease | None = None
        self._key: bytes | None = None
        self._branch_id: str | None = None
        self._coverage_reason: str | None = None
        # Tail repair changes the file before a prelude can fail. A retry's
        # rescan sees the repaired file, so keep the removed-byte evidence
        # independently of the current scan until a runtime can report it.
        self._recovered_truncated_bytes = 0
        self._closed = False
        self._worker_stuck = False
        self._recovery: RecoveryScan | None = None
        self._background_waits: set[asyncio.Task[Any]] = set()
        self._close_task: asyncio.Task[bool] | None = None

    # ----------------------------------------------------------------- props

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def session_dir(self) -> Path | None:
        return self._session_dir

    @property
    def main_actor(self) -> Actor:
        return self._main_actor

    @property
    def is_active(self) -> bool:
        # A terminal cleanup failure can still leave its writer published so
        # ``close`` can reach the worker; that runtime is not recording.
        state = self._activation
        return state.writer is not None and state.disabled_reason is None and not self._closed

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def disabled_reason(self) -> str | None:
        return self._activation.disabled_reason

    @property
    def runtime_id(self) -> str | None:
        writer = self._activation.writer
        return writer.runtime_id if writer is not None else None

    @property
    def coverage_reason(self) -> str | None:
        return self._coverage_reason

    @property
    def branch_id(self) -> str | None:
        return self._branch_id

    @property
    def fingerprint_key(self) -> bytes | None:
        return self._key

    @property
    def worker_stuck(self) -> bool:
        """Whether the last close abandoned a worker that still holds the lease."""
        return self._worker_stuck

    @property
    def writer(self) -> TrajectoryWriter | None:
        return self._activation.writer

    def side_call_actor(self, role: str) -> Actor:
        return side_call_actor(self._session_id, role)

    def main_actor_draft(
        self,
        event_type: str,
        *,
        payload: Mapping[str, Any] | None = None,
        turn_id: str | None = None,
        operation_id: str | None = None,
        parent_operation_id: str | None = None,
    ) -> EventDraft:
        """Build a draft attributed to the session's main agent."""
        return EventDraft(
            event_type=event_type,
            actor=self._main_actor,
            payload=payload if payload is not None else {},
            turn_id=turn_id,
            operation_id=operation_id,
            parent_operation_id=parent_operation_id,
        )

    def context(self, *, turn_id: str | None = None, run_operation_id: str | None = None) -> TrajectoryContext:
        """An ambient :class:`TrajectoryContext` for the main agent of this session."""
        return TrajectoryContext(
            sink=self,
            session_id=self._session_id,
            actor=self._main_actor,
            turn_id=turn_id,
            run_operation_id=run_operation_id,
            revisions=self._revisions,
            finalizers=self._finalizers,
        )

    def sub_agent_actor(self, invocation_id: str) -> Actor:
        return sub_agent_actor(self._session_id, invocation_id)

    # ------------------------------------------------------------------ emit

    async def emit(
        self, draft: EventDraft, *, payload_factory: Callable[[int], Mapping[str, Any]] | None = None
    ) -> EmitResult:
        state = self._activation
        if state.disabled_reason is not None:
            return EmitResult.DEGRADED
        if state.writer is not None:
            return await state.writer.emit(draft, payload_factory=payload_factory)
        if self._closed:
            return EmitResult.DEGRADED
        # Opening the log and committing this draft ride the same thread
        # call, so a producer cancelled while the log is still opening
        # keeps the line an active writer would already have committed
        # for it — otherwise the event that triggered activation is the
        # one event activation loses, and nothing accounts for it.
        return await asyncio.to_thread(self.emit_blocking, draft, payload_factory=payload_factory)

    def emit_blocking(
        self, draft: EventDraft, *, payload_factory: Callable[[int], Mapping[str, Any]] | None = None
    ) -> EmitResult:
        state = self._activation
        if state.writer is None and state.disabled_reason is None and not self._closed:
            # A no-op when another thread got there first; it holds the
            # activation lock either way, so the read below is of a settled
            # state and never of an activation halfway through.
            self._activate()
            state = self._activation
        if state.writer is None or state.disabled_reason is not None:
            # Failed activation cleanup can leave a terminal writer behind:
            # the runtime it belongs to was never announced.
            return EmitResult.DEGRADED
        return state.writer.emit_blocking(draft, payload_factory=payload_factory)

    def emit_soon(
        self, draft: EventDraft, *, payload_factory: Callable[[int], Mapping[str, Any]] | None = None
    ) -> EmitResult | None:
        """Queue *draft* in sequence order from a synchronous call site on the event loop.

        With an active writer the sequence is taken right here (so ordering
        against later ``emit`` calls holds) and the bounded ack wait runs in
        a background task. Before activation the whole ``emit`` is scheduled
        instead — activation must not block the loop — which only affects
        events that precede the recorder's first awaited event.

        ``None`` says the line took its sequence; a result says the sink
        refused it outright and no line will ever carry it.
        """
        state = self._activation
        if state.disabled_reason is not None:
            return EmitResult.DEGRADED
        writer = state.writer
        if writer is not None:
            submitted = writer.submit(draft, payload_factory=payload_factory)
            if isinstance(submitted.result, EmitResult):
                return submitted.result
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return None
            task = loop.create_task(writer.wait(submitted))
            self._background_waits.add(task)
            task.add_done_callback(self._background_waits.discard)
            return None
        if self._closed:
            return EmitResult.DEGRADED
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return self.emit_blocking(draft, payload_factory=payload_factory)
        task = loop.create_task(self.emit(draft, payload_factory=payload_factory))
        self._background_waits.add(task)
        task.add_done_callback(self._background_waits.discard)
        return None

    async def ensure_active(self) -> bool:
        """Open the log now if it has not been opened yet; return whether it is recording.

        Callers that must know the live branch — or place an event against a
        known sequence — cannot wait for the first emit to activate lazily.
        """
        if self._needs_activation():
            await asyncio.to_thread(self._activate)
        return self.is_active

    async def checkpoint(self) -> EmitResult:
        state = self._activation
        if state.writer is None or not self.is_active:
            return EmitResult.DEGRADED
        return await state.writer.checkpoint()

    def set_branch_id(self, branch_id: str) -> None:
        self._branch_id = branch_id
        writer = self._activation.writer
        if writer is not None:
            writer.set_branch_id(branch_id)

    def pin_session_start_info(self) -> None:
        """Resolve what ``session.started`` reports now, and keep that answer.

        Resolution is deferred to the first event because the fingerprints are
        not known until the build finishes — but the build that finished is the
        one the session started with, and reading its source again later would
        let a profile switch rewrite the value the session opened with.
        """
        resolve = self._session_start_info
        if resolve is None:
            return
        info = resolve()
        self._session_start_info = lambda: info

    def last_assigned_sequence(self) -> int:
        """Highest sequence this session's log has handed out (0 when none)."""
        writer = self._activation.writer
        if writer is not None:
            return writer.last_assigned_sequence
        if self._recovery is not None:
            return self._recovery.last_sequence
        if self._session_dir is None:
            return 0
        path = trajectory_events_path(self._session_dir)
        if not path.is_file():
            return 0
        try:
            return scan_for_recovery(path).last_sequence
        except OSError:
            return 0

    async def close(self, *, reason: str) -> bool:
        """Close the runtime (see :meth:`TrajectoryWriter.close`); idempotent."""
        with self._activation_lock:
            if self._close_task is not None:
                task = self._close_task
            else:
                if self._closed:
                    return True
                self._closed = True
                state = self._activation
                writer = state.writer
                if writer is None:
                    self._finalizers.close()
                    self._activation = replace(
                        state, disabled_reason=state.disabled_reason or TrajectoryDisabledReason.CLOSED
                    )
                    return True
                task = asyncio.ensure_future(self._close_writer(writer, reason=reason))
                self._close_task = task
        # The close is what hands the descriptor and this session's writer
        # lease back, and ``_closed`` is already set, so it can never be
        # abandoned half-done: cancellation detaches this caller, and every
        # later caller waits on the same close rather than being told the
        # runtime is already closed.
        return await asyncio.shield(task)

    async def _close_writer(self, writer: TrajectoryWriter, *, reason: str) -> bool:
        # Producers can retire while this recorder stays open. Preserve their
        # real results until here, then settle anything still being observed
        # before freezing the writer (including starts whose ack is pending).
        self._finalizers.close()
        if self._background_waits:
            # ``emit_soon`` acks still in flight: their lines are already
            # queued ahead of the close marker, so only the waits are gathered.
            await asyncio.gather(*tuple(self._background_waits), return_exceptions=True)
        closed = False
        try:
            closed = await writer.close(reason=reason, join_timeout=self._close_join_timeout)
        finally:
            if writer.thread_alive:
                # The lease is bound to the worker: a stuck thread keeps it so
                # no second writer can ever open this session's file. That
                # thread hands it back itself if its write ever returns.
                self._worker_stuck = True
        return closed

    def _release_lease(self) -> None:
        lease, self._lease = self._lease, None
        if lease is not None:
            lease.release()

    # ------------------------------------------------------------ activation

    def _needs_activation(self) -> bool:
        state = self._activation
        return state.writer is None and state.disabled_reason is None and not self._closed

    def _activate(self) -> None:
        with self._activation_lock:
            while self._needs_activation() and self._activation_attempts < MAX_ACTIVATION_ATTEMPTS:
                self._activation_attempts += 1
                try:
                    self._activate_locked()
                except Exception as exc:
                    logger.warning("Trajectory activation failed for session %s", self._session_id, exc_info=True)
                    if self._reset_failed_activation() and self._activation_attempts < MAX_ACTIVATION_ATTEMPTS:
                        continue
                    self._disable(_activation_failure_reason(exc))
                except BaseException:
                    # Preserve cancellation/interrupt propagation, but do not
                    # strand the private failed writer and its file lease.
                    self._reset_failed_activation()
                    raise
                self._report_disabled()
                return

    def _reset_failed_activation(self) -> bool:
        """Release a failed attempt so the same first event can retry safely."""
        writer, self._failed_activation_writer = self._failed_activation_writer, None
        if writer is not None:
            if not writer.abort(join_timeout=self._close_join_timeout):
                # Cleanup did not finish, so retrying could put two writers on
                # one file. This failure really is terminal and may now be
                # observed by the lock-free emit paths.
                self._activation = _Activation(
                    writer=writer,
                    disabled_reason=TrajectoryDisabledReason.ACTIVATION_FAILED,
                )
                return False
        else:
            self._release_lease()
        self._activation = _Activation()
        self._coverage_reason = None
        self._recovery = None
        return True

    def _report_disabled(self) -> None:
        """Report, once, why this activation left the session recording nothing."""
        reason = self._activation.disabled_reason
        if (
            reason is None
            or reason in _UNREPORTED_REASONS
            or self._disabled_reported
            or self._on_activation_failed is None
        ):
            return
        self._disabled_reported = True
        try:
            self._on_activation_failed(reason)
        except Exception:
            logger.debug("Trajectory activation failure callback failed", exc_info=True)

    def _disable(self, reason: str) -> None:
        """Stop recording for *reason*; the emit paths refuse from here on.

        Failed activation cleanup can leave a writer whose worker did not
        exit. Its lease has to stay with it: a runtime that gave up still owns
        the file until its worker exits.
        """
        state = self._activation
        self._activation = replace(state, disabled_reason=reason)
        if state.writer is not None:
            # The lease belongs to the worker, and a started writer still has
            # the descriptor: handing it back here would let a second writer
            # open this same file and append beside the first. Whatever gave
            # up on this runtime, only the worker's exit ends its claim.
            return
        self._release_lease()

    def _activate_locked(self) -> None:
        session_dir = self._session_dir
        if session_dir is None:
            self._disable(TrajectoryDisabledReason.NO_SESSION_DIR)
            return
        directory = trajectory_dir(session_dir)
        ensure_owner_only_directory(directory)
        lease = WriterLease.try_acquire(trajectory_lease_path(session_dir))
        if lease is None:
            logger.info(
                "Trajectory writer lease for session %s is held elsewhere; recording disabled", self._session_id
            )
            self._disable(TrajectoryDisabledReason.LEASE_HELD)
            return
        self._lease = lease
        try:
            self._key = self._load_key()
        except SecureFileError, OSError, TimeoutError:
            logger.warning("Trajectory fingerprint key unavailable; recording disabled", exc_info=True)
            self._disable(TrajectoryDisabledReason.KEY_UNAVAILABLE)
            return

        events_path = trajectory_events_path(session_dir)
        handle = secure_open_owner_only_append(events_path)
        fd = handle.fd
        try:
            # Whether there is anything to recover is the open's own answer.
            # A probe taken before it can be stale by the time it is used —
            # or simply unable to stat — and a log wrongly called absent has
            # its sequences handed out a second time.
            scan = None if handle.created else self._recover(fd)
        except BaseException:
            os.close(fd)
            raise
        self._recovery = scan
        if scan is not None:
            self._recovered_truncated_bytes += scan.truncated_bytes
        if scan is not None and scan.newer_schema_version is not None:
            # Another build wrote lines this one cannot read, so it cannot
            # know which slots they took. Reading such a log wrongly is a
            # diagnostic; writing into it would put two events on one slot and
            # damage the file for every later reader, so this session records
            # nothing instead.
            logger.warning(
                "Trajectory for session %s was written under schema %d; recording disabled",
                self._session_id,
                scan.newer_schema_version,
            )
            os.close(fd)
            self._disable(TrajectoryDisabledReason.SCHEMA_TOO_NEW)
            return
        if scan is not None and scan.unreadable_tail:
            # A complete line past the last readable event does not even name
            # the slot it took, so the next free sequence is exactly what
            # cannot be established. Resuming would be a guess, and a wrong
            # guess puts two events on one slot; this session records nothing
            # instead, and the damaged file is left exactly as it is.
            logger.warning(
                "Trajectory for session %s ends in a line that names no sequence; recording disabled",
                self._session_id,
            )
            os.close(fd)
            self._disable(TrajectoryDisabledReason.UNREADABLE_TAIL)
            return
        if scan is not None and scan.last_sequence >= INT64_MAX:
            # Every slot this build can name is spent. The next sequence is
            # one no line may carry, and the writer refuses events that would
            # — but a gap is encoded past that refusal, so a writer opened
            # here would spend the file on lines nothing can read back.
            logger.warning(
                "Trajectory for session %s has spent every sequence it can encode; recording disabled",
                self._session_id,
            )
            os.close(fd)
            self._disable(TrajectoryDisabledReason.SEQUENCE_EXHAUSTED)
            return
        truncated_bytes = self._recovered_truncated_bytes
        last_event = scan.last_event if scan is not None else None
        resumed = last_event is not None
        if last_event is not None:
            # Recovery only supplies a branch when nobody has chosen one: a
            # rollback that opened a branch before the log was activated is
            # already on the new branch, and must not be pulled back onto the
            # one it superseded.
            if self._branch_id is None:
                self._branch_id = last_event.branch_id
            self._coverage_reason = CoverageReason.RUNTIME_RESUMED
        else:
            if self._branch_id is None:
                self._branch_id = new_analytics_id()
            self._coverage_reason = (
                CoverageReason.FEATURE_INTRODUCED
                if self._persisted_state_probe(session_dir)
                else CoverageReason.SESSION_STARTED
            )
        writer = TrajectoryWriter(
            backend=FdWriteBackend(fd),
            session_id=self._session_id,
            runtime_id=new_analytics_id(),
            coverage_id=new_analytics_id(),
            branch_id=self._branch_id,
            initial_sequence=scan.last_sequence if scan is not None else 0,
            initial_offset=scan.complete_offset if scan is not None else 0,
            # Slots this file spent on lines no reader can show. They are a
            # hole in the sequence the moment this runtime appends past them,
            # so the gap that explains them goes in ahead of the prelude.
            recovered_gap=scan.unreadable_slots if scan is not None else None,
            write_ack_timeout=self._write_ack_timeout,
            # The lease belongs to the worker, not to this object: it is
            # handed back when the worker's descriptor is, which for an
            # abandoned worker can be long after ``close`` returned.
            on_worker_exit=self._release_lease,
        )
        try:
            writer.start()
            self._emit_prelude(writer, scan=scan, truncated_bytes=truncated_bytes, resumed=resumed)
        except BaseException:
            # Keep a retryable failure private while the activation lock is
            # held. Concurrent producers continue to see an unactivated sink,
            # join this activation through ``_activate()``, and emit against
            # the successful retry instead of returning DEGRADED.
            self._failed_activation_writer = writer
            raise
        else:
            # Published last, and in one store. Every emit path that skips the
            # activation lock does so by finding a writer here, so a producer
            # that finds one before the prelude has been submitted takes a
            # sequence ahead of the events that say which runtime and coverage
            # it belongs to. Failed attempts are never published here.
            self._activation = _Activation(writer=writer)

    def _load_key(self) -> bytes:
        config_dir = self._config_dir
        if config_dir is None:
            from chrys.foundation.platform import get_platform

            config_dir = get_platform().config_dir
        return load_or_create_fingerprint_key(config_dir)

    def _recover(self, fd: int) -> RecoveryScan:
        """Scan and truncate the torn tail under the session write lock.

        Through the descriptor, never the name: the file this writer holds is
        the one it has to measure, and the two stop being the same file the
        moment anything replaces the path behind it.
        """
        lock = (
            FileLock(self._write_lock_path, timeout=ACTIVATION_LOCK_TIMEOUT_SECONDS)
            if self._write_lock_path is not None
            else contextlib.nullcontext()
        )
        with lock:
            scan = scan_open_file(fd)
            if scan.newer_schema_version is None and not scan.unreadable_tail and scan.last_sequence < INT64_MAX:
                # A file this build is not going to write to is a file it does
                # not touch, torn tail included: truncating is the writer's
                # repair, and there is no writer here.
                truncate_torn_tail(fd, scan)
            return scan

    def _prelude_draft(self, event_type: str, *, actor: Actor, payload: Mapping[str, Any]) -> EventDraft:
        """A prelude event, stamped with the moment this recorder was bound."""
        return EventDraft(
            event_type=event_type,
            actor=actor,
            payload=payload,
            occurred_at=self._bound_at,
            monotonic_ns=self._bound_monotonic_ns,
        )

    def _emit_prelude(
        self, writer: TrajectoryWriter, *, scan: RecoveryScan | None, truncated_bytes: int, resumed: bool
    ) -> None:
        environment = runtime_environment()
        self._require_prelude_event(
            writer,
            self._prelude_draft(
                EventType.COVERAGE_STARTED,
                actor=SYSTEM_ACTOR,
                payload={
                    "coverage_id": writer.coverage_id,
                    "runtime_id": writer.runtime_id,
                    "coverage_reason": self._coverage_reason,
                },
            ),
        )
        self._require_prelude_event(
            writer,
            self._prelude_draft(EventType.RUNTIME_STARTED, actor=SYSTEM_ACTOR, payload=environment),
        )
        previous_unclosed = (
            scan is not None
            and scan.last_event is not None
            and scan.last_event.event_type != EventType.RUNTIME_FINISHED
        )
        if truncated_bytes > 0 or previous_unclosed:
            self._require_prelude_event(
                writer,
                self._prelude_draft(
                    EventType.RUNTIME_RECOVERED,
                    actor=SYSTEM_ACTOR,
                    payload={
                        "truncated_bytes": truncated_bytes,
                        "resumed_from_sequence": scan.last_sequence if scan is not None else 0,
                    },
                ),
            )
        if not resumed and self._coverage_reason == CoverageReason.SESSION_STARTED:
            info = self._session_start_info() if self._session_start_info is not None else None
            payload: dict[str, Any] = dict(environment)
            if info is not None and self._key is not None:
                payload["workspace_fingerprint"] = fingerprint_text(self._key, DOMAIN_WORKSPACE, info.primary_cwd)
                payload["agent_profile_fingerprint"] = info.agent_profile_fingerprint
                payload["model_profile_fingerprint"] = info.model_profile_fingerprint
            self._require_prelude_event(
                writer, self._prelude_draft(EventType.SESSION_STARTED, actor=self._main_actor, payload=payload)
            )

    @staticmethod
    def _require_prelude_event(writer: TrajectoryWriter, draft: EventDraft) -> None:
        if writer.emit_blocking(draft) is not EmitResult.WRITTEN:
            raise OSError("trajectory prelude event was not written")
