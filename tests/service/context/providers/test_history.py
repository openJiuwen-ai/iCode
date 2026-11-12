# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for :class:`CompressibleHistoryProvider` wire filtering (§2.4).

The provider's ``get_messages`` is the model-input reader: what it returns
is what the model sees as session history.  A state-RESIDENT flagged
``continue`` nudge is a legacy crash leftover and must be omitted; injections
are real user input and pass through.
"""

from __future__ import annotations

import pytest

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import EXCLUDED_KEY, Agent, AgentSession, ChatResponse, Content, Message, SessionContext, UsageDetails
from chrys.service.context.providers.history import CompressibleHistoryProvider, _auto_summary


def _turn_marker(turn: int) -> Message:
    m = Message("assistant", [""])
    m.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    m.additional_properties["_turn"] = turn
    return m


def _nudge(text: str = "continue") -> Message:
    m = Message("user", [text])
    m.additional_properties[HistoryMarkerKind.CONTINUATION_KEY] = True
    return m


def _injected_user(text: str) -> Message:
    m = Message("user", [text])
    m.additional_properties[HistoryMarkerKind.INJECTED_KEY] = True
    return m


@pytest.mark.asyncio
async def test_get_messages_omits_state_resident_flagged_nudge() -> None:
    """A leftover ``_continuation`` nudge in provider state never reaches the
    wire, while flagged injections and compressed summaries pass through and
    turn markers / excluded messages stay filtered."""
    summary = Message("assistant", ["[Compressed context: ctx_1]"])
    summary.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.SUMMARY
    opener = Message("user", ["do x"])
    work = Message("assistant", ["working"])
    excluded = Message("assistant", ["compacted away"])
    excluded.additional_properties[EXCLUDED_KEY] = True
    injection = _injected_user("also check the docs")
    leftover_nudge = _nudge()

    provider = CompressibleHistoryProvider()
    state = {
        "messages": [summary, opener, work, _turn_marker(1), excluded, injection, leftover_nudge],
    }

    wire = await provider.get_messages("local", state=state)

    assert [m.text for m in wire] == [
        "[Compressed context: ctx_1]",
        "do x",
        "working",
        "also check the docs",
    ]
    # The filter is a read-side view: state itself keeps the leftover for
    # post-run cleanup to remove.
    assert leftover_nudge in state["messages"]


@pytest.mark.asyncio
async def test_empty_resume_filters_persisted_legacy_nudge_without_scrubbing_state() -> None:
    """Empty input replays the real transcript while a legacy nudge stays local."""
    opener = Message("user", ["original request"])
    work = Message("assistant", ["interrupted work"])
    leftover = _nudge()

    provider = CompressibleHistoryProvider()
    state = {"messages": [opener, work, leftover]}
    session = AgentSession(session_id="local")
    context = SessionContext(
        session_id="local",
        service_session_id=None,
        input_messages=[],
    )

    await provider.before_run(agent=object(), session=session, context=context, state=state)

    wire = context.get_messages(include_input=True)
    assert [m.text for m in wire] == ["original request", "interrupted work"]
    assert leftover in state["messages"]


def test_auto_summary_never_quotes_flagged_nudge() -> None:
    """Fold-range snippets skip synthetic nudges — a fold summary never opens
    with "User: continue" — while injected messages are still quoted."""
    nudge = _nudge()
    work = Message("assistant", ["working"])
    injection = _injected_user("remember to use flag X")
    state = {"messages": [nudge, work, injection, _turn_marker(1)]}

    summary = _auto_summary(state, 3)

    assert "continue" not in summary
    assert "User: remember to use flag X" in summary


def test_auto_summary_all_nudges_falls_back_to_tools_only() -> None:
    """A fold range whose only user messages are synthetic nudges yields a
    snippet-less summary rather than quoting ``continue``."""
    nudge = _nudge()
    call = Message("assistant", [Content.from_function_call("c1", "search", arguments={})])
    result = Message("tool", [Content.from_function_result("c1", result="hit")])
    state = {"messages": [nudge, call, result, _turn_marker(1)]}

    summary = _auto_summary(state, 3)

    assert "continue" not in summary
    assert "Tools: search" in summary


def test_auto_summary_includes_every_specialized_hosted_call_name() -> None:
    calls = Message(
        "assistant",
        [
            Content.from_search_tool_call("search-1", tool_name="web_search", arguments={}),
            Content.from_code_interpreter_tool_call(call_id="code-1"),
            Content.from_image_generation_tool_call(image_id="image-1"),
            Content.from_shell_tool_call(call_id="shell-1", commands=["pwd"]),
            Content.from_mcp_server_tool_call("mcp-1", "lookup"),
            Content.from_hosted_tool_call("hosted-1", tool_name="remote_task"),
        ],
    )
    state = {"messages": [calls, _turn_marker(1)]}

    summary = _auto_summary(state, 1)

    assert summary == "Tools: code_interpreter, image_generation, lookup, remote_task, shell, web_search"


@pytest.mark.asyncio
async def test_agent_response_latest_usage_books_a_turn_boundary_pass() -> None:
    """A single model call exposes its usage as the latest-call occupancy; history is not rewritten after the run."""

    class _Client:
        model = "test-model"

        def get_response(self, *_args, **_kwargs):
            async def _resolve() -> ChatResponse:
                return ChatResponse(
                    messages=[Message("assistant", ["done"])],
                    usage_details=UsageDetails(input_token_count=90, output_token_count=5),
                )

            return _resolve()

    class _Strategy:
        def __init__(self) -> None:
            self.boundary_requests = 0

        def bind_state(self, _state: dict[str, object]) -> None:
            return None

        async def flush_pending_compressions(self) -> bool:
            return False

        def persist_exclusions_to_state(self, _messages: list[Message]) -> None:
            return None

        def request_turn_boundary_pass(self) -> bool:
            self.boundary_requests += 1
            return True

    strategy = _Strategy()
    provider = CompressibleHistoryProvider(
        compaction_strategy=strategy,  # type: ignore[arg-type]
        max_context_tokens=100,
        force_compress_pct=0.70,
    )
    state: dict[str, object] = {
        "messages": [Message("user", ["old request"]), Message("assistant", ["old answer"])],
    }
    CompressibleHistoryProvider.insert_marker(state, 1)
    session = AgentSession(session_id="usage-force")
    session.state[provider.source_id] = state
    agent = Agent(client=_Client(), context_providers=[provider])

    response = await agent.run("new request", session=session)

    assert response.usage_details == {"input_token_count": 90, "output_token_count": 5}
    assert strategy.boundary_requests == 1
    assert state.get("compressed_msgs", []) == []


@pytest.mark.asyncio
async def test_turn_end_below_the_force_line_books_nothing() -> None:
    class _Strategy:
        boundary_requests = 0

        def request_turn_boundary_pass(self) -> bool:
            self.boundary_requests += 1
            return True

    strategy = _Strategy()
    provider = CompressibleHistoryProvider(
        compaction_strategy=strategy,  # type: ignore[arg-type]
        max_context_tokens=100,
        force_compress_pct=0.70,
    )
    state: dict[str, object] = {
        "messages": [Message("user", ["old request"]), Message("assistant", ["old answer"])],
    }
    CompressibleHistoryProvider.insert_marker(state, 1)
    context = SessionContext(input_messages=[Message("user", ["new request"])])
    context._response = ChatResponse(
        messages=[Message("assistant", ["done"])],
        usage_details=UsageDetails(input_token_count=70),
    )

    assert not await provider._check_turn_end_usage(state, context)
    assert strategy.boundary_requests == 0

    context._response = ChatResponse(
        messages=[Message("assistant", ["done"])],
        usage_details=UsageDetails(input_token_count=71),
    )

    assert not await provider._check_turn_end_usage(state, context)
    assert strategy.boundary_requests == 1


@pytest.mark.asyncio
async def test_turn_end_without_an_older_turn_books_nothing() -> None:
    """The pass leaves the turn that just ended alone, so with no older turn it has nothing to work on."""

    class _Strategy:
        boundary_requests = 0

        def request_turn_boundary_pass(self) -> bool:
            self.boundary_requests += 1
            return True

    strategy = _Strategy()
    provider = CompressibleHistoryProvider(
        compaction_strategy=strategy,  # type: ignore[arg-type]
        max_context_tokens=100,
        force_compress_pct=0.70,
    )
    state: dict[str, object] = {"messages": [Message("user", ["first request"])]}
    context = SessionContext(input_messages=[Message("user", ["first request"])])
    context._response = ChatResponse(
        messages=[Message("assistant", ["done"])],
        usage_details=UsageDetails(input_token_count=90),
    )

    assert not await provider._check_turn_end_usage(state, context)
    assert strategy.boundary_requests == 0


@pytest.mark.asyncio
async def test_turn_end_fold_skips_when_latest_call_usage_unavailable() -> None:
    """A high billing aggregate with an unavailable final-call usage must NOT
    fold a turn — the aggregate is not an occupancy signal."""
    provider = CompressibleHistoryProvider(
        max_context_tokens=100,
        force_compress_pct=0.70,
    )
    state: dict[str, object] = {
        "messages": [Message("user", ["old request"]), Message("assistant", ["old answer"])],
    }
    CompressibleHistoryProvider.insert_marker(state, 1)
    context = SessionContext(input_messages=[Message("user", ["new request"])])
    context._response = ChatResponse(
        messages=[Message("assistant", ["done"])],
        usage_details=UsageDetails(input_token_count=90),
        latest_usage_details=None,
    )

    changed = await provider._check_turn_end_usage(state, context)

    assert not changed
    assert state.get("compressed_msgs", []) == []


@pytest.mark.asyncio
async def test_turn_end_check_ignores_deepseek_hosted_aggregate_usage() -> None:
    """Hosted billing aggregate is not the final request's context occupancy."""

    class _Strategy:
        estimated_context_input_tokens = 20
        boundary_requests = 0

        def request_turn_boundary_pass(self) -> bool:
            self.boundary_requests += 1
            return True

    strategy = _Strategy()
    provider = CompressibleHistoryProvider(
        compaction_strategy=strategy,  # type: ignore[arg-type]
        max_context_tokens=100,
        force_compress_pct=0.70,
        use_local_context_estimate_for_hosted_usage=True,
    )
    state: dict[str, object] = {
        "messages": [Message("user", ["old request"]), Message("assistant", ["old answer"])],
    }
    CompressibleHistoryProvider.insert_marker(state, 1)
    context = SessionContext(input_messages=[Message("user", ["new request"])])
    context._response = ChatResponse(
        messages=[
            Message(
                "assistant",
                [Content.from_search_tool_call("ws1", tool_name="web_search", arguments={})],
            )
        ],
        usage_details=UsageDetails(input_token_count=90, output_token_count=5),
    )

    changed = await provider._check_turn_end_usage(state, context)

    assert not changed
    assert strategy.boundary_requests == 0


