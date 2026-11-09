# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for cancelling a queued mid-run user injection (Esc flow).

Covers both pending phases of an active-turn injection:

- **Admission phase** — the injection is still inside awaited hook/skill
  admission work; a cancel mark recorded on ``TurnRuntimeState`` must abort
  the commit before the text is queued or any reminders are applied.
- **Queued phase** — the injection already sits on the middleware queue and
  in the approval judge context; ``on_user_inject_cancel`` must remove both
  and publish an abandoned :class:`UserInjectResult` carrying the id.

Plus the fresh-turn hole: a locked-input submit that outlives its run must
not start a new turn when the user already cancelled it.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentRuntimeDetails,
    RuntimeModelDetails,
    UserInject,
    UserInjectCancel,
    UserInjectResult,
    UserMessage,
)
from chrys.orchestration.engine.run.active_injection import ActiveTurnInjector
from chrys.orchestration.engine.state.machine import EngineState, Trigger
from chrys.orchestration.engine.trajectory import TrajectoryRecorder
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.agent_middleware.system_reminder import CurrentRunReminderScope, CurrentRunReminderTarget
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.schema import HookDecision
from chrys.service.session.history import FreshTurnCleanup
from chrys.service.trajectory.preparation import PreparationOutcome, PreparationTrace
from tests.service.trajectory._fakes import FakeSink, make_context
from tests.support.components import make_current, make_turn_state
from tests.support.loaded_agents import SkillRefreshLoader, install_loaded_agent, make_manifest
from tests.support.turn_services import make_turn_coordinator


class _PromptHookManager:
    def __init__(self, decision: HookDecision | None = None) -> None:
        self.decision = decision or HookDecision()
        self.payloads: list[dict[str, object]] = []

    def has_hooks_for(self, event: HookEvent) -> bool:
        return event == HookEvent.USER_PROMPT_SUBMIT

    async def fire(self, _event: HookEvent, payload: dict[str, object], **_kwargs: object) -> HookDecision:
        self.payloads.append(payload)
        return self.decision


class _BlockingPromptHookManager(_PromptHookManager):
    """Parks the first hook call so a cancel can interleave mid-admission."""

    def __init__(self, decision: HookDecision | None = None) -> None:
        super().__init__(decision)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def fire(self, _event: HookEvent, payload: dict[str, object], **_kwargs: object) -> HookDecision:
        self.payloads.append(payload)
        self.entered.set()
        await self.release.wait()
        return self.decision


class _Executor:
    """TurnBindings fake backed by a real InjectionMiddleware queue."""

    def __init__(self, *, running: bool = False) -> None:
        self.running = running
        self.trajectory_context = None
        self.injection = InjectionMiddleware()
        self.approval_context: list[str] = []
        self.injected_reminders: list[tuple[str, ...]] = []

    def inject(
        self,
        text: str,
        created_at: object = None,
        injection_id: str | None = None,
        reminders: tuple[str, ...] = (),
        preparation: PreparationTrace | None = None,
        target_turn_id: str | None = None,
    ) -> None:
        self.injected_reminders.append(reminders)
        self.injection.queue(
            text,
            created_at=created_at,
            injection_id=injection_id,
            reminders=reminders,
            preparation=preparation,
            target_turn_id=target_turn_id,
        )

    def cancel_injection(self, injection_id: str):
        return self.injection.cancel(injection_id)

    def append_user_message(self, text: str) -> None:
        self.approval_context.append(text)

    def remove_user_message(self, text: str) -> None:
        if text in self.approval_context:
            self.approval_context.remove(text)

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


class _Fsm:
    def __init__(self, *, state: EngineState) -> None:
        self.state = state
        self.transitions: list[Trigger] = []

    def is_running(self) -> bool:
        return self.state in (EngineState.RUNNING, EngineState.PENDING_RETRY, EngineState.AWAITING_SUB_AGENTS)

    def try_transition(self, trigger: Trigger) -> None:
        self.transitions.append(trigger)


class _History:
    def __init__(self) -> None:
        self.messages: list[object] = []

    def has_trailing_error_markers(self) -> bool:
        return False

    def remove_trailing_markers(self) -> None:
        return None

    def prepare_fresh_turn(self, *, other_records_present: bool = False) -> FreshTurnCleanup:
        return FreshTurnCleanup()


