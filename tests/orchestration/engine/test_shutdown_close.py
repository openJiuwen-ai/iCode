# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for engine shutdown cleanup of executor-owned resources."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import chrys.orchestration.engine.build.builder as agent_builder_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalAutoFulfillBlocked, SessionNew, UserInject, UserMessage, UserRetry
from chrys.foundation.models.invocations import PassHandle
from chrys.foundation.trajectory.event_types import EventType
from chrys.kernel import FunctionTool, Message
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.build.construction import StagedBuild
from chrys.orchestration.engine.build.loaded import CompletedBuild
from chrys.orchestration.engine.execution import PendingRetry
from chrys.orchestration.engine.state.machine import Trigger
from chrys.orchestration.invoker import runtime as runtime_factory_module
from chrys.orchestration.invoker.contracts import AbortCause, AbortResult
from chrys.orchestration.invoker.resources import PreparedAgent
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.approval.judge import JudgeVerdict
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.runner import HookResult
from chrys.service.hooks.schema import HookConfig, HookDecision, HookExecution, HookRun, HooksFile
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    MCPServerConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.resolver import ModelSelection
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from chrys.service.trajectory.hooks import HookOutcome
from chrys.service.trajectory.preparation import PreparationOutcome, PreparationScope, PreparationTrace
from chrys.service.trajectory.waits import WaitOutcome
from tests.service.trajectory._fakes import FakeSink, make_context
from tests.support.loaded_agents import install_loaded_agent, make_loaded_agent, reminder_resources
from tests.support.turn_services import make_turn_retry


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


class _FakeHookManager:
    def __init__(self) -> None:
        self.fired = False
        self.drained = False

    async def fire(self, *_args: object, **_kwargs: object) -> HookDecision:
        self.fired = True
        return HookDecision()

    async def drain_session(self, *, close: bool = True) -> None:
        self.drained = True


class _BareExecutor:
    def __init__(self) -> None:
        self.history_state: dict = {"messages": []}
        self.running = False
        self.closed = False

    async def close(self) -> None:
        self.closed = True

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


class _ActiveExecutor(_BareExecutor):
    active_handle = PassHandle("turn", "pass")

    def __init__(self, *, context: Any, run_release: asyncio.Event) -> None:
        super().__init__()
        self.running = True
        self.trajectory_context = context
        self._run_release = run_release
        self.injected: list[str] = []

    async def abort(self, handle: PassHandle, cause: AbortCause) -> AbortResult:
        assert handle is self.active_handle
        assert cause is AbortCause.OWNER_CLOSE
        self.running = False
        self._run_release.set()
        return AbortResult.REQUESTED

    def inject(self, text: str, **_kwargs: object) -> None:
        self.injected.append(text)

    def append_user_message(self, _text: str) -> None:
        return None

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


class _PreparationCheckingRecorder:
    def __init__(self, sink: FakeSink, *, expected_outcome: str) -> None:
        self.sink = sink
        self.expected_outcome = expected_outcome
        self._context = make_context(sink)
        self.closed = False

    def context(self):
        return self._context

    async def close(self, *, reason: str) -> None:
        assert self.sink.only(EventType.PREPARATION_FINISHED).payload["outcome"] == self.expected_outcome
        self.closed = True


class _PreAdmissionCheckingRecorder:
    def __init__(self, sink: FakeSink) -> None:
        self.sink = sink
        self._context = make_context(sink)
        self.closed = False

    def context(self):
        return self._context

    async def close(self, *, reason: str) -> None:
        assert self.sink.only(EventType.PREPARATION_FINISHED).payload["outcome"] == PreparationOutcome.OWNER_CHANGED
        assert WaitOutcome.CANCELLED in [
            draft.payload["outcome"] for draft in self.sink.of_type(EventType.WAIT_FINISHED)
        ]
        self.sink.assert_operations_settled()
        self.closed = True


class _InjectionHookCheckingRecorder:
    def __init__(self, sink: FakeSink) -> None:
        self.sink = sink
        self._context = make_context(sink)
        self.closed = False

    def context(self):
        return self._context

    async def close(self, *, reason: str) -> None:
        assert self.sink.only(EventType.PREPARATION_FINISHED).payload["outcome"] == PreparationOutcome.OWNER_CHANGED
        assert self.sink.of_type(EventType.HOOK_OPERATION_FINISHED) == []
        self._context.finalizers.close()
        assert self.sink.only(EventType.HOOK_OPERATION_FINISHED).payload["outcome"] == HookOutcome.ABANDONED
        self.sink.assert_operations_settled()
        self.closed = True


