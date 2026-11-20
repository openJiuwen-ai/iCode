# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""One run's write-once listing header, immutable specification and log-owned terminal.

Large values, diagnostics and output identities live outside the bounded lifecycle
log. Payload identities use node/activation/request keys because trajectory envelopes
reserve *_id keys for analytics identities.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from chrys.foundation.models.workdir_migration import BackupOriginal, ReportReadError, source_edit
from chrys.foundation.models.workflow_session import WorkflowModelSelection
from chrys.foundation.platform.files import (
    atomic_write_basename_budget,
    atomic_write_owner_only_bytes,
    read_owner_verified_bounded,
    require_writable_file,
    secure_open_owner_only_append,
)
from chrys.foundation.trajectory.envelope import EventDraft
from chrys.foundation.trajectory.event_types import KNOWN_EVENT_TYPES, RuntimeFinishReason
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory.keys import ensure_owner_only_directory
from chrys.foundation.trajectory.reader import TrajectoryReadResult, read_trajectory
from chrys.foundation.trajectory.writer import (
    DEFAULT_WRITE_ACK_TIMEOUT_SECONDS,
    EmitResult,
    FdWriteBackend,
    TrajectoryWriter,
    WriteBackend,
)
from chrys.foundation.util.filenames import bounded_safe_stem
from chrys.foundation.util.lock import FileLock
from chrys.service.state._session_files import session_active_lock_path
from chrys.service.workflows.graph import KIND_JOIN, KIND_PYTHON
from chrys.service.workflows.layout import (
    EVENTS_FILE,
    HEADER_FILE,
    INPUT_FILE,
    NODES_DIR,
    OUTPUT_INDEX_FILE,
    RUN_OUTPUT_FILE,
    SOURCE_FILE,
    SPEC_FILE,
    WORKFLOWS_DIR,
)
from chrys.service.workflows.protocol import LIMITS

logger = logging.getLogger(__name__)

NODE_RECORD_INPUT: Final = "input"
NODE_RECORD_OUTPUT: Final = "output"
NODE_RECORD_DIAGNOSTICS: Final = "diagnostics"
NODE_RECORD_SESSION: Final = "session"
NODE_RECORD_USAGE: Final = "usage"
MAX_HEADER_BYTES: Final = 64 * 1024
INPUT_EXCERPT_CHARS: Final = 2000
MAX_SPEC_BYTES: Final = 16 * 1024 * 1024
MAX_INPUT_BYTES: Final = 16 * 1024 * 1024
# Serialized ceilings include JSON wrappers and worst-case control-character escaping.
MAX_PYTHON_VALUE_BYTES: Final = 6 * LIMITS.max_frame_bytes + 4096
MAX_JOIN_VALUE_BYTES: Final = 6 * LIMITS.max_join_retained_bytes + 4096
MAX_AGENT_VALUE_BYTES: Final = 128 * 1024 * 1024
MAX_EMITS_BYTES: Final = 6 * LIMITS.max_emit_bytes_per_attempt + 128 * LIMITS.max_emits_per_attempt
MAX_OUTPUT_INDEX_BYTES: Final = 16 * 1024 * 1024
MAX_RUN_OUTPUT_BYTES: Final = 1024 * 1024
MAX_DIAGNOSTICS_BYTES: Final = 1024 * 1024
RUN_RECORD_WRITE_ACK_TIMEOUT_SECONDS: Final = DEFAULT_WRITE_ACK_TIMEOUT_SECONDS
"""Bound on one run-record write, read when a store opens without its own.

Unlike the session trajectory, whose writer degrades and the turn goes on, a run
record that misses its ack fails the run as storage-failed: the record is the
run's source of truth. A host whose disk stalls for longer under fsync pressure
(a loaded test runner) widens this one name instead of every ``open`` call.
"""
"""Room for the worker's bounded stdout and traceback, including JSON escaping."""
NODE_RECORD_EMITS: Final = "emits"
"""The per-attempt log of ``ctx.emit`` texts in full, one JSON line ``{ordinal, text}`` each."""
DATA_DROPPED_KEY: Final = "data_dropped_at_agent_boundary"
"""Set on an agent activation's input record when its value carried ``data`` the agent boundary drops."""


class RunRecord(StrEnum):
    """Event types the run log carries."""

    RUN_STARTED = "workflow.log.run.started"
    NODE_STATE = "workflow.log.node.state"
    NODE_OUTPUT = "workflow.log.node.output"
    NODE_ASK = "workflow.log.node.ask"
    NODE_ANSWER = "workflow.log.node.answer"
    LOOP_ITERATION = "workflow.log.loop.iteration"
    RUN_NOTICE = "workflow.log.run.notice"
    RETRY_KEY = "workflow.log.retry.key"
    RUN_FINISHED = "workflow.log.run.finished"


