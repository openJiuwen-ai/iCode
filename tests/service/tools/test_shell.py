# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for cross-platform shell execution tools."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import sys
import time
from pathlib import Path

import pytest

import chrys.service.tools.builtins.shell as shell_module
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.platform import ShellInfo
from chrys.foundation.platform.runtime_paths import build_shell_subprocess_env
from chrys.foundation.text.tool_output import truncate_output as _truncate_output
from chrys.service.tools.builtins.shell import (
    ShellTools,
    _await_pty_master_drained,
    _prepare_pipe_command,
    _process_carriage_returns,
    _strip_ansi,
    shell_progress_callback,
)
from tests.support.gc_isolation import earlier_tests_heap_frozen
from tests.support.pty_masters import linux_style_master
from tests.support.waiting import wait_for

IS_UNIX = sys.platform != "win32"


def _python_cwd_command(shell_name: str) -> str:
    """Return a shell-specific command that prints the child process cwd."""
    code = "from pathlib import Path; print(Path.cwd().resolve())"
    if shell_name in {"pwsh", "powershell"}:
        executable = sys.executable.replace("'", "''")
        script = code.replace("'", "''")
        return f"& '{executable}' -c '{script}'"
    if shell_name == "cmd":
        return f'"{sys.executable}" -c "{code}"'
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"


@pytest.fixture
def shell() -> ShellTools:
    runtime = SessionEnvironment.capture()
    return ShellTools(runtime)


# ───────────────────────── Integration tests ──────────────────────────


async def test_execute_simple_command(shell: ShellTools) -> None:
    result = await shell.execute("echo hello", reason="test")
    assert "hello" in result
    assert "[exit_code: 0]" in result


