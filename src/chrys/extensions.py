# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Python extension authoring API: ``from chrys.extensions import Extension, ToolContext, ToolError``.

An extension is a module-level ``extension = Extension()``; the agents that
enable it get what it declares. Today that is tools: each ``@extension.tool``
function is an ``async def`` whose parameters (``Annotated[T, "description"]``)
become the schema the model fills in; an optional ``ToolContext`` parameter
receives where and for whom the call runs. ``@extension.tool`` returns the
function unchanged, so a test calls it directly with
``ToolContext.for_testing(...)``.

This module imports nothing else from iCode: an extension's tests and type
checks need only this file. The names exported here and ``ToolContext``'s
members are a stable surface that only grows.
"""

from __future__ import annotations

import inspect
import re
import types
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar, get_args, get_origin, overload

__all__ = ["Extension", "ToolContext", "ToolError"]

DEFAULT_MAX_RESULT_TOKENS = 8000
"""The estimated-token bound on a tool's result unless it names another (``0`` turns it off)."""

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_JSON_SCALARS: tuple[object, ...] = (str, int, float, bool, type(None), Any)
_FIELD_QUALIFIERS: tuple[object, ...] = (typing.Required, typing.NotRequired, typing.ReadOnly)

ToolFunction = TypeVar("ToolFunction", bound=Callable[..., Awaitable[Any]])


class ToolError(Exception):
    """A failure the model reads as ``Error: <message>``.

    *code* names the kind of failure in the call's failure record (for example
    ``"not_found"``). Any other exception shows the model only that the tool
    failed; its text goes to the failure record alone.
    """

    def __init__(self, message: str, *, code: str = "tool_error") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _missing_dir(name: str) -> Callable[[], Path]:
    def missing() -> Path:
        raise RuntimeError(f"Pass {name}= to ToolContext.for_testing to use ctx.{name}.")

    return missing


def _test_dir(path: Path) -> Callable[[], Path]:
    def make() -> Path:
        path.mkdir(parents=True, exist_ok=True)
        return path

    return make


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolContext:
    """Where and for whom one tool call runs; valid for that call only."""

    cwd: Path
    """The workspace's primary folder: resolve relative paths against it, as iCode's own tools do."""
    workspace_dirs: tuple[Path, ...]
    """Every workspace folder, ``cwd`` first."""
    agent_name: str
    """The calling agent's profile name (a sub-agent or workflow node names its own profile)."""
    session_id: str | None
    """The saved session the call belongs to, or None outside one."""
    _data_dir: Callable[[], Path] = field(repr=False, compare=False)
    _session_data_dir: Callable[[], Path | None] = field(repr=False, compare=False)

    @property
    def data_dir(self) -> Path:
        """This extension's own folder for the current user, created on first use and readable only by them."""
        return self._data_dir()

    @property
    def session_data_dir(self) -> Path | None:
        """This extension's own folder inside the current session, created on first use; None outside a session.

        Forking a session copies it, so keep paths in it relative.
        """
        return self._session_data_dir()

    @classmethod
    def for_testing(
        cls,
        cwd: Path | str,
        *,
        data_dir: Path | str | None = None,
        session_data_dir: Path | str | None = None,
        agent_name: str = "test",
        session_id: str | None = None,
    ) -> ToolContext:
        """A context for calling a tool directly in a test; it never touches the real iCode folders.

        ``ctx.data_dir`` raises unless *data_dir* is given; ``ctx.session_data_dir``
        is None (no session) unless *session_data_dir* is given.
        """
        root = Path(cwd)
        session_dir = Path(session_data_dir) if session_data_dir is not None else None
        return cls(
            cwd=root,
            workspace_dirs=(root,),
            agent_name=agent_name,
            session_id=session_id,
            _data_dir=_test_dir(Path(data_dir)) if data_dir is not None else _missing_dir("data_dir"),
            _session_data_dir=_test_dir(session_dir) if session_dir is not None else lambda: None,
        )


@dataclass(frozen=True, slots=True)
class ToolDeclaration:
    """One ``@extension.tool`` function, as iCode binds it."""

    func: Callable[..., Awaitable[Any]]
    name: str
    description: str
    requires_approval: bool
    max_result_tokens: int
    context_parameter: str | None
    """The parameter that receives the ``ToolContext``, if any."""
    signature: inspect.Signature
    """The model-facing parameters, annotations resolved, without the context one."""


