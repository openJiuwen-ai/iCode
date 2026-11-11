# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Node hover follows cell geometry without changing the workflow selection."""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.geometry import Offset
from textual.screen import ModalScreen
from textual.widgets import Static

from chrys.app.tui.widgets.markdown.diagram.model import Direction
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.app.tui.widgets.workflow.node_view import NodeView
from tests.support.waiting import wait_for


@pytest.mark.parametrize("count", [2, 4, 201], ids=["centered", "scrolled", "list"])
@pytest.mark.parametrize("direction", [Direction.LEFT_RIGHT, Direction.TOP_DOWN])
async def test_hover_highlights_without_selecting_or_reflowing(
    count: int, direction: Direction, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {
        "nodes": [{"id": f"node{index}", "kind": "python", "callable": {"name": "读取"}} for index in range(count)],
        "edges": [{"src": f"node{index}", "dst": f"node{index + 1}"} for index in range(count - 1)],
    }
    opened: list[str] = []

    class Harness(App):
        CSS = """
        WorkflowGraph { margin: 1; border: solid $primary; }
        #outside { height: 1; }
        """

        def compose(self) -> ComposeResult:
            yield WorkflowGraph()
            yield Static(Text("Outside graph"), id="outside")

        def on_mount(self) -> None:
            graph = self.query_one(WorkflowGraph)
            graph.direction = direction
            graph.show_manifest(manifest, [])
            graph.show_nodes({f"node{count - 1}": NodeView("completed")})

        @on(WorkflowGraph.NodeSelected)
        def node_selected(self, event: WorkflowGraph.NodeSelected) -> None:
            opened.append(event.node_id)
            self.push_screen(ModalScreen())

    size = (140, 40) if count == 2 else (50, 16)
    async with Harness().run_test(size=size) as pilot:
        graph = pilot.app.query_one(WorkflowGraph)
        assert not graph.selected_node
        graph.select_node("node0")
        target = f"node{count - 1}"
        box = graph.geometry[target]
        graph.scroll_to(x=max(0, box.x - 2), y=max(0, box.y - 2), animate=False, force=True, immediate=True)
        offset = (
            graph.scrollable_content_region.offset
            - graph.region.offset
            + graph.diagram_origin
            - graph.scroll_offset
            + Offset(box.x + 1, box.y + (0 if graph.list_fallback else 1))
        )
        diagram, geometry, scroll = graph.diagram, graph.geometry, graph.scroll_offset
        styles = create_autospec(graph._restyle_nodes, side_effect=graph._restyle_nodes)
        compile_manifest = create_autospec(graph._compile_manifest, side_effect=graph._compile_manifest)
        monkeypatch.setattr(graph, "_restyle_nodes", styles)
        monkeypatch.setattr(graph, "_compile_manifest", compile_manifest)

        def highlighted(node: str) -> bool:
            node_box = graph.geometry[node]
            inset = 0 if graph.list_fallback else 1
            fill = graph.get_component_rich_style(
                "workflow-node--selected" if node == graph.selected_node else "workflow-node--hover"
            ).bgcolor
            if graph.list_fallback:
                return graph._node_style(node)[0].bgcolor == fill
            return any(
                span.start == node_box.x + inset and span.style.bgcolor == fill
                for span in graph.cell_styles[node_box.y + inset]
            )

        assert await pilot.hover(graph, offset=offset)
        await wait_for(lambda: highlighted(target), pilot=pilot)
        assert graph.selected_node == "node0"
        assert highlighted("node0")
        assert opened == []
        row_y = box.y + (0 if graph.list_fallback else 1) + graph.diagram_origin.y - graph.scroll_offset.y
        assert any(
            segment.style and segment.style.bgcolor == graph.get_component_rich_style("workflow-node--hover").bgcolor
            for segment in graph.render_line(row_y)
        )
        assert graph.diagram is diagram and graph.geometry is geometry and graph.scroll_offset == scroll
        compile_manifest.assert_not_called()

        # Moving within one node should not rebuild even its style spans.
        styles.reset_mock()
        assert await pilot.hover(graph, offset=offset + Offset(1, 0))
        styles.assert_not_called()
        assert await pilot.hover("#outside")
        await wait_for(lambda: not highlighted(target), pilot=pilot)
        assert highlighted("node0")

        assert await pilot.click(graph, offset=offset)
        await wait_for(lambda: opened == [target], pilot=pilot)
        await wait_for(lambda: isinstance(pilot.app.screen, ModalScreen) and pilot.app.screen.is_mounted, pilot=pilot)
        assert graph.selected_node == target and graph._hovered_node == ""
        pilot.app.pop_screen()
        await wait_for(lambda: pilot.app.screen is graph.screen, pilot=pilot)
        assert highlighted(target)
        assert await pilot.hover("#outside")
        assert graph.selected_node == target and highlighted(target)
        assert not highlighted("node0")

        # Keyboard activation retains the same navigation anchor.
        graph.select_node(target)
        graph.focus()
        await pilot.press("enter")
        await wait_for(lambda: opened == [target, target], pilot=pilot)
        await wait_for(lambda: isinstance(pilot.app.screen, ModalScreen) and pilot.app.screen.is_mounted, pilot=pilot)
        pilot.app.pop_screen()
        await wait_for(lambda: pilot.app.screen is graph.screen, pilot=pilot)
        assert graph.selected_node == target and highlighted(target)
        graph.focus()
        await pilot.press("k")
        assert graph.selected_node == f"node{count - 2}"


@pytest.mark.parametrize("direction", [Direction.LEFT_RIGHT, Direction.TOP_DOWN])
async def test_stationary_hover_tracks_scroll_and_blank_space(direction: Direction) -> None:
    manifest = {
        "nodes": [{"id": f"node{index}", "kind": "python", "callable": {"name": "prepare"}} for index in range(3)],
        "edges": [{"src": "node0", "dst": "node1"}, {"src": "node1", "dst": "node2"}],
    }

    class Harness(App):
        def compose(self) -> ComposeResult:
            yield WorkflowGraph()

        def on_mount(self) -> None:
            graph = self.query_one(WorkflowGraph)
            graph.direction = direction
            graph.show_manifest(manifest, [])
            graph.show_nodes({})

    async with Harness().run_test(size=(50, 10)) as pilot:
        graph = pilot.app.query_one(WorkflowGraph)
        assert not graph.selected_node
        graph.select_node("node0")
        first, second = graph.geometry["node0"], graph.geometry["node1"]
        offset = Offset(first.x + 1, first.y + 1) + graph.diagram_origin
        assert await pilot.hover(graph, offset=offset)
        await wait_for(lambda: graph._hovered_node == "node0", pilot=pilot)
        graph.scroll_to(x=second.x - first.x, y=second.y - first.y, animate=False, force=True, immediate=True)
        await wait_for(lambda: graph._hovered_node == "node1", pilot=pilot)
        assert graph.selected_node == "node0"

        # Blank canvas clears the transient highlight before a click clears selection.
        assert await pilot.hover(graph, offset=(0, 0))
        await wait_for(lambda: graph._hovered_node == "", pilot=pilot)
        assert graph.selected_node == "node0"
        assert await pilot.click(graph, offset=(0, 0))
        assert graph.selected_node == ""
