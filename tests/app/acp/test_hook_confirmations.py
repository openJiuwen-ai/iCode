# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A hook's ``ask`` over ACP: a standard permission request of its own, answered, withdrawn or closed.

Written from the plan (``.plan/hook-ask-confirm.md`` §5 ACP) and the PyCharm
check it records: the question carries its own ``toolCallId``; title and
content each name the hook, the reason and the subject; a withdrawn question
is closed on the client with a terminal ``tool_call_update`` (``failed``);
a failed or closed frontend answers unavailable, never "the user declined".
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from acp import RequestError
from acp import schema as acp_schema
from acp.helpers import text_block

import chrys.app.acp.server as server_module
from chrys.app.acp.hook_confirmations import AcpSessionHookConfirmations, confirmation_tool_call_id
from chrys.app.acp.server import ChrysAcpServer
from chrys.app.acp.session_manager import ManagedSession
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import HookConfirmationRequest, HookConfirmationResponse, Warning
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_WRITE
from chrys.orchestration.session_host import EndTurn
from chrys.service.hooks.confirmation import HookConfirmationResult, HookConfirmationService
from chrys.service.hooks.operation import HookOperation
from tests.app.acp._server_fakes import (
    _FakeClient,
    _FakeHost,
    _FakeLoadManager,
    _FakeManager,
    _FakeSession,
    _LoadedSession,
)
from tests.support.waiting import wait_for, wait_until

_SESSION = "s1"


async def _setup(client: _FakeClient) -> tuple[EventBus, HookConfirmationService, AcpSessionHookConfirmations]:
    bus = EventBus()
    service = HookConfirmationService(bus=bus, enabled=True)
    await service.start()
    adapter = AcpSessionHookConfirmations(_SESSION, bus, lambda: client)  # type: ignore[arg-type,return-value]
    await adapter.subscribe()
    return bus, service, adapter


def _tool_operation() -> HookOperation:
    return HookOperation(
        subject="tool",
        session_id=_SESSION,
        tool_name="edit_file",
        tool_kind=KIND_FILESYSTEM_WRITE,
        arguments={"path": "deploy/prod.yaml"},
    )


def _input_operation() -> HookOperation:
    return HookOperation(subject="input", session_id=_SESSION, input_text="deploy with sk-123")


async def _ask(service: HookConfirmationService, operation: HookOperation) -> HookConfirmationResult:
    return await service.ask(
        operation, run_id="run-1", hook_id="prod-guard", hook_source="workdir", reason="changes production"
    )


async def _shown(held: asyncio.Event, task: asyncio.Task[Any]) -> None:
    """Wait until the client holds the question, surfacing the ask's own error if it ended first."""
    await wait_for(lambda: held.is_set() or task.done(), description="the client showing the question")
    if task.done():
        await task
    assert held.is_set()


def _text(content: list[Any]) -> str:
    return "\n".join(item.content.text for item in content)


@pytest.mark.asyncio
async def test_a_tool_question_is_its_own_permission_request_and_says_it_all() -> None:
    client = _FakeClient()
    _bus, service, _adapter = await _setup(client)

    result = await asyncio.wait_for(_ask(service, _tool_operation()), timeout=5)

    assert (result.outcome, result.end_reason) == ("approved", "user")
    (request,) = client.permission_requests
    call = request.tool_call
    assert call.tool_call_id.startswith("hook-confirm-")
    assert call.kind == "edit"
    assert "prod-guard" in call.title and "changes production" in call.title and "edit_file" in call.title
    body = _text(call.content)
    assert "prod-guard" in body and "changes production" in body and "deploy/prod.yaml" in body
    assert "working directory hook" in body
    assert [option.kind for option in request.options] == ["allow_once", "reject_once"]


