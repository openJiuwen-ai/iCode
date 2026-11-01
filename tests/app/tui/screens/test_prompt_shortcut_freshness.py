# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests in a real app: the / list's shortcuts are only ever used for the folders and switches they were read under.

When the workspace or a ``prompt_shortcuts`` setting changes, shortcuts read
under the old ones stop at once — not listed, not matched, not expanded when
typed, not expanded from a row still on screen — and one new read starts.
A refresh under unchanged folders keeps the current list while it reads. Once
the new read lands, its shortcuts are the ones used.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from textual.widgets import Button

import chrys.app.tui.screens.main.suggestions as suggestions_module
from chrys.app.tui.app import ChrysApp
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.chrome.input_bar import InputBar, _ChatTextArea
from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import AgentRuntimeDetails, RuntimeSkillDetails, UserMessage, UserRetry
from chrys.foundation.platform import get_platform
from chrys.service.prompt_shortcuts import PromptShortcut, load_prompt_shortcuts
from tests.support.tui_app_harness import SessionGenerationEngine, make_chrys_app
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

if TYPE_CHECKING:
    from textual.pilot import Pilot

_SECTION_SHORTCUTS = "Prompt Shortcuts"
_SECTIONS = ("System Commands", _SECTION_SHORTCUTS, "Loaded Skills")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _global_shortcut(name: str, text: str) -> None:
    _write(get_platform().config_dir / "shortcuts" / f"{name}.md", text)


def _project_shortcut(root: Path, name: str, text: str) -> None:
    _write(root / ".chrys" / "shortcuts" / f"{name}.md", text)


class _GatedLoader:
    """Stands in for the shortcut loader: records every read and, while held, keeps each from landing."""

    def __init__(self) -> None:
        self.reads: list[dict[str, Any]] = []
        self.held = False
        self.release = threading.Event()

    def __call__(self, **folders: Any) -> list[PromptShortcut]:
        self.reads.append(folders)
        if self.held:
            # Runs on a worker thread; bounded so a failing test never wedges teardown.
            self.release.wait(ENGINE_TURN_TIMEOUT)
        return load_prompt_shortcuts(**folders)


@pytest.fixture
def loader(monkeypatch: pytest.MonkeyPatch) -> Any:
    gated = _GatedLoader()
    monkeypatch.setattr(suggestions_module, "load_prompt_shortcuts", gated)
    yield gated
    gated.release.set()


async def _ready(pilot: Pilot) -> tuple[MainScreen, _ChatTextArea]:
    app = pilot.app
    assert isinstance(app, ChrysApp)

    def ready() -> bool:
        screen = app._main_screen
        return (
            screen is not None
            and app.screen is screen
            and not screen._state.run.agent_loading
            and screen.query_one("#chat-input").has_focus
        )

    await wait_for(ready, pilot=pilot, description="main screen ready with chat input focused")
    main = app._main_screen
    assert main is not None
    return main, main.query_one("#chat-input", _ChatTextArea)


def _list(main: MainScreen) -> SuggestionList:
    return main.query_one(SuggestionList)


def _list_open(main: MainScreen) -> bool:
    suggestions = _list(main)
    return suggestions.is_visible and not suggestions.is_loading


def _shortcut_rows(main: MainScreen) -> list[str]:
    rows = [content.plain for content in _list(main)._contents]
    if _SECTION_SHORTCUTS not in rows:
        return []
    group: list[str] = []
    for row in rows[rows.index(_SECTION_SHORTCUTS) + 1 :]:
        if row in _SECTIONS:
            break
        if row.strip():
            group.append(row.split()[0])
    return group


async def _reset(pilot: Pilot, main: MainScreen, chat: _ChatTextArea) -> None:
    if _list(main).is_visible:
        await pilot.press("escape")
    chat.clear()
    await wait_for(lambda: chat.text == "" and not _list(main).is_visible, pilot=pilot, description="composer reset")


async def _open_list(pilot: Pilot, main: MainScreen, chat: _ChatTextArea, text: str = "/zz") -> None:
    chat.insert(text)
    await wait_for(lambda: chat.text == text and _list_open(main), pilot=pilot, description=f"list open for {text}")


