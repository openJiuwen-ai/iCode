# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What the hook runner keeps of a run for the Hooks panel's details.

Captured output survives a timeout and a stopped process, the bytes cut from
the middle of each stream are counted, a missing result file is told apart from
an invalid one, a detached hand-off returns its log path, and a given
``payload_text`` is what the hook reads, byte for byte. Hooks are real
``python -c`` subprocesses.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest

from chrys.service.hooks import runner as runner_mod
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.runner import HookRunner
from chrys.service.hooks.schema import HookConfig, HookExecution, HookMatch, HookRun
from tests.support.stopped_child import STOPS_ITSELF
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for


@pytest.fixture
def hooks_dir(tmp_path: Path) -> Path:
    path = tmp_path / "hooks"
    path.mkdir()
    return path


@pytest.fixture
def runner(hooks_dir: Path) -> HookRunner:
    return HookRunner(hooks_dir=hooks_dir)


def _hook(script: str, *, timeout: float = ENGINE_TURN_TIMEOUT, hook_id: str = "t") -> HookConfig:
    return HookConfig(
        id=hook_id,
        event=HookEvent.BEFORE_TOOL_CALL,
        run=HookRun(type="command", argv=[sys.executable, "-c", script]),
        execution=HookExecution(mode="blocking", timeout_seconds=timeout),
        match=HookMatch(),
    )


def _detached_hook(script: str, *, hook_id: str = "handed-off") -> HookConfig:
    return HookConfig(
        id=hook_id,
        event=HookEvent.SESSION_END,
        run=HookRun(type="command", argv=[sys.executable, "-c", script]),
        execution=HookExecution(mode="fire_and_forget", detach=True, timeout_seconds=ENGINE_TURN_TIMEOUT),
        match=HookMatch(),
    )


def _write_result(text: str) -> str:
    return f"import os\nopen(os.environ['CHRYS_HOOK_RESULT'], 'w').write({text!r})\n"


# --- Output kept when the run does not end by itself (5.3) ----------------------


async def test_a_timed_out_run_keeps_what_it_printed_before_the_timeout(runner: HookRunner) -> None:
    script = textwrap.dedent(
        """
        import sys, time
        sys.stdout.write("stdout before the timeout\\n")
        sys.stdout.flush()
        sys.stderr.write("stderr before the timeout\\n")
        sys.stderr.flush()
        time.sleep(30)
        """
    )
    result = await runner.run_and_wait(_hook(script, timeout=1.0), {})
    assert (result.timed_out, result.stopped) == (True, False)
    assert "stdout before the timeout" in result.stdout
    assert "stderr before the timeout" in result.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups and SIGSTOP only")
