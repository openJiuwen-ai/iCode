# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Builtin tools report a deleted session working directory as ``working_dir_missing``.

The directory can be deleted or moved outside the app mid-session. A tool whose
path depends on it says so in one wording that tells the model to stop, instead
of a misleading "not found" about the shell, the file or the search root; paths
that never resolve against it keep working.
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.tool_result_metadata import TOOL_ERROR_DETAILS_METADATA_KEY, TOOL_ERROR_KIND_METADATA_KEY
from chrys.service.tools.builtins import search
from chrys.service.tools.builtins.doc_converter import DocConverterTools
from chrys.service.tools.builtins.filesystem import FilesystemTools
from chrys.service.tools.builtins.search import SearchTools
from chrys.service.tools.builtins.shell import ShellTools
from chrys.service.tools.result_metadata import tool_result_metadata
from chrys.service.tools.session_artifacts import make_document_artifact_handle
from chrys.service.tools.workspace_paths import (
    WORKING_DIR_MISSING_KIND,
    missing_base_cwd_error,
    working_dir_missing_error,
)
from tests.support.symlinks import symlink_or_skip


def _missing_text(path: Path | str) -> str:
    return (
        f"Error: working directory no longer exists — {path} (deleted or moved outside {APP_DISPLAY_NAME}). "
        "Stop and tell the user; they need to choose another working directory."
    )


@contextmanager
def _tool_metadata() -> Iterator[dict[str, object]]:
    metadata: dict[str, object] = {}
    token = tool_result_metadata.set(metadata)
    try:
        yield metadata
    finally:
        tool_result_metadata.reset(token)


def _assert_working_dir_missing(result: str, metadata: dict[str, object], gone: Path) -> None:
    assert result == _missing_text(gone)
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == WORKING_DIR_MISSING_KIND
    assert metadata[TOOL_ERROR_DETAILS_METADATA_KEY] == {"cwd": str(gone)}


@pytest.fixture
def gone(tmp_path: Path) -> Path:
    """A session working directory that was deleted after the session bound it."""
    path = tmp_path / "workspace"
    path.mkdir()
    path.rmdir()
    return path


@pytest.fixture
def gone_runtime(gone: Path) -> SessionEnvironment:
    return dataclasses.replace(SessionEnvironment.capture(), cwd=str(gone))


@pytest.fixture
def elsewhere(tmp_path: Path) -> Path:
    """An existing directory outside the deleted working directory."""
    path = tmp_path / "elsewhere"
    path.mkdir()
    return path


# ─────────────────────────── missing_base_cwd_error ───────────────────────────


def test_working_dir_missing_error_text_and_metadata(gone: Path) -> None:
    with _tool_metadata() as metadata:
        result = working_dir_missing_error(str(gone))

    _assert_working_dir_missing(result, metadata, gone)


@pytest.mark.parametrize("raw_path", ["notes.txt", ".", "src/../notes.txt", "  notes.txt  "])
def test_relative_path_under_a_deleted_base_is_reported(gone: Path, raw_path: str) -> None:
    assert missing_base_cwd_error(raw_path, str(gone)) == _missing_text(gone)


@pytest.mark.parametrize("raw_path", ["notes.txt", "."])
@pytest.mark.parametrize("base", ["existing", "none", "empty"])
def test_relative_path_needs_a_deleted_base_to_be_reported(tmp_path: Path, raw_path: str, base: str) -> None:
    base_cwd = {"existing": str(tmp_path), "none": None, "empty": ""}[base]

    assert missing_base_cwd_error(raw_path, base_cwd) is None


@pytest.mark.parametrize("inside", ["", "notes.txt", "src/notes.txt", "src/../notes.txt"])
def test_absolute_path_inside_a_deleted_base_is_reported(gone: Path, inside: str) -> None:
    raw_path = os.path.join(str(gone), inside) if inside else str(gone)

    assert missing_base_cwd_error(raw_path, str(gone)) == _missing_text(gone)


def test_absolute_path_beside_a_deleted_base_is_not_inside_it(gone: Path) -> None:
    """A sibling that shares the base's name as a prefix is a different directory."""
    sibling = f"{gone}-other{os.sep}notes.txt"

    assert missing_base_cwd_error(sibling, str(gone)) is None