@pytest.mark.asyncio
async def test_turn_end_check_prefers_provider_hosted_context_occupancy() -> None:
    """A provider-derived final window wins over both aggregate and local estimates."""

    class _Strategy:
        estimated_context_input_tokens = 90
        boundary_requests = 0

        def request_turn_boundary_pass(self) -> bool:
            self.boundary_requests += 1
            return True

    strategy = _Strategy()
    provider = CompressibleHistoryProvider(
        compaction_strategy=strategy,  # type: ignore[arg-type]
        max_context_tokens=100,
        force_compress_pct=0.70,
        use_local_context_estimate_for_hosted_usage=True,
    )
    state: dict[str, object] = {
        "messages": [Message("user", ["old request"]), Message("assistant", ["old answer"])],
    }
    CompressibleHistoryProvider.insert_marker(state, 1)
    context = SessionContext(input_messages=[Message("user", ["new request"])])
    context._response = ChatResponse(
        messages=[
            Message(
                "assistant",
                [Content.from_search_tool_call("ws1", tool_name="web_search", arguments={})],
            )
        ],
        usage_details=UsageDetails(
            input_token_count=90,
            output_token_count=5,
            context_input_token_count=20,
        ),
    )

    changed = await provider._check_turn_end_usage(state, context)

    assert not changed
    assert strategy.boundary_requests == 0


