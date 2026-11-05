# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for SubAgentToolCall: task prompt rendering, nested inner-tool lifecycle and status rules, args updates, subtitles, and copy actions."""

from __future__ import annotations

import time

import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Static

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.widgets.chat.agent_transcript_surface import (
    AgentTranscriptSurface,
    TranscriptToolProgressOp,
    TranscriptToolResultOp,
    TranscriptToolStartOp,
)
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
)
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
from chrys.app.tui.widgets.chat.tool_call import (
    BaseToolCard,
    ToolCardHeader,
    ToolGroup,
)
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.config.settings import Settings
from chrys.foundation.tool_kinds import (
    KIND_FILESYSTEM_READ,
    KIND_MCP,
    KIND_SEARCH,
    KIND_SHELL,
    KIND_SUB_AGENT,
)
from chrys.foundation.tool_result_metadata import (
    SHELL_EXIT_CODE_METADATA_KEY,
    SHELL_TIMED_OUT_METADATA_KEY,
    TOOL_ERROR_KIND_METADATA_KEY,
    TOOL_FAILED_METADATA_KEY,
)
from chrys.foundation.util.sub_agent_context import SUB_AGENT_PROGRESS_METADATA_KEY
from tests.support.tui_helpers import (
    LocalizedWidgetApp,
    click_copy_button,
    header_zone_click,
    mount_sub_agent_detail,
    title_text,
    wait_for_sub_agent_inner_tool,
)
from tests.support.waiting import wait_for


async def test_sub_agent_renders_task_prompt_as_markdown_in_dedicated_panel() -> None:
    prompt = "## Investigate\n\n- check **tests**"

    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Explore", args={"prompt": prompt})
    ).run_test() as pilot:
        await pilot.pause()
        tc = pilot.app.query_one(SubAgentToolCall)

        task_panel = tc.query_one("#sa-task-panel")
        border_title = task_panel.border_title
        title = border_title.plain if isinstance(border_title, Text) else str(border_title)
        assert title == "Task"

        task = tc.query_one("#sa-task", VirtualizedMarkdown)
        assert task.source == prompt
        assert not list(tc.query("#sa-detail-action"))
        header = tc.query_one(ToolCardHeader)
        assert header.actions_visible is True
        assert header.copy_action_visible is False
        assert header._actions_text().plain.strip() == "View details"
        latest = tc.query_one("#sa-activity-text", Static)
        assert title_text(latest) == "Running Explore"

        await tc.add_inner_tool_start("inner1", "read_file", {"path": "README.md"})
        await pilot.pause()

        surface = await mount_sub_agent_detail(tc, pilot)
        await wait_for(
            lambda: len(surface.query(ToolGroup)) == 1,
            pilot=pilot,
            description="nested tool rendered in detail transcript",
        )
        group = surface.query_one(ToolGroup)
        group.collapsed = False
        matched: list[BaseToolCard] = []

        def find_nested() -> bool:
            nested = group.get_tool("inner1")
            if not isinstance(nested, BaseToolCard):
                return False
            matched[:] = [nested]
            return True

        await wait_for(
            find_nested,
            pilot=pilot,
            description="nested read tool mounted in detail transcript",
        )
        nested = matched[0]
        assert isinstance(nested, BaseToolCard)
        assert nested.tool_name == "read_file"
        assert prompt not in [message.text for message in surface.query(AgentMessage)]


def test_sub_agent_renderer_uses_base_tool_card_contract() -> None:
    tool = SubAgentToolCall("c1", "Explore", args={"prompt": "investigate usage"})

    assert isinstance(tool, BaseToolCard)
    assert tool.call_id == "c1"
    assert tool.tool_name == "Explore"
    assert tool.status == "running"
    assert tool.result_text == ""
    assert tool.duration_ms == 0
    assert tool.args == {"prompt": "investigate usage"}


