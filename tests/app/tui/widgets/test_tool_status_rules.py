# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for tool card status rules: shell exit codes and timeouts, structured failure metadata, hook denial, and error-prefixed text across tool kinds."""

from __future__ import annotations

import pytest
from textual.widgets import Static

from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall
from chrys.app.tui.widgets.chat.tool_call import (
    ToolCall,
    ToolGroup,
)
from chrys.foundation.tool_kinds import (
    KIND_FILESYSTEM_READ,
    KIND_MCP,
    KIND_SHELL,
)
from chrys.foundation.tool_result_metadata import (
    SHELL_EXIT_CODE_METADATA_KEY,
    SHELL_TIMED_OUT_METADATA_KEY,
    TOOL_ERROR_KIND_METADATA_KEY,
    TOOL_FAILED_METADATA_KEY,
)
from tests.support.tui_helpers import (
    ChatPanelApp,
    LocalizedWidgetApp,
    exec_panel_text,
    open_process_for,
)
from tests.support.waiting import wait_for


async def test_shell_tool_streamed_tail_survives_prune_rebuild() -> None:
    """Shell cards should rebuild with the streamed output tail, not the raw result head."""
    result_lines = [f"line{i:02d}" for i in range(1, 21)]
    expected_tail = result_lines[-10:]

    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("run")
        await cp.add_tool_start("sh1", "bash", KIND_SHELL, args={"command": "printf lines"})
        open_process_for(cp.query(ToolGroup).last())
        cp.query(ToolGroup).last().collapsed = False
        cp.update_tool_progress("sh1", result_lines)
        await cp.add_tool_result("sh1", "bash", "\n".join(result_lines) + "\n[exit_code: 0]", 123)
        await pilot.pause()

        shell = cp.query_one(ExecuteToolCall)
        assert exec_panel_text(shell).splitlines() == expected_tail

        await cp.add_agent_message("done")
        group = cp.query_one(ToolGroup)
        await wait_for(lambda: not group._content_mounted, pilot=pilot, description="not group._content_mounted")

        assert group.collapsed is True
        assert group._content_mounted is False
        assert len(group._tools) == 0

        open_process_for(group)
        group.collapsed = False
        await wait_for(lambda: group._content_mounted, pilot=pilot, description="group._content_mounted")

        rebuilt = group._tools["sh1"]
        assert isinstance(rebuilt, ExecuteToolCall)
        assert exec_panel_text(rebuilt).splitlines() == expected_tail


async def test_shell_tool_timeout_result_overrides_streamed_progress_on_rebuild() -> None:
    """Timeout results should render as errors even when live progress was already streamed."""
    result = (
        "Error: command timed out after 180 seconds.\n[partial output]\n1009 tests collected in 0.35s\n[exit_code: 0]"
    )

    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("run")
        await cp.add_tool_start("sh1", "bash", KIND_SHELL, args={"command": "pytest", "timeout": 180})
        open_process_for(cp.query(ToolGroup).last())
        cp.query(ToolGroup).last().collapsed = False
        cp.update_tool_progress("sh1", ["1009 tests collected in 0.35s"])
        await cp.add_tool_result("sh1", "bash", result, 180000, metadata={SHELL_TIMED_OUT_METADATA_KEY: True})
        await pilot.pause()

        shell = cp.query_one(ExecuteToolCall)
        panel = shell.query_one("#exec-panel")
        assert shell.status == "error"
        assert shell.has_class("-error")
        assert not shell.has_class("-success")
        assert str(panel.border_subtitle) == "Errored"
        assert exec_panel_text(shell).startswith("Error: command timed out")

        await cp.add_agent_message("done")
        group = cp.query_one(ToolGroup)
        await wait_for(lambda: not group._content_mounted, pilot=pilot, description="not group._content_mounted")

        open_process_for(group)
        group.collapsed = False
        await wait_for(lambda: group._content_mounted, pilot=pilot, description="group._content_mounted")

        rebuilt = group._tools["sh1"]
        assert isinstance(rebuilt, ExecuteToolCall)
        rebuilt_panel = rebuilt.query_one("#exec-panel")
        assert rebuilt.status == "error"
        assert rebuilt.has_class("-error")
        assert not rebuilt.has_class("-success")
        assert str(rebuilt_panel.border_subtitle) == "Errored"
        assert exec_panel_text(rebuilt).startswith("Error: command timed out")


