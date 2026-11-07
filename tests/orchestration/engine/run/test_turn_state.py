# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for turn-runtime state scaffolding."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from chrys.orchestration.engine.execution import PendingRetry, RetryInput
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.engine.run.turn_state import ActiveInjectionTarget
from chrys.orchestration.invoker.kernel import KernelConversation
from chrys.service.agent_middleware.system_reminder import (
    CurrentRunReminderScope,
    CurrentRunReminderTarget,
    SystemReminderMiddleware,
)
from chrys.service.trajectory.preparation import PreparationOutcome, PreparationScope, PreparationTrace
from tests.service.trajectory._fakes import FakeSink, make_context
from tests.support.components import make_permits, make_session, make_turn_state


def test_prompt_admission_reserve_release_is_exact() -> None:
    state = make_turn_state()

    first = state.lease.reserve_prompt_admission(kind="fresh", session_generation=2, build_generation=3)
    second = state.lease.reserve_prompt_admission(kind="retry", session_generation=2, build_generation=3)

    assert first is not None
    assert second is not None
    assert first.admission_id == 1
    assert second.admission_id == 2
    assert state.lease.active_admission_count() == 2

    assert state.lease.release_prompt_admission(first) is True
    assert state.lease.release_prompt_admission(first) is False
    assert state.lease.active_admission_count() == 1

    state.lease.close_prompt_admission_for_rebuild()
    assert state.lease.reserve_prompt_admission(kind="fresh", session_generation=2, build_generation=3) is None
    state.lease.reopen_prompt_admission_after_rebuild()
    assert state.lease.reserve_prompt_admission(kind="fresh", session_generation=2, build_generation=3) is not None


def test_pending_retry_records_owner_and_dispatch_invalidation() -> None:
    state = make_turn_state()
    admission = state.lease.reserve_prompt_admission(kind="retry", session_generation=4, build_generation=5)
    assert admission is not None

    state.lease.begin_current_run_scope(
        owner_admission_id=admission.admission_id,
        session_generation=4,
        build_generation=5,
        reminder_scope=CurrentRunReminderScope(1),
    )

    assert state.lease.upsert_pending_retry_from_admission(admission, RetryInput("first", "t1")) is False
    assert state.lease.pending_retry.retry.text == "first"
    assert state.lease.pending_retry.retry.created_at == "t1"
    assert state.lease.pending_retry.owner_admission_id == admission.admission_id
    assert state.lease.pending_retry.updated_by_admission_id == admission.admission_id

    assert state.lease.upsert_pending_retry_from_admission(admission, RetryInput("latest", "t2")) is True
    assert state.lease.pending_retry.retry.text == "latest"
    assert state.lease.pending_retry.retry.created_at == "t2"

    state.lease.disable_pending_retry_dispatch_for_session_transition(4)
    assert state.lease.pending_retry.dispatch_disabled is True
    assert state.lease.pending_retry_dispatch_disabled_for_session_generation == 4


@pytest.mark.asyncio
async def test_pending_retry_requeue_settles_replaced_and_cleared_preparations() -> None:
    sink = FakeSink()
    state = make_turn_state()
    state.lease.begin_current_run_scope(
        owner_admission_id=1,
        session_generation=4,
        build_generation=5,
        reminder_scope=CurrentRunReminderScope(1),
    )

    first_trace = PreparationTrace.open(
        scope=PreparationScope.PRE_TURN,
        phase="retry_admission",
        context=make_context(sink).with_turn(None).with_run(None),
    )
    second_trace = PreparationTrace.open(
        scope=PreparationScope.PRE_TURN,
        phase="retry_admission",
        context=make_context(sink).with_turn(None).with_run(None),
    )
    assert first_trace is not None
    assert second_trace is not None
    await first_trace.started()
    await second_trace.started()
    first = state.lease.reserve_prompt_admission(
        kind="retry",
        session_generation=4,
        build_generation=5,
        preparation_trace=first_trace,
    )
    second = state.lease.reserve_prompt_admission(
        kind="retry",
        session_generation=4,
        build_generation=5,
        preparation_trace=second_trace,
    )
    assert first is not None
    assert second is not None

    assert state.lease.upsert_pending_retry_from_admission(first, RetryInput("first", "t1")) is False
    assert state.lease.upsert_pending_retry_from_admission(second, RetryInput("second", "t2")) is True
    cleared = state.lease.clear_pending_retry(outcome=PreparationOutcome.DROPPED)

    assert cleared.preparation_trace is second_trace
    assert [draft.payload["state"] for draft in sink.of_type("preparation.state")] == ["queued", "requeued"]
    assert [draft.payload["outcome"] for draft in sink.of_type("preparation.finished")] == [
        PreparationOutcome.SUPERSEDED,
        PreparationOutcome.DROPPED,
    ]
    sink.assert_operations_settled()


