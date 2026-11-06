# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Kernel and ACP construction for workflow agent activations."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.foundation.events.types import Warning
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.retry import TRANSIENT_RETRY_BACKOFF_SECONDS, StreamStall
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_READ, KIND_SHELL, get_tool_kind
from chrys.kernel import RESPONSE_READY_KWARG, LoopRecorder, StallExhaustedAction
from chrys.orchestration.invoker.acp import AcpConversation, AcpInvocationCounters
from chrys.orchestration.invoker.acp_protocol import AcpPermissionBroker, AcpUpdateTranslator
from chrys.orchestration.invoker.acp_spec import resolve_acp_spec
from chrys.orchestration.invoker.attempts import (
    AgentRunKwargs,
    AttemptRecipe,
    AttemptRunner,
    AttemptTaskHandle,
    BlockingCallTiming,
    HistoryRollback,
    ModelRunTrace,
    RestoreAndFallback,
    RetryBoundaryPolicy,
    WireRecipe,
    continuation_token_observer_for,
    has_live_continuation_token,
)
from chrys.orchestration.invoker.child_compaction import ChildCompactionEvents, CompactionRollback
from chrys.orchestration.invoker.child_history import ChildHistory, service_storage_side
from chrys.orchestration.invoker.contracts import (
    FailureDisposition,
)
from chrys.orchestration.invoker.kernel import KernelConversation, KernelPassObserver
from chrys.orchestration.invoker.origin import BoundEmitter
from chrys.orchestration.invoker.resources import Conversation
from chrys.orchestration.invoker.runtime import (
    ApprovalInputs,
    AskUserInputs,
    ChildRecipe,
    ContextInputs,
    LastWordsInputs,
    ReminderInputs,
    SharedRuntime,
    create_approval,
    create_ask_user,
    create_runtime,
    create_validation,
)
from chrys.orchestration.sub_agents.registration import (
    acp_sub_agent_depth,
    sub_agent_registration_state,
    sub_agent_skip_reason,
)
from chrys.orchestration.sub_agents.tools import SubAgentTools
from chrys.service.agent_middleware.control.hook_commit import HookCommitMiddleware
from chrys.service.agent_middleware.control.sleep import SleepMiddleware
from chrys.service.agent_middleware.events.sub_agent_events import SubAgentEventMiddleware
from chrys.service.approval.policy import ApprovalPolicy
from chrys.service.approval.turn_context import TurnContextHolder
from chrys.service.context.compaction.spill import COMPACTIONS_DIR_NAME, sub_agent_dropped_turn_relative_path
from chrys.service.context.compaction.strategy import UnifiedContextStrategy
from chrys.service.context.memory_loader import load_memory_content, memory_truncated_warning
from chrys.service.context.middleware.usage import UsageTrackingMiddleware
from chrys.service.llm.clients import create_client
from chrys.service.llm.route_sessions import derive_llm_route_session_id
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.mcp.thinking_warning import warn_if_tool_loading_unbinds_thinking
from chrys.service.profiles.models.options import effective_chat_options, uses_responses_compact_continuation
from chrys.service.profiles.models.schema import API_STYLE_RESPONSES
from chrys.service.session.sub_agent_logs import SubAgentLogStats
from chrys.service.skills.adapter import create_skills_provider
from chrys.service.state.serializers import serialized_message_payload
from chrys.service.tools import packs as tool_packs
from chrys.service.tools.names import chrys_reserved_tool_names

if TYPE_CHECKING:
    from collections.abc import Sequence

    from chrys.foundation.config.settings import Settings
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.models.workspace import Workspace
    from chrys.kernel import ChatResponse, Message
    from chrys.orchestration.invoker.content_projection import ContentProjector
    from chrys.orchestration.session_usage import SessionUsagePublisher
    from chrys.orchestration.workflows.agent_archive import AgentNodeArchive
    from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
    from chrys.service.approval.judge import ApprovalJudge
    from chrys.service.approval.policy import ApprovalMode
    from chrys.service.context.compaction.spill import SpillQuota
    from chrys.service.hooks.manager import HookManager
    from chrys.service.mcp.cache import MCPConnectionCache
    from chrys.service.mutations.coordination import MutationCoordinator
    from chrys.service.mutations.tracker import MutationTracker
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.schema import ModelProfile
    from chrys.service.tools.background import BackgroundTasks
    from chrys.service.workflows.admission import AgentBinding

