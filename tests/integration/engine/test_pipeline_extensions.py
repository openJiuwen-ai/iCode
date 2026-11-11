# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A Python extension end to end: the built main agent calls its tools and reads its contexts."""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from chrys.foundation.events.types import (
    ApprovalRequest,
    ApprovalResponse,
    RuntimeExtensionDetails,
    UserMessage,
    Warning,
)
from chrys.service.llm.mock import MockResponse
from tests.support.event_capture import capture_events
from tests.support.extensions import EXAMPLE_EXTENSION_ID, install_file_extension
from tests.support.pipeline_helpers import error_on_nth, extract_tool_results, wait_for_event, wait_for_idle
from tests.support.waiting import ENGINE_TURN_TIMEOUT

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_NOTE = {"title": "Faster builds", "tags": ["perf"]}


async def test_the_main_agent_runs_extension_tools_and_asks_first_where_a_tool_says_so(
    make_pipeline_ctx, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_file_extension(monkeypatch)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    responses = [
        MockResponse(tool_calls=[("acme_project_name", "c1", {})]),
        MockResponse(tool_calls=[("acme_add_release_note", "c2", {"note": _NOTE})]),
        MockResponse(text="Noted."),
    ]
    ctx = await make_pipeline_ctx(responses, approval_default="auto", extensions=[EXAMPLE_EXTENSION_ID, "acme_gone"])
    details = ctx.engine.runtime_details
    assert details.extensions == {
        EXAMPLE_EXTENSION_ID: RuntimeExtensionDetails(tools=["acme_project_name", "acme_add_release_note"])
    }
    assert details.extension_failures == {"acme_gone": "it is not installed"}

    await ctx.bus.publish(UserMessage(text="Add a release note"))
    (request,) = await wait_for_event(ctx.events, ApprovalRequest, timeout=ENGINE_TURN_TIMEOUT)
    # The profile says auto; only the tool that declares it asks first.
    assert (request.tool_name, request.tool_kind) == ("acme_add_release_note", f"extension.{EXAMPLE_EXTENSION_ID}")
    assert [result["result"] for result in extract_tool_results(ctx.events)] == ["demo"]

    await ctx.approve(request.request_id)
    await wait_for_idle(ctx)
    results = extract_tool_results(ctx.events)
    assert [result["tool_name"] for result in results] == ["acme_project_name", "acme_add_release_note"]
    assert json.loads(results[1]["result"]) == {"notes": 1, "last": _NOTE, "agent": "test-pipeline"}


_CONTEXTS = """\
from chrys.extensions import Extension

extension = Extension()
ASKED = []


@extension.context
async def ticket(ctx):
    ASKED.append(ctx.prompt)
    return f"Ticket for {ctx.prompt!r}, asked {len(ASKED)} times"


@extension.context
async def broken(ctx):
    raise RuntimeError("tracker down")
"""


async def test_the_main_agent_asks_its_extension_contexts_each_turn_and_a_retry_keeps_their_text(
    make_pipeline_ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_file_extension(monkeypatch, "acme_ctx", _CONTEXTS)
    ctx = await make_pipeline_ctx([MockResponse(text="one"), MockResponse(text="two")], extensions=["acme_ctx"])
    warnings = await capture_events(ctx.bus, Warning)
    await ctx.send_message("first")
    error_on_nth(ctx, 1)
    await ctx.send_message("second")
    await ctx.send_retry()

    assert sys.modules["_chrys_extension_acme_ctx"].ASKED == ["first", "second"]
    first_call, retry_call = ctx.mock_client.call_history[0][0], ctx.mock_client.call_history[-1][0]
    first_text = [m.text for m in first_call if m.role == "user"][-1]
    assert "Ticket for 'first', asked 1 times" in first_text
    sent = "\n".join(m.text or "" for m in retry_call if m.role == "user")
    assert sent.count("Ticket for 'second', asked 2 times") == 1
    assert [(w.code, w.message) for w in warnings if w.code == "extension_context_failed"] == [
        (
            "extension_context_failed",
            "Agent test-pipeline goes without context broken from extension acme_ctx: RuntimeError: tracker down",
        )
    ]


_TOOL_HOOKS = """\
from typing import Annotated

from chrys.extensions import Extension, ToolContext, ToolError

extension = Extension()


@extension.tool
async def acme_echo(ctx: ToolContext, text: Annotated[str, "Text to echo"]) -> str:
    \"\"\"Echo the text.\"\"\"
    return text


@extension.before_tool
async def redact(call):
    if call.arguments.get("text") == "forbidden":
        raise ToolError("Not that one.")
    if "text" in call.arguments:
        return {"text": call.arguments["text"].replace("secret", "***")}
    return None


@extension.after_tool
async def sign(call, result):
    return None if result.rejected else f"{result.text} (checked)"
"""


async def test_the_main_agent_runs_extension_tool_hooks_before_approval_again_on_an_edit_and_after_the_call(
    make_pipeline_ctx, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_file_extension(monkeypatch, "acme_hooks", _TOOL_HOOKS)
    responses = [
        MockResponse(tool_calls=[("acme_echo", "c1", {"text": "forbidden"})]),
        MockResponse(tool_calls=[("acme_echo", "c2", {"text": "hello secret"})]),
        MockResponse(text="Done."),
    ]
    ctx = await make_pipeline_ctx(responses, extensions=["acme_hooks"])
    await ctx.bus.publish(UserMessage(text="Echo"))
    (request,) = await wait_for_event(ctx.events, ApprovalRequest, timeout=ENGINE_TURN_TIMEOUT)
    # The refused call never asked; the other asks with the arguments the hook left.
    assert (request.tool_name, request.args) == ("acme_echo", {"text": "hello ***"})

    # The user's edit is checked again before the tool runs it.
    await ctx.bus.publish(
        ApprovalResponse(request_id=request.request_id, approved=True, modified_args={"text": "secret again"})
    )
    await wait_for_idle(ctx)
    results = [result["result"] for result in extract_tool_results(ctx.events)]
    assert results == ["Error: Not that one.", "*** again (checked)"]
    last_request = ctx.mock_client.call_history[-1][0]
    sent = [c.result for m in last_request for c in m.contents if c.type == "function_result"]
    assert sent == ["Error: Not that one.", "*** again (checked)"]
