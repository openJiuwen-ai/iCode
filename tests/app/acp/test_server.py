# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ACP session establishment, prompt outcomes, ask-user input, plan updates, and settings warnings."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from acp import RequestError
from acp import schema as acp_schema
from acp.helpers import image_block, text_block

from chrys.app.acp import server as server_module
from chrys.app.acp.server import ChrysAcpServer
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AskUserResponse,
    Error,
    Event,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationMessage,
    QuestionToUser,
    SessionTitleUpdated,
    TodoListUpdated,
    UsageUpdate,
    Warning,
)
from chrys.foundation.models.ask_user import AskUserAnswer, AskUserOption, AskUserQuestion
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.models.todos import TodoItem
from chrys.orchestration.session_host import Cancelled, EndTurn, Errored, Refused
from chrys.service.todos.tracker import TodoTracker
from tests.app.acp._server_fakes import (
    _append_async,
    _FakeClient,
    _FakeEngine,
    _FakeHost,
    _FakeLoadManager,
    _FakeManager,
    _FakeSession,
    _plan_updates,
    _WarningRejectingClient,
)

# ACP carries the client's ``messageId`` as a UUID: the server rejects anything
# else with ``invalid_params`` before it starts a turn.
_CLIENT_MESSAGE_ID = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"

# Session totals around one turn, used to pin the split between ``usage``
# (this turn's spend) and ``sessionUsage`` (the cumulative total).
_USAGE_BASELINE = UsageUpdate(
    total_session_input_tokens=70,
    total_session_output_tokens=20,
    total_session_tokens=100,
    total_session_cache_hit_tokens=11,
)
_USAGE_AFTER_TURN = UsageUpdate(
    total_session_input_tokens=79,
    total_session_output_tokens=24,
    total_session_tokens=113,
    total_session_cache_hit_tokens=13,
)


class _RecordingUsageEngine(_FakeEngine):
    """An engine whose cumulative usage advances once the turn has run.

    The server snapshots usage twice per turn — at admission and in the
    ``finally`` — so the second snapshot is what the completed turn added to
    the session counters, and the first is the baseline it is measured against.
    """

    def __init__(self, *, before: UsageUpdate, after: UsageUpdate | None = None) -> None:
        super().__init__(usage=after if after is not None else before)
        self._before = before
        self._after = after if after is not None else before
        self._snapshots = 0

    @property
    def usage_publisher(self):
        return SimpleNamespace(make_usage_event=self._snapshot_usage)

    def _snapshot_usage(self, *, session_id: str | None = None) -> UsageUpdate:
        self._snapshots += 1
        return self._before if self._snapshots == 1 else self._after


def _stub_replay(
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: BaseException | None = None,
    answers: dict[str, tuple[str, ...]] | None = None,
) -> list[tuple[Any, ...]]:
    """Replace the module-level history replay and return the calls it records.

    The patch target must stay ``server_module``: the server resolves the
    replay through the module attribute at call time.
    """
    replays: list[tuple[Any, ...]] = []

    async def _replay(*args: Any, **_kwargs: Any) -> dict[str, tuple[str, ...]]:
        replays.append(args)
        if error is not None:
            raise error
        return dict(answers or {})

    monkeypatch.setattr(server_module, "replay_session_history", _replay)
    return replays


@pytest.mark.anyio
async def test_session_title_watcher_forwards_post_turn_updates() -> None:
    """Generated titles land after the prompt event stream has closed, so
    they must reach the client via the long-lived per-session subscription."""
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    await server._watch_session_titles(_FakeSession(host=host))  # type: ignore[arg-type]

    await host.event_bus.publish(
        SessionTitleUpdated(session_id="s1", title="Login bug fix", custom=False, display_title="Login bug fix")
    )
    # Cross-session events on the same bus are not this session's updates.
    await host.event_bus.publish(SessionTitleUpdated(session_id="s2", title="Other", display_title="Other"))

    info_updates = [n for n in client.updates if isinstance(n.update, acp_schema.SessionInfoUpdate)]
    assert len(info_updates) == 1
    assert info_updates[0].session_id == "s1"
    assert info_updates[0].update.title == "Login bug fix"
    assert info_updates[0].update.updated_at is not None


