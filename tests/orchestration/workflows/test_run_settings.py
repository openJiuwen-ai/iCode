# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow runs apply their own project policy without changing Chat settings."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation import platform
from chrys.foundation.config.context import EvalContext
from chrys.foundation.config.process_env import freeze_process_env
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import load_settings
from chrys.foundation.config.spec import Source
from chrys.orchestration.workflows.session import WorkflowSessionOwner
from chrys.service.workflows.outcomes import RunOutcome
from tests.orchestration.workflows._hosting import confirm, make_host, make_project, run, write_workflow
from tests.support.workflow_workers import python_workflow


@pytest.mark.parametrize("has_chat_project", [False, True])
@pytest.mark.parametrize("retry_pin", [None, 1])
async def test_restored_run_loads_its_project_and_preserves_explicit_choices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_chat_project: bool, retry_pin: int | None
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.yaml").write_text("project:\n  config_enabled: true\n", encoding="utf-8")
    fake = replace(platform.get_platform(), config_dir=config)
    monkeypatch.setattr(platform, "get_platform", lambda: fake)
    project = make_project(tmp_path)
    (project / ".chrys" / "settings.yaml").write_text("llm:\n  retry:\n    max_transient: 0\n", encoding="utf-8")
    write_workflow(project, "review", python_workflow("def check(text):\n    return text\n", "check"))
    original = make_host(tmp_path, project=project)
    try:
        await confirm(original, "review")
        await run(original, "review")
        identity = original.workflow_session_id
    finally:
        await original.shutdown()

    chat_project = tmp_path / "chat" if has_chat_project else None
    if chat_project is not None:
        (chat_project / ".chrys").mkdir(parents=True)
        (chat_project / ".chrys" / "settings.yaml").write_text(
            "llm:\n  retry:\n    max_transient: 2\n", encoding="utf-8"
        )
    freeze_process_env()
    loaded = load_settings(
        project_root=chat_project, eval_context=EvalContext(frontend_default_max_transient_retries=15)
    ).overlay(Source.SESSION, model_profile="mock-profile", ask_user_timeout_seconds=77)
    if retry_pin is not None:
        loaded = loaded.overlay(Source.SESSION, max_transient_retries=retry_pin)
    restored = make_host(tmp_path, project=chat_project, loaded_settings=loaded)
    restored.engine.settings_handle.override(ask_user_timeout_seconds=99, model_profile_override="chat-only")
    chat_settings = restored.engine.loaded_settings
    observed: list[Settings] = []
    prepare = WorkflowSessionOwner.prepare

    async def capture(self, **kwargs):
        observed.append(kwargs["settings"])
        return await prepare(self, **kwargs)

    monkeypatch.setattr(WorkflowSessionOwner, "prepare", create_autospec(prepare, side_effect=capture))
    try:
        await restored.load_workflow_session(identity)
        result, _ = await run(restored, "review")
        assert result.outcome is RunOutcome.COMPLETED
        assert len(observed) == 1
        settings = observed[0]
        assert settings.max_transient_retries == (retry_pin if retry_pin is not None else 0)
        assert settings.frontend_default_max_transient_retries == 15
        assert settings.model_profile == "mock-profile"
        assert settings.ask_user_timeout_seconds == 77
        assert settings.model_profile_override == ""
        assert restored.engine.loaded_settings is chat_settings
        assert restored.engine.settings.max_transient_retries == (
            retry_pin if retry_pin is not None else (2 if has_chat_project else None)
        )
    finally:
        await restored.shutdown()