logger = logging.getLogger(__name__)

RETRY_BACKOFF_SCHEDULE = TRANSIENT_RETRY_BACKOFF_SECONDS
"""Seconds between a node's in-pass request and ACP connection retries, as the chat agent waits."""


@dataclass(frozen=True, slots=True)
class AgentNodeResources:
    """What every agent node of one run borrows from the session that hosts the run."""

    bus: EventBus
    session_id: str
    session_dir: Path
    approval_anchor: str
    usage_publisher: SessionUsagePublisher
    workspace: Workspace
    settings: Settings
    approval_mode: Callable[[], ApprovalMode]
    approval_judge_for: Callable[[ModelProfile | None], ApprovalJudge | None]
    hook_manager: HookManager | None
    mutation_tracker: MutationTracker | None
    mutation_coordinator: MutationCoordinator | None
    spill_quota: SpillQuota | None
    background_tasks: BackgroundTasks | None
    allow_user_interaction: bool
    mcp_cache: MCPConnectionCache | None
    agent_registry: AgentProfileRegistry | None
    """Resolves the sub-agent profiles a node's agent references; none registers no sub-agent tools."""
    model_registry: ModelProfileRegistry | None
    """Resolves a sub-agent's own model binding; the node's model is the fallback."""


@dataclass(frozen=True, slots=True)
class AgentNodeCallbacks:
    """Lifecycle and accounting callbacks retained by the activation shell."""

    observer: KernelPassObserver
    content: Callable[[], ContentProjector | None]
    """The projector of the pass in flight, which publishes its model text."""
    response_ready: Callable[[ChatResponse], Awaitable[None]]
    checkpoint: Callable[[], Coroutine[Any, Any, None]]
    usage: Callable[..., None]
    side_call_usage: Callable[[Mapping[str, Any]], None]
    wire_retry: Callable[[str, int, int, int, BaseException], Awaitable[None]]
    service_retry: Callable[[str, int, int, int, BaseException], Awaitable[None]]
    interruptible_sleep: Callable[[int], Awaitable[bool]]
    emitter: Callable[[], BoundEmitter]
    """The publisher of the attempt in flight; each scheduler attempt publishes under its own origin."""
    acp_usage: Callable[..., None]
    adopt_translator: Callable[[AcpUpdateTranslator], Awaitable[None]]
    acp_counters: AcpInvocationCounters


@dataclass(frozen=True, slots=True)
class KernelNodeParts:
    backend: KernelConversation
    history: ChildHistory
    events: SubAgentEventMiddleware
    approval: ApprovalMiddleware
    compaction: UnifiedContextStrategy
    run_kwargs: AgentRunKwargs
    trace: ModelRunTrace
    sub_agent_tools: SubAgentTools | None
    """The node's sub-agent registry, when its profile references any; the shell routes controls to it."""
    hosted_unkept: Callable[[Sequence[Message]], tuple[str, ...]]
    """Provider-hosted tool calls the newest model request executed that the given resumed history does not
    keep with that request's landed response: a resumed pass would send that request again."""


@dataclass(frozen=True, slots=True)
class AcpNodeParts:
    backend: AcpConversation


class _AbortProbe:
    def __init__(self, observer: KernelPassObserver) -> None:
        self._observer = observer

    @property
    def is_interrupted(self) -> bool:
        return self._observer.abort_cause() is not None