@pytest.mark.anyio
async def test_prompt_returns_cancelled_stop_reason() -> None:
    host = _FakeHost(
        event_bus=EventBus(),
        events=[
            InvocationContentDelta(
                text="working",
                segment_id="seg-1",
                session_id="s1",
                origin=InvocationOrigin("turn", "s1", "turn-test", None),
            )
        ],
        outcome=Cancelled(),
    )
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    response = await server.prompt([text_block("stop")], session_id="s1", message_id=_CLIENT_MESSAGE_ID)

    assert response.stop_reason == "cancelled"
    assert response.user_message_id == _CLIENT_MESSAGE_ID
    assert len(client.updates) == 1
    assert client.updates[0].update.session_update == "agent_message_chunk"


@pytest.mark.anyio
async def test_prompt_snapshots_cumulative_usage_in_finally_while_holding_prompt_lock() -> None:
    host = _FakeHost(event_bus=EventBus(), outcome=EndTurn())
    manager = _FakeManager(host)
    snapshot_lock_states: list[bool] = []

    class _Engine:
        @property
        def usage_publisher(self):
            return SimpleNamespace(make_usage_event=self._snapshot_usage)

        def _snapshot_usage(self, *, session_id: str | None = None) -> UsageUpdate:
            assert session_id == "s1"
            snapshot_lock_states.append(manager._session.prompt_lock.locked())
            return _USAGE_BASELINE if len(snapshot_lock_states) == 1 else _USAGE_AFTER_TURN

    host.engine = _Engine()  # type: ignore[assignment]
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient())

    response = await server.prompt([text_block("run")], session_id="s1")

    # One snapshot before the turn and one in the finally: the response reports
    # the difference, and both are taken while the prompt lock is held.
    assert snapshot_lock_states == [True, True]
    assert response.usage is not None
    assert response.usage.input_tokens == 9
    assert response.usage.output_tokens == 4
    assert response.usage.total_tokens == 13
    assert response.usage.cached_read_tokens == 2
    assert response.field_meta == {
        "chrys": {"sessionUsage": {"inputTokens": 79, "outputTokens": 24, "totalTokens": 113, "cachedReadTokens": 13}}
    }


@pytest.mark.anyio
async def test_prompt_errored_outcome_carries_usage_in_request_error_data() -> None:
    host = _FakeHost(
        event_bus=EventBus(),
        outcome=Errored(error=Error(code="model_failed", message="provider stopped")),
        engine=_RecordingUsageEngine(
            before=UsageUpdate(
                total_session_input_tokens=30,
                total_session_output_tokens=5,
                total_session_tokens=35,
            )
        ),
    )
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient())

    with pytest.raises(RequestError) as exc_info:
        await server.prompt([text_block("run")], session_id="s1")

    assert exc_info.value.data["code"] == "model_failed"
    # The cause is the message; an error nothing classified reports no verdict.
    assert str(exc_info.value) == "provider stopped"
    assert {key: exc_info.value.data[key] for key in ("kind", "retryable", "statusCode", "hint")} == dict.fromkeys(
        ("kind", "retryable", "statusCode", "hint")
    )
    # ``usage`` is this turn's spend, ``sessionUsage`` the session total it was
    # measured against.
    assert exc_info.value.data["usage"] == {
        "inputTokens": 0,
        "outputTokens": 0,
        "totalTokens": 0,
    }
    assert exc_info.value.data["sessionUsage"] == {
        "inputTokens": 30,
        "outputTokens": 5,
        "totalTokens": 35,
    }


_REFUSED = Error(code="hook_blocked", message="hook 'gate' blocked: not this one", refused_input="prompt")


@pytest.mark.anyio
async def test_a_refused_prompt_answers_refusal_and_says_why_while_it_holds_the_session() -> None:
    host = _FakeHost(event_bus=EventBus(), outcome=Refused(error=_REFUSED))
    manager = _FakeManager(host)
    held_while_sent: list[tuple[bool, bool]] = []

    class _Client(_FakeClient):
        async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
            session = manager._session
            held_while_sent.append((session.prompt_lock.locked(), bool(session.turns_reserved)))
            await super().session_update(session_id, update, **kwargs)

    client = _Client()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)
    published: list[Event] = []

    async def record(event: Event) -> None:
        published.append(event)

    for kind in (InvocationContentDelta, InvocationContentFinished, InvocationMessage):
        await host.event_bus.subscribe(kind, record)

    response = await server.prompt([text_block("the secret plan")], session_id="s1", message_id=_CLIENT_MESSAGE_ID)

    # A normal end of the request, not a JSON-RPC error a client may treat as a broken agent.
    assert response.stop_reason == "refusal"
    assert response.user_message_id == _CLIENT_MESSAGE_ID
    assert response.field_meta is not None
    assert (response.field_meta["chrys"]["code"], response.field_meta["chrys"]["message"]) == (
        "hook_blocked",
        "hook 'gate' blocked: not this one",
    )
    notices = [u.update for u in client.updates if u.update.session_update == "agent_message_chunk"]
    assert [n.content.text for n in notices] == ["Input rejected: hook 'gate' blocked: not this one"]
    assert notices[0].field_meta == {"chrys": {"code": "hook_blocked", "refusedInput": "prompt"}}
    # Still the session's request while it says why: no other prompt is admitted, nor can it run first.
    assert held_while_sent[-1] == (True, True)
    # The notice is the ACP frontend's own: nothing the session's history is built from carries it.
    assert published == []


