# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Chrys TUI theme definitions."""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Any, TypedDict

from textual.color import Color, ColorParseError
from textual.design import DEFAULT_DARK_BACKGROUND, DEFAULT_LIGHT_BACKGROUND
from textual.theme import Theme

if TYPE_CHECKING:
    from textual.app import App

    _TuiVariableDefaultsBase = App[Any]
else:
    _TuiVariableDefaultsBase = object

# Built-in inventory only; these names never select rendering rules.
CHRYS_THEMES = {"chrys", "chrys-legacy", "chrys-ansi"}


# Shared Dracula-inspired palette used by every chrys theme.
class _ChrysColors(TypedDict):
    """Keyword-compatible color subset accepted by ``Theme``."""

    primary: str
    secondary: str
    warning: str
    error: str
    success: str
    accent: str
    foreground: str


_CHRYS_COLORS: _ChrysColors = {
    "primary": "#AF87FF",
    "secondary": "#5F5FAF",
    "warning": "#FFAF5F",
    "error": "#FF5F5F",
    "success": "#5FFF87",
    "accent": "#FF87D7",
    "foreground": "#EEEEEE",
}
CHRYS_COLOR_NAMES = tuple(_CHRYS_COLORS)

# Shared control slots. These RGB defaults are the Chrys palette; the resolver
# derives other themes' defaults from their own surfaces and foregrounds.
CONTROL_VARIABLE_DEFAULTS: dict[str, str] = {
    "control-background": "#303030",
    "control-background-muted": "#262626",
    "control-foreground-muted": "#585858",
    "overlay-background": "#3A3A3A",
    "markdown-block-background": "#303030",
}

# CSS variables that must remain resolvable with every Textual theme.
TUI_VARIABLE_DEFAULTS: dict[str, str] = {
    **CONTROL_VARIABLE_DEFAULTS,
    "border-opacity": "80%",
    # Boxes inside a bordered panel (the sidebar's sections) sit fainter than the panel's own outline.
    "inner-border-opacity": "50%",
    "control-disabled-background": "transparent",
    "hatch-color": "#808080 15%",
    "ansi-background": "ansi_default",
    "ansi-foreground": "ansi_default",
}

# The editor exposes only common controls. Semantic fills already follow the
# main palette. Existing per-variant YAML overrides remain compatible.
BUTTON_COLOR_VARIABLES = (
    "button-flat-foreground",
    "button-hover-foreground",
    "button-hover-background",
    "button-disabled-foreground",
    "button-disabled-background",
)


@lru_cache(maxsize=1024)
def concrete_theme_color(value: str, background: str, dark: bool) -> str:
    """Flatten legacy color/percentage expressions into theme-derived colors.

    ANSI ink stays terminal-owned: Textual ignores its alpha, so removing the
    percentage preserves the actual rendering. RGB/auto ink is blended over
    the background; terminal backgrounds use an RGB approximation for that.
    Plain color tokens and non-color CSS values keep their original meaning.
    """
    parts = value.rsplit(maxsplit=1)
    if len(parts) != 2 or not parts[1].endswith("%"):
        return value
    color_text, percentage = parts
    try:
        alpha = float(percentage[:-1]) / 100
        if not 0 <= alpha <= 1:
            return value
        if color_text != "auto" and Color.parse(color_text).ansi is not None:
            return color_text

        def rgb(text: str, *, is_background: bool) -> Color:
            color = Color.parse(text)
            if color.ansi == -1:
                return Color(0, 0, 0) if dark == is_background else Color(255, 255, 255)
            if color.ansi is not None:
                return Color(*color.rich_color.get_truecolor())
            return color

        base = rgb(background, is_background=True)
        ink = base.get_contrast_text(1) if color_text == "auto" else rgb(color_text, is_background=False)
        return base.blend(ink, ink.a * alpha, 1).hex
    except ColorParseError, ValueError:
        return value


def _readable_button_text(background: str, foreground: str) -> str:
    """Keep semantic ink unless its opaque RGB fill makes it hard to read.

    ANSI slots and explicit button overrides retain their own policies;
    this only repairs generated flat-button fallbacks.
    """
    try:
        fill = Color.parse(background)
        if foreground.startswith("auto ") and foreground.endswith("%"):
            alpha = float(foreground.removeprefix("auto ").removesuffix("%")) / 100
            ink = fill + fill.get_contrast_text(alpha)
        else:
            ink = Color.parse(foreground)
    except ColorParseError, ValueError:
        return foreground
    if fill.ansi is not None or ink.ansi is not None or fill.a != 1 or ink.a != 1:
        return foreground

    def luminance(color: Color) -> float:
        channels = (value / 255 for value in (color.r, color.g, color.b))
        linear = (value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels)
        return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    fill_luma, ink_luma = luminance(fill), luminance(ink)
    low, high = sorted((fill_luma, ink_luma))
    if (high + 0.05) / (low + 0.05) >= 3:
        return foreground
    return "#000000" if fill_luma > 0.179 else "#FFFFFF"


