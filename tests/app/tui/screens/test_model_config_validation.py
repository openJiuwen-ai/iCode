# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for model configuration save-time validation: rows, tokens, protected keys, headers, and charsets."""

# Cross-locale coverage is narrower than it looks. Of the clauses below, only the float
# chat-option message has a Chinese pin, in tests/app/tui/i18n/test_config_screens_validation.py
# (test_model_chat_option_clause_validation_renders_chinese_with_row_and_key). That file's
# protected-key test pins the *toast* wording rather than the ``_validate()`` clause asserted
# here. Every other clause below is English-only, so keep the float clause in step with its
# Chinese twin and do not read this as "all of these are covered in both locales".

from __future__ import annotations

import json
from pathlib import Path

import pytest
from textual.widgets import Button, Input

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.util.chrys_headers import MODEL_ID_HEADER, X_SESSION_ID_HEADER
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.app.tui.screens._model_config_support import (
    _capture_notifications,
    _wait_for_kv_rows,
    fill_kv_row,
    open_model_config,
    single_profile_registry,
)

pytestmark = pytest.mark.usefixtures("isolated_model_config_dir", "clear_model_profile_env")


async def test_model_config_validate_rejects_partial_key_value_rows() -> None:
    """Partially-filled key-value rows should block Save with clear errors."""

    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        headers_container = screen.query_one("#mc-headers-list")
        header_row = headers_container.query_one(".mc-kv-item-row")
        header_row.query_one(".mc-kv-key-input", Input).value = "X-Team"

        options_container = screen.query_one("#mc-options-list")
        option_row = options_container.query_one(".mc-kv-item-row")
        option_row.query_one(".mc-kv-value-input", Input).value = "0.7"

        errors = screen._validate()

    assert "HTTP Extra Headers row 1: value is required for key 'X-Team'." in errors
    assert "Chat Options row 1: key name is required when a value is set." in errors


async def test_model_config_cross_validates_context_and_output_token_limits() -> None:
    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        context_input = screen.query_one("#mc-max-tokens", Input)
        output_input = screen.query_one("#mc-max-output-tokens", Input)

        context_input.value = "99"
        output_input.value = "50"
        too_small = screen._validate()
        context_input.value = "100"
        output_input.value = "100"
        overlap = screen._validate()
        output_input.value = "99"
        valid = screen._validate()
        context_input.value = "bad"
        malformed = screen._validate()
        context_input.value = ""
        blank = screen._validate()

    assert "Max context tokens must be at least 100." in too_small
    assert "Max output tokens must be less than max context tokens." in overlap
    assert not any("must be at least" in error or "must be less than" in error for error in valid)
    assert not any("must be at least" in error or "must be less than" in error for error in malformed)
    assert not any("must be at least" in error or "must be less than" in error for error in blank)


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("messages", "[]", "'messages' is protected"),
        ("prompt", '{"id": "pmpt_1"}', "'prompt' is protected"),
        ("conversation_id", '"resp_1"', "'conversation_id' is protected"),
        ("extra_body", '{"input": []}', "extra_body contains protected key(s): input"),
        ("extra_body", '{"custom": true}', ""),
    ],
)
async def test_model_config_rejects_protected_chat_option_keys(key: str, value: str, expected: str) -> None:
    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        row = screen.query_one("#mc-options-list").query_one(".mc-kv-item-row")
        row.query_one(".mc-kv-key-input", Input).value = key
        row.query_one(".mc-kv-value-input", Input).value = value
        errors = screen._validate()

    if expected:
        assert any(expected in error for error in errors)
    else:
        assert not any("protected" in error for error in errors)


async def test_model_config_validate_rejects_duplicate_key_value_rows() -> None:
    """Duplicate keys should block Save instead of silently overwriting."""

    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        headers_container = screen.query_one("#mc-headers-list")
        fill_kv_row(headers_container, "X-Team", "platform")

        screen.query_one("#mc-hadd").press()
        await _wait_for_kv_rows(headers_container, pilot, 2)

        rows = list(headers_container.query(".mc-kv-item-row"))
        rows[1].query_one(".mc-kv-key-input", Input).value = "X-Team"
        rows[1].query_one(".mc-kv-value-input", Input).value = "infra"

        errors = screen._validate()

    assert "HTTP Extra Headers row 2: duplicate key 'X-Team'." in errors


