# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Usage publisher ownership and ordering without an engine host."""

from __future__ import annotations

import asyncio

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import UsageUpdate
from chrys.orchestration.engine.state.active_session import ActiveSession
from chrys.orchestration.engine.state.current_agent import CurrentAgent
from chrys.orchestration.engine.usage import UsagePublisher
from chrys.service.session.persistence import SessionPersistence


def _publisher(bus: EventBus) -> tuple[ActiveSession, UsagePublisher]:
    session = ActiveSession(persistence=SessionPersistence(None, bus), workspace_plan=None, approval_mode=None)
    session.session_id = "original"
    return session, UsagePublisher(bus=bus, session=session, current=CurrentAgent())


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_queued_usage_is_ordered_and_later_delivery_survives_cancelled_predecessor(cancel_first: bool) -> None:
    bus = EventBus()
    session, publisher = _publisher(bus)
    entered = asyncio.Event()
    release = asyncio.Event()
    events: list[UsageUpdate] = []

    async def receive(event: UsageUpdate) -> None:
        if event.total_tokens == 10:
            entered.set()
            await release.wait()
        events.append(event)

    await bus.subscribe(UsageUpdate, receive)
    publisher.publish_usage(10, input_tokens=7, output_tokens=3)
    first = publisher.tail
    assert first is not None
    await asyncio.wait_for(entered.wait(), 5)
    publisher.publish_usage(20, input_tokens=14, output_tokens=6)
    second = publisher.tail
    assert second is not None and second is not first
    assert publisher.tasks == {first, second}
    session.session_id = "replacement"
    if cancel_first:
        first.cancel()
    release.set()
    await publisher.drain()
    await publisher.settle()
    assert [event.total_tokens for event in events] == ([20] if cancel_first else [10, 20])
    assert all(event.session_id == "original" for event in events)
    assert publisher.tasks == set()
    assert publisher.tail is None


async def test_usage_publishers_have_independent_task_sets_and_totals() -> None:
    bus = EventBus()
    session, first = _publisher(bus)
    other_session, second = _publisher(bus)
    assert first.tasks is not second.tasks
    first.publish_usage(12, input_tokens=8, output_tokens=4)
    assert session.runtime_meta.total_session_tokens == 12
    assert other_session.runtime_meta.total_session_tokens == 0
    assert second.tail is None
    await first.settle()
    assert first.tasks == set()
