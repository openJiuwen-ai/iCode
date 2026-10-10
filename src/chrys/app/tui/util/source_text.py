# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Display-only normalization and control-character filtering for literal source text."""

from __future__ import annotations

from chrys.foundation.i18n.formatting import sanitize_terminal_block

# Bidi controls can show text in another order than it is stored, and the other
# format characters and separators here take no column, so they hide text (tag
# characters can spell out a whole message nobody sees). ZWJ and ZWNJ are left
# out, since scripts and emoji need them.
BIDI_AND_ZERO_WIDTH_CODEPOINTS: tuple[int, ...] = (
    0x061C,
    0x180E,
    0x200B,
    0x200E,
    0x200F,
    *range(0x2028, 0x202F),
    *range(0x2060, 0x2065),
    *range(0x2066, 0x2070),
    0xFEFF,
    *range(0xFFF9, 0xFFFC),
    *range(0x13430, 0x13440),
    *range(0x1BCA0, 0x1BCA4),
    *range(0x1D173, 0x1D17B),
    0xE0001,
    *range(0xE0020, 0xE0080),
)
_HIDDEN_FORMAT_MARKS = dict.fromkeys(BIDI_AND_ZERO_WIDTH_CODEPOINTS, "\ufffd")


def sanitize_source_text(source: str, *, tab_size: int = 4) -> str:
    """Preserve line breaks and indentation before replacing C0/C1 and DEL controls."""
    return sanitize_terminal_block(source).expandtabs(tab_size)


def mark_hidden_format(text: str) -> str:
    """Show each character of ``BIDI_AND_ZERO_WIDTH_CODEPOINTS`` in *text* as U+FFFD, one for one."""
    return text.translate(_HIDDEN_FORMAT_MARKS)
