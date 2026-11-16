# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression coverage for palette intent, modal feedback and edit identity."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest
from textual.color import Color
from textual.containers import VerticalScroll
from textual.css.stylesheet import Stylesheet, StylesheetParseError
from textual.widgets import Button, Input, Tabs

from chrys.app.tui.screens.themes.dialogs import _PickerModal
from chrys.app.tui.screens.themes.editor import ResettableThemeEditor
from chrys.app.tui.screens.themes.palette import _AnsiTokenPicker, _Xterm256PalettePicker
from chrys.app.tui.theme import CHRYS_ANSI_THEME, CHRYS_LEGACY_THEME
from chrys.app.tui.themes.document import copy_theme
from chrys.app.tui.themes.preview import css_error
from chrys.app.tui.widgets.color_picker import ColorPicker
from chrys.app.tui.widgets.select import Select
from chrys.foundation.i18n import Localizer
from tests.support.tui_helpers import resize_when_settled
from tests.support.waiting import wait_for, wait_until

from .helpers import make_app as _app
from .helpers import open_editor, press_button, wait_for_editor, wait_for_picker


async def test_picker_follows_current_theme_when_switching_between_ansi_and_rgb(tmp_path: Path) -> None:
    # This transition belongs to the editor and its modal, not MainScreen.
    # Real dock resize/focus regressions below keep the complete app host.
    initial_theme = "chrys-ansi"
    app = _app(tmp_path, initial_theme, component=True)
    async with app.run_test(size=(140, 50)) as pilot:
        editor = await open_editor(app, pilot, tmp_path)
        other_theme = "chrys"
        for step, name in enumerate((initial_theme, other_theme, initial_theme)):
            editor.query_one("#theme-select", Select).value = name
            await wait_for(
                lambda name=name: editor.document.draft.name == name and not editor._switching,
                pilot=pilot,
            )
            await wait_for(lambda: app.focused is editor._color_buttons["primary"], pilot=pilot)
            await press_button(pilot, editor._color_buttons["success"])
            expected = _Xterm256PalettePicker if name == "chrys-ansi" else ColorPicker
            await wait_for_picker(pilot, expected)
            assert app.screen.query(expected)
            tabs = app.screen.query_one(Tabs)
            assert tabs.active == ("picker-mode-xterm" if name == "chrys-ansi" else "picker-mode-rgb")
            original = editor.document.draft.success
            # Inspecting the alternate palette must not quantize the color.
            tabs.active = "picker-mode-ansi" if name == "chrys-ansi" else "picker-mode-xterm"
            alternate = _AnsiTokenPicker if name == "chrys-ansi" else _Xterm256PalettePicker
            await wait_for_picker(pilot, alternate)
            await press_button(pilot, "#picker-confirm")
            # Pop precedes ScreenResume. Its selector refresh must finish
            # before the next iteration submits another theme selection.
            await wait_for_editor(pilot)
            assert editor.document.draft.success == original
            assert not editor.document.unsaved
            assert not editor.document.undo_stack
            # Exercise RGB editing after the ANSI -> RGB transition. The initial
            # RGB mount already has its own mode/unchanged-confirm assertions.
            if name == "chrys" and step > 0:
                await press_button(pilot, editor._color_buttons["success"])
                await wait_for_picker(pilot, ColorPicker)
                app.screen.query_one("#color-expression", Input).value = "#123456"
                await wait_for(lambda: app.current_theme.success == "#123456", pilot=pilot)
                await press_button(pilot, "#picker-confirm")
                await wait_for_editor(pilot)
                assert editor.document.draft.success == "#123456"
                assert len(editor.document.undo_stack) == 1
                await press_button(pilot, "#theme-undo")
                await wait_for(lambda original=original: editor.document.draft.success == original, pilot=pilot)
                assert not editor.document.unsaved


def test_css_diagnostic_preserves_user_brackets_without_framework_markup() -> None:
    sheet = Stylesheet(variables={"draft": '"bad[blue]color"'})
    sheet.add_source("Static { color: $draft; }")
    with pytest.raises(StylesheetParseError) as caught:
        sheet.parse()
    diagnostic = css_error(caught.value)
    assert "[blue]" in diagnostic
    assert "[i]" not in diagnostic
    assert "$draft" in diagnostic
    assert "CSS 值无效" in css_error(caught.value, Localizer("zh-Hans"))


