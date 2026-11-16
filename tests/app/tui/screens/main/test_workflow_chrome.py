# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow mode menus and graph controls retain the main screen's chrome."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from rich.cells import cell_len
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Button, Footer, OptionList, Static, Tab, TabbedContent, Tabs

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.screens.dialogs.app_mode import AppModeDialog
from chrys.app.tui.screens.dialogs.runtime_details import RuntimeDetailsDialog
from chrys.app.tui.screens.dialogs.workflow_input import WorkflowInputDialog
from chrys.app.tui.screens.dialogs.workflow_picker import WorkflowPickerDialog
from chrys.app.tui.screens.guides.screen import GuideDialog
from chrys.app.tui.screens.main import workflow_content
from chrys.app.tui.screens.main.model_indicator import ModelIndicatorState
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen, set_widget_visibility_without_layout
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.chrome.status_bar import STATUS_INTERRUPTED, StatusBar
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from tests.app.tui.screens.main._workflow_support import (
    WorkflowEngine,
    open_workflow,
    select_workflow_view,
    start_workflow,
    switch_mode,
    workflow_selection,
)
from tests.orchestration.workflows._hosting import make_project, write_workflow
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled, resize_when_settled
from tests.support.waiting import wait_for
from tests.support.workflow_workers import python_workflow


async def test_workflow_hides_status_bar_through_run_and_restores_chat_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    engine, bus = WorkflowEngine(), EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(140, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        bar = main.query_one(StatusBar)
        bar.set_tool_info("13 tools · 1 skill")
        bar.clear_status()
        await click_when_settled(pilot, "#status-tool-info")
        await wait_for(lambda: isinstance(app.screen, RuntimeDetailsDialog) and app.screen.is_mounted, pilot=pilot)
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)

        await switch_mode(main, pilot)
        await wait_for(
            lambda: not bar.display and bar.region.height == 0,
            pilot=pilot,
            description="workflow mode removes the status bar from layout",
        )
        assert not bar.display and bar.region.height == 0
        assert main.query_one(Footer).display
        # Chat notifications and chrome refreshes must not resurrect the row.
        bar.set_tool_info("7 tools · 2 skills")
        bar.flash(STATUS_INTERRUPTED.bind())
        main._view_adapter.sync_main_surface()
        await pilot.pause()
        assert not bar.display and bar.region.height == 0
        assert not main.query_one(InputBar).display
        preview = await open_workflow(main, pilot, "demo-workflow")
        assert not bar.display and bar.region.height == 0
        manifest = {**preview.manifest, "nodes": preview.manifest["nodes"][:1], "edges": []}
        await start_workflow(pilot)
        await wait_for(lambda: bool(main._workflow.awaiting_engine), pilot=pilot)
        await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
        await bus.publish(
            events.WorkflowRunAccepted(
                request_id=main._workflow.run_control._pending_run.request_id,
                run_id="run",
                selection=workflow_selection(main, "workflow-session"),
            )
        )
        await bus.publish(events.WorkflowRunStarted(run_id="run", title="Captured run", manifest=manifest))
        run = main._workflow.session_view.view_run()
        assert run is not None and run.status == "running"
        await pilot.pause()
        assert not bar.display and bar.region.height == 0
        await bus.publish(
            events.WorkflowNodeStateChanged(
                run_id="run", node_id=manifest["nodes"][0]["id"], activation_id="node", state="awaiting_retry"
            )
        )
        assert run.status == "awaiting_retry"
        await pilot.pause()
        assert not bar.display
        await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        await bus.publish(events.WorkflowRunFinished(run_id="run", outcome="cancelled"))
        assert run.status == "cancelled"
        await pilot.pause()
        assert not bar.display
        await switch_mode(main, pilot)
        await wait_for(
            lambda: bar.display and bar.region.height == 1,
            pilot=pilot,
            description="chat mode restores the status bar row",
        )
        assert bar.display and bar.region.height == 1
        assert str(bar.query_one("#status-flash-trail", Static).content) == "7 tools · 2 skills"
        bar.clear_status()
        await click_when_settled(pilot, "#status-tool-info")
        await wait_for(lambda: isinstance(app.screen, RuntimeDetailsDialog) and app.screen.is_mounted, pilot=pilot)


