# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace selection and editing preserve conversation state and return to the caller."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from threading import Event, get_ident
from unittest.mock import create_autospec

import pytest
from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Button, OptionList, Static

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.dialogs.file_picker import FilePicker, _FilteredDirectoryTree
from chrys.app.tui.screens.workspaces import edit as workspace_edit
from chrys.app.tui.screens.workspaces.picker import WorkspacesScreen
from chrys.app.tui.util.rich_style import rich_style_from_textual_color
from chrys.app.tui.widgets import EnhancedInput
from chrys.foundation.config.settings import Settings
from chrys.service.workspaces import registry as registry_module
from chrys.service.workspaces.registry import WorkspaceRegistry
from tests.support.tui_helpers import PlaceholderHostApp, click_when_settled
from tests.support.waiting import wait_for
from tests.support.workspace_registry import registry as registry
from tests.support.workspace_registry import workspace_edit_dialog


def _painted_lines(widget) -> list[str]:
    return [widget.render_line(y).text.strip() for y in range(widget.size.height)]


async def _return_folder(pilot, path):
    await wait_for(lambda: isinstance(pilot.app.screen, FilePicker) and pilot.app.screen.is_mounted, pilot=pilot)
    pilot.app.screen.dismiss(str(path))


class _WorkspaceFramesApp(PlaceholderHostApp):
    """Observe the regions actually submitted to the display, starting at its first frame."""

    def __init__(self) -> None:
        self.dialog = None
        self.frames = []
        super().__init__()

    def _display(self, screen, renderable) -> None:
        super()._display(screen, renderable)
        if screen is self.dialog and renderable is not None:
            options = (
                screen.query_one(OptionList)
                if isinstance(screen, WorkspacesScreen)
                else screen.query_one(".workspace-folder-path", Static)
            )
            if options.size.width and (not isinstance(options, OptionList) or options.option_count):
                self.frames.append(
                    (
                        tuple((widget.id, widget.region) for widget in screen.walk_children() if widget.id),
                        _painted_lines(options),
                    )
                )


@pytest.fixture
def folders(tmp_path: Path) -> tuple[Path, Path, Path]:
    roots = (tmp_path / "one", tmp_path / "two", tmp_path / "three")
    for root in roots:
        root.mkdir()
    return roots


async def _open(app: App, screen) -> None:
    app.push_screen(screen)
    await wait_for(lambda: app.screen is screen and screen.is_mounted, description="workspace dialog mounted")

    if isinstance(screen, WorkspacesScreen):
        await wait_for(lambda: screen.query_one(OptionList).option_count > 0, description="workspace rows loaded")


class _HeldFolderCheck:
    """Hold the folder check of *path*; Windows ``os.path.isdir`` does not go through ``os.stat``."""

    def __init__(self, monkeypatch, path: Path) -> None:
        self.started = Event()
        self.release = Event()
        loop_thread = get_ident()
        isdir = os.path.isdir

        def held_isdir(target):
            if os.fspath(target) == str(path):
                assert get_ident() != loop_thread, f"folder check of {path} ran on the event loop"
                self.started.set()
                if not self.release.wait(5):
                    raise TimeoutError(f"test did not release folder check of {path}")
            return isdir(target)

        monkeypatch.setattr(os.path, "isdir", held_isdir)


async def test_workspace_editor_paints_paths_while_missing_check_is_held(registry, folders, monkeypatch):
    primary, missing, _ = folders
    workspace = registry.create("Original", str(primary), [str(missing)])
    missing.rmdir()
    app = _WorkspaceFramesApp()
    async with app.run_test(size=(100, 30)) as pilot:
        held = _HeldFolderCheck(monkeypatch, missing)
        dialog = workspace_edit_dialog(registry, str(primary), workspace)
        app.dialog = dialog
        try:
            app.push_screen(dialog)
            await wait_for(lambda: app.frames and held.started.is_set(), pilot=pilot)
            row = dialog.query_one(".workspace-folder-path", Static)
            before = _painted_lines(row)
            assert len(before) == 1 and before[0].endswith(missing.name)
            region = row.region
            field = dialog.query_one("#workspace-name", EnhancedInput)
            await pilot.press("ctrl+a", "a")
            assert field.value != workspace.name
        finally:
            held.release.set()
        warning = "Folder was deleted or moved"
        await wait_for(lambda: _painted_lines(row)[0].endswith(warning), pilot=pilot)
        assert _painted_lines(row)[0].split(" · ", 1)[0].rstrip() == before[0]
        assert row.region == region


