# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Require the real grammar wheels for parser-dependent tests in CI."""

from __future__ import annotations

import asyncio
import importlib
import os
from pathlib import Path

import pytest

from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.platform import ShellInfo
from chrys.kernel.tools import FunctionTool
from chrys.service.tools.builtins.shell import ShellTools


def bound_bash_tool() -> FunctionTool:
    """A configured Bash tool for middleware tests that stop before launch."""
    runtime = SessionEnvironment.capture()
    return ShellTools(runtime, shell=ShellInfo("bash", runtime.platform.shell.path, ["-c"])).tools()[0]


async def set_fixture_search_path(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Give launch-eligibility tests a realistic search path independent of the host."""
    first, second, inert = (root / name for name in ("fixture-system-bin", "fixture-extra-bin", "fixture-file"))
    await asyncio.to_thread(first.mkdir)
    await asyncio.to_thread(second.mkdir)
    await asyncio.to_thread(inert.touch)
    monkeypatch.setenv("PATH", os.pathsep.join(map(str, (first, root / "fixture-missing", second, inert))))


def require_grammars(*modules: str) -> None:
    for name in modules:
        if os.environ.get("CI"):
            importlib.import_module(name)
        else:
            pytest.importorskip(name)
