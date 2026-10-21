# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the conversation-selections wire span the mention scanner relies on."""

from __future__ import annotations

from chrys.foundation.text.conversation_selections import (
    Quote,
    compose_outgoing_text,
    selections_span,
    split_selections_block,
)


def test_selections_span_slices_rejoin_byte_for_byte() -> None:
    text = compose_outgoing_text("body text", [Quote("quoted @file.png", "assistant")])

    body, tail = selections_span(text)

    assert body + tail == text
    assert body == "body text"
    assert tail.startswith("\n\n# Conversation selections:")


def test_selections_span_without_a_block_is_all_body() -> None:
    plain = "plain text\n\n```python\nx = 1\n```"

    assert selections_span(plain) == (plain, "")


def test_split_still_fails_open_on_a_malformed_block() -> None:
    text = "keep this\n\n# Conversation selections:\n```chrys-conversation-selections\nnot json\n```"

    assert split_selections_block(text) == (text, [])
