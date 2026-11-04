# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared infrastructure for pipeline integration tests.

Provides:
- Deterministic test tools (echo, concat, uppercase, guarded_echo)
- PipelineTestContext for engine lifecycle management
- create_test_engine() factory that wires up everything with mocks
- Event/session extraction helpers
- Wait helpers for async test coordination
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import itertools
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Annotated, Any

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ApprovalRequest,
    ApprovalResponse,
    ApprovalReviewed,
    Event,
    InvocationContentDelta,
    InvocationMessage,
    InvocationToolCallResult,
    InvocationToolCallStart,
    SessionReady,
    UserInterrupt,
    UserMessage,
    UserRetry,
)
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workspace import Workspace
from chrys.kernel import FunctionTool
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.engine import AgentEngine
from chrys.service.agent_middleware.response_validation import ResponseValidationMiddleware
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    SkillsConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from tests.support.phase4_stubs import StubLastWordsGenerator
from tests.support.scripted_clients import HostedMockChatClient
from tests.support.tool_packs import install_tools
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from chrys.kernel.middleware import ChatContext


def make_mock_settings_and_registry(
    *,
    stream: bool = False,
    max_context_tokens: int = 100_000,
) -> tuple[Settings, ModelProfileRegistry]:
    """Build a Settings + ModelProfileRegistry pair pre-wired with a mock profile.

    Replaces the old ``Settings(provider="mock", stream=..., max_context_tokens=...)``
    pattern now that those fields live on ``ModelProfile``.  Tests that
    monkey-patch ``create_client`` should pass both back to ``AgentEngine``.
    Advisory workspace scans are disabled: they have independent disk-I/O
    deadlines and are covered by the workspace-change tests, not mock turns.
    """
    registry = ModelProfileRegistry()
    registry.register(
        ModelProfile(
            id="mock-profile",
            name="mock",
            provider="mock",
            model_id="mock",
            stream=stream,
            max_context_tokens=max_context_tokens,
        )
    )
    return Settings(model_profile="mock-profile", workspace_change_notice=False), registry


# ---------------------------------------------------------------------------
# Test tools — deterministic, no subprocess, fast
# ---------------------------------------------------------------------------


def _echo(message: Annotated[str, "Message to echo"]) -> str:
    return f"echo: {message}"


def _concat(a: Annotated[str, "First string"], b: Annotated[str, "Second string"]) -> str:
    return f"concat: {a}{b}"


def _uppercase(text: Annotated[str, "Text to uppercase"]) -> str:
    return f"upper: {text.upper()}"


echo_tool = FunctionTool(func=_echo, name="echo", description="Echo a message")
concat_tool = FunctionTool(func=_concat, name="concat", description="Concatenate strings")
uppercase_tool = FunctionTool(func=_uppercase, name="uppercase", description="Uppercase text")
guarded_tool = FunctionTool(func=_echo, name="guarded_echo", description="Echo with approval")

ALL_TEST_TOOLS = [echo_tool, concat_tool, uppercase_tool, guarded_tool]


# ---------------------------------------------------------------------------
# PipelineTestContext
# ---------------------------------------------------------------------------