async def _drive_auto_approval(approval_middleware: ApprovalMiddleware) -> None:
    called = False

    async def _next() -> None:
        nonlocal called
        called = True

    await approval_middleware.process(_approval_context(), _next)
    assert called


@pytest.mark.asyncio
async def test_engine_shutdown_unsubscribes_approval_middleware() -> None:
    bus = EventBus()
    approval = ApprovalMiddleware(
        approval_policy=_approval_policy(),
        event_bus=bus,
        approval_mode=ApprovalMode.AUTO,
        approval_judge=_FakeJudge(),
    )
    await _drive_auto_approval(approval)
    assert approval._on_auto_fulfill_blocked in bus._handlers[ApprovalAutoFulfillBlocked]

    executor = _FakeExecutor(approval)
    engine = assemble_agent_engine(bus, settings=Settings(), initial_approval_mode=ApprovalMode.AUTO)
    install_loaded_agent(engine, bindings=executor)
    install_loaded_agent(engine, prepared=PreparedAgent())
    engine.current.loaded.prepared.own(executor.close)

    await engine.shutdown()

    assert executor.closed is True
    assert bus._handlers[ApprovalAutoFulfillBlocked] == []


@pytest.mark.asyncio
async def test_engine_shutdown_discards_closed_hook_manager_for_restart() -> None:
    bus = EventBus()
    engine = assemble_agent_engine(bus, settings=Settings())
    manager = _FakeHookManager()
    engine.session.hook_manager = manager

    await engine.shutdown()

    assert manager.fired is True
    assert manager.drained is True
    assert engine.session.hook_manager is None


@pytest.mark.asyncio
async def test_engine_shutdown_settles_pending_retry_before_trajectory_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = FakeSink()
    trace = PreparationTrace.open(
        scope=PreparationScope.PRE_TURN,
        phase="retry_admission",
        context=make_context(sink).with_turn(None).with_run(None),
    )
    assert trace is not None
    await trace.started()
    recorder = _PreparationCheckingRecorder(sink, expected_outcome=PreparationOutcome.DROPPED)
    monkeypatch.setattr("chrys.orchestration.engine.assembly.trajectory_recorder.TrajectoryRecorder", lambda: recorder)
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine.turns.turn_state.lease.pending_retry = PendingRetry(
        text="retry",
        session_generation=engine.session_generation,
        preparation_trace=trace,
    )

    await engine.shutdown()

    assert recorder.closed is True
    assert engine.turns.turn_state.lease.pending_retry == PendingRetry()
    sink.assert_operations_settled()


@pytest.mark.asyncio
async def test_engine_shutdown_settles_active_admission_before_trajectory_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = FakeSink()
    trace = PreparationTrace.open(
        scope=PreparationScope.PRE_TURN,
        phase="prompt_admission",
        context=make_context(sink).with_turn(None).with_run(None),
    )
    assert trace is not None
    await trace.started()
    recorder = _PreparationCheckingRecorder(sink, expected_outcome=PreparationOutcome.OWNER_CHANGED)
    monkeypatch.setattr("chrys.orchestration.engine.assembly.trajectory_recorder.TrajectoryRecorder", lambda: recorder)
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    admission = engine.turns.turn_state.lease.reserve_prompt_admission(
        kind="fresh",
        session_generation=engine.session_generation,
        build_generation=engine.build_generation,
        preparation_trace=trace,
    )
    assert admission is not None

    await engine.shutdown()

    assert recorder.closed is True
    assert engine.turns.turn_state.lease.active_admission_count() == 0
    sink.assert_operations_settled()


@pytest.mark.asyncio
async def test_engine_shutdown_cancel_fallback_settles_queued_injection_before_trajectory_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = FakeSink()
    trace = PreparationTrace.open(
        scope=PreparationScope.PRE_TURN,
        phase="injection_admission",
        context=make_context(sink),
    )
    assert trace is not None
    await trace.started()
    recorder = _PreparationCheckingRecorder(sink, expected_outcome=PreparationOutcome.TARGET_STALE)
    monkeypatch.setattr("chrys.orchestration.engine.assembly.trajectory_recorder.TrajectoryRecorder", lambda: recorder)
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    install_loaded_agent(engine, injection=InjectionMiddleware())
    target_turn_id = "b" * 32
    engine.current.loaded.injection.queue(
        "queued during shutdown",
        preparation=trace,
        target_turn_id=target_turn_id,
    )
    run_task = asyncio.create_task(asyncio.Event().wait())
    engine.turns.turn_state.lease.run_task = run_task
    monkeypatch.setattr("chrys.orchestration.engine.session_lifecycle._SHUTDOWN_POST_RUN_TIMEOUT_SECONDS", 0.01)

    injection = engine.current.loaded.injection
    await engine.shutdown()

    assert engine.turns.turn_state.shutdown_used_cancel_fallback is True
    assert run_task.cancelled()
    assert injection.drain_pending() == []
    assert engine.current.loaded is None
    assert recorder.closed is True
    assert sink.only(EventType.PREPARATION_FINISHED).payload["target_turn_id"] == target_turn_id
    sink.assert_operations_settled()


