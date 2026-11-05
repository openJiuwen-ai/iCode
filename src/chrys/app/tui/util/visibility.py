# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cheap widget-visibility toggles and checks for fixed chrome."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import events
from textual.css.scalar import Scalar
from textual.dom import NoScreen
from textual.widget import Widget

from chrys.foundation.patches.textual_reflow_reuse import reflow_reusing_records

if TYPE_CHECKING:
    from collections.abc import Iterable


def set_widget_visibility_without_layout(widget: Widget, visible: bool) -> bool:
    """Set ``visibility`` without Textual's full-layout escalation.

    Visibility deliberately preserves geometry, but Textual marks its public
    setter as layout-affecting anyway: every toggle invalidates ancestor
    arrangement caches, recomputes layout math, and storms Resize events.
    Writing the local rule directly keeps those caches warm; the compositor is
    then resynchronized with one reflow that arranges only the flipped
    widget's ancestor path (see ``_sync_compositor_after_visibility_change``).

    Returns whether the local visibility rule changed.
    """
    return set_widgets_visibility_without_layout([(widget, visible)])


def set_widgets_visibility_without_layout(changes: Iterable[tuple[Widget, bool | None]]) -> bool:
    """Apply several visibility flips with a single compositor resync.

    All widgets must live on the same screen. Multi-widget mode switches
    (e.g. the status bar swapping its run/flash faces) pay one arrange
    instead of one per flip. A ``None`` value clears the widget's local rule
    so it inherits visibility from its parent again.

    Returns whether any local visibility rule changed.
    """
    return set_rules_without_layout(visibility=changes)


def set_rules_without_layout(
    *,
    visibility: Iterable[tuple[Widget, bool | None]] = (),
    heights: Iterable[tuple[Widget, int]] = (),
    remap: bool = True,
) -> bool:
    """Write visibility and fixed-height rules directly, then remap the screen once, with no layout.

    *visibility* as :func:`set_widgets_visibility_without_layout` takes it. Each
    of *heights* pins a widget to that many rows; a changed one drops the
    recorded arrangement of the widget and its ancestors, so the one remap
    places them again (a widget sized by what it shows, such as an overlay
    card, stays out of the screen's layout this way). With *remap* False
    nothing is remapped yet: for a hidden widget, the flip that shows it
    remaps it. All widgets must live on the same screen.

    Returns whether any rule changed.
    """
    changed: list[Widget] = []
    for widget, visible in visibility:
        rule_changed = (
            widget.styles.clear_rule("visibility")
            if visible is None
            else widget.styles.set_rule("visibility", "visible" if visible else "hidden")
        )
        if rule_changed:
            if visible is False:
                _blur_focus_within(widget)
            changed.append(widget)
    for widget, rows in heights:
        if widget.styles.set_rule("height", Scalar.from_number(rows)):
            for node in widget.ancestors_with_self:
                if isinstance(node, Widget):
                    node._clear_arrangement_cache()
            changed.append(widget)
    if not changed:
        return False
    if remap:
        _sync_compositor_after_visibility_change(changed[0])
        for widget in changed:
            widget.refresh()
    return True


def resync_compositor_regions(widget: Widget) -> None:
    """Remap widget regions after a manual style-rule change, without layout.

    For callers that mutate raw style rules (``styles.set_rule``) and clear
    the affected arrangement caches themselves: runs the same guarded
    reflow the visibility flips use, so the new placements paint. The rule
    writes stamp the changed widgets, which is what makes that reflow arrange
    them again rather than replay their recorded placements.
    """
    _sync_compositor_after_visibility_change(widget)
    widget.refresh()


def _blur_focus_within(widget: Widget) -> None:
    """Immediately drop focus held inside a subtree that is being hidden.

    The compositor resync posts a stock ``Hide`` event, whose handler blurs a
    focused widget — but events are asynchronous, leaving a window where an
    invisible button still receives key input. Blur synchronously instead.
    """
    try:
        screen = widget.screen
    except NoScreen:
        return
    focused = screen.focused
    if focused is not None and widget in focused.ancestors_with_self:
        focused.blur()


