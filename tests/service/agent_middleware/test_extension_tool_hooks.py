# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Extension tool hooks as both tool event middlewares run them: before a call to refuse or change it, after to read or change its result."""

from __future__ import annotations

import asyncio
import logging
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

import pytest

from chrys.extensions import Extension, ToolCall, ToolError, ToolResult
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import InvocationToolCallResult
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.kernel import Content
from chrys.kernel.exceptions import GENERIC_TOOL_ERROR_TEXT
from chrys.service.agent_middleware import SubAgentEventMiddleware, ToolEventMiddleware
from chrys.service.agent_middleware._metadata_keys import _REJECTION_SOURCE_KEY
from chrys.service.agent_middleware.events.extension_hooks import ExtensionToolHooks
from chrys.service.extensions import bind_extension
from tests.service.agent_middleware._event_fakes import _ctx
from tests.service.extensions._support import extension_build
from tests.support.event_capture import capture_events

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from chrys.kernel.middleware import FunctionMiddleware

_TURN = InvocationOrigin("turn", "s-1", "turn-test", None)


def _main(bus: EventBus, hooks: ExtensionToolHooks) -> FunctionMiddleware:
    return ToolEventMiddleware(bus, session_id="s-1", origin=_TURN, extension_hooks=hooks)


def _sub_agent(bus: EventBus, hooks: ExtensionToolHooks) -> FunctionMiddleware:
    origin = InvocationOrigin("sub_agent", "s-1", "inv-1", _TURN)
    return SubAgentEventMiddleware(
        bus, agent_name="Explore", invocation_id="inv-1", session_id="s-1", origin=origin, extension_hooks=hooks
    )


_MIDDLEWARES = pytest.mark.parametrize("make", [_main, _sub_agent], ids=["main", "sub_agent"])


def _hooks(tmp_path: Path, *extensions: tuple[str, Extension]) -> ExtensionToolHooks:
    before: list[Any] = []
    after: list[Any] = []
    for extension_id, extension in extensions:
        bound = bind_extension(extension_id, extension, extension_build(tmp_path))
        before.extend(bound.before_tool_hooks)
        after.extend(bound.after_tool_hooks)
    return ExtensionToolHooks(tuple(before), tuple(after))


class _Tool:
    """A tool that records the arguments it ran with and returns *result*."""

    def __init__(self, result: Any = "ran") -> None:
        self.ran_with: list[dict[str, Any]] = []
        self.result = result

    def runner(self, context: Any) -> Callable[[], Any]:
        async def run() -> None:
            self.ran_with.append(dict(context.arguments))
            if isinstance(self.result, Exception):
                raise self.result
            context.result = self.result

        return run


async def _call(
    make: Callable[[EventBus, ExtensionToolHooks], FunctionMiddleware],
    hooks: ExtensionToolHooks,
    tool: _Tool,
    args: dict[str, Any],
) -> tuple[Any, InvocationToolCallResult]:
    bus = EventBus()
    results = await capture_events(bus, InvocationToolCallResult)
    context = _ctx("run_shell", "shell", args=args)
    await make(bus, hooks).process(context, tool.runner(context))
    (result,) = results
    return context, result


@_MIDDLEWARES
async def test_before_hooks_change_the_call_in_turn_and_a_tool_error_refuses_it(
    make: Callable[[EventBus, ExtensionToolHooks], FunctionMiddleware], tmp_path: Path
) -> None:
    seen: list[ToolCall] = []
    after: list[ToolResult] = []
    extension = Extension()

    @extension.before_tool
    async def quote(call: ToolCall) -> dict[str, Any]:
        seen.append(call)
        return {**call.arguments, "command": f"'{call.arguments['command']}'"}

    @extension.before_tool
    async def guard(call: ToolCall) -> None:
        seen.append(call)
        if "rm" in call.arguments["command"]:
            # The prefix the model reads anyway is not doubled.
            raise ToolError("Error: Not on this machine.")

    @extension.after_tool
    async def record(_call: ToolCall, result: ToolResult) -> None:
        after.append(result)

    hooks = _hooks(tmp_path, ("acme", extension))
    tool = _Tool()
    context, published = await _call(make, hooks, tool, {"command": "ls"})
    assert tool.ran_with == [{"command": "'ls'"}]
    assert [call.arguments for call in seen] == [{"command": "ls"}, {"command": "'ls'"}]
    assert (seen[0].name, seen[0].kind, seen[0].call_id == seen[1].call_id) == ("run_shell", "shell", True)
    assert seen[0].context.agent_name == "Coder"
    assert published.result == "ran"

    seen.clear()
    tool = _Tool()
    context, published = await _call(make, hooks, tool, {"command": "rm -rf /"})
    assert tool.ran_with == []
    assert context.result == published.result == "Error: Not on this machine."
    assert context.metadata[_REJECTION_SOURCE_KEY] == "hook"
    assert [(r.text, r.failed, r.rejected) for r in after] == [
        ("ran", False, False),
        ("Error: Not on this machine.", True, True),
    ]