WORKFLOW_EVENT_TYPES: Final = frozenset(RunRecord)

RUN_KNOWN_EVENT_TYPES: Final = KNOWN_EVENT_TYPES | WORKFLOW_EVENT_TYPES


class WorkflowStorageFailed(RuntimeError):
    """The run record can no longer be trusted; the run must stop."""


@dataclass(frozen=True, slots=True)
class RunHeader:
    """Small, immutable listing metadata in ``run.json``."""

    run_id: str
    session_id: str
    workflow_id: str
    source_kind: str
    canonical_path: str
    title: str
    input_excerpt: str
    entry_digest: str
    manifest_digest: str
    schema_version: int
    spec_digest: str
    started_at: str = ""
    mode: str = ""
    model: WorkflowModelSelection | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "workflow_id": self.workflow_id,
            "source_kind": self.source_kind,
            "canonical_path": self.canonical_path,
            "title": self.title,
            "input_excerpt": self.input_excerpt[:INPUT_EXCERPT_CHARS],
            "entry_digest": self.entry_digest,
            "manifest_digest": self.manifest_digest,
            "schema_version": self.schema_version,
            "spec_digest": self.spec_digest,
            "started_at": self.started_at,
            "mode": self.mode,
            "model": asdict(self.model) if self.model is not None else None,
        }


@dataclass(frozen=True, slots=True)
class RunSpec:
    """The immutable definition and resolution snapshot loaded only for replay or details."""

    manifest: Mapping[str, Any]
    environment: Mapping[str, Any]
    resolved_nodes: Sequence[Mapping[str, Any]] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest": dict(self.manifest),
            "environment": dict(self.environment),
            "resolved_nodes": [dict(node) for node in self.resolved_nodes],
        }


BackendFactory = Callable[[int], WriteBackend]


@dataclass(frozen=True, slots=True)
class RunTerminal:
    """The validated terminal facts owned exclusively by the lifecycle log."""

    outcome: str
    last_seq: int
    reason: str = ""
    finished_at: str = ""


