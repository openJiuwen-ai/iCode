# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the todo_write tool."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import TodoListUpdated
from chrys.foundation.models.todos import MAX_TODO_ITEMS, MAX_TODO_TEXT_LENGTH, TodoItem
from chrys.foundation.tool_result_metadata import TOOL_ERROR_KIND_METADATA_KEY, TOOL_FAILED_METADATA_KEY
from chrys.kernel import ChatResponse, Content, Message
from chrys.kernel.middleware import ChatMiddlewareLayer, FunctionInvocationContext
from chrys.service.todos.tracker import TodoTracker
from chrys.service.tools.builtins.todo import TodoTools
from chrys.service.tools.result_metadata import tool_result_metadata
from tests.support.transcript_invariants import InvariantCheckedToolLoopLayer
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from collections.abc import Iterator

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ScriptedTodoClient:
    """Innermost fake wire client replaying scripted assistant turns."""

    def __init__(self, turns: list[list[Content] | str]) -> None:
        self.turns = list(turns)
        self.calls: list[list[Message]] = []

    def get_response(
        self,
        messages: list[Message],
        *,
        stream: bool = False,
        options: dict[str, object] | None = None,
        **_kwargs: object,
    ) -> ChatResponse:
        self.calls.append(list(messages))
        assert not stream
        assert options is not None
        turn = self.turns.pop(0)
        contents: list[Any] = [turn] if isinstance(turn, str) else list(turn)
        return ChatResponse(messages=[Message(role="assistant", contents=contents)])


class _TodoSession:
    """A session's todo list with a pre-existing entry, its bus and what the bus published."""

    SENTINEL = (TodoItem(content="pre-existing"),)

    def __init__(self) -> None:
        self.bus = EventBus()
        self.tracker = TodoTracker()
        self.published: list[TodoListUpdated] = []
        self.tools = TodoTools(self.tracker, self.bus, session_id="sess-1")

    async def start(self) -> _TodoSession:
        await self.tracker.replace(self.SENTINEL)
        await self.bus.subscribe(TodoListUpdated, self._collect)
        return self

    async def _collect(self, event: TodoListUpdated) -> None:
        self.published.append(event)

    async def run(self, turns: list[list[Content] | str]) -> tuple[str, _ScriptedTodoClient]:
        """Run *turns* through the kernel tool loop with this list's ``todo_write``."""
        wire = _ScriptedTodoClient(turns)
        client = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire))
        response = await client.get_response(
            [Message(role="user", contents=["go"])],
            options={"tools": self.tools.tools()},
        )
        return response.text, wire


def _todo_call(call_id: str, todos: list[dict[str, str]]) -> Content:
    return Content.from_function_call(call_id=call_id, name="todo_write", arguments={"todos": todos})


def _function_results(messages: list[Message]) -> list[Content]:
    return [item for msg in messages for item in msg.contents if item.type == "function_result"]


@pytest.fixture
def tool_metadata() -> Iterator[dict[str, object]]:
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        yield metadata
    finally:
        tool_result_metadata.reset(token)


async def _write(tools: TodoTools, todos: object) -> Any:
    return await tools.todo_write.invoke(arguments={"todos": todos}, skip_parsing=True)


# ---------------------------------------------------------------------------
# Schema and the build without a list
# ---------------------------------------------------------------------------


async def test_todo_write_without_a_todo_list_records_structured_failure(tool_metadata: dict[str, object]) -> None:
    """A build without the session's list (a sub-agent's) gets a structured tool_error."""
    result = await _write(TodoTools(), [{"content": "x"}])

    assert result == "Error: todo tracking is not available in this session."
    assert tool_metadata[TOOL_FAILED_METADATA_KEY] is True
    assert tool_metadata[TOOL_ERROR_KIND_METADATA_KEY] == "todo_unavailable"


