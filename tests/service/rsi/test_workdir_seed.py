# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workdir seed materialization for RSI cases."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from chrys.service.rsi import RsiDatasetInvalid, build_rsi_service_context, load_dataset
from chrys.service.rsi.case_runner import StubCaseRunner
from chrys.service.rsi.errors import RsiBadRequest
from chrys.service.rsi.workdir_seed import WorkdirSeed, materialize_workdir, release_workdir_seeds_under
from tests.support.symlinks import symlink_or_skip


def _write_dataset(path: Path, *, workdir: dict[str, object] | None = None) -> Path:
    case: dict[str, object] = {
        "case_id": "feature",
        "prompt": "Add a greeting file",
        "evaluation": {
            "method": "script_based",
            "expect": {"files_exist": ["hello.txt"], "file_contains": {"hello.txt": ["hello"]}},
        },
        "fixture": {"response": "done", "files": {"hello.txt": "hello\n"}},
    }
    if workdir is not None:
        case["workdir"] = workdir
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "dataset_id": "seed-demo",
                "cases": [case],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_parse_workdir_seed(tmp_path: Path) -> None:
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "README").write_text("x\n", encoding="utf-8")
    path = _write_dataset(
        tmp_path / "dataset.json",
        workdir={"from": "seed", "strategy": "copy"},
    )
    dataset = load_dataset(path)
    assert dataset.cases[0].workdir == WorkdirSeed(source="seed", strategy="copy", ref=None)


def test_parse_workdir_rejects_bad_strategy(tmp_path: Path) -> None:
    path = _write_dataset(
        tmp_path / "dataset.json",
        workdir={"from": "seed", "strategy": "rsync"},
    )
    with pytest.raises(RsiDatasetInvalid, match=r"workdir\.strategy"):
        load_dataset(path)


