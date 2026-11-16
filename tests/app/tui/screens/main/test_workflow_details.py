# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Node records, attempt selection, transcript routing and retry races through real screens."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, Static, Tab, TabbedContent, TabPane, Tabs

from chrys.app.tui.screens.dialogs import workflow_node
from chrys.app.tui.screens.dialogs.workflow_node import WorkflowNodeDialog
from chrys.app.tui.widgets.chat.agent_transcript_surface import AgentTranscriptSurface
from chrys.app.tui.widgets.chat.messages import AgentMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.app.tui.widgets.workflow.node_view import NodeView
from chrys.app.tui.widgets.workflow.projector import WorkflowProjector
from chrys.app.tui.widgets.workflow.values import ShownValue, ValueDocument, ValueTab, WorkflowValueView
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.service.session.sub_agent_transcript import PersistedSubAgentTranscript
from chrys.service.workflows.journal import WorkflowJournal
from chrys.service.workflows.outcomes import RunOutcome
from chrys.service.workflows.scheduler import AttemptRef
from chrys.service.workflows.store import DATA_DROPPED_KEY, node_value_path
from chrys.service.workflows.transcript import NodeTranscript, NodeUsage, read_node_transcript
from tests.app.tui.screens.main._workflow_support import workflow_selection
from tests.orchestration.workflows._hosting import make_project, write_workflow
from tests.support.secure_files import plant_owner_only_bytes
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import BusyWidget, click_when_settled
from tests.support.waiting import wait_for
from tests.support.workflow_workers import python_workflow

from ._workflow_support import (
    WorkflowEngine,
    confirm_workflow_cancel,
    open_workflow,
    run_store,
    shown_texts,
    value_settled,
    value_text,
    value_texts,
)


async def test_node_record_completion_during_shutdown_does_not_update_widgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 45)) as pilot:
        dialog = WorkflowNodeDialog(
            {"id": "fn", "kind": "python"},
            run=None,
            directory=None,
            history=list,
            retry=lambda _: False,
            retry_pending=lambda _: False,
        )
        await app.push_screen(dialog)
        input_widget = dialog.query_one("#workflow-node-input", WorkflowValueView)
        await wait_for(lambda: value_text(input_widget) == "No record available.", pilot=pilot)
        show = create_autospec(input_widget.show, side_effect=input_widget.show)
        monkeypatch.setattr(input_widget, "show", show)
        original_to_thread = asyncio.to_thread

        async def complete_during_shutdown(func, /, *args, **kwargs):
            result = await original_to_thread(func, *args, **kwargs)
            if func == dialog._read:
                app.exit()
                assert dialog.is_mounted and not dialog.is_attached
            return result

        monkeypatch.setattr(
            asyncio, "to_thread", create_autospec(original_to_thread, side_effect=complete_during_shutdown)
        )
        await dialog.load_records(dialog._generation, dialog.selected).wait()
        show.assert_not_called()


async def test_node_record_completion_during_removal_does_not_update_widgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 45)) as pilot:
        dialog = WorkflowNodeDialog(
            {"id": "fn", "kind": "python"},
            run=None,
            directory=None,
            history=list,
            retry=lambda _: False,
            retry_pending=lambda _: False,
        )
        await app.push_screen(dialog)
        input_widget = dialog.query_one("#workflow-node-input", WorkflowValueView)
        await wait_for(lambda: value_text(input_widget) == "No record available.", pilot=pilot)
        show = create_autospec(input_widget.show, side_effect=input_widget.show)
        monkeypatch.setattr(input_widget, "show", show)
        original_to_thread = asyncio.to_thread
        release = asyncio.Event()

        async def complete_during_removal(func, /, *args, **kwargs):
            result = await original_to_thread(func, *args, **kwargs)
            if func == dialog._read:
                # The dialog cancels this read when it is unmounted itself, which waits for every
                # child to go first. One of them stays busy while the others leave.
                dialog.query_one("#workflow-node-output", WorkflowValueView).call_later(release.wait)
                # Popping the screen skips the dialog's own dismissal, which cancels the read up front.
                app.pop_screen()
                await wait_for(lambda: not input_widget.is_attached, description="node dialog lost its children")
                assert dialog.is_mounted and dialog.is_attached and app.is_running
            return result

        monkeypatch.setattr(
            asyncio, "to_thread", create_autospec(original_to_thread, side_effect=complete_during_removal)
        )
        try:
            await dialog.load_records(dialog._generation, dialog.selected).wait()
        finally:
            release.set()
        show.assert_not_called()


