# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Chrys ACP extension notifications, extension request routing, and session runtime/mutation reads."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from acp import PROTOCOL_VERSION, RequestError
from acp import schema as acp_schema

from chrys.app.acp.bridge import AcpEventBridge
from chrys.app.acp.server import ChrysAcpServer
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentLoadProgress,
    AgentRuntimeDetails,
    CompactionFinished,
    CompactionStarted,
    Error,
    InvocationCompactionCommitted,
    InvocationCompactionFinished,
    InvocationCompactionStarted,
    InvocationContextPressure,
    InvocationPaused,
    InvocationProgress,
    InvocationStarted,
    RuntimeHookDetails,
    RuntimeHookSourceDetails,
    RuntimeModelDetails,
    RuntimeSkillDetails,
    UsageUpdate,
    UserInjectResult,
    Warning,
    WorkspaceUpdated,
)
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.service.approval.policy import ApprovalMode
from chrys.service.mutations.types import (
    FileHashDiff,
    FileMutation,
    MutationOp,
    MutationSource,
    TurnMutations,
)
from chrys.service.trajectory.state import TurnRecord
from tests.app.acp._server_fakes import (
    _FakeBlobStore,
    _FakeClient,
    _FakeEngine,
    _FakeHost,
    _FakeManager,
    _FakeMutationTracker,
)


@pytest.mark.anyio
async def test_error_and_warning_events_are_exposed_as_chrys_extension_notifications() -> None:
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    await server._handle_event(
        "s1",
        Error(code="boom", message="Something failed", recoverable=False, session_id="s1"),
        AcpEventBridge(),
        {},
    )
    await server._handle_event(
        "s1",
        Warning(code="heads_up", message="Be careful", session_id="s1"),
        AcpEventBridge(),
        {},
    )

    assert client.ext_notifications == [
        (
            "chrys/error",
            {
                "sessionId": "s1",
                "code": "boom",
                "message": "Something failed",
                "recoverable": False,
            },
        ),
        (
            "chrys/warning",
            {
                "sessionId": "s1",
                "code": "heads_up",
                "message": "Be careful",
            },
        ),
    ]


@pytest.mark.anyio
async def test_extension_inject_request_publishes_to_active_session() -> None:
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]

    await server.ext_method("session/inject", {"sessionId": "s1", "text": "one more thing"})

    assert manager.injected == [("s1", "one more thing")]


@pytest.mark.anyio
async def test_user_inject_result_is_exposed_as_extension_notification() -> None:
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    await server._handle_event(
        "s1",
        UserInjectResult(text="extra context", consumed=True, created_at="now", session_id="s1"),
        AcpEventBridge(),
        {},
    )

    assert client.ext_notifications == [
        (
            "chrys/user_inject_result",
            {
                "sessionId": "s1",
                "text": "extra context",
                "consumed": True,
                "createdAt": "now",
                "injectionId": None,
            },
        )
    ]


@pytest.mark.anyio
async def test_extension_rollback_request_returns_result_and_notifies_client() -> None:
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)
    client = _FakeClient()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    result = await server.ext_method(
        "session/rollback",
        {
            "sessionId": "s1",
            "targetTurn": 0,
            "revertChanges": True,
            "selectedPaths": ["/workspace/a.py"],
        },
    )

    assert manager.rollbacks == [
        {
            "session_id": "s1",
            "target_turn": 0,
            "revert_changes": True,
            "selected_paths": ["/workspace/a.py"],
        }
    ]
    assert result["targetTurn"] == 0
    assert result["rolledBackUserText"] == "discarded prompt"
    assert result["filesReverted"] == 1
    assert result["restoreResults"][0]["outcome"] == "applied"
    assert client.ext_notifications[0][0] == "chrys/rollback_result"
    assert client.ext_notifications[0][1]["rolledBackUserText"] == "discarded prompt"