@pytest.mark.asyncio
async def test_session_transition_sweep_settles_blocked_user_message_before_trajectory_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = FakeSink()
    recorder = _PreAdmissionCheckingRecorder(sink)
    monkeypatch.setattr("chrys.orchestration.engine.assembly.trajectory_recorder.TrajectoryRecorder", lambda: recorder)
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    install_loaded_agent(engine, bindings=_BareExecutor())
    owner = "session:test-user-message:1"
    engine.turns.turn_state.lease.close_prompt_admission_for_rebuild(owner)
    gate_entered = asyncio.Event()
    wait_for_prompt_admission_open = engine.turns.turn_state.lease.wait_for_prompt_admission_open

    async def observed_prompt_gate() -> None:
        gate_entered.set()
        await wait_for_prompt_admission_open()

    engine.turns.turn_state.lease.wait_for_prompt_admission_open = observed_prompt_gate  # type: ignore[method-assign]
    task = asyncio.create_task(engine.turns.on_user_message(UserMessage(text="cross-session prompt")))
    try:
        await asyncio.wait_for(gate_entered.wait(), timeout=5.0)
        entry = next(iter(engine.turns.turn_state.lease.pre_admission_preparations.values()))
        assert entry.current_wait is not None

        engine.turns.turn_state.lease.invalidate_for_session_transition_pre_shutdown(
            old_session_generation=engine.session_generation,
            prompt_admission_owner=owner,
        )
        await engine.shutdown()

        assert recorder.closed is True
        assert entry.preparation.finished_state is True
        assert engine.turns.turn_state.lease.pre_admission_preparations == {}
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_session_transition_sweep_settles_blocked_user_retry_before_trajectory_close(
    monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    sink = FakeSink()
    recorder = _PreAdmissionCheckingRecorder(sink)
    monkeypatch.setattr("chrys.orchestration.engine.assembly.trajectory_recorder.TrajectoryRecorder", lambda: recorder)
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    install_loaded_agent(engine, bindings=_BareExecutor())
    owner = "session:test-user-retry:1"
    engine.turns.turn_state.lease.close_prompt_admission_for_rebuild(owner)
    gate_entered = asyncio.Event()
    wait_for_prompt_admission_open = engine.turns.turn_state.lease.wait_for_prompt_admission_open

    async def observed_prompt_gate() -> None:
        gate_entered.set()
        await wait_for_prompt_admission_open()

    engine.turns.turn_state.lease.wait_for_prompt_admission_open = observed_prompt_gate  # type: ignore[method-assign]
    task = asyncio.create_task(
        make_turn_retry(
            current=engine.current,
            session=engine.session,
            turn_state=engine.turns.turn_state,
            permits=engine.permits,
            loader=engine.loader,
            bus=engine.event_bus,
            fsm=engine_services(engine).fsm,
            history=engine_services(engine).history,
            trajectory_recorder=engine_services(engine).trajectory_recorder,
        ).handle_user_retry(UserRetry(text="cross-session retry"))
    )
    try:
        await asyncio.wait_for(gate_entered.wait(), timeout=5.0)
        entry = next(iter(engine.turns.turn_state.lease.pre_admission_preparations.values()))
        assert entry.current_wait is not None

        engine.turns.turn_state.lease.invalidate_for_session_transition_pre_shutdown(
            old_session_generation=engine.session_generation,
            prompt_admission_owner=owner,
        )
        await engine.shutdown()

        assert recorder.closed is True
        assert entry.preparation.finished_state is True
        assert engine.turns.turn_state.lease.pre_admission_preparations == {}
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_session_transition_sweep_settles_blocked_user_inject_before_trajectory_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sink = FakeSink()
    recorder = _PreAdmissionCheckingRecorder(sink)
    monkeypatch.setattr("chrys.orchestration.engine.assembly.trajectory_recorder.TrajectoryRecorder", lambda: recorder)
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    install_loaded_agent(engine, bindings=_BareExecutor())
    engine.permits.begin_agent_load()
    gate_entered = asyncio.Event()
    wait_for_agent_load_idle = engine.permits.wait_for_agent_load_idle

    async def observed_agent_load_gate() -> None:
        gate_entered.set()
        await wait_for_agent_load_idle()

    engine.permits.wait_for_agent_load_idle = observed_agent_load_gate  # type: ignore[method-assign]
    task = asyncio.create_task(
        engine.turns.on_user_inject(UserInject(text="cross-session injection", injection_id="inject-1"))
    )
    try:
        await asyncio.wait_for(gate_entered.wait(), timeout=5.0)
        entry = next(iter(engine.turns.turn_state.lease.pre_admission_preparations.values()))
        assert entry.current_wait is not None

        engine.turns.turn_state.lease.invalidate_for_session_transition_pre_shutdown(
            old_session_generation=engine.session_generation,
            prompt_admission_owner="session:test-user-inject:1",
        )

        assert recorder.closed is False
        assert sink.only(EventType.PREPARATION_FINISHED).payload["outcome"] == PreparationOutcome.OWNER_CHANGED
        assert sink.only(EventType.WAIT_FINISHED).payload["outcome"] == WaitOutcome.CANCELLED
        sink.assert_operations_settled()

        await engine.shutdown()

        assert recorder.closed is True
        assert engine.turns.turn_state.lease.pre_admission_preparations == {}
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_session_transition_settles_active_injection_and_blocking_hook_at_trajectory_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, engine_services
) -> None:
    sink = FakeSink()
    recorder = _InjectionHookCheckingRecorder(sink)
    hook = HookConfig(
        id="prompt-guard",
        event=HookEvent.USER_PROMPT_SUBMIT,
        run=HookRun(type="command", argv=["unused"]),
        execution=HookExecution(mode="blocking"),
    )
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    manager = HookManager(file=HooksFile(hooks=[hook]), hooks_dir=hooks_dir)
    hook_entered = asyncio.Event()
    hook_release = asyncio.Event()

    async def _run_and_wait(
        _hook: HookConfig,
        _payload: dict[str, Any],
        *,
        payload_text: str | None = None,
        result: HookResult | None = None,
    ) -> HookResult:
        hook_entered.set()
        await hook_release.wait()
        return HookResult(hook_id=hook.id, exit_code=0)

    manager._runner.run_and_wait = _run_and_wait  # type: ignore[method-assign]
    manager.trajectory_context_provider = recorder.context

    monkeypatch.setattr("chrys.orchestration.engine.assembly.trajectory_recorder.TrajectoryRecorder", lambda: recorder)
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine.session.hook_manager = manager
    install_loaded_agent(engine, **reminder_resources())
    run_release = asyncio.Event()
    executor = _ActiveExecutor(context=recorder.context(), run_release=run_release)
    install_loaded_agent(engine, bindings=cast(Any, executor))
    engine_services(engine).fsm.transition(Trigger.START)
    engine_services(engine).fsm.transition(Trigger.USER_MESSAGE)
    scope = engine.turns.turn_state.lease.begin_current_run_scope(
        owner_admission_id=1,
        session_generation=engine.session_generation,
        build_generation=engine.build_generation,
        reminder_scope=engine.current.loaded.reminder_middleware.create_current_run_scope(),
    )
    engine.turns.turn_state.lease.open_injection_admission(scope)
    run_task = asyncio.create_task(run_release.wait())
    engine.turns.turn_state.lease.run_task = run_task
    injection_task = asyncio.create_task(
        engine.turns.on_user_inject(UserInject(text="cross-session injection", injection_id="inject-1"))
    )
    try:
        await asyncio.wait_for(hook_entered.wait(), timeout=5.0)
        entry = next(iter(engine.turns.turn_state.lease.pre_admission_preparations.values()))
        preparation_started = sink.only(EventType.PREPARATION_STARTED)
        hook_started = sink.only(EventType.HOOK_OPERATION_STARTED)
        assert executor.trajectory_context.turn_id is not None
        assert preparation_started.turn_id is None
        assert hook_started.turn_id is None
        assert hook_started.parent_operation_id == entry.preparation.operation_id
        assert hook_started.payload["target_operation_id"] == entry.preparation.operation_id

        engine.turns.turn_state.lease.invalidate_for_session_transition_pre_shutdown(
            old_session_generation=engine.session_generation,
            prompt_admission_owner="session:test-active-injection-hook:1",
        )

        preparation_finished = sink.only(EventType.PREPARATION_FINISHED)
        assert preparation_finished.turn_id is None
        assert preparation_finished.payload["outcome"] == PreparationOutcome.OWNER_CHANGED
        assert sink.of_type(EventType.HOOK_OPERATION_FINISHED) == []

        await engine.shutdown()

        assert recorder.closed is True
        hook_finished = sink.only(EventType.HOOK_OPERATION_FINISHED)
        assert hook_finished.turn_id is None
        assert hook_finished.parent_operation_id == entry.preparation.operation_id
        assert hook_finished.payload["outcome"] == HookOutcome.ABANDONED
        sink.assert_operations_settled()
        event_count_at_close = len(sink.drafts)

        hook_release.set()
        await asyncio.wait_for(injection_task, timeout=5.0)

        assert len(sink.drafts) == event_count_at_close
        assert len(sink.of_type(EventType.HOOK_OPERATION_FINISHED)) == 1
        assert executor.injected == []
    finally:
        hook_release.set()
        run_release.set()
        if not injection_task.done():
            injection_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await injection_task
        if not run_task.done():
            run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task


