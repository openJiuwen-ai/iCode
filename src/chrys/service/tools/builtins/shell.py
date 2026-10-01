# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shell execution tools — instance-based tools that hold SessionEnvironment.

Uses Chrys kernel instance tool support: @tool on class methods binds to the
instance via the descriptor protocol, giving each tool access to self._runtime
for working directory, platform info, etc.

On Unix, commands run with stdout/stderr attached to a PTY and
``TERM=dumb`` so programs see terminal output streams but suppress most ANSI
formatting.  Stdin is ``/dev/null`` and the PTY is not installed as a
controlling terminal, so interactive terminal reads fail instead of triggering
job-control stops.  On Windows, commands run with stdout/stderr pipes (no PTY
available).

Output is automatically truncated to ``max_tokens`` with a 1:2 head:tail
ratio.  Every backend first assembles the same canonical result shape; when a
session directory is available, the complete cleaned result is spilled there
before the bounded view is returned.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import os
import struct
import sys
import time
from contextlib import AsyncExitStack, suppress
from contextvars import ContextVar
from typing import TYPE_CHECKING, Annotated, Any, cast

from chrys.foundation.platform.paths import resolve_workspace_path
from chrys.foundation.platform.process import (
    SubprocessStoppedError,
    decode_subprocess_output,
    kill_process_group,
    kill_process_session,
    managed_subprocess,
    wait_for_subprocess,
)
from chrys.foundation.platform.pty_output import PtyOutputProtocol
from chrys.foundation.platform.runtime_paths import reorder_path_demoting_runtime
from chrys.foundation.platform.win_console import preserve_console_mode
from chrys.foundation.text.tool_output import (
    process_carriage_returns as _process_carriage_returns,
)
from chrys.foundation.text.tool_output import (
    strip_ansi as _strip_ansi,
)
from chrys.foundation.text.tool_output import (
    truncate_output as _truncate_output,
)
from chrys.foundation.tool_result_metadata import (
    SHELL_EXIT_CODE_METADATA_KEY,
    SHELL_TIMED_OUT_METADATA_KEY,
    SHELL_TIMEOUT_SECONDS_METADATA_KEY,
    TOOL_ERRORED_METADATA_KEY,
)
from chrys.service.mutations.trace import shell_trace_argv
from chrys.service.tools.kinds import KIND_SHELL, set_tool_kind, tool
from chrys.service.tools.result_metadata import tool_error
from chrys.service.tools.spill import truncate_with_spill

if sys.platform != "win32":
    import fcntl
    import pty
    import select
    import signal
    import termios

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.foundation.platform import ShellInfo
    from chrys.service.tools.builtins.shell_filter import ShellCommandFilter

logger = logging.getLogger(__name__)

_DEFAULT_MAX_TOKENS = 8000

SHELL_REASON_DESCRIPTION = (
    "Brief user-facing explanation of why this command needs to run (1-2 sentences). "
    "Write it in the same language as the user's latest prompt whenever possible. "
    "This reason is shown to the user and recorded in audit logs, so state the command's intent plainly "
    "without private reasoning or hidden context."
)

# ---------------------------------------------------------------------------
# Shell execution context
# ---------------------------------------------------------------------------

shell_progress_callback: ContextVar[Callable[[list[str]], Awaitable[None]] | None] = ContextVar(
    "shell_progress_callback", default=None
)
"""Contextvar for streaming shell output lines to the TUI.

Set by ``ToolEventMiddleware`` before shell tool execution.  The callback
receives batches of cleaned output lines (ANSI-stripped, \\r handled).
"""

shell_result_metadata: ContextVar[dict[str, object] | None] = ContextVar("shell_result_metadata", default=None)
"""Mutable metadata buffer set by ``ToolEventMiddleware`` for shell calls."""


def _record_shell_exit_code(returncode: int) -> None:
    metadata = shell_result_metadata.get(None)
    if metadata is not None:
        metadata[SHELL_EXIT_CODE_METADATA_KEY] = returncode


def _record_shell_timeout(timeout: int) -> None:
    metadata = shell_result_metadata.get(None)
    if metadata is not None:
        metadata[SHELL_TIMED_OUT_METADATA_KEY] = True
        metadata[SHELL_TIMEOUT_SECONDS_METADATA_KEY] = timeout