@pytest.mark.anyio
async def test_sub_agent_events_are_exposed_as_extension_notifications() -> None:
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    await server._handle_event(
        "s1",
        InvocationProgress(
            agent_name="Explore",
            tool_call_count=2,
            total_tokens=42,
            session_id="s1",
            origin=InvocationOrigin("sub_agent", "s1", "inv1", None),
        ),
        AcpEventBridge(),
        {},
    )
    await server._handle_event(
        "s1",
        InvocationPaused(
            agent_name="Explore",
            tool_name="explore",
            reason="stream_stall",
            last_error="stalled",
            retry_attempts=3,
            session_id="s1",
            origin=InvocationOrigin("sub_agent", "s1", "inv1", None),
        ),
        AcpEventBridge(),
        {},
    )

    assert client.ext_notifications == [
        (
            "chrys/sub_agent_progress",
            {
                "sessionId": "s1",
                "agentName": "Explore",
                "invocationId": "inv1",
                "toolCallCount": 2,
                "totalTokens": 42,
                "totalUsageTokens": 0,
                "usageUnreportedAttempts": 0,
            },
        ),
        (
            "chrys/sub_agent_paused",
            {
                "sessionId": "s1",
                "agentName": "Explore",
                "invocationId": "inv1",
                "toolName": "explore",
                "reason": "stream_stall",
                "lastError": "stalled",
                "retryAttempts": 3,
            },
        ),
    ]


@pytest.mark.anyio
async def test_session_history_forwards_cwd_for_workspace_scoping() -> None:
    """session/history is scoped by cwd (like load/delete), not a raw store read."""
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]

    result = await server.ext_method("session/history", {"sessionId": "s1", "cwd": "/tmp/project"})

    assert manager.history_reads == [("/tmp/project", "s1")]
    assert result["sessionId"] == "s1"
    assert result["turns"] == []
    assert result["messages"] == [
        {
            "turnId": None,
            "turnNumber": None,
            "message": {"role": "user", "contents": [{"type": "text", "text": "hello s1"}]},
        }
    ]


@pytest.mark.anyio
async def test_sub_agent_events_also_emit_standard_parent_tool_call_updates() -> None:
    """Sub-agent events are dual-path: ext notification AND standard bridge updates.

    A standard-only ACP client relies on `session/update` tool-call progress on
    the parent sub_agent call; the Chrys extension handler must not suppress it.
    """
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)
    bridge = AcpEventBridge()

    await server._handle_event(
        "s1",
        InvocationStarted(
            agent_name="Explore",
            tool_name="explore",
            parent_call_id="call-1",
            session_id="s1",
            origin=InvocationOrigin("sub_agent", "s1", "inv1", None),
        ),
        bridge,
        {},
    )
    await server._handle_event(
        "s1",
        InvocationProgress(
            agent_name="Explore",
            tool_call_count=2,
            total_tokens=42,
            session_id="s1",
            origin=InvocationOrigin("sub_agent", "s1", "inv1", None),
        ),
        bridge,
        {},
    )

    # Extension notifications still flow for Chrys-aware clients.
    assert [method for method, _ in client.ext_notifications] == [
        "chrys/sub_agent_invocation_start",
        "chrys/sub_agent_progress",
    ]
    # And standard tool-call updates on the parent call are no longer suppressed.
    assert len(client.updates) == 2
    assert all(update.update.tool_call_id == "call-1" for update in client.updates)


@pytest.mark.anyio
async def test_compaction_events_emit_extension_notifications() -> None:
    """Main-agent Phase-4 compaction events are ext-only (like ContextCompressed)."""
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)
    bridge = AcpEventBridge()

    await server._handle_event("s1", CompactionStarted(compaction_id="c-1", session_id="s1"), bridge, {})
    await server._handle_event(
        "s1",
        CompactionFinished(
            compaction_id="c-1",
            outcome="ok",
            duration_ms=68_000,
            last_words="## Note",
            format_violation='missing required heading "## Next"',
            session_id="s1",
        ),
        bridge,
        {},
    )
    await server._handle_event(
        "s1",
        InvocationContextPressure(
            origin=InvocationOrigin("sub_agent", "s1", "inv-1", None),
            reason="side_call_budget",
            attempts=2,
            side_call_tokens=300_000,
            side_call_token_budget=300_000,
            source="sub_agent",
            session_id="s1",
        ),
        bridge,
        {},
    )

    assert client.ext_notifications == [
        ("chrys/compaction_started", {"sessionId": "s1", "compactionId": "c-1", "phase": "phase4"}),
        (
            "chrys/compaction_finished",
            {
                "sessionId": "s1",
                "compactionId": "c-1",
                "outcome": "ok",
                "durationMs": 68_000,
                "lastWords": "## Note",
                "formatViolation": 'missing required heading "## Next"',
                "failureReason": "",
            },
        ),
        (
            "chrys/context_pressure",
            {
                "sessionId": "s1",
                "reason": "side_call_budget",
                "attempts": 2,
                "sideCallTokens": 300_000,
                "sideCallTokenBudget": 300_000,
                "source": "sub_agent",
                "invocationId": "inv-1",
            },
        ),
    ]
    assert client.updates == []


