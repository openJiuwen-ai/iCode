# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared semantic messages for clipboard feedback."""

from chrys.foundation.i18n import msg

COPY_TITLE = msg("tui.copy.title.action", fallback="Copy")
COPIED_TITLE = msg("tui.copy.title.success", fallback="Copied")
COPY_FAILED = msg(
    "tui.clipboard.copy_failed",
    fallback="Could not copy to the system clipboard. Please try again.",
)

__all__ = ["COPIED_TITLE", "COPY_FAILED", "COPY_TITLE"]
