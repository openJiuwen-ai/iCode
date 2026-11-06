# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow model selection stays isolated while approval policy belongs to the launch."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from chrys.foundation import platform
from chrys.foundation.config.process_env import freeze_process_env
from chrys.foundation.config.settings_store import load_settings
from chrys.foundation.config.spec import Source
from chrys.foundation.events.types import SetModelProfile, SettingsReload, WorkflowRunStarted
from chrys.service.approval.policy import ApprovalMode
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import ModelConfig
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workflows.outcomes import RunOutcome
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_profile,
    make_project,
    of_type,
    patch_runtime,
    run,
    write_workflow,
)
from tests.support.workflow_workers import python_workflow


@pytest.mark.parametrize("layered", [False, True])
async def test_chat_model_switch_does_not_change_workflow_node_bindings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layered: bool
) -> None:
    patch_runtime(
        monkeypatch,
        [MockChatClient(responses=[]), *[MockChatClient(responses=[MockResponse(text="done")]) for _ in range(4)]],
    )
    project = make_project(tmp_path)
    write_workflow(
        project,
        "review",
        b"from chrys.workflows import WorkflowBuilder\n"
        b"wf = WorkflowBuilder('review')\n"
        b"bound = wf.agent('bound', profile='Bound')\n"
        b"fallback = wf.agent('fallback', profile='Headless')\n"
        b"wf.start(bound)\nwf.chain(bound, fallback)\nwf.output(fallback)\nworkflow = wf.build()\n",
    )
    loaded = load_settings(env={}).overlay(Source.SESSION, model_profile="mock-profile") if layered else None
    host = make_host(
        tmp_path,
        project=project,
        profiles=[make_profile(), replace(make_profile("Bound"), model=ModelConfig(profile_id="bound-model"))],
        loaded_settings=loaded,
    )
    registry = host.engine.model_registry
    assert registry is not None
    for identity in ("bound-model", "chat-model"):
        registry.register(ModelProfile(id=identity, name=identity, provider="mock", model_id=identity))
    try:
        await host.start()
        await confirm(host, "review")
        first, before = await run(host, "review")
        workflow_session_id = host.workflow_session_id
        await host.event_bus.publish(SetModelProfile(profile_id="chat-model"), raise_handler_errors=True)
        assert host.engine.settings.model_profile_override == "chat-model"
        # Embedders can also use runtime overrides. Those are still Chat-owned.
        host.engine.settings_handle.override(model_profile_override="chat-model", model_profile="chat-model")
        second, after = await run(host, "review")
        assert first.outcome is second.outcome is RunOutcome.COMPLETED
        assert host.workflow_session_id == workflow_session_id
        for events in (before, after):
            started = of_type(events, WorkflowRunStarted)[0]
            assert {node["node_id"]: node["model_profile_id"] for node in started.resolved_nodes} == {
                "bound": "bound-model",
                "fallback": "mock-profile",
            }
        assert host.engine.settings.model_profile_override == "chat-model"
    finally:
        await host.shutdown()


@pytest.mark.parametrize("launch_mode", [None, ApprovalMode.BYPASS])
async def test_reload_updates_saved_default_but_preserves_launch_policy_for_all_workflow_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launch_mode: ApprovalMode | None
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    path = config / "settings.yaml"
    path.write_text("approval:\n  default_mode: auto\n", encoding="utf-8")
    fake = replace(platform.get_platform(), config_dir=config)
    monkeypatch.setattr(platform, "get_platform", lambda: fake)
    freeze_process_env()
    project = make_project(tmp_path)
    write_workflow(project, "review", python_workflow("def check(text):\n    return text\n", "check"))
    loaded = load_settings(project_root=project).overlay(Source.SESSION, model_profile="mock-profile")
    host = make_host(tmp_path, project=project, loaded_settings=loaded, approval_mode=launch_mode)
    store = JsonFileStateStore(tmp_path / "sessions")
    try:
        await confirm(host, "review")
        result, _ = await run(host, "review")
        assert result.outcome is RunOutcome.COMPLETED
        original_id = host.workflow_session_id
        original = (await store.load_workflow_session(original_id)).encode()
        assert original is not None and "approval_mode" not in original
        expected = launch_mode or ApprovalMode.AUTO
        assert host.engine.approval_mode is expected

        path.write_text("approval:\n  default_mode: manual\n", encoding="utf-8")
        await host.event_bus.publish(SettingsReload(), raise_handler_errors=True)
        assert host.engine.settings.default_approval_mode == "manual"
        result, _ = await run(host, "review", new_session=True)
        assert result.outcome is RunOutcome.COMPLETED
        assert host.workflow_session_id != original_id
        fresh = (await store.load_workflow_session(host.workflow_session_id)).encode()
        assert fresh is not None and "approval_mode" not in fresh
        assert host.engine.approval_mode is expected

        await host.load_workflow_session(original_id)
        result, _ = await run(host, "review")
        assert result.outcome is RunOutcome.COMPLETED
        restored = (await store.load_workflow_session(original_id)).encode()
        assert restored is not None and "approval_mode" not in restored
        assert host.engine.approval_mode is expected
    finally:
        await host.shutdown()