@pytest.mark.anyio
async def test_sub_agent_compaction_events_are_dual_path() -> None:
    """Sub-agent compaction events emit ext notifications AND standard parent tool-call updates."""
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)
    bridge = AcpEventBridge()

    await server._handle_event(
        "s1",
        InvocationStarted(
            agent_name="Explore",
            tool_name="explore",
            parent_call_id="call-1",
            session_id="s1",
            origin=InvocationOrigin("sub_agent", "s1", "inv1", None),
        ),
        bridge,
        {},
    )
    await server._handle_event(
        "s1",
        InvocationCompactionStarted(
            agent_name="Explore",
            compaction_id="c-1",
            session_id="s1",
            origin=InvocationOrigin("sub_agent", "s1", "inv1", None),
        ),
        bridge,
        {},
    )
    await server._handle_event(
        "s1",
        InvocationCompactionFinished(
            agent_name="Explore",
            compaction_id="c-1",
            outcome="ok",
            duration_ms=2_500,
            format_violation='missing required heading "## Next"',
            session_id="s1",
            origin=InvocationOrigin("sub_agent", "s1", "inv1", None),
        ),
        bridge,
        {},
    )

    await server._handle_event(
        "s1",
        InvocationCompactionCommitted(
            agent_name="Explore",
            compaction_id="c-1",
            session_id="s1",
            origin=InvocationOrigin("sub_agent", "s1", "inv1", None),
        ),
        bridge,
        {},
    )

    assert [method for method, _ in client.ext_notifications] == [
        "chrys/sub_agent_invocation_start",
        "chrys/sub_agent_compaction_started",
        "chrys/sub_agent_compaction_finished",
        "chrys/sub_agent_compaction_committed",
    ]
    started_payload = client.ext_notifications[1][1]
    assert started_payload["invocationId"] == "inv1"
    assert started_payload["compactionId"] == "c-1"
    finished_payload = client.ext_notifications[2][1]
    assert finished_payload["outcome"] == "ok"
    assert finished_payload["durationMs"] == 2_500
    assert finished_payload["formatViolation"] == 'missing required heading "## Next"'
    assert finished_payload["failureReason"] == ""
    committed_payload = client.ext_notifications[3][1]
    assert committed_payload["invocationId"] == "inv1"
    assert committed_payload["compactionId"] == "c-1"
    # Standard-only clients get parent tool-call progress updates too —
    # but the committed signal is ext-only (no human-facing note).
    assert len(client.updates) == 3
    assert all(update.update.tool_call_id == "call-1" for update in client.updates)


@pytest.mark.anyio
async def test_sub_agent_retry_and_abort_extension_requests_route_to_manager() -> None:
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]

    await server.ext_method("sub_agent/retry", {"sessionId": "s1", "invocationId": "inv1"})
    await server.ext_method("sub_agent/abort", {"sessionId": "s1", "invocationId": "inv2"})

    assert manager.sub_agent_retries == [("s1", "inv1")]
    assert manager.sub_agent_aborts == [("s1", "inv2")]


@pytest.mark.anyio
async def test_set_session_mode_routes_to_manager_and_emits_current_mode_update() -> None:
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)
    client = _FakeClient()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    result = await server.set_session_mode(mode_id="auto", session_id="s1")

    assert manager.approval_modes == [("s1", "auto")]
    assert isinstance(result, acp_schema.SetSessionModeResponse)
    assert len(client.updates) == 1
    assert client.updates[0].update.current_mode_id == "auto"


@pytest.mark.anyio
async def test_set_session_mode_rejects_unknown_mode() -> None:
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(_FakeClient())

    with pytest.raises(RequestError):
        await server.set_session_mode(mode_id="yolo", session_id="s1")
    assert manager.approval_modes == []


