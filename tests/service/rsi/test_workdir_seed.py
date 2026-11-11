# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workdir seed materialization for RSI cases."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import types
from pathlib import Path

import pytest

from chrys.service.rsi import RsiDatasetInvalid, build_rsi_service_context, load_dataset
from chrys.service.rsi import workdir_seed as workdir_seed_module
from chrys.service.rsi.case_runner import StubCaseRunner
from chrys.service.rsi.errors import RsiBadRequest
from chrys.service.rsi.workdir_seed import WorkdirSeed, materialize_workdir, write_text_nofollow
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


def test_git_worktree_seed_is_a_private_clone(tmp_path: Path, git_repo_factory) -> None:
    source = git_repo_factory(tmp_path / "repo")
    _commit_file(source, "app.py", "print('hi')\n", "app")
    _git(source, "remote", "add", "origin", "https://example.invalid/repo.git")
    worktrees_before = _git(source, "worktree", "list", "--porcelain").stdout
    dest = tmp_path / "wt"
    materialize_workdir(
        WorkdirSeed(source=str(source), strategy="git_worktree"),
        dest=dest,
        dataset_dir=tmp_path,
    )
    assert (dest / "app.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert (dest / ".git").is_dir()
    assert _remotes(dest) == []
    assert _git(source, "worktree", "list", "--porcelain").stdout == worktrees_before
    _git(dest, "branch", "case-only")
    assert "case-only" not in _git(source, "branch", "--list").stdout
    (dest / "app.py").write_text("print('wt')\n", encoding="utf-8")
    assert (source / "app.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_delete_never_runs_git_against_a_repo_named_in_a_task_file(tmp_path: Path, git_repo_factory) -> None:
    user_repo = git_repo_factory(tmp_path / "user-repo")
    _commit_file(user_repo, "a.txt", "a\n", "a")
    unmounted = tmp_path / "unmounted-disk" / "wt"
    _git(user_repo, "worktree", "add", "--detach", str(unmounted))
    shutil.rmtree(unmounted.parent)
    worktrees_before = _git(user_repo, "worktree", "list", "--porcelain").stdout
    assert str(unmounted) in worktrees_before or str(unmounted.resolve()) in worktrees_before

    ctx = build_rsi_service_context(tasks_root=tmp_path / "tasks")
    task_id = ctx.create_task(name="planted")["task_id"]
    case_dir = tmp_path / "tasks" / task_id / "run" / "evaluations" / "x" / "eval-1" / "case_workspaces" / "c"
    case_dir.mkdir(parents=True)
    # What an agent running without approval prompts could write into its own case folder.
    (case_dir / ".chrys_rsi_workdir_seed.json").write_text(
        json.dumps({"strategy": "git_worktree", "source": str(user_repo)}), encoding="utf-8"
    )
    ctx.delete_task(task_id)
    assert not (tmp_path / "tasks" / task_id).exists()
    assert _git(user_repo, "worktree", "list", "--porcelain").stdout == worktrees_before


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


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args],
        cwd=cwd,
        check=check,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _remotes(repo: Path) -> list[str]:
    return _git(repo, "remote").stdout.split()


def _copy_seed(source: Path, dest: Path, tmp_path: Path) -> Path:
    return materialize_workdir(WorkdirSeed(source=str(source), strategy="copy"), dest=dest, dataset_dir=tmp_path)


def test_copy_rebuilds_nested_repo_without_remotes(tmp_path: Path, git_repo_factory) -> None:
    upstream = git_repo_factory(tmp_path / "userlib-upstream")
    source = git_repo_factory(tmp_path / "src")
    nested = source / "deps" / "userlib"
    nested.parent.mkdir()
    _git(tmp_path, "clone", "--quiet", str(upstream), str(nested))
    assert _remotes(nested) == ["origin"]
    upstream_head = _git(upstream, "rev-parse", "HEAD").stdout.strip()

    dest = _copy_seed(source, tmp_path / "dest", tmp_path)

    copied = dest / "deps" / "userlib"
    assert (copied / ".git").is_dir()
    assert _remotes(copied) == []
    assert _remotes(dest) == []
    assert _git(copied, "rev-parse", "HEAD").stdout.strip() == upstream_head
    assert _git(copied, "push", "--force", "origin", "HEAD:main", check=False).returncode != 0
    assert _git(upstream, "rev-parse", "HEAD").stdout.strip() == upstream_head


def test_copy_rebuilds_submodule_as_private_repo(tmp_path: Path, git_repo_factory) -> None:
    lib = git_repo_factory(tmp_path / "lib")
    source = git_repo_factory(tmp_path / "src")
    _git(source, "submodule", "--quiet", "add", str(lib), "deps/lib")
    _git(source, "commit", "--quiet", "-m", "add submodule")
    assert (source / "deps" / "lib" / ".git").is_file()

    dest = _copy_seed(source, tmp_path / "dest", tmp_path)

    sub = dest / "deps" / "lib"
    assert (sub / ".git").is_dir()
    assert _remotes(sub) == []
    assert not (dest / ".git" / "modules").exists()
    assert _git(dest, "status", "--porcelain").stdout == ""


def test_copy_of_linked_worktree_keeps_its_git_history(tmp_path: Path, git_repo_factory) -> None:
    source = git_repo_factory(tmp_path / "repo")
    worktree = tmp_path / "wt"
    _git(source, "worktree", "add", "--quiet", "--detach", str(worktree))
    _commit_file(worktree, "wt.txt", "wt\n", "worktree only")
    worktree_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    source_head = _git(source, "rev-parse", "HEAD").stdout.strip()

    dest = _copy_seed(worktree, tmp_path / "dest", tmp_path)

    assert (dest / ".git").is_dir()
    assert _git(dest, "rev-parse", "HEAD").stdout.strip() == worktree_head
    _git(dest, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--quiet", "--allow-empty", "-m", "case")
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == worktree_head
    assert _git(source, "rev-parse", "HEAD").stdout.strip() == source_head
    assert str(dest.resolve()) not in _git(source, "worktree", "list", "--porcelain").stdout


def test_copy_drops_core_worktree_pointing_at_source(tmp_path: Path, git_repo_factory) -> None:
    source = git_repo_factory(tmp_path / "src")
    _git(source, "config", "core.worktree", str(source.resolve()))
    (source / "README.md").write_text("user's uncommitted edit", encoding="utf-8")

    dest = _copy_seed(source, tmp_path / "dest", tmp_path)

    assert _git(dest, "config", "--get", "core.worktree", check=False).returncode == 1
    assert _git(dest, "status", "--porcelain").stdout == " M README.md\n"
    _git(dest, "restore", "README.md")
    assert (source / "README.md").read_text(encoding="utf-8") == "user's uncommitted edit"
    assert (dest / "README.md").read_text(encoding="utf-8") == "committed content"


def test_git_clone_ignores_inherited_git_dir(tmp_path: Path, git_repo_factory, monkeypatch) -> None:
    source = git_repo_factory(tmp_path / "repo")
    _git(source, "remote", "add", "origin", "https://example.invalid/repo.git")
    monkeypatch.setenv("GIT_DIR", str(source / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(source))

    materialize_workdir(
        WorkdirSeed(source=str(source), strategy="git_clone"), dest=tmp_path / "clone", dataset_dir=tmp_path
    )

    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")
    assert _remotes(source) == ["origin"]
    assert _remotes(tmp_path / "clone") == []


def test_git_clone_refuses_tracked_symlink_escaping_the_clone(tmp_path: Path, git_repo_factory) -> None:
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    source = git_repo_factory(tmp_path / "repo")
    symlink_or_skip(source / "leak", outside)
    _git(source, "add", "leak")
    _git(source, "commit", "--quiet", "-m", "leak")
    with pytest.raises(RsiBadRequest, match="symlink escaping"):
        materialize_workdir(
            WorkdirSeed(source=str(source), strategy="git_clone"), dest=tmp_path / "clone", dataset_dir=tmp_path
        )


def test_rsi_files_do_not_show_in_case_git_status(tmp_path: Path, git_repo_factory) -> None:
    from chrys.service.rsi.workdir_seed import RESPONSE_FILE_NAME

    source = git_repo_factory(tmp_path / "src")
    dest = _copy_seed(source, tmp_path / "dest", tmp_path)
    (dest / RESPONSE_FILE_NAME).write_text("answer", encoding="utf-8")
    assert _git(dest, "status", "--porcelain").stdout == ""
    assert not list(dest.glob(".chrys_rsi_workdir_seed*"))


def test_copy_refuses_destination_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "project"
    source.mkdir()
    (source / "a.txt").write_text("a\n", encoding="utf-8")
    with pytest.raises(RsiBadRequest, match=r"inside workdir\.from"):
        materialize_workdir(
            WorkdirSeed(source=".", strategy="copy"),
            dest=source / "out" / "case",
            dataset_dir=source,
        )
    assert not (source / "out").exists()


def test_enclosing_git_work_tree(tmp_path: Path, git_repo_factory) -> None:
    from chrys.service.rsi.workdir_seed import enclosing_git_work_tree

    repo = git_repo_factory(tmp_path / "repo")
    found = enclosing_git_work_tree(repo / "not-yet" / "out")
    assert found is not None
    assert found.resolve() == repo.resolve()
    plain = tmp_path / "plain"
    plain.mkdir()
    assert enclosing_git_work_tree(plain / "out") is None


def test_write_text_nofollow_writes_exact_bytes(tmp_path: Path) -> None:
    target = tmp_path / "response.txt"
    write_text_nofollow(target, "a\nb\r\nc ✓", what="response file")
    assert target.read_bytes() == "a\nb\r\nc ✓".encode()


def test_write_text_nofollow_finishes_a_short_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_write = os.write
    shadow = types.ModuleType("os")
    shadow.__dict__.update(os.__dict__)

    def one_byte_at_a_time(fd: int, data: bytes | memoryview) -> int:
        return real_write(fd, bytes(data[:1]))

    shadow.write = one_byte_at_a_time
    monkeypatch.setattr(workdir_seed_module, "os", shadow)
    target = tmp_path / "response.txt"
    write_text_nofollow(target, "hello world", what="response file")
    assert target.read_bytes() == b"hello world"


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
