# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the model configuration screen: layout, clone, delete, read-only, and default/process pointers."""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
from textual.containers import Vertical
from textual.geometry import Region
from textual.widgets import Button, Checkbox, Input, Label, OptionList, Select, Static

from chrys.app.tui.screens.models.screen import (
    _API_STYLE_CHAT_COMPLETIONS,
    _API_STYLE_RESPONSES,
    _BASE_URL,
    _CHAT_OPTIONS,
    _HEADER_NAME_PLACEHOLDER,
    _HEADER_VALUE_PLACEHOLDER,
    _OPTION_NAME_PLACEHOLDER,
    _OPTION_VALUE_PLACEHOLDER,
    _PROVIDER_BASE_URL,
    _VISION,
)
from chrys.foundation.i18n import Localizer
from chrys.foundation.i18n.formatting import format_message
from chrys.service.profiles.models.loader import load_profile_from_yaml
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.app.tui.screens._model_config_support import (
    _capture_notifications,
    _model_config_result_events,
    _wait_for_kv_rows,
    fill_kv_row,
    open_model_config,
    single_profile_registry,
)
from tests.support.paths import SRC_ROOT
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for, wait_until

pytestmark = pytest.mark.usefixtures("isolated_model_config_dir", "clear_model_profile_env")


def test_model_labels_follow_localization_contract() -> None:
    assert format_message(_API_STYLE_CHAT_COMPLETIONS.bind()) == "Chat Completions"
    assert format_message(_API_STYLE_RESPONSES.bind()) == "Responses"
    assert format_message(_VISION.bind()) == "Vision Model"
    chinese = Localizer("zh-Hans")
    assert chinese.render(_API_STYLE_CHAT_COMPLETIONS.bind()) == "Chat Completions"
    assert chinese.render(_API_STYLE_RESPONSES.bind()) == "Responses"
    assert chinese.render(_BASE_URL.bind()) == "服务地址"
    assert chinese.render(_PROVIDER_BASE_URL.bind(provider="OpenAI")) == "OpenAI 服务地址"
    assert chinese.render(_CHAT_OPTIONS.bind()) == "Chat 选项（额外请求字段）"  # noqa: RUF001
    assert chinese.render(_VISION.bind()) == "视觉模型"


def test_model_option_and_header_placeholders_show_usable_examples() -> None:
    assert format_message(_HEADER_NAME_PLACEHOLDER.bind()) == "e.g. X-Auth-Token"
    assert format_message(_HEADER_VALUE_PLACEHOLDER.bind()) == "e.g. {{AUTH_TOKEN}}"
    assert format_message(_OPTION_NAME_PLACEHOLDER.bind()) == "e.g. extra_body, temperature"
    assert format_message(_OPTION_VALUE_PLACEHOLDER.bind()) == "e.g. 0.7, true, or {...}"

    chinese = Localizer("zh-Hans")
    assert chinese.render(_HEADER_NAME_PLACEHOLDER.bind()) == "例如 X-Auth-Token"
    assert chinese.render(_HEADER_VALUE_PLACEHOLDER.bind()) == "例如 {{AUTH_TOKEN}}"
    assert chinese.render(_OPTION_NAME_PLACEHOLDER.bind()) == "例如 extra_body、temperature"
    assert chinese.render(_OPTION_VALUE_PLACEHOLDER.bind()) == "例如 0.7、true 或 {...}"


def test_provider_editor_tables_cover_every_provider() -> None:
    """The editor's label and default-base-url maps have silent ``.title()``/empty
    fallbacks, so a provider missing from either ships a visibly broken editor."""
    from chrys.app.tui.screens.models.screen import _PROVIDER_DEFAULT_BASE_URLS, _PROVIDER_LABELS, _PROVIDERS

    provider_ids = {provider_id for _label, provider_id in _PROVIDERS}
    assert provider_ids <= set(_PROVIDER_LABELS)
    assert provider_ids <= set(_PROVIDER_DEFAULT_BASE_URLS)
    assert _PROVIDER_LABELS["glm-openai"] == "GLM (OpenAI)"
    assert _PROVIDER_DEFAULT_BASE_URLS["glm-openai"] == "https://open.bigmodel.cn/api/paas/v4"


async def test_model_config_uses_default_token_limits() -> None:
    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        assert screen.query_one("#mc-max-tokens", Input).value == "200000"
        assert screen.query_one("#mc-max-output-tokens", Input).value == "32000"


def _shown_label_text(label: Label) -> str:
    """The label's text as painted, wrapped lines joined, so clipped cells are missing."""
    lines = label.render_lines(Region(0, 0, label.size.width, label.size.height))
    return " ".join(strip.text.strip() for strip in lines)


def _label_fits(label: Label) -> bool:
    width = label.size.width
    return label.size.height == label.get_content_height(label.container_size, label.container_size, width)


async def test_model_config_token_labels_hint_the_loaded_profile() -> None:
    # Neither value is a compose default, so the hint can only come from loading the profile.
    registry, profile = single_profile_registry(max_context_tokens=1_000_000, max_output_tokens=128_000)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        context_label = screen.query_one("#mc-max-tokens-label", Label)
        output_label = screen.query_one("#mc-max-output-label", Label)
        await wait_for(lambda: str(context_label.render()).endswith("Max Context Window · 1M"), pilot=pilot)
        await wait_for(lambda: str(output_label.render()).endswith(" · 128K"), pilot=pilot)


