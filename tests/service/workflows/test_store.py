# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run store: layout, ordered sequences, the four storage faults, and append cost."""

from __future__ import annotations

import asyncio
import errno
import json
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import pytest

from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory.writer import FdWriteBackend
from chrys.service.workflows import store as store_module
from chrys.service.workflows.orphans import read_run_terminal
from chrys.service.workflows.store import (
    MAX_DIAGNOSTICS_BYTES,
    RunHeader,
    RunRecord,
    RunSpec,
    WorkflowRunStore,
    WorkflowStorageFailed,
    node_emits_path,
    read_node_diagnostics,
    read_node_emits,
    read_node_value,
    read_run_events,
    read_run_header,
    read_run_spec,
)
from tests.support.gc_isolation import earlier_tests_heap_frozen
from tests.support.trajectory_invariants import assert_trajectory_accounted
from tests.support.waiting import ENGINE_TURN_TIMEOUT


def header() -> RunHeader:
    return RunHeader(
        run_id=new_analytics_id(),
        session_id=str(uuid.uuid4()),
        workflow_id="wf",
        source_kind="workdir",
        canonical_path="/work/.chrys/workflows/wf.py",
        title="t",
        input_excerpt="go",
        entry_digest="e" * 64,
        manifest_digest="m" * 64,
        schema_version=1,
        spec_digest="s" * 64,
        started_at="2026-09-13T00:00:00Z",
        mode="headless",
    )


@dataclass
class FaultBackend:
    """Real descriptor backend with scripted write, short-write, and fsync failures."""

    inner: FdWriteBackend
    fail_writes: int = 0
    short_write: int | None = None
    fail_fsync: bool = False

    def write(self, data: memoryview) -> int:
        if self.short_write is not None:
            count = self.inner.write(data[: self.short_write])
            self.short_write = None
            self.fail_writes += 1
            return count
        if self.fail_writes:
            self.fail_writes -= 1
            raise OSError(errno.ENOSPC, "no space left")
        return self.inner.write(data)

    def fsync(self) -> None:
        if self.fail_fsync:
            raise OSError(errno.EIO, "fsync failed")
        self.inner.fsync()

    def truncate(self, size: int) -> None:
        self.inner.truncate(size)

    def close(self) -> None:
        self.inner.close()


def _open(tmp_path: Path, **faults: object) -> tuple[WorkflowRunStore, Path]:
    run_dir = tmp_path / "run"
    store = WorkflowRunStore.open(
        spec=RunSpec(
            manifest={"nodes": {}},
            environment={"python_version": "3.14.7"},
            resolved_nodes=({"node_id": "review", "profile": "QA"},),
        ),
        input_text="go",
        run_dir=run_dir,
        header=header(),
        source=b"workflow = None\n",
        backend_factory=lambda fd: FaultBackend(FdWriteBackend(fd), **faults),  # type: ignore[arg-type]
        write_ack_timeout=1.0,
    )
    return store, run_dir


async def test_open_takes_the_module_bound_as_it_stands_unless_given_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The root conftest widens the bound for every test; a store opened without
    # one of its own reads the name at open time, so hosts can widen it too.
    assert store_module.RUN_RECORD_WRITE_ACK_TIMEOUT_SECONDS == ENGINE_TURN_TIMEOUT
    monkeypatch.setattr(store_module, "RUN_RECORD_WRITE_ACK_TIMEOUT_SECONDS", 7.5)
    unhurried = WorkflowRunStore.open(
        tmp_path / "unhurried",
        header=header(),
        spec=RunSpec(manifest={"nodes": {}}, environment={}, resolved_nodes=()),
        input_text="go",
        source=b"workflow = None\n",
    )
    explicit, _run_dir = _open(tmp_path)
    try:
        assert unhurried._writer.write_ack_timeout == 7.5
        assert explicit._writer.write_ack_timeout == 1.0
    finally:
        await unhurried.close()
        await explicit.close()


