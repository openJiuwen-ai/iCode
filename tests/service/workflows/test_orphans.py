# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Restore-time orphan reconciliation: the appended terminal, idempotence, torn tails, and what gets skipped."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import threading
import uuid
from pathlib import Path

import pytest

import chrys.service.workflows.layout as layout
import chrys.service.workflows.orphans as orphans
from chrys.foundation.platform.files import atomic_write_owner_only_bytes
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.util.lock import FileLock
from chrys.service.workflows.layout import EVENTS_FILE, HEADER_FILE, WORKFLOWS_DIR
from chrys.service.workflows.layout import run_dir as run_dir_of
from chrys.service.workflows.orphans import OrphanReconciliation, read_run_terminal, reconcile_orphaned_runs
from chrys.service.workflows.outcomes import ORPHAN_REASON_PROCESS_TERMINATED, RunOutcome
from chrys.service.workflows.store import (
    RunHeader,
    RunRecord,
    RunSpec,
    WorkflowRunStore,
    read_run_events,
    read_run_header,
)


def _header(session_id: str, run_id: str | None = None) -> RunHeader:
    return RunHeader(
        run_id=run_id or new_analytics_id(),
        session_id=session_id,
        workflow_id="wf",
        source_kind="workdir",
        canonical_path="/p/wf.py",
        title="t",
        input_excerpt="go",
        entry_digest="e" * 64,
        manifest_digest="m" * 64,
        schema_version=1,
        spec_digest="s" * 64,
    )


async def _seed_run(session_dir: Path, *, records: int = 2, terminal: str | None = None) -> Path:
    """A run whose store was closed (its runtime markers are in the log) with or without a run terminal."""
    header = _header(session_dir.name)
    run_dir = run_dir_of(session_dir, header.run_id)
    store = WorkflowRunStore.open(
        spec=RunSpec(manifest={"nodes": {}}, environment={}, resolved_nodes=()),
        input_text="go",
        run_dir=run_dir,
        header=header,
        source=b"workflow = None\n",
    )
    for index in range(records):
        await store.append(RunRecord.NODE_STATE, {"node": f"n{index}", "state": "running"})
    if terminal is not None:
        await store.finish(terminal, {})
    assert await store.close() is True
    return run_dir


def _last_sequence(run_dir: Path) -> int:
    return read_run_events(run_dir).events[-1].sequence


async def test_an_unfinished_run_gets_its_orphaned_terminal_at_the_next_sequence(tmp_path: Path) -> None:
    session_dir = tmp_path / str(uuid.uuid4())
    run_dir = await _seed_run(session_dir)
    before = read_run_events(run_dir)
    assert all(event.event_type != RunRecord.RUN_FINISHED for event in before.events)
    last_seq = before.events[-1].sequence

    result = reconcile_orphaned_runs(session_dir)

    assert result == OrphanReconciliation(reconciled=(run_dir.name,))
    after = read_run_events(run_dir)
    assert after.corrupt_lines == []
    assert after.torn_tail_bytes == 0
    terminal = after.events[-1]
    assert terminal.event_type == RunRecord.RUN_FINISHED
    assert terminal.sequence == last_seq + 1
    assert terminal.payload == {"outcome": RunOutcome.ORPHANED.value, "reason": ORPHAN_REASON_PROCESS_TERMINATED}
    assert terminal.runtime_id == read_run_header(run_dir)["run_id"]
    assert terminal.coverage_id == before.events[-1].coverage_id
    assert terminal.branch_id == before.events[-1].branch_id
    terminal = read_run_terminal(run_dir)
    assert (terminal.outcome, terminal.last_seq, terminal.reason) == (
        RunOutcome.ORPHANED.value,
        last_seq + 1,
        ORPHAN_REASON_PROCESS_TERMINATED,
    )
    if sys.platform != "win32":
        assert (run_dir / EVENTS_FILE).stat().st_mode & 0o777 == 0o600
        assert (run_dir / HEADER_FILE).stat().st_mode & 0o777 == 0o600


