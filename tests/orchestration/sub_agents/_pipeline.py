# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The parent + sub-agent engine pipeline that sub-agent integration tests drive.

The parent and its ``Explore`` sub-agent each get an :class:`ErrorMockChatClient`, so a test scripts both sides'
responses and failures and drives the full chain: UserMessage → engine → parent ``agent.run()`` → sub-agent tool →
``SubAgentToolShell`` → sub-agent ``agent.run()`` → retry / pause / resume / abort → the parent's tool result.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import chrys.orchestration.engine.build.builder as builder_module
import chrys.orchestration.sub_agents.tools as sub_agent_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ApprovalRequest,
    ApprovalResponse,
    Event,
    InvocationAborted,
    InvocationCascadeAborted,
    InvocationContentDelta,
    InvocationMessage,
    InvocationPaused,
    InvocationProgress,
    InvocationResumed,
    InvocationRetryAttempt,
    InvocationToolCallResult,
    InvocationToolCallStart,
    SessionReady,
    UsageUpdate,
)
from chrys.foundation.models.workspace import Workspace
from chrys.orchestration.sub_agents.kernel_policy import KernelSubAgentPolicy
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    ModelConfig,
    SkillsConfig,
    SubAgentRef,
    SubAgentsConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import DEFAULT_MAX_OUTPUT_TOKENS, ModelProfile
from chrys.service.state.store import JsonFileStateStore
from tests.support.scripted_clients import ErrorMockChatClient
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, await_run_task_chain, wait_for

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.foundation.models.session_surface import SessionSurface
    from chrys.orchestration.engine.engine import AgentEngine


