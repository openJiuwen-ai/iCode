# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A workflow agent node delegates to sub-agents, sees its skills and reads its memory like the chat agent."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.sub_agents.tools as sub_agent_module
import chrys.orchestration.workflows.agent_node as agent_node_module
import chrys.orchestration.workflows.agent_node_build as agent_node_build_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.types import (
    InvocationAborted,
    InvocationPaused,
    InvocationStarted,
    InvocationToolCallResult,
    InvocationToolCallStart,
    SetApprovalMode,
    WorkflowNodeRetryRequest,
    WorkflowNodeStateChanged,
)
from chrys.foundation.tool_result_metadata import TOOL_RESULT_METADATA_KEY
from chrys.kernel import ChatResponseUpdate, Content
from chrys.orchestration.invoker.resources import Conversation
from chrys.orchestration.invoker.runtime import ApprovalInputs
from chrys.orchestration.workflows.agent_node_build import KernelNodeParts
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.approval.policy import ApprovalMode
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    MemoryConfig,
    SkillsConfig,
    SubAgentRef,
    SubAgentsConfig,
)
from chrys.service.session.sub_agent_logs import pending_dir, sessions_dir
from chrys.service.workflows.layout import run_dir
from chrys.service.workflows.transcript import read_node_transcript
from tests.orchestration.sub_agents._pipeline import use_fast_sub_agent_policy
from tests.orchestration.workflows._hosting import (
    PROFILE,
    confirm,
    make_host,
    make_profile,
    make_project,
    patch_runtime,
    run,
    write_workflow,
)
from tests.orchestration.workflows._transcript_support import ArchiveClient
from tests.orchestration.workflows._transcript_support import source as archive_source
from tests.support.event_capture import capture_events
from tests.support.extensions import install_file_extension
from tests.support.scripted_clients import ErrorMockChatClient, FrameworkBoom
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from chrys.kernel import FinishReason, FinishReasonLiteral

_WORKFLOW = (
    "from chrys.workflows import WorkflowBuilder\nwf = WorkflowBuilder('agent')\n"
    f"node = wf.agent('node', profile={PROFILE!r})\nwf.start(node)\nwf.output(node)\nworkflow = wf.build()\n"
).encode()


def _delegating_profile() -> AgentProfile:
    profile = make_profile()
    profile.sub_agents = SubAgentsConfig(max_total_concurrency=1, agents=[SubAgentRef(profile="Child")])
    return profile


def _child_profile() -> AgentProfile:
    child = make_profile("Child")
    child.description = "Inspects on the node's behalf."
    child.sub_agent_only = True
    return child


def _hand_out_child_client(monkeypatch: pytest.MonkeyPatch, client: MockChatClient) -> None:
    """The node's sub-agent registration creates the child's client through the sub-agent module."""

    async def create_client(
        model_profile: Any,
        *,
        session_id: str,
        parent_session_id: str,
        use_route_session_context: bool,
        session_dir: Path,
        tool_result_ceiling_tokens: int | None,
    ) -> MockChatClient:
        return client

    monkeypatch.setattr(sub_agent_module, "create_client", create_client)


def _delegate(prompt: str) -> MockResponse:
    return MockResponse(tool_calls=[("Child", "call-1", {"prompt": prompt})])


async def test_a_node_delegates_to_its_profile_sub_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node_client = MockChatClient(responses=[_delegate("inspect the tree"), MockResponse(text="node done")])
    child_client = MockChatClient(responses=[MockResponse(text="child says the tree is fine")])
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client])
    _hand_out_child_client(monkeypatch, child_client)
    project = make_project(tmp_path)
    write_workflow(project, "delegate", _WORKFLOW)
    host = make_host(tmp_path, project=project, profiles=[_delegating_profile(), _child_profile()])
    starts = await capture_events(host.event_bus, InvocationStarted)
    tool_starts = await capture_events(host.event_bus, InvocationToolCallStart)
    tool_results = await capture_events(host.event_bus, InvocationToolCallResult)
    try:
        await confirm(host, "delegate")
        result, _events = await run(host, "delegate", input_text="Go")
        session_dir = host.workflow_session_dir
        assert session_dir is not None
    finally:
        await host.shutdown()

    assert result.outcome.value == "completed"
    assert child_client.call_count == 1
    node_start, child_start = starts
    assert node_start.origin.kind == "workflow_node"
    assert child_start.origin.kind == "sub_agent"
    assert child_start.origin.parent == node_start.origin
    # The delegation is bound to the node's tool call exactly as a chat turn's would be.
    (delegation,) = [event for event in tool_starts if event.origin == node_start.origin]
    assert delegation.tool_name == "Child"
    assert child_start.parent_call_id == delegation.call_id
    (delegated,) = [event for event in tool_results if event.origin == node_start.origin]
    assert "child says the tree is fine" in delegated.result
    assert delegated.metadata["sub_agent_invocation_id"] == child_start.origin.invocation_id
    assert delegated.metadata["sub_agent_audit_complete"] is True
    # The child's identity persists in the node's transcript, where the audit reader resolves it.
    transcript = read_node_transcript(run_dir(session_dir, result.run_id), "node@iter#1", 1)
    assert transcript is not None
    (persisted,) = [
        content
        for message in transcript.replay.messages
        for content in message["contents"]
        if content["type"] == "function_result"
    ]
    persisted_metadata = persisted["additional_properties"][TOOL_RESULT_METADATA_KEY]
    assert persisted_metadata["sub_agent_invocation_id"] == child_start.origin.invocation_id
    assert (sessions_dir(session_dir) / persisted_metadata["sub_agent_log_file"]).is_file()
    # The finished child left no control record behind for a chat restore to find.
    assert not pending_dir(session_dir).exists() or list(pending_dir(session_dir).iterdir()) == []


