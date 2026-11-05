# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""End-to-end integration tests for sub-agent pause / retry / abort.

These tests drive the full pipeline through :class:`MockChatClient` for
both the parent and the sub-agent, exercising the complete chain:

    UserMessage → Engine → TurnBindings → main agent.run()
                → MockChatClient (parent) emits tool_call for sub-agent
                → SubAgentTools._invoke → SubAgentToolShell.run()
                → sub-agent.run() → MockChatClient (sub) raises / succeeds
                → retry loop / pause / resume / abort
                → parent continues with tool_result

The controller-only tests in ``test_sub_agent_controller.py`` use a
hand-rolled stub agent; they cover the controller's state machine in
isolation. These tests exercise AgentEngine and SubAgentToolShell together —
FSM edges, marker insertion under real publish timing,
parent-history repair, event wiring. That's what this file is for.

Retry budgets and backoffs are aggressively shrunk (``max_retries=1``,
zero delays) so tests converge in milliseconds. That's handled by
monkey-patching :class:`KernelSubAgentPolicy`'s constructor defaults for
the duration of the test.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import (
    ApprovalRequest,
    ApprovalResponse,
    Event,
    InvocationAborted,
    InvocationAbortRequested,
    InvocationCascadeAborted,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationMessage,
    InvocationPaused,
    InvocationProgress,
    InvocationResumed,
    InvocationRetryAttempt,
    InvocationRetryRequested,
    InvocationToolCallResult,
    InvocationToolCallStart,
    UsageUpdate,
    UserInterrupt,
    UserMessage,
)
from chrys.foundation.hosted_tools import HostedToolFamily
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.platform import get_platform
from chrys.foundation.tool_kinds import KIND_SKILL
from chrys.foundation.util.sub_agent_context import (
    SUB_AGENT_PROGRESS_METADATA_KEY,
    SUB_AGENT_TRANSCRIPT_FINAL_TEXT_METADATA_KEY,
)
from chrys.kernel import ChatResponseUpdate, Content
from chrys.orchestration.engine.state.machine import EngineState
from chrys.service.context.compaction.last_words import LastWordsGenerator
from chrys.service.llm.mock import MockResponse
from chrys.service.profiles.agents.schema import (
    CompactionConfig,
    SkillConfig,
    SkillsConfig,
)
from chrys.service.session.message_metadata import TOOL_RESULT_METADATA_KEY
from chrys.service.skills.constants import RUN_SKILL_SCRIPT_TOOL_NAME
from chrys.service.tools.background import MAIN_SCOPE
from tests.orchestration.sub_agents._pipeline import (
    _child_answer_segments,
    _explore_result,
    _make_ctx,
    _sub_tool_call,
    _SubAgentPipelineCtx,
    use_fast_sub_agent_policy,
)
from tests.support.extensions import install_file_extension
from tests.support.pipeline_helpers import fail_nested_before_response_on_nth
from tests.support.scripted_clients import FrameworkBoom, HostedMockResponse, hosted_image_result
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from chrys.kernel import FinishReason, FinishReasonLiteral


# ---------------------------------------------------------------------------
# Error-capable mock client
# ---------------------------------------------------------------------------


# A retryable SDK-style exception.  The name matches
# ``errors.RETRYABLE_TYPE_NAMES`` so it hits the auto-retry path
# without pausing.
class APIConnectionError(Exception):
    pass


# ---------------------------------------------------------------------------
# Fixture: fast controller (shrink retry budget + zero backoff)
# ---------------------------------------------------------------------------


