# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A declared tool as the model and the approval policy meet it."""

from __future__ import annotations

import json
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, NotRequired, TypedDict

import pytest

from chrys.extensions import Extension, ToolContext, ToolError
from chrys.foundation.platform import get_platform
from chrys.foundation.tool_approval import declares_requires_approval
from chrys.foundation.tool_kinds import get_tool_kind
from chrys.foundation.tool_result_metadata import TOOL_ERROR_KIND_METADATA_KEY, TOOL_FAILED_METADATA_KEY
from chrys.kernel import FunctionTool
from chrys.service.approval.policy import ApprovalPolicy
from chrys.service.extensions import bind_extension_tools
from chrys.service.profiles.agents.schema import ApprovalConfig
from chrys.service.tools.result_metadata import tool_result_metadata
from chrys.service.tools.spill import TOOL_RESULTS_DIR_NAME
from tests.service.extensions._support import extension_build
from tests.support import acme_release_extension

_WORDS = " ".join(f"word{i}" for i in range(4000))


@contextmanager
def _tool_metadata() -> Iterator[dict[str, object]]:
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        yield metadata
    finally:
        tool_result_metadata.reset(token)


def _bound(extension: Extension, tmp_path: Path, *, session_dir: Path | None = None) -> dict[str, FunctionTool]:
    build = extension_build(tmp_path, session_dir=session_dir)
    return {tool.name: tool for tool in bind_extension_tools("demo", extension, build)}


async def _call(tool: FunctionTool, **arguments: Any) -> Any:
    return await tool.invoke(arguments=arguments, skip_parsing=True)


def test_the_model_sees_the_declared_parameters_and_never_the_context(tmp_path: Path) -> None:
    tools = _bound(acme_release_extension.extension, tmp_path)
    assert list(tools) == ["acme_project_name", "acme_add_release_note"]
    spec = tools["acme_add_release_note"].to_json_schema_spec()["function"]
    assert spec["description"] == "Add a note to this session's release notes and say how many there are."
    assert list(spec["parameters"]["properties"]) == ["note"]
    assert spec["parameters"]["required"] == ["note"]
    note = spec["parameters"]["properties"]["note"]
    assert note["description"] == "The note to add to this session's release notes"
    name = tools["acme_project_name"].to_json_schema_spec()["function"]["parameters"]
    assert name["properties"]["path"]["default"] == "pyproject.toml"
    assert "required" not in name or name["required"] == []


def test_each_tool_has_its_extension_s_kind_and_its_own_approval_default(tmp_path: Path) -> None:
    tools = _bound(acme_release_extension.extension, tmp_path)
    assert {get_tool_kind(tool) for tool in tools.values()} == {"extension.demo"}
    assert not declares_requires_approval(tools["acme_project_name"])
    assert declares_requires_approval(tools["acme_add_release_note"])


@pytest.mark.parametrize(
    ("overrides", "asks"),
    [
        ({}, {"acme_project_name": False, "acme_add_release_note": True}),
        ({"extension.demo": "auto"}, {"acme_project_name": False, "acme_add_release_note": False}),
        ({"extension.demo.acme_project_name": "require"}, {"acme_project_name": True, "acme_add_release_note": True}),
    ],
    ids=["declared", "kind-override", "kind-and-name-override"],
)
def test_a_profile_overrides_an_extension_s_approval_by_its_kind(
    tmp_path: Path, overrides: dict[str, Any], asks: dict[str, bool]
) -> None:
    tools = list(_bound(acme_release_extension.extension, tmp_path).values())
    policy = ApprovalPolicy(ApprovalConfig(default="auto", overrides=overrides), tools)
    assert {name: policy.should_require_approval(name) for name in asks} == asks


async def test_a_tool_gets_validated_arguments_and_this_agent_s_context(tmp_path: Path) -> None:
    seen: list[tuple[ToolContext, dict[str, Any]]] = []
    extension = Extension()

    @extension.tool
    async def record(
        context: ToolContext,
        note: Annotated[acme_release_extension.ReleaseNote, "A note"],
        count: Annotated[int, "A count"] = 1,
    ) -> None:
        """Record the call."""
        seen.append((context, {"note": note, "count": count}))

    extra = tmp_path / "docs"
    build = extension_build(tmp_path, extra_dirs=(extra, tmp_path), session_dir=tmp_path / "session")
    (tool,) = bind_extension_tools("demo", extension, build)
    assert await _call(tool, note={"title": "T", "tags": ["a"]}) == ""
    with pytest.raises(TypeError, match="Invalid arguments"):
        await _call(tool, note={"title": "T"})
    ((ctx, arguments),) = seen
    assert arguments == {"note": {"title": "T", "tags": ["a"]}, "count": 1}
    assert type(arguments["note"]) is dict
    assert (ctx.cwd, ctx.workspace_dirs, ctx.agent_name, ctx.session_id) == (
        tmp_path,
        (tmp_path, extra),
        "Coder",
        "s-1",
    )


class _Labelled(TypedDict):
    label: str | None
    note: NotRequired[str]


