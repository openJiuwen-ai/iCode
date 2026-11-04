# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the Basic, Compaction, and Tools agent configuration panels."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from textual.css.query import NoMatches
from textual.widgets import (
    Checkbox,
    Label,
    TextArea,
)

from chrys.app.tui.screens.agents.panels.basic import BasicConfigPanel
from chrys.app.tui.screens.agents.panels.compaction import CompactionConfigPanel
from chrys.app.tui.screens.agents.panels.tools import ToolsConfigPanel
from chrys.service.profiles.agents.schema import (
    CompactionConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.app.tui.screens._agent_config_support import (
    _DEFAULT_WAIT_TIMEOUT,
    _registry,
    _wait_for_selectors,
)
from tests.support.tui_helpers import WidgetApp
from tests.support.waiting import wait_for

# The polling helpers in _agent_config_support share one 45s ceiling, below
# the repository's 60s default per-test timeout. This module retains a 120s
# override for expensive screen mounting before the first wait; once polling
# starts, the helper deadline still produces a clean AssertionError before the
# thread timeout can kill the xdist worker and surface only "worker gwN crashed".
# No ``isolated_chrys_config_dir``: nothing here reads the platform record, and
# the autouse ``_isolate_platform_config_dir`` in tests/conftest.py already pins
# the config directory for every test. The three agent-config modules opt into
# that fixture because they assert against ``tmp_path`` itself, which this one
# never touches.
pytestmark = [pytest.mark.timeout(120)]


def test_basic_get_config_preserves_seed_before_children_mount() -> None:
    profile = copy.deepcopy(_registry().get("Code"))
    assert profile is not None
    panel = BasicConfigPanel(profile)

    cfg = panel.get_config()

    assert cfg["name"] == profile.name
    assert cfg["display_name"] == profile.display_name
    assert cfg["description"] == profile.description
    assert cfg["sub_agent_only"] == profile.sub_agent_only
    assert cfg["model_profile_id"] == ""


def test_basic_get_config_uses_mounted_model_select_when_checkbox_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = copy.deepcopy(_registry().get("Code"))
    assert profile is not None
    panel = BasicConfigPanel(profile)

    def query_one(selector: str, *_args: object, **_kwargs: object) -> object:
        if selector == "#bc-model-profile":
            return SimpleNamespace(value="live-model-profile")
        raise NoMatches(f"No nodes match {selector!r}")

    monkeypatch.setattr(panel, "query_one", query_one)

    cfg = panel.get_config()

    assert cfg["model_profile_id"] == "live-model-profile"


async def test_basic_sub_agent_only_description_matches_option_format() -> None:
    profile = copy.deepcopy(_registry().get("Code"))
    assert profile is not None

    app = WidgetApp(lambda: BasicConfigPanel(profile))
    async with app.run_test(size=(46, 40)) as pilot:
        await pilot.pause()

        desc = app.query_one(".bc-option-desc", Label)
        await wait_for(
            lambda: desc.size.height > 1,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="sub-agent-only description wrap",
        )

        assert desc.render().plain == (
            "Focused helper agent called by other agents; cannot be selected as the main agent"
        )


async def test_basic_active_model_profile_name_is_literal_checkbox_text() -> None:
    profile = copy.deepcopy(_registry().get("Code"))
    assert profile is not None
    model_name = "fast [type=missing, input_value={}, input_type=dict])"
    model_registry = ModelProfileRegistry()
    model_registry.register(ModelProfile(id="model-markup", name=model_name, provider="mock", model_id="mock"))

    app = WidgetApp(lambda: BasicConfigPanel(profile, model_registry=model_registry, active_profile_id="model-markup"))
    async with app.run_test(size=(80, 40)) as pilot:
        checkbox = pilot.app.query_one("#bc-model-use-active", Checkbox)
        assert checkbox.label.plain == f"Use active model profile (Current: {model_name})"

        checkbox.value = False
        await pilot.pause()
        checkbox.value = True
        await pilot.pause()

        assert checkbox.label.plain == f"Use active model profile (Current: {model_name})"


def test_compaction_get_config_preserves_supplement_before_children_mount() -> None:
    panel = CompactionConfigPanel(CompactionConfig(last_words_template="Extra coding emphasis"))

    assert panel.get_config().last_words_template == "Extra coding emphasis"


@pytest.mark.parametrize("supplement", ["", "Extra coding emphasis"])
async def test_compaction_panel_supplement_round_trip_without_default_prefill(supplement: str) -> None:
    app = WidgetApp(lambda: CompactionConfigPanel(CompactionConfig(last_words_template=supplement)))
    async with app.run_test(size=(80, 40)) as pilot:
        await _wait_for_selectors(app, pilot, "#cc-last-words-template")

        description = app.query_one(".cc-section-desc", Label)
        assert (
            "Compaction triggers automatically at the context window minus the model's maximum output tokens "
            "and a safety margin. Both limits are configured on the model profile." in str(description.render())
        )
        assert str(description.styles.height) == "auto"
        template = app.query_one("#cc-last-words-template", TextArea)
        assert template.text == supplement
        panel = app.query_one(CompactionConfigPanel)
        assert not panel.query("#cc-reserved-context-pct")
        assert panel.validate() == []
        assert panel.get_config().last_words_template == supplement


async def test_tools_descriptions_wrap_under_checkbox_label() -> None:
    app = WidgetApp(ToolsConfigPanel)
    async with app.run_test(size=(38, 40)) as pilot:
        await pilot.pause()

        descriptions = list(app.query(".tc-cat-desc").results(Label))
        desc = next(label for label in descriptions if label.render().plain.startswith("Convert PDF"))
        await wait_for(
            lambda: desc.size.height > 1,
            pilot=pilot,
            timeout=_DEFAULT_WAIT_TIMEOUT,
            description="tool category description wrap",
        )

        assert desc.render().plain == "Convert PDF, DOCX, PPTX, XLSX, XLS to Markdown"


async def test_tools_panel_todo_checkbox_round_trip() -> None:
    """The todo category surfaces as #tc-cat-todo and round-trips get_config()."""
    app = WidgetApp(lambda: ToolsConfigPanel(ToolsConfig(builtins=["todo"])))
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()

        checkbox = app.query_one("#tc-cat-todo", Checkbox)
        assert checkbox.value is True
        assert checkbox.disabled is False
        assert app.query_one(ToolsConfigPanel).get_config().builtins == ["todo"]

        checkbox.value = False
        await pilot.pause()
        assert "todo" not in app.query_one(ToolsConfigPanel).get_config().builtins


async def test_tools_panel_todo_checkbox_defaults_off_when_profile_omits_category() -> None:
    app = WidgetApp(lambda: ToolsConfigPanel(ToolsConfig(builtins=["shell"])))
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()

        checkbox = app.query_one("#tc-cat-todo", Checkbox)
        assert checkbox.value is False
        assert app.query_one(ToolsConfigPanel).get_config().builtins == ["shell"]


async def test_tools_panel_has_no_switch_for_the_always_loaded_sleep() -> None:
    """Every agent has sleep, so the panel neither shows it nor writes it."""
    app = WidgetApp(lambda: ToolsConfigPanel(ToolsConfig(builtins=["shell"])))
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()

        assert app.query("#tc-cat-shell")
        assert not app.query("#tc-cat-sleep")
        assert app.query_one(ToolsConfigPanel).get_config().builtins == ["shell"]


async def test_tools_panel_toggles_web_search_and_web_fetch_independently() -> None:
    """Web search and Web fetch are separate switches, each writing only its own category."""
    app = WidgetApp(lambda: ToolsConfigPanel(ToolsConfig(builtins=["web_fetch", "shell"])))
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()

        search = app.query_one("#tc-cat-web-search", Checkbox)
        fetch = app.query_one("#tc-cat-web-fetch", Checkbox)
        assert (search.value, fetch.value) == (False, True)
        assert app.query_one(ToolsConfigPanel).get_config().builtins == ["web_fetch", "shell"]

        search.value = True
        assert app.query_one(ToolsConfigPanel).get_config().builtins == ["web_search", "web_fetch", "shell"]
        fetch.value = False
        assert app.query_one(ToolsConfigPanel).get_config().builtins == ["web_search", "shell"]
