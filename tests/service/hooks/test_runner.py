# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the hook subprocess runner.

These tests shell out to ``python -c`` so they're platform-portable
without a fixture script tree.  They cover the happy path, the
``CHRYS_HOOK_RESULT`` contract, the timeout, and the env-var
plumbing.  The ``detach`` path is harder to test deterministically
(by design — we lose visibility of the child) so it gets a single
smoke test that just confirms detached launch doesn't raise.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from chrys.foundation.platform import process as process_mod
from chrys.foundation.platform import runtime_paths, script_commands
from chrys.service.hooks import runner as runner_mod
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.loader import load_hooks_project
from chrys.service.hooks.runner import HookResult, HookRunner
from chrys.service.hooks.schema import HookConfig, HookExecution, HookMatch, HookRun
from tests.support.stopped_child import STOPS_ITSELF
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for, wait_until


@pytest.fixture
def runner(tmp_path: Path) -> HookRunner:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    return HookRunner(hooks_dir=hooks_dir)


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


def _command_hook(*, argv: list[str], timeout: float = 5.0) -> HookConfig:
    return HookConfig(
        id="t",
        event=HookEvent.BEFORE_TOOL_CALL,
        run=HookRun(type="command", argv=argv),
        execution=HookExecution(mode="blocking", timeout_seconds=timeout),
        match=HookMatch(),
    )


def _process_state(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)],
        capture_output=True,
        check=False,
        text=True,
    )
    state = result.stdout.strip()
    return state[:1] if state else None