@dataclass
class PipelineTestContext:
    """Holds all components created by create_test_engine."""

    engine: AgentEngine
    bus: EventBus
    events: list[Event]
    mock_client: MockChatClient
    store: JsonFileStateStore
    session_id: str
    _create_client_patcher: pytest.MonkeyPatch | None = field(repr=False, default=None)

    async def cleanup(self) -> None:
        """Shut down engine and restore monkey-patches."""
        await self.engine.shutdown()
        if self._create_client_patcher is not None:
            self._create_client_patcher.undo()

    async def send_message(self, text: str) -> None:
        """Publish a UserMessage and wait for the engine to finish."""
        stale_task = self._completed_run_task()
        await self.bus.publish(UserMessage(text=text))
        await wait_for_idle(self, stale_task=stale_task)

    async def send_interrupt(self) -> None:
        """Publish a UserInterrupt."""
        await self.bus.publish(UserInterrupt())

    async def send_retry(self) -> None:
        """Publish a UserRetry and wait for engine to finish."""
        stale_task = self._completed_run_task()
        await self.bus.publish(UserRetry())
        await wait_for_idle(self, stale_task=stale_task)

    def _completed_run_task(self) -> asyncio.Task[None] | None:
        """Return the lingering completed run task, if any, at publish time.

        A task that is still running is NOT stale: a message published mid-run
        is consumed by that task (injection), so waiting on it stays correct.
        """
        task = self.engine.turns.turn_state.lease.run_task
        return task if task is not None and task.done() else None

    async def approve(self, request_id: str, reason: str = "") -> None:
        """Publish an ApprovalResponse(approved=True)."""
        await self.bus.publish(ApprovalResponse(request_id=request_id, approved=True, reason=reason))

    async def reject(self, request_id: str, reason: str = "") -> None:
        """Publish an ApprovalResponse(approved=False)."""
        await self.bus.publish(ApprovalResponse(request_id=request_id, approved=False, reason=reason))

    async def get_session_messages(self) -> list[dict[str, Any]]:
        """Load raw session messages from the state store."""
        raw = await self.store.load_session_raw(self.session_id)
        return raw or []


# ---------------------------------------------------------------------------
# Engine factory
# ---------------------------------------------------------------------------


