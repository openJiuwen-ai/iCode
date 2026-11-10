# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shell sandbox bypass: enabled calls ``run_shell``; disabled keeps the host executor."""

from __future__ import annotations

import ast
import dataclasses
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest

import chrys.service.tools.builtins.shell as shell_mod
import chrys.service.tools.packs as packs_mod
from chrys.foundation.config.process_env import freeze_process_env
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import load_settings
from chrys.foundation.config.user_settings import user_settings_path
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.platform import ShellInfo
from chrys.foundation.platform.output_capture import CapturedOutput
from chrys.foundation.tool_result_metadata import (
    SHELL_BACKGROUND_TASK_METADATA_KEY,
    SHELL_EXIT_CODE_METADATA_KEY,
    SHELL_TIMED_OUT_METADATA_KEY,
    TOOL_ERROR_KIND_METADATA_KEY,
    TOOL_ERRORED_METADATA_KEY,
)
from chrys.kernel.tools import FunctionTool
from chrys.service.profiles.agents.schema import ToolsConfig
from chrys.service.sandbox.backend import SandboxError, SandboxExec
from chrys.service.tools.approval_targets import ApprovedTargets, approved_targets
from chrys.service.tools.background import BackgroundTasks
from chrys.service.tools.builtins.shell import (
    ShellTools,
    _prepare_pipe_command,
    _shell_argv_variants,
    shell_result_metadata,
)
from chrys.service.tools.builtins.shell_filter import ShellCommandFilter
from chrys.service.tools.result_metadata import tool_result_metadata

_LINUX_ONLY = "Sandbox execution is supported only on Linux."
_HOST_EXECUTORS = ("_execute_pty", "_execute_pipe", "_spawn_pty", "_spawn_pipe")
_SANDBOX_TIMEOUT = "Sandbox request timed out."
_CHOSEN_SHELLS = (
    ShellInfo(name="sh", path="/bin/sh", args=["-c"]),
    ShellInfo(name="bash", path="/bin/bash", args=["-c"], version="5.2"),
    ShellInfo(name="powershell", path="/powershell", args=["-Command"], version="5.1"),
)


class _ShellCall:
    """One recorded ``run_shell`` invocation."""

    def __init__(self, settings: object, argv: tuple[str, ...], cwd: str, timeout_seconds: int, linux: bool) -> None:
        self.settings = settings
        self.argv = argv
        self.cwd = cwd
        self.timeout_seconds = timeout_seconds
        self.linux = linux


@pytest.fixture
def runtime() -> SessionEnvironment:
    return SessionEnvironment.capture()


@contextmanager
def _metadata() -> Iterator[dict[str, object]]:
    values: dict[str, object] = {}
    shell_token = shell_result_metadata.set(values)
    tool_token = tool_result_metadata.set(values)
    try:
        yield values
    finally:
        tool_result_metadata.reset(tool_token)
        shell_result_metadata.reset(shell_token)


def _bytes_capture(text: str) -> CapturedOutput:
    raw = text.encode("utf-8")
    return CapturedOutput(raw, b"", len(raw))


