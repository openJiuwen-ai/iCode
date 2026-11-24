# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ``reminders.runtime_env``: the runtime hint and its Python execution paths."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.models.workspace import WorkingDir, Workspace, WorkspaceDeparture
from chrys.foundation.platform import PlatformInfo, ShellInfo
from chrys.service.agent_middleware.reminders import runtime_env
from chrys.service.agent_middleware.reminders.runtime_env import RuntimeEnvSource


def _exe(path: Path) -> Path:
    """Return *path* with a platform-appropriate executable suffix.

    On Windows, ``shutil.which`` only matches files whose suffix is in
    ``PATHEXT`` (``.exe``/``.bat``/…), so a stub file created as bare
    ``uv`` or ``python3`` is invisible to the production discovery code.
    The production path itself is fine — real installs ship ``uv.exe``
    and friends — so the suffix only needs to be added inside the test
    scaffolding when constructing the stub paths.
    """
    if sys.platform == "win32" and not path.suffix:
        return path.with_name(path.name + ".exe")
    return path


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    path.chmod(0o755)


def _linux_runtime(tmp_path: Path) -> SessionEnvironment:
    platform = PlatformInfo(
        os_name="linux",
        os_version="test",
        arch="amd64",
        shell=ShellInfo(name="bash", path="/bin/bash", args=["-c"]),
        config_dir=tmp_path,
        data_dir=tmp_path,
    )
    return SessionEnvironment(cwd=str(tmp_path), platform=platform)


def _patch_executable_lookup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    runtime_python: Path,
    which: dict[str, str],
) -> None:
    monkeypatch.setattr(runtime_env.sys, "executable", str(runtime_python))
    if sys.platform == "win32":
        # Pin PATHEXT so shutil.which returns ".exe" (lowercase) — matching
        # the case _exe() writes on disk. Without this the GHA runner's
        # default ".EXE"-cased PATHEXT would make shutil.which return
        # uppercase-suffixed paths and our string assertions would fail.
        monkeypatch.setenv("PATHEXT", ".exe")
    path_dirs = [str(runtime_python.parent)]
    for raw_path in which.values():
        executable = Path(raw_path)
        _make_executable(executable)
        path_dirs.append(str(executable.parent))
    monkeypatch.setenv("PATH", os.pathsep.join(dict.fromkeys(path_dirs)))


def _runtime_python_line(path: Path) -> str:
    version = runtime_env.sys.version_info
    return f"    - your runtime Python ({version.major}.{version.minor}.{version.micro}): {path}"


def _hint(runtime: SessionEnvironment, *, shell_tool_enabled: bool) -> str:
    hint = RuntimeEnvSource(runtime, shell_tool_enabled=shell_tool_enabled).snapshot()
    assert hint is not None
    return hint