@pytest.mark.asyncio
async def test_turn_end_fold_skips_provider_owned_service_context() -> None:
    provider = CompressibleHistoryProvider(
        max_context_tokens=100,
        force_compress_pct=0.70,
        skip_local_history_for_service_context=True,
    )
    state: dict[str, object] = {
        "messages": [Message("user", ["old request"]), Message("assistant", ["old answer"])],
    }
    CompressibleHistoryProvider.insert_marker(state, 1)
    context = SessionContext(service_session_id="resp_123", input_messages=[Message("user", ["new request"])])
    context._response = ChatResponse(
        messages=[Message("assistant", ["done"])],
        usage_details=UsageDetails(input_token_count=90),
    )

    changed = await provider._check_turn_end_usage(state, context)

    assert not changed
    assert state.get("compressed_msgs", []) == []


@pytest.mark.asyncio
async def test_turn_end_fold_skips_first_store_true_service_request() -> None:
    provider = CompressibleHistoryProvider(
        max_context_tokens=100,
        force_compress_pct=0.70,
        skip_local_history_for_service_context=True,
        service_context_default_options={"store": True},
    )
    state: dict[str, object] = {
        "messages": [Message("user", ["old request"]), Message("assistant", ["old answer"])],
    }
    CompressibleHistoryProvider.insert_marker(state, 1)
    context = SessionContext(input_messages=[Message("user", ["new request"])])
    context._response = ChatResponse(
        messages=[Message("assistant", ["done"])],
        usage_details=UsageDetails(input_token_count=90),
    )

    changed = await provider._check_turn_end_usage(state, context)

    assert not changed
    assert state.get("compressed_msgs", []) == []
