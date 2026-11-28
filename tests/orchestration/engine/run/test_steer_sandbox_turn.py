# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A steer stays in its turn, so the sandbox's grants "for this turn" last through it.

The approval middleware's ``reset`` is where a turn's sandbox grants end
(``ApprovalMiddleware.reset`` calls the runtime's ``end_turn``). It runs once
when a turn starts; a steer goes on in the same run, so it must not run again
until the person's next turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from chrys.foundation.events.types import UserMessage
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.llm.mock import MockChatClient, MockResponse
from tests.support.waiting import await_run_task_chain

from .test_steer import _STEER_EVENTS, _HeldTool, _start, _wait_entered

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


async def test_a_steer_does_not_end_the_turn_s_sandbox_grants_and_the_next_turn_does(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    resets: list[int] = []
    original = ApprovalMiddleware.reset

    def counting(self: ApprovalMiddleware) -> None:
        resets.append(1)
        original(self)

    monkeypatch.setattr(ApprovalMiddleware, "reset", counting)
    held = _HeldTool()
    client = MockChatClient(
        responses=[
            MockResponse(text="Let me check", tool_calls=[("hold", "c1", {"message": "x"})]),
            MockResponse(text="Steered."),
            MockResponse(text="Next turn."),
        ]
    )
    started = await _start(agent_engine, tmp_path, monkeypatch, client, event_types=_STEER_EVENTS, tools=[held.tool])
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_entered(started, held)
        await started.bus.publish(UserMessage(text="do this instead", steer=True))
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
        assert client.call_count == 2
        assert len(resets) == 1, "a steer ended the turn's grants"
        await started.bus.publish(UserMessage(text="and now"))
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        held.release.set()

    assert client.call_count == 3
    assert len(resets) == 2, "the next turn did not end the last turn's grants"