@pytest.fixture
def fast_sub_agent_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make :class:`KernelSubAgentPolicy` use minimal retries + no backoff."""
    use_fast_sub_agent_policy(monkeypatch)


# ---------------------------------------------------------------------------
# Scenario tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("respect", [True, False])
async def test_sub_agent_search_inherits_parent_setting(tmp_path: Path, git_repo_factory, agent_engine, respect: bool):
    root = git_repo_factory(tmp_path / "repo")
    (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (root / "ignored.py").write_text("NEEDLE\n", encoding="utf-8")
    (root / "visible.py").write_text("NEEDLE\n", encoding="utf-8")
    ctx = await _make_ctx(
        root,
        main_outcomes=[_sub_tool_call("Search files"), MockResponse(text="Done")],
        sub_outcomes=[
            MockResponse(tool_calls=[("grep", "search-call", {"pattern": "NEEDLE", "glob": "*.py"})]),
            MockResponse(text="Searched"),
        ],
        sub_builtins=["search"],
        search_respect_gitignore=respect,
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="Search"))
        await ctx.wait_for_idle()
        results = [
            event for event in ctx.events if isinstance(event, InvocationToolCallResult) and event.tool_name == "grep"
        ]
        assert len(results) == 1
        assert "visible.py" in str(results[0].result)
        assert ("ignored.py" in str(results[0].result)) is not respect
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_sub_agent_gates_runtime_skill_script_under_parent_auto_default(
    tmp_path: Path,
    fast_sub_agent_controller: None,
    agent_engine,
) -> None:
    """Sub-agent provider tools retain KIND_SKILL through the real kernel loop."""
    sub_skills = SkillsConfig(
        inline=[SkillConfig(name="review", description="Review files", instructions="Review carefully.")],
        auto_load_user_agents_skills=False,
        auto_load_cwd_agents_skills=False,
    )
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("Run the review skill"), MockResponse(text="Parent finished")],
        sub_outcomes=[
            MockResponse(
                tool_calls=[
                    (
                        RUN_SKILL_SCRIPT_TOOL_NAME,
                        "sub-skill-call",
                        {"skill_name": "review", "script_name": "scripts/review.py"},
                    )
                ]
            ),
            MockResponse(text="Sub-agent handled rejection"),
        ],
        sub_skills=sub_skills,
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="Delegate the review"))

        approval_events = await ctx.wait_for_event(ApprovalRequest)
        request = approval_events[0]
        assert isinstance(request, ApprovalRequest)
        assert request.tool_name == RUN_SKILL_SCRIPT_TOOL_NAME
        assert request.tool_kind == KIND_SKILL

        await ctx.bus.publish(
            ApprovalResponse(request_id=request.request_id, approved=False, reason="untrusted sub-agent skill")
        )
        await ctx.wait_for_idle()

        assert ctx.sub_client.call_count == 2
        assert ctx.main_client.call_count == 2
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_auto_retry_succeeds_without_pausing(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine
) -> None:
    """Retryable SDK error → controller auto-retries inside StreamRetryLoop
    and succeeds on the second attempt; no pause, no user input required.
    The parent continues to a final text response."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[
            _sub_tool_call("explore please"),
            MockResponse(text="Final summary after sub-agent."),
        ],
        sub_outcomes=[
            APIConnectionError("peer closed connection"),
            MockResponse(text="sub-agent result"),
        ],
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        await ctx.wait_for_idle()

        # No pause should have fired.
        paused = [e for e in ctx.events if (isinstance(e, InvocationPaused) and e.origin.kind == "sub_agent")]
        assert paused == []

        # At least one InvocationRetryAttempt published for the transient error.
        retries = [e for e in ctx.events if (isinstance(e, InvocationRetryAttempt) and e.origin.kind == "sub_agent")]
        assert len(retries) >= 1
        assert retries[0].origin.invocation_id  # non-empty

        # Parent received the sub-agent's result as a ToolCallResult and
        # produced a final assistant message.
        finals = [
            e for e in ctx.events if (isinstance(e, InvocationMessage) and e.origin.kind == "turn") and e.is_final
        ]
        assert any("Final summary" in f.text for f in finals)

        # Sub-agent was called twice (error + success); parent twice
        # (tool_call dispatch + final summary).
        assert ctx.sub_client.call_count == 2
        assert ctx.main_client.call_count == 2

        # Engine ends IDLE, no lingering paused_sub_agents.
        assert ctx.engine.state is EngineState.IDLE
        assert ctx.engine.turns.turn_state.paused_sub_agents == set()
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_sub_agent_uses_model_profile_stream_setting(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine
) -> None:
    """A sub-agent pinned to a streaming model profile should call its LLM with ``stream=True``."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[
            _sub_tool_call("explore with streaming"),
            MockResponse(text="Final summary after sub-agent."),
        ],
        sub_outcomes=[
            MockResponse(text="streamed sub-agent result"),
        ],
        sub_stream=True,
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        await ctx.wait_for_idle()

        assert ctx.sub_client.stream_flags == [True]
        assert ctx.main_client.stream_flags == [False, False]
        tool_results = [
            e
            for e in ctx.events
            if (isinstance(e, InvocationToolCallResult) and e.origin.kind == "turn") and e.tool_name == "Explore"
        ]
        assert tool_results and "streamed sub-agent result" in tool_results[-1].result
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_sub_agent_usage_updates_flow_before_parent_tool_finishes(
    tmp_path: Path,
    fast_sub_agent_controller: None,
    agent_engine,
    engine_services,
) -> None:
    """Sub-agent usage publishes live UsageUpdate events and cumulative card progress."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[
            _sub_tool_call("explore with live usage"),
            MockResponse(text="Parent saw sub-agent usage."),
        ],
        sub_outcomes=[
            MockResponse(
                tool_calls=[("sleep", "sleep-1", {"seconds": 1, "reason": "hold the sub-agent open"})],
                usage_details={"input_token_count": 10, "output_token_count": 2, "total_token_count": 12},
            ),
            MockResponse(
                tool_calls=[("sleep", "sleep-2", {"seconds": 0, "reason": "continue"})],
                usage_details={"input_token_count": 30, "output_token_count": 4, "total_token_count": 34},
            ),
            MockResponse(
                text="sub-agent result",
                usage_details={"input_token_count": 8, "output_token_count": 3, "total_token_count": 11},
            ),
        ],
        sub_builtins=["sleep"],
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))

        first_usage = (await ctx.wait_for_event(UsageUpdate))[0]
        assert isinstance(first_usage, UsageUpdate)
        assert first_usage.agent_profile == "Explore"
        assert first_usage.usage_source_id
        assert first_usage.usage_source_id != ctx.session_id
        assert first_usage.total_tokens == 12
        assert first_usage.total_session_tokens == 12
        assert not [
            e
            for e in ctx.events
            if (isinstance(e, InvocationToolCallResult) and e.origin.kind == "turn") and e.tool_name == "Explore"
        ]
        assert not [
            e for e in ctx.events if (isinstance(e, InvocationMessage) and e.origin.kind == "turn") and e.is_final
        ]

        await ctx.wait_for_idle()

        usage_updates = [e for e in ctx.events if isinstance(e, UsageUpdate)]
        assert any(
            e.agent_profile == "Explore"
            and e.usage_source_id == first_usage.usage_source_id
            and e.total_tokens == 34
            and e.total_session_tokens == 46
            for e in usage_updates
        )
        assert any(
            e.agent_profile == "Explore"
            and e.usage_source_id == first_usage.usage_source_id
            and e.total_tokens == 11
            and e.total_session_tokens == 57
            for e in usage_updates
        )

        progress = [e for e in ctx.events if (isinstance(e, InvocationProgress) and e.origin.kind == "sub_agent")]
        assert any(e.total_tokens == 12 and e.total_usage_tokens == 12 for e in progress)
        assert any(e.total_tokens == 34 and e.total_usage_tokens == 46 for e in progress)
        assert any(e.total_tokens == 11 and e.total_usage_tokens == 57 for e in progress)
        final_progress_index = next(
            i
            for i, e in enumerate(ctx.events)
            if (isinstance(e, InvocationProgress) and e.origin.kind == "sub_agent") and e.total_usage_tokens == 57
        )
        parent_result_index = next(
            i
            for i, e in enumerate(ctx.events)
            if (isinstance(e, InvocationToolCallResult) and e.origin.kind == "turn") and e.tool_name == "Explore"
        )
        assert final_progress_index < parent_result_index
        # The saved parent result keeps the run's final counters, so a restored card shows them too.
        [metadata] = [
            content.additional_properties[TOOL_RESULT_METADATA_KEY]
            for message in engine_services(ctx.engine).history.messages
            for content in message.contents
            if content.type == "function_result" and content.call_id == "call-1"
        ]
        assert metadata[SUB_AGENT_PROGRESS_METADATA_KEY] == {
            "tool_calls": 2,
            "context_tokens": 11,
            "usage_tokens": 57,
        }
    finally:
        await ctx.cleanup()


async def _narrated_sub_agent_ctx(tmp_path: Path, agent_engine) -> _SubAgentPipelineCtx:
    """A sub-agent that narrates one tool call, then answers."""
    return await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("explore"), MockResponse(text="Parent done.")],
        sub_outcomes=[
            MockResponse(
                text="Let me check the tree. ",
                tool_calls=[("sleep", "sleep-1", {"seconds": 0, "reason": "look around"})],
            ),
            MockResponse(text="The answer is 42."),
        ],
        sub_builtins=["sleep"],
        agent_engine=agent_engine,
    )


