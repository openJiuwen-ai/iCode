# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Active session paths, bookkeeping, and lock ownership."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, create_autospec
from uuid import UUID

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.util.session_ids import session_short_id
from chrys.orchestration.engine.build.construction import StagedBuild
from chrys.orchestration.engine.state.active_session import ActiveSession
from chrys.service.approval.policy import ApprovalMode
from chrys.service.hooks.manager import HookManager
from chrys.service.mutations.coordination import MutationCoordinator
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.session.persistence import SessionPersistence
from chrys.service.session.runtime_metadata import SessionRuntimeMetadata
from chrys.service.state.locks import ActiveSessionGuard
from chrys.service.state.store import JsonFileStateStore, StateStore, session_write_lock_path
from chrys.service.todos.tracker import TodoTracker
from tests.support.components import make_session, plan_workspace


@pytest.mark.parametrize("store_kind", ["json", "custom", "none"])
def test_session_paths_follow_the_configured_store(tmp_path, monkeypatch, store_kind):
    session_id = "abcdef12-3456-7890-abcd-123456789012"
    captured_id = "12345678-3456-7890-abcd-123456789012"
    root = tmp_path / store_kind
    if store_kind == "json":
        store = JsonFileStateStore(root)
        directory = root / session_short_id(session_id)
        captured_directory = root / session_short_id(captured_id)
    elif store_kind == "custom":
        store = create_autospec(StateStore, instance=True)
        root = root / "custom"
        store.session_dir.side_effect = lambda value: root / value
        directory = root / session_id
        captured_directory = root / captured_id
    else:
        store = None
        monkeypatch.setattr("chrys.foundation.config.settings.resolve_sessions_dir", lambda: root)
        directory = root / session_short_id(session_id)
        captured_directory = root / session_short_id(captured_id)
    session = make_session(persistence=SessionPersistence(store, EventBus()), workspace=None, approval_mode=None)

    assert session.session_dir is None
    session.session_id = ""
    assert session.session_dir is None
    session.session_id = session_id
    assert session.session_dir == directory
    assert session.session_dir_for(captured_id) == captured_directory
    assert session.sessions_root_dir(captured_id) == root
    lock_path = session.session_write_lock_path(captured_id)
    assert lock_path == session_write_lock_path(root, captured_id)
    assert lock_path.parent.is_dir()
    assert session.session_id == session_id


def test_workspace_cwd_uses_the_current_workspace_or_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("chrys.foundation.platform.safe_getcwd", lambda: str(tmp_path))
    session = make_session(persistence=SessionPersistence(None, EventBus()), workspace=None, approval_mode=None)
    assert session.workspace_cwd() == str(tmp_path)
    workspace = Workspace(primary_cwd=str(tmp_path / "workspace"))
    session.install_workspace(plan_workspace(workspace))
    assert session.workspace_cwd() == workspace.primary_cwd


def test_reset_spill_quota_replaces_only_the_ledger():
    session = make_session(persistence=SessionPersistence(None, EventBus()), workspace=None, approval_mode=None)
    session.session_id = "session"
    session.turn_number = 7
    original = session.spill_quota
    assert original.try_reserve(12)
    original.commit(12, 12)
    assert original.spent_bytes == 12
    runtime_meta = session.runtime_meta

    session.reset_spill_quota()

    assert session.spill_quota is not original
    assert session.spill_quota.spent_bytes == 0
    assert original.spent_bytes == 12
    assert session.session_id == "session"
    assert session.turn_number == 7
    assert session.runtime_meta is runtime_meta


def test_guard_ensure_install_and_release(tmp_path):
    store = JsonFileStateStore(tmp_path)
    session = make_session(persistence=SessionPersistence(store, EventBus()), workspace=None, approval_mode=None)
    guard = session.guard
    contender = ActiveSessionGuard(store)
    assert not guard.owns("first")
    try:
        assert guard.ensure("first")
        assert guard.owns("first")
        assert guard.ensure("first")
        with pytest.raises(TimeoutError):
            contender.acquire_for_restore("first", timeout=0)

        second = guard.acquire_for_restore("second", timeout=0)
        guard.install("second", second)
        assert guard.owns("second")
        assert not guard.owns("first")
        contender.install("first", contender.acquire_for_restore("first", timeout=0))
        assert contender.owns("first")

        guard.release()
        assert not guard.owns("second")
        guard.release()
        contender.install("second", contender.acquire_for_restore("second", timeout=0))
        assert contender.owns("second")
    finally:
        guard.release()
        contender.release()


