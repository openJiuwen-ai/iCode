# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""OpenAI schemas that both wire protocols must send verbatim with strict omitted."""

import pytest


def non_strict_schema_cases() -> pytest.MarkDecorator:
    return pytest.mark.parametrize(
        ("title", "raw"),
        [
            pytest.param(
                "Open",
                {"type": "object", "properties": {"a": {"type": "string"}}, "additionalProperties": True},
                id="open-object",
            ),
            pytest.param(
                "Open",
                {
                    "type": "object",
                    "properties": {"inner": {"type": "object", "properties": {}, "additionalProperties": True}},
                },
                id="open-nested-object",
            ),
            pytest.param(
                "Open",
                {"type": "object", "properties": {}, "additionalProperties": {"type": "string"}},
                id="schema-additional-properties",
            ),
            pytest.param(
                "Branch",
                {
                    "type": "object",
                    "properties": {
                        "choice": {"oneOf": [{"type": "object", "properties": {"value": {"type": "string"}}}]}
                    },
                },
                id="one-of",
            ),
            pytest.param(
                "Branch",
                {
                    "type": "object",
                    "properties": {
                        "x": {"allOf": [{"type": "object", "properties": {}}, {"type": "object", "properties": {}}]}
                    },
                },
                id="all-of",
            ),
            pytest.param(
                "Branch",
                {"type": "object", "properties": {}, "patternProperties": {"^x": {"type": "object", "properties": {}}}},
                id="pattern-properties",
            ),
            pytest.param(
                "Branch",
                {"type": "object", "properties": {"a": {"type": "string"}}, "dependentRequired": {"a": ["b"]}},
                id="dependent-required",
            ),
            pytest.param(
                "Branch",
                {
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                    "dependentSchemas": {"a": {"type": "string"}},
                },
                id="dependent-schemas",
            ),
            pytest.param(
                "Branch",
                {
                    "type": "object",
                    "properties": {},
                    "patternProperties": {
                        "^x": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "additionalProperties": False,
                        }
                    },
                },
                id="missing-required",
            ),
            pytest.param(
                "Branch",
                {
                    "type": "object",
                    "properties": {},
                    "patternProperties": {
                        "^x": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "additionalProperties": False,
                            "required": ["value", "value"],
                        }
                    },
                },
                id="duplicate-required",
            ),
            pytest.param(
                "Branch",
                {
                    "type": "object",
                    "properties": {},
                    "patternProperties": {
                        "^x": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "additionalProperties": False,
                            "required": [["value"]],
                        }
                    },
                },
                id="unhashable-required",
            ),
            pytest.param("Root", {"type": "string"}, id="scalar-root"),
            pytest.param(
                "Root",
                {"type": "object", "properties": {}, "anyOf": [{"type": "object", "properties": {}}]},
                id="object-root-any-of",
            ),
            pytest.param(
                "Root",
                {"anyOf": [{"type": "object", "properties": {}}, {"type": "object", "properties": {}}]},
                id="untyped-root-any-of",
            ),
        ],
    )