async def create_test_engine(
    responses: list[MockResponse],
    tmp_path: Path,
    *,
    workspace: Workspace | None = None,
    approval_default: str = "auto",
    approval_overrides: dict[str, str] | None = None,
    stream: bool = False,
    tools: list[FunctionTool] | None = None,
    skills: SkillsConfig | None = None,
    compaction: CompactionConfig | None = None,
    max_context_tokens: int = 100_000,
    max_transient_retries: int | None = None,
    surface: SessionSurface | None = None,
    extensions: list[str] | None = None,
    builtins: list[str] | None = None,
) -> PipelineTestContext:
    """Create a fully wired mock engine without advisory workspace scans.

    Args:
        responses: Scripted LLM responses; a ``HostedMockResponse`` adds provider-hosted output.
        tmp_path: Temporary directory for session storage.
        workspace: Explicit workspace (defaults to tmp_path, never the checkout).
        approval_default: Approval policy ("auto", "require", "skip").
        stream: Whether to use streaming mode.
        tools: Custom tools to use (defaults to ALL_TEST_TOOLS).
        skills: Runtime skill-provider configuration for the built agent.
        compaction: Custom compaction config (default: disabled).
        max_context_tokens: Model context window size.
        surface: The launch surface a turn records on its session.
        extensions: The Python extensions the profile enables, loaded as in production.
        builtins: First-party packs the profile enables, created as in production beside the test tools.

    Returns:
        PipelineTestContext ready for test use.
    """
    events: list[Event] = []

    async def _collect(event: Event) -> None:
        events.append(event)

    bus = EventBus()
    # Subscribe to all event types we care about
    # ApprovalResponse, ApprovalReviewed, and UserInterrupt are collected so
    # wait_for_idle can tell a run stalled on user approval (request without
    # response, not interrupted since, judge verdict flagged or absent-and-
    # not-judging) from one still working.
    for event_type in (
        SessionReady,
        InvocationContentDelta,
        InvocationMessage,
        InvocationToolCallStart,
        InvocationToolCallResult,
        ApprovalRequest,
        ApprovalResponse,
        ApprovalReviewed,
        UserInterrupt,
    ):
        await bus.subscribe(event_type, _collect)

    store = JsonFileStateStore(tmp_path / "sessions")

    profile = AgentProfile(
        name="test-pipeline",
        instructions="You are a test assistant. Use the tools provided.",
        tools=ToolsConfig(builtins=builtins or []),
        extensions=extensions or [],
        skills=skills or SkillsConfig(auto_load_user_agents_skills=False, auto_load_cwd_agents_skills=False),
        approval=ApprovalConfig(default=approval_default, overrides=approval_overrides or {}),
        compaction=compaction or CompactionConfig(enabled=False),
    )

    # Register a mock ModelProfile so the resolver picks it up; the
    # active profile carries provider/stream/max_context_tokens (now
    # model-scoped, no longer on Settings).
    model_registry = ModelProfileRegistry()
    mock_profile = ModelProfile(
        id="mock-profile",
        name="mock",
        provider="mock",
        model_id="mock",
        stream=stream,
        max_context_tokens=max_context_tokens,
    )
    model_registry.register(mock_profile)

    settings = Settings(
        model_profile="mock-profile",
        max_transient_retries=max_transient_retries,
        workspace_change_notice=False,
    )

    mock_client = HostedMockChatClient(responses=responses)
    test_tools = tools if tools is not None else list(ALL_TEST_TOOLS)

    # Monkey-patch create_client to return our mock with callbacks forwarded.
    # The engine passes on_intermediate_text to create_client;
    # we forward it to the mock client so batch_id boundaries work.
    async def _patched_create_client(s: Any = None, **kw: Any) -> MockChatClient:
        mock_client._on_intermediate_text = kw.get("on_intermediate_text")
        return mock_client

    create_client_patcher = pytest.MonkeyPatch()
    create_client_patcher.setattr(builder_module, "create_client", _patched_create_client)

    # The build's only local tools are the test tools.
    tools_patcher = pytest.MonkeyPatch()
    install_tools(tools_patcher, test_tools, first_party=builtins or ())

    engine = assemble_agent_engine(
        bus,
        settings=settings,
        state_store=store,
        model_registry=model_registry,
        initial_workspace=workspace if workspace is not None else Workspace.from_cwd(str(tmp_path)),
        surface=surface,
    )
    # Restore/reload replaces layered settings; retain the mock-turn policy.
    engine.settings_handle.override(workspace_change_notice=False)

    try:
        await engine.start(profile)
    except Exception:
        create_client_patcher.undo()
        tools_patcher.undo()
        raise

    # Restore tool assembly immediately — we only need it during start()
    tools_patcher.undo()

    # Phase 4's real LAST_WORDS generator makes an LLM side call, which would
    # consume scripted MockChatClient responses out from under the main tool
    # loop and then burn minutes in note-shortfall retry backoff (3/7/15/30s)
    # whenever a small max_context_tokens pushes usage past the compaction
    # threshold.  Pipeline tests script main-loop responses only, so bind the
    # stub (same pattern as test_engine_integration's _wire_phase4).  Direct
    # attribute access: a started engine must carry an executor and strategy —
    # a silent skip here would resurrect the real-LLM path unnoticed.
    executor = engine.current.loaded.bindings
    assert executor is not None, "engine.start() must install an executor"
    strategy = executor._compaction_strategy
    assert strategy is not None, "built engine must carry a compaction strategy"
    strategy.set_last_words_generator(StubLastWordsGenerator())

    session_id = engine.session.session_id or ""

    return PipelineTestContext(
        engine=engine,
        bus=bus,
        events=events,
        mock_client=mock_client,
        store=store,
        session_id=session_id,
        _create_client_patcher=create_client_patcher,
    )


# ---------------------------------------------------------------------------
# Wait helpers
# ---------------------------------------------------------------------------


