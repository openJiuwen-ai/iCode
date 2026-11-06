# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``/reload`` reconnecting MCP servers one by one, from the real TUI through the next model request."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
import pytest

from chrys.app.tui.screens.dialogs.agent_load import AgentLoadDialog
from chrys.foundation.events import types as ev
from chrys.foundation.i18n.formatting import format_message
from chrys.service.llm.mock import MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import ApprovalConfig, MCPServerConfig
from chrys.service.profiles.agents.serializer import save_profile
from chrys.service.profiles.models.env_bridge import activate_model_profile
from tests.app.tui.screens.test_manual_reload import ask_after_reload, slash_reload
from tests.app.tui.screens.test_prompt_shortcut_send import MAIN_MODEL, make_agent, running_session
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

pytestmark = pytest.mark.usefixtures("clear_model_profile_env")

PROBE_SERVER = Path(__file__).parents[3] / "support" / "mcp_reload_probe_server.py"
AGENT = "Writer"


def state_dir(tmp_path: Path, name: str = "first") -> Path:
    state = tmp_path / name
    state.mkdir(exist_ok=True)
    return state


def stdio_server(state: Path, name: str = "first", **fields) -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=[str(PROBE_SERVER), "--state-dir", str(state)],
        **fields,
    )


def flag(state: Path, name: str) -> None:
    (state / name).write_text("", encoding="utf-8")


def starts(state: Path) -> list[int]:
    path = state / "starts"
    return [int(line) for line in path.read_text(encoding="utf-8").split()] if path.exists() else []


def calls(state: Path) -> list[str]:
    path = state / "calls"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def agent_with(*servers: MCPServerConfig):
    profile = make_agent(AGENT)
    profile.approval = ApprovalConfig(default="auto", overrides={})
    profile.tools.mcp = list(servers)
    return profile


@asynccontextmanager
async def mcp_session(agent_engine, tmp_path, monkeypatch, *servers: MCPServerConfig):
    """The real app on an agent read from disk, so ``/reload`` picks up edits to its file."""
    save_profile(agent_with(*servers))
    agents = AgentProfileRegistry()
    agents.load_all()
    activate_model_profile(MAIN_MODEL)
    async with running_session(agent_engine, tmp_path, monkeypatch, registry=agents, startup_agent=AGENT) as session:
        yield session


async def reloaded(session) -> list[str]:
    """Run ``/reload`` to its end and return the summary lines it reported."""
    before = len(session.seen.of(ev.SettingsReloaded))
    session.bar.focus_input()
    await slash_reload(session)
    await wait_for(
        lambda: len(session.seen.of(ev.SettingsReloaded)) > before and session.app.screen is session.main,
        pilot=session.pilot,
        timeout=ENGINE_TURN_TIMEOUT,
        description="manual reload completed",
    )
    return [format_message(line) for line in session.seen.of(ev.SettingsReloaded)[-1].report]


def tools_sent(session) -> dict:
    """The tools of the last model request, by name."""
    _messages, options = session.model.call_history[-1]
    return {tool.name: tool.parameters() for tool in options.get("tools") or []}


async def call_tool(session, name: str, label: str) -> None:
    """A real call through the model's tool loop."""
    session.model.scripted = [
        MockResponse(tool_calls=[(name, f"call-{label}", {"label": label})]),
        MockResponse(text="Done."),
    ]
    await ask_after_reload(session, f"call {name}")


def running_children() -> set[int]:
    """Pids of this test's own live child processes; only a child can't have been recycled unseen."""
    return {child.pid for child in psutil.Process().children(recursive=True) if child.status() != psutil.STATUS_ZOMBIE}


async def closed(process: psutil.Process, session) -> None:
    await wait_for(
        lambda: not process.is_running(),
        pilot=session.pilot,
        timeout=ENGINE_TURN_TIMEOUT,
        description=f"process {process.pid} closed",
    )


