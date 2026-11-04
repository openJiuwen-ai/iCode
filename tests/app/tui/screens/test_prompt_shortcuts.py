# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for prompt shortcuts in a real app: the / list, expansion into the draft and name resolution."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.widgets import Checkbox, Label

from chrys.app.tui.app import ChrysApp
from chrys.app.tui.screens.agents.panels.skills import SkillDirCard
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.chrome.input_bar import ChatDraft, InputBar, _ChatTextArea
from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList
from chrys.app.tui.widgets.directory_path import DirectoryPathCard
from chrys.foundation.events.types import AgentRuntimeDetails, RuntimeSkillDetails
from chrys.foundation.platform import get_platform
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from textual.pilot import Pilot

_SECTION_COMMANDS = "System Commands"
_SECTION_SHORTCUTS = "Prompt Shortcuts"
_SECTION_SKILLS = "Loaded Skills"

_TRANSLATE = "---\ndescription: Translate a passage\nargument-hint: <tone> <text>\n---\n\nUse a $1 tone for:\n$2\n"
_TRANSLATE_HINTS = "Use a argument 1 tone for:\nargument 2"


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _global_shortcut(name: str, text: str) -> Path:
    return _write(get_platform().config_dir / "shortcuts" / f"{name}.md", text)


def _project_shortcut(name: str, text: str) -> Path:
    return _write(Path.cwd() / ".chrys" / "shortcuts" / f"{name}.md", text)


