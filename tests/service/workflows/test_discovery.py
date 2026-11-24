# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow discovery: the three source layers, precedence by id, what is skipped, and the bytes that are read."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

import chrys.service.workflows.discovery as discovery_module
from chrys.foundation.platform.files import atomic_write_owner_only_bytes
from chrys.service.workflows.discovery import (
    MAX_SOURCE_BYTES,
    SOURCE_KIND_BUILTIN,
    SOURCE_KIND_GLOBAL,
    SOURCE_KIND_WORKDIR,
    discover_workflows,
    global_workflows_dir,
    workdir_workflows_dir,
)


def _write(directory: Path, name: str, body: bytes = b"workflow = None\n") -> Path:
    """A file as its author would leave it; see tests/support/secure_files.py for why not ``write_bytes``."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    atomic_write_owner_only_bytes(path, body)
    return path


def test_layers_shadow_by_workflow_id_and_the_result_is_sorted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    builtin_dir = tmp_path / "builtins"
    monkeypatch.setattr(discovery_module, "BUILTIN_DIR", builtin_dir)
    config_dir = tmp_path / "config"
    workdir = tmp_path / "workdir"
    _write(builtin_dir, "demo-workflow.py", b"# builtin\n")
    _write(builtin_dir, "alpha.py", b"# builtin alpha\n")
    _write(global_workflows_dir(config_dir), "demo-workflow.py", b"# global\n")
    _write(global_workflows_dir(config_dir), "mine.py", b"# global mine\n")
    _write(workdir_workflows_dir(workdir), "mine.py", b"# workdir mine\n")
    _write(workdir_workflows_dir(workdir), "zeta.py", b"# workdir zeta\n")

    found = discover_workflows(config_dir=config_dir, workdir=workdir)

    assert [(s.workflow_id, s.source_kind) for s in found.sources] == [
        ("alpha", SOURCE_KIND_BUILTIN),
        ("demo-workflow", SOURCE_KIND_GLOBAL),
        ("mine", SOURCE_KIND_WORKDIR),
        ("zeta", SOURCE_KIND_WORKDIR),
    ]
    assert found.find("mine") is not None
    assert found.find("mine").source == b"# workdir mine\n"
    assert found.find("missing") is None
    assert found.skipped == ()
    assert [
        (item.source.workflow_id, item.source.source_kind, item.shadowed_by.source_kind) for item in found.shadowed
    ] == [
        ("demo-workflow", SOURCE_KIND_BUILTIN, SOURCE_KIND_GLOBAL),
        ("mine", SOURCE_KIND_GLOBAL, SOURCE_KIND_WORKDIR),
    ]


def test_without_a_workdir_only_builtins_and_global_files_are_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery_module, "BUILTIN_DIR", tmp_path / "no-builtins")
    config_dir = tmp_path / "config"
    _write(global_workflows_dir(config_dir), "mine.py")
    found = discover_workflows(config_dir=config_dir, workdir=None)
    assert [s.workflow_id for s in found.sources] == ["mine"]
    assert found.sources[0].canonical_path == str((global_workflows_dir(config_dir) / "mine.py").resolve())


def test_a_source_records_the_exact_bytes_and_their_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery_module, "BUILTIN_DIR", tmp_path / "no-builtins")
    body = "wf = 1  # 中文 注释\r\n".encode()
    _write(global_workflows_dir(tmp_path), "bytes.py", body)
    source = discover_workflows(config_dir=tmp_path, workdir=None).find("bytes")
    assert source is not None
    assert source.source == body
    assert source.entry_sha256 == hashlib.sha256(body).hexdigest()


def test_only_plain_python_files_directly_under_the_directory_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery_module, "BUILTIN_DIR", tmp_path / "no-builtins")
    directory = global_workflows_dir(tmp_path)
    _write(directory, "ok.py")
    _write(directory, "_private.py")
    _write(directory, ".hidden.py")
    _write(directory, "notes.txt")
    _write(directory, ".py")
    _write(directory / "nested", "deep.py")
    (directory / "sdk").mkdir(exist_ok=True)
    found = discover_workflows(config_dir=tmp_path, workdir=None)
    assert [s.workflow_id for s in found.sources] == ["ok"]
    assert found.skipped == ()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs a privilege on Windows")
def test_a_symlinked_workflow_file_is_reported_without_following_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery_module, "BUILTIN_DIR", tmp_path / "no-builtins")
    directory = global_workflows_dir(tmp_path)
    target = _write(tmp_path / "elsewhere", "real.py")
    directory.mkdir(parents=True)
    os.symlink(target, directory / "linked.py")
    found = discover_workflows(config_dir=tmp_path, workdir=None)
    assert found.sources == ()
    assert len(found.skipped) == 1
    assert found.skipped[0].path == str(directory / "linked.py")
    assert found.skipped[0].source_kind == SOURCE_KIND_GLOBAL


def test_an_oversized_file_is_skipped_with_its_reason(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery_module, "BUILTIN_DIR", tmp_path / "no-builtins")
    directory = global_workflows_dir(tmp_path)
    _write(directory, "small.py")
    big = _write(directory, "big.py", b"#" * (MAX_SOURCE_BYTES + 1))
    found = discover_workflows(config_dir=tmp_path, workdir=None)
    assert [s.workflow_id for s in found.sources] == ["small"]
    assert [(Path(item.path), "larger than" in item.reason) for item in found.skipped] == [(big.resolve(), True)]


def test_missing_directories_are_not_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(discovery_module, "BUILTIN_DIR", tmp_path / "no-builtins")
    found = discover_workflows(config_dir=tmp_path / "no-config", workdir=tmp_path / "no-workdir")
    assert found.sources == ()
    assert found.skipped == ()
