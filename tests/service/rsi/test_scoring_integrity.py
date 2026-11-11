# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Scores compare only what the checks judged, the same way on every path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.rsi import RsiDatasetInvalid, build_rsi_service_context, load_dataset
from chrys.service.rsi.case_runner import CaseRunResult, StubCaseRunner
from chrys.service.rsi.dataset import RsiCase
from chrys.service.rsi.errors import RsiBadRequest
from chrys.service.rsi.evaluator import CaseEvalRecord
from chrys.service.rsi.mutations import AppendInstructions
from chrys.service.rsi.optimizer import MutationProposal, ProposalOutcome
from chrys.service.rsi.snapshot import maybe_update_best_pointer, read_best_pointer

_CHECK_WITH_HELPER = """\
import os
import sys
from pathlib import Path

import helper

root = Path(os.environ["CHRYS_RSI_DATASET_DIR"])
expected = (root / "expected" / "answer.txt").read_text(encoding="utf-8").strip()
response = Path(os.environ["CHRYS_RSI_RESPONSE_PATH"]).read_text(encoding="utf-8")
sys.exit(0 if helper.matches(response, expected) else 1)
"""


def _write_helper_dataset(folder: Path, *, support_files: list[str] | None) -> Path:
    folder.mkdir(parents=True)
    (folder / "check.py").write_text(_CHECK_WITH_HELPER, encoding="utf-8")
    (folder / "helper.py").write_text("def matches(text, needle):\n    return needle in text\n", encoding="utf-8")
    (folder / "expected").mkdir()
    (folder / "expected" / "answer.txt").write_text("forty-two\n", encoding="utf-8")
    payload: dict[str, object] = {
        "schema_version": 1,
        "dataset_id": "helper",
        "cases": [
            {
                "case_id": "answer",
                "prompt": "What is the answer?",
                "evaluation": {"method": "script_based", "script": "check.py"},
                "fixture": {"response": "It is forty-two."},
            }
        ],
    }
    if support_files is not None:
        payload["support_files"] = support_files
    path = folder / "dataset.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize(("support_files", "expected_score"), [(["helper.py", "expected/"], 1.0), (None, 0.0)])
async def test_adhoc_and_task_eval_score_a_dataset_with_helper_files_the_same(
    tmp_path: Path, support_files: list[str] | None, expected_score: float
) -> None:
    dataset = _write_helper_dataset(tmp_path / "suite", support_files=support_files)
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    adhoc = await ctx.evaluate_dataset(dataset, runner=StubCaseRunner(), output_dir=tmp_path / "adhoc")
    task_id = ctx.create_task(name="helper", dataset_path=dataset)["task_id"]
    task = await ctx.evaluate_task(task_id, runner=StubCaseRunner())
    assert adhoc.score == task.score == expected_score
    assert adhoc.errored_count == task.errored_count == 0


def test_support_files_must_exist_inside_the_dataset_folder(tmp_path: Path) -> None:
    dataset = _write_helper_dataset(tmp_path / "suite", support_files=["missing.py"])
    with pytest.raises(RsiDatasetInvalid, match="support_files entry not found"):
        load_dataset(dataset)
    (tmp_path / "outside.py").write_text("x = 1\n", encoding="utf-8")
    dataset = _write_helper_dataset(tmp_path / "suite2", support_files=["../outside.py"])
    with pytest.raises(RsiDatasetInvalid, match="support_files"):
        load_dataset(dataset)


def _write_response_dataset(path: Path, needles: dict[str, str]) -> Path:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "responses",
                "cases": [
                    {
                        "case_id": case_id,
                        "prompt": f"Say {needle}",
                        "evaluation": {"method": "script_based", "expect": {"response_contains": [needle]}},
                    }
                    for case_id, needle in needles.items()
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


class _ScriptedRunner:
    """Answers or fails per (case, whether the profile carries ``FIX``); counts calls."""

    def __init__(self, profile: AgentProfile, plan: dict[tuple[str, bool], list[str | None]], calls: list[str]):
        self._fixed = "FIX" in (profile.instructions or "")
        self._plan = plan
        self._calls = calls

    async def run_case(self, case: RsiCase, workdir: Path) -> CaseRunResult:
        workdir.mkdir(parents=True, exist_ok=True)
        self._calls.append(f"{case.case_id}:{'fixed' if self._fixed else 'base'}")
        outcomes = self._plan[(case.case_id, self._fixed)]
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if outcome is None:
            return CaseRunResult(case_id=case.case_id, response_text="", workdir=workdir, error="provider timed out")
        return CaseRunResult(case_id=case.case_id, response_text=outcome, workdir=workdir)


class _AppendFix:
    def __init__(self) -> None:
        self.histories: list[tuple[ProposalOutcome, ...]] = []

    async def propose(
        self,
        profile: AgentProfile,
        failed: list[CaseEvalRecord],
        *,
        instruction: str | None,
        epoch: int,
        history: tuple[ProposalOutcome, ...],
    ) -> MutationProposal:
        del profile, instruction, epoch
        assert all(not record.errored for record in failed)
        self.histories.append(history)
        return MutationProposal.ready([AppendInstructions(text="FIX")])


async def _optimize(tmp_path: Path, needles: dict[str, str], plan, *, epochs: int = 1):
    dataset = _write_response_dataset(tmp_path / "dataset.json", needles)
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="errors", max_iterations=epochs, dataset_path=dataset)["task_id"]
    calls: list[str] = []
    optimizer = _AppendFix()
    result = await ctx.optimize_task(
        task_id,
        base_profile=AgentProfile(name="RsiCandidate", instructions=""),
        max_epochs=epochs,
        optimizer=optimizer,
        runner_factory=lambda profile: _ScriptedRunner(profile, plan, calls),
    )
    return ctx, task_id, result, calls, optimizer


