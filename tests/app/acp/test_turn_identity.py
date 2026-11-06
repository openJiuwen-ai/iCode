# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Turn identity on the ACP surface: ``_meta.chrys``, ``messageId`` and retry."""

from __future__ import annotations

from typing import Any

import pytest
from acp.helpers import text_block

from chrys.app.acp.server import ChrysAcpServer
from chrys.foundation.errors import ErrorKind
from chrys.foundation.errors.display import DISPLAY_WITH_HINT
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    InvocationContentDelta,
    InvocationRetryAttempt,
    TurnStarted,
    UnansweredInputDropped,
)
from chrys.foundation.models.failures import FailureFacts
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.trajectory.metadata import ANALYTICS_ITEM_ID_KEY
from chrys.orchestration.session_host import Cancelled
from chrys.service.state.store import SessionTranscript
from chrys.service.trajectory.state import TurnRecord
from tests.app.acp._server_fakes import _FakeClient, _FakeHost, _FakeManager

_CLIENT_MESSAGE_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
_NO_USAGE = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}


_ITEM_ID = "item-1"


class _SeededStore:
    """A state store whose single session carries the given turn registry."""

    def __init__(self, turns: list[TurnRecord]) -> None:
        self._turns = turns

    async def load_session_transcript(self, session_id: str, *, prefer_recovery: bool = False) -> SessionTranscript:
        _ = prefer_recovery
        return SessionTranscript(
            messages=[
                {
                    "role": "user",
                    "contents": [{"type": "text", "text": f"hello {session_id}"}],
                    "additional_properties": {ANALYTICS_ITEM_ID_KEY: _ITEM_ID},
                }
            ],
            turns=list(self._turns),
        )


def _origin() -> InvocationOrigin:
    return InvocationOrigin("turn", "s1", "turn-a", None)


def _turn_started(**overrides: Any) -> TurnStarted:
    fields: dict[str, Any] = {"session_id": "s1", "turn_id": "turn-a", "turn_number": 1}
    fields.update(overrides)
    return TurnStarted(**fields)


def _server(events: list[Any], *, turns: list[TurnRecord] | None = None) -> tuple[ChrysAcpServer, _FakeClient]:
    host = _FakeHost(event_bus=EventBus(), events=events, outcome=Cancelled())
    manager = _FakeManager(host)
    if turns is not None:
        manager.state_store = _SeededStore(turns)  # type: ignore[assignment]
    client = _FakeClient()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)
    return server, client


def _retries(client: _FakeClient) -> list[dict[str, Any]]:
    return [params for method, params in client.ext_notifications if method == "chrys/retry_attempt"]


@pytest.mark.anyio
async def test_one_turn_id_reaches_the_response_live_and_history() -> None:
    """The client joins its own transcript on one id: the same value everywhere."""
    server, client = _server(
        [
            _turn_started(user_message_id=_CLIENT_MESSAGE_ID, opening_item_id=_ITEM_ID),
            InvocationContentDelta(origin=_origin(), segment_id="seg-1", text="answer"),
        ],
        turns=[
            TurnRecord(turn_number=1, turn_id="turn-a", opening_item_id=_ITEM_ID, user_message_id=_CLIENT_MESSAGE_ID)
        ],
    )

    response = await server.prompt([text_block("go")], session_id="s1", message_id=_CLIENT_MESSAGE_ID)

    assert response.field_meta == {"chrys": {"turnId": "turn-a", "turnNumber": 1, "sessionUsage": _NO_USAGE}}
    assert client.ext_notifications == [
        (
            "chrys/turn_started",
            {
                "sessionId": "s1",
                "turnId": "turn-a",
                "turnNumber": 1,
                "isRetry": False,
                "userMessageId": _CLIENT_MESSAGE_ID,
                "openingItemId": _ITEM_ID,
            },
        )
    ]
    assert client.updates[0].update.field_meta == {
        "chrys": {"segmentId": "seg-1", "channel": "answer", "turnId": "turn-a", "turnNumber": 1}
    }

    history = await server.ext_method("session/history", {"sessionId": "s1"})
    assert history["turns"] == [{"turnId": "turn-a", "turnNumber": 1, "userMessageId": _CLIENT_MESSAGE_ID}]


@pytest.mark.anyio
async def test_a_run_with_no_turn_started_reports_no_turn_id() -> None:
    """Nothing opened a turn, so no id is invented and no notification claims one."""
    server, client = _server([])

    response = await server.prompt([text_block("go")], session_id="s1")

    # No turn opened, so no id is invented; the request still made no spend.
    assert response.field_meta == {"chrys": {"sessionUsage": _NO_USAGE}}
    assert client.ext_notifications == []


