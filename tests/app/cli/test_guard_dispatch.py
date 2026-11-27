# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``icode`` hands the sandbox to ``chrys.foundation.guard`` before anything else runs."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chrys.app.cli import app, run
from chrys.foundation import guard


@pytest.fixture(autouse=True)
def _restore_guard() -> Any:
    installed = guard.current()
    yield
    guard.set_current(installed)


def test_a_command_that_runs_an_agent_starts_the_guard_first(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    started: list[list[str]] = []

    def install(argv: list[str], decision: guard.Decision, config_dir: Path) -> guard.Guard:
        started.append(argv)
        return guard._NoGuard(guard.GuardStatus(state="active", requested=True))

    monkeypatch.setattr(guard, "_load_plugin", lambda: SimpleNamespace(install=install))
    monkeypatch.setattr(guard, "_config_dir", lambda: tmp_path)
    assert app._enable_sandbox(["run", "--sandbox", "-a", "Code", "hi"]) == ["run", "-a", "Code", "hi"]
    assert started == [["run", "-a", "Code", "hi"]]
    assert guard.status().state == "active"


def test_icode_sandbox_goes_to_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(guard, "cli", lambda argv: seen.append(argv) or 7)
    monkeypatch.setattr(sys, "argv", ["icode", "sandbox", "status", "--json"])
    assert app.main() == 7
    assert seen == [["status", "--json"]]


def test_a_headless_run_says_whether_it_is_sandboxed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    guard.set_current(guard._NoGuard(guard.GuardStatus(state="active", requested=True, mechanism="Landlock ABI 4")))
    run._report_sandbox()
    assert capsys.readouterr().err == "Sandbox: active (Landlock ABI 4)\n"
    guard.set_current(guard._NoGuard())
    run._report_sandbox()
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("flag", ["--sandbox", "--no-sandbox"])
@pytest.mark.parametrize("before", [False, True])
def test_serve_passes_sandbox_choice_to_sessions_without_confining_server(monkeypatch, flag, before):
    import shlex

    from chrys.app.cli import serve

    def forbidden(argv, *, config_dir=None, resolve_launch_roots=None):
        raise AssertionError("the HTTP server must not initialize a session guard")

    monkeypatch.setattr(guard, "install_at_startup", forbidden)
    argv = [flag, "serve"] if before else ["serve", flag]
    rest = app._enable_sandbox(argv)
    args = serve.build_parser().parse_args(rest[1:])
    child = shlex.split(serve.build_tui_command(args.session, sandbox=args.sandbox))
    _, decision = guard.decide(child, Path("/nonexistent"), env={})
    assert decision.source == "cli"
    assert decision.enabled is (flag == "--sandbox")
