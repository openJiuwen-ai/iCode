# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent resources, installed manifests, and completed build candidates."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import TYPE_CHECKING

from chrys.foundation.events.types import AgentRuntimeDetails, RuntimeSkillDetails

if TYPE_CHECKING:
    from chrys.foundation.config.settings_store import PreparedSettings
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.kernel import Agent, LoopRecorder
    from chrys.orchestration.engine.build.builder import AgentBuildResult
    from chrys.orchestration.engine.build.construction import StagedBuild
    from chrys.orchestration.engine.run.bindings import TurnBindings
    from chrys.orchestration.invoker.resources import Conversation, PreparedAgent
    from chrys.orchestration.sub_agents.tools import SubAgentTools
    from chrys.service.agent_middleware.injection import ConsumedInjection, InjectionMiddleware
    from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware
    from chrys.service.approval.judge import ApprovalJudge
    from chrys.service.context.compaction import UnifiedContextStrategy
    from chrys.service.context.compaction.last_words_state import LastWordsState
    from chrys.service.mcp.adapter import MCPAdapter
    from chrys.service.mutations.coordination import MutationCoordinator
    from chrys.service.mutations.tracker import MutationTracker
    from chrys.service.mutations.workspace_changes import WorkspaceRetarget
    from chrys.service.profiles.models.schema import ModelProfile
    from chrys.service.skills.provider import ChrysSkillsProvider
    from chrys.service.todos.tracker import TodoTracker


@dataclass(frozen=True, slots=True, kw_only=True)
class LoadedAgent:
    """Resources belonging to one installed build."""

    prepared: PreparedAgent
    conversation: Conversation
    agent: Agent
    bindings: TurnBindings
    runtime: SessionEnvironment
    injection: InjectionMiddleware
    consumed_injections: list[ConsumedInjection]
    intermediate_texts: dict[int, str]
    loop_recorder: LoopRecorder
    reminder_middleware: SystemReminderMiddleware
    last_words: LastWordsState
    approval_judge: ApprovalJudge
    sub_agent_tools: SubAgentTools | None
    skills_provider: ChrysSkillsProvider | None
    mcp_adapter: MCPAdapter | None

    async def aclose(self) -> None:
        """Close the owner of all resources in this build."""
        await self.prepared.aclose()


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentManifest:
    """Configuration of the last installed build, retained after close."""

    tool_names: tuple[str, ...]
    tool_kinds: Mapping[str, str]
    skill_names: tuple[str, ...]
    memory_files: tuple[str, ...]
    instructions: str = ""
    """The system prompt the installed build hands the model, kept so hooks can be shown it."""
    profile_instructions: str = ""
    """The agent profile's own instructions, without memory or provider contributions."""
    agent_profile_fingerprint: str
    model_profile_fingerprint: str
    runtime_details: AgentRuntimeDetails
    active_profile: ModelProfile | None

    @classmethod
    def empty(cls) -> AgentManifest:
        """Create a distinct empty manifest."""
        return cls(
            tool_names=(),
            tool_kinds=MappingProxyType({}),
            skill_names=(),
            memory_files=(),
            agent_profile_fingerprint="",
            model_profile_fingerprint="",
            runtime_details=AgentRuntimeDetails(),
            active_profile=None,
        )

    @classmethod
    def from_build(cls, result: AgentBuildResult) -> AgentManifest:
        """Capture the configuration supplied by the completed builder."""
        return cls(
            tool_names=tuple(result.tool_names),
            tool_kinds=MappingProxyType(dict(result.tool_kinds)),
            skill_names=tuple(result.skill_names),
            memory_files=tuple(result.memory_files),
            instructions=result.instructions,
            profile_instructions=result.profile_instructions,
            agent_profile_fingerprint=result.agent_profile_fingerprint,
            model_profile_fingerprint=result.model_profile_fingerprint,
            runtime_details=copy.deepcopy(result.runtime_details),
            active_profile=copy.deepcopy(result.active_profile),
        )

    def with_skill_refresh(
        self,
        *,
        skill_names: Sequence[str],
        skill_sources: Mapping[str, Sequence[str]],
        skill_details: Sequence[RuntimeSkillDetails],
    ) -> AgentManifest:
        """Return a new manifest without changing the previous runtime snapshot."""
        details = replace(
            self.runtime_details,
            skill_sources={key: list(value) for key, value in skill_sources.items()},
            skill_details=list(skill_details),
        )
        return replace(self, skill_names=tuple(skill_names), runtime_details=details)


@dataclass(frozen=True, kw_only=True)
class CompletedBuild:
    """A candidate and all build-owned values to install together."""

    staged: StagedBuild
    settings: PreparedSettings
    workspace_retarget: WorkspaceRetarget
    loaded: LoadedAgent
    manifest: AgentManifest
    compaction_strategy: UnifiedContextStrategy | None
    mutation_tracker: MutationTracker | None
    todo_tracker: TodoTracker | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReplacedBuild:
    """The resources displaced by a successful installation."""

    loaded: LoadedAgent | None
    coordinator: MutationCoordinator | None
