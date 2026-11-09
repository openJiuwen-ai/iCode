# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A tool group that made one call, which succeeded, says what that call did; any other group keeps its counts."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from rich.style import Style
from rich.text import Text
from textual.message import Message

from chrys.app.tui.util.paths import middle_cut_path
from chrys.app.tui.widgets.chat.fold_rows import FoldRow, FoldRowStyles, fit_fold_row
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.single_call_row import SingleCallRow, single_call_row
from chrys.app.tui.widgets.chat.tool_call import ToolGroup, ToolGroupTitle
from chrys.foundation.hosted_tools import HostedToolFamily
from chrys.foundation.i18n import Localizer
from chrys.foundation.tool_kinds import (
    KIND_ASK_USER,
    KIND_CONTEXT,
    KIND_DOC_CONVERTER,
    KIND_FILESYSTEM_READ,
    KIND_FILESYSTEM_WRITE,
    KIND_MCP,
    KIND_SEARCH,
    KIND_SHELL,
    KIND_SKILL,
    KIND_SLEEP,
    KIND_SUB_AGENT,
    KIND_TODO,
    KIND_WEB_FETCH,
    KIND_WEB_SEARCH,
    TOOL_CALL_KIND_METADATA_KEY,
)
from tests.support.tui_helpers import ChatPanelApp, resize_when_settled
from tests.support.waiting import wait_for

# Absolute on every OS: a Windows path needs its drive.
_ROOT = Path(Path(__file__).anchor)
_CWD = str(_ROOT / "work" / "repo")
_INSIDE = str(Path(_CWD, "src", "app.py"))
_OUTSIDE = str(_ROOT / "etc" / "hosts")
_RELATIVE = str(Path("src", "app.py"))
_DEEP = str(Path(_CWD, "src", "chrys", "app", "tui", "app.py"))
_DEEP_RELATIVE = str(Path("src", "chrys", "app", "tui", "app.py"))
# Lies under the folder as written, yet climbs out of it.
_CLIMBING_OUT = str(Path(_CWD, "src", "..", "..", "etc", "hosts"))

