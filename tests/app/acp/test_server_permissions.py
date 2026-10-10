# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ACP permission bridging, pending-wait cancellation, and session teardown release ordering."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from acp import RequestError
from acp import schema as acp_schema
from acp.helpers import text_block

from chrys.app.acp.bridge import AcpEventBridge
from chrys.app.acp.server import ChrysAcpServer
from chrys.foundation.config.settings_store import load_settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ApprovalCancelled,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalReviewed,
    AskUserResponse,
    AskUserTimedOut,
    QuestionToUser,
)
from chrys.foundation.models.ask_user import AskUserQuestion
from chrys.orchestration.session_host import Cancelled, EndTurn
from tests.app.acp._server_fakes import _FakeClient, _FakeHost, _FakeManager, _FakeSession


@dataclass
class _BlockedPermission:
    """A prompt turn parked on an approval request the client never answers."""

    host: _FakeHost
    manager: _FakeManager
    server: ChrysAcpServer
    client: _FakeClient
    prompt_task: asyncio.Task[Any]
    responses: list[ApprovalResponse]


async def blocked_permission_prompt(
    *,
    text: str = "run",
    message_id: str | None = None,
    on_response: Callable[[ApprovalResponse], None] | None = None,
) -> _BlockedPermission:
    """Start a turn whose permission request wedges in the client, and return once it is pending.

    ``on_response`` runs inside the ``ApprovalResponse`` subscriber before the
    event is recorded, so delivery-ordering invariants can be asserted there.
    """
    started = asyncio.Event()

    async def _block_permission(_session_id: str, _tool_call: Any) -> acp_schema.RequestPermissionResponse:
        started.set()
        await asyncio.Future()
        raise AssertionError("unreachable after cancellation")

    host = _FakeHost(
        event_bus=EventBus(),
        events=[ApprovalRequest(request_id="r1", tool_name="bash", args={}, session_id="s1")],
        outcome=Cancelled(),
    )
    manager = _FakeManager(host)
    client = _FakeClient(permission_responder=_block_permission)
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)
    responses: list[ApprovalResponse] = []

    async def _collect(event: ApprovalResponse) -> None:
        if on_response is not None:
            on_response(event)
        responses.append(event)

    await host.event_bus.subscribe(ApprovalResponse, _collect)
    prompt_task = asyncio.create_task(server.prompt([text_block(text)], session_id="s1", message_id=message_id))
    await asyncio.wait_for(started.wait(), timeout=1)
    return _BlockedPermission(host, manager, server, client, prompt_task, responses)


@pytest.mark.anyio
async def test_manual_approval_request_bridges_to_acp_permission() -> None:
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)
    responses: list[ApprovalResponse] = []

    async def _collect(event: ApprovalResponse) -> None:
        responses.append(event)

    await host.event_bus.subscribe(ApprovalResponse, _collect)

    await server._handle_event(
        "s1",
        ApprovalRequest(
            request_id="r1",
            tool_name="bash",
            tool_kind="shell",
            args={"command": "rm file"},
            intent_summary="Run shell command",
            session_id="s1",
        ),
        AcpEventBridge(),
        {},
    )

    assert len(client.permission_requests) == 1
    tool_call = client.permission_requests[0].tool_call
    assert tool_call.tool_call_id == "r1"
    assert tool_call.title == "Run shell command"
    assert tool_call.kind == "execute"
    assert tool_call.field_meta == {"chrys": {"tool_name": "bash", "tool_kind": "shell"}}
    assert len(responses) == 1
    assert responses[0].request_id == "r1"
    assert responses[0].approved is True
    assert responses[0].reason == ""
    assert responses[0].session_id == "s1"


