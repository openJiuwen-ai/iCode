# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for profile discovery CLI commands."""

from __future__ import annotations

import json
from typing import Any, ClassVar

import pytest

from chrys.app.cli import profiles as profiles_cli
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.orchestration.startup import RuntimeBootstrap
from chrys.service.profiles.agents.schema import AcpAgentConfig, AgentProfile, ModelConfig
from chrys.service.profiles.models.schema import ModelProfile


class FakeAgentRegistry:
    profiles: ClassVar[list[AgentProfile]] = []
    builtin_names: ClassVar[set[str]] = set()

    def load_all(self) -> int:
        return len(self.profiles)

    def list_profiles(self, *, include_sub_agent_only: bool = False) -> list[AgentProfile]:
        profiles = list(self.profiles)
        if not include_sub_agent_only:
            profiles = [profile for profile in profiles if not profile.sub_agent_only]
        return profiles

    def is_builtin(self, name: str) -> bool:
        return name in self.builtin_names


class FakeModelRegistry:
    profiles: ClassVar[list[ModelProfile]] = []

    def load_all(self) -> int:
        return len(self.profiles)

    def get(self, profile_id: str) -> ModelProfile | None:
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile
        return None

    def list_profiles(self) -> list[ModelProfile]:
        return list(self.profiles)


