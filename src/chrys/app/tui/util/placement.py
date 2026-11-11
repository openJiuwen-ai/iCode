# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pin an overlay widget to fixed cells inside its container's arrangement.

CSS ``overlay`` or ``position: absolute`` only keeps such a widget out of
flow; the region it shows on is set here, in the container's ``arrange``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from textual.geometry import Offset, Region

if TYPE_CHECKING:
    from textual._arrange import DockArrangeResult
    from textual.widget import Widget


def pin_fixed_placement(result: DockArrangeResult, widget: Widget, place: Callable[[Region], Region]) -> None:
    """Show *widget* on ``place(its arranged region)``, above every sibling, whatever the container's scroll.

    Runs on every arrange, cache hits that hand back the same result
    included, so it is idempotent and cheap: an already-pinned placement
    returns untouched, and the search starts from the end, where overlays sit
    in a long container. Resetting ``result._spatial_map`` forces an
    O(children) rebuild on the next spatial query, so it happens only when the
    placement changes.
    """
    placements = result.placements
    for index in range(len(placements) - 1, -1, -1):
        placement = placements[index]
        if placement.widget is not widget:
            continue
        region = place(placement.region)
        if placement.fixed and placement.region == region and placement.offset == Offset():
            return
        placements[index] = placement._replace(
            region=region,
            offset=Offset(),
            order=2**31 - 1,
            # Fixed keeps the region in the container's viewport rather than
            # shifting it by the container's scroll offset.
            fixed=True,
            # The fixed region owns positioning; CSS only keeps the widget out of flow.
            absolute=False,
        )
        result._spatial_map = None
        return