@pytest.mark.anyio
async def test_set_session_model_routes_to_manager_and_sends_runtime_update() -> None:
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)
    client = _FakeClient()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    result = await server.set_session_model(model_id="m1", session_id="s1")

    assert manager.model_switches == [("s1", "m1")]
    assert isinstance(result, acp_schema.SetSessionModelResponse)
    method, payload = client.ext_notifications[0]
    assert method == "chrys/runtime_update"
    # Unified envelope: {sessionId, runtime: {...}} — same shape as the
    # event-bridged senders, so a client handler reads one shape.
    assert payload["sessionId"] == "s1"
    assert "modelProfileId" in payload["runtime"]
    assert payload["runtime"]["sessionId"] == "s1"


@pytest.mark.anyio
async def test_set_workspace_sends_runtime_update() -> None:
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)
    client = _FakeClient()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    result = await server.ext_method("session/set_workspace", {"sessionId": "s1", "primaryCwd": "/tmp/project"})

    assert manager.workspace_updates == [("s1", "/tmp/project")]
    assert result["primaryCwd"] == "/tmp/project"
    # A workspace soft-restart can change skills/MCP/memory, so the client must
    # get a refreshed runtime envelope.
    method, payload = client.ext_notifications[0]
    assert method == "chrys/runtime_update"
    assert payload["sessionId"] == "s1"
    assert payload["runtime"]["sessionId"] == "s1"


@pytest.mark.anyio
async def test_session_mode_and_model_states_advertise_available_options() -> None:
    engine = _FakeEngine(
        runtime_details=AgentRuntimeDetails(model=RuntimeModelDetails(profile_id="m1", name="Mock")),
        approval_mode=ApprovalMode.AUTO,
    )
    host = _FakeHost(event_bus=EventBus(), engine=engine)
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]

    modes = server._session_mode_state("s1")
    models = server._session_model_state("s1")

    assert modes.current_mode_id == "auto"
    assert [mode.id for mode in modes.available_modes] == ["manual", "auto", "bypass"]
    assert models.current_model_id == "m1"
    assert [model.model_id for model in models.available_models] == ["m1"]


@pytest.mark.anyio
async def test_session_runtime_extension_returns_runtime_payload() -> None:
    runtime_details = AgentRuntimeDetails(
        model=RuntimeModelDetails(profile_id="m1", name="Mock", max_context_tokens=128000),
        mcp_tools={"server1": ["tool_a"]},
        skill_sources={"builtin": ["skill1"]},
        hook_sources=[
            RuntimeHookSourceDetails(
                scope="project",
                source_path="/repo/.chrys/hooks/hooks.yaml",
                hooks=[
                    RuntimeHookDetails(
                        id="guard",
                        event="before_tool_call",
                        execution_mode="blocking",
                        enabled=True,
                        description="Guard writes",
                    )
                ],
            )
        ],
    )
    engine = _FakeEngine(
        runtime_details=runtime_details,
        usage=UsageUpdate(
            input_tokens=60,
            output_tokens=40,
            total_tokens=100,
            pct=12.5,
            max_context_tokens=128000,
        ),
    )
    host = _FakeHost(event_bus=EventBus(), engine=engine)
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]

    result = await server.ext_method("chrys/session_runtime", {"sessionId": "s1"})

    assert result["sessionId"] == "s1"
    assert result["agentProfile"] == "Code"
    assert result["modelProfileId"] == "m1"
    assert result["maxContextTokens"] == 128000
    assert result["inputTokens"] == 60
    assert result["outputTokens"] == 40
    assert result["totalTokens"] == 100
    assert result["pct"] == 12.5
    assert result["runtimeDetails"]["mcp_tools"] == {"server1": ["tool_a"]}
    assert result["runtimeDetails"]["model"]["selection_source"] == "active"
    assert result["runtimeDetails"]["hook_sources"] == [
        {
            "scope": "project",
            "source_path": "/repo/.chrys/hooks/hooks.yaml",
            "hooks": [
                {
                    "id": "guard",
                    "event": "before_tool_call",
                    "execution_mode": "blocking",
                    "enabled": True,
                    "description": "Guard writes",
                }
            ],
        }
    ]


