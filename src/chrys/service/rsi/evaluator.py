# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run an RSI dataset through a CaseRunner and write scorecard artifacts."""

from __future__ import annotations

import json
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chrys.foundation.platform.files import atomic_write_text
from chrys.service.rsi.case_runner import CaseRunner, CaseRunResult
from chrys.service.rsi.dataset import RsiCase, RsiDataset
from chrys.service.rsi.engine_events import EngineEvent
from chrys.service.rsi.errors import RsiBadRequest
from chrys.service.rsi.judge_inputs import JudgeInputs
from chrys.service.rsi.models import utcnow_iso
from chrys.service.rsi.script_judge import JudgeResult, judge_case
from chrys.service.rsi.workdir_seed import materialize_workdir

EventSink = Callable[[EngineEvent], Awaitable[None]]


@dataclass(slots=True, frozen=True)
class CaseEvalRecord:
    """One case's run + judge result."""

    case_id: str
    passed: bool
    score: float
    response_text: str
    findings: tuple[dict[str, str], ...]
    error: str | None
    workdir: str
    session_id: str | None
    prompt: str = ""
    attempts: int = 1
    first_error: str | None = None
    """Why the first attempt failed, when the case ran a second time."""

    @property
    def errored(self) -> bool:
        """The case could not run for a reason outside the agent; the judge never saw it."""
        return self.error is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "passed": self.passed,
            "errored": self.errored,
            "score": self.score,
            "response_text": self.response_text,
            "findings": list(self.findings),
            "error": self.error,
            "workdir": self.workdir,
            "session_id": self.session_id,
            "prompt": self.prompt,
            "attempts": self.attempts,
            "first_error": self.first_error,
        }


@dataclass(slots=True, frozen=True)
class EvalSuiteResult:
    """Aggregate evaluation result for one dataset run."""

    run_id: str
    dataset_id: str
    output_dir: Path
    records: tuple[CaseEvalRecord, ...]
    passed_count: int
    total_count: int
    score: float

    @property
    def scorecard_path(self) -> Path:
        return self.output_dir / "scorecard.json"

    @property
    def cases_path(self) -> Path:
        return self.output_dir / "cases.jsonl"

    @property
    def errored_count(self) -> int:
        return sum(1 for record in self.records if record.errored)

    def to_scorecard(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "dataset_id": self.dataset_id,
            "passed": self.passed_count,
            "errored": self.errored_count,
            "total": self.total_count,
            "score": self.score,
            "solved_threshold": 1.0,
            "cases": [
                {
                    "case_id": record.case_id,
                    "passed": record.passed,
                    "errored": record.errored,
                    "score": record.score,
                    "error": record.error,
                    "attempts": record.attempts,
                    "first_error": record.first_error,
                }
                for record in self.records
            ],
        }


class ScriptBasedEvaluator:
    """Phase 2 evaluator: case runner + script_based judge + durable artifacts.

    A case that errors (the environment kept it from running, or it reached
    its time limit) runs once more in ``retry_workspaces/<case_id>``, and its
    record is the second attempt's, with the first attempt's error kept in
    ``first_error``. A case that times out on both attempts counts as the
    agent's: a judged failure, like any error the agent caused.

    Check scripts run from the files captured when the suite started (see
    :mod:`chrys.service.rsi.judge_inputs`). With ``guard_inputs``, the
    dataset's own folder belongs to RSI: after every case run it is compared
    with that capture, and a case whose agent changed it is put back and
    fails with ``judge_tampered``.
    """

    def __init__(self, runner: CaseRunner) -> None:
        self.runner = runner

    async def run_suite(
        self,
        dataset: RsiDataset,
        *,
        output_dir: Path,
        on_event: EventSink | None = None,
        task_id: str = "",
        judge_inputs: JudgeInputs | None = None,
        guard_inputs: bool = False,
    ) -> EvalSuiteResult:
        run_id = f"eval-{uuid.uuid4().hex[:8]}"
        out = Path(output_dir) / run_id
        inputs = judge_inputs if judge_inputs is not None else JudgeInputs.capture(dataset)
        cases_dir = out / "case_workspaces"
        cases_dir.mkdir(parents=True, exist_ok=True)
        records: list[CaseEvalRecord] = []
        seq = 0

        async def emit(family: str, kind: str, payload: dict[str, Any]) -> None:
            nonlocal seq
            if on_event is None:
                return
            seq += 1
            await on_event(
                EngineEvent(
                    family=family,
                    kind=kind,
                    task_id=task_id,
                    event_id=seq,
                    ts=utcnow_iso(),
                    payload=payload,
                )
            )

        await emit(
            "node",
            "created",
            {
                "node": {
                    "ref": "root",
                    "parent_ref": None,
                    "type": "ROOT",
                    "accepted": True,
                    "score": None,
                    "summary": f"dataset {dataset.dataset_id}",
                }
            },
        )

        total = len(dataset.cases)
        dataset_dir = dataset.source_path.parent
        for index, case in enumerate(dataset.cases, start=1):
            first = await self._run_case(case, cases_dir / case.case_id, dataset_dir=dataset_dir)
            record = _settle(case, first, inputs=inputs, guard_inputs=guard_inputs)
            if record.errored:
                # One more run, in a fresh workdir: a passing hiccup must not decide the score.
                retry = await self._run_case(case, out / "retry_workspaces" / case.case_id, dataset_dir=dataset_dir)
                record = _settle(case, retry, inputs=inputs, guard_inputs=guard_inputs, first=first)
            records.append(record)
            node_ref = f"case-{case.case_id}"
            await emit(
                "node",
                "created",
                {
                    "node": {
                        "ref": node_ref,
                        "parent_ref": "root",
                        "type": "ADOPTED" if record.passed else "REJECTED",
                        "accepted": record.passed,
                        "score": record.score,
                        "summary": case.case_id,
                        "failure_reason": record.error or (record.findings[0]["message"] if record.findings else None),
                    }
                },
            )
            passed_so_far = sum(1 for item in records if item.passed)
            await emit(
                "progress",
                "metric",
                {
                    "iteration": index,
                    "total_iterations": total,
                    "score": passed_so_far / total if total else 0.0,
                    "baseline": None,
                },
            )

        passed_count = sum(1 for item in records if item.passed)
        score = (passed_count / total) if total else 0.0
        result = EvalSuiteResult(
            run_id=run_id,
            dataset_id=dataset.dataset_id,
            output_dir=out,
            records=tuple(records),
            passed_count=passed_count,
            total_count=total,
            score=score,
        )
        _write_artifacts(result)
        return result

    async def _run_case(self, case: RsiCase, workdir: Path, *, dataset_dir: Path) -> CaseRunResult:
        try:
            if case.workdir is not None:
                materialize_workdir(case.workdir, dest=workdir, dataset_dir=dataset_dir)
            return await self.runner.run_case(case, workdir)
        except RsiBadRequest as exc:
            return CaseRunResult(case_id=case.case_id, response_text="", workdir=workdir, error=exc.message)


