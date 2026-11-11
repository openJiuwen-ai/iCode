# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Generation and resource identity changes invalidate awaited prompt admissions."""

from __future__ import annotations

import asyncio

import pytest

from chrys.foundation.events.types import UserInject, UserRetry
from chrys.service.hooks.schema import HookDecision
from chrys.service.trajectory.preparation import PreparationOutcome
from tests.orchestration.engine.run.test_lifecycle_hooks import (
    _BlockingPromptHookManager,
    _cancel_active_run,
    _Executor,
    _Host,
    _install_active_run,
    _Reminder,
    on_user_inject,
    on_user_retry,
)
from tests.support.loaded_agents import install_loaded_agent
from tests.support.waiting import wait_for


@pytest.mark.parametrize("changed", ["session_generation", "build_generation", "resources"])
@pytest.mark.parametrize("route", ["inject", "active_retry", "retry"])
async def test_hook_await_invalidates_only_the_changed_owner(changed: str, route: str) -> None:
    active = route != "retry"
    host = _Host(decision=HookDecision(), executor_running=active)
    host._turn_state.lease.clear_pending_retry(outcome=PreparationOutcome.DROPPED)
    if active:
        _install_active_run(host)
    hook = _BlockingPromptHookManager(HookDecision(system_reminders=["must not queue"]))
    host.session.hook_manager = hook
    old_executor = host.current.loaded.bindings
    old_reminder = host.current.loaded.reminder_middleware
    task = asyncio.create_task(
        on_user_inject(host, UserInject(text="must not inject"))
        if route == "inject"
        else on_user_retry(host, UserRetry(text="must not call provider"))
    )
    try:
        await wait_for(hook.entered.is_set, description="prompt hook awaiting owner check")
        if changed == "session_generation":
            host.permits.session_generation += 1
        elif changed == "build_generation":
            host.permits.build_generation += 1
        else:
            install_loaded_agent(host, bindings=_Executor(running=active))
            install_loaded_agent(host, reminder_middleware=_Reminder())
        hook.release.set()
        await task
        assert host.session.session_id == "s1"
        assert host._turn_state.lease.pending_retry.retry.text == ""
        assert host._turn_state.lease.pending_retry.retry.created_at is None
        assert host.run_texts == host.retry_texts == []
        assert host._fsm.transitions == []
        assert old_executor.injected == host.current.loaded.bindings.injected == []
        assert old_executor.approval_context == host.current.loaded.bindings.approval_context == []
        assert old_reminder.queued == host.current.loaded.reminder_middleware.queued == []
    finally:
        hook.release.set()
        await asyncio.gather(task, return_exceptions=True)
        if active:
            await _cancel_active_run(host)


@pytest.mark.parametrize("changed", ["session_generation", "build_generation", "resources"])
async def test_retry_note_commit_rechecks_captured_owner_before_queue(
    changed: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import create_autospec

    from chrys.orchestration.engine.run.retry import RetryCoordinator

    host = _Host(decision=HookDecision(system_reminders=["must not queue"]), executor_running=True)
    host._turn_state.lease.clear_pending_retry(outcome=PreparationOutcome.DROPPED)
    _install_active_run(host)
    entered, release = asyncio.Event(), asyncio.Event()
    original = RetryCoordinator._commit_retry_note_side_effects_to_target

    async def commit(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(
        RetryCoordinator, "_commit_retry_note_side_effects_to_target", create_autospec(original, side_effect=commit)
    )
    old_executor, old_reminder = host.current.loaded.bindings, host.current.loaded.reminder_middleware
    task = asyncio.create_task(on_user_retry(host, UserRetry(text="must not run")))
    try:
        await wait_for(entered.is_set, description="retry side-effect target captured")
        if changed == "session_generation":
            host.permits.session_generation += 1
        elif changed == "build_generation":
            host.permits.build_generation += 1
        else:
            install_loaded_agent(host, bindings=_Executor(running=True))
            install_loaded_agent(host, reminder_middleware=_Reminder())
        release.set()
        await task
        assert old_reminder.queued == host.current.loaded.reminder_middleware.queued == []
        assert old_executor.injected == host.current.loaded.bindings.injected == []
        assert old_executor.approval_context == host.current.loaded.bindings.approval_context == []
        assert host.run_texts == host.retry_texts == []
        assert host._turn_state.lease.pending_retry.retry.text == ""
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _cancel_active_run(host)
