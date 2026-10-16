# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the SuggestionList overlay widget shared by the /, @, #, and $ popups."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.content import Content
from textual.screen import Screen
from textual.style import Style
from textual.widgets import Static

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionItem, SuggestionList
from chrys.app.tui.widgets.loading import ChrysLoadingIndicator
from chrys.app.tui.widgets.marquee import OverflowMarquee
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.types import RuntimeSkillDetails
from tests.support.tui_helpers import make_suggestion_handler, make_suggestion_screen


class _SuggestionListApp(App):
    def __init__(self) -> None:
        self.selected: list[tuple[str, str, bool, str]] = []
        super().__init__()

    def compose(self) -> ComposeResult:
        yield SuggestionList()

    def on_suggestion_list_selected(self, event: SuggestionList.Selected) -> None:
        self.selected.append((event.text, event.mode, event.execute, event.kind))


class _TitledApp(App):
    # The -100% offset overlay needs content above it to paint over,
    # like the transcript it covers in the real layout.
    CSS = "#filler { height: 15; }"

    def __init__(self, *, locale_controller: LocaleController | None = None) -> None:
        self._titled_locale_controller = locale_controller
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Static("filler", id="filler")
        yield SuggestionList(locale_controller=self._titled_locale_controller)


class _FakeTimer:
    """Stands in for the Textual timer that drives the marquee animation."""

    def __init__(self, callback: Callable[[], None] | None = None) -> None:
        self.callback = callback
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True

    def fire(self) -> None:
        self.stopped = True
        if self.callback is not None:
            self.callback()


def _mouse_event(event_type: type[events.MouseEvent], x: int = 0, y: int = 0) -> events.MouseEvent:
    return event_type(None, x, y, 0, 0, 1, False, False, False)


async def test_suggestion_list_groups_items_and_selects_first_enabled_item() -> None:
    async with _SuggestionListApp().run_test() as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        suggestion_list.show(
            "commands",
            [
                SuggestionItem(
                    value="new",
                    label="/new",
                    section="System Commands",
                    kind="command",
                    disabled=True,
                ),
                SuggestionItem(
                    value="review",
                    label="/review",
                    section="Loaded Skills",
                    kind="skill",
                ),
            ],
        )

        assert len(suggestion_list._contents) == 4
        assert str(suggestion_list._contents[0]) == "System Commands"
        assert str(suggestion_list._contents[2]) == "Loaded Skills"
        assert suggestion_list._highlighted == 3
        assert suggestion_list.select_highlighted(execute=True) is True
        await pilot.pause()
        suggestion_list._highlighted = 1
        assert suggestion_list.select_highlighted(execute=True) is False
        await pilot.pause()

    assert pilot.app.selected == [("review", "commands", True, "skill")]


async def test_suggestion_list_navigation_wraps_and_skips_disabled_items() -> None:
    async with _SuggestionListApp().run_test() as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        suggestion_list.show(
            "commands",
            [
                SuggestionItem(value="new", label="/new", section="System Commands"),
                SuggestionItem(value="exit", label="/exit", section="System Commands", disabled=True),
                SuggestionItem(value="theme", label="/theme", section="System Commands"),
                SuggestionItem(value="review", label="/review", section="Loaded Skills"),
            ],
        )

        assert suggestion_list._highlighted == 1

        suggestion_list.move_cursor_up()
        assert suggestion_list._highlighted == 5

        suggestion_list.move_cursor_down()
        assert suggestion_list._highlighted == 1

        suggestion_list.move_cursor_down()
        assert suggestion_list._highlighted == 3


