# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""User theme failures recover at the live stylesheet boundary, which also keeps parsed rules cached."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, create_autospec

import pytest
import yaml
from textual.app import ComposeResult
from textual.color import Color
from textual.css import stylesheet as textual_stylesheet
from textual.css.parse import parse as parse_css
from textual.css.stylesheet import Stylesheet, StylesheetParseError
from textual.screen import Screen
from textual.widgets import Button, Footer, Input, Static

from chrys.app.tui.app import ChrysApp
from chrys.app.tui.screens.dialogs.editor import EditorDialog
from chrys.app.tui.screens.themes import ThemesScreen
from chrys.app.tui.theme_stylesheet import ThemeStylesheet
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.editor import EditorBufferSnapshot, MessageEditor
from chrys.foundation.config.settings import Settings, persist_theme
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import Warning
from chrys.service.state.store import JsonFileStateStore
from tests.app.tui.screens.themes.helpers import wait_for_themes
from tests.support.paths import SRC_ROOT
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import EmptyAgentRegistry, ShutdownOnlyEngine, make_chrys_app
from tests.support.waiting import wait_for

# These tests count what Textual tokenizes through ThemeStylesheet's own rule cache.
pytestmark = pytest.mark.real_css_parse


class ThemeScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Input()
        yield Footer()


class ThemeApp(ChrysApp):
    CSS_PATH = SRC_ROOT / "chrys" / "app" / "tui" / "chrys.tcss"

    def _build_main_screen(self) -> Screen:
        return ThemeScreen()


def _app(tmp_path: Path, variables: dict[str, str], *, saved: str = "custom") -> ThemeApp:
    directory = tmp_path / "platform-config" / "themes"
    directory.mkdir(parents=True)
    (directory / "custom.yaml").write_text(yaml.safe_dump({"primary": "red", "variables": variables}), encoding="utf-8")
    (directory / "good.yaml").write_text('primary: red\nvariables:\n  footer-background: "#123456"\n', encoding="utf-8")
    return ThemeApp(
        EventBus(),
        ShutdownOnlyEngine(),  # type: ignore[arg-type]
        settings=Settings(theme=saved),
        state_store=JsonFileStateStore(tmp_path / "state"),
        agent_registry=EmptyAgentRegistry(),  # type: ignore[arg-type]
        gc_freeze_enabled=False,
    )


@pytest.mark.parametrize("saved", ["custom", "chrys"])
@pytest.mark.parametrize("variables", [{"footer-background": "not-a-color"}, {"background": "initial"}])
async def test_invalid_css_recovers_at_startup_or_theme_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved: str, variables: dict[str, str]
) -> None:
    persisted = create_autospec(persist_theme)
    monkeypatch.setattr("chrys.app.tui.app.persist_theme", persisted)
    app = _app(tmp_path, variables, saved=saved)
    warnings: list[Warning] = []

    async def record_warning(event: Warning) -> None:
        warnings.append(event)

    await app._bus.subscribe(Warning, record_warning)

    async with app.run_test() as pilot:
        if saved == "chrys":
            app.apply_theme_setting("custom")
        await wait_for(lambda: len(warnings) == 1 and app.theme == "chrys", pilot=pilot)
        assert warnings[0].code == "user_theme_skipped"
        assert not app.has_class("-chrys")
        assert app._settings.theme == "custom"
        assert all(call.args != ("chrys",) for call in persisted.call_args_list)
        persisted.reset_mock()

        app.apply_theme_setting("good")
        await wait_for(lambda: app.screen.query_one(Footer).styles.background == Color.parse("#123456"), pilot=pilot)
        persisted.assert_called_once_with("good")


@pytest.mark.parametrize("target", ["button", "chat", "editor"])
async def test_lazily_loaded_styles_recover_without_losing_ui_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    variables = {
        # Flat fill colors now appear in the global stylesheet. Focus text
        # style is still introduced only when Button's own CSS is loaded.
        "button": {"button-focus-text-style": "not-a-style"},
        "chat": {"hatch-color": "not-a-color"},
        "editor": {"ansi-foreground": "auto"},
    }[target]
    persisted = create_autospec(persist_theme)
    monkeypatch.setattr("chrys.app.tui.app.persist_theme", persisted)
    app = _app(tmp_path, variables)
    warnings: list[Warning] = []

    async def record_warning(event: Warning) -> None:
        warnings.append(event)

    await app._bus.subscribe(Warning, record_warning)

    async with app.run_test() as pilot:
        assert app.theme == "custom"
        assert warnings == []
        draft = app.screen.query_one(Input)
        draft.value = "keep this draft"
        if target == "editor":
            await app.push_screen(EditorDialog(EditorBufferSnapshot(draft.value, (0, 4))))
        else:
            await app.screen.mount(Button("OK") if target == "button" else ChatPanel())
        await wait_for(lambda: app.theme == "chrys" and len(warnings) == 1, pilot=pilot)
        assert draft.value == "keep this draft"
        if target == "editor":
            assert app.screen.query_one(MessageEditor).text == draft.value
        persisted.assert_not_called()


