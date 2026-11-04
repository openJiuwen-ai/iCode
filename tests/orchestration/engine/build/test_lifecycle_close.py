# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for agent lifecycle cleanup of executor-owned resources."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings, PreparedSettings, SettingsHandle
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import AgentRuntimeDetails, ApprovalAutoFulfillBlocked
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.recovery import RecoveryPersistOutcome
from chrys.orchestration.engine.build import construction as agent_lifecycle
from chrys.orchestration.engine.build.builder import AgentBuildResult
from chrys.orchestration.engine.build.loaded import ReplacedBuild
from chrys.orchestration.engine.loader import AgentLoader
from chrys.orchestration.engine.run.turn_hooks import TurnHookDispatcher
from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
from chrys.orchestration.engine.state.active_session import ActiveSession
from chrys.orchestration.invoker.resources import Conversation, PreparedAgent
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.agent_middleware.reminders.archive_pointer import CATALOG_POINTER_RECORD_COUNT_STATE_KEY
from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware
from chrys.service.approval.judge import JudgeVerdict
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.approval.turn_context import TurnContextHolder
from chrys.service.context.compaction.last_words_state import LastWordsState
from chrys.service.context.compaction.spill import SpillQuota
from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker, WorkspaceRetarget
from chrys.service.profiles.agents.schema import AgentProfile, ApprovalConfig
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.session.runtime_metadata import SessionRuntimeMetadata
from tests.support.loaded_agents import install_loaded_agent, make_loaded_agent, make_manifest


def _approval_context() -> MagicMock:
    ctx = MagicMock()
    ctx.function = SimpleNamespace(name="write_file", chrys_kind="filesystem.write")
    ctx.arguments = {"path": "/tmp/file.txt"}
    ctx.metadata = {}
    ctx.result = None
    return ctx


def _approval_policy() -> ApprovalPolicy:
    tool = MagicMock()
    tool.name = "write_file"
    tool.chrys_kind = "filesystem.write"
    return ApprovalPolicy(ApprovalConfig(default="auto", overrides={"filesystem.write": "require"}), tools=[tool])


class _FakeJudge:
    async def evaluate(self, **_kwargs: object) -> JudgeVerdict:
        return JudgeVerdict(approved=True, reason="safe")


class _FakeExecutor:
    def __init__(self, approval_middleware: ApprovalMiddleware) -> None:
        self.approval_middleware = approval_middleware
        self.history_state: dict = {}
        self.running = False
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        await self.approval_middleware.close()

    @property
    def backend(self):
        return self

    @property
    def inputs(self):
        return self

    @property
    def state(self):
        return self

    @property
    def approval(self):
        return self

    @property
    def tool_events(self):
        return self


class _Permits:
    def advance_build_generation(self) -> None:
        return None


class _FakeCheckpoints:
    async def save_checkpoint(self) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def persist_now(self) -> bool:
        return True

    async def persist_barrier(self) -> RecoveryPersistOutcome:
        return RecoveryPersistOutcome.PERSISTED


