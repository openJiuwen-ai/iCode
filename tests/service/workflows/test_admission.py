# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Manifest admission: the spec digest, agent/model binding per agent node, and the deterministic rejections."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chrys.foundation.config.settings import Settings
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AcpAgentConfig, AgentProfile, ModelConfig, ToolsConfig
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.workflows.admission import (
    REJECT_AGENT_PROFILE_MISSING,
    REJECT_MANIFEST_INVALID,
    REJECT_MODEL_UNRESOLVABLE,
    AdmissionError,
    admit_manifest,
    spec_digest,
)
from chrys.service.workflows.sdk import WorkflowBuilder


def _manifest(*, model: str | None = None, suffix: str | None = None, second_agent: bool = False) -> dict[str, Any]:
    wf = WorkflowBuilder("admit")
    prepare = wf.python("prepare", lambda text: text)
    review = wf.agent("review", profile="Reviewer", model=model, instructions_suffix=suffix)
    wf.start(prepare)
    wf.chain(prepare, review)
    if second_agent:
        judge = wf.agent("judge", profile="reviewer")
        wf.chain(review, judge)
        wf.output(judge)
    else:
        wf.output(review)
    return wf.build().manifest()


def _registries(
    *, agent_model: str | None = None, sub_agent_only: bool = False, acp: AcpAgentConfig | None = None
) -> tuple[AgentProfileRegistry, ModelProfileRegistry, Settings]:
    agents = AgentProfileRegistry()
    agents.register(
        AgentProfile(
            name="Reviewer",
            display_name="PR Reviewer",
            instructions="Review.",
            tools=ToolsConfig(builtins=[]),
            model=ModelConfig(profile_id=agent_model or ""),
            sub_agent_only=sub_agent_only,
            acp=acp,
        )
    )
    models = ModelProfileRegistry()
    models.register(ModelProfile(id="default-model", name="default", provider="mock", model_id="m-default"))
    models.register(ModelProfile(id="fast-model", name="fast", provider="mock", model_id="m-fast"))
    return agents, models, Settings(model_profile="default-model")


def test_spec_digest_covers_every_component_of_the_triple() -> None:
    base = spec_digest("e" * 64, "m" * 64, 1)
    assert base == spec_digest("e" * 64, "m" * 64, 1)
    assert (
        len(
            {
                base,
                spec_digest("f" * 64, "m" * 64, 1),
                spec_digest("e" * 64, "n" * 64, 1),
                spec_digest("e" * 64, "m" * 64, 2),
            }
        )
        == 4
    )


def test_admission_binds_agent_nodes_in_node_order_with_a_data_only_snapshot() -> None:
    agents, models, settings = _registries()
    admitted = admit_manifest(
        _manifest(suffix="Be terse.", second_agent=True),
        agent_registry=agents,
        model_registry=models,
        settings=settings,
    )
    assert list(admitted.bindings) == ["review", "judge"]
    review = admitted.binding("review")
    assert review.agent.name == "Reviewer"
    assert review.model.id == "default-model"
    assert review.instructions_suffix == "Be terse."
    assert admitted.binding("judge").agent is review.agent  # caseless selector resolves to the same profile
    snapshot = admitted.resolved_nodes()[0]
    assert snapshot == {
        "node_id": "review",
        "agent": "Reviewer",
        "agent_id": review.agent.id,
        "agent_display_name": "PR Reviewer",
        "acp": False,
        "model_profile_id": "default-model",
        "model_profile_name": "default",
        "provider": "mock",
        "api_style": review.model.api_style,
        "model_id": "m-default",
        "base_url_origin": snapshot["base_url_origin"],
        "instructions_suffix": "Be terse.",
    }
    assert all(isinstance(value, str | bool) for value in snapshot.values())
    with pytest.raises(KeyError):
        admitted.binding("prepare")


def test_a_node_level_model_overrides_the_profile_binding() -> None:
    agents, models, settings = _registries(agent_model="default-model")
    admitted = admit_manifest(_manifest(model="fast"), agent_registry=agents, model_registry=models, settings=settings)
    assert admitted.binding("review").model.id == "fast-model"


