# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Python extension authoring API: ``from chrys.extensions import Extension, ToolContext, ToolError``.

An extension is a module-level ``extension = Extension()``; the agents that
enable it get what it declares, each an ``async def`` function:

- ``@extension.tool``: a tool the model calls. Its parameters
  (``Annotated[T, "description"]``) become the schema the model fills in; an
  optional ``ToolContext`` parameter receives where and for whom the call runs.
- ``@extension.context``: context the agent reads, asked for once per turn
  with a ``TurnContext``.
- ``@extension.before_tool`` / ``@extension.after_tool``: hooks that see each
  tool call iCode runs for the agent (a ``ToolCall``) before it runs, to refuse
  or change it, and after, to read or change its result (a ``ToolResult``).
  Tools the model provider runs itself (such as a provider's own web search)
  and calls whose arguments do not fit the tool never reach them.

Every declaration returns the function unchanged, so a test calls it directly
with ``ToolContext.for_testing(...)``, ``TurnContext.for_testing(...)`` or a
``ToolCall``/``ToolResult`` it builds.

This module imports nothing else from iCode: an extension's tests and type
checks need only this file. The names exported here and the members of the
classes they name are a stable surface that only grows.
"""

from __future__ import annotations

import inspect
import math
import re
import types
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, TypeVar, get_args, get_origin, overload

__all__ = ["Extension", "ToolCall", "ToolContext", "ToolError", "ToolResult", "TurnContext"]

DEFAULT_MAX_RESULT_TOKENS = 8000
"""The estimated-token bound on a tool's result unless it names another (``0`` turns it off)."""
DEFAULT_TIMEOUT_SECONDS = 10.0
"""How long a context or a tool hook may run unless it names another bound."""
CONTEXT_MAX_TOKENS = 4000
"""The estimated-token bound on the text one ``@extension.context`` function gives; longer text is cut short."""

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
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
        raise RuntimeError(f"Pass {name}= to for_testing to use ctx.{name}.")

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
        return cls(**_testing_fields(cwd, data_dir, session_data_dir, agent_name, session_id))


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnContext(ToolContext):
    """Where and for whom the turn an ``@extension.context`` function is asked about runs; valid for that call only."""

    prompt: str
    """What the turn was asked to do: the user's message, a sub-agent's task or a workflow step's prompt."""

    @classmethod
    def for_testing(
        cls,
        cwd: Path | str,
        *,
        prompt: str = "",
        data_dir: Path | str | None = None,
        session_data_dir: Path | str | None = None,
        agent_name: str = "test",
        session_id: str | None = None,
    ) -> TurnContext:
        """A context for calling a context function directly in a test, as :meth:`ToolContext.for_testing`."""
        return cls(prompt=prompt, **_testing_fields(cwd, data_dir, session_data_dir, agent_name, session_id))


def _testing_fields(
    cwd: Path | str,
    data_dir: Path | str | None,
    session_data_dir: Path | str | None,
    agent_name: str,
    session_id: str | None,
) -> dict[str, Any]:
    root = Path(cwd)
    session_dir = Path(session_data_dir) if session_data_dir is not None else None
    return {
        "cwd": root,
        "workspace_dirs": (root,),
        "agent_name": agent_name,
        "session_id": session_id,
        "_data_dir": _test_dir(Path(data_dir)) if data_dir is not None else _missing_dir("data_dir"),
        "_session_data_dir": _test_dir(session_dir) if session_dir is not None else lambda: None,
    }


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCall:
    """One tool call iCode runs for the agent, as ``@extension.before_tool`` and ``@extension.after_tool`` hooks see it."""

    name: str
    """The tool's name, as the model called it."""
    kind: str
    """The tool's kind, such as ``shell``, ``mcp`` or ``extension.<id>``; empty for a tool without one."""
    call_id: str
    """The call's id, the same in its before and after hooks."""
    arguments: dict[str, Any]
    """The arguments the call runs with, as JSON values. A copy: a before hook returns new arguments instead."""
    context: ToolContext
    """Where and for whom the call runs, with *this* extension's folders, whichever tool is called."""


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolResult:
    """How one tool call ended, as ``@extension.after_tool`` hooks see it."""

    text: str
    """The text the model reads: the tool's result, or why the call failed or did not run."""
    failed: bool
    """Whether the call did not succeed: the tool raised or reported an error, or the call did not run."""
    rejected: bool
    """Whether the call did not run because the user, a hook or an extension refused it."""
    duration_ms: int
    """How long the call took, in milliseconds."""


@dataclass(frozen=True, slots=True)
class ContextDeclaration:
    """One ``@extension.context`` function, as iCode calls it."""

    func: Callable[[TurnContext], Awaitable[str | None]]
    name: str
    """The context's name, unique in its extension; iCode keeps it in saved sessions."""
    timeout: float


@dataclass(frozen=True, slots=True)
class ToolHookDeclaration:
    """One ``@extension.before_tool`` or ``@extension.after_tool`` function, as iCode calls it."""

    func: Callable[..., Awaitable[Any]]
    name: str
    """The function's name, for messages that say which hook failed."""
    timeout: float


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


ContextFunction = TypeVar("ContextFunction", bound=Callable[[TurnContext], Awaitable[str | None]])
BeforeToolFunction = TypeVar("BeforeToolFunction", bound=Callable[[ToolCall], Awaitable[dict[str, Any] | None]])
AfterToolFunction = TypeVar("AfterToolFunction", bound=Callable[[ToolCall, ToolResult], Awaitable[str | None]])


class Extension:
    """What one extension adds to the agents that enable it; define it as a module-level ``extension``."""

    def __init__(self) -> None:
        self._tools: list[ToolDeclaration] = []
        self._contexts: list[ContextDeclaration] = []
        self._before_tool_hooks: list[ToolHookDeclaration] = []
        self._after_tool_hooks: list[ToolHookDeclaration] = []

    @property
    def tools(self) -> tuple[ToolDeclaration, ...]:
        """The extension's tools, in declaration order."""
        return tuple(self._tools)

    @property
    def contexts(self) -> tuple[ContextDeclaration, ...]:
        """The extension's context functions, in declaration order."""
        return tuple(self._contexts)

    @property
    def before_tool_hooks(self) -> tuple[ToolHookDeclaration, ...]:
        """The extension's ``before_tool`` hooks, in declaration order."""
        return tuple(self._before_tool_hooks)

    @property
    def after_tool_hooks(self) -> tuple[ToolHookDeclaration, ...]:
        """The extension's ``after_tool`` hooks, in declaration order."""
        return tuple(self._after_tool_hooks)

    @overload
    def context(self, func: ContextFunction, /) -> ContextFunction: ...

    @overload
    def context(
        self, *, name: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS
    ) -> Callable[[ContextFunction], ContextFunction]: ...

    def context(
        self,
        func: ContextFunction | None = None,
        /,
        *,
        name: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> ContextFunction | Callable[[ContextFunction], ContextFunction]:
        """Declare an ``async def`` that gives the agent context, and return it unchanged.

        iCode calls it with a :class:`TurnContext` when a turn starts and shows
        the agent the text it returns, sending it again only when it changes or
        is no longer in what the agent sees (a long conversation was condensed);
        None means it has nothing to say this turn, and context it gave earlier
        no longer applies. Text longer than :data:`CONTEXT_MAX_TOKENS`
        (estimated) is cut short. *name* defaults to the function's name. A call
        that raises or runs longer than *timeout* seconds counts as None.
        """

        def register(f: ContextFunction) -> ContextFunction:
            label = _check_callback(f, "Context", "one parameter, the TurnContext", 1)
            context_name = name if name is not None else getattr(f, "__name__", "")
            if not _NAME.fullmatch(context_name):
                raise ValueError(f"Context name {context_name!r} must be 1-64 letters, digits, '_' or '-'.")
            if any(existing.name == context_name for existing in self._contexts):
                raise ValueError(f"This extension already has a context named {context_name!r}.")
            self._contexts.append(ContextDeclaration(f, context_name, _check_timeout(timeout, label)))
            return f

        return register(func) if func is not None else register

    @overload
    def before_tool(self, func: BeforeToolFunction, /) -> BeforeToolFunction: ...

    @overload
    def before_tool(
        self, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
    ) -> Callable[[BeforeToolFunction], BeforeToolFunction]: ...

    def before_tool(
        self, func: BeforeToolFunction | None = None, /, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
    ) -> BeforeToolFunction | Callable[[BeforeToolFunction], BeforeToolFunction]:
        """Declare an ``async def`` that sees each tool call iCode runs for the agent before it runs; return it unchanged.

        It takes a :class:`ToolCall`. Raising :class:`ToolError` refuses the
        call (the model reads ``Error: <message>``); returning a dict runs the
        call with those arguments instead; None lets it run as it is. A hook
        that raises anything else or runs longer than *timeout* seconds refuses
        the call too, with a message that names the extension. The user's hooks
        and the approval check see the arguments it leaves. It runs again for
        the same call when the user edits the arguments while approving it, and
        the calls of one model response are checked concurrently.
        """

        def register(f: BeforeToolFunction) -> BeforeToolFunction:
            label = _check_callback(f, "Hook", "one parameter, the ToolCall", 1)
            self._before_tool_hooks.append(ToolHookDeclaration(f, label, _check_timeout(timeout, label)))
            return f

        return register(func) if func is not None else register

    @overload
    def after_tool(self, func: AfterToolFunction, /) -> AfterToolFunction: ...

    @overload
    def after_tool(
        self, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
    ) -> Callable[[AfterToolFunction], AfterToolFunction]: ...

    def after_tool(
        self, func: AfterToolFunction | None = None, /, *, timeout: float = DEFAULT_TIMEOUT_SECONDS
    ) -> AfterToolFunction | Callable[[AfterToolFunction], AfterToolFunction]:
        """Declare an ``async def`` that sees how each tool call iCode ran for the agent ended; return it unchanged.

        It takes a :class:`ToolCall` and a :class:`ToolResult`. Returning text
        replaces the text the model reads (unless the tool raised, when the
        model reads that error); None leaves it. If the hook raises or runs
        longer than *timeout* seconds, the model reads a note that the result
        was withheld instead.
        """

        def register(f: AfterToolFunction) -> AfterToolFunction:
            label = _check_callback(f, "Hook", "two parameters, the ToolCall and the ToolResult", 2)
            self._after_tool_hooks.append(ToolHookDeclaration(f, label, _check_timeout(timeout, label)))
            return f

        return register(func) if func is not None else register

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
    _async_label(func, "Tool")
    tool_name = name if name is not None else getattr(func, "__name__", "")
    if not _NAME.fullmatch(tool_name):
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


def _async_label(func: Callable[..., Any], what: str) -> str:
    """*func*'s name, once it is an ``async def``."""
    label = getattr(func, "__qualname__", repr(func))
    if not inspect.iscoroutinefunction(func):
        raise TypeError(
            f"{what} {label} must be an 'async def' function: iCode cannot stop a worker thread when the user "
            "presses Stop. Run blocking work with 'await asyncio.to_thread(...)'."
        )
    return label


def _check_callback(func: Callable[..., Any], what: str, takes: str, arity: int) -> str:
    """*func*'s name, once it is an ``async def`` taking exactly *arity* positional parameters."""
    label = _async_label(func, what)
    parameters = inspect.signature(func).parameters.values()
    positional = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    if len(parameters) != arity or any(parameter.kind not in positional for parameter in parameters):
        raise TypeError(f"{what} {label} must take {takes}.")
    return label


def _check_timeout(timeout: object, label: str) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < math.inf:
        raise ValueError(f"{label}: timeout must be a number of seconds greater than 0.")
    return float(timeout)


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