# (tool name, kind, arguments, hosted family): English row, Chinese row.
_ROWS: dict[str, tuple[tuple[str, str, dict[str, Any], str], str, str]] = {
    "read-inside": (
        ("read_file", KIND_FILESYSTEM_READ, {"path": _INSIDE}, ""),
        f"Read {_RELATIVE}",
        f"读取 {_RELATIVE}",
    ),
    "read-outside": (
        ("read_file", KIND_FILESYSTEM_READ, {"path": _OUTSIDE}, ""),
        f"Read {_OUTSIDE}",
        f"读取 {_OUTSIDE}",
    ),
    "read-relative": (("read_file", KIND_FILESYSTEM_READ, {"path": "notes.md"}, ""), "Read notes.md", "读取 notes.md"),
    "read-climbing-out": (
        ("read_file", KIND_FILESYSTEM_READ, {"path": _CLIMBING_OUT}, ""),
        f"Read {_CLIMBING_OUT}",
        f"读取 {_CLIMBING_OUT}",
    ),
    "view-image": (
        ("view_image", KIND_FILESYSTEM_READ, {"path": "shot.png"}, ""),
        "Viewed image shot.png",
        "查看图片 shot.png",
    ),
    "document": (
        ("convert_document", KIND_DOC_CONVERTER, {"path": "spec.pdf"}, ""),
        "Read document spec.pdf",
        "读取文档 spec.pdf",
    ),
    "grep": (("grep", KIND_SEARCH, {"pattern": "TODO"}, ""), "Searched for TODO", "搜索 TODO"),
    "grep-in": (
        ("grep", KIND_SEARCH, {"pattern": "TODO", "path": str(Path(_CWD, "src"))}, ""),
        "Searched for TODO in src",
        "搜索 TODO（在 src 中）",  # noqa: RUF001
    ),
    "grep-in-the-working-folder": (
        ("grep", KIND_SEARCH, {"pattern": "TODO", "path": _CWD}, ""),
        "Searched for TODO",
        "搜索 TODO",
    ),
    "glob": (("glob", KIND_SEARCH, {"pattern": "**/*.py"}, ""), "Found files matching **/*.py", "查找文件 **/*.py"),
    "edit": (("edit_file", KIND_FILESYSTEM_WRITE, {"path": _INSIDE}, ""), f"Edited {_RELATIVE}", f"编辑 {_RELATIVE}"),
    "write": (("write_file", KIND_FILESYSTEM_WRITE, {"path": "out.txt"}, ""), "Wrote out.txt", "写入 out.txt"),
    # Another agent's delete, titled as it likes, reads as the counts read it.
    "edit-by-another-agent": (
        ("Delete notes.md", KIND_FILESYSTEM_WRITE, {"path": "notes.md"}, ""),
        "Edited notes.md",
        "编辑 notes.md",
    ),
    "shell-reason": (
        ("zsh", KIND_SHELL, {"command": "git status", "reason": "Check the working tree"}, ""),
        "Ran zsh command: Check the working tree",
        "运行 zsh 命令：Check the working tree",  # noqa: RUF001
    ),
    "shell-command": (
        ("pwsh", KIND_SHELL, {"command": "Get-ChildItem\nGet-Date"}, ""),
        "Ran pwsh command: Get-ChildItem …",
        "运行 pwsh 命令：Get-ChildItem …",  # noqa: RUF001
    ),
    "git-bash": (("git_bash", KIND_SHELL, {"command": "ls"}, ""), "Ran Git Bash command: ls", "运行 Git Bash 命令：ls"),  # noqa: RUF001
    "shell-named-by-title": (
        ("Run `ls -la`", KIND_SHELL, {"command": "ls -la"}, ""),
        "Ran command: ls -la",
        "运行命令：ls -la",  # noqa: RUF001
    ),
    "shell-legacy-execute": (("execute", KIND_SHELL, {"command": "ls"}, ""), "Ran command: ls", "运行命令：ls"),  # noqa: RUF001
    # Another agent's shell call titled like a name, which names no shell.
    "shell-named-like-a-name": (
        ("run_command", KIND_SHELL, {"command": "npm test"}, ""),
        "Ran command: npm test",
        "运行命令：npm test",  # noqa: RUF001
    ),
    "hosted-shell": (("shell", "", {"command": "ls"}, HostedToolFamily.SHELL), "Ran command: ls", "运行命令：ls"),  # noqa: RUF001
    "web-search": (
        ("web_search", KIND_WEB_SEARCH, {"query": "textual 8.2 changelog"}, ""),
        "Searched the web: textual 8.2 changelog",
        "网页搜索：textual 8.2 changelog",  # noqa: RUF001
    ),
    "fetch": (
        ("web_fetch", KIND_WEB_FETCH, {"url": "https://docs.python.org/3/library/re.html?highlight=x#syntax"}, ""),
        "Fetched docs.python.org/3/library/re.html",
        "抓取 docs.python.org/3/library/re.html",
    ),
    "fetch-with-credentials": (
        ("web_fetch", KIND_WEB_FETCH, {"url": "https://me:secret@example.com/docs?token=x"}, ""),
        "Fetched example.com/docs",
        "抓取 example.com/docs",
    ),
    "agent": (
        ("Explore", KIND_SUB_AGENT, {"agent_name": "Explore Agent", "prompt": "look"}, ""),
        "Ran agent Explore Agent",
        "运行子智能体 Explore Agent",
    ),
    "load-skill": (
        ("load_skill", KIND_SKILL, {"skill_name": "pdf"}, ""),
        "Loaded skill pdf",
        "加载 Skill pdf",
    ),
    "skill-resource": (
        ("read_skill_resource", KIND_SKILL, {"skill_name": "pdf", "resource_name": "forms.md"}, ""),
        "Read skill resource pdf/forms.md",
        "读取 Skill 资源 pdf/forms.md",
    ),
    "skill-script": (
        ("run_skill_script", KIND_SKILL, {"skill_name": "pdf", "script_name": "fill.py"}, ""),
        "Ran skill script pdf/fill.py",
        "执行 Skill 脚本 pdf/fill.py",
    ),
    "skill-resource-without-its-skill": (
        ("read_skill_resource", KIND_SKILL, {"resource_name": "forms.md"}, ""),
        "Read skill resource forms.md",
        "读取 Skill 资源 forms.md",
    ),
    "skill-script-without-its-name": (
        ("run_skill_script", KIND_SKILL, {"skill_name": "pdf"}, ""),
        "Ran skill script pdf",
        "执行 Skill 脚本 pdf",
    ),
    "mcp": (
        ("create_issue", KIND_MCP, {"title": "x"}, ""),
        "Called MCP tool create_issue",
        "调用 MCP 工具 create_issue",
    ),
    "question": (
        ("ask_user", KIND_ASK_USER, {"questions": [{"question": "Which library?", "header": "Library"}]}, ""),
        "Asked you: Which library?",
        "向你提问：Which library?",  # noqa: RUF001
    ),
    "question-with-only-a-header": (
        ("ask_user", KIND_ASK_USER, {"questions": [{"header": "Library"}]}, ""),
        "Asked you: Library",
        "向你提问：Library",  # noqa: RUF001
    ),
    "questions": (
        ("ask_user", KIND_ASK_USER, {"questions": [{"question": "A?"}, {"question": "B?"}]}, ""),
        "Asked you 2 questions",
        "向你提了 2 个问题",
    ),
    "todo": (("todo_write", KIND_TODO, {"todos": []}, ""), "Updated the todo list", "更新待办列表"),
    "sleep": (
        ("sleep", KIND_SLEEP, {"seconds": 30, "reason": "Wait for the build"}, ""),
        "Slept 30s: Wait for the build",
        "休眠了 30 秒：Wait for the build",  # noqa: RUF001
    ),
    "other": (("recall_context", KIND_CONTEXT, {}, ""), "Called recall_context", "调用 recall_context"),
}