async def test_a_node_has_sleep_though_its_profile_omits_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pause = MockResponse(tool_calls=[("sleep", "call-1", {"seconds": 0, "reason": "pause"})])
    node_client = MockChatClient(responses=[pause, MockResponse(text="node done")])
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client], builtin_tools=True)
    project = make_project(tmp_path)
    write_workflow(project, "pause", _WORKFLOW)
    host = make_host(tmp_path, project=project, profiles=[make_profile()])
    try:
        await confirm(host, "pause")
        result, _events = await run(host, "pause", input_text="Go")
    finally:
        await host.shutdown()

    assert result.outcome.value == "completed"
    messages, _options = node_client.call_history[1]
    results = [
        content.result for message in messages for content in message.contents if content.type == "function_result"
    ]
    assert results == ["Slept for 0 seconds."]


async def test_a_node_sees_its_profile_skills_and_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node_client = MockChatClient(responses=[MockResponse(text="node done")])
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client])
    project = make_project(tmp_path)
    (project / "NOTES.md").write_text("Remember: the build is green.", encoding="utf-8")
    skill_dir = tmp_path / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: review\ndescription: Reviews a change.\n---\n\nLook at every hunk.\n", encoding="utf-8"
    )
    profile = make_profile()
    profile.skills = SkillsConfig(
        paths=[str(tmp_path / "skills")], auto_load_user_agents_skills=False, auto_load_cwd_agents_skills=False
    )
    profile.memory = MemoryConfig(files=["NOTES.md"])
    write_workflow(project, "skilled", _WORKFLOW)
    host = make_host(tmp_path, project=project, profiles=[profile])
    try:
        await confirm(host, "skilled")
        result, _events = await run(host, "skilled", input_text="Go")
    finally:
        await host.shutdown()

    assert result.outcome.value == "completed"
    ((messages, options),) = node_client.call_history
    assert "Remember: the build is green." in options["instructions"]
    assert "<name>review</name>" in (messages[-1].text or "")
    assert {"load_skill", "read_skill_resource", "run_skill_script"} <= {tool.name for tool in options["tools"]}


