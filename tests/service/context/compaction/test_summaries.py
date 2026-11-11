# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ``_build_summary`` output and ``_set_summarized`` bookkeeping."""

import base64
import copy

import pytest

from chrys.kernel import (
    Content,
    Message,
)
from chrys.service.context.compaction import (
    _build_summary,
    _set_summarized,
)
from chrys.service.context.compaction.summaries import _SUMMARY_TOTAL_MAX
from tests.service.context.compaction._compaction_helpers import (
    _anthropic_fetched_pdf_exchange,
    _assistant_text,
    _assistant_tool_call,
    _build_tool_group,
    _tool_result,
)


def test_summary_preserves_args():
    """Summary text includes function name and arguments."""
    group = _build_tool_group("c1", "grep", "47 matches found", args={"pattern": "class Foo", "path": "src/"})
    summary = _build_summary(group)
    assert "grep" in summary
    assert "class Foo" in summary
    assert "src/" in summary
    assert "\u2192" in summary  # → arrow


@pytest.mark.parametrize("field", ["result", "narration"])
def test_summary_truncates_long_results(field: str) -> None:
    """Long results and absorbed narration alike are trimmed with a char count."""
    if field == "result":
        msgs = _build_tool_group("c1", "read_file", "x" * 500, args={"path": "foo.py"})
    else:
        msgs = [
            _assistant_tool_call("c1", "read_file", args={"path": "a.py"}),
            _assistant_text("n" * 500),
            _tool_result("c1", "content A"),
        ]
    summary = _build_summary(msgs)
    assert "(500 chars)" in summary
    assert "x" * 500 not in summary
    assert "n" * 500 not in summary


def test_summary_multiple_tools():
    """Summary handles multiple tool calls in one group."""
    msgs = [
        _assistant_tool_call("c1", "read_file", args={"path": "a.py"}),
        _tool_result("c1", "content A"),
        _assistant_tool_call("c2", "read_file", args={"path": "b.py"}),
        _tool_result("c2", "content B"),
    ]
    summary = _build_summary(msgs)
    assert "a.py" in summary
    assert "b.py" in summary


@pytest.mark.parametrize("restored", [False, True], ids=["live", "restored"])
def test_summary_preserves_hosted_mcp_name_and_result_text(restored: bool) -> None:
    messages = [
        Message(
            "assistant",
            [Content.from_mcp_server_tool_call("mcp-1", "search_docs", arguments={"query": "Chrys"})],
        ),
        Message(
            "assistant",
            [Content.from_mcp_server_tool_result("mcp-1", output=[Content.from_text("found the guide")])],
        ),
    ]
    if restored:
        messages = [Message.from_dict(message.to_dict()) for message in messages]

    summary = _build_summary(messages)

    assert "search_docs" in summary
    assert 'query="Chrys"' in summary
    assert "found the guide" in summary


@pytest.mark.parametrize("restored", [False, True], ids=["live", "restored"])
def test_summary_replaces_a_hosted_base64_document_with_a_placeholder(restored: bool) -> None:
    payload = base64.b64encode(b"%PDF-1.7\n" + b"binary" * 40).decode()
    messages = _anthropic_fetched_pdf_exchange(payload)
    if restored:
        messages = [Message.from_dict(message.to_dict()) for message in messages]

    summary = _build_summary(messages)

    assert "[application/pdf artifact]" in summary
    assert payload[:32] not in summary


def test_summary_prefers_hosted_items_over_duplicate_result_mirror() -> None:
    messages = [
        Message(
            "assistant",
            [Content.from_hosted_tool_call("hosted-1", tool_name="remote_task", arguments={"task": "inspect"})],
        ),
        Message(
            "assistant",
            [
                Content.from_hosted_tool_result(
                    "hosted-1",
                    tool_name="remote_task",
                    result="authoritative output",
                    items=[Content.from_text("authoritative output")],
                )
            ],
        ),
    ]

    summary = _build_summary(messages)

    assert summary.count("authoritative output") == 1


