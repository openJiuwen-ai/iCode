# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ownership of archived sessions reachable through multiple filesystem paths."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import Error, SessionRestore
from chrys.foundation.util.lock import FileLock
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.service.state.locks import ActiveSessionGuard
from chrys.service.state.store import JsonFileStateStore, StartupSessionInUseError
from tests.support.symlinks import junction_or_skip, symlink_or_skip

IDENTITY = "12345678-abcd-4111-8222-123456789abc"


@pytest.mark.parametrize("owner_is_alias", [False, True])
@pytest.mark.parametrize("junction", [False, True])
async def test_linked_session_shares_ownership_and_excludes_restore(
    tmp_path: Path, owner_is_alias: bool, junction: bool
) -> None:
    source = JsonFileStateStore(tmp_path / "source")
    alias = JsonFileStateStore(tmp_path / "alias")
    if owner_is_alias:
        source.save_recovery_session(IDENTITY, {})
    else:
        await source.save_session(IDENTITY, {})
    original = source.session_dir(IDENTITY)
    if original.with_name(original.name.upper()).exists():
        original = original.with_name(original.name.upper())
    if junction:
        junction_or_skip(alias.session_dir(IDENTITY), original)
    else:
        symlink_or_skip(alias.session_dir(IDENTITY), original, target_is_directory=True)
    owner, other = (alias, source) if owner_is_alias else (source, alias)
    guard = ActiveSessionGuard(owner)
    bus = EventBus()
    errors: list[Error] = []

    async def collect_error(event: Error) -> None:
        errors.append(event)

    await bus.subscribe(Error, collect_error)
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=other)
    try:
        assert guard.ensure(IDENTITY)
        assert json.loads(other.active_owner_path(IDENTITY).read_bytes())["session_id"] == IDENTITY
        with (
            FileLock(owner._write_lock_path(IDENTITY), timeout=0),
            pytest.raises(TimeoutError),
            FileLock(other._write_lock_path(IDENTITY), timeout=0),
        ):
            pass
        with pytest.raises(StartupSessionInUseError):
            await other.resolve_startup_session(IDENTITY.upper(), require_inactive=True)
        await engine.on_session_restore(SessionRestore(session_id=IDENTITY))
        assert [(error.code, str(os.getpid()) in error.message) for error in errors] == [("session_in_use", True)]
        assert engine.session.session_id is None
    finally:
        guard.release()
        await engine.shutdown()
    assert not other.active_owner_path(IDENTITY).exists()
    assert (await other.resolve_startup_session(IDENTITY, require_inactive=True)).session_id == IDENTITY
    await alias.delete_session(IDENTITY)
    assert not alias.session_dir(IDENTITY).exists()
    checkpoint = "session.recovery.json" if owner_is_alias else "session.json"
    assert (source.session_dir(IDENTITY) / checkpoint).is_file()


@pytest.mark.parametrize("junction", [False, True])
async def test_linked_archive_respects_the_physical_roots_pending_delete(tmp_path: Path, junction: bool) -> None:
    from chrys.service.trajectory.tombstone import INTENT_SUFFIX, tombstones_dir

    source, alias = (JsonFileStateStore(tmp_path / name) for name in ("source", "alias"))
    await source.save_session(IDENTITY, {})
    archive = source.session_dir(IDENTITY)
    if junction:
        junction_or_skip(alias.session_dir(IDENTITY), archive)
    else:
        symlink_or_skip(alias.session_dir(IDENTITY), archive, target_is_directory=True)

    guard = ActiveSessionGuard(alias)
    graveyard = tombstones_dir(source.sessions_root)
    graveyard.mkdir()
    name = archive.name.upper() if archive.with_name(archive.name.upper()).exists() else archive.name
    intent = graveyard / f"{name}{INTENT_SUFFIX}"
    intent.write_text(name, encoding="utf-8")

    assert await source.resolve_startup_session(IDENTITY) is None
    assert await alias.resolve_startup_session(IDENTITY) is None
    assert await alias.resolve_startup_session(IDENTITY, require_inactive=True) is None
    try:
        assert guard.ensure(IDENTITY)
        assert await alias.load_chat_for_restore(IDENTITY, prefer_recovery=True) is None
    finally:
        guard.release()
    intent.unlink()
    assert (await alias.resolve_startup_session(IDENTITY, require_inactive=True)).session_id == IDENTITY


