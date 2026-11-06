# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real catalog previews and stores for workflow UI tests with an inert execution lease."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

from rich.syntax import Syntax
from rich.text import Text
from textual.content import Content
from textual.widgets import Button, OptionList, Static, TabbedContent, Tabs

from chrys.app.tui.screens.dialogs.app_mode import AppModeDialog
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog, NoticeDialog
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ExecutionChanged
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.models.workflow_session import WorkflowIdentity, WorkflowSessionSelection, WorkspaceSnapshot
from chrys.foundation.models.workspace import Workspace
from chrys.service.state.store import StateStore
from chrys.service.state.workflow import WorkflowSessionState
from chrys.service.workflows.artifacts import session_runs
from chrys.service.workflows.store import RunHeader, RunSpec, WorkflowRunStore
from tests.support.tui_app_harness import SessionGenerationEngine
from tests.support.tui_helpers import click_when_settled, rich_plain
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

if TYPE_CHECKING:
    from pathlib import Path

    from textual.pilot import Pilot
    from textual.widget import Widget

    from chrys.app.tui.screens.main.screen import MainScreen
    from chrys.app.tui.widgets.workflow.values import WorkflowValueView
    from chrys.orchestration.workflows.preview import WorkflowPreview


class WorkflowCoordinatorView:
    def mutation_snapshot(self, session_id: str) -> None:
        return None


class WorkflowEngine(SessionGenerationEngine):
    workspace: Workspace | None = None
    workflows = WorkflowCoordinatorView()

    snapshot = ExecutionSnapshot("idle")

    def execution(self) -> ExecutionSnapshot:
        return self.snapshot

    def available_rollback_turns(self) -> list[int]:
        return []

    async def set_execution(self, snapshot: ExecutionSnapshot, bus: EventBus) -> None:
        self.snapshot = snapshot
        await bus.publish(ExecutionChanged(snapshot=snapshot))


async def start_workflow(pilot: Pilot, text: str = "") -> None:
    """Submit through the real Run input dialog, including empty input."""
    from chrys.app.tui.screens.dialogs.workflow_input import WorkflowInputDialog
    from chrys.app.tui.widgets.editor import MessageEditor

    main = pilot.app._main_screen
    assert main is not None
    await click_when_settled(pilot, main.query_one("#workflow-start", Button))
    await wait_for(
        lambda: isinstance(pilot.app.screen, WorkflowInputDialog) and pilot.app.screen.is_mounted, pilot=pilot
    )
    pilot.app.screen.query_one(MessageEditor).load_text(text)
    await click_when_settled(pilot, "#workflow-input-start")
    await wait_for(lambda: not isinstance(pilot.app.screen, WorkflowInputDialog), pilot=pilot)


async def confirm_workflow_cancel(pilot: Pilot) -> None:
    """Accept the workflow's cancellation warning through its real modal."""
    await wait_for(
        lambda: isinstance(pilot.app.screen, ConfirmDialog) and bool(pilot.app.screen.query("#confirm-yes")),
        pilot=pilot,
    )
    await click_when_settled(pilot, "#confirm-yes")


