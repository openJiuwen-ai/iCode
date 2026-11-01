# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for the Runtime Details dialog's Shortcuts tab: tab order, per-folder sections and failure states."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from rich.cells import cell_len
from textual.app import App, ComposeResult
from textual.widgets import Static, Tab, TabbedContent, TabPane

from chrys.app.tui.app import ChrysApp
from chrys.app.tui.screens.dialogs.runtime_details import RuntimeDetailsDialog
from chrys.foundation.events.types import AgentRuntimeDetails
from chrys.foundation.platform import get_platform
from chrys.service.prompt_shortcuts import PromptShortcut
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.pilot import Pilot

_EMPTY_TEXT = "No prompt shortcuts loaded."
_LOADING_TEXT = "Reading prompt shortcuts…"

Sources = list[tuple[Path, list[PromptShortcut]]]


class _HostApp(App):
    def compose(self) -> ComposeResult:
        yield Static("host")


def _command(name: str, *, description: str = "", agent: str = "", model: str = "") -> PromptShortcut:
    return PromptShortcut(
        name=name,
        template=f"Body of {name}",
        path=f"/somewhere/{name}.md",
        description=description,
        agent=agent,
        model=model,
    )


def _sections(dialog: RuntimeDetailsDialog) -> list[Static]:
    pane = dialog.query_one("#runtime-shortcuts", TabPane)
    return list(pane.query(".runtime-detail-section").results(Static))


def _plain(section: Static) -> str:
    return section.content.plain


def _loaded(dialog: RuntimeDetailsDialog) -> bool:
    sections = _sections(dialog)
    return bool(sections) and all(_LOADING_TEXT not in _plain(section) for section in sections)


async def _open(
    pilot: Pilot,
    load_shortcuts: Callable[[], Sources] | None,
    *,
    initial_tab: str | None = "runtime-shortcuts",
    shortcut_model: Callable[[str], str] | None = None,
) -> RuntimeDetailsDialog:
    app = pilot.app
    dialog = RuntimeDetailsDialog(
        AgentRuntimeDetails(), initial_tab=initial_tab, load_shortcuts=load_shortcuts, shortcut_model=shortcut_model
    )
    await app.push_screen(dialog)
    await wait_for(lambda: app.screen is dialog and dialog.is_mounted, pilot=pilot, description="dialog shown")
    await wait_for(lambda: _loaded(dialog), pilot=pilot, description="shortcuts tab filled")
    return dialog


async def test_tabs_are_in_order_with_shortcuts_before_skills() -> None:
    async with _HostApp().run_test(size=(120, 40)) as pilot:
        dialog = await _open(pilot, list, initial_tab=None)
        tabs = dialog.query_one("#runtime-details-tabs", TabbedContent)

        assert [pane.id for pane in tabs.query(TabPane)] == [
            "runtime-model",
            "runtime-tools",
            "runtime-mcp",
            "runtime-shortcuts",
            "runtime-skills",
            "runtime-hooks",
            "runtime-files",
        ]
        assert [tab.label.plain for tab in tabs.query(Tab)] == [
            "Model",
            "Tools",
            "MCP",
            "Shortcuts",
            "Skills",
            "Hooks",
            "Files",
        ]


async def test_one_section_per_folder_in_order_with_aligned_lines() -> None:
    calls: list[tuple[bool, bool]] = []
    main_thread = threading.current_thread()

    def load() -> Sources:
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        calls.append((on_loop, threading.current_thread() is main_thread))
        return [
            (
                Path("/cfg/shortcuts"),
                [
                    _command("a", description="Short one"),
                    _command("longer/name", description="Nested", agent="QA", model="anthropic/m1"),
                    _command("bare"),
                ],
            ),
            (Path("/extra/zfolder"), [_command("only-agent", agent="Code")]),
            (Path("/repo/.chrys/shortcuts"), [_command("model-only", description="Desc", model="gpt-x")]),
        ]

    listed: list[str] = []

    def shortcut_model(spec: str) -> str:
        listed.append(spec)
        return spec.removeprefix("anthropic/")

    async with _HostApp().run_test(size=(160, 50)) as pilot:
        dialog = await _open(pilot, load, shortcut_model=shortcut_model)
        sections = _sections(dialog)

        assert calls == [(False, False)]
        # Each model as the screen lists it; a shortcut without one asks nothing.
        assert sorted(listed) == ["anthropic/m1", "gpt-x"]
        # Each folder as the system writes it (backslashes on Windows).
        assert [str(section.border_title) for section in sections] == [
            str(Path("/cfg/shortcuts")),
            str(Path("/extra/zfolder")),
            str(Path("/repo/.chrys/shortcuts")),
        ]

        first = [line.rstrip() for line in _plain(sections[0]).splitlines()]
        assert [line.split()[0] for line in first] == ["/a", "/longer/name", "/bare"]
        column = cell_len("/longer/name")
        assert first[0][column:].strip().startswith("Short one")
        assert first[0][len("/a") : column].strip() == ""
        assert first[1][column:].strip().startswith("Nested")
        # The agent and the model as listed, unlabelled to keep the row short.
        assert first[1].endswith("Nested  QA · m1")
        assert first[2] == "/bare"
        assert "·" not in first[0]

        (second,) = [line.rstrip() for line in _plain(sections[1]).splitlines()]
        assert second.startswith("/only-agent")
        assert second.endswith("  Code")
        assert "·" not in second

        (third,) = [line.rstrip() for line in _plain(sections[2]).splitlines()]
        assert third.startswith("/model-only")
        assert third.endswith("Desc  gpt-x")
        assert "·" not in third