async def test_application_css_errors_are_not_hidden(tmp_path: Path) -> None:
    app = _app(tmp_path, {"footer-background": "initial"})
    async with app.run_test():
        # CSS hot reload works on a copy. It must preserve recovery, while an
        # error that also fails with the default theme must still propagate.
        stylesheet = app.stylesheet.copy()
        stylesheet.add_source("* { height: red; }")
        with pytest.raises(StylesheetParseError):
            stylesheet.parse()
        assert app.theme == "custom"
        assert app._startup_warnings == []


async def test_stylesheet_copy_keeps_theme_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chrys.app.tui.app.persist_theme", create_autospec(persist_theme))
    app = _app(tmp_path, {"button-focus-text-style": "not-a-style"})
    async with app.run_test():
        assert app.theme == "custom"
        stylesheet = app.stylesheet.copy()
        stylesheet.add_source("* { text-style: $button-focus-text-style; }")
        stylesheet.parse()
        assert app.theme == "chrys"
        assert stylesheet.rules


def _no_theme_recovery(_stylesheet: Stylesheet, error: Exception) -> bool:
    pytest.fail(f"valid CSS needed theme recovery: {error}")


def _spy_on_tokenizing(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Record every source Textual tokenizes; a source served from the rule cache is never tokenized."""
    spy = create_autospec(parse_css, side_effect=parse_css)
    monkeypatch.setattr(textual_stylesheet, "parse", spy)
    return spy


def test_parse_past_textual_rule_cache_size_tokenizes_only_the_new_source(monkeypatch: pytest.MonkeyPatch) -> None:
    # Textual's rule cache holds 64 sources; the walk over 100 must still find each unchanged one.
    stylesheet = ThemeStylesheet(variables={}, recover=_no_theme_recovery)
    for index in range(100):
        stylesheet.add_source(
            f"#probe-{index} {{ width: {index + 1}; }}",
            read_from=("probe.py", f"Probe{index}.DEFAULT_CSS"),
            is_default_css=True,
        )
    stylesheet.parse()
    added = ("probe.py", "Added.DEFAULT_CSS")
    stylesheet.add_source("#added { width: 1; }", read_from=added, is_default_css=True)
    tokenized = _spy_on_tokenizing(monkeypatch)

    stylesheet.parse()

    assert [call.args[2] for call in tokenized.call_args_list] == [added]
    assert len(stylesheet.rules) == 101


class CssCacheProbe(Static):
    DEFAULT_CSS = """
    CssCacheProbe {
        width: 7;
    }
    """


class CssCacheProbeScreen(Screen):
    CSS = """
    CssCacheProbeScreen {
        background: red;
    }
    """


@pytest.mark.parametrize("first", ["widget", "screen"])
async def test_first_widget_or_screen_of_a_type_tokenizes_only_its_own_css(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    """Once the live stylesheet outgrows Textual's rule cache, new CSS must not re-tokenize every source."""
    app = make_chrys_app(tmp_path)
    async with app.run_test() as pilot:
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot, description="startup screen settled")
        # Workflow mode and its dialogs take the app past Textual's 64 sources; stand in for them.
        for index in range(64):
            app.stylesheet.add_source(
                f"#css-cache-filler-{index} {{ width: 1; }}",
                read_from=("filler.py", f"Filler{index}.DEFAULT_CSS"),
                is_default_css=True,
            )
        app.stylesheet.parse()
        existing = set(app.stylesheet.source)
        tokenized = _spy_on_tokenizing(monkeypatch)

        if first == "widget":
            await app.screen.mount(CssCacheProbe())
            added = "CssCacheProbe.DEFAULT_CSS"
        else:
            screen = CssCacheProbeScreen()
            await app.push_screen(screen)
            await wait_for(lambda: app.screen is screen and screen.is_mounted, pilot=pilot)
            added = "CssCacheProbeScreen.CSS"

        sources = [call.args[2] for call in tokenized.call_args_list]
        assert [source for source in sources if source[1] == added] != []
        assert [source for source in sources if source in existing] == []