async def test_shell_tool_non_timeout_failure_keeps_streamed_tail() -> None:
    """Failing shell commands with exit codes should keep the streamed tail summary."""
    progress_lines = [f"progress {index}" for index in range(12)]
    result = "banner\nsetup\n...\nFAILED tests/test_example.py::test_case\n[exit_code: 1]"

    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("run")
        await cp.add_tool_start("sh1", "bash", KIND_SHELL, args={"command": "pytest"})
        open_process_for(cp.query(ToolGroup).last())
        cp.query(ToolGroup).last().collapsed = False
        cp.update_tool_progress("sh1", progress_lines)
        await cp.add_tool_result("sh1", "bash", result, 1200)
        await pilot.pause()

        shell = cp.query_one(ExecuteToolCall)
        panel = shell.query_one("#exec-panel")
        assert shell.status == "error"
        assert str(panel.border_subtitle) == "Errored [1]"
        assert exec_panel_text(shell) == "\n".join(progress_lines[-10:])


@pytest.mark.parametrize(
    ("result", "metadata_kwargs"),
    [
        pytest.param("Error: expected message from stdout\n[exit_code: 0]", {}, id="exit-code-suffix"),
        pytest.param(
            "Error: expected message from stdout",
            {"metadata": {SHELL_EXIT_CODE_METADATA_KEY: 0}},
            id="structured-exit-code",
        ),
    ],
)
async def test_shell_tool_exit_code_zero_overrides_error_prefixed_stdout_on_rebuild(
    result: str, metadata_kwargs: dict[str, object]
) -> None:
    """Shell stdout may start with ``Error:``; a zero exit code remains success, live and after rebuild.

    The exit code may arrive as the legacy ``[exit_code: N]`` suffix or as
    structured metadata with no suffix text at all; both are authoritative. The
    suffix case passes no metadata at all rather than an explicit ``None``, so
    it still proves the suffix alone carries the verdict past
    ``add_tool_result``'s own default.
    """
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("run")
        await cp.add_tool_start("sh1", "bash", KIND_SHELL, args={"command": "printf error"})
        open_process_for(cp.query(ToolGroup).last())
        cp.query(ToolGroup).last().collapsed = False
        await cp.add_tool_result("sh1", "bash", result, 25, **metadata_kwargs)
        await pilot.pause()

        shell = cp.query_one(ExecuteToolCall)
        assert shell.status == "complete"
        assert shell.has_class("-success")
        assert not shell.has_class("-error")

        await cp.add_agent_message("done")
        group = cp.query_one(ToolGroup)
        await wait_for(lambda: not group._content_mounted, pilot=pilot, description="not group._content_mounted")

        open_process_for(group)
        group.collapsed = False
        await wait_for(lambda: group._content_mounted, pilot=pilot, description="group._content_mounted")

        rebuilt = group._tools["sh1"]
        assert isinstance(rebuilt, ExecuteToolCall)
        assert rebuilt.status == "complete"
        assert rebuilt.has_class("-success")
        assert not rebuilt.has_class("-error")


async def test_shell_tool_user_rejection_status_is_rejected_not_error() -> None:
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_tool_start("sh1", "bash", KIND_SHELL, args={"command": "rm file"})
        open_process_for(cp.query(ToolGroup).last())
        cp.query(ToolGroup).last().collapsed = False
        await cp.add_tool_result(
            "sh1",
            "bash",
            "Error: Tool execution was rejected by user.",
            25,
            approval="user_rejected",
        )
        await pilot.pause()

        shell = cp.query_one(ExecuteToolCall)
        assert shell.status == "rejected"
        assert shell.has_class("-rejected")
        assert not shell.has_class("-error")


async def test_shell_tool_structured_nonzero_exit_code_renders_error_without_suffix() -> None:
    """Structured shell exit-code metadata should not require parsing formatted output text."""
    result = "boom"
    metadata = {SHELL_EXIT_CODE_METADATA_KEY: 42}

    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("run")
        await cp.add_tool_start("sh1", "bash", KIND_SHELL, args={"command": "false"})
        open_process_for(cp.query(ToolGroup).last())
        cp.query(ToolGroup).last().collapsed = False
        await cp.add_tool_result("sh1", "bash", result, 1234, metadata=metadata)
        await pilot.pause()

        shell = cp.query_one(ExecuteToolCall)
        panel = shell.query_one("#exec-panel")
        assert shell.status == "error"
        assert shell.has_class("-error")
        assert not shell.has_class("-success")
        assert str(panel.border_subtitle) == "Errored [42]"


async def test_shell_tool_exit_suffix_removal_preserves_leading_whitespace() -> None:
    """Removing a shell exit-code suffix must not trim indentation or leave fake blank rows."""
    result = "  indented\nstill body\n\n[exit_code: 0]"

    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_user_message("run")
        await cp.add_tool_start("sh1", "bash", KIND_SHELL, args={"command": "printf indent"})
        open_process_for(cp.query(ToolGroup).last())
        cp.query(ToolGroup).last().collapsed = False
        await cp.add_tool_result("sh1", "bash", result, 25)
        await pilot.pause()

        shell = cp.query_one(ExecuteToolCall)
        text = exec_panel_text(shell)
        assert text == "  indented\nstill body"
        assert not text.endswith("\n")


