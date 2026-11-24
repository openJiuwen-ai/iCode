# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A workflow node's tool call goes through the same hook check: asked where someone can answer, refused where not.

Written from the plan (``.plan/hook-ask-confirm.md`` §5): workflow managers are
built by the engine's one factory and ask through the engine's one service; a
headless run asks nobody.
"""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

import chrys.orchestration.sub_agents.tools as sub_agent_module
from chrys.foundation.events.types import InvocationToolCallResult, WorkflowCancelRequest, WorkflowRunAccepted
from chrys.foundation.platform import get_platform
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import AgentProfile, SubAgentRef, SubAgentsConfig
from tests.orchestration.workflows._hosting import (
    PROFILE,
    confirm,
    make_host,
    make_profile,
    make_workdir,
    patch_runtime,
    run,
    write_workflow,
)
from tests.support.event_capture import capture_events
from tests.support.hook_confirmations import FakeConfirmationFrontend, confirmations_settled
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, ENGINE_TURN_TIMEOUT, wait_for, with_wait_deadline

_WORKFLOW = (
    "from chrys.workflows import WorkflowBuilder\nwf = WorkflowBuilder('agent')\n"
    f"node = wf.agent('node', profile={PROFILE!r})\nwf.start(node)\nwf.output(node)\nworkflow = wf.build()\n"
).encode()


def _profiles() -> list[AgentProfile]:
    node = make_profile()
    node.sub_agents = SubAgentsConfig(max_total_concurrency=1, agents=[SubAgentRef(profile="Child")])
    child = make_profile("Child")
    child.description = "Inspects on the node's behalf."
    child.sub_agent_only = True
    return [node, child]


def _write_ask_hook() -> None:
    script = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "ask", "reason": "delegating"}, f)
        """
    )
    hooks_dir = get_platform().config_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 1,
        "hooks": [
            {
                "id": "delegate-guard",
                "event": "before_tool_call",
                "match": {"tool_name": "Child"},
                "run": {"type": "command", "argv": [sys.executable, "-c", script]},
                "execution": {"mode": "blocking", "timeout_seconds": 30},
            }
        ],
    }
    (hooks_dir / "hooks.json").write_text(json.dumps(document), encoding="utf-8")


def _hand_out_child_client(monkeypatch: pytest.MonkeyPatch, client: MockChatClient) -> None:
    async def create_client(_model_profile: Any, **_kwargs: Any) -> MockChatClient:
        return client

    monkeypatch.setattr(sub_agent_module, "create_client", create_client)


@pytest.mark.parametrize("interactive", [False, True])
async def test_a_node_s_tool_call_asks_where_someone_can_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interactive: bool
) -> None:
    _write_ask_hook()
    node_client = MockChatClient(
        responses=[MockResponse(tool_calls=[("Child", "call-1", {"prompt": "look"})]), MockResponse(text="node done")]
    )
    child_client = MockChatClient(responses=[MockResponse(text="looked")])
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client])
    _hand_out_child_client(monkeypatch, child_client)
    project = make_workdir(tmp_path)
    write_workflow(project, "delegate", _WORKFLOW)
    host = make_host(tmp_path, project=project, profiles=_profiles(), allow_user_interaction=interactive)
    frontend = FakeConfirmationFrontend(host.event_bus, session_id=None, answer_inline="approved")
    await frontend.attach()
    results = await capture_events(host.event_bus, InvocationToolCallResult)
    try:
        await confirm(host, "delegate")
        result, _events = await run(host, "delegate", input_text="Go")
    finally:
        await host.shutdown()

    assert result.outcome.value == "completed"
    (delegated,) = [event for event in results if event.tool_name == "Child"]
    if interactive:
        (request,) = frontend.requests
        assert request.origin is not None and request.origin.kind == "workflow_node"
        assert child_client.call_count == 1
    else:
        assert frontend.requests == []
        assert child_client.call_count == 0
        assert "no one can confirm it in this session" in str(delegated.result)