def _with_button_css_variables(theme: Theme, variables: dict[str, str]) -> dict[str, str]:
    """Resolve button overrides identically for live themes and editor drafts.

    The common foreground overrides semantic label defaults; a per-variant
    foreground takes precedence. Unset hover/disabled fills retain each
    variant's palette. Internal aliases let those fallbacks remain distinct
    without requiring users to configure every variant/state combination.
    """
    resolved = dict(variables)
    overrides = dict(theme.variables)
    # A shared user color must match its swatch on every button variant.
    # Only generated contrast defaults adapt separately to each fill.
    for name in (*BUTTON_COLOR_VARIABLES, "button-color-foreground"):
        if name in overrides:
            background = concrete_theme_color(
                variables.get(name.removesuffix("-foreground") + "-background", variables["background"]),
                variables["background"],
                theme.dark,
            )
            overrides[name] = concrete_theme_color(overrides[name], background, theme.dark)
            resolved[name] = overrides[name]
    if not theme.ansi and "button-color-foreground" not in overrides:
        resolved["button-color-foreground"] = "auto 100%"
    if "button-flat-foreground" in overrides:
        resolved["button-color-foreground"] = overrides["button-flat-foreground"]
    # Textual's button-color-foreground belongs to solid/raised fills. Many
    # bundled themes set dark ink there, which is unreadable on muted flat
    # fills. Keep a separate flat override, copied as ordinary theme data.
    ink = overrides.get("button-flat-foreground", resolved["button-color-foreground"] if theme.ansi else None)
    for variant in ("default", "primary", "success", "warning", "error"):
        default = variant == "default"
        prefix = "button" if default else f"button-{variant}"
        background = overrides.get(f"{prefix}-background", resolved["surface" if default else f"{variant}-muted"])
        foreground = overrides.get(
            f"{prefix}-foreground", ink or ("auto 90%" if default else resolved[f"text-{variant}"])
        )
        if ink is None and f"{prefix}-foreground" not in overrides and (default or f"text-{variant}" not in overrides):
            foreground = _readable_button_text(background, foreground)
        alias = f"tui-button-{variant}"
        resolved[f"{alias}-background"] = background
        resolved[f"{alias}-foreground"] = foreground
        resolved[f"{alias}-hover-background"] = overrides.get(
            "button-hover-background", resolved["primary" if default else variant]
        )
        resolved[f"{alias}-hover-foreground"] = overrides.get(
            "button-hover-foreground", ink or ("auto 90%" if default else resolved["text"])
        )
        if ink is None and "button-hover-foreground" not in overrides and "text" not in overrides:
            resolved[f"{alias}-hover-foreground"] = _readable_button_text(
                resolved[f"{alias}-hover-background"], resolved[f"{alias}-hover-foreground"]
            )
        resolved[f"{alias}-disabled-background"] = overrides.get("button-disabled-background", background)
        resolved[f"{alias}-disabled-foreground"] = overrides.get(
            "button-disabled-foreground", "auto 50%" if default else foreground
        )
        # Flat borders complete the colored fill; they must follow the button
        # palette, including on themes with neutral container outlines.
        for state in ("", "-hover", "-disabled"):
            resolved[f"tui-border-button-{variant}{state}"] = resolved[f"{alias}{state}-background"]
    resolved["button-active-tint"] = overrides.get("button-active-tint", f"{resolved['background']} 30%")
    return resolved


_BORDER_COLOR_VARIABLE_SOURCES: dict[str, str] = {
    "tui-border-accent": "accent",
    "tui-border-block-hover-background": "block-hover-background",
    "tui-border-control-background": "control-background",
    "tui-border-error": "error",
    "tui-border-foreground": "foreground",
    "tui-border-primary": "primary",
    "tui-border-primary-darken-2": "primary-darken-2",
    "tui-border-primary-darken-3": "primary-darken-3",
    "tui-border-secondary": "secondary",
    "tui-border-success": "success",
    "tui-border-warning": "warning",
    "tui-border-warning-darken-1": "warning-darken-1",
}
_BORDER_COLOR_LITERAL_DEFAULTS: dict[str, str] = {
    "tui-border-neutral-100": "rgb(100, 100, 100)",
    "tui-border-neutral-128": "#808080",
    "tui-border-neutral-160": "rgb(160, 160, 160)",
    "tui-border-neutral-gray": "gray",
}
_SEMANTIC_BORDER_COLOR_VARIABLE_SOURCES: dict[str, str] = {
    "tui-border-agent-message": "success",
    "tui-border-selected-turn": "primary",
    "tui-border-status-error": "error",
    "tui-border-status-warning": "warning",
    "tui-border-user-message": "accent",
}
_BORDER_TITLE_COLOR_VARIABLE_SOURCES: dict[str, str] = {
    "tui-border-title-accent": "accent",
    "tui-border-title-primary": "primary",
    "tui-border-title-warning": "warning",
}