@pytest.mark.parametrize("physical_alias", [False, True], ids=["direct", "alias"])
async def test_picker_orders_by_containment_then_recency_without_assigning(registry, folders, tmp_path, physical_alias):
    from tests.support.symlinks import symlink_or_skip

    other = registry.create("Other", str(folders[1]))
    inside = registry.create("Inside", str(folders[0]))
    registry.touch(other.id)
    cwd = folders[0]
    if physical_alias:
        cwd = tmp_path / "cwd-alias"
        symlink_or_skip(cwd, folders[0], target_is_directory=True)
    async with PlaceholderHostApp().run_test() as pilot:
        dialog = WorkspacesScreen(registry, str(cwd), other if physical_alias else None)
        await _open(pilot.app, dialog)
        options = dialog.query_one(OptionList)
        assert [options.get_option_at_index(i).id for i in range(options.option_count)] == [
            inside.id,
            other.id,
            "unspecified",
            "create",
        ]
        assert options.get_option_at_index(options.highlighted).id == (other.id if physical_alias else "unspecified")
        assert options.get_option("unspecified").prompt.plain.startswith("◦ ") is (not physical_alias)
        assert options.get_option("create").prompt.style == "bold"
        container = dialog.query_one("#container")
        assert not container.border_subtitle
        await wait_for(lambda: "+ New workspace…" in _painted_lines(options), pilot=pilot)
        lines = _painted_lines(options)
        assert [line for line in lines if "Edit" in line or "Delete" in line] == [
            line for line in lines if line.removeprefix("◦ ").startswith(("Inside", "Other"))
        ]
        await pilot.press("e", "delete")
        assert pilot.app.screen is dialog
        assert registry.snapshot().workspace(inside.id) == inside


async def test_picker_row_actions_name_the_action_under_the_mouse(registry, folders):
    registry.create("Team", str(folders[0]))
    async with PlaceholderHostApp().run_test(size=(80, 24)) as pilot:
        dialog = WorkspacesScreen(registry, str(folders[1]), None)
        await _open(pilot.app, dialog)
        options = dialog.query_one(OptionList)
        await wait_for(lambda: any("Edit" in line for line in _painted_lines(options)), pilot=pilot)
        y, line = next(
            (y, options.render_line(y).text)
            for y in range(options.size.height)
            if "Edit" in options.render_line(y).text
        )
        await pilot.hover(options, offset=(line.index("Edit") + 1, y))
        assert options.tooltip is not None and options.tooltip.plain == "Edit"
        await pilot.hover(options, offset=(line.index("Delete") + 1, y))
        assert options.tooltip is not None and options.tooltip.plain == "Delete"
        await pilot.hover(options, offset=(1, y))
        assert options.tooltip is None


async def test_empty_picker_highlights_unspecified(registry, folders):
    async with PlaceholderHostApp().run_test() as pilot:
        dialog = WorkspacesScreen(registry, str(folders[0]), None)
        await _open(pilot.app, dialog)
        options = dialog.query_one(OptionList)
        assert options.get_option_at_index(options.highlighted).id == "unspecified"
        assert dialog.query_one("#container").border_subtitle is None
        assert [option.id for option in options.options] == ["unspecified", "create"]


async def test_picker_paints_the_last_read_first_and_keeps_the_highlight_through_a_newer_one(
    registry, folders, monkeypatch
):
    from chrys.app.tui.screens.workspaces import picker as picker_module
    from chrys.app.tui.util.workspace_order import read_workspace_order

    first = registry.create("First", str(folders[1]))
    second = registry.create("Second", str(folders[2]))
    cached = read_workspace_order(registry, str(folders[0]), None)
    added = registry.create("Added", str(folders[0]))
    started, release = Event(), Event()

    def held_read(*args):
        started.set()
        release.wait(10)
        return read_workspace_order(*args)

    monkeypatch.setattr(picker_module, "read_workspace_order", held_read)
    app = _WorkspaceFramesApp()
    async with app.run_test(size=(100, 30)) as pilot:
        dialog = WorkspacesScreen(registry, str(folders[0]), None, highlight=second.id, order=cached)
        app.dialog = dialog
        try:
            app.push_screen(dialog)
            await wait_for(lambda: app.frames and started.is_set(), pilot=pilot)
            assert any(line.startswith("Second") for line in app.frames[0][1])
            options = dialog.query_one(OptionList)
            assert options.get_option_at_index(options.highlighted).id == second.id
            await pilot.press("up")
            assert options.get_option_at_index(options.highlighted).id == first.id
        finally:
            release.set()
        await wait_for(lambda: options.option_count == 5, pilot=pilot)
        assert options.get_option_at_index(0).id == added.id
        assert options.get_option_at_index(options.highlighted).id == first.id


