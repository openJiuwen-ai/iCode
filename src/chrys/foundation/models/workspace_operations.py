# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Explicit workspace operation intent."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chrys.foundation.models.workspace import Workspace


class WorkspaceOperation(StrEnum):
    """Choose, leave, or align saved membership to the installed workspace."""

    NONE = "none"
    SELECT = "select"
    UNSPECIFY = "unspecify"
    ALIGN_MEMBERSHIP = "align_membership"


@dataclass(frozen=True, slots=True)
class WorkspaceDefinitionEdit:
    """The exact definition being edited and its requested name and reference folders."""

    expected: Workspace
    name: str
    folders: tuple[str, ...]


def default_workspace_operation() -> WorkspaceOperation:
    """An event field default must be a literal the event-declaration guard can see, so it comes from here."""
    return WorkspaceOperation.NONE