@pytest.mark.anyio
async def test_permission_title_falls_back_to_shell_command() -> None:
    """Without an intent summary the title is the command itself, not
    "Execute zsh" — the shell tool's name is the bare shell binary."""
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    await server._handle_event(
        "s1",
        ApprovalRequest(
            request_id="r1",
            tool_name="zsh",
            tool_kind="shell",
            args={"command": "cloc --vcs=git ."},
            session_id="s1",
        ),
        AcpEventBridge(),
        {},
    )

    assert len(client.permission_requests) == 1
    assert client.permission_requests[0].tool_call.title == "cloc --vcs=git ."


@pytest.mark.anyio
async def test_auto_approval_judging_request_waits_for_flagged_review() -> None:
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient(option_id="reject")
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)
    pending: dict[str, ApprovalRequest] = {}
    responses: list[ApprovalResponse] = []

    async def _collect(event: ApprovalResponse) -> None:
        responses.append(event)

    await host.event_bus.subscribe(ApprovalResponse, _collect)
    request = ApprovalRequest(
        request_id="r1",
        tool_name="write_file",
        args={"path": "a.py"},
        intent_summary="Write file",
        session_id="s1",
        judging=True,
    )

    await server._handle_event("s1", request, AcpEventBridge(), pending)
    assert client.permission_requests == []
    assert pending == {"r1": request}

    await server._handle_event(
        "s1",
        ApprovalReviewed(request_id="r1", approved=False, reason="Risky", session_id="s1"),
        AcpEventBridge(),
        pending,
    )

    assert len(client.permission_requests) == 1
    assert len(responses) == 1
    assert responses[0].request_id == "r1"
    assert responses[0].approved is False
    assert responses[0].reason == "Rejected by ACP client."
    assert responses[0].session_id == "s1"


@pytest.mark.anyio
async def test_cancel_resolves_pending_request_input() -> None:
    started = asyncio.Event()

    async def _block_input(_method: str, _params: dict[str, Any]) -> dict[str, Any]:
        started.set()
        await asyncio.Future()
        return {"answers": [{"values": ["too late"], "note": ""}], "cancelled": False}

    host = _FakeHost(
        event_bus=EventBus(),
        events=[QuestionToUser(request_id="q1", questions=(AskUserQuestion("Continue?"),), session_id="s1")],
        outcome=Cancelled(),
    )
    manager = _FakeManager(host)
    client = _FakeClient(input_responder=_block_input)
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)
    responses: list[AskUserResponse] = []

    async def _collect(event: AskUserResponse) -> None:
        responses.append(event)

    await host.event_bus.subscribe(AskUserResponse, _collect)
    prompt_task = asyncio.create_task(server.prompt([text_block("ask")], session_id="s1", message_id="m1"))
    await asyncio.wait_for(started.wait(), timeout=1)

    await server.cancel("s1")
    response = await asyncio.wait_for(prompt_task, timeout=1)

    assert response.stop_reason == "cancelled"
    assert manager.cancelled_sessions == ["s1"]
    assert client.input_requests[0][0] == "chrys/request_input"
    assert len(responses) == 1
    assert responses[0].request_id == "q1"
    assert responses[0].cancelled is True


@pytest.mark.anyio
async def test_cancel_interrupts_before_rejecting_pending_permission_and_allows_following_turn() -> None:
    def _engine_interrupted_first(_event: ApprovalResponse) -> None:
        assert pending.manager.cancelled_sessions == ["s1"], "engine interruption must precede bridge rejection"

    pending = await blocked_permission_prompt(message_id="m1", on_response=_engine_interrupted_first)

    await pending.server.cancel("s1")
    response = await asyncio.wait_for(pending.prompt_task, timeout=1)

    assert response.stop_reason == "cancelled"
    assert len(pending.responses) == 1
    assert pending.responses[0].approved is False
    assert pending.responses[0].reason == "ACP permission request was cancelled."
    assert pending.server._pending_permission_cancels == {}
    assert pending.server._pending_permission_tasks == {}

    pending.host.events = []
    pending.host.outcome = EndTurn()
    following = await asyncio.wait_for(
        pending.server.prompt([text_block("next")], session_id="s1", message_id="m2"),
        timeout=1,
    )
    assert following.stop_reason == "end_turn"