async def test_linked_pending_delete_hides_cached_rows_and_latest_session(tmp_path: Path) -> None:
    from chrys.foundation.models.session_surface import SessionSurface
    from chrys.service.trajectory.tombstone import INTENT_SUFFIX, tombstones_dir

    source, alias = (JsonFileStateStore(tmp_path / name) for name in ("source", "alias"))
    await source.save_session(IDENTITY, {})
    archive = source.session_dir(IDENTITY)
    # The picker can already hold a flat row when the folder-format copy is added.
    (alias.sessions_root / f"{archive.name}.json").write_bytes((archive / "session.json").read_bytes())
    flat_listing = await alias.open_session_listing(kind="chat")
    assert flat_listing.entries[0].legacy
    symlink_or_skip(alias.session_dir(IDENTITY), archive, target_is_directory=True)
    folder_listing = await alias.open_session_listing(kind="chat")
    assert not folder_listing.entries[0].legacy
    assert await alias.load_latest_session_id() == IDENTITY

    graveyard = tombstones_dir(source.sessions_root)
    graveyard.mkdir()
    (graveyard / f"{archive.name}{INTENT_SUFFIX}").write_text(archive.name, encoding="utf-8")

    for snapshot in (flat_listing, folder_listing):
        assert (await alias.load_session_page(snapshot, surfaces={SessionSurface.TUI})).metas == ()
    assert await alias.list_sessions() == []
    assert (await alias.open_session_listing(kind="chat")).entries == ()
    assert await alias.load_latest_session_id() is None


async def test_release_after_directory_link_retarget_leaves_new_owner_alone(tmp_path: Path) -> None:
    first, second, alias = (JsonFileStateStore(tmp_path / name) for name in ("first", "second", "alias"))
    for store in (first, second):
        await store.save_session(IDENTITY, {})
    link = alias.session_dir(IDENTITY)
    symlink_or_skip(link, first.session_dir(IDENTITY), target_is_directory=True)
    previous = ActiveSessionGuard(alias)
    current = ActiveSessionGuard(second)
    try:
        assert previous.ensure(IDENTITY)
        assert current.ensure(IDENTITY)
        link.unlink()
        symlink_or_skip(link, second.session_dir(IDENTITY), target_is_directory=True)
        assert not previous.owns(IDENTITY)
        previous.release()
        assert not first.active_owner_path(IDENTITY).exists()
        assert second.active_owner_path(IDENTITY).exists()
    finally:
        current.release()
        previous.release()


@pytest.mark.parametrize("committed", [False, True])
async def test_individually_linked_live_recovery_is_never_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    source = JsonFileStateStore(tmp_path / "source")
    alias = JsonFileStateStore(tmp_path / "alias")
    source.save_recovery_session(IDENTITY, {}, primary_cwd="recovered")
    if committed:
        await alias.save_session(IDENTITY, {}, primary_cwd="committed")
    link = alias.session_dir(IDENTITY) / "session.recovery.json"
    link.parent.mkdir(exist_ok=True)
    original = source.session_dir(IDENTITY) / link.name
    symlink_or_skip(link, original)
    guard = ActiveSessionGuard(source)
    open_file = Path.open

    def open_committed(path: Path, *args, **kwargs):
        assert path not in (link, original)
        return open_file(path, *args, **kwargs)

    try:
        assert guard.ensure(IDENTITY)
        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", open_committed)
            if committed:
                meta = await alias.resolve_startup_session(IDENTITY, require_inactive=True)
                assert meta is not None and meta.primary_cwd == "committed"
            else:
                with pytest.raises(StartupSessionInUseError):
                    await alias.resolve_startup_session(IDENTITY, require_inactive=True)
            with FileLock(alias.active_lock_path(IDENTITY), timeout=0):
                if committed:
                    restored = await alias.load_chat_for_restore(IDENTITY, prefer_recovery=True)
                    assert restored is not None and not restored.recovered_from_sidecar
                else:
                    with pytest.raises(StartupSessionInUseError):
                        await alias.load_chat_for_restore(IDENTITY, prefer_recovery=True)
        assert link.is_symlink() and original.is_file()
    finally:
        guard.release()