@pytest.mark.asyncio
async def test_a_question_asked_again_says_so_and_what_changed() -> None:
    client = _FakeClient()
    _bus, service, _adapter = await _setup(client)
    operation = _tool_operation()

    assert (await asyncio.wait_for(_ask(service, operation), timeout=5)).outcome == "approved"
    operation.replace_arguments({"path": "deploy/prod.yaml", "force": True})
    assert (await asyncio.wait_for(_ask(service, operation), timeout=5)).outcome == "approved"

    first, second = (_text(request.tool_call.content) for request in client.permission_requests)
    assert "Asked again" not in first
    assert "Asked again" in second and "force: (none) → true" in second


@pytest.mark.asyncio
async def test_an_input_question_is_kind_other_and_shows_the_message() -> None:
    client = _FakeClient(option_id="reject")
    _bus, service, _adapter = await _setup(client)

    result = await asyncio.wait_for(_ask(service, _input_operation()), timeout=5)

    assert (result.outcome, result.end_reason) == ("declined", "user")
    call = client.permission_requests[0].tool_call
    assert call.kind == "other"
    assert "send your message" in call.title
    assert "deploy with sk-123" in _text(call.content)


@pytest.mark.asyncio
async def test_the_title_alone_says_what_is_asked_about() -> None:
    # Clients that show only the title (an ``other`` kind in PyCharm) must still see what they
    # are asked about: the arguments, what changed when asked again, or the message.
    client = _FakeClient()
    _bus, service, _adapter = await _setup(client)
    operation = _tool_operation()

    assert (await asyncio.wait_for(_ask(service, operation), timeout=5)).outcome == "approved"
    operation.replace_arguments({"path": "deploy/prod.yaml", "force": True})
    assert (await asyncio.wait_for(_ask(service, operation), timeout=5)).outcome == "approved"
    assert (await asyncio.wait_for(_ask(service, _input_operation()), timeout=5)).outcome == "approved"

    first, again, message = (request.tool_call.title for request in client.permission_requests)
    assert "edit_file" in first and "deploy/prod.yaml" in first
    assert again.startswith("Asked again:") and "force changed; force is now true" in again
    assert "deploy with sk-123" in message
    assert all("\n" not in title for title in (first, again, message))


@pytest.mark.asyncio
async def test_a_title_asked_again_keeps_the_new_value_when_the_old_one_is_long() -> None:
    client = _FakeClient()
    _bus, service, _adapter = await _setup(client)
    script = "echo start; " + "x" * 3000 + "; rm -rf build"
    operation = HookOperation(
        subject="tool", session_id=_SESSION, tool_name="shell", tool_kind="", arguments={"command": script, "cwd": "a"}
    )

    assert (await asyncio.wait_for(_ask(service, operation), timeout=5)).outcome == "approved"
    operation.replace_arguments({"command": script.replace("rm -rf build", "rm -rf /"), "cwd": "b"})
    assert (await asyncio.wait_for(_ask(service, operation), timeout=5)).outcome == "approved"

    title = client.permission_requests[1].tool_call.title
    # Every changed name, and the first one's new value around where it changed.
    assert "command, cwd changed" in title and "rm -rf /" in title, title
    assert "rm -rf build" not in title, "the old value is left to the content"
    assert len(title) < 400, title


@pytest.mark.asyncio
async def test_long_arguments_are_shortened_in_the_content_and_whole_in_raw_input() -> None:
    client = _FakeClient()
    _bus, service, _adapter = await _setup(client)
    content = "\n".join(f"line {n}" for n in range(200)) + "\nTAIL-MARK"
    operation = HookOperation(
        subject="tool", session_id=_SESSION, tool_name="edit_file", tool_kind="", arguments={"content": content}
    )

    assert (await asyncio.wait_for(_ask(service, operation), timeout=5)).outcome == "approved"

    call = client.permission_requests[0].tool_call
    body = _text(call.content)
    assert "TAIL-MARK" not in body and "(Shortened; the whole arguments are in rawInput.)" in body
    assert call.raw_input == {"tool": "edit_file", "args": {"content": content}}