@pytest.mark.parametrize("mode", ["xterm", "ansi"])
async def test_palette_click_applies_an_approximate_highlight_but_right_click_does_not(
    tmp_path: Path, mode: str
) -> None:
    source = copy_theme(CHRYS_ANSI_THEME if mode == "ansi" else CHRYS_LEGACY_THEME)
    source.primary = "#123456"
    app = _app(tmp_path, source, component=True)
    async with app.run_test(size=(140, 50)) as pilot:
        editor = await open_editor(app, pilot, tmp_path)
        await pilot.click(editor._color_buttons["primary"])
        await wait_for_picker(pilot, _Xterm256PalettePicker if mode == "ansi" else ColorPicker)
        app.screen.query_one(Tabs).active = f"picker-mode-{mode}"
        if mode == "ansi":
            await wait_for_picker(pilot, _AnsiTokenPicker)
            palette = app.screen.query_one(_AnsiTokenPicker)
            point, other = (1, palette._selected_index), (1, palette._selected_index + 1)
            expected = palette.token
        else:
            await wait_for_picker(pilot, _Xterm256PalettePicker)
            palette = app.screen.query_one(_Xterm256PalettePicker)
            point = (palette._selected_column * 4 + 1, palette._selected_row)
            other = (1, 1)
            expected = palette.color.hex
        assert app.current_theme.primary == "#123456"
        await pilot.click(palette, offset=other, button=3)
        assert not await wait_until(lambda: app.current_theme.primary != "#123456", timeout=0.2, pilot=pilot)
        await pilot.click(palette, offset=point)
        await wait_for(lambda: app.current_theme.primary == expected, pilot=pilot)
        assert await pilot.click("#picker-confirm")
        assert editor.document.draft.primary == expected
        assert len(editor.document.undo_stack) == 1


@pytest.mark.parametrize("mode", ["xterm", "ansi"])
async def test_keyboard_palette_selection_scrolls_into_view_in_a_narrow_dialog(tmp_path: Path, mode: str) -> None:
    source = copy_theme(CHRYS_ANSI_THEME if mode == "ansi" else CHRYS_LEGACY_THEME)
    # Start beside the bottom/right edges. Crossing them with real keys tests
    # auto-scroll without repeatedly previewing every intervening color.
    source.success = "ansi_bright_cyan" if mode == "ansi" else "#FF87AF"
    # Keep this CI scrolling regression on the real covered MainScreen.
    app = _app(tmp_path, source)
    async with app.run_test(size=(60, 24)) as pilot:
        editor = await open_editor(app, pilot, tmp_path)
        modal = _PickerModal(editor, target="color:success", initial=editor.document.draft.success)
        with patch.object(modal, "scroll_to_center", wraps=modal.scroll_to_center) as center:
            await app.push_screen(modal)
            await wait_for_picker(pilot, _AnsiTokenPicker if mode == "ansi" else ColorPicker)
            modal.query_one(Tabs).active = f"picker-mode-{mode}"
            kind = _AnsiTokenPicker if mode == "ansi" else _Xterm256PalettePicker
            await wait_for_picker(pilot, kind)
            await pilot.pause()
        assert not any(
            isinstance(call.args[0], (_AnsiTokenPicker, _Xterm256PalettePicker)) for call in center.call_args_list
        )
        palette = modal.query_one(kind)
        body = modal.query_one("#picker-body", VerticalScroll)
        body.scroll_to(0, 0, animate=False, immediate=True)
        await pilot.pause()
        # Mount/resize/selection callbacks can share one refresh. Repeating
        # the request before the compositor moves must not scroll twice.
        modal._scroll_palette_selection()
        position = body.scroll_offset
        modal._scroll_palette_selection()
        assert body.scroll_offset == position
        await wait_for(
            lambda: body.scrollable_content_region.contains_region(
                palette.selection_region.translate(palette.content_region.offset)
            ),
            pilot=pilot,
        )
        if mode == "xterm":
            before_x = body.scroll_x
            await pilot.press("right", "right")
            await wait_for(lambda: body.scroll_x > before_x, pilot=pilot)
        before_y = body.scroll_y
        # The last step also exercises the clamped bottom edge. Xterm crosses
        # the grayscale and transparent section headers on its way there.
        await pilot.press(*(("down",) * (2 if mode == "ansi" else 4)))
        await wait_for(
            lambda: (
                body.scroll_y > before_y
                and body.scrollable_content_region.contains_region(
                    palette.selection_region.translate(palette.content_region.offset)
                )
            ),
            pilot=pilot,
            description="last selected palette row is fully visible after scrolling and layout",
        )
        assert app.screen.can_view_entire(modal.query_one("#picker-confirm"))
        expected = "ansi_bright_white" if mode == "ansi" else "transparent"
        await wait_for(lambda: app.current_theme.success == expected, pilot=pilot)
        await pilot.press("escape")
        assert editor.document.undo_stack == []
        assert app.current_theme.success == source.success