def _forbid_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if the sandbox path reaches a host executor or spawn."""

    async def execute_pipe(
        self: ShellTools,
        command: str,
        shell: ShellInfo,
        cwd: str,
        timeout: int | float,
        reason: str = "",
    ) -> tuple[CapturedOutput, int, CapturedOutput]:
        raise AssertionError(f"host execute called: {command}")

    async def execute_pty(
        self: ShellTools,
        command: str,
        shell: ShellInfo,
        cwd: str,
        timeout: int | float,
        reason: str = "",
    ) -> tuple[CapturedOutput, int]:
        raise AssertionError(f"host execute called: {command}")

    async def spawn_pipe(command: str, shell: ShellInfo, cwd: str) -> None:
        raise AssertionError(f"host spawn called: {command}")

    async def spawn_pty(command: str, shell: ShellInfo, cwd: str) -> None:
        raise AssertionError(f"host spawn called: {command}")

    monkeypatch.setattr(ShellTools, "_execute_pipe", execute_pipe)
    monkeypatch.setattr(ShellTools, "_execute_pty", execute_pty)
    monkeypatch.setattr(shell_mod, "_spawn_pipe", spawn_pipe)
    monkeypatch.setattr(shell_mod, "_spawn_pty", spawn_pty)


def _spy_host(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count host executor and spawn calls. A call fails the test."""
    counts = dict.fromkeys(_HOST_EXECUTORS, 0)

    async def execute_pipe(
        self: ShellTools,
        command: str,
        shell: ShellInfo,
        cwd: str,
        timeout: int | float,
        reason: str = "",
    ) -> tuple[CapturedOutput, int, CapturedOutput]:
        counts["_execute_pipe"] += 1
        raise AssertionError(f"host execute called: {command}")

    async def execute_pty(
        self: ShellTools,
        command: str,
        shell: ShellInfo,
        cwd: str,
        timeout: int | float,
        reason: str = "",
    ) -> tuple[CapturedOutput, int]:
        counts["_execute_pty"] += 1
        raise AssertionError(f"host execute called: {command}")

    async def spawn_pipe(command: str, shell: ShellInfo, cwd: str) -> None:
        counts["_spawn_pipe"] += 1
        raise AssertionError(f"host spawn called: {command}")

    async def spawn_pty(command: str, shell: ShellInfo, cwd: str) -> None:
        counts["_spawn_pty"] += 1
        raise AssertionError(f"host spawn called: {command}")

    monkeypatch.setattr(ShellTools, "_execute_pipe", execute_pipe)
    monkeypatch.setattr(ShellTools, "_execute_pty", execute_pty)
    monkeypatch.setattr(shell_mod, "_spawn_pipe", spawn_pipe)
    monkeypatch.setattr(shell_mod, "_spawn_pty", spawn_pty)
    return counts


def _assert_host_idle(counts: dict[str, int]) -> None:
    assert counts == dict.fromkeys(_HOST_EXECUTORS, 0)


def _pipe_argv(shell: ShellInfo, command: str) -> tuple[str, ...]:
    """The bare argv ``_run_sandboxed`` passes to ``run_shell`` for *shell*."""
    prepared = _prepare_pipe_command(command, shell)
    variants = _shell_argv_variants(shell, prepared)
    return variants[-1]


def _shell_source() -> ast.Module:
    return ast.parse(Path(shell_mod.__file__).read_text(encoding="utf-8"))


def _shell_tools_class(tree: ast.Module) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ShellTools":
            return node
    raise AssertionError("ShellTools is missing from shell.py")


def _async_method(cls: ast.ClassDef, name: str) -> ast.AsyncFunctionDef:
    for node in cls.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is missing from ShellTools")


def _call_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Attribute):
            names.add(func.attr)
        elif isinstance(func, ast.Name):
            names.add(func.id)
    return names


def _assert_sandbox_bypass_avoids_host(tree: ast.Module) -> None:
    """The enabled check returns before any host executor, and the sandbox path never calls one."""
    run = _async_method(_shell_tools_class(tree), "_run")
    bypass_at: int | None = None
    host_at: int | None = None
    for index, stmt in enumerate(run.body):
        names = _call_names(stmt)
        if bypass_at is None and "_run_sandboxed" in names:
            bypass_at = index
        if host_at is None and not names.isdisjoint(_HOST_EXECUTORS):
            host_at = index
    assert bypass_at is not None
    assert host_at is not None
    assert bypass_at <= 1
    assert bypass_at < host_at
    sandboxed = _call_names(_async_method(_shell_tools_class(tree), "_run_sandboxed"))
    assert sandboxed.isdisjoint(_HOST_EXECUTORS)
    assert sandboxed.isdisjoint({"spawn_managed_stdio_process", "adopt", "_backgrounded_result", "_follow"})
    assert "run_shell" in sandboxed


