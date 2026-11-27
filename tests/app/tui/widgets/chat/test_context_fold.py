# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The context fold shows its summary as display text, never as terminal control."""

from __future__ import annotations

from chrys.app.tui.widgets.chat.context_fold import ContextFoldWidget


def test_the_fold_summary_shows_control_characters_as_text() -> None:
    summary = "## Progress\r\n- read \x1b[2Jfiles\tok\x9b"

    rendered = ContextFoldWidget("ctx-1", summary).render().plain

    assert "\x1b" not in rendered
    assert "\x9b" not in rendered
    assert "\r" not in rendered
    assert "\t" not in rendered
    # The summary keeps its own line breaks; every other control is replaced.
    assert rendered.endswith("\n  ## Progress�\n- read �[2Jfiles�ok�")