_STATUS_CLASSES = {"complete": "-success", "error": "-error", "rejected": "-rejected"}


@pytest.mark.parametrize(
    ("tool_name", "tool_kind", "args", "output_text", "metadata_kwargs", "expected_status"),
    [
        pytest.param(
            "external_tool",
            KIND_MCP,
            {"query": "status"},
            "Error: expected text from remote tool",
            {},
            "complete",
            id="mcp-error-prefixed-text-stays-complete",
        ),
        pytest.param(
            "compress_context",
            "",
            {"marker_id": "missing"},
            "Error: marker_id not found",
            {"metadata": {TOOL_FAILED_METADATA_KEY: True}},
            "error",
            id="unkinded-structured-failure-renders-error",
        ),
        pytest.param(
            "custom_tool",
            "",
            {"value": 1},
            "Error: blocked by policy",
            {"metadata": {TOOL_ERROR_KIND_METADATA_KEY: "hook_denied"}},
            "rejected",
            id="hook-denied-renders-rejected-not-errored",
        ),
        pytest.param(
            "compress_context",
            "",
            {"marker_id": "remote"},
            "Error: literal remote payload",
            {},
            "error",
            id="legacy-unkinded-chrys-error-text-renders-error",
        ),
        pytest.param(
            "custom_read",
            KIND_FILESYSTEM_READ,
            {"path": "a.txt"},
            "Error: expected literal file contents",
            {"metadata": {TOOL_FAILED_METADATA_KEY: False}},
            "complete",
            id="failed-false-suppresses-error-text-fallback",
        ),
        pytest.param(
            "external_tool",
            KIND_MCP,
            {"query": "status"},
            "completed text",
            {"metadata": {TOOL_FAILED_METADATA_KEY: True}},
            "error",
            id="failed-true-marks-external-tool-failed",
        ),
        pytest.param(
            "custom_read",
            KIND_FILESYSTEM_READ,
            {"path": "missing.txt"},
            "Error: file not found",
            {},
            "error",
            id="chrys-error-prefixed-text-renders-error",
        ),
    ],
)
async def test_tool_result_status_rules(
    tool_name: str,
    tool_kind: str,
    args: dict[str, object],
    output_text: str,
    metadata_kwargs: dict[str, object],
    expected_status: str,
) -> None:
    """Structured failure metadata wins; the ``Error:`` text convention only applies to Chrys-owned kinds.

    External/MCP output may begin with ``Error:`` without meaning the tool
    failed, ``failed`` metadata overrides the text either way, hook denial
    renders as rejected rather than errored, and fallback renderers for
    Chrys-owned (or legacy unkinded) tools still honour the text convention.

    The text-convention rows omit ``metadata`` rather than passing ``None``:
    they are precisely the cases that must be decided by the text against
    ``add_tool_result``'s own default, which an explicit ``None`` would mask.
    """
    async with ChatPanelApp().run_test() as pilot:
        cp = pilot.app.query_one(ChatPanel)
        await cp.add_tool_start("call-1", tool_name, tool_kind, args=args)
        open_process_for(cp.query(ToolGroup).last())
        cp.query(ToolGroup).last().collapsed = False
        await cp.add_tool_result("call-1", tool_name, output_text, 17, **metadata_kwargs)
        await pilot.pause()

        tool = cp.query_one(ToolCall)
        assert tool.tool_kind == tool_kind
        assert tool.status == expected_status
        assert {name for name in _STATUS_CLASSES.values() if tool.has_class(name)} == {_STATUS_CLASSES[expected_status]}
        panel = tool.query_one("#tc-panel")
        assert (str(panel.border_subtitle) == "Rejected") is (expected_status == "rejected")


async def test_read_file_renderer_shows_parsed_error_even_when_structured_metadata_says_success() -> None:
    from chrys.app.tui.widgets.chat.renderers.read_file import ReadFileToolCall

    async with LocalizedWidgetApp(
        lambda: ReadFileToolCall("read1", "read_file", args={"path": "missing.txt"})
    ).run_test() as pilot:
        tool = pilot.app.query_one(ReadFileToolCall)

        tool.set_complete(
            "Error: file not found",
            duration_ms=10,
            metadata={TOOL_FAILED_METADATA_KEY: False},
        )
        await pilot.pause()

        assert tool.status == "error"
        assert tool.has_class("-error")
        assert not tool.has_class("-success")
        assert tool.query_one("#rf-panel", Static).render().plain == "Error: file not found"
