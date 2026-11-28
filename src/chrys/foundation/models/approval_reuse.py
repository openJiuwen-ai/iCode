# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Typed approval choices and presentation data; neither supplies authorization keys."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# ``EXACT_TURN`` and ``EXACT_WORKDIR`` are only offered for sandbox folder grants.
ReuseChoice = Literal[
    "", "EXACT_TURN", "EXACT_SESSION", "EXACT_WORKDIR", "EXACT_WORKSPACE", "PREFIX_SESSION", "PREFIX_WORKSPACE"
]


@dataclass(frozen=True)
class ApprovalReuseOffer:
    """What a dialog may offer to remember.

    ``targets`` are the command or files shown (``read_folder``, ``write_folder``:
    the folder a sandbox grant would cover); ``prefix`` says whether appended
    arguments can also be allowed (literal simple commands only).
    """

    kind: Literal["command", "files", "read_folder", "write_folder"]
    targets: tuple[str, ...]
    prefix: bool = False
    workspace: bool = False
