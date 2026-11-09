# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for provider-neutral hosted-tool presentation."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import Any

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    InvocationProgress,
    InvocationToolCallArgsUpdated,
    InvocationToolCallProgress,
    InvocationToolCallResult,
    InvocationToolCallStart,
    InvocationToolCallStatusUpdated,
)
from chrys.foundation.hosted_tools import (
    HostedRetrySafety,
    HostedToolPhase,
    HostedToolStatus,
)
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.tool_result_metadata import (
    TOOL_ERROR_CODE_METADATA_KEY,
    TOOL_ERRORED_METADATA_KEY,
    TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY,
    tool_result_metadata_failure_state,
)
from chrys.kernel import Content, Message
from chrys.service.agent_middleware.events.hosted_tools import (
    CodeHostedAdapter,
    FinalTextOp,
    GenericHostedAdapter,
    HostedPresentationBridge,
    HostedToolArgsOp,
    HostedToolProgressOp,
    HostedToolResultOp,
    HostedToolStartOp,
    HostedToolStatusOp,
    ImageHostedAdapter,
    McpHostedAdapter,
    ResponsePresentationPlan,
    SearchHostedAdapter,
    ShellHostedAdapter,
    adapt_hosted_tool,
    get_hosted_adapter,
)
from chrys.service.agent_middleware.events.sub_agent_events import SubAgentEventMiddleware
from chrys.service.session.sub_agent_logs import SubAgentLogStats
from tests.support.waiting import wait_for

HOSTED_OPERATION_TYPES = (
    HostedToolStartOp,
    HostedToolArgsOp,
    HostedToolProgressOp,
    HostedToolStatusOp,
    HostedToolResultOp,
)


def _search_call(
    call_id: str = "search_1",
    *,
    item_id: str = "item_1",
    phase: str = HostedToolPhase.START,
    status: str = "running",
) -> Content:
    return Content.from_search_tool_call(
        call_id,
        tool_name="web_search",
        arguments={"query": "Chrys"},
        status=status,
        hosted_provider="openai",
        provider_item_type="web_search_call",
        provider_item_id=item_id,
        provider_phase=phase,
        provider_status=status,
        retry_safety=HostedRetrySafety.READ_ONLY,
    )


def _search_result(
    call_id: str = "search_1",
    *,
    item_id: str = "item_1",
    status: str = "completed",
    phase: str = HostedToolPhase.TERMINAL,
    result: object = "found",
    additional_properties: dict[str, object] | None = None,
) -> Content:
    return Content.from_search_tool_result(
        call_id,
        tool_name="web_search",
        result=result,
        status=status,
        hosted_provider="openai",
        provider_item_type="web_search_call",
        provider_item_id=item_id,
        provider_phase=phase,
        provider_status=status,
        retry_safety=HostedRetrySafety.READ_ONLY,
        additional_properties=additional_properties,
    )


def _assistant(contents: list[Content]) -> Message:
    return Message(role="assistant", contents=contents)


def _operation_types(plan: ResponsePresentationPlan) -> list[type[object]]:
    return [type(operation) for operation in plan.operations]


async def _present_blocking(bridge: HostedPresentationBridge, messages: list[Message]) -> None:
    """Drive one non-streaming response the way validation and the run's completion do."""
    await bridge.attempt_started()
    await bridge.observe_contents([content for message in messages for content in message.contents], is_final=True)
    await bridge.attempt_accepted(messages)
    await bridge.reconcile_accepted(messages, final=True)


class TestHostedAdapters:
    @pytest.mark.parametrize(
        ("family", "adapter_type"),
        [
            ("search", SearchHostedAdapter),
            ("fetch", SearchHostedAdapter),
            ("mcp", McpHostedAdapter),
            ("code", CodeHostedAdapter),
            ("image", ImageHostedAdapter),
            ("shell", ShellHostedAdapter),
            ("tool_discovery", GenericHostedAdapter),
            ("file_operation", GenericHostedAdapter),
            ("generic", GenericHostedAdapter),
        ],
    )
    def test_family_selection(self, family: str, adapter_type: type[GenericHostedAdapter]) -> None:
        assert isinstance(get_hosted_adapter(family), adapter_type)

    def test_search_view_extracts_safe_fields(self) -> None:
        call = _search_call()
        image = Content.from_uri("data:image/png;base64,AAA", media_type="image/png")
        artifact = Content.from_hosted_file("file_1", media_type="text/csv", name="report.csv")
        result = _search_result(result=[Content.from_text("found"), image, artifact])

        view = adapt_hosted_tool(call, result)

        assert view.family == "search"
        assert view.provider == "openai"
        assert view.provider_item_type == "web_search_call"
        assert view.provider_call_id == "search_1"
        assert view.tool_name == "web_search"
        assert view.display_title == "Hosted Search"
        assert view.arguments == {"query": "Chrys"}
        assert view.result_text.startswith("found")
        assert view.image_contents == [image]
        assert view.artifacts == [artifact]
        assert (view.status, view.phase, view.retry_safety) == ("completed", "terminal", "read_only")

    def test_shell_view_normalizes_command_outputs_and_metadata(self) -> None:
        call = Content.from_shell_tool_call(
            call_id="shell_1",
            commands=["printf hi"],
            hosted_provider="openai",
            provider_status="running",
        )
        artifact = Content.from_hosted_file("file_1", name="output.txt")
        result = Content.from_shell_tool_result(
            call_id="shell_1",
            outputs=[
                Content.from_shell_command_output(
                    stdout="hi",
                    stderr="",
                    exit_code=0,
                    timed_out=False,
                ),
                artifact,
            ],
            hosted_provider="openai",
            provider_phase="terminal",
            provider_status="completed",
        )

        view = adapt_hosted_tool(call, result)

        assert view.result_text == "hi"
        assert view.artifacts == [artifact]
        assert view.metadata["stdout"] == "hi"
        assert view.metadata["exit_code"] == 0
        assert view.metadata["timed_out"] is False
        assert view.metadata["outputs"] == [{"stdout": "hi", "stderr": "", "exit_code": 0, "timed_out": False}]
        assert (view.status, view.phase, view.retry_safety) == ("completed", "terminal", "side_effectful")

    def test_code_view_unwraps_anthropic_input_object(self) -> None:
        call = Content.from_code_interpreter_tool_call(
            call_id="code_1",
            inputs=[Content.from_text('{"code":"print(1)"}')],
            hosted_provider="anthropic",
            provider_phase="start",
            provider_status="running",
        )

        view = adapt_hosted_tool(call)

        assert view.arguments == {"code": "print(1)"}

    def test_search_view_serializes_structured_result_as_json(self) -> None:
        payload = {"results": [{"title": "Chrys", "url": "https://example.test/chrys"}]}

        view = adapt_hosted_tool(_search_call(), _search_result(result=payload))

        assert json.loads(view.result_text) == payload

    def test_bare_image_media_type_counts_as_image_not_artifact(self) -> None:
        # OpenAI code outputs tag URL images with the bare "image" media type;
        # the extraction predicate accepts it, so the artifact predicate must
        # not claim the same content.
        plot = Content.from_uri("https://files.test/plot.png", media_type="image")

        view = adapt_hosted_tool(_search_call(), _search_result(result=[plot]))

        assert view.image_contents == [plot]
        assert view.artifacts == []

    def test_failed_view_matches_existing_failure_contract(self) -> None:
        result = _search_result(
            status="failed",
            result="quota exceeded",
            additional_properties={"is_error": True, "error": {"error_code": "rate_limit"}},
        )

        view = adapt_hosted_tool(_search_call(), result)

        assert view.status == "failed"
        assert view.result_text == "Error: quota exceeded"
        assert view.metadata[TOOL_ERRORED_METADATA_KEY] is True
        assert view.metadata[TOOL_ERROR_CODE_METADATA_KEY] == "rate_limit"
        assert tool_result_metadata_failure_state(view.metadata) is True

    @pytest.mark.parametrize(
        ("provider_text", "provider_status", "expected_text", "expected_synthesized"),
        [
            ("quota exceeded", "", "Error: quota exceeded", False),
            ("", "failed", "Error: failed", False),
            ("", "", "Error: Provider-hosted tool failed.", True),
        ],
        ids=["provider-text", "provider-status", "blank-provider-payload"],
    )
    def test_failure_fallback_metadata_identifies_only_synthesized_text(
        self,
        provider_text: str,
        provider_status: str,
        expected_text: str,
        expected_synthesized: bool,
    ) -> None:
        result = _search_result(
            status=provider_status,
            result=provider_text,
            additional_properties={"is_error": True},
        )

        view = adapt_hosted_tool(_search_call(status=""), result)

        assert view.result_text == expected_text
        assert (view.metadata.get(TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY) is True) is expected_synthesized