async def test_model_config_token_labels_follow_typed_values() -> None:
    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        context_label = screen.query_one("#mc-max-tokens-label", Label)
        output_label = screen.query_one("#mc-max-output-label", Label)

        context_input = screen.query_one("#mc-max-tokens", Input)
        context_input.focus()
        await wait_for(lambda: context_input.has_focus, pilot=pilot)
        await pilot.press("ctrl+a", *"1280000")
        await wait_for(lambda: str(context_label.render()).endswith("Max Context Window · 1.28M"), pilot=pilot)
        await pilot.press("ctrl+a", *"128040")
        await wait_for(lambda: str(context_label.render()).endswith("Max Context Window · 128.04K"), pilot=pilot)
        await pilot.press("ctrl+a", *"3500008")
        await wait_for(lambda: str(context_label.render()).endswith("Max Context Window · ~3.5M"), pilot=pilot)
        await pilot.press("ctrl+a", *"999")
        await wait_for(lambda: str(context_label.render()).endswith("Max Context Window"), pilot=pilot)

        output_input = screen.query_one("#mc-max-output-tokens", Input)
        output_input.focus()
        await wait_for(lambda: output_input.has_focus, pilot=pilot)
        await pilot.press("ctrl+a", *"64000")
        await wait_for(lambda: str(output_label.render()).endswith(" · 64K"), pilot=pilot)


async def test_model_config_output_hint_survives_provider_and_api_style_changes() -> None:
    registry, profile = single_profile_registry(provider="openai", max_output_tokens=64_000)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        output_label = screen.query_one("#mc-max-output-label", Label)
        await wait_for(
            lambda: str(output_label.render()) == "Max Output Tokens (max_completion_tokens) · 64K", pilot=pilot
        )

        screen.query_one("#mc-api-style", Select).value = "responses"
        await wait_for(lambda: str(output_label.render()) == "Max Output Tokens (max_output_tokens) · 64K", pilot=pilot)

        screen.query_one("#mc-provider", Select).value = "anthropic"
        await wait_for(lambda: str(output_label.render()) == "Max Output Tokens (max_tokens) · 64K", pilot=pilot)


async def test_model_config_token_labels_wrap_instead_of_clipping_the_hint_on_a_narrow_terminal() -> None:
    registry, profile = single_profile_registry(max_context_tokens=1_000_000, max_output_tokens=128_000)

    # At 70 columns the context title fits one line alone but needs a second line with a hint.
    async with open_model_config(registry, global_default_profile_id=profile.id, size=(70, 40)) as (screen, pilot):
        context_label = screen.query_one("#mc-max-tokens-label", Label)
        output_label = screen.query_one("#mc-max-output-label", Label)
        await wait_for(lambda: "· 128K" in _shown_label_text(output_label), pilot=pilot)
        assert output_label.size.height > 1
        assert _label_fits(output_label)
        await wait_for(lambda: "· 1M" in _shown_label_text(context_label), pilot=pilot)

        context_input = screen.query_one("#mc-max-tokens", Input)
        context_input.focus()
        await wait_for(lambda: context_input.has_focus, pilot=pilot)
        await pilot.press("ctrl+a", *"999")
        await wait_for(lambda: context_label.size.height == 1, pilot=pilot)
        assert _label_fits(context_label)
        await pilot.press("ctrl+a", *"1280000")
        await wait_for(lambda: "· 1.28M" in _shown_label_text(context_label), pilot=pilot)
        assert context_label.size.height == 2
        assert _label_fits(context_label)


async def test_model_config_lists_profiles_in_reading_order_and_selects_the_first_listed() -> None:
    """The sidebar orders profiles as the picker does, and with no usable global default the first listed is selected."""
    names = ["huoshan-seed-pro-2.1", "Kimi K3", "GPT-5.10", "glm-5.3", "GPT-5.9", "DeepSeek-V4"]
    registry = ModelProfileRegistry()
    for index, name in enumerate(names):
        registry.register(ModelProfile(id=f"id-{index}", name=name, model_id="wire"))

    async with open_model_config(registry, global_default_profile_id="ghost-id") as (screen, _pilot):
        sidebar = screen.query_one("#mc-list", OptionList)
        listed = [option.prompt.plain for option in sidebar.options]

        assert listed == ["DeepSeek-V4", "glm-5.3", "GPT-5.9", "GPT-5.10", "huoshan-seed-pro-2.1", "Kimi K3"]
        assert sidebar.highlighted == 0
        assert screen._selected_profile_id == "id-5"


