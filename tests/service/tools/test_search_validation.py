# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Empty-input validation and directory disappearance preserve failure causes."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from chrys.foundation.tool_result_metadata import PROCESS_EXIT_CODE_METADATA_KEY, TOOL_ERROR_KIND_METADATA_KEY
from chrys.service.tools.builtins import search
from chrys.service.tools.result_metadata import tool_result_metadata


@pytest.mark.parametrize(
    ("pattern", "context_lines", "diagnostic"),
    [("[invalid", 0, "unclosed character class"), ("foo(?=bar)", 0, "look-around"), ("NEEDLE", -1, "flag -C")],
)
async def test_empty_candidates_still_report_invalid_search_arguments(
    tmp_path: Path, pattern: str, context_lines: int, diagnostic: str
) -> None:
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        result = await search.grep(pattern, path=str(tmp_path), glob="*.py", context_lines=context_lines)
    finally:
        tool_result_metadata.reset(token)

    assert result.startswith("Error:") and diagnostic in result
    assert "No matches" not in result
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "search_process_failed"
    assert metadata[PROCESS_EXIT_CODE_METADATA_KEY] == 2


async def test_empty_validation_uses_rust_regex_and_does_not_search_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    (tmp_path / "unrelated.py").write_text("NEEDLE\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    result = await search.grep(r"\p{Letter}+", path=str(root), glob="*.py")

    assert result.startswith("No matches found")
    assert "unrelated.py" not in result


@pytest.mark.parametrize(("operation", "phase"), [("grep", "listing"), ("glob", "listing"), ("grep", "content")])
async def test_deleted_search_directory_does_not_claim_ripgrep_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, phase: str
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "file.py").write_text("NEEDLE\n", encoding="utf-8")
    original_run = search._run_rg
    removed = False

    async def remove_before_spawn(
        args: list[str],
        *,
        timeout: int = 30,
        cwd: str | None = None,
        consume: Callable[[bytes], bool] | None = None,
    ) -> tuple[str, str, int]:
        nonlocal removed
        if not removed and ((phase == "listing" and "--files" in args) or (phase == "content" and "--json" in args)):
            shutil.rmtree(root)
            removed = True
        return await original_run(args, timeout=timeout, cwd=cwd, consume=consume)

    monkeypatch.setattr(search, "_run_rg", remove_before_spawn)
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        result = (
            await search.grep("NEEDLE", path=str(root), glob="*.py")
            if operation == "grep"
            else await search.glob("*.py", path=str(root))
        )
    finally:
        tool_result_metadata.reset(token)

    assert removed
    assert "search directory unavailable" in result and str(root) in result
    assert "ripgrep (rg) not found" not in result
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "path_not_found"


@pytest.mark.parametrize("operation", ["grep", "glob"])
async def test_missing_executable_still_reports_ripgrep_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setattr(search, "_find_rg", lambda: str(tmp_path / "missing-rg"))
    result = (
        await search.grep("NEEDLE", path=str(tmp_path), glob="*.py")
        if operation == "grep"
        else await search.glob("*.py", path=str(tmp_path))
    )
    assert result == "Error: ripgrep (rg) not found"


@pytest.mark.parametrize("operation", ["grep", "glob"])
@pytest.mark.parametrize("max_results", [0, -1])
async def test_max_results_below_one_is_rejected_before_searching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, max_results: int
) -> None:
    # Matches exist, so a clamped search would report them: only the rejection is acceptable.
    (tmp_path / "a.py").write_text("NEEDLE\n", encoding="utf-8")

    async def forbidden_run_rg(
        args: list[str],
        *,
        timeout: int = 30,
        cwd: str | None = None,
        consume: Callable[[bytes], bool] | None = None,
    ) -> tuple[str, str, int]:
        raise AssertionError("ripgrep must not run when max_results is invalid")

    monkeypatch.setattr(search, "_run_rg", forbidden_run_rg)
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        result = (
            await search.grep("NEEDLE", path=str(tmp_path), glob="*.py", max_results=max_results)
            if operation == "grep"
            else await search.glob("*.py", path=str(tmp_path), max_results=max_results)
        )
    finally:
        tool_result_metadata.reset(token)

    assert result == "Error: max_results must be an integer >= 1 (the maximum number of results to return)."
    assert "No matches" not in result
    assert "Found" not in result
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "invalid_max_results"


@pytest.mark.parametrize(("operation", "header"), [("grep", "Found 1 match(es)"), ("glob", "Found 1 file(s)")])
async def test_max_results_one_still_searches(tmp_path: Path, operation: str, header: str) -> None:
    (tmp_path / "a.py").write_text("NEEDLE\n", encoding="utf-8")

    result = (
        await search.grep("NEEDLE", path=str(tmp_path), glob="*.py", max_results=1)
        if operation == "grep"
        else await search.glob("*.py", path=str(tmp_path), max_results=1)
    )

    assert header in result
    assert "a.py" in result
