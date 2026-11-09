# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``/buddy`` subcommands: what each one does to the saved buddy, and what it answers."""

from __future__ import annotations

from random import Random
from typing import TYPE_CHECKING

import pytest
from rich.cells import cell_len

from chrys.app.features.buddy import actions
from chrys.app.features.buddy import store as store_module
from chrys.app.features.buddy.commands import (
    CommandAnswer,
    buddy_card,
    handle_buddy_command,
    pet_refusal,
    split_command,
)
from chrys.app.features.buddy.model import Rarity, Trait
from chrys.app.features.buddy.replies import STOCK_REPLIES
from chrys.foundation.i18n import Localizer, MessageRef
from chrys.foundation.i18n.formatting import format_message
from tests.support.buddies import a_buddy, a_record, turns_to_finish

if TYPE_CHECKING:
    from pathlib import Path


# The card shows the hatch day on the user's own calendar.
_HATCH_DAY = f"{a_record().hatched_at.astimezone():%Y-%m-%d}"


def _text(answer: CommandAnswer) -> str:
    message, _severity = answer
    return message if isinstance(message, str) else format_message(message)


@pytest.mark.parametrize("arg", [None, "", "   "])
def test_bare_buddy_introduces_the_egg_and_then_the_buddy(arg: str | None) -> None:
    answer = handle_buddy_command(arg)
    assert answer[1] == "information"
    assert "/buddy hatch" in _text(answer)

    buddy = actions.hatch(Random(1))
    assert buddy.name in _text(handle_buddy_command(arg))


@pytest.mark.parametrize("arg", ["info", "mute", "name Pico"])
def test_a_command_about_a_buddy_needs_one(arg: str) -> None:
    answer = handle_buddy_command(arg)

    assert answer[1] == "warning"
    assert "/buddy hatch" in _text(answer)
    assert actions.current_buddy() is None


def test_hatch_hatches_once_and_shows_the_newcomer() -> None:
    answer = handle_buddy_command("hatch")
    buddy = actions.current_buddy()

    assert buddy is not None
    assert answer[1] == "information"
    assert buddy_card(buddy) in _text(answer)

    again = handle_buddy_command("HATCH")
    assert again[1] == "warning"
    assert buddy.name in _text(again)
    assert actions.current_buddy() == buddy


def test_info_answers_with_the_card() -> None:
    buddy = actions.hatch(Random(2))

    assert handle_buddy_command("info") == (buddy_card(buddy), "information")


def _in_brackets(reference: MessageRef) -> str:
    return f"<{reference.definition.key}>"


def test_the_card_shows_the_persona_in_the_language_of_whoever_asked_for_it() -> None:
    hatched = _text(handle_buddy_command("hatch", render=_in_brackets))
    buddy = actions.current_buddy()
    assert buddy is not None
    persona = f"“<{buddy.persona.definition.key}>”"

    assert persona in hatched
    assert persona in buddy_card(buddy, render=_in_brackets).splitlines()
    assert _text(handle_buddy_command("info", render=_in_brackets)) == buddy_card(buddy, render=_in_brackets)
    assert f"“{format_message(buddy.persona)}”" in buddy_card(buddy).splitlines()


def test_mute_toggles_and_says_which_way() -> None:
    actions.hatch(Random(3))

    muted = _text(handle_buddy_command("mute"))
    assert actions.current_buddy().muted
    unmuted = _text(handle_buddy_command("mute"))
    assert not actions.current_buddy().muted
    assert muted != unmuted


def test_name_renames_to_the_cleaned_name_and_refuses_an_empty_one() -> None:
    hatched = actions.hatch(Random(4))

    assert handle_buddy_command("name")[1] == "warning"
    assert handle_buddy_command("name    ")[1] == "warning"
    assert actions.current_buddy() == hatched

    answer = handle_buddy_command("Name   Sir   Quacks ")
    assert answer[1] == "information"
    assert "Sir Quacks" in _text(answer)
    assert actions.current_buddy().name == "Sir Quacks"


def test_an_unknown_command_is_named_back_with_the_ones_that_exist() -> None:
    answer = handle_buddy_command("dance wildly")

    assert answer[1] == "warning"
    assert "dance wildly" in _text(answer)
    assert "hatch" in _text(answer)


def test_pet_asked_here_is_counted_and_answered_with_a_stock_line() -> None:
    assert handle_buddy_command("pet")[1] == "warning"

    buddy = actions.hatch(Random(5))
    answer = handle_buddy_command(" Pet ")

    assert answer == (answer[0], "information")
    assert _text(answer) in {format_message(line.bind(name=buddy.name)) for line in STOCK_REPLIES}
    assert actions.current_buddy().record.pets == 1


def test_pet_asked_here_answers_in_the_user_language() -> None:
    buddy = actions.hatch(Random(5))
    message, severity = handle_buddy_command(" Pet ")

    assert severity == "information"
    assert isinstance(message, MessageRef)
    chinese = Localizer("zh-Hans").render(message)
    assert buddy.name in chinese
    assert chinese != format_message(message)


def test_a_muted_buddy_is_not_petted_through_the_command() -> None:
    actions.hatch(Random(5))
    actions.set_muted(True)

    assert pet_refusal(actions.current_buddy()) == handle_buddy_command("pet")
    assert handle_buddy_command("pet")[1] == "warning"
    assert actions.current_buddy().record.pets == 0
    assert pet_refusal(a_buddy()) is None