@pytest.mark.asyncio
async def test_the_content_keeps_every_argument_by_name_and_counts_those_past_ten() -> None:
    client = _FakeClient()
    _bus, service, _adapter = await _setup(client)
    arguments: dict[str, object] = {"command": "echo start; " + "x" * 3000, "timeout": 30}
    arguments.update({f"k{n}": n for n in range(10)})
    operation = HookOperation(subject="tool", session_id=_SESSION, tool_name="zsh", tool_kind="", arguments=arguments)

    assert (await asyncio.wait_for(_ask(service, operation), timeout=5)).outcome == "approved"

    body = _text(client.permission_requests[0].tool_call.content)
    assert '"command": "echo start; xxx' in body and '…",' in body
    assert '"timeout": 30,' in body and "… (2 more)" in body


@pytest.mark.asyncio
async def test_a_long_message_shows_its_start() -> None:
    client = _FakeClient()
    _bus, service, _adapter = await _setup(client)
    text = "\n".join(f"line {n}" for n in range(50))
    operation = HookOperation(subject="input", session_id=_SESSION, input_text=text)

    assert (await asyncio.wait_for(_ask(service, operation), timeout=5)).outcome == "approved"

    body = _text(client.permission_requests[0].tool_call.content)
    assert "line 9…" in body and "line 10" not in body


@pytest.mark.asyncio
async def test_a_client_that_cancels_the_request_dismissed_it() -> None:
    client = _FakeClient(permission_outcome=acp_schema.DeniedOutcome(outcome="cancelled"))
    _bus, service, _adapter = await _setup(client)

    result = await asyncio.wait_for(_ask(service, _tool_operation()), timeout=5)

    assert (result.outcome, result.end_reason) == ("declined", "dismissed")


@pytest.mark.asyncio
async def test_a_client_that_cannot_show_it_answers_unavailable_not_declined() -> None:
    client = _FakeClient(permission_exc=RuntimeError("client went away"))
    _bus, service, _adapter = await _setup(client)

    result = await asyncio.wait_for(_ask(service, _tool_operation()), timeout=5)

    assert (result.outcome, result.end_reason) == ("unavailable", "frontend_unavailable")
    # Whatever the client managed to show is ended, once.
    await wait_for(lambda: _settled(client), description="the card settled")
    assert _settled(client) == ["failed"]


@pytest.mark.asyncio
async def test_a_withdrawn_question_is_closed_on_the_client() -> None:
    held = asyncio.Event()

    async def _never_answers(_session_id: str, _tool_call: Any) -> acp_schema.RequestPermissionResponse:
        held.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    client = _FakeClient(permission_responder=_never_answers)
    _bus, service, _adapter = await _setup(client)
    operation = _tool_operation()

    task = asyncio.create_task(_ask(service, operation))
    await _shown(held, task)
    operation.invalidate("turn_ended")
    result = await asyncio.wait_for(task, timeout=5)

    assert result.outcome == "invalidated"
    tool_call_id = client.permission_requests[0].tool_call.tool_call_id
    await wait_for(
        lambda: [u.update for u in client.updates if getattr(u.update, "tool_call_id", None) == tool_call_id],
        description="the client told the question is over",
    )
    (closing,) = [u.update for u in client.updates if getattr(u.update, "tool_call_id", None) == tool_call_id]
    assert (closing.session_update, closing.status) == ("tool_call_update", "failed")


@pytest.mark.asyncio
async def test_detaching_under_an_open_question_ends_it_unavailable() -> None:
    held = asyncio.Event()

    async def _never_answers(_session_id: str, _tool_call: Any) -> acp_schema.RequestPermissionResponse:
        held.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    client = _FakeClient(permission_responder=_never_answers)
    _bus, service, adapter = await _setup(client)

    task = asyncio.create_task(_ask(service, _tool_operation()))
    await _shown(held, task)
    await adapter.close()
    result = await asyncio.wait_for(task, timeout=5)

    assert (result.outcome, result.end_reason) == ("unavailable", "frontend_unavailable")
    # Detached: a later question finds no frontend at all.
    assert (await asyncio.wait_for(_ask(service, _tool_operation()), timeout=5)).end_reason == "unclaimed"


