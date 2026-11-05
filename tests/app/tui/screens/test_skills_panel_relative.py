# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for workspace-relative skill directory rows."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Button, Checkbox, Input, Label, Static

from chrys.app.tui.screens.agents.panels.skills import SkillDirCard, SkillsConfigPanel
from chrys.service.profiles.agents.schema import SkillsConfig
from tests.support.paths import deny_path_probes


@pytest.fixture
def lexical_preview(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    """Keep previews off the host filesystem: no resolve, and the given spellings report missing.

    ``/abs/path``, ``~/shared`` and ``C:\\foo`` are fixtures, not directories;
    on the platform where a spelling is native the preview would otherwise
    stat the real root, home directory, or drive.
    """
    monkeypatch.setattr(SkillDirCard, "_resolve_for_preview", staticmethod(lambda path: (path, False)))

    def deny(*spellings: str) -> None:
        deny_path_probes(monkeypatch, [os.path.expanduser(spelling) for spelling in spellings])

    return deny


class _SkillsPanelApp(App):
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


async def _mount_panel(panel: SkillsConfigPanel, pilot) -> list[SkillDirCard]:
    await pilot.app.mount(panel)
    await pilot.pause()
    return list(panel.query(SkillDirCard))


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("skills", True),
        ("./skills", True),
        ("../shared", True),
        ("/abs/path", False),
        ("~/shared", False),
        (r"C:\foo", False),
    ],
)
@pytest.mark.asyncio
async def test_skill_dir_relative_checkbox_inferred_from_saved_path(
    path: str, expected: bool, lexical_preview: Callable[..., None]
) -> None:
    lexical_preview(path)
    app = _SkillsPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        cards = await _mount_panel(SkillsConfigPanel(SkillsConfig(paths=[path])), pilot)

        assert cards[0].query_one("#sk-rel-0", Checkbox).value is expected


@pytest.mark.asyncio
async def test_skill_dir_relative_toggle_hides_and_restores_browse() -> None:
    app = _SkillsPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        cards = await _mount_panel(SkillsConfigPanel(SkillsConfig(paths=["/tmp/skills"])), pilot)
        checkbox = cards[0].query_one("#sk-rel-0", Checkbox)
        browse = cards[0].query_one("#sk-browse-0", Button)

        assert browse.display is True

        checkbox.toggle()
        await pilot.pause()
        assert browse.display is False

        checkbox.toggle()
        await pilot.pause()
        assert browse.display is True


@pytest.mark.asyncio
async def test_skill_dir_relative_checkbox_is_below_path_row() -> None:
    app = _SkillsPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        cards = await _mount_panel(SkillsConfigPanel(SkillsConfig(paths=["/tmp/skills"])), pilot)
        card = cards[0]
        path_input = card.query_one("#sk-path-0", Input)
        browse = card.query_one("#sk-browse-0", Button)
        checkbox = card.query_one("#sk-rel-0", Checkbox)

        assert isinstance(path_input.parent, Horizontal)
        assert browse.parent is path_input.parent
        assert checkbox.parent is not path_input.parent


@pytest.mark.asyncio
async def test_validate_rejects_absolute_path_when_relative_checked() -> None:
    app = _SkillsPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = SkillsConfigPanel(SkillsConfig(paths=["/abs/skills"]), workspace_cwd="/repo")
        cards = await _mount_panel(panel, pilot)
        cards[0].query_one("#sk-rel-0", Checkbox).toggle()
        await pilot.pause()

        errors = panel.validate()

    assert "Skill Directory 1: '/abs/skills' is absolute; turn off Workspace relative." in errors


@pytest.mark.parametrize("path", ["/definitely/missing", r"C:\definitely\missing"])
@pytest.mark.asyncio
async def test_missing_absolute_preview_says_path_does_not_exist(
    path: str, lexical_preview: Callable[..., None]
) -> None:
    """Both native and foreign-absolute missing paths must surface as missing.

    Windows reads ``/definitely/missing`` as drive-relative (no drive letter)
    and POSIX reads ``C:\\definitely\\missing`` as a relative-looking name —
    in both cases ``Path.is_absolute()`` returns False on the host platform,
    but ``is_absolute_path`` (cross-platform) returns True. The preview must
    still resolve and report missing rather than silently dropping the note.
    """
    lexical_preview(path)
    app = _SkillsPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        cards = await _mount_panel(SkillsConfigPanel(SkillsConfig(paths=[path])), pilot)
        preview = cards[0].query_one("#sk-preview-0", Label)

        assert "Path does not exist" in str(preview.content)
        assert "Missing in current workspace" not in str(preview.content)


