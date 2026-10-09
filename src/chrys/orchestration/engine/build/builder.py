# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentBuilder — constructs Agent + TurnBindings from an AgentProfile.

Extracts the infrastructure-wiring logic from ``AgentEngine._build_agent``
into a reusable function.  The engine provides callbacks (for intermediate
text, usage, injection) that close over engine state; this module handles
everything else: compaction strategy, context management, tool loading,
sub-agent registration, skills, middleware chain, and Agent/TurnBindings creation.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.types import (
    AGENT_LOAD_PHASE_AGENT,
    AGENT_LOAD_PHASE_MCP,
    AGENT_LOAD_PHASE_MODEL,
    AGENT_LOAD_PHASE_RUNTIME,
    AGENT_LOAD_PHASE_SKILLS,
    AGENT_LOAD_PHASE_SUB_AGENTS,
    AGENT_LOAD_PHASE_TOOLS,
    AGENT_LOAD_STATUS_DONE,
    AGENT_LOAD_STATUS_FAILED,
    AGENT_LOAD_STATUS_RUNNING,
    AGENT_LOAD_STATUS_SKIPPED,
    AgentRuntimeDetails,
    CompactionFinished,
    CompactionStarted,
    InvocationContextPressure,
    InvocationMessage,
    InvocationRetryAttempt,
    RuntimeHookDetails,
    RuntimeHookSourceDetails,
    RuntimeModelDetails,
    Warning,
)
from chrys.foundation.i18n import DisplayBlock, msg
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.retry import RetryAttemptInfo
from chrys.foundation.tool_kinds import KIND_ASK_USER, KIND_FILESYSTEM_READ, KIND_SHELL, get_tool_kind
from chrys.kernel import Agent, LoopRecorder
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.invoker.origin import InvocationPublishers
from chrys.orchestration.invoker.resources import Conversation, PreparedAgent, rollback_resources
from chrys.orchestration.invoker.runtime import (
    ApprovalInputs,
    AskUserInputs,
    ContextInputs,
    LastWordsInputs,
    MainRecipe,
    ReminderInputs,
    SharedRuntime,
    create_approval,
    create_ask_user,
    create_context,
    create_runtime,
    create_validation,
)
from chrys.orchestration.sub_agents.registration import (
    acp_sub_agent_depth,
    sub_agent_registration_state,
    sub_agent_skip_reason,
)
from chrys.service.agent_middleware import (
    IntermediateTextBuffer,
    TodoMiddleware,
)
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.agent_middleware.system_reminder import DropRoundBreakerState, SystemReminderMiddleware
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.context.compaction.spill import COMPACTIONS_DIR_NAME
from chrys.service.context.memory_loader import load_memory_content, memory_truncated_warning
from chrys.service.llm.clients import create_client, effective_model_base_url
from chrys.service.llm.route_sessions import derive_llm_route_session_id
from chrys.service.profiles.models.options import (
    effective_chat_options,
    protected_chat_option_keys_warning_structured,
    uses_responses_compact_continuation,
)
from chrys.service.profiles.models.resolver import resolve_selection_for_agent
from chrys.service.profiles.models.schema import API_STYLE_CHAT_COMPLETIONS, API_STYLE_RESPONSES
from chrys.service.session.persistence import agent_profile_context_fingerprint, model_profile_context_fingerprint
from chrys.service.state.serializers import serialized_message_payload
from chrys.service.todos.reminder import format_todo_reminder
from chrys.service.tools.names import chrys_reserved_tool_names

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.models.workspace import Workspace
    from chrys.orchestration.sub_agents.tools import SubAgentTools
    from chrys.service.approval.judge import ApprovalJudge
    from chrys.service.approval.turn_context import TurnContextHolder
    from chrys.service.context.compaction import CompactionInfo, CompressInfo, PreCompactInfo
    from chrys.service.context.compaction.last_words import CompactionStatus
    from chrys.service.context.compaction.spill import SpillQuota
    from chrys.service.hooks.manager import HookManager
    from chrys.service.hooks.schema import HooksFile
    from chrys.service.mcp.adapter import MCPAdapter
    from chrys.service.mcp.cache import MCPConnectionCache
    from chrys.service.mutations.coordination import MutationCoordinator
    from chrys.service.mutations.tracker import MutationTracker
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.schema import ModelProfile
    from chrys.service.skills.provider import ChrysSkillsProvider
    from chrys.service.todos.tracker import TodoTracker

logger = logging.getLogger(__name__)

_RETRY_LAST_WORDS_COMPACTION = msg(
    "retry.last_words_compaction",
    fallback="LAST_WORDS compaction: {reason}",
    multiline=True,
)
_RETRY_INVALID_RESPONSE = msg(
    "retry.invalid_response",
    fallback="Invalid response: {reason}",
    multiline=True,
)
_BUILDER_PROTECTED_CHAT_OPTIONS_STRIPPED = msg(
    "builder.protected_chat_options_stripped",
    fallback=(
        "Protected chat option key(s) were stripped: {keys}. "
        "These keys can no longer be configured in profile YAML because they bypass context admission. "
        "Reusable prompts and profile-pinned continuation IDs are unavailable there; store: true only enables "
        "automatically managed Responses continuation."
    ),
)


