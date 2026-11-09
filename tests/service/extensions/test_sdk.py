# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The authoring API, ``chrys.extensions``, as an extension author meets it."""

from __future__ import annotations

import ast
import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any, Literal, NotRequired, ReadOnly, Required, TypedDict

import pytest
from pydantic import BaseModel

from chrys.extensions import Extension, ToolCall, ToolContext, ToolError, ToolResult, TurnContext
from tests.support import acme_release_extension
from tests.support.extensions import EXAMPLE_EXTENSION
from tests.support.paths import SRC_ROOT


class Span(TypedDict):
    start: int
    end: int


class Nested(TypedDict):
    spans: list[Span]
    label: str | None
    note: NotRequired[str]
    kind: Required[ReadOnly[Literal["a", "b"]]]


def _tool_taking(annotation: Any) -> Any:
    """A tool whose one parameter has *annotation* (string annotations could not name a parameter)."""

    async def tool(value: Any) -> str:
        return ""

    tool.__annotations__ = {"value": Annotated[annotation, "A value"], "return": str}
    return tool


async def test_a_tool_is_returned_unchanged_and_declared_in_order() -> None:
    extension = Extension()

    async def first(ctx: ToolContext, text: Annotated[str, "Text"]) -> str:
        """Say it."""
        return text

    assert extension.tool(first) is first

    @extension.tool(name="second-tool", description="Overrides the docstring", requires_approval=False)
    async def second(count: Annotated[int, "How many"] = 1) -> str:
        """Unused."""
        return str(count)

    assert [d.name for d in extension.tools] == ["first", "second-tool"]
    one, two = extension.tools
    assert (one.description, one.requires_approval, one.context_parameter) == ("Say it.", True, "ctx")
    assert list(one.signature.parameters) == ["text"]
    assert (two.description, two.requires_approval, two.context_parameter) == ("Overrides the docstring", False, None)
    assert two.max_result_tokens == 8000
    assert await second(count=3) == "3"


@pytest.mark.parametrize(
    "annotation",
    [
        str,
        int,
        float,
        bool,
        Any,
        Literal["a", "b", 1],
        list[str],
        dict[str, int],
        str | None,
        list[dict[str, float | None]],
        Span,
        Nested,
        Annotated[list[Span], "spans"],
    ],
    ids=lambda a: repr(a)[:40],
)
def test_json_shaped_parameters_are_accepted(annotation: Any) -> None:
    extension = Extension()
    extension.tool(_tool_taking(annotation))
    assert len(extension.tools) == 1


@dataclasses.dataclass
class _Point:
    x: int


class _Model(BaseModel):
    x: int


@pytest.mark.parametrize(
    ("annotation", "kind"),
    [(_Model, "pydantic"), (_Point, "dataclass"), (dict[int, str], "int-keyed dict"), (tuple[int, int], "tuple")],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_values_that_are_not_plain_json_are_refused_when_declared(annotation: Any, kind: str) -> None:
    with pytest.raises(TypeError, match="parameter 'value' has type"):
        Extension().tool(_tool_taking(annotation))


def test_a_sync_function_is_refused() -> None:
    def blocking(text: Annotated[str, "Text"]) -> str:
        return text

    with pytest.raises(TypeError, match="must be an 'async def' function"):
        Extension().tool(blocking)  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    "source",
    [
        "async def tool(*args: str) -> str: ...",
        "async def tool(**kwargs: str) -> str: ...",
        "async def tool(text) -> str: ...",
        "async def tool(a: ToolContext, b: ToolContext) -> str: ...",
    ],
    ids=["var-positional", "var-keyword", "unannotated", "two-contexts"],
)
def test_unsupported_signatures_are_refused(source: str) -> None:
    namespace: dict[str, Any] = {"ToolContext": ToolContext}
    exec(source, namespace)
    with pytest.raises(TypeError):
        Extension().tool(namespace["tool"])


@pytest.mark.parametrize("name", ["", "has space", "dotted.name", "x" * 65])
def test_tool_names_follow_the_model_api_rule(name: str) -> None:
    async def tool() -> str:
        return ""

    with pytest.raises(ValueError, match="must be 1-64"):
        Extension().tool(name=name)(tool)


