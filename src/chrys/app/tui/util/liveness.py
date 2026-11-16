# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Whether a screen, or a widget it composed, may still query and update the widgets it composed."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.widget import Widget


def composed_live(node: Widget) -> bool:
    """Whether *node*, a screen or a widget it composed, may query and update the widgets it composed.

    Not once its removal begins, the app's shutdown included: that prunes them
    before the screen unmounts, innermost first, while ``is_attached`` still
    holds, so work resuming then (a worker, a callback) finds them gone, and a
    widget can mount with its own children already pruned. Nor when its screen
    is pushed as the app begins to exit, or in its last running turn to mount
    once it stopped: none of them mount then. Owners may report to it, and it
    may mount, at any of those moments; nothing renders it then.
    """
    return node.is_attached and node.app.is_running and not node._pruning and not node._closing
