# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The tool-pack catalog and the one assembly every agent build uses."""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.models.workspace import WorkingDir
from chrys.foundation.tool_kinds import KIND_ASK_USER, get_tool_kind
from chrys.service.profiles.agents.schema import ALWAYS_LOADED_TOOL_CATEGORIES, ToolsConfig
from chrys.service.tools import packs as tool_packs
from chrys.service.tools.builtins.doc_converter import DocConverterTools
from chrys.service.tools.names import chrys_reserved_tool_names

_EVERY_SWITCHABLE = [
    "filesystem.read",
    "filesystem.write",
    "search",
    "shell",
    "ask_user",
    "doc_converter",
    "todo",
]


def _build(
    builtins: list[str],
    *,
    settings: Settings | None = None,
    vision: bool = False,
    allow_user_interaction: bool = True,
    session_dir: Path | None = None,
) -> tool_packs.ToolBuild:
    return tool_packs.ToolBuild(
        tools=ToolsConfig(builtins=builtins),
        agent_name="test",
        runtime=SessionEnvironment.capture(),
        settings=settings or Settings(),
        session_id="test",
        session_dir=session_dir,
        vision=vision,
        allow_user_interaction=allow_user_interaction,
    )


async def _names(builtins: list[str], *, vision: bool = False) -> list[str]:
    return [tool.name for tool in (await tool_packs.assemble_tools(_build(builtins, vision=vision))).tools]


def test_catalog_ids_are_unique_and_cover_the_always_loaded_packs() -> None:
    ids = [pack.id for pack in tool_packs.BUILTIN_PACKS]
    assert len(ids) == len(set(ids))
    assert set(ids) == tool_packs.BUILTIN_PACK_IDS
    assert set(ALWAYS_LOADED_TOOL_CATEGORIES) <= tool_packs.BUILTIN_PACK_IDS
    # Only the always-loaded packs go without an editor switch.
    assert {pack.id for pack in tool_packs.BUILTIN_PACKS if pack.title is None} == set(ALWAYS_LOADED_TOOL_CATEGORIES)
    assert all(pack.description is not None for pack in tool_packs.BUILTIN_PACKS if pack.title is not None)


@pytest.mark.parametrize(
    ("builtins", "expected"),
    [
        (["filesystem.write"], ["write_file", "edit_file", "sleep"]),
        (["filesystem.read"], ["read_file", "view_image", "sleep"]),
        (["search"], ["grep", "glob", "sleep"]),
        (["ask_user"], ["ask_user", "sleep"]),
        (["todo"], ["todo_write", "sleep"]),
        (["filesystem.read", "search", "ask_user"], ["read_file", "view_image", "grep", "glob", "ask_user", "sleep"]),
    ],
)
async def test_each_pack_contributes_its_tools_in_profile_order(builtins: list[str], expected: list[str]) -> None:
    assert await _names(builtins, vision=True) == expected


async def test_shell_pack_adds_one_tool_per_shell() -> None:
    runtime = SessionEnvironment.capture()
    tools = (await tool_packs.assemble_tools(_build(["shell"]))).tools
    # The platform shell, any extra one (Git Bash on Windows), then sleep.
    assert len(tools) == 2 + len(runtime.platform.extra_shells)


async def test_extra_shells_share_one_workspace_listing(tmp_path: Path) -> None:
    # Every shell offers list_workspace_dirs, which lists the same folders whatever the shell;
    # two tools of one name would stop the agent from starting.
    runtime = SessionEnvironment.capture()
    platform_shell = runtime.platform.shell.name
    git_bash = replace(runtime.platform.shell, name="bash" if platform_shell != "bash" else "zsh", path="/git/bash")
    runtime = replace(
        runtime,
        platform=replace(runtime.platform, extra_shells=(git_bash,)),
        working_dirs=(WorkingDir(path=str(tmp_path), is_primary=True),),
    )
    tools = (await tool_packs.assemble_tools(replace(_build(["shell"]), runtime=runtime))).tools
    assert [tool.name for tool in tools] == [platform_shell, "list_workspace_dirs", git_bash.name, "sleep"]