class TestResponsePresentationPlan:
    def test_text_call_result_text_plans_only_the_card_and_the_final_text(self) -> None:
        plan = ResponsePresentationPlan.from_messages(
            [_assistant([Content.from_text("before"), _search_call(), _search_result(), Content.from_text("after")])]
        )

        assert _operation_types(plan) == [
            HostedToolStartOp,
            HostedToolArgsOp,
            HostedToolStatusOp,
            HostedToolArgsOp,
            HostedToolResultOp,
        ]
        assert plan.final_text == "after"

    def test_mixed_transcript_plans_only_hosted_card_operations(self) -> None:
        local = Content.from_function_call("local_1", "read_file", arguments={"path": "x"})
        plan = ResponsePresentationPlan.from_messages(
            [
                _assistant(
                    [
                        Content.from_text_reasoning(id="reasoning_1", text="thinking"),
                        Content.from_text("Let me look. "),
                        local,
                    ]
                ),
                Message(role="tool", contents=[Content.from_function_result(call_id="local_1", result="contents")]),
                _assistant(
                    [Content.from_text("Searching. "), _search_call(), _search_result(), Content.from_text("ok")]
                ),
            ]
        )

        assert plan.operations
        assert all(isinstance(operation, HOSTED_OPERATION_TYPES) for operation in plan.operations)
        assert {operation.view.tool_name for operation in plan.operations} == {"web_search"}
        assert plan.final_text == "ok"

    def test_standalone_hosted_result_preserves_its_payload(self) -> None:
        result = Content.from_hosted_tool_result(
            "standalone",
            tool_name="server_task",
            result="valuable output",
            status="completed",
            hosted_family="generic",
            hosted_provider="openai",
            provider_phase=HostedToolPhase.TERMINAL,
            provider_status="completed",
        )

        plan = ResponsePresentationPlan.from_messages([_assistant([result])])

        result_operation = next(operation for operation in plan.operations if isinstance(operation, HostedToolResultOp))
        assert result_operation.view.result_text == "valuable output"
        assert plan.structured_output_completed is True

    def test_text_between_call_and_result_is_not_final(self) -> None:
        plan = ResponsePresentationPlan.from_messages(
            [_assistant([_search_call(), Content.from_text("working"), _search_result(), Content.from_text("answer")])]
        )

        assert plan.final_text == "answer"

    def test_running_call_at_end_has_empty_final_text(self) -> None:
        plan = ResponsePresentationPlan.from_messages(
            [_assistant([_search_call(), Content.from_text("still working")])]
        )

        assert plan.final_text == ""

    def test_completed_image_without_text_is_successful_structured_output(self) -> None:
        call = Content.from_image_generation_tool_call(
            image_id="img_1",
            hosted_provider="openai",
            provider_phase=HostedToolPhase.START,
            provider_status="generating",
        )
        result = Content.from_image_generation_tool_result(
            image_id="img_1",
            outputs=[Content.from_uri("data:image/png;base64,AAA", media_type="image/png")],
            hosted_provider="openai",
            provider_phase=HostedToolPhase.TERMINAL,
            provider_status="completed",
        )

        plan = ResponsePresentationPlan.from_messages([_assistant([call, result, Content.from_text("")])])

        assert plan.final_text == ""
        assert plan.structured_output_completed is True

    def test_no_hosted_contents_match_extract_final_text_behavior(self) -> None:
        messages = [
            _assistant([Content.from_text("old")]),
            _assistant([Content.from_text("last "), Content.from_text("answer")]),
        ]

        plan = ResponsePresentationPlan.from_messages(messages)

        assert plan.operations == []
        assert plan.final_text == "last answer"

    def test_local_function_call_is_only_a_text_boundary(self) -> None:
        local = Content.from_function_call("local_1", "read_file", arguments={"path": "x"})
        plan = ResponsePresentationPlan.from_messages(
            [
                _assistant(
                    [
                        _search_call(),
                        _search_result(),
                        Content.from_text("before local"),
                        local,
                        _search_call("search_2", item_id="item_2"),
                        _search_result("search_2", item_id="item_2"),
                        Content.from_text("answer"),
                    ]
                )
            ]
        )

        starts = [operation for operation in plan.operations if isinstance(operation, HostedToolStartOp)]
        assert len(starts) == 2
        assert all(operation.view.tool_name != "read_file" for operation in starts)
        assert plan.final_text == "answer"

    def test_final_text_after_local_tool_follow_up_stays_final(self) -> None:
        # Run-completion reconciles span the whole transcript: local calls,
        # their tool-role results, and the post-tool follow-up response.
        local = Content.from_function_call("local_1", "read_file", arguments={"path": "x"})
        plan = ResponsePresentationPlan.from_messages(
            [
                _assistant([_search_call(), _search_result(), Content.from_text("checking"), local]),
                Message(role="tool", contents=[Content.from_function_result(call_id="local_1", result="contents")]),
                _assistant([Content.from_text("Final answer.")]),
            ]
        )

        assert plan.final_text == "Final answer."

    def test_text_between_local_call_and_its_result_is_not_final(self) -> None:
        local = Content.from_function_call("local_1", "read_file", arguments={"path": "x"})
        plan = ResponsePresentationPlan.from_messages(
            [
                _assistant([_search_call(), _search_result(), local, Content.from_text("running it")]),
                Message(role="tool", contents=[Content.from_function_result(call_id="local_1", result="contents")]),
                _assistant([Content.from_text("Done.")]),
            ]
        )

        assert plan.final_text == "Done."

    def test_informational_function_call_requires_explicit_hosted_marker(self) -> None:
        old_call = Content.from_function_call("old_1", "legacy_hosted", informational_only=True)
        unmarked = ResponsePresentationPlan.from_messages([_assistant([old_call, Content.from_text("answer")])])
        marked_call = Content.from_function_call("new_1", "server_tool", informational_only=True)
        marked_call.provider_hosted = True
        marked_call.hosted_family = "generic"
        marked_call.provider_phase = "start"
        marked = ResponsePresentationPlan.from_messages([_assistant([marked_call])])

        assert unmarked.operations == []
        assert unmarked.final_text == "answer"
        assert any(isinstance(operation, HostedToolStartOp) for operation in marked.operations)
        assert marked.final_text == ""

    def test_embedded_result_before_call_emits_semantic_start_then_result_once(self) -> None:
        call = _search_call()
        result = _search_result()

        plan = ResponsePresentationPlan.from_messages([_assistant([result, call, Content.from_text("answer")])])

        starts = [operation for operation in plan.operations if isinstance(operation, HostedToolStartOp)]
        results = [operation for operation in plan.operations if isinstance(operation, HostedToolResultOp)]
        assert len(starts) == len(results) == 1
        assert plan.operations.index(starts[0]) < plan.operations.index(results[0])
        assert plan.final_text == "answer"


