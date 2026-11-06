# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Check that the directory a command was launched from still exists.

A shell can keep a directory as its cwd after it was deleted or moved. Commands
that use the cwd to resolve a workspace or project settings check it after
parsing their arguments. A valid absolute ``-C`` path also passes this check.
Otherwise, a missing cwd stops the command with a single error message.
"""

from __future__ import annotations

import os

LAUNCH_CWD_MISSING_CODE = "working_dir_missing"


def launch_cwd_missing_message(*, workdir_flag: bool, workdir: str = "") -> str | None:
    """Return the error line when the process cwd no longer exists, else ``None``.

    *workdir_flag* says whether the command accepts ``-C``, so the hint only
    names options the user can actually pass. *workdir* is the ``-C`` value
    given, if any: with the launch directory gone only an existing absolute
    directory replaces it, because a relative one (``-C .``) resolves against
    the missing directory.
    """
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = None
    if cwd is not None and os.path.isdir(cwd):
        return None
    expanded = os.path.expanduser(workdir) if workdir else ""
    if expanded and os.path.isabs(expanded) and os.path.isdir(expanded):
        return None
    hint = (
        "cd to an existing directory or pass -C with an absolute path."
        if workdir_flag
        else "cd to an existing directory."
    )
    return f"The current directory no longer exists; {hint}"
