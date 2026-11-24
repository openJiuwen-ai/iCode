# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for crash-safe session writes — backup healing, the recovery sidecar, and rollback-snapshot fallback."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import chrys.service.state.store as store_module
from chrys.foundation.util.lock import FileLock
from chrys.kernel import Message
from chrys.service.session.message_metadata import MESSAGE_CREATED_AT_KEY, stamp_message_created_at
from chrys.service.state.store import (
    SESSION_RECOVERY_FILE_NAME,
    JsonFileStateStore,
)
from tests.service.state._store_helpers import SkewedDateTime

# --- crash-safe session.json writes --------------------------------------


async def test_save_preserves_existing_session_when_replace_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash during the primary replace keeps the prior session readable."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [Message("user", ["old"])], "compressed_msgs": []})

    session_file = tmp_path / "s1" / "session.json"
    before = session_file.read_text(encoding="utf-8")
    real_replace = store_module.os.replace

    def fail_primary_replace(src: object, dst: object) -> None:
        if Path(dst).name == "session.json":
            raise RuntimeError("simulated crash during primary replace")
        real_replace(src, dst)

    monkeypatch.setattr(store_module.os, "replace", fail_primary_replace)

    with pytest.raises(RuntimeError, match="simulated crash"):
        await store.save_session("s1", {"messages": [Message("user", ["new"])], "compressed_msgs": []})

    assert session_file.read_text(encoding="utf-8") == before
    loaded = await store.load_session("s1")
    assert loaded is not None
    assert loaded["messages"][0].text == "old"
    assert not list((tmp_path / "s1").glob("session.json.*.tmp"))


async def test_load_recovers_corrupt_primary_from_backup(tmp_path: Path) -> None:
    """If primary JSON is corrupt, the backup is used and the primary is healed."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [Message("user", ["latest"])], "compressed_msgs": []})

    session_file = tmp_path / "s1" / "session.json"
    session_file.write_text("{ not valid json", encoding="utf-8")

    loaded = await store.load_session("s1")

    assert loaded is not None
    assert loaded["messages"][0].text == "latest"
    healed = json.loads(session_file.read_text(encoding="utf-8"))
    assert healed["meta"]["session_id"] == "s1"


async def test_load_recovers_missing_primary_from_backup(tmp_path: Path) -> None:
    """If session.json disappears, the backup is enough to restore and heal it."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [Message("user", ["backup"])], "compressed_msgs": []})

    session_file = tmp_path / "s1" / "session.json"
    session_file.unlink()

    loaded = await store.load_session("s1")

    assert loaded is not None
    assert loaded["messages"][0].text == "backup"
    assert json.loads(session_file.read_text(encoding="utf-8"))["meta"]["session_id"] == "s1"


async def test_chat_restore_reads_healthy_checkpoint_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [Message("user", ["history"])]}, title="saved title")
    read = store_module.read_session_envelope
    paths: list[Path] = []

    def track_read(path: Path, *, probe_legacy_source: bool = False):
        paths.append(path)
        return read(path, probe_legacy_source=probe_legacy_source)

    monkeypatch.setattr(store_module, "read_session_envelope", track_read)
    with FileLock(store.active_lock_path("s1"), timeout=0):
        restored = await store.load_chat_for_restore("s1", prefer_recovery=True)

    assert restored is not None and restored.meta is not None
    assert restored.state["messages"][0].text == "history"
    assert restored.meta.title == "saved title"
    assert not restored.recovered_from_sidecar
    assert paths == [store.session_dir("s1") / "session.json"]