async def test_narrow_picker_keeps_the_highlight_in_view_when_missing_notes_arrive(registry, tmp_path, monkeypatch):
    from chrys.app.tui.screens.workspaces import picker as picker_module

    for index in range(6):
        folder = tmp_path / f"folder-{index}"
        folder.mkdir()
        registry.create(f"Workspace {index}", str(folder))
        if index % 2 == 0:
            folder.rmdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    started, release = Event(), Event()
    real_missing = picker_module.missing_folders

    def held_missing(paths):
        started.set()
        release.wait(10)
        return real_missing(paths)

    monkeypatch.setattr(picker_module, "missing_folders", held_missing)
    async with PlaceholderHostApp().run_test(size=(30, 18)) as pilot:
        dialog = WorkspacesScreen(registry, str(cwd), None)
        await _open(pilot.app, dialog)
        options = dialog.query_one(OptionList)
        await wait_for(started.is_set, pilot=pilot)
        await pilot.press("up")
        target = options.get_option_at_index(options.highlighted).id
        folder = Path(registry.snapshot().workspace(target).primary_cwd).name
        assert folder == "folder-5"
        await wait_for(lambda: any(line.endswith(folder) for line in _painted_lines(options)), pilot=pilot)
        release.set()
        await wait_for(lambda: dialog._primary_missing, pilot=pilot)
        await wait_for(lambda: any(line.endswith(folder) for line in _painted_lines(options)), pilot=pilot)


async def test_missing_notes_leave_a_picker_the_user_scrolled_where_it_is(registry, tmp_path, monkeypatch):
    from chrys.app.tui.screens.workspaces import picker as picker_module

    for index in range(12):
        folder = tmp_path / f"folder-{index:02}"
        folder.mkdir()
        registry.create(f"Workspace {index:02}", str(folder))
        if index % 2 == 0:
            folder.rmdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    started, release = Event(), Event()
    real_missing = picker_module.missing_folders

    def held_missing(paths):
        started.set()
        release.wait(10)
        return real_missing(paths)

    monkeypatch.setattr(picker_module, "missing_folders", held_missing)
    async with PlaceholderHostApp().run_test(size=(30, 18)) as pilot:
        dialog = WorkspacesScreen(registry, str(cwd), None)
        await _open(pilot.app, dialog)
        options = dialog.query_one(OptionList)
        await wait_for(started.is_set, pilot=pilot)
        await wait_for(lambda: options.scroll_y > 0, pilot=pilot)
        options.scroll_home(animate=False, immediate=True)
        await pilot.pause()
        release.set()
        await wait_for(lambda: dialog._primary_missing, pilot=pilot)
        await pilot.pause()
        assert options.scroll_y == 0


@pytest.mark.parametrize("assigned", [True, False])
async def test_unreadable_registry_picker_is_read_only(registry, folders, assigned):
    workspace = registry.create("One", str(folders[0]))
    context = registry.snapshot().workspace(workspace.id) if assigned else None
    registry.path.write_text("broken", encoding="utf-8")
    async with PlaceholderHostApp().run_test() as pilot:
        dialog = WorkspacesScreen(registry, str(folders[0]), context)
        await _open(pilot.app, dialog)
        options = dialog.query_one(OptionList)
        assert [options.get_option_at_index(i).id for i in range(options.option_count)] == ["error", "unspecified"]
        assert options.get_option("error").disabled is True
        assert options.get_option_at_index(options.highlighted).id == "unspecified"
        assert not dialog.query_one("#container").border_subtitle
        assert options.get_option("unspecified").prompt.plain.startswith("◦ ") is (not assigned)
        await wait_for(lambda: "No workspace" in _painted_lines(options), pilot=pilot)
        lines = _painted_lines(options)
        assert any(line.startswith("Cannot read the workspace list:") for line in lines)
        assert all("Edit" not in line and "Delete" not in line for line in lines)
        assert registry.path.read_text(encoding="utf-8") == "broken"


async def test_workspace_form_rejects_reserved_name_on_submit(registry, folders):
    name = "Unspecified"
    before = registry.snapshot().workspaces
    async with PlaceholderHostApp().run_test() as pilot:
        dialog = workspace_edit_dialog(registry, str(folders[0]))
        await _open(pilot.app, dialog)
        field = dialog.query_one("#workspace-name", EnhancedInput)
        field.value = name
        await pilot.pause()
        error = dialog.query_one("#workspace-error", Static)
        assert error.render().plain == ""
        assert not dialog.query_one("#workspace-save", Button).disabled

        await click_when_settled(pilot, "#workspace-save")
        await wait_for(lambda: error.render().plain == f"The workspace name {name} is reserved.", pilot=pilot)
        assert pilot.app.screen is dialog
        assert registry.snapshot().workspaces == before


