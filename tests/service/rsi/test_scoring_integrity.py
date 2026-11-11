# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Scores compare only what the checks judged, the same way on every path."""

from __future__ import annotations

import json
import re
import shutil
import types
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.rsi import RsiDatasetInvalid, build_rsi_service_context, load_dataset
from chrys.service.rsi import evaluator as evaluator_module
from chrys.service.rsi.case_runner import CaseErrorCause, CaseRunResult, StubCaseRunner, case_sessions_dir
from chrys.service.rsi.context import RsiServiceContext
from chrys.service.rsi.dataset import RsiCase
from chrys.service.rsi.errors import RsiBadRequest
from chrys.service.rsi.evaluator import CaseEvalRecord
from chrys.service.rsi.judge_inputs import JudgeInputs
from chrys.service.rsi.mutations import AppendInstructions
from chrys.service.rsi.optimizer import MutationProposal, ProposalOutcome
from chrys.service.rsi.snapshot import maybe_update_best_pointer, read_best_pointer
from tests.support.symlinks import symlink_or_skip

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


class _Tamperer:
    """Changes the task's judge files by their absolute paths, then answers wrong."""

    def __init__(self, tamper: Callable[[Path], None], input_dir: Path) -> None:
        self._tamper = tamper
        self._input_dir = input_dir

    async def run_case(self, case: RsiCase, workdir: Path) -> CaseRunResult:
        workdir.mkdir(parents=True, exist_ok=True)
        self._tamper(self._input_dir)
        return CaseRunResult(case_id=case.case_id, response_text="wrong", workdir=workdir)


def _pass_everything(input_dir: Path) -> None:
    (input_dir / "check.py").write_text("import sys\nsys.exit(0)\n", encoding="utf-8")


def _match_anything(input_dir: Path) -> None:
    (input_dir / "helper.py").write_text("def matches(text, needle):\n    return True\n", encoding="utf-8")


def _expect_wrong(input_dir: Path) -> None:
    (input_dir / "expected" / "answer.txt").write_text("wrong\n", encoding="utf-8")


def _drop_the_script(input_dir: Path) -> None:
    payload = json.loads((input_dir / "dataset.json").read_text(encoding="utf-8"))
    payload["cases"][0]["evaluation"] = {"method": "script_based", "expect": {"response_contains": ["wrong"]}}
    (input_dir / "dataset.json").write_text(json.dumps(payload), encoding="utf-8")


def _link_expected_outside(input_dir: Path) -> None:
    outside = input_dir.parents[2] / "outside-expected"
    outside.mkdir()
    (outside / "answer.txt").write_text("wrong\n", encoding="utf-8")
    shutil.rmtree(input_dir / "expected")
    symlink_or_skip(input_dir / "expected", outside, target_is_directory=True)


def _judge_files_now(ctx: RsiServiceContext, task_id: str) -> dict[str, str]:
    input_file = ctx.store.get(task_id).input_file
    assert input_file is not None
    return JudgeInputs.capture(load_dataset(input_file)).fingerprints()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tamper", "changed"),
    [
        (_pass_everything, "check.py"),
        (_match_anything, "helper.py"),
        (_expect_wrong, "expected/answer.txt"),
        (_drop_the_script, "dataset.json"),
        (_link_expected_outside, "expected"),
    ],
    ids=["check-script", "helper", "expected-answer", "dataset-file", "support-folder-link"],
)
async def test_agent_that_changes_the_task_judge_files_fails_and_they_are_put_back(
    tmp_path: Path, tamper: Callable[[Path], None], changed: str
) -> None:
    dataset = _write_helper_dataset(tmp_path / "suite", support_files=["helper.py", "expected/"])
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="tamper", dataset_path=dataset)["task_id"]
    recorded = ctx.store.get(task_id).config["judge_files"]

    result = await ctx.evaluate_task(task_id, runner=_Tamperer(tamper, tmp_path / "tasks" / task_id / "input"))

    (record,) = result.records
    assert (record.passed, record.errored, record.attempts) == (False, False, 1)
    (finding,) = record.findings
    assert finding["code"] == "judge_tampered"
    assert changed in finding["message"].split(": ", 1)[1].split(", ")
    assert _judge_files_now(ctx, task_id) == recorded
    if tamper is _link_expected_outside:
        assert (tmp_path / "outside-expected" / "answer.txt").read_text(encoding="utf-8") == "wrong\n"
    honest = await ctx.evaluate_task(task_id, runner=StubCaseRunner())
    assert (honest.score, honest.records[0].findings) == (1.0, ())


