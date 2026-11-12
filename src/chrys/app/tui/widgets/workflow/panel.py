# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Selection and run views. Operations and modal ownership belong to the main screen."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.events import Click
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Button, Static, Tab, TabbedContent, TabPane, Tabs

from chrys.app.tui.clipboard import copy_text_to_clipboards
from chrys.app.tui.copy_messages import COPIED_TITLE
from chrys.app.tui.util.formatting import format_elapsed
from chrys.app.tui.util.git_branch import format_workspace_subtitle
from chrys.app.tui.util.logo import WORKFLOW_LOGO
from chrys.app.tui.util.message_gate import messages_disabled
from chrys.app.tui.util.removal import finish_shielded
from chrys.app.tui.util.source_text import sanitize_source_text
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen, set_widget_visibility_without_layout
from chrys.app.tui.widgets.chat.messages import COPY_MESSAGE_BUTTON, MessageCopyButton, MessageHeaderRow
from chrys.app.tui.widgets.dialog_buttons import DialogButtonRow, DialogButtonSpec
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.app.tui.widgets.markdown.diagram.model import Direction
from chrys.app.tui.widgets.markdown.parser import create_user_text_markdown_parser
from chrys.app.tui.widgets.welcome import WelcomeWidget
from chrys.app.tui.widgets.workflow import text
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.app.tui.widgets.workflow.info import INFO_TAB, WorkflowInfo, WorkflowInfoData
from chrys.app.tui.widgets.workflow.node_view import NodeView, RetryTarget
from chrys.app.tui.widgets.workflow.output import WorkflowOutputText, WorkflowOutputView, WorkflowStatusOutput
from chrys.app.tui.widgets.workflow.scrollbar import WorkflowScrollBar
from chrys.app.tui.widgets.workflow.source import WorkflowSourceSyntax
from chrys.foundation.util.session_ids import session_short_id
from chrys.service.workflows.graph import AgentSpec, manifest_warnings

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.widgets.workflow.projector import ObservedRun
    from chrys.orchestration.workflows.preview import WorkflowPreview


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    """Definition currently displayed, which may differ from the next prepared run."""

    workflow_id: str = ""
    source_kind: str = ""
    manifest: dict = field(default_factory=dict)

    agents: dict[str, AgentSpec] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "agents",
            {
                node["id"]: AgentSpec.from_manifest(node["agent"])
                for node in self.manifest.get("nodes", [])
                if node["kind"] == "agent"
            },
        )


