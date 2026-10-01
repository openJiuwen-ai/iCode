# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Carry a confirmed file destination across async middleware and worker threads."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from chrys.foundation.platform.paths import resolve_workspace_path


@dataclass(frozen=True)
class FileWriteTarget:
    """The replaced entry and, for a symlink, the source read by write/edit previews."""

    path: str
    link_target: str | None = None
    referent: str | None = None


_approved_targets: ContextVar[tuple[FileWriteTarget, ...] | None] = ContextVar("approved_file_targets", default=None)


def file_write_target(path: str, *, base_cwd: str | None = None) -> FileWriteTarget:
    """Track one-time approval independently of whether a destination is reusable."""
    lexical = resolve_workspace_path(path, base_cwd=base_cwd)
    # Resolve only the parent: atomic replacement replaces the final entry,
    # not its referent. Pin this entry even when it is a final-file symlink.
    parent, name = os.path.split(lexical)
    entry = os.path.join(os.path.realpath(parent, strict=os.path.ALLOW_MISSING), name)
    if os.path.islink(entry):
        # Non-strict resolution preserves the existing ability to replace a
        # dangling or looping final link; such links still cannot mint grants.
        return FileWriteTarget(entry, os.readlink(entry), os.path.realpath(entry))
    return FileWriteTarget(entry)


@contextmanager
def approved_file_targets(targets: tuple[FileWriteTarget, ...] | None) -> Iterator[None]:
    token = _approved_targets.set(targets)
    try:
        yield
    finally:
        _approved_targets.reset(token)


def approved_write_path(path: str, *, base_cwd: str | None = None) -> str:
    """Check at the write boundary and pin the path used by the whole operation."""
    targets = _approved_targets.get()
    if targets is None:
        return path
    target = file_write_target(path, base_cwd=base_cwd)
    if target not in targets:
        raise ValueError("File destination changed after approval; request approval again.")
    return target.path