async def test_without_a_loader_the_tab_says_how_to_choose_folders() -> None:
    async with _HostApp().run_test(size=(160, 50)) as pilot:
        dialog = await _open(pilot, None)
        (section,) = _sections(dialog)
        assert _plain(section) == _EMPTY_TEXT


async def test_empty_result_says_how_to_choose_folders() -> None:
    async with _HostApp().run_test(size=(160, 50)) as pilot:
        dialog = await _open(pilot, list)
        (section,) = _sections(dialog)
        assert _plain(section) == _EMPTY_TEXT


async def test_a_failing_loader_shows_the_empty_state_and_keeps_the_app_running() -> None:
    def load() -> Sources:
        raise OSError("disk went away")

    app = _HostApp()
    async with app.run_test(size=(160, 50)) as pilot:
        dialog = await _open(pilot, load)
        (section,) = _sections(dialog)
        assert _plain(section) == _EMPTY_TEXT
        assert app.is_running
        assert app.screen is dialog

        await pilot.press("escape")
        await wait_for(lambda: not isinstance(app.screen, RuntimeDetailsDialog), pilot=pilot, description="closed")
        assert app.is_running


async def test_control_characters_in_names_and_descriptions_are_sanitized() -> None:
    def load() -> Sources:
        return [
            (
                Path("/cfg/shortcuts"),
                [_command("evil\x1b[31mred", description="ring\x07\x1b]0;title\x07 done\x9b", agent="Q\x1bA")],
            )
        ]

    async with _HostApp().run_test(size=(160, 50)) as pilot:
        dialog = await _open(pilot, load)
        (section,) = _sections(dialog)
        text = _plain(section)
        assert text.startswith("/evil")
        assert "done" in text
        for raw in ("\x1b", "\x07", "\x9b"):
            assert raw not in text


async def test_control_characters_in_a_folder_title_are_sanitized() -> None:
    def load() -> Sources:
        return [(Path("/work/evil\x1b]0;title\x07dir/.chrys/shortcuts"), [_command("ok")])]

    async with _HostApp().run_test(size=(160, 50)) as pilot:
        dialog = await _open(pilot, load)
        (section,) = _sections(dialog)
        title = str(section.border_title)
        assert "evil" in title
        assert title.endswith("shortcuts")
        for raw in ("\x1b", "\x07"):
            assert raw not in title


async def test_main_screen_runtime_details_lists_the_users_shortcut_files(tmp_path: Path) -> None:
    shortcut = get_platform().config_dir / "shortcuts" / "zzruntime.md"
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    shortcut.write_text("---\ndescription: Shown in runtime details\nagent: QA\n---\nBody", encoding="utf-8")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(160, 50)) as pilot:
        assert isinstance(app, ChrysApp)

        def main_ready() -> bool:
            screen = app._main_screen
            return screen is not None and app.screen is screen and not screen._state.run.agent_loading

        await wait_for(main_ready, pilot=pilot, description="main screen ready")
        main = app._main_screen
        assert main is not None
        main.action_runtime_details()
        await wait_for(
            lambda: isinstance(app.screen, RuntimeDetailsDialog) and app.screen.is_mounted,
            pilot=pilot,
            description="runtime details shown",
        )
        dialog = app.screen
        assert isinstance(dialog, RuntimeDetailsDialog)
        await wait_for(lambda: _loaded(dialog), pilot=pilot, description="shortcuts tab filled")

        matching = [section for section in _sections(dialog) if "/zzruntime" in _plain(section)]
        assert len(matching) == 1
        # A long temp path is shortened in the middle to fit the border; its end names the folder.
        assert str(matching[0].border_title).endswith(str(Path(shortcut.parent.parent.name) / "shortcuts"))
        (line,) = [line for line in _plain(matching[0]).splitlines() if line.startswith("/zzruntime")]
        assert "Shown in runtime details" in line
        assert line.rstrip().endswith("  QA")