@pytest.mark.anyio
async def test_session_mutations_extension_returns_tracker_summary() -> None:
    mutation = FileMutation(
        path="/workspace/a.py",
        operation=MutationOp.MODIFY,
        source=MutationSource.WRITE_FILE,
        tool_call_id="tc1",
        timestamp=1.0,
        before_hash="before",
        after_hash="after",
    )
    tracker = _FakeMutationTracker(
        store=_FakeBlobStore(blobs={}),
        turns=[TurnMutations(turn_id=1, mutations=[mutation])],
        session_summary={"/workspace/a.py": FileHashDiff(before="before", after="after")},
    )
    engine = _FakeEngine(
        mutation_tracker=tracker,
        current_turn_number=2,
        rollback_turns=[0, 1],
        turn_records_value=[TurnRecord(turn_number=1, turn_id="turn-b")],
    )
    host = _FakeHost(event_bus=EventBus(), engine=engine)
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]

    result = await server.ext_method("session/mutations", {"sessionId": "s1"})

    assert result["sessionId"] == "s1"
    assert result["currentTurn"] == 2
    assert result["availableRollbackTurns"] == [0, 1]
    assert result["turns"] == [
        {
            "turnNumber": 1,
            "turnId": "turn-b",
            "mutationCount": 1,
            "mutations": [
                {
                    "path": "/workspace/a.py",
                    "operation": "modify",
                    "source": "write_file",
                    "toolCallId": "tc1",
                    "timestamp": 1.0,
                    "oldPath": None,
                    "beforeHash": "before",
                    "afterHash": "after",
                    "beforeSkip": None,
                    "afterSkip": None,
                    "provenance": "proven",
                    "contested": False,
                }
            ],
        }
    ]
    assert result["files"] == [
        {
            "path": "/workspace/a.py",
            "beforeHash": "before",
            "afterHash": "after",
            "operation": "modify",
            "contested": False,
            "inferred": False,
        }
    ]


@pytest.mark.anyio
async def test_session_mutations_reports_no_turn_id_when_two_turns_share_the_ordinal() -> None:
    """A drifted ordinal names two turns; the mutation data is reported, the identity is not."""
    mutation = FileMutation(
        path="/workspace/a.py",
        operation=MutationOp.MODIFY,
        source=MutationSource.WRITE_FILE,
        tool_call_id="tc1",
        timestamp=1.0,
    )
    tracker = _FakeMutationTracker(
        store=_FakeBlobStore(blobs={}),
        turns=[TurnMutations(turn_id=2, mutations=[mutation])],
    )
    engine = _FakeEngine(
        mutation_tracker=tracker,
        current_turn_number=2,
        turn_records_value=[
            TurnRecord(turn_number=2, turn_id="turn-early"),
            TurnRecord(turn_number=2, turn_id="turn-late"),
        ],
    )
    host = _FakeHost(event_bus=EventBus(), engine=engine)
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]

    result = await server.ext_method("session/mutations", {"sessionId": "s1"})

    assert result["turns"][0]["turnNumber"] == 2
    assert result["turns"][0]["turnId"] is None
    assert result["turns"][0]["mutationCount"] == 1


@pytest.mark.anyio
async def test_session_mutations_reports_no_turn_id_when_nothing_registered_the_ordinal() -> None:
    mutation = FileMutation(
        path="/workspace/a.py",
        operation=MutationOp.MODIFY,
        source=MutationSource.WRITE_FILE,
        tool_call_id="tc1",
        timestamp=1.0,
    )
    tracker = _FakeMutationTracker(
        store=_FakeBlobStore(blobs={}),
        turns=[TurnMutations(turn_id=5, mutations=[mutation])],
    )
    engine = _FakeEngine(mutation_tracker=tracker, current_turn_number=5, turn_records_value=[])
    host = _FakeHost(event_bus=EventBus(), engine=engine)
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]

    result = await server.ext_method("session/mutations", {"sessionId": "s1"})

    assert result["turns"][0]["turnNumber"] == 5
    assert result["turns"][0]["turnId"] is None
    assert result["turns"][0]["mutations"][0]["path"] == "/workspace/a.py"


@pytest.mark.anyio
async def test_rollback_reports_the_turns_it_superseded() -> None:
    """A client that labelled its transcript by turn joins on the discarded ids."""
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)
    manager.rollback_superseded_turn_ids = ["turn-c", "turn-b"]
    client = _FakeClient()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    result = await server.ext_method("session/rollback", {"sessionId": "s1", "targetTurn": 1})

    assert result["supersededTurnIds"] == ["turn-c", "turn-b"]
    assert client.ext_notifications[0][1]["supersededTurnIds"] == ["turn-c", "turn-b"]


