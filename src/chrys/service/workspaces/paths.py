# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Filesystem-aware containment for registry folder validation."""

from __future__ import annotations

import os
from pathlib import Path

from chrys.foundation.i18n import DisplayPath, MessageRef, msg
from chrys.foundation.models.workspace import path_identity

PRIMARY_MISSING = msg(
    "workspaces.primary_missing",
    fallback="Primary folder does not exist: {path}.",
)


class MissingWorkspacePrimary(ValueError):
    """An explicit selection needs a primary folder that is gone."""

    code = "workspace_primary_missing"

    def __init__(self, primary: str, *, display_message: MessageRef | None = None) -> None:
        super().__init__(f"Primary folder does not exist: {primary}.")
        self.display_message = display_message or PRIMARY_MISSING.bind(path=DisplayPath(primary))


def contains(folder: str, directory: str) -> bool:
    """Whether the workspace folder contains *directory*, including aliases.

    Identity is never cached across calls: a moved folder is no longer at its
    original path. Missing tails still compare against existing ancestors.
    """
    identity = path_identity(folder)
    if isinstance(identity, str):
        candidate = os.path.normcase(os.path.realpath(directory))
        try:
            return os.path.commonpath((identity, candidate)) == identity
        except ValueError:
            return False
    candidate_path = Path(os.path.realpath(directory))
    return any(path_identity(str(candidate)) == identity for candidate in (candidate_path, *candidate_path.parents))
