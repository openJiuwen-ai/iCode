# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host-level scaffolding for workflow run tests: a project holding workflow files, a headless host on mock clients."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
import chrys.orchestration.workflows.agent_node_build as agent_node_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.events.types import Event, WorkflowRunAccepted
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform.files import atomic_write_owner_only_bytes
from chrys.orchestration.session_host import ChrysSessionHost
from chrys.orchestration.workflows.preview import WorkflowPreview
from chrys.orchestration.workflows.runner import WorkflowRunResult
from chrys.service.approval.policy import ApprovalMode
from chrys.service.llm.mock import MockChatClient
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile, ApprovalConfig, CompactionConfig, ToolsConfig
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workflows.discovery import project_workflows_dir
from tests.support.tool_packs import install_tools

PROFILE = "Headless"
"""The session's own profile; agent nodes in the tests bind to it too."""


def hold_workflow_deadline(monkeypatch: pytest.MonkeyPatch, timeout: float) -> asyncio.Event:
    """Replace only the requested deadline's sleep with a barrier, leaving all other waits real."""
    expire = asyncio.Event()
    real_sleep = asyncio.sleep

    async def sleep(delay: float, result: Any = None) -> Any:
        if delay == timeout:
            await expire.wait()
            return result
        return await real_sleep(delay, result)

    monkeypatch.setattr(asyncio, "sleep", create_autospec(real_sleep, side_effect=sleep))
    return expire


def patch_runtime(
    monkeypatch: pytest.MonkeyPatch, clients: list[MockChatClient], *, builtin_tools: bool = False
) -> None:
    """Hand out *clients* in order: the session's build takes the first, every agent node activation one more.

    Agents get no builtin tools unless *builtin_tools* asks for the real registry.
    """

    main_client = clients.pop(0)

    def _configure(client: MockChatClient, kwargs: dict) -> MockChatClient:
        client._on_intermediate_text = kwargs.get("on_intermediate_text")
        return client

    async def _main_client(*_args: Any, **kwargs: Any) -> MockChatClient:
        return _configure(main_client, kwargs)

    async def _create_client(*_args: Any, **kwargs: Any) -> MockChatClient:
        return _configure(clients.pop(0), kwargs)

    monkeypatch.setattr(builder_module, "create_client", _main_client)
    monkeypatch.setattr(agent_node_module, "create_client", _create_client)
    if not builtin_tools:
        install_tools(monkeypatch)


def make_profile(name: str = PROFILE, *, builtins: Sequence[str] = ()) -> AgentProfile:
    return AgentProfile(
        name=name,
        instructions="You are a test assistant.",
        tools=ToolsConfig(builtins=list(builtins)),
        approval=ApprovalConfig(default="require", overrides={}),
        compaction=CompactionConfig(enabled=False),
    )


def make_host(
    tmp_path: Path,
    *,
    project: Path | None,
    profiles: Sequence[AgentProfile] = (),
    allow_user_interaction: bool = False,
    session_id: str | None = None,
    profile_name: str = PROFILE,
    stream: bool = False,
    chat_options: str = "",
    workspace: Workspace | None = None,
    loaded_settings: LoadedSettings | None = None,
    settings: Settings | None = None,
    approval_mode: ApprovalMode | None = ApprovalMode.BYPASS,
    surface: SessionSurface | None = None,
    models: Sequence[ModelProfile] = (),
) -> ChrysSessionHost:
    model_registry = ModelProfileRegistry()
    model_registry.register(
        ModelProfile(
            id="mock-profile", name="mock", provider="mock", model_id="mock", stream=stream, chat_options=chat_options
        )
    )
    for model in models:
        model_registry.register(model)
    agents = AgentProfileRegistry()
    for profile in profiles or (make_profile(),):
        agents.register(profile)
    return ChrysSessionHost(
        profile_name=profile_name,
        settings=None if loaded_settings is not None else settings or Settings(model_profile="mock-profile"),
        loaded_settings=loaded_settings,
        approval_mode=approval_mode,
        agent_registry=agents,
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
        cwd=str(project) if project is not None else None,
        workspace=workspace,
        allow_user_interaction=allow_user_interaction,
        session_id=session_id,
        surface=surface,
    )


def make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project_workflows_dir(project).mkdir(parents=True)
    return project


def write_workflow(project: Path, workflow_id: str, source: bytes) -> Path:
    """Write (or rewrite) a project workflow file as its author would; see tests/support/secure_files.py."""
    path = project_workflows_dir(project) / f"{workflow_id}.py"
    atomic_write_owner_only_bytes(path, source)
    return path


def write_workflow_package(
    project: Path, workflow_id: str, source: bytes, files: Mapping[str, bytes] | None = None
) -> Path:
    """Write (or rewrite) a project workflow folder ``<id>/<id>.py`` plus *files* (relative paths); returns the entry."""
    folder = project_workflows_dir(project) / workflow_id
    for relative, content in (files or {}).items():
        member = folder / relative
        member.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_owner_only_bytes(member, content)
    folder.mkdir(exist_ok=True)
    entry = folder / f"{workflow_id}.py"
    atomic_write_owner_only_bytes(entry, source)
    return entry


async def confirm(host: ChrysSessionHost, workflow_id: str) -> WorkflowPreview:
    """What the CLI does when the user says yes: preview the file and record the confirmation."""
    preview = await host.preview_workflow(host.workflow_target(workflow_id), trust=True)
    host.confirm_workflow(preview)
    return preview.preview


async def run(host: ChrysSessionHost, workflow_id: str, **kwargs: Any) -> tuple[WorkflowRunResult, list[Event]]:
    """Run to the terminal and return the full result with every streamed event."""
    events: list[Event] = []
    run_id = ""
    target = host.workflow_target(workflow_id, new_session=kwargs.pop("new_session", False))
    async for event in host.iter_workflow_events(target, **kwargs):
        events.append(event)
        if isinstance(event, WorkflowRunAccepted):
            run_id = event.run_id
    result = host.engine.workflows.result(run_id)
    assert result is not None
    return result, events


def of_type[E: Event](events: Sequence[Event], event_type: type[E]) -> list[E]:
    return [event for event in events if isinstance(event, event_type)]