@pytest.mark.anyio
async def test_late_permission_allow_after_cancel_is_ignored() -> None:
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_late_reply = asyncio.Event()
    late_reply_returned = asyncio.Event()

    async def _late_allow(_session_id: str, _tool_call: Any) -> acp_schema.RequestPermissionResponse:
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release_late_reply.wait()
        late_reply_returned.set()
        return acp_schema.RequestPermissionResponse(
            outcome=acp_schema.AllowedOutcome(outcome="selected", optionId="allow")
        )

    host = _FakeHost(
        event_bus=EventBus(),
        events=[ApprovalRequest(request_id="r1", tool_name="bash", args={}, session_id="s1")],
        outcome=Cancelled(),
    )
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient(permission_responder=_late_allow))
    responses: list[ApprovalResponse] = []

    async def _collect(event: ApprovalResponse) -> None:
        responses.append(event)

    await host.event_bus.subscribe(ApprovalResponse, _collect)
    prompt_task = asyncio.create_task(server.prompt([text_block("run")], session_id="s1"))
    await asyncio.wait_for(started.wait(), timeout=1)

    await server.cancel("s1")
    await asyncio.wait_for(prompt_task, timeout=1)
    await asyncio.wait_for(cancellation_seen.wait(), timeout=1)
    release_late_reply.set()
    await asyncio.wait_for(late_reply_returned.wait(), timeout=1)
    await asyncio.sleep(0)

    assert len(responses) == 1
    assert responses[0].approved is False


@pytest.mark.anyio
async def test_wedged_permission_client_times_out_with_configured_seconds() -> None:
    async def _never_reply(_session_id: str, _tool_call: Any) -> acp_schema.RequestPermissionResponse:
        await asyncio.Future()
        raise AssertionError("unreachable after timeout")

    host = _FakeHost(
        event_bus=EventBus(),
        events=[ApprovalRequest(request_id="r1", tool_name="bash", args={}, session_id="s1")],
        outcome=EndTurn(),
    )
    server = ChrysAcpServer(  # type: ignore[arg-type]
        _FakeManager(host),
        initial_vision=False,
        permission_timeout_seconds=load_settings(
            env={"CHRYS_APPROVAL_TIMEOUT_SECONDS": "1"}
        ).settings.approval_timeout_seconds,
    )
    server.on_connect(_FakeClient(permission_responder=_never_reply))
    responses: list[ApprovalResponse] = []

    async def _collect(event: ApprovalResponse) -> None:
        responses.append(event)

    await host.event_bus.subscribe(ApprovalResponse, _collect)

    response = await asyncio.wait_for(server.prompt([text_block("run")], session_id="s1"), timeout=3)

    assert response.stop_reason == "end_turn"
    assert len(responses) == 1
    assert responses[0].approved is False
    assert responses[0].reason == "ACP permission request timed out."
    assert server._pending_permission_cancels == {}
    assert server._pending_permission_tasks == {}


@pytest.mark.anyio
async def test_wedged_permission_in_one_session_does_not_block_another_session_prompt() -> None:
    permission_started = asyncio.Event()

    async def _block_permission(_session_id: str, _tool_call: Any) -> acp_schema.RequestPermissionResponse:
        permission_started.set()
        await asyncio.Future()
        raise AssertionError("unreachable after cancellation")

    first_host = _FakeHost(
        event_bus=EventBus(),
        events=[ApprovalRequest(request_id="r1", tool_name="bash", args={}, session_id="s1")],
        outcome=Cancelled(),
    )
    second_host = _FakeHost(event_bus=EventBus(), events=[], outcome=EndTurn())
    sessions = {
        "s1": _FakeSession(host=first_host, session_id="s1"),
        "s2": _FakeSession(host=second_host, session_id="s2"),
    }

    class _MultiSessionManager:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        def get(self, session_id: str) -> _FakeSession:
            return sessions[session_id]

        async def cancel(self, session_id: str) -> None:
            self.cancelled.append(session_id)

    manager = _MultiSessionManager()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient(permission_responder=_block_permission))
    first_prompt = asyncio.create_task(server.prompt([text_block("block")], session_id="s1"))
    await asyncio.wait_for(permission_started.wait(), timeout=1)

    second_response = await asyncio.wait_for(
        server.prompt([text_block("independent")], session_id="s2"),
        timeout=0.2,
    )

    assert second_response.stop_reason == "end_turn"
    await server.cancel("s1")
    first_response = await asyncio.wait_for(first_prompt, timeout=1)
    assert first_response.stop_reason == "cancelled"
    assert manager.cancelled == ["s1"]


