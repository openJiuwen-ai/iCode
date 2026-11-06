# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lifecycle permits preserve owner clocks, admission fences, and task ownership."""

from __future__ import annotations

import asyncio

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.orchestration.engine.execution import RunTaskDrainOutcome
from chrys.orchestration.engine.state.lifecycle_permits import LifecyclePermits, RebuildPermit, RebuildPermitDenied
from chrys.service.session.persistence import SessionPersistence
from tests.support.components import make_permits, make_session, make_turn_state
from tests.support.waiting import wait_for


@pytest.mark.parametrize(
    ("case", "expected_code"),
    [
        ("shutdown", "runtime_mutation_shutdown"),
        ("session_changed", "runtime_mutation_session_changed"),
        ("superseded", "runtime_mutation_superseded"),
        ("load_generation", "runtime_mutation_superseded"),
        ("load_active", "runtime_mutation_load_active"),
        ("active_admission", "runtime_mutation_busy"),
        ("active_run", "runtime_mutation_busy"),
        ("drain_cancelled", "runtime_mutation_busy"),
    ],
)
async def test_rebuild_permit_validator_denial_codes(
    case: str, expected_code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = make_session(persistence=SessionPersistence(None, EventBus()), workspace=None, approval_mode=None)
    session.session_id = "sid"
    turn_state = make_turn_state()
    permits = make_permits(turn_state=turn_state, session=session)
    token = permits.capture_control_token()
    release_run = asyncio.Event()
    run_task: asyncio.Task[None] | None = None

    if case == "shutdown":
        session.shutting_down = True
    elif case == "session_changed":
        permits.advance_session_generation()
    elif case == "superseded":
        permits.advance_build_generation()
    elif case == "load_generation":
        permits.begin_agent_load()
        permits.finish_agent_load()
    elif case == "load_active":
        permits.begin_agent_load()
        token = permits.capture_control_token()
    elif case == "active_admission":
        assert turn_state.lease.reserve_prompt_admission(
            kind="fresh",
            session_generation=token.session_generation,
            build_generation=token.build_generation,
        )
    elif case == "active_run":

        async def _run() -> None:
            await release_run.wait()

        run_task = asyncio.create_task(_run())
        turn_state.lease.run_task = run_task

    async def boundary_idle() -> None:
        return None

    async def drain_boundary() -> RunTaskDrainOutcome:
        return RunTaskDrainOutcome(cancelled=case == "drain_cancelled")

    monkeypatch.setattr(permits, "wait_for_agent_load_idle", boundary_idle)
    monkeypatch.setattr(turn_state.lease, "wait_for_active_admissions_idle", boundary_idle)
    monkeypatch.setattr(turn_state, "drain_for_boundary", drain_boundary)
    try:
        denied = await permits.acquire_rebuild_permit(token)
    finally:
        release_run.set()
        if run_task is not None:
            await asyncio.wait_for(run_task, timeout=5.0)

    assert isinstance(denied, RebuildPermitDenied)
    assert denied.code == expected_code


@pytest.fixture
def permits() -> LifecyclePermits:
    session = make_session(persistence=SessionPersistence(None, EventBus()), workspace=None, approval_mode=None)
    session.session_id = "original"
    return make_permits(turn_state=make_turn_state(), session=session)


async def test_rebuild_permit_belongs_to_acquiring_task(permits: LifecyclePermits) -> None:
    permit = await permits.acquire_rebuild_permit(permits.capture_control_token())
    assert isinstance(permit, RebuildPermit)
    assert permits.current_task_owns_rebuild_permit() is True
    permits.ensure_rebuild_permit(permit)

    async def other_task() -> None:
        assert permits.current_task_owns_rebuild_permit() is False
        with pytest.raises(RuntimeError, match="Invalid rebuild permit"):
            permits.ensure_rebuild_permit(permit)

    try:
        await asyncio.create_task(other_task())
    finally:
        permits.release_rebuild_permit(permit)
    assert permits.gate_lock.locked() is False
    assert permits.current_task_owns_rebuild_permit() is False


@pytest.mark.parametrize("commit", [False, True])
async def test_transition_releases_lock_and_admission(permits: LifecyclePermits, commit: bool) -> None:
    owner = await permits.prepare_session_transition("restore")
    assert owner is not None
    assert permits.current_task_owns_session_transition_permit() is True
    assert permits.prompt_admission_owner_for_current_task() == owner
    if commit:
        permits.commit_session_transition(owner)
        assert permits.session_generation == 1
        permits.finish_session_transition(owner)
    else:
        permits.abort_prepared_session_transition(owner)
        assert permits.session_generation == 0
    assert permits.gate_lock.locked() is False
    assert permits.prompt_admission_owner_for_current_task() is None
    next_permit = await permits.acquire_rebuild_permit(permits.capture_control_token())
    assert isinstance(next_permit, RebuildPermit)
    permits.release_rebuild_permit(next_permit)


async def test_transition_rejects_owner_after_session_clock_advances(permits: LifecyclePermits) -> None:
    generation = permits.session_generation
    permits.advance_session_generation()
    owner = await permits.prepare_session_transition_if_current(
        "restore", session_id="original", session_generation=generation
    )
    assert owner is None
    assert permits.gate_lock.locked() is False


@pytest.mark.parametrize("change", ["session", "shutdown"])
async def test_rebuild_reads_session_after_waiting(change: str, monkeypatch: pytest.MonkeyPatch) -> None:
    session = make_session(persistence=SessionPersistence(None, EventBus()), workspace=None, approval_mode=None)
    session.session_id = "original"
    turn_state = make_turn_state()
    permits = make_permits(turn_state=turn_state, session=session)
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def boundary() -> RunTaskDrainOutcome:
        entered.set()
        await resume.wait()
        return RunTaskDrainOutcome()

    monkeypatch.setattr(turn_state, "drain_for_boundary", boundary)
    task = asyncio.create_task(permits.acquire_rebuild_permit(permits.capture_control_token()))
    try:
        await wait_for(entered.is_set, description="rebuild boundary entered")
        if change == "session":
            session.session_id = "replacement"
        else:
            session.shutting_down = True
        resume.set()
        denied = await task
        assert isinstance(denied, RebuildPermitDenied)
        assert denied.reason == ("session_changed" if change == "session" else "shutdown")
        assert permits.gate_lock.locked() is False
    finally:
        resume.set()
        await task


async def test_transition_reads_session_after_admission_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    session = make_session(persistence=SessionPersistence(None, EventBus()), workspace=None, approval_mode=None)
    session.session_id = "original"
    turn_state = make_turn_state()
    permits = make_permits(turn_state=turn_state, session=session)
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def admission_idle() -> None:
        entered.set()
        await resume.wait()

    monkeypatch.setattr(turn_state.lease, "wait_for_active_admissions_idle", admission_idle)
    task = asyncio.create_task(
        permits.prepare_session_transition_if_current("restore", session_id="original", session_generation=0)
    )
    try:
        await wait_for(entered.is_set, description="transition admission wait entered")
        permits.advance_session_generation()
        resume.set()
        assert await task is None
        assert permits.gate_lock.locked() is False
    finally:
        resume.set()
        await task


async def test_manual_reload_sees_turn_started_by_an_inflight_admission_before_draining() -> None:
    session = make_session(persistence=SessionPersistence(None, EventBus()), workspace=None, approval_mode=None)
    state = make_turn_state()
    executing = asyncio.Event()
    permits = LifecyclePermits(turn_state=state, session=session, turn_executing=executing.is_set)
    token = permits.capture_control_token()
    admission = state.lease.reserve_prompt_admission(
        kind="fresh", session_generation=token.session_generation, build_generation=token.build_generation
    )
    assert admission is not None
    refresh = asyncio.create_task(permits.acquire_rebuild_permit(token, reject_running=True))

    async def run_until_released() -> None:
        await release.wait()

    release = asyncio.Event()
    runner = asyncio.create_task(run_until_released())
    state.lease.run_task = runner
    try:
        await asyncio.sleep(0)  # Let the reload reach the outstanding admission.
        assert not refresh.done()
        executing.set()
        state.lease.release_prompt_admission(admission)
        await wait_for(refresh.done, description="manual refresh refused before turn drain")
        assert isinstance(await refresh, RebuildPermitDenied)
        assert not runner.done()
    finally:
        release.set()
        state.lease.release_prompt_admission(admission)
        await runner
        await refresh
