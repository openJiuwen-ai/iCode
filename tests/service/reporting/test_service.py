# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentRuntimeDetails,
    AgentRuntimeUpdated,
    ExecutionChanged,
    RuntimeHookSourceDetails,
    RuntimeModelDetails,
    RuntimeSkillDetails,
    SessionDeleted,
    SessionReady,
    SessionRestored,
    SessionSaved,
    TurnStarted,
)
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.reporting.config import ReportConfig
from chrys.foundation.reporting.diagnostics import ReportDiagnostics
from chrys.foundation.reporting.frames import (
    KIND_AGENT_ENV,
    KIND_REPO_IDENTITY,
    KIND_SESSION_META,
    KIND_SNAPSHOT_CHUNK,
    KIND_SNAPSHOT_MANIFEST,
    KIND_TRAJECTORY_LINES,
    KIND_WORKSPACE_MANIFEST,
    Frame,
    decode_frames,
    decompress_body,
    scope_session_meta,
)
from chrys.foundation.reporting.manifest import ManifestReconciler
from chrys.foundation.reporting.pump import TransportResult
from chrys.foundation.reporting.wal import DurableWAL
from chrys.foundation.util.session_ids import session_short_id
from chrys.kernel import Message
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.reporting import hooks, startup
from chrys.service.reporting.cursors import SessionCursor, SessionCursorStore
from chrys.service.reporting.meta import WORKSPACE_KIND_NO_REPO, ReportMeta
from chrys.service.reporting.service import ReportingService
from chrys.service.reporting.snapshot import SnapshotTap
from chrys.service.session.persistence import SessionPersistence
from chrys.service.session.runtime_metadata import TOTAL_SESSION_TOKENS_KEY
from tests.support.waiting import wait_for, wait_until

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


def _runtime_details(*, model_id: str = "claude", skill: str = "") -> AgentRuntimeDetails:
    """Runtime facts as the engine publishes them on ``SessionReady``."""
    return AgentRuntimeDetails(
        model=RuntimeModelDetails(
            profile_id="model-1",
            name="Claude",
            provider="anthropic",
            model_id=model_id,
        ),
        skill_details=[RuntimeSkillDetails(name=skill, source="")] if skill else [],
        hook_sources=[
            RuntimeHookSourceDetails(
                scope="global",
                source_path="",
                hooks=[],
            )
        ],
    )


async def test_the_lifecycle_events_queue_the_documented_units(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    network_repo: Path,
) -> None:
    from chrys.service.reporting import service as service_module

    sessions = tmp_path / "sessions"
    monkeypatch.setattr(service_module, "resolve_sessions_dir", lambda **_kwargs: sessions)
    repo = network_repo
    (repo / "notes.txt").write_text("dirty content", encoding="utf-8")
    folder = sessions / session_short_id(SESSION_ID)
    (folder / "trajectory").mkdir(parents=True)
    events = _line(1) + _line(2)
    (folder / "trajectory" / "events.jsonl").write_bytes(events)
    (folder / "session.json").write_bytes(b'{"schema_version":1}')

    bus = EventBus()
    reporting = ReportingService(bus, _config(tmp_path))
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
                runtime_details=_runtime_details(skill="triage"),
            )
        )
        await wait_for(
            lambda: {KIND_REPO_IDENTITY, KIND_AGENT_ENV} <= _kinds(reporting),
            description="repository identity and agent environment",
        )
        # Opening a session must not capture the workspace.
        assert KIND_WORKSPACE_MANIFEST not in _kinds(reporting)
        # The environment frame carries the workspace classification and the
        # capture moment on its envelope, never in its payload.
        environment = next(frame for frame in _queued(reporting) if frame.header.kind == KIND_AGENT_ENV)
        assert environment.header.workspace_kind == "git"
        assert environment.header.captured_at
        assert b"workspaceKind" not in environment.payload
        assert b"capturedAt" not in environment.payload

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

        # The final lease release is the turn-end fallback: a line the save
        # events did not cover is still tailed.
        trailing = _line(3)
        with (folder / "trajectory" / "events.jsonl").open("ab") as handle:
            handle.write(trailing)
        await bus.publish(ExecutionChanged(session_id=SESSION_ID, snapshot=ExecutionSnapshot(kind="idle")))
        await wait_for(
            lambda: (
                trailing
                in b"".join(frame.payload for frame in _queued(reporting) if frame.header.kind == KIND_TRAJECTORY_LINES)
            ),
            description="the trailing line reaches the queue",
        )
        await bus.publish(SessionDeleted(session_id=SESSION_ID))
        cursors = reporting._cursors
        assert cursors is not None
        await wait_for(lambda: not cursors.path_for(SESSION_ID).exists(), description="cursor removed on delete")
    finally:
        await reporting.close()