@pytest.mark.asyncio
async def test_session_transition_sweep_settles_retry_post_admission_wait_before_trajectory_close(
    monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    sink = FakeSink()
    recorder = _PreAdmissionCheckingRecorder(sink)
    monkeypatch.setattr("chrys.orchestration.engine.assembly.trajectory_recorder.TrajectoryRecorder", lambda: recorder)
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    install_loaded_agent(engine, bindings=_BareExecutor())
    engine_services(engine).history.bind({"messages": [Message("user", ["original request"])]})
    engine_services(engine).fsm.transition(Trigger.START)
    engine_services(engine).fsm.transition(Trigger.RESTORE_INTERRUPTED)
    existing_run_task = asyncio.create_task(asyncio.Event().wait())
    engine.turns.turn_state.lease.run_task = existing_run_task
    wait_entered = asyncio.Event()
    coordinator = make_turn_retry(
        current=engine.current,
        session=engine.session,
        turn_state=engine.turns.turn_state,
        permits=engine.permits,
        loader=engine.loader,
        bus=engine.event_bus,
        fsm=engine_services(engine).fsm,
        history=engine_services(engine).history,
        trajectory_recorder=engine_services(engine).trajectory_recorder,
    )

    async def observed_existing_run_task_wait() -> None:
        wait_entered.set()
        await existing_run_task

    coordinator._wait_for_existing_run_task = observed_existing_run_task_wait  # type: ignore[method-assign]
    retry_task = asyncio.create_task(coordinator.handle_user_retry(UserRetry()))
    try:
        await asyncio.wait_for(wait_entered.wait(), timeout=5.0)
        entry = next(iter(engine.turns.turn_state.lease.pre_admission_preparations.values()))
        admission = next(iter(engine.turns.turn_state.lease.active_admissions.values()))
        assert entry.preparation is admission.preparation_trace
        assert entry.current_wait is not None
        wait_operation_id = entry.current_wait.operation_id

        engine.turns.turn_state.lease.invalidate_for_session_transition_pre_shutdown(
            old_session_generation=engine.session_generation,
            prompt_admission_owner="session:test-retry-post-admission:1",
        )

        assert recorder.closed is False
        assert sink.only(EventType.PREPARATION_FINISHED).payload["outcome"] == PreparationOutcome.OWNER_CHANGED
        wait_finished = next(
            draft for draft in sink.of_type(EventType.WAIT_FINISHED) if draft.operation_id == wait_operation_id
        )
        assert wait_finished.payload["outcome"] == WaitOutcome.CANCELLED
        sink.assert_operations_settled()

        existing_run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await existing_run_task
        with contextlib.suppress(asyncio.CancelledError):
            await retry_task
        await engine.shutdown()

        assert recorder.closed is True
        assert engine.turns.turn_state.lease.active_admission_count() == 0
        assert engine.turns.turn_state.lease.pre_admission_preparations == {}
    finally:
        if not existing_run_task.done():
            existing_run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await existing_run_task
        if not retry_task.done():
            retry_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await retry_task


@pytest.mark.asyncio
async def test_start_without_hooks_config_does_not_create_hook_runtime_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agent_engine, *, engine_services
) -> None:
    import chrys.foundation.platform as platform_mod
    from chrys.foundation.models.workspace import Workspace

    bus = EventBus()
    engine = agent_engine(bus, settings=Settings())
    engine.session.workspace = Workspace(primary_cwd=str(tmp_path))

    async def _build_agent(_profile: AgentProfile, staged: StagedBuild) -> CompletedBuild:
        return CompletedBuild(
            staged=staged,
            settings=engine.settings_handle.prepare(staged.loaded),
            workspace_retarget=engine_services(engine).workspace_change_tracker.resolve_retarget(
                staged.workspace,
                resolve_scope=engine.settings_handle.prepare(staged.loaded).effective.settings.workspace_change_notice,
            ),
            loaded=make_loaded_agent(bindings=_BareExecutor()),
            manifest=engine.current.manifest,
            mutation_tracker=None,
            todo_tracker=None,
            compaction_strategy=None,
        )

    monkeypatch.setattr(platform_mod, "get_platform", lambda: SimpleNamespace(config_dir=tmp_path))
    monkeypatch.setattr(engine.loader, "build", _build_agent)

    try:
        await engine.start(AgentProfile(name="Code"))
        assert engine.session.hook_manager is None
        assert engine.session.outbox_recovery_task is None
        assert not (tmp_path / "hooks").exists()
    finally:
        await engine.shutdown()