async def build_kernel_node(
    conversation: Conversation,
    *,
    binding: AgentBinding,
    node_id: str,
    invocation_id: str,
    res: AgentNodeResources,
    archive: AgentNodeArchive,
    callbacks: AgentNodeCallbacks,
    stats: SubAgentLogStats,
) -> KernelNodeParts:
    session_id = res.session_id
    display_name = binding.agent.display_name or binding.agent.name
    model = binding.model
    if model is None:
        raise RuntimeError("A kernel workflow node requires a model binding.")
    profile = binding.agent
    chat_options = effective_chat_options(model)
    client = await create_client(
        model,
        session_id=derive_llm_route_session_id(
            session_id,
            route_kind="workflow-node",
            route_parts=(node_id, profile.name, invocation_id),
            model_profile=model,
        ),
        parent_session_id=session_id,
        session_dir=res.session_dir,
        tool_result_ceiling_tokens=res.settings.tool_result_ceiling_tokens,
    )
    await conversation.own_or_release(client.aclose)
    environment = SessionEnvironment.capture(session_id=res.session_id, workspace=res.workspace)
    tool_build = tool_packs.ToolBuild(
        tools=profile.tools,
        agent_name=profile.name,
        runtime=environment,
        settings=res.settings,
        session_id=session_id,
        session_dir=res.session_dir,
        model_profile_id=model.id,
        chat_options=chat_options,
        vision=model.vision,
        allow_user_interaction=res.allow_user_interaction,
        background_tasks=res.background_tasks,
    )
    assembled_tools = await tool_packs.assemble_tools(tool_build)
    web_tools = assembled_tools.web
    for warning in assembled_tools.warnings:
        await res.bus.publish(warning)
    tools = list(assembled_tools.tools)
    workspace_roots = [res.workspace.primary_cwd, *(wd.path for wd in res.workspace.working_dirs)]
    # Node inputs can be generated by earlier nodes; approval stays anchored to the user's run request.
    turn_context = TurnContextHolder()
    turn_context.replace([res.approval_anchor])
    # Same order as the chat builder: sub-agents, then MCP (which reserves every name so far), then skills.
    sub_agent_tools = await _register_sub_agents(
        conversation,
        profile=profile,
        model=model,
        res=res,
        environment=environment,
        workspace_roots=workspace_roots,
        turn_context=turn_context,
    )
    if sub_agent_tools is not None:
        tools.extend(sub_agent_tools.get_tools())
    mcp_adapter: MCPAdapter | None = None
    if profile.tools.mcp:
        reserved_names = chrys_reserved_tool_names()
        reserved_names.update(tool.name for tool in tools)
        # A provider-hosted web tool owns its name too, so a clashing MCP tool fails
        # the build with the guidance to rename or exclude it.
        reserved_names.update(web_tools.hosted)
        mcp_adapter = MCPAdapter(
            cache=res.mcp_cache,
            stdio_cwd=environment.cwd,
            session_dir=res.session_dir,
            reserved_tool_names=reserved_names,
        )
        conversation.own(mcp_adapter.disconnect_all)
        tools.extend(await mcp_adapter.connect_all(profile.tools.mcp))
        warn_if_tool_loading_unbinds_thinking(profile, model, chat_options, mcp_adapter.tool_names_by_server)
    # Extensions come last: their tools yield every name the agent's other tools take.
    extension_tools = await tool_packs.assemble_extension_tools(
        tool_build,
        profile.extensions,
        taken=[
            *(tool.name for tool in tools),
            *web_tools.hosted,
            *(mcp_adapter.tool_namespace if mcp_adapter is not None else ()),
        ],
    )
    for warning in extension_tools.warnings:
        await res.bus.publish(warning)
    tools.extend(extension_tools.tools)
    web_tools.check_names(tools)
    skills_provider, skill_warnings = await create_skills_provider(
        profile.skills,
        runtime=environment,
        session_dir=res.session_dir,
        project_skills_enabled=res.settings.project_skills_enabled,
    )
    for warn in skill_warnings:
        await res.bus.publish(Warning(code=warn.code, message=warn.message, session_id=session_id))
    # Read once per activation, like the chat agent reads once per build.
    memory_content = load_memory_content(profile.memory, workspace_cwd=res.workspace.primary_cwd)
    if memory_content.truncated:
        await res.bus.publish(memory_truncated_warning(memory_content, session_id=session_id))
    instructions = profile.instructions
    suffix = binding.instructions_suffix
    if suffix:
        instructions = f"{instructions}\n\n{suffix}" if instructions else suffix
    reporting = ChildCompactionEvents(
        emitter=callbacks.emitter,
        name=display_name,
        tool_name=node_id,
        profile=profile.name,
        workspace_cwd=res.workspace.primary_cwd,
        hook_manager=res.hook_manager,
    )
    log_dir = archive.log_dir
    context_inputs = ContextInputs(
        profile=model,
        compaction_config=profile.compaction,
        on_pre_compact=reporting.pre_compact,
        on_context_pressure=reporting.context_pressure,
        debug_log_dir=log_dir,
        spill_quota=res.spill_quota,
        spill_record_dir=sub_agent_dropped_turn_relative_path(node_id, invocation_id, 0).parent,
        include_context_management=False,
        parent_session_id=session_id,
        session_dir=res.session_dir,
        skip_local_history_for_service_context=(model.provider == "openai" and model.api_style == API_STYLE_RESPONSES),
        service_context_default_options=chat_options,
        memory_text=memory_content.text,
    )
    skill_tool_names = skills_provider.tool_names if skills_provider is not None else []
    reminder_inputs = ReminderInputs(
        runtime=environment,
        sub_agent_names=[tool.name for tool in sub_agent_tools.get_tools()] if sub_agent_tools is not None else None,
        shell_tool_enabled=any(get_tool_kind(tool) == KIND_SHELL for tool in tools),
        tool_names=[*(tool.name for tool in tools), *skill_tool_names],
        session_root=res.session_dir,
        file_read_available=any(get_tool_kind(tool) == KIND_FILESYSTEM_READ for tool in tools),
        spill_quota=res.spill_quota,
        catalog_pointer_enabled=False,
        skill_catalog_provider=skills_provider.render_catalog_reminder if skills_provider is not None else None,
        mcp_instructions_provider=mcp_adapter.render_instructions_reminder if mcp_adapter is not None else None,
    )
    last_words_inputs = LastWordsInputs(
        profile=model,
        template=profile.compaction.last_words_template,
        max_output_tokens=profile.compaction.last_words_max_output_tokens,
        session_id=derive_llm_route_session_id(
            session_id,
            route_kind="last-words",
            route_parts=(node_id, profile.name, invocation_id),
            model_profile=model,
        ),
        parent_session_id=session_id,
        session_dir=res.session_dir,
        max_transient_retries=res.settings.max_transient_retries,
        report_usage=callbacks.side_call_usage,
        publish_retry=reporting.retry,
        publish_status=reporting.status,
        log_dir=log_dir,
    )
    providers: list[Any] = []
    if skills_provider is not None:
        providers.append(skills_provider)
    if mcp_adapter is not None and (resume_provider := mcp_adapter.create_resume_provider()) is not None:
        # After skills, so newly advertised always-load tools see every visible name.
        providers.append(resume_provider)
    runtime = create_runtime(
        conversation,
        SharedRuntime(
            client=client,
            tools=tools,
            providers=providers,
            name=display_name,
            instructions=instructions,
            vision=model.vision,
        ),
        ChildRecipe(
            context=context_inputs,
            reminder=reminder_inputs,
            last_words=last_words_inputs,
            registration=False,
        ),
    )
    agent = runtime.agent
    await agent.__aenter__()
    await conversation.own_or_release(lambda: agent.__aexit__(None, None, None))
    runtime.reminder.prepare_turn()
    ctx = runtime.context

    sub_event_mw = SubAgentEventMiddleware(
        res.bus,
        display_name,
        invocation_id,
        mutation_tracker=res.mutation_tracker,
        hook_manager=res.hook_manager,
        profile_name=profile.name,
        session_id=session_id,
        workspace_cwd=res.workspace.primary_cwd,
        mutation_coordinator=res.mutation_coordinator,
        tool_result_ceiling_tokens=res.settings.tool_result_ceiling_tokens,
        stats=stats,
        origin=callbacks.emitter().origin,
    )
    middleware: list[Any] = [sub_event_mw]
    if res.allow_user_interaction:
        middleware.append(
            create_ask_user(
                conversation,
                AskUserInputs(
                    event_bus=res.bus,
                    session_id=session_id,
                    caller_name=display_name,
                    timeout_seconds=res.settings.ask_user_timeout_seconds,
                ),
            )
        )
    approval = await create_approval(
        conversation,
        ApprovalInputs(
            approval_policy=ApprovalPolicy(profile.approval, tools=tools),
            event_bus=res.bus,
            session_id=session_id,
            tool_kinds={tool.name: kind for tool in tools if (kind := get_tool_kind(tool))},
            caller_name=display_name,
            workspace_roots=workspace_roots,
            approval_mode=res.approval_mode(),
            approval_judge=res.approval_judge_for(model),
            approval_log_dir=res.session_dir / "approvals",
            workspace_cwd=res.workspace.primary_cwd,
            dev_mode=False,
            hook_manager=res.hook_manager,
            profile_name=profile.name,
            session_archive_read_roots=[res.session_dir / COMPACTIONS_DIR_NAME],
            turn_context=turn_context,
        ),
    )
    approval.bind_publisher(callbacks.emitter())
    middleware.append(approval)
    middleware.append(SleepMiddleware(res.bus, session_id=session_id, background_tasks=res.background_tasks))
    # Innermost function middleware: runs the version the event middleware's hooks checked, or nothing.
    middleware.append(HookCommitMiddleware())
    # Innermost last: validation judges each landed response; usage, under it, counts every one first.
    validation = create_validation(conversation)
    middleware.append(validation)
    middleware.append(
        UsageTrackingMiddleware(
            max_context_tokens=ctx.usage_middleware.max_context_tokens,
            warn_threshold_pct=ctx.usage_middleware.warn_threshold_pct,
            on_usage=callbacks.usage,
            compaction_strategy=ctx.compaction_strategy,
            use_local_context_estimate_for_hosted_usage=ctx.use_local_context_estimate_for_hosted_usage,
        )
    )

    loop_recorder = LoopRecorder(
        capture_service_loop_messages=uses_responses_compact_continuation(model, chat_options),
        on_result_checkpoint=callbacks.checkpoint,
        message_hasher=serialized_message_payload,
    )
    session = runtime.create_session()
    client_kwargs: dict[str, Any] = {"loop_recorder": loop_recorder}
    run_kwargs: AgentRunKwargs = {
        "session": session,
        "client_kwargs": client_kwargs,
        "middleware": middleware,
        "compaction_strategy": ctx.compaction_strategy,
        "tokenizer": ctx.compaction_strategy.tokenizer,
    }
    if chat_options:
        options_copy = dict(chat_options)
        if isinstance(extra_body := options_copy.get("extra_body"), dict):
            options_copy["extra_body"] = dict(extra_body)
        run_kwargs["options"] = options_copy
    history = ChildHistory(session, loop_recorder)
    service_storage = service_storage_side(client, run_kwargs.get("options"))
    compaction = CompactionRollback(ctx.compaction_strategy)
    # A failed request is retried where it failed, with the chat agent's budget and backoff: in place on
    # the wire under local storage, as a whole run under service-side storage, where the wire may not.
    max_retries = res.settings.effective_max_transient_retries()
    if not service_storage:
        client_kwargs["wire_retry_policy"] = WireRecipe(
            max_retries=max_retries,
            stall_timeout_seconds=model.http_read_timeout,
            stall_max_retries=max_retries,
            stall_exhausted_action=StallExhaustedAction.BLOCKING_FALLBACK,
            backoff_schedule=RETRY_BACKOFF_SCHEDULE,
            interrupted=lambda: callbacks.observer.abort_cause() is not None,
            interruptible_sleep=callbacks.interruptible_sleep,
            publish_retry=callbacks.wire_retry,
            prepare_retry=None,
            hosted_commits_in_flight=validation.hosted_commits_in_flight,
            answer_shown=None,
        ).build()
    # A background response a failed poll left running is resumed by the retry, never created twice.
    client_kwargs["continuation_token_observer"] = continuation_token_observer_for(run_kwargs)
    client_kwargs[RESPONSE_READY_KWARG] = callbacks.response_ready

    def check_abort() -> None:
        if callbacks.observer.abort_cause() is not None:
            raise asyncio.CancelledError

    attempt_handle = AttemptTaskHandle()
    recipe = AttemptRecipe(
        stall_exhaustion=RestoreAndFallback(sub_event_mw.reject_hosted_attempt),
        stall_error=lambda timeout: StreamStall(f"no streaming updates received for {timeout:g}s"),
        retry_boundary=RetryBoundaryPolicy.OBSERVE,
        blocking_call_timing=BlockingCallTiming.BEFORE_ATTEMPT_TASK,
        before_attempt=check_abort,
        history_state=history.state,
    )
    rollback = HistoryRollback(
        session,
        history_state=recipe.history_state,
        snapshot_caller=compaction.snapshot,
        restore_caller=compaction.restore,
    )
    attempt_trace = ModelRunTrace(
        interrupted=lambda: callbacks.observer.abort_cause() is not None,
        service_side=lambda: service_storage,
        committed=lambda: loop_recorder.committed_count > 0,
    )
    attempts = AttemptRunner(
        agent=agent,
        session=session,
        handle=attempt_handle,
        rollback=rollback,
        retry_participant=None,
        interrupt=_AbortProbe(callbacks.observer),
        trace=attempt_trace,
        stream_observer=callbacks.content,
        content_exposed=lambda: (projector := callbacks.content()) is not None and projector.exposed,
        publish_retry=callbacks.service_retry,
        interruptible_sleep=callbacks.interruptible_sleep,
        max_retries=lambda: max_retries,
        backoff_schedule=lambda: RETRY_BACKOFF_SCHEDULE,
        stream_timeout=lambda: model.http_read_timeout,
        committed_count=lambda: loop_recorder.committed_count,
        hosted_commits=validation.hosted_commits_observed,
        recipe=recipe,
    )
    backend = KernelConversation(
        owner=conversation,
        session=session,
        attempts=attempts,
        attempt_handle=attempt_handle,
        observer=callbacks.observer,
        run_kwargs=lambda: run_kwargs,
        stream=lambda: model.stream,
        service_side=lambda: service_storage,
        recorder=loop_recorder,
        hosted_observed=validation.hosted_commits_observed,
        start_hooks=(
            web_tools.begin_pass,
            lambda: validation.set_observation_hook(sub_event_mw.begin_hosted_pass()),
            lambda: validation.begin_pass_hosted_baseline(
                resumes_background_response=has_live_continuation_token(run_kwargs)
            ),
        ),
        failure_disposition=FailureDisposition.CALLER_DECISION,
    )
    return KernelNodeParts(
        backend,
        history,
        sub_event_mw,
        approval,
        ctx.compaction_strategy,
        run_kwargs,
        attempt_trace,
        sub_agent_tools,
        lambda kept: validation.hosted_commits_in_flight_unkept(kept, loop_recorder.landed_response),
    )