def test_command_invocation_demotes_runtime_path_when_frozen(
    runner: HookRunner,
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
    hook = _command_hook(argv=["python", "--version"])

    invocation = runner._build_invocation(hook, {"cwd": str(tmp_path), "profile": "Code", "session_id": "s1"})

    assert invocation.env["PATH"].split(os.pathsep)[:2] == [str(system_bin), str(runtime_bin)]


def test_command_invocation_strips_inherited_python_runtime_overrides(
    runner: HookRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONHOME", "/bad/home")
    monkeypatch.setenv("PYTHONPATH", "/bad/path")
    hook = _command_hook(argv=["python", "--version"])

    invocation = runner._build_invocation(hook, {"cwd": str(tmp_path), "profile": "Code", "session_id": "s1"})

    try:
        env_keys = {key.upper() for key in invocation.env}
        assert "PYTHONHOME" not in env_keys
        assert "PYTHONPATH" not in env_keys
    finally:
        invocation.result_path.unlink(missing_ok=True)
        invocation.payload_path.unlink(missing_ok=True)


def test_command_invocation_allows_explicit_python_runtime_overrides(
    runner: HookRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONHOME", "/bad/home")
    monkeypatch.setenv("PYTHONPATH", "/bad/path")
    hook = HookConfig(
        id="explicit-python-env",
        event=HookEvent.SESSION_START,
        run=HookRun(
            type="command",
            argv=["python", "--version"],
            env={"PYTHONHOME": "/explicit/home", "PYTHONPATH": "/explicit/path"},
        ),
        execution=HookExecution(mode="blocking", timeout_seconds=5),
        match=HookMatch(),
    )

    invocation = runner._build_invocation(hook, {"cwd": str(tmp_path), "profile": "Code", "session_id": "s1"})

    try:
        assert invocation.env["PYTHONHOME"] == "/explicit/home"
        assert invocation.env["PYTHONPATH"] == "/explicit/path"
    finally:
        invocation.result_path.unlink(missing_ok=True)
        invocation.payload_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_command_runs_and_captures_stdout(runner: HookRunner) -> None:
    hook = _command_hook(argv=[sys.executable, "-c", "print('hello')"])
    result = await runner.run_and_wait(hook, {"profile": "Code", "session_id": "s1"})
    assert result.exit_code == 0
    assert "hello" in result.stdout
    assert result.timed_out is False


@pytest.mark.asyncio
async def test_decision_via_result_file(runner: HookRunner) -> None:
    script = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "block", "reason": "policy: nope"}, f)
        print("debug log line")
        """
    )
    hook = _command_hook(argv=[sys.executable, "-c", script])
    result = await runner.run_and_wait(hook, {"profile": "Code"})
    assert result.exit_code == 0
    # stdout reserved for logs, not the decision.
    assert "debug log line" in result.stdout
    assert result.decision == {"action": "block", "reason": "policy: nope"}


@pytest.mark.asyncio
async def test_noisy_hook_output_is_bounded_and_the_decision_still_comes_from_the_result_file(
    runner: HookRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_mod, "HOOK_OUTPUT_CAPTURE_LIMIT_BYTES", 3000)
    script = textwrap.dedent(
        """
        import json, os, sys
        for i in range(2000):
            sys.stdout.write("log %04d %s\\n" % (i, "o" * 80))
            sys.stderr.write("err %04d %s\\n" % (i, "e" * 80))
        print("last log line")
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "block", "reason": "policy: nope"}, f)
        """
    )
    hook = _command_hook(argv=[sys.executable, "-c", script], timeout=ENGINE_TURN_TIMEOUT)

    result = await runner.run_and_wait(hook, {"profile": "Code"})

    assert result.decision == {"action": "block", "reason": "policy: nope"}
    assert result.stdout.startswith("log 0000 ") and result.stdout.rstrip().endswith("last log line")
    assert result.stderr.startswith("err 0000 ") and result.stderr.rstrip().endswith("err 1999 " + "e" * 80)
    assert "bytes omitted" in result.stdout and len(result.stdout) < 3100
    assert "bytes omitted" in result.stderr and len(result.stderr) < 3100


@pytest.mark.asyncio
async def test_empty_result_file_is_no_opinion(runner: HookRunner) -> None:
    hook = _command_hook(argv=[sys.executable, "-c", "pass"])
    result = await runner.run_and_wait(hook, {})
    assert result.exit_code == 0
    assert result.decision == {}


def test_oversized_result_file_is_rejected_before_json_parse(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_mod, "HOOK_RESULT_FILE_MAX_BYTES", 100)
    path = tmp_path / "oversized.json"
    path.write_bytes(b'{"action":"allow","padding":"' + b"x" * 100 + b'"}')

    with caplog.at_level(logging.WARNING, logger="chrys.service.hooks.runner"):
        result = runner_mod._read_result_file(path)

    assert result == ({}, "invalid")
    assert "exceeded the 100-byte limit" in caplog.text


def test_result_file_at_byte_limit_is_parsed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = b'{"action":"allow"}'
    monkeypatch.setattr(runner_mod, "HOOK_RESULT_FILE_MAX_BYTES", len(payload))
    path = tmp_path / "at-limit.json"
    path.write_bytes(payload)

    assert runner_mod._read_result_file(path) == ({"action": "allow"}, "ok")


@pytest.mark.asyncio
async def test_payload_visible_via_file(runner: HookRunner) -> None:
    script = textwrap.dedent(
        """
        import os, json
        with open(os.environ["CHRYS_HOOK_PAYLOAD_FILE"]) as payload_file:
            payload = json.load(payload_file)
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"extra_context": payload["event"]}, f)
        """
    )
    hook = _command_hook(argv=[sys.executable, "-c", script])
    result = await runner.run_and_wait(hook, {"event": "before_tool_call"})
    assert result.decision == {"extra_context": "before_tool_call"}


@pytest.mark.asyncio
async def test_launch_error_returns_failed_result(runner: HookRunner) -> None:
    hook = _command_hook(argv=["/definitely/not/a/real/chrys-hook-command"])
    result = await runner.run_and_wait(hook, {})
    assert result.exit_code is None
    assert result.stderr


@pytest.mark.asyncio
async def test_timeout_kills_process(runner: HookRunner) -> None:
    hook = _command_hook(
        argv=[sys.executable, "-c", "import time; time.sleep(10)"],
        timeout=0.5,
    )
    result = await runner.run_and_wait(hook, {})
    assert result.timed_out is True
    assert result.exit_code is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
@pytest.mark.asyncio
async def test_stopped_grandchild_hook_returns_and_cleans_group(runner: HookRunner, tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    child = (
        f"import os, pathlib; pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='ascii'); "
        + STOPS_ITSELF
    )
    script = textwrap.dedent(
        f"""
        import subprocess
        import sys

        subprocess.Popen([sys.executable, "-c", {child!r}])
        """
    )
    hook = _command_hook(argv=[sys.executable, "-c", script], timeout=10)

    result = await runner.run_and_wait(hook, {})

    assert result.timed_out is True
    assert result.stopped is True
    assert "stopped state" in result.stderr
    child_pid = int(pid_file.read_text(encoding="ascii"))
    for _ in range(20):
        state = _process_state(child_pid)
        if state is None or state == "Z":
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail(f"stopped hook grandchild survived cleanup with state={state!r}")


# Stands in for ``taskkill``: it announces itself, then exits only once the test
# creates the release file, and records that it saw it.
_TREE_KILL_STAND_IN = textwrap.dedent(
    """
    import pathlib, sys, time
    started, release, released = map(pathlib.Path, sys.argv[1:])
    started.touch()
    deadline = time.monotonic() + 20
    while not release.exists():
        if time.monotonic() > deadline:
            sys.exit(1)
        time.sleep(0.01)
    released.touch()
    """
)


def _route_tree_kill_to_stand_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, list[int]]:
    """Send every process tree kill to the stand-in.

    Returns its release and released files and the pids the kills were aimed at.
    """
    started, release, released = tmp_path / "started", tmp_path / "release", tmp_path / "released"
    targets: list[int] = []

    def tree_kill_argv(pid: int) -> list[str]:
        targets.append(pid)
        return [sys.executable, "-c", _TREE_KILL_STAND_IN, str(started), str(release), str(released)]

    monkeypatch.setattr(process_mod, "_windows_tree_kill_argv", tree_kill_argv)
    monkeypatch.setattr(process_mod, "_WINDOWS_TREE_KILL_TIMEOUT", 25.0)
    return release, released, targets


async def _time_out_into_the_tree_kill(runner: HookRunner, tmp_path: Path) -> asyncio.Task[HookResult]:
    """Start a hook that times out, and return its run once the stand-in tree kill is running."""
    hook = _command_hook(argv=[sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.2)
    run = asyncio.create_task(runner.run_and_wait(hook, {}))
    started = tmp_path / "started"
    await wait_for(lambda: started.exists() or run.done(), timeout=20, description="the tree kill to start")
    assert not run.done(), f"the hook finished before this test saw its tree kill running: {run.result()!r}"
    return run


@pytest.mark.asyncio
async def test_timeout_keeps_the_loop_running_while_the_process_tree_kill_runs(
    runner: HookRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out hook's tree kill is a subprocess the loop awaits, not one it blocks on.

    Only this coroutine creates the release file, so a kill that held the loop until
    its own deadline would leave the stand-in waiting and never mark it released.
    """
    release, released, targets = _route_tree_kill_to_stand_in(monkeypatch, tmp_path)
    run = await _time_out_into_the_tree_kill(runner, tmp_path)

    release.touch()
    result = await asyncio.wait_for(run, timeout=20)

    assert result.timed_out is True
    assert len(targets) == 1
    assert released.exists()