@pytest.mark.parametrize("mode", ["commands", "agents", "models", "history"])
async def test_suggestion_list_marquee_has_one_race_safe_timer_and_resets_static(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    marquee = OverflowMarquee(start_delay=10.0, step_interval=10.0, end_delay=10.0)
    app = _SuggestionListApp()
    async with app.run_test(size=(40, 20)) as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        suggestion_list._marquee = marquee
        timers: list[_FakeTimer] = []

        def set_timer(_delay: float, callback) -> _FakeTimer:
            timer = _FakeTimer(callback)
            timers.append(timer)
            return timer

        monkeypatch.setattr(suggestion_list, "set_timer", set_timer)
        first_label = Content.assemble("/first  ", ("A description that is far too long for this popup", "dim"))
        second_label = Content.assemble("/second  ", ("Another description that also exceeds the popup", "dim"))
        suggestion_list.show(
            mode,
            [
                SuggestionItem(value="first", label=first_label, marquee_start=len("/first  ")),
                SuggestionItem(value="second", label=second_label, marquee_start=len("/second  ")),
            ],
        )
        await pilot.pause()

        active_timers = [timer for timer in timers if not timer.stopped]
        assert len(active_timers) == 1
        first_generation_timer = active_timers[0]
        assert str(suggestion_list.render()).splitlines()[0] == first_label.plain

        first_generation_timer.fire()
        scrolling_first_row = str(suggestion_list.render()).splitlines()[0]
        assert scrolling_first_row.startswith("/first  ")
        assert scrolling_first_row != first_label.plain
        scrolling_timer = next(timer for timer in timers if not timer.stopped)

        suggestion_list.move_cursor_down()
        assert scrolling_timer.stopped is True
        assert str(suggestion_list.render()).splitlines() == [first_label.plain, second_label.plain]
        replacement_timer = next(timer for timer in timers if not timer.stopped)

        # A callback already queued by Textual before stop() must not advance
        # the replacement selection or disturb its one live timer.
        first_generation_timer.callback()
        assert next(timer for timer in timers if not timer.stopped) is replacement_timer
        assert str(suggestion_list.render()).splitlines() == [first_label.plain, second_label.plain]

        monkeypatch.setattr(suggestion_list, "_is_on_current_screen", lambda: False)
        replacement_timer.fire()
        assert marquee.active is False
        assert [timer for timer in timers if not timer.stopped] == []

        monkeypatch.setattr(suggestion_list, "_is_on_current_screen", lambda: True)
        suggestion_list.resume_marquee()
        resumed_timer = next(timer for timer in timers if not timer.stopped)
        suggestion_list.pause_marquee()
        assert resumed_timer.stopped is True
        assert marquee.active is False
        assert [timer for timer in timers if not timer.stopped] == []

        suggestion_list.resume_marquee()
        assert len([timer for timer in timers if not timer.stopped]) == 1
        suggestion_list.hide()
        assert marquee.active is False
        assert [timer for timer in timers if not timer.stopped] == []


async def test_suggestion_list_does_not_animate_files_or_fitting_labels() -> None:
    async with _SuggestionListApp().run_test(size=(40, 20)) as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)

        suggestion_list.show("files", [("long", "x" * 100)])
        assert suggestion_list._marquee_timer is None

        suggestion_list.show(
            "commands",
            [SuggestionItem(value="short", label="/short  Fits", marquee_start=len("/short  "))],
        )
        assert suggestion_list._marquee_timer is None


def test_suggestion_list_fired_timer_cancels_after_visibility_is_lost() -> None:
    marquee = OverflowMarquee()
    suggestion_list = SuggestionList(marquee=marquee)
    timer = _FakeTimer()
    suggestion_list._marquee_timer = timer  # type: ignore[assignment]
    suggestion_list.visible = False
    marquee.activate(Content("a long description"), viewport_width=4)

    suggestion_list._advance_marquee(suggestion_list._marquee_generation)

    assert timer.stopped is True
    assert suggestion_list._marquee_timer is None
    assert marquee.active is False


