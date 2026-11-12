# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""`dataset validate` catches checks that could never judge anything."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chrys.service.rsi import validate_dataset_file


def _validate(tmp_path: Path, expect: dict[str, object]) -> dict[str, object]:
    path = tmp_path / "dataset.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "checks",
                "cases": [
                    {
                        "case_id": "c1",
                        "prompt": "Do the thing",
                        "evaluation": {"method": "script_based", "expect": expect},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return validate_dataset_file(path)


@pytest.mark.parametrize(
    ("expect", "reason"),
    [
        ({"response_regex": ["(unclosed"]}, "response_regex"),
        ({"response_contains": [""]}, "must not be empty"),
        ({"response_regex": [""]}, "must not be empty"),
        ({"file_contains": {"a.txt": [""]}}, "must not be empty"),
        ({"files_exist": ["a\\b.txt", "a/b.txt"]}, "twice"),
        ({"file_contains": {"a\\b.txt": ["x"], "a/b.txt": ["y"]}}, "twice"),
        ({"file_equals": {"a\\b.txt": "x", "a/b.txt": "y"}}, "twice"),
    ],
)
def test_validate_rejects_checks_that_cannot_judge(tmp_path: Path, expect: dict[str, object], reason: str) -> None:
    result = _validate(tmp_path, expect)
    assert result["valid"] is False
    errors = result["errors"]
    assert isinstance(errors, list)
    assert reason in errors[0]["reason"]


def test_validate_accepts_well_formed_checks(tmp_path: Path) -> None:
    result = _validate(
        tmp_path,
        {
            "response_regex": [r"\bok\b"],
            "response_contains": ["ok"],
            "files_exist": ["a/b.txt", "a/c.txt"],
            "file_contains": {"a/b.txt": ["x"]},
        },
    )
    assert result["valid"] is True
