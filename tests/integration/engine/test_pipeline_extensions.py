# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A Python extension end to end: the built main agent calls its tools."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from chrys.foundation.events.types import ApprovalRequest, UserMessage
from chrys.service.llm.mock import MockResponse
from tests.support.extensions import EXAMPLE_EXTENSION_ID, install_file_extension
from tests.support.pipeline_helpers import extract_tool_results, wait_for_event, wait_for_idle
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
    assert details.extension_tools == {EXAMPLE_EXTENSION_ID: ["acme_project_name", "acme_add_release_note"]}
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