async def test_repeated_reconciliation_appends_nothing(tmp_path: Path) -> None:
    session_dir = tmp_path / str(uuid.uuid4())
    run_dir = await _seed_run(session_dir)
    reconcile_orphaned_runs(session_dir)
    size = (run_dir / EVENTS_FILE).stat().st_size
    header = read_run_header(run_dir)

    result = reconcile_orphaned_runs(session_dir)

    assert result == OrphanReconciliation(already_finished=(run_dir.name,))
    assert (run_dir / EVENTS_FILE).stat().st_size == size
    assert read_run_header(run_dir) == header


async def test_a_torn_tail_is_terminated_and_reads_as_one_corrupt_line(tmp_path: Path) -> None:
    session_dir = tmp_path / str(uuid.uuid4())
    run_dir = await _seed_run(session_dir)
    last_seq = _last_sequence(run_dir)
    torn = b'{"schema_version": 1, "sequence": 99, "event_type": "workflow.node.state", "payload": {"no'
    with (run_dir / EVENTS_FILE).open("ab") as log:
        log.write(torn)

    result = reconcile_orphaned_runs(session_dir)

    assert result.reconciled == (run_dir.name,)
    after = read_run_events(run_dir)
    assert after.torn_tail_bytes == 0
    assert len(after.corrupt_lines) == 1
    assert after.events[-1].event_type == RunRecord.RUN_FINISHED
    assert after.events[-1].sequence == last_seq + 1  # the torn line's sequence is never trusted
    assert read_run_terminal(run_dir).last_seq == last_seq + 1
    assert reconcile_orphaned_runs(session_dir).already_finished == (run_dir.name,)


async def test_a_header_only_run_gets_a_fresh_log_starting_at_one(tmp_path: Path) -> None:
    session_dir = tmp_path / str(uuid.uuid4())
    header = _header(session_dir.name)
    run_dir = run_dir_of(session_dir, header.run_id)
    run_dir.mkdir(parents=True)
    atomic_write_owner_only_bytes(run_dir / HEADER_FILE, json.dumps(header.to_dict()).encode())

    result = reconcile_orphaned_runs(session_dir)

    assert result.reconciled == (run_dir.name,)
    after = read_run_events(run_dir)
    assert [event.sequence for event in after.events] == [1]
    assert after.events[0].event_type == RunRecord.RUN_FINISHED
    assert after.events[0].runtime_id == header.run_id
    assert after.events[0].session_id == session_dir.name
    assert read_run_terminal(run_dir).last_seq == 1


async def test_a_log_with_only_undecodable_lines_continues_after_the_highest_sequence_seen(tmp_path: Path) -> None:
    session_dir = tmp_path / str(uuid.uuid4())
    header = _header(session_dir.name)
    run_dir = run_dir_of(session_dir, header.run_id)
    run_dir.mkdir(parents=True)
    atomic_write_owner_only_bytes(run_dir / HEADER_FILE, json.dumps(header.to_dict()).encode())
    atomic_write_owner_only_bytes(
        run_dir / EVENTS_FILE, b'{"schema_version": 1, "sequence": 7}\n{"schema_version": 1, "sequence": 3}\n'
    )

    reconcile_orphaned_runs(session_dir)

    after = read_run_events(run_dir)
    assert after.events[-1].sequence == 8
    assert len(after.corrupt_lines) == 2


async def test_a_finished_log_leaves_the_header_unchanged(tmp_path: Path) -> None:
    session_dir = tmp_path / str(uuid.uuid4())
    run_dir = await _seed_run(session_dir, terminal="completed")
    finished_seq = read_run_terminal(run_dir).last_seq
    stale = {key: value for key, value in read_run_header(run_dir).items() if key not in {"outcome", "last_seq"}}
    atomic_write_owner_only_bytes(run_dir / HEADER_FILE, json.dumps(stale).encode())
    size = (run_dir / EVENTS_FILE).stat().st_size

    result = reconcile_orphaned_runs(session_dir)

    assert result == OrphanReconciliation(already_finished=(run_dir.name,))
    assert (run_dir / EVENTS_FILE).stat().st_size == size
    header = read_run_header(run_dir)
    assert header == stale
    assert read_run_terminal(run_dir).last_seq == finished_seq
    assert not (run_dir / orphans.RECONCILE_LOCK_FILE).exists()
    assert "reason" not in header


