# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression pins for PR #162 owner-review hardening."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chrys.service.rsi import RsiBadRequest, RsiDatasetInvalid, build_rsi_service_context, load_dataset
from chrys.service.rsi.mutations import parse_mutation
from chrys.service.rsi.paths import is_forbidden_absolute_relpath, safe_relpath_under, validate_case_id
from chrys.service.rsi.snapshot import export_profile_yaml, read_best_pointer
from chrys.service.rsi.workdir_seed import WorkdirSeed, materialize_workdir


def test_validate_case_id_rejects_dotdot() -> None:
    with pytest.raises(ValueError, match="invalid case_id"):
        validate_case_id("..")
    with pytest.raises(ValueError, match="invalid case_id"):
        validate_case_id("../x")
    assert validate_case_id("complex-calc") == "complex-calc"


def test_forbidden_windows_and_unc_paths() -> None:
    assert is_forbidden_absolute_relpath("C:/Windows/System32")
    assert is_forbidden_absolute_relpath("c:foo")
    assert is_forbidden_absolute_relpath("//server/share")
    assert is_forbidden_absolute_relpath("/etc/passwd")
    assert not is_forbidden_absolute_relpath("hello.txt")


def test_safe_relpath_under_refuses_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ValueError):
        safe_relpath_under(root, "../outside")
    with pytest.raises(ValueError):
        safe_relpath_under(root, "C:/abs")
    nested = safe_relpath_under(root, "a/b.txt")
    assert nested == (root / "a" / "b.txt").resolve()


def test_dataset_rejects_absolute_files_exist_and_bad_case_id(tmp_path: Path) -> None:
    path = tmp_path / "ds.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "bad-paths",
                "cases": [
                    {
                        "case_id": "..",
                        "prompt": "x",
                        "evaluation": {
                            "method": "script_based",
                            "expect": {"files_exist": ["C:/Windows/win.ini"]},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RsiDatasetInvalid, match="case_id"):
        load_dataset(path)

    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "bad-paths",
                "cases": [
                    {
                        "case_id": "ok",
                        "prompt": "x",
                        "evaluation": {
                            "method": "script_based",
                            "expect": {"files_exist": ["C:/Windows/win.ini"]},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RsiDatasetInvalid, match="files_exist"):
        load_dataset(path)


def test_dataset_rejects_casefold_collision(tmp_path: Path) -> None:
    path = tmp_path / "fold.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "fold",
                "cases": [
                    {
                        "case_id": "Hello",
                        "prompt": "a",
                        "evaluation": {"method": "script_based", "expect": {"response_contains": ["a"]}},
                    },
                    {
                        "case_id": "hello",
                        "prompt": "b",
                        "evaluation": {"method": "script_based", "expect": {"response_contains": ["b"]}},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RsiDatasetInvalid, match="case-insensitive"):
        load_dataset(path)


def test_upsert_inline_skill_requires_description() -> None:
    with pytest.raises(RsiBadRequest, match="description"):
        parse_mutation(
            {
                "kind": "upsert_inline_skill",
                "name": "rsi-fix",
                "description": "   ",
                "instructions": "x",
            }
        )
    with pytest.raises(RsiBadRequest, match="1024"):
        parse_mutation(
            {
                "kind": "upsert_inline_skill",
                "name": "rsi-fix",
                "description": "x" * 1025,
                "instructions": "x",
            }
        )


def test_create_copies_script_under_input_not_escape(tmp_path: Path) -> None:
    ds_dir = tmp_path / "dataset"
    ds_dir.mkdir()
    (ds_dir / "check.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    dataset = ds_dir / "dataset.json"
    dataset.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "copy-script",
                "cases": [
                    {
                        "case_id": "c1",
                        "prompt": "hi",
                        "evaluation": {
                            "method": "script_based",
                            "script": "check.py",
                            "expect": {"response_contains": ["ok"]},
                        },
                        "fixture": {"response": "ok"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    created = ctx.create_task(name="t", dataset_path=dataset)
    input_dir = Path(created["input_file"]).parent
    assert (input_dir / "check.py").is_file()
    assert input_dir.resolve().is_relative_to((tmp_path / "tasks").resolve())


def test_export_profile_yaml_owner_only_refuses_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "src.yaml"
    source.write_text("name: X\n", encoding="utf-8")
    dest = tmp_path / "out.yaml"
    export_profile_yaml(source, dest)
    assert dest.is_file()
    with pytest.raises(RsiBadRequest, match="already exists"):
        export_profile_yaml(source, dest)
    export_profile_yaml(source, dest, overwrite=True)


@pytest.mark.asyncio
async def test_optimize_run_scoped_candidates_and_deferred_best(tmp_path: Path) -> None:
    from chrys.service.profiles.agents.schema import AgentProfile

    fixture = Path(__file__).parent / "fixtures" / "optimize_dataset.json"
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    created = ctx.create_task(name="scoped", max_iterations=1, dataset_path=fixture)
    task_id = created["task_id"]
    first = await ctx.optimize_task(
        task_id,
        base_profile=AgentProfile(name="RsiCandidate", instructions=""),
        max_epochs=1,
    )
    first_best = str(read_best_pointer(ctx.store, task_id)["candidate_id"])
    assert first.run_id
    assert first_best.startswith(f"{first.run_id}-")
    assert not (Path(ctx.store.get(task_id).run_dir) / "candidates" / "baseline").exists()

    second = await ctx.optimize_task(
        task_id,
        base_profile=AgentProfile(name="RsiCandidate", instructions=""),
        max_epochs=1,
    )
    second_best = str(read_best_pointer(ctx.store, task_id)["candidate_id"])
    assert second.run_id != first.run_id
    # First run artifacts remain on disk.
    assert (Path(ctx.store.get(task_id).run_dir) / "candidates" / f"{first.run_id}-baseline").is_dir()
    assert second_best.startswith(f"{second.run_id}-")


def test_copy_linked_gitfile_is_detached(tmp_path: Path) -> None:
    source = tmp_path / "linked"
    source.mkdir()
    (source / "hello.txt").write_text("hi\n", encoding="utf-8")
    # Simulate a linked worktree gitfile pointing outside.
    (source / ".git").write_text("gitdir: /tmp/real-repo/.git/worktrees/x\n", encoding="utf-8")
    dest = tmp_path / "private"
    materialize_workdir(
        WorkdirSeed(source=str(source), strategy="copy"),
        dest=dest,
        dataset_dir=tmp_path,
    )
    assert (dest / "hello.txt").is_file()
    assert not (dest / ".git").exists()
