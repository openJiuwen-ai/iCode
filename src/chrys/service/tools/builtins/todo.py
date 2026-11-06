# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""todo_write: the agent keeps the session's todo list.

The list lives in the session's ``TodoTracker``, which the engine owns, saves
and restores. ``todo_write`` checks a complete new list, replaces the
tracker's and publishes ``TodoListUpdated`` for the frontends. A build without
the session's tracker (a sub-agent or workflow node whose profile lists
``todo``) gets a ``todo_write`` that reports ``todo_unavailable``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel

from chrys.foundation.events.types import TodoListUpdated
from chrys.foundation.models.todos import MAX_TODO_ITEMS, MAX_TODO_TEXT_LENGTH, TodoItem, TodoStatus
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.service.tools.kinds import KIND_TODO, tool
from chrys.service.tools.result_metadata import tool_error

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus
    from chrys.kernel import FunctionTool
    from chrys.service.todos.tracker import TodoTracker


class TodoItemParam(BaseModel):
    """One todo entry as supplied by the model."""

    content: str
    status: TodoStatus = "pending"
    active_form: str = ""


class TodoTools:
    """``todo_write`` over one session's todo list; without a list it reports ``todo_unavailable``."""

    def __init__(
        self,
        tracker: TodoTracker | None = None,
        event_bus: EventBus | None = None,
        session_id: str | None = None,
    ) -> None:
        self._tracker = tracker
        self._bus = event_bus
        self._session_id = session_id

    def tools(self) -> list[FunctionTool]:
        """The tools bound to this list."""
        return [self.todo_write]

    @tool(kind=KIND_TODO)
    async def todo_write(
        self,
        todos: Annotated[
            list[TodoItemParam],
            "The complete todo list. Replaces the previous list entirely.",
        ],
        context: FunctionInvocationContext,
    ) -> str:
        """Use this tool to create and manage a structured task list for the current session.

        It helps you track progress and shows the user a live checklist of what
        you're doing.

        **When to use**: multi-step tasks (3+ distinct steps), work spanning
        multiple files or subsystems, or several tasks given at once.
        **When NOT to use**: single straightforward tasks or purely conversational
        turns.

        **Semantics**: each call replaces the entire list — always send the full
        updated list. Remove an item by omitting it. Call ``todo_write`` at most
        once per response — never in parallel with another ``todo_write``. Keep
        exactly ONE item ``in_progress`` at a time. Mark an item ``completed``
        immediately after finishing it; never mark it completed if tests failed or
        work is partial. ``content`` is imperative ("Fix the auth bug");
        ``active_form`` is the present-continuous label shown while in progress
        ("Fixing the auth bug").

        **Language**: write ``content`` and ``active_form`` in the language the
        user writes in (e.g. a Chinese request gets Chinese todo items); when the
        user's language is unclear or mixed, default to English.
        """
        if self._tracker is None or self._bus is None:
            return tool_error("todo_unavailable", "todo tracking is not available in this session.")
        # Each call replaces the whole list, so which of two calls in one response
        # wins would depend on scheduling: none does, and the model resends one list.
        if context.same_tool_calls_in_batch > 1:
            return tool_error(
                "todo_batch_conflict",
                "multiple todo_write calls in one response; send a single call with the complete list.",
            )
        items = _checked_items(todos)
        if isinstance(items, str):
            return tool_error("todo_invalid", items)
        applied = await self._tracker.replace(items)
        # Published after replace() released the tracker's lock (EventBus.publish awaits
        # its subscribers inline); host streams drop an event without a session id.
        await self._bus.publish(TodoListUpdated(items=list(applied), session_id=self._session_id))
        completed = sum(1 for item in applied if item.status == "completed")
        in_progress = sum(1 for item in applied if item.status == "in_progress")
        pending = len(applied) - completed - in_progress
        return (
            f"Todo list updated: {len(applied)} items "
            f"({completed} completed, {in_progress} in progress, {pending} pending)."
        )


def _checked_items(todos: list[TodoItemParam]) -> tuple[TodoItem, ...] | str:
    """The list as tracker items, or why it is refused: the bounds the schema does not state."""
    if len(todos) > MAX_TODO_ITEMS:
        return f"too many todo items ({len(todos)}); the list is capped at {MAX_TODO_ITEMS}."
    items: list[TodoItem] = []
    # The kernel hands validated arguments over as plain values.
    for index, entry in enumerate(TodoItemParam.model_validate(todo) for todo in todos):
        if not entry.content.strip():
            return f"todo item {index + 1} has empty content."
        if len(entry.content) > MAX_TODO_TEXT_LENGTH:
            return f"todo item {index + 1} content exceeds {MAX_TODO_TEXT_LENGTH} characters."
        if len(entry.active_form) > MAX_TODO_TEXT_LENGTH:
            return f"todo item {index + 1} active_form exceeds {MAX_TODO_TEXT_LENGTH} characters."
        items.append(TodoItem(content=entry.content, status=entry.status, active_form=entry.active_form))
    return tuple(items)
