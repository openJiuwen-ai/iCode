# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What the side question card shows, read the way its tests read it."""

from __future__ import annotations

from typing import TYPE_CHECKING

from chrys.app.tui.widgets.btw import BtwAnswer, BtwQuestion

if TYPE_CHECKING:
    from chrys.app.tui.widgets.btw import BtwPanel

CLOSE_GLYPH = "✕"


def shown_question(panel: BtwPanel) -> str:
    """The question the card shows; empty when it shows none."""
    shown = [message for message in panel.query(BtwQuestion) if not message._pruning]
    return shown[-1].text if shown else ""


def shown_answer(panel: BtwPanel) -> str | None:
    """The answer the card shows; None when it shows none, or the one it has is still parsing."""
    shown = [message for message in panel.query(BtwAnswer) if not message._pruning and message.display]
    return shown[-1].text if shown else None
