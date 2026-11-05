# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Engine-internal companion module for ``AgentEngine`` build and restart orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from chrys.foundation.events.types import (
    UserInjectResult,
)
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workspace import Workspace
from chrys.orchestration.engine.build.builder import AgentBuildResult
from chrys.orchestration.engine.build.loaded import AgentManifest, CompletedBuild, LoadedAgent
from chrys.orchestration.invoker.resources import rollback_resources
from chrys.orchestration.invoker.runtime import restore_phase4_state
from chrys.service.agent_middleware import ResponseBatchCounter
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.mutations.coordination import ATTRIBUTION_DIR_NAME, MutationCoordinator
from chrys.service.mutations.store import SnapshotPolicy, SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker
from chrys.service.session.history import stamp_history_item_ids
from chrys.service.todos.tracker import TodoTracker
from chrys.service.trajectory.preparation import PreparationOutcome

if TYPE_CHECKING:
    from chrys.foundation.config.settings_store import LoadedSettings, PreparedSettings
    from chrys.foundation.events.bus import EventBus
    from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.orchestration.engine.state.session_writer import RecoveryCheckpoints
    from chrys.orchestration.engine.usage import UsagePublisher
    from chrys.service.agent_middleware.injection import ConsumedInjection
    from chrys.service.approval.turn_context import TurnContextHolder
    from chrys.service.context.compaction import PreCompactInfo
    from chrys.service.hooks.manager import HookManager
    from chrys.service.mcp.cache import MCPConnectionCache
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.session.persistence import SessionPersistence


logger = logging.getLogger(__name__)


BuildAgentFn = Callable[..., Awaitable[AgentBuildResult]]


@dataclass(frozen=True, slots=True)
class StagedBuild:
    """What a rebuild reads *instead of* live engine state, committed whole.

    A rebuild used to stage its inputs by assigning them to the engine before
    the build and assigning them back on failure. Every await inside the build
    made that a lie: any concurrent task reading the engine saw an uncommitted,
    possibly-about-to-roll-back value. The staged inputs travel here as
    parameters instead, the build reads only these, and
    :func:`build_agent` installs all of them in the same breath as the
    executor they configured — so live state either describes the old build
    entirely or the new one entirely, never a mixture.

    ``prepared`` carries settings and provenance as the one value they already
    are — the load, the session's own choices over it and the effective value
    the build reads (:attr:`loaded`); the agent profile, the workspace, the hook
    manager and the mutation-coordinator target are the other four things a
    rebuild may change. Six commitments, one commit point.
    """

    prepared: PreparedSettings
    agent_profile: AgentProfile
    """The profile the build is configured from, recorded by the commit.

    Recorded there rather than by the caller after the build returns: every
    await between the commit and the caller's next line can be cancelled, and
    a session running the new executor while still recording the old profile
    would save — and later restore — as something it is not.
    """

    workspace: Workspace | None
    hook_manager: HookManager | None
    mutation_coordinator: MutationCoordinator | None
    """The coordinator the new build runs with — a *candidate*, not installed.

    The current instance when it survives, ``None`` when the staged settings
    turn coordination off, or a fresh not-yet-installed instance when they
    turn it on. The old instance is closed only after the commit; a failed
    build closes the candidate instead (see :func:`build_agent`).
    """

    @property
    def loaded(self) -> LoadedSettings:
        """The effective settings the build is configured from, the session's own choices included."""
        return self.prepared.effective


