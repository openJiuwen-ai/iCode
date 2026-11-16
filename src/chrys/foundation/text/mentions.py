# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared helpers for parsing and formatting user-authored ``@file`` mentions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_TRAILING_PUNCTUATION = ".,;:!?)]}>"
_UNQUOTED_MENTION_RE = re.compile(r"@[^\s]+")


@dataclass(frozen=True)
class MentionToken:
    """A parsed ``@`` mention plus the unescaped value it refers to."""

    mention: str
    value: str
    start: int
    end: int


def iter_mention_tokens(text: str) -> list[MentionToken]:
    """Return mention tokens using Chrys' user-message ``@file`` parsing rules."""
    tokens: list[MentionToken] = []
    length = len(text)
    i = 0
    while i < length:
        if text[i] != "@" or (i > 0 and not text[i - 1].isspace()):
            i += 1
            continue
        parsed = _parse_mention_at(text, i)
        if parsed is None:
            i += 1
            continue
        token, end = parsed
        tokens.append(token)
        i = end
    return tokens


def format_file_mention(path: str | Path) -> str:
    """Return the canonical quoted ``@file`` mention for *path*."""
    escaped = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'@"{escaped}"'


def _parse_mention_at(text: str, start: int) -> tuple[MentionToken, int] | None:
    value_start = start + 1
    if value_start >= len(text):
        return None

    quote = text[value_start]
    if quote in ("'", '"'):
        return _parse_quoted_mention(text, start, quote)
    return _parse_unquoted_mention(text, start)


def _parse_quoted_mention(text: str, start: int, quote: str) -> tuple[MentionToken, int] | None:
    value_start = start + 2
    chars: list[str] = []
    i = value_start
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text) and text[i + 1] in (quote, "\\"):
            chars.append(text[i + 1])
            i += 2
            continue
        if char == quote:
            value = "".join(chars).strip()
            if not value:
                return None
            end = i + 1
            return MentionToken(mention=text[start:end], value=value, start=start, end=end), end
        chars.append(char)
        i += 1
    return None


def _parse_unquoted_mention(text: str, start: int) -> tuple[MentionToken, int] | None:
    match = _UNQUOTED_MENTION_RE.match(text, start)
    if match is None:
        return None
    raw = match.group(0)
    stripped = raw.rstrip(_TRAILING_PUNCTUATION)
    if stripped == "@":
        return None
    end = start + len(stripped)
    return MentionToken(mention=stripped, value=stripped[1:], start=start, end=end), end