async def _highlight_folder(picker, path, pilot):
    tree = picker.query_one("#fsd-tree", _FilteredDirectoryTree)
    tree.path = str(path.parent)

    def selected_child():
        return next((node for node in tree.root.children if node.data and node.data.path == path), None)

    await wait_for(selected_child, pilot=pilot)
    child = selected_child()
    assert child is not None
    _ = tree.last_line
    tree.move_cursor(child)
    tree.select_node(child)
    await wait_for(lambda: picker._selected_path == str(path), pilot=pilot)


@pytest.mark.parametrize(
    "choice",
    ["primary-child", "reference", "gone", "new"],
)
async def test_reference_folder_selection_returns_to_form_without_an_overlap_notice(
    registry, folders, monkeypatch, choice
):
    primary, reference_parent, new = folders
    reference = reference_parent / "reference"
    reference_child = reference / "child"
    primary_child = primary / "child"
    gone = new / "gone"
    for folder in (reference_child, primary_child, gone):
        folder.mkdir(parents=True)
    workspace = registry.create("Original", str(primary), [str(reference)])
    path = {
        "primary-child": primary_child,
        "reference": reference,
        "gone": gone,
        "new": new,
    }[choice]

    returned = asyncio.Event()
    add_folder = workspace_edit.WorkspaceEditDialog._folder_added

    async def record_result(dialog, selected):
        await add_folder(dialog, selected)
        assert selected == str(path)
        returned.set()

    monkeypatch.setattr(workspace_edit.WorkspaceEditDialog, "_folder_added", record_result)
    async with PlaceholderHostApp().run_test(notifications=True) as pilot:
        app = pilot.app
        dialog = workspace_edit_dialog(registry, str(primary), workspace)
        await _open(app, dialog)
        draft = dialog.query_one("#workspace-name", EnhancedInput)
        draft.value = "Local draft"
        await click_when_settled(pilot, "#workspace-add-folder")
        await wait_for(lambda: isinstance(app.screen, FilePicker) and app.screen.is_mounted, pilot=pilot)
        picker = app.screen
        assert picker.query_one("#fsd-container").border_title == "Add reference folder"
        assert picker.query_one("#fsd-tree", _FilteredDirectoryTree).path == primary.parent
        await _highlight_folder(picker, path, pilot)
        select = picker.query_one("#fsd-select", Button)
        await wait_for(lambda: picker._selected_path == str(path) and not select.disabled, pilot=pilot)
        if choice == "gone":
            await asyncio.to_thread(gone.rmdir)
        await click_when_settled(pilot, select)
        await wait_for(returned.is_set, pilot=pilot)

        expected = [str(reference), str(path)] if choice == "new" else [str(reference)]
        assert app.screen is dialog
        assert [row.name for row in dialog.query(".workspace-folder-path")] == expected
        assert dialog._folders == expected and draft.value == "Local draft"
        assert dialog.query_one("#workspace-error", Static).render().plain == ""
        assert not app._notifications


@pytest.mark.parametrize("editing", [False, True])
async def test_workspace_dialog_checks_duplicate_scope_only_on_submit(registry, folders, editing):
    primary, reference, extra = map(str, folders)
    registry.create("Existing", primary, [reference])
    workspace = registry.create("Draft", primary) if editing else None
    async with PlaceholderHostApp().run_test() as pilot:
        dialog = workspace_edit_dialog(registry, extra, workspace)
        await _open(pilot.app, dialog)
        dialog.query_one("#workspace-name", EnhancedInput).value = "Draft"

        async def choose(selector, path):
            await click_when_settled(pilot, selector)
            await wait_for(
                lambda: isinstance(pilot.app.screen, FilePicker) and pilot.app.screen.is_mounted, pilot=pilot
            )
            picker = pilot.app.screen
            await _highlight_folder(picker, Path(path), pilot)
            await click_when_settled(pilot, "#fsd-select")
            await wait_for(lambda: pilot.app.screen is dialog, pilot=pilot)

        if not editing:
            await choose("#workspace-primary", primary)
        await choose("#workspace-add-folder", reference)
        await wait_for(lambda: dialog._folders == [reference], pilot=pilot)
        error = dialog.query_one("#workspace-error", Static)
        assert error.render().plain == ""
        await click_when_settled(pilot, "#workspace-save")
        await wait_for(
            lambda: (
                error.render().plain == "Workspace “Existing” already has the same primary and reference folders. "
                "Add or remove a reference folder, or use “Existing”."
            ),
            pilot=pilot,
        )
        assert pilot.app.screen is dialog and dialog._folders == [reference]
        assert dialog.query_one("#workspace-name", EnhancedInput).value == "Draft"
        await choose("#workspace-add-folder", extra)
        await wait_for(lambda: dialog._folders == [reference, extra], pilot=pilot)
        await click_when_settled(pilot, "#workspace-save")
        await wait_for(lambda: pilot.app.screen is not dialog, pilot=pilot)
        saved = next(workspace for workspace in registry.snapshot().workspaces if workspace.name == "Draft")
        assert [directory.path for directory in saved.working_dirs] == [reference, extra]


