# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for ContextManagementProvider.

Tests the provider's before_run hook (instruction/tool injection),
the three tool method wrappers, and ContextManager wiring.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from chrys.foundation.tool_result_metadata import TOOL_ERROR_KIND_METADATA_KEY
from chrys.foundation.trajectory.metadata import read_analytics_item_id
from chrys.kernel import AgentSession, Content, Message, SessionContext
from chrys.service.context.compaction import UnifiedContextStrategy
from chrys.service.context.providers.context_management import ContextManagementProvider, _format_messages_as_text
from chrys.service.context.providers.history import CompressibleHistoryProvider
from chrys.service.profiles.models.resolver import default_profile
from chrys.service.tools.result_metadata import tool_result_metadata
from tests.kernel._fakes import (
    _assert_actionable_argument_error,
    _call_response,
    _ProbeFunction,
    _result_contents,
    _stack,
    _text_response,
    _user,
)
from tests.support.provider_errors import openai_context_overflow

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HISTORY_SOURCE_ID = CompressibleHistoryProvider.DEFAULT_SOURCE_ID


def _build_state(num_turns: int) -> dict:
    """Build a history state dict simulating *num_turns* of conversation with markers."""
    state: dict = {"messages": [], "compressed_msgs": [], "turn_counter": 0}
    for i in range(1, num_turns + 1):
        state["messages"].append(Message("user", [f"User message turn {i}"]))
        state["messages"].append(Message("assistant", [f"Assistant response turn {i}"]))
        CompressibleHistoryProvider.insert_marker(state, i)
    return state


def _make_provider() -> ContextManagementProvider:
    return ContextManagementProvider(default_profile(), strategy=UnifiedContextStrategy())


def _bind_session(provider: ContextManagementProvider, history_state: dict | None = None) -> AgentSession:
    """Create a session, optionally seed history state, and bind to provider."""
    session = AgentSession()
    state = history_state if history_state is not None else {}
    session.state[_HISTORY_SOURCE_ID] = state
    # Simulate before_run binding
    provider._session = session
    provider._strategy.bind_state(state)
    return session


# ---------------------------------------------------------------------------
# before_run: instruction + tool injection
# ---------------------------------------------------------------------------


class TestBeforeRun:
    @pytest.mark.asyncio
    async def test_injects_instructions(self) -> None:
        provider = _make_provider()
        session = AgentSession()
        context = SessionContext(input_messages=[])

        await provider.before_run(agent=None, session=session, context=context, state={})

        assert len(context.instructions) == 1
        text = context.instructions[0]
        assert "Context self-management" in text
        assert "compress_context" in text
        assert "user's own content is" in text
        assert "always first" in text
        assert "trailing `<system-reminder>`" in text
        assert "`&lt;system-reminder&gt;`" in text
        assert "literal user-authored text" in text
        # Compaction + cross-turn compression awareness
        assert "compaction and compression" in text.lower()
        assert "LAST_WORDS" in text
        assert "sub-agent" in text
        # Must not leak context management details to the user
        assert "silently" in text

    @pytest.mark.asyncio
    async def test_injects_three_tools(self) -> None:
        provider = _make_provider()
        session = AgentSession()
        context = SessionContext(input_messages=[])

        await provider.before_run(agent=None, session=session, context=context, state={})

        tool_names = {t.name for t in context.tools}
        assert tool_names == {"compress_context", "recall_context", "list_compressed_contexts"}

    @pytest.mark.asyncio
    async def test_binds_session(self) -> None:
        provider = _make_provider()
        session = AgentSession()
        context = SessionContext(input_messages=[])

        await provider.before_run(agent=None, session=session, context=context, state={})

        assert provider._session is session

    @pytest.mark.asyncio
    async def test_tools_carry_context_kind_out_of_band(self) -> None:
        from chrys.foundation.tool_kinds import KIND_CONTEXT, get_tool_kind

        provider = _make_provider()
        session = AgentSession()
        context = SessionContext(input_messages=[])

        await provider.before_run(agent=None, session=session, context=context, state={})

        for t in context.tools:
            assert get_tool_kind(t) == KIND_CONTEXT, f"{t.name} should carry KIND_CONTEXT"
            assert t.kind is None, f"{t.name} must keep chrys kinds off FunctionTool.kind"