async def test_value_tabs_switch_only_their_own_pane_by_click_or_key(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 45)) as pilot:
        dialog = WorkflowNodeDialog(
            {"id": "fn", "kind": "python"},
            run=None,
            directory=None,
            history=list,
            retry=lambda _: False,
            retry_pending=lambda _: False,
        )
        await app.push_screen(dialog)
        # The dialog's own record load must not overwrite the documents shown below.
        await dialog.load_records(dialog._generation, dialog.selected).wait()
        input_view = dialog.query_one("#workflow-node-input", WorkflowValueView)
        output_view = dialog.query_one("#workflow-node-output", WorkflowValueView)
        lines = "\n".join(f"  input line {index}" for index in range(80))
        await input_view.show(ValueDocument((ShownValue(lines, {"score": 3}),)))
        await output_view.show(ValueDocument((ShownValue("**output**"),)))
        tabs = input_view.query_one(Tabs)
        assert [tab.label_text for tab in tabs.query(Tab)] == ["Markdown", "Plain text", "Data"]
        assert [tab.label_text for tab in output_view.header.query(Tab)] == ["Markdown", "Plain text"]
        assert len(dialog.query(VirtualizedMarkdown)) == 2

        await click_when_settled(pilot, tabs.query_one("#workflow-value-plain", Tab))
        await wait_for(
            lambda: value_texts(input_view) == [lines] and not input_view.query(VirtualizedMarkdown),
            pilot=pilot,
            description="the input pane shows its text literally",
        )
        assert output_view.query_one(VirtualizedMarkdown).source == "**output**"
        # Switching tabs rebuilds only the body, so the tab bar keeps the keyboard focus.
        assert input_view.query_one(Tabs) is tabs and app.focused is tabs

        # The tab bar spans the pane above the content; only the content scrolls, and its
        # one-cell scrollbar sits against the dialog border.
        container, scroll = dialog.query_one("#workflow-node"), input_view.scroll
        assert scroll is not None
        # The scrollbar that the overflow turns on is placed by a later layout pass.
        await wait_for(
            lambda: scroll.max_scroll_y > 0 and scroll.vertical_scrollbar.region.height > 0,
            pilot=pilot,
            description="plain text overflows the pane and its scrollbar is placed",
        )
        scrollbar = scroll.vertical_scrollbar.region
        assert (tabs.region.x, tabs.region.right) == (container.region.x + 1, container.region.right - 1)
        assert scroll.region.y == tabs.region.bottom + 1 and scrollbar.y == scroll.region.y
        assert (scrollbar.width, scrollbar.right) == (1, container.region.right - 1)
        tabs_y = tabs.region.y
        scroll.scroll_end(animate=False, immediate=True)
        await wait_for(lambda: scroll.scroll_y == scroll.max_scroll_y, pilot=pilot)
        assert tabs.region.y == tabs_y

        await pilot.press("right")
        await wait_for(
            # The rebuild scrolls the new tab home once its mount completes.
            lambda: value_texts(input_view) == ["score  3"] and value_settled(input_view),
            pilot=pilot,
            description="the input pane shows its data fields",
        )
        assert tabs.active == "workflow-value-data" and input_view.selected is ValueTab.DATA
        assert scroll.scroll_y == 0
        assert output_view.selected is ValueTab.MARKDOWN


async def test_closing_node_details_cancels_record_read_before_widgets_are_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    read_started, release_read, read_finished = Event(), Event(), Event()

    def read_records(
        _dialog: WorkflowNodeDialog, _attempt: events.WorkflowNodeStateChanged | None
    ) -> tuple[None, None, str]:
        read_started.set()
        try:
            assert release_read.wait(timeout=10)
            return None, None, ""
        finally:
            read_finished.set()

    monkeypatch.setattr(
        WorkflowNodeDialog, "_read", create_autospec(WorkflowNodeDialog._read, side_effect=read_records)
    )
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        main._workflow.session_view.open_node("write_tour")
        try:
            await wait_for(read_started.is_set, pilot=pilot)
            dialog = app.screen
            assert isinstance(dialog, WorkflowNodeDialog)
            worker = next(
                worker for worker in app.workers if worker.node is dialog and worker.group == "workflow-node-records"
            )
            original_pop = app.pop_screen
            cancelled_before_pop: list[bool] = []

            def pop_screen():
                cancelled_before_pop.append(worker.is_cancelled)
                release_read.set()
                return original_pop()

            monkeypatch.setattr(app, "pop_screen", create_autospec(original_pop, side_effect=pop_screen))
            await pilot.press("escape")
            await wait_for(lambda: app.screen is main, pilot=pilot)
            assert cancelled_before_pop == [True]
        finally:
            release_read.set()
            await wait_for(read_finished.is_set, pilot=pilot)


@pytest.mark.parametrize("late_validation", [False, True])
async def test_shutdown_while_node_tabs_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, late_validation: bool
) -> None:
    from textual.await_complete import AwaitComplete

    from chrys.app.tui.widgets.workflow.projector import WorkflowProjector

    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    original_add = Tabs.add_tab
    stopped = False

    def add_tab(tabs: Tabs, tab: Tab) -> AwaitComplete:
        mounted = original_add(tabs, tab)

        async def finish_mount() -> None:
            nonlocal stopped
            await mounted
            if tabs.id == "workflow-iteration-tabs" and tab.id == "workflow-iteration-1":
                stopped = True
                app.exit()
                await tabs.clear()
                if late_validation:
                    # Tabs.add_tab validates its active ID after awaiting mount.
                    # Teardown can remove that tab before this continuation runs.
                    tabs.active = tab.id or ""

        return AwaitComplete(finish_mount())

    monkeypatch.setattr(Tabs, "add_tab", add_tab)
    projector = WorkflowProjector()
    projector.record(events.WorkflowRunStarted(run_id="run"))
    for number in (1, 2):
        projector.record(
            events.WorkflowNodeStateChanged(
                run_id="run",
                node_id="fn",
                activation_id=f"opaque-{number}",
                iteration=number,
                attempt=1,
                state="completed",
            )
        )
    async with app.run_test(size=(120, 45)):
        app.push_screen(
            WorkflowNodeDialog(
                {"id": "fn", "kind": "python"},
                run=projector.current,
                directory=None,
                history=list,
                retry=lambda _: False,
                retry_pending=lambda _: False,
            )
        )
        await wait_for(lambda: stopped)


