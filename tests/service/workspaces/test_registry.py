# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace registry membership, validation and atomic read/write behavior."""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import threading

import pytest

from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs
from chrys.service.workspaces import registry as registry_module
from chrys.service.workspaces.paths import contains, folders_overlap, resolve_workspace_directory
from chrys.service.workspaces.registry import (
    WorkspaceNameError,
    WorkspaceRegistry,
    WorkspaceRegistryError,
    validate_workspace_folders,
)
from tests.support.symlinks import symlink_or_skip
from tests.support.workspace_registry import make_workdirs
from tests.support.workspace_registry import registry as registry


def test_registry_format_and_membership_writes(registry, tmp_path):
    assert registry.snapshot().workspaces == ()
    assert not registry.path.exists()
    first = registry.create("First", str(tmp_path))
    second_root = tmp_path / "second"
    second_root.mkdir()
    second = registry.create("Second", str(second_root))
    assert registry.snapshot().workspace_id_for_session("one") is None
    assert registry.set_session_workspace("one", first.id, if_absent=True)
    assert not registry.set_session_workspace("one", second.id, if_absent=True)
    assert registry.set_session_workspace("two", second.id)
    assert registry.set_session_workspace("one", second.id)
    assert not registry.set_session_workspace("one", None, leaving=first.id)
    assert registry.snapshot().sessions == {"one": second.id, "two": second.id}
    assert registry.set_session_workspace("one", None, leaving=second.id)
    registry.delete(first.id)
    payload = json.loads(registry.path.read_bytes())
    assert payload == {
        "version": 1,
        "workspaces": [
            {
                "id": second.id,
                "name": "Second",
                "primary_cwd": str(second_root),
                "working_dirs": [],
                "last_used_at": "",
            }
        ],
        "sessions": {"two": second.id},
    }
    before = registry.path.read_bytes(), registry.path.stat().st_mtime_ns
    assert registry.set_session_workspace("two", second.id)
    assert registry.set_session_workspace("missing", None)
    assert (registry.path.read_bytes(), registry.path.stat().st_mtime_ns) == before


FULL = "4201eebc-ca45-4328-8882-272f3d7c41cb"
TWIN = "4201eebc-ca45-4999-8882-000000000000"
SHORT = "4201eebcca45"


@pytest.mark.parametrize("clear", [False, True])
@pytest.mark.parametrize("saved_id", [FULL, SHORT])
def test_delete_can_clear_the_active_session_mapped_to_another_workspace(registry, tmp_path, clear, saved_id):
    first, second = make_workdirs(tmp_path, "first", "second")
    current = registry.create("Current", str(first))
    target = registry.create("Target", str(second))
    registry.set_session_workspace(saved_id, target.id)
    registry.set_session_workspace("other-session", target.id)
    registry.set_session_workspace("current-session", current.id)

    registry.delete(current.id, expected=current, clear_session_id=saved_id.upper() if clear else None)

    payload = json.loads(registry.path.read_bytes())
    assert payload["sessions"] == (
        {"other-session": target.id} if clear else {saved_id: target.id, "other-session": target.id}
    )
    assert [item["id"] for item in payload["workspaces"]] == [target.id]


