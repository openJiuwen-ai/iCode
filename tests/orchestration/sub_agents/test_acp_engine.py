# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Full engine wiring for external ACP sub-agent profiles."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    InvocationAnswerWithdrawn,
    InvocationCascadeAborted,
    InvocationContentDelta,
    InvocationMessage,
    InvocationPaused,
    InvocationProgress,
    InvocationResumed,
    InvocationRetryRequested,
    InvocationStarted,
    InvocationToolCallResult,
    QuestionToUser,
    UsageUpdate,
    UserInterrupt,
    UserMessage,
)
from chrys.foundation.models.workspace import Workspace
from chrys.orchestration.sub_agents.acp_policy import AcpSubAgentPolicy
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AcpAgentConfig,
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    SubAgentRef,
    SubAgentsConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.session import sub_agent_logs
from chrys.service.session.sub_agent_transcript import load_persisted_sub_agent_transcript
from chrys.service.state.store import JsonFileStateStore
from tests.support.acp_fixtures import STUB_SCRIPT
from tests.support.paths import SRC_ROOT
from tests.support.platform_fakes import platform_with_config_dir
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, wait_for, with_wait_deadline


async def _start_acp_engine(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine: Any,
    scenario: str,
    flag_path: Path | None = None,
    expected_tools: list[str] | None = None,
    allow_user_interaction: bool = True,
    launch_fields: dict[str, Any] | None = None,
) -> tuple[Any, EventBus, MockChatClient, dict[type, list[Any]]]:
    # Both processes use the same isolated workspace; stub code/import paths
    # stay absolute so no repository checkout is needed as the working directory.
    workdir = tmp_path / "workspace"
    workdir.mkdir(exist_ok=True)
    bus = EventBus()
    captured: dict[type, list[Any]] = {}

    async def collect(event: Any) -> None:
        captured.setdefault(type(event), []).append(event)

    for event_type in (
        InvocationMessage,
        InvocationContentDelta,
        InvocationAnswerWithdrawn,
        InvocationCascadeAborted,
        InvocationStarted,
        InvocationPaused,
        InvocationProgress,
        InvocationResumed,
        QuestionToUser,
        InvocationToolCallResult,
        UsageUpdate,
    ):
        await bus.subscribe(event_type, collect)

    main_client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("Explore", "delegate-1", {"prompt": "inspect the project"})]),
            MockResponse(text="Parent finished"),
        ]
    )

    async def create_client(_profile: Any = None, **kwargs: Any) -> MockChatClient:
        main_client._on_intermediate_text = kwargs.get("on_intermediate_text")
        return main_client

    monkeypatch.setattr(builder_module, "create_client", create_client)
    monkeypatch.setattr(
        sub_agent_logs,
        "get_platform",
        lambda: platform_with_config_dir(tmp_path / "config"),
    )

    class FastAcpController(AcpSubAgentPolicy):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("backoff_schedule", (0, 0, 0, 0, 0))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr("chrys.orchestration.sub_agents.acp_policy.AcpSubAgentPolicy", FastAcpController)

    model_registry = ModelProfileRegistry()
    model_registry.register(
        ModelProfile(
            id="main-model",
            name="main",
            provider="mock",
            model_id="mock",
            stream=False,
            max_context_tokens=100_000,
        )
    )
    parent = AgentProfile(
        name="Parent",
        tools=ToolsConfig(),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
        sub_agents=SubAgentsConfig(
            max_total_concurrency=2,
            agents=[SubAgentRef(profile="External", tool_name="Explore")],
        ),
    )
    env = {
        "PYTHONPATH": str(SRC_ROOT),
        "CHRYS_ACP_STUB_SCENARIO": scenario,
    }
    if flag_path is not None:
        env["CHRYS_ACP_STUB_FLAG_FILE"] = str(flag_path)
    external = AgentProfile(
        name="External",
        display_name="External",
        sub_agent_only=True,
        acp=AcpAgentConfig(
            **{
                "command": sys.executable,
                "args": [str(STUB_SCRIPT)],
                "env": env,
                "cwd": str(workdir),
                "handshake_timeout_seconds": 20,
                "idle_timeout_seconds": 2,
                **(launch_fields or {}),
            }
        ),
    )
    registry = AgentProfileRegistry()
    registry.register(parent)
    registry.register(external)
    engine = agent_engine(
        bus,
        settings=Settings(model_profile="main-model", workspace_change_notice=False),
        agent_registry=registry,
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
        initial_workspace=Workspace(primary_cwd=str(workdir)),
        allow_user_interaction=allow_user_interaction,
    )
    await engine.start(parent)
    assert engine.current.loaded.sub_agent_tools is not None
    assert engine.current.loaded.sub_agent_tools.tool_names() == (
        ["Explore"] if expected_tools is None else expected_tools
    )
    return engine, bus, main_client, captured


