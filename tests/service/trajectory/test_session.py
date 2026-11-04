# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``SessionTrajectory`` contract: lazy activation, prelude, emit paths, close, resume, lease."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import threading
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from chrys.foundation import platform as platform_module
from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import SecureFileError
from chrys.foundation.trajectory import writer as writer_module
from chrys.foundation.trajectory.context import TrajectoryContext
from chrys.foundation.trajectory.envelope import (
    INT64_MAX,
    SCHEMA_VERSION,
    SYSTEM_ACTOR,
    ActorKind,
    ActorRole,
    EventDraft,
    TrajectoryEvent,
    encode_json_line,
    monotonic_now_ns,
)
from chrys.foundation.trajectory.event_types import CoverageReason, EventType, GapReason, RuntimeFinishReason
from chrys.foundation.trajectory.fingerprint import DOMAIN_WORKSPACE, FINGERPRINT_KEY_BYTES, fingerprint_text
from chrys.foundation.trajectory.ids import is_valid_analytics_id
from chrys.foundation.trajectory.lease import WriterLease
from chrys.foundation.trajectory.reader import read_trajectory
from chrys.foundation.trajectory.writer import EmitResult, FdWriteBackend, TrajectoryWriter, WriterState
from chrys.service.trajectory import session as session_module
from chrys.service.trajectory.session import (
    SessionStartInfo,
    SessionTrajectory,
    TrajectoryDisabledReason,
    runtime_environment,
    trajectory_dir,
    trajectory_events_path,
    trajectory_lease_path,
)
from tests.support.secure_files import plant_owner_only_bytes
from tests.support.symlinks import symlink_or_skip
from tests.support.trajectory_invariants import assert_trajectory_accounted
from tests.support.waiting import DEFAULT_WAIT_TIMEOUT as WAIT_TIMEOUT
from tests.support.waiting import wait_until

SESSION_ID = "12345678-1234-1234-1234-123456789abc"
START_INFO = SessionStartInfo(
    primary_cwd="/work/project",
    agent_profile_fingerprint="a" * 64,
    model_profile_fingerprint="b" * 64,
)

TrajectoryFactory = Callable[..., SessionTrajectory]


@pytest.fixture
async def make_trajectory(tmp_path: Path) -> AsyncIterator[TrajectoryFactory]:
    """Build recorders rooted under ``tmp_path`` and close every unclosed one at teardown."""
    created: list[SessionTrajectory] = []

    def _factory(
        session_dir: Path | None = None,
        *,
        session_id: str = SESSION_ID,
        config_dir: Path | None = None,
        session_start_info: Callable[[], SessionStartInfo | None] | None = lambda: START_INFO,
        **kwargs: Any,
    ) -> SessionTrajectory:
        trajectory = SessionTrajectory(
            session_id=session_id,
            session_dir=session_dir,
            config_dir=config_dir if config_dir is not None else tmp_path / "config",
            session_start_info=session_start_info,
            **kwargs,
        )
        created.append(trajectory)
        return trajectory

    yield _factory

    for trajectory in reversed(created):
        if not trajectory.is_closed:
            await trajectory.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)


def _draft(n: int) -> EventDraft:
    return EventDraft(event_type=EventType.TURN_STARTED, payload={"turn_number": n})


def _events(session_dir: Path) -> list[TrajectoryEvent]:
    return read_trajectory(trajectory_events_path(session_dir)).events


def _types(events: list[TrajectoryEvent]) -> list[str]:
    return [event.event_type for event in events]


def _line_count(session_dir: Path) -> int:
    return trajectory_events_path(session_dir).read_bytes().count(b"\n")


# ------------------------------------------------------------------ disabled


async def test_no_session_dir_is_disabled_without_touching_the_filesystem(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    config_dir = tmp_path / "config"
    reports: list[str] = []
    trajectory = make_trajectory(None, config_dir=config_dir, on_activation_failed=reports.append)
    assert trajectory.session_dir is None
    assert trajectory.is_active is False
    assert trajectory.disabled_reason is None  # activation is lazy: nothing decided yet
    assert trajectory.runtime_id is None
    assert trajectory.branch_id is None
    assert trajectory.fingerprint_key is None
    assert trajectory.last_assigned_sequence() == 0

    assert await trajectory.emit(_draft(1)) is EmitResult.DEGRADED
    assert trajectory.is_active is False
    assert trajectory.disabled_reason == TrajectoryDisabledReason.NO_SESSION_DIR
    assert trajectory.writer is None
    assert await trajectory.checkpoint() is EmitResult.DEGRADED
    # A disabled recorder is a no-op on every path.
    assert trajectory.emit_blocking(_draft(2)) is EmitResult.DEGRADED
    trajectory.emit_soon(_draft(3))
    assert list(tmp_path.iterdir()) == []  # not even the key
    assert not config_dir.exists()

    context = trajectory.context(turn_id="t" * 32, run_operation_id="r" * 32)
    assert isinstance(context, TrajectoryContext)
    assert context.sink is trajectory
    assert context.session_id == SESSION_ID
    assert context.actor == trajectory.main_actor
    assert context.turn_id == "t" * 32
    assert context.run_operation_id == "r" * 32
    # Revision chains are owned by the recorder: every derived context shares one registry.
    assert trajectory.context().revisions is context.revisions

    assert await trajectory.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN) is True
    assert trajectory.is_closed
    assert trajectory.disabled_reason == TrajectoryDisabledReason.NO_SESSION_DIR
    # Nothing to record into is how such a session is meant to run, not a failure to report.
    assert reports == []


