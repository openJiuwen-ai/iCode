# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Subprocess-based skill script runner.

Implements the chrys :class:`~chrys.service.skills.model.SkillScriptRunner`
protocol using async subprocess execution, suitable for chrys's async
architecture.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.foundation.platform.output_capture import BoundedCapture, CapturedOutput, drain_process_pipes
from chrys.foundation.platform.process import (
    MissingWorkingDirectoryError,
    SubprocessStoppedError,
    managed_subprocess,
    wait_for_subprocess,
)
from chrys.foundation.platform.runtime_paths import (
    reorder_path_demoting_runtime,
    strip_python_runtime_overrides,
)
from chrys.foundation.platform.script_commands import build_script_command
from chrys.foundation.platform.win_console import preserve_console_mode
from chrys.foundation.text.model_json import model_path
from chrys.foundation.text.tool_output import process_carriage_returns, strip_ansi
from chrys.foundation.tool_result_metadata import PARTIAL_OUTPUT_LABEL
from chrys.service.skills.constants import DEFAULT_SCRIPT_RESULT_MAX_TOKENS
from chrys.service.tools.result_metadata import record_process_result, record_process_timeout, tool_error
from chrys.service.tools.spill import bound_process_output
from chrys.service.tools.workspace_paths import working_dir_missing_error

if TYPE_CHECKING:
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.service.skills.model import Skill, SkillScript

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Command building
# ---------------------------------------------------------------------------


def _interpreter_not_found(skill: Skill, script: SkillScript, *, reason: str | None = None) -> str:
    """The tool error for a script with no program to run it; *reason*, when given, is Chrys' own text."""
    message = f"Interpreter not found for script '{script.name}'."
    return tool_error(
        "interpreter_not_found",
        f"{message} {reason}" if reason else message,
        details={"skill_name": skill.name, "script_name": script.name},
    )


def _flag_name(key: str) -> str:
    """Return the CLI flag name for an ``args`` key."""
    return key if key.startswith("-") else f"--{key}"


def _args_to_flags(args: dict[str, Any]) -> list[str]:
    """Convert an args dict to CLI flags.

    Keys without leading dashes become ``--key``. Keys that already begin
    with ``-`` pass through verbatim, which lets callers express short and
    combined flags. Lists expand to ``--key v1 v2 ...`` (argparse
    ``nargs='+'`` style). Empty lists, ``None``, and ``False`` are all
    omitted — falsy "no value" inputs uniformly skip the flag.
    """
    flags: list[str] = []
    for key, value in args.items():
        flag = _flag_name(key)
        if isinstance(value, bool):
            if value:
                flags.append(flag)
        elif value is None:
            continue
        elif isinstance(value, list):
            if not value:
                continue
            flags.append(flag)
            flags.extend(str(v) for v in value)
        else:
            flags.append(flag)
            flags.append(str(value))
    return flags


# ---------------------------------------------------------------------------
# Script runner
# ---------------------------------------------------------------------------


def _clean_output(text: str) -> str:
    return process_carriage_returns(strip_ansi(text))


def _script_output(stdout: CapturedOutput, stderr: CapturedOutput, *, returncode: int | None = None) -> str:
    """Assemble a script's captured output and a nonzero exit code into one text."""
    parts: list[str] = []
    if stdout.seen:
        parts.append(stdout.text(_clean_output))
    if stderr.seen:
        parts.append(f"[stderr]\n{stderr.text(_clean_output)}")
    if returncode:
        parts.append(f"[exit_code: {returncode}]")
    return "\n".join(parts).strip()