async def test_sub_agent_inner_tool_start_upserts_for_lifetime_and_ignores_late_terminal_reemit() -> None:
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Remote", args={"prompt": "delegate"})
    ).run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)
        tool.update_progress(4, 100, 250, 1)

        await tool.add_inner_tool_start("inner-0", "pending", {"value": 1})
        await tool.add_inner_tool_start("inner-0", "renamed", {"value": 2}, tool_kind=KIND_SHELL)
        assert tool._progress_tool_calls == 4
        assert tool._total_inner_calls == 1
        assert tool._inner_tools["inner-0"].tool_name == "renamed"
        start_operations = [
            operation
            for operation in tool._transcript_journal.operations
            if isinstance(operation, TranscriptToolStartOp) and operation.call_id == "inner-0"
        ]
        assert start_operations[-1].args == {"value": 2}

        for index in range(1, 8):
            await tool.add_inner_tool_start(f"inner-{index}", f"tool-{index}", {})
        assert "inner-0" in tool._inner_tools
        assert tool._total_inner_calls == 8

        tool.complete_inner_tool("inner-0", "done", 15)
        assert "inner-0" not in tool._inner_tools
        await tool.add_inner_tool_start("inner-new", "new", {})

        await tool.add_inner_tool_start("inner-0", "late", {"value": 4})
        assert "inner-0" not in tool._inner_tools
        assert tool._total_inner_calls == 9
        assert "Spend: 250 tokens · 1 unreported attempt" in tool._render_subtitle()


async def test_sub_agent_inner_hosted_updates_mutate_the_existing_nested_entry() -> None:
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Explore", args={"prompt": "search"})
    ).run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)
        await tool.add_inner_tool_start("hosted:1", "web_search", {"query": "old"}, tool_kind=KIND_SEARCH)

        tool.update_inner_tool_args("hosted:1", {"query": "Chrys"})
        tool.update_inner_tool_progress(
            "hosted:1",
            ["Searching documentation"],
            image_contents=[{"uri": "data:image/png;base64,AAA"}],
        )

        entry = tool._inner_tools["hosted:1"]
        progress_operations = [
            operation
            for operation in tool._transcript_journal.operations
            if isinstance(operation, TranscriptToolProgressOp)
        ]
        assert progress_operations[-1].lines == ["Searching documentation"]
        assert progress_operations[-1].image_contents == [{"uri": "data:image/png;base64,AAA"}]

        tool.update_inner_tool_status("hosted:1", "interrupted")
        assert entry.status == "interrupted"
        assert "hosted:1" not in tool._terminal_inner_call_ids

        tool.complete_inner_tool(
            "hosted:1",
            "Error: interrupted by provider",
            25,
            image_contents=[{"uri": "data:image/png;base64,BBB"}],
            artifacts=[{"name": "partial.csv"}],
            metadata={TOOL_FAILED_METADATA_KEY: True},
        )
        assert "hosted:1" in tool._terminal_inner_call_ids
        assert entry.status == "error"
        assert tool._completed_inner_calls == 1


async def test_sub_agent_inner_completed_snapshot_accepts_late_authoritative_result() -> None:
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Explore", args={"prompt": "generate"})
    ).run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)
        await tool.add_inner_tool_start("hosted:1", "image_generation", {})

        tool.update_inner_tool_status("hosted:1", "completed", metadata={"result_text": "snapshot"})
        entry = tool._inner_tools["hosted:1"]
        assert entry.status == "complete"
        assert "hosted:1" not in tool._terminal_inner_call_ids
        assert tool._completed_inner_calls == 1

        tool.complete_inner_tool(
            "hosted:1",
            "authoritative",
            25,
            image_contents=[{"uri": "data:image/png;base64,AAA"}],
            artifacts=[{"name": "report.csv"}],
            metadata={"provider_item_type": "image_generation_call"},
        )

        assert "hosted:1" in tool._terminal_inner_call_ids
        assert tool._completed_inner_calls == 1
        await wait_for_sub_agent_inner_tool(tool, "hosted:1", pilot)
        results = [
            operation
            for operation in tool._transcript_journal.operations
            if isinstance(operation, TranscriptToolResultOp)
        ]
        assert results[-1].artifacts == [{"name": "report.csv"}]