@pytest.mark.asyncio
async def test_cancelling_a_hook_during_its_process_tree_kill_lets_the_kill_finish(
    runner: HookRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hook cancelled mid tree kill, as a drain past its grace does, lets the kill finish first.

    Stopping the kill there would leave cleanup to end only the direct child, and the
    descendants the kill had yet to reach would keep running.
    """
    release, released, _targets = _route_tree_kill_to_stand_in(monkeypatch, tmp_path)
    run = await _time_out_into_the_tree_kill(runner, tmp_path)

    run.cancel()
    assert not await wait_until(run.done, timeout=0.5), "the cancellation cut the tree kill short"
    release.touch()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run, timeout=20)

    assert released.exists()


@pytest.mark.asyncio
async def test_nonzero_exit_recorded(runner: HookRunner) -> None:
    hook = _command_hook(argv=[sys.executable, "-c", "import sys; sys.exit(7)"])
    result = await runner.run_and_wait(hook, {})
    assert result.exit_code == 7


@pytest.mark.asyncio
async def test_extra_env_passed_through(runner: HookRunner) -> None:
    script = textwrap.dedent(
        """
        import os, json
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"extra_context": os.environ.get("MY_VAR", "")}, f)
        """
    )
    hook = HookConfig(
        id="env",
        event=HookEvent.SESSION_START,
        run=HookRun(type="command", argv=[sys.executable, "-c", script], env={"MY_VAR": "set-via-${profile}"}),
        execution=HookExecution(mode="blocking", timeout_seconds=5),
        match=HookMatch(),
    )
    result = await runner.run_and_wait(hook, {"profile": "Code"})
    assert result.decision == {"extra_context": "set-via-Code"}


@pytest.mark.asyncio
async def test_script_path_resolves_relative_to_hooks_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:

    hooks_dir = tmp_path / "hooks"
    scripts_dir = hooks_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    script = scripts_dir / "smoke.py"
    script.write_text("print('from-script')\n", encoding="utf-8")

    runner = HookRunner(hooks_dir=hooks_dir)
    hook = HookConfig(
        id="s",
        event=HookEvent.SESSION_START,
        run=HookRun(type="script", path="scripts/smoke.py"),
        execution=HookExecution(mode="blocking", timeout_seconds=5),
        match=HookMatch(),
    )
    monkeypatch.setattr(script_commands, "_find_python_runner", lambda: [sys.executable])
    result = await runner.run_and_wait(hook, {})
    assert result.exit_code == 0
    assert "from-script" in result.stdout


@pytest.mark.asyncio
async def test_project_script_path_resolves_relative_to_project_hooks_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    config_hooks_dir = tmp_path / "config" / "hooks"
    project_scripts_dir = project_root / ".chrys" / "hooks" / "scripts"
    project_scripts_dir.mkdir(parents=True)
    config_hooks_dir.mkdir(parents=True)
    (project_root / ".chrys" / "hooks" / "hooks.yaml").write_text(
        """\