class _Reminder:
    def __init__(self) -> None:
        self.queued: list[tuple[list[str], bool]] = []
        self._next_scope_id = 1
        self._scopes: set[CurrentRunReminderScope] = set()

    def create_current_run_scope(self) -> CurrentRunReminderScope:
        scope = CurrentRunReminderScope(self._next_scope_id)
        self._next_scope_id += 1
        self._scopes.add(scope)
        return scope

    def capture_current_run_target(self, reminder_scope: CurrentRunReminderScope) -> CurrentRunReminderTarget | None:
        if reminder_scope not in self._scopes:
            return None
        return CurrentRunReminderTarget(reminder_scope, "pre_prepare", 0)

    def is_current_run_target_valid(self, _target: CurrentRunReminderTarget) -> bool:
        return True

    def set_skill_catalog_for_current_run(
        self,
        _target: CurrentRunReminderTarget,
        _skill_catalog: str | None,
    ) -> bool:
        return True

    def queue_drained_injection_reminders(self, reminders: list[str]) -> None:
        self.queued.append((reminders, False))

    def expire_current_run_scope(self, reminder_scope: CurrentRunReminderScope) -> None:
        self._scopes.discard(reminder_scope)


class _HostPermits:
    async def wait_for_agent_load_idle(self) -> None:
        return None


class _Host:
    def __init__(self, *, executor_running: bool = False) -> None:
        self.current = make_current(loaded=SimpleNamespace(), manifest=make_manifest())
        self._current = self.current
        self.permits = _HostPermits()
        self.session = SimpleNamespace()
        self.permits.agent_loading = False
        self._bus = EventBus()
        self.session.session_id = "s1"
        install_loaded_agent(self, bindings=_Executor(running=executor_running))
        self._fsm = _Fsm(state=EngineState.RUNNING if executor_running else EngineState.IDLE)
        self._turn_state = make_turn_state()
        self._history = _History()
        install_loaded_agent(self, reminder_middleware=_Reminder())
        install_loaded_agent(self, runtime_details=AgentRuntimeDetails(model=RuntimeModelDetails(vision=True)))
        install_loaded_agent(self, skills_provider=None)
        install_loaded_agent(self, tool_names=[])
        install_loaded_agent(self, skill_names=[])
        install_loaded_agent(self, memory_files=[])
        self.session.workspace = None
        self.session.hook_manager = _PromptHookManager()
        self.session.agent_profile = None
        self._trajectory_recorder = TrajectoryRecorder()
        self.session.shutting_down = False
        self.session.turn_number = 1
        self.permits.session_generation = 0
        self.permits.build_generation = 0
        self.run_texts: list[str] = []
        self.loader = SkillRefreshLoader(self.current)

    async def run_and_save(self, text: str, **_kwargs: object) -> None:
        self.run_texts.append(text)


async def _never_finishes() -> None:
    await asyncio.Event().wait()


def _install_active_run(host: _Host) -> None:
    scope = host._turn_state.lease.begin_current_run_scope(
        owner_admission_id=1,
        session_generation=host.permits.session_generation,
        build_generation=host.permits.build_generation,
        reminder_scope=host.current.loaded.reminder_middleware.create_current_run_scope(),
    )
    host._turn_state.lease.open_injection_admission(scope)
    host._turn_state.lease.run_task = asyncio.create_task(_never_finishes())


async def _cancel_active_run(host: _Host) -> None:
    task = host._turn_state.lease.run_task
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _collect_inject_results(bus: EventBus) -> list[UserInjectResult]:
    results: list[UserInjectResult] = []

    async def _on_result(event: UserInjectResult) -> None:
        results.append(event)

    await bus.subscribe(UserInjectResult, _on_result)
    return results


async def on_user_message(host: _Host, event: UserMessage) -> None:
    await make_turn_coordinator(
        current=host.current,
        session=host.session,
        turn_state=host._turn_state,
        permits=host.permits,
        loader=host.loader,
        bus=host._bus,
        fsm=host._fsm,
        history=host._history,
        trajectory_recorder=host._trajectory_recorder,
        run_and_save=host.run_and_save,
    ).on_user_message(event)  # type: ignore[arg-type]


