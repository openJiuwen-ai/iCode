# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ownership + compatibility pins for the kernel public-type façade."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import BaseModel

import chrys.kernel as kernel
from chrys.foundation.reasoning_origin import REASONING_ORIGIN_KEY, ReasoningOrigin
from chrys.kernel import _types as kernel_private_types
from chrys.kernel import types as kernel_types
from chrys.kernel._tool_expansion import _get_tool_expander, _set_tool_expander
from chrys.kernel.identity import ContentList

OWNED_TYPE_NAMES = [
    "OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY",
    "RETRY_BOUNDARY_UPDATE_KEY",
    "AgentResponse",
    "AgentResponseUpdate",
    "AgentRunInputs",
    "Annotation",
    "BaseChatClient",
    "ChatOptions",
    "ChatResponse",
    "ChatResponseUpdate",
    "Content",
    "ContinuationToken",
    "FinishReason",
    "FinishReasonLiteral",
    "Message",
    "ResponseStream",
    "Role",
    "TextSpanRegion",
    "UsageDetails",
    "add_usage_details",
    "detect_media_type_from_base64",
    "is_retry_boundary_update",
    "map_chat_to_agent_update",
    "merge_chat_options",
    "normalize_messages",
    "normalize_stream_usage",
    "prepend_instructions_to_messages",
    "validate_chat_options",
    "validate_tool_mode",
]

TYPE_ALIAS_NAMES = {
    "AgentRunInputs",
    "FinishReasonLiteral",
}

# Chrys-owned since Step 3 — served from ``kernel/tools.py``, NOT framework
# identity. ``FunctionTool`` owns construction/schema/serialization/invoke;
# ``normalize_tools`` mirrors the full framework surface and clones exact
# framework-base residual tools into Chrys instances.
CHRYS_OWNED_NAMES = [
    "FunctionInvocationConfiguration",
    "FunctionTool",
    "SKIP_PARSING",
    "ToolTypes",
    "normalize_tools",
    "tool",
]

EXCEPTION_NAMES = [
    "AdditionItemMismatch",
    "AgentInvalidResponseException",
    "ChatClientContentFilterException",
    "ChatClientException",
    "ChatClientInvalidRequestException",
    "ContentError",
    "ToolException",
]

# Kept out of the façade on purpose (see the types.py docstring): the MCP
# tool classes stay confined to the service/mcp red-line domain.
EXCLUDED_NAMES = [
    "MCPStdioTool",
    "MCPStreamableHTTPTool",
]


@pytest.fixture
def restore_tool_expander() -> Iterator[None]:
    previous = _get_tool_expander()
    try:
        yield
    finally:
        _set_tool_expander(previous)


@pytest.mark.parametrize("name", OWNED_TYPE_NAMES)
def test_facade_exports_chrys_owned_type_surface(name: str) -> None:
    facade_obj = getattr(kernel_types, name)
    if name not in TYPE_ALIAS_NAMES:
        assert getattr(facade_obj, "__module__", "chrys.kernel._types").startswith("chrys.kernel")


@pytest.mark.parametrize("name", EXCEPTION_NAMES)
def test_facade_exports_chrys_owned_exceptions(name: str) -> None:
    facade_obj = getattr(kernel_types, name)
    assert getattr(facade_obj, "__module__", None) in {
        "chrys.kernel.exceptions",
        "chrys.kernel.tools",
    }


@pytest.mark.parametrize("name", CHRYS_OWNED_NAMES)
def test_chrys_owned_names_diverge_from_framework(name: str) -> None:
    """The owned tool names must come from the kernel tools module."""
    facade_obj = getattr(kernel_types, name)
    assert facade_obj.__module__ == "chrys.kernel.tools"


def test_chrys_function_tool_is_owned() -> None:
    assert kernel_types.FunctionTool.__name__ == "FunctionTool"
    assert kernel_types.FunctionTool.__module__ == "chrys.kernel.tools"
    assert kernel_types.FunctionTool.invoke.__module__ == "chrys.kernel.tools"
    assert kernel_types.FunctionTool.parse_result.__module__ == "chrys.kernel.tools"


def test_normalize_stream_usage_keeps_latest_non_null_value_per_key() -> None:
    assert kernel.normalize_stream_usage(
        [
            {"input_token_count": 100, "output_token_count": 3},
            {"input_token_count": None, "output_token_count": 20},
        ]
    ) == {"input_token_count": 100, "output_token_count": 20}


