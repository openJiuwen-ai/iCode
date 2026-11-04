# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the Memory configuration panel."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Checkbox, Input, Label, Static

from chrys.app.tui.screens.agents.panels import memory as memory_module
from chrys.app.tui.screens.agents.panels.memory import MemoryConfigPanel, MemoryFileCard, MemoryFolderCard
from chrys.app.tui.widgets.path_entry import PathEntryCard
from chrys.service.profiles.agents.schema import MemoryConfig
from tests.support.paths import deny_path_probes
from tests.support.waiting import wait_for


@pytest.fixture
def lexical_preview(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Keep previews off the host filesystem: no resolve, and the given spellings report missing.

    ``/abs/notes.md``, ``~/notes.md`` and ``C:\\notes.md`` are fixtures, not
    files; on the platform where a spelling is native the preview would
    otherwise stat the real root, home directory, or drive.
    """
    monkeypatch.setattr(PathEntryCard, "_resolve_for_preview", staticmethod(lambda path: (path, False)))

    def deny(*spellings: str) -> None:
        deny_path_probes(monkeypatch, [os.path.expanduser(spelling) for spelling in spellings])

    return deny


class _MemoryPanelApp(App):
    def compose(self) -> ComposeResult:
        yield Static("placeholder")


def _set_fake_platform(monkeypatch: pytest.MonkeyPatch, os_name: str) -> None:
    fake_platform = type(
        "P",
        (),
        {
            "is_macos": os_name == "macos",
            "is_windows": os_name == "windows",
            "is_linux": os_name == "linux",
        },
    )()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)


async def _mount_panel(panel: MemoryConfigPanel, pilot) -> list[MemoryFileCard]:
    await pilot.app.mount(panel)
    await pilot.pause()
    return list(panel.query(MemoryFileCard))


async def _wait_for_memory_cards(
    panel: MemoryConfigPanel,
    pilot,
    *,
    files: int,
    folders: int,
) -> tuple[list[MemoryFileCard], list[MemoryFolderCard]]:
    def cards_ready() -> bool:
        file_cards = list(panel.query(MemoryFileCard))
        folder_cards = list(panel.query(MemoryFolderCard))
        if len(file_cards) != files or len(folder_cards) != folders:
            return False
        file_inputs_ready = all(
            file_cards[index].is_mounted and file_cards[index].query(f"#mem-file-path-{index}")
            for index in range(files)
        )
        folder_inputs_ready = all(
            folder_cards[index].is_mounted and folder_cards[index].query(f"#mem-folder-path-{index}")
            for index in range(folders)
        )
        return file_inputs_ready and folder_inputs_ready

    await wait_for(
        cards_ready, pilot=pilot, description=f"{files} memory file and {folders} folder path inputs are mounted"
    )
    return list(panel.query(MemoryFileCard)), list(panel.query(MemoryFolderCard))


@pytest.mark.parametrize(
    (
        "os_name",
        "absolute_file_placeholder",
        "relative_file_placeholder",
        "absolute_folder_placeholder",
        "relative_folder_placeholder",
    ),
    [
        (
            "windows",
            r"C:\path\to\file.md",
            r"relative\path\to\file.md",
            r"C:\path\to\folder",
            r"relative\path\to\folder",
        ),
        ("macos", "/Users/you/file.md", "relative/path/to/file.md", "/Users/you/folder", "relative/path/to/folder"),
        ("linux", "/home/you/file.md", "relative/path/to/file.md", "/home/you/folder", "relative/path/to/folder"),
    ],
)
@pytest.mark.asyncio
async def test_memory_path_placeholders_are_platform_specific(
    os_name: str,
    absolute_file_placeholder: str,
    relative_file_placeholder: str,
    absolute_folder_placeholder: str,
    relative_folder_placeholder: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_fake_platform(monkeypatch, os_name)
    app = _MemoryPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MemoryConfigPanel(MemoryConfig(files=[""], folders=[""]))
        await _mount_panel(panel, pilot)

        file_input = panel.query_one("#mem-file-path-0", Input)
        checkbox = panel.query_one("#mem-file-rel-0", Checkbox)
        folder_input = panel.query_one("#mem-folder-path-0", Input)
        folder_checkbox = panel.query_one("#mem-folder-rel-0", Checkbox)

        # Empty rows infer absolute mode for files and folders alike.
        assert file_input.placeholder == absolute_file_placeholder
        assert folder_input.placeholder == absolute_folder_placeholder

        checkbox.toggle()
        folder_checkbox.toggle()
        await pilot.pause()

        assert file_input.placeholder == relative_file_placeholder
        assert folder_input.placeholder == relative_folder_placeholder


@pytest.mark.asyncio
async def test_add_memory_paths_insert_new_cards_before_existing_paths() -> None:
    app = _MemoryPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MemoryConfigPanel(MemoryConfig(files=["docs/a.md"], folders=["notes"]))
        await _mount_panel(panel, pilot)

        panel.query_one("#mem-add-file", Button).press()
        panel.query_one("#mem-add-folder", Button).press()

        file_cards, folder_cards = await _wait_for_memory_cards(panel, pilot, files=2, folders=2)
        assert file_cards[0].query_one("#mem-file-path-0", Input).value == ""
        assert file_cards[1].query_one("#mem-file-path-1", Input).value == "docs/a.md"
        assert folder_cards[0].query_one("#mem-folder-path-0", Input).value == ""
        assert folder_cards[1].query_one("#mem-folder-path-1", Input).value == "notes"
        assert panel.get_config().files == ["docs/a.md"]
        assert panel.get_config().folders == ["notes"]


@pytest.mark.parametrize("kind", ["file", "folder"])
async def test_remove_memory_path_waits_for_old_cards_and_preserves_edits(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    unmount_started, release_unmount = asyncio.Event(), asyncio.Event()
    release_unmount.set()

    class HeldMemoryFileCard(MemoryFileCard):
        async def on_unmount(self) -> None:
            if self._index == 1:
                unmount_started.set()
                await release_unmount.wait()

    class HeldMemoryFolderCard(MemoryFolderCard):
        async def on_unmount(self) -> None:
            if self._index == 1:
                unmount_started.set()
                await release_unmount.wait()

    monkeypatch.setattr(memory_module, "MemoryFileCard", HeldMemoryFileCard)
    monkeypatch.setattr(memory_module, "MemoryFolderCard", HeldMemoryFolderCard)
    app = _MemoryPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        initial = MemoryConfig(files=["docs/one.md", "docs/two.md"], folders=["notes/one", "notes/two"])
        panel = MemoryConfigPanel(initial)
        await app.mount(panel)
        files, folders = await _wait_for_memory_cards(panel, pilot, files=2, folders=2)
        cards = files if kind == "file" else folders
        edited = "docs/edited.md" if kind == "file" else "notes/edited"
        cards[1].query_one(f"#mem-{kind}-path-1", Input).value = edited
        expected = MemoryConfig(
            files=[edited] if kind == "file" else initial.files,
            folders=[edited] if kind == "folder" else initial.folders,
        )
        container = panel.query_one(f"#mem-{kind}s")
        with patch.object(container, "mount", autospec=True, side_effect=container.mount) as mount:
            try:
                release_unmount.clear()
                cards[0].query_one("#mem-delete-btn-0", Button).press()
                await wait_for(unmount_started.is_set, description="old memory cards started unmounting")
                card_type = MemoryFileCard if kind == "file" else MemoryFolderCard
                await wait_for(
                    lambda: list(panel.query(card_type)) == [cards[1]],
                    description="only the held old memory card remains",
                )
                assert cards[1].get_path() == ("docs/two.md" if kind == "file" else "notes/two")
                mount.assert_not_called()
                # The other section still has live inputs during this rebuild.
                if kind == "file":
                    folders[0].query_one("#mem-folder-path-0", Input).value = "notes/live-edit"
                    expected.folders[0] = "notes/live-edit"
                else:
                    files[0].query_one("#mem-file-path-0", Input).value = "docs/live-edit.md"
                    expected.files[0] = "docs/live-edit.md"
                assert panel.get_config() == expected
            finally:
                release_unmount.set()

        files, folders = await _wait_for_memory_cards(
            panel, pilot, files=len(expected.files), folders=len(expected.folders)
        )
        remaining = files if kind == "file" else folders
        assert remaining[0].query_one(f"#mem-{kind}-path-0", Input).value == edited
        assert panel.get_config() == expected
        remaining[0].query_one(f"#mem-{kind}-path-0", Input).value = "edited-again.md"
        if kind == "file":
            expected.files[0] = "edited-again.md"
        else:
            expected.folders[0] = "edited-again.md"
        assert panel.get_config() == expected


@pytest.mark.parametrize("kind", ["file", "folder"])
async def test_remove_memory_path_preserves_scroll_position(kind: str) -> None:
    app = _MemoryPanelApp()
    async with app.run_test(size=(120, 24)) as pilot:
        paths = [f"docs/path-{index}.md" for index in range(15)]
        panel = MemoryConfigPanel(
            MemoryConfig(files=paths if kind == "file" else [], folders=paths if kind == "folder" else [])
        )
        await app.mount(panel)
        files, folders = await _wait_for_memory_cards(
            panel, pilot, files=15 if kind == "file" else 0, folders=15 if kind == "folder" else 0
        )
        cards = files if kind == "file" else folders
        await wait_for(lambda: panel.max_scroll_y > 100, pilot=pilot)
        panel.scroll_to(y=100, animate=False, immediate=True)
        await wait_for(lambda: panel.scroll_y == 100, pilot=pilot)
        previous_max = panel.max_scroll_y

        container = panel.query_one(f"#mem-{kind}s")
        remove_children = container.remove_children

        async def remove_before_refresh() -> None:
            await remove_children()
            app.screen.refresh(layout=True)
            app.screen._on_timer_update()

        with patch.object(container, "remove_children", autospec=True, side_effect=remove_before_refresh):
            cards[-1].query_one("#mem-delete-btn-14", Button).press()
            await _wait_for_memory_cards(
                panel, pilot, files=14 if kind == "file" else 0, folders=14 if kind == "folder" else 0
            )
        await wait_for(
            lambda: 100 < panel.max_scroll_y < previous_max,
            pilot=pilot,
            description="remaining memory cards have been laid out",
        )
        assert panel.scroll_y == 100


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("notes.md", True),
        ("./notes.md", True),
        ("../shared/notes.md", True),
        ("/abs/notes.md", False),
        ("~/notes.md", False),
        (r"C:\notes.md", False),
    ],
)
@pytest.mark.asyncio
async def test_memory_file_relative_checkbox_inferred_from_saved_path(
    path: str, expected: bool, lexical_preview: Callable[..., None]
) -> None:
    lexical_preview(path)
    app = _MemoryPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        cards = await _mount_panel(MemoryConfigPanel(MemoryConfig(files=[path])), pilot)

        assert cards[0].query_one("#mem-file-rel-0", Checkbox).value is expected


@pytest.mark.asyncio
async def test_memory_file_relative_toggle_hides_and_restores_browse() -> None:
    app = _MemoryPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        cards = await _mount_panel(MemoryConfigPanel(MemoryConfig(files=["/tmp/notes.md"])), pilot)
        checkbox = cards[0].query_one("#mem-file-rel-0", Checkbox)
        browse = cards[0].query_one("#mem-file-browse-0", Button)

        assert browse.display is True

        checkbox.toggle()
        await pilot.pause()
        assert browse.display is False

        checkbox.toggle()
        await pilot.pause()
        assert browse.display is True


@pytest.mark.asyncio
async def test_memory_file_relative_checkbox_is_below_path_row_and_folders_match() -> None:
    app = _MemoryPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MemoryConfigPanel(MemoryConfig(files=["/tmp/notes.md"], folders=["docs"]))
        cards = await _mount_panel(panel, pilot)
        card = cards[0]
        path_input = card.query_one("#mem-file-path-0", Input)
        browse = card.query_one("#mem-file-browse-0", Button)
        checkbox = card.query_one("#mem-file-rel-0", Checkbox)

        assert isinstance(path_input.parent, Horizontal)
        assert browse.parent is path_input.parent
        assert checkbox.parent is not path_input.parent
        # Folder rows carry the same path-mode chrome as file rows.
        folder_checkbox = panel.query_one("#mem-folder-rel-0", Checkbox)
        assert folder_checkbox.value is True
        # Relative mode hides Browse (same as file rows).
        assert panel.query_one("#mem-folder-browse-0", Button).display is False


@pytest.mark.asyncio
async def test_memory_file_validate_rejects_absolute_path_when_relative_checked() -> None:
    app = _MemoryPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MemoryConfigPanel(MemoryConfig(files=["/abs/notes.md"]), workspace_cwd="/repo")
        cards = await _mount_panel(panel, pilot)
        cards[0].query_one("#mem-file-rel-0", Checkbox).toggle()
        await pilot.pause()

        errors = panel.validate()

    assert "Memory File 1: '/abs/notes.md' is absolute; turn off Workspace relative." in errors


@pytest.mark.asyncio
async def test_memory_file_validate_rejects_relative_path_when_unchecked() -> None:
    app = _MemoryPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MemoryConfigPanel(MemoryConfig(files=["notes.md"]), workspace_cwd="/repo")
        cards = await _mount_panel(panel, pilot)
        cards[0].query_one("#mem-file-rel-0", Checkbox).toggle()
        await pilot.pause()

        errors = panel.validate()

    assert "Memory File 1: 'notes.md' is relative; enable Workspace relative." in errors


@pytest.mark.asyncio
async def test_memory_folder_validate_rejects_absolute_path_when_relative_checked() -> None:
    app = _MemoryPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MemoryConfigPanel(MemoryConfig(folders=["/abs/notes"]), workspace_cwd="/repo")
        await pilot.app.mount(panel)
        await _wait_for_memory_cards(panel, pilot, files=0, folders=1)
        panel.query_one("#mem-folder-rel-0", Checkbox).toggle()
        await pilot.pause()

        errors = panel.validate()

    assert "Memory Folder 1: '/abs/notes' is absolute; turn off Workspace relative." in errors


@pytest.mark.asyncio
async def test_memory_folder_validate_rejects_relative_path_when_unchecked() -> None:
    app = _MemoryPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MemoryConfigPanel(MemoryConfig(folders=["notes"]), workspace_cwd="/repo")
        await pilot.app.mount(panel)
        await _wait_for_memory_cards(panel, pilot, files=0, folders=1)
        panel.query_one("#mem-folder-rel-0", Checkbox).toggle()
        await pilot.pause()

        errors = panel.validate()

    assert "Memory Folder 1: 'notes' is relative; enable Workspace relative." in errors


@pytest.mark.asyncio
async def test_memory_folder_validate_allows_absolute_path_when_unchecked() -> None:
    app = _MemoryPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MemoryConfigPanel(MemoryConfig(folders=["/abs/notes"]), workspace_cwd="/repo")
        await pilot.app.mount(panel)
        await _wait_for_memory_cards(panel, pilot, files=0, folders=1)

        errors = panel.validate()

    assert errors == []


@pytest.mark.asyncio
async def test_memory_file_validate_allows_absolute_path_when_unchecked() -> None:
    app = _MemoryPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MemoryConfigPanel(MemoryConfig(files=["/abs/notes.md"]), workspace_cwd="/repo")
        await _mount_panel(panel, pilot)

        errors = panel.validate()

    assert errors == []


@pytest.mark.asyncio
async def test_memory_file_preview_handles_resolve_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MemoryPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = MemoryConfigPanel(MemoryConfig(files=["notes.md"]), workspace_cwd="/repo")
        cards = await _mount_panel(panel, pilot)

        def fail_resolve(self: Path) -> Path:
            raise OSError("resolve failed")

        monkeypatch.setattr(Path, "resolve", fail_resolve)
        cards[0]._refresh_preview()
        await pilot.pause()

        preview = cards[0].query_one("#mem-file-preview-0", Label)
        expected_path = str(Path("/repo") / "notes.md")
        assert f"Current: {expected_path}" in str(preview.content)
        assert "Missing in current workspace" in str(preview.content)


@pytest.mark.asyncio
async def test_memory_file_foreign_absolute_preview_reports_missing(lexical_preview: Callable[..., None]) -> None:
    lexical_preview(r"C:\notes.md")
    app = _MemoryPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        cards = await _mount_panel(MemoryConfigPanel(MemoryConfig(files=[r"C:\notes.md"])), pilot)
        preview = cards[0].query_one("#mem-file-preview-0", Label)

        assert "File does not exist" in str(preview.content)
        assert "Missing in current workspace" not in str(preview.content)


@pytest.mark.asyncio
async def test_memory_file_browse_initial_path_prefers_workspace_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _MemoryPanelApp()
    pushed: dict[str, object] = {}

    async with app.run_test(size=(120, 40)) as pilot:
        panel = MemoryConfigPanel(MemoryConfig(files=[""]), workspace_cwd=str(tmp_path))
        cards = await _mount_panel(panel, pilot)

        def fake_push_screen(screen: object, callback: object | None = None) -> None:
            pushed["screen"] = screen
            pushed["callback"] = callback

        monkeypatch.setattr(app, "push_screen", fake_push_screen)
        cards[0].query_one("#mem-file-browse-0", Button).press()
        await pilot.pause()

    assert pushed["screen"]._initial_path == str(tmp_path)


def test_memory_get_config_preserves_seed_before_children_mount() -> None:
    panel = MemoryConfigPanel(MemoryConfig(files=["docs/a.md"], folders=["notes"]))
    card = MemoryFileCard("docs/a.md", index=0)

    cfg = panel.get_config()

    assert cfg.files == ["docs/a.md"]
    assert cfg.folders == ["notes"]
    assert card.get_path() == "docs/a.md"
