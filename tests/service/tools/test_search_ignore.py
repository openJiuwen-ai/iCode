# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Search ignore policy, native glob syntax, and bounded file batches."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.service.profiles.agents.schema import ToolsConfig
from chrys.service.tools.builtins import search
from chrys.service.tools.packs import ToolBuild, assemble_tools


@pytest.fixture
def project(tmp_path: Path, git_repo_factory) -> Path:
    root = git_repo_factory(tmp_path / "repo")
    (root / ".gitignore").write_text("ignored.py\nignored_dir/\n", encoding="utf-8")
    (root / ".ignore").write_text("dot_ignored.py\n", encoding="utf-8")
    (root / ".rgignore").write_text("rg_ignored.py\n", encoding="utf-8")
    for name in (
        "visible.py",
        "ignored.py",
        "ignored_dir/inside.py",
        "dot_ignored.py",
        "rg_ignored.py",
        ".hidden.py",
        ".hidden_dir/inside.py",
        ".git/probe.py",
    ):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("SEARCH_NEEDLE\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("respect", [True, False])
@pytest.mark.parametrize("pattern", [None, "*", "*.py", "**"])
async def test_assembled_search_tools_apply_ignore_policy(project: Path, respect: bool, pattern: str | None) -> None:
    runtime = SessionEnvironment.capture(workspace=Workspace.from_cwd(str(project)))
    assembled = await assemble_tools(
        ToolBuild(
            tools=ToolsConfig(builtins=["search"]),
            agent_name="test",
            runtime=runtime,
            settings=Settings(search_respect_gitignore=respect),
        )
    )
    tools = {tool.name: tool for tool in assembled.tools}
    grep_result = await tools["grep"]("SEARCH_NEEDLE", glob=pattern)
    glob_result = await tools["glob"](pattern or "*")

    for result in (grep_result, glob_result):
        assert "visible.py" in result
        assert ("ignored.py" in result) is not respect
        assert ("ignored_dir/inside.py" in result) is not respect
        for excluded in ("dot_ignored.py", "rg_ignored.py", ".hidden.py", ".hidden_dir/", ".git/"):
            assert excluded not in result


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("*.{py,ts}", {"main.py", "web.ts", "src/util.py", "src/deep/other.py"}),
        ("src/*.py", {"src/util.py"}),
        ("/src/*.py", {"src/util.py"}),
        ("**/*.py", {"main.py", "src/util.py", "src/deep/other.py"}),
        ("!*.py", {"web.ts", "notes.txt"}),
    ],
)
async def test_native_glob_syntax_is_preserved(tmp_path: Path, pattern: str, expected: set[str]) -> None:
    for name in ("main.py", "web.ts", "src/util.py", "src/deep/other.py", "notes.txt"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("NEEDLE\n", encoding="utf-8")

    files = await search._search_files(str(tmp_path), pattern, True)
    assert isinstance(files, list)
    assert {Path(name).relative_to(tmp_path).as_posix() for name in files} == expected
    result = await search.grep("NEEDLE", path=str(tmp_path), glob=pattern, context_lines=0)
    assert f"Found {len(expected)} match(es)" in result
    assert all(f"{name}:1" in result for name in expected)


@pytest.mark.parametrize("respect", [True, False])
async def test_explicit_ignored_file_remains_searchable(project: Path, respect: bool) -> None:
    result = await search._grep_impl("SEARCH_NEEDLE", path=str(project / "ignored.py"), respect_gitignore=respect)
    assert "Found 1 match(es)" in result


async def test_non_git_directory_retains_ripgrep_ignore_defaults(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (tmp_path / "ignored.py").write_text("NEEDLE\n", encoding="utf-8")
    assert "ignored.py" in await search.glob("*.py", path=str(tmp_path))
    assert "ignored.py" in await search.grep("NEEDLE", path=str(tmp_path), glob="*.py")


async def test_ambient_ripgrep_config_cannot_override_policy(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "ripgreprc"
    config.write_text("--no-ignore\n--hidden\n", encoding="utf-8")
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", str(config))
    result = await search.glob("*.py", path=str(project))
    assert "visible.py" in result
    assert "ignored.py" not in result
    assert ".hidden.py" not in result


async def test_nested_ignore_and_negation_rules(project: Path) -> None:
    sub = project / "sub"
    sub.mkdir()
    (sub / ".gitignore").write_text("*.py\n!keep.py\n", encoding="utf-8")
    (sub / "keep.py").write_text("NEEDLE\n", encoding="utf-8")
    (sub / "skip.py").write_text("NEEDLE\n", encoding="utf-8")
    result = await search.glob("*.py", path=str(sub))
    assert "keep.py" in result
    assert "skip.py" not in result
    result = await search._glob_impl("*.py", path=str(sub), respect_gitignore=False)
    assert "keep.py" in result and "skip.py" in result


async def test_empty_candidates_validate_only_against_empty_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    run_rg = search._run_rg

    async def recording_run(
        args: list[str],
        *,
        timeout: int = 30,
        cwd: str | None = None,
        consume: Callable[[bytes], bool] | None = None,
    ) -> tuple[str, str, int]:
        calls.append(args)
        return await run_rg(args, timeout=timeout, cwd=cwd, consume=consume)

    monkeypatch.setattr(search, "_run_rg", recording_run)
    result = await search.grep("NEEDLE", path=str(tmp_path), glob="*.py")
    assert "No matches found" in result
    assert calls and all("--files" in args for args in calls[:-1])
    assert calls[-1][calls[-1].index("--") + 1 :] == ["-"]
    assert "--json" in calls[-1]


async def test_grep_batches_search_past_candidate_limit_and_share_result_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = []
    for index in range(12):
        path = tmp_path / f"file{index}.py"
        path.write_text("NEEDLE\n" if index >= 8 else "nothing\n", encoding="utf-8")
        files.append(str(path))

    async def ordered_files(root: str, pattern: str | None, respect_gitignore: bool) -> list[str]:
        return files

    def single_file_batches(files: list[str], *, command: list[str]) -> Iterator[list[str]]:
        for name in files:
            yield [name]

    monkeypatch.setattr(search, "_search_files", ordered_files)
    monkeypatch.setattr(search, "_file_batches", single_file_batches)
    result = await search.grep("NEEDLE", path=str(tmp_path), glob="*.py", max_results=2, context_lines=0)
    assert "Found 2 match(es)" in result
    assert "file8.py:1" in result and "file9.py:1" in result
    assert "file10.py:1" not in result


@pytest.mark.parametrize("name", [" space 文件.py", "-option.py", "line\nbreak.py", b"raw_\xff.py"])
async def test_candidate_filenames_are_preserved(tmp_path: Path, name: str | bytes) -> None:
    try:
        # Probe raw filesystem bytes, not an unpaired UTF-16 surrogate that
        # Windows can create but rg cannot round-trip through its UTF-8 output.
        name = os.fsdecode(name)
        path = tmp_path / name
        path.write_text("NEEDLE\n", encoding="utf-8")
    except OSError, UnicodeError:
        pytest.skip("Filesystem does not support this filename")
    files = await search._search_files(str(tmp_path), "*.py", True)
    assert files == [str(path)]
    result = await search.grep("NEEDLE", path=str(tmp_path), glob="*.py")
    assert "Found 1 match(es)" in result
    assert surrogate_safe_text(name) in result
    result.encode("utf-8")


async def test_encoding_duplicates_do_not_consume_result_budget(tmp_path: Path) -> None:
    # Both passes see the ASCII match; only the GBK pass sees the later Chinese match.
    (tmp_path / "encoded.py").write_bytes("NEEDLE\n测试\n".encode("gbk"))
    result = await search.grep("NEEDLE|测试", path=str(tmp_path), glob="*.py", max_results=2, context_lines=0)
    assert "Found 2 match(es)" in result
    assert "测试" in result


@pytest.mark.parametrize("tool", ["grep", "glob"])
async def test_search_timeout_covers_candidate_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    cancelled = False

    async def blocked_run(
        args: list[str],
        *,
        timeout: int = 30,
        cwd: str | None = None,
        consume: Callable[[bytes], bool] | None = None,
    ) -> tuple[str, str, int]:
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        finally:
            cancelled = True
        raise AssertionError("unreachable")

    monkeypatch.setattr(search, "_run_rg", blocked_run)
    monkeypatch.setattr(search, "_TIMEOUT", 0)
    result = (
        await search.grep("NEEDLE", path=str(tmp_path), glob="*.py")
        if tool == "grep"
        else await search.glob("*.py", path=str(tmp_path))
    )
    assert result.startswith("Error: search timed out")
    assert cancelled
