# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The saved record and the buddy derived from it."""

from __future__ import annotations

from dataclasses import replace

import pytest

from chrys.app.features.buddy.model import (
    NAME_MAX_LENGTH,
    TRAIT_MAX,
    Appearance,
    Buddy,
    BuddyRecord,
    Rarity,
    Species,
    Trait,
    clean_name,
)
from chrys.app.features.buddy.progression import FINAL_LEVEL, XP_PER_TURN, xp_for_level
from tests.support.buddies import a_record, turns_to_finish


def test_species_are_listed_alphabetically_and_tiers_lowest_first() -> None:
    names = [species.value for species in Species]
    assert names == sorted(names)
    assert [rarity.rank for rarity in Rarity] == [0, 1, 2, 3]


def test_a_record_survives_the_trip_through_json() -> None:
    record = a_record(turns=12, pets=3, muted=True, shiny=True, name="Señor 🦉")
    assert BuddyRecord.from_json(record.to_json()) == record


@pytest.mark.parametrize(
    "damage",
    [
        {"species": "unicorn"},
        {"rarity": "UR"},
        {"traits": {"focus": 60}},
        {"traits": {"focus": 60, "curiosity": 40, "grit": 30, "charm": 101}},
        {"traits": {"focus": 60, "curiosity": 40, "grit": 30, "charm": 0}},
        {"traits": [60, 40, 30, 20]},
        {"name": "   "},
        {"hatched_at": "yesterday"},
        {"hatched_at": 1_790_000_000},
        {"hatched_at": "2026-09-22T08:30:00"},
        {"hatched_at": "2026-09-22"},
        {"turns": -1},
        {"pets": -1},
        {"turns": "many"},
        # Nothing is coerced into shape: each of these merely passes for the type that is saved.
        {"turns": True},
        {"turns": 3.0},
        {"turns": "7"},
        {"pets": float("inf")},
        {"shiny": "false"},
        {"muted": 0},
        {"name": None},
        {"name": "Pi\u200bco"},
        {"name": "  Pico"},
        {"traits": {"focus": 60, "curiosity": 40, "grit": 30, "charm": True}},
        {"traits": {"focus": 60, "curiosity": 40, "grit": 30, "charm": 20, "luck": 5}},
        {"level": 99},
    ],
)
def test_a_damaged_document_is_not_a_record(damage: dict[str, object]) -> None:
    assert BuddyRecord.from_json({**a_record().to_json(), **damage}) is None


@pytest.mark.parametrize(
    "missing", ["species", "rarity", "shiny", "traits", "name", "hatched_at", "turns", "pets", "muted"]
)
def test_an_incomplete_document_is_not_a_record(missing: str) -> None:
    document = a_record().to_json()
    del document[missing]
    assert BuddyRecord.from_json(document) is None


@pytest.mark.parametrize("document", [None, [], "buddy", 7])
def test_only_an_object_can_be_a_record(document: object) -> None:
    assert BuddyRecord.from_json(document) is None


def test_a_record_is_frozen_all_the_way_down() -> None:
    given = {Trait.FOCUS: 60, Trait.CURIOSITY: 40, Trait.GRIT: 30, Trait.CHARM: 20}
    record = a_record(traits=given)
    given[Trait.FOCUS] = 1

    assert record.traits[Trait.FOCUS] == 60
    with pytest.raises(TypeError):
        record.traits[Trait.FOCUS] = 1  # type: ignore[index]
    assert replace(record, pets=1).traits == record.traits
    assert hash(record) == hash(a_record())
    assert hash(Buddy.of(record)) == hash(Buddy.of(a_record()))


@pytest.mark.parametrize(
    ("typed", "kept"),
    [
        ("Pi\x1b[31mco", "Pi[31mco"),
        ("Pi\x00co", "Pico"),
        ("Pi\u200bco", "Pico"),
        ("\u202eocip", "ocip"),
        ("\u200b \u200d", ""),
        ("Pico\u00a0the\u3000owl", "Pico the owl"),
        ("Señor 🦉", "Señor 🦉"),
        ("caf\udce9", "caf"),
    ],
)
def test_names_keep_only_what_can_be_seen(typed: str, kept: str) -> None:
    assert clean_name(typed) == kept
    assert clean_name(kept) == kept


