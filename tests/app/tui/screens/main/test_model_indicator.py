# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for pure input-bar model indicator state computation."""

from __future__ import annotations

from typing import Any, cast

import pytest

from chrys.app.tui.screens.main.model_indicator import (
    ModelIndicatorState,
    compute_model_indicator_state,
    fmt_context_size,
    is_model_selection_locked,
)
from chrys.foundation.events.types import RuntimeModelDetails
from chrys.foundation.i18n import Localizer

_EN = Localizer("en")


def _details(
    *,
    source: str = "active",
    name: str = "DeepSeek V4 Flash",
    api_style: str = "responses",
    stream: bool = False,
    vision: bool = False,
) -> RuntimeModelDetails:
    return RuntimeModelDetails(
        profile_id="deepseek-v4",
        name=name,
        provider="deepseek-openai",
        api_style=api_style,
        model_id="deepseek-v4-flash",
        max_context_tokens=200_000,
        stream=stream,
        vision=vision,
        selection_source=cast(Any, source),
    )


@pytest.mark.parametrize(
    ("has_selectable_profile", "label", "mode", "tooltip"),
    [
        (False, "Select Model", "configure", "Open model settings"),
        (True, "Select Model", "select", "Choose a model profile"),
    ],
)
def test_unconfirmed_details_use_only_registry_action_state(
    has_selectable_profile: bool,
    label: str,
    mode: str,
    tooltip: str,
) -> None:
    state = compute_model_indicator_state(None, has_selectable_profile, "Code", _EN)

    assert state.label == label
    assert state.mode == mode
    assert state.tooltip == tooltip
    assert state.profile_id == ""
    assert state.visible is True


def test_missing_agent_label_hides_model_action_until_startup_confirms_agent() -> None:
    state = compute_model_indicator_state(None, True, "", _EN)

    assert state == ModelIndicatorState(label="", tooltip="", mode="locked", profile_id="", visible=False)


def test_unconfirmed_runtime_hides_model_action_even_when_agent_label_is_seeded() -> None:
    state = compute_model_indicator_state(None, True, "Code Agent", _EN, runtime_confirmed=False)

    assert state == ModelIndicatorState(label="", tooltip="", mode="locked", profile_id="", visible=False)


def test_confirmed_active_model_remains_visible_while_agent_label_is_temporarily_empty() -> None:
    state = compute_model_indicator_state(_details(), True, "", _EN)

    assert state.label == "DeepSeek V4 Flash"
    assert state.mode == "select"
    assert state.profile_id == "deepseek-v4"
    assert state.visible is True


def test_confirmed_agent_locked_model_without_agent_label_uses_generic_reason() -> None:
    state = compute_model_indicator_state(_details(source="agent"), True, "", _EN)

    assert state.label == "DeepSeek V4 Flash"
    assert state.mode == "locked"
    assert state.tooltip.endswith("Model selection is locked.")


def test_no_selectable_profile_takes_precedence_over_locked_source() -> None:
    state = compute_model_indicator_state(_details(source="agent"), False, "Code", _EN)

    assert state.label == "Select Model"
    assert state.mode == "configure"
    assert state.profile_id == ""
    assert state.visible is True


def test_default_source_uses_select_action_state() -> None:
    state = compute_model_indicator_state(_details(source="default", name="Default"), True, "Code", _EN)

    assert state.label == "Select Model"
    assert state.mode == "select"
    assert state.profile_id == ""


def test_active_source_uses_original_profile_name_without_suffix() -> None:
    state = compute_model_indicator_state(_details(name="My Fast Model"), True, "Code", _EN)

    assert state.label == "My Fast Model"
    assert state.mode == "select"
    assert state.profile_id == "deepseek-v4"
    assert state.visible is True


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("agent", "Model selection is controlled by Code Agent."),
        ("override", "This session is pinned to this model."),
        ("inherited", "Model selection is locked."),
    ],
)
def test_bound_sources_are_locked_with_source_specific_reason(source: str, reason: str) -> None:
    state = compute_model_indicator_state(_details(source=source), True, "Code Agent", _EN)

    assert state.label == "DeepSeek V4 Flash"
    assert state.mode == "locked"
    assert state.profile_id == "deepseek-v4"
    assert state.tooltip.splitlines()[-1] == reason


