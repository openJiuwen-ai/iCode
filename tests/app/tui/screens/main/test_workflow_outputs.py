# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Complete, paged outputs through the archived-run UI and artifact reader."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, Static

from chrys.app.tui.screens.main import workflow_content
from chrys.service.workflows.store import RunRecord, node_value_path
from tests.app.tui.screens.main._workflow_support import select_archived_run
from tests.orchestration.workflows._hosting import make_workdir
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

from ._workflow_support import WorkflowEngine, open_workflow, run_store, select_workflow_view, workflow_selection

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


@pytest.mark.parametrize("first_result", ["long", "long_line", "damaged", "missing"])
async def test_all_output_nodes_remain_accessible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_result: str
) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        session_id = str(uuid4())
        main._workflow.session_view.selection = workflow_selection(main, session_id)
        directory = main._workflow.session_view._session_dir()
        assert directory is not None
        run_directory = directory / "workflows" / uuid4().hex
        store = run_store(run_directory, preview, session_id=session_id, started_at="2026-01-01")
        body = "完整文本" * 9000 if first_result == "long_line" else "完整文本\n" * 7000
        first = "First [literal]\n" + body + "first tail"
        second = "Second [literal] output survives"
        try:
            for node, value in (("first", first), ("second", second)):
                await store.append(
                    RunRecord.NODE_STATE,
                    {
                        "node": node,
                        "activation": node,
                        "attempt": 1,
                        "state": "completed",
                        "iteration": 0,
                        "failure_phase": "",
                    },
                )
                store.write_node_value(node, 1, "output", {"value": {"text": value}})
            if first_result == "damaged":
                node_value_path(run_directory, "first", 1, "output").write_text(
                    '{"value": "invalid"}', encoding="utf-8"
                )
            elif first_result == "missing":
                node_value_path(run_directory, "first", 1, "output").unlink()
            await store.finish(
                "completed",
                {"outputs": [{"node": node, "activation": node, "attempt": 1} for node in ("first", "second")]},
            )
        finally:
            await store.close()
        read_output = create_autospec(workflow_content.read_node_output, side_effect=workflow_content.read_node_output)
        monkeypatch.setattr(workflow_content, "read_node_output", read_output)
        await select_archived_run(main, pilot, store.header.run_id)
        await wait_for(lambda: app.screen is main and main._workflow_panel.run_id == store.header.run_id, pilot=pilot)
        await select_workflow_view(main, pilot, "output")
        panel = main._workflow_panel
        output = panel.query_one("#workflow-outputs", Static)
        output_scroll = panel.query_one("#workflow-outputs-scroll", VerticalScroll)
        await wait_for(lambda: str(output.content).startswith("Outputs\nfirst\n"), pilot=pilot)
        pager = panel.query_one("#workflow-output-pages")
        if first_result.startswith("long"):
            assert pager.display
            assert panel.query_one("#workflow-output-pages #previous-page", Button).disabled
            chunks = [str(output.content).removeprefix("Outputs\n")]
            next_page = panel.query_one("#workflow-output-pages #next-page", Button)
            page_label = panel.query_one("#workflow-output-pages #page-number", Static)
            # Each click is a user navigation operation, not a readiness poll.
            while not next_page.disabled:
                await wait_for(lambda: next_page.region.height > 0 and not next_page.has_class("-active"), pilot=pilot)
                before = page_label.content
                await click_when_settled(pilot, "#workflow-output-pages #next-page")
                await wait_for(lambda before=before: page_label.content != before, pilot=pilot)
                assert app.focused is output_scroll
                chunks.append(str(output.content).removeprefix("Outputs\n"))
            assert "".join(chunks) == f"first\n{first}\n\nsecond\n{second}"
            assert max(map(len, chunks)) <= 32_768
            assert all(len(chunk) >= 16_384 for chunk in chunks[:-1])
            assert "first tail" in str(output.content) and second in str(output.content)
            # Repainting a terminal run, changing tabs, or changing locale retains the page.
            final_page = str(output.content).removeprefix("Outputs\n")
            main._workflow.refresh()
            app.locale_controller.switch_locale("zh-Hans")
            await wait_for(
                lambda: "页" in str(panel.query_one("#workflow-output-pages #page-number", Static).content), pilot=pilot
            )
            assert str(output.content).endswith(final_page)
            await click_when_settled(pilot, "#workflow-output-pages #previous-page")
            await wait_for(lambda: not str(output.content).endswith(final_page), pilot=pilot)
            assert app.focused is output_scroll
            assert output.region.width > 0
            assert pager.region.bottom <= panel.region.bottom
            app.save_screenshot("workflow-output-pages.svg", path=str(tmp_path))
        else:
            assert not pager.display
            expected = "Invalid workflow output record" if first_result == "damaged" else "Full output is unavailable"
            assert expected in str(output.content)
            assert second in str(output.content)
            if first_result == "missing":
                app.locale_controller.switch_locale("zh-Hans")
                await wait_for(lambda: "无法读取完整输出" in str(output.content), pilot=pilot)
                assert second in str(output.content)
        assert read_output.call_count == 2
        panel.clear_outputs()
        assert not pager.display and not str(output.content)