async def test_suggestion_marquee_reveals_end_then_restores_ellipsized_static_frame() -> None:
    class _OverlayApp(App):
        CSS = "#filler { height: 8; }"

        def compose(self) -> ComposeResult:
            yield Static("filler", id="filler")
            yield SuggestionList(marquee=OverflowMarquee(start_delay=60.0, step_interval=60.0, end_delay=60.0))

    async with _OverlayApp().run_test(size=(40, 10)) as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        label = Content.assemble(
            "/review  ",
            ("Review, refactor, and debug every changed file in the workspace", "dim"),
        )
        suggestion_list.show(
            "commands",
            [SuggestionItem(value="review", label=label, marquee_start=len("/review  "))],
        )
        await pilot.pause()
        await pilot.pause()

        def selected_row() -> str:
            rows = [strip.text for strip in pilot.app.screen._compositor.render_strips()]
            return next(row for row in rows if row.startswith("│") and row.endswith("│"))

        static_row = selected_row()
        assert "/review" in static_row
        assert "…" in static_row

        while suggestion_list._marquee.frame.cell_length > suggestion_list.content_size.width:
            assert suggestion_list._marquee.frame.plain.startswith("/review  ")
            suggestion_list._marquee.advance()
        assert suggestion_list._marquee.frame.plain.startswith("/review  ")
        suggestion_list.refresh()
        await pilot.pause()
        end_row = selected_row()
        assert "/review" in end_row
        assert "the workspace" in end_row
        assert "…" not in end_row

        suggestion_list._marquee.reset()
        suggestion_list.refresh()
        await pilot.pause()
        assert selected_row() == static_row


async def test_suggestion_marquee_repaints_static_frame_after_screen_resume() -> None:
    class _MarqueeScreen(Screen):
        CSS = "#filler { height: 8; } SuggestionList { offset-y: 0; }"

        def compose(self) -> ComposeResult:
            yield Static("filler", id="filler")
            yield SuggestionList(marquee=OverflowMarquee(start_delay=60.0, step_interval=60.0, end_delay=60.0))

        def on_screen_suspend(self) -> None:
            self.query_one(SuggestionList).pause_marquee()

        def on_screen_resume(self) -> None:
            self.query_one(SuggestionList).resume_marquee()

    class _CoveringScreen(Screen):
        def compose(self) -> ComposeResult:
            yield Static("Covering screen")

    class _ScreenStackApp(App):
        def on_mount(self) -> None:
            self.push_screen(_MarqueeScreen())

    async with _ScreenStackApp().run_test(size=(40, 12)) as pilot:
        suggestion_list = pilot.app.screen.query_one(SuggestionList)
        label = Content.assemble(
            "/review  ",
            ("Review, refactor, and debug every changed file in the workspace", "dim"),
        )
        suggestion_list.show(
            "commands",
            [SuggestionItem(value="review", label=label, marquee_start=len("/review  "))],
        )
        await pilot.pause()
        await pilot.pause()

        def selected_row() -> str:
            rows = [strip.text for strip in pilot.app.screen._compositor.render_strips()]
            return next(row for row in rows if "/review" in row)

        static_row = selected_row()
        suggestion_list._marquee.advance()
        suggestion_list.refresh()
        await pilot.pause()
        assert selected_row() != static_row

        pilot.app.push_screen(_CoveringScreen())
        await pilot.pause()
        await pilot.app.pop_screen()
        await pilot.pause()

        assert selected_row() == static_row


async def test_suggestion_list_long_window_tracks_wrapped_highlight() -> None:
    async with _SuggestionListApp().run_test() as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        suggestion_list.show(
            "commands",
            [SuggestionItem(value=f"item-{index}", label=f"Item {index}") for index in range(15)],
        )

        assert suggestion_list._highlighted == 0
        assert suggestion_list._window_start == 0

        suggestion_list._hovered_row = 2
        suggestion_list.move_cursor_up()
        assert suggestion_list._highlighted == 14
        assert suggestion_list._window_start == 3
        assert suggestion_list._hovered_row is None
        assert "Item 14" in str(suggestion_list.render())

        suggestion_list._hovered_row = 2
        suggestion_list.move_cursor_down()
        assert suggestion_list._highlighted == 0
        assert suggestion_list._window_start == 0
        assert suggestion_list._hovered_row is None