@pytest.mark.parametrize("name", ["a\x1b[31mred", "bad\udcff"], ids=["escape-sequence", "undecodable-byte"])
@pytest.mark.asyncio
async def test_preview_shows_a_cleaned_copy_while_the_row_keeps_the_raw_path(
    name: str, lexical_preview: Callable[..., None]
) -> None:
    """A folder name's control character or undecodable byte never reaches the terminal; the saved path is as typed."""
    path = f"/abs/{name}"
    lexical_preview(path)
    app = _SkillsPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        cards = await _mount_panel(SkillsConfigPanel(SkillsConfig(paths=[path])), pilot)
        preview = str(cards[0].query_one("#sk-preview-0", Label).content)

        assert "Current: /abs/" in preview
        assert "\x1b" not in preview
        assert not any("\ud800" <= char <= "\udfff" for char in preview)
        assert cards[0].get_path() == path


@pytest.mark.asyncio
async def test_skill_dir_preview_handles_resolve_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _SkillsPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = SkillsConfigPanel(SkillsConfig(paths=["skills"]), workspace_cwd="/repo")
        cards = await _mount_panel(panel, pilot)

        def fail_resolve(self: Path) -> Path:
            raise OSError("resolve failed")

        monkeypatch.setattr(Path, "resolve", fail_resolve)
        cards[0]._refresh_preview()
        await pilot.pause()

        preview = cards[0].query_one("#sk-preview-0", Label)
        expected_path = str(Path("/repo") / "skills")
        assert f"Current: {expected_path}" in str(preview.content)
        assert "Missing in current workspace" in str(preview.content)


@pytest.mark.asyncio
async def test_validate_allows_workspace_relative_path_when_checked() -> None:
    app = _SkillsPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = SkillsConfigPanel(SkillsConfig(paths=["skills"]), workspace_cwd="/repo")
        await _mount_panel(panel, pilot)

        assert panel.validate() == []


@pytest.mark.asyncio
async def test_validate_rejects_relative_path_when_unchecked() -> None:
    app = _SkillsPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = SkillsConfigPanel(SkillsConfig(paths=["/repo/skills"]), workspace_cwd="/repo")
        cards = await _mount_panel(panel, pilot)
        path_input = cards[0].query_one("#sk-path-0", Input)
        path_input.value = "skills"
        await pilot.pause()

        errors = panel.validate()

    assert "Skill Directory 1: 'skills' is relative; enable Workspace relative." in errors


@pytest.mark.parametrize(
    "paths",
    [
        ["skills", "./skills"],
        ["skills", "/repo/skills"],
    ],
)
@pytest.mark.asyncio
async def test_validate_dedupes_after_workspace_relative_resolution(paths: list[str]) -> None:
    app = _SkillsPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        panel = SkillsConfigPanel(SkillsConfig(paths=paths), workspace_cwd="/repo")
        await _mount_panel(panel, pilot)

        errors = panel.validate()

    assert any("Skill Directory 2" in error and "duplicates Skill Directory 1" in error for error in errors)


@pytest.mark.asyncio
async def test_browse_initial_path_prefers_workspace_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _SkillsPanelApp()
    pushed: dict[str, object] = {}

    async with app.run_test(size=(120, 40)) as pilot:
        panel = SkillsConfigPanel(SkillsConfig(paths=[""]), workspace_cwd=str(tmp_path))
        cards = await _mount_panel(panel, pilot)

        def fake_push_screen(screen: object, callback: object | None = None) -> None:
            pushed["screen"] = screen
            pushed["callback"] = callback

        monkeypatch.setattr(app, "push_screen", fake_push_screen)
        cards[0].query_one("#sk-browse-0", Button).press()
        await pilot.pause()

    assert pushed["screen"]._initial_path == str(tmp_path)


@pytest.mark.parametrize(
    ("os_name", "absolute_placeholder", "relative_placeholder"),
    [
        ("windows", r"C:\path\to\skills", r"relative\path\to\skills"),
        ("macos", "/Users/you/skills", "relative/path/to/skills"),
        ("linux", "/home/you/skills", "relative/path/to/skills"),
    ],
)
@pytest.mark.asyncio
async def test_skill_dir_placeholder_examples_are_platform_specific(
    os_name: str,
    absolute_placeholder: str,
    relative_placeholder: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_fake_platform(monkeypatch, os_name)
    app = _SkillsPanelApp()
    async with app.run_test(size=(120, 40)) as pilot:
        cards = await _mount_panel(SkillsConfigPanel(SkillsConfig(paths=[""])), pilot)
        path_input = cards[0].query_one("#sk-path-0", Input)
        checkbox = cards[0].query_one("#sk-rel-0", Checkbox)

        assert path_input.placeholder == absolute_placeholder

        checkbox.toggle()
        await pilot.pause()

        assert path_input.placeholder == relative_placeholder
