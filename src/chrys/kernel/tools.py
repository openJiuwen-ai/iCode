# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Chrys-owned FunctionTool and tool-normalization surface.

This module provides ``FunctionTool`` construction, schema generation,
serialization and invocation,
``SKIP_PARSING``, ``tool()``, and ``normalize_tools()``. Telemetry remains a
loop concern in ``loop.py``; ``FunctionTool.invoke`` intentionally stays free
of observability and decorative logging. Explicit-null restoration is
classified and applied by the finite static policy in ``_null_overlay.py``.

HARD RULE: kernel modules may import only the stdlib, intra-package modules,
allowed third-party packages, and downward ``chrys.foundation.*`` modules.
Sibling or upward ``chrys.*`` imports remain forbidden.
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import inspect
import json
import logging
import types
import typing
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from functools import reduce, wraps
from operator import or_
from typing import (
    Annotated,
    Any,
    ClassVar,
    Final,
    Literal,
    TypedDict,
    TypeGuard,
    get_args,
    get_origin,
    overload,
)

from pydantic import (
    BaseModel,
    BeforeValidator,
    Field,
    ValidationError,
    create_model,
)

from ._content import Content
from ._null_overlay import (
    OverlayPolicy,
    OverlayTier,
    _classification_fallback,
    classify,
    restore_explicit_nulls,
)
from ._serialization import SerializationMixin
from .exceptions import ToolException
from .middleware import FunctionInvocationContext

__all__ = [
    "SKIP_PARSING",
    "FunctionInvocationConfiguration",
    "FunctionTool",
    "SyncToolCancelledAfterCompletion",
    "ToolException",
    "ToolTypes",
    "normalize_tools",
    "tool",
]

logger = logging.getLogger(__name__)


class SyncToolCancelledAfterCompletion(asyncio.CancelledError):
    """Cancellation that landed after a threaded sync tool already returned.

    The worker thread finished — side effects are done and the return value
    exists — but the invoking task was cancelled before its ``await`` could
    resume and deliver it. ``completed_result`` carries that value so the
    tool loop can journal it before cancellation proceeds; plain
    ``except asyncio.CancelledError`` handlers still treat this as a normal
    cancellation.
    """

    def __init__(self, completed_result: Any) -> None:
        super().__init__()
        self.completed_result = completed_result


class _SkipParsingSentinel:
    """Sentinel meaning ``FunctionTool.invoke`` should return raw values."""

    _instance: ClassVar[_SkipParsingSentinel | None] = None

    def __new__(cls) -> _SkipParsingSentinel:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "SKIP_PARSING"


SKIP_PARSING: Final[_SkipParsingSentinel] = _SkipParsingSentinel()

type ResultParser = Callable[[Any], str | list[Content]]


def _is_result_parser(value: object) -> TypeGuard[ResultParser]:
    """Narrow a configured parser after the runtime callable check."""
    return callable(value)


def _is_skip_parsing_sentinel(value: object) -> bool:
    """Accept SKIP_PARSING and foreign sentinels with the same class name and representation."""
    return value is SKIP_PARSING or (type(value).__name__ == "_SkipParsingSentinel" and repr(value) == "SKIP_PARSING")


def _annotation_includes_function_invocation_context(annotation: Any) -> bool:
    """Check whether an annotation resolves to FunctionInvocationContext."""
    candidates = get_args(annotation) or (annotation,)
    return any(
        candidate is FunctionInvocationContext
        or candidate == "FunctionInvocationContext"
        or (
            getattr(candidate, "__name__", None) == "FunctionInvocationContext"
            and str(getattr(candidate, "__module__", "")).endswith("middleware")
        )
        for candidate in candidates
    )


def _parse_annotation(annotation: Any) -> Any:
    """Convert string annotation metadata into Pydantic field descriptions."""
    origin = get_origin(annotation)
    if origin is not None:
        if origin is Literal:
            return annotation

        args = get_args(annotation)
        if len(args) > 1 and isinstance(args[1], str):
            args_list = list(args)
            if len(args_list) == 2:
                return Annotated.__getitem__((args_list[0], Field(description=args_list[1])))
            return Annotated.__getitem__((args_list[0], Field(description=args_list[1]), tuple(args_list[2:])))
    return annotation


def _reject_bool_as_int(value: Any) -> Any:
    """Reject bool before Pydantic applies its otherwise useful int coercions."""
    if isinstance(value, bool):
        raise ValueError("boolean values are not valid integers")
    return value