@pytest.mark.asyncio
async def test_baseline_error_that_clears_on_retry_does_not_adopt_a_no_op(tmp_path: Path) -> None:
    # The agent always answers correctly; only its very first run hits a provider timeout.
    plan = {("hello", False): [None, "hello"], ("hello", True): ["hello"]}
    _ctx, _task_id, result, calls, optimizer = await _optimize(tmp_path, {"hello": "hello"}, plan)
    assert calls == ["hello:base", "hello:base"]
    assert result.baseline_score == result.best_score == 1.0
    assert result.best_candidate_id.endswith("-baseline")
    assert result.epochs == ()
    assert optimizer.histories == []


@pytest.mark.asyncio
async def test_baseline_that_keeps_erroring_stops_optimize(tmp_path: Path) -> None:
    plan = {("hello", False): [None], ("hello", True): ["hello"]}
    with pytest.raises(RsiBadRequest, match=r"baseline could not run 1 of 1 case.*hello: provider timed out"):
        await _optimize(tmp_path, {"hello": "hello"}, plan)


@pytest.mark.asyncio
async def test_candidate_erroring_in_the_gate_is_inconclusive(tmp_path: Path) -> None:
    plan = {("alpha", False): ["nothing"], ("alpha", True): [None]}
    ctx, task_id, result, calls, optimizer = await _optimize(tmp_path, {"alpha": "alpha"}, plan, epochs=2)
    assert calls.count("alpha:fixed") == 4  # one gate run plus its retry, per epoch
    assert [(epoch.inconclusive, epoch.adopted) for epoch in result.epochs] == [(True, False), (True, False)]
    assert result.best_candidate_id.endswith("-baseline")
    assert [(item.epoch, item.verdict) for item in optimizer.histories[1]] == [(1, "inconclusive")]
    descriptions = [node.get("description") or "" for node in ctx.get_tree(task_id)["nodes"]]
    assert any(item.startswith("inconclusive: 1 case(s) could not run") for item in descriptions)


@pytest.mark.asyncio
async def test_candidate_erroring_in_the_confirm_run_is_not_adopted(tmp_path: Path) -> None:
    # The candidate fixes alpha (the gate re-runs only alpha), but beta errors in the full confirm run.
    plan = {
        ("alpha", False): ["beta only"],
        ("beta", False): ["beta"],
        ("alpha", True): ["alpha"],
        ("beta", True): [None],
    }
    _ctx, _task_id, result, calls, _optimizer = await _optimize(
        tmp_path, {"alpha": "alpha", "beta": "beta"}, plan, epochs=1
    )
    assert calls.count("beta:fixed") == 2
    (epoch,) = result.epochs
    assert epoch.inconclusive is True
    assert epoch.adopted is False
    assert result.best_score == result.baseline_score == 0.5
    assert result.perfect is False


def test_best_pointer_compares_scores_only_within_one_base(tmp_path: Path) -> None:
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="bases")["task_id"]
    profile_path = tmp_path / "p.yaml"

    def update(candidate_id: str, score: float, base: str, kind: str = "live") -> bool:
        return maybe_update_best_pointer(
            ctx.store, task_id, candidate_id=candidate_id, score=score, profile_path=profile_path, kind=kind, base=base
        )

    assert update("a-high", 0.9, "base-a") is True
    assert update("a-low", 0.5, "base-a") is False
    assert update("b-low", 0.5, "base-b") is True
    assert read_best_pointer(ctx.store, task_id)["base"] == "base-b"
    assert update("b-lower", 0.4, "base-b") is False
    assert update("stub-high", 1.0, "base-b", kind="stub") is False
    assert read_best_pointer(ctx.store, task_id)["candidate_id"] == "b-low"


@pytest.mark.asyncio
async def test_reoptimize_from_another_base_profile_replaces_the_best(tmp_path: Path) -> None:
    dataset = _write_response_dataset(tmp_path / "dataset.json", {"hello": "hello"})
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="rebase", max_iterations=1, dataset_path=dataset)["task_id"]

    class _Answer:
        def __init__(self, text: str) -> None:
            self._text = text

        async def run_case(self, case: RsiCase, workdir: Path) -> CaseRunResult:
            workdir.mkdir(parents=True, exist_ok=True)
            return CaseRunResult(case_id=case.case_id, response_text=self._text, workdir=workdir)

    first = await ctx.optimize_task(
        task_id,
        base_profile=AgentProfile(name="RsiCandidate", instructions="first base"),
        max_epochs=1,
        runner_factory=lambda _profile: _Answer("hello"),
    )
    assert first.best_score == 1.0
    second = await ctx.optimize_task(
        task_id,
        base_profile=AgentProfile(name="RsiCandidate", instructions="second base"),
        max_epochs=1,
        optimizer=_AppendFix(),
        runner_factory=lambda _profile: _Answer("no"),
    )
    assert second.best_score == 0.0
    assert second.best_json_updated is True
    assert read_best_pointer(ctx.store, task_id)["candidate_id"] == second.best_candidate_id