@pytest.mark.anyio
async def test_permission_cancel_tracking_is_scoped_by_session_and_request_id() -> None:
    server = ChrysAcpServer(  # type: ignore[arg-type]
        _FakeManager(_FakeHost(event_bus=EventBus())),
        initial_vision=False,
    )
    loop = asyncio.get_running_loop()
    first = loop.create_future()
    second = loop.create_future()
    server._pending_permission_cancels[("s1", "same-request")] = first
    server._pending_permission_cancels[("s2", "same-request")] = second

    server._cancel_pending_waits("s1")

    assert first.done()
    assert not second.done()
    second.cancel()


@pytest.mark.parametrize("kind", ["permission", "ask_user"])
@pytest.mark.anyio
async def test_out_of_band_cancellation_tombstone_prevents_lost_early_cancel(kind: str) -> None:
    """An out-of-band cancellation that arrives before its wait is installed
    must land as a tombstone the later request consumes, for either wait kind."""
    permission = kind == "permission"
    request_id = "early" if permission else "early-input"
    host = _FakeHost(event_bus=EventBus())
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    client = _FakeClient()
    server.on_connect(client)
    responses: list[Any] = []

    async def _collect(event: Any) -> None:
        responses.append(event)

    def tombstones() -> set[tuple[str, str]]:
        return server._permission_cancel_tombstones if permission else server._input_cancel_tombstones

    await host.event_bus.subscribe(ApprovalResponse if permission else AskUserResponse, _collect)
    await server._watch_nested_wait_cancellations(_FakeSession(host=host))  # type: ignore[arg-type]
    if permission:
        await host.event_bus.publish(ApprovalCancelled(request_id=request_id, session_id="s1"))
    else:
        await host.event_bus.publish(AskUserTimedOut(request_id=request_id, session_id="s1"))
    assert ("s1", request_id) in tombstones()

    if permission:
        wait = server._request_permission(
            "s1",
            ApprovalRequest(
                request_id=request_id,
                tool_name="zsh",
                tool_kind="shell",
                args={"cmd": "echo"},
                session_id="s1",
            ),
        )
    else:
        wait = server._request_input(
            "s1",
            QuestionToUser(
                request_id=request_id,
                questions=(AskUserQuestion("Continue?"),),
                session_id="s1",
            ),
        )
    await asyncio.wait_for(wait, timeout=1)

    assert ("s1", request_id) not in tombstones()
    assert (server._pending_permission_cancels if permission else server._pending_input_cancels) == {}
    assert len(responses) == 1
    if permission:
        assert responses[0].approved is False
        assert responses[0].reason == "ACP permission request was cancelled."
    else:
        assert responses[0].cancelled is True
    # The wait was already dead — the client must never see a request for it
    # (a sent request would linger as a stale dialog; nothing revokes it).
    assert (client.permission_requests if permission else client.input_requests) == []


