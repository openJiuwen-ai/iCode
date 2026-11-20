# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared Info stays readable and shows the selected preview or recorded run without executing source."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from threading import Event
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from rich.table import Table
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Static, TabbedContent, TabPane

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.screens.dialogs.workflow_confirm import WorkflowConfirmDialog
from chrys.app.tui.screens.main import workflow_content
from chrys.app.tui.widgets.workflow.info import WorkflowInfo
from chrys.foundation.config.settings import Settings
from chrys.service.workflows.layout import SPEC_FILE
from tests.orchestration.workflows._hosting import make_workdir, write_workflow
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled, resize_when_settled, rich_plain
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workflow_history import record_workflow_run
from tests.support.workflow_workers import python_workflow

from ._workflow_support import WorkflowEngine, open_workflow, run_store, save_workflow_session, select_workflow_view

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


def _values(info: WorkflowInfo) -> list[str]:
    return [str(widget.content) for widget in info.query(Static)]


def _nodes(info: WorkflowInfo) -> str:
    table = info.query_one("#workflow-info-nodes", Static).content
    assert isinstance(table, Table)
    return rich_plain(table)


@pytest.mark.parametrize(
    ("locale", "theme", "size"),
    [("en", "chrys", (160, 48)), ("zh-Hans", "chrys-ansi", (100, 34)), ("en", "textual-light", (80, 30))],
)
async def test_loaded_info_reuses_metadata_and_preserves_reading_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locale: str, theme: str, size: tuple[int, int]
) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), settings=Settings(locale=locale, theme=theme))
    async with app.run_test(size=size) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        panel = main._workflow_panel
        read = create_autospec(main._workflow.browser.catalog.preview)
        monkeypatch.setattr(main._workflow.browser.catalog, "preview", read)
        await select_workflow_view(main, pilot, "info")
        tabs = panel.query_one("#workflow-run", TabbedContent)
        assert [pane.id for pane in tabs.query(TabPane)] == [
            "workflow-graph-tab",
            "workflow-info-tab",
            "workflow-code-tab",
            "workflow-input-tab",
            "workflow-output-tab",
        ]
        info = panel.query_one(WorkflowInfo)
        # The view's sections mount one after another, and the fingerprints are the last of them.
        await wait_for(
            lambda: bool(info.query("#workflow-info-nodes")) and preview.spec_digest in _values(info), pilot=pilot
        )
        assert info.data is not None and not info.data.requires_trust
        values = _values(info)
        assert preview.title in values
        assert preview.environment.python_version in values
        assert preview.environment.executable in values
        assert preview.load.entry_digest in values and preview.spec_digest in values
        nodes = _nodes(info)
        assert all(node["id"] in nodes for node in preview.manifest["nodes"])
        assert not info.query_one(Collapsible).collapsed
        scroll = panel.query_one("#workflow-info-scroll", VerticalScroll)
        # Mounting the content precedes scrollbar and layout negotiation.
        await wait_for(lambda: scroll.max_scroll_x == 0, pilot=pilot, description="Info layout fits horizontally")
        assert info.styles.padding.left == info.styles.padding.right == 1
        app.save_screenshot(f"workflow-info-{locale}-{size[0]}.svg", path=str(tmp_path))
        panel.set_preview_models(
            [{"node_id": "architecture", "agent_display_name": "Review [literal]", "model_id": "model [literal]"}]
        )
        await wait_for(
            lambda: bool(info.query("#workflow-info-nodes")) and "model [literal]" in _nodes(info), pilot=pilot
        )
        # Tall content may overflow at the old height too: scroll only once laid out at the new one.
        await resize_when_settled(pilot, size[0], 24)
        await wait_for(lambda: scroll.max_scroll_y > 0, pilot=pilot)
        scroll.scroll_end(animate=False)
        await wait_for(lambda: scroll.scroll_y > 0, pilot=pilot)
        position = scroll.scroll_y
        verification = info.query_one(Collapsible)
        await select_workflow_view(main, pilot, "code")
        await select_workflow_view(main, pilot, "info")
        assert info.query_one(Collapsible) is verification and not verification.collapsed
        assert scroll.scroll_y == position
        assert read.call_count == 0
        verification.collapsed = True
        await wait_for(lambda: info._verification_collapsed, pilot=pilot)
        panel.set_preview_models(
            [{"node_id": "architecture", "agent_display_name": "Reviewer", "model_id": "updated model"}]
        )
        await wait_for(
            lambda: bool(info.query("#workflow-info-nodes")) and "updated model" in _nodes(info), pilot=pilot
        )
        assert info.query_one(Collapsible).collapsed
        # A real locale switch updates both tab and content without reloading source.
        app.locale_controller.switch_locale("en" if locale == "zh-Hans" else "zh-Hans")
        label = "Execution Environment" if locale == "zh-Hans" else "运行环境"
        await wait_for(lambda: label in _values(info), pilot=pilot)
        assert info.query_one(Collapsible).collapsed
        assert tabs.get_tab("workflow-info-tab").label_text == ("Info" if locale == "zh-Hans" else "信息")
        await pilot.press("escape")
        await wait_for(lambda: panel.graph_visible, pilot=pilot)
        assert app.screen is main and read.call_count == 0