async def test_a_run_whose_process_stopped_keeps_what_it_printed(runner: HookRunner, tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    child = f"import os, pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); {STOPS_ITSELF}"
    script = textwrap.dedent(
        f"""
        import subprocess, sys
        sys.stdout.write("printed before the stop\\n")
        sys.stdout.flush()
        sys.stderr.write("warned before the stop\\n")
        sys.stderr.flush()
        subprocess.Popen([sys.executable, "-c", {child!r}])
        """
    )
    result = await runner.run_and_wait(_hook(script, timeout=10), {})
    # Named apart from a timeout, which it is ended as.
    assert (result.timed_out, result.stopped) == (True, True)
    assert "printed before the stop" in result.stdout
    assert "warned before the stop" in result.stderr


# --- Bytes dropped from the middle ----------------------------------------------


async def test_the_bytes_cut_from_each_stream_are_counted(runner: HookRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    limit = 3000
    monkeypatch.setattr(runner_mod, "HOOK_OUTPUT_CAPTURE_LIMIT_BYTES", limit)
    lines = 2000
    out_line = "out %04d " + "o" * 80 + "\n"
    err_line = "err %04d " + "e" * 40 + "\n"
    script = textwrap.dedent(
        f"""
        import sys
        for i in range({lines}):
            sys.stdout.write({out_line!r} % i)
            sys.stderr.write({err_line!r} % i)
        """
    )
    out_total = sum(len((out_line % i).encode()) for i in range(lines))
    err_total = sum(len((err_line % i).encode()) for i in range(lines))

    result = await runner.run_and_wait(_hook(script), {})

    assert result.exit_code == 0
    assert out_total - limit <= result.stdout_dropped < out_total
    assert err_total - limit <= result.stderr_dropped < err_total
    assert result.stdout_dropped != result.stderr_dropped


async def test_nothing_is_counted_as_dropped_when_the_output_fits(runner: HookRunner) -> None:
    result = await runner.run_and_wait(_hook("import sys; print('short'); sys.stderr.write('also short')"), {})
    assert "short" in result.stdout
    assert (result.stdout_dropped, result.stderr_dropped) == (0, 0)


# --- Result file status ---------------------------------------------------------


@pytest.mark.parametrize(
    ("script", "status", "decision"),
    [
        ("pass", "missing", {}),
        (_write_result(""), "missing", {}),
        (_write_result('{"action": "block", "reason": "no"}'), "ok", {"action": "block", "reason": "no"}),
        (_write_result("{}"), "ok", {}),
        (_write_result("this is not json"), "invalid", {}),
        (_write_result('["an", "array"]'), "invalid", {}),
        (_write_result('"a string"'), "invalid", {}),
    ],
    ids=["absent", "empty", "object", "empty-object", "not-json", "array", "string"],
)
async def test_no_result_file_is_told_apart_from_an_invalid_one(
    runner: HookRunner, script: str, status: str, decision: dict[str, object]
) -> None:
    result = await runner.run_and_wait(_hook(script), {})
    assert result.exit_code == 0
    assert result.result_file == status
    assert result.decision == decision


async def test_a_result_file_over_the_size_limit_is_invalid(
    runner: HookRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_mod, "HOOK_RESULT_FILE_MAX_BYTES", 100)
    text = json.dumps({"action": "block", "reason": "x" * 200})
    result = await runner.run_and_wait(_hook(_write_result(text)), {})
    assert result.result_file == "invalid"
    assert result.decision == {}


# --- payload_text ---------------------------------------------------------------


def _copy_payload_to(target: Path) -> str:
    return textwrap.dedent(
        f"""
        import os
        with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"], "rb") as source:
            data = source.read()
        with open({str(target)!r}, "wb") as copy:
            copy.write(data)
        """
    )


async def test_a_given_payload_text_is_written_to_the_payload_file_as_it_is(runner: HookRunner, tmp_path: Path) -> None:
    received = tmp_path / "received"
    payload = {"event": "before_tool_call", "session_id": "s1", "profile": "Code", "tool": {"args": {"p": "é"}}}
    # Spacing and key order no serializer of *payload* would produce, and a non-ASCII character.
    text = '{ "tool":{"args": {"p":"é"}} ,"event":"before_tool_call",  "session_id":"s1","profile":"Code"}'
    result = await runner.run_and_wait(_hook(_copy_payload_to(received)), payload, payload_text=text)
    assert result.exit_code == 0
    assert received.read_bytes() == text.encode("utf-8")


async def test_without_payload_text_the_payload_is_serialized_as_before(runner: HookRunner, tmp_path: Path) -> None:
    received = tmp_path / "received"
    payload = {"event": "before_tool_call", "tool": {"args": {"path": Path("/x/y"), "n": 1}}}
    result = await runner.run_and_wait(_hook(_copy_payload_to(received)), payload)
    assert result.exit_code == 0
    assert received.read_bytes() == json.dumps(payload, default=str).encode("utf-8")


# --- Detached hand-off ----------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="detached worker timing is flaky on Windows CI")
async def test_a_detached_hand_off_returns_its_log_path(runner: HookRunner, hooks_dir: Path) -> None:
    script = "print('written by the detached hook', flush=True)"
    log_path = await runner.spawn_detached(_detached_hook(script), {"session_id": "abc", "event": "session_end"})
    assert isinstance(log_path, Path)
    assert log_path.parent == hooks_dir / "logs" / "abc"
    assert log_path.name.startswith("handed-off.") and log_path.suffix == ".log"
    await wait_for(
        lambda: log_path.exists() and "written by the detached hook" in log_path.read_text(encoding="utf-8"),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the detached hook wrote its log",
    )


@pytest.mark.skipif(sys.platform == "win32", reason="detached worker timing is flaky on Windows CI")
async def test_a_detached_hand_off_writes_the_given_payload_text_as_it_is(runner: HookRunner, tmp_path: Path) -> None:
    received = tmp_path / "received"
    payload = {"session_id": "abc", "event": "session_end"}
    text = '{"event":"session_end",   "session_id":"abc"}'
    await runner.spawn_detached(_detached_hook(_copy_payload_to(received)), payload, payload_text=text)
    await wait_for(
        lambda: received.exists() and received.read_bytes() == text.encode("utf-8"),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the detached hook read the given payload text",
    )