async def wait_for_idle(
    ctx: PipelineTestContext,
    # Sized for contended CI workers (a loaded Windows runner has pushed
    # mock turns past 30s) while staying under the global 60s per-test cap
    # so this loud diagnostic still beats the thread kill. The happy path
    # never waits this long — the wait ends at task completion.
    timeout: float = 45.0,
    *,
    expect_task: bool = True,
    stale_task: asyncio.Task[None] | None = None,
) -> None:
    """Wait until the engine's run task completes (includes session save).

    ``_turn_state.lease.run_task`` wraps ``run_and_save`` / ``retry_and_save`` which call
    ``_post_run()`` after the executor stops.  Waiting on the task itself —
    rather than ``executor.state.running`` — guarantees that session persistence
    has finished before the caller inspects the store.

    ``stale_task`` is the previous turn's completed task still installed as
    ``run_task`` at publish time.  A completed task lingers there between
    turns, so phase 1 must not accept it as "the engine picked up this
    message": on a slow runner the new turn's task can take longer than the
    successor-recheck window to appear, and waiting on the stale task returns
    before the new turn has even started.

    A run that outlives ``timeout`` fails the test loudly.  Returning while
    the turn is still active is worse than failing: the caller's next
    ``send_message`` would be consumed as an *injection into the running
    turn* instead of starting a new one, corrupting every downstream
    session/event assertion (the historical silent 5s timeout did exactly
    this on overloaded Windows runners).  The one legitimate "still running"
    exit is a run blocked on a user approval request, which only the test's
    ``approve()``/``reject()`` can unblock — detected via collected events
    and returned early so approval tests don't burn the full timeout.
    """
    # Yield to event loop so any just-created tasks can start executing.
    await asyncio.sleep(0)

    deadline = asyncio.get_running_loop().time() + timeout

    # Phase 1: wait for the run task to appear (engine received the message)
    while asyncio.get_running_loop().time() < deadline:
        task = ctx.engine.turns.turn_state.lease.run_task
        if task is not None and task is not stale_task:
            if task.done():
                await asyncio.sleep(0.05)
                if ctx.engine.turns.turn_state.lease.run_task is not task:
                    continue
            break
        await asyncio.sleep(0.01)
    else:
        if expect_task:
            raise AssertionError("wait_for_idle: no run task ever started — did you call send_message() first?")
        return

    # Phase 2: wait for the run-task chain to complete (includes retry tasks
    # installed during finalization plus _post_run + session save).
    waiter = asyncio.ensure_future(ctx.engine.wait_for_run_task())
    try:
        while not waiter.done():
            if _pending_user_approval(ctx):
                # Blocked on the test's approve()/reject(); nothing to wait for.
                return
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail(
                    f"engine run task did not complete within {timeout}s — "
                    "returning now would let assertions race the still-active turn"
                )
            await asyncio.sleep(0.02)
        with contextlib.suppress(asyncio.CancelledError):
            await waiter
    finally:
        if not waiter.done():
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter

    # Small settle for any fire-and-forget tasks (event publishing, etc.)
    await asyncio.sleep(0.05)


def _pending_user_approval(ctx: PipelineTestContext) -> bool:
    """Return whether the run is blocked awaiting a user's ApprovalResponse.

    A published ApprovalRequest IS the production policy's decision that
    approval is needed: the middleware rules out every auto-approve path
    (policy auto/skip, safe read-only shell, workspace git write, BYPASS
    mode) before publishing, then blocks on a future that nothing but a
    published ApprovalResponse resolves.  Re-deriving the mode from the
    profile config here would misclassify kind-based overrides ("shell"),
    qualified overrides ("shell.run_command"), and sensitive-access
    triggers — so trust the request.

    ``judging`` requests defer to the judge's verdict (ApprovalReviewed):
    with no verdict yet, the judge may still resolve the future directly
    (approved verdicts auto-fulfil without any ApprovalResponse event), so
    the request proves nothing and we keep waiting — erring toward the loud
    deadline failure rather than returning early on a still-working run.  A
    FLAGGED verdict (approved=False, including judge errors) re-blocks the
    request on the user: _run_judge deliberately leaves the future
    unresolved so only the test's approve()/reject() can finish it — that
    request pends like a non-judging one.

    A UserInterrupt published *after* the request clears its pending status:
    the interrupt cancels the run task (and with it the approval await), so
    the run finishes unwinding on its own and wait_for_idle must keep
    waiting for that instead of returning early.
    """
    responded = {e.request_id for e in ctx.events if isinstance(e, ApprovalResponse)}
    flagged = {e.request_id for e in ctx.events if isinstance(e, ApprovalReviewed) and not e.approved}
    last_interrupt = -1
    for index, event in enumerate(ctx.events):
        if isinstance(event, UserInterrupt):
            last_interrupt = index
    for index, event in enumerate(ctx.events):
        if not isinstance(event, ApprovalRequest) or event.request_id in responded:
            continue
        if index < last_interrupt:
            continue
        if event.judging and event.request_id not in flagged:
            continue
        return True
    return False


