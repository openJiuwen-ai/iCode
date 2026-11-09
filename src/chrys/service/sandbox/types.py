# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Structural settings view for the sandbox backend.

The fields are the ones ``run_shell`` reads. A ``Settings`` instance matches
once those attributes exist; tests can pass a stand-in with the same three
attributes without constructing ``Settings``.
"""

from __future__ import annotations

from typing import Protocol


class SandboxSettings(Protocol):
    """Sandbox settings ``run_shell`` is allowed to read.

    The attributes are read-only so a frozen ``Settings`` value matches.
    """

    @property
    def sandbox_enabled(self) -> bool: ...

    @property
    def sandbox_url(self) -> str: ...

    @property
    def sandbox_token(self) -> str: ...
