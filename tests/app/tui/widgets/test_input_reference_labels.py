# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Image, quote and pasted-text labels in the chat input: one unit while editing, expanded when sent."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult
from textual.events import Paste

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.support import prompt_history
from chrys.app.tui.widgets.chrome.input_bar import InputBar, _ChatTextArea
from chrys.app.tui.widgets.chrome.prompt_draft import ShortcutIntent
from chrys.foundation.config.settings import Settings
from chrys.foundation.text.draft_references import iter_reference_labels
from chrys.foundation.text.mentions import format_file_mention
from tests.support.tui_helpers import click_when_settled, png_bytes
from tests.support.waiting import wait_for


class _LabelApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.opened: list[InputBar.ReferenceOpened] = []
        self.submitted: list[InputBar.UserSubmitted] = []

    def compose(self) -> ComposeResult:
        yield InputBar()

    def on_input_bar_reference_opened(self, event: InputBar.ReferenceOpened) -> None:
        self.opened.append(event)

    def on_input_bar_user_submitted(self, event: InputBar.UserSubmitted) -> None:
        self.submitted.append(event)


def _parts(app: _LabelApp) -> tuple[InputBar, _ChatTextArea]:
    input_bar = app.query_one(InputBar)
    return input_bar, input_bar.query_one("#chat-input", _ChatTextArea)


def _live(text_area: _ChatTextArea) -> list[str]:
    return [
        text_area.text[label.start : label.end] for label in iter_reference_labels(text_area.text, text_area.references)
    ]


@pytest.mark.asyncio
async def test_a_new_quote_lands_beside_the_label_the_cursor_was_put_inside() -> None:
    app = _LabelApp()
    async with app.run_test() as pilot:
        _input_bar, text_area = _parts(app)
        text_area.focus()
        assert text_area.insert_reference("quote", "first")
        text_area.move_cursor((0, 4))
        await pilot.pause()

        assert text_area.insert_reference("quote", "second")

        assert text_area.text == "[Quote #2] [Quote #1] "
        assert _live(text_area) == ["[Quote #2]", "[Quote #1]"]


@pytest.mark.asyncio
async def test_a_programmatic_insert_inside_a_label_goes_after_it() -> None:
    app = _LabelApp()
    async with app.run_test() as pilot:
        _input_bar, text_area = _parts(app)
        assert text_area.insert_reference("quote", "q")
        await pilot.pause()

        text_area.insert("x", (0, 4))

        assert text_area.text == "[Quote #1]x "
        assert _live(text_area) == ["[Quote #1]"]


@pytest.mark.asyncio
async def test_arrow_keys_step_over_a_label_as_one_unit() -> None:
    app = _LabelApp()
    async with app.run_test() as pilot:
        input_bar, text_area = _parts(app)
        input_bar.value = "a "
        text_area.focus()
        text_area.move_cursor((0, 2))
        assert text_area.insert_reference("quote", "q")
        text_area.move_cursor((0, 13))
        await pilot.pause()
        assert text_area.text == "a [Quote #1] "

        await pilot.press("left")
        assert text_area.cursor_location == (0, 12)
        await pilot.press("left")
        assert text_area.cursor_location == (0, 2)
        await pilot.press("right")
        assert text_area.cursor_location == (0, 12)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cursor", "key", "left"),
    [((0, 13), "backspace", "hi  tail"), ((0, 3), "delete", "hi  tail"), ((0, 13), "ctrl+w", "hi  tail")],
    ids=["backspace-after", "delete-before", "delete-word-left"],
)
async def test_deleting_into_a_label_removes_all_of_it(cursor: tuple[int, int], key: str, left: str) -> None:
    app = _LabelApp()
    async with app.run_test() as pilot:
        input_bar, text_area = _parts(app)
        input_bar.value = "hi  tail"
        text_area.focus()
        text_area.move_cursor((0, 3))
        assert text_area.insert_reference("quote", "q")
        assert text_area.text == "hi [Quote #1] tail"
        text_area.move_cursor(cursor)
        await pilot.pause()

        await pilot.press(key)

        assert text_area.text == left
        assert _live(text_area) == []


@pytest.mark.asyncio
async def test_extending_a_selection_into_a_label_takes_the_whole_label() -> None:
    app = _LabelApp()
    async with app.run_test() as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        assert text_area.insert_reference("quote", "q")
        text_area.move_cursor((0, 10))
        await pilot.pause()

        await pilot.press("shift+left")

        assert text_area.selected_text == "[Quote #1]"
        await pilot.press("x")
        assert input_bar.value == "x "


