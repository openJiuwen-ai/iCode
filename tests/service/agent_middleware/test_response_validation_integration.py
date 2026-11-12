# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Integration tests for :class:`ResponseValidationMiddleware` in the full pipeline.

The unit tests in ``test_response_validation.py`` exercise the middleware
in isolation with a hand-rolled ``_FakeCallNext``.  These integration
tests drive the **real** pipeline — :class:`AgentEngine` →
:class:`TurnBindings` → ``Agent.run()`` → :class:`MockChatClient` — in
both streaming and blocking mode.

What these tests guard against:

1. A response that showed nothing is sampled once more, announced as a
   retry; a second one fails the turn through the normal Error path with
   its own reason as the error text: no blank final message, and the error
   marker persisted for replay. Any other response without an answer fails
   at once.
2. The rejected response's usage is counted exactly once.
3. Answer text is never judged by what it says, and the tool loop and
   the streamed answer segments pass through validation unchanged.
4. An output-limit stop with nothing generated makes the user's Retry
   compact first; a stop after reasoning does not.
5. A sub-agent whose response showed nothing twice pauses on that reason,
   and the user's Retry recovers it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import chrys.orchestration.engine.build.builder as builder_module
import chrys.orchestration.sub_agents.tools as sub_agent_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    Error,
    Event,
    InvocationMessage,
    InvocationPaused,
    InvocationRetryAttempt,
    InvocationRetryRequested,
    InvocationStarted,
    InvocationToolCallResult,
    ToolCompacted,
    UsageUpdate,
    UserMessage,
    Warning,
)
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.workspace import Workspace
from chrys.orchestration.engine.engine import AgentEngine
from chrys.orchestration.engine.state.machine import EngineState
from chrys.service.agent_middleware.validators import (
    NO_VISIBLE_OUTPUT_REASON,
    OUTPUT_TRUNCATED_REASON,
    REASONING_EXHAUSTED_OUTPUT_REASON,
)
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    ModelConfig,
    SubAgentRef,
    SubAgentsConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from tests.support.pipeline_helpers import (
    create_test_engine,
    extract_answer_segments,
    extract_final_messages,
    extract_tool_results,
    extract_tool_starts,
)
from tests.support.waiting import ENGINE_TURN_TIMEOUT, await_run_task_chain, wait_for

if TYPE_CHECKING:
    from pathlib import Path

_MODES = pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])


# ===========================================================================
# Main agent — terminal verdicts
# ===========================================================================


_BLANK_RESPONSES = pytest.mark.parametrize(
    ("response", "reason"),
    [
        pytest.param(MockResponse(text=""), "empty contents", id="empty"),
        pytest.param(MockResponse(text="\n\n\n"), "empty or whitespace-only text response", id="whitespace"),
        pytest.param(MockResponse(reasoning_text="internal reasoning only"), NO_VISIBLE_OUTPUT_REASON, id="reasoning"),
    ],
)


