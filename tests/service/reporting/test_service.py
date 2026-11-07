# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ExecutionChanged,
    SessionDeleted,
    SessionReady,
    SessionSaved,
    TurnStarted,
)
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.reporting.config import ReportConfig
from chrys.foundation.reporting.frames import (
    KIND_AGENT_ENV,
    KIND_REPO_IDENTITY,
    KIND_SESSION_META,
    KIND_SNAPSHOT_MANIFEST,
    KIND_TRAJECTORY_LINES,
    KIND_WORKSPACE_MANIFEST,
    Frame,
    decode_frames,
)
from chrys.foundation.util.session_ids import session_short_id
from chrys.service.reporting import hooks, startup
from chrys.service.reporting.meta import ReportMeta
from chrys.service.reporting.service import ReportingService
from chrys.service.session.persistence import SessionPersistence
from tests.support.waiting import wait_for

SESSION_ID = "12345678-1234-1234-1234-123456789abc"


def _config(tmp_path: Path) -> ReportConfig:
    """No endpoint: the collectors run, the pump stays off."""
    return ReportConfig(
        enabled=True,
        endpoint="",
        token="",
        upload_timeout_seconds=1.0,
        batch_max_bytes=8 << 20,
        poll_interval_seconds=1.0,
        queue_bytes_max=8 << 20,
        workspace_capture=True,
        agent_config=True,
        diagnostics=False,
        directory=tmp_path / "reporting",
    )


def _queued(reporting: ReportingService) -> list[Frame]:
    wal = reporting._wal
    if wal is None:
        return []
    batch = wal.next_batch(max_bytes=8 << 20)
    return [] if batch is None else decode_frames(wal.read(batch))


def _kinds(reporting: ReportingService) -> set[str]:
    return {frame.header.kind for frame in _queued(reporting)}


def _line(sequence: int) -> bytes:
    return json.dumps({"schema_version": 1, "sequence": sequence, "event_type": "event.turn"}).encode() + b"\n"


async def test_the_lifecycle_events_queue_the_documented_units(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_repo_factory: Callable[[Path], Path],
) -> None:
    from chrys.service.reporting import service as service_module

    sessions = tmp_path / "sessions"
    monkeypatch.setattr(service_module, "resolve_sessions_dir", lambda **_kwargs: sessions)
    repo = git_repo_factory(tmp_path / "repo")
    (repo / "notes.txt").write_text("dirty content", encoding="utf-8")
    folder = sessions / session_short_id(SESSION_ID)
    (folder / "trajectory").mkdir(parents=True)
    events = _line(1) + _line(2)
    (folder / "trajectory" / "events.jsonl").write_bytes(events)
    (folder / "session.json").write_bytes(b'{"schema_version":1}')

    bus = EventBus()
    reporting = ReportingService(bus, _config(tmp_path), settings=Settings())
    await reporting.start()
    try:
        await bus.publish(
            SessionReady(
                session_id=SESSION_ID,
                primary_cwd=str(repo),
                agent_profile="reviewer",
                display_name="Reviewer",
                model_profile_id="model-1",
                tool_names=["read_file"],
                tool_kinds={"read_file": "fs"},
                skill_names=["triage"],
            )
        )
        await wait_for(
            lambda: {KIND_REPO_IDENTITY, KIND_AGENT_ENV} <= _kinds(reporting),
            description="repository identity and agent environment",
        )
        # Opening a session must not capture the workspace.
        assert KIND_WORKSPACE_MANIFEST not in _kinds(reporting)

        await bus.publish(SessionSaved(session_id=SESSION_ID))
        await wait_for(
            lambda: {KIND_TRAJECTORY_LINES, KIND_SNAPSHOT_MANIFEST} <= _kinds(reporting),
            description="trajectory lines and snapshot manifest",
        )
        trajectory = [frame for frame in _queued(reporting) if frame.header.kind == KIND_TRAJECTORY_LINES]
        assert b"".join(frame.payload for frame in trajectory) == events

        reporting.offer_meta(ReportMeta(session_id=SESSION_ID, title="Fix the parser", message_count=4))
        await wait_for(
            lambda: KIND_SESSION_META in _kinds(reporting),
            description="session meta after the save hook",
        )

        await bus.publish(TurnStarted(session_id=SESSION_ID, turn_id="turn-1", turn_number=1))
        await wait_for(
            lambda: KIND_WORKSPACE_MANIFEST in _kinds(reporting),
            description="workspace manifest at turn start",
        )

        await bus.publish(ExecutionChanged(session_id=SESSION_ID, snapshot=ExecutionSnapshot(kind="idle")))
        cursor_path = reporting._cursors.path_for(SESSION_ID) if reporting._cursors is not None else None
        assert cursor_path is not None
        await wait_for(cursor_path.exists, description="cursor written beside the queue")
        await bus.publish(SessionDeleted(session_id=SESSION_ID))
        await wait_for(lambda: not cursor_path.exists(), description="cursor removed on delete")
    finally:
        await reporting.close()