async def test_terminal_reason_and_timestamp_are_owned_by_the_log(tmp_path: Path) -> None:
    """A finished log never needs a header rewrite or an append lock."""
    session_dir = tmp_path / str(uuid.uuid4())
    header = _header(session_dir.name)
    run_dir = run_dir_of(session_dir, header.run_id)
    store = WorkflowRunStore.open(
        spec=RunSpec(manifest={"nodes": {}}, environment={}, resolved_nodes=()),
        input_text="go",
        run_dir=run_dir,
        header=header,
        source=b"workflow = None\n",
    )
    terminal_seq = await store.append(
        RunRecord.RUN_FINISHED, {"outcome": "cancelled", "reason": "deadline_exceeded"}, durable=True
    )
    assert await store.close() is True
    assert "outcome" not in read_run_header(run_dir)

    result = reconcile_orphaned_runs(session_dir)

    assert result == OrphanReconciliation(already_finished=(run_dir.name,))
    recovered = read_run_header(run_dir)
    terminal = read_run_terminal(run_dir)
    assert terminal.finished_at
    assert (terminal.outcome, terminal.reason, terminal.last_seq) == (
        "cancelled",
        "deadline_exceeded",
        terminal_seq,
    )
    assert reconcile_orphaned_runs(session_dir) == OrphanReconciliation(already_finished=(run_dir.name,))
    assert read_run_header(run_dir) == recovered


async def test_directories_without_a_header_are_not_runs(tmp_path: Path) -> None:
    session_dir = tmp_path / str(uuid.uuid4())
    workflows = session_dir / WORKFLOWS_DIR
    (workflows / "aborted-create").mkdir(parents=True)
    (workflows / "aborted-create" / EVENTS_FILE).write_bytes(b"")
    (workflows / ".partial").mkdir()
    (workflows / ".partial" / HEADER_FILE).write_text("{}", encoding="utf-8")
    (workflows / "stray.txt").write_text("", encoding="utf-8")

    assert reconcile_orphaned_runs(session_dir) == OrphanReconciliation()
    assert reconcile_orphaned_runs(tmp_path / "no-such-session") == OrphanReconciliation()


@pytest.mark.parametrize(
    "header_bytes",
    [
        pytest.param(b"not json", id="unparseable"),
        pytest.param(b"[]", id="not-an-object"),
        pytest.param(b'{"session_id": "s"}', id="no-run-id"),
        pytest.param(b'{"run_id": "", "session_id": "s"}', id="empty-run-id"),
        pytest.param(b'{"run_id": "r"}', id="no-session-id"),
    ],
)
def test_a_run_whose_header_cannot_be_trusted_is_skipped_and_reported(tmp_path: Path, header_bytes: bytes) -> None:
    session_dir = tmp_path / str(uuid.uuid4())
    run_dir = run_dir_of(session_dir, "r" * 32)
    run_dir.mkdir(parents=True)
    atomic_write_owner_only_bytes(run_dir / HEADER_FILE, header_bytes)

    result = reconcile_orphaned_runs(session_dir)

    assert result == OrphanReconciliation(skipped=(run_dir.name,))
    assert not (run_dir / EVENTS_FILE).exists()
    assert (run_dir / HEADER_FILE).read_bytes() == header_bytes


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs a privilege on Windows")
def test_a_planted_header_link_is_never_followed(tmp_path: Path) -> None:
    session_dir = tmp_path / str(uuid.uuid4())
    header = _header(session_dir.name)
    run_dir = run_dir_of(session_dir, header.run_id)
    run_dir.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps(header.to_dict()), encoding="utf-8")
    os.symlink(elsewhere, run_dir / HEADER_FILE)

    result = reconcile_orphaned_runs(session_dir)

    assert result == OrphanReconciliation(skipped=(run_dir.name,))
    assert not (run_dir / EVENTS_FILE).exists()