def _record_shell_error() -> None:
    metadata = shell_result_metadata.get(None)
    if metadata is not None:
        metadata[TOOL_ERRORED_METADATA_KEY] = True


_PROGRESS_THROTTLE_INTERVAL = 0.2
"""Minimum seconds between progress emissions to avoid flooding the TUI."""

_PTY_DRAIN_GRACE = 2.0
"""Seconds to let the PTY reader catch up before the last slave fd is closed."""


async def _await_pty_master_drained(master_fd: int, *, grace: float = _PTY_DRAIN_GRACE) -> None:
    """Yield until the PTY reader has taken everything the tty still holds.

    macOS drops whatever is still buffered in a tty the moment its last slave fd
    closes, so releasing the parent's copy the instant the child exits discards
    the output of any command whose reader callback has not been scheduled yet —
    a busy event loop turns ``echo hello`` into a bare ``[exit_code: 0]``.  The
    child has already exited by the time this runs, so nothing new can arrive:
    once the master stops reporting readable, the reader holds everything that
    was written.  ``grace`` bounds the wait so a reader paused by flow control
    cannot stall the command.
    """
    # Unlike select(), poll() accepts descriptors above FD_SETSIZE (1024).
    poller = select.poll()
    poller.register(master_fd, select.POLLIN)
    deadline = time.monotonic() + grace
    while True:
        try:
            events = poller.poll(0)
            if not events or any(event & select.POLLNVAL for _, event in events):
                return
        except OSError:
            return
        if time.monotonic() >= deadline:
            return
        await asyncio.sleep(0)


def _kill_pty_process(
    proc: asyncio.subprocess.Process,
    process_group_id: int | None = None,
    process_session_id: int | None = None,
) -> None:
    """Kill a PTY subprocess without signaling Chrys's own process group."""
    if process_session_id is not None and kill_process_session(process_session_id):
        return
    if process_group_id is not None and kill_process_group(process_group_id):
        return
    if proc.returncode is not None or proc.pid is None:
        return

    posix_os = cast(Any, os)
    with suppress(ProcessLookupError, PermissionError, OSError):
        child_pgid = posix_os.getpgid(proc.pid)
        parent_pgid = posix_os.getpgrp()
        if child_pgid == proc.pid and child_pgid != parent_pgid:
            posix_os.killpg(child_pgid, signal.SIGKILL)
            return

    with suppress(ProcessLookupError, OSError):
        os.kill(proc.pid, signal.SIGKILL)


def _format_timeout_result(timeout: int, output: str = "", stderr: str = "") -> str:
    """Assemble the canonical timeout result with captured output."""
    message = f"Error: command timed out after {timeout} seconds."
    body = _process_carriage_returns(_strip_ansi(output)) if output else ""
    stderr_clean = _process_carriage_returns(_strip_ansi(stderr)) if stderr else ""
    if not body and not stderr_clean:
        return message

    parts = [message]
    if body:
        parts.append(f"[partial output]\n{body}")
    if stderr_clean:
        parts.append(f"[partial stderr]\n{stderr_clean}")
    return "\n".join(parts)


_PIPEFAIL_SHELLS = frozenset({"bash", "zsh", "git_bash"})
"""Shells launched with ``-o pipefail`` so pipelines propagate upstream failures.

Without pipefail a pipeline's status is its last stage's, so
``make 2>&1 | tail -20`` reports success even when the build failed.
Deliberately conservative: a basename cannot prove capability (a ``ksh``
may be ksh88 without pipefail, ``sh`` may be an old dash), and a wrong
guess makes the shell reject its argv — every command would fail.  Shells
not listed here keep their native pipeline semantics unchanged.
"""


def _pipefail_args(shell: ShellInfo) -> tuple[str, ...]:
    """Return extra launch args enabling pipefail for shells known to support it."""
    return ("-o", "pipefail") if shell.name in _PIPEFAIL_SHELLS else ()


def _shell_argv_variants(shell: ShellInfo, command: str) -> list[tuple[str, ...]]:
    """Launch argvs to try in order: traced first (when armed), then bare.

    The mutation pipeline hands an fsatrace wrapper prefix over via
    contextvar; wrapping happens at
    argv level — no shell-quoting hazards.  Spawn failure of the traced
    variant must degrade to the bare one: tracing may never change
    user-visible shell behavior.
    """
    base = (shell.path, *_pipefail_args(shell), *shell.args, command)
    prefix = shell_trace_argv.get()
    if prefix:
        return [(*prefix, *base), base]
    return [base]


