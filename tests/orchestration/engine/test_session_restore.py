# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session restore and new-session transitions: sidecars, profile resolution, transition fences, and the failed-startup reset."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, create_autospec

import pytest

from chrys.foundation.config.settings import (
    Settings,
)
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    Error,
    SessionNew,
    SessionRestore,
    SessionRestored,
    SettingsReload,
    Warning,
)
from chrys.foundation.i18n import DisplayPath
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.util.session_ids import session_short_id
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.service.approval.policy import ApprovalMode
from chrys.service.mutations import workspace_changes
from chrys.service.profiles.agents.schema import (
    AgentProfile,
)
from chrys.service.state.locks import ActiveSessionGuard
from chrys.service.state.store import SESSION_RECOVERY_FILE_NAME, JsonFileStateStore
from tests.orchestration.engine._recovery_helpers import (
    _profile,
    _registry,
    _seed_recovery_sidecar,
    _seed_restorable_session,
    stub_engine_lifecycle,
    stub_engine_shutdown,
)
from tests.support.event_capture import assert_display_message, collect_events


async def test_restore_loads_winning_recovery_sidecar_after_acquiring_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, text="primary", agent_profile=profile.name)
    await _seed_recovery_sidecar(store, agent_profile=profile.name)
    events: list[SessionRestored] = []
    bus = EventBus()
    await bus.subscribe(SessionRestored, lambda event: collect_events(events, event))
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store, agent_registry=_registry(profile))

    stub_engine_lifecycle(monkeypatch, engine)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me"))
    finally:
        engine.session.guard.release()

    assert engine.session.turn_number == 9
    assert engine.recovered_from_sidecar is True
    assert len(events) == 1
    assert events[0].recovered_from_sidecar is True