@pytest.mark.asyncio
async def test_a_typed_label_nobody_registered_is_plain_text() -> None:
    app = _LabelApp()
    async with app.run_test() as pilot:
        input_bar, text_area = _parts(app)
        input_bar.value = "[Quote #1]"
        text_area.focus()
        text_area.move_cursor((0, 10))
        await pilot.pause()

        await pilot.press("left")
        assert text_area.cursor_location == (0, 9)
        await pilot.press("backspace")
        assert input_bar.value == "[Quote #]"


@pytest.mark.asyncio
async def test_a_sent_draft_numbers_the_next_messages_labels_from_one_again() -> None:
    app = _LabelApp()
    async with app.run_test() as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        assert text_area.insert_reference("quote", "a")
        assert text_area.insert_reference("quote", "b")
        await pilot.press("enter")
        await wait_for(lambda: bool(app.submitted), pilot=pilot, description="the draft was sent")
        assert input_bar.value == ""

        assert text_area.insert_reference("quote", "c")

        assert input_bar.value == "[Quote #1] "
        entry = text_area.references.get("quote", 1)
        assert entry is not None
        assert entry.content == "c"


@pytest.mark.asyncio
async def test_undo_after_emptying_the_draft_brings_its_label_back_live() -> None:
    app = _LabelApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        await text_area._on_paste(Paste(_FOUR_LINES))

        await pilot.press("backspace")
        assert input_bar.value == ""
        text_area.action_undo()

        assert _live(text_area) == ["[Pasted text #1 +3 lines]"]
        assert input_bar.draft().sent_text() == _FOUR_LINES


@pytest.mark.asyncio
async def test_undo_and_redo_of_the_only_paste_keep_its_label_live() -> None:
    app = _LabelApp()
    async with app.run_test(size=(100, 30)):
        input_bar, text_area = _parts(app)
        text_area.focus()
        await text_area._on_paste(Paste(_FOUR_LINES))

        text_area.action_undo()
        assert input_bar.value == ""
        text_area.action_redo()

        assert input_bar.draft().sent_text() == _FOUR_LINES


@pytest.mark.asyncio
async def test_cutting_a_whole_draft_and_pasting_it_back_keeps_its_label_live() -> None:
    app = _LabelApp()
    async with app.run_test(size=(100, 30)):
        input_bar, text_area = _parts(app)
        text_area.focus()
        await text_area._on_paste(Paste(_FOUR_LINES))

        text_area.select_all()
        text_area.action_cut()
        assert input_bar.value == ""
        await text_area._on_paste(Paste(app.clipboard))

        assert input_bar.draft().sent_text() == _FOUR_LINES


@pytest.mark.asyncio
async def test_history_keeps_the_text_that_was_sent_and_no_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Opt back in to the history file, kept under this test's own folder.
    monkeypatch.setattr(prompt_history, "get_platform", lambda: SimpleNamespace(config_dir=tmp_path))
    monkeypatch.delenv("CHRYS_HISTORY_DISABLE", raising=False)
    app = _LabelApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        text_area.insert("note ")
        await text_area._on_paste(Paste("a secret\nnever\nsent\nat all"))
        await pilot.press("backspace")
        assert input_bar.value == "note "
        assert text_area.insert_reference("quote", "kept")
        input_bar.submit_input()
        await wait_for(lambda: bool(app.submitted), pilot=pilot, description="draft submitted")
        input_bar.add_to_history(app.submitted[0].text)
        await input_bar.load_prompt_history(max_entries=10)

    assert prompt_history.load_history() == ["note “kept”"]
    assert "secret" not in (tmp_path / "prompt_history.jsonl").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_clicking_a_label_opens_what_it_stands_for(tmp_path: Path) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(png_bytes((10, 20, 30)))
    app = _LabelApp()
    async with app.run_test() as pilot:
        input_bar, text_area = _parts(app)
        input_bar.set_paste_cwd(str(tmp_path))
        text_area.focus()
        await text_area._on_paste(Paste(str(image)))
        await pilot.pause()
        assert input_bar.value == "[Image #1] "
        content = text_area.content_region
        offset = (content.x - text_area.region.x + 4, content.y - text_area.region.y)

        await click_when_settled(pilot, text_area, offset=offset)
        await wait_for(lambda: bool(app.opened), pilot=pilot, description="label click reported")

        opened = app.opened[0].reference
        assert (opened.kind, opened.number) == ("image", 1)
        assert Path(opened.content).resolve() == image.resolve()
        assert _live(text_area) == ["[Image #1]"]