def merge_suite(previous: EvalSuiteResult, partial: EvalSuiteResult) -> EvalSuiteResult:
    """Overlay *partial* case records onto *previous* and recompute the suite score.

    Cases absent from *partial* keep their previous outcome. Optimize uses this
    so a gate that re-runs only failures still scores the whole dataset.
    """
    updates = {record.case_id: record for record in partial.records}
    records = tuple(updates.get(record.case_id, record) for record in previous.records)
    passed_count = sum(1 for record in records if record.passed)
    total_count = len(records)
    score = (passed_count / total_count) if total_count else 0.0
    return EvalSuiteResult(
        run_id=partial.run_id,
        dataset_id=previous.dataset_id,
        output_dir=partial.output_dir,
        records=records,
        passed_count=passed_count,
        total_count=total_count,
        score=score,
    )


def _settle(
    case: RsiCase,
    run: CaseRunResult,
    *,
    inputs: JudgeInputs,
    guard_inputs: bool,
    first: CaseRunResult | None = None,
) -> CaseEvalRecord:
    """Record *run*: a failure if its agent changed the judge's files (put back first), else as judged."""
    changed = inputs.changed() if guard_inputs else ()
    if not changed:
        return _record_for(case, run, inputs=inputs, first=first)
    inputs.restore()
    message = f"the agent changed files the judge reads, which were put back: {', '.join(changed)}"
    return CaseEvalRecord(
        case_id=case.case_id,
        passed=False,
        score=0.0,
        response_text=run.response_text,
        findings=({"code": "judge_tampered", "message": message},),
        error=None,
        workdir=str(run.workdir),
        session_id=run.session_id,
        prompt=case.prompt,
        attempts=1 if first is None else 2,
        first_error=first.error if first is not None else None,
    )


def _record_for(
    case: RsiCase, run: CaseRunResult, *, inputs: JudgeInputs, first: CaseRunResult | None = None
) -> CaseEvalRecord:
    """Judge *run*, or record why it could not be judged.

    *first* is the errored attempt *run* retried, if any. What the agent caused
    is a judged failure the optimizer sees. A timeout is the agent's only when
    *first* timed out too. Anything else stays errored, kept out of every score
    comparison, and names both attempts' errors when they differ.
    """
    first_error = first.error if first is not None else None
    attempts = 1 if first is None else 2
    if not run.ok:
        message = run.error or "runner failed"
        if run.error_cause == "agent":
            code = "agent_error"
        elif run.error_cause == "timeout" and first is not None and first.error_cause == "timeout":
            code = "case_timeout"
        else:
            code = "runner_error"
            if first_error is not None and first_error != message:
                message = f"first run: {first_error}; second run: {message}"
        return CaseEvalRecord(
            case_id=case.case_id,
            passed=False,
            score=0.0,
            response_text=run.response_text,
            findings=({"code": code, "message": message},),
            error=message if code == "runner_error" else None,
            workdir=str(run.workdir),
            session_id=run.session_id,
            prompt=case.prompt,
            attempts=attempts,
            first_error=first_error,
        )
    judged = _judge(case, run, inputs=inputs)
    return CaseEvalRecord(
        case_id=case.case_id,
        passed=judged.passed,
        score=judged.score,
        response_text=run.response_text,
        findings=tuple({"code": item.code, "message": item.message} for item in judged.findings),
        error=None,
        workdir=str(run.workdir),
        session_id=run.session_id,
        prompt=case.prompt,
        attempts=attempts,
        first_error=first_error,
    )


def _judge(case: RsiCase, run: CaseRunResult, *, inputs: JudgeInputs) -> JudgeResult:
    if not case.script:
        return judge_case(case, response_text=run.response_text, workdir=run.workdir, dataset_dir=inputs.source_dir)
    # Written only now that the agent is done, and outside every case folder.
    with tempfile.TemporaryDirectory(prefix="icode-rsi-judge-", ignore_cleanup_errors=True) as staged:
        return judge_case(
            case,
            response_text=run.response_text,
            workdir=run.workdir,
            dataset_dir=inputs.stage(Path(staged)),
        )


def _write_artifacts(result: EvalSuiteResult) -> None:
    result.output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        result.scorecard_path,
        json.dumps(result.to_scorecard(), ensure_ascii=True, indent=2) + "\n",
    )
    lines = [json.dumps(record.to_dict(), ensure_ascii=True) for record in result.records]
    atomic_write_text(result.cases_path, "\n".join(lines) + ("\n" if lines else ""))