@pytest.mark.anyio
async def test_an_errored_prompt_sends_no_refusal_notice() -> None:
    host = _FakeHost(event_bus=EventBus(), outcome=Errored(error=Error(code="model_failed", message="stopped")))
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    with pytest.raises(RequestError):
        await server.prompt([text_block("run")], session_id="s1")

    assert not any(
        u.update.session_update == "agent_message_chunk" and u.update.content.text.startswith("Input rejected")
        for u in client.updates
    )


@pytest.mark.anyio
async def test_prompt_execution_exception_carries_usage_but_pre_execution_validation_does_not() -> None:
    class _ExplodingHost(_FakeHost):
        async def iter_turn_events(self, _message: Any):
            if False:
                yield Event()
            raise RuntimeError("event bridge failed")

    host = _ExplodingHost(
        event_bus=EventBus(),
        engine=_RecordingUsageEngine(
            before=UsageUpdate(
                total_session_input_tokens=9,
                total_session_output_tokens=4,
                total_session_tokens=13,
            )
        ),
    )
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient())

    with pytest.raises(RequestError) as executed:
        await server.prompt([text_block("run")], session_id="s1")
    assert executed.value.data["usage"] == {
        "inputTokens": 0,
        "outputTokens": 0,
        "totalTokens": 0,
    }
    assert executed.value.data["sessionUsage"] == {
        "inputTokens": 9,
        "outputTokens": 4,
        "totalTokens": 13,
    }

    with pytest.raises(RequestError) as validation:
        await server.prompt([image_block("not-image-data", "image/png")], session_id="s1")
    assert "usage" not in validation.value.data


@pytest.mark.anyio
async def test_request_input_sends_structured_questions_and_answers() -> None:
    questions = (
        AskUserQuestion(
            "Which library?",
            "Library",
            (AskUserOption("tenacity", "Existing dependency"),),
        ),
        AskUserQuestion("Which targets?", "Targets", (AskUserOption("TUI"), AskUserOption("ACP")), True),
        AskUserQuestion("Rollout?", "Rollout"),
    )

    async def _answer(_method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert set(params) == {"sessionId", "requestId", "questions", "callerName"}
        assert params["questions"][0] == {
            "question": "Which library?",
            "header": "Library",
            "options": [{"label": "tenacity", "description": "Existing dependency"}],
            "multiSelect": False,
        }
        return {
            "answers": [
                {"values": ["tenacity"], "note": "only here"},
                {"values": ["TUI", "ACP"], "note": ""},
                {"values": [], "note": ""},
            ],
            "cancelled": False,
        }

    host = _FakeHost(event_bus=EventBus())
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    client = _FakeClient(input_responder=_answer)
    server.on_connect(client)
    responses: list[AskUserResponse] = []
    await host.event_bus.subscribe(AskUserResponse, lambda event: _append_async(responses, event))
    await server._request_input("s1", QuestionToUser(request_id="r", questions=questions, session_id="s1"))
    assert responses[0].answers == (
        AskUserAnswer(("tenacity",), "only here"),
        AskUserAnswer(("TUI", "ACP")),
        AskUserAnswer(),
    )
    assert responses[0].cancelled is False


@pytest.mark.parametrize(
    "reply",
    [{"text": "A"}, {"text": ""}, {}, {"cancelled": True}, {"answers": [{"values": ["A"], "note": ""}], "text": "A"}],
    ids=["text-only", "empty-text", "empty", "cancelled", "text-alongside-answers"],
)
@pytest.mark.anyio
async def test_request_input_without_structured_answers_cancels(reply: dict[str, Any]) -> None:
    async def _answer(_method: str, _params: dict[str, Any]) -> dict[str, Any]:
        return reply

    host = _FakeHost(event_bus=EventBus())
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    client = _FakeClient(input_responder=_answer)
    server.on_connect(client)
    responses: list[AskUserResponse] = []
    await host.event_bus.subscribe(AskUserResponse, lambda event: _append_async(responses, event))
    questions = (AskUserQuestion("Q?", options=(AskUserOption("A"),)),)
    await server._request_input("s1", QuestionToUser(request_id="r", questions=questions, session_id="s1"))
    assert len(client.input_requests) == 1
    assert responses[0].cancelled is True
    assert responses[0].answers is None


@pytest.mark.anyio
async def test_request_input_cancel_wins_a_co_ready_response() -> None:
    host = _FakeHost(event_bus=EventBus())
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]

    async def _answer(_method: str, _params: dict[str, Any]) -> dict[str, Any]:
        server._pending_input_cancels[("s1", "r")].set_result(None)
        return {"answers": [{"values": ["A"], "note": ""}], "cancelled": False}

    client = _FakeClient(input_responder=_answer)
    server.on_connect(client)
    responses: list[AskUserResponse] = []
    await host.event_bus.subscribe(AskUserResponse, lambda event: _append_async(responses, event))
    questions = (AskUserQuestion("Q?", options=(AskUserOption("A"),)),)

    await server._request_input("s1", QuestionToUser(request_id="r", questions=questions, session_id="s1"))

    assert len(client.input_requests) == 1
    assert responses[0].cancelled is True
    assert responses[0].answers is None