@pytest.mark.parametrize("arg", ["hatch", "mute", "name Nori", "pet"])
@pytest.mark.parametrize("failure", [PermissionError("read-only"), TimeoutError("Timed out acquiring lock")])
def test_a_save_file_that_cannot_be_written_is_a_warning_and_not_a_crash(
    arg: str, failure: OSError, monkeypatch: pytest.MonkeyPatch
) -> None:
    if arg != "hatch":
        actions.hatch(Random(6))
    before = actions.current_buddy()

    def fail(path: Path, payload: str) -> bytes:
        raise failure

    monkeypatch.setattr(store_module, "atomic_write_text", fail)
    answer = handle_buddy_command(arg)

    assert answer[1] == "warning"
    assert "save file" in _text(answer)
    assert actions.current_buddy() == before


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        (None, ("", "")),
        ("  ", ("", "")),
        ("INFO", ("info", "")),
        ("name  Nori the Second ", ("name", "Nori the Second ")),
        # Whatever the input method puts between the words separates them.
        ("name\u3000Nori", ("name", "Nori")),
        ("name\tNori", ("name", "Nori")),
    ],
)
def test_a_command_splits_into_its_verb_and_the_rest(arg: str | None, expected: tuple[str, str]) -> None:
    assert split_command(arg) == expected


def test_a_gauge_is_full_only_when_what_it_measures_is() -> None:
    # 96 of 100 rounds to ten cells and must still show nine.
    almost = buddy_card(a_buddy(turns=9, pets=3)).splitlines()[1]
    assert almost == "level      ▰▰▰▰▰▰▰▰▰▱  1 · 96/100 XP"


def test_the_card_of_a_hatchling_shows_where_it_stands() -> None:
    assert buddy_card(a_buddy(turns=3, pets=2)).splitlines() == [
        "🐾 Pico · owl · R",
        "level      ▰▰▰▱▱▱▱▱▱▱  1 · 34/100 XP",
        "focus      ▰▰▰▰▰▰▱▱▱▱  60",
        "curiosity  ▰▰▰▰▱▱▱▱▱▱  40",
        "grit       ▰▰▰▱▱▱▱▱▱▱  30",
        "charm      ▰▰▱▱▱▱▱▱▱▱  20",
        "“Watches every keystroke in silence and hates being interrupted.”",
        f"{_HATCH_DAY} · 3 turns · 2 pets",
    ]


def test_the_card_of_a_finished_buddy_shows_what_it_became() -> None:
    top = a_buddy(turns=turns_to_finish(3))
    card = buddy_card(top).splitlines()

    assert top.rarity is Rarity.SSR
    assert card[0] == "🐾 🌟 Pico · owl · SSR · shiny"
    assert card[1] == "level      ▰▰▰▰▰▰▰▰▰▰  100 · fully grown"
    assert card[2] == "focus      ▰▰▰▰▰▰▰▰▰▱  90"
    assert card[-1] == f"{_HATCH_DAY} · {top.record.turns} turns · 0 pets · evolution 2"


def test_the_card_labels_are_in_the_language_of_whoever_asked_for_it() -> None:
    card = buddy_card(a_buddy(turns=3, pets=2), render=_in_brackets).splitlines()
    assert card[0] == "🐾 Pico · owl · R"
    assert card[1].startswith("<tui.buddy.card.level>")
    assert card[1].endswith("  ▰▰▰▱▱▱▱▱▱▱  <tui.buddy.card.progress>")
    assert [line.split("  ")[0] for line in card[2:6]] == [f"<tui.buddy.card.trait.{trait.value}>" for trait in Trait]
    assert card[-1] == "<tui.buddy.card.since>"

    finished = buddy_card(a_buddy(turns=turns_to_finish(3)), render=_in_brackets).splitlines()
    assert finished[0] == "🐾 🌟 Pico · owl · SSR · <tui.buddy.card.shiny>"
    assert finished[1].endswith("  ▰▰▰▰▰▰▰▰▰▰  <tui.buddy.card.fully_grown>")
    assert finished[-1] == "<tui.buddy.card.since> · <tui.buddy.card.evolution>"


def test_the_card_lines_its_gauges_up_by_cell_whatever_the_labels_are_made_of() -> None:
    # Labels a character wide, two wide, and mixed: every gauge still starts on the same column.
    labels = {
        "tui.buddy.card.level": "等级",
        "tui.buddy.card.trait.focus": "专注力",
        "tui.buddy.card.trait.curiosity": "好奇心",
        "tui.buddy.card.trait.grit": "毅力",
        "tui.buddy.card.trait.charm": "魅力 x",
    }

    def render(reference: MessageRef) -> str:
        return labels.get(reference.definition.key, format_message(reference))

    card = buddy_card(a_buddy(turns=3, pets=2), render=render).splitlines()

    assert card[1].startswith("等级    ▰")
    assert {_gauge_column(line) for line in card[1:6]} == {cell_len("专注力") + 2}


def _gauge_column(line: str) -> int:
    return cell_len(line[: min(index for index in (line.find("▰"), line.find("▱")) if index >= 0)])