async def wait_for_event(
    events: list[Event], event_type: type, timeout: float = 45.0, *, min_count: int = 1
) -> list[Event]:
    """Wait until at least min_count events of event_type appear in the list."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        matches = [e for e in events if isinstance(e, event_type)]
        if len(matches) >= min_count:
            return matches
        await asyncio.sleep(0.02)
    found = len([e for e in events if isinstance(e, event_type)])
    raise TimeoutError(f"Expected {min_count} {event_type.__name__} events, got {found}")


# ---------------------------------------------------------------------------
# Event extraction helpers
# ---------------------------------------------------------------------------


def extract_tool_starts(events: list[Event]) -> list[dict[str, str]]:
    """Extract ToolCallStart events as dicts."""
    return [
        {"tool_name": e.tool_name, "call_id": e.call_id}
        for e in events
        if (isinstance(e, InvocationToolCallStart) and e.origin.kind == "turn")
    ]


def extract_tool_results(events: list[Event]) -> list[dict[str, Any]]:
    """Extract ToolCallResult events as dicts."""
    return [
        {"tool_name": e.tool_name, "call_id": e.call_id, "result": e.result}
        for e in events
        if (isinstance(e, InvocationToolCallResult) and e.origin.kind == "turn")
    ]


def extract_final_messages(events: list[Event]) -> list[str]:
    """Extract text from final AgentMessage events."""
    return [e.text for e in events if (isinstance(e, InvocationMessage) and e.origin.kind == "turn") and e.is_final]


def extract_answer_segments(events: list[Event]) -> list[str]:
    """Join the main turn's streamed answer deltas into one text per segment, in opening order."""
    segments: dict[str, str] = {}
    for e in events:
        if isinstance(e, InvocationContentDelta) and e.origin.kind == "turn" and e.channel == "answer":
            segments[e.segment_id] = segments.get(e.segment_id, "") + e.text
    return list(segments.values())


def extract_approval_requests(events: list[Event]) -> list[dict[str, Any]]:
    """Extract ApprovalRequest events as dicts."""
    return [
        {"request_id": e.request_id, "tool_name": e.tool_name, "args": e.args}
        for e in events
        if isinstance(e, ApprovalRequest)
    ]


# ---------------------------------------------------------------------------
# Session message extraction helpers
# ---------------------------------------------------------------------------


def extract_session_tool_names(raw_messages: list[dict[str, Any]]) -> list[str]:
    """Extract tool names from function_call contents in session messages."""
    names: list[str] = []
    for msg in raw_messages:
        if msg.get("role") != "assistant":
            continue
        for c in msg.get("contents", []):
            if isinstance(c, dict) and c.get("type") == "function_call":
                names.append(c.get("name", ""))
    return names


def extract_session_batch_ids(raw_messages: list[dict[str, Any]]) -> list[int | None]:
    """Extract _batch_id from assistant messages that have function_calls."""
    ids: list[int | None] = []
    for msg in raw_messages:
        if msg.get("role") != "assistant":
            continue
        contents = msg.get("contents", [])
        has_fc = any(isinstance(c, dict) and c.get("type") == "function_call" for c in contents)
        if has_fc:
            extra = msg.get("additional_properties", {}) or {}
            ids.append(extra.get("_batch_id"))
    return ids