@pytest.mark.parametrize(("saved", "key"), [("custom", "enter"), ("custom", "escape"), ("chrys", "enter")])
async def test_theme_picker_accepts_or_cancels_without_moving_highlight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved: str, key: str
) -> None:
    directory = tmp_path / "platform-config" / "themes"
    directory.mkdir(parents=True)
    (directory / "custom.yaml").write_text(
        "primary: red\nvariables:\n  footer-background: not-a-color\n", encoding="utf-8"
    )
    persisted = create_autospec(persist_theme)
    monkeypatch.setattr("chrys.app.tui.app.persist_theme", persisted)
    app = make_chrys_app(tmp_path, settings=Settings(theme=saved))

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: app.theme == "chrys", pilot=pilot)
        persisted.assert_not_called()
        assert app._settings.theme == saved

        await pilot.press("f9")
        await wait_for_themes(pilot)
        await pilot.press(key)
        await wait_for(lambda: not isinstance(app.screen, ThemesScreen), pilot=pilot)

        if key == "enter" and saved != "chrys":
            await wait_for(lambda: app._settings.theme == "chrys", pilot=pilot)
            persisted.assert_called_once_with("chrys")
        else:
            assert app._settings.theme == saved
            persisted.assert_not_called()


async def test_theme_picker_escape_restores_original_after_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    persisted = create_autospec(persist_theme)
    monkeypatch.setattr("chrys.app.tui.app.persist_theme", persisted)
    app = make_chrys_app(tmp_path, settings=Settings(theme="chrys"))

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.press("f9")
        await wait_for_themes(pilot)
        await pilot.press("down")
        await wait_for(lambda: app.theme != "chrys", pilot=pilot)

        await pilot.press("escape")
        await wait_for(lambda: not isinstance(app.screen, ThemesScreen) and app.theme == "chrys", pilot=pilot)
        assert app._settings.theme == "chrys"
        # Browsing and cancelling must leave the saved choice untouched.
        persisted.assert_not_called()


@pytest.mark.parametrize("broken", [False, True], ids=["missing-theme", "invalid-css"])
async def test_theme_slash_command_saves_the_showing_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: bool
) -> None:
    if broken:
        directory = tmp_path / "platform-config" / "themes"
        directory.mkdir(parents=True)
        (directory / "custom.yaml").write_text(
            "primary: red\nvariables:\n  footer-background: not-a-color\n", encoding="utf-8"
        )
    persisted = create_autospec(persist_theme)
    monkeypatch.setattr("chrys.app.tui.app.persist_theme", persisted)
    app = make_chrys_app(tmp_path, settings=Settings(theme="custom"))

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: app.theme == "chrys" and bool(app.screen.query(InputBar)), pilot=pilot)
        assert app._settings.theme == "custom"
        persisted.assert_not_called()

        input_bar = app.screen.query_one(InputBar)
        input_bar.value = "/theme chrys"
        await input_bar.action_submit()
        await wait_for(lambda: app._settings.theme == "chrys", pilot=pilot)
        assert app.theme == "chrys"
        persisted.assert_called_once_with("chrys")


async def test_distinct_invalid_themes_each_notify_but_repeated_failure_is_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "platform-config" / "themes"
    directory.mkdir(parents=True)
    for name in ("bad-a", "bad-b"):
        (directory / f"{name}.yaml").write_text(
            "primary: red\nvariables:\n  footer-background: not-a-color\n", encoding="utf-8"
        )
    app = make_chrys_app(tmp_path, settings=Settings(theme="chrys"))
    warnings: list[Warning] = []

    async def record_warning(event: Warning) -> None:
        warnings.append(event)

    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: bool(app.screen.query(InputBar)), pilot=pilot)
        notify = create_autospec(app.screen.notify)
        monkeypatch.setattr(app.screen, "notify", notify)
        # Subscribe after MainScreen so an observed event has passed UI deduplication.
        await app._bus.subscribe(Warning, record_warning)
        for count, name in enumerate(("bad-a", "bad-b", "bad-a"), start=1):
            app.apply_theme_setting(name)
            await wait_for(lambda count=count: app.theme == "chrys" and len(warnings) == count, pilot=pilot)
            assert name in warnings[-1].message
            assert notify.call_count == min(count, 2)