@pytest.mark.parametrize("cls_name", ["ChatResponse", "AgentResponse"])
def test_response_serialization_round_trips_latest_usage(cls_name: str) -> None:
    """The occupancy view must survive to_dict/from_dict independently of the
    billing aggregate — including an explicit None (final call's usage
    unknown), which must NOT be re-inherited from the aggregate on load."""
    cls = getattr(kernel_types, cls_name)

    response = cls(
        messages=[kernel_types.Message(role="assistant", contents=["done"])],
        usage_details=kernel_types.UsageDetails(input_token_count=85),
        latest_usage_details=kernel_types.UsageDetails(input_token_count=45),
    )
    restored = cls.from_dict(response.to_dict())
    assert restored.usage_details == {"input_token_count": 85}
    assert restored.latest_usage_details == {"input_token_count": 45}

    unavailable = cls(
        messages=[kernel_types.Message(role="assistant", contents=["done"])],
        usage_details=kernel_types.UsageDetails(input_token_count=85),
        latest_usage_details=None,
    )
    restored = cls.from_dict(unavailable.to_dict())
    assert restored.usage_details == {"input_token_count": 85}
    assert restored.latest_usage_details is None


def test_tool_factory_builds_chrys_subclass() -> None:
    built = kernel_types.tool(lambda x: x, name="t", description="d")
    assert type(built) is kernel_types.FunctionTool


def test_normalize_tools_preserves_chrys_instances() -> None:
    base = kernel_types.tool(lambda x: x, name="b", description="d")
    out = kernel_types.normalize_tools([base])
    assert out[0] is base
    assert type(out[0]) is kernel_types.FunctionTool


@pytest.mark.asyncio
async def test_validate_tools_leaves_normalized_tools_when_no_expander_registered(restore_tool_expander: None) -> None:
    _set_tool_expander(None)
    tool = kernel_types.FunctionTool(func=lambda text: text, name="echo", description="d")

    out = await kernel_private_types.validate_tools([tool])

    assert out == [tool]


@pytest.mark.asyncio
async def test_validate_tools_does_not_expand_structural_mcp_lookalike() -> None:
    import chrys.service.mcp.owned  # noqa: F401

    assert _get_tool_expander() is not None

    class StructuralMCPTool:
        is_connected = False

        def __init__(self) -> None:
            self.connected = False
            self.functions = [object()]

        async def connect(self) -> None:
            self.connected = True

    tool = StructuralMCPTool()

    out = await kernel_private_types.validate_tools([tool])

    assert out == [tool]
    assert tool.connected is False


@pytest.mark.asyncio
async def test_validate_tools_expands_owned_mcp_functions() -> None:
    from chrys.foundation.text.tokenizer import MixedLanguageTokenizer
    from chrys.service.mcp.owned import MCPStdioTool

    mcp_tool = MCPStdioTool(name="m", command="python", load_tools=False)
    func = kernel_types.FunctionTool(func=lambda text: text, name="mcp_echo", description="d")
    mcp_tool._functions = [func]
    mcp_tool.is_connected = True

    out = await kernel_private_types.validate_tools([mcp_tool])

    assert len(out) == 1
    assert out[0] is not func
    assert isinstance(out[0], kernel_types.FunctionTool)
    assert out[0].name == func.name
    result = await out[0].func("x" * 40_000)
    assert "truncated" in result
    assert MixedLanguageTokenizer().count_tokens(result) <= 8_000


def test_facade_all_is_sorted_and_complete() -> None:
    assert set(kernel_types.__all__) == set(OWNED_TYPE_NAMES) | set(CHRYS_OWNED_NAMES) | set(EXCEPTION_NAMES)
    public_bindings = {name for name in vars(kernel_types) if not name.startswith("_")}
    assert public_bindings == set(kernel_types.__all__)


@pytest.mark.parametrize("name", OWNED_TYPE_NAMES + CHRYS_OWNED_NAMES + EXCEPTION_NAMES)
def test_package_reexports_facade(name: str) -> None:
    assert getattr(kernel, name) is getattr(kernel_types, name)
    assert name in kernel.__all__


@pytest.mark.parametrize("name", EXCLUDED_NAMES)
def test_deliberate_exclusions_stay_excluded(name: str) -> None:
    assert name not in kernel_types.__all__
    assert name not in kernel.__all__


def test_serialization_type_ids_stay_framework_compatible() -> None:
    chrys_message = kernel_types.Message("user", ["hi"])
    assert chrys_message.to_dict()["type"] == "message"

    chrys_response = kernel_types.ChatResponse(messages=[chrys_message])
    assert chrys_response.to_dict()["type"] == "chat_response"

    chrys_update = kernel_types.ChatResponseUpdate(contents=[kernel_types.Content.from_text("hi")])
    assert chrys_update.to_dict()["type"] == "chat_response_update"


@pytest.mark.parametrize("response_type", [kernel_types.ChatResponse, kernel_types.AgentResponse])
def test_structured_value_uses_final_non_empty_assistant_message(response_type: type) -> None:
    messages = [
        kernel_types.Message("assistant", ['{"answer": "old"}']),
        kernel_types.Message("assistant", ['{"answer": "final"}']),
        kernel_types.Message("tool", ['{"tool_noise": true}']),
        kernel_types.Message("assistant", ["   "]),
    ]
    response = response_type(messages=messages, response_format={"type": "object"})

    assert response.value == {"answer": "final"}
    assert "old" in response.text
    assert "tool_noise" in response.text


