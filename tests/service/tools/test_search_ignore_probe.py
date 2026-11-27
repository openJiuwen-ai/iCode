# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Avoid unnecessary Git processes without replacing authoritative ignore decisions."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.service.tools.builtins import search


@pytest.fixture
def project(tmp_path: Path, git_repo_factory, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # Runtime overrides deliberately force the Git probe. Let only the test supply them.
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)
    monkeypatch.delenv("GIT_CONFIG_PARAMETERS", raising=False)
    root = git_repo_factory(tmp_path / "repo")
    (root / "src/pkg").mkdir(parents=True)
    (root / "src/pkg/main.py").write_text("NEEDLE\n", encoding="utf-8")
    return root


@pytest.fixture
def windows_decoding(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    decoded: list[bytes] = []

    def fsdecode(filename: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> str:
        value = os.fspath(filename)
        if isinstance(value, bytes):
            decoded.append(value)
            return value.decode("utf-8", errors="surrogatepass")
        return value

    monkeypatch.setattr(search.os, "fsdecode", create_autospec(os.fsdecode, side_effect=fsdecode))
    return decoded


async def test_ordinary_subdirectory_searches_do_not_spawn_git(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (project / ".gitignore").write_text("*.pyc\n/dist/\n__pycache__/\n.venv/\n", encoding="utf-8")
    launch = create_autospec(search.managed_subprocess, side_effect=search.managed_subprocess)
    monkeypatch.setattr(search, "managed_subprocess", launch)

    assert "main.py" in await search.glob("*.py", path=str(project / "src/pkg"))
    assert "main.py:1" in await search.grep("NEEDLE", path=str(project / "src/pkg"))
    assert "main.py:1" in await search.grep("NEEDLE", glob="src/pkg/*.py", path=str(project))
    assert launch.call_count > 0
    assert not any("check-ignore" in call.args for call in launch.call_args_list)


async def test_non_utf8_git_output_preserves_explicit_search(project: Path, windows_decoding: list[bytes]) -> None:
    root = project / "src/pkg"
    rule = b"/src/p[!\xe9]g/"
    (project / ".git/info/exclude").write_bytes(rule + b"\n")
    # Returning False after a decoding error would leave this rule active.
    (root / ".gitignore").write_text("*\n", encoding="utf-8")

    for result in (
        await search.glob("*.py", path=str(root)),
        await search.grep("NEEDLE", path=str(root)),
        await search.grep("NEEDLE", path=str(root), glob="*.py"),
    ):
        assert result.startswith("Found 1") and "main.py" in result
    assert any(b".git/info/exclude\0" in value and rule in value for value in windows_decoding)
    assert await search._directory_is_gitignored(str(root))


@pytest.mark.parametrize("source", [".gitignore", ".git/info/exclude", ".git/config"])
async def test_non_utf8_comments_do_not_spawn_git(
    project: Path, monkeypatch: pytest.MonkeyPatch, windows_decoding: list[bytes], source: str
) -> None:
    (project / ".gitignore").write_text("*.pyc\n__pycache__/\n", encoding="utf-8")
    path = project / source
    path.write_bytes(path.read_bytes() + "\n# 构建产物\n".encode("gbk"))
    launch = create_autospec(search.managed_subprocess, side_effect=search.managed_subprocess)
    monkeypatch.setattr(search, "managed_subprocess", launch)
    root = project / "src/pkg"

    assert "main.py" in await search.glob("*.py", path=str(root))
    assert "main.py:1" in await search.grep("NEEDLE", path=str(root))
    assert "main.py:1" in await search.grep("NEEDLE", path=str(project), glob="src/pkg/*.py")
    assert windows_decoding
    assert launch.call_count > 0
    assert not any("check-ignore" in call.args for call in launch.call_args_list)


@pytest.mark.parametrize(
    ("source", "rule"),
    [
        ("src/pkg/.gitignore", "\ufeff*\n"),
        (".gitignore", "\ufeffsrc/pkg/\n"),
        (".gitignore", "\ufeff/src/pkg/*\n"),
        (".gitignore", "/src/pkg/*   \n"),
        (".gitignore", "\ufeff/src/pkg/*   \r\n"),
        (".git/info/exclude", "\ufeff/src/pkg/*   \r\n"),
    ],
)
async def test_bom_and_trailing_spaces_do_not_skip_git_probe(project: Path, source: str, rule: str) -> None:
    (project / source).write_bytes(rule.encode("utf-8"))
    root = project / "src/pkg"
    (root / "skip.py").write_text("NEEDLE\n", encoding="utf-8")
    (root / ".ignore").write_text("skip.py\n", encoding="utf-8")

    assert await search._directory_is_gitignored(str(root))
    assert "main.py" in await search.glob("*", path=str(root))
    assert "main.py" in await search.glob("src/pkg/*.py", path=str(project))
    for pattern in (None, "*.py"):
        result = await search.grep("NEEDLE", path=str(root), glob=pattern)
        assert "Found 1 match(es)" in result and "main.py:1" in result
        assert "skip.py" not in result


async def test_escaped_trailing_space_in_a_directory_name_remains_searchable(project: Path) -> None:
    root = project / "src/pkg "
    root.mkdir(exist_ok=True)
    if root.name not in {entry.name for entry in root.parent.iterdir()}:
        pytest.skip("Filesystem normalizes trailing spaces in directory names")
    (project / ".gitignore").write_text("/src/pkg\\ \n", encoding="utf-8")
    (root / "main.py").write_text("NEEDLE\n", encoding="utf-8")

    assert await search._directory_is_gitignored(str(root))
    assert "main.py" in await search.glob("*", path=str(root))


@pytest.mark.parametrize("rule", ["src/*/", "**/p[k]g/", "SRC/PKG/"])
async def test_wildcard_and_case_insensitive_rules_still_receive_git_confirmation(project: Path, rule: str) -> None:
    with (project / ".git/config").open("a", encoding="utf-8") as config:
        config.write("\n[core]\nignoreCase = true\n")
    (project / ".gitignore").write_text(rule + "\n", encoding="utf-8")

    root = project / "src/pkg"
    assert await search._directory_is_gitignored(str(root))
    assert "main.py:1" in await search.grep("NEEDLE", path=str(root))


@pytest.mark.parametrize("source", ["ancestor", "info", "global", "include", "xdg"])
async def test_new_ignore_rules_are_visible_without_a_stale_cached_probe(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    root = project / "src/pkg"
    assert not await search._directory_is_gitignored(str(root))
    if source == "ancestor":
        rules = project / "src/.gitignore"
        rule = "pkg/*\n"
    elif source == "info":
        rules = project / ".git/info/exclude"
        rule = "src/pkg/*\n"
    elif source == "xdg":
        rules = tmp_path / "xdg/git/ignore"
        rule = "src/pkg/*\n"
    else:
        rules = tmp_path / "custom-ignore"
        config = tmp_path / "global-config"
        config.write_text(f"[core]\nexcludesFile = {json.dumps(rules.as_posix())}\n", encoding="utf-8")
        if source == "include":
            included = tmp_path / "included-config"
            config.rename(included)
            config.write_text(f"[include]\npath = {json.dumps(included.as_posix())}\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
        rule = "src/pkg/*\n"
    rules.parent.mkdir(parents=True, exist_ok=True)
    rules.write_text(rule, encoding="utf-8")

    launch = create_autospec(search.managed_subprocess, side_effect=search.managed_subprocess)
    monkeypatch.setattr(search, "managed_subprocess", launch)
    assert await search._directory_is_gitignored(str(root))
    assert any("check-ignore" in call.args for call in launch.call_args_list)
    assert "main.py:1" in await search.grep("NEEDLE", path=str(root))
    if source in {"ancestor", "info"}:
        assert "main.py:1" not in await search.grep("NEEDLE", path=str(project))

    rules.write_text("# rule removed\n", encoding="utf-8")
    assert not await search._directory_is_gitignored(str(root))
    assert "main.py:1" in await search.grep("NEEDLE", path=str(project))
