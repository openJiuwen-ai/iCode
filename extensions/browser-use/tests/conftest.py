# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# The extension adapter imports `chrys.extensions`; tests run against the local
# iCode checkout unless ICODE_SRC points somewhere else.
ICODE_SRC = Path(os.environ.get("ICODE_SRC", ROOT.parents[1] / "src"))
if ICODE_SRC.is_dir():
    sys.path.insert(0, str(ICODE_SRC))


@pytest.fixture(autouse=True)
def isolated_browser_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the developer's saved browser connection, AppData, and env vars out of plugin tests."""
    for key in list(os.environ):
        if key.startswith("ICODE_BROWSER_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