def test_unknown_source_fails_closed_as_locked() -> None:
    state = compute_model_indicator_state(_details(source="future-source"), True, "Code", _EN)

    assert state.label == "DeepSeek V4 Flash"
    assert state.mode == "locked"
    assert state.profile_id == "deepseek-v4"
    assert state.visible is True
    assert state.tooltip.endswith("Model selection is locked.")


def test_empty_api_style_omits_style_segment() -> None:
    state = compute_model_indicator_state(_details(api_style="", stream=True), True, "Code", _EN)

    assert state.tooltip == (
        "DeepSeek V4 Flash · deepseek-openai · deepseek-v4-flash · 200k context · Streaming · Text-only"
    )
    assert "·  ·" not in state.tooltip


def test_details_tooltip_contains_model_id_context_and_api_style() -> None:
    state = compute_model_indicator_state(_details(), True, "Code", _EN)

    assert "deepseek-v4-flash" in state.tooltip
    assert "200k context" in state.tooltip
    assert "responses" in state.tooltip


@pytest.mark.parametrize(
    ("stream", "vision", "suffix"),
    [
        (True, True, "Streaming · Vision"),
        (True, False, "Streaming · Text-only"),
        (False, True, "Non-streaming · Vision"),
        (False, False, "Non-streaming · Text-only"),
    ],
)
def test_details_tooltip_states_streaming_and_vision_capabilities(stream: bool, vision: bool, suffix: str) -> None:
    state = compute_model_indicator_state(_details(stream=stream, vision=vision), True, "Code", _EN)

    assert state.tooltip.endswith(suffix)


def test_capability_segments_render_through_injected_localizer() -> None:
    zh = Localizer("zh-Hans")

    on = compute_model_indicator_state(_details(stream=True, vision=True), True, "Code", zh)
    off = compute_model_indicator_state(_details(), True, "Code", zh)

    assert on.tooltip.endswith("流式输出 · 视觉模型")
    assert off.tooltip.endswith("非流式输出 · 纯文本模型")
    assert "上下文 200k" in on.tooltip


def test_action_labels_render_through_injected_localizer() -> None:
    zh = Localizer("zh-Hans")

    configure = compute_model_indicator_state(None, False, "Code", zh)
    select = compute_model_indicator_state(None, True, "Code", zh)

    assert configure.label == "选择模型"
    assert select.label == "选择模型"


def test_locked_reason_renders_through_injected_localizer() -> None:
    state = compute_model_indicator_state(_details(source="agent"), True, "Code Agent", Localizer("zh-Hans"))

    assert state.tooltip.splitlines()[-1] == "模型选择由 Code Agent 控制。"


def test_fmt_context_size_uses_compact_lowercase_units() -> None:
    assert fmt_context_size(500) == "500"
    assert fmt_context_size(200_000) == "200k"
    assert fmt_context_size(999_949) == "999.9k"
    assert fmt_context_size(999_950) == "1.0m"
    assert fmt_context_size(999_999) == "1.0m"
    assert fmt_context_size(1_000_000) == "1m"


@pytest.mark.parametrize("source", ["active", "default", "agent", "override", "inherited"])
def test_lock_predicate_agrees_with_the_indicator_it_mirrors(source: str) -> None:
    """The $ picker and the status-bar tag must never disagree about ownership.

    Both read the same ``selection_source``; this pins the standalone
    predicate to the indicator's own ``locked`` verdict so a new source value
    cannot be classified one way for the tag and the other for the picker.
    """
    details = _details(source=source)

    indicator = compute_model_indicator_state(details, True, "Code", _EN)

    assert is_model_selection_locked(details, runtime_confirmed=True) is (indicator.mode == "locked")


@pytest.mark.parametrize("source", ["active", "default", "agent", "override", "inherited"])
def test_unconfirmed_runtime_never_locks_the_picker(source: str) -> None:
    """The tag hides while details are unconfirmed; the picker stays usable."""
    assert is_model_selection_locked(_details(source=source), runtime_confirmed=False) is False
