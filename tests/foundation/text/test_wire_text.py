# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A provider request body whose strings all encode as strict UTF-8."""

from __future__ import annotations

import json
from typing import Any

import pytest

from chrys.foundation.text.wire_text import surrogate_safe_value


def test_a_lone_surrogate_becomes_its_escape_at_every_depth_and_reads_back() -> None:
    value: dict[Any, Any] = {
        "model": "m",
        "messages": [{"role": "user", "content": "bad \udcff byte"}, ("\udc80key", {"text": "a\udcfeb"})],
        "nothing_to_do": 3,
    }

    safe = surrogate_safe_value(value)

    assert safe == {
        "model": "m",
        "messages": [{"role": "user", "content": "bad \\udcff byte"}, ("\\udc80key", {"text": "a\\udcfeb"})],
        "nothing_to_do": 3,
    }
    # What the provider SDK does with it: strict UTF-8 must not raise, and the
    # model reads each escape back as the plain text it now is.
    body = json.dumps(safe, ensure_ascii=False).encode("utf-8")
    messages = json.loads(body)["messages"]
    assert messages[0]["content"] == "bad \\udcff byte"
    assert messages[1] == ["\\udc80key", {"text": "a\\udcfeb"}]


def test_text_that_encodes_comes_back_as_the_same_object_character_for_character() -> None:
    text = "北京 🌼 👨‍👩‍👧 é ẹ́"
    value = {"messages": [{"role": "user", "content": text}], "tools": [{"name": "t"}]}

    safe = surrogate_safe_value(value)

    assert safe is value
    assert safe["messages"][0]["content"] is text


def test_values_json_does_not_write_as_text_are_left_alone() -> None:
    class Marker:
        pass

    marker = Marker()
    payload = b"\xff\xfe"
    value = {"marker": marker, "payload": payload, "count": 2, "ratio": 0.5, "missing": None}

    assert surrogate_safe_value(value) is value


def test_a_rebuilt_container_keeps_what_needed_no_repair() -> None:
    nested = {"kept": "fine"}
    value = {"changed": "bad \udcff", "nested": nested}

    safe = surrogate_safe_value(value)

    assert safe is not value
    assert safe["nested"] is nested


def test_two_keys_that_escape_to_the_same_key_are_refused() -> None:
    # A lossy repair would send a schema missing a property, which is worse than a local error.
    with pytest.raises(ValueError, match="merge two entries into one"):
        surrogate_safe_value({"\udcff": "a", "\\udcff": "b"})
