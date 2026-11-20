# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Public shell command filtering API.

Implementation details live in ``chrys.service.tools.builtins.shell_command_filtering``.
"""

from __future__ import annotations

from chrys.foundation.util.shell_tokens import _split_on_operators as _split_on_operators
from chrys.service.tools.builtins.shell_command_filtering.approval_safe import (
    ApprovalShellDecision,
    SafeShellCommand,
    classify_shell_for_approval,
)
from chrys.service.tools.builtins.shell_command_filtering.policy import FilterResult, ShellCommandFilter
from chrys.service.tools.builtins.shell_command_filtering.presets import (
    READONLY,
    UNRESTRICTED,
    get_preset,
    preset_names,
)
from chrys.service.tools.builtins.shell_command_filtering.readonly import is_safe_readonly_command
from chrys.service.tools.builtins.shell_command_filtering.tokenizer import parse_commands

__all__ = [
    "READONLY",
    "UNRESTRICTED",
    "ApprovalShellDecision",
    "FilterResult",
    "SafeShellCommand",
    "ShellCommandFilter",
    "classify_shell_for_approval",
    "get_preset",
    "is_safe_readonly_command",
    "parse_commands",
    "preset_names",
]
