# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The hook runs a :class:`HookManager` reports in its snapshots, and who waits for each.

Every hook here is a real ``python -c`` subprocess. A held hook polls for a
release file the test creates, so the test decides when it ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import sys
import textwrap
import time
from collections.abc import AsyncIterator
from pathlib import Path

import psutil
import pytest

from chrys.foundation.models.hook_executions import HookExecutionView
from chrys.foundation.trajectory.context import trajectory_scope
from chrys.foundation.trajectory.event_types import EventType
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.schema import (
    HookConfig,
    HookDecision,
    HookExecution,
    HookMatch,
    HookRun,
    HookSettings,
    HooksFile,
)
from tests.service.trajectory._fakes import FakeSink, make_context
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for, wait_until

Snapshots = list[tuple[HookExecutionView, ...]]


def _held(release: Path, *, exit_code: int = 0, result: str = "") -> str:
    write = ""
    if result:
        write = f"open(os.environ['CHRYS_HOOK_RESULT'], 'w').write({result!r})\n"
    return textwrap.dedent(
        f"""
        import os, sys, time
        while not os.path.exists({str(release)!r}):
            time.sleep(0.01)
        """
    ) + (write + f"sys.exit({exit_code})\n")


def _hook(
    hook_id: str,
    event: HookEvent,
    *,
    mode: str = "blocking",
    script: str = "pass",
    argv: list[str] | None = None,
    timeout: float = 30.0,
    on_error: str = "warn",
    tool_name: str | None = None,
    detach: bool = False,
) -> HookConfig:
    return HookConfig(
        id=hook_id,
        event=event,
        run=HookRun(type="command", argv=argv or [sys.executable, "-c", script]),
        execution=HookExecution(mode=mode, timeout_seconds=timeout, on_error=on_error, detach=detach),
        match=HookMatch(tool_name=tool_name),
    )


def _manager(hooks_dir: Path, *hooks: HookConfig, **settings: float | int) -> HookManager:
    return HookManager(file=HooksFile(settings=HookSettings(**settings), hooks=list(hooks)), hooks_dir=hooks_dir)


def _record_snapshots(manager: HookManager) -> Snapshots:
    snapshots: Snapshots = []

    async def on_change(changed: HookManager) -> None:
        snapshots.append(changed.executions())

    manager.on_change = on_change
    return snapshots


def _tool_payload(name: str = "shell") -> dict[str, object]:
    return {"tool": {"name": name, "kind": "shell", "call_id": "c1", "args": {}}}


@pytest.fixture
def hooks_dir(tmp_path: Path) -> Path:
    path = tmp_path / "hooks"
    path.mkdir()
    return path


@pytest.fixture
async def release(tmp_path: Path) -> AsyncIterator[Path]:
    """A release file every held hook waits for; created at teardown so no hook outlives the test."""
    path = tmp_path / "release"
    yield path
    path.touch()


async def _running(manager: HookManager, hook_id: str) -> HookExecutionView:
    await wait_for(
        lambda: any(view.hook_id == hook_id and not view.queued for view in manager.executions()),
        timeout=ENGINE_TURN_TIMEOUT,
        description=f"{hook_id} running",
    )
    return next(view for view in manager.executions() if view.hook_id == hook_id)


# --- Snapshots of blocking runs -------------------------------------------------


async def test_a_blocking_tool_hook_is_queued_then_running_then_removed(hooks_dir: Path, release: Path) -> None:
    manager = _manager(hooks_dir, _hook("guard", HookEvent.BEFORE_TOOL_CALL, script=_held(release)))
    snapshots = _record_snapshots(manager)
    before = time.monotonic()
    fire = asyncio.create_task(manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool_payload("zsh")))
    try:
        view = await _running(manager, "guard")
        assert not fire.done()
        assert view.waited_at == "call"
        assert before <= view.started_at <= time.monotonic()
        # The snapshot published last is the manager's state now.
        assert snapshots[-1] == manager.executions()
    finally:
        release.touch()
    decision = await fire
    assert decision.blocked is False
    assert manager.executions() == ()
    # Always added as queued first, replaced by running once it has a slot, removed at the end.
    assert [[view.queued for view in snapshot] for snapshot in snapshots] == [[True], [False], []]


async def test_a_blocking_hook_on_an_event_without_a_tool_is_waited_at_the_call(hooks_dir: Path, release: Path) -> None:
    manager = _manager(hooks_dir, _hook("lint", HookEvent.AFTER_TURN, script=_held(release)))
    _record_snapshots(manager)
    fire = asyncio.create_task(manager.fire(HookEvent.AFTER_TURN, {"turn": 1, "status": "ok"}))
    try:
        view = await _running(manager, "lint")
        assert (view.hook_id, view.waited_at) == ("lint", "call")
    finally:
        release.touch()
    await fire
    assert manager.executions() == ()