@pytest.mark.parametrize("spelling", ["base_through_link", "path_through_link", "dots_after_link"])
def test_absolute_path_inside_a_deleted_base_through_a_symlink_is_reported(tmp_path: Path, spelling: str) -> None:
    """The base and the path may name the same folder through a symlinked parent."""
    real = tmp_path / "deep" / "real"
    (real / "app").mkdir(parents=True)
    link = tmp_path / "link"
    symlink_or_skip(link, real, target_is_directory=True)
    (real / "app").rmdir()
    base, path = {
        "base_through_link": (link / "app", str(real / "app" / "out.txt")),
        "path_through_link": (real / "app", str(link / "app" / "out.txt")),
        # The tools drop ".." as written: this names deep/real/app, not the link target's parent.
        "dots_after_link": (link / "app", os.path.join(str(link), "..", "deep", "real", "app", "out.txt")),
    }[spelling]

    assert missing_base_cwd_error(path, str(base)) == _missing_text(base)


def test_absolute_and_home_paths_never_depend_on_the_base(gone: Path, elsewhere: Path) -> None:
    assert missing_base_cwd_error(str(elsewhere / "notes.txt"), str(gone)) is None
    assert missing_base_cwd_error("~/notes.txt", str(gone)) is None
    # A Windows path is absolute on every host, as the tools resolve it.
    assert missing_base_cwd_error(r"C:\work\notes.txt", str(gone)) is None


@pytest.mark.parametrize(
    "handle",
    [make_document_artifact_handle("report.md"), "chrys-session-document:../outside.md"],
    ids=["valid", "malformed"],
)
def test_document_artifact_handles_are_left_to_their_resolver(gone: Path, handle: str) -> None:
    assert missing_base_cwd_error(handle, str(gone)) is None


# ─────────────────────────────────── shell ───────────────────────────────────


async def test_shell_reports_a_deleted_session_cwd_not_a_missing_shell(
    gone_runtime: SessionEnvironment, gone: Path
) -> None:
    shell = ShellTools(gone_runtime)

    with _tool_metadata() as metadata:
        result = await shell.execute("echo hello", reason="test")

    _assert_working_dir_missing(result, metadata, gone)
    assert "shell not found" not in result


async def test_shell_relative_working_dir_under_a_deleted_session_cwd_is_reported(
    gone_runtime: SessionEnvironment, gone: Path
) -> None:
    shell = ShellTools(gone_runtime)

    with _tool_metadata() as metadata:
        result = await shell.execute("echo hello", reason="test", workdir="sub")

    _assert_working_dir_missing(result, metadata, gone)
    assert not gone.exists()


@pytest.mark.parametrize("relative", [True, False])
async def test_shell_nonexistent_working_dir_is_reported_as_that_directory(elsewhere: Path, relative: bool) -> None:
    runtime = dataclasses.replace(SessionEnvironment.capture(), cwd=str(elsewhere))
    shell = ShellTools(runtime)
    workdir = "nope" if relative else str(elsewhere / "nope")

    with _tool_metadata() as metadata:
        result = await shell.execute("echo hello", reason="test", workdir=workdir)

    resolved = str(elsewhere / "nope")
    assert result == f"Error: workdir not found — {resolved}"
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] == "working_dir_not_found"
    assert metadata[TOOL_ERROR_DETAILS_METADATA_KEY] == {"working_dir": workdir, "resolved_path": resolved}


async def test_shell_absolute_working_dir_still_runs_after_the_session_cwd_is_deleted(
    gone_runtime: SessionEnvironment, elsewhere: Path
) -> None:
    shell = ShellTools(gone_runtime)

    result = await shell.execute("echo hello", reason="test", workdir=str(elsewhere))

    assert "hello" in result
    assert "[exit_code: 0]" in result


# ───────────────────────────────── filesystem ─────────────────────────────────


@pytest.mark.parametrize(
    "call",
    [
        lambda tools: tools.read_file("notes.txt"),
        lambda tools: tools.edit_file("notes.txt", "alpha", "beta"),
        lambda tools: tools.view_image("shot.png")[0].text,
    ],
    ids=["read_file", "edit_file", "view_image"],
)
def test_filesystem_relative_paths_under_a_deleted_session_cwd_are_reported(
    gone_runtime: SessionEnvironment, gone: Path, call: Callable[[FilesystemTools], str]
) -> None:
    with _tool_metadata() as metadata:
        result = call(FilesystemTools(gone_runtime))

    _assert_working_dir_missing(result, metadata, gone)


@pytest.mark.parametrize("absolute", [False, True], ids=["relative", "absolute"])
def test_write_file_does_not_recreate_a_deleted_session_cwd(
    gone_runtime: SessionEnvironment, gone: Path, absolute: bool
) -> None:
    path = str(gone / "src" / "notes.txt") if absolute else "src/notes.txt"

    with _tool_metadata() as metadata:
        result = FilesystemTools(gone_runtime).write_file(path, "alpha\n")

    _assert_working_dir_missing(result, metadata, gone)
    assert not gone.exists()