def test_an_extension_cannot_name_two_tools_alike() -> None:
    extension = Extension()

    async def one() -> str:
        return ""

    extension.tool(name="same")(one)
    with pytest.raises(ValueError, match="already has a tool named 'same'"):
        extension.tool(name="same")(one)


@pytest.mark.parametrize("bound", [-1, True, 1.5])
def test_the_result_bound_is_a_whole_number(bound: Any) -> None:
    async def tool() -> str:
        return ""

    with pytest.raises(ValueError, match="max_result_tokens"):
        Extension().tool(max_result_tokens=bound)(tool)


def test_tool_error_carries_its_message_and_code() -> None:
    error = ToolError("No such release.", code="not_found")
    assert (str(error), error.message, error.code) == ("No such release.", "No such release.", "not_found")
    assert ToolError("Failed.").code == "tool_error"


def test_a_test_context_creates_only_the_folders_it_was_given(tmp_path: Path) -> None:
    bare = ToolContext.for_testing(tmp_path)
    assert (bare.cwd, bare.workspace_dirs, bare.session_id, bare.agent_name) == (tmp_path, (tmp_path,), None, "test")
    assert bare.session_data_dir is None
    with pytest.raises(RuntimeError, match="Pass data_dir="):
        _ = bare.data_dir

    full = ToolContext.for_testing(tmp_path, data_dir=tmp_path / "data", session_data_dir=tmp_path / "session")
    assert not (tmp_path / "data").exists()
    assert full.data_dir == tmp_path / "data"
    assert full.data_dir.is_dir()
    assert full.session_data_dir == tmp_path / "session"
    assert full.session_data_dir.is_dir()


async def test_the_example_extension_is_tested_the_way_its_author_would(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
    ctx = ToolContext.for_testing(tmp_path, session_data_dir=tmp_path / "notes")
    assert await acme_release_extension.acme_project_name(ctx) == "demo"
    with pytest.raises(ToolError) as missing:
        await acme_release_extension.acme_project_name(ctx, path="nowhere.toml")
    assert missing.value.code == "invalid_project"
    note = {"title": "Faster builds", "tags": ["perf"]}
    assert await acme_release_extension.acme_add_release_note(ctx, note=note) == {
        "notes": 1,
        "last": note,
        "agent": "test",
    }
    assert json.loads((tmp_path / "notes" / "notes.json").read_text(encoding="utf-8")) == [note]


async def test_contexts_and_tool_hooks_are_returned_unchanged_and_declared_in_order(tmp_path: Path) -> None:
    extension = Extension()

    async def ticket(ctx: TurnContext) -> str | None:
        return f"Working on: {ctx.prompt}"

    assert extension.context(ticket) is ticket

    @extension.context(name="branch-status", timeout=2.5)
    async def status(ctx: TurnContext) -> str | None:
        return None

    @extension.before_tool
    async def guard(call: ToolCall) -> dict[str, Any] | None:
        if call.kind == "shell" and "rm -rf" in call.arguments.get("command", ""):
            raise ToolError("Destructive commands are not allowed.")
        return None

    @extension.before_tool(timeout=1)
    async def dry_run(call: ToolCall) -> dict[str, Any] | None:
        return {**call.arguments, "dry_run": True}

    @extension.after_tool
    async def redact(call: ToolCall, result: ToolResult) -> str | None:
        return result.text.replace("s3cret", "[redacted]")

    assert [(d.name, d.timeout) for d in extension.contexts] == [("ticket", 10.0), ("branch-status", 2.5)]
    assert [(d.func, d.timeout) for d in extension.before_tool_hooks] == [(guard, 10.0), (dry_run, 1.0)]
    assert [(d.func, d.name) for d in extension.after_tool_hooks] == [(redact, redact.__qualname__)]
    assert extension.tools == ()

    turn = TurnContext.for_testing(tmp_path, prompt="Fix the login bug")
    assert await ticket(turn) == "Working on: Fix the login bug"
    call = ToolCall(
        name="shell",
        kind="shell",
        call_id="1",
        arguments={"command": "rm -rf /"},
        context=ToolContext.for_testing(tmp_path),
    )
    with pytest.raises(ToolError, match="not allowed"):
        await guard(call)
    assert await dry_run(call) == {"command": "rm -rf /", "dry_run": True}
    result = ToolResult(text="token=s3cret", failed=False, rejected=False, duration_ms=3)
    assert await redact(call, result) == "token=[redacted]"


_DECLARERS = [Extension.context, Extension.before_tool, Extension.after_tool]
_DECLARER_IDS = ["context", "before_tool", "after_tool"]


@pytest.mark.parametrize("declare", _DECLARERS, ids=_DECLARER_IDS)
def test_a_sync_context_or_hook_is_refused(declare: Any) -> None:
    def blocking(*_: Any) -> None:
        return None

    with pytest.raises(TypeError, match="must be an 'async def' function"):
        declare(Extension(), blocking)


@pytest.mark.parametrize(
    ("declare", "source"),
    [
        (Extension.context, "async def f() -> None: ..."),
        (Extension.context, "async def f(ctx, extra) -> None: ..."),
        (Extension.context, "async def f(*, ctx) -> None: ..."),
        (Extension.before_tool, "async def f(*calls) -> None: ..."),
        (Extension.before_tool, "async def f(call, **options) -> None: ..."),
        (Extension.after_tool, "async def f(call) -> None: ..."),
    ],
    ids=[
        "context-none",
        "context-two",
        "context-keyword-only",
        "before-var-positional",
        "before-var-keyword",
        "after-one",
    ],
)
def test_a_context_or_hook_must_take_exactly_its_parameters(declare: Any, source: str) -> None:
    namespace: dict[str, Any] = {}
    exec(source, namespace)
    with pytest.raises(TypeError, match="must take"):
        declare(Extension(), namespace["f"])


def test_context_names_follow_the_tool_name_rule_and_are_unique() -> None:
    extension = Extension()

    async def ticket(ctx: TurnContext) -> str | None:
        return None

    for name in ["", "dotted.name", "x" * 65]:
        with pytest.raises(ValueError, match="must be 1-64"):
            extension.context(name=name)(ticket)
    extension.context(ticket)
    with pytest.raises(ValueError, match="already has a context named 'ticket'"):
        extension.context(ticket)


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), "10"], ids=repr)
@pytest.mark.parametrize("declare", _DECLARERS, ids=_DECLARER_IDS)
def test_a_timeout_is_a_positive_number_of_seconds(declare: Any, timeout: Any) -> None:
    async def one(first: Any) -> None:
        return None

    async def two(first: Any, second: Any) -> None:
        return None

    with pytest.raises(ValueError, match="timeout must be a number of seconds"):
        declare(Extension(), timeout=timeout)(two if declare is Extension.after_tool else one)


