# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real file tools and approval must agree with persisted diffs and rollback."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chrys.app.tui.screens.diff import load_diff_entries_by_period
from chrys.foundation.events.types import ApprovalRequest, UserInterrupt, UserMessage
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.models.workspace import Workspace
from chrys.service.llm.mock import MockResponse
from chrys.service.tools.builtins.filesystem import FilesystemTools
from tests.support.pipeline_helpers import wait_for_event, wait_for_idle
from tests.support.waiting import ENGINE_TURN_TIMEOUT


@pytest.mark.parametrize(
    "scenario", ["reject-edit", "interrupt-edit", "reject-create", "approve-edit", "reject-then-approve"]
)
async def test_approval_file_changes_match_disk_and_saved_history(
    tmp_path: Path, make_pipeline_ctx, scenario: str
) -> None:
    target = tmp_path / "work.txt"
    creating = scenario == "reject-create"
    if not creating:
        await asyncio.to_thread(target.write_bytes, b"original\n")
    tools = FilesystemTools(SessionEnvironment.capture(workspace=Workspace.from_cwd(str(tmp_path))))
    args = {"path": str(target), "old_string": "original", "new_string": "approved"}
    tool_name = "write_file" if creating else "edit_file"
    if creating:
        args = {"path": str(target), "content": "new file\n"}
    responses = [MockResponse(tool_calls=[(tool_name, "first", args)])]
    if scenario == "reject-then-approve":
        responses.append(MockResponse(tool_calls=[(tool_name, "second", args)]))
    responses.append(MockResponse(text="Done"))
    ctx = await make_pipeline_ctx(responses, tools=tools.tools(), approval_overrides={tool_name: "require"})

    await ctx.bus.publish(UserMessage(text="Edit the file"))
    requests = await wait_for_event(ctx.events, ApprovalRequest, timeout=ENGINE_TURN_TIMEOUT)
    assert ctx.engine.is_turn_lifecycle_active
    if scenario == "interrupt-edit":
        await ctx.bus.publish(UserInterrupt())
    elif scenario == "approve-edit":
        await ctx.approve(requests[0].request_id)
    else:
        await ctx.reject(requests[0].request_id, reason="Keep the original")
    if scenario == "reject-then-approve":
        requests = await wait_for_event(ctx.events, ApprovalRequest, timeout=ENGINE_TURN_TIMEOUT, min_count=2)
        await ctx.approve(requests[1].request_id)
    await wait_for_idle(ctx)

    diff = await asyncio.to_thread(load_diff_entries_by_period, ctx.store, ctx.session_id, str(tmp_path))
    tracker = ctx.engine.mutation_tracker
    assert tracker is not None
    plan = tracker.get_rollback_plan(1)
    if scenario in {"approve-edit", "reject-then-approve"}:
        assert [(row.path, row.before_text, row.after_text) for row in diff.all_entries] == [
            (str(target), "original\n", "approved\n")
        ]
        assert [(row.path, row.before_text, row.after_text) for row in diff.per_period_entries[1]] == [
            (str(target), "original\n", "approved\n")
        ]
        assert plan.paths == {str(target)}
        assert await asyncio.to_thread(target.read_text, encoding="utf-8") == "approved\n"
    else:
        assert diff.all_entries == []
        assert diff.per_period_entries == {}
        assert plan.entries == []
        assert plan.exclusions == []
        assert plan.paths == set()
        if creating:
            assert not target.exists()
        else:
            assert await asyncio.to_thread(target.read_text, encoding="utf-8") == "original\n"
    rows = tracker.get_turn_mutations(1).mutations
    assert len(rows) == (2 if scenario == "reject-then-approve" else 1)
