# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP abort-result and transport cancellation share the shell's once lock."""

from __future__ import annotations

import asyncio
from unittest.mock import create_autospec

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import InvocationPaused
from chrys.orchestration.invoker.contracts import AbortCause
from tests.orchestration.sub_agents._acp_fakes import make_controller
from tests.service.acp_client.helpers import make_spec
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, with_wait_deadline


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_acp_abort_result_and_cancel_active_finalize_once(tmp_path, monkeypatch, wait_for_child_window):
    bus = EventBus()
    shell = make_controller(
        bus, tmp_path, spec_factory=lambda ordinal: make_spec(tmp_path, scenario="prompt_cancelled")
    )
    paused, abort_entered, release_abort = asyncio.Event(), asyncio.Event(), asyncio.Event()
    finalize_entered, both_entries, release_finalize = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def on_paused(event):
        paused.set()

    await bus.subscribe(InvocationPaused, on_paused)
    policy = shell.policy
    abort_result = policy.abort_result
    finalize = policy.finalize_cancellation
    shell_finalize = shell.finalize_cancellation
    paths, finalized = [], []

    async def abort(*, by_user: bool):
        result = await abort_result(by_user=by_user)
        abort_entered.set()
        await release_abort.wait()
        return result

    async def terminal():
        finalized.append("policy")
        finalize_entered.set()
        await release_finalize.wait()
        await finalize()

    async def enter_finalize():
        paths.append(asyncio.current_task().get_name())
        if len(paths) == 2:
            both_entries.set()
        await shell_finalize()

    monkeypatch.setattr(policy, "abort_result", create_autospec(abort_result, side_effect=abort))
    monkeypatch.setattr(policy, "finalize_cancellation", create_autospec(finalize, side_effect=terminal))
    monkeypatch.setattr(shell, "finalize_cancellation", create_autospec(shell_finalize, side_effect=enter_finalize))

    async def caller():
        try:
            return await shell.run()
        finally:
            # ACP's caller recipe owns cancellation finalization after run unwinds.
            await shell.finalize_cancellation()

    async def cancel():
        policy.latch_abort(AbortCause.CASCADE)
        await shell.cascade_abort()
        await shell.finalize_cancellation()

    running = asyncio.create_task(caller(), name="abort-result")
    cancelling = None
    try:
        await wait_for_child_window(running, paused, "ACP sub-agent paused")
        assert shell.request_abort() is True
        await asyncio.wait_for(abort_entered.wait(), 5)
        cancelling = asyncio.create_task(cancel(), name="cancel-active")
        await asyncio.wait_for(finalize_entered.wait(), 5)
        release_abort.set()
        await asyncio.wait_for(both_entries.wait(), 5)
        assert paths == ["cancel-active", "abort-result"]
        assert finalized == ["policy"]
        release_finalize.set()
        results = await asyncio.gather(running, cancelling, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1] is None
        assert finalized == ["policy"]
        policy.finalize_cancellation.assert_awaited_once()
    finally:
        release_abort.set()
        release_finalize.set()
        if not running.done():
            running.cancel()
        await asyncio.gather(running, *([cancelling] if cancelling is not None else []), return_exceptions=True)
        await policy.backend.aclose()
