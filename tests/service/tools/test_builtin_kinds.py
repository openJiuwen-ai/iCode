# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression test — every chrys built-in tool is classified out of band.

Each builtin must carry its canonical kind on the chrys-owned channel
(``get_tool_kind``) while leaving ``FunctionTool.kind`` ``None``:
``chrys_kind`` is the one kind channel, so approval, the tool loop and tool
events all read the same value.  A new builtin that writes
``.kind`` (or ships unclassified) is caught here.
"""

from __future__ import annotations

from chrys.foundation.tool_kinds import (
    KIND_ASK_USER,
    KIND_DOC_CONVERTER,
    KIND_FILESYSTEM_READ,
    KIND_FILESYSTEM_WRITE,
    KIND_SEARCH,
    KIND_SHELL,
    KIND_SLEEP,
    KIND_TODO,
    TOOL_KINDS,
    get_tool_kind,
)
from chrys.service.tools.builtins.ask_user import ask_user
from chrys.service.tools.builtins.filesystem import edit_file, read_file, view_image, write_file
from chrys.service.tools.builtins.search import glob, grep
from chrys.service.tools.builtins.sleep import sleep
from chrys.service.tools.builtins.todo import TodoTools
from chrys.service.tools.builtins.web.fetch.tool import WebFetchTools
from chrys.service.tools.builtins.web.search.tool import WebSearchTools


def test_static_builtin_tools_use_out_of_band_kinds() -> None:
    """Each module-level builtin carries its canonical kind, with ``.kind`` left None."""
    tools = {
        "read_file": (read_file, KIND_FILESYSTEM_READ),
        "view_image": (view_image, KIND_FILESYSTEM_READ),
        "write_file": (write_file, KIND_FILESYSTEM_WRITE),
        "edit_file": (edit_file, KIND_FILESYSTEM_WRITE),
        "grep": (grep, KIND_SEARCH),
        "glob": (glob, KIND_SEARCH),
        "ask_user": (ask_user, KIND_ASK_USER),
        "sleep": (sleep, KIND_SLEEP),
        "todo_write": (TodoTools.todo_write, KIND_TODO),
        "web_search": (WebSearchTools.web_search, "web_search"),
        "web_fetch": (WebFetchTools.web_fetch, "web_fetch"),
    }
    for name, (tool, expected) in tools.items():
        assert get_tool_kind(tool) == expected, f"{name}: kind={get_tool_kind(tool)!r} expected {expected!r}"
        assert tool.kind is None, (
            f"{name}: FunctionTool.kind={tool.kind!r} must stay None — "
            "chrys kinds ride only on chrys_kind (set_tool_kind)"
        )


def test_shell_tool_uses_out_of_band_kind(tmp_path) -> None:
    """``ShellTools.tools()`` classifies every tool as ``shell`` without touching ``.kind``."""
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.service.tools.builtins.shell import ShellTools

    runtime = SessionEnvironment.capture()
    shell = ShellTools(runtime)
    tools = shell.tools()
    assert tools, "ShellTools should produce at least one tool"
    for t in tools:
        assert get_tool_kind(t) == KIND_SHELL, f"shell tool {t.name!r} kind={get_tool_kind(t)!r}"
        assert t.kind is None, (
            f"shell tool {t.name!r} has FunctionTool.kind={t.kind!r} — "
            "chrys kinds ride only on chrys_kind (set_tool_kind)"
        )


def test_doc_converter_tool_uses_out_of_band_kind(tmp_path) -> None:
    """``DocConverterTools.tools()`` produces tools classified as ``doc_converter``."""
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.service.tools.builtins.doc_converter import DocConverterTools

    runtime = SessionEnvironment.capture()
    dc = DocConverterTools(runtime, session_id="test")
    tools = dc.tools()
    assert tools, "DocConverterTools should produce at least one tool"
    for t in tools:
        assert get_tool_kind(t) == KIND_DOC_CONVERTER
        assert t.kind is None


async def test_assembled_builtins_all_classified_and_kindless(tmp_path) -> None:
    """End-to-end: assemble every first-party pack and verify every emitted
    tool carries a canonical chrys kind out of band while ``FunctionTool.kind``
    stays None.

    A new builtin pack that writes ``.kind`` or ships an unregistered kind
    value is caught here.
    """
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.models.session_env import SessionEnvironment
    from chrys.service.profiles.agents.schema import ToolsConfig
    from chrys.service.tools.packs import BUILTIN_PACK_IDS, ToolBuild, assemble_tools

    assembled = await assemble_tools(
        ToolBuild(
            tools=ToolsConfig(builtins=sorted(BUILTIN_PACK_IDS)),
            agent_name="test",
            runtime=SessionEnvironment.capture(),
            settings=Settings(),
            session_id="test",
            vision=True,
        )
    )
    tools = assembled.tools
    assert tools, "assembly should produce tools for these packs"
    unclassified = [t.name for t in tools if get_tool_kind(t) not in TOOL_KINDS]
    assert not unclassified, (
        f"Built-in tools without a canonical chrys kind: {unclassified}. "
        "Use a constant from chrys.foundation.tool_kinds (or add a new one)."
    )
    leaked = [(t.name, t.kind) for t in tools if getattr(t, "kind", None) is not None]
    assert not leaked, (
        f"Built-in tools writing FunctionTool.kind: {leaked}. "
        "Use chrys.foundation.tool_kinds.set_tool_kind / the chrys @tool wrapper instead — "
        "chrys_kind is the one kind channel."
    )