@pytest.mark.parametrize("case", list(_ROWS))
def test_one_call_reads_as_what_it_did_in_each_language(case: str) -> None:
    (tool_name, kind, args, family), english, chinese = _ROWS[case]
    for language, expected in (("en", english), ("zh-Hans", chinese)):
        row = single_call_row(
            Localizer(language), tool_name, kind, args, hosted_family=family, slept_ms=30_000, workspace_cwd=_CWD
        )
        assert row is not None
        assert row.words == expected


def test_a_call_s_path_is_the_part_a_narrow_row_cuts() -> None:
    english = Localizer("en")
    read = single_call_row(english, "read_file", KIND_FILESYSTEM_READ, {"path": _INSIDE}, workspace_cwd=_CWD)
    assert read == SingleCallRow(f"Read {_RELATIVE}", _RELATIVE)
    # A pattern or a command is no path: the row cuts its end.
    grep = single_call_row(english, "grep", KIND_SEARCH, {"pattern": "a/b/c", "path": "lib"})
    assert grep == SingleCallRow("Searched for a/b/c in lib")


@pytest.mark.parametrize(
    ("tool_name", "kind", "args", "family"),
    [
        ("read_file", KIND_FILESYSTEM_READ, {}, ""),
        ("read_file", KIND_FILESYSTEM_READ, {"path": _CWD}, ""),
        ("edit_file", KIND_FILESYSTEM_WRITE, {"path": 42}, ""),
        ("grep", KIND_SEARCH, {"path": "src"}, ""),
        ("zsh", KIND_SHELL, {"command": "  "}, ""),
        ("web_search", KIND_WEB_SEARCH, {}, ""),
        ("web_fetch", KIND_WEB_FETCH, {"url": ""}, ""),
        ("web_fetch", KIND_WEB_FETCH, {"url": "example.com/a?token=secret"}, ""),
        ("web_fetch", KIND_WEB_FETCH, {"url": "https://[::1/a?token=secret"}, ""),
        ("load_skill", KIND_SKILL, {}, ""),
        ("ask_user", KIND_ASK_USER, {"questions": []}, ""),
        ("sleep", KIND_SLEEP, {"seconds": 5}, ""),
        ("code_interpreter", "", {"code": "1"}, HostedToolFamily.CODE),
        (HostedToolFamily.MCP.value, "", {}, HostedToolFamily.MCP),
    ],
    ids=[
        "read-without-a-path",
        "read-of-the-working-folder",
        "edit-with-no-text-path",
        "grep-without-a-pattern",
        "blank-command",
        "web-search-without-a-query",
        "fetch-without-a-url",
        "fetch-without-a-host",
        "fetch-of-no-readable-url",
        "skill-without-a-name",
        "no-questions",
        "sleep-without-a-reason",
        "hosted-code",
        "hosted-mcp-without-a-name",
    ],
)
def test_a_call_that_says_too_little_keeps_the_group_s_count(
    tool_name: str, kind: str, args: dict[str, Any], family: str
) -> None:
    assert single_call_row(Localizer("en"), tool_name, kind, args, hosted_family=family, workspace_cwd=_CWD) is None


