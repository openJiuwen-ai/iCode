# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Main/child recipes observed at real model requests, including wire retries."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import ApprovalRequest, ApprovalResponse, SetModelProfile, UserMessage
from chrys.kernel import AgentSession, Content, Message, SessionContext
from chrys.kernel.compaction import _token_count
from chrys.kernel.middleware import ChatMiddleware
from chrys.orchestration.engine.build import builder
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.invoker.resources import Conversation
from chrys.orchestration.invoker.runtime import (
    KernelRuntime,
    MainRecipe,
    SharedRuntime,
    create_runtime,
    create_validation,
)
from chrys.orchestration.sub_agents.kernel_policy import KernelSubAgentPolicy
from chrys.service.agent_middleware.injection import InjectionMiddleware
from chrys.service.agent_middleware.response_validation import ResponseValidationMiddleware
from chrys.service.agent_middleware.system_reminder import SystemReminderMiddleware
from chrys.service.context.middleware.usage import UsageTrackingMiddleware
from chrys.service.context.providers.history import CompressibleHistoryProvider
from chrys.service.llm.mock import MockResponse
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.vision import NonVisionImageStubMiddleware
from tests.orchestration.invoker._build_fixtures import build_recipe_engine
from tests.support.engines import AgentEngineFactory
from tests.support.waiting import await_run_task_chain


@pytest.mark.parametrize("stream", [False, True])
async def test_engine_model_switch_recounts_tool_result_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine: AgentEngineFactory, stream: bool
) -> None:
    engine, text_client, vision_client = await build_recipe_engine(
        agent_engine,
        monkeypatch,
        tmp_path,
        stream=stream,
        main=[MockResponse(text="text done"), MockResponse(text="text done again")],
        child=[MockResponse(text="vision done")],
    )
    assert engine.model_registry is not None
    engine.model_registry.register(
        ModelProfile(
            id="vision-model", name="Vision", provider="mock", model_id="vision-model", stream=stream, vision=True
        )
    )
    image = Content.from_data(b"image-bytes", "image/png", additional_properties={"width": 2048, "height": 2048})
    engine.current.loaded.bindings.backend.history_state["messages"] = [
        Message("user", ["inspect this tool output"]),
        Message("assistant", [Content.from_function_call("image_call", "image_tool")]),
        Message("tool", [Content.from_function_result("image_call", result=[image])]),
    ]
    estimates: list[int] = []
    try:
        for profile_id, client in (
            ("root-model", text_client),
            ("vision-model", vision_client),
            ("root-model", text_client),
        ):
            if estimates:
                await engine.event_bus.publish(SetModelProfile(profile_id=profile_id))
            await engine.event_bus.publish(UserMessage(text="continue"))
            await await_run_task_chain(engine, turn_state=engine.turns.turn_state, expect_installed=True)
            assert not engine.current.loaded.bindings.state.run_failed
            wire_image = next(
                message
                for message in client.call_history[-1][0]
                if any(
                    content.type == "function_result" and content.call_id == "image_call"
                    for content in message.contents
                )
            )
            estimate = _token_count(wire_image)
            assert estimate is not None
            if profile_id == "vision-model":
                assert estimate > 2000
                assert wire_image.contents[0].items[0].type == "data"
            else:
                assert estimate < 200
                assert wire_image.contents[0].items[0].type == "text"
            estimates.append(estimate)
        assert max(estimates[0], estimates[2]) < estimates[1] // 10
    finally:
        await engine.shutdown()


async def test_main_conversations_exclude_context_management_from_shared_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine: AgentEngineFactory
) -> None:
    captured: list[tuple[SharedRuntime, MainRecipe]] = []
    original_runtime = builder.create_runtime

    def capture(owner, shared, recipe, **kwargs):
        assert isinstance(recipe, MainRecipe)
        captured.append((shared, recipe))
        return original_runtime(owner, shared, recipe, **kwargs)

    monkeypatch.setattr(builder, "create_runtime", create_autospec(original_runtime, side_effect=capture))
    engine, _, _ = await build_recipe_engine(agent_engine, monkeypatch, tmp_path, main=[], child=[])
    assert len(captured) == 1
    shared, recipe = captured[0]

    async def open_main(owner: Conversation) -> KernelRuntime:
        return create_runtime(owner, shared, replace(recipe, validation=create_validation))

    try:
        first = await engine.current.loaded.prepared.open(open_main)
        second = await engine.current.loaded.prepared.open(open_main)
        assert first.injection is not second.injection
        assert first.validation is not second.validation
        assert first.context is not second.context
        assert first.reminder is not second.reminder
        first_provider = first.context.context_mgmt_provider
        second_provider = second.context.context_mgmt_provider
        assert first_provider is not None and second_provider is not None
        assert first_provider is not second_provider
        assert all(provider is not first_provider and provider is not second_provider for provider in shared.providers)
        sessions = [AgentSession(), AgentSession()]
        contexts = [SessionContext(input_messages=[]), SessionContext(input_messages=[])]
        for runtime, provider, session, context in zip(
            (first, second), (first_provider, second_provider), sessions, contexts, strict=True
        ):
            await provider.before_run(agent=runtime.agent, session=session, context=context, state={})
        # Invoke the first tool only after the second provider has bound its
        # session. A lookup creates history state in exactly the owning session.
        for index, context in enumerate(contexts):
            recall = next(tool for tool in context.tools if tool.name == "recall_context")
            result = await recall.func(compressed_context_id=f"session-{index}", question="sentinel")
            assert f"session-{index}" in result and "not found" in result
            assert CompressibleHistoryProvider.DEFAULT_SOURCE_ID in sessions[index].state
            if index == 0:
                assert CompressibleHistoryProvider.DEFAULT_SOURCE_ID not in sessions[1].state
        assert first_provider._session is sessions[0]
        assert second_provider._session is sessions[1]
        assert contexts[0].tools[0] is not contexts[1].tools[0]
    finally:
        await engine.shutdown()