async def on_user_inject(host: _Host, event: UserInject) -> None:
    await make_turn_coordinator(
        current=host.current,
        session=host.session,
        turn_state=host._turn_state,
        permits=host.permits,
        loader=host.loader,
        bus=host._bus,
        fsm=host._fsm,
        history=host._history,
        trajectory_recorder=host._trajectory_recorder,
        run_and_save=host.run_and_save,
    ).on_user_inject(event)  # type: ignore[arg-type]


async def on_user_inject_cancel(host: _Host, event: UserInjectCancel) -> None:
    await make_turn_coordinator(
        current=host.current,
        session=host.session,
        turn_state=host._turn_state,
        permits=host.permits,
        loader=host.loader,
        bus=host._bus,
        fsm=host._fsm,
        history=host._history,
        trajectory_recorder=host._trajectory_recorder,
        run_and_save=host.run_and_save,
    ).on_user_inject_cancel(event)  # type: ignore[arg-type]


class _TrajectoryRecorderWithContext:
    def __init__(self, context: object) -> None:
        self._context = context

    def context(self) -> object:
        return self._context


@pytest.mark.asyncio
async def test_cancel_removes_queued_injection_and_approval_context() -> None:
    """A queued injection is withdrawn from the middleware and judge context."""
    host = _Host(executor_running=True)
    host.session.hook_manager = _PromptHookManager(HookDecision(system_reminders=["hook context"]))
    _install_active_run(host)
    results = await _collect_inject_results(host._bus)

    try:
        event = UserMessage(text="change of plans", injection_id="inj-1")
        await on_user_message(host, event)
        # Queued into the live run: the frontend keeps showing it as pending.
        assert event.started_turn is False
        assert host.current.loaded.bindings.approval_context == ["change of plans"]
        # The reminders travel with the queued text until a call drains it.
        assert host.current.loaded.reminder_middleware.queued == []
        assert host.current.loaded.bindings.injected_reminders == [("hook context",)]

        await on_user_inject_cancel(host, UserInjectCancel(injection_id="inj-1"))
    finally:
        await _cancel_active_run(host)

    assert host.current.loaded.bindings.injection.drain_pending() == []
    assert host.current.loaded.bindings.approval_context == []
    # The reminders leave with the text: none was ever queued for a call.
    assert host.current.loaded.reminder_middleware.queued == []
    assert [(r.consumed, r.injection_id, r.text) for r in results] == [(False, "inj-1", "change of plans")]
    # The queued-phase removal resolves the cancel mark immediately.
    assert host._turn_state.cancelled_injection_ids == set()


