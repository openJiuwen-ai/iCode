# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bind a certified call to one isolated launch of its configured shell."""

from __future__ import annotations

import asyncio
import os
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.foundation.platform.runtime_paths import build_shell_subprocess_env

if TYPE_CHECKING:
    from chrys.foundation.platform import ShellInfo

_BASH_DIALECTS = frozenset({"bash", "git_bash"})


class ApprovalInvalidatedError(PermissionError):
    """The approved shell call no longer matches the intended launch."""


def _trusted_locations(locations: tuple[str, ...], roots: tuple[str, ...]) -> bool:
    """Defer relative lookup and paths which the workspace can modify."""
    try:
        workspace = [Path(root).resolve() for root in roots]
        return all(
            Path(location).is_absolute()
            and not any(Path(location).resolve().is_relative_to(root) for root in workspace)
            for location in locations
        )
    except OSError, ValueError, RuntimeError:
        return False


def _trusted_lookup(env: dict[str, str], executables: tuple[str, ...], roots: tuple[str, ...]) -> bool:
    if not roots:
        return True
    directories = env.get("PATH", "").split(os.pathsep)
    try:
        links = []
        for word in set(executables):
            if "/" in word or "\\" in word:
                continue
            names = (word, word + ".exe") if os.name == "nt" else (word,)
            for directory in directories:
                for name in names:
                    candidate = Path(directory) / name
                    if candidate.is_symlink():
                        links.append(str(candidate))
        return _trusted_locations(tuple(links), roots)
    except OSError, ValueError:
        return False


@dataclass(slots=True)
class ApprovedShell:
    dialect: str
    source: str
    owner: asyncio.Task[Any]
    environment: dict[str, str] = field(default_factory=dict)
    locations: tuple[str, ...] = ()
    roots: tuple[str, ...] = ()
    executables: tuple[str, ...] = ()
    shell_path: str = ""
    shell_args: tuple[str, ...] = ()
    active: bool = True
    claimed: bool = False
    issued: bool = False

    def _matches(self, shell: ShellInfo, source: str) -> bool:
        return (
            self.active
            and asyncio.current_task() is self.owner
            and (self.dialect, self.source) == (shell.name, source)
            and (self.shell_path, self.shell_args) == (shell.path, tuple(shell.args))
        )

    def claim(self, shell: ShellInfo, source: str) -> bool:
        if self.claimed or not self._matches(shell, source):
            return False
        self.claimed = True
        return True

    def require_launch(self, shell: ShellInfo, source: str) -> None:
        if (
            not self.claimed
            or self.issued
            or not self._matches(shell, source)
            or not _trusted_locations(self.locations, self.roots)
            or not _trusted_lookup(self.environment, self.executables, self.roots)
        ):
            raise ApprovalInvalidatedError("The structured approval no longer binds this shell launch.")
        self.issued = True


def prepare_shell(
    shell: ShellInfo, source: str, executables: tuple[str, ...], roots: tuple[str, ...]
) -> ApprovedShell | None:
    """Compute shared Shadow and Enforce eligibility without running a shell."""
    owner = asyncio.current_task()
    if owner is None or not supports_isolated_launch(shell):
        return None
    env = build_shell_subprocess_env()
    locations = (
        shell.path,
        *env.get("PATH", "").split(os.pathsep),
        *(word for word in executables if "/" in word or "\\" in word),
    )
    if not _trusted_locations(locations, roots) or not _trusted_lookup(env, executables, roots):
        return None
    for key in tuple(env):
        if key in {"BASH_ENV", "ENV"} or key.startswith("BASH_FUNC_"):
            del env[key]
    return ApprovedShell(
        shell.name,
        source,
        owner,
        env,
        locations,
        roots,
        executables,
        shell_path=shell.path,
        shell_args=tuple(shell.args),
    )


def supports_isolated_launch(shell: ShellInfo) -> bool:
    return shell.name in _BASH_DIALECTS and shell.args == ["-c"]


isolated_shell_call: ContextVar[ApprovedShell | None] = ContextVar("prejudge_isolated_shell_call", default=None)