def test_chat_response_structured_value_joins_split_text_without_separator() -> None:
    # The split lands mid-key on purpose: the pre-fix space join would yield
    # '{"ans wer": "final"}' — syntactically valid JSON with the WRONG key —
    # so this test discriminates against the buggy `Message.text` path.
    response = kernel_types.ChatResponse(
        messages=[
            kernel_types.Message(
                "assistant",
                [kernel_types.Content.from_text('{"ans'), kernel_types.Content.from_text('wer": "final"}')],
            )
        ],
        response_format={"type": "object"},
    )

    assert response.value == {"answer": "final"}


def test_agent_response_structured_value_joins_split_text_for_pydantic_model() -> None:
    class Output(BaseModel):
        answer: int

    # Mid-key split: the pre-fix space join yields '{"ans wer": 42}', which
    # fails Output validation — pinning the no-separator join.
    response = kernel_types.AgentResponse(
        messages=[
            kernel_types.Message(
                "assistant",
                [kernel_types.Content.from_text('{"ans'), kernel_types.Content.from_text('wer": 42}')],
            )
        ],
        response_format=Output,
    )

    assert response.value == Output(answer=42)


@pytest.mark.parametrize("response_type", [kernel_types.ChatResponse, kernel_types.AgentResponse])
def test_structured_value_tolerates_none_text_content(response_type: type) -> None:
    response = response_type(
        messages=[
            kernel_types.Message(
                "assistant",
                [kernel_types.Content("text", text=None), kernel_types.Content.from_text('{"answer": 42}')],
            )
        ],
        response_format={"type": "object"},
    )

    assert response.value == {"answer": 42}


def test_structured_value_supports_pydantic_on_final_assistant_message() -> None:
    class Output(BaseModel):
        answer: int

    response = kernel_types.AgentResponse(
        messages=[
            kernel_types.Message("assistant", ['{"answer": 1}']),
            kernel_types.Message("assistant", ['{"answer": 42}']),
            kernel_types.Message("tool", ["ignored"]),
        ],
        response_format=Output,
    )

    assert response.value == Output(answer=42)


@pytest.mark.parametrize("response_type", [kernel_types.ChatResponse, kernel_types.AgentResponse])
def test_structured_value_without_non_empty_assistant_message_is_none(response_type: type) -> None:
    response = response_type(
        messages=[kernel_types.Message("tool", ['{"answer": 42}']), kernel_types.Message("assistant", [""])],
        response_format={"type": "object"},
    )

    assert response.value is None


def test_streamed_structured_value_uses_final_assistant_message_id() -> None:
    updates = [
        kernel_types.AgentResponseUpdate(
            contents=[kernel_types.Content.from_text('{"answer": "old"}')],
            role="assistant",
            message_id="assistant-1",
        ),
        kernel_types.AgentResponseUpdate(
            contents=[kernel_types.Content.from_text('{"answer": "final"}')],
            role="assistant",
            message_id="assistant-2",
        ),
        kernel_types.AgentResponseUpdate(
            contents=[kernel_types.Content.from_text('{"tool_noise": true}')],
            role="tool",
            message_id="tool-1",
        ),
        kernel_types.AgentResponseUpdate(
            contents=[kernel_types.Content.from_text("  ")],
            role="assistant",
            message_id="assistant-blank",
        ),
    ]

    response = kernel_types.AgentResponse.from_updates(updates, output_format_type={"type": "object"})

    assert response.value == {"answer": "final"}


@pytest.mark.parametrize("response_type", [kernel_types.ChatResponse, kernel_types.AgentResponse])
def test_explicit_transport_heartbeat_does_not_create_empty_message(response_type: type) -> None:
    chat_heartbeat = kernel_types.ChatResponseUpdate.transport_heartbeat()
    if response_type is kernel_types.ChatResponse:
        heartbeat = chat_heartbeat
    else:
        heartbeat = kernel_types.AgentResponseUpdate(
            contents=[],
            author_name="Chrys",
            raw_representation=chat_heartbeat,
        )

    response = response_type.from_updates([heartbeat])

    assert response.messages == []


@pytest.mark.parametrize(
    ("response_type", "update_type"),
    [
        (kernel_types.ChatResponse, kernel_types.ChatResponseUpdate),
        (kernel_types.AgentResponse, kernel_types.AgentResponseUpdate),
    ],
)
def test_duplicate_content_at_new_message_boundary_does_not_leave_empty_message(
    response_type: type,
    update_type: type,
) -> None:
    call = kernel_types.Content.from_function_call("c1", "echo", arguments={"text": "once"})
    updates = [
        update_type(contents=[call], role="assistant", message_id="assistant-1"),
        update_type(contents=[call], role="assistant", message_id="assistant-2"),
    ]

    response = response_type.from_updates(updates)

    assert len(response.messages) == 1
    assert response.messages[0].message_id == "assistant-1"
    assert response.messages[0].contents == [call]


