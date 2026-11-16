# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow folders in the workflow browser: their rows, Info, Source page and deletion."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from textual.widgets import Static

import chrys.service.workflows.discovery as discovery_module
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.main import workflow_content
from chrys.app.tui.widgets.workflow.info import WorkflowInfo
from chrys.app.tui.widgets.workflow.selection import WorkflowList
from chrys.app.tui.widgets.workflow.source import WorkflowSourceSyntax
from chrys.foundation.platform.files import atomic_write_owner_only_bytes
from chrys.service.workflows.discovery import SkippedSource, WorkflowSource, project_workflows_dir
from tests.orchestration.workflows._hosting import make_project, write_workflow, write_workflow_package
from tests.support.symlinks import symlink_or_skip
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for
from tests.support.workflow_workers import python_workflow

from ._workflow_support import (
    WorkflowEngine,
    dismiss_workflow_notice,
    open_workflow,
    select_workflow_view,
    workflow_notice_text,
)

SOURCE = python_workflow("import helpers\ndef fn(value):\n    return value\n", "fn")
FILES = {"helpers.py": b"VALUE = 1\n", "data/name.txt": b"world\n"}


async def test_a_folder_workflow_shows_its_file_count_and_its_source_page_reads_only_the_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    entry = write_workflow_package(project, "pkg", SOURCE, FILES)
    reads: list[Path] = []
    real_read = workflow_content.read_entry_bytes

    def recording_read(path: Path, kind: str) -> bytes:
        reads.append(path)
        return real_read(path, kind)

    monkeypatch.setattr(workflow_content, "read_entry_bytes", recording_read)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "pkg")
        panel = main._workflow_panel
        await select_workflow_view(main, pilot, "info")
        files = panel.query_one(WorkflowInfo).query_one("#workflow-info-package")
        assert [str(cell.content) for cell in files.query(Static)] == ["Files", "3"]

        await select_workflow_view(main, pilot, "code")
        source = panel.query_one("#workflow-code-source", Static)
        assert isinstance(source.content, WorkflowSourceSyntax) and source.content.source == SOURCE
        await select_workflow_view(main, pilot, "graph")
        helper = entry.parent / "helpers.py"
        helper.unlink()
        symlink_or_skip(helper, tmp_path / "outside.py")
        # Replacing a file changes the folder itself, which the periodic check notices.
        await dismiss_workflow_notice(main, pilot, "This workflow changed")
        assert panel.stale
        read_before = len(reads)

        await select_workflow_view(main, pilot, "code")

        # The folder no longer reads as a workflow, but the page shows the entry it previewed.
        await wait_for(lambda: len(reads) > read_before, pilot=pilot)
        assert reads[-1] == Path(preview.source.canonical_path)
        assert isinstance(source.content, WorkflowSourceSyntax) and source.content.source == SOURCE
        assert workflow_notice_text(main) == ""


@pytest.mark.parametrize("listing", ["listed", "skipped"])
async def test_deleting_a_folder_workflow_says_what_stays_and_removes_only_its_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: str
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    entry = write_workflow_package(project, "pkg", SOURCE, FILES)
    if listing == "skipped":
        monkeypatch.setattr(discovery_module, "MAX_PACKAGE_ENTRIES", 2)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        main.action_workflow()
        await wait_for(
            lambda: any(row.workflow_id == "pkg" for row in main._workflow.browser._picker.selection.rows),
            pilot=pilot,
        )
        selection = main._workflow.browser._picker.selection
        [row] = [row for row in selection.rows if row.workflow_id == "pkg"]
        assert isinstance(row.source, SkippedSource if listing == "skipped" else WorkflowSource)
        assert row.package_folder == str(entry.parent.resolve())
        if listing == "skipped":
            assert "has more than 2 files and folders" in str(selection.query_one("#workflow-warnings", Static).content)
        picker = selection.query_one(WorkflowList)
        picker.highlighted = selection.rows.index(row)
        picker.focus()

        await pilot.press("d")

        await wait_for(
            lambda: (
                isinstance(app.screen, ConfirmDialog)
                and app.screen.is_mounted
                and bool(app.screen.query("#confirm-yes"))
            ),
            pilot=pilot,
        )
        body = str(app.screen.query_one("#confirm-message", Static).content)
        assert "Only pkg.py is deleted. The other files in" in body and "are kept." in body
        await click_when_settled(pilot, "#confirm-yes")
        await wait_for(
            lambda: not any(row.workflow_id == "pkg" for row in main._workflow.browser._picker.selection.rows),
            pilot=pilot,
        )
        assert not entry.exists()
        assert sorted(path.name for path in entry.parent.iterdir()) == ["data", "helpers.py"]


async def test_an_edit_deep_in_a_folder_is_noticed_when_the_source_page_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    entry = write_workflow_package(project, "pkg", SOURCE, FILES)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "pkg")
        panel = main._workflow_panel
        atomic_write_owner_only_bytes(entry.parent / "data" / "name.txt", b"moon\n")
        # Only data/ changed, so the periodic check, which looks at the entry and the folder, can't tell.
        assert not main._workflow.browser.check_preview()
        assert not panel.stale

        await select_workflow_view(main, pilot, "code")

        await dismiss_workflow_notice(main, pilot, "This workflow changed")
        assert panel.stale


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file names can't hold control characters")
async def test_names_from_workflow_files_reach_the_browser_without_terminal_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    entry = write_workflow_package(project, "kit", SOURCE, FILES)
    (tmp_path / "outside.py").write_bytes(b"")
    symlink_or_skip(entry.parent / "bad\x1b[2J", tmp_path / "outside.py")
    write_workflow(project, "e\x1b[2Jx", SOURCE)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        main.action_workflow()
        await wait_for(
            lambda: {"kit", "e\x1b[2Jx"} <= {row.workflow_id for row in main._workflow.browser._picker.selection.rows},
            pilot=pilot,
        )
        selection = main._workflow.browser._picker.selection

        rows = "".join(str(option.prompt) for option in selection.query_one(WorkflowList).options)
        warnings = str(selection.query_one("#workflow-warnings", Static).content)

        assert "\x1b" not in rows and "\x1b" not in warnings
        assert "[2Jx" in rows and "contains a link: bad\ufffd[2J" in warnings


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file names can't hold control characters")
async def test_a_refused_delete_is_explained_before_any_confirmation_without_terminal_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    name = "e\x1b[2Jx"
    real = tmp_path / "elsewhere" / name
    real.mkdir(parents=True)
    atomic_write_owner_only_bytes(real / f"{name}.py", SOURCE)
    link = project_workflows_dir(project) / name
    symlink_or_skip(link, real, target_is_directory=True)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        main.action_workflow()
        await wait_for(
            lambda: any(row.workflow_id == name for row in main._workflow.browser._picker.selection.rows),
            pilot=pilot,
        )
        selection = main._workflow.browser._picker.selection
        picker = selection.query_one(WorkflowList)
        picker.highlighted = next(index for index, row in enumerate(selection.rows) if row.workflow_id == name)
        picker.focus()

        await pilot.press("d")

        await wait_for(lambda: "is a link" in workflow_notice_text(main), pilot=pilot)
        # The notice is a ConfirmDialog subclass; no deletion was ever offered for confirming.
        assert not any(type(screen) is ConfirmDialog for screen in app.screen_stack)
        assert "\x1b" not in workflow_notice_text(main) and "e\ufffd[2Jx" in workflow_notice_text(main)
        assert link.is_symlink() and (real / f"{name}.py").exists()