class TestHostedPresentationBridgeStreaming:
    @pytest.fixture
    def sink(self) -> tuple[list[object], object]:
        operations: list[object] = []

        async def publish(operation: object) -> None:
            operations.append(operation)

        return operations, publish

    async def test_call_first_publishes_live(self, sink: tuple[list[object], object]) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish, run_generation=3)
        await bridge.attempt_started()

        await bridge.observe_contents([_search_call()])

        assert [type(operation) for operation in operations] == [
            HostedToolStartOp,
            HostedToolArgsOp,
            HostedToolStatusOp,
        ]
        assert operations[0].presentation_id == "hosted:3:0:0:0"

    async def test_local_only_segmented_text_publishes_nothing(self, sink: tuple[list[object], object]) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()
        text = Content.from_text("Before local tool.")
        local_call = Content.from_function_call("local_1", "read_file", arguments={"path": "README.md"})

        await bridge.observe_contents([text, local_call])
        await bridge.attempt_accepted([_assistant([text, local_call])])

        assert operations == []

    async def test_terminal_item_arguments_publish_before_the_result(self, sink: tuple[list[object], object]) -> None:
        # OpenAI web_search announces the action only on the terminal item —
        # the start streams with empty arguments.
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()
        bare = _search_call()
        bare.arguments = None
        await bridge.observe_contents([bare])
        assert not any(isinstance(operation, HostedToolArgsOp) for operation in operations)

        await bridge.observe_contents(
            [_search_call(phase=HostedToolPhase.TERMINAL, status="completed"), _search_result()]
        )

        types = [type(operation) for operation in operations]
        args_index = types.index(HostedToolArgsOp)
        assert args_index < types.index(HostedToolResultOp)
        assert operations[args_index].view.arguments == {"query": "Chrys"}

    async def test_text_before_a_hosted_call_does_not_hold_its_card(self, sink: tuple[list[object], object]) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()
        await bridge.observe_contents([Content.from_text("before"), _search_call()])
        assert [type(operation) for operation in operations] == [
            HostedToolStartOp,
            HostedToolArgsOp,
            HostedToolStatusOp,
        ]

        await bridge.reconcile_accepted(
            [_assistant([Content.from_text("before"), _search_call(), _search_result(), Content.from_text("answer")])]
        )

        assert [type(operation) for operation in operations] == [
            HostedToolStartOp,
            HostedToolArgsOp,
            HostedToolStatusOp,
            HostedToolResultOp,
            FinalTextOp,
        ]
        assert operations[-1] == FinalTextOp("answer")

    async def test_bridge_emits_only_hosted_card_operations_and_final_text(
        self, sink: tuple[list[object], object]
    ) -> None:
        # Model text belongs to the run's content projector: narration around
        # local and hosted calls never becomes a bridge operation.
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        local_call = Content.from_function_call("local_1", "read_file", arguments={"path": "README.md"})
        first = _assistant(
            [
                Content.from_text_reasoning(id="reasoning_1", text="thinking"),
                Content.from_text("Let me look. "),
                local_call,
            ]
        )

        await bridge.begin_response()
        await bridge.attempt_started()
        for content in first.contents:
            await bridge.observe_contents([content])
        await bridge.local_call_start_published("local_1")
        await bridge.attempt_accepted([first])
        assert operations == []

        second = _assistant(
            [
                Content.from_text("Searching. "),
                _search_call(),
                Content.from_text("Still searching. "),
                _search_result(),
                Content.from_usage(usage_details={"input_token_count": 1}),
                Content.from_text("answer"),
            ]
        )
        await bridge.begin_response()
        await bridge.attempt_started()
        await bridge.observe_contents(second.contents, is_final=True)
        await bridge.attempt_accepted([second])
        await bridge.reconcile_accepted(
            [first, Message("tool", [Content.from_function_result("local_1", result="ok")]), second],
            final=True,
        )

        assert all(isinstance(operation, (*HOSTED_OPERATION_TYPES, FinalTextOp)) for operation in operations)
        assert any(isinstance(operation, HostedToolResultOp) for operation in operations)
        assert [operation for operation in operations if isinstance(operation, FinalTextOp)] == [FinalTextOp("answer")]
        assert operations[-1] == FinalTextOp("answer")

    async def test_interrupted_attempt_terminalizes_visible_card_as_interrupted(
        self, sink: tuple[list[object], object]
    ) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()

        await bridge.observe_contents([Content.from_text("Checking sources."), _search_call()])
        await bridge.attempt_rejected("Execution interrupted", status=HostedToolStatus.INTERRUPTED)

        assert all(isinstance(operation, HOSTED_OPERATION_TYPES) for operation in operations)
        terminal = operations[-1]
        assert isinstance(terminal, HostedToolStatusOp)
        assert terminal.presentation_id == operations[0].presentation_id
        assert (terminal.view.status, terminal.view.provider_status) == (
            HostedToolStatus.INTERRUPTED,
            HostedToolStatus.INTERRUPTED,
        )
        assert tool_result_metadata_failure_state(terminal.view.metadata) is not True

    @pytest.mark.parametrize(
        "non_ordering_content",
        [
            Content.from_usage(usage_details={"input_token_count": 1}),
            Content.from_text_reasoning(id="reasoning_1", text="internal reasoning"),
        ],
        ids=["usage", "reasoning"],
    )
    async def test_text_and_non_ordering_content_publish_nothing_before_final_reconcile(
        self,
        sink: tuple[list[object], object],
        non_ordering_content: Content,
    ) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()
        streamed = Content.from_text("Final answer.")
        canonical = Content.from_text("Final answer.")
        messages = [_assistant([canonical])]

        await bridge.observe_contents([streamed])
        await bridge.observe_contents([non_ordering_content])
        await bridge.attempt_accepted(messages)

        assert operations == []

        await bridge.reconcile_accepted(messages, final=True)

        assert operations == [FinalTextOp("Final answer.")]

    async def test_trailing_stream_text_stays_final_until_reconciliation(
        self, sink: tuple[list[object], object]
    ) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()
        call = _search_call()
        result = _search_result()
        await bridge.observe_contents([call, result])
        live_count = len(operations)
        final = Content.from_text("Final answer.")

        await bridge.observe_contents([final])
        assert len(operations) == live_count

        await bridge.reconcile_accepted([_assistant([call, result, final])])
        assert operations[-1] == FinalTextOp("Final answer.")

    async def test_landed_text_between_hosted_outputs_holds_later_cards_until_it_shows(
        self, sink: tuple[list[object], object]
    ) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()
        first_call = _search_call("first", item_id="first")
        first_result = _search_result("first", item_id="first")
        between = Content.from_text("Between searches.")
        second_call = _search_call("second", item_id="second")
        second_result = _search_result("second", item_id="second")
        messages = [
            _assistant(
                [first_call, first_result, between, second_call, second_result, Content.from_text("Final answer.")]
            )
        ]

        def cards() -> tuple[list[int], list[int]]:
            types = [type(operation) for operation in operations]
            starts = [index for index, operation_type in enumerate(types) if operation_type is HostedToolStartOp]
            results = [index for index, operation_type in enumerate(types) if operation_type is HostedToolResultOp]
            return starts, results

        await bridge.observe_contents(messages[0].contents, is_final=True)
        starts, results = cards()
        assert len(starts) == len(results) == 1
        await bridge.text_shown(between)

        starts, results = cards()
        assert len(starts) == len(results) == 2
        assert starts[0] < results[0] < starts[1] < results[1]
        live_count = len(operations)

        await bridge.reconcile_accepted(messages)

        assert len(operations) == live_count + 1
        assert operations[-1] == FinalTextOp("Final answer.")

    async def test_final_reconcile_releases_barriers_for_never_started_calls(
        self, sink: tuple[list[object], object]
    ) -> None:
        # Unknown tools and pre-pipeline argument rejections never publish a
        # ToolEventMiddleware start; at run completion their barriers must not
        # gate the final text forever.
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()
        ghost = Content.from_function_call("ghost_1", "no_such_tool", arguments={})
        messages = [
            _assistant([_search_call(), _search_result(), ghost]),
            Message(
                role="tool",
                contents=[Content.from_function_result(call_id="ghost_1", result="Error: not found")],
            ),
            _assistant([Content.from_text("Recovered answer.")]),
        ]

        await bridge.reconcile_accepted(messages, final=True)

        assert isinstance(operations[-1], FinalTextOp)
        assert operations[-1].text == "Recovered answer."

    async def test_non_final_reconcile_still_gates_on_unstarted_calls(self, sink: tuple[list[object], object]) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()
        local = Content.from_function_call("local_1", "read_file", arguments={"path": "x"})

        await bridge.reconcile_accepted([_assistant([_search_call(), _search_result(), local])])
        assert not any(isinstance(operation, FinalTextOp) for operation in operations)

        await bridge.local_call_start_published("local_1")
        assert isinstance(operations[-1], FinalTextOp)

    async def test_reject_terminalizes_visible_card_as_failed(self, sink: tuple[list[object], object]) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()
        await bridge.observe_contents([_search_call(), Content.from_text("hidden")])

        await bridge.attempt_rejected("invalid response")

        assert all(isinstance(operation, HOSTED_OPERATION_TYPES) for operation in operations)
        terminal = operations[-1]
        assert isinstance(terminal, HostedToolStatusOp)
        assert terminal.view.status == "failed"
        assert terminal.view.result_text == "Error: invalid response"
        assert tool_result_metadata_failure_state(terminal.view.metadata) is True

    async def test_reject_without_reason_uses_terminal_status_not_synthesized_fallback(
        self, sink: tuple[list[object], object]
    ) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()
        await bridge.observe_contents([_search_call()])

        await bridge.attempt_rejected("")

        terminal = operations[-1]
        assert isinstance(terminal, HostedToolStatusOp)
        assert terminal.view.result_text == "Error: failed"
        assert TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY not in terminal.view.metadata

    async def test_accept_publishes_only_unpublished_suffix(self, sink: tuple[list[object], object]) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()
        call = _search_call()
        result = _search_result()
        await bridge.observe_contents([call, result])
        live_count = len(operations)

        await bridge.reconcile_accepted([_assistant([call, result, Content.from_text("answer")])])

        assert sum(isinstance(operation, HostedToolStartOp) for operation in operations) == 1
        assert sum(isinstance(operation, HostedToolResultOp) for operation in operations) == 1
        assert len(operations) == live_count + 1
        assert operations[-1] == FinalTextOp("answer")

    async def test_tool_loop_response_acceptance_leaves_final_text_to_run_completion(
        self, sink: tuple[list[object], object]
    ) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        first_call = _search_call("first", item_id="first")
        first_result = _search_result("first", item_id="first")
        local_call = Content.from_function_call("local_1", "read_file", arguments={"path": "README.md"})
        first = _assistant([first_call, first_result, Content.from_text("Checking details."), local_call])

        await bridge.begin_response()
        await bridge.attempt_started()
        await bridge.observe_contents(first.contents, is_final=True)
        await bridge.attempt_accepted([first])

        first_count = len(operations)
        assert not any(isinstance(operation, FinalTextOp) for operation in operations)

        second_call = _search_call("second", item_id="second")
        second_result = _search_result("second", item_id="second")
        second = _assistant([second_call, second_result, Content.from_text("answer")])
        await bridge.begin_response()
        await bridge.attempt_started()
        await bridge.observe_contents(second.contents, is_final=True)
        await bridge.attempt_accepted([second])

        starts = [index for index, operation in enumerate(operations) if isinstance(operation, HostedToolStartOp)]
        assert len(starts) == 2
        assert starts[0] < first_count <= starts[1]
        assert not any(isinstance(operation, FinalTextOp) for operation in operations)

        await bridge.reconcile_accepted(
            [
                first,
                Message("tool", [Content.from_function_result("local_1", result="ok")]),
                second,
            ],
            final=True,
        )
        assert sum(isinstance(operation, FinalTextOp) for operation in operations) == 1
        assert operations[-1] == FinalTextOp("answer")

    async def test_local_call_barrier_holds_later_hosted_ops(self, sink: tuple[list[object], object]) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()
        local = Content.from_function_call("local_1", "read_file", arguments={"path": "x"})

        await bridge.observe_contents([local, _search_call()])
        assert operations == []

        await bridge.local_call_start_published("local_1")

        assert isinstance(operations[0], HostedToolStartOp)

    async def test_reused_local_call_id_does_not_release_later_response_barrier(
        self, sink: tuple[list[object], object]
    ) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        first_local = Content.from_function_call("reused", "read_file", arguments={"path": "first"})

        await bridge.begin_response()
        await bridge.attempt_started()
        await bridge.observe_contents([first_local], is_final=True)
        await bridge.local_call_start_published("reused")
        await bridge.attempt_accepted([_assistant([first_local])])

        second_local = Content.from_function_call("reused", "read_file", arguments={"path": "second"})
        second_call = _search_call("second", item_id="second")
        second_result = _search_result("second", item_id="second")
        second = _assistant([second_local, second_call, second_result])
        await bridge.begin_response()
        await bridge.attempt_started()
        await bridge.observe_contents(second.contents, is_final=True)
        await bridge.attempt_accepted([second])

        assert not any(isinstance(operation, HostedToolStartOp) for operation in operations)

        await bridge.local_call_start_published("reused")

        assert isinstance(operations[0], HostedToolStartOp)

    async def test_reused_hosted_call_id_reconciles_to_the_current_response(
        self, sink: tuple[list[object], object]
    ) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        first_call = _search_call("reused", item_id="")
        first_call.arguments = {"query": "first"}
        first_result = _search_result("reused", item_id="", result="one")

        await bridge.begin_response()
        await bridge.attempt_started()
        await bridge.observe_contents([first_call, first_result], is_final=True)
        await bridge.attempt_accepted([_assistant([first_call, first_result])])

        second_call = _search_call("reused", item_id="")
        second_call.arguments = {"query": "second"}
        second_result = _search_result("reused", item_id="", result="two")
        await bridge.begin_response()
        await bridge.attempt_started()
        await bridge.observe_contents([second_call, second_result], is_final=True)
        await bridge.attempt_accepted([_assistant([second_call, second_result])])

        starts = [operation for operation in operations if isinstance(operation, HostedToolStartOp)]
        assert len(starts) == 2
        first_id, second_id = (operation.presentation_id for operation in starts)
        assert {
            (operation.presentation_id, operation.view.arguments["query"])
            for operation in operations
            if isinstance(operation, HostedToolArgsOp)
        } == {(first_id, "first"), (second_id, "second")}
        assert {
            (operation.presentation_id, operation.view.result_text)
            for operation in operations
            if isinstance(operation, HostedToolResultOp)
        } == {(first_id, "one"), (second_id, "two")}

    async def test_final_reconciliation_matches_reused_hosted_call_ids_fifo(
        self, sink: tuple[list[object], object]
    ) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        messages: list[Message] = []
        for query, result in [("first", "one"), ("second", "two")]:
            call = _search_call("reused", item_id="")
            call.arguments = {"query": query}
            call_result = _search_result("reused", item_id="", result=result)
            message = _assistant([call, call_result])
            messages.append(message)
            await bridge.begin_response()
            await bridge.attempt_started()
            await bridge.observe_contents(message.contents, is_final=True)
            await bridge.attempt_accepted([message])

        hosted_operation_count = sum(
            isinstance(operation, HostedToolStartOp | HostedToolArgsOp | HostedToolResultOp) for operation in operations
        )
        await bridge.reconcile_accepted([*messages, _assistant([Content.from_text("answer")])], final=True)

        assert (
            sum(
                isinstance(operation, HostedToolStartOp | HostedToolArgsOp | HostedToolResultOp)
                for operation in operations
            )
            == hosted_operation_count
        )
        assert operations[-1] == FinalTextOp("answer")

    async def test_occurrence_ordinals_never_reuse_across_attempts_or_response_reset(
        self, sink: tuple[list[object], object]
    ) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish, run_generation=7)
        await bridge.attempt_started()
        await bridge.observe_contents([_search_call("one", item_id="one")])
        await bridge.attempt_rejected("retry")
        await bridge.attempt_started()
        await bridge.observe_contents([_search_call("two", item_id="two")])
        await bridge.begin_response(response_index=0)
        await bridge.attempt_started()
        await bridge.observe_contents([_search_call("three", item_id="three")])

        starts = [operation for operation in operations if isinstance(operation, HostedToolStartOp)]
        assert [operation.occurrence_ordinal for operation in starts] == [0, 1, 2]
        assert [operation.presentation_id for operation in starts] == [
            "hosted:7:0:0:0",
            "hosted:7:1:0:1",
            "hosted:7:0:0:2",
        ]

    async def test_final_reconciliation_reuses_accepted_responses_and_local_barriers(
        self, sink: tuple[list[object], object]
    ) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        first_call = _search_call("first", item_id="first")
        first_result = _search_result("first", item_id="first")
        local_call = Content.from_function_call("local_1", "read_file", arguments={"path": "x"})

        await bridge.begin_response()
        await bridge.attempt_started()
        await bridge.observe_contents([first_call, first_result, local_call])
        await bridge.local_call_start_published("local_1")
        await bridge.attempt_accepted()

        second_call = _search_call("second", item_id="second")
        second_result = _search_result("second", item_id="second")
        await bridge.begin_response()
        await bridge.attempt_started()
        await bridge.observe_contents([second_call, second_result])
        await bridge.attempt_accepted()
        await bridge.reconcile_accepted(
            [
                _assistant([first_call, first_result, local_call]),
                Message("tool", [Content.from_function_result("local_1", result="ok")]),
                _assistant([second_call, second_result, Content.from_text("answer")]),
            ]
        )

        assert sum(isinstance(operation, HostedToolStartOp) for operation in operations) == 2
        assert operations[-1] == FinalTextOp("answer")

    async def test_continuation_reuses_provider_item_occurrence(self, sink: tuple[list[object], object]) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()
        await bridge.observe_contents([_search_call()])
        await bridge.attempt_started(continuation=True)
        await bridge.observe_contents([_search_result()])

        starts = [operation for operation in operations if isinstance(operation, HostedToolStartOp)]
        results = [operation for operation in operations if isinstance(operation, HostedToolResultOp)]
        assert len(starts) == len(results) == 1
        assert starts[0].presentation_id == results[0].presentation_id

    async def test_terminal_call_reemission_keeps_provider_item_occurrence(
        self, sink: tuple[list[object], object]
    ) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()
        call = _search_call()
        result = _search_result()

        await bridge.observe_contents([call])
        call.provider_phase = HostedToolPhase.SNAPSHOT
        call.provider_status = "completed"
        await bridge.observe_contents([call])
        call.provider_phase = HostedToolPhase.TERMINAL
        await bridge.observe_contents([call, result])

        starts = [operation for operation in operations if isinstance(operation, HostedToolStartOp)]
        results = [operation for operation in operations if isinstance(operation, HostedToolResultOp)]
        assert len(starts) == len(results) == 1
        assert {operation.presentation_id for operation in operations} == {starts[0].presentation_id}

    async def test_terminal_call_reemission_keeps_pairing_key_occurrence(
        self, sink: tuple[list[object], object]
    ) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()
        call = _search_call(item_id="")
        result = _search_result(item_id="")

        await bridge.observe_contents([call])
        call.provider_phase = HostedToolPhase.SNAPSHOT
        call.provider_status = "completed"
        await bridge.observe_contents([call])
        call.provider_phase = HostedToolPhase.TERMINAL
        await bridge.observe_contents([call, result])

        starts = [operation for operation in operations if isinstance(operation, HostedToolStartOp)]
        results = [operation for operation in operations if isinstance(operation, HostedToolResultOp)]
        assert len(starts) == len(results) == 1
        assert {operation.presentation_id for operation in operations} == {starts[0].presentation_id}

    async def test_result_before_call_does_not_restart_or_revive_occurrence(
        self, sink: tuple[list[object], object]
    ) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()

        await bridge.observe_contents([_search_result(), _search_call()])

        assert sum(isinstance(operation, HostedToolStartOp) for operation in operations) == 1
        assert sum(isinstance(operation, HostedToolResultOp) for operation in operations) == 1
        assert not any(
            isinstance(operation, HostedToolStatusOp) and operation.view.status == "running" for operation in operations
        )

    async def test_image_partials_reuse_provider_item_occurrence_until_terminal(
        self, sink: tuple[list[object], object]
    ) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()
        call = Content.from_image_generation_tool_call(
            image_id="img_1",
            hosted_provider="openai",
            provider_item_id="img_1",
            provider_phase="start",
            provider_status="generating",
        )
        partial = Content.from_image_generation_tool_result(
            image_id="img_1",
            outputs=[Content.from_uri("data:image/png;base64,AAA", media_type="image/png")],
            hosted_provider="openai",
            provider_item_id="img_1",
            provider_phase="snapshot",
            provider_status="generating",
        )

        await bridge.observe_contents([call, partial])

        partial.outputs = [Content.from_uri("data:image/png;base64,BBB", media_type="image/png")]
        await bridge.observe_contents([partial])
        partial.provider_phase = HostedToolPhase.TERMINAL
        partial.provider_status = "completed"
        await bridge.observe_contents([partial])

        starts = [operation for operation in operations if isinstance(operation, HostedToolStartOp)]
        progress = [operation for operation in operations if isinstance(operation, HostedToolProgressOp)]
        results = [operation for operation in operations if isinstance(operation, HostedToolResultOp)]
        assert len(starts) == len(results) == 1
        assert len(progress) == 2
        assert {operation.presentation_id for operation in operations} == {starts[0].presentation_id}
        assert bridge.published_terminal_signatures

    async def test_failed_result_has_structured_failure_metadata(self, sink: tuple[list[object], object]) -> None:
        operations, publish = sink
        bridge = HostedPresentationBridge(publish)
        await bridge.attempt_started()

        await bridge.observe_contents(
            [
                _search_call(),
                _search_result(
                    status="failed",
                    result="provider failed",
                    additional_properties={"is_error": True, "error": {"code": "bad_query"}},
                ),
            ]
        )

        result = next(operation for operation in operations if isinstance(operation, HostedToolResultOp))
        assert result.view.status == "failed"
        assert result.view.provider_status == "failed"
        assert result.view.result_text.startswith("Error: ")
        assert result.view.metadata[TOOL_ERROR_CODE_METADATA_KEY] == "bad_query"
        assert tool_result_metadata_failure_state(result.view.metadata) is True