# ---------------------------------------------------------------------------
# _compress_context
# ---------------------------------------------------------------------------


class TestCompressContext:
    @pytest.mark.asyncio
    async def test_compress_succeeds(self) -> None:
        provider = _make_provider()
        state = _build_state(3)
        _bind_session(provider, state)

        result = await provider._compress_context(marker_id="turn_2", summary="Summary of turns 1-2")
        await provider._strategy(state["messages"])

        assert "compressed_context_id=" in result
        assert "Compressed messages up to turn_2" in result
        assert len(state["compressed_msgs"]) == 1
        assert state["compressed_msgs"][0].summary_text == "Summary of turns 1-2"

    @pytest.mark.asyncio
    async def test_compress_invalid_marker(self) -> None:
        provider = _make_provider()
        state = _build_state(2)
        _bind_session(provider, state)

        result = await provider._compress_context(marker_id="marker_999", summary="nope")

        assert result.startswith("Error:")

    @pytest.mark.asyncio
    async def test_compress_reports_visible_count(self) -> None:
        provider = _make_provider()
        state = _build_state(3)
        _bind_session(provider, state)

        result = await provider._compress_context(marker_id="turn_2", summary="Summary")

        # After compression: summary + user3 + assistant3 = 3 visible (marker_3 excluded)
        assert "3 visible message(s)" in result
        assert "1 compressed block(s)" in result


# ---------------------------------------------------------------------------
# _list_compressed_contexts
# ---------------------------------------------------------------------------


async def test_list_tool_rejects_extra_arguments_before_execution_and_accepts_retry() -> None:
    provider = _make_provider()
    _bind_session(provider, _build_state(3))
    name = "list_compressed_contexts"
    probe = _ProbeFunction()
    layer, wire = _stack(
        [
            _call_response(("initial", name, {})),
            _call_response(("bad-call", name, {"_call": "check before compressing"})),
            _call_response(("bad-note", name, {"note": "check before compressing"})),
            _call_response(("retry", name, {})),
            _text_response(),
        ]
    )

    response = await layer.get_response([_user()], options={"tools": provider._tools}, middleware=[probe])

    initial, bad_call, bad_note, retry = _result_contents(response)
    for result, field in ((bad_call, "_call"), (bad_note, "note")):
        message = _assert_actionable_argument_error(result, name, f"unknown '{field}'", "Expected: {}.")
        assert "check before compressing" not in message
    assert initial.exception is retry.exception is None
    assert initial.result == retry.result
    assert "turn_3" in str(retry.result)
    assert [context.arguments for context in probe.contexts] == [{}, {}]
    assert any(bad_call in message.contents for message in wire.calls[2]["messages"])


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("compress_context", {"marker_id": "turn_2", "summary": "Summary"}),
        ("recall_context", {"compressed_context_id": "block", "question": "What changed?"}),
    ],
)
@pytest.mark.parametrize("field", ["_call", "note"])
async def test_context_tools_reject_extra_arguments_before_execution(
    name: str, arguments: dict[str, str], field: str
) -> None:
    provider = _make_provider()
    probe = _ProbeFunction()
    # Leave the provider unbound: an invalid call must never reach its body.
    layer, _wire = _stack([_call_response(("bad", name, {**arguments, field: "extra"})), _text_response()])

    response = await layer.get_response([_user()], options={"tools": provider._tools}, middleware=[probe])

    (result,) = _result_contents(response)
    _assert_actionable_argument_error(result, name, f"unknown '{field}'")
    assert probe.contexts == []


