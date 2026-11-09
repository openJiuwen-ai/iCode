# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Turn identity end to end: minted once, registered, published, and kept across retries.

The turn's id is the first pass's id and never changes for that question, so
a failed or cancelled turn that leaves the saved counter behind cannot hand
its identity to the next one.  These tests drive a real engine over the event
bus and read the identity back from all three places a frontend can find it:
the ``TurnStarted`` event, the session's turn registry, and the pass's
``turn.started`` trajectory line.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    Error,
    SessionReady,
    SessionSaved,
    TurnStarted,
    UserInterrupt,
    UserMessage,
    UserRetry,
)
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.metadata import ANALYTICS_ITEM_ID_KEY
from chrys.foundation.trajectory.reader import read_trajectory
from chrys.kernel import ChatResponseUpdate, FinishReason, FinishReasonLiteral
from chrys.orchestration.engine.run.runner import _resumed_turn
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.state.store import JsonFileStateStore
from chrys.service.trajectory.session import trajectory_events_path
from chrys.service.trajectory.state import TRAJECTORY_STATE_KEY, turn_records
from tests.orchestration.engine.run._engine_run_helpers import _PROFILE, _make_registry
from tests.support.event_capture import collect_events
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.waiting import ENGINE_TURN_TIMEOUT, await_run_task_chain, wait_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterable
    from pathlib import Path

    from chrys.foundation.trajectory.envelope import TrajectoryEvent

_CLIENT_MESSAGE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


class _InterruptibleClient(MockChatClient):
    """Hold only the first stream until the engine cancels it, so a turn can be cut short.

    Later responses stream normally: the retry has to be able to finish.
    """

    def __init__(self, responses: list[MockResponse]) -> None:
        super().__init__(responses=responses)
        self.stream_held = asyncio.Event()
        self.stream_cancelled = asyncio.Event()
        self.release_stream = asyncio.Event()
        self.held = False

    async def _stream_updates(
        self,
        resp: MockResponse,
        model_id: str,
        finish_reason: FinishReasonLiteral | FinishReason,
    ) -> AsyncIterable[ChatResponseUpdate]:
        hold = not self.held
        self.held = True
        async for update in super()._stream_updates(resp, model_id, finish_reason):
            yield update
            if hold:
                hold = False
                # Signalled inside the stream, so a test knows the turn is past
                # admission and holding before it publishes anything else.
                self.stream_held.set()
                try:
                    await self.release_stream.wait()
                except asyncio.CancelledError:
                    self.stream_cancelled.set()
                    raise


class _Started:
    """A started engine with its turn events collected and its trajectory readable."""

    def __init__(self, engine, bus: EventBus, turns: list[TurnStarted], events: list[object]) -> None:
        self.engine = engine
        self.bus = bus
        self.turns = turns
        self.events = events
        self.session_dir = engine.session_dir

    def errors(self) -> list[Error]:
        return [event for event in self.events if isinstance(event, Error)]

    def passes(self) -> list[TrajectoryEvent]:
        """Every ``turn.started`` line of this session's log, oldest first."""
        readback = read_trajectory(trajectory_events_path(self.session_dir))
        assert readback.corrupt_lines == []
        return [event for event in readback.events if event.event_type == EventType.TURN_STARTED]