async def test_a_node_asks_its_extension_contexts_once_and_its_retry_keeps_their_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user's Retry runs the next attempt from another task: it continues the read the failed attempt kept
    after the opener, which renders as the first attempt sent it, though the context would answer differently."""
    install_file_extension(
        monkeypatch,
        "acme_task",
        "from chrys.extensions import Extension\nextension = Extension()\nASKED = []\n\n"
        "@extension.context\nasync def task(ctx):\n    ASKED.append(ctx.prompt)\n"
        "    return f'Asked {len(ASKED)} time(s) to: {ctx.prompt}'\n",
    )
    project = make_project(tmp_path)
    target = project / "input.txt"
    target.write_text("Retained work", encoding="utf-8")
    client = ArchiveClient(target, "failed")
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client], builtin_tools=True)
    write_workflow(project, "archive", archive_source())
    profile = make_profile(builtins=["filesystem.read"])
    profile.extensions = ["acme_task"]
    host = make_host(tmp_path, project=project, profiles=[profile], allow_user_interaction=True)
    paused = await capture_events(host.event_bus, WorkflowNodeStateChanged)
    task = None
    try:
        await confirm(host, "archive")
        task = asyncio.create_task(run(host, "archive", input_text="Read twice"))
        await wait_for(
            lambda: any(event.state == "awaiting_retry" for event in paused) or task.done(),
            timeout=ENGINE_TURN_TIMEOUT,
            description="the failed attempt waiting for the user",
        )
        if task.done():
            await task
        (failed,) = [event for event in paused if event.state == "awaiting_retry"]
        client.outcome = "completed"
        await host.event_bus.publish(
            WorkflowNodeRetryRequest(
                run_id=failed.run_id,
                node_id=failed.node_id,
                activation_id=failed.activation_id,
                expected_failed_attempt=1,
                request_id="retry",
            )
        )
        result, _events = await task
    finally:
        await host.shutdown()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    assert result.outcome.value == "completed"
    assert sys.modules["_chrys_extension_acme_task"].ASKED == ["Read twice"]
    first, retried = client.call_history[0][0], client.call_history[-1][0]
    opener = first[-1]
    assert "Asked 1 time(s) to: Read twice" in (opener.text or "")
    roles = [message.role for message in retried]
    assert roles[:3] == ["user", "assistant", "tool"]
    assert roles.count("user") == 1
    assert retried[0].text == opener.text


async def test_a_node_runs_its_extension_tool_hooks_around_its_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_file_extension(
        monkeypatch,
        "acme_sign",
        "from chrys.extensions import Extension\nextension = Extension()\n\n"
        "@extension.before_tool\nasync def relative(call):\n    return {**call.arguments, 'path': 'input.txt'}\n\n"
        "@extension.after_tool\nasync def sign(call, result):\n    return f'{result.text} [acme read {call.arguments[\"path\"]}]'\n",
    )
    project = make_project(tmp_path)
    (project / "input.txt").write_text("Retained work", encoding="utf-8")
    client = ArchiveClient(project / "elsewhere.txt", "completed")
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client], builtin_tools=True)
    write_workflow(project, "archive", archive_source())
    profile = make_profile(builtins=["filesystem.read"])
    profile.extensions = ["acme_sign"]
    host = make_host(tmp_path, project=project, profiles=[profile], allow_user_interaction=True)
    try:
        await confirm(host, "archive")
        result, _events = await run(host, "archive", input_text="Read twice")
    finally:
        await host.shutdown()

    assert result.outcome.value == "completed"
    sent = [c.result for m in client.call_history[1][0] for c in m.contents if c.type == "function_result"]
    assert len(sent) == 1
    assert "Retained work" in sent[0]
    assert sent[0].endswith("[acme read input.txt]")


@pytest.mark.parametrize("enabled", [True, False], ids=["on", "off"])
async def test_project_skills_reach_every_agent_only_when_the_user_turns_them_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    main_client = MockChatClient(responses=[_delegate("inspect the tree"), MockResponse(text="chat done")])
    child_client = MockChatClient(responses=[MockResponse(text="child done")])
    node_client = MockChatClient(responses=[MockResponse(text="node done")])
    patch_runtime(monkeypatch, [main_client, node_client])
    _hand_out_child_client(monkeypatch, child_client)
    project = make_project(tmp_path)
    skill_dir = project / ".agents" / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: review\ndescription: Reviews a change.\n---\n", encoding="utf-8")
    profiles = [_delegating_profile(), _child_profile()]
    for profile in profiles:
        profile.skills = SkillsConfig(auto_load_user_agents_skills=False)
    write_workflow(project, "skilled", _WORKFLOW)
    host = make_host(
        tmp_path,
        project=project,
        profiles=profiles,
        settings=Settings(model_profile="mock-profile", project_skills_enabled=enabled),
    )
    try:
        await host.run_until_final("Go")
        await confirm(host, "skilled")
        result, _events = await run(host, "skilled", input_text="Go")
    finally:
        await host.shutdown()

    assert result.outcome.value == "completed"
    # The chat agent, its sub-agent and the workflow node each list the project's skill only when it is on.
    for client in (main_client, child_client, node_client):
        (messages, _options) = client.call_history[0]
        assert ("<name>review</name>" in (messages[-1].text or "")) is enabled


async def test_a_failed_node_child_ends_without_a_pause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No workflow surface shows a paused child card, so a failure ends the child at once and the node's
    model reads the error, as a headless chat run's would."""
    node_client = MockChatClient(responses=[_delegate("try hard"), MockResponse(text="node done")])
    child_client = ErrorMockChatClient(outcomes=[FrameworkBoom("boom")])
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client])
    _hand_out_child_client(monkeypatch, child_client)
    project = make_project(tmp_path)
    write_workflow(project, "failing", _WORKFLOW)
    host = make_host(tmp_path, project=project, profiles=[_delegating_profile(), _child_profile()])
    paused = await capture_events(host.event_bus, InvocationPaused)
    aborted = await capture_events(host.event_bus, InvocationAborted)
    tool_results = await capture_events(host.event_bus, InvocationToolCallResult)
    try:
        await confirm(host, "failing")
        result, _events = await run(host, "failing", input_text="Go")
        session_dir = host.workflow_session_dir
        assert session_dir is not None
    finally:
        await host.shutdown()

    assert result.outcome.value == "completed"
    assert child_client.call_count == 1
    assert paused == []
    (ended,) = aborted
    assert ended.origin.kind == "sub_agent" and ended.origin.root.kind == "workflow_node"
    assert "boom" in ended.last_error
    (delegated,) = [event for event in tool_results if event.origin.kind == "workflow_node"]
    assert delegated.result.startswith("Error: sub-agent 'Child' failed — ")
    assert "boom" in delegated.result
    # Nothing paused, so no control record awaits a chat restore that could never resume it.
    assert not pending_dir(session_dir).exists() or list(pending_dir(session_dir).iterdir()) == []