async def test_app_mode_and_workflow_are_separate_menus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(100, 32)) as pilot:
        main = app._main_screen
        assert main is not None
        badge = main.query_one("#mode-badge", Static)
        assert str(badge.content) == " APP MODE: Chat "
        assert badge.region.width == len(str(badge.content))
        assert main._suggestions.dispatch_slash_command("/help")
        await wait_for(lambda: isinstance(app.screen, GuideDialog) and app.screen.is_mounted, pilot=pilot)
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        await pilot.press("f8")
        await wait_for(lambda: isinstance(app.screen, GuideDialog) and app.screen.is_mounted, pilot=pilot)
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        await click_when_settled(pilot, badge)
        await wait_for(
            lambda: isinstance(app.screen, AppModeDialog) and app.screen.is_mounted,
            pilot=pilot,
            description="app mode dialog and its options are mounted",
        )
        options = app.screen.query_one(OptionList)
        assert options.option_count == 2
        assert options.get_option_at_index(0).disabled
        app.save_screenshot("app-mode.svg", path=str(tmp_path))
        await pilot.press("escape")
        assert not main._workflow.workflow_mode
        await switch_mode(main, pilot)
        assert app.screen is main
        assert not main.query("#workflow-list")
        panel = main._workflow_panel
        assert [button.id for button in panel.query("#workflow-controls Button") if button.display] == ["workflow-new"]
        assert str(panel.query_one("#workflow-new", Button).label) == "New Session"
        assert panel.query_one("#workflow-controls").parent is panel.query_one("#workflow-graph-tab")
        assert panel.query_one("#workflow-new").region.y == panel.query_one("#workflow-empty").region.bottom + 2
        assert not panel.query_one("#workflow-run", TabbedContent).query_one(Tabs).display
        assert not main.query_one(StatusBar).display
        assert not main.query("#workflow-tag, #workflow-label")
        await click_when_settled(pilot, "#workflow-new")
        await wait_for(
            lambda: (
                isinstance(app.screen, WorkflowPickerDialog)
                and app.screen.is_mounted
                and len(main._workflow.browser._picker.selection.rows) == 1
            ),
            pilot=pilot,
        )
        picker = app.screen.query_one(OptionList)
        assert not app.screen.query(Button)
        assert picker.option_count == 1
        assert all(picker.get_option_at_index(index).id != "workflow-create" for index in range(picker.option_count))
        app.save_screenshot("workflow-picker.svg", path=str(tmp_path))
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        assert not panel.query_one("#workflow-new", Button).has_focus