def _write_read_ask_hook() -> None:
    script = textwrap.dedent(
        """
        import json, os
        with open(os.environ["CHRYS_HOOK_RESULT"], "w") as f:
            json.dump({"action": "ask", "reason": "reading"}, f)
        """
    )
    hooks_dir = get_platform().config_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    document = {
        "version": 1,
        "hooks": [
            {
                "id": "read-guard",
                "event": "before_tool_call",
                "match": {"tool_name": "read_file"},
                "run": {"type": "command", "argv": [sys.executable, "-c", script]},
                "execution": {"mode": "blocking", "timeout_seconds": 30},
            }
        ],
    }
    (hooks_dir / "hooks.json").write_text(json.dumps(document), encoding="utf-8")


@pytest.mark.parametrize("interactive", [False, True])
async def test_a_tool_call_of_a_node_s_sub_agent_asks_the_same_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interactive: bool
) -> None:
    _write_read_ask_hook()
    project = make_workdir(tmp_path)
    notes = project / "notes.txt"
    notes.write_text("the build is green", encoding="utf-8")
    node_client = MockChatClient(
        responses=[MockResponse(tool_calls=[("Child", "call-1", {"prompt": "read"})]), MockResponse(text="node done")]
    )
    child_client = MockChatClient(
        responses=[MockResponse(tool_calls=[("read_file", "read-1", {"path": str(notes)})]), MockResponse(text="read")]
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client], builtin_tools=True)
    _hand_out_child_client(monkeypatch, child_client)
    write_workflow(project, "delegate", _WORKFLOW)
    node, child = _profiles()
    child.tools = make_profile("Child", builtins=["filesystem.read"]).tools
    host = make_host(tmp_path, project=project, profiles=[node, child], allow_user_interaction=interactive)
    frontend = FakeConfirmationFrontend(host.event_bus, session_id=None, answer_inline="approved")
    await frontend.attach()
    results = await capture_events(host.event_bus, InvocationToolCallResult)
    try:
        await confirm(host, "delegate")
        result, _events = await run(host, "delegate", input_text="Go")
    finally:
        await host.shutdown()

    assert result.outcome.value == "completed"
    (read,) = [event for event in results if event.tool_name == "read_file"]
    if interactive:
        (request,) = frontend.requests
        assert request.tool_name == "read_file"
        assert request.origin is not None and request.origin.kind == "sub_agent"
        assert request.origin.parent is not None and request.origin.parent.kind == "workflow_node"
        assert "the build is green" in str(read.result)
    else:
        assert frontend.requests == []
        assert "no one can confirm it in this session" in str(read.result)


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_cancelling_a_run_withdraws_a_node_s_question_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_ask_hook()
    node_client = MockChatClient(
        responses=[MockResponse(tool_calls=[("Child", "call-1", {"prompt": "look"})]), MockResponse(text="node done")]
    )
    child_client = MockChatClient(responses=[MockResponse(text="looked")])
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client])
    _hand_out_child_client(monkeypatch, child_client)
    project = make_workdir(tmp_path)
    write_workflow(project, "delegate", _WORKFLOW)
    host = make_host(tmp_path, project=project, profiles=_profiles(), allow_user_interaction=True)
    # Claims the question and never answers it.
    frontend = FakeConfirmationFrontend(host.event_bus, session_id=None)
    await frontend.attach()
    accepted = await capture_events(host.event_bus, WorkflowRunAccepted)
    task = None
    try:
        await confirm(host, "delegate")
        task = asyncio.create_task(run(host, "delegate", input_text="Go"))
        await wait_for(lambda: frontend.claimed, timeout=ENGINE_TURN_TIMEOUT, description="the node's question")
        (started,) = accepted
        await host.event_bus.publish(WorkflowCancelRequest(run_id=started.run_id, session_id=host.workflow_session_id))
        await wait_for(task.done, timeout=ENGINE_TURN_TIMEOUT, description="the cancelled run")
        result, _events = task.result()
        await wait_for(lambda: frontend.withdrawn, description="the question withdrawn")

        assert result.outcome.value == "cancelled"
        assert [w.request_id for w in frontend.withdrawn] == frontend.claimed
        assert child_client.call_count == 0
        confirmations = host.engine._hook_confirmations
        assert confirmations is not None
        await wait_for(lambda: confirmations_settled(confirmations), description="the question wound down")
    finally:
        await host.shutdown()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