class TestMainAgentTerminalVerdicts:
    @_BLANK_RESPONSES
    @_MODES
    @pytest.mark.asyncio
    async def test_a_response_that_showed_nothing_is_sampled_once_more(
        self, tmp_path: Path, stream: bool, response: MockResponse, reason: str
    ) -> None:
        ctx = await create_test_engine([response, MockResponse(text="done")], tmp_path, stream=stream)
        retries: list[InvocationRetryAttempt] = []
        errors: list[Error] = []

        async def _on_retry(ev: InvocationRetryAttempt) -> None:
            retries.append(ev)

        async def _on_error(ev: Error) -> None:
            errors.append(ev)

        await ctx.bus.subscribe(InvocationRetryAttempt, _on_retry)
        await ctx.bus.subscribe(Error, _on_error)

        try:
            await ctx.send_message("go")

            assert ctx.mock_client.call_count == 2
            assert [(retry.message, retry.attempt, retry.max_attempts, retry.delay_seconds) for retry in retries] == [
                (reason, 1, 1, 0)
            ]
            assert retries[0].session_id == ctx.session_id
            notice = retries[0].display_message
            assert notice is not None and notice.definition.key == "retry.blank_response"
            assert errors == []
            assert extract_final_messages(ctx.events) == ["done"]
            assert ctx.engine.state is EngineState.IDLE
        finally:
            await ctx.cleanup()

    @_BLANK_RESPONSES
    @_MODES
    @pytest.mark.asyncio
    async def test_a_second_response_that_showed_nothing_fails_the_turn_with_its_reason(
        self, tmp_path: Path, stream: bool, response: MockResponse, reason: str
    ) -> None:
        ctx = await create_test_engine(
            [response, response, MockResponse(text="must stay unused")], tmp_path, stream=stream
        )
        errors: list[Error] = []

        async def _on_error(ev: Error) -> None:
            errors.append(ev)

        await ctx.bus.subscribe(Error, _on_error)

        try:
            await ctx.send_message("go")

            assert ctx.mock_client.call_count == 2
            assert [(error.code, error.message) for error in errors] == [("executor_error", reason)]
            assert errors[0].session_id == ctx.session_id
            # Shown in the user's language; the raw reason stays the error text.
            display = errors[0].display_message
            assert display is not None and display.definition.key == "error.kind.blank_response"
            assert extract_final_messages(ctx.events) == []
            assert ctx.engine.state is EngineState.FAILED

            raw_messages = await ctx.get_session_messages()
            marker = raw_messages[1]
            assert marker.get("additional_properties", {}).get(HistoryMarkerKind.KEY) == HistoryMarkerKind.INTERRUPTED
            assert marker.get("additional_properties", {}).get("_interrupted_by") == "error"
            assert marker.get("contents", [{}])[0].get("text") == reason
        finally:
            await ctx.cleanup()

    @_MODES
    @pytest.mark.asyncio
    async def test_truncated_empty_response_fails_without_blank_agent_message_and_persists_error_marker(
        self,
        tmp_path: Path,
        stream: bool,
    ) -> None:
        """finish_reason=length + no output is a terminal turn error.

        It must render through the normal Error path, not the Warning/notify
        path, and it must persist the same error marker that replay uses after
        session reload.  The executor must not publish an empty final
        AgentMessage, which would create a blank Code Agent block in the TUI.
        Its usage is counted once, by the usage middleware below validation.
        """
        ctx = await create_test_engine(
            [
                MockResponse(
                    text="",
                    finish_reason="length",
                    usage_details={"input_token_count": 123, "output_token_count": 0, "total_token_count": 123},
                )
            ],
            tmp_path,
            stream=stream,
        )
        errors: list[Error] = []
        warnings: list[Warning] = []
        retries: list[InvocationRetryAttempt] = []
        usage_updates: list[UsageUpdate] = []

        async def _on_error(ev: Error) -> None:
            errors.append(ev)

        async def _on_warning(ev: Warning) -> None:
            warnings.append(ev)

        async def _on_retry(ev: InvocationRetryAttempt) -> None:
            retries.append(ev)

        async def _on_usage(ev: UsageUpdate) -> None:
            usage_updates.append(ev)

        await ctx.bus.subscribe(Error, _on_error)
        await ctx.bus.subscribe(Warning, _on_warning)
        await ctx.bus.subscribe(InvocationRetryAttempt, _on_retry)
        await ctx.bus.subscribe(UsageUpdate, _on_usage)

        try:
            await ctx.send_message("go")
            await wait_for(
                lambda: len(usage_updates) == 1,
                timeout=ENGINE_TURN_TIMEOUT,
                description="single usage update after invalid response",
            )

            assert ctx.mock_client.call_count == 1
            assert retries == []
            assert warnings == []
            assert len(usage_updates) == 1
            assert usage_updates[0].input_tokens == 123
            assert usage_updates[0].output_tokens == 0
            assert usage_updates[0].total_tokens == 123
            assert len(errors) == 1
            assert errors[0].code == "executor_error"
            assert errors[0].message == OUTPUT_TRUNCATED_REASON
            assert extract_final_messages(ctx.events) == []
            assert ctx.engine.state is EngineState.FAILED

            raw_messages = await ctx.get_session_messages()
            assert [m.get("role") for m in raw_messages] == ["user", "assistant", "assistant"]
            marker = raw_messages[1]
            assert marker.get("additional_properties", {}).get(HistoryMarkerKind.KEY) == HistoryMarkerKind.INTERRUPTED
            assert marker.get("additional_properties", {}).get("_interrupted_by") == "error"
            assert marker.get("contents", [{}])[0].get("text") == OUTPUT_TRUNCATED_REASON
            assert raw_messages[2].get("additional_properties", {}).get(HistoryMarkerKind.KEY) == HistoryMarkerKind.TURN
        finally:
            await ctx.cleanup()


# ===========================================================================
# Main agent — what validation leaves alone
# ===========================================================================