async def _typed_tab(pilot: Pilot, main: MainScreen, chat: _ChatTextArea, text: str) -> None:
    """Type ``/name args`` and press Tab, as a user expanding a shortcut by name."""
    await _reset(pilot, main, chat)
    chat.insert(text)
    await wait_for(lambda: chat.text == text, pilot=pilot, description="typed")
    await pilot.press("tab")
    await pilot.pause()


async def _highlight(pilot: Pilot, main: MainScreen, name: str) -> None:
    suggestions = _list(main)
    rows = [content.plain for content in suggestions._contents]
    target = next(
        i for i, row in enumerate(rows) if row.split()[:1] == [f"/{name}"] and i > rows.index(_SECTION_SHORTCUTS)
    )
    for _ in range(len(rows)):
        if suggestions._highlighted == target:
            return
        await pilot.press("down")
    pytest.fail(f"could not highlight /{name}")


async def test_a_workspace_switch_stops_the_old_shortcuts_until_the_new_read_lands(
    tmp_path: Path, loader: _GatedLoader
) -> None:
    _global_shortcut("zzglob", "Global body")
    _project_shortcut(Path.cwd(), "zzproj", "Project body")
    elsewhere = tmp_path / "elsewhere"
    _project_shortcut(elsewhere, "zzother", "Other body")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat = await _ready(pilot)
        handler = main._suggestions
        await wait_for(
            lambda: handler.resolve_typed_name("/zzproj x").kind == "shortcut",
            pilot=pilot,
            description="first read landed",
        )
        await _open_list(pilot, main, chat)
        await wait_for(
            lambda: _shortcut_rows(main) == ["/zzglob", "/zzproj"], pilot=pilot, description="old rows listed"
        )
        await _highlight(pilot, main, "zzproj")

        loader.held = True
        reads_before = len(loader.reads)
        main._set_workspace_cwd(str(elsewhere))

        # A row still on screen from the old read expands nothing.
        await pilot.press("tab")
        await pilot.pause()
        assert chat.text == "/zz"
        assert not main._prompt_card.active
        # Nothing read under the old folders matches or expands, the global shortcut included.
        assert handler.resolve_typed_name("/zzproj x").kind != "shortcut"
        assert handler.resolve_typed_name("/zzglob x").kind != "shortcut"
        assert not handler.expand_typed_name(handler.resolve_typed_name("/zzproj x"))
        await _typed_tab(pilot, main, chat, "/zzglob x")
        assert chat.text == "/zzglob x"
        await _typed_tab(pilot, main, chat, "/zzproj x")
        assert chat.text == "/zzproj x"
        # Nor is it listed.
        await _reset(pilot, main, chat)
        await _open_list(pilot, main, chat)
        assert _shortcut_rows(main) == []
        # A new read started for the new workspace; more keystrokes start no more.
        new_reads = [read for read in loader.reads[reads_before:] if read["workspace"] == str(elsewhere)]
        assert new_reads
        assert all(read["workspace"] == str(elsewhere) for read in loader.reads[reads_before:])
        reads_while_typing = len(loader.reads)
        await pilot.press("o", "t", "h")
        await wait_for(lambda: chat.text == "/zzoth", pilot=pilot, description="filter typed")
        assert len(loader.reads) == reads_while_typing

        loader.release.set()
        await wait_for(
            lambda: handler.resolve_typed_name("/zzother x").kind == "shortcut",
            pilot=pilot,
            description="new read landed",
        )
        assert handler.resolve_typed_name("/zzproj x").kind != "shortcut"
        assert handler.resolve_typed_name("/zzglob x").kind == "shortcut"
        await _typed_tab(pilot, main, chat, "/zzother y")
        await wait_for(lambda: chat.text == "Other body\n\ny", pilot=pilot, description="new shortcut expanded")


async def test_a_refresh_under_unchanged_folders_keeps_the_list_while_it_reads(
    tmp_path: Path, loader: _GatedLoader
) -> None:
    _global_shortcut("zzglob", "Global body")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat = await _ready(pilot)
        handler = main._suggestions
        await wait_for(
            lambda: handler.resolve_typed_name("/zzglob x").kind == "shortcut",
            pilot=pilot,
            description="first read landed",
        )
        reads_before = len(loader.reads)
        loader.held = True

        handler.refresh_prompt_shortcuts()

        await wait_for(lambda: len(loader.reads) > reads_before, pilot=pilot, description="refresh read started")
        assert len(loader.reads) == reads_before + 1
        assert handler.resolve_typed_name("/zzglob x").kind == "shortcut"
        await _open_list(pilot, main, chat)
        assert _shortcut_rows(main) == ["/zzglob"]
        await _typed_tab(pilot, main, chat, "/zzglob y")
        await wait_for(lambda: chat.text == "Global body\n\ny", pilot=pilot, description="expanded meanwhile")
        loader.release.set()


