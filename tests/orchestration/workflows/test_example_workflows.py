# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Example graphs on real workers and builtin profiles, with mock model responses."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chrys.foundation.events.types import InvocationStarted, WorkflowLoopIteration, WorkflowNodeStateChanged
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_profile,
    make_workdir,
    of_type,
    patch_runtime,
    run,
    write_workflow,
)

FIXTURES = Path(__file__).resolve().parents[2] / "service/workflows/fixtures"


def _profiles() -> list[AgentProfile]:
    registry = AgentProfileRegistry()
    registry.load_builtins()
    return [make_profile(), *registry.list_profiles(include_sub_agent_only=True)]


async def test_research_fans_out_before_synthesizing_both_named_perspectives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = ("First research result", "Second research result")
    clients = [MockChatClient(responses=[MockResponse(text=text)]) for text in responses]
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), *clients])
    project = make_workdir(tmp_path)
    write_workflow(project, "research_synthesis", (FIXTURES / "research_synthesis.py").read_bytes())
    host = make_host(tmp_path, project=project, profiles=_profiles())
    started: set[str] = set()
    both = asyncio.Event()

    async def barrier(event: InvocationStarted) -> None:
        if event.origin.kind == "workflow_node":
            started.add(event.tool_name)
            if len(started) == 2:
                both.set()
            await both.wait()  # Neither research agent can finish before the other starts.

    await host.event_bus.subscribe(InvocationStarted, barrier)
    try:
        await confirm(host, "research_synthesis")
        result, events = await run(host, "research_synthesis", input_text="Is this design viable?")
        assert started == {"evidence", "counterarguments"}
        assert result.outcome.value == "completed"
        (output,) = result.outputs
        assert output.node_id == "synthesis"
        # Parallel input writes may finish in either order, so client allocation is not node identity.
        contributions = {}
        for client, response in zip(clients, responses, strict=True):
            prompt = "\n".join(message.text for message in client.call_history[0][0])
            assert "Is this design viable?" in prompt
            instructions = client.call_history[0][1]["instructions"]
            if "Find supporting evidence and cite your sources." in instructions:
                perspective = "evidence"
            else:
                assert "Research counterarguments, uncertainties and missing evidence." in instructions
                perspective = "counterarguments"
            assert perspective not in contributions
            contributions[perspective] = response
        assert output.value.data == [
            {"perspective": "evidence", "text": contributions["evidence"]},
            {"perspective": "counterarguments", "text": contributions["counterarguments"]},
        ]
        assert output.value.text == (
            f"# Research synthesis\n\n## evidence\n{contributions['evidence']}"
            f"\n\n## counterarguments\n{contributions['counterarguments']}"
        )
        states = [(e.node_id, e.state) for e in of_type(events, WorkflowNodeStateChanged)]
        assert states.index(("synthesis", "running")) > states.index(("counterarguments", "completed"))
        assert states.index(("synthesis", "running")) > states.index(("evidence", "completed"))
    finally:
        await host.event_bus.unsubscribe(InvocationStarted, barrier)
        await host.shutdown()


@pytest.mark.parametrize("ready", [True, False])
async def test_release_switch_runs_only_its_selected_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ready: bool
) -> None:
    assessment = "Checks complete.\nREADY: YES" if ready else "Fix compatibility.\nREADY: NO"
    assess = MockChatClient(responses=[MockResponse(text=assessment)])
    plan = MockChatClient(responses=[MockResponse(text="Remediation: fix compatibility first.")])
    clients = [MockChatClient(responses=[]), assess, *([] if ready else [plan])]
    patch_runtime(monkeypatch, clients)
    project = make_workdir(tmp_path)
    write_workflow(project, "release_readiness", (FIXTURES / "release_readiness.py").read_bytes())
    host = make_host(tmp_path, project=project, profiles=_profiles())
    try:
        await confirm(host, "release_readiness")
        result, events = await run(host, "release_readiness", input_text="release 2.0")
        assert result.outcome.value == "completed"
        assert [(o.node_id, o.value.text) for o in result.outputs] == [
            ("release_report", "# Release readiness\n\n" + assessment)
            if ready
            else ("remediation", "Remediation: fix compatibility first.")
        ]
        assert any("release 2.0" in message.text for message in assess.call_history[0][0])
        skipped = [e.node_id for e in of_type(events, WorkflowNodeStateChanged) if e.state == "skipped"]
        assert skipped == ["remediation" if ready else "release_report"]
        if not ready:
            assert any(assessment in message.text for message in plan.call_history[0][0])
        assert not clients
    finally:
        await host.shutdown()


@pytest.mark.parametrize("passes", [True, False], ids=["passes-second-check", "exhausts"])
async def test_refactor_uses_tools_and_carries_request_and_feedback_between_iterations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, passes: bool
) -> None:
    iterations = 2 if passes else 3
    project = make_workdir(tmp_path)
    write_workflow(project, "code_refactor", (FIXTURES / "code_refactor.py").read_bytes())
    target = project / "refactor.txt"
    editors = [
        MockChatClient(
            responses=[
                *(
                    [MockResponse(tool_calls=[("read_file", f"read-{index}", {"path": str(target)})])]
                    if index > 1
                    else []
                ),
                MockResponse(
                    tool_calls=[
                        (
                            "write_file",
                            f"write-{index}",
                            {"path": str(target), "content": f"edit {index}", "overwrite": index > 1},
                        )
                    ]
                ),
                MockResponse(text=f"Refactored iteration {index}; please check the diff."),
            ]
        )
        for index in range(1, iterations + 1)
    ]
    checks = [
        MockChatClient(
            responses=[
                MockResponse(
                    text="Verified.\nCHECK: PASS"
                    if passes and index == iterations
                    else f"Fix issue {index}.\nCHECK: FAIL"
                )
            ]
        )
        for index in range(1, iterations + 1)
    ]
    clients = [MockChatClient(responses=[]), *[client for pair in zip(editors, checks, strict=True) for client in pair]]
    patch_runtime(monkeypatch, clients, builtin_tools=True)
    host = make_host(tmp_path, project=project, profiles=_profiles())
    try:
        await confirm(host, "code_refactor")
        result, events = await run(host, "code_refactor", input_text="Extract the parser")
        assert result.outcome.value == ("completed" if passes else "loop_exhausted")
        assert target.read_text() == f"edit {iterations}"
        assert [(e.iteration, e.verdict) for e in of_type(events, WorkflowLoopIteration)] == [
            *[(index, "continue") for index in range(1, iterations)],
            (iterations, "exit" if passes else "exhausted"),
        ]
        for index, editor in enumerate(editors):
            prompt = "\n".join(message.text for message in editor.call_history[0][0])
            assert "Extract the parser" in prompt
            if index:
                assert f"Fix issue {index}." in prompt
            assert any(f"Refactored iteration {index + 1}" in m.text for m in checks[index].call_history[0][0])
        if passes:
            assert result.outputs[0].value.data == {"request": "Extract the parser", "iteration": 2}
            assert result.outputs[0].value.text == "Verified.\nCHECK: PASS"
        else:
            assert result.node_id == "refine" and not result.outputs
        assert not clients
    finally:
        await host.shutdown()
