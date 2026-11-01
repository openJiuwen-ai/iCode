# Copyright (c) 2024 Anthropic, PBC
# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from the Model Context Protocol Python SDK and Microsoft Agent Framework (MIT License; see NOTICE).

"""Stdio MCP transport and diagnostics."""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import inspect
import io
import json
import logging
import ntpath
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import TracebackType
from typing import Any, TextIO, cast

from chrys.foundation.i18n.formatting import sanitize_legacy_block, sanitize_legacy_scalar
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.platform.runtime_paths import (
    is_windows_store_python_stub,
    path_env_key,
    reorder_path_demoting_runtime,
    strip_python_runtime_overrides,
    which_excluding_runtime,
)
from chrys.foundation.text.tool_output import process_carriage_returns, strip_ansi
from chrys.foundation.util.env_templates import COMMAND_HANDLE_RE, command_handles_in
from chrys.service.mcp._tool_mixins import _NoPrePagePingMixin, _StructuredContentFallbackMixin
from chrys.service.mcp.owned import MCPStdioTool

logger = logging.getLogger(__name__)

# Cap how many non-JSON banner lines we keep per stdio server so a chatty
# logger can't unboundedly grow our diagnostic state.
MAX_BANNER_LINES_CAPTURED = 10

# Cap each captured banner line's length so a single very long log line
# (e.g. a stack trace serialized to one row) can't blow up an error
# message or notification.
MAX_BANNER_LINE_CHARS = 200

# Keep only a bounded tail of stderr from each stdio child.  Capturing a tail
# (rather than a prefix) preserves the final exception and exit context in the
# common traceback case; 16 KiB is enough that launcher output such as
# ``uv``/``npm`` — one ``error:`` line followed by screens of hints — still
# keeps the ``error:`` line, while a noisy server cannot grow Chrys memory
# without bound.
MAX_STDIO_STDERR_BYTES_CAPTURED = 16 * 1024

# The full tail lives on ``MCPConnectionError.stderr_tail`` (structured
# consumers and tests read it there); the error *message* — which is what the
# warning toast, the MCP Test dialog and runtime details currently show —
# previews only the last few lines, each capped like banner lines.
MAX_STDIO_STDERR_PREVIEW_LINES = 10

# Display cap for filesystem-derived paths (executable, cwd) in error messages.
MAX_STDIO_PATH_DISPLAY_CHARS = 512
_STDIO_ENV_OVERRIDE: ContextVar[dict[str, str] | None] = ContextVar("mcp_stdio_env_override", default=None)
_STDIO_CWD_OVERRIDE: ContextVar[str | None] = ContextVar("mcp_stdio_cwd_override", default=None)
_STDIO_ENV_IS_COMPLETE: ContextVar[bool] = ContextVar("mcp_stdio_env_is_complete", default=False)


def _clean_stdio_stderr(text: str) -> str:
    """Normalize untrusted subprocess stderr for logs and UI diagnostics."""
    normalized = process_carriage_returns(strip_ansi(text))
    return sanitize_legacy_block(normalized).strip()


@dataclasses.dataclass(slots=True)
class _StdioProcessDiagnostics:
    """Bounded, mutable diagnostics populated by one stdio transport run."""

    _stderr_tail: bytearray = dataclasses.field(default_factory=bytearray)
    stderr_bytes_seen: int = 0
    exit_code: int | None = None
    resolved_executable: str = ""
    effective_cwd: str = ""

    def reset(self) -> None:
        """Reset state before a new child process is spawned."""
        self._stderr_tail.clear()
        self.stderr_bytes_seen = 0
        self.exit_code = None
        self.resolved_executable = ""
        self.effective_cwd = ""

    def record_spawn(self, *, resolved_executable: str, cwd: object) -> None:
        """Record what is about to be spawned and where (``None`` cwd = inherited)."""
        self.resolved_executable = resolved_executable
        if cwd is None or cwd == "":
            try:
                self.effective_cwd = os.getcwd()
            except OSError:
                self.effective_cwd = ""
        else:
            self.effective_cwd = str(cwd)

    def append_stderr(self, chunk: bytes) -> None:
        """Append bytes while retaining only the configured tail."""
        if not chunk:
            return
        self.stderr_bytes_seen += len(chunk)
        self._stderr_tail.extend(chunk)
        overflow = len(self._stderr_tail) - MAX_STDIO_STDERR_BYTES_CAPTURED
        if overflow > 0:
            del self._stderr_tail[:overflow]

    def record_exit_code(self, wait_result: object, process: Any) -> None:
        """Record a return code from an external anyio/MCP process wrapper."""
        exit_code = wait_result if type(wait_result) is int else getattr(process, "returncode", None)
        if type(exit_code) is not int:
            popen = getattr(process, "popen", None)
            exit_code = getattr(popen, "returncode", None) if popen is not None else None
        if type(exit_code) is int:
            self.exit_code = exit_code

    @property
    def stderr_tail(self) -> str:
        """Return a display-safe decoded copy of the retained stderr tail."""
        return _clean_stdio_stderr(bytes(self._stderr_tail).decode("utf-8", errors="replace"))

    @property
    def stderr_dropped_bytes(self) -> int:
        """Number of earlier stderr bytes omitted from the retained tail."""
        return max(0, self.stderr_bytes_seen - len(self._stderr_tail))