version: 1
hooks:
  - id: project-script
    event: session_start
    run:
      type: script
      path: scripts/project_smoke.py
    execution:
      mode: blocking
      timeout_seconds: 5
""",
        encoding="utf-8",
    )
    (project_scripts_dir / "project_smoke.py").write_text("print('from-project-script')\n", encoding="utf-8")

    file = load_hooks_project(project_root)
    assert file is not None
    hook = file.hooks[0]
    runner = HookRunner(hooks_dir=config_hooks_dir)

    monkeypatch.setattr(script_commands, "_find_python_runner", lambda: [sys.executable])
    result = await runner.run_and_wait(hook, {})

    assert result.exit_code == 0
    assert "from-project-script" in result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="setsid not available on Windows")
@pytest.mark.asyncio
async def test_detached_spawn_does_not_raise(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    runner = HookRunner(hooks_dir=hooks_dir)
    hook = HookConfig(
        id="detached",
        event=HookEvent.SESSION_END,
        run=HookRun(type="command", argv=[sys.executable, "-c", "pass"]),
        execution=HookExecution(mode="fire_and_forget", detach=True, timeout_seconds=5),
        match=HookMatch(),
    )
    await runner.spawn_detached(hook, {"session_id": "abc"})
    # Poll for the detached child to flush its log dir: a fixed sleep races the
    # detached subprocess spawn under slow/parallel CI.
    log_root = hooks_dir / "logs" / "abc"
    assert await wait_until(log_root.exists), "expected log dir for detached hook"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
@pytest.mark.asyncio
async def test_detached_log_file_is_owner_only(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    runner = HookRunner(hooks_dir=hooks_dir)
    hook = HookConfig(
        id="detached",
        event=HookEvent.SESSION_END,
        run=HookRun(type="command", argv=[sys.executable, "-c", "pass"]),
        execution=HookExecution(mode="fire_and_forget", detach=True, timeout_seconds=5),
        match=HookMatch(),
    )
    await runner.spawn_detached(hook, {"session_id": "abc"})

    log_files = list((hooks_dir / "logs" / "abc").glob("*.log"))
    assert log_files, "expected detached hook log file"
    assert os.stat(log_files[0]).st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_detached_spawn_uses_windows_creation_flags(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import subprocess

    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    runner = HookRunner(hooks_dir=hooks_dir)
    hook = HookConfig(
        id="detached-win",
        event=HookEvent.SESSION_END,
        run=HookRun(type="command", argv=[sys.executable, "-c", "pass"]),
        execution=HookExecution(mode="fire_and_forget", detach=True, timeout_seconds=5),
        match=HookMatch(),
    )
    seen: dict[str, object] = {}

    class _FakePopen:
        def __init__(self, cmd: list[str], **kwargs: object) -> None:
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs

    def _fake_popen(cmd: list[str], **kwargs: object) -> _FakePopen:
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return _FakePopen(cmd, **kwargs)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(runtime_paths.sysconfig, "get_path", lambda name: "")
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x00000008, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.setenv("PYAPP", "1")
    monkeypatch.setenv("PYTHONHOME", "/bad/home")
    monkeypatch.setenv("PYTHONPATH", "/bad/path")

    await runner.spawn_detached(hook, {"session_id": "abc"})

    cmd = seen["cmd"]
    assert isinstance(cmd, list)
    assert cmd[:4] == [sys.executable, "-s", "-m", "chrys.service.hooks.detached_worker"]
    assert len(cmd) == 5
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["creationflags"] == 0x00000208
    assert "start_new_session" not in kwargs
    assert kwargs["stdout"] is kwargs["stderr"]
    env = kwargs["env"]
    assert isinstance(env, dict)
    env_keys = {str(key).upper() for key in env}
    assert "PYTHONHOME" not in env_keys
    assert "PYTHONPATH" not in env_keys
    assert env["PYTHONNOUSERSITE"] == "1"


@pytest.mark.asyncio
async def test_detached_spawn_keeps_user_site_for_non_frozen_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    runner = HookRunner(hooks_dir=hooks_dir)
    hook = HookConfig(
        id="detached-user-site",
        event=HookEvent.SESSION_END,
        run=HookRun(type="command", argv=[sys.executable, "-c", "pass"]),
        execution=HookExecution(mode="fire_and_forget", detach=True, timeout_seconds=5),
        match=HookMatch(),
    )
    seen: dict[str, object] = {}

    class _FakePopen:
        def __init__(self, cmd: list[str], **kwargs: object) -> None:
            seen["cmd"] = cmd
            seen["kwargs"] = kwargs

    def _fake_popen(cmd: list[str], **kwargs: object) -> _FakePopen:
        return _FakePopen(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    monkeypatch.delenv("PYAPP", raising=False)
    monkeypatch.delenv("CHRYS_FORCE_FROZEN", raising=False)
    monkeypatch.delenv("PYTHONNOUSERSITE", raising=False)

    await runner.spawn_detached(hook, {"session_id": "abc"})

    cmd = seen["cmd"]
    assert isinstance(cmd, list)
    assert cmd[:3] == [sys.executable, "-m", "chrys.service.hooks.detached_worker"]
    assert len(cmd) == 4
    kwargs = seen["kwargs"]
    assert isinstance(kwargs, dict)
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert "PYTHONNOUSERSITE" not in env


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
@pytest.mark.asyncio
async def test_detached_invocation_file_is_owner_only(tmp_path: Path) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    runner = HookRunner(hooks_dir=hooks_dir)
    hook = HookConfig(
        id="detached",
        event=HookEvent.SESSION_END,
        run=HookRun(type="command", argv=[sys.executable, "-c", "pass"]),
        execution=HookExecution(mode="fire_and_forget", detach=True, timeout_seconds=5),
        match=HookMatch(),
    )
    invocation = runner._build_invocation(hook, {"session_id": "abc"})
    invocation_path = runner._write_detached_invocation(invocation, hook_id=hook.id, job=None)
    try:
        assert os.stat(invocation_path).st_mode & 0o777 == 0o600
    finally:
        invocation_path.unlink(missing_ok=True)
        invocation.result_path.unlink(missing_ok=True)
        invocation.payload_path.unlink(missing_ok=True)


def test_detached_invocation_json_strips_inherited_python_runtime_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    runner = HookRunner(hooks_dir=hooks_dir)
    hook = HookConfig(
        id="detached",
        event=HookEvent.SESSION_END,
        run=HookRun(type="command", argv=[sys.executable, "-c", "pass"]),
        execution=HookExecution(mode="fire_and_forget", detach=True, timeout_seconds=5),
        match=HookMatch(),
    )
    monkeypatch.setenv("PYTHONHOME", "/bad/home")
    monkeypatch.setenv("PYTHONPATH", "/bad/path")
    invocation = runner._build_invocation(hook, {"session_id": "abc"})
    invocation_path = runner._write_detached_invocation(invocation, hook_id=hook.id, job=None)
    try:
        raw = json.loads(invocation_path.read_text(encoding="utf-8"))
        env = raw["env"]
        assert isinstance(env, dict)
        env_keys = {key.upper() for key in env}
        assert "PYTHONHOME" not in env_keys
        assert "PYTHONPATH" not in env_keys
    finally:
        invocation_path.unlink(missing_ok=True)
        invocation.result_path.unlink(missing_ok=True)
        invocation.payload_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_a_shell_script_hook_with_no_bash_fails_without_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_bash() -> str:
        raise FileNotFoundError(errno.ENOENT, script_commands._NO_WINDOWS_BASH)

    hooks_dir = tmp_path / "hooks"
    (hooks_dir / "scripts").mkdir(parents=True)
    (hooks_dir / "scripts" / "notify.sh").write_text("echo 1\n", encoding="utf-8")
    monkeypatch.setattr(script_commands, "_find_bash", no_bash)
    hook = HookConfig(
        id="s",
        event=HookEvent.SESSION_START,
        run=HookRun(type="script", path="scripts/notify.sh"),
        execution=HookExecution(mode="blocking", timeout_seconds=5),
        match=HookMatch(),
    )

    result = await HookRunner(hooks_dir=hooks_dir).run_and_wait(hook, {})

    assert result.exit_code is None
    assert script_commands._NO_WINDOWS_BASH in result.stderr
