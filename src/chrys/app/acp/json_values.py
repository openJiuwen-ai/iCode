# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Turn free-form Python values into plain JSON data for the ACP wire.

The ACP SDK encodes every outgoing message with ``json.dumps`` (a model's
fields are dumped in Python mode first), so one value JSON cannot encode fails
the send, and a failed send inside a prompt fails the whole turn. Typed event
fields are JSON by construction. Fields typed ``Any`` are not: tool arguments
come back from validation as whatever the tool's parameter types are (a
``datetime``, a ``Path``), sub-agent tool-result metadata carries the mutation
tracker's dataclasses with enum members and the raw paths of failed tool
calls, and profile metadata keeps the dates and sets YAML parsed. Every such
field passes through :func:`to_json_value` on its way into a payload.
In-process events keep the original objects, which the TUI reads by type.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, time
from enum import Enum
from typing import Any

from pydantic import BaseModel

from chrys.foundation.platform.files import surrogate_safe_text

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


def to_json_value(value: object) -> JsonValue:
    """Return *value* as plain JSON data.

    Enum members become their ``value``; dataclasses objects keyed by field
    name, and pydantic models objects as they serialize (by alias where one is
    declared, the keys a tool's argument schema shows); dates and times ISO 8601
    text; paths their ``os.fspath`` text; bytes UTF-8 text with replacement
    characters; tuples and sets arrays (sets in a stable order); mapping keys
    strings; non-finite floats ``null`` (``json.dumps`` would write ``NaN``,
    which JSON parsers reject). In strings, keys included, a surrogate pair
    (YAML reads the escape ``\\ud83d\\ude00`` as one) becomes the character it
    encodes, and an unpaired surrogate (an undecodable byte in a path) the
    ``\\udcXX`` text that :func:`surrogate_safe_text` writes, since strict
    parsers refuse the escape ``json.dumps`` would write for it. Any other
    object becomes ``str(value)``. The
    conversion itself raises only on a cyclic value, but an object's own
    ``__str__`` or ``model_dump`` can.
    """
    if isinstance(value, Enum):
        return to_json_value(value.value)
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, str):
        return _json_text(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return to_json_object(value)
    if isinstance(value, list | tuple):
        return [to_json_value(member) for member in value]
    if isinstance(value, set | frozenset):
        return sorted((to_json_value(member) for member in value), key=repr)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, BaseModel):
        return to_json_value(value.model_dump(by_alias=True))
    if isinstance(value, date | time):
        return value.isoformat()
    if isinstance(value, os.PathLike):
        return to_json_value(os.fspath(value))
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).decode("utf-8", errors="replace")
    return _json_text(str(value))


def to_json_object(value: Mapping[Any, object]) -> dict[str, JsonValue]:
    """Return a mapping as a JSON object, converting keys and values alike."""
    return {_json_key(key): to_json_value(member) for key, member in value.items()}


def _json_text(text: str) -> str:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        # Pairs are joined first: `surrogate_safe_text` spells out every surrogate.
        return surrogate_safe_text(text.encode("utf-16", "surrogatepass").decode("utf-16", "surrogatepass"))
    return text


def _json_key(key: object) -> str:
    plain = to_json_value(key)
    # A converted value always encodes; non-string keys read as their JSON text.
    return plain if isinstance(plain, str) else json.dumps(plain)