class TestPythonExecutionPathHints:
    def test_runtime_hint_omits_python_execution_paths_without_shell_tool(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime_python = tmp_path / "runtime-python"
        system_uv = tmp_path / "system" / "bin" / "uv"
        _patch_executable_lookup(monkeypatch, runtime_python=runtime_python, which={"uv": str(system_uv)})

        hint = _hint(_linux_runtime(tmp_path), shell_tool_enabled=False)

        assert "Python execution paths" not in hint
        assert "system uv" not in hint

    def test_runtime_hint_lists_system_and_runtime_paths_when_shell_tool_enabled(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime_python = tmp_path / "runtime-python"
        system_uv = _exe(tmp_path / "system" / "bin" / "uv")
        system_python = _exe(tmp_path / "system" / "bin" / "python3")
        _patch_executable_lookup(
            monkeypatch,
            runtime_python=runtime_python,
            which={"uv": str(system_uv), "python3": str(system_python)},
        )

        hint = _hint(_linux_runtime(tmp_path), shell_tool_enabled=True)

        assert "Python execution paths (for Python scripts or Python commands)" in hint
        assert "shell tool is enabled" not in hint
        assert f"    - system uv: {system_uv}" in hint
        assert f"    - system Python: {system_python}" in hint
        assert "system Python (3." not in hint
        assert _runtime_python_line(runtime_python) in hint
        assert "Consider uv or uvx for ad-hoc Python scripts/tools" in hint
        assert "avoid modifying user system or project Python environments" in hint
        assert "fallback for Python scripts/commands" in hint
        assert "when no suitable system uv or Python executable is available" in hint
        assert "Avoid broad Python-process termination commands" in hint
        assert "Get-Process python | Stop-Process" in hint
        assert "they may terminate your own runtime" in hint
        assert "Target specific PIDs or child processes you started" in hint
        assert "your runtime uv" not in hint
        assert "runtime uv/Python" not in hint
        assert "Preferred" not in hint

    @pytest.mark.parametrize("alias_name", ["chrys-runtime", "chrys-runtime.exe", "chrys-runtimew.exe"])
    def test_runtime_hint_omits_process_kill_warning_when_runtime_python_uses_chrys_alias(
        self,
        alias_name: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime_python = tmp_path / "python" / "bin" / alias_name
        system_uv = _exe(tmp_path / "system" / "bin" / "uv")
        _patch_executable_lookup(monkeypatch, runtime_python=runtime_python, which={"uv": str(system_uv)})

        hint = _hint(_linux_runtime(tmp_path), shell_tool_enabled=True)

        assert _runtime_python_line(runtime_python) in hint
        assert "Avoid broad Python-process termination commands" not in hint
        assert "Get-Process python | Stop-Process" not in hint

    def test_runtime_hint_lists_system_python_when_system_uv_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime_python = tmp_path / "runtime-python"
        system_python = _exe(tmp_path / "system" / "bin" / "python")
        _patch_executable_lookup(monkeypatch, runtime_python=runtime_python, which={"python": str(system_python)})

        hint = _hint(_linux_runtime(tmp_path), shell_tool_enabled=True)

        assert f"    - system Python: {system_python}" in hint
        assert "    - system uv:" not in hint
        assert _runtime_python_line(runtime_python) in hint
        assert "consider uv or uvx" not in hint
        assert "your runtime uv" not in hint

    def test_runtime_hint_does_not_treat_non_windows_py_as_system_python(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime_python = tmp_path / "runtime-python"
        py_command = tmp_path / "system" / "bin" / "py"
        _patch_executable_lookup(monkeypatch, runtime_python=runtime_python, which={"py": str(py_command)})
        monkeypatch.setattr(runtime_env, "_python_executable_names", lambda: ("python3", "python"))

        hint = _hint(_linux_runtime(tmp_path), shell_tool_enabled=True)

        assert "    - system Python:" not in hint
        assert _runtime_python_line(runtime_python) in hint

    def test_runtime_hint_allows_windows_py_launcher_as_system_python(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime_python = tmp_path / "runtime-python"
        py_launcher = _exe(tmp_path / "system" / "bin" / "py")
        _patch_executable_lookup(monkeypatch, runtime_python=runtime_python, which={"py": str(py_launcher)})
        monkeypatch.setattr(runtime_env, "_python_executable_names", lambda: ("python3", "python", "py"))

        hint = _hint(_linux_runtime(tmp_path), shell_tool_enabled=True)

        assert f"    - system Python: {py_launcher}" in hint
        assert _runtime_python_line(runtime_python) in hint

    def test_runtime_hint_skips_active_runtime_dir_when_resolving_system_python(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime_python = _exe(tmp_path / "venv" / "bin" / "python3")
        _make_executable(runtime_python)
        system_python = _exe(tmp_path / "system" / "bin" / "python3")
        _patch_executable_lookup(monkeypatch, runtime_python=runtime_python, which={"python3": str(system_python)})

        hint = _hint(_linux_runtime(tmp_path), shell_tool_enabled=True)

        assert f"    - system Python: {system_python}" in hint
        assert _runtime_python_line(runtime_python) in hint
        assert f"system Python: {runtime_python}" not in hint

    def test_runtime_hint_does_not_list_colocated_runtime_uv(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime_bin = tmp_path / "runtime" / "bin"
        runtime_bin.mkdir(parents=True)
        runtime_python = _exe(runtime_bin / "python")
        runtime_python.write_text("", encoding="utf-8")
        runtime_uv = _exe(runtime_bin / "uv")
        runtime_uv.write_text("", encoding="utf-8")
        _patch_executable_lookup(monkeypatch, runtime_python=runtime_python, which={})

        hint = _hint(_linux_runtime(tmp_path), shell_tool_enabled=True)

        assert _runtime_python_line(runtime_python) in hint
        assert f"    - your runtime uv: {runtime_uv}" not in hint

    def test_runtime_hint_does_not_list_windows_style_runtime_uv(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime_scripts = tmp_path / "runtime" / "Scripts"
        runtime_scripts.mkdir(parents=True)
        runtime_python = runtime_scripts / "python.exe"
        runtime_python.write_text("", encoding="utf-8")
        runtime_uv = runtime_scripts / "uv.exe"
        runtime_uv.write_text("", encoding="utf-8")
        _patch_executable_lookup(monkeypatch, runtime_python=runtime_python, which={})

        hint = _hint(_linux_runtime(tmp_path), shell_tool_enabled=True)

        assert _runtime_python_line(runtime_python) in hint
        assert f"    - your runtime uv: {runtime_uv}" not in hint


def _main_hint(runtime: SessionEnvironment) -> str:
    hint = RuntimeEnvSource(runtime, shell_tool_enabled=False, workspace_section=True).snapshot()
    assert hint is not None
    return hint


def _workspace_runtime(tmp_path: Path, name: str = "Selected") -> SessionEnvironment:
    primary = tmp_path / "primary"
    primary.mkdir(exist_ok=True)
    runtime = SessionEnvironment.capture(
        "project-session",
        Workspace(str(tmp_path), [WorkingDir(str(primary))], id="p", name=name),
        workspace_index=("Workspace folders:", f"- {primary} (primary)"),
    )
    return replace(runtime, platform=_linux_runtime(tmp_path).platform)


def test_a_hand_written_workdir_name_cannot_add_lines_to_the_reminder(tmp_path: Path) -> None:
    """Control characters in a name written into the project list by hand reach the model as spaces."""
    member = _workspace_runtime(tmp_path, name="Two\nlines\x07")
    main = RuntimeEnvSource(member, shell_tool_enabled=False, workspace_section=True).snapshot()
    sub_agent = RuntimeEnvSource(member, shell_tool_enabled=False).snapshot()
    left = SessionEnvironment.capture(
        "left-session", Workspace.from_cwd(str(tmp_path)), departed_workspace=WorkspaceDeparture("Old\r\nname")
    )
    departed = RuntimeEnvSource(left, shell_tool_enabled=False, workspace_section=True).snapshot()
    assert main is not None and sub_agent is not None and departed is not None
    assert "  Workspace: Two lines " in main.splitlines()
    assert f"    - {tmp_path / 'primary'}" in sub_agent.splitlines()
    assert '  Workspace: Unspecified (left workspace "Old  name")' in departed.splitlines()