async def test_shutdown_starts_before_node_screen_detaches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from textual.await_complete import AwaitComplete

    from chrys.app.tui.widgets.workflow.projector import WorkflowProjector

    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    ready, shutting_down, validated = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_add, original_close = Tabs.add_tab, app._close_all
    projector = WorkflowProjector()
    projector.record(events.WorkflowRunStarted(run_id="run"))
    projector.record(
        events.WorkflowNodeStateChanged(
            run_id="run", node_id="fn", activation_id="fn#1", iteration=1, attempt=1, state="completed"
        )
    )
    dialog = WorkflowNodeDialog(
        {"id": "fn", "kind": "python"},
        run=projector.current,
        directory=None,
        history=list,
        retry=lambda _: False,
        retry_pending=lambda _: False,
    )

    def add_tab(tabs: Tabs, tab: Tab) -> AwaitComplete:
        mounted = original_add(tabs, tab)

        async def finish_mount() -> None:
            await mounted
            if tabs.id == "workflow-attempt-tabs":
                ready.set()
                await shutting_down.wait()
                try:
                    assert dialog.is_attached and not app.is_running
                    await tabs.clear()
                    tabs.active = tab.id or ""  # The real late validation, while still attached.
                finally:
                    validated.set()

        return AwaitComplete(finish_mount())

    async def close_all() -> None:
        shutting_down.set()
        if ready.is_set():
            await validated.wait()
        await original_close()

    monkeypatch.setattr(Tabs, "add_tab", add_tab)
    monkeypatch.setattr(app, "_close_all", close_all)
    async with app.run_test(size=(120, 45)):
        app.push_screen(dialog)
        await wait_for(ready.is_set)
    assert validated.is_set()


async def test_closing_node_details_while_its_tabs_mount(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Removal drops new mounts before it detaches the dialog, so the added tab never mounts.

    Activating it then keeps the bar's current tab instead of raising "No Tab with id".
    """
    from textual.await_complete import AwaitComplete

    from chrys.app.tui.widgets.workflow.projector import WorkflowProjector

    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    original_add = Tabs.add_tab
    added = asyncio.Event()
    projector = WorkflowProjector()
    projector.record(events.WorkflowRunStarted(run_id="run"))
    projector.record(
        events.WorkflowNodeStateChanged(
            run_id="run", node_id="fn", activation_id="fn#1", iteration=1, attempt=1, state="completed"
        )
    )
    dialog = WorkflowNodeDialog(
        {"id": "fn", "kind": "python"},
        run=projector.current,
        directory=None,
        history=list,
        retry=lambda _: False,
        retry_pending=lambda _: False,
    )

    def add_tab(tabs: Tabs, tab: Tab) -> AwaitComplete:
        async def add_during_removal() -> None:
            if tabs.id != "workflow-attempt-tabs":
                await original_add(tabs, tab)
                return
            dialog.dismiss()
            # Textual marks the subtree when removal starts and mounts nothing into it from then on.
            await wait_for(lambda: tabs._pruning, description="node dialog is being removed")
            assert dialog.is_attached and app.is_running
            await original_add(tabs, tab)
            assert tabs.active == "" and not tabs.query(Tab)
            added.set()

        return AwaitComplete(add_during_removal())

    monkeypatch.setattr(Tabs, "add_tab", add_tab)
    async with app.run_test(size=(120, 45)) as pilot:
        app.push_screen(dialog)
        await wait_for(lambda: added.is_set() and not dialog.is_attached, pilot=pilot)
        assert app.is_running


def _pane_text(dialog: WorkflowNodeDialog, pane: str) -> str:
    return value_text(dialog.query_one(f"#workflow-node-{pane}", WorkflowValueView))


async def test_output_tab_shows_only_the_record_while_the_transcript_is_still_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Diagnostics and errors stay hidden until there are some, not until the transcript has loaded.

    The record lands on the Output tab before the agent transcript is loaded, and the empty
    diagnostics area must not show in between.
    """
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 45)) as pilot:
        dialog = WorkflowNodeDialog(
            {"id": "node1", "kind": "agent", "agent": {"profile": "Code", "model": "review-model"}},
            run=None,
            directory=None,
            history=list,
            retry=lambda _: False,
            retry_pending=lambda _: False,
        )
        release = asyncio.Event()
        load_transcript = dialog._load_transcript

        async def stalled_load_transcript(generation: int, attempt: object) -> str:
            await release.wait()
            return await load_transcript(generation, attempt)

        monkeypatch.setattr(
            dialog, "_load_transcript", create_autospec(load_transcript, side_effect=stalled_load_transcript)
        )
        await app.push_screen(dialog)
        output_pane = dialog.query_one(TabbedContent).get_pane("workflow-output-tab")
        output = dialog.query_one("#workflow-node-output", WorkflowValueView)
        await wait_for(lambda: value_text(output) == "No record available.", pilot=pilot)
        assert shown_texts(output_pane.query_one(VerticalScroll)) == ["No record available."]

        release.set()
        await wait_for(lambda: bool(dialog.query("#workflow-node-transcript Static")), pilot=pilot)
        assert shown_texts(output_pane.query_one(VerticalScroll)) == ["No record available."]