async def test_the_meta_row_is_written_once_per_distinct_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.service.reporting import service as service_module

    monkeypatch.setattr(service_module, "resolve_sessions_dir", lambda **_kwargs: tmp_path / "sessions")
    bus = EventBus()
    reporting = ReportingService(bus, _config(tmp_path), settings=Settings())
    await reporting.start()
    try:
        await bus.publish(SessionReady(session_id=SESSION_ID, primary_cwd=""))
        meta = ReportMeta(session_id=SESSION_ID, title="One title")
        reporting.offer_meta(meta)
        await wait_for(lambda: KIND_SESSION_META in _kinds(reporting), description="first meta")
        first = [frame for frame in _queued(reporting) if frame.header.kind == KIND_SESSION_META]

        reporting.offer_meta(meta)
        await wait_for(
            lambda: len([frame for frame in _queued(reporting) if frame.header.kind == KIND_SESSION_META]) == 2,
            description="second meta",
        )

        second = [frame for frame in _queued(reporting) if frame.header.kind == KIND_SESSION_META]
        assert second[0].header.unit_id == second[1].header.unit_id
        assert first[0].header.unit_id == second[0].header.unit_id
    finally:
        await reporting.close()


def test_arming_is_a_settings_read_and_nothing_else(tmp_path: Path) -> None:
    startup.clear_armed()
    try:
        assert startup.arm_reporting(Settings()) is False
        assert startup.armed_config() is None

        enabled = replace(
            Settings(),
            report_enabled=True,
            report_endpoint="http://report.example.test",
        )
        assert startup.arm_reporting(enabled) is True
        config = startup.armed_config()
        assert config is not None
        assert config.endpoint == "http://report.example.test"
        assert config.deliverable is True
    finally:
        startup.clear_armed()


async def test_starting_without_an_armed_configuration_does_nothing() -> None:
    startup.clear_armed()
    hooks.clear_reporting()

    assert await startup.start_armed_reporting(EventBus()) is None
    assert hooks.active_reporting() is None


async def test_the_armed_runtime_starts_once_and_stops(tmp_path: Path) -> None:
    startup.clear_armed()
    hooks.clear_reporting()
    startup.arm_reporting(replace(Settings(), report_enabled=True, report_endpoint="", report_poll_interval_ms=60_000))
    try:
        first = await startup.start_armed_reporting(EventBus(), settings=Settings())
        second = await startup.start_armed_reporting(EventBus(), settings=Settings())

        assert first is not None
        assert second is first
        assert hooks.active_reporting() is first

        await startup.stop_reporting()

        assert hooks.active_reporting() is None
    finally:
        startup.clear_armed()
        await startup.stop_reporting()


def test_the_save_hook_forwards_only_primitives(tmp_path: Path) -> None:
    recorded: list[ReportMeta] = []

    class _Recorder:
        def offer_meta(self, meta: ReportMeta) -> None:
            recorded.append(meta)

    hooks.install_reporting(_Recorder())  # type: ignore[arg-type]
    try:
        persistence = SessionPersistence(None, EventBus())
        persistence._publish_report_meta(
            SESSION_ID,
            {"messages": [], "turn_counter": 3, "total_session_tokens": 42},
            agent_profile_name="reviewer",
            agent_display_name="Reviewer",
            workspace=None,
            model_profile=None,
        )
    finally:
        hooks.clear_reporting()

    assert len(recorded) == 1
    meta = recorded[0]
    assert meta.session_id == SESSION_ID
    assert (meta.turn_count, meta.total_tokens, meta.message_count) == (3, 42, 0)
    assert meta.agent_profile == "reviewer"
    assert meta.title == ""


def test_the_save_hook_is_a_no_op_when_reporting_is_off() -> None:
    hooks.clear_reporting()

    SessionPersistence(None, EventBus())._publish_report_meta(
        SESSION_ID,
        {"messages": []},
        agent_profile_name="reviewer",
        agent_display_name="",
        workspace=None,
        model_profile=None,
    )
