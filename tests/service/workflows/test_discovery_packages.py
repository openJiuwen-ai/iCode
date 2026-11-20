# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow folders: ``<id>/<id>.py`` candidates, their precedence, what a folder's digest covers, and what is refused."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

import chrys.service.workflows.discovery as discovery_module
from chrys.foundation.platform.files import atomic_write_owner_only_bytes
from chrys.service.workflows.discovery import (
    LAYOUT_FILE,
    LAYOUT_PACKAGE,
    SOURCE_KIND_BUILTIN,
    SOURCE_KIND_GLOBAL,
    SOURCE_KIND_WORKDIR,
    Discovery,
    WorkflowSourceError,
    discover_workflows,
    global_workflows_dir,
    package_signature,
    read_source,
    workdir_workflows_dir,
)
from tests.support.symlinks import junction_or_skip, symlink_or_skip


@pytest.fixture(autouse=True)
def _builtins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    builtin_dir = tmp_path / "builtins"
    builtin_dir.mkdir()
    monkeypatch.setattr(discovery_module, "BUILTIN_DIR", builtin_dir)
    return builtin_dir


def _write(path: Path, body: bytes = b"workflow = None\n") -> Path:
    """A file as its author would leave it; see tests/support/secure_files.py for why not ``write_bytes``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_owner_only_bytes(path, body)
    return path


def _package(root: Path, workflow_id: str, files: dict[str, bytes] | None = None) -> Path:
    """A workflow folder with its entry and *files*; returns the folder."""
    folder = root / workflow_id
    _write(folder / f"{workflow_id}.py", f"# {workflow_id} entry\n".encode())
    for relative, body in (files or {}).items():
        _write(folder / relative, body)
    return folder


def _discover(tmp_path: Path) -> Discovery:
    return discover_workflows(config_dir=tmp_path / "config", workdir=tmp_path / "workdir")


def _workdir(tmp_path: Path) -> Path:
    return workdir_workflows_dir(tmp_path / "workdir")


def _global(tmp_path: Path) -> Path:
    return global_workflows_dir(tmp_path / "config")


def test_a_folder_holding_an_entry_of_its_name_is_a_workflow(tmp_path: Path) -> None:
    folder = _package(_workdir(tmp_path), "review", {"helpers.py": b"VALUE = 1\n", "prompts/ask.md": b"ask\n"})
    _package(_global(tmp_path), "nightly")

    found = _discover(tmp_path)

    assert [(s.workflow_id, s.source_kind, s.layout) for s in found.sources] == [
        ("nightly", SOURCE_KIND_GLOBAL, LAYOUT_PACKAGE),
        ("review", SOURCE_KIND_WORKDIR, LAYOUT_PACKAGE),
    ]
    source = found.find("review")
    assert source is not None and source.package is not None
    assert source.canonical_path == str(folder.resolve() / "review.py")
    assert source.source == b"# review entry\n"
    assert source.package.directory == str(folder.resolve())
    assert (source.package.file_count, source.package.total_bytes) == (3, len(b"# review entry\nVALUE = 1\nask\n"))
    # A confirmation pins the whole folder, not only the entry.
    assert source.source_digest == source.package.digest != source.entry_sha256
    assert found.skipped == () and found.shadowed == ()


def test_a_single_file_digest_is_still_its_entry_hash(tmp_path: Path) -> None:
    _write(_workdir(tmp_path) / "single.py")

    [source] = _discover(tmp_path).sources

    assert (source.layout, source.package) == (LAYOUT_FILE, None)
    assert source.source_digest == source.entry_sha256 == hashlib.sha256(b"workflow = None\n").hexdigest()


def test_the_digest_format_is_pinned(tmp_path: Path) -> None:
    """Saved confirmations hold this digest: changing how it is computed silently revokes all of them."""
    _package(_workdir(tmp_path), "pinned", {"注释.py": "x = '中文'\n".encode(), "data/b.txt": b"b"})

    [source] = _discover(tmp_path).sources

    files = sorted(
        [relpath, hashlib.sha256(body).hexdigest()]
        for relpath, body in [
            ("pinned.py", b"# pinned entry\n"),
            ("注释.py", "x = '中文'\n".encode()),
            ("data/b.txt", b"b"),
        ]
    )
    document = {"format": "chrys-workflow-package-1", "files": files}
    encoded = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    assert source.source_digest == hashlib.sha256(encoded).hexdigest()


def test_precedence_is_workdir_folder_then_file_then_global_folder_then_file_then_builtin(
    tmp_path: Path, _builtins: Path
) -> None:
    _package(_workdir(tmp_path), "x")
    _write(_workdir(tmp_path) / "x.py", b"# workdir file\n")
    _package(_global(tmp_path), "x")
    _write(_global(tmp_path) / "x.py", b"# global file\n")
    _write(_builtins / "x.py", b"# builtin\n")

    found = _discover(tmp_path)

    [winner] = found.sources
    assert (winner.source_kind, winner.layout) == (SOURCE_KIND_WORKDIR, LAYOUT_PACKAGE)
    assert [(item.source.source_kind, item.source.layout) for item in found.shadowed] == [
        (SOURCE_KIND_WORKDIR, LAYOUT_FILE),
        (SOURCE_KIND_GLOBAL, LAYOUT_PACKAGE),
        (SOURCE_KIND_GLOBAL, LAYOUT_FILE),
        (SOURCE_KIND_BUILTIN, LAYOUT_FILE),
    ]
    assert all(item.shadowed_by is winner for item in found.shadowed)


def test_a_broken_folder_is_reported_and_the_next_candidate_wins(tmp_path: Path) -> None:
    folder = _workdir(tmp_path) / "x"
    (folder / "x.py").mkdir(parents=True)
    _write(_workdir(tmp_path) / "x.py", b"# workdir file\n")

    found = _discover(tmp_path)

    [winner] = found.sources
    assert (winner.layout, winner.source) == (LAYOUT_FILE, b"# workdir file\n")
    [skipped] = found.skipped
    assert (skipped.path, skipped.workflow_id, skipped.source_kind) == (str(folder.resolve()), "x", SOURCE_KIND_WORKDIR)
    assert skipped.reason == "the entry x.py must be a regular file, not a folder"
    assert found.shadowed == ()


@pytest.mark.parametrize(("kind", "found"), [("link", "a link"), ("pipe", "a special file")])
def test_an_entry_that_is_not_a_regular_file_is_reported(tmp_path: Path, kind: str, found: str) -> None:
    folder = _workdir(tmp_path) / "x"
    folder.mkdir(parents=True)
    if kind == "link":
        symlink_or_skip(folder / "x.py", _write(tmp_path / "outside.py"))
    elif sys.platform == "win32":
        pytest.skip("named pipes are POSIX file system entries")
    else:
        os.mkfifo(folder / "x.py")

    found_workflows = _discover(tmp_path)

    assert found_workflows.sources == ()
    [skipped] = found_workflows.skipped
    assert (skipped.path, skipped.workflow_id) == (str(folder.resolve()), "x")
    assert skipped.reason == f"the entry x.py must be a regular file, not {found}"


@pytest.mark.parametrize(
    ("folder", "entry"),
    [("Review", "review.py"), ("review", "Review.py"), ("review", "review.PY"), ("review", "other.py")],
)
def test_only_an_entry_spelled_exactly_like_its_folder_counts(tmp_path: Path, folder: str, entry: str) -> None:
    # Names compare as listed, so a case-insensitive file system doesn't widen the match either.
    _write(_workdir(tmp_path) / folder / entry)

    found = _discover(tmp_path)

    assert (found.sources, found.skipped) == ((), ())


def test_folders_below_a_folder_and_builtin_folders_are_not_workflows(tmp_path: Path, _builtins: Path) -> None:
    _write(_workdir(tmp_path) / "group" / "inner" / "inner.py")
    _package(_builtins, "template")

    found = _discover(tmp_path)

    assert (found.sources, found.skipped) == ((), ())


def test_hidden_and_private_folders_are_not_workflows(tmp_path: Path) -> None:
    _package(_workdir(tmp_path), ".hidden")
    _package(_workdir(tmp_path), "_private")

    assert _discover(tmp_path).sources == ()


@pytest.mark.parametrize("name", ["sdk", "SDK"])
def test_the_global_sdk_folder_is_reserved(tmp_path: Path, name: str) -> None:
    _package(_global(tmp_path), name)
    _package(_workdir(tmp_path), "sdk")

    found = _discover(tmp_path)

    # Only the workdir may use the name; the global copy is reported, not silently dropped.
    [source] = found.sources
    assert (source.workflow_id, source.source_kind) == ("sdk", SOURCE_KIND_WORKDIR)
    [skipped] = found.skipped
    assert (skipped.workflow_id, skipped.source_kind) == (name, SOURCE_KIND_GLOBAL)
    assert skipped.reason == 'the name "sdk" is reserved in the global workflow folder'


@pytest.mark.parametrize("workdir", ["home", "linked-home"])
def test_the_sdk_folder_stays_reserved_when_the_workdir_folder_is_the_global_one(tmp_path: Path, workdir: str) -> None:
    home = tmp_path / "home"
    config_dir = home / ".chrys"
    _package(global_workflows_dir(config_dir), "sdk")
    workdir = home
    if workdir == "linked-home":
        workdir = tmp_path / "linked"
        symlink_or_skip(workdir, home, target_is_directory=True)

    # Run from the home directory, the workdir's workflow folder is the global one.
    found = discover_workflows(config_dir=config_dir, workdir=workdir)

    assert found.sources == ()
    assert {skipped.reason for skipped in found.skipped} == {'the name "sdk" is reserved in the global workflow folder'}


def test_the_global_sdk_cache_folder_is_not_reported(tmp_path: Path) -> None:
    _write(_global(tmp_path) / "sdk" / "chrys" / "__init__.py")

    found = _discover(tmp_path)

    assert (found.sources, found.skipped) == ((), ())


def _link_folder(kind: str, link: Path, target: Path) -> None:
    if kind == "symlink":
        symlink_or_skip(link, target, target_is_directory=True)
    else:
        junction_or_skip(link, target)


@pytest.mark.parametrize("kind", ["symlink", "junction"])
def test_a_workflow_folder_that_is_a_link_is_reported_unresolved(tmp_path: Path, kind: str) -> None:
    target = _package(tmp_path / "elsewhere", "x")
    _workdir(tmp_path).mkdir(parents=True)
    _link_folder(kind, _workdir(tmp_path) / "x", target)
    _link_folder(kind, _workdir(tmp_path) / "y", tmp_path / "elsewhere")

    found = _discover(tmp_path)

    assert found.sources == ()
    # A link to a folder without a matching entry is just not a workflow.
    [skipped] = found.skipped
    assert (skipped.path, skipped.workflow_id) == (str(_workdir(tmp_path).resolve() / "x"), "x")
    assert skipped.reason == "workflow folders can't be links"
    assert (target / "x.py").read_bytes() == b"# x entry\n"


@pytest.mark.parametrize("member", ["helpers.py", "lib", "__pycache__"])
def test_a_link_inside_a_folder_is_refused(tmp_path: Path, member: str) -> None:
    folder = _package(_workdir(tmp_path), "x")
    target = _write(tmp_path / "outside" / "shared.py")
    # Only a real __pycache__ folder is left out; a link of that name is a member like any other.
    if member != "helpers.py":
        symlink_or_skip(folder / member, target.parent, target_is_directory=True)
    else:
        symlink_or_skip(folder / member, target)

    found = _discover(tmp_path)

    assert found.sources == ()
    [skipped] = found.skipped
    assert (skipped.path, skipped.reason) == (str(folder.resolve()), f"contains a link: {member}")


@pytest.mark.parametrize("member", ["lib", "__pycache__"])
def test_a_junction_inside_a_folder_is_refused(tmp_path: Path, member: str) -> None:
    folder = _package(_workdir(tmp_path), "x")
    _write(tmp_path / "outside" / "shared.py")
    _link_folder("junction", folder / member, tmp_path / "outside")

    [skipped] = _discover(tmp_path).skipped

    assert skipped.reason == f"contains a link: {member}"


@pytest.mark.skipif(sys.platform == "win32", reason="named pipes are POSIX file system entries")
def test_a_special_file_inside_a_folder_is_refused(tmp_path: Path) -> None:
    folder = _package(_workdir(tmp_path), "x")
    (folder / "data").mkdir()
    os.mkfifo(folder / "data" / "pipe")

    [skipped] = _discover(tmp_path).skipped

    assert skipped.reason == "unsupported file type: data/pipe"


@pytest.mark.parametrize(
    ("limit", "value", "files", "problem"),
    [
        ("MAX_PACKAGE_ENTRIES", 3, {"a.py": b"", "b/c.py": b""}, "has more than 3 files and folders"),
        ("MAX_PACKAGE_BYTES", 20, {"a.py": b"x" * 11}, "is larger than 20 bytes in total"),
        ("MAX_PACKAGE_DEPTH", 2, {"a/b/c.py": b""}, "is nested deeper than 2 levels at a/b/c.py"),
    ],
)
def test_a_folder_over_a_limit_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str, value: int, files: dict[str, bytes], problem: str
) -> None:
    monkeypatch.setattr(discovery_module, limit, value)
    folder = _package(_workdir(tmp_path), "x", files)
    _write(folder / ".venv" / "lib" / "big.py", b"y" * 100)

    found = _discover(tmp_path)

    assert found.sources == ()
    [skipped] = found.skipped
    assert skipped.reason == (
        f"the workflow folder {problem} (hidden entries such as .venv and bytecode caches in __pycache__ don't count)"
    )


def test_a_member_hashed_in_chunks_gets_the_whole_file_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _package(_workdir(tmp_path), "x", {"tool.bin": bytes(range(256)) * 3})
    [whole] = _discover(tmp_path).sources
    monkeypatch.setattr(discovery_module, "_HASH_CHUNK_BYTES", 5)

    [chunked] = _discover(tmp_path).sources

    assert chunked.source_digest == whole.source_digest
    assert chunked.package is not None and chunked.package.total_bytes == len(b"# x entry\n") + 768


def test_a_member_that_grows_after_the_size_check_is_read_in_chunks_only_past_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(discovery_module, "MAX_PACKAGE_BYTES", 40)
    monkeypatch.setattr(discovery_module, "_HASH_CHUNK_BYTES", 4)
    folder = _package(_workdir(tmp_path), "x", {"tool.bin": b"small"})
    real_open = discovery_module.secure_open_owner_verified_binary
    reads: list[int] = []

    class Counted:
        def __init__(self, path: Path) -> None:
            self._handle = real_open(path)

        def __enter__(self) -> Counted:
            return self

        def __exit__(self, *_: object) -> None:
            self._handle.close()

        def read(self, size: int) -> bytes:
            chunk = self._handle.read(size)
            reads.append(len(chunk))
            return chunk

    def grow_then_open(path: Path) -> object:
        if Path(path).name != "tool.bin":
            return real_open(path)
        with open(path, "ab") as grown:
            grown.write(b"y" * 1000)
        return Counted(path)

    monkeypatch.setattr(discovery_module, "secure_open_owner_verified_binary", grow_then_open)

    with pytest.raises(WorkflowSourceError) as raised:
        read_source(folder / "x.py", SOURCE_KIND_WORKDIR, layout=LAYOUT_PACKAGE)

    assert str(raised.value) == (
        "the workflow folder is larger than 40 bytes in total"
        " (hidden entries such as .venv and bytecode caches in __pycache__ don't count)"
    )
    # tool.bin sorts before the entry, so it is read with the whole budget: 41 bytes, never the other 964.
    assert sum(reads) == 41 and max(reads) == 4


def test_hidden_entries_and_bytecode_caches_are_outside_the_digest(tmp_path: Path) -> None:
    folder = _package(_workdir(tmp_path), "x", {"helpers.py": b"VALUE = 1\n"})
    [before] = _discover(tmp_path).sources

    _write(folder / ".venv" / "lib" / "site.py", b"anything\n")
    _write(folder / ".git" / "HEAD", b"ref\n")
    _write(folder / "__pycache__" / "helpers.cpython-314.pyc", b"\0")
    _write(folder / "sub" / "__pycache__" / "y.cpython-39.opt-1.pyc", b"\0")
    _write(folder / "sub" / "__pycache__" / "y.cpython-314.pyc.4391", b"\0")
    [after] = _discover(tmp_path).sources

    assert after.source_digest == before.source_digest
    assert after.package is not None and after.package.file_count == 2

    _write(folder / "helpers.py", b"VALUE = 2\n")
    [changed] = _discover(tmp_path).sources
    assert changed.source_digest != before.source_digest
    # A new, empty file is covered too.
    _write(folder / "sub" / "empty.txt", b"")
    [grown] = _discover(tmp_path).sources
    assert grown.source_digest not in {before.source_digest, changed.source_digest}


def test_a_file_named_like_a_bytecode_cache_folder_is_covered(tmp_path: Path) -> None:
    folder = _package(_workdir(tmp_path), "x")
    [before] = _discover(tmp_path).sources

    _write(folder / "__pycache__", b"data\n")

    [after] = _discover(tmp_path).sources
    assert after.package is not None and after.package.file_count == 2
    assert after.source_digest != before.source_digest


@pytest.mark.parametrize("name", ["tools.py", "tools.pyc", "tools.cpython-314.pyc.txt"])
def test_a_bytecode_cache_folder_covers_what_an_import_could_load(tmp_path: Path, name: str) -> None:
    folder = _package(_workdir(tmp_path), "x")
    _write(folder / "__pycache__" / "x.cpython-314.pyc", b"\0")
    [before] = _discover(tmp_path).sources

    # `import __pycache__.tools` would load either of the first two.
    _write(folder / "__pycache__" / name, b"VALUE = 1\n")

    [after] = _discover(tmp_path).sources
    assert after.package is not None and after.package.file_count == 2
    assert after.source_digest != before.source_digest


def test_only_regular_files_are_taken_for_bytecode_caches(tmp_path: Path) -> None:
    folder = _package(_workdir(tmp_path), "x")
    _write(folder / "__pycache__" / "lib.cpython-314.pyc" / "steps.py", b"VALUE = 1\n")
    [before] = _discover(tmp_path).sources
    assert before.package is not None and before.package.file_count == 2

    _write(folder / "__pycache__" / "lib.cpython-314.pyc" / "steps.py", b"VALUE = 2\n")
    [after] = _discover(tmp_path).sources
    assert after.source_digest != before.source_digest

    symlink_or_skip(folder / "__pycache__" / "x.cpython-314.pyc", _write(tmp_path / "outside.pyc"))
    [skipped] = _discover(tmp_path).skipped
    assert skipped.reason == "contains a link: __pycache__/x.cpython-314.pyc"


def test_the_signature_follows_the_covered_files(tmp_path: Path) -> None:
    folder = _package(_workdir(tmp_path), "x", {"helpers.py": b"VALUE = 1\n"})
    [source] = _discover(tmp_path).sources
    assert source.package is not None

    assert package_signature(folder.resolve()) == source.package.signature
    _write(folder / "__pycache__" / "helpers.cpython-314.pyc", b"\0")
    assert package_signature(folder.resolve()) == source.package.signature
    _write(folder / "helpers.py", b"VALUE = 22\n")
    assert package_signature(folder.resolve()) != source.package.signature


@pytest.mark.skipif(sys.platform == "win32", reason="Windows file names are Unicode, never undecodable bytes")
def test_an_undecodable_name_and_its_escaped_spelling_give_different_digests(tmp_path: Path) -> None:
    # The same folder in two workdirs, differing only in how one member's name is spelled.
    raw, literal = tmp_path / "raw", tmp_path / "literal"
    try:
        _write(_package(workdir_workflows_dir(raw), "x") / os.fsdecode(b"name\xff.py"), b"same\n")
    except OSError as error:
        pytest.skip(f"this file system refuses undecodable names: {error}")
    _write(_package(workdir_workflows_dir(literal), "x") / "name\\udcff.py", b"same\n")

    [raw_source] = discover_workflows(config_dir=tmp_path / "config", workdir=raw).sources
    [literal_source] = discover_workflows(config_dir=tmp_path / "config", workdir=literal).sources

    assert raw_source.source_digest != literal_source.source_digest


def test_reading_a_folder_needs_the_folder_layout_and_a_matching_entry(tmp_path: Path) -> None:
    folder = _package(_workdir(tmp_path), "x")
    _write(folder / "other.py")

    with pytest.raises(ValueError, match="named after the folder"):
        read_source(folder / "other.py", SOURCE_KIND_WORKDIR, layout=LAYOUT_PACKAGE)
    with pytest.raises(ValueError, match="single files"):
        read_source(folder / "x.py", SOURCE_KIND_BUILTIN, layout=LAYOUT_PACKAGE)
    # The global root's own name says nothing about layout: workflows/workflows.py is a file workflow.
    root_file = _write(_global(tmp_path) / "workflows.py")
    assert read_source(root_file, SOURCE_KIND_GLOBAL, layout=LAYOUT_FILE).package is None


def test_a_member_that_changes_while_it_is_read_is_reported_with_its_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _package(_workdir(tmp_path), "x", {"helpers.py": b"VALUE = 1\n"})
    real_open = discovery_module.secure_open_owner_verified_binary

    def refuse_helper(path: Path):
        if Path(path).name == "helpers.py":
            raise PermissionError(13, "Permission denied")
        return real_open(path)

    monkeypatch.setattr(discovery_module, "secure_open_owner_verified_binary", refuse_helper)

    with pytest.raises(WorkflowSourceError) as raised:
        read_source(folder / "x.py", SOURCE_KIND_WORKDIR, layout=LAYOUT_PACKAGE)

    assert (raised.value.reason, raised.value.detail) == ("unreadable", "Permission denied")
    assert raised.value.path == str(folder.resolve() / "helpers.py")
    assert str(raised.value) == "could not read helpers.py: Permission denied"