@pytest.mark.asyncio
async def test_parent_receives_the_final_segment_not_the_narration(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine
) -> None:
    """Text written between the sub-agent's tool calls stays in its transcript."""
    ctx = await _narrated_sub_agent_ctx(tmp_path, agent_engine)
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        await ctx.wait_for_idle()

        tool_result = _explore_result(ctx)
        assert tool_result.result == "The answer is 42."
        # The result is the final segment, so the transcript needs no declaration.
        assert SUB_AGENT_TRANSCRIPT_FINAL_TEXT_METADATA_KEY not in tool_result.metadata
        sent = [
            str(content.result)
            for messages, _options in ctx.main_client.call_history
            for message in messages
            for content in message.contents
            if content.type == "function_result" and content.call_id == "call-1"
        ]
        assert sent == ["The answer is 42."]
        assert [text for _index, text in _child_answer_segments(ctx)] == [
            "Let me check the tree. ",
            "The answer is 42.",
        ]
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_a_streamed_sub_agent_answer_shows_while_its_response_is_still_streaming(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("explore"), MockResponse(text="Parent done.")],
        sub_outcomes=[MockResponse(text="Looking it up.", chunk_size=8, chunk_delay=0)],
        sub_stream=True,
        agent_engine=agent_engine,
    )
    original = ctx.sub_client._stream_updates
    release = asyncio.Event()

    async def held(
        resp: MockResponse, model_id: str, finish_reason: FinishReasonLiteral | FinishReason
    ) -> AsyncIterator[ChatResponseUpdate]:
        held_once = False
        async for update in original(resp, model_id, finish_reason):
            yield update
            if update.text and not held_once:
                held_once = True
                await release.wait()

    monkeypatch.setattr(ctx.sub_client, "_stream_updates", held)
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        await wait_for(lambda: _child_answer_segments(ctx), timeout=ENGINE_TURN_TIMEOUT, description="the first chunk")
        # The rest of the response is still held: the first chunk went out as it arrived.
        assert [text for _index, text in _child_answer_segments(ctx)] == ["Looking "]
        assert not [e for e in ctx.events if isinstance(e, InvocationToolCallResult) and e.tool_name == "Explore"]
        release.set()
        await ctx.wait_for_idle()

        assert _explore_result(ctx).result == "Looking it up."
        assert [text for _index, text in _child_answer_segments(ctx)] == ["Looking it up."]
        deltas = [e for e in ctx.events if isinstance(e, InvocationContentDelta) and e.origin.kind == "sub_agent"]
        # One child invocation under the parent turn streamed them.
        assert len({e.origin.invocation_id for e in deltas}) == 1
        assert {e.origin.root.kind for e in deltas} == {"turn"}
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [("no_such_tool", "c-1", {}), ("sleep", "sleep-1", {})],
    ids=["unknown-tool", "invalid-arguments"],
)
async def test_streamed_narration_of_a_call_outside_the_tool_pipeline_reaches_the_transcript(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine, call: tuple[str, str, dict[str, Any]]
) -> None:
    """The kernel answers these calls without a tool start; the text streamed before them still shows once."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("explore"), MockResponse(text="Parent done.")],
        sub_outcomes=[
            MockResponse(text="Let me check the tree. ", tool_calls=[call]),
            MockResponse(text="The answer is 42."),
        ],
        sub_stream=True,
        sub_builtins=["sleep"],
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        await ctx.wait_for_idle()

        assert _explore_result(ctx).result == "The answer is 42."
        segments = _child_answer_segments(ctx)
        assert [text for _index, text in segments] == ["Let me check the tree. ", "The answer is 42."]
        assert segments[0][0] < ctx.events.index(_explore_result(ctx))
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("sub_stream", [True, False], ids=["streamed", "blocking"])
@pytest.mark.parametrize(
    "call",
    [("sleep", "sleep-1", {"seconds": 0}), ("no_such_tool", "c-1", {})],
    ids=["tool-start", "unknown-tool"],
)
async def test_narration_before_hosted_output_reaches_the_transcript_once(
    tmp_path: Path,
    fast_sub_agent_controller: None,
    agent_engine,
    call: tuple[str, str, dict[str, Any]],
    sub_stream: bool,
) -> None:
    """The narration precedes the next response's hosted output and shows once."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("explore"), MockResponse(text="Parent done.")],
        sub_outcomes=[
            MockResponse(text="Let me check the tree. ", tool_calls=[call]),
            HostedMockResponse(text="Here it is.", hosted=[hosted_image_result()]),
        ],
        sub_stream=sub_stream,
        sub_builtins=["sleep"],
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        await ctx.wait_for_idle()

        assert _explore_result(ctx).result == "Here it is."
        child_results = [
            (e.tool_name, e.provider_hosted)
            for e in ctx.events
            if isinstance(e, InvocationToolCallResult) and e.origin.kind == "sub_agent"
        ]
        assert (HostedToolFamily.IMAGE, True) in child_results
        assert (call[0] == "sleep") == (("sleep", False) in child_results)
        segments = _child_answer_segments(ctx)
        assert [text for _index, text in segments] == ["Let me check the tree. ", "Here it is."]
        hosted_start_at = next(
            i
            for i, e in enumerate(ctx.events)
            if isinstance(e, InvocationToolCallStart) and e.origin.kind == "sub_agent" and e.provider_hosted
        )
        assert segments[0][0] < hosted_start_at
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("sub_stream", [True, False], ids=["streamed", "blocking"])
async def test_text_before_a_hosted_call_ends_its_segment_before_the_call_starts(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine, sub_stream: bool
) -> None:
    """A card that shows each segment once it ends then shows that text above the call, as the response holds it."""
    # The provider's item id matches the landed call to the streamed one.
    hosted = Content.from_image_generation_tool_result(
        image_id="image-1",
        outputs=[Content.from_uri("data:image/png;base64,QUJD", media_type="image/png")],
        hosted_provider="openai",
        provider_item_id="ig_1",
        provider_phase="terminal",
        provider_status="completed",
    )
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("explore"), MockResponse(text="Parent done.")],
        sub_outcomes=[HostedMockResponse(lead_text="Drawing it.", hosted=[hosted], text="Here it is.")],
        sub_stream=sub_stream,
        agent_engine=agent_engine,
    )
    child: list[list[str]] = []

    async def record(event: InvocationContentDelta | InvocationContentFinished | InvocationToolCallStart) -> None:
        if event.origin.kind != "sub_agent":
            return
        if isinstance(event, InvocationToolCallStart):
            child.append(["start", event.tool_name])
        elif isinstance(event, InvocationContentFinished):
            child.append(["end", event.status])
        elif child and child[-1][0] == "text":
            child[-1][1] += event.text
        else:
            child.append(["text", event.text])

    for event_type in (InvocationContentDelta, InvocationContentFinished, InvocationToolCallStart):
        await ctx.bus.subscribe(event_type, record)
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        await ctx.wait_for_idle()

        assert child == [
            ["text", "Drawing it."],
            ["end", "completed"],
            ["start", HostedToolFamily.IMAGE],
            ["text", "Here it is."],
            ["end", "completed"],
        ]
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_narration_of_a_failed_pass_precedes_its_pause(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streamed text of a call that started no tool belongs to the pass that wrote it, not to the retry."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("explore"), MockResponse(text="Parent done.")],
        sub_outcomes=[
            MockResponse(text="Let me check the tree. ", tool_calls=[("no_such_tool", "c-1", {})]),
            MockResponse(text="The answer is 42."),
        ],
        sub_stream=True,
        agent_engine=agent_engine,
    )
    # The second response fails before it starts: the pass pauses after the text it streamed.
    fail_nested_before_response_on_nth(monkeypatch, 2, main=ctx.engine.current.loaded.bindings._response_validation)

    try:
        await ctx.bus.publish(UserMessage(text="go"))
        (paused,) = await ctx.wait_for_event(InvocationPaused)
        ((narrated_at, narration),) = _child_answer_segments(ctx)
        assert narration == "Let me check the tree. "
        assert narrated_at < ctx.events.index(paused)

        await ctx.bus.publish(InvocationRetryRequested(invocation_id=paused.origin.invocation_id))
        await ctx.wait_for_event(InvocationResumed)
        await ctx.wait_for_idle()

        assert _explore_result(ctx).result == "The answer is 42."
        # The retry continues after the answered call: the narration is never streamed again.
        assert [text for _index, text in _child_answer_segments(ctx)] == [
            "Let me check the tree. ",
            "The answer is 42.",
        ]
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_sub_agent_has_sleep_though_its_profile_omits_it(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine
) -> None:
    """Every agent has sleep: a child whose profile lists no builtin category still runs it."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("pause"), MockResponse(text="Parent done.")],
        sub_outcomes=[
            MockResponse(tool_calls=[("sleep", "sleep-1", {"seconds": 0, "reason": "pause"})]),
            MockResponse(text="back from the pause"),
        ],
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        await ctx.wait_for_idle()

        sent = ctx.sub_client.call_history[1][0]
        results = [
            content.result for message in sent for content in message.contents if content.type == "function_result"
        ]
        assert results == ["Slept for 0 seconds."]
        assert _explore_result(ctx).result == "back from the pause"
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_sub_agent_asks_its_extension_contexts_with_its_task(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child's reminders carry its own profile's extension contexts, asked with the task it was given."""
    install_file_extension(
        monkeypatch,
        "acme_task",
        "from chrys.extensions import Extension\nextension = Extension()\n\n"
        "@extension.context\nasync def task(ctx):\n    return f'Asked to: {ctx.prompt}'\n",
    )
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("inspect the tree"), MockResponse(text="Parent done.")],
        sub_outcomes=[MockResponse(text="fine")],
        sub_extensions=["acme_task"],
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        await ctx.wait_for_idle()

        ((sent, _options),) = ctx.sub_client.call_history
        assert "Asked to: inspect the tree" in (sent[-1].text or "")
        # The parent's profile enables no extension.
        parent_texts = [m.text or "" for messages, _ in ctx.main_client.call_history for m in messages]
        assert not any("<extension_context" in text for text in parent_texts)
    finally:
        await ctx.cleanup()