@pytest.mark.parametrize(
    ("path", "cells", "shown"),
    [
        ("src/chrys/app/tui/app.py", 30, "src/chrys/app/tui/app.py"),
        ("src/chrys/app/tui/app.py", 22, "src/chrys/…/tui/app.py"),
        ("src/chrys/app/tui/app.py", 14, "src/…/app.py"),
        ("src/chrys/app/tui/app.py", 9, "…/app.py"),
        ("src/chrys/app/tui/app.py", 5, "…p.py"),
        ("/home/me/repo/src/main.py", 18, "/…/src/main.py"),
        ("C:\\Users\\me\\proj\\main.py", 18, "C:\\…\\proj\\main.py"),
        ("a_very_long_file_name.py", 12, "…ile_name.py"),
        # A file name keeps its spaces as written: they tell names apart.
        ("folder/my  file.py", 11, "…y  file.py"),
        ("folder/my  long  file.py", 10, "…  file.py"),
        # Where the name alone fits, ``…`` still tells of the folders cut off; with no room for it, nothing shows.
        ("/etc/nginx/sites/app.py", 7, "…app.py"),
        ("src/app.py", 1, "…"),
        ("src/app.py", 0, ""),
        # One end's next folder too long, the other end's still join.
        ("averyveryverylongrootfolder/a/b/c.py", 12, "…/a/b/c.py"),
        ("/Users/someone/projects/a/b/c.py", 14, "/…/a/b/c.py"),
    ],
)
def test_a_cut_path_keeps_its_file_name_and_folders_from_either_end(path: str, cells: int, shown: str) -> None:
    cut = middle_cut_path(path, cells)
    assert cut == shown
    assert Text(cut).cell_len <= cells


def test_a_narrow_row_cuts_its_path_in_the_middle_before_its_end() -> None:
    styles = FoldRowStyles(detail=Style(dim=True), failed=Style(color="red"), interrupted=Style(color="yellow"))
    row = FoldRow("▸ ", ("Read src/chrys/app/tui/app.py",), subject="src/chrys/app/tui/app.py")
    assert fit_fold_row(row, 80, styles).plain == "▸ Read src/chrys/app/tui/app.py"
    assert fit_fold_row(row, 29, styles).plain == "▸ Read src/chrys/…/tui/app.py"
    # Too narrow for any folder, then for the file name: the row's end goes last.
    assert fit_fold_row(row, 15, styles).plain == "▸ Read …/app.py"
    assert fit_fold_row(row, 8, styles).plain == "▸ Read …"
    # No room for even ``…`` in the path: the row's end goes, its ``…`` with it.
    assert fit_fold_row(row, 7, styles).plain == "▸ Read…"
    assert fit_fold_row(row, 6, styles).plain == "▸ Rea…"
    # The words read in the detail's style, the path included.
    assert [span.style for span in fit_fold_row(row, 29, styles).spans] == [Style(dim=True)]


