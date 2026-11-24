# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Require the real grammar wheels for parser-dependent tests in CI."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.platform import ShellInfo
from chrys.kernel.tools import FunctionTool
from chrys.service.tools.builtins.shell import ShellTools


def bound_bash_tool(monkeypatch: pytest.MonkeyPatch) -> FunctionTool:
    """A configured Bash tool for middleware tests that stop before launch."""
    runtime = SessionEnvironment.capture()
    monkeypatch.setenv("PATH", str(Path(runtime.platform.shell.path).parent))
    return ShellTools(runtime, shell=ShellInfo("bash", runtime.platform.shell.path, ["-c"])).tools()[0]


def require_grammars(*modules: str) -> None:
    for name in modules:
        if os.environ.get("CI"):
            importlib.import_module(name)
        else:
            pytest.importorskip(name)