def test_filesystem_absolute_paths_still_work_after_the_session_cwd_is_deleted(
    gone_runtime: SessionEnvironment, elsewhere: Path
) -> None:
    tools = FilesystemTools(gone_runtime)
    target = elsewhere / "notes.txt"

    assert "Written" in tools.write_file(str(target), "alpha\n")
    assert "1|alpha" in tools.read_file(str(target))
    assert "Error" not in tools.edit_file(str(target), "alpha", "beta")
    assert target.read_text(encoding="utf-8") == "beta\n"


def test_read_file_leaves_a_document_handle_to_its_resolver(gone_runtime: SessionEnvironment) -> None:
    result = FilesystemTools(gone_runtime).read_file(make_document_artifact_handle("report.md"))

    assert result.startswith("Error:")
    assert "working directory no longer exists" not in result


# ─────────────────────────────────── search ───────────────────────────────────


@pytest.mark.parametrize("operation", ["grep", "glob"])
async def test_search_relative_paths_under_a_deleted_session_cwd_are_reported(
    gone_runtime: SessionEnvironment, gone: Path, operation: str
) -> None:
    tools = SearchTools(gone_runtime)

    with _tool_metadata() as metadata:
        result = await (tools.grep("needle") if operation == "grep" else tools.glob("*.py"))

    _assert_working_dir_missing(result, metadata, gone)


@pytest.mark.parametrize("operation", ["grep", "glob"])
async def test_search_absolute_paths_still_work_after_the_session_cwd_is_deleted(
    gone_runtime: SessionEnvironment, elsewhere: Path, operation: str
) -> None:
    (elsewhere / "match.py").write_text("needle\n", encoding="utf-8")
    tools = SearchTools(gone_runtime)

    result = await (
        tools.grep("needle", path=str(elsewhere), context_lines=0)
        if operation == "grep"
        else tools.glob("*.py", path=str(elsewhere))
    )

    assert "match.py" in result
    assert not result.startswith("Error:")


@pytest.mark.parametrize("operation", ["grep", "globbed_grep", "glob"])
async def test_search_reports_a_session_cwd_deleted_just_before_rg_starts_in_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """The directory can vanish between the up-front check and the spawn of an rg that runs inside it."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "match.py").write_text("needle\n", encoding="utf-8")
    runtime = dataclasses.replace(SessionEnvironment.capture(), cwd=str(workspace))
    real_run_rg = search._run_rg
    deletions: list[str] = []

    async def delete_then_run_rg(
        args: list[str],
        *,
        timeout: int = 30,
        cwd: str | None = None,
        consume: Callable[[bytes], bool] | None = None,
    ) -> tuple[str, str, int]:
        if not deletions:
            deletions.append(str(workspace))
            (workspace / "match.py").unlink()
            workspace.rmdir()
        return await real_run_rg(args, timeout=timeout, cwd=cwd, consume=consume)

    monkeypatch.setattr(search, "_run_rg", delete_then_run_rg)
    tools = SearchTools(runtime)

    with _tool_metadata() as metadata:
        if operation == "grep":
            result = await tools.grep("needle", context_lines=0)
        elif operation == "globbed_grep":
            result = await tools.grep("needle", glob="*.py", context_lines=0)
        else:
            result = await tools.glob("*.py")

    assert deletions == [str(workspace)]
    _assert_working_dir_missing(result, metadata, workspace)


# ─────────────────────────────── doc converter ───────────────────────────────


async def test_convert_document_relative_path_under_a_deleted_session_cwd_is_reported(
    gone_runtime: SessionEnvironment, gone: Path
) -> None:
    with _tool_metadata() as metadata:
        result = await DocConverterTools(gone_runtime).convert_document("report.pdf")

    _assert_working_dir_missing(result, metadata, gone)


async def test_convert_document_absolute_path_keeps_its_own_error_after_the_session_cwd_is_deleted(
    gone_runtime: SessionEnvironment, elsewhere: Path
) -> None:
    with _tool_metadata() as metadata:
        result = await DocConverterTools(gone_runtime).convert_document(str(elsewhere / "report.pdf"))

    assert result.startswith("Error: file not found")
    assert metadata[TOOL_ERROR_KIND_METADATA_KEY] != WORKING_DIR_MISSING_KIND