@pytest.mark.anyio
async def test_a_turn_retry_is_forwarded_with_the_turn_id() -> None:
    server, client = _server(
        [
            _turn_started(),
            InvocationRetryAttempt(
                origin=_origin(), message="retrying", attempt=2, max_attempts=10, delay_seconds=3, scope="wire"
            ),
        ]
    )

    await server.prompt([text_block("go")], session_id="s1")

    assert _retries(client) == [
        {
            "sessionId": "s1",
            "turnId": "turn-a",
            "attempt": 2,
            "maxAttempts": 10,
            "delaySeconds": 3,
            "scope": "wire",
            "message": "retrying",
            "kind": None,
            "statusCode": None,
            "hint": None,
        }
    ]


@pytest.mark.anyio
async def test_a_turn_retry_says_what_is_being_retried() -> None:
    """A classified failure's verdict rides the notice; it is being retried, so no ``retryable``."""
    server, client = _server(
        [
            _turn_started(),
            InvocationRetryAttempt(
                origin=_origin(),
                message="Error code: 429 - Rate limit reached",
                attempt=1,
                max_attempts=10,
                delay_seconds=3,
                scope="wire",
                display_message=DISPLAY_WITH_HINT.bind(message="Rate limited.", hint="Waiting."),
                facts=FailureFacts(kind=ErrorKind.RATE_LIMITED, retryable=True, status_code=429),
            ),
        ]
    )

    await server.prompt([text_block("go")], session_id="s1")

    (retry,) = _retries(client)
    assert {key: retry.get(key, "absent") for key in ("message", "kind", "retryable", "statusCode", "hint")} == {
        "message": "Error code: 429 - Rate limit reached",
        "kind": "rate_limited",
        "retryable": "absent",
        "statusCode": 429,
        "hint": "Rate limited. Waiting.",
    }


@pytest.mark.anyio
async def test_a_sub_agent_retry_is_not_forwarded_as_the_turns_retry() -> None:
    """Only the main turn's retries carry the turn id; a child names its own invocation."""
    server, client = _server(
        [
            _turn_started(),
            InvocationRetryAttempt(
                origin=InvocationOrigin("sub_agent", "s1", "sub-1", None),
                message="retrying",
                attempt=1,
                max_attempts=3,
                scope="run",
            ),
        ]
    )

    await server.prompt([text_block("go")], session_id="s1")

    assert _retries(client) == []


@pytest.mark.anyio
async def test_a_compaction_retry_is_forwarded_with_its_scope() -> None:
    """Compaction retries ride the same notification; the scope is what tells them apart."""
    server, client = _server(
        [
            _turn_started(),
            InvocationRetryAttempt(
                origin=_origin(), message="compacting", attempt=1, max_attempts=10, scope="compaction"
            ),
        ]
    )

    await server.prompt([text_block("go")], session_id="s1")

    assert [params["scope"] for params in _retries(client)] == ["compaction"]


@pytest.mark.anyio
async def test_a_retired_turn_is_announced_before_the_turn_that_reuses_its_number() -> None:
    """The client drops the retired turn by id; the replacement then takes its number."""
    server, client = _server(
        [
            UnansweredInputDropped(
                session_id="s1", retired_previous_turn=True, removed_item_ids=("opener",), retired_turn_id="turn-a"
            ),
            _turn_started(turn_id="turn-b", user_message_id=_CLIENT_MESSAGE_ID),
        ]
    )

    await server.prompt([text_block("go")], session_id="s1", message_id=_CLIENT_MESSAGE_ID)

    assert client.ext_notifications[0] == (
        "chrys/user_messages_dropped",
        {"sessionId": "s1", "itemIds": ["opener"], "retiredTurnId": "turn-a"},
    )
    assert [method for method, _params in client.ext_notifications] == [
        "chrys/user_messages_dropped",
        "chrys/turn_started",
    ]


@pytest.mark.anyio
async def test_cleanup_that_keeps_the_turn_names_the_user_messages_it_dropped() -> None:
    """The failed turn stays, but the notes it never answered leave history: the client drops them by item."""
    server, client = _server(
        [
            UnansweredInputDropped(session_id="s1", removed_item_ids=("note-1", "note-2")),
            _turn_started(user_message_id=_CLIENT_MESSAGE_ID),
        ]
    )

    await server.prompt([text_block("go")], session_id="s1", message_id=_CLIENT_MESSAGE_ID)

    assert client.ext_notifications[0] == (
        "chrys/user_messages_dropped",
        {"sessionId": "s1", "itemIds": ["note-1", "note-2"], "retiredTurnId": None},
    )
    assert [method for method, _params in client.ext_notifications] == [
        "chrys/user_messages_dropped",
        "chrys/turn_started",
    ]


@pytest.mark.anyio
async def test_cleanup_that_removes_nothing_sends_nothing() -> None:
    server, client = _server(
        [UnansweredInputDropped(session_id="s1"), _turn_started(user_message_id=_CLIENT_MESSAGE_ID)]
    )

    await server.prompt([text_block("go")], session_id="s1", message_id=_CLIENT_MESSAGE_ID)

    assert [method for method, _params in client.ext_notifications] == ["chrys/turn_started"]