@pytest.mark.anyio
async def test_prompt_turn_end_sweeps_tombstone_of_judge_aborted_approval() -> None:
    host = _FakeHost(event_bus=EventBus(), outcome=Cancelled())
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    client = _FakeClient()
    server.on_connect(client)
    await server._watch_nested_wait_cancellations(_FakeSession(host=host))  # type: ignore[arg-type]

    async def _judged_then_cancelled(_message: Any):
        # A judging approval installs no cancel waiter (it waits in
        # pending_approvals for ApprovalReviewed). Cancellation aborts the
        # judge, so the out-of-band ApprovalCancelled has nothing to resolve
        # and lands as a tombstone no later request will ever consume.
        yield ApprovalRequest(
            request_id="judged",
            tool_name="zsh",
            tool_kind="shell",
            args={"cmd": "echo"},
            judging=True,
            session_id="s1",
        )
        await host.event_bus.publish(ApprovalCancelled(request_id="judged", session_id="s1"))
        assert ("s1", "judged") in server._permission_cancel_tombstones
        host.last_turn_outcome = host.outcome

    host.iter_turn_events = _judged_then_cancelled  # type: ignore[method-assign]

    response = await asyncio.wait_for(server.prompt([text_block("run")], session_id="s1"), timeout=1)

    assert response.stop_reason == "cancelled"
    assert client.permission_requests == []
    assert server._permission_cancel_tombstones == set()
    assert server._input_cancel_tombstones == set()


@pytest.mark.anyio
async def test_session_teardown_clears_nested_wait_cancellation_tombstones() -> None:
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server._permission_cancel_tombstones.update({("s1", "p"), ("s2", "keep")})
    server._input_cancel_tombstones.update({("s1", "q"), ("s2", "keep")})

    await server.close_session("s1")

    assert server._permission_cancel_tombstones == {("s2", "keep")}
    assert server._input_cancel_tombstones == {("s2", "keep")}


@pytest.mark.parametrize("lifecycle", ["close", "delete"])
@pytest.mark.anyio
async def test_session_teardown_cancelled_during_wait_release_still_clears_cancellation_state(
    lifecycle: str,
) -> None:
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)

    async def _cancel_and_die(session_id: str) -> None:
        _ = session_id
        raise asyncio.CancelledError

    manager.cancel = _cancel_and_die  # type: ignore[method-assign]
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient())
    server._permission_cancel_tombstones.add(("s1", "p"))
    server._input_cancel_tombstones.add(("s1", "q"))

    with pytest.raises(asyncio.CancelledError):
        if lifecycle == "close":
            await server.close_session("s1")
        else:
            await server.delete_session("s1")

    assert server._permission_cancel_tombstones == set()
    assert server._input_cancel_tombstones == set()
    assert manager.closed_sessions == []
    assert manager.deleted_sessions == []


@pytest.mark.anyio
async def test_close_releases_pending_ask_user_before_waiting_for_prompt_lock() -> None:
    started = asyncio.Event()

    async def _block_input(_method: str, _params: dict[str, Any]) -> dict[str, Any]:
        started.set()
        await asyncio.Future()
        return {"answers": [{"values": ["too late"], "note": ""}], "cancelled": False}

    host = _FakeHost(
        event_bus=EventBus(),
        events=[QuestionToUser(request_id="q1", questions=(AskUserQuestion("Continue?"),), session_id="s1")],
        outcome=Cancelled(),
    )
    manager = _FakeManager(host)
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient(input_responder=_block_input))
    prompt_task = asyncio.create_task(server.prompt([text_block("ask")], session_id="s1"))
    await asyncio.wait_for(started.wait(), timeout=1)

    await asyncio.wait_for(server.close_session("s1"), timeout=1)
    response = await asyncio.wait_for(prompt_task, timeout=1)

    assert response.stop_reason == "cancelled"
    assert manager.lifecycle_calls == ["begin_close:s1", "cancel:s1", "close:s1"]
    assert server._pending_input_cancels == {}