async def test_model_config_input_ctrl_a_selects_and_deletes_text() -> None:
    registry = ModelProfileRegistry()
    profile = ModelProfile(id="model-a", name="Model A", model_id="deepseek-v4-flash")
    registry.register(profile)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        model_input = screen.query_one("#mc-model", Input)
        model_input.focus()
        await wait_for(lambda: model_input.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()

        await pilot.press("ctrl+a")
        await pilot.pause()

        assert model_input.selected_text == "deepseek-v4-flash"

        await pilot.press("backspace")
        await pilot.pause()

        assert model_input.value == ""


async def test_model_config_clone_saves_copy_with_new_id(
    tmp_path: Path,
) -> None:
    registry = ModelProfileRegistry()
    profile = ModelProfile(
        id="model-a",
        name="Model A",
        provider="anthropic",
        model_id="claude-test",
        max_context_tokens=200000,
        base_url="https://example.test",
        api_key="{{MODEL_KEY}}",
        http_connect_timeout=5.0,
        http_read_timeout=60.0,
        http_max_retries=4,
        verify_ssl=False,
        bypass_proxy=True,
        http_headers=json.dumps({"X-Team": "platform"}),
        chat_options=json.dumps({"temperature": 0.7}),
        stream=False,
    )
    registry.register(profile)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        screen.query_one("#mc-clone", Button).press()
        # The clone handler hops to a thread for the save; poll for its
        # selection switch instead of betting on a single pause.
        await wait_for(
            lambda: screen._selected_profile_id != profile.id,
            pilot=pilot,
            description="clone selected",
        )

        copied = registry.get(screen._selected_profile_id)

    assert copied is not None
    assert copied.id != profile.id
    assert copied.name == "Model A Copy"
    assert copied.provider == profile.provider
    assert copied.model_id == profile.model_id
    assert copied.max_context_tokens == profile.max_context_tokens
    assert copied.base_url == profile.base_url
    assert copied.api_key == profile.api_key
    assert copied.http_connect_timeout == profile.http_connect_timeout
    assert copied.http_read_timeout == profile.http_read_timeout
    assert copied.http_max_retries == profile.http_max_retries
    assert copied.verify_ssl == profile.verify_ssl
    assert copied.bypass_proxy == profile.bypass_proxy
    assert copied.http_headers == profile.http_headers
    assert copied.chat_options == profile.chat_options
    assert copied.stream is False
    assert (tmp_path / "models" / f"{copied.id}.yaml").is_file()


@pytest.mark.parametrize(
    ("seed", "clone_presses", "expected_names"),
    [
        pytest.param(
            ModelProfile(id="model-a", name="Model A", model_id="gpt-test"),
            3,
            {"Model A", "Model A Copy", "Model A Copy 2", "Model A Copy 3"},
            id="from_root_profile",
        ),
        pytest.param(
            ModelProfile(id="model-copy", name="Model A copy 2", model_id="gpt-test"),
            1,
            {"Model A copy 2", "Model A Copy 3"},
            id="without_root_profile",
        ),
    ],
)
async def test_model_config_clone_increments_clone_suffix(
    seed: ModelProfile,
    clone_presses: int,
    expected_names: set[str],
) -> None:
    """Clone numbering continues from the highest existing suffix, with or without the root profile."""

    registry = ModelProfileRegistry()
    registry.register(seed)

    async with open_model_config(registry, global_default_profile_id=seed.id) as (screen, pilot):
        # Each clone must land before the next press: the handler saves on a
        # thread, and the following clone reads the selection it installs.
        for expected in range(2, clone_presses + 2):
            screen.query_one("#mc-clone", Button).press()
            await wait_for(
                lambda expected=expected: len(registry.list_profiles()) == expected,
                pilot=pilot,
                description="clone registered",
            )

        selected = registry.get(screen._selected_profile_id)

    assert selected is not None
    assert selected.name == "Model A Copy 3"
    assert {p.name for p in registry.list_profiles()} == expected_names


async def test_model_config_footer_buttons_stay_inside_modal_after_clone() -> None:
    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id, size=(90, 40)) as (screen, pilot):
        screen.query_one("#mc-clone", Button).press()
        await wait_for(
            lambda: len(registry.list_profiles()) == 2,
            pilot=pilot,
            description="clone registered",
        )

        container = screen.query_one("#mc-container", Vertical)
        container_left = container.region.x
        container_right = container.region.x + container.region.width
        buttons = [
            screen.query_one(f"#{button_id}", Button)
            for button_id in ("mc-new", "mc-clone", "mc-delete", "mc-save", "mc-cancel")
        ]
        sidebar = screen.query_one("#mc-list", OptionList)

        assert all(container_left <= button.region.x for button in buttons)
        assert all(button.region.x + button.region.width <= container_right for button in buttons)
        assert list(screen.query("#mc-activate")) == []
        assert all("(Active)" not in sidebar.get_option(item.id).prompt.plain for item in registry.list_profiles())

    css_path = SRC_ROOT / "chrys/app/tui/screens/models/screen.tcss"
    assert "#mc-activate" not in css_path.read_text(encoding="utf-8")


async def test_model_config_save_global_default_is_file_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.service.profiles.models.env_bridge import (
        get_global_default_profile_id,
        set_global_default_profile_id,
    )
    from chrys.service.profiles.models.serializer import save_profile

    io_threads: list[int] = []

    def recording_save(profile: ModelProfile) -> Path:
        io_threads.append(threading.get_ident())
        return save_profile(profile)

    def recording_default(profile_id: str) -> None:
        io_threads.append(threading.get_ident())
        set_global_default_profile_id(profile_id)

    monkeypatch.setattr("chrys.service.profiles.models.serializer.save_profile", recording_save)
    monkeypatch.setattr(
        "chrys.service.profiles.models.env_bridge.set_global_default_profile_id",
        recording_default,
    )

    registry = ModelProfileRegistry()
    global_default = ModelProfile(id="model-a", name="Model A", model_id="global-wire")
    runtime_effective = ModelProfile(id="model-b", name="Model B", model_id="runtime-wire")
    registry.register(global_default)
    registry.register(runtime_effective)
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", runtime_effective.id)
    # The settings document starts without a default profile so the assertion below proves the
    # save path itself performed the file write.
    assert get_global_default_profile_id() == ""

    async with open_model_config(registry, global_default_profile_id=global_default.id) as (screen, _pilot):
        saved = await screen._save_only()
        result = screen._cancel_result()

        assert saved is not None
        assert get_global_default_profile_id() == global_default.id
        assert os.environ["CHRYS_MODEL_PROFILE"] == runtime_effective.id
        assert result == "updated"
        assert len(io_threads) == 2
        assert threading.get_ident() not in io_threads

    published = await _model_config_result_events(result, registry)
    assert len(published) == 1


async def test_first_save_with_no_global_default_promotes_and_close_adopts() -> None:
    """A first configured model becomes the default at save and active at close."""
    from chrys.service.profiles.models.env_bridge import get_global_default_profile_id

    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="model-a", name="Model A", model_id="first-wire"))
    assert get_global_default_profile_id() == ""

    async with open_model_config(registry, global_default_profile_id="") as (screen, _pilot):
        saved = await screen._save_only()
        result = screen._cancel_result()

        assert saved is not None
        # The save claims the empty global default, file-only: the process
        # pointer stays unset until the screen closes.
        assert get_global_default_profile_id() == "model-a"
        assert "CHRYS_MODEL_PROFILE" not in os.environ
        assert result == "updated"

    published = await _model_config_result_events(result, registry)
    assert len(published) == 1
    assert os.environ["CHRYS_MODEL_PROFILE"] == "model-a"