@pytest.mark.asyncio
async def test_candidate_that_changes_the_judge_is_not_adopted(tmp_path: Path) -> None:
    dataset = _write_helper_dataset(tmp_path / "suite", support_files=["helper.py", "expected/"])
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="tamper", max_iterations=1, dataset_path=dataset)["task_id"]
    recorded = ctx.store.get(task_id).config["judge_files"]
    input_dir = tmp_path / "tasks" / task_id / "input"
    optimizer = _AppendFix()

    class _Honest:
        async def run_case(self, case: RsiCase, workdir: Path) -> CaseRunResult:
            workdir.mkdir(parents=True, exist_ok=True)
            return CaseRunResult(case_id=case.case_id, response_text="wrong", workdir=workdir)

    result = await ctx.optimize_task(
        task_id,
        base_profile=AgentProfile(name="RsiCandidate", instructions=""),
        max_epochs=1,
        optimizer=optimizer,
        runner_factory=lambda p: (
            _Tamperer(_pass_everything, input_dir) if "FIX" in (p.instructions or "") else _Honest()
        ),
    )

    (epoch,) = result.epochs
    assert (epoch.adopted, epoch.inconclusive) == (False, False)
    assert result.best_score == result.baseline_score == 0.0
    assert result.best_candidate_id.endswith("-baseline")
    assert _judge_files_now(ctx, task_id) == recorded


@pytest.mark.asyncio
async def test_adhoc_eval_judges_with_the_files_it_started_with(tmp_path: Path) -> None:
    dataset = _write_helper_dataset(tmp_path / "suite", support_files=["helper.py", "expected/"])
    out = tmp_path / "adhoc"
    seen_in_eval_folder: list[str] = []

    class _EditsTheDataset:
        async def run_case(self, case: RsiCase, workdir: Path) -> CaseRunResult:
            workdir.mkdir(parents=True, exist_ok=True)
            seen_in_eval_folder.extend(p.name for p in out.rglob("*") if p.name in {"check.py", "helper.py"})
            _pass_everything(dataset.parent)
            return CaseRunResult(case_id=case.case_id, response_text="wrong", workdir=workdir)

    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    result = await ctx.evaluate_dataset(dataset, runner=_EditsTheDataset(), output_dir=out)

    assert seen_in_eval_folder == []
    (record,) = result.records
    assert (record.passed, [item["code"] for item in record.findings]) == (False, ["script"])
    # A user's own dataset folder is theirs: the evaluation never rewrites it.
    assert (dataset.parent / "check.py").read_text(encoding="utf-8") == "import sys\nsys.exit(0)\n"


def _write_seeded_dataset(tmp_path: Path) -> Path:
    seed = tmp_path / "project"
    seed.mkdir()
    (seed / "notes.txt").write_text("todo\n", encoding="utf-8")
    case = {
        "case_id": "notes",
        "prompt": "Finish the notes",
        "workdir": {"from": str(seed), "strategy": "copy"},
        "evaluation": {"method": "script_based", "expect": {"file_contains": {"notes.txt": ["done"]}}},
    }
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps({"schema_version": 1, "dataset_id": "notes", "cases": [case]}), encoding="utf-8")
    return path


def _task_files_above(workdir: Path) -> list[Path]:
    """The task's seeds and candidate profiles, as an agent looking in the folders above its own finds them."""
    return [
        path
        for parent in workdir.parents
        for path in (parent / "input" / "seeds", parent / "run" / "candidates")
        if path.exists()
    ]