@pytest.mark.parametrize(
    ("saved", "requested", "found", "untouched"),
    [
        pytest.param((FULL,), FULL.upper(), True, (), id="uuid-uppercase"),
        pytest.param((FULL,), FULL.replace("-", ""), True, (), id="uuid-no-dashes"),
        pytest.param((FULL, FULL.upper()), FULL.replace("-", "").upper(), True, (), id="uuid-duplicate-spellings"),
        pytest.param((FULL,), TWIN, False, (FULL,), id="unmapped-twin"),
        pytest.param((FULL, TWIN), FULL, True, (TWIN,), id="mapped-twin"),
        pytest.param((FULL,), SHORT, False, (FULL,), id="short-is-not-full"),
        pytest.param((SHORT,), FULL, False, (SHORT,), id="full-is-not-short"),
        pytest.param((FULL, TWIN, SHORT), SHORT.upper(), True, (FULL, TWIN), id="saved-short-id"),
        pytest.param(("opaque-1",), "opaque1", False, ("opaque-1",), id="opaque-dash"),
        pytest.param(("opaque-1",), "Opaque-1", False, ("opaque-1",), id="opaque-case"),
        pytest.param(("opaque-1", "opaque1"), "opaque-1", True, ("opaque1",), id="opaque-exact"),
    ],
)
@pytest.mark.parametrize("operation", ["read", "rebind", "remove"])
def test_session_mappings_use_saved_ids(registry, tmp_path, saved, requested, found, untouched, operation):
    roots = make_workdirs(tmp_path, "original", "replacement", "other")
    original, replacement, other = (
        registry.create(name, str(root)) for name, root in zip(("Original", "Replacement", "Other"), roots, strict=True)
    )
    initial = {key: other.id if key == TWIN else original.id for key in saved}
    payload = json.loads(registry.path.read_bytes())
    payload["sessions"] = initial
    registry.path.write_text(json.dumps(payload), encoding="utf-8")

    if operation == "read":
        snapshot = registry.snapshot()
        assert snapshot.maps_session(requested) is found
        assert snapshot.session_workspace(requested) == (original if found else None)
        assert snapshot.workspace_id_for_session(requested) == (original.id if found else None)
        return

    expected = {key: initial[key] for key in untouched}
    if operation == "rebind":
        assert registry.set_session_workspace(requested, replacement.id)
        expected[requested] = replacement.id
    else:
        registry.remove_session_workspace(requested)
    assert json.loads(registry.path.read_bytes())["sessions"] == expected


def test_registry_ignores_dangling_session_mappings(registry, tmp_path):
    workspace = registry.create("Team", str(tmp_path))
    payload = json.loads(registry.path.read_bytes())
    payload["sessions"] = {"live": workspace.id, "orphan": "ws_" + "f" * 32}
    registry.path.write_text(json.dumps(payload), encoding="utf-8")

    snapshot = registry.snapshot()
    assert snapshot.error is None
    assert snapshot.sessions == {"live": workspace.id}
    assert not snapshot.maps_session("orphan")
    assert snapshot.session_workspace("orphan") is None
    assert snapshot.workspace_id_for_session("orphan") is None
    registry.touch(workspace.id)
    assert json.loads(registry.path.read_bytes())["sessions"] == {"live": workspace.id}


@pytest.mark.parametrize(
    ("payload", "error", "key"),
    [
        pytest.param(b"broken", "Cannot read the workspace list", None, id="malformed-json"),
        pytest.param(
            b'{"version":1,"sessions":[],"workspaces":[]}', "Cannot read the workspace list", None, id="invalid-shape"
        ),
        pytest.param(
            b'{"version":2,"workspaces":[],"sessions":{}}',
            "The workspace list was written by a newer version of iCode",
            "workspaces.registry_newer_version",
            id="newer-version",
        ),
    ],
)
def test_unreadable_registry_refuses_membership_write(registry, tmp_path, payload, error, key):
    registry.path.write_bytes(payload)
    snapshot = registry.snapshot()
    if key is None:
        assert snapshot.error is not None
    else:
        assert error in str(snapshot.error)
    writes = [lambda: registry.set_session_workspace("session", "ws_saved")]
    if key is not None:
        writes.append(lambda: registry.create("New", str(tmp_path)))
    for write in writes:
        with pytest.raises(WorkspaceRegistryError, match=error) as caught:
            write()
        if key is not None:
            assert caught.value.display_message.definition.key == key
    assert registry.path.read_bytes() == payload