@pytest.mark.parametrize("header_key", ["CHRYS_TRACE", X_SESSION_ID_HEADER])
async def test_model_config_validate_rejects_reserved_http_headers(header_key: str) -> None:
    """Header names the app owns are rejected in the modal, by prefix and by exact name."""

    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        headers_container = screen.query_one("#mc-headers-list")
        fill_kv_row(headers_container, header_key, "1")

        errors = screen._validate()

    assert f"HTTP Extra Headers row 1: header key '{header_key}' is reserved for {APP_DISPLAY_NAME}." in errors


async def test_model_config_validate_rejects_common_mapping_chat_options_that_are_not_objects() -> None:
    """Common mapping-valued chat options should fail in the modal, not during the next send."""

    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        options_container = screen.query_one("#mc-options-list")
        option_row = options_container.query_one(".mc-kv-item-row")
        option_row.query_one(".mc-kv-key-input", Input).value = "metadata"
        value_input = option_row.query_one(".mc-kv-value-input", Input)

        value_input.value = '"not-a-map"'
        non_object_errors = screen._validate()

        value_input.value = "{not-json"
        invalid_json_errors = screen._validate()

        value_input.value = '{"X-Team": "platform"}'
        valid_errors = screen._validate()

        option_row.query_one(".mc-kv-key-input", Input).value = "extra_body"
        value_input.value = '"not-a-map"'
        extra_body_errors = screen._validate()

        option_row.query_one(".mc-kv-key-input", Input).value = "extra_headers"
        value_input.value = '{"X-Config": {"nested": true}}'
        nested_header_errors = screen._validate()

        value_input.value = '{"X-Config": "{\\"nested\\": true}"}'
        valid_header_errors = screen._validate()

        value_input.value = '{"chrys-trace": "1"}'
        reserved_header_errors = screen._validate()

        value_input.value = f'{{"{MODEL_ID_HEADER}": "wrong"}}'
        reserved_model_header_errors = screen._validate()

        value_input.value = f'{{"{X_SESSION_ID_HEADER}": "wrong"}}'
        reserved_x_session_header_errors = screen._validate()

    assert "Chat Options row 1: 'metadata' must be a JSON object/map, got str." in non_object_errors
    assert any("'metadata' must be a valid JSON object/map" in error for error in invalid_json_errors)
    assert not any("metadata" in error for error in valid_errors)
    assert "Chat Options row 1: 'extra_body' must be a JSON object/map, got str." in extra_body_errors
    assert "Chat Options row 1: 'extra_headers' value for header 'X-Config' must be a string." in nested_header_errors
    assert not any("extra_headers" in error for error in valid_header_errors)
    assert (
        f"Chat Options row 1: 'extra_headers' header 'chrys-trace' is reserved for {APP_DISPLAY_NAME}."
        in reserved_header_errors
    )
    assert (
        f"Chat Options row 1: 'extra_headers' header '{MODEL_ID_HEADER}' is reserved for {APP_DISPLAY_NAME}."
        in reserved_model_header_errors
    )
    assert (
        f"Chat Options row 1: 'extra_headers' header '{X_SESSION_ID_HEADER}' is reserved for {APP_DISPLAY_NAME}."
        in reserved_x_session_header_errors
    )