def with_tui_css_variables(theme: Theme, variables: dict[str, str]) -> dict[str, str]:
    """Resolve chrome and button colors for live, preview and recovery paths."""
    resolved = dict(variables)
    # Resolve the roots before building dependent colors; otherwise a legacy
    # foreground like "red 40%" would become the invalid "red 40% 15%" hatch.
    background = theme.background or (
        "ansi_default" if theme.ansi else DEFAULT_DARK_BACKGROUND if theme.dark else DEFAULT_LIGHT_BACKGROUND
    )
    resolved["background"] = concrete_theme_color(resolved["background"], background, theme.dark)
    resolved["foreground"] = concrete_theme_color(resolved["foreground"], resolved["background"], theme.dark)
    # One common track color covers hover and drag too. Explicit advanced
    # per-state YAML overrides remain supported, including in ANSI mode.
    for state in ("hover", "active"):
        key = f"scrollbar-background-{state}"
        resolved[key] = theme.variables.get(key, resolved["scrollbar-background"])
    for name, source in (
        ("control-background", "surface"),
        ("control-background-muted", "surface"),
        ("control-disabled-background", "background"),
        ("control-foreground-muted", "text-disabled"),
        ("overlay-background", "panel"),
        ("markdown-block-background", "surface"),
    ):
        resolved[name] = theme.variables.get(name, resolved[source])
    resolved["hatch-color"] = theme.variables.get("hatch-color", f"{resolved['foreground']} 15%")
    # Native ANSI colors ignore alpha; opaque neutral fills keep turn text readable.
    resolved["tui-ansi-turn-background"] = "#303030" if theme.dark else "#DADADA"
    resolved["tui-ansi-turn-focus-background"] = "#444444" if theme.dark else "#C6C6C6"
    border_override = theme.variables.get("border-color")
    for alias, source in _BORDER_COLOR_VARIABLE_SOURCES.items():
        resolved[alias] = border_override or resolved[source]
    for alias, color in _BORDER_COLOR_LITERAL_DEFAULTS.items():
        resolved[alias] = border_override or color
    # Transcript chrome remains semantic when a theme uses uniform outlines.
    for alias, source in _SEMANTIC_BORDER_COLOR_VARIABLE_SOURCES.items():
        resolved[alias] = resolved[source]
    for alias, source in _BORDER_TITLE_COLOR_VARIABLE_SOURCES.items():
        resolved[alias] = resolved[source]
    resolved["tui-tool-group-title"] = theme.variables.get("tool-group-title-color", resolved["warning"])
    # A user message stands apart from the turn it opens: the foreground faintly over the background, lighter in a
    # dark theme and darker in a light one.
    resolved["tui-user-message-background"] = theme.variables.get(
        "user-message-background", f"{resolved['foreground']} 6%"
    )
    resolved = _with_button_css_variables(theme, resolved)
    # The editable palette uses concrete colors. Internal contrast roles are
    # shared across different fills (e.g. warning badges and primary buttons);
    # CSS must resolve those against the consuming control, not the app surface.
    # Active tint is a blend effect, not an editable foreground/background.
    # Making it opaque would paint over the pressed button and its label.
    contrast_roles = {"button-color-foreground", "text", *(f"text-{color}" for color in CHRYS_COLOR_NAMES)}
    return {
        name: value
        if name == "button-active-tint" or (name in contrast_roles and value.startswith("auto "))
        else concrete_theme_color(
            value,
            concrete_theme_color(
                resolved.get(name.removesuffix("-foreground") + "-background", resolved["background"]),
                resolved["background"],
                theme.dark,
            )
            if name.endswith("-foreground")
            else resolved["background"],
            theme.dark,
        )
        for name, value in resolved.items()
    }


