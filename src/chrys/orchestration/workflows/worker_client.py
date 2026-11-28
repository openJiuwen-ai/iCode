# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Owner of one workflow worker process and the RPC client that talks to it.

One reader task demultiplexes every frame the worker writes: responses go to
the future registered under their id, ``emit`` notifications go through an
ordered dispatcher, ``ask`` requests each get their own task so a human answer
never blocks the channel. Worker EOF, a malformed frame or the worker's exit
(noticed on its own, since a forked child of the worker can keep its pipes
open) rejects every pending future with :class:`WorkerLostError`, cancels the
helpers and terminates the process tree, in that order and only once.

Two contracts the runner relies on live here:

* **attempt fence** — once a ``run_python`` attempt has its terminal envelope,
  a later ``emit`` for it is dropped and a later ``ask`` is answered
  ``attempt_terminated``; a ``cancel`` names one exact attempt.
* **projection barrier** — ``run_python`` returns only after the emit
  dispatcher has processed every ordinal up to the envelope's
  ``last_emit_ordinal``; the cancel and worker-lost paths are exempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from chrys.foundation.models.ask_user import AskUserAnswer, AskUserQuestion
from chrys.foundation.platform import get_platform
from chrys.foundation.platform.process import (
    ManagedStdioProcess,
    MissingWorkingDirectoryError,
    process_group_alive,
    spawn_managed_stdio_process,
)
from chrys.foundation.trajectory.keys import ensure_owner_only_directory
from chrys.service.workflows import protocol
from chrys.service.workflows.asks import answers_to_wire, questions_from_wire
from chrys.service.workflows.environment import PreparedEnvironment
from chrys.service.workflows.graph import ManifestError, manifest_warnings
from chrys.service.workflows.protocol import (
    LIMITS,
    PROTOCOL_VERSION,
    ErrorCode,
    Method,
    ProtocolError,
    decode_frame,
    encode_frame,
    ref_from_wire,
    ref_key,
    ref_to_wire,
)
from chrys.service.workflows.scheduler import AttemptRef
from chrys.service.workflows.sdk import SourceValue, WorkflowValue
from chrys.service.workflows.sdk_artifact import SdkArtifact
from chrys.service.workflows.values import canonical_json, source_to_wire, value_from_wire, value_to_wire

logger = logging.getLogger(__name__)

HOST_PATH = Path(protocol.__file__).resolve().parent / "worker_host.py"
BYTECODE_CACHE_ENV = "PYTHONPYCACHEPREFIX"
STDERR_TAIL_BYTES = 64 * 1024
MAX_IN_FLIGHT_REQUESTS = 64
_STDIO_LIMIT = LIMITS.max_frame_bytes + 2


def _bounded_diagnostic(text: str) -> str:
    """Cap diagnostic text so its error frame always fits the wire limit; a dropped error reply hangs the worker."""
    limit = LIMITS.captured_output_bytes
    if len(text) <= limit:
        return text
    return text[:limit] + "... [" + str(len(text) - limit) + " chars dropped]"


_CONTROL_METHODS = frozenset({Method.CANCEL, Method.SHUTDOWN})
# Requests that run one attempt each; their terminal envelope carries the attempt's leak verdict.
_ATTEMPT_METHODS = frozenset({Method.RUN_PYTHON, Method.EVAL_OUTGOING, Method.EVAL_LOOP_UNTIL, Method.COMBINE})
_EVAL_METHODS = frozenset({Method.EVAL_OUTGOING, Method.EVAL_LOOP_UNTIL, Method.COMBINE})

AskHandler = Callable[[AttemptRef, tuple[AskUserQuestion, ...]], Awaitable[tuple[AskUserAnswer, ...]]]
EmitHandler = Callable[[AttemptRef, int, str], Awaitable[None]]


class WorkerError(Exception):
    """Base of every failure the client reports."""


class WorkerStartError(WorkerError):
    """The worker never became usable (probe, spawn, hello, SDK origin)."""


class WorkerLostError(WorkerError):
    """The worker exited, broke the protocol, or was killed; the run is over."""


