# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The Result button beside Cancel and the modal holding a finished run's final outputs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from types import ModuleType
from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from rich.cells import cell_len
from textual.widgets import Button, Static, Tab, Tabs

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.screens.dialogs import workflow_result
from chrys.app.tui.screens.dialogs.workflow_result import WorkflowResultDialog
from chrys.app.tui.screens.main import workflow_content
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolCall
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.app.tui.widgets.workflow import text
from chrys.app.tui.widgets.workflow.values import ShownValue, ValueDocument, WorkflowValueView
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.service.workflows.store import RunRecord
from tests.orchestration.workflows._hosting import make_workdir
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled, resize_when_settled
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workflow_history import record_workflow_run

from ._workflow_support import (
    WorkflowEngine,
    open_workflow,
    run_store,
    save_workflow_session,
    select_archived_run,
    start_workflow,
    value_settled,
    value_text,
    workflow_selection,
)

if TYPE_CHECKING:
    from textual.pilot import Pilot

    from chrys.app.tui.app import ChrysApp
    from chrys.app.tui.screens.main.screen import MainScreen
    from chrys.orchestration.workflows.preview import WorkflowPreview

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")

_SUMMARY_ONLY = "Full output is unavailable. Only the saved summary is shown."


async def _record_run(
    main: MainScreen, preview: WorkflowPreview, outputs: dict[str, dict[str, Any] | None], *, outcome: str
) -> str:
    """Archive a run of the open session that produced ``outputs`` (node → stored record, None = never written).

    Without an ``outcome`` the run never finished, and replays as orphaned.
    """
    view = main._workflow.session_view
    directory, selection = view._session_dir(), view.selection
    assert directory is not None and selection is not None
    run_directory = directory / "workflows" / uuid4().hex
    store = run_store(run_directory, preview, session_id=selection.session_id, started_at="2026-01-01")
    activations = {node: f"output-{index}" for index, node in enumerate(outputs)}
    try:
        for node, record in outputs.items():
            await store.append(
                RunRecord.NODE_STATE,
                {
                    "node": node,
                    "activation": activations[node],
                    "attempt": 1,
                    "state": "completed",
                    "iteration": 0,
                    "failure_phase": "",
                },
            )
            if record is not None:
                store.write_node_value(activations[node], 1, "output", record)
        if outcome:
            await store.finish(
                outcome,
                {"outputs": [{"node": node, "activation": activations[node], "attempt": 1} for node in outputs]},
            )
    finally:
        await store.close()
    return run_directory.name


def _result(main: MainScreen) -> Button:
    return main._workflow_panel.query_one("#workflow-result", Button)


def _result_visible_when_run_tabs_rebuild(monkeypatch: pytest.MonkeyPatch, main: MainScreen) -> list[bool]:
    """Record Result's visibility each time the run tabs start rebuilding, an ``await`` before the next projection."""
    panel, seen = main._workflow_panel, []
    original = panel.show_runs

    async def show_runs(run_ids: list[str]) -> None:
        seen.append(_result(main).visible)
        await original(run_ids)

    monkeypatch.setattr(panel, "show_runs", create_autospec(original, side_effect=show_runs))
    return seen


def _views(dialog: WorkflowResultDialog) -> list[WorkflowValueView]:
    return [dialog.query_one(f"#workflow-result-{index}", WorkflowValueView) for index in range(len(dialog.outputs))]


async def _open_result(app: ChrysApp, pilot: Pilot) -> WorkflowResultDialog:
    """Click Result and wait until every output's view has been filled."""
    main = app._main_screen
    assert main is not None
    await click_when_settled(pilot, _result(main))
    await wait_for(
        lambda: (
            isinstance(app.screen, WorkflowResultDialog)
            and app.screen.is_mounted
            and all(view.document != ValueDocument() and value_settled(view) for view in _views(app.screen))
        ),
        pilot=pilot,
        description="the result dialog shows every output",
    )
    dialog = app.screen
    assert isinstance(dialog, WorkflowResultDialog)
    return dialog


async def _requests(bus: EventBus) -> list[events.WorkflowRunRequest]:
    requests: list[events.WorkflowRunRequest] = []

    async def requested(event: events.WorkflowRunRequest) -> None:
        requests.append(event)

    await bus.subscribe(events.WorkflowRunRequest, requested)
    return requests