async def _hangs(_call: ToolCall) -> None:
    await asyncio.Event().wait()


async def _raises(_call: ToolCall) -> None:
    raise RuntimeError("token=abc123")


async def _returns_text(_call: ToolCall) -> str:
    return "ls"


async def _returns_an_object(_call: ToolCall) -> dict[str, Any]:
    # It would reach the tool as an object while later hooks and the approval saw its text.
    return {"command": PurePosixPath("ls")}


@pytest.mark.parametrize(
    ("hook", "reason"),
    [
        (_raises, "RuntimeError"),
        (_hangs, "it took longer than 0.05 seconds"),
        (_returns_text, "it returned str, not a dict of JSON arguments"),
        (_returns_an_object, "it returned arguments that are not plain JSON"),
    ],
    ids=["raises", "hangs", "returns_text", "returns_an_object"],
)
async def test_a_before_hook_that_fails_refuses_the_call_naming_itself_not_the_error(
    hook: Any, reason: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    extension = Extension()
    extension.before_tool(timeout=0.05)(hook)
    tool = _Tool()
    with caplog.at_level(logging.WARNING):
        context, _published = await _call(_main, _hooks(tmp_path, ("acme", extension)), tool, {"command": "ls"})
    assert tool.ran_with == []
    name = hook.__name__
    assert (
        context.result == f"Error: Extension acme's before_tool hook {name} failed ({reason}), so the call did not run."
    )
    assert f"Extension acme tool hook {name}" in caplog.text


@_MIDDLEWARES
async def test_after_hooks_replace_the_text_in_turn_keeping_its_images(
    make: Callable[[EventBus, ExtensionToolHooks], FunctionMiddleware], tmp_path: Path
) -> None:
    image = Content.from_uri("data:image/png;base64,iVBORw0KGgo=", media_type="image/png")
    audio = Content.from_uri("data:audio/wav;base64,UklGRg==", media_type="audio/wav")
    first, second = Extension(), Extension()

    @first.after_tool
    async def shout(call: ToolCall, result: ToolResult) -> str:
        call.arguments["command"] = "edited"  # its own copy
        return result.text.upper()

    @second.after_tool
    async def sign(call: ToolCall, result: ToolResult) -> str:
        return f"{result.text} (checked {call.arguments['command']})"

    hooks = _hooks(tmp_path, ("first", first), ("second", second))
    tool = _Tool([Content.from_text("listing"), image, audio])
    context, published = await _call(make, hooks, tool, {"command": "ls"})
    # The card shows the attachments' placeholders after the new text, as it does for any such result.
    assert published.result.startswith("LISTING (checked ls)\n[image/png image]\n")
    assert [c.text for c in context.result if c.type == "text"] == ["LISTING (checked ls)"]
    assert context.result[1:] == [image, audio]
    assert published.image_contents == [image]


async def test_a_failing_after_hook_withholds_the_result_and_later_hooks_see_the_note(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[str] = []
    extension = Extension()

    @extension.after_tool
    async def redact(_call: ToolCall, _result: ToolResult) -> str:
        raise RuntimeError("secret in /home/me")

    @extension.after_tool
    async def record(_call: ToolCall, result: ToolResult) -> None:
        seen.append(result.text)

    with caplog.at_level(logging.WARNING):
        context, _published = await _call(_main, _hooks(tmp_path, ("acme", extension)), _Tool(), {"command": "ls"})
    # A hook's name is its qualified name; a module-level hook's is its plain name.
    # AIxCoding branding: extension after_tool failure note assertion
    note = f"[Extension acme's after_tool hook {redact.__qualname__} failed (RuntimeError), so AIxCoding withheld this result.]"
    assert context.result == note
    assert seen == [note]
    assert "secret in /home/me" in caplog.text


async def test_a_tool_that_raised_shows_hooks_the_model_s_error_text_and_keeps_it(tmp_path: Path) -> None:
    seen: list[ToolResult] = []
    extension = Extension()

    @extension.after_tool
    async def rewrite(_call: ToolCall, result: ToolResult) -> str:
        seen.append(result)
        return "all good"

    tool = _Tool(RuntimeError("failed reading /home/me/.netrc"))
    bus = EventBus()
    context = _ctx("run_shell", "shell", args={"command": "ls"})
    with pytest.raises(RuntimeError):
        await _main(bus, _hooks(tmp_path, ("acme", extension))).process(context, tool.runner(context))
    ((text, failed, rejected),) = [(r.text, r.failed, r.rejected) for r in seen]
    assert (text, failed, rejected) == (GENERIC_TOOL_ERROR_TEXT, True, False)
    assert context.result is None