@pytest.mark.parametrize(
    ("theme", "locale", "missing"),
    [("textual-dark", "en", "Folder was deleted or moved"), ("ansi-dark", "zh-Hans", "文件夹已删除或移动")],
)
async def test_edit_marks_missing_folders_and_keeps_unchanged_paths(registry, folders, locale, missing, theme):
    """Mark missing folders, preserve stored paths, and reject a new missing primary."""
    workspace = registry.create("One", str(folders[0]), tuple(map(str, folders[1:])))
    folders[0].rmdir()
    folders[1].rmdir()
    app = PlaceholderHostApp()
    app.theme = theme
    app.locale_controller = LocaleController(Settings(locale=locale))
    results = []
    async with app.run_test(size=(80, 24)) as pilot:
        dialog = workspace_edit_dialog(registry, str(folders[2]), workspace, locale_controller=app.locale_controller)
        app.push_screen(dialog, results.append)
        await wait_for(lambda: app.screen is dialog and dialog.is_mounted, pilot=pilot)
        assert dialog.query_one("#workspace-name", EnhancedInput).value == "One"
        primary = dialog.query_one("#workspace-primary", Static)
        await wait_for(lambda: _painted_lines(primary) and missing in _painted_lines(primary)[0], pilot=pilot)
        path, mark = _painted_lines(primary)[0].split(" · ", 1)
        assert ("…" in path and path.rstrip().endswith(os.sep + folders[0].name), mark) == (True, missing)
        related, present = dialog.query(".workspace-folder-path").results(Static)
        path, mark = _painted_lines(related)[0].split(" · ", 1)
        assert (path.rstrip().endswith(os.sep + folders[1].name), mark) == (True, missing)
        warning = rich_style_from_textual_color(app.theme_variables["warning"]).color
        for widget in (primary, related):
            assert next(segment.style.color for segment in widget.render_line(0) if missing in segment.text) == warning
            assert next(segment.style.color for segment in widget.render_line(0) if os.sep in segment.text) != warning
        assert len(_painted_lines(present)) == 1
        await click_when_settled(pilot, dialog.query_one("#workspace-save", Button))
        await wait_for(lambda: bool(results), pilot=pilot)
        assert registry.snapshot().workspace(workspace.id) == workspace
        assert not primary.can_focus


@pytest.mark.parametrize("entry", ["click", "enter"])
async def test_related_folders_are_limited_to_five_painted_rows(registry, folders, entry):
    related = tuple(folders[1] / str(index) for index in range(8))
    for folder in related:
        folder.mkdir()
    workspace = registry.create("Workspace", str(folders[0]), tuple(map(str, related)))
    app = PlaceholderHostApp()
    async with app.run_test(size=(80, 30)) as pilot:
        dialog = workspace_edit_dialog(registry, workspace.primary_cwd, workspace)
        app.push_screen(dialog)
        await wait_for(lambda: app.screen is dialog and dialog.is_mounted, pilot=pilot)
        folder_list = dialog.query_one("#workspace-folders", VerticalScroll)
        await wait_for(lambda: folder_list.size.height == 5 and folder_list.show_vertical_scrollbar, pilot=pilot)
        assert len(folder_list.query(Button)) == len(related)
        add = folder_list.query_one("#workspace-add-folder", Static)
        await pilot.press(*(["tab"] * (len(related) + 1)))
        await wait_for(lambda: add.has_focus and folder_list.scroll_offset.y > 0, pilot=pilot)
        if entry == "click":
            await click_when_settled(pilot, add)
        else:
            await pilot.press("enter")
        await _return_folder(pilot, folders[2])
        await wait_for(lambda: app.screen is dialog and len(folder_list.query(Button)) == len(related) + 1, pilot=pilot)
        assert folder_list.children[-1] is add
        await click_when_settled(pilot, "#workspace-save")
        await wait_for(lambda: app.screen is not dialog, pilot=pilot)
        assert tuple(directory.path for directory in registry.snapshot().workspace(workspace.id).working_dirs) == (
            *map(str, related),
            str(folders[2]),
        )


