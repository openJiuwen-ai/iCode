# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Turn identity and ``messageId`` across replay and ``session/history``."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from acp import RequestError
from acp.helpers import text_block

from chrys.app.acp.history import index_turns, replay_session_history, session_history_payload
from chrys.app.acp.server import ChrysAcpServer
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import UserMessage
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.trajectory.metadata import ANALYTICS_ITEM_ID_KEY
from chrys.kernel import Content, Message
from chrys.orchestration.session_host import Cancelled, EndTurn
from chrys.service.state.store import JsonFileStateStore
from chrys.service.trajectory.state import TurnRecord
from tests.app.acp._server_fakes import _FakeClient, _FakeHost, _FakeManager
from tests.support.images import image_bytes

_CLIENT_MESSAGE_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"


class _ReplayClient:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        _ = session_id, kwargs
        self.updates.append(update)


def _user_message(text: str, item_id: str | None, **markers: Any) -> Message:
    message = Message("user", [text])
    if item_id is not None:
        message.additional_properties[ANALYTICS_ITEM_ID_KEY] = item_id
    message.additional_properties.update(markers)
    return message


async def _save(store: JsonFileStateStore, tmp_path: Any, messages: list[Message], turns: list[TurnRecord]) -> None:
    await store.save_session(
        "s1",
        {
            "messages": messages,
            "chrys_trajectory": {
                "turn_log": [
                    {
                        key: value
                        for key, value in (
                            ("turn_number", record.turn_number),
                            ("turn_id", record.turn_id),
                            ("opening_item_id", record.opening_item_id),
                            ("user_message_id", record.user_message_id),
                            ("origin_session_id", record.origin_session_id),
                        )
                        if value is not None
                    }
                    for record in turns
                ]
            },
        },
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )


async def _replay(store: JsonFileStateStore) -> list[Any]:
    client = _ReplayClient()
    await replay_session_history(client, store, "s1")  # type: ignore[arg-type]
    return client.updates


