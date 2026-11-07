# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Relay guard: the save-time invariant tying relay profiles to an independent approval judge."""

from __future__ import annotations

import pytest

from chrys.service.profiles.models.relay_guard import (
    check_profile_delete,
    check_profile_write,
    effective_host,
    issue_text,
    relay_guard_issues,
)
from chrys.service.profiles.models.schema import ModelProfile

_PROVIDER_ENV_VARS = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "ZAI_API_KEY",
    "OPENAI_BASE_URL",
    "ANTHROPIC_BASE_URL",
    "DEEPSEEK_BASE_URL",
    "ZAI_BASE_URL",
)


def _relay(**overrides: object) -> ModelProfile:
    fields: dict[str, object] = {
        "id": "relay",
        "name": "Relay",
        "model_id": "gpt-x",
        "base_url": "https://relay.example.com/v1",
        "api_key": "sk-relay",
        "is_relay": True,
    }
    fields.update(overrides)
    return ModelProfile(**fields)  # type: ignore[arg-type]


def _judge(**overrides: object) -> ModelProfile:
    fields: dict[str, object] = {
        "id": "judge",
        "name": "Judge",
        "model_id": "gpt-y",
        "base_url": "https://direct.example.com/v1",
        "api_key": "sk-judge",
    }
    fields.update(overrides)
    return ModelProfile(**fields)  # type: ignore[arg-type]


def _codes(issues: list) -> list[str]:
    return [issue.code for issue in issues]


@pytest.fixture(autouse=True)
def _no_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host and credential checks must not depend on the developer's exported provider variables."""
    for var in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.mark.parametrize("selector", ["", "   "], ids=["empty", "blank"])
def test_relay_without_judge_selector_is_rejected(selector: str) -> None:
    assert _codes(relay_guard_issues([_relay()], selector)) == ["judge_unset"]


def test_selector_without_relays_stays_free() -> None:
    assert relay_guard_issues([_judge()], "") == []


def test_unresolved_judge_selector_is_rejected() -> None:
    issues = relay_guard_issues([_relay(), _judge()], "missing")

    assert _codes(issues) == ["judge_unresolved"]
    assert "missing" in issues[0].text


@pytest.mark.parametrize(
    ("selector", "field"),
    [
        pytest.param("judge", "id", id="by-id"),
        pytest.param("Judge", "name", id="by-name"),
        pytest.param("judge", "casefold", id="by-casefolded-name"),
    ],
)
def test_judge_resolves_by_id_or_unique_name(selector: str, field: str) -> None:
    name = "JUDGE" if field == "casefold" else "Judge"
    assert relay_guard_issues([_relay(), _judge(name=name)], selector) == []


def test_ambiguous_judge_name_is_rejected() -> None:
    profiles = [_relay(), _judge(id="j1", name="Dup"), _judge(id="j2", name="Dup")]

    assert _codes(relay_guard_issues(profiles, "Dup")) == ["judge_unresolved"]


def test_judge_pointing_at_the_relay_itself_is_rejected() -> None:
    assert "judge_is_relay" in _codes(relay_guard_issues([_relay(), _judge()], "relay"))


def test_second_relay_profile_is_not_an_independent_judge() -> None:
    other_relay = _relay(id="relay2", name="Relay 2", base_url="https://other.example.com/v1")

    assert "judge_is_relay" in _codes(relay_guard_issues([_relay(), other_relay], "relay2"))


def test_mock_judge_is_rejected_without_field_noise() -> None:
    mock = _judge(provider="mock", base_url="", api_key="", model_id="")

    assert _codes(relay_guard_issues([_relay(), mock], "judge")) == ["judge_mock"]


def test_blank_draft_judge_is_rejected() -> None:
    draft = _judge(model_id="")

    assert "judge_not_configured" in _codes(relay_guard_issues([_relay(), draft], "judge"))


def test_judge_with_disabled_tls_verification_is_rejected() -> None:
    insecure = _judge(verify_ssl=False)

    assert "judge_tls_disabled" in _codes(relay_guard_issues([_relay(), insecure], "judge"))


def test_judge_without_any_credential_source_is_rejected() -> None:
    bare = _judge(api_key="")

    assert "judge_no_credentials" in _codes(relay_guard_issues([_relay(), bare], "judge"))


def test_judge_with_provider_env_credential_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    bare = _judge(api_key="")

    assert relay_guard_issues([_relay(), bare], "judge") == []


def test_judge_with_api_key_template_counts_as_credentialed() -> None:
    templated = _judge(api_key="{{OPENAI_JUDGE_KEY}}")

    assert relay_guard_issues([_relay(), templated], "judge") == []


def test_judge_on_loopback_endpoint_needs_no_key() -> None:
    local = _judge(base_url="http://127.0.0.1:11434/v1", api_key="")

    assert relay_guard_issues([_relay(), local], "judge") == []


