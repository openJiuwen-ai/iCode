# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the shell tool's bounded output capture and the note that closes a result it cut."""

from __future__ import annotations

import contextlib
import re
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.platform import ShellInfo, output_capture
from chrys.foundation.platform.output_capture import CapturedOutput
from chrys.foundation.text.tokenizer import MixedLanguageTokenizer
from chrys.foundation.tool_result_metadata import result_text_exit_code, result_text_without_exit_code
from chrys.foundation.trajectory.context import trajectory_scope
from chrys.foundation.trajectory.event_types import EventType
from chrys.kernel._result_ceiling import apply_result_ceiling
from chrys.service.agent_middleware import ToolEventMiddleware
from chrys.service.tools import spill
from chrys.service.tools.builtins import shell as shell_mod
from chrys.service.tools.builtins.shell import ShellTools, _clean_output, _Timeout, shell_progress_callback
from chrys.service.tools.spill import TOOL_RESULTS_DIR_NAME
from tests.service.agent_middleware._event_fakes import _ctx
from tests.service.trajectory._fakes import FakeSink, make_context
from tests.support.processes import ExitedProcess

IS_UNIX = sys.platform != "win32"

_LIMIT = 3000
"""Capture limit for these tests: a 1000-byte head and a 2000-byte tail per stream."""

_LINES = 200

_WRITER = f"""\
import sys
out, err = sys.stdout.buffer, sys.stderr.buffer
for i in range({_LINES}):
    out.write(b"out-%04d " % i + b"o" * 90 + b"\\n")
    out.flush()
    err.write(b"err-%04d " % i + b"e" * 90 + b"\\n")
    err.flush()
err.write(b"ERR-END\\n")
err.flush()
out.write(b"OUT-END\\n")
out.flush()
sys.exit(3)
"""

_STREAM_BYTES = _LINES * 100 + len("OUT-END\n")
"""Bytes ``_WRITER`` writes to each stream."""

_OMITTED = re.compile(r"\[\.\.\. (\d+) bytes omitted \.\.\.\]")
_NOTE = re.compile(
    r"\n\[Output capture limit reached: (\d+) bytes from the middle were not kept\.\](?:\n\[exit_code: -?\d+\])?\Z"
)
"""The note, then the exit code when the result has one."""


@pytest.fixture
def small_capture_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(output_capture, "OUTPUT_CAPTURE_LIMIT_BYTES", _LIMIT)


@pytest.fixture(autouse=True)
def _temp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the user's shell with a temp home and no startup-file variables, so no startup file of theirs adds output."""
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE", "APPDATA"):
        monkeypatch.setenv(name, str(home))
    for name in ("ZDOTDIR", "BASH_ENV", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)


def _command(tmp_path: Path, source: str = _WRITER) -> str:
    script = tmp_path / "writer.py"
    script.write_text(source, encoding="utf-8")
    if sys.platform == "win32":
        # PowerShell's call operator, and its exit code passed on.
        return f'& "{sys.executable}" "{script}"; exit $LASTEXITCODE'
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script))}"


def _split_note(result: str) -> tuple[str, int]:
    """Split the closing note (and exit code) off *result* and return the byte count it gives."""
    match = _NOTE.search(result)
    assert match is not None, result[-300:]
    return result[: match.start()], int(match.group(1))


def _omitted(text: str) -> int:
    return sum(int(count) for count in _OMITTED.findall(text))


# ---------------------------------------------------------------------------
# Backends keep a bounded head and tail
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("small_capture_limit")
async def test_pipe_backend_keeps_the_head_and_tail_of_both_streams(tmp_path: Path) -> None:
    shell = ShellTools(SessionEnvironment.capture())

    stdout, returncode, stderr = await shell._execute_pipe(_command(tmp_path), shell._shell, cwd=".", timeout=15)

    assert returncode == 3
    assert stdout.seen == stderr.seen == _STREAM_BYTES
    assert (len(stdout.head), len(stdout.tail), len(stderr.head), len(stderr.tail)) == (1000, 2000, 1000, 2000)
    assert stdout.head.startswith(b"out-0000 ")
    assert stdout.tail.endswith(b"out-0199 " + b"o" * 90 + b"\nOUT-END\n")
    assert stderr.head.startswith(b"err-0000 ")
    assert stderr.tail.endswith(b"err-0199 " + b"e" * 90 + b"\nERR-END\n")