@pytest.mark.asyncio
async def test_another_session_s_question_is_left_to_its_own_frontend() -> None:
    client = _FakeClient()
    _bus, service, _adapter = await _setup(client)
    other = HookOperation(subject="input", session_id="another", input_text="hi")

    result = await asyncio.wait_for(_ask(service, other), timeout=5)

    assert result.end_reason == "unclaimed"
    assert client.permission_requests == []


def test_the_question_id_is_never_a_tool_card_s() -> None:
    assert confirmation_tool_call_id("abc") == "hook-confirm-abc"


@pytest.mark.asyncio
async def test_closing_right_after_a_claim_answers_it_unavailable_exactly_once() -> None:
    # Whether the question's task runs before the close cancels it or not, the backend gets one
    # answer: from the task when it ran (the client failed), from the close when it never did.
    client = _FakeClient(permission_exc=RuntimeError("client went away"))
    bus = EventBus()
    adapter = AcpSessionHookConfirmations(_SESSION, bus, lambda: client)  # type: ignore[arg-type,return-value]
    await adapter.subscribe()
    answers: list[str] = []

    async def record(event: HookConfirmationResponse) -> None:
        answers.append(event.answer)

    await bus.subscribe(HookConfirmationResponse, record)
    request = HookConfirmationRequest(
        session_id=_SESSION, request_id="r1", operation_id="op", revision=1, run_id="run", hook_id="h", subject="input"
    )
    await bus.publish(request)
    await adapter.close()

    assert request.claim.claimed is True
    assert answers == ["frontend_unavailable"]


@pytest.mark.asyncio
async def test_a_withdrawn_question_s_task_is_drained_on_close() -> None:
    closing = asyncio.Event()
    finish_closing = asyncio.Event()

    class _SlowClosingClient(_FakeClient):
        async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
            closing.set()
            await finish_closing.wait()
            await super().session_update(session_id, update, **kwargs)

    held = asyncio.Event()

    async def _never_answers(_session_id: str, _tool_call: Any) -> acp_schema.RequestPermissionResponse:
        held.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    client = _SlowClosingClient(permission_responder=_never_answers)
    _bus, service, adapter = await _setup(client)
    operation = _tool_operation()

    task = asyncio.create_task(_ask(service, operation))
    await _shown(held, task)
    operation.invalidate("turn_ended")
    assert (await asyncio.wait_for(task, timeout=5)).outcome == "invalidated"
    await wait_for(closing.is_set, description="the dialog closing")
    detach = asyncio.create_task(adapter.close())
    await asyncio.sleep(0)
    # The withdrawn task is still closing the client's dialog: detach waits for it.
    assert not detach.done()
    finish_closing.set()
    await asyncio.wait_for(detach, timeout=5)

    assert [u.update.status for u in client.updates] == ["failed"]


@pytest.mark.parametrize(("option_id", "outcome"), [("allow", "approved"), ("reject", "declined")])
@pytest.mark.asyncio
async def test_the_server_shows_a_question_asked_before_the_prompt_yields_anything(
    option_id: str, outcome: str
) -> None:
    # A new input's own check runs while the server is still admitting the prompt, before the
    # turn's first event: the question reaches the client out of band all the same.
    bus = EventBus()
    service = HookConfirmationService(bus=bus, enabled=True)
    await service.start()
    results: list[HookConfirmationResult] = []

    class _AskingHost(_FakeHost):
        async def iter_turn_events(self, _message: Any):
            results.append(await _ask(service, HookOperation(subject="input", session_id="s1", input_text="hi")))
            async for event in super().iter_turn_events(_message):
                yield event

    host = _AskingHost(event_bus=bus, outcome=EndTurn())
    client = _FakeClient(option_id=option_id)
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)  # type: ignore[arg-type]
    await server.new_session(cwd="/workspace")

    await asyncio.wait_for(
        server.prompt([text_block("hi")], session_id="s1", message_id="3f2504e0-4f89-11d3-9a0c-0305e82c3301"),
        timeout=5,
    )

    assert [r.outcome for r in results] == [outcome]
    (request,) = client.permission_requests
    assert request.tool_call.kind == "other"
    await server.close_session("s1")