async def test_layout_sequences_and_close_markers(tmp_path: Path) -> None:
    store, run_dir = _open(tmp_path)
    assert (run_dir / "source.py").read_bytes() == b"workflow = None\n"
    written = read_run_header(run_dir)
    assert written["run_id"] == store.header.run_id
    assert read_run_spec(run_dir)["resolved_nodes"] == [{"node_id": "review", "profile": "QA"}]
    assert written["spec_digest"] == "s" * 64
    if sys.platform != "win32":
        assert (run_dir / "events.jsonl").stat().st_mode & 0o777 == 0o600
        assert run_dir.stat().st_mode & 0o777 == 0o700

    sequences = [await store.append(RunRecord.NODE_STATE, {"node": "a", "state": s}) for s in ("running", "done")]
    assert sequences == [1, 2]
    assert store.last_written_seq == 2
    value_path = store.write_node_value("a@iter#1", 1, "output", {"text": "hello", "data": None})
    assert json.loads(value_path.read_text(encoding="utf-8")) == {"text": "hello", "data": None}
    assert value_path.parent == run_dir / "nodes"

    assert store.write_node_value("a@iter#1", 1, "input", {"text": "in", "data": {"k": 1}}).parent == run_dir / "nodes"
    if sys.platform != "win32":
        assert (run_dir / "nodes").stat().st_mode & 0o777 == 0o700
    assert read_node_value(run_dir, "a@iter#1", 1, "input") == {"text": "in", "data": {"k": 1}}
    assert read_node_value(run_dir, "a@iter#1", 2, "input") is None

    assert await store.finish("cancelled", {"outputs": ["a"], "reason": "deadline_exceeded"}) == 3
    assert await store.close() is True

    result = read_run_events(run_dir)
    assert [event.event_type for event in result.events] == [
        RunRecord.NODE_STATE,
        RunRecord.NODE_STATE,
        RunRecord.RUN_FINISHED,
        EventType.CHECKPOINT,
        EventType.COVERAGE_ENDED,
        EventType.RUNTIME_FINISHED,
    ]
    assert result.unsupported_event_count == 0
    assert result.torn_tail_bytes == 0
    assert result.events[2].payload == {"outcome": "cancelled", "outputs": ["a"], "reason": "deadline_exceeded"}
    assert result.events[0].runtime_id == store.header.run_id
    assert_trajectory_accounted(result)
    assert read_run_header(run_dir) == written
    terminal = read_run_terminal(run_dir)
    assert (terminal.outcome, terminal.reason, terminal.last_seq) == ("cancelled", "deadline_exceeded", 3)


async def test_every_workflow_event_type_is_accepted_and_reads_back_known(tmp_path: Path) -> None:
    store, run_dir = _open(tmp_path)
    types = [
        RunRecord.RUN_STARTED,
        RunRecord.NODE_STATE,
        RunRecord.NODE_OUTPUT,
        RunRecord.NODE_ASK,
        RunRecord.LOOP_ITERATION,
        RunRecord.RUN_NOTICE,
        RunRecord.RETRY_KEY,
    ]
    for event_type in types:
        await store.append(event_type, {"node": "a"})
    await store.finish("completed", {})
    await store.close()
    result = read_run_events(run_dir)
    assert [event.event_type for event in result.events][: len(types) + 1] == [*types, RunRecord.RUN_FINISHED]
    assert result.unsupported_event_count == 0


async def test_a_surrogateescaped_path_in_the_header_reads_back_unchanged(tmp_path: Path) -> None:
    executable = "/work/venv-\udcff/bin/python"  # what os.fsdecode() makes of a b"\xff" byte on POSIX: identity
    store = WorkflowRunStore.open(
        spec=RunSpec(
            manifest={"nodes": {}},
            environment={"executable": executable},
            resolved_nodes=({"node_id": "review", "profile": "QA"},),
        ),
        input_text="go",
        run_dir=tmp_path / "run",
        header=header(),
        source=b"workflow = None\n",
    )
    try:
        assert read_run_spec(tmp_path / "run")["environment"]["executable"] == executable
    finally:
        await store.close()


async def test_node_value_files_stay_distinct_after_sanitising(tmp_path: Path) -> None:
    store, _ = _open(tmp_path)
    paths = [
        store.write_node_value(activation, 1, "output", {"text": activation, "data": None})
        for activation in ("a:b@iter#1", "a_b@iter#1", "A_b@iter#1")
    ]
    assert len({path.name.lower() for path in paths}) == 3
    assert [json.loads(path.read_text(encoding="utf-8"))["text"] for path in paths] == [
        "a:b@iter#1",
        "a_b@iter#1",
        "A_b@iter#1",
    ]
    await store.close()


async def test_append_rejects_unknown_types_and_closed_store(tmp_path: Path) -> None:
    store, _ = _open(tmp_path)
    with pytest.raises(ValueError, match="not a workflow event type"):
        await store.append("session.started", {})
    await store.close()
    with pytest.raises(WorkflowStorageFailed, match="closed"):
        await store.append(RunRecord.NODE_STATE, {"node": "a"})