def _build_subprocess_env() -> dict[str, str]:
    """Build a colour-suppressed environment for shell subprocesses.

    Shared by both the PTY (Unix) and pipe (Windows) execution paths.
    """
    env = os.environ.copy()
    env["TERM"] = "dumb"
    env["CHRYS"] = "1"
    env["NO_COLOR"] = "1"
    # Force UTF-8 output from Python subprocesses (and other tools
    # that respect these variables) so CJK text is never garbled.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Prevent git (and other tools) from launching a pager, which
    # would block forever waiting for interactive input.
    env["GIT_PAGER"] = "cat"
    env["PAGER"] = "cat"
    # Suppress colour-forcing variables
    env.pop("FORCE_COLOR", None)
    env.pop("COLORTERM", None)
    env.pop("CLICOLOR_FORCE", None)
    # Shell commands intentionally mirror the user's environment more closely
    # than managed hook/skill/MCP subprocesses, so keep PYTHONHOME/PYTHONPATH.
    reorder_path_demoting_runtime(env)
    return env


_POWERSHELL_UTF8_PREAMBLE = "\n".join(
    (
        "$__chrysUtf8 = [System.Text.UTF8Encoding]::new($false)",
        "try {",
        "  [Console]::InputEncoding = $__chrysUtf8",
        "  [Console]::OutputEncoding = $__chrysUtf8",
        "} catch { }",
        "$OutputEncoding = $__chrysUtf8",
    )
)


def _prepare_pipe_command(command: str, shell: ShellInfo) -> str:
    """Adjust commands for pipe-based shells before execution."""
    if shell.name not in ("pwsh", "powershell"):
        return command
    return f"{_POWERSHELL_UTF8_PREAMBLE}\n{command}"


SHELL_TOOL_NAMES = frozenset(
    {"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh", "csh", "pwsh", "powershell", "cmd", "git_bash"}
)
"""All possible names for shell command tools.

Shell command tools are named after their shell (e.g. ``zsh``, ``pwsh``, ``powershell``)
by ``ShellTools.tools()``.  This set covers all shells that
``platform.py`` can detect.  Used by TUI renderers for tool-specific display.
"""