@pytest.mark.anyio
async def test_close_releases_pending_permission_before_waiting_for_prompt_lock() -> None:
    pending = await blocked_permission_prompt()

    await asyncio.wait_for(pending.server.close_session("s1"), timeout=1)
    response = await asyncio.wait_for(pending.prompt_task, timeout=1)

    assert response.stop_reason == "cancelled"
    assert pending.manager.lifecycle_calls == ["begin_close:s1", "cancel:s1", "close:s1"]
    assert len(pending.responses) == 1
    assert pending.responses[0].approved is False
    assert pending.server._pending_permission_cancels == {}
    assert pending.server._pending_permission_tasks == {}


@pytest.mark.anyio
async def test_delete_releases_pending_permission_before_waiting_for_prompt_lock() -> None:
    pending = await blocked_permission_prompt()

    await asyncio.wait_for(pending.server.delete_session("s1", cwd="/tmp/project"), timeout=1)
    response = await asyncio.wait_for(pending.prompt_task, timeout=1)

    assert response.stop_reason == "cancelled"
    assert pending.manager.lifecycle_calls == ["begin_delete:s1", "cancel:s1", "delete:s1"]
    assert pending.manager.deleted_sessions == [("/tmp/project", "s1")]
    assert len(pending.responses) == 1
    assert pending.responses[0].approved is False
    assert pending.server._pending_permission_cancels == {}
    assert pending.server._pending_permission_tasks == {}


@pytest.mark.anyio
async def test_delete_by_short_id_releases_waits_under_the_canonical_session_id() -> None:
    pending = await blocked_permission_prompt()

    await asyncio.wait_for(pending.server.delete_session("short1", cwd="/tmp/project"), timeout=1)
    response = await asyncio.wait_for(pending.prompt_task, timeout=1)

    assert response.stop_reason == "cancelled"
    assert pending.manager.lifecycle_calls == ["begin_delete:short1", "cancel:s1", "delete:s1"]
    assert pending.manager.deleted_sessions == [("/tmp/project", "s1")]
    assert pending.server._pending_permission_cancels == {}
    assert pending.server._pending_permission_tasks == {}


@pytest.mark.anyio
async def test_delete_rejected_for_workspace_scope_does_not_interrupt_the_active_turn() -> None:
    pending = await blocked_permission_prompt()

    with pytest.raises(RequestError):
        await pending.server.delete_session("s1", cwd="/somewhere/else")

    assert pending.manager.lifecycle_calls == ["begin_delete:s1"]
    assert pending.manager.cancelled_sessions == []
    assert pending.manager.deleted_sessions == []
    assert pending.manager._session.closing is False
    assert list(pending.server._pending_permission_cancels) == [("s1", "r1")]

    await pending.server.cancel("s1")
    response = await asyncio.wait_for(pending.prompt_task, timeout=1)
    assert response.stop_reason == "cancelled"


@pytest.mark.anyio
async def test_delete_rejects_a_queued_prompt_instead_of_admitting_it_into_a_new_turn() -> None:
    pending = await blocked_permission_prompt(text="one")
    second_prompt = asyncio.create_task(pending.server.prompt([text_block("two")], session_id="s1"))
    # Pure scheduler ticks (no wall-clock) so the second prompt reaches its
    # prompt-lock wait before the delete starts.
    for _ in range(5):
        await asyncio.sleep(0)
    assert not second_prompt.done()

    # Deleting must release the first prompt's permission wait AND reject the
    # queued second prompt: without the closing mark set during validation,
    # the second prompt would be admitted the moment the first drains, and
    # the teardown would wedge behind its brand-new turn.
    await asyncio.wait_for(pending.server.delete_session("s1", cwd="/tmp/project"), timeout=1)

    first_response = await asyncio.wait_for(pending.prompt_task, timeout=1)
    assert first_response.stop_reason == "cancelled"
    with pytest.raises(RequestError):
        await asyncio.wait_for(second_prompt, timeout=1)
    assert pending.manager.lifecycle_calls == ["begin_delete:s1", "cancel:s1", "delete:s1"]
    assert pending.manager.deleted_sessions == [("/tmp/project", "s1")]
    assert pending.server._pending_permission_cancels == {}
    assert pending.server._pending_permission_tasks == {}