async def test_workflow_frame_and_controls_center_after_resize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    write_workflow(
        project,
        "centered",
        python_workflow(
            "# Scrollable source\n" * 60 + "def fn(value):\n    return value\n", "fn", title="A [workflow]"
        ),
    )
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(140, 42)) as pilot:
        main = app._main_screen
        assert main is not None
        chat = main.query_one(ChatPanel)
        status = main.query_one(StatusBar)
        status.set_model(ModelIndicatorState("Test Model", "Test model", "select", "model", True))
        await open_workflow(main, pilot, "centered")
        main._workflow.session_view.selection = workflow_selection(main, "abcdef1234567890")
        main._workflow.refresh()
        panel = main._workflow_panel
        graph = panel.query_one(WorkflowGraph)
        tabs = panel.query_one("#workflow-run", TabbedContent)
        assert "A [workflow]" in Text.from_markup(str(panel.border_title)).plain
        assert "abcdef123456" in str(panel.border_title)
        assert panel.border_subtitle == chat.border_subtitle
        assert panel.styles.border == chat.styles.border
        assert not status.display
        assert "Test Model" in str(main.query_one("#model-tag", Static).content)
        assert not panel.query("#workflow-pause")
        assert not panel.query("#workflow-code")
        assert not panel.query("#workflow-output")
        warnings = panel.query_one("#workflow-manifest-warnings", Static)
        assert list(tabs.get_pane("workflow-graph-tab").query(Static)) == [
            panel.query_one("#workflow-header", Static),
            warnings,
        ]
        assert not warnings.display
        assert not tabs.get_pane("workflow-output-tab").query("#workflow-header")
        assert tabs.get_pane("workflow-output-tab").query_one("#workflow-iterations", Static)
        for width in (140, 100, 80):
            # The screen, not just the App's size, must be at the new width before any scroll reads the layout.
            await resize_when_settled(pilot, width, 42)
            await wait_for(lambda: graph.outer_size.width == panel.content_size.width, pilot=pilot)
            start = panel.query_one("#workflow-start", Button)
            stop = panel.query_one("#workflow-stop", Button)
            layout = panel.query_one("#workflow-layout", Button)
            assert start.flat and stop.flat and layout.compact
            assert start.region.y == stop.region.y
            assert start.region.height == stop.region.height == 3
            assert start.region.right < stop.region.x
            new = panel.query_one("#workflow-new", Button)
            assert new.region.right < start.region.x
            assert not panel.query("#workflow-commands")
            assert new.region.y == start.region.y
            header = panel.query_one("#workflow-header")
            assert layout.region.y == header.region.y
            assert layout.region.height == 1
            assert layout.region.x >= header.region.right
            assert layout.region.right == panel.content_region.right - 1
            controls = panel.query_one("#workflow-controls")
            # No run has finished: Result is hidden but keeps its slot, so showing it moves no other button.
            result = panel.query_one("#workflow-result", Button)
            assert not result.visible and not result.region
            placed = [new.region, start.region, stop.region]
            set_widget_visibility_without_layout(result, True)
            assert [new.region, start.region, stop.region] == placed
            assert result.region.x == stop.region.right + 1 and result.region.y == stop.region.y
            assert result.region.height == stop.region.height and result.flat and result.variant == "warning"
            for button in controls.query(Button):
                assert button.content_size.width >= cell_len(str(button.label))
            if controls.max_scroll_x:
                assert controls.show_horizontal_scrollbar
                result.scroll_visible(animate=False, immediate=True)
                await wait_for(lambda result=result: result.region.right <= panel.content_region.right, pilot=pilot)
                new.scroll_visible(animate=False, immediate=True)
                await wait_for(lambda new=new: new.region.x >= panel.content_region.x, pilot=pilot)
            else:
                assert result.region.right <= panel.content_region.right
                assert (
                    abs((new.region.x + result.region.right) - (panel.content_region.x + panel.content_region.right))
                    <= 1
                )
            set_widget_visibility_without_layout(result, False)
            assert list(tabs.get_pane("workflow-graph-tab").query(Button)) == [layout, new, start, stop, result]
            assert tabs.region.x == panel.region.x + 1
            assert tabs.region.right == panel.region.right - 1
            assert tabs.query_one(Tabs).size.height == 2
            assert tabs.get_tab("workflow-graph-tab").size.height == 1
            assert graph.diagram_origin.x == (graph.scrollable_content_region.width - graph.diagram.width) // 2
            assert graph.diagram_origin.y == (graph.scrollable_content_region.height - graph.diagram.height) // 2
            box = graph.geometry["fn"]
            assert abs(2 * (graph.diagram_origin.x + box.x) + box.width - graph.scrollable_content_region.width) <= 1
            assert abs(2 * (graph.diagram_origin.y + box.y) + box.height - graph.scrollable_content_region.height) <= 1
            app.save_screenshot(f"workflow-{width}.svg", path=str(tmp_path))
        await select_workflow_view(main, pilot, "code")
        assert tabs.get_tab("workflow-code-tab").has_class("-active")
        assert not tabs.get_pane("workflow-graph-tab").display
        assert not tabs.get_pane("workflow-code-tab").query(Button)
        assert not is_widget_shown_on_active_screen(controls)
        assert all(button not in main.focus_chain for button in controls.query(Button))
        code_scroll = panel.query_one("#workflow-code-scroll", VerticalScroll)
        # Visibility flags settle before the compositor assigns scrollbar geometry.
        await wait_for(
            lambda: (
                code_scroll.show_vertical_scrollbar
                and code_scroll.show_horizontal_scrollbar
                and code_scroll.vertical_scrollbar.region.width > 0
                and code_scroll.horizontal_scrollbar.region.height > 0
            ),
            pilot=pilot,
        )
        assert code_scroll.scrollbar_size_vertical == 1
        assert code_scroll.region.bottom == panel.content_region.bottom
        assert code_scroll.vertical_scrollbar.region.width == 1
        assert code_scroll.horizontal_scrollbar.region.height == 1
        code_scroll.focus()
        await pilot.press("right")
        await wait_for(lambda: code_scroll.scroll_x > 0, pilot=pilot)
        code_scroll.scroll_to(x=code_scroll.max_scroll_x, animate=False)
        await wait_for(lambda: code_scroll.scroll_x == code_scroll.max_scroll_x, pilot=pilot)
        source = panel.query_one("#workflow-code-source", Static)
        assert source.region.right == code_scroll.scrollable_content_region.right
        app.save_screenshot("workflow-code-tab.svg", path=str(tmp_path))
        await select_workflow_view(main, pilot, "output")
        assert tabs.get_tab("workflow-output-tab").has_class("-active")
        assert not panel.query_one("#workflow-output-pages").display
        assert all(
            not is_widget_shown_on_active_screen(button) and button not in main.focus_chain
            for button in tabs.get_pane("workflow-output-tab").query(Button)
        )
        assert not is_widget_shown_on_active_screen(controls)
        outputs = panel.query_one("#workflow-outputs-scroll", VerticalScroll)
        status_output = panel.query_one("#workflow-status-output", Static)
        assert outputs.region.x == panel.content_region.x
        assert outputs.region.bottom == panel.content_region.bottom
        assert status_output.region.x == outputs.region.x + 1
        assert status_output.region.right == outputs.region.right - 1
        await click_when_settled(pilot, tabs.get_tab("workflow-input-tab"))
        await wait_for(lambda: tabs.active == "workflow-input-tab", pilot=pilot)
        assert not is_widget_shown_on_active_screen(controls)
        assert not tabs.get_pane("workflow-input-tab").query(Button)
        assert (
            tabs.get_pane("workflow-input-tab").query_one(VerticalScroll).region.bottom == panel.content_region.bottom
        )
        await pilot.press("escape")
        assert app.screen is main
        assert tabs.active == "workflow-graph-tab"
        assert app.focused is graph
        assert is_widget_shown_on_active_screen(controls)
        assert not panel.code_visible and not panel.output_visible
        app.locale_controller.switch_locale("zh-Hans")
        assert [
            tab.label_text
            for tab in (
                tabs.get_tab(pane)
                for pane in (
                    "workflow-graph-tab",
                    "workflow-info-tab",
                    "workflow-code-tab",
                    "workflow-input-tab",
                    "workflow-output-tab",
                )
            )
        ] == ["工作流", "信息", "源代码", "输入", "输出"]
        # Tab labels update before their new widths reach the compositor.
        localized_layout = asyncio.Event()
        main.call_after_refresh(localized_layout.set)
        await wait_for(localized_layout.is_set, pilot=pilot, description="localized tab layout refreshed")
        await select_workflow_view(main, pilot, "output")
        app.save_screenshot("workflow-output-tab.svg", path=str(tmp_path))