@pytest.mark.asyncio
async def test_acp_profile_runs_through_agent_engine_with_usage_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine: Any,
) -> None:
    engine, bus, main_client, captured = await _start_acp_engine(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        agent_engine=agent_engine,
        scenario="usage_gauge",
    )
    await bus.publish(UserMessage(text="Delegate this"))
    await wait_for(lambda: main_client.call_count == 2, timeout=20, description="parent completion")
    await wait_for(
        lambda: any(event.text == "Parent finished" for event in captured.get(InvocationMessage, [])),
        timeout=20,
        description="final agent message",
    )
    await engine.wait_for_run_task()

    results = [event for event in captured.get(InvocationToolCallResult, []) if event.tool_name == "Explore"]
    assert results and results[-1].result == "stub response"
    assert engine.session.runtime_meta.total_session_tokens == 8
    assert engine.session.runtime_meta.total_session_input_tokens == 3
    assert engine.session.runtime_meta.total_session_output_tokens == 5
    assert engine.session.runtime_meta.total_session_cache_hit_tokens == 1
    usages = [event for event in captured.get(UsageUpdate, []) if event.agent_profile == "External"]
    assert usages[-1].total_tokens == 120
    assert usages[-1].max_context_tokens == 1_000

    session_dir = engine.session.session_dir
    assert session_dir is not None
    audit_paths = list((session_dir / "sub_agents" / "sessions").glob("*.json"))
    assert len(audit_paths) == 1
    envelope = json.loads(audit_paths[0].read_text(encoding="utf-8"))
    assert envelope["meta"]["runner"] == "acp"
    assert envelope["acp_state"]["stop_reason"] == "end_turn"
    assert envelope["acp_state"]["launch"]["args"] == ["<redacted>"]
    if os.name != "nt":
        assert audit_paths[0].stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "launch_fields",
    [
        {"command": "{{!tool --password hunter2}}"},
        {"command": "{{CHRYS_TEST_NOT_SET}}", "args": ["--header {{!tool --password hunter2}}"]},
    ],
    ids=["refused_command", "another_field_unresolved"],
)
@pytest.mark.asyncio
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_launch_that_never_resolved_records_no_command_text_in_the_audit(
    launch_fields: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine: Any,
) -> None:
    engine, bus, main_client, captured = await _start_acp_engine(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        agent_engine=agent_engine,
        scenario="usage_gauge",
        launch_fields=launch_fields,
    )

    await bus.publish(UserMessage(text="Delegate this"))
    await wait_for(lambda: main_client.call_count == 2, timeout=20, description="parent completion")
    await engine.wait_for_run_task()

    results = [event for event in captured.get(InvocationToolCallResult, []) if event.tool_name == "Explore"]
    assert results and results[-1].result.startswith("Error: ")
    assert "hunter2" not in results[-1].result
    session_dir = engine.session.session_dir
    assert session_dir is not None
    audit_paths = list((session_dir / "sub_agents" / "sessions").glob("*.json"))
    assert len(audit_paths) == 1
    assert "hunter2" not in audit_paths[0].read_text(encoding="utf-8")
    assert json.loads(audit_paths[0].read_text(encoding="utf-8"))["acp_state"]["launch"]["command"] == ""


@pytest.mark.asyncio
async def test_noninteractive_engine_suppresses_acp_request_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine: Any,
) -> None:
    engine, bus, main_client, captured = await _start_acp_engine(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        agent_engine=agent_engine,
        scenario="ask_user",
        allow_user_interaction=False,
    )

    await bus.publish(UserMessage(text="Delegate this"))
    await wait_for(lambda: main_client.call_count == 2, timeout=20, description="parent completion")
    await engine.wait_for_run_task()

    results = [event for event in captured.get(InvocationToolCallResult, []) if event.tool_name == "Explore"]
    assert results and results[-1].result == "answer:"
    assert captured.get(QuestionToUser, []) == []