@pytest.mark.asyncio
async def test_chat_from_update_generator_survives_content_id_reuse() -> None:
    """A merged-away fragment's reused id must never skip a fresh fragment.

    ``from_update_generator`` retains nothing beyond the response being
    built: once function-call fragments merge, the earlier objects are
    released mid-pass and CPython can hand a later fresh fragment the same
    id. The assembly-pass duplicate check must key on RETAINED objects —
    a bare-integer id set would silently drop the fresh fragment and
    truncate the call's arguments.
    """

    async def fragments():
        for part in ('{"text": "', "once", '"}'):
            yield kernel_types.ChatResponseUpdate(
                contents=[kernel_types.Content.from_function_call("c1", "echo", arguments=part)],
                role="assistant",
            )

    response = await kernel_types.ChatResponse.from_update_generator(fragments())

    calls = [c for m in response.messages for c in m.contents if c.type == "function_call"]
    assert [c.arguments for c in calls] == ['{"text": "once"}']


@pytest.mark.asyncio
async def test_agent_from_update_generator_survives_content_id_reuse() -> None:
    """Agent-side async assembly has the same retention obligation."""

    async def fragments():
        for part in ('{"text": "', "twice", '"}'):
            yield kernel_types.AgentResponseUpdate(
                contents=[kernel_types.Content.from_function_call("c1", "echo", arguments=part)],
                role="assistant",
            )

    response = await kernel_types.AgentResponse.from_update_generator(fragments())

    calls = [c for m in response.messages for c in m.contents if c.type == "function_call"]
    assert [c.arguments for c in calls] == ['{"text": "twice"}']


def test_raw_only_provider_update_preserves_raw_payload() -> None:
    raw_payload = object()
    update = kernel_types.ChatResponseUpdate(contents=[], raw_representation=raw_payload)

    response = kernel_types.ChatResponse.from_updates([update])

    assert len(response.messages) == 1
    assert response.messages[0].contents == []
    assert response.raw_representation == [raw_payload]


def test_content_roundtrip_preserves_wire_shape() -> None:
    chrys_content = kernel_types.Content.from_function_call("call-1", "echo", arguments={"text": "hi"})
    assert chrys_content.to_dict(exclude_none=False) == {
        "type": "function_call",
        "text": None,
        "protected_data": None,
        "uri": None,
        "media_type": None,
        "message": None,
        "error_code": None,
        "error_details": None,
        "usage_details": None,
        "call_id": "call-1",
        "name": "echo",
        "arguments": {"text": "hi"},
        "exception": None,
        "result": None,
        "items": None,
        "file_id": None,
        "vector_store_id": None,
        "inputs": None,
        "outputs": None,
        "image_id": None,
        "commands": None,
        "timeout_ms": None,
        "max_output_length": None,
        "status": None,
        "stdout": None,
        "stderr": None,
        "exit_code": None,
        "timed_out": None,
        "tool_name": None,
        "server_name": None,
        "output": None,
        "id": None,
        "additional_properties": {},
    }
    assert kernel_types.Content.from_dict(chrys_content.to_dict()) == chrys_content


def test_content_annotation_raw_representation_is_runtime_only() -> None:
    raw_annotation = object()
    content = kernel_types.Content.from_text(
        "cited answer",
        annotations=[
            kernel_types.Annotation(
                type="citation",
                title="Docs",
                url="https://example.test/docs",
                raw_representation=raw_annotation,
            )
        ],
    )

    serialized = content.to_dict()

    assert content.annotations is not None
    assert content.annotations[0]["raw_representation"] is raw_annotation
    assert serialized["annotations"] == [
        {
            "type": "citation",
            "title": "Docs",
            "url": "https://example.test/docs",
        }
    ]
    assert json.loads(json.dumps(serialized)) == serialized
    assert kernel_types.Content.from_dict(serialized).to_dict() == serialized


def test_hosted_tool_content_roundtrip_preserves_canonical_fields() -> None:
    content = kernel_types.Content.from_hosted_tool_result(
        "hosted-1",
        tool_name="tool_search",
        result={"tools": ["search_docs"]},
        items=[kernel_types.Content.from_text("search_docs")],
        status="completed",
        hosted_family="tool_discovery",
        hosted_provider="openai",
        provider_item_type="tool_search_call",
        provider_item_id="item-1",
        provider_phase="terminal",
        retry_safety="read_only",
    )

    serialized = content.to_dict()
    restored = kernel_types.Content.from_dict(serialized)

    assert restored == content
    assert restored.provider_hosted is True
    assert restored.provider_status == "completed"
    assert restored.items is not None and restored.items[0].text == "search_docs"