async def test_reselecting_an_attempt_while_its_transcript_is_removed_shows_it_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record load cancelled while it removes the transcript strands no widget and keeps no identity.

    Record loads are exclusive: selecting the first attempt again cancels the second attempt's load
    while that load is still removing the first attempt's transcript.
    """
    archived = NodeTranscript(PersistedSubAgentTranscript(), "completed", "", NodeUsage(), 0)
    monkeypatch.setattr(
        workflow_node, "read_node_transcript", create_autospec(read_node_transcript, return_value=archived)
    )
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 45)) as pilot:
        dialog = WorkflowNodeDialog(
            {"id": "node1", "kind": "agent", "agent": {"profile": "Code", "model": "review-model"}},
            run=None,
            directory=tmp_path,
            history=list,
            retry=lambda _: False,
            retry_pending=lambda _: False,
        )
        await app.push_screen(dialog)
        dialog.attempts = [
            events.WorkflowNodeStateChanged(node_id="node1", activation_id="node1", attempt=attempt, state="completed")
            for attempt in (1, 2)
        ]
        container = dialog.query_one("#workflow-node-transcript", Vertical)
        dialog.select_attempt(0)
        await wait_for(lambda: bool(container.query(AgentTranscriptSurface)), pilot=pilot)
        first = container.query_one(AgentTranscriptSurface)
        busy = BusyWidget()
        holder = Vertical(busy)
        await container.mount(holder)
        busy.hold()
        try:
            await wait_for(lambda: busy.holding, description="the busy widget holds its message loop")
            dialog.select_attempt(1)
            await wait_for(
                lambda: not first.is_attached, description="the second attempt's load removes the transcript"
            )
            dialog.select_attempt(0)
        finally:
            busy.release.set()
        await wait_for(
            lambda: not holder.is_attached and bool(container.query(AgentTranscriptSurface)),
            pilot=pilot,
            description="the first attempt's transcript is shown again",
        )
        [surface] = container.children
        assert isinstance(surface, AgentTranscriptSurface) and surface is not first


async def test_each_attempt_shows_its_own_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry that started over follows its own live journal; the earlier attempt shows its archived one."""
    archived = NodeTranscript(PersistedSubAgentTranscript(), "failed", "no response within 30s", NodeUsage(), 0)
    reader = create_autospec(read_node_transcript, return_value=archived)
    monkeypatch.setattr(workflow_node, "read_node_transcript", reader)

    def origin(attempt: int) -> InvocationOrigin:
        return InvocationOrigin("workflow_node", "", "node1", None, attempt=attempt)

    def state(attempt: int, name: str) -> events.WorkflowNodeStateChanged:
        return events.WorkflowNodeStateChanged(
            run_id="run", node_id="node1", activation_id="node1", attempt=attempt, state=name, invocation_id="node1"
        )

    projector = WorkflowProjector()
    projector.record(events.WorkflowRunStarted(run_id="run", manifest={"nodes": []}))
    projector.record(state(1, "running"))
    projector.record_invocation(events.InvocationToolCallStart(origin=origin(1), call_id="a", tool_name="read"))
    first, second = state(1, "retrying"), state(2, "running")
    projector.record(first)
    projector.record(second)
    projector.record_invocation(events.InvocationToolCallStart(origin=origin(2), call_id="b", tool_name="read"))
    run = projector.current
    assert run is not None
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 45)) as pilot:
        dialog = WorkflowNodeDialog(
            {"id": "node1", "kind": "agent", "agent": {"profile": "Code", "model": "review-model"}},
            run=run,
            directory=tmp_path,
            history=list,
            retry=lambda _: False,
            retry_pending=lambda _: False,
        )
        await app.push_screen(dialog)
        dialog.attempts = [first, second]
        container = dialog.query_one("#workflow-node-transcript", Vertical)
        dialog.select_attempt(1)
        await wait_for(lambda: bool(container.query(AgentTranscriptSurface)), pilot=pilot)
        live = container.query_one(AgentTranscriptSurface)
        assert live._journal is run.journals["node1", 2]
        assert reader.call_count == 0
        dialog.select_attempt(0)
        await wait_for(
            lambda: not live.is_attached and bool(container.query(AgentTranscriptSurface)),
            pilot=pilot,
            description="the first attempt's archived transcript replaces the live one",
        )
        reader.assert_called_once_with(tmp_path, "node1", 1)
        assert container.query_one(AgentTranscriptSurface)._journal is not run.journals["node1", 2]