async def test_restore_snapshots_settings_inside_the_transition_fence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A settings reload racing the restore must not be silently clobbered:
    the restore takes the transition boundary (shared with the rebuild gate)
    before it snapshots and derives, so a concurrent reload either lands
    before the snapshot or waits and is *audibly denied* — never committed
    and then overwritten by a staged load routed against a stale copy."""
    profile = _profile()
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, agent_profile=profile.name)
    original_settings = Settings()
    restore_settings = Settings(default_approval_mode="manual")
    replacement_settings = Settings(default_approval_mode="auto")
    errors: list[Error] = []
    bus = EventBus()
    await bus.subscribe(Error, lambda event: collect_events(errors, event))
    engine = assemble_agent_engine(
        bus, settings=original_settings, state_store=store, agent_registry=_registry(profile)
    )
    engine.session.agent_profile = profile

    loop = asyncio.get_running_loop()
    release_restore_load = threading.Event()
    restore_load_entered = asyncio.Event()

    def hanging_restore_load(**kwargs: Any) -> LoadedSettings:
        loop.call_soon_threadsafe(restore_load_entered.set)
        release_restore_load.wait(5)
        return LoadedSettings(settings=restore_settings, provenance={})

    async def fake_shutdown(*, keep_side_answers: bool = False) -> None:
        pass

    async def fake_start(
        start_profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        _ = start_profile, operation
        if staged_loaded is not None:
            engine.settings_handle.install_prepared(engine.settings_handle.prepare(staged_loaded, session=session))
        if workspace is not None:
            engine.session.workspace = workspace

    monkeypatch.setattr("chrys.orchestration.engine.session_lifecycle.load_settings", hanging_restore_load)
    monkeypatch.setattr(
        "chrys.orchestration.engine.state.controls.load_settings",
        lambda **kwargs: LoadedSettings(settings=replacement_settings, provenance={}),
    )
    monkeypatch.setattr(engine.lifecycle, "shutdown", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session_in_place", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "start", fake_start)

    try:
        restore = asyncio.create_task(engine.on_session_restore(SessionRestore(session_id="restore_me")))
        await restore_load_entered.wait()
        reload = asyncio.create_task(engine._on_settings_reload(SettingsReload()))
        for _ in range(5):
            await asyncio.sleep(0)
        # The reload is parked at the shared gate: nothing it does may land
        # between the restore's snapshot and the restore's install.
        assert engine.settings is original_settings
        release_restore_load.set()
        await asyncio.gather(restore, reload)
    finally:
        release_restore_load.set()
        engine.session.guard.release()

    # The restore's derivation is in force, the parked reload's never was —
    # its token predates the transition, so it was denied out loud instead of
    # committing first and being silently overwritten.
    assert engine.settings is restore_settings
    assert errors != []


async def test_failed_restore_load_leaves_the_old_session_generation_intact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The transition fence is *prepared* before the load but *committed* only
    after it succeeds: the generation bump and the turn-state invalidation
    (pending retries, injection) live in that one commit, so a load failure
    must release the fence with the generation unchanged — and a reload that
    was parked behind the failed restore proceeds instead of being denied."""
    profile = _profile()
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, agent_profile=profile.name)
    original_settings = Settings()
    replacement_settings = Settings(default_approval_mode="auto")
    errors: list[Error] = []
    bus = EventBus()
    await bus.subscribe(Error, lambda event: collect_events(errors, event))
    engine = assemble_agent_engine(
        bus, settings=original_settings, state_store=store, agent_registry=_registry(profile)
    )
    generation_before = engine.permits.session_generation

    loop = asyncio.get_running_loop()
    release_restore_load = threading.Event()
    restore_load_entered = asyncio.Event()

    def failing_restore_load(**kwargs: Any) -> LoadedSettings:
        loop.call_soon_threadsafe(restore_load_entered.set)
        release_restore_load.wait(5)
        raise RuntimeError("unreadable config")

    monkeypatch.setattr("chrys.orchestration.engine.session_lifecycle.load_settings", failing_restore_load)
    monkeypatch.setattr(
        "chrys.orchestration.engine.state.controls.load_settings",
        lambda **kwargs: LoadedSettings(settings=replacement_settings, provenance={}),
    )

    try:
        restore = asyncio.create_task(engine.on_session_restore(SessionRestore(session_id="restore_me")))
        await restore_load_entered.wait()
        reload = asyncio.create_task(engine._on_settings_reload(SettingsReload()))
        for _ in range(5):
            await asyncio.sleep(0)
        assert engine.settings is original_settings
        release_restore_load.set()
        results = await asyncio.gather(restore, reload, return_exceptions=True)
    finally:
        release_restore_load.set()
        engine.session.guard.release()

    # The load failure aborted the restore out loud with the old session intact.
    assert isinstance(results[0], RuntimeError)
    assert results[1] is None
    assert engine.permits.session_generation == generation_before
    # The parked reload went through: an uncommitted fence leaves its token
    # valid, so nothing denied it and its derivation is now in force.
    assert engine.settings is replacement_settings
    # The failure is a bus event, not just a raised exception: interactive
    # callers publish with swallow-and-log delivery, and the Error is what
    # clears their restore loading state.
    assert [error.code for error in errors] == ["session_restore_failed"]
    assert errors[0].session_id == "restore_me"


@pytest.mark.parametrize("raise_handler_errors", [False, True])
async def test_restore_hydration_failure_publishes_error_after_releasing_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raise_handler_errors: bool, *, engine_services
) -> None:
    """The interactive bus swallows handler errors; hydration must still terminate its loading UI."""
    profile = _profile()
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, agent_profile=profile.name, primary_cwd=str(tmp_path))
    bus = EventBus()
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store, agent_registry=_registry(profile))
    stub_engine_lifecycle(monkeypatch, engine)
    errors: list[Error] = []
    restored: list[SessionRestored] = []

    async def on_error(event: Error) -> None:
        assert not engine.permits.current_task_owns_session_transition_permit()
        errors.append(event)

    await bus.subscribe(Error, on_error)
    await bus.subscribe(SessionRestored, lambda event: collect_events(restored, event))
    await bus.subscribe(SessionRestore, engine.on_session_restore)
    failure = ValueError("path is on a different mount")
    tracker = engine_services(engine).workspace_change_tracker
    monkeypatch.setattr(
        tracker,
        "restore",
        create_autospec(tracker.restore, side_effect=failure),
    )
    try:
        if raise_handler_errors:
            with pytest.raises(ValueError, match="different mount"):
                await bus.publish(SessionRestore(session_id="restore_me"), raise_handler_errors=True)
        else:
            await bus.publish(SessionRestore(session_id="restore_me"))
    finally:
        engine.session.guard.release()

    assert restored == []
    assert [(error.code, error.session_id, error.message) for error in errors] == [
        ("session_restore_failed", "restore_me", str(failure))
    ]


