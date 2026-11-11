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


def test_judge_response_file_does_not_follow_planted_symlink(tmp_path: Path) -> None:
    from chrys.service.rsi.script_judge import judge_case
    from chrys.service.rsi.workdir_seed import RESPONSE_FILE_NAME
    from tests.support.symlinks import symlink_or_skip

    ds_dir = tmp_path / "dataset"
    ds_dir.mkdir()
    (ds_dir / "check.py").write_text(
        "import os, pathlib, sys\n"
        "sys.exit(0 if pathlib.Path(os.environ['CHRYS_RSI_RESPONSE_PATH']).read_text() == 'answer' else 1)\n",
        encoding="utf-8",
    )
    dataset = ds_dir / "dataset.json"
    dataset.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "judge-symlink",
                "cases": [
                    {
                        "case_id": "c1",
                        "prompt": "hi",
                        "evaluation": {"method": "script_based", "script": "check.py"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    case = load_dataset(dataset).cases[0]
    outside = tmp_path / "user-file.txt"
    outside.write_text("keep me\n", encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    symlink_or_skip(workdir / RESPONSE_FILE_NAME, outside)

    result = judge_case(case, response_text="answer", workdir=workdir, dataset_dir=ds_dir)

    assert result.passed, result.findings
    assert outside.read_text(encoding="utf-8") == "keep me\n"
    assert not (workdir / RESPONSE_FILE_NAME).is_symlink()


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


def test_copy_refuses_gitfile_git_cannot_open(tmp_path: Path) -> None:
    source = tmp_path / "linked"
    source.mkdir()
    (source / "hello.txt").write_text("hi\n", encoding="utf-8")
    (source / ".git").write_text(f"gitdir: {tmp_path / 'missing-repo' / '.git'}\n", encoding="utf-8")
    dest = tmp_path / "private"
    with pytest.raises(RsiBadRequest, match="git cannot open"):
        materialize_workdir(
            WorkdirSeed(source=str(source), strategy="copy"),
            dest=dest,
            dataset_dir=tmp_path,
        )


def test_upsert_rejects_non_ascii_skill_name_like_loader() -> None:
    # Loader ``_SKILL_NAME_RE`` rejects this; the old ``str.islower()`` path accepted it.
    with pytest.raises(RsiBadRequest, match="name is invalid"):
        parse_mutation(
            {
                "kind": "upsert_inline_skill",
                "name": "café-fix",
                "description": "bad",
                "instructions": "x",
            }
        )


def test_create_refuses_script_that_escapes_via_dotdot(tmp_path: Path) -> None:
    # Resolves back inside the dataset dir, so a resolve-then-contain check
    # accepts it; copied to ``input/<script>`` it would land beside ``input/``.
    ds_dir = tmp_path / "dataset"
    ds_dir.mkdir()
    (ds_dir / "check.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    dataset = ds_dir / "dataset.json"
    dataset.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "escape",
                "cases": [
                    {
                        "case_id": "c1",
                        "prompt": "hi",
                        "evaluation": {
                            "method": "script_based",
                            "script": "../dataset/check.py",
                            "expect": {"response_contains": ["ok"]},
                        },
                        "fixture": {"response": "ok"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RsiDatasetInvalid, match="script"):
        load_dataset(dataset)


@pytest.mark.asyncio
async def test_stub_optimize_does_not_replace_live_best(tmp_path: Path) -> None:
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.rsi.paths import write_json_atomic
    from chrys.service.rsi.snapshot import best_pointer_path

    fixture = Path(__file__).parent / "fixtures" / "optimize_dataset.json"
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    created = ctx.create_task(name="live-guard", max_iterations=1, dataset_path=fixture)
    task_id = created["task_id"]
    # Plant a live best that a stub dry-run must not overwrite.
    planted = "planted-live"
    write_json_atomic(
        best_pointer_path(ctx.store, task_id),
        {
            "candidate_id": planted,
            "score": 0.9,
            "profile_path": "/tmp/planted.yaml",
            "kind": "live",
        },
    )
    await ctx.optimize_task(
        task_id,
        base_profile=AgentProfile(name="RsiCandidate", instructions=""),
        max_epochs=1,
    )
    pointer = read_best_pointer(ctx.store, task_id)
    assert pointer["candidate_id"] == planted
    assert pointer.get("kind") == "live"


@pytest.mark.asyncio
async def test_live_optimize_replaces_higher_scoring_stub_best(tmp_path: Path) -> None:
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.rsi.case_runner import CaseRunResult

    fixture = Path(__file__).parent / "fixtures" / "optimize_dataset.json"
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="stub-then-live", max_iterations=1, dataset_path=fixture)["task_id"]
    stub = await ctx.optimize_task(
        task_id,
        base_profile=AgentProfile(name="RsiCandidate", instructions=""),
        max_epochs=1,
    )
    assert stub.best_score == 1.0
    assert read_best_pointer(ctx.store, task_id)["kind"] == "stub"

    class _HalfRunner:
        async def run_case(self, case, workdir: Path) -> CaseRunResult:
            workdir.mkdir(parents=True, exist_ok=True)
            return CaseRunResult(case_id=case.case_id, response_text="ready", workdir=workdir)

    def factory(_profile: AgentProfile) -> _HalfRunner:
        return _HalfRunner()

    live = await ctx.optimize_task(
        task_id,
        base_profile=AgentProfile(name="RsiCandidate", instructions=""),
        max_epochs=1,
        runner_factory=factory,
    )
    assert live.best_score == 0.5
    pointer = read_best_pointer(ctx.store, task_id)
    assert pointer["kind"] == "live"
    assert pointer["candidate_id"] == live.best_candidate_id
    assert str(pointer["candidate_id"]).startswith(f"{live.run_id}-")
    assert live.best_json_updated is True
    assert live.installed_candidate_id == live.best_candidate_id
    assert live.installed_kind == "live"

    exported = ctx.install_best(task_id, destination=tmp_path / "out.yaml")
    assert exported["candidate_id"] == live.best_candidate_id
    assert Path(exported["export_path"]).read_text(encoding="utf-8") == Path(str(pointer["profile_path"])).read_text(
        encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_lower_score_reoptimize_keeps_previous_best(tmp_path: Path) -> None:
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.profiles.agents.serializer import save_profile
    from chrys.service.rsi.case_runner import CaseRunResult
    from chrys.service.rsi.paths import write_json_atomic
    from chrys.service.rsi.snapshot import best_pointer_path, profile_fingerprint

    fixture = Path(__file__).parent / "fixtures" / "optimize_dataset.json"
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    created = ctx.create_task(name="score-guard", max_iterations=1, dataset_path=fixture)
    task_id = created["task_id"]
    base = AgentProfile(name="RsiCandidate", instructions="")
    write_json_atomic(
        best_pointer_path(ctx.store, task_id),
        {
            "candidate_id": "kept-high",
            "score": 1.0,
            "profile_path": "/tmp/high.yaml",
            "kind": "live",
            "base": profile_fingerprint(save_profile(base, target_dir=tmp_path / "base")),
        },
    )

    # A live run from the same base whose agent never answers scores 0.0; the planted live 1.0 stays.
    class _Silent:
        async def run_case(self, case, workdir: Path) -> CaseRunResult:
            workdir.mkdir(parents=True, exist_ok=True)
            return CaseRunResult(case_id=case.case_id, response_text="", workdir=workdir)

    def factory(_profile: AgentProfile) -> _Silent:
        return _Silent()

    result = await ctx.optimize_task(task_id, base_profile=base, max_epochs=1, runner_factory=factory)
    pointer = read_best_pointer(ctx.store, task_id)
    assert pointer["candidate_id"] == "kept-high"
    assert float(pointer["score"]) == 1.0
    assert result.best_json_updated is False
    assert result.installed_candidate_id == "kept-high"
    assert result.best_candidate_id != "kept-high"
    config = ctx.store.get(task_id).config
    assert config["last_optimize_best_json_updated"] is False
    assert config["last_optimize_install_candidate"] == "kept-high"


def test_rsi_main_keyboard_interrupt_returns_130(tmp_path: Path, monkeypatch) -> None:
    from chrys.app.cli import rsi as rsi_cli

    class _Prepared:
        loaded = object()
        settings = object()

    def _prepared() -> _Prepared:
        return _Prepared()

    ctx = build_rsi_service_context(tasks_root=tmp_path)

    def _ctx() -> object:
        return ctx

    def _interrupt() -> list[dict[str, object]]:
        raise KeyboardInterrupt

    monkeypatch.setattr(rsi_cli, "_prepare_runtime", _prepared)
    monkeypatch.setattr(rsi_cli, "build_rsi_service_context", _ctx)
    # Ctrl-C from inside the command body, not a patched dispatcher seam.
    monkeypatch.setattr(ctx, "list_tasks", _interrupt)
    assert rsi_cli.main(["list"]) == 130


def test_busy_lock_refuses_concurrent_optimize(tmp_path: Path) -> None:
    import threading

    from chrys.foundation.util.lock import FileLock

    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    created = ctx.create_task(name="busy")
    task_id = created["task_id"]
    lock_path = ctx.busy_lock_path(task_id)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    held = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        with FileLock(lock_path, timeout=5):
            held.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=_holder)
    thread.start()
    assert held.wait(timeout=5)
    try:
        with pytest.raises(RsiBadRequest, match="active eval, optimize or start"), ctx._task_busy_lock(task_id):
            pass
        with pytest.raises(RsiBadRequest, match="active eval, optimize or start"):
            ctx.delete_task(task_id)
        assert ctx.store.get(task_id).task_id == task_id
    finally:
        release.set()
        thread.join(timeout=5)


def test_busy_lock_body_timeout_is_not_reported_as_busy(tmp_path: Path) -> None:
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="busy-body")["task_id"]
    with pytest.raises(TimeoutError, match="from the body"), ctx._task_busy_lock(task_id):
        raise TimeoutError("from the body")
    # The lock was released: taking it again succeeds.
    with ctx._task_busy_lock(task_id):
        pass


def test_busy_lock_after_delete_refuses_the_removed_task(tmp_path: Path) -> None:
    from chrys.service.rsi.errors import RsiTaskNotFound

    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="gone")["task_id"]
    ctx.delete_task(task_id)
    with pytest.raises(RsiTaskNotFound), ctx._task_busy_lock(task_id):
        pass
    assert not (tmp_path / "tasks" / task_id).exists()