@pytest.mark.anyio
async def test_session_mutations_extension_without_tracker_returns_empty_summary() -> None:
    host = _FakeHost(event_bus=EventBus(), engine=_FakeEngine(mutation_tracker=None, current_turn_number=3))
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]

    result = await server.ext_method("session/mutations", {"sessionId": "s1"})

    assert result == {
        "sessionId": "s1",
        "currentTurn": 3,
        "availableRollbackTurns": [],
        "turns": [],
        "files": [],
    }


@pytest.mark.anyio
async def test_session_diff_extension_returns_text_entries_and_filters() -> None:
    tracker = _FakeMutationTracker(
        store=_FakeBlobStore(blobs={"before": b"old", "after": b"new"}),
        session_summary={"/workspace/a.py": FileHashDiff(before="before", after="after", contested=True)},
        turn_summaries={1: {"/workspace/b.py": FileHashDiff(before=None, after="after", inferred=True)}},
    )
    host = _FakeHost(event_bus=EventBus(), engine=_FakeEngine(mutation_tracker=tracker))
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]

    session_diff = await server.ext_method("session/diff", {"sessionId": "s1"})
    path_diff = await server.ext_method("session/diff", {"sessionId": "s1", "path": "/workspace/a.py"})
    turn_diff = await server.ext_method("session/diff", {"sessionId": "s1", "turn": 1})

    assert session_diff["entries"] == [
        {
            "path": "/workspace/a.py",
            "operation": "modify",
            "beforeHash": "before",
            "afterHash": "after",
            "beforeText": "old",
            "afterText": "new",
            "isBinary": False,
            "bytesChanged": True,
            "contested": True,
            "inferred": False,
        }
    ]
    assert path_diff["entries"][0]["path"] == "/workspace/a.py"
    assert turn_diff == {
        "sessionId": "s1",
        "turn": 1,
        "entries": [
            {
                "path": "/workspace/b.py",
                "operation": "create",
                "beforeHash": None,
                "afterHash": "after",
                "beforeText": "",
                "afterText": "new",
                "isBinary": False,
                "bytesChanged": True,
                "contested": False,
                "inferred": True,
            }
        ],
    }


@pytest.mark.anyio
async def test_integer_extension_params_reject_bool_values() -> None:
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]

    with pytest.raises(RequestError):
        await server.ext_method("session/rollback", {"sessionId": "s1", "targetTurn": True})
    with pytest.raises(RequestError):
        await server.ext_method("session/diff", {"sessionId": "s1", "turn": False})

    assert manager.rollbacks == []


@pytest.mark.anyio
async def test_session_delete_extension_routes_to_manager() -> None:
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]

    await server.ext_method("session/delete", {"sessionId": "s1", "cwd": "/tmp/project"})

    assert manager.deleted_sessions == [("/tmp/project", "s1")]


@pytest.mark.anyio
async def test_mcp_list_and_skills_list_extension_read_runtime_details() -> None:
    runtime_details = AgentRuntimeDetails(
        mcp_tools={"server1": ["tool_a"]},
        mcp_failures={"server2": "timeout"},
        skill_sources={"builtin": ["skill1"]},
        skill_details=[
            RuntimeSkillDetails(
                name="skill1", description="desc", source="builtin", content_sha256="a" * 64, file_based=True
            )
        ],
    )
    host = _FakeHost(event_bus=EventBus(), engine=_FakeEngine(runtime_details=runtime_details))
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]

    mcp = await server.ext_method("mcp/list", {"sessionId": "s1"})
    skills = await server.ext_method("skills/list", {"sessionId": "s1"})

    assert mcp == {
        "sessionId": "s1",
        "mcpTools": {"server1": ["tool_a"]},
        "mcpFailures": {"server2": "timeout"},
    }
    assert skills == {
        "sessionId": "s1",
        "skillSources": {"builtin": ["skill1"]},
        "skillDetails": [{"name": "skill1", "description": "desc", "source": "builtin"}],
    }


