# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Phase 3 optimize loop: propose mutations, materialize, gate on eval score."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.rsi.errors import RsiBadRequest
from chrys.service.rsi.evaluator import CaseEvalRecord, EvalSuiteResult, merge_suite
from chrys.service.rsi.mutations import (
    AppendInstructions,
    Mutation,
    UpsertInlineSkill,
    case_marker,
)
from chrys.service.rsi.snapshot import (
    CandidateSnapshot,
    materialize_candidate,
    maybe_update_best_pointer,
    profile_fingerprint,
    read_best_pointer,
)
from chrys.service.rsi.store import RsiTaskStore


@dataclass(frozen=True, slots=True)
class MutationProposal:
    """One optimizer attempt.

    ``dropped`` means the attempt failed (model error or unparsable output)
    and the loop should try the next epoch. An empty proposal with
    ``dropped`` false means the optimizer has nothing further to try.
    """

    mutations: tuple[Mutation, ...]
    dropped: bool = False

    @staticmethod
    def ready(mutations: list[Mutation] | tuple[Mutation, ...]) -> MutationProposal:
        return MutationProposal(mutations=tuple(mutations), dropped=False)

    @staticmethod
    def none() -> MutationProposal:
        return MutationProposal(mutations=(), dropped=False)

    @staticmethod
    def drop() -> MutationProposal:
        return MutationProposal(mutations=(), dropped=True)


ProposalVerdict = Literal["adopted", "rejected", "inconclusive"]


@dataclass(frozen=True, slots=True)
class ProposalOutcome:
    """What became of one earlier proposal in the same optimize run.

    ``inconclusive`` means some case could not run even after a retry, so the
    proposal was neither adopted nor judged worse.
    """

    epoch: int
    verdict: ProposalVerdict
    mutations: tuple[dict[str, Any], ...]
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "epoch": self.epoch,
            "verdict": self.verdict,
            "score": self.score,
            "mutations": [dict(item) for item in self.mutations],
        }


class MutationOptimizer(Protocol):
    """Propose profile/skills mutations from failed eval records.

    *history* lists this run's earlier proposals, oldest first; adopted ones are
    already applied to *profile*.
    """

    async def propose(
        self,
        profile: AgentProfile,
        failed: list[CaseEvalRecord],
        *,
        instruction: str | None,
        epoch: int,
        history: tuple[ProposalOutcome, ...],
    ) -> MutationProposal: ...


class OptimizeEvents(Protocol):
    """Synchronous tree updates around one optimize run (no extra awaits)."""

    def baseline(self, *, candidate_id: str, score: float, total_epochs: int) -> None: ...

    def candidate(
        self,
        *,
        epoch: int,
        candidate_id: str,
        parent_id: str,
        score: float,
        best_score: float,
        baseline_score: float,
        adopted: bool,
        total_epochs: int,
        summary: str,
    ) -> None: ...


class StubOptimizer:
    """Deterministic optimizer for CI: append ``rsi:case:<id>`` markers (and optional skill).

    Paired with :class:`~chrys.service.rsi.case_runner.ProfileAwareStubRunner`, which
    synthesizes a passing case response when the matching marker is present.
    """

    async def propose(
        self,
        profile: AgentProfile,
        failed: list[CaseEvalRecord],
        *,
        instruction: str | None,
        epoch: int,
        history: tuple[ProposalOutcome, ...],
    ) -> MutationProposal:
        del profile, epoch, history  # stub ignores current profile text, epoch index and history
        if not failed:
            return MutationProposal.none()
        lines = [case_marker(record.case_id) for record in failed]
        if instruction and instruction.strip():
            lines.append(f"# rsi:hint: {instruction.strip()}")
        mutations: list[Mutation] = [AppendInstructions(text="\n".join(lines))]
        hint = (instruction or "").lower()
        if "skill" in hint:
            first = failed[0].case_id
            # Skill names must match loader ``_SKILL_NAME_RE`` (no ``--``, ASCII only).
            raw = "".join(ch if ch.isalnum() else "-" for ch in first.lower())
            while "--" in raw:
                raw = raw.replace("--", "-")
            safe = raw.strip("-")
            skill_name = f"rsi-fix-{safe}"[:64].rstrip("-")
            mutations.append(
                UpsertInlineSkill(
                    name=skill_name or "rsi-fix",
                    description=f"RSI stub skill for {first}",
                    instructions=f"When solving case {first}, follow the expect checks.\n{case_marker(first)}\n",
                )
            )
        return MutationProposal.ready(mutations)


