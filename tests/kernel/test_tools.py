# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Acceptance tests for the chrys-owned FunctionTool surface.

Behavior matrix for ``chrys.kernel.tools``: ``invoke``
(result-parsing five states, validation, ctx injection, owned ``__call__``
limit semantics), the ``tool`` factory passthrough, and ``normalize_tools``
surface. The null-overlay policy (``chrys.kernel._null_overlay``) is
covered by ``test_null_overlay.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from threading import Event as ThreadEvent
from threading import get_ident
from typing import Any

import pytest
from pydantic import BaseModel, model_validator

from chrys.kernel import SKIP_PARSING, FunctionTool, ToolException, normalize_tools, tool
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.kernel.tools import (
    SyncToolCancelledAfterCompletion,
    _matches_json_schema_type,
    _validate_arguments_against_schema,
)
from chrys.kernel.types import ChatResponse, Content, Message
from chrys.service.mcp.owned import MCPStdioTool
from tests.support.waiting import wait_for

# ---------------------------------------------------------------------------
# Helpers — the two tool forms (risk 3: schema-supplied vs func-introspection)
# ---------------------------------------------------------------------------

_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "mode": {"type": "string", "enum": ["a", "b"]},
    },
    "required": ["text"],
    "additionalProperties": False,
}


class _ConsumingInvokeArguments(BaseModel):
    items: list[dict[str, int]]

    @model_validator(mode="before")
    @classmethod
    def _consume_nested(cls, data: Any) -> dict[str, Any]:
        item = data["items"].pop(0)
        return {"items": [{"x": item.pop("x")}]}


class _CycleNormalizingArguments(BaseModel):
    saw_cycle: bool

    @model_validator(mode="before")
    @classmethod
    def _normalize_cycle(cls, data: Any) -> dict[str, bool]:
        payload = data["payload"]
        return {"saw_cycle": payload["self"] is payload}


def _introspected_tool(**kwargs: Any) -> FunctionTool:
    @tool(name="echo", **kwargs)
    async def echo(text: str) -> str:
        """Echo the text."""
        return f"echo:{text}"

    return echo


def _schema_supplied_tool(func: Any = None, **kwargs: Any) -> FunctionTool:
    async def echo(**call_kwargs: Any) -> str:
        return f"echo:{call_kwargs.get('text')}"

    return FunctionTool(
        name="echo",
        description="Echo the text.",
        func=func or echo,
        input_model=_SCHEMA,
        **kwargs,
    )


def _foreign_content(text: str) -> object:
    class ForeignContent:
        type = "text"
        raw_representation = None

        def to_dict(self, exclude_none: bool = False) -> dict[str, Any]:
            del exclude_none
            return {"type": "text", "text": text, "additional_properties": {}}

    return ForeignContent()


# ---------------------------------------------------------------------------
# A. Surface pins — owned behavior
# ---------------------------------------------------------------------------


