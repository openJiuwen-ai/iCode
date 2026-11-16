# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JSON-safe serialization fallbacks and reconstruction input ownership."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import ClassVar

import pytest

from chrys.kernel._serialization import SerializationMixin, make_json_safe


class _InjectedOptions(SerializationMixin):
    TYPE: ClassVar[str] = "test_injected_options"
    INJECTABLE: ClassVar[set[str]] = {"options"}

    def __init__(self, name: str, options: dict[str, int]) -> None:
        self.name = name
        self.options = options


@pytest.mark.parametrize("instance_specific", [False, True], ids=["type_dependency", "instance_dependency"])
def test_from_dict_dependency_merge_keeps_reusable_inputs_unchanged(instance_specific: bool) -> None:
    value = {"type": _InjectedOptions.TYPE, "name": "agent", "options": {"timeout": 10, "keep": 1}}
    injected = {"timeout": 20, "retries": 3}
    type_dependencies = {"name:agent": {"options": injected}} if instance_specific else {"options": injected}
    dependencies = {_InjectedOptions.TYPE: type_dependencies}
    original_value = deepcopy(value)
    original_dependencies = deepcopy(dependencies)

    first = _InjectedOptions.from_dict(value, dependencies=dependencies)
    assert first.options == {"timeout": 20, "keep": 1, "retries": 3}
    assert value == original_value
    assert dependencies == original_dependencies

    second = _InjectedOptions.from_dict(value)
    assert second.options == {"timeout": 10, "keep": 1}
    first.options["timeout"] = 99
    assert value == original_value
    assert dependencies == original_dependencies


def test_make_json_safe_serializes_dataclass_via_asdict() -> None:
    @dataclass
    class _Record:
        label: str
        count: int

    assert make_json_safe(_Record(label="ready", count=2)) == {"label": "ready", "count": 2}


class _ModelDumpObject:
    def model_dump(self) -> dict[str, str]:
        return {"source": "model_dump"}


class _ToDictObject:
    def to_dict(self) -> dict[str, str]:
        return {"source": "to_dict"}


class _DictObject:
    def dict(self) -> dict[str, str]:
        return {"source": "dict"}


@pytest.mark.parametrize(
    ("value", "source"),
    [
        (_ModelDumpObject(), "model_dump"),
        (_ToDictObject(), "to_dict"),
        (_DictObject(), "dict"),
    ],
)
def test_make_json_safe_uses_supported_conversion_method(value: object, source: str) -> None:
    assert make_json_safe(value) == {"source": source}


def test_make_json_safe_broken_conversion_methods_fall_through_silently() -> None:
    class _BrokenMethods:
        def __init__(self) -> None:
            self.value = "fallback"

        def model_dump(self) -> dict[str, object]:
            raise TypeError("broken model_dump")

        def to_dict(self) -> dict[str, object]:
            raise TypeError("broken to_dict")

        def dict(self) -> dict[str, object]:
            raise TypeError("broken dict")

    assert make_json_safe(_BrokenMethods()) == {"value": "fallback"}


class _Color(Enum):
    RED = "red"
    PAIR = ("a", 1)


class _Level(IntEnum):
    HIGH = 3


def test_make_json_safe_serializes_an_enum_member_as_its_value() -> None:
    assert make_json_safe(_Color.RED) == "red"
    assert make_json_safe({"color": _Color.RED, "pair": _Color.PAIR}) == {"color": "red", "pair": ["a", 1]}
    # A member that is already an int stays itself, which JSON writes as 3.
    assert json.dumps(make_json_safe(_Level.HIGH)) == "3"


def test_make_json_safe_serializes_frozenset_deterministically() -> None:
    assert make_json_safe(frozenset({"beta", "alpha"})) == ["alpha", "beta"]


def test_make_json_safe_depth_cap_terminates_self_referencing_mapping() -> None:
    value: dict[str, object] = {}
    value["self"] = value

    safe = make_json_safe(value)

    assert isinstance(safe, dict)
    json.dumps(safe)


def test_make_json_safe_uses_vars_as_final_object_fallback() -> None:
    class _PlainObject:
        def __init__(self) -> None:
            self.label = "plain"
            self.count = 3

    assert make_json_safe(_PlainObject()) == {"label": "plain", "count": 3}