@pytest.mark.parametrize(
    ("blocked_file", "deny_stat"),
    [("session.json", False), (SESSION_RECOVERY_FILE_NAME, False), (SESSION_RECOVERY_FILE_NAME, True)],
)
async def test_restore_preserves_unreadable_checkpoints_until_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocked_file: str, deny_stat: bool
) -> None:
    store = JsonFileStateStore(tmp_path)
    session_id = "12345678-abcd-4111-8222-123456789abc"
    await store.save_session(session_id, {"messages": [Message("user", ["committed"])]})
    if blocked_file == SESSION_RECOVERY_FILE_NAME:
        await asyncio.to_thread(store.save_recovery_session, session_id, {"messages": [Message("user", ["latest"])]})
    path = store.session_dir(session_id) / blocked_file
    before = path.read_bytes()
    open_file = Path.open
    stat_file = Path.stat

    def denied(target, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
        if target == path:
            raise PermissionError("temporary checkpoint read failure")
        return open_file(target, mode, buffering, encoding, errors, newline)

    def denied_stat(target, *, follow_symlinks=True):
        if target == path and follow_symlinks:
            raise PermissionError("temporary checkpoint read failure")
        return stat_file(target, follow_symlinks=follow_symlinks)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", denied)
        if deny_stat:
            patch.setattr(Path, "stat", denied_stat)
        with pytest.raises(PermissionError, match="checkpoint read failure"):
            await store.resolve_startup_session(session_id, require_inactive=True)
        with FileLock(store.active_lock_path(session_id), timeout=0):
            with pytest.raises(PermissionError, match="checkpoint read failure"):
                await store.load_chat_for_restore(session_id, prefer_recovery=True)
            with pytest.raises(PermissionError, match="checkpoint read failure"):
                await store.load_chat_for_restore(session_id, prefer_recovery=False, ignore_recovery=True)

    assert path.read_bytes() == before
    with FileLock(store.active_lock_path(session_id), timeout=0):
        restored = await store.load_chat_for_restore(session_id, prefer_recovery=True)
    assert restored is not None
    expected = "latest" if blocked_file == SESSION_RECOVERY_FILE_NAME else "committed"
    assert restored.state["messages"][0].text == expected


@pytest.mark.parametrize("invalid_history", [False, True])
async def test_rollback_discards_a_sidecar_with_an_equivalent_uuid(tmp_path: Path, invalid_history: bool) -> None:
    store = JsonFileStateStore(tmp_path)
    session_id = "12345678-abcd-4111-8222-123456789abc"
    await store.save_session(session_id, {"messages": [Message("user", ["rolled back"])]})
    await asyncio.to_thread(store.save_recovery_session, session_id, {"messages": [Message("user", ["old"])]})
    directory = store.session_dir(session_id)
    recovery = directory / SESSION_RECOVERY_FILE_NAME
    sidecar = json.loads(recovery.read_text(encoding="utf-8"))
    sidecar["meta"].update(session_id=session_id.replace("-", "").upper(), updated_at="2999-01-01T00:00:00Z")
    recovery.write_text(json.dumps(sidecar), encoding="utf-8")
    if invalid_history:
        primary = directory / "session.json"
        checkpoint = json.loads(primary.read_text(encoding="utf-8"))
        checkpoint["state"]["compressed_msgs"] = [{}]
        primary.write_text(json.dumps(checkpoint), encoding="utf-8")

    with FileLock(store.active_lock_path(session_id), timeout=0):
        restored = await store.load_chat_for_restore(session_id, prefer_recovery=False, ignore_recovery=True)

    assert not recovery.exists()
    if invalid_history:
        assert restored is None
    else:
        assert restored is not None and restored.state["messages"][0].text == "rolled back"


@pytest.mark.parametrize("initially_damaged", [True, False])
async def test_read_does_not_discard_a_replacement_sidecar(
    tmp_path: Path, monkeypatch, initially_damaged: bool
) -> None:
    store = JsonFileStateStore(tmp_path)
    session_id = "12345678-abcd-4111-8222-123456789abc"
    await store.save_session(session_id, {"messages": []})
    directory = store.session_dir(session_id)
    recovery = directory / SESSION_RECOVERY_FILE_NAME
    sidecar = json.loads((directory / "session.json").read_text(encoding="utf-8"))
    sidecar["meta"]["updated_at"] = "2020-01-01T00:00:00Z"
    recovery.write_text("{" if initially_damaged else json.dumps(sidecar), encoding="utf-8")
    sidecar["meta"]["session_id"] = session_id[:-1] + "0"
    replacement = json.dumps(sidecar)
    clean_up = store._delete_recovery_session_if_not_newer

    def replace_before_cleanup(target: str, cleanup) -> None:
        with FileLock(store._write_lock_path(target), timeout=0):
            recovery.write_text(replacement, encoding="utf-8")
        clean_up(target, cleanup)

    monkeypatch.setattr(store, "_delete_recovery_session_if_not_newer", replace_before_cleanup)
    assert (await store.load_session_meta(session_id, prefer_recovery=True)).session_id == session_id
    assert recovery.read_text(encoding="utf-8") == replacement


async def test_save_succeeds_when_backup_update_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The backup is best-effort; a .bak write failure must not fail the primary save."""
    store = JsonFileStateStore(tmp_path)
    real_atomic_write_text = store_module._atomic_write_text

    def fail_backup_write(path: Path, payload: str, *, encoding: str = "utf-8") -> bytes:
        if Path(path).name == "session.json.bak":
            raise OSError("simulated backup failure")
        return real_atomic_write_text(path, payload, encoding=encoding)

    monkeypatch.setattr(store_module, "_atomic_write_text", fail_backup_write)

    await store.save_session("s1", {"messages": [Message("user", ["primary"])], "compressed_msgs": []})

    loaded = await store.load_session("s1")
    assert loaded is not None
    assert loaded["messages"][0].text == "primary"
    assert (tmp_path / "s1" / "session.json").exists()


async def test_recovery_sidecar_wins_when_explicitly_allowed_without_healing_primary(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "s1",
        {"messages": [Message("user", ["primary"])], "compressed_msgs": []},
        service_session_id="provider-session",
    )
    await asyncio.to_thread(
        store.save_recovery_session,
        "s1",
        {"messages": [Message("user", ["recovered"])], "compressed_msgs": []},
    )

    recovery_file = tmp_path / "s1" / SESSION_RECOVERY_FILE_NAME
    recovery = json.loads(recovery_file.read_text(encoding="utf-8"))
    recovery["meta"]["updated_at"] = "2999-01-01T00:00:00+00:00"
    recovery_file.write_text(json.dumps(recovery), encoding="utf-8")

    primary = await store.load_session("s1")
    loaded = await store.load_session("s1", prefer_recovery=True)
    raw = await store.load_session_raw("s1", prefer_recovery=True)
    meta = await store.load_session_meta("s1", prefer_recovery=True)

    assert primary is not None
    assert primary["messages"][0].text == "primary"
    assert loaded is not None
    assert raw is not None
    assert meta is not None
    assert await store.recovery_session_wins("s1") is True
    assert loaded["messages"][0].text == "recovered"
    assert raw[0]["contents"][0]["text"] == "recovered"
    assert meta.service_session_id == ""
    primary = json.loads((tmp_path / "s1" / "session.json").read_text(encoding="utf-8"))
    assert primary["state"]["messages"][0]["contents"][0]["text"] == "primary"


async def test_save_recovery_session_neutralizes_surrogate_workspace_metadata(tmp_path: Path) -> None:
    """A surrogateescaped workspace path (undecodable byte in cwd/args) must not
    crash the recovery sidecar write. The recovery path funnels through the total
    text sink (atomic_write_text), not a strict bytes encode, so the file stays
    strict-UTF-8 and the crash-recovery checkpoint is not silently lost."""
    store = JsonFileStateStore(tmp_path)
    await asyncio.to_thread(
        store.save_recovery_session,
        "s1",
        {"messages": [Message("user", ["recovered"])], "compressed_msgs": []},
        primary_cwd="/work/pro\udcffject",
        working_dirs=["/root/\udcfe"],
        title="draft \udcfd",
    )
    recovery_file = tmp_path / "s1" / SESSION_RECOVERY_FILE_NAME
    # Discriminator: a strict read of the on-disk bytes must succeed (no lone
    # surrogate in the file); the pre-fix strict bytes-encode raised here.
    recovery = json.loads(recovery_file.read_text(encoding="utf-8"))
    assert recovery["state"]["messages"][0]["contents"][0]["text"] == "recovered"

    meta = await store.load_session_meta("s1", prefer_recovery=True)

    assert meta is not None
    assert meta.primary_cwd == "/work/pro\udcffject"
    assert meta.working_dirs == ["/root/\udcfe"]


async def test_load_session_meta_treats_null_workspace_paths_as_empty(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [], "compressed_msgs": []})
    session_file = tmp_path / "s1" / "session.json"
    envelope = json.loads(session_file.read_text(encoding="utf-8"))
    envelope["meta"]["primary_cwd"] = None
    envelope["meta"]["working_dirs"] = None
    session_file.write_text(json.dumps(envelope), encoding="utf-8")

    meta = await store.load_session_meta("s1")

    assert meta is not None
    assert meta.primary_cwd == ""
    assert meta.working_dirs == []


async def test_recovery_title_mirror_neutralizes_surrogate_title(tmp_path: Path) -> None:
    """The title-mirror into a live recovery sidecar shares the same sink; a
    surrogate-bearing title must not crash (or silently skip) the mirror write."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [Message("user", ["primary"])], "compressed_msgs": []})
    await asyncio.to_thread(
        store.save_recovery_session,
        "s1",
        {"messages": [Message("user", ["recovered"])], "compressed_msgs": []},
    )
    await store.update_session_titles("s1", custom_title="my \udcff title")
    recovery_file = tmp_path / "s1" / SESSION_RECOVERY_FILE_NAME
    # Discriminator: file strict-UTF-8 readable AND the patch actually landed. The
    # pre-fix strict encode raised inside the (OSError-only) mirror guard, so the
    # mirror was skipped and custom_title stayed unset.
    recovery = json.loads(recovery_file.read_text(encoding="utf-8"))
    assert (recovery["meta"].get("custom_title") or "").startswith("my ")


async def test_stale_recovery_sidecar_is_ignored_and_deleted(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [Message("user", ["primary"])], "compressed_msgs": []})
    await asyncio.to_thread(
        store.save_recovery_session,
        "s1",
        {"messages": [Message("user", ["stale"])], "compressed_msgs": []},
    )

    recovery_file = tmp_path / "s1" / SESSION_RECOVERY_FILE_NAME
    recovery = json.loads(recovery_file.read_text(encoding="utf-8"))
    recovery["meta"]["updated_at"] = "2000-01-01T00:00:00+00:00"
    recovery_file.write_text(json.dumps(recovery), encoding="utf-8")

    loaded = await store.load_session("s1", prefer_recovery=True)

    assert loaded is not None
    assert loaded["messages"][0].text == "primary"
    assert not recovery_file.exists()


