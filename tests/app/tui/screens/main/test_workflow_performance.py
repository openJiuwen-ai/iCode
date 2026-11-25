# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Count work during large workflow event bursts with a real, populated main screen."""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import create_autospec

import pytest
from rich.syntax import Syntax
from textual.widgets import Static

from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.widgets.chat.agent_transcript_surface import (
    TranscriptAssistantOp,
    TranscriptErrorOp,
    TranscriptResumedOp,
    TranscriptToolResultOp,
    TranscriptToolStartOp,
)
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolCall
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.models.invocations import InvocationOrigin
from tests.orchestration.workflows._hosting import make_workdir
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for

from ._workflow_support import WorkflowEngine, open_workflow, select_workflow_view

if TYPE_CHECKING:
    from collections.abc import Iterable

    from rich.console import Console, ConsoleOptions
    from rich.segment import Segment

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


async def test_execution_transitions_invalidate_workflow_bindings_without_focus_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    engine = WorkflowEngine()
    app = make_chrys_app(tmp_path / "sessions", engine=engine)
    async with app.run_test(size=(140, 50)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        main._sync_workflow_chrome()
        refresh = create_autospec(main.refresh_bindings, side_effect=main.refresh_bindings)
        monkeypatch.setattr(main, "refresh_bindings", refresh)

        # Check the synchronous owner of this invalidation, before unrelated
        # focus or layout messages can happen to refresh the footer for it.
        for snapshot in (ExecutionSnapshot("workflow", "run", True), ExecutionSnapshot("idle")):
            await engine.set_execution(snapshot, main._services.bus)
            main._sync_workflow_chrome()
            refresh.assert_called_once_with()
            refresh.reset_mock()
            main._sync_workflow_chrome()
            refresh.assert_not_called()


@pytest.mark.parametrize("hidden", [False, True])
async def test_twelve_thousand_events_coalesce_without_main_layout_or_footer_recompose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hidden: bool,
) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    engine, bus = WorkflowEngine(), EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(140, 50)) as pilot:
        main = app._main_screen
        assert main is not None
        chat = main.query_one(ChatPanel)
        cards = [ToolCall(f"chat-{index}", "read_file", args={"path": f"file-{index}.py"}) for index in range(30)]
        await chat.mount(*cards)
        for card in cards:
            card.set_complete("File contents\n" * 20)
        await wait_for(lambda: chat.virtual_size.height > chat.size.height, pilot=pilot)
        preview = await open_workflow(main, pilot, "demo-workflow")
        panel, controller = main._workflow_panel, main._workflow
        panel.run_id = "run"
        await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
        await bus.publish(events.WorkflowRunStarted(run_id="run", title=preview.title, manifest=preview.manifest))
        # Settle the initial run projection before measuring the event burst.
        await bus.publish(
            events.WorkflowNodeStateChanged(
                run_id="run",
                node_id="write_tour",
                activation_id="write_tour@iter#1",
                attempt=1,
                state="running",
                invocation_id="child",
            )
        )
        await wait_for(
            lambda: (
                not controller._refresh_pending
                and main._execution_binding_busy == (engine.snapshot.kind != "idle")
                and screen_is_settled(app, main)
            ),
            pilot=pilot,
        )
        footer = main.query_one(ChrysFooter)
        # A queued binding recompose has neither flag set yet. Wait for the
        # workflow-start binding signature to commit before measuring the burst.
        await wait_for(
            lambda: (
                not footer._binding_recompose_in_progress
                and not footer._binding_recompose_dirty
                and footer._visible_binding_signature == footer._binding_signature(main)
                and screen_is_settled(app, main)
            ),
            pilot=pilot,
        )
        settled = asyncio.Event()
        main.call_after_refresh(settled.set)
        await wait_for(settled.is_set, pilot=pilot)
        if hidden:
            await app.push_screen(ConfirmDialog())
            settled.clear()
            app.call_after_refresh(settled.set)
            await wait_for(settled.is_set, pilot=pilot)
        graph = panel.query_one(WorkflowGraph)
        styles = create_autospec(graph._node_spans, side_effect=graph._node_spans)
        layout = create_autospec(main._refresh_layout, side_effect=main._refresh_layout)
        recompose = create_autospec(footer.recompose, side_effect=footer.recompose)
        main_styles = create_autospec(main.update_node_styles, side_effect=main.update_node_styles)
        monkeypatch.setattr(graph, "_node_spans", styles)
        monkeypatch.setattr(main, "_refresh_layout", layout)
        monkeypatch.setattr(main, "update_node_styles", main_styles)
        monkeypatch.setattr(footer, "recompose", recompose)
        diagram, geometry, scroll = graph.diagram, graph.geometry, graph.scroll_offset
        # Inline bus subscribers cannot yield a visual frame during this deterministic burst.
        # Every semantic fact still reaches its owner; only the visual callback is coalesced.
        for ordinal in range(2000):
            state = events.WorkflowNodeStateChanged(
                run_id="run",
                node_id="write_tour",
                activation_id="write_tour@iter#1",
                attempt=ordinal // 2 + 1,
                state="retrying" if ordinal % 2 else "running",
                invocation_id="child",
            )
            origin = InvocationOrigin("workflow_node", "", "child", None, attempt=state.attempt)
            # Each attempt starts before its tools and fails after their results; every retry resumes the
            # attempt before it, so its transcript carries the earlier ones on.
            if state.state == "running":
                await bus.publish(state, raise_handler_errors=True)
                if state.attempt > 1:
                    await bus.publish(events.InvocationResumed(origin=origin), raise_handler_errors=True)
            await bus.publish(
                events.InvocationToolCallStart(
                    origin=origin,
                    call_id=str(ordinal),
                    tool_name="read_file",
                    tool_kind="filesystem.read",
                    args={"path": "file.py"},
                ),
                raise_handler_errors=True,
            )
            await bus.publish(
                events.InvocationToolCallResult(
                    origin=origin, call_id=str(ordinal), tool_name="read_file", result="full tool result"
                ),
                raise_handler_errors=True,
            )
            await bus.publish(
                events.InvocationMessage(origin=origin, text=f"message {ordinal}", is_final=True),
                raise_handler_errors=True,
            )
            await bus.publish(
                events.InvocationProgress(origin=origin, tool_call_count=ordinal + 1, total_usage_tokens=ordinal * 100),
                raise_handler_errors=True,
            )
            await bus.publish(
                events.WorkflowNodeOutput(
                    run_id="run",
                    node_id="write_tour",
                    activation_id="write_tour@iter#1",
                    attempt=state.attempt,
                    kind="emit",
                    ordinal=ordinal,
                    summary_text=str(ordinal),
                ),
                raise_handler_errors=True,
            )
            if state.state == "retrying":
                await bus.publish(state, raise_handler_errors=True)
        assert controller._refresh_pending and styles.call_count == 0
        await wait_for(lambda: not controller._refresh_pending, pilot=pilot)
        assert styles.call_count == (0 if hidden else 1)
        assert layout.call_count == recompose.call_count == main_styles.call_count == 0
        assert graph.diagram is diagram and graph.geometry is geometry and graph.scroll_offset == scroll
        run = controller.session_view.projector.current
        assert run is not None and run.fact_count == 4002
        assert len(run.facts) == 200
        assert run.usage["child"].tool_calls == 2000
        assert run.usage["child"].usage_tokens == 199900
        # Earlier attempts show their archives: only the live attempt's transcript is retained.
        assert list(run.journals) == [("child", 1000)]
        assert Counter(type(operation) for operation in run.journals["child", 1000].operations) == {
            TranscriptToolStartOp: 2000,
            TranscriptToolResultOp: 2000,
            TranscriptAssistantOp: 2000,
            TranscriptErrorOp: 1000,
            TranscriptResumedOp: 999,
        }
        assert len(chat.query(ToolCall)) == 30
        if hidden:
            await pilot.press("escape")
            await wait_for(lambda: app.screen is main and styles.call_count == 1, pilot=pilot)
            assert graph._views["write_tour"].state == "retrying"