@pytest.mark.parametrize("terminal", ["complete", "error"])
async def test_sub_agent_terminal_card_frees_inner_call_id_tracking(terminal: str) -> None:
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Remote", args={"prompt": "delegate"})
    ).run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)
        for index in range(3):
            await tool.add_inner_tool_start(f"inner-{index}", f"tool-{index}", {})
            tool.complete_inner_tool(f"inner-{index}", "done", 5)
        # Completion frees the seen-set slot immediately: the terminal
        # guard short-circuits duplicate starts before the seen-check.
        assert tool._seen_inner_call_ids == set()
        assert tool._terminal_inner_call_ids == {"inner-0", "inner-1", "inner-2"}
        assert tool._total_inner_calls == 3

        if terminal == "complete":
            tool.set_complete("all done")
        else:
            tool.set_error("boom")
        # A finished card stays mounted for the session — it must not pin
        # per-call id sets (thousands per ACP attempt, fresh ids per retry).
        assert tool._seen_inner_call_ids == set()
        assert tool._terminal_inner_call_ids == set()
        assert tool._inner_tools == {}
        assert tool._total_inner_calls == 3


async def test_sub_agent_resume_after_pause_resets_attempt_local_inner_call_tracking() -> None:
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Remote", args={"prompt": "delegate"})
    ).run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)
        await tool.add_inner_tool_start("inner-0", "first-attempt", {})
        tool.complete_inner_tool("inner-0", "done", 5)
        await tool.add_inner_tool_start("inner-1", "still-running", {})
        assert tool._total_inner_calls == 2

        tool.set_paused("acp_transport", "connection closed", 1)
        tool.set_resumed_after_pause()
        # Inner-call tracking is attempt-local: the dead attempt's stream is
        # already drained, and a fresh attempt may legitimately reuse raw
        # call ids. Nothing from the old feed may survive the resume.
        assert tool._seen_inner_call_ids == set()
        assert tool._terminal_inner_call_ids == set()
        assert tool._inner_tools == {}

        # A reused id must render as a brand-new running call — the stale
        # terminal guard must not swallow it.
        await tool.add_inner_tool_start("inner-0", "reused-id", {})
        assert tool._inner_tools["inner-0"].tool_name == "reused-id"
        assert tool._inner_tools["inner-0"].status == "running"
        # Spend counters stay cumulative across attempts.
        assert tool._total_inner_calls == 3


async def test_sub_agent_retry_drops_the_answer_its_failed_attempt_showed() -> None:
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Explore", args={"prompt": "delegate"})
    ).run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)
        tool.add_answer("segment-1", "Looking first.")
        await tool.add_inner_tool_start("inner-0", "read_file", {})
        tool.complete_inner_tool("inner-0", "done", 5)
        tool.add_answer("segment-2", "The result is")
        surface = await mount_sub_agent_detail(tool, pilot)

        def answers(view: AgentTranscriptSurface) -> list[str]:
            return [message.text for message in view.query(AgentMessage)]

        await wait_for(lambda: answers(surface) == ["Looking first.", "The result is"], pilot=pilot)

        tool.set_paused("stream_error", "connection closed", 0)
        tool.set_resumed_after_pause()
        # The retried attempt asks again for the answer it withdraws: that goes, the tool work stays.
        tool.withdraw_answer(("segment-2",))
        await wait_for(lambda: answers(surface) == ["Looking first."] and not surface._pending, pilot=pilot)
        assert len(surface.query(ToolGroup)) == 1
        tool.add_answer("segment-3", "The result is 42.")
        await wait_for(lambda: answers(surface) == ["Looking first.", "The result is 42."], pilot=pilot)

        reopened = await mount_sub_agent_detail(tool, pilot)
        await wait_for(lambda: answers(reopened) == ["Looking first.", "The result is 42."], pilot=pilot)