@pytest.mark.anyio
async def test_request_input_propagates_its_own_cancellation() -> None:
    started = asyncio.Event()
    callback_cancelled = asyncio.Event()

    async def _block_input(_method: str, _params: dict[str, Any]) -> dict[str, Any]:
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            callback_cancelled.set()
            raise
        return {"answers": [{"values": ["too late"], "note": ""}], "cancelled": False}

    host = _FakeHost(event_bus=EventBus())
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    client = _FakeClient(input_responder=_block_input)
    server.on_connect(client)
    responses: list[AskUserResponse] = []
    await host.event_bus.subscribe(AskUserResponse, lambda event: _append_async(responses, event))
    event = QuestionToUser(request_id="r", questions=(AskUserQuestion("Continue?"),), session_id="s1")

    task = asyncio.create_task(server._request_input("s1", event))
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    # The owner tearing the prompt down must see its cancellation, not a
    # synthetic "cancelled" answer that would let the turn carry on.
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(callback_cancelled.wait(), timeout=5)
    assert responses == []
    assert ("s1", "r") not in server._pending_input_cancels


@pytest.mark.anyio
async def test_settings_reload_tells_the_client_which_values_were_dropped() -> None:
    """A reload that answers "done" and never says what it refused misinforms.

    ``_handle_event`` only runs inside a prompt turn, and the bus does not replay
    events to later subscribers — so the reload's warnings had nowhere to go.
    """
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient()
    manager = _FakeManager(host)
    manager.reload_warnings.append(Warning(code="setting_rejected", message="Ignoring CHRYS_THEME=x", session_id="s1"))
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    await server.ext_method("settings/reload", {"sessionId": "s1"})

    assert ("chrys/warning", {"sessionId": "s1", "code": "setting_rejected", "message": "Ignoring CHRYS_THEME=x"}) in [
        (method, params) for method, params in client.ext_notifications
    ]


@pytest.mark.anyio
async def test_setting_a_config_option_tells_the_client_what_the_reload_refused() -> None:
    """This route persists the value *before* reloading it, so silence is worse here.

    ``rollback_snapshots_keep=0`` is written to disk, clamped to 1 at load, and
    the client was told only that the call succeeded.
    """
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient()
    manager = _FakeManager(host)
    manager.reload_warnings.append(
        Warning(code="setting_clamped", message="Raising CHRYS_ROLLBACK_SNAPSHOTS_KEEP=0 to the minimum of 1.")
    )
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    await server.ext_method(
        "session/set_config_option",
        {"sessionId": "s1", "key": "rollback_snapshots_keep", "value": "0"},
    )

    assert (
        "chrys/warning",
        {
            "sessionId": "s1",
            "code": "setting_clamped",
            "message": "Raising CHRYS_ROLLBACK_SNAPSHOTS_KEEP=0 to the minimum of 1.",
        },
    ) in [(method, params) for method, params in client.ext_notifications]