def test_specialized_hosted_factories_set_execution_location_and_family() -> None:
    contents = [
        kernel_types.Content.from_search_tool_call("search-1", tool_name="web_search"),
        kernel_types.Content.from_search_tool_result("search-1", tool_name="web_search"),
        kernel_types.Content.from_mcp_server_tool_call("mcp-1", "lookup"),
        kernel_types.Content.from_mcp_server_tool_result("mcp-1"),
        kernel_types.Content.from_code_interpreter_tool_call(call_id="code-1"),
        kernel_types.Content.from_code_interpreter_tool_result(call_id="code-1"),
        kernel_types.Content.from_image_generation_tool_call(image_id="image-1"),
        kernel_types.Content.from_image_generation_tool_result(image_id="image-1"),
        kernel_types.Content.from_shell_tool_call(call_id="shell-1"),
        kernel_types.Content.from_shell_tool_result(call_id="shell-1"),
    ]

    assert [content.hosted_family for content in contents] == [
        "search",
        "search",
        "mcp",
        "mcp",
        "code",
        "code",
        "image",
        "image",
        "shell",
        "shell",
    ]
    assert all(content.provider_hosted is True for content in contents)


def test_old_content_payload_without_hosted_fields_roundtrips_unchanged() -> None:
    payload = {
        "type": "search_tool_call",
        "call_id": "search-1",
        "tool_name": "web_search",
        "arguments": {"query": "Chrys"},
        "additional_properties": {},
    }

    restored = kernel_types.Content.from_dict(payload)

    assert restored.provider_hosted is False
    assert restored.hosted_family is None
    assert restored.hosted_provider is None
    assert restored.provider_item_type is None
    assert restored.provider_item_id is None
    assert restored.provider_phase is None
    assert restored.provider_status is None
    assert restored.retry_safety is None
    assert restored.to_dict() == payload
    assert (
        not {
            "provider_hosted",
            "hosted_family",
            "hosted_provider",
            "provider_item_type",
            "provider_item_id",
            "provider_phase",
            "provider_status",
            "retry_safety",
        }
        & restored.to_dict(exclude_none=False).keys()
    )


def test_informational_function_call_roundtrip_is_sparse_and_merge_is_sticky() -> None:
    first = kernel_types.Content.from_function_call(
        "hosted-1",
        "provider_search",
        arguments='{"query":"chrys"',
        informational_only=True,
    )
    second = kernel_types.Content.from_function_call(
        "hosted-1",
        "provider_search",
        arguments="}",
    )

    merged = first + second
    serialized = merged.to_dict()

    assert merged.informational_only is True
    assert serialized["informational_only"] is True
    assert kernel_types.Content.from_dict(serialized) == merged
    ordinary = kernel_types.Content.from_function_call("local-1", "echo")
    assert "informational_only" not in ordinary.to_dict(exclude_none=False)


def test_function_call_merge_uses_name_from_later_delta() -> None:
    first = kernel_types.Content("function_call", call_id="call-1", name=None, arguments='{"text":')
    second = kernel_types.Content("function_call", call_id="call-1", name="echo", arguments='"hi"}')

    merged = first + second

    assert merged.name == "echo"
    assert merged.arguments == '{"text":"hi"}'


def _streamed_calls(*parts: tuple[str, dict[str, Any]]) -> list[tuple[str | None, Any]]:
    """The calls one streamed message assembles from function-call contents sharing call id ``same``."""
    updates = [
        kernel_types.ChatResponseUpdate(
            contents=[
                kernel_types.Content.from_function_call(
                    "same", "lookup", arguments=arguments, additional_properties=properties
                )
            ]
        )
        for arguments, properties in parts
    ]
    response = kernel_types.ChatResponse.from_updates(updates)
    return [(content.call_id, content.arguments) for content in response.messages[0].contents]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param({"output_index": 0, "fc_id": "fc_1"}, {"output_index": 1, "fc_id": "fc_2"}, id="two_items"),
        pytest.param({"output_index": 0}, {"output_index": 1}, id="two_positions"),
        pytest.param({"fc_id": "fc_1"}, {"fc_id": "fc_2"}, id="two_item_ids"),
    ],
)
def test_calls_from_two_responses_output_items_stay_two_under_one_call_id(
    first: dict[str, Any], second: dict[str, Any]
) -> None:
    assert _streamed_calls(('{"city": "Paris"}', first), ('{"city": "Rome"}', second)) == [
        ("same", '{"city": "Paris"}'),
        ("same", '{"city": "Rome"}'),
    ]


@pytest.mark.parametrize(
    "second",
    [
        pytest.param({"output_index": 0, "fc_id": "fc_1"}, id="the_same_item"),
        pytest.param({}, id="a_fragment_naming_no_item"),
    ],
)
def test_fragments_of_one_call_still_merge(second: dict[str, Any]) -> None:
    first = {"output_index": 0, "fc_id": "fc_1"}

    assert _streamed_calls(('{"city": ', first), ('"Paris"}', second)) == [("same", '{"city": "Paris"}')]


def test_reasoning_text_and_summary_contents_do_not_merge() -> None:
    summary = kernel_types.Content.from_text_reasoning(id="rs_1", text="Visible summary")
    reasoning_text = kernel_types.Content.from_text_reasoning(
        id="rs_1",
        text="Private reasoning",
        additional_properties={"reasoning_text": True},
    )

    with pytest.raises(kernel_types.AdditionItemMismatch, match="reasoning text with a reasoning summary"):
        summary + reasoning_text