class WorkflowPanel(Vertical):
    workspace_cwd: reactive[str] = reactive("", layout=False)

    DEFAULT_CSS = """
    WorkflowPanel {
        display: none; min-width: 35; min-height: 3; height: 1fr; width: 1fr;
        border: round $tui-border-primary $border-opacity;
        border-title-align: left; border-title-color: $primary;
        border-subtitle-align: right; border-subtitle-color: $primary;
        padding: 0;
    }
    WorkflowPanel #workflow-manifest-warnings { height: auto; max-height: 4; overflow-y: auto; }
    WorkflowPanel #workflow-run-tabs { height: 2; display: none; }
    WorkflowPanel #workflow-empty { height: auto; display: none; }
    WorkflowPanel #workflow-run { height: 1fr; }
    WorkflowPanel.-empty #workflow-graph-tab { align: center middle; }
    WorkflowPanel.-empty #workflow-empty { display: block; }
    WorkflowPanel.-empty #workflow-run > ContentTabs,
    WorkflowPanel.-empty #workflow-graph-header,
    WorkflowPanel.-empty WorkflowGraph { display: none; }
    WorkflowPanel #workflow-run > ContentSwitcher, WorkflowPanel TabPane { height: 1fr; }
    WorkflowPanel TabPane { padding: 0; }
    WorkflowPanel #workflow-graph-header { height: 1; margin-bottom: 1; }
    WorkflowPanel #workflow-header {
        width: 1fr; min-width: 0; height: 1; text-align: center; color: $text-muted;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    WorkflowPanel #workflow-layout {
        min-width: 0; width: auto; height: 1; margin: 0 1; padding: 0;
        border: none; background: transparent; tint: transparent; background-tint: transparent;
        color: $primary; text-style: underline; text-wrap: nowrap;
    }
    WorkflowPanel #workflow-layout:hover, WorkflowPanel #workflow-layout:focus {
        color: $accent; text-style: bold underline;
    }
    WorkflowPanel #workflow-controls {
        dock: none; height: auto; align: center top; padding: 0;
        overflow: auto hidden; scrollbar-size-horizontal: 1;
    }
    WorkflowPanel #workflow-controls Button { min-width: 16; width: auto; height: 3; margin: 0 1 0 0; }
    WorkflowPanel #workflow-controls #workflow-stop { margin-right: 0; }
    WorkflowPanel #workflow-controls #workflow-result { margin: 0 0 0 1; visibility: hidden; }
    WorkflowPanel.-empty #workflow-controls { margin-top: 2; }
    WorkflowPanel.-empty #workflow-new { margin-right: 0; }
    WorkflowPanel.-empty #workflow-start,
    WorkflowPanel.-empty #workflow-stop,
    WorkflowPanel.-empty #workflow-result { display: none; }
    WorkflowPanel #workflow-code-scroll { height: 1fr; overflow: auto auto; scrollbar-size: 1 1; }
    WorkflowPanel #workflow-info-scroll { height: 1fr; padding: 0; scrollbar-size: 1 1; }
    WorkflowPanel #workflow-input-scroll { height: 1fr; scrollbar-size-vertical: 1; }
    WorkflowPanel #workflow-input-actions { align-horizontal: right; padding: 0 1; }
    WorkflowPanel #workflow-input-actions > MessageCopyButton { display: block; }
    WorkflowPanel #workflow-run-input { height: auto; margin: 0 1; padding: 0; background: transparent; }
    WorkflowPanel #workflow-code-source { height: auto; width: auto; min-width: 100%; margin-left: -1; }
    """

    class RunSelected(Message):
        def __init__(self, run_id: str) -> None:
            super().__init__()
            self.run_id = run_id

    class StartRequested(Message):
        pass

    class NewSessionRequested(Message):
        pass

    class StopRequested(Message):
        pass

    class ResultRequested(Message):
        pass

    class ViewChanged(Message):
        pass

    class WorkingDirClicked(Message):
        pass

    def __init__(self, *, locale_controller: LocaleController | None = None) -> None:
        super().__init__(id="workflow-panel", classes="-empty")
        self.locale_controller = locale_controller
        self._welcome = WelcomeWidget(
            WORKFLOW_LOGO,
            compact_logo=text.render(text.MODE_WORKFLOW.bind(), locale_controller),
            id="workflow-empty",
        )
        self.previewing = False
        self.preview: WorkflowPreview | None = None
        self.run_id = ""
        self.run_ids: list[str] = []
        self._run_tabs_lock = asyncio.Lock()
        self._run_tabs_revision = 0
        self.definition = WorkflowDefinition()
        self._preview_models: list[dict] = []
        self.stale = False
        self.code_differs = False
        self._graph_run_id: str | None = None
        self._graph_dirty = False
        self._painted_header = ""
        self._painted_input: str | None = None
        self._start_label = text.START
        self._subtitle_cwd: str | None = None
        self._subtitle_width: int | None = None

    @property
    def empty(self) -> bool:
        return not self.definition.workflow_id

    def compose(self) -> ComposeResult:
        yield Tabs(id="workflow-run-tabs")
        with TabbedContent(initial="workflow-graph-tab", id="workflow-run"):
            with TabPane(
                Content.from_text(text.render(text.GRAPH_VIEW.bind(), self.locale_controller), markup=False),
                id="workflow-graph-tab",
            ):
                yield self._welcome
                with Horizontal(id="workflow-graph-header"):
                    yield Static(id="workflow-header")
                    yield Button(
                        Text(text.render(text.LAYOUT_VERTICAL.bind(), self.locale_controller)),
                        id="workflow-layout",
                        compact=True,
                    )
                warnings = Static(id="workflow-manifest-warnings")
                warnings.display = False
                yield warnings
                graph = WorkflowGraph()
                yield graph
                yield DialogButtonRow(
                    DialogButtonSpec(
                        Text(text.render(text.NEW_SESSION.bind(), self.locale_controller)), id="workflow-new"
                    ),
                    DialogButtonSpec(
                        Text(text.render(text.START.bind(), self.locale_controller)),
                        id="workflow-start",
                        variant="success",
                    ),
                    DialogButtonSpec(
                        Text(text.render(text.STOP.bind(), self.locale_controller)),
                        id="workflow-stop",
                        variant="error",
                        disabled=True,
                    ),
                    DialogButtonSpec(
                        Text(text.render(text.RESULT.bind(), self.locale_controller)),
                        id="workflow-result",
                        variant="warning",
                    ),
                    id="workflow-controls",
                )
                yield WorkflowScrollBar(graph)
            with (
                TabPane(
                    Content.from_text(text.render(INFO_TAB.bind(), self.locale_controller), markup=False),
                    id="workflow-info-tab",
                ),
                VerticalScroll(id="workflow-info-scroll"),
            ):
                yield WorkflowInfo(locale_controller=self.locale_controller)
            with (
                TabPane(
                    Content.from_text(text.render(text.CODE_VIEW.bind(), self.locale_controller), markup=False),
                    id="workflow-code-tab",
                ),
                VerticalScroll(id="workflow-code-scroll"),
            ):
                yield Static(id="workflow-code-source", expand=True)
            with TabPane(
                Content.from_text(text.render(text.INPUT.bind(), self.locale_controller), markup=False),
                id="workflow-input-tab",
            ):
                # Selecting the rendered input copies display text; this copies it as submitted.
                input_actions = MessageHeaderRow(id="workflow-input-actions")
                input_actions.display = False
                with input_actions:
                    yield MessageCopyButton(
                        tooltip=text.render(text.COPY_RUN_INPUT.bind(), self.locale_controller),
                        text=text.render(COPY_MESSAGE_BUTTON.bind(), self.locale_controller),
                    )
                with VerticalScroll(id="workflow-input-scroll"):
                    # The text the user started the run with, shown as their chat messages are.
                    yield VirtualizedMarkdown(id="workflow-run-input", parser_factory=create_user_text_markdown_parser)
            with TabPane(
                Content.from_text(text.render(text.OUTPUT.bind(), self.locale_controller), markup=False),
                id="workflow-output-tab",
            ):
                yield WorkflowOutputView(self.locale_controller)

    def on_mount(self) -> None:
        # Textual's CSS parser rejects line-pad: 0; the public style accepts it.
        for button in self.query("#workflow-controls Button"):
            button.styles.line_pad = 0
        # Result keeps its slot while hidden, so showing it moves no other button and needs no layout.
        # Its local rule is the one the selected run flips.
        self.query_one("#workflow-result", Button).styles.set_rule("visibility", "hidden")
        if self.locale_controller is not None:
            self.locale_controller.register_surface(self)

    def on_unmount(self) -> None:
        if self.locale_controller is not None:
            self.locale_controller.unregister_surface(self)

    def refresh_localization(self) -> None:
        self.query_one(WorkflowOutputView).refresh_localization()
        self.query_one(WorkflowInfo).refresh_localization()
        self._welcome.update_info(
            compact_logo=text.render(text.MODE_WORKFLOW.bind(), self.locale_controller),
        )
        self.query_one("#workflow-start", Button).label = Text(
            text.render(self._start_label.bind(), self.locale_controller)
        )
        copy_input = self.query_one("#workflow-input-actions > MessageCopyButton", MessageCopyButton)
        copy_input.update(Text(text.render(COPY_MESSAGE_BUTTON.bind(), self.locale_controller)))
        copy_input.tooltip = text.render(text.COPY_RUN_INPUT.bind(), self.locale_controller)
        self._graph_run_id = None
        self._graph_dirty = True
        self.post_message(self.ViewChanged())
        self.query_one("#workflow-stop", Button).label = Text(text.render(text.STOP.bind(), self.locale_controller))
        self.query_one("#workflow-result", Button).label = Text(text.render(text.RESULT.bind(), self.locale_controller))
        for number, tab in enumerate(self.query_one("#workflow-run-tabs", Tabs).query(Tab), 1):
            tab.label = Content.from_text(
                text.render(text.RUN_TAB.bind(number=number), self.locale_controller), markup=False
            )
        self.query_one("#workflow-new", Button).label = Text(
            text.render(text.NEW_SESSION.bind(), self.locale_controller)
        )
        self._update_layout_button()
        tabs = self.query_one("#workflow-run", TabbedContent)
        for pane_id, label in (
            ("workflow-graph-tab", text.GRAPH_VIEW),
            ("workflow-info-tab", INFO_TAB),
            ("workflow-code-tab", text.CODE_VIEW),
            ("workflow-input-tab", text.INPUT),
            ("workflow-output-tab", text.OUTPUT),
        ):
            tabs.get_tab(pane_id).label = Content.from_text(
                text.render(label.bind(), self.locale_controller), markup=False
            )

    def watch_workspace_cwd(self, cwd: str) -> None:
        """Keep the empty state's directory current, including while hidden."""
        self._welcome.update_info(cwd=cwd)
        self._update_border_subtitle()

    def on_resize(self) -> None:
        """Refit the subtitle only when the border's cell budget changes."""
        if self.region.width != self._subtitle_width:
            self._update_border_subtitle()

    def _update_border_subtitle(self) -> None:
        cwd, width = self.workspace_cwd, self.region.width
        if cwd == self._subtitle_cwd and width == self._subtitle_width:
            return
        subtitle = format_workspace_subtitle(text.shown(cwd), branch="", width=max(0, width - 6) if width else None)
        self._subtitle_cwd = cwd
        self._subtitle_width = width
        self.border_subtitle = subtitle or None

    def begin_preview(self) -> None:
        self.previewing = True
        self.query_one("#workflow-start", Button).disabled = True

    def end_preview(self) -> None:
        self.previewing = False

    @property
    def info_data(self) -> WorkflowInfoData | None:
        return self.query_one(WorkflowInfo).data

    @info_data.setter
    def info_data(self, data: WorkflowInfoData | None) -> None:
        self.query_one(WorkflowInfo).data = data

    def show_preview(self, preview: WorkflowPreview, *, run_id: str = "") -> None:
        self.preview = preview
        self._preview_models = []
        self.definition = WorkflowDefinition(preview.source.workflow_id, preview.source.source_kind, preview.manifest)
        self.stale = False
        self._show_run_view(preview.manifest, [], run_id=run_id)
        if not run_id:
            self.info_data = WorkflowInfoData.from_preview(preview)
        elif self.info_data is not None and self.info_data.run_id != run_id:
            # The new preview prepares the next run; the selected run still owns Info.
            self.info_data = None

    def set_preview_models(self, nodes: list[dict]) -> None:
        if nodes == self._preview_models:
            return
        self._preview_models = nodes
        if not self.run_id and self.preview is not None:
            self.query_one(WorkflowGraph).show_manifest(self.preview.manifest, nodes, locale=self.locale_controller)
            self.info_data = WorkflowInfoData.from_preview(self.preview, nodes)

    def show_history(self, run: ObservedRun, *, preview: WorkflowPreview | None = None) -> None:
        """Display recorded artifacts without loading or trusting the current workflow file."""
        self.preview = (
            preview
            if preview is not None
            and (preview.source.canonical_path, preview.spec_digest)
            == (run.started.canonical_path, run.started.spec_digest)
            else None
        )
        self.definition = WorkflowDefinition(run.started.workflow_id, run.started.source_kind, run.started.manifest)
        self.stale = False
        self._show_run_view(run.started.manifest, run.started.resolved_nodes, run_id=run.started.run_id)
        self._graph_run_id = run.started.run_id
        self._show_run_info(run)

    def show_draft_history(self, run: ObservedRun) -> None:
        """Keep an archived definition as an unbound draft; Start will preview its source."""
        self.preview = None
        self.definition = WorkflowDefinition(run.started.workflow_id, run.started.source_kind, run.started.manifest)
        self._show_run_view(run.started.manifest, run.started.resolved_nodes, run_id="")
        self._show_run_info(run)

    def _show_run_info(self, run: ObservedRun) -> None:
        self.info_data = WorkflowInfoData.from_run(run.started)

    def _show_run_view(self, manifest: dict, resolved_nodes: list[dict], *, run_id: str) -> None:
        warnings = self.query_one("#workflow-manifest-warnings", Static)
        messages = "\n".join(f"⚠ {warning.message}" for warning in manifest_warnings(manifest))
        warnings.update(Text(messages))
        warnings.display = bool(messages)
        self.code_differs = False
        self.query_one("#workflow-code-source", Static).update(Text(""))
        self.query_one("#workflow-info-scroll", VerticalScroll).scroll_home(animate=False)
        self.clear_outputs()
        if run_id != self.run_id:
            # Result belongs to the selected run; a refreshed preview that keeps it keeps Result too.
            set_widget_visibility_without_layout(self.query_one("#workflow-result", Button), False)
        self.run_id = run_id
        self._select_run_tab()
        self._graph_run_id = None
        self.previewing = False
        self.remove_class("-empty")
        self.query_one(WorkflowGraph).show_manifest(
            manifest, resolved_nodes, locale=self.locale_controller, reserve_usage=bool(run_id)
        )

    async def show_runs(self, run_ids: list[str]) -> None:
        requested = list(run_ids)
        self._run_tabs_revision += 1
        revision = self._run_tabs_revision
        async with self._run_tabs_lock:
            if revision != self._run_tabs_revision:
                return
            tabs = self.query_one("#workflow-run-tabs", Tabs)
            # Adding the first tab activates it once the tab is mounted, before add_tab() returns.
            with messages_disabled(Tabs.TabActivated, tabs):
                # Read the mounted tabs, so a cancelled update can be resumed too.
                existing = [(tab.id or "").removeprefix("run-") for tab in tabs.query(Tab)]
                if requested[: len(existing)] != existing:
                    # Flow tasks call this, and a newer restore or run switch cancels them.
                    await finish_shielded(tabs.clear())
                    existing = []
                for number, run_id in enumerate(requested[len(existing) :], len(existing) + 1):
                    await tabs.add_tab(
                        Tab(
                            Text(text.render(text.RUN_TAB.bind(number=number), self.locale_controller)),
                            id=f"run-{run_id}",
                        )
                    )
                self.run_ids = requested
                tabs.active = f"run-{self.run_id}" if self.run_id in requested else ""
                tabs.display = bool(requested)

    def _select_run_tab(self) -> None:
        """Move the tab bar to a run shown after its switch ended; ``show_runs`` settles tabs it still adds."""
        tabs = self.query_one("#workflow-run-tabs", Tabs)
        tab_id = f"run-{self.run_id}"
        if self.run_id and tabs.active != tab_id and tabs.query(f"#tabs-list > #{tab_id}"):
            with tabs.prevent(Tabs.TabActivated):
                tabs.active = tab_id

    @on(Tabs.TabActivated, "#workflow-run-tabs")
    def run_selected(self, event: Tabs.TabActivated) -> None:
        event.stop()
        run_id = (event.tab.id or "").removeprefix("run-")
        if event.tabs.active == event.tab.id and run_id in self.run_ids and run_id != self.run_id:
            self.post_message(self.RunSelected(run_id))

    def project(
        self,
        run: ObservedRun | None,
        *,
        busy: bool,
        workflow_active: bool,
        can_stop: bool | None = None,
        starting: bool = False,
        retry_pending: frozenset[tuple[str, str, int]] | None = None,
    ) -> None:
        if not self.display or (self.empty or self.previewing) or not self.definition.manifest:
            return
        graph = self.query_one(WorkflowGraph)
        if run is not None and (self.info_data is None or self.info_data.run_id != run.started.run_id):
            self._show_run_info(run)
        if run is not None and (self._graph_dirty or self._graph_run_id != run.started.run_id):
            graph.show_manifest(
                run.started.manifest,
                run.started.resolved_nodes,
                locale=self.locale_controller,
                reserve_usage=True,
            )
            self._graph_run_id = run.started.run_id
        if run is None and (self._graph_dirty or self._graph_run_id is not None):
            graph.show_manifest(self.definition.manifest, self._preview_models, locale=self.locale_controller)
            self._graph_run_id = None
        self._graph_dirty = False
        iterations = run.loop_iterations if run else {}
        graph.show_iterations(iterations)
        views = {}
        if run is not None:
            active = not run.finished and workflow_active
            for node_id, node in run.nodes.items():
                timing = run.timings.get(node.activation_id)
                retry = (
                    RetryTarget(node.run_id, node.activation_id, node.attempt)
                    if active and node.state == "awaiting_retry"
                    else None
                )
                views[node_id] = NodeView(
                    state=node.state,
                    elapsed_seconds=timing.seconds if timing is not None else None,
                    running_since=timing.running_since if timing is not None and active else None,
                    usage=run.usage.get(node.invocation_id),
                    retry=retry,
                    retry_pending=bool(
                        retry and retry_pending and (retry.run_id, retry.activation_id, retry.attempt) in retry_pending
                    ),
                )
        graph.show_nodes(views)
        self._update_header(run)
        self.query_one(WorkflowOutputView).show_iterations(iterations)
        start = self.query_one("#workflow-start", Button)
        self._start_label = text.STARTING if starting else text.START
        start.label = Text(text.render(self._start_label.bind(), self.locale_controller))
        start.disabled = (
            (self.preview is None and not self.definition.workflow_id)
            or starting
            or busy
            or (self.stale and not self.run_ids)
        )
        self.query_one("#workflow-stop", Button).disabled = not (workflow_active if can_stop is None else can_stop)
        self.query_one("#workflow-new", Button).disabled = busy or starting
        set_widget_visibility_without_layout(
            self.query_one("#workflow-result", Button),
            run is not None and run.finished is not None and bool(run.finished.outputs),
        )
        input_text = run.started.input_text if run else ""
        if input_text != self._painted_input:
            self._painted_input = input_text
            self.query_one("#workflow-input-actions").display = bool(input_text.strip())
            self.query_one("#workflow-run-input", VirtualizedMarkdown).update(sanitize_source_text(input_text))

    def on_message_copy_button_clicked(self, event: MessageCopyButton.Clicked) -> None:
        """Copy the run input exactly as it was submitted."""
        event.stop()
        run_input = self._painted_input
        if not run_input or not run_input.strip():
            return
        copy_text_to_clipboards(self.app, run_input)
        self.notify(
            text.render(text.RUN_INPUT_COPIED.bind(), self.locale_controller),
            title=text.render(COPIED_TITLE.bind(), self.locale_controller),
            timeout=2,
            markup=False,
        )

    @property
    def graph_visible(self) -> bool:
        return self.query_one("#workflow-run", TabbedContent).active == "workflow-graph-tab"

    @property
    def code_visible(self) -> bool:
        return self.query_one("#workflow-run", TabbedContent).active == "workflow-code-tab"

    @property
    def info_visible(self) -> bool:
        return self.query_one("#workflow-run", TabbedContent).active == "workflow-info-tab"

    def advance_animation(self, run: ObservedRun | None) -> None:
        self.query_one(WorkflowGraph).advance_animation()
        if is_widget_shown_on_active_screen(self.query_one("#workflow-header")):
            self._update_header(run)

    def set_header(self, title: str, *, session_id: str, cwd: str) -> None:
        title = title or text.render(text.TITLE.bind(), self.locale_controller)
        if session_id:
            title = text.render(
                text.SESSION_TITLE.bind(name=title, session_id=session_short_id(session_id)), self.locale_controller
            )
        self.border_title = Text(text.shown(title))
        self.workspace_cwd = cwd
        self._update_border_subtitle()

    def _update_header(self, run: ObservedRun | None) -> None:
        if not self.definition.manifest:
            return
        state = run.status if run else "idle"
        status = text.state_label(state, self.locale_controller)
        if run is not None and state == "running":
            elapsed = (datetime.now(UTC) - run.started.timestamp).total_seconds()
            status += f" ({format_elapsed(elapsed)})"
        source_label = text.SOURCES.get(self.definition.source_kind)
        header = " · ".join(
            part
            for part in (
                self.definition.workflow_id,
                text.render(source_label.bind(), self.locale_controller) if source_label else "",
                status,
            )
            if part
        )
        if header != self._painted_header:
            self._painted_header = header
            self.query_one("#workflow-header", Static).update(Text(header), layout=False)

    @property
    def output_visible(self) -> bool:
        return self.query_one("#workflow-run", TabbedContent).active == "workflow-output-tab"

    def show_graph(self, *, focus: bool = False) -> None:
        self.query_one("#workflow-run", TabbedContent).active = "workflow-graph-tab"
        if focus:
            self.query_one(WorkflowGraph).focus()

    def show_code(self, source: bytes, *, differs: bool) -> None:
        self.code_differs = differs
        view = self.query_one("#workflow-code-source", Static)
        # Every Code tab visit re-reads the source; a view already showing these
        # bytes keeps its rendering and layout instead of re-highlighting them.
        if not (isinstance(view.content, WorkflowSourceSyntax) and view.content.source == source):
            view.update(WorkflowSourceSyntax(source))

    @on(TabbedContent.TabActivated, "#workflow-run")
    def view_changed(self, event: TabbedContent.TabActivated) -> None:
        event.stop()
        if event.pane.id == event.tabbed_content.active and not (self.empty or self.previewing):
            self.post_message(self.ViewChanged())

    def clear_outputs(self) -> None:
        self.query_one(WorkflowOutputView).clear()

    def show_status(self, run: ObservedRun | None) -> None:
        if self.output_visible:
            self.query_one(WorkflowStatusOutput).show_run(run)

    def show_outputs(self, outputs: tuple[WorkflowOutputText, ...]) -> None:
        self.query_one(WorkflowOutputView).show_outputs(outputs)

    @on(Button.Pressed, "#workflow-new")
    def new_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.screen.set_focus(None)
        self.post_message(self.NewSessionRequested())

    @on(Button.Pressed, "#workflow-start")
    def start_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.screen.set_focus(None)
        self.post_message(self.StartRequested())

    @on(Button.Pressed, "#workflow-stop")
    def stop_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.screen.set_focus(None)
        self.post_message(self.StopRequested())

    @on(Button.Pressed, "#workflow-result")
    def result_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.screen.set_focus(None)
        self.post_message(self.ResultRequested())

    @on(Button.Pressed, "#workflow-layout")
    def layout_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        graph = self.query_one(WorkflowGraph)
        graph.toggle_layout()
        self._update_layout_button()
        graph.focus()

    def _update_layout_button(self) -> None:
        target = (
            text.LAYOUT_VERTICAL
            if self.query_one(WorkflowGraph).direction == Direction.LEFT_RIGHT
            else text.LAYOUT_HORIZONTAL
        )
        button = self.query_one("#workflow-layout", Button)
        label = text.render(target.bind(), self.locale_controller)
        if str(button.label) != label:
            button.label = Text(label)
            # Button.label only repaints; auto width must also follow the new label.
            button.refresh(layout=True)

    def on_click(self, event: Click) -> None:
        if event.screen_y == self.region.bottom - 1 and self.border_subtitle:
            event.prevent_default()
            event.stop()
            self.post_message(self.WorkingDirClicked())