@pytest.mark.asyncio
async def test_a_cancelled_detach_still_answers_the_open_question_unavailable() -> None:
    held = asyncio.Event()

    async def _never_answers(_session_id: str, _tool_call: Any) -> acp_schema.RequestPermissionResponse:
        held.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    client = _FakeClient(permission_responder=_never_answers)
    _bus, service, adapter = await _setup(client)

    task = asyncio.create_task(_ask(service, _tool_operation()))
    await _shown(held, task)
    detach = asyncio.create_task(adapter.close())
    await asyncio.sleep(0)
    detach.cancel()
    with pytest.raises(asyncio.CancelledError):
        await detach
    result = await asyncio.wait_for(task, timeout=5)

    assert (result.outcome, result.end_reason) == ("unavailable", "frontend_unavailable")


@pytest.mark.parametrize(("option_id", "status"), [("allow", "completed"), ("reject", "failed")])
@pytest.mark.asyncio
async def test_an_answered_question_s_card_is_settled_on_the_client(option_id: str, status: str) -> None:
    client = _FakeClient(option_id=option_id)
    _bus, service, _adapter = await _setup(client)

    await asyncio.wait_for(_ask(service, _tool_operation()), timeout=5)

    tool_call_id = client.permission_requests[0].tool_call.tool_call_id
    settled = [u.update for u in client.updates if getattr(u.update, "tool_call_id", None) == tool_call_id]
    assert [(u.session_update, u.status) for u in settled] == [("tool_call_update", status)]


@pytest.mark.asyncio
async def test_a_session_set_up_again_on_a_new_bus_shows_its_questions() -> None:
    # The first setup fails after the questions were attached; the session set up again on a
    # new bus under the same id must still have its questions claimed and shown.
    class _FirstWarningFails(_FakeClient):
        failed = False

        async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("client went away")
            await super().session_update(session_id, update, **kwargs)

    first = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(first)
    manager.session_warnings.append(Warning(code="workdir_config_dormant", message="Workdir settings found but idle"))
    client = _FirstWarningFails()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError):
        await server.new_session(cwd="/workspace")

    bus = EventBus()
    service = HookConfirmationService(bus=bus, enabled=True)
    await service.start()
    manager.session_warnings.clear()
    manager._session = _FakeSession(host=_FakeHost(event_bus=bus))
    await server.new_session(cwd="/workspace")

    result = await asyncio.wait_for(_ask(service, _tool_operation()), timeout=5)

    assert result.outcome == "approved"
    assert len(client.permission_requests) == 1
    await server.close_session("s1")


@pytest.mark.asyncio
async def test_a_session_loaded_again_on_a_new_bus_shows_its_questions(monkeypatch: pytest.MonkeyPatch) -> None:
    # The same for a load: the first one fails after the questions were attached (its history replay).
    replays: list[str] = []

    async def replay_fails_once(*_args: Any, **_kwargs: Any) -> dict[str, tuple[str, ...]]:
        replays.append("replay")
        if len(replays) == 1:
            raise RuntimeError("client went away")
        return {}

    monkeypatch.setattr(server_module, "replay_session_history", replay_fails_once)
    manager = _FakeLoadManager()
    client = _FakeClient()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)  # type: ignore[arg-type]
    with pytest.raises(RequestError):
        await server.load_session(cwd="/workspace", session_id="s1")
    assert manager.closed == ["s1"]

    bus = EventBus()
    service = HookConfirmationService(bus=bus, enabled=True)
    await service.start()
    manager.session = _LoadedSession(session_id="s1", host=_FakeHost(event_bus=bus))
    await server.load_session(cwd="/workspace", session_id="s1")

    result = await asyncio.wait_for(_ask(service, _tool_operation()), timeout=5)

    assert result.outcome == "approved"
    assert len(client.permission_requests) == 1


