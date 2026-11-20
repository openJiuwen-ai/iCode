# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Additional branch coverage for ``service/tools/builtins/shell.py``:

- Command filter rejection
- ``list_workspace_dirs`` tool + its inclusion by ``tools()``
- ``FileNotFoundError`` / generic-exception branches in ``execute``
- Progress streaming (PTY ``_drain`` streaming path)
- Windows pipe execution path (sys.platform patched)
- ``_truncate_output`` fits-after-tokenization edge case
- Explicit ``_Timeout`` return value

The Windows pipe path is forced by monkey-patching ``sys.platform`` and
replacing the inner subprocess helpers — no real Windows host required.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.models.workspace import WorkingDir, Workspace
from chrys.foundation.platform import ShellInfo
from chrys.foundation.platform.output_capture import CapturedOutput
from chrys.foundation.text.tool_output import truncate_output as _truncate_output
from chrys.foundation.tool_result_metadata import (
    SHELL_EXIT_CODE_METADATA_KEY,
    SHELL_TIMED_OUT_METADATA_KEY,
    SHELL_TIMEOUT_SECONDS_METADATA_KEY,
)
from chrys.kernel.tools import SyncToolCancelledAfterCompletion
from chrys.service.tools.builtins import shell as shell_mod
from chrys.service.tools.builtins.shell import (
    _PIPEFAIL_SHELLS,
    SHELL_TOOL_NAMES,
    ShellTools,
    _Command,
    _shell_argv_variants,
    _signal_command,
    _Timeout,
    shell_progress_callback,
    shell_result_metadata,
)
from chrys.service.tools.builtins.shell_filter import ShellCommandFilter
from chrys.service.tools.spill import TOOL_RESULTS_DIR_NAME

IS_UNIX = sys.platform != "win32"


@pytest.fixture
def runtime() -> SessionEnvironment:
    return SessionEnvironment.capture()


# ───────────────────────── command filter branch ─────────────────────


async def test_execute_blocked_by_filter(runtime: SessionEnvironment) -> None:
    """A command rejected by the filter returns ``Error: command blocked``."""
    # Filter that only allows ``echo``
    cf = ShellCommandFilter(whitelist={"echo"})
    shell = ShellTools(runtime, command_filter=cf)

    out = await shell.execute("rm -rf /tmp/whatever", reason="attempt")
    assert out.startswith("Error: command blocked")
    # Exit code marker should NOT be present — we never ran anything
    assert "[exit_code:" not in out


async def test_execute_allowed_by_filter_runs(runtime: SessionEnvironment) -> None:
    cf = ShellCommandFilter(whitelist={"echo"})
    shell = ShellTools(runtime, command_filter=cf)

    out = await shell.execute("echo hello", reason="test")
    assert "hello" in out
    assert "[exit_code: 0]" in out


# ───────────────────────── list_workspace_dirs ────────────────────────


def _runtime_with_dirs(tmp_path: Path) -> SessionEnvironment:
    primary = tmp_path / "primary"
    primary.mkdir()
    secondary = tmp_path / "secondary"
    secondary.mkdir()
    ws = Workspace(
        primary_cwd=str(primary),
        working_dirs=[
            WorkingDir(path=str(primary), label="main", is_primary=True),
            WorkingDir(path=str(secondary), label="extra", is_primary=False),
        ],
    )
    return SessionEnvironment.capture(workspace=ws)


def test_tools_includes_list_workspace_dirs(tmp_path: Path) -> None:
    """``tools()`` adds ``list_workspace_dirs`` when the workspace has dirs."""
    shell = ShellTools(_runtime_with_dirs(tmp_path))
    names = [t.name for t in shell.tools()]
    assert "list_workspace_dirs" in names