async def test_snapshots_are_sent_without_a_trajectory_context(hooks_dir: Path) -> None:
    """No trajectory context (the trace is ``None``): the runs are still reported."""
    manager = _manager(hooks_dir, _hook("quick", HookEvent.BEFORE_TOOL_CALL))
    snapshots = _record_snapshots(manager)
    await manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool_payload())
    assert snapshots
    assert any(view.hook_id == "quick" for snapshot in snapshots for view in snapshot)
    assert snapshots[-1] == ()


async def test_an_event_with_no_matching_hook_sends_no_snapshot(hooks_dir: Path) -> None:
    manager = _manager(hooks_dir, _hook("only-write", HookEvent.BEFORE_TOOL_CALL, tool_name="write_file"))
    snapshots = _record_snapshots(manager)
    await manager.fire(HookEvent.AFTER_TURN, {"turn": 1})
    await manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool_payload("read_file"))
    assert snapshots == []


# --- Which runs are waited on --------------------------------------------------


@pytest.mark.parametrize(
    ("event", "scope", "waited_at"),
    [(HookEvent.AFTER_TURN, "turn", "turn"), (HookEvent.SESSION_START, "session", "session")],
)
async def test_an_async_hook_is_reported_from_its_start_with_its_drain_point(
    hooks_dir: Path, release: Path, event: HookEvent, scope: str, waited_at: str
) -> None:
    manager = _manager(hooks_dir, _hook("bg", event, mode="async", script=_held(release)))
    snapshots = _record_snapshots(manager)
    await manager.fire(event, {}, scope=scope)
    # fire() returned: the run is reported at once, before it ends.
    assert [view.hook_id for view in manager.executions()] == ["bg"]
    view = manager.executions()[0]
    assert (view.hook_id, view.waited_at) == ("bg", waited_at)
    assert snapshots and snapshots[-1] == manager.executions()
    release.touch()
    if scope == "turn":
        await asyncio.wait_for(manager.drain_turn(), ENGINE_TURN_TIMEOUT)
    else:
        await asyncio.wait_for(manager.drain_session(close=False), ENGINE_TURN_TIMEOUT)
    assert manager.executions() == ()
    assert snapshots[-1] == ()
    await manager.drain_session()


async def test_a_detached_scope_blocking_hook_is_reported_as_waited_on_by_nobody(
    hooks_dir: Path, release: Path
) -> None:
    """``user_interrupt`` runs fire with scope ``detached``: registered, with ``waited_at="none"``."""
    manager = _manager(hooks_dir, _hook("on-interrupt", HookEvent.USER_INTERRUPT, script=_held(release)))
    snapshots = _record_snapshots(manager)
    fire = asyncio.create_task(manager.fire(HookEvent.USER_INTERRUPT, {}, scope="detached"))
    try:
        view = await _running(manager, "on-interrupt")
        assert (view.hook_id, view.waited_at) == ("on-interrupt", "none")
        assert not fire.done()
    finally:
        release.touch()
    await fire
    assert manager.executions() == ()
    assert [[v.queued for v in snapshot] for snapshot in snapshots] == [[True], [False], []]


@pytest.mark.parametrize(
    ("mode", "scope", "event"),
    [
        ("async", "detached", HookEvent.USER_INTERRUPT),
        ("fire_and_forget", "turn", HookEvent.AFTER_TOOL_CALL),
        ("fire_and_forget", "session", HookEvent.SESSION_START),
    ],
    ids=["detached-async", "fire-and-forget", "fire-and-forget-session"],
)
async def test_runs_nobody_waits_for_are_reported_with_none(
    hooks_dir: Path, release: Path, mode: str, scope: str, event: HookEvent
) -> None:
    manager = _manager(hooks_dir, _hook("observer", event, mode=mode, script=_held(release)))
    snapshots = _record_snapshots(manager)
    await manager.fire(event, _tool_payload(), scope=scope)
    try:
        view = await _running(manager, "observer")
        assert (view.hook_id, view.waited_at) == ("observer", "none")
    finally:
        release.touch()
    await wait_for(lambda: manager.executions() == (), timeout=ENGINE_TURN_TIMEOUT, description="the run ended")
    assert snapshots[-1] == ()
    states: list[str] = []
    for snapshot in snapshots:
        state = "queued" if snapshot and snapshot[0].queued else ("running" if snapshot else "absent")
        if not states or states[-1] != state:
            states.append(state)
    assert states == ["queued", "running", "absent"]
    # drain_turn does not wait for them, but the manager still sees them end.
    await manager.drain_turn()