@pytest.mark.anyio
async def test_initialize_advertises_additional_directories_capability() -> None:
    server = ChrysAcpServer(_FakeManager(_FakeHost(event_bus=EventBus())), initial_vision=False)  # type: ignore[arg-type]

    response = await server.initialize(protocol_version=PROTOCOL_VERSION)

    caps = response.agentCapabilities.sessionCapabilities
    # Multi-root support must be discoverable so conforming clients send the option.
    assert caps.additional_directories is not None
    assert caps.close is not None
    assert caps.list is not None


_ManagerRead = tuple[Callable[[_FakeManager], Any], Any]
"""A typed reader for one manager attribute paired with the value it must hold."""


@dataclass(frozen=True)
class _RouteCase:
    """One ``ext_method`` route: what it answers and what it leaves on the manager."""

    method: str
    params: dict[str, Any] = field(default_factory=dict)
    expected: dict[str, Any] = field(default_factory=dict)
    manager_reads: tuple[_ManagerRead, ...] = ()
    expects_runtime_update: bool = False
    # Routes whose whole response body is part of the contract, not just the keys named above.
    exact_response: bool = False


_OPTIONS = [{"key": "theme", "envKey": "CHRYS_THEME", "settingKey": "ui.theme", "value": "chrys"}]

_ROUTE_CASES = [
    pytest.param(
        _RouteCase(
            "session/switch_agent",
            {"sessionId": "s1", "agentProfile": "QA"},
            {"toProfile": "QA"},
            ((lambda manager: manager.agent_switches, [("s1", "QA")]),),
        ),
        id="switch_agent",
    ),
    pytest.param(
        # settings/reload must push a runtime_update so clients refresh after a reload.
        _RouteCase(
            "settings/reload",
            {"sessionId": "s1"},
            {},
            ((lambda manager: manager.settings_reloads, ["s1"]),),
            expects_runtime_update=True,
        ),
        id="settings_reload",
    ),
    pytest.param(
        _RouteCase(
            "session/set_workspace",
            {"sessionId": "s1", "primaryCwd": "/tmp/project"},
            {"primaryCwd": "/tmp/project"},
            ((lambda manager: manager.workspace_updates, [("s1", "/tmp/project")]),),
        ),
        id="set_workspace",
    ),
    pytest.param(
        _RouteCase(
            "session/history",
            {"sessionId": "s1"},
            {
                "turns": [],
                "messages": [
                    {
                        "turnId": None,
                        "turnNumber": None,
                        "message": {"role": "user", "contents": [{"type": "text", "text": "hello s1"}]},
                    }
                ],
            },
        ),
        id="session_history",
    ),
    pytest.param(
        _RouteCase("profiles/agents/list", {}, {"agents": [{"name": "Code", "displayName": "Code"}]}),
        id="agents_list",
    ),
    pytest.param(
        _RouteCase("profiles/agents/read", {"name": "Code"}, {"profile": {"name": "Code", "description": "agent"}}),
        id="agents_read",
    ),
    pytest.param(
        _RouteCase("profiles/agents/write", {"profile": {"name": "Custom"}}, {"profile": {"name": "Custom"}}),
        id="agents_write",
    ),
    pytest.param(
        _RouteCase("profiles/agents/delete", {"name": "Custom"}, {"deleted": True}),
        id="agents_delete",
    ),
    pytest.param(
        _RouteCase(
            "profiles/agents/reset",
            {"name": "Code"},
            {"profile": {"name": "Code", "builtin": True}, "changed": True},
            exact_response=True,
        ),
        id="agents_reset",
    ),
    pytest.param(
        _RouteCase("profiles/models/list", {}, {"models": [{"id": "m1", "name": "Model", "modelId": "gpt"}]}),
        id="models_list",
    ),
    pytest.param(
        _RouteCase("profiles/models/read", {"id": "m1"}, {"profile": {"id": "m1", "api_key": ""}}),
        id="models_read",
    ),
    pytest.param(
        _RouteCase(
            "profiles/models/write",
            {"profile": {"id": "m2", "name": "Other"}},
            {"profile": {"id": "m2", "name": "Other"}},
        ),
        id="models_write",
    ),
    pytest.param(
        _RouteCase("profiles/models/delete", {"id": "m2"}, {"deleted": True}),
        id="models_delete",
    ),
    pytest.param(
        _RouteCase(
            "mcp/test",
            {"server": {"name": "test", "transport": "stdio"}},
            {"ok": True},
            ((lambda manager: manager.mcp_tests, [{"name": "test", "transport": "stdio"}]),),
        ),
        id="mcp_test",
    ),
    pytest.param(
        # The scope the manager answers for is the server's to forward verbatim:
        # no sessionId means the base query, never a guessed session.
        _RouteCase(
            "settings/options",
            {},
            {"options": _OPTIONS},
            ((lambda manager: manager.config_option_queries, [None]),),
        ),
        id="settings_options",
    ),
    pytest.param(
        _RouteCase(
            "settings/options",
            {"sessionId": "s1"},
            {"options": _OPTIONS},
            ((lambda manager: manager.config_option_queries, ["s1"]),),
        ),
        id="settings_options_scoped",
    ),
    pytest.param(
        _RouteCase(
            "session/set_config_option",
            {"sessionId": "s1", "key": "theme", "value": "dark"},
            {
                "sessionId": "s1",
                "key": "theme",
                "envKey": "CHRYS_THEME",
                "settingKey": "ui.theme",
                "value": "dark",
            },
            (
                (lambda manager: manager.config_updates, [("theme", "dark")]),
                (lambda manager: manager.settings_reloads, ["s1"]),
            ),
            exact_response=True,
        ),
        id="set_config_option",
    ),
]


