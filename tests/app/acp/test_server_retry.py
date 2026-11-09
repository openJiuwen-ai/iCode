# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
"""``session/retry`` — resuming a failed or interrupted turn over ACP.

An ACP retry is the existing TUI retry reached from another frontend: the same
``UserRetry`` event, the same engine admission. These tests pin the two things
the ACP surface adds — the eligibility refusal before anything runs, and the
response naming the turn that was resumed rather than a new one.
"""

from __future__ import annotations

from typing import Any

import pytest
from acp import RequestError
from acp.helpers import text_block

from chrys.app.acp.server import ChrysAcpServer
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    Error,
    InvocationAnswerWithdrawn,
    InvocationContentDelta,
    InvocationMessage,
    InvocationSteered,
    InvocationToolCallStart,
    TurnStarted,
)
from chrys.foundation.models.invocations import ContentChannel, InvocationOrigin
from chrys.orchestration.session_host import Cancelled, EndTurn, Errored, Refused
from tests.app.acp._server_fakes import _FakeClient, _FakeEngine, _FakeHost, _FakeManager

_TURN_ID = "4" * 32


def _server(host: _FakeHost, *, client: _FakeClient | None = None) -> ChrysAcpServer:
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client if client is not None else _FakeClient())
    return server


def _retry_host(*, can_retry: bool, outcome: object) -> _FakeHost:
    """A host that answers a retry pass with one resumed turn."""
    engine = _FakeEngine()
    engine.can_retry = can_retry
    return _FakeHost(
        event_bus=EventBus(),
        engine=engine,
        retry_events=[
            TurnStarted(turn_id=_TURN_ID, turn_number=3, is_retry=True, session_id="s1"),
            InvocationMessage(
                text="answer",
                is_final=True,
                session_id="s1",
                origin=InvocationOrigin("turn", "s1", _TURN_ID, None),
            ),
        ],
        outcome=outcome,
    )


@pytest.mark.anyio
async def test_session_retry_resumes_the_same_turn_and_labels_it_a_retry() -> None:
    host = _retry_host(can_retry=True, outcome=EndTurn("answer"))
    client = _FakeClient()

    result = await _server(host, client=client).ext_method("session/retry", {"sessionId": "s1", "text": "try again"})

    assert host.retry_texts == ["try again"]
    turn_started = [params for method, params in client.ext_notifications if method == "chrys/turn_started"]
    assert len(turn_started) == 1
    assert turn_started[0]["isRetry"] is True
    assert turn_started[0]["turnId"] == _TURN_ID
    assert result["stopReason"] == "end_turn"
    assert result["_meta"]["chrys"]["turnId"] == _TURN_ID
    assert result["_meta"]["chrys"]["turnNumber"] == 3


@pytest.mark.anyio
async def test_session_retry_carries_no_user_message_id() -> None:
    """A retry opens no new user message, so it must not acknowledge one."""
    host = _retry_host(can_retry=True, outcome=EndTurn("answer"))

    result = await _server(host).ext_method("session/retry", {"sessionId": "s1", "text": "try again"})

    assert "userMessageId" not in result


@pytest.mark.anyio
async def test_session_retry_without_text_still_resumes_the_turn() -> None:
    host = _retry_host(can_retry=True, outcome=EndTurn("answer"))

    result = await _server(host).ext_method("session/retry", {"sessionId": "s1"})

    assert host.retry_texts == [None]
    assert result["_meta"]["chrys"]["turnId"] == _TURN_ID


@pytest.mark.anyio
async def test_session_retry_refuses_a_session_with_no_turn_to_resume() -> None:
    """The eligibility check runs before anything is published."""
    host = _retry_host(can_retry=False, outcome=EndTurn("answer"))

    with pytest.raises(RequestError) as exc_info:
        await _server(host).ext_method("session/retry", {"sessionId": "s1"})

    assert exc_info.value.data == {"details": "Session s1 has no turn that can be retried."}
    # The message a client shows by itself says why, not just "Invalid params".
    assert (exc_info.value.code, str(exc_info.value)) == (-32602, "Session s1 has no turn that can be retried.")
    assert host.retry_texts == []