class TestMainAgentPassthrough:
    @_MODES
    @pytest.mark.asyncio
    async def test_tool_call_marker_text_is_delivered_as_the_answer(self, tmp_path: Path, stream: bool) -> None:
        text = "before\n<function_call>bad</function_call>\nminimax:tool_call {} </minimax:tool_call>"
        ctx = await create_test_engine([MockResponse(text=text)], tmp_path, stream=stream)
        errors: list[Error] = []

        async def _on_error(ev: Error) -> None:
            errors.append(ev)

        await ctx.bus.subscribe(Error, _on_error)

        try:
            await ctx.send_message("go")

            assert ctx.mock_client.call_count == 1
            assert errors == []
            assert extract_final_messages(ctx.events) == [text]
            if stream:
                assert extract_answer_segments(ctx.events) == [text]
        finally:
            await ctx.cleanup()

    @_MODES
    @pytest.mark.asyncio
    async def test_the_tool_loop_runs_through_validation(self, tmp_path: Path, stream: bool) -> None:
        ctx = await create_test_engine(
            [
                MockResponse(text="working on it...", tool_calls=[("echo", "call_1", {"message": "hi"})]),
                MockResponse(text="done"),
            ],
            tmp_path,
            stream=stream,
        )

        try:
            await ctx.send_message("go")

            assert ctx.mock_client.call_count == 2
            assert [start["tool_name"] for start in extract_tool_starts(ctx.events)] == ["echo"]
            [result] = extract_tool_results(ctx.events)
            assert "echo: hi" in result["result"]
            assert extract_final_messages(ctx.events) == ["done"]
            if stream:
                # The text before the call is its own answer segment, streamed as it arrived.
                assert extract_answer_segments(ctx.events) == ["working on it...", "done"]
        finally:
            await ctx.cleanup()


# ===========================================================================
# Main agent — output truncation and compaction
# ===========================================================================


class TestOutputTruncationCompaction:
    @pytest.mark.parametrize("text", ["", "   "], ids=["empty", "whitespace"])
    @_MODES
    @pytest.mark.asyncio
    async def test_a_reply_cut_off_before_any_output_makes_the_retry_compact_first(
        self,
        tmp_path: Path,
        stream: bool,
        text: str,
    ) -> None:
        """The input left no room to answer: the turn still fails, and its Retry compacts before resending."""
        ctx = await create_test_engine(
            [
                MockResponse(tool_calls=[("echo", "call-1", {"message": "x" * 4000})]),
                MockResponse(text="first answer"),
                MockResponse(text=text, finish_reason="length"),
                MockResponse(text="recovered answer"),
            ],
            tmp_path,
            stream=stream,
            compaction=CompactionConfig(enabled=True),
        )
        errors: list[Error] = []
        compacted: list[ToolCompacted] = []

        async def _on_error(ev: Error) -> None:
            errors.append(ev)

        async def _on_compacted(ev: ToolCompacted) -> None:
            compacted.append(ev)

        await ctx.bus.subscribe(Error, _on_error)
        await ctx.bus.subscribe(ToolCompacted, _on_compacted)

        try:
            await ctx.send_message("first")
            await ctx.send_message("second")

            assert ctx.mock_client.call_count == 3
            assert [error.message for error in errors] == [OUTPUT_TRUNCATED_REASON]
            assert compacted == []

            await ctx.send_retry()

            assert ctx.mock_client.call_count == 4
            assert [event.phase for event in compacted] == ["phase1", "phase2"]
            assert extract_final_messages(ctx.events) == ["first answer", "recovered answer"]
        finally:
            await ctx.cleanup()

    @_MODES
    @pytest.mark.asyncio
    async def test_an_empty_reply_after_hidden_reasoning_does_not_make_the_retry_compact(
        self,
        tmp_path: Path,
        stream: bool,
    ) -> None:
        """Usage counts reasoning the API did not return: the output limit ran out, not the window."""
        ctx = await create_test_engine(
            [
                MockResponse(tool_calls=[("echo", "call-1", {"message": "x" * 4000})]),
                MockResponse(text="first answer"),
                MockResponse(
                    finish_reason="length",
                    usage_details={"output_token_count": 4096, "reasoning_output_token_count": 4096},
                ),
                MockResponse(text="recovered answer"),
            ],
            tmp_path,
            stream=stream,
            compaction=CompactionConfig(enabled=True),
        )
        errors: list[Error] = []
        compacted: list[ToolCompacted] = []

        async def _on_error(ev: Error) -> None:
            errors.append(ev)

        async def _on_compacted(ev: ToolCompacted) -> None:
            compacted.append(ev)

        await ctx.bus.subscribe(Error, _on_error)
        await ctx.bus.subscribe(ToolCompacted, _on_compacted)

        try:
            await ctx.send_message("first")
            await ctx.send_message("second")
            assert [error.message for error in errors] == [OUTPUT_TRUNCATED_REASON]

            await ctx.send_retry()

            assert ctx.mock_client.call_count == 4
            assert extract_final_messages(ctx.events) == ["first answer", "recovered answer"]
            assert compacted == []
        finally:
            await ctx.cleanup()

    @_MODES
    @pytest.mark.asyncio
    async def test_a_reply_cut_off_after_reasoning_does_not_make_the_next_turn_compact(
        self,
        tmp_path: Path,
        stream: bool,
    ) -> None:
        """Reasoning was generated, so the window was not full: neither the resend nor the next turn compacts."""
        ctx = await create_test_engine(
            [
                MockResponse(tool_calls=[("echo", "call-1", {"message": "x" * 4000})]),
                MockResponse(text="first answer"),
                MockResponse(reasoning_text="thinking", finish_reason="length"),
                MockResponse(reasoning_text="thinking again", finish_reason="length"),
                MockResponse(text="third answer"),
            ],
            tmp_path,
            stream=stream,
            compaction=CompactionConfig(enabled=True),
        )
        errors: list[Error] = []
        compacted: list[ToolCompacted] = []

        async def _on_error(ev: Error) -> None:
            errors.append(ev)

        async def _on_compacted(ev: ToolCompacted) -> None:
            compacted.append(ev)

        await ctx.bus.subscribe(Error, _on_error)
        await ctx.bus.subscribe(ToolCompacted, _on_compacted)

        try:
            await ctx.send_message("first")
            await ctx.send_message("second")
            assert [error.message for error in errors] == [REASONING_EXHAUSTED_OUTPUT_REASON]

            await ctx.send_message("third")

            assert ctx.mock_client.call_count == 5
            assert extract_final_messages(ctx.events) == ["first answer", "third answer"]
            assert compacted == []
        finally:
            await ctx.cleanup()