def test_todo_write_schema_expresses_status_literal_and_replacement() -> None:
    """The status Literal lives in the JSON schema — kernel argument validation
    owns type/shape/status errors — and the injected call context is not in it."""
    schema = TodoTools().todo_write.parameters()
    assert schema["required"] == ["todos"]
    assert list(schema["properties"]) == ["todos"]
    assert "Replaces the previous list entirely" in schema["properties"]["todos"]["description"]
    flattened = json.dumps(schema)
    for status in ("pending", "in_progress", "completed"):
        assert status in flattened


# ---------------------------------------------------------------------------
# The tool — validation, apply and publish
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("todos", "expected_fragment"),
    [
        ([{"content": f"item {i}"} for i in range(MAX_TODO_ITEMS + 1)], f"too many todo items ({MAX_TODO_ITEMS + 1})"),
        ([{"content": "x" * (MAX_TODO_TEXT_LENGTH + 1)}], "content exceeds"),
        ([{"content": "ok", "active_form": "y" * (MAX_TODO_TEXT_LENGTH + 1)}], "active_form exceeds"),
        ([{"content": "   "}], "empty content"),
    ],
    ids=["too-many", "long-content", "long-active-form", "blank-content"],
)
async def test_todo_write_rejects_what_the_schema_cannot_state_without_touching_the_list(
    todos: list[dict[str, str]], expected_fragment: str, tool_metadata: dict[str, object]
) -> None:
    session = await _TodoSession().start()

    result = await _write(session.tools, todos)

    assert result.startswith("Error:")
    assert expected_fragment in result
    assert tool_metadata[TOOL_FAILED_METADATA_KEY] is True
    assert tool_metadata[TOOL_ERROR_KIND_METADATA_KEY] == "todo_invalid"
    assert session.tracker.snapshot() == _TodoSession.SENTINEL
    assert session.published == []


async def test_todo_write_in_a_batch_with_another_is_refused(tool_metadata: dict[str, object]) -> None:
    session = await _TodoSession().start()
    tool = session.tools.todo_write
    context = FunctionInvocationContext(function=tool, arguments={})
    context.same_tool_calls_in_batch = 2

    result = await tool.invoke(arguments={"todos": [{"content": "would win"}]}, context=context, skip_parsing=True)

    assert result == "Error: multiple todo_write calls in one response; send a single call with the complete list."
    assert tool_metadata[TOOL_ERROR_KIND_METADATA_KEY] == "todo_batch_conflict"
    assert session.tracker.snapshot() == _TodoSession.SENTINEL
    assert session.published == []


async def test_todo_write_applies_and_publishes_after_lock_release() -> None:
    """The event carries the applied list; publish happens after the tracker
    lock is released — a subscriber that awaits a tracker WRITER completes
    instead of deadlocking."""
    session = await _TodoSession().start()

    async def rewrite_from_event(event: TodoListUpdated) -> None:
        # A lock-held publish would deadlock here (EventBus.publish awaits
        # subscribers inline; replace() needs the same lock).
        await asyncio.wait_for(session.tracker.replace(tuple(event.items)), timeout=1)

    await session.bus.subscribe(TodoListUpdated, rewrite_from_event)
    todos = [
        {"content": "Fix the auth bug", "status": "completed", "active_form": "Fixing the auth bug"},
        {"content": "Run tests", "status": "in_progress", "active_form": "Running tests"},
        {"content": "Update docs"},
    ]

    result = await asyncio.wait_for(_write(session.tools, todos), timeout=5)

    expected = (
        TodoItem(content="Fix the auth bug", status="completed", active_form="Fixing the auth bug"),
        TodoItem(content="Run tests", status="in_progress", active_form="Running tests"),
        TodoItem(content="Update docs"),
    )
    assert result == "Todo list updated: 3 items (1 completed, 1 in progress, 1 pending)."
    assert session.tracker.snapshot() == expected
    assert [(tuple(event.items), event.session_id) for event in session.published] == [(expected, "sess-1")]