async def test_model_config_validate_rejects_known_typed_chat_options() -> None:
    """Common chat options should be type-checked before save."""

    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        options_container = screen.query_one("#mc-options-list")
        option_row = options_container.query_one(".mc-kv-item-row")
        key_input = option_row.query_one(".mc-kv-key-input", Input)
        value_input = option_row.query_one(".mc-kv-value-input", Input)

        key_input.value = "temperature"
        value_input.value = '"hot"'
        temperature_errors = screen._validate()

        value_input.value = "0.7"
        valid_temperature_errors = screen._validate()

        key_input.value = "max_tokens"
        value_input.value = "0"
        max_tokens_errors = screen._validate()

        key_input.value = "max_output_tokens"
        value_input.value = "4096"
        max_output_tokens_errors = screen._validate()

        key_input.value = "max_completion_tokens"
        value_input.value = "4096"
        max_completion_tokens_errors = screen._validate()

        key_input.value = "top_p"
        value_input.value = "1.5"
        top_p_errors = screen._validate()

        key_input.value = "seed"
        value_input.value = "1.5"
        seed_errors = screen._validate()

        # Past Python's integer digit limit json.loads raises a plain ValueError.
        value_input.value = "1" * 5000
        oversized_seed_errors = screen._validate()

        key_input.value = "store"
        value_input.value = '"true"'
        bool_errors = screen._validate()

        key_input.value = "logit_bias"
        value_input.value = '{"42": 101}'
        logit_bias_errors = screen._validate()

        key_input.value = "stop"
        value_input.value = '["DONE"]'
        valid_stop_errors = screen._validate()

        key_input.value = "top_k"
        value_input.value = "0"
        ignored_provider_option_errors = screen._validate()

    assert "Chat Options row 1: 'temperature' must be a JSON number between 0.0 and 2.0." in temperature_errors
    assert not any("temperature" in error for error in valid_temperature_errors)
    assert (
        "Chat Options row 1: 'max_tokens' is not saved on model profiles — "
        "remove this row and set the Max Output Tokens field above instead." in max_tokens_errors
    )
    assert (
        "Chat Options row 1: 'max_output_tokens' is not saved on model profiles — "
        "remove this row and set the Max Output Tokens field above instead." in max_output_tokens_errors
    )
    assert (
        "Chat Options row 1: 'max_completion_tokens' is not saved on model profiles — "
        "remove this row and set the Max Output Tokens field above instead." in max_completion_tokens_errors
    )
    assert "Chat Options row 1: 'top_p' must be a JSON number between 0.0 and 1.0." in top_p_errors
    assert "Chat Options row 1: 'seed' must be a JSON integer." in seed_errors
    assert "Chat Options row 1: 'seed' must be a JSON integer." in oversized_seed_errors
    assert "Chat Options row 1: 'store' must be a JSON boolean (true or false)." in bool_errors
    assert (
        "Chat Options row 1: 'logit_bias' value for token '42' must be a JSON number between -100 and 100."
        in logit_bias_errors
    )
    assert not any("stop" in error for error in valid_stop_errors)
    assert not any("top_k" in error for error in ignored_provider_option_errors)


async def test_model_config_save_rejects_output_not_less_than_context(tmp_path: Path) -> None:
    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        screen.query_one("#mc-max-tokens", Input).value = "32000"
        screen.query_one("#mc-max-output-tokens", Input).value = "32000"
        captured = _capture_notifications(screen)
        screen.query_one("#mc-save", Button).press()
        await pilot.pause()

    assert ("error", "Max output tokens must be less than max context tokens.") in captured
    assert not (tmp_path / "models" / "model-a.yaml").exists()
    assert registry.get("model-a") is profile


@pytest.mark.parametrize("store", [True, False])
async def test_model_config_save_warns_only_for_responses_store_true(store: bool, tmp_path: Path) -> None:
    """Saving an OpenAI Responses profile warns about compaction only when store=true."""

    registry, _profile = single_profile_registry(
        provider="openai",
        api_style="responses",
        chat_options=json.dumps({"store": store}),
    )

    async with open_model_config(registry, global_default_profile_id="model-a") as (screen, pilot):
        captured = _capture_notifications(screen)
        screen.query_one("#mc-save", Button).press()
        await pilot.pause()

    assert ("information", "Model profile saved") in captured
    assert (tmp_path / "models" / "model-a.yaml").is_file()
    warnings = [message for severity, message in captured if severity == "warning"]
    if store:
        assert len(warnings) == 1
        assert "compaction" in warnings[0]
    else:
        assert warnings == []


