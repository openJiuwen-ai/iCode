# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Herdr reporter folding and sending, driven through a real EventBus."""

from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from itertools import pairwise
from typing import TYPE_CHECKING

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ApprovalRequest,
    ApprovalResponse,
    ApprovalReviewed,
    AskUserResponse,
    ExecutionChanged,
    QuestionToUser,
    SessionReady,
    SessionRestored,
    WorkflowNodeAnswered,
    WorkflowNodeAskUser,
)
from chrys.foundation.models.ask_user import AskUserQuestion
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.service import herdr_reporter
from chrys.service.herdr_reporter import HerdrReporter
from tests.support.waiting import wait_for, wait_until

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _turn(changed: str = "turn") -> ExecutionChanged:
    return ExecutionChanged(snapshot=ExecutionSnapshot(changed, cancellable=True))


def _state_at(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


class _RecordingCli:
    """Herdr CLI double; thread-safe because every send runs in ``to_thread``."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self._lock = threading.Lock()

    def __call__(self, argv: list[str]) -> None:
        with self._lock:
            self.calls.append(list(argv))

    def states(self) -> list[str]:
        with self._lock:
            return [_state_at(argv, "--state") for argv in self.calls if "--state" in argv]

    def snapshot(self) -> list[list[str]]:
        with self._lock:
            return [list(argv) for argv in self.calls]


@pytest.fixture
async def cli(monkeypatch: pytest.MonkeyPatch) -> _RecordingCli:
    recorder = _RecordingCli()
    monkeypatch.setattr(herdr_reporter, "_invoke_herdr_cli", recorder)
    return recorder


@asynccontextmanager
async def _reporting(bus: EventBus) -> AsyncIterator[HerdrReporter]:
    reporter = HerdrReporter(pane_id="pane-1", bin_path="/usr/bin/herdr")
    await reporter.start(bus)
    try:
        yield reporter
    finally:
        await reporter.close()


async def test_from_env_outside_herdr_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HERDR_ENV", "HERDR_PANE_ID", "HERDR_BIN_PATH", "HERDR_SOCKET_PATH"):
        monkeypatch.delenv(name, raising=False)
    assert HerdrReporter.from_env() is None
    monkeypatch.setenv("HERDR_ENV", "1")
    assert HerdrReporter.from_env() is None
    monkeypatch.setenv("HERDR_PANE_ID", "  ")
    monkeypatch.setenv("HERDR_BIN_PATH", "/usr/bin/herdr")
    assert HerdrReporter.from_env() is None
    monkeypatch.setenv("HERDR_PANE_ID", "pane-1")
    reporter = HerdrReporter.from_env()
    assert reporter is not None
    monkeypatch.setenv("HERDR_ENV", "0")
    assert HerdrReporter.from_env() is None


async def test_turn_lifecycle_reports_working_then_idle(cli: _RecordingCli) -> None:
    bus = EventBus()
    async with _reporting(bus):
        await bus.publish(_turn())
        await wait_for(lambda: cli.states() == ["working"], description="working report")
        await bus.publish(_turn("idle"))
        await wait_for(lambda: cli.states() == ["working", "idle"], description="idle report")


async def test_workflow_run_reports_working(cli: _RecordingCli) -> None:
    bus = EventBus()
    async with _reporting(bus):
        await bus.publish(_turn("workflow"))
        await wait_for(lambda: cli.states() == ["working"], description="working report")


async def test_approval_blocks_then_resumes(cli: _RecordingCli) -> None:
    bus = EventBus()
    async with _reporting(bus):
        await bus.publish(_turn())
        await wait_for(lambda: cli.states() == ["working"], description="working report")
        await bus.publish(ApprovalRequest(request_id="r1", tool_name="Bash", intent_summary="rm -rf build"))
        await wait_for(lambda: cli.states() == ["working", "blocked"], description="blocked report")
        message = _state_at(cli.snapshot()[-1], "--message")
        assert message == "Bash: rm -rf build"
        await bus.publish(ApprovalResponse(request_id="r1", approved=True))
        await wait_for(lambda: cli.states() == ["working", "blocked", "working"], description="resumed report")


async def test_judging_approval_blocks_only_once_flagged(cli: _RecordingCli) -> None:
    bus = EventBus()
    async with _reporting(bus):
        await bus.publish(_turn())
        await wait_for(lambda: cli.states() == ["working"], description="working report")
        await bus.publish(ApprovalRequest(request_id="r1", tool_name="Bash", judging=True))
        assert not await wait_until(lambda: "blocked" in cli.states(), timeout=0.5)
        await bus.publish(ApprovalReviewed(request_id="r1", approved=False, reason="destructive command"))
        await wait_for(lambda: cli.states() == ["working", "blocked"], description="blocked report")
        message = _state_at(cli.snapshot()[-1], "--message")
        assert message == "destructive command"


async def test_ask_user_blocks_until_answered(cli: _RecordingCli) -> None:
    bus = EventBus()
    async with _reporting(bus):
        await bus.publish(_turn())
        await wait_for(lambda: cli.states() == ["working"], description="working report")
        await bus.publish(
            QuestionToUser(
                request_id="q1",
                questions=(AskUserQuestion(question="Which database?", header="Choice"),),
            )
        )
        await wait_for(lambda: cli.states() == ["working", "blocked"], description="blocked report")
        message = _state_at(cli.snapshot()[-1], "--message")
        assert message == "Choice"
        await bus.publish(AskUserResponse(request_id="q1"))
        await wait_for(lambda: cli.states() == ["working", "blocked", "working"], description="resumed report")


async def test_workflow_node_ask_user_blocks(cli: _RecordingCli) -> None:
    bus = EventBus()
    async with _reporting(bus):
        await bus.publish(_turn("workflow"))
        await wait_for(lambda: cli.states() == ["working"], description="working report")
        await bus.publish(
            WorkflowNodeAskUser(request_id="w1", questions=(AskUserQuestion(question="Proceed with cleanup?"),))
        )
        await wait_for(lambda: cli.states() == ["working", "blocked"], description="blocked report")
        message = _state_at(cli.snapshot()[-1], "--message")
        assert message == "Proceed with cleanup?"
        await bus.publish(WorkflowNodeAnswered(request_id="w1"))
        await wait_for(lambda: cli.states() == ["working", "blocked", "working"], description="resumed report")


async def test_session_report_carries_resume_argv_and_dedups(cli: _RecordingCli) -> None:
    bus = EventBus()
    async with _reporting(bus):
        await bus.publish(SessionReady(session_id="sess-abc"))
        await wait_for(lambda: len(cli.calls) == 1, description="session report")
        call = cli.snapshot()[0]
        assert _state_at(call, "--state") == "idle"
        assert _state_at(call, "--agent-session-id") == "sess-abc"
        assert call[call.index("--") + 1 :] == ["icode", "--session", "sess-abc"]
        # SessionReady refires on every rebuild; the same session never re-arms it.
        await bus.publish(SessionReady(session_id="sess-abc"))
        assert not await wait_until(lambda: len(cli.calls) > 1, timeout=0.5)
        await bus.publish(SessionRestored(session_id="sess-def"))
        await wait_for(lambda: len(cli.calls) == 2, description="restored session report")
        second = cli.snapshot()[1]
        assert _state_at(second, "--agent-session-id") == "sess-def"


async def test_in_flight_send_merges_to_latest_state(monkeypatch: pytest.MonkeyPatch) -> None:
    class _GatedCli:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.gate = threading.Event()

        def __call__(self, argv: list[str]) -> None:
            self.calls.append(list(argv))
            self.gate.wait(timeout=5)

    gated = _GatedCli()
    monkeypatch.setattr(herdr_reporter, "_invoke_herdr_cli", gated)
    bus = EventBus()
    async with _reporting(bus):
        await bus.publish(_turn())
        await wait_for(lambda: len(gated.calls) == 1, description="first send in flight")
        await bus.publish(ApprovalRequest(request_id="r1", tool_name="Bash"))
        await bus.publish(ApprovalResponse(request_id="r1", approved=True))
        gated.gate.set()
        # blocked never hits the wire: the folded state is working again by
        # the time the in-flight send finishes.
        assert not await wait_until(lambda: len(gated.calls) > 1, timeout=0.5)


async def test_seq_values_are_strictly_monotonic(cli: _RecordingCli) -> None:
    bus = EventBus()
    async with _reporting(bus):
        for changed, want in (("turn", "working"), ("idle", "idle"), ("turn", "working"), ("idle", "idle")):
            await bus.publish(_turn(changed))
            await wait_for(lambda want=want: cli.states()[-1:] == [want], description=f"{want} report")
        seqs = [int(_state_at(argv, "--seq")) for argv in cli.snapshot() if "--seq" in argv]
    assert len(seqs) >= 4
    assert all(later > earlier for earlier, later in pairwise(seqs))


async def test_failed_send_is_silent_and_next_change_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FlakyCli:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []
            self.fail_next = True

        def __call__(self, argv: list[str]) -> None:
            self.calls.append(list(argv))
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("herdr is gone")

    flaky = _FlakyCli()
    monkeypatch.setattr(herdr_reporter, "_invoke_herdr_cli", flaky)
    bus = EventBus()
    async with _reporting(bus):
        await bus.publish(_turn())
        await wait_for(lambda: len(flaky.calls) == 1, description="failing send attempted")
        # The failed report never marked the state as sent.
        await bus.publish(_turn("idle"))
        await wait_for(lambda: len(flaky.calls) == 2, description="retry send")
        assert _state_at(flaky.calls[1], "--state") == "idle"


async def test_close_releases_the_pane_exactly_once_per_call(cli: _RecordingCli) -> None:
    bus = EventBus()
    reporter = HerdrReporter(pane_id="pane-1", bin_path="/usr/bin/herdr")
    await reporter.start(bus)
    await reporter.close()
    await wait_for(lambda: any("release-agent" in argv for argv in cli.calls), description="release sent")
    release = next(argv for argv in cli.snapshot() if "release-agent" in argv)
    assert release[:4] == ["/usr/bin/herdr", "pane", "release-agent", "pane-1"]
    assert release[release.index("--source") + 1] == "icode"
    # Closing again is safe and sends one more best-effort release.
    await reporter.close()
    await wait_for(
        lambda: sum(1 for argv in cli.calls if "release-agent" in argv) == 2,
        description="second release sent",
    )