@pytest.mark.anyio
async def test_session_retry_refuses_a_session_with_a_turn_in_flight() -> None:
    """A retry arriving mid-turn is refused, never queued behind the running turn.

    Queued, it would wait for the running turn to finish and then start a paid
    retry pass long after the client gave up on the request. The session admits
    one turn at a time, and an admitted turn counts from the moment it is
    admitted — not from the moment its work acquires the lock.
    """
    host = _retry_host(can_retry=True, outcome=EndTurn("answer"))
    server = _server(host)
    session = server._manager.get("s1")

    with pytest.raises(RequestError) as exc_info:
        async with session.prompt_lock:
            _ = session.reserve_turn()
            try:
                await server.ext_method("session/retry", {"sessionId": "s1"})
            finally:
                session.release_turn()

    assert exc_info.value.code == -32602
    assert exc_info.value.data == {"details": "Session s1 is running a turn; cancel it before retrying."}
    assert host.retry_texts == []


@pytest.mark.anyio
async def test_session_retry_and_second_prompt_refuse_each_other_while_a_turn_is_admitted() -> None:
    """One turn per session: the second caller is told no, not made to wait.

    Both requests admit a turn before they touch the lock, so whichever arrives
    second fails immediately — including in the window where the first turn has
    the lock but the second has not yet asked for it.
    """
    host = _retry_host(can_retry=True, outcome=EndTurn("answer"))
    server = _server(host)
    session = server._manager.get("s1")

    async with session.prompt_lock:
        _ = session.reserve_turn()
        try:
            with pytest.raises(RequestError) as exc_info:
                await server.prompt([text_block("second")], session_id="s1")
        finally:
            session.release_turn()

    assert exc_info.value.code == -32602
    assert exc_info.value.data == {"details": "Session s1 is running a turn."}
    assert host.retry_texts == []


@pytest.mark.anyio
async def test_session_retry_reports_a_cancelled_retry_as_cancelled() -> None:
    host = _retry_host(can_retry=True, outcome=Cancelled(reason="stopped"))

    result = await _server(host).ext_method("session/retry", {"sessionId": "s1"})

    assert result["stopReason"] == "cancelled"
    assert result["_meta"]["chrys"]["turnId"] == _TURN_ID


@pytest.mark.anyio
async def test_session_retry_maps_a_failed_retry_to_a_request_error_with_its_turn() -> None:
    host = _retry_host(
        can_retry=True,
        outcome=Errored(error=Error(code="model_failed", message="provider stopped")),
    )

    with pytest.raises(RequestError) as exc_info:
        await _server(host).ext_method("session/retry", {"sessionId": "s1"})

    assert exc_info.value.data["code"] == "model_failed"
    assert exc_info.value.data["turnId"] == _TURN_ID


@pytest.mark.anyio
async def test_session_retry_reports_a_refused_note_as_refusal() -> None:
    error = Error(code="hook_blocked", message="hook 'notes' blocked: not this note", refused_input="retry_note")
    host = _retry_host(can_retry=True, outcome=Refused(error=error))
    client = _FakeClient()

    result = await _server(host, client=client).ext_method("session/retry", {"sessionId": "s1", "text": "a note"})

    assert result["stopReason"] == "refusal"
    assert "userMessageId" not in result
    assert (result["_meta"]["chrys"]["code"], result["_meta"]["chrys"]["message"]) == (
        "hook_blocked",
        "hook 'notes' blocked: not this note",
    )
    notices = [
        u.update.content.text
        for u in client.updates
        if u.update.session_update == "agent_message_chunk" and u.update.content.text.startswith("Input rejected")
    ]
    assert notices == ["Input rejected: hook 'notes' blocked: not this note"]


