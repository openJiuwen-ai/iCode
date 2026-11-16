# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for sleep tool middleware."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import InvocationToolCallResult, SleepSkip, UserInterrupt
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.tool_kinds import KIND_SLEEP
from chrys.foundation.tool_result_metadata import TOOL_RESULT_METADATA_KEY
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.invoker.attempts import AttemptTaskHandle
from chrys.orchestration.invoker.origin import BoundEmitter
from chrys.service.agent_middleware._metadata_keys import _SLEEP_INTERRUPTED_KEY, _SLEEP_SKIPPED_KEY
from chrys.service.agent_middleware.control import sleep as sleep_mod
from chrys.service.agent_middleware.control.interrupt import InterruptMiddleware
from chrys.service.agent_middleware.control.sleep import SleepMiddleware
from chrys.service.agent_middleware.events.hook_dispatch import set_call_id
from chrys.service.agent_middleware.events.tool_events import ToolEventMiddleware
from chrys.service.tools.background import BackgroundTasks, TaskScope, current_scope
from tests.support.background_tasks import HeldProcess
from tests.support.frozen_clock import freeze_monotonic
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for


@pytest.fixture
def frozen_sleep_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the sleep's own clock: an early wake-up then reports exactly 0 seconds slept."""
    freeze_monotonic(monkeypatch, sleep_mod)


def _sleep_context(seconds: object, *, call_id: str = "sleep-call") -> SimpleNamespace:
    ctx = SimpleNamespace(
        function=SimpleNamespace(name="sleep", chrys_kind=KIND_SLEEP),
        arguments={"seconds": seconds, "reason": "test wait"},
        result=None,
        metadata={},
    )
    set_call_id(ctx, call_id)
    return ctx


async def test_sleep_middleware_completes_without_calling_tool() -> None:
    bus = EventBus()
    mw = SleepMiddleware(bus)
    ctx = _sleep_context(0)
    called = False

    async def _next() -> None:
        nonlocal called
        called = True

    await mw.process(ctx, _next)

    assert not called
    assert ctx.result == "Slept for 0 seconds."


@pytest.mark.usefixtures("frozen_sleep_clock")
async def test_sleep_middleware_skip_returns_tool_result() -> None:
    bus = EventBus()
    mw = SleepMiddleware(bus)
    ctx = _sleep_context(30, call_id="c1")

    async def _next() -> None:
        raise AssertionError("sleep middleware should not call the underlying tool")

    task = asyncio.create_task(mw.process(ctx, _next))
    await asyncio.sleep(0)

    await bus.publish(SleepSkip(call_id="c1"))
    await asyncio.wait_for(task, timeout=1)

    assert ctx.result == "Sleep skipped by user after 0 seconds (requested 30 seconds)."
    assert ctx.metadata[_SLEEP_SKIPPED_KEY] is True


@pytest.mark.usefixtures("frozen_sleep_clock")
async def test_sleep_middleware_user_interrupt_returns_tool_result() -> None:
    bus = EventBus()
    mw = SleepMiddleware(bus)
    ctx = _sleep_context(30, call_id="c1")

    async def _next() -> None:
        raise AssertionError("sleep middleware should not call the underlying tool")

    task = asyncio.create_task(mw.process(ctx, _next))
    await asyncio.sleep(0)

    await bus.publish(UserInterrupt())
    await asyncio.wait_for(task, timeout=1)

    assert ctx.result == "Sleep interrupted after 0 seconds (requested 30 seconds)."
    assert ctx.metadata[_SLEEP_INTERRUPTED_KEY] is True


@pytest.mark.usefixtures("frozen_sleep_clock")
async def test_executor_interrupt_lets_active_sleep_publish_result_before_cancel() -> None:
    bus = EventBus()
    sleep_mw = SleepMiddleware(bus)
    tool_events = ToolEventMiddleware(bus, origin=InvocationOrigin("turn", "", "turn-test", None))
    ctx = _sleep_context(30, call_id="c1")
    results: list[InvocationToolCallResult] = []

    async def _capture(event: InvocationToolCallResult) -> None:
        results.append(event)

    async def _next() -> None:
        async def _underlying() -> None:
            raise AssertionError("sleep middleware should not call the underlying tool")

        await sleep_mw.process(ctx, _underlying)

    await bus.subscribe(InvocationToolCallResult, _capture)
    task = asyncio.create_task(tool_events.process(ctx, _next))
    await asyncio.sleep(0)
    assert sleep_mw.active_call_ids == ("c1",)

    executor = TurnBindings.__new__(TurnBindings)
    executor._hosted_run_generation = 0
    executor._interrupt = InterruptMiddleware()
    executor._attempt_handle = AttemptTaskHandle(task=task)
    executor._sleep = sleep_mw
    executor._bus = bus
    executor._bound_emitter = BoundEmitter(bus, tool_events._origin)

    await TurnBindings.interrupt(executor)
    await asyncio.wait_for(task, timeout=1)

    assert results[-1].result == "Sleep interrupted after 0 seconds (requested 30 seconds)."
    assert results[-1].metadata["sleep_interrupted"] is True