@dataclass(frozen=True, slots=True)
class EpochResult:
    """One optimize epoch's gate outcome."""

    epoch: int
    candidate_id: str
    score: float
    passed: int
    total: int
    adopted: bool
    mutations: tuple[dict[str, Any], ...]
    dropped: bool = False
    # Some case could not run even after one retry; the candidate was not compared.
    inconclusive: bool = False


@dataclass(frozen=True, slots=True)
class OptimizeResult:
    """Full optimize-loop outcome."""

    task_id: str
    baseline_score: float
    best_score: float
    best_candidate_id: str
    epochs: tuple[EpochResult, ...]
    perfect: bool
    run_id: str = ""
    # What ``best.json`` (the candidate ``install`` exports) holds after this run;
    # it differs from ``best_candidate_id`` when the run did not replace it.
    best_json_updated: bool = False
    installed_candidate_id: str = ""
    installed_score: float | None = None
    installed_kind: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "baseline_score": self.baseline_score,
            "best_score": self.best_score,
            "best_candidate_id": self.best_candidate_id,
            "perfect": self.perfect,
            "best_json_updated": self.best_json_updated,
            "installed_candidate_id": self.installed_candidate_id,
            "installed_score": self.installed_score,
            "installed_kind": self.installed_kind,
            "epochs": [
                {
                    "epoch": e.epoch,
                    "candidate_id": e.candidate_id,
                    "score": e.score,
                    "passed": e.passed,
                    "total": e.total,
                    "adopted": e.adopted,
                    "dropped": e.dropped,
                    "inconclusive": e.inconclusive,
                    "mutations": list(e.mutations),
                }
                for e in self.epochs
            ],
        }


EvaluateFn = Callable[[AgentProfile, str, Collection[str] | None], Awaitable[EvalSuiteResult]]


def new_optimize_run_id() -> str:
    """Return a short unique id for one optimize run's candidate / analysis dirs."""
    return f"r{secrets.token_hex(4)}"