async def test_rgb_mode_switch_preserves_original_comparison_and_invalid_confirm_state(tmp_path: Path) -> None:
    app = _app(tmp_path, "chrys-legacy", component=True)
    async with app.run_test(size=(140, 50)) as pilot:
        await open_editor(app, pilot, tmp_path)
        await wait_for(lambda: bool(app.screen.query(ResettableThemeEditor)), pilot=pilot)
        editor = app.screen.query_one(ResettableThemeEditor)
        original = editor.document.draft.primary
        await pilot.click(editor._color_buttons["primary"])
        await wait_for_picker(pilot, ColorPicker)
        field = app.screen.query_one("#color-expression", Input)
        field.value = "not-a-color"
        await wait_for(lambda: app.screen.query_one("#picker-confirm", Button).disabled, pilot=pilot)
        error = app.screen.query_one("#color-error")
        assert error.rich_style.color != error.rich_style.bgcolor
        await pilot.press("ctrl+enter")
        assert isinstance(app.screen, _PickerModal)
        assert editor.document.undo_stack == []
        field.value = "#123456"
        await wait_for(lambda: app.current_theme.primary == "#123456", pilot=pilot)
        await wait_for(lambda: not app.screen.query_one("#picker-confirm", Button).disabled, pilot=pilot)
        app.screen.query_one(Tabs).active = "picker-mode-xterm"
        await wait_for_picker(pilot, _Xterm256PalettePicker)
        app.screen.query_one(Tabs).active = "picker-mode-rgb"
        await wait_for_picker(pilot, ColorPicker)
        picker = app.screen.query_one(ColorPicker)
        await wait_for(lambda: picker.region.width > 0, pilot=pilot)
        assert picker.original.color == Color.parse(original)
        assert picker.state.color == Color.parse("#123456")
        await pilot.click("#picker-restore")
        await wait_for(lambda: app.current_theme.primary == original, pilot=pilot)
        assert not app.screen.query_one("#picker-confirm", Button).disabled
        await pilot.click("#picker-confirm")
        assert editor.document.undo_stack == []


async def test_rgb_focus_stays_visible_in_short_dialog_without_scrolling_a_replacement_picker(tmp_path: Path) -> None:
    app = _app(tmp_path, "chrys-legacy")
    async with app.run_test(size=(80, 24)) as pilot:
        editor = await open_editor(app, pilot, tmp_path)
        await pilot.click(editor._color_buttons["primary"])
        modal = await wait_for_picker(pilot, ColorPicker)
        picker = modal.query_one(ColorPicker)
        plane = picker.query_one("#color-sv")
        body = modal.query_one("#picker-body", VerticalScroll)
        await wait_for(lambda: body.scrollable_content_region.contains_region(plane.region), pilot=pilot)
        assert plane.has_focus and body.scroll_y > 0
        await resize_when_settled(pilot, 140, 24)
        await wait_for(lambda: body.scrollable_content_region.contains_region(plane.region), pilot=pilot)
        modal.query_one(Tabs).active = "picker-mode-xterm"
        await wait_for_picker(pilot, _Xterm256PalettePicker)
        palette = modal.query_one(_Xterm256PalettePicker)

        def selection_visible() -> bool:
            return body.scrollable_content_region.contains_region(
                palette.selection_region.translate(palette.content_region.offset)
            )

        await wait_for(selection_visible, pilot=pilot)
        # Removing the focused plane must not hand focus to the tabs with a
        # scroll that animates the selection back out of view.
        assert not await wait_until(
            lambda: app.animator.is_being_animated(body, "scroll_y") or not selection_visible(),
            timeout=0.5,
            pilot=pilot,
        )
        position = body.scroll_offset
        # Simulate a queued visibility request belonging to the removed RGB mode.
        modal._scroll_rgb_plane(picker)
        assert body.scroll_offset == position and palette.has_focus
        modal.query_one(Tabs).active = "picker-mode-rgb"
        await wait_for_picker(pilot, ColorPicker)
        replacement = modal.query_one(ColorPicker)
        assert replacement is not picker
        plane = replacement.query_one("#color-sv")
        await wait_for(lambda: body.scrollable_content_region.contains_region(plane.region), pilot=pilot)
        assert plane.has_focus
        await pilot.press("escape")
        assert editor.document.undo_stack == []


