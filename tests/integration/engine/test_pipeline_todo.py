# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The main agent's todo_write end to end: the session's list, the frontends' event and the save."""

from __future__ import annotations

from chrys.foundation.events.types import TodoListUpdated, UserMessage
from chrys.foundation.models.todos import TodoItem
from chrys.service.llm.mock import MockResponse
from tests.support.pipeline_helpers import extract_tool_results, wait_for_idle

_TODOS = [
    {"content": "Write tests", "status": "in_progress", "active_form": "Writing tests"},
    {"content": "Run gates", "status": "pending", "active_form": ""},
]


async def test_the_main_agent_s_todo_write_changes_the_session_s_list_and_its_save(make_pipeline_ctx) -> None:
    responses = [
        MockResponse(tool_calls=[("todo_write", "c1", {"todos": _TODOS})]),
        MockResponse(text="Planned."),
    ]
    ctx = await make_pipeline_ctx(responses, builtins=["todo"])
    updates: list[TodoListUpdated] = []

    async def collect(event: TodoListUpdated) -> None:
        updates.append(event)

    await ctx.bus.subscribe(TodoListUpdated, collect)

    await ctx.bus.publish(UserMessage(text="Plan the work"))
    await wait_for_idle(ctx)

    expected = (
        TodoItem(content="Write tests", status="in_progress", active_form="Writing tests"),
        TodoItem(content="Run gates"),
    )
    assert [result["result"] for result in extract_tool_results(ctx.events)] == [
        "Todo list updated: 2 items (0 completed, 1 in progress, 1 pending)."
    ]
    tracker = ctx.engine.todo_tracker
    assert tracker is not None
    assert tracker.snapshot() == expected
    # Host streams forward only events that name their session.
    assert [(tuple(event.items), event.session_id) for event in updates] == [(expected, ctx.session_id)]
    saved = await ctx.store.load_session(ctx.session_id)
    assert saved is not None
    assert saved["chrys_todos"] == _TODOS