_SIGNING_HOOKS = (
    "from chrys.extensions import Extension, ToolError\nextension = Extension()\n\n"
    "@extension.before_tool\nasync def no_sleep(call):\n    raise ToolError('No sleeping.')\n\n"
    "@extension.after_tool\nasync def sign(call, result):\n    return f'{result.text} [acme]'\n"
)


async def test_a_sub_agent_runs_its_extension_tool_hooks_around_its_own_calls(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_file_extension(monkeypatch, "acme_sign", _SIGNING_HOOKS)
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("wait a bit"), MockResponse(text="Parent done.")],
        sub_outcomes=[
            MockResponse(tool_calls=[("sleep", "sleep-1", {"seconds": 0, "reason": "wait"})]),
            MockResponse(text="fine"),
        ],
        sub_builtins=["sleep"],
        sub_extensions=["acme_sign"],
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        await ctx.wait_for_idle()

        sent = [c.result for m in ctx.sub_client.call_history[-1][0] for c in m.contents if c.type == "function_result"]
        assert sent == ["Error: No sleeping. [acme]"]
        # The parent's own call is not the child's extensions' to see.
        parent = [
            c.result for m in ctx.main_client.call_history[-1][0] for c in m.contents if c.type == "function_result"
        ]
        assert parent == ["fine"]
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_declared_final_segment_reaches_the_parent_result_metadata(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine
) -> None:
    """A run that ends on hosted output returns everything it said and tells the transcript so."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("explore"), MockResponse(text="Parent done.")],
        sub_outcomes=[
            MockResponse(
                text="Let me check the tree. ",
                tool_calls=[("sleep", "sleep-1", {"seconds": 0, "reason": "look around"})],
            ),
            HostedMockResponse(text="", hosted=[hosted_image_result()]),
        ],
        sub_builtins=["sleep"],
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        await ctx.wait_for_idle()

        assert any(
            isinstance(e, InvocationToolCallResult) and e.origin.kind == "sub_agent" and e.provider_hosted
            for e in ctx.events
        )
        tool_result = _explore_result(ctx)
        assert tool_result.result == "Let me check the tree. "
        assert tool_result.metadata[SUB_AGENT_TRANSCRIPT_FINAL_TEXT_METADATA_KEY] == ""
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_pause_then_user_retry_recovers(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine, *, engine_services
) -> None:
    """Non-retryable error exhausts auto-retry budget → InvocationPaused.
    User publishes InvocationRetryRequested → sub-agent is re-run and succeeds;
    the parent receives the successful tool_result and completes normally."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[
            _sub_tool_call("try hard"),
            MockResponse(text="All good — recovered."),
        ],
        # FrameworkBoom isn't retryable — ``StreamRetryLoop`` re-raises
        # on the first occurrence without consuming the retry budget.
        # So one boom pauses, then the user's retry triggers a fresh
        # ``agent.run()`` that succeeds.
        sub_outcomes=[
            FrameworkBoom("initial boom"),
            MockResponse(text="recovered result"),
        ],
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        paused = await ctx.wait_for_event(InvocationPaused)
        assert paused[0].reason == "framework_exc"
        assert "initial boom" in paused[0].last_error
        invocation_id = paused[0].origin.invocation_id

        # FSM must be AWAITING_SUB_AGENTS while paused.
        assert ctx.engine.state is EngineState.AWAITING_SUB_AGENTS
        assert invocation_id in ctx.engine.turns.turn_state.paused_sub_agents

        # User clicks Retry on the paused card.
        await ctx.bus.publish(InvocationRetryRequested(invocation_id=invocation_id))
        await ctx.wait_for_event(InvocationResumed)
        await ctx.wait_for_idle()

        # Sub-agent called 2x (1 boom + 1 success); parent 2x.
        assert ctx.sub_client.call_count == 2
        assert ctx.main_client.call_count == 2

        finals = [
            e for e in ctx.events if (isinstance(e, InvocationMessage) and e.origin.kind == "turn") and e.is_final
        ]
        assert any("recovered" in f.text for f in finals)

        # Tool result delivered to parent carries the success text.
        tool_results = [
            e
            for e in ctx.events
            if (isinstance(e, InvocationToolCallResult) and e.origin.kind == "turn") and e.tool_name == "Explore"
        ]
        assert tool_results and "recovered result" in tool_results[-1].result

        # FSM back to IDLE, paused set empty, no awaiting marker.
        assert ctx.engine.state is EngineState.IDLE
        assert ctx.engine.turns.turn_state.paused_sub_agents == set()
        kinds = [
            m.additional_properties.get(HistoryMarkerKind.KEY) for m in engine_services(ctx.engine).history.messages
        ]
        assert HistoryMarkerKind.AWAITING_SUB_AGENTS not in kinds
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_pause_retry_continues_after_completed_sub_agent_tool(
    tmp_path: Path,
    fast_sub_agent_controller: None,
    agent_engine,
) -> None:
    """A paused sub-agent retry should continue after completed inner tool work."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[
            _sub_tool_call("try with tool"),
            MockResponse(text="Parent saw resumed sub-agent."),
        ],
        sub_outcomes=[
            MockResponse(tool_calls=[("sleep", "sleep-1", {"seconds": 0, "reason": "integration test"})]),
            FrameworkBoom("failed after sleep"),
            MockResponse(text="continued after sleep"),
        ],
        sub_builtins=["sleep"],
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        paused = await ctx.wait_for_event(InvocationPaused)
        assert "failed after sleep" in paused[0].last_error

        await ctx.bus.publish(InvocationRetryRequested(invocation_id=paused[0].origin.invocation_id))
        await ctx.wait_for_event(InvocationResumed)
        await ctx.wait_for_idle()

        assert ctx.sub_client.call_count == 3
        retry_messages = ctx.sub_client.call_history[2][0]
        user_texts = [m.text for m in retry_messages if m.role == "user"]
        assert len(user_texts) == 1
        assert user_texts[0].startswith("try with tool")
        assert any(
            content.type == "function_result" and content.call_id == "sleep-1"
            for message in retry_messages
            for content in message.contents
        )

        tool_results = [
            e
            for e in ctx.events
            if (isinstance(e, InvocationToolCallResult) and e.origin.kind == "turn") and e.tool_name == "Explore"
        ]
        assert tool_results and "continued after sleep" in tool_results[-1].result
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_pause_retry_from_completed_work_still_compacts_the_child_turn(
    tmp_path: Path,
    fast_sub_agent_controller: None,
    agent_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry that continues from the child's own history sends no new input,
    so the stored opener is the last user message the reminder middleware
    rebuilds.  Compaction must still resolve that turn and drop its work
    instead of silently sending an over-trigger request."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[
            _sub_tool_call("gather evidence"),
            MockResponse(text="Parent saw the compacted child finish."),
        ],
        sub_outcomes=[
            MockResponse(tool_calls=[("sleep", "sleep-1", {"seconds": 0, "reason": "evidence " * 30_000})]),
            MockResponse(text="finished from the note"),
        ],
        sub_builtins=["sleep"],
        sub_compaction=CompactionConfig(),
        sub_max_context_tokens=30_000,
        sub_max_output_tokens=3_000,
        agent_engine=agent_engine,
    )
    # The first pass fails before its second request reaches the client, so
    # compaction first sees the oversized turn on the retry.
    fail_nested_before_response_on_nth(monkeypatch, 2, main=ctx.engine.current.loaded.bindings._response_validation)

    async def generate(self: LastWordsGenerator, *_args: Any, **_kwargs: Any) -> str:
        return "Collected the evidence with one sleep call. Next, report the findings."

    generate_call = create_autospec(LastWordsGenerator.generate, side_effect=generate)
    monkeypatch.setattr(LastWordsGenerator, "generate", generate_call)
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        (paused,) = await ctx.wait_for_event(InvocationPaused)
        assert generate_call.call_count == 0

        await ctx.bus.publish(InvocationRetryRequested(invocation_id=paused.origin.invocation_id))
        await ctx.wait_for_event(InvocationResumed)
        await ctx.wait_for_idle()

        assert generate_call.call_count == 1
        assert ctx.sub_client.call_count == 2
        retry_messages = ctx.sub_client.call_history[1][0]
        assert not any(
            content.call_id == "sleep-1"
            for message in retry_messages
            for content in message.contents
            if content.type in ("function_call", "function_result")
        )
        (opener,) = [m for m in retry_messages if m.role == "user"]
        assert opener.text.startswith("gather evidence")
        assert "[LAST_WORDS] " in opener.text
        assert "Collected the evidence with one sleep call." in opener.text
        assert _explore_result(ctx).result == "finished from the note"
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_repeated_pause_retry_preserves_sub_agent_tool_order(
    tmp_path: Path,
    fast_sub_agent_controller: None,
    agent_engine,
) -> None:
    """Repeated sub-agent retries should append new tool work after earlier completed work."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[
            _sub_tool_call("try multiple tools"),
            MockResponse(text="Parent saw repeated resumed sub-agent."),
        ],
        sub_outcomes=[
            MockResponse(tool_calls=[("sleep", "sleep-1", {"seconds": 0, "reason": "first"})]),
            FrameworkBoom("failed after first sleep"),
            MockResponse(tool_calls=[("sleep", "sleep-2", {"seconds": 0, "reason": "second"})]),
            FrameworkBoom("failed after second sleep"),
            MockResponse(text="continued after both sleeps"),
        ],
        sub_builtins=["sleep"],
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        first_pause = (await ctx.wait_for_event(InvocationPaused))[0]
        assert "failed after first sleep" in first_pause.last_error

        await ctx.bus.publish(InvocationRetryRequested(invocation_id=first_pause.origin.invocation_id))
        second_pause = (await ctx.wait_for_event(InvocationPaused, min_count=2))[1]
        assert "failed after second sleep" in second_pause.last_error

        await ctx.bus.publish(InvocationRetryRequested(invocation_id=second_pause.origin.invocation_id))
        await ctx.wait_for_event(InvocationResumed, min_count=2)
        await ctx.wait_for_idle()

        assert ctx.sub_client.call_count == 5
        final_messages = ctx.sub_client.call_history[4][0]
        user_texts = [m.text for m in final_messages if m.role == "user"]
        assert len(user_texts) == 1
        assert user_texts[0].startswith("try multiple tools")

        result_call_ids = [
            content.call_id
            for message in final_messages
            for content in message.contents
            if content.type == "function_result"
        ]
        assert result_call_ids == ["sleep-1", "sleep-2"]

        tool_results = [
            e
            for e in ctx.events
            if (isinstance(e, InvocationToolCallResult) and e.origin.kind == "turn") and e.tool_name == "Explore"
        ]
        assert tool_results and "continued after both sleeps" in tool_results[-1].result
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_store_true_pause_retry_replays_local_history_without_stale_conversation(
    tmp_path: Path,
    fast_sub_agent_controller: None,
    agent_engine,
) -> None:
    """Responses ``store: true`` retries must replay repaired local history, not stale provider state."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[
            _sub_tool_call("try with stored tool"),
            MockResponse(text="Parent saw stored resumed sub-agent."),
        ],
        sub_outcomes=[
            MockResponse(
                tool_calls=[("sleep", "sleep-store-1", {"seconds": 0, "reason": "stored first"})],
                conversation_id="resp_partial_sub_1",
            ),
            FrameworkBoom("failed after stored sleep"),
            MockResponse(text="continued after stored sleep", conversation_id="resp_recovered_sub_1"),
        ],
        sub_builtins=["sleep"],
        sub_responses_store=True,
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        paused = await ctx.wait_for_event(InvocationPaused)
        assert "failed after stored sleep" in paused[0].last_error

        # The failed in-flight tool-loop call had a provider continuation id.
        assert ctx.sub_client.call_history[0][1]["store"] is True
        assert "conversation_id" not in ctx.sub_client.call_history[0][1]
        assert ctx.sub_client.call_history[1][1]["conversation_id"] == "resp_partial_sub_1"

        await ctx.bus.publish(InvocationRetryRequested(invocation_id=paused[0].origin.invocation_id))
        await ctx.wait_for_event(InvocationResumed)
        await ctx.wait_for_idle()

        assert ctx.sub_client.call_count == 3
        retry_messages, retry_options = ctx.sub_client.call_history[2]
        assert retry_options["store"] is True
        assert "conversation_id" not in retry_options

        user_texts = [m.text for m in retry_messages if m.role == "user"]
        assert len(user_texts) == 1
        assert user_texts[0].startswith("try with stored tool")
        assert any(
            content.type == "function_result" and content.call_id == "sleep-store-1"
            for message in retry_messages
            for content in message.contents
        )

        tool_results = [
            e
            for e in ctx.events
            if (isinstance(e, InvocationToolCallResult) and e.origin.kind == "turn") and e.tool_name == "Explore"
        ]
        assert tool_results and "continued after stored sleep" in tool_results[-1].result
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_streaming_store_true_pause_retry_replays_completed_tool_work(
    tmp_path: Path,
    fast_sub_agent_controller: None,
    agent_engine,
) -> None:
    """Streamed Responses ``store: true`` retries must preserve completed inner tool work."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[
            _sub_tool_call("try streamed stored tool"),
            MockResponse(text="Parent saw streamed stored resumed sub-agent."),
        ],
        sub_outcomes=[
            MockResponse(
                tool_calls=[("sleep", "sleep-stream-store-1", {"seconds": 0, "reason": "streamed stored"})],
                conversation_id="resp_stream_partial_sub_1",
                chunk_delay=0,
            ),
            FrameworkBoom("failed after streamed stored sleep"),
            MockResponse(
                text="continued after streamed stored sleep",
                conversation_id="resp_stream_recovered_sub_1",
                chunk_delay=0,
            ),
        ],
        sub_stream=True,
        sub_builtins=["sleep"],
        sub_responses_store=True,
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        paused = await ctx.wait_for_event(InvocationPaused)
        assert "failed after streamed stored sleep" in paused[0].last_error

        assert ctx.sub_client.stream_flags == [True, True]
        assert ctx.sub_client.call_history[1][1]["conversation_id"] == "resp_stream_partial_sub_1"

        await ctx.bus.publish(InvocationRetryRequested(invocation_id=paused[0].origin.invocation_id))
        await ctx.wait_for_event(InvocationResumed)
        await ctx.wait_for_idle()

        assert ctx.sub_client.stream_flags == [True, True, True]
        retry_messages, retry_options = ctx.sub_client.call_history[2]
        assert retry_options["store"] is True
        assert "conversation_id" not in retry_options

        user_texts = [m.text for m in retry_messages if m.role == "user"]
        assert len(user_texts) == 1
        assert user_texts[0].startswith("try streamed stored tool")
        assert any(
            content.type == "function_result" and content.call_id == "sleep-stream-store-1"
            for message in retry_messages
            for content in message.contents
        )

        tool_results = [
            e
            for e in ctx.events
            if (isinstance(e, InvocationToolCallResult) and e.origin.kind == "turn") and e.tool_name == "Explore"
        ]
        assert tool_results and "continued after streamed stored sleep" in tool_results[-1].result
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_store_true_repeated_pause_retry_clears_each_stale_conversation(
    tmp_path: Path,
    fast_sub_agent_controller: None,
    agent_engine,
) -> None:
    """Each pause after a Responses stored tool-loop failure must clear the newest provider id."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[
            _sub_tool_call("try multiple stored tools"),
            MockResponse(text="Parent saw repeated stored resumed sub-agent."),
        ],
        sub_outcomes=[
            MockResponse(
                tool_calls=[("sleep", "sleep-store-1", {"seconds": 0, "reason": "first stored"})],
                conversation_id="resp_partial_sub_1",
            ),
            FrameworkBoom("failed after first stored sleep"),
            MockResponse(
                tool_calls=[("sleep", "sleep-store-2", {"seconds": 0, "reason": "second stored"})],
                conversation_id="resp_partial_sub_2",
            ),
            FrameworkBoom("failed after second stored sleep"),
            MockResponse(text="continued after both stored sleeps", conversation_id="resp_recovered_sub_2"),
        ],
        sub_builtins=["sleep"],
        sub_responses_store=True,
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        first_pause = (await ctx.wait_for_event(InvocationPaused))[0]
        assert "failed after first stored sleep" in first_pause.last_error
        assert ctx.sub_client.call_history[1][1]["conversation_id"] == "resp_partial_sub_1"

        await ctx.bus.publish(InvocationRetryRequested(invocation_id=first_pause.origin.invocation_id))
        second_pause = (await ctx.wait_for_event(InvocationPaused, min_count=2))[1]
        assert "failed after second stored sleep" in second_pause.last_error
        assert "conversation_id" not in ctx.sub_client.call_history[2][1]
        assert ctx.sub_client.call_history[3][1]["conversation_id"] == "resp_partial_sub_2"

        await ctx.bus.publish(InvocationRetryRequested(invocation_id=second_pause.origin.invocation_id))
        await ctx.wait_for_event(InvocationResumed, min_count=2)
        await ctx.wait_for_idle()

        assert ctx.sub_client.call_count == 5
        final_messages, final_options = ctx.sub_client.call_history[4]
        assert final_options["store"] is True
        assert "conversation_id" not in final_options

        user_texts = [m.text for m in final_messages if m.role == "user"]
        assert len(user_texts) == 1
        assert user_texts[0].startswith("try multiple stored tools")
        result_call_ids = [
            content.call_id
            for message in final_messages
            for content in message.contents
            if content.type == "function_result"
        ]
        assert result_call_ids == ["sleep-store-1", "sleep-store-2"]

        tool_results = [
            e
            for e in ctx.events
            if (isinstance(e, InvocationToolCallResult) and e.origin.kind == "turn") and e.tool_name == "Explore"
        ]
        assert tool_results and "continued after both stored sleeps" in tool_results[-1].result
    finally:
        await ctx.cleanup()


def _retried_pass_settled(ctx: _SubAgentPipelineCtx) -> list[Event]:
    """Pauses and the parent's Explore result, in order: a retried pass ends in one or the other."""
    return [
        e
        for e in ctx.events
        if isinstance(e, InvocationPaused)
        or (isinstance(e, InvocationToolCallResult) and e.origin.kind == "turn" and e.tool_name == "Explore")
    ]


