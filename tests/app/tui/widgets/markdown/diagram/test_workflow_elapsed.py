# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Running-node footers stay inside their boxes and reuse the retry position."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import create_autospec

import pytest
from rich.cells import cell_len
from textual.app import App, ComposeResult

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.widgets.workflow import graph as graph_module
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.app.tui.widgets.workflow.node_view import NodeView, RetryTarget
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.types import WorkflowNodeStateChanged


@pytest.mark.parametrize("count", [1, 4, 201], ids=["centered", "scrolled", "list"])
@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_running_footer_formats_and_repaints_without_relayout(
    count: int, locale: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = datetime(2026, 9, 16, tzinfo=UTC)
    clock = create_autospec(datetime)
    clock.now.return_value = started
    monkeypatch.setattr(graph_module, "datetime", clock)
    node_id = f"node{count - 1}"
    manifest = {
        "nodes": [
            {"id": f"node{index}", "kind": "python", "callable": {"name": "读取" + "x" * (50 if count == 4 else 1)}}
            for index in range(count)
        ],
        "edges": [{"src": f"node{index}", "dst": f"node{index + 1}"} for index in range(count - 1)],
    }

    class Harness(App):
        def compose(self) -> ComposeResult:
            yield WorkflowGraph()

        def on_mount(self) -> None:
            self.animation_level = "none"
            graph = self.query_one(WorkflowGraph)
            graph.show_manifest(manifest, [], locale=LocaleController(Settings(locale=locale)))
            graph.show_nodes({node_id: NodeView("running", running_since=started)})

    async with Harness().run_test(size=(120, 20) if count == 1 else (38, 10)) as pilot:
        graph = pilot.app.query_one(WorkflowGraph)
        graph.select_node(node_id)
        box = graph.geometry[node_id]
        diagram, geometry = graph.diagram, graph.geometry
        for seconds, english, chinese in (
            (0.412, "412 ms", "412 毫秒"),
            (1, "1 second", "1 秒"),
            (30, "30 seconds", "30 秒"),
            (85, "1 minute 25 seconds", "1 分钟 25 秒"),
            (3599, "59 minutes 59 seconds", "59 分钟 59 秒"),
            (7380, "2 hours 3 minutes", "2 小时 3 分钟"),
            (180000, "2 days 2 hours", "2 天 2 小时"),
        ):
            clock.now.return_value = started + timedelta(seconds=seconds)
            graph.advance_animation()
            label = english if locale == "en" else chinese
            assert graph._elapsed_labels[node_id] == label
            if graph.list_fallback:
                x = (
                    cell_len(graph._list_rows[node_id])
                    + len("  ")
                    + cell_len(" · running · " if locale == "en" else " · 运行中 · ")
                )
            else:
                assert cell_len(label) <= box.width - 4
                x = box.x + (box.width - cell_len(label)) // 2
            graph.scroll_to(
                x=max(0, x - 3), y=max(0, box.y + box.height - 4), animate=False, force=True, immediate=True
            )
            y = box.y + box.height - 1 + graph.diagram_origin.y - round(graph.scroll_offset.y)
            row = graph.render_line(y)
            assert label in row.text
            assert not any(segment.style and segment.style.underline for segment in row)
            assert graph.diagram is diagram and graph.geometry is geometry

        failure = WorkflowNodeStateChanged(run_id="run", node_id=node_id, state="awaiting_retry")
        graph.show_nodes(
            {
                node_id: NodeView(
                    "awaiting_retry", retry=RetryTarget(failure.run_id, failure.activation_id, failure.attempt)
                )
            }
        )
        assert node_id not in graph._elapsed_labels
        assert graph._retry_regions[node_id].y == box.y + box.height - 1
        graph.show_nodes({node_id: NodeView("cancelled")})
        assert not graph._elapsed_labels and not graph._retry_regions