def extract_session_approvals(raw_messages: list[dict[str, Any]]) -> list[dict[str, str] | None]:
    """Extract _approval from assistant messages that have function_calls."""
    approvals: list[dict[str, str] | None] = []
    for msg in raw_messages:
        if msg.get("role") != "assistant":
            continue
        contents = msg.get("contents", [])
        has_fc = any(isinstance(c, dict) and c.get("type") == "function_call" for c in contents)
        if has_fc:
            extra = msg.get("additional_properties", {}) or {}
            approvals.append(extra.get("_approval"))
    return approvals


def extract_session_call_ids(raw_messages: list[dict[str, Any]]) -> list[str]:
    """Extract call_ids from function_call contents in session messages."""
    ids: list[str] = []
    for msg in raw_messages:
        if msg.get("role") != "assistant":
            continue
        for c in msg.get("contents", []):
            if isinstance(c, dict) and c.get("type") == "function_call":
                cid = c.get("call_id", "")
                if cid:
                    ids.append(cid)
    return ids


def extract_session_intermediate_texts(raw_messages: list[dict[str, Any]]) -> list[str | None]:
    """Extract _intermediate_text from assistant messages."""
    texts: list[str | None] = []
    for msg in raw_messages:
        if msg.get("role") != "assistant":
            continue
        extra = msg.get("additional_properties", {}) or {}
        itext = extra.get("_intermediate_text")
        if itext is not None:
            texts.append(itext)
    return texts


def extract_session_real_results(raw_messages: list[dict[str, Any]], *, marker: str = "echo:") -> list[str]:
    """Extract function_result texts that carry real tool output.

    An interrupted or unfinished call persists an empty/placeholder result, so
    filtering on the tool's own output ``marker`` is what distinguishes
    recovered work from a stub.
    """
    results: list[str] = []
    for msg in raw_messages:
        for c in msg.get("contents", []):
            if isinstance(c, dict) and c.get("type") == "function_result":
                result = c.get("result", "")
                if result and marker in result:
                    results.append(result)
    return results


# ---------------------------------------------------------------------------
# Loop-recorder wait helper
# ---------------------------------------------------------------------------


async def _wait_for_loop_snapshot(ctx: PipelineTestContext, min_iterations: int, *, timeout: float = 5.0) -> None:
    """Wait until the LoopRecorder has snapshotted ``min_iterations`` completed iterations.

    Wait for ``LoopRecorder.record_pre_call`` to capture completed iterations
    into recoverable state when FunctionInvocationLayer enters its next model
    call; a fixed ``asyncio.sleep`` cannot prove that checkpoint. Each completed
    iteration contributes an assistant (function_call) + tool (function_result)
    message pair to ``loop_messages``, so ``>= 2 * min_iterations`` snapshotted
    messages proves the iterations are recoverable before we interrupt.
    """

    def _snapshotted() -> bool:
        recorder = ctx.engine.current.loaded.loop_recorder
        if recorder is None:
            return False
        loop_msgs = recorder.loop_messages
        return loop_msgs is not None and len(loop_msgs) >= 2 * min_iterations

    await wait_for(_snapshotted, timeout=timeout, description="workspace snapshot")


# ---------------------------------------------------------------------------
# Scripted tools and mock-client patches for interrupt/error scenarios
# ---------------------------------------------------------------------------


def slow_echo_tool(
    delay: float = 0.0,
    *,
    name: str = "slow_echo",
    description: str = "Slow echo",
    result_prefix: str = "echo",
) -> FunctionTool:
    """Return an async echo tool that awaits ``delay`` seconds before answering.

    The await lets a test land an interrupt during FunctionInvocationLayer's
    tool execution; ``delay=0`` still yields to the event loop.
    """

    async def _slow_echo(message: Annotated[str, "Message to echo"]) -> str:
        await asyncio.sleep(delay)
        return f"{result_prefix}: {message}"

    return FunctionTool(func=_slow_echo, name=name, description=description)


