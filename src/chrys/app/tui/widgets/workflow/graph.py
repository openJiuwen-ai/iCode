# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Manifest-to-diagram projection and selectable workflow graph viewport."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from rich.cells import cell_len
from rich.style import Style
from textual.events import Click, Leave, MouseDown, MouseMove, MouseRelease, MouseUp
from textual.geometry import Offset, Region
from textual.message import Message
from textual.strip import Strip

from chrys.app.tui.binding_display import localized_binding
from chrys.app.tui.util.formatting import format_elapsed
from chrys.app.tui.util.invocation_progress import invocation_progress_parts
from chrys.app.tui.util.spinner import SPINNER_FRAMES, STATIC_SPINNER
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen
from chrys.app.tui.widgets.markdown.diagram.canvas import CellRow, CellStyleSpan, crop_cell_text
from chrys.app.tui.widgets.markdown.diagram.layout import compile_ir_with_geometry
from chrys.app.tui.widgets.markdown.diagram.model import (
    CompiledDiagram,
    DiagramEdge,
    DiagramIR,
    DiagramKind,
    DiagramNode,
    Direction,
    EdgeStyle,
    NodeShape,
    PlacedNode,
)
from chrys.app.tui.widgets.markdown.diagram.viewer import DiagramViewport
from chrys.app.tui.widgets.workflow import text
from chrys.app.tui.widgets.workflow.edges import EdgeCell, connection_state, edge_cells, ink_spans, pulse_strength
from chrys.app.tui.widgets.workflow.node_view import NodeView, RetryTarget
from chrys.service.workflows.graph import AgentSpec
from chrys.service.workflows.transcript import NodeUsage

if TYPE_CHECKING:
    from chrys.app.tui.i18n import LocaleController

_DEFAULT_NODE = NodeView()


MAX_DIAGRAM_NODES = 200


def _elapsed_width(locale: LocaleController | None) -> int:
    """Reserve footer space once so elapsed labels never reflow the graph."""
    return max(cell_len(text.elapsed_label(seconds, locale)) for seconds in (0.999, 59, 3599, 86340, 86396400))


def usage_label(usage: NodeUsage, locale: LocaleController | None = None) -> str:
    return " · ".join(
        invocation_progress_parts(
            tool_calls=usage.tool_calls,
            usage_tokens=usage.usage_tokens,
            unreported_attempts=usage.unreported_attempts,
            include_zero=True,
            render=lambda message: text.render(message, locale),
        )
    )


def _usage_width(locale: LocaleController | None) -> int:
    return cell_len(usage_label(NodeUsage(9999999, 999900000), locale)) + 6


def node_detail(
    node: dict[str, Any], resolved: dict[str, Any] | None = None, *, locale: LocaleController | None = None
) -> str:
    kind = node["kind"]
    if kind == "agent":
        agent = AgentSpec.from_manifest(node["agent"])
        profile = resolved["agent_display_name"] if resolved else agent.profile
        model = resolved["model_id"] if resolved else agent.model
        return f"{profile} · {model or text.render(text.DEFAULT_MODEL.bind(), locale)}"
    if kind in {"python", "join"}:
        function = (node.get("callable") or {}).get("name", "")
        return f"py · {function}" if kind == "python" else function
    if kind == "loop":
        return text.iteration_label(0, node["loop"]["max_iterations"], locale)
    return kind