def test_a_python_only_manifest_admits_with_no_bindings() -> None:
    wf = WorkflowBuilder("plain")
    node = wf.python("only", lambda text: text)
    wf.start(node)
    wf.output(node)
    agents, models, settings = _registries()
    admitted = admit_manifest(wf.build().manifest(), agent_registry=agents, model_registry=models, settings=settings)
    assert admitted.bindings == {}
    assert admitted.graph.title == "plain"


@pytest.mark.parametrize(
    ("manifest", "code"),
    [
        ({"schema_version": 999}, REJECT_MANIFEST_INVALID),
        ({}, REJECT_MANIFEST_INVALID),
    ],
)
def test_an_invalid_manifest_is_rejected_before_any_binding(manifest: dict[str, Any], code: str) -> None:
    agents, models, settings = _registries()
    with pytest.raises(AdmissionError) as info:
        admit_manifest(manifest, agent_registry=agents, model_registry=models, settings=settings)
    assert info.value.code == code


def test_a_missing_agent_profile_is_rejected_by_node() -> None:
    _agents, models, settings = _registries()
    with pytest.raises(AdmissionError) as info:
        admit_manifest(_manifest(), agent_registry=AgentProfileRegistry(), model_registry=models, settings=settings)
    assert info.value.code == REJECT_AGENT_PROFILE_MISSING
    assert "'review'" in info.value.message
    assert "'Reviewer'" in info.value.message


def test_an_unknown_node_model_is_rejected() -> None:
    agents, models, settings = _registries()
    with pytest.raises(AdmissionError) as info:
        admit_manifest(_manifest(model="nope"), agent_registry=agents, model_registry=models, settings=settings)
    assert info.value.code == REJECT_MODEL_UNRESOLVABLE
    assert "'nope'" in info.value.message


def test_a_profile_that_resolves_to_no_usable_model_is_rejected() -> None:
    agents, _models, _settings = _registries()
    with pytest.raises(AdmissionError) as info:
        admit_manifest(_manifest(), agent_registry=agents, model_registry=ModelProfileRegistry(), settings=Settings())
    assert info.value.code == REJECT_MODEL_UNRESOLVABLE
    assert "'Reviewer'" in info.value.message


def test_a_broken_agent_yaml_is_named_in_the_admission_rejection(tmp_path: Path) -> None:
    """A node whose selector missed must name a YAML file that could not be read.

    ``admission`` lives in service and cannot import the orchestration error
    builder, so it assembles the same facts through the shared diagnostics.
    """
    broken = tmp_path / "Reviewer.yaml"
    broken.write_text("name: [unclosed\n", encoding="utf-8")
    agents = AgentProfileRegistry()
    agents.load_user_profiles(tmp_path)
    _agents, models, settings = _registries()

    with pytest.raises(AdmissionError) as info:
        admit_manifest(_manifest(), agent_registry=agents, model_registry=models, settings=settings)

    assert info.value.code == REJECT_AGENT_PROFILE_MISSING
    assert info.value.node_id == "review"
    assert str(broken) in info.value.message
    assert "Could not read these profile files" in info.value.message


def test_a_broken_model_yaml_is_named_in_the_admission_rejection(tmp_path: Path) -> None:
    """The model branch gets the same diagnosis as the agent branch."""
    broken = tmp_path / "nope.yaml"
    broken.write_text("id: [unclosed\n", encoding="utf-8")
    models = ModelProfileRegistry()
    target, scan = models.read_profiles(tmp_path)
    models.install_profiles(target, scan)
    agents, _models, settings = _registries()

    with pytest.raises(AdmissionError) as info:
        admit_manifest(_manifest(model="nope"), agent_registry=agents, model_registry=models, settings=settings)

    assert info.value.code == REJECT_MODEL_UNRESOLVABLE
    assert info.value.node_id == "review"
    assert str(broken) in info.value.message
    assert "Could not read these profile files" in info.value.message


def test_a_model_the_agent_resolves_to_names_a_broken_file_it_could_not_parse(tmp_path: Path) -> None:
    """An agent's own model binding fails exactly like an explicit one, and says why.

    A corrupt model file leaves no profile behind, so the agent's binding falls back
    to the session default, which is unusable — the same rejection as a machine with
    nothing configured. Naming the file is the only way to tell the two apart.
    """
    broken = tmp_path / "gone.yaml"
    broken.write_text("id: [unclosed\n", encoding="utf-8")
    models = ModelProfileRegistry()
    target, scan = models.read_profiles(tmp_path)
    models.install_profiles(target, scan)
    agents, _models, _settings = _registries(agent_model="gone")

    with pytest.raises(AdmissionError) as info:
        admit_manifest(_manifest(), agent_registry=agents, model_registry=models, settings=Settings())

    assert info.value.code == REJECT_MODEL_UNRESOLVABLE
    assert info.value.node_id == "review"
    assert str(broken) in info.value.message
    assert "Could not read these profile files" in info.value.message
    assert "'Reviewer'" in info.value.message