def restore_mock_client(ctx: PipelineTestContext) -> None:
    """Drop any test patch installed over the mock client's inner call.

    ``error_on_nth`` / ``interrupt_on_nth`` shadow the bound method with an
    instance attribute, so deleting that attribute restores the original.
    Idempotent, so a test may restore mid-run and the fixture may restore again
    before shutting the engine down.
    """
    ctx.mock_client.__dict__.pop("_inner_get_response", None)


def error_on_nth(
    ctx: PipelineTestContext,
    n: int,
    *,
    message: str = "Simulated non-retryable error",
    error: BaseException | None = None,
) -> Callable[[], None]:
    """Make the ``n``-th LLM call raise; return a callable restoring the client.

    Simulates a non-retryable failure part-way through a tool loop so the test
    can assert that the iterations completed before it survive. *error*, such
    as a real provider rejection, replaces the default ``RuntimeError``. The
    failed call consumes no scripted response.
    """
    original = ctx.mock_client._inner_get_response
    calls = itertools.count(1)

    def _error_on_nth(*, messages, stream, options, **kwargs):
        if next(calls) == n:

            async def _throw():
                raise error if error is not None else RuntimeError(message)

            return _throw()
        return original(messages=messages, stream=stream, options=options, **kwargs)

    ctx.mock_client._inner_get_response = _error_on_nth
    return functools.partial(restore_mock_client, ctx)


def interrupt_on_nth(ctx: PipelineTestContext, n: int) -> Callable[[], None]:
    """Set the interrupt flag while the ``n``-th LLM call is in flight.

    Simulates the user pressing Stop after the request left for the provider:
    the response still arrives, so the engine — not the InterruptMiddleware,
    which only fires at tool-call boundaries — has to drop it.  Returns a
    callable restoring the client.
    """
    original = ctx.mock_client._inner_get_response
    interrupt_mw = ctx.engine.current.loaded.bindings._interrupt
    calls = itertools.count(1)

    def _interrupt_on_nth(*, messages, stream, options, **kwargs):
        if next(calls) == n:
            interrupt_mw.set_interrupted()
        return original(messages=messages, stream=stream, options=options, **kwargs)

    ctx.mock_client._inner_get_response = _interrupt_on_nth
    return functools.partial(restore_mock_client, ctx)


def fail_before_response_on_nth(ctx: PipelineTestContext, n: int, *, interrupt: bool = False) -> Callable[[], None]:
    """Make the ``n``-th model request fail before its response starts; return a restoring callable.

    The failure is raised above the response validation middleware, where a
    context or middleware failure between two responses lands, so no
    response-start hook runs for that request. With *interrupt*, the user's
    Stop arrives first and the failure ends an interrupted pass.
    """
    bindings = ctx.engine.current.loaded.bindings
    validation = bindings._response_validation
    assert validation is not None
    original = validation.process
    calls = itertools.count(1)

    async def _process(context: ChatContext, call_next: Callable[[], Awaitable[None]]) -> None:
        if next(calls) == n:
            if interrupt:
                bindings._interrupt.set_interrupted()
            raise RuntimeError("Simulated failure before the response started")
        await original(context, call_next)

    vars(validation)["process"] = _process

    def _restore() -> None:
        vars(validation).pop("process", None)

    return _restore


def fail_nested_before_response_on_nth(
    monkeypatch: pytest.MonkeyPatch, n: int, *, main: ResponseValidationMiddleware | None = None
) -> None:
    """Make the ``n``-th model request of a sub-agent or workflow node fail before its response starts.

    ``fail_before_response_on_nth`` for a middleware built after this call:
    every response validation middleware but *main*, the chat turn's own,
    counts toward ``n``, and the failure is raised above it, so no
    response-start hook runs for that request.
    """
    original = ResponseValidationMiddleware.process
    calls = itertools.count(1)

    async def _process(
        self: ResponseValidationMiddleware, context: ChatContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        if self is not main and next(calls) == n:
            raise RuntimeError("Simulated failure before the response started")
        await original(self, context, call_next)

    monkeypatch.setattr(ResponseValidationMiddleware, "process", _process)