def _sync_compositor_after_visibility_change(widget: Widget) -> None:
    """Resynchronize the compositor with just-written visibility rules.

    The compositor honours ``visibility`` only while (re)building its widget
    maps; painting and hit-testing then run purely off those maps. A bare
    ``refresh()`` therefore changes nothing on screen: a newly hidden widget
    stays in the stale map and keeps repainting, a newly shown widget is
    absent from the map and never renders.

    Run one reflow and mirror ``Screen._refresh_layout``'s Hide/Show/Resize
    protocol. A from-scratch reflow places every displayed widget again,
    which grows with the transcript, and chrome widgets flip dozens of times
    per turn. With the reflow-reuse patch installed the reflow replays the
    recorded placements of every subtree unchanged since the last full
    arrangement: the raw rule writes (``Styles.set_rule``/``clear_rule``)
    stamped the flipped widgets and their ancestors, so only those paths are
    arranged again (see ``reflow_reusing_records``). Without the patch this
    is the stock reflow. Deferring it instead (leaving the map invalidated)
    would run the arrangement at the next ``widget.region`` read, outside
    the anchor guard below.
    """
    try:
        screen = widget.screen
    except NoScreen:
        # Not mounted into a screen yet: the first reflow honours the rule.
        return
    compositor = getattr(screen, "_compositor", None)
    if compositor is None:
        return
    size = screen.outer_size
    if not size:
        # Never composited (pre-first-reflow): the mount reflow honours the rule.
        return
    # Arranging snaps anchored scrollables (the chat transcript) to their
    # bottom as a side effect. A visibility flip is not a scroll event —
    # programmatic sub-bottom scroll positions (the streaming settle dance)
    # must survive it — so hold the anchors released while arranging. Only
    # already-mapped widgets can be in that state; scanning map keys is a
    # plain attribute sweep, not an arrange. Releasing an anchor stamps its
    # scrollable, so the reflow arranges that scrollable again but still
    # replays its unchanged children.
    anchored = [
        node
        for node_map in (compositor._full_map, compositor._visible_map or {})
        for node in node_map
        if getattr(node, "_anchored", False) and not node._anchor_released
    ]
    for node in anchored:
        node._anchor_released = True
    try:
        hidden, shown, resized = reflow_reusing_records(compositor, screen, size)
        # reflow() leaves any pre-armed full-map invalidation armed; consume
        # it here so the rebuild it implies also runs under the anchor guard.
        full_map = compositor.full_map
    finally:
        for node in anchored:
            node._anchor_released = False
    for node in hidden:
        node.post_message(events.Hide())
    # A shown subtree may never have been sized (hidden since mount) or hold
    # sizes stale from before hiding: push map geometry into widget sizes the
    # way Screen._refresh_layout does, without its layout escalation.
    for node in shown | resized:
        geometry = full_map.get(node)
        if geometry is None:
            continue
        if node._size_updated(geometry.region.size, geometry.virtual_size, geometry.container_size, layout=False):
            node.post_message(events.Resize(geometry.region.size, geometry.virtual_size, geometry.container_size))
    for node in shown:
        node.post_message(events.Show())


def is_widget_shown(widget: Widget) -> bool:
    """Return whether *widget* was in its owning Screen's last composited frame.

    Retain this raw per-screen predicate so test oracles can distinguish a
    covered Screen's visible cut from visibility on the active Screen.

    ``DOMNode.is_on_screen`` resolves the widget through
    ``Screen.find_widget``, which recomputes the compositor's *full* map
    whenever it was invalidated — an O(all mounted widgets) arrange pass.
    Periodic timers (animation ticks, ``auto_refresh``) that gate on it turn
    every map invalidation (each scroll tick, any layout change) into a
    full-tree arrange, which dominates CPU on large chat transcripts.

    This helper only consults already-computed compositor state: widgets
    culled out of view, hidden by their own ``display`` setting, or hidden
    via an ancestor all report ``False`` without arranging anything. The
    answer can be one frame stale and includes covered Screens. Animations
    that must stop under overlays use ``is_widget_shown_on_active_screen``.

    ``visibility: hidden`` needs its own check: it reserves layout space, so
    the widget stays in the compositor's purely geometric ``visible_widgets``
    cut. ``Widget.visible`` resolves the inherited value by walking ancestors
    (O(depth), no arrange).
    """
    return _is_widget_shown(widget, active_screen_only=False)


def is_widget_shown_on_active_screen(widget: Widget) -> bool:
    """Gate animations on the active screen's cached visible cut."""
    return _is_widget_shown(widget, active_screen_only=True)


def _is_widget_shown(widget: Widget, *, active_screen_only: bool) -> bool:
    if not widget.is_attached or not widget.display or not widget.visible:
        return False
    try:
        screen = widget.screen
    except NoScreen:
        return False
    if active_screen_only and screen is not screen.app.screen:
        return False
    compositor = getattr(screen, "_compositor", None)
    if compositor is None:
        return False
    # The ``visible_widgets`` property never arranges: it derives its cut from
    # the cached ``_visible_map``/``_full_map`` attributes (one-frame stale at
    # worst) and applies the screen/clip overlap predicate. Raw membership in
    # those maps is NOT equivalent — they retain entries for widgets scrolled
    # out of view, which would misreport offscreen widgets as shown.
    return widget in compositor.visible_widgets
