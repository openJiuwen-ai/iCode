# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Filesystem-aware containment for registry folder validation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.foundation.i18n import DisplayPath, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.workspace import path_identity
from chrys.service.mutations.coordination import fold_case

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

PRIMARY_MISSING = msg(
    "workspaces.primary_missing",
    fallback="Primary folder does not exist: {path}.",
)
NAMED_PRIMARY_MISSING = msg(
    "workspaces.named_primary_missing",
    fallback="The primary folder of workspace “{name}”, {path}, does not exist.",
)


class MissingWorkspacePrimary(ValueError):
    """An explicit selection needs a primary folder that is gone."""

    code = "workspace_primary_missing"

    def __init__(
        self, primary: str, *, workspace_name: str | None = None, display_message: MessageRef | None = None
    ) -> None:
        path = DisplayPath(primary)
        primary_missing = (
            NAMED_PRIMARY_MISSING.bind(name=workspace_name, path=path)
            if workspace_name
            else PRIMARY_MISSING.bind(path=path)
        )
        super().__init__(format_message(primary_missing))
        self.display_message = display_message or primary_missing


def resolve_workspace_directory(path: str) -> str | None:
    """Resolve a directory with this platform's own realpath semantics.

    The same rule every other workspace path comparison uses, so what the
    registry resolves is what opens and what the tools read: POSIX follows
    each link before applying ``..``, while Windows folds ``..`` lexically
    first (``ntpath.realpath`` normalizes before following links).
    """
    try:
        if os.path.isdir(path):
            return os.path.realpath(path, strict=True)
    except OSError:
        pass
    return None


def missing_folders(paths: Iterable[str]) -> set[str]:
    """Return the supplied paths that do not currently lead to directories."""
    return {path for path in paths if not os.path.isdir(path)}


def contains(
    folder: str,
    directory: str,
    *,
    identity_of: Callable[[str | Path], tuple[int, int] | str] | None = None,
    realpath: Callable[[str], str] | None = None,
) -> bool:
    """Whether the workspace folder contains *directory*, including aliases.

    Identity is never cached across calls: a moved folder is no longer at its
    original path. One validation may pass cached lookups for its own paths.
    Missing tails still compare against existing ancestors.
    """
    identity_of = identity_of or path_identity
    realpath = realpath or os.path.realpath
    identity = identity_of(folder)
    candidate_path = Path(realpath(directory))
    if not isinstance(identity, str):
        return any(identity_of(candidate) == identity for candidate in (candidate_path, *candidate_path.parents))

    ancestor, missing = _split_missing_tail(Path(realpath(folder)), identity_of)
    candidate_ancestor, candidate_missing = _split_missing_tail(candidate_path, identity_of)
    return ancestor == candidate_ancestor and candidate_missing[: len(missing)] == missing


def _split_missing_tail(
    path: Path, identity_of: Callable[[str | Path], tuple[int, int] | str]
) -> tuple[tuple[int, int] | str, tuple[str, ...]]:
    """Return the existing ancestor's identity and the folded missing components."""
    missing: list[str] = []
    while True:
        identity = identity_of(path)
        parent = path.parent
        if not isinstance(identity, str) or parent == path:
            return identity, tuple(reversed(missing))
        missing.append(fold_case(path.name))
        path = parent


def folders_overlap(first: str, second: str) -> bool:
    """Whether two folder paths are equal or either contains the other."""
    return contains(first, second) or contains(second, first)