async def test_a_detached_hook_is_not_registered(hooks_dir: Path, tmp_path: Path) -> None:
    """``detach: true`` hands the process off; iCode cannot see it end, so it is never in a snapshot."""
    marker = tmp_path / "ran"
    script = f"open({str(marker)!r}, 'w').write('x')"
    manager = _manager(
        hooks_dir, _hook("handed-off", HookEvent.AFTER_TOOL_CALL, mode="fire_and_forget", script=script, detach=True)
    )
    snapshots = _record_snapshots(manager)
    await manager.fire(HookEvent.AFTER_TOOL_CALL, _tool_payload())
    assert manager.executions() == ()
    await wait_for(marker.exists, timeout=ENGINE_TURN_TIMEOUT, description="the detached hook ran")
    assert snapshots == []
    assert manager.executions() == ()


# --- Queueing -------------------------------------------------------------------


async def test_a_second_blocking_hook_queues_while_the_only_slot_is_busy(hooks_dir: Path, tmp_path: Path) -> None:
    release_a, release_b = tmp_path / "release-a", tmp_path / "release-b"
    manager = _manager(
        hooks_dir,
        _hook("hook-a", HookEvent.BEFORE_TOOL_CALL, script=_held(release_a), tool_name="a"),
        _hook("hook-b", HookEvent.BEFORE_TOOL_CALL, script=_held(release_b), tool_name="b"),
        max_parallel_hooks=1,
    )
    snapshots = _record_snapshots(manager)
    fire_a = asyncio.create_task(manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool_payload("a")))
    fire_b: asyncio.Task[object] | None = None
    try:
        await _running(manager, "hook-a")
        fire_b = asyncio.create_task(manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool_payload("b")))
        await wait_for(
            lambda: any(view.hook_id == "hook-b" for view in manager.executions()),
            timeout=ENGINE_TURN_TIMEOUT,
            description="hook-b registered",
        )
        by_id = {view.hook_id: view for view in manager.executions()}
        assert by_id["hook-a"].queued is False
        assert by_id["hook-b"].queued is True
        queued_at = by_id["hook-b"].started_at
        # The clock moves on before B takes its slot, so a stamp taken then would differ.
        await wait_for(lambda: time.monotonic() > queued_at, description="the clock moved past B's queueing")

        release_a.touch()
        running_b = await _running(manager, "hook-b")
        # The time B spent queued counts toward its age: the indicator shows a waited-on run once it
        # has lasted a second, queueing included, so a run already showing stays shown as it starts.
        assert running_b.started_at == queued_at
        assert [view.hook_id for view in manager.executions()] == ["hook-b"]
        release_b.touch()
        await asyncio.gather(fire_a, fire_b)
    finally:
        release_a.touch()
        release_b.touch()
        if fire_b is not None:
            await asyncio.gather(fire_b, return_exceptions=True)
    assert manager.executions() == ()

    states: list[str] = []
    for snapshot in snapshots:
        state = next(("queued" if v.queued else "running" for v in snapshot if v.hook_id == "hook-b"), "absent")
        if not states or states[-1] != state:
            states.append(state)
    assert states == ["absent", "queued", "running", "absent"]


async def test_the_hook_running_when_a_blocking_one_queues_is_any_waited_run(hooks_dir: Path, tmp_path: Path) -> None:
    """An async run filling the only slot makes a blocking hook show as queued."""
    release_bg, release_guard = tmp_path / "release-bg", tmp_path / "release-guard"
    manager = _manager(
        hooks_dir,
        _hook("bg", HookEvent.AFTER_TOOL_CALL, mode="async", script=_held(release_bg)),
        _hook("guard", HookEvent.BEFORE_TOOL_CALL, script=_held(release_guard)),
        max_parallel_hooks=1,
    )
    _record_snapshots(manager)
    await manager.fire(HookEvent.AFTER_TOOL_CALL, _tool_payload())
    fire: asyncio.Task[object] | None = None
    try:
        await _running(manager, "bg")
        fire = asyncio.create_task(manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool_payload()))
        await wait_for(
            lambda: any(view.hook_id == "guard" for view in manager.executions()),
            timeout=ENGINE_TURN_TIMEOUT,
            description="guard registered",
        )
        guard = next(view for view in manager.executions() if view.hook_id == "guard")
        assert (guard.queued, guard.waited_at) == (True, "call")
    finally:
        release_bg.touch()
        release_guard.touch()
    assert fire is not None
    await fire
    await manager.drain_turn()
    assert manager.executions() == ()


# --- Cancellation ---------------------------------------------------------------


