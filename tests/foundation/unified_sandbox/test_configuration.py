# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Versioned policy resolution at the trust-separated configuration boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from chrys.foundation.config.spec import Source
from chrys.foundation.sandbox.configuration import resolve_configuration
from chrys.foundation.sandbox.models import Capability, FailurePolicy, JiuwenboxOptions, SandboxError


def policy() -> dict[str, object]:
    return {
        "version": 1,
        "enabled": True,
        "profile": "local-files",
        "profiles": {
            "local-files": {"backend": "process", "require": {"file_tools.read_boundary": True}},
            "isolated-shell": {"backend": "jiuwenbox", "options": {"network": "offline"}},
        },
    }


def test_precedence_and_per_field_authority() -> None:
    decision = resolve_configuration(
        policy(),
        environment={"enabled": "off", "profile": "isolated-shell", "on_unavailable": "warn"},
        cli={"enabled": True, "on_unavailable": "refuse"},
    )
    assert decision is not None
    assert decision.enabled is True
    assert decision.backend == "jiuwenbox"
    assert decision.options == JiuwenboxOptions(network="offline")
    assert decision.on_unavailable == FailurePolicy.REFUSE
    assert dict(decision.origins) == {
        "enabled": Source.CLI,
        "profile": Source.ENV,
        "on_unavailable": Source.CLI,
        "profiles": Source.USER,
    }


@pytest.mark.parametrize("value", [True, 1, "  true ", "yes", "ON"])
def test_shared_boolean_grammar(value: object) -> None:
    decision = resolve_configuration(policy(), environment={"enabled": value})
    assert decision is not None and decision.enabled is True


@pytest.mark.parametrize("value", [False, 0, "  false ", "no", "OFF"])
def test_explicit_disabled_values(value: object) -> None:
    decision = resolve_configuration(policy(), cli={"enabled": value})
    assert decision is not None and decision.enabled is False


@pytest.mark.parametrize("value", [None, "", "maybe", 2, [], {}])
def test_invalid_boolean_is_not_silently_disabled_or_shadowed(value: object) -> None:
    with pytest.raises(SandboxError, match="enabled must be a boolean"):
        resolve_configuration(policy(), environment={"enabled": value}, cli={"enabled": True})


@pytest.mark.parametrize("value", [True, 2, "1", None])
def test_version_is_an_exact_supported_integer(value: object) -> None:
    with pytest.raises(SandboxError, match=r"sandbox\.version"):
        resolve_configuration({**policy(), "version": value})


@pytest.mark.parametrize(
    "injection",
    [
        {"enabled": False},
        {"profile": "isolated-shell", "on_unavailable": "warn"},
        {"profiles": {"local-files": {"backend": "jiuwenbox", "options": {"url": "https://attacker.example"}}}},
        {"version": 999, "options": {"token": "project-secret"}},
    ],
)
def test_workdir_never_changes_trusted_decision(injection: dict[str, object]) -> None:
    base = resolve_configuration(policy())
    decision = resolve_configuration(policy(), workdir=injection)
    assert base is not None and decision is not None
    assert decision.enabled == base.enabled
    assert decision.profile == base.profile
    assert decision.backend == base.backend
    assert decision.on_unavailable == base.on_unavailable
    assert decision.required == base.required
    assert decision.options == base.options
    assert decision.origins == base.origins
    assert decision.diagnostics == ("workdir_sandbox_ignored",)


def test_absent_section_is_explicit_legacy_handoff_not_a_disabled_decision() -> None:
    assert resolve_configuration(None, workdir={"enabled": True}) is None
    with pytest.raises(SandboxError, match="missing_configuration"):
        resolve_configuration(None, cli={"enabled": True})
    with pytest.raises(SandboxError, match="legacy"):
        resolve_configuration({"files": {"enabled": True}})
    with pytest.raises(SandboxError, match="legacy"):
        resolve_configuration({"enabled": True, "url": ""})


@pytest.mark.parametrize(
    "changes",
    [
        {"profile": "missing"},
        {"profile": "../module"},
        {"profile": None},
        {"on_unavailable": "ignore"},
        {"on_unavailable": []},
        {"files": {"enabled": True}},
        {"token": "secret"},
    ],
)
def test_invalid_or_mixed_configuration_is_rejected(changes: dict[str, object]) -> None:
    with pytest.raises(SandboxError):
        resolve_configuration({**policy(), **changes})