def _origin() -> InvocationOrigin:
    return InvocationOrigin("turn", "s1", _TURN_ID, None)


def _withdrawn(*segment_ids: str, origin: InvocationOrigin | None = None) -> InvocationAnswerWithdrawn:
    return InvocationAnswerWithdrawn(
        origin=origin if origin is not None else _origin(), session_id="s1", segment_ids=segment_ids
    )


def _delta(segment_id: str, text: str, channel: ContentChannel = "answer") -> InvocationContentDelta:
    return InvocationContentDelta(origin=_origin(), session_id="s1", segment_id=segment_id, channel=channel, text=text)


class _OrderedClient(_FakeClient):
    """Also records what the server sent, updates and extension notifications, in one order."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        await super().session_update(session_id, update)
        self.sent.append(update.session_update)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        await super().ext_notification(method, params)
        self.sent.append(method)


def _cut_off_turn_host(*withdrawn: str) -> _FakeHost:
    """A turn cancelled while it wrote its answer after a tool call; each retry withdraws *withdrawn* and writes again."""
    engine = _FakeEngine()
    engine.can_retry = True
    return _FakeHost(
        event_bus=EventBus(),
        engine=engine,
        events=[
            TurnStarted(turn_id=_TURN_ID, turn_number=3, session_id="s1"),
            _delta("intro", "Let me look."),
            InvocationToolCallStart(tool_name="read_file", call_id="c1", origin=_origin()),
            _delta("think", "Hmm", "reasoning"),
            _delta("answer", "The result "),
            _delta("answer", "is"),
        ],
        retry_events=[
            TurnStarted(turn_id=_TURN_ID, turn_number=3, is_retry=True, session_id="s1"),
            *([_withdrawn(*withdrawn)] if withdrawn else []),
            _delta("again", "The result is 42."),
        ],
        outcome=Cancelled(),
    )


def _answer_ids(client: _FakeClient) -> dict[str, str]:
    """Each answer chunk's text by its message's ``messageId``, joined per message."""
    texts: dict[str, str] = {}
    for notification in client.updates:
        update = notification.update
        if update.session_update == "agent_message_chunk":
            texts[update.message_id] = texts.get(update.message_id, "") + update.content.text
    return texts


def _retractions(client: _FakeClient) -> list[dict[str, Any]]:
    return [params for method, params in client.ext_notifications if method == "chrys/messages_retracted"]


@pytest.mark.anyio
async def test_a_bare_retry_withdraws_the_answer_its_turn_ended_with_before_writing_it_again() -> None:
    client = _OrderedClient()
    server = _server(_cut_off_turn_host("answer"), client=client)
    await server.prompt([text_block("go")], session_id="s1")
    intro_id, answer_id = _answer_ids(client)
    assert _answer_ids(client) == {intro_id: "Let me look.", answer_id: "The result is"}

    sent_before = len(client.sent)
    await server.ext_method("session/retry", {"sessionId": "s1"})

    # The text before the tool call and the thought stay; the cut-off answer goes before the new one streams.
    assert _retractions(client) == [{"sessionId": "s1", "turnId": _TURN_ID, "messageIds": [answer_id]}]
    assert client.sent[sent_before : sent_before + 3] == [
        "chrys/turn_started",
        "chrys/messages_retracted",
        "agent_message_chunk",
    ]


@pytest.mark.anyio
async def test_a_retry_the_backend_withdraws_nothing_from_retracts_nothing() -> None:
    """The server retracts only what the backend withdraws, never what a retry merely sent along."""
    client = _FakeClient()
    server = _server(_cut_off_turn_host(), client=client)
    await server.prompt([text_block("go")], session_id="s1")

    await server.ext_method("session/retry", {"sessionId": "s1", "text": "go on"})

    assert _retractions(client) == []


@pytest.mark.anyio
async def test_a_bare_retry_retracts_every_segment_the_backend_withdraws_in_one_notification() -> None:
    """Text before a call that never reached history (a provider-hosted one) is withdrawn with the answer."""
    client = _FakeClient()
    server = _server(_cut_off_turn_host("intro", "answer"), client=client)
    await server.prompt([text_block("go")], session_id="s1")
    intro_id, answer_id = _answer_ids(client)

    await server.ext_method("session/retry", {"sessionId": "s1"})

    assert [params["messageIds"] for params in _retractions(client)] == [[intro_id, answer_id]]


@pytest.mark.anyio
async def test_a_withdrawal_inside_a_sub_agent_retracts_no_chat_message() -> None:
    host = _cut_off_turn_host()
    child = InvocationOrigin("sub_agent", "s1", "child-run", _origin())
    host.retry_events.insert(1, _withdrawn("answer", origin=child))
    client = _FakeClient()
    server = _server(host, client=client)
    await server.prompt([text_block("go")], session_id="s1")

    await server.ext_method("session/retry", {"sessionId": "s1"})

    assert _retractions(client) == []


@pytest.mark.anyio
async def test_each_bare_retry_withdraws_only_the_answer_the_turn_before_it_ended_with() -> None:
    """A refused retry keeps the answer to withdraw; a retry that ran replaces it with its own."""
    host = _cut_off_turn_host("answer")
    client = _FakeClient()
    server = _server(host, client=client)
    await server.prompt([text_block("go")], session_id="s1")
    _intro_id, answer_id = _answer_ids(client)

    host.engine.can_retry = False
    with pytest.raises(RequestError):
        await server.ext_method("session/retry", {"sessionId": "s1"})
    host.engine.can_retry = True
    await server.ext_method("session/retry", {"sessionId": "s1"})
    first_again_id = list(_answer_ids(client))[-1]
    host.retry_events[1] = _withdrawn("again")
    await server.ext_method("session/retry", {"sessionId": "s1"})

    assert [params["messageIds"] for params in _retractions(client)] == [[answer_id], [first_again_id]]


@pytest.mark.anyio
async def test_a_new_turn_keeps_only_its_own_answer_messages_for_a_retry_to_withdraw() -> None:
    """A bare retry drops only the last turn's answer: the session no longer tracks the turns before it."""
    host = _cut_off_turn_host("answer")
    manager = _FakeManager(host)
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient())
    await server.prompt([text_block("go")], session_id="s1")
    host.events = [
        TurnStarted(turn_id="5" * 32, turn_number=4, session_id="s1"),
        _delta("next", "Next answer"),
    ]

    await server.prompt([text_block("next")], session_id="s1")

    assert set(manager.get("s1").answer_message_ids) == {"next"}


@pytest.mark.anyio
async def test_a_steer_retracts_the_answer_this_prompt_streamed_once_and_never_again() -> None:
    """A steer withdraws what the running prompt sent: its ids are still the bridge's, not yet the session's."""
    host = _cut_off_turn_host()
    host.events.extend([InvocationSteered(origin=_origin(), session_id="s1"), _withdrawn("answer"), _delta("b", "42.")])
    client = _FakeClient()
    server = _server(host, client=client)

    await server.prompt([text_block("go")], session_id="s1")

    intro_id, answer_id, steered_id = _answer_ids(client)
    assert _answer_ids(client)[steered_id] == "42."
    assert _retractions(client) == [{"sessionId": "s1", "turnId": _TURN_ID, "messageIds": [answer_id]}]
    # The withdrawn answer is no longer the turn's: a retry withdrawing the same segment retracts nothing more.
    host.retry_events = [
        TurnStarted(turn_id=_TURN_ID, turn_number=3, is_retry=True, session_id="s1"),
        _withdrawn("answer", "intro"),
    ]
    await server.ext_method("session/retry", {"sessionId": "s1"})
    assert [params["messageIds"] for params in _retractions(client)] == [[answer_id], [intro_id]]
