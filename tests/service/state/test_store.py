# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for JsonFileStateStore — save/load/list, deletion, metadata, write locking, and meta provenance."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import chrys.service.state.store as store_module
import chrys.service.trajectory.tombstone as tombstone_module
from chrys.foundation.util.lock import FileLock
from chrys.kernel import Content, Message
from chrys.service.session.message_metadata import stamp_message_created_at
from chrys.service.state.store import (
    JsonFileStateStore,
    SessionMeta,
    _dir_size,
)
from chrys.service.trajectory.tombstone import DeleteOutcome, DeleteResult
from tests.service.state._store_helpers import SkewedDateTime, browser_page, write_legacy_envelope


@pytest.mark.asyncio
async def test_save_and_load_session(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    state = {
        "messages": [Message("user", ["hello"])],
        "compressed_msgs": [],
    }
    await store.save_session("sess1", state, agent_profile="code")
    loaded = await store.load_session("sess1")

    assert loaded is not None
    assert len(loaded["messages"]) == 1
    assert loaded["messages"][0].role == "user"


@pytest.mark.asyncio
async def test_load_session_accepts_legacy_serialized_history(tmp_path: Path) -> None:
    """Legacy-compatible type ids deserialize into Chrys-owned Message/Content."""
    store = JsonFileStateStore(tmp_path)
    legacy_messages = [
        Message("user", [Content.from_text("legacy prompt")]),
        Message(
            "assistant",
            [
                Content.from_text("legacy tool call"),
                Content.from_function_call("call_legacy", "read_file", arguments={"path": "a.py"}),
            ],
        ),
        Message(
            "tool",
            [Content.from_function_result("call_legacy", result="contents")],
        ),
    ]
    envelope = {
        "meta": {"session_id": "framework-legacy"},
        "state": {
            "messages": [message.to_dict() for message in legacy_messages],
            "compressed_msgs": [],
            "turn_counter": 1,
        },
    }
    session_file = store.session_dir("framework-legacy") / "session.json"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(json.dumps(envelope), encoding="utf-8")

    loaded = await store.load_session("framework-legacy")

    assert loaded is not None
    restored_messages = loaded["messages"]
    assert [type(message) for message in restored_messages] == [Message, Message, Message]
    assert [message.to_dict() for message in restored_messages] == [message.to_dict() for message in legacy_messages]
    assert all(type(content) is Content for message in restored_messages for content in message.contents)


@pytest.mark.asyncio
async def test_load_nonexistent_session(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    result = await store.load_session("nope")
    assert result is None


@pytest.mark.asyncio
async def test_list_sessions(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [Message("user", ["a"])], "compressed_msgs": []}, agent_profile="code")
    await store.save_session("s2", {"messages": [], "compressed_msgs": []}, agent_profile="task")

    sessions = await store.list_sessions()
    assert len(sessions) == 2
    ids = {s.session_id for s in sessions}
    assert ids == {"s1", "s2"}


@pytest.mark.asyncio
async def test_a_session_whose_delete_could_not_finish_is_not_listed_again(tmp_path: Path) -> None:
    """A delete that left remains behind (an open trajectory file) still means deleted."""
    from chrys.service.trajectory.tombstone import INTENT_SUFFIX, tombstones_dir

    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [Message("user", ["a"])], "compressed_msgs": []}, agent_profile="code")
    await store.save_session("s2", {"messages": [Message("user", ["b"])], "compressed_msgs": []}, agent_profile="code")
    doomed = store.session_dir("s1")

    graveyard = tombstones_dir(tmp_path)
    graveyard.mkdir(parents=True, exist_ok=True)
    (graveyard / f"{doomed.name}{INTENT_SUFFIX}").write_text(doomed.name, encoding="utf-8")

    listed = {meta.session_id for meta in await store.list_sessions()}
    assert listed == {"s2"}


@pytest.mark.asyncio
async def test_list_sessions_turn_count_from_state_counter(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "turny",
        {"messages": [Message("user", ["a"])], "compressed_msgs": [], "turn_counter": 7},
    )

    sessions = await store.list_sessions()

    assert [s.turn_count for s in sessions] == [7]


