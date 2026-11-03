# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise terminal status through real task handoffs and workflow UI changes."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest

from chrys.app.tui.terminal.program_status import ProgramStatusReporter
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ExecutionChanged,
    InvocationMessage,
    UserInterrupt,
    UserMessage,
    UserRetry,
    WorkflowRunFinished,
)
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.service.llm.mock import MockChatClient, MockResponse
from tests.app.tui.screens.main._workflow_support import (
    WorkflowEngine,
    open_workflow,
    save_workflow_session,
    switch_mode,
)
from tests.app.tui.screens.main.test_workflow_result import _accept_live_run, _requests
from tests.app.tui.terminal.test_program_status import (
    _DONE,
    _IDLE,
    _WORKING,
    _App,
    _ForwardingDriver,
    _msg,
    _seq,
    _turn_origin,
)
from tests.orchestration.engine.run.test_retry_integration import started_retry_engine
from tests.orchestration.workflows._hosting import make_project
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import (
    ENGINE_TEST_WAIT_TIMEOUT,
    ENGINE_TURN_TIMEOUT,
    await_run_task_chain,
    wait_for,
    with_wait_deadline,
)
from tests.support.workflow_history import record_workflow_run

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from chrys.kernel import ChatResponse, FinishReason, FinishReasonLiteral


def _reports(driver: _ForwardingDriver) -> list[str]:
    return [value for value in driver.writes if value.startswith("\x1b]7501;")]


@pytest.mark.parametrize("outcome", ["completed", "failed"])
@pytest.mark.parametrize("action", ["new", "restore", "switch-mode", "failed-restore"])
async def test_workflow_context_changes_clear_only_acknowledged_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, outcome: str
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    bus, engine = EventBus(), WorkflowEngine()
    requests = await _requests(bus)
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(140, 42)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        driver = _ForwardingDriver(app._driver)
        app._driver = driver
        run_id = await _accept_live_run(pilot, engine, bus, requests)
        await bus.publish(WorkflowRunFinished(run_id=run_id, outcome=outcome))
        await engine.set_execution(ExecutionSnapshot("idle"), bus)
        # AIxCoding branding: OSC app name assertions
        result = _DONE if outcome == "completed" else _seq("state=error:app=AIxCoding")
        assert _reports(driver)[-1] == result

        if action == "new":
            assert await main._workflow.session_view.new_session()
            await pilot.pause()
            assert main._workflow.session_id == ""
            assert not main._workflow_panel.run_ids
        elif action == "restore":
            restored_id, archived = str(uuid4()), uuid4().hex
            store = main._services.state_store
            assert store is not None
            await record_workflow_run(
                store.session_dir(restored_id) / "workflows" / archived,
                session_id=restored_id,
                title="Archived",
                outcome="completed",
            )
            await save_workflow_session(store, restored_id, Path.cwd())
            await main._workflow.session_view.restore_session(restored_id)
            await wait_for(lambda: main._workflow_panel.run_id == archived, pilot=pilot)
        elif action == "failed-restore":
            await main._workflow.session_view.restore_session(str(uuid4()))
            await wait_for(lambda: not main._workflow.session_view.restore_flow.active, pilot=pilot)
        else:
            await switch_mode(main, pilot)
            assert main._workflow.workflow_mode is False
        assert _reports(driver)[-1] == (result if action == "failed-restore" else _IDLE)
    assert _reports(driver)[-1] == _IDLE


async def test_switching_from_chat_to_workflow_acknowledges_chat_result(tmp_path: Path) -> None:
    bus = EventBus()
    app = make_chrys_app(tmp_path, event_bus=bus, engine=WorkflowEngine())
    async with app.run_test() as pilot:
        main = app._main_screen
        assert main is not None
        driver = _ForwardingDriver(app._driver)
        app._driver = driver
        await bus.publish(InvocationMessage(origin=_turn_origin(), text="Ready", is_final=True))
        # AIxCoding branding: OSC app name assertions
        assert _reports(driver)[-1] == _seq(f"state=done:app=AIxCoding:{_msg('Ready')}")

        await switch_mode(main, pilot)

        assert main._workflow.workflow_mode is True
        assert _reports(driver)[-1] == _IDLE


class _HeldClient(MockChatClient):
    def __init__(self) -> None:
        super().__init__(responses=[MockResponse(text="interrupted"), MockResponse(text="retry completed")])
        self.first_entered = asyncio.Event()
        self.first_cancelled = asyncio.Event()
        self.release_cancellation = asyncio.Event()
        self.second_entered = asyncio.Event()
        self.release_second = asyncio.Event()

    async def _mock_response(
        self,
        resp: MockResponse,
        model_id: str,
        finish_reason: FinishReasonLiteral | FinishReason,
        async_cb: Callable[[str], Awaitable[None]] | None,
    ) -> ChatResponse[Any]:
        if self.call_count == 1:
            self.first_entered.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.first_cancelled.set()
                await self.release_cancellation.wait()
                raise
        self.second_entered.set()
        await self.release_second.wait()
        return await super()._mock_response(resp, model_id, finish_reason, async_cb)


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_queued_retry_reports_working_and_done_without_an_idle_lease_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine
) -> None:
    client = _HeldClient()
    started = await started_retry_engine(
        agent_engine, tmp_path, monkeypatch, client_factory=lambda: client, stream=False
    )
    bus, engine = started.bus, started.engine
    app = _App()
    reporter = ProgramStatusReporter(bus, app_provider=lambda: app)
    await reporter.subscribe()
    snapshots: list[str] = []

    async def capture(event: ExecutionChanged) -> None:
        snapshots.append(event.snapshot.kind)

    await bus.subscribe(ExecutionChanged, capture)
    try:
        await bus.publish(UserMessage(text="start"))
        await wait_for(client.first_entered.is_set, timeout=ENGINE_TURN_TIMEOUT)
        await engine.turns.turn_state.lease.settle_notifications()
        await bus.publish(UserInterrupt())
        await wait_for(client.first_cancelled.is_set, timeout=ENGINE_TURN_TIMEOUT)
        await bus.publish(UserRetry())
        assert engine.turns.turn_state.lease.pending_retry.owner_admission_id is not None
        assert app.writes[-1] == _IDLE

        client.release_cancellation.set()
        await wait_for(client.second_entered.is_set, timeout=ENGINE_TURN_TIMEOUT)
        await engine.turns.turn_state.lease.settle_notifications()
        assert engine.is_turn_lifecycle_active
        assert snapshots == ["turn", "turn"]
        assert app.writes[-1] == _WORKING

        client.release_second.set()
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
        await engine.turns.turn_state.lease.settle_notifications()
        assert any(
            event.is_final and event.text == "retry completed"
            for event in started.events
            if isinstance(event, InvocationMessage)
        )
        # AIxCoding branding: OSC app name assertions
        assert app.writes[-1] == _seq(f"state=done:app=AIxCoding:{_msg('retry completed')}")
    finally:
        client.release_cancellation.set()
        client.release_second.set()
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
        await bus.unsubscribe(ExecutionChanged, capture)
        await reporter.close()
