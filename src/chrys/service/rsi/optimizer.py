# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Phase 3 optimize loop: propose mutations, materialize, gate on eval score."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Protocol

from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.rsi.evaluator import CaseEvalRecord, merge_suite
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


class MutationOptimizer(Protocol):
    """Propose profile/skills mutations from failed eval records."""

    async def propose(
        self,
        profile: AgentProfile,
        failed: list[CaseEvalRecord],
        *,
        instruction: str | None,
        epoch: int,
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
    ) -> MutationProposal:
        del profile, epoch  # stub ignores current profile text and epoch index
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "baseline_score": self.baseline_score,
            "best_score": self.best_score,
            "best_candidate_id": self.best_candidate_id,
            "perfect": self.perfect,
            "epochs": [
                {
                    "epoch": e.epoch,
                    "candidate_id": e.candidate_id,
                    "score": e.score,
                    "passed": e.passed,
                    "total": e.total,
                    "adopted": e.adopted,
                    "dropped": e.dropped,
                    "mutations": list(e.mutations),
                }
                for e in self.epochs
            ],
        }


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
        evaluate,
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
        is confirmed on the full suite before adoption. ``best.json`` is written
        only after this run finishes successfully, and only when the new score
        is not worse than the previous best (stub never replaces a live best).
        Candidate ids are scoped by ``run_id`` so re-optimize never overwrites
        an earlier run's baseline/epoch directories.
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
        baseline_eval = await evaluate(baseline_snap.profile, baseline_id, None)
        best_score = baseline_eval.score
        best_snap = baseline_snap
        best_eval = baseline_eval
        if events is not None:
            events.baseline(candidate_id=baseline_id, score=best_score, total_epochs=max_epochs)

        epochs: list[EpochResult] = []
        working_profile = baseline_snap.profile
        parent_id = baseline_snap.candidate_id

        for epoch in range(1, max_epochs + 1):
            failed = [record for record in best_eval.records if not record.passed]
            if not failed:
                break
            proposal = await self.optimizer.propose(
                working_profile,
                failed,
                instruction=instruction,
                epoch=epoch,
            )
            candidate_id = f"{opt_run}-epoch-{epoch}"
            if proposal.dropped:
                epochs.append(
                    EpochResult(
                        epoch=epoch,
                        candidate_id=candidate_id,
                        score=best_score,
                        passed=best_eval.passed_count,
                        total=best_eval.total_count,
                        adopted=False,
                        mutations=(),
                        dropped=True,
                    )
                )
                if events is not None:
                    events.candidate(
                        epoch=epoch,
                        candidate_id=candidate_id,
                        parent_id=parent_id,
                        score=best_score,
                        best_score=best_score,
                        baseline_score=baseline_eval.score,
                        adopted=False,
                        total_epochs=max_epochs,
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
            failed_ids = [record.case_id for record in failed]
            gated_partial = await evaluate(snap.profile, candidate_id, failed_ids)
            gated = merge_suite(best_eval, gated_partial)
            # Fast reject when the failed subset did not improve the merged score.
            if gated.score <= best_score:
                epochs.append(
                    EpochResult(
                        epoch=epoch,
                        candidate_id=candidate_id,
                        score=gated.score,
                        passed=gated.passed_count,
                        total=gated.total_count,
                        adopted=False,
                        mutations=tuple(m.to_dict() for m in mutations),
                    )
                )
                if events is not None:
                    events.candidate(
                        epoch=epoch,
                        candidate_id=candidate_id,
                        parent_id=parent_id,
                        score=gated.score,
                        best_score=best_score,
                        baseline_score=baseline_eval.score,
                        adopted=False,
                        total_epochs=max_epochs,
                        summary=", ".join(mutation.kind for mutation in mutations),
                    )
                continue

            # Full-suite confirm before adopting (guards regressions on prior passes).
            confirmed = await evaluate(snap.profile, candidate_id, None)
            adopted = confirmed.score > best_score
            report_score = confirmed.score
            epochs.append(
                EpochResult(
                    epoch=epoch,
                    candidate_id=candidate_id,
                    score=report_score,
                    passed=confirmed.passed_count,
                    total=confirmed.total_count,
                    adopted=adopted,
                    mutations=tuple(m.to_dict() for m in mutations),
                )
            )
            if events is not None:
                events.candidate(
                    epoch=epoch,
                    candidate_id=candidate_id,
                    parent_id=parent_id,
                    score=report_score,
                    best_score=confirmed.score if adopted else best_score,
                    baseline_score=baseline_eval.score,
                    adopted=adopted,
                    total_epochs=max_epochs,
                    summary=", ".join(mutation.kind for mutation in mutations),
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
        )
        _write_candidate_score_meta(
            best_snap,
            score=best_score,
            adopted=True,
            best=updated,
        )

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
        )


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