def test_tools_excludes_list_workspace_dirs_when_empty(runtime: SessionEnvironment) -> None:
    """With no working dirs, only the detected shell tool is exported."""
    shell = ShellTools(runtime)
    names = [t.name for t in shell.tools()]
    # No list_workspace_dirs tool when there are no working_dirs
    assert "list_workspace_dirs" not in names


def test_list_workspace_dirs_no_workspace(runtime: SessionEnvironment) -> None:
    """Empty workspace → falls back to ``Current directory`` line."""
    shell = ShellTools(runtime)
    out = shell.list_workspace_dirs()
    assert "No workspace active" in out
    assert runtime.cwd in out


def test_list_workspace_dirs_lists_entries(tmp_path: Path) -> None:
    """Lists each dir with label, primary marker, and exists status."""
    rt = _runtime_with_dirs(tmp_path)
    out = ShellTools(rt).list_workspace_dirs()

    assert "Workspace directories" in out
    assert "[main]" in out
    assert "[extra]" in out
    assert "(primary)" in out
    # Both dirs exist on disk → ok marker
    assert " ok" in out


def test_list_workspace_dirs_missing_dir(tmp_path: Path) -> None:
    """Non-existent dir is marked MISSING."""
    ws = Workspace(
        primary_cwd=str(tmp_path),
        working_dirs=[WorkingDir(path=str(tmp_path / "gone"), label="", is_primary=True)],
    )
    rt = SessionEnvironment.capture(workspace=ws)
    out = ShellTools(rt).list_workspace_dirs()
    assert "MISSING" in out


# ───────────────────────── execute error branches ─────────────────────


async def test_execute_shell_not_found(runtime: SessionEnvironment) -> None:
    """If the shell binary doesn't exist, return a friendly error."""
    # Replace the shell path with a nonexistent binary
    bad_shell = replace(runtime.platform.shell, path="/definitely/not/a/real/shell")
    shell = ShellTools(runtime, shell=bad_shell)

    out = await shell.execute("echo hi", reason="test")
    assert out.startswith("Error: shell not found")
    assert "/definitely/not/a/real/shell" in out


