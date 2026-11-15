# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rendered control colors across transparent ANSI and light/dark themes."""

from __future__ import annotations

from pathlib import Path

import pytest
from rich.color import Color, ColorSystem
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, Checkbox, DataTable, Input, OptionList, Static, Tabs

from chrys.app.tui.screens.dialogs.editor import EditorDialog
from chrys.app.tui.screens.main.model_indicator import ModelIndicatorState
from chrys.app.tui.screens.themes.picker import ThemesScreen
from chrys.app.tui.theme import CHRYS_ANSI_THEME, CHRYS_LEGACY_THEME, CHRYS_THEME, TuiVariableDefaultsMixin
from chrys.app.tui.themes.document import copy_theme
from chrys.app.tui.widgets.chat.chrome import ScrollToBottomButton
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.chrome.status_bar import StatusBar
from chrys.app.tui.widgets.editor import EditorBufferSnapshot
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.config.settings import Settings
from chrys.foundation.patches import textual_button_ansi
from tests.support.paths import SRC_ROOT
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for


@pytest.mark.parametrize("theme_name", ["chrys", "chrys-legacy", "chrys-ansi", "textual-light", "nord", "ansi-light"])
async def test_theme_name_does_not_change_rendered_controls_or_modal_surfaces(theme_name: str) -> None:
    class Controls(ModalScreen):
        def compose(self) -> ComposeResult:
            with Vertical(id="container"):
                yield Tabs("First", "Second")
                yield Input("Input")
                yield Checkbox("Checkbox", value=True)
                yield OptionList("First", "Second")
                yield DataTable()
                yield Static("Code", classes="virtualized-markdown--fence")
                yield Static("Quote", classes="virtualized-markdown--block-quote")

    class ControlApp(TuiVariableDefaultsMixin, App):
        CSS_PATH = SRC_ROOT / "chrys" / "app" / "tui" / "chrys.tcss"

    app = ControlApp()
    for theme in (CHRYS_THEME, CHRYS_LEGACY_THEME, CHRYS_ANSI_THEME):
        app.register_theme(theme)
    source = app.get_theme(theme_name)
    assert source is not None
    # Both adding and removing the old prefix must be visually inert.
    for name in ("custom", "chrys-custom"):
        app.register_theme(copy_theme(source, name=name))
    app.theme = theme_name
    async with app.run_test(size=(100, 40)) as pilot:
        await app.push_screen(Controls())
        table = app.screen.query_one(DataTable)
        table.add_column("Column")
        table.add_row("Cell")
        app.screen.query_one(OptionList).highlighted = 0
        snapshots = []
        for name in (theme_name, "custom", "chrys-custom"):
            app.theme = name
            app.screen.set_focus(None)
            await pilot.pause()
            snapshots.append(
                (
                    app.get_css_variables(),
                    app.screen.rich_style,
                    tuple((widget.rich_style, widget.styles.border) for widget in app.screen.walk_children()),
                    app.screen.query_one(Checkbox).get_component_rich_style("toggle--button"),
                    table.get_component_rich_style("datatable--header"),
                    app.screen.query_one(OptionList).get_component_rich_style("option-list--option-highlighted"),
                )
            )
        assert snapshots[0] == snapshots[1] == snapshots[2]


async def test_theme_picker_uses_selection_ink_independently_of_button_ink(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path, settings=Settings(theme="chrys"))
    source = copy_theme(CHRYS_THEME)
    source.variables.update({"block-cursor-foreground": "#123456", "button-flat-foreground": "#FFFFFF"})
    app.register_theme(source)
    async with app.run_test(size=(100, 40)) as pilot:
        await app.push_screen(ThemesScreen(app.theme))
        options = app.screen.query_one(OptionList)
        options.focus()
        await wait_for(lambda: options.has_focus, pilot=pilot)
        assert options.get_component_rich_style("option-list--option-highlighted").color == Color.parse("#123456")