async def test_info_uses_selected_runs_saved_environment_and_discards_late_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_workdir(tmp_path)
    monkeypatch.chdir(project)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    entered, release = Event(), Event()
    async with app.run_test(size=(140, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        main.action_workflow()
        state_store = main._services.state_store
        assert state_store is not None
        session_id = str(uuid4())
        directory = state_store.session_dir(session_id)
        run_ids = [uuid4().hex, uuid4().hex]
        for number, run_id in enumerate(run_ids):
            run_dir = directory / "workflows" / run_id
            await record_workflow_run(run_dir, session_id=session_id, title=f"Archived {number}", outcome="completed")
            spec_path = run_dir / SPEC_FILE
            spec = json.loads(spec_path.read_text())
            spec["environment"] = {
                "mode": "byo",
                "python_version": f"3.9.{number}",
                "executable": f"/saved/[literal]/{number}/python",
            }
            spec_path.write_text(json.dumps(spec))
        await save_workflow_session(state_store, session_id, project)
        await main._workflow.session_view.restore_session(session_id, run_ids[0])
        await wait_for(lambda: app.screen is main, pilot=pilot)
        rendered = asyncio.Event()
        main.call_after_refresh(rendered.set)
        await wait_for(rendered.is_set, pilot=pilot)
        original = workflow_content.read_run_spec

        def delayed_read(path: Path) -> dict:
            if path.name == run_ids[0]:
                entered.set()
                assert release.wait(10), "test did not release the first Info read"
            return original(path)

        read = create_autospec(original, side_effect=delayed_read)
        monkeypatch.setattr(workflow_content, "read_run_spec", read)
        preview = create_autospec(main._workflow.browser.catalog.preview)
        monkeypatch.setattr(main._workflow.browser.catalog, "preview", preview)
        try:
            await select_workflow_view(main, pilot, "info")
            await wait_for(entered.is_set, pilot=pilot)
            await main._workflow.session_view.select_run(run_ids[1])
            info = main._workflow_panel.query_one(WorkflowInfo)
            # New data recomposes the view, and its sections mount one after another.
            expected = {"/saved/[literal]/1/python", "Archived 1", "3.9.1", "e" * 64}
            await wait_for(
                lambda: expected <= set(_values(info)) and bool(info.query("#workflow-info-nodes")), pilot=pilot
            )
            assert "check" in _nodes(info)
            release.set()
            tasks = tuple(main._workflow._tasks)
            if tasks:
                await asyncio.gather(*tasks)
            assert info.data is not None and info.data.title == "Archived 1"
            assert info.data.interpreter == "/saved/[literal]/1/python"
            await select_workflow_view(main, pilot, "code")
            await select_workflow_view(main, pilot, "info")
            assert read.call_count == 2  # Successful reads are reused when switching tabs.
            assert preview.call_count == 0  # The original file is absent; history still needs no trust or execution.
            app.save_screenshot("workflow-info-history.svg", path=str(tmp_path))
        finally:
            release.set()


async def test_new_preview_cannot_replace_selected_runs_info_or_poison_its_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_workdir(tmp_path)
    monkeypatch.chdir(project)
    write_workflow(
        project, "sample", python_workflow("def original(value):\n    return value\n", "original", title="Original R1")
    )
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(125, 42)) as pilot:
        main = app._main_screen
        assert main is not None
        original = await open_workflow(main, pilot, "sample")
        session_id, run_id = str(uuid4()), uuid4().hex
        state_store = main._services.state_store
        assert state_store is not None
        directory = state_store.session_dir(session_id)
        store = run_store(
            directory / "workflows" / run_id, original, session_id=session_id, started_at="2026-09-20T00:00:00+00:00"
        )
        await store.finish("completed", {"outputs": []})
        await store.close()
        await save_workflow_session(state_store, session_id, project)
        await main._workflow.session_view.restore_session(session_id, run_id)
        await wait_for(lambda: app.screen is main, pilot=pilot)
        panel = main._workflow_panel
        panel.query_one("#workflow-run", TabbedContent).active = "workflow-info-tab"
        content = main._workflow.session_view.content
        content.view_changed()
        # The record is read off-thread and reaches the panel on the refresh after that.
        await wait_for(lambda: content._info_data is not None and panel.info_data is content._info_data, pilot=pilot)
        old_info = panel.info_data
        write_workflow(
            project,
            "sample",
            python_workflow("def changed(value):\n    return value\n", "changed", title="Changed preview"),
        )
        main._workflow.browser.load_preview("sample")
        await wait_for(
            lambda: isinstance(app.screen, WorkflowConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        await click_when_settled(pilot, "#workflow-confirm-yes")
        await wait_for(
            lambda: app.screen is main and not panel.previewing and content._info_data is not None,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        assert panel.run_id == run_id and panel.info_visible
        assert panel.preview is not None and panel.preview.title == "Changed preview"
        for _ in range(3):
            main._workflow.refresh()
            assert panel.info_data == old_info
        assert panel.info_data is not None
        assert panel.info_data.source_digest == original.source.source_digest
        assert panel.info_data.spec_digest == original.spec_digest != panel.preview.spec_digest
        assert panel.info_data.title == "Original R1"
        assert panel.info_data.manifest == original.manifest
        # Showing this preview as a fresh draft adopts all of its Info together.
        panel.show_preview(panel.preview)
        assert panel.info_data.run_id == "" and panel.info_data.title == "Changed preview"
        assert panel.info_data.source_digest == panel.preview.source.source_digest
        assert panel.info_data.spec_digest == panel.preview.spec_digest