async def test_sub_agent_answer_a_failure_stopped_is_final_while_paused() -> None:
    """While paused, the answer the failed attempt showed is final in live and reopened detail views."""
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Explore", args={"prompt": "delegate"})
    ).run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)
        tool.add_answer("segment-1", "I'll write the file now.")
        surface = await mount_sub_agent_detail(tool, pilot)

        def states(view: AgentTranscriptSurface) -> list[tuple[str, bool, bool]]:
            return [
                (message.text, message._is_final, message.has_class("--streaming"))
                for message in view.query(AgentMessage)
            ]

        await wait_for(lambda: states(surface) == [("I'll write the file now.", False, True)], pilot=pilot)

        tool.set_paused("stream_error", "connection closed", 0)
        await wait_for(lambda: states(surface) == [("I'll write the file now.", True, False)], pilot=pilot)
        reopened = await mount_sub_agent_detail(tool, pilot)
        await wait_for(lambda: states(reopened) == [("I'll write the file now.", True, False)], pilot=pilot)

        # Retry still asks again for the answer the failed attempt showed.
        tool.set_resumed_after_pause()
        tool.withdraw_answer(("segment-1",))
        await wait_for(lambda: not states(surface) and not states(reopened), pilot=pilot)


async def test_sub_agent_acp_transport_pause_shows_ui_only_diagnostic_banner() -> None:
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Remote", args={"prompt": "delegate"})
    ).run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)
        tool.set_paused(
            "acp_transport",
            "connection closed",
            2,
            "/workspace/.chrys/sessions/s1/approvals/acp.log",
        )
        pause_info = tool.query_one("#sa-pause-info", Static)
        await wait_for(
            lambda: "External ACP transport interrupted" in pause_info.render().plain,
            pilot=pilot,
            description="ACP transport pause banner",
        )

        rendered = pause_info.render().plain
        assert "connection closed" in rendered
        assert "after 2 auto-retry attempts" in rendered
        assert "Diagnostics: /workspace/.chrys/sessions/s1/approvals/acp.log" in rendered


async def test_sub_agent_pause_shows_what_went_wrong_above_the_raw_error() -> None:
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Explore", args={"prompt": "delegate"})
    ).run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)
        tool.set_paused("framework_exc", "Connection error.", 0, None, "Can't resolve api.example.com.")
        pause_info = tool.query_one("#sa-pause-info", Static)
        await wait_for(
            lambda: "Connection error." in pause_info.render().plain,
            pilot=pilot,
            description="pause info with the display line",
        )

        lines = pause_info.render().plain.splitlines()
        assert lines.index("Can't resolve api.example.com.") + 1 == lines.index("Connection error.")


async def test_sub_agent_pause_diagnostic_path_display_copy_is_surrogate_safe() -> None:
    from chrys.foundation.platform.files import surrogate_safe_text

    surrogate_path = "/workspace/pro" + chr(0xDCFF) + "ject/acp.log"
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Remote", args={"prompt": "delegate"})
    ).run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)
        tool.set_paused("acp_transport", "connection closed", 1, surrogate_path)
        pause_info = tool.query_one("#sa-pause-info", Static)
        await wait_for(
            lambda: "Diagnostics:" in pause_info.render().plain,
            pilot=pilot,
            description="surrogate diagnostic line",
        )

        rendered = pause_info.render().plain
        assert surrogate_path not in rendered
        assert f"Diagnostics: {surrogate_safe_text(surrogate_path)}" in rendered
        rendered.encode("utf-8")  # the display copy must strict-encode