async def _accept_live_run(
    pilot: Pilot, engine: WorkflowEngine, bus: EventBus, requests: list[events.WorkflowRunRequest]
) -> str:
    """Start a run through the Run input and have the backend accept and start it."""
    main = pilot.app._main_screen
    assert main is not None
    preview = main._workflow_panel.preview
    assert preview is not None
    count = len(requests)
    await start_workflow(pilot)
    await wait_for(lambda: len(requests) == count + 1, pilot=pilot)
    run_id = uuid4().hex
    await engine.set_execution(ExecutionSnapshot("workflow", run_id, True), main._services.bus)
    await bus.publish(
        events.WorkflowRunAccepted(
            run_id=run_id, request_id=requests[-1].request_id, selection=workflow_selection(main, "workflow-session")
        )
    )
    await bus.publish(
        events.WorkflowRunStarted(
            run_id=run_id,
            canonical_path=preview.source.canonical_path,
            workflow_id="demo-workflow",
            manifest=preview.manifest,
        )
    )
    await wait_for(lambda: main._workflow_panel.run_id == run_id, pilot=pilot)
    return run_id


def _finished(run_id: str, summary: str = "Tour summary") -> events.WorkflowRunFinished:
    return events.WorkflowRunFinished(
        run_id=run_id,
        outcome="completed",
        outputs=[events.WorkflowOutputSummary("render_tour", "render_tour@iter#1", summary, 1)],
    )


async def test_result_opens_each_final_output_in_its_own_tab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(140, 42)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        main._workflow.session_view.selection = workflow_selection(main, str(uuid4()))
        panel, result = main._workflow_panel, _result(main)
        buttons = [panel.query_one(f"#workflow-{name}", Button) for name in ("new", "start", "stop")]
        placed = [(button.region.x, button.region.width) for button in buttons]
        assert not result.visible and result not in main.focus_chain
        run_id = await _record_run(
            main,
            preview,
            {
                "render_tour": {"value": {"text": "# Tour\n\nFirst [literal] stop", "data": None}},
                "next_steps": {"value": {"text": "Next steps", "data": {"count": 2}}},
            },
            outcome="completed",
        )
        await select_archived_run(main, pilot, run_id)
        await wait_for(lambda: panel.run_id == run_id and result.visible, pilot=pilot)
        # Result takes the slot it kept beside Cancel: no other button moved.
        assert [(button.region.x, button.region.width) for button in buttons] == placed
        stop = buttons[-1]
        assert result.region.x == stop.region.right + 1 and result.region.y == stop.region.y
        assert str(result.label) == "Result" and result in main.focus_chain

        dialog = await _open_result(app, pilot)
        container = dialog.query_one("#workflow-result-frame")
        # The dialog's stylesheet frames it: the rule matches the container's id.
        assert container.styles.border_top[0] == "round"
        assert str(container.border_title) == "Result"
        assert str(container.border_subtitle) == "completed"
        # One bar of output tabs, named after the output nodes in declaration order; no view tabs.
        [tabs] = dialog.query(Tabs)
        assert [tab.label_text for tab in tabs.query(Tab)] == ["render_tour", "next_steps"]
        first, second = _views(dialog)
        assert first.body.query_one(VirtualizedMarkdown).source == "# Tour\n\nFirst [literal] stop"
        assert second.body.query_one(VirtualizedMarkdown).source == "Next steps"
        app.save_screenshot("workflow-result.svg", path=str(tmp_path))
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        assert app.focused is not result

        # Narrow: every button keeps its label, and the row scrolls to reach Result.
        await resize_when_settled(pilot, 60, 42)
        controls = panel.query_one("#workflow-controls")
        await wait_for(lambda: controls.max_scroll_x > 0 and controls.show_horizontal_scrollbar, pilot=pilot)
        for button in [*buttons, result]:
            assert button.content_size.width >= cell_len(str(button.label))
        result.scroll_visible(animate=False, immediate=True)
        await wait_for(
            lambda: panel.content_region.x <= result.region.x < result.region.right <= panel.content_region.right,
            pilot=pilot,
        )


async def test_a_restored_run_with_one_result_opens_it_without_tabs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_workdir(tmp_path)
    monkeypatch.chdir(project)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        main.action_workflow()
        state_store = main._services.state_store
        assert state_store is not None
        session_id = str(uuid4())
        await record_workflow_run(
            state_store.session_dir(session_id) / "workflows" / uuid4().hex,
            session_id=session_id,
            title="Archived",
            outcome="completed",
        )
        await save_workflow_session(state_store, session_id, project)
        await main._workflow.session_view.restore_session(session_id)
        await wait_for(lambda: app.screen is main and _result(main).visible, pilot=pilot)
        dialog = await _open_result(app, pilot)
        assert not dialog.query(Tabs)
        assert str(dialog.query_one("#workflow-result-frame").border_title) == "Result · check"
        [view] = _views(dialog)
        assert view.body.query_one(VirtualizedMarkdown).source == "Archived workflow output"