def test_guard_without_store_does_not_claim_a_lock():
    session = make_session(persistence=SessionPersistence(None, EventBus()), workspace=None, approval_mode=None)
    assert session.guard.ensure("session")
    assert not session.guard.owns("session")
    session.guard.install("session", None)
    session.guard.release()
    assert not session.guard.owns("session")


def test_pins_and_approval_are_owned_by_each_session():
    persistence = SessionPersistence(None, EventBus())
    session = make_session(persistence=persistence, workspace=None, approval_mode=ApprovalMode.AUTO)
    other = make_session(persistence=persistence, workspace=None, approval_mode=None)
    session.model_profile_pinned = True
    session.ask_user_timeout_pinned = True
    assert session.model_profile_pinned
    assert session.ask_user_timeout_pinned
    assert session.approval_mode is ApprovalMode.AUTO
    assert not other.model_profile_pinned
    assert not other.ask_user_timeout_pinned
    assert other.approval_mode is ApprovalMode.MANUAL
    assert session.spill_quota is not other.spill_quota
    assert session.runtime_meta is not other.runtime_meta
    assert session.guard is not other.guard


def _session() -> ActiveSession:
    return make_session(persistence=SessionPersistence(None, EventBus()), workspace=None, approval_mode=None)


@pytest.mark.parametrize("has_identity", [False, True])
@pytest.mark.parametrize("workspace_kind", ["default", "live", "candidate"])
def test_begin_preserves_live_state_and_initializes_missing_identity(tmp_path, has_identity, workspace_kind):
    session = _session()
    profile = AgentProfile(name="Code")
    live = Workspace(primary_cwd=str(tmp_path / "live")) if workspace_kind == "live" else None
    if live is not None:
        session.install_workspace(plan_workspace(live))
    session.session_id = "existing" if has_identity else None
    session.session_end_fired = True
    before = vars(session).copy()

    session.begin(agent_profile=profile)

    assert session.session_id == "existing" if has_identity else UUID(session.session_id).version == 4
    assert session.workspace is live
    assert vars(session) == before | {
        "session_end_fired": False,
        "agent_profile": profile,
        "session_id": session.session_id,
    }


@pytest.mark.parametrize("install_trackers", [False, True])
def test_install_build_changes_only_completed_session_fields(install_trackers):
    session = _session()
    session.mutation_tracker = create_autospec(MutationTracker, instance=True)
    session.todo_tracker = create_autospec(TodoTracker, instance=True)
    staged = create_autospec(StagedBuild, instance=True)
    staged.agent_profile = AgentProfile(name="Other")
    staged.workspace_plan = plan_workspace(Workspace(primary_cwd="/workspace"))
    staged.hook_manager = create_autospec(HookManager, instance=True)
    staged.mutation_coordinator = create_autospec(MutationCoordinator, instance=True)
    mutation_tracker = create_autospec(MutationTracker, instance=True) if install_trackers else None
    todo_tracker = create_autospec(TodoTracker, instance=True) if install_trackers else None
    before = vars(session).copy()

    session.install_build(staged, mutation_tracker=mutation_tracker, todo_tracker=todo_tracker)

    assert vars(session) == before | {
        "agent_profile": staged.agent_profile,
        "workspace_plan": staged.workspace_plan,
        "hook_manager": staged.hook_manager,
        "mutation_coordinator": staged.mutation_coordinator,
        "mutation_tracker": mutation_tracker if install_trackers else before["mutation_tracker"],
        "todo_tracker": todo_tracker if install_trackers else before["todo_tracker"],
    }


@pytest.mark.parametrize("with_workspace", [False, True])
def test_reset_replaces_only_per_session_fields(tmp_path, with_workspace):
    session = _session()
    session.shutting_down = True
    session.recovered_from_sidecar = True
    session.turn_number = 12
    session.mutation_tracker = create_autospec(MutationTracker, instance=True)
    session.mutation_coordinator = create_autospec(MutationCoordinator, instance=True)
    session.todo_tracker = create_autospec(TodoTracker, instance=True)
    workspace = Workspace(primary_cwd=str(tmp_path)) if with_workspace else None
    if workspace is not None:
        session.install_workspace(plan_workspace(workspace))
    before = vars(session).copy()

    session.reset(session_id="reset")

    assert session.workspace is workspace
    assert session.spill_quota is not before["spill_quota"]
    assert session.runtime_meta is not before["runtime_meta"]
    assert vars(session) == before | {
        "shutting_down": False,
        "session_id": "reset",
        "turn_number": 0,
        "spill_quota": session.spill_quota,
        "runtime_meta": session.runtime_meta,
        "mutation_tracker": None,
        "mutation_coordinator": None,
        "todo_tracker": None,
        "recovered_from_sidecar": False,
    }