@pytest.mark.parametrize(
    ("tool_name", "tool_kind", "args", "output_text", "metadata_kwargs", "expected_status"),
    [
        pytest.param(
            "bash",
            KIND_SHELL,
            {"command": "false"},
            "normal-looking output",
            {"metadata": {SHELL_EXIT_CODE_METADATA_KEY: 1}},
            "error",
            id="shell-exit-code-marks-error",
        ),
        pytest.param(
            "bash",
            KIND_SHELL,
            {"command": "false"},
            "normal-looking output",
            {"metadata": {SHELL_TIMED_OUT_METADATA_KEY: True}},
            "error",
            id="shell-timeout-marks-error",
        ),
        pytest.param(
            "remote_lookup",
            KIND_MCP,
            {"query": "status"},
            "Error: expected text from remote tool",
            {},
            "complete",
            id="mcp-error-text-stays-complete",
        ),
        pytest.param(
            "remote_lookup",
            "",
            {"query": "status"},
            "Error: expected text from remote tool",
            {},
            "complete",
            id="unkinded-remote-error-text-stays-complete",
        ),
        pytest.param(
            "run_skill_script",
            "",
            {"skill_name": "docs"},
            "Error: script failed",
            {"metadata": {TOOL_FAILED_METADATA_KEY: True}},
            "error",
            id="unkinded-structured-failure-renders-error",
        ),
        pytest.param(
            "run_skill_script",
            "",
            {"skill_name": "docs"},
            "Error: literal remote payload",
            {},
            "error",
            id="legacy-unkinded-chrys-error-text-renders-error",
        ),
        pytest.param(
            "read_file",
            KIND_FILESYSTEM_READ,
            {"path": "secret.txt"},
            "Error: blocked by policy",
            {"metadata": {TOOL_FAILED_METADATA_KEY: True, TOOL_ERROR_KIND_METADATA_KEY: "hook_denied"}},
            "rejected",
            id="hook-denied-renders-rejected",
        ),
    ],
)
async def test_sub_agent_inner_tool_status_rules(
    tool_name: str,
    tool_kind: str,
    args: dict[str, object],
    output_text: str,
    metadata_kwargs: dict[str, object],
    expected_status: str,
) -> None:
    """Nested tool rows follow the same status rules as top-level cards.

    Shell rows fail on exit-code/timeout metadata regardless of output text,
    external or unknown tools may print ``Error:`` without failing, structured
    ``failed`` metadata and the Chrys-owned text convention render errors, and
    hook denial renders as rejected.

    The rows carrying no metadata omit the argument instead of passing ``None``:
    the text-convention rules they cover are exactly the ones that must hold
    against ``complete_inner_tool``'s own default.
    """
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Explore", args={"prompt": "investigate usage"})
    ).run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)

        await tool.add_inner_tool_start("inner1", tool_name, args, tool_kind=tool_kind)
        tool.complete_inner_tool("inner1", output_text, 25, **metadata_kwargs)
        await pilot.pause()

        nested = await wait_for_sub_agent_inner_tool(tool, "inner1", pilot)
        assert nested.tool_name == tool_name
        assert nested.status == expected_status


@pytest.mark.parametrize(
    ("result_text", "failed", "expected_status"),
    [
        pytest.param(
            "Error: EACCES is returned when permission is denied.",
            False,
            "complete",
            id="failed-false-suppresses-error-text",
        ),
        pytest.param("sub-agent failed after retries", True, "error", id="failed-true-renders-error-without-prefix"),
    ],
)
async def test_sub_agent_card_status_follows_structured_failure_metadata(
    result_text: str, failed: bool, expected_status: str
) -> None:
    """The ``failed`` metadata flag decides the card status, not the ``Error:`` text convention."""
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Explore", args={"prompt": "investigate"})
    ).run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)

        tool.set_complete(result_text, 25, metadata={TOOL_FAILED_METADATA_KEY: failed})
        await pilot.pause()

        assert tool.status == expected_status
        assert tool.has_class(f"-{expected_status}")
        assert not tool.has_class("-error" if expected_status == "complete" else "-complete")


@pytest.mark.parametrize(
    ("result_text", "expected"),
    [
        pytest.param(
            "Error: sub-agent 'Explore' failed — Streaming response produced no progress for 300s",
            "Errored: Streaming response produced no progress for 300s",
            id="policy-failure-shows-its-cause",
        ),
        pytest.param(
            "Error: sub-agent 'Explore' aborted by user after failure — Error code: 400",
            "Errored: aborted by user after failure — Error code: 400",
            id="user-abort-keeps-the-abort",
        ),
        pytest.param("Error: sub-agent 'Explore' failed — ", "Errored", id="no-cause-falls-back"),
        pytest.param("Error: cancelled (global interrupt)", "Errored: cancelled (global interrupt)", id="other-error"),
    ],
)
async def test_sub_agent_error_line_names_the_failure_reason(result_text: str, expected: str) -> None:
    """A failed card says why on its one-line summary instead of a bare "Errored"."""
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Explore", args={"prompt": "investigate"})
    ).run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)

        tool.set_complete(result_text, 25, metadata={TOOL_FAILED_METADATA_KEY: True})
        await pilot.pause()

        assert tool.status == "error"
        assert tool.query_one("#sa-activity-text", Static).render().plain == expected
        assert tool.result_text == result_text