def test_from_updates_keeps_reasoning_text_and_summary_as_siblings() -> None:
    updates = [
        kernel_types.ChatResponseUpdate(
            contents=[kernel_types.Content.from_text_reasoning(id="rs_1", text="Visible summary")]
        ),
        kernel_types.ChatResponseUpdate(
            contents=[
                kernel_types.Content.from_text_reasoning(
                    id="rs_1",
                    text="Private reasoning",
                    additional_properties={"reasoning_text": True},
                )
            ]
        ),
    ]

    response = kernel_types.ChatResponse.from_updates(updates)

    assert [(content.text, content.additional_properties) for content in response.messages[0].contents] == [
        ("Visible summary", {}),
        ("Private reasoning", {"reasoning_text": True}),
    ]


def test_from_updates_keeps_openai_output_messages_with_distinct_envelopes_as_siblings() -> None:
    first_envelope = {"id": "msg_1", "status": "completed", "phase": "commentary"}
    second_envelope = {"id": "msg_2", "status": "completed", "phase": "final_answer"}
    updates = [
        kernel_types.ChatResponseUpdate(
            contents=[
                kernel_types.Content.from_text(
                    text="working",
                    additional_properties={kernel_types.OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY: first_envelope},
                )
            ]
        ),
        kernel_types.ChatResponseUpdate(
            contents=[
                kernel_types.Content.from_text(
                    text="done",
                    additional_properties={kernel_types.OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY: second_envelope},
                )
            ]
        ),
    ]

    response = kernel_types.ChatResponse.from_updates(updates)

    assert [content.text for content in response.messages[0].contents] == ["working", "done"]
    assert [
        content.additional_properties[kernel_types.OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY]
        for content in response.messages[0].contents
    ] == [first_envelope, second_envelope]


def test_reasoning_contents_with_different_wire_formats_do_not_merge() -> None:
    """GLM/DeepSeek dialect contents must never fold into other providers' reasoning."""
    content_dialect = kernel_types.Content.from_text_reasoning(
        text="dialect text",
        additional_properties={"openai_reasoning_format": "reasoning_content"},
    )
    details_dialect = kernel_types.Content.from_text_reasoning(
        text="details text",
        additional_properties={"openai_reasoning_format": "reasoning_details"},
    )
    unmarked = kernel_types.Content.from_text_reasoning(text="foreign text")

    with pytest.raises(kernel_types.AdditionItemMismatch, match="different wire formats"):
        content_dialect + details_dialect
    with pytest.raises(kernel_types.AdditionItemMismatch, match="different wire formats"):
        content_dialect + unmarked


@pytest.mark.parametrize("redacted_first", [True, False])
def test_redacted_and_ordinary_anthropic_reasoning_do_not_coalesce(redacted_first: bool) -> None:
    redacted = kernel_types.Content.from_text_reasoning(
        protected_data="opaque-redacted",
        additional_properties={"anthropic_redacted_thinking": True},
    )
    ordinary = kernel_types.Content.from_text_reasoning(text="ordinary thinking")
    contents = [redacted, ordinary] if redacted_first else [ordinary, redacted]

    kernel_private_types._coalesce_text_content(contents, "text_reasoning")

    assert len(contents) == 2
    assert [content.text for content in contents] == (
        [None, "ordinary thinking"] if redacted_first else ["ordinary thinking", None]
    )


def test_reasoning_contents_from_different_endpoints_do_not_merge() -> None:
    """Each side replays only to its own endpoint; an unstamped side came from none in particular."""
    here = ReasoningOrigin("anthropic_messages", "https://api.anthropic.com:443")
    there = ReasoningOrigin("anthropic_messages", "https://gateway.example:443")

    def thinking(origin: ReasoningOrigin | None, text: str) -> kernel_types.Content:
        content = kernel_types.Content.from_text_reasoning(text=text)
        if origin is not None:
            origin.stamp(content.additional_properties)
        return content

    with pytest.raises(kernel_types.AdditionItemMismatch, match="different endpoints"):
        thinking(here, "a") + thinking(there, "b")
    with pytest.raises(kernel_types.AdditionItemMismatch, match="different endpoints"):
        thinking(here, "a") + thinking(None, "b")
    with pytest.raises(kernel_types.AdditionItemMismatch, match="different endpoints"):
        thinking(None, "a") + thinking(here, "b")

    merged = thinking(here, "a") + thinking(here, "b")
    assert merged.text == "ab"
    assert merged.additional_properties[REASONING_ORIGIN_KEY] == here.stamp_value()