class Extension:
    """What one extension adds to the agents that enable it; define it as a module-level ``extension``."""

    def __init__(self) -> None:
        self._tools: list[ToolDeclaration] = []

    @property
    def tools(self) -> tuple[ToolDeclaration, ...]:
        """The extension's tools, in declaration order."""
        return tuple(self._tools)

    @overload
    def tool(self, func: ToolFunction, /) -> ToolFunction: ...

    @overload
    def tool(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
        requires_approval: bool = True,
        max_result_tokens: int = DEFAULT_MAX_RESULT_TOKENS,
    ) -> Callable[[ToolFunction], ToolFunction]: ...

    def tool(
        self,
        func: ToolFunction | None = None,
        /,
        *,
        name: str | None = None,
        description: str | None = None,
        requires_approval: bool = True,
        max_result_tokens: int = DEFAULT_MAX_RESULT_TOKENS,
    ) -> ToolFunction | Callable[[ToolFunction], ToolFunction]:
        """Declare an ``async def`` tool and return it unchanged.

        *name* defaults to the function's name and *description* to its
        docstring. With *requires_approval* (the default) the tool asks before
        it runs unless the agent's approval rules say otherwise; False leaves
        it to the agent's default. A result longer than *max_result_tokens* is
        shortened, its full text saved in the session; ``0`` turns this bound
        off. Unsupported signatures fail here, at import, not when called.
        """

        def register(f: ToolFunction) -> ToolFunction:
            declaration = _declare(
                f,
                name=name,
                description=description,
                requires_approval=requires_approval,
                max_result_tokens=max_result_tokens,
            )
            if any(existing.name == declaration.name for existing in self._tools):
                raise ValueError(f"This extension already has a tool named {declaration.name!r}.")
            self._tools.append(declaration)
            return f

        return register(func) if func is not None else register


def _declare(
    func: Callable[..., Awaitable[Any]],
    *,
    name: str | None,
    description: str | None,
    requires_approval: bool,
    max_result_tokens: int,
) -> ToolDeclaration:
    label = getattr(func, "__qualname__", repr(func))
    if not inspect.iscoroutinefunction(func):
        raise TypeError(
            f"Tool {label} must be an 'async def' function: iCode cannot stop a worker thread when a call is "
            "interrupted. Run blocking work with 'await asyncio.to_thread(...)'."
        )
    tool_name = name if name is not None else getattr(func, "__name__", "")
    if not _TOOL_NAME.fullmatch(tool_name):
        raise ValueError(f"Tool name {tool_name!r} must be 1-64 letters, digits, '_' or '-'.")
    if isinstance(max_result_tokens, bool) or not isinstance(max_result_tokens, int) or max_result_tokens < 0:
        raise ValueError(f"Tool {tool_name!r}: max_result_tokens must be a whole number, 0 or more.")
    try:
        hints = typing.get_type_hints(func, include_extras=True)
    except Exception as exc:
        raise TypeError(f"Tool {tool_name!r}: its parameter annotations cannot be resolved ({exc}).") from exc

    context_parameter: str | None = None
    parameters: list[inspect.Parameter] = []
    for parameter in inspect.signature(func).parameters.values():
        if parameter.kind not in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            raise TypeError(f"Tool {tool_name!r}: parameter {parameter.name!r} must be a named parameter.")
        if parameter.name not in hints:
            raise TypeError(f"Tool {tool_name!r}: parameter {parameter.name!r} needs a type annotation.")
        hint = hints[parameter.name]
        if hint is ToolContext:
            if context_parameter is not None:
                raise TypeError(f"Tool {tool_name!r} takes more than one ToolContext parameter.")
            context_parameter = parameter.name
            continue
        if not _json_shaped(hint, set()):
            raise TypeError(
                f"Tool {tool_name!r}: parameter {parameter.name!r} has type {hint!r}. Use str, int, float, bool, "
                "None, Any, Literal, list, dict[str, ...], unions of these, or a TypedDict of these."
            )
        parameters.append(parameter.replace(annotation=hint))
    return ToolDeclaration(
        func=func,
        name=tool_name,
        description=description if description is not None else (inspect.getdoc(func) or ""),
        requires_approval=requires_approval,
        max_result_tokens=max_result_tokens,
        context_parameter=context_parameter,
        signature=inspect.Signature(parameters, return_annotation=str),
    )


def _json_shaped(hint: Any, seen: set[int]) -> bool:
    """Whether a value of *hint* reaches the tool as the same plain value the approval saw."""
    origin = get_origin(hint)
    # TypedDict field qualifiers say whether a key is present, not what its value is.
    if origin is Annotated or origin in _FIELD_QUALIFIERS:
        return _json_shaped(get_args(hint)[0], seen)
    if any(hint is scalar for scalar in _JSON_SCALARS) or hint is None:
        return True
    if hint is list or hint is dict:
        return True
    if origin is Literal:
        return all(isinstance(value, (str, int, float, bool, type(None))) for value in get_args(hint))
    if origin is list:
        return all(_json_shaped(arg, seen) for arg in get_args(hint))
    if origin is dict:
        key, value = get_args(hint)
        return key is str and _json_shaped(value, seen)
    if origin is typing.Union or origin is types.UnionType:
        return all(_json_shaped(arg, seen) for arg in get_args(hint))
    if typing.is_typeddict(hint):
        if id(hint) in seen:
            return True
        seen.add(id(hint))
        return all(
            _json_shaped(field_hint, seen) for field_hint in typing.get_type_hints(hint, include_extras=True).values()
        )
    return False
