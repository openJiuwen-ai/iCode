# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A command the main agent moved to the background outlives its turn and ends with its session."""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from typing import Any

import psutil
import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.types import (
    BackgroundTasksChanged,
    BackgroundTaskStop,
    InvocationToolCallStart,
    ShellToBackground,
    UserInject,
)
from chrys.foundation.platform import get_platform
from chrys.orchestration.session_host import ChrysSessionHost
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile, CompactionConfig, ToolsConfig
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

_SERVE = """\
import os, pathlib, sys, time
pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding="utf-8")
while True:
    time.sleep(0.1)
"""


def _holds_pid(pid_file: Path) -> bool:
    """Whether the command has written its pid: ``write_text`` creates the file before it writes to it."""
    return pid_file.exists() and pid_file.read_text(encoding="utf-8").isdigit()


def _shell_call(call_id: str, script: Path, pid_file: Path, timeout: int) -> MockResponse:
    # PowerShell runs a quoted program path only through its call operator.
    prefix = "& " if sys.platform == "win32" else ""
    command = f'{prefix}"{sys.executable}" "{script}" "{pid_file}"'
    arguments = {"command": command, "reason": "serve", "timeout": timeout}
    return MockResponse(tool_calls=[(get_platform().shell.name, call_id, arguments)])


def _host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: MockChatClient, *, builtins: list[str]
) -> ChrysSessionHost:
    async def create_client(_profile: Any = None, **_kwargs: Any) -> MockChatClient:
        return client

    monkeypatch.setattr(builder_module, "create_client", create_client)
    models = ModelProfileRegistry()
    models.register(ModelProfile(id="mock", name="mock", provider="mock", model_id="mock", stream=False))
    agents = AgentProfileRegistry()
    agents.register(
        AgentProfile(
            name="Serve",
            instructions="Start the server.",
            tools=ToolsConfig(builtins=builtins),
            compaction=CompactionConfig(enabled=False),
        )
    )
    return ChrysSessionHost(
        profile_name="Serve",
        settings=Settings(model_profile="mock"),
        agent_registry=agents,
        model_registry=models,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )


async def test_background_command_outlives_an_interrupted_turn_and_stops_with_the_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "serve.py"
    script.write_text(_SERVE, encoding="utf-8")
    server_pid, held_pid = tmp_path / "server.pid", tmp_path / "held.pid"
    client = MockChatClient(
        responses=[
            _shell_call("call-1", script, server_pid, timeout=1),
            MockResponse(text="started"),
            _shell_call("call-2", script, held_pid, timeout=60),
        ]
    )

    host = _host(monkeypatch, tmp_path, client, builtins=["shell"])
    try:
        result = await host.run_until_final("start the server", timeout=ENGINE_TURN_TIMEOUT)
        assert result.text == "started"
        tasks = host.engine.session.background_tasks
        assert tasks is not None
        [task] = tasks.running
        await wait_for(lambda: _holds_pid(server_pid), timeout=ENGINE_TURN_TIMEOUT, description="the server started")
        server = psutil.Process(int(server_pid.read_text(encoding="utf-8")))

        # An interrupted turn kills its own command but leaves the background alone.
        turn = asyncio.create_task(host.run_until_final("hold"))
        await wait_for(lambda: _holds_pid(held_pid) or turn.done(), timeout=ENGINE_TURN_TIMEOUT, description="held")
        assert not turn.done()
        held = psutil.Process(int(held_pid.read_text(encoding="utf-8")))
        await host.cancel_current_turn()
        with pytest.raises(RuntimeError, match="cancelled"):
            await asyncio.wait_for(turn, ENGINE_TURN_TIMEOUT)
        await wait_for(lambda: not held.is_running(), timeout=ENGINE_TURN_TIMEOUT, description="held command killed")
        assert server.is_running()
        assert tasks.running == [task]
    finally:
        await host.shutdown()

    assert host.engine.session.background_tasks is None
    assert task.log_path.read_text(encoding="utf-8").endswith("[icode] process stopped at session end\n")
    await wait_for(lambda: not server.is_running(), timeout=ENGINE_TURN_TIMEOUT, description="the server exited")


async def test_the_user_moves_a_command_to_the_background_watches_it_and_stops_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "serve.py"
    script.write_text(_SERVE, encoding="utf-8")
    server_pid = tmp_path / "server.pid"
    client = MockChatClient(responses=[_shell_call("call-1", script, server_pid, timeout=60), MockResponse(text="ok")])
    host = _host(monkeypatch, tmp_path, client, builtins=["shell"])
    told: list[BackgroundTasksChanged] = []

    async def on_change(event: BackgroundTasksChanged) -> None:
        told.append(event)

    await host.event_bus.subscribe(BackgroundTasksChanged, on_change)
    try:
        turn = asyncio.create_task(host.run_until_final("serve"))
        await wait_for(lambda: _holds_pid(server_pid) or turn.done(), timeout=ENGINE_TURN_TIMEOUT, description="served")
        assert not turn.done()
        server = psutil.Process(int(server_pid.read_text(encoding="utf-8")))
        session_id = host.engine.session.session_id
        await host.event_bus.publish(ShellToBackground(session_id=session_id))

        assert (await asyncio.wait_for(turn, ENGINE_TURN_TIMEOUT)).text == "ok"
        [view] = told[-1].tasks
        assert (told[-1].session_id, view.trigger, view.running) == (session_id, "user", True)
        assert server.is_running()

        await host.event_bus.publish(BackgroundTaskStop(task_id=view.id, session_id=session_id))
        await wait_for(lambda: not told[-1].tasks[0].running, timeout=ENGINE_TURN_TIMEOUT, description="stopped")
        assert told[-1].tasks[0].stopped_by == "user"
        await wait_for(lambda: not server.is_running(), timeout=ENGINE_TURN_TIMEOUT, description="the server exited")
    finally:
        await host.shutdown()


async def test_main_agents_sleep_returns_once_the_user_sends_a_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("sleep", "call-1", {"seconds": 600, "reason": "wait"})]),
            MockResponse(text="done"),
        ]
    )
    host = _host(monkeypatch, tmp_path, client, builtins=["sleep"])
    sleeping = asyncio.Event()

    async def on_start(event: InvocationToolCallStart) -> None:
        if event.tool_name == "sleep":
            sleeping.set()

    await host.event_bus.subscribe(InvocationToolCallStart, on_start)
    try:
        turn = asyncio.create_task(host.run_until_final("wait for me"))
        await wait_for(lambda: sleeping.is_set() or turn.done(), timeout=ENGINE_TURN_TIMEOUT, description="asleep")
        assert not turn.done()
        await host.event_bus.publish(UserInject(text="check the logs now", injection_id="inject-1"))

        result = await asyncio.wait_for(turn, ENGINE_TURN_TIMEOUT)
    finally:
        await host.shutdown()

    assert result.text == "done"
    sent = client.call_history[1][0]
    [slept] = [content.result for message in sent for content in message.contents if content.type == "function_result"]
    assert re.fullmatch(
        r"Sleep ended early after \d+ seconds? \(requested 600 seconds\)\.\nThe user sent a new message\.", slept
    )
    assert sent[-1].text.endswith("check the logs now")