async def test_restore_preserves_linked_recovery_when_its_owner_cannot_be_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, alias = (JsonFileStateStore(tmp_path / name) for name in ("source", "alias"))
    await alias.save_session(IDENTITY, {}, primary_cwd="committed")
    source.save_recovery_session(IDENTITY, {}, primary_cwd="recovered")
    link = alias.session_dir(IDENTITY) / "session.recovery.json"
    original = source.session_dir(IDENTITY) / link.name
    symlink_or_skip(link, original)
    foreign = source.active_lock_path(IDENTITY)
    with FileLock(foreign, timeout=0):
        pass
    open_lock = FileLock._open_lock_file

    def denied(lock: FileLock):
        if lock.path == foreign:
            raise PermissionError("temporary owner lock read failure")
        return open_lock(lock)

    with FileLock(alias.active_lock_path(IDENTITY), timeout=0):
        with monkeypatch.context() as patch:
            patch.setattr(FileLock, "_open_lock_file", denied)
            with pytest.raises(PermissionError, match="owner lock read failure"):
                await alias.load_chat_for_restore(IDENTITY, prefer_recovery=True)
        assert link.is_symlink() and link.samefile(original)
        restored = await alias.load_chat_for_restore(IDENTITY, prefer_recovery=True)
        assert restored is not None and restored.recovered_from_sidecar


async def test_startup_flat_comparison_does_not_read_busy_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session(IDENTITY, {}, primary_cwd="backup")
    directory = store.session_dir(IDENTITY)
    store.save_recovery_session(IDENTITY, {}, primary_cwd="live")
    primary = directory / "session.json"
    flat = primary.rename(tmp_path / f"{directory.name}.json")
    recovery = directory / "session.recovery.json"
    assert flat.is_file() and (directory / "session.json.bak").is_file() and recovery.is_file()
    assert not primary.exists()
    open_file = Path.open

    def open_committed(path: Path, *args, **kwargs):
        assert path != recovery
        return open_file(path, *args, **kwargs)

    with FileLock(store.active_lock_path(IDENTITY), timeout=0):
        monkeypatch.setattr(Path, "open", open_committed)
        meta = await store.resolve_startup_session(IDENTITY)
        assert meta is not None and meta.primary_cwd == "backup"


async def test_unavailable_shared_lock_leaves_archive_intact_and_can_retry(tmp_path: Path) -> None:
    seed = JsonFileStateStore(tmp_path / "seed")
    await seed.save_session(IDENTITY, {}, primary_cwd="saved-workspace")
    source = JsonFileStateStore(tmp_path / "physical")
    archive = source.session_dir(IDENTITY)
    seed.session_dir(IDENTITY).rename(archive)
    alias = JsonFileStateStore(tmp_path / "alias")
    symlink_or_skip(alias.session_dir(IDENTITY), archive, target_is_directory=True)

    checkpoint = archive / "session.json"
    original = checkpoint.read_bytes()
    entries = set(archive.iterdir())
    blocker = source.sessions_root / ".locks"
    blocker.touch()
    guard = ActiveSessionGuard(alias)
    try:
        with pytest.raises(OSError):
            guard.ensure(IDENTITY)
        assert checkpoint.read_bytes() == original
        assert set(archive.iterdir()) == entries

        blocker.unlink()
        assert guard.ensure(IDENTITY)
        with pytest.raises(StartupSessionInUseError):
            await source.resolve_startup_session(IDENTITY, require_inactive=True)
    finally:
        guard.release()
    restored = await alias.resolve_startup_session(IDENTITY, require_inactive=True)
    assert restored is not None and restored.primary_cwd == "saved-workspace"