async def test_sleep_tool_result_metadata_marks_skip() -> None:
    bus = EventBus()
    tool_events = ToolEventMiddleware(bus, origin=InvocationOrigin("turn", "", "turn-test", None))
    sleep_mw = SleepMiddleware(bus)
    ctx = _sleep_context(30, call_id="c1")
    results: list[InvocationToolCallResult] = []

    async def _capture(event: InvocationToolCallResult) -> None:
        results.append(event)

    async def _next() -> None:
        async def _underlying() -> None:
            raise AssertionError("sleep middleware should not call the underlying tool")

        await sleep_mw.process(ctx, _underlying)

    await bus.subscribe(InvocationToolCallResult, _capture)
    task = asyncio.create_task(tool_events.process(ctx, _next))
    await asyncio.sleep(0)

    await bus.publish(SleepSkip(call_id="c1"))
    await asyncio.wait_for(task, timeout=1)

    assert results[-1].metadata["sleep_skipped"] is True
    assert ctx.metadata[TOOL_RESULT_METADATA_KEY] == {"sleep_skipped": True}


async def test_sleep_middleware_rejects_long_sleep() -> None:
    bus = EventBus()
    mw = SleepMiddleware(bus)
    ctx = _sleep_context(3601)

    async def _next() -> None:
        raise AssertionError("invalid sleep should not call the underlying tool")

    await mw.process(ctx, _next)

    assert ctx.result.startswith("Error: sleep duration cannot exceed 3600 seconds.")


async def test_sleep_middleware_cancel_propagates() -> None:
    bus = EventBus()
    mw = SleepMiddleware(bus)
    ctx = _sleep_context(30)

    async def _next() -> None:
        raise AssertionError("sleep middleware should not call the underlying tool")

    task = asyncio.create_task(mw.process(ctx, _next))
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ctx.result is None


async def test_sleep_middleware_ignores_other_tools() -> None:
    bus = EventBus()
    mw = SleepMiddleware(bus)
    ctx = SimpleNamespace(
        function=SimpleNamespace(name="other", chrys_kind="search"),
        arguments={},
        result=None,
        metadata={},
    )
    called = False

    async def _next() -> None:
        nonlocal called
        called = True
        ctx.result = "ok"

    await mw.process(ctx, _next)

    assert called
    assert ctx.result == "ok"


# ───────────────────────── early wake ─────────────────────────


@pytest.fixture
async def tasks(tmp_path: Path) -> AsyncIterator[BackgroundTasks]:
    registry = BackgroundTasks(tmp_path)
    yield registry
    await registry.close()


async def _no_tool() -> None:
    raise AssertionError("sleep middleware should not call the underlying tool")


async def test_sleep_wakes_with_the_end_of_the_log_when_its_agents_task_ends(tasks: BackgroundTasks) -> None:
    process = HeldProcess()
    task = tasks.adopt(process, command="make", trigger="timeout")
    assert task is not None and process.sink is not None
    ctx = _sleep_context(600)
    sleeping = asyncio.create_task(SleepMiddleware(EventBus(), background_tasks=tasks).process(ctx, _no_tool))
    await asyncio.sleep(0)
    assert not sleeping.done()

    process.sink(0, b"compiled\n")
    process.exit(0)
    await asyncio.wait_for(sleeping, ENGINE_TURN_TIMEOUT)

    first, *rest = ctx.result.split("\n")
    assert first.startswith("Sleep ended early after ")
    assert first.endswith(" (requested 600 seconds).")
    assert rest == [
        f"Background task {task.id} (pid 4242): exited with code 0. Log: {task.log_path}",
        "End of its log:",
        "before",
        "compiled",
        "[icode] process exited with code 0",
    ]
    assert task.reported == task.outcome


async def test_sleep_wakes_once_when_its_agents_task_outlives_its_shell(tasks: BackgroundTasks) -> None:
    process = HeldProcess()
    task = tasks.adopt(process, command="make serve", trigger="timeout")
    assert task is not None
    middleware = SleepMiddleware(EventBus(), background_tasks=tasks)
    ctx = _sleep_context(600)
    sleeping = asyncio.create_task(middleware.process(ctx, _no_tool))
    await asyncio.sleep(0)
    assert not sleeping.done()

    process.outlive(0, (5001,))
    await asyncio.wait_for(sleeping, ENGINE_TURN_TIMEOUT)

    assert (
        f"Background task {task.id} (pid 5001): running after its shell exited with code 0. Log: {task.log_path}"
        in ctx.result
    )
    again = _sleep_context(0, call_id="again")
    await middleware.process(again, _no_tool)
    assert again.result == "Slept for 0 seconds."