class TuiVariableDefaultsMixin(_TuiVariableDefaultsBase):
    """Supply Chrys-specific CSS variables to lightweight Textual hosts.

    Border alpha applies to RGB-backed themes. Textual deliberately preserves
    native ANSI palette tokens as opaque, so ``ansi-dark`` and ``ansi-light``
    ignore the percentage while ``chrys-ansi`` (RGB palette, ANSI output mode)
    honors it.
    """

    def get_theme_variable_defaults(self) -> dict[str, str]:
        return TUI_VARIABLE_DEFAULTS

    def get_css_variables(self) -> dict[str, str]:
        variables = with_tui_css_variables(self.current_theme, super().get_css_variables())
        self.theme_variables = variables
        return variables


# Chrys palettes override the same semantic and control slots as user themes.
# The legacy theme draws its chrome in the accent colors; ANSI keeps that look.
_CHRYS_LEGACY_VARIABLES = {
    # RGB keeps bold button labels black even when the terminal brightens
    # ANSI black to gray (or defines its black palette entry as gray).
    "button-color-foreground": "#000000",
    "button-flat-foreground": "#000000",
    "button-disabled-background": "#303030",
    "button-disabled-foreground": "#5F5FAF",
    "button-active-tint": "transparent",
    "border": "#AF87FF",
    "border-blurred": "#5F5FAF",
    "scrollbar": "#5F5FAF",
    "scrollbar-hover": "#AF87FF",
    "scrollbar-active": "#FF87D7",
    "scrollbar-background": "#1C1C1C",
    "primary-muted": "#875FAF",
    "error-muted": "#D75F5F",
    "warning-muted": "#D7875F",
    "success-muted": "#5FD75F",
    "block-cursor-foreground": "#303030",
    "block-cursor-background": "#8787D7",
    "block-cursor-blurred-background": "#8787D7",
    "block-hover-background": "#4E4E4E",
    "footer-background": "#262626",
    "footer-foreground": "#EEEEEE",
    "user-message-background": "#262626",
    "footer-key-foreground": "#AF87FF",
    "footer-key-background": "transparent",
    "footer-description-foreground": "#D0D0D0",
    "footer-description-background": "transparent",
    "footer-item-background": "transparent",
    **CONTROL_VARIABLE_DEFAULTS,
}

# The default theme quiets that chrome down to grays.
_CHRYS_VARIABLES = {
    **_CHRYS_LEGACY_VARIABLES,
    "border-color": "#5F5F5F",
    "tool-group-title-color": "#949494",
    "scrollbar": "#585858",
    "scrollbar-hover": "#767676",
    "scrollbar-active": "#808080",
}

_CHRYS_ANSI_VARIABLES = {
    **_CHRYS_LEGACY_VARIABLES,
    "ansi-background": "ansi_black",
    "ansi-foreground": "ansi_white",
    "foreground-muted": "ansi_bright_black",
    "text-muted": "ansi_white 40%",
    "text-disabled": "ansi_bright_black",
    "markdown-h1-color": _CHRYS_COLORS["primary"],
    "markdown-h2-color": _CHRYS_COLORS["primary"],
    "markdown-h3-color": _CHRYS_COLORS["primary"],
    "markdown-h4-color": _CHRYS_COLORS["foreground"],
    "markdown-h5-color": _CHRYS_COLORS["foreground"],
    # Nearest 256-color approximations of Chrys alpha colors over _DARK_GRAY.
    # Keep these covered by tests/app/tui/behaviors/test_chrys_themes.py so chrys-ansi remains close
    # to chrys without forcing the truecolor theme onto flat approximations.
    "markdown-h6-color": "#9E9E9E",
    "input-selection-background": "#5F5F87",
    "screen-selection-background": "#5F5F87",
}

_DARK_GRAY = "#1C1C1C"

CHRYS_THEME = Theme(
    name="chrys",
    **_CHRYS_COLORS,
    background=_DARK_GRAY,
    surface=_DARK_GRAY,
    panel=_DARK_GRAY,
    boost=_DARK_GRAY,
    dark=True,
    variables=_CHRYS_VARIABLES,
)

CHRYS_LEGACY_THEME = Theme(
    name="chrys-legacy",
    **_CHRYS_COLORS,
    background=_DARK_GRAY,
    surface=_DARK_GRAY,
    panel=_DARK_GRAY,
    boost=_DARK_GRAY,
    dark=True,
    variables=_CHRYS_LEGACY_VARIABLES,
)

CHRYS_ANSI_THEME = Theme(
    name="chrys-ansi",
    ansi=True,
    **_CHRYS_COLORS,
    background="ansi_default",
    surface="ansi_default",
    panel="ansi_default",
    boost="ansi_default",
    dark=True,
    variables=_CHRYS_ANSI_VARIABLES,
)