class ShellTools:
    """Instance-based shell tools backed by SessionEnvironment.

    Each @tool-decorated method is a FunctionTool that binds to the instance,
    so it can access self._runtime for cwd and platform info.

    Usage::

        runtime = SessionEnvironment.capture()
        shell = ShellTools(runtime)
        tools = shell.tools()  # → [shell.<detected shell name>]
    """

    def __init__(
        self,
        runtime: SessionEnvironment,
        command_filter: ShellCommandFilter | None = None,
        shell: ShellInfo | None = None,
        session_dir: Path | None = None,
    ) -> None:
        self._runtime = runtime
        self._filter = command_filter
        self._shell = shell or runtime.platform.shell
        self._session_dir = session_dir

    @property
    def shell(self) -> ShellInfo:
        """Return the shell configuration used by this tool instance."""
        return self._shell

    async def _bound_result(self, canonical: str, budget: int) -> str:
        plain = _truncate_output(canonical, budget)
        if plain == canonical:
            return canonical
        return await truncate_with_spill(self._session_dir, "shell", canonical, budget)

    def tools(self) -> list:
        """Return all tool instances bound to this object.

        Dynamically sets descriptions to include detected shell info so the
        LLM knows which shell/version it is writing commands for.
        """
        shell = self._shell
        shell_label = f"{shell.name} {shell.version}".strip() if shell.version else shell.name
        if shell.name in ("pwsh", "powershell"):
            output_guidance = (
                "For large expected output, use PowerShell-native limiting/search commands "
                "such as Select-Object -First/-Last, Select-String, or Get-Content -Tail. "
                "To read a file's content, use the read_file tool instead of Get-Content (if present). "
            )
        elif shell.name in _PIPEFAIL_SHELLS:
            output_guidance = (
                "For large expected output, pipe through tail -n N or grep. "
                "Pipelines fail when any stage fails (pipefail is enabled by default). "
                "Avoid head and grep -q/-m in pipelines — they exit early and can kill "
                "the upstream command with SIGPIPE, often causing the pipeline to exit "
                "with status 141; if that happens, rerun without the early-exiting "
                "stage to see the real status. "
            )
        else:
            output_guidance = "For large expected output, pipe through head, tail, or grep. "

        execute = self.execute
        execute.name = shell.name
        set_tool_kind(execute, KIND_SHELL)
        description = (
            f"Execute a command in {shell_label} ({shell.path}). "
            "Commands run with a configurable timeout (default 30s). "
            "Consider a larger timeout for builds, tests, downloads, and other long-running commands. "
            "This call waits for the command's result. Calls in the same response are dispatched "
            "together with no guaranteed execution order. The calling agent resumes only after the "
            "entire batch returns. Calls that depend on this result or could conflict through shared "
            "state must go in a later response. "
            "No asynchronous task handle is provided. "
            "Output is truncated to max_tokens (default 8000) preserving the first "
            "and last portions with a truncation notice in the middle; when possible, "
            "the complete output is saved under the current session directory. "
            f"{output_guidance}"
            "Set reason to a brief user-facing intent in the same language as the user's latest prompt; "
            "it is shown to the user and audit logs. "
            "Use timeout, working_dir, and max_tokens to control runtime, cwd, and output size. "
            "Do NOT use the shell to read/write/edit files or search code — "
            "use the dedicated read_file, write_file, edit_file, grep, and glob tools instead. "
            "Do NOT run interactive/TUI programs (vim, nano, less, htop, top) "
            "or commands requiring user input (passwords, y/n prompts). "
            "Use non-interactive flags when available "
            "(e.g. --yes, --no-pager, --no-input, DEBIAN_FRONTEND=noninteractive)."
        )
        platform_info = self._runtime.platform
        if shell.name == "powershell":
            description += (
                " IMPORTANT: This is Windows PowerShell 5.x, NOT bash/sh/cmd. "
                "You MUST use PowerShell syntax — do NOT write bash/Unix commands. "
                "Platform: Windows — paths use backslash (C:\\Users\\name\\project), "
                "home dir is $env:USERPROFILE, "
                "temp dir is $env:TEMP, program files at $env:ProgramFiles. "
                "Command chaining: use ';' to separate commands — '&&' and '||' "
                "are NOT supported and will cause syntax errors. "
                "For conditional execution: "
                "if ($LASTEXITCODE -eq 0) { next-command }. "
                "Environment variables: $env:VAR (NOT $VAR or %VAR%). "
                "Comparison operators: -eq, -ne, -gt, -lt, -ge, -le, -like, -match "
                "(NOT ==, !=, >, <). "
                "Logical operators: -and, -or, -not/! (NOT &&, ||). "
                "Null output: | Out-Null or > $null (NOT > /dev/null). "
                "Line continuation: backtick ` at end of line (NOT \\). "
                'Here-strings: @" must end its line, "@ must start its own line '
                "(NOT << 'EOF'). "
                "Prefer full cmdlet names over aliases: Get-ChildItem, Select-String, "
                "Remove-Item -Recurse -Force, Set-Location, Copy-Item, and Move-Item. "
                "If you must use Get-Content in Windows PowerShell 5.x, specify "
                "-Encoding UTF8; otherwise BOM-less UTF-8 can be decoded as the ANSI "
                "code page and non-ASCII text can be corrupted. "
                "Quoting: double quotes interpolate variables ($var expands), "
                "single quotes are literal. "
                "Exit codes: check $LASTEXITCODE for native commands, "
                "$? for cmdlet success. "
                "Examples: "
                "Get-ChildItem C:\\project\\src -Filter *.py -Recurse. "
                "Select-String -Path .\\*.log -Pattern 'error'. "
                "$env:PATH -split ';'. "
                "Test-Path C:\\Users\\name\\file.txt. "
                "Join-Path $env:USERPROFILE 'Documents' 'project'."
            )
        elif shell.name == "pwsh":
            if platform_info.is_windows:
                platform_note = (
                    "Platform: Windows — paths use backslash "
                    "(C:\\Users\\name\\project) or forward slash. "
                    "Home dir: $HOME (e.g. C:\\Users\\name), "
                    "temp dir: $env:TEMP, program files: $env:ProgramFiles. "
                    "Path separator: ';'."
                )
                path_examples = (
                    "Get-ChildItem C:\\project\\src -Filter *.py -Recurse. "
                    "Join-Path $HOME 'Documents' 'project'. "
                    "Test-Path C:\\Users\\name\\file.txt. "
                    "$env:PATH -split ';'."
                )
            elif platform_info.is_macos:
                platform_note = (
                    "Platform: macOS — paths use forward slash "
                    "(/Users/name/project). "
                    "Home dir: $HOME (e.g. /Users/name), "
                    "temp dir: $env:TMPDIR or /tmp. "
                    "Path separator: ':'."
                )
                path_examples = (
                    "Get-ChildItem ~/project/src -Filter *.py -Recurse. "
                    "Join-Path $HOME 'Documents' 'project'. "
                    "Test-Path /usr/local/bin/python3. "
                    "$env:PATH -split ':'."
                )
            else:
                platform_note = (
                    "Platform: Linux — paths use forward slash "
                    "(/home/name/project). "
                    "Home dir: $HOME (e.g. /home/name), "
                    "temp dir: /tmp. "
                    "Path separator: ':'."
                )
                path_examples = (
                    "Get-ChildItem ~/project/src -Filter *.py -Recurse. "
                    "Join-Path $HOME 'Documents' 'project'. "
                    "Test-Path /usr/bin/python3. "
                    "$env:PATH -split ':'."
                )
            description += (
                " IMPORTANT: This is PowerShell 7+ (cross-platform), NOT bash/sh. "
                "You MUST use PowerShell syntax — do NOT write bash/Unix commands. "
                f"{platform_note} "
                "Command chaining: '&&' and '||' are supported as pipeline chain "
                "operators (success/failure), ';' separates independent statements. "
                "Environment variables: $env:VAR (NOT $VAR or export VAR=val). "
                "Comparison operators: -eq, -ne, -gt, -lt, -ge, -le, -like, -match "
                "(NOT ==, !=, >, <). "
                "Logical operators: -and, -or, -not/! (do NOT use && or || as logical operators). "
                "Null output: | Out-Null or > $null (NOT > /dev/null). "
                "Line continuation: backtick ` at end of line (NOT \\). "
                'Here-strings: @" must end its line, "@ must start its own line '
                "(NOT << 'EOF'). "
                "Ternary: $cond ? $trueVal : $falseVal. "
                "Null-coalescing: $a ?? $default. "
                "Prefer full cmdlet names over aliases: Get-ChildItem, Select-String, "
                "Remove-Item -Recurse -Force, Set-Location, Copy-Item, and Move-Item. "
                "Quoting: double quotes interpolate variables ($var expands), "
                "single quotes are literal. "
                "Exit codes: check $LASTEXITCODE for native commands, "
                "$? for cmdlet success. "
                f"Examples: {path_examples}"
            )
        execute.description = description

        tools = [execute]
        if self._runtime.working_dirs:
            list_dirs = self.list_workspace_dirs
            set_tool_kind(list_dirs, KIND_SHELL)
            tools.append(list_dirs)
        return tools

    @tool(kind=KIND_SHELL)
    async def execute(
        self,
        command: Annotated[str, "The command to execute in the platform's default shell."],
        reason: Annotated[str, SHELL_REASON_DESCRIPTION],
        timeout: Annotated[int, "Timeout in seconds. Default: 30."] = 30,
        working_dir: Annotated[
            str | None,
            "Working directory for the command. Relative paths are resolved against the session workspace. "
            "Default: session workspace directory.",
        ] = None,
        max_tokens: Annotated[
            int,
            "Token budget for command output. Large outputs are truncated to fit.",
        ] = _DEFAULT_MAX_TOKENS,
    ) -> str:
        """Execute a shell command on the current platform."""
        budget = max(100, max_tokens)
        # Validate command against filter policy (if configured)
        if self._filter is not None:
            result = self._filter.validate(command)
            if not result.allowed:
                _record_shell_error()
                return tool_error(
                    "command_blocked",
                    f"command blocked — {result.reason}",
                    details={"command": command, "reason": result.reason},
                )

        shell = self._shell
        cwd = resolve_workspace_path(working_dir, base_cwd=self._runtime.cwd) if working_dir else self._runtime.cwd

        try:
            if sys.platform == "win32":
                # Windows: snapshot outer TUI's console mode so a child TUI
                # (e.g. the agent running ``uv run chrys``) cannot leave the
                # outer chrys without mouse/keyboard input after it exits.
                # See ``preserve_console_mode`` for the full rationale.
                with preserve_console_mode():
                    stdout_text, returncode, stderr_text = await self._execute_pipe(command, shell, cwd, timeout)
                body = _process_carriage_returns(_strip_ansi(stdout_text))
                stderr_clean = _process_carriage_returns(_strip_ansi(stderr_text)) if stderr_text else ""
                exit_suffix = f"[exit_code: {returncode}]"
                parts: list[str] = []
                if body:
                    parts.append(body)
                if stderr_clean:
                    parts.append(f"[stderr]\n{stderr_clean}")
                parts.append(exit_suffix)
                _record_shell_exit_code(returncode)
                return await self._bound_result("\n".join(parts), budget)
            merged, returncode = await self._execute_pty(command, shell, cwd, timeout)
            body = _process_carriage_returns(_strip_ansi(merged))
            suffix = f"[exit_code: {returncode}]"
            parts = [body, suffix] if body else [suffix]
            _record_shell_exit_code(returncode)
            return await self._bound_result("\n".join(parts), budget)
        except _Timeout as exc:
            _record_shell_timeout(timeout)
            canonical = _format_timeout_result(timeout, exc.output, exc.stderr)
            return await self._bound_result(canonical, budget)
        except SubprocessStoppedError:
            _record_shell_error()
            return tool_error("process_stopped", "command entered stopped state and was terminated.")
        except FileNotFoundError:
            _record_shell_error()
            return tool_error("shell_not_found", f"shell not found at {shell.path}", details={"shell_path": shell.path})
        except Exception as e:
            _record_shell_error()
            return tool_error(
                "command_execution_failed", f"failed to execute command — {e}", details={"command": command}
            )

    # ------------------------------------------------------------------
    # Execution backends
    # ------------------------------------------------------------------

    async def _execute_pty(self, command: str, shell: ShellInfo, cwd: str, timeout: int | float) -> tuple[str, int]:
        """Run *command* in a PTY (Unix only). Returns ``(output, returncode)``."""
        master, slave = pty.openpty()
        try:
            # Set terminal size to 80x24
            size = struct.pack("HHHH", 24, 80, 0, 0)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, size)

            # Non-blocking master
            posix_os = cast(Any, os)
            flags = fcntl.fcntl(master, fcntl.F_GETFL)
            fcntl.fcntl(master, fcntl.F_SETFL, flags | posix_os.O_NONBLOCK)

            env = _build_subprocess_env()

            def _setup_pty() -> None:
                posix_os.setsid()

            # fsatrace (when armed) execs between the PTY and the shell:
            # stdio fds are inherited, ``setsid`` applies to fsatrace, and
            # the shell inherits its process group — draining and
            # group-kill behave exactly as untraced.
            argv_variants = _shell_argv_variants(shell, command)
            proc: asyncio.subprocess.Process | None = None
            for index, argv in enumerate(argv_variants):
                try:
                    proc = await asyncio.create_subprocess_exec(
                        *argv,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=slave,
                        stderr=slave,
                        cwd=cwd,
                        env=env,
                        preexec_fn=_setup_pty,
                    )
                    break
                except OSError:
                    if index == len(argv_variants) - 1:
                        raise
                    logger.debug("Traced shell spawn failed; degrading to untraced", exc_info=True)
            if proc is None:
                raise RuntimeError("No shell process was created by the configured spawn variants.")
        except BaseException:
            # Clean up both fds if subprocess creation fails
            os.close(slave)
            os.close(master)
            raise

        # Keep the parent's ``slave`` fd open until the drain has finished.  On
        # macOS, if every slave fd closes (child exit + the parent closing its
        # copy) before the async reader has drained the master, the PTY can
        # reach EOF and the child's final output is silently dropped.  Holding
        # the slave open here keeps the master from reaching EOF until we close
        # it ourselves — after the child has exited and its output is read.
        transport: asyncio.BaseTransport | None = None
        process_group_id = proc.pid
        process_session_id = proc.pid
        cleanup_process_group = True
        slave_closed = False

        def _release_slave() -> None:
            nonlocal slave_closed
            if not slave_closed:
                slave_closed = True
                with suppress(OSError):
                    os.close(slave)

        try:
            # Async reader on PTY master (same pattern as TUI shell)
            reader = asyncio.StreamReader()
            loop = asyncio.get_running_loop()
            transport, _ = await loop.connect_read_pipe(
                lambda: PtyOutputProtocol(reader),
                os.fdopen(master, "rb", 0),
            )

            output = bytearray()

            async def _drain() -> None:
                progress_cb = shell_progress_callback.get(None)
                if progress_cb is None:
                    # Original path — no progress streaming
                    while chunk := await reader.read(64 * 1024):
                        output.extend(chunk)
                    return

                # Streaming path — extract lines and emit progress
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                line_buf = ""
                pending: list[str] = []
                last_emit = time.monotonic()
                # The callback below may still be busy when the program ends.  What
                # arrived meanwhile is read all the same: PtyOutputProtocol ends the
                # output after it, however the master reported the end.
                while chunk := await reader.read(64 * 1024):
                    output.extend(chunk)
                    text = decoder.decode(chunk)
                    line_buf += text
                    while "\n" in line_buf:
                        line, line_buf = line_buf.split("\n", 1)
                        cleaned = _strip_ansi(line.rstrip("\r"))
                        # Preserve completed blank lines.  The tool card
                        # renders the streamed progress after completion,
                        # so dropping empty lines here changes visible
                        # command output compared to a real terminal.
                        pending.append(cleaned)
                    now = time.monotonic()
                    if pending and now - last_emit >= _PROGRESS_THROTTLE_INTERVAL:
                        await progress_cb(pending)
                        pending = []
                        last_emit = now
                # Flush remaining
                line_buf += decoder.decode(b"", final=True)
                if line_buf.strip():
                    remaining = _strip_ansi(line_buf.rstrip("\r\n"))
                    if remaining:
                        pending.append(remaining)
                if pending:
                    await progress_cb(pending)

            async def _wait_then_release() -> None:
                # Once the child exits, release the parent's slave fd so the PTY
                # master reaches EOF *after* the child's buffered output has been
                # flushed, letting _drain() observe EOF and finish.  Waiting for
                # the reader to catch up first is what makes that flush real: see
                # _await_pty_master_drained.
                try:
                    await proc.wait()
                    await _await_pty_master_drained(master)
                finally:
                    _release_slave()

            await wait_for_subprocess(
                asyncio.gather(_wait_then_release(), _drain()),
                timeout=timeout,
                process_group_id=process_group_id,
                process_session_id=process_session_id,
            )
            returncode = proc.returncode if proc.returncode is not None else -1
            cleanup_process_group = False
            return decode_subprocess_output(output), returncode
        except TimeoutError:
            raise _Timeout(output=decode_subprocess_output(output)) from None
        finally:
            # Release the parent's slave fd on every exit path (success,
            # timeout, cancellation, stopped-state, or error) so it is never
            # leaked and the master reader can observe EOF.
            _release_slave()
            # Kill the process if it's still running (timeout, cancellation,
            # or any other early exit).  Without this, CancelledError from
            # user interrupt leaves the subprocess alive in the background.
            if cleanup_process_group:
                _kill_pty_process(proc, process_group_id, process_session_id)
            if transport is not None:
                transport.close()

    async def _execute_pipe(
        self, command: str, shell: ShellInfo, cwd: str, timeout: int | float
    ) -> tuple[str, int, str]:
        """Run *command* with pipes (Windows fallback). Returns ``(stdout, returncode, stderr)``."""
        env = _build_subprocess_env()
        command = _prepare_pipe_command(command, shell)
        argv_variants = _shell_argv_variants(shell, command)

        async with AsyncExitStack() as stack:
            proc: asyncio.subprocess.Process | None = None
            for index, argv in enumerate(argv_variants):
                try:
                    proc = await stack.enter_async_context(
                        managed_subprocess(
                            *argv,
                            # stdin=DEVNULL so the child's STD_INPUT_HANDLE is not the
                            # outer chrys's console input handle on Windows.  Without this,
                            # a grandchild TUI (e.g. ``uv run chrys`` inside PowerShell)
                            # can call ``SetConsoleMode`` on the inherited handle and wipe
                            # the outer TUI's mouse/keyboard flags.  Harmless on Unix.
                            stdin=asyncio.subprocess.DEVNULL,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            cwd=cwd,
                            env=env,
                        )
                    )
                    break
                except OSError:
                    # Spawn failure surfaces at context entry, before the
                    # cleanup registers — safe to retry the bare argv.
                    if index == len(argv_variants) - 1:
                        raise
                    logger.debug("Traced shell spawn failed; degrading to untraced", exc_info=True)
            if proc is None:
                raise RuntimeError("No shell process was created by the configured spawn variants.")
            capture = _PipeOutputCapture()
            try:
                progress_cb = shell_progress_callback.get(None)
                process_group_id = getattr(proc, "pid", None) if sys.platform != "win32" else None
                stdout, stderr = await wait_for_subprocess(
                    self._stream_pipe(proc, progress_cb, capture=capture),
                    timeout=timeout,
                    process_group_id=process_group_id,
                )
            except TimeoutError:
                raise _Timeout(
                    output=decode_subprocess_output(capture.stdout_bytes()),
                    stderr=decode_subprocess_output(capture.stderr_bytes()),
                ) from None

            returncode = proc.returncode if proc.returncode is not None else -1
            return (
                decode_subprocess_output(stdout) if stdout else "",
                returncode,
                decode_subprocess_output(stderr) if stderr else "",
            )

    @staticmethod
    async def _stream_pipe(
        proc: asyncio.subprocess.Process,
        progress_cb: Callable[[list[str]], Awaitable[None]] | None,
        *,
        capture: _PipeOutputCapture | None = None,
    ) -> tuple[bytes, bytes]:
        """Read stdout line-by-line with progress, read stderr fully."""
        capture = capture or _PipeOutputCapture()
        pending: list[str] = []
        last_emit = time.monotonic()

        async def _drain_stdout() -> None:
            nonlocal last_emit
            if proc.stdout is None:
                raise RuntimeError("The shell process has no stdout pipe.")
            line_buf = bytearray()
            while chunk := await proc.stdout.read(64 * 1024):
                capture.stdout_parts.append(chunk)
                if progress_cb is None:
                    continue
                line_buf.extend(chunk)
                while b"\n" in line_buf:
                    newline_idx = line_buf.index(0x0A)
                    raw_line = bytes(line_buf[:newline_idx]).rstrip(b"\r")
                    del line_buf[: newline_idx + 1]
                    pending.append(_strip_ansi(decode_subprocess_output(raw_line)))
                now = time.monotonic()
                if pending and now - last_emit >= _PROGRESS_THROTTLE_INTERVAL:
                    await progress_cb(list(pending))
                    pending.clear()
                    last_emit = now
            if progress_cb is not None:
                if line_buf.strip():
                    remaining = _strip_ansi(decode_subprocess_output(bytes(line_buf).rstrip(b"\r\n")))
                    if remaining:
                        pending.append(remaining)
                if pending:
                    await progress_cb(list(pending))
                    pending.clear()

        async def _drain_stderr() -> None:
            if proc.stderr is None:
                raise RuntimeError("The shell process has no stderr pipe.")
            while chunk := await proc.stderr.read(64 * 1024):
                capture.stderr_parts.append(chunk)

        await asyncio.gather(_drain_stdout(), _drain_stderr(), proc.wait())
        return capture.stdout_bytes(), capture.stderr_bytes()

    # ------------------------------------------------------------------
    # Other tools
    # ------------------------------------------------------------------

    @tool
    def list_workspace_dirs(self) -> str:
        """List all working directories in the current workspace.

        Returns directory paths with their labels and primary status.
        Useful for understanding the project structure across multiple repositories.
        """
        if not self._runtime.working_dirs:
            return f"No workspace active. Current directory: {self._runtime.cwd}"
        lines = [f"Workspace directories (cwd: {self._runtime.cwd}):"]
        for wd in self._runtime.working_dirs:
            marker = " (primary)" if wd.is_primary else ""
            label = f" [{wd.label}]" if wd.label else ""
            exists = " ok" if wd.exists else " MISSING"
            lines.append(f"  {wd.path}{label}{marker}{exists}")
        return "\n".join(lines)


class _PipeOutputCapture:
    """Mutable byte buffers shared with the timeout handler."""

    def __init__(self) -> None:
        self.stdout_parts: list[bytes] = []
        self.stderr_parts: list[bytes] = []

    def stdout_bytes(self) -> bytes:
        return b"".join(self.stdout_parts)

    def stderr_bytes(self) -> bytes:
        return b"".join(self.stderr_parts)


class _Timeout(Exception):
    """Internal sentinel — keeps timeout handling out of the backends."""

    def __init__(self, output: str = "", stderr: str = "") -> None:
        super().__init__()
        self.output = output
        self.stderr = stderr
