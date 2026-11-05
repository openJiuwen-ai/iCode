# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Owned ACP process spawning, environment hygiene, and bounded stderr capture."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import math
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO

from acp import default_environment

from chrys.foundation.platform.files import SecureFileError, secure_open_owner_only
from chrys.foundation.platform.process import ManagedStdioProcess, spawn_managed_stdio_process
from chrys.foundation.util.env_templates import EnvVarResolutionError, command_handles_in

from .errors import AcpClientError, AcpConfigError, classify_spawn_error
from .spec import AcpAgentSpec

ACP_STDIO_LIMIT_BYTES = 50 * 1024 * 1024
STDERR_LOG_QUOTA_BYTES = 5 * 1024 * 1024
STDERR_TAIL_BYTES = 4 * 1024
_STDERR_DRAIN_TIMEOUT_SECONDS = 2.0
DEPTH_ENV_VAR = "CHRYS_ACP_SUBAGENT_DEPTH"


def _validate_exact_spawn_string(value: object, *, field: str) -> str:
    if type(value) is not str:
        raise AcpConfigError(f"ACP {field} must be a string.")
    if "\x00" in value:
        raise AcpConfigError(f"ACP {field} cannot contain NUL.")
    return value


def build_child_environment(spec: AcpAgentSpec) -> dict[str, str]:
    """Build a case-fold-deduplicated trimmed environment for one child."""
    if type(spec.depth) is not int or spec.depth < 0:
        raise AcpConfigError("ACP recursion depth must be a non-negative integer.")

    base = default_environment()
    canonical_by_fold = {key.casefold(): key for key in base}
    merged: dict[str, str] = {}
    present_by_fold: dict[str, str] = {}

    def overlay(key: object, value: object, *, preserve_base_case: bool) -> None:
        key_text = _validate_exact_spawn_string(key, field="environment key")
        value_text = _validate_exact_spawn_string(value, field=f"environment value for {key_text!r}")
        if not key_text or "=" in key_text:
            raise AcpConfigError("ACP environment keys must be non-empty and cannot contain '='.")
        folded = key_text.casefold()
        previous = present_by_fold.get(folded)
        if previous is not None:
            merged.pop(previous, None)
        output_key = canonical_by_fold.get(folded, key_text) if preserve_base_case else key_text
        merged[output_key] = value_text
        present_by_fold[folded] = output_key

    for key, value in base.items():
        overlay(key, value, preserve_base_case=True)
    for key, value in spec.env.items():
        overlay(key, value, preserve_base_case=True)
    overlay(DEPTH_ENV_VAR, str(spec.depth + 1), preserve_base_case=False)
    return merged


def validate_agent_spec(spec: AcpAgentSpec) -> None:
    """Fail closed on malformed values that reach the process/protocol boundary."""
    _validate_exact_spawn_string(spec.command, field="command")
    cwd = _validate_exact_spawn_string(spec.cwd, field="cwd")
    if not os.path.isabs(cwd):
        raise AcpConfigError("ACP cwd must be absolute.")
    for arg in spec.args:
        _validate_exact_spawn_string(arg, field="argument")
    for key, value in spec.env.items():
        _validate_exact_spawn_string(key, field="environment key")
        _validate_exact_spawn_string(value, field=f"environment value for {key!r}")
    if type(spec.handshake_timeout_seconds) not in {int, float} or not math.isfinite(spec.handshake_timeout_seconds):
        raise AcpConfigError("ACP handshake timeout must be finite.")
    if spec.handshake_timeout_seconds <= 0:
        raise AcpConfigError("ACP handshake timeout must be greater than zero.")
    if type(spec.idle_timeout_seconds) not in {int, float} or not math.isfinite(spec.idle_timeout_seconds):
        raise AcpConfigError("ACP idle timeout must be finite.")
    if spec.idle_timeout_seconds < 0:
        raise AcpConfigError("ACP idle timeout cannot be negative.")
    if type(spec.best_effort_options) is not bool:
        raise AcpConfigError("ACP best-effort option mode must be a boolean.")
    if type(spec.session_mode) is not str or type(spec.model_id) is not str:
        raise AcpConfigError("ACP session mode and model id must be strings.")
    for root in spec.additional_directories:
        directory = _validate_exact_spawn_string(root, field="additional workspace directory")
        # ACP requires every additional workspace root to be absolute; a
        # relative value would resolve agent-dependently.
        if not os.path.isabs(directory):
            raise AcpConfigError("ACP additional workspace directories must be absolute.")
    for option_id, value in spec.config_options.items():
        if type(option_id) is not str or not option_id:
            raise AcpConfigError("ACP config option ids must be non-empty strings.")
        if type(value) not in {str, bool}:
            raise AcpConfigError("ACP config option values must be exact strings or booleans.")
    if type(spec.depth) is not int or spec.depth < 0:
        raise AcpConfigError("ACP recursion depth must be a non-negative integer.")


