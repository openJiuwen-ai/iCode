# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Acceptance tests for the chrys-owned tool loop.

Pins the core loop invariants in ``chrys.kernel.loop`` against the production
composition ``ToolLoopLayer(ChatMiddlewareLayer(inner))``: stack delegation,
blocking and streaming loop mechanics, batch stamping, execution-boundary
upcasting, the tool-result ceiling and the loop-defaults drift pin. The
scenario families live in the ``test_loop_*.py`` siblings and the shared
doubles in ``_fakes.py``.
"""

from __future__ import annotations

import asyncio
import functools
from typing import TYPE_CHECKING, Annotated

import pytest
from pydantic import BaseModel, model_validator

from chrys.foundation.tool_execution_stamp import EXECUTION_STAMP_KEY
from chrys.foundation.tool_invocation_order import TOOL_INVOCATION_ORDER_KEY
from chrys.foundation.tool_result_metadata import (
    COMMITTED_ERROR_TEXT_KEY,
    TOOL_ERROR_KIND_METADATA_KEY,
    TOOL_ERROR_MESSAGE_METADATA_KEY,
    TOOL_FAILED_METADATA_KEY,
)
from chrys.foundation.trajectory.ids import is_valid_analytics_id
from chrys.foundation.trajectory.metadata import (
    ANALYTICS_ITEM_ID_KEY,
    OPERATION_ID_KEY,
)
from chrys.foundation.trajectory_timing import TRAJECTORY_TIMING_KEY
from chrys.kernel import AgentSession, FunctionTool, LoopRecorder, tool
from chrys.kernel.exceptions import ModelVisibleToolError, ToolExecutionException
from chrys.kernel.loop import (
    DEFAULT_MAX_CONSECUTIVE_ERRORS,
    DEFAULT_MAX_ITERATIONS,
    ToolLoopLayer,
    _extract_function_calls,
)
from chrys.kernel.middleware import (
    ChatMiddlewareLayer,
    FunctionInvocationContext,
    FunctionMiddleware,
)
from chrys.kernel.types import (
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    ResponseStream,
)
from tests.kernel._fakes import (
    _assert_actionable_argument_error,
    _call_response,
    _call_update,
    _final_response,
    _make_tool,
    _ProbeChat,
    _ProbeFunction,
    _result_contents,
    _ScriptedClient,
    _stack,
    _TerminateFunction,
    _text_response,
    _text_update,
    _user,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@pytest.mark.asyncio
async def test_tool_result_ceiling_bounds_transcript_and_raw_journal() -> None:
    @tool(name="large")
    async def large() -> str:
        return "HEAD" + "x" * 10_000 + "TAIL"

    recorder = LoopRecorder()
    layer, _wire = _stack(
        [_call_response(("c1", "large", {})), _text_response()],
        tool_result_ceiling_tokens=100,
    )

    response = await layer.get_response(
        [_user()],
        options={"tools": [large]},
        client_kwargs={"loop_recorder": recorder},
    )

    result = _result_contents(response)[0]
    assert isinstance(result.result, str)
    assert "truncated" in result.result
    assert len(result.result) < 1_000
    journal_results = [
        content
        for message in recorder.loop_messages or []
        for content in message.contents
        if content.type == "function_result"
    ]
    assert len(journal_results) == 1
    assert journal_results[0].result == result.result


@pytest.mark.asyncio
async def test_tool_result_ceiling_bounds_prebuilt_middleware_termination() -> None:
    prebuilt = Content.from_function_result("cached", result="x" * 10_000)
    layer, _wire = _stack(
        [_call_response(("c1", "echo", {"text": "unused"}))],
        tool_result_ceiling_tokens=100,
    )

    response = await layer.get_response(
        [_user()],
        options={"tools": [_make_tool()]},
        middleware=[_TerminateFunction(exc_result=prebuilt)],
    )

    result = _result_contents(response)[0]
    assert result is not prebuilt
    assert result.call_id == "c1"
    assert isinstance(result.result, str)
    assert "truncated" in result.result
    assert len(result.result) < 1_000


# ---------------------------------------------------------------------------
# Stack composition and delegation
# ---------------------------------------------------------------------------


class TestStackAndDelegation:
    @pytest.mark.asyncio
    async def test_loop_drives_chat_layer_then_wire(self) -> None:
        """Production stack order: loop → chat middleware → wire client."""
        probe = _ProbeChat()
        layer, wire = _stack([_call_response(("c1", "echo", {"text": "a"})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [_make_tool()]}, middleware=[probe])
        assert len(wire.calls) == 2
        assert probe.calls == 2, "per-call chat middleware must run on every loop iteration"
        assert response.messages[-1].contents[0].text == "done"

    @pytest.mark.asyncio
    async def test_explicit_none_client_middleware_merges_with_per_call_middleware(self) -> None:
        probe = _ProbeChat()
        layer, _wire = _stack([_text_response()])

        response = await layer.get_response(
            [_user()],
            middleware=[probe],
            client_kwargs={"middleware": None},
        )

        assert probe.calls == 1
        assert response.messages[-1].text == "done"

    @pytest.mark.asyncio
    async def test_single_per_call_middleware_is_accepted_like_a_list(self) -> None:
        chat_probe = _ProbeChat()
        function_probe = _ProbeFunction()
        layer, wire = _stack([_call_response(("c1", "echo", {"text": "a"})), _text_response()])

        response = await layer.get_response(
            [_user()],
            options={"tools": [_make_tool()]},
            middleware=function_probe,
            client_kwargs={"middleware": chat_probe},
        )

        assert len(wire.calls) == 2
        assert chat_probe.calls == 2
        assert [ctx.function.name for ctx in function_probe.contexts] == ["echo"]
        assert response.messages[-1].text == "done"

    def test_unsupported_per_call_middleware_rejected_synchronously(self) -> None:
        layer, wire = _stack([_text_response()])

        async def naked(_context: object, _call_next: object) -> None:
            return None

        with pytest.raises(TypeError, match="plain callables are not supported"):
            layer.get_response([_user()], middleware=naked)  # type: ignore[arg-type]

        assert wire.calls == []

    def test_getattr_two_hop_delegation(self) -> None:
        wire = _ScriptedClient([])
        wire.model = "wire-model"  # type: ignore[attr-defined]
        wire.STORES_BY_DEFAULT = True  # type: ignore[attr-defined]
        layer = ToolLoopLayer(ChatMiddlewareLayer(wire))
        assert layer.model == "wire-model"
        assert layer.STORES_BY_DEFAULT is True

    def test_getattr_attribute_error_passthrough(self) -> None:
        layer = ToolLoopLayer(ChatMiddlewareLayer(_ScriptedClient([])))
        with pytest.raises(AttributeError):
            _ = layer.does_not_exist
        with pytest.raises(AttributeError):
            _ = ChatMiddlewareLayer(_ScriptedClient([])).also_missing

    def test_ctor_rejects_chat_middleware(self) -> None:
        with pytest.raises(TypeError, match="chat middleware belongs to the inner"):
            ToolLoopLayer(_ScriptedClient([]), middleware=[_ProbeChat()])

    def test_ctor_defaults_mirror_framework_values(self) -> None:
        layer = ToolLoopLayer(_ScriptedClient([]))
        assert layer.max_iterations == DEFAULT_MAX_ITERATIONS
        assert layer.max_consecutive_errors == DEFAULT_MAX_CONSECUTIVE_ERRORS
        assert layer.max_function_calls is None


# ---------------------------------------------------------------------------
# Non-streaming loop
# ---------------------------------------------------------------------------


def _raised_from(exc: BaseException, cause: BaseException) -> BaseException:
    """Return *exc* as ``raise exc from cause`` leaves it."""
    exc.__cause__ = cause
    return exc


def _wrapping(message: str, inner: Exception) -> BaseException:
    """Return what ``raise ToolExecutionException(message, inner_exception=inner) from inner`` raises."""
    return _raised_from(ToolExecutionException(message, inner_exception=inner), inner)


def _cause_chain(depth: int) -> BaseException:
    """Return ``RuntimeError: <depth - 1>``, each raised from the next lower number down to ``OSError: 0``."""
    exc: BaseException = OSError("0")
    for number in range(1, depth):
        exc = _raised_from(RuntimeError(str(number)), exc)
    return exc


class TestNonStreamingLoop:
    @pytest.mark.asyncio
    async def test_informational_function_call_never_executes_same_named_local_tool(self) -> None:
        events: list[str] = []
        hosted_call = Content.from_function_call(
            "hosted-1",
            "echo",
            arguments={"text": "must stay remote"},
            informational_only=True,
        )
        response = ChatResponse(messages=[Message(role="assistant", contents=[hosted_call])])
        layer, wire = _stack([response])

        result = await layer.get_response([_user()], options={"tools": [_make_tool(events)]})

        assert result is response
        assert events == []
        assert len(wire.calls) == 1
        assert result.messages[0].contents == [hosted_call]

    @pytest.mark.asyncio
    async def test_single_turn_no_tool_calls(self) -> None:
        layer, wire = _stack([_text_response("plain")])
        response = await layer.get_response([_user()], options={"tools": [_make_tool()]})
        assert len(wire.calls) == 1
        assert response.messages[-1].contents[0].text == "plain"

    @pytest.mark.asyncio
    async def test_two_iteration_loop_extends_prepped_and_prepends_fcc(self) -> None:
        events: list[str] = []
        layer, wire = _stack([_call_response(("c1", "echo", {"text": "a"})), _text_response("final")])
        response = await layer.get_response([_user("q")], options={"tools": [_make_tool(events)]})

        assert events == ["tool:echo:a"]
        # Second wire call sees user + assistant(call) + tool(result).
        second = wire.calls[1]["messages"]
        assert [m.role for m in second] == ["user", "assistant", "tool"]
        # Final response carries the accumulated loop transcript (fcc prepend).
        assert [m.role for m in response.messages] == ["assistant", "tool", "assistant"]
        results = _result_contents(response)
        assert len(results) == 1
        assert results[0].call_id == "c1"
        assert "echo:a" in str(results[0].result)

    @pytest.mark.asyncio
    async def test_successful_tool_result_preserves_function_call_metadata(self) -> None:
        function_call = Content.from_function_call("c1", "echo", arguments={"text": "a"})
        function_call.additional_properties["provider_marker"] = "kept"
        layer, _wire = _stack([ChatResponse(messages=[Message("assistant", [function_call])]), _text_response("final")])

        response = await layer.get_response([_user("q")], options={"tools": [_make_tool()]})

        results = _result_contents(response)
        assert len(results) == 1
        assert results[0].additional_properties["provider_marker"] == "kept"
        assert TOOL_INVOCATION_ORDER_KEY not in results[0].additional_properties
        timing = function_call.additional_properties[TRAJECTORY_TIMING_KEY]
        assert timing == results[0].additional_properties[TRAJECTORY_TIMING_KEY]
        assert timing["duration_ms"] == 0
        call_props = function_call.additional_properties
        assert {
            key: value
            for key, value in call_props.items()
            if key not in (TRAJECTORY_TIMING_KEY, ANALYTICS_ITEM_ID_KEY, OPERATION_ID_KEY)
        } == {"provider_marker": "kept", TOOL_INVOCATION_ORDER_KEY: 0}
        # Landing minted the call's item id and tool operation id; the result
        # shares the operation and owns a distinct item id.
        assert is_valid_analytics_id(call_props[ANALYTICS_ITEM_ID_KEY])
        assert is_valid_analytics_id(call_props[OPERATION_ID_KEY])
        result_props = results[0].additional_properties
        assert result_props[OPERATION_ID_KEY] == call_props[OPERATION_ID_KEY]
        assert is_valid_analytics_id(result_props[ANALYTICS_ITEM_ID_KEY])
        assert result_props[ANALYTICS_ITEM_ID_KEY] != call_props[ANALYTICS_ITEM_ID_KEY]

    @pytest.mark.asyncio
    async def test_caller_containers_not_mutated(self) -> None:
        tools = [_make_tool()]
        options = {"tools": tools}
        caller_messages = [_user("q")]
        layer, _wire = _stack([_call_response(("c1", "echo", {"text": "a"})), _text_response()])
        await layer.get_response(caller_messages, options=options)
        assert options == {"tools": tools}, "caller options must not gain loop-internal keys"
        assert len(tools) == 1
        assert len(caller_messages) == 1

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error_result_without_raising(self) -> None:
        function_call = Content.from_function_call("c1", "ghost", arguments={})
        function_call.additional_properties["existing"] = "kept"
        layer, _wire = _stack([ChatResponse(messages=[Message("assistant", [function_call])]), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [_make_tool()]})
        results = _result_contents(response)
        assert len(results) == 1
        assert 'Requested function "ghost" not found' in str(results[0].result)
        assert results[0].exception is not None
        assert results[0].additional_properties["existing"] == "kept"
        assert results[0].additional_properties[TOOL_FAILED_METADATA_KEY] is True
        assert results[0].additional_properties[TOOL_ERROR_KIND_METADATA_KEY] == "tool_not_found"
        assert results[0].additional_properties[TOOL_ERROR_MESSAGE_METADATA_KEY] == (
            'Requested function "ghost" not found.'
        )
        assert EXECUTION_STAMP_KEY not in results[0].additional_properties
        assert TOOL_INVOCATION_ORDER_KEY not in results[0].additional_properties
        timing = function_call.additional_properties[TRAJECTORY_TIMING_KEY]
        assert timing == results[0].additional_properties[TRAJECTORY_TIMING_KEY]
        assert timing["duration_ms"] == 0
        assert timing["started_at"] == timing["finished_at"]
        assert {
            key: value
            for key, value in function_call.additional_properties.items()
            if key not in (TRAJECTORY_TIMING_KEY, ANALYTICS_ITEM_ID_KEY, OPERATION_ID_KEY)
        } == {"existing": "kept", TOOL_INVOCATION_ORDER_KEY: 0}

    @pytest.mark.asyncio
    async def test_prevalidation_failure_skips_function_pipeline(self) -> None:
        probe = _ProbeFunction()
        function_call = Content.from_function_call("c1", "echo", arguments={"wrong_arg": 1})
        function_call.additional_properties["existing"] = "kept"
        layer, _wire = _stack([ChatResponse(messages=[Message("assistant", [function_call])]), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [_make_tool()]}, middleware=[probe])
        results = _result_contents(response)
        assert len(results) == 1
        message = _assert_actionable_argument_error(
            results[0],
            "echo",
            "unknown 'wrong_arg'",
            "missing 'text'",
            "Expected: {text: string}",
        )
        assert results[0].additional_properties["existing"] == "kept"
        assert results[0].additional_properties[TOOL_FAILED_METADATA_KEY] is True
        assert results[0].additional_properties[TOOL_ERROR_MESSAGE_METADATA_KEY] == message.removeprefix("Error: ")
        assert EXECUTION_STAMP_KEY not in results[0].additional_properties
        assert TOOL_INVOCATION_ORDER_KEY not in results[0].additional_properties
        timing = function_call.additional_properties[TRAJECTORY_TIMING_KEY]
        assert timing == results[0].additional_properties[TRAJECTORY_TIMING_KEY]
        assert timing["duration_ms"] == 0
        assert timing["started_at"] == timing["finished_at"]
        assert {
            key: value
            for key, value in function_call.additional_properties.items()
            if key not in (TRAJECTORY_TIMING_KEY, ANALYTICS_ITEM_ID_KEY, OPERATION_ID_KEY)
        } == {"existing": "kept", TOOL_INVOCATION_ORDER_KEY: 0}
        assert probe.contexts == [], "bad arguments must fail before the middleware pipeline runs"

    @pytest.mark.asyncio
    async def test_typed_tool_rejects_unknown_argument_and_suggests_valid_names(self) -> None:
        received: list[str] = []

        @tool(name="read_file")
        async def read_file(
            path: Annotated[str, "Absolute or relative path to the file to read."],
            max_tokens: int = 5000,
            line_range: list[int] | None = None,
        ) -> str:
            received.append(path)
            return path

        layer, _wire = _stack(
            [
                _call_response(
                    ("c1", "read_file", {"file_path": "wrong.py", "max_tokens": 4000}),
                    ("c2", "read_file", {"path": "right.py", "file_path": "wrong.py"}),
                ),
                _text_response("recovered"),
            ]
        )
        response = await layer.get_response([_user()], options={"tools": [read_file]})

        assert received == []
        missing_path, extra_only = _result_contents(response)
        missing_path_text = _assert_actionable_argument_error(
            missing_path,
            "read_file",
            "unknown 'file_path' (did you mean 'path'?)",
            "missing 'path'",
            "Expected: {path: string, max_tokens?: integer, line_range?: [integer] | null}",
        )
        assert len(missing_path_text) < 220
        extra_only_text = _assert_actionable_argument_error(
            extra_only,
            "read_file",
            "unknown 'file_path' (did you mean 'path'?)",
        )
        assert "missing '" not in extra_only_text

    @pytest.mark.asyncio
    async def test_before_validator_model_keeps_rewritten_argument_names(self) -> None:
        # A model-level before-validator may accept and rewrite top-level keys
        # the schema and field spellings can't advertise; the unknown-argument
        # pre-check must stand down for such models.
        received: list[int] = []

        class UnwrapArguments(BaseModel):
            x: int

            @model_validator(mode="before")
            @classmethod
            def _unwrap(cls, data: object) -> object:
                if isinstance(data, dict) and "payload" in data:
                    return {"x": data["payload"]}
                return data

        async def unwrap_like(x: int) -> str:
            received.append(x)
            return str(x)

        unwrap_tool = FunctionTool(
            name="unwrap_like",
            description="Unwrap a payload.",
            func=unwrap_like,
            input_model=UnwrapArguments,
        )
        layer, _wire = _stack(
            [
                _call_response(("c1", "unwrap_like", {"payload": 7})),
                _text_response("done"),
            ]
        )
        response = await layer.get_response([_user()], options={"tools": [unwrap_tool]})

        assert received == [7]
        results = _result_contents(response)
        assert results[0].exception is None

    @pytest.mark.asyncio
    async def test_tool_exception_returns_short_error_result(self) -> None:
        @tool(name="boom")
        async def boom(text: str) -> str:
            raise RuntimeError("kaput")

        layer, _wire = _stack([_call_response(("c1", "boom", {"text": "x"})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [boom]})
        results = _result_contents(response)
        assert str(results[0].result) == "Error: Function failed."
        assert "kaput" in str(results[0].exception)

    @pytest.mark.asyncio
    async def test_consecutive_error_cap_forces_tool_choice_none(self) -> None:
        @tool(name="boom")
        async def boom(text: str) -> str:
            raise RuntimeError("kaput")

        layer, wire = _stack(
            [_call_response(("c1", "boom", {"text": "x"})), _text_response("recovered")],
            max_consecutive_errors=1,
        )
        response = await layer.get_response([_user()], options={"tools": [boom]})
        assert len(wire.calls) == 2
        assert wire.calls[1]["options"].get("tool_choice") == "none"
        # The error result is still submitted before the final turn.
        assert "Error: Function failed." in str(_result_contents(response)[0].result)
        assert response.messages[-1].contents[0].text == "recovered"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("message", "model_reads"),
        [
            (
                "ENOENT: 'a.txt' not found. Did you mean 'b.txt'?",
                "Error: ENOENT: 'a.txt' not found. Did you mean 'b.txt'?",
            ),
            ("Error: field 'owner' is required", "Error: field 'owner' is required"),
            ("Error:field 'owner' is required", "Error: field 'owner' is required"),
            ("Errors: 2 fields are required", "Error: Errors: 2 fields are required"),
            ("  bad name \udcff\n", "Error: bad name \\udcff"),
            (" \n", "Error: Function failed."),
            ("Error: ", "Error: Function failed."),
        ],
        ids=[
            "message",
            "already-prefixed",
            "prefixed-without-space",
            "starts-with-another-word",
            "stripped-and-surrogate-safe",
            "blank-falls-back",
            "bare-prefix-falls-back",
        ],
    )
    async def test_model_visible_tool_error_hands_its_message_to_the_model(
        self, message: str, model_reads: str
    ) -> None:
        @tool(name="boom")
        async def boom(text: str) -> str:
            raise ModelVisibleToolError(message, inner_exception=RuntimeError("/home/me/.secret"))

        layer, wire = _stack([_call_response(("c1", "boom", {"text": "x"})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [boom]})

        (result,) = _result_contents(response)
        assert result.result == model_reads
        # The inner exception reaches telemetry, never the model.
        assert result.exception is not None and "/home/me/.secret" in result.exception
        (sent,) = [c for m in wire.calls[1]["messages"] for c in m.contents if c.type == "function_result"]
        assert sent.result == model_reads

    @pytest.mark.asyncio
    async def test_a_middleware_replacement_survives_a_raise_and_reaches_the_model(self) -> None:
        """A middleware that cleaned the error owns the text the model reads.

        The middleware commits its replacement under ``COMMITTED_ERROR_TEXT_KEY``
        and lets the exception through. The exception branch must not rebuild the
        result from the exception alone: the original text is exactly what the
        cleaning hook was there to remove. ``context.result`` alone is not enough
        — it already holds the tool's own output when the tool succeeded and a
        later middleware raised, so the mark is what says the text is a replacement.
        """

        @tool(name="boom")
        async def boom(text: str) -> str:
            raise ModelVisibleToolError("SECRET ORIGINAL")

        class _CleanTheError(FunctionMiddleware):
            async def process(
                self,
                context: FunctionInvocationContext,
                call_next: Callable[[], Awaitable[None]],
            ) -> None:
                try:
                    await call_next()
                except ModelVisibleToolError:
                    context.result = "REDACTED"
                    context.metadata[COMMITTED_ERROR_TEXT_KEY] = "REDACTED"
                    raise

        layer, wire = _stack([_call_response(("c1", "boom", {"text": "x"})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [boom]}, middleware=[_CleanTheError()])

        (result,) = _result_contents(response)
        assert result.result == "REDACTED"
        # Still a failed result: the failure record keeps naming the exception, so
        # the consecutive-error count and the providers' error flag are unchanged.
        assert result.exception is not None and "ModelVisibleToolError" in result.exception
        (sent,) = [c for m in wire.calls[1]["messages"] for c in m.contents if c.type == "function_result"]
        assert sent.result == "REDACTED"

    @pytest.mark.asyncio
    async def test_an_unreplaced_raise_still_shows_the_original_error(self) -> None:
        """With no middleware replacement, the exception's own text is what the model reads."""

        @tool(name="boom")
        async def boom(text: str) -> str:
            raise ModelVisibleToolError("owner is required")

        layer, _wire = _stack([_call_response(("c1", "boom", {"text": "x"})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [boom]})

        (result,) = _result_contents(response)
        assert result.result == "Error: owner is required"

    @pytest.mark.asyncio
    async def test_a_middleware_left_with_an_empty_result_does_not_blank_the_error(self) -> None:
        """An empty committed result must fall back to the exception text, never to nothing."""

        @tool(name="boom")
        async def boom(text: str) -> str:
            raise ModelVisibleToolError("owner is required")

        class _LeavesItEmpty(FunctionMiddleware):
            async def process(
                self,
                context: FunctionInvocationContext,
                call_next: Callable[[], Awaitable[None]],
            ) -> None:
                try:
                    await call_next()
                except ModelVisibleToolError:
                    context.result = ""
                    raise

        layer, _wire = _stack([_call_response(("c1", "boom", {"text": "x"})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [boom]}, middleware=[_LeavesItEmpty()])

        (result,) = _result_contents(response)
        assert result.result == "Error: owner is required"

    @pytest.mark.asyncio
    async def test_a_late_middleware_raise_does_not_hand_on_the_tools_success_text(self) -> None:
        """The tool returned, then a later middleware raised: the model must still read the failure.

        ``context.result`` already holds the tool's own successful output by then,
        so reading it as the committed error text would hand the model the success
        text while the failure record says the call failed.
        """

        @tool(name="works")
        async def works(text: str) -> str:
            return "TOOL SUCCEEDED"

        class _FailsAfterTheTool(FunctionMiddleware):
            async def process(
                self,
                context: FunctionInvocationContext,
                call_next: Callable[[], Awaitable[None]],
            ) -> None:
                await call_next()
                raise RuntimeError("the cleanup middleware broke")

        layer, _wire = _stack([_call_response(("c1", "works", {"text": "x"})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [works]}, middleware=[_FailsAfterTheTool()])

        (result,) = _result_contents(response)
        assert "TOOL SUCCEEDED" not in result.result, "a success result was handed on as the error text"
        assert result.result.startswith("Error: ")

    @pytest.mark.asyncio
    async def test_model_visible_tool_error_counts_toward_the_consecutive_error_cap(self) -> None:
        @tool(name="boom")
        async def boom(text: str) -> str:
            raise ModelVisibleToolError("owner is required")

        layer, wire = _stack(
            [_call_response(("c1", "boom", {"text": "x"})), _text_response("recovered")],
            max_consecutive_errors=1,
        )
        response = await layer.get_response([_user()], options={"tools": [boom]})

        assert wire.calls[1]["options"].get("tool_choice") == "none"
        assert _result_contents(response)[0].result == "Error: owner is required"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raised", "record"),
        [
            (RuntimeError("kaput"), "RuntimeError: kaput"),
            (RuntimeError(), "RuntimeError"),
            (KeyError("missing"), "KeyError: missing"),
            (
                ToolExecutionException("Failed to reconnect.", inner_exception=OSError("refused")),
                "ToolExecutionException: Failed to reconnect. (caused by OSError: refused)",
            ),
            (
                _wrapping("Failed to call tool 'remote'.", _raised_from(ValueError("bad frame"), OSError("closed"))),
                (
                    "ToolExecutionException: Failed to call tool 'remote'. "
                    "(caused by ValueError: bad frame (caused by OSError: closed))"
                ),
            ),
            (
                # MCP's reconnect: raised from the ping failure, wrapping the reconnect failure.
                _raised_from(
                    ToolExecutionException(
                        "Failed to establish MCP connection.",
                        inner_exception=PermissionError("reconnect authentication denied"),
                    ),
                    ConnectionError("ping failed"),
                ),
                (
                    "ToolExecutionException: Failed to establish MCP connection. "
                    "(caused by ConnectionError: ping failed; PermissionError: reconnect authentication denied)"
                ),
            ),
            (
                # str() of a group only counts its members: the record lists each one.
                ExceptionGroup(
                    "unhandled errors in a TaskGroup",
                    [ValueError("bad protocol bytes"), _raised_from(RuntimeError("closed"), OSError("reset"))],
                ),
                (
                    "ExceptionGroup: unhandled errors in a TaskGroup "
                    "[ValueError: bad protocol bytes; RuntimeError: closed (caused by OSError: reset)]"
                ),
            ),
            (
                ToolExecutionException(
                    "Failed to call tool 'remote'.",
                    inner_exception=ExceptionGroup("outer", [ExceptionGroup("inner", [KeyError("k")])]),
                ),
                (
                    "ToolExecutionException: Failed to call tool 'remote'. "
                    "(caused by ExceptionGroup: outer [ExceptionGroup: inner [KeyError: k]])"
                ),
            ),
        ],
        ids=[
            "message",
            "empty-message",
            "key-error-unquoted",
            "inner-exception",
            "cause-chain",
            "cause-and-inner-exception-differ",
            "exception-group-members",
            "nested-exception-groups",
        ],
    )
    async def test_failed_call_records_type_and_message_of_each_cause(self, raised: Exception, record: str) -> None:
        @tool(name="boom")
        async def boom(text: str) -> str:
            raise raised

        layer, _wire = _stack([_call_response(("c1", "boom", {"text": "x"})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [boom]})

        (result,) = _result_contents(response)
        assert result.exception == record
        assert result.result == "Error: Function failed."

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("raised", "record"),
        [
            (
                ExceptionGroup("many", [ValueError(str(number)) for number in range(40)]),
                "ExceptionGroup: many [{}; …]".format("; ".join(f"ValueError: {number}" for number in range(15))),
            ),
            (
                # Deeper than the recursion limit: the exception cap also bounds the walk.
                _cause_chain(2000),
                functools.reduce(
                    lambda cause, number: f"RuntimeError: {number} (caused by {cause})", range(1984, 2000), "…"
                ),
            ),
            (RuntimeError("x" * 10_000), f"RuntimeError: {'x' * 999}…"),
            (
                # Each message is clipped on its own, so a huge first one can't crowd out its causes.
                _raised_from(RuntimeError("a" * 10_000), _raised_from(ValueError("b" * 10_000), OSError("c" * 10_000))),
                f"RuntimeError: {'a' * 999}… (caused by ValueError: {'b' * 999}… (caused by OSError: {'c' * 999}…))",
            ),
            (
                ExceptionGroup("g" * 10_000, [KeyError("k")]),
                f"ExceptionGroup: {'g' * 999}… [KeyError: k]",
            ),
        ],
        ids=["many-group-members", "deep-cause-chain", "long-message", "each-message-clipped", "long-group-message"],
    )
    async def test_failure_record_is_bounded(self, raised: Exception, record: str) -> None:
        @tool(name="boom")
        async def boom(text: str) -> str:
            raise raised

        layer, _wire = _stack([_call_response(("c1", "boom", {"text": "x"})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [boom]})

        assert _result_contents(response)[0].exception == record

    @pytest.mark.asyncio
    async def test_failure_record_is_capped_as_a_whole(self) -> None:
        # Fifteen clipped messages still add up to more than the whole-record cap.
        group = ExceptionGroup("many long", [ValueError(f"{number} " + "v" * 2000) for number in range(20)])

        @tool(name="boom")
        async def boom(text: str) -> str:
            raise group

        layer, _wire = _stack([_call_response(("c1", "boom", {"text": "x"})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [boom]})

        record = _result_contents(response)[0].exception
        assert record is not None
        assert len(record) == 4000
        assert record.startswith(f"ExceptionGroup: many long [ValueError: 0 {'v' * 997}…; ValueError: 1 ")
        assert record.endswith("…")

    @pytest.mark.asyncio
    async def test_cause_whose_str_raises_still_yields_a_failed_result(self) -> None:
        class _Unprintable(Exception):
            def __str__(self) -> str:
                raise RuntimeError("no text")

        @tool(name="boom")
        async def boom(text: str) -> str:
            raise _wrapping("Failed to call tool 'remote'.", _Unprintable())

        layer, _wire = _stack([_call_response(("c1", "boom", {"text": "x"})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [boom]})

        (result,) = _result_contents(response)
        assert result.result == "Error: Function failed."
        assert result.exception == (
            "ToolExecutionException: Failed to call tool 'remote'. (caused by _Unprintable: <exception str() failed>)"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("call", "record"),
        [
            (("c1", "ghost", {}), 'KeyError: Function "ghost" not found.'),
            (("c1", "echo", {"wrong_arg": 1}), "TypeError: Unexpected argument(s) for 'echo'."),
        ],
        ids=["unknown-tool", "unexpected-argument"],
    )
    async def test_rejected_call_records_why(self, call: tuple[str, str, dict[str, int]], record: str) -> None:
        layer, _wire = _stack([_call_response(call), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [_make_tool()]})

        assert _result_contents(response)[0].exception == record

    @pytest.mark.asyncio
    async def test_ceiling_preserves_unknown_tool_failure_for_consecutive_error_cap(self) -> None:
        missing_name = "missing-" + "x" * 10_000
        layer, wire = _stack(
            [_call_response(("c1", missing_name, {})), _text_response("recovered")],
            max_consecutive_errors=1,
            tool_result_ceiling_tokens=100,
        )

        response = await layer.get_response([_user()], options={"tools": [_make_tool()]})

        result = _result_contents(response)[0]
        assert result.exception is not None
        assert result.additional_properties[TOOL_FAILED_METADATA_KEY] is True
        assert wire.calls[1]["options"].get("tool_choice") == "none"

    @pytest.mark.asyncio
    async def test_parallel_multi_failure_batch_counts_one_error_increment(self) -> None:
        """A batch failing several calls at once advances the cap by ONE, not per call."""
        layer, wire = _stack(
            [
                _call_response(("c1", "echo", {"wrong_arg": 1}), ("c2", "ghost", {})),
                _call_response(("c3", "echo", {"wrong_arg": 2})),
                _text_response("recovered"),
            ],
            max_consecutive_errors=2,
        )
        response = await layer.get_response([_user()], options={"tools": [_make_tool()]})
        assert len(wire.calls) == 3
        # Two same-batch failures = one increment: still under the cap.
        assert wire.calls[1]["options"].get("tool_choice") is None
        # The second failing batch reaches the cap: final submit disables tools.
        assert wire.calls[2]["options"].get("tool_choice") == "none"
        assert response.messages[-1].contents[0].text == "recovered"

    @pytest.mark.asyncio
    async def test_max_iterations_exhaustion_final_turn_without_tools(self) -> None:
        layer, wire = _stack(
            [_call_response(("c1", "echo", {"text": "a"})), _text_response("forced final")],
            max_iterations=1,
        )
        response = await layer.get_response([_user()], options={"tools": [_make_tool()]})
        assert len(wire.calls) == 2
        assert wire.calls[1]["options"].get("tool_choice") == "none"
        # Exhaustion final turn prepends the accumulated transcript.
        assert [m.role for m in response.messages] == ["assistant", "tool", "assistant"]

    @pytest.mark.asyncio
    async def test_required_tool_choice_resets_after_one_iteration(self) -> None:
        layer, wire = _stack([_call_response(("c1", "echo", {"text": "a"})), _text_response()])
        await layer.get_response([_user()], options={"tools": [_make_tool()], "tool_choice": "required"})
        assert wire.calls[0]["options"]["tool_choice"] == "required"
        assert wire.calls[1]["options"]["tool_choice"] is None

    @pytest.mark.asyncio
    async def test_usage_aggregates_across_iterations(self) -> None:
        layer, _wire = _stack(
            [
                _call_response(("c1", "echo", {"text": "a"}), usage_details={"input_token_count": 3}),
                _text_response("final", usage_details={"input_token_count": 4}),
            ]
        )
        response = await layer.get_response([_user()], options={"tools": [_make_tool()]})
        assert response.usage_details["input_token_count"] == 7
        assert response.latest_usage_details == {"input_token_count": 4}

    @pytest.mark.asyncio
    async def test_termination_returns_current_response_without_fcc_prepend(self) -> None:
        """Framework shape: middleware termination returns the current
        iteration's response (tool results appended), with no fcc prepend."""
        layer, wire = _stack(
            [
                _call_response(("c1", "echo", {"text": "a"})),
                _call_response(("c2", "echo", {"text": "b"})),
                _text_response("never"),
            ]
        )
        terminator = _TerminateFunction(result="interrupted")
        # First iteration runs normally; second iteration's middleware terminates.
        flip = {"first": True}

        class _SecondCallTerminates(FunctionMiddleware):
            async def process(
                self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
            ) -> None:
                if flip["first"]:
                    flip["first"] = False
                    await call_next()
                    return
                await terminator.process(context, call_next)

        response = await layer.get_response(
            [_user()], options={"tools": [_make_tool()]}, middleware=[_SecondCallTerminates()]
        )
        assert len(wire.calls) == 2, "loop must stop right after the terminated batch"
        # Only the second iteration's messages — no fcc prepend on terminate.
        assert [m.role for m in response.messages] == ["assistant", "tool"]
        assert str(_result_contents(response)[0].result) == "interrupted"

    @pytest.mark.asyncio
    async def test_termination_exc_result_content_is_owned_by_invocation(self) -> None:
        canned = Content.from_function_result(call_id="c1", result="canned")
        layer, _wire = _stack([_call_response(("c1", "echo", {"text": "a"}))])
        response = await layer.get_response(
            [_user()],
            options={"tools": [_make_tool()]},
            middleware=[_TerminateFunction(exc_result=canned)],
        )
        result = _result_contents(response)[0]
        assert result is not canned
        assert result.call_id == "c1"
        assert result.result == "canned"
        assert canned.call_id == "c1"

    @pytest.mark.asyncio
    async def test_parallel_tool_calls_execute_concurrently(self) -> None:
        barrier = asyncio.Barrier(2)

        @tool(name="alpha")
        async def alpha(text: str) -> str:
            await asyncio.wait_for(barrier.wait(), timeout=2)
            return "alpha-done"

        @tool(name="beta")
        async def beta(text: str) -> str:
            await asyncio.wait_for(barrier.wait(), timeout=2)
            return "beta-done"

        layer, _wire = _stack(
            [_call_response(("c1", "alpha", {"text": "x"}), ("c2", "beta", {"text": "y"})), _text_response()]
        )
        response = await layer.get_response([_user()], options={"tools": [alpha, beta]})
        results = _result_contents(response)
        assert [r.call_id for r in results] == ["c1", "c2"], "batch order is preserved"

    @pytest.mark.asyncio
    async def test_duplicate_and_already_resolved_calls_are_skipped(self) -> None:
        events: list[str] = []
        call_a = Content.from_function_call(call_id="c1", name="echo", arguments={"text": "a"})
        dup_a = Content.from_function_call(call_id="c1", name="echo", arguments={"text": "a"})
        resolved_call = Content.from_function_call(call_id="c2", name="echo", arguments={"text": "b"})
        resolved_result = Content.from_function_result(call_id="c2", result="cached")
        first = ChatResponse(
            messages=[
                Message(role="assistant", contents=[call_a, dup_a, resolved_call]),
                Message(role="tool", contents=[resolved_result]),
            ]
        )
        layer, _wire = _stack([first, _text_response()])
        await layer.get_response([_user()], options={"tools": [_make_tool(events)]})
        assert events == ["tool:echo:a"], "duplicate call_id and already-resolved calls must not execute"

    @pytest.mark.asyncio
    async def test_runtime_kwargs_merge_and_filtering(self) -> None:
        probe = _ProbeFunction()
        session = AgentSession()
        layer, _wire = _stack([_call_response(("c1", "echo", {"text": "a"})), _text_response()])
        await layer.get_response(
            [_user()],
            options={
                "tools": [_make_tool()],
                "additional_function_arguments": {"from_options": 2},
            },
            middleware=[probe],
            function_invocation_kwargs={"from_fik": 1, "middleware": "smuggled", "conversation_id": "nope"},
            client_kwargs={"session": session},
        )
        ctx = probe.contexts[0]
        assert ctx.kwargs["from_fik"] == 1
        assert ctx.kwargs["from_options"] == 2
        assert "middleware" not in ctx.kwargs
        assert "conversation_id" not in ctx.kwargs
        assert ctx.kwargs["session"] is session
        assert ctx.session is session
        assert ctx.metadata["call_id"] == "c1"

    @pytest.mark.asyncio
    async def test_additional_function_arguments_not_sent_to_wire(self) -> None:
        layer, wire = _stack([_text_response()])
        await layer.get_response(
            [_user()],
            options={"tools": [_make_tool()], "additional_function_arguments": {"x": 1}},
        )
        assert "additional_function_arguments" not in wire.calls[0]["options"]

    @pytest.mark.asyncio
    async def test_middleware_argument_rewrite_reaches_tool(self) -> None:
        class _Rewrite(FunctionMiddleware):
            async def process(
                self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
            ) -> None:
                assert isinstance(context.arguments, dict)
                context.arguments["text"] = "rewritten"
                await call_next()

        layer, _wire = _stack([_call_response(("c1", "echo", {"text": "original"})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [_make_tool()]}, middleware=[_Rewrite()])
        assert "echo:rewritten" in str(_result_contents(response)[0].result)

    @pytest.mark.asyncio
    async def test_progressive_tool_exposure_via_live_tools(self) -> None:
        events: list[str] = []
        late_tool = _make_tool(events, name="late")

        class _AddTool(FunctionMiddleware):
            async def process(
                self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
            ) -> None:
                assert context.tools is not None
                if late_tool not in context.tools:
                    context.tools.append(late_tool)
                await call_next()

        layer, wire = _stack(
            [
                _call_response(("c1", "echo", {"text": "a"})),
                _call_response(("c2", "late", {"text": "b"})),
                _text_response(),
            ]
        )
        await layer.get_response([_user()], options={"tools": [_make_tool(events)]}, middleware=[_AddTool()])
        assert "tool:late:b" in events
        # The model also sees the grown list (same run-local list in options).
        assert len(wire.calls[1]["options"]["tools"]) == 2

    @pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
    async def test_conversation_id_continuation(self, stream: bool) -> None:
        session = AgentSession()
        if stream:
            call_update = ChatResponseUpdate(
                contents=[Content.from_function_call(call_id="c1", name="echo", arguments={"text": "a"})],
                role="assistant",
                conversation_id="conv-1",
            )
            layer, wire = _stack([[call_update], [_text_update("final")]])
        else:
            layer, wire = _stack(
                [
                    _call_response(("c1", "echo", {"text": "a"}), conversation_id="conv-1"),
                    _text_response("final", conversation_id="conv-1"),
                ]
            )
        await _final_response(
            layer,
            [_user()],
            stream=stream,
            options={"tools": [_make_tool()]},
            client_kwargs={"session": session, "x-extra": "kept"},
        )
        # Only the new tool-result message is resent; the service holds the rest.
        second = wire.calls[1]["messages"]
        assert [m.role for m in second] == ["tool"]
        # Continuation id written back into the shared client_kwargs dict + options.
        inner_client_kwargs = wire.calls[1]["kwargs"]["client_kwargs"]
        assert inner_client_kwargs["conversation_id"] == "conv-1"
        assert inner_client_kwargs["x-extra"] == "kept"
        assert "session" not in inner_client_kwargs
        assert "loop_recorder" not in inner_client_kwargs
        assert wire.calls[1]["options"]["conversation_id"] == "conv-1"
        assert session.service_session_id == "conv-1"


# ---------------------------------------------------------------------------
# same_tool_calls_in_batch stamping
# ---------------------------------------------------------------------------


class TestSameToolCallsInBatch:
    """Per-name batch counts stamped onto every FunctionInvocationContext.

    Middleware with singleton semantics (whole-list replacement tools like
    ``todo_write``) reads the stamped count to reject same-batch duplicates
    deterministically instead of racing the gather.
    """

    @pytest.mark.asyncio
    async def test_two_same_name_calls_stamp_two_on_both(self) -> None:
        probe = _ProbeFunction()
        layer, _wire = _stack(
            [_call_response(("c1", "echo", {"text": "a"}), ("c2", "echo", {"text": "b"})), _text_response()]
        )
        await layer.get_response([_user()], options={"tools": [_make_tool()]}, middleware=[probe])
        assert [ctx.same_tool_calls_in_batch for ctx in probe.contexts] == [2, 2]

    @pytest.mark.asyncio
    async def test_mixed_names_stamp_one_each(self) -> None:
        probe = _ProbeFunction()
        layer, _wire = _stack(
            [_call_response(("c1", "echo", {"text": "a"}), ("c2", "other", {"text": "b"})), _text_response()]
        )
        await layer.get_response(
            [_user()],
            options={"tools": [_make_tool(), _make_tool(name="other")]},
            middleware=[probe],
        )
        assert {ctx.function.name: ctx.same_tool_calls_in_batch for ctx in probe.contexts} == {
            "echo": 1,
            "other": 1,
        }

    @pytest.mark.asyncio
    async def test_count_recomputed_per_batch(self) -> None:
        """A singleton batch after a duplicate batch stamps 1, not a stale 2."""
        probe = _ProbeFunction()
        layer, _wire = _stack(
            [
                _call_response(("c1", "echo", {"text": "a"}), ("c2", "echo", {"text": "b"})),
                _call_response(("c3", "echo", {"text": "c"})),
                _text_response(),
            ]
        )
        await layer.get_response([_user()], options={"tools": [_make_tool()]}, middleware=[probe])
        assert [ctx.same_tool_calls_in_batch for ctx in probe.contexts] == [2, 2, 1]

    def test_default_is_one_outside_the_loop(self) -> None:
        context = FunctionInvocationContext(function=_make_tool(), arguments={})
        assert context.same_tool_calls_in_batch == 1


# ---------------------------------------------------------------------------
# Streaming loop
# ---------------------------------------------------------------------------


class TestStreamingLoop:
    @pytest.mark.asyncio
    async def test_stream_usage_normalizes_each_call_before_aggregating_iterations(self) -> None:
        first_call = [
            _call_update("c1", "echo", {"text": "a"}),
            ChatResponseUpdate(
                contents=[
                    Content.from_usage(
                        usage_details={"input_token_count": 20, "output_token_count": 1, "total_token_count": 21}
                    )
                ],
                role="assistant",
            ),
            ChatResponseUpdate(
                contents=[
                    Content.from_usage(
                        usage_details={"input_token_count": 20, "output_token_count": 2, "total_token_count": 22}
                    )
                ],
                role="assistant",
            ),
        ]
        final_call = [
            _text_update("done"),
            ChatResponseUpdate(
                contents=[
                    Content.from_usage(
                        usage_details={"input_token_count": 22, "output_token_count": 1, "total_token_count": 23}
                    )
                ],
                role="assistant",
            ),
            ChatResponseUpdate(
                contents=[
                    Content.from_usage(
                        usage_details={"input_token_count": 22, "output_token_count": 3, "total_token_count": 25}
                    )
                ],
                role="assistant",
            ),
        ]
        layer, wire = _stack([first_call, final_call])

        stream = layer.get_response([_user()], stream=True, options={"tools": [_make_tool()]})
        _ = [update async for update in stream]
        response = await stream.get_final_response()

        assert len(wire.calls) == 2
        assert response.usage_details == {
            "input_token_count": 42,
            "output_token_count": 5,
            "total_token_count": 47,
        }
        assert response.latest_usage_details == {
            "input_token_count": 22,
            "output_token_count": 3,
            "total_token_count": 25,
        }

    @pytest.mark.asyncio
    async def test_stream_usage_identity_can_repeat_across_model_calls(self) -> None:
        usage = Content.from_usage(
            usage_details={"input_token_count": 3, "output_token_count": 1, "total_token_count": 4}
        )
        first_call = [
            _call_update("c1", "echo", {"text": "a"}),
            ChatResponseUpdate(contents=[usage], role="assistant"),
        ]
        final_call = [
            _text_update("done"),
            ChatResponseUpdate(contents=[usage], role="assistant"),
        ]
        layer, wire = _stack([first_call, final_call])

        stream = layer.get_response([_user()], stream=True, options={"tools": [_make_tool()]})
        updates = [update async for update in stream]
        response = await stream.get_final_response()
        streamed_usage = [content for update in updates for content in update.contents if content.type == "usage"]

        assert len(wire.calls) == 2
        assert len(streamed_usage) == 2
        assert all(content is usage for content in streamed_usage)
        assert response.usage_details == {
            "input_token_count": 6,
            "output_token_count": 2,
            "total_token_count": 8,
        }
        assert response.latest_usage_details == {
            "input_token_count": 3,
            "output_token_count": 1,
            "total_token_count": 4,
        }

    @pytest.mark.asyncio
    async def test_streaming_informational_function_call_does_not_continue_local_loop(self) -> None:
        events: list[str] = []
        hosted_call = Content.from_function_call(
            "hosted-1",
            "echo",
            arguments={"text": "must stay remote"},
            informational_only=True,
        )
        update = ChatResponseUpdate(contents=[hosted_call], role="assistant")
        layer, wire = _stack([[update]])

        stream = layer.get_response([_user()], stream=True, options={"tools": [_make_tool(events)]})
        observed = [item async for item in stream]
        result = await stream.get_final_response()

        assert observed == [update]
        assert events == []
        assert len(wire.calls) == 1
        assert result.messages[0].contents == [hosted_call]

    def test_stream_returns_synchronously_without_executing(self) -> None:
        layer, wire = _stack([[_text_update("hi")]])
        stream = layer.get_response([_user()], stream=True, options={"tools": [_make_tool()]})
        assert isinstance(stream, ResponseStream)
        assert wire.calls == [], "stream construction must execute nothing"

    @pytest.mark.asyncio
    async def test_stream_single_turn_passthrough(self) -> None:
        layer, wire = _stack([[_text_update("hello")]])
        stream = layer.get_response([_user()], stream=True, options={"tools": [_make_tool()]})
        updates = [u async for u in stream]
        assert len(updates) == 1
        final = await stream.get_final_response()
        assert final.messages[-1].contents[-1].text == "hello"
        assert len(wire.calls) == 1

    @pytest.mark.asyncio
    async def test_stream_tool_loop_yields_synthetic_tool_update(self) -> None:
        events: list[str] = []
        layer, wire = _stack([[_call_update("c1", "echo", {"text": "a"})], [_text_update("final")]])
        stream = layer.get_response([_user()], stream=True, options={"tools": [_make_tool(events)]})
        updates = [u async for u in stream]
        assert events == ["tool:echo:a"]
        assert len(wire.calls) == 2
        roles = [u.role for u in updates]
        assert roles == ["assistant", "tool", "assistant"]
        tool_update = updates[1]
        assert tool_update.contents[0].type == "function_result"
        # from_updates rebuilds the full transcript including the tool results.
        final = await stream.get_final_response()
        kinds = [item.type for msg in final.messages for item in msg.contents]
        assert "function_call" in kinds
        assert "function_result" in kinds

    @pytest.mark.asyncio
    async def test_stream_inner_result_hook_runs_before_tool_execution(self) -> None:
        events: list[str] = []

        def hook(response: ChatResponse) -> ChatResponse:
            events.append("hook")
            return response

        layer, _wire = _stack([[_call_update("c1", "echo", {"text": "a"})], [_text_update("final")]], result_hook=hook)
        stream = layer.get_response([_user()], stream=True, options={"tools": [_make_tool(events)]})
        async for _ in stream:
            pass
        await stream.get_final_response()
        assert events == ["hook", "tool:echo:a", "hook"], (
            "the inner stream's result hooks must fire (via get_final_response) before tool extraction, "
            "and the outer finalizer must not re-run them"
        )

    @pytest.mark.asyncio
    async def test_stream_termination_stops_after_synthetic_update(self) -> None:
        layer, wire = _stack([[_call_update("c1", "echo", {"text": "a"})], [_text_update("never")]])
        stream = layer.get_response(
            [_user()],
            stream=True,
            options={"tools": [_make_tool()]},
            middleware=[_TerminateFunction(result="stopped")],
        )
        updates = [u async for u in stream]
        assert len(wire.calls) == 1, "termination must stop the loop before another wire call"
        assert updates[-1].role == "tool"
        assert str(updates[-1].contents[0].result) == "stopped"

    @pytest.mark.asyncio
    async def test_stream_exhaustion_final_turn_without_tools(self) -> None:
        layer, wire = _stack(
            [[_call_update("c1", "echo", {"text": "a"})], [_text_update("forced")]],
            max_iterations=1,
        )
        stream = layer.get_response([_user()], stream=True, options={"tools": [_make_tool()]})
        updates = [u async for u in stream]
        assert len(wire.calls) == 2
        assert wire.calls[1]["options"].get("tool_choice") == "none"
        assert updates[-1].contents[-1].text == "forced"


class TestExecutionBoundaryUpcast:
    """The loop executes only chrys-owned FunctionTools."""

    @pytest.mark.asyncio
    async def test_direct_caller_single_callable_tool_is_normalized(self) -> None:
        def echo(text: str) -> str:
            return f"echo:{text}"

        layer, wire = _stack([_call_response(("c1", "echo", {"text": "hello"})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": echo})

        assert str(_result_contents(response)[0].result) == "echo:hello"
        assert isinstance(wire.calls[0]["options"]["tools"], list)
        assert all(type(tool_item) is FunctionTool for tool_item in wire.calls[0]["options"]["tools"])

    @pytest.mark.asyncio
    async def test_direct_caller_tuple_callable_tool_is_normalized_before_execution(self) -> None:
        def echo(text: str) -> str:
            return f"echo:{text}"

        layer, wire = _stack([_call_response(("c1", "echo", {"text": "hello"})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": (echo,)})

        result_content = _result_contents(response)[0]
        assert result_content.items and all(isinstance(item, Content) for item in result_content.items)
        assert str(result_content.result) == "echo:hello"
        assert isinstance(wire.calls[1]["options"]["tools"], list)
        assert all(type(tool_item) is FunctionTool for tool_item in wire.calls[1]["options"]["tools"])


# ---------------------------------------------------------------------------
# Drift contracts — loop defaults (the chrys.kernel.tools pins live in
# tests/kernel/test_tools.py::TestDriftPins)
# ---------------------------------------------------------------------------


class TestDriftContracts:
    def test_loop_defaults_stay_stable(self) -> None:
        assert DEFAULT_MAX_ITERATIONS == 40
        assert DEFAULT_MAX_CONSECUTIVE_ERRORS == 3


def test_extract_function_calls_dedups_within_the_whole_response() -> None:
    """Response-scoped on purpose, not exchange-scoped: a call answered
    anywhere in the same response is resolved, and a repeated call_id is
    collected once — landing already removed echoed calls before this
    filter sees the response."""
    answered = Message(role="assistant", contents=[Content.from_function_call("c1", "first", arguments={})])
    repeated_first = Message(role="assistant", contents=[Content.from_function_call("c2", "second", arguments={})])
    repeated_second = Message(role="assistant", contents=[Content.from_function_call("c2", "second", arguments={})])
    response = ChatResponse(
        messages=[
            answered,
            Message(role="tool", contents=[Content.from_function_result("c1", result="done")]),
            repeated_first,
            repeated_second,
        ]
    )

    calls = _extract_function_calls(response)

    assert calls == [repeated_first.contents[0]]
