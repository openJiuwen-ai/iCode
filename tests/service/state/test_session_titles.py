# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for session title overlays — SessionMeta.display_title and update_session_titles."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import Message
from chrys.service.state.store import (
    SESSION_RECOVERY_FILE_NAME,
    ChatSessionMeta,
    JsonFileStateStore,
    SessionMeta,
    _extract_title,
)


def _meta(**overrides) -> SessionMeta:
    now = datetime.now(UTC)
    defaults = {
        "session_id": "sess1",
        "agent_profile": "code",
        "agent_display_name": "Code",
        "created_at": now,
        "updated_at": now,
        "message_count": 1,
    }
    defaults.update(overrides)
    return ChatSessionMeta(**defaults)


async def _saved_store(tmp_path: Path, session_id: str = "sess1") -> JsonFileStateStore:
    store = JsonFileStateStore(tmp_path)
    state = {"messages": [Message("user", ["fix the login bug"])], "compressed_msgs": []}
    await store.save_session(session_id, state, agent_profile="code")
    return store


# ─── display_title precedence ───────────────────────────────────────


async def test_display_title_precedence() -> None:
    meta = _meta(title="first message")
    assert meta.display_title == "first message"
    meta.generated_title = "Login bug fix"
    assert meta.display_title == "Login bug fix"
    meta.custom_title = "My session"
    assert meta.display_title == "My session"
    meta.custom_title = ""
    assert meta.display_title == "Login bug fix"


# ─── update_session_titles ──────────────────────────────────────────


async def test_update_generated_title_persists(tmp_path: Path) -> None:
    store = await _saved_store(tmp_path)

    updated = await store.update_session_titles("sess1", generated_title="Login bug fix")
    assert updated is not None
    assert updated.generated_title == "Login bug fix"
    assert updated.display_title == "Login bug fix"

    reloaded = await store.load_session_meta("sess1")
    assert reloaded is not None
    assert reloaded.generated_title == "Login bug fix"
    assert reloaded.title == "fix the login bug"


async def test_custom_title_wins_and_blocks_generated_updates(tmp_path: Path) -> None:
    store = await _saved_store(tmp_path)

    updated = await store.update_session_titles("sess1", custom_title="My session")
    assert updated is not None
    assert updated.custom_title == "My session"
    assert updated.display_title == "My session"

    refused = await store.update_session_titles("sess1", generated_title="should not land")
    assert refused is None
    reloaded = await store.load_session_meta("sess1")
    assert reloaded is not None
    assert reloaded.custom_title == "My session"
    assert reloaded.generated_title == ""


async def test_clearing_custom_title_reenables_generated(tmp_path: Path) -> None:
    store = await _saved_store(tmp_path)
    await store.update_session_titles("sess1", custom_title="My session")

    cleared = await store.update_session_titles("sess1", custom_title="")
    assert cleared is not None
    assert cleared.custom_title == ""

    updated = await store.update_session_titles("sess1", generated_title="Auto title")
    assert updated is not None
    assert updated.display_title == "Auto title"


