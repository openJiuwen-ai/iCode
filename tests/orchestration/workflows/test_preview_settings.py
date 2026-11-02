# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Preview model badges use the target workspace and explicit workflow choices."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation import platform
from chrys.foundation.config.process_env import freeze_process_env
from chrys.foundation.config.settings_store import SettingsHandle, load_settings
from chrys.foundation.models.workflow_session import WorkflowModelSelection, WorkspaceSnapshot
from chrys.foundation.models.workspace import Workspace
from chrys.orchestration.workflows import settings as workflow_settings
from chrys.orchestration.workflows.settings import preview_bindings
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.workflows.sdk import WorkflowBuilder
from tests.orchestration.workflows._hosting import make_profile


@pytest.mark.parametrize("explicit", [False, True])
async def test_preview_resolves_target_project_fallback_without_chat_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit: bool
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.yaml").write_text(
        "project:\n  config_enabled: true\nmodel:\n  profile:\n    active: target\n", encoding="utf-8"
    )
    fake = replace(platform.get_platform(), config_dir=config)
    monkeypatch.setattr(platform, "get_platform", lambda: fake)
    for name in ("chat", "target"):
        directory = tmp_path / name / ".chrys"
        directory.mkdir(parents=True)
        (directory / "settings.yaml").write_text(
            f"llm:\n  retry:\n    max_transient: {2 if name == 'chat' else 0}\n", encoding="utf-8"
        )
    monkeypatch.delenv("CHRYS_MODEL_PROFILE", raising=False)
    freeze_process_env()
    startup = load_settings(project_root=tmp_path / "chat")
    handle = SettingsHandle(startup)
    handle.override(model_profile="chat", model_profile_override="chat", model_profile_override_sub_agents=True)
    agents, models = AgentProfileRegistry(), ModelProfileRegistry()
    agents.register(make_profile())
    for name in ("chat", "target", "chosen"):
        models.register(ModelProfile(id=name, name=name, provider="mock", model_id=f"model-{name}"))
    builder = WorkflowBuilder("preview")
    node = builder.agent("review", profile="Headless")
    builder.start(node)
    builder.output(node)
    selected = WorkflowModelSelection("chosen", "chosen", "model-chosen") if explicit else None
    admit = create_autospec(workflow_settings.admit_manifest, side_effect=workflow_settings.admit_manifest)
    monkeypatch.setattr(workflow_settings, "admit_manifest", admit)
    nodes = await preview_bindings(
        builder.build().manifest(),
        workspace=WorkspaceSnapshot.capture(Workspace.from_cwd(tmp_path / "target")),
        selected=selected,
        agent_registry=agents,
        model_registry=models,
        settings_handle=handle,
        startup=startup,
    )
    assert nodes[0]["model_id"] == ("model-chosen" if explicit else "model-target")
    assert admit.call_args.kwargs["settings"].max_transient_retries == 0
    assert handle.settings.max_transient_retries == 2
    assert handle.settings.model_profile == "chat"
    assert handle.settings.model_profile_override == "chat"