def _render_todo_reminder(tracker: TodoTracker | None) -> str | None:
    """Todo reminder provider body — reads the sync lock-free tracker snapshot."""
    if tracker is None:
        return None
    return format_todo_reminder(tracker.snapshot())


def _runtime_api_style(provider: str, api_style: str) -> str:
    """Return the API style users should see for the active model transport."""
    if provider in ("openai", "deepseek-openai"):
        return api_style
    if provider == "glm-openai":
        return API_STYLE_CHAT_COMPLETIONS
    return ""


def _runtime_hook_source_details(
    scope: Literal["project", "global"],
    file: HooksFile,
) -> RuntimeHookSourceDetails:
    """Build the non-sensitive runtime view for one hook config source."""
    return RuntimeHookSourceDetails(
        scope=scope,
        source_path=surrogate_safe_text(file.source),
        hooks=[
            RuntimeHookDetails(
                id=hook.id,
                event=str(hook.event),
                execution_mode=hook.execution.mode,
                enabled=hook.enabled,
                description=hook.description,
            )
            for hook in file.hooks
        ],
    )


def _runtime_hook_sources(hook_manager: HookManager | None) -> list[RuntimeHookSourceDetails]:
    """Snapshot the live project/global hook manager without executable configuration."""
    if hook_manager is None:
        return []
    sources: list[RuntimeHookSourceDetails] = []
    project = hook_manager.file.project
    if project is not None:
        sources.append(_runtime_hook_source_details("project", project))
    global_ = hook_manager.file.global_
    if global_ is not None:
        sources.append(_runtime_hook_source_details("global", global_))
    return sources


class LoadProgressCallback(Protocol):
    """Typed callback used by the engine to surface build progress."""

    def __call__(
        self,
        *,
        phase: str,
        message: str,
        server_name: str = "",
        current: int = 0,
        total: int = 0,
        failed: int = 0,
        status: str = "",
        subject: str = "",
        detail: str = "",
    ) -> Awaitable[None]: ...


@dataclass
class AgentBuildResult:
    """Everything produced by ``build_agent``."""

    prepared: PreparedAgent
    conversation: Conversation
    agent: Agent
    bindings: TurnBindings
    runtime: SessionEnvironment
    loop_recorder: LoopRecorder
    reminder_middleware: SystemReminderMiddleware
    sub_agent_tools: SubAgentTools | None
    mcp_adapter: MCPAdapter | None
    skills_provider: ChrysSkillsProvider | None
    tool_names: list[str]
    tool_kinds: dict[str, str]
    skill_names: list[str]
    memory_files: list[str]
    agent_profile_fingerprint: str
    model_profile_fingerprint: str
    runtime_details: AgentRuntimeDetails
    compaction_strategy: Any
    active_profile: ModelProfile