@pytest.mark.parametrize("restored", [False, True], ids=["live", "restored"])
def test_summary_preserves_specialized_hosted_calls_and_results(restored: bool) -> None:
    image = Content.from_uri("data:image/png;base64,QUJD", media_type="image/png")
    messages = [
        Message(
            "assistant",
            [
                Content.from_search_tool_call("search-1", tool_name="web_search", arguments={"query": "Chrys"}),
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
                Content.from_search_tool_result("search-1", tool_name="web_search", result="search result"),
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
    if restored:
        messages = [Message.from_dict(message.to_dict()) for message in messages]

    summary = _build_summary(messages)

    for fragment in (
        "web_search",
        'query="Chrys"',
        "search result",
        "code_interpreter",
        "code input",
        "code result",
        "image_generation",
        'image_id="image-1"',
        "image/png image",
        "shell",
        "shell-input",
        "shell result",
        "exit code: 0",
    ):
        assert fragment in summary


@pytest.mark.parametrize(
    ("items", "expected"),
    [
        pytest.param([Content.from_text("47 matches")], "47 matches", id="text"),
        pytest.param([Content.from_data(b"\x89PNG", "image/png")], "[image/png image]", id="image-data"),
        pytest.param(
            [Content.from_data(b"%PDF-1.7", "application/pdf")],
            "[application/pdf artifact]",
            id="document-data",
        ),
        pytest.param(
            [Content.from_uri("https://example.com/shot.png", media_type="image/png")],
            "[image/png image]",
            id="image-uri",
        ),
    ],
)
@pytest.mark.parametrize("restored", [False, True], ids=["live", "restored"])
def test_summary_renders_function_result_items(items: list[Content], expected: str, restored: bool) -> None:
    """A function result's non-text items render as placeholders, never as ``completed``."""
    messages = [
        _assistant_tool_call("c1", "view_image", args={"path": "shot.png"}),
        Message("tool", [Content.from_function_result("c1", result=items)]),
    ]
    if restored:
        messages = [Message.from_dict(message.to_dict()) for message in messages]

    summary = _build_summary(messages)

    assert expected in summary
    assert "→ completed" not in summary


def test_summary_renders_hosted_file_placeholder() -> None:
    """Hosted file results keep their name instead of collapsing to their id."""
    messages = [
        Message("assistant", [Content.from_shell_tool_call(call_id="shell-1", commands=["ls"])]),
        Message(
            "tool",
            [
                Content.from_shell_tool_result(
                    call_id="shell-1",
                    outputs=[
                        Content.from_hosted_file(file_id="file-1", name="report.pdf"),
                        Content.from_shell_command_output(stdout="ok", exit_code=0),
                    ],
                )
            ],
        ),
    ]

    summary = _build_summary(messages)

    assert "[file: report.pdf]" in summary
    assert "ok" in summary


def test_summary_preserves_absorbed_narration():
    """Fused groups may hold assistant narration between the call and its
    results; the summary carries that text in transcript order."""
    msgs = [
        _assistant_tool_call("c1", "read_file", args={"path": "a.py"}),
        _assistant_text("Now checking the parser config"),
        _tool_result("c1", "content A"),
    ]
    summary = _build_summary(msgs)
    assert 'assistant: "Now checking the parser config"' in summary
    assert "read_file" in summary
    assert summary.index("Now checking the parser config") < summary.index("content A")


def test_summary_bounds_total_length() -> None:
    calls = [
        Content.from_function_call(f"call_{index}", f"tool_{index}", arguments={"value": index}) for index in range(300)
    ]
    results = [
        Content.from_function_result(f"call_{index}", result=f"result-{index}-" + "x" * 100) for index in range(300)
    ]

    summary = _build_summary([Message("assistant", calls), Message("tool", results)])

    assert len(summary) <= _SUMMARY_TOTAL_MAX
    assert "tool_0" in summary
    assert "tool_299" not in summary
    assert "... [truncated]" in summary


def test_set_summarized_does_not_mutate_shared_group_dict():
    """_set_summarized creates a new _group dict, never mutating the original."""
    original = _assistant_tool_call("c1", "read_file")
    original.additional_properties["_group"] = {"_group_id": "group_c1", "_group_kind": "tool_call"}

    msg_copy = copy.copy(original)
    msg_copy.additional_properties = dict(original.additional_properties)

    assert original.additional_properties["_group"] is msg_copy.additional_properties["_group"]

    _set_summarized(msg_copy, "tool_summary_group_c1")

    assert "_summarized_by_summary_id" in msg_copy.additional_properties["_group"]
    assert "_summarized_by_summary_id" not in original.additional_properties["_group"]