async def test_values_arrive_as_the_model_sent_them_nulls_included(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []
    extension = Extension()

    @extension.tool
    async def keep(
        value: Annotated[str | None, "A value"], item: Annotated[_Labelled, "An item"], count: Annotated[int, "N"] = 2
    ) -> str:
        """Keep what arrives."""
        seen.append({"value": value, "item": item, "count": count})
        return ""

    (tool,) = _bound(extension, tmp_path).values()
    await _call(tool, value=None, item={"label": None})
    await _call(tool, value="v", item={"label": "l", "note": "n"}, count=3)
    assert seen == [
        {"value": None, "item": {"label": None}, "count": 2},
        {"value": "v", "item": {"label": "l", "note": "n"}, "count": 3},
    ]


async def test_private_folders_are_made_owner_only_when_first_used(tmp_path: Path) -> None:
    folders: list[Path | None] = []
    extension = Extension()

    @extension.tool
    async def touch(ctx: ToolContext) -> str:
        """Use both folders."""
        folders.extend((ctx.data_dir, ctx.session_data_dir))
        return ""

    session = tmp_path / "session"
    tools = _bound(extension, tmp_path, session_dir=session)
    data = get_platform().config_dir / "extension-data" / "demo"
    assert not data.exists()
    assert not (session / "extension-data").exists()
    await _call(tools["touch"])
    assert folders == [data, session / "extension-data" / "demo"]
    if sys.platform != "win32":
        assert {stat.S_IMODE(folder.stat().st_mode) for folder in folders if folder} == {0o700}


async def test_without_a_session_folder_a_tool_has_no_session_data_dir(tmp_path: Path) -> None:
    extension = Extension()

    @extension.tool
    async def where(ctx: ToolContext) -> str:
        """Report it."""
        return repr(ctx.session_data_dir)

    assert await _call(_bound(extension, tmp_path)["where"]) == "None"


async def test_a_tool_error_is_the_model_s_error_text_with_its_code_as_kind(tmp_path: Path) -> None:
    tools = _bound(acme_release_extension.extension, tmp_path)
    with _tool_metadata() as metadata:
        result = await _call(tools["acme_project_name"], path="missing.toml")
    assert result == "Error: Missing or invalid project name."
    assert metadata[TOOL_FAILED_METADATA_KEY] is True
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "invalid_project"


async def test_another_exception_shows_the_model_only_that_the_tool_failed(tmp_path: Path) -> None:
    extension = Extension()

    @extension.tool
    async def leaky() -> str:
        """Fail with a secret."""
        raise RuntimeError("token=sk-secret")

    (tool,) = _bound(extension, tmp_path).values()
    with pytest.raises(RuntimeError, match="sk-secret"):
        await _call(tool)


@pytest.mark.parametrize(
    ("value", "text"),
    [("plain", "plain"), (None, ""), ({"name": "café", "n": [1, 2]}, '{"name": "café", "n": [1, 2]}'), (3, "3")],
    ids=["str", "none", "json", "number"],
)
async def test_results_reach_the_model_as_text(tmp_path: Path, value: Any, text: str) -> None:
    extension = Extension()

    @extension.tool
    async def give() -> Any:
        """Return the value."""
        return value

    assert await _call(_bound(extension, tmp_path)["give"]) == text


async def test_a_long_result_is_cut_to_its_bound_and_saved_whole(tmp_path: Path) -> None:
    extension = Extension()

    @extension.tool(max_result_tokens=200)
    async def long() -> str:
        """Return a lot."""
        return _WORDS

    session = tmp_path / "session"
    result = await _call(_bound(extension, tmp_path, session_dir=session)["long"])
    assert len(result) < len(_WORDS) // 4
    (spill,) = (session / TOOL_RESULTS_DIR_NAME).iterdir()
    assert spill.name.startswith("extension_")
    assert spill.read_text(encoding="utf-8") == _WORDS
    assert str(spill.resolve()) in result or spill.name in result


async def test_a_long_tool_error_is_cut_to_the_tool_s_bound_too(tmp_path: Path) -> None:
    extension = Extension()

    @extension.tool(max_result_tokens=200)
    async def fail() -> str:
        """Fail with a long diagnosis."""
        raise ToolError(_WORDS, code="server_said")

    session = tmp_path / "session"
    with _tool_metadata() as metadata:
        result = await _call(_bound(extension, tmp_path, session_dir=session)["fail"])
    assert result.startswith("Error: word0 ")
    assert len(result) < len(_WORDS) // 4
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "server_said"
    (spill,) = (session / TOOL_RESULTS_DIR_NAME).iterdir()
    assert spill.read_text(encoding="utf-8") == f"Error: {_WORDS}"


async def test_a_lone_surrogate_in_a_result_reaches_the_model_escaped(tmp_path: Path) -> None:
    # What os.fsdecode gives for a POSIX file name that is not UTF-8.
    name = "bad\udcff.txt"
    extension = Extension()

    @extension.tool
    async def as_text() -> str:
        """Name it."""
        return name

    @extension.tool
    async def as_data() -> dict[str, str]:
        """Name it in an object."""
        return {"path": name}

    tools = _bound(extension, tmp_path)
    text = await _call(tools["as_text"])
    data = await _call(tools["as_data"])
    for result in (text, data):
        result.encode("utf-8")
    assert text == "bad\\udcff.txt"
    assert json.loads(data) == {"path": name}


async def test_a_zero_bound_keeps_the_whole_result(tmp_path: Path) -> None:
    extension = Extension()

    @extension.tool(max_result_tokens=0)
    async def long() -> str:
        """Return a lot."""
        return _WORDS

    assert await _call(_bound(extension, tmp_path, session_dir=tmp_path / "session")["long"]) == _WORDS
    assert not (tmp_path / "session" / TOOL_RESULTS_DIR_NAME).exists()


async def test_the_example_extension_runs_bound_as_it_does_under_its_author_s_tests(tmp_path: Path) -> None:
    tools = _bound(acme_release_extension.extension, tmp_path, session_dir=tmp_path / "session")
    first = {"title": "One", "tags": []}
    assert json.loads(await _call(tools["acme_add_release_note"], note=first)) == {
        "notes": 1,
        "last": first,
        "agent": "Coder",
    }
    notes = tmp_path / "session" / "extension-data" / "demo" / "notes.json"
    assert json.loads(notes.read_text(encoding="utf-8")) == [first]