async def test_execute_generic_exception(runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-Timeout, non-FileNotFoundError → ``Error: failed to execute command``."""
    shell = ShellTools(runtime)

    async def _boom(*_a: Any, **_kw: Any) -> tuple[str, int]:
        raise RuntimeError("kaboom")

    # On Unix, execute() calls _execute_pty; on Windows, _execute_pipe. Patch whichever.
    if IS_UNIX:
        monkeypatch.setattr(shell, "_execute_pty", _boom)
    else:
        monkeypatch.setattr(shell, "_execute_pipe", _boom)

    out = await shell.execute("echo hi", reason="test")
    assert out.startswith("Error: failed to execute command")
    assert "kaboom" in out


async def test_execute_records_exit_code_metadata(runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
    """Shell execution should expose process exit code out-of-band for UI renderers."""
    shell = ShellTools(runtime)
    metadata: dict[str, object] = {}

    async def _pty_done(
        command: str,
        shell: ShellInfo,
        cwd: str,
        timeout: int | float,
        reason: str = "",
        *,
        argv: Sequence[str] | None = None,
        sanitized: bool = False,
    ) -> tuple[CapturedOutput, int]:
        return CapturedOutput(b"boom", b"", 4), 7

    async def _pipe_done(
        command: str,
        shell: ShellInfo,
        cwd: str,
        timeout: int | float,
        reason: str = "",
        *,
        argv: Sequence[str] | None = None,
        sanitized: bool = False,
    ) -> tuple[CapturedOutput, int, CapturedOutput]:
        return CapturedOutput(b"boom", b"", 4), 7, CapturedOutput(b"", b"", 0)

    if IS_UNIX:
        monkeypatch.setattr(shell, "_execute_pty", _pty_done)
    else:
        monkeypatch.setattr(shell, "_execute_pipe", _pipe_done)

    token = shell_result_metadata.set(metadata)
    try:
        out = await shell.execute("false", reason="test")
    finally:
        shell_result_metadata.reset(token)

    assert out.endswith("[exit_code: 7]")
    assert metadata[SHELL_EXIT_CODE_METADATA_KEY] == 7


async def test_execute_records_timeout_metadata(runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
    """Timeout state should be structured metadata, not only formatted result text."""
    shell = ShellTools(runtime)
    metadata: dict[str, object] = {}

    async def _timeout(
        command: str,
        shell: ShellInfo,
        cwd: str,
        timeout: int | float,
        reason: str = "",
        *,
        argv: Sequence[str] | None = None,
        sanitized: bool = False,
    ) -> tuple[CapturedOutput, int]:
        raise _Timeout((CapturedOutput(b"before", b"", 6),))

    if IS_UNIX:
        monkeypatch.setattr(shell, "_execute_pty", _timeout)
    else:
        monkeypatch.setattr(shell, "_execute_pipe", _timeout)

    token = shell_result_metadata.set(metadata)
    try:
        out = await shell.execute("sleep 999", reason="test", timeout=123)
    finally:
        shell_result_metadata.reset(token)

    assert out.startswith("Error: command timed out after 123 seconds.")
    assert metadata[SHELL_TIMED_OUT_METADATA_KEY] is True
    assert metadata[SHELL_TIMEOUT_SECONDS_METADATA_KEY] == 123


@pytest.mark.skipif(not IS_UNIX, reason="PTY timeout capture is Unix-only")
async def test_execute_timeout_includes_partial_output(runtime: SessionEnvironment) -> None:
    """A timed-out shell command should return output already captured by the PTY reader."""
    shell = ShellTools(runtime)

    out = await shell.execute("printf 'before-timeout\\n'; sleep 5", reason="test", timeout=1)

    assert out.startswith("Error: command timed out after 1 seconds.")
    assert "[partial output]" in out
    assert "before-timeout" in out
    assert "[exit_code:" not in out


# ───────────────────────── _truncate_output edge case ─────────────────


def test_truncate_output_returns_original_when_lines_fit() -> None:
    """Tiny budgets remain bounded even when the full marker cannot fit."""
    text = "a\nb\nc\nd\n"
    out = _truncate_output(text, max_tokens=1)

    assert out != text
    assert len(out) <= 7


async def test_bound_result_spills_full_canonical_output(runtime: SessionEnvironment, tmp_path: Path) -> None:
    canonical = f"{'x' * 10_000}\n[stderr]\nimportant failure\n[exit_code: 7]"
    shell = ShellTools(runtime, session_dir=tmp_path)

    out = await shell._bound_result(canonical, 100)

    assert "truncated" in out
    assert "Full output saved to:" in out
    spills = list((tmp_path / TOOL_RESULTS_DIR_NAME).glob("shell_*.txt"))
    assert len(spills) == 1
    assert spills[0].read_text() == canonical
    assert "[exit_code: 7]" in out


async def test_bound_result_without_session_dir_is_truncate_only(runtime: SessionEnvironment) -> None:
    out = await ShellTools(runtime)._bound_result("x" * 10_000, 100)

    assert "truncated" in out
    assert "Full output saved to:" not in out


async def test_bound_result_propagates_completed_spill_result_on_cancellation(
    runtime: SessionEnvironment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = "[bounded completed shell result]"

    async def cancelled_after_completion(
        dir_path: Path | None,
        prefix: str,
        text: str,
        budget: int,
        captures: Sequence[CapturedOutput] = (),
        *,
        lead: str = "",
    ) -> str:
        raise SyncToolCancelledAfterCompletion(completed)

    monkeypatch.setattr(shell_mod, "bound_process_output", cancelled_after_completion)

    with pytest.raises(SyncToolCancelledAfterCompletion) as exc_info:
        await ShellTools(runtime)._bound_result("x" * 10_000, 100)

    assert exc_info.value.completed_result == completed


# ───────────────────────── PTY progress streaming ─────────────────────


@pytest.mark.skipif(not IS_UNIX, reason="PTY path is Unix-only")
async def test_pty_streams_progress_lines(runtime: SessionEnvironment) -> None:
    """When a ``shell_progress_callback`` is set, PTY drains line-by-line
    and invokes the callback with batches of cleaned lines."""
    shell = ShellTools(runtime)
    received: list[list[str]] = []

    async def cb(lines: list[str]) -> None:
        received.append(lines)

    token = shell_progress_callback.set(cb)
    try:
        # Emit several lines with a brief pause so batch ticks cross the
        # 0.2s throttle interval at least once.
        await shell.execute(
            "printf 'one\\ntwo\\n'; sleep 0.25; printf 'three\\n'",
            reason="stream test",
            timeout=5,
        )
    finally:
        shell_progress_callback.reset(token)

    flat = [line for batch in received for line in batch]
    # All three lines should have surfaced via the callback (possibly split
    # across multiple batches)
    assert "one" in flat
    assert "two" in flat
    assert "three" in flat


@pytest.mark.skipif(not IS_UNIX, reason="PTY path is Unix-only")
async def test_pty_streams_blank_progress_lines(runtime: SessionEnvironment) -> None:
    """Completed blank lines should reach the progress callback."""
    shell = ShellTools(runtime)
    received: list[list[str]] = []

    async def cb(lines: list[str]) -> None:
        received.append(lines)

    token = shell_progress_callback.set(cb)
    try:
        await shell.execute(
            "printf '[line 1]\\n\\n[line 2]\\n\\n[line 3]\\n'",
            reason="stream blank line test",
            timeout=5,
        )
    finally:
        shell_progress_callback.reset(token)

    flat = [line for batch in received for line in batch]
    assert flat == ["[line 1]", "", "[line 2]", "", "[line 3]"]


@pytest.mark.skipif(not IS_UNIX, reason="PTY process groups are Unix-only")
def test_signal_command_skips_group_when_child_shares_parent_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defense-in-depth: never signal Chrys's own process group."""
    calls: list[tuple[str, int, int]] = []

    class Proc:
        returncode = None
        pid = 1234

    monkeypatch.setattr(shell_mod, "signal_process_session", lambda _sid, _signum: False)
    monkeypatch.setattr(os, "getpgid", lambda _pid: 999)
    monkeypatch.setattr(os, "getpgrp", lambda: 999)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig)))
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))

    _signal_command(Proc(), signal.SIGKILL)  # type: ignore[arg-type]

    assert calls == [("kill", 1234, signal.SIGKILL)]