def test_reasoning_contents_with_distinct_protected_payloads_do_not_merge() -> None:
    """Id-less blocks that both carry opaque payloads would lose the earlier one."""
    first = kernel_types.Content.from_text_reasoning(protected_data='["D1"]')
    second = kernel_types.Content.from_text_reasoning(protected_data='["D2"]')

    with pytest.raises(kernel_types.AdditionItemMismatch, match="distinct protected payloads"):
        first + second

    # Identical payloads stay mergeable (idempotent re-delivery).
    merged = first + kernel_types.Content.from_text_reasoning(protected_data='["D1"]')
    assert merged.protected_data == '["D1"]'


def test_same_id_reasoning_protected_payload_keeps_later_wins() -> None:
    """Responses streams refine one reasoning id: snapshot payload, then terminal."""
    snapshot = kernel_types.Content.from_text_reasoning(id="rs_1", protected_data="snapshot-A")
    final = kernel_types.Content.from_text_reasoning(id="rs_1", protected_data="final-B")

    merged = snapshot + final

    assert merged.protected_data == "final-B"
    assert merged.id == "rs_1"


def test_from_updates_keeps_distinct_protected_reasoning_blocks_as_siblings() -> None:
    updates = [
        kernel_types.ChatResponseUpdate(contents=[kernel_types.Content.from_text_reasoning(protected_data='["D1"]')]),
        kernel_types.ChatResponseUpdate(contents=[kernel_types.Content.from_text_reasoning(protected_data='["D2"]')]),
    ]

    response = kernel_types.ChatResponse.from_updates(updates)

    assert [content.protected_data for content in response.messages[0].contents] == ['["D1"]', '["D2"]']


def test_message_roundtrip_preserves_reasoning_format_marker_and_raw_fields() -> None:
    message = kernel_types.Message(
        role="assistant",
        contents=[
            kernel_types.Content.from_text_reasoning(
                text="GLM thinking",
                additional_properties={"openai_reasoning_format": "reasoning_content"},
            ),
            kernel_types.Content.from_text("answer"),
        ],
        additional_properties={
            "reasoning_content": "GLM thinking",
            "openai_reasoning_format": "reasoning_content",
        },
    )

    restored = kernel_types.Message.from_dict(message.to_dict())

    assert restored.contents[0].additional_properties["openai_reasoning_format"] == "reasoning_content"
    assert restored.contents[0].text == "GLM thinking"
    assert restored.additional_properties["reasoning_content"] == "GLM thinking"
    assert restored.additional_properties["openai_reasoning_format"] == "reasoning_content"


def test_message_roundtrip_preserves_reasoning_origin_stamps() -> None:
    origin = ReasoningOrigin("chat_completions", "https://openrouter.ai:443")
    content = kernel_types.Content.from_text_reasoning(protected_data='[{"type": "reasoning.encrypted"}]')
    origin.stamp(content.additional_properties)
    message = kernel_types.Message(role="assistant", contents=[content], additional_properties={})
    origin.stamp(message.additional_properties)

    restored = kernel_types.Message.from_dict(json.loads(json.dumps(message.to_dict())))

    assert restored.contents[0].additional_properties[REASONING_ORIGIN_KEY] == origin.stamp_value()
    assert restored.additional_properties[REASONING_ORIGIN_KEY] == origin.stamp_value()


@pytest.mark.parametrize(
    "data_uri",
    [
        "not-a-data-uri",
        "data:text/plain,hello",
        "data:image/png;base64",
    ],
)
def test_detect_media_type_from_base64_rejects_malformed_data_uri(data_uri: str) -> None:
    with pytest.raises(ValueError):
        kernel_types.detect_media_type_from_base64(data_uri=data_uri)


def test_mcp_server_tool_calls_are_inherently_informational_without_extra_wire_field() -> None:
    content = kernel_types.Content.from_mcp_server_tool_call("mcp-1", "search")

    assert content.informational_only is True
    assert "informational_only" not in content.to_dict(exclude_none=False)
    assert kernel_types.Content.from_dict(content.to_dict()).informational_only is True


def test_message_persistence_roundtrip_preserves_informational_call() -> None:
    message = kernel_types.Message(
        "assistant",
        [kernel_types.Content.from_function_call("hosted-1", "search", informational_only=True)],
    )

    restored = kernel_types.Message.from_dict(message.to_dict())

    assert restored.contents[0].informational_only is True
    assert restored.to_dict() == message.to_dict()


def test_from_dict_rejects_mismatched_type_identifier() -> None:
    """The identifier is resolved from the class (matching ``to_dict``), so a
    payload typed for a different class must raise instead of silently
    deserializing into whatever class was asked."""
    payload = {"type": "function_tool", "role": "user", "contents": []}

    with pytest.raises(ValueError, match="Type mismatch"):
        kernel_types.Message.from_dict(payload)
    with pytest.raises(ValueError, match="Type mismatch"):
        kernel_types.Message.from_json(json.dumps(payload))


def test_from_dict_round_trip_still_accepts_own_identifier() -> None:
    message = kernel_types.Message("user", [kernel_types.Content.from_text("hi")])

    restored = kernel_types.Message.from_dict(message.to_dict())

    assert restored.to_dict() == message.to_dict()


