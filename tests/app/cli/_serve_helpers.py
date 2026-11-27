# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared server and command doubles for the browser CLI tests."""

from __future__ import annotations

from typing import Any, ClassVar


class FakeServer:
    """Minimal textual-serve stand-in for CLI tests."""

    instances: ClassVar[list[FakeServer]] = []

    def __init__(self, command: str, **kwargs: Any) -> None:
        self.command = command
        self.kwargs = kwargs
        self.debug: bool | None = None
        FakeServer.instances.append(self)

    def serve(self, *, debug: bool = False) -> None:
        self.debug = debug


def fixed_tui_command(_session: str | None, *, sandbox: bool | None = None, on_unavailable: str | None = None) -> str:
    return "chrys __tui_subprocess__"
