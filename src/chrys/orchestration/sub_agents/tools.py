# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SubAgentTools — manages sub-agent instances and concurrency control.

Creates FunctionTool wrappers around sub-agents so the parent agent can
delegate tasks via tool calls.  Enforces two levels of concurrency limits:

- **Global**: ``max_total_concurrency`` caps total active sub-agent calls.
- **Per-agent**: ``SubAgentRef.max_concurrency`` caps calls to a single sub-agent.

When either limit is exceeded, the tool returns an error string immediately
rather than blocking.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
from contextvars import Token
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from chrys.foundation.config.settings import DEFAULT_ASK_USER_TIMEOUT_SECONDS
from chrys.foundation.events.types import (
    InvocationCompactionCommitted,
    InvocationCompactionFinished,
    InvocationCompactionStarted,
    InvocationContextPressure,
    InvocationRetryAttempt,
    InvocationStarted,
    Warning,
)
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.platform.files import (
    secure_unlink_owner_verified,
)
from chrys.foundation.retry import RetryAttemptInfo
from chrys.foundation.tool_kinds import (
    KIND_ASK_USER,
    KIND_FILESYSTEM_READ,
    KIND_SHELL,
    KIND_SUB_AGENT,
    get_tool_kind,
    set_tool_kind,
)
from chrys.foundation.tool_result_metadata import tool_result_metadata_failure_state
from chrys.foundation.trajectory.context import TRAJECTORY_CONTEXT_KWARG, trajectory_scope
from chrys.foundation.trajectory.event_types import TraceCoverage
from chrys.foundation.util.filenames import safe_fs_name
from chrys.foundation.util.once_close import finish_close
from chrys.foundation.util.sub_agent_context import (
    SubAgentParentResultMetadata,
    sub_agent_parent_call_id,
    sub_agent_parent_result_metadata,
)
from chrys.kernel import (
    RESPONSE_READY_KWARG,
    Agent,
    FunctionTool,
    LoopRecorder,
    Message,
    StallExhaustedAction,
)
from chrys.orchestration.invoker.attempts import AgentRunKwargs, WireRecipe
from chrys.orchestration.invoker.child_history import service_storage_side
from chrys.orchestration.invoker.contracts import SubAgentFailureReason, SubAgentStatus, Unbind
from chrys.orchestration.invoker.origin import BoundEmitter, current_invocation_origin, invocation_routing_key
from chrys.orchestration.invoker.resources import Conversation, OperationLifetime, PreparedAgent, rollback_resources
from chrys.orchestration.invoker.runtime import (
    ApprovalInputs,
    AskUserInputs,
    ChildRecipe,
    ContextInputs,
    KernelRuntime,
    LastWordsInputs,
    ReminderInputs,
    SharedRuntime,
    create_approval,
    create_ask_user,
    create_runtime,
    create_validation,
)
from chrys.orchestration.sub_agents.kernel_policy import KernelSubAgentPolicy
from chrys.service.agent_middleware import (
    ApprovalMiddleware,
    SleepMiddleware,
    SubAgentEventMiddleware,
    SubAgentStatsMiddleware,
)
from chrys.service.agent_middleware.control.hook_commit import HookCommitMiddleware
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.approval.turn_context import TurnContextHolder
from chrys.service.context.compaction.last_words_state import DropRoundBreakerState
from chrys.service.context.compaction.spill import COMPACTIONS_DIR_NAME, sub_agent_dropped_turn_relative_path
from chrys.service.context.manager import ContextManager
from chrys.service.context.middleware.usage import UsageTrackingMiddleware
from chrys.service.hooks.events import HookEvent
from chrys.service.llm.clients import create_client, effective_model_base_url
from chrys.service.llm.route_sessions import derive_llm_route_session_id, llm_parent_session_id, llm_route_session_id
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.mcp.thinking_warning import warn_if_tool_loading_unbinds_thinking
from chrys.service.profiles.agents.schema import DEFAULT_SUB_AGENT_CONCURRENCY
from chrys.service.profiles.models.options import effective_chat_options, uses_responses_compact_continuation
from chrys.service.profiles.models.schema import API_STYLE_RESPONSES
from chrys.service.session.message_metadata import TOOL_RESULT_METADATA_KEY
from chrys.service.session.sub_agent_logs import (
    SubAgentLogStats,
    SubAgentModelProvenance,
    SubAgentSessionLogWriter,
    pending_dir,
    sanitize_base_url_origin,
    sessions_dir,
)
from chrys.service.state.serializers import serialized_message_payload
from chrys.service.tools.background import TaskScope
from chrys.service.tools.builtins.web.build import assemble_web_tools
from chrys.service.tools.names import chrys_reserved_tool_names
from chrys.service.tools.registry import ToolRegistry
from chrys.service.tools.result_metadata import tool_error, tool_result_metadata
from chrys.service.trajectory.sub_agents import SubAgentStatus as TrajectorySubAgentStatus
from chrys.service.trajectory.sub_agents import SubAgentTrace
from chrys.service.vision import filter_image_tools

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Mapping

    from chrys.foundation.config.settings import Settings
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.orchestration.invoker.acp_protocol import AcpPermissionBroker
    from chrys.orchestration.sub_agents.acp_policy import AcpSubAgentPolicy
    from chrys.service.approval.judge import ApprovalJudge
    from chrys.service.context.compaction import PreCompactInfo
    from chrys.service.context.compaction.last_words import CompactionStatus
    from chrys.service.context.compaction.spill import SpillQuota
    from chrys.service.hooks.manager import HookManager
    from chrys.service.mcp.cache import MCPConnectionCache
    from chrys.service.mutations.coordination import MutationCoordinator
    from chrys.service.mutations.tracker import MutationTracker
    from chrys.service.profiles.agents.schema import AgentProfile, ApprovalConfig, SubAgentRef
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.schema import ModelProfile
    from chrys.service.skills.provider import ChrysSkillsProvider
    from chrys.service.tools.background import BackgroundTask, BackgroundTasks

from .shell import SubAgentToolShell

logger = logging.getLogger(__name__)


# Item cap for the cross-attempt ACP audit-update trail — matches the
# write-time envelope ring (``normalize_acp_state``'s ``_ACP_RING_ITEMS``),
# which also applies the authoritative byte bound on every write.
_ACP_AUDIT_TRAIL_ITEMS = 500
# Mirrors the write-time attempt ring in ``normalize_acp_state``
# (``_ACP_ATTEMPT_ITEMS``): manual retries are unlimited, so the in-memory
# attempt history must be bounded the same newest-wins way.
_ACP_AUDIT_ATTEMPT_ITEMS = 50


def _execution_note(per_agent_max: int, max_total: int) -> str:
    """Describe execution and concurrency limits for either sub-agent backend."""
    return (
        f"\n(Concurrency limits: max {per_agent_max} concurrent calls to this tool, "
        f"max {max_total} concurrent sub-agent calls total. "
        "Exceeding either limit returns an error.)\n"
        "This call waits for the sub-agent's result. Calls in the same response are dispatched "
        "together with no guaranteed execution order. The calling agent resumes only after the entire "
        "batch returns. Calls that depend on this result or could conflict through shared state must go "
        "in a later response. No asynchronous task handle is provided."
    )


def _close_sub_agent_trace(
    trace: SubAgentTrace | None,
    *,
    hook_status: str,
    failure_code: str | None,
    controller: SubAgentToolShell | None = None,
    failure_reason: SubAgentFailureReason | None = None,
) -> None:
    """Close the boundary from the invocation's terminal status (queued, never awaited: cleanup path).

    ``hook_status`` is read off the result text, so a child that reports its
    failure in prose records as a success, and one whose answer merely opens
    like an error records as a failure. The controller knows which it was;
    where it has run, its verdict decides — with the structured result
    metadata deciding what its terminal status leaves open.
    """
    if trace is None:
        return
    if hook_status == "cancelled":
        trace.finished_soon(status=TrajectorySubAgentStatus.CANCELLED)
        return
    succeeded = hook_status == "ok"
    reason_code = failure_code
    if controller is not None:
        if controller.status == SubAgentStatus.COMPLETED:
            # COMPLETED says the invocation reached its end, not that the work
            # succeeded: a controller that gives up (spawn, auth, refusal)
            # still completes, and records the failure structurally on its way
            # out. That verdict decides where it exists; the prose never does.
            succeeded = tool_result_metadata_failure_state(tool_result_metadata.get(None)) is not True
        elif controller.status in (SubAgentStatus.ABORTED, SubAgentStatus.CASCADE_ABORTED):
            succeeded = False
            reason_code = failure_code or controller.status.value
        if failure_reason is not None:
            reason_code = failure_reason.value
    if succeeded:
        trace.finished_soon(status=TrajectorySubAgentStatus.OK)
        return
    trace.finished_soon(
        status=TrajectorySubAgentStatus.SETUP_FAILED
        if failure_code == "setup_failed"
        else TrajectorySubAgentStatus.FAILED,
        failure_reason_code=reason_code,
    )


def _parent_result_replay_metadata(metadata: SubAgentParentResultMetadata) -> dict[str, Any]:
    result_metadata: dict[str, Any] = {}
    if metadata.sub_agent_invocation_id:
        result_metadata["sub_agent_invocation_id"] = metadata.sub_agent_invocation_id
    if metadata.sub_agent_log_file:
        result_metadata["sub_agent_log_file"] = metadata.sub_agent_log_file
    if metadata.sub_agent_audit_complete:
        result_metadata["sub_agent_audit_complete"] = True
    return result_metadata


def _commit_interrupted_parent_result(metadata: SubAgentParentResultMetadata | None) -> None:
    if metadata is None or metadata.commit_interrupted_result is None or metadata.interrupted_result_committed:
        return
    metadata.commit_interrupted_result(_parent_result_replay_metadata(metadata))
    metadata.interrupted_result_committed = True


def _upgrade_interrupted_parent_result(metadata: SubAgentParentResultMetadata | None) -> None:
    """Merge late audit references into an already committed interrupted result."""
    if metadata is None or metadata.commit_interrupted_result is None or not metadata.interrupted_result_committed:
        return
    metadata.commit_interrupted_result(_parent_result_replay_metadata(metadata))


async def _finish_cancellation_cleanup(awaitable: Coroutine[Any, Any, None]) -> None:
    task = asyncio.create_task(awaitable)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    try:
        task.result()
    except Exception:
        logger.debug("sub-agent cancellation finalization failed", exc_info=True)


def _acp_stderr_path(
    parent_session_dir: Path | None,
    writer: SubAgentSessionLogWriter | None,
    invocation_id: str,
) -> Path:
    if parent_session_dir is not None and writer is not None:
        return sessions_dir(parent_session_dir) / f"{writer.log_file_name}.stderr.log"
    return Path(tempfile.gettempdir()) / f"chrys-acp-{invocation_id}.stderr.log"