class WorkerRpcError(WorkerError):
    """An ``error`` response; ``code`` is one of :class:`protocol.ErrorCode`."""

    def __init__(self, code: str, message: str, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data: dict[str, Any] = dict(data or {})
        self.stdout = _captured(self.data.get("stdout"))
        self.traceback = _string(self.data.get("traceback", ""), "traceback")


class AttemptTimeout(WorkerError):
    """A deadline passed; the attempt was cancelled (``leaked_thread`` says whether a thread stayed behind)."""

    def __init__(self, ref: AttemptRef, *, leaked_thread: bool, data: Mapping[str, Any]) -> None:
        super().__init__(f"{ref.activation_id} attempt {ref.attempt} timed out")
        self.ref = ref
        self.leaked_thread = leaked_thread
        self.stdout = _captured(data.get("stdout"))
        self.traceback = _string(data.get("traceback", ""), "traceback")


class AskUnavailable(Exception):
    """Raised by an ask handler when nobody can answer (headless)."""


@dataclass(frozen=True, slots=True)
class CapturedOutput:
    text: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class Hello:
    protocol_version: int
    python_version: str
    implementation: str
    platform: str
    sdk_origin_ok: bool
    sdk_origin_error: str | None


@dataclass(frozen=True, slots=True)
class LoadResult:
    entry_digest: str
    manifest_digest: str
    manifest: dict[str, Any]
    stdout: CapturedOutput
    sites: dict[str, tuple[str | None, int | None]] = field(default_factory=dict)
    """Where each node was declared, as ``(file, line)``; only a ``diagnose`` load reports them."""
    sites_truncated: bool = False
    """The sites were left out to keep the response within the frame limit."""


LOAD_TIMED_OUT: Final = "timeout"
"""``WorkerRpcError.data["reason"]`` of a load that did not finish within ``LIMITS.load_timeout``."""


@dataclass(frozen=True, slots=True)
class LoadNote:
    message: str
    file: str | None
    line: int | None


@dataclass(frozen=True, slots=True)
class LoadDiagnostic:
    """A load failure as the worker placed it; lines and character columns count from 1.

    ``end_column`` is the column just past the faulty range; ``node`` is the
    node or edge an SDK validation error names.
    """

    code: str
    message: str
    file: str | None
    line: int | None
    column: int | None
    end_line: int | None
    end_column: int | None
    node: str | None
    source_line: str | None
    notes: tuple[LoadNote, ...]
    hint: str | None


def load_diagnostics(data: Mapping[str, Any]) -> tuple[tuple[LoadDiagnostic, ...], bool]:
    """The diagnostics of a ``diagnose`` load's ``load_failed`` data, and whether any were left out."""
    raw = data.get("diagnostics", [])
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ProtocolError("diagnostics must be a list of objects.")
    diagnostics = tuple(
        LoadDiagnostic(
            code=_string(item.get("code"), "diagnostic code"),
            message=_string(item.get("message"), "diagnostic message"),
            file=_optional_string(item.get("file"), "diagnostic file"),
            line=_optional_count(item.get("line"), "diagnostic line"),
            column=_optional_count(item.get("column"), "diagnostic column"),
            end_line=_optional_count(item.get("end_line"), "diagnostic end_line"),
            end_column=_optional_count(item.get("end_column"), "diagnostic end_column"),
            node=_optional_string(item.get("node"), "diagnostic node"),
            source_line=_optional_string(item.get("source_line"), "diagnostic source_line"),
            notes=_load_notes(item.get("notes", [])),
            hint=_optional_string(item.get("hint"), "diagnostic hint"),
        )
        for item in raw
    )
    return diagnostics, _boolean(data.get("diagnostics_truncated", False), "diagnostics_truncated")


def _load_notes(raw: Any) -> tuple[LoadNote, ...]:
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ProtocolError("diagnostic notes must be a list of objects.")
    return tuple(
        LoadNote(
            message=_string(item.get("message"), "note message"),
            file=_optional_string(item.get("file"), "note file"),
            line=_optional_count(item.get("line"), "note line"),
        )
        for item in raw
    )


def _load_sites(raw: Any) -> dict[str, tuple[str | None, int | None]]:
    """``{"files": [file, ...], "nodes": {node_id: [file_index, line]}}`` as ``{node_id: (file, line)}``."""
    if raw is None:
        return {}
    files, nodes = (raw.get("files"), raw.get("nodes")) if isinstance(raw, dict) else (None, None)
    if not isinstance(files, list) or not isinstance(nodes, dict):
        raise ProtocolError("sites must hold a files list and a nodes object.")
    paths = [_optional_string(file, "site file") for file in files]
    sites: dict[str, tuple[str | None, int | None]] = {}
    for node_id, site in nodes.items():
        if not (isinstance(site, list) and len(site) == 2 and type(site[0]) is int and 0 <= site[0] < len(paths)):
            raise ProtocolError("a node site must be [file_index, line].")
        sites[node_id] = (paths[site[0]], _optional_count(site[1], "site line"))
    return sites


@dataclass(frozen=True, slots=True)
class PythonResult:
    value: WorkflowValue
    stdout: CapturedOutput
    last_emit_ordinal: int


@dataclass(frozen=True, slots=True)
class CancelResult:
    cancelled: bool
    leaked_threads_by_pool: dict[str, int]


@dataclass(frozen=True, slots=True)
class NativeOutput:
    text: str
    dropped_bytes: int


def _encode(frame: dict[str, Any]) -> bytes:
    try:
        return encode_frame(frame)
    except ProtocolError as exc:
        raise WorkerRpcError(ErrorCode.VALUE_TOO_LARGE, str(exc)) from exc
    except ValueError as exc:
        raise WorkerRpcError(ErrorCode.VALUE_NOT_SERIALIZABLE, str(exc)) from exc


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ProtocolError(f"{field} must be a string.")
    return value


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ProtocolError(f"{field} must be a boolean.")
    return value


def _optional_string(value: Any, field: str) -> str | None:
    return None if value is None else _string(value, field)


def _optional_count(value: Any, field: str) -> int | None:
    """A 1-based line or column, or ``None``."""
    if value is not None and (type(value) is not int or value < 1):
        raise ProtocolError(f"{field} must be a positive integer or null.")
    return value


def _ordinal(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ProtocolError("last_emit_ordinal must be a non-negative integer.")
    return value


def _leaks(payload: Mapping[str, Any]) -> dict[str, int]:
    pools = payload.get("leaked_threads_by_pool")
    if (
        not isinstance(pools, dict)
        or set(pools) != {"body", "eval"}
        or any(type(count) is not int or count < 0 for count in pools.values())
    ):
        raise ProtocolError("invalid per-pool thread leak verdict.")
    return pools


def _captured(payload: Any) -> CapturedOutput:
    if payload is None:
        return CapturedOutput(text="", truncated=False)
    if not isinstance(payload, dict):
        raise ProtocolError("stdout must be an object.")
    return CapturedOutput(
        _string(payload.get("text"), "stdout.text"), _boolean(payload.get("truncated"), "stdout.truncated")
    )


@dataclass(frozen=True, slots=True)
class EvaluationResult[T]:
    value: T
    stdout: CapturedOutput


@dataclass(slots=True)
class _Pending:
    method: str
    key: str | None
    future: asyncio.Future[dict[str, Any]]
    lanes: tuple[str, ...]
    sent: bool = False


class WorkflowWorkerClient:
    """Async RPC client bound to one worker process; see the module docstring."""

    def __init__(
        self,
        process: ManagedStdioProcess,
        *,
        bytecode_cache: Path,
        ask_handler: AskHandler | None,
        emit_handler: EmitHandler | None,
    ) -> None:
        self._process = process
        self._bytecode_cache = bytecode_cache
        self._ask_handler = ask_handler
        self._emit_handler = emit_handler
        self._loop = asyncio.get_running_loop()
        self._next_id = 1
        self._pending: dict[int, _Pending] = {}
        self._in_flight = {"body": 0, "eval": 0, "sync": 0}
        self._capacity = {
            "body": MAX_IN_FLIGHT_REQUESTS,
            "eval": LIMITS.eval_thread_pool_size,
            "sync": LIMITS.worker_thread_pool_size,
        }
        self._capacity_changed = asyncio.Event()
        self._hello: asyncio.Future[Hello] = self._loop.create_future()
        self._terminal: set[str] = set()
        self._projected: dict[str, int] = {}
        self._projection = asyncio.Condition()
        self._emits: asyncio.Queue[tuple[AttemptRef, int, str] | None] = asyncio.Queue()
        self._ask_tasks: dict[str, set[asyncio.Task[None]]] = defaultdict(set)
        self._asks: set[asyncio.Task[None]] = set()  # every live ask task, whichever attempt it served
        self._lost: WorkerLostError | None = None
        self._lost_event = asyncio.Event()
        self._closing: asyncio.Task[None] | None = None
        self._frames = 0
        self._bytes = 0
        self._stderr_tail = bytearray()
        self._leaked_threads_by_pool = {"body": 0, "eval": 0}
        self._reader = asyncio.create_task(self._read_loop(), name="chrys.workflow.worker.reader")
        self._tasks = [
            self._reader,
            asyncio.create_task(self._dispatch_emits(), name="chrys.workflow.worker.emits"),
            asyncio.create_task(self._read_stderr(), name="chrys.workflow.worker.stderr"),
            asyncio.create_task(self._watch_exit(), name="chrys.workflow.worker.exit"),
        ]

    # ----------------------------------------------------------------- lifecycle

    @classmethod
    async def launch(
        cls,
        *,
        environment: PreparedEnvironment,
        sdk: SdkArtifact,
        workspace: Path,
        bytecode_cache: Path,
        env: Mapping[str, str] | None = None,
        ask_handler: AskHandler | None = None,
        emit_handler: EmitHandler | None = None,
        host_path: Path = HOST_PATH,
        hello_timeout: float = LIMITS.hello_timeout,
    ) -> WorkflowWorkerClient:
        """Spawn the host on the prepared interpreter, inject *sdk*, and wait for a hello that matches both.

        The interpreter and every Python process it starts keep compiled
        bytecode only under the private *bytecode_cache*, never next to a
        source file, so a cache planted or left beside a workflow can't run in
        place of the source that was confirmed.
        """
        if environment.sdk_digest != sdk.digest:
            raise WorkerStartError("The environment was prepared against a different SDK build than the one injected.")
        try:
            await asyncio.to_thread(ensure_owner_only_directory, bytecode_cache)
        except OSError as exc:
            raise WorkerStartError(
                f"Cannot prepare the workflow bytecode cache {str(bytecode_cache)!r}: {exc}"
            ) from exc
        child_env = dict(os.environ)
        child_env.update(env or {})
        child_env[BYTECODE_CACHE_ENV] = str(bytecode_cache)
        from chrys.foundation import guard

        try:
            # Its compiled bytecode: this worker's alone, no command the model starts may change it.
            with guard.current().launching_configured(writes=[str(bytecode_cache)]):
                process = await spawn_managed_stdio_process(
                    environment.executable,
                    str(host_path),
                    str(sdk.path),
                    cwd=str(workspace),
                    env=child_env,
                    limit=_STDIO_LIMIT,
                    parent_env=dict(os.environ),
                )
        except MissingWorkingDirectoryError as exc:
            raise WorkerStartError(f"Cannot start the workflow worker: {exc}") from exc
        except OSError as exc:
            raise WorkerStartError(f"Cannot start the workflow worker with {environment.executable!r}: {exc}") from exc
        client = cls(process, bytecode_cache=bytecode_cache, ask_handler=ask_handler, emit_handler=emit_handler)
        try:
            await client._handshake(hello_timeout, environment)
        except BaseException:
            # Nobody else holds the client yet: a failed or cancelled launch owns its cleanup.
            await client.close()
            raise
        return client

    async def _handshake(self, hello_timeout: float, environment: PreparedEnvironment) -> None:
        try:
            hello = await asyncio.wait_for(asyncio.shield(self._hello), timeout=hello_timeout)
        except TimeoutError:
            raise WorkerStartError(f"The workflow worker sent no hello within {hello_timeout:g}s.") from None
        except WorkerLostError as exc:
            raise WorkerStartError(f"The workflow worker exited before hello: {exc}{self._stderr_suffix()}") from exc
        if hello.protocol_version != PROTOCOL_VERSION:
            raise WorkerStartError(f"Worker speaks protocol {hello.protocol_version}, expected {PROTOCOL_VERSION}.")
        if not hello.sdk_origin_ok:
            raise WorkerStartError(f"Workflow SDK injection failed: {hello.sdk_origin_error}")
        facts = (hello.implementation, hello.python_version, hello.platform)
        expected = (environment.implementation, environment.python_version, environment.platform)
        if facts != expected:
            raise WorkerStartError(
                f"The worker is {' '.join(facts)}, but the environment was prepared as {' '.join(expected)}."
            )

    @property
    def lost(self) -> WorkerLostError | None:
        return self._lost

    async def wait_lost(self) -> WorkerLostError:
        """Return once the worker is lost, for whatever reason (a close counts): the runner's watch."""
        await self._lost_event.wait()
        return self._lost_error()

    @property
    def stderr_tail(self) -> str:
        return bytes(self._stderr_tail).decode("utf-8", errors="replace")

    async def close(self, *, grace: float = LIMITS.shutdown_grace) -> None:
        """Shut the worker down: shutdown RPC, EOF, terminate, kill; concurrent callers share one close."""
        if self._closing is None:
            self._closing = asyncio.create_task(self._close(grace), name="chrys.workflow.worker.close")
        await asyncio.shield(self._closing)  # a cancelled waiter must not take the close down with it

    async def _close(self, grace: float) -> None:
        if self._lost is None and self._process.returncode is None:
            with contextlib.suppress(WorkerError, TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(self._request(Method.SHUTDOWN, {}, key=None), timeout=grace)
        self._process.close_stdin()
        if not await self._wait_exit(grace):
            self._process.terminate_tree()
            if not await self._wait_exit(grace):
                self._process.kill_tree()
                await self._wait_exit(grace)
        self._mark_lost("worker closed")
        for task in self._tasks:
            task.cancel()
        # Ask tasks were cancelled by settlement or by _mark_lost; their handlers may still be cleaning up.
        for task in [*self._tasks, *self._asks]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._process.close_transports()

    async def _wait_exit(self, timeout: float) -> bool:
        """True once the worker and everything left in its process group are gone: the tree is what we own."""
        deadline = self._loop.time() + timeout
        try:
            await asyncio.wait_for(self._process.wait(), timeout=timeout)
        except TimeoutError:
            return False
        while self._group_alive():
            if self._loop.time() >= deadline:
                return False
            await asyncio.sleep(0.02)
        return True

    def _group_alive(self) -> bool:
        """A body's child that ignores SIGTERM outlives the worker; it still sits in the worker's process group."""
        group = self._process.process_group_id
        if get_platform().is_windows or group is None:  # the Windows job object takes the tree down on close
            return False
        return process_group_alive(group)

    # ----------------------------------------------------------------- RPC surface

    async def load(
        self,
        source: bytes,
        *,
        filename: str,
        workspace: Path,
        package_dir: str | None = None,
        diagnose: bool = False,
        precompile: Sequence[str] = (),
    ) -> LoadResult:
        """Execute the workflow file from *source* bytes; ``filename`` is its original canonical path.

        *package_dir* names a workflow folder: its modules' cached bytecode is
        cleared first, so every file in it compiles from its current source.
        With *diagnose*, the worker first compiles the *precompile* files (the
        folder's other Python files) without running them, places a failure
        in ``load_diagnostics(error.data)`` and reports where each node was
        declared; a load that times out carries ``data["reason"] == LOAD_TIMED_OUT``.
        """
        try:
            text = source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkerRpcError(ErrorCode.LOAD_FAILED, f"workflow file is not UTF-8: {exc}") from exc
        params: dict[str, Any] = {
            "source": text,
            "filename": filename,
            "workspace": str(workspace),
            "bytecode_cache": str(self._bytecode_cache),
        }
        if package_dir is not None:
            params["package_dir"] = package_dir
        if diagnose:
            params["diagnose"] = True
            params["precompile"] = list(precompile)
        try:
            result = await asyncio.wait_for(self._call(Method.LOAD, params, key=None), timeout=LIMITS.load_timeout)
        except TimeoutError:
            await self.close()
            raise WorkerRpcError(
                ErrorCode.LOAD_FAILED,
                f"Workflow load did not finish within {LIMITS.load_timeout:g}s.",
                {"reason": LOAD_TIMED_OUT},
            ) from None
        manifest = result.get("manifest")
        if not isinstance(manifest, dict):
            raise ProtocolError("load result must carry a manifest object.")
        if type(manifest.get("schema_version")) is not int:
            raise ProtocolError("manifest schema_version must be an integer.")
        try:
            manifest_warnings(manifest)
        except ManifestError as exc:
            raise ProtocolError(str(exc)) from exc
        return LoadResult(
            entry_digest=hashlib.sha256(source).hexdigest(),
            manifest_digest=hashlib.sha256(canonical_json(manifest).encode("utf-8", "surrogatepass")).hexdigest(),
            manifest=manifest,
            stdout=_captured(result.get("stdout")),
            sites=_load_sites(result.get("sites")),
            sites_truncated=_boolean(result.get("sites_truncated", False), "sites_truncated"),
        )

    async def run_python(
        self, ref: AttemptRef, value: WorkflowValue, *, blocking: bool, timeout: float | None = None
    ) -> PythonResult:
        """Run one python-node attempt; raises :class:`WorkerRpcError`, :class:`AttemptTimeout`, or lost."""
        params = {"ref": ref_to_wire(ref), "value": value_to_wire(value)}
        frame = await self._request(
            Method.RUN_PYTHON, params, key=ref_key(ref), ref=ref, timeout=timeout, blocking=blocking
        )
        error = frame.get("error")
        if error is not None:
            data = error.get("data", {})
            if error["code"] != ErrorCode.ATTEMPT_TERMINATED:
                await self.wait_projected(ref, _ordinal(data.get("last_emit_ordinal", 0)))
            raise WorkerRpcError(error["code"], error["message"], data)
        result = frame["result"]
        last_emit_ordinal = _ordinal(result.get("last_emit_ordinal", 0))
        await self.wait_projected(ref, last_emit_ordinal)
        return PythonResult(
            value=value_from_wire(result.get("value"), where="python result"),
            stdout=_captured(result.get("stdout")),
            last_emit_ordinal=last_emit_ordinal,
        )

    async def eval_outgoing(
        self, ref: AttemptRef, value: WorkflowValue, edge_ids: tuple[str, ...], *, timeout: float | None = None
    ) -> EvaluationResult[dict[str, bool]]:
        params = {"ref": ref_to_wire(ref), "value": value_to_wire(value), "edge_ids": list(edge_ids)}
        result = await self._evaluate(Method.EVAL_OUTGOING, params, ref, timeout)
        decisions = result.get("decisions")
        if not isinstance(decisions, dict) or set(decisions) != set(edge_ids):
            raise ProtocolError("eval_outgoing decisions do not cover the requested edges.")
        return EvaluationResult(
            {edge_id: _boolean(decisions[edge_id], "edge decision") for edge_id in edge_ids},
            _captured(result.get("stdout")),
        )

    async def eval_loop_until(
        self, ref: AttemptRef, iteration: int, value: WorkflowValue, *, timeout: float | None = None
    ) -> EvaluationResult[bool]:
        params = {"ref": ref_to_wire(ref), "iteration": iteration, "value": value_to_wire(value)}
        result = await self._evaluate(Method.EVAL_LOOP_UNTIL, params, ref, timeout)
        return EvaluationResult(_boolean(result.get("verdict"), "until verdict"), _captured(result.get("stdout")))

    async def combine(
        self, ref: AttemptRef, sources: tuple[SourceValue, ...], *, timeout: float | None = None
    ) -> EvaluationResult[WorkflowValue]:
        wire = [source_to_wire(source) for source in sources]
        retained = len(canonical_json(wire).encode("utf-8", "surrogatepass"))
        if retained > LIMITS.max_join_retained_bytes:
            raise WorkerRpcError(ErrorCode.VALUE_TOO_LARGE, "join sources exceed max_join_retained_bytes.")
        result = await self._evaluate(Method.COMBINE, {"ref": ref_to_wire(ref), "sources": wire}, ref, timeout)
        return EvaluationResult(
            value_from_wire(result.get("value"), where="combine result"), _captured(result.get("stdout"))
        )

    async def cancel(self, ref: AttemptRef) -> CancelResult:
        """Cancel exactly this attempt; the ack sums the leak verdicts its terminal envelopes just carried."""
        result = await self._call(Method.CANCEL, {"ref": ref_to_wire(ref)}, key=None)
        outcome = CancelResult(
            cancelled=_boolean(result.get("cancelled"), "cancelled"), leaked_threads_by_pool=_leaks(result)
        )
        if self._lost is not None:
            raise self._lost_error()
        return outcome

    async def native_output(self) -> NativeOutput:
        result = await self._call(Method.NATIVE_OUTPUT, {}, key=None)
        return NativeOutput(text=str(result["text"]), dropped_bytes=int(result["dropped_bytes"]))

    async def wait_projected(self, ref: AttemptRef, ordinal: int) -> None:
        """Block until every emit of *ref* up to *ordinal* went through the emit handler."""
        key = ref_key(ref)
        async with self._projection:
            await self._projection.wait_for(lambda: self._lost is not None or self._projected.get(key, 0) >= ordinal)
            if self._projected.get(key, 0) < ordinal:
                raise self._lost_error()  # the emits can no longer be projected: converge to worker-lost

    # ----------------------------------------------------------------- internals

    async def _evaluate(
        self, method: str, params: dict[str, Any], ref: AttemptRef, timeout: float | None
    ) -> dict[str, Any]:
        deadline = LIMITS.eval_deadline if timeout is None else timeout
        frame = await self._request(method, params, key=None, ref=ref, timeout=deadline)
        error = frame.get("error")
        if error is not None:
            raise WorkerRpcError(error["code"], error["message"], error.get("data"))
        return frame["result"]

    async def _deadline(
        self, request: Awaitable[dict[str, Any]], pending: _Pending, ref: AttemptRef, timeout: float | None
    ) -> dict[str, Any]:
        if timeout is None:
            return await request
        task = asyncio.ensure_future(request)
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
            if done:
                return task.result()
            grace = LIMITS.shutdown_grace
            try:
                outcome = await asyncio.wait_for(self.cancel(ref), timeout=grace)
            except TimeoutError:
                self._mark_lost(f"cancel of {ref.activation_id} was not acknowledged within {grace:g}s")
                raise self._lost_error() from None
            # The worker sends the terminal envelope before acknowledging cancellation.
            if not pending.future.done():
                self._mark_lost(f"cancel of {ref.activation_id} was acknowledged without a terminal envelope")
            frame = pending.future.result()
            payload = frame["error"].get("data", {}) if "error" in frame else frame["result"]
            raise AttemptTimeout(ref, leaked_thread=any(outcome.leaked_threads_by_pool.values()), data=payload)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _call(self, method: str, params: dict[str, Any], *, key: str | None) -> dict[str, Any]:
        frame = await self._request(method, params, key=key)
        error = frame.get("error")
        if error is not None:
            raise WorkerRpcError(error["code"], error["message"], error.get("data"))
        return frame["result"]

    async def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        key: str | None,
        ref: AttemptRef | None = None,
        timeout: float | None = None,
        blocking: bool = False,
    ) -> dict[str, Any]:
        # No deadline runs while waiting for capacity. The pending entry owns its slot, even when
        # its caller stops waiting: only a terminal envelope, loss, or a never-sent frame releases it.
        lanes = () if method in _CONTROL_METHODS else ("eval",) if method in _EVAL_METHODS else ("body",)
        if blocking:
            lanes += ("sync",)
        while (
            self._lost is None
            and lanes
            and (
                any(self._in_flight[lane] >= self._capacity[lane] for lane in lanes)
                or len(self._pending) >= LIMITS.max_pending_requests
            )
        ):
            self._capacity_changed.clear()
            await self._capacity_changed.wait()
        if self._lost is not None:
            raise self._lost_error()
        request_id = self._next_id
        self._next_id += 2
        data = _encode({"id": request_id, "method": method, "params": params})
        future: asyncio.Future[dict[str, Any]] = self._loop.create_future()
        pending = _Pending(method, key, future, lanes)
        self._pending[request_id] = pending
        for lane in lanes:
            self._in_flight[lane] += 1
        try:
            request = self._exchange(data, pending)
            return await request if ref is None else await self._deadline(request, pending, ref, timeout)
        finally:
            if not pending.sent:
                self._pop_pending(request_id)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # loss during transmission may settle it before _exchange awaits it

    async def _exchange(self, data: bytes, pending: _Pending) -> dict[str, Any]:
        await self._transmit(data, pending=pending)
        return await pending.future

    def _pop_pending(self, request_id: int) -> _Pending | None:
        pending = self._pending.pop(request_id, None)
        if pending is not None:
            for lane in pending.lanes:
                self._in_flight[lane] -= 1
            self._capacity_changed.set()
        return pending

    async def _send(self, frame: dict[str, Any]) -> None:
        await self._transmit(_encode(frame))

    async def _transmit(self, data: bytes, *, pending: _Pending | None = None) -> None:
        self._account(len(data))
        if self._lost is not None:
            raise self._lost_error()
        try:
            self._process.stdin.write(data)
            if pending is not None:
                pending.sent = True
            await self._process.stdin.drain()
        except (OSError, RuntimeError, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            self._mark_lost(f"write failed: {exc}")
            raise self._lost_error() from exc

    def _account(self, size: int) -> None:
        self._frames += 1
        self._bytes += size
        if self._frames > LIMITS.max_run_frames or self._bytes > LIMITS.max_run_bytes:
            self._mark_lost("run frame budget exhausted")

    def _charge(self, frame: dict[str, Any]) -> None:
        """Charge the threads an attempt left behind to the pool budget.

        Called once per attempt, from its terminal envelope: each phase of a ref (body, then each
        evaluation) is its own attempt with its own verdict, and a cancel ack only sums the verdicts the
        envelopes it follows already carried.
        """
        payload = frame["error"].get("data", {}) if "error" in frame else frame["result"]
        # Invalid requests may fail before an attempt was registered, without a pool verdict.
        if "error" in frame and "leaked_threads_by_pool" not in payload:
            return
        pools = _leaks(payload)
        for pool, budget in (("body", LIMITS.worker_leak_budget), ("eval", LIMITS.eval_leak_budget)):
            self._leaked_threads_by_pool[pool] += pools[pool]
            if self._leaked_threads_by_pool[pool] >= budget:
                self._mark_lost(f"{pool} thread leak budget exhausted")
        # Do not reuse capacity charged as leaked: its abandoned work may never finish. Keep
        # admission matched to the remaining threads, rather than queueing behind abandoned work.
        self._capacity["eval"] = max(0, LIMITS.eval_thread_pool_size - self._leaked_threads_by_pool["eval"])
        self._capacity["sync"] = max(0, LIMITS.worker_thread_pool_size - self._leaked_threads_by_pool["body"])

    def _lost_error(self) -> WorkerLostError:
        if self._lost is None:
            raise RuntimeError("The workflow worker has not been marked lost.")
        return self._lost

    def _stderr_suffix(self) -> str:
        tail = self.stderr_tail.strip()
        return f" (stderr: {tail[-500:]})" if tail else ""

    def _mark_lost(self, reason: str) -> None:
        if self._lost is not None:
            return
        self._lost = WorkerLostError(reason)
        self._lost_event.set()
        for request_id in tuple(self._pending):
            pending = self._pop_pending(request_id)
            if pending is None:
                raise RuntimeError("A registered worker request disappeared during failure handling.")
            if not pending.future.done():
                pending.future.set_exception(self._lost)
        self._capacity_changed.set()  # also wake callers that never registered a request
        if not self._hello.done():
            self._hello.set_exception(self._lost)
        for tasks in self._ask_tasks.values():
            for task in tasks:
                task.cancel()
        self._emits.put_nowait(None)
        self._loop.create_task(self._wake_projection(), name="chrys.workflow.worker.wake")
        with contextlib.suppress(Exception):
            self._process.terminate_tree()

    async def _wake_projection(self) -> None:
        async with self._projection:
            self._projection.notify_all()

    async def _read_loop(self) -> None:
        stdout = self._process.stdout
        reason = "worker exited"
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                self._account(len(line))
                self._on_frame(decode_frame(line.rstrip(b"\r\n")))
        except asyncio.CancelledError:
            raise
        except (ProtocolError, ValueError) as exc:
            reason = f"protocol error: {exc}"
        except Exception as exc:
            reason = f"reader failed: {exc!r}"
        self._mark_lost(f"{reason}{self._stderr_suffix()}")

    async def _watch_exit(self) -> None:
        """The exit status settles a dead worker's debts even while a forked child of its holds the pipes open."""
        code = await self._process.wait()
        # Frames written before the exit are still in the pipe: the reader drains them and, at EOF, marks the loss.
        await asyncio.wait({self._reader}, timeout=LIMITS.shutdown_grace)
        self._mark_lost(f"worker exited with status {code}{self._stderr_suffix()}")

    async def _read_stderr(self) -> None:
        stderr = self._process.stderr
        while chunk := await stderr.read(64 * 1024):
            self._stderr_tail.extend(chunk)
            if len(self._stderr_tail) > STDERR_TAIL_BYTES:
                del self._stderr_tail[:-STDERR_TAIL_BYTES]

    def _on_frame(self, frame: dict[str, Any]) -> None:
        if self._lost is not None:
            return  # lost: its debts are settled already, and an ask would start a handler nobody cancels
        if "method" not in frame:
            pending = self._pop_pending(frame["id"])
            if pending is None:
                return
            if pending.key is not None and pending.method == Method.RUN_PYTHON:
                self._settle_attempt(pending.key)
            if pending.method in _ATTEMPT_METHODS:
                try:
                    self._charge(frame)
                except (TypeError, ValueError) as exc:
                    self._mark_lost(str(exc))
                    if not pending.future.done():
                        pending.future.set_exception(self._lost_error())
                    return
            if not pending.future.done():
                pending.future.set_result(frame)
            return
        method, params = frame["method"], frame["params"]
        if "id" in frame:
            if method == Method.ASK:
                self._on_ask(frame["id"], params)
            else:
                self._loop.create_task(
                    self._respond_error(frame["id"], ErrorCode.UNKNOWN_METHOD, f"unknown reverse request {method!r}.")
                )
        elif method == Method.HELLO:
            if not self._hello.done():
                self._hello.set_result(_hello_from_wire(params))
        elif method == Method.EMIT:
            self._on_emit(params)

    def _settle_attempt(self, key: str) -> None:
        self._terminal.add(key)
        for task in self._ask_tasks.pop(key, ()):
            task.cancel()

    def _on_emit(self, params: dict[str, Any]) -> None:
        ref = ref_from_wire(params.get("ref"))
        ordinal, text = params.get("ordinal"), params.get("text")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or not isinstance(text, str):
            raise ProtocolError("emit needs an int ordinal and a str text.")
        if ref_key(ref) in self._terminal:
            return
        self._emits.put_nowait((ref, ordinal, text))

    async def _dispatch_emits(self) -> None:
        while (item := await self._emits.get()) is not None:
            ref, ordinal, text = item
            try:
                if self._emit_handler is not None:
                    await self._emit_handler(ref, ordinal, text)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("workflow emit handler failed for %s", ref.activation_id, exc_info=True)
            finally:
                async with self._projection:
                    key = ref_key(ref)
                    self._projected[key] = max(self._projected.get(key, 0), ordinal)
                    self._projection.notify_all()

    def _on_ask(self, request_id: int, params: dict[str, Any]) -> None:
        ref = ref_from_wire(params.get("ref"))
        questions = questions_from_wire(params.get("questions"))
        key = ref_key(ref)
        if key in self._terminal:
            self._loop.create_task(self._respond_error(request_id, ErrorCode.ATTEMPT_TERMINATED, "attempt is over."))
            return
        if self._ask_handler is None:
            self._loop.create_task(
                self._respond_error(request_id, ErrorCode.ASK_UNAVAILABLE, "nobody can answer in this run.")
            )
            return
        task = self._loop.create_task(self._serve_ask(request_id, ref, questions), name="chrys.workflow.worker.ask")
        self._ask_tasks[key].add(task)
        self._asks.add(task)
        task.add_done_callback(lambda done, key=key: self._forget_ask(key, done))

    def _forget_ask(self, key: str, task: asyncio.Task[None]) -> None:
        self._ask_tasks[key].discard(task)
        self._asks.discard(task)

    async def _serve_ask(self, request_id: int, ref: AttemptRef, questions: tuple[AskUserQuestion, ...]) -> None:
        if self._ask_handler is None:
            raise RuntimeError("Serving a workflow question requires an ask handler.")
        try:
            answers = answers_to_wire(questions, await self._ask_handler(ref, questions))
        except asyncio.CancelledError:
            with contextlib.suppress(WorkerError):
                await self._respond_error(request_id, ErrorCode.ATTEMPT_TERMINATED, "attempt is over.")
            raise
        except AskUnavailable as exc:
            await self._respond_error(request_id, ErrorCode.ASK_UNAVAILABLE, str(exc) or "nobody can answer.")
        except Exception as exc:
            logger.warning("workflow ask handler failed for %s", ref.activation_id, exc_info=True)
            await self._respond_error(request_id, ErrorCode.ASK_UNAVAILABLE, f"ask handler failed: {exc!r}")
        else:
            try:
                await self._send({"id": request_id, "result": {"answers": answers}})
            except WorkerRpcError as exc:  # the answer cannot be framed: the worker must not wait forever
                await self._respond_error(request_id, ErrorCode.ASK_UNAVAILABLE, f"answer is not serializable: {exc}")
            except WorkerLostError:
                pass

    async def _respond_error(self, request_id: int, code: str, message: str) -> None:
        message = _bounded_diagnostic(message.encode("utf-8", "replace").decode("utf-8"))  # must always frame
        with contextlib.suppress(WorkerError):
            await self._send({"id": request_id, "error": {"code": code, "message": message, "data": {}}})


def _hello_from_wire(params: dict[str, Any]) -> Hello:
    try:
        return Hello(
            protocol_version=int(params["protocol_version"]),
            python_version=str(params["python_version"]),
            implementation=str(params["implementation"]),
            platform=str(params["platform"]),
            sdk_origin_ok=bool(params["sdk_origin_ok"]),
            sdk_origin_error=None if params.get("sdk_origin_error") is None else str(params["sdk_origin_error"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ProtocolError(f"hello frame is malformed: {exc!r}") from exc