def manifest_ir(
    manifest: dict[str, Any],
    resolved_nodes: Sequence[dict[str, Any]] = (),
    *,
    locale: LocaleController | None = None,
    direction: Direction = Direction.LEFT_RIGHT,
    reserve_usage: bool = False,
) -> DiagramIR:
    """Expand structured loops to entry/body/exit edges, preserving switch conditions.

    ``reserve_usage`` gives every agent node the row and width of a usage label. A run sets it for
    all of them up front, so usage arriving later never moves the graph; a definition without a
    run has nothing to show there and stays compact.
    """
    resolved = {node["node_id"]: node for node in resolved_nodes}
    nodes = manifest.get("nodes", [])
    loops = {node["id"]: node["loop"] for node in nodes if node["kind"] == "loop"}
    switches = {edge["src"] for edge in manifest.get("edges", []) if edge.get("switch")}
    boxes = []
    min_width = _elapsed_width(locale) + 4
    usage_width = _usage_width(locale)
    for node in nodes:
        usage = reserve_usage and node["kind"] == "agent"
        shape = (
            NodeShape.ROUNDED
            if node["kind"] == "loop"
            else NodeShape.DECISION
            if node["id"] in switches
            else NodeShape.ROUNDED
        )
        boxes.append(
            DiagramNode(
                node["id"],
                node["id"],
                shape,
                sections=((node_detail(node, resolved.get(node["id"]), locale=locale),),) + (("",),) * usage,
                min_width=max(min_width, usage_width) if usage else min_width,
            )
        )
    edges = []
    for edge in manifest.get("edges", []):
        src = edge["src"]
        if src in loops:
            src = loops[src]["exit"]
        predicate = edge.get("predicate")
        edges.append(
            DiagramEdge(
                src,
                edge["dst"],
                # Reserve the label's padding in layout, routing and edge animation.
                f" {predicate} " if predicate else "",
                style=EdgeStyle.DOTTED if edge.get("conditional") else EdgeStyle.SOLID,
            )
        )
    for loop_id, loop in loops.items():
        edges.append(DiagramEdge(loop_id, loop["entry"]))
        edges.append(DiagramEdge(loop["exit"], loop_id, style=EdgeStyle.DOTTED, constrains_rank=False))
    return DiagramIR(DiagramKind.FLOWCHART, direction, tuple(boxes), tuple(edges))