async def build_agent(
    # -- required -----------------------------------------------------
    profile: AgentProfile,
    settings: Settings,
    workspace: Workspace | None,
    session_id: str | None,
    bus: EventBus,
    agent_registry: AgentProfileRegistry | None,
    injection: InjectionMiddleware,
    intermediate_buffer: IntermediateTextBuffer,
    on_intermediate_async: Callable[[str], Awaitable[None]],
    on_intermediate_sync: Callable[[str], None],
    on_usage: Callable[..., None],
    *,
    # -- optional -----------------------------------------------------
    model_registry: ModelProfileRegistry | None = None,
    on_sub_agent_usage: Callable[..., None] | None = None,
    on_side_call_usage: Callable[[Mapping[str, Any]], None] | None = None,
    commit_intermediate_text: Callable[[str, int], None] | None = None,
    drain_parent_usage_publishes: Callable[[], Awaitable[None]] | None = None,
    on_compaction: Callable[[CompactionInfo], Awaitable[None]] | None = None,
    on_pre_compact: Callable[[PreCompactInfo], Awaitable[None]] | None = None,
    on_compress: Callable[[CompressInfo], Awaitable[None]] | None = None,
    on_load_progress: LoadProgressCallback | None = None,
    mutation_tracker: MutationTracker | None = None,
    mutation_coordinator: MutationCoordinator | None = None,
    todo_tracker: TodoTracker | None = None,
    file_change_provider: Callable[[], str | None] | None = None,
    approval_mode: ApprovalMode = ApprovalMode.MANUAL,
    approval_judge: ApprovalJudge | None = None,
    session_dir: Path | None = None,
    mcp_cache: MCPConnectionCache | None = None,
    hook_manager: HookManager | None = None,
    spill_quota: SpillQuota | None = None,
    persist_recovery_now: Callable[[], Awaitable[bool]] | None = None,
    turn_context: TurnContextHolder | None = None,
    allow_user_interaction: bool = True,
) -> AgentBuildResult:
    """Build an ``Agent``, ``TurnBindings``, and all supporting infrastructure.

    Parameters
    ----------
    profile:
        The agent profile to build from.  Explicit main-agent model overrides
        in ``settings`` win first; otherwise the profile's ``model.profile_id``
        (if set and registered) overrides the session-active profile.
    settings:
        Application settings — provides the session-active model profile
        id via ``settings.model_profile``.
    model_registry:
        Registry used to resolve the main agent's ``profile_id``
        override and per-sub-agent overrides.  ``None`` → everything
        falls back to the hardcoded default profile.
    workspace:
        Current workspace (used for ``SessionEnvironment``).
    session_id:
        Current session ID (passed to ``TurnBindings``).
    bus:
        Event bus (passed to ``TurnBindings``).
    agent_registry:
        Profile registry (used for sub-agent resolution).
    injection:
        Pre-configured ``InjectionMiddleware`` (engine owns the callbacks).
    intermediate_buffer:
        Pre-configured ``IntermediateTextBuffer``.
    on_intermediate_async:
        Async callback for intermediate text (non-streaming path).
    on_intermediate_sync:
        Sync callback for intermediate text (streaming path).
    commit_intermediate_text:
        Accepted-only persistence callback for a pre-reserved provisional batch.
    on_usage:
        Callback for context usage updates.
    on_side_call_usage:
        Callback receiving raw provider ``usage_details`` from Phase-4
        LAST_WORDS side calls (main agent and sub-agents) so their spend
        joins the session totals.
    on_compaction:
        Callback for intra-turn tool compaction events.

    Returns
    -------
    AgentBuildResult
        All constructed components needed by the engine.
    """

    prepared = PreparedAgent()

    async def _acquire(conversation: Conversation) -> AgentBuildResult:
        nonlocal turn_context

        async def _progress(
            phase: str,
            message: str,
            *,
            server_name: str = "",
            current: int = 0,
            total: int = 0,
            failed: int = 0,
            status: str = "",
            subject: str = "",
            detail: str = "",
        ) -> None:
            if on_load_progress is not None:
                await on_load_progress(
                    phase=phase,
                    message=message,
                    server_name=server_name,
                    current=current,
                    total=total,
                    failed=failed,
                    status=status,
                    subject=subject,
                    detail=detail,
                )

        if profile.acp is not None:
            raise ValueError(f"ACP profile {profile.name!r} is sub-agent-only and cannot be launched as the main agent")

        transient_retries = settings.effective_max_transient_retries()

        if turn_context is None:
            from chrys.service.approval.turn_context import TurnContextHolder

            turn_context = TurnContextHolder()

        await _progress(AGENT_LOAD_PHASE_MODEL, "Resolving model profile", status=AGENT_LOAD_STATUS_RUNNING)

        # Resolve the main agent's ModelProfile: profile override → active.
        # Missing ids fall back silently to active (no throw).
        model_selection = resolve_selection_for_agent(model_registry, settings, profile)
        active_profile = model_selection.profile

        # Capture the session environment for this agent build.
        await _progress(AGENT_LOAD_PHASE_RUNTIME, "Capturing workspace context", status=AGENT_LOAD_STATUS_RUNNING)
        runtime = SessionEnvironment.capture(workspace=workspace)
        serialize_implicit_windows = not settings.parallel_implicit_tools

        # Auto-loaded memory (files + folders configured on the profile).  Read
        # once per build; subsequent edits to source files are picked up by the
        # next build (e.g. profile-save soft-restart).  Sub-agents skip this by
        # not passing ``memory_text`` on their own ContextManager.
        memory_content = load_memory_content(
            profile.memory,
            workspace_cwd=workspace.primary_cwd if workspace else None,
        )
        acp_depth = acp_sub_agent_depth()
        effective_sub_agents: list[dict[str, Any]] = []
        for ref in profile.sub_agents.agents:
            resolved = agent_registry.get(ref.profile) if agent_registry is not None else None
            effective_sub_agents.append(
                {
                    "reference": asdict(ref),
                    "resolved_profile": asdict(resolved) if resolved is not None else None,
                    "state": sub_agent_registration_state(resolved, acp_depth=acp_depth),
                }
            )
        agent_profile_fingerprint = agent_profile_context_fingerprint(
            profile,
            memory_text=memory_content.text,
            effective_sub_agents=effective_sub_agents,
            allow_user_interaction=allow_user_interaction,
        )

        # Resolve session-scoped log directories from a single session_dir source.
        # If the caller didn't pass session_dir (e.g. engine with no state_store)
        # but a session_id exists, fall back to the active sessions directory — this
        # preserves on-disk logging for real runs. Tests with ``tmp_path`` stores
        # always pass session_dir, so their logs stay in the temp dir.
        effective_session_dir = session_dir
        if effective_session_dir is None and session_id:
            from chrys.foundation.config.settings import resolve_sessions_dir
            from chrys.foundation.util.session_ids import session_short_id

            effective_session_dir = resolve_sessions_dir() / session_short_id(session_id)
        approval_log_dir = effective_session_dir / "approvals" if effective_session_dir else None
        last_words_log_dir = effective_session_dir / "compactions" / "debug" if effective_session_dir else None

        # Parse chat options early — needed by context history mode, executor, and sub-agents.
        chat_options = effective_chat_options(active_profile)
        if protected_options_warning := protected_chat_option_keys_warning_structured(active_profile):
            await bus.publish(
                Warning(
                    code="protected_chat_options_stripped",
                    message=protected_options_warning.message,
                    display_message=_BUILDER_PROTECTED_CHAT_OPTIONS_STRIPPED.bind(
                        keys=", ".join(protected_options_warning.keys)
                    ),
                    session_id=session_id,
                )
            )
        model_profile_fingerprint = model_profile_context_fingerprint(active_profile, chat_options=chat_options)
        uses_responses_service_history = (
            active_profile.provider == "openai" and active_profile.api_style == API_STYLE_RESPONSES
        )
        capture_service_loop_messages = uses_responses_compact_continuation(active_profile, chat_options)
        context_recall_session_id = derive_llm_route_session_id(
            session_id,
            route_kind="context-recall",
            model_profile=active_profile,
        )
        last_words_session_id = derive_llm_route_session_id(
            session_id,
            route_kind="last-words",
            route_parts=(profile.name,),
            model_profile=active_profile,
        )

        def _publish_context_pressure(
            reason: str,
            breaker: DropRoundBreakerState,
            budget: int,
        ) -> Awaitable[None]:
            emitter = invocation_publishers.capture()
            return emitter.publish(
                InvocationContextPressure(
                    origin=emitter.origin,
                    reason=reason,
                    attempts=breaker.attempts,
                    side_call_tokens=breaker.side_call_tokens,
                    side_call_token_budget=budget,
                    session_id=session_id,
                )
            )

        # Context management (providers + middleware + compaction from profile config)
        context_inputs = ContextInputs(
            profile=active_profile,
            on_usage=on_usage,
            on_compaction=on_compaction,
            on_pre_compact=on_pre_compact,
            on_compress=on_compress,
            on_context_pressure=_publish_context_pressure,
            spill_quota=spill_quota,
            debug_log_dir=last_words_log_dir,
            compaction_config=profile.compaction,
            memory_text=memory_content.text,
            warn_threshold_pct=settings.warn_threshold_pct,
            session_id=context_recall_session_id,
            parent_session_id=session_id,
            session_dir=effective_session_dir,
            skip_local_history_for_service_context=uses_responses_service_history,
            service_context_default_options=chat_options,
        )

        ctx = create_context(context_inputs)

        # Create LLM client with intermediate text callbacks.  ``session_dir`` is
        # only used by opt-in raw HTTP logging; normal transport construction stays
        # unchanged.
        invocation_publishers = InvocationPublishers(bus)

        async def _publish_intermediate(text: str) -> None:
            emitter = invocation_publishers.capture()
            await on_intermediate_async(text)
            await emitter.publish(
                InvocationMessage(
                    origin=emitter.origin,
                    text=text,
                    is_final=False,
                    is_intermediate=True,
                    session_id=session_id,
                )
            )

        client = await create_client(
            active_profile,
            on_intermediate_text_async=_publish_intermediate,
            on_intermediate_text_sync=on_intermediate_sync,
            session_id=session_id,
            session_dir=effective_session_dir,
            tool_result_ceiling_tokens=settings.tool_result_ceiling_tokens,
            # AIxCoding telemetry: workspace cwd for llm piggyback projectName/git.
            workspace_cwd=runtime.cwd,
        )
        await prepared.own_or_release(client.aclose)

        # Build tools from profile's builtin categories
        from chrys.service.tools.builtins.web.build import assemble_web_tools
        from chrys.service.tools.registry import ToolRegistry

        await _progress(AGENT_LOAD_PHASE_TOOLS, "Loading built-in tools", status=AGENT_LOAD_STATUS_RUNNING)
        tool_registry = ToolRegistry(vision_enabled=active_profile.vision)
        builtin_categories = profile.tools.builtins or []
        web_tools = assemble_web_tools(
            builtin_categories,
            settings,
            profile.tools.web_search,
            profile.tools.web_fetch,
            chat_options=chat_options,
            agent=profile.name,
            model_profile_id=active_profile.id,
            session_id=session_id,
        )
        tool_registry.load_builtins(
            builtin_categories,
            runtime=runtime,
            settings=settings,
            shell_filter_config=profile.tools.shell_filter,
            session_id=session_id,
            session_dir=effective_session_dir,
            web=web_tools,
        )
        # Published once the web tools are decided, like the skill and memory warnings
        # further on; a later step can still fail the whole build.
        for warning in web_tools.warnings:
            await bus.publish(warning)
        from chrys.service.vision import filter_image_tools

        tools = filter_image_tools(tool_registry.get_all(), vision_enabled=active_profile.vision)
        if not allow_user_interaction:
            tools = [tool for tool in tools if get_tool_kind(tool) != KIND_ASK_USER]
        builtin_tools: dict[str, list[str]] = {}
        for category in dict.fromkeys(builtin_categories):
            names = [
                tool.name
                for tool in filter_image_tools(
                    tool_registry.get_by_category(category), vision_enabled=active_profile.vision
                )
                if allow_user_interaction or get_tool_kind(tool) != KIND_ASK_USER
            ]
            if names:
                builtin_tools[category] = names
        builtin_tool_count = len(tools)
        await _progress(
            AGENT_LOAD_PHASE_TOOLS,
            "Built-in tools loaded",
            current=builtin_tool_count,
            total=builtin_tool_count,
            status=AGENT_LOAD_STATUS_DONE,
        )

        # Collect workspace roots for file-write auto-approval (needed by both
        # the main agent's ApprovalMiddleware and each sub-agent's fresh one).
        workspace_roots: list[str] = []
        if workspace:
            workspace_roots.append(workspace.primary_cwd)
            workspace_roots.extend(wd.path for wd in workspace.working_dirs)

        # Build sub-agent tools at the composition root. The displaced owner
        # stays live until construction atomically installs this result.
        sub_agent_tools: SubAgentTools | None = None
        sub_agent_tool_names: list[str] = []
        from chrys.service.skills.adapter import create_skills_provider

        if profile.sub_agents.agents and agent_registry:
            sub_agent_total = len(profile.sub_agents.agents)
            await _progress(
                AGENT_LOAD_PHASE_SUB_AGENTS,
                "Loading sub-agent tools",
                current=0,
                total=sub_agent_total,
                status=AGENT_LOAD_STATUS_RUNNING,
            )
            from chrys.orchestration.sub_agents.tools import SubAgentTools

            sub_agent_tools = SubAgentTools(
                max_total_concurrency=profile.sub_agents.max_total_concurrency,
                event_bus=bus,
                session_id=session_id,
                session_dir=effective_session_dir,
                on_sub_agent_usage=on_sub_agent_usage,
                on_side_call_usage=on_side_call_usage,
                drain_parent_usage_publishes=drain_parent_usage_publishes,
                parent_approval=profile.approval,
                mutation_tracker=mutation_tracker,
                mutation_coordinator=mutation_coordinator,
                approval_mode=approval_mode,
                approval_judge=approval_judge,
                workspace_roots=workspace_roots,
                approval_log_dir=approval_log_dir,
                mcp_cache=mcp_cache,
                hook_manager=hook_manager,
                workspace_cwd=runtime.cwd,
                serialize_implicit_windows=serialize_implicit_windows,
                spill_quota=spill_quota,
                ask_user_timeout_seconds=settings.ask_user_timeout_seconds,
                turn_context=turn_context,
                max_transient_retries=transient_retries,
                tool_result_ceiling_tokens=settings.tool_result_ceiling_tokens,
                allow_user_interaction=allow_user_interaction,
                # A headless run shows no card to pause on, so a failed child ends with its error.
                human_failure_decisions=allow_user_interaction,
            )
            prepared.own(sub_agent_tools.cleanup)
            for index, ref in enumerate(profile.sub_agents.agents, start=1):
                await _progress(
                    AGENT_LOAD_PHASE_SUB_AGENTS,
                    f"Loading sub-agent {ref.profile}",
                    server_name=ref.profile,
                    current=index - 1,
                    total=sub_agent_total,
                    status=AGENT_LOAD_STATUS_RUNNING,
                    subject=ref.profile,
                )
                sub_profile = agent_registry.get(ref.profile)
                if sub_profile is None:
                    logger.warning("Sub-agent profile not found: %s", ref.profile)
                    await _progress(
                        AGENT_LOAD_PHASE_SUB_AGENTS,
                        f"Skipped sub-agent {ref.profile}",
                        server_name=ref.profile,
                        current=index,
                        total=sub_agent_total,
                        status=AGENT_LOAD_STATUS_SKIPPED,
                        subject=ref.profile,
                    )
                    continue
                registration_state = effective_sub_agents[index - 1]["state"]
                if registration_state in {"empty_command", "depth_limit"}:
                    reason = sub_agent_skip_reason(registration_state, acp_depth=acp_depth)
                    logger.warning("Skipping sub-agent profile %s: %s", ref.profile, reason)
                    await _progress(
                        AGENT_LOAD_PHASE_SUB_AGENTS,
                        f"Skipped sub-agent {ref.profile}: {reason}",
                        server_name=ref.profile,
                        current=index,
                        total=sub_agent_total,
                        status=AGENT_LOAD_STATUS_SKIPPED,
                        subject=ref.profile,
                        detail=reason,
                    )
                    continue
                if sub_profile.acp is not None:
                    await sub_agent_tools.register_acp(ref, sub_profile, runtime)
                else:
                    await sub_agent_tools.register(
                        ref,
                        sub_profile,
                        runtime,
                        settings=settings,
                        fallback_profile=active_profile,
                        model_registry=model_registry,
                    )
                await _progress(
                    AGENT_LOAD_PHASE_SUB_AGENTS,
                    f"Loaded sub-agent {ref.profile}",
                    server_name=ref.profile,
                    current=index,
                    total=sub_agent_total,
                    status=AGENT_LOAD_STATUS_DONE,
                    subject=ref.profile,
                )
            sub_agent_tool_instances = sub_agent_tools.get_tools()
            sub_agent_tool_names = [tool.name for tool in sub_agent_tool_instances]
            tools.extend(sub_agent_tool_instances)

        # Build MCP tools; the new Prepared owns this acquisition.
        mcp_adapter: MCPAdapter | None = None
        mcp_tools_by_server: dict[str, list[str]] = {}
        if profile.tools.mcp:
            from chrys.service.mcp.adapter import MCPAdapter

            reserved_tool_names = chrys_reserved_tool_names()
            reserved_tool_names.update(tool.name for tool in tools)
            # A provider-hosted web tool owns its name too, so a clashing MCP tool fails
            # the build with the guidance to rename or exclude it.
            reserved_tool_names.update(web_tools.hosted)
            mcp_adapter = MCPAdapter(
                bus=bus,
                session_id=session_id,
                cache=mcp_cache,
                stdio_cwd=runtime.cwd,
                session_dir=effective_session_dir,
                reserved_tool_names=reserved_tool_names,
            )
            prepared.own(mcp_adapter.disconnect_all)
            enabled_mcp = [config for config in profile.tools.mcp if config.enabled]
            if enabled_mcp:
                await _progress(
                    AGENT_LOAD_PHASE_MCP,
                    "Connecting MCP servers",
                    total=len(enabled_mcp),
                    status=AGENT_LOAD_STATUS_RUNNING,
                )

            async def _mcp_progress(config: Any, state: str, current: int, total: int, failed: int) -> None:
                action = {
                    "starting": "Connecting",
                    "connected": "Connected",
                    "failed": "Failed",
                }.get(state, "Loading")
                status = {
                    "starting": AGENT_LOAD_STATUS_RUNNING,
                    "connected": AGENT_LOAD_STATUS_DONE,
                    "failed": AGENT_LOAD_STATUS_FAILED,
                }.get(state, AGENT_LOAD_STATUS_RUNNING)
                await _progress(
                    AGENT_LOAD_PHASE_MCP,
                    f"{action} MCP server {config.name}",
                    server_name=config.name,
                    current=current,
                    total=total,
                    failed=failed,
                    status=status,
                    subject=config.name,
                )

            mcp_tools = await mcp_adapter.connect_all(profile.tools.mcp, progress=_mcp_progress)
            mcp_tools_by_server = mcp_adapter.tool_names_by_server
            tools.extend(mcp_tools)

        # Build skills provider (if configured)
        await _progress(AGENT_LOAD_PHASE_SKILLS, "Loading skills", status=AGENT_LOAD_STATUS_RUNNING)
        context_providers = []
        progressive_resume_provider = mcp_adapter.create_resume_provider() if mcp_adapter is not None else None

        skills_provider, skill_warnings = await create_skills_provider(
            profile.skills,
            runtime=runtime,
            session_dir=effective_session_dir,
        )
        if skills_provider is not None:
            context_providers.append(skills_provider)
        if progressive_resume_provider is not None:
            # Run after history and skills so restored calls can be inspected
            # and newly advertised always-load tools can detect every visible
            # name before contributing to this invocation.
            context_providers.append(progressive_resume_provider)
        for warn in skill_warnings:
            await bus.publish(Warning(code=warn.code, message=warn.message, session_id=session_id))
        skill_names = skills_provider.skill_names() if skills_provider else []
        skill_sources = skills_provider.skill_sources() if skills_provider else {}
        skill_details = skills_provider.skill_details() if skills_provider else []
        await _progress(
            AGENT_LOAD_PHASE_SKILLS,
            "Skills loaded",
            current=len(skill_names),
            total=len(skill_names),
            status=AGENT_LOAD_STATUS_DONE,
        )

        if memory_content.truncated:
            await bus.publish(memory_truncated_warning(memory_content, session_id=session_id))

        # Capture tool/skill names
        await _progress(AGENT_LOAD_PHASE_AGENT, "Finalizing agent", status=AGENT_LOAD_STATUS_RUNNING)
        tool_names = [t.name for t in tools]
        tool_kinds = {t.name: k for t in tools if (k := get_tool_kind(t))}
        context_tool_names = ctx.context_mgmt_provider.tool_names if ctx.context_mgmt_provider is not None else []
        skill_tool_names = skills_provider.tool_names if skills_provider is not None else []
        mcp_failures = {name: str(err) for name, err in (mcp_adapter.failures if mcp_adapter else {}).items()}
        runtime_details = AgentRuntimeDetails(
            model=RuntimeModelDetails(
                profile_id=active_profile.id,
                name=active_profile.name,
                provider=active_profile.provider,
                api_style=_runtime_api_style(active_profile.provider, active_profile.api_style),
                model_id=active_profile.model_id,
                max_context_tokens=active_profile.max_context_tokens,
                base_url=effective_model_base_url(active_profile),
                stream=active_profile.stream,
                vision=active_profile.vision,
                selection_source=model_selection.source,
            ),
            builtin_tools=builtin_tools,
            web_search_providers=web_tools.provider_origins(),
            sub_agent_tools=sub_agent_tool_names,
            mcp_tools=mcp_tools_by_server,
            mcp_failures=mcp_failures,
            skill_sources=skill_sources,
            skill_details=skill_details,
            hook_sources=_runtime_hook_sources(hook_manager),
            memory_sources=memory_content.loaded_files_by_source,
        )

        # Loop-message recorder for interrupt recovery — fed per run by the
        # chrys ToolLoopLayer via client_kwargs (TurnBindings._build_run_kwargs).
        loop_recorder = LoopRecorder(
            capture_service_loop_messages=capture_service_loop_messages,
            message_hasher=serialized_message_payload,
        )

        # Todo wiring gates on the profile category; the tracker's own
        # lifecycle (create/hydrate/save) is profile-independent and engine-owned.
        todo_enabled = "todo" in builtin_categories and todo_tracker is not None

        # System reminder middleware (ChatMiddleware) — enriches the user
        # message with <system-reminder> tags before each LLM call.
        # Placed last so it runs closest to the LLM.
        reminder_inputs = ReminderInputs(
            runtime=runtime,
            max_context_tokens=active_profile.max_context_tokens,
            warn_threshold_pct=settings.warn_threshold_pct,
            sub_agent_names=[t.name for t in sub_agent_tools.get_tools()] if sub_agent_tools else None,
            shell_tool_enabled=KIND_SHELL in tool_kinds.values(),
            tool_names=[*tool_names, *context_tool_names, *skill_tool_names],
            session_root=effective_session_dir,
            file_read_available=KIND_FILESYSTEM_READ in tool_kinds.values(),
            spill_quota=spill_quota,
            skill_catalog_provider=skills_provider.render_catalog_reminder if skills_provider else None,
            todo_state_provider=partial(_render_todo_reminder, todo_tracker) if todo_enabled else None,
            mcp_instructions_provider=mcp_adapter.render_instructions_reminder if mcp_adapter is not None else None,
            file_change_provider=file_change_provider,
        )

        # Wire up Phase 4 LAST_WORDS generator for the main agent.  Sub-agents
        # do the same wiring in ``SubAgentTools.register`` using their own
        # profile.compaction and their own SystemReminderMiddleware.  The
        # log dir is derived from ``session_dir`` (computed above), so tests
        # using ``tmp_path`` stores keep all log output in the temp dir.

        async def _publish_last_words_retry(info: RetryAttemptInfo) -> None:
            emitter = invocation_publishers.capture()
            await emitter.publish(
                InvocationRetryAttempt(
                    origin=emitter.origin,
                    message=f"LAST_WORDS compaction: {info.reason}",
                    attempt=info.attempt,
                    max_attempts=info.max_attempts,
                    delay_seconds=int(info.delay_seconds),
                    scope="compaction",
                    session_id=session_id,
                    display_message=_RETRY_LAST_WORDS_COMPACTION.bind(reason=DisplayBlock(info.reason)),
                    detail=info.reason,
                )
            )

        async def _publish_compaction_status(status: CompactionStatus) -> None:
            if status.stage == "started":
                await bus.publish(CompactionStarted(compaction_id=status.compaction_id, session_id=session_id))
                return
            if status.stage == "committed":
                # The main agent has a dedicated post-commit signal already
                # (``on_compaction`` → ToolCompacted); mapping this to
                # CompactionFinished would publish a bogus second finish.
                return
            await bus.publish(
                CompactionFinished(
                    compaction_id=status.compaction_id,
                    outcome=status.outcome,
                    duration_ms=status.duration_ms,
                    last_words=status.last_words,
                    format_violation=status.format_violation,
                    failure_reason=status.failure_reason,
                    session_id=session_id,
                )
            )

        last_words_inputs = LastWordsInputs(
            profile=active_profile,
            template=profile.compaction.last_words_template,
            max_output_tokens=profile.compaction.last_words_max_output_tokens,
            log_dir=last_words_log_dir,
            session_id=last_words_session_id,
            parent_session_id=session_id,
            session_dir=effective_session_dir,
            max_transient_retries=transient_retries,
            publish_retry=_publish_last_words_retry,
            publish_status=_publish_compaction_status,
            report_usage=on_side_call_usage,
        )

        # Response-validation middleware — placed innermost (closest to
        # ``_inner_get_response``) so its retries on structurally invalid
        # responses do not double-fire outer middleware (usage, system
        # reminder, loop capture).  On exhausted retries the response is
        # returned with ``function_call`` contents stripped (so the tool
        # loop exits cleanly) and structurally-empty assistant messages
        # dropped from both ``response.messages`` and ``updates`` (so the
        # streaming rebuild + persistence stay clean).  Empty-assistant
        # scrub also runs on the valid path to catch ``[empty, valid]``
        # multi-message shapes that pass validation but would otherwise
        # land a stray ``content: []`` message in ``session.json``.
        # ``publish_retry`` surfaces client-mode validation retries on the
        # bus; service-side failures raise to the executor's whole-run
        # retry, which publishes its own ``RetryAttempt``.
        async def _publish_validation_retry(info: RetryAttemptInfo) -> None:
            emitter = invocation_publishers.capture()
            await emitter.publish(
                InvocationRetryAttempt(
                    scope="wire",
                    origin=emitter.origin,
                    message=f"Invalid response: {info.reason}",
                    attempt=info.attempt,
                    max_attempts=info.max_attempts,
                    delay_seconds=int(info.delay_seconds),
                    session_id=session_id,
                    display_message=_RETRY_INVALID_RESPONSE.bind(reason=DisplayBlock(info.reason)),
                    detail=info.reason,
                )
            )

        recipe = MainRecipe(
            context=context_inputs,
            reminder=reminder_inputs,
            last_words=last_words_inputs,
            injection=InjectionMiddleware,
            validation=lambda owner: create_validation(owner, publish_retry=_publish_validation_retry),
            persist_recovery=persist_recovery_now,
        )
        web_tools.check_names(tools)

        assembled = create_runtime(
            conversation,
            SharedRuntime(
                client=client,
                name="Chrys",
                instructions=profile.instructions,
                tools=tools,
                providers=context_providers,
                vision=active_profile.vision,
            ),
            recipe,
            context=ctx,
            injection=injection,
        )
        validation_middleware = assembled.validation
        if validation_middleware is None:
            raise RuntimeError("The main agent assembly requires response validation middleware.")
        agent = assembled.agent
        reminder_middleware = assembled.reminder
        await agent.__aenter__()
        # Register agent cleanup BEFORE anything below can fail — an un-exited
        # Agent leaks its transport/session.
        await prepared.own_or_release(partial(agent.__aexit__, None, None, None))

        # Create executor
        session = agent.create_session()
        approval_policy = ApprovalPolicy(profile.approval, tools=tools)

        approval_middleware = await create_approval(
            conversation,
            ApprovalInputs(
                approval_policy=approval_policy,
                event_bus=bus,
                session_id=session_id,
                tool_kinds=tool_kinds,
                caller_name=profile.display_name or profile.name,
                workspace_roots=workspace_roots,
                approval_mode=approval_mode,
                approval_judge=approval_judge,
                approval_log_dir=approval_log_dir,
                workspace_cwd=runtime.cwd,
                dev_mode=settings.dev_mode,
                hook_manager=hook_manager,
                profile_name=profile.name,
                session_archive_read_roots=(
                    [effective_session_dir / COMPACTIONS_DIR_NAME] if effective_session_dir else None
                ),
                turn_context=turn_context,
            ),
        )
        ask_user_middleware = create_ask_user(
            conversation,
            AskUserInputs(
                event_bus=bus,
                session_id=session_id,
                caller_name=profile.display_name or profile.name,
                timeout_seconds=settings.ask_user_timeout_seconds,
            ),
        )
        extra_function_middleware: list[Any] = []
        if todo_enabled and todo_tracker is not None:
            extra_function_middleware.append(TodoMiddleware(bus, todo_tracker, session_id))
        executor = TurnBindings(
            conversation=conversation,
            agent=agent,
            session=session,
            event_bus=bus,
            approval_middleware=approval_middleware,
            ask_user_middleware=ask_user_middleware,
            injection_middleware=injection,
            loop_recorder=loop_recorder,
            session_id=session_id,
            compaction_strategy=ctx.compaction_strategy,
            stream=active_profile.stream,
            intermediate_buffer=intermediate_buffer,
            chat_options=chat_options,
            mutation_tracker=mutation_tracker,
            mutation_coordinator=mutation_coordinator,
            stream_attempt_timeout=active_profile.http_read_timeout,
            max_transient_retries=transient_retries,
            hook_manager=hook_manager,
            profile_name=profile.name,
            workspace_cwd=runtime.cwd,
            serialize_implicit_windows=serialize_implicit_windows,
            extra_function_middleware=extra_function_middleware,
            run_cycle_start_hooks=(
                web_tools.begin_pass,
                validation_middleware.reset_service_retry_state,
            ),
            hosted_commits_probe=validation_middleware.hosted_commits_observed,
            hosted_commits_in_flight_probe=validation_middleware.hosted_commits_in_flight,
            response_validation_middleware=validation_middleware,
            publish_intermediate_text=_publish_intermediate,
            invocation_publishers=invocation_publishers,
            commit_intermediate_text=commit_intermediate_text,
            tool_result_ceiling_tokens=settings.tool_result_ceiling_tokens,
        )

        return AgentBuildResult(
            prepared=prepared,
            conversation=conversation,
            agent=agent,
            bindings=executor,
            runtime=runtime,
            loop_recorder=loop_recorder,
            reminder_middleware=reminder_middleware,
            sub_agent_tools=sub_agent_tools,
            mcp_adapter=mcp_adapter,
            skills_provider=skills_provider,
            tool_names=tool_names,
            tool_kinds=tool_kinds,
            skill_names=skill_names,
            memory_files=memory_content.loaded_files,
            agent_profile_fingerprint=agent_profile_fingerprint,
            model_profile_fingerprint=model_profile_fingerprint,
            runtime_details=runtime_details,
            compaction_strategy=ctx.compaction_strategy,
            active_profile=active_profile,
        )

    try:
        return await prepared.open(_acquire)
    except BaseException:
        await rollback_resources(prepared)
        raise