def _options_tokens(ctx: _SubAgentPipelineCtx) -> list[object]:
    return [options.get("continuation_token") for _messages, options in ctx.sub_client.call_history]


@pytest.mark.asyncio
@pytest.mark.parametrize("after_automatic_retry", [False, True], ids=["first-request", "after-automatic-retry"])
async def test_retry_that_polls_an_empty_background_response_never_creates_its_hosted_work_again(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine, after_automatic_retry: bool
) -> None:
    """Retry polls the response the failed pass left running; when the poll comes back empty, a new request would
    run the response's hosted call again, so the sub-agent pauses again instead. An automatic retry before the
    response started replaces the request options, and the poll still counts as resuming that response."""
    leading: list[MockResponse | BaseException] = (
        [ConnectionResetError("connection reset by peer")] if after_automatic_retry else []
    )
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("file the issue"), MockResponse(text="Parent saw the failure.")],
        sub_outcomes=[
            *leading,
            HostedMockResponse(
                hosted=[Content.from_mcp_server_tool_call("mc1", "create_issue")],
                continuation_token={"response_id": "resp_sub_1"},
                error_after_hosted=FrameworkBoom("stream dropped"),
            ),
            MockResponse(text=""),
            MockResponse(text="created again"),
        ],
        sub_stream=True,
        sub_responses_store=True,
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        (first_pause,) = await ctx.wait_for_event(InvocationPaused)

        await ctx.bus.publish(InvocationRetryRequested(invocation_id=first_pause.origin.invocation_id))
        await wait_for(
            lambda: len(_retried_pass_settled(ctx)) >= 2,
            timeout=ENGINE_TURN_TIMEOUT,
            description="the retried pass settled",
        )
        second_pause = _retried_pass_settled(ctx)[1]
        assert isinstance(second_pause, InvocationPaused)
        assert _options_tokens(ctx) == [*([None] if after_automatic_retry else []), None, {"response_id": "resp_sub_1"}]

        await ctx.bus.publish(InvocationAbortRequested(invocation_id=second_pause.origin.invocation_id))
        await ctx.wait_for_event(InvocationAborted)
        await ctx.wait_for_idle()
        assert ctx.sub_client.call_count == len(leading) + 2
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_a_retry_after_an_ended_background_response_keeps_its_transient_retries(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine
) -> None:
    """The poll that ended the background response settled its hosted work: the user's Retry sends a new request,
    and a connection failure before any new hosted work is retried as usual."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("file the issue"), MockResponse(text="Parent done.")],
        sub_outcomes=[
            HostedMockResponse(
                hosted=[Content.from_mcp_server_tool_call("mc1", "create_issue")],
                continuation_token={"response_id": "resp_sub_1"},
                error_after_hosted=ConnectionResetError("connection reset by peer"),
            ),
            # The automatic retry polls the response, which ends empty: the pass pauses.
            MockResponse(text=""),
            ConnectionResetError("connection reset by peer"),
            MockResponse(text="Filed the issue."),
        ],
        sub_stream=True,
        sub_responses_store=True,
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        (first_pause,) = await ctx.wait_for_event(InvocationPaused)
        assert _options_tokens(ctx) == [None, {"response_id": "resp_sub_1"}]

        await ctx.bus.publish(InvocationRetryRequested(invocation_id=first_pause.origin.invocation_id))
        await wait_for(
            lambda: len(_retried_pass_settled(ctx)) >= 2,
            timeout=ENGINE_TURN_TIMEOUT,
            description="the retried pass settled",
        )
        assert not isinstance(_retried_pass_settled(ctx)[1], InvocationPaused)
        await ctx.wait_for_idle()

        assert _explore_result(ctx).result == "Filed the issue."
        assert _options_tokens(ctx) == [None, {"response_id": "resp_sub_1"}, None, None]
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_pause_then_user_abort_returns_error_to_parent(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine
) -> None:
    """User abort on a paused sub-agent: the tool call returns an
    ``Error:`` string to the parent, the parent produces a final
    response acknowledging the failure, and the run completes IDLE
    with no dangling paused state."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[
            _sub_tool_call("explore"),
            MockResponse(text="Acknowledged failure."),
        ],
        sub_outcomes=[
            FrameworkBoom("cannot connect to service"),
            FrameworkBoom("cannot connect to service"),
        ],
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        paused = await ctx.wait_for_event(InvocationPaused)
        invocation_id = paused[0].origin.invocation_id

        # User aborts.
        await ctx.bus.publish(InvocationAbortRequested(invocation_id=invocation_id))
        aborted = await ctx.wait_for_event(InvocationAborted)
        assert aborted[0].origin.invocation_id == invocation_id
        await ctx.wait_for_idle()

        # Parent tool result is an Error: string carrying the failure.
        tool_results = [
            e
            for e in ctx.events
            if (isinstance(e, InvocationToolCallResult) and e.origin.kind == "turn") and e.tool_name == "Explore"
        ]
        assert tool_results, "Parent never received a tool_result for the sub-agent"
        result_text = tool_results[-1].result
        assert result_text.startswith("Error: sub-agent 'Explore' aborted by user")
        assert "cannot connect to service" in result_text

        # Parent proceeded to its final response.
        finals = [
            e for e in ctx.events if (isinstance(e, InvocationMessage) and e.origin.kind == "turn") and e.is_final
        ]
        assert any("Acknowledged" in f.text for f in finals)

        # Engine terminal state clean.
        assert ctx.engine.state is EngineState.IDLE
        assert ctx.engine.turns.turn_state.paused_sub_agents == set()
    finally:
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_cascade_abort_via_user_interrupt(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine, *, engine_services
) -> None:
    """Global interrupt while a sub-agent is paused: the engine cascades,
    the sub-agent's controller emits InvocationCascadeAborted, and the
    parent run transitions to INTERRUPTED with an interrupted marker."""
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[
            _sub_tool_call("run forever"),
            # The parent should NOT get a chance to produce this — the
            # interrupt unwinds before the main agent runs again.
            MockResponse(text="Should not appear."),
        ],
        sub_outcomes=[
            FrameworkBoom("bad auth"),
            FrameworkBoom("bad auth"),
        ],
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        paused = await ctx.wait_for_event(InvocationPaused)
        invocation_id = paused[0].origin.invocation_id
        assert ctx.engine.state is EngineState.AWAITING_SUB_AGENTS

        # Global UserInterrupt — cascades to every live sub-agent.
        await ctx.bus.publish(UserInterrupt())
        cascades = await ctx.wait_for_event(InvocationCascadeAborted)
        assert any(ev.origin.invocation_id == invocation_id for ev in cascades)
        await ctx.wait_for_idle()

        # Parent never produced the final text; it was interrupted.
        finals = [
            e for e in ctx.events if (isinstance(e, InvocationMessage) and e.origin.kind == "turn") and e.is_final
        ]
        assert not any("Should not appear" in f.text for f in finals)

        # Interrupted marker in history; no awaiting marker left over.
        kinds = [
            m.additional_properties.get(HistoryMarkerKind.KEY) for m in engine_services(ctx.engine).history.messages
        ]
        assert HistoryMarkerKind.INTERRUPTED in kinds
        assert HistoryMarkerKind.AWAITING_SUB_AGENTS not in kinds

        # FSM in INTERRUPTED, paused set drained.
        assert ctx.engine.state is EngineState.INTERRUPTED
        assert ctx.engine.turns.turn_state.paused_sub_agents == set()
    finally:
        await ctx.cleanup()


