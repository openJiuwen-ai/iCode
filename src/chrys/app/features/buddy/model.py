# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What a buddy is: the record that is saved, and the living view derived from it."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

from chrys.app.features.buddy.progression import FINAL_LEVEL, Progress, earned_xp, progress
from chrys.foundation.i18n import MessageDef, MessageRef, msg

if TYPE_CHECKING:
    from collections.abc import Mapping

TRAIT_MIN = 1
TRAIT_MAX = 100
NAME_MAX_LENGTH = 24


class Rarity(StrEnum):
    """Rarity tiers, lowest first. A buddy evolves upwards through them."""

    N = "N"
    R = "R"
    SR = "SR"
    SSR = "SSR"

    @property
    def rank(self) -> int:
        """Position among the tiers, starting at 0."""
        return _RARITIES.index(self)


_RARITIES = tuple(Rarity)


class Species(StrEnum):
    """Every species with pixel artwork, in alphabetical order."""

    ALIEN = "alien"
    AXOLOTL = "axolotl"
    BAT = "bat"
    BEE = "bee"
    CACTUS = "cactus"
    CAPYBARA = "capybara"
    CAT = "cat"
    CRAB = "crab"
    DRAGON = "dragon"
    DUCK = "duck"
    FOX = "fox"
    FROG = "frog"
    GHOST = "ghost"
    GOOSE = "goose"
    JELLY = "jelly"
    LLAMA = "llama"
    MUSHROOM = "mushroom"
    OCTOPUS = "octopus"
    OWL = "owl"
    PANDA = "panda"
    PENGUIN = "penguin"
    RABBIT = "rabbit"
    ROBOT = "robot"
    SHARK = "shark"
    SNAIL = "snail"
    SNAKE = "snake"
    TABBY = "tabby"
    TURTLE = "turtle"


class Trait(StrEnum):
    """What a buddy is like. The strongest trait decides its persona."""

    FOCUS = "focus"
    CURIOSITY = "curiosity"
    GRIT = "grit"
    CHARM = "charm"


_PERSONA_FOCUS = msg(
    "tui.buddy.persona.focus",
    fallback="Watches every keystroke in silence and hates being interrupted.",
)
_PERSONA_CURIOSITY = msg(
    "tui.buddy.persona.curiosity",
    fallback="Wants to know what every file does and why it is named that way.",
)
_PERSONA_GRIT = msg("tui.buddy.persona.grit", fallback="Never gives up on a failing test, however late it gets.")
_PERSONA_CHARM = msg("tui.buddy.persona.charm", fallback="Cheers for every green build as if it were the first.")
# The info card and the sidebar show the persona in the user's language; the model is told it in English.
_PERSONAS: dict[Trait, MessageDef] = {
    Trait.FOCUS: _PERSONA_FOCUS,
    Trait.CURIOSITY: _PERSONA_CURIOSITY,
    Trait.GRIT: _PERSONA_GRIT,
    Trait.CHARM: _PERSONA_CHARM,
}


@dataclass(frozen=True)
class Appearance:
    """Everything a portrait needs to know."""

    species: Species
    rarity: Rarity
    shiny: bool = False