async def test_large_graph_list_uses_same_keyboard_and_click_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.app.tui.widgets.workflow import graph as graph_module

    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        nodes = [
            {"id": f"node{index}", "kind": "agent", "agent": {"profile": "Code", "model": "review-model"}}
            for index in range(201)
        ]
        large = replace(preview, manifest={"nodes": nodes, "edges": []})
        compiler = create_autospec(
            graph_module.compile_ir_with_geometry, side_effect=graph_module.compile_ir_with_geometry
        )
        monkeypatch.setattr(graph_module, "compile_ir_with_geometry", compiler)
        panel = main._workflow_panel
        panel.show_preview(large)
        graph = panel.query_one(WorkflowGraph)
        assert graph.list_fallback and compiler.call_count == 0
        graph.show_nodes({"node1": NodeView("failed")})
        graph.focus()
        await wait_for(lambda: graph.size.height > 0, pilot=pilot)
        assert "node1 · Code · review-model · failed" in graph.render_line(1).text
        await pilot.press("j", "j", "j", "k", "enter")
        await wait_for(
            lambda: isinstance(app.screen, WorkflowNodeDialog) and app.screen.is_mounted,
            pilot=pilot,
            description="node dialog and its tabs are mounted",
        )
        assert isinstance(app.screen, WorkflowNodeDialog) and app.screen.node["id"] == "node1"
        dialog = app.screen
        tabs = dialog.query_one(TabbedContent)
        container = dialog.query_one("#workflow-node")
        output_pane = tabs.get_pane("workflow-output-tab")
        await wait_for(
            lambda: (
                tabs.region.width > 0
                and _pane_text(dialog, "input") == "No record available."
                and shown_texts(output_pane.query_one(VerticalScroll)) == ["No record available."]
            ),
            pilot=pilot,
        )
        assert tabs.region == container.region.shrink((1, 1, 1, 1))
        assert container.styles.border_title_color == panel.styles.border_title_color
        assert str(container.border_subtitle) == "pending"
        assert container.styles.border_subtitle_align == "right"
        assert not container.query_children("Static")
        assert [
            tabs.get_tab(pane).label_text
            for pane in ("workflow-input-tab", "workflow-output-tab", "workflow-transcript-tab")
        ] == ["Input", "Output", "Transcript"]
        assert not dialog.query("#workflow-status-tab")
        await click_when_settled(pilot, tabs.get_tab("workflow-output-tab"))
        await wait_for(lambda: output_pane.display and output_pane.region.width > 0, pilot=pilot)
        output = output_pane.query_one("#workflow-node-output", WorkflowValueView)
        assert output.region == output_pane.region
        assert output.body.region.x == container.region.x + 2
        assert output.body.region.right == container.region.right - 2
        assert output.body.region.y == output_pane.region.y
        assert output_pane.region.right == container.region.right - 1
        assert output_pane.region.bottom == container.region.bottom - 1
        app.save_screenshot("workflow-node-subtitle.svg", path=str(tmp_path))
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        graph.select_node("node190")
        await wait_for(lambda: graph.scroll_y > 0, pilot=pilot)
        box = graph.geometry["node190"]
        await pilot.click(
            graph, offset=(1 + graph.diagram_origin.x, box.y - round(graph.scroll_y) + graph.diagram_origin.y)
        )
        await wait_for(lambda: isinstance(app.screen, WorkflowNodeDialog) and app.screen.is_mounted, pilot=pilot)
        assert isinstance(app.screen, WorkflowNodeDialog) and app.screen.node["id"] == "node190"


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_idle_agent_has_transcript_tab_but_python_and_loop_nodes_do_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 45)) as pilot:
        app.locale_controller.switch_locale(locale)
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        for node_id in ("read_request", "refine", "architecture"):
            main._workflow.session_view.open_node(node_id)
            await wait_for(
                lambda: (
                    isinstance(app.screen, WorkflowNodeDialog)
                    and app.screen.is_mounted
                    and bool(app.screen.query(TabbedContent))
                ),
                pilot=pilot,
            )
            dialog = app.screen
            assert isinstance(dialog, WorkflowNodeDialog)
            tabs = dialog.query_one(TabbedContent)
            expected = ["Input", "Output"] if locale == "en" else ["输入", "输出"]
            if node_id == "architecture":
                expected.append("Transcript" if locale == "en" else "对话记录")
                await click_when_settled(pilot, tabs.get_tab("workflow-transcript-tab"))
                await wait_for(
                    lambda dialog=dialog: bool(dialog.query_one("#workflow-node-transcript").query(Static)), pilot=pilot
                )
            else:
                assert not dialog.query("#workflow-transcript-tab")
                assert not dialog.query(AgentTranscriptSurface)
            assert [tabs.get_tab(pane.id).label_text for pane in tabs.query(TabPane)] == expected
            await pilot.press("escape")
            await wait_for(lambda: app.screen is main, pilot=pilot)


