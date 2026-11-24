# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Synthetic long chat transcripts for the chat panel profiler and the GC freeze calibration.

Each workload drives a ``ChatPanel`` through the calls a live turn makes:
user messages, answer text streamed as segments (text a tool call follows
settles as intermediate text), tool starts and results, and the completed
answer. Importing it applies no patches: the scripts that measure apply
Chrys's Textual patches (``apply_all``) before they import it, and a test
imports it without rewriting the installed Textual.
"""

from __future__ import annotations

import asyncio

from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
from chrys.app.tui.widgets.chat.tool_renderers import register_kind_renderer
from chrys.service.tools.kinds import (
    KIND_FILESYSTEM_READ,
    KIND_FILESYSTEM_WRITE,
    KIND_SEARCH,
    KIND_SHELL,
    KIND_SUB_AGENT,
)


def _sample_file(name: str, turn: int, tool_index: int, *, changed: bool) -> str:
    lines = [
        f"# {name}",
        "",
        "from __future__ import annotations",
        "",
    ]
    for idx in range(36):
        value = turn * 1000 + tool_index * 100 + idx
        if changed and idx in {4, 12, 20, 28}:
            lines.append(f"VALUE_{idx} = {value + 17}")
            lines.append(f"def changed_{idx}() -> str:")
            lines.append(f"    return 'turn={turn} tool={tool_index} idx={idx}'")
        else:
            lines.append(f"VALUE_{idx} = {value}")
            lines.append(f"def function_{idx}() -> int:")
            lines.append(f"    return VALUE_{idx}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _read_file_result(path: str, turn: int, tool_index: int) -> str:
    body = _sample_file(path, turn, tool_index, changed=False).splitlines()
    preview = body[:18]
    lines = [f"File: {path} ({len(body)} lines, {sum(len(line) for line in body)} chars)"]
    lines.extend(f"{idx}|{line}" for idx, line in enumerate(preview, 1))
    lines.append("[Truncated: showing first 18 lines]")
    return "\n".join(lines)


def _large_markdown_result(turn: int, tool_index: int, target_lines: int) -> str:
    """Build a deterministic large markdown final answer for sub-agent cards."""
    lines = [
        f"# Sub-agent report {turn}-{tool_index}",
        "",
        "This synthetic result mixes headings, paragraphs, lists, block quotes, tables, and fenced code.",
        "",
    ]
    section = 0
    while len(lines) < target_lines:
        lines.extend(
            [
                f"## Finding {section}",
                "",
                (
                    "The renderer should handle a long markdown answer without making chat-panel "
                    f"scrolling scale with every offscreen row. This paragraph belongs to turn {turn}, "
                    f"tool {tool_index}, section {section}."
                ),
                "",
                "- Observation: the answer includes enough structure to exercise markdown-it token parsing.",
                "- Evidence: repeated paragraphs and fenced blocks force layout and strip rendering paths.",
                "- Action: keep expensive rendering lazy when the containing tool group is collapsed.",
                "",
                "> A quoted note adds nested markdown block styling and wrapping work.",
                "",
                "| Metric | Value | Notes |",
                "| --- | ---: | --- |",
                f"| turn | {turn} | synthetic |",
                f"| tool | {tool_index} | synthetic |",
                f"| section | {section} | generated |",
                "",
                "```python",
            ]
        )
        for code_line in range(10):
            value = turn * 100_000 + tool_index * 1_000 + section * 10 + code_line
            lines.append(f"def measured_case_{section}_{code_line}() -> int:")
            lines.append(f"    return {value}")
            lines.append("")
        lines.extend(["```", ""])
        section += 1
    return "\n".join(lines) + "\n"


def _table_cell_text(turn: int, result_index: int, table_index: int, row: int, col: int, cell_words: int) -> str:
    """Build deterministic markdown-table cell text with controllable wrapping pressure."""
    tokens = [
        f"r{row}",
        f"c{col}",
        f"case-{turn}-{result_index}-{table_index}",
        f"value-{row * (col + 1)}",
    ]
    tokens.extend(f"wrap-{word_index}-{(row + 1) * (col + 1)}" for word_index in range(max(0, cell_words)))
    return " ".join(tokens)


def _table_markdown_result(
    turn: int,
    result_index: int,
    target_lines: int,
    *,
    table_columns: int,
    table_rows: int,
    cell_words: int,
) -> str:
    """Build markdown dominated by large tables for table virtualization profiling."""
    table_columns = max(1, table_columns)
    table_rows = max(1, table_rows)
    lines = [
        f"# Table-heavy report {turn}-{result_index}",
        "",
        (
            "This synthetic answer is dominated by markdown tables so the benchmark can isolate "
            "table layout, strip construction, and scroll rendering cost."
        ),
        "",
    ]
    table_index = 0
    while len(lines) < target_lines:
        lines.extend(
            [
                f"## Wide table {table_index}",
                "",
                "| " + " | ".join(f"Column {col}" for col in range(table_columns)) + " |",
                "| " + " | ".join("---" for _ in range(table_columns)) + " |",
            ]
        )
        for row in range(table_rows):
            cells = [
                _table_cell_text(turn, result_index, table_index, row, col, cell_words) for col in range(table_columns)
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
        table_index += 1
    return "\n".join(lines) + "\n"


def _mixed_markdown_result(
    turn: int,
    result_index: int,
    target_lines: int,
    *,
    fence_lines: int,
    table_columns: int,
    table_rows: int,
    cell_words: int,
) -> str:
    """Build a mixed normal agent response with long fences, quotes, and tables."""
    table_columns = max(1, table_columns)
    table_rows = max(1, table_rows)
    fence_lines = max(1, fence_lines)
    lines = [
        f"# Mixed markdown response {turn}-{result_index}",
        "",
        (
            "This synthetic answer mimics a long agent reply with dense prose, block quotes, "
            "wide tables, and long fenced code blocks interleaved throughout the response."
        ),
        "",
    ]
    section = 0
    while len(lines) < target_lines:
        lines.extend(
            [
                f"## Section {section}",
                "",
                (
                    "The scroll path should stay smooth when this response alternates between ordinary "
                    "paragraph wrapping, quoted notes, tabular status data, and code fences. "
                    f"This paragraph belongs to turn {turn}, message {result_index}, section {section}."
                ),
                "",
                "> A quoted diagnostic note exercises block quote border rendering and wrapped text.",
                ">",
                "> Nested-looking quoted content adds enough rows to expose per-line style overhead.",
                "",
                "| " + " | ".join(f"Column {col}" for col in range(table_columns)) + " |",
                "| " + " | ".join("---" for _ in range(table_columns)) + " |",
            ]
        )
        for row in range(table_rows):
            cells = [
                _table_cell_text(turn, result_index, section, row, col, cell_words) for col in range(table_columns)
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines.extend(["", "```python"])
        for code_line in range(fence_lines):
            value = turn * 1_000_000 + result_index * 100_000 + section * 1_000 + code_line
            lines.append(
                f"result_{section}_{code_line} = transform_input({value}, label='turn-{turn}-message-{result_index}')"
            )
            if code_line % 6 == 5:
                lines.append(
                    f"if result_{section}_{code_line} % 3 == 0: audit_trail.append(('section', {section}, {code_line}))"
                )
        lines.extend(
            [
                "```",
                "",
                "- Follow-up: retain row-level cache locality while scrolling.",
                "- Follow-up: avoid first-visible-block work on the UI thread.",
                "",
            ]
        )
        section += 1
    return "\n".join(lines) + "\n"


def _expand_all_groups(panel: ChatPanel) -> None:
    from chrys.app.tui.widgets.chat.process_block import ProcessBlock

    for process in panel.query(ProcessBlock):
        if process.collapsed:
            panel._toggle_process_block(process)
    for group in panel.query("ToolGroup"):
        group.collapsed = False


async def _stream_answer(panel: ChatPanel, segment_id: str, text: str) -> None:
    """Stream *text* as one answer segment; a tool call after it settles it as intermediate text."""
    await panel.add_answer_delta(segment_id, text)
    panel.finish_answer_segment(segment_id, completed=True)


async def _finish_turn(panel: ChatPanel, segment_id: str, text: str) -> None:
    """Stream *text* as the turn's answer and complete the turn."""
    await _stream_answer(panel, segment_id, text)
    await panel.add_agent_message(text)