def test_copy_seed_does_not_modify_source(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    marker = source / "keep.txt"
    marker.write_text("original\n", encoding="utf-8")
    dest = tmp_path / "dest"
    materialize_workdir(
        WorkdirSeed(source=str(source), strategy="copy"),
        dest=dest,
        dataset_dir=tmp_path,
    )
    (dest / "keep.txt").write_text("changed\n", encoding="utf-8")
    assert marker.read_text(encoding="utf-8") == "original\n"
    assert (dest / "keep.txt").read_text(encoding="utf-8") == "changed\n"


def test_copy_refuses_escaping_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("nope\n", encoding="utf-8")
    source = tmp_path / "src"
    source.mkdir()
    symlink_or_skip(source / "leak", outside)
    with pytest.raises(RsiBadRequest, match="symlink escaping"):
        materialize_workdir(
            WorkdirSeed(source=str(source), strategy="copy"),
            dest=tmp_path / "dest",
            dataset_dir=tmp_path,
        )


def test_git_clone_seed_isolates_source(tmp_path: Path, git_repo_factory) -> None:
    source = git_repo_factory(tmp_path / "repo")
    (source / "app.py").write_text("print('hi')\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "app.py"],
        cwd=source,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "app"],
        cwd=source,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    dest = tmp_path / "clone"
    materialize_workdir(
        WorkdirSeed(source=str(source), strategy="git_clone"),
        dest=dest,
        dataset_dir=tmp_path,
    )
    (dest / "app.py").write_text("print('changed')\n", encoding="utf-8")
    assert (source / "app.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_git_worktree_seed_and_release(tmp_path: Path, git_repo_factory) -> None:
    source = git_repo_factory(tmp_path / "repo")
    (source / "app.py").write_text("print('hi')\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "app.py"],
        cwd=source,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "app"],
        cwd=source,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    dest = tmp_path / "wt"
    materialize_workdir(
        WorkdirSeed(source=str(source), strategy="git_worktree"),
        dest=dest,
        dataset_dir=tmp_path,
    )
    assert (dest / "app.py").is_file()
    (dest / "app.py").write_text("print('wt')\n", encoding="utf-8")
    assert (source / "app.py").read_text(encoding="utf-8") == "print('hi')\n"
    release_workdir_seeds_under(tmp_path)
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=source,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert str(dest.resolve()) not in listed.stdout


@pytest.mark.asyncio
async def test_create_task_snapshots_seed_and_eval_uses_copy(tmp_path: Path) -> None:
    seed = tmp_path / "project"
    seed.mkdir()
    (seed / "base.txt").write_text("base\n", encoding="utf-8")
    dataset_path = _write_dataset(
        tmp_path / "dataset.json",
        workdir={"from": str(seed), "strategy": "copy"},
    )
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    created = ctx.create_task(name="seeded", dataset_path=dataset_path)
    task_dataset = Path(created["input_file"])
    loaded = json.loads(task_dataset.read_text(encoding="utf-8"))
    workdir_field = loaded["cases"][0]["workdir"]
    assert workdir_field["strategy"] == "copy"
    assert Path(workdir_field["from"]).is_dir()
    assert (Path(workdir_field["from"]) / "base.txt").is_file()
    # Mutating the caller's seed after create must not affect the snapshot.
    (seed / "base.txt").write_text("mutated\n", encoding="utf-8")

    result = await ctx.evaluate_task(created["task_id"], runner=StubCaseRunner())
    assert result.passed_count == 1
    case_workdir = Path(result.records[0].workdir)
    assert (case_workdir / "base.txt").read_text(encoding="utf-8") == "base\n"
    assert (case_workdir / "hello.txt").read_text(encoding="utf-8") == "hello\n"


def _commit_file(repo: Path, rel: str, content: str, message: str) -> None:
    (repo / rel).write_text(content, encoding="utf-8")
    subprocess.run(
        ["git", "add", rel],
        cwd=repo,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=repo,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )


@pytest.mark.asyncio
async def test_task_path_git_clone_keeps_private_git(tmp_path: Path, git_repo_factory) -> None:
    """create --dataset with git_clone must still have .git after rewrite-to-copy."""
    import asyncio

    source = git_repo_factory(tmp_path / "repo")
    await asyncio.to_thread(_commit_file, source, "app.py", "print('hi')\n", "app")
    # Add a remote that must be stripped from the private copy.
    await asyncio.to_thread(
        subprocess.run,
        ["git", "remote", "add", "origin", str(source)],
        cwd=source,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    dataset_path = _write_dataset(
        tmp_path / "dataset.json",
        workdir={"from": str(source), "strategy": "git_clone"},
    )
    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    created = ctx.create_task(name="git-seed", dataset_path=dataset_path)
    task_dataset = Path(created["input_file"])
    workdir_field = json.loads(task_dataset.read_text(encoding="utf-8"))["cases"][0]["workdir"]
    seed_dest = Path(workdir_field["from"])
    assert workdir_field["strategy"] == "copy"
    assert (seed_dest / ".git").is_dir()
    remotes = await asyncio.to_thread(
        subprocess.run,
        ["git", "remote"],
        cwd=seed_dest,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert remotes.stdout.strip() == ""

    result = await ctx.evaluate_task(created["task_id"], runner=StubCaseRunner())
    case_workdir = Path(result.records[0].workdir)
    assert (case_workdir / ".git").is_dir()
    case_remotes = await asyncio.to_thread(
        subprocess.run,
        ["git", "remote"],
        cwd=case_workdir,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert case_remotes.stdout.strip() == ""


def test_nested_gitfile_disconnected_on_copy(tmp_path: Path) -> None:
    source = tmp_path / "src"
    nested = source / ".worktrees" / "feature"
    nested.mkdir(parents=True)
    (source / "root.txt").write_text("r\n", encoding="utf-8")
    (nested / "leaf.txt").write_text("l\n", encoding="utf-8")
    (nested / ".git").write_text("gitdir: /tmp/user-repo/.git/worktrees/feature\n", encoding="utf-8")
    dest = tmp_path / "dest"
    materialize_workdir(
        WorkdirSeed(source=str(source), strategy="copy"),
        dest=dest,
        dataset_dir=tmp_path,
    )
    assert (dest / ".worktrees" / "feature" / "leaf.txt").is_file()
    assert not (dest / ".worktrees" / "feature" / ".git").exists()


def test_marker_write_does_not_follow_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "external.json"
    outside.write_text('{"keep": true}\n', encoding="utf-8")
    source = tmp_path / "src"
    source.mkdir()
    (source / "ok.txt").write_text("x\n", encoding="utf-8")
    dest = tmp_path / "dest"
    dest.mkdir()
    # Pre-plant a symlink at the marker path; materialize requires dest absent,
    # so plant the symlink on a nested path by calling _write_marker after copy.
    from chrys.service.rsi.workdir_seed import _SEED_MARKER, _write_marker

    materialize_workdir(
        WorkdirSeed(source=str(source), strategy="copy"),
        dest=dest / "tree",
        dataset_dir=tmp_path,
    )
    marker = dest / "tree" / _SEED_MARKER
    marker.unlink(missing_ok=True)
    symlink_or_skip(marker, outside)
    _write_marker(dest / "tree", strategy="copy", source=source)
    assert outside.read_text(encoding="utf-8") == '{"keep": true}\n'
    assert marker.is_file() and not marker.is_symlink()
    assert "strategy" in marker.read_text(encoding="utf-8")


def test_git_clone_removes_origin(tmp_path: Path, git_repo_factory) -> None:
    source = git_repo_factory(tmp_path / "repo")
    _commit_file(source, "a.txt", "a\n", "a")
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.invalid/repo.git"],
        cwd=source,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
    )
    dest = tmp_path / "clone"
    materialize_workdir(
        WorkdirSeed(source=str(source), strategy="git_clone"),
        dest=dest,
        dataset_dir=tmp_path,
    )
    remotes = subprocess.run(
        ["git", "remote"],
        cwd=dest,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert remotes.stdout.strip() == ""
    assert (dest / ".git").is_dir()