async def open_workflow(main: MainScreen, pilot: Pilot, workflow_id: str) -> WorkflowPreview:
    main.action_workflow()
    if main._workflow.browser.draft.follows_workspace and not main._workflow.session_id:
        await wait_for(
            lambda: (
                main._workflow.workspace == main._workflow.current_workspace()
                and not main._workflow.browser.workspace_busy
            ),
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
    preview = await main._workflow.browser.catalog.preview(workflow_id, trust=True)
    main._workflow.browser.catalog.confirm(preview)
    if main._workflow.session_id:
        main._workflow.browser.load_preview(workflow_id)
    else:
        main._workflow.browser.open(workflow_id)
    await wait_for(
        lambda: main._workflow_panel.preview is not None and not workflow_selecting(main),
        pilot=pilot,
        timeout=ENGINE_TURN_TIMEOUT,
    )
    rendered = asyncio.Event()
    main.call_after_refresh(rendered.set)
    await wait_for(rendered.is_set, pilot=pilot, description="preview tab geometry is rendered")
    # Pilot captures click coordinates before pumping layout. Logical preview readiness
    # does not imply that controls formerly under display:none have compositor geometry.
    start = main._workflow_panel.query_one("#workflow-start")
    await wait_for(
        lambda: start.region.width > 0 and main.app.get_widget_at(start.region.x, start.region.y)[0] is start,
        pilot=pilot,
        description="run controls are hit-testable",
    )
    return preview


async def select_workflow_view(
    main: MainScreen, pilot: Pilot, view: Literal["graph", "info", "code", "output"]
) -> None:
    """Select a real tab and wait for its content, including the initial source read."""
    panel = main._workflow_panel
    tabs = panel.query_one("#workflow-run", TabbedContent)
    pane_id = f"workflow-{view}-tab"
    tab = tabs.get_tab(pane_id)
    tab.scroll_visible(animate=False, immediate=True, force=True)
    await click_when_settled(pilot, tab)
    await wait_for(lambda: tabs.active == pane_id and tabs.get_pane(pane_id).display, pilot=pilot)
    if view == "code":
        await wait_for(
            lambda: isinstance(panel.query_one("#workflow-code-source", Static).content, Syntax), pilot=pilot
        )


async def dismiss_workflow_notice(main: MainScreen, pilot: Pilot, message: str) -> None:
    """Acknowledge user-facing feedback through the real modal."""
    await wait_for(
        lambda: (
            isinstance(main.app.screen, NoticeDialog)
            and main._workflow.feedback.notice is main.app.screen
            and bool(main.app.screen.query("#confirm-yes"))
        ),
        pilot=pilot,
    )
    dialog = main.app.screen
    assert message in str(dialog.query_one("#confirm-message", Static).content)
    await pilot.press("escape")
    await wait_for(lambda: main.app.screen is not dialog and main._workflow.feedback.notice is not dialog, pilot=pilot)


def run_store(directory: Path, preview: WorkflowPreview, *, session_id: str, started_at: str) -> WorkflowRunStore:
    return WorkflowRunStore.open(
        spec=RunSpec(manifest=preview.manifest, environment={}, resolved_nodes=()),
        input_text="draft",
        run_dir=directory,
        source=preview.source.source,
        header=RunHeader(
            run_id=directory.name,
            session_id=session_id,
            workflow_id=preview.source.workflow_id,
            source_kind=preview.source.source_kind,
            canonical_path=preview.source.canonical_path,
            title=preview.title,
            input_excerpt="draft",
            entry_digest=preview.load.entry_digest,
            manifest_digest=preview.load.manifest_digest,
            schema_version=preview.manifest["schema_version"],
            spec_digest=preview.spec_digest,
            started_at=started_at,
        ),
    )


async def switch_mode(main: MainScreen, pilot: Pilot) -> None:
    workflow = not main._workflow.workflow_mode
    await click_when_settled(pilot, "#mode-badge")
    # The dialog is the active screen before it has composed its option list.
    await wait_for(
        lambda: isinstance(main.app.screen, AppModeDialog) and main.app.screen.is_mounted,
        pilot=pilot,
        description="app mode dialog and its options are mounted",
    )
    picker = main.app.screen.query_one(OptionList)
    picker.highlighted = 1 if workflow else 0
    await pilot.press("enter")
    await wait_for(
        lambda: main._workflow.workflow_mode == workflow and not isinstance(main.app.screen, AppModeDialog),
        pilot=pilot,
    )


def workflow_selection(main: MainScreen, session_id: str = "workflow-session") -> WorkflowSessionSelection:
    preview = main._workflow_panel.preview
    source = preview.source if preview is not None else None
    identity = (
        WorkflowIdentity(source.workflow_id, source.canonical_path, source.source_kind)
        if source is not None
        else WorkflowIdentity("test", "/test.py", "project")
    )
    workspace = main._workflow.workspace
    return WorkflowSessionSelection(session_id, identity, workspace)


async def save_workflow_session(store: StateStore, session_id: str, cwd: Path) -> None:
    records = session_runs(store.session_dir(session_id))
    header = records[0].header
    state = WorkflowSessionState(
        WorkflowIdentity(header["workflow_id"], header["canonical_path"], header["source_kind"]),
        WorkspaceSnapshot.capture(Workspace.from_cwd(cwd)),
        run_count=len(records),
        latest_run_id=header["run_id"],
    )
    await store.save_workflow_session(session_id, state)


def workflow_selecting(main: MainScreen) -> bool:
    panel = main._workflow_panel
    return panel.empty or panel.previewing or main._workflow.browser._picker is not None


def workflow_notice_text(main: MainScreen) -> str:
    dialog = main._workflow.feedback.notice
    if dialog is None or not dialog.is_mounted:
        return ""
    return str(dialog.query_one("#confirm-message", Static).content)


async def select_archived_run(main: MainScreen, pilot: Pilot, run_id: str) -> None:
    view = main._workflow.session_view
    if run_id not in view._run_ids:
        view._run_ids.append(run_id)
    await main._workflow_panel.show_runs(view._run_ids)
    await view.select_run(run_id)


def shown_texts(root: Widget) -> list[str]:
    """The text of each displayed Static or Markdown source under *root*, without value tab bars."""
    texts: list[str] = []
    for child in root.children:
        if not child.display or isinstance(child, Tabs):
            continue
        if isinstance(child, VirtualizedMarkdown):
            texts.append(child.source)
        elif isinstance(child, Static):
            content = child.content
            texts.append(str(content) if isinstance(content, str | Text | Content) else rich_plain(content))
        else:
            texts.extend(shown_texts(child))
    return texts


def shown_text(root: Widget) -> str:
    return "\n".join(shown_texts(root))


def value_texts(view: WorkflowValueView) -> list[str]:
    """The notice and shown tab of *view*, without the widgets that scroll below its content."""
    return shown_texts(view.header) + shown_texts(view.body)


def value_text(view: WorkflowValueView) -> str:
    return "\n".join(value_texts(view))


def value_settled(view: WorkflowValueView) -> bool:
    """Whether *view*'s rebuild of its document has finished.

    Mounting puts a widget's text in the DOM before the mount completes, and a rebuild mounts
    the notice before the value, so text alone doesn't prove the rebuild finished: a rebuild
    cancelled at that point runs again, from an empty body, when the document is next shown.
    """
    return view._shown == (view.document, view.shown_tab)