async def test_failed_startup_restore_reset_releases_target_without_saving(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, text="saved target", agent_profile="Code")
    session_file = store.session_dir("restore_me") / "session.json"
    original_payload = session_file.read_bytes()
    target_cwd = tmp_path / "restored-workspace"
    target_cwd.mkdir()

    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "restore_me"
    engine.session.workspace = Workspace.from_cwd(str(target_cwd))
    target_lock = engine.session.guard.acquire_for_restore("restore_me")
    engine.session.guard.install("restore_me", target_lock)

    await engine.reset_after_failed_startup_restore()

    assert engine.session.session_id is None
    assert engine.session.workspace == Workspace.from_cwd()
    assert engine.current.loaded is None
    assert engine.session.mutation_tracker is None
    assert engine.session.suppress_save is False
    assert not engine.session.guard.owns("restore_me")
    assert session_file.read_bytes() == original_payload


async def test_failed_startup_restore_reset_restores_save_flag_when_cleanup_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=JsonFileStateStore(tmp_path))

    def fail_cleanup() -> None:
        raise RuntimeError("cleanup failed")

    stub_engine_shutdown(monkeypatch, engine)
    monkeypatch.setattr(engine.lifecycle, "reset_turn_runtime_after_session_shutdown", fail_cleanup)

    with pytest.raises(RuntimeError, match="cleanup failed"):
        await engine.reset_after_failed_startup_restore()

    assert engine.session.suppress_save is False


async def test_failed_startup_restore_reset_rederives_settings_for_the_fallback_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback startup runs in the process cwd, so whatever the failed target
    left installed describes the wrong project trust domain."""
    replacement_settings = Settings(default_approval_mode="auto")
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=JsonFileStateStore(tmp_path))
    load_kwargs: dict[str, Any] = {}

    def fake_load(**kwargs: Any) -> LoadedSettings:
        load_kwargs.update(kwargs)
        return LoadedSettings(settings=replacement_settings, provenance={})

    monkeypatch.setattr("chrys.orchestration.engine.session_lifecycle.load_settings", fake_load)
    stub_engine_shutdown(monkeypatch, engine)

    await engine.reset_after_failed_startup_restore()

    assert load_kwargs["project_root"] == Path(os.getcwd())
    assert engine.settings is replacement_settings


async def test_failed_startup_restore_rederivation_failure_keeps_the_installed_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort: the reset must never mask the original failure, and the
    live settings remain a usable baseline without the re-derivation."""
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=JsonFileStateStore(tmp_path))
    installed = engine.loaded_settings

    def fail_load(**_kwargs: Any) -> LoadedSettings:
        raise OSError("config unreadable")

    monkeypatch.setattr("chrys.orchestration.engine.session_lifecycle.load_settings", fail_load)
    stub_engine_shutdown(monkeypatch, engine)

    await engine.reset_after_failed_startup_restore()

    assert engine.loaded_settings is installed