async def test_the_environment_is_resent_when_the_facts_or_the_classification_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.service.reporting import service as service_module

    monkeypatch.setattr(service_module, "resolve_sessions_dir", lambda **_kwargs: tmp_path / "sessions")
    bus = EventBus()
    reporting = ReportingService(bus, _config(tmp_path))
    await reporting.start()
    try:
        await bus.publish(SessionReady(session_id=SESSION_ID, primary_cwd="", runtime_details=_runtime_details()))
        await wait_for(lambda: KIND_AGENT_ENV in _kinds(reporting), description="first environment")
        first = [frame for frame in _queued(reporting) if frame.header.kind == KIND_AGENT_ENV]
        assert first[0].header.workspace_kind == "no_repo"
        cursors = reporting._cursors
        assert cursors is not None
        cursor = cursors.load(SESSION_ID)
        assert cursor.agent_config_sha256

        # A runtime update with the same facts re-arms nothing: the cursor's
        # content hash and classification are unchanged, so no second frame.
        await bus.publish(AgentRuntimeUpdated(session_id=SESSION_ID, runtime_details=_runtime_details()))
        assert not await wait_until(
            lambda: len([frame for frame in _queued(reporting) if frame.header.kind == KIND_AGENT_ENV]) > 1,
            timeout=1.0,
        )

        # A real change — the model identity — produces a new unit.
        await bus.publish(
            AgentRuntimeUpdated(
                session_id=SESSION_ID,
                runtime_details=_runtime_details(model_id="claude-opus"),
            )
        )
        await wait_for(
            lambda: len([frame for frame in _queued(reporting) if frame.header.kind == KIND_AGENT_ENV]) == 2,
            description="environment after the model switch",
        )
        second = [frame for frame in _queued(reporting) if frame.header.kind == KIND_AGENT_ENV]
        assert second[1].header.unit_id != second[0].header.unit_id
        assert second[1].header.workspace_kind == "no_repo"
    finally:
        await reporting.close()


async def test_a_restored_session_without_facts_reports_absent_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.service.reporting import service as service_module

    monkeypatch.setattr(service_module, "resolve_sessions_dir", lambda **_kwargs: tmp_path / "sessions")
    bus = EventBus()
    reporting = ReportingService(bus, _config(tmp_path))
    await reporting.start()
    try:
        await bus.publish(SessionRestored(session_id=SESSION_ID, primary_cwd="", agent_profile="reviewer"))
        await wait_for(lambda: KIND_AGENT_ENV in _kinds(reporting), description="environment without facts")

        environment = next(frame for frame in _queued(reporting) if frame.header.kind == KIND_AGENT_ENV)
        document = json.loads(environment.payload)
        assert document["model"] == {"profileId": "", "provider": "", "modelId": "", "params": {}}
        assert document["sections"]["skills"] == "absent"
        assert document["sections"]["hooks"] == "absent"
    finally:
        await reporting.close()


