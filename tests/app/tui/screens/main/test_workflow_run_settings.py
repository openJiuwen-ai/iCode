# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run settings reflect the node backends and keep preview and workspace changes atomic."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from rich.text import Text
from textual.color import Color
from textual.widgets import Button, OptionList, Static

from chrys.app.tui.screens.dialogs.confirm import NoticeDialog
from chrys.app.tui.screens.dialogs.file_picker import FilePicker
from chrys.app.tui.screens.dialogs.workflow_confirm import WorkflowConfirmDialog
from chrys.app.tui.screens.dialogs.workflow_input import WorkflowInputDialog
from chrys.app.tui.support.workspace_mru import ensure_workspace_mru_index, session_root_key
from chrys.app.tui.widgets.editor import MessageEditor
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.foundation.events import types as events
from chrys.foundation.models.workspace import WorkingDir, Workspace
from chrys.foundation.platform.files import atomic_write_owner_only_bytes
from chrys.orchestration.workflows import preview as preview_module
from chrys.orchestration.workflows.catalog import WorkflowCatalog
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AcpAgentConfig
from chrys.service.workflows.discovery import BUILTIN_DIR, global_workflows_dir
from tests.orchestration.workflows._hosting import make_profile, make_project, write_workflow
from tests.support.event_capture import capture_event_sequence
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, ENGINE_TURN_TIMEOUT, wait_for, with_wait_deadline
from tests.support.workflow_workers import python_workflow

from ._workflow_support import WorkflowEngine, open_workflow, switch_mode, workflow_selection

if TYPE_CHECKING:
    from textual.pilot import Pilot

    from chrys.app.tui.screens.main.screen import MainScreen


async def _choose_directory(main: MainScreen, pilot: Pilot, path: Path, entry: str) -> WorkflowInputDialog | None:
    dialog = None
    if entry == "input":
        await click_when_settled(pilot, "#workflow-start")
        await wait_for(
            lambda: isinstance(main.app.screen, WorkflowInputDialog) and main.app.screen.is_mounted, pilot=pilot
        )
        dialog = main.app.screen
        assert isinstance(dialog, WorkflowInputDialog)
        await click_when_settled(pilot, "#workflow-input-change-directory")
    else:
        main._workspace_actions.open_working_dir_picker()
    await wait_for(
        lambda: (
            isinstance(main.app.screen, FilePicker)
            and main.app.screen.is_mounted
            and bool(main.app.screen.query("#fsd-tree"))
        ),
        pilot=pilot,
    )
    main.app.screen.dismiss(str(path))
    return dialog