async def test_a_cancelled_running_blocking_run_is_reported_removed(hooks_dir: Path, release: Path) -> None:
    """A cancelled run (an interrupt) publishes a snapshot without it, from its own task."""
    manager = _manager(
        hooks_dir,
        _hook("held", HookEvent.BEFORE_TOOL_CALL, script=_held(release), tool_name="slow"),
        _hook("other", HookEvent.AFTER_TURN, mode="async", script=_held(release)),
    )
    snapshots = _record_snapshots(manager)
    await manager.fire(HookEvent.AFTER_TURN, {"turn": 1})
    await _running(manager, "other")
    fire = asyncio.create_task(manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool_payload("slow")))
    try:
        await _running(manager, "held")
        published = len(snapshots)
        fire.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await fire
        # Reported by the time the cancelled task has finished.
        assert len(snapshots) > published, "the cancel path publishes the removal"
        assert [view.hook_id for view in snapshots[-1]] == ["other"]
        assert snapshots[-1] == manager.executions()
    finally:
        release.touch()
    await asyncio.wait_for(manager.drain_turn(), ENGINE_TURN_TIMEOUT)
    assert snapshots[-1] == ()


async def test_a_run_cancelled_while_queued_is_reported_removed(hooks_dir: Path, release: Path) -> None:
    manager = _manager(
        hooks_dir,
        _hook("holder", HookEvent.AFTER_TOOL_CALL, mode="fire_and_forget", script=_held(release)),
        _hook("waiting", HookEvent.BEFORE_TOOL_CALL, script="pass"),
        max_parallel_hooks=1,
    )
    snapshots = _record_snapshots(manager)
    await manager.fire(HookEvent.AFTER_TOOL_CALL, _tool_payload())
    fire: asyncio.Task[object] | None = None
    try:
        await _running(manager, "holder")
        fire = asyncio.create_task(manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool_payload()))
        await wait_for(
            lambda: any(v.hook_id == "waiting" and v.queued for v in manager.executions()),
            timeout=ENGINE_TURN_TIMEOUT,
            description="queued behind the holder",
        )
        published = len(snapshots)
        fire.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await fire
        assert len(snapshots) > published
        assert [view.hook_id for view in snapshots[-1]] == ["holder"]
    finally:
        release.touch()
    await wait_for(lambda: manager.executions() == (), timeout=ENGINE_TURN_TIMEOUT, description="the holder ended")
    assert snapshots[-1] == ()


async def test_a_run_cancelled_during_its_own_first_report_is_reported_removed(hooks_dir: Path) -> None:
    """Cancelled while the observer handles its queued registration: the removal is still reported."""
    manager = _manager(hooks_dir, _hook("guard", HookEvent.BEFORE_TOOL_CALL, script="pass"))
    snapshots: Snapshots = []
    first_report = asyncio.Event()
    hold_first = asyncio.Event()

    async def on_change(changed: HookManager) -> None:
        snapshots.append(changed.executions())
        if len(snapshots) == 1:
            first_report.set()
            await hold_first.wait()

    manager.on_change = on_change
    fire = asyncio.create_task(manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool_payload()))
    await wait_for(lambda: first_report.is_set() or fire.done(), description="the first report in progress")
    assert not fire.done()
    assert [view.hook_id for view in snapshots[0]] == ["guard"]
    fire.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await fire
    assert manager.executions() == ()
    assert snapshots[-1] == (), snapshots


async def test_a_non_closing_drain_reports_runs_its_grace_timeout_cancels_and_stays_open(
    hooks_dir: Path, release: Path
) -> None:
    """``drain_session(close=False)`` (session delete): a timed-out async run is reported removed; the manager serves on."""
    manager = _manager(
        hooks_dir,
        _hook("lingering", HookEvent.SESSION_END, mode="async", script=_held(release)),
        _hook("later", HookEvent.AFTER_TURN, script="pass"),
        shutdown_grace_seconds=0.3,
    )
    snapshots = _record_snapshots(manager)
    await manager.fire(HookEvent.SESSION_END, {}, scope="session")
    try:
        await _running(manager, "lingering")
        published = len(snapshots)
        await asyncio.wait_for(manager.drain_session(close=False), ENGINE_TURN_TIMEOUT)
        assert manager.executions() == ()
        assert len(snapshots) > published
        assert snapshots[-1] == ()
    finally:
        release.touch()
    # Still open: a later run is reported as usual.
    after = len(snapshots)
    await manager.fire(HookEvent.AFTER_TURN, {"turn": 2})
    later = snapshots[after:]
    assert any(view.hook_id == "later" for snapshot in later for view in snapshot)
    assert later[-1] == ()
    await manager.drain_session()


