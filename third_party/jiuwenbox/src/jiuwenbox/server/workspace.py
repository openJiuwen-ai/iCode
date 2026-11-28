# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Server-managed workspace paths.

``JIUWENBOX_HOME`` in the server's environment names the home directory when
it is an absolute path; otherwise the home is ``.jiuwenbox`` under the
effective user's password-database home. A host that runs several servers
gives each its own home, so their state, policy and workspace files never
meet.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

ENV_HOME = "JIUWENBOX_HOME"


def _effective_user_home() -> Path:
    """Return the effective user's home directory without trusting $HOME."""
    try:
        return Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except KeyError:
        return Path.home()


def _jiuwenbox_home() -> Path:
    configured = os.environ.get(ENV_HOME, "")
    if configured and os.path.isabs(configured):
        return Path(configured)
    return _effective_user_home() / ".jiuwenbox"


JIUWENBOX_HOME = _jiuwenbox_home()
SANDBOX_WORKSPACE = JIUWENBOX_HOME / "workspace"