@pytest.mark.asyncio
async def test_list_sessions_total_tokens_from_state(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "tokeny",
        {"messages": [Message("user", ["a"])], "compressed_msgs": [], "total_session_tokens": 123_456},
    )
    await store.save_session("pre-usage", {"messages": [Message("user", ["b"])], "compressed_msgs": []})

    sessions = await store.list_sessions()

    by_id = {s.session_id: s.total_tokens for s in sessions}
    assert by_id == {"tokeny": 123_456, "pre-usage": 0}


@pytest.mark.asyncio
async def test_list_sessions_turn_count_falls_back_to_turn_markers(tmp_path: Path) -> None:
    """Sessions saved before ``turn_counter`` existed count their markers."""
    from chrys.foundation.models.history_markers import HistoryMarkerKind

    store = JsonFileStateStore(tmp_path)
    marker_one = Message("user", ["turn 1"])
    marker_one.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    marker_two = Message("user", ["turn 2"])
    marker_two.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    await store.save_session(
        "legacy-turns",
        {"messages": [Message("user", ["a"]), marker_one, marker_two], "compressed_msgs": []},
    )
    # Strip the counter the serializer stamped, mimicking a pre-counter file.
    session_file = store.session_dir("legacy-turns") / "session.json"
    envelope = json.loads(session_file.read_text(encoding="utf-8"))
    del envelope["state"]["turn_counter"]
    session_file.write_text(json.dumps(envelope), encoding="utf-8")

    sessions = await store.list_sessions()

    assert [s.turn_count for s in sessions] == [2]


