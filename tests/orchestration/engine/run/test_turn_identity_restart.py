# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
"""Turn identity across a save, a restart and a restore.

Fresh prompts retire no-work failed turns and keep retained turns' boundaries.
Older sessions can still have drifted ordinals: their retries keep resolving
the turn by its recorded identity instead of whichever entry the ordinal names.

The second half drives a real reopen: the first runtime is shut down, a second
one is built over the same store, and the session comes back through the
ordinary ``SessionRestore`` path rather than having its state assigned.
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
    SessionRestore,
    SessionRestored,
    TurnStarted,
    UserMessage,
    UserRetry,
    Warning,
)
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.workspace import Workspace
from chrys.kernel import ChatResponseUpdate, Message
from chrys.orchestration.engine.state.machine import EngineState
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.state.store import JsonFileStateStore
from chrys.service.trajectory.state import TRAJECTORY_STATE_KEY
from tests.orchestration.engine.run._engine_run_helpers import _PROFILE, _make_registry
from tests.support.event_capture import collect_events
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.waiting import ENGINE_TURN_TIMEOUT, await_run_task_chain, wait_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterable
    from pathlib import Path

_SESSION_ID = "a0a1b2c3-d4e5-4f60-8a91-b2c3d4e5f603"


class ProviderRefusal(RuntimeError):
    """A provider failure the classifier cannot call transient, so no retry runs."""


class _RefusingClient(MockChatClient):
    """Refuse every wire call, so each turn the runtime admits fails."""

    async def _stream_updates(
        self,
        resp: MockResponse,
        model_id: str,
        finish_reason: str,
    ) -> AsyncIterable[ChatResponseUpdate]:
        raise ProviderRefusal("provider refused the call")
        yield  # pragma: no cover - unreachable, keeps the method a generator


class _FailSecondClient(MockChatClient):
    """Answer the first wire call, refuse the second, then answer every later one."""

    def __init__(self, responses: list[MockResponse]) -> None:
        super().__init__(responses=responses)
        self.calls = 0

    async def _stream_updates(
        self,
        resp: MockResponse,
        model_id: str,
        finish_reason: str,
    ) -> AsyncIterable[ChatResponseUpdate]:
        self.calls += 1
        if self.calls == 2:
            raise ProviderRefusal("provider refused the call")
        async for update in super()._stream_updates(resp, model_id, finish_reason):
            yield update


async def _client_stub(*args: object, **kwargs: object) -> object:
    """Stand-in for the async client factory; the spec comes from the real one."""
    raise NotImplementedError


class _Session:
    """One runtime over a shared store: its turn events and the engine behind them."""

    def __init__(self, engine, turns: list[TurnStarted], events: list[object]) -> None:
        self.engine = engine
        self._turns = turns
        self.events = events

    def started(self) -> list[TurnStarted]:
        return list(self._turns)

    def complaints(self) -> list[object]:
        return [event for event in self.events if isinstance(event, Error | Warning)]

    async def restore(self, session_id: str) -> None:
        """Drive the ordinary restore path and wait for it to be observed."""
        await self.engine.event_bus.publish(SessionRestore(session_id=session_id), raise_handler_errors=True)
        await wait_for(
            lambda: any(event.session_id == session_id for event in self.events if isinstance(event, SessionRestored)),
            timeout=ENGINE_TURN_TIMEOUT,
            description="the restored session",
        )


async def _run_engine(
    agent_engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store,
    client: MockChatClient,
    *,
    session_id: str = _SESSION_ID,
) -> _Session:
    """Build and start one engine over *store*."""
    turns: list[TurnStarted] = []
    events: list[object] = []
    bus = EventBus()
    await bus.subscribe(TurnStarted, lambda event, _turns=turns: collect_events(_turns, event))
    for cls in (Error, Warning, SessionRestored):
        await bus.subscribe(cls, lambda event, _events=events: collect_events(_events, event))
    settings, model_registry = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False, max_transient_retries=0)
    # ``create_client`` is an async factory, and an autospecified stand-in keeps
    # the call signature the builder passes.
    monkeypatch.setattr(builder_module, "create_client", create_autospec(_client_stub, return_value=client))
    engine = agent_engine(
        bus,
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=model_registry,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    # Before start: the trajectory recorder takes its session directory from the
    # id the session has when it binds, not from a later assignment.
    engine.session.session_id = session_id
    await engine.start(_PROFILE)
    return _Session(engine, turns, events)


async def _ask(session: _Session, text: str, *, client_message_id: str | None = None) -> None:
    await session.engine.event_bus.publish(UserMessage(text=text, client_message_id=client_message_id))
    await await_run_task_chain(session.engine, turn_state=session.engine.turns.turn_state)


@pytest.mark.asyncio
async def test_a_reload_keeps_only_retained_turns_with_their_opening_identity(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-work failed opener is retired before the next prompt reuses its slot."""
    store = JsonFileStateStore(tmp_path)
    first = await _run_engine(agent_engine, tmp_path, monkeypatch, store, _RefusingClient(responses=[]))

    await _ask(first, "A", client_message_id="msg-a")
    failed = first.engine.turn_records()
    assert len(failed) == 1, "a failed turn is still a turn"
    assert failed[0].user_message_id == "msg-a"
    assert failed[0].opening_item_id is not None

    await _ask(first, "B", client_message_id="msg-b")
    recorded = first.engine.turn_records()
    assert [record.turn_number for record in recorded] == [1]
    assert recorded[0].turn_id != failed[0].turn_id
    assert [record.user_message_id for record in recorded] == ["msg-b"]

    # What the session file holds is what a restart will read back.
    saved = await store.load_session_transcript(_SESSION_ID)
    assert saved is not None
    assert [(r.turn_id, r.turn_number, r.user_message_id) for r in saved.turns] == [
        (r.turn_id, r.turn_number, r.user_message_id) for r in recorded
    ]
    assert [r.opening_item_id for r in saved.turns] == [r.opening_item_id for r in recorded]