def test_content_from_dict_rejects_unknown_fields() -> None:
    """``Content.__init__`` has no ``**kwargs`` catch-all, so unknown keys
    raise ``TypeError`` — ``FunctionTool.parse_result`` relies on this to fall
    back to preserving content-like objects as JSON text. Payloads carrying
    since-removed fields (e.g. the 2026-07-04 approval surface) fail the same
    way; accepted, because chrys never persisted such contents."""
    with pytest.raises(TypeError):
        kernel_types.Content.from_dict({"type": "text", "text": "hi", "payload": {"kept": True}})


def test_parse_result_returns_chrys_content_only() -> None:
    parsed = kernel_types.FunctionTool.parse_result([kernel_types.Content.from_text("x"), {"structured": True}])
    assert all(type(item) is kernel_types.Content for item in parsed)


def test_message_contents_is_content_list_backed_by_public_dict_key() -> None:
    """Storage contract: the coercing property MUST back onto
    ``__dict__["contents"]`` — ``to_dict`` enumerates ``__dict__`` and drops
    ``_``-prefixed keys, so a private backing would silently erase contents
    from every persisted message."""
    message = kernel_types.Message("user", ["hi"])

    assert type(message.contents) is ContentList
    assert message.__dict__["contents"] is message.contents
    assert "_contents" not in message.__dict__


def test_message_contents_setter_coerces_plain_list_breaking_alias() -> None:
    """Declared narrow API change: the setter ALLOCATES a ``ContentList``
    from a plain list, so a caller's captured plain-list alias no longer
    aliases ``msg.contents`` afterward."""
    content = kernel_types.Content.from_text("x")
    alias = [content]
    message = kernel_types.Message("user")

    message.contents = alias

    assert type(message.contents) is ContentList
    assert message.contents is not alias
    assert message.contents == alias
    alias.append(kernel_types.Content.from_text("y"))
    assert len(message.contents) == 1


def test_message_contents_setter_preserves_content_list_identity() -> None:
    """Idempotence for ``ContentList`` input — required for deepcopy
    re-coercion — and shared-list twins: assigning one message's contents to
    another shares the SAME list object, exactly today's aliasing."""
    contents = ContentList([kernel_types.Content.from_text("x")])
    message = kernel_types.Message("user")
    message.contents = contents
    assert message.contents is contents

    twin = kernel_types.Message("assistant")
    twin.contents = message.contents
    assert twin.contents is message.contents


def test_message_contents_in_place_mutation_keeps_list_identity() -> None:
    """Captors snapshot the list object; in-place mutation (append/clear)
    must not replace it — only assignment does."""
    message = kernel_types.Message("user", ["a"])
    captured = message.contents

    message.contents.append(kernel_types.Content.from_text("b"))

    assert message.contents is captured
    assert len(captured) == 2


def test_message_deepcopy_recoerces_contents_through_the_setter() -> None:
    """``SerializationMixin.__deepcopy__`` sets attributes via
    ``object.__setattr__``, which follows the descriptor protocol and invokes
    the coercing setter with the already-copied ``ContentList``."""
    message = kernel_types.Message("user", ["a"])

    duplicate = copy.deepcopy(message)

    assert type(duplicate.contents) is ContentList
    assert duplicate.contents is not message.contents
    assert duplicate.__dict__["contents"] is duplicate.contents
    assert duplicate.contents[0] is not message.contents[0]
    assert duplicate.contents[0].text == "a"


def test_message_roundtrip_keeps_contents_under_public_key() -> None:
    message = kernel_types.Message("user", ["hello"])

    data = message.to_dict()
    assert "contents" in data

    restored = kernel_types.Message.from_dict(data)
    assert type(restored.contents) is ContentList
    assert restored.to_dict() == data


def test_message_takes_a_bare_string_as_one_text_content() -> None:
    message = kernel_types.Message("user", "Hello")

    assert [(content.type, content.text) for content in message.contents] == [("text", "Hello")]


def test_merge_chat_options_adds_a_single_mapping_tool_whole() -> None:
    spec = {"type": "function", "function": {"name": "lookup"}}

    merged = kernel_types.merge_chat_options({"tools": ["base"]}, {"tools": spec})

    assert merged["tools"] == ["base", spec]


def test_merge_chat_options_takes_tool_tuples_and_single_base_tools() -> None:
    single = object()

    assert kernel_types.merge_chat_options({"model": "m"}, {"tools": ("a", "b")})["tools"] == ["a", "b"]
    assert kernel_types.merge_chat_options({"tools": single}, {"tools": ["a"]})["tools"] == [single, "a"]


def test_merge_chat_options_does_not_consume_a_tool_iterator() -> None:
    for base in ({"model": "m"}, {"tools": ["base"]}):
        tools = iter(["a"])

        kernel_types.merge_chat_options(base, {"tools": tools})

        assert list(tools) == ["a"]