class TestHostedPresentationBridgeBlocking:
    async def test_cards_publish_before_final_text_last(self) -> None:
        operations: list[object] = []
        bridge = HostedPresentationBridge(operations.append)

        await _present_blocking(
            bridge,
            [_assistant([Content.from_text("before"), _search_call(), _search_result(), Content.from_text("answer")])],
        )

        assert [type(operation) for operation in operations] == [
            HostedToolStartOp,
            HostedToolArgsOp,
            HostedToolStatusOp,
            HostedToolResultOp,
            FinalTextOp,
        ]
        assert operations[-1] == FinalTextOp("answer")

    async def test_cards_after_landed_text_wait_until_that_text_shows(self) -> None:
        operations: list[object] = []
        bridge = HostedPresentationBridge(operations.append)
        before, answer = Content.from_text("before"), Content.from_text("answer")
        message = _assistant([before, _search_call(), _search_result(), answer])

        await bridge.attempt_started()
        await bridge.observe_contents(message.contents, is_final=True)
        await bridge.attempt_accepted([message])
        # The text the call follows has not shown yet.
        assert operations == []
        await bridge.text_shown(before)
        cards = [HostedToolStartOp, HostedToolArgsOp, HostedToolStatusOp, HostedToolResultOp]
        assert [type(operation) for operation in operations] == cards
        await bridge.text_shown(answer)
        await bridge.text_shown(None)
        assert [type(operation) for operation in operations] == cards
        await bridge.reconcile_accepted([message], final=True)
        assert [type(operation) for operation in operations] == [*cards, FinalTextOp]

    @pytest.mark.parametrize("lead", ["text", "local_call"])
    async def test_a_rejected_response_shows_its_cards_though_what_they_follow_never_shows(self, lead: str) -> None:
        operations: list[object] = []
        bridge = HostedPresentationBridge(operations.append)
        before = (
            Content.from_text("before")
            if lead == "text"
            else Content.from_function_call("local_1", "read_file", arguments={"path": "x"})
        )

        await bridge.attempt_started()
        await bridge.observe_contents([before, _search_call()], is_final=True)
        assert operations == []
        await bridge.attempt_rejected("rejected")

        # The card shows running, then ends as failed with the response.
        assert [type(operation) for operation in operations] == [
            HostedToolStartOp,
            HostedToolArgsOp,
            HostedToolStatusOp,
            HostedToolStatusOp,
        ]
        terminal = operations[-1]
        assert isinstance(terminal, HostedToolStatusOp) and terminal.view.status == HostedToolStatus.FAILED

    @pytest.mark.parametrize("restart", ["attempt", "response"])
    async def test_a_new_attempt_shows_the_cards_the_last_one_held(self, restart: str) -> None:
        operations: list[object] = []
        bridge = HostedPresentationBridge(operations.append)

        await bridge.attempt_started()
        await bridge.observe_contents([Content.from_text("before"), _search_call()], is_final=True)
        assert operations == []
        # The landed response never reached its caller.
        if restart == "attempt":
            await bridge.attempt_started()
        else:
            await bridge.begin_response()

        assert [type(operation) for operation in operations] == [
            HostedToolStartOp,
            HostedToolArgsOp,
            HostedToolStatusOp,
        ]

    async def test_streamed_text_never_holds_the_cards_after_it(self) -> None:
        operations: list[object] = []
        bridge = HostedPresentationBridge(operations.append)

        await bridge.attempt_started()
        await bridge.observe_contents([Content.from_text("before"), _search_call()])

        assert [type(operation) for operation in operations][:1] == [HostedToolStartOp]

    @pytest.mark.parametrize("second", ["text_shown", "local_call_start"])
    async def test_a_call_from_another_task_waits_while_one_publishes(self, second: str) -> None:
        """An interrupt and the run, or two parallel tool calls, publish one after the other."""
        holding, release = asyncio.Event(), asyncio.Event()
        operations: list[object] = []
        published_while_held: list[object] = []

        async def publish(operation: object) -> None:
            if holding.is_set() and not release.is_set():
                published_while_held.append(operation)
            operations.append(operation)
            if isinstance(operation, HostedToolStartOp) and not holding.is_set():
                # A frontend still mounting the card when the card's next event arrives drops that event.
                holding.set()
                await release.wait()

        bridge = HostedPresentationBridge(publish)
        before = Content.from_text("before")
        if second == "text_shown":
            contents = [before, _search_call(), _search_result()]
        else:
            contents = [
                Content.from_function_call("local_a", "read_file", arguments={"path": "a"}),
                _search_call("search_1", item_id="item_1"),
                _search_result("search_1", item_id="item_1"),
                Content.from_function_call("local_b", "read_file", arguments={"path": "b"}),
                _search_call("search_2", item_id="item_2"),
                _search_result("search_2", item_id="item_2"),
            ]
        await bridge.attempt_started()
        await bridge.observe_contents(contents, is_final=True)
        assert operations == []
        first_call = (
            bridge.attempt_rejected("Execution interrupted", status=HostedToolStatus.INTERRUPTED)
            if second == "text_shown"
            else bridge.local_call_start_published("local_a")
        )
        first = asyncio.create_task(first_call)
        later = None
        try:
            await wait_for(lambda: holding.is_set() or first.done(), description="the first card starting")
            assert holding.is_set()
            later = asyncio.create_task(
                bridge.text_shown(before) if second == "text_shown" else bridge.local_call_start_published("local_b")
            )
            # One step runs the later call as far as it gets without waiting.
            await asyncio.sleep(0)
            assert published_while_held == []
            assert not later.done()
        finally:
            release.set()
            await asyncio.gather(first, *(task for task in (later,) if task is not None))

        cards = [HostedToolStartOp, HostedToolArgsOp, HostedToolStatusOp, HostedToolResultOp]
        assert [type(operation) for operation in operations] == (cards if second == "text_shown" else cards * 2)
        if second == "local_call_start":
            ids = [operation.presentation_id for operation in operations if isinstance(operation, HostedToolStartOp)]
            assert [operation.presentation_id for operation in operations] == [ids[0]] * 4 + [ids[1]] * 4

    async def test_empty_final_text_still_emits_final_last(self) -> None:
        operations: list[object] = []
        bridge = HostedPresentationBridge(operations.append)

        await _present_blocking(bridge, [_assistant([_search_call()])])

        assert operations[-1] == FinalTextOp("")

    async def test_completed_image_emits_structured_final_sentinel(self) -> None:
        operations: list[object] = []
        bridge = HostedPresentationBridge(operations.append)
        call = Content.from_image_generation_tool_call(
            image_id="img_1",
            hosted_provider="openai",
            provider_phase=HostedToolPhase.START,
            provider_status="generating",
        )
        result = Content.from_image_generation_tool_result(
            image_id="img_1",
            outputs=[Content.from_uri("data:image/png;base64,AAA", media_type="image/png")],
            hosted_provider="openai",
            provider_phase=HostedToolPhase.TERMINAL,
            provider_status="completed",
        )

        await _present_blocking(bridge, [_assistant([call, result, Content.from_text("")])])

        assert operations[-1] == FinalTextOp("", structured_output_completed=True)


