# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Startup feedback is visible before imports and preserves diagnostics."""

from __future__ import annotations

import io
import sys
from types import ModuleType

import pytest

from chrys.app.cli import app as cli_app
from chrys.app.cli.app import _show_startup_notice as show_startup_notice


class TerminalOutput(io.StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flushed = False

    def isatty(self) -> bool:
        return True

    def flush(self) -> None:
        self.flushed = True


@pytest.mark.parametrize("failure", [None, RuntimeError("import failed"), KeyboardInterrupt()])
def test_feedback_precedes_tui_import_and_preserves_errors(monkeypatch, failure) -> None:
    output = TerminalOutput()
    monkeypatch.setattr(sys, "stderr", output)
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    fake_tui = ModuleType("chrys.app.tui.app")

    def load_main(name: str):
        if name == "__path__":
            raise AttributeError(name)
        assert name == "main"
        # AIxCoding branding: startup message
        assert output.getvalue() == "Starting AIxCoding...\n"
        assert output.flushed
        if failure is not None:
            output.write("diagnostic\n")
            raise failure
        return lambda: None

    fake_tui.__getattr__ = load_main
    monkeypatch.setitem(sys.modules, fake_tui.__name__, fake_tui)
    if failure is None:
        assert cli_app._run_tui([]) == 0
    else:
        with pytest.raises(type(failure)):
            cli_app._run_tui([])
    assert output.getvalue() == "Starting AIxCoding...\n" + ("diagnostic\n" if failure else "")


@pytest.mark.parametrize("argv", [["--help"], ["-h"], ["-s", "session", "--help"], ["--version"], ["-v"]])
def test_help_has_no_startup_notice(monkeypatch, argv) -> None:
    output = TerminalOutput()
    monkeypatch.setattr(sys, "stderr", output)
    show_startup_notice(argv)
    assert output.getvalue() == ""


def test_redirected_output_has_no_startup_notice(monkeypatch) -> None:
    output = io.StringIO()
    monkeypatch.setattr(sys, "stderr", output)
    show_startup_notice([])
    assert output.getvalue() == ""


def test_web_driver_has_no_startup_notice(monkeypatch) -> None:
    output = TerminalOutput()
    monkeypatch.setattr(sys, "stderr", output)
    monkeypatch.setenv("TEXTUAL_DRIVER", "textual.drivers.web_driver:WebDriver")
    show_startup_notice([])
    assert output.getvalue() == ""


def test_unwritable_terminal_does_not_block_startup(monkeypatch) -> None:
    class BrokenOutput(TerminalOutput):
        def write(self, value: str) -> int:
            raise OSError("closed terminal")

    monkeypatch.setattr(sys, "stderr", BrokenOutput())
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    show_startup_notice([])