async def test_execute_pipe_closes_stdin_at_once(shell: ShellTools, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows pipe path gives the command an input that has already ended.

    This stops a child TUI from inheriting the outer chrys's console
    input handle and calling ``SetConsoleMode`` on it (which would
    wipe the outer TUI's mouse / keyboard flags).  The Unix PTY path uses
    its own DEVNULL assertion below.
    """
    from tests.support.processes import ExitedProcess

    class _Proc(ExitedProcess):
        stdin_closed = False

        def close_stdin(self) -> None:
            self.stdin_closed = True

        def close_transports(self) -> None:
            pass

        def kill_tree(self) -> None:
            pass

        def live_processes(self) -> dict[int, float]:
            return {}

    proc = _Proc()

    async def spawn(*_args: object, **_kwargs: object) -> _Proc:
        return proc

    from chrys.service.tools.builtins import shell as shell_mod

    monkeypatch.setattr(shell_mod, "spawn_managed_stdio_process", spawn)
    await shell._execute_pipe("echo hi", shell._shell, cwd=".", timeout=5)

    assert proc.stdin_closed


async def test_execute_python_cjk_output(shell: ShellTools, tmp_path: Path) -> None:
    """Python subprocess printing CJK text should produce correct UTF-8 output.

    ``build_shell_subprocess_env`` sets ``PYTHONUTF8=1`` so Python children
    always encode stdout as UTF-8 regardless of the system locale.

    The CJK literal lives in a UTF-8 script file (not in ``-c`` on the
    command line) so the test is independent of the host's command-line
    encoding.  On Windows the ANSI code page mojibakes non-ASCII argv
    to ``?``; putting the literal in a file sidesteps that entirely.
    On Windows we also prefix with ``&`` — PowerShell's call operator —
    because a quoted path at the start of a command is otherwise parsed
    as a string expression, not a command invocation.
    """
    script = tmp_path / "cjk_output.py"
    script.write_text("print('你好世界')\n", encoding="utf-8")
    prefix = "& " if sys.platform == "win32" else ""
    cmd = f'{prefix}"{sys.executable}" "{script}"'
    result = await shell.execute(cmd, reason="test")
    assert "你好世界" in result
    assert "[exit_code: 0]" in result


async def test_execute_stderr(shell: ShellTools) -> None:
    result = await shell.execute("echo err >&2", reason="test")
    assert "err" in result
    if IS_UNIX:
        # PTY merges stdout/stderr — no [stderr] marker
        assert "[stderr]" not in result
    else:
        assert "[stderr]" in result


async def test_execute_nonzero_exit(shell: ShellTools) -> None:
    result = await shell.execute("exit 42", reason="test")
    assert "[exit_code: 42]" in result


async def test_execute_timeout(shell: ShellTools) -> None:
    result = await shell.execute("sleep 5", reason="test", timeout=0.1)
    assert "timed out" in result


@pytest.mark.skipif(not IS_UNIX, reason="POSIX stopped state only")
async def test_execute_stopped_process_returns_promptly(shell: ShellTools) -> None:
    script = "import os, signal, time; os.kill(os.getpid(), signal.SIGSTOP); time.sleep(30)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} | tail -n 20"

    with earlier_tests_heap_frozen():
        started = time.monotonic()
        result = await shell.execute(command, reason="test", timeout=10)
        elapsed = time.monotonic() - started

    assert "stopped state" in result
    assert elapsed < 5


@pytest.mark.skipif(not IS_UNIX, reason="PTY path is Unix-only")
async def test_pty_stdio_contract(shell: ShellTools) -> None:
    script = "import sys; print(sys.stdin.isatty(), sys.stdout.isatty(), sys.stderr.isatty())"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    result = await shell.execute(command, reason="test", timeout=5)

    assert "False True True" in result
    assert "[exit_code: 0]" in result


@pytest.mark.skipif(not IS_UNIX, reason="PTY path is Unix-only")
async def test_pty_has_no_controlling_terminal(shell: ShellTools) -> None:
    script = (
        "import errno, os\n"
        "try:\n"
        "    fd = os.open('/dev/tty', os.O_RDONLY)\n"
        "except OSError as exc:\n"
        "    print('tty_error=' + errno.errorcode.get(exc.errno, str(exc.errno)))\n"
        "else:\n"
        "    os.close(fd); print('tty_opened')"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"

    result = await shell.execute(command, reason="test", timeout=5)

    assert "tty_error=" in result
    assert "tty_opened" not in result
    assert "[exit_code: 0]" in result


@pytest.mark.skipif(not IS_UNIX, reason="PTY path is Unix-only")
async def test_pty_background_process_group_reads_stdin_as_eof(shell: ShellTools) -> None:
    script = "import os; os.setpgid(0, 0); print('stdin=' + repr(os.read(0, 1)), flush=True)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} | tail -n 20"

    result = await shell.execute(command, reason="test", timeout=5)

    assert "stdin=b''" in result
    assert "[exit_code: 0]" in result


@pytest.mark.skipif(not IS_UNIX, reason="PTY path is Unix-only")
async def test_pty_background_process_group_cannot_read_dev_tty(shell: ShellTools) -> None:
    script = (
        "import errno, os\n"
        "os.setpgid(0, 0)\n"
        "try:\n"
        "    fd = os.open('/dev/tty', os.O_RDONLY)\n"
        "except OSError as exc:\n"
        "    print('tty_error=' + errno.errorcode.get(exc.errno, str(exc.errno)), flush=True)\n"
        "else:\n"
        "    print('tty_opened', flush=True); print(os.read(fd, 1), flush=True)"
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)} | tail -n 20"

    result = await shell.execute(command, reason="test", timeout=5)

    assert "tty_error=" in result
    assert "stopped state" not in result
    assert "[exit_code: 0]" in result


@pytest.mark.skipif(not IS_UNIX, reason="PTY path is Unix-only")
async def test_pty_execute_does_not_leak_slave_fd(shell: ShellTools) -> None:
    """The PTY backend holds its slave fd open until drain finishes, then must
    release it on every run — verify repeated runs do not leak file descriptors."""
    import gc

    await shell.execute("echo warmup", reason="test", timeout=5)
    gc.collect()
    before = len(os.listdir("/dev/fd"))
    for _ in range(25):
        result = await shell.execute("echo out && echo err >&2", reason="test", timeout=5)
        assert "out" in result and "err" in result
    gc.collect()
    after = len(os.listdir("/dev/fd"))
    assert after - before <= 2, f"slave fd leak across runs: {before} -> {after}"


@pytest.mark.skipif(not IS_UNIX, reason="PTY path is Unix-only")
async def test_pty_drain_waits_until_the_reader_has_taken_everything() -> None:
    """The parent's slave fd must not close while the tty still holds output.

    macOS discards unread tty data the moment the last slave fd closes, so
    returning here with bytes still buffered loses a command's entire output and
    leaves only its exit code.
    """
    import pty
    import select

    master, slave = pty.openpty()
    consumed = b""

    async def consumer() -> None:
        nonlocal consumed
        await asyncio.sleep(0.05)
        consumed = os.read(master, 4096)

    try:
        os.write(slave, b"hello\n")
        reader = asyncio.create_task(consumer())
        await _await_pty_master_drained(master, grace=5.0)

        assert consumed.startswith(b"hello"), "returned before the reader took the pending output"
        assert select.select([master], [], [], 0)[0] == []
        await reader
    finally:
        os.close(slave)
        os.close(master)


@pytest.mark.skipif(not IS_UNIX, reason="PTY path is Unix-only")
async def test_pty_drain_is_bounded_when_the_reader_never_catches_up() -> None:
    """A reader paused by flow control must not stall the command forever."""
    import pty

    master, slave = pty.openpty()
    try:
        os.write(slave, b"never read\n")
        started = time.monotonic()
        await _await_pty_master_drained(master, grace=0.1)

        assert time.monotonic() - started < 2.0
    finally:
        os.close(slave)
        os.close(master)


@pytest.mark.skipif(not IS_UNIX, reason="PTY path is Unix-only")
async def test_pty_drain_returns_at_once_with_nothing_pending() -> None:
    import pty

    master, slave = pty.openpty()
    try:
        started = time.monotonic()
        await _await_pty_master_drained(master, grace=5.0)

        assert time.monotonic() - started < 1.0
    finally:
        os.close(slave)
        os.close(master)


@pytest.mark.skipif(not IS_UNIX, reason="PTY path is Unix-only")
async def test_pty_drain_gives_up_on_a_closed_descriptor() -> None:
    """A master closed underneath us reports EBADF; that is a finished drain."""
    import pty

    master, slave = pty.openpty()
    os.close(slave)
    os.close(master)

    await _await_pty_master_drained(master, grace=5.0)


@pytest.mark.skipif(not IS_UNIX, reason="PTY path is Unix-only")
async def test_execute_with_pty_descriptor_above_select_limit(
    shell: ShellTools, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A busy process can allocate its PTY beyond select()'s FD_SETSIZE."""
    import errno
    import fcntl
    import pty

    openpty = pty.openpty
    high_descriptors: list[int] = []

    def high_descriptor_pty() -> tuple[int, int]:
        master, slave = openpty()
        try:
            high = fcntl.fcntl(master, fcntl.F_DUPFD, 1024)
        except OSError as error:
            os.close(slave)
            if error.errno in {errno.EINVAL, errno.EMFILE}:
                pytest.skip("Open-file limit does not allow descriptors above 1023")
            raise
        finally:
            os.close(master)
        high_descriptors.append(high)
        return high, slave

    monkeypatch.setattr(pty, "openpty", high_descriptor_pty)

    result = await shell.execute("echo hello", reason="test", timeout=5)

    assert high_descriptors and min(high_descriptors) >= 1024
    assert "hello" in result
    assert "[exit_code: 0]" in result


# ───────────────────── Pipeline exit status (pipefail) ─────────────────────


@pytest.fixture
def pipeline_status_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep user shell config out of pipeline-status assertions.

    ``$BASH_ENV`` can revert the injected pipefail after argv parsing; an
    inherited ``SHELLOPTS=pipefail`` can enable it where chrys does not
    inject.  Both would flip the expected exit codes below.
    """
    monkeypatch.delenv("BASH_ENV", raising=False)
    monkeypatch.delenv("SHELLOPTS", raising=False)


def _shell_tools_for(name: str) -> ShellTools:
    """ShellTools pinned to *name* resolved from PATH; skip when absent."""
    path = shutil.which(name)
    if path is None:
        pytest.skip(f"{name} not available on this system")
    runtime = SessionEnvironment.capture()
    return ShellTools(runtime, shell=ShellInfo(name=name, path=path, args=["-c"]))


@pytest.mark.skipif(not IS_UNIX, reason="PTY not available on Windows")
async def test_pty_pipeline_propagates_upstream_failure(pipeline_status_env: None) -> None:
    shell = _shell_tools_for("bash")
    result = await shell.execute("exit 3 | tail -n 1", reason="pipeline repro")
    assert "[exit_code: 3]" in result


@pytest.mark.skipif(not IS_UNIX, reason="PTY not available on Windows")
async def test_pty_pipeline_success_still_returns_zero(pipeline_status_env: None) -> None:
    shell = _shell_tools_for("bash")
    result = await shell.execute("true | tail -n 1", reason="pipeline control")
    assert "[exit_code: 0]" in result


@pytest.mark.skipif(not IS_UNIX, reason="PTY not available on Windows")
async def test_pty_non_pipeline_exit_code_unchanged(pipeline_status_env: None) -> None:
    shell = _shell_tools_for("bash")
    result = await shell.execute("exit 7", reason="non-pipeline control")
    assert "[exit_code: 7]" in result


@pytest.mark.skipif(not IS_UNIX, reason="PTY not available on Windows")
async def test_pty_pipeline_unlisted_shell_keeps_native_semantics(pipeline_status_env: None) -> None:
    """Shells outside the pipefail allowlist get no injection: a POSIX ``sh``
    pipeline still reports the last stage's status."""
    shell = _shell_tools_for("sh")
    result = await shell.execute("false | tail -n 1", reason="no pipefail injection")
    assert "[exit_code: 0]" in result


async def test_execute_with_working_dir(shell: ShellTools, tmp_path: str) -> None:
    result = await shell.execute("pwd", reason="test", working_dir=str(tmp_path))
    assert "[exit_code: 0]" in result


async def test_execute_with_relative_working_dir_resolves_against_runtime_cwd(tmp_path: Path) -> None:
    runtime = SessionEnvironment.capture()
    workspace = tmp_path / "workspace"
    subdir = workspace / "subdir"
    subdir.mkdir(parents=True)
    shell = ShellTools(
        SessionEnvironment(
            cwd=str(workspace),
            platform=runtime.platform,
            session_id=runtime.session_id,
        )
    )

    cmd = _python_cwd_command(shell._shell.name)
    result = await shell.execute(cmd, reason="test", working_dir="subdir")

    assert "[exit_code: 0]" in result
    assert str(subdir.resolve()) in result


async def test_execute_uses_launch_dir(tmp_path: str) -> None:
    """execute should use SessionEnvironment.cwd as default working directory."""
    runtime = SessionEnvironment.capture()
    runtime_with_tmp = SessionEnvironment(
        cwd=str(tmp_path),
        platform=runtime.platform,
        session_id=runtime.session_id,
    )
    shell = ShellTools(runtime_with_tmp)
    result = await shell.execute("pwd", reason="test")
    assert str(tmp_path) in result


def test_tools_returns_bound_tools() -> None:
    runtime = SessionEnvironment.capture()
    shell = ShellTools(runtime)
    tools = shell.tools()
    assert len(tools) == 1
    assert tools[0].name == runtime.platform.shell.name


def test_tools_description_includes_shell_info() -> None:
    runtime = SessionEnvironment.capture()
    shell = ShellTools(runtime)
    tools = shell.tools()
    desc = tools[0].description
    assert runtime.platform.shell.name in desc
    assert runtime.platform.shell.path in desc


def test_tools_description_includes_guidance() -> None:
    runtime = SessionEnvironment.capture()
    shell = ShellTools(runtime)
    tools = shell.tools()
    desc = tools[0].description
    assert "truncated" in desc.lower()
    assert "interactive" in desc.lower()


# ───────────────────────── Output truncation ──────────────────────────


async def test_execute_with_max_tokens(shell: ShellTools) -> None:
    """Large output is truncated when max_tokens is small."""
    if sys.platform == "win32":
        cmd = 'uv run python -c "for i in range(10000): print(i)"'
    else:
        cmd = "seq 1 10000"
    result = await shell.execute(cmd, reason="test", max_tokens=200)
    assert "truncated" in result.lower()
    assert "[exit_code: 0]" in result


def test_truncate_short_unchanged() -> None:
    text = "line 1\nline 2\nline 3"
    assert _truncate_output(text, 5000) == text


def test_truncate_head_tail_ratio() -> None:
    # Build text large enough to require truncation at budget=50
    lines = [f"line {i}" for i in range(200)]
    text = "\n".join(lines)
    result = _truncate_output(text, 50)

    assert "truncated" in result.lower()
    # Should have head, marker, and tail
    parts = result.split("[...")
    assert len(parts) == 2  # head part + "truncated ...]\ntail"

    # Head should be shorter than tail (1:2 ratio)
    head_part = parts[0].strip()
    tail_part = parts[1].split("...]", 1)[1].strip()
    head_count = len(head_part.splitlines()) if head_part else 0
    tail_count = len(tail_part.splitlines()) if tail_part else 0
    # Tail should have at least as many lines as head (2:1 ratio)
    assert tail_count >= head_count


def test_truncate_preserves_complete_lines() -> None:
    lines = [f"line {i}" for i in range(200)]
    text = "\n".join(lines)
    result = _truncate_output(text, 50)
    # Every non-marker line should be a complete original line
    for line in result.splitlines():
        if "truncated" not in line.lower():
            assert line in lines


def test_truncate_marker_contains_info() -> None:
    lines = [f"line {i}" for i in range(200)]
    text = "\n".join(lines)
    result = _truncate_output(text, 50)
    # Marker should contain line count and token info
    assert "lines" in result
    assert "tokens" in result


# ───────────────────────── ANSI stripping ─────────────────────────────


def test_strip_ansi_csi_colors() -> None:
    assert _strip_ansi("\x1b[31mred\x1b[0m") == "red"


def test_strip_ansi_bold_underline() -> None:
    assert _strip_ansi("\x1b[1m\x1b[4mbold underline\x1b[0m") == "bold underline"


def test_strip_ansi_osc_title() -> None:
    assert _strip_ansi("\x1b]0;window title\x07text") == "text"


def test_strip_ansi_osc_st_terminator() -> None:
    assert _strip_ansi("\x1b]0;title\x1b\\text") == "text"


def test_strip_ansi_cursor_movement() -> None:
    assert _strip_ansi("\x1b[2J\x1b[Htext") == "text"


def test_strip_ansi_preserves_plain_text() -> None:
    text = "hello world\nno escapes here"
    assert _strip_ansi(text) == text


def test_strip_ansi_mixed() -> None:
    text = "\x1b[32mgreen\x1b[0m normal \x1b[1mbold\x1b[0m"
    assert _strip_ansi(text) == "green normal bold"


# ──────────────────── Carriage return handling ────────────────────────


def test_cr_progress_bar() -> None:
    text = "Progress: 10%\rProgress: 50%\rProgress: 100%"
    assert _process_carriage_returns(text) == "Progress: 100%"


def test_cr_multiline() -> None:
    text = "normal line\nfoo\rbar\nanother normal"
    result = _process_carriage_returns(text)
    assert result == "normal line\nbar\nanother normal"


def test_cr_no_cr_passthrough() -> None:
    text = "line 1\nline 2\nline 3"
    assert _process_carriage_returns(text) == text


def test_cr_empty_overwrite() -> None:
    text = "something\r"
    assert _process_carriage_returns(text) == ""


# ──────────────────── PTY execution (Unix only) ──────────────────────


@pytest.mark.skipif(not IS_UNIX, reason="PTY not available on Windows")
async def test_pty_basic(shell: ShellTools) -> None:
    result = await shell.execute("echo pty_test", reason="test")
    assert "pty_test" in result
    assert "[exit_code: 0]" in result


@pytest.mark.skipif(not IS_UNIX, reason="PTY not available on Windows")
@pytest.mark.parametrize("newline", [b"\n", b"\r\n"], ids=["lf", "crlf"])
@pytest.mark.parametrize("with_progress", [False, True], ids=["buffered", "streamed"])
async def test_pty_cat_preserves_file_contents(tmp_path: Path, newline: bytes, with_progress: bool) -> None:
    """Real PTY output must retain file text in both progress and the model result."""
    expected = "# Title\n\n正文 body\n"
    path = tmp_path / "line endings.md"
    path.write_bytes(expected.encode("utf-8").replace(b"\n", newline))
    shell = ShellTools(SessionEnvironment.capture(), shell=ShellInfo(name="sh", path="/bin/sh", args=["-c"]))
    received: list[str] = []

    async def collect(lines: list[str]) -> None:
        received.extend(lines)

    token = shell_progress_callback.set(collect if with_progress else None)
    try:
        result = await shell.execute(f"cat {shlex.quote(str(path))}", reason="test", timeout=5)
    finally:
        shell_progress_callback.reset(token)

    assert result == f"{expected}\n[exit_code: 0]"
    if with_progress:
        assert received == ["# Title", "", "正文 body"]
        assert result.removesuffix("\n[exit_code: 0]") == "\n".join(received) + "\n"


@pytest.mark.skipif(not IS_UNIX, reason="PTY not available on Windows")
async def test_pty_output_written_while_progress_is_being_shown_is_kept_to_the_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The program may write its last words and exit while the progress callback is still busy.

    Linux then reports the end as an I/O error on the master, which a `StreamReader` raises ahead of
    handing over what it had already read.
    """
    master, master_ended = linux_style_master()
    monkeypatch.setattr(shell_module, "PtyOutputProtocol", master)
    monkeypatch.setattr(shell_module, "_PROGRESS_THROTTLE_INTERVAL", 0.0)
    gate = tmp_path / "gate"
    os.mkfifo(gate)
    shell = ShellTools(SessionEnvironment.capture(), shell=ShellInfo(name="sh", path="/bin/sh", args=["-c"]))
    shown: list[str] = []

    def open_the_gate() -> bool:
        try:
            fd = os.open(gate, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            # Nobody is reading it yet.
            return False
        os.write(fd, b"\n")
        os.close(fd)
        return True

    async def show(lines: list[str]) -> None:
        first = not shown
        shown.extend(lines)
        if first:
            # Busy from before the program's last words until the master has failed over them.
            await wait_for(open_the_gate, description="the program to wait at the gate")
            await wait_for(master_ended.is_set, description="the master to end")

    token = shell_progress_callback.set(show)
    try:
        result = await shell.execute(
            f"printf 'first\\n'; read _ < {shlex.quote(str(gate))}; printf 'last words\\n'",
            reason="test",
            timeout=10,
        )
    finally:
        shell_progress_callback.reset(token)

    assert result == "first\nlast words\n\n[exit_code: 0]"
    assert shown == ["first", "last words"]


@pytest.mark.skipif(not IS_UNIX, reason="PTY not available on Windows")
async def test_pty_preserves_program_emitted_crlf() -> None:
    """CRLF output such as HTTP headers must survive without any file involved."""
    shell = ShellTools(SessionEnvironment.capture(), shell=ShellInfo(name="sh", path="/bin/sh", args=["-c"]))

    result = await shell.execute(
        r"printf 'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nbody\n'",
        reason="test",
        timeout=5,
    )

    assert result == "HTTP/1.1 200 OK\nContent-Type: text/plain\n\nbody\n\n[exit_code: 0]"


@pytest.mark.skipif(not IS_UNIX, reason="PTY not available on Windows")
async def test_pty_isatty(shell: ShellTools) -> None:
    result = await shell.execute('python3 -c "import sys; print(sys.stdout.isatty())"', reason="test")
    assert "True" in result


@pytest.mark.skipif(not IS_UNIX, reason="PTY not available on Windows")
async def test_pty_timeout(shell: ShellTools) -> None:
    result = await shell.execute("sleep 5", reason="test", timeout=0.1)
    assert "timed out" in result


@pytest.mark.skipif(not IS_UNIX, reason="PTY not available on Windows")
async def test_pty_term_dumb(shell: ShellTools) -> None:
    result = await shell.execute("echo $TERM", reason="test")
    assert "dumb" in result


@pytest.mark.skipif(not IS_UNIX, reason="PTY not available on Windows")
async def test_pty_merged_output(shell: ShellTools) -> None:
    """PTY merges stdout and stderr — both appear but no [stderr] marker."""
    result = await shell.execute("echo out && echo err >&2", reason="test")
    assert "out" in result
    assert "err" in result
    assert "[stderr]" not in result


# ──────────────── _truncate_output head_ratio parameter ─────────────────


def test_truncate_head_ratio_default_favours_tail() -> None:
    """Default head_ratio=1/3 gives more lines to the tail."""
    lines = [f"line {i}" for i in range(200)]
    text = "\n".join(lines)
    result = _truncate_output(text, 50)
    parts = result.split("[...")
    head_part = parts[0].strip()
    tail_part = parts[1].split("...]", 1)[1].strip()
    head_count = len(head_part.splitlines()) if head_part else 0
    tail_count = len(tail_part.splitlines()) if tail_part else 0
    assert tail_count > head_count


def test_truncate_head_ratio_two_thirds_favours_head() -> None:
    """head_ratio=2/3 gives more lines to the head (stderr-style)."""
    lines = [f"line {i}" for i in range(200)]
    text = "\n".join(lines)
    result = _truncate_output(text, 50, head_ratio=2 / 3)
    parts = result.split("[...")
    head_part = parts[0].strip()
    tail_part = parts[1].split("...]", 1)[1].strip()
    head_count = len(head_part.splitlines()) if head_part else 0
    tail_count = len(tail_part.splitlines()) if tail_part else 0
    assert head_count > tail_count


def test_truncate_head_ratio_half_roughly_equal() -> None:
    """head_ratio=0.5 gives roughly equal head and tail."""
    lines = [f"line {i}" for i in range(200)]
    text = "\n".join(lines)
    result = _truncate_output(text, 50, head_ratio=0.5)
    parts = result.split("[...")
    head_part = parts[0].strip()
    tail_part = parts[1].split("...]", 1)[1].strip()
    head_count = len(head_part.splitlines()) if head_part else 0
    tail_count = len(tail_part.splitlines()) if tail_part else 0
    assert abs(head_count - tail_count) <= 5


def test_truncate_head_ratio_no_truncation_when_fits() -> None:
    """head_ratio doesn't matter if text fits within budget."""
    text = "short text"
    assert _truncate_output(text, 5000, head_ratio=0.9) == text


# ──────────────── ANSI stripping on stderr (Windows pipe path) ──────────


def test_strip_ansi_powershell_error() -> None:
    """PowerShell-style red bold error should be fully cleaned."""
    raw = "\x1b[31;1mxargs: \x1b[31;1mThe term 'xargs' is not recognized\x1b[0m"
    result = _strip_ansi(raw)
    assert "\x1b" not in result
    assert "[31;1m" not in result
    assert "[0m" not in result
    assert "xargs:" in result
    assert "The term 'xargs' is not recognized" in result


def test_strip_ansi_nested_bold_color() -> None:
    """Nested ANSI sequences (bold + color + reset) are all removed."""
    raw = "\x1b[1m\x1b[31mError:\x1b[0m \x1b[33mwarning\x1b[0m"
    assert _strip_ansi(raw) == "Error: warning"


@pytest.mark.skipif(not IS_UNIX, reason="Integration test uses PTY path on Unix")
async def test_pty_strips_ansi_from_output(shell: ShellTools) -> None:
    """Even if a program forces ANSI output, the result should be clean."""
    result = await shell.execute(
        r'printf "\x1b[31mred\x1b[0m normal"',
        reason="test",
    )
    assert "\x1b" not in result
    assert "[31m" not in result
    assert "red" in result
    assert "normal" in result


# ──────────────── build_shell_subprocess_env ──────────────────────────────────


def test_build_env_sets_term_dumb() -> None:
    env = build_shell_subprocess_env()
    assert env["TERM"] == "dumb"


def test_build_env_sets_no_color() -> None:
    env = build_shell_subprocess_env()
    assert env["NO_COLOR"] == "1"


def test_build_env_sets_chrys_marker() -> None:
    env = build_shell_subprocess_env()
    assert env["CHRYS"] == "1"


def test_build_env_disables_pager() -> None:
    env = build_shell_subprocess_env()
    assert env["GIT_PAGER"] == "cat"
    assert env["PAGER"] == "cat"


def test_build_env_removes_color_forcing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Colour-forcing env vars are removed even if present in the parent env."""
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.setenv("CLICOLOR_FORCE", "1")
    env = build_shell_subprocess_env()
    assert "FORCE_COLOR" not in env
    assert "COLORTERM" not in env
    assert "CLICOLOR_FORCE" not in env


def test_build_env_inherits_path() -> None:
    """PATH and other standard vars are inherited from the parent env."""
    env = build_shell_subprocess_env()
    assert "PATH" in env


def test_build_env_demotes_runtime_path_when_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Packaged Chrys shell children should prefer system executables on PATH."""
    from chrys.foundation.platform import runtime_paths

    runtime_bin = tmp_path / "runtime" / "bin"
    system_bin = tmp_path / "system" / "bin"
    monkeypatch.setattr(runtime_paths.sys, "executable", str(runtime_bin / "python"))
    monkeypatch.setattr(runtime_paths.sys, "prefix", str(tmp_path / "runtime"))
    monkeypatch.setattr(runtime_paths.sys, "exec_prefix", str(tmp_path / "runtime"))
    monkeypatch.setattr(
        runtime_paths.sysconfig,
        "get_path",
        lambda name: str(runtime_bin) if name == "scripts" else "",
    )
    monkeypatch.setenv("PYAPP", "1")
    monkeypatch.setenv("PATH", os.pathsep.join([str(runtime_bin), str(system_bin)]))

    env = build_shell_subprocess_env()
    path_key = runtime_paths.path_env_key(env)

    assert path_key is not None
    assert env[path_key].split(os.pathsep)[:2] == [str(system_bin), str(runtime_bin)]


def test_build_env_forces_utf8() -> None:
    """Subprocess env forces UTF-8 output for Python (and compatible tools)."""
    env = build_shell_subprocess_env()
    assert env["PYTHONUTF8"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8"


@pytest.mark.parametrize("shell_name", ["pwsh", "powershell"])
def test_prepare_pipe_command_sets_powershell_utf8(shell_name: str) -> None:
    shell = ShellInfo(name=shell_name, path=shell_name, args=["-NoProfile", "-Command"])
    command = _prepare_pipe_command("Write-Output 'ok'", shell)
    assert "[Console]::OutputEncoding = $__chrysUtf8" in command
    assert "} catch { }" in command
    assert "$OutputEncoding = $__chrysUtf8" in command
    assert "$PSDefaultParameterValues" not in command
    assert command.endswith("Write-Output 'ok'")


def test_prepare_pipe_command_leaves_non_powershell_unchanged() -> None:
    shell = ShellInfo(name="cmd", path="cmd.exe", args=["/c"])
    assert _prepare_pipe_command("echo ok", shell) == "echo ok"


def test_shell_tools_names_pwsh_tool_when_shell_is_pwsh() -> None:
    runtime = SessionEnvironment.capture()
    shell = ShellInfo(
        name="pwsh",
        path=r"C:\Program Files\PowerShell\7\pwsh.exe",
        args=["-NoProfile", "-Command"],
        version="7.5.0",
    )

    tools = ShellTools(runtime, shell=shell).tools()

    execute = tools[0]
    assert execute.name == "pwsh"
    assert "Execute a command in pwsh 7.5.0" in execute.description
    assert r"(C:\Program Files\PowerShell\7\pwsh.exe)" in execute.description


@pytest.mark.parametrize(("shell_name", "version"), [("pwsh", "7.5.0"), ("powershell", "5.1")])
def test_powershell_tool_description_uses_powershell_output_guidance(shell_name: str, version: str) -> None:
    runtime = SessionEnvironment.capture()
    shell = ShellInfo(
        name=shell_name,
        path=f"{shell_name}.exe",
        args=["-NoProfile", "-Command"],
        version=version,
    )

    execute = ShellTools(runtime, shell=shell).tools()[0]
    description = execute.description

    assert "Select-Object -First/-Last" in description
    assert "Get-Content -Tail" in description
    assert "Prefer full cmdlet names over aliases" in description
    assert "head, tail, or grep" not in description


@pytest.mark.parametrize("shell_name", ["bash", "zsh", "git_bash"])
def test_pipefail_shell_description_documents_pipeline_semantics(shell_name: str) -> None:
    runtime = SessionEnvironment.capture()
    shell = ShellInfo(name=shell_name, path=f"/bin/{shell_name}", args=["-c"])

    description = ShellTools(runtime, shell=shell).tools()[0].description

    assert "pipefail" in description
    assert "Avoid head" in description
    assert "pipe through head" not in description
    # The guidance string must end with a trailing space — it is concatenated
    # directly against the next sentence in the tool description.
    assert "status. Set reason" in description


@pytest.mark.parametrize("shell_name", ["sh", "dash", "fish", "tcsh"])
def test_unlisted_shell_description_makes_no_pipefail_claim(shell_name: str) -> None:
    runtime = SessionEnvironment.capture()
    shell = ShellInfo(name=shell_name, path=f"/bin/{shell_name}", args=["-c"])

    description = ShellTools(runtime, shell=shell).tools()[0].description

    assert "pipefail" not in description
    assert "pipe through head, tail, or grep" in description


def test_powershell_tool_descriptions_prefer_read_file_and_windows_powershell_requires_utf8() -> None:
    runtime = SessionEnvironment.capture()

    powershell = ShellInfo(
        name="powershell",
        path="powershell.exe",
        args=["-NoProfile", "-Command"],
        version="5.1",
    )
    pwsh = ShellInfo(
        name="pwsh",
        path="pwsh.exe",
        args=["-NoProfile", "-Command"],
        version="7.5.0",
    )

    powershell_description = ShellTools(runtime, shell=powershell).tools()[0].description
    pwsh_description = ShellTools(runtime, shell=pwsh).tools()[0].description

    assert "use the read_file tool instead of Get-Content (if present)" in powershell_description
    assert "use the read_file tool instead of Get-Content (if present)" in pwsh_description
    assert "Get-Content in Windows PowerShell 5.x" in powershell_description
    assert "-Encoding UTF8" in powershell_description
    assert "ANSI code page" in powershell_description
    assert "Get-Content in Windows PowerShell 5.x" not in pwsh_description
    assert "-Encoding UTF8" not in pwsh_description


# ──────────────── PTY NO_COLOR integration ──────────────────────────────


@pytest.mark.skipif(not IS_UNIX, reason="PTY not available on Windows")
async def test_pty_no_color_set(shell: ShellTools) -> None:
    """PTY subprocess should see NO_COLOR=1."""
    result = await shell.execute("echo $NO_COLOR", reason="test")
    assert "1" in result


# ──────────────── Truncation tail ordering ──────────────────────────────


def test_truncate_tail_lines_preserve_order() -> None:
    """Tail lines should appear in their original order after truncation."""
    lines = [f"line {i:04d}" for i in range(500)]
    text = "\n".join(lines)
    result = _truncate_output(text, 100)
    # Extract tail lines (after the truncation marker)
    parts = result.split("...]")
    assert len(parts) == 2
    tail_part = parts[1].strip()
    tail_lines = tail_part.splitlines()
    # Each successive tail line should have a larger number
    nums = [int(tl.split()[-1]) for tl in tail_lines if tl.startswith("line")]
    assert nums == sorted(nums)
    assert len(nums) > 1


def test_truncate_head_lines_preserve_order() -> None:
    """Head lines should appear in their original order after truncation."""
    lines = [f"line {i:04d}" for i in range(500)]
    text = "\n".join(lines)
    result = _truncate_output(text, 100)
    parts = result.split("[...")
    head_part = parts[0].strip()
    head_lines = head_part.splitlines()
    nums = [int(hl.split()[-1]) for hl in head_lines if hl.startswith("line")]
    assert nums == sorted(nums)
    assert nums[0] == 0  # starts from the beginning


# ──────────────── Layer-3 trace wrapper (fsatrace argv prefix) ────────────────


def _exec_passthrough_tracer(tmp_path: Path) -> tuple[tuple[str, ...], Path]:
    """An argv prefix that drops a marker, then execs the remaining argv.

    Stands in for fsatrace so the wrap itself is observable without the
    real binary; ``execv`` preserves the PTY/pipe/process-group contract
    exactly like the real tracer does.
    """
    marker = tmp_path / "wrapped.marker"
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "import os, sys\n" + f"open({str(marker)!r}, 'a').write('wrapped')\n" + "os.execv(sys.argv[1], sys.argv[1:])\n"
    )
    return (sys.executable, str(wrapper)), marker


@pytest.mark.skipif(not IS_UNIX, reason="PTY execution is Unix-only")
async def test_pty_trace_prefix_wraps_execution(shell: ShellTools, tmp_path: Path) -> None:
    from chrys.service.mutations.trace import shell_trace_argv

    prefix, marker = _exec_passthrough_tracer(tmp_path)
    token = shell_trace_argv.set(prefix)
    try:
        output, returncode = await shell._execute_pty("echo traced-hello", shell._shell, cwd=".", timeout=15)
    finally:
        shell_trace_argv.reset(token)
    assert returncode == 0
    assert "traced-hello" in output.text()
    assert marker.exists()  # the wrapper really interposed


@pytest.mark.skipif(not IS_UNIX, reason="execv passthrough semantics are POSIX")
async def test_pipe_trace_prefix_wraps_execution(shell: ShellTools, tmp_path: Path) -> None:
    from chrys.service.mutations.trace import shell_trace_argv

    prefix, marker = _exec_passthrough_tracer(tmp_path)
    token = shell_trace_argv.set(prefix)
    try:
        stdout, returncode, _stderr = await shell._execute_pipe("echo traced-hello", shell._shell, cwd=".", timeout=15)
    finally:
        shell_trace_argv.reset(token)
    assert returncode == 0
    assert "traced-hello" in stdout.text()
    assert marker.exists()


@pytest.mark.skipif(not IS_UNIX, reason="PTY execution is Unix-only")
async def test_pty_broken_tracer_degrades_to_untraced(shell: ShellTools) -> None:
    """Spawn failure of the traced argv must fall back to the bare argv —
    tracing may never change user-visible shell behavior."""
    from chrys.service.mutations.trace import shell_trace_argv

    untraced, rc_untraced = await shell._execute_pty("echo degrade-check", shell._shell, cwd=".", timeout=15)
    token = shell_trace_argv.set(("/nonexistent/fsatrace-does-not-exist",))
    try:
        traced, rc_traced = await shell._execute_pty("echo degrade-check", shell._shell, cwd=".", timeout=15)
    finally:
        shell_trace_argv.reset(token)
    assert (traced, rc_traced) == (untraced, rc_untraced)


async def test_pipe_broken_tracer_degrades_to_untraced(shell: ShellTools) -> None:
    from chrys.service.mutations.trace import shell_trace_argv

    untraced = await shell._execute_pipe("echo degrade-check", shell._shell, cwd=".", timeout=15)
    token = shell_trace_argv.set(("/nonexistent/fsatrace-does-not-exist",))
    try:
        traced = await shell._execute_pipe("echo degrade-check", shell._shell, cwd=".", timeout=15)
    finally:
        shell_trace_argv.reset(token)
    assert traced == untraced