# ===========================================================================
# Sub-agent — a rejected child response pauses on its reason
# ===========================================================================


class _SubAgentMiniCtx:
    def __init__(
        self,
        *,
        engine: AgentEngine,
        bus: EventBus,
        events: list[Event],
        main_client: MockChatClient,
        sub_client: MockChatClient,
        session_id: str,
        create_client_patcher: pytest.MonkeyPatch,
    ) -> None:
        self.engine = engine
        self.bus = bus
        self.events = events
        self.main_client = main_client
        self.sub_client = sub_client
        self.session_id = session_id
        self._create_client_patcher = create_client_patcher

    async def cleanup(self) -> None:
        self._create_client_patcher.undo()

    async def wait_for_idle(self, *, timeout: float = ENGINE_TURN_TIMEOUT) -> None:
        """Wait for the complete parent run-task chain, including its save."""
        await await_run_task_chain(
            self.engine, turn_state=self.engine.turns.turn_state, timeout=timeout, expect_installed=True
        )


async def _make_sub_agent_ctx(
    tmp_path: Path,
    *,
    main_responses: list[MockResponse],
    sub_responses: list[MockResponse],
    sub_stream: bool,
    agent_engine: Any,
) -> _SubAgentMiniCtx:
    """Build an engine with a parent + one sub-agent, each backed by its own
    ``MockChatClient`` routed via ``ModelProfile.id``."""
    events: list[Event] = []

    async def _collect(ev: Event) -> None:
        events.append(ev)

    bus = EventBus()
    for cls in (
        InvocationMessage,
        InvocationToolCallResult,
        InvocationStarted,
        InvocationPaused,
        InvocationRetryAttempt,
    ):
        await bus.subscribe(cls, _collect)

    model_registry = ModelProfileRegistry()
    model_registry.register(
        ModelProfile(
            id="main-mock",
            name="main",
            provider="mock",
            model_id="mock",
            stream=False,
            max_context_tokens=100_000,
        )
    )
    model_registry.register(
        ModelProfile(
            id="sub-mock",
            name="sub",
            provider="mock",
            model_id="mock",
            stream=sub_stream,
            max_context_tokens=100_000,
        )
    )
    # The turn budget must not go to advisory disk scans.
    settings = Settings(model_profile="main-mock", workspace_change_notice=False)
    store = JsonFileStateStore(tmp_path / "sessions")

    parent_profile = AgentProfile(
        name="Parent",
        instructions="Use Explore.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
        sub_agents=SubAgentsConfig(
            max_total_concurrency=2,
            agents=[SubAgentRef(profile="Explore", tool_name="Explore")],
        ),
    )
    sub_profile = AgentProfile(
        name="Explore",
        instructions="Explore and summarise.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
        model=ModelConfig(profile_id="sub-mock"),
    )
    agent_registry = AgentProfileRegistry()
    agent_registry.register(parent_profile)
    agent_registry.register(sub_profile)

    main_client = MockChatClient(responses=main_responses)
    sub_client = MockChatClient(responses=sub_responses)

    async def _patched_create(p: Any = None, **kw: Any) -> MockChatClient:
        profile_id = getattr(p, "id", "") or ""
        client = sub_client if profile_id == "sub-mock" else main_client
        client._on_intermediate_text = kw.get("on_intermediate_text")
        return client

    create_client_patcher = pytest.MonkeyPatch()
    create_client_patcher.setattr(builder_module, "create_client", _patched_create)
    sub_create_client_patcher = pytest.MonkeyPatch()
    sub_create_client_patcher.setattr(sub_agent_module, "create_client", _patched_create)

    engine = agent_engine(
        bus,
        settings=settings,
        agent_registry=agent_registry,
        model_registry=model_registry,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    try:
        await engine.start(parent_profile)
    except Exception:
        create_client_patcher.undo()
        sub_create_client_patcher.undo()
        raise

    # Restore sub_agent_module so any further sub-agent builds during the
    # test (there shouldn't be any) use the real path; the builder_module
    # patch stays live until cleanup.
    sub_create_client_patcher.undo()

    return _SubAgentMiniCtx(
        engine=engine,
        bus=bus,
        events=events,
        main_client=main_client,
        sub_client=sub_client,
        session_id=engine.session.session_id or "",
        create_client_patcher=create_client_patcher,
    )


class TestSubAgentRejectedResponse:
    @pytest.mark.parametrize("sub_stream", [False, True], ids=["blocking", "streaming"])
    @pytest.mark.asyncio
    async def test_a_rejected_child_response_pauses_on_its_reason_and_the_users_retry_recovers(
        self,
        tmp_path: Path,
        sub_stream: bool,
        agent_engine: Any,
    ) -> None:
        ctx = await _make_sub_agent_ctx(
            tmp_path,
            main_responses=[
                MockResponse(tool_calls=[("Explore", "call_1", {"prompt": "explore"})]),
                MockResponse(text="parent final"),
            ],
            sub_responses=[
                MockResponse(text=""),
                MockResponse(text=""),
                MockResponse(text="sub-agent final summary"),
            ],
            sub_stream=sub_stream,
            agent_engine=agent_engine,
        )

        try:
            await ctx.bus.publish(UserMessage(text="go"))
            await wait_for(
                lambda: any(isinstance(e, InvocationPaused) for e in ctx.events),
                timeout=ENGINE_TURN_TIMEOUT,
                description="sub-agent paused on the rejected response",
            )

            [paused] = [e for e in ctx.events if isinstance(e, InvocationPaused)]
            assert paused.last_error == "empty contents"
            assert ctx.sub_client.call_count == 2
            # The pause routes to the card the sub-agent's start opened.
            [started] = [
                e
                for e in ctx.events
                if isinstance(e, InvocationStarted) and e.origin.kind == "sub_agent" and e.tool_name == "Explore"
            ]
            assert paused.origin.invocation_id == started.origin.invocation_id

            await ctx.bus.publish(InvocationRetryRequested(invocation_id=paused.origin.invocation_id))
            await ctx.wait_for_idle()

            assert ctx.sub_client.call_count == 3
            # The one automatic resend, announced on the child's card.
            [resend] = [e for e in ctx.events if isinstance(e, InvocationRetryAttempt)]
            assert resend.origin == started.origin
            tool_results = [
                e
                for e in ctx.events
                if isinstance(e, InvocationToolCallResult) and e.origin.kind == "turn" and e.tool_name == "Explore"
            ]
            assert tool_results and "sub-agent final summary" in tool_results[-1].result
            assert extract_final_messages(ctx.events) == ["parent final"]
            assert ctx.engine.state is EngineState.IDLE
        finally:
            await ctx.cleanup()