async def test_ignore_recovery_restore_discards_recovery_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rollback-style restores must not consume a live/stale checkpoint sidecar."""
    profile = _profile()
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, text="primary", agent_profile=profile.name)
    await _seed_recovery_sidecar(store, agent_profile=profile.name)

    engine = assemble_agent_engine(
        EventBus(), settings=Settings(), state_store=store, agent_registry=_registry(profile)
    )
    active_lock = engine.session.guard.acquire_for_restore("restore_me")
    engine.session.guard.install("restore_me", active_lock)

    stub_engine_lifecycle(monkeypatch, engine)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me", ignore_recovery=True))
    finally:
        engine.session.guard.release()

    assert engine.session.turn_number == 1
    assert not (store.session_dir("restore_me") / SESSION_RECOVERY_FILE_NAME).exists()


async def test_ignore_recovery_restore_removes_recovery_only_session_dir(tmp_path: Path) -> None:
    bus = EventBus()
    errors: list[Error] = []
    await bus.subscribe(Error, lambda event: collect_events(errors, event))
    store = JsonFileStateStore(tmp_path)
    await _seed_recovery_sidecar(store, agent_profile="Code")
    session_dir = store.session_dir("restore_me")
    assert session_dir.is_dir()

    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    await engine.on_session_restore(SessionRestore(session_id="restore_me", ignore_recovery=True))

    assert [event.code for event in errors] == ["session_not_found"]
    assert not session_dir.exists()


async def test_ignore_recovery_restore_keeps_current_recovery_only_session_dir(tmp_path: Path) -> None:
    bus = EventBus()
    errors: list[Error] = []
    await bus.subscribe(Error, lambda event: collect_events(errors, event))
    store = JsonFileStateStore(tmp_path)
    await _seed_recovery_sidecar(store, agent_profile="Code")
    session_dir = store.session_dir("restore_me")

    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store)
    active_lock = engine.session.guard.acquire_for_restore("restore_me")
    engine.session.guard.install("restore_me", active_lock)
    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me", ignore_recovery=True))
    finally:
        engine.session.guard.release()

    assert [event.code for event in errors] == ["session_not_found"]
    assert not (session_dir / SESSION_RECOVERY_FILE_NAME).exists()
    assert session_dir.exists()


async def test_current_recovered_session_restore_keeps_recovery_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, text="primary", agent_profile=profile.name)
    await _seed_recovery_sidecar(store, agent_profile=profile.name)

    engine = assemble_agent_engine(
        EventBus(), settings=Settings(), state_store=store, agent_registry=_registry(profile)
    )
    active_lock = engine.session.guard.acquire_for_restore("restore_me")
    engine.session.guard.install("restore_me", active_lock)
    engine.session.recovered_from_sidecar = True

    stub_engine_lifecycle(monkeypatch, engine)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me"))
    finally:
        engine.session.guard.release()

    assert engine.session.turn_number == 9
    assert engine.recovered_from_sidecar is True
    assert (store.session_dir("restore_me") / SESSION_RECOVERY_FILE_NAME).exists()


async def test_session_restore_keeps_admission_closed_through_restored_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile()
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, text="primary", agent_profile=profile.name)
    bus = EventBus()
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store, agent_registry=_registry(profile))
    restored_event_closed_states: list[bool] = []
    start_closed_states: list[bool] = []
    shutdown_closed_states: list[bool] = []
    await bus.subscribe(
        SessionRestored,
        lambda _event: collect_events(
            restored_event_closed_states, engine.turns.turn_state.lease.prompt_admission_closed
        ),
    )

    async def fake_shutdown(*, keep_side_answers: bool = False) -> None:
        shutdown_closed_states.append(engine.turns.turn_state.lease.prompt_admission_closed)

    async def fake_start(
        start_profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        _ = start_profile, operation
        start_closed_states.append(engine.turns.turn_state.lease.prompt_admission_closed)

    monkeypatch.setattr(engine.lifecycle, "shutdown", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session_in_place", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "start", fake_start)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me"))
    finally:
        engine.session.guard.release()

    assert shutdown_closed_states == [True]
    assert start_closed_states == [True]
    assert restored_event_closed_states == [True]
    assert engine.turns.turn_state.lease.prompt_admission_closed is False


async def test_new_session_keeps_admission_closed_through_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _profile()
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine.session.agent_profile = profile
    states: list[tuple[str, bool]] = []

    async def fake_shutdown(*, keep_side_answers: bool = False) -> None:
        states.append(("shutdown", engine.turns.turn_state.lease.prompt_admission_closed))

    async def fake_start(
        start_profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        _ = start_profile, operation
        states.append(("start", engine.turns.turn_state.lease.prompt_admission_closed))

    monkeypatch.setattr(engine.lifecycle, "shutdown", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "close_session_in_place", fake_shutdown)
    monkeypatch.setattr(engine.lifecycle, "start", fake_start)

    await engine._on_new_session(SessionNew())

    assert states == [("shutdown", True), ("start", True)]
    assert engine.session_generation == 1
    assert engine.turns.turn_state.lease.prompt_admission_closed is False


async def test_new_session_uses_profile_after_transition_permit(monkeypatch: pytest.MonkeyPatch) -> None:
    old_profile = _profile("Code")
    new_profile = _profile("Explore")
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine.session.agent_profile = old_profile

    async def fake_begin_session_transition(operation: str) -> str:
        assert operation == "new_session"
        engine.session.agent_profile = new_profile
        return "session:new_session:test"

    monkeypatch.setattr(engine.permits, "begin_session_transition", fake_begin_session_transition)
    monkeypatch.setattr(engine.permits, "finish_session_transition", lambda _owner: None)
    stubbed = stub_engine_lifecycle(monkeypatch, engine)

    await engine._on_new_session(SessionNew())

    assert stubbed.start_calls == [(new_profile, "new_session")]


async def test_new_session_keeps_the_approval_mode_chosen_this_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    # Choosing bypass saves ``auto`` so the next launch is safe; within this launch the choice stands.
    profile = _profile()
    engine = assemble_agent_engine(EventBus(), settings=Settings(default_approval_mode="auto"))
    engine.session.agent_profile = profile
    engine.session.approval_mode = ApprovalMode.BYPASS
    stubbed = stub_engine_lifecycle(monkeypatch, engine)

    await engine._on_new_session(SessionNew())

    assert stubbed.start_calls == [(profile, "new_session")]
    assert engine.session.approval_mode is ApprovalMode.BYPASS


async def test_prepared_startup_profile_is_blank_session_restore_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fallback_profile = _profile("Code")
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, text="primary", agent_profile="")
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store, agent_registry=_registry())
    await engine.prepare(fallback_profile)

    async def fake_begin_session_transition(operation: str) -> str:
        assert operation == "restore"
        return "session:restore:test"

    monkeypatch.setattr(engine.permits, "begin_session_transition", fake_begin_session_transition)
    monkeypatch.setattr(engine.permits, "finish_session_transition", lambda _owner: None)
    stubbed = stub_engine_lifecycle(monkeypatch, engine)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me"))
    finally:
        engine.session.guard.release()

    assert stubbed.start_calls == [(fallback_profile, "restore")]


async def test_session_restore_live_profile_fallback_is_sampled_after_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_profile = _profile("Code")
    new_profile = _profile("Explore")
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, text="primary", agent_profile="")
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store, agent_registry=_registry())
    engine.session.agent_profile = old_profile

    async def fake_prepare_session_transition(operation: str) -> str:
        # The boundary acquisition is the exclusion point: a profile switch
        # landing just before it must be what the fallback then samples.
        assert operation == "restore"
        engine.session.agent_profile = new_profile
        return "session:restore:test"

    monkeypatch.setattr(engine.permits, "prepare_session_transition", fake_prepare_session_transition)
    monkeypatch.setattr(engine.permits, "commit_session_transition", lambda _owner: None)
    monkeypatch.setattr(engine.permits, "finish_session_transition", lambda _owner: None)
    stubbed = stub_engine_lifecycle(monkeypatch, engine)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me"))
    finally:
        engine.session.guard.release()

    assert stubbed.start_calls == [(new_profile, "restore")]


async def test_session_restore_resolves_saved_agent_by_id_after_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renamed_profile = _profile("Renamed")
    renamed_profile.id = "stable-agent-id"
    stale_name_collision = _profile("OldName")
    stale_name_collision.id = "different-agent-id"
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, agent_profile="OldName", agent_profile_id="stable-agent-id")
    engine = assemble_agent_engine(
        EventBus(),
        settings=Settings(),
        state_store=store,
        agent_registry=_registry(stale_name_collision, renamed_profile),
    )
    stubbed = stub_engine_lifecycle(monkeypatch, engine)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me"))
    finally:
        engine.session.guard.release()

    assert stubbed.start_calls == [(renamed_profile, "restore")]


async def test_session_restore_unresolved_explicit_agent_stops_instead_of_falling_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_profile = _profile("Current")
    first_available = _profile("First")
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, agent_profile="Saved")
    bus = EventBus()
    errors: list[Error] = []
    await bus.subscribe(Error, lambda event: collect_events(errors, event))
    engine = assemble_agent_engine(
        bus,
        settings=Settings(),
        state_store=store,
        agent_registry=_registry(first_available),
    )
    engine.session.agent_profile = current_profile
    start = AsyncMock()
    monkeypatch.setattr(engine.lifecycle, "start", start)

    await engine.on_session_restore(SessionRestore(session_id="restore_me", profile_name="Missing"))

    start.assert_not_awaited()
    resolution_errors = [event for event in errors if event.code == "requested_agent_profile_unresolved"]
    assert len(resolution_errors) == 1
    assert_display_message(
        resolution_errors[0],
        "restore.requested_agent_profile_unresolved",
        {"profile": "Missing"},
    )
    assert not engine.session.guard.owns("restore_me")


async def test_session_restore_stale_agent_id_does_not_match_reused_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_profile = _profile("Current")
    replacement_profile = _profile("Legacy")
    replacement_profile.id = "new-profile-id"
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(
        store,
        agent_profile=replacement_profile.name,
        # Deliberately equals the replacement's name: an id-only lookup must
        # not reinterpret this stale identity as a generic name selector.
        agent_profile_id=replacement_profile.name,
    )
    bus = EventBus()
    warnings: list[Warning] = []
    await bus.subscribe(Warning, lambda event: collect_events(warnings, event))
    engine = assemble_agent_engine(
        bus,
        settings=Settings(),
        state_store=store,
        agent_registry=_registry(replacement_profile),
    )
    engine.session.agent_profile = current_profile
    stubbed = stub_engine_lifecycle(monkeypatch, engine)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me"))
    finally:
        engine.session.guard.release()

    assert stubbed.start_calls == [(current_profile, "restore")]
    resolution_warnings = [event for event in warnings if event.code == "saved_agent_profile_unresolved"]
    assert len(resolution_warnings) == 1
    assert_display_message(
        resolution_warnings[0],
        "restore.agent_profile_unresolved_using_current",
        {"saved": "Legacy", "current": "Current"},
    )


async def test_session_restore_ambiguous_agent_id_uses_saved_name_without_current_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_match = _profile("First")
    first_match.id = "shared-agent-id"
    saved_profile = _profile("Second")
    saved_profile.id = "shared-agent-id"
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, agent_profile=saved_profile.name, agent_profile_id=saved_profile.id)
    engine = assemble_agent_engine(
        EventBus(),
        settings=Settings(),
        state_store=store,
        agent_registry=_registry(first_match, saved_profile),
    )
    stubbed = stub_engine_lifecycle(monkeypatch, engine)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me"))
    finally:
        engine.session.guard.release()

    assert stubbed.start_calls == [(saved_profile, "restore")]


async def test_session_restore_unresolved_agent_id_without_current_agent_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement_profile = _profile("Legacy")
    replacement_profile.id = "new-profile-id"
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, agent_profile=replacement_profile.name, agent_profile_id="deleted-profile-id")
    bus = EventBus()
    errors: list[Error] = []
    await bus.subscribe(Error, lambda event: collect_events(errors, event))
    engine = assemble_agent_engine(
        bus,
        settings=Settings(),
        state_store=store,
        agent_registry=_registry(replacement_profile),
    )
    start = AsyncMock()
    monkeypatch.setattr(engine.lifecycle, "start", start)

    await engine.on_session_restore(SessionRestore(session_id="restore_me"))

    start.assert_not_awaited()
    resolution_errors = [event for event in errors if event.code == "saved_agent_profile_unresolved"]
    assert len(resolution_errors) == 1
    assert_display_message(
        resolution_errors[0],
        "restore.agent_profile_unresolved",
        {"saved": "Legacy"},
    )
    assert not engine.session.guard.owns("restore_me")


async def test_session_restore_missing_saved_profile_keeps_current_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_profile = _profile("Current")
    first_available = _profile("First")
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, agent_profile="Deleted")
    engine = assemble_agent_engine(
        EventBus(),
        settings=Settings(),
        state_store=store,
        agent_registry=_registry(first_available),
    )
    engine.session.agent_profile = current_profile
    stubbed = stub_engine_lifecycle(monkeypatch, engine)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me"))
    finally:
        engine.session.guard.release()

    assert stubbed.start_calls == [(current_profile, "restore")]


async def test_session_restore_missing_saved_profile_without_current_agent_uses_first_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_available = _profile("First")
    second_available = _profile("Second")
    store = JsonFileStateStore(tmp_path)
    await _seed_restorable_session(store, agent_profile="Deleted")
    engine = assemble_agent_engine(
        EventBus(),
        settings=Settings(),
        state_store=store,
        agent_registry=_registry(first_available, second_available),
    )
    stubbed = stub_engine_lifecycle(monkeypatch, engine)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me"))
    finally:
        engine.session.guard.release()

    assert stubbed.start_calls == [(first_available, "restore")]


async def test_session_restore_derives_settings_from_the_target_sessions_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The restore crosses into the target's project trust domain: its root —
    not the current session's — is what the staged load is derived from."""
    agent_profile = _profile("Code")
    target_cwd = tmp_path / "restored-workspace"
    target_cwd.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await _seed_restorable_session(store, agent_profile=agent_profile.name, primary_cwd=str(target_cwd))
    replacement_settings = Settings(default_approval_mode="auto")
    engine = assemble_agent_engine(
        EventBus(), settings=Settings(), state_store=store, agent_registry=_registry(agent_profile)
    )
    load_kwargs: dict[str, Any] = {}

    def fake_load(**kwargs: Any) -> LoadedSettings:
        load_kwargs.update(kwargs)
        return LoadedSettings(settings=replacement_settings, provenance={})

    monkeypatch.setattr("chrys.orchestration.engine.session_lifecycle.load_settings", fake_load)
    stub_engine_lifecycle(monkeypatch, engine, expect_operation="restore")

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me"))
    finally:
        engine.session.guard.release()

    assert load_kwargs["project_root"] == Path(str(target_cwd))
    assert engine.settings is replacement_settings