@pytest.mark.anyio
async def test_prompt_forwards_todo_list_updates_as_plan_updates() -> None:
    items = [TodoItem(content="write tests", status="in_progress", active_form="writing tests")]
    host = _FakeHost(
        event_bus=EventBus(),
        events=[TodoListUpdated(items=items, session_id="s1")],
        outcome=Cancelled(),
    )
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    await server.prompt([text_block("go")], session_id="s1", message_id=_CLIENT_MESSAGE_ID)

    plans = _plan_updates(client)
    assert len(plans) == 1
    assert [(entry.content, entry.status, entry.priority) for entry in plans[0].entries] == [
        ("write tests", "in_progress", "medium")
    ]


@pytest.mark.anyio
async def test_new_session_seeds_empty_plan_update() -> None:
    """A fresh session clears any stale plan panel left by a previous session."""
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    response = await server.new_session(cwd="/workspace")

    assert response.session_id == "s1"
    plans = _plan_updates(client)
    assert len(plans) == 1
    assert plans[0].entries == []


@pytest.mark.anyio
async def test_load_session_seeds_plan_from_todo_tracker(monkeypatch) -> None:
    manager = _FakeLoadManager()
    tracker = TodoTracker()
    await tracker.replace((TodoItem(content="restored step", status="in_progress", active_form="restoring"),))
    manager.session.host.engine.todo_tracker = tracker
    client = _FakeClient()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    _stub_replay(monkeypatch)

    await server.load_session(cwd="/workspace", session_id="s1")

    plans = _plan_updates(client)
    assert len(plans) == 1
    assert [(entry.content, entry.status) for entry in plans[0].entries] == [("restored step", "in_progress")]


@pytest.mark.anyio
async def test_load_session_without_todos_seeds_empty_plan(monkeypatch) -> None:
    """A session with no tracker (or an empty one) still sends a clearing plan."""
    manager = _FakeLoadManager()
    assert manager.session.host.engine.todo_tracker is None
    client = _FakeClient()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    _stub_replay(monkeypatch)

    await server.load_session(cwd="/workspace", session_id="s1")

    plans = _plan_updates(client)
    assert len(plans) == 1
    assert plans[0].entries == []


@pytest.mark.anyio
async def test_new_session_forwards_the_loads_settings_warnings() -> None:
    """Session creation runs outside any prompt turn, so the load's verdicts
    only reach the client if the handler forwards what it collected."""
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient()
    manager = _FakeManager(host)
    manager.session_warnings.append(Warning(code="project_config_dormant", message="Project settings found but idle"))
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    await server.new_session(cwd="/workspace")

    assert (
        "chrys/warning",
        {"sessionId": "s1", "code": "project_config_dormant", "message": "Project settings found but idle"},
    ) in [(method, params) for method, params in client.ext_notifications]


@pytest.mark.anyio
async def test_load_session_forwards_the_loads_settings_warnings(monkeypatch) -> None:
    manager = _FakeLoadManager()
    manager.session_warnings.append(Warning(code="project_config_dormant", message="Project settings found but idle"))
    client = _FakeClient()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    _stub_replay(monkeypatch)

    await server.load_session(cwd="/workspace", session_id="s1")

    assert (
        "chrys/warning",
        {"sessionId": "s1", "code": "project_config_dormant", "message": "Project settings found but idle"},
    ) in [(method, params) for method, params in client.ext_notifications]


@pytest.mark.anyio
async def test_load_session_closes_loaded_host_when_warning_send_fails() -> None:
    """The warning forwarding sits in the same cleanup scope as the replay:
    a send failure must not leave the freshly loaded session in the map."""
    manager = _FakeLoadManager()
    manager.session_warnings.append(Warning(code="project_config_dormant", message="Project settings found but idle"))
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_WarningRejectingClient())

    with pytest.raises(RequestError):
        await server.load_session(cwd="/workspace", session_id="s1")

    assert manager.closed == ["s1"]


@pytest.mark.anyio
async def test_load_session_keeps_existing_host_when_warning_send_fails() -> None:
    manager = _FakeLoadManager(reused_existing=True)
    manager.session_warnings.append(Warning(code="project_config_dormant", message="Project settings found but idle"))
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_WarningRejectingClient())

    with pytest.raises(RequestError):
        await server.load_session(cwd="/workspace", session_id="s1")

    assert manager.closed == []


@pytest.mark.anyio
async def test_new_session_closes_created_host_when_warning_send_fails() -> None:
    """new_session has the same establishment window as load_session: the
    session is in the active map but its id never reached the caller, so a
    failed send must close it rather than strand it."""
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)
    manager.session_warnings.append(Warning(code="project_config_dormant", message="Project settings found but idle"))
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_WarningRejectingClient())

    with pytest.raises(RuntimeError):
        await server.new_session(cwd="/workspace")

    assert manager.closed_sessions == ["s1"]