@pytest.mark.asyncio
async def test_a_send_expands_labels_and_keeps_the_draft_for_hand_back(tmp_path: Path) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(png_bytes((10, 20, 30)))
    app = _LabelApp()
    async with app.run_test() as pilot:
        input_bar, text_area = _parts(app)
        input_bar.set_paste_cwd(str(tmp_path))
        input_bar.value = "why "
        text_area.focus()
        text_area.move_cursor((0, 4))
        await text_area._on_paste(Paste(str(image)))
        assert text_area.insert_reference("quote", "it failed")
        await pilot.pause()

        input_bar.submit_input()
        await wait_for(lambda: bool(app.submitted), pilot=pilot, description="draft submitted")

        sent = app.submitted[0]
        entry = sent.draft.references.get("image", 1)
        assert entry is not None
        assert sent.text == f"why {format_file_mention(entry.content)} “it failed”"
        assert sent.draft.text == "why [Image #1] [Quote #1] "


@pytest.mark.asyncio
async def test_many_pasted_image_paths_stay_image_labels_and_never_fold(tmp_path: Path) -> None:
    images = []
    for index in range(70):
        image = tmp_path / f"shot{index}.png"
        image.write_bytes(png_bytes((10, 20, 30)))
        images.append(image)
    app = _LabelApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        input_bar.set_paste_cwd(str(tmp_path))
        text_area.focus()
        await text_area._on_paste(Paste("\n".join(str(image) for image in images)))

        draft = input_bar.draft()
        assert [entry.kind for entry in draft.references.entries] == ["image"] * 70
        assert len(_live(text_area)) == 70
        await pilot.press("enter")
        await wait_for(lambda: bool(app.submitted), pilot=pilot, description="draft submitted")
        assert app.submitted[0].text.split() == [format_file_mention(str(image)) for image in images]


@pytest.mark.asyncio
async def test_history_brings_a_sent_message_back_as_the_text_it_sent() -> None:
    app = _LabelApp()
    async with app.run_test() as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        assert text_area.insert_reference("quote", "said")
        input_bar.submit_input()
        await wait_for(lambda: bool(app.submitted), pilot=pilot, description="draft submitted")
        input_bar.add_to_history(app.submitted[0].text)
        await pilot.pause()

        await pilot.press("up")

        assert input_bar.value == "“said”"
        assert not text_area.references


_FOUR_LINES = "def f():\n    return 1\n\nprint(f())"


@pytest.mark.asyncio
async def test_a_long_paste_folds_into_a_label_that_sends_the_text_where_it_stood() -> None:
    app = _LabelApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        input_bar.value = "ab"
        text_area.focus()
        text_area.move_cursor((0, 1))
        await pilot.pause()

        await text_area._on_paste(Paste(_FOUR_LINES))
        await text_area._on_paste(Paste("x" * 801))

        assert input_bar.value == "a[Pasted text #1 +3 lines][Pasted text #2]b"
        assert _live(text_area) == ["[Pasted text #1 +3 lines]", "[Pasted text #2]"]
        assert input_bar.draft().sent_text() == f"a{_FOUR_LINES}{'x' * 801}b"


@pytest.mark.asyncio
async def test_a_short_paste_stays_text() -> None:
    app = _LabelApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        await pilot.pause()

        await text_area._on_paste(Paste("one\ntwo\nthree"))

        assert input_bar.value == "one\ntwo\nthree"
        assert not text_area.references


@pytest.mark.asyncio
async def test_a_pasted_text_label_is_one_unit_and_opens_its_text() -> None:
    app = _LabelApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        await text_area._on_paste(Paste(_FOUR_LINES))
        await pilot.pause()
        content = text_area.content_region
        offset = (content.x - text_area.region.x + 3, content.y - text_area.region.y)

        await click_when_settled(pilot, text_area, offset=offset)
        await wait_for(lambda: bool(app.opened), pilot=pilot, description="label click reported")
        assert app.opened[0].reference.content == _FOUR_LINES

        text_area.move_cursor((0, len(input_bar.value)))
        await pilot.press("backspace")
        assert input_bar.value == ""