@dataclasses.dataclass(frozen=True, slots=True)
class _StdioFailureDiagnostics:
    """Immutable snapshot attached to an MCP connection failure."""

    stderr_tail: str = ""
    stderr_dropped_bytes: int = 0
    process_exit_code: int | None = None
    resolved_executable: str = ""
    effective_cwd: str = ""


def _display_path(value: str) -> str:
    """Make a raw filesystem path safe for a single-line message.

    Paths come from ``os.fsdecode`` and config, so they may carry lone
    surrogates (not UTF-8 encodable) or control characters (which could forge
    extra diagnostic lines).  The raw value stays on the exception attribute.
    """
    text = sanitize_legacy_scalar(surrogate_safe_text(value))
    if len(text) > MAX_STDIO_PATH_DISPLAY_CHARS:
        text = text[: MAX_STDIO_PATH_DISPLAY_CHARS - 1] + "…"
    return text


def _resolve_spawn_executable(command: str, env: Mapping[str, str]) -> str:
    """Best-effort path of what ``Popen`` will exec for *command* under *env*.

    Mirrors POSIX ``subprocess``: a command with a path component is used as
    is; otherwise the search uses the CHILD environment's ``PATH``
    (``os.get_exec_path(env)``), which is what decides "which ``uv``" when the
    TUI and a headless run carry different runtime paths.  Falls back to the
    unresolved command when nothing is found — the spawn itself is unchanged.
    """
    if os.path.dirname(command):
        return command
    try:
        found = shutil.which(command, path=os.pathsep.join(os.get_exec_path(env)))
    except OSError, TypeError, ValueError:
        return command
    return found or command


def _past_store_python_stub(
    command: str, resolved: str, env: dict[str, str], cwd: str | os.PathLike[str] | None
) -> str:
    """*resolved*, the SDK's lookup of *command*, unless that is Windows' placeholder for a Python not installed.

    The placeholder only offers Python from the Microsoft Store and exits 9009, so a match of the same name on the
    PATH of the server's environment *env* is taken instead; with none, the server fails to start and says why.
    """
    if not is_windows_store_python_stub(resolved):
        return resolved
    alternative = "" if os.path.dirname(command) else which_excluding_runtime(command, env=_path_from(env, cwd))
    if not alternative:
        advice = "Write the full path of an installed Python, or put its folder on PATH"
        if ntpath.splitext(ntpath.basename(command))[0].casefold() == "python3":
            advice += "; python.org's traditional installer adds no python3, so write 'python' for its Python"
        raise FileNotFoundError(
            errno.ENOENT,
            f"{command!r} starts only Windows' placeholder that offers Python from the Microsoft Store. {advice}.",
        )
    return alternative


def _path_from(env: dict[str, str], cwd: str | os.PathLike[str] | None) -> dict[str, str]:
    """*env* with each relative PATH entry read from *cwd*, the folder the server starts in, as the server reads it."""
    key = path_env_key(env)
    if cwd is None or key is None:
        return env
    return {**env, key: os.pathsep.join(os.path.join(cwd, entry) for entry in env[key].split(os.pathsep) if entry)}


