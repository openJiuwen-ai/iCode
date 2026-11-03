# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the model picker modal."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option, OptionDoesNotExist

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.models.picker import ModelPickerAction, ModelsScreen
from chrys.foundation.config.settings import Settings
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.support.waiting import wait_for


def _profile(profile_id: str, name: str, *, model_id: str | None = None) -> ModelProfile:
    return ModelProfile(
        id=profile_id,
        name=name,
        provider="openai",
        model_id=model_id if model_id is not None else f"{profile_id}-wire",
    )


def _registry(*profiles: ModelProfile) -> ModelProfileRegistry:
    registry = ModelProfileRegistry()
    for profile in profiles:
        registry.register(profile)
    return registry


class _PickerHarness(App[None]):
    def __init__(
        self,
        registry: ModelProfileRegistry,
        current_profile_id: str = "",
        *,
        locale: str = "en",
    ) -> None:
        super().__init__()
        self.locale_controller = LocaleController(Settings(locale=locale))
        self.picker = ModelsScreen(registry, current_profile_id)
        self.results: list[str | ModelPickerAction | None] = []

    def compose(self) -> ComposeResult:
        yield Static("base")

    def on_mount(self) -> None:
        self.push_screen(self.picker, self.results.append)


@pytest.mark.asyncio
async def test_model_picker_matches_current_by_id_and_hides_incomplete_profiles() -> None:
    registry = _registry(
        _profile("current", "Shared name"),
        _profile("other", "Shared name"),
        _profile("action:manage", "Sentinel-shaped ID"),
        _profile("incomplete", "Incomplete", model_id=""),
    )
    app = _PickerHarness(registry, current_profile_id="current")

    async with app.run_test() as pilot:
        await pilot.pause()
        option_list = app.picker.query_one(OptionList)

        current = option_list.get_option("profile:current")
        same_name = option_list.get_option("profile:other")
        sentinel = option_list.get_option("profile:action:manage")

        assert current.disabled is True
        assert current.prompt.plain == "◦ Shared name"
        assert same_name.disabled is False
        assert same_name.prompt.plain == "Shared name"
        assert sentinel.prompt.plain == "Sentinel-shaped ID"
        # AIxCoding catalog: manage entry disabled — profiles only.
        with pytest.raises(OptionDoesNotExist):
            option_list.get_option("action:manage")
        with pytest.raises(OptionDoesNotExist):
            option_list.get_option("profile:incomplete")


@pytest.mark.asyncio
async def test_model_picker_lists_profiles_without_rules_or_manage_action() -> None:
    registry = _registry(_profile("first", "First"), _profile("second", "Second"), _profile("third", "Third"))
    app = _PickerHarness(registry, current_profile_id="first")

    async with app.run_test() as pilot:
        option_list = app.picker.query_one(OptionList)
        await wait_for(lambda: option_list.virtual_size.height, pilot=pilot, description="picker laid out")

        # Profiles only, no separator rule.
        assert option_list.option_count == 3
        assert option_list.virtual_size.height == 3


@pytest.mark.asyncio
async def test_model_picker_lists_models_in_reading_order() -> None:
    names = ["huoshan-seed-pro-2.1", "Kimi K3", "GPT-5.10", "glm-5.3", "GPT-5.9", "DeepSeek-V4", "GPT-5.9 [Responses]"]
    registry = _registry(*(_profile(f"id-{index}", name) for index, name in enumerate(names)))
    app = _PickerHarness(registry, current_profile_id="id-2")

    async with app.run_test() as pilot:
        await pilot.pause()
        option_list = app.picker.query_one(OptionList)
        listed = [str(option.prompt).removeprefix("◦ ") for option in option_list.options[: len(names)]]

    # Case aside, and 5.9 before 5.10; the current model keeps its place in the order.
    assert listed == [
        "DeepSeek-V4",
        "glm-5.3",
        "GPT-5.9",
        "GPT-5.9 [Responses]",
        "GPT-5.10",
        "huoshan-seed-pro-2.1",
        "Kimi K3",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("option_id", "expected"),
    [
        ("action:manage", ModelPickerAction.MANAGE),
        ("profile:action:manage", "action:manage"),
    ],
)
async def test_model_picker_namespaces_manage_action_and_profile_ids(
    option_id: str,
    expected: str | ModelPickerAction,
) -> None:
    app = _PickerHarness(_registry(_profile("action:manage", "Sentinel-shaped ID")))

    async with app.run_test() as pilot:
        await pilot.pause()
        option_list = app.picker.query_one(OptionList)
        if option_id == "action:manage":
            # AIxCoding catalog: manage routing via synthesized event — the
            # entry is no longer rendered, but the protocol path stays covered.
            option = Option("Manage models…", id="action:manage")
            option_index = option_list.option_count
        else:
            option = option_list.get_option(option_id)
            option_index = option_list.get_option_index(option_id)

        option_list.post_message(OptionList.OptionSelected(option_list, option, option_index))
        await pilot.pause()

    assert app.results == [expected]


@pytest.mark.asyncio
async def test_model_picker_escape_closes_without_selection() -> None:
    app = _PickerHarness(_registry(_profile("model-a", "Model A")))

    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    assert app.results == [None]


@pytest.mark.asyncio
async def test_model_picker_renders_chinese_title() -> None:
    app = _PickerHarness(_registry(), locale="zh-Hans")

    async with app.run_test() as pilot:
        await pilot.pause()
        container = app.picker.query_one("#container")

        # The manage entry is gone, but the title still renders localized.
        assert container.border_title == "模型"
