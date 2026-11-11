# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent, model and runtime stand-ins shared by the headless session-host test modules."""

from __future__ import annotations

from typing import Annotated, Any

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.config.settings import Settings
from chrys.kernel import FunctionTool
from chrys.service.llm.mock import MockChatClient
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.support.tool_packs import install_tools


def _echo(message: Annotated[str, "Message to echo"]) -> str:
    return f"echo: {message}"


def _make_tool() -> FunctionTool:
    return FunctionTool(func=_echo, name="guarded_echo", description="Echo with approval")


def _make_profile(*, approval_default: str = "require") -> AgentProfile:
    return AgentProfile(
        name="Headless",
        instructions="You are a test assistant. Use the tools provided.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default=approval_default, overrides={}),
        compaction=CompactionConfig(enabled=False),
    )


def _make_agent_registry(profile: AgentProfile) -> AgentProfileRegistry:
    registry = AgentProfileRegistry()
    registry.register(profile)
    return registry


def _make_agent_registry_many(*profiles: AgentProfile) -> AgentProfileRegistry:
    registry = AgentProfileRegistry()
    for profile in profiles:
        registry.register(profile)
    return registry


def _make_settings_and_models() -> tuple[Settings, ModelProfileRegistry]:
    registry = ModelProfileRegistry()
    registry.register(
        ModelProfile(
            id="mock-profile",
            name="mock",
            provider="mock",
            model_id="mock",
            max_context_tokens=100_000,
            stream=False,
        )
    )
    return Settings(model_profile="mock-profile"), registry


def _patch_runtime(monkeypatch: pytest.MonkeyPatch, clients: list[MockChatClient], tools: list[FunctionTool]) -> None:
    async def _patched_create_client(_settings: Any = None, **kwargs: Any) -> MockChatClient:
        client = clients.pop(0)
        client._on_intermediate_text = kwargs.get("on_intermediate_text")
        return client

    monkeypatch.setattr(builder_module, "create_client", _patched_create_client)
    install_tools(monkeypatch, tools)