async def _register_sub_agents(
    conversation: Conversation,
    *,
    profile: AgentProfile,
    model: ModelProfile,
    res: AgentNodeResources,
    environment: SessionEnvironment,
    workspace_roots: list[str],
    turn_context: TurnContextHolder,
) -> SubAgentTools | None:
    """Register the profile's sub-agents as tools of this node, as the chat builder does for its agent.

    The registry is owned by *conversation* from construction, so an activation that fails part-way
    through registration still releases the sub-agents it prepared.
    """
    if not profile.sub_agents.agents or res.agent_registry is None:
        return None
    sub_agent_tools = SubAgentTools(
        max_total_concurrency=profile.sub_agents.max_total_concurrency,
        event_bus=res.bus,
        session_id=res.session_id,
        session_dir=res.session_dir,
        on_sub_agent_usage=res.usage_publisher.accumulate_invocation_usage,
        on_side_call_usage=res.usage_publisher.accumulate_side_call_usage,
        drain_parent_usage_publishes=res.usage_publisher.drain,
        parent_approval=profile.approval,
        mutation_tracker=res.mutation_tracker,
        mutation_coordinator=res.mutation_coordinator,
        approval_mode=res.approval_mode(),
        approval_judge=res.approval_judge_for(model),
        workspace_roots=workspace_roots,
        approval_log_dir=res.session_dir / "approvals",
        mcp_cache=res.mcp_cache,
        hook_manager=res.hook_manager,
        workspace_cwd=environment.cwd,
        serialize_implicit_windows=not res.settings.parallel_implicit_tools,
        spill_quota=res.spill_quota,
        background_tasks=res.background_tasks,
        ask_user_timeout_seconds=res.settings.ask_user_timeout_seconds,
        turn_context=turn_context,
        max_transient_retries=res.settings.effective_max_transient_retries(),
        tool_result_ceiling_tokens=res.settings.tool_result_ceiling_tokens,
        allow_user_interaction=res.allow_user_interaction,
        # The workflow surfaces (graph, node transcript, headless run) show no per-child card, so a
        # failed child cannot wait for a decision: it ends, and its error reaches the node's model.
        human_failure_decisions=False,
        # Nor any of a child's text, so the text it wrote never stops a retry.
        shows_answer_text=False,
    )
    conversation.own(sub_agent_tools.cleanup)
    acp_depth = acp_sub_agent_depth()
    for ref in profile.sub_agents.agents:
        sub_profile = res.agent_registry.get(ref.profile)
        state = sub_agent_registration_state(sub_profile, acp_depth=acp_depth)
        if sub_profile is None or state != "registered":
            logger.warning(
                "Skipping sub-agent profile %s: %s", ref.profile, sub_agent_skip_reason(state, acp_depth=acp_depth)
            )
            continue
        if sub_profile.acp is not None:
            await sub_agent_tools.register_acp(ref, sub_profile, environment)
        else:
            await sub_agent_tools.register(
                ref,
                sub_profile,
                environment,
                settings=res.settings,
                fallback_profile=model,
                model_registry=res.model_registry,
            )
    return sub_agent_tools