class _Sent:
    """Records what the main screen actually submits as a chat message."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.drafts: list[ChatDraft | None] = []

    def __call__(self, text: str, draft: ChatDraft | None = None) -> None:
        self.texts.append(text)
        self.drafts.append(draft)


async def _ready(pilot: Pilot, monkeypatch: pytest.MonkeyPatch) -> tuple[MainScreen, _ChatTextArea, _Sent]:
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
    sent = _Sent()
    monkeypatch.setattr(main, "_submit_user_text", sent)
    return main, main.query_one("#chat-input", _ChatTextArea), sent


def _list(main: MainScreen) -> SuggestionList:
    return main.query_one(SuggestionList)


def _rows(main: MainScreen) -> list[str]:
    return [content.plain for content in _list(main)._contents]


def _list_open(main: MainScreen) -> bool:
    suggestions = _list(main)
    return suggestions.is_visible and not suggestions.is_loading


def _group(main: MainScreen, section: str) -> list[str]:
    """The rows under *section*, up to the next section header."""
    rows = _rows(main)
    if section not in rows:
        return []
    group: list[str] = []
    for row in rows[rows.index(section) + 1 :]:
        if row in (_SECTION_COMMANDS, _SECTION_SHORTCUTS, _SECTION_SKILLS):
            break
        group.append(row)
    return group


def _has_shortcut_row(main: MainScreen, name: str) -> bool:
    return any(row.split()[0] == f"/{name}" for row in _group(main, _SECTION_SHORTCUTS) if row.strip())


async def _type(pilot: Pilot, text: str) -> None:
    await pilot.press(*["space" if char == " " else char for char in text])


async def _open_list(pilot: Pilot, main: MainScreen, filter_text: str = "") -> None:
    await pilot.press("/")
    await _type(pilot, filter_text)
    await wait_for(lambda: _list_open(main), pilot=pilot, description="/ list open")


async def _highlight_shortcut(pilot: Pilot, main: MainScreen, name: str) -> None:
    """Move the list highlight onto the shortcut row for *name*."""
    await wait_for(lambda: _has_shortcut_row(main, name), pilot=pilot, description=f"/{name} shortcut row listed")
    suggestions = _list(main)
    rows = _rows(main)
    start = rows.index(_SECTION_SHORTCUTS)
    target = next(i for i in range(start + 1, len(rows)) if rows[i].split()[0] == f"/{name}")
    for _ in range(len(rows)):
        if suggestions._highlighted == target:
            return
        await pilot.press("down")
    pytest.fail(f"could not highlight /{name}")


async def test_slash_list_groups_shortcuts_between_system_commands_and_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _global_shortcut("zzglobal", "---\ndescription: My own prompt\n---\nGlobal body")
    _project_shortcut("zzproject", "---\ndescription: Team prompt\n---\nProject body")
    _global_shortcut("git/commit-msg", "Write a commit message")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, _chat, _sent = await _ready(pilot, monkeypatch)
        main._state.runtime.details = AgentRuntimeDetails(
            skill_details=[RuntimeSkillDetails(name="zzskill", description="A loaded skill")]
        )

        await _open_list(pilot, main)
        await wait_for(
            lambda: _has_shortcut_row(main, "zzproject") and _has_shortcut_row(main, "zzglobal"),
            pilot=pilot,
            description="shortcut rows listed",
        )

        rows = _rows(main)
        assert rows.index(_SECTION_COMMANDS) < rows.index(_SECTION_SHORTCUTS) < rows.index(_SECTION_SKILLS)
        shortcuts = _group(main, _SECTION_SHORTCUTS)
        assert any(row.startswith("/zzglobal") and "My own prompt" in row for row in shortcuts)
        assert any(row.startswith("/zzproject") and "Team prompt" in row for row in shortcuts)
        assert any(row.startswith("/git/commit-msg") for row in shortcuts)
        assert not any("[project" in row or "[global]" in row for row in rows)


async def test_shortcut_rows_filter_like_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _global_shortcut("zzalpha", "Alpha")
    _global_shortcut("zzbeta", "Beta")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, _chat, _sent = await _ready(pilot, monkeypatch)
        await _open_list(pilot, main, "zzal")

        await wait_for(lambda: _has_shortcut_row(main, "zzalpha"), pilot=pilot, description="matching row listed")
        assert not _has_shortcut_row(main, "zzbeta")


async def test_burst_slash_input_filters_before_selecting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _global_shortcut("explain", "Explain $1")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, sent = await _ready(pilot, monkeypatch)
        await wait_for(
            lambda: any(command.name == "explain" for command in main._suggestions._prompt_shortcuts),
            pilot=pilot,
            description="shortcuts loaded before typing",
        )
        for char in "/ex":
            chat.post_message(events.Key(char, char))
        await wait_for(lambda: _list_open(main) and chat.text == "/ex", pilot=pilot, description="burst handled")

        assert [row.split()[0] for row in _group(main, _SECTION_COMMANDS)] == ["/exit"]
        assert _has_shortcut_row(main, "explain")
        await pilot.press("tab")
        await wait_for(lambda: chat.text == "/exit ", pilot=pilot, description="matching command selected")
        assert sent.texts == []


@pytest.mark.parametrize("key", ["tab", "enter"])
async def test_choosing_a_shortcut_row_expands_it_without_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    _global_shortcut("zztranslate", _TRANSLATE)
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, sent = await _ready(pilot, monkeypatch)
        await _open_list(pilot, main, "zztrans")
        await _highlight_shortcut(pilot, main, "zztranslate")

        await pilot.press(key)

        await wait_for(lambda: chat.text == _TRANSLATE_HINTS, pilot=pilot, description="shortcut expanded")
        assert chat.selected_text == "argument 1"
        assert chat.prompt_fields == ((6, 16), (27, 37))
        assert not _list(main).is_visible
        assert sent.texts == []


async def test_expanded_fields_take_typing_and_tab_jumps_and_wraps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _global_shortcut("zztranslate", _TRANSLATE)
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, sent = await _ready(pilot, monkeypatch)
        await _open_list(pilot, main, "zztrans")
        await _highlight_shortcut(pilot, main, "zztranslate")
        await pilot.press("tab")
        await wait_for(lambda: chat.text == _TRANSLATE_HINTS, pilot=pilot, description="shortcut expanded")

        await pilot.press("tab")
        await wait_for(lambda: chat.selection.end == (1, 10), pilot=pilot, description="second field selected")
        assert chat.selected_text == "argument 2"
        await pilot.press("tab")
        await wait_for(lambda: chat.selection.end == (0, 16), pilot=pilot, description="wrapped to first field")
        assert chat.selected_text == "argument 1"

        await _type(pilot, "dry")
        assert chat.text == "Use a dry tone for:\nargument 2"
        assert chat.prompt_fields == ((20, 30),)
        await pilot.press("tab")
        await wait_for(lambda: chat.selected_text == "argument 2", pilot=pilot, description="remaining field selected")
        assert sent.texts == []


async def test_enter_with_untouched_fields_sends_only_on_second_press(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _global_shortcut("zztranslate", _TRANSLATE)
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40), notifications=True) as pilot:
        main, chat, sent = await _ready(pilot, monkeypatch)
        await _open_list(pilot, main, "zztrans")
        await _highlight_shortcut(pilot, main, "zztranslate")
        await pilot.press("enter")
        await wait_for(lambda: chat.text == _TRANSLATE_HINTS, pilot=pilot, description="shortcut expanded")

        await pilot.press("enter")
        assert sent.texts == []
        assert chat.text == _TRANSLATE_HINTS

        await pilot.press("enter")
        await wait_for(lambda: sent.texts == [_TRANSLATE_HINTS], pilot=pilot, description="second Enter sent")


async def test_an_at_file_picked_after_the_fields_keeps_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Picking from the @ list rewrites only the @ token: the unfilled fields stay, so Enter still asks first."""
    Path("notes.txt").write_text("notes", encoding="utf-8")
    _global_shortcut("zztranslate", _TRANSLATE)
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40), notifications=True) as pilot:
        main, chat, sent = await _ready(pilot, monkeypatch)
        await _open_list(pilot, main, "zztrans")
        await _highlight_shortcut(pilot, main, "zztranslate")
        await pilot.press("enter")
        await wait_for(lambda: chat.text == _TRANSLATE_HINTS, pilot=pilot, description="shortcut expanded")
        fields = chat.prompt_fields
        assert len(fields) == 2

        chat.move_cursor(chat.document.end)
        await pilot.press("space", "@")
        await wait_for(lambda: _list_open(main), pilot=pilot, description="file list open")
        await pilot.press("enter")
        await wait_for(lambda: chat.text == f"{_TRANSLATE_HINTS} @notes.txt ", pilot=pilot, description="file picked")

        assert chat.prompt_fields == fields
        assert chat.cursor_location == chat.document.end
        await pilot.press("enter")
        assert sent.texts == []