class WorkflowRunStore:
    """Writer side of one run directory; open with :meth:`open`."""

    def __init__(
        self, run_dir: Path, header: RunHeader, spec: RunSpec, input_text: str, writer: TrajectoryWriter
    ) -> None:
        self._run_dir = run_dir
        self._header = header
        self.spec = spec
        self.input_text = input_text
        self._writer = writer
        self._last_written = 0
        self._closing: asyncio.Task[bool] | None = None

    @classmethod
    def open(
        cls,
        run_dir: Path,
        *,
        header: RunHeader,
        spec: RunSpec,
        input_text: str,
        source: bytes,
        backend_factory: BackendFactory | None = None,
        write_ack_timeout: float | None = None,
    ) -> WorkflowRunStore:
        """Create the directory (owner-only), write header and source, and start the log writer.

        ``write_ack_timeout`` defaults to :data:`RUN_RECORD_WRITE_ACK_TIMEOUT_SECONDS` as it stands when the store opens.
        """
        try:
            ensure_owner_only_directory(run_dir)
            ensure_owner_only_directory(run_dir / NODES_DIR)
            atomic_write_owner_only_bytes(run_dir / SOURCE_FILE, source)
            _write_bounded_json(run_dir / SPEC_FILE, spec.to_dict(), MAX_SPEC_BYTES)
            encoded_input = input_text.encode("utf-8", errors="surrogatepass")
            if len(encoded_input) > MAX_INPUT_BYTES:
                raise ValueError("Workflow input exceeds the size limit.")
            atomic_write_owner_only_bytes(run_dir / INPUT_FILE, encoded_input)
            write_run_header(run_dir / HEADER_FILE, header.to_dict())
            handle = secure_open_owner_only_append(run_dir / EVENTS_FILE)
        except (OSError, ValueError, TypeError) as exc:
            raise WorkflowStorageFailed(f"Cannot create the run record under {run_dir}: {exc}") from exc
        make_backend: BackendFactory = backend_factory or FdWriteBackend
        writer = TrajectoryWriter(
            backend=make_backend(handle.fd),
            session_id=header.session_id,
            runtime_id=header.run_id,
            coverage_id=new_analytics_id(),
            branch_id=new_analytics_id(),
            write_ack_timeout=RUN_RECORD_WRITE_ACK_TIMEOUT_SECONDS if write_ack_timeout is None else write_ack_timeout,
            thread_name=f"chrys-workflow-run-{header.run_id[:8]}",
        )
        writer.start()
        return cls(run_dir, header, spec, input_text, writer)

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    @property
    def header(self) -> RunHeader:
        return self._header

    @property
    def last_written_seq(self) -> int:
        return self._last_written

    async def append(self, event_type: str, payload: Mapping[str, Any], *, durable: bool = False) -> int:
        """Append one lifecycle record; returns its sequence once it is in the file.

        ``durable`` follows the record with a checkpoint and fsync. Anything
        but WRITTEN raises :class:`WorkflowStorageFailed`.
        """
        if event_type not in WORKFLOW_EVENT_TYPES:
            raise ValueError(f"{event_type!r} is not a workflow event type.")
        if self._closing is not None:
            raise WorkflowStorageFailed("the run record is closed.")
        submitted = self._writer.submit(EventDraft(event_type=event_type, payload=payload))
        outcome = await self._writer.wait(submitted)
        result = submitted.result
        if outcome is not EmitResult.WRITTEN or isinstance(result, EmitResult):
            raise WorkflowStorageFailed(self._failure(f"{event_type} was not written"))
        self._last_written = result.sequence
        if durable and await self._writer.checkpoint() is not EmitResult.WRITTEN:
            raise WorkflowStorageFailed(self._failure(f"{event_type} could not be made durable"))
        return result.sequence

    def write_node_value(self, activation_id: str, attempt: int, kind: str, payload: Mapping[str, Any]) -> Path:
        """Persist a full value under ``nodes/``; returns the file written."""
        path = node_value_path(self._run_dir, activation_id, attempt, kind)
        try:
            atomic_write_owner_only_bytes(path, _json_bytes(dict(payload)), create_parents=False)
        except (OSError, TypeError, ValueError) as exc:
            raise WorkflowStorageFailed(f"Cannot write {path.name}: {exc}") from exc
        return path

    def append_node_emit(self, activation_id: str, attempt: int, ordinal: int, text: str) -> None:
        """Append one ``ctx.emit`` in full to the attempt's emit log under ``nodes/``."""
        path = node_emits_path(self._run_dir, activation_id, attempt)
        try:
            line = json.dumps({"ordinal": ordinal, "text": text}, ensure_ascii=False, sort_keys=True)
            payload = (line + "\n").encode("utf-8", errors="backslashreplace")
            handle = secure_open_owner_only_append(path)
            try:
                while payload:
                    payload = payload[os.write(handle.fd, payload) :]
            finally:
                os.close(handle.fd)
        except (OSError, TypeError, ValueError) as exc:
            raise WorkflowStorageFailed(f"Cannot append to {path.name}: {exc}") from exc

    def write_node_diagnostics(
        self,
        activation_id: str,
        attempt: int,
        *,
        phase: str,
        iteration: int,
        stdout: str = "",
        truncated: bool = False,
        traceback: str = "",
    ) -> None:
        """Retain diagnostics by phase and iteration; these best-effort records never decide the outcome."""
        if not stdout and not truncated and not traceback:
            return
        try:
            record = read_node_diagnostics(self._run_dir, activation_id, attempt) or {"phases": []}
            phases = record.get("phases")
            if not isinstance(phases, list):
                raise ValueError("diagnostics phases must be a list")
            diagnostics = {
                "phase": phase,
                "iteration": iteration,
                "stdout": {"text": stdout, "truncated": truncated},
                "traceback": traceback,
            }
            phases.append(diagnostics)
            if len(_json_bytes(record)) > MAX_DIAGNOSTICS_BYTES:
                raise ValueError("diagnostics exceed the record size limit")
            self.write_node_value(activation_id, attempt, NODE_RECORD_DIAGNOSTICS, record)
        except OSError, ValueError, TypeError, WorkflowStorageFailed:
            logger.warning(
                "workflow diagnostics for %s attempt %s could not be written", activation_id, attempt, exc_info=True
            )

    async def finish(self, outcome: str, payload: Mapping[str, Any]) -> int:
        """Commit the only authoritative terminal; the header is never rewritten."""
        return await self.append(RunRecord.RUN_FINISHED, {"outcome": outcome, **payload}, durable=True)

    async def write_outputs(self, outputs: Sequence[Mapping[str, Any]]) -> None:
        await self.offload(self.write_output_index, outputs)

    async def offload(self, write: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Drain the thread before propagating cancellation or allowing terminal/close."""
        task = asyncio.create_task(asyncio.to_thread(write, *args, **kwargs))
        cancelled = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = exc
        task.result()
        if cancelled is not None:
            raise cancelled

    def write_output_index(self, outputs: Sequence[Mapping[str, Any]]) -> None:
        """Commit exact declared output identities before the terminal that references them."""
        try:
            _write_bounded_json(self._run_dir / OUTPUT_INDEX_FILE, {"outputs": list(outputs)}, MAX_OUTPUT_INDEX_BYTES)
        except (OSError, TypeError, ValueError) as exc:
            raise WorkflowStorageFailed(f"Cannot write output index: {exc}") from exc

    def write_run_output(self, payload: Mapping[str, Any]) -> None:
        """Best-effort bounded load/native diagnostics, outside the terminal record."""
        _write_bounded_json(self._run_dir / RUN_OUTPUT_FILE, payload, MAX_RUN_OUTPUT_BYTES)

    async def close(self) -> bool:
        """Close the log (runtime markers + fsync); idempotent. False when the runtime stays unclosed."""
        if self._closing is None:  # one close; every caller, concurrent or late, gets its verdict
            self._closing = asyncio.create_task(
                self._writer.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN), name="chrys.workflow.store.close"
            )
        return await asyncio.shield(self._closing)  # a cancelled waiter must not take the close down with it

    def _failure(self, what: str) -> str:
        reason = self._writer.snapshot().degraded_reason
        return f"{what} ({reason})" if reason else what


def node_value_path(run_dir: Path, activation_id: str, attempt: int, kind: str) -> Path:
    """The ``nodes/`` file for one (activation, attempt, kind); the same function names it for reads."""
    return _node_file(run_dir, activation_id, attempt, kind, ".json")


def node_emits_path(run_dir: Path, activation_id: str, attempt: int) -> Path:
    """The ``nodes/`` emit log of one attempt (see :data:`NODE_RECORD_EMITS`)."""
    return _node_file(run_dir, activation_id, attempt, NODE_RECORD_EMITS, ".jsonl")


def _node_file(run_dir: Path, activation_id: str, attempt: int, kind: str, suffix: str) -> Path:
    identity = f"{activation_id}.{attempt}.{kind}"
    # Sanitising can fold distinct ids together (``a:b`` / ``a_b``, case-insensitive
    # filesystems): a digest of the raw identity keeps every file distinct.
    digest = hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()[:8]
    stem = bounded_safe_stem(f"{identity}.{digest}", suffix=suffix, max_filename_bytes=atomic_write_basename_budget())
    return run_dir / NODES_DIR / f"{stem}{suffix}"


def read_node_value(
    run_dir: Path, activation_id: str, attempt: int, kind: str, *, node_kind: str = ""
) -> dict[str, Any] | None:
    """The stored record for one (activation, attempt, kind), or ``None`` when it was never written."""
    path = node_value_path(run_dir, activation_id, attempt, kind)
    return _optional_json(path, node_record_limit(kind, node_kind=node_kind))


def read_node_emits(run_dir: Path, activation_id: str, attempt: int) -> list[tuple[int, str]]:
    """Every ``(ordinal, text)`` the attempt emitted, in log order; a torn tail ends the readable prefix."""
    try:
        raw = read_owner_verified_bounded(node_emits_path(run_dir, activation_id, attempt), max_bytes=MAX_EMITS_BYTES)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return []
        raise
    emits: list[tuple[int, str]] = []
    for line in raw.split(b"\n"):
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            break
        if not isinstance(record, dict):
            break
        ordinal, text = record.get("ordinal"), record.get("text")
        if not isinstance(ordinal, int) or not isinstance(text, str):
            break
        emits.append((ordinal, text))
    return emits


def read_node_diagnostics(run_dir: Path, activation_id: str, attempt: int) -> dict[str, Any] | None:
    """Captured stdout (text and truncation flag) and traceback, when the attempt produced them."""
    path = node_value_path(run_dir, activation_id, attempt, NODE_RECORD_DIAGNOSTICS)
    return _optional_json(path, MAX_DIAGNOSTICS_BYTES)


def read_run_events(run_dir: Path) -> TrajectoryReadResult:
    """Decode ``events.jsonl`` with workflow event types counted as known."""
    return read_trajectory(run_dir / EVENTS_FILE, known_event_types=RUN_KNOWN_EVENT_TYPES)


def read_run_header(run_dir: Path) -> dict[str, Any]:
    header = read_json_object(run_dir / HEADER_FILE, MAX_HEADER_BYTES)
    source_edit(header)
    return header


def write_run_header(path: Path, payload: Mapping[str, Any], *, before_write: Callable[[], None] | None = None) -> None:
    """Write the bounded run header atomically."""
    encoded = _bounded_json_bytes(path, payload, MAX_HEADER_BYTES)
    if before_write is not None:
        before_write()
    atomic_write_owner_only_bytes(path, encoded, create_parents=False)


def migrate_run_headers(session_dir: Path, *, backup: BackupOriginal, on_read_error: ReportReadError) -> bool:
    """Rewrite listing headers under the active-session lock held during run creation."""

    def candidate(path: Path) -> tuple[bytes, dict[str, Any]] | None:
        try:
            raw, header = _read_json_object(path, MAX_HEADER_BYTES)
        except (OSError, ValueError, RecursionError) as error:
            if not isinstance(error, OSError) or error.errno not in {errno.ENOENT, errno.ENOTDIR}:
                on_read_error(path, error)
            return None
        return (raw, header) if source_edit(header) else None

    changed = False
    for path in (session_dir / WORKFLOWS_DIR).glob(f"*/{HEADER_FILE}"):
        if candidate(path) is None:
            continue
        try:
            lock = session_active_lock_path(session_dir.parent, session_dir.name)
            lock.parent.mkdir(parents=True, exist_ok=True)
            with FileLock(lock, timeout=0):
                record = candidate(path)
                if record is None:
                    continue
                raw, header = record

                def preserve(path: Path = path, raw: bytes = raw) -> None:
                    require_writable_file(path)
                    backup(path, raw)

                write_run_header(path, header, before_write=preserve)
                changed = True
        except OSError, ValueError, TypeError, RecursionError:
            continue
    return changed


def read_run_spec(run_dir: Path) -> dict[str, Any]:
    return read_json_object(run_dir / SPEC_FILE, MAX_SPEC_BYTES)


def read_run_input(run_dir: Path) -> str:
    return read_owner_verified_bounded(run_dir / INPUT_FILE, max_bytes=MAX_INPUT_BYTES).decode("utf-8", "surrogatepass")


def read_run_output(run_dir: Path) -> dict[str, Any] | None:
    return _optional_json(run_dir / RUN_OUTPUT_FILE, MAX_RUN_OUTPUT_BYTES)


def node_record_limit(kind: str, *, node_kind: str = "") -> int:
    """Record-kind limits admit joins and agent text independently of worker frame ceilings."""
    if kind == NODE_RECORD_DIAGNOSTICS:
        return MAX_DIAGNOSTICS_BYTES
    if kind == NODE_RECORD_USAGE:
        return 4096
    if kind in {NODE_RECORD_INPUT, NODE_RECORD_OUTPUT}:
        if node_kind == KIND_PYTHON:
            return MAX_PYTHON_VALUE_BYTES
        if node_kind == KIND_JOIN:
            return MAX_JOIN_VALUE_BYTES
    return MAX_AGENT_VALUE_BYTES


def read_json_object(path: Path, max_bytes: int) -> dict[str, Any]:
    return _read_json_object(path, max_bytes)[1]


def _read_json_object(path: Path, max_bytes: int) -> tuple[bytes, dict[str, Any]]:
    raw = read_owner_verified_bounded(path, max_bytes=max_bytes)
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise ValueError(f"{path.name} must be a JSON object.")
    return raw, record


def _optional_json(path: Path, max_bytes: int) -> dict[str, Any] | None:
    try:
        return read_json_object(path, max_bytes)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return None
        raise


def _write_bounded_json(path: Path, payload: Mapping[str, Any], max_bytes: int) -> None:
    atomic_write_owner_only_bytes(path, _bounded_json_bytes(path, payload, max_bytes), create_parents=False)


def _bounded_json_bytes(path: Path, payload: Mapping[str, Any], max_bytes: int) -> bytes:
    encoded = _json_bytes(payload)
    if len(encoded) > max_bytes:
        raise ValueError(f"{path.name} exceeds the {max_bytes}-byte ceiling")
    return encoded


def node_stderr_path(run_dir: Path, activation_id: str) -> Path:
    return _node_file(run_dir, activation_id, 0, "acp-stderr", ".log")


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    # As the trajectory writer encodes: a lone surrogate (a surrogateescaped path in the environment) becomes its
    # JSON escape instead of raising, and reads back as the same str because json.dumps escaped every real backslash.
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8", errors="backslashreplace")