@pytest.mark.parametrize("finish", ["save", "cancel"])
async def test_folder_remove_buttons_stage_click_and_keyboard_removals(registry, folders, finish):
    extra = (folders[0].parent / "four", folders[0].parent / "five")
    for folder in extra:
        folder.mkdir()
    workspace = registry.create("Workspace", str(folders[0]), tuple(map(str, (*folders[1:], *extra))))
    async with PlaceholderHostApp().run_test(size=(80, 24)) as pilot:
        dialog = workspace_edit_dialog(registry, workspace.primary_cwd, workspace)
        await _open(pilot.app, dialog)
        folder_list = dialog.query_one("#workspace-folders", VerticalScroll)
        clicked, retained, middle, last = folder_list.query(Button)
        await wait_for(lambda: clicked.region.size == (3, 1), pilot=pilot)
        assert clicked.label.plain == "✕"
        assert clicked.tooltip is None
        await pilot.hover(clicked, offset=(1, 0))
        assert clicked.styles.background.a == 0 and clicked.styles.background_tint.a == 0
        await click_when_settled(pilot, clicked, offset=(1, 0))
        await wait_for(
            lambda: list(folder_list.query(Button)) == [retained, middle, last],
            pilot=pilot,
            description="only the clicked folder row was removed",
        )
        assert dialog.query_one("#workspace-name", EnhancedInput).has_focus
        assert all(not button.has_focus for button in (retained, middle, last))
        removing = middle if finish == "save" else last
        await pilot.press(*(("tab",) * (2 if finish == "save" else 3)))
        await wait_for(lambda: removing.has_focus, pilot=pilot)
        assert removing.styles.background.a == 0 and removing.styles.background_tint.a == 0
        assert removing.styles.text_style.underline and not removing.styles.text_style.reverse
        await pilot.press("delete")
        assert list(folder_list.query(Button)) == [retained, middle, last]
        assert removing.has_focus
        await pilot.press("enter")
        neighbor = last if finish == "save" else middle
        await wait_for(lambda: list(folder_list.query(Button)) == [retained, neighbor], pilot=pilot)
        await wait_for(lambda: neighbor.has_focus, pilot=pilot)
        await pilot.press("enter")
        await wait_for(lambda: list(folder_list.query(Button)) == [retained], pilot=pilot)
        assert retained.has_focus
        if finish == "cancel":
            await pilot.press("enter")
            await wait_for(lambda: not list(folder_list.query(Button)), pilot=pilot)
            assert dialog.query_one("#workspace-add-folder", Static).has_focus
            assert not dialog.query_one("#workspace-primary", Static).can_focus
        assert registry.snapshot().workspace(workspace.id) == workspace
        await click_when_settled(pilot, f"#workspace-{finish}")
        await wait_for(lambda: pilot.app.screen is not dialog, pilot=pilot)
        assert tuple(directory.path for directory in registry.snapshot().workspace(workspace.id).working_dirs) == (
            (str(folders[2]),) if finish == "save" else tuple(directory.path for directory in workspace.working_dirs)
        )


def _refuse_write(monkeypatch):
    monkeypatch.setattr(
        registry_module,
        "atomic_write_text",
        create_autospec(
            registry_module.atomic_write_text,
            side_effect=PermissionError(13, "Permission denied", "/workspaces/[draft]/workspaces.json"),
        ),
    )


async def _submit_mutation(pilot, dialog, operation):
    await click_when_settled(
        pilot, dialog.query_one("#workspace-delete" if operation == "delete" else "#workspace-save", Button)
    )
    if operation == "delete":
        await wait_for(lambda: isinstance(pilot.app.screen, ConfirmDialog) and pilot.app.screen.is_mounted, pilot=pilot)
        await click_when_settled(pilot, pilot.app.screen.query_one("#confirm-yes", Button))


