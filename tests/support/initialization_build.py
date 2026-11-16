# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real agent builders with deterministic model clients and synthetic dependency roots."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, create_autospec

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.orchestration.engine.build import builder
from chrys.orchestration.sub_agents import tools as children
from chrys.service.agent_middleware import ResponseBatchCounter
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.approval.policy import ApprovalMode
from chrys.service.llm.mock import MockChatClient
from chrys.service.mcp.cache import MCPConnectionCache
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    CompactionConfig,
    MCPServerConfig,
    SkillsConfig,
    SubAgentRef,
    SubAgentsConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.skills import adapter as skills_adapter


def _client(
    profile: ModelProfile,
    on_intermediate_text: Callable[[str], None] | None = None,
    session_id: str | None = None,
    parent_session_id: str | None = None,
    use_route_session_context: bool = False,
    session_dir: Path | None = None,
    tool_result_ceiling_tokens: int | None = None,
) -> MockChatClient:
    return MockChatClient()


class InitializationBuild:
    """Keep production dependency construction; replace only external model access and skill roots."""

    def __init__(self, root: Path, child_count: int, configs: list[MCPServerConfig]) -> None:
        self.root = root
        self.registry = AgentProfileRegistry()
        self.models = ModelProfileRegistry()
        self.models.register(ModelProfile(id="mock-model", name="mock", provider="mock", model_id="mock"))
        self.profile = self._profile("Parent", configs)
        self.profile.sub_agents = SubAgentsConfig(
            agents=[SubAgentRef(profile=f"Child{i}", tool_name=f"Child{i}") for i in range(child_count)]
        )
        self.registry.register(self.profile)
        for i in range(child_count):
            self.registry.register(self._profile(f"Child{i}", configs))
        self.bus = EventBus()

    def _profile(self, name: str, configs: list[MCPServerConfig]) -> AgentProfile:
        return AgentProfile(
            name=name,
            instructions="Synthetic initialization fixture.",
            tools=ToolsConfig(builtins=[], mcp=list(configs)),
            skills=SkillsConfig(
                paths=[str(self.root / "skills")], auto_load_user_agents_skills=False, auto_load_cwd_agents_skills=False
            ),
            compaction=CompactionConfig(enabled=False),
        )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for module in (builder, children):
            monkeypatch.setattr(module, "create_client", create_autospec(module.create_client, side_effect=_client))
        monkeypatch.setattr(
            skills_adapter,
            "_collect_skill_paths",
            create_autospec(skills_adapter._collect_skill_paths, return_value=[str(self.root / "skills")]),
        )

    async def build(self, cache: MCPConnectionCache) -> builder.AgentBuildResult:
        return await builder.build_agent(
            profile=self.profile,
            settings=Settings(model_profile="mock-model"),
            workspace=None,
            session_id="initialization-probe",
            bus=self.bus,
            agent_registry=self.registry,
            injection=InjectionMiddleware(),
            batch_counter=ResponseBatchCounter(),
            on_intermediate=MagicMock(),
            on_usage=MagicMock(),
            model_registry=self.models,
            approval_mode=ApprovalMode.BYPASS,
            session_dir=self.root / "session",
            mcp_cache=cache,
        )


def create_skills(root: Path, count: int) -> None:
    for i in range(count):
        skill = root / "skills" / f"skill-{i}"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            f"---\nname: skill-{i}\ndescription: Synthetic fixture\n---\n" + "content " * 128, encoding="utf-8"
        )
        for name in ("one.md", "two.md", "run.py"):
            (skill / name).write_text("# Synthetic file, never executed.\n", encoding="utf-8")