def test_restore_operations_keep_their_distinct_write_boundaries():
    session = _session()
    session.shutting_down = True
    session.turn_number = 12
    before = vars(session).copy()

    session.adopt_restore_identity(session_id="restored", recovered_from_sidecar=True)

    assert session.spill_quota is not before["spill_quota"]
    assert vars(session) == before | {
        "shutting_down": False,
        "session_id": "restored",
        "recovered_from_sidecar": True,
        "spill_quota": session.spill_quota,
    }
    before = vars(session).copy()
    metadata = SessionRuntimeMetadata(
        total_session_tokens=23,
        total_session_input_tokens=17,
        total_session_output_tokens=6,
        last_usage_details={"total_token_count": 23},
    )
    session.restore_position(runtime_meta=metadata, turn_number=4)
    assert session.runtime_meta is metadata
    assert session.turn_number == 4
    assert vars(session) == before | {"runtime_meta": metadata, "turn_number": 4}


def test_identity_markers_and_delete_operations_change_only_their_fields():
    session = _session()
    before = vars(session).copy()
    session.mark_closing()
    assert vars(session) == before | {"shutting_down": True}
    before = vars(session).copy()
    session.mark_session_end_fired()
    assert vars(session) == before | {"session_end_fired": True}
    for recovered in (True, False):
        before = vars(session).copy()
        session.mark_recovered_from_sidecar(recovered)
        assert vars(session) == before | {"recovered_from_sidecar": recovered}
    session.session_id = "live"
    before = vars(session).copy()
    assert session.detach_for_delete() == "live"
    assert vars(session) == before | {"session_id": None}
    assert session.detach_for_delete() is None
    before = vars(session).copy()
    session.reattach_after_failed_delete("live")
    assert vars(session) == before | {"session_id": "live", "session_end_fired": False}


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize("failure", [None, ValueError, asyncio.CancelledError])
def test_saves_suppressed_restores_nested_scopes_on_all_exits(initial, failure):
    session = _session()
    session.suppress_save = initial
    before = vars(session).copy()

    def scoped():
        with session.saves_suppressed():
            assert session.suppress_save is True
            with session.saves_suppressed():
                assert session.suppress_save is True
            assert session.suppress_save is True
            if failure is not None:
                raise failure

    if failure is None:
        scoped()
    else:
        with pytest.raises(failure):
            scoped()
    assert session.suppress_save is initial
    assert vars(session) == before


@pytest.mark.parametrize("failure", [TimeoutError, RuntimeError, asyncio.CancelledError])
async def test_failed_delete_reattaches_only_after_ordinary_failure(
    tmp_path, monkeypatch, agent_engine, failure, *, engine_services
):
    engine = agent_engine(EventBus(), settings=Settings(), state_store=JsonFileStateStore(tmp_path))
    engine.session.adopt_restore_identity(session_id="live", recovered_from_sidecar=False)
    engine.session.mark_session_end_fired()
    reattach = create_autospec(
        engine.session.reattach_after_failed_delete, side_effect=engine.session.reattach_after_failed_delete
    )
    monkeypatch.setattr(engine.session, "reattach_after_failed_delete", reattach)
    monkeypatch.setattr(engine.session.guard, "owns", lambda session_id: True)
    monkeypatch.setattr(engine.lifecycle, "fire_session_end_hooks", AsyncMock())
    monkeypatch.setattr(engine.lifecycle, "close_trajectory_log", AsyncMock())
    monkeypatch.setattr(engine_services(engine).persistence, "delete_session", AsyncMock(side_effect=failure("failed")))

    if failure is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await engine.lifecycle._delete_session_reporting("live")
        reattach.assert_not_called()
        assert engine.session.session_id is None
        assert engine.session.session_end_fired is True
    else:
        result = await engine.lifecycle._delete_session_reporting("live")
        assert result is not None
        assert result.code == ("session_busy" if failure is TimeoutError else "session_delete_failed")
        reattach.assert_called_once_with("live")
        assert engine.session.session_id == "live"
        assert engine.session.session_end_fired is False