async def _never_finishes() -> None:
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_injection_window_and_commit_validation() -> None:
    state = make_turn_state()
    scope = state.lease.begin_current_run_scope(
        owner_admission_id=1,
        session_generation=1,
        build_generation=1,
        reminder_scope=CurrentRunReminderScope(1),
    )
    window = state.lease.open_injection_admission(scope)
    task = asyncio.create_task(_never_finishes())
    target = ActiveInjectionTarget(
        route="fsm_active",
        session_id="s1",
        session_generation=1,
        build_generation=1,
        current_run_scope=scope,
        run_task=task,
        conversation=cast("KernelConversation", object()),
        bindings=cast(TurnBindings, object()),
        reminder_middleware=cast(SystemReminderMiddleware, object()),
        reminder_target=CurrentRunReminderTarget(CurrentRunReminderScope(1), "pre_prepare", 0),
        injection_window=window,
    )

    try:
        assert state.lease.is_injection_admission_current(window) is True
        assert state.lease.begin_active_injection_commit(target) is True
        assert state.lease.active_injection_commits == 1
        state.lease.finish_active_injection_commit()
        assert state.lease.active_injection_commits == 0

        state.lease.close_injection_admission(scope)
        assert state.lease.is_injection_admission_current(window) is False
        assert state.lease.begin_active_injection_commit(target) is False
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_session_transition_invalidation_preserves_task_until_reset() -> None:
    state = make_turn_state()
    admission = state.lease.reserve_prompt_admission(kind="fresh", session_generation=7, build_generation=1)
    scope = state.lease.begin_current_run_scope(
        owner_admission_id=admission.admission_id,
        session_generation=7,
        build_generation=1,
        reminder_scope=CurrentRunReminderScope(1),
    )
    state.lease.open_injection_admission(scope)
    task = asyncio.create_task(_never_finishes())
    state.lease.run_task = task
    state.set_current_input("work", ["work"], "created")

    try:
        assert admission is not None
        state.lease.invalidate_for_session_transition_pre_shutdown(old_session_generation=7)

        assert state.lease.prompt_admission_closed is False
        assert state.lease.active_admission_count() == 1
        assert state.lease.run_task is task
        assert state.lease.current_run_scope == scope
        assert state.current_input.text == "work"
        assert state.lease.injection_admission_open is False

        old_scope = state.reset_after_session_shutdown()

        assert old_scope == scope
        assert state.lease.run_task is None
        assert state.lease.current_run_scope is None
        assert state.current_input.text == ""
        assert state.history_start_index == 0
        assert state.lease.prompt_admission_closed is False
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_queued_session_transition_prepare_rejects_stale_owner_without_invalidating_current_session() -> None:
    session = make_session()
    turn_state = make_turn_state()
    permits = make_permits(session=session, turn_state=turn_state)
    session.session_id = "session-a"
    stale_generation = permits.session_generation

    current_owner = await permits.begin_session_transition("restore")
    queued = asyncio.create_task(
        permits.prepare_session_transition_if_current(
            "rollback",
            session_id="session-a",
            session_generation=stale_generation,
        )
    )
    try:
        await asyncio.sleep(0)
        assert queued.done() is False
    finally:
        permits.finish_session_transition(current_owner)

    assert await asyncio.wait_for(queued, timeout=5.0) is None
    assert permits.session_generation == stale_generation + 1
    assert turn_state.lease.prompt_admission_closed is False
    assert permits.gate_lock.locked() is False


@pytest.mark.asyncio
async def test_prepared_session_transition_waits_for_admission_without_invalidating_runtime() -> None:
    session = make_session()
    turn_state = make_turn_state()
    permits = make_permits(session=session, turn_state=turn_state)
    session.session_id = "session-a"
    admission = turn_state.lease.reserve_prompt_admission(
        kind="fresh",
        session_generation=permits.session_generation,
        build_generation=permits.build_generation,
    )
    assert admission is not None
    turn_state.lease.injection_admission_open = True
    prepared_ready = asyncio.Event()
    commit_requested = asyncio.Event()

    async def _prepare_and_commit() -> None:
        owner = await permits.prepare_session_transition_if_current(
            "rollback",
            session_id="session-a",
            session_generation=permits.session_generation,
        )
        assert owner is not None
        try:
            prepared_ready.set()
            await commit_requested.wait()
            permits.commit_session_transition(owner)
        finally:
            permits.finish_session_transition(owner)

    transition_task = asyncio.create_task(_prepare_and_commit())
    await asyncio.sleep(0)
    assert transition_task.done() is False
    assert turn_state.lease.prompt_admission_closed is True
    assert permits.session_generation == 0
    assert turn_state.lease.injection_admission_open is True
    assert turn_state.lease.active_admission_count() == 1

    turn_state.lease.release_prompt_admission(admission)
    await asyncio.wait_for(prepared_ready.wait(), timeout=5.0)
    assert permits.session_generation == 0
    assert turn_state.lease.injection_admission_open is True

    commit_requested.set()
    await asyncio.wait_for(transition_task, timeout=5.0)
    assert permits.session_generation == 1
    assert turn_state.lease.injection_admission_open is False


