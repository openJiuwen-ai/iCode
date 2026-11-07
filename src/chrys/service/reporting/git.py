# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The one way reporting talks to ``git``.

Read-only probes only, always bounded by a wall-clock timeout, always with a
detached stdin: under ACP that descriptor carries the JSON-RPC stream and a
child that reads it would swallow protocol bytes. Callers run these from a
worker thread, never the event loop.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Final

from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs

logger = logging.getLogger(__name__)

GIT_TIMEOUT_SECONDS: Final = 10.0
"""Budget of a single git probe."""


@dataclass(frozen=True, slots=True)
class GitResult:
    """A finished git invocation."""

    returncode: int
    stdout: bytes

    @property
    def ok(self) -> bool:
        """Whether git exited successfully."""
        return self.returncode == 0

    def text(self) -> str:
        """The standard output decoded for path/URL parsing."""
        return self.stdout.decode("utf-8", "surrogateescape").strip()


def git_available() -> bool:
    """Whether a ``git`` executable exists on this machine."""
    return shutil.which("git") is not None


def run_git(args: list[str], cwd: str, *, timeout: float = GIT_TIMEOUT_SECONDS) -> GitResult | None:
    """Run one read-only git command in *cwd*; ``None`` when it could not run."""
    executable = shutil.which("git")
    if executable is None or timeout <= 0:
        return None
    environment = os.environ.copy()
    environment["GIT_LITERAL_PATHSPECS"] = "1"
    # Optional locks would let a read-only probe rewrite the index of a repo
    # another process may be working in.
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        completed = subprocess.run(  # noqa: S603
            [executable, *args],
            cwd=cwd,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
            **windows_hidden_subprocess_kwargs(),
        )
    except OSError, subprocess.TimeoutExpired:
        logger.debug("git %s failed in %s", args[0] if args else "", cwd, exc_info=True)
        return None
    return GitResult(returncode=completed.returncode, stdout=completed.stdout)


def nul_fields(raw: bytes) -> list[str]:
    """Split a ``-z`` git stream into decoded non-empty fields."""
    return [field for field in raw.decode("utf-8", "surrogateescape").split("\0") if field]