# -- a /name typed while the shortcuts are still being read -----------------------

_STILL_READING = "Prompt shortcuts are still being read; press Enter again in a moment."


class _Published:
    def __init__(self) -> None:
        self.events: list[UserMessage | UserRetry] = []

    async def __call__(self, event: UserMessage | UserRetry) -> None:
        self.events.append(event)

    def described(self) -> list[tuple[str, str]]:
        return [(type(event).__name__, event.text) for event in self.events]


def _warnings(app: ChrysApp) -> list[str]:
    return [str(notification.message) for notification in app._notifications]


async def _app_with_bus(tmp_path: Path) -> tuple[ChrysApp, _Published]:
    bus = EventBus()
    published = _Published()
    await bus.subscribe(UserMessage, published)
    await bus.subscribe(UserRetry, published)
    return make_chrys_app(tmp_path / "state", engine=SessionGenerationEngine(), event_bus=bus), published


async def _sendable(pilot: Pilot, monkeypatch: pytest.MonkeyPatch) -> tuple[MainScreen, _ChatTextArea]:
    main, chat = await _ready(pilot)
    main._state.runtime.details = AgentRuntimeDetails(
        skill_details=[RuntimeSkillDetails(name="zzskill", description="A loaded skill")]
    )
    # The harness has no usable model; every send would stop at the model setup dialog.
    monkeypatch.setattr(main, "_model_unconfigured", lambda: False)
    return main, chat


async def _send_with(pilot: Pilot, main: MainScreen, chat: _ChatTextArea, entry: str, text: str) -> None:
    """Send *text* from an empty composer through *entry*."""
    await _reset(pilot, main, chat)
    if not chat.has_focus:
        chat.focus()
        await wait_for(lambda: chat.has_focus, pilot=pilot, description="composer focused")
    input_bar = main.query_one(InputBar)
    if entry == "continue":
        input_bar.retry_mode = True
        await wait_for(lambda: input_bar.retry_mode, pilot=pilot, description="continue state")
    chat.insert(text)
    await wait_for(lambda: chat.text == text, pilot=pilot, description="typed")
    suggestions = _list(main)
    if entry == "list_fallback":
        # The / list is open on a name it lists nothing for: Enter falls back to sending the text.
        await wait_for(lambda: _list_open(main), pilot=pilot, description="list open")
        assert suggestions._highlighted is None
        await pilot.press("enter")
        return
    await wait_for(
        lambda: not suggestions.is_visible or not suggestions.is_loading, pilot=pilot, description="list settled"
    )
    if suggestions.is_visible:
        await pilot.press("escape")
        await wait_for(lambda: not suggestions.is_visible, pilot=pilot, description="list dismissed")
    if entry == "send_button":
        # The button's own Pressed message, as a click posts it; where the click lands is not under test.
        send = main.query_one("#send-btn", Button)
        await wait_for(lambda: not send.disabled, pilot=pilot, description="send button enabled by the typed text")
        send.press()
    else:
        await pilot.press("enter")


