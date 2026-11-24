# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Explicit workspace operation intent."""

from __future__ import annotations

from enum import StrEnum


class WorkspaceOperation(StrEnum):
    """Choose a workspace, make the session Unspecified, or request neither operation."""

    NONE = "none"
    SELECT = "select"
    UNSPECIFY = "unspecify"


def default_workspace_operation() -> WorkspaceOperation:
    """An event field default must be a literal the event-declaration guard can see, so it comes from here."""
    return WorkspaceOperation.NONE
