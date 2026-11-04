# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ``build_agent`` partial-build resource cleanup.

When a resource (MCP adapter, sub-agent tools, entered Agent) is allocated
and then a subsequent step fails, ``build_agent`` must roll back the
already-allocated resources in reverse order so the caller never ends up
with orphaned MCP subprocesses or an un-exited Agent.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from collections.abc import Awaitable, Callable
from contextlib import contextmanager, nullcontext
from typing import Any
from unittest.mock import AsyncMock, MagicMock, create_autospec, patch

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AGENT_LOAD_STATUS_DONE,
    AGENT_LOAD_STATUS_FAILED,
    AGENT_LOAD_STATUS_RUNNING,
    AGENT_LOAD_STATUS_SKIPPED,
    ApprovalAutoFulfillBlocked,
    CompactionFinished,
    CompactionStarted,
    InvocationRetryAttempt,
    Warning,
)
from chrys.foundation.i18n import DisplayBlock
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.retry import RetryAttemptInfo
from chrys.foundation.util.env_templates import EnvVarResolutionError
from chrys.orchestration.engine.build import builder as ab
from chrys.orchestration.invoker import runtime as runtime_factory_module
from chrys.orchestration.invoker.origin import invocation_routing_key
from chrys.service.context.compaction.last_words import CompactionStatus
from chrys.service.context.memory_loader import MemoryContent
from chrys.service.llm.route_sessions import derive_llm_route_session_id
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.profiles.agents.schema import (
    AcpAgentConfig,
    AgentProfile,
    CompactionConfig,
    MCPServerConfig,
    SubAgentRef,
    SubAgentsConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.options import ProtectedChatOptionsWarning
from chrys.service.profiles.models.resolver import ModelSelection
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.session.persistence import agent_profile_context_fingerprint, model_profile_context_fingerprint
from chrys.service.skills.model import SkillProviderWarning
from chrys.service.tools import packs as tool_packs
from tests.support.close_races import ReleaseGate, assert_cancel_during_rollback
from tests.support.extensions import install_file_extension


def _assert_warning_display_message(
    warning: Warning,
    key: str,
    args: dict[str, str | int],
) -> None:
    reference = warning.display_message
    assert reference is not None
    assert reference.definition.key == key
    assert dict(reference.args) == args


async def _capture_warnings(bus: EventBus) -> list[Warning]:
    warnings: list[Warning] = []

    async def _capture(event: Warning) -> None:
        warnings.append(event)

    await bus.subscribe(Warning, _capture)
    return warnings


def _agent_mock() -> MagicMock:
    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    agent_mock.create_session = MagicMock(return_value=MagicMock())
    return agent_mock


_REAL_ASSEMBLE = tool_packs.assemble_tools


def _capturing_assembly(captured: list[tool_packs.AssembledTools]) -> Callable[[tool_packs.ToolBuild], Any]:
    """Assemble the real tools, keeping each result in *captured*."""

    async def assemble(build: tool_packs.ToolBuild) -> tool_packs.AssembledTools:
        captured.append(await _REAL_ASSEMBLE(build))
        return captured[-1]

    return assemble


def test_runtime_api_style_labels_openai_compatible_transports() -> None:
    assert ab._runtime_api_style("openai", "responses") == "responses"
    assert ab._runtime_api_style("openai", "chat_completions") == "chat_completions"
    assert ab._runtime_api_style("deepseek-openai", "responses") == "responses"
    assert ab._runtime_api_style("glm-openai", "responses") == "chat_completions"
    assert ab._runtime_api_style("anthropic", "chat_completions") == ""


@contextmanager
def _build_agent_env(
    *,
    mcp_mock: MagicMock | MCPAdapter | None,
    agent_mock: MagicMock,
    real_packs: bool = False,
    model_profile: ModelProfile | None = None,
    chat_options: dict[str, Any] | None = None,
):
    """Patch every external dependency of ``build_agent`` so the test drives
    only the rollback logic.

    Function-local imports (tool assembly, ``MCPAdapter``,
    ``create_skills_provider``, ``SubAgentTools``) are patched at their
    source modules; module-level bindings are patched on ``agent_builder``.
    The packs yield no tools unless *real_packs*; the web tools are always assembled.
    """

    fake_ctx = MagicMock()
    fake_ctx.providers = []
    fake_ctx.middleware = []
    fake_ctx.compaction_strategy = MagicMock()
    last_words_cls = MagicMock(return_value=MagicMock(aclose=AsyncMock()))
    reminder_cls = MagicMock(return_value=MagicMock())

    mcp_patch = (
        patch("chrys.service.mcp.adapter.MCPAdapter", return_value=mcp_mock)
        if mcp_mock is not None
        else patch("chrys.service.mcp.adapter.MCPAdapter")
    )

    with (
        patch.object(runtime_factory_module, "Agent", return_value=agent_mock),
        patch.object(runtime_factory_module, "ContextManager", return_value=fake_ctx) as context_manager_cls,
        patch.object(ab, "create_client", return_value=MagicMock(aclose=AsyncMock())),
        patch.object(
            ab,
            "resolve_selection_for_agent",
            return_value=ModelSelection(model_profile or ModelProfile(id="test-id", name="test"), "override"),
        ),
        patch.object(ab, "effective_chat_options", return_value=chat_options or {}),
        patch.object(ab, "LoopRecorder", return_value=MagicMock()),
        patch.object(runtime_factory_module, "SystemReminderMiddleware", reminder_cls),
        patch.object(runtime_factory_module, "LastWordsGenerator", last_words_cls),
        nullcontext() if real_packs else patch.object(tool_packs, "create_packs", return_value=[]),
        patch("chrys.service.skills.adapter.create_skills_provider", new=AsyncMock(return_value=(None, []))),
        mcp_patch as mcp_cls,
    ):
        yield {
            "last_words_cls": last_words_cls,
            "mcp_cls": mcp_cls,
            "context": fake_ctx,
            "context_manager_cls": context_manager_cls,
            "reminder_cls": reminder_cls,
        }


async def _invoke_build_agent(
    profile: AgentProfile,
    *,
    session_id: str | None = None,
    agent_registry: object | None = None,
    on_load_progress: Callable[..., Awaitable[None]] | None = None,
    bus: EventBus | None = None,
    spill_quota: object | None = None,
    persist_recovery_now: Callable[[], Awaitable[bool]] | None = None,
    on_side_call_usage: Callable[..., None] | None = None,
    settings: Settings | None = None,
    allow_user_interaction: bool = True,
) -> ab.AgentBuildResult:
    """Call ``build_agent`` with the minimum set of dependencies required to
    reach the ``Agent.__aenter__`` step.  Returns the ``AgentBuildResult`` so
    success-path tests can assert on its contents."""
    event_bus = bus or EventBus()
    effective_settings = settings or Settings()

    def _sync_noop(*_a: object, **_k: object) -> None:
        return None

    return await ab.build_agent(
        profile=profile,
        settings=effective_settings,
        workspace=None,
        session_id=session_id,
        bus=event_bus,
        agent_registry=agent_registry,
        injection=MagicMock(),
        batch_counter=MagicMock(),
        on_intermediate=_sync_noop,
        on_usage=_sync_noop,
        on_load_progress=on_load_progress,
        spill_quota=spill_quota,
        persist_recovery_now=persist_recovery_now,
        on_side_call_usage=on_side_call_usage,
        allow_user_interaction=allow_user_interaction,
    )


async def test_build_agent_propagates_cancel_received_during_rollback() -> None:
    release = ReleaseGate()
    mcp = MagicMock()
    mcp.connect_all = AsyncMock(return_value=[])
    mcp.disconnect_all = AsyncMock(side_effect=release.__call__)
    agent = MagicMock()
    agent.__aenter__ = AsyncMock(side_effect=ValueError("Agent enter failed"))
    profile = AgentProfile(
        name="test", tools=ToolsConfig(mcp=[MCPServerConfig(name="srv", transport="stdio", command="python")])
    )
    with _build_agent_env(mcp_mock=mcp, agent_mock=agent):
        building = asyncio.create_task(_invoke_build_agent(profile))
        await assert_cancel_during_rollback(building, release)
    mcp.disconnect_all.assert_awaited_once()


async def test_build_agent_surfaces_missing_chat_options_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The main build path should fail loudly when chat_options references a missing env var."""
    monkeypatch.delenv("CHRYS_MISSING_CHAT_OPTION", raising=False)
    profile = AgentProfile(name="test")
    active_profile = ModelProfile(
        id="test-id",
        name="test-model",
        chat_options='{"metadata": {"token": "{{CHRYS_MISSING_CHAT_OPTION}}"}}',
    )

    fake_ctx = MagicMock()
    fake_ctx.providers = []
    fake_ctx.middleware = []
    fake_ctx.compaction_strategy = MagicMock()
    agent_mock = MagicMock()

    with (
        patch.object(runtime_factory_module, "Agent", return_value=agent_mock),
        patch.object(runtime_factory_module, "ContextManager", return_value=fake_ctx),
        patch.object(ab, "create_client", return_value=MagicMock(aclose=AsyncMock())),
        patch.object(
            ab,
            "resolve_selection_for_agent",
            return_value=ModelSelection(active_profile, "active"),
        ),
        patch.object(tool_packs, "create_packs", return_value=[]),
        pytest.raises(EnvVarResolutionError) as info,
    ):
        await _invoke_build_agent(profile)

    message = str(info.value)
    assert "CHRYS_MISSING_CHAT_OPTION" in message
    assert "model profile 'test-model' chat option['metadata']['token']" in message


# ---------------------------------------------------------------------------
# Rollback on Agent.__aenter__ failure
# ---------------------------------------------------------------------------


async def test_rolls_back_mcp_when_agent_enter_fails() -> None:
    """Agent context-manager failure must trigger MCPAdapter.disconnect_all."""
    profile = AgentProfile(
        name="test",
        tools=ToolsConfig(mcp=[MCPServerConfig(name="srv", transport="stdio", command="python")]),
    )

    mcp_mock = MagicMock()
    mcp_mock.connect_all = AsyncMock(return_value=[])
    mcp_mock.disconnect_all = AsyncMock()

    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(side_effect=RuntimeError("enter failed"))
    agent_mock.__aexit__ = AsyncMock()

    with (
        _build_agent_env(mcp_mock=mcp_mock, agent_mock=agent_mock),
        pytest.raises(RuntimeError, match="enter failed"),
    ):
        await _invoke_build_agent(profile)

    mcp_mock.connect_all.assert_awaited_once()
    mcp_mock.disconnect_all.assert_awaited_once()
    # Agent.__aenter__ raised, so __aexit__ must NOT be called on the
    # half-entered agent (rollback is registered only after a successful enter).
    agent_mock.__aexit__.assert_not_awaited()


async def test_build_rejects_mcp_chrys_name_collision_and_rolls_back() -> None:
    """Builder reserves native names and does not downgrade collisions to warnings."""
    from chrys.service.mcp.adapter import MCPToolNameCollisionError

    profile = AgentProfile(
        name="test",
        tools=ToolsConfig(mcp=[MCPServerConfig(name="srv", transport="stdio", command="python")]),
    )
    collision = MCPToolNameCollisionError(
        "srv",
        "stdio",
        conflicting_names={"read_file"},
        conflict_with="a reserved iCode tool",
        guidance="Exclude the remote tool from the Permitted Tool Set or configure a Tool Name Prefix.",
    )
    mcp_mock = MagicMock()
    mcp_mock.connect_all = AsyncMock(side_effect=collision)
    mcp_mock.disconnect_all = AsyncMock()
    agent_mock = MagicMock()

    with (
        _build_agent_env(mcp_mock=mcp_mock, agent_mock=agent_mock) as dependencies,
        pytest.raises(MCPToolNameCollisionError, match=r"reserved iCode tool.*read_file"),
    ):
        await _invoke_build_agent(profile)

    reserved_names = dependencies["mcp_cls"].call_args.kwargs["reserved_tool_names"]
    assert "read_file" in reserved_names
    mcp_mock.disconnect_all.assert_awaited_once()


async def test_build_reserves_provider_hosted_web_tool_names_from_mcp() -> None:
    """An MCP tool named like a provider-run web tool is refused at connect, where the guidance is."""
    profile = AgentProfile(
        name="test",
        tools=ToolsConfig(mcp=[MCPServerConfig(name="srv", transport="stdio", command="python")]),
    )
    mcp_mock = MagicMock()
    mcp_mock.connect_all = AsyncMock(side_effect=RuntimeError("stop after connect"))
    mcp_mock.disconnect_all = AsyncMock()
    hosted = {"tools": [{"type": "web_search_preview_2025_03_11"}]}

    with (
        _build_agent_env(mcp_mock=mcp_mock, agent_mock=MagicMock()) as dependencies,
        patch.object(ab, "effective_chat_options", return_value=hosted),
        pytest.raises(RuntimeError, match="stop after connect"),
    ):
        await _invoke_build_agent(profile)

    assert "web_search" in dependencies["mcp_cls"].call_args.kwargs["reserved_tool_names"]


async def test_build_rejects_provider_invalid_mcp_tool_name_and_rolls_back() -> None:
    """Provider-invalid MCP names abort agent loading instead of becoming warnings."""
    from chrys.service.mcp.adapter import MCPToolNameValidationError

    profile = AgentProfile(
        name="test",
        tools=ToolsConfig(mcp=[MCPServerConfig(name="srv", transport="stdio", command="python")]),
    )
    invalid = MCPToolNameValidationError(
        "srv",
        "stdio",
        violations={"tool name 'remote' is 65 characters; the maximum is 64."},
    )
    mcp_mock = MagicMock()
    mcp_mock.connect_all = AsyncMock(side_effect=invalid)
    mcp_mock.disconnect_all = AsyncMock()
    agent_mock = MagicMock()

    with (
        _build_agent_env(mcp_mock=mcp_mock, agent_mock=agent_mock),
        pytest.raises(MCPToolNameValidationError, match="maximum is 64"),
    ):
        await _invoke_build_agent(profile)

    mcp_mock.disconnect_all.assert_awaited_once()


async def test_no_rollback_when_mcp_disabled() -> None:
    """Profile without MCP: build reaching Agent failure doesn't blow up on
    missing mcp_adapter (the rollback list is just shorter)."""
    profile = AgentProfile(name="test")  # no MCP

    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(side_effect=RuntimeError("enter failed"))
    agent_mock.__aexit__ = AsyncMock()

    with _build_agent_env(mcp_mock=None, agent_mock=agent_mock), pytest.raises(RuntimeError, match="enter failed"):
        await _invoke_build_agent(profile)

    agent_mock.__aexit__.assert_not_awaited()


async def test_rollback_swallows_disconnect_errors_and_propagates_original() -> None:
    """If a rollback callback raises, the original build exception still wins."""
    profile = AgentProfile(
        name="test",
        tools=ToolsConfig(mcp=[MCPServerConfig(name="srv", transport="stdio", command="python")]),
    )

    mcp_mock = MagicMock()
    mcp_mock.connect_all = AsyncMock(return_value=[])
    mcp_mock.disconnect_all = AsyncMock(side_effect=RuntimeError("disconnect broke"))

    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(side_effect=RuntimeError("enter failed"))
    agent_mock.__aexit__ = AsyncMock()

    with (
        _build_agent_env(mcp_mock=mcp_mock, agent_mock=agent_mock),
        pytest.raises(RuntimeError, match="enter failed"),
    ):
        await _invoke_build_agent(profile)

    mcp_mock.disconnect_all.assert_awaited_once()


# ---------------------------------------------------------------------------
# Success path: rollback does NOT run
# ---------------------------------------------------------------------------


async def test_success_path_does_not_disconnect_mcp() -> None:
    """On a successful build, rollback is abandoned — lifecycle transfers
    to the engine which calls disconnect_all during shutdown."""
    profile = AgentProfile(
        name="test",
        tools=ToolsConfig(mcp=[MCPServerConfig(name="srv", transport="stdio", command="python")]),
    )

    mcp_mock = MagicMock()
    mcp_mock.connect_all = AsyncMock(return_value=[])
    mcp_mock.disconnect_all = AsyncMock()

    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    agent_mock.create_session = MagicMock(return_value=MagicMock())

    # TurnBindings is reached on the success path — patch it too so we don't
    # build a real one.
    executor_mock = MagicMock()
    with (
        _build_agent_env(mcp_mock=mcp_mock, agent_mock=agent_mock),
        patch.object(ab, "TurnBindings", return_value=executor_mock),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        result = await _invoke_build_agent(profile)

    # Success: the adapter must still be live, not disconnected.
    mcp_mock.disconnect_all.assert_not_awaited()
    # The returned AgentBuildResult carries the freshly-built agent/executor
    # and the still-live MCP adapter — lifecycle now belongs to the caller.
    assert isinstance(result, ab.AgentBuildResult)
    assert result.agent is agent_mock
    assert result.bindings is executor_mock
    assert result.mcp_adapter is mcp_mock
    assert result.runtime_details.model.selection_source == "override"


async def test_build_result_fingerprint_includes_loaded_memory_and_interaction_capability() -> None:
    profile = AgentProfile(name="test", instructions="Use test.")
    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    agent_mock.create_session = MagicMock(return_value=MagicMock())
    executor_mock = MagicMock()

    with (
        _build_agent_env(mcp_mock=None, agent_mock=agent_mock),
        patch.object(
            ab,
            "load_memory_content",
            return_value=MemoryContent(text="memory v1", loaded_files=["AGENTS.md"]),
        ),
        patch.object(ab, "TurnBindings", return_value=executor_mock),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        result = await _invoke_build_agent(profile, allow_user_interaction=False)

    assert result.agent_profile_fingerprint == agent_profile_context_fingerprint(
        profile,
        memory_text="memory v1",
        allow_user_interaction=False,
    )
    assert result.agent_profile_fingerprint != agent_profile_context_fingerprint(profile, memory_text="memory v2")
    assert result.agent_profile_fingerprint != agent_profile_context_fingerprint(
        profile,
        memory_text="memory v1",
        allow_user_interaction=True,
    )
    assert result.model_profile_fingerprint == model_profile_context_fingerprint(
        ModelProfile(id="test-id", name="test"),
        chat_options={},
    )


async def test_build_agent_publishes_protected_chat_options_warning() -> None:
    profile = AgentProfile(name="test")
    bus = EventBus()
    warnings: list[Warning] = []

    async def _capture_warning(event: Warning) -> None:
        warnings.append(event)

    await bus.subscribe(Warning, _capture_warning)
    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    agent_mock.create_session = MagicMock(return_value=MagicMock())

    with (
        _build_agent_env(mcp_mock=None, agent_mock=agent_mock),
        patch.object(
            ab,
            "protected_chat_option_keys_warning_structured",
            return_value=ProtectedChatOptionsWarning(keys=("conversation_id", "prompt")),
        ),
        patch.object(ab, "TurnBindings", return_value=MagicMock()),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        await _invoke_build_agent(profile, session_id="session-1", bus=bus)

    assert len(warnings) == 1
    assert (warnings[0].code, warnings[0].message, warnings[0].session_id) == (
        "protected_chat_options_stripped",
        (
            "Protected chat option key(s) were stripped: conversation_id, prompt. "
            "These keys can no longer be configured in profile YAML because they bypass context admission. "
            "Reusable prompts and profile-pinned continuation IDs are unavailable there; store: true only enables "
            "automatically managed Responses continuation."
        ),
        "session-1",
    )
    _assert_warning_display_message(
        warnings[0],
        "builder.protected_chat_options_stripped",
        {"keys": "conversation_id, prompt"},
    )


async def test_build_agent_reports_hosted_web_tools_that_displace_a_local_category() -> None:
    profile = AgentProfile(name="test", tools=ToolsConfig(builtins=["web_search"]))
    bus = EventBus()
    warnings = await _capture_warnings(bus)
    captured: list[tool_packs.AssembledTools] = []
    hosted = {"tools": [{"type": "web_search"}]}

    with (
        _build_agent_env(mcp_mock=None, agent_mock=_agent_mock()),
        patch.object(
            tool_packs, "assemble_tools", create_autospec(_REAL_ASSEMBLE, side_effect=_capturing_assembly(captured))
        ),
        patch.object(ab, "effective_chat_options", return_value=hosted),
        patch.object(ab, "TurnBindings", return_value=MagicMock()),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        await _invoke_build_agent(profile, session_id="session-1", bus=bus)

    assert [warning.code for warning in warnings] == ["hosted_web_tools_preferred"]
    assert warnings[0].session_id == "session-1"
    assert "web_search" in warnings[0].message
    assert "test-id" in warnings[0].message
    assert [result.web.hosted for result in captured] == [{"web_search"}]
    _assert_warning_display_message(warnings[0], "builder.hosted_web_tools_preferred", {"tools": "web_search"})


async def test_build_agent_does_not_expose_displaced_local_web_tool_to_model() -> None:
    profile = AgentProfile(name="test", tools=ToolsConfig(builtins=["web_search"]))
    hosted = {"tools": [{"type": "web_search_2025_08_26"}]}

    with (
        _build_agent_env(mcp_mock=None, agent_mock=_agent_mock(), real_packs=True),
        patch.object(ab, "effective_chat_options", return_value=hosted),
        patch.object(ab, "TurnBindings", return_value=MagicMock()),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        result = await _invoke_build_agent(profile, session_id="session-1")

    assert "web_search" not in result.tool_names


# Pinned apart from ``packs.IMPLEMENTATION_MODULES``: dropping one there must fail here.
_TOOL_IMPLEMENTATION_MODULES = (
    "chrys.service.tools.builtins.filesystem",
    "chrys.service.tools.builtins.search",
    "chrys.service.tools.builtins.web.build",
    "chrys.service.tools.builtins.shell",
    "chrys.service.tools.builtins.ask_user",
    "chrys.service.tools.builtins.todo",
    "chrys.service.tools.builtins.sleep",
    "chrys.service.vision",
)


async def test_build_agent_imports_the_tool_implementations_off_the_event_loop(monkeypatch) -> None:
    # A cold start spends a visible part of the build importing them, which on
    # the event loop would freeze the TUI's loading animation.
    agent_mock = _agent_mock()
    agent_mock.__aenter__ = AsyncMock(side_effect=RuntimeError("stop after the tools"))
    loaded = {name: importlib.import_module(name) for name in _TOOL_IMPLEMENTATION_MODULES}
    warmed: list[tuple[str, ...]] = []

    async def import_off_loop(*requested: str) -> None:
        warmed.append(requested)
        for name in requested:
            if name in loaded:
                monkeypatch.setitem(sys.modules, name, loaded[name])

    # The document converter, enabled by few agents, loads when created.
    every_pack = [pack.id for pack in tool_packs.BUILTIN_PACKS if pack.id != "doc_converter"]
    with (
        _build_agent_env(mcp_mock=None, agent_mock=agent_mock, real_packs=True),
        patch.object(ab, "import_off_loop", import_off_loop),
        pytest.raises(RuntimeError, match="stop after the tools"),
    ):
        # Until the build hands them to the worker, importing them fails.
        for name in _TOOL_IMPLEMENTATION_MODULES:
            monkeypatch.setitem(sys.modules, name, None)
        await _invoke_build_agent(AgentProfile(name="test", tools=ToolsConfig(builtins=every_pack)))

    assert len(warmed) == 1
    assert set(_TOOL_IMPLEMENTATION_MODULES) <= set(warmed[0])


async def test_build_agent_loads_sleep_though_the_profile_omits_it() -> None:
    """Every agent has sleep, headless runs included; the runtime details list it like any builtin."""
    profile = AgentProfile(name="test", tools=ToolsConfig(builtins=["todo"]))

    with (
        _build_agent_env(mcp_mock=None, agent_mock=_agent_mock(), real_packs=True),
        patch.object(ab, "TurnBindings", return_value=MagicMock()),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        result = await _invoke_build_agent(profile, session_id="session-1", allow_user_interaction=False)

    assert "sleep" in result.tool_names
    assert result.runtime_details.builtin_tools == {"todo": ["todo_write"], "sleep": ["sleep"]}


@pytest.mark.parametrize(
    ("name", "builtins", "chat_options", "mcp_namespace"),
    [
        ("acme_free", [], None, frozenset({"lookup"})),
        ("todo_write", ["todo"], None, frozenset()),
        ("web_search", [], {"tools": [{"type": "web_search"}]}, frozenset()),
        ("lookup", [], None, frozenset({"lookup", "load_srv_tools"})),
    ],
    ids=["no-clash", "local-tool", "hosted", "mcp-tool-not-yet-loaded"],
)
async def test_build_agent_loads_extensions_last_so_their_tools_yield_every_name_its_other_tools_take(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    builtins: list[str],
    chat_options: dict[str, Any] | None,
    mcp_namespace: frozenset[str],
) -> None:
    install_file_extension(
        monkeypatch,
        "acme_clash",
        "from chrys.extensions import Extension\nextension = Extension()\n"
        "@extension.tool\nasync def acme_fine() -> str:\n    return ''\n"
        f"@extension.tool(name={name!r})\nasync def other() -> str:\n    return ''\n",
    )
    # The MCP server's tool is deferred: it is in the server's namespace, not among the tools it returns.
    mcp_mock = MagicMock()
    mcp_mock.connect_all = AsyncMock(return_value=[])
    mcp_mock.disconnect_all = AsyncMock()
    mcp_mock.tool_namespace = mcp_namespace
    mcp_mock.tool_names_by_server = {}
    mcp_mock.failures = {}
    profile = AgentProfile(
        name="test",
        tools=ToolsConfig(
            builtins=builtins,
            mcp=[MCPServerConfig(name="srv", transport="stdio", command="python")],
        ),
        extensions=["acme_clash"],
    )
    bus = EventBus()
    warnings = await _capture_warnings(bus)

    with (
        _build_agent_env(mcp_mock=mcp_mock, agent_mock=_agent_mock(), real_packs=True, chat_options=chat_options),
        patch.object(ab, "TurnBindings", return_value=MagicMock()),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        result = await _invoke_build_agent(profile, session_id="session-1", bus=bus)

    extension_warnings = [warning for warning in warnings if warning.code == "extension_unavailable"]
    if name == "acme_free":
        assert result.tool_names[-2:] == ["acme_fine", "acme_free"]
        assert result.runtime_details.extension_tools == {"acme_clash": ["acme_fine", "acme_free"]}
        assert extension_warnings == []
    else:
        assert "acme_fine" not in result.tool_names
        reason = f"its tools {name} have names already in use"
        assert result.runtime_details.extension_failures == {"acme_clash": reason}
        assert [warning.message for warning in extension_warnings] == [
            f"Agent test starts without extension acme_clash: {reason}"
        ]


async def test_build_agent_omits_hosted_web_notice_when_no_local_category_yields() -> None:
    """A declaration that displaces nothing local must not claim it disabled a tool."""
    profile = AgentProfile(name="test", tools=ToolsConfig(builtins=[]))
    bus = EventBus()
    warnings = await _capture_warnings(bus)
    captured: list[tool_packs.AssembledTools] = []
    hosted = {"tools": [{"type": "web_search"}]}

    with (
        _build_agent_env(mcp_mock=None, agent_mock=_agent_mock()),
        patch.object(
            tool_packs, "assemble_tools", create_autospec(_REAL_ASSEMBLE, side_effect=_capturing_assembly(captured))
        ),
        patch.object(ab, "effective_chat_options", return_value=hosted),
        patch.object(ab, "TurnBindings", return_value=MagicMock()),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        await _invoke_build_agent(profile, session_id="session-1", bus=bus)

    assert warnings == []
    assert [result.web.hosted for result in captured] == [{"web_search"}]


async def test_build_agent_publishes_semantic_memory_truncation_warning() -> None:
    profile = AgentProfile(name="test")
    bus = EventBus()
    warnings: list[Warning] = []

    async def _capture_warning(event: Warning) -> None:
        warnings.append(event)

    await bus.subscribe(Warning, _capture_warning)
    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    agent_mock.create_session = MagicMock(return_value=MagicMock())
    memory = MemoryContent(
        text="memory",
        loaded_files=["AGENTS.md", "notes.md"],
        skipped_files=["archive.md"],
        truncated=True,
    )

    with (
        _build_agent_env(mcp_mock=None, agent_mock=agent_mock),
        patch.object(ab, "load_memory_content", return_value=memory),
        patch.object(ab, "TurnBindings", return_value=MagicMock()),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        await _invoke_build_agent(profile, session_id="session-1", bus=bus)

    assert len(warnings) == 1
    assert (warnings[0].code, warnings[0].message, warnings[0].session_id) == (
        "memory_truncated",
        "Auto-loaded memory truncated at the token cap. Loaded 2 file(s); skipped 1.",
        "session-1",
    )
    _assert_warning_display_message(
        warnings[0],
        "builder.memory_truncated",
        {"loaded_count": 2, "skipped_count": 1},
    )


async def test_build_agent_keeps_skill_warning_pass_through_unbound() -> None:
    profile = AgentProfile(name="test")
    bus = EventBus()
    warnings: list[Warning] = []

    async def _capture_warning(event: Warning) -> None:
        warnings.append(event)

    await bus.subscribe(Warning, _capture_warning)
    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    agent_mock.create_session = MagicMock(return_value=MagicMock())
    skill_warning = SkillProviderWarning(code="skill_load_error", message="dynamic skill diagnostic")

    with (
        _build_agent_env(mcp_mock=None, agent_mock=agent_mock),
        patch(
            "chrys.service.skills.adapter.create_skills_provider",
            new=AsyncMock(return_value=(None, [skill_warning])),
        ),
        patch.object(ab, "TurnBindings", return_value=MagicMock()),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        await _invoke_build_agent(profile, session_id="session-1", bus=bus)

    assert len(warnings) == 1
    assert (warnings[0].code, warnings[0].message, warnings[0].session_id) == (
        "skill_load_error",
        "dynamic skill diagnostic",
        "session-1",
    )
    assert warnings[0].display_message is None


async def test_build_failure_unsubscribes_approval_middleware() -> None:
    """A build failure after ApprovalMiddleware construction must close its EventBus handler."""
    profile = AgentProfile(name="test")
    bus = EventBus()

    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    agent_mock.create_session = MagicMock(return_value=MagicMock())

    def _executor_fails(**kwargs: object) -> None:
        approval_middleware = kwargs["approval_middleware"]
        bus._handlers[ApprovalAutoFulfillBlocked].append(approval_middleware._on_auto_fulfill_blocked)
        approval_middleware._approval_arbiter._subscribed = True
        raise RuntimeError("executor failed")

    with (
        _build_agent_env(mcp_mock=None, agent_mock=agent_mock),
        patch.object(ab, "TurnBindings", side_effect=_executor_fails),
        pytest.raises(RuntimeError, match="executor failed"),
    ):
        await _invoke_build_agent(profile, bus=bus)

    assert bus._handlers[ApprovalAutoFulfillBlocked] == []
    agent_mock.__aexit__.assert_awaited_once()


async def test_build_agent_passes_session_id_to_last_words_generator() -> None:
    """Main-agent Phase 4 compaction should carry the active session header."""
    supplement = "Preserve exact integration-test evidence."
    profile = AgentProfile(name="test", compaction=CompactionConfig(last_words_template=supplement))
    bus = EventBus()
    retries: list[InvocationRetryAttempt] = []

    async def _capture_retry(event: InvocationRetryAttempt) -> None:
        retries.append(event)

    await bus.subscribe(InvocationRetryAttempt, _capture_retry)

    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    agent_mock.create_session = MagicMock(return_value=MagicMock())

    with (
        _build_agent_env(mcp_mock=None, agent_mock=agent_mock) as env,
        patch.object(ab, "TurnBindings", return_value=MagicMock()) as bindings_cls,
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        await _invoke_build_agent(profile, session_id="sess-phase4", bus=bus)

    env["last_words_cls"].assert_called_once()
    last_words_kwargs = env["last_words_cls"].call_args.kwargs
    expected_session_id = derive_llm_route_session_id(
        "sess-phase4",
        route_kind="last-words",
        route_parts=(profile.name,),
        model_profile=last_words_kwargs["profile"],
    )
    assert last_words_kwargs["session_id"] == expected_session_id
    assert last_words_kwargs["parent_session_id"] == "sess-phase4"
    assert last_words_kwargs["template"] == supplement
    publish_retry = last_words_kwargs["publish_retry"]
    assert callable(publish_retry)

    origin = InvocationOrigin("turn", "sess-phase4", "callback-test", None)
    bindings_cls.call_args.kwargs["invocation_publishers"].bind(origin)
    token = invocation_routing_key.set(origin.invocation_id)
    try:
        await publish_retry(RetryAttemptInfo(reason="connection dropped", attempt=1, max_attempts=5, delay_seconds=3))
    finally:
        invocation_routing_key.reset(token)

    assert len(retries) == 1
    assert retries[0].message == "LAST_WORDS compaction: connection dropped"
    assert retries[0].attempt == 1
    assert retries[0].max_attempts == 5
    assert retries[0].delay_seconds == 3
    assert retries[0].scope == "compaction"
    assert retries[0].session_id == "sess-phase4"
    assert retries[0].detail == "connection dropped"
    assert retries[0].display_message is not None
    assert retries[0].display_message.definition.key == "retry.last_words_compaction"
    assert dict(retries[0].display_message.args) == {"reason": DisplayBlock("connection dropped")}


async def test_build_agent_threads_effective_transient_retry_budget() -> None:
    profile = AgentProfile(name="test")
    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    agent_mock.create_session = MagicMock(return_value=MagicMock())
    executor_cls = MagicMock(return_value=MagicMock())

    with (
        _build_agent_env(mcp_mock=None, agent_mock=agent_mock) as env,
        patch.object(ab, "TurnBindings", executor_cls),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        await _invoke_build_agent(
            profile,
            settings=Settings(max_transient_retries=8, frontend_default_max_transient_retries=10),
        )

    assert executor_cls.call_args.kwargs["max_transient_retries"] == 8
    assert env["last_words_cls"].call_args.kwargs["max_transient_retries"] == 8


async def test_build_agent_wires_dark_recovery_callback_and_spill_quota() -> None:
    from chrys.service.context.compaction.spill import SpillQuota

    profile = AgentProfile(name="test")
    quota = SpillQuota()

    async def _persist_recovery_now() -> bool:
        return True

    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    agent_mock.create_session = MagicMock(return_value=MagicMock())

    with (
        _build_agent_env(mcp_mock=None, agent_mock=agent_mock) as env,
        patch.object(ab, "TurnBindings", return_value=MagicMock()),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        await _invoke_build_agent(
            profile,
            session_id="spill-wiring",
            spill_quota=quota,
            persist_recovery_now=_persist_recovery_now,
        )

    context_manager_call = env["context_manager_cls"].call_args
    assert context_manager_call.kwargs["spill_quota"] is quota
    assert context_manager_call.kwargs["session_dir"] is not None
    reminder_kwargs = env["reminder_cls"].call_args.kwargs
    assert reminder_kwargs["session_root"] == context_manager_call.kwargs["session_dir"]
    assert reminder_kwargs["spill_quota"] is quota
    assert reminder_kwargs["file_read_available"] is False
    assert callable(context_manager_call.kwargs["on_context_pressure"])
    env["context"].compaction_strategy.set_recovery_persistence_callback.assert_called_once_with(_persist_recovery_now)


async def test_build_agent_wires_compaction_status_to_bus_events() -> None:
    """Phase-4 status signals surface as CompactionStarted/Finished bus events."""
    profile = AgentProfile(name="test")
    bus = EventBus()
    started_events: list[CompactionStarted] = []
    finished_events: list[CompactionFinished] = []

    async def _capture_started(event: CompactionStarted) -> None:
        started_events.append(event)

    async def _capture_finished(event: CompactionFinished) -> None:
        finished_events.append(event)

    await bus.subscribe(CompactionStarted, _capture_started)
    await bus.subscribe(CompactionFinished, _capture_finished)

    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    agent_mock.create_session = MagicMock(return_value=MagicMock())

    with (
        _build_agent_env(mcp_mock=None, agent_mock=agent_mock) as env,
        patch.object(ab, "TurnBindings", return_value=MagicMock()),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        await _invoke_build_agent(profile, session_id="sess-phase4", bus=bus)

    publish_status = env["last_words_cls"].call_args.kwargs["publish_status"]
    assert callable(publish_status)

    await publish_status(CompactionStatus(compaction_id="c-1", stage="started"))
    await publish_status(
        CompactionStatus(
            compaction_id="c-1",
            stage="finished",
            outcome="ok",
            duration_ms=1234,
            last_words="[note]",
            format_violation='missing required heading "## Next"',
        )
    )

    assert len(started_events) == 1
    assert started_events[0].compaction_id == "c-1"
    assert started_events[0].session_id == "sess-phase4"
    assert len(finished_events) == 1
    assert finished_events[0].compaction_id == "c-1"
    assert finished_events[0].outcome == "ok"
    assert finished_events[0].duration_ms == 1234
    assert finished_events[0].last_words == "[note]"
    assert finished_events[0].format_violation == 'missing required heading "## Next"'
    assert finished_events[0].session_id == "sess-phase4"


async def test_build_agent_wires_side_call_usage_to_last_words_generator() -> None:
    """The engine's side-call usage sink reaches the main-agent Phase-4
    generator as its ``report_usage`` hook (Token Usage panel accounting)."""
    profile = AgentProfile(name="test")
    usage_sink = MagicMock()

    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    agent_mock.create_session = MagicMock(return_value=MagicMock())

    with (
        _build_agent_env(mcp_mock=None, agent_mock=agent_mock) as env,
        patch.object(ab, "TurnBindings", return_value=MagicMock()),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        await _invoke_build_agent(profile, session_id="sess-phase4", on_side_call_usage=usage_sink)

    assert env["last_words_cls"].call_args.kwargs["report_usage"] is usage_sink


async def test_sub_agent_progress_counts_after_register_completes() -> None:
    """Sub-agent progress must not report 1/1 until registration finishes."""
    profile = AgentProfile(
        name="test",
        sub_agents=SubAgentsConfig(agents=[SubAgentRef(profile="Explore")]),
    )
    sub_profile = AgentProfile(name="Explore")

    class _Registry:
        def get(self, name: str) -> AgentProfile | None:
            return sub_profile if name == "Explore" else None

    timeline: list[tuple[str, int, int, str, str, str, str] | str] = []

    async def _on_progress(
        *,
        phase: str,
        message: str,
        server_name: str = "",
        current: int = 0,
        total: int = 0,
        failed: int = 0,
        status: str = "",
        subject: str = "",
        detail: str = "",
    ) -> None:
        del failed
        if phase == "sub_agents":
            timeline.append((message, current, total, status, subject, detail, server_name))

    async def _register(*_args: object, **_kwargs: object) -> None:
        timeline.append("register")

    sub_agent_tools = MagicMock()
    sub_agent_tools.register = AsyncMock(side_effect=_register)
    sub_agent_tools.cleanup = AsyncMock()
    sub_agent_tools.get_tools = MagicMock(return_value=[])

    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    agent_mock.create_session = MagicMock(return_value=MagicMock())

    with (
        _build_agent_env(mcp_mock=None, agent_mock=agent_mock),
        patch(
            "chrys.orchestration.sub_agents.tools.SubAgentTools", return_value=sub_agent_tools
        ) as sub_agent_tools_cls,
        patch.object(ab, "TurnBindings", return_value=MagicMock()),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        await _invoke_build_agent(
            profile,
            agent_registry=_Registry(),
            on_load_progress=_on_progress,
            settings=Settings(max_transient_retries=8),
            allow_user_interaction=False,
        )

    assert timeline[:4] == [
        ("Loading sub-agent tools", 0, 1, AGENT_LOAD_STATUS_RUNNING, "", "", ""),
        ("Loading sub-agent Explore", 0, 1, AGENT_LOAD_STATUS_RUNNING, "Explore", "", "Explore"),
        "register",
        ("Loaded sub-agent Explore", 1, 1, AGENT_LOAD_STATUS_DONE, "Explore", "", "Explore"),
    ]
    assert sub_agent_tools_cls.call_args.kwargs["max_transient_retries"] == 8
    assert sub_agent_tools_cls.call_args.kwargs["allow_user_interaction"] is False


@pytest.mark.parametrize(
    ("sub_profile", "expected_message", "expected_detail"),
    [
        (None, "Skipped sub-agent External", ""),
        (
            AgentProfile(name="External", acp=AcpAgentConfig(command="")),
            "Skipped sub-agent External: empty ACP command",
            "empty ACP command",
        ),
    ],
)
async def test_sub_agent_skip_progress_preserves_message_and_exposes_reason(
    sub_profile: AgentProfile | None,
    expected_message: str,
    expected_detail: str,
) -> None:
    profile = AgentProfile(
        name="test",
        sub_agents=SubAgentsConfig(agents=[SubAgentRef(profile="External")]),
    )

    class _Registry:
        def get(self, name: str) -> AgentProfile | None:
            return sub_profile if name == "External" else None

    progress_events: list[dict[str, object]] = []

    async def _on_progress(
        *,
        phase: str,
        message: str,
        server_name: str = "",
        current: int = 0,
        total: int = 0,
        failed: int = 0,
        status: str = "",
        subject: str = "",
        detail: str = "",
    ) -> None:
        progress_events.append(
            {
                "phase": phase,
                "message": message,
                "server_name": server_name,
                "current": current,
                "total": total,
                "failed": failed,
                "status": status,
                "subject": subject,
                "detail": detail,
            }
        )

    sub_agent_tools = MagicMock()
    sub_agent_tools.cleanup = AsyncMock()
    sub_agent_tools.get_tools = MagicMock(return_value=[])
    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    agent_mock.create_session = MagicMock(return_value=MagicMock())

    with (
        _build_agent_env(mcp_mock=None, agent_mock=agent_mock),
        patch("chrys.orchestration.sub_agents.tools.SubAgentTools", return_value=sub_agent_tools),
        patch.object(ab, "TurnBindings", return_value=MagicMock()),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        await _invoke_build_agent(profile, agent_registry=_Registry(), on_load_progress=_on_progress)

    skipped = [event for event in progress_events if event["status"] == AGENT_LOAD_STATUS_SKIPPED]
    assert skipped == [
        {
            "phase": "sub_agents",
            "message": expected_message,
            "server_name": "External",
            "current": 1,
            "total": 1,
            "failed": 0,
            "status": AGENT_LOAD_STATUS_SKIPPED,
            "subject": "External",
            "detail": expected_detail,
        }
    ]


async def test_agent_load_progress_maps_builder_and_mcp_states_without_changing_messages() -> None:
    config = MCPServerConfig(name="srv", transport="stdio", command="python")
    profile = AgentProfile(name="test", tools=ToolsConfig(mcp=[config]))
    progress_events: list[dict[str, object]] = []

    async def _on_progress(
        *,
        phase: str,
        message: str,
        server_name: str = "",
        current: int = 0,
        total: int = 0,
        failed: int = 0,
        status: str = "",
        subject: str = "",
        detail: str = "",
    ) -> None:
        progress_events.append(
            {
                "phase": phase,
                "message": message,
                "server_name": server_name,
                "current": current,
                "total": total,
                "failed": failed,
                "status": status,
                "subject": subject,
                "detail": detail,
            }
        )

    async def _connect_all(
        configs: list[MCPServerConfig],
        *,
        progress: Callable[..., Awaitable[None]],
    ) -> list[object]:
        active = configs[0]
        await progress(active, "starting", 0, 1, 0)
        await progress(active, "connected", 1, 1, 0)
        await progress(active, "failed", 0, 1, 1)
        await progress(active, "unrecognized", 0, 1, 0)
        return []

    mcp_mock = MagicMock()
    mcp_mock.connect_all = AsyncMock(side_effect=_connect_all)
    mcp_mock.disconnect_all = AsyncMock()
    mcp_mock.tool_names_by_server = {}
    mcp_mock.create_resume_provider = MagicMock(return_value=None)
    mcp_mock.failures = {}
    agent_mock = MagicMock()
    agent_mock.__aenter__ = AsyncMock(return_value=agent_mock)
    agent_mock.__aexit__ = AsyncMock()
    agent_mock.create_session = MagicMock(return_value=MagicMock())

    with (
        _build_agent_env(mcp_mock=mcp_mock, agent_mock=agent_mock),
        patch.object(ab, "TurnBindings", return_value=MagicMock()),
        patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
        patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
        patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
    ):
        await _invoke_build_agent(profile, on_load_progress=_on_progress)

    base_progress = [
        (event["phase"], event["message"], event["status"]) for event in progress_events if event["subject"] == ""
    ]
    assert base_progress == [
        ("model", "Resolving model profile", AGENT_LOAD_STATUS_RUNNING),
        ("runtime", "Capturing workspace context", AGENT_LOAD_STATUS_RUNNING),
        ("tools", "Loading built-in tools", AGENT_LOAD_STATUS_RUNNING),
        ("tools", "Built-in tools loaded", AGENT_LOAD_STATUS_DONE),
        ("mcp", "Connecting MCP servers", AGENT_LOAD_STATUS_RUNNING),
        ("skills", "Loading skills", AGENT_LOAD_STATUS_RUNNING),
        ("skills", "Skills loaded", AGENT_LOAD_STATUS_DONE),
        ("agent", "Finalizing agent", AGENT_LOAD_STATUS_RUNNING),
    ]
    mcp_progress = [
        (
            event["message"],
            event["status"],
            event["subject"],
            event["server_name"],
            event["detail"],
        )
        for event in progress_events
        if event["subject"] == "srv"
    ]
    assert mcp_progress == [
        ("Connecting MCP server srv", AGENT_LOAD_STATUS_RUNNING, "srv", "srv", ""),
        ("Connected MCP server srv", AGENT_LOAD_STATUS_DONE, "srv", "srv", ""),
        ("Failed MCP server srv", AGENT_LOAD_STATUS_FAILED, "srv", "srv", ""),
        ("Loading MCP server srv", AGENT_LOAD_STATUS_RUNNING, "srv", "srv", ""),
    ]


@pytest.mark.parametrize(
    ("chat_options", "warned"),
    [({}, True), ({"thinking": {"type": "disabled"}}, False)],
    ids=["no-thinking-option", "thinking-disabled"],
)
async def test_build_warns_once_when_on_demand_mcp_tools_can_unbind_thinking(
    caplog: pytest.LogCaptureFixture, chat_options: dict[str, Any], warned: bool
) -> None:
    servers = [
        MCPServerConfig(name=name, transport="stdio", command="python", use_progressive_disclosure=True)
        for name in ("alpha", "down", "beta")
    ]
    servers.append(
        MCPServerConfig(
            name="idle", transport="stdio", command="python", use_progressive_disclosure=True, enabled=False
        )
    )
    profile = AgentProfile(name="test", tools=ToolsConfig(mcp=servers))
    model = ModelProfile(id="claude", name="Claude 5.5", provider="anthropic", model_id="claude-opus-5-5")
    adapter = MCPAdapter()

    async def connect(_adapter: MCPAdapter, config: MCPServerConfig) -> list[Any]:
        if config.name == "down":
            raise RuntimeError("server down")
        return []

    try:
        with (
            _build_agent_env(
                mcp_mock=adapter, agent_mock=_agent_mock(), model_profile=model, chat_options=chat_options
            ),
            patch.object(MCPAdapter, "connect", new=create_autospec(MCPAdapter.connect, side_effect=connect)),
            patch.object(ab, "TurnBindings", return_value=MagicMock()),
            patch.object(runtime_factory_module, "ApprovalMiddleware", return_value=MagicMock(close=AsyncMock())),
            patch.object(runtime_factory_module, "AskUserMiddleware", return_value=MagicMock()),
            patch.object(ab, "ApprovalPolicy", return_value=MagicMock()),
            caplog.at_level(logging.WARNING, logger="chrys.service.mcp.thinking_warning"),
        ):
            await _invoke_build_agent(profile)
    finally:
        await adapter.disconnect_all()

    messages = [record.getMessage() for record in caplog.records if record.name == "chrys.service.mcp.thinking_warning"]
    if warned:
        [message] = messages
        assert message.startswith(
            "Agent 'test' on model profile 'Claude 5.5': MCP server(s) 'alpha', 'beta' load tools on demand"
        )
    else:
        assert messages == []
