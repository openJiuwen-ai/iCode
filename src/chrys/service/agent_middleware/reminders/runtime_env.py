# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The ``runtime`` catalog: where the agent runs and, with a shell tool, which Python to run.

Snapshotted when a turn starts fresh; ``system_reminder`` decides when it is
sent.  Python-execution-path discovery searches ``PATH`` on every snapshot.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from chrys.foundation.models.workspace import control_chars_as_spaces
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.platform.runtime_paths import normalize_path as _normalise_path
from chrys.foundation.platform.runtime_paths import same_path, which_excluding_runtime
from chrys.foundation.text.model_json import model_path

if TYPE_CHECKING:
    from chrys.foundation.models.session_env import SessionEnvironment

_PYTHON_EXECUTABLE_NAMES = ("python3", "python")
_WINDOWS_PYTHON_EXECUTABLE_NAMES = ("python3", "python", "py")
_PROCESS_SAFE_RUNTIME_PYTHON_BASENAMES = frozenset({"chrys-runtime", "chrys-runtime.exe", "chrys-runtimew.exe"})
_WORKSPACE_PREFIX = "  Workspace: "
_DEPARTURE_PREFIX = 'Unspecified (left workspace "'


@dataclass(frozen=True)
class _PythonExecutionPath:
    """A discovered path relevant to Python execution via a shell tool."""

    label: str
    path: str
    version: str = ""


class RuntimeEnvSource:
    """The runtime environment one middleware reports: cwd, OS, shells, workspace, working directories."""

    name = "runtime"
    """The catalog's record name."""
    withdrawn = None
    """None: when no runtime hint is offered, the last one in view stays standing."""

    def __init__(
        self,
        runtime: SessionEnvironment | None,
        *,
        shell_tool_enabled: bool,
        workspace_section: bool = False,
    ) -> None:
        self._runtime = runtime
        self._shell_tool_enabled = shell_tool_enabled
        # Only the main agent reads its workspace here; a sub-agent gets no workspace
        # context, just the workspace folders it may work in, listed as working directories.
        self._workspace_section = workspace_section

    def snapshot(self) -> str | None:
        """The runtime hint, or None without a session environment; callers cache it at turn start."""
        rt = self._runtime
        if rt is None:
            return None

        shell = rt.platform.shell
        shell_line = f"  Shell: {shell.name} {shell.version} ({shell.path})".rstrip()
        lines = [
            "[Runtime Environment]",
            f"  Working directory: {model_path(rt.cwd)}",
            f"  OS: {rt.platform.os_name} ({rt.platform.arch})",
            shell_line,
        ]
        lines.extend(
            f"  Shell: {extra.name} {extra.version} ({extra.path})".rstrip() for extra in rt.platform.extra_shells
        )
        lines.extend(self._workspace_lines(rt))
        if self._shell_tool_enabled:
            lines.extend(_format_python_execution_paths_hint())
        working_dirs = () if self._workspace_section and rt.workspace_name else rt.working_dirs
        if working_dirs:
            lines.append("  Working directories:")
            for wd in working_dirs:
                marker = " (primary)" if wd.is_primary else ""
                label = f" [{control_chars_as_spaces(wd.label)}]" if wd.label else ""
                lines.append(f"    - {model_path(wd.path)}{label}{marker}")
        return surrogate_safe_text("\n".join(lines))

    def with_departure(self, hint: str | None, visible_runtime: str | None) -> str | None:
        """Name the workspace just left only when the model saw that workspace."""
        rt = self._runtime
        if not hint or not visible_runtime or not self._workspace_section or rt is None or rt.workspace_name:
            return hint
        lines = hint.split("\n")
        if any(line.startswith(_WORKSPACE_PREFIX) for line in lines):
            return hint
        visible_lines = visible_runtime.split("\n")
        workspace_line = next((line for line in visible_lines if line.startswith(_WORKSPACE_PREFIX)), "")
        name = workspace_line.removeprefix(_WORKSPACE_PREFIX)
        if not name:
            return hint
        if "  Workspace folders:" not in visible_lines and _is_departure_line(workspace_line):
            # Already delivered for this runtime: keep that exact catalog, or
            # send the new directory without the stale departure notice.
            without_departure = [line for line in visible_lines if line != workspace_line]
            return visible_runtime if without_departure == lines else hint
        insert_at = max(
            (index + 1 for index, line in enumerate(lines) if line.startswith("  Shell: ")),
            default=1,
        )
        lines.insert(insert_at, f'{_WORKSPACE_PREFIX}{_DEPARTURE_PREFIX}{name}")')
        return "\n".join(lines)

    def _workspace_lines(self, rt: SessionEnvironment) -> list[str]:
        """Workspace membership and its folder index once."""
        if not self._workspace_section:
            return []
        if rt.workspace_name:
            return [f"  Workspace: {rt.workspace_name}", *(f"  {line}" for line in rt.workspace_index)]
        return []


