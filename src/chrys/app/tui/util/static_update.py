# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Replace a ``Static``'s content, laying the screen out only when the widget's geometry changes.

``Static.update()`` defaults to ``layout=True``. A layout request from a widget in the chat
transcript clears the arrangement of every auto-sized ancestor up to the chat panel and makes the
Screen reflow, and the reflow's cost grows with the mounted transcript. Running tool cards update a
spinner frame eight times a second, an elapsed-time label once a second and a streamed output tail
per line, and almost none of those updates changes the widget's size.

Textual caches each widget's box model per set of arrangement inputs and reuses it until the widget
itself requests a layout; a content change that skips the layout therefore must leave the box model
unchanged. Only an ``auto`` width or height reads the content, so a widget without either never needs
one. Otherwise the new content is measured the way the arrangement measures it, at the laid-out
width, and compared with the laid-out size: the width and height the arrangement last gave the
widget are fixed points of its clamping, so an equal measurement yields the same box model.
Hidden content invalidates cached measurements without scheduling a layout. The layout that
shows it reads the new content, whichever container was responsible for hiding it.

Geometry comes from ``outer_size``, which the last arrangement stored on the widget. ``size`` and
``region`` look the widget up in the compositor map, which an offscreen repaint invalidates, so
reading them could rebuild the map for the whole transcript.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.content import Content
from textual.geometry import Size
from textual.widget import Widget

if TYPE_CHECKING:
    from textual.visual import VisualType
    from textual.widgets import Static

_NEVER_ARRANGED = Size(0, 0)


def update_static_in_place(static: Static, content: VisualType) -> None:
    """Replace *static*'s content, requesting a layout only when its box model would change.

    A hidden subtree has no geometry on screen. Invalidate its cached measurements in place.
    """
    static.update(content, layout=False)
    styles = static.styles
    width_rule = styles.width
    height_rule = styles.height
    auto_width = width_rule is not None and width_rule.is_auto
    auto_height = height_rule is not None and height_rule.is_auto
    if not (auto_width or auto_height):
        return
    if not _invalidate_hidden_geometry(static) and _content_changes_geometry(
        static, auto_width=auto_width, auto_height=auto_height
    ):
        static.refresh(layout=True)


def refresh_static_geometry(static: Static) -> None:
    """Repaint a static whose geometry can change independently of its content."""
    static.refresh(layout=not _invalidate_hidden_geometry(static))


def _invalidate_hidden_geometry(static: Static) -> bool:
    if not static.is_mounted:
        return False
    chain = [node for node in static.ancestors_with_self if isinstance(node, Widget)]
    for index in range(len(chain) - 1, -1, -1):
        if not chain[index].display:
            # A hidden wrapper can cache its children's placements and its
            # own auto size. Drop both up to the outermost hidden wrapper so
            # revealing it remeasures the child in the same arrangement.
            for node in chain[: index + 1]:
                node._clear_arrangement_cache()
                node._layout_updates += 1
            return True
    return False


def _content_changes_geometry(static: Static, *, auto_width: bool, auto_height: bool) -> bool:
    styles = static.styles
    if not static.is_mounted:
        return True
    outer = static.outer_size
    if outer == _NEVER_ARRANGED:
        # No arrangement has measured the widget, so no cached box model holds its old content: the
        # arrangement that first shows a widget below a hidden ancestor measures the current content.
        # An auto-width widget can legitimately arrange to zero cells, so it keeps its layout.
        return auto_width or static.display
    if static.is_container or styles.overflow_x != "hidden" or styles.overflow_y != "hidden":
        # Children are measured through the layout, and a scrollbar adds cells between measuring
        # and clamping; neither is predicted here.
        return True
    gutter = styles.gutter
    content_width = outer.width - gutter.width
    if auto_width:
        if static.expand or static.shrink or not isinstance(static.render(), Content):
            # Only text content measures its optimal width independently of the container, whose
            # arrangement width the widget does not store.
            return True
        if static.get_content_width(static.container_size, static.container_size) != content_width:
            return True
    if auto_height:
        # A leaf widget's height depends on its content width alone; the container and viewport
        # arguments only matter to a widget that arranges children.
        measured = static.get_content_height(static.container_size, static.container_size, content_width)
        if measured != outer.height - gutter.height:
            return True
    return False
