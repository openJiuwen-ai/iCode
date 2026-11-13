# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Approval-safe shell commands must execute the validated argv, not a shell string."""

from __future__ import annotations

import pytest

import chrys.service.tools.builtins.shell as shell_module
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.platform import ShellInfo
from chrys.foundation.tool_result_metadata import SHELL_EXIT_CODE_METADATA_KEY
from chrys.service.tools.builtins.shell import ShellTools, shell_result_metadata


def _shell() -> ShellTools:
    shell = ShellTools(SessionEnvironment.capture())
    # Force the POSIX classifier even on Windows; the spawn is mocked below.
    shell._shell = ShellInfo(name="bash", path="bash", args=["-c"])
    return shell


async def _capture_spawn(monkeypatch: pytest.MonkeyPatch, calls: list[tuple[str, tuple[str, ...] | None, bool]]):
    async def fake_spawn(command, shell, cwd, *, direct_argv=None, sanitized=False):
        calls.append((command, direct_argv, sanitized))
        raise RuntimeError("stop before spawning")

    monkeypatch.setattr(shell_module, "_spawn_pty", fake_spawn)
    monkeypatch.setattr(shell_module, "_spawn_pipe", fake_spawn)


async def test_safe_external_command_passes_validated_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[str, ...] | None, bool]] = []
    await _capture_spawn(monkeypatch, calls)

    await _shell().execute("ls -la src", reason="test")

    assert calls == [("ls -la src", ("ls", "-la", "src"), True)]


async def test_safe_builtin_uses_the_shell_with_sanitized_env(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, tuple[str, ...] | None, bool]] = []
    await _capture_spawn(monkeypatch, calls)

    await _shell().execute("echo hello", reason="test")

    assert calls == [("echo hello", None, True)]


async def test_quoted_argument_stays_one_argv_element(monkeypatch: pytest.MonkeyPatch) -> None:
    """The direct path must pass a quoted path as one argument, not re-split it."""
    calls: list[tuple[str, tuple[str, ...] | None, bool]] = []
    await _capture_spawn(monkeypatch, calls)

    await _shell().execute("cat 'file with space'", reason="test")

    assert calls == [("cat 'file with space'", ("cat", "file with space"), True)]


async def test_missing_direct_executable_reports_command_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing auto-approved binary is not a missing shell; keep shell exit 127 semantics."""

    async def missing_spawn(command, shell, cwd, *, direct_argv=None, sanitized=False):
        raise FileNotFoundError(direct_argv[0] if direct_argv else command)

    monkeypatch.setattr(shell_module, "_spawn_pty", missing_spawn)
    monkeypatch.setattr(shell_module, "_spawn_pipe", missing_spawn)
    metadata: dict[str, object] = {}
    token = shell_result_metadata.set(metadata)
    try:
        result = await _shell().execute("md5sum README.md", reason="test")
    finally:
        shell_result_metadata.reset(token)

    assert "command not found — md5sum" in result
    assert "[exit_code: 127]" in result
    assert metadata[SHELL_EXIT_CODE_METADATA_KEY] == 127
    assert "shell not found" not in result


@pytest.mark.parametrize(
    "command",
    [
        "rg --pre ./x.sh -e y .",
        "rg --pre$IFS./x.sh -e y .",
        "cat .env*",
        "cat > out",
    ],
)
async def test_unexpandable_or_unapproved_command_keeps_shell_path(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    calls: list[tuple[str, tuple[str, ...] | None, bool]] = []
    await _capture_spawn(monkeypatch, calls)

    await _shell().execute(command, reason="test")

    assert calls == [(command, None, False)]


def test_approval_safe_env_removes_startup_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", "/tmp/rg.conf")
    monkeypatch.setenv("BASH_ENV", "/tmp/bash_env")
    monkeypatch.setenv("PYTHONPATH", "/tmp/python")
    monkeypatch.setenv("BASH_FUNC_rg%%", "() { echo hacked; }")

    env = shell_module._approval_safe_env()

    assert "RIPGREP_CONFIG_PATH" not in env
    assert "BASH_ENV" not in env
    assert "PYTHONPATH" not in env
    assert not any(name.startswith("BASH_FUNC_") for name in env)
