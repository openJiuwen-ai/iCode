# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A draft for a model without vision: every image it would send becomes a plain path, a folded paste's mentions too."""

from __future__ import annotations

from pathlib import Path

from chrys.app.tui.screens.main.event_handlers import _with_image_paths
from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft, ShortcutIntent
from chrys.foundation.text.draft_references import EMPTY_REFERENCES
from tests.support.tui_helpers import png_bytes


def test_every_image_a_draft_sends_becomes_a_plain_path(tmp_path: Path) -> None:
    for name in ("label.png", "typed.png", "pasted.png"):
        (tmp_path / name).write_bytes(png_bytes((1, 2, 3)))
    references, _ = EMPTY_REFERENCES.with_new("image", str(tmp_path / "label.png"))
    references, _ = references.with_new("quote", "see @pasted.png")
    references, _ = references.with_new("paste", "look at @pasted.png\nand [Image #1]\n.\n.")
    owner = ShortcutIntent("look", "", "")
    draft = ChatDraft("[Image #1] @typed.png [Quote #1] [Pasted text #1 +3 lines]", owner=owner, references=references)

    converted = _with_image_paths(draft, tmp_path)

    assert converted.text == f"{tmp_path / 'label.png'} {tmp_path / 'typed.png'} [Quote #1] [Pasted text #1 +3 lines]"
    paste = converted.references.get("paste", 1)
    assert paste is not None
    # Label text in a paste is just text (one level); its typed image mention attaches, so it becomes a path.
    assert paste.content == f"look at {tmp_path / 'pasted.png'}\nand [Image #1]\n.\n."
    # A quote attaches nothing, so it is left as it was; the owner stays.
    assert converted.references.get("quote", 1) == references.get("quote", 1)
    assert converted.owner == owner
    assert "@" not in converted.sent_text().replace("\\@", "")
    assert converted.references.get("paste", 1).label == references.get("paste", 1).label  # type: ignore[union-attr]


def test_a_draft_without_images_is_left_as_it_is(tmp_path: Path) -> None:
    references, _ = EMPTY_REFERENCES.with_new("paste", "plain\ntext\nonly\nhere")
    draft = ChatDraft("hello [Pasted text #1 +3 lines]", references=references)

    assert _with_image_paths(draft, tmp_path) == draft