class StderrCapture:
    """Quota-bounded owner-only stderr log plus a small diagnostics tail."""

    def __init__(self, reader: asyncio.StreamReader, path: Path) -> None:
        self._reader = reader
        self._path = path
        self._tail = bytearray()
        self._written_bytes = 0
        self._dropped_bytes = 0
        self._task: asyncio.Task[None] | None = None
        self._file: BinaryIO | None = None

    @property
    def tail(self) -> str:
        return bytes(self._tail).decode("utf-8", errors="replace")

    @property
    def dropped_bytes(self) -> int:
        return self._dropped_bytes

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("stderr capture already started")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = secure_open_owner_only(self._path, write=True, create=True, truncate=True)
        except SecureFileError as exc:
            if exc.errno not in {errno.EEXIST, 80, 183}:
                raise
            fd = secure_open_owner_only(self._path, write=True, truncate=True)
        self._file = os.fdopen(fd, "wb", closefd=True)
        self._task = asyncio.create_task(self._run(), name="chrys.acp.stderr")

    async def _run(self) -> None:
        try:
            while chunk := await self._reader.read(64 * 1024):
                self._tail.extend(chunk)
                if len(self._tail) > STDERR_TAIL_BYTES:
                    del self._tail[:-STDERR_TAIL_BYTES]
                remaining = STDERR_LOG_QUOTA_BYTES - self._written_bytes
                if remaining <= 0 or self._file is None:
                    self._dropped_bytes += len(chunk)
                    continue
                payload = chunk[:remaining]
                try:
                    written = self._file.write(payload)
                except OSError:
                    self._dropped_bytes += len(chunk)
                    self._close_file()
                    continue
                self._written_bytes += written
                self._dropped_bytes += len(chunk) - written
        finally:
            self._close_file()

    def _close_file(self) -> None:
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.flush()
            with contextlib.suppress(OSError):
                os.fsync(self._file.fileno())
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        # Bounded drain before cancel: the child is already dead or dying when
        # the close ladder gets here, so EOF arrives promptly — cancelling
        # first would discard flushed-but-unread final diagnostics.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.wait({task}, timeout=_STDERR_DRAIN_TIMEOUT_SECONDS)
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        self._task = None


@dataclass(slots=True)
class AcpSpawnResult:
    """Spawned process and its owned stderr diagnostic capture."""

    process: ManagedStdioProcess
    stderr: StderrCapture

    @property
    def stderr_tail(self) -> str:
        return self.stderr.tail


async def with_fresh_tokens(spec: AcpAgentSpec) -> AcpAgentSpec:
    """Return *spec* with a token just fetched for each ``{{!command}}`` in its arguments and environment.

    A running agent can't be given a new one, so each launch fetches its own.  Only
    the launch gets this copy: the spec the audit records keeps its handles.
    """
    values = [*spec.args, *spec.env.values()]
    if not command_handles_in(values):
        return spec
    from chrys.service.command_credentials import refresh, substitute

    try:
        await refresh(values)
    except EnvVarResolutionError as exc:
        raise AcpConfigError(str(exc), cause=exc) from exc
    return replace(
        spec,
        args=tuple(substitute(arg) for arg in spec.args),
        env={key: substitute(value) for key, value in spec.env.items()},
    )


async def spawn_acp_process(spec: AcpAgentSpec) -> AcpSpawnResult:
    """Launch the resolved ACP command with an owned process tree and streams."""
    validate_agent_spec(spec)
    command = _validate_exact_spawn_string(spec.command, field="command")
    cwd = _validate_exact_spawn_string(spec.cwd, field="cwd")
    args = tuple(_validate_exact_spawn_string(arg, field="argument") for arg in spec.args)
    parent_env = dict(os.environ)
    env = build_child_environment(spec)
    try:
        process = await spawn_managed_stdio_process(
            command,
            *args,
            cwd=cwd,
            env=env,
            limit=ACP_STDIO_LIMIT_BYTES,
            parent_env=parent_env,
        )
    except asyncio.CancelledError:
        raise
    except AcpClientError:
        raise
    except BaseException as exc:
        raise classify_spawn_error(exc) from exc

    capture = StderrCapture(process.stderr, spec.stderr_log_path)
    try:
        capture.start()
    except BaseException as exc:
        process.kill_tree()
        try:
            # Cancellation during this wait must still release the transports:
            # the caller has no AcpSpawnResult yet, so its rollback cannot
            # reach the process handles this function still owns.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=2)
        finally:
            process.close_transports()
        raise AcpConfigError("The ACP stderr log could not be created securely.", cause=exc) from exc
    return AcpSpawnResult(process=process, stderr=capture)
