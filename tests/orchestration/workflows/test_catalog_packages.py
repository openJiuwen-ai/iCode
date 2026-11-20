# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Catalog behaviour for workflow folders: when a preview goes stale, what a check reads, and what deleting removes."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

import chrys.foundation.platform.files as files_module
import chrys.service.workflows.discovery as discovery_module
from chrys.foundation.platform.files import atomic_write_owner_only_bytes
from chrys.orchestration.workflows.catalog import WorkflowCatalog
from chrys.service.workflows.discovery import (
    LAYOUT_FILE,
    LAYOUT_PACKAGE,
    SOURCE_KIND_GLOBAL,
    global_workflows_dir,
    workdir_workflows_dir,
)
from tests.orchestration.workflows._hosting import make_workdir, write_workflow_package
from tests.support.symlinks import junction_or_skip, symlink_or_skip
from tests.support.workflow_workers import python_workflow

SOURCE = python_workflow("def fn(text):\n    return text\n", "fn")


def _catalog(tmp_path: Path) -> WorkflowCatalog:
    return WorkflowCatalog(config_dir=tmp_path / "config", workdir=make_workdir(tmp_path))


def _write(path: Path, body: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_owner_only_bytes(path, body)
    return path


async def test_a_folder_preview_is_stale_once_a_covered_file_changes(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    entry = write_workflow_package(catalog.workdir, "pkg", SOURCE, {"helpers.py": b"VALUE = 1\n"})
    preview = await catalog.preview("pkg", trust=True)
    assert preview.source.layout == LAYOUT_PACKAGE
    assert catalog.candidate_paths(preview.source)[:2] == (entry.resolve(), entry.parent.resolve())
    assert catalog.is_current(preview)

    # What the folder digest leaves out can change freely.
    _write(entry.parent / "__pycache__" / "helpers.cpython-314.pyc", b"\0")
    _write(entry.parent / ".venv" / "lib" / "site.py", b"anything\n")
    assert catalog.is_current(preview)

    _write(entry.parent / "helpers.py", b"VALUE = 22\n")
    assert not catalog.is_current(preview)


@pytest.mark.parametrize("kind", ["workdir", "global"])
async def test_a_folder_appearing_above_a_file_makes_its_preview_stale(tmp_path: Path, kind: str) -> None:
    catalog = _catalog(tmp_path)
    root = workdir_workflows_dir(catalog.workdir) if kind == "workdir" else global_workflows_dir(catalog.config_dir)
    _write(root / "x.py", SOURCE)
    preview = await catalog.preview("x", trust=True)
    assert preview.source.layout == LAYOUT_FILE and catalog.is_current(preview)

    _write(root / "x" / "x.py", SOURCE)

    assert not catalog.is_current(preview)


@pytest.mark.parametrize("project", ["elsewhere", "home", "linked-home"])
async def test_a_global_sdk_workflow_stays_current_after_its_preview_writes_the_sdk(
    tmp_path: Path, project: str
) -> None:
    home = tmp_path / "home"
    config_dir = home / ".chrys"
    # Run from the home directory, the project's workflow folder is the global one.
    workdir = make_workdir(tmp_path) if project == "elsewhere" else home
    if project == "linked-home":
        home.mkdir()
        workdir = tmp_path / "linked"
        symlink_or_skip(workdir, home, target_is_directory=True)
    catalog = WorkflowCatalog(config_dir=config_dir, workdir=workdir)
    _write(global_workflows_dir(config_dir) / "sdk.py", SOURCE)

    preview = await catalog.preview("sdk", trust=True)

    # The preview wrote the SDK into the reserved global sdk folder, which never shadows a workflow.
    assert (global_workflows_dir(config_dir) / "sdk").is_dir()
    assert catalog.is_current(preview)


async def test_a_broken_folder_above_the_preview_is_never_read_by_a_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(tmp_path)
    _write(global_workflows_dir(catalog.config_dir) / "x.py", SOURCE)
    broken = write_workflow_package(catalog.workdir, "x", SOURCE).parent
    symlink_or_skip(broken / "shared.py", _write(tmp_path / "outside.py", b"VALUE = 1\n"))
    preview = await catalog.preview("x", trust=True)
    assert preview.source.source_kind == SOURCE_KIND_GLOBAL
    opened: list[Path] = []
    real_open = discovery_module.secure_open_owner_verified_binary

    def recording_open(path: Path):
        opened.append(Path(path))
        return real_open(path)

    monkeypatch.setattr(discovery_module, "secure_open_owner_verified_binary", recording_open)

    assert catalog.is_current(preview)
    assert opened and not any(path.is_relative_to(broken.resolve()) for path in opened)

    # Its entry changing could change the winner, so that is noticed without reading it either.
    _write(broken / "x.py", SOURCE + b"# changed\n")
    assert not catalog.is_current(preview)
    assert not any(path.is_relative_to(broken.resolve()) for path in opened)
    again = await catalog.preview("x", trust=True)
    assert again.source.source_kind == SOURCE_KIND_GLOBAL and catalog.is_current(again)


@pytest.mark.parametrize("argument", ["entry", "folder"])
async def test_deleting_a_folder_workflow_removes_only_its_entry(tmp_path: Path, argument: str) -> None:
    catalog = _catalog(tmp_path)
    entry = write_workflow_package(catalog.workdir, "pkg", SOURCE, {"helpers.py": b"VALUE = 1\n"})
    _write(entry.parent / ".git" / "HEAD", b"ref\n")
    history = _write(tmp_path / "session" / "workflows" / "run" / "source.py", SOURCE)
    preview = await catalog.preview("pkg", trust=True)
    catalog.confirm(preview)
    assert catalog.ledger().recorded(preview.source.canonical_path, preview.source.source_kind) is not None

    catalog.delete(preview.source.canonical_path if argument == "entry" else str(entry.parent.resolve()))

    assert not entry.exists()
    assert sorted(path.name for path in entry.parent.iterdir()) == [".git", "helpers.py"]
    assert history.read_bytes() == SOURCE
    assert catalog.ledger().recorded(preview.source.canonical_path, preview.source.source_kind) is None
    assert catalog.discover().find("pkg") is None


def test_an_entry_inside_a_folder_of_its_name_can_be_deleted(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    entry = _write(global_workflows_dir(catalog.config_dir) / "nested" / "nested.py", SOURCE)

    catalog.delete(str(entry.resolve()))

    assert not entry.exists() and entry.parent.is_dir()


@pytest.mark.parametrize("kind", ["symlink", "junction"])
@pytest.mark.parametrize("argument", ["entry", "folder"])
def test_a_folder_swapped_for_a_link_is_not_deleted_through(tmp_path: Path, kind: str, argument: str) -> None:
    catalog = _catalog(tmp_path)
    entry = write_workflow_package(catalog.workdir, "pkg", SOURCE)
    canonical = entry.resolve()
    target = tmp_path / "elsewhere"
    target.mkdir()
    target_entry = _write(target / "pkg.py", SOURCE)
    folder = entry.parent
    entry.unlink()
    folder.rmdir()
    if kind == "symlink":
        symlink_or_skip(folder, target, target_is_directory=True)
    else:
        junction_or_skip(folder, target)

    with pytest.raises(ValueError, match="is a link; remove the link yourself"):
        catalog.delete(str(canonical) if argument == "entry" else str(canonical.parent))

    assert target_entry.read_bytes() == SOURCE


@pytest.mark.parametrize(
    ("entry_kind", "refusal"),
    [("link", r"pkg\.py is a link; remove the link yourself"), ("folder", r"pkg\.py is not a regular file")],
    ids=["link", "folder"],
)
def test_an_entry_that_is_not_a_file_is_refused_by_the_check_before_any_confirmation(
    tmp_path: Path, entry_kind: str, refusal: str
) -> None:
    catalog = _catalog(tmp_path)
    entry = write_workflow_package(catalog.workdir, "pkg", SOURCE, {"helpers.py": b"VALUE = 1\n"})
    entry.unlink()
    if entry_kind == "link":
        target = _write(tmp_path / "elsewhere.py", SOURCE)
        symlink_or_skip(entry, target)
    else:
        _write(entry / "inner.py", SOURCE)
    [skipped] = [item for item in catalog.discover().skipped if Path(item.path).name == "pkg"]
    before = sorted(path.name for path in entry.parent.rglob("*"))

    for operation in (catalog.check_delete, catalog.delete):
        with pytest.raises(ValueError, match=refusal):
            operation(skipped.path)

    assert sorted(path.name for path in entry.parent.rglob("*")) == before


def test_an_entry_another_user_owns_is_refused_by_the_check_before_any_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _catalog(tmp_path)
    entry = write_workflow_package(catalog.workdir, "pkg", SOURCE, {"helpers.py": b"VALUE = 1\n"})
    if sys.platform == "win32":

        def foreign_owner(fd: int) -> None:
            raise files_module.SecureFileError("Secure file is not owned by the current Windows user.")

        monkeypatch.setattr(files_module, "_verify_windows_owner_identity", foreign_owner)
    else:
        owner = entry.stat().st_uid
        monkeypatch.setattr(files_module, "_posix_effective_uid", lambda: owner + 1)

    for operation in (catalog.check_delete, catalog.delete):
        with pytest.raises(ValueError, match=r"pkg\.py could not be verified as yours to delete"):
            operation(str(entry.parent.resolve()))

    assert entry.read_bytes() == SOURCE


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_an_entry_you_own_but_cannot_read_still_passes_the_check_and_is_deleted(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    entry = write_workflow_package(catalog.workdir, "pkg", SOURCE, {"helpers.py": b"VALUE = 1\n"})
    entry.chmod(0)

    catalog.check_delete(str(entry.parent.resolve()))
    catalog.delete(str(entry.parent.resolve()))

    assert not entry.exists() and (entry.parent / "helpers.py").exists()


def test_an_entry_the_file_system_would_keep_is_refused_by_the_check_before_any_confirmation(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    entry = write_workflow_package(catalog.workdir, "pkg", SOURCE, {"helpers.py": b"VALUE = 1\n"})
    if sys.platform != "win32" and os.geteuid() == 0:
        pytest.skip("root may delete from a read-only folder")
    # POSIX keeps the files of a folder that can't be written; Windows keeps a read-only file.
    locked = entry if sys.platform == "win32" else entry.parent
    mode = locked.stat().st_mode
    locked.chmod(stat.S_IREAD if sys.platform == "win32" else 0o555)
    try:
        for operation in (catalog.check_delete, catalog.delete):
            with pytest.raises(ValueError, match=r"pkg\.py could not be verified as yours to delete"):
                operation(str(entry.parent.resolve()))
    finally:
        locked.chmod(mode)

    assert entry.read_bytes() == SOURCE


def test_the_delete_check_passes_a_deletable_folder_workflow_and_deletes_nothing(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    entry = write_workflow_package(catalog.workdir, "pkg", SOURCE, {"helpers.py": b"VALUE = 1\n"})

    catalog.check_delete(str(entry.parent.resolve()))
    catalog.check_delete(str(entry.resolve()))

    assert entry.read_bytes() == SOURCE
