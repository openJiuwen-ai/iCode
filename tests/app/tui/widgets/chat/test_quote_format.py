# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the conversation-quote wire format and bubble view."""

from __future__ import annotations

import json

from chrys.app.tui.widgets.chat.messages import compose_user_message_view
from chrys.app.tui.widgets.chat.quote_format import (
    MAX_PENDING_QUOTES,
    SINGLE_QUOTE_MAX_CHARS,
    SOURCE_ASSISTANT,
    SOURCE_USER,
    TOTAL_QUOTE_MAX_CHARS,
    Quote,
    compose_outgoing_text,
    quote_preview,
    source_label_reference,
    split_selections_block,
)
from chrys.foundation.i18n.formatting import format_message


def test_compose_appends_one_block_after_a_blank_line() -> None:
    wire = compose_outgoing_text("explain this", [Quote(text="selected text", source=SOURCE_ASSISTANT)])

    assert wire == (
        "explain this\n\n"
        "# Conversation selections:\n"
        "```chrys-conversation-selections\n"
        '[{"source": "assistant", "text": "selected text"}]\n'
        "```"
    )


def test_compose_without_quotes_returns_text_untouched() -> None:
    assert compose_outgoing_text("  keep spacing  \n", []) == "  keep spacing  \n"


def test_compose_with_empty_typed_text_is_only_the_block() -> None:
    wire = compose_outgoing_text("  \n", [Quote(text="only a quote")])

    assert wire.startswith("# Conversation selections:\n")
    assert wire.endswith("\n```")
    visible, quotes = split_selections_block(wire)
    assert visible == ""
    assert quotes == [Quote(text="only a quote")]


def test_compose_parse_roundtrip_multiple_quotes_and_unicode() -> None:
    quotes = [
        Quote(text="多行\n引用 **文本**", source=SOURCE_ASSISTANT),
        Quote(text="plain 'ascii' \"text\"", source=SOURCE_USER),
        Quote(text="no source"),
    ]

    visible, parsed = split_selections_block(compose_outgoing_text("typed", quotes))

    assert visible == "typed"
    assert parsed == quotes


def test_split_returns_text_unchanged_without_a_trailing_block() -> None:
    text = "# Conversation selections:\n```chrys-conversation-selections\nbut not trailing"

    assert split_selections_block(text) == (text, [])
    assert split_selections_block("") == ("", [])


def test_split_keeps_malformed_blocks_as_plain_text() -> None:
    header = "# Conversation selections:\n```chrys-conversation-selections\n"

    for payload in ("not json", "[]", "{}", '[{"unknown": 1}]', '[{"text": 3}]', '[{"text": "a", "source": "x"}]'):
        text = f"typed\n\n{header}{payload}\n```"
        assert split_selections_block(text) == (text, []), payload


def test_split_accepts_entries_with_extra_whitespace_after_the_fence() -> None:
    text = "typed\n\n# Conversation selections:\n```chrys-conversation-selections\n[]\n```   "
    # An empty array is treated as no block: nothing quotable was carried.
    assert split_selections_block(text) == (text, [])


def test_wire_payload_is_valid_json_with_expected_fields() -> None:
    wire = compose_outgoing_text("t", [Quote(text="a", source=SOURCE_USER), Quote(text="b")])

    payload = json.loads(wire.split("```chrys-conversation-selections\n", 1)[1].rsplit("\n```", 1)[0])
    assert payload == [{"source": "user", "text": "a"}, {"text": "b"}]


def test_quote_limit_constants_match_the_product_budgets() -> None:
    assert SINGLE_QUOTE_MAX_CHARS == 8_000
    assert MAX_PENDING_QUOTES == 8
    assert TOTAL_QUOTE_MAX_CHARS == 16_000


def test_quote_preview_collapses_whitespace_and_caps_length() -> None:
    assert quote_preview("  a   b  ") == "a b"
    assert quote_preview("x" * 100, max_chars=10) == "x" * 9 + "…"
    long_cjk = "字" * 100
    assert len(quote_preview(long_cjk)) <= 81


def test_source_label_reference_covers_every_wire_source() -> None:
    assert "User" in format_message(source_label_reference(SOURCE_USER))
    assert "Assistant" in format_message(source_label_reference(SOURCE_ASSISTANT))
    assert format_message(source_label_reference(None))
    assert format_message(source_label_reference("mystery"))


def test_compose_user_message_view_without_quotes_is_sanitized_text() -> None:
    view = compose_user_message_view("plain text", [], format_message)

    assert view == "plain text"


def test_compose_user_message_view_renders_quotes_as_blockquotes() -> None:
    view = compose_user_message_view(
        "typed",
        [Quote(text="line one\nline two", source=SOURCE_ASSISTANT), Quote(text="solo")],
        format_message,
    )

    assert view.startswith("typed\n\n")
    assert "> **Assistant message**\n> line one\n> line two" in view
    assert "> **Conversation**\n> solo" in view
    # The wire block never leaks into the rendered view.
    assert "chrys-conversation-selections" not in view


def test_compose_user_message_view_with_empty_text_leads_with_the_title() -> None:
    view = compose_user_message_view("", [Quote(text="only")], format_message)

    assert view.startswith("**Quoted conversation**")
    assert "> only" in view