def _patch_runtime(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    def fake_bootstrap_runtime(*, configure_stdio: bool, setup_telemetry: bool) -> RuntimeBootstrap:
        assert configure_stdio is True
        assert setup_telemetry is False
        return RuntimeBootstrap(loaded=LoadedSettings(settings=settings, provenance={}))

    monkeypatch.setattr(profiles_cli, "bootstrap_runtime", fake_bootstrap_runtime)
    monkeypatch.setattr(profiles_cli, "AgentProfileRegistry", FakeAgentRegistry)
    monkeypatch.setattr(profiles_cli, "ModelProfileRegistry", FakeModelRegistry)


def test_agents_command_renders_main_and_sub_agent_profiles(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    FakeAgentRegistry.profiles = [
        AgentProfile(name="Code", id="code-id", display_name="Code Agent", description="General [coding]"),
        AgentProfile(
            name="QA",
            id="qa-id",
            display_name="QA Agent",
            description="Review assistant",
            model=ModelConfig(profile_id="model-id"),
        ),
        AgentProfile(
            name="Plan",
            id="plan-id",
            display_name="Plan Agent",
            sub_agent_only=True,
            acp=AcpAgentConfig(command="plan"),
        ),
    ]
    FakeAgentRegistry.builtin_names = {"Code", "Plan"}
    FakeModelRegistry.profiles = [ModelProfile(id="model-id", name="Friendly Model")]
    _patch_runtime(monkeypatch, Settings(default_agent="QA", locale="en"))

    assert profiles_cli.agents_main([]) == 0

    out = capsys.readouterr()
    assert "Code" in out.out
    assert "General [coding]" in out.out
    assert "QA" in out.out
    assert "Friendly Model" in out.out
    assert "Plan" in out.out
    for heading in ("Default", "Name", "Display Name", "ID", "Model", "Source", "Description"):
        assert heading in out.out


def test_agents_command_json_includes_kind_and_launchability(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    FakeAgentRegistry.profiles = [
        AgentProfile(name="Code", id="code-id"),
        AgentProfile(name="Plan", id="plan-id", sub_agent_only=True, acp=AcpAgentConfig(command="plan")),
    ]
    FakeAgentRegistry.builtin_names = {"Code", "Plan"}
    FakeModelRegistry.profiles = []
    _patch_runtime(monkeypatch, Settings(default_agent="Plan"))

    assert profiles_cli.agents_main(["list", "--json"]) == 0

    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload["default"] == "Code"
    assert [profile["name"] for profile in payload["agents"]] == ["Code", "Plan"]
    by_name = {profile["name"]: profile for profile in payload["agents"]}
    assert by_name["Code"]["kind"] == "kernel"
    assert by_name["Code"]["launchableAsMain"] is True
    assert by_name["Plan"]["kind"] == "acp"
    assert by_name["Plan"]["subAgentOnly"] is True
    assert by_name["Plan"]["launchableAsMain"] is False


def test_models_command_json_marks_active_model(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    FakeAgentRegistry.profiles = []
    FakeAgentRegistry.builtin_names = set()
    FakeModelRegistry.profiles = [
        ModelProfile(id="model-a", name="Alpha", provider="openai", model_id="gpt-alpha"),
        ModelProfile(
            id="model-b",
            name="Beta",
            provider="anthropic",
            model_id="claude-beta",
            stream=True,
            vision=True,
        ),
    ]
    _patch_runtime(monkeypatch, Settings(model_profile="Beta"))

    assert profiles_cli.models_main(["--json"]) == 0

    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload["active"] == "model-b"
    by_name = {profile["name"]: profile for profile in payload["models"]}
    assert by_name["Alpha"]["active"] is False
    assert by_name["Beta"]["active"] is True
    assert by_name["Beta"]["stream"] is True
    assert by_name["Beta"]["vision"] is True


def test_models_command_renders_rich_table(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    FakeAgentRegistry.profiles = []
    FakeAgentRegistry.builtin_names = set()
    FakeModelRegistry.profiles = [
        ModelProfile(id="model-b", name="Beta", provider="anthropic", model_id="claude-beta", max_context_tokens=200000)
    ]
    _patch_runtime(monkeypatch, Settings(model_profile="model-b", locale="en"))

    assert profiles_cli.models_main([]) == 0

    out = capsys.readouterr()
    assert "Beta" in out.out
    assert "model-b" in out.out
    assert "anthropic" in out.out
    assert "200k" in out.out
    for heading in ("Active", "ID", "Name", "Provider", "API", "Model", "Context", "Flags"):
        assert heading in out.out


@pytest.mark.parametrize(
    "locale",
    ["en", "zh-Hans"],
)
def test_empty_profile_list_messages_stay_english_across_locales(
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    locale: str,
) -> None:
    FakeAgentRegistry.profiles = []
    FakeAgentRegistry.builtin_names = set()
    FakeModelRegistry.profiles = []
    _patch_runtime(monkeypatch, Settings(locale=locale))

    assert profiles_cli.agents_main([]) == 0
    assert capsys.readouterr().out.strip() == "No agent profiles found."
    assert profiles_cli.models_main([]) == 0
    assert capsys.readouterr().out.strip() == "No model profiles found."


def test_profile_table_output_is_identical_english_across_locales(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    FakeAgentRegistry.profiles = [AgentProfile(name="Code", id="code-id", display_name="Code Agent")]
    FakeAgentRegistry.builtin_names = {"Code"}
    FakeModelRegistry.profiles = [ModelProfile(id="model-a", name="Alpha", provider="openai", model_id="gpt-alpha")]
    outputs: dict[str, tuple[str, str]] = {}

    for locale in ("en", "zh-Hans"):
        _patch_runtime(monkeypatch, Settings(locale=locale, model_profile="model-a"))
        assert profiles_cli.agents_main([]) == 0
        agent_output = capsys.readouterr().out
        assert profiles_cli.models_main([]) == 0
        model_output = capsys.readouterr().out
        outputs[locale] = (agent_output, model_output)

    assert outputs["en"] == outputs["zh-Hans"]
    for heading in ("Default", "Name", "Display Name", "ID", "Model", "Source", "Description"):
        assert heading in outputs["en"][0]
    for heading in ("Active", "ID", "Name", "Provider", "API", "Model", "Context", "Flags"):
        assert heading in outputs["en"][1]


@pytest.mark.parametrize(
    ("main", "command", "noun"),
    [(profiles_cli.agents_main, "agent", "agent profiles"), (profiles_cli.models_main, "model", "model profiles")],
)
def test_help_names_the_singular_command_and_its_list_subcommand(
    main: Any, command: str, noun: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith(f"usage: icode {command} ")
    assert f"List the {noun} (the default)" in out
