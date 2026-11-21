# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from icode_browser_use.errors import BadRequest
from icode_browser_use.validation import to_json_schema, validate


def test_defaults_and_known_keys() -> None:
    params = {"a": {"type": "string", "required": True}, "b": {"type": "integer", "default": 3}}
    assert validate(params, {"a": "x"}) == {"a": "x", "b": 3}


def test_missing_required() -> None:
    with pytest.raises(BadRequest):
        validate({"a": {"type": "string", "required": True}}, {})


def test_unknown_parameter() -> None:
    with pytest.raises(BadRequest):
        validate({"a": {"type": "string"}}, {"b": "x"})


def test_non_object_arguments() -> None:
    with pytest.raises(BadRequest):
        validate({"a": {"type": "string"}}, ["x"])


def test_enum() -> None:
    with pytest.raises(BadRequest):
        validate({"mode": {"enum": ["a", "b"]}}, {"mode": "c"})


def test_integer_bounds_and_bool_rejection() -> None:
    with pytest.raises(BadRequest):
        validate({"n": {"type": "integer", "min": 1, "max": 3}}, {"n": 4})
    with pytest.raises(BadRequest):
        validate({"n": {"type": "integer"}}, {"n": True})


def test_string_type_and_pattern() -> None:
    with pytest.raises(BadRequest):
        validate({"s": {"type": "string"}}, {"s": 1})
    with pytest.raises(BadRequest):
        validate({"s": {"type": "string", "pattern": "^[a-z]+$"}}, {"s": "A"})


def test_boolean_is_strict() -> None:
    with pytest.raises(BadRequest):
        validate({"b": {"type": "boolean"}}, {"b": "true"})


def test_array_items_and_bounds() -> None:
    params = {"values": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 2}}
    assert validate(params, {"values": ["a"]}) == {"values": ["a"]}
    assert validate(params, {"values": ["a", "b"]}) == {"values": ["a", "b"]}
    for bad in ({"values": "a"}, {"values": []}, {"values": ["a", "b", "c"]}, {"values": [1]}):
        with pytest.raises(BadRequest):
            validate(params, bad)


def test_array_schema_keeps_items_verbatim() -> None:
    schema = to_json_schema({"values": {"type": "array", "items": {"type": "string"}, "minItems": 1}})
    assert schema["properties"]["values"] == {"type": "array", "items": {"type": "string"}, "minItems": 1}


def test_array_item_max_length() -> None:
    params = {"values": {"type": "array", "items": {"type": "string", "maxLength": 3}, "minItems": 1}}
    assert validate(params, {"values": ["abc"]}) == {"values": ["abc"]}
    with pytest.raises(BadRequest):
        validate(params, {"values": ["abcd"]})


def test_scalar_max_length_is_enforced_and_projected() -> None:
    params = {"text": {"type": "string", "default": "", "maxLength": 3}}
    assert validate(params, {"text": "abc"}) == {"text": "abc"}
    assert validate(params, {}) == {"text": ""}
    with pytest.raises(BadRequest):
        validate(params, {"text": "abcd"})
    assert to_json_schema(params)["properties"]["text"]["maxLength"] == 3


def test_json_schema_uses_standard_bounds_without_mutating_specs() -> None:
    params = {"n": {"type": "integer", "min": 0, "max": 3, "default": 1}}
    schema = to_json_schema(params)
    assert schema["properties"]["n"] == {
        "type": "integer",
        "minimum": 0,
        "maximum": 3,
        "default": 1,
    }
    assert params["n"]["min"] == 0
    assert params["n"]["max"] == 3
    assert "required" not in schema
    assert validate(params, {"n": 0}) == {"n": 0}
    assert validate(params, {"n": 3}) == {"n": 3}
    with pytest.raises(BadRequest):
        validate(params, {"n": -1})


def test_json_schema_marks_required() -> None:
    schema = to_json_schema({"a": {"type": "string", "required": True}, "b": {"type": "integer", "default": 1}})
    assert schema["required"] == ["a"]
    assert schema["properties"]["b"] == {"type": "integer", "default": 1}