@pytest.mark.skipif(not IS_UNIX, reason="PTY execution is Unix-only")
@pytest.mark.usefixtures("small_capture_limit")
async def test_pty_backend_keeps_the_head_and_tail_of_the_merged_output(tmp_path: Path) -> None:
    shell = ShellTools(SessionEnvironment.capture())

    merged, returncode = await shell._execute_pty(_command(tmp_path), shell._shell, cwd=".", timeout=15)

    assert returncode == 3
    # The terminal may turn each newline into CR LF.
    assert merged.seen >= 2 * _STREAM_BYTES
    assert (len(merged.head), len(merged.tail)) == (1000, 2000)
    assert merged.head.startswith(b"out-0000 ")
    assert merged.tail.rstrip().endswith(b"ERR-END\r\nOUT-END")


# ---------------------------------------------------------------------------
# The note closes the result, right before the exit code
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("small_capture_limit")
async def test_execute_puts_the_capture_note_right_before_the_exit_code(tmp_path: Path) -> None:
    shell = ShellTools(SessionEnvironment.capture(), session_dir=tmp_path)

    result = await shell.execute(_command(tmp_path), reason="test", timeout=15)

    body, dropped = _split_note(result)
    assert dropped == _omitted(body) > 0
    # The output comes right before the note; a terminal merges both streams.
    assert body.endswith(("OUT-END", "ERR-END"))
    # What the result cards and ACP read.
    assert result_text_exit_code(result) == 3
    assert result_text_without_exit_code(result).endswith(" bytes from the middle were not kept.]")
    assert "out-0000" in result and "OUT-END" in result
    assert "saved to:" not in result
    assert not (tmp_path / TOOL_RESULTS_DIR_NAME).exists()


@pytest.mark.usefixtures("small_capture_limit")
async def test_a_small_budget_spills_the_kept_output_and_keeps_the_note_and_exit_code(tmp_path: Path) -> None:
    shell = ShellTools(SessionEnvironment.capture(), session_dir=tmp_path)

    # Room for the spill notice, whose path is a long temporary one.
    result = await shell.execute(_command(tmp_path), reason="test", timeout=15, max_output_tokens=200)

    _body, dropped = _split_note(result)
    assert MixedLanguageTokenizer().count_tokens(result) <= 200
    assert result_text_exit_code(result) == 3
    assert "Kept output saved to:" in result
    [spilled] = (tmp_path / TOOL_RESULTS_DIR_NAME).glob("shell_*.txt")
    kept = spilled.read_text(encoding="utf-8")
    assert _omitted(kept) == dropped
    assert "OUT-END" in kept and kept.endswith("[exit_code: 3]")


@pytest.mark.usefixtures("small_capture_limit")
async def test_a_small_budget_without_a_session_keeps_the_note_and_exit_code(tmp_path: Path) -> None:
    shell = ShellTools(SessionEnvironment.capture())

    result = await shell.execute(_command(tmp_path), reason="test", timeout=15, max_output_tokens=100)

    _split_note(result)
    assert MixedLanguageTokenizer().count_tokens(result) <= 100
    assert result_text_exit_code(result) == 3
    assert "saved to:" not in result


@pytest.mark.usefixtures("small_capture_limit")
async def test_a_failed_spill_keeps_the_note_and_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[Path] = []

    def failing_write(path: Path, _payload: bytes) -> None:
        writes.append(path)
        raise OSError("disk full")

    monkeypatch.setattr(spill, "atomic_write_owner_only_bytes", failing_write)
    shell = ShellTools(SessionEnvironment.capture(), session_dir=tmp_path)

    # Room for the spill notice, so the write is tried.
    result = await shell.execute(_command(tmp_path), reason="test", timeout=15, max_output_tokens=200)

    assert len(writes) == 1
    _split_note(result)
    assert MixedLanguageTokenizer().count_tokens(result) <= 200
    assert result_text_exit_code(result) == 3
    assert "saved to:" not in result


async def test_a_timed_out_result_keeps_the_note_last(monkeypatch: pytest.MonkeyPatch) -> None:
    shell = ShellTools(SessionEnvironment.capture())

    async def timed_out(command: str, shell: ShellInfo, cwd: str, timeout: int | float, reason: str = "") -> Any:
        raise _Timeout((CapturedOutput(b"started\n", b"still going\n", 5000),))

    monkeypatch.setattr(shell, "_execute_pty" if IS_UNIX else "_execute_pipe", timed_out)

    result = await shell.execute("sleep 999", reason="test", timeout=7)

    assert result.startswith("Error: command timed out after 7 seconds.\n[partial output]\nstarted\n")
    assert "still going" in result
    assert result.endswith(" bytes from the middle were not kept.]")
    assert _split_note(result)[1] == 5000 - 20