async def test_one_bad_run_does_not_stop_the_others(tmp_path: Path) -> None:
    session_dir = tmp_path / str(uuid.uuid4())
    good = await _seed_run(session_dir)
    bad = run_dir_of(session_dir, "b" * 32)
    bad.mkdir()
    atomic_write_owner_only_bytes(bad / HEADER_FILE, b"{")

    result = reconcile_orphaned_runs(session_dir)

    assert result.reconciled == (good.name,)
    assert result.skipped == (bad.name,)


async def test_the_scan_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session_dir = tmp_path / str(uuid.uuid4())
    for _ in range(3):
        await _seed_run(session_dir, records=0)
    monkeypatch.setattr(layout, "MAX_SCANNED_RUN_DIRS", 2)

    result = reconcile_orphaned_runs(session_dir)

    assert result.truncated is True
    assert len(result.reconciled) <= 2
    assert not result.skipped


async def test_two_reconciliations_of_one_run_take_turns_and_append_one_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restore cancelled mid-reconciliation leaves its thread running; the next restore's waits for it.

    The first reconciliation is held between its read and its append; the second reaches the run's lock
    while it is held, and reads only after the first appended, so it finds the terminal instead of adding one.
    """
    session_dir = tmp_path / str(uuid.uuid4())
    run_dir = await _seed_run(session_dir)
    last_seq = _last_sequence(run_dir)
    entered = threading.Event()
    release = threading.Event()
    second_at_lock = threading.Event()
    reads: list[bool] = []
    real_write = orphans._write_all

    def _write(fd: int, payload: bytes) -> None:
        reads.append(True)
        entered.set()
        assert release.wait(5)
        real_write(fd, payload)

    class _ObservedLock(FileLock):
        def acquire(self) -> None:
            if entered.is_set():
                second_at_lock.set()
            super().acquire()

    monkeypatch.setattr(orphans, "_write_all", _write)
    monkeypatch.setattr(orphans, "FileLock", _ObservedLock)
    first = asyncio.create_task(asyncio.to_thread(reconcile_orphaned_runs, session_dir))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        second = asyncio.create_task(asyncio.to_thread(reconcile_orphaned_runs, session_dir))
        assert await asyncio.to_thread(second_at_lock.wait, 5)
        assert not first.done()
    finally:
        release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result == OrphanReconciliation(reconciled=(run_dir.name,))
    assert second_result == OrphanReconciliation(already_finished=(run_dir.name,))
    assert reads == [True]
    after = read_run_events(run_dir)
    terminals = [event for event in after.events if event.event_type == RunRecord.RUN_FINISHED]
    assert len(terminals) == 1
    assert terminals[0].sequence == last_seq + 1
    assert after.corrupt_lines == []
    terminal = read_run_terminal(run_dir)
    assert (terminal.outcome, terminal.last_seq) == (RunOutcome.ORPHANED.value, last_seq + 1)


@pytest.mark.skipif(
    sys.platform == "win32", reason="the held lock file keeps the directory from being deleted on Windows"
)
async def test_a_run_deleted_during_terminal_append_is_not_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir = tmp_path / str(uuid.uuid4())
    await _seed_run(session_dir)
    entered, release = threading.Event(), threading.Event()
    real_write = orphans._write_all

    def paused(fd: int, payload: bytes) -> None:
        entered.set()
        assert release.wait(5)
        real_write(fd, payload)

    monkeypatch.setattr(orphans, "_write_all", paused)
    task = asyncio.create_task(asyncio.to_thread(reconcile_orphaned_runs, session_dir))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        shutil.rmtree(session_dir)
    finally:
        release.set()
        await task
    assert not session_dir.exists()