@pytest.mark.asyncio
async def test_a_retry_after_a_real_restore_resolves_the_turn_by_id_not_by_ordinal(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restore a legacy missing boundary and retry by registry identity despite ordinal drift."""
    store = JsonFileStateStore(tmp_path)
    first = await _run_engine(agent_engine, tmp_path, monkeypatch, store, _RefusingClient(responses=[]))
    await _ask(first, "B", client_message_id="msg-b")
    # Keep B through the next fresh prompt; new sessions repair boundaries.
    history = first.engine.current.require_loaded().bindings.backend.history_state
    history["messages"].insert(1, Message("assistant", ["legacy retained work"]))
    await _ask(first, "X", client_message_id="msg-x")
    recorded = first.engine.turn_records()
    assert len(recorded) == 2
    x_id = recorded[-1].turn_id
    # Seed the legacy file after admission: fresh cleanup now repairs a missing
    # boundary before executing X, so it can no longer generate this old layout.
    first_marker = next(
        index
        for index, message in enumerate(history["messages"])
        if message.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.TURN
    )
    del history["messages"][first_marker]
    history["turn_counter"] = 1
    await first.engine.writer.save_current_session()
    saved = await store.load_session_transcript(_SESSION_ID)
    assert saved is not None
    assert [r.turn_id for r in saved.turns] == [r.turn_id for r in recorded]
    saved_counter = (await store.load_session(_SESSION_ID) or {}).get("turn_counter", 0)
    assert saved_counter < recorded[-1].turn_number

    # Reopen for real: the first runtime releases the session write lock, and a
    # second one starts over the same store and restores the saved session.
    await first.engine.shutdown()
    second = await _run_engine(
        agent_engine, tmp_path, monkeypatch, store, MockChatClient(responses=[MockResponse(text="retried")])
    )
    await second.restore(_SESSION_ID)
    assert second.engine.state is EngineState.FAILED
    assert second.engine.session.turn_number == saved_counter
    # Without this the scenario proves nothing: the ordinal the reopened runtime
    # continues from is not the one X was registered under.
    assert second.engine.session.turn_number != recorded[-1].turn_number

    await second.engine.event_bus.publish(UserRetry())
    await wait_for(
        lambda: any(event.is_retry for event in second.started()),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the retry pass starting",
    )
    await await_run_task_chain(second.engine, turn_state=second.engine.turns.turn_state)

    assert second.complaints() == [], "the retry was refused"
    retried = [event for event in second.started() if event.is_retry]
    assert len(retried) == 1, "one retry pass, and it names the turn it resumes"
    assert retried[0].turn_id == x_id
    assert retried[0].turn_number == recorded[-1].turn_number
    # The retry does not re-open the turn: the registry is unchanged.
    assert [record.turn_id for record in second.engine.turn_records()] == [r.turn_id for r in recorded]

    # And the follow-up question opens a turn of its own. Its ordinal continues
    # from the saved counter, so it reuses the failed turn's — ordinals are the
    # saved timeline, and the ids are what tell the turns apart.
    await _ask(second, "C", client_message_id="msg-c")
    after = second.engine.turn_records()
    assert len(after) == 3
    assert [record.turn_id for record in after[:2]] == [r.turn_id for r in recorded]
    assert after[-1].turn_number == saved_counter + 1
    assert after[-1].user_message_id == "msg-c"
    assert after[-1].turn_id not in {record.turn_id for record in recorded}


@pytest.mark.asyncio
@pytest.mark.parametrize("fork_depth", [0, 1, 2], ids=["restore", "fork", "fork-of-fork"])
async def test_a_retry_does_not_adopt_a_legacy_registries_older_turn(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch, fork_depth: int
) -> None:
    """A registry too old to have recorded the last question names no turn to retry.

    Registration used to happen inside the trajectory writer, so a session whose
    last turn never reached the log holds a registry that stops at an earlier
    question. A retry then continues the turn the *ordinal* points at — the
    question that failed — and must not be handed the older turn's identity and
    client message id.
    """
    store = JsonFileStateStore(tmp_path)
    first = await _run_engine(
        agent_engine, tmp_path, monkeypatch, store, _FailSecondClient(responses=[MockResponse(text="first")])
    )
    await _ask(first, "A", client_message_id="msg-a")
    await _ask(first, "B", client_message_id="msg-b")
    a_turn = first.engine.turn_records()[0]
    assert first.engine.session.turn_number == 2

    state = first.engine.current.require_loaded().bindings.backend.history_state
    # Rewrite what is saved as the pre-upgrade layout, holding only A: the
    # registry B never reached.
    state[TRAJECTORY_STATE_KEY] = {"turns": {"1": {"turn_id": a_turn.turn_id, "started_sequence": 3}}}

    await first.engine.shutdown()
    restore_id = _SESSION_ID
    for _ in range(fork_depth):
        restore_id = await asyncio.to_thread(store.fork_session, restore_id)
    second = await _run_engine(
        agent_engine,
        tmp_path,
        monkeypatch,
        store,
        MockChatClient(responses=[MockResponse(text="retried")]),
        session_id=restore_id,
    )
    await second.restore(restore_id)
    assert second.engine.state is EngineState.FAILED
    assert [record.turn_id for record in second.engine.turn_records()] == [a_turn.turn_id]

    await second.engine.event_bus.publish(UserRetry())
    await wait_for(
        lambda: any(event.is_retry for event in second.started()),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the retry pass starting",
    )
    await await_run_task_chain(second.engine, turn_state=second.engine.turns.turn_state)

    assert second.complaints() == [], "the retry was refused"
    retried = [event for event in second.started() if event.is_retry]
    assert len(retried) == 1
    # B's retry is B: it must not come back wearing A's identity or ordinal,
    # which is what the registry it could not find B in would otherwise hand it.
    # No frontend id is reported either — the retry carries no message of its
    # own, and A's id is the one a mismatch would have copied.
    assert retried[0].turn_id != a_turn.turn_id
    assert retried[0].turn_number == 2
    assert retried[0].user_message_id is None
    opened = [record for record in second.engine.turn_records() if record.turn_id == retried[0].turn_id]
    assert len(opened) == 1, "the turn the retry opened is registered"
    assert opened[0].turn_number == 2