@pytest.mark.parametrize("ansi", [False, True])
async def test_legacy_color_expression_opens_palette_without_changing_the_document(tmp_path: Path, ansi: bool) -> None:
    original = "ansi_white 40%" if ansi else "auto 60%"
    source = copy_theme(CHRYS_ANSI_THEME if ansi else CHRYS_LEGACY_THEME)
    source.variables["text-muted"] = original
    app = _app(tmp_path, source, component=True)
    async with app.run_test(size=(140, 50)) as pilot:
        editor = await open_editor(app, pilot, tmp_path)
        swatch = editor._variable_swatches["text-muted"]
        expected = app.theme_variables["text-muted"]
        assert str(swatch.label) == ("#C0C0C0" if ansi else expected)
        assert expected == "ansi_white" if ansi else expected.startswith("#")
        swatch.press()
        kind = _AnsiTokenPicker if ansi else ColorPicker
        modal = await wait_for_picker(pilot, kind)
        assert not modal.query("#css-value")
        assert modal._current_color().hex == expected
        await pilot.click("#picker-confirm")
        await wait_for(lambda: app.screen is editor.screen, pilot=pilot)
        assert editor.document.draft.variables["text-muted"] == original
        assert editor.document.undo_stack == []
        swatch.press()
        modal = await wait_for_picker(pilot, kind)
        if ansi:
            picker = modal.query_one(_AnsiTokenPicker)
            picker.action_move(1)
        else:
            modal.query_one("#color-expression", Input).value = "#123456"
        await wait_for(lambda: app.current_theme.variables["text-muted"] != original, pilot=pilot)
        await pilot.click("#picker-restore")
        await wait_for(lambda: app.current_theme.variables["text-muted"] == original, pilot=pilot)
        assert modal._current_color().hex == expected
        await pilot.click("#picker-cancel")
        await wait_for(lambda: app.screen is editor.screen, pilot=pilot)
        assert editor.document.draft.variables["text-muted"] == original
        assert editor.document.undo_stack == []


async def test_rgb_comparison_keeps_palette_selection_when_switching_back(tmp_path: Path) -> None:
    from chrys.app.tui.widgets.color_picker.controls import ColorComparison

    app = _app(tmp_path, "chrys-legacy", component=True)
    async with app.run_test(size=(140, 50)) as pilot:
        editor = await open_editor(app, pilot, tmp_path)
        await pilot.click(editor._color_buttons["primary"])
        modal = await wait_for_picker(pilot, ColorPicker)
        tabs = modal.query_one(Tabs)
        tabs.active = "picker-mode-xterm"
        await wait_for_picker(pilot, _Xterm256PalettePicker)
        palette = modal.query_one(_Xterm256PalettePicker)
        row, column = next((i, cells.index(57)) for i, (_, cells) in enumerate(palette._rows) if 57 in cells)
        await pilot.click(palette, offset=(column * palette._CELL_WIDTH, row))
        await wait_for(lambda: modal._current_value == "#5F00FF", pilot=pilot)
        tabs.active = "picker-mode-rgb"
        await wait_for_picker(pilot, ColorPicker)
        picker = modal.query_one(ColorPicker)
        await wait_for(lambda: app.focused is picker.query_one("#color-sv"), pilot=pilot)
        comparison = picker.query_one(ColorComparison)
        assert picker.value == comparison.color.hex == "#5F00FF"
        assert comparison.original.hex == "#AF87FF"
        await pilot.click("#picker-confirm")
        await wait_for(lambda: app.screen is editor.screen, pilot=pilot)
        assert editor.document.draft.primary == "#5F00FF"
        assert len(editor.document.undo_stack) == 1