async def test_damaged_and_missing_results_do_not_hide_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    original = workflow_result.read_node_output

    def read_node_output(directory: Path, activation_id: str, attempt: int, *, node_kind: str = "") -> Any:
        if activation_id == "output-3":
            raise OSError("[red]disk\x1b[2J failed")
        return original(directory, activation_id, attempt, node_kind=node_kind)

    monkeypatch.setattr(workflow_result, "read_node_output", create_autospec(original, side_effect=read_node_output))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(140, 42)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        main._workflow.session_view.selection = workflow_selection(main, str(uuid4()))
        run_id = await _record_run(
            main,
            preview,
            {
                "good": {"value": {"text": "Good [literal] result", "data": None}},
                "missing": None,
                "damaged": {"value": "invalid"},
                "[b]odd\x1b[2J": {"value": {"text": "never read", "data": None}},
            },
            outcome="completed",
        )
        await select_archived_run(main, pilot, run_id)
        await wait_for(lambda: _result(main).visible, pilot=pilot)
        dialog = await _open_result(app, pilot)
        [tabs] = dialog.query(Tabs)
        assert [tab.label_text for tab in tabs.query(Tab)] == ["good", "missing", "damaged", "[b]odd�[2J"]
        # Archived runs keep no summaries, so a missing record has nothing to stand in for it.
        assert [value_text(view) for view in _views(dialog)] == [
            "Good [literal] result",
            "No record available.",
            "Invalid workflow output record: expected a text value.",
            "[red]disk�[2J failed",
        ]


async def test_result_follows_the_selected_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(140, 42)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        main._workflow.session_view.selection = workflow_selection(main, str(uuid4()))
        panel, result = main._workflow_panel, _result(main)
        output = {"render_tour": {"value": {"text": "Tour", "data": None}}}
        runs = {
            "no outputs": await _record_run(main, preview, {}, outcome="completed"),
            "orphaned": await _record_run(main, preview, output, outcome=""),
            "failed": await _record_run(main, preview, output, outcome="failed"),
            "cancelled": await _record_run(main, preview, output, outcome="cancelled"),
        }
        stop = panel.query_one("#workflow-stop", Button)
        for name, shown in (("no outputs", False), ("failed", True), ("orphaned", False), ("cancelled", True)):
            await select_archived_run(main, pilot, runs[name])
            await wait_for(
                lambda name=name, shown=shown: (
                    panel.run_id == runs[name] and not main._workflow._refresh_pending and result.visible is shown
                ),
                pilot=pilot,
                description=f"Result is {'shown' if shown else 'hidden'} for a {name} run",
            )
            # A hidden Result can be neither reached with Tab nor clicked.
            slot = (stop.region.right + 2, stop.region.y + 1)
            assert (result in main.focus_chain) is shown
            assert (app.get_widget_at(*slot)[0] is result) is shown
        # A new session leaves the cancelled run before its run tabs clear, so its Result never shows meanwhile.
        seen = _result_visible_when_run_tabs_rebuild(monkeypatch, main)
        await main._workflow.session_view.new_session()
        await wait_for(lambda: panel.run_id == "" and not result.visible, pilot=pilot)
        assert seen == [False]


