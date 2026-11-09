# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for SubprocessScriptRunner — real subprocess execution.

Script paths are absolute (validated by the chrys loader at discovery time)
and the runner also re-checks containment before execution because it is the
final boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import shutil
import sys
import time
from typing import TYPE_CHECKING, Any

import psutil
import pytest

from chrys.foundation.platform import output_capture, runtime_paths, script_commands
from chrys.foundation.platform.output_capture import BoundedCapture
from chrys.foundation.platform.process import MissingWorkingDirectoryError
from chrys.foundation.text.tokenizer import MixedLanguageTokenizer
from chrys.foundation.tool_result_metadata import (
    TOOL_ERROR_DETAILS_METADATA_KEY,
    TOOL_ERROR_KIND_METADATA_KEY,
    tool_payload_observation,
)
from chrys.kernel.tools import SyncToolCancelledAfterCompletion
from chrys.service.skills import runner as runner_mod
from chrys.service.skills.loader import load_file_skill
from chrys.service.skills.model import Skill, SkillScript
from chrys.service.skills.runner import SubprocessScriptRunner
from chrys.service.tools.result_metadata import tool_result_metadata
from chrys.service.tools.spill import TOOL_RESULTS_DIR_NAME
from chrys.service.tools.workspace_paths import WORKING_DIR_MISSING_KIND, working_dir_missing_error
from tests.support.gc_isolation import earlier_tests_heap_frozen
from tests.support.processes import ExitedProcess
from tests.support.stopped_child import STOPS_ITSELF
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Coroutine, Mapping
    from pathlib import Path


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> SubprocessScriptRunner:
    monkeypatch.setattr(script_commands, "_find_python_runner", lambda: [sys.executable])
    return SubprocessScriptRunner(timeout=30)


def _make_skill(tmp_path: Path, script_name: str, script_content: str) -> tuple[Skill, SkillScript]:
    """Create a skill directory with a single Python script.

    Builds a :class:`Skill` rooted at ``tmp_path/test-skill`` and a
    :class:`SkillScript` whose ``full_path`` points at the on-disk
    script file (the runner requires the script path to be absolute).
    """
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir(exist_ok=True)
    script_path = skill_dir / script_name
    script_path.write_text(script_content, encoding="utf-8")

    skill = Skill(name="test-skill", description="test skill", content="body", path=str(skill_dir))
    script = SkillScript(name=script_name, full_path=str(script_path))
    return skill, script


def _exe(path: Path) -> Path:
    if sys.platform == "win32" and not path.suffix:
        return path.with_name(path.name + ".exe")
    return path


def _patch_runtime(
    monkeypatch: pytest.MonkeyPatch,
    *,
    python: Path,
    prefix: Path,
    scripts: Path,
) -> None:
    monkeypatch.setattr(runtime_paths.sys, "executable", str(python))
    monkeypatch.setattr(runtime_paths.sys, "prefix", str(prefix))
    monkeypatch.setattr(runtime_paths.sys, "exec_prefix", str(prefix))
    monkeypatch.setattr(
        runtime_paths.sysconfig,
        "get_path",
        lambda name: str(scripts) if name == "scripts" else "",
    )