async def test_id_looking_payload_keys_become_a_gap_not_a_line(tmp_path: Path) -> None:
    """The envelope holds every ``*_id`` key to an id format: such a record is refused, the run continues."""
    store, run_dir = _open(tmp_path)
    with pytest.raises(WorkflowStorageFailed, match="not written"):
        await store.append(RunRecord.NODE_STATE, {"node_id": "a@iter#1"})
    assert await store.append(RunRecord.NODE_STATE, {"activation": "a@iter#1"}) == 3
    assert await store.close() is True
    result = read_run_events(run_dir)
    assert [event.event_type for event in result.events][:2] == [EventType.GAP, RunRecord.NODE_STATE]
    assert_trajectory_accounted(result)


async def test_enospc_degrades_the_store_and_the_prefix_stays_accounted(tmp_path: Path) -> None:
    store, run_dir = _open(tmp_path, fail_writes=1)
    with pytest.raises(WorkflowStorageFailed, match="write_failure"):
        await store.append(RunRecord.NODE_STATE, {"node": "a"})
    with pytest.raises(WorkflowStorageFailed):
        await store.append(RunRecord.NODE_STATE, {"node": "b"})
    await store.close()
    result = read_run_events(run_dir)
    assert result.corrupt_lines == []
    assert_trajectory_accounted(result)


async def test_partial_write_leaves_no_torn_line(tmp_path: Path) -> None:
    store, run_dir = _open(tmp_path, short_write=10)
    with pytest.raises(WorkflowStorageFailed):
        await store.append(RunRecord.NODE_STATE, {"node": "a"})
    await store.close()
    result = read_run_events(run_dir)
    assert result.corrupt_lines == []
    assert result.torn_tail_bytes == 0
    assert_trajectory_accounted(result)


async def test_fsync_failure_fails_the_durable_append(tmp_path: Path) -> None:
    """Lines still reach the file, so the store is not degraded; durability is what the caller loses."""
    store, _ = _open(tmp_path, fail_fsync=True)
    assert await store.append(RunRecord.NODE_STATE, {"node": "a"}) == 1
    with pytest.raises(WorkflowStorageFailed, match="durable"):
        await store.append(RunRecord.RUN_FINISHED, {"outcome": "completed"}, durable=True)
    assert await store.append(RunRecord.NODE_STATE, {"node": "b"}) == 4
    assert await store.close() is False
    assert await store.close() is False  # the verdict is kept; "already closed" never turns it into True


async def test_concurrent_closes_share_one_close_and_its_verdict(tmp_path: Path) -> None:
    store, _ = _open(tmp_path, fail_fsync=True)
    await store.append(RunRecord.NODE_STATE, {"node": "a"})
    assert await asyncio.gather(store.close(), store.close()) == [False, False]


async def test_a_cancelled_close_waiter_leaves_the_shared_close_alone(tmp_path: Path) -> None:
    store, _ = _open(tmp_path, fail_fsync=True)
    await store.append(RunRecord.NODE_STATE, {"node": "a"})
    first = asyncio.create_task(store.close())
    second = asyncio.create_task(store.close())
    await asyncio.sleep(0)  # both wait on the one close, which has not run yet
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert await first is False
    assert await store.close() is False  # the verdict survives for later callers too


async def test_torn_tail_is_reported_and_the_prefix_still_reads(tmp_path: Path) -> None:
    store, run_dir = _open(tmp_path)
    await store.append(RunRecord.NODE_STATE, {"node": "a"})
    await store.close()
    with (run_dir / "events.jsonl").open("ab") as handle:
        handle.write(b'{"schema_version": 1, "seq')
    result = read_run_events(run_dir)
    assert result.torn_tail_bytes == len(b'{"schema_version": 1, "seq')
    assert result.events[0].event_type == RunRecord.NODE_STATE
    assert_trajectory_accounted(result)


async def test_thousand_appends_are_cheap(tmp_path: Path, record_property: pytest.FixtureRequest) -> None:
    store, _ = _open(tmp_path)
    with earlier_tests_heap_frozen():
        started = time.perf_counter()
        for index in range(1000):
            await store.append(RunRecord.NODE_STATE, {"node": "a", "state": "running", "index": index})
        elapsed = time.perf_counter() - started
    record_property("append_1000_seconds", round(elapsed, 3))
    assert store.last_written_seq == 1000
    await store.close()
    assert elapsed < 10.0


async def test_long_activation_ids_fit_the_atomic_writer(tmp_path: Path) -> None:
    store, _ = _open(tmp_path)
    path = store.write_node_value("n" * 230 + "@iter#1", 1, "output", {"text": "long", "data": None})
    assert json.loads(path.read_text(encoding="utf-8"))["text"] == "long"
    await store.close()