def build_acp_node(
    conversation: Conversation,
    *,
    binding: AgentBinding,
    node_id: str,
    invocation_id: str,
    res: AgentNodeResources,
    archive: AgentNodeArchive,
    callbacks: AgentNodeCallbacks,
) -> AcpNodeParts:
    session_id = res.session_id
    display_name = binding.agent.display_name or binding.agent.name
    config = binding.agent.acp
    if config is None:
        raise RuntimeError("An ACP workflow node requires ACP configuration.")
    if binding.model is not None:
        config = replace(config, model_id=binding.model.model_id)
    environment = SessionEnvironment.capture(session_id=res.session_id, workspace=res.workspace)
    roots = [res.workspace.primary_cwd, *(wd.path for wd in res.workspace.working_dirs)]
    turn_context = TurnContextHolder()
    turn_context.replace([res.approval_anchor])
    broker = AcpPermissionBroker(
        event_bus=res.bus,
        session_id=session_id,
        caller_name=display_name,
        mode_getter=res.approval_mode,
        turn_context=turn_context,
        workspace_roots=roots,
        workspace_cwd=environment.cwd,
        approval_judge=res.approval_judge_for(binding.model),
        ask_user_timeout_seconds=res.settings.ask_user_timeout_seconds,
        allow_user_interaction=res.allow_user_interaction,
    )
    stderr_path = archive.stderr_path
    backend = AcpConversation(
        tool_name=node_id,
        agent_name=display_name,
        prompt="",
        origin=callbacks.emitter().origin,
        spec_factory=lambda _attempt: resolve_acp_spec(config, environment, stderr_path, workspace_roots=roots),
        broker=broker,
        event_bus=res.bus,
        terminal_projection=lambda _result: None,
        pass_started=lambda: None,
        session_id=session_id,
        result_mode=config.result_mode,
        usage_callback=callbacks.acp_usage,
        translator_callback=callbacks.adopt_translator,
        counters=callbacks.acp_counters,
        backoff_schedule=RETRY_BACKOFF_SCHEDULE,
    )
    conversation.own(backend.aclose)
    return AcpNodeParts(backend)