@pytest.mark.asyncio
async def test_a_draft_handed_back_whole_keeps_its_labels_and_sent_text_comes_back_as_text() -> None:
    app = _LabelApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        await text_area._on_paste(Paste(_FOUR_LINES))
        assert text_area.insert_reference("quote", "said")
        await pilot.pause()
        input_bar.submit_input()
        await wait_for(lambda: bool(app.submitted), pilot=pilot, description="draft submitted")
        submitted = app.submitted[0]

        # A send that is refused hands its draft back whole.
        assert input_bar.restore_draft(submitted.draft)
        assert input_bar.value == "[Pasted text #1 +3 lines] [Quote #1] "
        assert _live(text_area) == ["[Pasted text #1 +3 lines]", "[Quote #1]"]
        assert input_bar.draft().sent_text() == submitted.text

        # Text that was sent (a rollback, a returned injection) comes back as that text.
        input_bar.replace_draft("")
        assert input_bar.restore_draft(submitted.text)
        assert input_bar.value == f"{_FOUR_LINES} “said”"
        assert not text_area.references


_LABEL_SHAPED = re.compile(r"\[(?:Image|Quote|Pasted text) #[0-9]+(?: \+[0-9]+ lines)?\]")


def _dead(text_area: _ChatTextArea) -> list[str]:
    """Label-shaped text in the composer that stands for nothing."""
    live = set(_live(text_area))
    return [match.group(0) for match in _LABEL_SHAPED.finditer(text_area.text) if match.group(0) not in live]


@pytest.mark.asyncio
async def test_undo_never_brings_back_a_label_a_whole_draft_replacement_ended() -> None:
    app = _LabelApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        input_bar.add_to_history("recalled prompt")
        text_area.focus()
        await text_area._on_paste(Paste(_FOUR_LINES))
        await pilot.pause()

        await pilot.press("up")
        assert input_bar.value == "recalled prompt"
        for _ in range(3):
            text_area.action_undo()
            await pilot.pause()

        assert _dead(text_area) == []
        # A later paste numbered #1 again cannot take over the old label either.
        await text_area._on_paste(Paste("w\nx\ny\nz"))
        text_area.action_undo()
        text_area.action_undo()
        assert _dead(text_area) == []


@pytest.mark.asyncio
async def test_label_text_inside_a_long_paste_is_sent_as_text() -> None:
    # One level, as Claude Code does: a copied draft pasted back long folds, its label text plain.
    app = _LabelApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        assert text_area.insert_reference("quote", "said")
        await pilot.pause()
        copied = text_area.text

        text_area.move_cursor((0, len(copied)))
        await text_area._on_paste(Paste(f"{copied}\n.\n.\n."))

        assert input_bar.draft().sent_text() == f"“said” {copied}\n.\n.\n.".strip()


@pytest.mark.parametrize(
    ("typed", "shortcut", "sent"),
    [
        ("check !`echo ", True, False),
        ("check !`echo ", False, True),
        ("check !`echo x` and ", True, True),
    ],
    ids=["inside-a-shortcut-command", "not-a-shortcut-draft", "outside-the-command"],
)
@pytest.mark.asyncio
async def test_a_quote_inside_a_command_a_shortcut_runs_is_not_sent(typed: str, shortcut: bool, sent: bool) -> None:
    app = _LabelApp()
    async with app.run_test() as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        text_area.insert(typed)
        assert text_area.insert_reference("quote", "$(id)")
        text_area.insert("`" if typed.endswith("echo ") else "")
        if shortcut:
            text_area.set_draft_owner(ShortcutIntent("deploy", "", ""))
        await pilot.pause()
        draft = input_bar.draft()

        input_bar.submit_input()
        await pilot.pause()

        if sent:
            assert [event.text for event in app.submitted] == [draft.sent_text()]
        else:
            assert app.submitted == []
            assert input_bar.draft() == draft
            assert app._notifications


@pytest.mark.asyncio
async def test_a_quote_inside_a_command_a_paste_opens_is_not_sent() -> None:
    app = _LabelApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        text_area.focus()
        await text_area._on_paste(Paste("one\ntwo\nthree\nrun !`echo "))
        assert text_area.insert_reference("quote", "$(id)")
        text_area.insert("`")
        text_area.set_draft_owner(ShortcutIntent("deploy", "", ""))
        await pilot.pause()

        input_bar.submit_input()
        await pilot.pause()

        assert app.submitted == []
        assert app._notifications