def test_names_are_one_bounded_line() -> None:
    assert clean_name("  Sir \n Quacks\ta lot ") == "Sir Quacks a lot"
    assert clean_name(" \n ") == ""
    long = clean_name("x" * (NAME_MAX_LENGTH - 1) + " yz")
    assert long == "x" * (NAME_MAX_LENGTH - 1)
    assert len(clean_name("y" * 200)) == NAME_MAX_LENGTH


def test_a_hatchling_is_level_one_and_looks_the_way_it_hatched() -> None:
    buddy = Buddy.of(a_record())
    assert (buddy.level, buddy.evolved, buddy.shiny) == (1, False, False)
    assert buddy.display_name == buddy.name == "Pico"
    assert buddy.traits == a_record().traits
    assert buddy.appearance == Appearance(Species.OWL, Rarity.R, False)


def test_the_strongest_hatch_trait_sets_the_persona_for_good() -> None:
    focused = Buddy.of(a_record())
    charming = Buddy.of(a_record(traits={Trait.FOCUS: 20, Trait.CURIOSITY: 40, Trait.GRIT: 30, Trait.CHARM: 60}))
    assert focused.persona != charming.persona
    assert Buddy.of(a_record(turns=turns_to_finish(1))).persona == focused.persona
    # A message, not text: whoever shows it puts it in the user's language.
    assert focused.persona.definition.key == "tui.buddy.persona.focus"
    assert charming.persona.definition.key == "tui.buddy.persona.charm"


def test_evolving_moves_up_a_tier_marks_the_name_and_grows_every_trait() -> None:
    evolved = Buddy.of(a_record(turns=turns_to_finish(1)))
    assert (evolved.evolved, evolved.level, evolved.rarity) == (True, 1, Rarity.SR)
    assert evolved.display_name == "✨ Pico"
    assert evolved.traits == {trait: value + 10 for trait, value in a_record().traits.items()}
    assert evolved.record.rarity is Rarity.R


def test_levels_grow_traits_a_point_every_ten_and_never_past_the_cap() -> None:
    turns_to_level_ten = -(-sum(xp_for_level(level, 0) for level in range(1, 10)) // XP_PER_TURN)
    assert Buddy.of(a_record(turns=turns_to_level_ten)).traits[Trait.FOCUS] == 61
    strong = a_record(traits={Trait.FOCUS: 99, Trait.CURIOSITY: 40, Trait.GRIT: 30, Trait.CHARM: 20})
    assert Buddy.of(replace(strong, turns=turns_to_finish(2))).traits[Trait.FOCUS] == TRAIT_MAX


def test_reaching_the_top_tier_by_evolving_earns_the_shine() -> None:
    top = Buddy.of(a_record(turns=turns_to_finish(2)))
    assert (top.rarity, top.shiny) == (Rarity.SSR, True)
    assert top.display_name == "🌟 Pico"
    assert top.appearance == Appearance(Species.OWL, Rarity.SSR, True)
    assert not Buddy.of(a_record(turns=turns_to_finish(1))).shiny


def test_a_buddy_hatched_in_the_top_tier_earns_the_shine_at_the_final_level() -> None:
    record = a_record(rarity=Rarity.SSR)
    almost = Buddy.of(replace(record, turns=turns_to_finish(1) - 1))
    assert (almost.shiny, almost.honored, almost.display_name) == (False, False, "Pico")
    finished = Buddy.of(replace(record, turns=turns_to_finish(1)))
    assert (finished.level, finished.evolved, finished.shiny) == (FINAL_LEVEL, False, True)
    assert finished.honored
    assert finished.display_name == "🌟 Pico"
    assert finished.progress.maxed


def test_a_buddy_hatched_shiny_stays_shiny() -> None:
    assert Buddy.of(a_record(shiny=True)).shiny


def test_the_name_is_marked_for_what_was_earned_not_for_what_was_hatched() -> None:
    # Hatched shiny, at level one: the shine is real, the mark is not.
    lucky = Buddy.of(a_record(shiny=True))
    assert (lucky.shiny, lucky.honored, lucky.display_name) == (True, False, "Pico")