async def _populate_file_diff(panel: ChatPanel, *, turns: int, tool_sets_per_turn: int, collapsed: bool) -> None:
    panel.set_tool_kinds(
        {
            "read_file": KIND_FILESYSTEM_READ,
            "write_file": KIND_FILESYSTEM_WRITE,
            "edit_file": KIND_FILESYSTEM_WRITE,
        }
    )
    await panel.add_user_message("Start synthetic long session with many file operations.")
    for turn in range(turns):
        await _stream_answer(panel, f"intro-{turn}", f"Turn {turn}: inspecting and editing files.")
        for tool_index in range(tool_sets_per_turn):
            path = f"src/pkg/module_{turn:02d}_{tool_index:02d}.py"

            read_id = f"read-{turn}-{tool_index}"
            await panel.add_tool_start(
                read_id,
                "read_file",
                KIND_FILESYSTEM_READ,
                args={"path": path},
            )
            await panel.add_tool_result(read_id, "read_file", _read_file_result(path, turn, tool_index), 11)

            write_id = f"write-{turn}-{tool_index}"
            write_after = _sample_file(path, turn, tool_index, changed=True)
            await panel.add_tool_start(
                write_id,
                "write_file",
                KIND_FILESYSTEM_WRITE,
                args={"path": path, "overwrite": True},
            )
            await panel.add_tool_result(
                write_id,
                "write_file",
                f"Wrote {path} ({len(write_after.splitlines())} lines)",
                17,
                file_snapshot=("", write_after),
            )

            edit_id = f"edit-{turn}-{tool_index}"
            before = _sample_file(path, turn, tool_index, changed=False)
            after = _sample_file(path, turn, tool_index, changed=True)
            await panel.add_tool_start(
                edit_id,
                "edit_file",
                KIND_FILESYSTEM_WRITE,
                args={"path": path},
            )
            await panel.add_tool_result(
                edit_id,
                "edit_file",
                f"Edited {path}",
                23,
                file_snapshot=(before, after),
            )

        await _finish_turn(panel, f"answer-{turn}", f"Turn {turn}: done.")
        if not collapsed:
            _expand_all_groups(panel)
        await asyncio.sleep(0)