async def test_suggestion_list_wrap_to_first_item_keeps_section_header_visible() -> None:
    async with _SuggestionListApp().run_test() as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        suggestion_list.show(
            "commands",
            [
                SuggestionItem(
                    value=f"command-{index}",
                    label=f"/command-{index}",
                    section="System Commands",
                )
                for index in range(15)
            ],
        )

        assert suggestion_list._highlighted == 1
        assert suggestion_list._window_start == 0

        suggestion_list.move_cursor_up()
        assert suggestion_list._highlighted == 15
        assert suggestion_list._window_start == 4

        suggestion_list.move_cursor_down()
        assert suggestion_list._highlighted == 1
        assert suggestion_list._window_start == 0
        assert str(suggestion_list.render()).splitlines()[0] == "System Commands"


@pytest.mark.parametrize("mode", ["commands", "files", "agents", "models"])
async def test_suggestion_list_hover_paints_selectable_row_without_layout(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """The shared /, @, #, and $ popup gets TOC-like hover without a reflow."""

    class _HoverSuggestionListApp(_SuggestionListApp):
        CSS = "SuggestionList { offset-y: 0; }"

    async with _HoverSuggestionListApp().run_test() as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        suggestion_list.show(
            mode,
            [
                SuggestionItem(value="first", label="First"),
                SuggestionItem(value="second", label="Second"),
                SuggestionItem(value="disabled", label="Disabled", disabled=True),
            ],
        )
        await pilot.pause()
        layout_refreshes: list[None] = []
        monkeypatch.setattr(pilot.app.screen, "_refresh_layout", lambda: layout_refreshes.append(None))

        content_x = suggestion_list.gutter.left
        second_row_y = suggestion_list.gutter.top + 1
        assert await pilot.hover(suggestion_list, offset=(content_x, second_row_y)) is True
        await pilot.pause()

        assert suggestion_list._hovered_row == 1
        hovered_row = suggestion_list.render().split()[1]
        hover_span_style = hovered_row.spans[-1].style
        assert isinstance(hover_span_style, Style)
        assert (
            hover_span_style.background
            == suggestion_list.get_component_styles("suggestion-list--option-hover").background
        )
        assert hovered_row.cell_length == suggestion_list.content_size.width
        assert layout_refreshes == []

        selected_row_y = suggestion_list.gutter.top
        assert await pilot.hover(suggestion_list, offset=(content_x, selected_row_y)) is True
        await pilot.pause()
        assert suggestion_list._hovered_row == 0
        hovered_selected_row = suggestion_list.render().split()[0]
        selected_hover_style = hovered_selected_row.spans[-1].style
        assert isinstance(selected_hover_style, Style)
        assert (
            selected_hover_style.background
            == suggestion_list.get_component_styles("suggestion-list--option-hover").background
        )
        assert hovered_selected_row.cell_length == suggestion_list.content_size.width
        assert layout_refreshes == []

        disabled_row_y = suggestion_list.gutter.top + 2
        assert await pilot.hover(suggestion_list, offset=(content_x, disabled_row_y)) is True
        await pilot.pause()
        assert suggestion_list._hovered_row is None
        assert layout_refreshes == []

        assert await pilot.hover(suggestion_list, offset=(content_x, second_row_y)) is True
        await pilot.hover(offset=(0, 10))
        await pilot.pause()
        assert suggestion_list._hovered_row is None
        assert layout_refreshes == []


async def test_suggestion_list_click_maps_rows_through_border() -> None:
    """Click coordinates are widget-relative: border rows AND border columns
    must select nothing while content cells map to their items."""
    app = _SuggestionListApp()
    async with app.run_test() as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        suggestion_list.show(
            "commands",
            [
                SuggestionItem(value="first", label="First"),
                SuggestionItem(value="second", label="Second"),
            ],
        )
        await pilot.pause()

        top = suggestion_list.gutter.top
        suggestion_list.on_click(_mouse_event(events.Click, x=2, y=top - 1))  # border row
        await pilot.pause()
        assert app.selected == []

        suggestion_list.on_click(_mouse_event(events.Click, x=2, y=top + 1))  # second item
        await pilot.pause()
        assert [selected[0] for selected in app.selected] == ["second"]

        # Clicks on the vertical border columns must not select the row.
        left_border_x = suggestion_list.gutter.left - 1
        right_border_x = suggestion_list.size.width - suggestion_list.gutter.right
        suggestion_list.on_click(_mouse_event(events.Click, x=left_border_x, y=top))
        suggestion_list.on_click(_mouse_event(events.Click, x=right_border_x, y=top))
        await pilot.pause()
        assert [selected[0] for selected in app.selected] == ["second"]

        # The first content column still selects.
        suggestion_list.on_click(_mouse_event(events.Click, x=suggestion_list.gutter.left, y=top))
        await pilot.pause()
        assert [selected[0] for selected in app.selected] == ["second", "first"]


async def test_suggestion_list_wheel_scrolls_window_without_wrapping() -> None:
    """The wheel pans the viewport like the replaced OptionList: the
    highlight stays put and panning clamps at both edges instead of wrapping."""
    async with _SuggestionListApp().run_test() as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        suggestion_list.show(
            "commands",
            [SuggestionItem(value=f"item-{index}", label=f"Item {index}") for index in range(15)],
        )

        assert suggestion_list._highlighted == 0
        assert suggestion_list._window_start == 0

        # Wheel-up at the top edge: no wrap to the last item.
        suggestion_list._hovered_row = 1
        suggestion_list._on_mouse_scroll_up(_mouse_event(events.MouseScrollUp))
        assert suggestion_list._highlighted == 0
        assert suggestion_list._window_start == 0
        assert suggestion_list._hovered_row == 1

        # Wheel-down pans the window and leaves the highlight in place.
        suggestion_list._on_mouse_scroll_down(_mouse_event(events.MouseScrollDown))
        assert suggestion_list._highlighted == 0
        assert suggestion_list._window_start == 1
        assert suggestion_list._hovered_row is None

        # Panning clamps at the bottom edge instead of wrapping.
        for _ in range(20):
            suggestion_list._on_mouse_scroll_down(_mouse_event(events.MouseScrollDown))
        assert suggestion_list._window_start == 3  # 15 items - 12 visible rows
        assert suggestion_list._highlighted == 0
        assert "Item 14" in str(suggestion_list.render())

        # And clamps again at the top on the way back.
        for _ in range(20):
            suggestion_list._on_mouse_scroll_up(_mouse_event(events.MouseScrollUp))
        assert suggestion_list._window_start == 0


async def test_suggestion_list_flattens_multiline_labels_to_one_row_each() -> None:
    """Window math and click mapping assume one physical row per item."""
    multiline_label = "first line\nvalue [type=missing, input_value={}, input_type=dict])"
    async with _SuggestionListApp().run_test() as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        suggestion_list.show(
            "commands",
            [
                SuggestionItem(value="multi", label=multiline_label),
                SuggestionItem(value="plain", label="plain item", section="Bad\nSection"),
            ],
        )

        rendered = str(suggestion_list.render())
        # One physical row per entry (item, section, item): any newline inside
        # a label would shift every row under the mouse-mapping math.
        assert rendered.count("\n") == 2
        assert multiline_label.replace("\n", " ") in rendered

        assert suggestion_list._values[suggestion_list._index_at_y(0)] == "multi"
        assert suggestion_list._index_at_y(1) is not None  # flattened section row
        assert suggestion_list._values[suggestion_list._index_at_y(2)] == "plain"
        assert suggestion_list._index_at_y(3) is None


async def test_suggestion_list_height_tracks_row_count() -> None:
    """A short result set must not blank transcript rows above it.

    An oversized overlay paints opaque empty rows over the chat and swallows
    the mouse events aimed there; the box must hug its rendered rows.
    """
    async with _SuggestionListApp().run_test() as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)

        border_rows = suggestion_list.gutter.height  # separator border around the overlay
        suggestion_list.show("commands", [SuggestionItem(value="one", label="only item")])
        await pilot.pause()
        assert suggestion_list.region.height == 1 + border_rows

        suggestion_list.update([SuggestionItem(value=f"i{n}", label=f"Item {n}") for n in range(20)])
        await pilot.pause()
        assert suggestion_list.region.height == 12 + border_rows

        suggestion_list.update([SuggestionItem(value="a", label="A"), SuggestionItem(value="b", label="B")])
        await pilot.pause()
        assert suggestion_list.region.height == 2 + border_rows