async def test_save_does_not_override_existing_global_default() -> None:
    from chrys.service.profiles.models.env_bridge import (
        get_global_default_profile_id,
        set_global_default_profile_id,
    )

    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="model-a", name="Model A", model_id="first-wire"))
    registry.register(ModelProfile(id="model-b", name="Model B", model_id="default-wire"))
    set_global_default_profile_id("model-b")

    async with open_model_config(registry, global_default_profile_id="model-b") as (screen, pilot):
        screen._load_profile("model-a")
        await pilot.pause()

        saved = await screen._save_only()

        assert saved is not None
        assert saved.id == "model-a"
        assert get_global_default_profile_id() == "model-b"


@pytest.mark.parametrize("stale_default", ["ghost-id", "hollow-id"])
async def test_save_reclaims_unresolvable_global_default(stale_default: str) -> None:
    """A stored default whose profile vanished (models directory replaced,
    file deleted externally) or was never filled in is no default at all;
    the next successful save claims the pointer like the empty case."""
    from chrys.service.profiles.models.env_bridge import (
        get_global_default_profile_id,
        set_global_default_profile_id,
    )

    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="model-a", name="Model A", model_id="first-wire"))
    registry.register(ModelProfile(id="hollow-id", name="Hollow"))
    set_global_default_profile_id(stale_default)

    async with open_model_config(registry, global_default_profile_id=stale_default) as (screen, pilot):
        screen._load_profile("model-a")
        await pilot.pause()

        saved = await screen._save_only()

        assert saved is not None
        assert saved.id == "model-a"
        assert get_global_default_profile_id() == "model-a"


async def test_rename_normalizes_name_based_process_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Renaming the runtime-effective profile must not strand a name-based
    process pointer: it is normalized to the profile id, or modal close
    would misread the runtime as inactive and adopt the global default —
    silently switching the live model."""
    from chrys.service.profiles.models.env_bridge import (
        get_global_default_profile_id,
        set_global_default_profile_id,
    )

    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="model-a", name="Old Name", model_id="runtime-wire"))
    registry.register(ModelProfile(id="model-b", name="Model B", model_id="default-wire"))
    set_global_default_profile_id("model-b")
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "Old Name")

    async with open_model_config(registry, global_default_profile_id="model-b") as (screen, pilot):
        screen._load_profile("model-a")
        await pilot.pause()
        screen.query_one("#mc-name", Input).value = "New Name"

        saved = await screen._save_only()

        assert saved is not None
        assert saved.name == "New Name"
        assert os.environ["CHRYS_MODEL_PROFILE"] == "model-a"
        assert get_global_default_profile_id() == "model-b"


async def test_rename_leaves_other_profiles_name_pointer_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.service.profiles.models.env_bridge import set_global_default_profile_id

    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="model-a", name="Old Name", model_id="runtime-wire"))
    registry.register(ModelProfile(id="model-b", name="Model B", model_id="default-wire"))
    set_global_default_profile_id("model-b")
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "Model B")

    async with open_model_config(registry, global_default_profile_id="model-b") as (screen, pilot):
        screen._load_profile("model-a")
        await pilot.pause()
        screen.query_one("#mc-name", Input).value = "New Name"

        await screen._save_only()

        assert os.environ["CHRYS_MODEL_PROFILE"] == "Model B"


async def test_model_config_delete_runtime_effective_reloads_from_global_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.service.profiles.models.env_bridge import (
        get_global_default_profile_id,
        set_global_default_profile_id,
    )

    registry = ModelProfileRegistry()
    global_default = ModelProfile(id="model-a", name="Model A", model_id="global-wire")
    runtime_effective = ModelProfile(id="model-b", name="Model B", model_id="runtime-wire")
    registry.register(global_default)
    registry.register(runtime_effective)
    set_global_default_profile_id(global_default.id)
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", runtime_effective.id)

    async with open_model_config(registry, global_default_profile_id=global_default.id) as (screen, _pilot):
        await screen._do_delete(runtime_effective.id)
        result = screen._cancel_result()

        assert registry.get(runtime_effective.id) is None
        assert get_global_default_profile_id() == global_default.id
        assert os.environ["CHRYS_MODEL_PROFILE"] == global_default.id
        assert result == "deleted"

    published = await _model_config_result_events(result, registry)
    assert len(published) == 1


async def test_model_config_delete_global_default_leaves_the_pointer_and_reloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.service.profiles.models.env_bridge import (
        get_global_default_profile_id,
        set_global_default_profile_id,
    )

    registry = ModelProfileRegistry()
    global_default = ModelProfile(id="model-a", name="Model A", model_id="global-wire")
    runtime_effective = ModelProfile(id="model-b", name="Model 10", model_id="runtime-wire")
    # Promotion follows the sidebar's reading order, not registration order: registered last, listed first.
    promoted_default = ModelProfile(id="model-c", name="Model 2", model_id="promoted-wire")
    registry.register(global_default)
    registry.register(runtime_effective)
    registry.register(promoted_default)
    set_global_default_profile_id(global_default.id)
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", runtime_effective.id)

    async with open_model_config(registry, global_default_profile_id=global_default.id) as (screen, _pilot):
        await screen._do_delete(global_default.id)
        result = screen._cancel_result()

        assert registry.get(global_default.id) is None
        assert get_global_default_profile_id() == promoted_default.id
        assert os.environ["CHRYS_MODEL_PROFILE"] == runtime_effective.id
        # Any deletion reloads: the engine drops whatever the session used that is gone.
        assert result == "deleted"

    published = await _model_config_result_events(result, registry)
    assert len(published) == 1


async def test_model_config_delete_recognizes_name_based_process_pointer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The runtime-effective check resolves the pointer by id OR unique name;
    a name pointer at the deleted profile must resync the environment and
    request a reload instead of keeping the dead executor running."""
    from chrys.service.profiles.models.env_bridge import set_global_default_profile_id

    registry = ModelProfileRegistry()
    global_default = ModelProfile(id="model-a", name="Model A", model_id="global-wire")
    runtime_effective = ModelProfile(id="model-b", name="Model B", model_id="runtime-wire")
    registry.register(global_default)
    registry.register(runtime_effective)
    set_global_default_profile_id(global_default.id)
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", runtime_effective.name)

    async with open_model_config(registry, global_default_profile_id=global_default.id) as (screen, _pilot):
        await screen._do_delete(runtime_effective.id)
        result = screen._cancel_result()

        assert os.environ["CHRYS_MODEL_PROFILE"] == global_default.id
        assert result == "deleted"


