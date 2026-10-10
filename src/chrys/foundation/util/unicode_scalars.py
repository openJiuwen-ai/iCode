# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unpaired-surrogate detection and repair for text that must cross a JSON wire."""

from __future__ import annotations


def find_unpaired_surrogate(text: str) -> int:
    """Return the index of the first unpaired surrogate in *text*, or ``-1``.

    A high surrogate directly followed by a low surrogate counts as a pair
    (JSON encoders emit and decoders re-join it); anything else in the
    surrogate range is unpaired.
    """
    # Text with no surrogate at all, nearly all text, encodes; the encoder
    # checks it far faster than the scan below.
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        pass
    else:
        return -1
    index = 0
    length = len(text)
    while index < length:
        codepoint = ord(text[index])
        if 0xD800 <= codepoint <= 0xDBFF:
            if index + 1 >= length or not 0xDC00 <= ord(text[index + 1]) <= 0xDFFF:
                return index
            index += 2
            continue
        if 0xDC00 <= codepoint <= 0xDFFF:
            return index
        index += 1
    return -1


def replace_unpaired_surrogates(text: str) -> str:
    """Join surrogate pairs into their scalar and replace lone ones with U+FFFD."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return text