@pytest.mark.anyio
async def test_cancelled_permission_request_rejects_tool_call() -> None:
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient(permission_outcome=acp_schema.DeniedOutcome(outcome="cancelled"))
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)
    responses: list[ApprovalResponse] = []

    async def _collect(event: ApprovalResponse) -> None:
        responses.append(event)

    await host.event_bus.subscribe(ApprovalResponse, _collect)

    await server._handle_event(
        "s1",
        ApprovalRequest(
            request_id="r1", tool_name="bash", args={}, intent_summary="Run shell command", session_id="s1"
        ),
        AcpEventBridge(),
        {},
    )

    assert len(responses) == 1
    assert responses[0].approved is False
    assert responses[0].reason == "ACP permission request was cancelled."


@pytest.mark.anyio
async def test_failed_permission_request_rejects_tool_call() -> None:
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient(permission_exc=RuntimeError("client gone"))
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)
    responses: list[ApprovalResponse] = []

    async def _collect(event: ApprovalResponse) -> None:
        responses.append(event)

    await host.event_bus.subscribe(ApprovalResponse, _collect)

    await server._handle_event(
        "s1",
        ApprovalRequest(
            request_id="r1", tool_name="bash", args={}, intent_summary="Run shell command", session_id="s1"
        ),
        AcpEventBridge(),
        {},
    )

    assert len(responses) == 1
    assert responses[0].approved is False
    assert responses[0].reason == "ACP permission request failed or was cancelled."


@pytest.mark.anyio
@pytest.mark.parametrize("explicit_zero", [False, True])
async def test_unlimited_permission_wait_accepts_a_later_response(monkeypatch, explicit_zero: bool) -> None:
    from types import ModuleType

    from chrys.app.acp import server as server_module

    entered = asyncio.Event()
    release = asyncio.Event()
    timeouts: list[float | None] = []
    real_wait = asyncio.wait

    async def observed_wait(tasks, *, timeout, return_when):
        timeouts.append(timeout)
        entered.set()
        return await real_wait(tasks, timeout=timeout, return_when=return_when)

    shadow = ModuleType("asyncio")
    shadow.__dict__.update(vars(asyncio))
    shadow.wait = observed_wait
    monkeypatch.setattr(server_module, "asyncio", shadow)

    async def reply_later(_session_id: str, _tool_call: Any) -> acp_schema.RequestPermissionResponse:
        await release.wait()
        return acp_schema.RequestPermissionResponse(
            outcome=acp_schema.AllowedOutcome(outcome="selected", optionId="allow")
        )

    host = _FakeHost(
        event_bus=EventBus(),
        events=[ApprovalRequest(request_id="unlimited", tool_name="bash", args={}, session_id="s1")],
        outcome=EndTurn(),
    )
    options = {"permission_timeout_seconds": 0} if explicit_zero else {}
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False, **options)  # type: ignore[arg-type]
    server.on_connect(_FakeClient(permission_responder=reply_later))
    responses: list[ApprovalResponse] = []

    async def collect(event: ApprovalResponse) -> None:
        responses.append(event)

    await host.event_bus.subscribe(ApprovalResponse, collect)
    task = asyncio.create_task(server.prompt([text_block("run")], session_id="s1"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert timeouts == [None]
        assert not responses
        assert not task.done()
        release.set()
        await asyncio.wait_for(task, timeout=1)
        assert len(responses) == 1
        assert responses[0].approved is True
        assert not server._pending_permission_tasks
        assert not server._pending_permission_cancels
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("seconds", [-1, float("inf"), float("nan")])
def test_permission_timeout_rejects_invalid_deadlines(seconds: float) -> None:
    host = _FakeHost(event_bus=EventBus(), events=[], outcome=EndTurn())
    with pytest.raises(ValueError, match="finite and non-negative"):
        ChrysAcpServer(_FakeManager(host), initial_vision=False, permission_timeout_seconds=seconds)  # type: ignore[arg-type]
