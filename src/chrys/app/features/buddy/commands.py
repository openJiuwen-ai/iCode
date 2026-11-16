# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The ``/buddy`` command: what each subcommand does and what it answers.

Everything here answers at once. The TUI can wait for a model, so it answers ``/buddy pet``
itself, after :func:`pet_refusal`, and never asks for it here. Asked here anyway, petting is
counted and gets a stock line.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

from rich.cells import cell_len

from chrys.app.features.buddy import actions
from chrys.app.features.buddy.model import TRAIT_MAX, Trait, clean_name
from chrys.app.features.buddy.replies import stock_reply
from chrys.foundation.i18n import DisplayBlock, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message

if TYPE_CHECKING:
    from collections.abc import Callable

    from chrys.app.features.buddy.model import Buddy

logger = logging.getLogger(__name__)

_GAUGE_CELLS = 10

type Severity = Literal["information", "warning"]
type CommandAnswer = tuple[MessageRef | str, Severity]
# The card is text put together here, so the persona line inside it is rendered here, by the caller's renderer.
type Render = Callable[[MessageRef], str]

_NO_BUDDY_TO_PET = msg(
    "tui.buddy.no_buddy_to_pet",
    fallback="There is nobody to pet yet. Hatch a buddy with /buddy hatch.",
)
_BUDDY_MUTED = msg(
    "tui.buddy.muted",
    fallback="{name} is muted and stays quiet. Use /buddy mute to hear from it again.",
)
_INTRO_NO_BUDDY = msg(
    "tui.buddy.intro_no_buddy",
    fallback="🥚 There is an egg here, and nobody knows what is in it.\n\n/buddy hatch finds out.",
    multiline=True,
)
_INTRO_WITH_BUDDY = msg(
    "tui.buddy.intro_with_buddy",
    fallback="🐾 {name} is keeping you company.\n\n/buddy info shows how it is doing, /buddy pet gives it a pat.",
    multiline=True,
)
_NOT_HATCHED = msg("tui.buddy.not_hatched", fallback="No buddy has hatched yet. Start with /buddy hatch.")
_HATCH_ALREADY = msg("tui.buddy.hatch_already", fallback="{name} is already here, and one buddy is all you get.")
_HATCH_SUCCESS = msg(
    "tui.buddy.hatch_success",
    fallback="🐣 Out of the egg:\n\n{info}\n\nFrom now on it lives in the sidebar, on the Buddy tab.",
    multiline=True,
)
_MUTE_ON = msg("tui.buddy.mute_on", fallback="Your buddy will keep quiet from now on. It is still around.")
_MUTE_OFF = msg("tui.buddy.mute_off", fallback="Your buddy can speak up again.")
_NAME_EMPTY = msg("tui.buddy.name_empty", fallback="Give the new name too, for example /buddy name Mochi")
_NAME_SUCCESS = msg("tui.buddy.name_success", fallback="Your buddy answers to {name} now.")
_SAVE_FAILED = msg(
    "tui.buddy.save_failed",
    fallback="The buddy save file could not be updated, so nothing has changed. Try again in a moment.",
)
_UNKNOWN_COMMAND = msg(
    "tui.buddy.unknown",
    fallback='/buddy does not know "{arg}".\n\nIt knows: hatch, info, pet, mute, name <new name>',
    multiline=True,
)
# The card's labels. Species and rarity stay as they are spelled everywhere else.
_CARD_SHINY = msg("tui.buddy.card.shiny", fallback="shiny")
_CARD_LEVEL = msg("tui.buddy.card.level", fallback="level")
_CARD_FULLY_GROWN = msg("tui.buddy.card.fully_grown", fallback="{level} · fully grown")
_CARD_PROGRESS = msg("tui.buddy.card.progress", fallback="{level} · {xp}/{needed} XP")
_CARD_TRAIT_FOCUS = msg("tui.buddy.card.trait.focus", fallback="focus")
_CARD_TRAIT_CURIOSITY = msg("tui.buddy.card.trait.curiosity", fallback="curiosity")
_CARD_TRAIT_GRIT = msg("tui.buddy.card.trait.grit", fallback="grit")
_CARD_TRAIT_CHARM = msg("tui.buddy.card.trait.charm", fallback="charm")
_CARD_SINCE = msg("tui.buddy.card.since", fallback="{hatched} · {turns} turns · {pets} pets")
_CARD_TRAITS = {
    Trait.FOCUS: _CARD_TRAIT_FOCUS,
    Trait.CURIOSITY: _CARD_TRAIT_CURIOSITY,
    Trait.GRIT: _CARD_TRAIT_GRIT,
    Trait.CHARM: _CARD_TRAIT_CHARM,
}