async def test_recovery_sidecar_with_invalid_timestamp_is_ignored_when_primary_exists(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [Message("user", ["primary"])], "compressed_msgs": []})
    await asyncio.to_thread(
        store.save_recovery_session,
        "s1",
        {"messages": [Message("user", ["invalid recovery"])], "compressed_msgs": []},
    )

    recovery_file = tmp_path / "s1" / SESSION_RECOVERY_FILE_NAME
    recovery = json.loads(recovery_file.read_text(encoding="utf-8"))
    recovery["meta"]["updated_at"] = "not-a-timestamp"
    recovery_file.write_text(json.dumps(recovery), encoding="utf-8")

    loaded = await store.load_session("s1", prefer_recovery=True)

    assert loaded is not None
    assert loaded["messages"][0].text == "primary"
    assert not recovery_file.exists()


async def test_primary_save_deletes_recovery_sidecar_after_success(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await asyncio.to_thread(
        store.save_recovery_session,
        "s1",
        {"messages": [Message("user", ["recover"])], "compressed_msgs": []},
    )

    await store.save_session("s1", {"messages": [Message("user", ["primary"])], "compressed_msgs": []})

    assert not (tmp_path / "s1" / SESSION_RECOVERY_FILE_NAME).exists()


async def test_primary_save_retiring_sidecar_advances_updated_at_before_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [Message("user", ["old primary"])], "compressed_msgs": []})
    primary_file = tmp_path / "s1" / "session.json"
    primary = json.loads(primary_file.read_text(encoding="utf-8"))
    primary["meta"]["updated_at"] = "2026-01-01T00:00:00+00:00"
    primary_file.write_text(json.dumps(primary), encoding="utf-8")
    await asyncio.to_thread(
        store.save_recovery_session,
        "s1",
        {"messages": [Message("user", ["recovery"])], "compressed_msgs": []},
    )
    recovery_file = tmp_path / "s1" / SESSION_RECOVERY_FILE_NAME
    recovery = json.loads(recovery_file.read_text(encoding="utf-8"))

    # Simulate a crash after the primary write but before structural sidecar deletion.
    monkeypatch.setattr(store, "_delete_recovery_session_unlocked", lambda _session_id: None)
    await store.save_session("s1", {"messages": [Message("user", ["clean primary"])], "compressed_msgs": []})

    primary_after = json.loads(primary_file.read_text(encoding="utf-8"))
    assert datetime.fromisoformat(primary_after["meta"]["updated_at"]) >= datetime.fromisoformat(
        recovery["meta"]["updated_at"]
    )
    loaded = await store.load_session("s1", prefer_recovery=True)
    assert loaded is not None
    assert loaded["messages"][0].text == "clean primary"


