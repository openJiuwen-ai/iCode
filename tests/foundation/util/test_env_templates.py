# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for runtime environment-template resolution."""

from __future__ import annotations

import pytest

from chrys.foundation.util.env_templates import (
    COMMAND_HANDLE_RE,
    EnvVarResolutionError,
    blank_commands,
    command_handles_in,
    command_placeholder,
    resolve_env_template_values,
    resolve_env_templates,
)


def test_resolve_env_templates_replaces_multiple_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_AUTH_TYPE", "Bearer")
    monkeypatch.setenv("CHRYS_AUTH_TOKEN", "abc123")

    assert resolve_env_templates("{{CHRYS_AUTH_TYPE}} {{CHRYS_AUTH_TOKEN}}", location="test field") == "Bearer abc123"


def test_resolve_env_templates_treats_empty_env_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_EMPTY_SECRET", "")

    with pytest.raises(EnvVarResolutionError, match="CHRYS_EMPTY_SECRET"):
        resolve_env_templates("{{CHRYS_EMPTY_SECRET}}", location="test field")


def test_resolve_env_templates_does_not_re_resolve_env_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_OUTER", "literal {{CHRYS_INNER}}")
    monkeypatch.setenv("CHRYS_INNER", "resolved")

    assert resolve_env_templates("{{CHRYS_OUTER}}", location="test field") == "literal {{CHRYS_INNER}}"


def test_resolve_env_templates_preserves_non_env_double_brace_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHRYS_TOKEN", "resolved")

    value = 'payload={{"kind":"json"}} token={{CHRYS_TOKEN}}'

    assert resolve_env_templates(value, location="test field") == 'payload={{"kind":"json"}} token=resolved'


def test_resolve_env_templates_preserves_hyphenated_double_brace_payload() -> None:
    assert resolve_env_templates("{{MY-TOKEN}}", location="test field") == "{{MY-TOKEN}}"


def test_resolve_env_templates_allows_escaped_literal_double_braces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHRYS_TOKEN", "resolved")

    value = 'payload={{{{"kind":"json"}}}} token={{CHRYS_TOKEN}}'

    assert resolve_env_templates(value, location="test field") == 'payload={{"kind":"json"}} token=resolved'


def test_resolve_env_templates_rejects_spaces_inside_braces() -> None:
    with pytest.raises(EnvVarResolutionError, match="no spaces"):
        resolve_env_templates("{{ CHRYS_TOKEN }}", location="test field")


def test_resolve_env_templates_rejects_empty_placeholder() -> None:
    with pytest.raises(EnvVarResolutionError, match="Invalid environment variable placeholder"):
        resolve_env_templates("{{}}", location="test field")


def test_resolve_env_templates_rejects_stray_closing_braces() -> None:
    with pytest.raises(EnvVarResolutionError, match="Invalid environment variable placeholder"):
        resolve_env_templates("value }}", location="test field")


def test_a_command_placeholder_resolves_to_a_stable_handle_inside_its_text() -> None:
    first = resolve_env_templates("Bearer {{!tool get token}}", location="test")
    second = resolve_env_templates("Bearer {{! tool get token }}", location="elsewhere")

    assert first == second
    assert first.startswith("Bearer ")
    handle = first.removeprefix("Bearer ")
    assert COMMAND_HANDLE_RE.fullmatch(handle)
    assert command_handles_in([first]) == {handle}
    assert command_placeholder(handle).command == "tool get token"
    assert command_placeholder(handle).location == "elsewhere"


def test_different_commands_get_different_handles() -> None:
    assert resolve_env_templates("{{!one}}", location="test") != resolve_env_templates("{{!two}}", location="test")


def test_an_environment_value_holding_a_command_placeholder_stays_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FROM_ENV", "{{!must-not-run}}")

    resolved = resolve_env_templates("{{FROM_ENV}}", location="test")

    assert resolved == "{{!must-not-run}}"
    assert command_handles_in([resolved]) == set()


def test_an_escaped_command_placeholder_stays_text() -> None:
    resolved = resolve_env_templates("{{{{!must-not-run}}}}", location="test")

    assert resolved == "{{!must-not-run}}"
    assert command_handles_in([resolved]) == set()


def test_text_shaped_like_a_handle_is_no_handle_without_its_placeholder() -> None:
    assert command_handles_in([f"chrys-cmd-{'0' * 32}"]) == set()


def test_a_command_writes_closing_braces_with_the_usual_escape() -> None:
    handle = resolve_env_templates("{{!jq '{a:{b:1}}}}'}}", location="test")

    assert command_placeholder(handle).command == "jq '{a:{b:1}}'"


def test_blanking_commands_hides_their_text_and_keeps_every_other_position() -> None:
    value = "Bearer {{!cat 凭据}} ▼ {{TOKEN}}"

    blanked = blank_commands(value)

    assert "凭据" not in blanked
    assert len(blanked) == len(value)
    assert blanked.index("▼") == value.index("▼")
    assert blanked.endswith("{{TOKEN}}")


@pytest.mark.parametrize(
    "value",
    [
        "{{!}}",
        "{{!   }}",
        "{{ !secret-command }}",
        "{{\t!secret-command}}",
        "{{\n!secret-command}}",
        "{{!secret-command",
    ],
)
def test_a_malformed_command_placeholder_is_refused_without_showing_the_command(value: str) -> None:
    with pytest.raises(EnvVarResolutionError) as raised:
        resolve_env_templates(value, location="test")

    assert "secret-command" not in str(raised.value)


def test_a_command_placeholder_is_refused_where_commands_are_not_allowed() -> None:
    with pytest.raises(EnvVarResolutionError, match="not supported in the place") as raised:
        resolve_env_templates("{{!secret-command}}", location="the place", allow_commands=False)

    assert "secret-command" not in str(raised.value)


def test_allow_commands_reaches_nested_values() -> None:
    with pytest.raises(EnvVarResolutionError):
        resolve_env_template_values({"a": [{"b": "{{!cmd}}"}]}, location="test", allow_commands=False)