async def test_model_config_delete_global_default_promotes_first_selectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Promotion skips hollow profiles: a never-filled auto-seeded profile can
    precede valid ones as the sidebar lists them, and promoting it would point
    both pointers at an unusable model while real ones exist."""
    from chrys.service.profiles.models.env_bridge import (
        get_global_default_profile_id,
        set_global_default_profile_id,
    )

    registry = ModelProfileRegistry()
    hollow = ModelProfile(id="model-hollow", name="Profile 1")
    global_default = ModelProfile(id="model-a", name="Model A", model_id="global-wire")
    # Listed after the hollow profile, so only the selectable filter can pick it.
    selectable = ModelProfile(id="model-b", name="Qwen3-Max", model_id="promoted-wire")
    registry.register(hollow)
    registry.register(global_default)
    registry.register(selectable)
    set_global_default_profile_id(global_default.id)
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", global_default.id)

    async with open_model_config(registry, global_default_profile_id=global_default.id) as (screen, _pilot):
        await screen._do_delete(global_default.id)

        assert get_global_default_profile_id() == selectable.id
        assert os.environ["CHRYS_MODEL_PROFILE"] == selectable.id


@pytest.mark.parametrize("file_default_pointer", ["model-hollow", "ghost-id"])
async def test_model_config_delete_runtime_effective_skips_unresolvable_file_default(
    file_default_pointer: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deleting the runtime-effective profile must land the process pointer on
    a selectable profile even when the file default is hollow or dangling;
    adopting that default verbatim would strand the session on the
    placeholder while a usable profile exists."""
    from chrys.service.profiles.models.env_bridge import (
        get_global_default_profile_id,
        set_global_default_profile_id,
    )

    registry = ModelProfileRegistry()
    hollow = ModelProfile(id="model-hollow", name="Profile 1")
    runtime_effective = ModelProfile(id="model-a", name="Model A", model_id="runtime-wire")
    # Listed after the hollow profile, so only the selectable filter can pick it.
    selectable = ModelProfile(id="model-b", name="Qwen3-Max", model_id="fallback-wire")
    registry.register(hollow)
    registry.register(runtime_effective)
    registry.register(selectable)
    set_global_default_profile_id(file_default_pointer)
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", runtime_effective.id)

    async with open_model_config(registry, global_default_profile_id=file_default_pointer) as (screen, _pilot):
        await screen._do_delete(runtime_effective.id)
        result = screen._cancel_result()

        # The file pointer is repaired only when the default itself is
        # deleted; the process pointer still lands on a usable profile.
        assert get_global_default_profile_id() == file_default_pointer
        assert os.environ["CHRYS_MODEL_PROFILE"] == selectable.id
        assert result == "deleted"


async def test_model_config_delete_runtime_effective_clears_pointer_when_nothing_selectable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.service.profiles.models.env_bridge import set_global_default_profile_id

    registry = ModelProfileRegistry()
    hollow = ModelProfile(id="model-hollow", name="Profile 1")
    runtime_effective = ModelProfile(id="model-a", name="Model A", model_id="runtime-wire")
    registry.register(hollow)
    registry.register(runtime_effective)
    set_global_default_profile_id(hollow.id)
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", runtime_effective.id)

    async with open_model_config(registry, global_default_profile_id=hollow.id) as (screen, _pilot):
        await screen._do_delete(runtime_effective.id)

        assert "CHRYS_MODEL_PROFILE" not in os.environ


async def test_model_config_delete_callback_reports_failure_without_exiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, profile = single_profile_registry()
    registry.register(ModelProfile(id="model-b", name="Model B", model_id="gpt-test-2"))

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        captured = _capture_notifications(screen)

        async def _fail_delete(_profile_id: str) -> None:
            raise TimeoutError("settings lock timed out")

        monkeypatch.setattr(screen, "_do_delete", _fail_delete)
        screen.query_one("#mc-delete", Button).press()
        await pilot.pause()
        dialog = pilot.app.screen
        # The buttons are composed by the nested DialogButtonRow, which mounts
        # a refresh after the dialog itself; poll rather than assume one pause.
        assert await wait_until(lambda: bool(dialog.query("#confirm-yes")), pilot=pilot), "confirm button never mounted"
        dialog.query_one("#confirm-yes", Button).press()
        await pilot.pause()

        assert pilot.app.screen is screen
        assert captured == [("error", "settings lock timed out")]