@pytest.mark.parametrize(
    "judge_url",
    [
        pytest.param("https://relay.example.com/other-path", id="same-host-other-path"),
        pytest.param("https://relay.example.com:8443/v1", id="same-host-other-port"),
    ],
)
def test_same_host_is_not_independent(judge_url: str) -> None:
    same_host = _judge(base_url=judge_url)

    assert "judge_host_shared" in _codes(relay_guard_issues([_relay(), same_host], "judge"))


def test_same_host_across_protocols_is_not_independent() -> None:
    anthropic_judge = _judge(provider="anthropic", base_url="https://relay.example.com/v1")

    assert "judge_host_shared" in _codes(relay_guard_issues([_relay(), anthropic_judge], "judge"))


def test_default_base_urls_resolve_before_comparing_hosts() -> None:
    # Both rely on the openai default endpoint through different layers: the
    # relay pins the host explicitly, the judge falls back to the provider
    # default. Raw-string comparison would miss the shared host.
    relay = _relay(base_url="https://api.openai.com/v1")
    judge = _judge(base_url="")

    assert "judge_host_shared" in _codes(relay_guard_issues([relay, judge], "judge"))


def test_judge_with_unusable_address_is_rejected() -> None:
    malformed = _judge(base_url="not-a-url")

    assert "judge_url_invalid" in _codes(relay_guard_issues([_relay(), malformed], "judge"))


def test_effective_host_ignores_scheme_and_port() -> None:
    assert effective_host(_judge(base_url="https://Direct.EXAMPLE.com:8443/v1")) == "direct.example.com"
    assert effective_host(_judge(base_url="")) == "api.openai.com"
    assert effective_host(_judge(base_url="not-a-url")) is None


def test_write_validates_the_prospective_set_not_the_stored_one() -> None:
    # The only existing profile is being promoted to a relay while the judge
    # selector still points at it: the stored record is a valid judge, the
    # prospective one is not.
    promoted = _judge(is_relay=True)

    issues = check_profile_write([_judge()], promoted, "judge")

    assert "judge_is_relay" in _codes(issues)


def test_write_appending_a_relay_with_valid_judge_passes() -> None:
    assert check_profile_write([_relay(), _judge()], _relay(id="relay2"), "judge") == []


def test_write_of_unrelated_profile_never_blocks_repair_of_a_broken_set() -> None:
    # The set is already invalid (relay present, no judge selected — e.g. a
    # hand-edited YAML); writing a third, non-judge profile must stay open or
    # the user could never create the future judge.
    third = _judge(id="third", name="Third")

    assert check_profile_write([_relay()], third, "") == []


def test_write_of_the_judge_profile_rechecks_its_own_qualification() -> None:
    # Editing the judge's address onto the relay host breaks the relays that
    # depend on it, so the non-relay write is still refused.
    degraded = _judge(base_url="https://relay.example.com/v1")

    assert "judge_host_shared" in _codes(check_profile_write([_relay(), _judge()], degraded, "judge"))


def test_write_turning_the_judge_into_a_relay_is_rejected_via_prospective_set() -> None:
    issues = check_profile_write([_relay(), _judge()], _judge(is_relay=True), "judge")

    assert "judge_is_relay" in _codes(issues)


def test_write_of_the_judge_without_any_relays_stays_free() -> None:
    # With no relay profiles anywhere the invariant is vacuous: ordinary edits
    # to an existing judge (rename, mock provider, dropped key) must stay as
    # accepted as they were before the relay guard existed.
    renamed_mock = _judge(provider="mock", name="Mock Judge")

    assert check_profile_write([], renamed_mock, "Mock Judge") == []
    assert check_profile_write([_judge()], renamed_mock, "judge") == []


def test_delete_of_the_judge_names_the_dependency() -> None:
    issues = check_profile_delete([_relay(), _judge()], "judge", "judge")

    assert _codes(issues) == ["judge_delete_dependency"]
    assert "Judge" in issues[0].text


def test_delete_of_the_judge_without_remaining_relays_is_allowed() -> None:
    assert check_profile_delete([_judge()], "judge", "judge") == []


def test_delete_of_the_last_relay_is_allowed() -> None:
    assert check_profile_delete([_relay(), _judge()], "relay", "judge") == []


def test_delete_of_unrelated_profile_keeps_the_invariant() -> None:
    other = _judge(id="other", name="Other")
    assert check_profile_delete([_relay(), _judge(), other], "other", "judge") == []


def test_delete_of_unrelated_profile_with_broken_selection_stays_open() -> None:
    # Deleting a non-judge must not be blocked by an already-broken selection.
    assert check_profile_delete([_relay(), _judge()], "judge", "") == []


def test_issue_text_joins_plain_english_reasons() -> None:
    issues = relay_guard_issues([_relay()], "")

    assert issue_text(issues) == issues[0].text