class SubprocessScriptRunner:
    """Async skill script runner using subprocess execution.

    Satisfies the chrys :class:`~chrys.service.skills.model.SkillScriptRunner`
    protocol; ``ChrysSkillsProvider.run_skill_script`` invokes it with the
    full ``args``/``arguments``/``cwd`` signature.

    Resolves the script's absolute path from :attr:`SkillScript.full_path`,
    verifies it still lives under the owning :class:`Skill` directory, converts
    args to CLI flags, and runs via ``asyncio.create_subprocess_exec``.
    """

    def __init__(
        self,
        timeout: int = 600,
        runtime: SessionEnvironment | None = None,
        session_dir: Path | None = None,
    ) -> None:
        self._timeout = timeout
        self._runtime = runtime
        self._session_dir = session_dir

    async def __call__(
        self,
        skill: Skill,
        script: SkillScript,
        args: dict[str, Any] | list[str] | None = None,
        arguments: list[str] | None = None,
        cwd: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """Run a file-based skill script as a subprocess.

        Args:
            skill: The file-based skill that owns the script.
            script: The file-based script to run.  Its ``full_path`` is
                expected to be absolute (validated at discovery time and
                re-checked here before execution).
            args: Optional keyword arguments forwarded as CLI flags (--key value),
                or a legacy/list-style positional argument array. Dict-derived
                flags are appended after ``arguments``.
            arguments: Optional ordered argument tokens passed before dict-derived
                flags.
            cwd: Optional absolute working directory for the script. When
                ``None``, defaults to the primary working directory from
                ``SessionEnvironment`` (or the script's parent directory if no
                runtime is bound). Relative paths are rejected.
            max_tokens: Estimated-token budget for the returned output. Values
                below 100 are clamped to 100.

        Returns:
            Captured output, or an error message string.
        """
        if not skill.path or not str(skill.path).strip():
            return tool_error("skill_path_missing", f"Skill '{skill.name}' has no directory path.")

        if not script.full_path:
            return tool_error("script_path_missing", f"Script '{script.name}' has no resolved file path.")

        try:
            skill_dir = Path(skill.path).resolve()
            script_path = Path(script.full_path).resolve()
            script_path.relative_to(skill_dir)
        except ValueError:
            return tool_error(
                "script_path_outside_skill",
                f"Script path escapes skill directory: {script.full_path}",
                details={"skill_name": skill.name, "script_name": script.name, "script_path": script.full_path},
            )
        except OSError as e:
            return tool_error(
                "script_path_resolution_failed",
                f"Failed to resolve script path '{script.full_path}': {e}",
                details={"skill_name": skill.name, "script_name": script.name, "script_path": script.full_path},
            )

        # A working directory skill can live inside the deleted working directory: report
        # the directory, not its script, so the model stops instead of retrying.
        if cwd is None and self._runtime is not None and self._runtime.cwd and not os.path.isdir(self._runtime.cwd):
            return working_dir_missing_error(self._runtime.cwd)

        if not script_path.is_file():
            return tool_error(
                "script_file_not_found",
                f"Script file not found: {script_path}",
                details={"skill_name": skill.name, "script_name": script.name, "script_path": str(script_path)},
            )

        resolved_cwd = self._resolve_cwd(cwd, script_path)
        if resolved_cwd.startswith("Error: "):
            return resolved_cwd

        try:
            cmd = build_script_command(script_path)
        except FileNotFoundError as error:
            return _interpreter_not_found(skill, script, reason=error.strerror)
        positional_args = [str(a) for a in arguments] if arguments else []
        flag_args: list[str] = []
        if isinstance(args, list):
            positional_args = [str(a) for a in args] + positional_args
        elif isinstance(args, dict) and args:
            flag_args = _args_to_flags(args)
        if positional_args:
            cmd.extend(positional_args)
        if flag_args:
            cmd.extend(flag_args)

        # Force UTF-8 output from Python (and other interpreters that
        # respect these variables) so CJK text is never garbled.
        env = os.environ.copy()
        # Skills have no declarative env override, so keep PYTHONPATH for
        # shared helpers while still stripping PYTHONHOME to protect PyApp tools.
        strip_python_runtime_overrides(env, strip_pythonpath=False)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        reorder_path_demoting_runtime(env)

        budget = max(100, DEFAULT_SCRIPT_RESULT_MAX_TOKENS if max_tokens is None else max_tokens)
        stdout_capture = BoundedCapture()
        stderr_capture = BoundedCapture()
        try:
            # Skill scripts may invoke a TUI. ``preserve_console_mode`` guards
            # the outer chrys's Windows console mode against child mutation;
            # ``stdin=DEVNULL`` stops the child inheriting the outer console
            # input handle.  See chrys.foundation.platform.win_console for the full story.
            with preserve_console_mode():
                async with managed_subprocess(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=resolved_cwd,
                    env=env,
                ) as proc:
                    process_group_id = getattr(proc, "pid", None) if sys.platform != "win32" else None
                    await wait_for_subprocess(
                        drain_process_pipes(proc, stdout_capture, stderr_capture),
                        timeout=self._timeout,
                        process_group_id=process_group_id,
                    )

                captures = (stdout_capture.snapshot(), stderr_capture.snapshot())
                record_process_result(proc.returncode or 0)
                canonical = _script_output(*captures, returncode=proc.returncode) or "(no output)"
                return await bound_process_output(self._session_dir, "skill", canonical, budget, captures)

        except TimeoutError:
            record_process_timeout(self._timeout)
            error = tool_error(
                "script_timeout",
                f"Script '{script.name}' timed out after {self._timeout}s.",
                retryable=True,
                details={"skill_name": skill.name, "script_name": script.name, "timeout_seconds": self._timeout},
            )
            captures = (stdout_capture.snapshot(), stderr_capture.snapshot())
            partial = _script_output(*captures)
            if not partial:
                return error
            # The output stays out of the failure record.
            return await bound_process_output(
                self._session_dir, "skill", partial, budget, captures, lead=f"{error}\n{PARTIAL_OUTPUT_LABEL}\n"
            )
        except SubprocessStoppedError:
            return tool_error(
                "process_stopped",
                f"Script '{script.name}' entered stopped state and was terminated.",
                details={"skill_name": skill.name, "script_name": script.name},
            )
        except MissingWorkingDirectoryError as e:
            if cwd is None:
                return working_dir_missing_error(e.path)
            return tool_error(
                "invalid_cwd", f"'cwd' is not an existing directory: {model_path(e.path)}", details={"cwd": e.path}
            )
        except FileNotFoundError:
            return _interpreter_not_found(skill, script)
        except OSError as e:
            return tool_error(
                "script_execution_failed",
                f"Failed to execute script '{script.name}': {e}",
                details={"skill_name": skill.name, "script_name": script.name},
            )

    def _resolve_cwd(self, cwd: str | None, script_path: Path) -> str:
        """Resolve the working directory for a script run.

        Returns the cwd string on success, or an ``"Error: ..."`` message on
        failure (caller short-circuits and returns it to the agent).

        Resolution order:
            1. Explicit ``cwd`` from the agent — must be absolute and must
               exist as a directory.
            2. ``self._runtime.cwd`` if a ``SessionEnvironment`` was bound.
            3. Legacy fallback: ``script_path.parent`` (for standalone runner
               use without a runtime).
        """
        if cwd is not None:
            if not os.path.isabs(cwd):
                return tool_error(
                    "invalid_cwd", f"'cwd' must be an absolute path, got: {model_path(cwd)}", details={"cwd": cwd}
                )
            if not os.path.isdir(cwd):
                return tool_error(
                    "invalid_cwd", f"'cwd' is not an existing directory: {model_path(cwd)}", details={"cwd": cwd}
                )
            return cwd
        if self._runtime is not None:
            return self._runtime.cwd
        return str(script_path.parent)