def _is_departure_line(line: str) -> bool:
    prefix = f"{_WORKSPACE_PREFIX}{_DEPARTURE_PREFIX}"
    return line.startswith(prefix) and line.endswith('")') and len(line) > len(prefix) + 2


def _format_python_execution_paths_hint() -> list[str]:
    """Format Python execution paths for agents that can use a shell tool."""
    paths = _python_execution_paths()
    if not paths:
        return []

    has_runtime_python = any(path.label == "your runtime Python" for path in paths)
    has_system_uv = any(path.label == "system uv" for path in paths)
    lines = ["  Python execution paths (for Python scripts or Python commands):"]
    lines.extend(_format_python_execution_path_line(path) for path in paths)
    if has_system_uv:
        lines.append(
            "  Consider uv or uvx for ad-hoc Python scripts/tools with temporary dependencies to avoid modifying "
            "user system or project Python environments, unless user/project/skill instructions say otherwise."
        )
    if has_runtime_python:
        lines.append(
            "  Your runtime Python path is a fallback for Python scripts/commands when no suitable system uv or "
            "Python executable is available."
        )
        if not _runtime_python_uses_process_safe_alias():
            lines.append(
                "  Avoid broad Python-process termination commands (for example Get-Process python | Stop-Process, "
                "pkill python, or killall python); they may terminate your own runtime. Target specific PIDs or child "
                "processes you started."
            )
    return lines


def _runtime_python_uses_process_safe_alias() -> bool:
    """Return true when the runtime executable basename is Chrys' PyApp alias."""
    basename = sys.executable.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return basename in _PROCESS_SAFE_RUNTIME_PYTHON_BASENAMES


def _python_execution_paths() -> list[_PythonExecutionPath]:
    """Return available Python execution paths in stable discovery order."""
    runtime_python = _normalise_path(sys.executable)

    paths: list[_PythonExecutionPath] = []
    system_uv = which_excluding_runtime("uv", frozen_only=False, fallback_to_runtime=False)
    if system_uv:
        _append_python_execution_path(paths, _python_execution_path("system uv", system_uv))

    system_python = _first_executable(
        _python_executable_names(),
        exclude_paths=(runtime_python,),
    )
    if system_python:
        _append_python_execution_path(
            paths,
            _python_execution_path("system Python", system_python),
        )

    if runtime_python:
        _append_python_execution_path(
            paths,
            _python_execution_path("your runtime Python", runtime_python, version=_runtime_python_version()),
        )

    return paths


def _format_python_execution_path_line(path: _PythonExecutionPath) -> str:
    version = f" ({path.version})" if path.version else ""
    return f"    - {path.label}{version}: {path.path}"


def _append_python_execution_path(paths: list[_PythonExecutionPath], new_path: _PythonExecutionPath) -> None:
    """Append *new_path*, merging labels when the same executable is already listed."""
    for index, existing in enumerate(paths):
        if same_path(existing.path, new_path.path):
            labels = [*existing.label.split(" / "), new_path.label]
            version = existing.version or new_path.version
            paths[index] = _PythonExecutionPath(
                label=" / ".join(dict.fromkeys(labels)),
                path=existing.path,
                version=version,
            )
            return
    paths.append(new_path)


def _python_execution_path(label: str, path: str, *, version: str = "") -> _PythonExecutionPath:
    return _PythonExecutionPath(label=label, path=path, version=version)


def _runtime_python_version() -> str:
    version = sys.version_info
    return f"{version.major}.{version.minor}.{version.micro}"


def _python_executable_names() -> tuple[str, ...]:
    """Return executable names that should be treated as Python on this platform."""
    if sys.platform == "win32":
        return _WINDOWS_PYTHON_EXECUTABLE_NAMES
    return _PYTHON_EXECUTABLE_NAMES


def _first_executable(
    names: tuple[str, ...],
    *,
    exclude_paths: tuple[str, ...] = (),
) -> str:
    """Return the first executable found on PATH."""
    for name in names:
        path = which_excluding_runtime(
            name,
            frozen_only=False,
            fallback_to_runtime=False,
            exclude_paths=exclude_paths,
        )
        if path:
            return path
    return ""