def test_replace_failure_preserves_registry_bytes(registry, tmp_path, monkeypatch):
    from chrys.foundation.platform import files

    selected = registry.create("Selected", str(tmp_path))
    registry.set_session_workspace("session", selected.id)
    before = registry.path.read_bytes()
    attempted = []

    def refuse_replace(source, target):
        if target != registry.path:
            pytest.fail(f"unexpected replacement of {target}")
        attempted.append(json.loads(source.read_bytes()))
        raise PermissionError("registry publish denied")

    monkeypatch.setattr(files, "replace_with_retry", refuse_replace)
    with pytest.raises(WorkspaceRegistryError, match="registry publish denied"):
        registry.set_session_workspace("other-session", selected.id)
    assert registry.path.read_bytes() == before
    assert attempted[0]["sessions"] == {"session": selected.id, "other-session": selected.id}
    assert not list(registry.path.parent.glob("workspaces.json.*.tmp"))


@pytest.mark.parametrize(
    ("name", "folders", "error"),
    [
        ("", [], "Enter a workspace name"),
        ("Two\nlines", [], "control characters"),
        ("Valid", ["relative"], "absolute"),
        ("Valid", ["/bad\x00folder"], "absolute"),
    ],
)
def test_registry_validates_definition(registry, tmp_path, name, folders, error):
    with pytest.raises(WorkspaceRegistryError, match=error):
        registry.create(name, str(tmp_path), folders)


@pytest.mark.parametrize("name", ["uNsPeCiFiEd", "未指定"])
def test_registry_reserves_unspecified_names_on_create_and_rename(registry, tmp_path, name):
    primary, other = make_workdirs(tmp_path, "primary", "other")
    workspace = registry.create("Original", str(primary))
    before = registry.path.read_bytes()
    with pytest.raises(WorkspaceNameError, match="is reserved"):
        registry.create(name, str(other))
    with pytest.raises(WorkspaceNameError, match="is reserved"):
        registry.update(workspace.id, name=name, primary=str(primary))
    assert registry.path.read_bytes() == before


def test_registry_loads_and_edits_an_existing_reserved_name(registry, tmp_path):
    primary, reference = make_workdirs(tmp_path, "primary", "reference")
    workspace = registry.create("Original", str(primary))
    data = json.loads(registry.path.read_bytes())
    data["workspaces"][0]["name"] = "Unspecified"
    registry.path.write_text(json.dumps(data), encoding="utf-8")

    assert registry.snapshot().workspace(workspace.id).name == "Unspecified"
    assert registry.update(workspace.id, name="Unspecified", primary=str(primary), folders=[str(reference)]).name == (
        "Unspecified"
    )
    assert registry.update(workspace.id, name="Renamed", primary=str(primary)).name == "Renamed"


def test_registry_edits_definition_without_moving_primary(registry, tmp_path):
    primary, related = make_workdirs(tmp_path, "primary", "related")
    selected = registry.create("  Selected  ", str(primary), [str(related)])
    assert selected.name == "Selected"
    with pytest.raises(WorkspaceRegistryError, match="already exists"):
        registry.create("selected", str(related))
    with pytest.raises(WorkspaceRegistryError, match="overlap"):
        registry.update(selected.id, name="Selected", primary=str(primary), folders=[str(primary / "nested")])
    primary.rmdir()
    edited = registry.update(selected.id, name="Renamed", primary=str(primary), folders=[])
    assert (edited.name, edited.primary_cwd) == ("Renamed", str(primary))
    before = registry.path.read_bytes()
    with pytest.raises(WorkspaceRegistryError):
        registry.update(selected.id, name="Renamed", primary=str(related))
    assert registry.path.read_bytes() == before