@pytest.mark.parametrize(
    ("text", "cursor", "trigger", "replacement", "expected"),
    [
        # The token runs to the end of the text, wherever the cursor is: nothing typed is left behind.
        ("/hlp", 2, "/", "/help ", "/help "),
        ("@note", 3, "@", "@notes.txt ", "@notes.txt "),
        ("x @no", 0, "@", "@notes.txt ", "x @notes.txt "),
        # An @ token starts at the last @; the text before it stays.
        ("@a x @b", 2, "@", "@notes.txt ", "@a x @notes.txt "),
        # A slash token is the leading one, though a typed shortcut name may hold more slashes.
        ("/git/x", 6, "/", "/gitignore ", "/gitignore "),
        ("/theme da", 9, "/theme ", "/theme name ", "/theme name "),
        # Fullwidth triggers count.
        ("\uff20no", 3, "@", "@notes.txt ", "@notes.txt "),
        ("\uff0fhlp", 4, "/", "/help ", "/help "),
        # No token, no change.
        ("plain text", 3, "@", "@notes.txt ", "plain text"),
        ("x /help", 7, "/", "/help ", "x /help"),
    ],
)
async def test_a_completion_replaces_the_token_the_list_filtered_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    cursor: int,
    trigger: str,
    replacement: str,
    expected: str,
) -> None:
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, _sent = await _ready(pilot, monkeypatch)
        bar = main.query_one(InputBar)

        chat.load_text(text)
        chat.move_cursor((0, cursor))
        bar.replace_trigger_text(trigger, replacement)

        assert chat.text == expected
        if expected != text:
            assert chat.cursor_location == chat.document.end


