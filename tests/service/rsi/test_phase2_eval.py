# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Phase 2 RSI script_based dataset evaluation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chrys.service.rsi import RsiDatasetInvalid, build_rsi_service_context, load_dataset, validate_dataset_file
from chrys.service.rsi.case_runner import StubCaseRunner
from chrys.service.rsi.script_judge import judge_case

_FIXTURE = Path(__file__).parent / "fixtures" / "demo_dataset.json"


def test_load_and_validate_demo_dataset() -> None:
    dataset = load_dataset(_FIXTURE)
    assert dataset.dataset_id == "rsi-demo-script"
    assert dataset.sample_count == 2
    projection = validate_dataset_file(_FIXTURE)
    assert projection["valid"] is True
    assert projection["sample_count"] == 2


def test_dataset_rejects_non_script_method(tmp_path: Path) -> None:
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "bad",
                "cases": [
                    {
                        "case_id": "x",
                        "prompt": "hi",
                        "evaluation": {"method": "llm_as_judge", "expect": {"response_contains": ["x"]}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RsiDatasetInvalid, match="script_based"):
        load_dataset(path)


def test_judge_declarative_file_and_response(tmp_path: Path) -> None:
    dataset = load_dataset(_FIXTURE)
    case = dataset.cases[0]
    workdir = tmp_path / "w"
    workdir.mkdir()
    (workdir / "hello.txt").write_text("hello world\n", encoding="utf-8")
    result = judge_case(
        case,
        response_text="Created hello.txt with hello world",
        workdir=workdir,
        dataset_dir=dataset.source_path.parent,
    )
    assert result.passed is True
    assert result.score == 1.0


def test_judge_script_check(tmp_path: Path) -> None:
    dataset_dir = tmp_path / "ds"
    dataset_dir.mkdir()
    script = dataset_dir / "check_ok.py"
    script.write_text(
        "import os, pathlib, sys\n"
        "workdir = pathlib.Path(os.environ['CHRYS_RSI_WORKDIR'])\n"
        "sys.exit(0 if (workdir / 'marker.txt').is_file() else 1)\n",
        encoding="utf-8",
    )
    path = dataset_dir / "dataset.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "script-check",
                "cases": [
                    {
                        "case_id": "marked",
                        "prompt": "mark",
                        "evaluation": {
                            "method": "script_based",
                            "script": "check_ok.py",
                            "expect": {},
                        },
                        "fixture": {"response": "ok", "files": {"marker.txt": "1"}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    # expect empty alone is invalid; but script is present — reload after fixing expect optional.
    # Empty expect {} with script is allowed by parser.
    dataset = load_dataset(path)
    case = dataset.cases[0]
    workdir = tmp_path / "case"
    workdir.mkdir()
    (workdir / "marker.txt").write_text("1", encoding="utf-8")
    judged = judge_case(case, response_text="ok", workdir=workdir, dataset_dir=dataset_dir)
    assert judged.passed is True


@pytest.mark.asyncio
async def test_stub_eval_suite_writes_scorecard(tmp_path: Path) -> None:
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    created = ctx.create_task(name="eval-demo", dataset_path=_FIXTURE)
    assert created["input_file"]
    assert Path(created["input_file"]).is_file()

    result = await ctx.evaluate_task(created["task_id"], runner=StubCaseRunner())
    assert result.total_count == 2
    assert result.passed_count == 2
    assert result.score == 1.0
    assert result.scorecard_path.is_file()
    assert result.cases_path.is_file()
    scorecard = json.loads(result.scorecard_path.read_text(encoding="utf-8"))
    assert scorecard["passed"] == 2
    detail = ctx.get_task(created["task_id"])
    assert detail["config"]["input_file"]
    # Tree projection updated during evaluate_task
    tree = ctx.get_tree(created["task_id"])
    types = {node["type"] for node in tree["nodes"]}
    assert "ROOT" in types
    assert "ADOPTED" in types


@pytest.mark.asyncio
async def test_adhoc_eval_dataset_cli_path(tmp_path: Path) -> None:
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    result = await ctx.evaluate_dataset(_FIXTURE, runner=StubCaseRunner(), output_dir=tmp_path / "out")
    assert result.passed_count == 2
    assert (tmp_path / "out" / result.run_id / "scorecard.json").is_file()