class TestListCompressedContexts:
    def test_empty_state(self) -> None:
        provider = _make_provider()
        state: dict = {"messages": [], "compressed_msgs": []}
        _bind_session(provider, state)

        result = provider._list_compressed_contexts()

        assert "No compressed blocks yet." in result
        assert "No fold markers available" in result

    def test_lists_markers(self) -> None:
        provider = _make_provider()
        state = _build_state(3)
        _bind_session(provider, state)

        result = provider._list_compressed_contexts()

        assert "turn_1" in result
        assert "turn_2" in result
        assert "turn_3" in result

    def test_lists_blocks_after_compress(self) -> None:
        provider = _make_provider()
        state = _build_state(3)
        _bind_session(provider, state)
        CompressibleHistoryProvider.compress(state, "turn_2", "Summary A")

        block = state["compressed_msgs"][0]
        assert all(read_analytics_item_id(message.additional_properties) for message in block.messages)

        result = provider._list_compressed_contexts()

        assert "Summary A" in result
        assert "turn_3" in result
        # Folded markers should not appear
        assert "turn_1 " not in result
        assert "turn_2 " not in result


# ---------------------------------------------------------------------------
# _recall_context
# ---------------------------------------------------------------------------


class TestRecallContext:
    def test_recall_formatter_preserves_specialized_hosted_calls_and_results(self) -> None:
        image = Content.from_uri("data:image/png;base64,QUJD", media_type="image/png")
        messages = [
            Message(
                "assistant",
                [
                    Content.from_search_tool_call(
                        "search-1",
                        tool_name="web_search",
                        arguments={"query": "Chrys"},
                    ),
                    Content.from_code_interpreter_tool_call(
                        call_id="code-1",
                        inputs=[Content.from_text("print('code input')")],
                    ),
                    Content.from_image_generation_tool_call(image_id="image-1"),
                    Content.from_shell_tool_call(call_id="shell-1", commands=["printf shell-input"]),
                ],
            ),
            Message(
                "tool",
                [
                    Content.from_search_tool_result(
                        "search-1",
                        tool_name="web_search",
                        result="search result",
                    ),
                    Content.from_code_interpreter_tool_result(
                        call_id="code-1",
                        outputs=[Content.from_text("code result")],
                    ),
                    Content.from_image_generation_tool_result(image_id="image-1", outputs=[image]),
                    Content.from_shell_tool_result(
                        call_id="shell-1",
                        outputs=[Content.from_shell_command_output(stdout="shell result", exit_code=0)],
                    ),
                ],
            ),
        ]

        formatted = _format_messages_as_text(messages)

        for fragment in (
            "web_search",
            '"query": "Chrys"',
            "search result",
            "code_interpreter",
            "code input",
            "code result",
            "image_generation",
            '"image_id": "image-1"',
            "image/png image",
            "shell",
            "shell-input",
            "shell result",
        ):
            assert fragment in formatted

    def test_recall_formatter_renders_non_text_function_result(self) -> None:
        messages = [
            Message("assistant", [Content.from_function_call("call", "view_image", arguments={"path": "shot.png"})]),
            Message(
                "tool",
                [Content.from_function_result("call", result=[Content.from_data(b"\x89PNG", "image/png")])],
            ),
        ]

        formatted = _format_messages_as_text(messages)

        assert "[Tool result: [image/png image]]" in formatted
        assert "completed" not in formatted

    @pytest.mark.asyncio
    async def test_recall_invalid_id(self) -> None:
        provider = _make_provider()
        state = _build_state(2)
        _bind_session(provider, state)

        result = await provider._recall_context(compressed_context_id="ctx_nonexistent", question="what?")

        assert "not found" in result

    @pytest.mark.asyncio
    async def test_recall_empty_block(self) -> None:
        provider = _make_provider()
        state = _build_state(2)
        _bind_session(provider, state)

        # Compress and then empty the block's messages to simulate edge case
        CompressibleHistoryProvider.compress(state, "turn_1", "Summary")
        block = state["compressed_msgs"][0]
        ctx_id = block.compressed_context_id
        block.messages.clear()

        result = await provider._recall_context(compressed_context_id=ctx_id, question="anything?")

        assert "no readable messages" in result

    @pytest.mark.asyncio
    async def test_recall_passes_session_id_to_client(self) -> None:
        class _Response:
            text = "remembered"

        class _Client:
            async def get_response(self, _messages: list[Any], **_kwargs: Any) -> _Response:
                return _Response()

            async def aclose(self) -> None:
                pass

        provider = ContextManagementProvider(
            default_profile(),
            strategy=UnifiedContextStrategy(),
            session_id="sess-recall",
            parent_session_id="parent-recall",
        )
        state = _build_state(2)
        _bind_session(provider, state)
        CompressibleHistoryProvider.compress(state, "turn_1", "Summary")
        ctx_id = state["compressed_msgs"][0].compressed_context_id

        with patch("chrys.service.llm.clients.create_client", return_value=_Client()) as create_client:
            result = await provider._recall_context(compressed_context_id=ctx_id, question="what happened?")

        assert result == "remembered"
        create_client.assert_called_once()
        assert create_client.call_args.kwargs["session_id"] == "sess-recall"
        assert create_client.call_args.kwargs["parent_session_id"] == "parent-recall"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("answers", [True, False])
    async def test_recall_closes_its_client_on_success_and_failure(self, answers: bool) -> None:
        class _Response:
            text = "remembered"

        class _Client:
            closes = 0

            async def get_response(self, _messages: list[Any], **_kwargs: Any) -> _Response:
                if not answers:
                    raise ConnectionError("offline")
                return _Response()

            async def aclose(self) -> None:
                self.closes += 1

        provider = ContextManagementProvider(default_profile(), strategy=UnifiedContextStrategy())
        state = _build_state(2)
        _bind_session(provider, state)
        CompressibleHistoryProvider.compress(state, "turn_1", "Summary")
        ctx_id = state["compressed_msgs"][0].compressed_context_id
        client = _Client()

        with patch("chrys.service.llm.clients.create_client", return_value=client):
            result = await provider._recall_context(compressed_context_id=ctx_id, question="what happened?")

        assert (result == "remembered") is answers
        assert client.closes == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("overflow", [True, False])
    async def test_recall_failure_keeps_the_exception_text_from_the_model(self, overflow: bool) -> None:
        error = (
            await openai_context_overflow()
            if overflow
            else ConnectionError("connect to https://gateway.internal/v1 failed; sk-test-secret; /Users/me/.chrys")
        )

        class _Client:
            async def get_response(self, _messages: list[Any], *, stream: bool, options: Any) -> Any:
                raise error

            async def aclose(self) -> None:
                pass

        provider = ContextManagementProvider(default_profile(), strategy=UnifiedContextStrategy())
        state = _build_state(2)
        _bind_session(provider, state)
        CompressibleHistoryProvider.compress(state, "turn_1", "Summary")
        ctx_id = state["compressed_msgs"][0].compressed_context_id
        metadata: dict[str, object] = {}
        token = tool_result_metadata.set(metadata)
        try:
            with patch("chrys.service.llm.clients.create_client", return_value=_Client()):
                result = await provider._recall_context(compressed_context_id=ctx_id, question="what happened?")
        finally:
            tool_result_metadata.reset(token)

        expected = "too large for this model's context window" if overflow else "did not complete"
        assert result.startswith("Error: recall failed — ")
        assert expected in result
        assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "context_recall_failed"
        for leaked in ("131072", "gateway.internal", "sk-test-secret", "/Users/me"):
            assert leaked not in result
            assert leaked not in str(metadata)