async def test_session_restore_hydrates_the_workspace_baseline_with_the_target_sessions_notice_setting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The baseline restore runs before the build commits the staged settings:
    with the notice on in the live session but off for the restored target,
    it must read the target's value and skip the root probes."""
    agent_profile = _profile("Code")
    target_cwd = tmp_path / "restored-workspace"
    target_cwd.mkdir()
    store = JsonFileStateStore(tmp_path / "sessions")
    await _seed_restorable_session(store, agent_profile=agent_profile.name, primary_cwd=str(target_cwd))
    engine = assemble_agent_engine(
        EventBus(),
        settings=Settings(workspace_change_notice=True),
        state_store=store,
        agent_registry=_registry(agent_profile),
    )
    assert engine.settings.workspace_change_notice is True

    def fake_load(**_kwargs: Any) -> LoadedSettings:
        return LoadedSettings(settings=Settings(workspace_change_notice=False), provenance={})

    probed: list[Path] = []

    def _record(path: Path, *_args: Any, **_kwargs: Any) -> Path | None:
        probed.append(path)
        return None

    monkeypatch.setattr("chrys.orchestration.engine.session_lifecycle.load_settings", fake_load)
    stub_engine_lifecycle(monkeypatch, engine, expect_operation="restore")
    # Installed after construction: the live engine's own retarget legitimately probes.
    monkeypatch.setattr(workspace_changes, "resolve_git_root", _record)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me"))
    finally:
        engine.session.guard.release()

    assert engine.settings.workspace_change_notice is False
    assert probed == []