async def test_skill_subprocess_env_demotes_runtime_path_when_frozen(
    runner: SubprocessScriptRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_bin = tmp_path / "runtime" / "bin"
    system_bin = tmp_path / "system" / "bin"
    _patch_runtime(
        monkeypatch,
        python=_exe(runtime_bin / "python"),
        prefix=tmp_path / "runtime",
        scripts=runtime_bin,
    )
    monkeypatch.setenv("PYAPP", "1")
    monkeypatch.setenv("PATH", os.pathsep.join([str(runtime_bin), str(system_bin)]))
    skill, script = _make_skill(tmp_path, "env_probe.py", "print('ok')")
    captured_env: dict[str, str] = {}

    @contextlib.asynccontextmanager
    async def fake_managed_subprocess(*_cmd: object, **kwargs: object):
        captured_env.update(kwargs["env"])

        yield ExitedProcess(b"ok\n")

    monkeypatch.setattr(runner_mod, "managed_subprocess", fake_managed_subprocess)

    result = await runner(skill, script)

    assert result == "ok"
    assert captured_env["PATH"].split(os.pathsep)[:2] == [str(system_bin), str(runtime_bin)]


async def test_skill_subprocess_env_strips_inherited_pythonhome_but_preserves_pythonpath(
    runner: SubprocessScriptRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONHOME", "/bad/home")
    monkeypatch.setenv("PYTHONPATH", "/shared/helpers")
    skill, script = _make_skill(tmp_path, "env_probe.py", "print('ok')")
    captured_env: dict[str, str] = {}

    @contextlib.asynccontextmanager
    async def fake_managed_subprocess(*_cmd: object, **kwargs: object):
        captured_env.update(kwargs["env"])

        yield ExitedProcess(b"ok\n")

    monkeypatch.setattr(runner_mod, "managed_subprocess", fake_managed_subprocess)

    result = await runner(skill, script)

    assert result == "ok"
    env_keys = {key.upper() for key in captured_env}
    assert "PYTHONHOME" not in env_keys
    assert captured_env["PYTHONPATH"] == "/shared/helpers"


async def test_returns_error_when_script_file_missing(runner: SubprocessScriptRunner, tmp_path: Path) -> None:
    """Script discovery validated full_path at construction, but the file may be deleted later."""
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    placeholder = skill_dir / "placeholder.py"
    placeholder.write_text("", encoding="utf-8")

    skill = Skill(name="test", description="test", content="body", path=str(skill_dir))
    script = SkillScript(name="missing.py", full_path=str(skill_dir / "missing.py"))
    result = await runner(skill, script)
    assert "not found" in result


async def test_rejects_resolved_script_path_outside_skill_dir(
    runner: SubprocessScriptRunner,
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text('print("escaped")\n', encoding="utf-8")

    skill = Skill(name="test", description="test", content="body", path=str(skill_dir))
    script = SkillScript(name="outside.py", full_path=str(outside))

    result = await runner(skill, script)

    assert result.startswith("Error:")
    assert "escapes skill directory" in result
    assert "escaped" not in result


async def test_runs_python_script(runner: SubprocessScriptRunner, tmp_path: Path) -> None:
    skill, script = _make_skill(tmp_path, "hello.py", 'print("hello world")')
    result = await runner(skill, script)
    assert "hello world" in result


async def test_loader_script_can_read_unlisted_binary_and_deep_files(
    runner: SubprocessScriptRunner,
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "test-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: test-skill\ndescription: test skill\n---\n\nBody.\n",
        encoding="utf-8",
    )
    script_path = skill_dir / "run.py"
    script_path.write_text(
        """\
from pathlib import Path

root = Path(__file__).parent
payload = (root / "payload.bin").read_bytes().decode("utf-8")
query = (root / "assets" / "nested" / "query.sql").read_text(encoding="utf-8")
has_skill = "name: test-skill" in (root / "SKILL.md").read_text(encoding="utf-8")
print(f"{payload}|{query}|{has_skill}")
""",
        encoding="utf-8",
    )
    (skill_dir / "payload.bin").write_bytes(b"binary payload")
    helper_dir = skill_dir / "assets" / "nested"
    helper_dir.mkdir(parents=True)
    (helper_dir / "query.sql").write_text("SELECT 1", encoding="utf-8")
    skill = load_file_skill(str(skill_dir), script_extensions=(".py",), search_depth=1)

    assert isinstance(skill, Skill)
    assert skill.resources == []
    assert [script.name for script in skill.scripts] == ["run.py"]

    result = await runner(skill, skill.scripts[0])

    assert result == "binary payload|SELECT 1|True"


async def test_stdin_is_devnull(runner: SubprocessScriptRunner, tmp_path: Path) -> None:
    """Skill scripts must not inherit the parent's stdin handle.

    On Windows this prevents a child TUI from calling ``SetConsoleMode``
    on the outer chrys's console input.  Here we just verify the
    observable: ``sys.stdin.read()`` returns immediately with an empty
    string (DEVNULL), instead of blocking or reading test-runner input.
    """
    skill, script = _make_skill(
        tmp_path,
        "stdin_probe.py",
        "import sys\nprint('stdin_empty=' + str(len(sys.stdin.read()) == 0))",
    )
    result = await runner(skill, script)
    assert "stdin_empty=True" in result


async def test_captures_stderr(runner: SubprocessScriptRunner, tmp_path: Path) -> None:
    skill, script = _make_skill(tmp_path, "warn.py", 'import sys; sys.stderr.write("warning\\n")')
    result = await runner(skill, script)
    assert "[stderr]" in result
    assert "warning" in result


async def test_reports_nonzero_exit(runner: SubprocessScriptRunner, tmp_path: Path) -> None:
    skill, script = _make_skill(tmp_path, "fail.py", "import sys; sys.exit(42)")
    result = await runner(skill, script)
    assert "[exit_code: 42]" in result


async def test_converts_args_to_flags(runner: SubprocessScriptRunner, tmp_path: Path) -> None:
    skill, script = _make_skill(
        tmp_path,
        "args.py",
        """\
import argparse
p = argparse.ArgumentParser()
p.add_argument("--name")
p.add_argument("--count", type=int)
a = p.parse_args()
print(f"{a.name} {a.count}")
""",
    )
    result = await runner(skill, script, args={"name": "alice", "count": 3})
    assert "alice 3" in result


async def test_arguments_precede_args_for_subcommand_flags(
    runner: SubprocessScriptRunner,
    tmp_path: Path,
) -> None:
    skill, script = _make_skill(
        tmp_path,
        "subcommand.py",
        """\
import argparse
p = argparse.ArgumentParser()
sub = p.add_subparsers(dest="cmd", required=True)
log = sub.add_parser("log")
log.add_argument("--oneline", action="store_true")
log.add_argument("--max-count", type=int)
a = p.parse_args()
print(f"{a.cmd} {a.oneline} {a.max_count}")
""",
    )

    result = await runner(script=script, skill=skill, arguments=["log"], args={"oneline": True, "max-count": 5})

    assert "log True 5" in result


async def test_dash_prefixed_arg_keys_pass_through(
    runner: SubprocessScriptRunner,
    tmp_path: Path,
) -> None:
    skill, script = _make_skill(
        tmp_path,
        "short_flags.py",
        """\
import sys
print("|".join(sys.argv[1:]))
""",
    )

    result = await runner(skill, script, args={"-i": True, "-xzf": True})

    assert "-i|-xzf" in result


async def test_list_args_are_treated_as_positional_arguments(
    runner: SubprocessScriptRunner,
    tmp_path: Path,
) -> None:
    skill, script = _make_skill(
        tmp_path,
        "positional.py",
        """\
import sys
print("|".join(sys.argv[1:]))
""",
    )

    result = await runner(skill, script, args=["input.txt", "output.txt"])

    assert "input.txt|output.txt" in result


async def test_list_value_expands_to_nargs(runner: SubprocessScriptRunner, tmp_path: Path) -> None:
    skill, script = _make_skill(
        tmp_path,
        "nargs.py",
        """\
import argparse
p = argparse.ArgumentParser()
p.add_argument("--key", nargs="+")
a = p.parse_args()
print("|".join(a.key))
""",
    )
    result = await runner(skill, script, args={"key": ["1", "2", "3"]})
    assert "1|2|3" in result


async def test_empty_list_omits_flag(runner: SubprocessScriptRunner, tmp_path: Path) -> None:
    skill, script = _make_skill(
        tmp_path,
        "empty_list.py",
        """\
import argparse
p = argparse.ArgumentParser()
p.add_argument("--key", nargs="+", default=["fallback"])
a = p.parse_args()
print("|".join(a.key))
""",
    )
    # Empty list should be skipped entirely — script sees its default,
    # not a bare ``--key`` (which would error under nargs='+').
    result = await runner(skill, script, args={"key": []})
    assert "fallback" in result


async def test_bool_true_becomes_flag(runner: SubprocessScriptRunner, tmp_path: Path) -> None:
    skill, script = _make_skill(
        tmp_path,
        "flag.py",
        """\
import argparse
p = argparse.ArgumentParser()
p.add_argument("--verbose", action="store_true")
a = p.parse_args()
print(f"verbose={a.verbose}")
""",
    )
    result = await runner(skill, script, args={"verbose": True})
    assert "verbose=True" in result


async def test_bool_false_not_added(runner: SubprocessScriptRunner, tmp_path: Path) -> None:
    skill, script = _make_skill(
        tmp_path,
        "flag2.py",
        """\
import argparse
p = argparse.ArgumentParser()
p.add_argument("--verbose", action="store_true")
a = p.parse_args()
print(f"verbose={a.verbose}")
""",
    )
    result = await runner(skill, script, args={"verbose": False})
    assert "verbose=False" in result


async def test_none_value_not_added(runner: SubprocessScriptRunner, tmp_path: Path) -> None:
    skill, script = _make_skill(tmp_path, "noop.py", 'print("ok")')
    result = await runner(skill, script, args={"key": None})
    assert "ok" in result


async def test_a_shell_script_with_no_bash_to_run_it_is_an_interpreter_error(
    tmp_path: Path, runner: SubprocessScriptRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_bash() -> str:
        raise FileNotFoundError(errno.ENOENT, script_commands._NO_WINDOWS_BASH)

    monkeypatch.setattr(script_commands, "_find_bash", no_bash)
    skill, script = _make_skill(tmp_path, "notify.sh", "echo 1\n")

    result = await runner(skill, script)

    assert result == f"Error: Interpreter not found for script 'notify.sh'. {script_commands._NO_WINDOWS_BASH}"


async def test_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(script_commands, "_find_python_runner", lambda: [sys.executable])
    runner = SubprocessScriptRunner(timeout=1)
    skill, script = _make_skill(tmp_path, "slow.py", "import time; time.sleep(5)")
    result = await runner(skill, script)
    assert "timed out" in result


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stopped state only")
async def test_stopped_script_returns_promptly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(script_commands, "_find_python_runner", lambda: [sys.executable])
    runner = SubprocessScriptRunner(timeout=10)
    skill, script = _make_skill(
        tmp_path,
        "stopped.py",
        STOPS_ITSELF + "\n",
    )

    with earlier_tests_heap_frozen():
        started = time.monotonic()
        result = await runner(skill, script)
        elapsed = time.monotonic() - started

    assert "stopped state" in result
    assert elapsed < 5


async def test_no_output(runner: SubprocessScriptRunner, tmp_path: Path) -> None:
    skill, script = _make_skill(tmp_path, "empty.py", "")
    result = await runner(skill, script)
    assert result == "(no output)"


async def test_large_output_is_cleaned_truncated_and_spilled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(script_commands, "_find_python_runner", lambda: [sys.executable])
    runner = SubprocessScriptRunner(timeout=30, session_dir=tmp_path)
    skill, script = _make_skill(
        tmp_path,
        "large.py",
        """\
import sys
sys.stdout.write("\\x1b[31mstart\\x1b[0m\\rfinal:" + "x" * 10000 + "\\n")
sys.stderr.write("important stderr\\n")
raise SystemExit(7)
""",
    )

    result = await runner(skill, script, max_tokens=100)

    assert "truncated" in result
    assert "Full output saved to:" in result
    assert MixedLanguageTokenizer().count_tokens(result) <= 100
    spills = list((tmp_path / TOOL_RESULTS_DIR_NAME).glob("skill_*.txt"))
    assert len(spills) == 1
    full = spills[0].read_text()
    assert "\x1b" not in full
    assert "start" not in full
    assert full.startswith("final:")
    assert "[stderr]\nimportant stderr" in full
    assert full.endswith("[exit_code: 7]")


async def test_script_max_tokens_zero_clamps_to_one_hundred(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(script_commands, "_find_python_runner", lambda: [sys.executable])
    skill, script = _make_skill(tmp_path, "large.py", 'print("x" * 10000)')

    result = await SubprocessScriptRunner(timeout=30)(skill, script, max_tokens=0)

    assert "truncated" in result
    assert MixedLanguageTokenizer().count_tokens(result) <= 100
    assert "Full output saved to:" not in result


async def test_completed_script_result_propagates_spill_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(script_commands, "_find_python_runner", lambda: [sys.executable])
    completed = "[bounded completed skill result]"

    async def cancelled_after_completion(*_args: object) -> str:
        raise SyncToolCancelledAfterCompletion(completed)

    monkeypatch.setattr(runner_mod, "bound_process_output", cancelled_after_completion)
    skill, script = _make_skill(tmp_path, "large.py", 'print("x" * 10000)')

    with pytest.raises(SyncToolCancelledAfterCompletion) as exc_info:
        await SubprocessScriptRunner(timeout=30, session_dir=tmp_path)(skill, script, max_tokens=100)

    assert exc_info.value.completed_result == completed


# ---------------------------------------------------------------------------
# Bounded output capture
# ---------------------------------------------------------------------------

_NOISY_SCRIPT = """\
import sys
out, err = sys.stdout.buffer, sys.stderr.buffer
for i in range(200):
    out.write(b"out-%04d " % i + b"o" * 90 + b"\\n")
    err.write(b"err-%04d " % i + b"e" * 90 + b"\\n")
out.write(b"OUT-END\\n")
err.write(b"ERR-END\\n")
out.flush()
err.flush()
"""

_NOISY_STREAM_BYTES = 200 * 100 + len("OUT-END\n")


@pytest.fixture
def small_capture_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a 1000-byte head and a 2000-byte tail of each stream."""
    monkeypatch.setattr(output_capture, "OUTPUT_CAPTURE_LIMIT_BYTES", 3000)
    monkeypatch.setattr(script_commands, "_find_python_runner", lambda: [sys.executable])


@pytest.mark.usefixtures("small_capture_limit")
async def test_both_streams_keep_their_head_and_tail_and_a_note_precedes_the_exit_code(tmp_path: Path) -> None:
    skill, script = _make_skill(tmp_path, "noisy.py", _NOISY_SCRIPT + "raise SystemExit(3)\n")
    observation: dict[str, object] = {}

    token = tool_payload_observation.set(observation)
    try:
        result = await SubprocessScriptRunner(timeout=30, session_dir=tmp_path)(skill, script)
    finally:
        tool_payload_observation.reset(token)

    dropped = 2 * (_NOISY_STREAM_BYTES - 3000)
    assert result.startswith("out-0000 ")
    assert "OUT-END\n\n[stderr]\nerr-0000 " in result
    assert result.endswith(
        f"ERR-END\n[Output capture limit reached: {dropped} bytes from the middle were not kept.]\n[exit_code: 3]"
    )
    assert observation == {"original_bytes": 2 * _NOISY_STREAM_BYTES, "truncated": True}


@pytest.mark.usefixtures("small_capture_limit")
async def test_a_timed_out_script_returns_its_bounded_output_and_is_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The test times the script out once its output is read, in place of the timer."""
    skill, script = _make_skill(tmp_path, "hangs.py", _NOISY_SCRIPT + "import time\ntime.sleep(60)\n")
    procs: list[asyncio.subprocess.Process] = []
    captures: list[BoundedCapture] = []
    children: list[psutil.Process] = []
    real_drain = runner_mod.drain_process_pipes
    real_wait = runner_mod.wait_for_subprocess

    def recording_drain(
        proc: asyncio.subprocess.Process, stdout: BoundedCapture, stderr: BoundedCapture
    ) -> Coroutine[Any, Any, None]:
        procs.append(proc)
        captures.extend((stdout, stderr))
        return real_drain(proc, stdout, stderr)

    async def time_out_once_the_output_is_read(
        awaitable: Coroutine[Any, Any, None], *, timeout: float | None, process_group_id: int | None
    ) -> None:
        [proc] = procs
        drain = asyncio.ensure_future(awaitable)

        async def read_then_time_out() -> None:
            try:
                # Counted, not read to the end: a launcher between the runner
                # and the script can hold the pipes open while the script sleeps.
                await wait_for(
                    lambda: drain.done() or all(c.snapshot().seen == _NOISY_STREAM_BYTES for c in captures),
                    timeout=ENGINE_TURN_TIMEOUT,
                    description="all output of both streams read",
                )
                assert not drain.done()
                children.append(psutil.Process(proc.pid))
                raise TimeoutError
            finally:
                drain.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await drain

        await real_wait(read_then_time_out(), timeout=None, process_group_id=process_group_id)

    monkeypatch.setattr(runner_mod, "drain_process_pipes", recording_drain)
    monkeypatch.setattr(runner_mod, "wait_for_subprocess", time_out_once_the_output_is_read)

    result = await SubprocessScriptRunner(timeout=30)(skill, script)

    dropped = 2 * (_NOISY_STREAM_BYTES - 3000)
    assert result.startswith("Error: Script 'hangs.py' timed out after 30s.\n[partial output]\nout-0000 ")
    assert "OUT-END\n\n[stderr]\nerr-0000 " in result
    assert result.endswith(f"ERR-END\n[Output capture limit reached: {dropped} bytes from the middle were not kept.]")
    [child] = children
    assert not child.is_running()


@pytest.mark.usefixtures("small_capture_limit")
async def test_a_timed_out_script_keeps_its_error_line_under_a_small_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill, script = _make_skill(tmp_path, "hangs.py", "")
    noisy = b"".join(b"line %04d " % i + b"x" * 90 + b"\n" for i in range(200))

    @contextlib.asynccontextmanager
    async def fake_managed_subprocess(
        *_cmd: str, stdin: int, stdout: int, stderr: int, cwd: str, env: Mapping[str, str]
    ) -> AsyncIterator[ExitedProcess]:
        yield ExitedProcess(noisy, noisy)

    async def drain_then_time_out(
        awaitable: Coroutine[Any, Any, None], *, timeout: float | None, process_group_id: int | None
    ) -> None:
        await awaitable
        raise TimeoutError

    monkeypatch.setattr(runner_mod, "managed_subprocess", fake_managed_subprocess)
    monkeypatch.setattr(runner_mod, "wait_for_subprocess", drain_then_time_out)

    result = await SubprocessScriptRunner(timeout=30, session_dir=tmp_path)(skill, script, max_tokens=200)

    assert result.startswith("Error: Script 'hangs.py' timed out after 30s.\n[partial output]\n")
    assert "Kept output saved to:" in result
    assert result.endswith(" bytes from the middle were not kept.]")
    assert MixedLanguageTokenizer().count_tokens(result) <= 200
    # The spill holds the output, not the error line.
    [spilled] = (tmp_path / TOOL_RESULTS_DIR_NAME).glob("skill_*.txt")
    assert spilled.read_text(encoding="utf-8").startswith("line 0000 ")


# ---------------------------------------------------------------------------
# CWD resolution
# ---------------------------------------------------------------------------


def _cwd_script() -> str:
    """Script body that prints its own os.getcwd()."""
    return "import os; print(os.getcwd())"


async def test_cwd_defaults_to_runtime_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When no cwd is passed, the subprocess runs in runtime.cwd."""
    import dataclasses

    from chrys.foundation.models.session_env import SessionEnvironment

    work_dir = tmp_path / "user-workspace"
    work_dir.mkdir()

    monkeypatch.setattr(script_commands, "_find_python_runner", lambda: [sys.executable])
    runtime = dataclasses.replace(SessionEnvironment.capture(), cwd=str(work_dir))

    runner = SubprocessScriptRunner(timeout=30, runtime=runtime)
    skill, script = _make_skill(tmp_path, "cwd.py", _cwd_script())
    result = await runner(skill, script)
    assert str(work_dir) in result


async def test_explicit_absolute_cwd_is_honored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An absolute cwd supplied by the agent overrides the default."""
    monkeypatch.setattr(script_commands, "_find_python_runner", lambda: [sys.executable])
    runner = SubprocessScriptRunner(timeout=30)
    alt_dir = tmp_path / "elsewhere"
    alt_dir.mkdir()

    skill, script = _make_skill(tmp_path, "cwd.py", _cwd_script())
    result = await runner(skill, script, cwd=str(alt_dir))
    assert str(alt_dir) in result


async def test_relative_cwd_is_rejected(tmp_path: Path) -> None:
    runner = SubprocessScriptRunner(timeout=30)
    skill, script = _make_skill(tmp_path, "cwd.py", _cwd_script())
    result = await runner(skill, script, cwd="relative/path")
    assert result.startswith("Error:")
    assert "absolute path" in result


async def test_nonexistent_cwd_is_rejected(tmp_path: Path) -> None:
    runner = SubprocessScriptRunner(timeout=30)
    skill, script = _make_skill(tmp_path, "cwd.py", _cwd_script())
    missing = tmp_path / "does-not-exist"
    result = await runner(skill, script, cwd=str(missing))
    assert result.startswith("Error:")
    assert "not an existing directory" in result


async def test_cwd_falls_back_to_script_parent_without_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a runtime and without an explicit cwd, use script_path.parent (legacy)."""
    monkeypatch.setattr(script_commands, "_find_python_runner", lambda: [sys.executable])
    runner = SubprocessScriptRunner(timeout=30)  # no runtime bound
    skill, script = _make_skill(tmp_path, "cwd.py", _cwd_script())
    result = await runner(skill, script)
    assert str(tmp_path / "test-skill") in result


def _runtime_at(cwd: Path) -> Any:
    import dataclasses

    from chrys.foundation.models.session_env import SessionEnvironment

    return dataclasses.replace(SessionEnvironment.capture(), cwd=str(cwd))


async def _run_with_metadata(
    runner: SubprocessScriptRunner, skill: Skill, script: SkillScript, *, cwd: str | None = None
) -> tuple[str, dict[str, object]]:
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        return await runner(skill, script, cwd=cwd), metadata
    finally:
        tool_result_metadata.reset(token)


@pytest.mark.parametrize("skill_inside_workspace", [True, False])
async def test_deleted_working_directory_is_reported_before_the_script_is_looked_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, skill_inside_workspace: bool
) -> None:
    """A project skill lives inside the working directory: report the directory, not its missing script."""
    monkeypatch.setattr(script_commands, "_find_python_runner", lambda: [sys.executable])
    workspace = tmp_path / "workspace"
    skills_root = workspace / ".agents" / "skills" if skill_inside_workspace else tmp_path / "user-skills"
    skills_root.mkdir(parents=True)
    workspace.mkdir(exist_ok=True)
    skill, script = _make_skill(skills_root, "cwd.py", _cwd_script())
    runner = SubprocessScriptRunner(timeout=30, runtime=_runtime_at(workspace))
    shutil.rmtree(workspace)

    result, metadata = await _run_with_metadata(runner, skill, script)

    assert result == working_dir_missing_error(str(workspace))
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == WORKING_DIR_MISSING_KIND
    assert metadata[TOOL_ERROR_DETAILS_METADATA_KEY] == {"cwd": str(workspace)}


async def test_explicit_cwd_still_runs_after_the_working_directory_is_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(script_commands, "_find_python_runner", lambda: [sys.executable])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alt_dir = tmp_path / "elsewhere"
    alt_dir.mkdir()
    skill, script = _make_skill(tmp_path, "cwd.py", _cwd_script())
    runner = SubprocessScriptRunner(timeout=30, runtime=_runtime_at(workspace))
    workspace.rmdir()

    result = await runner(skill, script, cwd=str(alt_dir))

    assert str(alt_dir) in result
    assert not result.startswith("Error:")


@pytest.mark.parametrize("explicit_cwd", [False, True])
async def test_directory_deleted_just_before_the_spawn_is_reported_as_that_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_cwd: bool
) -> None:
    """The check before the spawn can race a deletion; the spawn's own failure names the same directory."""
    monkeypatch.setattr(script_commands, "_find_python_runner", lambda: [sys.executable])
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill, script = _make_skill(tmp_path, "cwd.py", _cwd_script())
    runner = SubprocessScriptRunner(timeout=30, runtime=_runtime_at(workspace))
    spawned_in: list[object] = []

    @contextlib.asynccontextmanager
    async def deleted_before_spawn(*_cmd: object, **kwargs: object) -> AsyncIterator[ExitedProcess]:
        spawned_in.append(kwargs["cwd"])
        if spawned_in:
            raise MissingWorkingDirectoryError(str(workspace))
        yield ExitedProcess(b"")

    monkeypatch.setattr(runner_mod, "managed_subprocess", deleted_before_spawn)

    result, metadata = await _run_with_metadata(runner, skill, script, cwd=str(workspace) if explicit_cwd else None)

    assert spawned_in == [str(workspace)]
    if explicit_cwd:
        assert result == f"Error: 'cwd' is not an existing directory: {workspace}"
        assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "invalid_cwd"
    else:
        assert result == working_dir_missing_error(str(workspace))
        assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == WORKING_DIR_MISSING_KIND
