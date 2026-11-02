# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RSI composition root: store, projector, worker, thin services."""

from __future__ import annotations

import json
import shutil
from collections.abc import Collection
from pathlib import Path
from typing import Any

from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.rsi.case_runner import CaseRunner, ProfileAwareStubRunner, StubCaseRunner
from chrys.service.rsi.dataset import load_dataset, subset_dataset
from chrys.service.rsi.errors import RsiBadRequest
from chrys.service.rsi.evaluator import EvalSuiteResult, ScriptBasedEvaluator
from chrys.service.rsi.models import (
    RsiTask,
    Scenario,
    TaskStatus,
    generate_task_id,
    utcnow_iso,
)
from chrys.service.rsi.optimizer import (
    MutationOptimizer,
    OptimizeEvents,
    OptimizeLoop,
    OptimizeResult,
    StubOptimizer,
)
from chrys.service.rsi.paths import resolve_rsi_tasks_root, task_dir
from chrys.service.rsi.projector import RsiProjector
from chrys.service.rsi.provider import MockRsiProvider, RsiProvider
from chrys.service.rsi.snapshot import (
    best_export_path,
    dump_best_summary,
    export_profile_yaml,
    load_candidate,
    read_best_pointer,
)
from chrys.service.rsi.store import RsiTaskStore
from chrys.service.rsi.workdir_seed import snapshot_seed_for_task
from chrys.service.rsi.worker import PushCallback, RsiWorker