@pytest.mark.parametrize("acp_path", [False, True], ids=("tui", "acp"))
@pytest.mark.asyncio
async def test_queued_injection_preparation_closes_as_cancelled_without_waiting_for_ack(
    acp_path: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both input routes transfer the open trace to the cancellable queue item."""
    host = _Host(executor_running=True)
    sink = FakeSink()
    host._trajectory_recorder = _TrajectoryRecorderWithContext(make_context(sink))  # type: ignore[assignment]
    _install_active_run(host)

    try:
        if acp_path:
            await on_user_inject(host, UserInject(text="change of plans", injection_id="inj-1"))
        else:
            await on_user_message(host, UserMessage(text="change of plans", injection_id="inj-1"))

        assert sink.of_type("preparation.finished") == []

        async def blocked_finished(
            _trace: PreparationTrace,
            *,
            outcome: str,
            target_turn_id: str | None = None,
        ) -> None:
            del outcome, target_turn_id
            await asyncio.Event().wait()

        monkeypatch.setattr(PreparationTrace, "finished", blocked_finished)
        await asyncio.wait_for(
            on_user_inject_cancel(host, UserInjectCancel(injection_id="inj-1")),
            timeout=1.0,
        )
    finally:
        await _cancel_active_run(host)

    terminal = sink.only("preparation.finished")
    assert terminal.payload["outcome"] == PreparationOutcome.CANCELLED


@pytest.mark.parametrize("acp_path", [False, True], ids=("tui", "acp"))
@pytest.mark.asyncio
async def test_post_queue_exception_preserves_preparation_handoff(acp_path: bool) -> None:
    """A synchronous failure after queueing cannot reclaim the trace."""
    host = _Host(executor_running=True)
    sink = FakeSink()
    host._trajectory_recorder = _TrajectoryRecorderWithContext(make_context(sink))  # type: ignore[assignment]
    _install_active_run(host)

    def fail_after_queue(_text: str) -> None:
        raise RuntimeError("approval context failure")

    host.current.loaded.bindings.approval.append_user_message = fail_after_queue  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="approval context failure"):
            if acp_path:
                await on_user_inject(host, UserInject(text="queued first", injection_id="inj-1"))
            else:
                await on_user_message(host, UserMessage(text="queued first", injection_id="inj-1"))

        queued = host.current.loaded.bindings.injection.drain_pending()
        assert len(queued) == 1
        assert sink.of_type("preparation.finished") == []
        assert queued[0].preparation is not None
        await queued[0].preparation.finished(outcome=PreparationOutcome.INJECTED)
    finally:
        await _cancel_active_run(host)

    assert sink.only("preparation.finished").payload["outcome"] == PreparationOutcome.INJECTED


@pytest.mark.asyncio
async def test_post_queue_cancellation_preserves_preparation_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancellation during post-queue publishing leaves the queued item in charge."""
    host = _Host(executor_running=True)
    sink = FakeSink()
    host._trajectory_recorder = _TrajectoryRecorderWithContext(make_context(sink))  # type: ignore[assignment]
    _install_active_run(host)
    publish_entered = asyncio.Event()

    async def block_runtime_skill_publish(
        _injector: ActiveTurnInjector,
        _committed: object,
        *,
        session_id: str | None,
    ) -> None:
        del session_id
        publish_entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ActiveTurnInjector, "_publish_staged_runtime_skill_refresh", block_runtime_skill_publish)
    task = asyncio.create_task(on_user_inject(host, UserInject(text="queued first", injection_id="inj-1")))
    try:
        await asyncio.wait_for(publish_entered.wait(), timeout=5.0)
        assert host._turn_state.lease.pre_admission_preparations == {}
        host._turn_state.lease.clear_pre_admission_preparations()
        assert sink.of_type("preparation.finished") == []
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        queued = host.current.loaded.bindings.injection.drain_pending()
        assert len(queued) == 1
        assert sink.of_type("preparation.finished") == []
        assert queued[0].preparation is not None
        await queued[0].preparation.finished(outcome=PreparationOutcome.INJECTED)
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await _cancel_active_run(host)

    assert sink.only("preparation.finished").payload["outcome"] == PreparationOutcome.INJECTED


@pytest.mark.asyncio
async def test_cancel_preserves_other_queued_injections() -> None:
    """Cancelling one injection leaves other queued injections untouched."""
    host = _Host(executor_running=True)
    _install_active_run(host)

    try:
        await on_user_message(host, UserMessage(text="first", injection_id="inj-1"))
        await on_user_message(host, UserMessage(text="second", injection_id="inj-2"))

        await on_user_inject_cancel(host, UserInjectCancel(injection_id="inj-1"))
    finally:
        await _cancel_active_run(host)

    assert [q.text for q in host.current.loaded.bindings.injection.drain_pending()] == ["second"]
    assert host.current.loaded.bindings.approval_context == ["second"]


@pytest.mark.asyncio
async def test_cancel_during_admission_aborts_before_commit() -> None:
    """A cancel landing mid-admission prevents queueing, reminders, and context."""
    host = _Host(executor_running=True)
    host.session.hook_manager = _BlockingPromptHookManager(HookDecision(system_reminders=["from hook"]))
    _install_active_run(host)
    results = await _collect_inject_results(host._bus)

    task = asyncio.create_task(on_user_message(host, UserMessage(text="withdraw me", injection_id="inj-1")))
    try:
        await asyncio.wait_for(host.session.hook_manager.entered.wait(), timeout=5.0)
        await on_user_inject_cancel(host, UserInjectCancel(injection_id="inj-1"))
        host.session.hook_manager.release.set()
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        host.session.hook_manager.release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await _cancel_active_run(host)

    assert host.current.loaded.bindings.injection.drain_pending() == []
    assert host.current.loaded.bindings.approval_context == []
    assert host.current.loaded.reminder_middleware.queued == []
    assert [(r.consumed, r.injection_id) for r in results] == [(False, "inj-1")]
    # The admission observed and consumed the mark on its way out.
    assert host._turn_state.cancelled_injection_ids == set()


