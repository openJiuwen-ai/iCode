# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Turn consumers fence awaited work by the captured backend conversation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from chrys.foundation.events.types import UserInject, UserRetry
from chrys.service.hooks.schema import HookDecision
from chrys.service.trajectory.preparation import PreparationOutcome
from tests.orchestration.engine.run.test_lifecycle_hooks import (
    _BlockingPromptHookManager,
    _cancel_active_run,
    _Host,
    _install_active_run,
    on_user_inject,
    on_user_retry,
)
from tests.support.loaded_agents import install_loaded_agent
from tests.support.waiting import wait_for


@pytest.mark.parametrize("route", ["inject", "active_retry", "retry"])
async def test_same_turn_bindings_cannot_retarget_a_captured_conversation(route: str) -> None:
    active = route != "retry"
    host = _Host(decision=HookDecision(), executor_running=active)
    host._turn_state.lease.clear_pending_retry(outcome=PreparationOutcome.DROPPED)
    if active:
        _install_active_run(host)
    original = host.current.loaded.bindings
    bindings = SimpleNamespace(backend=object(), state=original, trajectory_context=None)
    install_loaded_agent(host, bindings=bindings)
    hook = _BlockingPromptHookManager(HookDecision(system_reminders=["must not queue"]))
    host.session.hook_manager = hook
    task = asyncio.create_task(
        on_user_inject(host, UserInject(text="must not inject"))
        if route == "inject"
        else on_user_retry(host, UserRetry(text="must not run"))
    )
    try:
        await wait_for(hook.entered.is_set, description="conversation captured before prompt hook")
        bindings.backend = object()
        hook.release.set()
        await task
        assert host.current.loaded.bindings is bindings
        assert original.injected == original.approval_context == []
        assert host.current.loaded.reminder_middleware.queued == []
        assert host.run_texts == host.retry_texts == []
        assert host._turn_state.lease.pending_retry.retry.text == ""
    finally:
        hook.release.set()
        await asyncio.gather(task, return_exceptions=True)
        if active:
            await _cancel_active_run(host)


async def test_turn_shell_accumulates_failed_and_retry_passes_once(executor) -> None:
    from unittest.mock import create_autospec

    from chrys.kernel import AgentResponse
    from chrys.orchestration.invoker.contracts import Failed
    from tests.orchestration.invoker._main_pass import continuation_pass, fresh_pass

    backend = executor.backend
    backend._attempts.run = create_autospec(
        backend._attempts.run,
        side_effect=[ValueError("failed"), AgentResponse(messages=[]), AgentResponse(messages=[])],
    )
    await fresh_pass(executor, ["input"])
    first = executor.inputs.outcome
    assert isinstance(first, Failed)
    await continuation_pass(executor, [])
    second = executor.inputs.outcome
    assert second is not None
    assert executor.inputs.evidence.passes == (first.handle.pass_id, second.handle.pass_id)
    accumulated = executor.inputs.evidence
    with pytest.raises(ValueError, match="already accumulated"):
        executor.record_outcome(second)
    assert executor.inputs.evidence is accumulated
    await fresh_pass(executor, ["new turn"])
    assert executor.inputs.evidence.invocation_id != accumulated.invocation_id
    assert len(executor.inputs.evidence.passes) == 1