async def test_the_meta_row_is_scoped_by_the_snapshot_it_describes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.service.reporting import service as service_module

    sessions = tmp_path / "sessions"
    monkeypatch.setattr(service_module, "resolve_sessions_dir", lambda **_kwargs: sessions)
    folder = sessions / session_short_id(SESSION_ID)
    folder.mkdir(parents=True)
    (folder / "session.json").write_bytes(b'{"schema_version":1}')
    bus = EventBus()
    reporting = ReportingService(bus, _config(tmp_path))
    await reporting.start()
    try:
        await bus.publish(SessionReady(session_id=SESSION_ID, primary_cwd=""))
        meta = ReportMeta(session_id=SESSION_ID, title="One title")
        reporting.offer_meta(meta)
        await wait_for(lambda: KIND_SESSION_META in _kinds(reporting), description="first meta")
        first = _meta_frames(reporting)
        # The server joins the row to this snapshot by content hash: the scope
        # names the snapshot, never the meta's own bytes.
        first_snapshot = _snapshot_hash(reporting)
        assert first_snapshot
        assert first[0].header.scope == scope_session_meta(first_snapshot)

        # Same content, same snapshot: the identical unit, so the server's
        # receipt table answers the second send as a duplicate.
        reporting.offer_meta(meta)
        await wait_for(lambda: len(_meta_frames(reporting)) == 2, description="second meta")
        assert _meta_frames(reporting)[1].header.unit_id == first[0].header.unit_id

        # A save moves the snapshot, and the next meta names the new one.
        (folder / "session.json").write_bytes(b'{"schema_version":1,"title":"One title"}')
        await bus.publish(SessionSaved(session_id=SESSION_ID))
        await wait_for(lambda: _snapshot_hash(reporting) not in {"", first_snapshot}, description="a newer snapshot")
        reporting.offer_meta(meta)
        await wait_for(lambda: len(_meta_frames(reporting)) == 3, description="third meta")
        third = _meta_frames(reporting)[2]
        assert third.header.scope == scope_session_meta(_snapshot_hash(reporting))
        assert third.header.unit_id != first[0].header.unit_id
    finally:
        await reporting.close()


