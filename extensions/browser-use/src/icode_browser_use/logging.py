# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Diagnostic logging for the plugin."""

from __future__ import annotations

import sys
from collections.abc import Callable

_LOG_SINK: Callable[[str], None] | None = None


def set_log_sink(sink: Callable[[str], None] | None) -> None:
    """Route diagnostics to *sink* instead of stderr."""
    global _LOG_SINK
    _LOG_SINK = sink


def log(message: str) -> None:
    """Write a diagnostic line to the configured sink, or stderr (never stdout)."""
    line = f"icode-browser-use: {message}"
    sink = _LOG_SINK
    if sink is not None:
        sink(line)
        return
    print(line, file=sys.stderr, flush=True)