def _stderr_preview(stderr_tail: str, *, dropped_bytes: int) -> str:
    """Render the last few stderr lines for an error message / warning toast.

    The full tail stays on the exception; the message keeps at most
    :data:`MAX_STDIO_STDERR_PREVIEW_LINES` lines, each capped like banner
    lines, so a traceback cannot turn a toast into a wall of text.
    """
    lines = stderr_tail.splitlines()
    omitted_lines = max(0, len(lines) - MAX_STDIO_STDERR_PREVIEW_LINES)
    shown = [
        (line[:MAX_BANNER_LINE_CHARS] + "…") if len(line) > MAX_BANNER_LINE_CHARS else line
        for line in lines[-MAX_STDIO_STDERR_PREVIEW_LINES:]
    ]
    omitted: list[str] = []
    if omitted_lines:
        omitted.append(f"{omitted_lines} earlier lines")
    if dropped_bytes:
        omitted.append(f"{dropped_bytes} earlier bytes")
    suffix = f" ({' and '.join(omitted)} omitted)" if omitted else ""
    body = "\n  ".join(shown)
    return f"Server stderr tail{suffix}:\n  {body}"


def _mirror_no_proxy_aliases(env: dict[str, str], extra_keys: set[str]) -> None:
    """Keep ``NO_PROXY`` and ``no_proxy`` aligned for stdio child processes.

    User environments commonly set either canonical uppercase ``NO_PROXY``
    or lowercase ``no_proxy``.  Stdio MCP servers are a different boundary:
    the child command may be implemented by a runtime that only checks the
    lowercase proxy convention.  Mirror the value into the missing casing
    just before spawning, without mutating the parent process environment.

    Per-server MCP env values should win over inherited parent values.  When
    the user supplies only one casing in the per-server env, mirror that
    supplied value over the inherited alias too.  When the user explicitly
    supplies both casings, preserve both values unchanged.

    This deliberately handles only the proxy-bypass list, not proxy URL
    variables such as ``HTTP_PROXY`` / ``http_proxy``: those have different
    security and precedence behavior across runtimes, including the
    historical CGI ``HTTP_PROXY`` hazard.
    """
    upper = "NO_PROXY"
    lower = "no_proxy"

    if upper in extra_keys and lower not in extra_keys:
        env[lower] = env[upper]
        return
    if lower in extra_keys and upper not in extra_keys:
        env[upper] = env[lower]
        return
    if upper in env and lower not in env:
        env[lower] = env[upper]
        return
    if lower in env and upper not in env:
        env[upper] = env[lower]