async def test_a_slash_pick_with_the_cursor_inside_the_token_replaces_all_of_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixing a typo inside ``/hel`` and picking: the list filtered on the whole token, so the pick replaces it."""
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, _sent = await _ready(pilot, monkeypatch)
        await _open_list(pilot, main, "hel")
        suggestions = _list(main)
        target = _rows(main).index(next(row for row in _rows(main) if row.split()[:1] == ["/help"]))
        for _ in range(len(_rows(main))):
            if suggestions._highlighted == target:
                break
            await pilot.press("down")
        assert suggestions._highlighted == target

        chat.move_cursor((0, 2))
        await pilot.press("tab")

        await wait_for(lambda: chat.text == "/help ", pilot=pilot, description="/help picked")
        assert chat.cursor_location == chat.document.end


@pytest.mark.parametrize(("typed_before", "cursor"), [("", (0, 3)), ("x ", (0, 0))])
async def test_an_at_pick_replaces_the_whole_token_wherever_the_cursor_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, typed_before: str, cursor: tuple[int, int]
) -> None:
    Path("notes.txt").write_text("notes", encoding="utf-8")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, _sent = await _ready(pilot, monkeypatch)
        await _type(pilot, typed_before)
        await pilot.press("@", "n", "o", "t", "e")
        await wait_for(
            lambda: _list_open(main) and any("notes.txt" in row for row in _rows(main)),
            pilot=pilot,
            description="notes.txt listed",
        )

        chat.move_cursor(cursor)
        await pilot.press("enter")

        await wait_for(lambda: chat.text == f"{typed_before}@notes.txt ", pilot=pilot, description="file picked whole")


@pytest.mark.parametrize("draft", ["/etc/hosts is broken", "/help me"], ids=["path", "command"])
async def test_tab_on_a_slash_draft_that_names_no_shortcut_moves_focus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, draft: str
) -> None:
    """Only a draft a shortcut expands keeps Tab: a path, a command or any other ``/word …`` text
    moves focus as Tab always does."""
    _global_shortcut("zztranslate", _TRANSLATE)
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, sent = await _ready(pilot, monkeypatch)
        # Read: no name is still waiting to turn out a shortcut.
        await wait_for(lambda: main._suggestions._shortcuts() is not None, pilot=pilot, description="shortcuts read")
        chat.insert(draft)
        await wait_for(lambda: chat.text == draft, pilot=pilot, description="draft typed")
        assert not _list(main).is_visible

        await pilot.press("tab")

        await wait_for(lambda: not chat.has_focus, pilot=pilot, description="focus moved on")
        assert main.focused is main.query_one("#send-btn")
        assert chat.text == draft
        assert sent.texts == []


@pytest.mark.parametrize("button", ["#editor-btn", "#send-btn"])
async def test_tab_from_a_button_in_the_bar_moves_focus_over_a_shortcut_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, button: str
) -> None:
    """Tab belongs to the shortcut only in the chat input: from the bar's buttons it moves focus, the draft unexpanded."""
    _global_shortcut("zztranslate", _TRANSLATE)
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, sent = await _ready(pilot, monkeypatch)
        await wait_for(lambda: main._suggestions._shortcuts() is not None, pilot=pilot, description="shortcuts read")
        chat.insert("/zztranslate formal hello")
        await wait_for(lambda: chat.text == "/zztranslate formal hello", pilot=pilot, description="draft typed")
        start = main.query_one(button)
        # Send takes focus once the bar has handled the draft's change.
        await wait_for(lambda: start.focusable, pilot=pilot, description="button can take focus")
        start.focus()
        await wait_for(lambda: main.focused is start, pilot=pilot, description="button focused")

        await pilot.press("tab")

        await wait_for(lambda: main.focused is not start, pilot=pilot, description="focus moved on")
        assert chat.text == "/zztranslate formal hello"
        assert sent.texts == []


@pytest.mark.parametrize("key", ["tab", "enter"])
async def test_typed_name_with_arguments_expands_without_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    _global_shortcut("zztranslate", _TRANSLATE)
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, sent = await _ready(pilot, monkeypatch)
        await _type(pilot, '/zztranslate formal "the meeting moved"')
        await wait_for(lambda: not _list(main).is_visible, pilot=pilot, description="list closed after the space")

        await pilot.press(key)

        await wait_for(
            lambda: chat.text == "Use a formal tone for:\nthe meeting moved",
            pilot=pilot,
            description="typed shortcut expanded",
        )
        assert chat.prompt_fields == ()
        assert sent.texts == []
        # The expanding Tab is the shortcut's: focus stays to edit the draft.
        assert chat.has_focus

        await pilot.press("enter")
        await wait_for(
            lambda: sent.texts == ["Use a formal tone for:\nthe meeting moved"],
            pilot=pilot,
            description="expanded text sent on the next Enter",
        )


