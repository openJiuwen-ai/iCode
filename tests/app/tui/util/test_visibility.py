# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Painted-frame contract of ``set_widget_visibility_without_layout``.

The helper bypasses Textual's layout-escalating visibility setter, so it must
itself keep the compositor's widget map, caches, and widget sizes coherent.
These tests pin the full contract against the composited frame — a widget
being ``visible``/in-map is NOT sufficient, the cells must actually repaint —
so upstream drift in the private compositor internals the helper touches
fails loudly on stock Textual.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Button, Static

from chrys.app.tui.util.visibility import set_widget_visibility_without_layout
from chrys.app.tui.widgets.loading import ChrysLoadingIndicator
from tests.support.waiting import wait_for


async def test_animation_visibility_rejects_detached_and_screenless_nodes() -> None:
    """A timer must tolerate both teardown shapes without resolving a missing screen."""
    from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen

    indicator = ChrysLoadingIndicator()
    assert not is_widget_shown_on_active_screen(indicator)
    indicator.automatic_refresh()
    async with App().run_test() as pilot:
        # App.mount normally delegates to a Screen; construct the exceptional
        # attached-but-screenless ancestry explicitly at Textual's attach seam.
        indicator._attach(pilot.app)
        try:
            assert indicator.is_attached
            assert not is_widget_shown_on_active_screen(indicator)
            indicator.automatic_refresh()
        finally:
            indicator._detach()


async def test_active_screen_visibility_is_false_once_shutdown_empties_the_screen_stack() -> None:
    """Shutdown pops every screen before it cancels the workers and timers that ask."""
    from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen

    async with _ToggleApp().run_test() as pilot:
        marker = pilot.app.query_one("#marker")
        await wait_for(lambda: is_widget_shown_on_active_screen(marker), pilot=pilot)
        stack = pilot.app._screen_stack
        screens = list(stack)
        stack.clear()
        try:
            assert not is_widget_shown_on_active_screen(marker)
        finally:
            stack.extend(screens)


def _frame_text(app: App) -> str:
    """Text of the currently composited frame."""
    strips = app.screen._compositor.render_strips()
    return "\n".join(strip.text for strip in strips)


class _ToggleApp(App):
    """A quiet app: nothing reflows unless a test forces it."""

    def compose(self) -> ComposeResult:
        yield Static("ROW-ABOVE", id="above")
        with Vertical(id="subject"):
            yield Static("SUBJECT-MARKER", id="marker")
        yield Static("ROW-BELOW", id="below")


