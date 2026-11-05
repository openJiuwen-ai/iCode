# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Whether a screen may still query and update the widgets it composed."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.screen import Screen


def composed_live(screen: Screen) -> bool:
    """Whether *screen* may query and update the widgets it composed.

    Not once its removal begins, the app's shutdown included: that prunes them
    before the screen unmounts, innermost first, while ``screen.is_attached``
    still holds, so work resuming then (a worker, a callback) finds them gone.
    Nor when the screen is pushed as the app begins to exit, or in its last
    running turn to mount once it stopped: none of them mount then. Owners may
    report to it, and it may mount, at any of those moments; nothing renders it
    then.
    """
    return screen.is_attached and screen.app.is_running and not screen._pruning and not screen._closing
