# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for loading a materialized RSI candidate profile."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.rsi.errors import RsiBadRequest, RsiNotFound
from chrys.service.rsi.snapshot import (
    best_export_path,
    candidates_root,
    load_candidate,
    materialize_candidate,
)
from chrys.service.rsi.store import RsiTaskStore
from tests.support.symlinks import symlink_or_skip

TASK_ID = "task-1"
CANDIDATE_ID = "c1"


def _store(tmp_path: Path) -> RsiTaskStore:
    return RsiTaskStore(tmp_path / "tasks")


def _materialize(store: RsiTaskStore, *, profile_name: str = "RsiCandidate") -> Path:
    snapshot = materialize_candidate(
        store,
        TASK_ID,
        candidate_id=CANDIDATE_ID,
        base_profile=AgentProfile(name=profile_name, instructions=""),
    )
    return snapshot.profile_path


def _set_profile_name(store: RsiTaskStore, profile_name: str) -> None:
    # meta.json is on-disk state a tampered workspace can rewrite; only load_candidate guards it.
    meta_path = candidates_root(store, TASK_ID) / CANDIDATE_ID / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["profile_name"] = profile_name
    meta_path.write_text(json.dumps(meta), encoding="utf-8")


def test_load_candidate_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _materialize(store)
    snapshot = load_candidate(store, TASK_ID, CANDIDATE_ID)
    assert snapshot.profile.name == "RsiCandidate"
    assert snapshot.profile_path.is_file()


@pytest.mark.parametrize("profile_name", ["../escape", "a\\b", "sub/dir", "..", "."])
def test_load_candidate_rejects_path_shaped_names(tmp_path: Path, profile_name: str) -> None:
    store = _store(tmp_path)
    _materialize(store)
    _set_profile_name(store, profile_name)
    with pytest.raises(RsiBadRequest, match="invalid profile_name"):
        load_candidate(store, TASK_ID, CANDIDATE_ID)


@pytest.mark.parametrize("profile_name", ["CON", "foo:bar", "C:foo", "a\x00b"])
def test_load_candidate_rejects_unsafe_filenames(tmp_path: Path, profile_name: str) -> None:
    store = _store(tmp_path)
    _materialize(store)
    _set_profile_name(store, profile_name)
    with pytest.raises(RsiBadRequest, match="invalid profile_name"):
        load_candidate(store, TASK_ID, CANDIDATE_ID)


def test_load_candidate_refuses_yaml_symlink_outside_candidate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    profile_path = _materialize(store)
    outside = tmp_path / "outside.yaml"
    outside.write_text("name: Outside\n", encoding="utf-8")
    profile_path.unlink()
    symlink_or_skip(profile_path, outside)
    with pytest.raises(RsiBadRequest, match="escapes root"):
        load_candidate(store, TASK_ID, CANDIDATE_ID)


def test_load_candidate_missing_yaml_is_not_found(tmp_path: Path) -> None:
    store = _store(tmp_path)
    profile_path = _materialize(store)
    profile_path.unlink()
    with pytest.raises(RsiNotFound, match="profile YAML missing"):
        load_candidate(store, TASK_ID, CANDIDATE_ID)


@pytest.mark.parametrize("profile_name", ["CON", "foo:bar", "..", "a\\b"])
def test_best_export_path_rejects_unsafe_names(tmp_path: Path, profile_name: str) -> None:
    store = _store(tmp_path)
    with pytest.raises(RsiBadRequest, match="invalid profile name for export"):
        best_export_path(store, TASK_ID, profile_name)


def test_best_export_path_accepts_bare_name(tmp_path: Path) -> None:
    store = _store(tmp_path)
    path = best_export_path(store, TASK_ID, "RsiCandidate")
    assert path.name == "RsiCandidate.yaml"