@pytest.mark.asyncio
async def test_cancelled_session_transition_begin_does_not_invalidate_current_session() -> None:
    session = make_session()
    turn_state = make_turn_state()
    permits = make_permits(session=session, turn_state=turn_state)
    turn_state.lease.active_injection_commits = 1
    turn_state.lease.active_injection_commits_idle.clear()
    turn_state.lease.injection_admission_open = True
    turn_state.lease.pending_retry = PendingRetry(
        retry=RetryInput("retry note", "created"), session_generation=permits.session_generation
    )

    task = asyncio.create_task(permits.begin_session_transition("restore"))
    try:
        await asyncio.sleep(0)
        assert turn_state.lease.prompt_admission_closed is True

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert permits.session_generation == 0
        assert turn_state.lease.prompt_admission_closed is False
        assert turn_state.lease.injection_admission_open is True
        assert turn_state.lease.pending_retry.dispatch_disabled is False
        assert permits.gate_lock.locked() is False
    finally:
        turn_state.lease.active_injection_commits = 0
        turn_state.lease.active_injection_commits_idle.set()


def test_session_transition_disables_pending_retry_dispatch_for_current_generation() -> None:
    session = make_session()
    turn_state = make_turn_state()
    permits = make_permits(session=session, turn_state=turn_state)
    permits.invalidate_for_session_transition_pre_shutdown()

    turn_state.lease.pending_retry = PendingRetry(
        retry=RetryInput("retry note", "created"), session_generation=permits.session_generation
    )

    permits.invalidate_for_session_transition_pre_shutdown()

    assert turn_state.lease.pending_retry.dispatch_disabled is True


def test_pre_prepare_reminder_target_expires_when_another_scope_is_prepared() -> None:
    reminder = SystemReminderMiddleware()
    first_scope = reminder.create_current_run_scope()
    second_scope = reminder.create_current_run_scope()
    stale_target = reminder.capture_current_run_target(first_scope)
    assert stale_target is not None

    reminder.prepare_turn(reminder_scope=second_scope)

    assert reminder.queue_hook_reminders_for_current_run(stale_target, ["stale"]) is False

    reminder.expire_current_run_scope(second_scope)

    assert reminder.queue_hook_reminders_for_current_run(stale_target, ["still stale"]) is False


def test_pre_prepare_reminder_target_expires_when_unscoped_turn_is_prepared() -> None:
    reminder = SystemReminderMiddleware()
    scope = reminder.create_current_run_scope()
    stale_target = reminder.capture_current_run_target(scope)
    assert stale_target is not None

    reminder.prepare_turn()

    assert reminder.queue_hook_reminders_for_current_run(stale_target, ["stale"]) is False
    assert reminder.queue_hook_reminders_for_current_run(stale_target, []) is False


def test_current_run_reminder_scope_is_identity_bound_to_middleware() -> None:
    first = SystemReminderMiddleware()
    first_scope = first.create_current_run_scope()
    stale_target = first.capture_current_run_target(first_scope)
    assert stale_target is not None

    second = SystemReminderMiddleware()
    second_scope = second.create_current_run_scope()

    assert first_scope.scope_id == second_scope.scope_id
    assert second.queue_hook_reminders_for_current_run(stale_target, ["stale"]) is False


def test_pre_prepare_reminder_target_survives_same_scope_prepare() -> None:
    reminder = SystemReminderMiddleware()
    scope = reminder.create_current_run_scope()
    target = reminder.capture_current_run_target(scope)
    assert target is not None

    reminder.prepare_turn(reminder_scope=scope)

    assert reminder.queue_hook_reminders_for_current_run(target, ["same scope"]) is True


def test_pre_prepare_current_run_reminders_are_consumed_by_same_scope_prepare() -> None:
    reminder = SystemReminderMiddleware()
    scope = reminder.create_current_run_scope()
    target = reminder.capture_current_run_target(scope)
    assert target is not None

    assert reminder.queue_hook_reminders_for_current_run(target, ["pre-prepare note"]) is True

    reminder.prepare_turn(reminder_scope=scope)

    assert "pre-prepare note" in reminder._build_reminders()