class TestToolInvocationContracts:
    @pytest.mark.asyncio
    async def test_invoke_returns_content_list(self) -> None:
        result = await _introspected_tool().invoke(arguments={"text": "hello"})

        assert isinstance(result, list)
        assert [type(item) for item in result] == [Content]
        assert result[0].text == "echo:hello"

    @pytest.mark.asyncio
    async def test_sync_tool_runs_off_the_event_loop(self) -> None:
        loop_thread = get_ident()
        worker_threads: list[int] = []

        @tool(name="threaded")
        def threaded() -> str:
            worker_threads.append(get_ident())
            return "done"

        result = await threaded.invoke(arguments={})

        assert [item.text for item in result] == ["done"]
        assert len(worker_threads) == 1
        assert worker_threads[0] != loop_thread

    def test_descriptor_binding_preserves_subclass_and_sidecar(self) -> None:
        class Tools:
            def execute(self, command: str) -> str:
                return command

        Tools.execute = FunctionTool(name="execute", description="d", func=Tools.execute)
        Tools.execute.chrys_kind = "shell"
        owner = Tools()
        bound = owner.execute
        assert bound is not Tools.__dict__["execute"]
        assert type(bound) is FunctionTool
        assert bound.chrys_kind == "shell"
        assert bound.bound_instance is owner
        assert Tools.execute.bound_instance is None
        assert Tools().execute.bound_instance is not owner

    def test_parse_result_is_chrys_owned_and_returns_chrys_content(self) -> None:
        assert FunctionTool.parse_result.__module__ == "chrys.kernel.tools"
        parsed = FunctionTool.parse_result("owned")
        assert type(parsed[0]) is Content

    def test_parse_result_converts_foreign_content_items(self) -> None:
        parsed = FunctionTool.parse_result([_foreign_content("mcp residual")])
        assert [type(item) for item in parsed] == [Content]
        assert parsed[0].text == "mcp residual"

    def test_parse_result_falls_back_for_invalid_content_like_objects(self) -> None:
        class ContentLike:
            type = "not_a_content_type"

            def to_dict(self, exclude_none: bool = False) -> dict[str, Any]:
                return {"type": self.type, "payload": {"kept": True}, "exclude_none": exclude_none}

        parsed = FunctionTool.parse_result(ContentLike())

        assert [type(item) for item in parsed] == [Content]
        assert json.loads(parsed[0].text or "{}") == {
            "type": "not_a_content_type",
            "payload": {"kept": True},
            "exclude_none": False,
        }

    def test_skip_parsing_sentinel_repr_stays_stable(self) -> None:
        assert repr(SKIP_PARSING) == "SKIP_PARSING"

    def test_serialization_type_identifier_unchanged(self) -> None:
        """Same class name → same snake_case type id → byte-identical dumps."""
        chrys_t = _introspected_tool()
        dumped = chrys_t.to_dict()
        assert dumped["type"] == "function_tool"

    def test_subclass_round_trips_through_inherited_type_identifier(self) -> None:
        # Serialized payloads always carry the instance ``type`` field
        # ("function_tool"), so a subclass's from_dict must validate against
        # that same inherited identifier — the snake_case class-name
        # fallback would reject every payload the subclass itself emitted.
        class _CustomTool(FunctionTool):
            pass

        payload = _CustomTool(name="mytool", description="d").to_dict()
        assert payload["type"] == "function_tool"
        restored = _CustomTool.from_dict(payload)
        assert isinstance(restored, _CustomTool)
        assert restored.name == "mytool"
        assert restored.description == "d"

    @pytest.mark.parametrize(
        "arguments,schema,expected",
        [
            ({}, {"required": ["a"], "properties": {"a": {"type": "string"}}}, "type_error"),
            (
                {"a": "x", "b": 1},
                {"properties": {"a": {"type": "string"}}, "additionalProperties": False},
                "type_error",
            ),
            ({"a": "x"}, {"properties": {"a": {"type": "string", "enum": ["y", "z"]}}}, "type_error"),
            ({"a": 1}, {"properties": {"a": {"type": "string"}}}, "type_error"),
            ({"a": True}, {"properties": {"a": {"type": "integer"}}}, "type_error"),
            ({"a": "x"}, {"properties": {"a": {"type": ["integer", "boolean"]}}}, "type_error"),
            ({"a": "x"}, {"properties": {"a": {"type": "string"}}}, "ok"),
            ({"a": [1]}, {"properties": {"a": {"type": "array"}}}, "ok"),
            ({"a": {"k": 1}}, {"properties": {"a": {"type": "object"}}}, "ok"),
            ({"a": None}, {"properties": {"a": {"type": "null"}}}, "ok"),
            ({"a": "x"}, {"properties": {"a": {"type": "made-up-type"}}}, "ok"),
            ({"a": 2}, {"properties": {"a": {"type": ["integer", "boolean"]}}}, "ok"),
        ],
    )
    def test_validation_contract(self, arguments: dict[str, Any], schema: dict[str, Any], expected: str) -> None:
        def outcome(fn: Any) -> tuple[str, Any]:
            try:
                return ("ok", fn(arguments=arguments, schema=schema, tool_name="t"))
            except TypeError:
                return ("type_error", None)

        assert outcome(_validate_arguments_against_schema)[0] == expected

    @pytest.mark.parametrize(
        "value,schema_type,expected",
        [
            ("s", "string", True),
            (1, "integer", True),
            (True, "integer", False),
            (1.5, "number", True),
            (True, "number", False),
            (True, "boolean", True),
            ([1], "array", True),
            ({"a": 1}, "object", True),
            (None, "null", True),
            ("anything", "unknown-type", True),
        ],
    )
    def test_type_matcher_contract(self, value: Any, schema_type: str, expected: bool) -> None:
        assert _matches_json_schema_type(value, schema_type) is expected