async def test_escape_codes_in_a_shortcut_never_reach_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file's text is shown in the input bar: control codes become visible marks, tabs and newlines stay."""
    _global_shortcut("zzescape", "Review this change: \x1b[8mhidden\x1b[0m\tthen\x07 $1\nThe next line of the prompt.")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, _sent = await _ready(pilot, monkeypatch)
        await _type(pilot, "/zzescape arg")
        await wait_for(lambda: not _list(main).is_visible, pilot=pilot, description="list closed after the space")

        await pilot.press("tab")

        await wait_for(
            lambda: (
                chat.text
                == "Review this change: \ufffd[8mhidden\ufffd[0m\tthen\ufffd arg\nThe next line of the prompt."
            ),
            pilot=pilot,
            description="shortcut expanded without its control codes",
        )


async def test_typed_name_with_too_few_arguments_leaves_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _global_shortcut("zztranslate", _TRANSLATE)
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        _main, chat, sent = await _ready(pilot, monkeypatch)
        await _type(pilot, "/zztranslate formal")
        await pilot.press("enter")

        await wait_for(lambda: chat.text == "Use a formal tone for:\nargument 2", pilot=pilot, description="expanded")
        assert chat.prompt_fields == ((23, 33),)
        assert chat.selected_text == "argument 2"
        assert sent.texts == []


async def test_typed_unknown_name_tab_does_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _global_shortcut("zztranslate", _TRANSLATE)
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, sent = await _ready(pilot, monkeypatch)
        await _type(pilot, "/zznotashortcut some words")
        await wait_for(lambda: not _list(main).is_visible, pilot=pilot, description="list closed")

        await pilot.press("tab")

        assert chat.text == "/zznotashortcut some words"
        assert chat.prompt_fields == ()
        assert sent.texts == []


async def test_system_command_wins_over_same_named_shortcut_when_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _global_shortcut("help", "SHORTCUT BODY for help")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, sent = await _ready(pilot, monkeypatch)
        dispatched: list[str] = []
        original = main._suggestions.dispatch_slash_command

        def record(text: str) -> bool:
            dispatched.append(text)
            return original(text)

        monkeypatch.setattr(main._suggestions, "dispatch_slash_command", record)

        await _open_list(pilot, main, "help")
        await wait_for(lambda: _has_shortcut_row(main, "help"), pilot=pilot, description="shortcut row still listed")
        assert any(row.split()[0] == "/help" for row in _group(main, _SECTION_COMMANDS))
        await pilot.press("escape")
        await wait_for(lambda: not _list(main).is_visible, pilot=pilot, description="list dismissed")

        await _type(pilot, " topic")
        await pilot.press("enter")

        await wait_for(lambda: dispatched == ["/help topic"], pilot=pilot, description="system command dispatched")
        assert "SHORTCUT BODY" not in chat.text
        assert sent.texts == []


async def test_shortcut_row_sharing_a_system_command_name_expands_its_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _global_shortcut("help", "SHORTCUT BODY for help")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, sent = await _ready(pilot, monkeypatch)
        await _open_list(pilot, main, "help")
        await _highlight_shortcut(pilot, main, "help")
        assert not _list(main)._disabled[_list(main)._highlighted or 0]

        await pilot.press("tab")

        await wait_for(lambda: chat.text == "SHORTCUT BODY for help", pilot=pilot, description="shortcut row expanded")
        assert sent.texts == []


async def test_loaded_skill_wins_over_same_named_shortcut_when_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _global_shortcut("zzdup", "SHORTCUT BODY for zzdup")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, sent = await _ready(pilot, monkeypatch)
        main._state.runtime.details = AgentRuntimeDetails(
            skill_details=[RuntimeSkillDetails(name="zzdup", description="Same-named skill")]
        )
        await _open_list(pilot, main, "zzdup")
        await wait_for(lambda: _has_shortcut_row(main, "zzdup"), pilot=pilot, description="shortcut row listed")
        assert any(row.split()[0] == "/zzdup" for row in _group(main, _SECTION_SKILLS))
        await pilot.press("escape")
        await wait_for(lambda: not _list(main).is_visible, pilot=pilot, description="list dismissed")

        await _type(pilot, " some args")
        await pilot.press("enter")

        await wait_for(lambda: sent.texts == ["/zzdup some args"], pilot=pilot, description="skill reference sent")
        assert "SHORTCUT BODY" not in chat.text