@pytest.mark.asyncio
async def test_an_insert_the_draft_refuses_keeps_the_undo_steps() -> None:
    class _Refusing(_ChatTextArea):
        refuse = False

        def _replace_via_keyboard(self, insert, start, end):  # type: ignore[no-untyped-def]
            return None if self.refuse else super()._replace_via_keyboard(insert, start, end)

    class _App(App):
        def compose(self) -> ComposeResult:
            yield _Refusing()

    app = _App()
    async with app.run_test() as pilot:
        text_area = app.query_one(_Refusing)
        text_area.focus()
        await pilot.press("a", "b")
        assert text_area.insert_reference("quote", "first")
        text_area.refuse = True

        assert not text_area.insert_reference("quote", "second")

        assert text_area.references.get("quote", 2) is None
        text_area.refuse = False
        text_area.action_undo()
        assert text_area.text == "ab"


@pytest.mark.asyncio
async def test_labels_read_in_the_composers_language() -> None:
    class _ChineseApp(App):
        def __init__(self) -> None:
            self.locale_controller = LocaleController(Settings(locale="zh-Hans"))
            super().__init__()

        def compose(self) -> ComposeResult:
            yield InputBar()

    app = _ChineseApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar = app.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", _ChatTextArea)
        text_area.focus()
        assert text_area.insert_reference("quote", "said")
        await text_area._on_paste(Paste(_FOUR_LINES))
        await pilot.pause()

        assert input_bar.value == "[引用 #1] [粘贴文本 #1 +3 行]"
        assert _live(text_area) == ["[引用 #1]", "[粘贴文本 #1 +3 行]"]
        assert input_bar.draft().sent_text() == f"“said” {_FOUR_LINES}"


def _label_colors(text_area: _ChatTextArea, row: int, label: str) -> set[object]:
    """The colors the label's cells are painted with on screen row *row*."""
    colors: set[object] = set()
    offset = 0
    start = text_area.document[row].index(label)
    for segment in text_area.render_line(row):
        end = offset + len(segment.text)
        if offset < start + len(label) and end > start and segment.style is not None:
            colors.add(segment.style.color)
        offset = end
    return colors


@pytest.mark.asyncio
async def test_a_label_keeps_its_color_on_the_cursor_line() -> None:
    app = _LabelApp()
    async with app.run_test(size=(100, 30)) as pilot:
        _input_bar, text_area = _parts(app)
        text_area.focus()
        assert text_area.insert_reference("quote", "first")
        text_area.insert("\n")
        assert text_area.insert_reference("quote", "second")
        await pilot.pause()
        assert text_area.highlight_cursor_line
        assert text_area.cursor_location[0] == 1
        label_color = text_area.get_component_rich_style("draft--reference", partial=False).color

        assert _label_colors(text_area, 0, "[Quote #1]") == {label_color}
        assert _label_colors(text_area, 1, "[Quote #2]") == {label_color}


@pytest.mark.asyncio
async def test_a_long_pasted_side_question_shows_the_side_question_button() -> None:
    """Typed or pasted (folded by its size), the same ``/btw`` question reads the same on the button."""
    from textual.widgets import Button

    app = _LabelApp()
    async with app.run_test(size=(100, 30)) as pilot:
        input_bar, text_area = _parts(app)
        input_bar.agent_running = True
        text_area.focus()
        await text_area._on_paste(Paste("/btw " + "why " * 250))
        assert text_area.text.startswith("[Pasted text #1")

        await wait_for(
            lambda: str(input_bar.query_one("#send-btn", Button).label) == "Send side question",
            pilot=pilot,
            description="the button offers the side question",
        )


@pytest.mark.asyncio
async def test_a_quote_is_capped_as_a_paste_of_the_same_text_is() -> None:
    app = _LabelApp()
    async with app.run_test(size=(100, 30)) as pilot:
        _input_bar, text_area = _parts(app)
        text_area._max_paste_tokens = 20
        long_text = "word " * 200
        await text_area._on_paste(Paste(long_text))
        pasted = text_area.text
        assert len(pasted) < len(long_text)
        text_area.clear()

        assert text_area.insert_reference("quote", long_text)
        await pilot.pause()

        quote = text_area.references.get("quote", 1)
        assert quote is not None
        assert quote.content == pasted
