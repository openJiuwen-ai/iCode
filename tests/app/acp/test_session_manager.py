# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ACP session listing, cwd scoping, session lifecycle, and host start."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from acp import schema as acp_schema

from chrys.app.acp import session_manager as session_manager_module
from chrys.app.acp.session_manager import AcpSessionError, AcpSessionManager, ManagedSession
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.events.types import ProfileSwitched
from chrys.foundation.util.lock import FileLock
from chrys.kernel import Message
from chrys.service.approval.policy import ApprovalMode
from chrys.service.state.store import ChatSessionMeta, JsonFileStateStore
from chrys.service.state.workflow import WorkflowSessionState
from tests.app.acp._session_manager_fakes import (
    _CloseHost,
    _FailingStartHost,
    _manager,
    _registries,
    _StartedHost,
    _StaticListStore,
    _stdio_mcp,
    _unsupported_sse_mcp,
)
from tests.support.workflow_history import workflow_state


@pytest.mark.asyncio
async def test_workflow_sessions_are_not_listed_or_loaded_as_acp_chat(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_workflow_session("workflow-session", WorkflowSessionState.decode(workflow_state(tmp_path)))
    await store.save_session("chat-session", {}, primary_cwd=str(tmp_path))
    manager = _manager(str(tmp_path), store)

    sessions, next_cursor = await manager.list_sessions(cwd=str(tmp_path), cursor=None)

    assert [session.session_id for session in sessions] == ["chat-session"]
    assert next_cursor is None
    with pytest.raises(AcpSessionError, match="Session not found"):
        await manager.load_session(session_id="workflow-session", cwd=str(tmp_path), mcp_servers=[])


@pytest.mark.asyncio
async def test_list_sessions_filters_to_process_cwd(tmp_path) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "project-session",
        {"messages": [Message("user", ["hello project"])]},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    await store.save_session(
        "other-session",
        {"messages": [Message("user", ["hello other"])]},
        agent_profile="Code",
        primary_cwd=str(other),
    )
    manager = _manager(str(project), store)

    sessions, next_cursor = await manager.list_sessions(cwd=str(project), cursor=None)

    assert next_cursor is None
    assert [session.session_id for session in sessions] == ["project-session"]
    assert sessions[0].cwd == str(project)
    assert sessions[0].field_meta == {
        "agent_profile": "Code",
        "agent_display_name": "",
        "message_count": 1,
        "model_provider": "",
        "model_api_style": "",
        "model_id": "",
    }


@pytest.mark.asyncio
async def test_list_sessions_excludes_primary_cwd_from_additional_directories(tmp_path) -> None:
    project = tmp_path / "project"
    extra = tmp_path / "extra"
    project.mkdir()
    extra.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    # ACP multi-dir sessions persist the primary cwd inside working_dirs.
    await store.save_session(
        "project-session",
        {"messages": [Message("user", ["hello project"])]},
        agent_profile="Code",
        primary_cwd=str(project),
        working_dirs=[str(project), str(extra)],
    )
    manager = _manager(str(project), store)

    sessions, _ = await manager.list_sessions(cwd=str(project), cursor=None)

    assert sessions[0].cwd == str(project)
    # The primary root is reported via cwd, not duplicated in additionalDirectories.
    assert sessions[0].additionalDirectories == [str(extra)]


@pytest.mark.asyncio
async def test_list_sessions_filters_by_additional_directories(tmp_path) -> None:
    project = tmp_path / "project"
    extra_a = tmp_path / "extra-a"
    extra_b = tmp_path / "extra-b"
    for d in (project, extra_a, extra_b):
        d.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "session-a",
        {"messages": [Message("user", ["a"])]},
        agent_profile="Code",
        primary_cwd=str(project),
        working_dirs=[str(project), str(extra_a)],
    )
    await store.save_session(
        "session-b",
        {"messages": [Message("user", ["b"])]},
        agent_profile="Code",
        primary_cwd=str(project),
        working_dirs=[str(project), str(extra_b)],
    )
    await store.save_session(
        "session-plain",
        {"messages": [Message("user", ["plain"])]},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    manager = _manager(str(project), store)

    # additionalDirectories is an exact additional-root filter: asking for [extra-a]
    # returns only the session scoped to exactly that root.
    scoped, _ = await manager.list_sessions(cwd=str(project), cursor=None, additional_directories=[str(extra_a)])
    assert [s.session_id for s in scoped] == ["session-a"]

    # Per the ACP schema, an empty list is equivalent to omitting the filter.
    empty_filter, _ = await manager.list_sessions(cwd=str(project), cursor=None, additional_directories=[])
    assert {s.session_id for s in empty_filter} == {"session-a", "session-b", "session-plain"}

    # Omitting the filter returns all sessions for the cwd (unchanged behavior).
    everything, _ = await manager.list_sessions(cwd=str(project), cursor=None)
    assert {s.session_id for s in everything} == {"session-a", "session-b", "session-plain"}


@pytest.mark.asyncio
async def test_list_sessions_rejects_relative_additional_directory(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    manager = _manager(str(project), store)

    # Filter roots must be absolute (ACP contract); a relative root would otherwise
    # resolve against the server process cwd instead of being rejected.
    with pytest.raises(AcpSessionError, match="absolute"):
        await manager.list_sessions(cwd=str(project), cursor=None, additional_directories=["../extra"])


@pytest.mark.asyncio
async def test_new_session_rejects_relative_cwd(tmp_path) -> None:
    manager = _manager(str(tmp_path), _StaticListStore([]))

    with pytest.raises(AcpSessionError, match="absolute"):
        await manager.new_session(cwd="relative/dir", mcp_servers=None)


@pytest.mark.asyncio
async def test_new_session_rejects_relative_additional_directory(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manager = _manager(str(project), _StaticListStore([]))

    with pytest.raises(AcpSessionError, match="absolute"):
        await manager.new_session(
            cwd=str(project),
            additional_directories=["../sibling"],
            mcp_servers=None,
        )


@pytest.mark.asyncio
async def test_list_sessions_without_cwd_before_dynamic_binding_returns_empty_page(tmp_path) -> None:
    manager = _manager(None, JsonFileStateStore(tmp_path / "sessions"))

    sessions, next_cursor = await manager.list_sessions(cwd=None, cursor=None)

    assert sessions == []
    assert next_cursor is None
    assert manager.process_cwd is None


@pytest.mark.asyncio
async def test_list_sessions_uses_explicit_cwd_without_binding_process_default(tmp_path) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "project-session",
        {"messages": [Message("user", ["hello project"])]},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    await store.save_session(
        "other-session",
        {"messages": [Message("user", ["hello other"])]},
        agent_profile="Code",
        primary_cwd=str(other),
    )
    manager = _manager(None, store)

    sessions, next_cursor = await manager.list_sessions(cwd=str(project), cursor=None)

    assert next_cursor is None
    assert manager.process_cwd is None
    assert [session.session_id for session in sessions] == ["project-session"]
    sessions, next_cursor = await manager.list_sessions(cwd=None, cursor=None)
    assert next_cursor is None
    assert sessions == []
    sessions, next_cursor = await manager.list_sessions(cwd=str(other), cursor=None)
    assert next_cursor is None
    assert [session.session_id for session in sessions] == ["other-session"]


@pytest.mark.asyncio
async def test_list_sessions_sorts_by_updated_at_descending(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    now = datetime(2026, 5, 18, tzinfo=UTC)
    store = _StaticListStore(
        [
            ChatSessionMeta(
                session_id="older",
                agent_profile="Code",
                agent_display_name="",
                created_at=now - timedelta(days=2),
                updated_at=now - timedelta(days=1),
                message_count=1,
                primary_cwd=str(project),
            ),
            ChatSessionMeta(
                session_id="newer",
                agent_profile="Code",
                agent_display_name="",
                created_at=now - timedelta(days=1),
                updated_at=now,
                message_count=1,
                primary_cwd=str(project),
            ),
        ]
    )
    manager = _manager(str(project), store)

    sessions, next_cursor = await manager.list_sessions(cwd=str(project), cursor=None)

    assert next_cursor is None
    assert [session.session_id for session in sessions] == ["newer", "older"]


@pytest.mark.asyncio
async def test_list_sessions_sorts_naive_and_aware_updated_at(tmp_path) -> None:
    """session/list ranks a mix of offset-free and offset-aware stamps without raising."""
    project = tmp_path / "project"
    project.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    for session_id, stamp in (("naive", "2026-09-02T09:30:00"), ("aware", "2026-09-03T09:30:00+00:00")):
        await store.save_session(session_id, {"messages": [Message("user", ["hi"])]}, primary_cwd=str(project))
        path = store.session_dir(session_id) / "session.json"
        envelope = json.loads(path.read_text(encoding="utf-8"))
        envelope["meta"]["created_at"] = stamp
        envelope["meta"]["updated_at"] = stamp
        path.write_text(json.dumps(envelope), encoding="utf-8")
    manager = _manager(str(project), store)

    sessions, next_cursor = await manager.list_sessions(cwd=str(project), cursor=None)

    assert next_cursor is None
    assert [session.session_id for session in sessions] == ["aware", "naive"]
    assert sessions[1].updatedAt == "2026-09-02T09:30:00+00:00"


@pytest.mark.asyncio
async def test_list_sessions_accepts_explicit_cwd_different_from_default(tmp_path) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "other-session",
        {"messages": [Message("user", ["hello other"])]},
        agent_profile="Code",
        primary_cwd=str(other),
    )
    manager = _manager(str(project), store)

    sessions, next_cursor = await manager.list_sessions(cwd=str(other), cursor=None)

    assert next_cursor is None
    assert [session.session_id for session in sessions] == ["other-session"]


@pytest.mark.asyncio
async def test_load_session_rejects_saved_session_from_other_cwd(tmp_path) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "other-session",
        {"messages": [Message("user", ["hello other"])]},
        agent_profile="Code",
        primary_cwd=str(other),
    )
    manager = _manager(str(project), store)

    with pytest.raises(AcpSessionError, match="belongs to"):
        await manager.load_session(cwd=str(project), session_id="other-session", mcp_servers=None)


@pytest.mark.asyncio
async def test_failed_load_does_not_bind_unbound_process_cwd(tmp_path) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "other-session",
        {"messages": [Message("user", ["hello other"])]},
        agent_profile="Code",
        primary_cwd=str(other),
    )
    manager = _manager(None, store)

    with pytest.raises(AcpSessionError, match="belongs to"):
        await manager.load_session(cwd=str(project), session_id="other-session", mcp_servers=None)

    assert manager.process_cwd is None


@pytest.mark.asyncio
async def test_load_session_rejects_saved_session_without_cwd_metadata(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "legacy-session",
        {"messages": [Message("user", ["hello legacy"])]},
        agent_profile="Code",
    )
    manager = _manager(str(project), store)

    with pytest.raises(AcpSessionError, match="no saved cwd metadata"):
        await manager.load_session(cwd=str(project), session_id="legacy-session", mcp_servers=None)


@pytest.mark.asyncio
async def test_load_session_returns_existing_active_session(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "project-session",
        {"messages": [Message("user", ["hello project"])]},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    manager = _manager(str(project), store)
    existing_host = _CloseHost()
    existing = ManagedSession(  # type: ignore[arg-type]
        session_id="project-session",
        cwd=str(project),
        profile_name="Code",
        host=existing_host,
    )
    manager._sessions["project-session"] = existing  # type: ignore[assignment]
    _FailingStartHost.instances = []
    monkeypatch.setattr(session_manager_module, "ChrysSessionHost", _FailingStartHost)

    loaded = await manager.load_session(cwd=str(project), session_id="project-session", mcp_servers=None)

    assert loaded.session is existing
    assert loaded.reused_existing is True
    assert _FailingStartHost.instances == []


@pytest.mark.asyncio
async def test_load_session_checks_active_sessions_before_persisted_list(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await asyncio.to_thread(
        store.save_recovery_session,
        "project-session",
        {"messages": [Message("user", ["recovered"])], "compressed_msgs": []},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    active_lock = FileLock(store.active_lock_path("project-session"), timeout=1.0)
    active_lock.acquire()
    manager = _manager(str(project), store)
    existing_host = _CloseHost()
    existing_host.engine.recovered_from_sidecar = True
    existing = ManagedSession(  # type: ignore[arg-type]
        session_id="project-session",
        cwd=str(project),
        profile_name="Code",
        host=existing_host,
    )
    manager._sessions["project-session"] = existing  # type: ignore[assignment]
    _FailingStartHost.instances = []
    monkeypatch.setattr(session_manager_module, "ChrysSessionHost", _FailingStartHost)

    try:
        loaded = await manager.load_session(cwd=str(project), session_id="project-session", mcp_servers=None)
    finally:
        active_lock.release()

    assert loaded.session is existing
    assert loaded.reused_existing is True
    assert loaded.recovered_from_sidecar is True
    assert _FailingStartHost.instances == []


@pytest.mark.asyncio
async def test_load_session_tracks_profile_resolved_by_restore(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "project-session",
        {"messages": [Message("user", ["hello project"])]},
        agent_profile="OldName",
        agent_profile_id="stable-id",
        primary_cwd=str(project),
    )
    manager = _manager(str(project), store)
    _StartedHost.instances = []

    class _RenamedProfileHost(_StartedHost):
        async def start(self) -> None:
            self.engine.snapshot = ProfileSwitched(to_profile="Renamed")

    monkeypatch.setattr(session_manager_module, "ChrysSessionHost", _RenamedProfileHost)

    loaded = await manager.load_session(cwd=str(project), session_id="project-session", mcp_servers=None)

    assert loaded.session.profile_name == "Renamed"


@pytest.mark.asyncio
async def test_session_history_uses_active_recovery_source(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await asyncio.to_thread(
        store.save_recovery_session,
        "project-session",
        {"messages": [Message("user", ["recovered"])], "compressed_msgs": []},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    active_lock = FileLock(store.active_lock_path("project-session"), timeout=1.0)
    active_lock.acquire()
    manager = _manager(str(project), store)
    existing_host = _CloseHost()
    existing_host.engine.recovered_from_sidecar = True
    manager._sessions["project-session"] = ManagedSession(  # type: ignore[assignment]
        session_id="project-session",
        cwd=str(project),
        profile_name="Code",
        host=existing_host,
    )

    try:
        canonical_id, transcript = await manager.session_history(cwd=str(project), session_id="project-session")
    finally:
        active_lock.release()

    assert canonical_id == "project-session"
    assert transcript is not None
    assert transcript.messages[0]["contents"][0]["text"] == "recovered"


@pytest.mark.asyncio
async def test_session_history_uses_inactive_recovery_source_when_it_wins(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "project-session",
        {"messages": [Message("user", ["primary"])], "compressed_msgs": []},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    await asyncio.to_thread(
        store.save_recovery_session,
        "project-session",
        {"messages": [Message("user", ["recovery"])], "compressed_msgs": []},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    manager = _manager(str(project), store)

    canonical_id, transcript = await manager.session_history(cwd=str(project), session_id="project-session")

    assert canonical_id == "project-session"
    assert transcript is not None
    assert transcript.messages[0]["contents"][0]["text"] == "recovery"


@pytest.mark.asyncio
async def test_session_history_ignores_external_active_recovery_sidecar(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "project-session",
        {"messages": [Message("user", ["primary"])], "compressed_msgs": []},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    await asyncio.to_thread(
        store.save_recovery_session,
        "project-session",
        {"messages": [Message("user", ["recovery"])], "compressed_msgs": []},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    active_lock = FileLock(store.active_lock_path("project-session"), timeout=1.0)
    active_lock.acquire()
    manager = _manager(str(project), store)

    try:
        canonical_id, transcript = await manager.session_history(cwd=str(project), session_id="project-session")
    finally:
        active_lock.release()

    assert canonical_id == "project-session"
    assert transcript is not None
    assert transcript.messages[0]["contents"][0]["text"] == "primary"


@pytest.mark.asyncio
async def test_begin_delete_session_canonicalizes_short_ids_and_scopes_by_cwd(tmp_path) -> None:
    from chrys.foundation.util.session_ids import session_short_id

    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "saved-session",
        {"messages": [Message("user", ["hello"])]},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    manager = _manager(str(project), store)

    canonical = await manager.begin_delete_session(cwd=str(project), session_id="saved-session")
    from_short = await manager.begin_delete_session(cwd=str(project), session_id=session_short_id("saved-session"))

    assert canonical == "saved-session"
    assert from_short == "saved-session"
    with pytest.raises(AcpSessionError):
        await manager.begin_delete_session(cwd=str(other), session_id="saved-session")
    with pytest.raises(AcpSessionError):
        await manager.begin_delete_session(cwd=str(project), session_id="missing-session")


@pytest.mark.asyncio
async def test_begin_delete_session_marks_an_active_target_closing_but_not_on_rejection(tmp_path) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "saved-session",
        {"messages": [Message("user", ["hello"])]},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    manager = _manager(str(project), store)
    host = _CloseHost()
    managed = ManagedSession(  # type: ignore[arg-type]
        session_id="saved-session",
        cwd=str(project),
        profile_name="Code",
        host=host,
    )
    manager._sessions["saved-session"] = managed

    with pytest.raises(AcpSessionError):
        await manager.begin_delete_session(cwd=str(other), session_id="saved-session")
    assert managed.closing is False

    canonical = await manager.begin_delete_session(cwd=str(project), session_id="saved-session")

    assert canonical == "saved-session"
    assert managed.closing is True
    with pytest.raises(AcpSessionError, match="not active"):
        manager.get("saved-session")

    await manager.finish_delete_session(canonical)
    assert host.shutdown_called is True
    assert "saved-session" not in manager._sessions
    assert all(meta.session_id != "saved-session" for meta in await store.list_sessions())


@pytest.mark.asyncio
async def test_load_session_rejects_a_closing_session_instead_of_reusing_or_reloading_it(tmp_path, monkeypatch) -> None:
    """A session marked closing stays in the map while close/delete drain waits.

    Handing it out as ``reused_existing`` would report success for a session
    about to shut down, and falling through to a fresh load would overwrite
    the map entry the in-flight teardown is about to pop — so load must
    reject, and must not start a replacement host.
    """
    from chrys.foundation.util.session_ids import session_short_id

    project = tmp_path / "project"
    project.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "project-session",
        {"messages": [Message("user", ["hello project"])]},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    manager = _manager(str(project), store)
    existing = ManagedSession(  # type: ignore[arg-type]
        session_id="project-session",
        cwd=str(project),
        profile_name="Code",
        host=_CloseHost(),
    )
    manager._sessions["project-session"] = existing  # type: ignore[assignment]
    await manager.begin_close("project-session")
    _FailingStartHost.instances = []
    monkeypatch.setattr(session_manager_module, "ChrysSessionHost", _FailingStartHost)

    with pytest.raises(AcpSessionError, match="closing"):
        await manager.load_session(cwd=str(project), session_id="project-session", mcp_servers=None)
    with pytest.raises(AcpSessionError, match="closing"):
        await manager.load_session(
            cwd=str(project),
            session_id=session_short_id("project-session"),
            mcp_servers=None,
        )
    with pytest.raises(AcpSessionError, match="closing"):
        await manager.session_history(cwd=str(project), session_id="project-session")

    assert _FailingStartHost.instances == []
    assert manager._sessions["project-session"] is existing


@pytest.mark.asyncio
async def test_close_waits_for_session_prompt_lock(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manager = _manager(str(project), JsonFileStateStore(tmp_path / "sessions"))
    host = _CloseHost()
    managed = ManagedSession(  # type: ignore[assignment]
        session_id="s1",
        cwd=str(project),
        profile_name="Code",
        host=host,
    )
    manager._sessions["s1"] = managed

    async with managed.prompt_lock:
        task = asyncio.create_task(manager.close("s1"))
        await asyncio.sleep(0)
        assert not task.done()
        assert host.shutdown_called is False
        with pytest.raises(AcpSessionError, match="not active"):
            manager.get("s1")

    await asyncio.wait_for(task, timeout=1)
    assert host.shutdown_called is True


@pytest.mark.asyncio
async def test_delete_session_removes_saved_session_and_shuts_down_active_host(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "project-session",
        {"messages": [Message("user", ["hello project"])]},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    manager = _manager(str(project), store)
    host = _CloseHost()
    manager._sessions["project-session"] = ManagedSession(  # type: ignore[assignment]
        session_id="project-session",
        cwd=str(project),
        profile_name="Code",
        host=host,
    )

    await manager.delete_session(cwd=str(project), session_id="project-session")

    assert host.shutdown_called is True
    assert "project-session" not in manager._sessions
    remaining = await store.list_sessions()
    assert [session.session_id for session in remaining] == []


@pytest.mark.asyncio
async def test_new_session_wires_per_session_title_updater(tmp_path, monkeypatch) -> None:
    """Each ACP session gets its own auto-title updater whose turn callbacks
    are composed with the process-level successful-turn callback, and close()
    drains it after the host."""
    project = tmp_path / "project"
    project.mkdir()
    agent_registry, model_registry = _registries()
    process_turns: list[bool] = []
    manager = AcpSessionManager(
        loaded_settings=LoadedSettings(settings=Settings(), provenance={}),
        profile_name="Code",
        approval_mode=ApprovalMode.MANUAL,
        process_cwd=None,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
        agent_registry=agent_registry,
        model_registry=model_registry,
        on_successful_turn=lambda: process_turns.append(True),
    )
    _StartedHost.instances = []
    monkeypatch.setattr(session_manager_module, "ChrysSessionHost", _StartedHost)

    managed = await manager.new_session(cwd=str(project), mcp_servers=None)
    updater = managed.title_updater
    assert updater is not None

    host = _StartedHost.instances[0]
    finished_turns: list[bool] = []
    started_turns: list[bool] = []
    monkeypatch.setattr(updater, "on_turn_finished", lambda: finished_turns.append(True))
    monkeypatch.setattr(updater, "on_turn_started", lambda: started_turns.append(True))
    host.kwargs["on_successful_turn"]()
    host.kwargs["on_turn_started"]()
    assert process_turns == [True]
    assert finished_turns == [True]
    assert started_turns == [True]

    await manager.close(managed.session_id)
    assert host.shutdown_called is True
    assert updater._closed is True


@pytest.mark.parametrize(
    ("prebind_process_cwd", "resume_saved_session"),
    [
        pytest.param(False, False, id="new-unbound"),
        pytest.param(True, False, id="new-prebound"),
        pytest.param(False, True, id="load-unbound"),
        pytest.param(True, True, id="load-prebound"),
    ],
)
async def test_session_start_failure_shuts_down_host_and_leaves_cwd_binding_intact(
    tmp_path, monkeypatch, prebind_process_cwd: bool, resume_saved_session: bool
) -> None:
    """A host whose start() raises is shut down, the process cwd is restored, and
    the manager's own cwd binding is left exactly as the attempt found it."""
    project = tmp_path / "project"
    project.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    if resume_saved_session:
        await store.save_session(
            "project-session",
            {"messages": [Message("user", ["hello project"])]},
            agent_profile="Code",
            primary_cwd=str(project),
        )
    manager = _manager(str(project) if prebind_process_cwd else None, store)
    _FailingStartHost.instances = []
    monkeypatch.setattr(session_manager_module, "ChrysSessionHost", _FailingStartHost)
    original_cwd = os.getcwd()

    try:
        with pytest.raises(RuntimeError, match="boom"):
            if resume_saved_session:
                await manager.load_session(cwd=str(project), session_id="project-session", mcp_servers=None)
            else:
                await manager.new_session(cwd=str(project), mcp_servers=None)
        assert os.getcwd() == original_cwd
        assert manager.process_cwd == (str(project) if prebind_process_cwd else None)
    finally:
        os.chdir(original_cwd)

    assert len(_FailingStartHost.instances) == 1
    assert _FailingStartHost.instances[0].shutdown_called is True


@pytest.mark.asyncio
async def test_new_session_allows_multiple_cwds_in_one_manager(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    other = tmp_path / "other"
    project.mkdir()
    other.mkdir()
    manager = _manager(None, JsonFileStateStore(tmp_path / "sessions"))
    _StartedHost.instances = []
    monkeypatch.setattr(session_manager_module, "ChrysSessionHost", _StartedHost)
    original_cwd = os.getcwd()

    try:
        first = await manager.new_session(cwd=str(project), mcp_servers=None)
        second = await manager.new_session(cwd=str(other), mcp_servers=None)
    finally:
        await manager.shutdown()
        os.chdir(original_cwd)

    assert manager.process_cwd is None
    assert first.cwd == str(project)
    assert second.cwd == str(other)
    assert len(_StartedHost.instances) == 2
    assert all(instance.shutdown_called for instance in _StartedHost.instances)


@pytest.mark.asyncio
async def test_new_session_uses_process_cwd_as_default_when_request_omits_cwd(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manager = _manager(str(project), JsonFileStateStore(tmp_path / "sessions"))
    _StartedHost.instances = []
    monkeypatch.setattr(session_manager_module, "ChrysSessionHost", _StartedHost)

    try:
        session = await manager.new_session(cwd=None, mcp_servers=None)
    finally:
        await manager.shutdown()

    assert session.cwd == str(project)
    assert manager.process_cwd == str(project)


@pytest.mark.asyncio
async def test_new_session_profile_not_found_does_not_bind_cwd(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manager = _manager(None, JsonFileStateStore(tmp_path / "sessions"), profile_name="Bogus")

    with pytest.raises(AcpSessionError, match="Agent profile not found"):
        await manager.new_session(cwd=str(project), mcp_servers=None)

    assert manager.process_cwd is None


@pytest.mark.asyncio
async def test_new_session_unsupported_mcp_overlay_does_not_bind_cwd(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manager = _manager(None, JsonFileStateStore(tmp_path / "sessions"))

    with pytest.raises(AcpSessionError, match="SSE MCP servers are not supported"):
        await manager.new_session(cwd=str(project), mcp_servers=_unsupported_sse_mcp())

    assert manager.process_cwd is None


@pytest.mark.asyncio
async def test_load_session_unsupported_mcp_overlay_does_not_bind_cwd(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "project-session",
        {"messages": [Message("user", ["hello project"])]},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    manager = _manager(None, store)

    with pytest.raises(AcpSessionError, match="SSE MCP servers are not supported"):
        await manager.load_session(cwd=str(project), session_id="project-session", mcp_servers=_unsupported_sse_mcp())

    assert manager.process_cwd is None


def test_acp_http_mcp_overlay_uses_literal_headers() -> None:
    configs = session_manager_module._mcp_overlay(
        [
            acp_schema.HttpMcpServer(
                type="http",
                name="remote",
                url="https://example.test/mcp",
                headers=[acp_schema.HttpHeader(name="Authorization", value="Bearer {{OPENAI_API_KEY}}")],
            )
        ]
    )

    assert configs[0].headers == {"Authorization": "Bearer {{OPENAI_API_KEY}}"}
    assert configs[0].resolve_header_templates is False


def test_acp_stdio_mcp_overlay_builds_stdio_config() -> None:
    configs = session_manager_module._mcp_overlay(
        [
            acp_schema.McpServerStdio(
                type="stdio",
                name="local",
                command="/usr/bin/python3",
                args=["-m", "local_server"],
                env=[acp_schema.EnvVariable(name="TOKEN", value="literal-{{NOT_A_PLACEHOLDER}}")],
            )
        ]
    )

    assert len(configs) == 1
    config = configs[0]
    assert config.name == "local"
    assert config.transport == "stdio"
    assert config.command == "/usr/bin/python3"
    assert config.args == ["-m", "local_server"]
    assert config.env == {"TOKEN": "literal-{{NOT_A_PLACEHOLDER}}"}
    # ACP has no template syntax, so the client's values reach the child as written.
    assert config.resolve_env_templates is False


@pytest.mark.asyncio
async def test_new_session_passes_stdio_mcp_overlay_to_host(tmp_path, monkeypatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manager = _manager(None, JsonFileStateStore(tmp_path / "sessions"))
    _StartedHost.instances.clear()
    monkeypatch.setattr(session_manager_module, "ChrysSessionHost", _StartedHost)

    try:
        await manager.new_session(cwd=str(project), mcp_servers=_stdio_mcp())
    finally:
        await manager.shutdown()

    overlay = _StartedHost.instances[-1].kwargs["mcp_overlay"]
    assert [config.transport for config in overlay] == ["stdio"]
    assert overlay[0].command == "/usr/bin/python3"


def test_mcp_test_config_accepts_client_supplied_stdio() -> None:
    config = session_manager_module._mcp_config_from_data(
        {
            "name": "local",
            "transport": "stdio",
            "command": "local-server",
            "args": ["--stdio"],
            "env": {"TOKEN": "literal-{{NOT_A_PLACEHOLDER}}"},
        }
    )

    assert config.transport == "stdio"
    assert config.command == "local-server"
    assert config.args == ["--stdio"]
    assert config.env == {"TOKEN": "literal-{{NOT_A_PLACEHOLDER}}"}
    assert config.resolve_env_templates is False


def test_mcp_test_http_config_keeps_headers_literal() -> None:
    config = session_manager_module._mcp_config_from_data(
        {
            "name": "remote",
            "transport": "http",
            "url": "https://example.test/mcp",
            "headers": {"Authorization": "Bearer {{OPENAI_API_KEY}}"},
        }
    )

    assert config.transport == "http"
    assert config.headers == {"Authorization": "Bearer {{OPENAI_API_KEY}}"}
    assert config.resolve_header_templates is False


@pytest.mark.asyncio
async def test_load_session_rejects_additional_directories_for_active_session(tmp_path) -> None:
    project = tmp_path / "project"
    extra = tmp_path / "extra"
    project.mkdir()
    extra.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "project-session",
        {"messages": [Message("user", ["hello project"])]},
        agent_profile="Code",
        primary_cwd=str(project),
    )
    manager = _manager(str(project), store)
    manager._sessions["project-session"] = ManagedSession(  # type: ignore[assignment]
        session_id="project-session",
        cwd=str(project),
        profile_name="Code",
        host=_CloseHost(),
    )

    with pytest.raises(AcpSessionError, match="additional directories"):
        await manager.load_session(
            cwd=str(project),
            session_id="project-session",
            additional_directories=[str(extra)],
            mcp_servers=None,
        )


def test_session_info_prefers_title_overlays() -> None:
    """ACP session listings must surface custom/generated titles, not just the first message."""
    from datetime import UTC, datetime

    from chrys.service.state.store import ChatSessionMeta

    now = datetime.now(UTC)
    meta = ChatSessionMeta(
        session_id="sess1",
        agent_profile="code",
        agent_display_name="Code",
        created_at=now,
        updated_at=now,
        message_count=1,
        title="fix the login bug",
        generated_title="Login bug fix",
    )
    info = session_manager_module._session_info(meta)
    assert info.title == "Login bug fix"

    meta.custom_title = "My session"
    assert session_manager_module._session_info(meta).title == "My session"
