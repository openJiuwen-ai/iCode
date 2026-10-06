# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Versioned local frontend contract. Chrys alone owns Buddy data and artwork."""

from __future__ import annotations

from dataclasses import asdict, replace
from random import SystemRandom
from typing import TYPE_CHECKING

from chrys.app.features.buddy.animation import _TEMPERAMENTS, FRAME_COUNT, IDLE_FRAME_COUNT
from chrys.app.features.buddy.hatchery import hatchling
from chrys.app.features.buddy.model import Buddy, Species, clean_name
from chrys.app.features.buddy.pixel_renderer import matrix_to_image
from chrys.app.features.buddy.pixel_sprites import (
    PIXEL_HEIGHT,
    PIXEL_WIDTH,
    _apply_blink,
    _build_petting_frame,
    species_sprite,
)
from chrys.app.features.buddy.store import BuddyStore

if TYPE_CHECKING:
    from chrys.app.features.buddy.model import BuddyRecord

PROTOCOL = "chrys-buddy-v1"


def snapshot(record: BuddyRecord | None) -> dict[str, object] | None:
    if record is None:
        return None
    buddy = Buddy.of(record)
    return {
        "record": record.to_json(),
        "level": buddy.level,
        "rarity": buddy.rarity.value,
        "shiny": buddy.shiny,
        "traits": {key.value: value for key, value in buddy.traits.items()},
        "progress": asdict(buddy.progress),
    }


def catalog() -> list[dict[str, object]]:
    artwork: list[dict[str, object]] = []
    for species in Species:
        sprite = species_sprite(species)
        idle, palette = sprite.frames, sprite.palette
        frames: list[list[int]] = []
        for index in range(FRAME_COUNT):
            raw = idle[index] if index < IDLE_FRAME_COUNT else _build_petting_frame(idle, index)
            image = matrix_to_image(raw, palette)
            frames.append(list(image.tobytes()))
        for raw in idle:
            image = matrix_to_image(raw, palette)
            pixels = image.load()
            if pixels is None:
                raise RuntimeError("The buddy sprite has no pixel buffer.")
            _apply_blink(pixels, list(raw), palette)
            frames.append(list(image.tobytes()))
        temperament = _TEMPERAMENTS[species]
        artwork.append(
            {
                "species": species.value,
                "width": PIXEL_WIDTH,
                "height": PIXEL_HEIGHT,
                "frames": frames,
                "restTicks": temperament.rest_ticks,
                "holdTicks": temperament.hold_ticks,
            }
        )
    return artwork


def handle(request: object) -> dict[str, object]:
    if not isinstance(request, dict) or request.get("protocol") != PROTOCOL:
        raise ValueError("Unsupported Buddy frontend protocol")
    action = request.get("action")
    if not isinstance(action, str) or action not in {"status", "catalog", "hatch", "pet", "rename"}:
        raise ValueError("Unsupported Buddy frontend action")
    if request.keys() != ({"protocol", "action", "name"} if action == "rename" else {"protocol", "action"}):
        raise ValueError("Unexpected Buddy frontend fields")
    if action == "catalog":
        return {"protocol": PROTOCOL, "artwork": catalog()}
    store = BuddyStore(strict=True)
    if action == "status":
        record = store.load()
    elif action == "hatch":
        record = store.update(lambda current: current if current is not None else hatchling(SystemRandom()))
    else:
        name = request.get("name")
        if action == "rename" and (not isinstance(name, str) or not clean_name(name)):
            raise ValueError("Buddy name must be nonempty")

        def edit(current: BuddyRecord | None) -> BuddyRecord:
            if current is None:
                raise ValueError("Hatch a Buddy first")
            if action == "pet":
                if current.muted:
                    raise ValueError("Buddy is muted")
                return replace(current, pets=current.pets + 1)
            if not isinstance(name, str):
                raise ValueError("Buddy name must be text")
            return replace(current, name=clean_name(name))

        record = store.update(edit)
    return {"protocol": PROTOCOL, "buddy": snapshot(record)}