@pytest.mark.asyncio
async def test_cancel_after_consumption_is_noop() -> None:
    """A cancel that loses the race to the model call changes nothing."""
    host = _Host(executor_running=True)
    _install_active_run(host)
    results = await _collect_inject_results(host._bus)

    try:
        await on_user_message(host, UserMessage(text="too late", injection_id="inj-1"))
        # Simulate the model call consuming the queue before the cancel lands.
        consumed = host.current.loaded.bindings.injection.drain_pending()
        assert [q.text for q in consumed] == ["too late"]

        await on_user_inject_cancel(host, UserInjectCancel(injection_id="inj-1"))
    finally:
        await _cancel_active_run(host)

    # No abandoned result — the consumed=True result owns this id's outcome —
    # and the approval context keeps the text the model actually saw. The
    # unmatched cancel must not leave a mark dangling for the session either.
    assert results == []
    assert host.current.loaded.bindings.approval_context == ["too late"]
    assert host._turn_state.cancelled_injection_ids == set()


@pytest.mark.asyncio
async def test_cancel_for_unknown_id_leaves_no_mark() -> None:
    """A cancel matching neither queue nor in-flight admission records nothing."""
    host = _Host(executor_running=True)
    _install_active_run(host)
    results = await _collect_inject_results(host._bus)

    try:
        await on_user_inject_cancel(host, UserInjectCancel(injection_id="never-seen"))
    finally:
        await _cancel_active_run(host)

    assert results == []
    assert host._turn_state.cancelled_injection_ids == set()


@pytest.mark.asyncio
async def test_cancel_without_executor_leaves_no_mark() -> None:
    """A cancel arriving before the engine has an executor records nothing."""
    host = _Host(executor_running=False)
    install_loaded_agent(host, loaded=None)

    await on_user_inject_cancel(host, UserInjectCancel(injection_id="inj-1"))

    assert host._turn_state.cancelled_injection_ids == set()


def test_inflight_injection_watch_drops_unobserved_mark_on_last_exit() -> None:
    """The last admission out takes an unobserved cancel mark with it."""
    state = make_turn_state()
    state.begin_inflight_injection("inj-1")
    state.begin_inflight_injection("inj-1")
    state.mark_injection_cancelled("inj-1")

    state.finish_inflight_injection("inj-1")
    assert state.is_injection_cancelled("inj-1")
    assert state.is_injection_inflight("inj-1")

    state.finish_inflight_injection("inj-1")
    assert not state.is_injection_cancelled("inj-1")
    assert not state.is_injection_inflight("inj-1")
    # Id-less submits are ignored by the watch.
    state.begin_inflight_injection(None)
    state.finish_inflight_injection(None)
    assert state.inflight_injection_ids == {}


@pytest.mark.asyncio
async def test_cancelled_locked_submit_does_not_start_fresh_turn() -> None:
    """A cancelled injection submit arriving after run end never starts a turn."""
    host = _Host(executor_running=False)
    results = await _collect_inject_results(host._bus)

    # Equivalent to a cancel landing while this submit was parked in the
    # admission waits (in flight, so the cancel handler recorded a mark) and
    # the run finishing before the submit resumed on the fresh-turn path.
    host._turn_state.mark_injection_cancelled("inj-1")
    event = UserMessage(text="withdrawn", injection_id="inj-1")
    await on_user_message(host, event)

    assert event.started_turn is False
    assert host.run_texts == []
    assert host._fsm.transitions == []
    assert [(r.consumed, r.injection_id, r.text) for r in results] == [(False, "inj-1", "withdrawn")]
    assert host._turn_state.cancelled_injection_ids == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("reminders", [True, False], ids=["reminder_scope", "no_reminder_middleware"])
async def test_uncancelled_submit_with_injection_id_still_starts_fresh_turn(reminders: bool) -> None:
    """The injection id alone must not stop a legitimate fresh-turn promotion."""
    host = _Host(executor_running=False)
    if not reminders:
        install_loaded_agent(host, reminder_middleware=None)

    event = UserMessage(text="still wanted", injection_id="inj-1")
    await on_user_message(host, event)
    # Marked before the publish returns, so the frontend shows a new turn
    # rather than an injection still waiting for its run — and returned
    # before the run's task could start, so its first events find that turn.
    assert event.started_turn is True
    assert host.run_texts == []
    task = host._turn_state.lease.run_task
    assert task is not None
    await task

    assert host.run_texts == ["still wanted"]
    assert host._fsm.transitions == [Trigger.USER_MESSAGE]