async def test_background_commands_a_sub_agent_started_stop_when_it_finishes(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine
) -> None:
    # PowerShell runs a quoted program path only through its call operator.
    prefix = "& " if sys.platform == "win32" else ""
    command = f'{prefix}"{sys.executable}" -c "import time; time.sleep(60)"'
    shell = (get_platform().shell.name, "s-1", {"command": command, "reason": "serve", "timeout": 1})
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("serve"), MockResponse(text="done")],
        sub_outcomes=[MockResponse(tool_calls=[shell]), MockResponse(text="Served.")],
        sub_builtins=["shell"],
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        [request] = await ctx.wait_for_event(ApprovalRequest)
        await ctx.bus.publish(ApprovalResponse(request_id=request.request_id, approved=True))
        await ctx.wait_for_idle()

        result = _explore_result(ctx).result
        assert re.fullmatch(
            r"Served\.\n\n\[Background commands this sub-agent started were stopped when it finished: "
            r"\S+ \(.*time\.sleep\(60\).*\)\]",
            result,
        ), result
        # The main agent never had a command of its own.
        tasks = ctx.engine.session.background_tasks
        assert tasks is not None
        assert tasks.running == []
        assert tasks.unreported(MAIN_SCOPE) == []
        assert tasks.reminder(get_platform().shell.name) is None
    finally:
        await ctx.cleanup()