async def test_code_tab_lexes_each_width_once_and_a_revisit_keeps_the_rendered_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(140, 50)) as pilot:
        main = app._main_screen
        assert main is not None
        chat = main.query_one(ChatPanel)
        cards = [ToolCall(f"chat-{index}", "read_file", args={"path": f"file-{index}.py"}) for index in range(30)]
        await chat.mount(*cards)
        for card in cards:
            card.set_complete("File contents\n" * 20)
        await wait_for(lambda: chat.virtual_size.height > chat.size.height, pilot=pilot)
        await open_workflow(main, pilot, "demo-workflow")
        panel, controller = main._workflow_panel, main._workflow
        source = panel.query_one("#workflow-code-source", Static)
        show_code = create_autospec(panel.show_code, side_effect=panel.show_code)
        monkeypatch.setattr(panel, "show_code", show_code)
        lexed_widths: list[int] = []
        get_syntax = Syntax._get_syntax

        def recording_get_syntax(self: Syntax, console: Console, options: ConsoleOptions) -> Iterable[Segment]:
            if self is source.content:
                lexed_widths.append(options.max_width)
            return get_syntax(self, console, options)

        monkeypatch.setattr(Syntax, "_get_syntax", recording_get_syntax)

        async def visit_code(visit: int) -> None:
            await select_workflow_view(main, pilot, "code")
            # Each visit re-reads the source; wait until it reached the view and was painted.
            await wait_for(
                lambda: (
                    show_code.call_count == visit
                    and not controller._tasks
                    and not controller._refresh_pending
                    and screen_is_settled(app, main)
                ),
                pilot=pilot,
                description=f"Code tab visit {visit} is laid out",
            )
            painted = asyncio.Event()
            assert main.call_after_refresh(painted.set)
            await wait_for(painted.is_set, pilot=pilot, description=f"Code tab visit {visit} is painted")

        await visit_code(1)
        shown = source.content
        assert isinstance(shown, Syntax) and lexed_widths
        # Height queries and paints at the widths with and without the scrollbar replay one rendering each.
        assert len(lexed_widths) == len(set(lexed_widths)), lexed_widths
        await select_workflow_view(main, pilot, "info")
        lexed_widths.clear()
        await visit_code(2)
        assert source.content is shown
        assert lexed_widths == []