def test_a_turn_context_is_a_tool_context_with_the_turn_prompt(tmp_path: Path) -> None:
    turn = TurnContext.for_testing(tmp_path, prompt="Ship it", agent_name="Code", session_id="s1")
    assert isinstance(turn, ToolContext)
    assert (turn.prompt, turn.cwd, turn.workspace_dirs, turn.agent_name, turn.session_id) == (
        "Ship it",
        tmp_path,
        (tmp_path,),
        "Code",
        "s1",
    )
    assert turn.session_data_dir is None
    with pytest.raises(RuntimeError, match="Pass data_dir="):
        _ = turn.data_dir
    assert TurnContext.for_testing(tmp_path, data_dir=tmp_path / "data").data_dir.is_dir()
    assert TurnContext.for_testing(tmp_path).prompt == ""
    with pytest.raises(dataclasses.FrozenInstanceError):
        turn.prompt = "changed"  # ty: ignore[invalid-assignment]


def _first_party_imports(path: Path) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)
    return {name for name in imported if name == "chrys" or name.startswith("chrys.")}


@pytest.mark.parametrize(
    "path", [SRC_ROOT / "chrys" / "extensions.py", EXAMPLE_EXTENSION], ids=["sdk", "example-extension"]
)
def test_the_sdk_and_the_example_extension_need_nothing_else_from_icode(path: Path) -> None:
    allowed = set() if path.name == "extensions.py" else {"chrys.extensions"}
    assert _first_party_imports(path) <= allowed


def test_importing_the_sdk_loads_no_other_icode_module() -> None:
    loaded = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, chrys.extensions; print(sorted(m for m in sys.modules if m.startswith('chrys')))",
        ],
        capture_output=True,
        text=True,
        check=True,
        stdin=subprocess.DEVNULL,
    ).stdout
    assert loaded.strip() == "['chrys', 'chrys.extensions']"