@pytest.mark.parametrize("kind", ["python", "agent", "join"])
async def test_node_records_attempts_previous_output_and_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    from chrys.app.tui.screens.dialogs import workflow_node

    history_read = create_autospec(workflow_node.latest_node_output, side_effect=workflow_node.latest_node_output)
    monkeypatch.setattr(workflow_node, "latest_node_output", history_read)
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    write_workflow(project, "wf", python_workflow("def fn(value):\n    return value\n", "fn"))
    bus, engine = EventBus(), WorkflowEngine()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "wf")
        node = {"id": "fn", "kind": kind, "agent": {"profile": "Code"}, "callable": {"name": "fn"}}
        preview = replace(preview, manifest={**preview.manifest, "nodes": [node]})
        session_id = str(uuid4())
        main._workflow.session_view.selection = workflow_selection(main, session_id)
        session_dir = main._workflow.session_view._session_dir()
        assert session_dir is not None
        directory = session_dir / "workflows"
        previous = run_store(directory / new_analytics_id(), preview, session_id=session_id, started_at="2026-01-01")
        ref = AttemptRef(previous.header.run_id, "fn", "fn@iter#1", 1)
        journal = WorkflowJournal(previous, None, session_id=session_id)
        try:
            previous.write_node_value(ref.activation_id, 1, "output", {"value": {"text": "previous full output"}})
            await journal.node_state(ref, "completed", iteration=1)
            await journal.finish(RunOutcome.COMPLETED)
        finally:
            await previous.close()
        store = run_store(directory / new_analytics_id(), preview, session_id=session_id, started_at="2026-01-02")
        journal = WorkflowJournal(store, bus, session_id=session_id)
        run_id = store.header.run_id
        panel = main._workflow_panel
        panel.show_preview(preview, run_id=run_id)
        await bus.publish(events.WorkflowRunStarted(run_id=run_id, manifest=preview.manifest))
        try:
            for iteration, attempt in ((1, 1), (1, 2), (2, 1)):
                ref = AttemptRef(run_id, "fn", f"fn@iter#{iteration}", attempt)
                value = {"text": f"input {iteration}/{attempt}", "data": {"literal": "[brackets]"}}
                input_record = (
                    {"sources": [{"node_id": "left", "value": value}]}
                    if kind == "join"
                    else {"value": value, DATA_DROPPED_KEY: kind == "agent"}
                )
                store.write_node_value(ref.activation_id, attempt, "input", input_record)
                store.write_node_value(
                    ref.activation_id, attempt, "output", {"value": {"text": f"full {iteration}/{attempt}"}}
                )
                store.write_node_diagnostics(
                    ref.activation_id,
                    attempt,
                    phase="body",
                    iteration=iteration,
                    stdout=f"stdout {iteration}/{attempt}\n",
                    traceback="Traceback: broken [function]",
                    truncated=True,
                )
                await journal.node_state(
                    ref, "running", iteration=iteration, invocation_id="child" if kind == "agent" else ""
                )
                await journal.node_state(ref, "completed", iteration=iteration)
            graph = panel.query_one(WorkflowGraph)
            await wait_for(lambda: bool(graph.geometry) and graph.region.height > 0, pilot=pilot)
            graph.focus()
            await pilot.press("j", "enter")
            await wait_for(
                lambda: (
                    isinstance(app.screen, WorkflowNodeDialog)
                    and app.screen.is_mounted
                    and app.screen.selected is not None
                ),
                pilot=pilot,
            )
            dialog = app.screen
            assert isinstance(dialog, WorkflowNodeDialog)
            await wait_for(lambda: "input 2/1" in _pane_text(dialog, "input"), pilot=pilot)
            input_view = dialog.query_one("#workflow-node-input", WorkflowValueView)
            assert [tab.label_text for tab in input_view.header.query(Tab)] == ["Markdown", "Plain text", "Data"]
            await click_when_settled(pilot, input_view.query_one("#workflow-value-data", Tab))
            await wait_for(lambda: "literal  [brackets]" in _pane_text(dialog, "input"), pilot=pilot)
            assert "input 2/1" not in _pane_text(dialog, "input")
            assert str(dialog.query_one("#workflow-node").border_subtitle) == "completed"
            rounds = dialog.query_one("#workflow-iteration-tabs", Tabs)
            attempts = dialog.query_one("#workflow-attempt-tabs", Tabs)
            await wait_for(lambda: rounds.display and rounds.active == "workflow-iteration-1", pilot=pilot)
            assert len(dialog.attempts) == 3
            assert [tab.label_text for tab in rounds.query(Tab)] == ["Iteration 1", "Iteration 2"]
            assert not attempts.display
            previous_view = dialog.query_one("#workflow-node-previous", WorkflowValueView)
            assert value_texts(previous_view) == ["previous full output"]
            assert previous_view.display and str(previous_view.border_title) == "Previous run"
            tabs = dialog.query_one(TabbedContent)
            assert [tabs.get_tab(pane.id).label_text for pane in tabs.query(TabPane)] == (
                ["Input", "Output", "Transcript"] if kind == "agent" else ["Input", "Output"]
            )
            if kind == "agent":
                assert "structured data was dropped" in _pane_text(dialog, "input")
                assert dialog.selected is not None and dialog.selected.invocation_id == "child"
                dialog.query_one(TabbedContent).active = "workflow-transcript-tab"
                await wait_for(lambda: bool(dialog.query(AgentTranscriptSurface)), pilot=pilot)
                await bus.publish(
                    events.InvocationMessage(
                        origin=InvocationOrigin("workflow_node", "", "child", None, attempt=1),
                        text="live child [message]",
                        is_final=True,
                    )
                )
                await wait_for(lambda: bool(dialog.query(AgentMessage)), pilot=pilot)
                assert not main.query_one(ChatPanel).query(AgentMessage)
            elif kind == "python":
                await click_when_settled(pilot, tabs.get_tab("workflow-output-tab"))
                diagnostics = tabs.get_pane("workflow-output-tab").query_one("#workflow-node-diagnostics", Static)
                await wait_for(lambda: diagnostics.region.height > 0, pilot=pilot)
                assert "stdout 2/1" in str(diagnostics.content)
                assert "Traceback: broken [function]" in str(diagnostics.content)
                assert "truncated" in str(diagnostics.content)
            else:
                source = dialog.query_one("#workflow-node-input .workflow-value-source")
                assert str(source.border_title) == "left"
                assert shown_texts(source) == ["literal  [brackets]"]
                assert "stdout 2/1" in str(dialog.query_one("#workflow-node-diagnostics", Static).content)
            content_tab = dialog.query_one(TabbedContent).active
            await click_when_settled(pilot, rounds.get_tab("workflow-iteration-0"))
            await wait_for(
                lambda: (
                    "full 1/2" in _pane_text(dialog, "output")
                    and attempts.display
                    and attempts.active == "workflow-attempt-1"
                    and attempts.region.height > 0
                ),
                pilot=pilot,
            )
            assert dialog.selected is not None and dialog.selected.attempt == 2
            assert [tab.label_text for tab in attempts.query(Tab)] == ["Attempt 1", "Attempt 2"]
            assert rounds.region.bottom == attempts.region.y
            assert attempts.region.bottom == dialog.query_one(TabbedContent).region.y
            assert rounds.region.x == attempts.region.x == dialog.query_one("#workflow-node").region.x + 1
            await click_when_settled(pilot, attempts.get_tab("workflow-attempt-0"))
            output_view = dialog.query_one("#workflow-node-output", WorkflowValueView)
            # The next round restarts the records load, which must find this attempt already shown.
            await wait_for(
                lambda: "full 1/1" in _pane_text(dialog, "output") and value_settled(output_view), pilot=pilot
            )
            # New rounds must not pull a reader away from an older attempt.
            ref = AttemptRef(run_id, "fn", "opaque-retry", 1)
            store.append_node_emit(ref.activation_id, 1, 1, "streamed 4/1")
            await journal.node_state(ref, "running", iteration=4)
            # Registration exposes the third tab before the async sync restores
            # the selected attempt. Wait for that owner before sending input.
            await wait_for(
                lambda: rounds.tab_count == 3 and not dialog._tabs_sync_pending,
                pilot=pilot,
                description="new iteration tabs finish synchronizing the selected attempt",
            )
            assert rounds.active == "workflow-iteration-0" and attempts.active == "workflow-attempt-0"
            assert str(dialog.query_one("#workflow-node").border_subtitle) == "completed"
            assert "full 1/1" in _pane_text(dialog, "output")
            assert rounds.get_tab("workflow-iteration-2").label_text == "Iteration 4"
            assert dialog.query_one(TabbedContent).active == content_tab
            assert len(dialog.query(AgentTranscriptSurface)) <= 1
            app.save_screenshot(f"workflow-node-rounds-{kind}.svg", path=str(tmp_path))
            # Keyboard navigation uses the same tab selection and shared panes.
            attempts.focus()
            await wait_for(
                lambda: app.focused is attempts,
                pilot=pilot,
                description="attempt tabs own keyboard focus",
            )
            await pilot.press("right")
            await wait_for(lambda: "full 1/2" in _pane_text(dialog, "output"), pilot=pilot)
            await click_when_settled(pilot, rounds.get_tab("workflow-iteration-2"))
            await wait_for(
                lambda: (
                    value_texts(dialog.query_one("#workflow-node-output", WorkflowValueView)) == ["1 streamed 4/1"]
                    and not attempts.display
                ),
                pilot=pilot,
            )
            store.write_node_value(ref.activation_id, 1, "output", {"value": {"text": "full 4/1"}})
            assert str(dialog.query_one("#workflow-node").border_subtitle) == "running"
            await journal.node_state(ref, "completed", iteration=4)
            await wait_for(lambda: dialog.selected is not None and dialog.selected.state == "completed", pilot=pilot)
            assert str(dialog.query_one("#workflow-node").border_subtitle) == "completed"
            await wait_for(lambda: "full 4/1" in _pane_text(dialog, "output"), pilot=pilot)
            # The load fills the diagnostics after every pane has shown.
            await wait_for(
                lambda: (
                    not any(
                        worker.node is dialog and worker.group == "workflow-node-records" and not worker.is_finished
                        for worker in app.workers
                    )
                ),
                pilot=pilot,
                description="the completed round's records finish loading",
            )
            # The streamed message moved behind its own tab once the output arrived.
            output_view = dialog.query_one("#workflow-node-output", WorkflowValueView)
            assert [tab.label_text for tab in output_view.header.query(Tab)] == [
                "Markdown",
                "Plain text",
                "Progress messages",
            ]
            history_read.assert_called_once()
            if kind == "python":
                assert not diagnostics.display
                assert not str(diagnostics.content)
            assert rounds.active == "workflow-iteration-2"
            await pilot.press("escape")
            await wait_for(lambda: app.screen is main, pilot=pilot)
            box = graph.geometry["fn"]
            await pilot.click(
                graph,
                offset=(
                    box.x + 1 - round(graph.scroll_x) + graph.diagram_origin.x,
                    box.y + 1 - round(graph.scroll_y) + graph.diagram_origin.y,
                ),
            )
            await wait_for(lambda: isinstance(app.screen, WorkflowNodeDialog) and app.screen.is_mounted, pilot=pilot)
        finally:
            await store.close()