async def test_todo_write_cancelled_while_publishing_keeps_the_applied_list() -> None:
    """The list is replaced before the event goes out: a call cancelled while a
    frontend handles the event has already changed the list the session saves."""
    session = await _TodoSession().start()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_frontend(_event: TodoListUpdated) -> None:
        entered.set()
        await release.wait()

    await session.bus.subscribe(TodoListUpdated, slow_frontend)
    call = asyncio.create_task(_write(session.tools, [{"content": "new list"}]))
    await wait_for(lambda: entered.is_set() or call.done(), description="todo_write publishing its list")
    assert not call.done()

    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call

    assert session.tracker.snapshot() == (TodoItem(content="new list"),)


# ---------------------------------------------------------------------------
# Through the kernel tool loop
# ---------------------------------------------------------------------------


async def test_todo_write_bad_status_fails_kernel_validation_before_the_tool_runs() -> None:
    """A bad status Literal is an argument_parsing error stamped by the KERNEL;
    the tool never runs."""
    session = await _TodoSession().start()

    text, wire = await session.run([[_todo_call("c1", [{"content": "x", "status": "done"}])], "done"])

    assert text == "done"
    results = _function_results(wire.calls[1])
    assert len(results) == 1
    result_text = str(results[0].result)
    assert result_text.startswith("Error: Invalid arguments for 'todo_write':")
    assert "todos[0].status: expected" in result_text
    assert '"pending" | "in_progress" | "completed"' in result_text
    assert "Expected: {todos: [{content: string," in result_text
    assert len(result_text) < 300
    assert results[0].additional_properties[TOOL_FAILED_METADATA_KEY] is True
    assert results[0].additional_properties[TOOL_ERROR_KIND_METADATA_KEY] == "argument_parsing"
    assert session.tracker.snapshot() == _TodoSession.SENTINEL
    assert session.published == []


async def test_todo_write_duplicate_batch_rejects_both_and_leaves_tracker_unchanged() -> None:
    """Two schema-valid todo_write calls in ONE batch → two conflict errors and
    the tracker list content is unchanged (no scheduling-defined winner)."""
    session = await _TodoSession().start()

    text, wire = await session.run(
        [[_todo_call("c1", [{"content": "first list"}]), _todo_call("c2", [{"content": "second list"}])], "done"]
    )

    assert text == "done"
    results = _function_results(wire.calls[1])
    assert len(results) == 2
    for result in results:
        assert result.result == (
            "Error: multiple todo_write calls in one response; send a single call with the complete list."
        )
    assert session.tracker.snapshot() == _TodoSession.SENTINEL
    assert session.published == []


async def test_todo_write_singleton_applies_through_loop() -> None:
    session = await _TodoSession().start()

    text, wire = await session.run(
        [
            [
                _todo_call(
                    "c1",
                    [
                        {"content": "Write tests", "status": "in_progress", "active_form": "Writing tests"},
                        {"content": "Run gates"},
                    ],
                )
            ],
            "done",
        ]
    )

    assert text == "done"
    results = _function_results(wire.calls[1])
    assert len(results) == 1
    assert results[0].result == "Todo list updated: 2 items (0 completed, 1 in progress, 1 pending)."
    assert session.tracker.snapshot() == (
        TodoItem(content="Write tests", status="in_progress", active_form="Writing tests"),
        TodoItem(content="Run gates"),
    )
    assert [event.session_id for event in session.published] == ["sess-1"]


async def test_todo_write_back_to_back_singleton_batches_both_apply() -> None:
    """Two sequential single-call batches through the SAME tool both apply —
    the duplicate count is per batch, not per run."""
    session = await _TodoSession().start()

    text, _wire = await session.run(
        [
            [_todo_call("c1", [{"content": "step one", "status": "in_progress"}])],
            [
                _todo_call(
                    "c2",
                    [{"content": "step one", "status": "completed"}, {"content": "step two", "status": "in_progress"}],
                )
            ],
            "done",
        ]
    )

    assert text == "done"
    assert session.tracker.snapshot() == (
        TodoItem(content="step one", status="completed"),
        TodoItem(content="step two", status="in_progress"),
    )
    assert [len(event.items) for event in session.published] == [1, 2]