async def test_session_restore_aborts_before_teardown_when_the_settings_load_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loaded before anything is torn down: an unreadable config file must
    abort the restore with the current session intact."""
    agent_profile = _profile("Code")
    store = JsonFileStateStore(tmp_path / "sessions")
    await _seed_restorable_session(store, agent_profile=agent_profile.name)
    engine = assemble_agent_engine(
        EventBus(), settings=Settings(), state_store=store, agent_registry=_registry(agent_profile)
    )
    engine.session.session_id = "current-session"
    installed = engine.loaded_settings

    def fail_load(**_kwargs: Any) -> LoadedSettings:
        raise OSError("config unreadable")

    monkeypatch.setattr("chrys.orchestration.engine.session_lifecycle.load_settings", fail_load)
    shutdown_calls = stub_engine_shutdown(monkeypatch, engine)

    with pytest.raises(OSError, match="config unreadable"):
        await engine.on_session_restore(SessionRestore(session_id="restore_me"))

    assert shutdown_calls == []
    assert engine.session.session_id == "current-session"
    assert engine.loaded_settings is installed


async def test_session_restore_refuses_a_session_whose_working_dir_is_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing is torn down or switched; the target's lock is free again for the next attempt."""
    profile = _profile()
    store = JsonFileStateStore(tmp_path / "sessions")
    gone = tmp_path / "gone"
    await _seed_restorable_session(store, agent_profile=profile.name, primary_cwd=str(gone))
    bus = EventBus()
    errors: list[Error] = []
    restored: list[SessionRestored] = []
    await bus.subscribe(Error, lambda event: collect_events(errors, event))
    await bus.subscribe(SessionRestored, lambda event: collect_events(restored, event))
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store, agent_registry=_registry(profile))
    engine.session.session_id = "current-session"
    start = AsyncMock()
    monkeypatch.setattr(engine.lifecycle, "start", start)
    shutdown_calls = stub_engine_shutdown(monkeypatch, engine)

    await engine.on_session_restore(SessionRestore(session_id="restore_me"))

    assert [(event.code, event.session_id) for event in errors] == [("session_cwd_missing", "restore_me")]
    assert (
        errors[0].message == f"Working directory of session {session_short_id('restore_me')} no longer exists: {gone}"
    )
    assert_display_message(errors[0], "restore.session_cwd_missing", {"path": DisplayPath(str(gone))})
    assert restored == []
    start.assert_not_awaited()
    assert shutdown_calls == []
    assert engine.session.session_id == "current-session"
    assert not engine.session.guard.owns("restore_me")
    other = ActiveSessionGuard(store)
    try:
        assert await asyncio.to_thread(other.ensure, "restore_me")
    finally:
        other.release()


