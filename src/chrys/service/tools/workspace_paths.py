# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tool errors for a session working directory that no longer exists.

The working directory can be deleted or moved outside the app while a turn
runs. Tools then report that fact in one wording, which tells the model to
stop and leave the choice of a new directory to the user, instead of a
misleading "not found" about the shell, interpreter or file.
"""

from __future__ import annotations

import os

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.platform.paths import is_absolute_path
from chrys.foundation.text.model_json import model_path
from chrys.service.tools.result_metadata import tool_error
from chrys.service.tools.session_artifacts import is_document_artifact_handle

WORKING_DIR_MISSING_KIND = "working_dir_missing"


def working_dir_missing_error(path: str) -> str:
    """Return the model-visible error for a session working directory that is gone."""
    return tool_error(
        WORKING_DIR_MISSING_KIND,
        f"working directory no longer exists — {model_path(path)} (deleted or moved outside {APP_DISPLAY_NAME}). "
        "Stop and tell the user; they need to choose another working directory.",
        details={"cwd": path},
    )


def missing_base_cwd_error(raw_path: str, base_cwd: str | None) -> str | None:
    """Return :func:`working_dir_missing_error` when *raw_path* depends on a missing *base_cwd*.

    A relative path resolves against the working directory, and an absolute
    path at or under it names a place inside it: using either would fail, or
    a write would quietly recreate the deleted directory. Absolute paths
    elsewhere and session artifact handles keep working.
    """
    if not base_cwd or os.path.isdir(base_cwd):
        return None
    if is_document_artifact_handle(raw_path):
        return None
    expanded = os.path.expanduser(raw_path.strip())
    if (os.path.isabs(expanded) or is_absolute_path(expanded)) and not _is_within(expanded, base_cwd):
        return None
    return working_dir_missing_error(base_cwd)


def _is_within(path: str, base: str) -> bool:
    """Return whether absolute *path* is *base* or lies under it.

    Compared as written and then with symlinks resolved, so another spelling
    of the same place (a symlinked parent folder, macOS's ``/tmp`` and
    ``/private/tmp``) also counts. Resolving follows the parts that still
    exist and keeps the missing rest as written.
    """
    if _is_lexically_within(path, base):
        return True
    # A path absolute only under the other platform's rules names no place here.
    if not (os.path.isabs(path) and os.path.isabs(base)):
        return False
    # ".." is removed first, as the tools' own resolver does, so a ".." after a
    # symlink climbs the path as written rather than the link's target.
    try:
        return _is_lexically_within(os.path.realpath(os.path.normpath(path)), os.path.realpath(os.path.normpath(base)))
    except OSError, ValueError:
        return False


def _is_lexically_within(path: str, base: str) -> bool:
    # normpath, not abspath: abspath of a relative base would read the process
    # cwd, which may be the deleted directory itself.
    path_key = os.path.normcase(os.path.normpath(path))
    base_key = os.path.normcase(os.path.normpath(base))
    try:
        return os.path.commonpath((path_key, base_key)) == base_key
    except ValueError:
        # Different drives, or an absolute and a relative path: never inside.
        return False