def _inherited_stdio_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build the env passed to a spawned stdio MCP server subprocess.

    Unlike the MCP SDK's ``get_default_environment()`` — which inherits a
    strict ~6-var allowlist (``PATH`` / ``HOME`` / ``USER`` / ...) — chrys
    forwards nearly all of the parent ``os.environ`` so subprocesses see the
    user's proxy settings (``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``NO_PROXY``),
    TLS bundle paths (``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` /
    ``REQUESTS_CA_BUNDLE``), locale (``LANG`` / ``LC_ALL``), language
    runtimes' caches, and other vars the user expects when running the same
    command from their shell.  Without this, a tool like ``uvx`` that reaches
    PyPI through a corporate proxy hangs forever, because the SDK default
    strips ``HTTPS_PROXY`` and the child never reaches the network — surfaced
    as an ``initialize`` timeout.

    Bash function exports (values starting with ``"()"``) are dropped, the
    same Shellshock-era filter the SDK applies.  Inherited ``PYTHONHOME`` and
    ``PYTHONPATH`` are also stripped so CPython-based server launchers are not
    forced to use the parent's stdlib/search path.  Optional ``extra``
    overrides — typically ``MCPServerConfig.env`` — are merged on top so
    per-server config wins over the sanitized inherited environment.  For
    packaged PyApp runs, Chrys runtime executable directories are moved behind
    user/system PATH entries so commands like ``uvx`` resolve the user's
    install first and fall back to the embedded runtime only if needed.

    An inherited value holding a ``{{!command}}`` handle is dropped as well: only
    the server's own settings may run a command and receive its token.
    """
    env: dict[str, str] = {
        k: v for k, v in os.environ.items() if not v.startswith("()") and not COMMAND_HANDLE_RE.search(v)
    }
    strip_python_runtime_overrides(env)
    extra_keys = set(extra or ())
    if extra:
        env.update(extra)
    _mirror_no_proxy_aliases(env, extra_keys)
    reorder_path_demoting_runtime(env)
    return env


@asynccontextmanager
async def tolerant_stdio_client(
    server: Any,
    errlog: TextIO = sys.stderr,
    *,
    dropped_banner_lines: list[str] | None = None,
    process_diagnostics: _StdioProcessDiagnostics | None = None,
    inherit_env: bool = True,
) -> AsyncGenerator[tuple[Any, Any]]:
    """``mcp.client.stdio.stdio_client`` variant that ignores banner lines.

    Many real-world stdio MCP servers (vendor CLI wrappers, several
    ``npx`` packages, locally-built tools) violate the spec by emitting
    human-readable startup chatter on stdout before they begin speaking
    JSON-RPC.
    Upstream ``stdio_client`` treats every line as a JSON-RPC message and
    pushes a parse exception onto the read stream when it fails — but the
    base session quietly drops those exceptions, so the pending
    ``initialize()`` waits forever.  The Test button hangs and the agent
    build hangs.

    This wrapper classifies each stdout line into three buckets:

    * **Empty/whitespace-only** — skipped silently (no log, not captured).
    * **Not JSON at all** (``json.loads`` fails) — treated as banner
      chatter.  Dropped.  The first ``MAX_BANNER_LINES_CAPTURED`` banner
      lines are appended to ``dropped_banner_lines`` (when supplied) so
      callers can include them in error messages, and the first one is
      logged at WARNING.  This catches plaintext banners like
      ``"FooBarServer v1.0 (build 42)"`` and bracketed log lines like
      ``"[INFO] starting"``.
    * **Valid JSON but fails JSON-RPC validation** — kept as the
      original "push exception onto read stream" behavior.  Note this
      doesn't surface immediately: ``BaseSession._receive_loop`` routes
      stream exceptions through ``_handle_incoming`` which the default
      handler swallows.  The pending ``initialize()`` then waits until
      ``read_timeout_seconds`` (set via ``request_timeout``) fires.  Our
      ``MCPAdapter`` injects a default ``request_timeout`` floor when
      none is configured, so this case is bounded — but it's "fails
      eventually under the timeout floor", not "fails fast".

    Process lifecycle and shutdown mirror upstream ``stdio_client``.  In
    addition to line handling and the inherited environment, stderr is piped
    into a bounded diagnostic tail and the subprocess return code is recorded
    during shutdown.  Stderr is always drained concurrently so a verbose child
    cannot block on a full pipe.

    This reader is built on private ``mcp.client.stdio`` primitives
    (``_create_platform_compatible_process`` / ``_get_executable_command``
    / ``_terminate_process_tree`` / ``PROCESS_TERMINATION_TIMEOUT``) plus the
    SDK's ``SessionMessage`` wrapper.  If a dependency update removes any of
    them, we degrade to the stock ``stdio_client`` — losing banner tolerance
    and bounded process diagnostics — instead of breaking every stdio MCP
    connection.  The sanitized inherited environment is still forwarded in
    that path by copying it onto the server params.
    """
    import anyio
    import anyio.lowlevel
    from anyio import to_thread
    from anyio.streams.text import TextReceiveStream
    from mcp import types

    diagnostics = process_diagnostics or _StdioProcessDiagnostics()
    diagnostics.reset()

    # A child keeps the environment it starts with, so each launch gets a token just fetched.
    if server.env and command_handles_in(server.env.values()):
        from chrys.service.command_credentials import refresh, substitute

        await refresh(server.env.values())
        server = server.model_copy(update={"env": {name: substitute(value) for name, value in server.env.items()}})

    try:
        from mcp.client.stdio import (
            PROCESS_TERMINATION_TIMEOUT,
            _create_platform_compatible_process,
            _get_executable_command,
            _terminate_process_tree,
        )
        from mcp.shared.message import SessionMessage
    except ImportError:
        logger.warning(
            "Tolerant stdio MCP reader is disabled because MCP SDK stdio internals changed; "
            "falling back to the stock stdio client (stdout banner and process diagnostics lost).",
            exc_info=True,
        )
        from mcp.client.stdio import stdio_client

        stock_server = (
            server.model_copy(update={"env": _inherited_stdio_environment(server.env)}) if inherit_env else server
        )
        async with stdio_client(stock_server, errlog=errlog) as streams:
            yield streams
        return

    captured = dropped_banner_lines if dropped_banner_lines is not None else []
    warned_once = False

    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    try:
        env = _inherited_stdio_environment(server.env) if inherit_env else dict(server.env or {})
        command = _past_store_python_stub(server.command, _get_executable_command(server.command), env, server.cwd)
        diagnostics.record_spawn(resolved_executable=_resolve_spawn_executable(command, env), cwd=server.cwd)
        # The SDK types ``errlog`` as ``TextIO`` but hands it straight to
        # ``anyio.open_process`` / ``subprocess.Popen(stderr=...)`` on every
        # platform path, so the ``PIPE`` sentinel is accepted at runtime.
        process = await _create_platform_compatible_process(
            command=command,
            args=server.args,
            env=env,
            errlog=cast("TextIO", subprocess.PIPE),
            cwd=server.cwd,
        )
    except OSError:
        await read_stream.aclose()
        await write_stream.aclose()
        await read_stream_writer.aclose()
        await write_stream_reader.aclose()
        raise

    async def stdout_reader() -> None:
        nonlocal warned_once
        if not process.stdout:
            raise RuntimeError("Opened process is missing stdout")
        try:
            async with read_stream_writer:
                buffer = ""
                async for chunk in TextReceiveStream(
                    process.stdout,
                    encoding=server.encoding,
                    errors=server.encoding_error_handler,
                ):
                    lines = (buffer + chunk).split("\n")
                    buffer = lines.pop()

                    for line in lines:
                        stripped = line.lstrip()
                        if not stripped:
                            # Empty/whitespace-only: silently ignore.
                            continue

                        try:
                            parsed = json.loads(line)
                        except json.JSONDecodeError:
                            # Not JSON at all — banner chatter.  Capture a
                            # few for diagnostics, then drop.
                            if len(captured) < MAX_BANNER_LINES_CAPTURED:
                                captured.append(line.rstrip("\r"))
                            if not warned_once:
                                warned_once = True
                                logger.warning(
                                    "MCP stdio server emitted non-JSON line on stdout (dropped): %r",
                                    line[:200],
                                )
                            else:
                                logger.debug(
                                    "MCP stdio server emitted non-JSON line on stdout (dropped): %r", line[:200]
                                )
                            continue

                        try:
                            message = types.JSONRPCMessage.model_validate(parsed)
                        except Exception as exc:
                            # Valid JSON but failed JSON-RPC validation.
                            # Push onto the read stream where the SDK's
                            # ``_handle_incoming`` will eventually surface
                            # it (in practice — once the pending request
                            # times out under our injected
                            # ``request_timeout`` floor).
                            logger.exception("Failed to parse JSONRPC message from server")
                            await read_stream_writer.send(exc)
                            continue

                        await read_stream_writer.send(SessionMessage(message))
        except anyio.ClosedResourceError, anyio.BrokenResourceError:
            # ClosedResourceError: our writer was closed (normal shutdown).
            # BrokenResourceError: the SDK closed the receive end before we
            # finished draining stdout — happens during failed-connect
            # cleanup, where letting this escape would mask the real
            # initialize() exception with an ExceptionGroup.
            await anyio.lowlevel.checkpoint()

    async def stdin_writer() -> None:
        if not process.stdin:
            raise RuntimeError("Opened process is missing stdin")
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    json_text = session_message.message.model_dump_json(by_alias=True, exclude_none=True)
                    await process.stdin.send(
                        (json_text + "\n").encode(
                            encoding=server.encoding,
                            errors=server.encoding_error_handler,
                        )
                    )
        except anyio.ClosedResourceError, anyio.BrokenResourceError:
            await anyio.lowlevel.checkpoint()

    stderr_drained = anyio.Event()

    async def stderr_reader() -> None:
        """Drain and retain a bounded tail from anyio or Windows fallback stderr."""
        try:
            stderr = getattr(process, "stderr", None)
            if stderr is None:
                return

            receive = getattr(stderr, "receive", None)
            if receive is not None:
                try:
                    while True:
                        chunk = await receive()
                        if chunk:
                            diagnostics.append_stderr(bytes(chunk))
                except anyio.EndOfStream, anyio.ClosedResourceError, anyio.BrokenResourceError, OSError, ValueError:
                    return

            # The SDK's Windows ``FallbackProcess`` exposes the raw Popen
            # stderr file.  Prefer ``read1`` (returns whatever is available)
            # over ``read`` (blocks until the size is filled or EOF) so a
            # hanging server still shows what it wrote so far.
            read = getattr(stderr, "read1", None) or getattr(stderr, "read", None)
            if read is None:
                return
            try:
                while chunk := await to_thread.run_sync(read, 64 * 1024):
                    if inspect.isawaitable(chunk):
                        # Not a sync file after all; never loop on an awaitable.
                        # Only real coroutine objects need (and have) close().
                        if inspect.iscoroutine(chunk):
                            chunk.close()
                        return
                    diagnostics.append_stderr(chunk if isinstance(chunk, bytes) else str(chunk).encode())
            except OSError, ValueError:
                return
        finally:
            stderr_drained.set()

    async with (
        anyio.create_task_group() as tg,
        process,
    ):
        tg.start_soon(stdout_reader)
        tg.start_soon(stdin_writer)
        tg.start_soon(stderr_reader)
        try:
            yield read_stream, write_stream
        finally:
            if process.stdin:
                with contextlib.suppress(Exception):
                    await process.stdin.aclose()
            wait_result: object = None
            try:
                try:
                    with anyio.fail_after(PROCESS_TERMINATION_TIMEOUT):
                        wait_result = await process.wait()
                except TimeoutError:
                    await _terminate_process_tree(process)
                    try:
                        with anyio.fail_after(PROCESS_TERMINATION_TIMEOUT):
                            wait_result = await process.wait()
                    except TimeoutError, ProcessLookupError:
                        pass
                except ProcessLookupError:
                    pass
            finally:
                diagnostics.record_exit_code(wait_result, process)
                # wait() only proves the child exited; unread bytes may still
                # be buffered in its stderr pipe.  Let the reader drain before
                # process.__aexit__ closes that stream.  A descendant can keep
                # the pipe open after the direct child exits, so this wait must
                # remain bounded.
                with anyio.move_on_after(PROCESS_TERMINATION_TIMEOUT):
                    await stderr_drained.wait()
            await read_stream.aclose()
            await write_stream.aclose()
            await read_stream_writer.aclose()
            await write_stream_reader.aclose()


class _SafeStdioTool(_NoPrePagePingMixin, _StructuredContentFallbackMixin, MCPStdioTool):
    """MCPStdioTool that hardens the stdio transport for chrys.

    Two behaviors layered on top of upstream ``MCPStdioTool``:

    1. **Bounded stderr capture.**  Upstream ``stdio_client`` defaults
       ``errlog=sys.stderr``, inheriting the parent's stderr fd into the
       spawned child.  Inside the Chrys TUI (especially on Python 3.14),
       ``sys.stderr.fileno()`` may raise or return a fd unusable for
       subprocess inheritance, causing ``anyio.open_process`` to fail
       outright.  Even when the fd is valid, child stderr bytes corrupt
       the Chrys TUI frame.  The tolerant transport instead pipes and drains
       stderr into a bounded tail, records the exit code, and uses devnull only
       for the compatibility fallback to the stock SDK client.

    2. **Tolerant stdout reader.**  Many stdio MCP servers (vendor CLI
       wrappers, some ``npx`` packages) emit human-readable banner text on stdout before
       speaking JSON-RPC.  Upstream's reader pushes a parse exception
       onto the read stream — which the base session silently drops —
       leaving the pending ``initialize()`` blocked forever.  We swap in
       :func:`tolerant_stdio_client` which ignores banner lines and
       captures them on ``dropped_banner_lines`` so the adapter can
       surface them in error messages.
    """

    def __init__(self, *args: Any, env_is_complete: bool = False, **kwargs: Any) -> None:
        self._errlog_file: io.TextIOWrapper | None = None
        self.dropped_banner_lines: list[str] = []
        self._process_diagnostics = _StdioProcessDiagnostics()
        self._env_is_complete = env_is_complete
        super().__init__(*args, **kwargs)

    def get_mcp_client(self) -> Any:
        """Build a tolerant stdio transport with bounded process diagnostics."""
        args: dict[str, Any] = {
            "command": self.command,
            "args": self.args,
            "env": self.env,
        }
        if self.encoding:
            args["encoding"] = self.encoding
        if self._client_kwargs:
            args.update(self._client_kwargs)
        try:
            from mcp.client.stdio import StdioServerParameters
        except ModuleNotFoundError as ex:
            raise ModuleNotFoundError("`mcp` is required to use `MCPStdioTool`. Please install `mcp`.") from ex

        return tolerant_stdio_client(
            server=StdioServerParameters(**args),
            errlog=self._devnull_errlog(),
            dropped_banner_lines=self.dropped_banner_lines,
            process_diagnostics=self._process_diagnostics,
            inherit_env=not self._env_is_complete,
        )

    def _devnull_errlog(self) -> io.TextIOWrapper:
        """Lazily open the stock-client fallback's safe stderr target."""
        if self._errlog_file is None or self._errlog_file.closed:
            self._errlog_file = open(os.devnull, "w")  # noqa: SIM115
        return self._errlog_file

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Clean up the stock-client fallback's devnull handle on exit."""
        try:
            await super().__aexit__(exc_type, exc_value, traceback)
        finally:
            if self._errlog_file is not None and not self._errlog_file.closed:
                self._errlog_file.close()
                self._errlog_file = None