async def test_sub_agent_error_line_is_one_sanitized_line() -> None:
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Explore", args={"prompt": "investigate"})
    ).run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)

        tool.set_error("Error: provider said\n\x1b[31mno")
        await pilot.pause()

        line = tool.query_one("#sa-activity-text", Static).render().plain
        assert line.startswith("Errored: provider said ")
        assert "\n" not in line and "\x1b" not in line


async def test_sub_agent_update_args_refreshes_task_prompt() -> None:
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Explore", args={"prompt": "old prompt"})
    ).run_test() as pilot:
        await pilot.pause()
        tc = pilot.app.query_one(SubAgentToolCall)

        tc.update_args({"prompt": "new prompt"})
        await pilot.pause()

        task = tc.query_one("#sa-task", VirtualizedMarkdown)
        assert task.source == "new prompt"


async def test_sub_agent_update_args_mounts_task_prompt_when_added() -> None:
    async with LocalizedWidgetApp(lambda: SubAgentToolCall("c1", "Explore", args={})).run_test() as pilot:
        await pilot.pause()
        tc = pilot.app.query_one(SubAgentToolCall)
        assert not list(tc.query("#sa-task-panel"))

        tc.update_args({"prompt": "new prompt"})
        await pilot.pause()

        task_panel = tc.query_one("#sa-task-panel")
        border_title = task_panel.border_title
        assert (border_title.plain if isinstance(border_title, Text) else str(border_title)) == "Task"
        task = tc.query_one("#sa-task", VirtualizedMarkdown)
        assert task.source == "new prompt"


async def test_sub_agent_update_args_removes_task_prompt_when_cleared() -> None:
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Explore", args={"prompt": "old prompt"})
    ).run_test() as pilot:
        await pilot.pause()
        tc = pilot.app.query_one(SubAgentToolCall)
        assert list(tc.query("#sa-task-panel"))

        tc.update_args({"prompt": ""})
        await pilot.pause()

        assert not list(tc.query("#sa-task-panel"))


async def test_sub_agent_rejected_title_omits_status_icon() -> None:
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "explore_agent", args={"prompt": "inspect"})
    ).run_test() as pilot:
        await pilot.pause()
        tc = pilot.app.query_one(SubAgentToolCall)

        tc.set_complete("Error: Tool execution was rejected by user.", approval="user_rejected")
        await pilot.pause()

        assert tc.approval == "user_rejected"
        assert tc.has_class("-rejected")
        panel = tc.query_one("#sa-panel")
        title = panel.border_title
        assert (title.plain if isinstance(title, Text) else str(title)) == "explore_agent"
        assert panel.border_subtitle == "Rejected"


async def test_sub_agent_omits_task_panel_when_prompt_is_empty() -> None:
    async with LocalizedWidgetApp(lambda: SubAgentToolCall("c1", "Explore", args={})).run_test() as pilot:
        await pilot.pause()
        tc = pilot.app.query_one(SubAgentToolCall)

        assert not list(tc.query("#sa-task-panel"))
        assert not list(tc.query(AgentTranscriptSurface))
        assert title_text(tc.query_one("#sa-activity-text", Static)) == "Running Explore"


async def test_sub_agent_copy_uses_prompt_input(monkeypatch: pytest.MonkeyPatch) -> None:
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Explore", args={"prompt": "investigate the tests"})
    ).run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        tc = pilot.app.query_one(SubAgentToolCall)
        header = tc.query_one(ToolCardHeader)
        assert header.actions_visible is True
        assert header.copy_action_visible is False
        assert header._actions_text().plain.strip() == "View details"

        view_click = header_zone_click(header, "view")
        assert view_click._stop_propagation is True

        tc.set_complete("final **answer**", duration_ms=100)
        await pilot.pause()
        assert header.actions_visible is True
        assert header.copy_action_visible is True
        assert header._actions_text().plain.strip() == "view / copy"

        click_copy_button(header)
        await pilot.pause()

        assert len(copied) == 1
        payload = copied[-1]
        assert "investigate the tests" in payload
        assert '"prompt"' not in payload
        assert "~~~markdown" in payload
        assert "final **answer**" in payload