@pytest.mark.parametrize("runs", [1, 2])
async def test_a_zero_grace_drain_reports_runs_it_cancels_before_their_first_step(
    hooks_dir: Path, release: Path, runs: int
) -> None:
    """With no grace the drain cancels the run's task before its body runs; the drain reports the removal."""
    hooks = [_hook(f"end-{index}", HookEvent.SESSION_END, mode="async", script=_held(release)) for index in range(runs)]
    manager = _manager(hooks_dir, *hooks, _hook("later", HookEvent.AFTER_TURN, script="pass"), shutdown_grace_seconds=0)
    snapshots = _record_snapshots(manager)
    try:
        await manager.fire(HookEvent.SESSION_END, {}, scope="session")
        # No await in between: the drain gets there before the runs' tasks take a step.
        await asyncio.wait_for(manager.drain_session(close=False), ENGINE_TURN_TIMEOUT)
        assert manager.executions() == ()
        assert snapshots, "the registrations were reported"
        assert {view.hook_id for snapshot in snapshots for view in snapshot} == {f"end-{i}" for i in range(runs)}
        assert snapshots[-1] == ()
    finally:
        release.touch()
    # Still open: a later run is reported normally.
    after = len(snapshots)
    await manager.fire(HookEvent.AFTER_TURN, {"turn": 1})
    later = snapshots[after:]
    assert any(view.hook_id == "later" for snapshot in later for view in snapshot)
    assert later[-1] == ()
    assert manager.executions() == ()
    await manager.drain_session()


async def test_a_drain_reports_a_session_run_cancelled_before_it_ever_ran(hooks_dir: Path, release: Path) -> None:
    """The drain sweeps tasks in its scopes that ended cancelled, even when someone else cancelled them."""
    manager = _manager(
        hooks_dir,
        _hook("never-ran", HookEvent.SESSION_END, mode="async", script=_held(release)),
        _hook("later", HookEvent.AFTER_TURN, script="pass"),
    )
    snapshots = _record_snapshots(manager)
    try:
        await manager.fire(HookEvent.SESSION_END, {}, scope="session")
        assert [view.hook_id for view in manager.executions()] == ["never-ran"]
        # Cancelled before its first step: no await between this cancel and the drain.
        (tracked,) = manager._inflight
        tracked.task.cancel()
        await asyncio.wait_for(manager.drain_session(close=False), ENGINE_TURN_TIMEOUT)
        assert manager.executions() == ()
        assert snapshots[-1] == ()
    finally:
        release.touch()
    after = len(snapshots)
    await manager.fire(HookEvent.AFTER_TURN, {"turn": 1})
    assert any(view.hook_id == "later" for snapshot in snapshots[after:] for view in snapshot)
    assert snapshots[-1] == ()
    await manager.drain_session()


async def test_a_drain_adds_no_report_for_a_run_cancelled_after_its_body_ran(hooks_dir: Path, release: Path) -> None:
    """The cancelled run's own ``finally`` already reported its removal; the drain's sweep adds nothing."""
    manager = _manager(hooks_dir, _hook("ran", HookEvent.SESSION_END, mode="async", script=_held(release)))
    snapshots = _record_snapshots(manager)
    try:
        await manager.fire(HookEvent.SESSION_END, {}, scope="session")
        await _running(manager, "ran")
        (tracked,) = manager._inflight
        tracked.task.cancel()
        await wait_for(tracked.task.done, timeout=ENGINE_TURN_TIMEOUT, description="the cancel settled")
        assert manager.executions() == ()
        assert snapshots[-1] == ()
        before_drain = len(snapshots)
        await asyncio.wait_for(manager.drain_session(close=False), ENGINE_TURN_TIMEOUT)
        assert len(snapshots) == before_drain, snapshots[before_drain:]
        assert manager.executions() == ()
    finally:
        release.touch()
    await manager.drain_session()


async def test_a_turn_drain_during_an_async_runs_first_report_waits_for_the_run(hooks_dir: Path, release: Path) -> None:
    """The run's task is tracked before it is reported: a drain the report lets in still waits for it."""
    manager = _manager(hooks_dir, _hook("bg", HookEvent.AFTER_TURN, mode="async", script=_held(release)))
    snapshots: Snapshots = []
    first_report = asyncio.Event()
    hold_first = asyncio.Event()

    async def on_change(changed: HookManager) -> None:
        snapshots.append(changed.executions())
        if len(snapshots) == 1:
            first_report.set()
            await hold_first.wait()

    manager.on_change = on_change
    fire = asyncio.create_task(manager.fire(HookEvent.AFTER_TURN, {}, scope="turn"))
    drain: asyncio.Task[tuple[str, ...]] | None = None
    try:
        await wait_for(lambda: first_report.is_set() or fire.done(), description="the first report in progress")
        assert not fire.done()
        drain = asyncio.create_task(manager.drain_turn())
        assert not await wait_until(drain.done, timeout=0.5), "the drain returned while the run was going on"
        hold_first.set()
        await asyncio.wait_for(fire, ENGINE_TURN_TIMEOUT)
        assert not drain.done()
        release.touch()
        await asyncio.wait_for(drain, ENGINE_TURN_TIMEOUT)
        assert manager.executions() == ()
        assert snapshots[-1] == ()
    finally:
        hold_first.set()
        release.touch()
        if drain is not None and not drain.done():
            drain.cancel()
        await asyncio.gather(fire, *([drain] if drain is not None else []), return_exceptions=True)
    await manager.drain_session()