async def test_retry_is_latched_across_dialog_reopen_and_new_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    bus, engine = EventBus(), WorkflowEngine()
    retries: list[events.WorkflowNodeRetryRequest] = []
    cancellations: list[events.WorkflowCancelRequest] = []

    async def retry(event: events.WorkflowNodeRetryRequest) -> None:
        retries.append(event)

    async def cancel(event: events.WorkflowCancelRequest) -> None:
        cancellations.append(event)

    await bus.subscribe(events.WorkflowNodeRetryRequest, retry)
    await bus.subscribe(events.WorkflowCancelRequest, cancel)
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        main._workflow_panel.run_id = "run"
        main._workflow.session_view._run_ids = ["run"]
        await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
        await bus.publish(events.WorkflowRunStarted(run_id="run", manifest=preview.manifest))
        failure = events.WorkflowNodeStateChanged(
            run_id="run", node_id="write_tour", activation_id="write_tour@iter#1", attempt=1, state="awaiting_retry"
        )
        await bus.publish(failure)
        main._workflow.session_view.open_node("write_tour")
        await wait_for(
            lambda: (
                isinstance(app.screen, WorkflowNodeDialog)
                and app.screen.is_mounted
                and bool(app.screen.query("#workflow-node-retry"))
                and app.screen.query_one("#workflow-node-actions").display
            ),
            pilot=pilot,
        )
        await click_when_settled(pilot, "#workflow-node-retry")
        await click_when_settled(pilot, "#workflow-node-retry")
        await wait_for(lambda: len(retries) == 1, pilot=pilot)
        assert retries[0].expected_failed_attempt == 1
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        main._workflow.session_view.open_node("write_tour")
        await wait_for(
            lambda: (
                isinstance(app.screen, WorkflowNodeDialog)
                and app.screen.is_mounted
                and bool(app.screen.query("#workflow-node-retry"))
                and app.screen.query_one("#workflow-node-actions").display
            ),
            pilot=pilot,
        )
        await click_when_settled(pilot, "#workflow-node-retry")
        assert len(retries) == 1
        assert app.screen.query_one("#workflow-node-retry", Button).disabled
        await bus.publish(replace(failure, attempt=2))
        dialog = app.screen
        assert isinstance(dialog, WorkflowNodeDialog)
        await wait_for(lambda: dialog.selected is not None and dialog.selected.attempt == 2, pilot=pilot)
        assert not dialog.query_one("#workflow-node-retry", Button).disabled
        # Textual suppresses clicks during the preceding press animation, even for a new attempt.
        await wait_for(lambda: not dialog.query_one("#workflow-node-retry", Button).has_class("-active"), pilot=pilot)
        await click_when_settled(pilot, "#workflow-node-retry")
        await wait_for(lambda: len(retries) == 2, pilot=pilot)
        assert retries[1].expected_failed_attempt == 2 and retries[1].request_id != retries[0].request_id
        assert not dialog.query("#workflow-node-cancel")
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        await click_when_settled(pilot, "#workflow-stop")
        await confirm_workflow_cancel(pilot)
        await wait_for(lambda: len(cancellations) == 1, pilot=pilot)
        await bus.publish(events.WorkflowRunFinished(run_id="run", outcome="cancelled"))
        assert len(retries) == 2