async def test_reserved_chrys_names_cover_every_builtin_tool() -> None:
    tools = (await tool_packs.assemble_tools(_build(_EVERY_SWITCHABLE, vision=True))).tools
    assert {tool.name for tool in tools} <= chrys_reserved_tool_names()


async def test_by_pack_lists_each_contributing_pack_in_load_order() -> None:
    assembled = await tool_packs.assemble_tools(_build(["todo", "filesystem.read"]))
    # A text-only model drops view_image; the pack still lists what it contributed.
    assert assembled.by_pack == {"todo": ["todo_write"], "filesystem.read": ["read_file"], "sleep": ["sleep"]}


async def test_unknown_pack_loads_nothing_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        names = await _names(["nonexistent"])
    assert names == ["sleep"]
    assert "nonexistent" in caplog.text


@pytest.mark.parametrize(
    "builtins",
    [[], ["todo"], ["sleep"], ["sleep", "todo", "sleep"], ["todo", "todo"]],
    ids=["none", "omitted", "listed", "listed-twice", "repeated"],
)
async def test_each_pack_loads_once_and_sleep_last(builtins: list[str], caplog: pytest.LogCaptureFixture) -> None:
    """Every agent gets sleep whatever its packs say; a listed one (older profiles) changes nothing."""
    with caplog.at_level(logging.WARNING):
        names = await _names(builtins)
    assert names == (["todo_write", "sleep"] if "todo" in builtins else ["sleep"])
    assert caplog.records == []


async def test_text_only_model_gets_no_image_reading_tool() -> None:
    assert "view_image" in await _names(["filesystem.read"], vision=True)
    assert "view_image" not in await _names(["filesystem.read"], vision=False)


async def test_ask_user_is_left_out_when_nobody_can_answer() -> None:
    tools = (await tool_packs.assemble_tools(_build(["ask_user", "todo"], allow_user_interaction=False))).tools
    assert [tool.name for tool in tools] == ["todo_write", "sleep"]
    assert all(get_tool_kind(tool) != KIND_ASK_USER for tool in tools)


async def test_runtime_bound_filesystem_and_search_tools_preserve_tool_schema() -> None:
    """Instance tools preserve function schemas, with search descriptions reflecting runtime settings."""
    from chrys.service.tools.builtins.filesystem import edit_file, read_file, view_image, write_file
    from chrys.service.tools.builtins.search import glob, grep

    tools = (
        await tool_packs.assemble_tools(_build(["filesystem.read", "filesystem.write", "search"], vision=True))
    ).tools
    loaded = {tool.name: tool for tool in tools}

    originals = {
        "read_file": read_file,
        "view_image": view_image,
        "write_file": write_file,
        "edit_file": edit_file,
        "grep": grep,
        "glob": glob,
    }
    for name, original in originals.items():
        generated = loaded[name]
        expected_schema = original.to_json_schema_spec()
        if name in {"grep", "glob"}:
            assert "Respect .gitignore is enabled" in generated.description
            expected_schema["function"]["description"] = generated.description
        assert generated.to_json_schema_spec() == expected_schema


@pytest.mark.parametrize(
    ("builtins", "vision", "expected"),
    [
        (["doc_converter", "filesystem.read"], True, True),
        (["filesystem.read", "doc_converter"], True, True),
        (["doc_converter", "filesystem.read"], False, False),
        (["doc_converter"], True, False),
    ],
    ids=["after-read", "before-read", "text-only", "no-view-image"],
)
async def test_doc_converter_extracts_images_only_for_an_agent_that_can_view_them(
    builtins: list[str], vision: bool, expected: bool, tmp_path: Path
) -> None:
    with patch.object(DocConverterTools, "set_image_extraction_enabled", autospec=True) as enable:
        await tool_packs.assemble_tools(_build(builtins, vision=vision, session_dir=tmp_path))
    assert [call.args[1] for call in enable.call_args_list] == [expected]