@pytest.mark.asyncio
async def test_agent_looking_above_its_folder_cannot_change_the_seeds_or_candidates(tmp_path: Path) -> None:
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="snoop", max_iterations=1, dataset_path=_write_seeded_dataset(tmp_path))["task_id"]
    found: list[Path] = []

    class _Snooper:
        """Finishes the notes in every seed it finds above its folder, never its own."""

        async def run_case(self, case: RsiCase, workdir: Path) -> CaseRunResult:
            for path in _task_files_above(workdir):
                found.append(path)
                for notes in path.rglob("notes.txt"):
                    notes.write_text("done\n", encoding="utf-8")
            return CaseRunResult(case_id=case.case_id, response_text="nothing", workdir=workdir)

    result = await ctx.optimize_task(
        task_id,
        base_profile=AgentProfile(name="RsiCandidate", instructions=""),
        max_epochs=1,
        optimizer=_AppendFix(),
        runner_factory=lambda _profile: _Snooper(),
    )

    assert found == []
    assert result.best_score == result.baseline_score == 0.0


class _LeavesWork:
    """Finishes its notes, leaves a session beside its folder and remembers where it ran."""

    def __init__(self) -> None:
        self.ran_in: list[Path] = []

    async def run_case(self, case: RsiCase, workdir: Path) -> CaseRunResult:
        self.ran_in.append(workdir)
        (workdir / "notes.txt").write_text("done\n", encoding="utf-8")
        sessions = case_sessions_dir(workdir)
        sessions.mkdir()
        (sessions / "session.json").write_text("{}", encoding="utf-8")
        return CaseRunResult(case_id=case.case_id, response_text="done", workdir=workdir)


@pytest.mark.asyncio
async def test_case_runs_outside_the_task_and_is_kept_with_its_evaluation(tmp_path: Path) -> None:
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="kept", dataset_path=_write_seeded_dataset(tmp_path))["task_id"]
    runner = _LeavesWork()

    result = await ctx.evaluate_task(task_id, runner=runner)

    (ran_in,) = runner.ran_in
    assert not ran_in.resolve().is_relative_to((tmp_path / "tasks").resolve())
    kept = result.output_dir / "case_workspaces" / "notes"
    (record,) = result.records
    assert (Path(record.workdir), record.passed) == (kept, True)
    assert (kept / "notes.txt").read_text(encoding="utf-8") == "done\n"
    assert (case_sessions_dir(kept) / "session.json").is_file()
    assert not ran_in.parent.exists()


@pytest.mark.asyncio
async def test_case_folder_that_cannot_be_moved_stays_where_it_ran(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(src: Path, dst: Path) -> None:
        raise PermissionError(f"cannot move {src} to {dst}")

    shadow = types.ModuleType("shutil")
    shadow.__dict__.update(shutil.__dict__)
    shadow.move = refuse
    monkeypatch.setattr(evaluator_module, "shutil", shadow)
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="stays", dataset_path=_write_seeded_dataset(tmp_path))["task_id"]
    runner = _LeavesWork()

    result = await ctx.evaluate_task(task_id, runner=runner)

    (ran_in,) = runner.ran_in
    try:
        (record,) = result.records
        assert (Path(record.workdir), record.passed) == (ran_in, True)
        assert (ran_in / "notes.txt").read_text(encoding="utf-8") == "done\n"
        assert (case_sessions_dir(ran_in) / "session.json").is_file()
        assert not (result.output_dir / "case_workspaces" / "notes").exists()
    finally:
        shutil.rmtree(ran_in.parent)


@pytest.mark.asyncio
async def test_task_whose_judge_files_changed_between_evaluations_is_refused(tmp_path: Path) -> None:
    dataset = _write_helper_dataset(tmp_path / "suite", support_files=["helper.py", "expected/"])
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="changed", dataset_path=dataset)["task_id"]
    input_dir = tmp_path / "tasks" / task_id / "input"
    _expect_wrong(input_dir)
    with pytest.raises(RsiBadRequest, match=r"changed since the task was created \(expected/answer\.txt\)"):
        await ctx.evaluate_task(task_id, runner=StubCaseRunner())
    with pytest.raises(RsiBadRequest, match=r"changed since the task was created"):
        await ctx.optimize_task(task_id, max_epochs=1)
    assert (input_dir / "expected" / "answer.txt").read_text(encoding="utf-8") == "wrong\n"