def split_command(arg: str | None) -> tuple[str, str]:
    """``/buddy <arg>`` as a lower-case subcommand and whatever follows it. Any whitespace separates the two."""
    verb, rest, *_ = [*(arg or "").split(None, 1), "", ""]
    return verb.lower(), rest


def pet_refusal(buddy: Buddy | None) -> CommandAnswer | None:
    """Why *buddy* cannot be petted through the command right now, or None when it can."""
    if buddy is None:
        return _NO_BUDDY_TO_PET.bind(), "warning"
    if buddy.muted:
        return _BUDDY_MUTED.bind(name=buddy.display_name), "warning"
    return None


def handle_buddy_command(arg: str | None, *, render: Render = format_message) -> CommandAnswer:
    """Carry out ``/buddy <arg>`` and say what happened. *render* puts the info card in the user's language."""
    try:
        return _carry_out(arg, render)
    except OSError:  # a read-only or full config directory, or another instance wedged on the save file
        logger.warning("Buddy save file could not be updated", exc_info=True)
        return _SAVE_FAILED.bind(), "warning"


def _carry_out(arg: str | None, render: Render) -> CommandAnswer:
    buddy = actions.current_buddy()
    verb, rest = split_command(arg)

    if not verb:
        if buddy is None:
            return _INTRO_NO_BUDDY.bind(), "information"
        return _INTRO_WITH_BUDDY.bind(name=buddy.display_name), "information"
    if verb == "hatch":
        if buddy is not None:
            return _HATCH_ALREADY.bind(name=buddy.display_name), "warning"
        return _HATCH_SUCCESS.bind(info=DisplayBlock(buddy_card(actions.hatch(), render=render))), "information"
    if verb == "pet":
        refusal = pet_refusal(buddy)
        if refusal is not None:
            return refusal
        if buddy is None:
            raise RuntimeError("Petting requires an existing buddy.")
        actions.record_pet()
        return stock_reply(buddy), "information"
    if verb not in {"info", "mute", "name"}:
        return _UNKNOWN_COMMAND.bind(arg=arg or ""), "warning"
    if buddy is None:
        return _NOT_HATCHED.bind(), "warning"
    if verb == "info":
        return buddy_card(buddy, render=render), "information"
    if verb == "mute":
        actions.set_muted(not buddy.muted)
        return (_MUTE_OFF if buddy.muted else _MUTE_ON).bind(), "information"
    name = clean_name(rest)
    if not name:
        return _NAME_EMPTY.bind(), "warning"
    actions.rename(name)
    return _NAME_SUCCESS.bind(name=name), "information"


def buddy_card(buddy: Buddy, *, render: Render = format_message) -> str:
    """*buddy* at a glance: who it is, how far it has come, what it is like, and since when."""
    progress, record = buddy.progress, buddy.record
    who = [buddy.display_name, buddy.species.value, buddy.rarity.value]
    if buddy.shiny:
        who.append(render(_CARD_SHINY.bind()))
    if progress.maxed:
        climb = f"{_gauge(1, 1)}  {render(_CARD_FULLY_GROWN.bind(level=buddy.level))}"
    else:
        into, needed = progress.xp_into_level, progress.xp_for_level
        climb = f"{_gauge(into, needed)}  {render(_CARD_PROGRESS.bind(level=buddy.level, xp=into, needed=needed))}"
    level_label = render(_CARD_LEVEL.bind())
    trait_labels = {trait: render(_CARD_TRAITS[trait].bind()) for trait in Trait}
    label_width = max(cell_len(label) for label in (level_label, *trait_labels.values()))
    traits = buddy.traits
    since = render(
        _CARD_SINCE.bind(hatched=f"{record.hatched_at.astimezone():%Y-%m-%d}", turns=record.turns, pets=record.pets)
    )
    return "\n".join(
        [
            f"🐾 {' · '.join(who)}",
            f"{_padded(level_label, label_width)}  {climb}",
            *(
                f"{_padded(trait_labels[trait], label_width)}  {_gauge(traits[trait], TRAIT_MAX)}  {traits[trait]}"
                for trait in Trait
            ),
            f"“{render(buddy.persona)}”",
            since,
        ]
    )


def _padded(label: str, width: int) -> str:
    # Padded by cells, not characters: a CJK label is two cells wide per character.
    return label + " " * (width - cell_len(label))


def _gauge(value: int, full: int) -> str:
    # Rounded down: a gauge is full only when what it measures is.
    filled = _GAUGE_CELLS * value // full
    return "▰" * filled + "▱" * (_GAUGE_CELLS - filled)