async def test_shortcut_row_sharing_a_skill_name_expands_its_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _global_shortcut("zzdup", "SHORTCUT BODY for zzdup")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, sent = await _ready(pilot, monkeypatch)
        main._state.runtime.details = AgentRuntimeDetails(
            skill_details=[RuntimeSkillDetails(name="zzdup", description="Same-named skill")]
        )
        await _open_list(pilot, main, "zzdup")
        await _highlight_shortcut(pilot, main, "zzdup")

        await pilot.press("enter")

        await wait_for(lambda: chat.text == "SHORTCUT BODY for zzdup", pilot=pilot, description="shortcut expanded")
        assert sent.texts == []


async def test_same_named_shortcuts_are_listed_once_and_global_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _global_shortcut("zzsame", "Global body")
    _project_shortcut("zzsame", "Project body")
    _project_shortcut("ZZSAME2", "Project upper")
    _global_shortcut("zzsame2", "Global lower")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, sent = await _ready(pilot, monkeypatch)
        await _open_list(pilot, main, "zzsame")
        await wait_for(lambda: _has_shortcut_row(main, "zzsame"), pilot=pilot, description="shortcut listed")

        shortcuts = [row for row in _group(main, _SECTION_SHORTCUTS) if row.strip()]
        assert [row.split()[0].casefold() for row in shortcuts] == ["/zzsame", "/zzsame2"]
        assert not any("[" in row for row in shortcuts)

        await _highlight_shortcut(pilot, main, "zzsame")
        await pilot.press("tab")
        await wait_for(lambda: chat.text == "Global body", pilot=pilot, description="global file expanded")
        assert sent.texts == []

        chat.clear()
        await _type(pilot, "/zzsame2 x")
        await pilot.press("tab")
        await wait_for(lambda: chat.text == "Global lower\n\nx", pilot=pilot, description="typed name uses global")


async def test_typed_shortcut_resolves_to_global_before_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _global_shortcut("zzsame", "Global $1")
    _project_shortcut("zzsame", "Project $1")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        _main, chat, sent = await _ready(pilot, monkeypatch)
        await _type(pilot, "/zzsame value")
        await pilot.press("enter")

        await wait_for(lambda: chat.text == "Global value", pilot=pilot, description="global shortcut expanded")
        assert sent.texts == []


async def test_project_shortcuts_load_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _project_shortcut("zzproject", "Project body")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        _main, chat, _sent = await _ready(pilot, monkeypatch)
        await _type(pilot, "/zzproject x")
        await pilot.press("tab")

        await wait_for(lambda: chat.text == "Project body\n\nx", pilot=pilot, description="project shortcut expanded")


async def test_path_like_text_closes_the_list_unless_a_shortcut_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _global_shortcut("git/commit-msg", "Write a commit message")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, _sent = await _ready(pilot, monkeypatch)
        await _open_list(pilot, main, "git/")
        await wait_for(
            lambda: _has_shortcut_row(main, "git/commit-msg"), pilot=pilot, description="nested shortcut listed"
        )
        assert _list(main).is_visible

        chat.clear()
        await wait_for(lambda: not _list(main).is_visible, pilot=pilot, description="list closed on clear")
        await _type(pilot, "/Users/me")
        await wait_for(lambda: not _list(main).is_visible, pilot=pilot, description="path closes the list")
        assert chat.text == "/Users/me"


async def test_file_edits_show_the_next_time_the_list_opens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _global_shortcut("zzedit", "---\ndescription: Before edit\n---\nBody")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, _sent = await _ready(pilot, monkeypatch)
        await _open_list(pilot, main, "zz")
        await wait_for(
            lambda: any("Before edit" in row for row in _group(main, _SECTION_SHORTCUTS)),
            pilot=pilot,
            description="first description shown",
        )
        await pilot.press("escape")
        chat.clear()
        await wait_for(lambda: not _list(main).is_visible and chat.text == "", pilot=pilot, description="reset")

        target.write_text("---\ndescription: After edit\n---\nBody", encoding="utf-8")
        _global_shortcut("zzadded", "Added")
        await _open_list(pilot, main, "zz")

        await wait_for(
            lambda: (
                any("After edit" in row for row in _group(main, _SECTION_SHORTCUTS))
                and _has_shortcut_row(main, "zzadded")
            ),
            pilot=pilot,
            description="edited and added shortcuts shown",
        )