async def test_a_cancelled_turn_drain_reports_runs_it_cancelled_before_their_first_step(
    hooks_dir: Path, release: Path
) -> None:
    """An interrupt during ``drain_turn``: the drain's cancel reaches runs that never took a step.

    Their entries go, and their spans close as cancelled.
    """
    manager = _manager(hooks_dir, _hook("bg", HookEvent.AFTER_TURN, mode="async", script=_held(release)))
    snapshots = _record_snapshots(manager)
    sink = FakeSink()
    current = asyncio.current_task()
    assert current is not None
    try:
        with trajectory_scope(make_context(sink)):
            # Queued ahead of the run's first step, which ``fire`` queues without suspending.
            asyncio.get_running_loop().call_soon(current.cancel)
            await manager.fire(HookEvent.AFTER_TURN, {}, scope="turn")
        assert [view.hook_id for view in manager.executions()] == ["bg"]
        (tracked,) = manager._inflight
        with pytest.raises(asyncio.CancelledError):
            await manager.drain_turn()
        current.uncancel()
        assert tracked.task.cancelled()
        assert manager.executions() == ()
        assert snapshots[-1] == ()
        assert sink.only(EventType.HOOK_OPERATION_FINISHED).payload["outcome"] == "cancelled"
        sink.assert_operations_settled()
    finally:
        release.touch()
    await manager.drain_session()


@pytest.mark.parametrize(
    ("mode", "scope", "event"),
    [("fire_and_forget", "turn", HookEvent.AFTER_TOOL_CALL), ("async", "detached", HookEvent.USER_INTERRUPT)],
    ids=["fire-and-forget", "detached-async"],
)
async def test_an_undrained_run_cancelled_before_its_first_step_leaves_no_entry(
    hooks_dir: Path, release: Path, mode: str, scope: str, event: HookEvent
) -> None:
    """No drain sweeps these runs: the task's own end drops the entry and closes the span as cancelled.

    The dropped entry goes unreported; the next report leaves it out.
    """
    manager = _manager(
        hooks_dir,
        _hook("observer", event, mode=mode, script=_held(release)),
        _hook("later", HookEvent.AFTER_TURN, script="pass"),
    )
    snapshots = _record_snapshots(manager)
    sink = FakeSink()
    try:
        with trajectory_scope(make_context(sink)):
            await manager.fire(event, _tool_payload(), scope=scope)
        assert [view.hook_id for view in manager.executions()] == ["observer"]
        # No await between the fire and this cancel: the task never took a step.
        (tracked,) = manager._inflight
        tracked.task.cancel()
        await wait_for(lambda: manager.executions() == (), description="the entry dropped")
        assert tracked.task.cancelled()
        assert tracked not in manager._inflight
        assert sink.only(EventType.HOOK_OPERATION_FINISHED).payload["outcome"] == "cancelled"
        sink.assert_operations_settled()
    finally:
        release.touch()
    after = len(snapshots)
    await manager.fire(HookEvent.AFTER_TURN, {"turn": 1})
    assert any(view.hook_id == "later" for snapshot in snapshots[after:] for view in snapshot)
    assert all(view.hook_id != "observer" for snapshot in snapshots[after:] for view in snapshot)
    assert snapshots[-1] == ()
    await manager.drain_session()


async def test_a_closing_session_drain_ends_with_an_empty_snapshot(hooks_dir: Path, release: Path) -> None:
    """A run the grace period cancels at close: what the UI shows ends empty."""
    manager = _manager(
        hooks_dir,
        _hook("lingering", HookEvent.SESSION_START, mode="async", script=_held(release)),
        shutdown_grace_seconds=0.3,
    )
    snapshots = _record_snapshots(manager)
    await manager.fire(HookEvent.SESSION_START, {}, scope="session")
    await _running(manager, "lingering")
    await asyncio.wait_for(manager.drain_session(), ENGINE_TURN_TIMEOUT)
    assert manager.executions() == ()
    assert snapshots[-1] == ()