async def test_a_changed_remote_schema_reaches_the_next_request(agent_engine, tmp_path, monkeypatch):
    state = state_dir(tmp_path)
    async with mcp_session(agent_engine, tmp_path, monkeypatch, stdio_server(state)) as session:
        (old_pid,) = starts(state)
        old_process = psutil.Process(old_pid)
        await ask_after_reload(session, "before")
        assert "extra" not in str(tools_sent(session)["probe"])
        flag(state, "extra_parameter")
        (state / "instructions").write_text("Mention probe first.", encoding="utf-8")

        report = await reloaded(session)

        assert not [line for line in report if "MCP server '" in line]
        await closed(old_process, session)
        await ask_after_reload(session, "after")
        assert "extra" in str(tools_sent(session)["probe"])
        request = session.model.call_history[-1][0]
        assert any("Mention probe first." in message.text for message in request)
        await call_tool(session, "probe", "fresh")
        (new_pid,) = [pid for pid in starts(state) if pid != old_pid]
        assert calls(state) == [f"{new_pid} fresh"]


@pytest.mark.parametrize("no_ping", [False, True], ids=["answers-ping", "method-not-found"])
async def test_a_failed_reconnect_keeps_the_healthy_connection_usable(agent_engine, tmp_path, monkeypatch, no_ping):
    state = state_dir(tmp_path)
    if no_ping:
        flag(state, "no_ping")
    async with mcp_session(agent_engine, tmp_path, monkeypatch, stdio_server(state)) as session:
        (old_pid,) = starts(state)
        flag(state, "refuse")

        report = await reloaded(session)

        assert len(starts(state)) == 2, "the reconnect was really tried"
        (line,) = [line for line in report if "MCP server '" in line]
        assert "'first'" in line and "using the existing connection" in line
        await call_tool(session, "probe", "fallback")
        assert calls(state) == [f"{old_pid} fallback"]
        assert "probe" in tools_sent(session)


async def test_a_failed_reconnect_withdraws_a_dead_connection_only(agent_engine, tmp_path, monkeypatch):
    first, second = state_dir(tmp_path), state_dir(tmp_path, "second")
    servers = stdio_server(first), stdio_server(second, "second", tool_name_prefix="second")
    async with mcp_session(agent_engine, tmp_path, monkeypatch, *servers) as session:
        process = psutil.Process(starts(first)[0])
        flag(first, "refuse")
        process.kill()
        await closed(process, session)

        report = await reloaded(session)

        (line,) = [line for line in report if "MCP server '" in line]
        assert "'first' is unavailable" in line and "existing connection:" in line
        await call_tool(session, "second_probe", "survivor")
        assert "probe" not in tools_sent(session) and "second_probe" in tools_sent(session)
        assert [call.split()[1] for call in calls(second)] == ["survivor"]


async def test_narrowed_tools_never_fall_back_to_the_old_connection(agent_engine, tmp_path, monkeypatch):
    first, second = state_dir(tmp_path), state_dir(tmp_path, "second")
    other = stdio_server(second, "second", tool_name_prefix="second")
    async with mcp_session(agent_engine, tmp_path, monkeypatch, stdio_server(first), other) as session:
        old_process = psutil.Process(starts(first)[0])
        flag(first, "refuse")
        save_profile(agent_with(stdio_server(first, allowed_tools=["probe"]), other))

        report = await reloaded(session)

        (line,) = [line for line in report if "MCP server '" in line]
        assert "'first' is unavailable" in line
        await closed(old_process, session)
        await call_tool(session, "second_probe", "survivor")
        assert "probe" not in tools_sent(session) and "second_probe" in tools_sent(session)


@pytest.mark.parametrize("change", ["removed", "disabled"])
async def test_a_removed_or_disabled_server_goes_away_at_once(agent_engine, tmp_path, monkeypatch, change):
    first, second = state_dir(tmp_path), state_dir(tmp_path, "second")
    other = stdio_server(second, "second", tool_name_prefix="second")
    async with mcp_session(agent_engine, tmp_path, monkeypatch, stdio_server(first), other) as session:
        process = psutil.Process(starts(first)[0])
        remaining = [other] if change == "removed" else [stdio_server(first, enabled=False), other]
        save_profile(agent_with(*remaining))

        report = await reloaded(session)

        assert not [line for line in report if "MCP server '" in line]
        await closed(process, session)
        await call_tool(session, "second_probe", "survivor")
        assert "probe" not in tools_sent(session) and "second_probe" in tools_sent(session)
        assert len(starts(first)) == 1, "nothing tried to reconnect it"