class TestHostedEventContract:
    @pytest.mark.parametrize(
        "event_type",
        [
            InvocationToolCallStart,
            InvocationToolCallArgsUpdated,
            InvocationToolCallProgress,
            InvocationToolCallResult,
            InvocationToolCallStart,
            InvocationToolCallResult,
        ],
    )
    def test_extended_events_keep_old_publisher_defaults(self, event_type: type[object]) -> None:
        event = event_type(origin=InvocationOrigin("turn", "", "turn-test", None))
        assert event.provider_hosted is False
        assert event.hosted_family == ""
        assert event.provider == ""
        assert event.provider_item_type == ""
        assert event.provider_call_id == ""
        assert event.provider_status == ""
        if isinstance(event, InvocationToolCallResult):
            assert event.artifacts == []

    @pytest.mark.parametrize(
        "event",
        [
            InvocationToolCallStatusUpdated(
                tool_name="web_search",
                call_id="hosted:1:0:0:0",
                status="running",
                provider_status="searching",
                provider_hosted=True,
                hosted_family="search",
                provider="openai",
                metadata={"provider_item_id": "item_1"},
                origin=InvocationOrigin("turn", "", "turn-test", None),
            ),
            InvocationToolCallArgsUpdated(
                agent_name="Explore",
                tool_name="web_search",
                call_id="hosted:1:0:0:0",
                args={"query": "x"},
                provider_hosted=True,
                hosted_family="search",
                provider_item_type="web_search_call",
                provider_call_id="search_1",
                origin=InvocationOrigin("sub_agent", "", "inv_1", None),
            ),
            InvocationToolCallStatusUpdated(
                agent_name="Explore",
                status="failed",
                provider_status="incomplete",
                metadata={TOOL_ERRORED_METADATA_KEY: True},
                origin=InvocationOrigin("sub_agent", "", "inv_1", None),
            ),
            InvocationToolCallProgress(
                image_contents=[Content.from_uri("data:image/png;base64,AAA", media_type="image/png")],
                snapshot_metadata={"partial_index": 0},
                provider_hosted=True,
                hosted_family="image",
                origin=InvocationOrigin("turn", "", "turn-test", None),
            ),
            InvocationToolCallProgress(
                agent_name="Explore",
                lines=["running"],
                snapshot_metadata={"stream": "stdout"},
                provider_hosted=True,
                hosted_family="code",
                origin=InvocationOrigin("sub_agent", "", "inv_1", None),
            ),
            InvocationToolCallResult(
                result="done",
                artifacts=[{"id": "file_1", "mime": "text/csv", "size": 12}],
                provider_hosted=True,
                hosted_family="code",
                origin=InvocationOrigin("turn", "", "turn-test", None),
            ),
        ],
    )
    def test_event_fields_round_trip(self, event: object) -> None:
        payload = asdict(event)
        payload["origin"] = InvocationOrigin(**payload["origin"])
        restored = type(event)(**payload)
        assert asdict(restored) == asdict(event)