@pytest.mark.parametrize(
    ("operation", "finish", "height", "locale"),
    [
        ("create", "retry", 10, "en"),
        ("save", "cancel", 15, "zh-Hans"),
        ("delete", "retry", 24, "en"),
    ],
)
async def test_workspace_dialog_write_failure_shows_reason_and_unlocks_every_action(
    tmp_path, monkeypatch, operation, finish, height, locale
):
    """A failed write keeps localized short-screen actions visible beside a scrollable error."""
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    original_root = tmp_path / "original"
    original_root.mkdir()
    original = registry.create("Original", str(original_root))
    before = registry.path.read_bytes()
    results = []
    app = PlaceholderHostApp()
    app.locale_controller = LocaleController(Settings(locale=locale))
    async with app.run_test(size=(80, height)) as pilot:
        dialog = workspace_edit_dialog(
            registry,
            str(tmp_path),
            None if operation == "create" else original,
            locale_controller=app.locale_controller,
        )
        pilot.app.push_screen(dialog, results.append)
        await wait_for(lambda: dialog.is_mounted, pilot=pilot)
        if operation != "delete":
            dialog.query_one("#workspace-name", EnhancedInput).value = "Changed"
        add_folder = dialog.query_one("#workspace-add-folder", Static)
        if operation == "save":
            add_folder.focus()
            await wait_for(lambda: dialog.focused is add_folder, pilot=pilot)
        with monkeypatch.context() as fault:
            _refuse_write(fault)
            await _submit_mutation(pilot, dialog, operation)
            await wait_for(
                lambda: (
                    not dialog.query_one("#workspace-cancel", Button).disabled
                    and "Permission denied" in dialog.query_one("#workspace-error", Static).render().plain
                ),
                pilot=pilot,
            )
            if operation == "save":
                await wait_for(lambda: dialog.focused is add_folder, pilot=pilot)
        assert pilot.app.screen is dialog
        assert results == []
        assert registry.path.read_bytes() == before
        name = dialog.query_one("#workspace-name", EnhancedInput)
        assert not name.disabled
        assert name.value == ("Original" if operation == "delete" else "Changed")
        assert not dialog.query_one("#workspace-add-folder", Static).disabled
        assert all(not button.disabled for button in dialog.query(Button))
        error = dialog.query_one("#workspace-error", Static)
        assert error.render().plain == (
            ("工作区列表无法保存：" if locale == "zh-Hans" else "Workspace list could not be saved: ")  # noqa: RUF001 - exact localized prose
            + "[Errno 13] Permission denied: '/workspaces/[draft]/workspaces.json'"
        )
        await pilot.pause()
        assert error.region.height > 1
        body = dialog.query_one("#workspace-edit-inner", VerticalScroll)
        for button in dialog.query(Button):
            assert app.screen.region.contains_region(button.region)
            assert button.region.y >= body.region.bottom
            assert app.get_widget_at(*button.region.offset)[0] is button
        if height < 24:
            assert body.max_scroll_y > 0
        body.scroll_end(animate=False, immediate=True)
        await pilot.pause()
        assert error.region.overlaps(body.region)
        if finish == "cancel":
            await pilot.press("escape")
            await wait_for(lambda: bool(results), pilot=pilot)
            assert results == [None]
            assert registry.path.read_bytes() == before
        else:
            await _submit_mutation(pilot, dialog, operation)
            await wait_for(lambda: bool(results), pilot=pilot)
            assert results[0] is not None
            assert registry.path.read_bytes() != before
            if operation == "delete":
                assert registry.snapshot().workspace(original.id) is None
            else:
                assert registry.snapshot().workspace(results[0].workspace.id).name == "Changed"