@pytest.mark.parametrize("reference_count", [0, 2], ids=["primary-only", "with-references"])
def test_registry_rejects_identical_folder_sets_in_any_order(registry, tmp_path, reference_count):
    primary, first, second = map(str, make_workdirs(tmp_path, "primary", "first", "second"))
    folders = [first, second][:reference_count]
    original = registry.create("Original", primary, folders)
    distinct = registry.create("Distinct", primary, [first])
    for write in (
        lambda: registry.create("Copy", primary, folders[::-1]),
        lambda: registry.update(distinct.id, name=distinct.name, primary=primary, folders=folders[::-1]),
    ):
        with pytest.raises(WorkspaceRegistryError) as failure:
            write()
        assert format_message(failure.value.display_message) == (
            "Workspace “Original” already has the same primary and reference folders. "
            "Add or remove a reference folder, or use “Original”."
        )
    assert registry.snapshot().workspaces == (original, distinct)
    assert registry.update(original.id, name="Renamed", primary=primary, folders=folders).name == "Renamed"


def test_writing_a_workspace_does_not_read_another_workspaces_folders(registry, tmp_path, monkeypatch):
    primary, shared, local, network = map(str, make_workdirs(tmp_path, "primary", "shared", "local", "network"))
    current = registry.create("Current", primary, [shared])
    registry.create("Extended", primary, [shared, local])
    registry.create("Same count", network, [shared])
    registry.create("Network", network)
    real_identity, real_realpath = registry_module.path_identity, os.path.realpath

    def guard(real):
        def read(path, *args, **kwargs):
            # Stands in for a mount that stopped answering.
            assert str(path) not in (shared, network), f"read {path}"
            return real(path, *args, **kwargs)

        return read

    monkeypatch.setattr(registry_module, "path_identity", guard(real_identity))
    monkeypatch.setattr(registry_module.os.path, "realpath", guard(real_realpath))
    assert registry.update(current.id, name="Current", primary=primary, folders=[]).working_dirs == []
    assert registry.create("Local", local).primary_cwd == local


def test_registry_keeps_existing_duplicate_definitions_available(registry, tmp_path):
    original = registry.create("Original", str(tmp_path))
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    duplicate = registry.create("Duplicate", str(alternate))
    payload = json.loads(registry.path.read_bytes())
    payload["workspaces"][1]["primary_cwd"] = str(tmp_path)
    registry.path.write_text(json.dumps(payload), encoding="utf-8")
    assert [item.name for item in registry.snapshot().workspaces] == ["Original", "Duplicate"]
    for workspace_id in (original.id, duplicate.id):
        registry.set_session_workspace("chat", workspace_id)
        assert registry.snapshot().session_workspace("chat").id == workspace_id


def test_registry_rejects_a_folder_that_aliases_the_primary(registry, tmp_path):
    primary, related = make_workdirs(tmp_path, "primary", "related")
    alias = tmp_path / "alias"
    symlink_or_skip(alias, primary, target_is_directory=True)
    for folder in (alias, alias / "missing"):
        with pytest.raises(WorkspaceRegistryError, match="overlap"):
            registry.create("Team", str(primary), [str(folder)])
    assert registry.create("Team", str(primary), [str(related)]).primary_cwd == str(primary)


@pytest.mark.parametrize("primary_overlaps", [False, True])
def test_registry_reports_earliest_overlapping_folder(registry, tmp_path, primary_overlaps):
    parent = tmp_path / "parent"
    parent.mkdir()
    first, second = make_workdirs(parent, "z-first", "a-second")
    primary = parent / "primary" if primary_overlaps else tmp_path / "primary"
    primary.mkdir()

    with pytest.raises(WorkspaceRegistryError) as failure:
        registry.create("Team", str(primary), [str(first), str(second), str(parent)])

    if primary_overlaps:
        expected = f"Reference folder {parent} overlaps with the primary folder {primary}."
    else:
        expected = f"Reference folders {first} and {parent} overlap."
    assert str(failure.value) == expected