class _HiddenAtMountApp(App):
    """Subject hidden from birth: its subtree is never composited or sized."""

    CSS = """
    #subject {
        height: 1;
        visibility: hidden;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("ROW-ABOVE", id="above")
        with Vertical(id="subject"):
            yield Static("SUBJECT-MARKER", id="marker")
        yield Static("ROW-BELOW", id="below")


@pytest.mark.asyncio
async def test_hide_without_layout_repaints_vacated_cells() -> None:
    """Hiding must actually clear the cells, not just flip model state."""
    app = _ToggleApp()
    async with app.run_test(size=(40, 10)) as pilot:
        subject = app.query_one("#subject")
        await pilot.pause()
        assert "SUBJECT-MARKER" in _frame_text(app)

        assert set_widget_visibility_without_layout(subject, False) is True
        await pilot.pause()
        await pilot.pause()
        frame = _frame_text(app)
        assert "SUBJECT-MARKER" not in frame
        assert "ROW-ABOVE" in frame
        assert "ROW-BELOW" in frame

        # Round trip back to visible without any intervening reflow.
        assert set_widget_visibility_without_layout(subject, True) is True
        await pilot.pause()
        await pilot.pause()
        assert "SUBJECT-MARKER" in _frame_text(app)


@pytest.mark.asyncio
async def test_show_without_layout_paints_never_composited_subtree() -> None:
    """Showing a subtree hidden since mount must size and paint it."""
    app = _HiddenAtMountApp()
    async with app.run_test(size=(40, 10)) as pilot:
        subject = app.query_one("#subject")
        await pilot.pause()
        assert "SUBJECT-MARKER" not in _frame_text(app)

        assert set_widget_visibility_without_layout(subject, True) is True
        await pilot.pause()
        await pilot.pause()
        assert "SUBJECT-MARKER" in _frame_text(app)
        # The reserved row keeps its geometry: neighbours are untouched.
        marker = app.query_one("#marker")
        assert marker.size.width > 0

        assert set_widget_visibility_without_layout(subject, False) is True
        await pilot.pause()
        await pilot.pause()
        assert "SUBJECT-MARKER" not in _frame_text(app)


@pytest.mark.asyncio
async def test_visibility_flip_does_not_snap_anchored_scroll() -> None:
    """The rebuild must not move anchored scrollables off a programmatic position."""

    class _AnchoredApp(App):
        CSS = """
        #log {
            height: 5;
            overflow-y: auto;
        }
        """

        def compose(self) -> ComposeResult:
            with Vertical(id="log"):
                for index in range(30):
                    yield Static(f"line {index}")
            yield Static("TOGGLE-ME", id="toggle")

    app = _AnchoredApp()
    async with app.run_test(size=(40, 12)) as pilot:
        log = app.query_one("#log", Vertical)
        toggle = app.query_one("#toggle")
        log.anchor()
        await pilot.pause()
        assert log.scroll_y == log.max_scroll_y

        # Anchored-but-above-bottom transient state (streaming settle dance).
        log.set_reactive(Vertical.scroll_y, log.max_scroll_y - 2)

        assert set_widget_visibility_without_layout(toggle, False) is True
        assert log.scroll_y == log.max_scroll_y - 2

        assert set_widget_visibility_without_layout(toggle, True) is True
        assert log.scroll_y == log.max_scroll_y - 2


@pytest.mark.asyncio
async def test_hide_blurs_focus_inside_hidden_subtree() -> None:
    """An invisible widget must not keep receiving key input.

    Display-driven hiding blurs via the stock Hide handler; the helper blurs
    synchronously so no key can land in the gap before event delivery.
    """

    class _FocusApp(App):
        def compose(self) -> ComposeResult:
            yield Static("above", id="above")
            with Vertical(id="subject"):
                yield Button("New", id="btn")

    app = _FocusApp()
    async with app.run_test(size=(40, 10)) as pilot:
        subject = app.query_one("#subject")
        button = app.query_one("#btn", Button)
        button.focus()
        await wait_for(lambda: button.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()
        assert app.focused is button

        assert set_widget_visibility_without_layout(subject, False) is True
        assert app.focused is not button

        # Hiding an unfocused subtree never touches focus.
        assert set_widget_visibility_without_layout(subject, True) is True
        focused_before = app.focused
        assert set_widget_visibility_without_layout(subject, False) is True
        assert app.focused is focused_before


@pytest.mark.asyncio
async def test_visibility_flip_is_idempotent_and_layout_free() -> None:
    """Unchanged rules are no-ops and flips never schedule a screen layout."""
    app = _ToggleApp()
    async with app.run_test(size=(40, 10)) as pilot:
        subject = app.query_one("#subject")
        await pilot.pause()

        layout_calls: list[None] = []
        original = app.screen._refresh_layout

        def _spy(*args: object, **kwargs: object) -> None:
            layout_calls.append(None)
            original(*args, **kwargs)

        app.screen._refresh_layout = _spy  # type: ignore[method-assign]

        assert set_widget_visibility_without_layout(subject, False) is True
        await pilot.pause()
        assert set_widget_visibility_without_layout(subject, False) is False
        assert set_widget_visibility_without_layout(subject, True) is True
        await pilot.pause()
        assert set_widget_visibility_without_layout(subject, True) is False

        assert layout_calls == []
        assert "SUBJECT-MARKER" in _frame_text(app)


async def test_is_widget_shown_rejects_offscreen_widgets_without_arranging() -> None:
    """Offscreen widgets in scroll containers must not report as shown.

    The raw compositor maps (``_visible_map``/``_full_map``) retain entries
    for widgets scrolled out of view; only the ``visible_widgets`` cut applies
    the screen/clip predicate. Regression: an offscreen indicator reported
    shown whenever the visible caches were cleared but a stale full map
    remained (the window right after a reflow), reintroducing the repaint
    work the ``auto_refresh`` gating exists to avoid.
    """
    from textual.containers import VerticalScroll

    from chrys.app.tui.util.visibility import is_widget_shown

    class _ScrollApp(App):
        def compose(self) -> ComposeResult:
            with VerticalScroll():
                for index in range(50):
                    yield Static(f"filler {index}")
                yield ChrysLoadingIndicator()

    async with _ScrollApp().run_test(size=(40, 10)) as pilot:
        indicator = pilot.app.query_one(ChrysLoadingIndicator)
        first_static = pilot.app.query(Static).first()
        compositor = pilot.app.screen._compositor

        # Viewport sits at the top; the indicator is 50 rows below it.
        assert not is_widget_shown(indicator)
        assert is_widget_shown(first_static)

        # Post-reflow window: visible caches cleared, stale full map retained
        # (what find_widget/hit-test traffic leaves behind).
        _ = compositor.full_map
        assert indicator in compositor._full_map
        original_arrange_root = compositor._arrange_root
        try:
            compositor._arrange_root = None  # type: ignore[assignment]  # tripwire: must not arrange
            compositor._visible_widgets = None
            compositor._visible_map = None
            assert not is_widget_shown(indicator)

            compositor._visible_widgets = None
            assert is_widget_shown(first_static)
        finally:
            compositor._arrange_root = original_arrange_root  # type: ignore[assignment]


async def test_is_widget_shown_rejects_visibility_hidden_widgets() -> None:
    """``visibility: hidden`` widgets must not report as shown.

    After a reflow the compositor prunes hidden subtrees from its map, but
    the cached ``visible_widgets`` cut is one frame stale: between a
    visibility flip and the next arrange the widget is still a member (and
    chrys deliberately keeps some visibility flips reflow-free, e.g. the
    scroll-to-bottom button). The helper must consult ``widget.visible`` —
    own rule or inherited — instead of trusting map membership.
    """
    from textual.containers import VerticalScroll

    from chrys.app.tui.util.visibility import is_widget_shown

    class _App(App):
        def compose(self) -> ComposeResult:
            with VerticalScroll():
                yield Static("content")

    async with _App().run_test(size=(40, 10)) as pilot:
        static = pilot.app.query_one(Static)
        container = pilot.app.query_one(VerticalScroll)
        assert is_widget_shown(static)

        # Inherited: the child has no visibility rule of its own. No pause —
        # the stale cached cut still holds the widget (premise assert).
        container.visible = False
        assert static in pilot.app.screen._compositor.visible_widgets
        assert not is_widget_shown(static)
        await pilot.pause()
        assert not is_widget_shown(static)

        container.visible = True
        await pilot.pause()
        assert is_widget_shown(static)

        # Own rule, same stale window.
        static.visible = False
        assert static in pilot.app.screen._compositor.visible_widgets
        assert not is_widget_shown(static)
        await pilot.pause()
        assert not is_widget_shown(static)