async def test_model_config_validate_rejects_wire_unsafe_charsets() -> None:
    """Non-ASCII in key/model/header fields is caught at save time, not first chat."""

    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        screen.query_one("#mc-api-key", Input).value = "sk-abc▼def"
        screen.query_one("#mc-model", Input).value = "gpt▼4o"

        headers_container = screen.query_one("#mc-headers-list")
        fill_kv_row(headers_container, "X 密", "secret▼")

        options_container = screen.query_one("#mc-options-list")
        fill_kv_row(options_container, "extra_headers", '{"X-Custom": "秘密token"}')

        errors = screen._validate()

    joined = "\n".join(errors)
    # API key: position only, never the content.
    assert "API key contains a non-ASCII or control character at position 7" in joined
    assert "sk-abc" not in joined
    # Model ID: offending character is echoed.
    assert "Model ID contains" in joined
    assert "U+25BC" in joined
    # Extra header rows: name errors echo the name, value errors never
    # echo the value.
    assert "HTTP Extra Headers row 1: Header name" in joined
    assert "value contains a non-ASCII or control character" in joined
    assert "secret" not in joined
    # Chat-options extra_headers ride per-request headers and get the
    # same charset gate.
    assert "Chat Options row 1: 'extra_headers':" in joined
    assert "秘密" not in joined


async def test_model_config_validation_notifications_disable_markup() -> None:
    """Invalid header names remain literal text in save error toasts."""
    registry, _profile = single_profile_registry()

    async with open_model_config(registry) as (screen, pilot):
        header_row = screen.query_one("#mc-headers-list .mc-kv-item-row")
        header_row.query_one(".mc-kv-key-input", Input).value = "[/]"
        header_row.query_one(".mc-kv-value-input", Input).value = "value"

        captured: list[tuple[str, bool]] = []

        def _notify(
            message: str,
            *,
            title: str = "",
            severity: str = "information",
            timeout: float | None = None,
            markup: bool = True,
        ) -> None:
            captured.append((message, markup))

        screen.notify = _notify  # type: ignore[method-assign]
        screen.query_one("#mc-save", Button).press()
        await pilot.pause()

    assert captured
    assert "Header name '[/]'" in captured[0][0]
    assert captured[0][1] is False


async def test_model_config_validate_accepts_env_templates_and_printable_ascii() -> None:
    """Templates and permissive printable-ASCII values pass save-time validation."""

    registry = ModelProfileRegistry()
    profile = ModelProfile(
        id="model-a",
        name="Model A",
        model_id="huggingface/WizardLM/WizardCoder-Python-34B-V1.0",
    )
    registry.register(profile)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        screen.query_one("#mc-api-key", Input).value = "{{CHRYS_OPENAI_KEY}}"

        headers_container = screen.query_one("#mc-headers-list")
        fill_kv_row(headers_container, "X-Api-Key", "Bearer {{CHRYS_HEADER_TOKEN}}")

        errors = screen._validate()

    assert errors == []


async def test_model_config_validate_accepts_command_text_only_the_command_output_must_be_ascii() -> None:
    registry = ModelProfileRegistry()
    profile = ModelProfile(id="model-a", name="Model A", model_id="m")
    registry.register(profile)

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        screen.query_one("#mc-api-key", Input).value = "{{!cat ~/凭据/key}}"
        fill_kv_row(screen.query_one("#mc-headers-list"), "X-Api-Key", "Bearer {{!cat ~/凭据/key}}")
        fill_kv_row(screen.query_one("#mc-options-list"), "extra_headers", '{"X-T": "{{!cat ~/凭据/key}}"}')

        accepted = screen._validate()

        fill_kv_row(screen.query_one("#mc-headers-list"), "X-Api-Key", "秘密 {{!cat ~/凭据/key}}")
        refused = screen._validate()

    assert accepted == []
    assert any("value contains a non-ASCII or control character at position 1" in error for error in refused)


async def test_model_config_validate_rejects_non_ascii_chat_option_model() -> None:
    """A chat-options model override rides the Chrys-Model-Id header; same charset gate."""

    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, _pilot):
        options_container = screen.query_one("#mc-options-list")
        fill_kv_row(options_container, "model", "模型")

        errors = screen._validate()

    joined = "\n".join(errors)
    assert "Chat Options row 1: 'model':" in joined
    assert "U+6A21" in joined