@pytest.mark.asyncio
async def test_list_sessions_turn_count_fallback_covers_compacted_turns(tmp_path: Path) -> None:
    """Pre-counter sessions whose markers were compacted away use turn_range."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("compacted", {"messages": [Message("user", ["tail"])], "compressed_msgs": []})
    session_file = store.session_dir("compacted") / "session.json"
    envelope = json.loads(session_file.read_text(encoding="utf-8"))
    del envelope["state"]["turn_counter"]
    envelope["state"]["compressed_msgs"] = [
        {
            "compressed_context_id": "c1",
            "messages": [],
            "summary_text": "…",
            "marker_id": "turn_9",
            "turn_range": [1, 9],
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    session_file.write_text(json.dumps(envelope), encoding="utf-8")

    sessions = await store.list_sessions()

    assert [s.turn_count for s in sessions] == [9]


@pytest.mark.asyncio
async def test_list_sessions_turn_count_ignores_restarted_counter(tmp_path: Path) -> None:
    """A counter restarted below the compacted evidence must not undercount."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("restarted", {"messages": [Message("user", ["tail"])], "compressed_msgs": []})
    session_file = store.session_dir("restarted") / "session.json"
    envelope = json.loads(session_file.read_text(encoding="utf-8"))
    envelope["state"]["turn_counter"] = 2
    envelope["state"]["compressed_msgs"] = [
        {
            "compressed_context_id": "c1",
            "messages": [],
            "summary_text": "…",
            "marker_id": "turn_9",
            "turn_range": [1, 9],
            "created_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    session_file.write_text(json.dumps(envelope), encoding="utf-8")

    sessions = await store.list_sessions()

    assert [s.turn_count for s in sessions] == [9]


@pytest.mark.asyncio
async def test_list_sessions_serves_cached_meta_until_file_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("cached", {"messages": [Message("user", ["a"])], "compressed_msgs": []})
    parsed: list[str] = []
    parse = store._session_meta_from_envelope

    def counting_parse(envelope: dict[str, Any], *, size_bytes: int) -> SessionMeta:
        parsed.append(envelope["meta"]["session_id"])
        return parse(envelope, size_bytes=size_bytes)

    monkeypatch.setattr(store, "_session_meta_from_envelope", counting_parse)

    first = await store.list_sessions()
    second = await store.list_sessions()

    # Unchanged on disk — the cached meta is reused, not re-parsed; the size is re-measured.
    assert parsed == ["cached"]
    assert second == first
    assert first[0].size_bytes == _dir_size(store.session_dir("cached")) > 0

    await store.save_session(
        "cached",
        {"messages": [Message("user", ["a"]), Message("user", ["b"])], "compressed_msgs": []},
    )
    third = await store.list_sessions()

    assert parsed == ["cached", "cached"]
    assert third[0].message_count == 2


@pytest.mark.asyncio
async def test_list_sessions_cache_evicts_deleted_sessions(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("keep", {"messages": [], "compressed_msgs": []})
    await store.save_session("drop", {"messages": [], "compressed_msgs": []})
    await store.list_sessions()

    await store.delete_session("drop")
    sessions = await store.list_sessions()

    assert [s.session_id for s in sessions] == ["keep"]
    assert set(store._meta_cache) == {"keep"}


async def test_list_sessions_ignores_lock_directory(tmp_path: Path) -> None:
    """Root-level .locks metadata is not a session directory."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("real", {"messages": [Message("user", ["a"])], "compressed_msgs": []})

    write_legacy_envelope(tmp_path / ".locks" / "session.json", "lock-metadata", agent_profile="not-a-session")

    sessions = await store.list_sessions()

    assert [s.session_id for s in sessions] == ["real"]


async def test_list_sessions_skips_malformed_timestamp_metadata(tmp_path: Path) -> None:
    """One bad timestamp must not abort folder or legacy session listing."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("good", {"messages": [], "compressed_msgs": []})

    write_legacy_envelope(tmp_path / "bad-folder" / "session.json", "bad-folder", created_at="not-a-timestamp")
    write_legacy_envelope(tmp_path / "bad-legacy.json", "bad-legacy", created_at=123)

    sessions = await store.list_sessions()
    browsed = await browser_page(store)

    assert [s.session_id for s in sessions] == ["good"]
    assert [meta.session_id for meta in browsed] == ["good"]


@pytest.mark.asyncio
async def test_delete_session(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("del_me", {"messages": [], "compressed_msgs": []})
    session_dir = tmp_path / "del_me"
    snap_dir = session_dir / "snapshots"
    snap_dir.mkdir()
    (snap_dir / "turn_2.json").write_text("{}", encoding="utf-8")
    store.active_owner_path("del_me").write_text("{}", encoding="utf-8")

    await store.delete_session("del_me")

    assert await store.load_session("del_me") is None
    assert not session_dir.exists()
    assert not store.active_owner_path("del_me").exists()


@pytest.mark.asyncio
async def test_delete_session_surfaces_an_unowned_surviving_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("still_here", {"messages": [], "compressed_msgs": []})
    store._pending_custom_titles["still_here"] = "Keep me"

    def fail_without_owner(*_args: object, **_kwargs: object) -> DeleteResult:
        return DeleteResult(DeleteOutcome.INTENT_FAILED)

    monkeypatch.setattr(tombstone_module, "delete_session_directory", fail_without_owner)

    with pytest.raises(OSError, match="could not be deleted or scheduled"):
        await store.delete_session("still_here")

    assert await store.load_session("still_here") is not None
    assert await store.load_latest_session_id() == "still_here"
    assert store._pending_custom_titles["still_here"] == "Keep me"


@pytest.mark.asyncio
async def test_delete_session_refuses_active_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Store-level delete must not remove a session open in another process."""
    monkeypatch.setattr(store_module, "SESSION_ACTIVE_LOCK_TIMEOUT_SECONDS", 0.05)
    store = JsonFileStateStore(tmp_path)
    await store.save_session("busy", {"messages": [], "compressed_msgs": []})

    held = FileLock(store.active_lock_path("busy"), timeout=1.0)
    held.acquire()
    try:
        with pytest.raises(TimeoutError):
            await store.delete_session("busy")
    finally:
        held.release()

    assert (tmp_path / "busy").is_dir()
    assert await store.load_session("busy") is not None


@pytest.mark.asyncio
async def test_delete_session_allow_active_for_current_owner(tmp_path: Path) -> None:
    """Engine current-session deletion already owns the active lock."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session("current", {"messages": [], "compressed_msgs": []})

    held = FileLock(store.active_lock_path("current"), timeout=1.0)
    held.acquire()
    try:
        await store.delete_session("current", allow_active=True)
    finally:
        held.release()

    assert not (tmp_path / "current").exists()


@pytest.mark.asyncio
async def test_save_updates_metadata(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [], "compressed_msgs": []}, agent_profile="code")
    sessions = await store.list_sessions()
    assert sessions[0].agent_profile == "code"
    assert sessions[0].message_count == 0

    # Save again with more messages
    await store.save_session("s1", {"messages": [Message("user", ["hi"])], "compressed_msgs": []})
    sessions = await store.list_sessions()
    assert sessions[0].message_count == 1
    # created_at should be preserved
    assert sessions[0].agent_profile == "code"


@pytest.mark.asyncio
async def test_save_inherits_metadata_from_valid_backup_hidden_by_malformed_primary(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    state = {"messages": [Message("user", ["old"])], "compressed_msgs": []}
    await store.save_session("s1", state, agent_profile="code")
    session_dir = store.session_dir("s1")
    primary_file = session_dir / "session.json"
    backup_file = session_dir / "session.json.bak"
    primary = json.loads(primary_file.read_text(encoding="utf-8"))
    backup = json.loads(backup_file.read_text(encoding="utf-8"))
    inherited_created_at = "2020-01-01T00:00:00+00:00"
    backup["meta"]["created_at"] = inherited_created_at
    backup["meta"]["updated_at"] = "2020-01-02T00:00:00+00:00"
    backup_file.write_text(json.dumps(backup), encoding="utf-8")
    primary["meta"] = "malformed"
    primary_file.write_text(json.dumps(primary), encoding="utf-8")

    await store.save_session(
        "s1",
        {"messages": [Message("user", ["old"]), Message("assistant", ["new"])], "compressed_msgs": []},
    )

    saved = json.loads(primary_file.read_text(encoding="utf-8"))
    assert saved["meta"]["created_at"] == inherited_created_at
    assert saved["meta"]["agent_profile"] == "code"


@pytest.mark.asyncio
async def test_missing_created_at_repairs_only_that_field(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    message = Message("user", ["old message"])
    stamp_message_created_at(message, "2020-01-01T00:00:00+00:00")
    state = {"messages": [message], "compressed_msgs": []}
    await store.save_session(
        "s1",
        state,
        agent_profile="code",
        model_provider="openai",
        primary_cwd="/work",
    )
    await store.update_session_titles("s1", custom_title="Pinned")
    session_dir = store.session_dir("s1")
    for path in (session_dir / "session.json", session_dir / "session.json.bak"):
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["meta"].pop("created_at")
        path.write_text(json.dumps(envelope), encoding="utf-8")

    await store.save_session("s1", state)

    saved = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))["meta"]
    assert datetime.fromisoformat(saved["created_at"]) == datetime(2020, 1, 1, tzinfo=UTC)
    assert saved["custom_title"] == "Pinned"
    assert saved["agent_profile"] == "code"
    assert saved["model_provider"] == "openai"
    assert saved["primary_cwd"] == "/work"


async def test_clock_rollback_clamps_updated_at_without_discarding_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(SkewedDateTime, "current", datetime(2026, 1, 1, 12, tzinfo=UTC))
    monkeypatch.setattr(store_module, "datetime", SkewedDateTime)
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "s1",
        {"messages": [Message("user", ["one"])], "compressed_msgs": []},
        agent_profile="code",
        model_provider="openai",
        primary_cwd="/work",
    )
    await store.update_session_titles("s1", custom_title="Pinned")

    SkewedDateTime.current = datetime(2026, 1, 1, 11, tzinfo=UTC)
    await store.save_session(
        "s1",
        {"messages": [Message("user", ["one"]), Message("assistant", ["two"])], "compressed_msgs": []},
    )
    skewed = json.loads((store.session_dir("s1") / "session.json").read_text(encoding="utf-8"))["meta"]
    assert datetime.fromisoformat(skewed["created_at"]) == datetime(2026, 1, 1, 12, tzinfo=UTC)
    assert datetime.fromisoformat(skewed["updated_at"]) == datetime(2026, 1, 1, 12, tzinfo=UTC)

    SkewedDateTime.current = datetime(2026, 1, 1, 13, tzinfo=UTC)
    await store.save_session(
        "s1",
        {
            "messages": [
                Message("user", ["one"]),
                Message("assistant", ["two"]),
                Message("user", ["three"]),
            ],
            "compressed_msgs": [],
        },
    )
    saved = json.loads((store.session_dir("s1") / "session.json").read_text(encoding="utf-8"))["meta"]
    assert saved["custom_title"] == "Pinned"
    assert saved["agent_profile"] == "code"
    assert saved["model_provider"] == "openai"
    assert saved["primary_cwd"] == "/work"


# --- _dir_size tests ---


def test_dir_size_empty(tmp_path: Path) -> None:
    assert _dir_size(tmp_path) == 0


def test_dir_size_nested(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.txt").write_text("abc", encoding="utf-8")
    (tmp_path / "top.txt").write_text("xy", encoding="utf-8")
    assert _dir_size(tmp_path) == 5  # 3 + 2


def test_dir_size_nonexistent(tmp_path: Path) -> None:
    assert _dir_size(tmp_path / "nope") == 0


# --- list_sessions size_bytes ---


@pytest.mark.asyncio
async def test_list_sessions_includes_size_bytes(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [Message("user", ["data"])], "compressed_msgs": []})

    sessions = await store.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].size_bytes > 0  # session.json has content


@pytest.mark.asyncio
async def test_save_times_out_when_write_lock_is_held(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second writer should fail cleanly instead of racing session.json."""
    store = JsonFileStateStore(tmp_path)
    monkeypatch.setattr(store_module, "SESSION_WRITE_LOCK_TIMEOUT_SECONDS", 0.01)

    lock_path = store_module.session_write_lock_path(tmp_path, "locked")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    held = FileLock(lock_path, timeout=1.0)
    held.acquire()
    try:
        with pytest.raises(TimeoutError):
            await store.save_session("locked", {"messages": [], "compressed_msgs": []})
    finally:
        held.release()


# --- meta provenance (schema_version + app_version) --------------------


async def test_save_stamps_schema_and_app_version(tmp_path: Path) -> None:
    """New saves write ``schema_version`` and ``app_version`` into meta
    so future readers can gate migration on version rather than sniffing
    field presence."""
    from chrys import __version__ as app_version
    from chrys.service.state.store import SESSION_SCHEMA_VERSION

    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [], "compressed_msgs": []}, agent_profile="code")

    raw = json.loads((tmp_path / "s1" / "session.json").read_text(encoding="utf-8"))
    assert raw["meta"]["schema_version"] == SESSION_SCHEMA_VERSION
    assert raw["meta"]["app_version"] == app_version


async def test_save_stamps_platform_os_and_arch(tmp_path: Path) -> None:
    """``os_name`` + ``arch`` come from ``common.platform`` so session
    files carry provenance of the runtime that wrote them."""
    from chrys.foundation.platform import get_platform

    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [], "compressed_msgs": []})

    plat = get_platform()
    meta = json.loads((tmp_path / "s1" / "session.json").read_text(encoding="utf-8"))["meta"]
    assert meta["os_name"] == plat.os_name
    assert meta["arch"] == plat.arch


async def test_save_stamps_model_fields_only_when_supplied(tmp_path: Path) -> None:
    """Model provenance is limited to non-sensitive model/session fields;
    nothing else from the caller's model profile should leak into the
    file."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "s1",
        {"messages": [], "compressed_msgs": []},
        model_provider="anthropic",
        model_api_style="chat_completions",
        model_id="claude-sonnet-4-6",
        model_base_url="https://api.anthropic.com",
        model_profile_fingerprint="model-fp",
        agent_profile_fingerprint="agent-fp",
        service_session_id="resp_123",
    )

    meta = json.loads((tmp_path / "s1" / "session.json").read_text(encoding="utf-8"))["meta"]
    assert meta["model_provider"] == "anthropic"
    assert meta["model_api_style"] == "chat_completions"
    assert meta["model_id"] == "claude-sonnet-4-6"
    assert meta["model_base_url"] == "https://api.anthropic.com"
    assert meta["model_profile_fingerprint"] == "model-fp"
    assert meta["agent_profile_fingerprint"] == "agent-fp"
    assert meta["service_session_id"] == "resp_123"
    # Explicitly assert the environment fields are NOT in meta — the
    # store's surface deliberately won't accept them, so disk output
    # stays provider/id/base_url only.
    for forbidden in ("api_key", "http_headers", "chat_options", "http_read_timeout"):
        assert forbidden not in meta


@pytest.mark.parametrize(
    ("field", "read"),
    [
        pytest.param("model_profile_id", lambda meta: meta.model_profile_id, id="model_profile_id"),
        pytest.param("agent_profile_id", lambda meta: meta.agent_profile_id, id="agent_profile_id"),
    ],
)
async def test_profile_id_round_trips_and_explicit_empty_overwrites(
    tmp_path: Path, field: str, read: Callable[[SessionMeta], str | None]
) -> None:
    store = JsonFileStateStore(tmp_path)
    state = {"messages": [], "compressed_msgs": []}

    await store.save_session("s1", state, **{field: "saved-profile"})

    saved = await store.load_session_meta("s1")
    assert saved is not None
    assert read(saved) == "saved-profile"

    await store.save_session("s1", state, **{field: ""})

    cleared = await store.load_session_meta("s1")
    assert cleared is not None
    assert read(cleared) == ""
    raw = json.loads((tmp_path / "s1" / "session.json").read_text(encoding="utf-8"))
    assert raw["meta"][field] == ""


@pytest.mark.parametrize(
    ("field", "read"),
    [
        pytest.param("model_profile_id", lambda meta: meta.model_profile_id, id="model_profile_id"),
        pytest.param("agent_profile_id", lambda meta: meta.agent_profile_id, id="agent_profile_id"),
    ],
)
async def test_recovery_session_profile_id_round_trips(
    tmp_path: Path, field: str, read: Callable[[SessionMeta], str | None]
) -> None:
    store = JsonFileStateStore(tmp_path)
    await asyncio.to_thread(
        store.save_recovery_session,
        "recovering",
        {"messages": [], "compressed_msgs": []},
        **{field: "recovery-profile"},
    )

    meta = await store.load_recovery_session_meta("recovering")

    assert meta is not None
    assert read(meta) == "recovery-profile"


async def test_save_preserves_model_fields_when_next_save_omits_them(tmp_path: Path) -> None:
    """If a later save call omits model_* (e.g. a profile swap where
    the caller lost the reference), the prior values are preserved
    rather than blanked."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "s1",
        {"messages": [], "compressed_msgs": []},
        model_provider="openai",
        model_api_style="responses",
        model_id="gpt-4o",
        model_base_url="https://api.openai.com/v1",
        service_session_id="resp_123",
    )
    # Second save with no model fields — must NOT overwrite the stamp.
    await store.save_session("s1", {"messages": [], "compressed_msgs": []})

    meta = json.loads((tmp_path / "s1" / "session.json").read_text(encoding="utf-8"))["meta"]
    assert meta["model_provider"] == "openai"
    assert meta["model_api_style"] == "responses"
    assert meta["model_id"] == "gpt-4o"
    assert meta["model_base_url"] == "https://api.openai.com/v1"
    assert meta["service_session_id"] == "resp_123"


@pytest.mark.parametrize(
    ("later_save", "expected"),
    [
        pytest.param(
            {
                "model_provider": "openai",
                "model_api_style": "chat_completions",
                "model_id": "gpt-5",
                "service_session_id": "",
            },
            {"model_api_style": "chat_completions", "service_session_id": ""},
            id="service_session_id",
        ),
        pytest.param(
            {
                "model_provider": "anthropic",
                "model_api_style": "",
                "model_id": "claude-sonnet-4-6",
                "service_session_id": "",
            },
            {
                "model_provider": "anthropic",
                "model_api_style": "",
                "model_id": "claude-sonnet-4-6",
                "service_session_id": "",
            },
            id="model_api_style",
        ),
    ],
)
async def test_save_can_clear_stale_model_fields(
    tmp_path: Path, later_save: dict[str, str], expected: dict[str, str]
) -> None:
    """A later save that names a field empty clears it: a non-Responses save drops the
    stale provider-side session id, a non-OpenAI save drops the stale Responses API style."""
    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "s1",
        {"messages": [], "compressed_msgs": []},
        model_provider="openai",
        model_api_style="responses",
        model_id="gpt-5",
        service_session_id="resp_123",
    )
    await store.save_session("s1", {"messages": [], "compressed_msgs": []}, **later_save)

    meta = json.loads((tmp_path / "s1" / "session.json").read_text(encoding="utf-8"))["meta"]
    assert {key: meta[key] for key in expected} == expected


@pytest.mark.asyncio
async def test_list_sessions_surfaces_version_fields(tmp_path: Path) -> None:
    """``SessionMeta`` round-trips ``schema_version`` and ``app_version``
    from disk."""
    from chrys import __version__ as app_version
    from chrys.service.state.store import SESSION_SCHEMA_VERSION

    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [], "compressed_msgs": []})

    sessions = await store.list_sessions()
    assert sessions[0].schema_version == SESSION_SCHEMA_VERSION
    assert sessions[0].app_version == app_version


@pytest.mark.asyncio
async def test_list_sessions_surfaces_platform_and_model(tmp_path: Path) -> None:
    """``SessionMeta`` also round-trips the platform + model fields."""
    from chrys.foundation.platform import get_platform

    store = JsonFileStateStore(tmp_path)
    await store.save_session(
        "s1",
        {"messages": [], "compressed_msgs": []},
        model_provider="anthropic",
        model_api_style="chat_completions",
        model_id="claude-sonnet-4-6",
        model_base_url="https://api.anthropic.com",
        service_session_id="resp_123",
    )
    sessions = await store.list_sessions()

    plat = get_platform()
    assert sessions[0].os_name == plat.os_name
    assert sessions[0].arch == plat.arch
    assert sessions[0].model_provider == "anthropic"
    assert sessions[0].model_api_style == "chat_completions"
    assert sessions[0].model_id == "claude-sonnet-4-6"
    assert sessions[0].model_base_url == "https://api.anthropic.com"
    assert sessions[0].service_session_id == "resp_123"


@pytest.mark.parametrize(
    ("field", "read"),
    [
        pytest.param(
            "agent_profile_fingerprint",
            lambda meta: meta.agent_profile_fingerprint,
            id="agent_profile_fingerprint",
        ),
        pytest.param(
            "model_profile_fingerprint",
            lambda meta: meta.model_profile_fingerprint,
            id="model_profile_fingerprint",
        ),
    ],
)
async def test_list_sessions_surfaces_profile_fingerprint(
    tmp_path: Path, field: str, read: Callable[[SessionMeta], str | None]
) -> None:
    store = JsonFileStateStore(tmp_path)
    await store.save_session("s1", {"messages": [], "compressed_msgs": []}, **{field: "profile-fp"})

    sessions = await store.list_sessions()

    assert read(sessions[0]) == "profile-fp"


@pytest.mark.asyncio
async def test_a_checkpoint_digests_the_bytes_that_reached_the_file(tmp_path: Path) -> None:
    """A surrogateescaped path is escaped on the way to disk; the digest must
    cover what landed, not a second encoding of the same string."""
    store = JsonFileStateStore(tmp_path)
    checkpoint = await store.save_session(
        "s1",
        {"messages": [Message("user", ["hi"])], "compressed_msgs": []},
        primary_cwd="/work/pro\udcffject",
    )
    on_disk = (tmp_path / "s1" / "session.json").read_bytes()
    assert checkpoint.content_hash == hashlib.sha256(on_disk).hexdigest()


@pytest.mark.parametrize("name", ["session.json", "session.json.bak"])
def test_restore_and_migration_share_the_normal_session_reader(tmp_path, monkeypatch, name):
    from chrys.service.state import _session_files

    store = JsonFileStateStore(tmp_path)
    session_dir = store.session_dir("session-a")
    session_dir.mkdir()
    path = session_dir / name
    original = b'{"meta":{"kind":"workflow"},"state":{"identity":{"source_kind":"project"}}}'
    path.write_bytes(original)
    opened = []
    read_bytes = Path.read_bytes

    def ordinary_read(source):
        if source == path:
            opened.append(source)
        return read_bytes(source)

    monkeypatch.setattr(Path, "read_bytes", ordinary_read)
    copies = {}
    assert store._read_json_file(path) == json.loads(original)
    assert _session_files.migrate_session_envelopes(
        session_dir, backup=copies.__setitem__, on_read_error=lambda *_: pytest.fail("read error")
    )
    assert copies == {path: original}
    assert opened.count(path) == 3  # normal restore, migration probe and locked re-read
    assert store._read_json_file(path)["state"]["identity"]["source_kind"] == "workdir"


@pytest.mark.parametrize("blocked", ["read-only", "lock", "re-read", "chat"])
def test_session_migration_uses_the_normal_write_lock_after_probing(tmp_path, monkeypatch, blocked):
    from chrys.service.state._session_files import migrate_session_envelopes, session_write_lock_path

    session_dir = tmp_path / "session-a"
    session_dir.mkdir()
    path = session_dir / "session.json"
    old = {"meta": {"kind": "workflow"}, "state": {"identity": {"source_kind": "project"}}}
    if blocked == "chat":
        old["meta"]["kind"] = "chat"
    original = json.dumps(old).encode()
    path.write_bytes(original)
    updated = json.dumps({**old, "state": {"identity": {"source_kind": "workdir"}}}).encode()
    acquire = FileLock.acquire
    write_lock = session_write_lock_path(tmp_path, session_dir.name)

    def acquire_after_other_writer(lock):
        assert lock._path == write_lock
        if blocked == "chat":
            pytest.fail("A chat envelope needs no write lock")
        if blocked == "lock":
            raise TimeoutError("busy")
        if blocked == "re-read":
            path.write_bytes(updated)
        acquire(lock)

    monkeypatch.setattr(FileLock, "acquire", acquire_after_other_writer)
    if blocked == "read-only":
        path.chmod(0o400)
    try:
        assert not migrate_session_envelopes(
            session_dir, backup=lambda *_: pytest.fail("skipped"), on_read_error=lambda *_: pytest.fail("read error")
        )
    finally:
        if blocked == "read-only":
            path.chmod(0o600)
    assert path.read_bytes() == (updated if blocked == "re-read" else original)


async def test_session_load_follows_a_symlinked_checkpoint(tmp_path):
    from tests.support.symlinks import symlink_or_skip

    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session("s1", {"messages": [Message("user", ["hello"])], "compressed_msgs": []})
    directory = store.session_dir("s1")
    path = directory / "session.json"
    target = tmp_path / "external.json"
    path.rename(target)
    (directory / "session.json.bak").unlink()
    symlink_or_skip(path, target)

    restored = await store.load_session("s1")
    assert restored is not None
    assert len(restored["messages"]) == 1
    assert restored["messages"][0].role == "user"
    assert path.is_symlink()


def test_session_migration_skips_parsing_without_legacy_source_markers(tmp_path, monkeypatch):
    from chrys.service.state import _session_files

    path = tmp_path / "session.json"
    path.write_bytes(b'{"meta":{"kind":"chat"},"state":{"content":"project"}}')

    def unexpected(*args, **kwargs):
        pytest.fail("Noncandidate checkpoints should not be parsed")

    monkeypatch.setattr(_session_files.json, "loads", unexpected)
    assert not _session_files.migrate_session_envelopes(tmp_path, backup=unexpected, on_read_error=unexpected)


def test_session_migration_reads_escaped_legacy_source_markers(tmp_path):
    from chrys.service.state._session_files import migrate_session_envelopes

    path = tmp_path / "session.json"
    path.write_bytes(rb'{"meta":{"kind":"workflow"},"state":{"identity":{"sourc\u0065_kind":"pr\u006fject"}}}')

    assert migrate_session_envelopes(tmp_path, backup=lambda *_: None, on_read_error=lambda *_: pytest.fail("invalid"))
    assert json.loads(path.read_bytes())["state"]["identity"]["source_kind"] == "workdir"


def test_session_migration_does_not_recreate_a_moved_session(tmp_path):
    from chrys.service.state._session_files import migrate_session_envelopes

    session = tmp_path / "session-a"
    session.mkdir()
    path = session / "session.json"
    raw = b'{"meta":{"kind":"workflow"},"state":{"identity":{"source_kind":"project"}}}'
    path.write_bytes(raw)
    moved = tmp_path / "moved"

    def move_session(_path, original):
        assert original == raw
        session.rename(moved)

    assert not migrate_session_envelopes(session, backup=move_session, on_read_error=lambda *_: None)
    assert not session.exists()
    assert (moved / path.name).read_bytes() == raw