async def test_current_session_restore_keeps_a_missing_working_dir_as_a_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rolling back the session already open is not a switch: it proceeds and only warns."""
    profile = _profile()
    store = JsonFileStateStore(tmp_path / "sessions")
    gone = tmp_path / "gone"
    await _seed_restorable_session(store, agent_profile=profile.name, primary_cwd=str(gone))
    bus = EventBus()
    errors: list[Error] = []
    restored: list[SessionRestored] = []
    await bus.subscribe(Error, lambda event: collect_events(errors, event))
    await bus.subscribe(SessionRestored, lambda event: collect_events(restored, event))
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store, agent_registry=_registry(profile))
    active_lock = engine.session.guard.acquire_for_restore("restore_me")
    engine.session.guard.install("restore_me", active_lock)
    stubbed = stub_engine_lifecycle(monkeypatch, engine)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me"))
    finally:
        engine.session.guard.release()

    assert errors == []
    assert [(event.session_id, event.cwd_warning) for event in restored] == [
        ("restore_me", f"Working directory no longer exists: {gone}")
    ]
    assert len(stubbed.start_calls) == 1


async def test_session_restore_into_a_chosen_directory_passes_the_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The frontend's recovery path: the user picked another directory for the session."""
    profile = _profile()
    store = JsonFileStateStore(tmp_path / "sessions")
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    await _seed_restorable_session(store, agent_profile=profile.name, primary_cwd=str(tmp_path / "gone"))
    bus = EventBus()
    errors: list[Error] = []
    restored: list[SessionRestored] = []
    await bus.subscribe(Error, lambda event: collect_events(errors, event))
    await bus.subscribe(SessionRestored, lambda event: collect_events(restored, event))
    engine = assemble_agent_engine(bus, settings=Settings(), state_store=store, agent_registry=_registry(profile))
    stubbed = stub_engine_lifecycle(monkeypatch, engine)

    try:
        await engine.on_session_restore(SessionRestore(session_id="restore_me", primary_cwd=str(chosen)))
    finally:
        engine.session.guard.release()

    assert errors == []
    assert [(event.session_id, event.primary_cwd, event.cwd_warning) for event in restored] == [
        ("restore_me", str(chosen), "")
    ]
    assert engine.session.workspace is not None
    assert engine.session.workspace.primary_cwd == str(chosen)
    assert len(stubbed.start_calls) == 1