class _FakeUsagePublisher:
    def publish_usage(self, *_args: object, **_kwargs: object) -> None:
        return None

    def accumulate_invocation_usage(self, *_args: object, **_kwargs: object) -> None:
        return None

    def accumulate_side_call_usage(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def drain(self) -> None:
        return None

    async def publish_compaction(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def publish_compress(self, *_args: object, **_kwargs: object) -> None:
        return None


class _BuildSession(SimpleNamespace):
    install_build = ActiveSession.install_build


class _LoaderFixture:
    def __init__(self, bus: EventBus, executor: _FakeExecutor) -> None:
        self.current = SimpleNamespace(loaded=SimpleNamespace(), manifest=make_manifest())
        self.permits = _Permits()
        self.session = _BuildSession(session_dir=None, background_tasks=None, surface=None)
        self.session.agent_profile = AgentProfile(name="Code")
        self.session.workspace = None
        self.session.session_id = "session-1"
        self.bus = bus
        self.persistence = SimpleNamespace(state_store=None)
        self.history = MagicMock()
        install_loaded_agent(self, bindings=executor)
        install_loaded_agent(self, prepared=PreparedAgent())

        async def close_executor() -> None:
            await executor.close()

        self.current.loaded.prepared.own(close_executor)
        install_loaded_agent(self, conversation=Conversation())
        install_loaded_agent(self, sub_agent_tools=None)
        install_loaded_agent(self, active_profile=None)
        install_loaded_agent(self, tool_names=[])
        install_loaded_agent(self, tool_kinds={})
        install_loaded_agent(self, skill_names=[])
        install_loaded_agent(self, memory_files=[])
        self.session.runtime_meta = SessionRuntimeMetadata()
        install_loaded_agent(self, runtime_details=AgentRuntimeDetails())
        self.session.approval_mode = ApprovalMode.AUTO
        self.allow_user_interaction = True
        install_loaded_agent(self, intermediate_texts={})
        self.session.mutation_tracker = None
        self.workspace_change_tracker = WorkspaceChangeTracker()
        self.session.mutation_coordinator = None
        self.model_registry = None
        self.settings_handle = SettingsHandle(LoadedSettings(settings=Settings(), provenance={}))
        install_loaded_agent(self, agent=None)
        self.mcp_cache = MagicMock()
        install_loaded_agent(self, mcp_adapter=None)
        self.agent_registry = None
        install_loaded_agent(self, runtime=None)
        install_loaded_agent(self, injection=None)
        install_loaded_agent(self, consumed_injections=[])
        install_loaded_agent(self, loop_recorder=None)
        install_loaded_agent(self, reminder_middleware=SystemReminderMiddleware())
        install_loaded_agent(self, approval_judge=None)
        self.session.hook_manager = None
        self.session.outbox_recovery_task = None
        self.session.todo_tracker = None
        self.turn_state = TurnRuntimeState()
        self.turn_context = TurnContextHolder()
        self.session.spill_quota = SpillQuota()
        self.writer = _FakeCheckpoints()
        self.usage_publisher = _FakeUsagePublisher()

    @property
    def settings(self) -> Settings:
        return self.settings_handle.settings

    @property
    def loaded_settings(self) -> LoadedSettings:
        return self.settings_handle.loaded

    async def _publish_pre_compact(self, *_args: object, **_kwargs: object) -> None:
        return None

    async def _publish_load_progress(self, *_args: object, **_kwargs: object) -> None:
        return None

    def make_loader(self, build_agent_fn) -> AgentLoader:
        return AgentLoader(
            bus=self.bus,
            persistence=self.persistence,
            agent_registry=self.agent_registry,
            model_registry=self.model_registry,
            settings_handle=self.settings_handle,
            session=self.session,
            current=self.current,
            permits=self.permits,
            checkpoints=self.writer,
            usage_publisher=self.usage_publisher,
            hooks=TurnHookDispatcher(session=self.session, current=self.current),
            history=self.history,
            turn_state=self.turn_state,
            workspace_change_tracker=self.workspace_change_tracker,
            trajectory_recorder=MagicMock(),
            fsm=MagicMock(),
            turn_context=self.turn_context,
            mcp_cache=self.mcp_cache,
            mcp_overlay=None,
            allow_user_interaction=self.allow_user_interaction,
            build_agent_fn=build_agent_fn,
            register_current_engine=lambda: None,
        )


def _prepared(loaded: LoadedSettings) -> PreparedSettings:
    """*loaded* staged as it stands: no session layer, no live choices."""
    return PreparedSettings(base=loaded, effective=loaded)


def _stage(engine: _LoaderFixture) -> agent_lifecycle.StagedBuild:
    """Stage the engine's own live state, as start/soft_restart do for a rebuild."""
    return agent_lifecycle.stage_build(
        session=engine.session,
        persistence=engine.persistence,
        prepared=engine.settings_handle.prepare(engine.settings_handle.base),
        agent_profile=AgentProfile(name="Code"),
        workspace=engine.session.workspace,
        hook_manager=engine.session.hook_manager,
    )


async def _build_through_loader(
    engine: _LoaderFixture,
    profile: AgentProfile,
    *,
    staged: agent_lifecycle.StagedBuild,
    build_agent_fn: agent_lifecycle.BuildAgentFn,
    preserved_history: dict | None = None,
) -> None:
    loader = engine.make_loader(build_agent_fn)
    completed = await loader.build(profile, staged, preserved_history=preserved_history)
    replaced = loader.install(completed)
    await loader.release(replaced)


async def _drive_auto_approval(approval_middleware: ApprovalMiddleware) -> None:
    called = False

    async def _next() -> None:
        nonlocal called
        called = True

    await approval_middleware.process(_approval_context(), _next)
    assert called


@pytest.mark.asyncio
async def test_agent_rebuild_closes_previous_executor_approval_handler() -> None:
    bus = EventBus()
    old_approval = ApprovalMiddleware(
        approval_policy=_approval_policy(),
        event_bus=bus,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=_FakeJudge(),
    )
    await _drive_auto_approval(old_approval)
    old_handler = old_approval._on_auto_fulfill_blocked
    old_executor = _FakeExecutor(old_approval)
    engine = _LoaderFixture(bus, old_executor)

    new_approvals: list[ApprovalMiddleware] = []

    async def _build_agent_fn(**_kwargs: object) -> AgentBuildResult:
        new_approval = ApprovalMiddleware(
            approval_policy=_approval_policy(),
            event_bus=bus,
            approval_mode=ApprovalMode.AUTO,
            approval_judge=_FakeJudge(),
        )
        await _drive_auto_approval(new_approval)
        new_approvals.append(new_approval)
        executor = _FakeExecutor(new_approval)
        prepared = PreparedAgent()
        prepared.own(executor.close)
        return AgentBuildResult(
            prepared=prepared,
            conversation=Conversation(),
            agent=MagicMock(),
            bindings=executor,
            runtime=MagicMock(),
            loop_recorder=MagicMock(),
            reminder_middleware=MagicMock(),
            last_words=MagicMock(spec=LastWordsState),
            sub_agent_tools=None,
            mcp_adapter=None,
            skills_provider=None,
            tool_names=[],
            tool_kinds={},
            skill_names=[],
            memory_files=[],
            agent_profile_fingerprint="agent-fp",
            model_profile_fingerprint="model-fp",
            runtime_details=AgentRuntimeDetails(),
            compaction_strategy=MagicMock(),
            active_profile=ModelProfile(id="test", name="test"),
        )

    await _build_through_loader(
        engine, AgentProfile(name="Code"), staged=_stage(engine), build_agent_fn=_build_agent_fn
    )

    new_handler = new_approvals[0]._on_auto_fulfill_blocked
    handlers = bus._handlers[ApprovalAutoFulfillBlocked]
    assert old_executor.closed is True
    assert old_handler not in handlers
    assert new_handler in handlers
    assert old_handler != new_handler


@pytest.mark.asyncio
async def test_agent_rebuild_retargets_workspace_after_install_before_awaited_cleanup(tmp_path: Path) -> None:
    bus = EventBus()
    old_approval = _fresh_approval(bus)
    old_executor = _FakeExecutor(old_approval)
    engine = _LoaderFixture(bus, old_executor)
    engine.session.workspace = Workspace.from_cwd(str(tmp_path))
    order: list[str] = []

    resolved: list[WorkspaceRetarget] = []

    class _Tracker(WorkspaceChangeTracker):
        def take_pending_notice(self) -> None:
            return None

        def resolve_retarget(self, workspace: Workspace | None, *, resolve_scope: bool = True) -> WorkspaceRetarget:
            resolved.append(super().resolve_retarget(workspace, resolve_scope=resolve_scope))
            return resolved[-1]

        def apply_retarget(self, retarget: WorkspaceRetarget) -> None:
            # The build resolved it for the session's workspace; install never resolves again.
            assert resolved == [retarget] and retarget is resolved[0]
            assert retarget.new_cwd == os.path.normpath(str(tmp_path))
            assert engine.current.loaded.bindings is not old_executor
            order.append("retarget")

    tracker = _Tracker()
    engine.workspace_change_tracker = tracker  # type: ignore[assignment]
    original_close = old_executor.close

    async def _close() -> None:
        order.append("close")
        await original_close()

    old_executor.close = _close  # type: ignore[method-assign]

    async def _build_agent_fn(**_kwargs: object) -> AgentBuildResult:
        return _make_build_result(_fresh_approval(bus))

    await _build_through_loader(
        engine, AgentProfile(name="Code"), staged=_stage(engine), build_agent_fn=_build_agent_fn
    )

    assert order == ["retarget", "close"]


@pytest.mark.anyio
@pytest.mark.parametrize("notice_enabled", [True, False])
async def test_agent_build_retargets_with_the_installed_notice_setting(tmp_path: Path, notice_enabled: bool) -> None:
    """The retarget reads the settings the build just installed: with the
    notice off the tracker is told not to probe the roots at all."""
    bus = EventBus()
    engine = _LoaderFixture(bus, _FakeExecutor(_fresh_approval(bus)))
    engine.session.workspace = Workspace.from_cwd(str(tmp_path))
    engine.settings_handle.install(
        LoadedSettings(settings=Settings(workspace_change_notice=notice_enabled), provenance={})
    )
    seen: list[bool] = []

    class _Tracker(WorkspaceChangeTracker):
        def take_pending_notice(self) -> None:
            return None

        def resolve_retarget(self, workspace, *, resolve_scope=True):
            seen.append(resolve_scope)
            return super().resolve_retarget(workspace, resolve_scope=resolve_scope)

    engine.workspace_change_tracker = _Tracker()  # type: ignore[assignment]

    async def _build_agent_fn(**_kwargs: object) -> AgentBuildResult:
        return _make_build_result(_fresh_approval(bus))

    await _build_through_loader(
        engine, AgentProfile(name="Code"), staged=_stage(engine), build_agent_fn=_build_agent_fn
    )

    assert seen == [notice_enabled]


def _make_build_result(approval: ApprovalMiddleware) -> AgentBuildResult:
    executor = _FakeExecutor(approval)
    prepared = PreparedAgent()
    prepared.own(executor.close)
    return AgentBuildResult(
        prepared=prepared,
        conversation=Conversation(),
        agent=MagicMock(),
        bindings=executor,
        runtime=MagicMock(),
        loop_recorder=MagicMock(),
        reminder_middleware=MagicMock(),
        last_words=MagicMock(spec=LastWordsState),
        sub_agent_tools=None,
        mcp_adapter=None,
        skills_provider=None,
        tool_names=[],
        tool_kinds={},
        skill_names=[],
        memory_files=[],
        agent_profile_fingerprint="agent-fp",
        model_profile_fingerprint="model-fp",
        runtime_details=AgentRuntimeDetails(),
        compaction_strategy=MagicMock(),
        active_profile=ModelProfile(id="test", name="test"),
    )


def _fresh_approval(bus: EventBus) -> ApprovalMiddleware:
    return ApprovalMiddleware(
        approval_policy=_approval_policy(),
        event_bus=bus,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=_FakeJudge(),
    )


@pytest.mark.asyncio
async def test_agent_rebuild_closes_coordinator_when_coordination_disabled() -> None:
    """``CHRYS_MUTATION_COORDINATION=0`` must bite on settings reload,
    not only at process start: the rebuild stamps + drops an existing
    coordinator instead of threading it into the new executor.
    """
    bus = EventBus()
    old_approval = _fresh_approval(bus)
    await _drive_auto_approval(old_approval)
    engine = _LoaderFixture(bus, _FakeExecutor(old_approval))
    coordinator = MagicMock()
    engine.session.mutation_coordinator = coordinator
    engine.settings_handle.install(LoadedSettings(settings=Settings(mutation_coordination=False), provenance={}))

    captured: dict = {}

    async def _build_agent_fn(**kwargs: object) -> AgentBuildResult:
        captured.update(kwargs)
        new_approval = _fresh_approval(bus)
        await _drive_auto_approval(new_approval)
        return _make_build_result(new_approval)

    await _build_through_loader(
        engine, AgentProfile(name="Code"), staged=_stage(engine), build_agent_fn=_build_agent_fn
    )

    coordinator.close.assert_called_once_with()
    assert engine.session.mutation_coordinator is None
    assert captured["mutation_coordinator"] is None


@pytest.mark.asyncio
async def test_agent_rebuild_keeps_coordinator_when_coordination_enabled() -> None:
    """The teardown branch must not touch a coordinator while the
    setting stays on — same instance threads into the rebuilt executor."""
    bus = EventBus()
    old_approval = _fresh_approval(bus)
    await _drive_auto_approval(old_approval)
    engine = _LoaderFixture(bus, _FakeExecutor(old_approval))
    coordinator = MagicMock()
    engine.session.mutation_coordinator = coordinator
    engine.settings_handle.install(LoadedSettings(settings=Settings(mutation_coordination=True), provenance={}))

    captured: dict = {}

    async def _build_agent_fn(**kwargs: object) -> AgentBuildResult:
        captured.update(kwargs)
        new_approval = _fresh_approval(bus)
        await _drive_auto_approval(new_approval)
        return _make_build_result(new_approval)

    await _build_through_loader(
        engine, AgentProfile(name="Code"), staged=_stage(engine), build_agent_fn=_build_agent_fn
    )

    coordinator.close.assert_not_called()
    assert engine.session.mutation_coordinator is coordinator
    assert captured["mutation_coordinator"] is coordinator
    assert captured["spill_quota"] is engine.session.spill_quota
    assert captured["persist_recovery_now"] == engine.writer.persist_now


# ──────────────── the staged-build transaction ─────────────────────────


@pytest.mark.asyncio
async def test_a_failed_build_leaves_the_live_state_untouched_and_drains_the_candidate(tmp_path: Path) -> None:
    """A failure before the commit is a failure that never happened, live-wise:
    settings handle, workspace, hooks, coordinator and executor all stay — only
    the never-went-live coordinator candidate is stamped closed."""
    bus = EventBus()
    old_approval = _fresh_approval(bus)
    old_executor = _FakeExecutor(old_approval)
    engine = _LoaderFixture(bus, old_executor)
    engine.session.workspace = Workspace.from_cwd(str(tmp_path / "old"))
    old_loaded = engine.loaded_settings
    old_workspace = engine.session.workspace
    old_hook_manager = MagicMock()
    engine.session.hook_manager = old_hook_manager
    old_coordinator = MagicMock()
    engine.session.mutation_coordinator = old_coordinator

    staged_coordinator = MagicMock()
    staged = agent_lifecycle.StagedBuild(
        prepared=_prepared(LoadedSettings(settings=Settings(default_approval_mode="auto"), provenance={})),
        agent_profile=AgentProfile(name="Code"),
        workspace=Workspace.from_cwd(str(tmp_path / "new")),
        hook_manager=MagicMock(),
        mutation_coordinator=staged_coordinator,
    )

    async def _failing_build(**_kwargs: object) -> AgentBuildResult:
        raise RuntimeError("build failed")

    with pytest.raises(RuntimeError, match="build failed"):
        await _build_through_loader(engine, AgentProfile(name="Code"), staged=staged, build_agent_fn=_failing_build)

    assert engine.loaded_settings is old_loaded
    assert engine.session.workspace is old_workspace
    assert engine.session.hook_manager is old_hook_manager
    assert engine.session.mutation_coordinator is old_coordinator
    assert engine.current.loaded.bindings is old_executor
    assert old_executor.closed is False
    old_coordinator.close.assert_not_called()
    staged_coordinator.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_the_build_commit_installs_the_staged_state_together(tmp_path: Path) -> None:
    """Six commitments, one commit point: what the build was configured from
    is exactly what goes live with the executor it produced — and the replaced
    coordinator is stamped closed only after that commit."""
    bus = EventBus()
    old_approval = _fresh_approval(bus)
    await _drive_auto_approval(old_approval)
    old_executor = _FakeExecutor(old_approval)
    engine = _LoaderFixture(bus, old_executor)
    engine.session.workspace = Workspace.from_cwd(str(tmp_path / "old"))
    old_coordinator = MagicMock()
    engine.session.mutation_coordinator = old_coordinator

    staged = agent_lifecycle.StagedBuild(
        prepared=_prepared(LoadedSettings(settings=Settings(default_approval_mode="auto"), provenance={})),
        agent_profile=AgentProfile(name="Code"),
        workspace=Workspace.from_cwd(str(tmp_path / "new")),
        hook_manager=MagicMock(),
        mutation_coordinator=MagicMock(),
    )
    captured: dict = {}

    async def _build_agent_fn(**kwargs: object) -> AgentBuildResult:
        captured.update(kwargs)
        new_approval = _fresh_approval(bus)
        await _drive_auto_approval(new_approval)
        return _make_build_result(new_approval)

    await _build_through_loader(engine, staged.agent_profile, staged=staged, build_agent_fn=_build_agent_fn)

    # The build read staged inputs, never the live fields it replaced.
    assert captured["settings"] is staged.loaded.settings
    assert captured["workspace"] is staged.workspace
    assert captured["hook_manager"] is staged.hook_manager
    assert captured["mutation_coordinator"] is staged.mutation_coordinator
    # The commit installed the same staged state, whole.
    assert engine.loaded_settings is staged.loaded
    assert engine.session.agent_profile is staged.agent_profile
    assert engine.session.workspace is staged.workspace
    assert engine.session.hook_manager is staged.hook_manager
    assert engine.session.mutation_coordinator is staged.mutation_coordinator
    assert engine.current.loaded.bindings is not old_executor
    assert old_executor.closed is True
    old_coordinator.close.assert_called_once_with()


@pytest.mark.asyncio
async def test_a_cancelled_post_commit_cleanup_still_leaves_the_committed_profile() -> None:
    """The awaited cleanups between the commit and the return can be
    cancelled; the profile went live inside the commit, so the session never
    runs the new executor under the old profile's name — and the remaining
    finalization steps still run before the cancellation resumes, because the
    commit already unhooked the replaced objects from every later cleanup."""
    bus = EventBus()
    old_approval = _fresh_approval(bus)
    await _drive_auto_approval(old_approval)

    class _CancelledOnClose(_FakeExecutor):
        async def close(self) -> None:
            raise asyncio.CancelledError

    old_executor = _CancelledOnClose(old_approval)
    engine = _LoaderFixture(bus, old_executor)
    old_profile = engine.session.agent_profile
    old_coordinator = MagicMock()
    engine.session.mutation_coordinator = old_coordinator
    replaced_cleanups: list[tuple[object, ...]] = []

    async def _record_replaced_cleanup() -> None:
        replaced_cleanups.append((engine.current.loaded.prepared,))

    engine.current.loaded.prepared.own(_record_replaced_cleanup)
    staged = agent_lifecycle.StagedBuild(
        prepared=_prepared(LoadedSettings(settings=Settings(), provenance={})),
        agent_profile=AgentProfile(name="Explore"),
        workspace=None,
        hook_manager=None,
        mutation_coordinator=None,
    )

    async def _build_agent_fn(**kwargs: object) -> AgentBuildResult:
        _ = kwargs
        new_approval = _fresh_approval(bus)
        await _drive_auto_approval(new_approval)
        return _make_build_result(new_approval)

    with pytest.raises(asyncio.CancelledError):
        await _build_through_loader(engine, staged.agent_profile, staged=staged, build_agent_fn=_build_agent_fn)

    assert engine.session.agent_profile is staged.agent_profile
    assert engine.session.agent_profile is not old_profile
    # The cancelled executor close did not strand the later steps.
    old_coordinator.close.assert_called_once_with()
    assert replaced_cleanups != []


@pytest.mark.asyncio
async def test_a_cancelled_post_commit_cleanup_still_carries_the_preserved_history() -> None:
    """The conversation rides the commit, not the finalization after it: once
    the executor is reachable it can be saved, so a cancellation landing in
    the awaited cleanup steps must never publish an executor whose empty
    history the next save would persist over the real one."""
    bus = EventBus()
    old_approval = _fresh_approval(bus)
    await _drive_auto_approval(old_approval)

    class _CancelledOnClose(_FakeExecutor):
        async def close(self) -> None:
            raise asyncio.CancelledError

    old_executor = _CancelledOnClose(old_approval)
    engine = _LoaderFixture(bus, old_executor)
    staged = agent_lifecycle.StagedBuild(
        prepared=_prepared(LoadedSettings(settings=Settings(), provenance={})),
        agent_profile=AgentProfile(name="Explore"),
        workspace=None,
        hook_manager=None,
        mutation_coordinator=None,
    )
    preserved = {
        "messages": [{"role": "user", "content": "kept"}],
        "turn_counter": 3,
        CATALOG_POINTER_RECORD_COUNT_STATE_KEY: 7,
    }

    async def _build_agent_fn(**kwargs: object) -> AgentBuildResult:
        _ = kwargs
        new_approval = _fresh_approval(bus)
        await _drive_auto_approval(new_approval)
        return _make_build_result(new_approval)

    with pytest.raises(asyncio.CancelledError):
        await _build_through_loader(
            engine,
            staged.agent_profile,
            staged=staged,
            build_agent_fn=_build_agent_fn,
            preserved_history=preserved,
        )

    assert engine.current.loaded.bindings is not old_executor
    assert engine.current.loaded.bindings.backend.history_state == preserved
    engine.current.loaded.last_words.restore.assert_called_once_with(preserved, available_relative_paths=None)
    archive_pointer = engine.current.loaded.reminder_middleware.sources.archive_pointer
    archive_pointer.restore_record_count.assert_called_once_with(7)
    # The history manager rode the same commit: bound to the very dict the
    # new executor holds, not left on the replaced executor's history.
    engine.history.bind.assert_called_with(engine.current.loaded.bindings.backend.history_state)
    assert engine.history.bind.call_args[0][0] is engine.current.loaded.bindings.backend.history_state


@pytest.mark.asyncio
async def test_replaced_resource_cleanup_survives_a_cancelled_step() -> None:
    """A cancellation inside one release step must not skip the steps after
    it: the commit already swapped the live pointers, so whatever is skipped
    here is unreachable and never runs again."""

    class _CancelledAgent:
        async def __aexit__(self, *args: object) -> None:
            raise asyncio.CancelledError

    cleaned: list[str] = []

    class _Tools:
        async def cleanup(self) -> None:
            cleaned.append("sub_agent_tools")

    class _Mcp:
        async def disconnect_all(self) -> None:
            cleaned.append("mcp_adapter")

    bus = EventBus()
    engine = _LoaderFixture(bus, _FakeExecutor(_fresh_approval(bus)))
    old_prepared = PreparedAgent()
    old_prepared.own(_Mcp().disconnect_all)
    old_prepared.own(_Tools().cleanup)
    old_prepared.own(lambda: _CancelledAgent().__aexit__(None, None, None))

    with pytest.raises(asyncio.CancelledError):
        await engine.make_loader(None).release(
            ReplacedBuild(loaded=make_loaded_agent(prepared=old_prepared), coordinator=None)
        )

    assert cleaned == ["sub_agent_tools", "mcp_adapter"]


# ──────────────── intermediate-text capture callbacks (§2.1.1) ─────────


async def _build_with_captured_callbacks() -> tuple[_LoaderFixture, dict]:
    """Build via the real ``build_agent`` and capture its callback kwargs."""
    bus = EventBus()
    old_approval = _fresh_approval(bus)
    await _drive_auto_approval(old_approval)
    engine = _LoaderFixture(bus, _FakeExecutor(old_approval))

    captured: dict = {}

    async def _build_agent_fn(**kwargs: object) -> AgentBuildResult:
        captured.update(kwargs)
        new_approval = _fresh_approval(bus)
        await _drive_auto_approval(new_approval)
        return _make_build_result(new_approval)

    await _build_through_loader(
        engine, AgentProfile(name="Code"), staged=_stage(engine), build_agent_fn=_build_agent_fn
    )
    return engine, captured


@pytest.mark.asyncio
async def test_intermediate_capture_records_batch_id_at_capture_time() -> None:
    """The callback maps the POST-increment batch id — the same id the
    batch's subsequent tool records are stamped with — to the captured text.
    An empty-text boundary advances the counter but writes no mapping entry.
    """
    engine, captured = await _build_with_captured_callbacks()
    on_intermediate = captured["on_intermediate"]
    counter = captured["batch_counter"]

    on_intermediate("")  # tool-only boundary
    assert counter.batch_id == 1
    assert engine.current.loaded.intermediate_texts == {}

    on_intermediate("first text")
    assert counter.batch_id == 2
    assert engine.current.loaded.intermediate_texts == {2: "first text"}

    on_intermediate("")  # another boundary — mapping unchanged
    assert counter.batch_id == 3
    assert engine.current.loaded.intermediate_texts == {2: "first text"}


@pytest.mark.asyncio
async def test_intermediate_capture_continues_numbering_across_passes() -> None:
    """A second pass continues the counter (retry semantics: nothing resets it)."""
    engine, captured = await _build_with_captured_callbacks()
    on_intermediate = captured["on_intermediate"]

    sequence = ["", "first text", "", "second text", ""]
    for text in [*sequence, *sequence]:  # pass 1 + retry continuation, uninterrupted counter
        on_intermediate(text)

    assert engine.current.loaded.intermediate_texts == {
        2: "first text",
        4: "second text",
        7: "first text",
        9: "second text",
    }
    assert captured["batch_counter"].batch_id == 2 * len(sequence)