@pytest.mark.asyncio
async def test_replay_marks_the_opening_message_with_its_turn_and_echoes_the_clients_id(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    messages = [
        _user_message("first", "item-1"),
        Message("assistant", ["answer"]),
        _user_message("an injection", "item-2", **{HistoryMarkerKind.INJECTED_KEY: True}),
    ]
    await _save(
        store,
        tmp_path,
        messages,
        [TurnRecord(turn_number=1, turn_id="turn-a", opening_item_id="item-1", user_message_id=_CLIENT_MESSAGE_ID)],
    )

    updates = await _replay(store)

    user_chunks = [update for update in updates if update.session_update == "user_message_chunk"]
    assert [chunk.content.text for chunk in user_chunks] == ["first", "an injection"]
    # Only the opener carries the id the client submitted; the injection is a
    # different message, so it belongs to the turn but names no client id.
    assert user_chunks[0].message_id == _CLIENT_MESSAGE_ID
    assert user_chunks[0].field_meta == {"chrys": {"turnId": "turn-a", "turnNumber": 1}}
    assert user_chunks[1].message_id is None
    assert user_chunks[1].field_meta == {"chrys": {"turnId": "turn-a", "turnNumber": 1}}


@pytest.mark.asyncio
async def test_replay_sends_a_messages_images_under_its_id_and_turn(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    image_only_id = "9b2c1d4e-5f60-4a7b-8c9d-0e1f2a3b4c5d"
    opener = _user_message("look", "item-1")
    opener.contents.append(Content.from_data(data=image_bytes(), media_type="image/png"))
    image_only = _user_message("", "item-2")
    image_only.contents.append(Content.from_data(data=image_bytes(), media_type="image/png"))
    await _save(
        store,
        tmp_path,
        [opener, Message("assistant", ["seen"]), image_only],
        [
            TurnRecord(turn_number=1, turn_id="turn-a", opening_item_id="item-1", user_message_id=_CLIENT_MESSAGE_ID),
            TurnRecord(turn_number=2, turn_id="turn-b", opening_item_id="item-2", user_message_id=image_only_id),
        ],
    )

    updates = await _replay(store)

    user_chunks = [update for update in updates if update.session_update == "user_message_chunk"]
    assert [(chunk.content.type, chunk.message_id) for chunk in user_chunks] == [
        ("text", _CLIENT_MESSAGE_ID),
        ("image", _CLIENT_MESSAGE_ID),
        ("image", image_only_id),
    ]
    assert [chunk.field_meta for chunk in user_chunks] == [
        {"chrys": {"turnId": "turn-a", "turnNumber": 1}},
        {"chrys": {"turnId": "turn-a", "turnNumber": 1}},
        {"chrys": {"turnId": "turn-b", "turnNumber": 2}},
    ]


@pytest.mark.asyncio
async def test_replay_leaves_a_turn_it_cannot_identify_unlabelled(tmp_path) -> None:
    """A message that is not any record's opener gets no turn, rather than a guessed one."""
    store = JsonFileStateStore(tmp_path / "sessions")
    messages = [_user_message("first", "item-1"), _user_message("second", "item-9")]
    await _save(store, tmp_path, messages, [TurnRecord(turn_number=1, turn_id="turn-a", opening_item_id="item-1")])

    updates = await _replay(store)

    user_chunks = [update for update in updates if update.session_update == "user_message_chunk"]
    assert [chunk.field_meta for chunk in user_chunks] == [
        {"chrys": {"turnId": "turn-a", "turnNumber": 1}},
        None,
    ]


@pytest.mark.asyncio
async def test_a_session_with_no_registry_replays_without_turn_metadata(tmp_path) -> None:
    """Old sessions are never backfilled: the messages still replay, unlabelled."""
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1",
        {"messages": [Message("user", ["old"]), Message("assistant", ["answer"])]},
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )

    updates = await _replay(store)

    assert [(update.session_update, update.field_meta) for update in updates] == [
        ("user_message_chunk", None),
        ("agent_message_chunk", None),
    ]


@pytest.mark.asyncio
async def test_drifted_ordinals_keep_each_message_on_its_own_turn(tmp_path) -> None:
    """Two turns share ordinal 2 after a rollback; each message keeps its own id."""
    store = JsonFileStateStore(tmp_path / "sessions")
    messages = [
        _user_message("first", "item-1"),
        Message("assistant", ["one"]),
        _user_message("second", "item-2"),
        Message("assistant", ["two"]),
    ]
    await _save(
        store,
        tmp_path,
        messages,
        [
            TurnRecord(turn_number=1, turn_id="turn-a", opening_item_id="item-1"),
            TurnRecord(turn_number=2, turn_id="turn-b", opening_item_id="item-2"),
        ],
    )

    updates = await _replay(store)

    labels = [update.field_meta["chrys"]["turnId"] for update in updates if update.field_meta]
    assert labels == ["turn-a", "turn-a", "turn-b", "turn-b"]


@pytest.mark.asyncio
async def test_replay_gives_every_answer_message_one_fresh_uuid(tmp_path) -> None:
    """A CP groups chunks by message id, so one persisted message is one id."""
    store = JsonFileStateStore(tmp_path / "sessions")
    await _save(
        store,
        tmp_path,
        [
            _user_message("ask", "item-1"),
            Message("assistant", [Content.from_text("part one")]),
            Message("assistant", [Content.from_text("part two"), Content.from_text("part three")]),
        ],
        [TurnRecord(turn_number=1, turn_id="turn-a", opening_item_id="item-1")],
    )

    updates = await _replay(store)

    chunks = [update for update in updates if update.session_update == "agent_message_chunk"]
    assert [chunk.content.text for chunk in chunks] == ["part one", "part two", "\n\npart three"]
    assert chunks[0].message_id != chunks[1].message_id
    assert chunks[1].message_id == chunks[2].message_id


def test_index_turns_pairs_only_a_registered_opener() -> None:
    """The pairing key is the analytics item id minted once and copied verbatim."""
    messages = [
        {
            "role": "user",
            "contents": [{"type": "text", "text": "opener"}],
            "additional_properties": {ANALYTICS_ITEM_ID_KEY: "item-1"},
        },
        {"role": "assistant", "contents": [{"type": "text", "text": "answer"}]},
        {
            "role": "user",
            "contents": [{"type": "text", "text": "retry note"}],
            "additional_properties": {ANALYTICS_ITEM_ID_KEY: "item-2"},
        },
    ]
    records = [TurnRecord(turn_number=1, turn_id="turn-a", opening_item_id="item-1")]

    aligned = index_turns(messages, records)

    assert [(None if entry is None else (entry[0].turn_id, entry[1], entry[2])) for entry in aligned] == [
        ("turn-a", 0, 2),
        None,
    ]


def test_history_payload_names_each_message_turn_and_reports_inherited_origin() -> None:
    messages = [
        {
            "role": "user",
            "contents": [{"type": "text", "text": "here"}],
            "additional_properties": {ANALYTICS_ITEM_ID_KEY: "item-1"},
        },
        {"role": "assistant", "contents": [{"type": "text", "text": "answer"}]},
    ]
    records = [
        TurnRecord(turn_number=1, turn_id="turn-a", opening_item_id="item-1", user_message_id=_CLIENT_MESSAGE_ID),
        TurnRecord(turn_number=2, turn_id="turn-b", origin_session_id="parent-session"),
    ]

    payload = session_history_payload("s1", messages, records)

    # Every turn the session knows is listed in start order, including one whose
    # messages are not in this transcript: record ``turn-b`` was inherited from
    # another session, and a client still needs its id to join what it replays.
    assert payload == {
        "sessionId": "s1",
        "turns": [
            {"turnId": "turn-a", "turnNumber": 1, "userMessageId": _CLIENT_MESSAGE_ID},
            {"turnId": "turn-b", "turnNumber": 2, "userMessageId": None, "inheritedFrom": "parent-session"},
        ],
        "messages": [
            {"turnId": "turn-a", "turnNumber": 1, "message": messages[0]},
            {"turnId": "turn-a", "turnNumber": 1, "message": messages[1]},
        ],
    }


def test_history_payload_reports_a_forks_inherited_turns() -> None:
    messages = [
        {
            "role": "user",
            "contents": [{"type": "text", "text": "here"}],
            "additional_properties": {ANALYTICS_ITEM_ID_KEY: "item-1"},
        },
    ]
    records = [TurnRecord(turn_number=1, turn_id="turn-a", opening_item_id="item-1", origin_session_id="parent")]

    payload = session_history_payload("s1", messages, records)

    assert payload["turns"] == [{"turnId": "turn-a", "turnNumber": 1, "userMessageId": None, "inheritedFrom": "parent"}]


@pytest.mark.anyio
@pytest.mark.parametrize("message_id", [_CLIENT_MESSAGE_ID, _CLIENT_MESSAGE_ID.upper()], ids=["lowercase", "uppercase"])
async def test_prompt_preserves_client_message_id_through_replay(tmp_path, message_id: str) -> None:
    received: list[UserMessage] = []

    class _RecordingHost(_FakeHost):
        async def iter_turn_events(self, message: UserMessage):
            received.append(message)
            async for event in super().iter_turn_events(message):
                yield event

    host = _RecordingHost(event_bus=EventBus(), outcome=EndTurn())
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient())

    response = await server.prompt([text_block("go")], session_id="s1", message_id=message_id)

    assert len(received) == 1
    assert received[0].client_message_id == message_id
    assert response.user_message_id == message_id

    store = JsonFileStateStore(tmp_path / "sessions")
    await _save(
        store,
        tmp_path,
        [_user_message(received[0].text, "item-1")],
        [
            TurnRecord(
                turn_number=1,
                turn_id="turn-a",
                opening_item_id="item-1",
                user_message_id=received[0].client_message_id,
            )
        ],
    )

    updates = await _replay(store)

    assert len(updates) == 1
    assert updates[0].session_update == "user_message_chunk"
    assert updates[0].message_id == message_id


@pytest.mark.anyio
async def test_prompt_mints_a_user_message_id_when_the_client_sends_none() -> None:
    host = _FakeHost(event_bus=EventBus(), outcome=Cancelled())
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient())

    response = await server.prompt([text_block("go")], session_id="s1")

    # ACP lets the client omit it, but the turn still owns an id: replay
    # reports it back as this turn's ``userMessageId``.
    assert response.user_message_id is not None
    assert uuid.UUID(response.user_message_id)


@pytest.mark.anyio
async def test_prompt_refuses_a_message_id_that_is_not_a_uuid() -> None:
    host = _FakeHost(event_bus=EventBus(), outcome=Cancelled())
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient())

    with pytest.raises(RequestError) as exc_info:
        await server.prompt([text_block("go")], session_id="s1", message_id="m1")

    assert exc_info.value.data == {"details": "messageId must be a UUID."}