async def test_primary_save_failure_leaves_recovery_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonFileStateStore(tmp_path)
    await asyncio.to_thread(
        store.save_recovery_session,
        "s1",
        {"messages": [Message("user", ["recover"])], "compressed_msgs": []},
    )
    real_atomic_write_text = store_module._atomic_write_text

    def fail_primary_write(path: Path, payload: str, *, encoding: str = "utf-8") -> None:
        if Path(path).name == "session.json":
            raise OSError("simulated primary failure")
        real_atomic_write_text(path, payload, encoding=encoding)

    monkeypatch.setattr(store_module, "_atomic_write_text", fail_primary_write)

    with pytest.raises(OSError, match="simulated primary failure"):
        await store.save_session("s1", {"messages": [Message("user", ["primary"])], "compressed_msgs": []})

    assert (tmp_path / "s1" / SESSION_RECOVERY_FILE_NAME).exists()


async def test_list_sessions_includes_recovery_only_sidecar(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await asyncio.to_thread(
        store.save_recovery_session,
        "s1",
        {"messages": [Message("user", ["recover"])], "compressed_msgs": []},
        agent_profile="code",
    )

    sessions = await store.list_sessions()

    assert len(sessions) == 1
    assert sessions[0].session_id == "s1"
    assert sessions[0].agent_profile == "code"


async def test_list_sessions_ignores_recovery_sidecar_when_active_lock_is_held(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "s1",
        {"messages": [Message("user", ["primary"])], "compressed_msgs": []},
        agent_profile="primary-agent",
    )
    await asyncio.to_thread(
        store.save_recovery_session,
        "s1",
        {"messages": [Message("user", ["recover"])], "compressed_msgs": []},
        agent_profile="recovery-agent",
    )
    recovery_file = tmp_path / "s1" / SESSION_RECOVERY_FILE_NAME
    recovery = json.loads(recovery_file.read_text(encoding="utf-8"))
    recovery["meta"]["updated_at"] = "2999-01-01T00:00:00+00:00"
    recovery_file.write_text(json.dumps(recovery), encoding="utf-8")

    active_lock = FileLock(store.active_lock_path("s1"), timeout=1.0)
    active_lock.acquire()
    try:
        sessions = await store.list_sessions()
    finally:
        active_lock.release()

    assert len(sessions) == 1
    assert sessions[0].agent_profile == "primary-agent"
    assert recovery_file.exists()


async def test_direct_session_readers_do_not_prefer_recovery_by_default_under_active_lock(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "s1",
        {"messages": [Message("user", ["primary"])], "compressed_msgs": []},
        agent_profile="primary-agent",
    )
    await asyncio.to_thread(
        store.save_recovery_session,
        "s1",
        {"messages": [Message("user", ["recover"])], "compressed_msgs": []},
        agent_profile="recovery-agent",
    )
    recovery_file = tmp_path / "s1" / SESSION_RECOVERY_FILE_NAME
    recovery = json.loads(recovery_file.read_text(encoding="utf-8"))
    recovery["meta"]["updated_at"] = "2999-01-01T00:00:00+00:00"
    recovery_file.write_text(json.dumps(recovery), encoding="utf-8")

    active_lock = FileLock(store.active_lock_path("s1"), timeout=1.0)
    active_lock.acquire()
    try:
        loaded = await store.load_session("s1")
        raw = await store.load_session_raw("s1")
        meta = await store.load_session_meta("s1")
    finally:
        active_lock.release()

    assert loaded is not None
    assert raw is not None
    assert meta is not None
    assert loaded["messages"][0].text == "primary"
    assert raw[0]["contents"][0]["text"] == "primary"
    assert meta.agent_profile == "primary-agent"
    assert recovery_file.exists()


async def test_list_sessions_skips_recovery_only_sidecar_when_active_lock_is_held(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await asyncio.to_thread(
        store.save_recovery_session,
        "s1",
        {"messages": [Message("user", ["recover"])], "compressed_msgs": []},
        agent_profile="code",
    )
    active_lock = FileLock(store.active_lock_path("s1"), timeout=1.0)
    active_lock.acquire()
    try:
        sessions = await store.list_sessions()
    finally:
        active_lock.release()

    assert sessions == []


# --- recovery timestamp arbitration ---


async def test_recovery_only_created_at_is_stable_and_repairs_drift_from_history(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    old_message = Message("user", ["old message"])
    stamp_message_created_at(old_message, "2020-01-01T00:00:00+00:00")
    state = {"messages": [old_message], "compressed_msgs": []}
    recovery_file = store.session_dir("s1") / SESSION_RECOVERY_FILE_NAME

    await asyncio.to_thread(store.save_recovery_session, "s1", state)
    first = json.loads(recovery_file.read_text(encoding="utf-8"))
    assert datetime.fromisoformat(first["meta"]["created_at"]) == datetime(2020, 1, 1, tzinfo=UTC)
    assert first["meta"]["created_at"] != first["meta"]["updated_at"]

    drifted = datetime.now(UTC).isoformat()
    first["meta"]["created_at"] = drifted
    first["meta"]["updated_at"] = drifted
    recovery_file.write_text(json.dumps(first), encoding="utf-8")

    await asyncio.to_thread(store.save_recovery_session, "s1", state)
    second = json.loads(recovery_file.read_text(encoding="utf-8"))
    assert datetime.fromisoformat(second["meta"]["created_at"]) == datetime(2020, 1, 1, tzinfo=UTC)

    await store.save_session("s1", state)
    primary = json.loads((store.session_dir("s1") / "session.json").read_text(encoding="utf-8"))
    assert datetime.fromisoformat(primary["meta"]["created_at"]) == datetime(2020, 1, 1, tzinfo=UTC)
    assert primary["state"]["messages"][0]["additional_properties"][MESSAGE_CREATED_AT_KEY].startswith("2020-01-01")


async def test_recovery_timestamp_stays_strictly_newer_when_clock_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(SkewedDateTime, "current", datetime(2026, 1, 1, 12, tzinfo=UTC))
    monkeypatch.setattr(store_module, "datetime", SkewedDateTime)
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "s1",
        {"messages": [Message("user", ["primary one"])], "compressed_msgs": []},
    )
    SkewedDateTime.current = datetime(2026, 1, 1, 15, tzinfo=UTC)
    await store.save_session(
        "s1",
        {
            "messages": [Message("user", ["primary one"]), Message("assistant", ["primary two"])],
            "compressed_msgs": [],
        },
    )

    SkewedDateTime.current = datetime(2026, 1, 1, 14, tzinfo=UTC)
    recovery_state = {"messages": [Message("user", ["recovery wins"])], "compressed_msgs": []}
    await asyncio.to_thread(store.save_recovery_session, "s1", recovery_state)

    session_dir = store.session_dir("s1")
    primary = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    recovery_file = session_dir / SESSION_RECOVERY_FILE_NAME
    recovery = json.loads(recovery_file.read_text(encoding="utf-8"))
    assert datetime.fromisoformat(recovery["meta"]["updated_at"]) > datetime.fromisoformat(
        primary["meta"]["updated_at"]
    )

    loaded = await store.load_session("s1", prefer_recovery=True)
    assert loaded is not None
    assert loaded["messages"][0].text == "recovery wins"
    assert recovery_file.exists()


async def test_healthy_primary_checkpoint_does_not_parse_existing_recovery_meta(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonFileStateStore(tmp_path)
    state = {"messages": [Message("user", ["hello"])], "compressed_msgs": []}
    await store.save_session("s1", state)
    await asyncio.to_thread(store.save_recovery_session, "s1", state)

    def fail_recovery_read(_session_id: str) -> dict[str, object]:
        raise AssertionError("healthy checkpoint parsed the recovery sidecar")

    monkeypatch.setattr(store, "_read_recovery_meta_unlocked", fail_recovery_read)
    await asyncio.to_thread(store.save_recovery_session, "s1", state)


async def test_naive_created_at_clamp_and_recovery_arbitration_are_utc_normalized(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    primary_state = {"messages": [Message("user", ["primary"])], "compressed_msgs": []}
    await store.save_session("s1", primary_state)
    session_dir = store.session_dir("s1")
    primary_file = session_dir / "session.json"
    backup_file = session_dir / "session.json.bak"
    for path in (primary_file, backup_file):
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["meta"]["created_at"] = "2030-01-01T12:00:00"
        envelope["meta"]["updated_at"] = "2029-01-01T00:00:00+00:00"
        path.write_text(json.dumps(envelope), encoding="utf-8")

    await store.save_session("s1", primary_state)
    primary = json.loads(primary_file.read_text(encoding="utf-8"))
    assert primary["meta"]["created_at"] == "2030-01-01T12:00:00"
    assert primary["meta"]["updated_at"] == "2030-01-01T12:00:00+00:00"

    recovery_state = {"messages": [Message("user", ["recovery"])], "compressed_msgs": []}
    await asyncio.to_thread(store.save_recovery_session, "s1", recovery_state)
    recovery_file = session_dir / SESSION_RECOVERY_FILE_NAME
    recovery = json.loads(recovery_file.read_text(encoding="utf-8"))
    recovery["meta"]["updated_at"] = "2031-01-01T00:00:00"
    recovery_file.write_text(json.dumps(recovery), encoding="utf-8")

    loaded = await store.load_session("s1", prefer_recovery=True)
    assert loaded is not None
    assert loaded["messages"][0].text == "recovery"


# --- rollback-snapshot fallback ---


async def test_load_recovers_corrupt_primary_and_backup_from_latest_snapshot(tmp_path: Path) -> None:
    """Rollback snapshots are the last-resort restore point if both live files are bad."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [Message("user", ["snapshot"])], "compressed_msgs": []})

    session_dir = tmp_path / "s1"
    snap_dir = session_dir / "snapshots"
    snap_dir.mkdir()
    (snap_dir / "turn_4.json").write_text((session_dir / "session.json").read_text(encoding="utf-8"), encoding="utf-8")
    (session_dir / "session.json").write_text("{ broken primary", encoding="utf-8")
    (session_dir / "session.json.bak").write_text("{ broken backup", encoding="utf-8")

    loaded = await store.load_session("s1")

    assert loaded is not None
    assert loaded["messages"][0].text == "snapshot"
    assert json.loads((session_dir / "session.json").read_text(encoding="utf-8"))["meta"]["session_id"] == "s1"


async def test_snapshot_recovery_uses_newest_turn(tmp_path: Path) -> None:
    """When live files are corrupt, recovery picks the highest-numbered valid snapshot."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [Message("user", ["older"])], "compressed_msgs": []})
    session_dir = tmp_path / "s1"
    older = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))

    await store.save_session("s1", {"messages": [Message("user", ["newer"])], "compressed_msgs": []})
    newer = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))

    snap_dir = session_dir / "snapshots"
    snap_dir.mkdir()
    (snap_dir / "turn_2.json").write_text(json.dumps(older), encoding="utf-8")
    (snap_dir / "turn_9.json").write_text(json.dumps(newer), encoding="utf-8")
    (session_dir / "session.json").write_text("{ broken primary", encoding="utf-8")
    (session_dir / "session.json.bak").write_text("{ broken backup", encoding="utf-8")

    loaded = await store.load_session("s1")

    assert loaded is not None
    assert loaded["messages"][0].text == "newer"


