# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""User theme loader tests — ``<config_dir>/themes/*.yaml`` → registered Themes."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from textual.color import Color
from textual.theme import BUILTIN_THEMES, Theme

from chrys.app.tui.theme import CHRYS_THEMES
from chrys.app.tui.theme_loader import (
    load_user_themes,
    theme_name_is_valid,
)
from chrys.foundation.config.settings import RENAMED_THEMES


def _write_theme(root, name: str, body: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(body, encoding="utf-8")


def test_missing_directory_returns_nothing(tmp_path) -> None:
    themes, warnings = load_user_themes(tmp_path / "themes")

    assert themes == []
    assert warnings == []


def test_valid_theme_file_loads(tmp_path) -> None:
    _write_theme(
        tmp_path / "themes",
        "my-theme.yaml",
        """
dark: true
primary: "#AF87FF"
secondary: "#5F5FAF"
warning: "#FFAF5F"
error: "#FF5F5F"
success: "#5FFF87"
accent: "#FF87D7"
foreground: "#EEEEEE"
background: "#1C1C1C"
surface: "#1C1C1C"
panel: "#1C1C1C"
boost: "#1C1C1C"
variables:
  footer-background: "#262626"
""",
    )

    themes, warnings = load_user_themes(tmp_path / "themes")

    assert warnings == []
    assert [t.name for t in themes] == ["my-theme"]
    theme = themes[0]
    assert theme.primary == "#AF87FF"
    assert theme.dark is True
    assert theme.variables == {"footer-background": "#262626"}


def test_minimal_theme_uses_textual_defaults(tmp_path) -> None:
    _write_theme(tmp_path / "themes", "pastel.yaml", 'primary: "#875FAF"\n')

    themes, warnings = load_user_themes(tmp_path / "themes")

    assert warnings == []
    assert themes[0].name == "pastel"
    # Unset colors fall back to whatever Textual's Theme dataclass defaults to.
    assert themes[0].dark is True


def test_yml_and_yaml_both_scanned_yaml_wins(tmp_path) -> None:
    _write_theme(tmp_path / "themes", "dupe.yaml", 'primary: "#111111"\n')
    _write_theme(tmp_path / "themes", "dupe.yml", 'primary: "#222222"\n')

    themes, _warnings = load_user_themes(tmp_path / "themes")

    assert [t.name for t in themes] == ["dupe"]
    assert themes[0].primary == "#111111"


@pytest.mark.parametrize(
    ("filename", "reason_fragment"),
    [
        ("nord.yaml", "reserved"),
        ("chrys-legacy.yaml", "reserved"),
        # Retired, and still not a user theme's to take: the setting that named it is renamed on the way in.
        ("chrys-dark.yaml", "reserved"),
        ("..yaml", "Invalid theme name"),
        ("with space.yaml", "Invalid theme name"),
        ("ansi.yaml.yaml", "Invalid theme name"),
    ],
)
def test_reserved_or_invalid_names_are_skipped_with_warning(tmp_path, filename: str, reason_fragment: str) -> None:
    _write_theme(tmp_path / "themes", filename, 'primary: "#875FAF"\n')

    themes, warnings = load_user_themes(tmp_path / "themes")

    assert themes == []
    assert len(warnings) == 1
    assert reason_fragment in warnings[0].message


def test_user_ansi_theme_preserves_native_mode(tmp_path) -> None:
    _write_theme(tmp_path / "themes", "transparent.yaml", "primary: ansi_blue\nansi: true\n")

    themes, warnings = load_user_themes(tmp_path / "themes")

    assert warnings == []
    assert len(themes) == 1
    assert themes[0].ansi is True


def test_unknown_key_is_rejected(tmp_path) -> None:
    _write_theme(tmp_path / "themes", "typo.yaml", 'primary: "#875FAF"\ntotally_not_a_key: "x"\n')

    themes, warnings = load_user_themes(tmp_path / "themes")

    assert themes == []
    assert "Unknown keys" in warnings[0].message


def test_non_mapping_top_level_is_rejected(tmp_path) -> None:
    _write_theme(tmp_path / "themes", "listish.yaml", "- one\n- two\n")

    themes, warnings = load_user_themes(tmp_path / "themes")

    assert themes == []
    assert "mapping" in warnings[0].message


def test_broken_yaml_is_rejected_without_raising(tmp_path) -> None:
    _write_theme(tmp_path / "themes", "broken.yaml", "dark: [unclosed\n")

    themes, warnings = load_user_themes(tmp_path / "themes")

    assert themes == []
    assert "Invalid YAML" in warnings[0].message


def test_one_bad_file_does_not_block_the_rest(tmp_path) -> None:
    _write_theme(tmp_path / "themes", "good.yaml", 'primary: "#875FAF"\n')
    _write_theme(tmp_path / "themes", "bad.yaml", "oops: [\n")

    themes, warnings = load_user_themes(tmp_path / "themes")

    assert [t.name for t in themes] == ["good"]
    assert len(warnings) == 1


def test_theme_name_validation_rules() -> None:
    assert theme_name_is_valid("my-theme")
    assert theme_name_is_valid("Theme_2.go")
    assert not theme_name_is_valid("")
    assert not theme_name_is_valid(".hidden")
    assert not theme_name_is_valid("with space")
    assert not theme_name_is_valid("trailing ")
    assert not theme_name_is_valid("slash/inside")
    assert not theme_name_is_valid("back\\slash")
    assert not theme_name_is_valid("colon:inside")
    assert not theme_name_is_valid("..")
    assert not theme_name_is_valid("name.yaml")


def test_reserved_names_cover_builtin_and_chrys_family() -> None:
    from chrys.app.tui.theme_loader import _RESERVED_THEME_NAMES

    assert set(BUILTIN_THEMES) <= _RESERVED_THEME_NAMES
    assert CHRYS_THEMES <= _RESERVED_THEME_NAMES
    assert set(RENAMED_THEMES) <= _RESERVED_THEME_NAMES


def test_textual_theme_roundtrip_via_register(tmp_path) -> None:
    """A loaded theme registers and activates through Textual's own APIs."""
    from textual.app import App

    _write_theme(tmp_path / "themes", "roundtrip.yaml", 'primary: "#875FAF"\ndark: false\n')
    themes, warnings = load_user_themes(tmp_path / "themes")
    assert warnings == []

    class ThemeApp(App):
        pass

    app = ThemeApp()
    app.register_theme(themes[0])
    app.theme = "roundtrip"
    assert app.theme == "roundtrip"
    assert isinstance(app.get_theme("roundtrip"), Theme)


def test_theme_construction_error_becomes_warning(tmp_path) -> None:
    """A color Textual itself rejects must not crash startup."""
    _write_theme(tmp_path / "themes", "notacolor.yaml", 'primary: "definitely-not-a-color"\n')

    themes, warnings = load_user_themes(tmp_path / "themes")

    assert themes == []
    assert len(warnings) == 1


def test_null_color_value_is_rejected_not_crashed(tmp_path) -> None:
    """``primary:`` (YAML null) must become a warning, not an AttributeError."""
    _write_theme(tmp_path / "themes", "nullyaml.yaml", "primary:\n")

    themes, warnings = load_user_themes(tmp_path / "themes")

    assert themes == []
    assert len(warnings) == 1
    assert "nullyaml" in warnings[0].message


def test_non_string_top_level_keys_are_rejected_not_crashed(tmp_path) -> None:
    """A mapping like ``1: foo`` must become a warning, not a TypeError."""
    _write_theme(tmp_path / "themes", "intkey.yaml", "1: foo\n")

    themes, warnings = load_user_themes(tmp_path / "themes")

    assert themes == []
    assert len(warnings) == 1
    assert "intkey" in warnings[0].message


def test_untokenizable_variable_value_is_rejected(tmp_path) -> None:
    """A variable value that would crash Textual's CSS tokenizer at switch
    time must be rejected at load time (adversarial review finding)."""
    _write_theme(tmp_path / "themes", "badvar.yaml", 'primary: "#875FAF"\nvariables:\n  footer-background: "["\n')

    themes, warnings = load_user_themes(tmp_path / "themes")

    assert themes == []
    assert len(warnings) == 1
    assert "footer-background" in warnings[0].message


def test_legal_non_color_variable_values_are_accepted(tmp_path) -> None:
    """Textual themes legally use non-color variable spellings; the
    tokenizer-based validation must not reject them."""
    _write_theme(
        tmp_path / "themes",
        "legalvars.yaml",
        'primary: "#875FAF"\nvariables:\n  text-muted: "ansi_white 40%"\n  footer-key-background: "transparent"\n  block-cursor-text-style: "none"\n  input-selection-background: "#81a1c1 35%"\n',
    )

    themes, warnings = load_user_themes(tmp_path / "themes")

    assert warnings == []
    assert [t.name for t in themes] == ["legalvars"]


def test_warnings_are_publishable_warning_events(tmp_path) -> None:
    """Loader warnings must be bus-publishable Warning events with a
    display_message, not bare strings (adversarial review finding)."""
    from chrys.foundation.events.types import Warning as WarningEvent
    from chrys.foundation.i18n import MessageRef

    _write_theme(tmp_path / "themes", "broken.yaml", "dark: [unclosed\n")

    _themes, warnings = load_user_themes(tmp_path / "themes")

    assert len(warnings) == 1
    warning = warnings[0]
    assert isinstance(warning, WarningEvent)
    assert warning.code == "user_theme_skipped"
    assert isinstance(warning.display_message, MessageRef)


def test_user_theme_registered_by_chrys_app_appears_in_available_themes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.config.settings_store import LoadedSettings
    from tests.app.tui.behaviors.test_app import _app_with_loaded_settings

    themes_dir = tmp_path / "platform-config" / "themes"
    themes_dir.mkdir(parents=True)
    (themes_dir / "user-pick.yaml").write_text(
        'dark: true\nprimary: "#875FAF"\nsecondary: "#5F5FAF"\nbackground: "#1C1C1C"\n',
        encoding="utf-8",
    )

    app = _app_with_loaded_settings(tmp_path, LoadedSettings(settings=Settings(theme="user-pick"), provenance={}))

    assert "user-pick" in app.available_themes
    assert app.theme == "user-pick"
    assert "-chrys" not in app.classes


def test_user_theme_prefix_does_not_add_styling_classes(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.config.settings_store import LoadedSettings
    from tests.app.tui.behaviors.test_app import _app_with_loaded_settings

    themes_dir = tmp_path / "platform-config" / "themes"
    themes_dir.mkdir(parents=True, exist_ok=True)
    (themes_dir / "chrys-custom.yaml").write_text(
        'dark: true\nprimary: "#875FAF"\nsecondary: "#5F5FAF"\nbackground: "#1C1C1C"\n',
        encoding="utf-8",
    )

    app = _app_with_loaded_settings(tmp_path, LoadedSettings(settings=Settings(theme="chrys-custom"), provenance={}))

    assert "chrys-custom" in app.available_themes
    assert app.theme == "chrys-custom"
    assert "-chrys" not in app.classes
    assert "-chrys-ansi" not in app.classes


def test_bad_user_theme_surfaces_as_startup_warning_not_crash(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.config.settings_store import LoadedSettings
    from tests.app.tui.behaviors.test_app import _app_with_loaded_settings

    themes_dir = tmp_path / "platform-config" / "themes"
    themes_dir.mkdir(parents=True)
    (themes_dir / "broken.yaml").write_text("dark: [unclosed\n", encoding="utf-8")

    app = _app_with_loaded_settings(tmp_path, LoadedSettings(settings=Settings(), provenance={}))

    assert app.theme == "chrys"
    assert len(app._startup_warnings) == 1
    assert "broken.yaml" in app._startup_warnings[0].message


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "utf-32", "gb18030"])
def test_user_theme_detects_encoding(tmp_path: Path, encoding: str) -> None:
    text = '# 这是用户自定义主题。保留中文注释并正确加载颜色设置。\nprimary: "#875FAF"\n'
    (tmp_path / "encoded.yaml").write_bytes(text.encode(encoding))

    themes, warnings = load_user_themes(tmp_path)

    assert warnings == []
    assert [theme.name for theme in themes] == ["encoded"]
    assert themes[0].primary == "#875FAF"


@pytest.mark.parametrize("content", [b'primary: "#875FAF"\n# \xff\n', 'primary: "#875FAF"\n'.encode("utf-16")])
def test_decodable_non_utf8_theme_does_not_block_loading(tmp_path: Path, content: bytes) -> None:
    _write_theme(tmp_path, "good.yaml", 'primary: "#875FAF"\n')
    (tmp_path / "legacy.yaml").write_bytes(content)

    themes, warnings = load_user_themes(tmp_path)

    assert [theme.name for theme in themes] == ["good", "legacy"]
    assert warnings == []


def test_damaged_utf8_theme_is_skipped(tmp_path: Path) -> None:
    _write_theme(tmp_path, "good.yaml", 'primary: "#875FAF"\n')
    damaged = ("# 中文主题说明\n" * 20).encode() + b'primary: "#875FAF"\n# \xff\n'
    (tmp_path / "damaged.yaml").write_bytes(damaged)

    themes, warnings = load_user_themes(tmp_path)

    assert [theme.name for theme in themes] == ["good"]
    assert len(warnings) == 1
    assert warnings[0].code == "user_theme_skipped"
    assert "damaged.yaml" in warnings[0].message


@pytest.mark.parametrize("value", ["2026-02-30", "2026-13-01", "!!int not-a-number", "!!timestamp not-a-date"])
def test_yaml_constructor_error_does_not_block_other_files(tmp_path: Path, value: str) -> None:
    _write_theme(tmp_path, "good.yaml", 'primary: "#875FAF"\n')
    _write_theme(tmp_path, "broken.yaml", f"primary: {value}\n")

    themes, warnings = load_user_themes(tmp_path)

    assert [theme.name for theme in themes] == ["good"]
    assert len(warnings) == 1
    assert "Invalid YAML" in warnings[0].message
    assert "broken.yaml" in warnings[0].message


@pytest.mark.parametrize("key", ["dark", "ansi", "luminosity_spread", "text_alpha"])
def test_null_scalar_is_rejected(tmp_path: Path, key: str) -> None:
    _write_theme(tmp_path, "broken.yaml", f'primary: "#875FAF"\n{key}: null\n')

    themes, warnings = load_user_themes(tmp_path)

    assert themes == []
    assert len(warnings) == 1
    assert key in warnings[0].message


@pytest.mark.parametrize("key", ["luminosity_spread", "text_alpha"])
@pytest.mark.parametrize("value", [".nan", ".inf", "-.inf", "true", '"0.15"'])
def test_non_finite_or_non_numeric_scalar_is_rejected(tmp_path: Path, key: str, value: str) -> None:
    _write_theme(tmp_path, "broken.yaml", f'primary: "#875FAF"\n{key}: {value}\n')

    themes, warnings = load_user_themes(tmp_path)

    assert themes == []
    assert len(warnings) == 1
    assert key in warnings[0].message


def test_css_values_are_left_to_the_live_stylesheet(tmp_path: Path) -> None:
    _write_theme(
        tmp_path, "custom.yaml", "primary: red\nvariables:\n  footer-background: initial\n  text-primary: auto\n"
    )

    themes, warnings = load_user_themes(tmp_path)

    assert warnings == []
    assert themes[0].variables == {"footer-background": "initial", "text-primary": "auto"}


@pytest.mark.parametrize("value", ["0", "1", "0.15"])
def test_finite_numeric_scalars_remain_usable(tmp_path: Path, value: str) -> None:
    _write_theme(
        tmp_path,
        "valid.yaml",
        f'primary: "#875FAF"\ndark: false\nansi: false\nluminosity_spread: {value}\ntext_alpha: {value}\n',
    )

    themes, warnings = load_user_themes(tmp_path)

    assert warnings == []
    assert themes[0].to_color_system().generate()


def test_other_css_value_domains_remain_supported(tmp_path: Path) -> None:
    _write_theme(
        tmp_path,
        "valid.yaml",
        'primary: "#875FAF"\nvariables:\n  link-style-hover: "bold not underline"\n'
        '  border-opacity: "50%"\n  custom-spacing: "1 2"\n  link-background: "initial"\n'
        '  footer-background: "initial"\n',
    )

    themes, warnings = load_user_themes(tmp_path)

    assert warnings == []
    assert themes[0].variables["custom-spacing"] == "1 2"


@pytest.mark.parametrize("name", [name for name, theme in BUILTIN_THEMES.items() if not theme.ansi])
def test_builtin_rgb_palettes_remain_loadable_as_user_themes(tmp_path: Path, name: str) -> None:
    import yaml

    data = {key: value for key, value in asdict(BUILTIN_THEMES[name]).items() if key != "name" and value is not None}
    _write_theme(tmp_path, "custom.yaml", yaml.safe_dump(data))

    themes, warnings = load_user_themes(tmp_path)

    assert warnings == []
    assert themes[0].variables == BUILTIN_THEMES[name].variables
    assert themes[0].to_color_system().generate()


@pytest.mark.parametrize(
    "content",
    [
        b'primary: "#875FAF"\n# \x00\n',
        ("# 中文主题说明\n" * 20).encode() + b'primary: "#875FAF"\n# \xff\n',
        'primary: "#875FAF"\n'.encode("utf-16")[:-1],
        b'primary: "#875FAF"\ndark: null\n',
        b'primary: "#875FAF"\nluminosity_spread: null\n',
        b'primary: "#875FAF"\nluminosity_spread: .nan\n',
        b"primary: 2026-02-30\n",
    ],
    ids=["control-character", "damaged-utf8", "truncated-utf16", "null-dark", "null-spread", "nan-spread", "yaml-date"],
)
async def test_invalid_saved_theme_falls_back_and_valid_theme_can_be_applied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: bytes
) -> None:
    from textual.app import ComposeResult
    from textual.screen import Screen
    from textual.widgets import Footer, Input

    from chrys.app.tui.app import ChrysApp
    from chrys.foundation.config.settings import Settings, persist_theme
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import Warning
    from chrys.service.state.store import JsonFileStateStore
    from tests.support.paths import SRC_ROOT
    from tests.support.tui_app_harness import EmptyAgentRegistry, ShutdownOnlyEngine
    from tests.support.waiting import wait_for

    class ThemeScreen(Screen):
        def compose(self) -> ComposeResult:
            yield Input()
            yield Footer()

    class ThemeApp(ChrysApp):
        CSS_PATH = SRC_ROOT / "chrys" / "app" / "tui" / "chrys.tcss"

        def _build_main_screen(self) -> Screen:
            return ThemeScreen()

    themes_dir = tmp_path / "platform-config" / "themes"
    _write_theme(
        themes_dir,
        "good.yaml",
        'primary: "#875FAF"\nvariables:\n  footer-background: "#123456"\n  text-muted: "auto 60%"\n'
        '  block-cursor-text-style: "none"\n  footer-key-background: "transparent"\n',
    )
    (themes_dir / "broken.yaml").write_bytes(content)
    persisted = create_autospec(persist_theme)
    monkeypatch.setattr("chrys.app.tui.app.persist_theme", persisted)
    bus = EventBus()
    published: list[Warning] = []

    async def record_warning(event: Warning) -> None:
        published.append(event)

    await bus.subscribe(Warning, record_warning)
    app = ThemeApp(
        bus,
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        settings=Settings(theme="broken"),
        state_store=JsonFileStateStore(tmp_path / "state"),
        agent_registry=EmptyAgentRegistry(),  # type: ignore[arg-type]
        gc_freeze_enabled=False,
    )
    async with app.run_test() as pilot:
        await wait_for(lambda: bool(published), pilot=pilot)
        assert app.theme == "chrys"
        assert "broken" not in app.available_themes
        assert [warning.code for warning in published] == ["user_theme_skipped"]
        persisted.assert_not_called()

        app.apply_theme_setting("good")
        await wait_for(lambda: app.screen.query_one(Footer).styles.background == Color.parse("#123456"), pilot=pilot)
        assert app.theme == "good"
        persisted.assert_called_once_with("good")