async def test_a_group_s_one_succeeded_call_names_what_it_did_and_any_other_group_counts() -> None:
    async with ChatPanelApp().run_test(size=(100, 24)) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        cp.set_workspace_cwd(_CWD)

        async def group_of(*calls: tuple[str, str, str]) -> ToolGroupTitle:
            await cp.add_user_message("prompt")
            for call_id, _result, path in calls:
                await cp.add_tool_start(call_id, "read_file", KIND_FILESYSTEM_READ, args={"path": path})
            group = cp._tool_groups_by_call_id[calls[0][0]]
            for call_id, result, _path in calls:
                await cp.add_tool_result(call_id, "read_file", result)
            await cp.add_agent_message("done")
            return group.query_one(ToolGroupTitle)

        one = await group_of(("a", "ok", _DEEP))
        failed = await group_of(("b", "Error: no such file", _INSIDE))
        two = await group_of(("c", "ok", _INSIDE), ("d", "ok", _OUTSIDE))
        await wait_for(
            lambda: (
                [title.render().plain[2:] for title in (one, failed, two)]
                == [f"Read {_DEEP_RELATIVE}", "Read 1 file · 1 failed", "Read 2 files"]
            ),
            pilot=pilot,
            description="each group's row",
        )
        # One colour for the whole sentence, as the counts have.
        detail = one.get_component_rich_style("toolgrouptitle--detail").color
        assert {span.style.color for span in one.render().spans if isinstance(span.style, Style)} == {detail}

        # The chat moves to another folder: the path no longer lies inside it, so it reads as the call gave it.
        cp.set_workspace_cwd(str(_ROOT / "elsewhere"))
        await wait_for(lambda: one.render().plain[2:] == f"Read {_DEEP}", pilot=pilot, description="the path anew")

        # Narrow, the path gives up folders from its middle and keeps its file name.
        await resize_when_settled(pilot, 22, 24)
        narrow = one.render().plain
        assert narrow.startswith("▸ Read ") and narrow.endswith(f"{os.sep}app.py") and "…" in narrow
        assert Text(narrow).cell_len <= one.content_size.width


async def test_an_interrupted_call_keeps_its_count() -> None:
    async with ChatPanelApp().run_test(size=(100, 24)) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("prompt")
        await cp.add_tool_start("s", "zsh", KIND_SHELL, args={"command": "sleep 60", "reason": "Wait"})
        title = cp._tool_groups_by_call_id["s"].query_one(ToolGroupTitle)
        await cp.add_interrupted()
        await wait_for(
            lambda: title.render().plain[2:] == "Ran 1 command · 1 interrupted", pilot=pilot, description="the row"
        )


async def test_a_replayed_group_s_one_call_names_its_path_in_the_chat_s_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    messages = [
        {"role": "user", "contents": [{"type": "text", "text": "read it"}]},
        {
            "role": "assistant",
            "contents": [
                {
                    "type": "function_call",
                    "call_id": "r",
                    "name": "read_file",
                    "arguments": {"path": _INSIDE},
                    "additional_properties": {TOOL_CALL_KIND_METADATA_KEY: KIND_FILESYSTEM_READ},
                }
            ],
        },
        {"role": "tool", "contents": [{"type": "function_result", "call_id": "r", "result": "1\tprint()"}]},
    ]
    async with ChatPanelApp().run_test(size=(100, 24)) as pilot:
        cp = pilot.app.query_one(ChatPanel)
        cp.set_workspace_cwd(_CWD)
        await cp.replay_history(messages)
        await cp.wait_replay_complete()
        group = cp.query_one(ToolGroup)
        title = group.query_one(ToolGroupTitle)
        await wait_for(
            lambda: title.render().plain[2:] == f"Read {_RELATIVE}", pilot=pilot, description="the replayed row"
        )

        posted: list[type[Message]] = []
        post = ToolGroup.post_message

        def recording(self: ToolGroup, message: Message) -> bool:
            posted.append(type(message))
            return post(self, message)

        monkeypatch.setattr(ToolGroup, "post_message", recording)
        # The chat moves to another folder: the row names the path anew, and the process block it belongs to hears
        # nothing, as none of its calls changed.
        cp.set_workspace_cwd(str(_ROOT / "elsewhere"))
        await wait_for(lambda: title.render().plain[2:] == f"Read {_INSIDE}", pilot=pilot, description="the path anew")
        assert ToolGroup.ProcessStateChanged not in posted
        # A change to its calls would have told it.
        group._update_title()
        assert ToolGroup.ProcessStateChanged in posted