def stage_build(
    *,
    session: ActiveSession,
    persistence: SessionPersistence,
    prepared: PreparedSettings,
    agent_profile: AgentProfile,
    workspace: Workspace | None,
    hook_manager: HookManager | None,
) -> StagedBuild:
    """Assemble a rebuild's staged input, deriving the coordinator target.

    Derived here rather than passed by callers because the target is a pure
    function of the staged settings and the session: settings say off →
    ``None`` (the escape hatch must bite without a restart); an instance
    exists and settings say on → keep it; none exists and the session can
    host one → stage a fresh candidate, which stays uninstalled until the
    build commits.
    """
    coordinator = session.mutation_coordinator
    if not prepared.effective.settings.mutation_coordination:
        coordinator = None
    elif coordinator is None:
        session_dir = session.session_dir if persistence.state_store is not None else None
        if session_dir is not None and session.session_id:
            # Registry root sits beside the session dirs so tests with a
            # relocated state store stay sandboxed automatically.
            coordinator = MutationCoordinator(
                registry_root=session_dir.parent / ATTRIBUTION_DIR_NAME,
                session_id=session.session_id,
            )
    return StagedBuild(
        prepared=prepared,
        agent_profile=agent_profile,
        workspace=workspace,
        hook_manager=hook_manager,
        mutation_coordinator=coordinator,
    )


async def _close_coordinator(coordinator: MutationCoordinator, *, reason: str) -> None:
    """Stamp a coordinator's registry file closed, off-loop and best-effort."""
    try:
        await asyncio.get_running_loop().run_in_executor(None, coordinator.close)
    except Exception:
        logger.debug("Mutation coordinator close failed (%s)", reason, exc_info=True)