async def test_edit_keeps_one_derived_button_state_while_registry_write_is_held(registry, folders, monkeypatch):
    """A held save ignores clicks and keys; when it fails, the controls come back as the draft derives them."""
    import threading

    original = registry.create("Original", str(folders[0]), (str(folders[2]),))
    update = registry.update
    entered, release = threading.Event(), threading.Event()

    def held(*args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test did not release workspace registry write")
        return update(*args, **kwargs)

    monkeypatch.setattr(registry, "update", create_autospec(update, side_effect=held))
    results = []
    app = PlaceholderHostApp()
    async with app.run_test() as pilot:
        dialog = workspace_edit_dialog(registry, str(folders[0]), original)
        app.push_screen(dialog, results.append)
        await wait_for(lambda: dialog.is_mounted, pilot=pilot)
        name = dialog.query_one("#workspace-name", EnhancedInput)
        name.value = "Changed"
        save = dialog.query_one("#workspace-save", Button)
        primary = dialog.query_one("#workspace-primary", Static)
        add = dialog.query_one("#workspace-add-folder", Static)
        remove = dialog.query_one(".workspace-folder-remove", Button)
        try:
            await click_when_settled(pilot, save)
            await wait_for(entered.is_set, pilot=pilot)
            assert all(button.disabled for button in dialog.query(Button))
            assert name.disabled and not primary.can_focus
            assert add.disabled and remove.disabled
            # Neither a folder click nor a key reaches a second save, a delete or a close.
            await pilot.press("escape", "enter", "delete")
            assert app.screen is dialog
            assert results == []
            # Another writer takes the name while this write is held.
            occupied = registry.create("Changed", str(folders[1]))
        finally:
            release.set()
        await wait_for(lambda: not name.disabled, pilot=pilot)
        assert (
            dialog.query_one("#workspace-error", Static).render().plain
            == "A workspace with the name Changed already exists."
        )
        assert not save.disabled
        assert not dialog.query_one("#workspace-cancel", Button).disabled
        assert not primary.disabled and not add.disabled and not remove.disabled
        occupied = registry.update(occupied.id, name="Renamed", primary=occupied.primary_cwd, expected=occupied)
        name.value = occupied.name
        await wait_for(
            lambda: not save.disabled and dialog.query_one("#workspace-error", Static).render().plain == "", pilot=pilot
        )
        WorkspaceRegistry(registry.path).delete(occupied.id, expected=occupied)
        name.focus()
        await pilot.press("enter")
        await wait_for(lambda: bool(results), pilot=pilot)
        assert results[0].workspace.id == original.id
        saved = registry.snapshot().workspace(original.id)
        assert (saved.name, saved.working_dirs) == ("Renamed", original.working_dirs)


@pytest.mark.parametrize(("operation", "retry"), [("save", "save"), ("delete", "delete"), ("delete", "save")])
async def test_workspace_dialog_rejects_stale_definition_and_keeps_draft(registry, folders, operation, retry):
    primary, extra = folders[:2]
    original = registry.create("Original", str(primary))
    app = PlaceholderHostApp()
    async with app.run_test(size=(100, 28)) as pilot:
        dialog = workspace_edit_dialog(registry, str(primary), original)
        await _open(app, dialog)
        draft = dialog.query_one("#workspace-name", EnhancedInput)
        draft.value = "Local draft"
        other = WorkspaceRegistry(registry.path)
        elsewhere = other.update(original.id, name="Saved elsewhere", primary=str(primary), folders=[str(extra)])
        await _submit_mutation(pilot, dialog, operation)
        message = (
            "This workspace was edited elsewhere. Close and reopen this dialog to edit it. "
            "Your draft is still here to copy."
        )
        error = dialog.query_one("#workspace-error", Static)
        await wait_for(lambda: error.render().plain == message, pilot=pilot)
        assert app.screen is dialog and draft.value == "Local draft"
        assert registry.snapshot().workspace(original.id) == elsewhere

        draft.value = "Local draft continued"
        await wait_for(lambda: error.render().plain == "", pilot=pilot)
        await _submit_mutation(pilot, dialog, retry)
        await wait_for(lambda: app.screen is not dialog or error.render().plain == message, pilot=pilot)
        assert registry.snapshot().workspace(original.id) == elsewhere
        assert app.screen is dialog and draft.value == "Local draft continued"


@pytest.mark.parametrize("disappears", ["before-save", "name-reused", "during-save"])
async def test_workspace_save_keeps_the_draft_if_the_definition_was_deleted(registry, folders, disappears):
    original = registry.create("Original", str(folders[0]))
    other = WorkspaceRegistry(registry.path)

    async def delete_while_saving(edit):
        await asyncio.to_thread(other.delete, original.id, expected=original)
        await asyncio.to_thread(
            registry.update, original.id, name=edit.name, primary=original.primary_cwd, expected=original
        )

    app = PlaceholderHostApp()
    results = []
    async with app.run_test(size=(100, 28), notifications=True) as pilot:
        if disappears == "during-save":
            dialog = workspace_edit.WorkspaceEditDialog(
                registry, original.primary_cwd, original, update_workspace=delete_while_saving
            )
        else:
            dialog = workspace_edit_dialog(registry, original.primary_cwd, original)
        await app.push_screen(dialog, results.append)
        await wait_for(lambda: dialog.is_mounted, pilot=pilot)
        draft = dialog.query_one("#workspace-name", EnhancedInput)
        draft.value = "Local draft"
        error = dialog.query_one("#workspace-error", Static)
        await wait_for(lambda: draft.has_focus, pilot=pilot)
        replacement = None
        if disappears != "during-save":
            await asyncio.to_thread(other.delete, original.id, expected=original)
        if disappears == "name-reused":
            replacement = await asyncio.to_thread(other.create, original.name, str(folders[1]))

        await _submit_mutation(pilot, dialog, "save")
        message = "This workspace was deleted elsewhere. You can copy your draft before closing this dialog."
        await wait_for(lambda: app.screen is not dialog or error.render().plain == message, pilot=pilot)
        assert app.screen is dialog and draft.value == "Local draft"
        assert error.render().plain == message
        assert draft.has_focus
        assert not results and not app._notifications
        assert registry.snapshot().workspaces == ((replacement,) if replacement is not None else ())


async def test_covered_picker_ignores_queued_selection(registry, folders):
    from textual.screen import ModalScreen

    registry.create("Workspace", str(folders[0]))
    dialog = WorkspacesScreen(registry, str(folders[0]), None)
    async with PlaceholderHostApp().run_test(size=(100, 30)) as pilot:
        await _open(pilot.app, dialog)
        options = dialog.query_one(OptionList)
        covered = ModalScreen()
        pilot.app.push_screen(covered)
        await wait_for(lambda: pilot.app.screen is covered and covered.is_mounted, pilot=pilot)
        options.post_message(OptionList.OptionSelected(options, options.get_option_at_index(0), 0))
        await pilot.pause()
        assert pilot.app.screen is covered and dialog in pilot.app.screen_stack
        assert dialog.dismiss_requested is False