@pytest.mark.skipif(not IS_UNIX, reason="PTY process groups are Unix-only")
def test_signal_command_signals_verified_child_group(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int, int]] = []

    class Proc:
        returncode = None
        pid = 1234

    monkeypatch.setattr(shell_mod, "signal_process_session", lambda _sid, _signum: False)
    monkeypatch.setattr(os, "getpgid", lambda _pid: 1234)
    monkeypatch.setattr(os, "getpgrp", lambda: 999)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig)))
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))

    _signal_command(Proc(), signal.SIGTERM)  # type: ignore[arg-type]

    assert calls == [("killpg", 1234, signal.SIGTERM)]


@pytest.mark.skipif(not IS_UNIX, reason="PTY process groups are Unix-only")
def test_signal_command_leaves_a_reaped_shells_pid_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once the shell was reaped, its pid may already name an unrelated process group."""
    calls: list[tuple[str, int, int]] = []

    class Proc:
        returncode = 0
        pid = 1234

    monkeypatch.setattr(shell_mod, "signal_process_session", lambda _sid, _signum: False)
    monkeypatch.setattr(os, "getpgid", lambda _pid: 1234)
    monkeypatch.setattr(os, "getpgrp", lambda: 999)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig)))
    monkeypatch.setattr(os, "kill", lambda pid, sig: calls.append(("kill", pid, sig)))

    _signal_command(Proc(), signal.SIGKILL)  # type: ignore[arg-type]

    assert calls == []


@pytest.mark.skipif(not IS_UNIX, reason="PTY process groups are Unix-only")
def test_signal_command_prefers_the_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int, int]] = []

    class Proc:
        returncode = None
        pid = 1234

    def fake_signal_process_session(process_session_id: int, signum: int) -> bool:
        calls.append(("session", process_session_id, signum))
        return True

    monkeypatch.setattr(shell_mod, "signal_process_session", fake_signal_process_session)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append(("killpg", pgid, sig)))

    _signal_command(Proc(), signal.SIGKILL)  # type: ignore[arg-type]

    assert calls == [("session", 1234, signal.SIGKILL)]


# ───────────────────────── Windows pipe branch (mocked) ───────────────


class _FakeStream:
    """Minimal StreamReader substitute exposing readline() / read()."""

    def __init__(self, lines: list[bytes], tail: bytes = b"") -> None:
        self._lines = list(lines)
        self._tail = tail
        self._tail_returned = False

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)

    async def read(self, _size: int = -1) -> bytes:
        if self._lines:
            data = b"".join(self._lines)
            self._lines.clear()
            return data
        if self._tail and not self._tail_returned:
            self._tail_returned = True
            return self._tail
        return b""


class _FakeWinProc:
    """Minimal stand-in for the managed process _execute_pipe spawns."""

    pid = 0

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = _FakeStream([], tail=stdout)
        self.stderr = _FakeStream([], tail=stderr)
        self.stdin_closed = False

    async def wait(self) -> int:
        return self.returncode

    def close_stdin(self) -> None:
        self.stdin_closed = True

    def close_transports(self) -> None:
        pass

    def kill_tree(self) -> None:
        pass

    def live_processes(self) -> dict[int, float]:
        # No real process: none of its tree is left once its shell exited.
        return {}


def _spawning(make: Callable[[], _FakeWinProc]) -> Callable[..., Awaitable[_FakeWinProc]]:
    """A stand-in for spawn_managed_stdio_process that starts the process *make* returns."""

    async def spawn(*_args: Any, **_kwargs: Any) -> _FakeWinProc:
        return make()

    return spawn


_fake_managed = _spawning(lambda: _FakeWinProc(stdout=b"hello from win\n", stderr=b"warn\n", returncode=0))


@contextlib.contextmanager
def _fake_preserve_console_mode():
    yield


async def test_windows_execute_pipe_path(runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the Windows branch of ``execute`` and verify assembly of the result string."""
    from chrys.service.tools.builtins import shell as shell_mod

    # Pretend we're on Windows (the compile-time imports at top of shell.py
    # guard fcntl/pty — those modules are NOT referenced on this code path)
    monkeypatch.setattr(shell_mod.sys, "platform", "win32")
    monkeypatch.setattr(shell_mod, "spawn_managed_stdio_process", _fake_managed)
    monkeypatch.setattr(shell_mod, "preserve_console_mode", _fake_preserve_console_mode)

    shell = ShellTools(runtime)
    out = await shell.execute("echo x", reason="test", timeout=5)

    assert "hello from win" in out
    # Stderr is labelled and appears BEFORE the exit code suffix
    assert "[stderr]" in out
    assert "warn" in out
    assert "[exit_code: 0]" in out
    # exit_code must be the last line
    assert out.strip().splitlines()[-1] == "[exit_code: 0]"


