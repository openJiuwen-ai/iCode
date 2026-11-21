# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared path discovery for the manual smoke scripts.

Scripts resolve the browser and the iCode checkout from explicit environment
variables first, then use browser discovery and the containing repository.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

from icode_browser_use.cdp.launcher import discover_chrome

_BROWSER_ENV = ("CHRYS_CHROME", "ICODE_BROWSER_EXECUTABLE")
_ICODE_ENV = ("CHRYS_ICODE_SRC", "ICODE_SRC")


def chrome_executable() -> Path:
    """Return the browser executable, or exit with an actionable message."""
    for name in _BROWSER_ENV:
        value = os.environ.get(name)
        if not value:
            continue
        candidate = Path(value)
        if not candidate.is_file():
            raise SystemExit(f"{name} is set but is not a file: {candidate}")
        print(f"icode-browser-use smoke: using browser from {name}: {candidate}", file=sys.stderr)
        return candidate
    found = discover_chrome()
    if not found:
        raise SystemExit(
            "Chrome/Edge was not found; set CHRYS_CHROME to its executable "
            "(for example C:/Program Files/Google/Chrome/Application/chrome.exe)"
        )
    print(f"icode-browser-use smoke: discovered browser: {found}", file=sys.stderr)
    return Path(found)


def icode_checkout() -> Path:
    """Return the iCode source checkout root (the folder containing ``src/chrys``)."""
    for name in _ICODE_ENV:
        value = os.environ.get(name)
        if not value:
            continue
        candidate = Path(value)
        root = candidate.parent if candidate.name == "src" else candidate
        if not (root / "src" / "chrys").is_dir():
            raise SystemExit(f"{name} is set but {root} does not contain src/chrys")
        print(f"icode-browser-use smoke: using iCode checkout from {name}: {root}", file=sys.stderr)
        return root
    root = Path(__file__).resolve().parents[3]
    if (root / "src" / "chrys").is_dir():
        print(f"icode-browser-use smoke: using iCode checkout: {root}", file=sys.stderr)
        return root
    raise SystemExit("iCode checkout not found; set CHRYS_ICODE_SRC to the iCode repository root")


_CLEARED_VARIABLES = {
    "PYTHONPATH",
    "PYTHONHOME",
    "VIRTUAL_ENV",
    "UV_PROJECT_ENVIRONMENT",
    "UV_CONFIG_FILE",
    "UV_TARGET",
    "UV_PREFIX",
    "UV_SYSTEM_PYTHON",
    "UV_BREAK_SYSTEM_PACKAGES",
}


def clean_environment() -> dict[str, str]:
    """This process's environment without plugin settings or anything that retargets an import."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("ICODE_BROWSER_") and key.upper() not in _CLEARED_VARIABLES
    }
