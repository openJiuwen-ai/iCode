# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The shipped workflow templates: floor-clean, pre-generated manifests current, admitted on the builtin profiles."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chrys.foundation.config.settings import Settings
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.workflows.admission import admit_manifest
from chrys.service.workflows.discovery import SOURCE_KIND_BUILTIN, discover_workflows, read_builtin_manifest
from chrys.service.workflows.graph import KIND_AGENT
from tests.support.workflow_builtins import builtin_manifest, builtin_templates, manifest_path
from tests.support.workflow_floor import violations

TEMPLATES = builtin_templates()
REGENERATE = "regenerate with: uv run python -m tests.support.workflow_builtins"


def test_the_shipped_template_set() -> None:
    assert [path.name for path in TEMPLATES] == ["demo-workflow.py"]


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.stem)
def test_templates_stay_stdlib_only_on_the_floor(template: Path) -> None:
    assert violations(template.read_text(encoding="utf-8"), path=template.name) == []


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.stem)
def test_templates_carry_no_copyright_header(template: Path) -> None:
    # Users copy a template as the start of their own workflow; a copyright line would travel with it.
    source = template.read_text(encoding="utf-8")
    assert "Copyright" not in source
    assert source.startswith("# ")


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.stem)
def test_pregenerated_manifests_are_current(template: Path) -> None:
    stored = json.loads(manifest_path(template).read_text(encoding="utf-8"))
    assert stored == builtin_manifest(template), REGENERATE
    assert stored["title"]


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda path: path.stem)
def test_templates_admit_on_the_builtin_agent_profiles(template: Path) -> None:
    agents = AgentProfileRegistry()
    agents.load_builtins()
    models = ModelProfileRegistry()
    models.register(ModelProfile(id="active-model", name="active", provider="mock", model_id="mock"))
    manifest = builtin_manifest(template)

    admitted = admit_manifest(
        manifest, agent_registry=agents, model_registry=models, settings=Settings(model_profile="active-model")
    )

    agent_nodes = [node["id"] for node in manifest["nodes"] if node["kind"] == KIND_AGENT]
    assert agent_nodes
    assert list(admitted.bindings) == agent_nodes
    assert {binding.model.id for binding in admitted.bindings.values() if binding.model is not None} == {"active-model"}


def test_discovery_lists_the_templates_as_builtin(tmp_path: Path) -> None:
    found = discover_workflows(config_dir=tmp_path, workdir=None)

    assert [(source.workflow_id, source.source_kind) for source in found.sources] == [
        (path.stem, SOURCE_KIND_BUILTIN) for path in TEMPLATES
    ]
    assert found.skipped == ()
    manifest = read_builtin_manifest("demo-workflow")
    assert manifest is not None
    assert manifest["title"] == "Workflow Demo · Project Tour"
    assert read_builtin_manifest("missing") is None