async def test_a_timed_out_result_keeps_its_error_line_under_a_small_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = ShellTools(SessionEnvironment.capture(), session_dir=tmp_path)
    head = "".join(f"started {i}\n" for i in range(100)).encode()
    tail = "".join(f"still going {i}\n" for i in range(200)).encode()

    async def timed_out(command: str, shell: ShellInfo, cwd: str, timeout: int | float, reason: str = "") -> Any:
        raise _Timeout((CapturedOutput(head, tail, 100_000),))

    monkeypatch.setattr(shell, "_execute_pty" if IS_UNIX else "_execute_pipe", timed_out)

    result = await shell.execute("sleep 999", reason="test", timeout=7, max_output_tokens=200)

    assert result.startswith("Error: command timed out after 7 seconds.\n")
    assert "Kept output saved to:" in result
    assert result.endswith(" bytes from the middle were not kept.]")
    assert MixedLanguageTokenizer().count_tokens(result) <= 200
    # The spill holds the output, not the error line.
    [spilled] = (tmp_path / TOOL_RESULTS_DIR_NAME).glob("shell_*.txt")
    assert spilled.read_text(encoding="utf-8").startswith("[partial output]\nstarted 0\n")


@pytest.mark.parametrize(
    ("head", "tail", "tail_text"),
    [
        # Cleaned as one text, this OSC would run up to the tail's BEL and hide the gap.
        (b"start\x1b]0;title", b"rest\x07 after\rfinal\n", "final\n"),
        (b"start\x1b[3", b"1mred\x1b[0m\n", "1mred\n"),
        # A link's address cut in two, then its string terminator.
        (b"start\x1b]8;;http", b"s://x\x1b\\link\n", "s://xlink\n"),
    ],
    ids=["osc", "csi", "osc-terminator"],
)
def test_an_escape_sequence_cut_by_the_gap_leaves_no_escape_and_does_not_hide_it(
    head: bytes, tail: bytes, tail_text: str
) -> None:
    captured = CapturedOutput(head, tail, 1000)

    text = captured.text(_clean_output)

    assert text == f"start\n[... {captured.dropped} bytes omitted ...]\n{tail_text}"


# ---------------------------------------------------------------------------
# Kernel ceiling and trajectory
# ---------------------------------------------------------------------------

_LARGE_WRITER = """\
import sys
out, err = sys.stdout.buffer, sys.stderr.buffer
for i in range(2000):
    words = " ".join(str(i * 7919 + j) for j in range(12)).encode()
    out.write(b"out-%04d " % i + words + b"\\n")
    err.write(b"err-%04d " % i + words + b"\\n")
out.flush()
err.flush()
sys.exit(3)
"""


async def test_the_note_and_exit_code_survive_the_kernel_ceiling_and_the_trajectory_records_every_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool's own budget is larger than the ceiling, which then cuts again and keeps the end."""
    monkeypatch.setattr(output_capture, "OUTPUT_CAPTURE_LIMIT_BYTES", 30_000)
    shell = ShellTools(SessionEnvironment.capture())
    middleware = ToolEventMiddleware(
        EventBus(),
        session_id="capture-test",
        tool_result_ceiling_tokens=2000,
        origin=InvocationOrigin("turn", "capture-test", "turn-test", None),
    )
    context = _ctx("bash", "shell", args={"command": "writer"})

    async def run_shell() -> None:
        context.result = await shell.execute(_command(tmp_path, _LARGE_WRITER), reason="test", timeout=15)

    sink = FakeSink()
    with trajectory_scope(make_context(sink)):
        await middleware.process(context, run_shell)

    visible = apply_result_ceiling(context.result, 2000)
    assert visible != context.result
    body, dropped = _split_note(context.result)
    assert visible.endswith(context.result[len(body) :])
    assert dropped > 0 and "[exit_code: 3]" in visible
    payload = sink.only(EventType.TOOL_PAYLOAD_OBSERVED).payload
    assert payload["truncated"] is True
    assert payload["model_visible_bytes"] == len(visible.encode())
    # What the program wrote, not what the capture kept.
    written = sum(len(line) for line in _large_writer_lines())
    if IS_UNIX:
        # The terminal may turn each newline into CR LF.
        assert payload["original_bytes"] >= written
    else:
        assert payload["original_bytes"] == written


def _large_writer_lines() -> list[bytes]:
    lines = []
    for i in range(2000):
        words = " ".join(str(i * 7919 + j) for j in range(12)).encode()
        lines.extend((b"out-%04d " % i + words + b"\n", b"err-%04d " % i + words + b"\n"))
    return lines


# ---------------------------------------------------------------------------
# Progress lines stay bounded
# ---------------------------------------------------------------------------