@pytest.mark.anyio
async def test_load_session_closes_loaded_host_when_cancelled_mid_establishment(monkeypatch) -> None:
    """Cancellation is a BaseException: it must run the same close as any
    other establishment failure, then keep propagating."""
    manager = _FakeLoadManager()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient())

    _stub_replay(monkeypatch, error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await server.load_session(cwd="/workspace", session_id="s1")

    assert manager.closed == ["s1"]


@pytest.mark.anyio
async def test_extension_rollback_to_turn_sends_plan_update_after_replay(monkeypatch) -> None:
    host = _FakeHost(event_bus=EventBus())
    tracker = TodoTracker()
    await tracker.replace((TodoItem(content="turn two step", status="pending"),))
    host.engine.todo_tracker = tracker
    manager = _FakeManager(host)
    client = _FakeClient()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)
    replays = _stub_replay(monkeypatch)

    await server.ext_method("session/rollback", {"sessionId": "s1", "targetTurn": 2})

    assert len(replays) == 1
    plans = _plan_updates(client)
    assert len(plans) == 1
    assert [(entry.content, entry.status) for entry in plans[0].entries] == [("turn two step", "pending")]


@pytest.mark.anyio
async def test_load_session_keeps_the_replayed_answer_a_bare_retry_withdraws(monkeypatch) -> None:
    manager = _FakeLoadManager(reused_existing=True)
    manager.session.answer_message_ids = {"before": ("before-the-reload",)}
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient())
    _stub_replay(monkeypatch, answers={"seg": ("replayed-half-answer",)})

    await server.load_session(cwd="/workspace", session_id="s1")

    assert manager.session.answer_message_ids == {"seg": ("replayed-half-answer",)}


@pytest.mark.anyio
@pytest.mark.parametrize(("target_turn", "expected"), [(2, {"seg": ("replayed-half-answer",)}), (0, {})])
async def test_rollback_keeps_the_replayed_answer_a_bare_retry_withdraws(
    monkeypatch, target_turn: int, expected: dict[str, tuple[str, ...]]
) -> None:
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)
    manager.get("s1").answer_message_ids = {"before": ("before-the-rollback",)}
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient())
    _stub_replay(monkeypatch, answers={"seg": ("replayed-half-answer",)})

    await server.ext_method("session/rollback", {"sessionId": "s1", "targetTurn": target_turn})

    assert manager.get("s1").answer_message_ids == expected


@pytest.mark.anyio
async def test_extension_rollback_to_welcome_sends_empty_plan_update() -> None:
    """targetTurn == 0 takes the no-replay branch but must still clear the plan."""
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)
    client = _FakeClient()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    await server.ext_method("session/rollback", {"sessionId": "s1", "targetTurn": 0})

    plans = _plan_updates(client)
    assert len(plans) == 1
    assert plans[0].entries == []


@pytest.mark.anyio
async def test_load_session_closes_loaded_host_when_history_replay_fails(monkeypatch) -> None:
    manager = _FakeLoadManager()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient())

    _stub_replay(monkeypatch, error=RuntimeError("replay boom"))

    with pytest.raises(RequestError):
        await server.load_session(cwd="/workspace", session_id="s1")

    assert manager.closed == ["s1"]


@pytest.mark.anyio
async def test_load_session_keeps_existing_host_when_history_replay_fails(monkeypatch) -> None:
    manager = _FakeLoadManager(reused_existing=True)
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient())

    _stub_replay(monkeypatch, error=RuntimeError("replay boom"))

    with pytest.raises(RequestError):
        await server.load_session(cwd="/workspace", session_id="s1")

    assert manager.closed == []


@pytest.mark.anyio
async def test_settings_options_is_read_off_the_event_loop_thread() -> None:
    # The read takes the settings document's file lock, whose timeout is ten
    # seconds. Inline, that wait belongs to the ACP loop, so one held lock
    # stalls every session on the connection rather than just this request.
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient())

    read_threads: list[int] = []
    inner = manager.get_config_options

    def recording(session_id: str | None = None) -> dict[str, object]:
        read_threads.append(threading.get_ident())
        return inner(session_id)

    manager.get_config_options = recording  # type: ignore[method-assign]

    await server.ext_method("settings/options", {})

    assert read_threads and threading.get_ident() not in read_threads