async def test_sub_agent_header_localizes_running_view_only_then_terminal_actions() -> None:
    class ToolApp(App):
        locale_controller = LocaleController(Settings(locale="zh-Hans"))

        def compose(self) -> ComposeResult:
            yield SubAgentToolCall("c1", "Explore", args={"prompt": "investigate"})

    async with ToolApp().run_test() as pilot:
        card = pilot.app.query_one(SubAgentToolCall)
        header = card.query_one(ToolCardHeader)
        assert header._actions_text().plain.strip() == "查看详情"
        assert header._actions_width() == 10

        card.set_complete("done", duration_ms=10)
        await pilot.pause()

        assert header.copy_action_visible is True
        assert header._actions_text().plain.strip() == "查看 / 复制"


def test_sub_agent_running_subtitle_includes_ctx_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000.0
    monkeypatch.setattr(time, "monotonic", lambda: now)
    tc = SubAgentToolCall("c1", "Explore", args={"prompt": "investigate usage"})

    tc._progress_tool_calls = 2
    tc._progress_ctx_tokens = 34_000
    tc._progress_total_usage_tokens = 46_000
    tc._start_time = now - 123

    assert tc._render_subtitle() == "Tool calls: 2 · Ctx: 34.0k tokens · Spend: 46.0k tokens · Duration: 2m 3s"


def test_sub_agent_running_subtitle_hides_subsecond_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_000.0
    monkeypatch.setattr(time, "monotonic", lambda: now)
    tc = SubAgentToolCall("c1", "Explore", args={"prompt": "investigate usage"})

    tc._progress_tool_calls = 1
    tc._progress_ctx_tokens = 900
    tc._progress_total_usage_tokens = 900
    tc._start_time = now - 0.5

    assert tc._render_subtitle() == "Tool calls: 1 · Ctx: 900 tokens · Spend: 900 tokens"


def test_sub_agent_done_subtitle_keeps_ctx_tokens() -> None:
    tc = SubAgentToolCall("c1", "Explore", args={"prompt": "investigate usage"})
    tc.status = "complete"
    tc.duration_ms = 132_000
    tc._total_inner_calls = 21
    tc._progress_ctx_tokens = 57_700
    tc._progress_total_usage_tokens = 236_100

    assert tc._render_subtitle() == "Tool calls: 21 · Ctx: 57.7k tokens · Spend: 236.1k tokens · Duration: 2m 12s"


def _panel_subtitle(tc: SubAgentToolCall) -> str:
    """The counters line the card's panel shows on its bottom border."""
    subtitle = tc.query_one("#sa-panel").border_subtitle
    return subtitle.plain if isinstance(subtitle, Text) else str(subtitle)


@pytest.mark.parametrize(
    ("result", "status"),
    [("A story.", "complete"), ("Error: sub-agent 'Explore' failed — boom", "error")],
    ids=["complete", "error"],
)
async def test_a_restored_card_shows_the_counters_its_saved_result_keeps(result: str, status: str) -> None:
    progress = {
        "tool_calls": 3,
        "context_tokens": 9_100,
        "usage_tokens": 25_700,
        "unreported_attempts": 1,
        "compactions": 2,
    }
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Explore", args={"prompt": "write a story"})
    ).run_test() as pilot:
        tc = pilot.app.query_one(SubAgentToolCall)
        tc.tool_kind = KIND_SUB_AGENT
        # A replayed card gets only its saved result: no progress or inner tool events ever reach it.
        tc.set_complete(result, duration_ms=18_000, metadata={SUB_AGENT_PROGRESS_METADATA_KEY: progress})
        await pilot.pause()

        assert tc.status == status
        assert _panel_subtitle(tc) == (
            "Tool calls: 3 · Ctx: 9.1k tokens · Spend: 25.7k tokens · 1 unreported attempt"
            " · Compactions: 2 · Duration: 18s"
        )