# ---------------------------------------------------------------------------
# B. invoke — result parsing five states + validation + ctx injection
# ---------------------------------------------------------------------------


class TestInvokeResultParsing:
    @pytest.mark.asyncio
    async def test_default_parse_to_content_list(self) -> None:
        result = await _introspected_tool().invoke(arguments={"text": "hi"})
        assert isinstance(result, list)
        assert [c.type for c in result] == ["text"]
        assert result[0].text == "echo:hi"

    @pytest.mark.asyncio
    async def test_skip_parsing_per_call_returns_raw(self) -> None:
        raw = await _introspected_tool().invoke(arguments={"text": "hi"}, skip_parsing=True)
        assert raw == "echo:hi"

    @pytest.mark.asyncio
    async def test_skip_parsing_via_configured_sentinel(self) -> None:
        t = _introspected_tool(result_parser=SKIP_PARSING)
        raw = await t.invoke(arguments={"text": "hi"})
        assert raw == "echo:hi"

    @pytest.mark.asyncio
    async def test_skip_parsing_sentinel_does_not_coerce_foreign_content(self) -> None:
        marker = _foreign_content("raw foreign content")

        async def impl() -> Any:
            return marker

        t = tool(impl, name="raw_content", description="d", result_parser=SKIP_PARSING)
        raw = await t.invoke(arguments={})

        assert raw is marker

    @pytest.mark.asyncio
    async def test_custom_parser_str_product_is_content_wrapped(self) -> None:
        t = _introspected_tool(result_parser=lambda r: f"parsed:{r}")
        result = await t.invoke(arguments={"text": "hi"})
        assert [c.type for c in result] == ["text"]
        assert result[0].text == "parsed:echo:hi"

    @pytest.mark.asyncio
    async def test_custom_parser_list_product_passes_through(self) -> None:
        marker = [Content.from_text("a"), Content.from_text("b")]
        t = _introspected_tool(result_parser=lambda r: marker)
        result = await t.invoke(arguments={"text": "hi"})
        assert result is marker

    @pytest.mark.asyncio
    async def test_custom_parser_foreign_content_product_is_converted(self) -> None:
        marker = _foreign_content("from custom parser")
        t = _introspected_tool(result_parser=lambda r: marker)

        result = await t.invoke(arguments={"text": "hi"})

        assert [type(item) for item in result] == [Content]
        assert result[0].text == "from custom parser"

    @pytest.mark.asyncio
    async def test_custom_parser_foreign_content_list_is_converted(self) -> None:
        marker = [_foreign_content("a"), _foreign_content("b")]
        t = _introspected_tool(result_parser=lambda r: marker)

        result = await t.invoke(arguments={"text": "hi"})

        assert result is not marker
        assert [type(item) for item in result] == [Content, Content]
        assert [item.text for item in result] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_parser_exception_falls_back_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        def boom(result: Any) -> str:
            raise RuntimeError("parser broke")

        t = _introspected_tool(result_parser=boom)
        with caplog.at_level(logging.WARNING, logger="chrys.kernel.tools"):
            result = await t.invoke(arguments={"text": "hi"})
        assert [c.type for c in result] == ["text"]
        assert result[0].text == "echo:hi"
        assert any("result parser failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_empty_parsed_list_returned_as_is(self) -> None:
        t = _introspected_tool(result_parser=lambda r: [])
        assert await t.invoke(arguments={"text": "hi"}) == []

    @pytest.mark.asyncio
    async def test_default_parser_wire_shape_stays_stable(self) -> None:
        async def impl(text: str) -> str:
            return f"echo:{text}"

        chrys_t = tool(impl, name="echo", description="d")
        chrys_out = await chrys_t.invoke(arguments={"text": "x"})
        assert [c.to_dict() for c in chrys_out] == [{"type": "text", "text": "echo:x", "additional_properties": {}}]


class TestInvokeValidation:
    async def test_declaration_only_refused(self) -> None:
        t = FunctionTool(name="decl", description="d", func=None)
        with pytest.raises(ToolException, match="declaration only"):
            await t.invoke(arguments={})

    async def test_derived_model_validates_mapping_arguments(self) -> None:
        with pytest.raises(TypeError, match="Invalid arguments for 'echo'"):
            await _introspected_tool().invoke(arguments={"text": 123})

    async def test_derived_model_rejects_foreign_base_model(self) -> None:
        class Other(BaseModel):
            text: str = "x"

        with pytest.raises(TypeError, match="Expected"):
            await _introspected_tool().invoke(arguments=Other())

    async def test_mapping_validation_cannot_mutate_the_caller_payload(self) -> None:
        received: list[list[dict[str, int]]] = []
        original = {"items": [{"x": 1}]}

        async def capture(items: list[dict[str, int]]) -> str:
            received.append(items)
            return "ok"

        consuming_tool = FunctionTool(
            name="isolated_mapping_validation",
            description="Before validators receive an isolated mapping.",
            func=capture,
            input_model=_ConsumingInvokeArguments,
        )

        await consuming_tool.invoke(arguments=original)
        assert received == [[{"x": 1}]]
        assert original == {"items": [{"x": 1}]}

    async def test_cyclic_mapping_validation_remains_supported(self) -> None:
        received: list[bool] = []
        cycle: dict[str, Any] = {}
        cycle["self"] = cycle

        async def capture(saw_cycle: bool) -> str:
            received.append(saw_cycle)
            return "ok"

        cyclic_tool = FunctionTool(
            name="cyclic_mapping_validation",
            description="Deep-copy validation preserves cyclic container topology.",
            func=capture,
            input_model=_CycleNormalizingArguments,
        )

        await cyclic_tool.invoke(arguments={"payload": cycle})
        assert received == [True]
        assert cycle["self"] is cycle

    async def test_non_mapping_arguments_rejected(self) -> None:
        with pytest.raises(TypeError, match="mapping-like"):
            await _introspected_tool().invoke(arguments=[("text", "hi")])  # type: ignore[arg-type]

    async def test_schema_supplied_validation_errors_and_happy_path(self) -> None:
        schema_tool = _schema_supplied_tool()
        with pytest.raises(TypeError, match="Missing required argument"):
            await schema_tool.invoke(arguments={})
        with pytest.raises(TypeError, match="is not in"):
            await schema_tool.invoke(arguments={"text": "hi", "mode": "z"})
        with pytest.raises(TypeError, match="Invalid type for 'text'"):
            await schema_tool.invoke(arguments={"text": 5})
        with pytest.raises(TypeError, match="Unexpected argument"):
            await schema_tool.invoke(arguments={"text": "hi", "extra": 1})
        result = await schema_tool.invoke(arguments={"text": "hi", "mode": "a"})
        assert result[0].text == "echo:hi"

    async def test_direct_kwargs_context_and_unexpected_kwargs(self) -> None:
        direct = await _introspected_tool().invoke(text="hi")
        assert direct[0].text == "echo:hi"

        with pytest.raises(TypeError, match="Unexpected keyword argument"):
            await _introspected_tool().invoke(text="hi", runtime_thing=1)

        t = _introspected_tool()
        ctx = FunctionInvocationContext(function=t, arguments={"text": "hi"})
        contextual = await t.invoke(context=ctx)
        assert contextual[0].text == "echo:hi"
        assert ctx.arguments == {"text": "hi"}


class TestInvokeContextInjection:
    """Uses the schema-supplied + unannotated-``ctx`` form — the production
    (MCP) shape. Step 3 owns context detection, so annotating the chrys context
    class is recognized directly."""

    def _ctx_tool(self, seen: list[Any]) -> FunctionTool:
        async def impl(ctx=None, **call_kwargs: Any) -> str:
            seen.append(ctx)
            return str(call_kwargs.get("text"))

        return FunctionTool(name="ctx_tool", description="d", func=impl, input_model=_SCHEMA)

    @pytest.mark.asyncio
    async def test_ctx_parameter_gets_internally_built_chrys_context(self) -> None:
        seen: list[Any] = []
        t = self._ctx_tool(seen)
        assert t._context_parameter_name == "ctx"
        await t.invoke(arguments={"text": "hi"})
        assert len(seen) == 1
        assert isinstance(seen[0], FunctionInvocationContext)
        assert seen[0].arguments == {"text": "hi"}

    @pytest.mark.asyncio
    async def test_passed_context_is_synced_and_injected(self) -> None:
        seen: list[Any] = []
        t = self._ctx_tool(seen)
        ctx = FunctionInvocationContext(function=t, arguments={}, kwargs={"runtime": 1})
        await t.invoke(arguments={"text": "hi"}, context=ctx)
        assert seen[0] is ctx
        assert ctx.arguments == {"text": "hi"}
        assert ctx.kwargs == {"runtime": 1}

    def test_chrys_context_annotation_is_detected(self) -> None:
        @tool(name="annotated")
        async def annotated(text: str, ctx: FunctionInvocationContext) -> str:
            return text

        assert annotated._context_parameter_name == "ctx"
        assert "ctx" not in annotated.parameters()["properties"]


class TestOwnedCallSemantics:
    @pytest.mark.asyncio
    async def test_max_invocations_enforced_via_owned_call(self) -> None:
        t = _introspected_tool(max_invocations=1)
        await t.invoke(arguments={"text": "a"})
        assert t.invocation_count == 1
        with pytest.raises(ToolException, match="maximum invocation limit"):
            await t.invoke(arguments={"text": "b"})

    @pytest.mark.asyncio
    async def test_max_invocation_exceptions_enforced(self) -> None:
        # Sync tool on purpose: ``__call__`` only counts exceptions raised
        # synchronously inside it; an async tool's exception surfaces later,
        # at the ``_invoke_function`` await.
        @tool(name="boom", max_invocation_exceptions=1)
        def boom(text: str) -> str:
            raise RuntimeError("nope")

        with pytest.raises(RuntimeError):
            await boom.invoke(arguments={"text": "a"})
        assert boom.invocation_exception_count == 1
        with pytest.raises(ToolException, match="maximum exception limit"):
            await boom.invoke(arguments={"text": "b"})


# ---------------------------------------------------------------------------
# C. tool factory — full kwargs passthrough
# ---------------------------------------------------------------------------


class TestToolFactory:
    def test_bare_and_parenthesized_decorator_forms(self) -> None:
        @tool
        def bare(text: str) -> str:
            """Bare."""
            return text

        @tool()
        def parens(text: str) -> str:
            """Parens."""
            return text

        assert type(bare) is FunctionTool
        assert type(parens) is FunctionTool
        assert bare.name == "bare"
        assert bare.description == "Bare."

    def test_full_kwargs_passthrough(self) -> None:
        def parser(result: Any) -> str:
            return str(result)

        built = tool(
            lambda text: text,
            name="custom",
            description="desc",
            schema=_SCHEMA,
            kind="shell",
            max_invocations=3,
            max_invocation_exceptions=2,
            additional_properties={"a": 1},
            result_parser=parser,
        )
        assert type(built) is FunctionTool
        assert built.name == "custom"
        assert built.description == "desc"
        assert built.kind == "shell"
        assert built.max_invocations == 3
        assert built.max_invocation_exceptions == 2
        assert built.additional_properties == {"a": 1}
        assert built.result_parser is parser
        assert built._schema_supplied is True
        assert built.parameters()["properties"] == _SCHEMA["properties"]

    def test_name_and_description_fall_back_to_function(self) -> None:
        @tool
        def named(text: str) -> str:
            """Doc here."""
            return text

        assert named.name == "named"
        assert named.description == "Doc here."


# ---------------------------------------------------------------------------
# D. normalize_tools — full surface
# ---------------------------------------------------------------------------


class TestNormalizeToolsThinLayer:
    def test_callable_wrapped_to_chrys_class(self) -> None:
        def impl(text: str) -> str:
            return text

        out = normalize_tools([impl])
        assert len(out) == 1
        assert type(out[0]) is FunctionTool

    def test_single_noncallable_leaf_is_preserved(self) -> None:
        spec = {"type": "web_search"}
        assert normalize_tools(spec) == [spec]

    def test_dict_and_mcp_tool_pass_through_untouched(self) -> None:
        spec = {"type": "web_search"}
        mcp = MCPStdioTool(name="m", command="true")
        out = normalize_tools([spec, mcp])
        assert out[0] is spec
        assert out[1] is mcp
        assert type(mcp) is MCPStdioTool

    def test_tools_collection_object_flattened(self) -> None:
        # NOT a dict subclass: dicts are preserved before the ``.tools``
        # collection check.
        class Box:
            @property
            def tools(self) -> list[Any]:
                return [lambda text: text]

        out = normalize_tools([Box()])
        assert len(out) == 1
        assert type(out[0]) is FunctionTool

    def test_toolbox_iterable_flattened_with_nested_callable_upcast(self) -> None:
        class Toolbox:
            def __iter__(self) -> Any:
                return iter([lambda text: text, {"type": "web_search"}])

        out = normalize_tools([Toolbox()])
        assert len(out) == 2
        assert type(out[0]) is FunctionTool
        assert out[1] == {"type": "web_search"}

    def test_none_and_empty_return_empty_list(self) -> None:
        assert normalize_tools(None) == []
        assert normalize_tools([]) == []

    def test_chrys_instances_pass_through_unchanged(self) -> None:
        t = _introspected_tool()
        out = normalize_tools([t])
        assert out[0] is t
        assert type(t) is FunctionTool

    def test_non_framework_function_tool_like_callable_is_wrapped(self) -> None:
        class ForeignTool:
            name = "foreign"
            func = None

            def __call__(self, text: str) -> str:
                return f"foreign:{text}"

            def parameters(self) -> dict[str, Any]:
                return {}

            def to_json_schema_spec(self) -> dict[str, Any]:
                return {}

        foreign = ForeignTool()
        out = normalize_tools([foreign])
        assert len(out) == 1
        assert type(out[0]) is FunctionTool
        assert out[0].func is foreign
        assert out[0](text="x") == "foreign:x"


# ---------------------------------------------------------------------------
# E. Production-path guarantee — everything reaching the loop through the
# agent is the chrys subclass
# ---------------------------------------------------------------------------


class TestAgentPathUpcastGuarantee:
    @pytest.mark.asyncio
    async def test_agent_normalization_delivers_chrys_subclass_to_the_wire(self) -> None:
        from chrys.kernel import Agent

        captured: dict[str, Any] = {}

        class _Wire:
            def get_response(self, messages: Any, *, stream: bool = False, options: Any = None, **kwargs: Any) -> Any:
                captured["tools"] = list((options or {}).get("tools") or [])

                async def _resolve() -> ChatResponse:
                    return ChatResponse(messages=[Message(role="assistant", contents=["done"])])

                return _resolve()

        ctor_tool = tool(lambda text: text, name="ctor_tool", description="d")

        def runtime_impl(text: str) -> str:
            return text

        agent = Agent(client=_Wire(), tools=[ctor_tool])

        await agent.run("hi", tools=[runtime_impl])
        delivered = captured["tools"]
        assert {t.name for t in delivered} == {"ctor_tool", "runtime_impl"}
        assert all(type(t) is FunctionTool for t in delivered)


class TestSyncWorkerCancellationDrain:
    """Cancellation racing a threaded sync tool that already returned."""

    async def test_cancel_after_worker_completion_carries_parsed_result(self) -> None:
        """Cancellation in the worker-done/await-suspended window drains the value."""
        release = ThreadEvent()

        @tool(name="threaded")
        def threaded() -> str:
            release.wait(timeout=5)
            return "done"

        coro = threaded.invoke(arguments={}, tool_call_id="c1")
        pending = coro.send(None)
        assert isinstance(pending, asyncio.Future)
        assert not pending.done()
        release.set()
        # The worker records its outcome strictly before the executor future
        # resolves, so a resolved future pins the exact race window: value
        # produced, ``await`` in the invoke coroutine not yet resumed.
        # Poll instead of awaiting: the manual ``coro.send`` drive already
        # consumed the future's ``__await__`` yield, so a second await would
        # trip its resumed-while-pending guard.
        await wait_for(pending.done, description="worker future resolved")
        with pytest.raises(SyncToolCancelledAfterCompletion) as exc_info:
            coro.throw(asyncio.CancelledError())
        completed = exc_info.value.completed_result
        assert [content.text for content in completed] == ["done"]

    async def test_cancelled_future_with_completed_worker_still_drains(self) -> None:
        """Race shape (b): the future loses to cancellation but the worker still finished.

        Pins the drain signal to the completion record rather than future
        state — a future-state implementation would see only the cancelled
        future here and drop the completed value.
        """
        release = ThreadEvent()
        body_done = ThreadEvent()

        @tool(name="threaded")
        def threaded() -> str:
            release.wait(timeout=30)
            body_done.set()
            return "done"

        coro = threaded.invoke(arguments={}, tool_call_id="c1")
        pending = coro.send(None)
        assert not pending.done()
        pending.cancel()
        release.set()
        assert await asyncio.to_thread(body_done.wait, 2)
        # The worker holds the GIL from ``body_done.set()`` through the
        # completion record, so the record precedes this coroutine's
        # resumption; the sleep is belt-and-braces on top of that ordering.
        await asyncio.sleep(0.05)
        with pytest.raises(SyncToolCancelledAfterCompletion) as exc_info:
            coro.throw(asyncio.CancelledError())
        completed = exc_info.value.completed_result
        assert [content.text for content in completed] == ["done"]

    async def test_cancel_while_worker_still_running_stays_plain_cancellation(self) -> None:
        started = ThreadEvent()
        release = ThreadEvent()

        @tool(name="blocked")
        def blocked() -> str:
            started.set()
            release.wait(timeout=5)
            return "late"

        coro = blocked.invoke(arguments={}, tool_call_id="c1")
        pending = coro.send(None)
        assert await asyncio.to_thread(started.wait, 2)
        assert not pending.done()
        with pytest.raises(asyncio.CancelledError) as exc_info:
            coro.throw(asyncio.CancelledError())
        assert not isinstance(exc_info.value, SyncToolCancelledAfterCompletion)
        release.set()
        # Poll instead of awaiting: the manual ``coro.send`` drive already
        # consumed the future's ``__await__`` yield, so a second await would
        # trip its resumed-while-pending guard.
        await wait_for(pending.done, description="worker future resolved")
        assert pending.result() == "late"
