# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Stable graph geometry, incremental ink and bounded visible-row rendering."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import create_autospec

import pytest
from rich.cells import cell_len
from textual.app import App, ComposeResult

from chrys.app.tui.widgets.markdown.diagram import canvas as canvas_module
from chrys.app.tui.widgets.markdown.diagram import compile_mermaid
from chrys.app.tui.widgets.markdown.diagram.canvas import CellRow, TerminalCanvas, crop_cell_text
from chrys.app.tui.widgets.markdown.diagram.layout import compile_ir_with_geometry
from chrys.app.tui.widgets.markdown.diagram.model import Direction
from chrys.app.tui.widgets.markdown.diagram.parser import parse_mermaid
from chrys.app.tui.widgets.markdown.diagram.router import draw_edges, draw_nodes
from chrys.app.tui.widgets.workflow import graph as graph_module
from chrys.app.tui.widgets.workflow import text
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.app.tui.widgets.workflow.node_view import NodeView, RetryTarget
from chrys.service.workflows.transcript import NodeUsage
from tests.support.waiting import wait_for


class GraphHarness(App):
    def compose(self) -> ComposeResult:
        yield WorkflowGraph()


@pytest.mark.parametrize("direction", [Direction.LEFT_RIGHT, Direction.TOP_DOWN])
@pytest.mark.parametrize("count", [40, 120])
async def test_usage_never_recompiles_or_changes_preview_fallback(
    direction: Direction, count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {
        "nodes": [{"id": f"n{i}", "kind": "agent", "agent": {"profile": "QA"}} for i in range(count)],
        "edges": [{"src": f"n{i}", "dst": f"n{i + 1}"} for i in range(count - 1)],
    }
    async with GraphHarness().run_test(size=(80, 24)) as pilot:
        graph = pilot.app.query_one(WorkflowGraph)
        graph.direction = direction
        graph.show_manifest(manifest, [])
        await wait_for(lambda: graph.allow_horizontal_scroll or graph.allow_vertical_scroll, pilot=pilot)
        graph.select_node("n30")
        await wait_for(lambda: graph.scroll_offset != (0, 0), pilot=pilot)
        diagram, geometry, scroll, fallback = graph.diagram, graph.geometry, graph.scroll_offset, graph.list_fallback
        compile_graph = create_autospec(graph._compile_manifest, side_effect=graph._compile_manifest)
        monkeypatch.setattr(graph, "_compile_manifest", compile_graph)
        views = {}
        for i in range(8):
            views[f"n{i}"] = NodeView("running", usage=NodeUsage(i, i * 1000))
            graph.show_nodes(views)
            assert graph.diagram is diagram and graph.geometry is geometry
            assert graph.scroll_offset == scroll and graph.selected_node == "n30"
            assert graph.list_fallback == fallback
        compile_graph.assert_not_called()


@pytest.mark.parametrize("direction", [Direction.LEFT_RIGHT, Direction.TOP_DOWN])
async def test_only_changed_nodes_and_incident_routes_restyle_with_shared_ink_priority(
    direction: Direction, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {
        "nodes": [{"id": node, "kind": "python"} for node in ("source", "left", "right", "end")],
        "edges": [
            {"src": "source", "dst": "left"},
            {"src": "source", "dst": "right"},
            {"src": "left", "dst": "end"},
            {"src": "right", "dst": "end"},
        ],
    }
    async with GraphHarness().run_test(size=(120, 40)) as pilot:
        graph = pilot.app.query_one(WorkflowGraph)
        graph.direction = direction
        graph.show_manifest(manifest, [])
        views = {"source": NodeView("completed"), "left": NodeView("running"), "right": NodeView("running")}
        graph.show_nodes(views)
        graph.select_node("left")
        await wait_for(lambda: not graph._layout_required, pilot=pilot)
        spans = create_autospec(graph._node_spans, side_effect=graph._node_spans)
        palette = create_autospec(graph._theme_palette, side_effect=graph._theme_palette)
        routes = create_autospec(graph._restyle_edges, side_effect=graph._restyle_edges)
        monkeypatch.setattr(graph, "_node_spans", spans)
        monkeypatch.setattr(graph, "_theme_palette", palette)
        monkeypatch.setattr(graph, "_restyle_edges", routes)
        unchanged = graph._node_styles["left"]
        titles = graph._title_spans
        views["right"] = NodeView("skipped")
        graph.show_nodes(views)
        spans.assert_called_once_with("right")
        routes.assert_called_once_with({1, 3})
        palette.assert_not_called()
        assert graph._title_spans is titles and graph._node_styles["left"] is unchanged
        # Shared junctions must retain the active branch when another becomes skipped.
        shared = 0
        for y, cells in graph._edge_rows.items():
            running = {cell.x for cell in cells if graph._edge_states[cell.edge] == "running"}
            skipped = {cell.x for cell in cells if graph._edge_states[cell.edge] == "skipped"}
            for x in running & skipped:
                shared += 1
                assert any(
                    span.start <= x < span.end and span.style == graph._palette["workflow-edge--running"]
                    for span in graph._edge_styles[y]
                )
        assert shared
        # Compare the incremental result with a complete restyle after the same updates.
        incremental = dict(graph.cell_styles)
        graph._rebuild_styles()
        assert graph.cell_styles == incremental
        spans.reset_mock()
        routes.reset_mock()
        graph.select_node("right")
        assert {call.args[0] for call in spans.call_args_list} == {"left", "right"}
        routes.assert_not_called()
        palette.assert_not_called()
        views["right"] = NodeView("unrecognized")
        graph.show_nodes(views)
        assert graph._edge_states[1] == "pending"
        graph._set_hovered_node("")
        graph.select_node("left")
        box = graph.geometry["right"]
        assert graph._node_styles["right"][box.y][0].style == graph._palette["workflow-node--pending"]


async def test_large_list_formats_finished_labels_once_and_only_styles_requested_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = datetime(2026, 9, 20, tzinfo=UTC)
    clock = create_autospec(datetime)
    clock.now.return_value = started
    monkeypatch.setattr(graph_module, "datetime", clock)
    manifest = {"nodes": [{"id": f"n{i}", "kind": "python"} for i in range(5000)]}
    async with GraphHarness().run_test(size=(100, 20)) as pilot:
        graph = pilot.app.query_one(WorkflowGraph)
        graph.show_manifest(manifest, [])
        views = {f"n{i}": NodeView("completed", elapsed_seconds=85) for i in range(5000)}
        views["n2"] = NodeView("running", running_since=started)
        views["n4998"] = NodeView("awaiting_retry", retry=RetryTarget("run", "n4998", 1))
        style = create_autospec(graph._node_style, side_effect=graph._node_style)
        monkeypatch.setattr(graph, "_node_style", style)
        graph.show_nodes(views)
        style.assert_not_called()
        await wait_for(lambda: graph.virtual_size.height == 5000 and not graph._layout_required, pilot=pilot)
        assert graph.list_fallback and graph.cell_styles == {} and graph._node_styles == {}
        label = create_autospec(text.elapsed_label, side_effect=text.elapsed_label)
        elapsed = create_autospec(graph._update_elapsed, side_effect=graph._update_elapsed)
        row = create_autospec(graph._list_row, side_effect=graph._list_row)
        monkeypatch.setattr(text, "elapsed_label", label)
        monkeypatch.setattr(graph, "_update_elapsed", elapsed)
        monkeypatch.setattr(graph, "_list_row", row)
        style.reset_mock()
        for tenth in range(1, 10):
            clock.now.return_value = started + timedelta(seconds=tenth / 10)
            graph.advance_animation()
        assert elapsed.call_count == 9 and {call.args[0] for call in elapsed.call_args_list} == {"n2"}
        # A sub-second run relabels as it ticks, in milliseconds: once per tenth of a second here.
        assert label.call_count == 9
        assert graph._elapsed_labels["n2"] == "900 ms"
        row.assert_not_called()
        clock.now.return_value = started + timedelta(seconds=1)
        graph.advance_animation()
        assert label.call_count == 10 and graph._elapsed_labels["n2"] == "1 second"
        clock.now.return_value = started + timedelta(seconds=1.5)
        graph.advance_animation()
        # Above a second, a half-second tick recomputes the label but it still reads the same.
        assert label.call_count == 11 and graph._elapsed_labels["n2"] == "1 second"
        graph._set_hovered_node("n3")
        row.assert_not_called()
        style.assert_not_called()
        rendered = graph.render_line(3)
        row.assert_called_once_with("n3")
        style.assert_called_once_with("n3")
        assert "n3" in rendered.text and "1 minute 25 seconds" in rendered.text
        assert graph._elapsed_labels["n4999"] == "1 minute 25 seconds"


def test_overlay_composition_preserves_wide_boundaries_and_precedence() -> None:
    base = "a用户👩🏽\u200d💻e\u0301" * 8
    overlays = [(3, "好🙂"), (4, "XY"), (12, " e\u0301 "), (90, "outside")]
    row = CellRow(base)
    expected = crop_cell_text(base, 0, 120)
    for x, overlay in overlays:
        end = x + cell_len(overlay)
        expected = crop_cell_text(expected, 0, x) + overlay + crop_cell_text(expected, end, 120 - end)
    for start in range(100):
        actual = row.composite(start, 17, overlays)
        assert actual == crop_cell_text(expected, start, 17)
        assert cell_len(actual) == 17


@pytest.mark.parametrize("direction", list(Direction))
def test_shared_compiler_trims_cycle_margins_and_translates_every_annotation(direction: Direction) -> None:
    source = f"flowchart {direction.value}\n" + "\n".join(
        [f"n{i}[节点{i}] --> n{i + 1}[节点{i + 1}]" for i in range(5)] + [f"n{i} -->|返回{i}| n0" for i in range(1, 6)]
    )
    diagram, boxes = compile_ir_with_geometry(source, parse_mermaid(source))
    assert not diagram.diagnostics
    assert compile_mermaid(source) == diagram
    assert diagram.rows[0].strip() and diagram.rows[-1].strip()
    assert min(len(row) - len(row.lstrip(" ")) for row in diagram.rows if row.strip()) == 0
    canvas = TerminalCanvas()
    draw_edges(canvas, list(diagram.routed_edges))
    draw_nodes(canvas, boxes.values())
    assert canvas.rows(diagram.width, diagram.height) == diagram.rows
    for route in diagram.routed_edges:
        for point in route.points:
            assert 0 <= point.x < diagram.width and 0 <= point.y < diagram.height
        if route.label_at is not None:
            point = route.label_at
            assert diagram.crop_plain_row(point.y, point.x, cell_len(route.edge.label)) == route.edge.label


@pytest.mark.parametrize("count", [1, 120], ids=["centered", "wide-scrolled"])
async def test_overlay_rendering_walks_only_visible_text_and_uses_centered_scroll_origin(
    count: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {
        "nodes": [{"id": f"n{i}", "kind": "python", "callable": {"name": "读取"}} for i in range(count)],
        "edges": [{"src": f"n{i}", "dst": f"n{i + 1}"} for i in range(count - 1)],
    }
    async with GraphHarness().run_test(size=(80, 24)) as pilot:
        graph = pilot.app.query_one(WorkflowGraph)
        graph.show_manifest(manifest, [])
        graph.show_nodes({f"n{i}": NodeView("running") for i in range(count)})
        await wait_for(lambda: not graph._layout_required, pilot=pilot)
        if count > 1:
            await wait_for(lambda: graph.max_scroll_x > 0, pilot=pilot)
            graph.scroll_to(x=2000, animate=False, force=True, immediate=True)
        assert not graph.list_fallback
        top = graph.geometry["n0"].y
        expected = graph.diagram.rows[top]
        for box in graph.geometry.values():
            x = box.x + 2
            expected = (
                crop_cell_text(expected, 0, x)
                + f" {graph._spinner()} "
                + crop_cell_text(expected, x + 3, graph.diagram.width - x - 3)
            )
        width = graph.scrollable_content_region.width
        expected = " " * graph.diagram_origin.x + crop_cell_text(
            expected, round(graph.scroll_offset.x), width - graph.diagram_origin.x
        )
        graphemes = create_autospec(canvas_module.iter_graphemes, side_effect=canvas_module.iter_graphemes)
        monkeypatch.setattr(canvas_module, "iter_graphemes", graphemes)
        rendered = graph.render_line(top + graph.diagram_origin.y - round(graph.scroll_offset.y))
        assert rendered.text == expected
        assert graphemes.call_count
        assert all(cell_len(call.args[0]) <= width for call in graphemes.call_args_list)