async def build_agent(
    profile: AgentProfile,
    *,
    session: ActiveSession,
    persistence: SessionPersistence,
    bus: EventBus,
    agent_registry: AgentProfileRegistry | None,
    model_registry: ModelProfileRegistry | None,
    workspace_change_tracker: WorkspaceChangeTracker,
    turn_state: TurnRuntimeState,
    turn_context: TurnContextHolder,
    mcp_cache: MCPConnectionCache,
    allow_user_interaction: bool,
    checkpoints: RecoveryCheckpoints,
    usage_publisher: UsagePublisher,
    fire_pre_compact: Callable[[PreCompactInfo], Awaitable[None]],
    publish_load_progress: Callable[..., Awaitable[None]],
    hold_injection_notification: Callable[[asyncio.Task[None]], None],
    staged: StagedBuild,
    build_agent_fn: BuildAgentFn,
    preserved_history: dict | None = None,
) -> CompletedBuild:
    """Build and prepare candidate values before the loader installs any live state.

    Preparation owns the returned resources until every fallible calculation
    succeeds. A failure closes both candidate resource groups before escaping.
    """
    if profile.acp is not None:
        raise ValueError(f"ACP profile {profile.name!r} is sub-agent-only and cannot be launched as the main agent")

    batch_counter = ResponseBatchCounter()
    intermediate_texts: dict[int, str] = {}

    # Every tool-calling response records ``batch_id → text`` at capture
    # time — list position cannot reconstruct ids after the fact (retries
    # continue numbering, and batch boundaries can fire without a captured
    # text). An empty-text boundary signal still advances the counter but
    # writes no mapping entry; a non-empty text records the post-increment
    # batch_id — the same id the batch's subsequent tool calls are stamped
    # with, so the text attaches to the batch it precedes.

    def _on_intermediate(text: str) -> None:
        batch_counter.new_batch()
        if text:
            intermediate_texts[batch_counter.batch_id] = text

    injection = InjectionMiddleware()
    consumed_injections: list[ConsumedInjection] = []

    async def _on_injection_batch_consumed(batch: tuple[ConsumedInjection, ...]) -> None:
        """Register one immutable wire batch before its first durability await."""
        newly_consumed: list[ConsumedInjection] = []
        for consumed in batch:
            existing_index = next(
                (
                    index
                    for index, existing in enumerate(consumed_injections)
                    if existing.consumption_id == consumed.consumption_id
                ),
                None,
            )
            if existing_index is None:
                consumed_injections.append(consumed)
                newly_consumed.append(consumed)
            else:
                # A retry reuses the stable consumption identity but updates
                # the failed attempt's anchor to the successful wire context.
                consumed_injections[existing_index] = consumed

        session_id = session.session_id

        async def _publish_results() -> None:
            for consumed in newly_consumed:
                await bus.publish(
                    UserInjectResult(
                        text=consumed.text,
                        consumed=True,
                        created_at=consumed.created_at,
                        injection_id=consumed.injection_id,
                        session_id=session_id,
                    )
                )

        if newly_consumed:
            # Consumption is already irrevocably registered. Neither writer
            # backpressure nor task cancellation may prevent its frontend
            # notification from being scheduled.
            for consumed in newly_consumed:
                if consumed.preparation is not None:
                    consumed.preparation.finished_soon(
                        outcome=PreparationOutcome.INJECTED,
                        target_turn_id=consumed.target_turn_id,
                    )
            # Preserve Chrys's established detached notification behavior: a
            # retry updates anchors but does not render the same injection a
            # second time, and Agent.run cancellation cannot tear delivery in
            # half. The complete batch is already registered above.
            hold_injection_notification(asyncio.create_task(_publish_results()))
        # Crash durability: the wire copy lives only in the tool loop and
        # the persisted copy is written by the finalizer — a hard crash in
        # between loses the injected user message.  Snapshot a recovery
        # checkpoint (which replays ``consumed_injections``) and FLUSH the
        # background writer before returning: ``_save_recovery_checkpoint``
        # alone only queues the snapshot, and every later snapshot also
        # carries the injection (the list is append-only until finalization
        # clears it), so awaiting the newest-wins drain is sound.  Injection
        # consumption is rare and immediately precedes an LLM call — one
        # awaited write here is negligible; plain LLM-boundary checkpoints
        # stay fire-and-forget.
        await checkpoints.save_checkpoint()
        await checkpoints.flush()

    injection.set_on_consumed_batch(_on_injection_batch_consumed)

    session_dir = session.session_dir if persistence.state_store is not None else None
    mutation_tracker = session.mutation_tracker
    if mutation_tracker is None and session_dir is not None:
        mutation_tracker = MutationTracker(
            SnapshotStore(session_dir, policy=SnapshotPolicy.from_settings(staged.loaded.settings))
        )
    # Profile-independent (unlike the tool/middleware wiring in build_agent):
    # a conditional tracker would drop ``chrys_todos`` on the first save after
    # switching to a todo-less profile. Restore pre-hydrates; respect it.
    todo_tracker = session.todo_tracker
    if todo_tracker is None:
        todo_tracker = TodoTracker()

    from chrys.service.approval.judge import ApprovalJudge
    from chrys.service.llm.route_sessions import derive_llm_route_session_id
    from chrys.service.profiles.models.resolver import resolve_for_agent, resolve_judge_profile

    # "Use the active model" means the model this agent runs on. A session override or the
    # agent profile's own model moves that away from ``settings.model_profile``, which may
    # then name nothing usable at all.
    judge_fallback = resolve_for_agent(model_registry, staged.loaded.settings, profile)
    judge_profile = resolve_judge_profile(model_registry, staged.loaded.settings, judge_fallback)
    judge_session_id = derive_llm_route_session_id(
        session.session_id,
        route_kind="approval-judge",
        model_profile=judge_profile,
    )
    approval_judge = ApprovalJudge(
        judge_profile,
        auto_review=staged.loaded.settings.approval_auto_review,
        session_id=judge_session_id,
        parent_session_id=session.session_id,
        session_dir=session_dir,
    )

    old_coordinator = session.mutation_coordinator
    result: AgentBuildResult | None = None
    try:
        result = await build_agent_fn(
            profile=profile,
            settings=staged.loaded.settings,
            model_registry=model_registry,
            workspace=staged.workspace,
            session_id=session.session_id,
            bus=bus,
            agent_registry=agent_registry,
            injection=injection,
            batch_counter=batch_counter,
            on_intermediate=_on_intermediate,
            on_usage=usage_publisher.publish_usage,
            on_sub_agent_usage=usage_publisher.accumulate_invocation_usage,
            on_side_call_usage=usage_publisher.accumulate_side_call_usage,
            drain_parent_usage_publishes=usage_publisher.drain,
            on_compaction=usage_publisher.publish_compaction,
            on_pre_compact=fire_pre_compact,
            on_compress=usage_publisher.publish_compress,
            on_load_progress=publish_load_progress,
            mutation_tracker=mutation_tracker,
            mutation_coordinator=staged.mutation_coordinator,
            todo_tracker=todo_tracker,
            file_change_provider=workspace_change_tracker.take_pending_notice,
            approval_mode=session.approval_mode,
            approval_judge=approval_judge,
            session_dir=session_dir,
            mcp_cache=mcp_cache,
            hook_manager=staged.hook_manager,
            spill_quota=session.spill_quota,
            background_tasks=session.background_tasks,
            persist_recovery_now=checkpoints.persist_now,
            turn_context=turn_context,
            allow_user_interaction=allow_user_interaction,
            # `icode run` holds a reply's text until it knows whether it is the final answer; the TUI and ACP stream it.
            shows_answer_text=session.surface is not SessionSurface.CLI,
            # Only the TUI's chat cards show a sub-agent's answer text; `icode run` and ACP show none of it.
            sub_agent_shows_answer_text=session.surface not in (SessionSurface.CLI, SessionSurface.ACP),
        )
        # The judge creates its client on first evaluation, never during the
        # build, so the build's own rollback has nothing of it to close.
        result.prepared.own(approval_judge.aclose)
        settings = staged.prepared
        result.loop_recorder.on_pre_wire_barrier = checkpoints.persist_barrier
        result.loop_recorder.on_result_checkpoint = checkpoints.save_checkpoint
        result.bindings.inputs.recovery_input_recorder = turn_state.set_current_input
        if preserved_history is not None:
            # Part of the commit, not of the post-commit cleanup below: once the
            # executor is reachable it can be saved, so the conversation must be
            # in it before anything awaits — a cancellation landing in the cleanup
            # steps would otherwise publish an executor whose empty history the
            # next save persists over the real one.
            result.bindings.backend.history_state = preserved_history
            if result.reminder_middleware is not None:
                restore_phase4_state(result.reminder_middleware, result.last_words, preserved_history)
        stamp_history_item_ids(result.bindings.backend.history_state)
        manifest = AgentManifest.from_build(result)
        if result.compaction_strategy is not None:
            session.runtime_meta.restore_context_calibration(
                result.compaction_strategy,
                model_profile_fingerprint=manifest.model_profile_fingerprint,
                agent_profile_fingerprint=manifest.agent_profile_fingerprint,
            )
        workspace_retarget = workspace_change_tracker.resolve_retarget(
            staged.workspace, resolve_scope=settings.effective.settings.workspace_change_notice
        )
        return CompletedBuild(
            staged=staged,
            loaded=LoadedAgent(
                prepared=result.prepared,
                conversation=result.conversation,
                agent=result.agent,
                bindings=result.bindings,
                runtime=result.runtime,
                injection=injection,
                consumed_injections=consumed_injections,
                intermediate_texts=intermediate_texts,
                loop_recorder=result.loop_recorder,
                reminder_middleware=result.reminder_middleware,
                last_words=result.last_words,
                approval_judge=approval_judge,
                sub_agent_tools=result.sub_agent_tools,
                skills_provider=result.skills_provider,
                mcp_adapter=result.mcp_adapter,
            ),
            manifest=manifest,
            settings=settings,
            workspace_retarget=workspace_retarget,
            mutation_tracker=mutation_tracker,
            todo_tracker=todo_tracker,
            compaction_strategy=result.compaction_strategy,
        )
    except BaseException as error:
        cancelled: asyncio.CancelledError | None = None
        if staged.mutation_coordinator is not None and staged.mutation_coordinator is not old_coordinator:
            try:
                await _close_coordinator(staged.mutation_coordinator, reason="failed build")
            except asyncio.CancelledError as exc:
                cancelled = exc
        if result is not None:
            try:
                await rollback_resources(result.prepared)
            except asyncio.CancelledError as exc:
                cancelled = exc
        if cancelled is not None:
            raise cancelled from error
        raise