async def test_model_config_read_only_hides_mutations_and_does_not_write(tmp_path: Path) -> None:
    registry = ModelProfileRegistry()
    profile = ModelProfile(
        id="model-a",
        name="Model A",
        provider="openai",
        model_id="gpt-test",
        http_headers=json.dumps({"X-Test": "true"}),
    )
    registry.register(profile)

    async with open_model_config(registry, global_default_profile_id=profile.id, read_only=True) as (screen, pilot):
        assert screen.query_one("#mc-name", Input).disabled is True
        assert screen.query_one("#mc-provider", Select).disabled is True
        assert screen.query_one("#mc-stream", Checkbox).disabled is True
        assert screen.query_one("#mc-right", Vertical).disabled is False
        assert screen.query_one("#mc-cancel", Button).display is True
        assert screen.query_one("#mc-cancel", Button).disabled is False
        notice = screen.query_one("#mc-read-only-notice", Static)
        assert notice.display is True
        assert notice.render().plain == "• Agent is running. This page is read-only."
        assert screen.query_one("#mc-buttons-spacer", Static).display is True
        footer = screen.query_one("#mc-footer", Vertical)
        close = screen.query_one("#mc-cancel", Button)
        assert notice.region.y == footer.region.y + footer.region.height - 1
        assert notice.region.y > close.region.y
        assert notice.region.x == footer.region.x + 1
        assert notice.region.width == footer.region.width - 2
        assert list(screen.query("#mc-activate")) == []
        for button_id in ("mc-new", "mc-clone", "mc-delete", "mc-save"):
            button = screen.query_one(f"#{button_id}", Button)
            assert button.display is False
            assert button.disabled is True
        assert all(button.display is False for button in screen.query(".mc-kv-add-btn"))
        assert all(button.display is False for button in screen.query(".mc-kv-remove-btn"))

        screen.query_one("#mc-save", Button).press()
        await pilot.pause()

        assert screen._cancel_result() == ""
        assert not (tmp_path / "models").exists()


async def test_model_config_read_only_empty_registry_does_not_seed_profile(tmp_path: Path) -> None:
    registry = ModelProfileRegistry()

    async with open_model_config(registry, read_only=True) as (screen, _pilot):
        assert registry.list_profiles() == []
        assert not (tmp_path / "models").exists()
        assert screen.query_one("#mc-save", Button).display is False


async def test_model_config_saves_draft_key_value_rows_without_add() -> None:
    """Filled header and chat-option rows should be serialized without pressing Add."""

    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        headers_container = screen.query_one("#mc-headers-list")
        fill_kv_row(headers_container, "X-Team", "platform")

        options_container = screen.query_one("#mc-options-list")
        fill_kv_row(options_container, "temperature", "0.7")

        saved = screen._build_profile_from_form()

    assert json.loads(saved.http_headers) == {"X-Team": "platform"}
    assert json.loads(saved.chat_options) == {"temperature": 0.7}


async def test_model_config_form_keeps_fields_it_has_no_control_for() -> None:
    registry, profile = single_profile_registry(provider="glm-openai", stream_requires_finish_reason=True)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        saved = screen._build_profile_from_form()

    assert saved.stream_requires_finish_reason is True


async def test_model_config_form_keeps_the_thinking_settings_it_has_no_control_for() -> None:
    registry, profile = single_profile_registry(
        provider="anthropic", model_id="claude-opus-5-5", thinking_block_binding="off", auto_interleaved_thinking=False
    )

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        saved = screen._build_profile_from_form()
        # A profile the registry no longer holds keeps nothing: it gets the defaults.
        registry.remove(profile.id)
        unstored = screen._build_profile_from_form()

    assert (saved.thinking_block_binding, saved.auto_interleaved_thinking) == ("off", False)
    assert (unstored.thinking_block_binding, unstored.auto_interleaved_thinking) == ("auto", True)


@pytest.mark.parametrize("provider", ["openai", "deepseek-openai"])
async def test_model_config_responses_capable_provider_api_style_round_trip(provider: str) -> None:
    registry = ModelProfileRegistry()
    profile = ModelProfile(
        id="model-a",
        name="Model A",
        provider=provider,
        api_style="responses",
        model_id="gpt-test",
    )
    registry.register(profile)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        api_style = screen.query_one("#mc-api-style", Select)
        assert screen.query_one("#mc-api-style-label", Label).display is True
        assert api_style.display is True
        assert api_style.value == "responses"

        responses_saved = screen._build_profile_from_form()
        api_style.value = "chat_completions"
        saved = screen._build_profile_from_form()

        api_style.value = "responses"
        screen.query_one("#mc-provider", Select).value = "anthropic"
        screen._update_provider_labels("anthropic")
        hidden_saved = screen._build_profile_from_form()

    assert responses_saved.provider == provider
    assert responses_saved.api_style == "responses"
    assert saved.api_style == "chat_completions"
    assert hidden_saved.provider == "anthropic"
    assert hidden_saved.api_style == "chat_completions"


