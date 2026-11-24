# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Journal: publish order equals record order, the storage-failure freeze, and the out-of-band terminal."""

from __future__ import annotations

import asyncio
import errno
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    Event,
    WorkflowLoopIteration,
    WorkflowNodeAskUser,
    WorkflowNodeOutput,
    WorkflowNodeStateChanged,
    WorkflowOutputSummary,
    WorkflowRunFinished,
    WorkflowRunNotice,
    WorkflowRunStarted,
)
from chrys.foundation.models.ask_user import AskUserQuestion
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory.writer import FdWriteBackend
from chrys.service.workflows.journal import SUMMARY_MAX_CHARS, WorkflowJournal, summarize
from chrys.service.workflows.orphans import read_run_terminal
from chrys.service.workflows.outcomes import RunOutcome
from chrys.service.workflows.scheduler import AttemptRef
from chrys.service.workflows.store import (
    WORKFLOW_EVENT_TYPES,
    RunHeader,
    RunRecord,
    RunSpec,
    WorkflowRunStore,
    WorkflowStorageFailed,
    read_run_events,
    read_run_header,
)
from tests.service.workflows.test_store import FaultBackend
from tests.support.event_capture import capture_event_sequence

LIFECYCLE = (
    WorkflowRunStarted,
    WorkflowNodeStateChanged,
    WorkflowNodeOutput,
    WorkflowNodeAskUser,
    WorkflowLoopIteration,
    WorkflowRunNotice,
    WorkflowRunFinished,
)