async def _start_engine(
    agent_engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: MockChatClient,
    *,
    state_store: JsonFileStateStore | None = None,
) -> _Started:
    turns: list[TurnStarted] = []
    events: list[object] = []
    bus = EventBus()
    await bus.subscribe(TurnStarted, lambda event, _events=turns: collect_events(_events, event))
    for cls in (Error, SessionSaved, SessionReady):
        await bus.subscribe(cls, lambda event, _events=events: collect_events(_events, event))
    settings, model_registry = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False)
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    engine = agent_engine(
        bus,
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=model_registry,
        state_store=state_store if state_store is not None else JsonFileStateStore(tmp_path),
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    await engine.start(_PROFILE)
    return _Started(engine, bus, turns, events)


@pytest.mark.asyncio
async def test_a_fresh_turn_registers_publishes_and_records_one_identity(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = await _start_engine(agent_engine, tmp_path, monkeypatch, MockChatClient([MockResponse(text="Ready.")]))

    await started.bus.publish(UserMessage(text="Hello", client_message_id=_CLIENT_MESSAGE_ID))
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    assert len(started.turns) == 1
    published = started.turns[0]
    assert published.is_retry is False
    assert published.turn_number == 1
    assert published.user_message_id == _CLIENT_MESSAGE_ID

    records = started.engine.turn_records()
    assert len(records) == 1
    record = records[0]
    assert record.turn_id == published.turn_id
    assert record.turn_number == 1
    assert record.user_message_id == _CLIENT_MESSAGE_ID
    # The opening id names the message that opened the turn, not just some id:
    # it is the analytics item id the same question carries in the transcript.
    opener_item_ids = {
        message.additional_properties.get(ANALYTICS_ITEM_ID_KEY)
        for message in started.engine.current.require_loaded().bindings.backend.history_state["messages"]
        if message.role == "user"
    }
    assert record.opening_item_id is not None
    assert opener_item_ids == {record.opening_item_id}

    # The log agrees: the pass's line carries the same id and landed where the
    # registry says it did, which is the range a rollback would supersede.
    passes = started.passes()
    assert len(passes) == 1
    assert passes[0].turn_id == published.turn_id
    assert passes[0].payload["turn_id"] == published.turn_id
    assert passes[0].payload["logical_turn_id"] == published.turn_id
    assert record.started_sequence == passes[0].sequence


@pytest.mark.asyncio
async def test_a_retry_keeps_the_turn_identity_and_marks_the_pass(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _InterruptibleClient([MockResponse(text="working", chunk_size=1), MockResponse(text="Done.")])
    started = await _start_engine(agent_engine, tmp_path, monkeypatch, client)

    await started.bus.publish(UserMessage(text="Please do it", client_message_id=_CLIENT_MESSAGE_ID))
    await wait_for(client.stream_held.is_set, timeout=ENGINE_TURN_TIMEOUT, description="first pass streaming")
    await started.bus.publish(UserInterrupt())
    await wait_for(client.stream_cancelled.is_set, timeout=ENGINE_TURN_TIMEOUT, description="stream cancelled")
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    first_turn_id = started.engine.turn_records()[-1].turn_id

    await started.bus.publish(UserRetry())
    await wait_for(
        lambda: any(event.is_retry for event in started.turns),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the retry pass starting",
    )
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    assert [event.turn_id for event in started.turns] == [first_turn_id, first_turn_id]
    assert [event.is_retry for event in started.turns] == [False, True]
    assert started.turns[1].turn_number == started.turns[0].turn_number
    # The retry belongs to the question it resumes, so it names that question's
    # opener rather than a client id of its own.
    assert started.turns[0].user_message_id == _CLIENT_MESSAGE_ID
    assert started.turns[1].user_message_id == _CLIENT_MESSAGE_ID
    # A retry does not re-open the turn: the id and the ordinal stay put.
    assert [record.turn_id for record in started.engine.turn_records()] == [first_turn_id]

    passes = started.passes()
    assert len(passes) == 2
    # The first pass *is* the turn — its pass id is the turn id clients hold.
    assert passes[0].turn_id == first_turn_id
    assert passes[0].payload["logical_turn_id"] == first_turn_id
    # The retry records a turn.started of its own and names the turn it resumes.
    assert passes[1].turn_id != first_turn_id
    assert passes[1].payload["logical_turn_id"] == first_turn_id


@pytest.mark.asyncio
@pytest.mark.parametrize("guidance", ["Use the other file", ""], ids=["guided", "bare"])
async def test_a_retry_pass_names_its_guidance_by_the_frontend_id(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch, guidance: str
) -> None:
    """A guided retry's TurnStarted echoes its note's client id beside the note's history item; a bare one names none."""
    client = _InterruptibleClient([MockResponse(text="working", chunk_size=1), MockResponse(text="Done.")])
    started = await _start_engine(agent_engine, tmp_path, monkeypatch, client)

    await started.bus.publish(UserMessage(text="Please do it", client_message_id=_CLIENT_MESSAGE_ID))
    await wait_for(client.stream_held.is_set, timeout=ENGINE_TURN_TIMEOUT, description="first pass streaming")
    await started.bus.publish(UserInterrupt())
    await wait_for(client.stream_cancelled.is_set, timeout=ENGINE_TURN_TIMEOUT, description="stream cancelled")
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    await started.bus.publish(UserRetry(text=guidance, client_message_id="note-1"))
    await wait_for(
        lambda: any(event.is_retry for event in started.turns),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the retry pass starting",
    )
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    first, retry = started.turns
    assert first.note_message_id is None
    assert retry.is_retry is True
    # The opener's id stays the opener's: the note has an id of its own.
    assert retry.user_message_id == _CLIENT_MESSAGE_ID
    if guidance:
        history = started.engine.current.require_loaded().bindings.backend.history_state["messages"]
        (note_item,) = [
            message.additional_properties[ANALYTICS_ITEM_ID_KEY]
            for message in history
            if message.role == "user" and message.text == guidance
        ]
        assert (retry.note_message_id, retry.opening_item_id) == ("note-1", note_item)
    else:
        assert (retry.note_message_id, retry.opening_item_id) == (None, None)


@pytest.mark.asyncio
async def test_a_cancelled_turn_does_not_hand_its_ordinal_to_the_next_one(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The running ordinal drifts ahead of the saved one; identity must not."""
    client = _InterruptibleClient([MockResponse(text="working", chunk_size=1), MockResponse(text="Second.")])
    started = await _start_engine(agent_engine, tmp_path, monkeypatch, client)

    await started.bus.publish(UserMessage(text="First"))
    await wait_for(client.stream_held.is_set, timeout=ENGINE_TURN_TIMEOUT, description="first pass streaming")
    await started.bus.publish(UserInterrupt())
    await wait_for(client.stream_cancelled.is_set, timeout=ENGINE_TURN_TIMEOUT, description="first stream cancelled")
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    await started.bus.publish(UserMessage(text="Second"))
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    records = started.engine.turn_records()
    assert len(records) == 2, "both turns are identifiable, whatever their ordinals"
    assert records[0].turn_id != records[1].turn_id
    assert records[0].user_message_id is None
    assert started.turns[-1].turn_id == records[1].turn_id
    assert [event.turn_id for event in started.turns] == [record.turn_id for record in records]


class TestResumedTurn:
    """Which stored turn a retry pass resumes: the one that ran last, whatever ordinal it holds."""

    def test_the_last_turn_ran_last_however_the_ordinals_read(self) -> None:
        # A failed turn moves the registry on without moving the save and a
        # reopen restores the saved counter, so the runtime can hold an ordinal
        # an earlier turn still carries. Identity comes from execution order.
        state = {
            TRAJECTORY_STATE_KEY: {
                "turn_log": [
                    {"turn_number": 1, "turn_id": "aaa"},
                    {"turn_number": 2, "turn_id": "bbb"},
                ]
            }
        }
        resumed = _resumed_turn(state, turn_number=1)

        assert resumed is not None
        assert resumed.turn_id == "bbb"

    def test_a_legacy_registry_is_ordered_by_when_its_turns_ran(self) -> None:
        # The legacy layout is keyed by ordinal, which is exactly what drifts:
        # the entry with the later sequence ran later even though the counter
        # it was stored under is the lower one, and that is the turn a retry
        # belongs to.
        state = {
            TRAJECTORY_STATE_KEY: {
                "turns": {
                    "1": {"turn_id": "aaa", "started_sequence": 5},
                    "2": {"turn_id": "bbb", "started_sequence": 9},
                    "3": {"turn_id": "ccc", "started_sequence": 7},
                }
            }
        }
        resumed = _resumed_turn(state, turn_number=2)

        assert resumed is not None
        assert resumed.turn_id == "bbb"
        assert [record.turn_id for record in turn_records(state)] == ["aaa", "ccc", "bbb"]

    def test_a_partly_sequenced_legacy_registry_keeps_its_ordinal_order(self) -> None:
        # One entry cannot say when it ran, so no ordering by sequence can
        # interleave the two without ranking a known time against an unknown
        # one. The ordinal is what is left, and it is the order they were
        # stored in — which is the best a registry this old can support.
        state = {
            TRAJECTORY_STATE_KEY: {
                "turns": {
                    "1": {"turn_id": "aaa"},
                    "2": {"turn_id": "bbb", "started_sequence": 3},
                    "3": {"turn_id": "ccc"},
                }
            }
        }

        assert [record.turn_id for record in turn_records(state)] == ["aaa", "bbb", "ccc"]
        resumed = _resumed_turn(state, turn_number=3)
        assert resumed is not None
        assert resumed.turn_id == "ccc"

    def test_a_legacy_registry_with_no_usable_sequences_keeps_its_ordinal_order(self) -> None:
        state = {
            TRAJECTORY_STATE_KEY: {
                "turns": {
                    "1": {"turn_id": "aaa"},
                    "2": {"turn_id": "bbb", "started_sequence": True},
                    "3": {"turn_id": "ccc"},
                }
            }
        }

        # A bool is not a sequence, so no entry can say when it ran and the
        # ordinal is the only order left — the behaviour before this layout.
        assert [record.turn_id for record in turn_records(state)] == ["aaa", "bbb", "ccc"]
        resumed = _resumed_turn(state, turn_number=3)
        assert resumed is not None
        assert resumed.turn_id == "ccc"

    def test_a_legacy_registry_behind_the_running_ordinal_resumes_nothing(self) -> None:
        # Registration used to happen inside the trajectory writer, so the last
        # question a session ran may never have been registered. That registry
        # names a turn older than the one being retried, and handing the retry
        # its identity would report the previous question as this one.
        state = {
            TRAJECTORY_STATE_KEY: {
                "turns": {
                    "1": {"turn_id": "aaa", "started_sequence": 5},
                }
            }
        }

        assert _resumed_turn(state, turn_number=2) is None
        # The same registry is trusted while the ordinal it carries is the one
        # running now: a retry of that very turn resumes it.
        assert _resumed_turn(state, turn_number=1) is not None
        # The append layout is trusted either way: every admitted pass registered.
        append_state = {TRAJECTORY_STATE_KEY: {"turn_log": [{"turn_number": 1, "turn_id": "aaa"}]}}
        assert _resumed_turn(append_state, turn_number=2) is not None

    def test_nothing_registered_resumes_nothing(self) -> None:
        assert _resumed_turn({}, turn_number=1) is None
        assert _resumed_turn({TRAJECTORY_STATE_KEY: {"turn_log": []}}, turn_number=1) is None
        assert _resumed_turn({TRAJECTORY_STATE_KEY: {"turns": {}}}, turn_number=1) is None


@pytest.mark.asyncio
async def test_a_turn_without_trajectory_still_has_an_identity(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No state store means no log; the turn is still registered and published."""
    client = MockChatClient([MockResponse(text="Ready.")])
    turns: list[TurnStarted] = []
    bus = EventBus()
    await bus.subscribe(TurnStarted, lambda event, _turns=turns: collect_events(_turns, event))
    settings, model_registry = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False)
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    engine = agent_engine(bus, settings=settings, agent_registry=_make_registry(), model_registry=model_registry)
    await engine.start(_PROFILE)

    await bus.publish(UserMessage(text="Hello"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    assert len(turns) == 1
    assert turns[0].turn_id != ""
    assert [record.turn_id for record in engine.turn_records()] == [turns[0].turn_id]
