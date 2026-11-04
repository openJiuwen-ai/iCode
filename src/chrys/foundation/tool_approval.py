# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Out-of-band "ask before it runs" default a tool declares about itself.

Mirrors the ``chrys_kind`` attribute channel (:mod:`chrys.foundation.tool_kinds`):
a tool that should not run unasked unless the user says so — it reaches the
network, or runs code the user did not write — declares it on the tool object,
and the approval policy reads it from the tool being called. The declaration
ranks below every explicit rule (session override, the profile's tool and kind
overrides) and above the profile default, so it only changes what the profile
leaves unsaid. Bound copies of instance-method tools keep it (``copy.copy``).
"""

from __future__ import annotations

from typing import Any, Final

_REQUIRES_APPROVAL_ATTR: Final[str] = "chrys_requires_approval"


def declare_requires_approval(tool: Any) -> None:
    """Make *tool* ask before it runs unless an explicit rule says otherwise."""
    setattr(tool, _REQUIRES_APPROVAL_ATTR, True)


def declares_requires_approval(tool: Any) -> bool:
    """Whether *tool* asks before it runs unless an explicit rule says otherwise."""
    return getattr(tool, _REQUIRES_APPROVAL_ATTR, False) is True