@pytest.mark.asyncio
async def test_task_created_before_judge_fingerprints_still_evaluates(tmp_path: Path) -> None:
    dataset = _write_helper_dataset(tmp_path / "suite", support_files=["helper.py", "expected/"])
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="older", dataset_path=dataset)["task_id"]
    task = ctx.store.get(task_id)
    task.config = {key: value for key, value in task.config.items() if key != "judge_files"}
    ctx.store.save(task)
    result = await ctx.evaluate_task(task_id, runner=StubCaseRunner())
    assert result.score == 1.0
    assert "judge_files" not in ctx.store.get(task_id).config


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


@dataclass(frozen=True)
class _Fail:
    error: str
    cause: CaseErrorCause


_PROVIDER_DOWN = _Fail("provider unavailable", "environment")
_TIMED_OUT = _Fail("case run timed out after 1s", "timeout")
_OVERFLOWED = _Fail("context window exceeded", "agent")

type _Plan = dict[tuple[str, bool], list[str | _Fail]]


class _ScriptedRunner:
    """Answers or fails per (case, whether the profile carries ``FIX``); counts calls."""

    def __init__(self, profile: AgentProfile, plan: _Plan, calls: list[str]):
        self._fixed = "FIX" in (profile.instructions or "")
        self._plan = plan
        self._calls = calls

    async def run_case(self, case: RsiCase, workdir: Path) -> CaseRunResult:
        workdir.mkdir(parents=True, exist_ok=True)
        self._calls.append(f"{case.case_id}:{'fixed' if self._fixed else 'base'}")
        outcomes = self._plan[(case.case_id, self._fixed)]
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if isinstance(outcome, _Fail):
            return CaseRunResult(
                case_id=case.case_id,
                response_text="",
                workdir=workdir,
                error=outcome.error,
                error_cause=outcome.cause,
            )
        return CaseRunResult(case_id=case.case_id, response_text=outcome, workdir=workdir)


class _AppendFix:
    def __init__(self) -> None:
        self.histories: list[tuple[ProposalOutcome, ...]] = []
        self.failures: list[list[CaseEvalRecord]] = []

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
        self.failures.append(failed)
        return MutationProposal.ready([AppendInstructions(text="FIX")])


async def _optimize(tmp_path: Path, needles: dict[str, str], plan: _Plan, *, epochs: int = 1):
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
@pytest.mark.parametrize("hiccup", [_PROVIDER_DOWN, _TIMED_OUT], ids=["provider-down", "timed-out"])
async def test_baseline_hiccup_that_clears_on_retry_does_not_adopt_a_no_op(tmp_path: Path, hiccup: _Fail) -> None:
    # The agent always answers correctly; only its very first run fails.
    plan: _Plan = {("hello", False): [hiccup, "hello"], ("hello", True): ["hello"]}
    _ctx, _task_id, result, calls, optimizer = await _optimize(tmp_path, {"hello": "hello"}, plan)
    assert calls == ["hello:base", "hello:base"]
    assert result.baseline_score == result.best_score == 1.0
    assert result.best_candidate_id.endswith("-baseline")
    assert result.epochs == ()
    assert optimizer.histories == []


@pytest.mark.asyncio
async def test_baseline_that_keeps_erroring_stops_optimize(tmp_path: Path) -> None:
    plan: _Plan = {("hello", False): [_PROVIDER_DOWN], ("hello", True): ["hello"]}
    with pytest.raises(RsiBadRequest, match=r"baseline could not run 1 of 1 case.*hello: provider unavailable"):
        await _optimize(tmp_path, {"hello": "hello"}, plan)


@pytest.mark.asyncio
async def test_baseline_case_that_always_times_out_is_a_failure_a_candidate_can_fix(tmp_path: Path) -> None:
    # The base agent loops on "build" until the time limit, every time; the candidate stops looping.
    plan: _Plan = {
        ("build", False): [_TIMED_OUT],
        ("hello", False): ["hello"],
        ("build", True): ["built"],
        ("hello", True): ["hello"],
    }
    _ctx, _task_id, result, calls, optimizer = await _optimize(
        tmp_path, {"build": "built", "hello": "hello"}, plan, epochs=1
    )
    assert calls.count("build:base") == 2
    assert result.baseline_score == 0.5
    ([seen],) = optimizer.failures
    assert (seen.case_id, seen.errored, seen.attempts) == ("build", False, 2)
    assert seen.findings == ({"code": "case_timeout", "message": "case run timed out after 1s"},)
    (epoch,) = result.epochs
    assert (epoch.adopted, epoch.inconclusive) == (True, False)
    assert result.best_score == 1.0