@pytest.mark.parametrize("entry", ["enter", "send_button", "continue", "list_fallback"])
async def test_a_shortcut_name_sent_before_its_read_lands_waits_and_then_expands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loader: _GatedLoader, entry: str
) -> None:
    elsewhere = tmp_path / "elsewhere"
    _project_shortcut(elsewhere, "zzqqq", "Review body")
    app, published = await _app_with_bus(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat = await _sendable(pilot, monkeypatch)
        handler = main._suggestions
        await wait_for(lambda: handler._shortcuts() is not None, pilot=pilot, description="startup read landed")
        loader.held = True
        reads_before = len(loader.reads)
        main._set_workspace_cwd(str(elsewhere))
        text = "/zzqqq" if entry == "list_fallback" else "/zzqqq x"

        await _send_with(pilot, main, chat, entry, text)

        await wait_for(lambda: _STILL_READING in _warnings(app), pilot=pilot, description="still-reading warning")
        await wait_for(lambda: chat.text == text, pilot=pilot, description="text back in the composer")
        await pilot.pause()
        assert published.events == []
        assert not main._state.submit.active
        # One read for the new workspace, however many callers asked meanwhile.
        await wait_for(lambda: len(loader.reads) > reads_before, pilot=pilot, description="new read started")
        assert [read["workspace"] for read in loader.reads[reads_before:]] == [str(elsewhere)]

        loader.release.set()
        await wait_for(lambda: handler._shortcuts() is not None, pilot=pilot, description="new read landed")
        await _send_with(pilot, main, chat, "enter", "/zzqqq x")
        await wait_for(lambda: chat.text == "Review body\n\nx", pilot=pilot, description="expanded once read")
        await pilot.pause()
        assert published.events == []
        assert len(loader.reads) == reads_before + 1


@pytest.mark.parametrize("held", ["help", "skill"])
async def test_system_commands_and_skills_never_wait_for_the_shortcut_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loader: _GatedLoader, held: str
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    app, published = await _app_with_bus(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat = await _sendable(pilot, monkeypatch)
        # Whether /help ran is the question, not how fast the guide reads its pages.
        guides: list[object] = []
        monkeypatch.setattr(main._view_adapter, "open_guide", guides.append)
        handler = main._suggestions
        await wait_for(lambda: handler._shortcuts() is not None, pilot=pilot, description="startup read landed")
        loader.held = True
        main._set_workspace_cwd(str(elsewhere))
        assert handler._shortcuts() is None
        text = "/help" if held == "help" else "/zzskill"
        assert handler.resolve_typed_name(text).kind != "pending"

        await _send_with(pilot, main, chat, "enter", text)

        if held == "help":
            await wait_for(lambda: len(guides) == 1, pilot=pilot, description="/help ran: guide asked for")
            assert published.events == []
        else:
            await wait_for(
                lambda: published.described() == [("UserMessage", "/zzskill")],
                pilot=pilot,
                description="skill sent",
            )
        assert _STILL_READING not in _warnings(app)
        loader.release.set()


async def test_a_failed_read_counts_as_read_with_no_shortcuts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[dict[str, Any]] = []

    def broken(**folders: Any) -> list[PromptShortcut]:
        attempts.append(folders)
        raise OSError("shortcut folder unreadable")

    monkeypatch.setattr(suggestions_module, "load_prompt_shortcuts", broken)
    _global_shortcut("zzqqq", "Review body")
    app, published = await _app_with_bus(tmp_path)
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat = await _sendable(pilot, monkeypatch)
        handler = main._suggestions
        await wait_for(lambda: handler._shortcuts() is not None, pilot=pilot, description="failed read landed")
        assert attempts
        assert handler._shortcuts() == []
        assert handler.resolve_typed_name("/zzqqq x").kind != "pending"

        await _send_with(pilot, main, chat, "enter", "/zzqqq x")

        # Nothing waits: the text goes out as typed.
        await wait_for(
            lambda: published.described() == [("UserMessage", "/zzqqq x")], pilot=pilot, description="sent as typed"
        )
        assert _STILL_READING not in _warnings(app)


_STILL_READING_TAB = "Prompt shortcuts are still being read; press Tab again in a moment."


async def test_tab_on_a_name_still_being_read_waits_with_a_notice_then_expands(
    tmp_path: Path, loader: _GatedLoader
) -> None:
    """As Enter holds a ``/name`` that may be a shortcut not read yet, Tab keeps it and says so;
    once the read lands, Tab expands it."""
    _global_shortcut("zzglob", "Global body")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat = await _ready(pilot)
        handler = main._suggestions
        await wait_for(lambda: handler._shortcuts() is not None, pilot=pilot, description="first read landed")
        loader.held = True
        main._set_workspace_cwd(str(elsewhere))
        assert handler._shortcuts() is None

        await _typed_tab(pilot, main, chat, "/zzglob x")

        await wait_for(lambda: _STILL_READING_TAB in _warnings(app), pilot=pilot, description="still-reading notice")
        assert chat.text == "/zzglob x"
        assert chat.has_focus
        assert _STILL_READING not in _warnings(app)

        loader.release.set()
        await wait_for(lambda: handler._shortcuts() is not None, pilot=pilot, description="new read landed")
        await pilot.press("tab")
        await wait_for(lambda: chat.text == "Global body\n\nx", pilot=pilot, description="expanded once read")