def test_overrides_cannot_define_profiles_or_redirect_endpoints() -> None:
    for override in ({"profiles": {}}, {"backend": "jiuwenbox"}, {"url": "https://attacker.example"}):
        with pytest.raises(SandboxError, match="overrides may only"):
            resolve_configuration(policy(), environment=override)
        with pytest.raises(SandboxError, match="overrides may only"):
            resolve_configuration(policy(), cli=override)


def test_inactive_backend_needs_no_installed_package_or_known_schema() -> None:
    doc = policy()
    doc["profiles"] = {
        "local-files": {"backend": "process"},
        "future": {"backend": "future-engine", "options": {"new-option": {"nested": "value"}}},
    }
    decision = resolve_configuration(doc)
    assert decision is not None and decision.backend == "process"
    with pytest.raises(SandboxError, match="unknown_backend"):
        resolve_configuration(doc, cli={"profile": "future"})


@pytest.mark.parametrize(
    "profile",
    [
        None,
        {"backend": "process", "extra": True},
        {"backend": "../plugin"},
        {"backend": "process", "options": []},
        {"backend": "process", "require": {"typo": False}},
        {"backend": "process", "require": {"shell.write_boundary": "false"}},
    ],
)
def test_inactive_profiles_still_have_structural_and_requirement_validation(profile: object) -> None:
    doc = {**policy(), "profiles": {"local-files": {"backend": "process"}, "inactive": profile}}
    with pytest.raises(SandboxError):
        resolve_configuration(doc)


def test_required_is_a_snapshot_and_false_does_not_disable_capabilities() -> None:
    requirements = {"file_tools.read_boundary": True, "shell.write_boundary": False}
    doc = {**policy(), "profiles": {"local-files": {"backend": "process", "require": requirements}}}
    original = deepcopy(doc)
    decision = resolve_configuration(doc)
    assert decision is not None
    assert doc == original
    requirements["shell.write_boundary"] = True
    assert decision.required == frozenset({Capability.FILE_READ})
    with pytest.raises(FrozenInstanceError):
        decision.enabled = False


def test_options_snapshot_and_repr_do_not_expose_secrets() -> None:
    options = {"url": "https://private.example:8443", "token": "test-sensitive-token"}
    doc = {**policy(), "profiles": {"local-files": {"backend": "jiuwenbox", "options": options}}}
    decision = resolve_configuration(doc)
    assert decision is not None
    options["token"] = "changed"
    assert decision.options == JiuwenboxOptions(url="https://private.example:8443", token="test-sensitive-token")
    assert "private.example" not in repr(decision)
    assert "test-sensitive-token" not in repr(decision.options)


@pytest.mark.parametrize(
    "backend,options",
    [
        ("process", {"network": "offline"}),
        ("jiuwenbox", {"network": "invalid"}),
        ("jiuwenbox", {"token": 123}),
        ("jiuwenbox", {"unknown-secret-option": "test-sensitive-token"}),
        ("jiuwenbox", {"url": "https://user:test-sensitive-token@host.example"}),
        ("jiuwenbox", {"url": "file:///test-sensitive-token"}),
        ("jiuwenbox", {"url": "https://host.example/?token=test-sensitive-token"}),
        ("jiuwenbox", {"url": "https://host.example:99999"}),
        ("jiuwenbox", {"url": "https://host.example\n"}),
    ],
)
def test_selected_options_are_typed_and_errors_do_not_echo_values(backend: str, options: dict[str, object]) -> None:
    doc = {**policy(), "profiles": {"local-files": {"backend": backend, "options": options}}}
    with pytest.raises(SandboxError, match="invalid_options") as error:
        resolve_configuration(doc)
    assert "test-sensitive-token" not in str(error.value)


def test_disabled_config_still_rejects_unknown_selected_backend() -> None:
    doc = {**policy(), "enabled": False, "profiles": {"local-files": {"backend": "unknown"}}}
    with pytest.raises(SandboxError, match="unknown_backend"):
        resolve_configuration(doc)