@pytest.mark.asyncio
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_acp_retry_from_engine_starts_fresh_attempt_and_discards_partial_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine: Any,
) -> None:
    engine, bus, main_client, captured = await _start_acp_engine(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        agent_engine=agent_engine,
        scenario="text_then_crash_once",
        flag_path=tmp_path / "crashed.flag",
    )
    await bus.publish(UserMessage(text="Delegate this"))
    await wait_for(
        lambda: bool(captured.get(InvocationPaused)),
        timeout=20,
        description="ACP transport pause",
    )
    pause = captured[InvocationPaused][-1]
    assert pause.reason == "acp_transport"
    assert pause.diagnostic_path
    session_dir = engine.session.session_dir
    assert session_dir is not None
    pending = list((session_dir / "sub_agents" / "pending").glob("*.json"))
    assert len(pending) == 1
    assert json.loads(pending[0].read_text(encoding="utf-8"))["runner"] == "acp"
    if os.name != "nt":
        assert pending[0].stat().st_mode & 0o777 == 0o600
    # The paused audit envelope must reflect the live controller, not the
    # zero-initialized stats snapshot: a crash while paused is the last write.
    audit_paths = list((session_dir / "sub_agents" / "sessions").glob("*.json"))
    assert len(audit_paths) == 1
    paused_envelope = json.loads(audit_paths[0].read_text(encoding="utf-8"))
    assert paused_envelope["meta"]["status"] == "paused"
    assert paused_envelope["meta"]["failure_reason"] == "acp_transport"
    assert paused_envelope["meta"]["last_error"]
    assert paused_envelope["meta"]["usage_unreported_attempts"] == 1

    await bus.publish(InvocationRetryRequested(invocation_id=pause.origin.invocation_id))
    await wait_for(lambda: bool(captured.get(InvocationResumed)), description="ACP resume")
    await wait_for(lambda: main_client.call_count == 2, timeout=20, description="parent completion")
    await engine.wait_for_run_task()
    # The retry's new session asks again for the answer the crashed one showed: the card withdraws it.
    (discarded,) = {delta.segment_id for delta in captured[InvocationContentDelta] if delta.text == "discarded partial"}
    (withdrawn,) = captured[InvocationAnswerWithdrawn]
    assert withdrawn.origin == pause.origin
    assert withdrawn.segment_ids == (discarded,)
    results = [event for event in captured.get(InvocationToolCallResult, []) if event.tool_name == "Explore"]
    assert results[-1].result == "stub response"
    assert "discarded partial" not in results[-1].result
    assert pause.diagnostic_path not in results[-1].result
    progress = captured[InvocationProgress][-1]
    assert progress.total_usage_tokens == 8
    assert progress.usage_unreported_attempts == 1
    await wait_for(
        lambda: not list((session_dir / "sub_agents" / "pending").glob("*.json")),
        description="post-save pending cleanup",
    )
    # The audit records the withdrawal: reopened, the card shows the retried answer once, as it did live.
    transcript = await load_persisted_sub_agent_transcript(session_dir, audit_paths[0].name)
    assert transcript is not None
    texts = [content["text"] for message in transcript.messages for content in message["contents"] if "text" in content]
    assert texts == ["stub response"]


@pytest.mark.asyncio
async def test_user_interrupt_cascades_through_live_acp_controller(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine: Any,
) -> None:
    engine, bus, _main_client, captured = await _start_acp_engine(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        agent_engine=agent_engine,
        scenario="idle_stall",
    )
    await bus.publish(UserMessage(text="Delegate this"))
    await wait_for(
        lambda: bool(engine.current.loaded.sub_agent_tools and engine.current.loaded.sub_agent_tools._controllers),
        timeout=20,
        description="live ACP controller",
    )
    await bus.publish(UserInterrupt())
    await wait_for(
        lambda: bool(captured.get(InvocationCascadeAborted)),
        timeout=20,
        description="ACP cascade event",
    )
    await engine.wait_for_run_task()


@pytest.mark.asyncio
async def test_builder_skips_acp_registration_at_recursion_depth_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine: Any,
) -> None:
    monkeypatch.setenv("CHRYS_ACP_SUBAGENT_DEPTH", "3")
    engine, _bus, main_client, _captured = await _start_acp_engine(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        agent_engine=agent_engine,
        scenario="happy",
        expected_tools=[],
    )
    assert engine.current.loaded.sub_agent_tools is not None
    assert engine.current.loaded.sub_agent_tools.tool_names() == []
    assert main_client.call_count == 0


@pytest.mark.asyncio
async def test_engine_main_build_rejects_acp_profile_before_client_construction(
    tmp_path: Path,
    agent_engine: Any,
) -> None:
    profile = AgentProfile(
        name="External",
        sub_agent_only=True,
        acp=AcpAgentConfig(command="agent"),
    )
    engine = agent_engine(
        EventBus(),
        settings=Settings(),
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )
    with pytest.raises(ValueError, match="cannot be launched as the main agent"):
        await engine.start(profile)