async def test_an_end_that_comes_while_sleep_reads_the_logs_is_news_for_the_next_sleep(
    tasks: BackgroundTasks, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = HeldProcess()
    task = tasks.adopt(process, command="make serve", trigger="timeout")
    assert task is not None
    process.outlive(0, (5001,))
    await wait_for(lambda: task.shell_exit_code is not None, description="the task outlived its shell")
    loop = asyncio.get_running_loop()
    read_tails = sleep_mod._log_tails

    async def _end() -> None:
        process.exit(0)
        await task.ended.wait()

    def _tails_after_the_task_ended(paths: list[Path]) -> list[str]:
        # Runs in a worker thread while sleep awaits it.
        asyncio.run_coroutine_threadsafe(_end(), loop).result(ENGINE_TURN_TIMEOUT)
        return read_tails(paths)

    monkeypatch.setattr(sleep_mod, "_log_tails", _tails_after_the_task_ended)
    middleware = SleepMiddleware(EventBus(), background_tasks=tasks)
    first = _sleep_context(600, call_id="first")
    await asyncio.wait_for(middleware.process(first, _no_tool), ENGINE_TURN_TIMEOUT)
    monkeypatch.setattr(sleep_mod, "_log_tails", read_tails)
    second = _sleep_context(0, call_id="second")
    await middleware.process(second, _no_tool)

    assert f"Background task {task.id} (pid 5001): running after its shell exited with code 0." in first.result
    assert f"Background task {task.id} (pid 5001): ended (its shell had exited with code 0)." in second.result
    assert second.result.endswith("[icode] process ended (its shell had exited with code 0)")


@pytest.mark.usefixtures("frozen_sleep_clock")
async def test_sleep_returns_at_once_for_a_task_that_already_ended_and_says_so_only_once(
    tasks: BackgroundTasks,
) -> None:
    process = HeldProcess()
    task = tasks.adopt(process, command="make", trigger="timeout")
    assert task is not None
    process.exit(3)
    await wait_for(task.ended.is_set, description="the task ended")
    middleware = SleepMiddleware(EventBus(), background_tasks=tasks)

    first = _sleep_context(600, call_id="first")
    await asyncio.wait_for(middleware.process(first, _no_tool), ENGINE_TURN_TIMEOUT)
    second = _sleep_context(0, call_id="second")
    await middleware.process(second, _no_tool)

    assert first.result.startswith(
        "Sleep ended early after 0 seconds (requested 600 seconds).\n"
        f"Background task {task.id} (pid 4242): exited with code 3."
    )
    assert second.result == "Slept for 0 seconds."


async def test_sleep_hears_only_of_its_own_agents_tasks_and_not_of_their_stop_files(tasks: BackgroundTasks) -> None:
    child_scope = TaskScope("sub_agent", "invocation-1")
    served = tasks.adopt(HeldProcess(), command="serve", trigger="timeout")
    child_process = HeldProcess()
    child = tasks.adopt(child_process, command="pytest", trigger="timeout", scope=child_scope)
    assert served is not None and child is not None
    served.stop_path.touch()
    await tasks.stop_requested()
    child_process.exit(1)
    await wait_for(child.ended.is_set, description="the child's task ended")
    middleware = SleepMiddleware(EventBus(), background_tasks=tasks)

    main_sleep = _sleep_context(0, call_id="main")
    await middleware.process(main_sleep, _no_tool)
    token = current_scope.set(child_scope)
    try:
        child_sleep = _sleep_context(0, call_id="child")
        await middleware.process(child_sleep, _no_tool)
    finally:
        current_scope.reset(token)

    assert main_sleep.result == "Slept for 0 seconds."
    assert f"Background task {child.id} (pid 4242): exited with code 1." in child_sleep.result


async def test_main_agents_sleep_returns_once_the_user_sent_a_message() -> None:
    waiting = False
    ctx = _sleep_context(600)
    middleware = SleepMiddleware(EventBus(), user_message_waiting=lambda: waiting)
    sleeping = asyncio.create_task(middleware.process(ctx, _no_tool))
    await asyncio.sleep(0)
    assert not sleeping.done()

    waiting = True
    await asyncio.wait_for(sleeping, ENGINE_TURN_TIMEOUT)

    first, user = ctx.result.split("\n")
    assert first.startswith("Sleep ended early after ")
    assert user == "The user sent a new message."


def test_log_tails_keep_the_last_whole_lines_within_each_and_all_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    many = tmp_path / "many.log"
    # Bytes, as a log holds them: text mode would write Windows line ends, which count against the limits.
    many.write_bytes("".join(f"line {index}\n" for index in range(100)).encode())
    wide = tmp_path / "wide.log"
    wide.write_bytes(b"x" * 5000 + b"\nlast\n")

    assert sleep_mod._log_tails([many, wide, tmp_path / "missing.log"]) == [
        "\n".join(f"line {index}" for index in range(60, 100)),
        "last",
        "",
    ]

    short = tmp_path / "short.log"
    short.write_bytes(b"a\nb\n")
    monkeypatch.setattr(sleep_mod, "_TAILS_BYTES", 4)
    assert sleep_mod._log_tails([short, many]) == ["a\nb", ""]