async def test_max_output_tokens_label_shows_wire_param_per_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Max Output Tokens label surfaces the actual wire parameter so users
    can tell max_tokens providers apart from max_completion_tokens ones.

    Labelling never imports a provider SDK or a client package built on one: on
    a process that has not loaded the SDK, the first open would import it on the
    UI loop.
    """
    # A blocked package stops its uncached submodules, but a cached submodule,
    # or a package cached as an attribute of its parent, would still import.
    clients = [f"chrys.service.llm.{name}" for name in ("chat_completions", "openai_responses", "anthropic_messages")]
    blocked = ("openai", "anthropic", *clients)
    cached = [name for name in sys.modules if name.startswith(tuple(f"{package}." for package in blocked))]
    for module in (*blocked, *cached):
        monkeypatch.setitem(sys.modules, module, None)
    for package in clients:
        monkeypatch.delattr(package, raising=False)
    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        label = screen.query_one("#mc-max-output-label", Label)

        def label_shows(param: str) -> Callable[[], bool]:
            return lambda: f"({param})" in label.visual.plain

        await wait_for(label_shows("max_completion_tokens"), pilot=pilot, description="initial label")

        screen.query_one("#mc-api-style", Select).value = "responses"
        await wait_for(label_shows("max_output_tokens"), pilot=pilot, description="responses label")

        screen.query_one("#mc-provider", Select).value = "deepseek-openai"
        screen.query_one("#mc-api-style", Select).value = "responses"
        await wait_for(label_shows("max_output_tokens"), pilot=pilot, description="deepseek responses label")

        screen.query_one("#mc-api-style", Select).value = "chat_completions"
        await wait_for(label_shows("max_tokens"), pilot=pilot, description="deepseek chat label")

        for provider in ("glm-openai", "anthropic"):
            screen.query_one("#mc-provider", Select).value = provider
            await wait_for(label_shows("max_tokens"), pilot=pilot, description=f"{provider} label")

        screen.query_one("#mc-provider", Select).value = "openai"
        await wait_for(label_shows("max_completion_tokens"), pilot=pilot, description="openai label restored")


async def test_model_config_preserves_env_templates_in_value_fields() -> None:
    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        screen.query_one("#mc-api-key", Input).value = "{{CHRYS_OPENAI_KEY}}"

        headers_container = screen.query_one("#mc-headers-list")
        fill_kv_row(headers_container, "Authorization", "Bearer {{CHRYS_HEADER_TOKEN}}")

        options_container = screen.query_one("#mc-options-list")
        fill_kv_row(options_container, "metadata", '{"token": "{{CHRYS_CHAT_TOKEN}}"}')

        saved = screen._build_profile_from_form()

    assert saved.api_key == "{{CHRYS_OPENAI_KEY}}"
    assert json.loads(saved.http_headers) == {"Authorization": "Bearer {{CHRYS_HEADER_TOKEN}}"}
    assert json.loads(saved.chat_options) == {"metadata": {"token": "{{CHRYS_CHAT_TOKEN}}"}}


async def test_model_config_saves_http_header_values_as_strings_when_json_like() -> None:
    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        headers_container = screen.query_one("#mc-headers-list")
        fill_kv_row(headers_container, "X-Config", '{"nested": true}')

        saved = screen._build_profile_from_form()

    assert json.loads(saved.http_headers) == {"X-Config": '{"nested": true}'}


async def test_model_config_reload_keeps_quoted_option_strings_as_strings() -> None:
    """A string that reads as JSON, or that the form's trim would change, reopens quoted, so the next Save
    keeps it the same string and passes validation."""
    options = {
        "user": "12345",
        "flag": "true",
        "effort": "high",
        "note": "\u00a05",
        "top_k": 5,
        "extra_body": {"thinking": "on"},
    }
    headers = {"X-Config": '{"nested": true}', "X-Count": "3"}
    registry, profile = single_profile_registry(chat_options=json.dumps(options), http_headers=json.dumps(headers))

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await _wait_for_kv_rows(screen.query_one("#mc-options-list"), pilot, len(options))
        await _wait_for_kv_rows(screen.query_one("#mc-headers-list"), pilot, len(headers))
        option_values = [
            row.query_one(".mc-kv-value-input", Input).value
            for row in screen.query_one("#mc-options-list").query(".mc-kv-item-row")
        ]
        captured = _capture_notifications(screen)
        screen.query_one("#mc-save", Button).press()
        await wait_for(
            lambda: ("information", "Model profile saved") in captured,
            pilot=pilot,
            description="profile saved again unchanged",
        )
        saved = registry.get(profile.id)

    assert option_values == ['"12345"', '"true"', "high", '"\\u00a05"', "5", '{"thinking": "on"}']
    assert saved is not None
    assert json.loads(saved.chat_options) == options
    assert json.loads(saved.http_headers) == headers


async def test_model_config_add_button_appends_another_editable_row() -> None:
    """Add creates another editable row instead of committing the current row."""

    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        headers_container = screen.query_one("#mc-headers-list")
        fill_kv_row(headers_container, "X-Team", "platform")

        screen.query_one("#mc-hadd").press()
        await _wait_for_kv_rows(headers_container, pilot, 2)

        rows = list(headers_container.query(".mc-kv-item-row"))
        assert len(rows) == 2
        rows[1].query_one(".mc-kv-key-input", Input).value = "X-Env"
        rows[1].query_one(".mc-kv-value-input", Input).value = "dev"

        saved = screen._build_profile_from_form()

    assert json.loads(saved.http_headers) == {"X-Team": "platform", "X-Env": "dev"}


async def test_model_config_sections_are_ordered_and_titled() -> None:
    """Each editor field sits in its titled section, and the sections compose in order."""

    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        skip_tls = screen.query_one("#mc-skip-tls", Checkbox)
        stream = screen.query_one("#mc-stream", Checkbox)
        vision = screen.query_one("#mc-vision", Checkbox)
        provider_select = screen.query_one("#mc-provider", Select)
        model_input = screen.query_one("#mc-model", Input)
        api_key_input = screen.query_one("#mc-api-key", Input)
        connect_timeout_input = screen.query_one("#mc-connect-timeout", Input)
        headers_list = screen.query_one("#mc-headers-list")
        chat_options_list = screen.query_one("#mc-options-list")
        model_options = screen.query_one("#mc-model-options")
        connection_options = screen.query_one("#mc-connection-options")
        http_options = screen.query_one("#mc-http-options")
        extra_options = screen.query_one("#mc-extra-options")
        scroll_children = list(screen.query_one("#mc-scroll").children)
        model_children = list(model_options.children)
        sections_in_order = [
            scroll_children.index(model_options),
            scroll_children.index(connection_options),
            scroll_children.index(http_options),
            scroll_children.index(extra_options),
        ]
        streaming_in_model_options = stream.parent is model_options
        vision_in_model_options = vision.parent is model_options
        vision_is_last_in_model_options = model_children[-1] is vision
        provider_in_model_options = provider_select.parent is model_options
        model_id_in_model_options = model_input.parent is model_options
        api_key_in_model_options = api_key_input.parent is model_options
        http_timeout_in_http_options = connect_timeout_input.parent is http_options
        headers_in_extra_options = headers_list.parent is extra_options
        chat_options_in_extra_options = chat_options_list.parent is extra_options
        skip_tls_in_options = skip_tls.parent is connection_options

    assert model_options.border_title == "Model Options"
    assert connection_options.border_title == "Connection Options"
    assert http_options.border_title == "HTTP Options"
    assert extra_options.border_title == "Extra Options"
    assert provider_in_model_options is True
    assert model_id_in_model_options is True
    assert api_key_in_model_options is True
    assert http_timeout_in_http_options is True
    assert headers_in_extra_options is True
    assert chat_options_in_extra_options is True
    assert skip_tls_in_options is True
    assert sections_in_order == sorted(sections_in_order)
    assert streaming_in_model_options is True
    assert vision_in_model_options is True
    assert vision_is_last_in_model_options is True


async def test_model_config_transport_checkboxes_default_secure_proxy_enabled_and_streaming() -> None:
    """Default model transport settings verify TLS, honor configured proxies, and stream."""

    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        skip_tls = screen.query_one("#mc-skip-tls", Checkbox)
        bypass_proxy = screen.query_one("#mc-bypass-proxy", Checkbox)
        stream = screen.query_one("#mc-stream", Checkbox)
        vision = screen.query_one("#mc-vision", Checkbox)
        tls_hint = screen.query_one("#mc-skip-tls-hint", Label)
        saved = screen._build_profile_from_form()

    assert skip_tls.value is False
    assert bypass_proxy.value is False
    assert stream.value is True
    assert vision.value is False
    assert tls_hint.display is False
    assert saved.verify_ssl is True
    assert saved.bypass_proxy is False
    assert saved.stream is True
    assert saved.vision is False


async def test_model_config_first_profile_on_empty_registry_streams(tmp_path: Path) -> None:
    """The profile seeded into an empty registry opens with Streaming checked and stores no ``stream`` key."""
    registry = ModelProfileRegistry()

    async with open_model_config(registry) as (screen, pilot):
        await wait_for(
            lambda: screen._selected_profile_id is not None,
            pilot=pilot,
            description="seeded profile selected",
        )
        seeded_id = screen._selected_profile_id
        checked = screen.query_one("#mc-stream", Checkbox).value

    assert checked is True
    raw = yaml.safe_load((tmp_path / "models" / f"{seeded_id}.yaml").read_text(encoding="utf-8"))
    assert "stream" not in raw


async def test_model_config_new_profile_streams_and_an_unchecked_save_reopens_unchecked(tmp_path: Path) -> None:
    """New never inherits the previous profile's Streaming off; unchecking it and saving writes ``stream: false``."""
    registry, profile = single_profile_registry(stream=False)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        stream = screen.query_one("#mc-stream", Checkbox)
        assert stream.value is False

        await click_when_settled(pilot, "#mc-new")
        await wait_for(
            lambda: screen._selected_profile_id not in {None, profile.id},
            pilot=pilot,
            description="new profile selected",
        )
        new_id = screen._selected_profile_id
        checked_on_new = stream.value

        screen.query_one("#mc-model", Input).value = "gpt-test"
        stream.value = False
        captured = _capture_notifications(screen)
        await click_when_settled(pilot, "#mc-save")
        await wait_for(
            lambda: ("information", "Model profile saved") in captured,
            pilot=pilot,
            description="new profile saved with streaming off",
        )

    assert checked_on_new is True
    assert new_id is not None
    path = tmp_path / "models" / f"{new_id}.yaml"
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["stream"] is False

    reloaded = load_profile_from_yaml(path)
    reopened_registry = ModelProfileRegistry()
    reopened_registry.register(reloaded)
    async with open_model_config(reopened_registry, global_default_profile_id=new_id) as (screen, _pilot):
        reopened_checked = screen.query_one("#mc-stream", Checkbox).value

    assert reopened_checked is False


