# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""User-facing product branding."""

from __future__ import annotations

# AIxCoding branding: display name and user command
APP_DISPLAY_NAME = "AIxCoding"
# The command users are shown. ``chrys`` stays installed beside it, and everything
# internal (package, config directory, CHRYS_* variables) keeps the original name.
APP_COMMAND = "aixcoding-cli"


def format_app_version_title(version: str) -> str:
    """Return the user-facing application title with a version suffix."""
    return f"{APP_DISPLAY_NAME} v{version}"
