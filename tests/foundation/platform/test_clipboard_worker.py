# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""An isolated clipboard worker requires a complete, valid text request."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from importlib.metadata import distribution
from pathlib import Path
from types import SimpleNamespace

import pytest

from chrys.foundation.platform import clipboard_worker
from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs
from tests.support.waiting import ENGINE_TURN_TIMEOUT


@pytest.fixture(autouse=True)
def _worker_stdout(monkeypatch: pytest.MonkeyPatch):
    output = io.BytesIO()
    monkeypatch.setattr(
        clipboard_worker, "sys", SimpleNamespace(stdin=sys.stdin, stdout=SimpleNamespace(buffer=output))
    )
    return output


@pytest.mark.parametrize("text", ["", "中文😀\nline", "large\n" * 20000], ids=["empty", "unicode", "large"])
def test_worker_writes_the_complete_utf8_request(monkeypatch: pytest.MonkeyPatch, text: str, _worker_stdout) -> None:
    copied: list[str] = []
    stream = SimpleNamespace(buffer=io.BytesIO(json.dumps(text, ensure_ascii=False).encode("utf-8")))
    monkeypatch.setattr(clipboard_worker.sys, "stdin", stream)
    monkeypatch.setattr(clipboard_worker, "_clipboard_copy_windows", copied.append)
    assert clipboard_worker.main() == 0
    assert copied == [text]
    assert _worker_stdout.getvalue() == b"R"


@pytest.mark.parametrize("payload", [b"", b'"incomplete', b"null", b"123", b"[]", b'"\xff"'])
def test_worker_never_touches_clipboard_for_missing_or_partial_authorization(monkeypatch, payload: bytes) -> None:
    copied: list[str] = []
    monkeypatch.setattr(clipboard_worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload)))
    monkeypatch.setattr(clipboard_worker, "_clipboard_copy_windows", copied.append)
    assert clipboard_worker.main() == 1
    assert copied == []


@pytest.mark.parametrize(("failure", "status"), [(PermissionError, 2), (OSError, 1)])
def test_worker_reports_contention_separately_from_other_errors(monkeypatch, failure, status: int) -> None:
    def write(text: str) -> None:
        raise failure("native write failed")

    monkeypatch.setattr(clipboard_worker.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b'"text"')))
    monkeypatch.setattr(clipboard_worker, "_clipboard_copy_windows", write)
    assert clipboard_worker.main() == status


def test_worker_entrypoint_imports_in_an_isolated_interpreter_without_reading_project_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in ("HOME", "USERPROFILE", "APPDATA"):
        monkeypatch.setenv(name, str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-P", "-m", "chrys.foundation.platform.clipboard_worker"],
        input=b"",
        capture_output=True,
        check=False,
        timeout=10,
        **windows_hidden_subprocess_kwargs(),
    )
    assert result.returncode == 1
    assert result.stdout == b"R"
    assert result.stderr == b""


def test_worker_can_load_from_a_user_site_installation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in ("HOME", "USERPROFILE", "APPDATA", "PYTHONUSERBASE"):
        monkeypatch.setenv(name, str(tmp_path))
    for name in ("PYTHONPATH", "PYTHONNOUSERSITE"):
        monkeypatch.delenv(name, raising=False)
    base_python = sys._base_executable
    result = subprocess.run(
        [base_python, "-P", "-c", "import site; print(site.getusersitepackages())"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=ENGINE_TURN_TIMEOUT,
        **windows_hidden_subprocess_kwargs(),
    )
    user_site = Path(os.fsdecode(result.stdout).strip())
    user_site.mkdir(parents=True, exist_ok=True)
    # Use a .pth install so this is portable to Windows without symlink rights.
    (user_site / "icode.pth").write_text(str(Path(clipboard_worker.__file__).parents[3]) + "\n", encoding="utf-8")
    metadata = user_site / "icode_tui-0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(distribution("iCode-TUI").read_text("METADATA") or "", encoding="utf-8")
    result = subprocess.run(
        [base_python, "-P", "-m", "chrys.foundation.platform.clipboard_worker"],
        input=b"",
        capture_output=True,
        check=False,
        cwd=tmp_path,
        timeout=ENGINE_TURN_TIMEOUT,
        **windows_hidden_subprocess_kwargs(),
    )
    assert result.returncode == 1
    assert result.stdout == b"R"
    assert result.stderr == b""