class WorkflowGraph(DiagramViewport):
    """A graph keeps its geometry stable while lifecycle events recolor node boxes."""

    # Dragging pans the canvas; the diagram rows are not selectable text.
    ALLOW_SELECT = False

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        component for state in text.STATES.values() for component in (state.node_component, state.edge_component)
    } | {
        "workflow-node--title",
        "workflow-node--detail",
        "workflow-node--selected",
        "workflow-node--hover",
        "workflow-node--active-fill",
        "workflow-pulse--1",
        "workflow-pulse--2",
        "workflow-pulse--3",
    }
    DEFAULT_CSS = """
    WorkflowGraph { padding: 0 0 0 1; scrollbar-size-vertical: 2; }
    WorkflowGraph .workflow-node--pending { color: $text-muted; }
    WorkflowGraph .workflow-node--running { color: $accent; text-style: bold; }
    WorkflowGraph .workflow-node--success { color: $success; }
    WorkflowGraph .workflow-node--error { color: $error; text-style: bold; }
    WorkflowGraph .workflow-node--warning { color: $warning; }
    WorkflowGraph .workflow-node--skipped { color: $text-muted; text-style: dim; }
    WorkflowGraph .workflow-node--title { color: $primary; text-style: bold; }
    WorkflowGraph .workflow-node--detail { color: $text-muted; }
    WorkflowGraph .workflow-node--selected { color: $primary; background: $primary 16%; }
    WorkflowGraph .workflow-node--hover { color: $primary; background: $primary 9%; }
    WorkflowGraph .workflow-node--active-fill { background: $accent 5%; }
    WorkflowGraph .workflow-edge--pending { color: $text-muted 45%; }
    WorkflowGraph .workflow-edge--skipped { color: $text-muted 25%; }
    WorkflowGraph .workflow-edge--running { color: $accent 45%; }
    WorkflowGraph .workflow-edge--completed { color: $success 70%; }
    WorkflowGraph .workflow-edge--failed, WorkflowGraph .workflow-edge--awaiting_retry { color: $error; }
    WorkflowGraph .workflow-edge--cancelled, WorkflowGraph .workflow-edge--retrying { color: $warning; }
    WorkflowGraph .workflow-pulse--1 { color: $accent 70%; }
    WorkflowGraph .workflow-pulse--2 { color: $accent; text-style: bold; }
    WorkflowGraph .workflow-pulse--3 { color: $text; text-style: bold; }
    """

    BINDINGS: ClassVar[list] = [
        localized_binding("j", "next_node", text.NEXT_NODE, show=False),
        localized_binding("k", "previous_node", text.PREVIOUS_NODE, show=False),
        localized_binding("enter", "open_node", text.OPEN_NODE, show=False),
        localized_binding("r", "retry_node", text.RETRY, show=False),
    ]

    class NodeSelected(Message):
        def __init__(self, node_id: str) -> None:
            super().__init__()
            self.node_id = node_id

    class RetryRequested(Message):
        def __init__(self, node_id: str, target: RetryTarget) -> None:
            super().__init__()
            self.node_id = node_id
            self.target = target

    def __init__(self) -> None:
        diagram, self.geometry = compile_ir_with_geometry("", manifest_ir({}))
        super().__init__(diagram, id="workflow-graph", center_diagram=True)
        self.selected_node = ""
        self._hovered_node = ""
        self._views: dict[str, NodeView] = {}
        self._palette: dict[str, Style] = {}
        self._badge_positions: dict[str, tuple[int, int, int]] = {}
        self._badges: dict[str, str] = {}
        self._list_rows: dict[str, str] = {}
        self._node_ids: list[str] = []
        self._node_indexes: dict[str, int] = {}
        self._locale: LocaleController | None = None
        self._animation_frame = 0
        self._edge_rows = edge_cells(diagram, self.geometry)
        self._edge_states: list[str] = []
        self._incident_edges: dict[str, set[int]] = {}
        self._route_rows: dict[int, set[int]] = {}
        self._flow_rows: dict[int, tuple[EdgeCell, ...]] = {}
        self._edge_styles: dict[int, tuple[CellStyleSpan, ...]] = {}
        self._node_styles: dict[str, dict[int, tuple[CellStyleSpan, ...]]] = {}
        self._nodes_by_row: dict[int, list[str]] = {}
        self._title_spans: dict[str, dict[int, tuple[int, int]]] = {}
        self._canvas_rows = tuple(CellRow(row) for row in diagram.rows)
        self._retry_regions: dict[str, Region] = {}
        self._retry_label = ""
        self._running_nodes: set[str] = set()
        self._elapsed_labels: dict[str, str] = {}
        self._elapsed_display_ms: dict[str, int] = {}
        self._usage_labels: dict[str, str] = {}
        self.direction = Direction.LEFT_RIGHT
        self._manifest: dict[str, Any] = {}
        self._resolved_nodes: list[dict[str, Any]] = []
        self._reserve_usage = False
        # Screen offset and scroll offset where the left button went down, while it is held.
        self._pan_origin: tuple[Offset, Offset] | None = None
        # This press has moved the canvas, so the click it ends in is not a click.
        self._pan_moved = False

    def on_mount(self) -> None:
        # Keep the one-column track at the right edge, with a blank column before it.
        self.vertical_scrollbar.styles.padding = (0, 0, 0, 1)

    @property
    def list_fallback(self) -> bool:
        return bool(self._list_rows)

    def show_manifest(
        self,
        manifest: dict[str, Any],
        resolved_nodes: list[dict[str, Any]],
        *,
        locale: LocaleController | None = None,
        reserve_usage: bool = False,
    ) -> None:
        self._manifest = manifest
        self._resolved_nodes = resolved_nodes
        self._reserve_usage = reserve_usage
        self._locale = locale
        self._views.clear()
        self._badges.clear()
        self._running_nodes.clear()
        self._elapsed_labels.clear()
        self._elapsed_display_ms.clear()
        self._usage_labels.clear()
        self.selected_node = ""
        self._retry_label = text.render(text.RETRY.bind(), locale)
        self._compile_manifest()
        self.show_iterations({})

    def _compile_manifest(self) -> None:
        manifest, resolved_nodes, locale = self._manifest, self._resolved_nodes, self._locale
        self._hovered_node = ""
        self._badge_positions.clear()
        self._list_rows.clear()
        nodes = manifest.get("nodes", [])
        if len(nodes) <= MAX_DIAGRAM_NODES:
            diagram, self.geometry = compile_ir_with_geometry(
                "",
                manifest_ir(
                    manifest,
                    resolved_nodes,
                    locale=locale,
                    direction=self.direction,
                    reserve_usage=self._reserve_usage,
                ),
            )
        if len(nodes) > MAX_DIAGRAM_NODES or (nodes and not self.geometry):
            resolved = {node["node_id"]: node for node in resolved_nodes}
            self._list_rows = {
                node["id"]: node["id"]
                if node["kind"] == "loop"
                else f"{node['id']} · {node_detail(node, resolved.get(node['id']), locale=locale)}"
                for node in nodes
            }
            width = (
                max(map(cell_len, self._list_rows.values()))
                + max(32, _elapsed_width(locale) + 16)
                + _usage_width(locale)
                + 32
            )
            self.geometry = {
                node_id: PlacedNode(DiagramNode(node_id, node_id), 0, y, width, 1, (label,))
                for y, (node_id, label) in enumerate(self._list_rows.items())
            }
            diagram = CompiledDiagram(
                "", DiagramKind.FLOWCHART, width, len(self._list_rows), tuple(self._list_rows.values())
            )
        self.set_diagram(diagram)
        self._node_ids = list(self.geometry)
        self._node_indexes = {node_id: index for index, node_id in enumerate(self._node_ids)}
        self._canvas_rows = () if self.list_fallback else tuple(CellRow(row) for row in diagram.rows)
        self._nodes_by_row = {}
        self._title_spans = {}
        for node_id, box in self.geometry.items():
            titles = self._title_spans[node_id] = {}
            for y in range(box.y, box.y + box.height):
                self._nodes_by_row.setdefault(y, []).append(node_id)
                if not self.list_fallback and box.y < y <= box.y + (
                    box.section_breaks[0] if box.section_breaks else box.height - 2
                ):
                    row = self._canvas_rows[y].composite(box.x + 1, box.width - 2)
                    label = row.strip()
                    if label:
                        start = box.x + 1 + cell_len(row) - cell_len(row.lstrip())
                        titles[y] = (start, start + cell_len(label))
        self._edge_rows = edge_cells(diagram, self.geometry)
        self._incident_edges = {}
        self._route_rows = {}
        for index, route in enumerate(diagram.routed_edges):
            for node_id in (route.edge.source, route.edge.target):
                self._incident_edges.setdefault(node_id, set()).add(index)
        for y, cells in self._edge_rows.items():
            for cell in cells:
                self._route_rows.setdefault(cell.edge, set()).add(y)
        self._edge_states = ["pending"] * len(diagram.routed_edges)
        if not self.list_fallback:
            for node in nodes:
                if node["kind"] != "loop" or node["id"] not in self.geometry:
                    continue
                box = self.geometry[node["id"]]
                badge = text.iteration_label(0, node["loop"]["max_iterations"], locale)
                for y in range(box.y, box.y + box.height):
                    row = self._canvas_rows[y].composite(box.x, box.width)
                    start = row.find(badge)
                    if start >= 0:
                        self._badge_positions[node["id"]] = (box.x + cell_len(row[:start]), y, box.x + box.width - 2)
                        break
        self._elapsed_display_ms.clear()
        self._retry_regions.clear()
        now = datetime.now(UTC)
        for node_id in self._views:
            self._update_labels(node_id, now)
        self._rebuild_styles()
        if self.is_mounted:
            self.call_after_refresh(self._refresh_hover)

    def toggle_layout(self) -> None:
        """Rebuild only the geometry, retaining selection and live presentation facts."""
        self.direction = Direction.LEFT_RIGHT if self.direction == Direction.TOP_DOWN else Direction.TOP_DOWN
        self._compile_manifest()
        self.call_after_refresh(self._scroll_to_selected_node)

    def show_iterations(self, iterations: Mapping[str, tuple[int, int]]) -> None:
        changed = set()
        for node in self._manifest.get("nodes", []):
            if node["kind"] == "loop":
                node_id = node["id"]
                iteration, maximum = iterations.get(node_id, (0, node["loop"]["max_iterations"]))
                label = text.iteration_label(iteration, maximum, self._locale)
                if label != self._badges.get(node_id):
                    self._badges[node_id] = label
                    changed.add(node_id)
        self._restyle_nodes(changed)

    def _node_style(self, node_id: str) -> tuple[Style, Style]:
        view = self._views.get(node_id, _DEFAULT_NODE)
        style = self._palette[text.state_presentation(view.state).node_component]
        fill = (
            "workflow-node--selected"
            if node_id == self.selected_node
            else "workflow-node--hover"
            if node_id == self._hovered_node
            else "workflow-node--active-fill"
            if view.state == "running"
            else ""
        )
        background = self._palette[fill].bgcolor if fill else None
        if node_id == self.selected_node or node_id == self._hovered_node:
            style += Style(color=self._palette[fill].color)
        detail = Style(
            color=self._palette["workflow-node--detail"].color,
            bgcolor=background,
            bold=False,
            dim=view.state == "skipped",
        )
        return style + Style(bgcolor=background) if self.list_fallback else style, detail

    def _list_row(self, node_id: str) -> tuple[str, Region | None]:
        """Build text and its retry span together, only when a row is needed."""
        view = self._views.get(node_id, _DEFAULT_NODE)
        spinner = self._spinner() if view.state == "running" else " "
        row = f"{spinner} {self._list_rows[node_id]} · {text.state_label(view.state, self._locale)}"
        for label in (self._badges.get(node_id), self._usage_labels.get(node_id)):
            if label:
                row += f" · {label}"
        box = self.geometry[node_id]
        region = None
        if view.retry is not None and view.state == "awaiting_retry":
            row += " · "
            region = Region(cell_len(row), box.y, cell_len(self._retry_label), 1)
            row += self._retry_label
        elif label := self._elapsed_labels.get(node_id):
            row += f" · {label}"
        return row, region

    def render_line(self, y: int) -> Strip:
        content_y = y + round(self.scroll_offset.y) - self.diagram_origin.y
        if not 0 <= content_y < self.diagram.height:
            return super().render_line(y)
        if self.list_fallback:
            node_id = self._node_ids[content_y]
            row, retry = self._list_row(node_id)
            style, _ = self._node_style(node_id)
            spans = [CellStyleSpan(0, self.diagram.width, style)]
            if retry is not None:
                pending = self._views[node_id].retry_pending
                spans.append(CellStyleSpan(retry.x, retry.right, Style(underline=not pending, dim=pending)))
            return self.render_diagram_row(row, content_y, overlays=tuple(spans))
        start = max(0, round(self.scroll_offset.x) - self.diagram_origin.x)
        width = self.scrollable_content_region.width - max(0, self.diagram_origin.x - round(self.scroll_offset.x))
        end = start + width
        overlays = []
        for node_id in self._nodes_by_row.get(content_y, ()):
            box = self.geometry[node_id]
            if box.x >= end or box.x + box.width <= start:
                continue
            position = self._badge_positions.get(node_id)
            if position is not None and position[1] == content_y and node_id in self._badges:
                x, _, right = position
                overlays.append((x, crop_cell_text(self._badges[node_id], 0, right - x)))
            if box.y == content_y:
                state = self._views.get(node_id, _DEFAULT_NODE).state
                marker = self._spinner() if state == "running" else text.state_presentation(state).marker
                overlays.append((box.x + 2, f" {marker} "))
            if (region := self._retry_regions.get(node_id)) is not None and region.y == content_y:
                overlays.append((region.x - 1, f" {self._retry_label} "))
            elif box.y + box.height - 1 == content_y and (label := self._elapsed_labels.get(node_id)):
                x = box.x + (box.width - cell_len(label)) // 2
                overlays.append((x - 1, f" {label} "))
            usage_row = self._reserve_usage and box.y + box.height - 2 == content_y
            if usage_row and (label := self._usage_labels.get(node_id)):
                label = crop_cell_text(label, 0, box.width - 4).rstrip()
                x = box.x + (box.width - cell_len(label)) // 2
                overlays.append((x, label))
        row = self._canvas_rows[content_y].composite(start, width, overlays)
        return self.render_diagram_row(row, content_y, overlays=self._pulse_spans(content_y), row_start=start)

    def _pulse_spans(self, y: int) -> tuple[CellStyleSpan, ...]:
        if self.app.animation_level == "none":
            return ()
        strengths: dict[int, int] = {}
        for cell in self._flow_rows.get(y, ()):
            strengths[cell.x] = max(strengths.get(cell.x, 0), pulse_strength(cell.distance, self._animation_frame))
        return ink_spans(
            {x: self._palette[f"workflow-pulse--{strength}"] for x, strength in strengths.items() if strength}
        )

    def _spinner(self) -> str:
        return (
            STATIC_SPINNER
            if self.app.animation_level == "none"
            else SPINNER_FRAMES[(self._animation_frame // 2) % len(SPINNER_FRAMES)]
        )

    def advance_animation(self) -> None:
        """Tick active nodes and connections; finished labels and offscreen rows stay cached."""
        if not is_widget_shown_on_active_screen(self):
            return
        now = datetime.now(UTC)
        animate = self.app.animation_level != "none"
        rows: set[int] = set()
        visible = Region(
            round(self.scroll_offset.x) - self.diagram_origin.x,
            round(self.scroll_offset.y) - self.diagram_origin.y,
            self.scrollable_content_region.width,
            self.scrollable_content_region.height,
        )
        if animate:
            for y, cells in self._flow_rows.items():
                if visible.y <= y < visible.bottom and any(visible.x <= cell.x < visible.right for cell in cells):
                    rows.add(y)
        for node_id in self._running_nodes:
            changed = self._update_elapsed(node_id, now)
            box = self.geometry[node_id]
            if box.x + box.width <= visible.x or box.x >= visible.right:
                continue
            if animate:
                rows.add(box.y)
            if changed:
                rows.add(box.y + box.height - 1)
        if animate and rows:
            self._animation_frame = (self._animation_frame + 1) % 80
        self._refresh_rows(rows)

    def _update_elapsed(self, node_id: str, now: datetime) -> bool:
        """Refresh the node's elapsed label, deduped at millisecond resolution.

        A sub-second node relabels each tick; a longer one only when its label changes,
        which is once a second.
        """
        view = self._views.get(node_id, _DEFAULT_NODE)
        if view.elapsed_seconds is None and view.running_since is None:
            self._elapsed_display_ms.pop(node_id, None)
            return self._elapsed_labels.pop(node_id, None) is not None
        elapsed = view.elapsed_seconds or 0.0
        if view.state == "running" and view.running_since is not None:
            elapsed += max(0.0, (now - view.running_since).total_seconds())
        millis = int(max(0.0, elapsed) * 1000)
        if self._elapsed_display_ms.get(node_id) == millis:
            return False
        self._elapsed_display_ms[node_id] = millis
        label = text.elapsed_label(elapsed, self._locale)
        box = self.geometry[node_id]
        if not self.list_fallback and cell_len(label) > box.width - 4:
            label = crop_cell_text(format_elapsed(elapsed), 0, box.width - 4).rstrip()
        changed = self._elapsed_labels.get(node_id) != label
        self._elapsed_labels[node_id] = label
        return changed

    def _update_labels(self, node_id: str, now: datetime, previous: NodeView | None = None) -> None:
        view = self._views.get(node_id, _DEFAULT_NODE)
        self._update_elapsed(node_id, now)
        if previous is None or view.usage != previous.usage:
            if view.usage is None:
                self._usage_labels.pop(node_id, None)
            else:
                self._usage_labels[node_id] = usage_label(view.usage, self._locale)

    def notify_style_update(self) -> None:
        super().notify_style_update()
        if self.is_mounted:
            self.call_later(self._refresh_theme)

    def _theme_palette(self) -> dict[str, Style]:
        return {name: self.get_component_rich_style(name) for name in self.COMPONENT_CLASSES}

    def _refresh_theme(self) -> None:
        palette = self._theme_palette()
        if palette != self._palette:
            self._palette = palette
            self._rebuild_styles()

    def show_nodes(self, views: Mapping[str, NodeView]) -> None:
        changed = {
            node_id
            for node_id in self._views.keys() | views.keys()
            if node_id in self.geometry and self._views.get(node_id, _DEFAULT_NODE) != views.get(node_id, _DEFAULT_NODE)
        }
        if not changed:
            return
        previous = self._views
        self._views = {node_id: view for node_id, view in views.items() if node_id in self.geometry}
        now = datetime.now(UTC)
        state_changed = set()
        for node_id in changed:
            view, old = self._views.get(node_id, _DEFAULT_NODE), previous.get(node_id, _DEFAULT_NODE)
            if view.state == "running":
                self._running_nodes.add(node_id)
            else:
                self._running_nodes.discard(node_id)
            if view.state != old.state:
                state_changed.add(node_id)
            self._update_labels(node_id, now, old)
        edge_rows = self._restyle_edges(
            {index for node_id in state_changed for index in self._incident_edges.get(node_id, ())}
        )
        self._restyle_nodes(changed, edge_rows)

    def _rebuild_styles(self) -> None:
        if not self._palette:
            self._palette = self._theme_palette()
        self._node_styles.clear()
        self._edge_styles.clear()
        self._flow_rows.clear()
        edge_rows = self._restyle_edges(set(range(len(self.diagram.routed_edges))))
        if self.list_fallback:
            for node_id, view in self._views.items():
                if view.retry is not None:
                    self._update_retry_region(node_id)
            self.refresh()
        else:
            self._restyle_nodes(set(self.geometry), edge_rows)

    def _restyle_edges(self, indexes: set[int]) -> set[int]:
        rows = set()
        for index in indexes:
            edge = self.diagram.routed_edges[index].edge
            self._edge_states[index] = connection_state(
                self._views.get(edge.source, _DEFAULT_NODE).state, self._views.get(edge.target, _DEFAULT_NODE).state
            )
            rows.update(self._route_rows.get(index, ()))
        for y in rows:
            by_x: dict[int, str] = {}
            active = []
            for cell in self._edge_rows[y]:
                state = self._edge_states[cell.edge]
                if state == "running":
                    active.append(cell)
                if (
                    cell.x not in by_x
                    or text.state_presentation(state).edge_priority
                    > text.state_presentation(by_x[cell.x]).edge_priority
                ):
                    by_x[cell.x] = state
            if active:
                self._flow_rows[y] = tuple(active)
            else:
                self._flow_rows.pop(y, None)
            self._edge_styles[y] = ink_spans(
                {x: self._palette[text.state_presentation(state).edge_component] for x, state in by_x.items()}
            )
        return rows

    def _update_retry_region(self, node_id: str) -> None:
        view = self._views.get(node_id, _DEFAULT_NODE)
        self._retry_regions.pop(node_id, None)
        if view.retry is None or view.state != "awaiting_retry":
            return
        box = self.geometry[node_id]
        if self.list_fallback:
            _, region = self._list_row(node_id)
        else:
            width = cell_len(self._retry_label)
            region = (
                Region(box.x + (box.width - width) // 2, box.y + box.height - 1, width, 1)
                if width <= box.width - 4
                else None
            )
        if region is not None:
            self._retry_regions[node_id] = region

    def _restyle_nodes(self, nodes: set[str], edge_rows: set[int] | None = None) -> None:
        rows = set() if edge_rows is None else set(edge_rows)
        for node_id in nodes:
            box = self.geometry.get(node_id)
            if box is None:
                continue
            self._update_retry_region(node_id)
            rows.update(range(box.y, box.y + box.height))
            if self.list_fallback:
                continue
            self._node_styles[node_id] = self._node_spans(node_id)
        if not self.list_fallback:
            for y in rows:
                self.cell_styles[y] = (
                    *self._edge_styles.get(y, ()),
                    *(
                        span
                        for node_id in self._nodes_by_row.get(y, ())
                        for span in self._node_styles[node_id].get(y, ())
                    ),
                )
        self._refresh_rows(rows)

    def _node_spans(self, node_id: str) -> dict[int, tuple[CellStyleSpan, ...]]:
        box = self.geometry[node_id]
        view = self._views.get(node_id, _DEFAULT_NODE)
        style, detail = self._node_style(node_id)
        rows = {}
        for y in range(box.y, box.y + box.height):
            spans = [CellStyleSpan(box.x, box.x + box.width, style)]
            if box.y < y < box.y + box.height - 1:
                spans.append(CellStyleSpan(box.x + 1, box.x + box.width - 1, detail))
                if title_span := self._title_spans[node_id].get(y):
                    title = self._palette["workflow-node--title"]
                    component = text.state_presentation(view.state).node_component
                    color = (
                        style.color
                        if component in {"workflow-node--success", "workflow-node--warning", "workflow-node--error"}
                        else title.color
                    )
                    spans.append(
                        CellStyleSpan(*title_span, Style(color=color, bold=title.bold, dim=view.state == "skipped"))
                    )
            region = self._retry_regions.get(node_id)
            if region is not None and region.y == y:
                spans.append(
                    CellStyleSpan(
                        region.x, region.right, style + Style(underline=not view.retry_pending, dim=view.retry_pending)
                    )
                )
            rows[y] = tuple(spans)
        return rows

    def _refresh_rows(self, rows: set[int]) -> None:
        top = self.diagram_origin.y - round(self.scroll_offset.y)
        for y in rows:
            visible_y = y + top
            if 0 <= visible_y < self.scrollable_content_region.height:
                self.refresh(Region(0, visible_y, self.size.width, 1))

    def select_node(self, node_id: str) -> None:
        if node_id not in self.geometry:
            return
        previous = self.selected_node
        self.selected_node = node_id
        self._scroll_to_selected_node()
        if previous != node_id:
            self._restyle_nodes({previous, node_id})

    def _scroll_to_selected_node(self) -> None:
        box = self.geometry.get(self.selected_node)
        if box is not None:
            self.scroll_to_region(
                Region(box.x + self.diagram_origin.x, box.y + self.diagram_origin.y, box.width, box.height),
                animate=False,
            )

    def _move(self, delta: int) -> None:
        if self._node_ids:
            index = self._node_indexes.get(self.selected_node)
            self.select_node(
                self._node_ids[(index + delta) % len(self._node_ids)]
                if index is not None
                else self._node_ids[0 if delta > 0 else -1]
            )

    def action_next_node(self) -> None:
        self._move(1)

    def action_previous_node(self) -> None:
        self._move(-1)

    def action_open_node(self) -> None:
        if self.selected_node:
            self._set_hovered_node("")
            self.post_message(self.NodeSelected(self.selected_node))

    def action_retry_node(self) -> None:
        view = self._views.get(self.selected_node, _DEFAULT_NODE)
        if view.retry is not None and view.state == "awaiting_retry" and not view.retry_pending:
            self.post_message(self.RetryRequested(self.selected_node, view.retry))

    def _diagram_offset(self, screen_offset: Offset) -> Offset | None:
        region = self.scrollable_content_region
        if screen_offset not in region:
            return None
        return screen_offset - region.offset + self.scroll_offset - self.diagram_origin

    def _node_at(self, offset: Offset | None) -> str:
        if offset is not None:
            for node_id in self._nodes_by_row.get(offset.y, ()):
                box = self.geometry[node_id]
                if Region(box.x, box.y, box.width, box.height).contains(*offset):
                    return node_id
        return ""

    @property
    def _panning(self) -> bool:
        return self._pan_origin is not None and self._pan_moved

    def _set_hovered_node(self, node_id: str) -> None:
        pointer = "grabbing" if self._panning else "pointer" if node_id else "default"
        if self.styles.pointer != pointer:
            self.styles.pointer = pointer
        if node_id != self._hovered_node:
            previous = self._hovered_node
            self._hovered_node = node_id
            self._restyle_nodes({previous, node_id})

    def _refresh_hover(self) -> None:
        """Recheck a stationary pointer after scrolling or changing the graph geometry."""
        hovering = self.app.mouse_over is self and not self._panning
        offset = self._diagram_offset(self.app.mouse_position) if hovering else None
        self._set_hovered_node(self._node_at(offset))

    def on_mouse_down(self, event: MouseDown) -> None:
        self._pan_moved = False
        if event.button == 1:
            self._pan_origin = (event.screen_offset, self.scroll_offset)
            self.capture_mouse()

    def on_mouse_move(self, event: MouseMove) -> None:
        if self._pan_origin is None:
            self._set_hovered_node(self._node_at(self._diagram_offset(event.screen_offset)))
            return
        start, scroll = self._pan_origin
        delta = event.screen_offset - start
        if not self._pan_moved and delta.is_origin:
            return
        if not self._pan_moved:
            self._pan_moved = True
            self._set_hovered_node("")
        self.scroll_to(scroll.x - delta.x, scroll.y - delta.y, animate=False, immediate=True)

    def on_mouse_up(self, event: MouseUp) -> None:
        self._end_pan()

    def on_mouse_release(self, event: MouseRelease) -> None:
        """Another widget took the mouse, or this graph released it."""
        self._end_pan()

    def on_blur(self) -> None:
        self._end_pan()

    def on_unmount(self) -> None:
        self._pan_origin = None
        self.release_mouse()

    def _end_pan(self) -> None:
        if self._pan_origin is None:
            return
        self._pan_origin = None
        self.release_mouse()
        self._refresh_hover()

    def on_leave(self, event: Leave) -> None:
        if event.node is self:
            self._set_hovered_node("")

    def on_resize(self) -> None:
        self.call_after_refresh(self._refresh_hover)

    def watch_scroll_x(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_x(old_value, new_value)
        if self.is_mounted and round(old_value) != round(new_value):
            self._refresh_hover()

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        if self.is_mounted and round(old_value) != round(new_value):
            self._refresh_hover()

    def on_click(self, event: Click) -> None:
        if self._pan_moved:
            # The press dragged the canvas: it neither opens a node nor clears the selection.
            self._pan_moved = False
            event.prevent_default()
            event.stop()
            return
        offset = self._diagram_offset(event.screen_offset)
        # Padding is blank canvas; scrollbar clicks still belong to native controls.
        if offset is None and event.screen_offset in self.content_region:
            return
        node_id = self._node_at(offset)
        region = self._retry_regions.get(node_id)
        if region is not None and offset is not None and offset in region:
            event.prevent_default()
            event.stop()
            self.select_node(node_id)
            self.action_retry_node()
            return
        if node_id := self._node_at(offset):
            event.prevent_default()
            event.stop()
            self.select_node(node_id)
            self.action_open_node()
            return
        if self.selected_node:
            event.prevent_default()
            event.stop()
            previous = self.selected_node
            self.selected_node = ""
            self._restyle_nodes({previous})