async def test_snapshot_recovery_works_when_primary_and_backup_are_missing(tmp_path: Path) -> None:
    """Rollback snapshots remain usable even if both live session files are gone."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [Message("user", ["snapshot only"])], "compressed_msgs": []})

    session_dir = tmp_path / "s1"
    snap_dir = session_dir / "snapshots"
    snap_dir.mkdir()
    (snap_dir / "turn_2.json").write_text(
        (session_dir / "session.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (session_dir / "session.json").unlink()
    (session_dir / "session.json.bak").unlink()

    loaded = await store.load_session("s1")

    assert loaded is not None
    assert loaded["messages"][0].text == "snapshot only"
    assert json.loads((session_dir / "session.json").read_text(encoding="utf-8"))["meta"]["session_id"] == "s1"


async def test_list_sessions_recovers_snapshot_only_session(tmp_path: Path) -> None:
    """Session listing should rediscover sessions whose only valid file is a rollback snapshot."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [Message("user", ["snapshot only"])], "compressed_msgs": []})

    session_dir = tmp_path / "s1"
    snap_dir = session_dir / "snapshots"
    snap_dir.mkdir()
    (snap_dir / "turn_2.json").write_text(
        (session_dir / "session.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (session_dir / "session.json").unlink()
    (session_dir / "session.json.bak").unlink()

    sessions = await store.list_sessions()

    assert [s.session_id for s in sessions] == ["s1"]
    assert (session_dir / "session.json").exists()


async def test_save_does_not_seed_metadata_from_rollback_snapshot(tmp_path: Path) -> None:
    """Save metadata inheritance must use live files only, not stale turn snapshots."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [Message("user", ["live"])], "compressed_msgs": []})

    session_dir = tmp_path / "s1"
    stale_snapshot = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    stale_snapshot["meta"]["agent_profile"] = "stale-profile"
    stale_snapshot["meta"]["agent_profile_history"] = ["stale"]
    stale_snapshot["meta"]["parent_session_id"] = "stale-parent"
    snap_dir = session_dir / "snapshots"
    snap_dir.mkdir()
    (snap_dir / "turn_9.json").write_text(json.dumps(stale_snapshot), encoding="utf-8")

    (session_dir / "session.json").write_text("{ broken primary", encoding="utf-8")
    (session_dir / "session.json.bak").write_text("{ broken backup", encoding="utf-8")

    await store.save_session("s1", {"messages": [Message("user", ["new"])], "compressed_msgs": []})

    meta = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))["meta"]
    assert meta["agent_profile"] == ""
    assert meta["agent_profile_history"] == []
    assert meta["parent_session_id"] == ""