@pytest.fixture
def progress_waits_for_the_end(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing is shown before the program ends, so every line waits.
    monkeypatch.setattr(shell_mod, "_PROGRESS_THROTTLE_INTERVAL", 3600)


@pytest.fixture
def small_pending_limit(progress_waits_for_the_end: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_mod, "_PROGRESS_PENDING_LIMIT", 1000)


@pytest.fixture
def small_line_limit(progress_waits_for_the_end: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_mod, "_PROGRESS_LINE_LIMIT", 100)


_SHORT_LINES = [f"line-{i:03d}-{'p' * 10}" for i in range(500)]


async def _follow_pipes(proc: ExitedProcess, show: Any) -> CapturedOutput:
    """Read *proc*'s pipes as the pipe backend does, returning what stdout kept."""
    token = shell_progress_callback.set(show)
    try:
        run = shell_mod._Command(
            proc, (proc.stdout, proc.stderr), contextlib.AsyncExitStack(), live=dict, stop=lambda _force: None
        )
        try:
            await run.follow(show, idle=False)
            stdout, _stderr = run.captured()
            return stdout
        finally:
            await run.close()
    finally:
        shell_progress_callback.reset(token)


def _waiting_size(lines: list[str]) -> int:
    """The size a progress batch counts toward its limit: each line and its newline."""
    return sum(len(line) + 1 for line in lines)


@pytest.mark.usefixtures("small_pending_limit")
async def test_pipe_progress_keeps_only_the_newest_waiting_lines() -> None:
    received: list[list[str]] = []

    async def show(lines: list[str]) -> None:
        received.append(lines)

    stdout = await _follow_pipes(ExitedProcess("".join(f"{line}\n" for line in _SHORT_LINES).encode()), show)

    [batch] = received
    assert batch == _SHORT_LINES[-len(batch) :]
    assert _waiting_size(batch) <= 1000 < _waiting_size([_SHORT_LINES[0], *batch])
    assert stdout.seen == sum(len(line) + 1 for line in _SHORT_LINES)


@pytest.mark.usefixtures("small_pending_limit")
async def test_pipe_progress_counts_blank_lines_toward_the_limit() -> None:
    received: list[list[str]] = []

    async def show(lines: list[str]) -> None:
        received.append(lines)

    await _follow_pipes(ExitedProcess(b"\n" * 100_000 + b"END\n"), show)

    [batch] = received
    assert batch[-1] == "END"
    assert len(batch) <= 1000


@pytest.mark.usefixtures("small_line_limit")
async def test_pipe_progress_drops_the_start_of_a_long_unfinished_line() -> None:
    received: list[list[str]] = []

    async def show(lines: list[str]) -> None:
        received.append(lines)

    await _follow_pipes(ExitedProcess(b"x" * 200_000 + b"\nEND\n"), show)

    [[long_line, end]] = received
    assert end == "END"
    # One read past the limit at most, never the whole line.
    assert set(long_line) == {"x"} and len(long_line) <= 100 + 64 * 1024


async def _pty_progress(tmp_path: Path, source: str) -> list[list[str]]:
    shell = ShellTools(SessionEnvironment.capture())
    received: list[list[str]] = []

    async def show(lines: list[str]) -> None:
        received.append(lines)

    token = shell_progress_callback.set(show)
    try:
        result = await shell.execute(_command(tmp_path, source), reason="test", timeout=15)
    finally:
        shell_progress_callback.reset(token)
    assert "[exit_code: 0]" in result
    return received


@pytest.mark.skipif(not IS_UNIX, reason="PTY execution is Unix-only")
@pytest.mark.usefixtures("small_pending_limit")
async def test_pty_progress_keeps_only_the_newest_waiting_lines(tmp_path: Path) -> None:
    source = (
        "import sys\n"
        f"for i in range({len(_SHORT_LINES)}):\n"
        "    sys.stdout.buffer.write(b'line-%03d-' % i + b'p' * 10 + b'\\n')\n"
    )

    [batch] = await _pty_progress(tmp_path, source)

    assert batch == _SHORT_LINES[-len(batch) :]
    assert _waiting_size(batch) <= 1000 < _waiting_size([_SHORT_LINES[0], *batch])


@pytest.mark.skipif(not IS_UNIX, reason="PTY execution is Unix-only")
@pytest.mark.usefixtures("small_line_limit")
async def test_pty_progress_drops_the_start_of_a_long_unfinished_line(tmp_path: Path) -> None:
    source = "import sys\nsys.stdout.buffer.write(b'x' * 200000 + b'\\nEND\\n')\n"

    [[long_line, end]] = await _pty_progress(tmp_path, source)

    assert end == "END"
    assert set(long_line) == {"x"} and len(long_line) <= 100 + 64 * 1024