# Either change asks for Trust before the load, and completing the load must not ask again: both run to
# completion. Once trusted, cancelling the load does not depend on why Trust was asked: one change cancels.
@pytest.mark.parametrize(("cancel", "change"), [(False, "source"), (False, "environment"), (True, "environment")])
async def test_workspace_preview_shows_loading_after_trust_and_keeps_cancel_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str, cancel: bool
) -> None:
    project, other = make_project(tmp_path), make_project(tmp_path / "other")
    monkeypatch.chdir(project)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        catalog = main._workflow.browser.catalog
        directory = global_workflows_dir(catalog.config_dir)
        directory.mkdir(parents=True, exist_ok=True)
        source_path = directory / "portable.py"
        atomic_write_owner_only_bytes(source_path, python_workflow("def echo(value):\n    return value\n", "echo"))
        await open_workflow(main, pilot, "portable")
        original_preview = main._workflow.browser.loaded.preview
        await click_when_settled(pilot, "#workflow-start")
        await wait_for(lambda: isinstance(app.screen, WorkflowInputDialog) and app.screen.is_mounted, pilot=pilot)
        dialog = app.screen
        if change == "source":
            source_path.write_bytes(source_path.read_bytes() + b"# changed\n")
        else:
            catalog.ledger().confirm(replace(original_preview.ledger_entry(), environment_fingerprint="0" * 64))
        entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_load = preview_module.load_workflow

        async def blocked_load(*args, **kwargs):
            entered.set()
            try:
                await release.wait()
                return await original_load(*args, **kwargs)
            finally:
                finished.set()

        monkeypatch.setattr(preview_module, "load_workflow", blocked_load)
        try:
            await click_when_settled(pilot, "#workflow-input-change-directory")
            await wait_for(lambda: isinstance(app.screen, FilePicker) and app.screen.is_mounted, pilot=pilot)
            app.screen.dismiss(str(other))
            await wait_for(
                lambda: isinstance(app.screen, WorkflowConfirmDialog) and app.screen.is_mounted,
                pilot=pilot,
                timeout=ENGINE_TURN_TIMEOUT,
            )
            assert not entered.is_set()  # Even an environment-only change asks before loading the module.
            await click_when_settled(pilot, "#workflow-confirm-yes")
            await wait_for(lambda: entered.is_set() and app.screen is dialog, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
            progress = dialog.query_one("#workflow-input-directory-loading", Static)
            assert progress.display and str(progress.content)
            assert dialog.query_one("#workflow-input-start", Button).disabled
            assert not dialog.query_one("#workflow-input-cancel", Button).disabled
            if cancel:
                await click_when_settled(pilot, "#workflow-input-cancel")
                await wait_for(lambda: finished.is_set() and not main._workflow.browser.workspace_busy, pilot=pilot)
                assert dialog not in app.screen_stack
                assert main._workflow.browser.loaded.preview is original_preview
            else:
                release.set()
                await wait_for(
                    lambda: not dialog.query_one("#workflow-input-start", Button).disabled,
                    pilot=pilot,
                    timeout=ENGINE_TURN_TIMEOUT,
                )
                # A second Trust prompt would leave the input dialog blocked instead of completing.
                assert app.screen is dialog and not progress.display
                assert str(dialog.query_one("#workflow-input-directory", Static).content) == str(other)
                current = main._workflow.browser.loaded.preview
                assert main._workflow.browser.catalog.ledger().is_confirmed(current.ledger_entry())
                await click_when_settled(pilot, "#workflow-input-cancel")
        finally:
            release.set()


def _backend_source(backend: str) -> bytes:
    if backend == "python":
        return python_workflow("def echo(value):\n    return value\n", "echo")
    profile = "Kernel" if backend == "kernel" else "External"
    nodes = f"a = wf.agent('first', profile={profile!r})\n"
    nodes += "b = wf.agent('second', profile='Kernel')\nwf.edge(a, b)\n" if backend == "mixed" else "b = a\n"
    return (
        "from chrys.workflows import WorkflowBuilder\nwf = WorkflowBuilder('backend')\n"
        + nodes
        + "wf.start(a)\nwf.output(b)\nworkflow = wf.build()\n"
    ).encode()


async def test_default_model_only_for_kernel_nodes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    agents = AgentProfileRegistry()
    agents.register(make_profile("Kernel"))
    agents.register(replace(make_profile("External"), acp=AcpAgentConfig(command="external")))
    backends = ("python", "kernel", "acp", "mixed")  # the model row alternates hidden and shown
    for backend in backends:
        write_workflow(project, f"backend_{backend}", _backend_source(backend))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        main._services.agent_registry = agents
        # Each backend opens in the same unbound draft; cancelling its Run input leaves the draft as it was.
        for backend in backends:
            await open_workflow(main, pilot, f"backend_{backend}")
            await click_when_settled(pilot, "#workflow-start")
            await wait_for(lambda: isinstance(app.screen, WorkflowInputDialog) and app.screen.is_mounted, pilot=pilot)
            dialog = app.screen
            model = dialog.query_one("#workflow-input-model-row")
            assert model.display == (backend in {"kernel", "mixed"}), backend
            assert str(model.query_one(Static).content) == "Default Model"
            assert str(dialog.query_one("#workflow-input-directory", Static).content) == str(project)
            assert not dialog.query_one("#workflow-input-change-directory").display
            assert dialog.query_one(MessageEditor).region.height > 0
            await click_when_settled(pilot, "#workflow-input-cancel")
            await wait_for(lambda: app.screen is main, pilot=pilot)
            assert not main._workflow.session_id
        main._workspace_actions.open_working_dir_picker()
        await wait_for(
            lambda: isinstance(app.screen, NoticeDialog) and bool(app.screen.query("#confirm-message")), pilot=pilot
        )
        assert "current working directory" in str(app.screen.query_one("#confirm-message", Static).content)
        assert main._workflow.project_cwd == str(project)


@pytest.mark.parametrize("source_kind", ["global", "builtin"])
async def test_directory_change_updates_preview_outer_path_and_submitted_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    # Editable installs can put the builtin source underneath the workspace.
    project = BUILTIN_DIR.parent if source_kind == "builtin" else make_project(tmp_path)
    other, shared = tmp_path / "other [literal]", tmp_path / "shared"
    other.mkdir()
    shared.mkdir()
    ensure_workspace_mru_index(
        [(str(other), datetime.now(UTC))], max_entries=7, root_key=session_root_key(tmp_path / "sessions")
    )
    monkeypatch.chdir(project)
    engine = WorkflowEngine()
    engine.workspace = Workspace(str(project), [WorkingDir(str(shared), "shared")])
    app = make_chrys_app(tmp_path / "sessions", engine=engine)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        if source_kind == "global":
            directory = global_workflows_dir(main._workflow.browser.catalog.config_dir)
            directory.mkdir(parents=True, exist_ok=True)
            atomic_write_owner_only_bytes(
                directory / "portable.py", python_workflow("def echo(value):\n    return value\n", "echo")
            )
        await open_workflow(main, pilot, "demo-workflow" if source_kind == "builtin" else "portable")
        old_preview = main._workflow_panel.preview
        assert old_preview is not None and old_preview.source.source_kind == source_kind
        if source_kind == "builtin":
            assert Path(old_preview.source.canonical_path).is_relative_to(project)
        await click_when_settled(pilot, "#workflow-start")
        await wait_for(lambda: isinstance(app.screen, WorkflowInputDialog) and app.screen.is_mounted, pilot=pilot)
        dialog = app.screen
        dialog.query_one(MessageEditor).load_text("keep [draft]\n")
        async with capture_event_sequence(main._services.bus, events.WorkspaceChange) as chat_changes:
            await click_when_settled(pilot, "#workflow-input-change-directory")
            # The picker is the active screen before it has composed its children.
            await wait_for(
                lambda: isinstance(app.screen, FilePicker) and app.screen.is_mounted,
                pilot=pilot,
                description="directory picker and its children are mounted",
            )
            picker = app.screen
            favorites = picker.query_one("#fsd-favorites", OptionList)
            await wait_for(
                lambda: (
                    str(other) in [favorites.get_option_at_index(index).id for index in range(favorites.option_count)]
                ),
                pilot=pilot,
            )
            assert favorites.region.width > 0 and favorites.region.height > 0
            favorites.highlighted = favorites.get_option_index(str(other))
            favorites.focus()
            await pilot.press("enter")
            await wait_for(lambda: not picker.query_one("#fsd-select", Button).disabled, pilot=pilot)
            await click_when_settled(pilot, "#fsd-select")
            await wait_for(
                lambda: (
                    app.screen is dialog
                    and str(dialog.query_one("#workflow-input-directory", Static).content) == str(other)
                    # The panel behind the dialog follows on the controller's next refresh.
                    and Text.from_markup(str(main._workflow_panel.border_subtitle)).plain == str(other)
                ),
                pilot=pilot,
                timeout=ENGINE_TURN_TIMEOUT,
            )
            assert main._workflow_panel.preview is not old_preview
            assert main._workflow.browser.loaded.catalog is main._workflow.browser.catalog
            assert not chat_changes and engine.workspace.primary_cwd == str(project)
            assert dialog.query_one(MessageEditor).text == "keep [draft]\n"
            graph = main._workflow_panel.query_one(WorkflowGraph)
            node_id = old_preview.manifest["nodes"][0]["id"]
            box = graph.geometry[node_id]
            title_y = box.y + 1 + graph.diagram_origin.y - round(graph.scroll_offset.y)
            assert any(
                segment.text == node_id
                and segment.style
                and segment.style.bold
                and segment.style.color == Color.parse(app.theme_variables["primary"]).rich_color
                for segment in graph.render_line(title_y)
            )
            await click_when_settled(pilot, "#workflow-input-cancel")
            await wait_for(lambda: app.screen is main, pilot=pilot)
            await click_when_settled(pilot, "#workflow-start")
            await wait_for(lambda: isinstance(app.screen, WorkflowInputDialog) and app.screen.is_mounted, pilot=pilot)
            assert str(app.screen.query_one("#workflow-input-directory", Static).content) == str(other)
            async with capture_event_sequence(main._services.bus, events.WorkflowRunRequest) as requests:
                await click_when_settled(pilot, "#workflow-input-start")
                await wait_for(lambda: bool(requests), pilot=pilot)
                assert requests[0].target.workspace.primary_cwd == str(other)
                assert requests[0].target.workspace.working_dirs[0].path == str(shared)
                assert requests[0].input_text == "keep [draft]\n"
                await main._services.bus.publish(
                    events.WorkflowRunRejected(request_id=requests[0].request_id, error="test")
                )


async def test_saved_session_directory_is_read_only_in_run_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        saved = workflow_selection(main)
        main._workflow.session_view.selection = saved
        await click_when_settled(pilot, "#workflow-start")
        await wait_for(lambda: isinstance(app.screen, WorkflowInputDialog) and app.screen.is_mounted, pilot=pilot)
        assert str(app.screen.query_one("#workflow-input-directory", Static).content) == saved.workspace.primary_cwd
        assert not app.screen.query_one("#workflow-input-change-directory").display


@pytest.mark.parametrize("entry", ["input", "footer"])
async def test_directory_change_rejects_shadowed_workflow_before_loading_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    project, other = make_project(tmp_path), make_project(tmp_path / "other")
    monkeypatch.chdir(project)
    source = python_workflow("def echo(value):\n    return value\n", "echo")
    marker = other / "loaded"
    write_workflow(other, "portable", f"from pathlib import Path\nPath({str(marker)!r}).touch()\n".encode() + source)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        directory = global_workflows_dir(main._workflow.browser.catalog.config_dir)
        directory.mkdir(parents=True, exist_ok=True)
        atomic_write_owner_only_bytes(directory / "portable.py", source)
        await open_workflow(main, pilot, "portable")
        target = main._workflow.session_view.selection
        prepared, catalog = main._workflow.browser.loaded.preview, main._workflow.browser.catalog
        dialog = await _choose_directory(main, pilot, other, entry)
        if dialog is not None:
            await wait_for(lambda: dialog.query_one("#workflow-input-directory-error").display, pilot=pilot)
            assert str(dialog.query_one("#workflow-input-directory", Static).content) == str(project)
            assert not dialog.query_one("#workflow-input-start", Button).disabled
        else:
            await wait_for(lambda: isinstance(app.screen, NoticeDialog), pilot=pilot)
        assert main._workflow.session_view.selection is target and not marker.exists()
        assert main._workflow.browser.loaded.preview is prepared and main._workflow.browser.catalog is catalog
        assert str(main._workflow_panel.border_subtitle) == str(project)


@pytest.mark.parametrize("entry", ["input", "footer"])
async def test_cancel_during_directory_preview_keeps_original_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str
) -> None:
    project, other = make_project(tmp_path), tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(project)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        target = main._workflow.session_view.selection
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = WorkflowCatalog.preview

        async def blocked(self, *args, **kwargs):
            entered.set()
            try:
                await release.wait()
                return await original(self, *args, **kwargs)
            finally:
                cancelled.set()

        monkeypatch.setattr(WorkflowCatalog, "preview", blocked)
        dialog = await _choose_directory(main, pilot, other, entry)
        await wait_for(entered.is_set, pilot=pilot)
        assert not main._workflow.run_control.start("must wait for preparation")
        if dialog is not None:
            assert dialog.query_one("#workflow-input-start", Button).disabled
            await click_when_settled(pilot, "#workflow-input-cancel")
        else:
            await switch_mode(main, pilot)
        await wait_for(lambda: app.screen is main and cancelled.is_set(), pilot=pilot)
        assert main._workflow.session_view.selection is target
        assert str(main._workflow_panel.border_subtitle) == str(project)