@pytest.mark.parametrize("chat_tab", ["tab-toc", "tab-tasks", "tab-context", "tab-debug"])
async def test_workflow_sidebar_hides_chat_tabs_and_restores_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, chat_tab: str
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        sidebar = main.query_one(SidebarPanel)
        tabs = sidebar.query_one(TabbedContent)
        await click_when_settled(pilot, tabs.get_tab(chat_tab))
        await wait_for(lambda: tabs.active == chat_tab, pilot=pilot)
        await switch_mode(main, pilot)
        expected = "tab-debug"
        await wait_for(lambda: tabs.active == expected and tabs.get_pane(expected).display, pilot=pilot)
        assert not tabs.get_pane("tab-toc").display
        assert not tabs.get_pane("tab-tasks").display
        assert not tabs.get_pane("tab-context").display
        assert [tab.label_text for tab in tabs.query_one(Tabs).query(Tab) if tab.display] == [
            "Debug",
            "Buddy",
        ]
        # Workflow mode keeps sidebar focus, so its tab strip stays keyboard-navigable.
        assert tabs.query_one(Tabs).can_focus
        sidebar.focus_tab("tab-tasks")
        assert tabs.active == expected
        sidebar.focus_tab("tab-context")
        assert tabs.active == expected
        app.locale_controller.switch_locale("zh-Hans")
        sidebar.focus_tab("tab-buddy")
        await wait_for(lambda: tabs.active == "tab-buddy", pilot=pilot)
        await switch_mode(main, pilot)
        await wait_for(lambda: tabs.active == chat_tab and tabs.get_pane(chat_tab).display, pilot=pilot)
        assert tabs.get_tab("tab-toc").display and tabs.get_tab("tab-tasks").display
        assert tabs.get_tab("tab-context").display
        assert not tabs.query_one(Tabs).can_focus