class _SlowSettlingClient(_FakeClient):
    """Answers allow at once; holds every card update until released."""

    def __init__(self) -> None:
        super().__init__()
        self.sending = asyncio.Event()
        self.release = asyncio.Event()

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.sending.set()
        await self.release.wait()
        await super().session_update(session_id, update, **kwargs)


def _settled(client: _FakeClient) -> list[str]:
    tool_call_id = client.permission_requests[0].tool_call.tool_call_id
    return [u.update.status for u in client.updates if getattr(u.update, "tool_call_id", None) == tool_call_id]


@pytest.mark.parametrize(("option_id", "outcome"), [("allow", "approved"), ("reject", "declined")])
@pytest.mark.asyncio
async def test_an_answer_counts_when_given_however_long_the_card_takes_to_settle(option_id: str, outcome: str) -> None:
    # The client answered in time; ending its card is slow. The answer is the user's, decided when
    # given: it never waits for the card, nor runs out the clock behind it.
    client = _SlowSettlingClient()
    client.option_id = option_id
    bus = EventBus()
    service = HookConfirmationService(bus=bus, enabled=True, timeout_seconds=30)
    await service.start()
    adapter = AcpSessionHookConfirmations(_SESSION, bus, lambda: client)  # type: ignore[arg-type,return-value]
    await adapter.subscribe()

    result = await asyncio.wait_for(_ask(service, _tool_operation()), timeout=5)

    assert result.outcome == outcome
    assert client.sending.is_set() and _settled(client) == []
    client.release.set()
    await asyncio.wait_for(adapter.close(), timeout=5)
    assert _settled(client) == ["completed" if outcome == "approved" else "failed"]


@pytest.mark.asyncio
async def test_a_card_settling_as_the_session_closes_still_settles_once() -> None:
    client = _SlowSettlingClient()
    _bus, service, adapter = await _setup(client)

    result = await asyncio.wait_for(_ask(service, _tool_operation()), timeout=5)
    await wait_for(client.sending.is_set, description="the card's update on its way")
    closing = asyncio.create_task(adapter.close())
    # The close cancels the question's task while its card is still being settled.
    assert not await wait_until(closing.done, timeout=0.2)
    client.release.set()
    await asyncio.wait_for(closing, timeout=5)

    assert result.outcome == "approved"
    assert _settled(client) == ["completed"]


class _FailingHost:
    def __init__(self, bus: EventBus) -> None:
        self.event_bus = bus

    async def shutdown(self) -> None:
        raise RuntimeError("host shutdown failed")


class _FailingTitles:
    async def shutdown(self) -> None:
        raise RuntimeError("title updater shutdown failed")


@pytest.mark.asyncio
async def test_a_session_closes_its_confirmations_however_the_rest_of_its_shutdown_goes() -> None:
    held = asyncio.Event()

    async def _never_answers(_session_id: str, _tool_call: Any) -> acp_schema.RequestPermissionResponse:
        held.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    client = _FakeClient(permission_responder=_never_answers)
    bus, service, adapter = await _setup(client)
    session = ManagedSession(
        session_id=_SESSION,
        cwd="/workspace",
        profile_name="Code",
        host=_FailingHost(bus),  # type: ignore[arg-type]
        title_updater=_FailingTitles(),  # type: ignore[arg-type]
        hook_confirmations=adapter,
    )
    task = asyncio.create_task(_ask(service, _tool_operation()))
    await _shown(held, task)

    with pytest.raises(RuntimeError, match="title updater"):
        await session.shutdown()
    result = await asyncio.wait_for(task, timeout=5)

    assert (result.outcome, result.end_reason) == ("unavailable", "frontend_unavailable")
    assert _settled(client) == ["failed"]