# -- the shared folder card, as the skills panel shows it -------------------------


class _CardHost(App[None]):
    def __init__(self, cards: list[DirectoryPathCard]) -> None:
        super().__init__()
        self._cards = cards

    def compose(self) -> ComposeResult:
        yield from self._cards


def _preview(card: DirectoryPathCard) -> str:
    label = card.query_one(".sk-preview", Label)
    return str(label.content) if label.display else ""


@pytest.mark.parametrize(
    ("path", "workspace", "expected"),
    [
        ("{existing}", "{ws}", "Current: {existing_resolved}"),
        ("{missing}", "{ws}", "Path does not exist"),
        ("prompts", "{ws}", "Current: {ws_prompts}"),
        ("nowhere", "{ws}", "Missing in current workspace"),
        ("prompts", "", "Resolved at session runtime"),
    ],
    ids=["absolute", "absolute-missing", "relative", "relative-missing", "relative-without-workspace"],
)
async def test_a_folder_card_previews_a_path(tmp_path: Path, path: str, workspace: str, expected: str) -> None:
    ws = tmp_path / "ws"
    (ws / "prompts").mkdir(parents=True)
    existing = tmp_path / "existing"
    existing.mkdir()
    values = {
        "ws": str(ws),
        "existing": str(existing),
        "existing_resolved": str(existing.resolve()),
        "missing": str(tmp_path / "missing"),
        "ws_prompts": str((ws / "prompts").resolve()),
    }
    raw = path.format(**values)
    cwd = workspace.format(**values) or None
    card = SkillDirCard(raw, index=0, workspace_cwd=cwd)
    async with _CardHost([card]).run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: bool(_preview(card)), pilot=pilot, description="preview")

        assert expected.format(**values) in _preview(card)


@pytest.mark.parametrize("relative", [False, True], ids=["relative-path-unchecked", "absolute-path-checked"])
async def test_a_folder_card_explains_a_wrong_relative_choice(tmp_path: Path, relative: bool) -> None:
    raw = str(tmp_path) if relative else "prompts"
    card = SkillDirCard(raw, index=0, workspace_cwd=str(tmp_path))
    async with _CardHost([card]).run_test(size=(120, 40)) as pilot:
        card.query_one(".sk-rel-cb", Checkbox).value = relative
        expected = "Turn off Workspace relative" if relative else "Enable Workspace relative"
        await wait_for(
            lambda: expected in _preview(card), pilot=pilot, description="the card explains the wrong choice"
        )


@pytest.mark.parametrize("raw", [".agents/skills", "prompts"])
async def test_the_skill_card_notes_the_working_folders_own_skill_folder(tmp_path: Path, raw: str) -> None:
    ws = tmp_path / "ws"
    (ws / ".agents" / "skills").mkdir(parents=True)
    (ws / "prompts").mkdir()
    card = SkillDirCard(raw, index=0, workspace_cwd=str(ws))
    async with _CardHost([card]).run_test(size=(120, 40)) as pilot:
        await wait_for(lambda: bool(_preview(card)), pilot=pilot, description="preview")

        assert ("Also auto-loaded" in _preview(card)) is (raw == ".agents/skills")


async def test_typed_escaped_quotes_reach_the_placeholder_as_quotes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opencode's documented example, typed after the shortcut name: ``\\"`` inside a quoted argument is a quote."""
    _global_shortcut("zzcreate", "Create $1 in $2 with: $3")
    app = make_chrys_app(tmp_path / "state")
    async with app.run_test(size=(120, 40)) as pilot:
        main, chat, sent = await _ready(pilot, monkeypatch)
        typed = r'/zzcreate config.json src "{ \"key\": \"value\" }"'
        await _type(pilot, typed)
        await wait_for(lambda: chat.text == typed, pilot=pilot, description="typed as written")
        await wait_for(lambda: not _list(main).is_visible, pilot=pilot, description="list closed after the space")

        await pilot.press("tab")

        await wait_for(
            lambda: chat.text == 'Create config.json in src with: { "key": "value" }',
            pilot=pilot,
            description="third argument expanded with literal quotes",
        )
        assert sent.texts == []