async def test_a_name_collision_fails_the_reload_and_leaves_the_running_connections(
    agent_engine, tmp_path, monkeypatch
):
    first, second = state_dir(tmp_path), state_dir(tmp_path, "second")
    async with mcp_session(agent_engine, tmp_path, monkeypatch, stdio_server(first)) as session:
        (old_pid,) = starts(first)
        save_profile(agent_with(stdio_server(first), stdio_server(second, "second")))
        session.bar.focus_input()
        await slash_reload(session)
        await wait_for(
            lambda: isinstance(session.app.screen, AgentLoadDialog) and session.app.screen.has_class("-error"),
            pilot=session.pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="the collision failed the build",
        )
        await click_when_settled(session.pilot, "#agent-load-ok")
        await wait_for(
            lambda: session.app.screen is session.main, pilot=session.pilot, description="failure acknowledged"
        )
        opened = {*starts(first), *starts(second)} - {old_pid}
        await wait_for(
            lambda: not opened & running_children(),
            pilot=session.pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="what the failed build opened closed",
        )
        assert old_pid in running_children()
        await call_tool(session, "probe", "still-running")
        assert calls(first) == [f"{old_pid} still-running"]


async def test_a_cancelled_reload_closes_what_it_opened_and_leaves_the_runtime(agent_engine, tmp_path, monkeypatch):
    state = state_dir(tmp_path)
    async with mcp_session(agent_engine, tmp_path, monkeypatch, stdio_server(state)) as session:
        (old_pid,) = starts(state)
        cache = session.engine._loader._mcp_cache
        flag(state, "hold")
        generation = session.main._services.session_generation()
        worker = asyncio.create_task(session.main._reload_settings_from_disk(session.main.chat_session_id, generation))
        await wait_for(
            lambda: len(starts(state)) == 2 or worker.done(),
            pilot=session.pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="the reconnect started its process",
        )
        assert not worker.done()
        (candidate_pid,) = [pid for pid in starts(state) if pid != old_pid]
        candidate = psutil.Process(candidate_pid)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(worker, ENGINE_TURN_TIMEOUT)
        assert not cache.refreshing
        (state / "hold").unlink()
        await closed(candidate, session)
        # Only an exiting app cancels this worker; its dialog goes with it there.
        session.main._events.cancel_agent_load()
        await wait_for(lambda: session.app.screen is session.main, pilot=session.pilot, description="dialog gone")

        await call_tool(session, "probe", "after-cancel")
        assert calls(state) == [f"{old_pid} after-cancel"]
        old_process = psutil.Process(old_pid)
        assert not [line for line in await reloaded(session) if "MCP server '" in line]
        await closed(old_process, session)
        await call_tool(session, "probe", "after-second-reload")
        assert not calls(state)[-1].startswith(str(old_pid))


@pytest.fixture
def http_server(tmp_path):
    """A real Streamable HTTP server that accepts one token, and whose connection stays healthy."""
    state = state_dir(tmp_path, "http")
    (state / "token").write_text("Bearer old-token-value", encoding="utf-8")
    port_file = state / "port"
    process = subprocess.Popen(
        [sys.executable, str(PROBE_SERVER), "--state-dir", str(state), "--port-file", str(port_file)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        yield state, port_file
    finally:
        process.kill()
        process.wait()


def http_config(port_file: Path, token: str) -> MCPServerConfig:
    return MCPServerConfig(
        name="first",
        transport="http",
        url=f"http://127.0.0.1:{port_file.read_text(encoding='utf-8')}/mcp",
        headers={"Authorization": f"Bearer {token}"},
        bypass_proxy=True,
    )


async def test_a_changed_token_never_falls_back_and_stays_out_of_the_report(
    agent_engine, tmp_path, monkeypatch, http_server
):
    state, port_file = http_server
    await wait_for(port_file.exists, timeout=ENGINE_TURN_TIMEOUT, description="HTTP server listening")
    second = state_dir(tmp_path, "second")
    other = stdio_server(second, "second", tool_name_prefix="second")
    config = http_config(port_file, "old-token-value")
    async with mcp_session(agent_engine, tmp_path, monkeypatch, config, other) as session:
        await call_tool(session, "probe", "with-old-token")
        assert [call.split()[1] for call in calls(state)] == ["with-old-token"]
        save_profile(agent_with(http_config(port_file, "new-token-value"), other))

        report = await reloaded(session)

        (line,) = [line for line in report if "MCP server '" in line]
        assert "'first' is unavailable" in line
        assert "token-value" not in line
        await call_tool(session, "second_probe", "survivor")
        assert "probe" not in tools_sent(session) and "second_probe" in tools_sent(session)
        assert [call.split()[1] for call in calls(state)] == ["with-old-token"]
