# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow agent nodes honor the bound profile's web tool configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import chrys.orchestration.workflows.agent_node_build as agent_node_build
import chrys.service.tools.builtins.web.build as web_build
from chrys.foundation.events.types import Warning
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.tools.builtins.web.build import WebTools
from chrys.service.tools.builtins.web.config import WebFetchConfigPatch, WebSearchConfigPatch
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


def _workflow(profile: str) -> bytes:
    return (
        "from chrys.workflows import WorkflowBuilder\nwf = WorkflowBuilder('agent')\n"
        f"node = wf.agent('node', profile={profile!r})\nwf.start(node)\nwf.output(node)\nworkflow = wf.build()\n"
    ).encode()


async def test_workflow_agent_node_passes_profile_web_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The node build must forward the profile web patches instead of global defaults."""
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[MockResponse(text="done")])])
    profile = make_profile(name="WebOff")
    profile.tools.web_search = WebSearchConfigPatch(mode="off")
    profile.tools.web_fetch = WebFetchConfigPatch(mode="off")
    calls: list[tuple[Any, ...]] = []
    original = web_build.assemble_web_tools

    def capture(categories: list[str], settings: Any, search: Any, fetch: Any, **kwargs: Any) -> WebTools:
        calls.append((search, fetch))
        return original(categories, settings, search, fetch, **kwargs)

    monkeypatch.setattr(web_build, "assemble_web_tools", capture)
    project = make_project(tmp_path)
    write_workflow(project, "web", _workflow("WebOff"))
    host = make_host(tmp_path, project=project, profiles=[make_profile(), profile])
    try:
        await confirm(host, "web")
        result, _events = await run(host, "web")
        assert result.outcome.value == "completed"
    finally:
        await host.shutdown()

    node_calls = [call for call in calls if call[0] is profile.tools.web_search]
    assert len(node_calls) == 1, calls
    assert node_calls[0][1] is profile.tools.web_fetch


async def test_workflow_agent_node_warns_when_hosted_web_suppresses_local_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[MockResponse(text="done")])])
    monkeypatch.setattr(agent_node_build, "effective_chat_options", lambda _model: {"tools": [{"type": "web_search"}]})
    profile = make_profile(name="HostedWeb", builtins=("web_search",))
    project = make_project(tmp_path)
    write_workflow(project, "hosted-web", _workflow(profile.name))
    host = make_host(tmp_path, project=project, profiles=[make_profile(), profile])
    try:
        await confirm(host, "hosted-web")
        result, events = await run(host, "hosted-web")
        assert result.outcome.value == "completed"
    finally:
        await host.shutdown()

    warnings = [event for event in of_type(events, Warning) if event.code == "hosted_web_tools_preferred"]
    assert len(warnings) == 1
    assert warnings[0].session_id
    assert "model profile mock-profile" in warnings[0].message


async def test_workflow_agent_node_rejects_duplicate_hosted_web_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[MockResponse(text="unused")])])
    monkeypatch.setattr(
        agent_node_build,
        "effective_chat_options",
        lambda _model: {"tools": [{"type": "web_search"}, {"type": "web_search_preview"}]},
    )
    profile = make_profile(name="DuplicateHostedWeb")
    project = make_project(tmp_path)
    write_workflow(project, "duplicate-hosted-web", _workflow(profile.name))
    host = make_host(tmp_path, project=project, profiles=[make_profile(), profile])
    try:
        await confirm(host, "duplicate-hosted-web")
        result, _events = await run(host, "duplicate-hosted-web")
    finally:
        await host.shutdown()

    assert result.outcome.value == "node_failed"
    assert "Provider-hosted web tool name 'web_search' is declared more than once" in result.error