@pytest.mark.skipif(sys.platform not in ("darwin", "win32"), reason="Requires platform folding of missing paths")
def test_registry_rejects_overlapping_missing_folders_that_differ_in_case(registry, tmp_path):
    (primary,) = make_workdirs(tmp_path, "Folder")
    selected = registry.create("Team", str(primary))
    primary.rmdir()
    before = registry.path.read_bytes()

    with pytest.raises(WorkspaceRegistryError, match="overlaps with the primary folder"):
        registry.update(selected.id, name="Team", primary=str(primary), folders=[str(tmp_path / "folder")])

    assert registry.path.read_bytes() == before


def test_registry_overlap_check_resolves_dotdot_as_the_workspace_opens(registry, tmp_path):
    """`alias/../refs` names physical/refs on POSIX but the sibling refs on Windows; both checks agree."""
    physical = tmp_path / "physical"
    physical.mkdir()
    child, inside = make_workdirs(physical, "child", "refs")
    (sibling,) = make_workdirs(tmp_path, "refs")
    alias = tmp_path / "alias"
    symlink_or_skip(alias, child, target_is_directory=True)
    reference = str(alias / ".." / "refs")

    opened = resolve_workspace_directory(reference)
    assert opened == str((sibling if sys.platform == "win32" else inside).resolve())
    if contains(str(physical), opened):
        with pytest.raises(WorkspaceRegistryError, match="overlap"):
            registry.create("Team", str(physical), [reference])
    else:
        assert registry.create("Team", str(physical), [reference]).working_dirs[0].path == reference


def test_overlap_check_matches_the_pairwise_rule(tmp_path):
    """The set-based check reports exactly what comparing every pair reports."""
    tree = tmp_path / "tree"
    for name in ("a/b/c", "a/d", "e/f", "g"):
        (tree / name).mkdir(parents=True)
    symlink_or_skip(tree / "link-a", tree / "a", target_is_directory=True)
    symlink_or_skip(tree / "link-f", tree / "e" / "f", target_is_directory=True)
    paths = [str(tree / name) for name in ("a", "a/b", "a/b/c", "a/d", "e", "e/f", "g", "link-a", "link-a/d")]
    paths += [str(tree / name) for name in ("link-f", "missing", "missing/x", "a/missing", "MISSING")]

    def pairwise(primary, related):
        for index, path in enumerate(related):
            if folders_overlap(path, primary):
                return f"Reference folder {path} overlaps with the primary folder {primary}."
            for other in related[:index]:
                if folders_overlap(path, other):
                    return f"Reference folders {other} and {path} overlap."
        return None

    generator = random.Random(0)
    outcomes = set()
    for _ in range(400):
        primary = generator.choice(paths[:7])
        related = generator.sample(paths, generator.randint(0, 6))
        expected = pairwise(primary, related)
        try:
            validate_workspace_folders(primary, related, None)
        except WorkspaceRegistryError as error:
            actual = str(error)
        else:
            actual = None
        assert actual == expected, (primary, related)
        outcomes.add(expected.split(" ", 2)[1] if expected else None)
    assert outcomes == {None, "folder", "folders"}