async def test_the_text_a_node_child_wrote_never_stops_its_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No workflow surface shows a node's sub-agent text: a stream dropped after a completed answer segment is
    sent again in place, as the chat child's would be had nothing shown."""
    use_fast_sub_agent_policy(monkeypatch)
    node_client = MockChatClient(responses=[_delegate("inspect the tree"), MockResponse(text="node done")])
    child_client = MockChatClient(
        responses=[
            MockResponse(text="Shown part.Cut off", chunk_size=11, chunk_delay=0),
            MockResponse(text="child retried answer"),
        ]
    )
    original = child_client._stream_updates

    async def cut(
        resp: MockResponse, model_id: str, finish_reason: FinishReasonLiteral | FinishReason
    ) -> AsyncIterator[ChatResponseUpdate]:
        first = child_client.call_count == 1
        async for update in original(resp, model_id, finish_reason):
            yield update
            if first and update.text:
                # Reasoning completes the first answer segment before the stream drops.
                yield ChatResponseUpdate(role="assistant", contents=[Content.from_text_reasoning(text="hmm")])
                raise ConnectionError("dropped")

    monkeypatch.setattr(child_client, "_stream_updates", cut)
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client])
    _hand_out_child_client(monkeypatch, child_client)
    project = make_project(tmp_path)
    write_workflow(project, "retried", _WORKFLOW)
    host = make_host(tmp_path, project=project, profiles=[_delegating_profile(), _child_profile()], stream=True)
    paused = await capture_events(host.event_bus, InvocationPaused)
    tool_results = await capture_events(host.event_bus, InvocationToolCallResult)
    try:
        await confirm(host, "retried")
        result, _events = await run(host, "retried", input_text="Go")
    finally:
        await host.shutdown()

    assert result.outcome.value == "completed"
    assert child_client.call_count == 2
    assert paused == []
    (delegated,) = [event for event in tool_results if event.origin.kind == "workflow_node"]
    assert delegated.result == "child retried answer"


async def test_a_mode_change_during_node_construction_reaches_its_sub_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Construction awaits registration, MCP and skills; a change arriving meanwhile finds no parts to
    apply to, so the node re-reads the launch policy once they exist."""
    node_client = MockChatClient(responses=[MockResponse(text="node done")])
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client])
    project = make_project(tmp_path)
    write_workflow(project, "modes", _WORKFLOW)
    host = make_host(tmp_path, project=project, profiles=[_delegating_profile(), _child_profile()])
    real_create_approval, real_build = agent_node_build_module.create_approval, agent_node_module.build_kernel_node
    built: list[KernelNodeParts] = []

    async def create_approval(owner: Conversation, inputs: ApprovalInputs) -> ApprovalMiddleware:
        # The sub-agent registry captured the launch mode already; the parts do not exist yet.
        await host.event_bus.publish(SetApprovalMode(mode="manual", persist=False), raise_handler_errors=True)
        return await real_create_approval(owner, inputs)

    async def build_kernel_node(
        conversation: Conversation,
        *,
        binding: Any,
        node_id: str,
        invocation_id: str,
        res: Any,
        archive: Any,
        callbacks: Any,
        stats: Any,
    ) -> KernelNodeParts:
        parts = await real_build(
            conversation,
            binding=binding,
            node_id=node_id,
            invocation_id=invocation_id,
            res=res,
            archive=archive,
            callbacks=callbacks,
            stats=stats,
        )
        built.append(parts)
        return parts

    monkeypatch.setattr(
        agent_node_build_module, "create_approval", create_autospec(real_create_approval, side_effect=create_approval)
    )
    monkeypatch.setattr(
        agent_node_module, "build_kernel_node", create_autospec(real_build, side_effect=build_kernel_node)
    )
    try:
        await confirm(host, "modes")
        result, _events = await run(host, "modes", input_text="Go")
    finally:
        await host.shutdown()

    assert result.outcome.value == "completed"
    assert host.engine.approval_mode is ApprovalMode.MANUAL
    (parts,) = built
    assert parts.approval.approval_mode is ApprovalMode.MANUAL
    assert parts.sub_agent_tools is not None
    assert parts.sub_agent_tools.approval_mode is ApprovalMode.MANUAL
