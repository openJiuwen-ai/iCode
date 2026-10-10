# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Loader for user-registered TUI themes (``<config_dir>/themes/*.yaml``).

Each ``<name>.yaml`` becomes a Textual ``Theme`` registered alongside the
built-ins, so every surface that lists ``App.available_themes`` (theme picker,
``/theme``, Settings panel) picks it up without further wiring. A user theme
cannot shadow a built-in, and a malformed file costs one warning, not startup.
"""

from __future__ import annotations

import logging
import math
import re
from pathlib import Path
from typing import Any

import yaml
from textual.color import Color, ColorParseError
from textual.css.tokenize import tokenize_values
from textual.theme import BUILTIN_THEMES, Theme

from chrys.app.tui.theme import CHRYS_THEMES
from chrys.foundation.config.settings import RENAMED_THEMES
from chrys.foundation.events.types import Warning
from chrys.foundation.i18n import msg
from chrys.foundation.platform import get_platform
from chrys.foundation.text.encoding import decode_bytes

logger = logging.getLogger(__name__)

_USER_THEME_SKIPPED = msg(
    "tui.theme_loader.skipped",
    fallback="Skipped user theme {path}: {reason}",
)

# Theme names that always resolve without a user file; user files never displace them.
# A retired name resolves too — to the theme that has it now — and a user theme
# taking one could never be selected: the setting would be renamed on the way in.
_RESERVED_THEME_NAMES = frozenset(BUILTIN_THEMES) | frozenset(CHRYS_THEMES) | frozenset(RENAMED_THEMES)
_RESERVED_THEME_NAMES_CASEFOLDED = frozenset(name.casefold() for name in _RESERVED_THEME_NAMES)

# The name doubles as the ``<name>.yaml`` stem it came from, so separators,
# drives, and traversal are all out by construction. UserThemeStore.validate_name
# additionally rejects Windows device names before creating a file.
_THEME_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_COLOR_KEYS = (
    "primary",
    "secondary",
    "warning",
    "error",
    "success",
    "accent",
    "foreground",
    "background",
    "surface",
    "panel",
    "boost",
)
_BOOL_KEYS = ("dark", "ansi")
_FLOAT_KEYS = ("luminosity_spread", "text_alpha")
_KNOWN_KEYS = frozenset(_COLOR_KEYS + _BOOL_KEYS + _FLOAT_KEYS + ("variables",))


class UserThemeLoadError(Exception):
    """Raised for a single theme file that cannot become a usable Theme."""


def default_theme_directory() -> Path:
    """``<config_dir>/themes`` — the single root user themes are scanned from."""
    return get_platform().config_dir / "themes"


def theme_name_is_valid(name: str) -> bool:
    """Return True when ``name`` can serve as a ``<name>.yaml`` stem."""
    if not name or name.strip() != name or name in {".", ".."} or name.startswith("."):
        return False
    if not _THEME_NAME_RE.match(name):
        return False
    return not name.lower().endswith((".yaml", ".yml"))


def theme_is_read_only(name: str) -> bool:
    """Built-in names are protected on case-sensitive and insensitive filesystems."""
    return name.casefold() in _RESERVED_THEME_NAMES_CASEFOLDED


def _load_theme_data(path: Path) -> dict[str, Any]:
    try:
        text = decode_bytes(path.read_bytes(), errors="strict")
    except (OSError, UnicodeError) as e:
        raise UserThemeLoadError(f"Cannot read theme file {path}: {e}") from e
    try:
        data = yaml.safe_load(text)
    except Exception as e:
        # PyYAML's scalar constructors also raise errors outside YAMLError
        # (e.g. ValueError for an impossible date, AttributeError for an
        # invalid explicit timestamp). Keep those inside the per-file boundary.
        raise UserThemeLoadError(f"Invalid YAML in {path}: {e}") from e
    if not isinstance(data, dict):
        raise UserThemeLoadError(f"Theme file {path} must contain a mapping at the top level")
    return data


def _theme_from_data(name: str, data: dict[str, Any], path: Path) -> Theme:
    unknown = {key for key in data if not isinstance(key, str) or key not in _KNOWN_KEYS}
    if unknown:
        listed = ", ".join(sorted(repr(key) for key in unknown))
        raise UserThemeLoadError(f"Unknown keys in theme {path}: {listed}")
    for key in _COLOR_KEYS:
        value = data.get(key)
        if value is not None and not isinstance(value, str):
            raise UserThemeLoadError(f"Theme {path}: '{key}' must be a color string")
    for key in _BOOL_KEYS:
        if key in data and not isinstance(data[key], bool):
            raise UserThemeLoadError(f"Theme {path}: '{key}' must be a boolean")
    for key in _FLOAT_KEYS:
        if key not in data:
            continue
        value = data[key]
        try:
            finite = isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise UserThemeLoadError(f"Theme {path}: '{key}' must be a finite number")
    variables = data.get("variables", {})
    if not isinstance(variables, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in variables.items()
    ):
        raise UserThemeLoadError(f"Theme {path}: 'variables' must be a mapping of strings to strings")
    # Theme() stores colors as strings and only parses them when the theme is
    # applied — a bad value would crash the picker preview instead of load.
    # Validate with Textual's own parser so what registers is what renders.
    # Color.parse raises AttributeError (not ColorParseError) for non-str
    # input like ``primary:`` (None), so catch both.
    for key in _COLOR_KEYS:
        if key in data:
            try:
                Color.parse(data[key])
            except (ColorParseError, AttributeError, TypeError) as e:
                raise UserThemeLoadError(f"Theme {path}: invalid color for '{key}': {data[key]!r}") from e
    # Variables are tokenized as CSS value tokens when the theme is applied;
    # an untokenizable value would crash the stylesheet refresh (TokenError
    # terminates the app). Pre-validate with the same tokenizer Textual uses,
    # so legal non-color spellings like ``ansi_white 40%`` or ``none`` pass.
    for var_key, var_value in variables.items():
        try:
            tokenize_values({var_key: var_value})
        except Exception as e:
            raise UserThemeLoadError(f"Theme {path}: invalid CSS value for variable '{var_key}': {var_value!r}") from e
    kwargs = {key: data[key] for key in _COLOR_KEYS + _BOOL_KEYS + _FLOAT_KEYS if key in data}
    try:
        theme = Theme(name=name, variables=variables, **kwargs)
        # Construction alone does not exercise derived shade arithmetic or
        # ColorSystem's parsing of variable overrides used by other colors.
        theme.to_color_system().generate()
    except Exception as e:
        raise UserThemeLoadError(f"Invalid theme {path}: {e}") from e
    return theme


def user_theme_warning(path: Path | str, reason: str) -> Warning:
    """Build the shared warning for a rejected file or an unusable live theme."""
    display = _USER_THEME_SKIPPED.bind(path=str(path), reason=reason)
    return Warning(code="user_theme_skipped", message=reason, display_message=display)


def load_user_themes(directory: Path | None = None) -> tuple[list[Theme], list[Warning]]:
    """Discover and parse every user theme under *directory*.

    Returns ``(themes, warnings)``; per-file failures become warning events
    (publishable on the bus) and the file is skipped, mirroring how an
    invalid agent profile behaves.
    """
    root = directory if directory is not None else default_theme_directory()
    themes: list[Theme] = []
    warnings: list[Warning] = []
    if not root.is_dir():
        return themes, warnings
    seen: set[str] = set()
    paths = sorted(root.glob("*.yaml")) + sorted(root.glob("*.yml"))
    for path in paths:
        name = path.name[: -len(path.suffix)]
        try:
            if not theme_name_is_valid(name):
                raise UserThemeLoadError(f"Invalid theme name {name!r} (from {path.name})")
            if theme_is_read_only(name):
                raise UserThemeLoadError(f"Theme {name!r} is reserved and cannot be overridden")
            if name in seen:
                # Same stem with .yaml and .yml: first (sorted) file wins.
                continue
            theme = _theme_from_data(name, _load_theme_data(path), path)
        except UserThemeLoadError as e:
            warnings.append(user_theme_warning(path, str(e)))
            logger.warning("Skipping user theme file %s: %s", path, e)
            continue
        seen.add(name)
        themes.append(theme)
    return themes, warnings