@pytest.mark.parametrize("case", _ROUTE_CASES)
@pytest.mark.anyio
async def test_remaining_extension_requests_route_to_manager(case: _RouteCase) -> None:
    host = _FakeHost(event_bus=EventBus())
    manager = _FakeManager(host)
    client = _FakeClient()
    server = ChrysAcpServer(manager, initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    result = await server.ext_method(case.method, case.params)

    if case.exact_response:
        assert result == case.expected
    else:
        for key, value in case.expected.items():
            if isinstance(value, bool):
                # Protocol booleans must stay JSON booleans: ``1 == True``, so compare by identity.
                assert result[key] is value
            else:
                assert result[key] == value
    for read, expected in case.manager_reads:
        assert read(manager) == expected
    if case.expects_runtime_update:
        assert ("chrys/runtime_update", {"sessionId": "s1"}) in [
            (method, {"sessionId": params.get("sessionId")}) for method, params in client.ext_notifications
        ]


@pytest.mark.anyio
async def test_runtime_extension_events_are_exposed() -> None:
    host = _FakeHost(event_bus=EventBus())
    client = _FakeClient()
    server = ChrysAcpServer(_FakeManager(host), initial_vision=False)  # type: ignore[arg-type]
    server.on_connect(client)

    await server._handle_event(
        "s1",
        AgentLoadProgress(phase="mcp", message="Loading tools", current=1, total=2),
        AcpEventBridge(),
        {},
    )
    await server._handle_event(
        "s1",
        UsageUpdate(total_tokens=100, pct=10.0, max_context_tokens=1000),
        AcpEventBridge(),
        {},
    )
    await server._handle_event(
        "s1",
        WorkspaceUpdated(primary_cwd="/tmp/project", working_dirs=["/tmp/project"], reference_files=[]),
        AcpEventBridge(),
        {},
    )

    assert client.ext_notifications == [
        (
            "chrys/agent_load_progress",
            {
                "sessionId": "s1",
                "phase": "mcp",
                "message": "Loading tools",
                "serverName": "",
                "current": 1,
                "total": 2,
                "failed": 0,
            },
        ),
        (
            "chrys/usage_update",
            {
                "sessionId": "s1",
                "agentProfile": "",
                "usageSourceId": "",
                "inputTokens": 0,
                "outputTokens": 0,
                "totalTokens": 100,
                "pct": 10.0,
                "maxContextTokens": 1000,
                "totalSessionTokens": 0,
                "totalSessionInputTokens": 0,
                "totalSessionOutputTokens": 0,
                "cacheHitTokens": None,
                "totalSessionCacheHitTokens": None,
                "localTokens": 0,
                "calibrationRatio": 1.0,
                "systemOverheadTokens": 0,
            },
        ),
        (
            "chrys/workspace_updated",
            {
                "sessionId": "s1",
                "primaryCwd": "/tmp/project",
                "workingDirs": ["/tmp/project"],
                "referenceFiles": [],
            },
        ),
    ]
