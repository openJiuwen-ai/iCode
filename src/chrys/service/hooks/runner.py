# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Subprocess runner for hooks.

Three execution modes are dispatched here:

* ``blocking`` / ``async`` — spawn with stdio pipes via
  :func:`chrys.foundation.platform.process.managed_subprocess` so cleanup is
  guaranteed on cancellation; await the process and parse its result
  file (path passed via ``CHRYS_HOOK_RESULT``).  The async-vs-blocking
  distinction is purely about *who* awaits — :class:`HookManager`
  decides — so the runner just exposes a single ``run_and_wait`` entry
  point.
* ``fire_and_forget`` with ``detach: false`` — spawn the same way and
  return immediately; the manager observes completion for logging/outbox
  updates but does not await it at turn or session boundaries.
* ``fire_and_forget`` with ``detach: true`` — spawn via raw
  :class:`subprocess.Popen` with platform-specific detach flags
  (``setsid`` / ``DETACHED_PROCESS``).  Stdio is redirected to a
  log file under ``<config_dir>/hooks/logs/``.  Once spawned chrys
  forgets about the child entirely; it survives ``engine.shutdown()``.

Hook input/output flows through temp files:

* ``CHRYS_HOOK_PAYLOAD_FILE`` points at the JSON event payload.  We do not
  put the payload directly in the environment because tool arguments/results
  can exceed OS env limits.
* ``CHRYS_HOOK_RESULT`` points at the optional JSON decision file.  Empty /
  missing result file means "no opinion" — the manager treats it as
  ``action: allow`` with no modifications.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Final

from chrys.foundation.models.hook_runs import HookResultFileStatus
from chrys.foundation.platform.output_capture import BoundedCapture, drain_process_pipes
from chrys.foundation.platform.process import (
    SubprocessStoppedError,
    managed_subprocess,
    wait_for_subprocess,
)
from chrys.foundation.platform.runtime_paths import (
    is_frozen_runtime,
    reorder_path_demoting_runtime,
    strip_python_runtime_overrides,
)
from chrys.foundation.platform.script_commands import build_script_command
from chrys.foundation.tool_execution_stamp import encode_json_like
from chrys.service.hooks.retired_shell_args import with_retired_names

if TYPE_CHECKING:
    from chrys.service.hooks.outbox import OutboxJob
    from chrys.service.hooks.schema import HookConfig

logger = logging.getLogger(__name__)

HOOK_RESULT_FILE_MAX_BYTES = 1 * 1024 * 1024

HOOK_OUTPUT_CAPTURE_LIMIT_BYTES = 256 * 1024
"""Bytes kept of a waited-for hook's stdout and of its stderr; decisions come from the result file."""

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class HookResult:
    """Raw outcome of a single hook subprocess.

    The :class:`HookManager` consumes this to build a :class:`HookDecision`;
    callers outside the manager rarely look at it directly.
    """

    hook_id: str
    exit_code: int | None
    """``None`` for ``fire_and_forget`` (spawn-and-forget — we never wait)
    and for timeouts that hit the SIGKILL fallback.
    """
    timed_out: bool = False
    stopped: bool = False
    """The process entered job-control stopped state and was terminated; ``timed_out`` is set too."""
    decision: dict[str, Any] = field(default_factory=dict)
    """Parsed JSON from ``CHRYS_HOOK_RESULT``; empty dict if the file
    was absent / empty / malformed.
    """
    stdout: str = ""
    stderr: str = ""
    stdout_dropped: int = 0
    stderr_dropped: int = 0
    """Bytes cut from the middle of each stream past ``HOOK_OUTPUT_CAPTURE_LIMIT_BYTES``."""
    result_file: HookResultFileStatus = "missing"
    """Whether the result file was read (``ok``), absent or empty, or ignored as invalid."""
    duration_ms: int = 0


@dataclass(frozen=True)
class HookInvocation:
    """Fully resolved subprocess invocation for one hook run."""

    cmd: list[str]
    env: dict[str, str]
    cwd: str
    result_path: Path
    payload_path: Path


# ---------------------------------------------------------------------------
# Path / template helpers
# ---------------------------------------------------------------------------


TEMPLATE_VARIABLES: Final = ("workspace_cwd", "chrys_home", "session_id", "profile")
"""The ``${name}`` placeholders :func:`_expand_template` fills in; any other ``${...}`` is passed on as written."""


