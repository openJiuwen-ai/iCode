# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Loop dispatch argument validation: bool/int guards, explicit nulls, middleware rewrites, length-cutoff truncation."""

from __future__ import annotations

import datetime
import decimal
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypedDict

import pytest
from pydantic import AliasGenerator, BaseModel, ConfigDict, Field, computed_field, field_serializer, model_validator

from chrys.foundation.tool_result_metadata import (
    TOOL_ERROR_KIND_METADATA_KEY,
    TOOL_FAILED_METADATA_KEY,
)
from chrys.kernel import FunctionTool, tool
from chrys.kernel._tool_execution import (
    _middleware_arguments_equal,
)
from chrys.kernel.middleware import (
    FunctionInvocationContext,
    FunctionMiddleware,
)
from chrys.kernel.types import (
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
)
from tests.kernel._fakes import (
    _AliasedNullable,
    _assert_actionable_argument_error,
    _call_response,
    _DivergentDefaults,
    _final_response,
    _make_tool,
    _NullableValue,
    _result_contents,
    _stack,
    _text_response,
    _text_update,
    _user,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class _SerializedNullable(BaseModel):
    """Field serializer owns one field while a safe root null remains restorable."""

    safe: str | None
    value: str

    @field_serializer("value")
    def _serialize_value(self, value: str) -> dict[str, Any]:
        return {"serialized": value}


class _ComputedGhost(BaseModel):
    """Computed field appears in every dump but is never caller-supplied."""

    count: int = 1

    @computed_field
    @property
    def ghost(self) -> int | None:
        return None


class _OptionalChild(BaseModel):
    value: str | None = None


class _ReorderingItems(BaseModel):
    """Same-shape field serializer: equal length no longer proves alignment."""

    items: list[_OptionalChild]

    @field_serializer("items")
    def _reverse(self, items: list[_OptionalChild]) -> list[_OptionalChild]:
        return list(reversed(items))


class _TupleItems(BaseModel):
    items: tuple[_NullableValue, ...]


class _Round15SplitAlias(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)

    value: str | None = Field(validation_alias="inputKey", serialization_alias="callableKey")


class _Round15ValidationAliasOnly(BaseModel):
    value: str | None = Field(validation_alias="inputKey")


class _Round15GeneratedValidationAlias(BaseModel):
    model_config = ConfigDict(
        alias_generator=AliasGenerator(validation_alias=str.upper),
        serialize_by_alias=True,
        validate_by_name=True,
    )

    value: str | None = Field(serialization_alias="callableKey")


class _Round15PlainArguments(BaseModel):
    value: str | None


class _Round16RewriteArguments(BaseModel):
    value: Any


class _Round17ConsumingArguments(BaseModel):
    x: int

    @model_validator(mode="before")
    @classmethod
    def _consume(cls, data: Any) -> dict[str, Any]:
        return {"x": data.pop("x")}


class _Round17NestedConsumingArguments(BaseModel):
    items: list[dict[str, int]]

    @model_validator(mode="before")
    @classmethod
    def _consume_nested(cls, data: Any) -> dict[str, Any]:
        item = data["items"].pop(0)
        return {"items": [{"x": item.pop("x")}]}


class _Round17OrderFastPathArguments(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)

    first: int = Field(validation_alias="inputFirst", serialization_alias="callableFirst")
    second: int


class _MemberNullable(TypedDict):
    value: str | None


class _MemberNullableHolder(BaseModel):
    inner: _MemberNullable


# ---------------------------------------------------------------------------
# Argument validation, explicit-null restoration, middleware rewrites
# ---------------------------------------------------------------------------


class TestNonStreamingLoop:
    """Argument handling between landing and ``FunctionTool.invoke`` on the loop's dispatch path."""

    @pytest.mark.parametrize("raw_seconds", [True, False])
    @pytest.mark.asyncio
    async def test_auto_model_rejects_bool_for_int(self, raw_seconds: bool) -> None:
        received: list[int] = []

        @tool(name="sleep_like")
        async def sleep_like(seconds: Annotated[int, "Seconds to wait."]) -> str:
            received.append(seconds)
            return f"slept:{seconds}"

        layer, _wire = _stack(
            [_call_response(("c1", "sleep_like", {"seconds": raw_seconds})), _text_response("recovered")]
        )
        response = await layer.get_response([_user()], options={"tools": [sleep_like]})

        assert received == []
        result = _result_contents(response)[0]
        _assert_actionable_argument_error(result, "sleep_like", "seconds: expected integer")

    @pytest.mark.parametrize("raw_mode", [True, False])
    @pytest.mark.asyncio
    async def test_auto_model_rejects_bool_for_int_literal(self, raw_mode: bool) -> None:
        received: list[int] = []

        @tool(name="mode_like")
        async def mode_like(mode: Annotated[Literal[0, 1], "Mode selector."]) -> str:
            received.append(mode)
            return f"mode:{mode}"

        layer, _wire = _stack([_call_response(("c1", "mode_like", {"mode": raw_mode})), _text_response("recovered")])
        response = await layer.get_response([_user()], options={"tools": [mode_like]})

        assert received == []
        result = _result_contents(response)[0]
        _assert_actionable_argument_error(result, "mode_like", "mode: expected 0 | 1")

    @pytest.mark.parametrize(
        ("literal_kind", "raw_mode"),
        [
            ("zero_or_true", False),
            ("zero_or_true", 1),
            ("one_or_false", True),
            ("one_or_false", 0),
            ("pure_bool", 1),
        ],
    )
    @pytest.mark.asyncio
    async def test_auto_model_rejects_cross_type_literal_match(self, literal_kind: str, raw_mode: bool | int) -> None:
        received: list[object] = []

        @tool(name="zero_or_true")
        async def zero_or_true(mode: Annotated[Literal[0, True], "Mixed mode."]) -> str:
            received.append(mode)
            return f"mode:{mode}"

        @tool(name="one_or_false")
        async def one_or_false(mode: Annotated[Literal[1, False], "Mixed mode."]) -> str:
            received.append(mode)
            return f"mode:{mode}"

        @tool(name="pure_bool")
        async def pure_bool(mode: Annotated[Literal[True], "Must be literal true."]) -> str:
            received.append(mode)
            return f"mode:{mode}"

        tools = {"zero_or_true": zero_or_true, "one_or_false": one_or_false, "pure_bool": pure_bool}
        layer, _wire = _stack([_call_response(("c1", literal_kind, {"mode": raw_mode})), _text_response("recovered")])
        response = await layer.get_response([_user()], options={"tools": [tools[literal_kind]]})

        assert received == []
        result = _result_contents(response)[0]
        _assert_actionable_argument_error(result, literal_kind, "mode: expected")

    @pytest.mark.asyncio
    async def test_auto_model_keeps_exact_mixed_literal_matches(self) -> None:
        received: list[object] = []

        @tool(name="mixed_like")
        async def mixed_like(mode: Annotated[Literal[0, True], "Mixed mode."]) -> str:
            received.append(mode)
            return f"mode:{mode}"

        layer, _wire = _stack(
            [
                _call_response(("c1", "mixed_like", {"mode": 0}), ("c2", "mixed_like", {"mode": True})),
                _text_response("done"),
            ]
        )
        response = await layer.get_response([_user()], options={"tools": [mixed_like]})

        assert received == [0, True]
        assert [type(value) for value in received] == [int, bool]
        results = _result_contents(response)
        assert results[0].exception is None
        assert results[1].exception is None

    @pytest.mark.asyncio
    async def test_auto_model_keeps_int_literal_and_bool_literal_semantics(self) -> None:
        received_mode: list[int] = []
        received_flag: list[bool] = []

        @tool(name="mode_like")
        async def mode_like(mode: Annotated[Literal[0, 1], "Mode selector."]) -> str:
            received_mode.append(mode)
            return f"mode:{mode}"

        @tool(name="flag_like")
        async def flag_like(flag: Annotated[Literal[True], "Must be literal true."]) -> str:
            received_flag.append(flag)
            return "flag:ok"

        layer, _wire = _stack(
            [
                _call_response(
                    ("c1", "mode_like", {"mode": 1}),
                    ("c2", "flag_like", {"flag": True}),
                ),
                _text_response("done"),
            ]
        )
        response = await layer.get_response([_user()], options={"tools": [mode_like, flag_like]})

        assert received_mode == [1]
        assert received_flag == [True]
        results = _result_contents(response)
        assert results[0].exception is None
        assert results[1].exception is None

    @pytest.mark.parametrize(("raw_seconds", "expected"), [("3", 3), (3.0, 3)])
    @pytest.mark.asyncio
    async def test_auto_model_keeps_non_bool_int_coercion(self, raw_seconds: str | float, expected: int) -> None:
        received: list[int] = []

        @tool(name="sleep_like")
        async def sleep_like(seconds: Annotated[int, "Seconds to wait."]) -> str:
            received.append(seconds)
            return f"slept:{seconds}"

        layer, _wire = _stack([_call_response(("c1", "sleep_like", {"seconds": raw_seconds})), _text_response("done")])
        response = await layer.get_response([_user()], options={"tools": [sleep_like]})

        assert received == [expected]
        assert _result_contents(response)[0].exception is None

    @pytest.mark.asyncio
    async def test_auto_model_rejects_bool_nested_in_int_list(self) -> None:
        received: list[list[int]] = []

        @tool(name="read_like")
        async def read_like(line_range: Annotated[list[int], "Inclusive line range."]) -> str:
            received.append(line_range)
            return f"read:{line_range}"

        layer, _wire = _stack(
            [_call_response(("c1", "read_like", {"line_range": [True, True]})), _text_response("recovered")]
        )
        response = await layer.get_response([_user()], options={"tools": [read_like]})

        assert received == []
        result = _result_contents(response)[0]
        _assert_actionable_argument_error(result, "read_like", "line_range[0]: expected integer")

    @pytest.mark.asyncio
    async def test_auto_model_guards_optional_int_without_changing_bool_params(self) -> None:
        received_optional: list[int | None] = []
        received_bool: list[bool] = []

        @tool(name="optional_int")
        async def optional_int(value: Annotated[int | None, "Optional integer."]) -> str:
            received_optional.append(value)
            return f"optional:{value}"

        @tool(name="boolean")
        async def boolean(value: Annotated[bool, "Boolean flag."]) -> str:
            received_bool.append(value)
            return f"boolean:{value}"

        layer, _wire = _stack(
            [
                _call_response(
                    ("c1", "optional_int", {"value": True}),
                    ("c2", "boolean", {"value": True}),
                ),
                _text_response("recovered"),
            ]
        )
        response = await layer.get_response([_user()], options={"tools": [optional_int, boolean]})

        assert received_optional == []
        assert received_bool == [True]
        results = _result_contents(response)
        _assert_actionable_argument_error(results[0], "optional_int", "value: expected integer | null")
        assert results[1].exception is None

    @pytest.mark.asyncio
    async def test_schema_supplied_integer_takes_a_whole_float_from_the_model(self) -> None:
        received: list[Any] = []

        async def count(**call_kwargs: Any) -> str:
            received.append(call_kwargs["count"])
            return "counted"

        counter = FunctionTool(
            name="count",
            description="Count.",
            func=count,
            input_model={
                "type": "object",
                "properties": {"count": {"type": "integer"}},
                "required": ["count"],
                "additionalProperties": False,
            },
        )
        seen_by_middleware: list[type] = []

        class _Record(FunctionMiddleware):
            async def process(
                self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
            ) -> None:
                # Read on entry: ``invoke`` later rebinds the context's arguments.
                seen_by_middleware.append(type(context.arguments["count"]))
                await call_next()

        layer, _wire = _stack([_call_response(("c1", "count", {"count": 3.0})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [counter]}, middleware=[_Record()])

        assert received == [3]
        assert type(received[0]) is int
        assert _result_contents(response)[0].exception is None
        # Middleware sees the value the callable gets.
        assert seen_by_middleware == [int]

    @pytest.mark.asyncio
    async def test_schema_supplied_argument_error_does_not_blame_a_whole_float_integer(self) -> None:
        async def count(**call_kwargs: Any) -> str:
            return "counted"

        counter = FunctionTool(
            name="count",
            description="Count.",
            func=count,
            input_model={
                "type": "object",
                "properties": {"count": {"type": "integer"}, "label": {"type": "string"}},
                "required": ["count", "label"],
                "additionalProperties": False,
            },
        )
        layer, _wire = _stack([_call_response(("c1", "count", {"count": 3.0, "label": 5})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [counter]})

        result_text = _assert_actionable_argument_error(_result_contents(response)[0], "count", "label")
        assert "count: expected" not in result_text

    @pytest.mark.asyncio
    async def test_explicit_null_for_required_nullable_argument_reaches_tool(self) -> None:
        received: list[str | None] = []

        @tool(name="nullable")
        async def nullable(value: str | None) -> str:
            received.append(value)
            return "ok"

        layer, _wire = _stack([_call_response(("c1", "nullable", {"value": None})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [nullable]})

        assert received == [None]
        assert _result_contents(response)[0].exception is None

    @pytest.mark.asyncio
    async def test_nested_explicit_null_reaches_tool(self) -> None:
        received: list[Any] = []

        @tool(name="nested_nullable")
        async def nested_nullable(inner: _NullableValue) -> str:
            received.append(inner)
            return "ok"

        layer, _wire = _stack([_call_response(("c1", "nested_nullable", {"inner": {"value": None}})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [nested_nullable]})

        assert received == [{"value": None}]
        assert _result_contents(response)[0].exception is None

    @pytest.mark.asyncio
    async def test_list_of_models_preserves_explicit_null_by_index(self) -> None:
        received: list[Any] = []

        @tool(name="listed_nullable")
        async def listed_nullable(items: list[_NullableValue]) -> str:
            received.append(items)
            return "ok"

        layer, _wire = _stack(
            [
                _call_response(("c1", "listed_nullable", {"items": [{"value": None}, {"value": "x"}]})),
                _text_response(),
            ]
        )
        response = await layer.get_response([_user()], options={"tools": [listed_nullable]})

        assert received == [[{"value": None}, {"value": "x"}]]
        assert _result_contents(response)[0].exception is None

    @pytest.mark.asyncio
    async def test_omitted_optional_argument_keeps_callable_default(self) -> None:
        received: list[str] = []

        @tool(name="optional_default")
        async def optional_default(value: str = "fallback") -> str:
            received.append(value)
            return "ok"

        layer, _wire = _stack([_call_response(("c1", "optional_default", {})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [optional_default]})

        assert received == ["fallback"]
        assert _result_contents(response)[0].exception is None

    @pytest.mark.asyncio
    async def test_custom_input_model_keeps_model_default_when_argument_is_omitted(self) -> None:
        received: list[int] = []

        async def custom_default(count: int = 3) -> str:
            received.append(count)
            return "ok"

        custom_tool = FunctionTool(
            name="custom_default",
            description="Use a custom input model.",
            func=custom_default,
            input_model=_DivergentDefaults,
        )
        layer, _wire = _stack([_call_response(("c1", "custom_default", {})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [custom_tool]})

        assert received == [7]
        assert _result_contents(response)[0].exception is None

    @pytest.mark.asyncio
    async def test_field_serializer_does_not_corrupt_explicit_null(self) -> None:
        received: list[dict[str, Any]] = []

        async def serialized(**kwargs: Any) -> str:
            received.append(kwargs)
            return "ok"

        serialized_tool = FunctionTool(
            name="serialized",
            description="Custom input model with a field serializer.",
            func=serialized,
            input_model=_SerializedNullable,
        )
        layer, _wire = _stack([_call_response(("c1", "serialized", {"safe": None, "value": "x"})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [serialized_tool]})

        assert received == [{"safe": None, "value": {"serialized": "x"}}]
        assert _result_contents(response)[0].exception is None

    @pytest.mark.asyncio
    async def test_computed_field_is_not_injected_into_arguments(self) -> None:
        received: list[int] = []

        async def computed(count: int) -> str:
            received.append(count)
            return "ok"

        computed_tool = FunctionTool(
            name="computed",
            description="Custom input model with a computed field.",
            func=computed,
            input_model=_ComputedGhost,
        )
        layer, _wire = _stack([_call_response(("c1", "computed", {"count": 5})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [computed_tool]})

        assert received == [5]
        assert _result_contents(response)[0].exception is None

    @pytest.mark.asyncio
    async def test_reordering_field_serializer_subtree_is_left_untouched(self) -> None:
        received: list[Any] = []

        async def reordered(items: list[Any]) -> str:
            received.append(items)
            return "ok"

        reordering_tool = FunctionTool(
            name="reordered",
            description="Field serializer reverses the list.",
            func=reordered,
            input_model=_ReorderingItems,
        )
        layer, _wire = _stack(
            [
                _call_response(("c1", "reordered", {"items": [{"value": None}, {"value": "KEEP"}]})),
                _text_response(),
            ]
        )
        response = await layer.get_response([_user()], options={"tools": [reordering_tool]})

        # The serializer's output is authoritative: the explicit null must NOT
        # be re-applied by original index onto the reordered dump. The loop's
        # early serialization is a discarded validation transform; invoke
        # receives the original validation-keyed payload, so its serializer's
        # single callable-facing reversal remains authoritative.
        assert received == [[{"value": "KEEP"}, {}]]
        assert _result_contents(response)[0].exception is None

    @pytest.mark.asyncio
    async def test_tuple_of_models_preserves_explicit_null_by_index(self) -> None:
        received: list[Any] = []

        async def tupled(items: Any) -> str:
            received.append(items)
            return "ok"

        tuple_tool = FunctionTool(
            name="tupled",
            description="Tuple-typed field dumps stay tuples in Python mode.",
            func=tupled,
            input_model=_TupleItems,
        )
        layer, _wire = _stack(
            [
                _call_response(("c1", "tupled", {"items": [{"value": None}, {"value": "x"}]})),
                _text_response(),
            ]
        )
        response = await layer.get_response([_user()], options={"tools": [tuple_tool]})

        assert received == [({"value": None}, {"value": "x"})]
        assert _result_contents(response)[0].exception is None

    @pytest.mark.asyncio
    async def test_serialize_by_alias_restores_null_under_the_wire_key(self) -> None:
        received: list[Any] = []

        async def aliased(**kwargs: Any) -> str:
            received.append(kwargs)
            return "ok"

        aliased_tool = FunctionTool(
            name="aliased_null",
            description="serialize_by_alias keys the dump by the wire alias.",
            func=aliased,
            input_model=_AliasedNullable,
        )
        layer, _wire = _stack([_call_response(("c1", "aliased_null", {"wire": None})), _text_response()])
        response = await layer.get_response([_user()], options={"tools": [aliased_tool]})

        # The restore must land under the alias the schema requires — a
        # field-name key fails validation as "'wire' missing" and the call
        # errors before reaching the tool.
        assert received == [{"wire": None}]
        assert _result_contents(response)[0].exception is None

    @pytest.mark.parametrize(
        ("input_model", "arguments", "expected"),
        [
            (_Round15SplitAlias, {"inputKey": None}, {"callableKey": None}),
            (_Round15SplitAlias, {"inputKey": "x"}, {"callableKey": "x"}),
            (_Round15ValidationAliasOnly, {"inputKey": None}, {"value": None}),
            (_Round15GeneratedValidationAlias, {"VALUE": None}, {"callableKey": None}),
            (_Round15PlainArguments, {"value": None}, {"value": None}),
        ],
        ids=["split-null", "split-non-null", "validation-only", "generated-validation", "plain"],
    )
    @pytest.mark.asyncio
    async def test_loop_preserves_validation_input_until_invoke(
        self, input_model: type[BaseModel], arguments: dict[str, Any], expected: dict[str, Any]
    ) -> None:
        received: list[dict[str, Any]] = []

        async def capture(**kwargs: Any) -> str:
            received.append(kwargs)
            return "ok"

        alias_tool = FunctionTool(
            name="round15_aliases",
            description="Loop validation never round-trips serialized keys through validation aliases.",
            func=capture,
            input_model=input_model,
        )
        layer, _wire = _stack([_call_response(("c1", "round15_aliases", arguments)), _text_response()])

        response = await layer.get_response([_user()], options={"tools": [alias_tool]})

        assert received == [expected]
        assert _result_contents(response)[0].exception is None

    @pytest.mark.parametrize("replacement", [True, 1.0], ids=["bool", "float"])
    @pytest.mark.asyncio
    async def test_equal_cross_type_middleware_rewrite_is_preserved(self, replacement: Any) -> None:
        received: list[Any] = []

        class _Rewrite(FunctionMiddleware):
            async def process(
                self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
            ) -> None:
                assert isinstance(context.arguments, dict)
                context.arguments["value"] = replacement
                await call_next()

        async def capture(value: Any) -> str:
            received.append(value)
            return "ok"

        rewrite_tool = FunctionTool(
            name="equal_rewrite",
            description="Type-changing middleware rewrites remain observable.",
            func=capture,
            input_model=_Round16RewriteArguments,
        )
        layer, _wire = _stack([_call_response(("c1", "equal_rewrite", {"value": 1})), _text_response()])

        response = await layer.get_response([_user()], options={"tools": [rewrite_tool]}, middleware=[_Rewrite()])

        assert len(received) == 1
        assert type(received[0]) is type(replacement)
        assert received[0] == replacement
        assert _result_contents(response)[0].exception is None

    @pytest.mark.parametrize("replacement", ["after", None], ids=["non-null", "null"])
    @pytest.mark.asyncio
    async def test_mutated_split_alias_arguments_use_callable_keyspace(self, replacement: Any) -> None:
        received: list[dict[str, Any]] = []

        class _Rewrite(FunctionMiddleware):
            async def process(
                self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
            ) -> None:
                assert context.arguments == {"callableKey": "before"}
                context.arguments["callableKey"] = replacement
                await call_next()

        async def capture(**kwargs: Any) -> str:
            received.append(kwargs)
            return "ok"

        alias_tool = FunctionTool(
            name="mutated_split_alias",
            description="Mutated middleware arguments already use callable-facing keys.",
            func=capture,
            input_model=_Round15SplitAlias,
        )
        layer, _wire = _stack([_call_response(("c1", "mutated_split_alias", {"inputKey": "before"})), _text_response()])

        response = await layer.get_response([_user()], options={"tools": [alias_tool]}, middleware=[_Rewrite()])

        assert received == [{"callableKey": replacement}]
        assert _result_contents(response)[0].exception is None

    def test_middleware_compare_is_representation_sensitive(self) -> None:
        utc = datetime.UTC
        named_utc = datetime.timezone(datetime.timedelta(0), "named")

        assert not _middleware_arguments_equal({"value": -0.0}, {"value": 0.0})
        assert not _middleware_arguments_equal(
            {"value": decimal.Decimal("1.0")},
            {"value": decimal.Decimal("1.00")},
        )
        assert not _middleware_arguments_equal(
            {"value": datetime.datetime(2026, 1, 1, tzinfo=utc)},
            {"value": datetime.datetime(2026, 1, 1, tzinfo=named_utc)},
        )

        assert _middleware_arguments_equal({"value": 1.5}, {"value": 1.5})
        assert _middleware_arguments_equal(
            {"value": decimal.Decimal("1.0")},
            {"value": decimal.Decimal("1.0")},
        )
        assert _middleware_arguments_equal(
            {"value": datetime.datetime(2026, 1, 1, tzinfo=utc)},
            {"value": datetime.datetime(2026, 1, 1, tzinfo=utc)},
        )

    @pytest.mark.asyncio
    async def test_signed_zero_middleware_rewrite_reaches_callable(self) -> None:
        received: list[float] = []

        class _Rewrite(FunctionMiddleware):
            async def process(
                self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
            ) -> None:
                assert context.arguments == {"value": -0.0}
                context.arguments["value"] = 0.0
                await call_next()

        async def capture(value: Any) -> str:
            received.append(value)
            return "ok"

        rewrite_tool = FunctionTool(
            name="signed_zero_rewrite",
            description="Representation-changing numeric rewrites remain observable.",
            func=capture,
            input_model=_Round16RewriteArguments,
        )
        layer, _wire = _stack([_call_response(("c1", "signed_zero_rewrite", {"value": -0.0})), _text_response()])

        response = await layer.get_response([_user()], options={"tools": [rewrite_tool]}, middleware=[_Rewrite()])

        assert received == [0.0]
        assert repr(received[0]) == "0.0"
        assert _result_contents(response)[0].exception is None

    @pytest.mark.parametrize(
        ("input_model", "arguments", "expected"),
        [
            (_Round17ConsumingArguments, {"x": 1}, {"x": 1}),
            (_Round17NestedConsumingArguments, {"items": [{"x": 1}]}, {"items": [{"x": 1}]}),
        ],
        ids=["top-level-pop", "nested-list-dict-pop"],
    )
    @pytest.mark.asyncio
    async def test_throwaway_validation_cannot_mutate_forwarded_original(
        self, input_model: type[BaseModel], arguments: dict[str, Any], expected: dict[str, Any]
    ) -> None:
        received: list[dict[str, Any]] = []

        async def capture(**kwargs: Any) -> str:
            received.append(kwargs)
            return "ok"

        consuming_tool = FunctionTool(
            name="consuming_validator",
            description="Throwaway validation receives an isolated container copy.",
            func=capture,
            input_model=input_model,
        )
        layer, _wire = _stack([_call_response(("c1", "consuming_validator", arguments)), _text_response()])

        response = await layer.get_response([_user()], options={"tools": [consuming_tool]})

        assert received == [expected]
        assert _result_contents(response)[0].exception is None

    @pytest.mark.asyncio
    async def test_invoke_validation_cannot_mutate_persisted_call_arguments(self) -> None:
        received: list[dict[str, Any]] = []
        arguments = {"items": [{"x": 1}]}
        function_call = Content.from_function_call(
            call_id="c1",
            name="persisted_arguments",
            arguments=arguments,
        )

        async def capture(**kwargs: Any) -> str:
            received.append(kwargs)
            return "ok"

        consuming_tool = FunctionTool(
            name="persisted_arguments",
            description="Invoke validation cannot mutate persisted tool-call input.",
            func=capture,
            input_model=_Round17NestedConsumingArguments,
        )
        first_turn = ChatResponse(messages=[Message(role="assistant", contents=[function_call])])
        layer, _wire = _stack([first_turn, _text_response()])

        response = await layer.get_response([_user()], options={"tools": [consuming_tool]})

        assert received == [{"items": [{"x": 1}]}]
        assert arguments == {"items": [{"x": 1}]}
        assert function_call.arguments == {"items": [{"x": 1}]}
        assert _result_contents(response)[0].exception is None

    @pytest.mark.asyncio
    async def test_cyclic_arguments_degrade_to_legacy_path(self) -> None:
        received: list[Any] = []
        cycle: dict[str, Any] = {}
        cycle["self"] = cycle

        async def capture(payload: Any) -> str:
            received.append(payload)
            return "ok"

        cyclic_tool = FunctionTool(
            name="cyclic_schema_arguments",
            description="Cyclic raw arguments cannot escape snapshot handling.",
            func=capture,
            input_model={
                "type": "object",
                "properties": {"payload": {"type": "object"}},
                "required": ["payload"],
                "additionalProperties": False,
            },
        )
        layer, _wire = _stack([_call_response(("c1", "cyclic_schema_arguments", {"payload": cycle})), _text_response()])

        response = await layer.get_response([_user()], options={"tools": [cyclic_tool]})

        assert len(received) == 1
        assert received[0]["self"] is received[0]
        assert _result_contents(response)[0].exception is None

    @pytest.mark.asyncio
    async def test_mapping_order_only_rewrite_may_use_untouched_fast_path(self) -> None:
        received_order: list[list[str]] = []

        class _Reorder(FunctionMiddleware):
            async def process(
                self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
            ) -> None:
                assert isinstance(context.arguments, dict)
                first = context.arguments.pop("first")
                context.arguments["first"] = first
                await call_next()

        async def capture(**kwargs: Any) -> str:
            received_order.append(list(kwargs))
            return "ok"

        ordered_tool = FunctionTool(
            name="ordered_schema_arguments",
            description="Mapping insertion order is part of middleware output.",
            func=capture,
            input_model={
                "type": "object",
                "properties": {"first": {"type": "integer"}, "second": {"type": "integer"}},
                "required": ["first", "second"],
                "additionalProperties": False,
            },
        )
        layer, _wire = _stack(
            [_call_response(("c1", "ordered_schema_arguments", {"first": 1, "second": 2})), _text_response()]
        )

        response = await layer.get_response([_user()], options={"tools": [ordered_tool]}, middleware=[_Reorder()])

        # Order-insensitive dict equality is intentional for trusted middleware.
        assert received_order == [["first", "second"]]
        assert _result_contents(response)[0].exception is None

    @pytest.mark.asyncio
    async def test_same_order_reinsert_keeps_split_alias_fast_path(self) -> None:
        received: list[dict[str, Any]] = []

        class _ReinsertLast(FunctionMiddleware):
            async def process(
                self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
            ) -> None:
                assert isinstance(context.arguments, dict)
                second = context.arguments.pop("second")
                context.arguments["second"] = second
                await call_next()

        async def capture(**kwargs: Any) -> str:
            received.append(kwargs)
            return "ok"

        alias_tool = FunctionTool(
            name="same_order_alias",
            description="A same-position reinsert remains structurally untouched.",
            func=capture,
            input_model=_Round17OrderFastPathArguments,
        )
        layer, _wire = _stack(
            [_call_response(("c1", "same_order_alias", {"inputFirst": 1, "second": 2})), _text_response()]
        )

        response = await layer.get_response([_user()], options={"tools": [alias_tool]}, middleware=[_ReinsertLast()])

        assert received == [{"callableFirst": 1, "second": 2}]
        assert _result_contents(response)[0].exception is None

    @pytest.mark.asyncio
    async def test_typed_dict_member_explicit_null_survives_loop_path(self) -> None:
        received: list[Any] = []

        async def td_nullable(**kwargs: Any) -> str:
            received.append(kwargs)
            return "ok"

        td_tool = FunctionTool(
            name="td_nullable",
            description="exclude_none drops TypedDict member nulls like model fields.",
            func=td_nullable,
            input_model=_MemberNullableHolder,
        )
        layer, _wire = _stack(
            [
                _call_response(("c1", "td_nullable", {"inner": {"value": None}})),
                _text_response(),
            ]
        )
        response = await layer.get_response([_user()], options={"tools": [td_tool]})

        assert received == [{"inner": {"value": None}}]
        assert _result_contents(response)[0].exception is None

    @pytest.mark.parametrize(
        ("stream", "calls", "tool_name", "expected"),
        [
            # A response cut off at the output token limit (finish_reason="length")
            # leaves the tool arguments as incomplete/unparseable JSON. The error
            # must name the token-limit cause instead of the opaque generic message
            # so the user knows to raise max_tokens and the model can split the call.
            pytest.param(False, [("c1", '{"text": "hello wor')], "echo", ["truncated"], id="unparseable_json"),
            # Streaming aggregates finish_reason via ``from_updates``; a "length"
            # cutoff must reach the truncation-aware error message on the stream path.
            pytest.param(True, [("c1", '{"text": "hello wor')], "echo", ["truncated"], id="stream_unparseable_json"),
            # Some providers can surface a length-cutoff tool call with an empty raw
            # argument string.  Content.parse_arguments() turns that into {}, so the
            # truncation classification must catch it before it degrades to generic
            # schema-validation failure.
            pytest.param(False, [("c1", "")], "echo", ["truncated"], id="empty_string_args"),
            # Anthropic's blocking SDK path delivers tool_use.input as a parsed dict,
            # even when a length cutoff likely omitted required fields.  An empty
            # final call mapping under finish_reason="length" should still get the
            # actionable truncation error instead of generic argument_parsing.
            pytest.param(False, [("c1", {})], "echo", ["truncated"], id="empty_final_mapping"),
            # Cover the non-empty parsed-mapping branch: Anthropic can return a
            # partial dict for a final tool call that has one required field present
            # and another omitted by the output-token cutoff.
            pytest.param(False, [("c1", {"path": "out.txt"})], "write_file", ["truncated"], id="partial_final_mapping"),
            # finish_reason="length" is response-wide. A call whose arguments parsed
            # cleanly but fail schema validation is a real argument error, not a
            # cutoff — it must keep the actionable "argument_parsing" classification
            # so the model fixes the arguments instead of being told to raise max_tokens.
            pytest.param(
                False,
                [("c1", {"wrong_arg": 1})],
                "echo",
                [("unknown 'wrong_arg'", "missing 'text'")],
                id="parseable_invalid_mapping",
            ),
            # The parsed-mapping heuristic exists for the final block that was most
            # likely cut off.  Earlier calls in the same length-truncated response
            # keep actionable argument_parsing unless their raw JSON is unparseable.
            pytest.param(
                False,
                [("c1", {}), ("c2", {"text": "ok"})],
                "echo",
                [("missing 'text'",), "ok"],
                id="non_final_mapping_stays_argument_parsing",
            ),
            # Same invariant as above, but with a JSON string so the
            # _arguments_unparseable json.loads branch is actually exercised.
            pytest.param(
                False,
                [("c1", '{"wrong_arg": 1}')],
                "echo",
                [("unknown 'wrong_arg'", "missing 'text'")],
                id="parseable_invalid_json_string",
            ),
            # finish_reason="length" alone must not fabricate an error: when the
            # arguments actually parse, the tool runs (no false positive).
            pytest.param(False, [("c1", {"text": "hello"})], "echo", ["ok"], id="valid_args_run_normally"),
            # Complete JSON that is not an object was not cut off: it is an
            # argument error naming the object requirement on both paths.
            pytest.param(
                False,
                [("c1", "[1]")],
                "echo",
                [("arguments must be a valid JSON object",)],
                id="non_object_json",
            ),
            pytest.param(
                True,
                [("c1", "[1]")],
                "echo",
                [("arguments must be a valid JSON object",)],
                id="stream_non_object_json",
            ),
            # A parsed non-mapping final block is not a cut-off mapping either.
            pytest.param(
                False,
                [("c1", [1])],
                "echo",
                [("arguments must be a valid JSON object",)],
                id="parsed_non_mapping_final",
            ),
            # Undecodable text and whitespace still read as a cutoff.
            pytest.param(False, [("c1", "oops")], "echo", ["truncated"], id="undecodable_text"),
            pytest.param(True, [("c1", "   ")], "echo", ["truncated"], id="stream_whitespace"),
        ],
    )
    async def test_length_finish_reason_classifies_truncation(
        self,
        stream: bool,
        calls: list[tuple[str, Any]],
        tool_name: str,
        expected: list[Any],
    ) -> None:
        """``finish_reason="length"`` yields ``argument_truncated`` only for arguments the cutoff broke.

        ``expected`` holds one verdict per call: ``"truncated"`` (the
        actionable max_tokens message), ``"ok"`` (the tool ran), or the tuple
        of substrings an actionable ``argument_parsing`` error must name.
        """
        events: list[str] = []
        if tool_name == "write_file":

            @tool(name="write_file")
            async def write_file(path: str, content: str) -> str:
                return f"{path}:{content}"

            tools: list[FunctionTool] = [write_file]
        else:
            tools = [_make_tool(events)]
        contents = [Content.from_function_call(call_id, tool_name, arguments=arguments) for call_id, arguments in calls]
        if stream:
            truncated_update = ChatResponseUpdate(contents=contents, role="assistant", finish_reason="length")
            layer, _wire = _stack([[truncated_update], [_text_update("final")]])
        else:
            truncated_response = ChatResponse(messages=[Message("assistant", contents)], finish_reason="length")
            layer, _wire = _stack([truncated_response, _text_response()])
        response = await _final_response(layer, [_user()], stream=stream, options={"tools": tools})
        results = _result_contents(response)
        assert len(results) == len(expected)
        for result, (_call_id, arguments), verdict in zip(results, calls, expected, strict=True):
            if verdict == "truncated":
                assert "max_tokens" in str(result.result)
                assert "cut off" in str(result.result)
                assert result.additional_properties[TOOL_FAILED_METADATA_KEY] is True
                assert result.additional_properties[TOOL_ERROR_KIND_METADATA_KEY] == "argument_truncated"
            elif verdict == "ok":
                assert result.exception is None
                assert f"echo:{arguments['text']}" in str(result.result)
            else:
                _assert_actionable_argument_error(result, tool_name, *verdict)
        ran = [
            arguments["text"] for (_call_id, arguments), verdict in zip(calls, expected, strict=True) if verdict == "ok"
        ]
        assert events == [f"tool:echo:{text}" for text in ran]