def test_a_model_the_agent_resolves_to_keeps_the_old_message_when_no_file_is_broken() -> None:
    """Nothing else reports an unreadable file, so the list stays out of this message.

    Adding the available list here would print it twice — the caller's hint already
    carries it — and would break every caller that matches this text.
    """
    agents, _models, _settings = _registries(agent_model="gone")

    with pytest.raises(AdmissionError) as info:
        admit_manifest(_manifest(), agent_registry=agents, model_registry=ModelProfileRegistry(), settings=Settings())

    assert info.value.message == ("Node 'review' has no usable model: agent profile 'Reviewer' resolves to no model.")


def test_the_available_model_list_leaves_out_profiles_a_node_cannot_select() -> None:
    """A profile with no model id is rejected at admission, so naming it sends the reader nowhere."""
    agents, _models, settings = _registries()
    models = ModelProfileRegistry()
    models.register(ModelProfile(id="unconfigured", name="Unconfigured"))
    models.register(ModelProfile(id="ready", name="Ready", provider="mock", model_id="m"))

    with pytest.raises(AdmissionError) as info:
        admit_manifest(_manifest(model="nope"), agent_registry=agents, model_registry=models, settings=settings)

    assert "Available model profiles: Ready (ready)" in info.value.message
    assert "Unconfigured" not in info.value.message


def test_two_model_profiles_sharing_a_name_are_told_apart_by_id() -> None:
    """``Same, Same`` says nothing; the id is the only way to choose, as ``workflow validate`` prints it."""
    agents, _models, settings = _registries()
    models = ModelProfileRegistry()
    models.register(ModelProfile(id="first-id", name="Same", provider="mock", model_id="m-one"))
    models.register(ModelProfile(id="second-id", name="Same", provider="mock", model_id="m-two"))

    with pytest.raises(AdmissionError) as info:
        admit_manifest(_manifest(model="nope"), agent_registry=agents, model_registry=models, settings=settings)

    assert "Available model profiles: Same (first-id), Same (second-id)" in info.value.message


def test_an_agent_node_stripped_of_its_agent_entry_is_a_manifest_error() -> None:
    manifest = _manifest()
    for raw in manifest["nodes"]:
        if raw["id"] == "review":
            del raw["agent"]
    agents, models, settings = _registries()
    with pytest.raises(AdmissionError) as info:
        admit_manifest(manifest, agent_registry=agents, model_registry=models, settings=settings)
    assert info.value.code == REJECT_MANIFEST_INVALID


def test_a_sub_agent_only_profile_is_a_valid_node_target() -> None:
    agents, models, settings = _registries(sub_agent_only=True)
    admitted = admit_manifest(_manifest(), agent_registry=agents, model_registry=models, settings=settings)
    assert list(admitted.bindings) == ["review"]
    assert admitted.binding("review").agent.sub_agent_only is True


def test_an_external_agent_profile_needs_no_kernel_model() -> None:
    agents, _models, _settings = _registries(
        sub_agent_only=True, acp=AcpAgentConfig(command="acp-agent", model_id="remote-model")
    )
    admitted = admit_manifest(_manifest(), agent_registry=agents, model_registry=None, settings=Settings())
    binding = admitted.binding("review")
    assert binding.agent.acp is not None
    assert binding.model is None
    assert binding.snapshot()["acp"] is True
    assert binding.snapshot()["model_profile_id"] == ""
    assert binding.snapshot()["model_id"] == "remote-model"


def test_explicit_model_override_on_acp_resolves_the_same_selector_as_kernel_nodes() -> None:
    agents, models, settings = _registries(acp=AcpAgentConfig(command="acp-agent"))
    admitted = admit_manifest(_manifest(model="fast"), agent_registry=agents, model_registry=models, settings=settings)
    assert admitted.binding("review").model is not None
    assert admitted.binding("review").model.model_id == "m-fast"