async def test_a_live_card_keeps_the_counters_it_saw_over_the_saved_ones() -> None:
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Explore", args={"prompt": "investigate usage"})
    ).run_test() as pilot:
        tc = pilot.app.query_one(SubAgentToolCall)
        tc.tool_kind = KIND_SUB_AGENT
        await tc.add_inner_tool_start("inner1", "read_file", {"path": "README.md"})
        tc.update_progress(1, 4_000, 6_000)
        tc.set_complete(
            "Done.",
            duration_ms=2_000,
            metadata={SUB_AGENT_PROGRESS_METADATA_KEY: {"tool_calls": 7, "context_tokens": 1, "usage_tokens": 2}},
        )
        await pilot.pause()

        assert _panel_subtitle(tc) == ("Tool calls: 1 · Ctx: 4.0k tokens · Spend: 6.0k tokens · Duration: 2s")


@pytest.mark.parametrize(
    "progress",
    [None, "12", ["tool_calls"], {"tool_calls": True, "context_tokens": -5, "usage_tokens": "9", "compactions": 1.5}],
    ids=["absent", "string", "list", "bad-counts"],
)
def test_a_saved_result_without_usable_counters_shows_none(progress: object) -> None:
    tc = SubAgentToolCall("c1", "Explore", args={"prompt": "investigate usage"})
    tc.tool_kind = KIND_SUB_AGENT
    metadata = {} if progress is None else {SUB_AGENT_PROGRESS_METADATA_KEY: progress}

    tc.set_complete("Done.", duration_ms=2_000, metadata=metadata)

    assert tc._render_subtitle() == "Duration: 2s"


def test_sub_agent_subtitle_shows_compaction_count_only_after_commit() -> None:
    tc = SubAgentToolCall("c1", "Explore", args={"prompt": "investigate usage"})
    tc._progress_tool_calls = 33
    tc._progress_ctx_tokens = 74_400

    # No compaction yet \u2014 no Compactions segment.
    assert tc._render_subtitle().startswith("Tool calls: 33 \u00b7 Ctx: 74.4k tokens")
    assert "Compactions:" not in tc._render_subtitle()

    tc.add_compaction_start("comp-1")
    assert "Compactions:" not in tc._render_subtitle()

    # finished(ok) alone doesn't count \u2014 the round may still be abandoned
    # by a failed spill write; only the committed signal counts.
    tc.complete_compaction("comp-1", outcome="ok", duration_ms=1_000)
    assert "Compactions:" not in tc._render_subtitle()

    tc.record_compaction_committed("comp-1")
    assert "Compactions: 1" in tc._render_subtitle()

    # Failed / canceled compactions never emit the committed signal.
    tc.add_compaction_start("comp-2")
    tc.complete_compaction("comp-2", outcome="failed", failure_reason="boom")
    tc.add_compaction_start("comp-3")
    tc.complete_compaction("comp-3", outcome="canceled")
    assert "Compactions: 1" in tc._render_subtitle()

    tc.add_compaction_start("comp-4")
    tc.complete_compaction("comp-4", outcome="ok", duration_ms=1_000)
    tc.record_compaction_committed("comp-4")
    assert "Compactions: 2" in tc._render_subtitle()

    # The committed signal is independent of the feed entry \u2014 an evicted
    # or never-seen line still counts.
    tc.record_compaction_committed("comp-unseen")
    assert "Compactions: 3" in tc._render_subtitle()


async def test_sub_agent_error_copy_preserves_result_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    async with LocalizedWidgetApp(
        lambda: SubAgentToolCall("c1", "Explore", args={"prompt": "investigate the failure"})
    ).run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        tc = pilot.app.query_one(SubAgentToolCall)
        tc.tool_kind = KIND_SUB_AGENT
        tc.set_complete("Error: sub-agent failed", duration_ms=1234)
        await pilot.pause()

        assert tc.duration_ms == 1234
        header = tc.query_one(ToolCardHeader)
        assert header.actions_visible is True

        click_copy_button(header)
        await pilot.pause()

        assert len(copied) == 1
        payload = copied[-1]
        assert "- **Status:** `error`" in payload
        assert "- **Duration:** `1s`" in payload
        assert "Error: sub-agent failed" in payload