def _pid_then_held(pid_file: Path, release: Path) -> str:
    """A hook that records its pid, then waits for *release*."""
    return textwrap.dedent(
        f"""
        import os, time
        open({str(pid_file)!r}, "w").write(str(os.getpid()))
        while not os.path.exists({str(release)!r}):
            time.sleep(0.01)
        """
    )


async def _alive_child(pid_file: Path) -> psutil.Process:
    await wait_for(
        lambda: pid_file.exists() and pid_file.read_text(encoding="utf-8").strip(),
        timeout=ENGINE_TURN_TIMEOUT,
        description="the hook process started",
    )
    return psutil.Process(int(pid_file.read_text(encoding="utf-8")))


async def test_a_fire_and_forget_run_ending_after_close_publishes_nothing(hooks_dir: Path, tmp_path: Path) -> None:
    """Once closed and cleared, a late background run's end must not overwrite a newer manager's snapshot."""
    release, pid_file = tmp_path / "release-ff", tmp_path / "ff.pid"
    manager = _manager(
        hooks_dir,
        _hook("late-ff", HookEvent.AFTER_TOOL_CALL, mode="fire_and_forget", script=_pid_then_held(pid_file, release)),
    )
    snapshots = _record_snapshots(manager)
    await manager.fire(HookEvent.AFTER_TOOL_CALL, _tool_payload())
    try:
        child = await _alive_child(pid_file)
        await _running(manager, "late-ff")
        await asyncio.wait_for(manager.drain_session(close=True), ENGINE_TURN_TIMEOUT)
        assert manager.executions() == ()
        assert snapshots[-1] == (), "the close publishes the cleared set"
        closed_at = len(snapshots)
    finally:
        release.touch()
    # The run really ends: its process exits (unless the close already stopped it).
    await wait_for(lambda: not child.is_running(), timeout=ENGINE_TURN_TIMEOUT, description="the late run ended")
    assert not await wait_until(lambda: len(snapshots) > closed_at, timeout=1.0), snapshots[closed_at:]
    assert manager.executions() == ()


async def test_a_detached_blocking_interrupt_hook_ending_after_close_publishes_nothing(
    hooks_dir: Path, tmp_path: Path
) -> None:
    release, pid_file = tmp_path / "release-int", tmp_path / "int.pid"
    manager = _manager(hooks_dir, _hook("late-int", HookEvent.USER_INTERRUPT, script=_pid_then_held(pid_file, release)))
    snapshots = _record_snapshots(manager)
    fire = asyncio.create_task(manager.fire(HookEvent.USER_INTERRUPT, {}, scope="detached"))
    drain: asyncio.Task[None] | None = None
    try:
        child = await _alive_child(pid_file)
        await _running(manager, "late-int")
        drain = asyncio.create_task(manager.drain_session(close=True))
        await wait_for(
            lambda: drain.done() or fire.done(), timeout=ENGINE_TURN_TIMEOUT, description="the close returned"
        )
        if drain.done():
            await drain
        assert drain.done(), "closing does not wait for a detached interrupt observer"
        assert not fire.done(), "the observer is still running after the close"
        assert manager.executions() == ()
        assert snapshots[-1] == ()
        closed_at = len(snapshots)
    finally:
        release.touch()
    # Judged by its own task: the detached fire returns once the hook has ended.
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(fire, ENGINE_TURN_TIMEOUT)
    if drain is not None:
        await asyncio.wait_for(drain, ENGINE_TURN_TIMEOUT)
    await wait_for(lambda: not child.is_running(), timeout=ENGINE_TURN_TIMEOUT, description="the process ended")
    assert len(snapshots) == closed_at, snapshots[closed_at:]
    assert manager.executions() == ()