async def test_an_interrupted_sub_agents_background_commands_have_ended_when_the_turn_does(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine
) -> None:
    # A command that ignores SIGTERM holds its stop for the kill grace, the window a rollback right after could hit.
    prefix = "& " if sys.platform == "win32" else ""
    script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    command = f'{prefix}"{sys.executable}" -c "{script}"'
    shell = (get_platform().shell.name, "s-1", {"command": command, "reason": "serve", "timeout": 1})
    hold = ("sleep", "sleep-1", {"seconds": 600, "reason": "wait for the server"})
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("serve"), MockResponse(text="Should not appear.")],
        sub_outcomes=[MockResponse(tool_calls=[shell]), MockResponse(tool_calls=[hold])],
        sub_builtins=["shell", "sleep"],
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        [request] = await ctx.wait_for_event(ApprovalRequest)
        await ctx.bus.publish(ApprovalResponse(request_id=request.request_id, approved=True))
        await wait_for(
            lambda: any(isinstance(e, InvocationToolCallStart) and e.tool_name == "sleep" for e in ctx.events),
            description="the sub-agent sleeps beside its background command",
        )
        tasks = ctx.engine.session.background_tasks
        assert tasks is not None
        [task] = tasks.running

        await ctx.bus.publish(UserInterrupt())
        await ctx.wait_for_idle()

        assert task.ended.is_set()
        assert task.outcome == "stopped when its agent finished"
    finally:
        await ctx.cleanup()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows stops a command by killing its Job at once")