def _record_chat_entries(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    entries: dict[str, list[str]] = {"ROOT": [], "CHILD": []}

    def wrap(cls: type[ChatMiddleware], label: str) -> None:
        original = cls.process

        async def process(self, context, call_next) -> None:
            kind = "ROOT" if "ROOT-INSTRUCTION" in context.options["instructions"] else "CHILD"
            entries[kind].append(label)
            await original(self, context, call_next)

        monkeypatch.setattr(cls, "process", create_autospec(original, side_effect=process))

    for cls, label in (
        (UsageTrackingMiddleware, "usage"),
        (InjectionMiddleware, "injection"),
        (NonVisionImageStubMiddleware, "image"),
        (SystemReminderMiddleware, "reminder"),
        (ResponseValidationMiddleware, "validation"),
    ):
        wrap(cls, label)
    return entries


@pytest.mark.parametrize("stream", [False, True])
async def test_real_build_preserves_main_child_model_context_approval_and_chat_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine: AgentEngineFactory, stream: bool
) -> None:
    monkeypatch.setenv("CHRYS_DOCS_ROOT", str(tmp_path / "missing-docs"))
    entries = _record_chat_entries(monkeypatch)
    engine, main, child = await build_recipe_engine(
        agent_engine,
        monkeypatch,
        tmp_path,
        stream=stream,
        main=[
            MockResponse(tool_calls=[("Explore", "parent-call", {"prompt": "child question"})]),
            MockResponse(text="root answer"),
        ],
        child=[
            MockResponse(tool_calls=[("read_file", "read-call", {"path": str(tmp_path / "ROOT.md")})]),
            MockResponse(text="child answer"),
        ],
    )
    approvals: list[ApprovalRequest] = []

    async def reject(event: ApprovalRequest) -> None:
        approvals.append(event)
        await engine.event_bus.publish(ApprovalResponse(request_id=event.request_id, approved=False))

    await engine.event_bus.subscribe(ApprovalRequest, reject)
    try:
        await engine.event_bus.publish(UserMessage(text="root question"))
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state, expect_installed=True)
        assert main.call_count == child.call_count == 2
        root_messages, root_options = main.call_history[0]
        child_messages, child_options = child.call_history[0]
        assert "ROOT-INSTRUCTION" in root_options["instructions"]
        # AIxCoding branding: prompt documentation title
        assert "AIxCoding documentation" in root_options["instructions"]
        assert "Product documentation could not be found" in root_options["instructions"]
        assert "ROOT-MEMORY-SENTINEL" in root_options["instructions"]
        assert child_options["instructions"].startswith("CHILD-INSTRUCTION")
        assert "AIxCoding documentation" not in child_options["instructions"]
        assert "ROOT-MEMORY-SENTINEL" not in child_options["instructions"]
        root_names = {tool.name for tool in root_options["tools"]}
        child_names = {tool.name for tool in child_options["tools"]}
        assert {"Explore", "compress_context", "recall_context", "list_compressed_contexts"} <= root_names
        assert engine.current.manifest.runtime_details.context_tools == [
            "compress_context",
            "recall_context",
            "list_compressed_contexts",
        ]
        assert "read_file" in child_names
        assert not {"Explore", "compress_context", "recall_context", "view_image"} & child_names
        assert root_messages[-1].text.startswith("root question <system-reminder>")
        assert child_messages[-1].text.startswith("child question <system-reminder>")
        assert str(tmp_path) in root_messages[-1].text
        assert str(tmp_path) in child_messages[-1].text
        assert len(approvals) == 1
        assert approvals[0].caller_name == "Explore"
        denied = [
            content.result
            for message in child.call_history[1][0]
            for content in message.contents
            if content.type == "function_result"
        ]
        assert denied == ["Error: Tool execution was rejected by user."]
        # Usage is innermost, so it counts the response validation then rejects.
        assert entries["ROOT"] == ["injection", "image", "reminder", "validation", "usage"] * 2
        assert entries["CHILD"] == ["image", "reminder", "validation", "usage"] * 2
        assert main.enters == child.enters == 1
        assert main.exits == child.exits == 0
    finally:
        await engine.event_bus.unsubscribe(ApprovalRequest, reject)
        await engine.shutdown()
    assert main.exits == child.exits == 1


@pytest.mark.parametrize("stream", [False, True])
async def test_real_wire_retries_fire_each_shells_pass_start_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine: AgentEngineFactory, stream: bool
) -> None:
    starts: list[tuple[ResponseValidationMiddleware, int, int]] = []
    clients = []
    original = ResponseValidationMiddleware.begin_pass_hosted_baseline

    def begin(self: ResponseValidationMiddleware, *, resumes_background_response: bool) -> None:
        starts.append((self, clients[0].call_count, clients[1].call_count))
        original(self, resumes_background_response=resumes_background_response)

    monkeypatch.setattr(
        ResponseValidationMiddleware, "begin_pass_hosted_baseline", create_autospec(original, side_effect=begin)
    )
    monkeypatch.setattr(TurnBindings, "_BACKOFF_SCHEDULE", (0,))
    original_init = KernelSubAgentPolicy.__init__

    def init(*args, **kwargs) -> None:
        kwargs["backoff_schedule"] = (0,)
        original_init(*args, **kwargs)

    monkeypatch.setattr(KernelSubAgentPolicy, "__init__", create_autospec(original_init, side_effect=init))
    engine, main, child = await build_recipe_engine(
        agent_engine,
        monkeypatch,
        tmp_path,
        stream=stream,
        main=[
            ConnectionError("root transport"),
            MockResponse(tool_calls=[("Explore", "c1", {"prompt": "child question"})]),
            MockResponse(text="root done"),
        ],
        child=[ConnectionError("child transport"), MockResponse(text="child done")],
    )
    clients.extend([main, child])
    try:
        await engine.event_bus.publish(UserMessage(text="root question"))
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state, expect_installed=True)
        assert not engine.current.loaded.bindings.state.run_failed
        assert main.call_count == 3
        assert child.call_count == 2
        root_validation = engine.current.loaded.bindings._response_validation
        # A wire retry stays inside its pass: each shell's pass starts once.
        assert [
            ("root" if owner is root_validation else "child", root_calls, child_calls)
            for owner, root_calls, child_calls in starts
        ] == [("root", 0, 0), ("child", 2, 0)]
        assert all("root question" in messages[-1].text for messages, _ in main.call_history[:2])
        assert all("child question" in messages[-1].text for messages, _ in child.call_history)
        assert not engine.current.loaded.sub_agent_tools._controllers
    finally:
        await engine.shutdown()


async def test_real_same_tool_conversations_isolate_state_and_keep_shared_client_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine: AgentEngineFactory
) -> None:
    import asyncio

    from chrys.orchestration.invoker.runtime import ChildRecipe
    from chrys.orchestration.sub_agents import tools as child_tools

    runtimes = []
    original_runtime = child_tools.create_runtime

    def create_runtime(*args, **kwargs):
        runtime = original_runtime(*args, **kwargs)
        if isinstance(args[2], ChildRecipe) and not args[2].registration:
            runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(child_tools, "create_runtime", create_autospec(original_runtime, side_effect=create_runtime))
    engine, main, child = await build_recipe_engine(
        agent_engine, monkeypatch, tmp_path, main=[], child=[MockResponse(text="done"), MockResponse(text="done")]
    )
    both_entered = asyncio.Event()
    arrivals = 0
    original_response = child._inner_get_response

    def response(*args, **kwargs):
        async def complete():
            nonlocal arrivals
            arrivals += 1
            if arrivals == 2:
                both_entered.set()
            await asyncio.wait_for(both_entered.wait(), 5)
            return await original_response(*args, **kwargs)

        return complete()

    monkeypatch.setattr(child, "_inner_get_response", create_autospec(original_response, side_effect=response))
    tools = engine.current.loaded.sub_agent_tools
    try:
        assert await asyncio.gather(
            tools.get_tools()[0].func(prompt="FIRST-SENTINEL"),
            tools.get_tools()[0].func(prompt="SECOND-SENTINEL"),
        ) == ["done", "done"]
        assert len(runtimes) == 2
        first, second = runtimes
        assert first.owner is not second.owner
        assert first.context is not second.context
        assert first.context.compaction_strategy is not second.context.compaction_strategy
        assert first.context.history_provider is not second.context.history_provider
        assert first.reminder is not second.reminder
        assert first.agent.client is second.agent.client is child
        assert first.context.context_mgmt_provider is second.context.context_mgmt_provider is None
        questions = [messages[-1].text.split(" <system-reminder>", 1)[0] for messages, _ in child.call_history]
        assert sorted(questions) == ["FIRST-SENTINEL", "SECOND-SENTINEL"]
        assert all(len(messages) == 1 for messages, _ in child.call_history)
        assert child.enters == 1 and child.exits == 0
        assert main.call_count == 0
        assert all(runtime.owner.closing for runtime in runtimes)
    finally:
        await engine.shutdown()
    assert child.exits == main.exits == 1
