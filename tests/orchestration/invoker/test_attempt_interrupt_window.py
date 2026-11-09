# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""An interrupt latched before the attempt task exists ends the attempt unsent.

An interrupt (Stop, steer) cancels the running attempt through its task handle.
One accepted while the attempt's operation is still being recorded finds no
task to cancel; with no wire policy to check the latch before the call
(service-side storage), the attempt must check it before its task starts.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from unittest.mock import create_autospec

import pytest

from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.invoker.attempts import ModelRunTrace


def _interrupt_while_recording(executor: TurnBindings, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Latch the interrupt while the attempt's operation is recorded, as a Stop landing in that await would."""
    outcomes: list[str] = []
    real_run = executor._attempts.trace.run

    @contextlib.asynccontextmanager
    async def _run(trace: ModelRunTrace, run_kwargs: dict[str, object], *, stream: bool) -> AsyncIterator[None]:
        del trace
        await asyncio.sleep(0)  # the recording's own await
        executor._interrupt.set_interrupted()
        assert executor._attempt_handle.task is None
        try:
            async with real_run(run_kwargs, stream=stream):
                yield
        except asyncio.CancelledError:
            outcomes.append("interrupted")
            raise

    monkeypatch.setattr(ModelRunTrace, "run", _run)
    return outcomes


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "blocking"])
async def test_an_interrupt_latched_before_the_attempt_task_starts_sends_nothing(
    executor: TurnBindings, monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    agent_run = create_autospec(executor._agent.run)
    monkeypatch.setattr(executor._agent, "run", agent_run)
    outcomes = _interrupt_while_recording(executor, monkeypatch)

    with pytest.raises(asyncio.CancelledError):
        if stream:
            await executor._attempts._stream_single_attempt([], {}, watchdog=False)
        else:
            await executor._attempts._run_agent([], {})

    agent_run.assert_not_called()
    assert outcomes == ["interrupted"]
    assert executor._attempt_handle.task is None