async def test_suggestion_list_loading_state_contains_only_chrys_indicator_until_ready() -> None:
    """Cold async sources must not flash an empty-state row before results arrive."""
    async with _SuggestionListApp().run_test() as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        loading = suggestion_list.query_one(ChrysLoadingIndicator)

        suggestion_list.show_loading("files", title="Files")
        await pilot.pause()

        assert suggestion_list.mode == "files"
        assert suggestion_list.is_loading is True
        assert loading.display is True
        assert suggestion_list._contents == []
        assert suggestion_list.render().plain == ""
        assert suggestion_list.select_highlighted(execute=True) is False
        assert suggestion_list.region.height == 1 + suggestion_list.gutter.height

        suggestion_list.update([SuggestionItem(value="ready.py", label="ready.py")])
        await pilot.pause()

        assert suggestion_list.is_loading is False
        assert loading.display is False
        assert suggestion_list._values == ["ready.py"]
        assert suggestion_list.render().plain == "ready.py"


async def test_suggestion_list_surgical_height_patch_matches_real_reflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The typing-path in-place map patch must equal stock reflow geometry.

    update() rewrites the overlay's compositor entries directly instead of
    remapping the whole screen per keystroke; this pins that shortcut to the
    ground truth a real reflow computes, so stock compositor drift fails loudly.
    """
    import chrys.app.tui.widgets.chrome.suggestion_list as suggestion_list_module

    async with _SuggestionListApp().run_test() as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        compositor = pilot.app.screen._compositor

        suggestion_list.show(
            "commands",
            [SuggestionItem(value=f"i{n}", label=f"Item {n}") for n in range(12)],
        )
        await pilot.pause()

        # The typing path must take the O(1) patch, never the full resync.
        def _no_resync(_widget: object) -> None:
            raise AssertionError("update() fell back to a compositor-wide remap")

        monkeypatch.setattr(suggestion_list_module, "resync_compositor_regions", _no_resync)
        # Any repaint of a widget outside the compositor's visible set (e.g.
        # a timer tick under Windows xdist load) arms a full-map rebuild,
        # which update() correctly defers to. Consume it here — no awaits
        # follow before update() — so the patch precondition holds and this
        # test pins the typing path, not ambient scheduling noise.
        assert compositor.full_map.get(suggestion_list) is not None
        assert not compositor._full_map_invalidated
        suggestion_list.update([SuggestionItem(value="a", label="A"), SuggestionItem(value="b", label="B")])
        patched = compositor._full_map.get(suggestion_list)
        assert patched is not None
        assert patched.region.height == 2 + suggestion_list.gutter.height

        pilot.app.screen._refresh_layout()
        await pilot.pause()
        ground_truth = compositor._full_map.get(suggestion_list)
        assert patched == ground_truth


async def test_suggestion_list_update_retitles_and_localizes_empty_state() -> None:
    controller = LocaleController(Settings(locale="zh-Hans"))

    async with _TitledApp(locale_controller=controller).run_test(size=(60, 20)) as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        suggestion_list.show("commands", [SuggestionItem(value="a", label="a")], title="Commands")
        await pilot.pause()
        english_title = suggestion_list.border_title
        assert english_title is not None

        # No title supplied: the current border is kept as-is.
        suggestion_list.update([SuggestionItem(value="b", label="b")])
        assert suggestion_list.border_title is english_title

        # A supplied title re-renders the border; empty results localize.
        suggestion_list.update([], title="命令")
        await pilot.pause()
        await pilot.pause()
        assert suggestion_list._contents[0].plain == "无结果"
        strips = pilot.app.screen._compositor.render_strips()
        frame = "\n".join(strip.text for strip in strips)
        assert "命令" in frame
        assert "无结果" in frame

        suggestion_list.update([], title="")
        assert suggestion_list.border_title is None


async def test_suggestion_list_paints_border_title() -> None:
    async with _TitledApp().run_test(size=(60, 20)) as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        suggestion_list.show(
            "files",
            [SuggestionItem(value="a.py", label="a.py")],
            title="Files under /tmp/proj",
        )
        await pilot.pause()
        await pilot.pause()
        strips = pilot.app.screen._compositor.render_strips()
        frame = "\n".join(strip.text for strip in strips)
        assert "Files under /tmp/proj" in frame

        suggestion_list.hide()
        assert suggestion_list.border_title is None


async def test_suggestion_list_empty_state_has_no_bullet() -> None:
    async with _SuggestionListApp().run_test() as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        suggestion_list.show("agents", [])
        assert suggestion_list._contents[0].plain == "No results"


async def test_suggestion_list_update_shows_caller_empty_text() -> None:
    async with _SuggestionListApp().run_test() as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        suggestion_list.show("history", [])
        suggestion_list.update([], empty_text="No prompt history yet.")
        assert [content.plain for content in suggestion_list._contents] == ["No prompt history yet."]
        assert suggestion_list._values == [None]

        suggestion_list.update([])
        assert suggestion_list._contents[0].plain == "No results"


async def test_runtime_skill_suggestion_metadata_is_rendered_as_literal_text() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()
    items = handler._runtime_skill_suggestion_items(
        [RuntimeSkillDetails(name="review[bad]", description="Review [broken markup")]
    )

    async with _SuggestionListApp().run_test() as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        suggestion_list.show("commands", items)
        await pilot.pause()

        prompt = suggestion_list._contents[1]

    assert str(prompt) == "/review[bad]  Review [broken markup"


@pytest.mark.asyncio
async def test_click_during_loading_popup_selects_nothing() -> None:
    """Clicks against a loading popup — content row, border columns, border
    row — neither select a row nor post a Selected message: loading clears
    any previously shown rows, and no row exists until the async source
    resolves, so no selection can reach the handler (and no handler-driven
    submit can fire)."""
    app = _SuggestionListApp()
    async with app.run_test() as pilot:
        suggestion_list = pilot.app.query_one(SuggestionList)
        # A prior popup's rows must not be selectable while loading.
        suggestion_list.show("history", [SuggestionItem(value="stale", label="Stale")])
        suggestion_list.show_loading("history", title="Prompt History")
        await pilot.pause()

        assert suggestion_list.is_loading is True
        assert suggestion_list._values == []

        top = suggestion_list.gutter.top
        content_x = suggestion_list.gutter.left
        # Content-row click maps to nothing while zero rows are rendered.
        suggestion_list.on_click(_mouse_event(events.Click, x=content_x, y=top))
        # Vertical border columns are rejected by the column guard.
        suggestion_list.on_click(_mouse_event(events.Click, x=suggestion_list.gutter.left - 1, y=top))
        suggestion_list.on_click(
            _mouse_event(events.Click, x=suggestion_list.size.width - suggestion_list.gutter.right, y=top)
        )
        # The border row above the popup is outside the content area.
        suggestion_list.on_click(_mouse_event(events.Click, x=content_x, y=top - 1))
        await pilot.pause()

        assert app.selected == []
        assert suggestion_list.is_loading is True
        assert suggestion_list._values == []

        # The same content click selects the row once content has arrived.
        suggestion_list.update([SuggestionItem(value="ready prompt", label="ready prompt")])
        await pilot.pause()
        suggestion_list.on_click(_mouse_event(events.Click, x=content_x, y=top))
        await pilot.pause()
        assert [selected[0] for selected in app.selected] == ["ready prompt"]
        assert suggestion_list.is_loading is False
