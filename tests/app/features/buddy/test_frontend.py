# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Local Buddy frontend uses the shared signed save and refuses damaged storage."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from chrys.app.features.buddy.frontend import PROTOCOL, handle
from chrys.app.features.buddy.model import Species
from chrys.app.features.buddy.pixel_sprites import build_pixel_frame
from chrys.app.features.buddy.store import BuddyStore

if TYPE_CHECKING:
    from pathlib import Path


def test_frontend_hatches_pets_renames_and_shares_tui_storage() -> None:
    assert handle({"protocol": PROTOCOL, "action": "status"})["buddy"] is None
    result = handle({"protocol": PROTOCOL, "action": "hatch"})
    store = BuddyStore()
    record = store.load()
    assert record is not None
    assert result["buddy"]["record"] == record.to_json()
    store.update(lambda current: replace(current, turns=10))
    after = handle({"protocol": PROTOCOL, "action": "pet"})["buddy"]
    assert after["record"]["turns"] == 10
    assert after["record"]["pets"] == 1
    assert after["level"] > 1
    handle({"protocol": PROTOCOL, "action": "rename", "name": "栗子"})
    assert store.load().name == "栗子"
    assert handle({"protocol": PROTOCOL, "action": "hatch"})["buddy"]["record"]["name"] == "栗子"


def test_frontend_catalog_contains_complete_base_pet_and_blink_frames() -> None:
    artwork = handle({"protocol": PROTOCOL, "action": "catalog"})["artwork"]
    assert {item["species"] for item in artwork} == {species.value for species in Species}
    for item in artwork:
        assert (item["width"], item["height"]) == (20, 16)
        assert len(item["frames"]) == 9
        assert all(len(frame) == 1280 for frame in item["frames"])
        assert item["frames"][0] != item["frames"][6]
        species = Species(item["species"])
        for index, frame in enumerate(item["frames"]):
            assert frame == list(
                build_pixel_frame(species, index if index < 6 else index - 6, blink=index >= 6).tobytes()
            )


def test_frontend_rejects_corruption_even_with_valid_backup() -> None:
    handle({"protocol": PROTOCOL, "action": "hatch"})
    store = BuddyStore()
    record = store.load()
    assert record is not None
    # Use the regular store to establish the backup used by the TUI.
    store.update(lambda current: replace(current, turns=1))
    before = store.backup_path.read_bytes()
    store.path.write_text("broken", encoding="utf-8")
    for action in ("status", "hatch", "pet"):
        with pytest.raises(ValueError, match="Invalid Buddy save"):
            handle({"protocol": PROTOCOL, "action": action})
    assert store.path.read_text(encoding="utf-8") == "broken"
    assert store.backup_path.read_bytes() == before
    store.path.unlink()
    with pytest.raises(ValueError, match="primary save is missing"):
        handle({"protocol": PROTOCOL, "action": "status"})


def test_frontend_propagates_permission_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    def denied(self: Path, *, encoding: str) -> str:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(PermissionError, match="denied"):
        handle({"protocol": PROTOCOL, "action": "status"})


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"protocol": "v0", "action": "status"},
        {"protocol": PROTOCOL, "action": "status", "species": "new"},
        {"protocol": PROTOCOL, "action": "turn"},
        {"protocol": PROTOCOL, "action": []},
        {"protocol": PROTOCOL, "action": {}},
    ],
)
def test_frontend_rejects_unknown_contract_and_fields(payload: object) -> None:
    with pytest.raises(ValueError):
        handle(payload)