# The Run input shows the directory it ends on, so it runs both answers; the footer adds only where the
# change starts, and its commit runs once (a decline undoes nothing the input's decline does not).
@pytest.mark.parametrize(("entry", "accept"), [("input", False), ("input", True), ("footer", True)])
async def test_directory_dependent_manifest_is_confirmed_before_committing_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accept: bool, entry: str
) -> None:
    from chrys.app.tui.screens.dialogs.workflow_confirm import WorkflowConfirmDialog

    project, other = make_project(tmp_path), tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(project)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        directory = global_workflows_dir(main._workflow.browser.catalog.config_dir)
        directory.mkdir(parents=True, exist_ok=True)
        source = python_workflow("def echo(value):\n    return value\n", "echo")
        source = b"import os\n" + source.replace(b"WorkflowBuilder('t')", b"WorkflowBuilder(os.getcwd())")
        atomic_write_owner_only_bytes(directory / "portable.py", source)
        await open_workflow(main, pilot, "portable")
        target = main._workflow.session_view.selection
        dialog = await _choose_directory(main, pilot, other, entry)
        await wait_for(
            lambda: isinstance(app.screen, WorkflowConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        assert main._workflow.session_view.selection is target
        await click_when_settled(pilot, "#workflow-confirm-yes" if accept else "#workflow-confirm-no")
        await wait_for(
            lambda: app.screen is (dialog or main) and main._workflow.browser._workspace_task is None, pilot=pilot
        )
        expected = str(other if accept else project)
        if dialog is not None:
            assert str(dialog.query_one("#workflow-input-directory", Static).content) == expected
            assert not dialog.query_one("#workflow-input-start", Button).disabled
        assert main._workflow.project_cwd == expected
        assert main._workflow_panel.preview is not None
        assert main._workflow_panel.preview.title == expected


async def test_newer_directory_change_drains_old_preparation_before_committing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    monkeypatch.chdir(project)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        controller = main._workflow
        target, prepared = controller.session_view.selection, controller.browser.loaded.preview
        entered, draining, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = WorkflowCatalog.preview

        async def preview(catalog, *args, **kwargs):
            if catalog.project_cwd == first:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    draining.set()
                    await release.wait()
            return await original(catalog, *args, **kwargs)

        monkeypatch.setattr(WorkflowCatalog, "preview", preview)
        old = asyncio.create_task(controller.browser.change_draft_workspace(str(first)))
        new = None
        try:
            await wait_for(entered.is_set, pilot=pilot)
            new = asyncio.create_task(controller.browser.change_draft_workspace(str(second)))
            await wait_for(draining.is_set, pilot=pilot)
            assert not new.done()
            assert controller.session_view.selection is target and controller.browser.loaded.preview is prepared
        finally:
            release.set()
            await asyncio.gather(*[task for task in (old, new) if task is not None], return_exceptions=True)
        assert controller.project_cwd == str(second)
        assert controller.browser.catalog.project_cwd == second
        assert (
            controller.browser.loaded is not None
            and controller.browser.loaded.preview.source.workflow_id == main._workflow_panel.definition.workflow_id
        )
        # The panel header follows on the controller's next refresh.
        await wait_for(lambda: str(main._workflow_panel.border_subtitle) == str(second), pilot=pilot)


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_draft_follows_workspace_updates_until_user_selects_a_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    engine = WorkflowEngine()
    engine.workspace = Workspace.from_cwd(str(project))
    app = make_chrys_app(tmp_path / "sessions", engine=engine)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        controller = main._workflow
        for name in ("first", "second"):
            cwd = tmp_path / name
            cwd.mkdir()
            engine.workspace = Workspace.from_cwd(str(cwd))
            await main._services.bus.publish(events.WorkspaceUpdated(primary_cwd=str(cwd)), raise_handler_errors=True)
            # Following re-previews the definition in a real worker, the same cost open_workflow budgets for.
            await wait_for(
                lambda cwd=cwd: controller.project_cwd == str(cwd),
                pilot=pilot,
                timeout=ENGINE_TURN_TIMEOUT,
                description=f"the draft follows the workspace into {cwd.name}",
            )
            assert controller.browser.draft.follows_workspace
            assert (
                controller.browser.loaded is not None
                and controller.browser.loaded.preview.source.workflow_id == main._workflow_panel.definition.workflow_id
            )
        await controller.browser.change_draft_workspace(str(project))
        engine.workspace = Workspace.from_cwd(str(tmp_path))
        await main._services.bus.publish(events.WorkspaceUpdated(primary_cwd=str(tmp_path)), raise_handler_errors=True)
        assert controller.project_cwd == str(project)


async def test_workspace_preview_cancellation_does_not_cancel_the_event_publisher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, other = make_project(tmp_path), tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(project)
    engine = WorkflowEngine()
    engine.workspace = Workspace.from_cwd(str(project))
    app = make_chrys_app(tmp_path / "sessions", engine=engine)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = WorkflowCatalog.preview

        async def preview(catalog, *args, **kwargs):
            entered.set()
            try:
                await release.wait()
                return await original(catalog, *args, **kwargs)
            finally:
                cancelled.set()

        monkeypatch.setattr(WorkflowCatalog, "preview", preview)
        engine.workspace = Workspace.from_cwd(str(other))
        publisher = asyncio.create_task(
            main._services.bus.publish(events.WorkspaceUpdated(primary_cwd=str(other)), raise_handler_errors=True)
        )
        try:
            await wait_for(entered.is_set, pilot=pilot)
            assert publisher.done() and not publisher.cancelled()
            main._workflow.leave()
            await wait_for(cancelled.is_set, pilot=pilot)
            assert publisher.exception() is None
            assert main._workflow.project_cwd == str(project)
        finally:
            release.set()
            await publisher


async def test_leaving_during_workspace_confirmation_dismisses_it_without_changing_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.app.tui.screens.dialogs.workflow_confirm import WorkflowConfirmDialog

    project, other = make_project(tmp_path), tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(project)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        directory = global_workflows_dir(main._workflow.browser.catalog.config_dir)
        directory.mkdir(parents=True, exist_ok=True)
        source = python_workflow("def echo(value):\n    return value\n", "echo")
        atomic_write_owner_only_bytes(
            directory / "portable.py",
            b"import os\n" + source.replace(b"WorkflowBuilder('t')", b"WorkflowBuilder(os.getcwd())"),
        )
        await open_workflow(main, pilot, "portable")
        target, prepared = main._workflow.session_view.selection, main._workflow.browser.loaded.preview
        await _choose_directory(main, pilot, other, "footer")
        await wait_for(lambda: isinstance(app.screen, WorkflowConfirmDialog), pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
        main._workflow.leave()
        await wait_for(lambda: app.screen is main and main._workflow.browser._workspace_task is None, pilot=pilot)
        assert main._workflow.session_view.selection is target and main._workflow.browser.loaded.preview is prepared