async def test_model_config_max_output_tokens_round_trip() -> None:
    """The output cap is a required positive integer; blank/zero are rejected."""

    registry, profile = single_profile_registry(
        max_output_tokens=8192,
    )

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        cap_input = screen.query_one("#mc-max-output-tokens", Input)
        assert cap_input.value == "8192"

        cap_input.value = "64000"
        assert screen._validate() == []
        assert screen._build_profile_from_form().max_output_tokens == 64000

        cap_input.value = ""
        assert any("Max output tokens is required" in e for e in screen._validate())

        for invalid in ("0", "-1", "abc"):
            cap_input.value = invalid
            assert any("Max output tokens" in e for e in screen._validate())


async def test_model_config_transport_checkboxes_round_trip() -> None:
    """Skip TLS is inverted to verify_ssl; bypass proxy maps directly."""

    registry, profile = single_profile_registry(
        verify_ssl=False,
        bypass_proxy=True,
        stream=True,
        vision=True,
    )

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        skip_tls = screen.query_one("#mc-skip-tls", Checkbox)
        bypass_proxy = screen.query_one("#mc-bypass-proxy", Checkbox)
        stream = screen.query_one("#mc-stream", Checkbox)
        vision = screen.query_one("#mc-vision", Checkbox)
        tls_hint = screen.query_one("#mc-skip-tls-hint", Label)

        assert skip_tls.value is True
        assert bypass_proxy.value is True
        assert stream.value is True
        assert vision.value is True
        assert tls_hint.display is True

        skip_tls.value = False
        bypass_proxy.value = False
        stream.value = False
        vision.value = False
        await pilot.pause()
        saved = screen._build_profile_from_form()

    assert tls_hint.display is False
    assert saved.verify_ssl is True
    assert saved.bypass_proxy is False
    assert saved.stream is False
    assert saved.vision is False


async def test_model_config_delete_last_key_value_row_recreates_blank_row() -> None:
    """Removing the last row should keep one blank editable row visible."""

    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        headers_container = screen.query_one("#mc-headers-list")
        headers_container.query_one(".mc-kv-remove-btn", Button).press()
        await pilot.pause()

        rows = list(headers_container.query(".mc-kv-item-row"))

        assert len(rows) == 1
        assert rows[0].query_one(".mc-kv-key-input", Input).value == ""
        assert rows[0].query_one(".mc-kv-value-input", Input).value == ""