def _shell_output(turn: int) -> str:
    lines = [f"$ uv run pytest tests/test_module_{turn}.py -q"]
    lines.extend(f"tests/test_module_{turn}.py::test_case_{index} PASSED [ {index * 2 + 2:3d}%]" for index in range(36))
    lines.append(f"36 passed in {1.0 + turn * 0.01:.2f}s")
    return "\n".join(lines)


async def _populate_realistic_session(
    panel: ChatPanel,
    *,
    turns: int,
    collapsed: bool,
) -> None:
    """Mimic a real agent session: every tool renderer plus mixed markdown per turn.

    Per turn: user message, intermediate agent message, read_file, grep,
    edit_file (inline diff), write_file (inline diff), shell output, a
    sub-agent markdown report, and a final agent message with mixed markdown
    (prose, quotes, a table, and a highlighted fence).
    """
    register_kind_renderer(KIND_SUB_AGENT, SubAgentToolCall)
    panel.set_tool_kinds(
        {
            "read_file": KIND_FILESYSTEM_READ,
            "write_file": KIND_FILESYSTEM_WRITE,
            "edit_file": KIND_FILESYSTEM_WRITE,
            "shell": KIND_SHELL,
            "grep": KIND_SEARCH,
            "explore_agent": KIND_SUB_AGENT,
        }
    )
    for turn in range(turns):
        await panel.add_user_message(f"Request {turn}: refactor module {turn}, run tests, and summarize findings.")
        await _stream_answer(panel, f"intro-{turn}", f"Turn {turn}: inspecting, editing, and verifying files.")

        path = f"src/pkg/module_{turn:03d}.py"

        read_id = f"read-{turn}"
        await panel.add_tool_start(read_id, "read_file", KIND_FILESYSTEM_READ, args={"path": path})
        await panel.add_tool_result(read_id, "read_file", _read_file_result(path, turn, 0), 11)

        grep_id = f"grep-{turn}"
        await panel.add_tool_start(grep_id, "grep", KIND_SEARCH, args={"pattern": f"function_{turn}", "path": "src/"})
        await panel.add_tool_result(
            grep_id, "grep", "\n".join(f"src/pkg/module_{turn:03d}.py:{10 + index}: match" for index in range(12)), 9
        )

        edit_id = f"edit-{turn}"
        before = _sample_file(path, turn, 0, changed=False)
        after = _sample_file(path, turn, 0, changed=True)
        await panel.add_tool_start(edit_id, "edit_file", KIND_FILESYSTEM_WRITE, args={"path": path})
        await panel.add_tool_result(edit_id, "edit_file", f"Edited {path}", 23, file_snapshot=(before, after))

        write_id = f"write-{turn}"
        await panel.add_tool_start(
            write_id, "write_file", KIND_FILESYSTEM_WRITE, args={"path": path, "overwrite": True}
        )
        await panel.add_tool_result(
            write_id, "write_file", f"Wrote {path} ({len(after.splitlines())} lines)", 17, file_snapshot=("", after)
        )

        shell_id = f"shell-{turn}"
        await panel.add_tool_start(
            shell_id, "shell", KIND_SHELL, args={"command": f"uv run pytest tests/test_module_{turn}.py -q"}
        )
        await panel.add_tool_result(shell_id, "shell", _shell_output(turn), 1_450)

        agent_id = f"agent-{turn}"
        await panel.add_tool_start(
            agent_id,
            "explore_agent",
            KIND_SUB_AGENT,
            args={"prompt": f"Verify refactor of module {turn}", "profile": "Explore"},
        )
        await panel.add_tool_result(agent_id, "explore_agent", _large_markdown_result(turn, 0, 90), 2_400)

        await _finish_turn(
            panel,
            f"answer-{turn}",
            _mixed_markdown_result(turn, 0, 110, fence_lines=40, table_columns=6, table_rows=8, cell_words=2),
        )
        if not collapsed:
            _expand_all_groups(panel)
        await asyncio.sleep(0)