async def test_generated_update_never_creates_session_file(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    result = await store.update_session_titles("ghost", generated_title="nope")
    assert result is None
    assert not (store.session_dir("ghost") / "session.json").exists()


async def test_custom_update_on_unsaved_session_stays_in_memory_until_first_save(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    # No session file is materialized for the pre-save title...
    updated = await store.update_session_titles("fresh", custom_title="Named early")
    assert updated is None
    assert not (store.session_dir("fresh") / "session.json").exists()

    # ...but the first full save merges it into the envelope.
    state = {"messages": [Message("user", ["hello"])], "compressed_msgs": []}
    await store.save_session("fresh", state, agent_profile="code")
    envelope = json.loads((store.session_dir("fresh") / "session.json").read_text(encoding="utf-8"))
    assert envelope["meta"]["custom_title"] == "Named early"


async def test_clear_on_unsaved_session_is_a_no_op(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    # Saving an empty title from the dialog on a never-saved session must
    # not create a ghost session file.
    cleared = await store.update_session_titles("fresh", custom_title="")
    assert cleared is None
    assert not store.session_dir("fresh").exists()

    # Set-then-clear before the first save discards the pending title too.
    await store.update_session_titles("fresh", custom_title="Named early")
    await store.update_session_titles("fresh", custom_title="")
    assert not store.session_dir("fresh").exists()
    state = {"messages": [Message("user", ["hello"])], "compressed_msgs": []}
    await store.save_session("fresh", state, agent_profile="code")
    meta = await store.load_session_meta("fresh")
    assert meta is not None
    assert meta.custom_title == ""


async def test_pending_title_does_not_resurrect_after_disk_clear(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.update_session_titles("fresh", custom_title="Named early")
    state = {"messages": [Message("user", ["hello"])], "compressed_msgs": []}
    await store.save_session("fresh", state, agent_profile="code")

    # Clear on disk, then save again: the consumed pending entry must not
    # bring the old title back.
    await store.update_session_titles("fresh", custom_title="")
    await store.save_session("fresh", state, agent_profile="code")
    meta = await store.load_session_meta("fresh")
    assert meta is not None
    assert meta.custom_title == ""


async def test_recovery_only_custom_title_survives_process_restart(tmp_path: Path) -> None:
    """A title set while only a crash sidecar exists must be mirrored into
    the sidecar and carried into the first primary save, even after the
    in-memory pending map is lost with the process."""
    store = JsonFileStateStore(tmp_path)
    state = {"messages": [Message("user", ["hello"])], "compressed_msgs": []}
    await asyncio.to_thread(store.save_recovery_session, "crashy", state, agent_profile="code")

    await store.update_session_titles("crashy", custom_title="Recovered custom")
    sidecar = json.loads((tmp_path / "crashy" / SESSION_RECOVERY_FILE_NAME).read_text(encoding="utf-8"))
    assert sidecar["meta"]["custom_title"] == "Recovered custom"
    assert not (store.session_dir("crashy") / "session.json").exists()

    # Simulate a restart: a fresh store has no pending titles in memory.
    restarted = JsonFileStateStore(tmp_path)
    await restarted.save_session("crashy", state, agent_profile="code")
    meta = await restarted.load_session_meta("crashy")
    assert meta is not None
    assert meta.custom_title == "Recovered custom"
    # The first primary save consumed and deleted the sidecar.
    assert not (tmp_path / "crashy" / SESSION_RECOVERY_FILE_NAME).exists()


async def test_recovery_resave_preserves_mirrored_titles(tmp_path: Path) -> None:
    """A later recovery save of a still-unsaved session must not blank the
    titles previously mirrored into the sidecar."""
    store = JsonFileStateStore(tmp_path)
    state = {"messages": [Message("user", ["hello"])], "compressed_msgs": []}
    await asyncio.to_thread(store.save_recovery_session, "crashy", state, agent_profile="code")
    await store.update_session_titles("crashy", custom_title="Recovered custom")

    restarted = JsonFileStateStore(tmp_path)
    await asyncio.to_thread(restarted.save_recovery_session, "crashy", state, agent_profile="code")
    sidecar = json.loads((tmp_path / "crashy" / SESSION_RECOVERY_FILE_NAME).read_text(encoding="utf-8"))
    assert sidecar["meta"]["custom_title"] == "Recovered custom"


async def test_pending_title_is_scoped_to_its_session(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    # Title set on a session that is then abandoned (e.g. /new before any
    # turn) must not leak into other sessions' saves.
    await store.update_session_titles("abandoned", custom_title="Orphan")
    state = {"messages": [Message("user", ["hello"])], "compressed_msgs": []}
    await store.save_session("other", state, agent_profile="code")
    meta = await store.load_session_meta("other")
    assert meta is not None
    assert meta.custom_title == ""
    assert not store.session_dir("abandoned").exists()


async def test_update_titles_preserves_state(tmp_path: Path) -> None:
    store = await _saved_store(tmp_path)
    await store.update_session_titles("sess1", custom_title="My session", generated_title="Auto")

    loaded = await store.load_session("sess1")
    assert loaded is not None
    assert len(loaded["messages"]) == 1
    assert loaded["messages"][0].text == "fix the login bug"


async def test_title_patch_does_not_promote_rollback_snapshot(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    live_state = {"messages": [Message("user", ["LIVE CONTENT"])], "compressed_msgs": []}
    await store.save_session("sess1", live_state)
    session_dir = store.session_dir("sess1")
    primary_file = session_dir / "session.json"
    backup_file = session_dir / "session.json.bak"
    snapshot = json.loads(primary_file.read_text(encoding="utf-8"))
    snapshot["state"]["messages"][0]["contents"][0]["text"] = "ROLLED BACK CONTENT"
    snapshots_dir = session_dir / "snapshots"
    snapshots_dir.mkdir()
    (snapshots_dir / "turn_1.json").write_text(json.dumps(snapshot), encoding="utf-8")
    primary_file.write_text("{ broken primary", encoding="utf-8")
    backup_file.write_text("{ broken backup", encoding="utf-8")

    updated = await store.update_session_titles("sess1", custom_title="new title")

    assert updated is None
    assert primary_file.read_text(encoding="utf-8") == "{ broken primary"
    assert backup_file.read_text(encoding="utf-8") == "{ broken backup"

    await store.save_session("sess1", live_state)
    loaded = await store.load_session("sess1")
    meta = await store.load_session_meta("sess1")
    assert loaded is not None
    assert loaded["messages"][0].text == "LIVE CONTENT"
    assert meta is not None
    assert meta.custom_title == "new title"


async def test_title_patch_preserves_an_unreadable_primary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = await _saved_store(tmp_path)
    primary = store.session_dir("sess1") / "session.json"
    backup = primary.with_suffix(".json.bak")
    before = primary.read_bytes()
    backup_bytes = backup.read_bytes()
    read = Path.read_bytes

    def deny_primary(path: Path) -> bytes:
        if path == primary:
            raise PermissionError("primary is unreadable")
        return read(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", deny_primary)
        with pytest.raises(PermissionError, match="primary is unreadable"):
            await store.update_session_titles("sess1", custom_title="New title")
    assert primary.read_bytes() == before
    assert backup.read_bytes() == backup_bytes


async def test_title_patch_fails_closed_on_parseable_invalid_primary_meta(tmp_path: Path) -> None:
    store = await _saved_store(tmp_path)
    session_dir = store.session_dir("sess1")
    primary_file = session_dir / "session.json"
    backup_file = session_dir / "session.json.bak"
    primary = json.loads(primary_file.read_text(encoding="utf-8"))
    primary["meta"] = "malformed"
    malformed_primary = json.dumps(primary)
    healthy_backup = backup_file.read_text(encoding="utf-8")
    primary_file.write_text(malformed_primary, encoding="utf-8")

    updated = await store.update_session_titles("sess1", generated_title="must not land")

    assert updated is None
    assert primary_file.read_text(encoding="utf-8") == malformed_primary
    assert backup_file.read_text(encoding="utf-8") == healthy_backup


@pytest.mark.parametrize("custom_title", ["Replacement pin", ""])
async def test_deferred_custom_title_overrides_valid_backup_after_invalid_primary_meta(
    tmp_path: Path,
    custom_title: str,
) -> None:
    store = await _saved_store(tmp_path)
    await store.update_session_titles("sess1", custom_title="Original pin")
    session_dir = store.session_dir("sess1")
    primary_file = session_dir / "session.json"
    primary = json.loads(primary_file.read_text(encoding="utf-8"))
    primary["meta"] = "malformed"
    primary_file.write_text(json.dumps(primary), encoding="utf-8")

    updated = await store.update_session_titles("sess1", custom_title=custom_title)
    assert updated is None

    state = {"messages": [Message("user", ["fix the login bug"])], "compressed_msgs": []}
    await store.save_session("sess1", state, agent_profile="code")
    saved = await store.load_session_meta("sess1")
    assert saved is not None
    assert saved.custom_title == custom_title


@pytest.mark.parametrize("custom_title", ["Replacement pin", ""])
async def test_recovery_deferred_custom_title_survives_store_restart(
    tmp_path: Path,
    custom_title: str,
) -> None:
    store = await _saved_store(tmp_path)
    await store.update_session_titles("sess1", custom_title="Original pin")
    session_dir = store.session_dir("sess1")
    primary_file = session_dir / "session.json"
    primary = json.loads(primary_file.read_text(encoding="utf-8"))
    primary["meta"] = "malformed"
    primary_file.write_text(json.dumps(primary), encoding="utf-8")
    state = {"messages": [Message("user", ["fix the login bug"])], "compressed_msgs": []}
    await asyncio.to_thread(store.save_recovery_session, "sess1", state, agent_profile="code")
    recovery_file = session_dir / SESSION_RECOVERY_FILE_NAME
    recovery = json.loads(recovery_file.read_text(encoding="utf-8"))
    recovery["meta"]["updated_at"] = "2000-01-01T00:00:00+00:00"
    recovery_file.write_text(json.dumps(recovery), encoding="utf-8")

    updated = await store.update_session_titles("sess1", custom_title=custom_title)
    assert updated is None
    mirrored = json.loads(recovery_file.read_text(encoding="utf-8"))
    assert mirrored["meta"]["updated_at"] == "2000-01-01T00:00:00+00:00"
    assert mirrored["meta"]["custom_title"] == custom_title

    restarted = JsonFileStateStore(tmp_path)
    await restarted.save_session("sess1", state, agent_profile="code")
    saved = await restarted.load_session_meta("sess1")
    assert saved is not None
    assert saved.custom_title == custom_title
    assert not (session_dir / SESSION_RECOVERY_FILE_NAME).exists()


async def test_wrong_session_recovery_titles_do_not_override_valid_backup(tmp_path: Path) -> None:
    store = await _saved_store(tmp_path)
    await store.update_session_titles("sess1", custom_title="Original pin")
    session_dir = store.session_dir("sess1")
    primary_file = session_dir / "session.json"
    primary = json.loads(primary_file.read_text(encoding="utf-8"))
    primary["meta"] = "malformed"
    primary_file.write_text(json.dumps(primary), encoding="utf-8")
    state = {"messages": [Message("user", ["fix the login bug"])], "compressed_msgs": []}
    await asyncio.to_thread(store.save_recovery_session, "sess1", state, agent_profile="code")
    recovery_file = session_dir / SESSION_RECOVERY_FILE_NAME
    recovery = json.loads(recovery_file.read_text(encoding="utf-8"))
    recovery["meta"]["session_id"] = "other-session"
    recovery["meta"]["custom_title"] = "Foreign pin"
    recovery_file.write_text(json.dumps(recovery), encoding="utf-8")

    restarted = JsonFileStateStore(tmp_path)
    await restarted.save_session("sess1", state, agent_profile="code")

    saved = await restarted.load_session_meta("sess1")
    assert saved is not None
    assert saved.custom_title == "Original pin"


async def test_valid_primary_title_is_not_reverted_by_newer_stale_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = await _saved_store(tmp_path)
    state = {"messages": [Message("user", ["fix the login bug"])], "compressed_msgs": []}
    await asyncio.to_thread(store.save_recovery_session, "sess1", state, agent_profile="code")
    recovery_file = store.session_dir("sess1") / SESSION_RECOVERY_FILE_NAME
    recovery = json.loads(recovery_file.read_text(encoding="utf-8"))
    recovery["meta"]["custom_title"] = "Stale recovery pin"
    recovery["meta"]["updated_at"] = "2999-01-01T00:00:00+00:00"
    recovery_file.write_text(json.dumps(recovery), encoding="utf-8")
    monkeypatch.setattr(store, "_patch_recovery_titles_unlocked", lambda *_args, **_kwargs: None)

    updated = await store.update_session_titles("sess1", custom_title="Primary pin")
    assert updated is not None

    await store.save_session("sess1", state, agent_profile="code")
    saved = await store.load_session_meta("sess1")
    assert saved is not None
    assert saved.custom_title == "Primary pin"


@pytest.mark.parametrize("next_title", ["Fresh pin", "Deferred pin"])
async def test_valid_title_patch_consumes_pending_after_primary_repair(
    tmp_path: Path,
    next_title: str,
) -> None:
    store = await _saved_store(tmp_path)
    session_dir = store.session_dir("sess1")
    primary_file = session_dir / "session.json"
    primary = json.loads(primary_file.read_text(encoding="utf-8"))
    primary["meta"] = "malformed"
    primary_file.write_text(json.dumps(primary), encoding="utf-8")
    assert await store.update_session_titles("sess1", custom_title="Deferred pin") is None
    primary_file.write_text((session_dir / "session.json.bak").read_text(encoding="utf-8"), encoding="utf-8")

    updated = await store.update_session_titles("sess1", custom_title=next_title)

    assert updated is not None
    assert updated.custom_title == next_title
    assert "sess1" not in store._pending_custom_titles
    state = {"messages": [Message("user", ["fix the login bug"])], "compressed_msgs": []}
    await store.save_session("sess1", state, agent_profile="code")
    saved = await store.load_session_meta("sess1")
    assert saved is not None
    assert saved.custom_title == next_title


async def test_ordinary_save_carries_titles_forward(tmp_path: Path) -> None:
    store = await _saved_store(tmp_path)
    await store.update_session_titles("sess1", custom_title="Pinned", generated_title="Auto")

    state = {
        "messages": [Message("user", ["fix the login bug"]), Message("assistant", ["done"])],
        "compressed_msgs": [],
    }
    await store.save_session("sess1", state, agent_profile="code")

    reloaded = await store.load_session_meta("sess1")
    assert reloaded is not None
    assert reloaded.custom_title == "Pinned"
    assert reloaded.generated_title == "Auto"


async def test_list_sessions_exposes_title_overlays(tmp_path: Path) -> None:
    store = await _saved_store(tmp_path)
    await store.update_session_titles("sess1", generated_title="Auto title")

    sessions = await store.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].display_title == "Auto title"


async def test_fork_carries_custom_title_verbatim(tmp_path: Path) -> None:
    store = await _saved_store(tmp_path)
    await store.update_session_titles("sess1", custom_title="Pinned", generated_title="Auto")

    fork_id = store.fork_session("sess1")
    meta = await store.load_session_meta(fork_id)
    assert meta is not None
    assert meta.custom_title == "Pinned"
    assert meta.generated_title == "Auto"
    assert meta.display_title == "Pinned"

    # The carried pin keeps auto-generated refreshes disabled on the fork,
    # exactly as on the parent.
    updated = await store.update_session_titles(fork_id, generated_title="Fork topic")
    assert updated is None

    # Forking a fork carries the same title again.
    nested_id = store.fork_session(fork_id)
    nested = await store.load_session_meta(nested_id)
    assert nested is not None
    assert nested.custom_title == "Pinned"

    # Renaming the fork stays independent of the parent.
    renamed = await store.update_session_titles(fork_id, custom_title="Fork pin")
    assert renamed is not None
    assert renamed.custom_title == "Fork pin"
    parent = await store.load_session_meta("sess1")
    assert parent is not None
    assert parent.custom_title == "Pinned"


async def test_fork_without_custom_title_stays_unpinned(tmp_path: Path) -> None:
    store = await _saved_store(tmp_path)
    await store.update_session_titles("sess1", generated_title="Auto")

    fork_id = store.fork_session("sess1")
    meta = await store.load_session_meta(fork_id)
    assert meta is not None
    assert meta.custom_title == ""
    assert meta.generated_title == "Auto"

    # No pin was carried, so the fork still accepts auto-generated titles.
    updated = await store.update_session_titles(fork_id, generated_title="Fork topic")
    assert updated is not None
    assert updated.generated_title == "Fork topic"


async def test_unchanged_title_patch_skips_rewrite(tmp_path: Path) -> None:
    store = await _saved_store(tmp_path)
    first = await store.update_session_titles("sess1", generated_title="Same title")
    assert first is not None
    session_file = store.session_dir("sess1") / "session.json"
    before = session_file.stat().st_mtime_ns

    again = await store.update_session_titles("sess1", generated_title="Same title")
    assert again is not None
    assert again.generated_title == "Same title"
    assert session_file.stat().st_mtime_ns == before


# --- _extract_title: which user message names a session ---


def test_extract_title_skips_leading_continuation_nudge() -> None:
    """The no-prior-user resume shape must not title the session "continue"."""
    nudge = Message("user", ["continue"])
    nudge.additional_properties[HistoryMarkerKind.CONTINUATION_KEY] = True
    real = Message("user", ["fix the flaky scroll test"])

    assert _extract_title([nudge, Message("assistant", ["resuming"]), real]) == "fix the flaky scroll test"
    assert _extract_title([nudge]) == ""


def test_extract_title_accepts_injected_user_message() -> None:
    injected = Message("user", ["mid-turn guidance"])
    injected.additional_properties[HistoryMarkerKind.INJECTED_KEY] = True

    assert _extract_title([injected]) == "mid-turn guidance"