@pytest.mark.asyncio
async def test_error_the_agent_caused_is_a_failure_and_is_not_run_again(tmp_path: Path) -> None:
    plan: _Plan = {("hello", False): [_OVERFLOWED], ("hello", True): ["hello"]}
    _ctx, _task_id, result, calls, optimizer = await _optimize(tmp_path, {"hello": "hello"}, plan)
    assert calls.count("hello:base") == 1
    assert result.baseline_score == 0.0
    ([seen],) = optimizer.failures
    assert (seen.errored, seen.attempts) == (False, 1)
    assert seen.findings == ({"code": "agent_error", "message": "context window exceeded"},)
    assert result.best_score == 1.0


_BOTH_RUNS = "first run: provider unavailable; second run: case run timed out after 1s"


@pytest.mark.asyncio
async def test_baseline_stop_names_both_runs_errors_when_they_differ(tmp_path: Path) -> None:
    plan: _Plan = {("hello", False): [_PROVIDER_DOWN, _TIMED_OUT], ("hello", True): ["hello"]}
    with pytest.raises(RsiBadRequest, match=re.escape(f"hello: {_BOTH_RUNS}")):
        await _optimize(tmp_path, {"hello": "hello"}, plan)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcomes", "passed", "finding", "error"),
    [
        ([_PROVIDER_DOWN, "hello"], True, None, None),
        ([_TIMED_OUT, "hello"], True, None, None),
        ([_PROVIDER_DOWN], False, "runner_error", "provider unavailable"),
        ([_TIMED_OUT], False, "case_timeout", None),
        # A timeout is the agent's only when both runs reached the time limit.
        ([_PROVIDER_DOWN, _TIMED_OUT], False, "runner_error", _BOTH_RUNS),
        (
            [_TIMED_OUT, _PROVIDER_DOWN],
            False,
            "runner_error",
            "first run: case run timed out after 1s; second run: provider unavailable",
        ),
    ],
    ids=[
        "provider-clears",
        "timeout-clears",
        "provider-stays-down",
        "timeout-repeats",
        "provider-then-timeout",
        "timeout-then-provider",
    ],
)
async def test_evaluation_keeps_the_second_run_of_an_errored_or_timed_out_case(
    tmp_path: Path, outcomes: list[str | _Fail], passed: bool, finding: str | None, error: str | None
) -> None:
    dataset = _write_response_dataset(tmp_path / "dataset.json", {"hello": "hello"})
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    calls: list[str] = []
    first = outcomes[0]
    assert isinstance(first, _Fail)
    runner = _ScriptedRunner(AgentProfile(name="Agent", instructions=""), {("hello", False): outcomes}, calls)
    result = await ctx.evaluate_dataset(dataset, runner=runner, output_dir=tmp_path / "out")
    assert calls == ["hello:base", "hello:base"]
    (record,) = result.records
    assert (record.passed, record.error, record.attempts, record.first_error) == (passed, error, 2, first.error)
    assert [item["code"] for item in record.findings][:1] == ([finding] if finding else [])
    assert Path(record.workdir) == result.output_dir / "retry_workspaces" / "hello"
    scorecard = json.loads(result.scorecard_path.read_text(encoding="utf-8"))
    (card,) = scorecard["cases"]
    assert (scorecard["passed"], scorecard["errored"]) == (int(passed), int(error is not None))
    assert (card["error"], card["attempts"], card["first_error"]) == (error, 2, first.error)


@pytest.mark.asyncio
async def test_candidate_erroring_in_the_gate_is_inconclusive(tmp_path: Path) -> None:
    plan: _Plan = {("alpha", False): ["nothing"], ("alpha", True): [_PROVIDER_DOWN]}
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
    plan: _Plan = {
        ("alpha", False): ["beta only"],
        ("beta", False): ["beta"],
        ("alpha", True): ["alpha"],
        ("beta", True): [_PROVIDER_DOWN],
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