async def test_a_refreshed_preview_keeps_the_result_and_the_next_run_hides_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    engine, bus = WorkflowEngine(), EventBus()
    requests = await _requests(bus)
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(140, 42)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        panel, result = main._workflow_panel, _result(main)
        first = await _accept_live_run(pilot, engine, bus, requests)
        assert not result.visible
        await bus.publish(_finished(first))
        await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        await wait_for(lambda: result.visible, pilot=pilot)
        # The live run's full value was never stored here, so its summary stands in.
        dialog = await _open_result(app, pilot)
        assert not dialog.query(Tabs)
        assert str(dialog.query_one("#workflow-result-frame").border_title) == "Result · render_tour"
        [view] = _views(dialog)
        assert value_text(view) == f"{_SUMMARY_ONLY}\nTour summary"
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)

        # Reloading the definition prepares the next run; the finished run stays selected.
        previous = panel.preview
        main._workflow.browser.load_preview("demo-workflow")
        await wait_for(
            lambda: panel.preview is not previous and not panel.previewing and not main._workflow._refresh_pending,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        assert panel.run_id == first and result.visible
        # Accepting selects the new run before its run tab exists, so the finished run's Result never shows over it.
        seen = _result_visible_when_run_tabs_rebuild(monkeypatch, main)
        await _accept_live_run(pilot, engine, bus, requests)
        await wait_for(lambda: not result.visible, pilot=pilot)
        assert seen == [False]


async def test_result_and_its_dialog_are_translated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    engine, bus = WorkflowEngine(), EventBus()
    requests = await _requests(bus)
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(140, 42)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        result = _result(main)
        run_id = await _accept_live_run(pilot, engine, bus, requests)
        await bus.publish(_finished(run_id))
        await wait_for(lambda: result.visible and str(result.label) == "Result", pilot=pilot)
        app.locale_controller.switch_locale("zh-Hans")
        await wait_for(lambda: str(result.label) == "结果", pilot=pilot)
        dialog = await _open_result(app, pilot)
        container = dialog.query_one("#workflow-result-frame")
        assert str(container.border_title) == "结果 · render_tour"
        completed = text.state_label("completed", app.locale_controller)
        assert completed != "completed" and str(container.border_subtitle) == completed
        notice = text.render(text.OUTPUT_SUMMARY_ONLY.bind(), app.locale_controller)
        assert "无法读取完整输出" in notice
        [view] = _views(dialog)
        assert value_text(view) == f"{notice}\nTour summary"


async def test_closing_the_result_cancels_its_read_before_widgets_are_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read_started, release_read, read_finished = Event(), Event(), Event()

    def read(_dialog: WorkflowResultDialog) -> list[ValueDocument]:
        read_started.set()
        try:
            assert release_read.wait(timeout=10)
            return [ValueDocument(placeholder="late")]
        finally:
            read_finished.set()

    monkeypatch.setattr(WorkflowResultDialog, "_read", create_autospec(WorkflowResultDialog._read, side_effect=read))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        dialog = WorkflowResultDialog(
            (events.WorkflowOutputSummary("out", "out@1", "summary", 1),), kinds={}, directory=None, outcome="completed"
        )
        app.push_screen(dialog)
        try:
            await wait_for(read_started.is_set, pilot=pilot)
            assert app.screen is dialog
            worker = next(
                worker for worker in app.workers if worker.node is dialog and worker.group == "workflow-result"
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


async def test_closing_a_covered_result_leaves_the_screen_above_it(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = WorkflowResultDialog(
            (events.WorkflowOutputSummary("out", "out@1", "summary", 1),), kinds={}, directory=None, outcome="completed"
        )
        await app.push_screen(dialog)
        cover = WorkflowResultDialog(
            (events.WorkflowOutputSummary("other", "other@1", "other", 1),), kinds={}, directory=None, outcome=""
        )
        await app.push_screen(cover)
        await wait_for(lambda: app.screen is cover and cover.is_mounted, pilot=pilot)
        # Popping takes the screen off the stack at once, so the stack shows any wrong pop right away.
        dialog.action_close()
        assert app.screen_stack[-2:] == [dialog, cover]
        cover.action_close()
        await wait_for(lambda: app.screen is dialog, pilot=pilot)


async def test_a_read_finishing_during_shutdown_does_not_update_the_dialog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        dialog = WorkflowResultDialog(
            (events.WorkflowOutputSummary("out", "out@1", "summary", 1),), kinds={}, directory=None, outcome="completed"
        )
        await app.push_screen(dialog)
        [view] = _views(dialog)
        await wait_for(lambda: value_text(view) == f"{_SUMMARY_ONLY}\nsummary", pilot=pilot)
        show = create_autospec(view.show, side_effect=view.show)
        monkeypatch.setattr(view, "show", show)
        original_to_thread = asyncio.to_thread

        async def complete_during_shutdown(func, /, *args, **kwargs):
            result = await original_to_thread(func, *args, **kwargs)
            app.exit()
            assert dialog.is_mounted and not dialog.is_attached
            return result

        shadow = ModuleType("asyncio")
        shadow.to_thread = create_autospec(original_to_thread, side_effect=complete_during_shutdown)
        monkeypatch.setattr(workflow_result, "asyncio", shadow)
        await dialog.load_outputs().wait()
        show.assert_not_called()


async def test_app_shutdown_does_not_wait_for_a_held_result_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read_started, release_read, read_finished = Event(), Event(), Event()

    def read(_dialog: WorkflowResultDialog) -> list[ValueDocument]:
        read_started.set()
        try:
            assert release_read.wait(timeout=10)
            return [ValueDocument(placeholder="late")]
        finally:
            read_finished.set()

    monkeypatch.setattr(WorkflowResultDialog, "_read", create_autospec(WorkflowResultDialog._read, side_effect=read))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            dialog = WorkflowResultDialog(
                (events.WorkflowOutputSummary("out", "out@1", "summary", 1),),
                kinds={},
                directory=None,
                outcome="completed",
            )
            app.push_screen(dialog)
            await wait_for(read_started.is_set, pilot=pilot)
            [view] = _views(dialog)
        # Leaving run_test exits the App while the read's thread is still blocked.
        assert not app.is_running and not read_finished.is_set()
        assert view.document == ValueDocument()
    finally:
        release_read.set()
        await wait_for(read_finished.is_set)


async def test_showing_result_and_its_dialog_leaves_a_populated_main_screen_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    engine, bus = WorkflowEngine(), EventBus()
    requests = await _requests(bus)
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
        await open_workflow(main, pilot, "demo-workflow")
        run_id = await _accept_live_run(pilot, engine, bus, requests)
        footer = main.query_one(ChrysFooter)
        await wait_for(
            lambda: (
                not main._workflow._refresh_pending
                and main._execution_binding_busy
                and not footer._binding_recompose_in_progress
                and not footer._binding_recompose_dirty
                and footer._visible_binding_signature == footer._binding_signature(main)
                and screen_is_settled(app, main)
            ),
            pilot=pilot,
        )
        settled = asyncio.Event()
        main.call_after_refresh(settled.set)
        await wait_for(settled.is_set, pilot=pilot)
        # The Output tab loads the finished run's outputs on its own and relayouts when they arrive;
        # holding that read leaves the spies with what Result and its dialog do.
        output_read, release_output = Event(), Event()
        original_read = workflow_content.read_node_output

        def held_read(directory: Path, activation_id: str, attempt: int, *, node_kind: str = "") -> Any:
            output_read.set()
            assert release_output.wait(timeout=10)
            return original_read(directory, activation_id, attempt, node_kind=node_kind)

        monkeypatch.setattr(workflow_content, "read_node_output", create_autospec(original_read, side_effect=held_read))
        layout = create_autospec(main._refresh_layout, side_effect=main._refresh_layout)
        styles = create_autospec(main.update_node_styles, side_effect=main.update_node_styles)
        recompose = create_autospec(footer.recompose, side_effect=footer.recompose)
        monkeypatch.setattr(main, "_refresh_layout", layout)
        monkeypatch.setattr(main, "update_node_styles", styles)
        monkeypatch.setattr(footer, "recompose", recompose)
        result = _result(main)
        try:
            await bus.publish(_finished(run_id))
            await wait_for(
                lambda: result.visible and output_read.is_set() and not main._workflow._refresh_pending, pilot=pilot
            )
            dialog = await _open_result(app, pilot)
            assert value_text(_views(dialog)[0]) == f"{_SUMMARY_ONLY}\nTour summary"
            await pilot.press("escape")
            await wait_for(lambda: app.screen is main and screen_is_settled(app, main), pilot=pilot)
            assert layout.call_count == styles.call_count == recompose.call_count == 0
        finally:
            release_output.set()
        outputs = main._workflow_panel.query_one("#workflow-outputs", Static)
        await wait_for(lambda: "Tour summary" in str(outputs.content), pilot=pilot)
        assert [card.is_attached for card in cards] == [True] * len(cards)


async def test_the_result_view_documents_hold_the_values_read() -> None:
    documents = WorkflowResultDialog(
        (
            events.WorkflowOutputSummary("live", "live@1", "Live summary", 1),
            events.WorkflowOutputSummary("archived", "archived@1", "", 1),
        ),
        kinds={},
        directory=None,
        outcome="completed",
    )._read()
    assert documents == [
        ValueDocument((ShownValue("Live summary"),), notice=_SUMMARY_ONLY),
        ValueDocument(placeholder="No record available."),
    ]


def test_an_unreadable_record_shows_its_error_above_the_live_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read = create_autospec(workflow_result.read_node_output, side_effect=OSError("[red]disk\x1b[2J failed"))
    monkeypatch.setattr(workflow_result, "read_node_output", read)
    documents = WorkflowResultDialog(
        (events.WorkflowOutputSummary("live", "live@1", "Live summary", 2),),
        kinds={"live": "python"},
        directory=tmp_path,
        outcome="completed",
    )._read()
    read.assert_called_once_with(tmp_path, "live@1", 2, node_kind="python")
    assert documents == [ValueDocument((ShownValue("Live summary"),), notice="[red]disk�[2J failed")]