class TestSubAgentHostedPresentation:
    async def test_sub_agent_sink_matches_main_hosted_lifecycle_fields(self) -> None:
        bus = EventBus()
        events: list[Any] = []

        async def capture(event: Any) -> None:
            events.append(event)

        for event_type in (
            InvocationToolCallStart,
            InvocationToolCallArgsUpdated,
            InvocationToolCallStatusUpdated,
            InvocationToolCallProgress,
            InvocationToolCallResult,
        ):
            await bus.subscribe(event_type, capture)

        middleware = SubAgentEventMiddleware(
            bus,
            "Explore",
            "inv-1",
            session_id="session-1",
            origin=InvocationOrigin("sub_agent", "session-1", "inv-1", None),
        )
        bridge = middleware.begin_hosted_pass()
        await _present_blocking(bridge, [_assistant([_search_call(), _search_result()])])

        assert [type(event) for event in events] == [
            InvocationToolCallStart,
            InvocationToolCallArgsUpdated,
            InvocationToolCallStatusUpdated,
            InvocationToolCallResult,
        ]
        for event in events:
            assert event.call_id.startswith(f"hosted:{bridge.run_generation}:0:0:")
            assert event.provider_hosted is True
            assert event.hosted_family == "search"
            assert event.provider == "openai"
        assert events[0].provider_call_id == "search_1"
        assert events[-1].result == "found"

    async def test_a_sub_agent_hosted_call_counts_alike_live_and_in_the_totals_a_reopened_run_shows(self) -> None:
        """A sub-agent card or workflow node shows the live count while it runs, and the kept totals once reopened."""
        bus = EventBus()
        progress: list[InvocationProgress] = []

        async def capture(event: InvocationProgress) -> None:
            progress.append(event)

        await bus.subscribe(InvocationProgress, capture)
        stats = SubAgentLogStats()
        middleware = SubAgentEventMiddleware(
            bus, "Explore", "inv-1", stats=stats, origin=InvocationOrigin("sub_agent", "", "inv-1", None)
        )
        bridge = middleware.begin_hosted_pass()

        await _present_blocking(bridge, [_assistant([_search_call(), _search_result()])])

        # Start, arguments, status and result: one call, shown live as soon as it starts.
        assert [event.tool_call_count for event in progress] == [1]
        assert stats.totals().tool_calls == 1

    async def test_sub_agent_hosted_passes_never_share_a_generation(self) -> None:
        # Each sub-agent call builds a fresh middleware; its cards must not reuse an earlier call's ids.
        def middleware(invocation_id: str) -> SubAgentEventMiddleware:
            return SubAgentEventMiddleware(
                EventBus(), "Explore", invocation_id, origin=InvocationOrigin("sub_agent", "", invocation_id, None)
            )

        generations = [middleware("inv-1").begin_hosted_pass().run_generation for _ in range(2)]
        generations.append(middleware("inv-2").begin_hosted_pass().run_generation)

        assert len(set(generations)) == 3

    async def test_sub_agent_retry_terminalizes_visible_hosted_card_as_interrupted(self) -> None:
        bus = EventBus()
        statuses: list[InvocationToolCallStatusUpdated] = []

        async def capture(event: InvocationToolCallStatusUpdated) -> None:
            statuses.append(event)

        await bus.subscribe(InvocationToolCallStatusUpdated, capture)
        middleware = SubAgentEventMiddleware(
            bus, "Explore", "inv-1", origin=InvocationOrigin("sub_agent", "", "inv-1", None)
        )
        bridge = middleware.begin_hosted_pass()

        await bridge.observe_contents([_search_call()])
        await middleware.reject_hosted_attempt("retrying")

        assert statuses[-1].status == "interrupted"
        assert statuses[-1].provider_status == "interrupted"
        assert statuses[-1].metadata["provider_item_type"] == "web_search_call"
        assert statuses[-1].metadata["provider_call_id"] == "search_1"

    async def test_pass_reconcile_releases_barriers_for_never_started_calls(self) -> None:
        # The controller reconciles once per completed pass; a call that never
        # entered the tool pipeline has no start callback, so its barrier must
        # not hold back the hosted events ordered behind it.
        bus = EventBus()
        events: list[Any] = []

        async def capture(event: Any) -> None:
            events.append(event)

        for event_type in (InvocationToolCallStart, InvocationToolCallResult):
            await bus.subscribe(event_type, capture)

        middleware = SubAgentEventMiddleware(
            bus,
            "Explore",
            "inv-1",
            session_id="session-1",
            origin=InvocationOrigin("sub_agent", "session-1", "inv-1", None),
        )
        bridge = middleware.begin_hosted_pass()
        await bridge.attempt_started()
        ghost = Content.from_function_call("ghost_1", "no_such_tool", arguments={})
        messages = [
            _assistant([ghost, _search_call(), _search_result()]),
            Message(
                role="tool",
                contents=[Content.from_function_result(call_id="ghost_1", result="Error: not found")],
            ),
        ]

        await middleware.reconcile_hosted_response(messages)

        assert any(
            (isinstance(event, InvocationToolCallStart) and event.origin.kind == "sub_agent") for event in events
        )
        result = next(
            event
            for event in events
            if (isinstance(event, InvocationToolCallResult) and event.origin.kind == "sub_agent")
        )
        assert result.result == "found"

    async def test_sub_agent_result_artifacts_keep_name_and_uri_separate(self) -> None:
        # OpenAI hosted_file artifacts carry only file_id and filename; the
        # descriptor must not promote the bare filename to "path", which
        # consumers turn into links.
        bus = EventBus()
        results: list[InvocationToolCallResult] = []

        async def capture(event: InvocationToolCallResult) -> None:
            results.append(event)

        await bus.subscribe(InvocationToolCallResult, capture)
        middleware = SubAgentEventMiddleware(
            bus,
            "Explore",
            "inv-1",
            session_id="session-1",
            origin=InvocationOrigin("sub_agent", "session-1", "inv-1", None),
        )
        bridge = middleware.begin_hosted_pass()
        hosted_file = Content.from_hosted_file("file_1", media_type="text/csv", name="report.csv")
        linked = Content.from_uri("https://files.test/plot.csv", media_type="text/csv")

        await _present_blocking(bridge, [_assistant([_search_call(), _search_result(result=[hosted_file, linked])])])

        assert results[-1].artifacts == [
            {"id": "file_1", "name": "report.csv", "mime": "text/csv"},
            {"path": "https://files.test/plot.csv", "mime": "text/csv"},
        ]


def test_artifact_descriptor_twins_stay_in_sync() -> None:
    # The executor and the sub-agent middleware each build artifact
    # descriptors from the same view; the two copies must not drift.
    from chrys.orchestration.engine.run.bindings import TurnBindings

    hosted_file = Content.from_hosted_file("file_1", media_type="text/csv", name="report.csv")
    linked = Content.from_uri("https://files.test/plot.csv", media_type="text/csv")
    view = adapt_hosted_tool(_search_call(), _search_result(result=[hosted_file, linked]))
    operation = HostedToolResultOp(0, "hosted:1", view, (0, 0, 0))

    descriptors = TurnBindings._artifact_descriptors(operation)
    assert descriptors == SubAgentEventMiddleware._artifact_descriptors(operation)
    assert descriptors == [
        {"id": "file_1", "name": "report.csv", "mime": "text/csv"},
        {"path": "https://files.test/plot.csv", "mime": "text/csv"},
    ]