async def test_slow_source_read_does_not_switch_back_to_code_tab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    write_workflow(project, "tabs", python_workflow("def fn(value):\n    return value\n", "fn"))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "tabs")
        panel, controller = main._workflow_panel, main._workflow
        tabs = panel.query_one("#workflow-run", TabbedContent)
        graph = panel.query_one(WorkflowGraph)
        diagram = graph.diagram
        started, release = threading.Event(), threading.Event()
        read_entry_bytes = workflow_content.read_entry_bytes

        def delayed_read(path: Path, kind: str) -> bytes:
            started.set()
            assert release.wait(10), "source read was not released"
            return read_entry_bytes(path, kind)

        monkeypatch.setattr(workflow_content, "read_entry_bytes", delayed_read)
        try:
            await click_when_settled(pilot, tabs.get_tab("workflow-code-tab"))
            await wait_for(started.is_set, pilot=pilot)
            await select_workflow_view(main, pilot, "output")
        finally:
            release.set()
            await asyncio.gather(*tuple(controller._tasks))
        assert panel.output_visible and not panel.code_visible
        assert graph.diagram is diagram
        await select_workflow_view(main, pilot, "code")
        assert panel.code_visible


async def test_a_workflow_title_is_shown_without_terminal_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    write_workflow(
        project, "esc", python_workflow("def fn(value):\n    return value\n", "fn", title="Review\x1b[2JInjected")
    )
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "esc")
        panel = main._workflow_panel
        await wait_for(lambda: "Injected" in str(panel.border_title), pilot=pilot)
        assert str(panel.border_title) == "Review\ufffd[2JInjected"

        await click_when_settled(pilot, main.query_one("#workflow-start", Button))
        await wait_for(lambda: isinstance(app.screen, WorkflowInputDialog) and app.screen.is_mounted, pilot=pilot)

        title = app.screen.query_one("#workflow-input-title", Static)
        assert str(title.content) == str(title.tooltip) == "Review\ufffd[2JInjected"