async def test_emits_append_per_attempt_and_read_back_in_order(tmp_path: Path) -> None:
    store, run_dir = _open(tmp_path)
    try:
        store.append_node_emit("n@iter#1", 1, 1, "first")
        store.append_node_emit("n@iter#1", 1, 2, "second, with a lone surrogate \udcff")
        store.append_node_emit("n@iter#1", 2, 1, "the retry's own log")
        assert read_node_emits(run_dir, "n@iter#1", 1) == [(1, "first"), (2, "second, with a lone surrogate \udcff")]
        assert read_node_emits(run_dir, "n@iter#1", 2) == [(1, "the retry's own log")]
        assert read_node_emits(run_dir, "n@iter#1", 3) == []
        assert read_node_emits(run_dir, "other@iter#1", 1) == []

        # A torn tail (the process died mid-line) ends the readable prefix instead of failing the read.
        with node_emits_path(run_dir, "n@iter#1", 1).open("ab") as handle:
            handle.write(b'{"ordinal": 3, "text": "cut')
        assert read_node_emits(run_dir, "n@iter#1", 1) == [(1, "first"), (2, "second, with a lone surrogate \udcff")]
    finally:
        await store.close()


async def test_an_unwritable_emit_log_is_a_storage_failure(tmp_path: Path) -> None:
    store, run_dir = _open(tmp_path)
    try:
        (run_dir / "nodes").rmdir()
        (run_dir / "nodes").write_text("not a directory", encoding="utf-8")
        with pytest.raises(WorkflowStorageFailed, match="Cannot append"):
            store.append_node_emit("n@iter#1", 1, 1, "lost")
    finally:
        await store.close()


async def test_diagnostics_keep_phases_iterations_and_attempts_separate(
    tmp_path: Path,
) -> None:
    store, directory = _open(tmp_path)
    try:
        store.write_node_diagnostics("a", 1, phase="body", iteration=0)
        assert list((directory / "nodes").iterdir()) == []
        store.write_node_diagnostics("a", 1, phase="body", iteration=0, stdout="body", truncated=True)
        store.write_node_diagnostics("a", 1, phase="until", iteration=2, traceback="evaluation traceback")
        store.write_node_diagnostics("a", 2, phase="body", iteration=0, stdout="retry")
        assert read_node_diagnostics(directory, "a", 1) == {
            "phases": [
                {"phase": "body", "iteration": 0, "stdout": {"text": "body", "truncated": True}, "traceback": ""},
                {
                    "phase": "until",
                    "iteration": 2,
                    "stdout": {"text": "", "truncated": False},
                    "traceback": "evaluation traceback",
                },
            ]
        }
        assert read_node_diagnostics(directory, "a", 2) == {
            "phases": [
                {"phase": "body", "iteration": 0, "stdout": {"text": "retry", "truncated": False}, "traceback": ""}
            ]
        }
        assert read_node_diagnostics(directory, "a", 3) is None
        if sys.platform != "win32":
            assert all(path.stat().st_mode & 0o777 == 0o600 for path in (directory / "nodes").iterdir())
        store.write_node_diagnostics("oversize", 1, phase="body", iteration=0, traceback="x" * MAX_DIAGNOSTICS_BYTES)
        assert read_node_diagnostics(directory, "oversize", 1) is None
        store.write_node_value("malformed", 1, "diagnostics", {"phases": None})
        store.write_node_diagnostics("malformed", 1, phase="body", iteration=0, stdout="diagnostic")
    finally:
        await store.close()


def test_header_migration_skips_active_sessions_and_uses_the_bounded_codec(tmp_path, monkeypatch):
    from chrys.foundation.platform.files import atomic_write_owner_only_bytes
    from chrys.foundation.util.lock import FileLock
    from chrys.service.state._session_files import session_active_lock_path

    session = tmp_path / "sessions" / "session-a"
    path = session / "workflows" / "run" / "run.json"
    original = b'{"source_kind":"project","title":"Old"}'
    atomic_write_owner_only_bytes(path, original)
    lock = session_active_lock_path(session.parent, session.name)
    lock.parent.mkdir()
    copies = {}
    errors = []

    def migrate():
        return store_module.migrate_run_headers(
            session, backup=copies.__setitem__, on_read_error=lambda path, err: errors.append((path, err))
        )

    with FileLock(lock):
        assert not migrate()
    assert path.read_bytes() == original
    with monkeypatch.context() as patch:
        patch.setattr(store_module, "MAX_HEADER_BYTES", len(original))
        assert read_run_header(path.parent)["source_kind"] == "workdir"
        assert not migrate()  # indented output would exceed the shared writer's limit
    assert path.read_bytes() == original
    assert not copies and not errors

    assert migrate()
    assert copies == {path: original}
    assert json.loads(path.read_bytes())["source_kind"] == "workdir"
