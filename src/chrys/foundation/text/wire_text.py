# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A provider request body with every string in it safe to encode as strict UTF-8.

A model's request leaves as JSON with ``ensure_ascii=False`` and is then encoded
as UTF-8, so every string in the body must encode strictly. A lone surrogate — a
``\\udcXX`` that ``json.loads`` produced from an MCP server's own JSON text, an
argv value the shell handed over undecodable, a file read with
``surrogateescape`` — is a legal ``str`` that strict UTF-8 refuses, and the whole
request fails before a byte of it leaves.

:func:`surrogate_safe_value` rewrites exactly those strings, and only in the
copy that goes on the wire: what the conversation keeps stays as it was, since
the raw value is the only thing that round-trips to the original bytes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from chrys.foundation.platform.files import is_utf8_encodable, surrogate_safe_text


def surrogate_safe_value(value: Any) -> Any:
    """*value* with every string in it — a dict's keys too — safe for a strict UTF-8 body.

    A string that already encodes is left alone, so a body without lone
    surrogates comes back as the same object and nothing is copied. One that
    does not is spelled the way :func:`~chrys.foundation.platform.files.surrogate_safe_text`
    spells it: each lone surrogate becomes its visible ``\\udcXX`` escape, which
    is what :func:`~chrys.foundation.text.model_json.model_json` already does for
    tool arguments. Valid text is untouched, astral characters and combining
    marks included, and a value JSON does not write as a string (a class,
    ``bytes``, a number) is returned as it is.

    Raising ``ValueError`` is the one thing left to a mapping whose keys would
    merge: two keys that differ only by a lone surrogate become one entry once
    escaped, and sending the object would silently drop one of them.
    """
    if isinstance(value, str):
        return value if is_utf8_encodable(value) else surrogate_safe_text(value)
    if isinstance(value, dict):
        rebuilt_items = {surrogate_safe_value(key): surrogate_safe_value(item) for key, item in value.items()}
        if len(rebuilt_items) != len(value):
            message = (
                "Two keys of one request object differ only by a lone surrogate, "
                "so escaping them would merge two entries into one."
            )
            raise ValueError(message)
        return value if _same_mapping(rebuilt_items, value) else rebuilt_items
    if isinstance(value, list):
        rebuilt_list = [surrogate_safe_value(item) for item in value]
        return value if _same_sequence(rebuilt_list, value) else rebuilt_list
    if isinstance(value, tuple):
        rebuilt_tuple = tuple(surrogate_safe_value(item) for item in value)
        return value if _same_sequence(rebuilt_tuple, value) else rebuilt_tuple
    return value


def _same_mapping(rebuilt: dict[Any, Any], original: dict[Any, Any]) -> bool:
    """Whether *rebuilt* holds the very objects *original* holds, key for key."""
    if rebuilt.keys() != original.keys():
        return False
    return all(rebuilt[key] is original[key] for key in original)


def _same_sequence(rebuilt: Sequence[Any], original: Sequence[Any]) -> bool:
    """Whether *rebuilt* holds the very objects *original* holds, position for position."""
    return len(rebuilt) == len(original) and all(new is old for new, old in zip(rebuilt, original, strict=True))