def _expand_template(value: str, *, workspace_cwd: str, chrys_home: Path, session_id: str, profile: str) -> str:
    """Substitute the ``${var}`` placeholders documented on :class:`HookRun`."""
    if not value:
        return value
    filled = zip(TEMPLATE_VARIABLES, (workspace_cwd, str(chrys_home), session_id, profile), strict=True)
    for name, text in filled:
        value = value.replace(f"${{{name}}}", text)
    return value


def _resolve_script_path(rel_or_abs: str, hooks_dir: Path) -> Path:
    p = Path(rel_or_abs)
    if p.is_absolute():
        return p
    return (hooks_dir / p).resolve()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class HookRunner:
    """Executes hooks as subprocesses.

    One instance per :class:`HookManager`; thread-safe by virtue of
    everything running on a single event loop.  All paths derived from
    ``hooks_dir`` are resolved eagerly so the runner doesn't re-walk
    the filesystem on every dispatch.
    """

    def __init__(self, *, hooks_dir: Path) -> None:
        self._hooks_dir = hooks_dir
        self._log_dir = hooks_dir / "logs"

    @property
    def hooks_dir(self) -> Path:
        return self._hooks_dir

    async def run_and_wait(
        self,
        hook: HookConfig,
        payload: dict[str, Any],
        *,
        payload_text: str | None = None,
        result: HookResult | None = None,
    ) -> HookResult:
        """Spawn *hook* with stdio pipes and await it (with timeout).

        Used for ``blocking`` and ``async`` modes.  The async variant
        only differs in *who* awaits — the manager does, after dispatch
        returns — so this entry point is shared. *payload_text*, when
        given, is written to the payload file as it is, in place of
        serializing *payload*. *result*, when given, is filled and returned
        in place of a new one: a caller whose wait is cancelled keeps what
        the hook printed until then.
        """
        start = datetime.now(UTC)
        if result is None:
            result = HookResult(hook_id=hook.id, exit_code=None)
        invocation: HookInvocation | None = None
        # Outside the subprocess block: a run that times out, is stopped or
        # is cancelled keeps what it printed until then.
        stdout = BoundedCapture(HOOK_OUTPUT_CAPTURE_LIMIT_BYTES)
        stderr = BoundedCapture(HOOK_OUTPUT_CAPTURE_LIMIT_BYTES)
        try:
            invocation = self._build_invocation(hook, payload, payload_text=payload_text)
            async with managed_subprocess(
                *invocation.cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=invocation.cwd,
                env=invocation.env,
            ) as proc:
                process_group_id = getattr(proc, "pid", None) if sys.platform != "win32" else None
                await wait_for_subprocess(
                    drain_process_pipes(proc, stdout, stderr),
                    timeout=hook.execution.timeout_seconds,
                    process_group_id=process_group_id,
                )
                result.exit_code = proc.returncode
                _take_output(result, stdout, stderr)
        except TimeoutError:
            result.timed_out = True
            _take_output(result, stdout, stderr)
            logger.warning(
                "Hook %r timed out after %.1fs",
                hook.id,
                hook.execution.timeout_seconds,
            )
            return result
        except SubprocessStoppedError:
            result.timed_out = result.stopped = True
            _take_output(result, stdout, stderr)
            note = "Hook process entered stopped state and was terminated."
            result.stderr = f"{result.stderr.rstrip()}\n{note}" if result.stderr.strip() else note
            logger.warning("Hook %r entered stopped state and was terminated", hook.id)
            return result
        except asyncio.CancelledError:
            _take_output(result, stdout, stderr)
            raise
        except (FileNotFoundError, OSError, ValueError) as exc:
            result.exit_code = None
            result.stderr = str(exc)
        finally:
            if invocation is not None:
                result.decision, result.result_file = _read_result_file(invocation.result_path)
                _cleanup_invocation_files(invocation)
            result.duration_ms = int((datetime.now(UTC) - start).total_seconds() * 1000)
        return result

    # ── internal ─────────────────────────────────────────────────────

    def _build_invocation(
        self,
        hook: HookConfig,
        payload: dict[str, Any],
        *,
        payload_text: str | None = None,
    ) -> HookInvocation:
        """Build the (argv, env, cwd) triple for a hook invocation."""
        workspace_cwd = payload.get("cwd") or os.getcwd()
        session_id = payload.get("session_id") or ""
        profile = payload.get("profile") or ""
        chrys_home = self._hooks_dir.parent

        run = hook.run
        if run.type == "script":
            script = _resolve_script_path(run.path, self._hooks_dir)
            cmd = [*build_script_command(script), *run.args]
        elif run.type == "command":
            cmd = [*run.argv, *run.args]
        else:  # shell
            cmd = self._build_shell_command(run.shell)

        cwd = (
            _expand_template(
                run.cwd,
                workspace_cwd=workspace_cwd,
                chrys_home=chrys_home,
                session_id=session_id,
                profile=profile,
            )
            or workspace_cwd
        )

        env = os.environ.copy()
        strip_python_runtime_overrides(env)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["CHRYS_HOOK_ID"] = hook.id
        env["CHRYS_HOOK_EVENT"] = str(payload.get("event", ""))
        tmp_dir = self._hooks_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        payload_fd = -1
        result_fd = -1
        payload_path_obj: Path | None = None
        result_path_obj: Path | None = None
        try:
            # Payload file: hooks read event data here.  Avoid putting large
            # tool args/results in the environment.
            payload_fd, payload_path = tempfile.mkstemp(
                prefix=f"{_safe_temp_prefix(hook.id)}-payload-",
                suffix=".json",
                dir=tmp_dir,
            )
            payload_path_obj = Path(payload_path)
            _set_owner_only(payload_fd)
            with os.fdopen(payload_fd, "w", encoding="utf-8") as f:
                payload_fd = -1
                if payload_text is None:
                    # DEPRECATED: with_retired_names shows a script the shell's retired argument names too.
                    f.write(encode_json_like(with_retired_names(payload)))
                else:
                    f.write(payload_text)
            env["CHRYS_HOOK_PAYLOAD_FILE"] = payload_path

            # Result file: hooks write their decision here so stdout/stderr
            # are free for logs.  The runner deletes the file after read.
            result_fd, result_path = tempfile.mkstemp(
                prefix=f"{_safe_temp_prefix(hook.id)}-result-",
                suffix=".json",
                dir=tmp_dir,
            )
            result_path_obj = Path(result_path)
            _set_owner_only(result_fd)
            os.close(result_fd)
            result_fd = -1
            env["CHRYS_HOOK_RESULT"] = result_path
        except BaseException:
            if payload_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(payload_fd)
            if result_fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(result_fd)
            for cleanup_path in (payload_path_obj, result_path_obj):
                if cleanup_path is not None:
                    with contextlib.suppress(OSError):
                        cleanup_path.unlink(missing_ok=True)
            raise
        if result_path_obj is None or payload_path_obj is None:
            msg = "failed to create hook payload/result temp files"
            raise RuntimeError(msg)

        for k, v in run.env.items():
            env[k] = _expand_template(
                v,
                workspace_cwd=workspace_cwd,
                chrys_home=chrys_home,
                session_id=session_id,
                profile=profile,
            )
        reorder_path_demoting_runtime(env)
        return HookInvocation(
            cmd=cmd,
            env=env,
            cwd=cwd,
            result_path=result_path_obj,
            payload_path=payload_path_obj,
        )

    def _build_shell_command(self, shell_str: str) -> list[str]:
        """Wrap a shell-syntax string in the user's shell.

        On POSIX we use ``$SHELL`` (fallback ``/bin/sh``); on Windows we
        use ``cmd.exe /c``.  Users who need a specific shell should pick
        ``type: command`` + an explicit argv.
        """
        if sys.platform == "win32":
            return ["cmd.exe", "/c", shell_str]
        shell = os.environ.get("SHELL") or "/bin/sh"
        return [shell, "-c", shell_str]

    async def spawn_detached(
        self,
        hook: HookConfig,
        payload: dict[str, Any],
        *,
        job: OutboxJob | None = None,
        payload_text: str | None = None,
    ) -> Path:
        """Spawn a detached worker, redirecting stdio to a log file; return that log's path.

        The worker runs the real hook command, updates durable outbox state,
        and cleans temp files.  On POSIX the worker starts a new session; on
        Windows it starts in a detached process group.  Either way it outlives
        the parent Chrys process. The log is named by the second the run
        started, so runs of one hook in the same second share it.
        """
        invocation = self._build_invocation(hook, payload, payload_text=payload_text)
        invocation_path: Path | None = None
        log_file: Any | None = None
        log_path = self._log_path_for(hook, payload)

        try:
            invocation_path = self._write_detached_invocation(invocation, hook_id=hook.id, job=job)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_file = _open_owner_only_append(log_path)
            worker_env = os.environ.copy()
            strip_python_runtime_overrides(worker_env)
            if is_frozen_runtime(worker_env):
                worker_cmd = [sys.executable, "-s", "-m", "chrys.service.hooks.detached_worker", str(invocation_path)]
                worker_env["PYTHONNOUSERSITE"] = "1"
            else:
                worker_cmd = [sys.executable, "-m", "chrys.service.hooks.detached_worker", str(invocation_path)]
            worker_env["PYTHONUTF8"] = "1"
            worker_env["PYTHONIOENCODING"] = "utf-8"

            kwargs: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": log_file,
                "stderr": log_file,
                "cwd": str(self._hooks_dir),
                "env": worker_env,
            }
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True

            await asyncio.to_thread(subprocess.Popen, worker_cmd, **kwargs)
        except BaseException:
            _cleanup_invocation_files(invocation)
            if invocation_path is not None:
                with contextlib.suppress(OSError):
                    invocation_path.unlink(missing_ok=True)
            raise
        finally:
            # The worker has dup'd the FD; closing here doesn't affect it.
            if log_file is not None:
                with contextlib.suppress(OSError):
                    log_file.close()
        return log_path

    def _write_detached_invocation(
        self,
        invocation: HookInvocation,
        *,
        hook_id: str,
        job: OutboxJob | None,
    ) -> Path:
        tmp_dir = self._hooks_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(
            prefix=f"{_safe_temp_prefix(hook_id)}-detached-",
            suffix=".json",
            dir=tmp_dir,
        )
        _set_owner_only(fd)
        path = Path(raw_path)
        data = {
            "cmd": invocation.cmd,
            "env": invocation.env,
            "cwd": invocation.cwd,
            "result_path": str(invocation.result_path),
            "payload_path": str(invocation.payload_path),
            "outbox_root": str(self._hooks_dir / "outbox") if job is not None else "",
            "job": job.__dict__ if job is not None else None,
        }
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                fd = -1
                json.dump(data, f)
        except BaseException:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
            raise
        return path

    def _log_path_for(self, hook: HookConfig, payload: dict[str, Any]) -> Path:
        session_id = _safe_temp_prefix(str(payload.get("session_id") or "no-session"))
        hook_id = _safe_temp_prefix(hook.id)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        return self._log_dir / session_id / f"{hook_id}.{ts}.log"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_result_file(path: Path) -> tuple[dict[str, Any], HookResultFileStatus]:
    """Parse ``CHRYS_HOOK_RESULT`` (``{}`` when absent, empty or invalid), and say which it was."""
    try:
        with path.open("rb") as result_file:
            raw = result_file.read(HOOK_RESULT_FILE_MAX_BYTES + 1)
    except OSError:
        return {}, "missing"
    if len(raw) > HOOK_RESULT_FILE_MAX_BYTES:
        logger.warning(
            "Hook result file %s exceeded the %d-byte limit and was ignored",
            path,
            HOOK_RESULT_FILE_MAX_BYTES,
        )
        return {}, "invalid"
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return {}, "invalid"
    if not text:
        return {}, "missing"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("Hook result file %s contained invalid JSON: %s", path, exc)
        return {}, "invalid"
    if not isinstance(parsed, dict):
        logger.warning("Hook result file %s must be a JSON object, got %s", path, type(parsed).__name__)
        return {}, "invalid"
    return parsed, "ok"


def _take_output(result: HookResult, stdout: BoundedCapture, stderr: BoundedCapture) -> None:
    out = stdout.snapshot()
    err = stderr.snapshot()
    result.stdout = out.text()
    result.stderr = err.text()
    result.stdout_dropped = out.dropped
    result.stderr_dropped = err.dropped


def _cleanup_invocation_files(invocation: HookInvocation) -> None:
    """Remove temp payload/result files for a completed non-detached invocation."""
    for path in (invocation.result_path, invocation.payload_path):
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def _safe_temp_prefix(value: str) -> str:
    """Return a filesystem-safe prefix for temp files derived from hook ids."""
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in value) or "hook"


def _set_owner_only(fd: int) -> None:
    """Best-effort 0600 permissions for hook temp files that may carry secrets."""
    try:
        os.fchmod(fd, 0o600)
    except AttributeError, OSError:
        return


def _open_owner_only_append(path: Path) -> BinaryIO:
    """Open *path* for append with best-effort owner-only permissions."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(path, flags, 0o600)
    try:
        _set_owner_only(fd)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return os.fdopen(fd, "ab")
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise
