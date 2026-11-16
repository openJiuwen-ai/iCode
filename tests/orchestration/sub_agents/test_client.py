# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for sub-agent LLM client construction."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.kernel import StallExhaustedAction
from chrys.orchestration.sub_agents.tools import SubAgentTools
from chrys.service.llm.route_sessions import derive_llm_route_session_id, llm_parent_session_id, llm_route_session_id
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.profiles.agents.schema import AgentProfile, MCPServerConfig, ModelConfig, SubAgentRef, ToolsConfig
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.orchestration.invoker._runtime_fixtures import SubAgentPolicyDouble
from tests.support.close_races import ReleaseGate, assert_cancel_during_rollback


async def test_register_propagates_cancel_received_during_rollback() -> None:
    release = ReleaseGate()
    tools = SubAgentTools(session_id="register-rollback")
    agent = MagicMock()
    agent.__aenter__ = AsyncMock(side_effect=ValueError("Agent enter failed"))
    profile = AgentProfile(
        name="Explore",
        tools=ToolsConfig(builtins=[], mcp=[MCPServerConfig(name="srv", transport="stdio", command="unused")]),
    )
    try:
        with (
            patch("chrys.orchestration.invoker.runtime.Agent", return_value=agent),
            patch("chrys.orchestration.sub_agents.tools.create_client", return_value=MagicMock(aclose=AsyncMock())),
            patch.object(MCPAdapter, "connect_all", new=AsyncMock(return_value=[])),
            patch.object(MCPAdapter, "disconnect_all", new=AsyncMock(side_effect=release.__call__)) as disconnect,
        ):
            registering = asyncio.create_task(
                tools.register(
                    SubAgentRef(profile="Explore", tool_name="Explore"),
                    profile,
                    SessionEnvironment.capture(session_id="register-rollback"),
                    settings=Settings(),
                    fallback_profile=ModelProfile(id="mock", name="mock", provider="mock", model_id="mock"),
                )
            )
            await assert_cancel_during_rollback(registering, release)
            disconnect.assert_awaited_once()
            assert tools._prepared_by_tool == {}
    finally:
        await tools.cleanup()


async def test_sub_agent_register_passes_child_and_parent_session_ids_to_client() -> None:
    """Sub-agent LLM requests should use a child session and expose the parent."""
    profile = AgentProfile(
        name="Explore",
        instructions="Investigate.",
        tools=ToolsConfig(builtins=[]),
    )
    fallback_profile = ModelProfile(
        id="parent-model",
        name="parent",
        provider="mock",
        model_id="mock",
    )
    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()

    side_usage_sink = MagicMock()
    tools = SubAgentTools(session_id="sess-123", on_side_call_usage=side_usage_sink)
    try:
        with (
            patch("chrys.orchestration.invoker.runtime.Agent", return_value=agent_mock),
            patch(
                "chrys.orchestration.sub_agents.tools.create_client", return_value=MagicMock(aclose=AsyncMock())
            ) as create_client,
        ):
            await tools.register(
                SubAgentRef(profile="Explore", tool_name="explore"),
                profile,
                SessionEnvironment.capture(session_id="sess-123"),
                settings=Settings(),
                fallback_profile=fallback_profile,
            )

        create_client.assert_called_once()
        child_session_id = create_client.call_args.kwargs["session_id"]
        assert child_session_id
        assert child_session_id != "sess-123"
        assert create_client.call_args.kwargs["parent_session_id"] == "sess-123"
        assert create_client.call_args.kwargs["use_route_session_context"] is True
        generator = tools._context_managers["explore"].compaction_strategy._last_words_generator
        expected_last_words_session_id = derive_llm_route_session_id(
            "sess-123",
            route_kind="last-words",
            route_parts=("explore", "Explore"),
            model_profile=fallback_profile,
        )
        assert generator._session_id == expected_last_words_session_id
        assert generator._session_id != child_session_id
        assert generator._parent_session_id == "sess-123"
        # Sub-agent Phase-4 side calls report spend through the same sink.
        assert generator._report_usage is side_usage_sink
    finally:
        await tools.cleanup()


@pytest.mark.parametrize(
    ("sub_provider", "sub_chat_options"),
    [
        ("openai", ""),
        ("deepseek-openai", '{"store": true}'),
    ],
)
async def test_sub_agent_run_options_come_from_sub_agent_model_profile(
    sub_provider: str,
    sub_chat_options: str,
) -> None:
    """Sub-agent options must not inherit parent continuation/storage settings."""
    import chrys.orchestration.sub_agents.tools as sub_agent_module

    profile = AgentProfile(
        name="Explore",
        instructions="Investigate.",
        tools=ToolsConfig(builtins=[]),
        model=ModelConfig(profile_id="sub-responses"),
    )
    parent_profile = ModelProfile(
        id="parent-model",
        name="parent",
        provider="openai",
        api_style="responses",
        model_id="gpt-parent",
        chat_options='{"store": true, "previous_response_id": "resp_parent"}',
    )
    sub_profile = ModelProfile(
        id="sub-responses",
        name="sub",
        provider=sub_provider,
        api_style="responses",
        model_id="gpt-sub",
        chat_options=sub_chat_options,
    )
    model_registry = ModelProfileRegistry()
    model_registry.register(sub_profile)
    agent_mock = MagicMock()
    agent_mock.client = SimpleNamespace(
        STORES_BY_DEFAULT=True,
        FORCES_STATELESS=sub_provider == "deepseek-openai",
    )
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    captured: dict[str, object] = {}

    class _Controller(SubAgentPolicyDouble):
        def __init__(self, *, shell, prompt: str, run_kwargs: dict, **_kwargs: object) -> None:
            super().__init__(shell=shell, prompt=prompt)
            invocation_id = shell.invocation_id
            captured["invocation_id"] = invocation_id
            captured["run_kwargs"] = run_kwargs

        async def execute(self) -> str:
            captured["route_session_id"] = llm_route_session_id.get()
            captured["parent_session_id"] = llm_parent_session_id.get()
            return "done"

    side_usage_sink = MagicMock()
    tools = SubAgentTools(session_id="sess-123", on_side_call_usage=side_usage_sink)
    try:
        with (
            patch("chrys.orchestration.invoker.runtime.Agent", return_value=agent_mock),
            patch("chrys.orchestration.sub_agents.tools.create_client", return_value=MagicMock(aclose=AsyncMock())),
            patch.object(sub_agent_module, "KernelSubAgentPolicy", _Controller),
        ):
            await tools.register(
                SubAgentRef(profile="Explore", tool_name="explore"),
                profile,
                SessionEnvironment.capture(session_id="sess-123"),
                settings=Settings(model_profile="parent-model"),
                fallback_profile=parent_profile,
                model_registry=model_registry,
            )
            assert await tools.get_tools()[0].func(prompt="hi") == "done"

        run_kwargs = captured["run_kwargs"]
        assert isinstance(run_kwargs, dict)
        assert run_kwargs["options"] == {"store": False, "max_tokens": 32000}
        assert "previous_response_id" not in run_kwargs["options"]
        wire_policy = run_kwargs["client_kwargs"]["wire_retry_policy"]
        assert wire_policy.stall_exhausted_action is StallExhaustedAction.RAISE
        assert wire_policy.before_retry() is None
        invocation_id = captured["invocation_id"]
        assert isinstance(invocation_id, str)
        assert captured["route_session_id"] == derive_llm_route_session_id(
            "sess-123",
            route_kind="sub-agent",
            route_parts=("explore", "Explore", invocation_id),
            model_profile=sub_profile,
        )
        assert captured["parent_session_id"] == "sess-123"
        compaction_strategy = run_kwargs["compaction_strategy"]
        generator = compaction_strategy._last_words_generator
        assert generator._session_id == derive_llm_route_session_id(
            "sess-123",
            route_kind="last-words",
            route_parts=("explore", "Explore", invocation_id),
            model_profile=sub_profile,
        )
        assert generator._parent_session_id == "sess-123"
        # The per-invocation generator carries the same usage sink too.
        assert generator._report_usage is side_usage_sink
    finally:
        await tools.cleanup()


async def test_concurrent_sub_agent_invocations_get_distinct_route_sessions() -> None:
    """Same-tool concurrent sub-agent calls should not share one route session."""
    import chrys.orchestration.sub_agents.tools as sub_agent_module

    profile = AgentProfile(
        name="Explore",
        instructions="Investigate.",
        tools=ToolsConfig(builtins=[]),
    )
    fallback_profile = ModelProfile(
        id="sub-model",
        name="sub",
        provider="mock",
        model_id="mock-sub",
    )
    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    both_started = asyncio.Event()
    captured: list[tuple[str, str, str]] = []

    class _Controller(SubAgentPolicyDouble):
        def __init__(self, *, shell, prompt: str, **_kwargs: object) -> None:
            super().__init__(shell=shell, prompt=prompt)
            invocation_id = shell.invocation_id
            self._invocation_id = invocation_id

        async def execute(self) -> str:
            captured.append(
                (
                    self._invocation_id,
                    llm_route_session_id.get(),
                    llm_parent_session_id.get(),
                )
            )
            if len(captured) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=5.0)
            return "done"

    tools = SubAgentTools(session_id="sess-123")
    try:
        with (
            patch("chrys.orchestration.invoker.runtime.Agent", return_value=agent_mock),
            patch("chrys.orchestration.sub_agents.tools.create_client", return_value=MagicMock(aclose=AsyncMock())),
            patch.object(sub_agent_module, "KernelSubAgentPolicy", _Controller),
        ):
            await tools.register(
                SubAgentRef(profile="Explore", tool_name="explore", max_concurrency=2),
                profile,
                SessionEnvironment.capture(session_id="sess-123"),
                settings=Settings(),
                fallback_profile=fallback_profile,
            )
            tool = tools.get_tools()[0]
            results = await asyncio.gather(tool.func(prompt="first"), tool.func(prompt="second"))

        assert results == ["done", "done"]
        assert len(captured) == 2
        invocation_ids = [item[0] for item in captured]
        route_session_ids = [item[1] for item in captured]
        parent_session_ids = [item[2] for item in captured]
        assert len(set(invocation_ids)) == 2
        assert len(set(route_session_ids)) == 2
        assert parent_session_ids == ["sess-123", "sess-123"]
        assert route_session_ids == [
            derive_llm_route_session_id(
                "sess-123",
                route_kind="sub-agent",
                route_parts=("explore", "Explore", invocation_id),
                model_profile=fallback_profile,
            )
            for invocation_id in invocation_ids
        ]
    finally:
        await tools.cleanup()


@pytest.mark.parametrize(
    ("parent_model_id", "sub_model_id", "sub_chat_options", "warned"),
    [
        ("claude-opus-4-7", "claude-opus-5-5", "", True),
        ("claude-opus-5-5", "claude-opus-4-7", "", False),
        ("claude-opus-5-5", "claude-opus-5-5", '{"thinking": {"type": "disabled"}}', False),
    ],
    ids=["sub-agent-model-binds", "parent-model-binds", "sub-agent-thinking-disabled"],
)
async def test_sub_agent_warns_once_when_its_on_demand_mcp_tools_can_unbind_its_thinking(
    caplog: pytest.LogCaptureFixture, parent_model_id: str, sub_model_id: str, sub_chat_options: str, warned: bool
) -> None:
    """The sub-agent's own model, chat options and connected servers decide the warning."""
    servers = [
        MCPServerConfig(name=name, transport="stdio", command="unused", use_progressive_disclosure=True)
        for name in ("alpha", "down", "beta")
    ]
    profile = AgentProfile(
        name="Explore",
        instructions="Investigate.",
        tools=ToolsConfig(builtins=[], mcp=servers),
        model=ModelConfig(profile_id="sub-model"),
    )
    parent_profile = ModelProfile(id="parent-model", name="parent", provider="anthropic", model_id=parent_model_id)
    model_registry = ModelProfileRegistry()
    model_registry.register(
        ModelProfile(
            id="sub-model", name="sub", provider="anthropic", model_id=sub_model_id, chat_options=sub_chat_options
        )
    )
    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()

    async def connect(_adapter: MCPAdapter, config: MCPServerConfig) -> list[Any]:
        if config.name == "down":
            raise RuntimeError("server down")
        return []

    tools = SubAgentTools(session_id="sess-123")
    try:
        with (
            patch("chrys.orchestration.invoker.runtime.Agent", return_value=agent_mock),
            patch("chrys.orchestration.sub_agents.tools.create_client", return_value=MagicMock(aclose=AsyncMock())),
            patch.object(MCPAdapter, "connect", new=create_autospec(MCPAdapter.connect, side_effect=connect)),
            caplog.at_level(logging.WARNING, logger="chrys.service.mcp.thinking_warning"),
        ):
            await tools.register(
                SubAgentRef(profile="Explore", tool_name="explore"),
                profile,
                SessionEnvironment.capture(session_id="sess-123"),
                settings=Settings(model_profile="parent-model"),
                fallback_profile=parent_profile,
                model_registry=model_registry,
            )
    finally:
        await tools.cleanup()

    messages = [record.getMessage() for record in caplog.records if record.name == "chrys.service.mcp.thinking_warning"]
    if warned:
        [message] = messages
        assert message.startswith(
            "Agent 'Explore' on model profile 'sub': MCP server(s) 'alpha', 'beta' load tools on demand"
        )
    else:
        assert messages == []


async def test_sub_agent_hands_its_servers_cwd_and_the_session_folder_to_the_mcp_adapter() -> None:
    """The sub-agent shell passes the session folder; ``cwd`` is resolved against it in the connection cache."""
    profile = AgentProfile(
        name="Explore",
        instructions="Investigate.",
        tools=ToolsConfig(
            builtins=[], mcp=[MCPServerConfig(name="own", transport="stdio", command="unused", cwd="tools/srv")]
        ),
    )
    parent_profile = ModelProfile(id="parent-model", name="parent", provider="mock", model_id="mock")
    model_registry = ModelProfileRegistry()
    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    seen: list[tuple[str, str | None]] = []

    async def connect(adapter: MCPAdapter, config: MCPServerConfig) -> list[Any]:
        seen.append((config.cwd, adapter._stdio_cwd))
        return []

    environment = SessionEnvironment.capture(session_id="sess-123")
    tools = SubAgentTools(session_id="sess-123")
    try:
        with (
            patch("chrys.orchestration.invoker.runtime.Agent", return_value=agent_mock),
            patch("chrys.orchestration.sub_agents.tools.create_client", return_value=MagicMock(aclose=AsyncMock())),
            patch.object(MCPAdapter, "connect", new=create_autospec(MCPAdapter.connect, side_effect=connect)),
        ):
            await tools.register(
                SubAgentRef(profile="Explore", tool_name="explore"),
                profile,
                environment,
                settings=Settings(model_profile="parent-model"),
                fallback_profile=parent_profile,
                model_registry=model_registry,
            )
    finally:
        await tools.cleanup()

    assert seen == [("tools/srv", environment.cwd)]