async def test_windows_execute_pipe_no_stderr(runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows path with empty stderr omits the ``[stderr]`` label."""
    from chrys.service.tools.builtins import shell as shell_mod

    _no_stderr = _spawning(lambda: _FakeWinProc(stdout=b"ok\n", stderr=b"", returncode=0))

    monkeypatch.setattr(shell_mod.sys, "platform", "win32")
    monkeypatch.setattr(shell_mod, "spawn_managed_stdio_process", _no_stderr)
    monkeypatch.setattr(shell_mod, "preserve_console_mode", _fake_preserve_console_mode)

    out = await ShellTools(runtime).execute("echo x", reason="test")
    assert "ok" in out
    assert "[stderr]" not in out
    assert "[exit_code: 0]" in out


async def test_windows_execute_pipe_timeout(runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows pipe branch: pipe streaming takes longer than timeout → ``command timed out``."""
    from chrys.service.tools.builtins import shell as shell_mod

    class _SlowStream:
        async def read(self, _size: int = -1) -> bytes:
            await asyncio.sleep(5)
            return b""

    class _SlowProc(_FakeWinProc):
        def __init__(self) -> None:
            super().__init__()
            self.stdout = _SlowStream()
            self.stderr = _SlowStream()

        async def wait(self) -> int:
            await asyncio.sleep(5)
            return 0

    _slow = _spawning(_SlowProc)

    monkeypatch.setattr(shell_mod.sys, "platform", "win32")
    monkeypatch.setattr(shell_mod, "spawn_managed_stdio_process", _slow)
    monkeypatch.setattr(shell_mod, "preserve_console_mode", _fake_preserve_console_mode)

    out = await ShellTools(runtime).execute("sleep 5", reason="test", timeout=1)
    assert out.startswith("Error: command timed out")


async def test_windows_execute_pipe_timeout_includes_partial_output_without_progress(
    runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows pipe timeouts should preserve captured stdout/stderr without a progress callback."""
    from chrys.service.tools.builtins import shell as shell_mod

    class _HangingProc(_FakeWinProc):
        def __init__(self) -> None:
            super().__init__(stdout=b"stdout-before\n", stderr=b"stderr-before\n")

        async def wait(self) -> int:
            await asyncio.sleep(5)
            return 0

    _hanging = _spawning(_HangingProc)

    monkeypatch.setattr(shell_mod.sys, "platform", "win32")
    monkeypatch.setattr(shell_mod, "spawn_managed_stdio_process", _hanging)
    monkeypatch.setattr(shell_mod, "preserve_console_mode", _fake_preserve_console_mode)

    out = await ShellTools(runtime).execute("anything", reason="test", timeout=1)

    assert out.startswith("Error: command timed out")
    assert "[partial output]" in out
    assert "stdout-before" in out
    assert "[partial stderr]" in out
    assert "stderr-before" in out


async def test_windows_execute_pipe_timeout_includes_partial_stderr_with_progress(
    runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The progress path should capture stderr chunks before EOF as well as stdout."""
    from chrys.service.tools.builtins import shell as shell_mod

    class _HangingProc(_FakeWinProc):
        def __init__(self) -> None:
            super().__init__(stdout=b"stdout-before\n", stderr=b"stderr-before\n")

        async def wait(self) -> int:
            await asyncio.sleep(5)
            return 0

    _hanging = _spawning(_HangingProc)

    monkeypatch.setattr(shell_mod.sys, "platform", "win32")
    monkeypatch.setattr(shell_mod, "spawn_managed_stdio_process", _hanging)
    monkeypatch.setattr(shell_mod, "preserve_console_mode", _fake_preserve_console_mode)

    received: list[list[str]] = []

    async def cb(lines: list[str]) -> None:
        received.append(list(lines))

    token = shell_progress_callback.set(cb)
    try:
        out = await ShellTools(runtime).execute("anything", reason="test", timeout=1)
    finally:
        shell_progress_callback.reset(token)

    assert out.startswith("Error: command timed out")
    assert "stdout-before" in out
    assert "stderr-before" in out
    assert [line for batch in received for line in batch] == ["stdout-before"]


# ───────────────────────── pipe streaming (Windows path) ───────────────


class _StreamingProc(_FakeWinProc):
    def __init__(self, out_lines: list[bytes], err: bytes = b"") -> None:
        super().__init__()
        self.stdout = _FakeStream(out_lines)
        self.stderr = _FakeStream([], tail=err)


async def _follow_pipes(proc: _StreamingProc, show: Any) -> tuple[CapturedOutput, ...]:
    """Run *proc* through the command reader as the pipe backend does, returning what each stream kept."""
    token = shell_progress_callback.set(show)
    try:
        run = _Command(
            proc,
            (proc.stdout, proc.stderr),  # type: ignore[arg-type]
            contextlib.AsyncExitStack(),
            live=proc.live_processes,
            stop=lambda _force: None,
        )
        try:
            await run.follow(show, idle=False)
            return run.captured()
        finally:
            await run.close()
    finally:
        shell_progress_callback.reset(token)


async def test_stream_pipe_emits_progress(runtime: SessionEnvironment) -> None:
    """The pipe reader passes stdout line by line to the progress callback."""
    received: list[list[str]] = []

    async def cb(lines: list[str]) -> None:
        received.append(list(lines))

    proc = _StreamingProc(
        out_lines=[b"line-one\n", b"line-two\n", b"line-three\n"],
        err=b"some stderr\n",
    )

    stdout, stderr = await _follow_pipes(proc, cb)

    # All three lines concatenated into the stdout capture
    assert stdout == CapturedOutput(b"line-one\nline-two\nline-three\n", b"", 29)
    assert stderr == CapturedOutput(b"some stderr\n", b"", 12)

    # Callback was invoked at least once (possibly coalesced) with cleaned lines
    flat = [line for batch in received for line in batch]
    assert "line-one" in flat
    assert "line-two" in flat
    assert "line-three" in flat


async def test_stream_pipe_progress_uses_subprocess_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live pipe progress should decode completed byte lines through the shared subprocess decoder."""
    from chrys.service.tools.builtins import shell as shell_mod

    received: list[list[str]] = []

    async def cb(lines: list[str]) -> None:
        received.append(list(lines))

    def decode_cp1252(raw: bytes | bytearray) -> str:
        return bytes(raw).decode("cp1252")

    monkeypatch.setattr(shell_mod, "decode_subprocess_output", decode_cp1252)
    proc = _StreamingProc(out_lines=[b"caf\xe9\n"], err=b"")

    stdout, stderr = await _follow_pipes(proc, cb)

    assert stdout == CapturedOutput(b"caf\xe9\n", b"", 5)
    assert stderr == CapturedOutput(b"", b"", 0)
    assert [line for batch in received for line in batch] == ["café"]


async def test_stream_pipe_preserves_blank_progress_lines(runtime: SessionEnvironment) -> None:
    """Windows pipe streaming should preserve blank output rows."""
    received: list[list[str]] = []

    async def cb(lines: list[str]) -> None:
        received.append(list(lines))

    proc = _StreamingProc(
        out_lines=[b"[line 1]\r\n", b"\r\n", b"[line 2]\r\n", b"\n", b"[line 3]\r\n"],
        err=b"",
    )

    stdout, stderr = await _follow_pipes(proc, cb)

    assert stdout.head == b"[line 1]\r\n\r\n[line 2]\r\n\n[line 3]\r\n"
    assert stderr.seen == 0
    flat = [line for batch in received for line in batch]
    assert flat == ["[line 1]", "", "[line 2]", "", "[line 3]"]


async def test_stream_pipe_bare_carriage_return_is_not_blank_line(runtime: SessionEnvironment) -> None:
    """A bare CR is an overwrite/progress control, not a completed row."""
    received: list[list[str]] = []

    async def cb(lines: list[str]) -> None:
        received.append(list(lines))

    proc = _StreamingProc(out_lines=[b"\r"], err=b"")

    stdout, stderr = await _follow_pipes(proc, cb)

    assert stdout == CapturedOutput(b"\r", b"", 1)
    assert stderr.seen == 0
    assert received == []


async def test_windows_pipe_progress_path_end_to_end(
    runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: with progress_cb set, the Windows branch streams progress lines."""
    from chrys.service.tools.builtins import shell as shell_mod

    _streaming = _spawning(lambda: _StreamingProc(out_lines=[b"alpha\n", b"beta\n"], err=b""))

    monkeypatch.setattr(shell_mod.sys, "platform", "win32")
    monkeypatch.setattr(shell_mod, "spawn_managed_stdio_process", _streaming)
    monkeypatch.setattr(shell_mod, "preserve_console_mode", _fake_preserve_console_mode)

    received: list[list[str]] = []

    async def cb(lines: list[str]) -> None:
        received.append(list(lines))

    token = shell_progress_callback.set(cb)
    try:
        out = await ShellTools(runtime).execute("anything", reason="test")
    finally:
        shell_progress_callback.reset(token)

    assert "alpha" in out
    assert "beta" in out
    flat = [line for batch in received for line in batch]
    assert "alpha" in flat and "beta" in flat


# ───────────────────────── constants ──────────────────────────────────


def test_shell_tool_names_contains_standard_shells() -> None:
    """Sanity-check the frozenset exported for middleware use."""
    assert "bash" in SHELL_TOOL_NAMES
    assert "zsh" in SHELL_TOOL_NAMES
    assert "pwsh" in SHELL_TOOL_NAMES
    assert "powershell" in SHELL_TOOL_NAMES
    assert "cmd" in SHELL_TOOL_NAMES
    assert "git_bash" in SHELL_TOOL_NAMES


# ───────────────────────── pipefail argv injection ────────────────────


_PIPE_CMD = "false | tail -n 1"


@pytest.mark.parametrize("name", sorted(_PIPEFAIL_SHELLS))
def test_argv_variants_inject_pipefail_for_allowlisted_shells(name: str) -> None:
    shell = ShellInfo(name=name, path=f"/bin/{name}", args=["-c"])
    (argv,) = _shell_argv_variants(shell, _PIPE_CMD)
    assert argv == (shell.path, "-o", "pipefail", "-c", _PIPE_CMD)


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("sh", ["-c"]),
        ("dash", ["-c"]),
        ("ash", ["-c"]),
        ("ksh", ["-c"]),
        ("mksh", ["-c"]),
        ("fish", ["-c"]),
        ("tcsh", ["-c"]),
        ("csh", ["-c"]),
        ("pwsh", ["-NoProfile", "-Command"]),
        ("powershell", ["-NoProfile", "-Command"]),
        ("cmd", ["/c"]),
    ],
)
def test_argv_variants_unlisted_shells_get_no_injection(name: str, args: list[str]) -> None:
    """Shells outside the allowlist launch byte-for-byte as before."""
    shell = ShellInfo(name=name, path=f"/bin/{name}", args=args)
    (argv,) = _shell_argv_variants(shell, _PIPE_CMD)
    assert argv == (shell.path, *args, _PIPE_CMD)


def test_argv_variants_traced_and_bare_share_pipefail() -> None:
    """The fsatrace-wrapped and bare variants must behave identically."""
    from chrys.service.mutations.trace import shell_trace_argv

    shell = ShellInfo(name="bash", path="/bin/bash", args=["-c"])
    prefix = ("/opt/fsatrace/fsatrace", "rwmdq", "/dev/null", "--")
    token = shell_trace_argv.set(prefix)
    try:
        traced, bare = _shell_argv_variants(shell, _PIPE_CMD)
    finally:
        shell_trace_argv.reset(token)
    assert bare == (shell.path, "-o", "pipefail", "-c", _PIPE_CMD)
    assert traced == (*prefix, *bare)