def use_fast_sub_agent_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make :class:`KernelSubAgentPolicy` use minimal retries + no backoff.

    ``SubAgentTools._make_tool`` constructs the controller without
    passing ``max_retries`` / ``backoff_schedule``, so defaults apply.
    We substitute a subclass whose defaults are test-friendly.
    """

    class _FastController(KernelSubAgentPolicy):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("max_retries", 1)
            kwargs.setdefault("backoff_schedule", (0,))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(sub_agent_module, "KernelSubAgentPolicy", _FastController)


class _SubAgentPipelineCtx:
    """Holds everything a sub-agent integration test needs."""

    def __init__(
        self,
        *,
        engine: AgentEngine,
        bus: EventBus,
        events: list[Event],
        main_client: ErrorMockChatClient,
        sub_client: ErrorMockChatClient,
        store: JsonFileStateStore,
        session_id: str,
        create_client_patcher: pytest.MonkeyPatch,
    ) -> None:
        self.engine = engine
        self.bus = bus
        self.events = events
        self.main_client = main_client
        self.sub_client = sub_client
        self.store = store
        self.session_id = session_id
        self._create_client_patcher = create_client_patcher

    async def cleanup(self) -> None:
        self._create_client_patcher.undo()

    # Ceilings sized for contended CI workers (a loaded Windows runner has
    # pushed the mock-client pipeline past 20s) while staying under the
    # global 60s per-test timeout, so the failure below still names the
    # observed events instead of a thread kill.
    async def wait_for_event(self, event_type: type, *, timeout: float = 45.0, min_count: int = 1) -> list[Event]:
        def matches() -> list[Event]:
            return [e for e in self.events if isinstance(e, event_type)]

        try:
            await wait_for(lambda: len(matches()) >= min_count, timeout=timeout, description=event_type.__name__)
        except AssertionError as exc:
            raise AssertionError(f"{exc} (saw {[type(e).__name__ for e in self.events]})") from None
        return matches()

    async def wait_for_idle(self, *, timeout: float = ENGINE_TEST_WAIT_TIMEOUT) -> None:
        """Wait for the complete parent run-task chain, including its save."""
        await await_run_task_chain(
            self.engine, turn_state=self.engine.turns.turn_state, timeout=timeout, expect_installed=True
        )


async def _make_ctx(
    tmp_path: Path,
    *,
    main_outcomes: list[MockResponse | BaseException],
    sub_outcomes: list[MockResponse | BaseException],
    sub_stream: bool = False,
    sub_builtins: list[str] | None = None,
    search_respect_gitignore: bool = True,
    sub_skills: SkillsConfig | None = None,
    sub_extensions: list[str] | None = None,
    sub_responses_store: bool = False,
    sub_compaction: CompactionConfig | None = None,
    sub_max_context_tokens: int = 100_000,
    sub_max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    surface: SessionSurface | None = None,
    allow_user_interaction: bool = True,
    agent_engine,
) -> _SubAgentPipelineCtx:
    """Build a parent+sub-agent engine with independent mock clients.

    The parent invokes the sub-agent tool ``Explore`` (arg ``prompt``).
    Clients are routed by ``ModelProfile.id``: the parent's active
    profile is ``"main-mock-profile"`` and the sub-agent pins
    ``"sub-mock-profile"`` via its ``ModelConfig.profile_id``. So a
    single ``create_client`` patch can return the right client to each
    caller.
    """
    events: list[Event] = []

    async def _collect(ev: Event) -> None:
        events.append(ev)

    bus = EventBus()
    for cls in (
        SessionReady,
        InvocationMessage,
        InvocationContentDelta,
        ApprovalRequest,
        ApprovalResponse,
        InvocationToolCallStart,
        InvocationToolCallResult,
        InvocationPaused,
        InvocationResumed,
        InvocationAborted,
        InvocationCascadeAborted,
        InvocationRetryAttempt,
        InvocationProgress,
        UsageUpdate,
    ):
        await bus.subscribe(cls, _collect)

    # Two model profiles so create_client routing is trivial.
    model_registry = ModelProfileRegistry()
    main_profile = ModelProfile(
        id="main-mock-profile",
        name="main",
        provider="mock",
        model_id="mock",
        stream=False,
        max_context_tokens=100_000,
    )
    sub_profile = ModelProfile(
        id="sub-mock-profile",
        name="sub",
        provider="openai" if sub_responses_store else "mock",
        api_style="responses" if sub_responses_store else "chat_completions",
        model_id="gpt-sub" if sub_responses_store else "mock",
        stream=sub_stream,
        max_context_tokens=sub_max_context_tokens,
        max_output_tokens=sub_max_output_tokens,
        chat_options='{"store": true}' if sub_responses_store else "",
    )
    model_registry.register(main_profile)
    model_registry.register(sub_profile)

    # These scenarios exercise parent/child turns, not advisory disk scans.
    settings = Settings(
        model_profile="main-mock-profile",
        workspace_change_notice=False,
        search_respect_gitignore=search_respect_gitignore,
    )
    store = JsonFileStateStore(tmp_path / "sessions")

    parent_profile = AgentProfile(
        name="Parent",
        instructions="You are the parent. Use the Explore sub-agent.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
        sub_agents=SubAgentsConfig(
            max_total_concurrency=2,
            agents=[SubAgentRef(profile="Explore", tool_name="Explore")],
        ),
    )
    sub_agent_profile = AgentProfile(
        name="Explore",
        instructions="You explore. Return a summary.",
        tools=ToolsConfig(builtins=sub_builtins or []),
        skills=sub_skills or SkillsConfig(auto_load_user_agents_skills=False, auto_load_cwd_agents_skills=False),
        approval=ApprovalConfig(default="auto"),
        compaction=sub_compaction or CompactionConfig(enabled=False),
        extensions=sub_extensions or [],
        # Pin sub-agent to its own model profile so create_client routes correctly.
        model=ModelConfig(profile_id="sub-mock-profile"),
    )
    agent_registry = AgentProfileRegistry()
    agent_registry.register(parent_profile)
    agent_registry.register(sub_agent_profile)

    main_client = ErrorMockChatClient(outcomes=main_outcomes)
    sub_client = ErrorMockChatClient(outcomes=sub_outcomes)

    async def _patched_create_client(p: Any = None, **kw: Any) -> MockChatClient:
        # Route by resolved ModelProfile id.  Both clients get the
        # intermediate-text callbacks installed so streaming callbacks
        # behave identically to the real path (main is non-streaming in
        # these tests, but this keeps the helper robust if a future test
        # flips streaming on).
        profile_id = getattr(p, "id", "") or ""
        client = sub_client if profile_id == "sub-mock-profile" else main_client
        client._on_intermediate_text = kw.get("on_intermediate_text")
        return client

    create_client_patcher = pytest.MonkeyPatch()
    create_client_patcher.setattr(builder_module, "create_client", _patched_create_client)

    # Also patch the sub_agent module's create_client reference so
    # SubAgentTools.register (which imports create_client at the module
    # top) picks up the test-scoped routing, not the real one.
    sub_create_client_patcher = pytest.MonkeyPatch()
    sub_create_client_patcher.setattr(sub_agent_module, "create_client", _patched_create_client)

    engine = agent_engine(
        bus,
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
        allow_user_interaction=allow_user_interaction,
        surface=surface,
    )
    try:
        await engine.start(parent_profile)
    except Exception:
        create_client_patcher.undo()
        sub_create_client_patcher.undo()
        raise

    # sub_agent_module import was already consumed during start; restore
    # it here so other tests aren't affected.  The parent builder_module
    # patch stays live until cleanup (it's needed if the engine rebuilds).
    sub_create_client_patcher.undo()

    return _SubAgentPipelineCtx(
        engine=engine,
        bus=bus,
        events=events,
        main_client=main_client,
        sub_client=sub_client,
        store=store,
        session_id=engine.session.session_id or "",
        create_client_patcher=create_client_patcher,
    )


def _sub_tool_call(prompt: str, call_id: str = "call-1") -> MockResponse:
    return MockResponse(tool_calls=[("Explore", call_id, {"prompt": prompt})])


def _child_answer_segments(ctx: _SubAgentPipelineCtx) -> list[tuple[int, str]]:
    """Each streamed sub-agent answer segment: the index of its first delta and its whole text."""
    segments: dict[str, tuple[int, str]] = {}
    for index, event in enumerate(ctx.events):
        if isinstance(event, InvocationContentDelta) and event.origin.kind == "sub_agent" and event.channel == "answer":
            first, text = segments.get(event.segment_id, (index, ""))
            segments[event.segment_id] = (first, text + event.text)
    return list(segments.values())


def _explore_result(ctx: _SubAgentPipelineCtx) -> InvocationToolCallResult:
    (tool_result,) = [
        e
        for e in ctx.events
        if isinstance(e, InvocationToolCallResult) and e.origin.kind == "turn" and e.tool_name == "Explore"
    ]
    return tool_result