def _make_bool_int_literal_guard(values: tuple[Any, ...]) -> Callable[[Any], Any]:
    """Require exact type+value literal matches for raw bool/int inputs.

    Pydantic matches ``Literal`` members by equality, and ``True == 1`` /
    ``False == 0`` in Python — so ``Literal[0, True]`` would otherwise accept
    ``False`` (as ``0``) and ``1`` (as ``True``). Non-bool/int inputs pass
    through so Pydantic keeps its own lax handling for them.
    """

    def _guard(value: Any) -> Any:
        if type(value) in (bool, int) and not any(type(member) is type(value) and member == value for member in values):
            raise ValueError("literal values require an exact type and value match")
        return value

    return _guard


def _prevent_bool_int_coercion(annotation: Any) -> Any:
    """Recursively guard int annotations while preserving other Pydantic coercions."""
    if annotation is int:
        return Annotated[int, BeforeValidator(_reject_bool_as_int)]

    origin = get_origin(annotation)
    if origin is Literal:
        values = get_args(annotation)
        if any(type(value) in (bool, int) for value in values):
            return Annotated[annotation, BeforeValidator(_make_bool_int_literal_guard(values))]
        return annotation
    if origin is None:
        return annotation

    args = get_args(annotation)
    if origin is Annotated:
        base, *metadata = args
        transformed = _prevent_bool_int_coercion(base)
        if get_origin(transformed) is Annotated:
            transformed_base, *transformed_metadata = get_args(transformed)
            return Annotated[transformed_base, *metadata, *transformed_metadata]
        return Annotated[transformed, *metadata]

    transformed_args = tuple(_prevent_bool_int_coercion(arg) for arg in args)
    if transformed_args == args:
        return annotation
    if origin is types.UnionType:
        return reduce(or_, transformed_args)
    return origin[transformed_args]


def _matches_json_schema_type(value: Any, schema_type: str, *, tuples_as_arrays: bool = False) -> bool:
    """Check a value against a JSON Schema primitive type."""
    match schema_type:
        case "string":
            return isinstance(value, str)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return (isinstance(value, int | float)) and not isinstance(value, bool)
        case "boolean":
            return isinstance(value, bool)
        case "array":
            # Python-mode ``model_dump`` preserves tuple-typed fields, so the
            # validated paths opt in via ``tuples_as_arrays``. Schema-supplied
            # raw arguments keep the strict list-only contract.
            if tuples_as_arrays and isinstance(value, tuple):
                return True
            return isinstance(value, list)
        case "object":
            return isinstance(value, dict)
        case "null":
            return value is None
        case _:
            return True


def _validate_arguments_against_schema(
    *,
    arguments: Mapping[str, Any],
    schema: Mapping[str, Any],
    tool_name: str,
    tuples_as_arrays: bool = False,
    values_prevalidated: bool = False,
) -> dict[str, Any]:
    """Apply lightweight schema checks at the callable argument boundary."""
    parsed_arguments = dict(arguments)

    required_fields = [field for field in schema.get("required", []) if isinstance(field, str)]
    missing_fields = [field for field in required_fields if field not in parsed_arguments]
    if missing_fields:
        raise TypeError(f"Missing required argument(s) for '{tool_name}': {', '.join(sorted(missing_fields))}")

    if values_prevalidated:
        return parsed_arguments

    properties: Mapping[str, Any] = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        unexpected_fields = sorted(field for field in parsed_arguments if field not in properties)
        if unexpected_fields:
            raise TypeError(f"Unexpected argument(s) for '{tool_name}': {', '.join(unexpected_fields)}")

    for field_name, field_value in parsed_arguments.items():
        if not isinstance(properties.get(field_name), dict):
            continue

        enum_values = properties.get(field_name, {}).get("enum")
        if isinstance(enum_values, list) and enum_values and field_value not in enum_values:
            raise TypeError(
                f"Invalid value for '{field_name}' in '{tool_name}': {field_value!r} is not in {enum_values!r}"
            )

        schema_type = properties.get(field_name, {}).get("type")
        if isinstance(schema_type, str):
            if not _matches_json_schema_type(field_value, schema_type, tuples_as_arrays=tuples_as_arrays):
                raise TypeError(
                    f"Invalid type for '{field_name}' in '{tool_name}': "
                    f"expected {schema_type}, got {type(field_value).__name__}"
                )
            continue

        if isinstance(schema_type, list):
            allowed_types: list[str] = [item for item in schema_type if isinstance(item, str)]
            if allowed_types and not any(
                _matches_json_schema_type(field_value, item, tuples_as_arrays=tuples_as_arrays)
                for item in allowed_types
            ):
                raise TypeError(
                    f"Invalid type for '{field_name}' in '{tool_name}': expected one of "
                    f"{allowed_types}, got {type(field_value).__name__}"
                )

    return parsed_arguments