def _load_invalid_sandbox(monkeypatch: pytest.MonkeyPatch, source: str) -> Settings:
    """A sealed ``sandbox.enabled``: the value is False and the loader still warned."""
    if source == "user_yaml":
        path = user_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("sandbox:\n  enabled: banana\n", encoding="utf-8")
    else:
        monkeypatch.setenv("CHRYS_SANDBOX_ENABLED", "banana")
    freeze_process_env()
    loaded = load_settings()
    assert loaded.settings.sandbox_enabled is False
    assert loaded.settings.sandbox_enabled_was_rejected() is True
    assert any(warning.key == "sandbox.enabled" and warning.rejected for warning in loaded.warnings)
    return loaded.settings


async def _assert_command_is_not_run(shell: ShellTools, monkeypatch: pytest.MonkeyPatch) -> None:
    counts = _spy_host(monkeypatch)
    calls: list[_ShellCall] = []
    _patch_run_shell(monkeypatch, calls, SandboxExec(0, "should-not-run", ""))

    with _metadata() as metadata:
        result = await shell.execute("echo hello", reason="test")

    assert result.startswith("Error:")
    assert "sandbox.enabled is invalid" in result
    assert "[exit_code:" not in result
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "sandbox_unavailable"
    assert metadata[TOOL_ERRORED_METADATA_KEY] is True
    assert calls == []
    _assert_host_idle(counts)


def _packed_command_tools(
    runtime: SessionEnvironment, settings: Settings, background: BackgroundTasks | None
) -> list[FunctionTool]:
    tools = packs_mod._create_shell(
        packs_mod.ToolBuild(
            tools=ToolsConfig(builtins=["shell"]),
            agent_name="test",
            runtime=runtime,
            settings=settings,
            background_tasks=background,
        )
    )
    instances = _shell_instances(tools)
    assert instances
    command_tools: list[FunctionTool] = []
    for instance in instances:
        matched = [
            tool
            for tool in tools
            if isinstance(tool, FunctionTool) and tool.bound_instance is instance and tool.name == instance.shell.name
        ]
        assert len(matched) == 1
        command_tools.append(matched[0])
        if settings.sandbox_enabled:
            assert instance._tasks is None
        else:
            assert instance._tasks is background
    return command_tools


def _shell_instances(tools: Sequence[object]) -> list[ShellTools]:
    instances: list[ShellTools] = []
    for tool in tools:
        if not isinstance(tool, FunctionTool):
            continue
        owner = tool.bound_instance
        if isinstance(owner, ShellTools) and tool.name == owner.shell.name:
            instances.append(owner)
    return instances


def _stub_host_executor(monkeypatch: pytest.MonkeyPatch, host_calls: list[str]) -> None:
    """Replace the host executors, and still refuse a real spawn."""
    _forbid_host(monkeypatch)
    stdout = _bytes_capture("host-executor-output")
    stderr = _bytes_capture("")

    async def execute_pipe(
        self: ShellTools,
        command: str,
        shell: ShellInfo,
        cwd: str,
        timeout: int | float,
        reason: str = "",
    ) -> tuple[CapturedOutput, int, CapturedOutput]:
        host_calls.append("pipe")
        return stdout, 0, stderr

    async def execute_pty(
        self: ShellTools,
        command: str,
        shell: ShellInfo,
        cwd: str,
        timeout: int | float,
        reason: str = "",
    ) -> tuple[CapturedOutput, int]:
        host_calls.append("pty")
        return stdout, 0

    monkeypatch.setattr(ShellTools, "_execute_pipe", execute_pipe)
    monkeypatch.setattr(ShellTools, "_execute_pty", execute_pty)