@pytest.mark.parametrize("theme_name", ["textual-light", "textual-dark"])
async def test_plain_code_fence_uses_readable_theme_text(theme_name: str) -> None:
    class MarkdownApp(TuiVariableDefaultsMixin, App):
        CSS_PATH = SRC_ROOT / "chrys" / "app" / "tui" / "chrys.tcss"

        def compose(self) -> ComposeResult:
            yield VirtualizedMarkdown("```\nplain sample\n```", id="markdown")

    app = MarkdownApp()
    app.theme = theme_name
    async with app.run_test(size=(80, 20)) as pilot:
        await pilot.pause()
        markdown = app.query_one(VirtualizedMarkdown)
        style = markdown.get_component_rich_style("virtualized-markdown--fence")
        assert style.color is not None and style.bgcolor is not None
        assert _contrast_ratio(style.color, style.bgcolor) >= 4.5
        samples = [
            segment
            for y in range(markdown.size.height)
            for segment in markdown.render_line(y)
            if "plain sample" in segment.text
        ]
        assert samples
        for segment in samples:
            assert segment.style is not None and segment.style.color is not None and segment.style.bgcolor is not None
            assert _contrast_ratio(segment.style.color, segment.style.bgcolor) >= 4.5


@pytest.mark.parametrize(
    ("theme_name", "variant", "hover"),
    [
        ("textual-light", "warning", False),
        ("atom-one-light", "warning", False),
        ("atom-one-light", "success", False),
        ("gruvbox", "error", True),
        ("catppuccin-latte", "success", True),
    ],
)
async def test_generated_button_ink_stays_readable_on_its_fill(theme_name: str, variant: str, hover: bool) -> None:
    class ButtonApp(TuiVariableDefaultsMixin, App):
        CSS_PATH = SRC_ROOT / "chrys" / "app" / "tui" / "chrys.tcss"

        def compose(self) -> ComposeResult:
            yield Button("Example", variant=variant, flat=True, id="example")

    app = ButtonApp()
    app.theme = theme_name
    async with app.run_test(size=(80, 20)) as pilot:
        app.screen.set_focus(None)
        await pilot.hover("#example" if hover else app.screen, offset=(0, 0) if hover else (70, 19))
        button = app.query_one(Button)
        assert button.rich_style.color is not None and button.rich_style.bgcolor is not None
        assert _contrast_ratio(button.rich_style.color, button.rich_style.bgcolor) >= 3


@pytest.mark.parametrize("ansi", [False, True])
async def test_scrollbar_track_uses_common_color_in_every_state_and_keeps_explicit_overrides(ansi: bool) -> None:
    class ScrollApp(TuiVariableDefaultsMixin, App):
        def compose(self) -> ComposeResult:
            yield Static("scroll", id="scroll")

    app = ScrollApp()
    source = copy_theme(CHRYS_ANSI_THEME if ansi else CHRYS_THEME, name="common")
    source.variables["scrollbar-background"] = "#123456"
    app.register_theme(source)
    explicit = copy_theme(source, name="explicit")
    explicit.variables["scrollbar-background-hover"] = "#AABBCC"
    app.register_theme(explicit)
    app.theme = "common"
    async with app.run_test() as pilot:
        widget = app.query_one("#scroll")
        for name in ("common", "explicit"):
            app.theme = name
            await pilot.pause()
            assert widget.styles.scrollbar_background.hex6 == "#123456"
            assert widget.styles.scrollbar_background_active.hex6 == "#123456"
            assert widget.styles.scrollbar_background_hover.hex6 == ("#AABBCC" if name == "explicit" else "#123456")