async def test_corrupt_new_attempt_clears_previous_attempt_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    write_workflow(project, "records", python_workflow("def fn(value):\n    return value\n", "fn"))
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "records")
        main._workflow.session_view.selection = workflow_selection(main, str(uuid4()))
        directory = main._workflow.session_view._session_dir()
        assert directory is not None
        run_id = uuid4().hex
        store = run_store(directory / "workflows" / run_id, preview, session_id=directory.name, started_at="")
        try:
            main._workflow_panel.run_id = run_id
            await bus.publish(events.WorkflowRunStarted(run_id=run_id, manifest=preview.manifest))
            first = events.WorkflowNodeStateChanged(
                run_id=run_id, node_id="fn", activation_id="fn@iter#1", attempt=1, state="awaiting_retry"
            )
            store.write_node_value(first.activation_id, 1, "input", {"value": "first input"})
            store.write_node_value(first.activation_id, 1, "output", {"value": "first output"})
            await bus.publish(first)
            main._workflow.session_view.open_node("fn")
            await wait_for(
                lambda: (
                    isinstance(app.screen, WorkflowNodeDialog)
                    and app.screen.is_mounted
                    and bool(app.screen.query("#workflow-node-output"))
                ),
                pilot=pilot,
            )
            dialog = app.screen
            await wait_for(
                lambda: _pane_text(dialog, "output") == "first output",
                pilot=pilot,
            )
            plant_owner_only_bytes(node_value_path(store.run_dir, first.activation_id, 2, "input"), b"{")
            await bus.publish(replace(first, attempt=2))
            await wait_for(
                lambda: "property name" in str(dialog.query_one("#workflow-node-error", Static).content),
                pilot=pilot,
            )
            assert str(dialog.query_one("#workflow-node").border_subtitle) == "awaiting retry"
            assert dialog.query_one("#workflow-node-errors").display
            assert "first input" not in _pane_text(dialog, "input")
            assert "first output" not in _pane_text(dialog, "output")
            assert not dialog.query_one("#workflow-node-previous").display
        finally:
            await store.close()