def _patch_run_shell(
    monkeypatch: pytest.MonkeyPatch, calls: list[_ShellCall], result: SandboxExec | SandboxError
) -> None:
    async def run_shell(
        settings: object,
        *,
        argv: Sequence[str],
        cwd: str,
        timeout_seconds: int,
        linux: bool,
    ) -> SandboxExec:
        calls.append(_ShellCall(settings, tuple(argv), cwd, timeout_seconds, linux))
        if isinstance(result, SandboxError):
            raise result
        return result

    monkeypatch.setattr("chrys.service.sandbox.backend.run_shell", run_shell)


def _enabled_shell(
    runtime: SessionEnvironment,
    monkeypatch: pytest.MonkeyPatch,
    *,
    command_filter: ShellCommandFilter | None = None,
) -> tuple[ShellTools, list[_ShellCall]]:
    calls: list[_ShellCall] = []
    _forbid_host(monkeypatch)
    _patch_run_shell(monkeypatch, calls, SandboxExec(0, "should-not-run", ""))
    shell = ShellTools(runtime, command_filter=command_filter, settings=Settings(sandbox_enabled=True))
    return shell, calls


# ───────────────────────── disabled stays on the host ─────────────────────────


@pytest.mark.parametrize(
    "settings",
    [None, Settings(sandbox_enabled=False)],
    ids=["unset", "disabled"],
)
async def test_disabled_sandbox_uses_host_executor(
    runtime: SessionEnvironment, settings: Settings | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_calls: list[str] = []
    sandbox_calls: list[_ShellCall] = []
    _stub_host_executor(monkeypatch, host_calls)
    _patch_run_shell(monkeypatch, sandbox_calls, SandboxExec(0, "sandbox-out", ""))
    shell = ShellTools(runtime, settings=settings)

    result = await shell.execute("echo hello", reason="test")

    assert host_calls == ["pipe" if sys.platform == "win32" else "pty"]
    assert sandbox_calls == []
    assert "host-executor-output" in result
    assert "[exit_code: 0]" in result
    assert "sandbox-out" not in result


# ───────────────────────── enabled uses the sandbox ─────────────────────────


async def test_sandbox_exec_returns_output_without_host(
    runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[_ShellCall] = []
    settings = Settings(sandbox_enabled=True)
    _forbid_host(monkeypatch)
    _patch_run_shell(monkeypatch, calls, SandboxExec(3, "from-sandbox", "sandbox-err"))
    shell = ShellTools(runtime, settings=settings)

    with _metadata() as metadata:
        result = await shell.execute("echo hello", reason="test", timeout=17)

    assert "from-sandbox" in result
    assert "sandbox-err" in result
    assert "[stderr]" in result
    assert result.endswith("[exit_code: 3]")
    assert metadata[SHELL_EXIT_CODE_METADATA_KEY] == 3
    assert metadata.get(TOOL_ERRORED_METADATA_KEY) is not True
    assert len(calls) == 1
    call = calls[0]
    assert call.settings is settings
    assert call.linux is (sys.platform == "linux")
    assert call.timeout_seconds == 17
    assert call.cwd == runtime.cwd
    assert call.argv == _pipe_argv(shell.shell, "echo hello")


async def test_sandbox_error_is_unavailable_and_skips_host(
    runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[_ShellCall] = []
    _forbid_host(monkeypatch)
    _patch_run_shell(monkeypatch, calls, SandboxError("down"))
    shell = ShellTools(runtime, settings=Settings(sandbox_enabled=True))

    with _metadata() as metadata:
        result = await shell.execute("echo hello", reason="test")

    assert result.startswith("Error:")
    assert "down" in result
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "sandbox_unavailable"
    assert metadata[TOOL_ERRORED_METADATA_KEY] is True
    assert len(calls) == 1
    assert "[exit_code:" not in result


async def test_non_linux_sandbox_is_unsupported(runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[_ShellCall] = []
    _forbid_host(monkeypatch)
    _patch_run_shell(monkeypatch, calls, SandboxError(_LINUX_ONLY))
    monkeypatch.setattr(sys, "platform", "darwin")
    shell = ShellTools(runtime, settings=Settings(sandbox_enabled=True))

    with _metadata() as metadata:
        result = await shell.execute("echo hello", reason="test")

    assert result.startswith("Error:")
    assert "supported only on Linux" in result
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "sandbox_unsupported"
    assert len(calls) == 1
    assert calls[0].linux is False
    assert "[exit_code:" not in result


# ───────────────────────── execute returns before the bypass ─────────────────────────


async def test_filter_rejection_returns_before_sandbox(
    runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell, calls = _enabled_shell(runtime, monkeypatch, command_filter=ShellCommandFilter(whitelist={"echo"}))

    result = await shell.execute("rm -rf /tmp/whatever", reason="attempt")

    assert result.startswith("Error:")
    assert "command blocked" in result
    assert calls == []


async def test_missing_working_directory_returns_before_sandbox(
    runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell, calls = _enabled_shell(runtime, monkeypatch)

    result = await shell.execute("echo hello", reason="test", workdir="missing-sandbox-dir")

    assert result.startswith("Error:")
    assert "workdir not found" in result
    assert calls == []


async def test_approval_cwd_mismatch_returns_before_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = dataclasses.replace(SessionEnvironment.capture(), cwd=str(tmp_path))
    shell, calls = _enabled_shell(runtime, monkeypatch)

    with approved_targets(ApprovedTargets(cwd=str(tmp_path / "not-the-approved-cwd"))):
        result = await shell.execute("echo hello", reason="test")

    assert result.startswith("Error:")
    assert "changed after approval" in result
    assert calls == []


# ───────────────────────── enabled result shape ─────────────────────────


async def test_sandbox_long_output_is_truncated_without_host(
    runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    counts = _spy_host(monkeypatch)
    calls: list[_ShellCall] = []
    stdout = "\n".join(f"sandbox-line-{index:04d}-{'x' * 40}" for index in range(400))
    _patch_run_shell(monkeypatch, calls, SandboxExec(5, stdout, ""))
    passed: list[str] = []
    real_bound = shell_mod.bound_process_output

    async def bound_process_output(*args: object, **kwargs: object) -> str:
        text = args[2] if len(args) > 2 else kwargs["text"]
        assert isinstance(text, str)
        passed.append(text)
        return await real_bound(*args, **kwargs)

    monkeypatch.setattr(shell_mod, "bound_process_output", bound_process_output)
    shell = ShellTools(runtime, settings=Settings(sandbox_enabled=True))

    result = await shell.execute("echo hello", reason="test", max_output_tokens=100)

    assert len(passed) == 1
    assert "sandbox-line-0000" in passed[0]
    assert passed[0].endswith("[exit_code: 5]")
    assert "truncated" in result.lower()
    assert result.endswith("[exit_code: 5]")
    assert len(result) < len(passed[0])
    assert len(calls) == 1
    _assert_host_idle(counts)


@pytest.mark.parametrize("shell_info", _CHOSEN_SHELLS, ids=[item.name for item in _CHOSEN_SHELLS])
async def test_sandbox_argv_follows_selected_shell(
    runtime: SessionEnvironment, shell_info: ShellInfo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sandbox argv stays the bare shell launch when an fsatrace prefix is armed."""
    from chrys.service.mutations.trace import shell_trace_argv

    counts = _spy_host(monkeypatch)
    calls: list[_ShellCall] = []
    _patch_run_shell(monkeypatch, calls, SandboxExec(0, "ok", ""))
    shell = ShellTools(runtime, shell=shell_info, settings=Settings(sandbox_enabled=True))
    prefix = ("/opt/not-in-sandbox/fsatrace", "wmdt", "/tmp/chrys-not-a-trace", "--")
    token = shell_trace_argv.set(prefix)
    try:
        result = await shell.execute("echo hello", reason="test")
        variants = _shell_argv_variants(shell.shell, _prepare_pipe_command("echo hello", shell.shell))
    finally:
        shell_trace_argv.reset(token)

    assert shell.shell == shell_info
    assert len(calls) == 1
    assert variants[0][: len(prefix)] == prefix
    assert calls[0].argv == variants[-1]
    assert calls[0].argv[: len(prefix)] != prefix
    assert prefix[0] not in calls[0].argv
    assert result.endswith("[exit_code: 0]")
    _assert_host_idle(counts)


@pytest.mark.parametrize(
    "outcome",
    [SandboxExec(0, "finished", ""), SandboxError(_SANDBOX_TIMEOUT)],
    ids=["completed", "sandbox-timeout"],
)
async def test_sandbox_timeout_does_not_background_or_spawn_host(
    runtime: SessionEnvironment,
    outcome: SandboxExec | SandboxError,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sandboxed = _call_names(_async_method(_shell_tools_class(_shell_source()), "_run_sandboxed"))
    assert sandboxed.isdisjoint(
        {
            "adopt",
            "_backgrounded_result",
            "_record_background_task",
            "_record_shell_timeout",
            "_follow",
            *_HOST_EXECUTORS,
        }
    )
    counts = _spy_host(monkeypatch)
    calls: list[_ShellCall] = []
    _patch_run_shell(monkeypatch, calls, outcome)
    background = BackgroundTasks(tmp_path)
    adopted: list[object] = []
    recorded: list[object] = []
    timeouts: list[int] = []

    def adopt(self: BackgroundTasks, process: object, **kwargs: object) -> None:
        adopted.append(process)
        raise AssertionError(f"sandbox path started a background task: {kwargs}")

    def record_background(task: object) -> None:
        recorded.append(task)

    def record_timeout(timeout: int) -> None:
        timeouts.append(timeout)

    monkeypatch.setattr(BackgroundTasks, "adopt", adopt)
    monkeypatch.setattr(shell_mod, "_record_background_task", record_background)
    monkeypatch.setattr(shell_mod, "_record_shell_timeout", record_timeout)
    shell = ShellTools(runtime, background_tasks=background, settings=Settings(sandbox_enabled=True))

    with _metadata() as metadata:
        result = await shell.execute("echo hello", reason="test", timeout=9)

    assert len(calls) == 1
    assert calls[0].timeout_seconds == 9
    _assert_host_idle(counts)
    assert adopted == []
    assert background.running == []
    assert recorded == []
    assert timeouts == []
    assert SHELL_BACKGROUND_TASK_METADATA_KEY not in metadata
    assert metadata.get(SHELL_TIMED_OUT_METADATA_KEY) is not True
    if isinstance(outcome, SandboxError):
        assert result.startswith("Error:")
        assert _SANDBOX_TIMEOUT in result
        assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "sandbox_unavailable"
        assert metadata[TOOL_ERRORED_METADATA_KEY] is True
        assert "[exit_code:" not in result
    else:
        assert result.endswith("[exit_code: 0]")
        assert "finished" in result
        assert metadata.get(TOOL_ERRORED_METADATA_KEY) is not True


# ───────────────────────── both shell constructions ─────────────────────────


def test_shell_pack_passes_settings() -> None:
    tree = ast.parse(Path(packs_mod.__file__).read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ShellTools"
    ]
    assert len(calls) == 1
    value = next((keyword.value for keyword in calls[0].keywords if keyword.arg == "settings"), None)
    assert isinstance(value, ast.Attribute)
    assert value.attr == "settings"
    assert isinstance(value.value, ast.Name)
    assert value.value.id == "build"


async def test_extra_shell_receives_settings_and_bypasses_host(
    runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_sandbox_bypass_avoids_host(_shell_source())
    extra = ShellInfo(name="git_bash", path="/opt/chrys-extra-bash", args=["-c"], version="5.2")
    runtime = dataclasses.replace(runtime, platform=dataclasses.replace(runtime.platform, extra_shells=(extra,)))
    settings = Settings(sandbox_enabled=True)
    counts = _spy_host(monkeypatch)
    calls: list[_ShellCall] = []
    _patch_run_shell(monkeypatch, calls, SandboxExec(0, "from-sandbox", ""))
    tools = packs_mod._create_shell(
        packs_mod.ToolBuild(
            tools=ToolsConfig(builtins=["shell"]),
            agent_name="test",
            runtime=runtime,
            settings=settings,
        )
    )
    instances = _shell_instances(tools)
    primary = next(instance for instance in instances if instance.shell is runtime.platform.shell)
    chosen = next(instance for instance in instances if instance.shell is extra)
    assert primary._settings is settings
    assert chosen._settings is settings

    for instance in (primary, chosen):
        result = await instance.execute("echo hello", reason="test")
        assert result.endswith("[exit_code: 0]")
        assert "from-sandbox" in result

    assert len(calls) == 2
    assert all(call.settings is settings for call in calls)
    for instance, call in zip((primary, chosen), calls, strict=True):
        assert call.argv == _pipe_argv(instance.shell, "echo hello")
    _assert_host_idle(counts)


# ───────────────────────── description matches what sandbox mode does ─────────────────────────


def test_sandboxed_shell_description_does_not_promise_background(runtime: SessionEnvironment, tmp_path: Path) -> None:
    tools = _packed_command_tools(runtime, Settings(sandbox_enabled=True), BackgroundTasks(tmp_path))

    assert tools
    for tool in tools:
        assert "moves to the background" not in tool.description
        assert "stop file" not in tool.description


def test_host_shell_description_promises_background_when_tasks_are_passed(
    runtime: SessionEnvironment, tmp_path: Path
) -> None:
    tools = _packed_command_tools(runtime, Settings(sandbox_enabled=False), BackgroundTasks(tmp_path))

    assert tools
    for tool in tools:
        assert "moves to the background" in tool.description
        assert "stop file" in tool.description


# ───────────────────────── invalid sandbox.enabled does not run on the host ─────────────────────────


@pytest.mark.parametrize("source", ["env", "user_yaml"])
async def test_invalid_sandbox_enabled_does_not_run_on_the_host(
    runtime: SessionEnvironment, source: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _load_invalid_sandbox(monkeypatch, source)

    await _assert_command_is_not_run(ShellTools(runtime, settings=settings), monkeypatch)


async def test_replacing_other_fields_keeps_an_invalid_sandbox_rejection(
    runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = dataclasses.replace(_load_invalid_sandbox(monkeypatch, "env"), model_profile="still-invalid")

    assert settings.sandbox_enabled is False
    assert settings.sandbox_enabled_was_rejected() is True
    await _assert_command_is_not_run(ShellTools(runtime, settings=settings), monkeypatch)


async def test_valid_sandbox_enabled_false_still_uses_the_host(
    runtime: SessionEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHRYS_SANDBOX_ENABLED", "false")
    freeze_process_env()
    settings = load_settings().settings
    assert settings.sandbox_enabled is False
    assert settings.sandbox_enabled_was_rejected() is False
    assert dataclasses.replace(settings, model_profile="kept-off").sandbox_enabled_was_rejected() is False
    host_calls: list[str] = []
    sandbox_calls: list[_ShellCall] = []
    _stub_host_executor(monkeypatch, host_calls)
    _patch_run_shell(monkeypatch, sandbox_calls, SandboxExec(0, "sandbox-out", ""))

    result = await ShellTools(runtime, settings=settings).execute("echo hello", reason="test")

    assert host_calls == ["pipe" if sys.platform == "win32" else "pty"]
    assert sandbox_calls == []
    assert "host-executor-output" in result
    assert "sandbox-out" not in result