class SubAgentTools:
    """Manages sub-agent lifecycle and exposes them as FunctionTools.

    Each sub-agent runs with an independent session (``propagate_session=False``).
    Concurrency is enforced at two levels:

    - ``_total_active``: shared counter across all sub-agents.
    - ``_agent_active[name]``: per-agent counter.

    Both are checked before execution; exceeding either returns an error
    string to the model.
    """

    def __init__(
        self,
        max_total_concurrency: int = DEFAULT_SUB_AGENT_CONCURRENCY,
        event_bus: EventBus | None = None,
        session_id: str | None = None,
        session_dir: Path | None = None,
        on_sub_agent_usage: Callable[..., None] | None = None,
        on_side_call_usage: Callable[[Mapping[str, Any]], None] | None = None,
        drain_parent_usage_publishes: Callable[[], Awaitable[None]] | None = None,
        parent_approval: ApprovalConfig | None = None,
        mutation_tracker: MutationTracker | None = None,
        mutation_coordinator: MutationCoordinator | None = None,
        approval_mode: ApprovalMode = ApprovalMode.MANUAL,
        approval_judge: ApprovalJudge | None = None,
        workspace_roots: list[str] | None = None,
        approval_log_dir: Path | None = None,
        mcp_cache: MCPConnectionCache | None = None,
        hook_manager: HookManager | None = None,
        workspace_cwd: str = "",
        serialize_implicit_windows: bool = False,
        spill_quota: SpillQuota | None = None,
        background_tasks: BackgroundTasks | None = None,
        ask_user_timeout_seconds: int | None = DEFAULT_ASK_USER_TIMEOUT_SECONDS,
        turn_context: TurnContextHolder | None = None,
        max_transient_retries: int | None = None,
        tool_result_ceiling_tokens: int | None = None,
        allow_user_interaction: bool = True,
        human_failure_decisions: bool = True,
        shows_answer_text: bool = True,
    ) -> None:
        self._max_total = max_total_concurrency
        self._bus = event_bus
        self._session_id = session_id
        self._session_dir = session_dir
        self._on_sub_agent_usage = on_sub_agent_usage
        self._on_side_call_usage = on_side_call_usage
        # Awaited before ``_invoke`` returns so the sub-agent's final
        # ``UsageUpdate`` — queued on the parent engine's
        # ``_usage_publish_tail`` via ``_track_usage_publish`` — is delivered
        # before the parent's ``InvocationToolCallResult``.  Without this, a slow
        # subscriber can let the parent tool result overtake the trailing
        # sub-agent usage event.
        self._drain_parent_usage_publishes = drain_parent_usage_publishes
        self._parent_approval = parent_approval
        self._mutation_tracker = mutation_tracker
        self._mutation_coordinator = mutation_coordinator
        self._approval_mode = approval_mode
        self._approval_judge = approval_judge
        self._workspace_roots = list(workspace_roots or [])
        self._approval_log_dir = approval_log_dir
        self._mcp_cache = mcp_cache
        self._hook_manager = hook_manager
        self._workspace_cwd = workspace_cwd
        self._serialize_implicit_windows = serialize_implicit_windows
        self._spill_quota = spill_quota
        self._background_tasks = background_tasks
        # Per-tool profile name for hook payloads.  Filled in
        # :meth:`register`; reads on missing keys fall back to the bare
        # tool name in :meth:`_make_tool._invoke`.
        self._hook_profile_names: dict[str, str] = {}
        self._total_active = 0
        self._runtime: SessionEnvironment | None = None
        self._ask_user_timeout_seconds = ask_user_timeout_seconds
        self._max_transient_retries = max_transient_retries
        self._tool_result_ceiling_tokens = tool_result_ceiling_tokens
        self._allow_user_interaction = allow_user_interaction
        # Whether a failed child pauses for the user's Retry/Abort on its card; a caller whose
        # surface shows no such card ends the child instead, its error becoming the tool result.
        self._human_failure_decisions = human_failure_decisions
        # Whether a frontend shows a kernel child's answer text: only then does text it wrote stop a retry.
        self._shows_answer_text = shows_answer_text
        self._turn_context = turn_context or TurnContextHolder()
        self._agent_active: dict[str, int] = {}  # tool_name → active count
        self._agent_max: dict[str, int] = {}  # tool_name → per-agent limit
        self._prepared_by_tool: dict[str, PreparedAgent] = {}
        self._agents: dict[str, Agent] = {}  # tool_name → Agent
        self._context_managers: dict[str, ContextManager] = {}  # tool_name → ContextManager
        self._mcp_adapters: dict[str, MCPAdapter] = {}  # tool_name → MCPAdapter
        self._chat_options_by_tool: dict[str, dict[str, Any] | None] = {}  # tool_name → agent.run(options=...)
        self._model_provenance_by_tool: dict[str, SubAgentModelProvenance] = {}
        self._approval_policies: dict[str, ApprovalPolicy] = {}  # tool_name → ApprovalPolicy
        self._tool_kinds: dict[str, dict[str, str]] = {}  # tool_name → {fn_name → kind}
        self._display_names: dict[str, str] = {}  # tool_name → human-readable agent name
        self._acp_profiles: dict[str, AgentProfile] = {}
        self._tools: list[FunctionTool] = []
        # Live ApprovalMiddleware instances for in-flight sub-agent runs.
        # Mid-run mode switches must propagate into these so subsequent tool
        # calls within the same run pick up the new mode.
        self._live_approvals: list[ApprovalMiddleware] = []
        # Live controllers keyed by invocation_id.  The engine routes
        # InvocationRetryRequested / InvocationAbortRequested events through
        # this registry so the right sub-agent receives the decision.
        self._controllers: dict[str, SubAgentToolShell] = {}
        self._pending_cleanup_paths: set[Path] = set()
        self._invocation_emitters: dict[str, BoundEmitter] = {}

    def _emitter_for(self, invocation_id: str) -> BoundEmitter:
        """Resolve a registered callback key; missing keys never borrow identity."""
        try:
            return self._invocation_emitters[invocation_id]
        except KeyError as exc:
            raise ValueError("Cannot route an unbound invocation callback") from exc

    async def register(
        self,
        ref: SubAgentRef,
        profile: AgentProfile,
        runtime: SessionEnvironment,
        *,
        settings: Settings,
        fallback_profile: ModelProfile,
        model_registry: ModelProfileRegistry | None = None,
    ) -> None:
        """Build a sub-agent from a profile and register it as a tool.

        ``fallback_profile`` is the parent agent's resolved
        ``ModelProfile`` — used when this sub-agent's
        ``profile.model.profile_id`` is empty or references a deleted
        profile.  Sub-agents with their own model binding use that profile;
        unbound sub-agents inherit the parent's resolved profile.  The
        command-line ``--model`` flag changes only the active fallback model,
        so it does not replace sub-agent model bindings.
        """
        prepared = PreparedAgent()

        async def _acquire(conversation: Conversation) -> None:
            from chrys.service.profiles.models.resolver import resolve_for_agent

            sub_active_profile = resolve_for_agent(model_registry, settings, profile, fallback=fallback_profile)

            self._runtime = runtime
            self._ask_user_timeout_seconds = settings.ask_user_timeout_seconds
            runtime_cwd = runtime.cwd if isinstance(runtime.cwd, str) else None
            if not self._workspace_cwd and runtime_cwd is not None:
                self._workspace_cwd = runtime_cwd
            chat_options = effective_chat_options(sub_active_profile)
            capture_service_loop_messages = uses_responses_compact_continuation(sub_active_profile, chat_options)
            tool_name = ref.tool_name or profile.name
            sub_agent_session_id = derive_llm_route_session_id(
                self._session_id,
                route_kind="sub-agent",
                route_parts=(tool_name, profile.name),
                model_profile=sub_active_profile,
            )
            last_words_session_id = derive_llm_route_session_id(
                self._session_id,
                route_kind="last-words",
                route_parts=(tool_name, profile.name),
                model_profile=sub_active_profile,
            )
            client = await create_client(
                sub_active_profile,
                session_id=sub_agent_session_id,
                parent_session_id=self._session_id,
                use_route_session_context=True,
                session_dir=self._session_dir,
                tool_result_ceiling_tokens=self._tool_result_ceiling_tokens,
            )
            await prepared.own_or_release(client.aclose)
            model_provenance = SubAgentModelProvenance(
                provider=sub_active_profile.provider,
                api_style=sub_active_profile.api_style,
                model_id=sub_active_profile.model_id,
                base_url_origin=sanitize_base_url_origin(effective_model_base_url(sub_active_profile)),
            )

            # Build sub-agent's own tools from its profile
            tool_registry = ToolRegistry(vision_enabled=sub_active_profile.vision)
            builtin_categories = profile.tools.builtins or []
            web_tools = assemble_web_tools(
                builtin_categories,
                settings,
                profile.tools.web_search,
                profile.tools.web_fetch,
                chat_options=chat_options,
                agent=profile.name,
                model_profile_id=sub_active_profile.id,
                session_id=self._session_id,
            )
            tool_registry.load_builtins(
                builtin_categories,
                runtime=runtime,
                settings=settings,
                shell_filter_config=profile.tools.shell_filter,
                session_id=self._session_id,
                session_dir=self._session_dir,
                web=web_tools,
                background_tasks=self._background_tasks,
            )
            if self._bus is not None:
                for warning in web_tools.warnings:
                    await self._bus.publish(warning)

            tools = filter_image_tools(tool_registry.get_all(), vision_enabled=sub_active_profile.vision)
            if not self._allow_user_interaction:
                tools = [tool for tool in tools if get_tool_kind(tool) != KIND_ASK_USER]

            # Load MCP tools from the sub-agent's profile
            mcp_adapter: MCPAdapter | None = None
            if profile.tools.mcp:
                reserved_tool_names = chrys_reserved_tool_names()
                reserved_tool_names.update(tool.name for tool in tools)
                # A provider-hosted web tool owns its name too, so a clashing MCP tool fails
                # the build with the guidance to rename or exclude it.
                reserved_tool_names.update(web_tools.hosted)
                mcp_adapter = MCPAdapter(
                    cache=self._mcp_cache,
                    stdio_cwd=runtime_cwd,
                    session_dir=self._session_dir,
                    reserved_tool_names=reserved_tool_names,
                )
                prepared.own(mcp_adapter.disconnect_all)
                mcp_tools = await mcp_adapter.connect_all(profile.tools.mcp)
                self._mcp_adapters[tool_name] = mcp_adapter
                tools.extend(mcp_tools)
                warn_if_tool_loading_unbinds_thinking(
                    profile, sub_active_profile, chat_options, mcp_adapter.tool_names_by_server
                )

            # Wire compaction + usage tracking for sub-agents.  Compression
            # tools (compress_context, recall_context, list_compressed_contexts)
            # are disabled — sub-agents are single-turn and don't need cross-turn
            # context management.
            # The registered template runs no invocation of its own, so no origin to name.
            async def _on_pre_compact(info: PreCompactInfo) -> None:
                await self._fire_pre_compact_hook(tool_name, info, origin=None)

            def _publish_context_pressure(
                reason: str,
                breaker: DropRoundBreakerState,
                budget: int,
            ) -> Awaitable[None] | None:
                invocation_id = invocation_routing_key.get()
                if self._bus is None:
                    return None
                emitter = self._emitter_for(invocation_id)
                return emitter.publish(
                    InvocationContextPressure(
                        origin=emitter.origin,
                        reason=reason,
                        attempts=breaker.attempts,
                        side_call_tokens=breaker.side_call_tokens,
                        side_call_token_budget=budget,
                        source="sub_agent",
                        session_id=self._session_id,
                    )
                )

            sub_log_dir: Path | None = None
            if self._session_dir:
                sub_log_dir = self._session_dir / "compactions" / "debug" / "sub_agents" / safe_fs_name(tool_name)

            context_inputs = ContextInputs(
                profile=sub_active_profile,
                compaction_config=profile.compaction,
                on_pre_compact=_on_pre_compact,
                on_context_pressure=_publish_context_pressure,
                spill_quota=self._spill_quota,
                spill_record_dir=sub_agent_dropped_turn_relative_path(tool_name, "registered", 0).parent,
                debug_log_dir=sub_log_dir,
                include_context_management=False,
                parent_session_id=self._session_id,
                session_dir=self._session_dir,
                skip_local_history_for_service_context=(
                    sub_active_profile.provider == "openai" and sub_active_profile.api_style == API_STYLE_RESPONSES
                ),
                service_context_default_options=chat_options,
            )

            # Load skills from the sub-agent's profile
            shared_providers = []
            progressive_resume_provider = mcp_adapter.create_resume_provider() if mcp_adapter is not None else None
            from chrys.service.skills.adapter import create_skills_provider

            skills_provider, skill_warnings = await create_skills_provider(
                profile.skills,
                runtime=runtime,
                session_dir=self._session_dir,
                project_skills_enabled=settings.project_skills_enabled,
            )
            if skills_provider is not None:
                shared_providers.append(skills_provider)
            if progressive_resume_provider is not None:
                shared_providers.append(progressive_resume_provider)
            if self._bus is not None:
                for warn in skill_warnings:
                    await self._bus.publish(Warning(code=warn.code, message=warn.message))

            # Sub-agents get their own SystemReminderMiddleware for runtime
            # context injection (no usage/switch — single-turn agents).
            shell_tool_enabled = any(get_tool_kind(t) == KIND_SHELL for t in tools)
            reminder_inputs = ReminderInputs(
                runtime=runtime,
                shell_tool_enabled=shell_tool_enabled,
                session_root=self._session_dir,
                file_read_available=any(get_tool_kind(tool) == KIND_FILESYSTEM_READ for tool in tools),
                spill_quota=self._spill_quota,
                catalog_pointer_enabled=False,
                skill_catalog_provider=skills_provider.render_catalog_reminder if skills_provider else None,
                mcp_instructions_provider=mcp_adapter.render_instructions_reminder if mcp_adapter is not None else None,
            )

            # Wire Phase 4 LAST_WORDS for the sub-agent.  Sub-agents run long
            # tool loops (e.g. Explore searching many files) that absolutely can
            # trigger Phase 4.  Without this wiring the compaction strategy's
            # `have_note` guard is always False and Phase 4 silently no-ops —
            # leaving the sub-agent to blow past the API context limit.
            #
            # NOTE: LAST_WORDS generation retries transient failures internally
            # against the same compaction input.  If that local retry budget is
            # exhausted, the controller pauses and the user decides (Retry / Abort)
            # without first replaying the whole sub-agent attempt.
            async def _publish_last_words_retry(info: RetryAttemptInfo, *, _tool_name=tool_name) -> None:
                invocation_id = invocation_routing_key.get()
                if self._bus is None or not invocation_id:
                    return
                emitter = self._emitter_for(invocation_id)
                await emitter.publish(
                    InvocationRetryAttempt(
                        scope="compaction",
                        origin=emitter.origin,
                        agent_name=self._display_names.get(_tool_name, _tool_name),
                        message=f"LAST_WORDS compaction: {info.reason}",
                        attempt=info.attempt,
                        max_attempts=info.max_attempts,
                        delay_seconds=int(info.delay_seconds),
                        session_id=self._session_id,
                    )
                )

            async def _publish_compaction_status(status: CompactionStatus, *, _tool_name=tool_name) -> None:
                invocation_id = invocation_routing_key.get()
                if self._bus is None or not invocation_id:
                    return
                await self._publish_sub_agent_compaction_status(status, _tool_name, self._emitter_for(invocation_id))

            last_words_inputs = LastWordsInputs(
                profile=sub_active_profile,
                template=profile.compaction.last_words_template,
                max_output_tokens=profile.compaction.last_words_max_output_tokens,
                log_dir=sub_log_dir,
                session_id=last_words_session_id,
                parent_session_id=self._session_id,
                session_dir=self._session_dir,
                max_transient_retries=self._max_transient_retries,
                publish_retry=_publish_last_words_retry,
                publish_status=_publish_compaction_status,
                report_usage=self._on_side_call_usage,
            )
            registered = create_runtime(
                conversation,
                SharedRuntime(
                    client=client,
                    tools=tools,
                    providers=shared_providers,
                    name=profile.display_name or profile.name,
                    instructions=profile.instructions,
                    vision=sub_active_profile.vision,
                ),
                ChildRecipe(
                    context=context_inputs, reminder=reminder_inputs, last_words=last_words_inputs, registration=True
                ),
            )
            ctx = registered.context
            agent = registered.agent
            await agent.__aenter__()
            await prepared.own_or_release(lambda: agent.__aexit__(None, None, None))

            per_agent_max = ref.max_concurrency
            description = (
                ref.tool_description
                or profile.description
                or f"Delegate task to {profile.display_name or profile.name}"
            )

            full_description = description + _execution_note(per_agent_max, self._max_total)

            self._agents[tool_name] = agent
            self._context_managers[tool_name] = ctx
            self._chat_options_by_tool[tool_name] = chat_options
            self._model_provenance_by_tool[tool_name] = model_provenance
            # Use the parent's approval config so sub-agents inherit the same
            # approval rules (e.g. shell: require) as the main agent.
            approval_config = self._parent_approval if self._parent_approval is not None else profile.approval
            self._approval_policies[tool_name] = ApprovalPolicy(approval_config, tools=tools)
            self._tool_kinds[tool_name] = {t.name: k for t in tools if (k := get_tool_kind(t))}
            self._display_names[tool_name] = profile.display_name or profile.name
            self._hook_profile_names[tool_name] = profile.name
            self._agent_active[tool_name] = 0
            self._agent_max[tool_name] = per_agent_max

            def _make_invocation_runtime(
                owner: Conversation,
                invocation_id: str,
            ) -> KernelRuntime:
                """Create per-invocation runtime state for registered sub-agents.

                ``Agent`` instances are cheap wrappers around the shared client and
                tools.  Building a fresh context manager/reminder per invocation
                keeps same-tool concurrent runs from sharing compaction calibration,
                LAST_WORDS, or history-provider state.
                """
                emitter = self._emitter_for(invocation_id)
                invocation_last_words_session_id = derive_llm_route_session_id(
                    self._session_id,
                    route_kind="last-words",
                    route_parts=(tool_name, profile.name, invocation_id),
                    model_profile=sub_active_profile,
                )

                async def _on_pre_compact(info: PreCompactInfo, *, _tool_name: str = tool_name) -> None:
                    await self._fire_pre_compact_hook(_tool_name, info, origin=emitter.origin)

                def _publish_invocation_context_pressure(
                    reason: str,
                    breaker: DropRoundBreakerState,
                    budget: int,
                ) -> Awaitable[None] | None:
                    if self._bus is None:
                        return None
                    return emitter.publish(
                        InvocationContextPressure(
                            origin=emitter.origin,
                            reason=reason,
                            attempts=breaker.attempts,
                            side_call_tokens=breaker.side_call_tokens,
                            side_call_token_budget=budget,
                            source="sub_agent",
                            session_id=self._session_id,
                        )
                    )

                context_inputs = ContextInputs(
                    profile=sub_active_profile,
                    compaction_config=profile.compaction,
                    on_pre_compact=_on_pre_compact,
                    on_context_pressure=_publish_invocation_context_pressure,
                    spill_quota=self._spill_quota,
                    spill_record_dir=sub_agent_dropped_turn_relative_path(tool_name, invocation_id, 0).parent,
                    debug_log_dir=sub_log_dir,
                    include_context_management=False,
                    parent_session_id=self._session_id,
                    session_dir=self._session_dir,
                    skip_local_history_for_service_context=(
                        sub_active_profile.provider == "openai" and sub_active_profile.api_style == API_STYLE_RESPONSES
                    ),
                    service_context_default_options=chat_options,
                )
                reminder_inputs = ReminderInputs(
                    runtime=runtime,
                    shell_tool_enabled=shell_tool_enabled,
                    session_root=self._session_dir,
                    file_read_available=any(get_tool_kind(tool) == KIND_FILESYSTEM_READ for tool in tools),
                    spill_quota=self._spill_quota,
                    catalog_pointer_enabled=False,
                    skill_catalog_provider=skills_provider.render_catalog_reminder if skills_provider else None,
                    mcp_instructions_provider=sub_mcp.render_instructions_reminder
                    if (sub_mcp := self._mcp_adapters.get(tool_name)) is not None
                    else None,
                )

                async def _publish_last_words_retry(
                    info: RetryAttemptInfo,
                    *,
                    _tool_name: str = tool_name,
                    _invocation_id: str = invocation_id,
                ) -> None:
                    if self._bus is None:
                        return
                    await emitter.publish(
                        InvocationRetryAttempt(
                            scope="compaction",
                            origin=emitter.origin,
                            agent_name=self._display_names.get(_tool_name, _tool_name),
                            message=f"LAST_WORDS compaction: {info.reason}",
                            attempt=info.attempt,
                            max_attempts=info.max_attempts,
                            delay_seconds=int(info.delay_seconds),
                            session_id=self._session_id,
                        )
                    )

                async def _publish_compaction_status(
                    status: CompactionStatus,
                    *,
                    _tool_name: str = tool_name,
                    _invocation_id: str = invocation_id,
                ) -> None:
                    if self._bus is None:
                        return
                    await self._publish_sub_agent_compaction_status(status, _tool_name, emitter)

                last_words_inputs = LastWordsInputs(
                    profile=sub_active_profile,
                    template=profile.compaction.last_words_template,
                    max_output_tokens=profile.compaction.last_words_max_output_tokens,
                    log_dir=sub_log_dir,
                    session_id=invocation_last_words_session_id,
                    parent_session_id=self._session_id,
                    session_dir=self._session_dir,
                    max_transient_retries=self._max_transient_retries,
                    publish_retry=_publish_last_words_retry,
                    publish_status=_publish_compaction_status,
                    report_usage=self._on_side_call_usage,
                )
                return create_runtime(
                    owner,
                    SharedRuntime(
                        client=client,
                        tools=tools,
                        providers=shared_providers,
                        name=profile.display_name or profile.name,
                        instructions=profile.instructions,
                        vision=sub_active_profile.vision,
                    ),
                    ChildRecipe(
                        context=context_inputs,
                        reminder=reminder_inputs,
                        last_words=last_words_inputs,
                        registration=False,
                    ),
                )

            web_tools.check_names(tools)

            fn_tool = self._make_tool(
                tool_name,
                full_description,
                prepared=prepared,
                runtime_factory=_make_invocation_runtime,
                begin_web_pass=web_tools.begin_pass,
                stream=sub_active_profile.stream,
                stream_attempt_timeout=sub_active_profile.http_read_timeout,
                skills_provider=skills_provider,
                capture_service_loop_messages=capture_service_loop_messages,
                sub_agent_session_id=sub_agent_session_id,
                profile=profile,
                active_profile=sub_active_profile,
                model_provenance=model_provenance,
            )
            self._tools.append(fn_tool)

            logger.debug("Registered sub-agent tool: %s (profile=%s, max=%d)", tool_name, profile.name, per_agent_max)

        try:
            await prepared.open(_acquire)
        except BaseException:
            await rollback_resources(prepared)
            raise
        self._prepared_by_tool[ref.tool_name or profile.name] = prepared

    async def _refresh_skill_catalog(self, provider: ChrysSkillsProvider, tool_name: str) -> None:
        """Refresh a sub-agent's runtime skills and publish non-fatal warnings."""
        try:
            skill_warnings = await provider.refresh_context()
        except Exception:
            logger.exception("Failed to refresh runtime skills for sub-agent '%s'", tool_name)
            return
        if self._bus is None:
            return
        for warn in skill_warnings:
            await self._bus.publish(Warning(code=warn.code, message=warn.message, session_id=self._session_id))

    async def register_acp(
        self,
        ref: SubAgentRef,
        profile: AgentProfile,
        runtime: SessionEnvironment,
    ) -> None:
        """Register an external ACP profile without constructing kernel machinery."""
        if profile.acp is None:
            raise ValueError("register_acp requires an ACP agent profile")
        self._runtime = runtime
        if not self._workspace_cwd:
            self._workspace_cwd = runtime.cwd
        tool_name = ref.tool_name or profile.name
        per_agent_max = ref.max_concurrency
        description = (
            ref.tool_description or profile.description or f"Delegate task to {profile.display_name or profile.name}"
        )
        description += _execution_note(per_agent_max, self._max_total)
        self._acp_profiles[tool_name] = profile
        self._display_names[tool_name] = profile.display_name or profile.name
        self._hook_profile_names[tool_name] = profile.name
        self._agent_active[tool_name] = 0
        self._agent_max[tool_name] = per_agent_max
        self._prepared_by_tool[tool_name] = PreparedAgent()
        self._tools.append(self._make_acp_tool(tool_name, description, profile, runtime))
        logger.debug("Registered ACP sub-agent tool: %s (profile=%s, max=%d)", tool_name, profile.name, per_agent_max)

    def _make_acp_tool(
        self,
        tool_name: str,
        description: str,
        profile: AgentProfile,
        runtime: SessionEnvironment,
    ) -> FunctionTool:
        from chrys.orchestration.invoker.acp_protocol import AcpPermissionBroker
        from chrys.orchestration.sub_agents.acp_policy import AcpSubAgentPolicy
        from chrys.service.acp_client import AcpConfigError

        async def _invoke_acp(prompt: str) -> str:
            from chrys.orchestration.invoker.acp_spec import resolve_acp_spec

            if self._total_active >= self._max_total:
                return tool_error(
                    "sub_agent_concurrency_limit",
                    (
                        f"total sub-agent concurrency limit reached ({self._total_active}/{self._max_total} active). "
                        "Wait for a running sub-agent to complete before starting another."
                    ),
                    retryable=True,
                    details={"active": self._total_active, "limit": self._max_total},
                )
            per_max = self._agent_max.get(tool_name, DEFAULT_SUB_AGENT_CONCURRENCY)
            per_active = self._agent_active.get(tool_name, 0)
            if per_active >= per_max:
                return tool_error(
                    "sub_agent_concurrency_limit",
                    (
                        f"concurrency limit for '{tool_name}' reached ({per_active}/{per_max} active). "
                        "Wait for a running call to complete before starting another."
                    ),
                    retryable=True,
                    details={"tool_name": tool_name, "active": per_active, "limit": per_max},
                )

            invocation_id = uuid4().hex[:12]
            origin = InvocationOrigin(
                "sub_agent", self._session_id or "", invocation_id, current_invocation_origin.get()
            )
            emitter = BoundEmitter(self._bus, origin)
            self._invocation_emitters[invocation_id] = emitter
            invocation_token = invocation_routing_key.set(invocation_id)
            parent_metadata = sub_agent_parent_result_metadata.get()
            parent_event_call_id = ""
            parent_provider_call_id = ""
            if parent_metadata is not None:
                parent_event_call_id = parent_metadata.parent_event_call_id
                parent_provider_call_id = parent_metadata.parent_provider_call_id
                parent_metadata.sub_agent_invocation_id = invocation_id
            else:
                parent_event_call_id = sub_agent_parent_call_id.get()
            hook_status = "failed"
            hook_summary = ""
            # ``boundary_only`` excludes the external ACP process's own model
            # runs and tools. Parent-recorded approvals, input waits, and
            # connection retries still appear under the child actor.
            sub_agent_trace = SubAgentTrace.open(
                invocation_id=invocation_id, trace_coverage=TraceCoverage.BOUNDARY_ONLY
            )
            sub_agent_failure_code: str | None = None
            hook_started = False
            controller: SubAgentToolShell | None = None
            policy: AcpSubAgentPolicy | None = None
            broker: AcpPermissionBroker | None = None
            log_stats = SubAgentLogStats()
            log_writer: SubAgentSessionLogWriter | None = None
            log_finalized = False
            attempts: list[dict[str, Any]] = []
            # Bounded audit-update trail instead of retaining every attempt's
            # translator: each translator holds up to ~5 MiB of assembled content
            # and thousands of call records, and manual retries are unlimited.
            # Only the newest entries survive the write-time envelope ring
            # (``normalize_acp_state``), so folding finished attempts down to
            # their ring-bounded updates loses nothing the audit would keep.
            audit_updates: list[Any] = []
            active_translator: Any = None
            last_agent_info: Any = None
            stderr_path: Path | None = None
            operation = OperationLifetime()
            conversation: Conversation | None = None
            unbind: Unbind | None = None
            controller = SubAgentToolShell(
                origin=origin,
                tool_name=tool_name,
                agent_name=self._display_names[tool_name],
                event_bus=self._bus,
                operation=operation,
                human_failure_decisions=self._human_failure_decisions,
            )

            def _audit_update_trail() -> list[Any]:
                trail = list(audit_updates)
                if active_translator is not None:
                    trail.extend(active_translator.translated_updates)
                # Same item cap as the write-time envelope ring; the byte
                # bound is applied by ``normalize_acp_state`` on write.
                return trail[-_ACP_AUDIT_TRAIL_ITEMS:]

            def _record_terminal_audit(written: bool) -> None:
                nonlocal log_finalized
                log_finalized = written
                if written and parent_metadata is not None and log_writer is not None:
                    parent_metadata.sub_agent_log_file = log_writer.log_file_name
                    parent_metadata.sub_agent_audit_complete = True
                    _upgrade_interrupted_parent_result(parent_metadata)

            try:
                # Inside the block whose ``finally`` releases them: the started
                # record awaits a write ack, and an interrupt there must not
                # leave an unfinished boundary or a counter nobody gives back.
                self._total_active += 1
                self._agent_active[tool_name] = per_active + 1

                async def _open_acp(owner: Conversation) -> Conversation:
                    return owner

                conversation = await self._prepared_by_tool[tool_name].open(_open_acp)
                unbind = conversation.bind_operation(controller)
                if sub_agent_trace is not None:
                    await sub_agent_trace.started(tool_name=tool_name, agent_profile=profile.name)
                config = profile.acp
                if config is None:
                    raise RuntimeError("An ACP sub-agent requires ACP configuration.")
                if self._session_dir is not None:
                    log_writer = SubAgentSessionLogWriter(
                        parent_session_dir=self._session_dir,
                        parent_session_id=self._session_id,
                        invocation_id=invocation_id,
                        tool_name=tool_name,
                        agent_profile=profile.name,
                        agent_display_name=self._display_names[tool_name],
                        prompt=prompt,
                        parent_provider_call_id=parent_provider_call_id,
                        parent_event_call_id=parent_event_call_id,
                        primary_cwd=self._workspace_cwd,
                        working_dirs=list(
                            self._workspace_roots or ([self._workspace_cwd] if self._workspace_cwd else [])
                        ),
                        stats=log_stats,
                        runner="acp",
                    )
                    stderr_path = _acp_stderr_path(self._session_dir, log_writer, invocation_id)
                    try:
                        initial_spec = resolve_acp_spec(
                            config, runtime, stderr_path, workspace_roots=self._workspace_roots
                        )
                    except AcpConfigError:
                        pass
                    else:
                        log_writer.set_acp_launch(initial_spec.command, list(initial_spec.args))
                    initial_log_written = await log_writer.write(
                        status="running",
                        acp_state={
                            "agent_info": None,
                            "attempts": [],
                            "stop_reason": "",
                            "translated_updates": [],
                        },
                    )
                    if initial_log_written and parent_metadata is not None:
                        parent_metadata.sub_agent_log_file = log_writer.log_file_name
                if stderr_path is None:
                    stderr_path = _acp_stderr_path(self._session_dir, log_writer, invocation_id)
                if self._bus is not None:
                    await emitter.publish(
                        InvocationStarted(
                            origin=emitter.origin,
                            agent_name=self._display_names[tool_name],
                            tool_name=tool_name,
                            parent_call_id=parent_event_call_id,
                            sub_agent_log_file=log_writer.log_file_name if log_writer is not None else "",
                            session_id=self._session_id,
                        )
                    )
                await self._fire_sub_agent_hook(
                    HookEvent.SUB_AGENT_START,
                    tool_name=tool_name,
                    invocation_id=invocation_id,
                    parent_call_id=parent_event_call_id,
                    parent_provider_call_id=parent_provider_call_id,
                    target_operation_id=sub_agent_trace.operation_id if sub_agent_trace is not None else None,
                )
                hook_started = True
                if self._bus is None:
                    raise RuntimeError("ACP sub-agents require an EventBus")
                broker = AcpPermissionBroker(
                    event_bus=self._bus,
                    session_id=self._session_id,
                    caller_name=self._display_names[tool_name],
                    mode_getter=lambda: self._approval_mode,
                    turn_context=self._turn_context,
                    workspace_roots=self._workspace_roots,
                    workspace_cwd=self._workspace_cwd,
                    approval_judge=self._approval_judge,
                    ask_user_timeout_seconds=self._ask_user_timeout_seconds,
                    allow_user_interaction=self._allow_user_interaction,
                    trajectory_context=sub_agent_trace.child_context if sub_agent_trace is not None else None,
                    trajectory_boundary_operation_id=(
                        sub_agent_trace.operation_id if sub_agent_trace is not None else None
                    ),
                )

                async def _adopt_translator(translator: Any) -> None:
                    # Fires from the controller BEFORE the attempt's transport
                    # exists: a permission decision recorded during
                    # open_session (ACP allows requests the moment session/new
                    # is announced) must already ride _audit_update_trail()
                    # when a pre-handshake failure writes the pause audit.
                    nonlocal active_translator
                    if active_translator is not None and active_translator is not translator:
                        audit_updates.extend(active_translator.translated_updates)
                        del audit_updates[:-_ACP_AUDIT_TRAIL_ITEMS]
                    active_translator = translator

                async def _record_attempt(attempt: int, handshake: Any, translator: Any) -> None:
                    nonlocal last_agent_info
                    attempts.append({"attempt": attempt, "acp_session_id": handshake.session_id})
                    del attempts[:-_ACP_AUDIT_ATTEMPT_ITEMS]
                    await _adopt_translator(translator)
                    if log_writer is not None:
                        if policy is not None and policy.backend.last_spec is not None:
                            log_writer.set_acp_launch(
                                policy.backend.last_spec.command,
                                list(policy.backend.last_spec.args),
                            )
                        last_agent_info = (
                            handshake.agent_info.model_dump(mode="json", by_alias=True)
                            if handshake.agent_info is not None
                            else None
                        )
                        await log_writer.write(
                            status="running",
                            acp_state={
                                "agent_info": last_agent_info,
                                "attempts": attempts,
                                "stop_reason": "",
                                "translated_updates": _audit_update_trail(),
                                "stderr_log_path": str(stderr_path),
                            },
                        )

                async def _record_pause() -> None:
                    if log_writer is None or not log_writer.active:
                        return
                    ctrl = policy
                    # The pause fires mid-run(): sync spend/tool counters from
                    # the live controller exactly like the terminal writes do —
                    # a crash while paused must not durably record zero
                    # counters for attempts the parent was already charged for.
                    if ctrl is not None:
                        log_stats.tool_call_count = ctrl.backend.completed_tool_calls
                        log_stats.total_tokens = ctrl.backend.latest_context_tokens
                        log_stats.total_usage_tokens = ctrl.backend.total_usage_tokens
                        log_stats.usage_unreported_attempts = ctrl.backend.usage_unreported_attempts
                        if ctrl.backend.last_spec is not None:
                            log_writer.set_acp_launch(ctrl.backend.last_spec.command, list(ctrl.backend.last_spec.args))
                    await log_writer.write(
                        status="paused",
                        failure_reason=SubAgentFailureReason.ACP_TRANSPORT.value,
                        last_error=ctrl.backend.last_error if ctrl is not None else "",
                        acp_state={
                            "agent_info": last_agent_info,
                            "attempts": attempts,
                            "stop_reason": ctrl.backend.stop_reason if ctrl is not None else "",
                            "translated_updates": _audit_update_trail(),
                            "stderr_log_path": str(stderr_path),
                            "usage_unreported_attempts": log_stats.usage_unreported_attempts,
                        },
                    )

                def _commit_parent_interruption() -> None:
                    _commit_interrupted_parent_result(parent_metadata)

                async def _finalize_cancellation() -> None:
                    nonlocal log_finalized
                    ctrl = policy
                    if ctrl is not None:
                        log_stats.tool_call_count = ctrl.backend.completed_tool_calls
                        log_stats.total_tokens = ctrl.backend.latest_context_tokens
                        log_stats.total_usage_tokens = ctrl.backend.total_usage_tokens
                        log_stats.usage_unreported_attempts = ctrl.backend.usage_unreported_attempts
                    if log_writer is None:
                        return
                    if not log_finalized:
                        terminal_written = await log_writer.write(
                            status="cancelled",
                            result="cancelled",
                            last_error="cancelled",
                            ended=True,
                            acp_state={
                                "agent_info": last_agent_info,
                                "attempts": attempts,
                                "stop_reason": ctrl.backend.stop_reason if ctrl is not None else "",
                                "translated_updates": _audit_update_trail(),
                                "stderr_log_path": str(stderr_path),
                                "usage_unreported_attempts": log_stats.usage_unreported_attempts,
                            },
                        )
                        _record_terminal_audit(terminal_written)

                if controller is None:
                    raise RuntimeError("The sub-agent invocation has no controller.")
                controller.bind_parent_interrupt_commit(_commit_parent_interruption)
                policy = AcpSubAgentPolicy(
                    shell=controller,
                    prompt=prompt,
                    spec_factory=lambda _attempt: resolve_acp_spec(
                        config, runtime, stderr_path, workspace_roots=self._workspace_roots
                    ),
                    broker=broker,
                    result_mode=config.result_mode,
                    usage_callback=self._on_sub_agent_usage,
                    attempt_callback=_record_attempt,
                    translator_callback=_adopt_translator,
                    pause_callback=_record_pause,
                    persist_dir=self._pending_dir(),
                    parent_provider_call_id=parent_provider_call_id,
                    parent_event_call_id=parent_event_call_id,
                    sub_agent_log_file=(
                        log_writer.log_file_name if log_writer is not None and log_writer.active else ""
                    ),
                    pending_record_finalizer=self.queue_pending_cleanup,
                    cancellation_finalizer=_finalize_cancellation,
                    trajectory_context=sub_agent_trace.child_context if sub_agent_trace is not None else None,
                    trajectory_boundary_operation_id=(
                        sub_agent_trace.operation_id if sub_agent_trace is not None else None
                    ),
                )
                controller.attach_policy(policy)
                self._controllers[invocation_id] = controller
                result = await controller.run()
                if parent_metadata is not None and policy.backend.transcript_final_text is not None:
                    parent_metadata.transcript_final_text = policy.backend.transcript_final_text
                log_stats.tool_call_count = policy.backend.completed_tool_calls
                log_stats.total_tokens = policy.backend.latest_context_tokens
                log_stats.total_usage_tokens = policy.backend.total_usage_tokens
                log_stats.usage_unreported_attempts = policy.backend.usage_unreported_attempts
                hook_status = "failed" if result.startswith("Error:") else "ok"
                hook_summary = result[:500]
                if hook_status == "failed":
                    sub_agent_failure_code = "tool_error"
                if log_writer is not None:
                    if policy.backend.last_spec is not None:
                        log_writer.set_acp_launch(
                            policy.backend.last_spec.command,
                            list(policy.backend.last_spec.args),
                        )
                    terminal_written = await log_writer.write(
                        status="completed" if hook_status == "ok" else "failed",
                        result=result,
                        ended=True,
                        acp_state={
                            "agent_info": last_agent_info,
                            "attempts": attempts,
                            "successful_attempt": policy.backend.transport_ordinal if hook_status == "ok" else None,
                            "stop_reason": policy.backend.stop_reason,
                            "translated_updates": _audit_update_trail(),
                            "stderr_log_path": str(stderr_path),
                            "usage_unreported_attempts": log_stats.usage_unreported_attempts,
                        },
                    )
                    _record_terminal_audit(terminal_written)
                return result
            except asyncio.CancelledError as exc:
                hook_status = "cancelled"
                hook_summary = "cancelled"
                _commit_interrupted_parent_result(parent_metadata)
                if policy is not None:
                    if controller is None:
                        raise RuntimeError("The sub-agent invocation has no controller.") from exc
                    await _finish_cancellation_cleanup(controller.finalize_cancellation())
                raise
            except BaseException as exc:
                hook_status = "failed"
                hook_summary = str(exc)[:500]
                sub_agent_failure_code = "setup_failed" if policy is None else "exception"
                raise
            finally:
                operation.begin_cleanup()
                cleanup_cancelled = False
                cleanup_error: Exception | None = None
                _close_sub_agent_trace(
                    sub_agent_trace,
                    hook_status=hook_status,
                    failure_code=sub_agent_failure_code,
                    controller=controller if policy is not None else None,
                )
                if broker is not None:
                    try:
                        if policy is not None:
                            await policy.backend.aclose()
                        else:
                            await broker.close()
                    except asyncio.CancelledError:
                        cleanup_cancelled = True
                    except Exception as exc:
                        cleanup_error = exc
                if policy is not None:
                    log_stats.tool_call_count = policy.backend.completed_tool_calls
                    log_stats.total_tokens = policy.backend.latest_context_tokens
                    log_stats.total_usage_tokens = policy.backend.total_usage_tokens
                    log_stats.usage_unreported_attempts = policy.backend.usage_unreported_attempts
                if log_writer is not None and not log_finalized:
                    try:
                        if policy is not None and policy.backend.last_spec is not None:
                            log_writer.set_acp_launch(
                                policy.backend.last_spec.command,
                                list(policy.backend.last_spec.args),
                            )
                        terminal_written = await log_writer.write(
                            status="cancelled" if hook_status == "cancelled" else "failed",
                            result=hook_summary,
                            ended=True,
                            acp_state={
                                "agent_info": last_agent_info,
                                "attempts": attempts,
                                "stop_reason": policy.backend.stop_reason if policy is not None else "",
                                "translated_updates": _audit_update_trail(),
                                "stderr_log_path": str(stderr_path) if stderr_path is not None else "",
                                "usage_unreported_attempts": log_stats.usage_unreported_attempts,
                            },
                        )
                        _record_terminal_audit(terminal_written)
                    except asyncio.CancelledError:
                        cleanup_cancelled = True
                    except Exception as exc:
                        cleanup_error = cleanup_error or exc
                if self._drain_parent_usage_publishes is not None:
                    try:
                        await self._drain_parent_usage_publishes()
                    except asyncio.CancelledError:
                        cleanup_cancelled = True
                    except Exception as exc:
                        cleanup_error = cleanup_error or exc
                if hook_started:
                    try:
                        await self._fire_sub_agent_hook(
                            HookEvent.SUB_AGENT_END,
                            tool_name=tool_name,
                            invocation_id=invocation_id,
                            parent_call_id=parent_event_call_id,
                            parent_provider_call_id=parent_provider_call_id,
                            status=hook_status,
                            result_summary=hook_summary,
                            target_operation_id=(sub_agent_trace.operation_id if sub_agent_trace is not None else None),
                        )
                    except asyncio.CancelledError:
                        cleanup_cancelled = True
                    except Exception:
                        logger.exception("Failed to fire ACP sub-agent end hook")
                self._controllers.pop(invocation_id, None)
                self._invocation_emitters.pop(invocation_id, None)
                self._total_active -= 1
                self._agent_active[tool_name] -= 1
                invocation_routing_key.reset(invocation_token)
                if unbind is not None:
                    unbind()
                operation.finish()
                if conversation is not None:
                    try:
                        await conversation.aclose()
                    except asyncio.CancelledError:
                        cleanup_cancelled = True
                if cleanup_cancelled:
                    raise asyncio.CancelledError
                if cleanup_error is not None:
                    raise cleanup_error

        tool = FunctionTool(func=_invoke_acp, name=tool_name, description=description)
        set_tool_kind(tool, KIND_SUB_AGENT)
        return tool

    def _make_tool(
        self,
        tool_name: str,
        description: str,
        *,
        prepared: PreparedAgent,
        runtime_factory: Callable[[Conversation, str], KernelRuntime],
        stream: bool,
        stream_attempt_timeout: float,
        skills_provider: ChrysSkillsProvider | None = None,
        capture_service_loop_messages: bool = False,
        profile: AgentProfile | None = None,
        active_profile: ModelProfile | None = None,
        sub_agent_session_id: str | None = None,
        model_provenance: SubAgentModelProvenance | None = None,
        begin_web_pass: Callable[[], None] | None = None,
    ) -> FunctionTool:
        """Create a FunctionTool that invokes the sub-agent with concurrency control."""

        def _route_profile_name() -> str:
            return profile.name if profile is not None else self._hook_profile_names.get(tool_name, tool_name)

        def _derive_invocation_session_id(invocation_id: str) -> str | None:
            if active_profile is None:
                return sub_agent_session_id
            return derive_llm_route_session_id(
                self._session_id,
                route_kind="sub-agent",
                route_parts=(tool_name, _route_profile_name(), invocation_id),
                model_profile=active_profile,
            )

        async def _invoke(prompt: str) -> str:
            # Global concurrency guard
            if self._total_active >= self._max_total:
                return tool_error(
                    "sub_agent_concurrency_limit",
                    (
                        f"total sub-agent concurrency limit reached ({self._total_active}/{self._max_total} active). "
                        "Wait for a running sub-agent to complete before starting another."
                    ),
                    retryable=True,
                    details={"active": self._total_active, "limit": self._max_total},
                )

            # Per-agent concurrency guard
            per_max = self._agent_max.get(tool_name, DEFAULT_SUB_AGENT_CONCURRENCY)
            per_active = self._agent_active.get(tool_name, 0)
            if per_active >= per_max:
                return tool_error(
                    "sub_agent_concurrency_limit",
                    (
                        f"concurrency limit for '{tool_name}' reached ({per_active}/{per_max} active). "
                        "Wait for a running call to complete before starting another."
                    ),
                    retryable=True,
                    details={"tool_name": tool_name, "active": per_active, "limit": per_max},
                )

            invocation_id = ""
            invocation_token: Token[str] | None = None
            route_session_token: Token[str] | None = None
            parent_session_token: Token[str] | None = None
            total_counter_incremented = False
            agent_counter_incremented = False
            parent_event_call_id = ""
            parent_provider_call_id = ""
            parent_call_id = ""
            hook_status = "failed"
            hook_result_summary = ""
            sub_agent_hook_started = False
            approval_mw: ApprovalMiddleware | None = None
            controller: SubAgentToolShell | None = None
            policy: KernelSubAgentPolicy | None = None
            sub_event_mw: SubAgentEventMiddleware | None = None
            sleep_mw: SleepMiddleware | None = None
            log_stats = SubAgentLogStats()
            log_writer: SubAgentSessionLogWriter | None = None
            parent_result_metadata = None
            sub_agent_trace: SubAgentTrace | None = None
            sub_agent_failure_code: str | None = None
            conversation: Conversation | None = None
            operation = OperationLifetime()
            unbind: Unbind | None = None
            background_tasks = self._background_tasks
            stopped: list[BackgroundTask] = []
            try:
                self._total_active += 1
                total_counter_incremented = True
                self._agent_active[tool_name] = per_active + 1
                agent_counter_incremented = True
                invocation_id = uuid4().hex[:12]
                origin = InvocationOrigin(
                    "sub_agent", self._session_id or "", invocation_id, current_invocation_origin.get()
                )
                emitter = BoundEmitter(self._bus, origin)
                self._invocation_emitters[invocation_id] = emitter
                invocation_session_id = _derive_invocation_session_id(invocation_id)
                invocation_token = invocation_routing_key.set(invocation_id)
                # In-process: the child's events inline into this writer under
                # the child's actor, hung off this boundary.
                sub_agent_trace = SubAgentTrace.open(invocation_id=invocation_id, trace_coverage=TraceCoverage.FULL)

                async def _open_operation(owner: Conversation) -> Conversation:
                    return owner

                controller = SubAgentToolShell(
                    origin=origin,
                    tool_name=tool_name,
                    agent_name=self._display_names.get(tool_name, tool_name),
                    event_bus=self._bus,
                    operation=operation,
                    human_failure_decisions=self._human_failure_decisions,
                )
                conversation = await prepared.open(_open_operation)
                unbind = conversation.bind_operation(controller)
                if sub_agent_trace is not None:
                    await sub_agent_trace.started(
                        tool_name=tool_name, agent_profile=self._hook_profile_names.get(tool_name, tool_name)
                    )
                route_session_token = (
                    llm_route_session_id.set(invocation_session_id) if invocation_session_id is not None else None
                )
                parent_session_token = llm_parent_session_id.set(self._session_id or "")
                parent_result_metadata = sub_agent_parent_result_metadata.get()
                if parent_result_metadata is not None:
                    parent_event_call_id = parent_result_metadata.parent_event_call_id
                    parent_provider_call_id = parent_result_metadata.parent_provider_call_id
                    parent_result_metadata.sub_agent_invocation_id = invocation_id
                else:
                    parent_event_call_id = sub_agent_parent_call_id.get()
                parent_call_id = parent_event_call_id
                if self._session_dir is not None:
                    log_writer = SubAgentSessionLogWriter(
                        parent_session_dir=self._session_dir,
                        parent_session_id=self._session_id,
                        invocation_id=invocation_id,
                        tool_name=tool_name,
                        agent_profile=self._hook_profile_names.get(tool_name, tool_name),
                        agent_display_name=self._display_names.get(tool_name, tool_name),
                        prompt=prompt,
                        parent_provider_call_id=parent_provider_call_id,
                        parent_event_call_id=parent_event_call_id,
                        model=model_provenance,
                        primary_cwd=self._workspace_cwd,
                        working_dirs=list(
                            self._workspace_roots or ([self._workspace_cwd] if self._workspace_cwd else [])
                        ),
                        stats=log_stats,
                    )
                if log_writer is not None:
                    initial_log_written = await log_writer.write(status="running")
                    if initial_log_written and parent_result_metadata is not None:
                        parent_result_metadata.sub_agent_log_file = log_writer.log_file_name
                # Sub-agents are single-turn and do not carry parent usage
                # or profile-switch context; this snapshots runtime only.
                if skills_provider is not None:
                    await self._refresh_skill_catalog(skills_provider, tool_name)

                invocation_runtime = runtime_factory(conversation, invocation_id)
                invocation_agent = invocation_runtime.agent
                invocation_ctx = invocation_runtime.context
                invocation_reminder = invocation_runtime.reminder
                invocation_reminder.prepare_turn()
                # Publish the invocation-start event BEFORE any LLM call so
                # the TUI links this invocation_id to the parent tool call's
                # widget deterministically — even if the first LLM call
                # fails immediately (e.g. 529 overloaded) and no inner tool
                # call arrives for a while.  See
                # :class:`InvocationStarted` docstring.
                if self._bus is not None:
                    await emitter.publish(
                        InvocationStarted(
                            origin=emitter.origin,
                            agent_name=self._display_names.get(tool_name, tool_name),
                            tool_name=tool_name,
                            parent_call_id=parent_call_id,
                            sub_agent_log_file=log_writer.log_file_name if log_writer is not None else "",
                            session_id=self._session_id,
                        )
                    )
                await self._fire_sub_agent_hook(
                    HookEvent.SUB_AGENT_START,
                    tool_name=tool_name,
                    invocation_id=invocation_id,
                    parent_call_id=parent_call_id,
                    parent_provider_call_id=parent_provider_call_id,
                    target_operation_id=sub_agent_trace.operation_id if sub_agent_trace is not None else None,
                )
                sub_agent_hook_started = True

                middleware = []
                loop_recorder = LoopRecorder(
                    capture_service_loop_messages=capture_service_loop_messages,
                    message_hasher=serialized_message_payload,
                )
                if self._bus is not None:
                    sub_event_mw = SubAgentEventMiddleware(
                        self._bus,
                        self._display_names.get(tool_name, tool_name),
                        invocation_id,
                        origin=origin,
                        mutation_tracker=self._mutation_tracker,
                        mutation_coordinator=self._mutation_coordinator,
                        hook_manager=self._hook_manager,
                        profile_name=self._hook_profile_names.get(tool_name, tool_name),
                        session_id=self._session_id,
                        workspace_cwd=self._workspace_cwd,
                        serialize_implicit_windows=self._serialize_implicit_windows,
                        stats=log_stats,
                        tool_result_ceiling_tokens=self._tool_result_ceiling_tokens,
                    )
                    middleware.append(sub_event_mw)
                    # AskUser middleware — sub-agent ask_user calls show the
                    # same dialog as the main agent via the shared EventBus.
                    middleware.append(
                        create_ask_user(
                            conversation,
                            AskUserInputs(
                                event_bus=self._bus,
                                session_id=self._session_id,
                                caller_name=self._display_names.get(tool_name, tool_name),
                                timeout_seconds=self._ask_user_timeout_seconds,
                            ),
                        )
                    )
                    # Approval middleware — gates sub-agent tool calls with
                    # user approval via the same EventBus flow as the main agent.
                    # Reads current approval_mode/judge off self so mid-session
                    # mode switches (MANUAL ↔ AUTO ↔ BYPASS) take effect on the
                    # next sub-agent invocation.
                    approval_policy = self._approval_policies.get(tool_name)
                    if approval_policy is not None:
                        approval_mw = await create_approval(
                            conversation,
                            ApprovalInputs(
                                approval_policy=approval_policy,
                                event_bus=self._bus,
                                session_id=self._session_id,
                                tool_kinds=self._tool_kinds.get(tool_name),
                                caller_name=self._display_names.get(tool_name, tool_name),
                                workspace_roots=self._workspace_roots,
                                approval_mode=self._approval_mode,
                                approval_judge=self._approval_judge,
                                approval_log_dir=self._approval_log_dir,
                                workspace_cwd=self._workspace_cwd,
                                # Developer-mode handoff review is scoped to main-agent → sub-agent delegation.
                                dev_mode=False,
                                hook_manager=self._hook_manager,
                                profile_name=self._hook_profile_names.get(tool_name, tool_name),
                                session_archive_read_roots=(
                                    [self._session_dir / COMPACTIONS_DIR_NAME] if self._session_dir else None
                                ),
                                turn_context=self._turn_context,
                            ),
                        )
                        approval_mw.bind_publisher(emitter)
                        middleware.append(approval_mw)
                        self._live_approvals.append(approval_mw)
                    sleep_mw = SleepMiddleware(
                        self._bus, session_id=self._session_id, background_tasks=self._background_tasks
                    )
                    middleware.append(sleep_mw)
                    # Innermost: runs the version the event middleware's hooks checked, or nothing.
                    middleware.append(HookCommitMiddleware())
                if sub_event_mw is None:
                    middleware.append(SubAgentStatsMiddleware(log_stats))
                # Wire usage callback so token counts flow to TUI via
                # InvocationProgress.  Per-invocation UsageTrackingMiddleware
                # — adding it to run-scoped middleware gives each concurrent
                # invocation its own callback state, so parallel sub-agent
                # runs no longer clobber each other's totals through the
                # registered tool's shared ``ctx.usage_middleware``.  For
                # registered sub-agents, ``ctx`` is also fresh per invocation,
                # so calibration/overhead/LAST_WORDS state cannot leak between
                # same-tool concurrent runs.
                ctx = invocation_ctx
                # Run-scoped middleware is concatenated AFTER the agent's own
                # chat middleware, so the last listed sits innermost: the
                # validation middleware judges each landed response, and the
                # usage middleware under it counts every one of them first.
                validation_middleware = create_validation(conversation)
                middleware.append(validation_middleware)
                if ctx:
                    parent_cb = self._on_sub_agent_usage
                    total_usage_tokens = 0
                    max_context_tokens = ctx.usage_middleware.max_context_tokens
                    agent_profile_name = self._hook_profile_names.get(tool_name, tool_name)

                    # Positional args mirror ``UsageTrackingMiddleware._fire_callback``;
                    # if that callback grows a new arg, add it here too or the
                    # parent accumulator won't see it (``*_rest`` will swallow it).
                    def _on_usage(
                        total,
                        inp=0,
                        out=0,
                        local=0,
                        ratio=1.0,
                        overhead=0,
                        cache_hit=None,
                        calibration_initialized=False,
                        use_local_context_estimate=False,
                        *_rest,
                        _mw=sub_event_mw,
                        _cb=parent_cb,
                        _max_context_tokens=max_context_tokens,
                        _agent_profile=agent_profile_name,
                        _usage_source_id=invocation_id,
                    ):
                        # ``normalize_stream_usage`` collapses provider deltas
                        # into a single per-call snapshot; ``total`` is the
                        # per-call cost, so we
                        # can accumulate it directly.  An ``(inp + out) or total``
                        # fallback would double-count on any future provider
                        # that reports ``total`` without an input/output split.
                        nonlocal total_usage_tokens
                        total_usage_tokens += total
                        log_stats.record_usage(total, total_usage_tokens)
                        if _mw is not None:
                            _mw.record_usage(total, total_usage_tokens)
                        if _cb is not None:
                            _cb(
                                total,
                                inp,
                                out,
                                local,
                                ratio,
                                overhead,
                                cache_hit,
                                _max_context_tokens,
                                _agent_profile,
                                _usage_source_id,
                                None,
                                use_local_context_estimate,
                            )

                    per_invocation_usage = UsageTrackingMiddleware(
                        max_context_tokens=max_context_tokens,
                        warn_threshold_pct=ctx.usage_middleware.warn_threshold_pct,
                        on_usage=_on_usage,
                        compaction_strategy=ctx.compaction_strategy,
                        use_local_context_estimate_for_hosted_usage=(ctx.use_local_context_estimate_for_hosted_usage),
                    )
                    middleware.append(per_invocation_usage)

                run_kwargs: AgentRunKwargs = {}
                sub_session = invocation_agent.create_session()
                run_kwargs["session"] = sub_session
                # Per-invocation channel for the chrys ToolLoopLayer — parallel
                # sub-agents share one client stack, so the recorder must ride
                # the run, never the client.
                run_client_kwargs: dict[str, Any] = {"loop_recorder": loop_recorder}
                if sub_agent_trace is not None:
                    run_client_kwargs[TRAJECTORY_CONTEXT_KWARG] = sub_agent_trace.child_context
                run_kwargs["client_kwargs"] = run_client_kwargs
                if middleware:
                    run_kwargs["middleware"] = middleware
                invocation_chat_options = self._chat_options_by_tool.get(tool_name)
                if invocation_chat_options:
                    options_copy = dict(invocation_chat_options)
                    if isinstance(extra_body := options_copy.get("extra_body"), dict):
                        options_copy["extra_body"] = dict(extra_body)
                    run_kwargs["options"] = options_copy
                if ctx is not None:
                    run_kwargs["compaction_strategy"] = ctx.compaction_strategy
                    # Same wiring as TurnBindings._build_run_kwargs: the raw client
                    # estimates live tool definitions with the strategy's own
                    # tokenizer.
                    run_kwargs["tokenizer"] = ctx.compaction_strategy.tokenizer

                # Controller owns the run → retry → pause → decide loop.
                # Registered before run() so an early cascade_abort (e.g.
                # global interrupt fired during concurrent startup) can
                # reach it even before it enters RUNNING.
                def _commit_parent_interruption() -> None:
                    _commit_interrupted_parent_result(parent_result_metadata)

                def _record_terminal_audit() -> None:
                    if parent_result_metadata is not None and log_writer is not None:
                        parent_result_metadata.sub_agent_log_file = log_writer.log_file_name
                        parent_result_metadata.sub_agent_audit_complete = True
                        _upgrade_interrupted_parent_result(parent_result_metadata)

                def _begin_hosted_pass() -> None:
                    if sub_event_mw is None:
                        validation_middleware.set_observation_hook(None)
                        return
                    validation_middleware.set_observation_hook(sub_event_mw.begin_hosted_pass())

                retry_kwargs: dict[str, Any] = {}
                if self._max_transient_retries is not None:
                    retry_kwargs["max_retries"] = self._max_transient_retries
                if controller is None:
                    raise RuntimeError("The sub-agent invocation has no controller.")
                controller.bind_parent_interrupt_commit(_commit_parent_interruption)
                policy = KernelSubAgentPolicy(
                    shell=controller,
                    conversation=invocation_runtime.owner,
                    agent=invocation_agent,
                    session=sub_session,
                    loop_recorder=loop_recorder,
                    prompt=prompt,
                    run_kwargs=run_kwargs,
                    # Persist the parent assistant function_call's
                    # call_id so session-reload recovery can re-pair a
                    # paused record to its dangling function_call by id
                    # instead of by tool_name + appearance order.
                    parent_call_id=parent_provider_call_id,
                    parent_event_call_id=parent_event_call_id,
                    sub_agent_log_file=log_writer.log_file_name if log_writer is not None and log_writer.active else "",
                    persist_dir=self._pending_dir(),
                    log_writer=log_writer,
                    log_stats=log_stats,
                    terminal_audit_callback=_record_terminal_audit,
                    pending_record_finalizer=self.queue_pending_cleanup,
                    tool_event_middleware=sub_event_mw,
                    stream=stream,
                    stream_attempt_timeout=stream_attempt_timeout,
                    shows_answer_text=self._shows_answer_text,
                    sleep_middleware=sleep_mw,
                    pass_start_hooks=(
                        *((begin_web_pass,) if begin_web_pass is not None else ()),
                        _begin_hosted_pass,
                    ),
                    hosted_commits_probe=validation_middleware.hosted_commits_observed,
                    begin_hosted_baseline=validation_middleware.begin_pass_hosted_baseline,
                    trajectory_context=sub_agent_trace.child_context if sub_agent_trace is not None else None,
                    trajectory_boundary_operation_id=(
                        sub_agent_trace.operation_id if sub_agent_trace is not None else None
                    ),
                    **retry_kwargs,
                )
                controller.attach_policy(policy)
                if not service_storage_side(invocation_agent.client, run_kwargs.get("options")):
                    # Look up callbacks at retry time, not while constructing
                    # the adapter.
                    def _wire_sleep(seconds: int) -> Awaitable[bool]:
                        return policy.sleep_for_wire_retry(seconds)

                    def _wire_notice(
                        message: str, attempt: int, maximum: int, delay: int, exc: BaseException
                    ) -> Awaitable[None]:
                        return policy.publish_wire_retry_attempt(message, attempt, maximum, delay, exc)

                    def _answer_shown() -> bool:
                        return policy.wire_answer_shown()

                    run_client_kwargs["wire_retry_policy"] = WireRecipe(
                        max_retries=lambda: policy.max_retries,
                        stall_timeout_seconds=stream_attempt_timeout,
                        stall_max_retries=lambda: policy.max_retries,
                        stall_exhausted_action=StallExhaustedAction.RAISE,
                        backoff_schedule=lambda: policy.backoff_schedule,
                        interrupted=lambda: controller.cascade_requested,
                        interruptible_sleep=_wire_sleep,
                        publish_retry=_wire_notice,
                        prepare_retry=None,
                        hosted_commits_in_flight=validation_middleware.hosted_commits_in_flight,
                        answer_shown=_answer_shown,
                    ).build()
                run_client_kwargs["continuation_token_observer"] = policy.observe_continuation_token
                run_client_kwargs[RESPONSE_READY_KWARG] = policy.response_ready
                self._controllers[invocation_id] = controller
                try:
                    with (
                        trajectory_scope(sub_agent_trace.child_context)
                        if sub_agent_trace is not None
                        else contextlib.nullcontext()
                    ):
                        result = await controller.run()
                finally:
                    # Its background commands end with it, however it ends; the
                    # cleanup below waits for them.
                    if background_tasks is not None:
                        stopped = background_tasks.stop_scope(TaskScope.of(origin))
                if stopped:
                    commands = "; ".join(
                        task.label
                        if task.shell_exit_code is None
                        else f"{task.label}, whose shell had exited with code {task.shell_exit_code}"
                        for task in stopped
                    )
                    result += (
                        f"\n\n[Background commands this sub-agent started were stopped when it finished: {commands}]"
                    )
                segment = policy.final_segment
                if parent_result_metadata is not None and segment is not None and segment.transcript != segment.result:
                    parent_result_metadata.transcript_final_text = segment.transcript
                hook_status = "failed" if result.startswith("Error:") else "ok"
                hook_result_summary = result[:500]
                if hook_status == "failed":
                    sub_agent_failure_code = "tool_error"
                return result
            except asyncio.CancelledError as exc:
                hook_status = "cancelled"
                hook_result_summary = "cancelled"
                _commit_interrupted_parent_result(parent_result_metadata)
                if policy is not None:
                    if controller is None:
                        raise RuntimeError("The sub-agent invocation has no controller.") from exc
                    await _finish_cancellation_cleanup(controller.finalize_cancellation())
                elif log_writer is not None:
                    with contextlib.suppress(Exception):
                        terminal_written = await log_writer.write(
                            status="cancelled", result="cancelled", last_error="cancelled", ended=True
                        )
                        if terminal_written and parent_result_metadata is not None:
                            parent_result_metadata.sub_agent_log_file = log_writer.log_file_name
                            parent_result_metadata.sub_agent_audit_complete = True
                            _upgrade_interrupted_parent_result(parent_result_metadata)
                raise
            except BaseException as exc:
                hook_status = "failed"
                hook_result_summary = str(exc)[:500]
                sub_agent_failure_code = "setup_failed" if policy is None else "exception"
                if policy is None and log_writer is not None:
                    with contextlib.suppress(Exception):
                        terminal_written = await log_writer.write(
                            status="setup_failed",
                            result=hook_result_summary,
                            failure_reason="setup_failed",
                            last_error=hook_result_summary,
                            ended=True,
                        )
                        if terminal_written and parent_result_metadata is not None:
                            parent_result_metadata.sub_agent_log_file = log_writer.log_file_name
                            parent_result_metadata.sub_agent_audit_complete = True
                            _upgrade_interrupted_parent_result(parent_result_metadata)
                raise
            finally:
                operation.begin_cleanup()
                cleanup_cancelled = False
                _close_sub_agent_trace(
                    sub_agent_trace,
                    hook_status=hook_status,
                    failure_code=sub_agent_failure_code,
                    controller=controller if policy is not None else None,
                    failure_reason=policy.failure_reason if policy is not None else None,
                )
                if sub_event_mw is not None:
                    try:
                        await sub_event_mw.flush_progress()
                    except asyncio.CancelledError:
                        cleanup_cancelled = True
                    except Exception:
                        logger.debug("sub-agent progress flush failed for invocation %s", invocation_id, exc_info=True)
                if self._drain_parent_usage_publishes is not None:
                    try:
                        await self._drain_parent_usage_publishes()
                    except asyncio.CancelledError:
                        cleanup_cancelled = True
                    except Exception:
                        logger.debug(
                            "parent usage publish drain failed for invocation %s", invocation_id, exc_info=True
                        )
                if sub_agent_hook_started:
                    try:
                        await self._fire_sub_agent_hook(
                            HookEvent.SUB_AGENT_END,
                            tool_name=tool_name,
                            invocation_id=invocation_id,
                            parent_call_id=parent_call_id,
                            parent_provider_call_id=parent_provider_call_id,
                            status=hook_status,
                            result_summary=hook_result_summary,
                            target_operation_id=(sub_agent_trace.operation_id if sub_agent_trace is not None else None),
                        )
                    except asyncio.CancelledError:
                        cleanup_cancelled = True
                        logger.debug("sub-agent end hook cancelled for invocation %s", invocation_id)
                    except Exception:
                        logger.warning("sub-agent end hook failed for invocation %s", invocation_id, exc_info=True)
                if invocation_id:
                    self._controllers.pop(invocation_id, None)
                    self._invocation_emitters.pop(invocation_id, None)
                if stopped and background_tasks is not None:
                    # Ended before the turn goes on, which an interrupt (cancelling
                    # more than once) and then a rollback can follow. Only once the
                    # controller is gone: an interrupt meanwhile must not mark the
                    # finished run aborted.
                    try:
                        await finish_close(asyncio.create_task(background_tasks.wait_stopped(stopped)))
                    except asyncio.CancelledError:
                        # The interrupted result must still point at the finished run's record.
                        _commit_interrupted_parent_result(parent_result_metadata)
                        cleanup_cancelled = True
                if total_counter_incremented:
                    self._total_active -= 1
                if agent_counter_incremented:
                    # ``register()`` seeds ``_agent_active[tool_name] = 0`` and
                    # the enclosing ``try`` already bumped it by 1, so direct
                    # subscript access is correct here.  A KeyError would be a
                    # genuine bug (tool invoked without registration) — surface
                    # it rather than silently masking.
                    try:
                        self._agent_active[tool_name] -= 1
                    except KeyError:
                        logger.error(
                            "sub-agent counter underflow: tool %r not in _agent_active (invocation %s)",
                            tool_name,
                            invocation_id,
                        )
                if route_session_token is not None:
                    llm_route_session_id.reset(route_session_token)
                if parent_session_token is not None:
                    llm_parent_session_id.reset(parent_session_token)
                if invocation_token is not None:
                    invocation_routing_key.reset(invocation_token)
                if unbind is not None:
                    unbind()
                if approval_mw is not None:
                    try:
                        if conversation is None:
                            raise RuntimeError("Closing sub-agent approvals requires their owning conversation.")
                        await conversation.release(approval_mw.close)
                    except asyncio.CancelledError:
                        cleanup_cancelled = True
                    except Exception:
                        logger.debug("approval middleware close failed for invocation %s", invocation_id, exc_info=True)
                    with contextlib.suppress(ValueError):
                        self._live_approvals.remove(approval_mw)
                operation.finish()
                if conversation is not None:
                    try:
                        await conversation.aclose()
                    except asyncio.CancelledError:
                        cleanup_cancelled = True
                if cleanup_cancelled:
                    raise asyncio.CancelledError

        sub_agent_tool = FunctionTool(
            func=_invoke,
            name=tool_name,
            description=description,
        )
        set_tool_kind(sub_agent_tool, KIND_SUB_AGENT)
        return sub_agent_tool

    async def _fire_sub_agent_hook(
        self,
        event: HookEvent,
        *,
        tool_name: str,
        invocation_id: str,
        parent_call_id: str,
        parent_provider_call_id: str = "",
        status: str = "",
        result_summary: str = "",
        target_operation_id: str | None = None,
    ) -> None:
        if self._hook_manager is None or not self._hook_manager.has_hooks_for(event):
            return
        payload: dict[str, Any] = {
            "session_id": self._session_id,
            "profile": self._hook_profile_names.get(tool_name, tool_name),
            "cwd": self._workspace_cwd,
            "sub_agent": {
                "name": self._display_names.get(tool_name, tool_name),
                "tool_name": tool_name,
                "invocation_id": invocation_id,
                "parent_call_id": parent_call_id,
                "parent_provider_call_id": parent_provider_call_id,
            },
        }
        if event == HookEvent.SUB_AGENT_END:
            payload["status"] = status
            payload["result_summary"] = result_summary
        await self._hook_manager.fire(event, payload, target_operation_id=target_operation_id)

    async def _fire_pre_compact_hook(
        self, tool_name: str, info: PreCompactInfo, *, origin: InvocationOrigin | None
    ) -> None:
        if self._hook_manager is None:
            return
        if not self._hook_manager.has_hooks_for(HookEvent.PRE_COMPACT):
            return
        await self._hook_manager.fire(
            HookEvent.PRE_COMPACT,
            {
                "session_id": self._session_id,
                "profile": self._hook_profile_names.get(tool_name, tool_name),
                "cwd": self._workspace_cwd,
                "trigger": info.trigger,
                "usage_pct": info.usage_pct,
                "tokens_before": info.tokens_before,
                "sub_agent": {
                    "name": self._display_names.get(tool_name, tool_name),
                    "tool_name": tool_name,
                },
            },
            target_operation_id=info.trajectory_operation_id,
            origin=origin,
        )

    async def _publish_sub_agent_compaction_status(
        self,
        status: CompactionStatus,
        tool_name: str,
        emitter: BoundEmitter,
    ) -> None:
        """Publish a Phase-4 compaction UX signal scoped to one invocation.

        Callers guard bus/invocation-id availability; this only translates
        the generator's :class:`CompactionStatus` into the sub-agent
        started/finished bus events (note text deliberately not forwarded —
        sub-agent cards only flip a status line).
        """
        bus = self._bus
        if bus is None:
            return
        agent_name = self._display_names.get(tool_name, tool_name)
        if status.stage == "started":
            await emitter.publish(
                InvocationCompactionStarted(
                    origin=emitter.origin,
                    agent_name=agent_name,
                    compaction_id=status.compaction_id,
                    session_id=self._session_id,
                )
            )
            return
        if status.stage == "committed":
            # Post-commit confirmation — a distinct event, NOT a second
            # finished signal: the card's compaction line already flipped on
            # finished-ok, this only drives the "Compactions: N" counter.
            await emitter.publish(
                InvocationCompactionCommitted(
                    origin=emitter.origin,
                    agent_name=agent_name,
                    compaction_id=status.compaction_id,
                    session_id=self._session_id,
                )
            )
            return
        await emitter.publish(
            InvocationCompactionFinished(
                origin=emitter.origin,
                agent_name=agent_name,
                compaction_id=status.compaction_id,
                outcome=status.outcome,
                duration_ms=status.duration_ms,
                format_violation=status.format_violation,
                failure_reason=status.failure_reason,
                session_id=self._session_id,
            )
        )

    def get_tools(self) -> list[FunctionTool]:
        """Return all registered sub-agent tools."""
        return list(self._tools)

    def tool_names(self) -> list[str]:
        """Return the tool names of all registered sub-agents."""
        return [tool.name for tool in self._tools]

    # -- Per-invocation dispatch (used by engine event handlers) ------

    def _pending_dir(self) -> Path | None:
        if not self._session_dir:
            return None
        return pending_dir(self._session_dir)

    def request_retry(self, invocation_id: str) -> bool:
        """Deliver a user-triggered retry to the controller for ``invocation_id``.

        Returns ``True`` if the controller existed and was paused;
        ``False`` if there's no such controller or it wasn't paused
        (e.g. finished or cascade-aborted between the click and delivery).
        """
        controller = self._controllers.get(invocation_id)
        if controller is None:
            logger.info("Sub-agent retry: no controller for invocation_id=%s", invocation_id)
            return False
        return controller.request_retry()

    def request_abort(self, invocation_id: str) -> bool:
        """Deliver a user-triggered abort to the controller for ``invocation_id``."""
        controller = self._controllers.get(invocation_id)
        if controller is None:
            logger.info("Sub-agent abort: no controller for invocation_id=%s", invocation_id)
            return False
        return controller.request_abort()

    def queue_pending_cleanup(self, path: Path | None) -> None:
        """Queue a pending control record for post-save cleanup."""
        if path is not None:
            self._pending_cleanup_paths.add(path)

    def finalize_pending_cleanups(self) -> None:
        """Delete queued pending records after a successful parent session save."""
        remaining: set[Path] = set()
        for path in self._pending_cleanup_paths:
            if secure_unlink_owner_verified(path):
                continue
            try:
                path.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                pass
            logger.warning("Failed to delete finalized sub-agent pending record %s", path)
            remaining.add(path)
        self._pending_cleanup_paths = remaining

    @staticmethod
    def _sub_agent_backlinks(parent_messages: list[Message]) -> tuple[set[str], set[str]]:
        invocation_ids: set[str] = set()
        log_files: set[str] = set()
        for message in parent_messages:
            for content in message.contents:
                metadata = content.additional_properties.get(TOOL_RESULT_METADATA_KEY)
                if not isinstance(metadata, dict):
                    continue
                invocation_id = metadata.get("sub_agent_invocation_id")
                if isinstance(invocation_id, str) and invocation_id:
                    invocation_ids.add(invocation_id)
                log_file = metadata.get("sub_agent_log_file")
                if isinstance(log_file, str) and log_file:
                    log_files.add(log_file)
        return invocation_ids, log_files

    async def cascade_abort_all(self) -> None:
        """Tear down every live sub-agent — used by global :class:`UserInterrupt`.

        Paused controllers resolve via the cascade decision path; running
        controllers cancel their active attempt directly so long-running
        inner tool calls stop before the parent task is cancelled.
        """
        controllers = list(self._controllers.values())
        results = await asyncio.gather(
            *(controller.cascade_abort() for controller in controllers),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                logger.debug("sub-agent cascade abort failed: %s", result)

    @property
    def approval_mode(self) -> ApprovalMode:
        """The mode a fresh invocation constructs its approval middleware with."""
        return self._approval_mode

    def set_approval_mode(self, mode: ApprovalMode) -> None:
        """Update the active approval mode for sub-agent invocations.

        Propagates to **live** ``ApprovalMiddleware`` instances so in-flight
        sub-agent runs pick up the new mode on their next tool call.  New
        invocations read the stored value at construction time.
        """
        self._approval_mode = mode
        for mw in list(self._live_approvals):
            mw.set_approval_mode(mode)

    async def cleanup(self) -> None:
        """Shut down all sub-agent instances and disconnect MCP servers."""
        cancelled: asyncio.CancelledError | None = None
        for prepared in reversed(tuple(self._prepared_by_tool.values())):
            try:
                await prepared.aclose()
            except asyncio.CancelledError as exc:
                cancelled = exc
        self._prepared_by_tool.clear()
        self._mcp_adapters.clear()
        self._agents.clear()
        self._acp_profiles.clear()
        self._chat_options_by_tool.clear()
        self._tools.clear()
        self._agent_active.clear()
        self._agent_max.clear()
        self._total_active = 0
        if cancelled is not None:
            raise cancelled