async def test_close_before_activation_disables_with_closed_reason(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    reports: list[str] = []
    trajectory = make_trajectory(session_dir, on_activation_failed=reports.append)
    assert await trajectory.close(reason=RuntimeFinishReason.SESSION_SWITCH) is True
    assert trajectory.is_closed
    assert trajectory.disabled_reason == TrajectoryDisabledReason.CLOSED
    assert await trajectory.emit(_draft(1)) is EmitResult.DEGRADED
    assert reports == []
    assert not trajectory_dir(session_dir).exists()


async def test_activation_failure_disables_and_never_raises(tmp_path: Path, make_trajectory: TrajectoryFactory) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file in the way")
    reports: list[str] = []
    trajectory = make_trajectory(blocker / "session", on_activation_failed=reports.append)
    assert await trajectory.emit(_draft(1)) is EmitResult.DEGRADED
    assert trajectory.disabled_reason == TrajectoryDisabledReason.ACTIVATION_FAILED
    assert reports == [TrajectoryDisabledReason.ACTIVATION_FAILED]
    assert trajectory.is_active is False
    assert trajectory.writer is None


async def test_unusable_key_disables_and_releases_the_lease(tmp_path: Path, make_trajectory: TrajectoryFactory) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    config_path = tmp_path / "config-is-a-file"
    config_path.write_text("")
    reports: list[str] = []
    trajectory = make_trajectory(session_dir, config_dir=config_path, on_activation_failed=reports.append)
    assert await trajectory.emit(_draft(1)) is EmitResult.DEGRADED
    assert trajectory.disabled_reason == TrajectoryDisabledReason.KEY_UNAVAILABLE
    assert await trajectory.emit(_draft(2)) is EmitResult.DEGRADED
    assert reports == [TrajectoryDisabledReason.KEY_UNAVAILABLE]
    assert trajectory.fingerprint_key is None
    assert not trajectory_events_path(session_dir).exists()
    # The lease taken before the key was loaded is given back on disable.
    assert WriterLease.is_held_elsewhere(trajectory_lease_path(session_dir)) is False


def _log_readable_by_others(session_dir: Path) -> None:
    if get_platform().is_windows:
        pytest.skip("POSIX mode bits")
    trajectory_dir(session_dir).mkdir()
    plant_owner_only_bytes(trajectory_events_path(session_dir), b"")
    trajectory_events_path(session_dir).chmod(0o644)


def _log_is_a_link(session_dir: Path) -> None:
    trajectory_dir(session_dir).mkdir()
    elsewhere = session_dir.parent / "elsewhere.jsonl"
    plant_owner_only_bytes(elsewhere, b"")
    symlink_or_skip(trajectory_events_path(session_dir), elsewhere)


def _trajectory_folder_is_a_link(session_dir: Path) -> None:
    elsewhere = session_dir.parent / "elsewhere"
    elsewhere.mkdir()
    symlink_or_skip(trajectory_dir(session_dir), elsewhere, target_is_directory=True)


def _trajectory_folder_is_a_dangling_link(session_dir: Path) -> None:
    symlink_or_skip(trajectory_dir(session_dir), session_dir.parent / "missing", target_is_directory=True)


def _session_folder_is_read_only(session_dir: Path) -> None:
    if get_platform().is_windows or os.geteuid() == 0:
        pytest.skip("POSIX mode bits, which root ignores")
    session_dir.chmod(0o500)


@pytest.mark.parametrize(
    "prepare",
    [
        _log_readable_by_others,
        _log_is_a_link,
        _trajectory_folder_is_a_link,
        _trajectory_folder_is_a_dangling_link,
        _session_folder_is_read_only,
    ],
    ids=[
        "log-readable-by-others",
        "log-is-a-link",
        "trajectory-folder-is-a-link",
        "trajectory-folder-is-a-dangling-link",
        "session-folder-is-read-only",
    ],
)
async def test_files_that_refuse_this_user_are_reported_as_permission(
    tmp_path: Path, make_trajectory: TrajectoryFactory, prepare: Callable[[Path], None]
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    prepare(session_dir)
    reports: list[str] = []
    trajectory = make_trajectory(session_dir, on_activation_failed=reports.append)
    try:
        assert await trajectory.emit(_draft(1)) is EmitResult.DEGRADED
        assert await trajectory.emit(_draft(2)) is EmitResult.DEGRADED
    finally:
        session_dir.chmod(0o700)
    assert trajectory.disabled_reason == TrajectoryDisabledReason.PERMISSION
    assert reports == [TrajectoryDisabledReason.PERMISSION]
    assert trajectory.writer is None


@pytest.mark.parametrize(
    ("windows", "error", "reason"),
    [
        (False, SecureFileError("Secure file is not owned by the current user."), "permission"),
        (False, SecureFileError(errno.EROFS, "Unable to open owner-only append file."), "permission"),
        (False, SecureFileError(errno.EMFILE, "Unable to open owner-only append file."), "activation_failed"),
        (False, OSError(errno.ENOSPC, "No space left on device"), "activation_failed"),
        (False, TimeoutError(), "activation_failed"),
        (True, SecureFileError("Secure file is not owned by the current Windows user."), "permission"),
        (True, SecureFileError(5, "Unable to open owner-only Windows file."), "permission"),
        # ERROR_WRITE_PROTECT: a read-only volume.
        (True, SecureFileError(19, "Unable to open owner-only Windows file."), "permission"),
        # ERROR_NETWORK_ACCESS_DENIED: a share that refuses this user.
        (True, SecureFileError(65, "Unable to open owner-only Windows file."), "permission"),
        # Win32 ERROR_INVALID_DATA shares its number with POSIX EACCES.
        (True, SecureFileError(13, "Unable to open owner-only Windows file."), "activation_failed"),
    ],
)
def test_an_activation_error_names_permission_only_when_the_files_refused_this_user(
    monkeypatch: pytest.MonkeyPatch, windows: bool, error: Exception, reason: str
) -> None:
    monkeypatch.setattr(platform_module, "get_platform", lambda: SimpleNamespace(is_windows=windows))
    assert session_module._activation_failure_reason(error) == reason


# ---------------------------------------------------------------- activation


async def test_first_emit_activates_and_writes_the_prelude(tmp_path: Path, make_trajectory: TrajectoryFactory) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    trajectory = make_trajectory(session_dir)
    assert not trajectory_dir(session_dir).exists()

    draft = _draft(1)
    assert await trajectory.emit(draft) is EmitResult.WRITTEN

    assert trajectory.is_active
    assert trajectory.disabled_reason is None
    assert trajectory_events_path(session_dir).is_file()
    assert trajectory_lease_path(session_dir).is_file()
    assert WriterLease.is_held_elsewhere(trajectory_lease_path(session_dir)) is True
    assert is_valid_analytics_id(trajectory.runtime_id)
    assert is_valid_analytics_id(trajectory.branch_id)
    assert trajectory.fingerprint_key is not None
    assert len(trajectory.fingerprint_key) == FINGERPRINT_KEY_BYTES
    assert trajectory.coverage_reason == CoverageReason.SESSION_STARTED
    assert isinstance(trajectory.writer, TrajectoryWriter)
    assert trajectory.writer.state is WriterState.ACTIVE
    assert trajectory.worker_stuck is False
    assert trajectory.last_assigned_sequence() == 4

    events = _events(session_dir)
    assert _types(events) == [
        EventType.COVERAGE_STARTED,
        EventType.RUNTIME_STARTED,
        EventType.SESSION_STARTED,
        EventType.TURN_STARTED,
    ]
    assert [event.sequence for event in events] == [1, 2, 3, 4]
    assert_trajectory_accounted(read_trajectory(trajectory_events_path(session_dir)))
    assert {event.runtime_id for event in events} == {trajectory.runtime_id}
    assert {event.branch_id for event in events} == {trajectory.branch_id}
    assert {event.session_id for event in events} == {SESSION_ID}
    assert {event.coverage_id for event in events} == {trajectory.writer.coverage_id}

    coverage, started, session_started, turn = events
    assert coverage.actor == SYSTEM_ACTOR
    assert coverage.payload == {
        "coverage_id": trajectory.writer.coverage_id,
        "runtime_id": trajectory.runtime_id,
        "coverage_reason": CoverageReason.SESSION_STARTED,
    }
    environment = runtime_environment()
    assert set(environment) == {"app_version", "os_name", "arch"}
    assert started.actor == SYSTEM_ACTOR
    assert started.payload == environment
    assert session_started.actor == trajectory.main_actor
    assert session_started.actor.kind == ActorKind.AGENT
    assert session_started.actor.role == ActorRole.MAIN
    assert session_started.payload == {
        **environment,
        "workspace_fingerprint": fingerprint_text(trajectory.fingerprint_key, DOMAIN_WORKSPACE, START_INFO.primary_cwd),
        "agent_profile_fingerprint": START_INFO.agent_profile_fingerprint,
        "model_profile_fingerprint": START_INFO.model_profile_fingerprint,
    }
    assert turn.event_id == draft.event_id
    assert turn.payload == {"turn_number": 1}
    assert turn.actor == SYSTEM_ACTOR  # the draft's default actor is kept as-is


async def test_the_prelude_is_stamped_when_the_recorder_was_bound(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    trajectory = make_trajectory(session_dir)
    bound_by = monotonic_now_ns()
    await asyncio.sleep(0.01)

    # Activation happens here, well after the binding above.
    assert await trajectory.emit(_draft(1)) is EmitResult.WRITTEN

    events = _events(session_dir)
    prelude, turn = events[:-1], events[-1]
    # The log opens on the first event, so an activation-time stamp would
    # report the runtime as starting when the first turn did.
    assert all(event.monotonic_ns <= bound_by for event in prelude)
    assert turn.monotonic_ns > bound_by
    assert all(event.occurred_at <= turn.occurred_at for event in prelude)


async def test_session_started_without_start_info_carries_environment_only(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    trajectory = make_trajectory(session_dir, session_start_info=None)
    await trajectory.emit(_draft(1))
    events = _events(session_dir)
    session_started = events[2]
    assert session_started.event_type == EventType.SESSION_STARTED
    assert session_started.payload == runtime_environment()


async def test_legacy_session_with_persisted_state_records_feature_introduced(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    """A session that already has state but no log gets coverage reason feature_introduced and no session.started."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "session.json").write_text("{}")
    trajectory = make_trajectory(session_dir)
    await trajectory.emit(_draft(1))
    assert trajectory.coverage_reason == CoverageReason.FEATURE_INTRODUCED
    events = _events(session_dir)
    assert _types(events) == [EventType.COVERAGE_STARTED, EventType.RUNTIME_STARTED, EventType.TURN_STARTED]
    assert events[0].payload["coverage_reason"] == CoverageReason.FEATURE_INTRODUCED


async def test_persisted_state_probe_is_injectable(tmp_path: Path, make_trajectory: TrajectoryFactory) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "session.json").write_text("{}")
    probed: list[Path] = []

    def probe(path: Path) -> bool:
        probed.append(path)
        return False

    trajectory = make_trajectory(session_dir, persisted_state_probe=probe)
    await trajectory.emit(_draft(1))
    assert probed == [session_dir]
    assert trajectory.coverage_reason == CoverageReason.SESSION_STARTED
    assert EventType.SESSION_STARTED in _types(_events(session_dir))


async def test_actor_helpers_and_main_actor_draft(make_trajectory: TrajectoryFactory) -> None:
    trajectory = make_trajectory(None)
    draft = trajectory.main_actor_draft(
        EventType.TURN_FINISHED, payload={"end_reason": "completed"}, turn_id="t" * 32, operation_id="o" * 32
    )
    assert draft.event_type == EventType.TURN_FINISHED
    assert draft.actor == trajectory.main_actor
    assert draft.payload == {"end_reason": "completed"}
    assert draft.turn_id == "t" * 32
    assert draft.operation_id == "o" * 32
    assert draft.parent_operation_id is None
    assert trajectory.main_actor_draft(EventType.TURN_SUSPENDED).payload == {}

    side = trajectory.side_call_actor(ActorRole.COMPACTION)
    assert (side.kind, side.role) == (ActorKind.SIDE_CALL, ActorRole.COMPACTION)
    assert is_valid_analytics_id(side.actor_id)
    sub = trajectory.sub_agent_actor("abcdef012345")
    assert (sub.kind, sub.role, sub.invocation_id) == (ActorKind.AGENT, ActorRole.SUB_AGENT, "abcdef012345")
    assert is_valid_analytics_id(sub.actor_id)
    assert len({trajectory.main_actor.actor_id, side.actor_id, sub.actor_id}) == 3


# ---------------------------------------------------------------- emit paths


async def test_emit_soon_keeps_sequence_order_ahead_of_a_later_emit(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    trajectory = make_trajectory(session_dir)
    await trajectory.emit(_draft(0))
    trajectory.emit_soon(_draft(1))
    trajectory.emit_soon(_draft(2))
    assert await trajectory.emit(_draft(3)) is EmitResult.WRITTEN
    events = [event for event in _events(session_dir) if event.event_type == EventType.TURN_STARTED]
    assert [event.payload["turn_number"] for event in events] == [0, 1, 2, 3]
    assert [event.sequence for event in events] == [4, 5, 6, 7]


async def test_emit_soon_before_activation_activates_in_the_background(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    trajectory = make_trajectory(session_dir)
    trajectory.emit_soon(_draft(1))
    assert await wait_until(
        lambda: trajectory.is_active and EventType.TURN_STARTED in _types(_events(session_dir)),
    )
    events = _events(session_dir)
    assert _types(events)[-1] == EventType.TURN_STARTED
    assert events[-1].sequence == 4


async def test_emit_blocking_from_a_worker_thread(tmp_path: Path, make_trajectory: TrajectoryFactory) -> None:
    """``emit_blocking`` is for threads without an event loop; it can also activate the recorder."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    trajectory = make_trajectory(session_dir)
    assert await asyncio.to_thread(trajectory.emit_blocking, _draft(1)) is EmitResult.WRITTEN
    assert trajectory.is_active
    assert await trajectory.emit(_draft(2)) is EmitResult.WRITTEN
    assert await asyncio.to_thread(trajectory.emit_blocking, _draft(3)) is EmitResult.WRITTEN
    turns = [event for event in _events(session_dir) if event.event_type == EventType.TURN_STARTED]
    assert [event.payload["turn_number"] for event in turns] == [1, 2, 3]
    assert [event.sequence for event in turns] == [4, 5, 6]


async def test_a_first_emit_cancelled_while_the_log_opens_still_writes_its_event(
    tmp_path: Path, make_trajectory: TrajectoryFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening the log is the one window where a cancelled producer could lose
    its line: the pass is interrupted (shutdown, user interrupt) while the
    first ``turn.started`` is still activating, and the close that follows
    would write that turn's terminal against a start nothing recorded."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    trajectory = make_trajectory(session_dir)
    activating = threading.Event()
    release = threading.Event()
    activate = trajectory._activate

    def _slow_activate() -> None:
        activating.set()
        release.wait(WAIT_TIMEOUT)
        activate()

    monkeypatch.setattr(trajectory, "_activate", _slow_activate)

    emitting = asyncio.ensure_future(trajectory.emit(_draft(1)))
    assert await wait_until(activating.is_set)
    emitting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await emitting
    release.set()

    assert await wait_until(lambda: EventType.TURN_STARTED in _types(_events(session_dir)))
    events = _events(session_dir)
    assert_trajectory_accounted(read_trajectory(trajectory_events_path(session_dir)))
    assert [event.payload["turn_number"] for event in events if event.event_type == EventType.TURN_STARTED] == [1]


async def test_emit_payload_factory_sees_the_assigned_sequence(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    trajectory = make_trajectory(session_dir)
    await trajectory.emit(_draft(1))
    seen: list[int] = []

    def factory(sequence: int) -> dict[str, Any]:
        seen.append(sequence)
        return {"superseded_to_sequence": sequence - 1}

    draft = trajectory.main_actor_draft(EventType.SESSION_ROLLBACK)
    assert await trajectory.emit(draft, payload_factory=factory) is EmitResult.WRITTEN
    assert seen == [5]
    last = _events(session_dir)[-1]
    assert last.sequence == 5
    assert last.payload == {"superseded_to_sequence": 4}


async def test_checkpoint_records_the_writer_watermarks(tmp_path: Path, make_trajectory: TrajectoryFactory) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    trajectory = make_trajectory(session_dir)
    assert await trajectory.checkpoint() is EmitResult.DEGRADED  # nothing to checkpoint before activation
    await trajectory.emit(_draft(1))
    assert await trajectory.checkpoint() is EmitResult.WRITTEN
    assert trajectory.last_assigned_sequence() == 5
    events = _events(session_dir)
    checkpoint = events[-1]
    assert checkpoint.event_type == EventType.CHECKPOINT
    assert checkpoint.actor == SYSTEM_ACTOR
    assert checkpoint.sequence == 5
    assert checkpoint.payload == {"last_assigned": 5, "last_written": 4, "last_durable": 0}
    assert trajectory.writer is not None
    assert trajectory.writer.snapshot().last_durable_sequence == 5

    assert await trajectory.checkpoint() is EmitResult.WRITTEN
    second = _events(session_dir)[-1]
    assert second.payload == {"last_assigned": 6, "last_written": 5, "last_durable": 5}


async def test_set_branch_id_stamps_subsequent_events(tmp_path: Path, make_trajectory: TrajectoryFactory) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    trajectory = make_trajectory(session_dir)
    await trajectory.emit(_draft(1))
    original = trajectory.branch_id
    assert original is not None
    new_branch = "f" * 32
    trajectory.set_branch_id(new_branch)
    assert trajectory.branch_id == new_branch
    assert trajectory.writer is not None
    assert trajectory.writer.branch_id == new_branch
    await trajectory.emit(_draft(2))
    events = _events(session_dir)
    assert [event.branch_id for event in events] == [original] * 4 + [new_branch]


# --------------------------------------------------------------------- close


async def test_close_writes_closure_markers_and_releases_the_lease(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    trajectory = make_trajectory(session_dir)
    await trajectory.emit(_draft(1))
    lines_before = _line_count(session_dir)

    assert await trajectory.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN) is True
    assert trajectory.is_closed
    assert trajectory.is_active is False
    assert trajectory.worker_stuck is False
    assert trajectory.writer is not None  # the writer object stays inspectable after close
    assert trajectory.writer.state is WriterState.CLOSED
    assert trajectory.writer.thread_alive is False
    assert trajectory.writer.runtime_closed
    assert WriterLease.is_held_elsewhere(trajectory_lease_path(session_dir)) is False

    events = _events(session_dir)
    assert _types(events)[-2:] == [EventType.COVERAGE_ENDED, EventType.RUNTIME_FINISHED]
    assert _line_count(session_dir) == lines_before + 2
    ended, finished = events[-2:]
    assert ended.payload == {"coverage_id": trajectory.writer.coverage_id, "last_sequence": ended.sequence - 1}
    assert finished.payload == {"reason": RuntimeFinishReason.GRACEFUL_SHUTDOWN}
    assert finished.sequence == 6
    raw = trajectory_events_path(session_dir).read_bytes()
    assert raw.endswith(b"\n")
    assert read_trajectory(trajectory_events_path(session_dir)).torn_tail_bytes == 0

    # Idempotent second close; later emits are dropped without touching the file.
    assert await trajectory.close(reason=RuntimeFinishReason.SESSION_SWITCH) is True
    assert await trajectory.emit(_draft(2)) is EmitResult.DEGRADED
    assert trajectory.emit_blocking(_draft(3)) is EmitResult.DEGRADED
    trajectory.emit_soon(_draft(4))
    assert await trajectory.checkpoint() is EmitResult.DEGRADED
    assert trajectory_events_path(session_dir).read_bytes() == raw
    assert trajectory.last_assigned_sequence() == 6


async def test_close_drains_emit_soon_acks_before_the_closure_markers(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    trajectory = make_trajectory(session_dir)
    await trajectory.emit(_draft(1))
    trajectory.emit_soon(_draft(2))
    trajectory.emit_soon(_draft(3))
    assert await trajectory.close(reason=RuntimeFinishReason.SESSION_SWITCH) is True
    events = _events(session_dir)
    assert _types(events)[-4:] == [
        EventType.TURN_STARTED,
        EventType.TURN_STARTED,
        EventType.COVERAGE_ENDED,
        EventType.RUNTIME_FINISHED,
    ]
    assert [event.payload["turn_number"] for event in events if event.event_type == EventType.TURN_STARTED] == [
        1,
        2,
        3,
    ]
    assert_trajectory_accounted(read_trajectory(trajectory_events_path(session_dir)))


# -------------------------------------------------------------------- resume


async def test_reopening_a_closed_session_appends_a_new_runtime(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    first = make_trajectory(session_dir, write_lock_path=tmp_path / "session.write.lock")
    await first.emit(_draft(1))
    first_runtime = first.runtime_id
    first_branch = first.branch_id
    await first.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    first_events = _events(session_dir)

    second = make_trajectory(session_dir, write_lock_path=tmp_path / "session.write.lock")
    assert second.last_assigned_sequence() == 6  # recovered by scanning the file, before activation
    assert await second.emit(_draft(2)) is EmitResult.WRITTEN
    assert second.runtime_id != first_runtime
    assert is_valid_analytics_id(second.runtime_id)
    assert second.branch_id == first_branch  # resumed runtimes continue the last branch
    assert second.coverage_reason == CoverageReason.RUNTIME_RESUMED

    events = _events(session_dir)
    assert events[: len(first_events)] == first_events  # append-only: nothing rewritten
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert _types(events)[len(first_events) :] == [
        EventType.COVERAGE_STARTED,
        EventType.RUNTIME_STARTED,
        EventType.TURN_STARTED,
    ]
    assert events[len(first_events)].payload["coverage_reason"] == CoverageReason.RUNTIME_RESUMED
    assert {event.runtime_id for event in events} == {first_runtime, second.runtime_id}
    assert_trajectory_accounted(read_trajectory(trajectory_events_path(session_dir)))
    assert read_trajectory(trajectory_events_path(session_dir)).corrupt_lines == []

    await second.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    assert _types(_events(session_dir))[-1] == EventType.RUNTIME_FINISHED


async def test_recovery_follows_the_open_not_a_probe_taken_before_it(
    tmp_path: Path, make_trajectory: TrajectoryFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A log that appears after a pre-open probe would have its sequences handed out twice."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    first = make_trajectory(session_dir)
    await first.emit(_draft(1))
    await first.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    path = trajectory_events_path(session_dir)
    prior = path.read_bytes()
    last = first.last_assigned_sequence()
    path.unlink()

    real_open = session_module.secure_open_owner_only_append

    def planting_open(target: Path) -> Any:
        # The log lands in the window a probe would have left open: absent
        # when anything looked before the open, present to the open itself.
        if not target.exists():
            plant_owner_only_bytes(target, prior)
        return real_open(target)

    monkeypatch.setattr(session_module, "secure_open_owner_only_append", planting_open)

    second = make_trajectory(session_dir)
    assert await second.emit(_draft(2)) is EmitResult.WRITTEN

    events = _events(session_dir)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert second.last_assigned_sequence() > last
    assert_trajectory_accounted(read_trajectory(trajectory_events_path(session_dir)))


@pytest.mark.skipif(os.name == "nt", reason="swapping the file a handle holds open is POSIX-only")
async def test_recovery_measures_the_file_it_opened_not_the_one_at_the_path(
    tmp_path: Path, make_trajectory: TrajectoryFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the open and the scan the pathname can come to mean another file."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    first = make_trajectory(session_dir)
    await first.emit(_draft(1))
    await first.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    last = first.last_assigned_sequence()

    real_open = session_module.secure_open_owner_only_append

    def replacing_open(target: Path) -> Any:
        handle = real_open(target)
        decoy = target.parent / "decoy.jsonl"
        decoy.write_bytes(trajectory_events_path(session_dir).read_bytes().splitlines(keepends=True)[0])
        os.replace(decoy, target)
        return handle

    monkeypatch.setattr(session_module, "secure_open_owner_only_append", replacing_open)

    second = make_trajectory(session_dir)
    assert await second.emit(_draft(2)) is EmitResult.WRITTEN
    # The writer resumed from its own file, whose slots run to *last* — the
    # decoy at the pathname claims only one.
    assert second.last_assigned_sequence() > last


async def test_the_writer_is_not_reachable_until_its_prelude_is_in(
    tmp_path: Path, make_trajectory: TrajectoryFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The emit paths that skip the activation lock find the writer on the recorder; until the
    prelude is submitted there is nothing there to find, so nobody takes a sequence ahead of it."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    published: list[TrajectoryWriter | None] = []
    original = SessionTrajectory._emit_prelude

    def recording_prelude(self: SessionTrajectory, writer: TrajectoryWriter, **kwargs: Any) -> None:
        published.append(self.writer)
        original(self, writer, **kwargs)
        published.append(self.writer)

    monkeypatch.setattr(SessionTrajectory, "_emit_prelude", recording_prelude)

    trajectory = make_trajectory(session_dir)
    assert await trajectory.emit(_draft(1)) is EmitResult.WRITTEN

    assert published == [None, None]
    assert trajectory.writer is not None
    assert _types(_events(session_dir))[0] == EventType.COVERAGE_STARTED


async def test_a_damaged_line_is_explained_by_a_gap_before_the_resumed_prelude(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    """Its slot is spent and its content unreadable, so the resumed runtime opens by saying so."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    first = make_trajectory(session_dir)
    await first.emit(_draft(1))
    await first.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    path = trajectory_events_path(session_dir)
    lines = path.read_bytes().splitlines(keepends=True)
    damaged = json.loads(lines[-1])
    damaged_sequence = damaged["sequence"]
    damaged["actor"] = {"kind": "not-an-actor"}  # header still reads; body no longer decodes
    path.write_bytes(b"".join(lines[:-1]) + encode_json_line(damaged))

    second = make_trajectory(session_dir)
    assert await second.emit(_draft(2)) is EmitResult.WRITTEN

    result = read_trajectory(path)
    assert len(result.corrupt_lines) == 1
    gap = result.events[len(lines) - 1]
    assert gap.event_type == EventType.GAP
    assert gap.sequence == damaged_sequence + 1  # written over nothing: the damaged slot stays spent
    assert gap.payload["first_sequence"] == damaged_sequence
    assert gap.payload["last_sequence"] == damaged_sequence
    assert gap.payload["reason"] == GapReason.RECOVERED_UNREADABLE
    assert _types(result.events)[len(lines) :] == [
        EventType.COVERAGE_STARTED,
        EventType.RUNTIME_STARTED,
        EventType.RUNTIME_RECOVERED,
        EventType.TURN_STARTED,
    ]
    # The hole the damaged line left is accounted for, so the file still reads
    # as one unbroken prefix.
    assert_trajectory_accounted(result)


async def test_a_log_written_by_a_newer_schema_is_left_alone(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    """A build that cannot read the tail cannot know which slots are free, so it does not write."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    first = make_trajectory(session_dir)
    await first.emit(_draft(1))
    await first.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    path = trajectory_events_path(session_dir)

    newer = json.loads(path.read_bytes().splitlines()[-1])
    newer["schema_version"] = SCHEMA_VERSION + 1
    newer["sequence"] += 1
    newer["actor"] = "main"  # a shape this build has no decoder for
    with path.open("ab") as handle:
        handle.write(encode_json_line(newer))
        handle.write(b'{"schema_version":2,"event_id":"torn')
    raw = path.read_bytes()

    reports: list[str] = []
    second = make_trajectory(session_dir, on_activation_failed=reports.append)
    assert await second.emit(_draft(2)) is EmitResult.DEGRADED
    assert second.disabled_reason == TrajectoryDisabledReason.SCHEMA_TOO_NEW
    assert reports == [TrajectoryDisabledReason.SCHEMA_TOO_NEW]
    assert second.is_active is False
    assert second.writer is None
    # Not even the torn bytes: a file this build will not write to is one it does not touch.
    assert path.read_bytes() == raw
    assert await second.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN) is True
    assert path.read_bytes() == raw
    assert WriterLease.is_held_elsewhere(trajectory_lease_path(session_dir)) is False


async def test_a_log_ending_in_a_line_that_names_no_slot_is_left_alone(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    """A complete line whose header does not read hides which sequence comes next."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    first = make_trajectory(session_dir)
    await first.emit(_draft(1))
    await first.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    path = trajectory_events_path(session_dir)

    with path.open("ab") as handle:
        handle.write(b"{not json\n")  # complete: it took a slot, and names none
        handle.write(b'{"schema_version":2,"event_id":"torn')
    raw = path.read_bytes()

    reports: list[str] = []
    second = make_trajectory(session_dir, on_activation_failed=reports.append)
    assert await second.emit(_draft(2)) is EmitResult.DEGRADED
    assert second.disabled_reason == TrajectoryDisabledReason.UNREADABLE_TAIL
    assert reports == [TrajectoryDisabledReason.UNREADABLE_TAIL]
    assert second.is_active is False
    assert second.writer is None
    # Appending here would reuse whatever slot that line took, so nothing is
    # written and nothing is trimmed.
    assert path.read_bytes() == raw
    assert await second.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN) is True
    assert path.read_bytes() == raw
    assert WriterLease.is_held_elsewhere(trajectory_lease_path(session_dir)) is False


async def test_a_log_that_has_spent_every_sequence_it_can_encode_is_left_alone(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    """Past the last slot a line may carry there is nothing left to hand out."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    first = make_trajectory(session_dir)
    await first.emit(_draft(1))
    await first.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    path = trajectory_events_path(session_dir)

    exhausted = json.loads(path.read_bytes().splitlines()[-1])
    exhausted["sequence"] = INT64_MAX
    with path.open("ab") as handle:
        handle.write(encode_json_line(exhausted))
        handle.write(b'{"schema_version":2,"event_id":"torn')
    raw = path.read_bytes()

    reports: list[str] = []
    second = make_trajectory(session_dir, on_activation_failed=reports.append)
    assert await second.emit(_draft(2)) is EmitResult.DEGRADED
    assert second.disabled_reason == TrajectoryDisabledReason.SEQUENCE_EXHAUSTED
    assert reports == [TrajectoryDisabledReason.SEQUENCE_EXHAUSTED]
    assert second.is_active is False
    assert second.writer is None
    # A gap is encoded past the check that refuses an out-of-range event, so
    # the one thing this must not do is open a writer here.
    assert path.read_bytes() == raw
    assert await second.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN) is True
    assert path.read_bytes() == raw
    assert WriterLease.is_held_elsewhere(trajectory_lease_path(session_dir)) is False


async def test_a_cancelled_close_still_closes_the_writer_and_hands_the_lease_back(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    """The close is what returns the descriptor and the lease, so it is never left half-done."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    trajectory = make_trajectory(session_dir, write_lock_path=trajectory_lease_path(session_dir))
    assert await trajectory.emit(_draft(1)) is EmitResult.WRITTEN
    writer = trajectory.writer
    assert writer is not None

    gate = asyncio.Event()
    entered = asyncio.Event()
    real_close = writer.close

    async def _slow_close(**kwargs: Any) -> bool:
        entered.set()
        await gate.wait()
        return await real_close(**kwargs)

    writer.close = _slow_close  # type: ignore[method-assign]

    cancelled_caller = asyncio.ensure_future(trajectory.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN))
    assert await wait_until(entered.is_set, timeout=WAIT_TIMEOUT)
    cancelled_caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_caller

    gate.set()
    # The next caller waits on the close still running rather than being told
    # the runtime is closed while its worker holds the file.
    assert await trajectory.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN) is True
    assert writer.thread_alive is False
    assert WriterLease.is_held_elsewhere(trajectory_lease_path(session_dir)) is False
    events = read_trajectory(trajectory_events_path(session_dir)).events
    assert events[-1].event_type == EventType.RUNTIME_FINISHED


async def test_torn_tail_is_truncated_and_recorded_as_runtime_recovered(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    first = make_trajectory(session_dir)
    await first.emit(_draft(1))
    await first.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    path = trajectory_events_path(session_dir)
    clean = path.read_bytes()
    garbage = b'{"schema_version":1,"event_id":"torn'
    with path.open("ab") as handle:
        handle.write(garbage)
    assert read_trajectory(path).torn_tail_bytes == len(garbage)

    second = make_trajectory(session_dir)
    assert second.last_assigned_sequence() == 6  # the torn bytes never count
    await second.emit(_draft(2))
    result = read_trajectory(path)
    assert result.torn_tail_bytes == 0
    assert result.corrupt_lines == []
    assert_trajectory_accounted(result)
    assert path.read_bytes().startswith(clean)
    assert _types(result.events)[6:] == [
        EventType.COVERAGE_STARTED,
        EventType.RUNTIME_STARTED,
        EventType.RUNTIME_RECOVERED,
        EventType.TURN_STARTED,
    ]
    recovered = result.events[8]
    assert recovered.actor == SYSTEM_ACTOR
    assert recovered.payload == {"truncated_bytes": len(garbage), "resumed_from_sequence": 6}
    assert [event.sequence for event in result.events] == list(range(1, 11))


async def test_torn_tail_recovery_evidence_survives_activation_retry(
    tmp_path: Path, make_trajectory: TrajectoryFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry sees a clean file, but must report the first scan's repair."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    first = make_trajectory(session_dir)
    await first.emit(_draft(1))
    await first.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    path = trajectory_events_path(session_dir)
    clean = path.read_bytes()
    garbage = b'{"schema_version":1,"event_id":"torn'
    with path.open("ab") as handle:
        handle.write(garbage)

    real_emit_prelude = SessionTrajectory._emit_prelude
    attempts = 0

    def fail_once(self: SessionTrajectory, writer: TrajectoryWriter, **kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("prelude temporarily unavailable")
        real_emit_prelude(self, writer, **kwargs)

    monkeypatch.setattr(SessionTrajectory, "_emit_prelude", fail_once)

    second = make_trajectory(session_dir)
    assert await second.emit(_draft(2)) is EmitResult.WRITTEN
    assert attempts == 2
    result = read_trajectory(path)
    assert result.torn_tail_bytes == 0
    assert result.corrupt_lines == []
    assert_trajectory_accounted(result)
    assert path.read_bytes().startswith(clean)
    assert _types(result.events)[6:] == [
        EventType.COVERAGE_STARTED,
        EventType.RUNTIME_STARTED,
        EventType.RUNTIME_RECOVERED,
        EventType.TURN_STARTED,
    ]
    assert result.events[8].payload == {"truncated_bytes": len(garbage), "resumed_from_sequence": 6}


async def test_unclosed_previous_runtime_is_recorded_as_recovered(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    """A crash leaves no runtime.finished: the next runtime says so even with no torn bytes."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    first = make_trajectory(session_dir)
    await first.emit(_draft(1))
    await first.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN)
    path = trajectory_events_path(session_dir)
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:-2]))  # drop coverage.ended + runtime.finished, as a crash would
    assert _types(_events(session_dir))[-1] == EventType.TURN_STARTED

    second = make_trajectory(session_dir)
    await second.emit(_draft(2))
    events = _events(session_dir)
    assert _types(events)[4:] == [
        EventType.COVERAGE_STARTED,
        EventType.RUNTIME_STARTED,
        EventType.RUNTIME_RECOVERED,
        EventType.TURN_STARTED,
    ]
    assert events[6].payload == {"truncated_bytes": 0, "resumed_from_sequence": 4}
    assert [event.sequence for event in events] == list(range(1, 9))
    assert second.coverage_reason == CoverageReason.RUNTIME_RESUMED


async def test_empty_existing_file_starts_a_fresh_session(tmp_path: Path, make_trajectory: TrajectoryFactory) -> None:
    session_dir = tmp_path / "session"
    trajectory_dir(session_dir).mkdir(parents=True)
    plant_owner_only_bytes(trajectory_events_path(session_dir))
    trajectory = make_trajectory(session_dir)
    await trajectory.emit(_draft(1))
    events = _events(session_dir)
    assert _types(events) == [
        EventType.COVERAGE_STARTED,
        EventType.RUNTIME_STARTED,
        EventType.SESSION_STARTED,
        EventType.TURN_STARTED,
    ]
    assert trajectory.coverage_reason == CoverageReason.SESSION_STARTED
    assert [event.sequence for event in events] == [1, 2, 3, 4]


# --------------------------------------------------------------------- lease


async def test_an_abandoned_worker_hands_the_lease_back_when_it_finally_exits(
    tmp_path: Path, make_trajectory: TrajectoryFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stuck worker keeps the lease — but only until it returns, not until the process exits."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    trajectory = make_trajectory(session_dir, close_join_timeout=0.05)
    # Activation performs real disk writes; only the deliberately blocked close
    # should use the fault-injection deadline, not the healthy prelude.
    assert await trajectory.emit(_draft(1)) is EmitResult.WRITTEN
    assert trajectory.writer is not None
    monkeypatch.setattr(trajectory.writer, "_write_ack_timeout", 0.05)
    lease_path = trajectory_lease_path(session_dir)
    assert WriterLease.is_held_elsewhere(lease_path) is True

    unblock = threading.Event()
    real_write = FdWriteBackend.write

    def blocking_write(self: FdWriteBackend, data: memoryview) -> int:
        unblock.wait(10.0)
        return real_write(self, data)

    monkeypatch.setattr(FdWriteBackend, "write", blocking_write)

    try:
        assert await trajectory.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN) is False
        assert trajectory.worker_stuck is True
        assert WriterLease.is_held_elsewhere(lease_path) is True
    finally:
        unblock.set()
    assert await wait_until(lambda: not WriterLease.is_held_elsewhere(lease_path))


async def test_a_transient_prelude_failure_rewinds_and_retries_activation(
    tmp_path: Path, make_trajectory: TrajectoryFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed unannounced runtime leaves no bytes before a fresh activation."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    real_emit_prelude = SessionTrajectory._emit_prelude
    attempts = 0

    def fail_once(self: SessionTrajectory, writer: TrajectoryWriter, **kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # Prove that a partially written prelude is removed, not merely
            # abandoned ahead of the retry's runtime.
            writer.emit_blocking(self._prelude_draft(EventType.COVERAGE_STARTED, actor=SYSTEM_ACTOR, payload={}))
            raise RuntimeError("prelude temporarily unavailable")
        real_emit_prelude(self, writer, **kwargs)

    monkeypatch.setattr(SessionTrajectory, "_emit_prelude", fail_once)

    trajectory = make_trajectory(session_dir)
    assert await trajectory.emit(_draft(1)) is EmitResult.WRITTEN
    assert attempts == 2
    assert trajectory.is_active is True
    assert trajectory.disabled_reason is None
    assert _types(_events(session_dir)) == [
        EventType.COVERAGE_STARTED,
        EventType.RUNTIME_STARTED,
        EventType.SESSION_STARTED,
        EventType.TURN_STARTED,
    ]


async def test_concurrent_emitters_wait_for_activation_retry_instead_of_dropping_events(
    tmp_path: Path, make_trajectory: TrajectoryFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No emit path may observe the retryable failure as a terminal state."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    real_emit_prelude = SessionTrajectory._emit_prelude
    real_abort = TrajectoryWriter.abort
    attempts = 0
    abort_entered = threading.Event()
    allow_abort = threading.Event()

    def fail_once(self: SessionTrajectory, writer: TrajectoryWriter, **kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("prelude temporarily unavailable")
        real_emit_prelude(self, writer, **kwargs)

    def paused_abort(self: TrajectoryWriter, *, join_timeout: float) -> bool:
        abort_entered.set()
        allow_abort.wait(WAIT_TIMEOUT)
        return real_abort(self, join_timeout=join_timeout)

    monkeypatch.setattr(SessionTrajectory, "_emit_prelude", fail_once)
    monkeypatch.setattr(TrajectoryWriter, "abort", paused_abort)

    trajectory = make_trajectory(session_dir)
    first_emit = asyncio.create_task(trajectory.emit(_draft(1)))
    second_emit: asyncio.Task[EmitResult] | None = None
    blocking_emit: asyncio.Task[EmitResult] | None = None
    try:
        assert await wait_until(abort_entered.is_set)
        second_emit = asyncio.create_task(trajectory.emit(_draft(2)))
        blocking_emit = asyncio.create_task(asyncio.to_thread(trajectory.emit_blocking, _draft(3)))
        assert trajectory.emit_soon(_draft(4)) is None
    finally:
        allow_abort.set()

    assert second_emit is not None
    assert blocking_emit is not None
    assert await asyncio.gather(first_emit, second_emit, blocking_emit) == [
        EmitResult.WRITTEN,
        EmitResult.WRITTEN,
        EmitResult.WRITTEN,
    ]
    assert attempts == 2
    assert await trajectory.close(reason=RuntimeFinishReason.GRACEFUL_SHUTDOWN) is True
    turns = [event for event in _events(session_dir) if event.event_type == EventType.TURN_STARTED]
    assert sorted(event.payload["turn_number"] for event in turns) == [1, 2, 3, 4]
    assert_trajectory_accounted(read_trajectory(trajectory_events_path(session_dir)))


async def test_a_persistent_activation_failure_is_terminal_and_reported_once(
    tmp_path: Path, make_trajectory: TrajectoryFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two failed attempts disable recording, release the lease, and notify once."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    attempts = 0
    reports: list[str] = []

    def failing_prelude(self: SessionTrajectory, writer: TrajectoryWriter, **kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("prelude unavailable")

    monkeypatch.setattr(SessionTrajectory, "_emit_prelude", failing_prelude)

    trajectory = make_trajectory(session_dir, on_activation_failed=reports.append)
    assert await trajectory.emit(_draft(1)) is EmitResult.DEGRADED
    assert attempts == 2
    assert reports == [TrajectoryDisabledReason.ACTIVATION_FAILED]
    assert trajectory.writer is None
    assert trajectory.disabled_reason == TrajectoryDisabledReason.ACTIVATION_FAILED
    assert trajectory.is_active is False
    assert await trajectory.ensure_active() is False
    assert await trajectory.checkpoint() is EmitResult.DEGRADED
    assert trajectory.emit_blocking(_draft(2)) is EmitResult.DEGRADED
    assert reports == [TrajectoryDisabledReason.ACTIVATION_FAILED]
    assert not trajectory_events_path(session_dir).read_bytes()
    assert WriterLease.is_held_elsewhere(trajectory_lease_path(session_dir)) is False


async def test_a_worker_that_never_started_still_hands_the_lease_back_on_close(
    tmp_path: Path, make_trajectory: TrajectoryFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thread creation can be refused; the session must not stay leased for the rest of the process."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()

    class _RefusedThread:
        """What ``threading.Thread`` is when the system has no thread left to give."""

        def start(self) -> None:
            raise RuntimeError("can't start new thread")

        def is_alive(self) -> bool:
            return False

        def join(self, timeout: float | None = None) -> None:
            raise AssertionError("a thread that never started must not be joined")

    # The module's own name, so the stdlib the loop runs on is untouched.
    monkeypatch.setattr(
        writer_module,
        "threading",
        SimpleNamespace(
            Thread=lambda *_args, **_kwargs: _RefusedThread(),
            Lock=threading.Lock,
            Condition=threading.Condition,
        ),
    )

    trajectory = make_trajectory(session_dir)
    assert await trajectory.emit(_draft(1)) is EmitResult.DEGRADED
    assert trajectory.disabled_reason == TrajectoryDisabledReason.ACTIVATION_FAILED
    lease_path = trajectory_lease_path(session_dir)
    assert WriterLease.is_held_elsewhere(lease_path) is False
    # The session is genuinely free again, not merely reported as free.
    taken = WriterLease.try_acquire(lease_path)
    assert taken is not None
    taken.release()


async def test_second_live_recorder_is_disabled_while_the_lease_is_held(
    tmp_path: Path, make_trajectory: TrajectoryFactory
) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    first = make_trajectory(session_dir)
    await first.emit(_draft(1))
    raw = trajectory_events_path(session_dir).read_bytes()

    reports: list[str] = []
    second = make_trajectory(session_dir, on_activation_failed=reports.append)
    assert await second.emit(_draft(2)) is EmitResult.DEGRADED
    assert second.disabled_reason == TrajectoryDisabledReason.LEASE_HELD
    assert reports == [TrajectoryDisabledReason.LEASE_HELD]
    assert second.is_active is False
    assert second.writer is None
    assert second.runtime_id is None
    assert trajectory_events_path(session_dir).read_bytes() == raw
    # The first recorder is unaffected and still owns the file.
    assert await first.emit(_draft(3)) is EmitResult.WRITTEN
    assert first.is_active
    assert await second.close(reason=RuntimeFinishReason.SESSION_SWITCH) is True
    assert second.disabled_reason == TrajectoryDisabledReason.LEASE_HELD
    assert WriterLease.is_held_elsewhere(trajectory_lease_path(session_dir)) is True

    # Once the holder closes, a fresh recorder can take over.
    await first.close(reason=RuntimeFinishReason.SESSION_SWITCH)
    third = make_trajectory(session_dir)
    assert await third.emit(_draft(4)) is EmitResult.WRITTEN
    assert third.runtime_id != first.runtime_id