async def test_a_meta_row_waits_for_the_snapshot_it_belongs_to(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A session with no saved file yet keeps its meta instead of sending it."""
    from chrys.service.reporting import service as service_module

    monkeypatch.setattr(service_module, "resolve_sessions_dir", lambda **_kwargs: tmp_path / "sessions")
    bus = EventBus()
    reporting = ReportingService(bus, _config(tmp_path))
    await reporting.start()
    try:
        await bus.publish(SessionReady(session_id=SESSION_ID, primary_cwd=""))
        reporting.offer_meta(ReportMeta(session_id=SESSION_ID, title="No file yet"))
        await wait_for(lambda: KIND_AGENT_ENV in _kinds(reporting), description="the first round ran")
        assert KIND_SESSION_META not in _kinds(reporting)
        assert SESSION_ID in reporting._metas
        assert reporting._bindings[SESSION_ID].meta_pending is True

        # The file appears: the held row now has the snapshot it belongs to.
        folder = tmp_path / "sessions" / session_short_id(SESSION_ID)
        folder.mkdir(parents=True)
        (folder / "session.json").write_bytes(b'{"schema_version":1}')
        await bus.publish(SessionSaved(session_id=SESSION_ID))
        await wait_for(lambda: KIND_SESSION_META in _kinds(reporting), description="the held meta lands")
    finally:
        await reporting.close()


def _meta_frames(reporting: ReportingService) -> list[Frame]:
    return [frame for frame in _queued(reporting) if frame.header.kind == KIND_SESSION_META]


def _snapshot_hash(reporting: ReportingService) -> str:
    """The content hash of the newest snapshot manifest this queue holds."""
    manifests = [frame for frame in _queued(reporting) if frame.header.kind == KIND_SNAPSHOT_MANIFEST]
    return manifests[-1].header.scope.removeprefix("manifest:") if manifests else ""


async def test_a_meta_row_for_an_unbound_session_is_dropped(tmp_path: Path) -> None:
    reporting = ReportingService(EventBus(), _config(tmp_path))

    reporting.offer_meta(ReportMeta(session_id="never-bound", title="Orphan"))

    assert reporting._metas == {}


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
        # Arming only remembers: no queue directory is created for it.
        assert not config.directory.exists()
    finally:
        startup.clear_armed()


async def test_starting_without_an_armed_configuration_does_nothing() -> None:
    assert await startup.start_armed_reporting(EventBus()) is None
    assert hooks.active_reporting() is None


async def test_the_armed_runtime_observes_every_bus_and_closes_with_its_last_bus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.service.reporting import service as service_module

    monkeypatch.setattr(service_module, "resolve_sessions_dir", lambda **_kwargs: tmp_path / "sessions")
    startup.arm_reporting(replace(Settings(), report_enabled=True, report_endpoint="", report_poll_interval_ms=60_000))
    first_bus, second_bus = EventBus(), EventBus()
    try:
        first = await startup.start_armed_reporting(first_bus)
        second = await startup.start_armed_reporting(second_bus)

        assert first is not None
        assert second is first
        assert hooks.active_reporting() is first

        # The second host's sessions are collected too: its bus is observed.
        await second_bus.publish(
            SessionReady(session_id=SESSION_ID, primary_cwd="", runtime_details=_runtime_details())
        )
        await wait_for(lambda: KIND_AGENT_ENV in _kinds(first), description="second bus observed")

        await startup.detach_reporting(first_bus)
        assert hooks.active_reporting() is first
        await startup.detach_reporting(second_bus)
        assert hooks.active_reporting() is None

        # A host that starts after the last bus went away gets a fresh runtime.
        third = await startup.start_armed_reporting(EventBus())
        assert third is not None
        assert third is not first
    finally:
        startup.clear_armed()
        await startup.stop_reporting()


class _RecordingService(ReportingService):
    """A service that records the rows the save hook hands over."""

    def __init__(self, bus: EventBus, config: ReportConfig) -> None:
        super().__init__(bus, config)
        self.recorded: list[ReportMeta] = []

    def offer_meta(self, meta: ReportMeta) -> None:
        self.recorded.append(meta)


def test_the_save_hook_forwards_only_primitives(tmp_path: Path) -> None:
    reporting = _RecordingService(EventBus(), _config(tmp_path))
    hooks.install_reporting(reporting)
    try:
        persistence = SessionPersistence(None, EventBus())
        persistence._publish_report_meta(
            SESSION_ID,
            {
                "messages": [Message(role="user", contents=["Fix the parser"])],
                "turn_counter": 3,
                TOTAL_SESSION_TOKENS_KEY: 42,
            },
            agent_profile_name="reviewer",
            agent_display_name="Reviewer",
            workspace=Workspace(primary_cwd=str(tmp_path / "repo")),
            model_profile=ModelProfile(
                id="model-1",
                name="Claude",
                provider="anthropic",
                model_id="claude",
            ),
        )
    finally:
        hooks.clear_reporting()

    assert len(reporting.recorded) == 1
    meta = reporting.recorded[0]
    assert meta.session_id == SESSION_ID
    assert meta.title == "Fix the parser"
    assert (meta.turn_count, meta.total_tokens, meta.message_count) == (3, 42, 1)
    assert meta.agent_profile == "reviewer"
    assert (meta.model_provider, meta.model_id) == ("anthropic", "claude")
    assert meta.primary_cwd == str(tmp_path / "repo")


def test_the_save_hook_is_a_no_op_when_reporting_is_off() -> None:
    assert hooks.active_reporting() is None

    SessionPersistence(None, EventBus())._publish_report_meta(
        SESSION_ID,
        {"messages": []},
        agent_profile_name="reviewer",
        agent_display_name="",
        workspace=None,
        model_profile=None,
    )

    assert hooks.active_reporting() is None


@dataclass
class _NegotiatingTransport:
    """Defers every batch unit and answers a manifest query with what the test names."""

    batches: list[list[Frame]] = field(default_factory=list)
    manifest_calls: int = 0
    missing_chunks: tuple[str, ...] = ()
    missing_files: tuple[str, ...] = ()
    acked_last_sequence: int = 0
    failure: Exception | None = None

    def post_batch(self, url: str, token: str, body: bytes, *, gzip_encoded: bool, timeout: float) -> TransportResult:
        frames = decode_frames(decompress_body(body, compressed=gzip_encoded))
        self.batches.append(frames)
        reply = json.dumps(
            {
                "accepted": [],
                "duplicates": [],
                "rejected": [],
                "deferred": [frame.header.unit_id for frame in frames],
                "retry_after_ms": 0,
            }
        ).encode()
        return TransportResult(status=200, body=reply)

    def post_manifest(self, url: str, token: str, body: bytes, *, timeout: float) -> TransportResult:
        self.manifest_calls += 1
        if self.failure is not None:
            raise self.failure
        answer = {
            "missing_chunk_hashes": list(self.missing_chunks),
            "missing_file_shas": list(self.missing_files),
            "acked_last_sequence": self.acked_last_sequence,
            "known_snapshot": True,
        }
        return TransportResult(status=200, body=json.dumps(answer).encode())

    def close(self) -> None:
        return


@contextlib.contextmanager
def _collector(tmp_path: Path, transport: _NegotiatingTransport) -> Iterator[ReportingService]:
    """A service with its collectors wired by hand: no pump thread, no driver task."""
    config = replace(_config(tmp_path), endpoint="http://report.test")
    service = ReportingService(EventBus(), config, transport=transport)
    wal = DurableWAL(config.directory, queue_bytes_max=config.queue_bytes_max, diagnostics=ReportDiagnostics())
    wal.open()
    service._wal = wal
    service._cursors = SessionCursorStore(wal.cursors_dir)
    service._snapshot = SnapshotTap(wal)
    service._reconciler = ManifestReconciler(config, transport)
    service._bind(SESSION_ID, "")
    try:
        yield service
    finally:
        wal.close()


def test_reconciliation_reopens_the_blocks_the_server_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.service.reporting import service as service_module

    sessions = tmp_path / "sessions"
    monkeypatch.setattr(service_module, "resolve_sessions_dir", lambda **_kwargs: sessions)
    (sessions / session_short_id(SESSION_ID)).mkdir(parents=True)
    block = b"snapshot bytes"
    (sessions / session_short_id(SESSION_ID) / "session.json").write_bytes(block)
    digest = hashlib.sha256(block).hexdigest()
    transport = _NegotiatingTransport(missing_chunks=(digest,), missing_files=("file-sha",), acked_last_sequence=7)

    with _collector(tmp_path, transport) as reporting:
        cursors = reporting._cursors
        assert cursors is not None
        cursors.save(
            SESSION_ID,
            SessionCursor(
                snapshot_content_hash="content",
                snapshot_chunk_hashes=[digest],
                snapshot_size=len(block),
                snapshot_mtime_ns=1,
                workspace_manifest_sha256="manifest",
                workspace_file_shas=["kept-sha", "file-sha"],
                workspace_signature="signature",
                workspace_kind=WORKSPACE_KIND_NO_REPO,
            ),
        )

        assert reporting._collect([], [SESSION_ID]) is True

        cursor = cursors.load(SESSION_ID)
        assert cursor.snapshot_chunk_hashes == [""]
        assert (cursor.snapshot_size, cursor.snapshot_mtime_ns) == (0, 0)
        assert cursor.workspace_file_shas == ["kept-sha"]
        assert cursor.workspace_signature == ""
        assert cursor.trajectory_last_sequence == 7
        binding = reporting._bindings[SESSION_ID]
        assert (binding.snapshot_pending, binding.workspace_pending) == (True, True)

        # The reopened cursor makes the next round queue the missing block again.
        requests = reporting._drain()
        assert len(requests) == 1
        assert reporting._collect(requests) is False

        frames = _queued(reporting)
        assert [frame.header.kind for frame in frames] == [KIND_SNAPSHOT_CHUNK, KIND_SNAPSHOT_MANIFEST]
        assert frames[0].payload == block


def test_a_failed_negotiation_leaves_the_cursor_untouched(tmp_path: Path) -> None:
    transport = _NegotiatingTransport(failure=OSError("manifest endpoint is gone"))

    with _collector(tmp_path, transport) as reporting:
        cursors = reporting._cursors
        assert cursors is not None
        cursors.save(
            SESSION_ID,
            SessionCursor(snapshot_content_hash="content", snapshot_chunk_hashes=["digest"], snapshot_size=12),
        )

        assert reporting._collect([], [SESSION_ID]) is False

        assert transport.manifest_calls == 1
        cursor = cursors.load(SESSION_ID)
        assert cursor.snapshot_chunk_hashes == ["digest"]
        assert cursor.snapshot_size == 12


async def test_a_deferred_session_reconciles_and_resends_the_missing_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.service.reporting import service as service_module

    sessions = tmp_path / "sessions"
    monkeypatch.setattr(service_module, "resolve_sessions_dir", lambda **_kwargs: sessions)
    folder = sessions / session_short_id(SESSION_ID)
    folder.mkdir(parents=True)
    block = b"snapshot bytes"
    (folder / "session.json").write_bytes(block)
    digest = hashlib.sha256(block).hexdigest()
    transport = _NegotiatingTransport(missing_chunks=(digest,), acked_last_sequence=3)
    config = replace(_config(tmp_path), endpoint="http://report.test")
    bus = EventBus()
    reporting = ReportingService(bus, config, transport=transport)
    await reporting.start()
    try:
        await bus.publish(SessionReady(session_id=SESSION_ID, primary_cwd=""))
        await bus.publish(SessionSaved(session_id=SESSION_ID))

        await wait_for(lambda: transport.manifest_calls > 0, timeout=15.0, description="manifest negotiation")
        await wait_for(
            lambda: len([frame for frame in _queued(reporting) if frame.header.kind == KIND_SNAPSHOT_CHUNK]) >= 2,
            timeout=15.0,
            description="the missing block was queued again",
        )

        cursors = reporting._cursors
        assert cursors is not None
        # The ack write and the re-queue are two steps of one round; wait for the
        # cursor instead of assuming it landed before the block came back.
        await wait_for(
            lambda: cursors.load(SESSION_ID).trajectory_last_sequence == 3,
            timeout=15.0,
            description="the negotiated ack reached the cursor",
        )
    finally:
        await reporting.close()


async def test_a_failing_negotiation_does_not_stop_the_pump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.service.reporting import service as service_module

    sessions = tmp_path / "sessions"
    monkeypatch.setattr(service_module, "resolve_sessions_dir", lambda **_kwargs: sessions)
    folder = sessions / session_short_id(SESSION_ID)
    folder.mkdir(parents=True)
    (folder / "session.json").write_bytes(b"snapshot bytes")
    transport = _NegotiatingTransport(failure=OSError("manifest endpoint is gone"))
    config = replace(_config(tmp_path), endpoint="http://report.test")
    bus = EventBus()
    reporting = ReportingService(bus, config, transport=transport)
    await reporting.start()
    try:
        await bus.publish(SessionReady(session_id=SESSION_ID, primary_cwd=""))
        await bus.publish(SessionSaved(session_id=SESSION_ID))
        await wait_for(lambda: transport.manifest_calls > 0, timeout=15.0, description="manifest negotiation")

        # The negotiation raised, yet the sender still works: a later save
        # reaches it and the pump thread is alive.
        before = len(transport.batches)
        await bus.publish(SessionSaved(session_id=SESSION_ID))
        await wait_for(
            lambda: len(transport.batches) > before, timeout=15.0, description="another batch reached the transport"
        )
        pump = reporting._pump
        assert pump is not None
        assert pump.running
    finally:
        await reporting.close()
