# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Candidate profile materialization under a task's run/ directory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.agents.serializer import save_profile
from chrys.service.rsi.errors import RsiBadRequest, RsiNotFound
from chrys.service.rsi.mutations import Mutation, apply_mutations, parse_mutation
from chrys.service.rsi.paths import (
    ensure_directory,
    read_json,
    resolve_under,
    task_dir,
    write_json_atomic,
)
from chrys.service.rsi.store import RsiTaskStore

CANDIDATES_DIRNAME = "candidates"
BEST_POINTER_NAME = "best.json"


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
    """One materialized candidate under ``run/candidates/<id>/``."""

    candidate_id: str
    directory: Path
    profile_path: Path
    mutations_path: Path
    meta_path: Path
    profile: AgentProfile
    mutations: tuple[Mutation, ...]
    parent_id: str | None
    epoch: int


def _task_run_dir(store: RsiTaskStore, task_id: str) -> Path:
    return task_dir(store.tasks_root, task_id) / "run"


def candidates_root(store: RsiTaskStore, task_id: str) -> Path:
    """``<task>/run/candidates``."""
    return _task_run_dir(store, task_id) / CANDIDATES_DIRNAME


def best_pointer_path(store: RsiTaskStore, task_id: str) -> Path:
    return _task_run_dir(store, task_id) / BEST_POINTER_NAME


def materialize_candidate(
    store: RsiTaskStore,
    task_id: str,
    *,
    candidate_id: str,
    base_profile: AgentProfile,
    mutations: list[Mutation] | tuple[Mutation, ...] = (),
    parent_id: str | None = None,
    epoch: int = 0,
    score: float | None = None,
    extra_meta: dict[str, Any] | None = None,
) -> CandidateSnapshot:
    """Apply mutations, write profile YAML + mutation JSON under the task run tree."""
    if not candidate_id or "/" in candidate_id or "\\" in candidate_id or ".." in candidate_id:
        raise RsiBadRequest(f"invalid candidate id: {candidate_id!r}")
    root = ensure_directory(candidates_root(store, task_id))
    directory = ensure_directory(root / candidate_id)
    profile = apply_mutations(base_profile, mutations)
    profile_path = save_profile(profile, target_dir=directory)

    mutation_dicts = [m.to_dict() for m in mutations]
    mutations_path = directory / "mutations.json"
    write_json_atomic(mutations_path, {"mutations": mutation_dicts})

    meta: dict[str, Any] = {
        "candidate_id": candidate_id,
        "parent_id": parent_id,
        "epoch": epoch,
        "profile_name": profile.name,
        "profile_path": str(profile_path),
        "mutation_count": len(mutation_dicts),
    }
    if score is not None:
        meta["score"] = score
    if extra_meta:
        meta.update(extra_meta)
    meta_path = directory / "meta.json"
    write_json_atomic(meta_path, meta)

    return CandidateSnapshot(
        candidate_id=candidate_id,
        directory=directory,
        profile_path=profile_path,
        mutations_path=mutations_path,
        meta_path=meta_path,
        profile=profile,
        mutations=tuple(mutations),
        parent_id=parent_id,
        epoch=epoch,
    )


def load_candidate(store: RsiTaskStore, task_id: str, candidate_id: str) -> CandidateSnapshot:
    """Reload a previously materialized candidate."""
    root = candidates_root(store, task_id)
    try:
        directory = resolve_under(root, candidate_id)
    except ValueError as exc:
        raise RsiBadRequest(str(exc)) from exc
    if not directory.is_dir():
        raise RsiNotFound(f"candidate not found: {candidate_id}")
    meta_path = directory / "meta.json"
    if not meta_path.is_file():
        raise RsiNotFound(f"candidate {candidate_id}: meta.json missing")
    meta = read_json(meta_path)
    if not isinstance(meta, dict):
        raise RsiBadRequest(f"candidate {candidate_id}: meta.json must be an object")
    profile_name = str(meta.get("profile_name") or "").strip()
    if not profile_name:
        raise RsiBadRequest(f"candidate {candidate_id}: missing profile_name")
    profile_path = directory / f"{profile_name}.yaml"
    if not profile_path.is_file():
        raise RsiNotFound(f"candidate {candidate_id}: profile YAML missing")
    profile = load_profile_from_yaml(profile_path)
    mutations: list[Mutation] = []
    mutations_path = directory / "mutations.json"
    if mutations_path.is_file():
        raw_mutations = read_json(mutations_path)
        if isinstance(raw_mutations, dict):
            items = raw_mutations.get("mutations")
            if isinstance(items, list):
                mutations = [parse_mutation(item) for item in items if isinstance(item, dict)]
    return CandidateSnapshot(
        candidate_id=candidate_id,
        directory=directory,
        profile_path=profile_path,
        mutations_path=mutations_path,
        meta_path=meta_path,
        profile=profile,
        mutations=tuple(mutations),
        parent_id=str(meta["parent_id"]) if meta.get("parent_id") else None,
        epoch=int(meta.get("epoch") or 0),
    )


def write_best_pointer(
    store: RsiTaskStore,
    task_id: str,
    *,
    candidate_id: str,
    score: float,
    profile_path: Path,
) -> Path:
    """Record the best gated candidate for later install."""
    path = best_pointer_path(store, task_id)
    write_json_atomic(
        path,
        {
            "candidate_id": candidate_id,
            "score": score,
            "profile_path": str(profile_path),
        },
    )
    return path


def read_best_pointer(store: RsiTaskStore, task_id: str) -> dict[str, Any]:
    path = best_pointer_path(store, task_id)
    if not path.is_file():
        raise RsiNotFound(f"task {task_id} has no best candidate yet (run optimize)")
    data = read_json(path)
    if not isinstance(data, dict) or not data.get("candidate_id"):
        raise RsiBadRequest(f"invalid best pointer: {path}")
    return data


def export_profile_yaml(source: Path, destination: Path) -> Path:
    """Copy a candidate profile YAML to an explicit install location."""
    if not source.is_file():
        raise RsiNotFound(f"profile not found: {source}")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    return destination


def best_export_path(store: RsiTaskStore, task_id: str, profile_name: str) -> Path:
    """Default install target: ``<task>/export/<name>.yaml`` (never auto-writes user agents)."""
    export_dir = ensure_directory(task_dir(store.tasks_root, task_id) / "export")
    safe = profile_name.strip() or "candidate"
    if "/" in safe or "\\" in safe or Path(safe).name != safe:
        raise RsiBadRequest(f"invalid profile name for export: {profile_name!r}")
    return export_dir / f"{safe}.yaml"


def dump_best_summary(store: RsiTaskStore, task_id: str) -> dict[str, Any]:
    """JSON-serializable best pointer + mutations."""
    pointer = read_best_pointer(store, task_id)
    candidate_id = str(pointer["candidate_id"])
    snap = load_candidate(store, task_id, candidate_id)
    return {
        "task_id": task_id,
        "candidate_id": candidate_id,
        "score": pointer.get("score"),
        "profile_name": snap.profile.name,
        "profile_path": str(snap.profile_path),
        "mutation_count": len(snap.mutations),
        "mutations": [m.to_dict() for m in snap.mutations],
    }