class OptimizeLoop:
    """Baseline eval → propose → materialize → gate; keep strictly better candidates."""

    def __init__(
        self,
        store: RsiTaskStore,
        *,
        optimizer: MutationOptimizer | None = None,
    ) -> None:
        self.store = store
        self.optimizer: MutationOptimizer = optimizer or StubOptimizer()

    async def run(
        self,
        task_id: str,
        *,
        base_profile: AgentProfile,
        evaluate: EvaluateFn,
        max_epochs: int,
        instruction: str | None = None,
        events: OptimizeEvents | None = None,
        run_id: str | None = None,
        kind: str = "stub",
    ) -> OptimizeResult:
        """Run the gated optimize loop.

        ``evaluate`` is ``(profile, label, case_ids) -> EvalSuiteResult``.
        ``case_ids`` is ``None`` for a full-dataset run. A gate first re-runs
        only still-failing ids (fast reject), then a candidate that looks better
        is confirmed on the full suite before adoption.

        Only judged outcomes are compared. Every evaluation re-runs the cases
        that could not run (runner error, timeout, workdir setup) once; a
        baseline that still has such cases stops the run, and a candidate that
        does is recorded as inconclusive and never adopted. So ``best_eval`` is
        always a complete full-suite result, and no run error reaches the
        optimizer.

        ``best.json`` is written only after this run finishes successfully (see
        :func:`maybe_update_best_pointer` for when it is replaced). Candidate ids
        are scoped by ``run_id`` so re-optimize never overwrites an earlier
        run's baseline/epoch directories.
        """
        if max_epochs < 1:
            max_epochs = 1
        opt_run = (run_id or new_optimize_run_id()).strip() or new_optimize_run_id()
        run_kind = "live" if kind == "live" else "stub"

        baseline_id = f"{opt_run}-baseline"
        baseline_snap = materialize_candidate(
            self.store,
            task_id,
            candidate_id=baseline_id,
            base_profile=base_profile,
            mutations=(),
            epoch=0,
        )
        baseline_eval = await _evaluate_retrying_errors(evaluate, baseline_snap.profile, baseline_id, None)
        if baseline_eval.errored_count:
            raise RsiBadRequest(_incomplete_baseline_message(baseline_eval))
        best_score = baseline_eval.score
        best_snap = baseline_snap
        best_eval = baseline_eval
        if events is not None:
            events.baseline(candidate_id=baseline_id, score=best_score, total_epochs=max_epochs)

        epochs: list[EpochResult] = []
        history: list[ProposalOutcome] = []
        working_profile = baseline_snap.profile
        parent_id = baseline_snap.candidate_id

        def settle(result: EpochResult, *, verdict: ProposalVerdict | None, best_after: float, summary: str) -> None:
            epochs.append(result)
            if verdict is not None:
                history.append(
                    ProposalOutcome(epoch=result.epoch, verdict=verdict, mutations=result.mutations, score=result.score)
                )
            if events is not None:
                events.candidate(
                    epoch=result.epoch,
                    candidate_id=result.candidate_id,
                    parent_id=parent_id,
                    score=result.score,
                    best_score=best_after,
                    baseline_score=baseline_eval.score,
                    adopted=result.adopted,
                    total_epochs=max_epochs,
                    summary=summary,
                )

        for epoch in range(1, max_epochs + 1):
            failed = [record for record in best_eval.records if not record.passed]
            if not failed:
                break
            proposal = await self.optimizer.propose(
                working_profile,
                failed,
                instruction=instruction,
                epoch=epoch,
                history=tuple(history),
            )
            candidate_id = f"{opt_run}-epoch-{epoch}"
            if proposal.dropped:
                settle(
                    EpochResult(
                        epoch=epoch,
                        candidate_id=candidate_id,
                        score=best_score,
                        passed=best_eval.passed_count,
                        total=best_eval.total_count,
                        adopted=False,
                        mutations=(),
                        dropped=True,
                    ),
                    verdict=None,
                    best_after=best_score,
                    summary="proposal dropped",
                )
                continue
            mutations = list(proposal.mutations)
            if not mutations:
                break
            snap = materialize_candidate(
                self.store,
                task_id,
                candidate_id=candidate_id,
                base_profile=working_profile,
                mutations=mutations,
                parent_id=parent_id,
                epoch=epoch,
            )
            mutation_dicts = tuple(m.to_dict() for m in mutations)
            kinds = ", ".join(mutation.kind for mutation in mutations)
            failed_ids = [record.case_id for record in failed]
            gated_partial = await _evaluate_retrying_errors(evaluate, snap.profile, candidate_id, failed_ids)
            gated = merge_suite(best_eval, gated_partial)
            if gated_partial.errored_count:
                settle(
                    _inconclusive_epoch(epoch, candidate_id, gated, mutation_dicts),
                    verdict="inconclusive",
                    best_after=best_score,
                    summary=f"inconclusive: {gated_partial.errored_count} case(s) could not run",
                )
                continue
            # Fast reject when the failed subset did not improve the merged score.
            if gated.score <= best_score:
                settle(
                    EpochResult(
                        epoch=epoch,
                        candidate_id=candidate_id,
                        score=gated.score,
                        passed=gated.passed_count,
                        total=gated.total_count,
                        adopted=False,
                        mutations=mutation_dicts,
                    ),
                    verdict="rejected",
                    best_after=best_score,
                    summary=kinds,
                )
                continue

            # Full-suite confirm before adopting (guards regressions on prior passes).
            confirmed = await _evaluate_retrying_errors(evaluate, snap.profile, candidate_id, None)
            if confirmed.errored_count:
                settle(
                    _inconclusive_epoch(epoch, candidate_id, confirmed, mutation_dicts),
                    verdict="inconclusive",
                    best_after=best_score,
                    summary=f"inconclusive: {confirmed.errored_count} case(s) could not run",
                )
                continue
            adopted = confirmed.score > best_score
            settle(
                EpochResult(
                    epoch=epoch,
                    candidate_id=candidate_id,
                    score=confirmed.score,
                    passed=confirmed.passed_count,
                    total=confirmed.total_count,
                    adopted=adopted,
                    mutations=mutation_dicts,
                ),
                verdict="adopted" if adopted else "rejected",
                best_after=confirmed.score if adopted else best_score,
                summary=kinds,
            )
            if adopted:
                best_score = confirmed.score
                best_snap = snap
                best_eval = confirmed
                working_profile = snap.profile
                parent_id = snap.candidate_id
                _write_candidate_score_meta(snap, score=confirmed.score, adopted=True)

        # Deferred: only commit best.json after a completed run so an interrupted
        # re-optimize cannot point install at an unevaluated overwrite. Stub dry-runs
        # and lower-scoring re-runs leave a prior live best alone.
        updated = maybe_update_best_pointer(
            self.store,
            task_id,
            candidate_id=best_snap.candidate_id,
            score=best_score,
            profile_path=best_snap.profile_path,
            kind=run_kind,
            base=profile_fingerprint(baseline_snap.profile_path),
        )
        _write_candidate_score_meta(
            best_snap,
            score=best_score,
            adopted=True,
            best=updated,
        )

        pointer = read_best_pointer(self.store, task_id)
        # best_eval is always from a full-suite evaluate (baseline or confirmed adopt).
        perfect = best_eval.passed_count == best_eval.total_count and best_eval.total_count > 0
        return OptimizeResult(
            task_id=task_id,
            baseline_score=baseline_eval.score,
            best_score=best_score,
            best_candidate_id=best_snap.candidate_id,
            epochs=tuple(epochs),
            perfect=perfect,
            run_id=opt_run,
            best_json_updated=updated,
            installed_candidate_id=str(pointer.get("candidate_id") or ""),
            installed_score=_pointer_score(pointer),
            installed_kind=str(pointer.get("kind") or "stub"),
        )