async def test_new_session_reuses_mcp_cache_and_final_shutdown_closes_it(tmp_path: Path, agent_engine) -> None:
    """``/new`` should release leases but keep the engine-owned MCP cache warm."""
    profile = AgentProfile(
        name="Code",
        instructions="Code instructions.",
        tools=ToolsConfig(mcp=[MCPServerConfig(name="srv", transport="stdio", command="python")]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )
    bus = EventBus()
    store = JsonFileStateStore(tmp_path)
    engine = agent_engine(bus, settings=Settings(), state_store=store)

    async def _remote(ctx, value: str = "") -> str:
        return value or "ok"

    remote_tool = FunctionTool(
        func=_remote,
        name="remote",
        input_model={"type": "object", "properties": {"value": {"type": "string"}}},
    )

    class _FakeMCPTool:
        def __init__(self) -> None:
            self.functions = [remote_tool]
            self.request_timeout = 30
            self.dropped_banner_lines: list[str] = []
            self.exit_count = 0

        async def __aenter__(self) -> _FakeMCPTool:
            return self

        async def __aexit__(self, *_args: object) -> None:
            self.exit_count += 1

    fake_mcp = _FakeMCPTool()
    fake_ctx = MagicMock()
    fake_ctx.providers = []
    fake_ctx.middleware = []
    fake_ctx.compaction_strategy = MagicMock()
    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    agent_mock.create_session = MagicMock(return_value=MagicMock())
    executor_mock = MagicMock()
    executor_mock.state.running = False
    executor_mock.backend.history_state = {}
    executor_mock.close = AsyncMock()

    with (
        patch.object(runtime_factory_module, "Agent", return_value=agent_mock),
        patch.object(runtime_factory_module, "ContextManager", return_value=fake_ctx),
        patch.object(agent_builder_module, "create_client", return_value=MagicMock(aclose=AsyncMock())),
        patch.object(
            agent_builder_module,
            "resolve_selection_for_agent",
            return_value=ModelSelection(ModelProfile(id="test-id", name="test-model"), "active"),
        ),
        patch.object(agent_builder_module, "effective_chat_options", return_value={}),
        patch.object(agent_builder_module, "LoopRecorder", return_value=MagicMock()),
        patch.object(runtime_factory_module, "SystemReminderMiddleware", return_value=MagicMock()),
        patch.object(runtime_factory_module, "LastWordsGenerator", return_value=MagicMock(aclose=AsyncMock())),
        patch.object(agent_builder_module, "TurnBindings", return_value=executor_mock),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(agent_builder_module, "ApprovalPolicy", return_value=MagicMock()),
        patch("chrys.service.tools.packs.create_packs", return_value=[]),
        patch("chrys.service.skills.adapter.create_skills_provider", new=AsyncMock(return_value=(None, []))),
        patch("chrys.service.mcp._connection._create_mcp_tool", return_value=fake_mcp) as factory,
    ):
        await engine.start(profile)
        await engine._on_new_session(SessionNew())
        assert factory.call_count == 1
        assert fake_mcp.exit_count == 0

        await engine.shutdown()

    assert fake_mcp.exit_count == 1