async def test_a_queued_background_run_that_takes_its_slot_after_close_publishes_nothing(
    hooks_dir: Path, release: Path
) -> None:
    """After the close cleared and reported the set, neither taking a slot nor ending reports again."""
    ran = hooks_dir.parent / "queued-ran"
    manager = _manager(
        hooks_dir,
        _hook("holder", HookEvent.AFTER_TOOL_CALL, mode="fire_and_forget", script=_held(release), tool_name="a"),
        _hook(
            "queued",
            HookEvent.AFTER_TOOL_CALL,
            mode="fire_and_forget",
            script=f"open({str(ran)!r}, 'w').write('ran')",
            tool_name="b",
        ),
        max_parallel_hooks=1,
    )
    snapshots = _record_snapshots(manager)
    try:
        await manager.fire(HookEvent.AFTER_TOOL_CALL, _tool_payload("a"))
        await _running(manager, "holder")
        await manager.fire(HookEvent.AFTER_TOOL_CALL, _tool_payload("b"))
        await wait_for(
            lambda: any(view.hook_id == "queued" and view.queued for view in manager.executions()),
            timeout=ENGINE_TURN_TIMEOUT,
            description="the second run queued behind the holder",
        )
        await asyncio.wait_for(manager.drain_session(), ENGINE_TURN_TIMEOUT)
        assert manager.executions() == ()
        assert snapshots[-1] == ()
        closed_at = len(snapshots)
    finally:
        release.touch()
    # Both runs finish for real: the holder ends, the queued one takes the slot, then ends too.
    await wait_for(lambda: not manager._inflight, timeout=ENGINE_TURN_TIMEOUT, description="both runs ended")
    assert ran.exists(), "the queued run took the slot and ran after the close"
    assert len(snapshots) == closed_at, snapshots[closed_at:]
    assert snapshots[-1] == ()


async def test_a_queued_interrupt_observer_that_takes_its_slot_after_close_publishes_nothing(
    hooks_dir: Path, release: Path
) -> None:
    ran = hooks_dir.parent / "observer-ran"
    manager = _manager(
        hooks_dir,
        _hook("holder", HookEvent.AFTER_TOOL_CALL, mode="fire_and_forget", script=_held(release)),
        _hook("observer", HookEvent.USER_INTERRUPT, script=f"open({str(ran)!r}, 'w').write('ran')"),
        max_parallel_hooks=1,
    )
    snapshots = _record_snapshots(manager)
    fire: asyncio.Task[object] | None = None
    try:
        await manager.fire(HookEvent.AFTER_TOOL_CALL, _tool_payload())
        await _running(manager, "holder")
        fire = asyncio.create_task(manager.fire(HookEvent.USER_INTERRUPT, {}, scope="detached"))
        await wait_for(
            lambda: any(view.hook_id == "observer" and view.queued for view in manager.executions()),
            timeout=ENGINE_TURN_TIMEOUT,
            description="the observer queued behind the holder",
        )
        await asyncio.wait_for(manager.drain_session(), ENGINE_TURN_TIMEOUT)
        assert manager.executions() == ()
        assert snapshots[-1] == ()
        closed_at = len(snapshots)
    finally:
        release.touch()
    assert fire is not None
    # Judged by its own task: the detached fire returns once the observer has run and ended.
    await asyncio.wait_for(fire, ENGINE_TURN_TIMEOUT)
    await wait_for(lambda: not manager._inflight, timeout=ENGINE_TURN_TIMEOUT, description="the holder ended")
    assert ran.exists(), "the observer took the slot and ran after the close"
    assert len(snapshots) == closed_at, snapshots[closed_at:]
    assert snapshots[-1] == ()


# --- The view itself -----------------------------------------------------------


def test_the_view_carries_only_what_the_indicator_uses() -> None:
    """The run's details live in the run log; the view only names the run there (``run_id``)."""
    assert [field.name for field in dataclasses.fields(HookExecutionView)] == [
        "hook_id",
        "waited_at",
        "queued",
        "started_at",
        "run_id",
    ]


# --- Callbacks only observe -----------------------------------------------------


async def _modify_and_trace(manager: HookManager) -> tuple[HookDecision, list[str], object]:
    sink = FakeSink()
    with trajectory_scope(make_context(sink)):
        decision = await manager.fire(HookEvent.BEFORE_TOOL_CALL, _tool_payload())
    finished = sink.only(EventType.HOOK_OPERATION_FINISHED).payload["outcome"]
    return decision, [str(kind) for kind in sink.event_types], finished


async def test_raising_callbacks_change_neither_decision_nor_trajectory(hooks_dir: Path) -> None:
    result = '{"action": "modify", "args_override": {"cmd": "safe"}}'
    script = f"import os\nopen(os.environ['CHRYS_HOOK_RESULT'], 'w').write({result!r})\n"
    control = _manager(hooks_dir, _hook("rewrite", HookEvent.BEFORE_TOOL_CALL, script=script))
    expected = await _modify_and_trace(control)

    observed = _manager(hooks_dir, _hook("rewrite", HookEvent.BEFORE_TOOL_CALL, script=script))
    calls: list[str] = []

    async def broken_change(_manager: HookManager) -> None:
        calls.append("change")
        raise RuntimeError("subscriber exploded")

    observed.on_change = broken_change
    actual = await _modify_and_trace(observed)
    assert calls
    assert actual[0] == expected[0]
    assert actual[0].args_override == {"cmd": "safe"}
    assert actual[1:] == expected[1:]
    assert observed.executions() == ()