async def _evaluate_retrying_errors(
    evaluate: EvaluateFn,
    profile: AgentProfile,
    label: str,
    case_ids: Collection[str] | None,
) -> EvalSuiteResult:
    """Evaluate, then re-run once every case that could not run and merge its new outcome."""
    result = await evaluate(profile, label, case_ids)
    errored = [record.case_id for record in result.records if record.errored]
    if not errored:
        return result
    return merge_suite(result, await evaluate(profile, label, errored))


def _incomplete_baseline_message(baseline: EvalSuiteResult) -> str:
    errored = [record for record in baseline.records if record.errored]
    shown = "; ".join(f"{record.case_id}: {record.error}" for record in errored[:3])
    more = f"; and {len(errored) - 3} more" if len(errored) > 3 else ""
    return (
        f"baseline could not run {len(errored)} of {baseline.total_count} case(s), even after a retry ({shown}{more}). "
        "Candidates are only compared against a baseline that ran every case: check the model and provider, "
        "or allow more time per case."
    )


def _inconclusive_epoch(
    epoch: int,
    candidate_id: str,
    suite: EvalSuiteResult,
    mutations: tuple[dict[str, Any], ...],
) -> EpochResult:
    return EpochResult(
        epoch=epoch,
        candidate_id=candidate_id,
        score=suite.score,
        passed=suite.passed_count,
        total=suite.total_count,
        adopted=False,
        mutations=mutations,
        inconclusive=True,
    )


def _pointer_score(pointer: dict[str, Any]) -> float | None:
    score = pointer.get("score")
    if score is None:
        return None
    try:
        return float(score)
    except TypeError, ValueError:
        return None


def _write_candidate_score_meta(
    snap: CandidateSnapshot,
    *,
    score: float,
    adopted: bool = False,
    best: bool = False,
) -> None:
    from chrys.service.rsi.paths import write_json_atomic

    meta: dict[str, Any] = {
        "candidate_id": snap.candidate_id,
        "parent_id": snap.parent_id,
        "epoch": snap.epoch,
        "profile_name": snap.profile.name,
        "profile_path": str(snap.profile_path),
        "mutation_count": len(snap.mutations),
        "score": score,
    }
    if adopted:
        meta["adopted"] = True
    if best:
        meta["best"] = True
    write_json_atomic(snap.meta_path, meta)