# ---------------------------------------------------------------------------
# Error: session not bound
# ---------------------------------------------------------------------------


class TestSessionNotBound:
    @pytest.mark.asyncio
    async def test_compress_raises_without_session(self) -> None:
        provider = _make_provider()
        with pytest.raises(RuntimeError, match="session not bound"):
            await provider._compress_context(marker_id="turn_1", summary="x")

    def test_list_without_bound_strategy_reports_empty(self) -> None:
        provider = _make_provider()
        result = provider._list_compressed_contexts()

        assert "No compressed blocks yet." in result
        assert "No fold markers available" in result


# ---------------------------------------------------------------------------
# ContextManager wiring
# ---------------------------------------------------------------------------


class TestContextManagerWiring:
    def test_provider_in_context_manager(self) -> None:
        from chrys.service.context.manager import ContextManager

        ctx = ContextManager(default_profile())

        assert any(isinstance(p, ContextManagementProvider) for p in ctx.providers)

    def test_provider_order(self) -> None:
        """ContextManagementProvider should come before CompressibleHistoryProvider."""
        from chrys.service.context.manager import ContextManager

        ctx = ContextManager(default_profile())

        types = [type(p).__name__ for p in ctx.providers]
        mgmt_idx = types.index("ContextManagementProvider")
        hist_idx = types.index("CompressibleHistoryProvider")
        assert mgmt_idx < hist_idx

    def test_context_manager_passes_session_id_to_provider(self) -> None:
        from chrys.service.context.manager import ContextManager

        ctx = ContextManager(default_profile(), session_id="sess-context")

        assert ctx.context_mgmt_provider is not None
        assert ctx.context_mgmt_provider._session_id == "sess-context"

    def test_context_manager_wires_derived_budgets_everywhere(self) -> None:
        from chrys.service.context.manager import ContextManager

        ctx = ContextManager(default_profile())

        assert ctx.compaction_strategy.trigger_pct == ctx.budgets.trigger_pct
        assert ctx.compaction_strategy.target_pct == ctx.budgets.target_pct
        assert ctx.history_provider._force_compress_pct == ctx.budgets.force_compress_pct
        assert ctx.usage_middleware._compaction_strategy is ctx.compaction_strategy

    def test_aggregate_hosted_usage_covers_anthropic_and_stateless_responses(self) -> None:
        from chrys.service.context.manager import ContextManager

        deepseek_responses = default_profile()
        deepseek_responses.provider = "deepseek-openai"
        deepseek_responses.api_style = "responses"
        deepseek = ContextManager(deepseek_responses)

        openai_responses = default_profile()
        openai_responses.provider = "openai"
        openai_responses.api_style = "responses"
        openai = ContextManager(openai_responses)

        openai_stored = ContextManager(openai_responses, service_context_default_options={"store": True})

        anthropic_profile = default_profile()
        anthropic_profile.provider = "anthropic"
        anthropic = ContextManager(anthropic_profile)

        deepseek_chat = default_profile()
        deepseek_chat.provider = "deepseek-openai"
        deepseek_chat.api_style = "chat_completions"
        chat = ContextManager(deepseek_chat)

        assert deepseek.use_local_context_estimate_for_hosted_usage
        assert deepseek.usage_middleware._use_local_context_estimate_for_hosted_usage
        assert deepseek.history_provider._use_local_context_estimate_for_hosted_usage
        assert openai.use_local_context_estimate_for_hosted_usage
        assert anthropic.use_local_context_estimate_for_hosted_usage
        assert anthropic.usage_middleware._use_local_context_estimate_for_hosted_usage
        assert anthropic.history_provider._use_local_context_estimate_for_hosted_usage
        assert not openai_stored.use_local_context_estimate_for_hosted_usage
        assert not chat.use_local_context_estimate_for_hosted_usage

    def test_internal_disable_also_disables_force_compress(self) -> None:
        from chrys.service.context.manager import ContextManager
        from chrys.service.profiles.agents.schema import CompactionConfig

        ctx = ContextManager(
            default_profile(),
            compaction_config=CompactionConfig(enabled=False),
        )

        assert not ctx.compaction_strategy._compaction_enabled
        assert ctx.history_provider._force_compress_pct == 1.0
        assert ctx.context_mgmt_provider is not None
        assert "compress_context" in ctx.context_mgmt_provider.tool_names