def test_registry_reads_a_list_saved_with_a_bom(registry, tmp_path):
    selected = registry.create("Team", str(tmp_path))
    registry.path.write_bytes(b"\xef\xbb\xbf" + registry.path.read_bytes())

    assert registry.snapshot().workspace(selected.id) == selected
    assert registry.update(selected.id, name="Renamed", primary=str(tmp_path), folders=[]).name == "Renamed"
    assert not registry.path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_registry_reads_handwritten_home_relative_folders(registry, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    primary, related = make_workdirs(tmp_path, "proj", "ref")
    selected = registry.create("Team", str(primary), [str(related)])
    payload = json.loads(registry.path.read_bytes())
    payload["workspaces"][0].update(primary_cwd="~/proj", working_dirs=["~/ref"])
    registry.path.write_text(json.dumps(payload), encoding="utf-8")

    assert registry.snapshot().workspace(selected.id) == selected


def test_registry_rejects_a_noncanonical_workspace_id(registry, tmp_path):
    registry.create("Team", str(tmp_path))
    payload = json.loads(registry.path.read_bytes())
    payload["workspaces"][0]["id"] = "team-one"
    registry.path.write_text(json.dumps(payload), encoding="utf-8")

    assert registry.snapshot().error.code == "workspace_registry_unreadable"
    with pytest.raises(WorkspaceRegistryError, match="Cannot read the workspace list"):
        registry.create("New", str(tmp_path))


def test_registry_normalizes_handwritten_paths_before_renaming(registry, tmp_path):
    primary, related = make_workdirs(tmp_path, "primary", "related")
    selected = registry.create("Team", str(primary), [str(related)])
    payload = json.loads(registry.path.read_bytes())
    payload["workspaces"][0].update(
        name="  Team  ", primary_cwd=str(primary) + os.sep, working_dirs=[str(related / ".") + os.sep]
    )
    registry.path.write_text(json.dumps(payload), encoding="utf-8")

    snapshot = registry.snapshot().workspace(selected.id)
    renamed = registry.update(selected.id, name="Renamed", primary=str(primary), folders=[str(related)])
    assert snapshot == selected
    assert renamed.name == "Renamed"
    payload = json.loads(registry.path.read_bytes())
    payload["workspaces"][0]["working_dirs"] = [str(related / "link" / "..")]
    registry.path.write_text(json.dumps(payload), encoding="utf-8")
    # `..` stays as written; the platform's own realpath resolves it when the workspace opens.
    assert registry.snapshot().workspace(selected.id).working_dirs[0].path == str(related / "link" / "..")


async def test_two_process_writers_keep_both_changes(tmp_path):
    """Hold writer one's read inside the write lock while writer two attempts to commit."""
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    selected = registry.create("Selected", str(tmp_path))
    code = """
import sys
from pathlib import Path
from chrys.foundation.util.lock import FileLock
from chrys.service.workspaces.registry import WorkspaceRegistry
class PausedRegistry(WorkspaceRegistry):
    def _mutate(self, change, **kwargs):
        def paused(*state):
            print('read', flush=True)
            sys.stdin.buffer.read(1)
            return change(*state)
        return super()._mutate(paused, **kwargs)
registry = (PausedRegistry if sys.argv[2] == 'First' else WorkspaceRegistry)(Path(sys.argv[1]))
if sys.argv[2] == 'Second':
    acquire = FileLock.acquire
    def signal_before_lock(self):
        print('locking', flush=True)
        return acquire(self)
    FileLock.acquire = signal_before_lock
registry.set_session_workspace(sys.argv[2], sys.argv[3])
"""
    environment = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), APPDATA=str(tmp_path))
    processes: list[asyncio.subprocess.Process] = []

    async def start(name: str, stdin: int) -> asyncio.subprocess.Process:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            str(registry.path),
            name,
            selected.id,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=environment,
            **windows_hidden_subprocess_kwargs(),
        )
        processes.append(process)
        return process

    try:
        first = await start("First", asyncio.subprocess.PIPE)
        assert (await asyncio.wait_for(first.stdout.readline(), 5)).rstrip() == b"read"
        second = await start("Second", asyncio.subprocess.DEVNULL)
        assert (await asyncio.wait_for(second.stdout.readline(), 5)).rstrip() == b"locking"
        first.stdin.write(b"x")
        await first.stdin.drain()
        outcomes = await asyncio.wait_for(asyncio.gather(first.communicate(), second.communicate()), 5)
        assert [process.returncode for process in processes] == [0, 0], outcomes
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()
    snapshot = WorkspaceRegistry(registry.path).snapshot()
    assert snapshot.sessions == {"First": selected.id, "Second": selected.id}
    assert snapshot.workspaces == (selected,)