async def _populate_process_history(panel: ChatPanel, *, turns: int, markdown_lines: int = 80) -> None:
    """Stream thinking and intermediate markdown with injected boundaries before successful completion."""
    for turn in range(turns):
        await panel.add_user_message(f"Request {turn}: inspect and verify.")
        for segment in range(2):
            if segment:
                await panel.add_user_message(f"Also check case {turn}.", is_injection=True)
            reasoning_id = f"reasoning-{turn}-{segment}"
            await panel.add_thinking_delta(reasoning_id, "reasoning", "Checking evidence.\n\n" * markdown_lines)
            panel.finish_thinking_segment(reasoning_id)
            await _stream_answer(panel, f"middle-{turn}-{segment}", "Intermediate findings.\n\n" * markdown_lines)
            call_id = f"read-{turn}-{segment}"
            await panel.add_tool_start(call_id, "read_file", KIND_FILESYSTEM_READ, args={"path": "sample.py"})
            await panel.add_tool_result(call_id, "read_file", "ok")
        await _finish_turn(panel, f"final-{turn}", f"Verified case {turn}.")
        await asyncio.sleep(0)


async def _populate_sub_agent_markdown(
    panel: ChatPanel,
    *,
    turns: int,
    tool_sets_per_turn: int,
    collapsed: bool,
    markdown_lines: int,
) -> None:
    register_kind_renderer(KIND_SUB_AGENT, SubAgentToolCall)
    panel.set_tool_kinds({"explore_agent": KIND_SUB_AGENT})
    await panel.add_user_message("Start synthetic long session with many sub-agent final reports.")
    for turn in range(turns):
        await _stream_answer(panel, f"intro-{turn}", f"Turn {turn}: delegating focused investigations.")
        for tool_index in range(tool_sets_per_turn):
            call_id = f"sub-agent-{turn}-{tool_index}"
            prompt = (
                f"Investigate scrolling and loading behavior for turn {turn}, case {tool_index}. "
                "Return a structured markdown report."
            )
            await panel.add_tool_start(
                call_id,
                "explore_agent",
                KIND_SUB_AGENT,
                args={"prompt": prompt, "profile": "Explore"},
            )
            await panel.add_tool_result(
                call_id,
                "explore_agent",
                _large_markdown_result(turn, tool_index, markdown_lines),
                2_400,
            )

        await _finish_turn(panel, f"answer-{turn}", f"Turn {turn}: findings incorporated.")
        if not collapsed:
            _expand_all_groups(panel)
        await asyncio.sleep(0)


async def _populate_table_markdown(
    panel: ChatPanel,
    *,
    turns: int,
    messages_per_turn: int,
    markdown_lines: int,
    table_columns: int,
    table_rows: int,
    cell_words: int,
) -> None:
    await panel.add_user_message("Start synthetic long session with table-heavy agent markdown.")
    for turn in range(turns):
        for result_index in range(messages_per_turn):
            await panel.add_agent_message(
                _table_markdown_result(
                    turn,
                    result_index,
                    markdown_lines,
                    table_columns=table_columns,
                    table_rows=table_rows,
                    cell_words=cell_words,
                ),
            )
            await asyncio.sleep(0)


async def _populate_mixed_markdown(
    panel: ChatPanel,
    *,
    turns: int,
    messages_per_turn: int,
    markdown_lines: int,
    fence_lines: int,
    table_columns: int,
    table_rows: int,
    cell_words: int,
) -> None:
    await panel.add_user_message("Start synthetic long session with mixed agent markdown.")
    for turn in range(turns):
        for result_index in range(messages_per_turn):
            await panel.add_agent_message(
                _mixed_markdown_result(
                    turn,
                    result_index,
                    markdown_lines,
                    fence_lines=fence_lines,
                    table_columns=table_columns,
                    table_rows=table_rows,
                    cell_words=cell_words,
                ),
            )
            await asyncio.sleep(0)
