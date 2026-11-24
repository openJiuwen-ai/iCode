# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A workflow agent node's LLM client closes with its attempt; cached judges close with the run's session."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec

import pytest

import chrys.service.llm.clients as clients_module
from chrys.orchestration.workflows.session import WorkflowSessionOwner
from chrys.service.approval.judge import ApprovalJudge
from chrys.service.llm.mock import MockChatClient, MockResponse
from tests.orchestration.workflows._hosting import (
    PROFILE,
    confirm,
    make_host,
    make_workdir,
    patch_runtime,
    run,
    write_workflow,
)
from tests.support.scripted_clients import ErrorMockChatClient, FrameworkBoom
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.service.profiles.models.schema import ModelProfile

_WORKFLOW = (
    "from chrys.workflows import WorkflowBuilder\nwf = WorkflowBuilder('agent')\n"
    f"node = wf.agent('node', profile={PROFILE!r})\nwf.start(node)\nwf.output(node)\nworkflow = wf.build()\n"
).encode()


class _OwnedClient(ErrorMockChatClient):
    """A scripted client that counts closes and can hold its first call until released."""

    def __init__(self, outcomes: list[MockResponse | BaseException], *, hold: bool = False) -> None:
        super().__init__(outcomes)
        self.closes = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self._hold = hold

    async def aclose(self) -> None:
        self.closes += 1

    def _inner_get_response(self, *, messages, stream, options, **kwargs):  # type: ignore[override]
        if not self._hold:
            return super()._inner_get_response(messages=messages, stream=stream, options=options, **kwargs)
        self.entered.set()
        respond = super()._inner_get_response

        async def _held() -> Any:
            await self.release.wait()
            return await respond(messages=messages, stream=stream, options=options, **kwargs)

        return _held()


@pytest.mark.parametrize(
    ("scenario", "outcome"), [("done", "completed"), ("error", "node_failed"), ("cancel", "cancelled")]
)
async def test_node_attempt_closes_its_client(
    scenario: str, outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script: list[MockResponse | BaseException] = [
        FrameworkBoom("node call failed") if scenario == "error" else MockResponse(text="done")
    ]
    node_client = _OwnedClient(script, hold=scenario == "cancel")
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client])
    project = make_workdir(tmp_path)
    write_workflow(project, "agent", _WORKFLOW)
    host = make_host(tmp_path, project=project)
    running: asyncio.Task[Any] | None = None
    try:
        await confirm(host, "agent")
        running = asyncio.create_task(run(host, "agent"))
        if scenario == "cancel":
            await wait_for(
                lambda: node_client.entered.is_set() or running.done(),
                timeout=ENGINE_TURN_TIMEOUT,
                description="node call in flight",
            )
            assert not running.done()
            assert node_client.closes == 0
            await host.cancel_workflow()
        result, _events = await asyncio.wait_for(running, ENGINE_TURN_TIMEOUT)
        assert result.outcome.value == outcome
        assert node_client.closes == 1
    finally:
        node_client.release.set()
        await host.shutdown()
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)


async def test_workflow_session_close_closes_cached_judges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node_client = _OwnedClient([MockResponse(text="done")], hold=True)
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client])
    judge_client = _OwnedClient([])
    monkeypatch.setattr(
        clients_module, "create_client", create_autospec(clients_module.create_client, return_value=judge_client)
    )
    judges: list[ApprovalJudge] = []
    judge_for = WorkflowSessionOwner.judge_for

    def record_judge(owner: WorkflowSessionOwner, node_model: ModelProfile | None) -> ApprovalJudge | None:
        judge = judge_for(owner, node_model)
        if judge is not None:
            judges.append(judge)
        return judge

    monkeypatch.setattr(WorkflowSessionOwner, "judge_for", create_autospec(judge_for, side_effect=record_judge))
    project = make_workdir(tmp_path)
    write_workflow(project, "agent", _WORKFLOW)
    host = make_host(tmp_path, project=project)
    running: asyncio.Task[Any] | None = None
    try:
        await confirm(host, "agent")
        running = asyncio.create_task(run(host, "agent"))
        await wait_for(
            lambda: node_client.entered.is_set() or running.done(),
            timeout=ENGINE_TURN_TIMEOUT,
            description="node call in flight",
        )
        assert not running.done()
        [judge] = judges
        assert await judge._get_client() is judge_client
        node_client.release.set()

        result, _events = await asyncio.wait_for(running, ENGINE_TURN_TIMEOUT)
        assert result.outcome.value == "completed"
        assert judge_client.closes == 1
    finally:
        node_client.release.set()
        await host.shutdown()
        if running is not None:
            await asyncio.gather(running, return_exceptions=True)