@pytest.mark.parametrize("ansi", [False, True])
async def test_picker_tab_set_and_active_tab_share_a_construction_snapshot(tmp_path: Path, ansi: bool) -> None:
    app = _app(tmp_path, "chrys-ansi" if ansi else "chrys", component=True)
    async with app.run_test(size=(140, 50)) as pilot:
        editor = await open_editor(app, pilot, tmp_path)
        modal = _PickerModal(editor, target="color:primary", initial=editor.document.draft.primary)
        # Exercise the inconsistent-snapshot crash without claiming that normal
        # document mutators can currently make this change between init/mount.
        editor.document.draft.ansi = not ansi
        try:
            await app.push_screen(modal)
            await wait_for_picker(pilot, _Xterm256PalettePicker if ansi else ColorPicker)
            tabs = modal.query_one(Tabs)
            expected = "picker-mode-xterm" if ansi else "picker-mode-rgb"
            assert tabs.active == expected
            assert tabs.get_tab(expected).is_mounted
        finally:
            editor.document.draft.ansi = ansi
        await pilot.click("#picker-cancel")


async def test_picker_accept_waits_for_covering_dialog_to_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

    composing, release = asyncio.Event(), asyncio.Event()

    class DelayedPickerModal(_PickerModal):
        async def _compose(self) -> None:
            composing.set()
            await release.wait()
            await super()._compose()

    monkeypatch.setattr("chrys.app.tui.screens.themes.editor._PickerModal", DelayedPickerModal)
    app = _app(tmp_path, "chrys-legacy", component=True)
    async with app.run_test(size=(140, 50)) as pilot:
        editor = await open_editor(app, pilot, tmp_path)
        # Reproduce the CI interleaving without relying on machine speed:
        # push_screen exposes the modal before its controls have composed.
        # Pilot would drain the deliberately blocked mount, so dispatch only.
        editor._color_buttons["primary"].press()
        ready = None
        modal = None
        opening_modal = None
        try:
            await wait_for(composing.is_set)
            assert isinstance(app.screen, _PickerModal)
            opening_modal = app.screen
            assert not opening_modal.query("#color-expression")
            ready = asyncio.create_task(wait_for_picker(pilot, ColorPicker))
            # Give the waiter the exposed-but-unmounted screen once; this is
            # an adversarial scheduler turn, not a readiness delay.
            await asyncio.sleep(0)
            assert not ready.done()
        finally:
            release.set()
            try:
                # Drain the real mount even if the waiter under test returned
                # too early, so a failed assertion cannot race app teardown.
                if opening_modal is not None:
                    await wait_for(lambda: opening_modal.is_mounted, pilot=pilot)
            finally:
                if ready is not None:
                    modal = await ready
        assert modal is not None
        modal.query_one("#color-expression", Input).value = "#123456"
        await wait_for(lambda: app.current_theme.primary == "#123456", pilot=pilot)
        cover = ConfirmDialog()
        await app.push_screen(cover)
        await wait_for(lambda: cover.is_mounted, pilot=pilot)
        modal.action_accept()
        assert app.screen is cover and modal in app.screen_stack
        assert modal._transaction_closed and modal._dismiss_requested
        assert editor.document.draft.primary == "#123456"
        await pilot.press("escape")
        await wait_for(lambda: app.screen is editor.screen, pilot=pilot)
        assert modal not in app.screen_stack and len(editor.document.undo_stack) == 1


# What the picker sees as it mounts once pushed in each window: whether it is pruning, the app running, and it attached.
_EXIT_WINDOWS = {"prune": (True, False, True), "late": (False, False, True)}


@pytest.mark.parametrize("window", list(_EXIT_WINDOWS))
async def test_a_picker_opened_as_the_app_exits_renders_nothing(tmp_path: Path, window: str) -> None:
    """The user can open a color picker and quit before it mounts: it focuses and previews nothing then.

    - ``prune``: pushed as the test's last step: the app then closes its screens, which prunes the picker's children;
    - ``late``: pushed in the app's last running turn: the picker mounts once the app stopped, and no child does.
    """
    mounted: list[tuple[bool, bool, bool]] = []

    class Mounting(_PickerModal):
        def on_mount(self) -> None:
            # Dispatch runs the picker's own handler after this one.
            mounted.append((self._pruning, self.app.is_running, self.is_attached))

    app = _app(tmp_path, copy_theme(CHRYS_LEGACY_THEME))
    # Leaving run_test re-raises what the picker's mount raised.
    async with app.run_test(size=(100, 40)) as pilot:
        editor = await open_editor(app, pilot, tmp_path)
        picker = Mounting(editor, target="color:success", initial=editor.document.draft.success)
        if window == "prune":
            app.push_screen(picker)
        else:
            # Pushed as leaving run_test yields, before the app shuts down.
            asyncio.get_running_loop().call_soon(app.push_screen, picker)
    assert mounted == [_EXIT_WINDOWS[window]]