@dataclass(frozen=True)
class BuddyRecord:
    """The saved buddy: what was drawn when it hatched, and what has happened to it since."""

    species: Species
    rarity: Rarity
    shiny: bool
    traits: Mapping[Trait, int] = field(hash=False)
    name: str
    hatched_at: datetime
    turns: int = 0
    pets: int = 0
    muted: bool = False

    def __post_init__(self) -> None:
        # Frozen all the way down: the caller's mapping is copied and the copy is read-only.
        object.__setattr__(self, "traits", MappingProxyType(dict(self.traits)))

    def to_json(self) -> dict[str, object]:
        """The JSON document this record is saved as."""
        return {
            "species": self.species.value,
            "rarity": self.rarity.value,
            "shiny": self.shiny,
            "traits": {trait.value: value for trait, value in self.traits.items()},
            "name": self.name,
            "hatched_at": self.hatched_at.isoformat(),
            "turns": self.turns,
            "pets": self.pets,
            "muted": self.muted,
        }

    @classmethod
    def from_json(cls, document: object) -> BuddyRecord | None:
        """Read a saved document back, or None unless it is exactly what :meth:`to_json` writes.

        Nothing is coerced: JSON's ``true`` is not a count, ``"7"`` is not a number,
        and a name that would be cleaned up differently was not written here.
        """
        if not isinstance(document, dict) or document.keys() != _DOCUMENT_KEYS:
            return None
        try:
            saved_traits = _exactly(dict, document["traits"])
            if saved_traits.keys() != _TRAIT_KEYS:
                return None
            record = cls(
                species=Species(_exactly(str, document["species"])),
                rarity=Rarity(_exactly(str, document["rarity"])),
                shiny=_exactly(bool, document["shiny"]),
                traits={trait: _exactly(int, saved_traits[trait.value]) for trait in Trait},
                name=_exactly(str, document["name"]),
                hatched_at=datetime.fromisoformat(_exactly(str, document["hatched_at"])),
                turns=_exactly(int, document["turns"]),
                pets=_exactly(int, document["pets"]),
                muted=_exactly(bool, document["muted"]),
            )
        except TypeError, ValueError:
            return None
        sound = (
            all(TRAIT_MIN <= value <= TRAIT_MAX for value in record.traits.values())
            and record.name
            and record.name == clean_name(record.name)
            and record.hatched_at.tzinfo is not None
            and record.turns >= 0
            and record.pets >= 0
        )
        return record if sound else None


_DOCUMENT_KEYS = frozenset({"species", "rarity", "shiny", "traits", "name", "hatched_at", "turns", "pets", "muted"})
_TRAIT_KEYS = frozenset(trait.value for trait in Trait)


def _exactly[T](kind: type[T], value: object) -> T:
    """*value*, which has to be a *kind* and nothing that merely passes for one (``True`` is not an ``int``)."""
    if not isinstance(value, kind) or type(value) is not kind:
        raise TypeError(f"expected {kind.__name__}, got {type(value).__name__}")
    return value


def clean_name(name: str) -> str:
    """One visible line of bounded length: a name is shown in toasts, a nameplate and a prompt.

    Control, formatting and other invisible characters are dropped, so a name cannot
    be empty to the eye, carry an escape sequence, or reorder the text around it.
    """
    words = ("".join(filter(str.isprintable, word)) for word in name.split())
    return " ".join(word for word in words if word)[:NAME_MAX_LENGTH].rstrip()


@dataclass(frozen=True)
class Buddy:
    """A record together with how far it has come."""

    record: BuddyRecord
    progress: Progress

    @classmethod
    def of(cls, record: BuddyRecord) -> Buddy:
        """Work out how far *record* has come."""
        final_stage = len(_RARITIES) - 1 - record.rarity.rank
        return cls(record, progress(earned_xp(record.turns, record.pets), final_stage))

    @property
    def name(self) -> str:
        return self.record.name

    @property
    def display_name(self) -> str:
        """The name, marked once the buddy has come all the way."""
        if not self.honored:
            return self.name
        return f"{'🌟' if self.rarity is _RARITIES[-1] else '✨'} {self.name}"

    @property
    def species(self) -> Species:
        return self.record.species

    @property
    def rarity(self) -> Rarity:
        """The tier the buddy has evolved to."""
        return _RARITIES[self.record.rarity.rank + self.progress.stage]

    @property
    def evolved(self) -> bool:
        return self.progress.stage > 0

    @property
    def level(self) -> int:
        return self.progress.level

    @property
    def honored(self) -> bool:
        """Whether the buddy has come all the way: it evolved, or it maxed the top tier."""
        return self.evolved or self.level == FINAL_LEVEL

    @property
    def shiny(self) -> bool:
        """Hatched shiny, or earned: reaching the end of the top tier, by evolution or by level."""
        earned = self.rarity is _RARITIES[-1] and self.honored
        return self.record.shiny or earned

    @property
    def traits(self) -> dict[Trait, int]:
        """Traits grow a point every ten levels and ten points every evolution."""
        growth = 10 * self.progress.stage + self.level // 10
        return {trait: min(TRAIT_MAX, value + growth) for trait, value in self.record.traits.items()}

    @property
    def persona(self) -> MessageRef:
        """One sentence of character, set by the trait the buddy hatched strongest in."""
        return _PERSONAS[max(Trait, key=lambda trait: self.record.traits[trait])].bind()

    @property
    def muted(self) -> bool:
        return self.record.muted

    @property
    def appearance(self) -> Appearance:
        return Appearance(self.species, self.rarity, self.shiny)