class RsiServiceContext:
    """Assembled RSI service domain for one tasks root."""

    def __init__(
        self,
        tasks_root: Path,
        *,
        provider: RsiProvider | None = None,
        push_callbacks: dict[str, PushCallback] | None = None,
    ) -> None:
        self.tasks_root = Path(tasks_root)
        self.store = RsiTaskStore(self.tasks_root)
        self.projector = RsiProjector(self.tasks_root)
        self.provider = provider or MockRsiProvider()
        self.worker = RsiWorker(
            store=self.store,
            provider=self.provider,
            projector=self.projector,
            push_callbacks=push_callbacks,
        )
        if push_callbacks and "rsi.training.status.changed" in push_callbacks:
            status_cb = push_callbacks["rsi.training.status.changed"]

            def _hook(task: RsiTask, previous: str | None) -> None:
                # Status hooks are synchronous (store holds no event loop).
                status_cb(
                    "rsi.training.status.changed",
                    task.task_id,
                    {
                        "task_id": task.task_id,
                        "status": task.status,
                        "previous_status": previous,
                    },
                )

            self.store.add_status_hook(_hook)

    def create_task(
        self,
        *,
        name: str,
        max_iterations: int = 1,
        optimization_instruction: str | None = None,
        model_refs: dict[str, Any] | None = None,
        dataset_path: str | Path | None = None,
    ) -> dict[str, Any]:
        cleaned = str(name or "").strip()
        if not cleaned:
            raise RsiBadRequest("name is required")
        if max_iterations < 1:
            raise RsiBadRequest("max_iterations must be >= 1")
        dataset = load_dataset(dataset_path) if dataset_path is not None else None
        task_id = generate_task_id()
        task = RsiTask(
            task_id=task_id,
            name=cleaned,
            scenario=Scenario.PROFILE.value,
            status=TaskStatus.CREATED.value,
            created_at=utcnow_iso(),
            max_iterations=int(max_iterations),
            optimization_instruction=(optimization_instruction or None),
            model_refs=dict(model_refs or {}),
        )
        created = self.store.create(task)
        if dataset is not None:
            input_dir = task_dir(self.tasks_root, created.task_id) / "input"
            input_dir.mkdir(parents=True, exist_ok=True)
            target = input_dir / "dataset.json"
            payload = json.loads(dataset.source_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise RsiBadRequest("dataset root must be an object")
            cases_raw = payload.get("cases")
            if not isinstance(cases_raw, list):
                raise RsiBadRequest("dataset cases must be an array")
            dataset_dir = dataset.source_path.parent
            # Copy sibling check scripts referenced by the dataset.
            for case in dataset.cases:
                if not case.script:
                    continue
                src = dataset_dir / case.script
                dest = input_dir / case.script
                dest.parent.mkdir(parents=True, exist_ok=True)
                if src.is_file():
                    shutil.copy2(src, dest)
            # Snapshot workdir seeds into the task so later eval never opens the
            # caller's original tree in-place; rewrite cases to copy from seeds/.
            for case, case_raw in zip(dataset.cases, cases_raw, strict=True):
                if not isinstance(case_raw, dict):
                    raise RsiBadRequest("dataset case must be an object")
                if case.workdir is None:
                    continue
                seed_dest = input_dir / "seeds" / case.case_id
                rewritten = snapshot_seed_for_task(
                    case.workdir,
                    dest=seed_dest,
                    dataset_dir=dataset_dir,
                )
                case_raw["workdir"] = rewritten.to_dict()
            target.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
            created.input_file = str(target.resolve())
            created.config = {
                **dict(created.config or {}),
                "dataset_id": dataset.dataset_id,
                "sample_count": dataset.sample_count,
            }
            created = self.store.save(created)
        self.projector.register_root(created.task_id)
        return {
            "task_id": created.task_id,
            "status": created.status,
            "input_file": created.input_file,
            "dataset_id": (created.config or {}).get("dataset_id"),
        }

    def list_tasks(self) -> list[dict[str, Any]]:
        return [task.list_projection() for task in self.store.list_tasks()]

    def get_task(self, task_id: str) -> dict[str, Any]:
        task = self.store.get(task_id)
        self.projector.load_from_disk(task_id)
        return task.get_projection(progress=self.projector.derive_progress(task_id))

    def delete_task(self, task_id: str) -> None:
        self.store.delete(task_id)

    def start(self, task_id: str) -> dict[str, Any]:
        status = self.worker.enqueue(task_id)
        return {"task_id": task_id, "status": status}

    def terminate(self, task_id: str) -> dict[str, Any]:
        status = self.worker.terminate(task_id)
        return {"task_id": task_id, "status": status}

    def get_tree(self, task_id: str) -> dict[str, Any]:
        self.store.get(task_id)  # raise if missing
        self.projector.load_from_disk(task_id)
        return self.projector.derive_tree(task_id)

    async def evaluate_dataset(
        self,
        dataset_path: str | Path,
        *,
        runner: CaseRunner | None = None,
        output_dir: Path | None = None,
        task_id: str = "",
        project: bool = True,
        case_ids: Collection[str] | None = None,
    ) -> EvalSuiteResult:
        """Run Phase 2 script_based evaluation and write scorecard artifacts.

        ``project=False`` still writes the scorecard but does not add per-case
        nodes. Optimize uses that so the tree stays candidate adopt/reject.
        """
        dataset = load_dataset(dataset_path)
        if case_ids is not None:
            dataset = subset_dataset(dataset, case_ids)
        out = Path(output_dir) if output_dir is not None else self.tasks_root / "_evals"
        evaluator = ScriptBasedEvaluator(runner or StubCaseRunner())
        project_task = task_id if project else ""

        async def on_event(event: Any) -> None:
            if event.is_node_created:
                self.projector.on_node_created(project_task or event.task_id or "adhoc", event.payload)
            elif event.is_progress_metric and (project_task or event.task_id):
                self.projector.on_progress_metric(project_task or event.task_id, event.payload)

        if project_task:
            self.projector.register_root(project_task)
        return await evaluator.run_suite(
            dataset,
            output_dir=out,
            on_event=on_event if project_task else None,
            task_id=project_task,
        )

    async def evaluate_task(self, task_id: str, *, runner: CaseRunner | None = None) -> EvalSuiteResult:
        task = self.store.get(task_id)
        dataset_path = task.input_file
        if not dataset_path:
            raise RsiBadRequest(f"task {task_id} has no dataset; create with --dataset")
        result = await self.evaluate_dataset(
            dataset_path,
            runner=runner,
            output_dir=Path(task.run_dir) / "evaluations",
            task_id=task_id,
        )
        task.config = {
            **dict(task.config or {}),
            "last_eval_run_id": result.run_id,
            "last_eval_score": result.score,
            "last_eval_passed": result.passed_count,
            "last_eval_total": result.total_count,
            "last_eval_scorecard": str(result.scorecard_path),
        }
        self.store.save(task)
        return result

    async def optimize_task(
        self,
        task_id: str,
        *,
        base_profile: AgentProfile | None = None,
        max_epochs: int | None = None,
        optimizer: MutationOptimizer | None = None,
        runner_factory=None,
    ) -> OptimizeResult:
        """Gate profile/skills mutations on dataset score (Phase 3).

        Default stub path uses :class:`ProfileAwareStubRunner` so markers from
        :class:`StubOptimizer` improve scores without a live model. A caller-supplied
        optimizer (the LLM proposer) and ``runner_factory`` (headless cases) replace
        that path. Candidate adopt/reject nodes land on the task tree either way.
        """
        task = self.store.get(task_id)
        dataset_path = task.input_file
        if not dataset_path:
            raise RsiBadRequest(f"task {task_id} has no dataset; create with --dataset")
        profile = base_profile or AgentProfile(
            name="RsiCandidate",
            display_name="RSI Candidate",
            description="RSI optimize base profile",
            instructions="",
        )
        epochs = int(max_epochs) if max_epochs is not None else max(1, int(task.max_iterations))
        loop = OptimizeLoop(self.store, optimizer=optimizer or StubOptimizer())
        events: OptimizeEvents = _OptimizeTreeEvents(self, task_id)

        async def evaluate(candidate: AgentProfile, label: str, case_ids: Collection[str] | None) -> EvalSuiteResult:
            runner = runner_factory(candidate) if runner_factory is not None else ProfileAwareStubRunner(candidate)
            out = Path(task.run_dir) / "evaluations" / label
            return await self.evaluate_dataset(
                dataset_path,
                runner=runner,
                output_dir=out,
                task_id=task_id,
                project=False,
                case_ids=case_ids,
            )

        result = await loop.run(
            task_id,
            base_profile=profile,
            evaluate=evaluate,
            max_epochs=epochs,
            instruction=task.optimization_instruction,
            events=events,
        )
        task.config = {
            **dict(task.config or {}),
            "last_optimize_best_candidate": result.best_candidate_id,
            "last_optimize_best_score": result.best_score,
            "last_optimize_baseline_score": result.baseline_score,
            "last_optimize_perfect": result.perfect,
        }
        self.store.save(task)
        return result

    def record_optimize_node(
        self,
        task_id: str,
        *,
        ref: str,
        parent_ref: str,
        adopted: bool,
        score: float,
        summary: str,
        epoch: int,
        total_epochs: int,
        best_score: float,
        baseline_score: float,
    ) -> None:
        """Project one optimize candidate. Progress is persisted with the node."""
        self.projector.register_root(task_id)
        self.projector.on_progress_metric(
            task_id,
            {
                "iteration": epoch,
                "total_iterations": total_epochs,
                "score": best_score,
                "baseline": baseline_score,
            },
        )
        self.projector.on_node_created(
            task_id,
            {
                "node": {
                    "ref": ref,
                    "parent_ref": parent_ref,
                    "type": "ADOPTED" if adopted else "REJECTED",
                    "accepted": adopted,
                    "adopted": adopted,
                    "score": score,
                    "summary": summary,
                }
            },
        )

    def install_best(
        self,
        task_id: str,
        *,
        destination: Path | None = None,
        user_agents_dir: Path | None = None,
        write_user: bool = False,
    ) -> dict[str, Any]:
        """Export the best gated candidate YAML.

        Default writes ``<task>/export/<name>.yaml``. Writing into the user
        agents directory requires ``write_user=True`` and an explicit dir
        (never implicit).
        """
        self.store.get(task_id)  # raise if missing
        pointer = read_best_pointer(self.store, task_id)
        snap = load_candidate(self.store, task_id, str(pointer["candidate_id"]))
        if write_user:
            if user_agents_dir is None:
                raise RsiBadRequest("write_user requires an explicit user_agents_dir")
            dest = Path(user_agents_dir) / f"{snap.profile.name}.yaml"
        elif destination is not None:
            dest = Path(destination)
        else:
            dest = best_export_path(self.store, task_id, snap.profile.name)
        written = export_profile_yaml(snap.profile_path, dest)
        summary = dump_best_summary(self.store, task_id)
        summary["export_path"] = str(written)
        summary["wrote_user_agents"] = bool(write_user)
        return summary

    async def wait_idle(self) -> None:
        await self.worker.drain()


class _OptimizeTreeEvents:
    """Adopt/reject nodes for one optimize run. Methods stay synchronous."""

    def __init__(self, ctx: RsiServiceContext, task_id: str) -> None:
        self._ctx = ctx
        self._task_id = task_id

    def baseline(self, *, score: float, total_epochs: int) -> None:
        self._ctx.record_optimize_node(
            self._task_id,
            ref="baseline",
            parent_ref="root",
            adopted=True,
            score=score,
            summary="scored baseline",
            epoch=0,
            total_epochs=total_epochs,
            best_score=score,
            baseline_score=score,
        )

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
    ) -> None:
        self._ctx.record_optimize_node(
            self._task_id,
            ref=candidate_id,
            parent_ref=parent_id,
            adopted=adopted,
            score=score,
            summary=summary or candidate_id,
            epoch=epoch,
            total_epochs=total_epochs,
            best_score=best_score,
            baseline_score=baseline_score,
        )


def build_rsi_service_context(
    tasks_root: Path | None = None,
    *,
    config_dir: Path | None = None,
    provider: RsiProvider | None = None,
    push_callbacks: dict[str, PushCallback] | None = None,
    create: bool = True,
) -> RsiServiceContext:
    """Build the RSI composition root under the config dir (or an explicit root)."""
    root = Path(tasks_root) if tasks_root is not None else resolve_rsi_tasks_root(config_dir, create=create)
    return RsiServiceContext(root, provider=provider, push_callbacks=push_callbacks)


__all__ = [
    "RsiServiceContext",
    "build_rsi_service_context",
    "resolve_rsi_tasks_root",
]