@pytest.fixture(autouse=True)
def filled_ansi_buttons(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use the same Button CSS as bootstrap, even with an unpatched install."""
    monkeypatch.setattr(Button, "DEFAULT_CSS", Button.DEFAULT_CSS)
    monkeypatch.setattr(Button, textual_button_ansi._RUNTIME_PATCH_MARKER, False, raising=False)
    textual_button_ansi.apply_runtime_patch()


async def test_chrys_ansi_scroll_and_selector_surfaces(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path, settings=Settings(theme="chrys-ansi"))
    async with app.run_test(size=(160, 45)) as pilot:
        await wait_for(lambda: bool(app.screen.query(StatusBar)), pilot=pilot)
        status = app.screen.query_one(StatusBar)
        status.set_profile("Code Agent")
        status.set_model(ModelIndicatorState("Test Model", "", "select", "model-id", True))
        profile = status.query_one("#profile-tag", Static)
        model = status.query_one("#model-tag", Static)
        await wait_for(
            lambda: profile.region.width > 0 and model.region.width > 0 and screen_is_settled(app, app.screen),
            pilot=pilot,
        )
        assert profile.rich_style.bgcolor == model.rich_style.bgcolor
        assert profile.rich_style.color == model.rich_style.color

        scroll = app.screen.query_one(ScrollToBottomButton)
        assert scroll.rich_style.bgcolor == Color.parse("#303030")
        assert not scroll.rich_style.dim
        # The fixed gray must survive terminals limited to xterm-256 colors.
        assert scroll.rich_style.bgcolor.downgrade(ColorSystem.EIGHT_BIT).number == 236

        await pilot.hover(model)
        await wait_for(lambda: model.rich_style.bgcolor == Color.parse("#eeeeee"), pilot=pilot)
        assert model.rich_style.color == Color.parse("#000000")
        await pilot.hover(profile)
        await wait_for(lambda: profile.rich_style.bgcolor == Color.parse("#eeeeee"), pilot=pilot)
        assert profile.rich_style.color == Color.parse("#000000")

        status.set_model(ModelIndicatorState("Locked Model", "", "locked", "model-id", True))
        await pilot.hover(model)
        await wait_for(lambda: model.styles.background.a == 0.3, pilot=pilot)
        assert model.styles.color.a == 0.5
        assert model.rich_style.bgcolor != Color.parse("#eeeeee")


@pytest.mark.parametrize(
    ("theme_name", "expected"),
    [
        ("chrys", "#000000"),
        ("chrys-ansi", "#000000"),
        ("ansi-dark", "default"),
        ("ansi-light", "default"),
    ],
)
async def test_filled_buttons_use_chrys_ink_or_native_ansi_foreground(theme_name: str, expected: str) -> None:
    class ButtonApp(TuiVariableDefaultsMixin, App):
        CSS_PATH = SRC_ROOT / "chrys" / "app" / "tui" / "chrys.tcss"

        def __init__(self) -> None:
            super().__init__()
            self.register_theme(CHRYS_THEME)
            self.register_theme(CHRYS_ANSI_THEME)
            self.theme = theme_name

        def compose(self) -> ComposeResult:
            for variant in ("primary", "success", "warning", "error"):
                yield Button(variant, variant=variant)
                yield Button(variant, variant=variant, flat=True)
            yield InputBar()

    app = ButtonApp()
    async with app.run_test(size=(100, 45)) as pilot:
        app.query_one(InputBar).value = "ready to send"
        app.query_one(InputBar).has_messages = True
        app.screen.set_focus(None)
        await wait_for(
            lambda: (
                app.focused is None
                and not app.query_one("#send-btn", Button).disabled
                and app.query_one("#new-btn", Button).content_size.width > 0
            ),
            pilot=pilot,
        )
        buttons = [*app.query(Button)]
        buttons.remove(app.query_one("#editor-btn", Button))  # transparent text affordance
        buttons.remove(app.query_one("#cancel-btn", Button))  # secondary action on a muted fill
        for button in buttons:
            color = button.rich_style.color
            assert color == Color.parse(expected), (theme_name, button, button.rich_style)
            label_styles = [
                segment.style
                for line in range(button.content_size.height)
                for segment in button.render_line(line)
                if segment.text.strip()
            ]
            assert label_styles, (button, button.content_size)
            assert all(style is not None and style.color == color for style in label_styles)
            # Chrys uses fixed RGB ink even when ANSI slots are remapped or
            # brightened on Windows. Native ANSI themes keep terminal ink.
            encoded = button.rich_style.render("label", color_system=ColorSystem.EIGHT_BIT)
            assert ";".join(color.downgrade(ColorSystem.EIGHT_BIT).get_ansi_codes()) in encoded
            if expected == "#000000":
                assert "38;5;16" in encoded
            else:
                assert color.is_default
                assert color.get_ansi_codes() == ("39",)

        bar = app.query_one(InputBar)
        bar.agent_running = True
        bar.lock_with_text()
        cancel = bar.query_one("#cancel-btn", Button)
        await wait_for(lambda: cancel.content_size.width > 0, pilot=pilot, description="queued cancel button visible")
        assert cancel.display and not cancel.disabled
        style = cancel.rich_style
        assert style.color is not None and style.bgcolor is not None
        if not style.color.is_default and not style.bgcolor.is_default:
            assert _contrast_ratio(style.color, style.bgcolor) >= 4.5
        label_styles = [segment.style for segment in cancel.render_line(0) if segment.text.strip()]
        assert label_styles
        assert all(label is not None and label.color == style.color for label in label_styles)


def _contrast_ratio(color: Color, background: Color) -> float:
    def luminance(value: Color) -> float:
        rgb = value.get_truecolor()
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (v / 255 for v in rgb)]
        return sum(c * weight for c, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    lower, upper = sorted((luminance(color), luminance(background)))
    return (upper + 0.05) / (lower + 0.05)


@pytest.mark.parametrize("theme_name", ["textual-dark", "textual-light", "gruvbox", "nord"])
async def test_editor_buttons_keep_contrast_on_muted_rgb_fills(tmp_path: Path, theme_name: str) -> None:
    app = make_chrys_app(tmp_path, settings=Settings(theme=theme_name))
    fixed_ink = Color.parse(
        app.current_theme.variables.get("button-color-foreground", "#000000" if app.current_theme.dark else "#ffffff")
    )
    async with app.run_test(size=(130, 45)) as pilot:
        await app.push_screen(EditorDialog(EditorBufferSnapshot("draft", (0, 5))))
        app.screen.set_focus(None)
        await wait_for(lambda: app.focused is None, pilot=pilot)
        for button in app.screen.query(Button):
            style = button.rich_style
            assert style.color is not None and style.bgcolor is not None
            # Preserve each theme's tinted text on muted fills. The regression
            # used ink intended for solid fills and reduced this contrast.
            assert _contrast_ratio(style.color, style.bgcolor) > _contrast_ratio(fixed_ink, style.bgcolor), (
                theme_name,
                button.id,
                style,
            )


@pytest.mark.parametrize("dark", [True, False])
async def test_rgb_button_ink_follows_fill_and_respects_theme_overrides(tmp_path: Path, dark: bool) -> None:
    app = make_chrys_app(tmp_path)
    app.register_theme(Theme(name="custom", primary="#101010", error="#eeeeee", dark=dark))
    app.register_theme(
        Theme(
            name="custom-ink",
            primary="#eeeeee",
            dark=dark,
            variables={"button-color-foreground": "#123456", "text-primary": "#abcdef"},
        )
    )
    app.theme = "custom"
    async with app.run_test(size=(130, 45)) as pilot:
        dark_button = Button("dark fill", variant="primary")
        light_button = Button("light fill", variant="error")
        flat_button = Button("muted fill", variant="primary", flat=True)
        await app.screen.mount(dark_button, light_button, flat_button)
        app.screen.set_focus(None)
        assert dark_button.rich_style.color == Color.parse("#ffffff")
        assert light_button.rich_style.color == Color.parse("#000000")

        app.theme = "custom-ink"
        await wait_for(lambda: dark_button.rich_style.color == Color.parse("#123456"), pilot=pilot)
        assert light_button.rich_style.color == Color.parse("#123456")
        assert flat_button.rich_style.color == Color.parse("#abcdef")