def _header(session_id: str) -> RunHeader:
    return RunHeader(
        run_id=new_analytics_id(),
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


@dataclass
class _FailAfter:
    """Real descriptor backend whose ``successes``-th successor write hits ENOSPC."""

    inner: FdWriteBackend
    successes: int

    def write(self, data: memoryview) -> int:
        if self.successes == 0:
            raise OSError(errno.ENOSPC, "no space left")
        self.successes -= 1
        return self.inner.write(data)

    def fsync(self) -> None:
        self.inner.fsync()

    def truncate(self, size: int) -> None:
        self.inner.truncate(size)


def _open(
    tmp_path: Path, bus: EventBus | None, *, fail_after: int | None = None, fail_fsync: bool = False
) -> tuple[WorkflowJournal, Path]:
    session_id = str(uuid.uuid4())
    run_dir = tmp_path / "run"

    def backend(fd: int) -> Any:
        if fail_after is not None:
            return _FailAfter(FdWriteBackend(fd), fail_after)
        return FaultBackend(FdWriteBackend(fd), fail_fsync=fail_fsync)

    store = WorkflowRunStore.open(
        spec=RunSpec(manifest={"nodes": {"a": {}}}, environment={}, resolved_nodes=[{"node_id": "a"}]),
        input_text="go",
        run_dir=run_dir,
        header=_header(session_id),
        source=b"workflow = None\n",
        backend_factory=backend,
        write_ack_timeout=1.0,
    )
    return WorkflowJournal(store, bus, session_id=session_id), run_dir


def _ref(journal: WorkflowJournal, node: str = "a", attempt: int = 1) -> AttemptRef:
    return AttemptRef(journal.run_id, node, f"{node}@iter#1", attempt)


async def test_every_lifecycle_record_publishes_its_written_sequence(tmp_path: Path) -> None:
    bus = EventBus()
    journal, run_dir = _open(tmp_path, bus)
    async with capture_event_sequence(bus, *LIFECYCLE) as events:
        ref = _ref(journal)
        await journal.run_started()
        await journal.node_state(ref, "running", invocation_id="0123456789ab")
        await journal.node_output(ref, "emit", 1, "progress")
        await journal.node_ask(ref, "req-1", (AskUserQuestion(question="continue?"),))
        await journal.loop_iteration(AttemptRef(journal.run_id, "loop", "loop@iter#1", 1), 1, "continue")
        await journal.run_notice("a", "a@iter#1", "data_dropped_at_agent_boundary", "data dropped")
        await journal.retry_key("retry-1", ref)
        await journal.node_state(ref, "completed", durable=True)
        seq = await journal.finish(
            RunOutcome.COMPLETED, outputs=[WorkflowOutputSummary("a", "a@iter#1", "done")], duration=1.5
        )
        assert await journal.store.close() is True

    assert seq == 10  # the durable ``completed`` state is followed by a checkpoint marker at 9
    assert [type(event) for event in events] == [
        WorkflowRunStarted,
        WorkflowNodeStateChanged,
        WorkflowNodeOutput,
        WorkflowNodeAskUser,
        WorkflowLoopIteration,
        WorkflowRunNotice,
        WorkflowNodeStateChanged,
        WorkflowRunFinished,
    ]
    sequences = [event.seq for event in events if not isinstance(event, WorkflowRunFinished)]
    assert sequences == [1, 2, 3, 4, 5, 6, 8]  # 7 = the retry key, store only
    assert all(event.session_id == journal.store.header.session_id for event in events)
    started = events[0]
    assert isinstance(started, WorkflowRunStarted)
    assert started.manifest == {"nodes": {"a": {}}}
    assert started.resolved_nodes == [{"node_id": "a"}]
    running = events[1]
    assert isinstance(running, WorkflowNodeStateChanged)
    assert (running.node_id, running.activation_id, running.attempt, running.invocation_id) == (
        "a",
        "a@iter#1",
        1,
        "0123456789ab",
    )
    finished = events[-1]
    assert isinstance(finished, WorkflowRunFinished)
    assert finished.outcome == "completed"
    assert finished.outputs == [WorkflowOutputSummary("a", "a@iter#1", "done")]
    assert finished.degraded is False

    result = read_run_events(run_dir)
    records = [event for event in result.events if event.event_type in WORKFLOW_EVENT_TYPES]
    assert [event.event_type for event in records] == [
        RunRecord.RUN_STARTED,
        RunRecord.NODE_STATE,
        RunRecord.NODE_OUTPUT,
        RunRecord.NODE_ASK,
        RunRecord.LOOP_ITERATION,
        RunRecord.RUN_NOTICE,
        RunRecord.RETRY_KEY,
        RunRecord.NODE_STATE,
        RunRecord.RUN_FINISHED,
    ]
    # Records carry no ``*_id`` keys (the envelope would refuse them) and no unbounded fields.
    assert [event.sequence for event in records] == [1, 2, 3, 4, 5, 6, 7, 8, 10]
    assert records[0].payload == {}
    assert records[1].payload == {
        "node": "a",
        "activation": "a@iter#1",
        "attempt": 1,
        "state": "running",
        "iteration": 0,
        "failure_phase": "",
        "invocation": "0123456789ab",
    }
    assert records[6].payload == {"node": "a", "activation": "a@iter#1", "attempt": 1, "request": "retry-1"}
    assert records[8].payload == {
        "outcome": "completed",
        "outputs": [{"node": "a", "activation": "a@iter#1", "attempt": 0}],
        "duration": 1.5,
    }
    assert read_run_terminal(run_dir).outcome == "completed"


async def test_concurrent_records_publish_in_sequence_order(tmp_path: Path) -> None:
    bus = EventBus()
    journal, _ = _open(tmp_path, bus)
    async with capture_event_sequence(bus, WorkflowNodeOutput) as events:
        await asyncio.gather(*(journal.node_output(_ref(journal), "emit", n, f"emit {n}") for n in range(1, 41)))
        await journal.store.close()
    sequences = [event.seq for event in events if isinstance(event, WorkflowNodeOutput)]
    assert sequences == sorted(sequences) == list(range(1, 41))


async def test_summaries_are_bounded_and_full_text_is_not_the_records_business(tmp_path: Path) -> None:
    bus = EventBus()
    journal, run_dir = _open(tmp_path, bus)
    long_text = "x" * (SUMMARY_MAX_CHARS * 8)
    async with capture_event_sequence(bus, WorkflowNodeOutput, WorkflowNodeStateChanged) as events:
        await journal.node_output(_ref(journal), "final", 1, long_text)
        await journal.node_state(_ref(journal), "failed", error=long_text, error_class="python_exception")
        await journal.store.close()
    output, failed = events
    assert isinstance(output, WorkflowNodeOutput)
    assert isinstance(failed, WorkflowNodeStateChanged)
    assert output.summary_text == summarize(long_text)
    assert len(output.summary_text) == SUMMARY_MAX_CHARS
    assert output.summary_text.endswith("…")
    assert failed.error == summarize(long_text)
    assert failed.error_class == "python_exception"
    result = read_run_events(run_dir)
    assert result.events[0].payload["summary"] == summarize(long_text)
    assert result.unsupported_event_count == 0


async def test_a_failed_append_freezes_the_journal_and_the_terminal_goes_out_of_band(tmp_path: Path) -> None:
    bus = EventBus()
    journal, run_dir = _open(tmp_path, bus, fail_after=2)
    async with capture_event_sequence(bus, *LIFECYCLE) as events:
        ref = _ref(journal)
        await journal.node_state(ref, "running")
        await journal.node_output(ref, "emit", 1, "one")
        with pytest.raises(WorkflowStorageFailed):
            await journal.node_output(ref, "emit", 2, "two")
        assert journal.storage_failed is True
        # Every later record is refused the same way: no side effect after the freeze.
        with pytest.raises(WorkflowStorageFailed):
            await journal.node_state(ref, "cancelled")
        assert await journal.finish(RunOutcome.CANCELLED, duration=0.5) is None
        assert await journal.finish(RunOutcome.CANCELLED) is None
        await journal.store.close()

    assert [type(event) for event in events] == [WorkflowNodeStateChanged, WorkflowNodeOutput, WorkflowRunFinished]
    terminal = events[-1]
    assert isinstance(terminal, WorkflowRunFinished)
    assert terminal.seq is None
    assert terminal.outcome == "storage_failed"
    assert terminal.degraded is True
    assert terminal.last_written_seq == 2
    assert terminal.duration == 0.5
    result = read_run_events(run_dir)
    assert [event.event_type for event in result.events][:2] == [RunRecord.NODE_STATE, RunRecord.NODE_OUTPUT]
    assert all(event.event_type != RunRecord.RUN_FINISHED for event in result.events)
    assert "outcome" not in read_run_header(run_dir)


async def test_a_terminal_that_cannot_be_written_is_published_as_storage_failed(tmp_path: Path) -> None:
    bus = EventBus()
    journal, _ = _open(tmp_path, bus, fail_fsync=True)
    async with capture_event_sequence(bus, WorkflowRunFinished) as events:
        await journal.node_state(_ref(journal), "running")
        assert await journal.finish(RunOutcome.COMPLETED) is None
        await journal.store.close()
    terminal = events[0]
    assert isinstance(terminal, WorkflowRunFinished)
    assert terminal.outcome == "storage_failed"
    assert terminal.seq is None
    assert terminal.last_written_seq == 2  # the finished line itself reached the file; its fsync did not


async def test_the_out_of_band_terminal_follows_every_published_record(tmp_path: Path) -> None:
    """A record being published when the failure lands is out before the terminal, never after."""
    bus = EventBus()
    journal, _ = _open(tmp_path, bus, fail_after=1)
    order: list[str] = []

    async def slow(event: Event) -> None:
        if isinstance(event, WorkflowNodeOutput):
            await asyncio.sleep(0.05)
        order.append(type(event).__name__)

    await bus.subscribe(WorkflowNodeOutput, slow)
    await bus.subscribe(WorkflowRunFinished, slow)
    ref = _ref(journal)
    first = asyncio.create_task(journal.node_output(ref, "emit", 1, "one"))
    await asyncio.sleep(0)
    second = asyncio.create_task(journal.node_output(ref, "emit", 2, "two"))
    with pytest.raises(WorkflowStorageFailed):
        await second
    await first
    await journal.finish(RunOutcome.CANCELLED)
    await journal.store.close()
    assert order == ["WorkflowNodeOutput", "WorkflowRunFinished"]


async def test_records_after_the_terminal_are_a_programming_error(tmp_path: Path) -> None:
    journal, _ = _open(tmp_path, None)
    await journal.finish(RunOutcome.COMPLETED)
    with pytest.raises(RuntimeError, match="after the run terminal"):
        await journal.node_state(_ref(journal), "running")
    await journal.store.close()


async def test_no_bus_still_records(tmp_path: Path) -> None:
    journal, run_dir = _open(tmp_path, None)
    assert await journal.node_state(_ref(journal), "running") == 1
    assert await journal.finish(RunOutcome.COMPLETED) == 2
    await journal.store.close()
    assert read_run_terminal(run_dir).last_seq == 2


def test_summarize_keeps_short_text_verbatim() -> None:
    assert summarize("short") == "short"
    assert summarize("") == ""
    assert summarize("ab", limit=1) == "…"


async def test_enospc_fault_message_names_the_writer_reason(tmp_path: Path) -> None:
    journal, _ = _open(tmp_path, None, fail_after=0)
    with pytest.raises(WorkflowStorageFailed, match="write_failure"):
        await journal.node_state(_ref(journal), "running")
    assert journal.store.last_written_seq == 0
    await journal.store.close()


async def _started(journal: WorkflowJournal) -> None:
    await journal.run_started()


async def test_a_wide_output_list_keeps_the_terminal_record_under_the_line_budget(tmp_path: Path) -> None:
    bus = EventBus()
    journal, run_dir = _open(tmp_path, bus)
    outputs = [WorkflowOutputSummary(f"review_{i}", f"review_{i}@iter#1", "ok") for i in range(80)]
    async with capture_event_sequence(bus, WorkflowRunFinished) as events:
        await _started(journal)
        seq = await journal.finish(RunOutcome.COMPLETED, outputs=outputs, duration=0.5)
        assert await journal.store.close() is True
    assert seq is not None
    finished = [e for e in events if isinstance(e, WorkflowRunFinished)]
    # The live event carries every output; only the durable record drops the identities.
    assert [o.node_id for o in finished[0].outputs] == [f"review_{i}" for i in range(80)]
    log = read_run_events(run_dir)
    assert not log.corrupt_lines and log.torn_tail_bytes == 0 and log.unsupported_event_count == 0
    records = [e.payload for e in log.events if e.event_type == RunRecord.RUN_FINISHED]
    assert records == [{"outcome": "completed", "outputs": [], "outputs_omitted": 80, "duration": 0.5}]
    assert read_run_terminal(run_dir).outcome == "completed"


async def test_a_small_output_list_is_recorded_verbatim(tmp_path: Path) -> None:
    journal, run_dir = _open(tmp_path, None)
    outputs = [WorkflowOutputSummary(f"n{i}", f"n{i}@iter#1", "ok") for i in range(3)]
    await _started(journal)
    await journal.finish(RunOutcome.COMPLETED, outputs=outputs, duration=0.25)
    assert await journal.store.close() is True
    records = [e.payload for e in read_run_events(run_dir).events if e.event_type == RunRecord.RUN_FINISHED]
    assert records == [
        {
            "outcome": "completed",
            "outputs": [{"node": f"n{i}", "activation": f"n{i}@iter#1", "attempt": 0} for i in range(3)],
            "duration": 0.25,
        }
    ]