async def test_an_interrupt_while_a_finished_sub_agents_commands_stop_keeps_it_completed(
    tmp_path: Path, fast_sub_agent_controller: None, agent_engine, *, engine_services
) -> None:
    script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    command = f'"{sys.executable}" -c "{script}"'
    shell = (get_platform().shell.name, "s-1", {"command": command, "reason": "serve", "timeout": 1})
    ctx = await _make_ctx(
        tmp_path,
        main_outcomes=[_sub_tool_call("serve"), MockResponse(text="Should not appear.")],
        sub_outcomes=[MockResponse(tool_calls=[shell]), MockResponse(text="Served.")],
        sub_builtins=["shell"],
        agent_engine=agent_engine,
    )
    try:
        await ctx.bus.publish(UserMessage(text="go"))
        [request] = await ctx.wait_for_event(ApprovalRequest)
        await ctx.bus.publish(ApprovalResponse(request_id=request.request_id, approved=True))
        tasks = ctx.engine.session.background_tasks
        assert tasks is not None
        # The command ignores SIGTERM, so it holds the finished sub-agent's call for the kill grace.
        await wait_for(
            lambda: any(task.stopped_by == "agent_finished" for task in tasks.running),
            timeout=ENGINE_TURN_TIMEOUT,
            description="the finished sub-agent waits for its command to stop",
        )

        await ctx.bus.publish(UserInterrupt())
        await ctx.wait_for_idle()

        assert tasks.running == []
        session_dir = ctx.engine.session.session_dir
        assert session_dir is not None
        [audit] = (session_dir / "sub_agents" / "sessions").glob("*.json")
        assert json.loads(audit.read_text(encoding="utf-8"))["meta"]["status"] == "completed"
        # The interrupted result still points at that record, so a reopened session replays the run.
        [metadata] = [
            content.additional_properties[TOOL_RESULT_METADATA_KEY]
            for message in engine_services(ctx.engine).history.messages
            for content in message.contents
            if content.type == "function_result" and content.call_id == "call-1"
        ]
        assert metadata["sub_agent_log_file"] == audit.name
        assert metadata["sub_agent_audit_complete"] is True
    finally:
        await ctx.cleanup()
