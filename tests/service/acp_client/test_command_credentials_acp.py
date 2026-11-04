# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``{{!command}}`` values in an external ACP agent's arguments and environment, at each launch."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import psutil
import pytest

import chrys.service.acp_client.client as client_mod
import chrys.service.command_credentials as credentials_module
from chrys.foundation.util.env_templates import resolve_env_templates
from chrys.service.acp_client import (
    AcpAgentClient,
    AcpAgentSpec,
    AcpClientError,
    AcpConfigError,
    AcpConnectError,
    AcpSpawnError,
)
from chrys.service.acp_client.spawn import AcpSpawnResult, with_fresh_tokens
from chrys.service.command_credentials import _CommandFailedError
from tests.support.waiting import wait_for

from .helpers import CallbackRecorder, make_spec

_WRITE_DOWN_LAUNCH = (
    "import json, os, sys\n"
    "with open(sys.argv[1], 'w', encoding='utf-8') as file:\n"
    "    json.dump({'arg': sys.argv[2], 'env': os.environ['TOKEN']}, file)\n"
)


class _Commands:
    """Stands in for running a command: answers from a script and counts the runs."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.outcomes: list[str | BaseException] = []
        self.runs = 0
        self.seconds = 0.0
        self.release: asyncio.Event | None = None
        monkeypatch.setattr(credentials_module, "_run_command", self._run)
        monkeypatch.setattr(credentials_module, "_credentials", {})

    async def _run(self, _command: str) -> str:
        self.runs += 1
        if self.seconds:
            await asyncio.sleep(self.seconds)
        if self.release is not None:
            await self.release.wait()
        outcome = self.outcomes[0] if len(self.outcomes) == 1 else self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> _Commands:
    return _Commands(monkeypatch)


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch) -> list[AcpAgentSpec]:
    """What each launch was asked to start with; it then stops there, with no process."""
    seen: list[AcpAgentSpec] = []

    async def stop_at_spawn(spec: AcpAgentSpec) -> AcpSpawnResult:
        # It yields, as a real spawn does: a handshake clock that has run out only fires at a yield.
        await asyncio.sleep(0)
        seen.append(spec)
        raise AcpSpawnError("Stopped by the test.")

    monkeypatch.setattr(client_mod, "spawn_acp_process", stop_at_spawn)
    return seen


def _handle(command: str = "tool get") -> str:
    return resolve_env_templates(f"{{{{!{command}}}}}", location="ACP environment 'TOKEN'")


def _spec(tmp_path: Path, handle: str, **fields: object) -> AcpAgentSpec:
    return dataclasses.replace(make_spec(tmp_path), args=(f"--key={handle}",), env={"TOKEN": handle}, **fields)


async def test_a_real_child_starts_with_the_token_and_the_spec_keeps_the_handle(
    tmp_path: Path, commands: _Commands
) -> None:
    commands.outcomes = ["good"]
    handle = _handle()
    record = tmp_path / "launch.json"
    spec = dataclasses.replace(
        make_spec(tmp_path),
        args=("-c", _WRITE_DOWN_LAUNCH, str(record), f"--key={handle}"),
        env={"TOKEN": f"Bearer {handle}"},
    )
    client = AcpAgentClient(spec, CallbackRecorder())

    # It exits without answering, which ends the handshake.
    with pytest.raises(AcpClientError):
        await client.connect()

    assert json.loads(record.read_text(encoding="utf-8")) == {"arg": "--key=good", "env": "Bearer good"}
    # What the audit records comes from this spec, so it never holds a token.
    assert client._spec is spec
    assert spec.args[-1] == f"--key={handle}"


async def test_a_spec_without_commands_is_returned_as_it_is(tmp_path: Path, commands: _Commands) -> None:
    spec = make_spec(tmp_path, extra_env={"TOKEN": "plain"})

    assert await with_fresh_tokens(spec) is spec
    assert commands.runs == 0


async def test_each_launch_gets_a_new_token(tmp_path: Path, commands: _Commands, launches: list[AcpAgentSpec]) -> None:
    commands.outcomes = ["first", "second"]
    spec = _spec(tmp_path, _handle())

    for _ in range(2):
        with pytest.raises(AcpSpawnError):
            await AcpAgentClient(spec, CallbackRecorder()).connect()

    assert [launch.env["TOKEN"] for launch in launches] == ["first", "second"]
    assert [launch.args for launch in launches] == [("--key=first",), ("--key=second",)]
    assert commands.runs == 2


async def test_a_command_that_fails_at_first_stops_the_launch_and_says_where(
    tmp_path: Path, commands: _Commands, launches: list[AcpAgentSpec]
) -> None:
    commands.outcomes = [_CommandFailedError("exited with code 3")]
    spec = _spec(tmp_path, _handle("tool --password hunter2"))

    with pytest.raises(AcpConfigError) as caught:
        await AcpAgentClient(spec, CallbackRecorder()).connect()

    assert "ACP environment 'TOKEN'" in str(caught.value)
    assert "exited with code 3" in str(caught.value)
    assert "hunter2" not in str(caught.value)
    assert launches == []


async def test_a_later_failure_launches_with_the_last_token(
    tmp_path: Path, commands: _Commands, launches: list[AcpAgentSpec]
) -> None:
    commands.outcomes = ["good", _CommandFailedError("exited with code 3")]
    spec = _spec(tmp_path, _handle())

    for _ in range(2):
        with pytest.raises(AcpSpawnError):
            await AcpAgentClient(spec, CallbackRecorder()).connect()

    assert [launch.env["TOKEN"] for launch in launches] == ["good", "good"]
    assert commands.runs == 2


async def test_a_command_slower_than_the_handshake_limit_does_not_use_it_up(
    tmp_path: Path, commands: _Commands, launches: list[AcpAgentSpec]
) -> None:
    commands.outcomes = ["good"]
    commands.seconds = 0.1
    spec = _spec(tmp_path, _handle(), handshake_timeout_seconds=0.01)

    # A handshake clock already running would end this as a timeout, before any launch.
    with pytest.raises(AcpSpawnError):
        await AcpAgentClient(spec, CallbackRecorder()).connect()

    assert [launch.env["TOKEN"] for launch in launches] == ["good"]


async def test_cancelling_while_the_command_runs_starts_nothing(
    tmp_path: Path, commands: _Commands, launches: list[AcpAgentSpec]
) -> None:
    commands.outcomes = ["good"]
    commands.release = asyncio.Event()
    handle = _handle()
    connecting = asyncio.create_task(AcpAgentClient(_spec(tmp_path, handle), CallbackRecorder()).connect())
    await wait_for(lambda: commands.runs == 1 or connecting.done(), description="the command to start")
    assert not connecting.done()
    running = credentials_module._credentials[handle].running
    assert running is not None

    connecting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await connecting

    commands.release.set()
    await running
    assert launches == []


async def test_closing_while_the_command_runs_leaves_no_agent_behind(
    tmp_path: Path, commands: _Commands, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands.outcomes = ["good"]
    commands.release = asyncio.Event()
    children: list[psutil.Process] = []
    real_spawn = client_mod.spawn_acp_process

    async def spawn_and_note(spec: AcpAgentSpec) -> AcpSpawnResult:
        spawned = await real_spawn(spec)
        children.append(psutil.Process(spawned.process.pid))
        return spawned

    monkeypatch.setattr(client_mod, "spawn_acp_process", spawn_and_note)
    spec = make_spec(tmp_path, extra_env={"TOKEN": _handle()})
    client = AcpAgentClient(spec, CallbackRecorder())
    connecting = asyncio.create_task(client.connect())
    await wait_for(lambda: commands.runs == 1 or connecting.done(), description="the command to start")
    assert not connecting.done()

    await client.force_close()
    commands.release.set()

    with pytest.raises(AcpConnectError):
        await connecting
    assert all(not child.is_running() for child in children)