@pytest.mark.parametrize(
    ("field", "value", "expected_field", "reason", "translated"),
    [
        ("id", "bad-id", "id", "ws_", None),
        ("name", "Badname", "name", "control characters", None),
        ("name", "  fIrSt  ", "name", "already exists", None),
        ("primary_cwd", "relative", "primary_cwd", "absolute", "绝对路径"),
        ("working_dirs", ["relative"], "working_dirs[0]", "absolute", None),
        ("working_dirs", "relative", "working_dirs", "Expected a list.", "应为列表"),
        ("last_used_at", 42, "last_used_at", "string", None),
    ],
)
def test_unreadable_registry_warning_points_to_the_invalid_workspace_row(
    registry, tmp_path, field, value, expected_field, reason, translated
):
    from chrys.foundation.i18n import Localizer
    from chrys.orchestration.engine.workspaces import registry_unreadable_warning

    first, second = make_workdirs(tmp_path, "first", "second")
    registry.create("First", str(first))
    registry.create("Second", str(second))
    payload = json.loads(registry.path.read_bytes())
    payload["workspaces"][1][field] = value
    registry.path.write_text(json.dumps(payload), encoding="utf-8")

    error = registry.snapshot().error
    assert error is not None
    warning = registry_unreadable_warning("saved", error)
    assert "workspaces.json" in warning.message
    assert f"Entry 2, field {expected_field}:" in warning.message
    assert reason in warning.message
    if translated is not None:
        assert warning.display_message is not None
        rendered = Localizer("zh-Hans").render(warning.display_message)
        assert expected_field in rendered and translated in rendered


async def test_recent_use_yields_to_another_writer_and_records_when_free(registry, tmp_path, monkeypatch):
    from threading import Event

    from chrys.foundation.util.lock import FileLock
    from chrys.service.workspaces import registry as registry_module
    from tests.support.async_tasks import running_task
    from tests.support.waiting import wait_for

    monkeypatch.setattr(registry_module, "_LOCK_TIMEOUT", 30)
    workspace = registry.create("Team", str(tmp_path))
    before = registry.snapshot().last_used_at
    entered, release = Event(), Event()

    def hold_lock():
        with FileLock(registry._lock_path, timeout=0):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("writer was not released")

    async with running_task(asyncio.to_thread(hold_lock)) as holder:
        try:
            await wait_for(lambda: entered.is_set() or holder.done())
            if holder.done():
                await holder
            assert entered.is_set()
            touch = asyncio.create_task(asyncio.to_thread(registry.touch, workspace.id))
            try:
                await wait_for(touch.done)
                await touch
                assert not release.is_set() and not holder.done()
                assert registry.snapshot().last_used_at == before
            finally:
                release.set()
                await touch
        finally:
            release.set()
        await holder
    await asyncio.to_thread(registry.touch, workspace.id)
    assert registry.snapshot().last_used_at[workspace.id] > before[workspace.id]


def test_edits_probe_their_own_folders_before_taking_the_registry_lock(registry, tmp_path, monkeypatch):
    primary, slow, other = make_workdirs(tmp_path, "primary", "slow-mount", "other")
    team = registry.create("Team", str(primary), [str(slow)])
    entered, release = threading.Event(), threading.Event()
    identity = registry_module.path_identity

    def held_identity(path):
        if str(path) == str(slow):
            entered.set()
            release.wait(10)
        return identity(path)

    monkeypatch.setattr(registry_module, "path_identity", held_identity)
    rename = threading.Thread(
        target=lambda: registry.update(team.id, name="Renamed", primary=str(primary), folders=[str(slow)])
    )
    rename.start()
    try:
        assert entered.wait(10)
        # While the rename waits on its own reference folder, another writer still gets the registry.
        created = threading.Thread(target=lambda: registry.create("Other", str(other)))
        created.start()
        created.join(2)
        assert not created.is_alive()
    finally:
        release.set()
        rename.join(10)
    names = {workspace.name for workspace in registry.snapshot().workspaces}
    assert names == {"Renamed", "Other"}
