# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Buddy CLI returns bounded JSON failures instead of leaking tracebacks."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from chrys.app.cli import buddy
from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b"\xff", "utf-8"),
        (b'{"protocol":"chrys-buddy-v1","action":[]}', "Unsupported Buddy frontend action"),
        ((" " * 65535 + "栗").encode("utf-8"), "exceeds 64 KiB"),
        (b"[" * 4000 + b"]" * 4000, "Unsupported Buddy frontend protocol"),
    ],
    ids=["invalid-utf8", "non-string-action", "oversized-utf8", "deeply-nested-json"],
)
def test_buddy_json_reports_invalid_and_oversized_input(
    payload: bytes, error: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def bootstrap(*, dotenv_override: bool, setup_telemetry: bool) -> None:
        assert dotenv_override is False
        assert setup_telemetry is False

    monkeypatch.setattr(buddy, "bootstrap_runtime", bootstrap)
    with io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8") as stdin:
        monkeypatch.setattr(sys, "stdin", stdin)
        assert buddy.main(["--json"]) == 1
    output = capsys.readouterr()
    response = json.loads(output.out)
    assert response["protocol"] == "chrys-buddy-v1"
    assert error in response["error"]
    assert "Traceback" not in output.err


def test_buddy_command_reads_and_updates_shared_storage(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.update(HOME=str(tmp_path), USERPROFILE=str(tmp_path), APPDATA=str(tmp_path))
    responses = []
    for action in ("status", "hatch", "status"):
        result = subprocess.run(
            [sys.executable, "-m", "chrys.app.cli.app", "buddy", "--json"],
            input=json.dumps({"protocol": "chrys-buddy-v1", "action": action}),
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=15,
            **windows_hidden_subprocess_kwargs(),
        )
        assert result.returncode == 0, result.stderr
        responses.append(json.loads(result.stdout))
    assert responses[0] == {"protocol": "chrys-buddy-v1", "buddy": None}
    assert responses[1]["buddy"] is not None
    assert responses[2] == responses[1]