def _coerce_content(value: Any) -> Content | None:
    """Convert compatible foreign content through its serialized shape into Chrys Content."""
    if isinstance(value, Content):
        return value
    to_dict = getattr(value, "to_dict", None)
    if not callable(to_dict) or not hasattr(value, "type"):
        return None
    try:
        payload = to_dict(exclude_none=False)
    except TypeError:
        payload = to_dict()
    if not isinstance(payload, Mapping) or not isinstance(payload.get("type"), str):
        return None
    try:
        converted = Content.from_dict(payload)
    except Exception:
        return None
    converted.raw_representation = getattr(value, "raw_representation", None)
    return converted


class FunctionTool(SerializationMixin):
    """A callable tool with argument validation, result parsing and serializable schema."""

    # Inherited class identifier: serialized payloads always carry the
    # instance ``type`` field ("function_tool"), so subclasses must validate
    # against that same identifier on from_dict — the snake_case class-name
    # fallback would reject every payload a subclass itself serialized.
    TYPE: ClassVar[str] = "function_tool"
    INJECTABLE: ClassVar[set[str]] = {"func"}
    DEFAULT_EXCLUDE: ClassVar[set[str]] = {
        "additional_properties",
        "input_model",
        "_invocation_duration_histogram",
        "_cached_parameters",
        "_input_schema",
        "_serialization_schema",
        "_serialization_schema_cached",
        "_null_overlay_policy",
        "_null_overlay_policy_cached",
        "_schema_supplied",
        "_invoke_sync_on_event_loop",
    }

    def __init__(
        self,
        *,
        name: str,
        description: str = "",
        kind: str | None = None,
        max_invocations: int | None = None,
        max_invocation_exceptions: int | None = None,
        additional_properties: dict[str, Any] | None = None,
        func: Callable[..., Any] | None = None,
        input_model: type[BaseModel] | Mapping[str, Any] | None = None,
        result_parser: Callable[[Any], str | list[Content]] | _SkipParsingSentinel | object | None = None,
        **kwargs: Any,
    ) -> None:
        self.name = name
        self.description = description
        self.kind = kind
        self.additional_properties = additional_properties
        self._invoke_sync_on_event_loop = False
        for key, value in kwargs.items():
            setattr(self, key, value)

        self.func = func
        self._instance = None
        self._context_parameter_name: str | None = None
        self._input_model_explicitly_provided = input_model is not None
        if self.func:
            self._discover_injected_parameters()

        self._input_schema_cached: dict[str, Any] | None = None
        self._serialization_schema_cached: dict[str, Any] | None = None
        self._null_overlay_policy_cached: OverlayPolicy | None = None
        if isinstance(input_model, Mapping):
            self._schema_supplied = True
            self._input_schema_cached = dict(input_model)
            self.input_model: type[BaseModel] | None = None
        else:
            self._schema_supplied = False
            self.input_model = self._resolve_input_model(input_model)
        self._cached_parameters: dict[str, Any] | None = None
        if max_invocations is not None and max_invocations < 1:
            raise ValueError("max_invocations must be at least 1 or None.")
        if max_invocation_exceptions is not None and max_invocation_exceptions < 1:
            raise ValueError("max_invocation_exceptions must be at least 1 or None.")
        self.max_invocations = max_invocations
        self.invocation_count = 0
        self.max_invocation_exceptions = max_invocation_exceptions
        self.invocation_exception_count = 0
        self._invocation_duration_histogram = None
        self.type: Literal["function_tool"] = "function_tool"
        self.result_parser = SKIP_PARSING if _is_skip_parsing_sentinel(result_parser) else result_parser

    def _discover_injected_parameters(self) -> None:
        """Inspect the wrapped function for runtime injection parameters."""
        func = self.func.func if isinstance(self.func, FunctionTool) else self.func
        if func is None:
            return

        signature = inspect.signature(func)
        try:
            type_hints = typing.get_type_hints(func)
        except Exception:
            type_hints = {name: param.annotation for name, param in signature.parameters.items()}

        for name, param in signature.parameters.items():
            if name in {"self", "cls"}:
                continue
            annotation = type_hints.get(name, param.annotation)
            if self._is_context_parameter(name, annotation):
                if self._context_parameter_name is not None:
                    raise ValueError(f"Function '{self.name}' defines multiple FunctionInvocationContext parameters.")
                self._context_parameter_name = name

    def _is_context_parameter(self, name: str, annotation: Any) -> bool:
        """Check whether a callable parameter should receive FunctionInvocationContext injection."""
        if _annotation_includes_function_invocation_context(annotation):
            return True
        return self._input_model_explicitly_provided and name == "ctx" and annotation is inspect.Parameter.empty

    def __str__(self) -> str:
        """Return a string representation of the tool."""
        if self.description:
            return f"{self.__class__.__name__}(name={self.name}, description={self.description})"
        return f"{self.__class__.__name__}(name={self.name})"

    @property
    def bound_instance(self) -> object | None:
        """Return the owner of a descriptor-bound tool, or None for an unbound tool."""
        return self._instance

    @property
    def declaration_only(self) -> bool:
        """Indicate whether the function is declaration only."""
        declaration_flag = self.__dict__.get("_declaration_only", False)
        if isinstance(declaration_flag, bool) and declaration_flag:
            return True
        return self.func is None

    def __get__(self, obj: Any, objtype: type | None = None) -> FunctionTool:
        """Bind descriptor tools to their owning instance."""
        if obj is None:
            return self

        if self.func is not None:
            sig = inspect.signature(self.func)
            params = list(sig.parameters.keys())
            if params and params[0] in {"self", "cls"}:
                bound_func = copy.copy(self)
                bound_func._instance = obj
                return bound_func

        return self

    def _resolve_input_model(self, input_model: type[BaseModel] | None) -> type[BaseModel]:
        """Resolve the input model for the function."""
        if input_model is not None:
            if inspect.isclass(input_model) and issubclass(input_model, BaseModel):
                return input_model
            raise TypeError("input_model must be a Pydantic BaseModel subclass or a JSON schema dict.")

        if self.func is None:
            return create_model(f"{self.name}_input")

        func = self.func.func if isinstance(self.func, FunctionTool) else self.func
        if func is None:
            return create_model(f"{self.name}_input")
        sig = inspect.signature(func)
        try:
            type_hints = typing.get_type_hints(func, include_extras=True)
        except Exception:
            type_hints = {}
        fields: dict[str, Any] = {
            pname: (
                _prevent_bool_int_coercion(_parse_annotation(type_hints.get(pname, param.annotation)))
                if type_hints.get(pname, param.annotation) is not inspect.Parameter.empty
                else str,
                param.default if param.default is not inspect.Parameter.empty else ...,
            )
            for pname, param in sig.parameters.items()
            if pname not in {"self", "cls"}
            and pname != self._context_parameter_name
            and param.kind not in {inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD}
        }
        return create_model(f"{self.name}_input", **fields)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Call the wrapped function with the provided arguments."""
        if self.declaration_only:
            raise ToolException(f"Function '{self.name}' is declaration only and cannot be invoked.")
        if self.max_invocations is not None and self.invocation_count >= self.max_invocations:
            raise ToolException(
                f"Function '{self.name}' has reached its maximum invocation limit, you can no longer use this tool."
            )
        if (
            self.max_invocation_exceptions is not None
            and self.invocation_exception_count >= self.max_invocation_exceptions
        ):
            raise ToolException(
                f"Function '{self.name}' has reached its maximum exception limit, "
                f"you tried to use this tool too many times and it kept failing."
            )
        self.invocation_count += 1
        try:
            func = self.func
            if func is None:
                raise ToolException(f"Function '{self.name}' has no implementation.")
            if self._instance is not None:
                return func(self._instance, *args, **kwargs)
            return func(*args, **kwargs)
        except Exception:
            self.invocation_exception_count += 1
            raise

    async def _invoke_function(self, call_kwargs: Mapping[str, Any]) -> Any:
        """Run sync tools off the event loop during async invocation."""
        func = self.func.func if isinstance(self.func, FunctionTool) else self.func
        if inspect.iscoroutinefunction(func) or self._invoke_sync_on_event_loop:
            res = self.__call__(**call_kwargs)
            return await res if inspect.isawaitable(res) else res

        # ``to_thread`` equivalent with a completion record the cancellation
        # path can inspect: the worker may finish — side effects done, value
        # produced — while this task is still suspended, and a cancellation
        # arriving in that window would otherwise drop the completed result.
        var_context = contextvars.copy_context()
        completed: list[Any] = []

        def _call_and_record() -> Any:
            value = var_context.run(self.__call__, **call_kwargs)
            completed.append(value)
            return value

        try:
            res = await asyncio.get_running_loop().run_in_executor(None, _call_and_record)
        except asyncio.CancelledError:
            if completed and not inspect.isawaitable(completed[0]):
                raise SyncToolCancelledAfterCompletion(completed[0]) from None
            raise
        return await res if inspect.isawaitable(res) else res

    @property
    def _null_overlay_policy(self) -> OverlayPolicy:
        """Get the input model's one-time static null-restore policy."""
        if self._null_overlay_policy_cached is None:
            if self.input_model is None:
                raise RuntimeError("Schema-supplied tools do not have a null-overlay policy.")
            classification_failed = False
            try:
                policy = classify(self.input_model)
            except Exception as exc:
                classification_failed = True
                reason = f"classification failed with {type(exc).__name__}"
                policy = _classification_fallback(self.input_model, reason)
                logger.debug(
                    "Tool %s null-overlay classification failed; using %s restoration: %s",
                    self.name,
                    policy.tier.value,
                    reason,
                    exc_info=True,
                )
            if not classification_failed and policy.tier is not OverlayTier.FULL:
                logger.debug(
                    "Tool %s uses %s explicit-null restoration: %s",
                    self.name,
                    policy.tier.value,
                    policy.reason or "nested annotation graph is outside the static whitelist",
                )
            self._null_overlay_policy_cached = policy
        return self._null_overlay_policy_cached

    def _dump_arguments(self, validated: BaseModel) -> dict[str, Any]:
        """Dump once, then apply the cached static explicit-null policy."""
        if self.input_model is None:
            raise RuntimeError("Schema-supplied tools do not use typed argument dumps.")
        if type(validated) is self.input_model:
            dumped = validated.model_dump(exclude_none=True)
        else:
            dumped = self.input_model.__pydantic_serializer__.to_python(validated, exclude_none=True)
        if not isinstance(dumped, dict):
            return dumped
        return restore_explicit_nulls(validated, dumped, self._null_overlay_policy)

    @overload
    async def invoke(
        self,
        *,
        arguments: BaseModel | Mapping[str, Any] | None = None,
        context: FunctionInvocationContext | None = None,
        tool_call_id: str | None = None,
        skip_parsing: Literal[True],
        **kwargs: Any,
    ) -> Any: ...

    @overload
    async def invoke(
        self,
        *,
        arguments: BaseModel | Mapping[str, Any] | None = None,
        context: FunctionInvocationContext | None = None,
        tool_call_id: str | None = None,
        skip_parsing: Literal[False] = False,
        **kwargs: Any,
    ) -> list[Content]: ...

    async def invoke(
        self,
        *,
        arguments: BaseModel | Mapping[str, Any] | None = None,
        context: FunctionInvocationContext | None = None,
        tool_call_id: str | None = None,
        skip_parsing: bool = False,
        **kwargs: Any,
    ) -> list[Content] | Any:
        """Run the wrapped function and parse the result to Chrys Content."""
        if self.declaration_only:
            raise ToolException(f"Function '{self.name}' is declaration only and cannot be invoked.")

        configured_parser = self.result_parser
        skip_parsing = skip_parsing or _is_skip_parsing_sentinel(configured_parser)
        parser = configured_parser if _is_result_parser(configured_parser) else FunctionTool.parse_result
        arguments_in_callable_keyspace = bool(kwargs.pop("_arguments_in_callable_keyspace", False))

        parameter_names = set(self.parameters().get("properties", {}).keys())
        direct_argument_kwargs = (
            {key: value for key, value in kwargs.items() if key in parameter_names} if arguments is None else {}
        )
        runtime_kwargs = dict(context.kwargs) if context is not None else {}
        unexpected_kwargs = {key: value for key, value in kwargs.items() if key not in direct_argument_kwargs}
        if unexpected_kwargs:
            unexpected_names = ", ".join(sorted(unexpected_kwargs))
            raise TypeError(
                f"Unexpected keyword argument(s) for tool '{self.name}': {unexpected_names}. "
                "Pass runtime data via FunctionInvocationContext instead."
            )
        if arguments is None and direct_argument_kwargs:
            arguments = direct_argument_kwargs
        if arguments is None and context is not None:
            arguments = context.arguments

        if arguments is None:
            validated_arguments: dict[str, Any] = {}
        else:
            # Tuple-typed fields only survive into the arguments via a
            # python-mode ``model_dump``; raw schema-supplied arguments keep
            # the strict list-only array contract.
            typed_model_dump = False
            try:
                if arguments_in_callable_keyspace:
                    if self.input_model is None or self._schema_supplied or not isinstance(arguments, Mapping):
                        raise TypeError("Callable-keyspace arguments require a typed-model mapping.")
                    parsed_arguments = dict(arguments)
                    typed_model_dump = True
                elif isinstance(arguments, Mapping):
                    parsed_arguments = dict(arguments)
                    if self.input_model is not None and not self._schema_supplied:
                        try:
                            validation_arguments = copy.deepcopy(parsed_arguments)
                        except Exception:
                            validation_arguments = parsed_arguments
                        validated = self.input_model.model_validate(validation_arguments)
                        parsed_arguments = self._dump_arguments(validated)
                        typed_model_dump = True
                elif isinstance(arguments, BaseModel):
                    if (
                        self.input_model is not None
                        and not self._schema_supplied
                        and not isinstance(arguments, self.input_model)
                    ):
                        raise TypeError(f"Expected {self.input_model.__name__}, got {type(arguments).__name__}")
                    if self.input_model is not None and not self._schema_supplied:
                        parsed_arguments = self._dump_arguments(arguments)
                    else:
                        # Schema-supplied/no-model tools have no declared model
                        # policy; preserve their runtime-model behavior.
                        parsed_arguments = arguments.model_dump(exclude_none=True)
                    typed_model_dump = not self._schema_supplied
                else:
                    raise TypeError(
                        f"Expected mapping-like arguments for tool '{self.name}', got {type(arguments).__name__}"
                    )
            except ValidationError as exc:
                raise TypeError(f"Invalid arguments for '{self.name}': {exc}") from exc

            validated_arguments = _validate_arguments_against_schema(
                arguments=parsed_arguments,
                schema=self._serialization_schema
                if typed_model_dump and not self._schema_supplied
                else self.parameters(),
                tool_name=self.name,
                tuples_as_arrays=typed_model_dump,
                values_prevalidated=typed_model_dump and not self._schema_supplied,
            )

        effective_context = context
        if effective_context is None and self._context_parameter_name is not None:
            effective_context = FunctionInvocationContext(
                function=self,
                arguments=validated_arguments,
                kwargs=runtime_kwargs,
            )
        if effective_context is not None:
            effective_context.function = self
            effective_context.arguments = validated_arguments
            effective_context.kwargs = dict(runtime_kwargs)

        call_kwargs = dict(validated_arguments)
        if self._context_parameter_name is not None and effective_context is not None:
            call_kwargs[self._context_parameter_name] = effective_context

        try:
            result = await self._invoke_function(call_kwargs)
        except SyncToolCancelledAfterCompletion as exc:
            # Parse the drained value in place (parsers are synchronous) so
            # the journaled result has the same shape a normal return would
            # have produced.
            if not skip_parsing:
                try:
                    parsed = parser(exc.completed_result)
                except Exception:
                    logger.warning("Function %s: result parser failed, falling back to str().", self.name)
                    parsed = [Content.from_text(str(exc.completed_result))]
                exc.completed_result = FunctionTool._normalize_parser_output(parsed)
            raise
        if skip_parsing:
            return result
        try:
            parsed = parser(result)
        except Exception:
            logger.warning("Function %s: result parser failed, falling back to str().", self.name)
            parsed = [Content.from_text(str(result))]
        return FunctionTool._normalize_parser_output(parsed)

    @property
    def _input_schema(self) -> dict[str, Any]:
        """Get the input schema, generating it lazily if needed."""
        if self._input_schema_cached is None:
            if self.input_model is not None:
                with suppress(Exception):
                    self.input_model.model_rebuild(force=False, raise_errors=False)
                self._input_schema_cached = self.input_model.model_json_schema()
            else:
                self._input_schema_cached = {}
        return self._input_schema_cached

    @property
    def _serialization_schema(self) -> dict[str, Any]:
        """Get the typed model's callable-facing serialization schema."""
        if self._serialization_schema_cached is None:
            if self.input_model is not None:
                with suppress(Exception):
                    self.input_model.model_rebuild(force=False, raise_errors=False)
                by_alias = bool(self.input_model.model_config.get("serialize_by_alias", False))
                try:
                    schema = self.input_model.model_json_schema(mode="serialization", by_alias=by_alias)
                except Exception:
                    schema = {}
                else:
                    policy = self._null_overlay_policy
                    if policy.tier is OverlayTier.FULL:
                        restorable = set(self.input_model.model_fields) - set(
                            policy.never_restore.get(self.input_model, frozenset())
                        )
                    elif policy.tier is OverlayTier.TOP_LEVEL:
                        restorable = set(policy.top_level_restorable)
                    else:
                        restorable = set()
                    policy_keys = policy.dump_keys.get(self.input_model, {})
                    required: set[str] = set()
                    for field_name, field_info in self.input_model.model_fields.items():
                        if by_alias:
                            dump_key = (
                                field_info.serialization_alias
                                if field_info.serialization_alias is not None
                                else field_info.alias
                                if field_info.alias is not None
                                else field_name
                            )
                        else:
                            dump_key = field_name
                        dump_key = policy_keys.get(field_name, dump_key)
                        if field_info.is_required() and field_name in restorable:
                            required.add(dump_key)
                    schema["required"] = sorted(required)
                self._serialization_schema_cached = schema
            else:
                self._serialization_schema_cached = {}
        return self._serialization_schema_cached

    def parameters(self) -> dict[str, Any]:
        """Create and cache the JSON schema for the function parameters."""
        if self._cached_parameters is None:
            self._cached_parameters = self._input_schema
        return self._cached_parameters

    @staticmethod
    def _make_dumpable(value: Any) -> Any:
        """Recursively convert a value to a JSON-dumpable form."""
        if isinstance(value, list):
            return [FunctionTool._make_dumpable(item) for item in value]
        if isinstance(value, dict):
            return {key: FunctionTool._make_dumpable(item) for key, item in value.items()}
        if content := _coerce_content(value):
            return content.to_dict(exclude={"raw_representation", "additional_properties"})
        if isinstance(value, BaseModel):
            return value.model_dump()
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            return to_dict()
        text = getattr(value, "text", None)
        if isinstance(text, str):
            return text
        return value

    @staticmethod
    def parse_result(result: Any) -> list[Content]:
        """Convert a raw function return value to Chrys ``Content`` items."""
        if result is None:
            return [Content.from_text("")]
        if isinstance(result, str):
            return [Content.from_text(result)]
        if content := _coerce_content(result):
            return [content]
        if isinstance(result, list) and any(_coerce_content(item) is not None for item in result):
            parsed_items: list[Content] = []
            for item in result:
                if content := _coerce_content(item):
                    parsed_items.append(content)
                else:
                    dumpable = FunctionTool._make_dumpable(item)
                    text = dumpable if isinstance(dumpable, str) else json.dumps(dumpable, default=str)
                    parsed_items.append(Content.from_text(text))
            return parsed_items
        dumpable = FunctionTool._make_dumpable(result)
        if isinstance(dumpable, str):
            return [Content.from_text(dumpable)]
        return [Content.from_text(json.dumps(dumpable, default=str))]

    @staticmethod
    def _normalize_parser_output(parsed: Any) -> Any:
        """Normalize custom parser output into Chrys Content while retaining existing Content lists."""
        if isinstance(parsed, str):
            return [Content.from_text(parsed)]
        if content := _coerce_content(parsed):
            return [content]
        if not isinstance(parsed, list) or not parsed:
            return parsed
        if all(isinstance(item, Content) for item in parsed):
            return parsed
        if not any(_coerce_content(item) is not None for item in parsed):
            return parsed

        normalized: list[Content] = []
        for item in parsed:
            if content := _coerce_content(item):
                normalized.append(content)
            else:
                dumpable = FunctionTool._make_dumpable(item)
                text = dumpable if isinstance(dumpable, str) else json.dumps(dumpable, default=str)
                normalized.append(Content.from_text(text))
        return normalized

    def to_json_schema_spec(self) -> dict[str, Any]:
        """Convert this tool to the JSON Schema function specification format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters(),
            },
        }

    def to_dict(self, *, exclude: set[str] | None = None, exclude_none: bool = True) -> dict[str, Any]:
        """Serialize the tool with input_model represented as its parameter schema."""
        as_dict = super().to_dict(exclude=exclude, exclude_none=exclude_none)
        if (exclude and "input_model" in exclude) or not self.input_model:
            return as_dict
        as_dict["input_model"] = self.parameters()
        return as_dict


type ToolTypes = FunctionTool | Mapping[str, Any] | object


class FunctionInvocationConfiguration(TypedDict, total=False):
    """Configuration for function invocation in chat clients."""

    enabled: bool
    max_iterations: int
    max_function_calls: int | None
    max_consecutive_errors_per_request: int
    terminate_on_unknown_calls: bool
    additional_tools: Sequence[FunctionTool]
    include_detailed_errors: bool


@overload
def tool(
    func: Callable[..., Any],
    *,
    name: str | None = None,
    description: str | None = None,
    schema: type[BaseModel] | Mapping[str, Any] | None = None,
    kind: str | None = None,
    max_invocations: int | None = None,
    max_invocation_exceptions: int | None = None,
    additional_properties: dict[str, Any] | None = None,
    result_parser: Callable[[Any], str | list[Content]] | _SkipParsingSentinel | object | None = None,
) -> FunctionTool: ...


@overload
def tool(
    func: None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    schema: type[BaseModel] | Mapping[str, Any] | None = None,
    kind: str | None = None,
    max_invocations: int | None = None,
    max_invocation_exceptions: int | None = None,
    additional_properties: dict[str, Any] | None = None,
    result_parser: Callable[[Any], str | list[Content]] | _SkipParsingSentinel | object | None = None,
) -> Callable[[Callable[..., Any]], FunctionTool]: ...


def tool(
    func: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    schema: type[BaseModel] | Mapping[str, Any] | None = None,
    kind: str | None = None,
    max_invocations: int | None = None,
    max_invocation_exceptions: int | None = None,
    additional_properties: dict[str, Any] | None = None,
    result_parser: Callable[[Any], str | list[Content]] | _SkipParsingSentinel | object | None = None,
) -> FunctionTool | Callable[[Callable[..., Any]], FunctionTool]:
    """Decorate a function into a Chrys ``FunctionTool``."""

    def decorator(func: Callable[..., Any]) -> FunctionTool:
        @wraps(func)
        def wrapper(f: Callable[..., Any]) -> FunctionTool:
            tool_name: str = name or getattr(f, "__name__", "unknown_function")
            tool_desc: str = description or (f.__doc__ or "")
            return FunctionTool(
                name=tool_name,
                description=tool_desc,
                kind=kind,
                max_invocations=max_invocations,
                max_invocation_exceptions=max_invocation_exceptions,
                additional_properties=additional_properties or {},
                func=f,
                input_model=schema,
                result_parser=result_parser,
            )

        return wrapper(func)

    return decorator(func) if func else decorator


def normalize_tools(
    tools: ToolTypes | Callable[..., Any] | Sequence[ToolTypes | Callable[..., Any]] | None,
) -> list[ToolTypes]:
    """Normalize tool inputs while preserving non-callable tool objects."""
    if not tools:
        return []

    if isinstance(tools, (str, bytes, bytearray, Mapping)) or not isinstance(tools, Sequence):
        tools = [tools]

    normalized: list[ToolTypes] = []
    for tool_item in tools:
        if isinstance(tool_item, FunctionTool):
            normalized.append(tool_item)
            continue
        if isinstance(tool_item, dict):
            normalized.append(tool_item)
            continue
        if callable(tool_item):
            normalized.append(tool(tool_item))
            continue
        collection_tools = getattr(tool_item, "tools", None)
        if isinstance(collection_tools, Iterable) and not isinstance(
            collection_tools, (str, bytes, bytearray, Mapping)
        ):
            normalized.extend(normalize_tools(list(collection_tools)))
            continue
        if isinstance(tool_item, Iterable) and not isinstance(tool_item, (str, bytes, bytearray, Mapping, BaseModel)):
            normalized.extend(normalize_tools(list(tool_item)))
            continue
        normalized.append(tool_item)
    return normalized


def declared_tool_name(tool_item: ToolTypes) -> str | None:
    """Return the declared name of an owned function tool or function-tool mapping."""
    if isinstance(tool_item, FunctionTool):
        return tool_item.name
    if isinstance(tool_item, Mapping):
        function = tool_item.get("function")
        if not isinstance(function, Mapping):
            return None
        name = function.get("name")
        return name if isinstance(name, str) else None
    return None


def _append_unique_tools(existing_tools: list[ToolTypes], new_tools: Sequence[ToolTypes]) -> list[ToolTypes]:
    """Append named tools with atomic duplicate-name validation.

    The caller supplies a throwaway ``existing_tools`` copy when it needs
    all-or-nothing mutation of a live list. Re-adding the exact same object is
    a no-op; a different object with the same declared name is rejected.
    Nameless provider tool objects remain append-only.
    """
    seen_by_name: dict[str, ToolTypes] = {}
    for tool_item in existing_tools:
        if tool_name := declared_tool_name(tool_item):
            seen_by_name[tool_name] = tool_item

    for tool_item in new_tools:
        tool_name = declared_tool_name(tool_item)
        if tool_name is None:
            existing_tools.append(tool_item)
            continue

        existing_tool = seen_by_name.get(tool_name)
        if existing_tool is None:
            seen_by_name[tool_name] = tool_item
            existing_tools.append(tool_item)
            continue
        if existing_tool is tool_item:
            continue
        raise ValueError(f"Duplicate tool name '{tool_name}'. Tool names must be unique.")

    return existing_tools
