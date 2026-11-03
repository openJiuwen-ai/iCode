# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Product documentation routing and on-demand reads through real agent builds."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Annotated
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import ApprovalRequest, ApprovalResponse, UserMessage
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_READ, set_tool_kind
from chrys.kernel import FunctionTool
from chrys.orchestration.engine.build import builder
from chrys.orchestration.invoker.runtime import create_runtime
from chrys.service.llm.mock import MockResponse
from tests.orchestration.invoker._build_fixtures import build_recipe_engine
from tests.support.engines import AgentEngineFactory
from tests.support.waiting import await_run_task_chain


@pytest.fixture
def documentation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    docs = tmp_path / "installed docs"
    page = docs / "en" / "compression.md"
    page.parent.mkdir(parents=True)
    index = docs / "index.yaml"
    index.write_text("locales: [en]\ntopics:\n  - path: compression.md\n", encoding="utf-8")
    page.write_text("# Compression\nPRODUCT-DOCUMENT-SENTINEL\n", encoding="utf-8")
    monkeypatch.setenv("CHRYS_DOCS_ROOT", str(docs))
    return index, page


@pytest.mark.parametrize("stream", [False, True])
async def test_custom_agents_receive_routing_and_read_docs_only_on_demand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine: AgentEngineFactory,
    documentation: tuple[Path, Path],
    stream: bool,
) -> None:
    index, page = documentation
    engine, main, child = await build_recipe_engine(
        agent_engine,
        monkeypatch,
        tmp_path,
        stream=stream,
        main=[
            MockResponse(
                tool_calls=[
                    (
                        "Explore",
                        "delegate",
                        {"prompt": f"Read the iCode compression docs using the topic index at {index}"},
                    )
                ]
            ),
            MockResponse(text="Documented behavior"),
        ],
        child=[
            MockResponse(tool_calls=[("read_file", "index", {"path": str(index)})]),
            MockResponse(tool_calls=[("read_file", "page", {"path": str(page)})]),
            MockResponse(text="Documented behavior"),
        ],
    )

    # Children inherit the parent's read_file=require override in this fixture.
    approvals: list[ApprovalRequest] = []

    async def approve(event: ApprovalRequest) -> None:
        approvals.append(event)
        await engine.event_bus.publish(ApprovalResponse(request_id=event.request_id, approved=True))

    await engine.event_bus.subscribe(ApprovalRequest, approve)
    try:
        await engine.event_bus.publish(UserMessage(text="How does iCode compression work?"))
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state, expect_installed=True)
        assert not engine.current.require_loaded().bindings.state.run_failed
        assert main.call_count == 2 and child.call_count == 3
        assert [(event.caller_name, event.tool_name) for event in approvals] == [("Explore", "read_file")] * 2
        assert "no read_file tool" in main.call_history[0][1]["instructions"]
        messages, options = child.call_history[0]
        assert "CHILD-INSTRUCTION" in options["instructions"]
        # AIxCoding branding: prompt documentation title absent
        assert "AIxCoding documentation" not in options["instructions"]
        assert str(index) in messages[-1].text
        assert str(index) in main.call_history[0][1]["instructions"]
        assert "delegate the lookup" in main.call_history[0][1]["instructions"]
        assert "PRODUCT-DOCUMENT-SENTINEL" not in options["instructions"]
        assert all("PRODUCT-DOCUMENT-SENTINEL" not in message.text for message in messages)
        results = [
            content.result
            for message in child.call_history[2][0]
            for content in message.contents
            if content.type == "function_result" and content.call_id == "page"
        ]
        assert len(results) == 1 and "PRODUCT-DOCUMENT-SENTINEL" in results[0]
        assert child.call_history[0][1]["instructions"] == child.call_history[2][1]["instructions"]
    finally:
        await engine.event_bus.unsubscribe(ApprovalRequest, approve)
        await engine.shutdown()


@pytest.mark.parametrize(
    ("name", "kind", "read_available", "expected"),
    [
        ("read_file", KIND_FILESYSTEM_READ, True, True),
        ("read_file", "sub_agent", True, False),
        ("read_file", None, True, False),
        ("custom_reader", KIND_FILESYSTEM_READ, True, False),
        ("read_file", KIND_FILESYSTEM_READ, False, False),
    ],
)
async def test_main_runtime_requires_the_real_read_file_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine: AgentEngineFactory,
    documentation: tuple[Path, Path],
    name: str,
    kind: str | None,
    read_available: bool,
    expected: bool,
) -> None:
    async def lookup(prompt: Annotated[str, "Delegated question"]) -> str:
        return prompt

    tool = FunctionTool(name=name, func=lookup)
    if kind is not None:
        set_tool_kind(tool, kind)

    def build(owner, shared, recipe, *, context=None, injection=None):
        return create_runtime(
            owner,
            replace(shared, tools=[tool]),
            replace(recipe, reminder={**recipe.reminder, "file_read_available": read_available}),
            context=context,
            injection=injection,
        )

    monkeypatch.setattr(builder, "create_runtime", create_autospec(create_runtime, side_effect=build))
    engine, main, _child = await build_recipe_engine(
        agent_engine, monkeypatch, tmp_path, main=[MockResponse(text="done")], child=[]
    )
    try:
        await engine.event_bus.publish(UserMessage(text="What is iCode?"))
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state, expect_installed=True)
        assert not engine.current.require_loaded().bindings.state.run_failed
        instructions = main.call_history[0][1]["instructions"]
        assert str(documentation[0]) in instructions
        assert ("no read_file tool" not in instructions) is expected
    finally:
        await engine.shutdown()
