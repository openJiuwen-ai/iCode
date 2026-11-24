# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bind a certified call to one isolated launch of its configured shell."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.foundation.platform.runtime_paths import build_shell_subprocess_env

if TYPE_CHECKING:
    from chrys.foundation.platform import ShellInfo

_BASH_DIALECTS = frozenset({"bash", "git_bash"})
type _Identity = tuple[int, int]
type _Locations = tuple[tuple[str, tuple[_Identity, ...]], ...]


class ApprovalInvalidatedError(PermissionError):
    """The approved shell call no longer matches the intended launch."""


def _identity(info: os.stat_result) -> _Identity:
    if not info.st_ino:
        raise OSError("Filesystem identity unavailable")
    return info.st_dev, info.st_ino


def _path_chain(path: Path, workspace: set[_Identity]) -> tuple[_Identity, ...]:
    """Check each visited entry, including each hop in a link's target."""
    if not path.is_absolute():
        raise ValueError("Relative launch path")
    current = Path(path.anchor)
    remaining = deque(path.parts[1:])
    identities: list[_Identity] = []
    links = 0
    while True:
        info = current.lstat()
        identity = _identity(info)
        if identity in workspace:
            raise ValueError("Workspace-controlled launch path")
        identities.append(identity)
        is_link = stat.S_ISLNK(info.st_mode)
        if sys.platform == "win32" and info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            is_link = is_link or info.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
            if not is_link:
                raise ValueError("Unsupported launch path reparse point")
        if is_link:
            links += 1
            if links > 40:
                raise ValueError("Too many launch path links")
            target = current.readlink()
            if target.anchor and not target.is_absolute():
                raise ValueError("Ambiguous launch path link target")
            current = Path(target.anchor) if target.is_absolute() else current.parent
            remaining.extendleft(reversed(target.parts[1:] if target.is_absolute() else target.parts))
        elif remaining:
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError("Invalid launch path ancestor")
            part = remaining.popleft()
            if part == ".." and sys.platform == "win32":
                raise ValueError("Ambiguous Windows parent path")
            current = current.parent if part == ".." else current / part
        else:
            if _identity(path.stat()) != identity:
                raise ValueError("Launch path resolution changed")
            return tuple(identities)


def _trusted_locations(locations: tuple[str, ...], roots: tuple[str, ...]) -> _Locations | None:
    """Record trusted path identities or defer when they cannot be certified."""
    try:
        workspace = {_identity(Path(root).stat()) for root in roots}
        return tuple((location, _path_chain(Path(location), workspace)) for location in locations)
    except OSError, ValueError, RuntimeError:
        return None


def _trusted_lookup(env: dict[str, str], executables: tuple[str, ...], roots: tuple[str, ...]) -> _Locations | None:
    directories = env.get("PATH", "").split(os.pathsep)
    try:
        candidates = []
        for word in dict.fromkeys(executables):
            if "/" in word or "\\" in word:
                continue
            names = (word, word + ".exe") if os.name == "nt" else (word,)
            for directory in directories:
                for name in names:
                    candidate = Path(directory) / name
                    try:
                        candidate.lstat()
                    except FileNotFoundError:
                        continue
                    candidates.append(str(candidate))
        return _trusted_locations(tuple(candidates), roots)
    except OSError, ValueError:
        return None


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
    location_identities: _Locations = ()
    lookup_identities: _Locations = ()
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
            or _trusted_locations(self.locations, self.roots) != self.location_identities
            or _trusted_lookup(self.environment, self.executables, self.roots) != self.lookup_identities
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
    location_identities = _trusted_locations(locations, roots)
    if location_identities is None:
        return None
    lookup_identities = _trusted_lookup(env, executables, roots)
    if lookup_identities is None:
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
        location_identities=location_identities,
        lookup_identities=lookup_identities,
    )


def supports_isolated_launch(shell: ShellInfo) -> bool:
    return shell.name in _BASH_DIALECTS and shell.args == ["-c"]


isolated_shell_call: ContextVar[ApprovedShell | None] = ContextVar("prejudge_isolated_shell_call", default=None)
